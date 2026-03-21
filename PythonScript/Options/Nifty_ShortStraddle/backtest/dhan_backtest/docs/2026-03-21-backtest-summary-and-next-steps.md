# Nifty Short Straddle Backtest — Complete Summary & Next Steps

**Date:** 2026-03-21
**Project:** `backtest_results/dhan_backtest/`
**Data:** Dhan expired options API, 1-min ATM weekly NIFTY options, 92,737 rows
**Period:** 2025-03-21 to 2026-03-21 (1 year, 224 trading days)

---

## Architecture

Two-phase pipeline:
1. **`dhan_data_fetcher.py`** — Fetches 1-min OHLC+IV+OI+spot from Dhan `/v2/charts/rollingoption` API → Parquet cache
2. **`nifty_straddle_bt.py`** — Custom pandas day-by-day simulation → analytics + charts

Custom simulation (not VectorBT) because VectorBT can't handle multi-leg options with independent per-leg SLs.

---

## Iteration Results

| Metric | Iter 1 (Core) | Iter 2 (BE Buffer Down) | Iter 3 (Production Match) |
|---|---|---|---|
| Total P&L | +₹8,208 | +₹185,754 | -₹23,107 |
| Win Rate | 47.8% | 96.0% | 59.4% |
| Avg Win | ₹1,990 | ₹987 | ₹936 |
| Avg Loss | -₹1,750 | -₹2,934 | -₹1,621 |
| Profit Factor | 1.04 | 8.04 | 0.84 |
| Max Drawdown | -₹28,696 | -₹4,793 | -₹31,648 |
| Sharpe | 0.24 | 9.18 | -0.84 |
| Calmar | 0.29 | 38.76 | 0.73 |

### Iteration 1 — Core Strategy
- Fixed SL with DTE overrides + dynamic time-of-day SL tightening
- Daily profit target / loss limit
- Hard exit at 15:15
- **Result:** Barely profitable. DTE 0 is the clear winner (+₹29K), DTE 1 & 2 loss-making.

### Iteration 2 — Advanced Risk Features
- Added: trailing SL, breakeven SL, combined decay exit, winner-leg booking
- Breakeven buffer applied as `(1 - buffer)` (tighter for shorts)
- **Result:** Breakeven SL was the game-changer (97% of improvement). 96% win rate.
- **Bugs found:** Same-candle trailing SL activation+hit, breakeven buffer direction, trailing_activated output field

### Iteration 3 — Full Production Parity
- Fixed breakeven buffer to match production: `(1 + buffer)` (looser)
- Added: FIX-XX net P&L guard, FIX-XXIV breakeven context awareness, FIX-XXV recovery lock, FIX-XXVII asymmetric booking, FIX-XXVIII combined profit trailing, spot-move exit
- **Result:** Net negative. Recovery lock (42%) and spot-move (26%) dominate exits.

---

## Critical Findings

### 1. Breakeven SL Buffer Direction (HIGHEST PRIORITY)

**The single most impactful parameter in the entire strategy.**

For a SHORT option, SL fires when price goes UP above SL level:
- `raw_be × (1 - buffer)` = LOWER level → fires SOONER → locks in small profit → +₹186K
- `raw_be × (1 + buffer)` = HIGHER level → fires LATER → overshoots breakeven → -₹23K

Production uses `(1 + buffer)`. The backtest proves `(1 - buffer)` dramatically outperforms.

**Example:**
```
CE sold at 200, hits SL at 240 → loss = -₹2,600
raw_be = PE_entry + loss/qty = 200 - 40 = 160

(1 + 10%) → be_price = 176 → PE exits at 176 → profit = +₹1,560 → net = -₹1,040 (LOSS)
(1 - 10%) → be_price = 144 → PE exits at 144 → profit = +₹3,640 → net = +₹1,040 (PROFIT)
```

### 2. Spot-Move Exit Too Aggressive

`spot_multiplier = 1.0` triggers on 26% of trades. NIFTY moves 100-250 points intraday frequently. This closes positions before theta decay can work, especially hurting DTE 0 (flipped from +₹29K to -₹5.7K).

### 3. Recovery Lock Cuts Profits Short

50% retracement threshold fires on 42% of trades. After one leg stops out at a loss, the survivor's recovery is trailed but the trail is too tight — locks in small profits instead of letting the winner run.

### 4. Unused Features

- **Asymmetric leg booking**: 0 triggers — fixed SL fires before the conditions are met
- **Combined profit trailing**: 0 triggers — combined decay exit fires first
- **Combined decay exit**: 1 trigger — spot-move and SLs fire before decay threshold is reached

---

## Per-DTE Pattern (Consistent Across Iterations)

