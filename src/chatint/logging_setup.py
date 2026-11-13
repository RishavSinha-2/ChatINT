from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import re
import sys
import threading
from pathlib import Path
from typing import Optional

from chatint.config import DATA_DIR

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+"),
    re.compile(r"(AIza[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._-]{6})[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"([A-Z0-9_]*API_KEY\s*=\s*)([^\s\"']+|\"[^\"]*\"|'[^']*')", re.IGNORECASE),
]

_LOGGING_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()


def scrub_secrets(text: str) -> str:
    if not text:
        return text
    out = str(text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(r"\1***", out)
    return out


class _SecretScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = scrub_secrets(msg)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass
        return True


def get_logs_dir() -> Path:
    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def configure_logging(log_dir: Optional[Path] = None) -> logging.Logger:
    global _LOGGING_CONFIGURED
    logger = logging.getLogger("chatint")
    with _CONFIG_LOCK:
        if _LOGGING_CONFIGURED:
            return logger

        logger.setLevel(logging.INFO)
        logger.propagate = False

        target_dir = log_dir or get_logs_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / "chatint.log"

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s"
        )
        scrub_filter = _SecretScrubFilter()

        try:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(scrub_filter)
            logger.addHandler(file_handler)
        except Exception:
            pass

        if not getattr(sys, "frozen", False):
            stream_handler = logging.StreamHandler()
            stream_handler.setLevel(logging.WARNING)
            stream_handler.setFormatter(formatter)
            stream_handler.addFilter(scrub_filter)
            logger.addHandler(stream_handler)

        orig_sys_hook = sys.excepthook
        orig_thread_hook = getattr(threading, "excepthook", None)

        def _sys_excepthook(exc_type, exc_value, exc_tb):
            if not issubclass(exc_type, KeyboardInterrupt):
                logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
            if orig_sys_hook:
                orig_sys_hook(exc_type, exc_value, exc_tb)

        def _thread_excepthook(args):
            if not issubclass(args.exc_type, SystemExit):
                logger.critical(
                    "Uncaught thread exception in %s",
                    getattr(args.thread, "name", "unknown"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            if orig_thread_hook:
                orig_thread_hook(args)

        sys.excepthook = _sys_excepthook
        if hasattr(threading, "excepthook"):
            threading.excepthook = _thread_excepthook

        _LOGGING_CONFIGURED = True
        return logger


def get_logger(name: str = "chatint") -> logging.Logger:
    configure_logging()
    return logging.getLogger(name if name.startswith("chatint") else f"chatint.{name}")
