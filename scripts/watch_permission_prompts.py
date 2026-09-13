#!/usr/bin/env python3
"""Bridge permission prompts until the running console loads the fixed version."""

import fcntl
import os
from pathlib import Path
import sys
import time


APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app  # noqa: E402


ASSETS_DIR = APP_DIR / ".data" / "terminal"
LOG_PATH = APP_DIR / ".data" / "permission-watcher.log"


def running_console_pid() -> int:
    result = app.run_command(
        ["ps", "-axo", "pid=,command="], timeout=20, check=False
    )
    marker = f"{APP_DIR / 'app.py'} --host"
    for line in result.stdout.splitlines():
        if marker not in line:
            continue
        try:
            return int(line.strip().split(None, 1)[0])
        except (IndexError, ValueError):
            continue
    return 0


def accept_visible_prompts() -> int:
    accepted = 0
    if not ASSETS_DIR.is_dir():
        return accepted
    app.TERMINAL_ASSETS_DIR = ASSETS_DIR
    for root in sorted(ASSETS_DIR.iterdir()):
        marker = root / "permission-status"
        log = root / "terminal.log"
        if marker.exists() or not log.is_file():
            continue
        screen_name = f"claude-eval-{root.name}"
        if not app.screen_session_running(screen_name):
            continue
        current = app.terminal_screen_text(root.name, screen_name)
        accept_input = app.container_permission_accept_input(current)
        if not accept_input:
            continue
        app.run_command(
            ["screen", "-S", screen_name, "-p", "0", "-X", "stuff", accept_input],
            timeout=20,
        )
        time.sleep(1)
        current = app.terminal_screen_text(root.name, screen_name)
        if (
            app.screen_session_running(screen_name)
            and not app.container_permission_accept_input(current)
        ):
            marker.write_text("accepted-by-fixed-watcher\n", encoding="utf-8")
            accepted += 1
            print(f"accepted {root.name}", flush=True)
    return accepted


def main() -> int:
    APP_DIR.joinpath(".data").mkdir(parents=True, exist_ok=True)
    log_file = LOG_PATH.open("a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    os.environ.setdefault("HOME", str(Path.home()))
    lock_path = APP_DIR / ".data" / "permission-watcher.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        print("permission watcher started", flush=True)
        screen_list = app.run_command(["screen", "-ls"], timeout=20, check=False)
        print(
            f"screen probe rc={screen_list.returncode}: {screen_list.stdout.strip()}",
            flush=True,
        )
        initial_console_pid = running_console_pid()
        print(f"watching console pid={initial_console_pid}", flush=True)
        while True:
            current_console_pid = running_console_pid()
            if (
                initial_console_pid
                and current_console_pid
                and current_console_pid != initial_console_pid
            ):
                break
            try:
                accept_visible_prompts()
            except Exception as exc:
                print(f"watcher error: {exc}", flush=True)
            time.sleep(0.25)
        print("new console process detected; watcher exiting", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
