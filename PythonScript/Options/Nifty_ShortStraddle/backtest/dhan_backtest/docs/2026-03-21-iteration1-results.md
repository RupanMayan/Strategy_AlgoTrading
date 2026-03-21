# Iteration 1 Backtest Results — Nifty Short Straddle (Core)

**Date:** 2026-03-21
**Period:** 2025-03-21 to 2026-03-21 (1 year)
**Data Source:** Dhan expired options API (1-min ATM weekly NIFTY options)
**Data Points:** 92,737 rows (1-min candles)

---

## Strategy Scope (Core Only)

- ATM short straddle entry (SELL CE + SELL PE)
- Per-leg fixed SL with DTE overrides (20% default, 25/28/30 for DTE 2/3/4)
- Dynamic time-of-day SL tightening (15% at 12:00, 10% at 13:30, 7% at 14:30)
- Hard exit at 15:15 IST
- Daily profit target (₹5,000/lot) and loss limit (₹4,000/lot)
- DTE-aware entry times (09:30-09:45)

**Not included:** Trailing SL, partial square-off, breakeven SL, combined decay exits, winner-leg booking, VIX/IVR/IVP filters, ORB filter, re-entry logic

---

## Summary Statistics

| Metric | Value |
|---|---|
| Total Trades | 224 |
| Total P&L | **+₹8,207.57** |
| Win Rate | 47.8% |
| Avg Win | ₹1,990.11 |
| Avg Loss | -₹1,749.86 |
| Profit Factor | 1.04 |
| Max Drawdown | -₹28,696.11 |
| Sharpe Ratio | 0.24 |
| Sortino Ratio | 0.46 |
| Calmar Ratio | 0.29 |

---

## SL Hit Analysis

| Metric | Count | % |
|---|---|---|
| CE SL Hits | 134 | 59.8% |
| PE SL Hits | 136 | 60.7% |
| Both SL Hit | 72 | 32.1% |
| No SL (full decay) | 26 | 11.6% |

---

## Per-DTE Performance

| DTE | Day | Trades | Total P&L | Avg P&L | Win Rate |
|-----|-----|--------|-----------|---------|----------|
| 0 | Tuesday (expiry) | 47 | +₹29,469 | +₹627 | 57% |
| 1 | Monday | 48 | -₹15,432 | -₹322 | 40% |
| 2 | Friday | 49 | -₹23,729 | -₹484 | 39% |
| 3 | Thursday | 46 | +₹5,991 | +₹130 | 50% |
| 4 | Wednesday | 34 | +₹11,909 | +₹350 | 56% |

---

## Monthly P&L

### 2025
| Month | P&L |
|-------|-----|
| Mar | +₹7,860 |
| Apr | -₹918 |
| May | +₹2,337 |
| Jun | -₹1,229 |
| Jul | -₹13,917 |
| Aug | +₹9,048 |
| Sep | -₹7,811 |
| Oct | +₹6,173 |
| Nov | +₹8,882 |
| Dec | — |

### 2026
| Month | P&L |
|-------|-----|
| Jan | -₹7,490 |
| Feb | +₹8,800 |
| Mar | -₹3,528 |

---

## Key Observations

1. **DTE 0 (expiry day) is the clear winner** — highest total P&L, highest win rate. Peak theta decay on expiry day makes the straddle most profitable.

2. **DTE 1 & 2 are consistently loss-making** — these days have lower theta and higher gamma risk. The fixed SL gets hit frequently without enough premium decay to compensate.

3. **SL hit rate is very high (60%+)** — both legs get stopped out in 32% of trades. This suggests the core strategy without trailing SL is too rigid — trailing SL would allow winning legs to run further before locking in.

4. **Only 11.6% of trades have no SL hit** — the vast majority of trades have at least one leg stopped out, confirming that advanced risk management (trailing SL, breakeven SL after partial) is critical.

5. **Monthly P&L is volatile** — swings from -₹13,917 (Jul 2025) to +₹9,048 (Aug 2025). The max drawdown of -₹28,696 indicates significant intra-month risk.

6. **Profit factor of 1.04 is marginal** — barely profitable without advanced features. This strongly suggests the production strategy's trailing SL, partial square-off, and winner-leg booking features provide the real edge.

---

## Implications for Iteration 2

The most impactful features to add next (in order of expected P&L impact):

1. **Trailing SL** — would allow winning legs to continue decaying instead of getting stopped out at fixed levels, especially on DTE 0/3/4 where theta works in our favor
2. **Partial square-off** — close only the losing leg while the winner continues, capturing more theta
3. **Combined decay exits** — exit when both legs have decayed sufficiently (avoids holding till hard exit)
4. **Winner-leg booking** — book the surviving leg after partial exit when it's deeply profitable

---

## Files

- Trade log: `output/bt_trades.csv` (224 trades, per-day detail)
- Summary: `output/bt_summary.json` (aggregate metrics)
- Charts: `output/charts/` (equity curve, drawdown, monthly heatmap, DTE breakdown)
