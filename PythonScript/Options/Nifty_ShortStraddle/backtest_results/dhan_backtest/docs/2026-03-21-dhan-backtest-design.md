# Nifty Short Straddle Backtest — Design Spec

**Date**: 2026-03-21
**Scope**: Core strategy backtest using Dhan expired options data + VectorBT
**Period**: 1 year (2025-03-21 to 2026-03-21)

---

## Overview

Backtest the production Nifty short straddle strategy (core features) using historical expired options data from Dhan's `/v2/charts/rollingoption` API. Uses custom pandas simulation engine for the event-driven strategy logic, with VectorBT used **only for post-hoc analytics** (equity curve, drawdown, ratios, heatmaps).

## Architecture

Two-phase pipeline with separated concerns:

```
Phase 1: dhan_data_fetcher.py → Parquet cache
Phase 2: nifty_straddle_bt.py → custom simulation → VectorBT analytics
```

### File Structure

All backtest files live in `backtest_results/` — isolated from production strategy code.

```
backtest_results/
└── dhan_backtest/
    ├── dhan_data_fetcher.py      # Fetch + cache expired options data from Dhan
    ├── nifty_straddle_bt.py      # Backtest engine (custom simulation + VectorBT analytics)
    ├── config_backtest.toml      # Backtest parameters (subset of production config)
    ├── nse_holidays.py           # NSE holiday calendar 2025-2026 for DTE computation
    ├── docs/
    │   └── 2026-03-21-dhan-backtest-design.md  # This spec
    ├── data/
    │   └── nifty_options_2025/   # Cached Parquet files
    └── output/
        ├── bt_trades.csv         # Per-day trade log
        ├── bt_summary.json       # Aggregate stats
        └── charts/               # Equity curve, drawdown, heatmap PNGs
```

## Known Limitations & Design Choices

1. **Rolling ATM approximation**: Dhan's API returns ATM data relative to current spot at each timestamp, not a fixed strike. After entry, if NIFTY moves significantly, the ATM data may shift to a different strike. For Iteration 1, we accept this approximation — ATM premiums for strikes 50 points apart are close enough for backtesting. Future iteration can use Dhan's `/v2/charts/historical` with exact security IDs.

2. **1-minute candle high for SL check**: Production checks LTP every 15 seconds. Backtest uses `candle_high` which is more conservative (catches intra-candle spikes). This is a deliberate choice to avoid look-through bias.

3. **SL fill at SL level**: When SL is hit, exit price = SL level. In reality, slippage may occur. A configurable `slippage_pct` parameter is included (default 0%) for sensitivity analysis.

4. **Tuesday holiday expiry shift**: NSE weekly expiry moves to Monday when Tuesday is a holiday. The engine uses an NSE holiday calendar to correctly compute expiry dates and DTE values.

5. **Lot size**: Hardcoded to 65 for the 2025-2026 period. Verified against current NIFTY lot size.

---

## Phase 1: Dhan Data Fetcher

### API Details

- **Endpoint**: `POST https://api.dhan.co/v2/charts/rollingoption`
- **Auth**: `access-token` header from `DHAN_ACCESS_TOKEN` env var
- **Rate limit**: 30-day max per request, need batching

### Request Parameters

```json
{
  "exchangeSegment": "NSE_FNO",
  "interval": "1",
  "instrument": "OPTIDX",
  "expiryCode": 0,
  "expiryFlag": "WEEK",
  "strike": "ATM",
  "drvOptionType": "CALL",
  "requiredData": ["open", "high", "low", "close", "iv", "volume", "oi", "spot"],
  "fromDate": "2025-03-21",
  "toDate": "2025-04-20"
}
```

### Batching Strategy

- Split 1-year range into 30-day windows (~13 batches)
- 2 calls per batch (CALL + PUT) = ~26 API calls total
- Sequential with 0.5s delay between calls to respect rate limits

### Output Schema (Parquet)

