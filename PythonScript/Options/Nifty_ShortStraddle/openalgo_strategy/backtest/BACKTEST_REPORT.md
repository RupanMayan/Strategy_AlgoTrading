# Nifty Short Straddle — Backtest Report & Optimization Summary

**Date**: 2026-03-28
**Period**: April 2021 – March 2026 (5 years, 1226 trading days)
**Capital**: Rs 2,50,000 (fixed)
**Data Source**: Dhan API — 1-minute candle data (Nifty Spot, ATM CE, ATM PE, India VIX)

---

## Strategy Overview

- **Type**: Sell ATM CE + ATM PE (Short Straddle) on Nifty 50, intraday MIS
- **Entry**: 09:17 IST daily
- **Exit**: 15:15 IST (hard time exit) or earlier via 13 exit modules
- **Lot Sizing**: Dynamic — uses SEBI historical lot sizes (25 → 75 → 65) and capital-based allocation

### 13 Exit Modules (Priority Order)
1. Per-Leg SL (30% above entry premium)
2. Net PNL Guard (defer SL up to 15 min if net position positive)
3. Breakeven SL (activated after one leg closes at loss)
4. Combined Decay Exit (60% decay, DTE-mapped)
5. Asymmetric Exit (winner ≤40%, loser ≥80%)
6. Combined Trail (activate at 30% decay, exit on 40% retracement)
7. Winner Booking (survivor decayed to 30%)
8. VIX Spike Exit (15% rise + above 18 absolute)
9. Daily P&L Target (+Rs 10,000/lot)
10. Daily Loss Limit (-Rs 6,000/lot)
11. Time Exit (15:15)
12. Re-entry logic (now disabled — see optimization below)

---

## Backtest Engine Audit

Before optimization, the backtest engine was audited line-by-line against the production script (`nifty_short_straddle.py`). **7 bugs were found and fixed**:

| # | Bug | Impact | Fix |
|---|-----|--------|-----|
| 1 | SL exit price used `candle["close"]` | Better fills than reality | Changed to `max(candle["close"], sl)` |
| 2 | Slippage direction reversed | Both entry and exit were favorable | Entry: `-slippage` (lower SELL fill), Exit: `+slippage` (higher BUY fill) |
| 3 | Daily P&L limits checked cumulative | Trades not exiting when they should | Changed to per-trade check (matching production) |
| 4 | Re-entry max_loss used `cumulative_pnl` | Wrong re-entry blocking | Changed to `last_trade_pnl` |
| 5 | SL calculation missing `round(..., 2)` | Minor price drift | Added rounding |
| 6 | Re-entry not blocked after daily limits | Extra trades that shouldn't happen | Added cumulative P&L check against daily limits |
| 7 | Daily tracking used net P&L (after charges) | Re-entry gate mismatch with production | Changed to gross P&L (matching production `closed_pnl`) |

**Pre-fix results**: Rs 13L (inflated)
**Post-fix results**: Rs 9.50L (realistic)

### Backtest vs Production Sync: ~92%

The remaining ~8% gap is inherent to candle-based simulation:
- Candle HIGH for SL detection vs live LTP
- Candle CLOSE for decay calculation vs live price
- Fixed slippage (1 pt) vs actual broker fills
- Pre-fetched ATM data vs real-time ATM strike selection

---

## Optimization Tests

All tests used identical base configuration (dynamic lot sizing, same 13 exit modules, same charges). Only **one parameter changed per test**.

### Test Results

| Test | Change | Trades | Net P&L | Win% | PF | Sharpe | Max DD | Calmar |
|------|--------|--------|---------|------|-----|--------|--------|--------|
| **Baseline** | Current production config | 1,425 | Rs 9,50,048 | 58.8% | 1.72 | 3.47 | -89,759 | 10.58 |
| **No Re-entry** | `max_per_day = 0` | 1,220 | Rs 10,37,791 | 61.6% | 1.92 | 3.85 | -74,782 | 13.88 |
| SL 25% | `sl_percent = 25.0` | 1,419 | Rs 2,69,160 | 51.1% | 1.17 | 1.04 | -1,63,612 | 1.65 |
| No DTE 1 | `trade_dte = [0,2,3,4]` | 1,166 | Rs 7,82,625 | 58.9% | 1.73 | 3.65 | -61,631 | 12.70 |
| Loss -5000 | `loss_limit = -5000` | 1,425 | Rs 9,46,227 | 58.8% | 1.71 | 3.46 | -89,759 | 10.54 |
| Re-entry profit only | Re-enter only after winning trade | 1,388 | Rs 9,75,720 | 59.5% | 1.76 | 3.58 | -88,363 | 11.04 |

### Analysis

1. **No Re-entry (WINNER)**: Every single metric improved. Re-entry trades had a 42% win rate and lost Rs 87,743 total. Disabling them saved Rs 88K in losses and Rs 21K in charges.

2. **SL 25% (WORST)**: Tighter SL destroyed the strategy. Win rate dropped to 51%, max drawdown nearly doubled, profit factor collapsed to 1.17. The 30% SL gives legs enough room to recover.

3. **No DTE 1**: Reduced P&L by Rs 1.67L. Even though DTE 1 has the weakest per-trade average, removing it costs more than it saves. All DTEs are net positive.

