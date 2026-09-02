from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fysio_paths import APP_ROOT, LOG_DIR, prepare_writable_layout


PREFERRED_PORT = 8765
HOST = "127.0.0.1"
INSTANCE_STATE_PATH = APP_ROOT / "fysio.instance.json"
MUTEX_NAME = "Local\\FysioApplicationSingleInstance"


class WindowsSingleInstance:
    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise OSError("Δεν δημιουργήθηκε το Windows mutex")
        return kernel32.GetLastError() != 183

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None


def configure_runtime_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fysio.runtime")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "fysio.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
        ))
        logger.addHandler(handler)
    return logger


def available_local_port(preferred: int = PREFERRED_PORT) -> int:
    for requested in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((HOST, requested))
            except OSError:
                if requested == preferred:
                    continue
                raise
            return int(probe.getsockname()[1])
    raise OSError("Δεν βρέθηκε διαθέσιμη localhost θύρα")


def application_is_ready(url: str, timeout: float = 0.75) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def open_browser_when_ready(
    url: str, logger: logging.Logger, *, attempts: int = 120, interval: float = 0.25,
) -> None:
    for _ in range(attempts):
        if application_is_ready(url):
            if not webbrowser.open(url, new=2):
                logger.warning("Ο default browser δεν επιβεβαίωσε το άνοιγμα")
            return
        time.sleep(interval)
    logger.error("Η εφαρμογή δεν έγινε διαθέσιμη εγκαίρως")


def write_instance_state(port: int) -> None:
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = INSTANCE_STATE_PATH.with_name(
        f".{INSTANCE_STATE_PATH.name}.{os.getpid()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps({"pid": os.getpid(), "port": port}) + "\n",
            encoding="utf-8",
        )
        temporary.replace(INSTANCE_STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def read_instance_state() -> dict[str, Any] | None:
    try:
        value = json.loads(INSTANCE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        port = int(value["port"])
        pid = int(value["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 1 <= port <= 65535 or pid <= 0:
        return None
    return {"port": port, "pid": pid}


def open_existing_instance(*, attempts: int = 80, interval: float = 0.25) -> bool:
    for _ in range(attempts):
        state = read_instance_state()
        if state:
            url = f"http://{HOST}:{state['port']}/"
            if application_is_ready(url):
                return bool(webbrowser.open(url, new=2))
        time.sleep(interval)
    return False


def remove_owned_instance_state() -> None:
    state = read_instance_state()
    if state and state["pid"] == os.getpid():
        INSTANCE_STATE_PATH.unlink(missing_ok=True)


def show_fatal_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Fysio", 0x10)
    else:
        print(message, file=sys.stderr)


def run() -> int:
    single_instance = WindowsSingleInstance()
    try:
        if not single_instance.acquire():
            if not open_existing_instance():
                show_fatal_error("Το Fysio εκτελείται ήδη, αλλά δεν ήταν δυνατή η επαναφορά του browser.")
                return 1
            return 0

        prepare_writable_layout()
        logger = configure_runtime_logging()
        try:
            port = available_local_port()
            write_instance_state(port)
            url = f"http://{HOST}:{port}/"

            from app import create_app
            from waitress import serve

            application = create_app()
            opener = threading.Thread(
                target=open_browser_when_ready,
                args=(url, logger),
                name="fysio-browser-opener",
                daemon=True,
            )
            opener.start()
            logger.info("Fysio started on localhost port %s", port)
            serve(application, host=HOST, port=port, threads=4)
            return 0
        except Exception as exc:
            logger.error("Fatal startup failure (%s)", type(exc).__name__)
            show_fatal_error("Το Fysio δεν μπόρεσε να ξεκινήσει. Ελέγξτε το αρχείο logs\\fysio.log.")
            return 1
        finally:
            remove_owned_instance_state()
    finally:
        single_instance.close()


if __name__ == "__main__":
    raise SystemExit(run())
