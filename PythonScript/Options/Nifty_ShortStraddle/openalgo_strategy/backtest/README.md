# Nifty Short Straddle — Backtest Engine

Production-sync backtest for `nifty_short_straddle.py`. Replays every exit module tick-by-tick on 1-min candle data with full brokerage and tax modelling.

---

## Folder Structure

```
backtest/
├── README.md                    # This file — plan & documentation
├── config/
│   └── config.toml              # Strategy parameters (mirrors production)
├── data/
│   ├── nifty_spot_1min.parquet  # Nifty 50 spot 1-min candles (5 years)
│   ├── nifty_options_ce.parquet # ATM CE 1-min candles per expiry
│   ├── nifty_options_pe.parquet # ATM PE 1-min candles per expiry
│   └── india_vix_daily.csv      # India VIX daily OHLC
├── scripts/
│   ├── data_fetcher.py          # Fetch all data from Dhan API
│   ├── backtest_engine.py       # Core simulation (mirrors production monitor)
│   ├── charges.py               # Brokerage + statutory charges calculator
│   ├── analytics.py             # VectorBT post-trade analytics & charts
│   └── run_backtest.py          # Main entry — orchestrates fetch → simulate → report
├── results/
│   └── YYYY-MM-DD_HH-MM-SS/    # Each run gets a timestamped folder
│       ├── report.md            # Human-readable summary
│       ├── summary.json         # Machine-readable metrics
│       ├── trades.csv           # All trades with P&L and charges
│       ├── config_snapshot.toml # Exact config used for this run
│       └── charts/
│           ├── equity_curve.png
│           ├── drawdown.png
│           ├── monthly_heatmap.png
│           ├── dte_breakdown.png
│           ├── exit_reasons.png
│           └── yearly_summary.png
```

---

## Data Requirements

### 1. Nifty 50 Spot — 1-min Candles (5 Years)

- **Source:** Dhan Historical Data API (`/v2/charts/historical`)
- **Security ID:** 13 (NIFTY 50 Index)
- **Period:** 2021-04-01 to 2026-03-28
- **Use:** ATM strike calculation at 09:17 (`round(spot / 50) * 50`)
- **API limit:** Max 30 days per call → ~61 calls per year

### 2. Nifty Weekly ATM Options — 1-min Candles (5 Years)

- **Source:** Dhan Rolling Option API (`/v2/charts/rollingoption`)
- **Security ID:** 13 (NIFTY 50)
- **Expiry Code:** 1 (weekly)
- **Instrument:** OPTIDX
- **Strike offset:** ATM (calculated from spot at 09:17)
- **Data needed:** Both CE and PE candles for each trading day
- **Use:** All exit logic — SL monitoring, decay tracking, P&L calculation

### 3. India VIX Daily

- **Source:** Dhan Historical API or NSE CSV download
- **Security ID:** 26 (INDIA VIX on Dhan)
- **Use:** VIX spike exit (15% intraday rise from entry VIX, abs floor 18)
- **Fallback:** If intraday VIX not available, use daily open as proxy

### 4. NSE Trading Holidays

- **Source:** Hardcoded list + NSE website
- **Use:** DTE calculation (count trading days to expiry, skip weekends + holidays)

### 5. Weekly Expiry Calendar

- **Source:** Derived from options data / hardcoded
- **History:** Weekly expiries shifted from Thursday → Tuesday (mid-2024)
- **Use:** Map each trading day to its nearest weekly expiry for DTE calc

---

## Production Logic — Module-by-Module Sync

The backtest engine replicates every exit check in the exact same priority order as `Monitor._tick_inner()`:

### Priority 1: Per-Leg SL Check (line 1077-1121)
```
For each active leg:
  ltp = current 1-min candle high (worst case for sold option)
  sl = entry_price * (1 + LEG_SL_PERCENT/100)  → 130% of entry
  If breakeven_active: sl = min(fixed_sl, breakeven_sl) with grace period
  If ltp >= sl: close leg
  Net P&L guard: defer SL up to 15min if net position is profitable
```

### Priority 2: Combined Checks — Both Legs Active (line 1127-1178)

**2a. Combined Decay Exit:**
```
decay_pct = (1 - combined_current / combined_entry) * 100
target = DTE_MAP[dte]  → {0: 60%, 1: 65%, 2: 60%, 3: 50%, 4: 50%}
If decay_pct >= target: close all
```

**2b. Asymmetric Leg Booking:**
```
If winner_leg <= 40% of entry AND loser_leg >= 80% of entry:
  Close winner leg only
```

**2c. Combined Profit Trailing:**
```
If decay_pct >= 30%: activate trail, track peak
If peak - current >= 40%: close all (profit retracement)
```

### Priority 3: Winner Booking — Single Survivor (line 1180-1192)
```
If one leg closed, survivor decayed to <= 30% of entry:
  Close survivor (winner booking)
```

### Priority 4: VIX Spike Exit (line 1206-1219)
```
Every 5 min: check current VIX
If VIX rose >= 15% from entry AND current VIX >= 18:
  Close all
```