| DTE | Day | Iter 1 | Iter 3 | Verdict |
|-----|-----|--------|--------|---------|
| 0 | Tuesday (expiry) | +₹29,469 | -₹5,722 | Best in core, hurt by spot-move exit |
| 1 | Monday | -₹15,432 | -₹15,800 | Consistently loss-making |
| 2 | Friday | -₹23,729 | -₹21,089 | Consistently loss-making |
| 3 | Thursday | +₹5,991 | +₹7,427 | Moderately profitable |
| 4 | Wednesday | +₹11,909 | +₹12,077 | Most consistent performer |

---

## File Inventory

```
backtest_results/dhan_backtest/
├── config_backtest.toml           # All parameters (matches production + new features)
├── dhan_data_fetcher.py           # Phase 1: Dhan API → Parquet
├── nifty_straddle_bt.py           # Phase 2: Simulation engine (Iteration 3)
├── nse_holidays.py                # NSE calendar, DTE, expiry utilities
├── data/
│   └── nifty_options_2025/
│       └── nifty_atm_weekly_1min.parquet  # 92,737 rows, 5.4 MB
├── output/
│   ├── bt_trades.csv              # 224 trades with full detail
│   ├── bt_summary.json            # Aggregate metrics
│   └── charts/
│       ├── equity_curve.png
│       ├── drawdown.png
│       ├── monthly_heatmap.png
│       ├── dte_breakdown.png
│       └── exit_reasons.png
└── docs/
    ├── 2026-03-21-dhan-backtest-design.md
    ├── 2026-03-21-implementation-plan.md
    ├── 2026-03-21-iteration1-results.md
    ├── 2026-03-21-iteration2-results.md
    ├── 2026-03-21-iteration3-results.md
    ├── 2026-03-21-backtest-vs-production-audit.md
    └── 2026-03-21-backtest-summary-and-next-steps.md  # THIS FILE
```

---

## Next Action Plan (Priority Order)

### Phase 1: Parameter Optimization (Next Session)

#### 1A. Fix Breakeven Buffer Direction in Production
- **Action:** Change production `config.toml` breakeven buffer from `(1 + buffer)` to `(1 - buffer)` in `src/monitor.py`
- **Validation:** Paper trade for 1 week, compare partial-exit P&L
- **Expected impact:** Single biggest P&L improvement

#### 1B. Tune Spot-Move Multiplier
- **Action:** Backtest with `spot_multiplier` values: 1.2, 1.5, 2.0, and disabled
- **Expected:** Higher multiplier → fewer premature exits → better DTE 0 performance
- **Quick test:** Run backtest grid with different multiplier values

#### 1C. Tune Recovery Lock Trail
- **Action:** Backtest with `trail_pct` values: 60%, 70%, 80%
- **Action:** Test `min_recovery_rs_per_lot`: 750, 1000, 1500
- **Expected:** Looser trail → lets winners run further → better average win

#### 1D. DTE-Specific Configuration
- **Action:** Consider disabling spot-move exit for DTE 0 (where theta is strongest)
- **Action:** Consider disabling DTE 1 & 2 from trading entirely (consistently loss-making)
- **Backtest:** Compare P&L with `trade_dte = [0, 3, 4]` vs `[0, 1, 2, 3, 4]`

### Phase 2: Feature Validation

#### 2A. Verify Unused Features
- **Asymmetric booking:** Lower `winner_decay_pct` from 40% to 50-60% to make it trigger
- **Combined profit trailing:** Lower `activate_pct` from 30% to 20%
- If still zero triggers, consider removing from production (dead code)

#### 2B. Add Slippage + Transaction Costs
- **Action:** Create `charges_impact.py` with realistic costs:
  - Brokerage: ₹20/order (Dhan)
  - STT: 0.0625% on sell side
  - Exchange charges: ~0.053%
  - GST: 18% on brokerage + exchange
- **Run:** Backtest best config with realistic costs

### Phase 3: Production Integration

#### 3A. Apply Optimized Parameters to Production
- Update `config.toml` with backtest-validated parameters
- Focus on: breakeven buffer direction, spot_multiplier, recovery trail_pct

#### 3B. Forward Test
- Paper trade for 2-4 weeks with optimized parameters
- Compare daily P&L with backtest predictions
- Validate that backtest results are achievable in live conditions

---

## Quick Start for Next Session

```bash
cd ~/Developer/ShareMarket_Automation/Github/Strategy_AlgoTrading/PythonScript/Options/Nifty_ShortStraddle/backtest_results/dhan_backtest
source ~/Developer/ShareMarket_Automation/algo_trading/bin/activate

# Run current backtest
python nifty_straddle_bt.py

# Quick parameter test (modify config_backtest.toml first)
# Or use Python directly:
python -c "
from nifty_straddle_bt import load_config, run_backtest
config = load_config()
config['risk']['spot_move_exit']['spot_multiplier'] = 1.5  # test different value
trades = run_backtest(config)
print(f'P&L: {trades[\"total_pnl\"].sum():,.0f}, Win: {(trades[\"total_pnl\"]>0).mean()*100:.1f}%')
"
```
