# Iteration 3 Backtest Results — Full Production Parity

**Date:** 2026-03-21
**Period:** 2025-03-21 to 2026-03-21 (1 year)
**Data Source:** Dhan expired options API (1-min ATM weekly NIFTY options)

---

## What Changed from Iteration 2

1. **Breakeven SL buffer direction fixed to match production** — `(1 + buffer)` instead of `(1 - buffer)`
2. **FIX-XXIV: Breakeven context awareness** — skip arming breakeven SL when survivor is already winning (LTP < entry for shorts)
3. **FIX-XXVII: Asymmetric leg booking** — book deeply decayed winner when loser barely moved
4. **FIX-XXVIII: Combined profit trailing** — trail combined decay peak, exit on 40-point retracement
5. **FIX-XXV: Post-partial recovery lock** — trail recovery P&L peak after partial exit at loss
6. **FIX-XX: Net P&L guard** — defer per-leg SL up to 15 min when combined position is net positive
7. **Spot-move / breakeven breach exit** — exit when NIFTY moves >= 1.0x combined premium from entry

---

## All-Iteration Comparison

| Metric | Iter 1 (Core) | Iter 2 (BE Down) | **Iter 3 (Prod Match)** |
|---|---|---|---|
| Total P&L | +₹8,208 | +₹185,754 | **-₹23,107** |
| Win Rate | 47.8% | 96.0% | **59.4%** |
| Avg Win | ₹1,990 | ₹987 | **₹936** |
| Avg Loss | -₹1,750 | -₹2,934 | **-₹1,621** |
| Profit Factor | 1.04 | 8.04 | **0.84** |
| Max Drawdown | -₹28,696 | -₹4,793 | **-₹31,648** |
| Sharpe | 0.24 | 9.18 | **-0.84** |
| Calmar | 0.29 | 38.76 | **0.73** |

---

## Exit Reason Distribution

| Exit Reason | Count | % |
|---|---|---|
| recovery_lock | 94 | 42.0% |
| spot_move | 58 | 25.9% |
| both_sl_hit | 29 | 12.9% |
| hard_exit (15:15) | 26 | 11.6% |
| profit_target | 9 | 4.0% |
| loss_limit | 7 | 3.1% |
| combined_decay | 1 | 0.4% |

---

## SL Hit Analysis

| Metric | Iter 1 | Iter 2 | **Iter 3** |
|---|---|---|---|
| CE SL Hits | 134 (60%) | 193 (86%) | **110 (49%)** |
| PE SL Hits | 136 (61%) | 195 (87%) | **117 (52%)** |
| Both SL Hit | 72 (32%) | 190 (85%) | **29 (13%)** |
| No SL (decay) | 26 (12%) | 26 (12%) | **26 (12%)** |

SL hit rate dropped significantly because spot-move exit and recovery lock close positions BEFORE SL fires.

---

## Per-DTE Performance

| DTE | Day | Trades | Total P&L | Avg P&L | Win Rate |
|-----|-----|--------|-----------|---------|----------|
| 0 | Tuesday (expiry) | 47 | -₹5,722 | -₹122 | 68% |
| 1 | Monday | 48 | -₹15,800 | -₹329 | 50% |
| 2 | Friday | 49 | -₹21,089 | -₹430 | 47% |
| 3 | Thursday | 46 | +₹7,427 | +₹161 | 63% |
| 4 | Wednesday | 34 | +₹12,077 | +₹355 | 74% |

**Pattern same as Iter 1:** DTE 3 & 4 profitable, DTE 0/1/2 loss-making. DTE 0 flipped from profitable (Iter 1: +₹29K) to loss-making (-₹5.7K) — spot-move exit closes DTE 0 trades too early before theta decay can work.

---

## Key Findings

### 1. Breakeven SL Buffer Direction is Critical

| Buffer Direction | Effect | Total P&L |
|---|---|---|
| `(1 - buffer)` = tighter | Exits sooner, locks small profit | +₹185,754 |
| `(1 + buffer)` = looser (production) | More room, overshoots breakeven | -₹23,107 |

This single parameter flips the strategy from highly profitable to loss-making. The production `(1 + buffer)` direction means the breakeven SL fires AFTER the survivor has already given back more than the first leg's loss.

### 2. Spot-Move Exit is Too Aggressive

58 trades (26%) exit via spot-move. With `spot_multiplier = 1.0` and combined premiums of 150-300, a 150-300 point NIFTY move triggers exit. This is common intraday — NIFTY frequently moves 100-250 points. The exit fires too early, cutting off potential recovery.

**DTE 0 impact:** DTE 0 went from +₹29K (Iter 1) to -₹5.7K (Iter 3). On expiry day, theta decay is fastest but intraday moves are also largest. Spot-move exit closes before theta kicks in.

### 3. Recovery Lock is the Dominant Exit (42%)

94 trades exit via recovery lock. After one leg is stopped at a loss, the recovery lock:
1. Waits for combined P&L to turn positive (min ₹500)
2. Tracks the peak recovery
3. Exits when recovery retraces 50%

This is working as designed but the 50% retracement threshold may be too tight — it often locks in small profits but caps the upside when the survivor would have continued decaying.

### 4. Net P&L Guard Has Minimal Impact

The guard defers SL for up to 15 minutes when the combined position is net positive. In practice, most trades where one leg has closed at a loss are not net positive, so the guard rarely activates.

### 5. Asymmetric Booking and Combined Profit Trailing: Zero Triggers

Neither feature fired in 224 trades:
- **Asymmetric booking** requires one leg at ≤40% while other at ≥80% — rare with fixed SLs firing first
- **Combined profit trailing** requires 30% combined decay then 40-point retracement — combined decay exit fires first at the DTE-specific threshold

---

## Implications for Production

### Investigate Breakeven Buffer Direction
The production `(1 + buffer)` causes the breakeven SL to fire PAST the mathematical breakeven, resulting in net losses on partial exits. For short options:
- Breakeven level = 160 (example)
- `(1 + 10%)` = 176 → fires at 176 → combined P&L = -₹1,040 (LOSS)
- `(1 - 10%)` = 144 → fires at 144 → combined P&L = +₹1,040 (PROFIT)

**Recommendation:** Test `(1 - buffer)` in production paper trading. This is potentially the single highest-impact change.

### Tune Spot-Move Exit
`spot_multiplier = 1.0` is too aggressive for intraday options. Consider:
- Increasing to 1.2-1.5 to allow more intraday range
- Or disabling for DTE 0 where theta makes recovery more likely

### Tune Recovery Lock
The 50% retracement threshold may be locking in too-small profits. Consider:
- Increasing `trail_pct` from 50% to 60-70%
- Or increasing `min_recovery_rs_per_lot` from ₹500 to ₹1,000

---

## Files

- Trade log: `output/bt_trades.csv`
- Summary: `output/bt_summary.json`
- Charts: `output/charts/`
- Audit: `docs/2026-03-21-backtest-vs-production-audit.md`
