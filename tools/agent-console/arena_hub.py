#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena-hub — чат с агентом Arena из твоего терминала.

Как это работает
----------------
Канал связи — GitHub Issue в твоём же репозитории. Ты пишешь сообщение здесь,
оно уходит комментарием в issue. Агент Arena читает issue, когда ты просишь его
об этом в чате Arena, и отвечает туда же. Приложение подхватывает ответ.

Чего здесь нет: автономности. Пока ты не откроешь чат в Arena, ответа не будет.

Запуск:  python arena_hub.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print("Нужна библиотека rich:  py -m pip install rich")
    raise SystemExit(1)

if os.name == "nt":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

REPO = os.environ.get("ARENA_HUB_REPO", "oleshaads/atoll")
ISSUE_TITLE = "agent-chat"
STATE_FILE = Path(os.environ.get("APPDATA", Path.home())) / "atoll-agent" / "hub_state.json"
POLL_SECONDS = 5
MARK_AGENT = "<!-- agent -->"
MARK_USER = "<!-- user -->"


# --------------------------------------------------------------------------- #
#  GitHub через gh CLI
# --------------------------------------------------------------------------- #


class GhError(RuntimeError):
    pass


def gh(*args: str, input_text: str | None = None) -> str:
    if not shutil.which("gh"):
        raise GhError("gh не установлен. Установи: winget install GitHub.cli")
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise GhError("gh не ответил за 60 секунд")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise GhError(err or f"gh {' '.join(args)} → код {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def ensure_auth(console: Console) -> None:
    try:
        gh("auth", "status")
        return
    except GhError:
        pass
    console.print(
        Panel(
            Text(
                "Нужно один раз войти в GitHub.\n\n"
                "Выполни в этом же окне (после выхода из приложения):\n"
                "  gh auth login\n\n"
                "Отвечай так: GitHub.com → HTTPS → Yes → Login with a web browser,\n"
                "дальше введи код в браузере. Потом запусти приложение снова.",
                style="yellow",
            ),
            title="требуется авторизация",
            border_style="yellow",
        )
    )
    raise SystemExit(2)


def find_issue(console: Console) -> int:
    raw = gh("issue", "list", "-R", REPO, "--state", "open", "--limit", "50", "--json", "number,title")
    issues = json.loads(raw or "[]")
    for item in issues:
        if item.get("title") == ISSUE_TITLE:
            return int(item["number"])
    raw = gh("issue", "create", "-R", REPO, "--title", ISSUE_TITLE, "--body", "Канал связи агента Arena и локального терминала.")
    for line in raw.splitlines():
        if "/issues/" in line:
            return int(line.rsplit("/", 1)[-1].strip())
    raise GhError(f"не смог определить номер issue из ответа: {raw.strip()[:200]}")


@dataclass
class Comment:
    id: int
    author: str
    body: str
    created: str


def fetch_comments(issue: int) -> list[Comment]:
    raw = gh("api", f"repos/{REPO}/issues/{issue}/comments?per_page=100", "--jq", ".[] | @json")
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        out.append(
            Comment(
                id=int(item["id"]),
                author=(item.get("user") or {}).get("login", "?"),
                body=item.get("body", ""),
                created=item.get("created_at", ""),
            )
        )
    return out


def post_comment(issue: int, body: str, mark: str) -> None:
    payload = json.dumps({"body": f"{mark}\n{body}"})
    gh("api", "-X", "POST", f"repos/{REPO}/issues/{issue}/comments", "--input", "-", input_text=payload)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_id": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #


def clean_body(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if line.strip() not in (MARK_AGENT, MARK_USER)).strip()


def show_comment(console: Console, c: Comment, me: str) -> None:
    mine = c.author == me
    title = f"[{'blue' if not mine else 'green'}]{c.author}[/] · {c.created[:16].replace('T', ' ')}"
    console.print(
        Panel(
            Markdown(clean_body(c.body) or "_(пусто)_"),
            border_style="blue" if not mine else "green",
            title=title,
            title_align="left",
        )
    )


def status_line(console: Console, issue: int, me: str) -> None:
    try:
        runs = json.loads(
            gh("api", f"repos/{REPO}/actions/runs?per_page=1", "--jq", "[.workflow_runs[] | {status, conclusion, created_at}]")
        )
        run = runs[0] if runs else None
    except GhError:
        run = None
    bits = [f"репозиторий [bold]{REPO}[/]", f"issue [bold]#{issue}[/]", f"аккаунт [bold]{me}[/]"]
    if run:
        bits.append(f"последний прогон: {run.get('status')}/{run.get('conclusion') or '-'}")
    console.print(Text.from_markup("  ·  ".join(bits), style="dim"))


def run_local(command: str, console: Console) -> None:
    console.print(Text(f"$ {command}", style="dim"))
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, timeout=900)
        out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        console.print(Panel(Text(out.strip() or "(нет вывода)"), border_style="magenta" if proc.returncode == 0 else "red"))
    except subprocess.TimeoutExpired:
        console.print(Text("таймаут 900с", style="red"))


