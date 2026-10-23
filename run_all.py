"""Start the shared-database web storefront and, when configured, the bot."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
_STOP_REQUESTED = False


def load_dotenv() -> None:
    """Load simple KEY=value settings without adding a runtime dependency."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)


def bot_is_configured() -> bool:
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        try:
            import config

            token = str(getattr(config, "BOT_TOKEN", "") or "")
        except ModuleNotFoundError:
            token = ""
    return bool(
        token
        and "ВАШ_ТОКЕН" not in token
        and "YOUR_" not in token.upper()
    )


def stop_children(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.time() + 10
    for child in children:
        if child.poll() is None:
            try:
                child.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                child.kill()


def request_stop(_signum, _frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    load_dotenv()
    from database.migrations import run_migrations

    Path("logs").mkdir(exist_ok=True)
    run_migrations()

    children: list[subprocess.Popen] = []
    web_host = os.environ.get("WEB_HOST", "127.0.0.1")
    web_port = os.environ.get("WEB_PORT", "8080")
    commands: dict[str, list[str]] = {}
    if bot_is_configured():
        commands["bot"] = [sys.executable, "main.py"]
    else:
        print(
            "BOT_TOKEN не настроен: запускаю только сайт. "
            "Для Telegram Stars и работы бота заполните config.py.",
            flush=True,
        )
    commands["web"] = [
        sys.executable,
        "-m",
        "web.app",
    ]
    children: dict[str, subprocess.Popen] = {}

    try:
        for name, command in commands.items():
            env = os.environ.copy()
            env["WEB_HOST"] = web_host
            env["WEB_PORT"] = web_port
            children[name] = subprocess.Popen(command, cwd=ROOT, env=env)
        print(
            f"Сайт: http://{web_host}:{web_port} · бот запускается отдельным процессом, "
            "если BOT_TOKEN задан.",
            flush=True,
        )
        while not _STOP_REQUESTED:
            web_child = children["web"]
            web_result = web_child.poll()
            if web_result is not None:
                print(f"Веб-сервер остановился с кодом {web_result}.", flush=True)
                return int(web_result) if web_result else 1

            bot_child = children.get("bot")
            if bot_child is not None and bot_child.poll() is not None:
                print(
                    f"Процесс бота завершился с кодом {bot_child.returncode}; "
                    "сайт продолжит работать, бот будет перезапущен через 10 секунд.",
                    flush=True,
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not _STOP_REQUESTED:
                    if children["web"].poll() is not None:
                        break
                    time.sleep(0.5)
                if _STOP_REQUESTED:
                    break
                if children["web"].poll() is not None:
                    continue
                env = os.environ.copy()
                env["WEB_HOST"] = web_host
                env["WEB_PORT"] = web_port
                children["bot"] = subprocess.Popen(
                    commands["bot"], cwd=ROOT, env=env
                )
            time.sleep(0.5)
        return 0
    finally:
        stop_children(list(children.values()))


if __name__ == "__main__":
    raise SystemExit(main())
