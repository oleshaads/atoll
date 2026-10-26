#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-app — одно окно: сам ставит зависимости, сам подключает GitHub,
сам создаёт канал связи с агентом Arena, сам пересылает сообщения в обе стороны,
умеет делать скриншоты и отправлять/забирать файлы.

Запуск:   python app.py          (или двойной клик по run.cmd)
Проверка: python app.py --selftest
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = os.environ.get("ARENA_REPO", "oleshaads/atoll")
CHAT_BRANCH = "chat"
OUT_BRANCH = "bridge-out"
STATE_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "atoll-agent" / "app_state.json"
POLL_SECONDS = 4
APP_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
#  Зависимости ставятся сами
# --------------------------------------------------------------------------- #


def ensure_deps() -> None:
    try:
        import rich  # noqa: F401
        return
    except ImportError:
        pass
    print("[setup] ставлю зависимости…")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "rich"]
    if subprocess.run(cmd).returncode != 0:
        print("Не смог поставить rich. Выполни вручную:  py -m pip install rich")
        raise SystemExit(1)


ensure_deps()

from rich.console import Console  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

if os.name == "nt":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

console = Console()
_print_lock = threading.Lock()


def say(*args, **kwargs) -> None:
    with _print_lock:
        console.print(*args, **kwargs)


# --------------------------------------------------------------------------- #
#  GitHub: установка и авторизация сами
# --------------------------------------------------------------------------- #


class GhError(RuntimeError):
    pass


def refresh_path() -> None:
    if os.name != "nt":
        return
    try:
        machine = os.environ.get("PATH", "")
        for hive in ("Machine", "User"):
            extra = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"[Environment]::GetEnvironmentVariable('PATH','{hive}')"],
                capture_output=True, timeout=30,
            ).stdout.decode("utf-8", "replace").strip()
            if extra and extra not in machine:
                machine = machine + os.pathsep + extra
        os.environ["PATH"] = machine
    except Exception:
        pass


def gh(*args: str, input_text: str | None = None, capture: bool = True, timeout: int = 90) -> str:
    if not shutil.which("gh"):
        raise GhError("gh не установлен")
    proc = subprocess.run(
        ["gh", *args],
        capture_output=capture,
        input=input_text.encode("utf-8") if input_text is not None else None,
        timeout=timeout,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace") if capture else ""
    err = (proc.stderr or b"").decode("utf-8", "replace") if capture else ""
    if proc.returncode != 0:
        raise GhError((err or out).strip() or f"gh {' '.join(args)} → код {proc.returncode}")
    return out


def api(path: str, method: str = "GET", fields: dict | None = None, raw: bool = False, allow_fail: bool = False) -> bytes | str:
    args = ["api", "-X", method, path]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw"]
    if fields:
        for key, value in fields.items():
            args += ["-f", f"{key}={value}"]
    try:
        if raw:
            proc = subprocess.run(["gh", *args], capture_output=True, timeout=180)
            if proc.returncode != 0:
                if allow_fail:
                    return b""
                raise GhError((proc.stderr or b"").decode("utf-8", "replace").strip())
            return proc.stdout
        return gh(*args)
    except (GhError, subprocess.TimeoutExpired) as exc:
        if allow_fail:
            return "" if not raw else b""
        raise GhError(str(exc)) from exc


def install_gh() -> bool:
    say(Text("[setup] gh не найден — ставлю GitHub CLI через winget…", style="yellow"))
    if not shutil.which("winget"):
        say(Text("winget отсутствует. Установи GitHub CLI вручную: https://cli.github.com", style="red"))
        return False
    subprocess.run(
        ["winget", "install", "--id", "GitHub.cli", "-e", "--silent",
         "--accept-package-agreements", "--accept-source-agreements"],
        check=False,
    )
    refresh_path()
    return bool(shutil.which("gh"))


def ensure_auth(interactive: bool = True) -> str:
    """Возвращает login пользователя, при необходимости проводит вход."""
    try:
        login = gh("api", "user", "--jq", ".login").strip()
        if login:
            return login
    except GhError:
        pass
    if not interactive:
        raise GhError("нет авторизации в GitHub (gh auth login)")
    say(Panel(
        Text(
            "Один раз нужно войти в GitHub. Сейчас запустится мастер — открой ссылку,\n"
            "введи показанный код и разреши доступ. Дальше приложение всё помнит.",
            style="yellow",
        ),
        border_style="yellow", title="вход в GitHub", title_align="left",
    ))
    code = subprocess.run(
        ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https",
         "--web", "--scopes", "repo,workflow"],
        check=False,
    ).returncode
    if code != 0:
        raise GhError("вход не завершён")
    return gh("api", "user", "--jq", ".login").strip()


