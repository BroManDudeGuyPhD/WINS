# WINS — Weighted Intelligence Network for Signals

A Claude-powered crypto trading system that uses AI signal synthesis to hunt mid-cap altcoin catalyst events. Paper trades first, scales on evidence.

---

## What it does

Every 15 minutes WINS runs a full decision cycle:

1. **Ingests** price, volume, social sentiment, GitHub activity, and macro data for a token universe
2. **Asks Claude** to synthesise the signals and return a structured buy/hold decision with entry, stop-loss, and target prices
3. **Validates** the decision against hard-coded risk rules Claude cannot override
4. **Executes** on paper (or live) and persists everything to Postgres
5. **Alerts** via Discord DM with trade details and cycle health

---

## Architecture

```
CoinGecko / LunarCrush / GitHub
           │
           ▼
    collector.py          ← signal ingestion
           │
           ▼
    decision.py           ← Haiku pre-filter → Sonnet → Opus (escalation only)
           │
           ▼
    risk.py               ← 8 hard rules, no Claude override
           │
           ▼
    executor.py           ← PaperExecutor | LiveExecutor (Binance / Coinbase)
           │
           ▼
    PostgreSQL            ← decision_log, trade_log, system_state
           │
           ▼
    Discord bot           ← trade alerts + cycle health DMs
```

See [DOCS/ARCHITECTURE.md](DOCS/ARCHITECTURE.md) for full Mermaid diagrams.

---

## Hard risk rules

These are constants in `wins/shared/config.py`. Claude cannot change them.

| Rule | Value |
|------|-------|
| Max stop-loss per trade | 20% |
| Max single position size | 50% of capital |
| Max open positions | 2 |
| Min confidence to trade | 0.65 |
| Min reward-to-risk | 2:1 |
| Drawdown kill switch | 40% from run-start capital |
| BTC macro gate | Risk-off BTC blocks all entries |
| Risk flag high | Blocks trade regardless of confidence |

---

## Claude model usage

| Model | Role |
|-------|------|
| `claude-haiku-4-5-20251001` | Pre-compress raw social/news text > 2000 chars |
| `claude-sonnet-4-6` | Every routine decision cycle |
| `claude-opus-4-7` | Escalation only — confidence ≥ 0.92 AND catalyst signal |

The system prompt is cached (Anthropic prompt caching). Only the market data changes per call, reducing effective input cost ~60–70%.

---

## Token universe

**Currently active (first live test — 5 tokens):**
`SOL · SUI · JUP · ARB · LINK`

**Macro gate only (never traded):** `BTC · ETH`

Expand back to the full 25-token list in `wins/shared/config.py` after verifying cache hits and cost on the first live run.

---

## Scaling ladder

```
Phase 1   Paper trade      Prove logic works. Free.
Phase 2   $100 Run #1      Real fills. Expect to learn.
Phase 3   $100 Run #2      Tuned from Run 1.
Phase 4   $100 Run #3      Edge exists or it doesn't.
Phase 5   $250 × 3 runs    Only if 2/3 above profitable.
Phase 6   $500 × 3 runs    Same gate.
Phase 7   $1,000 runs      Scale with evidence.
```

The discipline is never skipping a phase.

---

## Local setup

**Prerequisites:** Docker Desktop, Python 3.12+, [Doppler CLI](https://docs.doppler.com/docs/install-cli)

```bash
# 1. Clone and install deps
git clone <repo>
cd WINS
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure secrets via Doppler (see .env.example for required keys)
doppler setup

# 3. Start the database
doppler secrets download --no-file --format env > .env.doppler
docker compose up wins-db -d

# 4. Run a mock cycle (no API keys needed)
doppler run -- python -c "
import os, asyncio
os.environ['DATABASE_URL'] = os.environ['DATABASE_URL'].replace('wins-db:5432','localhost:5433')
os.environ['USE_MOCK_BRAIN'] = 'true'
os.environ['TRADE_MODE'] = 'paper'
from wins.brain.cycle import run_cycle
asyncio.run(run_cycle())
"
```

Or use the CLI runner with a pretty table:

```bash
doppler run -- python scripts/run_cycle.py --dry-run --verbose
```

---

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Required for real Claude calls |
| `DATABASE_URL` | Postgres connection string |
| `USE_MOCK_BRAIN` | `true` = rule-based decisions, no API cost |
| `TRADE_MODE` | `paper` or `live` |
| `GITHUB_TOKEN` | Raises GitHub rate limit from 60 → 5000 req/hr |
| `DISCORD_BOT_TOKEN` | Trade alert DMs |

Secrets are managed via [Doppler](https://doppler.com). Never committed to git.

---

## Tests

```bash
python -m pytest tests/ -v
```

- `tests/test_risk.py` — 16 tests covering all 8 hard risk rules
- `tests/test_cycle_mock.py` — 4 smoke tests: tuple interface, buy flow, hold flow, paused system

---

## Deploy to VPS

Push to `main` triggers the GitHub Actions workflow (`.github/workflows/deploy.yml`), which SSHs to the server and runs `scripts/deploy.sh`. The server pulls the latest code, re-exports secrets from Doppler, rebuilds images, and restarts containers.

```bash
git push origin main
```

Requires these GitHub secrets set on the repo: `PROD_SSH_HOST`, `PROD_SSH_USER`, `PROD_SSH_KEY`, `PROD_SSH_PORT`, and `WINS_DOPPLER_TOKEN` as a variable.

---

*The system is the asset. The $100 is tuition.*
