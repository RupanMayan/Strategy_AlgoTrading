# Nifty Short Straddle — Final Optimised Backtest Report

**Date:** 2026-03-21
**Config:** `config_optimized.toml` (v7.0.0) — synced with production `config.toml`
**Data:** Dhan expired options API, 1-min OHLC + IV + OI + spot
**Period:** 2025-03-21 to 2026-03-21 (1 year, 224 trading days)
**Data Points:** 92,737 candles

---

## Executive Summary

The optimised strategy generated **₹190,936** net profit over 1 year with a **64.8% win rate** and **profit factor of 2.18**. Maximum drawdown was contained to **₹12,812**. All 5 DTEs and all 12 trading months were profitable. Re-entry after SL exits contributed an additional **₹28,892** (+15%).

---

## Overall Performance

| Metric | Value |
|---|---|
| **Total P&L** | **₹190,936** |
| Total Trades | 290 |
| Winners | 188 (64.8%) |
| Losers | 101 (35.2%) |
| Breakeven | 1 |

## P&L Statistics

| Metric | Value |
|---|---|
| Avg P&L/Trade | ₹658 |
| Median P&L/Trade | ₹822 |
| Avg Win | ₹1,873 |
| Avg Loss | -₹1,596 |
| Max Win | ₹10,397 |
| Max Loss | -₹6,715 |
| Gross Wins | ₹352,165 |
| Gross Losses | ₹161,229 |

## Risk Metrics

| Metric | Value |
|---|---|
| **Profit Factor** | **2.18** |
| **Max Drawdown** | **-₹12,812** |
| Peak Equity | ₹194,758 |
| **Sharpe Ratio** | **4.61** |
| Sortino Ratio | 6.84 |
| Calmar Ratio | 14.90 |
| Recovery Factor | 14.90 |
| Payoff Ratio | 1.17 |
| Expectancy/Trade | ₹653 |
| Max Consecutive Wins | 15 |
| Max Consecutive Losses | 6 |

---

## Per-DTE Breakdown

| DTE | Day | Trades | P&L | Win Rate | Avg P&L |
|---|---|---|---|---|---|
| 0 | Tuesday (Expiry) | 77 | +₹81,179 | 67.5% | +₹1,054 |
| 1 | Monday | 48 | +₹26,099 | 66.7% | +₹544 |
| 2 | Friday | 55 | +₹18,967 | 63.6% | +₹345 |
| 3 | Thursday | 76 | +₹34,336 | 57.9% | +₹452 |
| 4 | Wednesday | 34 | +₹30,355 | 73.5% | +₹893 |

**All 5 DTEs profitable.** DTE0 (expiry day) is the strongest performer with highest avg P&L. DTE4 (Wednesday) has the highest win rate at 73.5%.

---

## Monthly P&L

| Month | Trades | P&L | Win Rate |
|---|---|---|---|
| 2025-03 | 7 | +₹9,106 | 71.4% |
| 2025-04 | 28 | +₹7,260 | 64.3% |
| 2025-05 | 27 | +₹29,133 | 70.4% |
| 2025-06 | 25 | +₹19,555 | 72.0% |
| 2025-07 | 32 | +₹9,631 | 53.1% |
| 2025-08 | 26 | +₹19,796 | 73.1% |
| 2025-09 | 28 | +₹4,931 | 50.0% |
| 2025-10 | 27 | +₹7,043 | 59.3% |
| 2025-11 | — | Skipped | — |
| 2025-12 | 29 | +₹30,211 | 79.3% |
| 2026-01 | 26 | +₹20,259 | 61.5% |
| 2026-02 | 21 | +₹20,943 | 61.9% |
| 2026-03 | 14 | +₹13,068 | 71.4% |

**All 12 trading months profitable.** Best: Dec 2025 (+₹30,211, 79.3% WR). Weakest: Sep 2025 (+₹4,931, 50% WR) — still positive.

---

## Exit Reasons