# --------------------------------------------------------------------------- #
#  Канал связи: ветка chat в репозитории
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_agent_seq": 0, "last_user_seq": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def ensure_chat_branch() -> None:
    refs = api(f"repos/{REPO}/git/ref/heads/{CHAT_BRANCH}", allow_fail=True)
    if refs:
        return
    main = json.loads(api(f"repos/{REPO}/git/ref/heads/main") or "{}")
    sha = (main.get("object") or {}).get("sha")
    if not sha:
        raise GhError("не смог определить sha ветки main")
    api(
        f"repos/{REPO}/git/refs",
        "POST",
        {"ref": f"refs/heads/{CHAT_BRANCH}", "sha": sha},
        allow_fail=True,
    )


def list_chat() -> list[int]:
    raw = api(f"repos/{REPO}/contents/chat?ref={CHAT_BRANCH}", allow_fail=True)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    seqs = []
    for item in items if isinstance(items, list) else []:
        match = re.match(r"^(\d+)\.json$", item.get("name", ""))
        if match:
            seqs.append(int(match.group(1)))
    return sorted(seqs)


def read_chat(seq: int) -> dict | None:
    raw = api(f"repos/{REPO}/contents/chat/{seq:06d}.json?ref={CHAT_BRANCH}", raw=True, allow_fail=True)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def write_chat(seq: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    api(
        f"repos/{REPO}/contents/chat/{seq:06d}.json",
        "PUT",
        {
            "message": f"chat {seq:06d} ({payload.get('from')})",
            "content": base64.b64encode(body).decode("ascii"),
            "branch": CHAT_BRANCH,
        },
    )


def publish_file(path: Path, name: str | None = None) -> str:
    """Отправить файл агенту: кладём в ветку bridge-out."""
    data = path.read_bytes()
    target = name or path.name
    api(
        f"repos/{REPO}/contents/{target}",
        "PUT",
        {
            "message": f"upload {target}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": OUT_BRANCH,
        },
        allow_fail=True,
    )
    return target


def fetch_file(name: str) -> bytes:
    return api(f"repos/{REPO}/contents/{name}?ref={OUT_BRANCH}", raw=True, allow_fail=True) or b""


# --------------------------------------------------------------------------- #
#  Локальные возможности
# --------------------------------------------------------------------------- #


def run_local(command: str, cwd: str | None = None) -> tuple[str, int]:
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    else:
        argv = ["/bin/bash", "-lc", command]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=900, cwd=cwd)
    except subprocess.TimeoutExpired:
        return "таймаут 900с", 1
    out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    return out.strip() or "(нет вывода)", proc.returncode


def screenshot() -> Path:
    target = Path(os.environ.get("TEMP", ".")) / f"shot-{time.strftime('%H%M%S')}.png"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
        "$g=[System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
        f"$bmp.Save('{target}',[System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=120)
    if not target.exists():
        raise GhError("скриншот не получился")
    return target


# --------------------------------------------------------------------------- #
#  Приложение
# --------------------------------------------------------------------------- #

