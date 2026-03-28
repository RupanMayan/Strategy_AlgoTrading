# Nifty Short Straddle — Backtest Engine

Production-sync backtest for `nifty_short_straddle.py`. Replays every exit module tick-by-tick on 1-min candle data with full brokerage/tax modelling and dynamic capital-based lot allocation.

---

## Results Summary (2026-03-29, Production Config with 7 Risk Fixes)

### Fixed Capital Mode (Rs 2.5L, no compounding) — RECOMMENDED

| Metric | Value |
|--------|-------|
| Period | Apr 2021 - Mar 2026 (5 years) |
| Starting Capital | Rs 2,50,000 |
| Final Capital | Rs 29,34,686 |
| Total Trades | 1,101 |
| Net P&L (after charges) | Rs 26,84,686 |
| ROI (5 year) | 1,074% |
| Annual ROI | ~215% |
| Win Rate | 85.2% |
| Profit Factor | 9.38 |
| Sharpe Ratio | 12.84 |
| Calmar Ratio | 282.13 |
| Max Drawdown | Rs -9,516 (3.8% of capital) |
| Total Charges | Rs 1,45,833 (5.2% of gross) |
| Avg Daily P&L | Rs 2,438 |
| Max Consecutive Loss Days | 3 |

### Compounded Capital Mode (Rs 2.5L start, profits reinvested, max 50 lots)

| Metric | Value |
|--------|-------|
| Total Trades | 1,042 |
| Net P&L (after charges) | Rs 4,24,37,949 (Rs 4.24 Cr) |
| ROI (5 year) | 16,975% |
| Win Rate | 85.6% |
| Profit Factor | 7.24 |
| Sharpe Ratio | 8.30 |
| Calmar Ratio | 106.92 |
| Max Drawdown | Rs -3,96,903 (158.8% of starting capital) |
| Total Charges | Rs 7,90,739 |
| Avg Lots/Trade | 44.49 |
| Largest Single Loss | Rs -3,10,298 |

**Why Fixed is Recommended:** Compounding hits max 50 lots by mid-2022 and stays capped. At 50 lots x 65 = 3,250 qty, liquidity in weekly NIFTY options becomes a real concern. Risk-adjusted returns (Sharpe 12.84 vs 8.30, Calmar 282x vs 107x) strongly favor fixed capital. A single bad compounding trade can lose Rs 3.1L vs Rs 6.7K in fixed mode.

### Year-Wise Summary (Fixed Capital)

| Year | Trades | Net P&L | Win Rate | Avg Lots | Lot Size |
|------|--------|---------|----------|----------|----------|
| 2021 | 180 | Rs 5,04,418 | 86.7% | 5.2 | 25 |
| 2022 | 229 | Rs 6,77,922 | 87.3% | 5.0 | 25 |
| 2023 | 217 | Rs 4,97,358 | 90.3% | 4.4 | 25 |
| 2024 | 234 | Rs 5,72,157 | 82.9% | 3.2 | 25-75 |
| 2025 | 198 | Rs 3,85,766 | 81.3% | 1.0 | 75 |
| 2026 | 43 | Rs 47,066 | 72.1% | 1.0 | 65 |

### Risk Management — 7 Institutional Fixes (All Enabled)

| Fix | Description | Impact |
|-----|-------------|--------|
| 1. Max Trade Loss | Rs 15,000/lot absolute cap per trade | Catastrophic loss prevention |
| 2. Margin Fail-Closed | Skip entry if margin API fails (was fail-open) | Prevents unintended exposure |
| 3. VIX Entry Filter | Skip entry if VIX < 11 or VIX > 25 | Avoids thin premiums and gamma risk |
| 4. Spot-Move Exit | Close if spot moves >= combined premium collected | Limits directional exposure |
| 5. Weekly Drawdown Guard | Skip entry if rolling 5-day P&L < Rs -20,000/lot | Prevents drawdown spirals |
| 6. ORB Filter | Skip entry if spot moved > 0.5% from 09:15 open | Avoids gap-up/gap-down entries |
| 7. Combined SL | 30% combined premium SL (replaces per-leg SL when both legs active) | Single biggest contributor — reduced per-leg SL hits from 87 to 22 |

