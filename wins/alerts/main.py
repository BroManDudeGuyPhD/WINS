"""wins/alerts/main.py — Alert service entrypoint with Discord gateway presence."""
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import asyncpg
import discord
import httpx
from discord import app_commands

from wins.shared.logger import get_logger
from wins.shared.config import (
    DISCORD_BOT_TOKEN, DISCORD_USER_ID, DISCORD_GUILD_ID,
    COINGECKO_API_KEY, DATABASE_URL,
)
from wins.ingestion.collector import COINGECKO_IDS
from wins.alerts.discord_bot import send_message, alert_daily_spend
from wins.alerts.presence import read_status, set_healthcheck_enabled, is_healthcheck_enabled

log = get_logger("alerts.main")

_GREEN  = 0x2ecc71
_RED    = 0xe74c3c
_BLUE   = 0x3498db

# Maps status value → (discord.Status, activity text shown under bot name)
_STATUS_MAP: dict[str, tuple[discord.Status, str]] = {
    "idle":       (discord.Status.online, "Watching markets"),
    "ingesting":  (discord.Status.idle,   "Gathering signals"),
    "trading":    (discord.Status.dnd,    "Executing trades"),
}


async def _fetch_live_prices(tokens: list[str]) -> dict[str, float]:
    """Fetch current USD prices for a list of token symbols from CoinGecko."""
    id_to_sym: dict[str, str] = {}
    for sym in tokens:
        cg_id = COINGECKO_IDS.get(sym.upper())
        if cg_id:
            id_to_sym[cg_id] = sym

    if not id_to_sym:
        return {}

    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                headers=headers,
                params={"ids": ",".join(id_to_sym), "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {id_to_sym[cg_id]: v["usd"] for cg_id, v in data.items() if cg_id in id_to_sym}
    except Exception as exc:
        log.warning(f"CoinGecko price fetch failed: {exc}")
        return {}


class WINSBot(discord.Client):
    """Gateway client — presence polling + slash commands."""

    def __init__(self) -> None:
        intents = discord.Intents.none()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._last_status: str | None = None
        self._owner_id: int | None = int(DISCORD_USER_ID) if DISCORD_USER_ID else None
        self._pool: asyncpg.Pool | None = None

    async def setup_hook(self) -> None:
        _register_commands(self)
        # Always publish globally so commands appear in every server the bot is in.
        # If DISCORD_GUILD_ID is set, also sync to that guild for instant dev updates
        # (global propagation can take up to 1 hour).
        await self.tree.sync()
        log.info("Slash commands synced globally")
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                log.info(f"Slash commands also synced to guild {DISCORD_GUILD_ID} (instant)")
            except discord.errors.Forbidden:
                log.warning(
                    "Guild command sync failed (403) — bot may be missing applications.commands scope."
                )
        self.loop.create_task(self._presence_loop())
        self.loop.create_task(self._daily_spend_loop())

    async def on_ready(self) -> None:
        log.info(f"WINS Alerts service started. Logged in as {self.user}")
        # Connect to DB for slash commands that need position data
        if DATABASE_URL:
            try:
                self._pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
                log.info("DB pool connected")
            except Exception as exc:
                log.warning(f"DB pool failed: {exc}")
        # Read persisted setting — default is OFF if file doesn't exist yet
        enabled = is_healthcheck_enabled()
        label = "ON" if enabled else "OFF"
        log.info(f"Healthcheck DMs: {label} (persisted)")
        msg = "**WINS Alerts service online.** 🟢"
        if enabled:
            msg += "\nHealthcheck DMs: **✅ ON**"
        await send_message(msg)

    async def close(self) -> None:
        try:
            await self.change_presence(status=discord.Status.invisible)
        except Exception:
            pass
        await send_message("**WINS Alerts service going offline.** 🔴")
        if self._pool:
            await self._pool.close()
        await super().close()

    async def _daily_spend_loop(self) -> None:
        """Post a spend summary at midnight UTC each day."""
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.now(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((midnight - now).total_seconds())
            if self.is_closed() or not self._pool:
                break
            try:
                rows = await self._pool.fetch(
                    """SELECT model_used,
                              COUNT(*)                            AS decisions,
                              COALESCE(SUM(prompt_tokens),    0)  AS prompt_tokens,
                              COALESCE(SUM(completion_tokens),0)  AS completion_tokens,
                              COALESCE(SUM(cache_read_tokens),0)  AS cache_read_tokens
                         FROM decision_log
                        WHERE ts >= NOW() - INTERVAL '24 hours'
                          AND model_used IS NOT NULL
                        GROUP BY model_used
                        ORDER BY model_used"""
                )
                await alert_daily_spend([dict(r) for r in rows])
            except Exception as exc:
                log.warning(f"Daily spend summary failed: {exc}")

    async def _presence_loop(self) -> None:
        """Poll the shared status file every 5 s and update bot presence."""
        _heartbeat = Path("/tmp/heartbeat")
        await self.wait_until_ready()
        while not self.is_closed():
            current = read_status()
            if current != self._last_status:
                status, activity_text = _STATUS_MAP.get(current, _STATUS_MAP["idle"])
                await self.change_presence(
                    status=status,
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=activity_text,
                    ),
                )
                self._last_status = current
                log.info(f"Bot presence → {current}")
            _heartbeat.touch()
            await asyncio.sleep(5)


def _register_commands(bot: WINSBot) -> None:

    @bot.tree.command(name="healthcheck", description="Enable or disable cycle healthcheck DMs")
    @app_commands.describe(state="on or off")
    @app_commands.choices(state=[
        app_commands.Choice(name="on",  value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def healthcheck(interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        # Only respond to the configured owner
        if bot._owner_id and interaction.user.id != bot._owner_id:
            await interaction.response.send_message(
                "⛔ You are not authorised to control WINS.", ephemeral=True
            )
            return

        enabled = state.value == "on"
        set_healthcheck_enabled(enabled)
        emoji = "✅" if enabled else "🔕"
        label = "**enabled**" if enabled else "**disabled**"
        log.info(f"Healthcheck DMs set to: {state.value} by {interaction.user}")
        await interaction.response.send_message(
            f"{emoji} Healthcheck DMs are now {label}.", ephemeral=True
        )

    @bot.tree.command(name="positions", description="Show open positions with live P&L")
    async def positions(interaction: discord.Interaction) -> None:
        if bot._owner_id and interaction.user.id != bot._owner_id:
            await interaction.response.send_message(
                "⛔ You are not authorised to control WINS.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if not bot._pool:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Database unavailable", color=_RED),
                ephemeral=True,
            )
            return

        try:
            rows = await bot._pool.fetch(
                """SELECT token, qty, entry_price, stop_loss_price, target_price, ts_open
                     FROM trade_log
                    WHERE ts_close IS NULL AND side = 'buy'
                 ORDER BY ts_open ASC"""
            )
        except Exception as exc:
            log.warning(f"/positions DB query failed: {exc}")
            await interaction.followup.send(
                embed=discord.Embed(title="❌ DB query failed", description=str(exc), color=_RED),
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="📊 Open Positions",
                    description="No open positions.",
                    color=_BLUE,
                ),
                ephemeral=True,
            )
            return

        # Fetch live prices for all open tokens
        tokens = [r["token"] for r in rows]
        prices = await _fetch_live_prices(tokens)

        fields: list[dict] = []
        total_unrealized = 0.0
        all_priced = True

        for r in rows:
            token     = r["token"]
            qty       = float(r["qty"])
            entry     = float(r["entry_price"])
            sl        = float(r["stop_loss_price"])
            tp        = float(r["target_price"])
            cost      = qty * entry
            current   = prices.get(token)

            if current is not None:
                pnl_usd = (current - entry) * qty
                pnl_pct = (current - entry) / entry * 100
                total_unrealized += pnl_usd
                sign_str = "+" if pnl_usd >= 0 else ""
                pnl_line = f"`{sign_str}{pnl_usd:.2f} USD  ({pnl_pct:+.2f}%)`"
                price_line = f"`${current:.4f}`"
                icon = "📈" if pnl_usd >= 0 else "📉"
            else:
                pnl_line = "`price unavailable`"
                price_line = "`—`"
                icon = "📊"
                all_priced = False

            fields.append({
                "name": f"{icon} **{token}**",
                "value": (
                    f"Entry: `${entry:.4f}`  →  Now: {price_line}\n"
                    f"SL: `${sl:.4f}`  |  TP: `${tp:.4f}`\n"
                    f"Size: `${cost:.2f}`  |  Unrealized P&L: {pnl_line}"
                ),
                "inline": False,
            })

        if all_priced:
            color = _GREEN if total_unrealized > 0 else (_RED if total_unrealized < 0 else _BLUE)
        else:
            color = _BLUE

        sign_str = "+" if total_unrealized >= 0 else ""
        embed = discord.Embed(
            title=f"📊 Open Positions ({len(rows)})",
            description=f"**Total unrealized: `{sign_str}{total_unrealized:.2f} USD`**",
            color=color,
        )
        for f in fields:
            embed.add_field(name=f["name"], value=f["value"], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


    @bot.tree.command(name="status", description="System status: capital, spend, and latest brain decisions")
    @app_commands.describe(hours="Lookback window for spend and decisions (default: 24)")
    async def status(interaction: discord.Interaction, hours: int = 24) -> None:
        if bot._owner_id and interaction.user.id != bot._owner_id:
            await interaction.response.send_message(
                "⛔ You are not authorised to control WINS.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if not bot._pool:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Database unavailable", color=_RED),
                ephemeral=True,
            )
            return

        try:
            state_row, spend_rows, decision_rows, position_rows = await asyncio.gather(
                bot._pool.fetchrow(
                    "SELECT * FROM system_state ORDER BY ts DESC LIMIT 1"
                ),
                bot._pool.fetch(
                    """
                    SELECT model_used,
                           COUNT(*)                            AS decisions,
                           COALESCE(SUM(prompt_tokens),    0)  AS prompt_tokens,
                           COALESCE(SUM(completion_tokens),0)  AS completion_tokens,
                           COALESCE(SUM(cache_read_tokens),0)  AS cache_read_tokens
                      FROM decision_log
                     WHERE ts >= NOW() - ($1 || ' hours')::interval
                       AND model_used IS NOT NULL
                     GROUP BY model_used
                     ORDER BY SUM(prompt_tokens) DESC
                    """,
                    str(hours),
                ),
                bot._pool.fetch(
                    """
                    SELECT DISTINCT ON (token)
                        token, action, confidence, model_used, reasoning, ts
                      FROM decision_log
                     WHERE ts >= NOW() - ($1 || ' hours')::interval
                     ORDER BY token, ts DESC
                    """,
                    str(hours),
                ),
                bot._pool.fetch(
                    """
                    SELECT token, qty, entry_price, stop_loss_price, target_price, ts_open
                      FROM trade_log
                     WHERE ts_close IS NULL AND side = 'buy'
                     ORDER BY ts_open ASC
                    """
                ),
            )
        except Exception as exc:
            log.warning(f"/status DB query failed: {exc}")
            await interaction.followup.send(
                embed=discord.Embed(title="❌ DB query failed", description=str(exc), color=_RED),
                ephemeral=True,
            )
            return

        embeds: list[discord.Embed] = []

        # ── Embed 1: System state ─────────────────────────────────────────────
        if state_row:
            s           = dict(state_row)
            capital     = float(s.get("capital_usd") or 0)
            start_cap   = float(s.get("run_starting_capital") or capital)
            open_pos    = int(s.get("open_positions") or 0)
            phase       = s.get("phase") or "—"
            mode        = (s.get("trade_mode") or "paper").upper()
            paused      = bool(s.get("system_paused"))
            pause_reason = s.get("pause_reason") or ""

            pnl     = capital - start_cap
            pnl_pct = (pnl / start_cap * 100) if start_cap else 0
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct:+.1f}%)"

            color   = _RED if paused else (_GREEN if pnl >= 0 else _RED)
            status_line = "🛑 **PAUSED**" if paused else "✅ Running"
            if paused and pause_reason:
                status_line += f"\n> {pause_reason[:120]}"

            state_embed = discord.Embed(
                title=f"📊 WINS Status · {mode}",
                description=status_line,
                color=color,
            )
            state_embed.add_field(name="Capital",        value=f"`${capital:.2f}`",  inline=True)
            state_embed.add_field(name="Run P&L",        value=f"`{pnl_str}`",       inline=True)
            state_embed.add_field(name="Open Positions", value=f"`{open_pos}`",      inline=True)
            state_embed.add_field(name="Phase",          value=f"`{phase}`",         inline=True)

            if position_rows:
                tokens_str = "  ".join(r["token"] for r in position_rows)
                state_embed.add_field(name="Positions", value=f"`{tokens_str}`", inline=True)

            embeds.append(state_embed)

        # ── Embed 2: API spend ────────────────────────────────────────────────
        _pricing: dict[str, dict[str, float]] = {
            "haiku":  {"input": 0.80,  "output": 4.00,  "cache_read": 0.08},
            "sonnet": {"input": 3.00,  "output": 15.00, "cache_read": 0.30},
            "opus":   {"input": 15.00, "output": 75.00, "cache_read": 1.50},
        }

        def _tier(model: str) -> str:
            m = (model or "").lower()
            for t in ("haiku", "sonnet", "opus"):
                if t in m:
                    return t
            return "sonnet"

        def _model_cost(prompt: int, compl: int, cache: int, model: str) -> float:
            p = _pricing[_tier(model)]
            return prompt * p["input"] / 1_000_000 + compl * p["output"] / 1_000_000 + cache * p["cache_read"] / 1_000_000

        total_cost      = 0.0
        total_decisions = 0
        spend_fields: list[dict] = []

        for r in spend_rows:
            model     = r["model_used"] or "unknown"
            decisions = int(r["decisions"])
            prompt    = int(r["prompt_tokens"])
            compl     = int(r["completion_tokens"])
            cache     = int(r["cache_read_tokens"])
            cost      = _model_cost(prompt, compl, cache, model)
            total_cost      += cost
            total_decisions += decisions
            tier = _tier(model).capitalize()
            spend_fields.append({
                "name":  f"`{tier}`",
                "value": (
                    f"Calls: `{decisions}` · In: `{prompt:,}` · Out: `{compl:,}` · Cache: `{cache:,}`\n"
                    f"Est. cost: `${cost:.4f}`"
                ),
                "inline": True,
            })

        if spend_fields:
            cost_color  = _GREEN if total_cost < 0.10 else (_YELLOW if total_cost < 1.00 else _RED)
            spend_embed = discord.Embed(
                title=f"💰 API Spend · last {hours} h",
                description=f"**{total_decisions} decisions · Est. total: `${total_cost:.4f}`**",
                color=cost_color,
            )
            for f in spend_fields:
                spend_embed.add_field(name=f["name"], value=f["value"], inline=f["inline"])
            spend_embed.set_footer(text="prices approximate")
            embeds.append(spend_embed)
        else:
            embeds.append(discord.Embed(
                title=f"💰 API Spend · last {hours} h",
                description="No Claude calls in this window.",
                color=_BLUE,
            ))

        # ── Embed 3: Brain decisions ──────────────────────────────────────────
        _action_icon = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}

        if decision_rows:
            now = datetime.now(timezone.utc)
            decisions_embed = discord.Embed(
                title=f"🧠 Latest Decisions · last {hours} h",
                color=_BLUE,
            )
            for d in sorted(decision_rows, key=lambda r: r["token"]):
                action  = (d["action"] or "hold").lower()
                conf    = float(d["confidence"] or 0)
                model   = _tier(d["model_used"] or "").capitalize()
                ts      = d["ts"]
                if ts:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_min = int((now - ts).total_seconds() / 60)
                    age_str = f"{age_min}m ago" if age_min < 60 else f"{age_min // 60}h ago"
                else:
                    age_str = "?"

                icon     = _action_icon.get(action, "⚪")
                reasoning = (d["reasoning"] or "—")[:80]
                decisions_embed.add_field(
                    name=f"{icon} **{d['token']}** — {action.upper()}",
                    value=f"conf: `{conf:.2f}` · {model} · {age_str}\n{reasoning}",
                    inline=False,
                )
            embeds.append(decisions_embed)
        else:
            embeds.append(discord.Embed(
                title=f"🧠 Latest Decisions · last {hours} h",
                description="No decisions in this window.",
                color=_BLUE,
            ))

        await interaction.followup.send(embeds=embeds, ephemeral=True)


async def main() -> None:
    if not DISCORD_BOT_TOKEN:
        log.warning("DISCORD_BOT_TOKEN not set — alerts disabled, running in no-op mode.")
        while True:
            await asyncio.sleep(3600)
        return

    bot = WINSBot()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.close()))

    try:
        await bot.start(DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
