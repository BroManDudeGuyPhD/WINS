"""
wins/shared/config.py
Central configuration loaded from environment variables.
Hard-coded risk rules are defined here — they are NOT configurable at runtime.
"""
import os
from decimal import Decimal


# ─── Hard-coded risk rules (WINS.md §Risk Management) ────────────────────────
MAX_STOP_LOSS_PCT       = Decimal("0.20")   # 20% max loss per trade
MAX_SINGLE_POSITION_PCT = Decimal("0.50")   # 50% max of capital in one trade
DRAWDOWN_KILL_SWITCH    = Decimal("0.40")   # Pause system if down 40% in a run
MIN_CONFIDENCE_TO_TRADE = Decimal("0.65")   # Minimum Claude confidence for entry
MAX_OPEN_POSITIONS      = 2                 # Never hold more than 2 positions

# ─── Claude models ───────────────────────────────────────────────────────────
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
OPUS_MODEL   = "claude-opus-4-7"

# ─── Environment ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # optional when USE_MOCK_BRAIN=true

# USE_MOCK_BRAIN=true — runs rule-based decisions with no Anthropic API calls.
# Safe for testing paper trade flow, cycle logic, risk layer, and DB logging.
USE_MOCK_BRAIN = os.environ.get("USE_MOCK_BRAIN", "false").lower() == "true"

DATABASE_URL = os.environ.get("DATABASE_URL", "")

TRADE_MODE = os.environ.get("TRADE_MODE", "paper")   # paper | live
if TRADE_MODE not in ("paper", "live"):
    raise ValueError(f"Invalid TRADE_MODE: {TRADE_MODE!r} — must be 'paper' or 'live'")

DECISION_INTERVAL_MINUTES = int(os.environ.get("DECISION_INTERVAL_MINUTES", "15"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_USER_ID   = os.environ.get("DISCORD_USER_ID", "")   # your numeric Discord user ID
DISCORD_GUILD_ID  = os.environ.get("DISCORD_GUILD_ID", "")  # optional: set for instant slash command sync (dev)

COINGECKO_API_KEY    = os.environ.get("COINGECKO_API_KEY", "")
LUNARCRUSH_API_KEY   = os.environ.get("LUNARCRUSH_API_KEY", "")
GLASSNODE_API_KEY    = os.environ.get("GLASSNODE_API_KEY", "")
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")

BINANCE_API_KEY     = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET  = os.environ.get("BINANCE_API_SECRET", "")
BINANCE_TESTNET_API_KEY    = os.environ.get("BINANCE_TESTNET_API_KEY", "")
BINANCE_TESTNET_API_SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
COINBASE_API_KEY    = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.environ.get("COINBASE_API_SECRET", "")
KRAKEN_API_KEY      = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET   = os.environ.get("KRAKEN_API_SECRET", "")

# Which exchange backend to use for live orders: binance | coinbase | kraken
# Ignored when TRADE_MODE=paper.
EXCHANGE_BACKEND = os.environ.get("EXCHANGE_BACKEND", "binance")

# ─── Target universe (WINS.md §Target Strategy) ──────────────────────────────
# Trimmed to 5 for first live test to validate caching and cost before expanding.
# Full list: SOL AVAX DOT LINK ARB OP INJ SUI APT NEAR FTM ATOM ALGO AAVE UNI
#            SNX CRV LDO DYDX GMX PENDLE JUP PYTH WIF BONK
TARGET_TOKENS: list[str] = ["SOL", "SUI", "JUP", "ARB", "LINK"]

MACRO_TOKENS = ["BTC", "ETH"]   # Used only as macro gate, never traded directly