### Optimization History (Applied)

| Change | Impact |
|--------|--------|
| Re-entry disabled (`max_per_day = 0`) | +9% P&L, -17% max DD, +3% win rate |
| DTE 0 wider SL (`sl_dte_map = {0: 40}`) | +28% P&L, -56% max DD |
| November enabled (`skip_months = []`) | More trading days, all months net positive |
| 7 risk fixes (combined) | +102% P&L, -71% max DD, +20pp win rate |

---

## SEBI Lot Size History

The engine uses SEBI-mandated NIFTY 50 lot sizes which changed over the backtest period:

| Period | Lot Size | Reason |
|--------|----------|--------|
| Apr 2021 - Nov 19, 2024 | 25 | Pre-SEBI revision |
| Nov 20, 2024 - Jan 5, 2026 | 75 | SEBI min contract value Rs 15L (tripled from 25) |
| Jan 6, 2026 onwards | 65 | NSE periodic revision (Sep 2025 avg prices) |

Sources: NSE Circular FAOP70616, Zerodha, Groww, Angel One

---

## Folder Structure

```
backtest/
├── README.md                    # This file
├── config/
│   ├── config.toml              # Baseline config (pre-risk fixes)
│   ├── config_production.toml   # Current production config (7 risk fixes)
│   ├── config_production_compound.toml  # Production + compounding
│   ├── config_enhanced_risk.toml # Enhanced risk config (used in comparison)
│   ├── opt_no_reentry.toml      # Test: no re-entry (APPLIED)
│   ├── opt_dte0_sl40.toml       # Test: DTE 0 wider SL (APPLIED)
│   ├── opt_sl25.toml            # Test: SL 25% (REJECTED)
│   ├── opt_no_dte1.toml         # Test: removed DTE 1 (REJECTED)
│   ├── opt_loss5000.toml        # Test: daily loss -5000 (REJECTED)
│   ├── opt_reentry_profit_only.toml  # Test: re-entry after profit only (REJECTED)
│   ├── opt_min_premium120.toml  # Test: min premium filter (REJECTED)
│   ├── opt_combined_sl30.toml   # Test: combined SL (REJECTED — tail risk)
│   └── opt_combined_best.toml   # Test: all 3 combined (REJECTED)
├── data/                        # .gitignored — regenerate with data_fetcher.py
│   ├── nifty_spot_1min.parquet  # 527K candles (2021-04 to 2026-03)
│   ├── nifty_atm_ce_1min.parquet # 462K candles (rolling ATM CE)
│   ├── nifty_atm_pe_1min.parquet # 462K candles (rolling ATM PE)
│   └── india_vix_1min.parquet   # 496K candles (OpenAlgo API)
├── scripts/
│   ├── data_fetcher.py          # Fetch data from Dhan API
│   ├── backtest_engine.py       # Core simulation (mirrors production monitor)
│   ├── charges.py               # Brokerage + statutory charges calculator
│   ├── analytics.py             # Post-trade analytics & charts
│   ├── dashboard.py             # Interactive HTML dashboard generator
│   ├── run_backtest.py          # Main entry — orchestrates everything
│   ├── run_comparison.py        # Baseline vs enhanced risk comparison
│   ├── run_fixed_vs_compound.py # Fixed vs compounding capital comparison
│   └── run_optimization.py      # Optimization test runner (multi-config)
└── results/
    ├── 2026-03-28/
    │   ├── fixed/index.html     # Dashboard (pre-risk-fixes config)
    │   ├── compounded/index.html
    │   ├── comparison/          # Baseline vs enhanced comparison
    │   └── optimization/        # All optimization test results
    └── 2026-03-29/
        ├── production/index.html  # Dashboard (production config, 7 risk fixes)
        ├── fixed/index.html       # Dashboard (fixed capital, latest)
        └── fixed_vs_compound/     # Fixed vs compounding comparison
```