4. **Loss -5000**: Virtually no change from baseline. The daily loss limit of -6000 rarely triggers, so tightening to -5000 doesn't help.

5. **Re-entry profit only**: Better than baseline (+Rs 26K) but still worse than no re-entry. Even after a profitable exit, afternoon re-entries have weaker edge due to decayed premiums.

### Why Re-entry Hurts

| Metric | With Re-entry | Without |
|--------|---------------|---------|
| Re-entry trades | 205 | 0 |
| Re-entry P&L | -Rs 87,743 | - |
| Re-entry win rate | 42% | - |
| Re-entry avg P&L | -Rs 428 | - |
| Hour 13-14 re-entry P&L | -Rs 72,151 | - |

Re-entries after SL hits are essentially chasing losses. Afternoon sessions have less time for theta decay, and the market has already moved significantly.

---

## Final Recommended Configuration

Applied to both production (`nifty_short_straddle.py`) and backtest (`config.toml`):

```toml
[instrument]
lot_size = 65
number_of_lots = 1
capital = 250000
dynamic_lot_sizing = true    # SEBI historical lot sizes
compound_capital = false     # Fixed capital mode

[timing]
entry_time = "09:17"
exit_time = "15:15"

[filters]
trade_dte = [0, 1, 2, 3, 4]
skip_months = []             # Trade all months including November

[risk.per_leg_sl]
sl_percent = 30.0            # DO NOT tighten — 25% destroys the strategy

[risk.daily_limits]
profit_target = 10000
loss_limit = -6000           # Tightening to -5000 has no effect

[risk.reentry]
max_per_day = 0              # DISABLED — saves Rs 88K and improves all metrics
cooldown_min = 45
max_loss = 2000
```

---

## Final Results

### Fixed Capital (1 lot, Rs 2.5L)

| Metric | Value |
|--------|-------|
| Total Trades | 1,220 |
| Net P&L | Rs 10,37,791 |
| ROI (5 year) | 415% |
| Annual ROI | 83% |
| Win Rate | 61.6% |
| Profit Factor | 1.92 |
| Sharpe Ratio | 3.85 |
| Calmar Ratio | 13.88 |
| Max Drawdown | Rs -74,782 (30% of capital) |
| Avg Win | Rs 2,882 |
| Avg Loss | Rs -2,414 |
| Best Month | Rs 81,489 (Mar 2022) |
| Worst Month | Rs -21,695 |
| Profitable Days | 61.6% |
| Total Charges | Rs 1,62,912 (13.6% of gross) |

### Compounded Capital

| Metric | Value |
|--------|-------|
| Total Trades | 1,220 |
| Net P&L | Rs 1,82,92,008 (Rs 1.83 Cr) |
| Win Rate | 62.5% |
| Profit Factor | 1.88 |
| Max Drawdown | Rs -10,33,535 |
| Calmar Ratio | 17.70 |

---

## Key Risks for Live Deployment

1. **Backtest ≠ Live (~8% gap)**: Expect slightly lower returns in production due to candle vs tick differences, real slippage, and broker execution delays.

2. **Max Drawdown**: Be prepared to sit through Rs 75K drawdowns (30% of capital) without stopping. This happened around Dec 2023.

3. **Losing streaks**: 2-3 losing months per year are normal. May 2023 was the worst at Rs -20K.

4. **Largest single loss**: Rs -24,073 (~10% of capital). These will happen.

5. **Charges**: Rs 1.63L over 5 years (13.6% of gross). Use a low-brokerage broker.

---

## Files & Dashboards

```
backtest/
├── config/
│   ├── config.toml                      # Final production-synced config
│   ├── opt_no_reentry.toml              # Test: no re-entry (WINNER)
│   ├── opt_sl25.toml                    # Test: SL 25% (WORST)
│   ├── opt_no_dte1.toml                 # Test: removed DTE 1
│   ├── opt_loss5000.toml                # Test: daily loss -5000
│   └── opt_reentry_profit_only.toml     # Test: re-entry after profit only
├── results/2026-03-28/
│   ├── fixed/index.html                 # Interactive dashboard (final, no re-entry)
│   ├── compounded/index.html            # Interactive dashboard (compounded)
│   └── optimization/
│       ├── no_reentry/index.html        # Dashboard for each optimization test
│       ├── sl25/index.html
│       ├── no_dte1/index.html
│       ├── loss5000/index.html
│       └── reentry_profit_only/index.html
├── scripts/
│   ├── run_backtest.py                  # Main backtest runner
│   ├── run_optimization.py              # Optimization test runner
│   ├── backtest_engine.py               # Core simulation engine
│   ├── analytics.py                     # Report generator
│   ├── dashboard.py                     # Interactive HTML dashboard
│   ├── data_fetcher.py                  # Dhan API data fetcher
│   └── charges.py                       # Brokerage & charges calculator
└── BACKTEST_REPORT.md                   # This file
```

---

## Conclusion

The Nifty Short Straddle strategy with **no re-entry** is the optimal configuration. It delivers:
- **Rs 10.38L net profit** on Rs 2.5L capital over 5 years (415% ROI)
- **Sharpe 3.85** — excellent risk-adjusted returns
- **Calmar 13.88** — strong return relative to max drawdown
- **61.6% win rate** with 1.92 profit factor

No further parameter optimization is needed. The strategy is ready for live deployment with 1 lot.
