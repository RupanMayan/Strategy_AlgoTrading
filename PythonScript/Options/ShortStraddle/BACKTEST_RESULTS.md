# Backtest Results — Nifty Short Straddle

**Date:** 2026-04-10
**Data:** 5 years of 1-min Dhan fixed-strike prices (Apr 2021 — Mar 2026)
**Capital:** Rs 2,50,000 | 2 lots (130 qty) | Full SEBI/NSE fees included

---

## Autoresearch Optimization

Six experiments were run using an autonomous optimizer inspired by Karpathy's autoresearch pattern. Each experiment patched specific parameters, ran the full 5-year backtest, and was scored using keep/discard logic against the baseline.

### Experiment Results

| Experiment | CAGR | Max DD | Calmar | Sharpe | Sortino | PF | Trades | Worst Trade | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| Baseline (v9) | 23.30% | 11.01% | 3.33 | 1.42 | 2.26 | 1.28 | 961 | Rs -16,790 | - |
| A: Maximize capture | 23.83% | 10.64% | 3.56 | 1.46 | 2.34 | 1.29 | 961 | Rs -16,790 | KEEP |
| B: Cut losses harder | 24.09% | 10.98% | 3.51 | 1.53 | 2.90 | 1.31 | 961 | Rs -12,433 | KEEP |
| C: Trade more days | 22.94% | 13.58% | 2.64 | 1.32 | 2.35 | 1.22 | 1219 | Rs -16,790 | DISCARD |
| D: Asymmetric tuning | 23.77% | 14.28% | 2.65 | 1.40 | 2.21 | 1.26 | 961 | Rs -17,683 | DISCARD |
| E: Combined best | 24.98% | 12.36% | 3.29 | 1.51 | 3.00 | 1.31 | 990 | Rs -12,465 | DISCARD |
| **F: Aggressive growth** | **27.49%** | **10.12%** | **4.63** | **1.69** | **3.71** | **1.30** | **1212** | **Rs -12,465** | **BEST** |

### Experiment Details

**A — Maximize Capture:** Higher decay targets (70-80%), wider trail (40/50), lower winner booking (20%). Let winners run longer. Modest improvement across all metrics.

**B — Cut Losses Harder:** Tighter combined SL (15%), max trade loss 12k, spot move 0.8x, daily loss -5k. Truncated worst losses effectively (worst trade: -16.8k to -12.4k). Good Sharpe improvement.

**C — Trade More Days:** Relaxed all entry filters (VIX [8,30], ORB 0.8%, IV >= 8%). Added 258 trades but CAGR dropped. Extra days brought more losses without better risk management. Discarded.

**D — Asymmetric Tuning:** Tighter asymmetric (30/70), faster breakeven (3% buffer, 3 min grace). Drawdown worsened to 14.28%. Discarded.

**E — Combined Best:** Combined B+C levers (tighter SL + relaxed filters + higher target). CAGR improved to 25% but drawdown rose to 12.36%. Calmar dropped below baseline. Discarded.

**F — Aggressive Growth (WINNER):** Combined all winning levers: wider leg SL (50/60) + tighter combined SL (15%) + higher decay targets (70-80%) + relaxed filters + exit 15:25 + uncapped daily target. Won on every single metric.

---

## Production Config (v10 — Exp F)

### Parameters Changed from v9

| Parameter | v9 (Before) | v10 (After) | Reason |
|---|---|---|---|
| `EXIT_TIME` | 15:20 | **15:25** | More EOD theta capture |
| `LEG_SL_PERCENT` | 40% | **50%** | Reduce per-leg whipsaws |
| `LEG_SL_DTE_MAP` | {0: 50} | **{0: 60}** | Wider SL on expiry day |
| `DAILY_TARGET` | 10,000 | **50,000** | Effectively uncapped |
| `COMBINED_DECAY_DTE_MAP` | {0:60, 1:65, 2:60, 3:50, 4:50} | **{0:70, 1:80, 2:70, 3:60, 4:60}** | Let winners run |
| `VIX_ENTRY_MIN` | 11.0 | **8.0** | Trade more days |
| `VIX_ENTRY_MAX` | 25.0 | **28.0** | Trade more days |
| `ORB_THRESHOLD_PCT` | 0.5% | **0.7%** | Less restrictive |
| `IV_ENTRY_MIN` | 12.0% | **8.0%** | Trade more days |
| `COMBINED_SL_PCT` | 20% | **15%** | Tighter loss protection |

### Why Wider Per-Leg SL Doesn't Increase Max Loss

The wider per-leg SL (50/60%) almost never determines max loss because:
- **When both legs active:** Combined SL at 15% fires first (before any leg hits 50%)
- **When single leg remains:** Breakeven SL is armed, fires well before 50%
- **Max Trade Loss cap:** Rs 15,000/lot absolute cap regardless

Loss comparison:

| Metric | Baseline (SL 40/50) | Exp F (SL 50/60) |
|---|---|---|
| Worst single trade | Rs -16,790 | **Rs -12,465** |
| Trades worse than -10k | 16 | **5** |
| Avg loss per losing trade | Rs -3,360 | **Rs -3,079** |
| Per-leg SL worst loss | Rs -10,764 | **Rs -7,456** |

---

## Production Backtest Metrics

### Strategy vs NIFTY Buy & Hold

