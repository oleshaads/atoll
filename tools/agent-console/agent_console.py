#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-console — локальный ИИ-агент в терминале.

Что умеет:
  * болтать с моделью (Ollama локально / Anthropic / любой OpenAI-совместимый API);
  * выполнять команды на этой машине, читать и писать файлы, искать по коду;
  * спрашивать подтверждение перед опасными действиями (режимы ask / smart / yolo);
  * сохранять историю и продолжать сессию.

Запуск:      python agent_console.py
Настройка:   python agent_console.py --init
Проверка:    python agent_console.py --selftest
Разово:      python agent_console.py -p "покажи версию dotnet"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover
    print("Не хватает библиотеки rich. Установи:  py -m pip install rich")
    raise SystemExit(1)

APP = "atoll-agent"
VERSION = "1.0.0"
DEFAULT_MAX_TURNS = 40
OUTPUT_LIMIT = 16000

if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Конфиг и сессии
# --------------------------------------------------------------------------- #


def app_dir() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / (".config" if os.name != "nt" else "AppData/Roaming")
    path = root / APP
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_PATH = None  # вычисляется лениво


def config_path() -> Path:
    global CONFIG_PATH
    if CONFIG_PATH is None:
        CONFIG_PATH = app_dir() / "config.json"
    return CONFIG_PATH


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "ollama",
    "model": "qwen2.5-coder:7b",
    "base_url": None,
    "api_key": None,
    "approval": "smart",
    "max_tokens": 4096,
    "system_extra": "",
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"config.json повреждён ({exc}), использую значения по умолчанию")
    env_map = {
        "provider": "ATOLL_AGENT_PROVIDER",
        "model": "ATOLL_AGENT_MODEL",
        "base_url": "ATOLL_AGENT_BASE_URL",
    }
    for key, env in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if not cfg.get("api_key"):
        if cfg.get("provider") == "anthropic":
            cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY")
        elif cfg.get("provider") == "openai":
            cfg["api_key"] = os.environ.get("OPENAI_API_KEY")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def sessions_dir() -> Path:
    path = app_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_session(session: "Session") -> Path:
    path = sessions_dir() / f"{session.id}.json"
    payload = {
        "id": session.id,
        "created": session.created,
        "provider": session.provider_name,
        "model": session.model,
        "cwd": session.cwd,
        "messages": session.messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_session(name: str) -> dict[str, Any] | None:
    files = sorted(sessions_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    target = files[-1] if name in ("last", "") else sessions_dir() / f"{name}.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Провайдеры моделей
# --------------------------------------------------------------------------- #


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return f"HTTP {exc.code}: {body[:900]}"


class Provider:
    name = "base"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, max_tokens: int = 4096):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.timeout = 300

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, Any]]:
        raise NotImplementedError

    # --- преобразования формата ------------------------------------------- #
    @staticmethod
    def to_openai(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            blocks = msg.get("content", [])
            if msg["role"] == "assistant":
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)},
                    }
                    for b in blocks
                    if b.get("type") == "tool_use"
                ]
                item: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    item["tool_calls"] = calls
                out.append(item)
            else:
                for b in blocks:
                    if b.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b.get("content", "")})
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if text:
                    out.append({"role": "user", "content": text})
        return out

    @staticmethod
    def to_ollama(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            blocks = msg.get("content", [])
            if msg["role"] == "assistant":
                item: dict[str, Any] = {"role": "assistant"}
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if text:
                    item["content"] = text
                calls = [
                    {"function": {"name": b["name"], "arguments": b.get("input", {})}}
                    for b in blocks
                    if b.get("type") == "tool_use"
                ]
                if calls:
                    item["tool_calls"] = calls
                out.append(item)
            else:
                for b in blocks:
                    if b.get("type") == "tool_result":
                        out.append({"role": "tool", "content": b.get("content", "")})
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if text:
                    out.append({"role": "user", "content": text})
        return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, max_tokens: int = 4096):
        super().__init__(model, api_key, base_url, max_tokens)
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        if not api_key:
            raise ProviderError("Нет ANTHROPIC_API_KEY — задай ключ или используй --provider ollama")

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.base_url + "/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key or "",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        pending: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    event = json.loads(payload)
                    etype = event.get("type")
                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending[event.get("index", 0)] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "json": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield ("text", delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta":
                            slot = pending.get(event.get("index", 0))
                            if slot is not None:
                                slot["json"] += delta.get("partial_json", "")
                    elif etype == "message_delta":
                        usage.update(event.get("usage") or {})
        except urllib.error.HTTPError as exc:
            raise ProviderError(_http_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Сеть недоступна: {exc.reason}") from exc

        calls = []
        for idx in sorted(pending):
            slot = pending[idx]
            try:
                args = json.loads(slot["json"]) if slot["json"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(slot["id"] or f"call_{idx}", slot["name"], args))
        if calls:
            yield ("tool_calls", calls)
        if usage:
            yield ("usage", usage)


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, max_tokens: int = 4096):
        super().__init__(model, api_key, base_url, max_tokens)
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        if not api_key:
            raise ProviderError("Нет ключа API — задай его в конфиге или через --api-key")

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + self.to_openai(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        slots: dict[int, dict[str, str]] = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if not payload:
                        continue
                    chunk = json.loads(payload)
                    if chunk.get("usage"):
                        yield ("usage", chunk["usage"])
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield ("text", delta["content"])
                    for call in delta.get("tool_calls") or []:
                        slot = slots.setdefault(int(call.get("index", 0)), {"id": "", "name": "", "args": ""})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            raise ProviderError(_http_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Сеть недоступна: {exc.reason}") from exc

        calls = []
        for idx in sorted(slots):
            slot = slots[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(slot["id"] or f"call_{idx}", slot["name"], args))
        if calls:
            yield ("tool_calls", calls)


class GitHubModelsProvider(OpenAIProvider):
    """Бесплатные модели GitHub Models (токен берётся из gh auth token)."""

    name = "github"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, max_tokens: int = 4096):
        token = api_key or os.environ.get("GITHUB_TOKEN") or _gh_token()
        if not token:
            raise ProviderError("Нужен токен GitHub: выполни gh auth login или задай GITHUB_TOKEN")
        super().__init__(model, token, base_url or "https://models.github.ai/inference", max_tokens)


def _gh_token() -> str | None:
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=30)
        token = proc.stdout.decode("utf-8", "replace").strip()
        return token or None
    except Exception:
        return None


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, max_tokens: int = 4096):
        super().__init__(model, api_key, base_url, max_tokens)
        base = base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        if not base.startswith("http"):
            base = "http://" + base
        self.base_url = base.rstrip("/")

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, Any]]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + self.to_ollama(messages),
            "stream": True,
            "options": {"num_ctx": 8192},
        }
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        calls: list[ToolCall] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("error"):
                        raise ProviderError(f"Ollama: {obj['error']}")
                    msg = obj.get("message") or {}
                    if msg.get("content"):
                        yield ("text", msg["content"])
                    for call in msg.get("tool_calls") or []:
                        fn = call.get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", fn.get("name", ""), args))
                    if obj.get("done") and obj.get("eval_count"):
                        yield ("usage", {"output_tokens": obj.get("eval_count")})
        except urllib.error.HTTPError as exc:
            raise ProviderError(_http_error(exc)) from exc
        except urllib.error.URLError as exc:
            hint = ""
            if "Connection refused" in str(exc.reason) or "10061" in str(exc.reason):
                hint = "\nПохоже, Ollama не запущен. Открой его или выполни: ollama serve"
            raise ProviderError(f"Не достучался до Ollama ({self.base_url}): {exc.reason}{hint}") from exc

        if calls:
            yield ("tool_calls", calls)