| Exit Reason | Count | % | P&L |
|---|---|---|---|
| Hard exit (15:15) | 164 | 56.6% | +₹174,729 |
| Winner booking | 65 | 22.4% | +₹105,920 |
| Both SL hit | 51 | 17.6% | -₹86,732 |
| Loss limit | 5 | 1.7% | -₹31,096 |
| Combined decay | 2 | 0.7% | +₹7,371 |
| Profit target | 2 | 0.7% | +₹20,563 |
| Asymmetric book | 1 | 0.3% | +₹182 |

**56.6% of trades hold to hard exit** — capturing maximum theta decay. Winner booking (22.4%) locks deep-decay profits on surviving legs after partial SL.

---

## Re-Entry Analysis

| Trade Type | Trades | P&L | Win Rate |
|---|---|---|---|
| First trade | 224 | +₹162,043 | 66.5% |
| Re-entry #1 | 41 | +₹17,871 | 58.5% |
| Re-entry #2 | 25 | +₹11,021 | 60.0% |

Re-entries contribute **+₹28,892** (15% of total P&L) with lower but still positive win rates. The 45-minute cooldown ensures re-entries happen after market stabilisation.

---

## Optimised Configuration (v7.0.0)

```toml
[risk]
leg_sl_percent              = 30.0     # Was 20% — wider SL lets trades breathe
daily_profit_target_per_lot = 10000    # Was 5000 — captures full theta
daily_loss_limit_per_lot    = -6000    # Was -4000 — fewer premature stops

[risk.dynamic_sl]
enabled = false                        # Was true — hurts with wider SL

[risk.trailing_sl]
enabled = false                        # Was true — rarely triggers

[risk.breakeven_sl]
buffer_pct = 5.0                       # Was 10% — tighter protection

[risk.combined_decay_exit.dte_override]
"0" = 60.0                             # Was 70% — exit expiry day slightly earlier

[risk.recovery_lock]
enabled = false                        # Was true — was cutting profits

[risk.spot_move_exit]
enabled = false                        # Was true — premature exits

[risk.reentry]
enabled              = true
max_per_day          = 2               # Was 1
cooldown_min         = 45              # Was 30
max_loss_per_lot     = 2000
```

### Key Insight: Simplicity Wins

The optimised strategy **disabled 4 features** (trailing SL, dynamic SL, recovery lock, spot-move exit) that were actively hurting performance. The core edge comes from:

1. **Wide SL (30%)** — lets trades survive morning volatility
2. **Breakeven SL (5% buffer)** — protects after partial SL hits
3. **Hard exit at 15:15** — captures maximum theta decay
4. **Winner booking** — locks deep-decay profits on surviving legs
5. **Re-entry** — recovers from early SL exits

---

## Charts

- Equity curve: `output/charts/final_optimised_equity_curve.png`
- DTE breakdown: `output/charts/final_optimised_dte_breakdown.png`
- Monthly P&L: `output/charts/final_optimised_monthly_pnl.png`
- Trade log: `output/final_optimised_trades.csv`

---

## Comparison: Before vs After Optimisation

| Metric | Before (v6.4.0) | After (v7.0.0) | Change |
|---|---|---|---|
| Total P&L | -₹23,107 | +₹190,936 | **+₹214,043** |
| Win Rate | 59.4% | 64.8% | +5.4% |
| Profit Factor | 0.84 | 2.18 | +1.34 |
| Max Drawdown | -₹31,648 | -₹12,812 | +₹18,836 (59% less) |
| Sharpe | — | 4.61 | — |
| Trades | 224 | 290 | +66 (re-entries) |

---

## Risk Warnings

1. **Overfitting risk:** Optimised on 1-year in-sample data. Forward walk / out-of-sample validation recommended before increasing lot size.
2. **Slippage:** At 1% slippage, P&L drops to ~₹168K. At 3%, still profitable (~₹105K).
3. **Market regime:** Strategy tested during a specific volatility regime. Performance may differ in extreme VIX environments (>30) or prolonged low-VIX (<12) periods.
4. **Re-entry caveat:** Re-entries use same-strike options at new prices. In production, ATM strike may shift — actual re-entry fills may differ.
5. **Paper trade first:** Run with 1 lot in paper mode for 2-4 weeks before scaling.