### Priority 5: Daily P&L Limits (line 1221-1231)
```
combined_pnl = closed_pnl + open_mtm
If combined_pnl >= +10,000/lot: close all (target)
If combined_pnl <= -6,000/lot: close all (loss limit)
```

### Priority 6: Time Exit (not in monitor — scheduler)
```
At 15:15 IST: close all remaining legs
```

### Re-Entry Logic (checked after full exit)
```
If REENTRY_ENABLED and position fully flat:
  Wait 45 min cooldown
  Max 2 re-entries per day
  Only if cumulative daily loss < 2,000/lot
  Only if not past daily target/loss limit
  Re-enter with fresh ATM strike
```

### Breakeven SL Activation (after partial exit)
```
When one leg closes at a loss:
  Survivor gets breakeven SL = entry + (closed_loss / qty) * 1.05
  Grace period: 5 minutes before SL becomes active
  Only if survivor is currently losing (not winning)
```

---

## Charges Model (Dhan)

All charges applied per order (entry and exit separately).

### Per-Order Charges

| Charge | Rate | Applied On |
|--------|------|-----------|
| **Brokerage** | Rs 20 per executed order | Every order |
| **STT** | 0.0625% of premium | Sell side only (entry for short, exit for buy-back) |
| **Exchange Txn** | 0.053% of premium | Both sides |
| **SEBI Charges** | 0.0001% of premium | Both sides |
| **GST** | 18% of (brokerage + exchange + SEBI) | Both sides |
| **Stamp Duty** | 0.003% of premium | Buy side only (exit for short straddle) |

### Per Round-Trip Cost (1 lot = 65 qty, both legs)

For a straddle with combined premium of Rs 400 (CE=200 + PE=200):
- Total premium value = 400 * 65 = Rs 26,000
- 4 orders (2 entry + 2 exit) = Rs 80 brokerage
- STT (sell side): 0.0625% * 26,000 = Rs 16.25
- Exchange: 0.053% * 26,000 * 2 = Rs 27.56
- SEBI: 0.0001% * 26,000 * 2 = Rs 0.52
- GST: 18% * (80 + 27.56 + 0.52) = Rs 19.45
- Stamp: 0.003% * 26,000 = Rs 0.78
- **Total per trade: ~Rs 144**

With re-entries (up to 2 extra trades/day), worst case: **~Rs 432/day**.

---

## Candle Price Usage for SL Accuracy

Production monitors LTP every 5 seconds. With 1-min candles, we use:

| Check | Candle Field | Rationale |
|-------|-------------|-----------|
| SL hit (sold option rising) | **High** | Worst-case — if high breached SL, it was hit intraday |
| Decay calculation | **Close** | End-of-minute fair value for decay % |
| P&L mark-to-market | **Close** | Standard MTM convention |
| Entry price | **Open of 09:17 candle** | Market order at 09:17 fills near open |
| Exit price (SL/target) | **Close of trigger candle** | Conservative — actual fill between trigger and close |

---

## Execution Plan

### Step 1: Data Fetch (`scripts/data_fetcher.py`)
1. Fetch Nifty spot 1-min candles (5 years) from Dhan
2. Fetch Nifty ATM CE+PE 1-min candles via rolling option API
3. Fetch India VIX daily data
4. Save all to `data/` as Parquet files
5. Validate: check for gaps, count trading days, verify expiry alignment

### Step 2: Backtest Engine (`scripts/backtest_engine.py`)
1. Load data and config
2. For each trading day:
   - Check filters (DTE, skip months, holidays)
   - At 09:17: calculate ATM, get CE+PE entry prices
   - Tick through 09:17-15:15 candles applying full exit hierarchy
   - Handle partial exits, breakeven activation, re-entry
   - Calculate charges on every order
3. Output: trades DataFrame with full metadata

### Step 3: Analytics (`scripts/analytics.py`)
1. Load trades from engine
2. Generate VectorBT-powered analytics:
   - Equity curve, drawdown chart
   - Monthly P&L heatmap (year x month)
   - DTE-wise breakdown
   - Exit reason distribution
   - Yearly summary table
   - Win rate, profit factor, Sharpe, max DD
3. Save all to timestamped results folder

### Step 4: Run (`scripts/run_backtest.py`)
1. Create timestamped results folder
2. Snapshot config
3. Run engine → analytics → report
4. Print summary to console

---

## How to Run

```bash
# Activate virtual environment
source ~/Developer/ShareMarket_Automation/algo_trading/bin/activate

# Set Dhan credentials
export DHAN_CLIENT_ID="your_client_id"
export DHAN_ACCESS_TOKEN="your_token"

# Step 1: Fetch data (one-time, ~10 min)
python scripts/data_fetcher.py

# Step 2: Run backtest
python scripts/run_backtest.py

# Results saved to: results/YYYY-MM-DD_HH-MM-SS/
```

---

## Comparing Results Across Runs

Each run creates `results/YYYY-MM-DD_HH-MM-SS/` with:
- `config_snapshot.toml` — exact parameters used
- `summary.json` — key metrics for programmatic comparison
- `report.md` — human-readable analysis

To compare two runs, diff their `summary.json` or read both `report.md` files.