| Column | Type | Source |
|---|---|---|
| `timestamp` | datetime64[ns, Asia/Kolkata] | Dhan epoch |
| `spot` | float64 | Dhan spot field |
| `ce_open` | float64 | CALL open |
| `ce_high` | float64 | CALL high |
| `ce_low` | float64 | CALL low |
| `ce_close` | float64 | CALL close |
| `ce_iv` | float64 | CALL IV |
| `ce_oi` | int64 | CALL OI |
| `ce_volume` | int64 | CALL volume |
| `pe_open` | float64 | PUT open |
| `pe_high` | float64 | PUT high |
| `pe_low` | float64 | PUT low |
| `pe_close` | float64 | PUT close |
| `pe_iv` | float64 | PUT IV |
| `pe_oi` | int64 | PUT OI |
| `pe_volume` | int64 | PUT volume |

### Error Handling

- Retry failed calls 3x with exponential backoff (1s, 2s, 4s)
- Skip date ranges with no data (holidays), log warnings
- Resume capability: check existing Parquet before fetching
- Progress bar via tqdm

### Credentials

- `DHAN_ACCESS_TOKEN` from environment variable
- `DHAN_CLIENT_ID` from environment variable
- Never hardcoded in source

---

## Phase 2: VectorBT Backtest Engine

### Strategy: Core Nifty Short Straddle

**Scope (Iteration 1 — Core Only)**:
- ATM short straddle entry (SELL CE + SELL PE)
- Per-leg fixed SL with DTE overrides
- Dynamic time-of-day SL tightening
- Hard exit at 15:15 IST
- Daily profit target / loss limit

**Excluded from Iteration 1** (future extensions):
- Trailing SL, breakeven SL
- Partial square-off advanced mechanics
- Combined decay exits, winner-leg booking
- VIX/IVR/IVP filters, ORB filter
- Re-entry logic
- Spot-move exit, VIX spike monitor
- Net P&L Guard (SL deferral when combined P&L is positive)
- Asymmetric booking, recovery lock, combined profit trailing
- Transaction costs (existing `charges_impact.py` can be integrated later)

### Entry Logic (per trading day)

```
1. Determine if trading day: skip weekends, skip SKIP_MONTHS
2. Compute DTE (days to nearest Tuesday expiry): 0-4
3. Skip if DTE not in TRADE_DTE list
4. At DTE-aware entry time:
   - CE entry price = ce_close at entry candle
   - PE entry price = pe_close at entry candle
   - combined_premium = CE + PE
```

### DTE Entry Time Map

| DTE | Entry Time | Day |
|-----|-----------|-----|
| 0 | 09:30 | Tuesday (expiry) |
| 1 | 09:30 | Monday |
| 2 | 09:35 | Friday |
| 3 | 09:40 | Thursday |
| 4 | 09:45 | Wednesday |

### Per-Leg SL Logic (every 1-min candle)

```python
# For each active leg, every minute after entry:
base_sl_pct = DTE_SL_OVERRIDE.get(dte, LEG_SL_PERCENT)  # 20-30%

# Dynamic time-of-day tightening
if time >= 14:30: dynamic_pct = 7.0
elif time >= 13:30: dynamic_pct = 10.0
elif time >= 12:00: dynamic_pct = 15.0
else: dynamic_pct = base_sl_pct

effective_sl_pct = min(base_sl_pct, dynamic_pct)
sl_level = entry_price * (1 + effective_sl_pct / 100)

# Check SL hit using candle high (conservative vs production's 15s LTP polling)
# This catches intra-candle spikes that 15s polling might miss
if candle_high >= sl_level:
    exit_price = sl_level * (1 + slippage_pct / 100)  # Configurable slippage
    leg_closed = True
```

### Edge Cases Handled

- **Missing entry candle**: If entry-time candle doesn't exist (late open / circuit halt), skip that day
- **NSE holidays**: Use holiday calendar for correct DTE; Tuesday holiday shifts expiry to Monday
- **Daily profit/loss limits**: Scaled by `number_of_lots` to match production behavior

### SL Percentages by DTE

