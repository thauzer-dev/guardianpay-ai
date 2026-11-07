"""Logs estruturados em JSON (uma linha por evento). Campos sensíveis são mascarados."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_SENSITIVE = ("api_key", "token", "authorization", "password", "secret")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update({k: ("***" if any(s in k.lower() for s in _SENSITIVE) else v) for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"guardianpay.{name}")
    root = logging.getLogger("guardianpay")
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        root.addHandler(h)
        root.setLevel(logging.INFO)
        root.propagate = False
    return logger


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
