# Iteration 2 Backtest Results — Nifty Short Straddle (Advanced Risk)

**Date:** 2026-03-21
**Period:** 2025-03-21 to 2026-03-21 (1 year)
**Data Source:** Dhan expired options API (1-min ATM weekly NIFTY options)
**Data Points:** 92,737 rows (1-min candles)

---

## Features Added (vs Iteration 1)

1. **Trailing SL** — Activate at 50% decay, lock at LTP × 1.15, ratchet-only (never loosens), capped at fixed SL
2. **Breakeven SL** — After partial exit at loss, tighten survivor SL to protect combined breakeven (5-min grace, 10% buffer)
3. **Combined decay exit** — Both legs active, DTE-aware thresholds (50-70%)
4. **Winner-leg booking** — Single survivor at ≤30% of entry price, book profit

---

## Summary Statistics

| Metric | Iteration 1 | **Iteration 2** | Change |
|---|---|---|---|
| Total Trades | 224 | **224** | — |
| Total P&L | +₹8,208 | **+₹185,754** | +22.6× |
| Win Rate | 47.8% | **96.0%** | +48.2pp |
| Avg Win | ₹1,990 | **₹987** | -50% |
| Avg Loss | -₹1,750 | **-₹2,934** | -68% |
| Profit Factor | 1.04 | **8.04** | +7.7× |
| Max Drawdown | -₹28,696 | **-₹4,793** | -83% |
| Sharpe Ratio | 0.24 | **9.18** | +38× |
| Sortino Ratio | 0.46 | **8.57** | +18.6× |
| Calmar Ratio | 0.29 | **38.76** | +134× |

---

## SL Hit Analysis

| Metric | Iteration 1 | **Iteration 2** |
|---|---|---|
| CE SL Hits | 134 (59.8%) | 193 (86.2%) |
| PE SL Hits | 136 (60.7%) | 195 (87.1%) |
| Both SL Hit | 72 (32.1%) | 190 (84.8%) |
| No SL (full decay) | 26 (11.6%) | 26 (11.6%) |

### Exit SL Type Breakdown (New in Iter 2)

| SL Type | CE Exits | PE Exits |
|---|---|---|
| Fixed/Dynamic | 128 (57%) | 133 (59%) |
| Breakeven | 95 (42%) | 89 (40%) |
| Trailing | 1 (<1%) | 2 (<1%) |

---

## Exit Reason Distribution

| Exit Reason | Count | % |
|---|---|---|
| both_sl_hit | 190 | 84.8% |
| hard_exit (15:15) | 20 | 8.9% |
| profit_target | 9 | 4.0% |
| loss_limit | 4 | 1.8% |
| combined_decay | 1 | 0.4% |

---

## Per-DTE Performance

| DTE | Day | Trades | Total P&L | Avg P&L | Win Rate |
|-----|-----|--------|-----------|---------|----------|
| 0 | Tuesday (expiry) | 47 | +₹30,606 | +₹651 | 96% |
| 1 | Monday | 48 | +₹42,147 | +₹878 | 94% |
| 2 | Friday | 49 | +₹42,955 | +₹877 | 98% |
| 3 | Thursday | 46 | +₹39,967 | +₹869 | 98% |
| 4 | Wednesday | 34 | +₹30,079 | +₹885 | 94% |

**Key change from Iter 1:** DTE 1 & 2 flipped from loss-making (-₹15,432 and -₹23,729) to the **most profitable** DTEs (+₹42,147 and +₹42,955). Breakeven SL protection is most impactful on these days where gamma risk is highest.

---

## Slippage Sensitivity

| Slippage | Total P&L | Win Rate | Change vs 0% |
|----------|-----------|----------|---------------|
| 0% | ₹185,754 | 96.0% | — |
| 1% | ₹173,271 | 96.0% | -₹12,483 (-6.7%) |
| 2% | ₹155,862 | 95.5% | -₹29,892 (-16.1%) |

Strategy is robust to slippage — maintains 95%+ win rate even at 2% slippage.

---

## Feature Isolation Analysis

| Configuration | Total P&L | Win Rate |
|---|---|---|
| Iter 1 (core only) | +₹8,208 | 47.8% |
| Iter 2 ALL features OFF except breakeven SL | +₹179,486 | 95.5% |
| Iter 2 ALL features ON except breakeven SL | -₹17,145 | 50.0% |
| **Iter 2 ALL features ON** | **+₹185,754** | **96.0%** |

**Breakeven SL is the dominant edge.** It contributes ~97% of the P&L improvement. Trailing SL, combined decay exit, and winner-leg booking are marginal in isolation.

---

## Key Observations

1. **Breakeven SL is the game-changer.** After one leg hits SL at a loss, the breakeven SL on the survivor exits at a level that ensures the combined trade P&L is slightly positive (avg +₹987). This converts ~48% of losing trades into small winners.

2. **Trailing SL rarely activates (3/224 trades).** The 50% decay trigger is rarely reached during a single trading day for ATM options. Trailing SL may become more impactful with different trigger levels or on DTE 0 where theta decay is fastest.

3. **Combined decay exit & winner-leg booking are negligible.** Only 1 combined decay exit and 0 winner bookings in 224 trades. These features are "blocked" by the breakeven SL — once one leg exits, the other exits via breakeven SL before reaching the decay/booking thresholds.

4. **Risk-reward is inverted but profitable.** 96% win rate × ₹987 avg win vs 4% loss rate × -₹2,934 avg loss. This is characteristic of short volatility strategies — many small wins, rare large losses. The max drawdown of ₹4,793 (vs ₹28,696 in Iter 1) shows the breakeven SL significantly reduces tail risk.

5. **All DTEs now profitable.** DTE 1 & 2, which were loss-making in Iter 1 (-₹15K and -₹24K), are now the top earners (+₹42K each). The breakeven SL is most impactful where gamma risk is highest.

---

## Bugs Fixed During Iteration 2

1. **Same-candle trailing SL activation + hit** — Trailing SL activated using `close` but immediately checked against `high` in the same 1-min candle. Fixed with 1-candle grace period.

2. **Breakeven SL buffer direction inverted** — Buffer was applied as `raw_be × (1 + buffer%)`, which moved the SL PAST breakeven for short options (causing consistent small losses). Fixed to `raw_be × (1 - buffer%)`, which exits BEFORE breakeven (locking in small net profits).

3. **`ce_trailing_activated` output field always True** — Was `ce_trailing_active or (not ce_active and ce_sl_hit)`, always True for any SL hit. Fixed to only reflect genuine trailing SL activation.

---

## Caveats

- **ATM rolling data limitation**: Dhan API returns the ATM option at each moment. If the ATM strike shifts during the day, the data may show premium from different strikes, potentially understating intraday price swings.
- **Zero slippage baseline**: Default results use 0% slippage. Even with 2% slippage, results remain strong (₹155K, 95.5% win rate).
- **No transaction costs**: Brokerage, STT, exchange charges, and GST are not modeled. See `charges_impact.py` for cost analysis.
- **1-lot backtest**: Results scale linearly with lot count, but margin requirements and slippage impact increase non-linearly.

---

## Files

- Trade log: `output/bt_trades.csv` (224 trades, per-day detail with SL type tracking)
- Summary: `output/bt_summary.json` (aggregate metrics)
- Charts: `output/charts/` (equity curve, drawdown, monthly heatmap, DTE breakdown, exit reasons pie)