def main() -> int:
    console = Console()
    console.print(
        Panel(
            Text.from_markup(
                "arena-hub\n\n"
                "Пиши сюда — сообщение уйдёт агенту.\n"
                "[bold]Важно:[/] агент читает его только когда ты откроешь чат Arena\n"
                "и напишешь там «проверь». Ответ появится здесь сам.\n\n"
                "[dim]/sync — проверить сейчас   /status — состояние   /exit — выход\n"
                "!команда — выполнить локально, минуя агента[/]"
            ),
            border_style="cyan",
            title="чат с агентом",
            title_align="left",
        )
    )

    ensure_auth(console)

    try:
        me = gh("api", "user", "--jq", ".login").strip()
        issue = find_issue(console)
    except GhError as exc:
        console.print(Panel(Text(str(exc), style="red"), border_style="red", title="ошибка GitHub"))
        return 2

    state = load_state()
    console.print()
    status_line(console, issue, me)

    # первичная синхронизация: показываем историю
    try:
        history = fetch_comments(issue)[-12:]
    except GhError as exc:
        console.print(Text(f"не смог прочитать историю: {exc}", style="red"))
        history = []
    if history:
        console.print(Text("--- последние сообщения ---", style="dim"))
        for c in history:
            show_comment(console, c, me)
        state["last_id"] = max(c.id for c in history)
        save_state(state)

    pending_reply = 0
    last_poll = 0.0
    console.print(Text("можно писать. Enter — отправить, Ctrl+C — выход.", style="dim"))

    while True:
        try:
            line = console.input("[bold green]ты ›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if line:
            if line in ("/exit", "/quit"):
                break
            if line == "/status":
                status_line(console, issue, me)
                continue
            if line == "/sync":
                last_poll = 0.0
            elif line.startswith("!"):
                run_local(line[1:].strip(), console)
                continue
            else:
                try:
                    post_comment(issue, line, MARK_USER)
                    console.print(Text("  отправлено агенту · жду ответа (напиши «проверь» в чате Arena)", style="dim"))
                    pending_reply += 1
                    last_poll = 0.0
                except GhError as exc:
                    console.print(Text(f"  не отправилось: {exc}", style="red"))
                continue

        if time.time() - last_poll < POLL_SECONDS:
            continue
        last_poll = time.time()
        try:
            comments = fetch_comments(issue)
        except GhError as exc:
            console.print(Text(f"  опрос не удался: {exc}", style="red"))
            continue
        new = [c for c in comments if c.id > state.get("last_id", 0)]
        for c in new:
            if c.author != me:
                show_comment(console, c, me)
                pending_reply = 0
            state["last_id"] = max(state.get("last_id", 0), c.id)
        if new:
            save_state(state)

    save_state(state)
    console.print(Text(f"состояние сохранено: {STATE_FILE}", style="dim"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
