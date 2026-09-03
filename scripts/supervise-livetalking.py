#!/usr/bin/env python3
"""Run LiveTalking and restart it when it exits or stops answering health checks."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
LIVETALKING_DIR = Path(
    os.environ.get("LIVETALKING_PATH", PROJECT_DIR.parent / "LiveTalking")
).expanduser().resolve()
PYTHON = LIVETALKING_DIR / ".venv" / "bin" / "python"
HEALTH_URL = os.environ.get("LIVETALKING_HEALTH_URL", "http://127.0.0.1:8010/health")
STARTUP_TIMEOUT = int(os.environ.get("LIVETALKING_STARTUP_TIMEOUT", "300"))
HEALTH_INTERVAL = float(os.environ.get("LIVETALKING_HEALTH_INTERVAL", "5"))
HEALTH_TIMEOUT = float(os.environ.get("LIVETALKING_HEALTH_TIMEOUT", "2"))
FAILURE_THRESHOLD = int(os.environ.get("LIVETALKING_HEALTH_FAILURES", "3"))
RESTART_DELAY = float(os.environ.get("LIVETALKING_RESTART_DELAY", "5"))

stopping = False
child = None


def log(message):
    print(f"[livetalking-supervisor] {message}", flush=True)


def request_stop(_signum, _frame):
    global stopping
    stopping = True


def healthy():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as response:
            payload = json.load(response)
            return response.status == 200 and payload.get("status") == "ready"
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return False


def stop_child():
    global child
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log("process did not stop cleanly; killing it")
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        child.wait(timeout=10)


def wait_or_stop(seconds):
    deadline = time.monotonic() + seconds
    while not stopping and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def run_once():
    global child
    command = [
        str(PYTHON),
        "app.py",
        "--transport",
        "webrtc",
        "--model",
        "wav2lip",
        "--avatar_id",
        "tutor_avatar_v3",
    ]
    log(f"starting LiveTalking from {LIVETALKING_DIR}")
    child = subprocess.Popen(command, cwd=LIVETALKING_DIR, start_new_session=True)

    startup_deadline = time.monotonic() + STARTUP_TIMEOUT
    while not stopping and child.poll() is None:
        if healthy():
            log("service is ready")
            break
        if time.monotonic() >= startup_deadline:
            log(f"service did not become ready within {STARTUP_TIMEOUT}s")
            stop_child()
            return
        wait_or_stop(HEALTH_INTERVAL)

    failures = 0
    while not stopping and child.poll() is None:
        wait_or_stop(HEALTH_INTERVAL)
        if stopping or child.poll() is not None:
            break
        if healthy():
            failures = 0
            continue
        failures += 1
        log(f"health check failed ({failures}/{FAILURE_THRESHOLD})")
        if failures >= FAILURE_THRESHOLD:
            log("service is unresponsive; restarting it")
            stop_child()
            return

    if child.poll() is not None:
        log(f"process exited with status {child.returncode}")


def main():
    if not (LIVETALKING_DIR / "app.py").is_file():
        raise SystemExit(f"LiveTalking app not found: {LIVETALKING_DIR / 'app.py'}")
    if not PYTHON.is_file():
        raise SystemExit(f"LiveTalking Python environment not found: {PYTHON}")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stopping:
            run_once()
            if not stopping:
                wait_or_stop(RESTART_DELAY)
    finally:
        stop_child()
    return 0


if __name__ == "__main__":
    sys.exit(main())