HELP = """\
**Что писать**
просто текст — уйдёт агенту в Arena (ответ придёт сам, как только он прочитает)
`!команда` — выполнить локально сразу
`/shot` — скриншот экрана и сразу отправить агенту
`/send <путь>` — отправить файл агенту
`/get <имя>` — забрать файл, который агент положил в репозиторий
`/sync` — проверить ответы сейчас
`/status` — состояние канала
`/help` — эта справка
`/exit` — выход

**Как приходит ответ**
Агент читает ветку `chat` в репозитории, когда ты пишешь ему в чате Arena «проверь».
Пока чат Arena закрыт, ответа не будет — это ограничение, а не баг.
"""


def banner(login: str, seq: int) -> None:
    body = Text()
    body.append("agent-app", style="bold cyan")
    body.append("   связь через GitHub\n", style="dim")
    body.append("репозиторий ", style="dim")
    body.append(REPO, style="bold")
    body.append("   ветка ", style="dim")
    body.append(CHAT_BRANCH, style="bold")
    body.append("   аккаунт ", style="dim")
    body.append(login, style="bold")
    body.append("\nсообщений в канале: ", style="dim")
    body.append(str(seq), style="bold")
    body.append("   /help — команды, /exit — выход", style="dim")
    say(Panel(body, border_style="cyan", padding=(0, 1)))


def show_message(payload: dict) -> None:
    who = payload.get("from")
    text = str(payload.get("text", ""))
    stamp = payload.get("ts", "")[11:19] if payload.get("ts") else ""
    if who == "agent":
        say(Panel(Markdown(text or "_(пусто)_"), border_style="blue",
                  title=f"[blue]агент[/] · {stamp}", title_align="left"))
    elif who == "user":
        say(Panel(Text(text), border_style="green", title=f"[green]ты[/] · {stamp}", title_align="left"))
    elif who == "system":
        say(Text(f"  {text}", style="dim"))


def poller(state: dict, stop: threading.Event) -> None:
    while not stop.is_set():
        stop.wait(POLL_SECONDS)
        if stop.is_set():
            break
        try:
            seqs = [s for s in list_chat() if s > state.get("last_agent_seq", 0)]
            for seq in seqs:
                payload = read_chat(seq)
                state["last_agent_seq"] = max(state.get("last_agent_seq", 0), seq)
                if not payload or payload.get("from") == "user":
                    continue
                show_message(payload)
                save_state(state)
        except Exception:
            continue


def status(state: dict) -> None:
    try:
        runs = json.loads(api(f"repos/{REPO}/actions/runs?per_page=1"))
        run = (runs.get("workflow_runs") or [None])[0]
    except Exception:
        run = None
    lines = [
        f"репозиторий: {REPO}",
        f"канал: ветка {CHAT_BRANCH}, последнее сообщение: {state.get('last_agent_seq', 0)}",
        f"файлы: ветка {OUT_BRANCH}",
    ]
    if run:
        lines.append(f"последний прогон: {run.get('status')} / {run.get('conclusion') or '-'}")
    say(Panel(Text("\n".join(lines)), border_style="cyan", title="статус", title_align="left"))


