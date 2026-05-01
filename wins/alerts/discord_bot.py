"""
wins/alerts/discord_bot.py
Sends trade and system health notifications as Discord DMs.

Uses the Discord Bot API directly via httpx — no event loop or gateway
connection required. The bot simply opens a DM channel with your user
and posts embeds into it.

Setup (one-time, ~2 minutes):
    1. Go to https://discord.com/developers/applications
       → New Application → name it "WINS" → Bot (left sidebar) → Reset Token → copy it
    2. Under Bot → Privileged Gateway Intents, enable "Server Members Intent"
    3. Get YOUR user ID:
       Discord Settings → Advanced → enable Developer Mode
       then right-click your username anywhere → Copy User ID
    4. Add the bot to a server with the correct scopes (bot + slash commands):
       https://discord.com/oauth2/authorize?client_id=YOUR_APP_CLIENT_ID&scope=bot%20applications.commands&permissions=2048
       (Replace YOUR_APP_CLIENT_ID with the Application ID from the General Information page)
    5. Set in Doppler / .env:
       DISCORD_BOT_TOKEN=your_bot_token_here
       DISCORD_USER_ID=your_numeric_user_id
       DISCORD_GUILD_ID=your_server_id   # optional — enables instant slash command sync
"""
from __future__ import annotations

import httpx
from wins.shared.config import DISCORD_BOT_TOKEN, DISCORD_USER_ID
from wins.shared.logger import get_logger

log = get_logger("alerts")

_API      = "https://discord.com/api/v10"
_DM_CACHE: str | None = None   # cached DM channel ID for this process lifetime

# Embed colour constants
_GREEN  = 0x2ecc71
_RED    = 0xe74c3c
_YELLOW = 0xf1c40f
_BLUE   = 0x3498db
_DARK   = 0x2c2f33


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


async def _get_dm_channel() -> str | None:
    """Open (or retrieve cached) the DM channel with the configured user."""
    global _DM_CACHE
    if _DM_CACHE:
        return _DM_CACHE
    if not DISCORD_BOT_TOKEN or not DISCORD_USER_ID:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_API}/users/@me/channels",
                headers=_headers(),
                json={"recipient_id": DISCORD_USER_ID},
                timeout=10,
            )
            resp.raise_for_status()
            _DM_CACHE = resp.json()["id"]
            return _DM_CACHE
    except Exception as exc:
        log.warning(f"Discord: failed to open DM channel: {exc}")
        return None


async def _send(payload: dict) -> None:
    """Send a message payload to the DM channel. Skips if bot not configured."""
    if not DISCORD_BOT_TOKEN or not DISCORD_USER_ID:
        log.debug("Discord bot not configured — alert suppressed.")
        return
    channel_id = await _get_dm_channel()
    if not channel_id:
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_API}/channels/{channel_id}/messages",
                headers=_headers(),
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Discord DM failed: {exc}")


async def send_message(text: str) -> None:
    """Send a plain text DM."""
    await _send({"content": text})


async def _post(channel_id_unused: str, payload: dict) -> None:
    """Internal compat shim — routes all embeds through _send."""
    await _send(payload)


async def alert_trade_opened(
    token: str,
    action: str,
    entry: float,
    sl: float,
    tp: float,
    size_usd: float,
    confidence: float,
    reasoning: str,
    mode: str,
) -> None:
    sl_pct = (entry - sl) / entry * 100
    tp_pct = (tp - entry) / entry * 100
    rr     = tp_pct / sl_pct if sl_pct > 0 else 0

    await _send({
        "embeds": [{
            "title": f"{'🟢' if action == 'buy' else '🔴'} {action.upper()} {token}  [{mode.upper()}]",
            "color": _GREEN if action == "buy" else _RED,
            "fields": [
                {"name": "Entry",      "value": f"`${entry:.4f}`",                        "inline": True},
                {"name": "Stop Loss",  "value": f"`${sl:.4f}`  (-{sl_pct:.1f}%)",         "inline": True},
                {"name": "Target",     "value": f"`${tp:.4f}`  (+{tp_pct:.1f}%)",         "inline": True},
                {"name": "Position",   "value": f"`${size_usd:.2f}`",                     "inline": True},
                {"name": "Confidence", "value": f"`{confidence:.0%}`",                    "inline": True},
                {"name": "R:R",        "value": f"`{rr:.1f}x`",                           "inline": True},
                {"name": "Reasoning",  "value": reasoning[:1024] if reasoning else "—",   "inline": False},
            ],
            "footer": {"text": "WINS · paper trade" if mode == "paper" else "WINS · LIVE"},
        }]
    })


async def alert_trade_closed(
    token: str,
    pnl_usd: float,
    pnl_pct: float,
    reason: str,
    mode: str,
) -> None:
    won = pnl_usd >= 0
    await _send({
        "embeds": [{
            "title": f"{'✅' if won else '❌'} CLOSED {token}  [{mode.upper()}]",
            "color": _GREEN if won else _RED,
            "fields": [
                {"name": "P&L",    "value": f"`{'+'if pnl_usd>=0 else ''}{pnl_usd:.2f} USD`", "inline": True},
                {"name": "Return", "value": f"`{pnl_pct:+.2f}%`",                              "inline": True},
                {"name": "Reason", "value": f"`{reason}`",                                      "inline": True},
            ],
            "footer": {"text": "WINS · paper trade" if mode == "paper" else "WINS · LIVE"},
        }]
    })


async def alert_kill_switch(reason: str) -> None:
    await _send({
        "content": "🚨 **KILL SWITCH TRIGGERED** — manual review required",
        "embeds": [{
            "title": "System paused",
            "description": reason,
            "color": _RED,
            "footer": {"text": "WINS — drawdown limit hit"},
        }]
    })


async def alert_system_health(
    capital: float,
    open_positions: int,
    phase: str,
    mode: str,
    cycle_count: int = 0,
) -> None:
    from wins.alerts.presence import is_healthcheck_enabled
    if not is_healthcheck_enabled():
        return
    await _send({
        "embeds": [{
            "title": f"📊 WINS Health Check  [{mode.upper()}]",
            "color": _BLUE,
            "fields": [
                {"name": "Phase",          "value": f"`{phase}`",          "inline": True},
                {"name": "Capital",        "value": f"`${capital:.2f}`",   "inline": True},
                {"name": "Open positions", "value": f"`{open_positions}`", "inline": True},
                {"name": "Cycle",          "value": f"`#{cycle_count}`",   "inline": True},
            ],
            "footer": {"text": "WINS automated health check"},
        }]
    })


async def alert_signal_summary(
    token: str,
    signal_type: str,
    confidence: float,
    reasoning: str,
    mode: str,
) -> None:
    """Post a brief summary when a signal is evaluated but not traded."""
    await _send({
        "embeds": [{
            "title": f"🔍 Signal: {token}  ({signal_type})",
            "color": _DARK,
            "fields": [
                {"name": "Confidence", "value": f"`{confidence:.0%}`",                  "inline": True},
                {"name": "Mode",       "value": f"`{mode}`",                            "inline": True},
                {"name": "Summary",    "value": reasoning[:512] if reasoning else "—",   "inline": False},
            ],
        }]
    })