class FakeProvider(Provider):
    """Для --selftest: заведомо детерминированный ответ без сети."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__("fake")
        self.turn = 0

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, Any]]:
        self.turn += 1
        if self.turn == 1:
            yield ("text", "Проверяю инструменты…\n")
            cmd = "Write-Output 'agent-selftest OK'" if os.name == "nt" else "echo agent-selftest OK"
            yield ("tool_calls", [ToolCall("call_1", "run_command", {"command": cmd})])
        else:
            yield ("text", "**Готово.** Инструмент выполнен, цикл агента работает.")


def ollama_ready(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def ollama_models(base_url: str) -> list[str]:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def ensure_ollama(console: Console, base_url: str, model: str) -> None:
    """Поднять сервер Ollama при необходимости и скачать модель, если её нет."""
    if not ollama_ready(base_url):
        console.print(Text("запускаю Ollama…", style="dim"))
        exe = shutil.which("ollama")
        if exe:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        for _ in range(30):
            if ollama_ready(base_url):
                break
            time.sleep(1)
        if not ollama_ready(base_url):
            console.print(Panel(
                Text("Ollama не отвечает. Установи: winget install Ollama.Ollama\n"
                     "Или выбери другой мозг: /provider github"),
                border_style="yellow", title="нет локальной модели", title_align="left",
            ))
            return
    have = ollama_models(base_url)
    short = model.split(":")[0]
    if any(m == model or m.startswith(short) for m in have):
        return
    console.print(Text(f"модели {model} нет — качаю один раз, потом работает офлайн…", style="yellow"))
    exe = shutil.which("ollama")
    if not exe:
        console.print(Text("не нашёл команду ollama", style="red"))
        return
    with console.status(f"ollama pull {model}", spinner="dots"):
        proc = subprocess.run([exe, "pull", model], capture_output=True, timeout=7200)
    if proc.returncode != 0:
        console.print(Text((proc.stderr or b"").decode("utf-8", "replace")[:400], style="red"))
    else:
        console.print(Text(f"модель {model} готова", style="green"))


def make_provider(cfg: dict[str, Any]) -> Provider:
    kind = cfg.get("provider", "ollama")
    kwargs = {
        "model": cfg.get("model") or DEFAULT_CONFIG["model"],
        "api_key": cfg.get("api_key"),
        "base_url": cfg.get("base_url"),
        "max_tokens": int(cfg.get("max_tokens") or 4096),
    }
    if kind == "fake":
        return FakeProvider()
    if kind == "anthropic":
        return AnthropicProvider(**kwargs)
    if kind == "openai":
        return OpenAIProvider(**kwargs)
    if kind == "github":
        return GitHubModelsProvider(**kwargs)
    return OllamaProvider(**kwargs)


# --------------------------------------------------------------------------- #
#  Инструменты
# --------------------------------------------------------------------------- #


def _clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… обрезано, всего {len(text)} символов"


def shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        wrapper = (
            "$ProgressPreference='SilentlyContinue'; "
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
        )
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", wrapper]
    return ["/bin/bash", "-lc", command]


def tool_run_command(args: dict[str, Any], cwd: str) -> tuple[str, bool]:
    command = str(args.get("command", "")).strip()
    if not command:
        return "пустая команда", True
    timeout = int(args.get("timeout_sec") or 120)
    timeout = max(5, min(timeout, 900))
    started = time.time()
    try:
        proc = subprocess.run(
            shell_argv(command),
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"таймаут {timeout}с — команда прервана", True
    except FileNotFoundError as exc:
        return f"оболочка не найдена: {exc}", True
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    text = out
    if err.strip():
        text += ("\n" if text else "") + "[stderr]\n" + err
    elapsed = time.time() - started
    text = _clip(text.strip()) or "(нет вывода)"
    text += f"\n[exit={proc.returncode}, {elapsed:.1f}s]"
    return text, proc.returncode != 0


def tool_read_file(args: dict[str, Any], cwd: str) -> tuple[str, bool]:
    path = Path(str(args.get("path", ""))).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    if not path.exists():
        return f"нет такого файла: {path}", True
    if path.is_dir():
        return f"{path} — это папка, используй list_dir", True
    offset = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or 400)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return f"не смог прочитать: {exc}", True
    chunk = lines[offset - 1 : offset - 1 + limit]
    body = "\n".join(f"{offset + i:>5}  {line}" for i, line in enumerate(chunk))
    if not body:
        body = "(пусто)"
    tail = f"\n… всего строк: {len(lines)}"
    return _clip(body + tail), False


def tool_write_file(args: dict[str, Any], cwd: str) -> tuple[str, bool]:
    path = Path(str(args.get("path", ""))).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    content = str(args.get("content", ""))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        return f"не смог записать: {exc}", True
    return f"записано {len(content)} символов в {path}", False


def tool_list_dir(args: dict[str, Any], cwd: str) -> tuple[str, bool]:
    path = Path(str(args.get("path", "."))).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    if not path.exists():
        return f"нет такой папки: {path}", True
    pattern = str(args.get("pattern") or "*")
    rows = []
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as exc:
        return f"не смог прочитать папку: {exc}", True
    for entry in entries:
        if pattern not in ("*", "") and not entry.match(pattern):
            continue
        if entry.is_dir():
            rows.append(f"  {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            rows.append(f"  {entry.name}  ({size} B)")
        if len(rows) >= 400:
            rows.append("  … список обрезан")
            break
    return _clip(f"{path}:\n" + "\n".join(rows) if rows else f"{path}: пусто"), False


def tool_search_files(args: dict[str, Any], cwd: str) -> tuple[str, bool]:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return "нужен pattern (регулярка)", True
    root = Path(str(args.get("path", "."))).expanduser()
    if not root.is_absolute():
        root = Path(cwd) / root
    glob = str(args.get("glob") or "*")
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "-e", pattern, "-g", glob, str(root)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return "поиск прерван по таймауту", True
        text = proc.stdout.decode("utf-8", "replace").strip()
        return _clip(text or "совпадений нет"), False
    hits: list[str] = []
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"плохая регулярка: {exc}", True
    for file in root.rglob(glob):
        if not file.is_file():
            continue
        try:
            for num, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file}:{num}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        break
        except Exception:
            continue
        if len(hits) >= 200:
            break
    return _clip("\n".join(hits) or "совпадений нет"), False


TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": (
            "Выполнить команду в оболочке этой машины (PowerShell на Windows, bash на Unix) "
            "и получить stdout/stderr и код возврата."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Текст команды"},
                "timeout_sec": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 120)"},
            },
            "required": ["command"],
        },
        "func": tool_run_command,
        "mutating": True,
    },
    {
        "name": "read_file",
        "description": "Прочитать текстовый файл с номерами строк.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу (абсолютный или от текущей папки)"},
                "offset": {"type": "integer", "description": "С какой строки начать (с 1)"},
                "limit": {"type": "integer", "description": "Сколько строк прочитать (по умолчанию 400)"},
            },
            "required": ["path"],
        },
        "func": tool_read_file,
        "mutating": False,
    },
    {
        "name": "write_file",
        "description": "Создать или перезаписать текстовый файл целиком.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу"},
                "content": {"type": "string", "description": "Новое содержимое файла"},
            },
            "required": ["path", "content"],
        },
        "func": tool_write_file,
        "mutating": True,
    },
    {
        "name": "list_dir",
        "description": "Показать содержимое папки.",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к папке"},
                "pattern": {"type": "string", "description": "Фильтр по имени, например *.py"},
            },
            "required": ["path"],
        },
        "func": tool_list_dir,
        "mutating": False,
    },
    {
        "name": "search_files",
        "description": "Поиск по содержимому файлов (регулярное выражение), как grep.",
        "schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Регулярное выражение"},
                "path": {"type": "string", "description": "Где искать (папка)"},
                "glob": {"type": "string", "description": "Маска файлов, например *.py"},
            },
            "required": ["pattern"],
        },
        "func": tool_search_files,
        "mutating": False,
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


def tool_schemas(kind: str) -> list[dict[str, Any]]:
    if kind == "anthropic":
        return [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in TOOLS]
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
        for t in TOOLS
    ]


def describe_call(call: ToolCall) -> str:
    if call.name == "run_command":
        return str(call.input.get("command", ""))
    if call.name in ("read_file", "list_dir", "write_file"):
        return str(call.input.get("path", ""))
    if call.name == "search_files":
        return f"{call.input.get('pattern', '')}  в  {call.input.get('path', '.')}"
    return json.dumps(call.input, ensure_ascii=False)[:200]


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #


class StreamRenderer:
    """Живой панельный вывод ответа модели."""

    def __init__(self, console: Console, label: str):
        self.console = console
        self.label = label
        self.buffer = ""
        self.live: Live | None = None
        self.started = time.time()
        self._last = 0.0

    def _renderable(self):
        header = f"{self.label} · {time.time() - self.started:.0f}с"
        if self.buffer.strip():
            return Panel(Markdown(self.buffer), border_style="cyan", title=header, title_align="left")
        return Panel(Text("думаю…", style="dim italic"), border_style="cyan", title=header, title_align="left")

    def __enter__(self) -> "StreamRenderer":
        self.live = Live(self._renderable(), console=self.console, refresh_per_second=8, transient=True)
        self.live.start()
        return self

    def feed(self, chunk: str) -> None:
        self.buffer += chunk
        now = time.time()
        if self.live and now - self._last > 0.12:
            self._last = now
            self.live.update(self._renderable())

    def __exit__(self, *exc_info) -> None:
        if self.live:
            self.live.stop()
            self.live = None
        if self.buffer.strip():
            self.console.print(Panel(Markdown(self.buffer), border_style="cyan", title=self.label, title_align="left"))
        elif exc_info and exc_info[0] is None:
            self.console.print(Text("(пустой ответ)", style="dim"))


def banner(console: Console, cfg: dict[str, Any], cwd: str) -> None:
    body = Text()
    body.append("agent-console", style="bold cyan")
    body.append(f"  v{VERSION}\n", style="dim")
    body.append("провайдер ", style="dim")
    body.append(f"{cfg.get('provider')}", style="bold")
    body.append("   модель ", style="dim")
    body.append(f"{cfg.get('model')}\n", style="bold")
    body.append("режим ", style="dim")
    body.append(f"{cfg.get('approval')}", style="bold")
    body.append("   папка ", style="dim")
    body.append(str(cwd), style="bold")
    body.append("\n/help — команды, Ctrl+C — прервать, /exit — выход", style="dim")
    console.print(Panel(body, border_style="cyan", padding=(0, 1)))


HELP_TEXT = """\
**Команды**
`/help` — эта справка
`/mode ask|smart|yolo` — режим подтверждений
`/model <имя>` — сменить модель
`/provider <имя>` — ollama / anthropic / openai
`/cd <путь>` — сменить рабочую папку
`/clear` — очистить историю
`/status` — состояние сессии
`/exit` — выйти
`!команда` — выполнить локально, минуя модель