def selftest() -> int:
    say(Text("agent-app — самопроверка", style="bold"))
    results: list[tuple[str, bool, str]] = []
    results.append(("python", sys.version_info >= (3, 9), sys.version.split()[0]))
    refresh_path()
    gh_ok = bool(shutil.which("gh"))
    results.append(("gh установлен", gh_ok, shutil.which("gh") or "нет"))
    if not gh_ok:
        for name, ok, detail in results:
            say(Text(f"  {name}: {'ok' if ok else 'провал'} ({detail})"))
        return 1
    try:
        login = ensure_auth(interactive=False)
        results.append(("авторизация", True, login))
    except GhError as exc:
        results.append(("авторизация", False, str(exc)))
        for name, ok, detail in results:
            say(Text(f"  {name}: {'ok' if ok else 'провал'} ({detail})"))
        return 1
    try:
        ensure_chat_branch()
        before = list_chat()
        seq = (max(before) if before else 0) + 1
        write_chat(seq, {"from": "user", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": "selftest"})
        got = read_chat(seq)
        results.append(("канал chat: запись/чтение", bool(got and got.get("text") == "selftest"), f"seq {seq}"))
    except Exception as exc:
        results.append(("канал chat", False, str(exc)))
    try:
        shot = screenshot()
        results.append(("скриншот", shot.exists(), f"{shot.stat().st_size // 1024} KB"))
    except Exception as exc:
        results.append(("скриншот", False, str(exc)))
    failed = 0
    for name, ok, detail in results:
        if not ok:
            failed += 1
        say(Text(f"  [{ 'green' if ok else 'red'}]{'ok' if ok else 'провал'}[/] {name} — {detail}"))
    say(Text("всё готово" if not failed else f"провалов: {failed}", style="green" if not failed else "red"))
    return 0 if not failed else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    refresh_path()
    if not shutil.which("gh") and not install_gh():
        return 2
    try:
        login = ensure_auth(interactive=True)
    except GhError as exc:
        say(Panel(Text(str(exc), style="red"), border_style="red", title="GitHub"))
        return 2

    state = load_state()
    try:
        ensure_chat_branch()
        seqs = list_chat()
    except GhError as exc:
        say(Panel(Text(str(exc), style="red"), border_style="red", title="канал недоступен"))
        return 2

    banner(login, seqs[-1] if seqs else 0)
    if seqs:
        recent = [s for s in seqs if s >= max(1, seqs[-1] - 8)]
        say(Text("--- последние сообщения ---", style="dim"))
        for s in recent:
            payload = read_chat(s)
            if payload:
                show_message(payload)
        state["last_agent_seq"] = seqs[-1]
        save_state(state)

    stop = threading.Event()
    thread = threading.Thread(target=poller, args=(state, stop), daemon=True)
    thread.start()
    say(Text("пиши. ответы появятся сами, когда агент прочитает канал.", style="dim"))

    try:
        while True:
            try:
                line = console.input("[bold green]›[/bold green] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                say(Markdown(HELP))
                continue
            if line == "/status":
                status(state)
                continue
            if line == "/sync":
                try:
                    for s in [x for x in list_chat() if x > state.get("last_agent_seq", 0)]:
                        payload = read_chat(s)
                        state["last_agent_seq"] = s
                        if payload:
                            show_message(payload)
                    save_state(state)
                except Exception as exc:
                    say(Text(f"не смог проверить: {exc}", style="red"))
                continue
            if line == "/shot":
                try:
                    shot = screenshot()
                    name = publish_file(shot, f"shot-{time.strftime('%H%M%S')}.png")
                    say(Text(f"  скриншот отправлен агенту: {name} ({shot.stat().st_size // 1024} KB)", style="green"))
                except Exception as exc:
                    say(Text(f"  скриншот не удался: {exc}", style="red"))
                continue
            if line.startswith("/send "):
                raw = line[6:].strip().strip('"')
                path = Path(raw).expanduser()
                if not path.is_file():
                    say(Text(f"  нет такого файла: {path}", style="red"))
                    continue
                try:
                    name = publish_file(path)
                    say(Text(f"  отправлено: {name}", style="green"))
                except Exception as exc:
                    say(Text(f"  не отправилось: {exc}", style="red"))
                continue
            if line.startswith("/get "):
                name = line[5:].strip()
                data = fetch_file(name)
                if not data:
                    say(Text(f"  в репозитории нет {name}", style="red"))
                    continue
                target = Path.cwd() / Path(name).name
                target.write_bytes(data)
                say(Text(f"  сохранено: {target} ({len(data) // 1024} KB)", style="green"))
                continue
            if line.startswith("!"):
                out, code = run_local(line[1:].strip())
                say(Panel(Text(out), border_style="magenta" if code == 0 else "red", title="$ " + line[1:].strip()[:80]))
                continue

            try:
                seq = (max(list_chat()) if list_chat() else 0) + 1
                write_chat(seq, {"from": "user", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": line})
                state["last_user_seq"] = seq
                save_state(state)
                say(Text("  отправлено · напиши «проверь» в чате Arena, чтобы получить ответ", style="dim"))
            except Exception as exc:
                say(Text(f"  не отправилось: {exc}", style="red"))
    finally:
        stop.set()
        save_state(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
