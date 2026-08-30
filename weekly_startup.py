from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import webbrowser
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def configured_report_day() -> int:
    env_file = ROOT / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SCHEDULE_DAY="):
            return DAY_INDEX[line.partition("=")[2].strip().lower()[:3]]
    return DAY_INDEX["sat"]


def is_report_day(now: datetime | None = None) -> bool:
    return (now or datetime.now()).weekday() == configured_report_day()


def wait_for_server(port: int, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(1)
    return False


def main() -> int:
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(
        filename=ROOT / "data/startup.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / ".deps")
    server = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=ROOT,
        env=environment,
    )
    if is_report_day() and wait_for_server(8000):
        logging.info("Opening the Saturday report dashboard")
        webbrowser.open("http://127.0.0.1:8000")
    return server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
