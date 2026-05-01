"""
wins/shared/logger.py
Structured JSON logger used by all services.
Each line written to logs/ is a JSON object for easy parsing.
"""
import logging
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from wins.shared.config import LOG_LEVEL

LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False  # prevent duplicate messages via root logger

    # stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_JsonFormatter())
    logger.addHandler(sh)

    # file handler — one file per service
    fh = logging.FileHandler(LOG_DIR / f"{name}.jsonl")
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    return logger