---

## Data Sources

| Data | Source | API | Period |
|------|--------|-----|--------|
| Nifty Spot 1-min | Dhan | `/v2/charts/intraday` (90-day chunks) | 5 years |
| ATM CE/PE 1-min | Dhan | `/v2/charts/rollingoption` (30-day chunks) | 5 years |
| India VIX 1-min | OpenAlgo | `/api/v1/history` (30-day chunks) | 5 years |

**Note:** Dhan VIX data was incorrect (returned Nifty spot values). VIX is sourced from OpenAlgo API instead. VIX has a data gap for Apr-Jul 2021 (86 days) — VIX entry filter can't fire for those days, which is conservative (allows more trades).

---

## Capital & Lot Allocation

The engine supports three modes configured in `config.toml`:

### 1. Fixed Mode (`dynamic_lot_sizing = false`)
Uses static `lot_size` and `number_of_lots` from config for all trades.

### 2. Dynamic Mode (`dynamic_lot_sizing = true, compound_capital = false`)
- Lot size per SEBI history for the trade date
- Number of lots = `floor(capital / (spot x lot_size x 9% margin x 1.20 buffer))`
- Capital stays fixed at Rs 2.5L throughout

### 3. Compounded Mode (`dynamic_lot_sizing = true, compound_capital = true`)
- Same as dynamic, but capital = starting capital + cumulative net P&L
- Profits (and losses) feed back into lot sizing
- `max_lots` caps the lot count for realism (default: 50)
- More aggressive growth but larger drawdowns in absolute terms

---

## Production Logic — Exit Priority Chain

The backtest engine replicates every exit check in the exact same priority order as `Monitor._tick_inner()` in `nifty_short_straddle.py`:

### Priority 0: Max Trade Loss (absolute rupee cap)
- Close all if combined MTM breaches Rs 15,000/lot
- Highest priority — overrides all other exits

### Priority 0b: Combined SL (both legs active)
- Exit if combined premium rises 30% from entry
- Replaces per-leg SL when both legs are active
- Single biggest contributor to improved results

### Priority 1: Per-Leg Stop Loss (single survivor only)
- SL = entry_price x 1.30 (30% above entry), **1.40 on DTE 0** (expiry day)
- Only fires when one leg has already been closed
- Uses candle **high** (worst case for sold option)
- Net P&L guard: defer SL up to 15min if net position is profitable
- Breakeven SL replaces fixed SL after partial exit

### Priority 2: Combined Checks (both legs active)

**2a. Combined Decay Exit:**
- Target decay by DTE: {0: 60%, 1: 65%, 2: 60%, 3: 50%, 4: 50%}
- Close all when combined premium decays to target

**2b. Asymmetric Leg Booking:**
- Winner leg <= 40% of entry AND loser leg >= 80% of entry -> close winner only

**2c. Combined Profit Trailing:**
- Activate at 30% combined decay, track peak
- Exit on 40% retracement from peak

### Priority 3: Winner Booking (single survivor)
- If survivor decayed to <= 30% of entry -> close it

### Priority 4: P&L Calculation
- Combined = closed_pnl + open_mtm

### Priority 5: VIX Spike Exit
- Every 5 min: if VIX rose >= 15% from entry AND VIX >= 18 -> close all

### Priority 5b: Spot-Move Exit
- Close if |current_spot - entry_spot| >= combined_premium x multiplier

### Priority 6: Daily P&L Limits
- Profit target: +Rs 10,000/lot -> close all
- Loss limit: -Rs 6,000/lot -> close all

### Priority 7: Time Exit
- At 15:15 IST -> close all remaining legs

### Entry Filters (checked before entry)

