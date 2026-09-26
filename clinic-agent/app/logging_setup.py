"""Structured JSON logs. Phone numbers are masked; message bodies are never logged at INFO+."""
from __future__ import annotations

import json
import logging
import re
import sys

_PHONE = re.compile(r"\+?\d{10,15}")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        msg = _PHONE.sub(lambda m: m.group(0)[:3] + "******" + m.group(0)[-4:], msg)
        out = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name, "msg": msg}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
