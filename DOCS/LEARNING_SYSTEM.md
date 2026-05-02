# WINS Self-Improvement System

Four components, ranked by value. Each is independent — they can be built and
deployed one at a time. None require trading expertise to operate.

---

## 1. Backtest Harness  *(done — `wins/backtest/harness.py`)*

**What it does:**  
Replays historical market data through `make_decision()` and scores each
decision against what the price actually did afterward. Answers: "if this
strategy had been running for the past 90 days, what would have happened?"

**Why it's first:**  
Without it, every change to the prompt or risk rules is a guess. With it, you
can evaluate a change in minutes instead of waiting weeks of paper trading.

**How it works:**
1. Pull historical OHLCV data from CoinGecko for each target token.
2. For each historical timestamp, build a `SignalBundle` the same way
   `cycle.py` does now — but using past data instead of live data.
3. Call `make_decision()` on each bundle (mock or real Claude, configurable).
4. Compare the resulting `action` + `entry_price` against the actual price
   48–72 hours later. Did a buy recommendation make money? By how much?
5. Output a summary: win rate, avg P&L, win rate by confidence bucket,
   win rate by signal_type.

**What you do with it:**  
Run it before and after any prompt change. If win rate goes up, keep the
change. If it goes down, revert.

**Key constraint:**  
Historical news/social data is hard to replay accurately — LunarCrush doesn't
expose a free historical endpoint. However, the system now caches raw social
signals (galaxy_score, alt_rank, sentiment, interactions_24h) to `signal_log`
every cycle via `cycle.py:_log_social_signals()`. Once enough history has
accumulated, the backtest harness can replay those cached values instead of
fetching live data, making social signals fully replayable. Start with price +
macro signals only until at least 4 weeks of `signal_log` data exists.

---

## 2. Confidence Auto-Calibration  *(done — `wins/brain/calibration.py`, cron: `calibration_cron.py`)*

**What it does:**  
Tracks whether Claude's confidence scores are actually honest. If Claude says
0.85 but those trades only win 50% of the time, calibration detects that and
tightens the effective confidence floor automatically — no prompt change needed.

**Why it matters:**  
A miscalibrated confidence score is dangerous because the risk layer trusts it.
If 0.75 really means 0.55, the system is taking bets it thinks are good while
they aren't. Calibration makes the numbers mean something.

**How it works:**
1. After each trade closes, write the decision's confidence score and the
   trade outcome (win/loss, P&L %) to a calibration table in Postgres.
2. Weekly cron: group closed trades by confidence bucket (0.65–0.75, 0.75–0.85,
   0.85+). Compute realized win rate per bucket.
3. Store the result as a `calibration_multiplier` per bucket.
4. `risk.py` reads the multiplier at validation time and applies it:
   effective_confidence = raw_confidence × multiplier.
   A 0.85 call with a 0.70 multiplier becomes 0.60 — below the 0.65 floor,
   so it gets blocked.

**What you do with it:**  
Nothing. Review the weekly calibration report posted to Discord to stay aware
of model drift. Intervene only if the multiplier drops below 0.5 across all
buckets — that signals the strategy is fundamentally broken.

**Key constraint:**  
Needs at least 30–50 closed trades per bucket to be statistically meaningful.
Do not enable the multiplier enforcement until that threshold is reached; before
then it's display-only.

---

## 3. Prompt Critique Loop  *(third — useful after 2–3 months of data)*

**What it does:**  
Feeds Claude its own losing trades — including the data it had at the time —
and asks it to propose small revisions to the system prompt. Surfaces patterns
in failures that you wouldn't notice manually. You review the proposals and
merge or reject them like any other code change.

**Why it's third:**  
It needs real data to critique. Running it on a thin sample produces noise.
The backtest harness and calibration data both feed into it.

**How it works:**
1. Weekly cron pulls the last 30 days of losing trades from `trade_log` joined
   with `decision_log` (the `SignalBundle` the model saw + its reasoning).
2. Groups them by failure pattern: all macro-gate misses together, all
   confidence overestimates together, etc.
3. Sends to Opus with the current `SYSTEM_PROMPT` and the failure summary:
   "Here is your current strategy. Here are the trades where your reasoning
   was wrong. Propose 1–3 small, specific changes to the system prompt that
   would have avoided these failures. Each change must be a single sentence
   that can be directly inserted or replaced."
4. Posts the proposals to Discord as a PR diff for review.
5. You merge, reject, or modify. Claude cannot push changes to itself.

**What you do with it:**  
Read the Discord post weekly. If a proposal looks reasonable, create the PR.
If a proposal looks wrong (e.g. "never buy during BTC rallies" — which would
block most good entries), reject it.

**Key constraint:**  
This is the only component that touches the strategy itself. Keep it gated on
human review permanently. Never automate the merge.

---

## 4. Strategy A/B Bake-Off  *(fourth — useful once you have variants worth comparing)*

**What it does:**  
Runs two versions of the system prompt in parallel paper books for a fixed
period (e.g. 4 weeks), then keeps the better-performing one. Lets you test
strategic hypotheses — e.g. "does weighting developer activity higher than
social sentiment improve results?" — with real paper outcomes instead of
guesses.

**Why it's last:**  
It requires you to have two strategies worth comparing. Until the backtest
harness and prompt critique loop have produced at least one meaningful prompt
variant, there's nothing to bake off against.

**How it works:**
1. Write a second version of `SYSTEM_PROMPT` — change one thing (e.g. signal
   weighting order, a new hard rule, different time horizon guidance).
2. Add a `strategy_variant` field to `decision_log` and `trade_log`.
3. Each cycle, run both strategies against the same `SignalBundle`. Log both
   decisions. Execute only the primary strategy's trades (paper still, one
   book), but record what the challenger would have done.
4. After the bake-off window, compare win rate, avg P&L, Sharpe ratio between
   primary and challenger.
5. If challenger wins by a statistically meaningful margin (p < 0.05 or at
   least 50 decisions), promote it to primary.

**What you do with it:**  
Write the challenger variant (or ask Claude to draft one based on the prompt
critique output). Review the final comparison before promoting.

**Key constraint:**  
Only change one variable per bake-off. Testing two changes at once makes it
impossible to know which one worked.

---

## What none of these are

None of these components autonomously mutate the trading strategy in a tight
loop without human review. That approach — trade → auto-adjust → trade again —
overfits to noise and compounds errors without a brake. The components above
either (a) only measure, never change anything, or (b) propose changes through
a human gate. The system improves, but you stay in the loop on every strategy
change.
