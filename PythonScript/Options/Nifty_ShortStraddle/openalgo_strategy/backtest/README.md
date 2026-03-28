# Nifty Short Straddle — Backtest Engine

Production-sync backtest for `nifty_short_straddle.py`. Replays every exit module tick-by-tick on 1-min candle data with full brokerage/tax modelling and dynamic capital-based lot allocation.

---

## Results Summary (2026-03-28, Post-Optimization)

### Fixed Capital Mode (₹2.5L, no compounding)

| Metric | Value |
|--------|-------|
| Period | Apr 2021 - Mar 2026 (5 years) |
| Starting Capital | Rs 2,50,000 |
| Total Trades | 1,220 |
| Net P&L (after charges) | Rs 13,31,475 |
| ROI (5 year) | 533% |
| Annual ROI | 106% |
| Win Rate | 65.2% |
| Profit Factor | 2.33 |
| Sharpe Ratio | 4.91 |
| Calmar Ratio | 40.47 |
| Max Drawdown | Rs -32,897 (13% of capital) |
| Total Charges | Rs 1,58,074 (10.6% of gross) |

### Compounded Capital Mode (₹2.5L start, profits reinvested, max 50 lots)

| Metric | Value |
|--------|-------|
| Total Trades | 1,220 |
| Net P&L (after charges) | Rs 2,49,14,096 (Rs 2.49 Cr) |
| Win Rate | 66.1% |
| Profit Factor | 2.27 |
| Max Drawdown | Rs -9,84,350 |

### Optimisation Applied

| Change | Impact |
|--------|--------|
| Re-entry disabled (`max_per_day = 0`) | +9% P&L, -17% max DD, +3% win rate |
| DTE 0 wider SL (`sl_dte_map = {0: 40}`) | +28% P&L, -56% max DD |
| November enabled (`skip_months = []`) | More trading days, all months net positive |

See `BACKTEST_REPORT.md` for full optimization history with all test results and rejected approaches.

---

## SEBI Lot Size History

The engine uses SEBI-mandated NIFTY 50 lot sizes which changed over the backtest period:

| Period | Lot Size | Reason |
|--------|----------|--------|
| Apr 2021 - Nov 19, 2024 | 25 | Pre-SEBI revision |
| Nov 20, 2024 - Jan 5, 2026 | 75 | SEBI min contract value ₹15L (tripled from 25) |
| Jan 6, 2026 onwards | 65 | NSE periodic revision (Sep 2025 avg prices) |

Sources: NSE Circular FAOP70616, Zerodha, Groww, Angel One

---

## Folder Structure

```
backtest/
├── README.md                    # This file
├── BACKTEST_REPORT.md           # Full optimization report & recommendations
├── config/
│   ├── config.toml              # Final production-synced config
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
│   └── run_optimization.py      # Optimization test runner (multi-config)
└── results/
    └── 2026-03-28/
        ├── fixed/index.html     # Interactive dashboard (final config)
        ├── compounded/index.html # Interactive dashboard (compounded mode)
        └── optimization/        # All optimization test results
```

---

## Data Sources

| Data | Source | API | Period |
|------|--------|-----|--------|
| Nifty Spot 1-min | Dhan | `/v2/charts/intraday` (90-day chunks) | 5 years |
| ATM CE/PE 1-min | Dhan | `/v2/charts/rollingoption` (30-day chunks) | 5 years |
| India VIX 1-min | OpenAlgo | `/api/v1/history` (30-day chunks) | 5 years |

**Note:** Dhan VIX data was incorrect (returned Nifty spot values). VIX is sourced from OpenAlgo API instead.

---

## Capital & Lot Allocation

The engine supports three modes configured in `config.toml`:

### 1. Fixed Mode (`dynamic_lot_sizing = false`)
Uses static `lot_size` and `number_of_lots` from config for all trades.

### 2. Dynamic Mode (`dynamic_lot_sizing = true, compound_capital = false`)
- Lot size per SEBI history for the trade date
- Number of lots = `floor(capital / (spot × lot_size × 9% margin × 1.20 buffer))`
- Capital stays fixed at ₹2.5L throughout

### 3. Compounded Mode (`dynamic_lot_sizing = true, compound_capital = true`)
- Same as dynamic, but capital = starting capital + cumulative net P&L
- Profits (and losses) feed back into lot sizing
- `max_lots` caps the lot count for realism (default: 50)
- More aggressive growth but larger drawdowns in absolute terms

---

## Production Logic — 13 Exit Modules (Priority Order)

The backtest engine replicates every exit check in the exact same priority order as `Monitor._tick_inner()` in `nifty_short_straddle.py`:

### Priority 1: Per-Leg Stop Loss
- SL = entry_price × 1.30 (30% above entry), **1.40 on DTE 0** (expiry day)
- DTE 0 gets wider SL to survive gamma spikes — avoids ~49 false SL exits
- Uses candle **high** (worst case for sold option)
- Net P&L guard: defer SL up to 15min if net position is profitable
- Breakeven SL replaces fixed SL after partial exit

### Priority 2: Combined Checks (both legs active)

**2a. Combined Decay Exit:**
- Target decay by DTE: {0: 60%, 1: 65%, 2: 60%, 3: 50%, 4: 50%}
- Close all when combined premium decays to target

**2b. Asymmetric Leg Booking:**
- Winner leg ≤ 40% of entry AND loser leg ≥ 80% of entry → close winner only

**2c. Combined Profit Trailing:**
- Activate at 30% combined decay, track peak
- Exit on 40% retracement from peak

### Priority 3: Winner Booking (single survivor)
- If survivor decayed to ≤ 30% of entry → close it

### Priority 4: VIX Spike Exit
- Every 5 min: if VIX rose ≥ 15% from entry AND VIX ≥ 18 → close all

### Priority 5: Daily P&L Limits
- Profit target: +₹10,000/lot → close all
- Loss limit: -₹6,000/lot → close all

### Priority 6: Time Exit
- At 15:15 IST → close all remaining legs

### Re-Entry Logic (DISABLED)
- `max_per_day = 0` — re-entry disabled after backtest optimization
- Re-entry trades had 42% win rate and lost Rs 87,743 total over 5 years
- Disabling improved all metrics: +9% P&L, better Sharpe, lower drawdown

### Breakeven SL Activation
- After one leg exits at a loss → survivor gets breakeven SL
- BE SL = entry + (closed_loss / qty) × 1.05 (5% buffer)
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
- STT + Exchange + SEBI + GST + Stamp ≈ Rs 64
- **Total ≈ Rs 144 per trade**

---

## Candle Price Usage

| Check | Candle Field | Rationale |
|-------|-------------|-----------|
| SL hit (sold option rising) | **High** | Worst case intraday |
| Decay / P&L calculation | **Close** | End-of-minute fair value |
| Entry price | **Open** of 09:17 candle | Market order fills near open |
| Exit price | **Close** of trigger candle | Conservative fill estimate |
| Slippage | +1 pt entry, -1 pt exit | Configurable in config.toml |

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

# Step 2: Run backtest
python scripts/run_backtest.py

# Custom date range
python scripts/run_backtest.py --start 2023-01-01 --end 2024-12-31

# Results saved to: results/YYYY-MM-DD/
```

### Switching Between Modes

Edit `config/config.toml`:

```toml
# Fixed capital (no compounding)
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

The report includes:
- Gross P&L, charges, and net P&L at monthly and yearly level
- Capital allocation per year (lots, lot size, qty)
- Win/loss statistics and exit reason analysis
- 6 charts: equity curve, drawdown, monthly heatmap, DTE breakdown, exit reasons, yearly summary
