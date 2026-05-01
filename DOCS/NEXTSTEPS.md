OPUS4.7 review (4/30/26)

Solid bones. Architecture, separation of concerns, hard-risk layer outside Claude, mock brain, prompt caching, exchange abstraction, paper portfolio with DB persistence — all the right shapes. But there are several real bugs and cost traps that will bite you the moment you flip USE_MOCK_BRAIN=false. Reviewing in priority order:

---

## ✅ COMPLETED (2026-04-30)

### 🔴 Blockers

**1. Model IDs fixed** `wins/shared/config.py`
- `HAIKU_MODEL`  → `claude-haiku-4-5-20251001` (was missing date suffix)
- `OPUS_MODEL`   → `claude-opus-4-7` (was `claude-opus-4-6`)
- `SONNET_MODEL` → `claude-sonnet-4-6` ✓ (was already correct)

**2. Opus escalation tightened** `wins/brain/decision.py`
- Threshold raised from `0.85` → `0.92`
- Escalation now also requires `signal_type == "catalyst"`
- Makes Opus spend predictable; routine momentum signals no longer trigger the rerun
- Cost scaling issue (#2 — 25× overrun) partially addressed; full fix requires pre-filtering tokens (see open items below)

**3. Drawdown kill switch now fires correctly** `wins/db/init.sql` + `wins/brain/cycle.py`
- Added `run_starting_capital` column to `system_state` — set once when a run is initialised
- `cycle.py` now reads `run_starting_capital` from DB instead of using `capital_usd` (which was always current capital, making drawdown always ~0)
- Migration included: `ALTER TABLE system_state ADD COLUMN IF NOT EXISTS run_starting_capital`

**4. Stub text stripped from signal bundle** `wins/ingestion/collector.py` + `wins/brain/prompts.py`
- All stub/placeholder strings removed (`"No news aggregator configured yet"`, `"Social data unavailable"`, etc.) — replaced with empty strings
- `build_user_message` now filters empty signal fields so Claude only sees real data
- Eliminates paying input tokens on placeholder strings AND misleading the model about signal completeness

**5. R:R 2:1 now enforced in risk layer** `wins/execution/risk.py`
- Added hard check: `reward / risk >= 2.0` — trades with sub-2:1 R:R are blocked at the risk layer
- Previously only stated in the system prompt; Claude could drift and return 1.4:1 without consequence
- Also fixed hold-check order: `Action.hold` now short-circuits before the macro gate (correct — a hold has no execution to block)

**6. Paper SL fill uses actual price** `wins/execution/paper_portfolio.py`
- Stop-loss fills now use `price` (current market price) not `stop_loss_price`
- Gap-downs in real trading fill at market, not at the SL limit — paper P&L was previously too optimistic

**7. Token usage wired into decision_log** `wins/brain/decision.py` + `wins/brain/cycle.py`
- `make_decision` now returns `(decision, model_used, input_tokens, output_tokens, cache_read_tokens)`
- All three usage fields logged to `decision_log` — verifiable that caching is working
- Added `cache_read_tokens` column to `decision_log` schema (with migration)

**8. Portfolio context + UTC timestamp in Claude prompt** `wins/brain/prompts.py` + `wins/brain/cycle.py`
- User message now includes `account_state` block: `capital_usd`, `open_positions`
- `as_of` UTC timestamp injected so Claude doesn't anchor to training cutoff
- Claude can now avoid correlated bets (e.g. SOL already open when JUP is evaluated)

**9. model_label now records actual model used** `wins/brain/cycle.py`
- `make_decision` returns actual model string (`"claude-sonnet-4-6"`, `"claude-opus-4-7"`, `"mock"`)
- Previously hardcoded `"sonnet/opus"` regardless of what fired

**10. Per-trade state persistence** `wins/brain/cycle.py`
- `system_state` is now persisted after each trade (not just at cycle end)
- Crash on token 12 of 25 no longer leaves `trade_log` and `system_state` inconsistent

**11. Dead code removed** `wins/alerts/telegram.py`
- Deleted — Discord bot is the alerter; two alerters were dead code

**12. Risk layer test suite** `tests/test_risk.py`
- 16 passing tests covering all 8 hard rules: macro gate, hold pass-through, min confidence, max positions, SL distance, SL=0 guard, R:R 2:1, kill switch (correct baseline), risk_flag=high, and the happy path

---

## 🔴 Open — fix before any real API call

**2. Cost estimate is still 25× off** *(partially addressed — Opus gated but Sonnet still called 25× per cycle)*
- Options: pre-filter to ~5 tokens with strongest precondition signals, OR run macro/screening pass on Haiku first
- Recommendation: cap `TARGET_TOKENS` to ~5 for first live test, then expand

---

## 🟡 Open — should-fix before live

**10. CoinGecko + GitHub rate limits**
- GitHub unauthenticated = 60 calls/hr; 25 tokens × 4 cycles/hr = 100 calls/hr over limit
- Set `GITHUB_TOKEN` env var (already in config, just needs a value)
- Consider TTL cache so GitHub/social is only re-fetched every N cycles

**11. No tests outside risk layer**
- At minimum: smoke test for `mock_decision` → risk → paper executor end-to-end
- Would catch interface regressions (e.g. if return tuple changes)

---

## 🟢 Open — smaller / nice-to-have

- `MAX_SINGLE_POSITION_PCT` (50%) + `MAX_OPEN_POSITIONS` (2): second position is sized at 50% of remaining = 25% gross — probably intended, but document explicitly
- Prompt doesn't include recent decisions on same token — Claude can't avoid re-entering a token it just exited
- No LunarCrush TTL cache — same social data re-fetched every cycle

---

## Pre-API-key checklist (from review)

- [x] Fix model IDs and verify with a single Haiku ping
- [x] Fix drawdown kill switch baseline
- [x] Add R:R check in risk layer
- [x] Add test_risk.py covering all hard rules
- [x] Strip stub fields from the bundle Claude sees
- [x] Wire response.usage into decision_log
- [x] Disable/tighten Opus escalation (raised threshold + catalyst gate)
- [ ] Cap TARGET_TOKENS at ~5 for first live brain test, then expand
- [ ] Run one single-token, single-cycle, real-API smoke test in paper mode — verify cache hit on call #2, verify token logging, verify a risk_flag=high is rejected


The two remaining open items are the right ones to focus on, but I'd reorder and add a few things:

Must-do before the API key goes in:

Cap TARGET_TOKENS to ~5 for first call. Current list is 25 tokens × Sonnet × every 15 min = ~100 Sonnet calls/hr just to learn whether caching works. Trim to ["SOL","ETH","SUI","JUP","ARB"] (or your pick) for the first 24h — proves the loop, then expand.
Single-cycle dry run with LOG_LEVEL=DEBUG before scheduling. Inspect one decision_log row end-to-end: cache_read_tokens should be 0 on call #1 and >0 on call #2 of the same cycle (system prompt cache hit).
Verify the risk_flag=high rejection path with one fixture — easy to add to test_risk.py if not already there (it is — confirmed at line ~16 tests).
Set GITHUB_TOKEN. With 5 tokens × 4 cycles/hr = 20 GitHub calls/hr you're under the 60/hr unauth limit, but if you re-expand to 25 you'll get rate-limited mid-cycle and the bundle will silently degrade. Set it now.
Worth adding (small lift, big payoff):

Smoke test for the full mock pipeline — test_cycle_mock.py that stubs collect_signal_bundles, runs run_cycle() against a test DB, asserts a row lands in trade_log and decision_log. The risk layer is well-tested in isolation but the tuple-unpacking interface between make_decision → cycle.py is exactly the kind of thing that breaks silently.
Position-sizing comment. calculate_position_size always returns 0.5 × capital; the min(...) is dead code since 0.5 × capital ≤ capital always. Either remove the min or document the intent — right now it reads like it's guarding against something that can't happen.
check_and_close_positions only sees TARGET_TOKENS prices. Not a bug today (you don't trade BTC/ETH), but if you ever add macro tokens to the trade universe, open positions in them would never get SL-checked. Worth a one-line guard: log if an open position has no current price.
Nice-to-have, defer:

Recent-decisions context in prompt (so Claude doesn't re-enter what it just exited)
LunarCrush / GitHub TTL caches
Pre-filter to ~5 tokens via a Haiku screening pass (the proper fix for cost overrun #2; the cap-to-5 above is the cheap version)
Suggested order before API plug-in

1. Trim TARGET_TOKENS to ~5     (1 line)
2. Set GITHUB_TOKEN              (env)
3. Add test_cycle_mock.py        (~30 min)
4. Run one mock cycle            (verify DB writes)
5. Flip USE_MOCK_BRAIN=false
6. Run ONE cycle, inspect decision_log row by hand
7. If cache_read_tokens > 0 on call #2 and tokens look sane → schedule it