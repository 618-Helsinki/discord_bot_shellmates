from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

APP_LOGGER_NAME = "bot"
AUDIT_LOGGER_NAME = "bot.audit"

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in _SECRETS:
            text = text.replace(secret, "***")
        return text


def setup_logging(log_dir: Path = Path("logs"), level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    formatter = RedactingFormatter(fmt)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.handlers.clear()
    app_logger.addHandler(console)
    app_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    app_logger.propagate = False

    discord_logger = logging.getLogger("discord")
    discord_logger.handlers.clear()
    discord_logger.addHandler(console)
    discord_logger.setLevel(logging.WARNING)
    discord_logger.propagate = False

    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.handlers.clear()
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "sync.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    audit_logger.addHandler(file_handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


def audit_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={json.dumps(str(value), ensure_ascii=False)}")
    logger.info(" ".join(parts))
