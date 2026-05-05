# WINS Roadmap — Post-Proof Enhancements

This document tracks enhancements that are **deliberately deferred** until the
current system has produced enough closed-trade data to evaluate them against
real outcomes. Building them earlier means tuning knobs blind.

---

## Volume Gate

Do not implement any item below until **at least 50 closed paper trades** are
in `trade_log`, with at minimum:

- 30+ trades in each of the three calibration confidence buckets
  (low 0.65–0.75, mid 0.75–0.85, high 0.85+) — this is also the threshold
  the `calibration_result` system uses before enforcing multipliers
  ([init.sql:92](../wins/db/init.sql#L92)).
- Coverage across at least two distinct macro regimes (one risk-on stretch
  and one risk-off stretch).
- Trades from at least 4 of the 5 target tokens.

Until these are met, treat the current exit system (fixed stop-loss + fixed
target, full exit on either) as authoritative. The discretionary sell branch
([cycle.py Step 2b](../wins/brain/cycle.py)) is the only override path during
the proof phase.

### How to check the gate

```sql
-- Run on stg via: ssh ironman "docker exec wins-db psql -U wins -d wins -c \"<sql>\""
SELECT
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL) AS closed_total,
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL AND d.confidence >= 0.85)             AS high_bucket,
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL AND d.confidence >= 0.75 AND d.confidence < 0.85) AS mid_bucket,
  COUNT(*) FILTER (WHERE ts_close IS NOT NULL AND d.confidence >= 0.65 AND d.confidence < 0.75) AS low_bucket,
  COUNT(DISTINCT t.token) FILTER (WHERE ts_close IS NOT NULL)                       AS tokens_traded
FROM trade_log t
LEFT JOIN decision_log d ON d.id = t.decision_id;
```

Re-run before opening any of the post-proof items below.

---

## Post-Proof Enhancement 1 — Partial scale-out at +R

**The idea.** When unrealized gain on a position equals the original risk
amount (the dollar distance from entry to stop-loss, called "1R"), sell half
the position and move the stop on the remaining half up to breakeven. The
trade is now risk-free; whatever happens to the second half is pure upside.

**Why it's the highest-leverage exit upgrade.** It directly addresses the
most common painful pattern in fixed-target systems: position goes +10%,
reverses, closes at stop-loss for a full loss. With +R scale-out, that same
path closes the first half at +1R profit and the second half at breakeven —
a small net win instead of a full loss.

**When to actually build it.** Only if the closed-trade data shows the
"goes green then stops out" pattern is frequent (>20% of losing trades had
unrealized gain ≥ 1R at some point). Without that pattern in the data,
scale-outs add complexity without recovering meaningful PnL.

**Where it would land.**
[paper_portfolio.py:check_and_close_positions](../wins/execution/paper_portfolio.py#L53)
— add a "partial close" branch before the full SL/TP branches. Requires
schema change: `trade_log` needs to support partial fills (either a
`scaled_out_qty` column or splitting one trade into two rows on scale-out).

---

## Post-Proof Enhancement 2 — Trailing stop after target

**The idea.** When price hits the original target, instead of fully exiting,
"trail" a stop-loss behind the price as it continues up. The stop only ever
moves up, never down. Exit when price reverses enough to hit the trailing
stop. This captures fat-tailed winners that the fixed target would have cut
off.

**Why it's deferred.** Trailing stops trade certainty for upside. A token
that hits +15% target and then runs to +50% is a clear win for trailing.
But a token that hits +15% target and then reverses to +8% before stopping
out is a loss versus the fixed-target version (you gave back 7%). Whether
the trade-off is worth it depends entirely on the distribution of *how
winners actually behave* in this system — which is exactly what the
post-proof data tells you.

**Decision rule.** Build this only if closed-trade analysis shows that
when the system hits target, the price continues meaningfully higher
(median continuation of >0.5x the original target distance) more than ~40%
of the time. If targets tend to be approximate tops, fixed exits are
correct and trailing would just give back gains.

**Where it would land.**
[paper_portfolio.py:check_and_close_positions](../wins/execution/paper_portfolio.py#L53)
— replace the `price >= pos.target_price` full-exit branch with a
state-machine that, on first target touch, switches the position into a
"trailing" mode. Requires `trade_log` to track `peak_price` since target
hit, plus a configurable trail distance (likely 0.5x the original
entry-to-target distance).

---

## Post-Proof Enhancement 3 — Combined: scale-out + trail

**The idea.** Take half off at target (lock the bulk of the win), then
trail the remaining half. This is the conservative-aggressive hybrid:
guarantees a respectable win on every target hit, while preserving upside
optionality on the half that runs.

**Why last.** This is the natural endpoint if both Enhancement 1 and
Enhancement 2 prove out independently. If only one proves out, build only
that one. If neither does, the fixed-target system was already correct and
the proof phase has validated the simple design.

---

## Things explicitly NOT on the roadmap

- **Pyramiding (adding to winners).** Doubles position-sizing complexity
  for marginal gain; current "no pyramiding" rule in
  [risk.py:64-72](../wins/execution/risk.py#L64-L72) stays.
- **Time-based exits ("close everything Friday").** Crypto trades 24/7;
  weekday heuristics from equities don't apply.
- **Leverage.** Out of scope; capital preservation > capital multiplication
  during the proof phase.
- **Auto-tuning the prompt from outcomes.** The
  [Self-Learning Prompt](SELF_LEARNING_PROMPT.md) doc covers this; it has
  its own gating criteria.
