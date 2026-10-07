"""Logging configuration: rotating file + stderr, with secrets scrubbed."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(password[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(_token[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(cf-turnstile-response=)[^&\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"([?&]sig=)[^&\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(/download/)eyJ[A-Za-z0-9_\-]+", re.IGNORECASE), r"\1<ticket>"),
    (re.compile(r"(ankergames_session=)[^;\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(XSRF-TOKEN=)[^;\s]+", re.IGNORECASE), r"\1***"),
]


def scrub(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def setup_logging(log_file: Path, level: str = "INFO", *, console: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    fmt = _ScrubbingFormatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(stream)

    for noisy in ("urllib3", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_level(level: str) -> None:
    value = getattr(logging, level.upper(), logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setLevel(value)
