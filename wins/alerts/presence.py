"""
wins/alerts/presence.py
Shared helper for cross-container bot-presence signaling via a status file,
and healthcheck-enabled flag.

Any service container can call write_status(); the alerts container reads it
and updates the Discord bot's gateway presence accordingly.

Status values:
  "idle"      → bot shows green  (online)   — normal / waiting
  "ingesting" → bot shows yellow (idle)     — collecting signals
  "trading"   → bot shows red   (dnd)       — executing a trade

Healthcheck flag (default OFF on every startup):
  is_healthcheck_enabled() / set_healthcheck_enabled(bool)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Resolves to ./logs/bot_status.json both locally and inside Docker
# (Docker workdir=/app, volume ./logs → /app/logs).
_LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
STATUS_FILE = _LOG_DIR / "bot_status.json"
_HEALTHCHECK_FILE = _LOG_DIR / "healthcheck_enabled.json"


def write_status(status: str) -> None:
    """Write current system status from any service container."""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps({"status": status}))
    except Exception:
        pass  # non-critical — presence is cosmetic


def read_status() -> str:
    """Read current system status, defaulting to 'idle'."""
    try:
        return json.loads(STATUS_FILE.read_text()).get("status", "idle")
    except Exception:
        return "idle"


def is_healthcheck_enabled() -> bool:
    """Returns whether healthcheck DMs are currently enabled. Defaults to False."""
    try:
        return json.loads(_HEALTHCHECK_FILE.read_text()).get("enabled", False)
    except Exception:
        return False


def set_healthcheck_enabled(enabled: bool) -> None:
    """Persist healthcheck enabled state to shared file."""
    try:
        _HEALTHCHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEALTHCHECK_FILE.write_text(json.dumps({"enabled": enabled}))
    except Exception:
        pass
