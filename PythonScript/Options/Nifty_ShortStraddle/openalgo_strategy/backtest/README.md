# Nifty Short Straddle — Backtest Engine

Production-sync backtest for `nifty_short_straddle.py`. Replays every exit module tick-by-tick on 1-min candle data with full brokerage/tax modelling and dynamic capital-based lot allocation.

---

## Results Summary (2026-03-28)

### Compounded Mode (₹2.5L start, profits reinvested, max 50 lots)

| Metric | Value |
|--------|-------|
| Period | Apr 2021 - Mar 2026 (5 years) |
| Starting Capital | Rs 2,50,000 |
| Final Capital | Rs 4.77 Cr |
| Net P&L (after charges) | Rs 4.74 Cr |
| Total Return | 18,963% |
| CAGR | 185.8% |
| Total Trades | 1,282 |
| Win Rate | 75.7% |
| Profit Factor | 4.69 |
| Sharpe Ratio | 7.30 |
| Max Drawdown | Rs -4.83L |
| Total Charges | Rs 9.71L (2.0% of gross) |

### Fixed Capital Mode (₹2.5L, no compounding)

| Metric | Value |
|--------|-------|
| Net P&L (after charges) | Rs 25.90L |
| Total Return | 1,036% |
| CAGR | 62.6% |
| Win Rate | 75.2% |
| Profit Factor | 5.12 |
| Max Drawdown | Rs -28,980 |
| Total Charges | Rs 1.70L (6.2% of gross) |

### Capital Growth (Compounded)

| Year | Start Capital | End Capital | Avg Lots | Lot Size | Net P&L |
|------|---------------|-------------|----------|----------|---------|
| 2021 | Rs 2.5L | Rs 15.1L | 14.8 | 25 | Rs 12.8L |
| 2022 | Rs 15.3L | Rs 95.2L | 48.5 | 25 | Rs 80.1L |
| 2023 | Rs 95.4L | Rs 1.42 Cr | 50 (capped) | 25 | Rs 46.9L |
| 2024 | Rs 1.42 Cr | Rs 2.28 Cr | 50 (capped) | 25/75 | Rs 84.5L |
| 2025 | Rs 2.27 Cr | Rs 4.31 Cr | 50 (capped) | 75 | Rs 2.05 Cr |
| 2026 | Rs 4.32 Cr | Rs 4.75 Cr | 50 (capped) | 65 | Rs 44.6L |

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
├── config/
│   └── config.toml              # Strategy parameters (mirrors production)
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
│   └── run_backtest.py          # Main entry — orchestrates everything
└── results/
    └── YYYY-MM-DD/              # Each run gets a date folder
        ├── report.md            # Full report with monthly/yearly breakdown
        ├── summary.json         # Machine-readable metrics
        ├── trades.csv           # All trades with P&L, charges, lots, capital
        ├── config_snapshot.toml # Exact config used for this run
        └── charts/
            ├── equity_curve.png
            ├── drawdown.png
            ├── monthly_heatmap.png
            ├── dte_breakdown.png
            ├── exit_reasons.png
            └── yearly_summary.png
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
- SL = entry_price × 1.30 (30% above entry)
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

### Re-Entry Logic
- 45 min cooldown after exit
- Max 2 re-entries per day
- Only if cumulative daily loss < ₹2,000/lot
- Blocked after daily target or loss limit hit

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