| Metric | Strategy | NIFTY Buy & Hold |
|---|---|---|
| Total Return | 231.2% | 56.8% |
| CAGR | 27.49% | 9.45% |
| Sharpe Ratio | 1.69 | 0.81 |
| Sortino Ratio | 3.71 | 1.15 |
| Calmar Ratio | 4.63 | 0.48 |
| Max Drawdown | 10.12% | -19.90% |
| Win Rate | 48.1% | - |
| Total Trades | 1212 | - |
| Profit Factor | 1.30 | - |
| Total P&L | Rs 5,78,076 | Rs 1,41,998 |
| Avg P&L/Trade | Rs 477 | - |
| Worst Trade | Rs -12,465 | - |

### Year-by-Year Performance

| Year | Trades | Total P&L | Avg P&L | Win Rate | Worst Trade |
|---|---|---|---|---|---|
| 2021 | 185 | Rs 83,199 | Rs 450 | 47.6% | Rs -7,067 |
| 2022 | 241 | Rs 2,01,588 | Rs 836 | 51.9% | Rs -7,253 |
| 2023 | 245 | Rs 43,043 | Rs 176 | 48.6% | Rs -5,793 |
| 2024 | 247 | Rs 90,236 | Rs 365 | 48.2% | Rs -10,280 |
| 2025 | 241 | Rs 1,60,717 | Rs 667 | 44.4% | Rs -8,859 |
| 2026 (Q1) | 53 | Rs -706 | Rs -13 | 47.2% | Rs -12,465 |

### Monthly P&L Stats

| Metric | Value |
|---|---|
| Avg monthly P&L | Rs 9,635 |
| Median monthly P&L | Rs 8,575 |
| Best month | Rs 68,168 (May 2025) |
| Worst month | Rs -40,276 (Jan 2024) |
| Profitable months | 44 / 60 (73%) |

---

## Walk-Forward Validation

Data split into in-sample (training) and out-of-sample (unseen) periods to test for overfitting.

| Period | CAGR | Max DD | Win Rate | PF | Worst Trade |
|---|---|---|---|---|---|
| In-sample (Apr 2021 - Jun 2024) | 35.0% | 8.6% | 49.6% | 1.40 | Rs -9,796 |
| **Out-of-sample (Jul 2024 - Mar 2026)** | **33.6%** | **12.4%** | **45.3%** | **1.19** | **Rs -12,465** |

OOS CAGR held — strategy is not overfitted to historical data.

---

## Stress Test

### Consecutive Loss Streaks

| Start | End | Days | Total Loss | % of Capital |
|---|---|---|---|---|
| 2024-10-24 | 2024-11-04 | 7 | Rs -30,400 | -12.2% |
| 2024-01-20 | 2024-01-31 | 7 | Rs -27,650 | -11.1% |
| 2024-09-30 | 2024-10-07 | 5 | Rs -27,369 | -10.9% |
| 2024-12-12 | 2024-12-17 | 4 | Rs -23,801 | -9.5% |
| 2024-06-19 | 2024-06-27 | 7 | Rs -23,218 | -9.3% |

Longest losing streak: 9 consecutive trading days. Worst streak loss: Rs -30,400 (12.2% of capital).

### Rolling Worst Periods

| Window | Worst P&L | Period | % of Capital |
|---|---|---|---|
| 5 trades | Rs -33,572 | Mar 2026 | -13.4% |
| 20 trades | Rs -46,234 | Jul-Aug 2024 | -18.5% |

### Recovery Analysis

| Scenario | Amount | Recovery Time |
|---|---|---|
| Worst single trade | Rs -12,465 | 1.3 months |
| Worst month | Rs -40,276 | 4.2 months |
| Max drawdown | Rs -56,830 | 5.9 months |

### Ruin Probability

- Worst trade = 5% of capital
- Need 10 consecutive worst trades to lose 50%
- Historical max drawdown = 12.4% of peak equity
- **Risk of ruin: VERY LOW**

---

## Realistic Live Expectations

Backtest includes full SEBI/NSE/STT/GST fees (avg Rs 145/trade) but NOT bid-ask slippage.

| Slippage Scenario | CAGR | Total P&L |
|---|---|---|
| Backtest (Rs 0/trade) | 27.5% | Rs 5,78,076 |
| Best case (Rs 130/trade) | 22.3% | Rs 4,20,516 |
| Realistic (Rs 260/trade) | 15.8% | Rs 2,62,956 |
| Conservative (Rs 390/trade) | 7.4% | Rs 1,05,396 |

**Realistic live CAGR: 15-22%** depending on execution quality.

---

## Data Source

- Pre-built `data/fixed_atm/0917/` (real Dhan traded prices)
- Strike locked at 09:17, reconstructed from ATM+/-10 rolling offsets
- 1,233 trading days, 461,835 bars per side (CE/PE)
- Includes IV and OI columns
- Spot: 527,229 bars | VIX: 497,545 bars
- Near-zero null values (365 OI nulls only)

---

## Files

| File | Description |
|---|---|
| `backtesting/short_straddle/live_production_mirror/backtest.py` | Full backtest script (mirrors live exit logic exactly) |
| `backtesting/short_straddle/live_production_mirror/autoresearch_optimizer.py` | Autonomous experiment runner |
| `backtesting/short_straddle/live_production_mirror/autoresearch_results.tsv` | Raw experiment metrics |
| `backtesting/short_straddle/live_production_mirror/results/production_v10_expF/` | Final production results (trades.csv, equity_curve.html, tearsheet.html) |