| Order | Filter | Condition |
|-------|--------|-----------|
| 1 | Skip Months | Month in skip list |
| 2 | DTE Filter | DTE not in [0,1,2,3,4] |
| 3 | VIX Entry Filter | VIX < 11 or VIX > 25 |
| 4 | ORB Filter | Spot moved > 0.5% from 09:15 open |
| 5 | Weekly Drawdown Guard | Rolling 5-day P&L < Rs -20,000/lot |
| 6 | Re-entry Check | Cooldown, max per day, max loss |
| 7 | Margin Guard | Insufficient margin (fail-closed) |

### Breakeven SL Activation
- After one leg exits at a loss -> survivor gets breakeven SL
- BE SL = entry + (closed_loss / qty) x 1.05 (5% buffer)
- 5 min grace period before activation
- Only if survivor is currently losing

---

## Charges Model (Dhan)

| Charge | Rate | Applied On |
|--------|------|-----------|
| Brokerage | Rs 20 per order | Every order (4 per straddle round-trip) |
| STT | 0.0625% | Sell side only |
| Exchange Txn | 0.053% | Both sides |
| SEBI Fee | 0.0001% | Both sides |
| GST | 18% of (brokerage + exchange + SEBI) | Both sides |
| Stamp Duty | 0.003% | Buy side only |

**Example:** Straddle with combined premium Rs 400, qty 65:
- Premium value = Rs 26,000
- Brokerage = Rs 80 (4 orders)
- STT + Exchange + SEBI + GST + Stamp ~ Rs 64
- **Total ~ Rs 144 per trade**

---

## Candle Price Usage

| Check | Candle Field | Rationale |
|-------|-------------|-----------|
| SL hit (sold option rising) | **High** | Worst case intraday |
| Combined SL | **High** + **High** | Conservative (both legs worst case) |
| Decay / P&L calculation | **Close** | End-of-minute fair value |
| Entry price | **Open** of 09:17 candle | Market order fills near open |
| Exit price | **Close** of trigger candle | Conservative fill estimate |
| SL fill price | **max(Close, SL level)** | Simulates market order fill at/above SL |
| Slippage | -1 pt entry (sell lower), +1 pt exit (buy higher) | Adverse for short positions |

---

## How to Run

```bash
# Activate virtual environment
source ~/Developer/ShareMarket_Automation/algo_trading/bin/activate

# Set Dhan credentials (for data fetch only)
export DHAN_CLIENT_ID="your_client_id"
export DHAN_ACCESS_TOKEN="your_token"

# Step 1: Fetch data (one-time, ~10 min)
python scripts/data_fetcher.py

# Step 2: Run backtest (production config)
python scripts/run_backtest.py --config config/config_production.toml

# Step 3: Compare fixed vs compounding
python scripts/run_fixed_vs_compound.py

# Step 4: Compare baseline vs enhanced risk
python scripts/run_comparison.py

# Custom date range
python scripts/run_backtest.py --start 2023-01-01 --end 2024-12-31

# Results saved to: results/YYYY-MM-DD/
```

### Switching Between Modes

Edit `config/config_production.toml`:

```toml
# Fixed capital (no compounding) — RECOMMENDED
compound_capital = false

# Compounded capital (profits reinvested)
compound_capital = true
max_lots = 50  # adjust cap as needed
```

---

## Comparing Results

Each run creates `results/YYYY-MM-DD/` with:
- `config_snapshot.toml` — exact parameters used
- `summary.json` — key metrics for programmatic comparison
- `trades.csv` — every trade with P&L, charges, lot size, capital used
- `report.md` — full report with year-wise and month-wise breakdowns
- `index.html` — interactive dashboard with equity curve, drawdown, monthly heatmap, DTE breakdown, exit reasons

---

## Backtest vs Production Sync Status

Last verified: 2026-03-29

- Config values: **100% match** (all 40+ parameters verified)
- Exit logic priority chain: **100% match** (all 13 exit modules in same order)
- Entry filter chain: **100% match** (all 7 filters in same order)
- Known intentional differences:
  - Backtest uses candle high for SL checks (conservative); production uses live LTP
  - Backtest uses max(close, SL) for SL fill price; production fills at market
  - Margin guard uses internal calculation in backtest vs API call in production