**Режимы**
`ask` — спрашивать перед каждой командой
`smart` — чтение без спроса, команды и запись с подтверждением
`yolo` — полный доступ без вопросов
"""


# --------------------------------------------------------------------------- #
#  Агент
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    provider_name: str = "ollama"
    model: str = ""
    cwd: str = field(default_factory=os.getcwd)
    messages: list[dict] = field(default_factory=list)
    approval: str = "smart"
    max_turns: int = DEFAULT_MAX_TURNS
    system_extra: str = ""
    tool_calls_total: int = 0

    def system_prompt(self) -> str:
        os_name = "Windows" if os.name == "nt" else ("macOS" if sys.platform == "darwin" else "Linux")
        shell = "PowerShell 5.1" if os.name == "nt" else "bash"
        extra = f"\n{self.system_extra}" if self.system_extra else ""
        return (
            f"Ты — консольный агент, работающий прямо на компьютере пользователя ({os_name}, оболочка {shell}).\n"
            f"Текущая папка: {self.cwd}\n"
            "У тебя есть инструменты: run_command, read_file, write_file, list_dir, search_files.\n"
            "Правила:\n"
            "- Действуй, а не советуй: если задачу можно выполнить инструментом — вызывай его.\n"
            "- Команды должны быть неинтерактивными (никаких пауз на ввод).\n"
            "- Не выполняй разрушительных операций (форматирование, удаление системных папок), если об этом не просят прямо.\n"
            "- Отвечай по-русски и коротко. Не пересказывай вывод команды, который пользователь уже видит.\n"
            "- Если чего-то не хватает (путь, имя, ключ) — сначала попробуй выяснить инструментами, и только потом спрашивай."
            f"{extra}"
        )


def ask_approval(console: Console, call: ToolCall, session: Session) -> bool:
    mode = session.approval
    spec = TOOL_BY_NAME.get(call.name)
    if mode == "yolo" or spec is None:
        return True
    if mode == "smart" and not spec.get("mutating"):
        return True
    console.print(
        Panel(
            Text(describe_call(call), style="bold"),
            title=f"[yellow]подтвердить: {call.name}[/yellow]",
            border_style="yellow",
            title_align="left",
        )
    )
    answer = Prompt.ask("  выполнить?", choices=["y", "a", "n"], default="y").lower()
    if answer == "a":
        session.approval = "yolo"
        console.print(Text("  режим переключён на yolo", style="dim"))
        return True
    return answer == "y"


def run_one_turn(console: Console, provider: Provider, session: Session) -> None:
    for _step in range(session.max_turns):
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        renderer = StreamRenderer(console, f"{provider.name} · {session.model}")
        with renderer:
            try:
                for kind, payload in provider.stream(
                    session.system_prompt(), session.messages, tool_schemas(provider.name)
                ):
                    if kind == "text":
                        renderer.feed(payload)
                        text_parts.append(payload)
                    elif kind == "tool_calls":
                        calls = list(payload)
                    elif kind == "usage":
                        pass
            except ProviderError as exc:
                console.print(Panel(Text(str(exc), style="red"), border_style="red", title="ошибка модели"))
                if session.messages and session.messages[-1]["role"] == "user":
                    session.messages.pop()
                return
            except KeyboardInterrupt:
                console.print(Text("\nпрервано", style="yellow"))
                return

        text = "".join(text_parts)
        blocks: list[dict[str, Any]] = []
        if text.strip():
            blocks.append({"type": "text", "text": text})
        for call in calls:
            blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
        if blocks:
            session.messages.append({"role": "assistant", "content": blocks})
        else:
            return
        if not calls:
            return

        results: list[dict[str, Any]] = []
        for call in calls:
            if not ask_approval(console, call, session):
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": "пользователь отклонил вызов", "is_error": True}
                )
                continue
            spec = TOOL_BY_NAME.get(call.name)
            if spec is None:
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": f"нет такого инструмента: {call.name}", "is_error": True}
                )
                continue
            started = time.time()
            try:
                output, is_error = spec["func"](call.input, session.cwd)
            except Exception as exc:  # noqa: BLE001
                output, is_error = f"инструмент упал: {exc}", True
            took = time.time() - started
            session.tool_calls_total += 1
            title = f"[magenta]{call.name}[/magenta] · {'ошибка' if is_error else 'ok'} · {took:.1f}с"
            style = "red" if is_error else "magenta"
            preview = output if len(output) < 3000 else output[:3000] + "\n…"
            console.print(Panel(Text(preview), border_style=style, title=title, title_align="left"))
            results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": bool(is_error)}
            )
        session.messages.append({"role": "user", "content": results})
    console.print(Text(f"достигнут лимит шагов ({session.max_turns})", style="yellow"))


def handle_slash(console: Console, session: Session, cfg: dict[str, Any], line: str, provider: Provider) -> tuple[bool, Provider]:
    parts = line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit", "/q"):
        return False, provider
    if cmd == "/help":
        console.print(Markdown(HELP_TEXT))
        return True, provider
    if cmd == "/mode":
        if arg in ("ask", "smart", "yolo"):
            session.approval = arg
            cfg["approval"] = arg
            save_config(cfg)
            console.print(Text(f"режим: {arg}", style="green"))
        else:
            console.print(Text("используй: /mode ask|smart|yolo", style="yellow"))
        return True, provider
    if cmd == "/model":
        if arg:
            session.model = arg
            cfg["model"] = arg
            save_config(cfg)
            try:
                provider = make_provider(cfg)
            except ProviderError as exc:
                console.print(Text(str(exc), style="red"))
            console.print(Text(f"модель: {arg}", style="green"))
        else:
            console.print(Text(f"текущая модель: {session.model}", style="dim"))
        return True, provider
    if cmd == "/provider":
        if arg in ("ollama", "anthropic", "openai", "github"):
            cfg["provider"] = arg
            save_config(cfg)
            try:
                provider = make_provider(cfg)
                session.provider_name = provider.name
                session.model = cfg.get("model", session.model)
                console.print(Text(f"провайдер: {arg}, модель: {session.model}", style="green"))
            except ProviderError as exc:
                console.print(Text(str(exc), style="red"))
        else:
            console.print(Text("используй: /provider ollama|github|anthropic|openai", style="yellow"))
        return True, provider
    if cmd == "/cd":
        target = Path(arg).expanduser() if arg else Path.home()
        if target.is_dir():
            session.cwd = str(target.resolve())
            console.print(Text(f"папка: {session.cwd}", style="green"))
        else:
            console.print(Text(f"нет такой папки: {target}", style="red"))
        return True, provider
    if cmd == "/clear":
        session.messages.clear()
        console.print(Text("история очищена", style="green"))
        return True, provider
    if cmd == "/status":
        table = Table(show_header=False, box=None)
        table.add_row("сессия", session.id)
        table.add_row("провайдер", f"{provider.name} / {session.model}")
        table.add_row("режим", session.approval)
        table.add_row("папка", session.cwd)
        table.add_row("сообщений", str(len(session.messages)))
        table.add_row("вызовов инструментов", str(session.tool_calls_total))
        console.print(Panel(table, border_style="cyan", title="статус", title_align="left"))
        return True, provider
    console.print(Text(f"неизвестная команда: {cmd} (см. /help)", style="yellow"))
    return True, provider


def interactive(console: Console, session: Session, cfg: dict[str, Any]) -> None:
    if cfg.get("provider", "ollama") == "ollama":
        ensure_ollama(console, cfg.get("base_url") or "http://localhost:11434", cfg.get("model") or DEFAULT_CONFIG["model"])
    provider = make_provider(cfg)
    banner(console, cfg, session.cwd)
    while True:
        try:
            line = console.input("[bold cyan]›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line.startswith("!"):
            command = line[1:].strip()
            output, is_error = tool_run_command({"command": command}, session.cwd)
            console.print(
                Panel(Text(output), border_style="red" if is_error else "magenta", title=f"$ {command}", title_align="left")
            )
            session.messages.append({"role": "user", "content": [{"type": "text", "text": f"Я выполнил вручную: {command}\nВывод:\n{output}"}]})
            continue
        if line.startswith("/"):
            alive, provider = handle_slash(console, session, cfg, line, provider)
            if not alive:
                break
            continue
        session.messages.append({"role": "user", "content": [{"type": "text", "text": line}]})
        run_one_turn(console, provider, session)
        save_session(session)
    save_session(session)
    console.print(Text(f"история сохранена: {sessions_dir() / (session.id + '.json')}", style="dim"))


def run_print(console: Console, session: Session, cfg: dict[str, Any], prompt: str) -> int:
    if cfg.get("provider", "ollama") == "ollama":
        ensure_ollama(console, cfg.get("base_url") or "http://localhost:11434", cfg.get("model") or DEFAULT_CONFIG["model"])
    try:
        provider = make_provider(cfg)
    except ProviderError as exc:
        console.print(Text(str(exc), style="red"))
        return 2
    session.messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
    run_one_turn(console, provider, session)
    save_session(session)
    return 0


def wizard(console: Console, cfg: dict[str, Any]) -> dict[str, Any]:
    console.print(Panel("Первичная настройка agent-console", border_style="cyan"))
    provider = Prompt.ask("Провайдер", choices=["ollama", "anthropic", "openai"], default=cfg.get("provider", "ollama"))
    cfg["provider"] = provider
    if provider == "ollama":
        cfg["model"] = Prompt.ask("Модель", default="qwen2.5-coder:7b")
        cfg["base_url"] = None
        cfg["api_key"] = None
        console.print(Text("Нужен установленный Ollama (ollama.com) и скачанная модель: ollama pull " + cfg["model"], style="dim"))
    elif provider == "anthropic":
        cfg["model"] = Prompt.ask("Модель", default="claude-sonnet-4-5")
        cfg["api_key"] = Prompt.ask("API-ключ (Enter — взять из ANTHROPIC_API_KEY)", default=cfg.get("api_key") or "", password=True)
        cfg["base_url"] = None
    else:
        cfg["model"] = Prompt.ask("Модель", default="gpt-4.1-mini")
        cfg["base_url"] = Prompt.ask("Base URL", default="https://api.openai.com/v1")
        cfg["api_key"] = Prompt.ask("API-ключ", default=cfg.get("api_key") or "", password=True)
    cfg["approval"] = Prompt.ask("Режим подтверждений", choices=["ask", "smart", "yolo"], default=cfg.get("approval", "smart"))
    save_config(cfg)
    console.print(Text(f"сохранено: {config_path()}", style="green"))
    return cfg


def selftest(console: Console) -> int:
    import tempfile

    console.print(Text(f"agent-console {VERSION} — самопроверка", style="bold"))
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python", sys.version_info >= (3, 9), sys.version.split()[0]))
    checks.append(("rich", True, "ok"))
    checks.append(("оболочка", shutil.which("powershell") is not None or os.name != "nt", "found"))
    checks.append(("конфиг", True, str(config_path())))

    with tempfile.TemporaryDirectory() as tmp:
        session = Session(provider_name="fake", model="fake", cwd=tmp, approval="yolo")
        session.messages.append({"role": "user", "content": [{"type": "text", "text": "selftest"}]})
        run_one_turn(console, FakeProvider(), session)
        tool_ok = any(
            block.get("type") == "tool_result" and "agent-selftest OK" in block.get("content", "")
            for msg in session.messages
            if msg["role"] == "user"
            for block in msg["content"]
        )
        checks.append(("цикл агента + инструмент", tool_ok, "run_command"))

        read_ok, _ = tool_write_file({"path": "probe.txt", "content": "привет"}, tmp)
        out, _ = tool_read_file({"path": "probe.txt"}, tmp)
        checks.append(("запись/чтение файла", "привет" in out, "probe.txt"))

    table = Table(show_header=True, box=None)
    table.add_column("проверка")
    table.add_column("итог")
    table.add_column("детали", style="dim")
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        table.add_row(name, "[green]ok[/green]" if ok else "[red]провал[/red]", detail)
    console.print(table)
    if failed:
        console.print(Text(f"провалено проверок: {failed}", style="red"))
        return 1
    console.print(Text("всё в порядке — можно запускать агента", style="green"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-console",
        description="Локальный ИИ-агент в терминале: команды, файлы, поиск.",
    )
    parser.add_argument("prompt", nargs="*", help="задача (если нужен разовый запуск)")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="выполнить и выйти")
    parser.add_argument("--provider", choices=["ollama", "github", "anthropic", "openai", "fake"])
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--cwd", help="рабочая папка")
    parser.add_argument("--mode", choices=["ask", "smart", "yolo"], help="режим подтверждений")
    parser.add_argument("--yes", action="store_true", help="то же, что --mode yolo")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--init", action="store_true", help="мастер настройки")
    parser.add_argument("--selftest", action="store_true", help="проверка установки")
    parser.add_argument("--resume", nargs="?", const="last", help="продолжить сессию (id или last)")
    parser.add_argument("--version", action="version", version=f"agent-console {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    cfg = load_config()

    if args.selftest:
        return selftest(console)

    prompt = " ".join(args.prompt).strip()
    wants_wizard = args.init or (not config_path().exists() and not args.print_mode and not prompt)
    if wants_wizard and sys.stdin.isatty():
        try:
            cfg = wizard(console, cfg)
        except (EOFError, KeyboardInterrupt):
            console.print(Text("\nнастройка пропущена, использую значения по умолчанию", style="yellow"))

    for key, value in (("provider", args.provider), ("model", args.model), ("base_url", args.base_url), ("api_key", args.api_key)):
        if value:
            cfg[key] = value
    if args.mode:
        cfg["approval"] = args.mode
    if args.yes:
        cfg["approval"] = "yolo"

    cwd = args.cwd or os.getcwd()
    if not Path(cwd).is_dir():
        console.print(Text(f"нет такой папки: {cwd}", style="red"))
        return 2

    session = Session(
        provider_name=cfg.get("provider", "ollama"),
        model=cfg.get("model", DEFAULT_CONFIG["model"]),
        cwd=str(Path(cwd).resolve()),
        approval=cfg.get("approval", "smart"),
        max_turns=args.max_turns or DEFAULT_MAX_TURNS,
        system_extra=cfg.get("system_extra") or "",
    )

    if args.resume:
        saved = load_session(args.resume)
        if saved:
            session.messages = saved.get("messages", [])
            session.cwd = saved.get("cwd", session.cwd)
            console.print(Text(f"продолжаю сессию {saved.get('id')} ({len(session.messages)} сообщений)", style="dim"))
        else:
            console.print(Text("сессия не найдена, начинаю новую", style="yellow"))

    if args.print_mode or (prompt and not sys.stdin.isatty()):
        if not prompt:
            console.print(Text("для -p нужен текст задачи", style="red"))
            return 2
        return run_print(console, session, cfg, prompt)

    interactive(console, session, cfg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nпрервано")
        raise SystemExit(130)