| DTE | Base SL% | Source |
|-----|---------|--------|
| 0 | 20.0% | Default (leg_sl_percent) |
| 1 | 20.0% | Default |
| 2 | 25.0% | dte_sl_override |
| 3 | 28.0% | dte_sl_override |
| 4 | 30.0% | dte_sl_override |

### Daily Limits

- **Profit target**: Rs.5000 per lot (close all legs when combined P&L reaches target)
- **Loss limit**: Rs.4000 per lot (close all legs when combined loss breaches limit)

### P&L Calculation

```python
# Short position: profit when price falls
leg_pnl = (entry_price - exit_price) * lot_size * num_lots
daily_pnl = ce_pnl + pe_pnl
```

### Hard Exit

All remaining open legs closed at 15:15 IST using `close` price of that candle.

### VectorBT Analytics

Custom simulation produces a daily trade log. VectorBT used for:

1. **Equity curve** — cumulative P&L over time
2. **Drawdown analysis** — max drawdown, drawdown duration
3. **Performance ratios** — Sharpe, Sortino, Calmar
4. **Monthly returns heatmap** — visual P&L by month
5. **Win/loss statistics** — win rate, avg win, avg loss, profit factor
6. **Per-DTE breakdown** — performance segmented by days-to-expiry
7. **SL hit analysis** — % of legs stopped out vs natural decay exit

### Config File (config_backtest.toml)

```toml
[instrument]
underlying = "NIFTY"
lot_size = 65
number_of_lots = 1

[timing]
entry_time = "09:30"
exit_time = "15:15"
use_dte_entry_map = true

[timing.dte_entry_time_map]
"0" = "09:30"
"1" = "09:30"
"2" = "09:35"
"3" = "09:40"
"4" = "09:45"

[risk]
leg_sl_percent = 20.0
daily_profit_target_per_lot = 5000
daily_loss_limit_per_lot = -4000

[risk.dte_sl_override]
"2" = 25.0
"3" = 28.0
"4" = 30.0

[risk.dynamic_sl]
enabled = true
schedule = [
    { time = "14:30", sl_pct = 7.0 },
    { time = "13:30", sl_pct = 10.0 },
    { time = "12:00", sl_pct = 15.0 },
]

[backtest]
from_date = "2025-03-21"
to_date = "2026-03-21"
skip_months = [11]
trade_dte = [0, 1, 2, 3, 4]
slippage_pct = 0.0   # Configurable slippage on SL fills (0% default)

[dhan]
# Credentials from env vars: DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
exchange_segment = "NSE_FNO"
instrument = "OPTIDX"
expiry_flag = "WEEK"
interval = 1
```

### Output Files

```
backtest_results/dhan_backtest/output/
├── bt_trades.csv        # Per-day: date, DTE, entry_CE, entry_PE, exit_CE, exit_PE,
│                        #          sl_hit_CE, sl_hit_PE, pnl_CE, pnl_PE, total_pnl
├── bt_summary.json      # Aggregate: total_pnl, win_rate, sharpe, max_dd, profit_factor
└── charts/
    ├── equity_curve.png
    ├── drawdown.png
    ├── monthly_heatmap.png
    └── dte_breakdown.png
```

---

## Future Extensions (Iteration 2+)

- Trailing SL with activation trigger and ratchet-only tightening
- Partial square-off: one leg SL hit, other continues with breakeven SL
- Combined decay exits with DTE overrides
- Winner-leg booking at 70% decay
- VIX/IVR/IVP filters (requires separate VIX data source)
- ORB filter (spot movement check at 09:17)
- Re-entry logic with cooldown
- Broker charges impact (STT, brokerage, exchange fees)
- Comparison with existing AlgoTest backtest results

---

## Dependencies

```
dhanhq           # Dhan Python SDK (or raw requests)
vectorbt         # Backtesting analytics
pandas           # Data manipulation
pyarrow          # Parquet I/O
tomli            # TOML config parsing
tqdm             # Progress bars
matplotlib       # Charts (via vectorbt)
```

## Virtual Environment

Use existing project venv: `~/Developer/ShareMarket_Automation/algo_trading/`
