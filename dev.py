#!/usr/bin/env python3
import os
import sys
import time
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

WATCH_SCRIPT = "bot.py"
RESTART_DEBOUNCE_SEC = 1.5
SHUTDOWN_TIMEOUT_SEC = 8


class RestartHandler(FileSystemEventHandler):
    def __init__(self, script_name):
        self.script_name = script_name
        self.process = None
        self.last_restart = 0.0
        self.start_process()

    def start_process(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

        print(f"🔄 Starting {self.script_name}...")
        self.process = subprocess.Popen(
            [sys.executable, self.script_name],
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        )
        self.last_restart = time.monotonic()

    def maybe_restart(self, path: str):
        if os.path.basename(path) != self.script_name:
            return
        if time.monotonic() - self.last_restart < RESTART_DEBOUNCE_SEC:
            return
        print(f"📝 Change in {path} – restarting...")
        self.start_process()

    def on_modified(self, event):
        if event.is_directory:
            return
        self.maybe_restart(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        self.maybe_restart(event.src_path)


if __name__ == "__main__":
    try:
        import watchdog  # noqa: F401
    except ImportError:
        print("❌ 'watchdog' is missing. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "watchdog"])

    handler = RestartHandler(WATCH_SCRIPT)
    observer = Observer()
    observer.schedule(handler, path=".", recursive=False)
    observer.start()
    print(f"👀 Watching '{WATCH_SCRIPT}' – edit and save to reload. Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        if handler.process:
            handler.process.terminate()
            try:
                handler.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handler.process.kill()
    observer.join()
