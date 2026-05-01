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
        if DISCORD_GUILD_ID:
            # Sync to specific guild — commands appear instantly.
            # Requires bot invited with applications.commands scope.
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID} (instant)")
            except discord.errors.Forbidden:
                log.warning(
                    "Guild command sync failed (403) — bot may be missing applications.commands scope. "
                    "Re-invite with: https://discord.com/oauth2/authorize"
                    f"?client_id={{YOUR_APP_ID}}&scope=bot%20applications.commands&permissions=2048"
                    " — falling back to global sync."
                )
                await self.tree.sync()
                log.info("Slash commands synced globally (may take up to 1 hour)")
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour)")
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
