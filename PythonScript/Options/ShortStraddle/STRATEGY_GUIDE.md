# Nifty Short Straddle — Strategy Guide

Single-file OpenAlgo Python Strategy for NIFTY weekly short straddle with comprehensive risk management, IV entry filter, hybrid exchange SL, and independent per-leg SL.

**Script:** `nifty_short_straddle.py` (v9.0)
**Deployment:** OpenAlgo Python Strategy (handles scheduling, holidays, log capture)
**Strategy Tag:** `Short Straddle` — all orders are tagged; won't interfere with other strategies

---

## How It Works

SELL ATM CE + SELL ATM PE at the same strike on the nearest weekly expiry. Profit comes from theta decay when NIFTY stays range-bound. Each leg has an independent stop-loss — if one leg gets stopped out, the other continues running.

---

## Backtest Results (5-Year: Apr 2021 — Apr 2026)

**Production Config (IV12 Filter + Hybrid Exchange SL, Fixed Capital Rs 2,50,000)**

| Metric | Value |
|--------|-------|
| Total Trades | 798 |
| Win Rate | 86.3% |
| Net P&L (after charges) | Rs 21,87,897 |
| Total Return | 875.2% |
| CAGR | 57.7% |
| Profit Factor | 10.56 |
| Sharpe Ratio | 13.67 |
| Calmar Ratio | 324.51 |
| Max Drawdown | Rs -6,742 |
| Avg Daily P&L | Rs 2,742 |
| Total Charges | Rs 1,24,796 (5.4% of gross) |
| Negative Months | 0 / 60 |

**Yearly Performance:**

| Year | Trades | Net P&L | Win Rate |
|------|--------|---------|----------|
| 2021 | 134 | Rs 4,36,464 | 86.6% |
| 2022 | 212 | Rs 6,53,222 | 87.7% |
| 2023 | 119 | Rs 3,62,188 | 89.9% |
| 2024 | 171 | Rs 4,25,783 | 84.8% |
| 2025 | 128 | Rs 2,62,084 | 85.2% |
| 2026 (Q1) | 34 | Rs 48,157 | 76.5% |

**Slippage Sensitivity (real-world expectations):**

| Slippage | Net P&L | Win Rate | Max DD | Profit Factor |
|----------|---------|----------|--------|---------------|
| 1 pt (backtest) | Rs 21,87,897 | 86.3% | Rs -6,742 | 10.56 |
| 3 pt (moderate) | Rs 10,42,519 | 71.4% | Rs -14,219 | 3.15 |
| 5 pt (stress) | Rs 6,30,825 | 61.3% | Rs -19,458 | 2.01 |

See `backtest/results/2026-04-04/fixed/` for full results, charts, and trade log.

---

## Configuration Parameters

All parameters are defined as constants at the top of the script. No external config file needed.

### Connection

| Parameter | Default | Description |
|-----------|---------|-------------|
| `OPENALGO_HOST` | env `OPENALGO_HOST` | OpenAlgo server URL |
| `OPENALGO_WS_URL` | env `OPENALGO_WS_URL` | WebSocket URL for live feed |
| `OPENALGO_API_KEY` | env `OPENALGO_APIKEY` | API key |
| `TELEGRAM_USER` | env `OPENALGO_USERNAME` | OpenAlgo username for Telegram |

### Instrument

| Parameter | Default | Description |
|-----------|---------|-------------|
| `UNDERLYING` | `NIFTY` | Index to trade |
| `LOT_SIZE` | `65` | NIFTY lot size (current SEBI lot size) |
| `NUMBER_OF_LOTS` | `1` | Lots per leg |
| `PRODUCT` | `MIS` | MIS (intraday) or NRML (carry forward) |
| `STRIKE_ROUNDING` | `50` | Strike interval for ATM calculation |

### Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENTRY_TIME` | `09:17` | Fixed entry time — captures opening IV premium |
| `EXIT_TIME` | `15:15` | Hard square-off before MIS auto-liquidation at 15:30 |
| `MONITOR_INTERVAL` | `5` | Seconds between monitor ticks |

### Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TRADE_DTE` | `[0,1,2,3,4]` | Allowed DTEs. 0=expiry day (Tue), 4=Wed |
| `SKIP_MONTHS` | `[]` | All months net positive in backtest |

---

## Risk Management Modules

### Entry Filters (checked before placing orders)

#### 1. VIX Entry Filter

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VIX_ENTRY_FILTER_ENABLED` | `true` | Skip entry if VIX outside safe range |
| `VIX_ENTRY_MIN` | `11.0` | Minimum VIX to trade |
| `VIX_ENTRY_MAX` | `25.0` | Maximum VIX to trade |

#### 2. ORB (Opening Range Breakout) Filter

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ORB_FILTER_ENABLED` | `true` | Skip if market already moved sharply from open |
| `ORB_THRESHOLD_PCT` | `0.5` | Max spot move % from 09:15 open |

#### 3. IV Entry Filter (Black-76)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `IV_ENTRY_FILTER_ENABLED` | `true` | Skip entry if ATM implied volatility too low |
| `IV_ENTRY_MIN` | `12.0` | Min avg(CE_IV, PE_IV) in % |

Fetches real-time IV via OpenAlgo `optiongreeks` API (Black-76 model). Skips low-IV days where premium is insufficient to absorb adverse moves. This filter removed 303 low-quality trades (27.5%) while improving:
- Profit Factor: 9.38 -> 10.73
- Max Drawdown: -Rs 9,516 -> -Rs 6,719 (29% better)
- Calmar: 282 -> 328

#### 4. Weekly Drawdown Guard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WEEKLY_DRAWDOWN_ENABLED` | `true` | Pause after sustained losses |
| `WEEKLY_LOSS_LIMIT` | `-20000` | Per-lot rolling 5-day loss threshold |

#### 5. Margin Guard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MARGIN_GUARD_ENABLED` | `true` | Pre-trade margin check |
| `MARGIN_BUFFER` | `1.20` | 20% headroom above required margin |
| `MARGIN_FAIL_OPEN` | `false` | Fail-closed: skip entry if margin API fails |

---

### Exit Monitors (checked every 5 seconds while in position)

Priority hierarchy — highest priority checks first:

#### Priority 0 (Pre-check): Hybrid Exchange SL-M Detection

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EXCHANGE_SL_ENABLED` | `true` | Enable exchange-level SL-M orders |
| `EXCHANGE_SL_PCT` | `45.0` | Trigger at 45% above entry per leg |

**Layer 2 catastrophic protection.** After entry, SL-M BUY orders are placed on the exchange at 45% above entry price for each leg. These orders live on the exchange independently — they fire even if the script crashes, API fails, or internet goes down.

**Lifecycle:**
1. **Place:** After fill capture, SL-M orders placed on exchange (3 retries, fail-open)
2. **Detect:** Each tick, `orderstatus()` checks if exchange SL triggered. If triggered, marks leg inactive before any exit logic runs (prevents double BUY)
3. **Cancel:** When script closes a leg (any exit reason), cancels that leg's exchange SL (3 retries). Also cancels on fully-flat cleanup
4. **Cost:** Zero — cancelled orders incur no charges. Only pays if exchange SL actually fills

**Protection layers (defence in depth):**
```
Layer 1: Script monitoring (every 5s) — 13 exit conditions below
Layer 2: Exchange SL-M at 45% per leg — lives on exchange, fires independently
Layer 3: Broker MIS auto-square at 15:15-15:30 — final backstop
```

#### Priority 0a: Max Trade Loss

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_TRADE_LOSS_ENABLED` | `true` | Absolute rupee cap per trade |
| `MAX_TRADE_LOSS` | `15000` | Per-lot max loss in Rs |

#### Priority 0b: Combined SL (both legs active)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COMBINED_SL_ENABLED` | `true` | Combined premium SL when both legs active |
| `COMBINED_SL_PCT` | `30.0` | Exit if combined premium rises 30% from entry |

When both legs are active, Combined SL governs (per-leg SL is skipped). Per-leg SL only applies for single survivor after partial exit.

#### Priority 1: Per-Leg SL (single survivor)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LEG_SL_PERCENT` | `30.0` | % of entry premium |
| `LEG_SL_DTE_MAP` | `{0: 40.0}` | Expiry day gets wider 40% SL (high gamma) |

Includes **Net P&L Guard**: defers SL up to 15 min if net position (closed P&L + open MTM) is still positive.

Includes **Breakeven SL**: after partial exit at a loss, tightens survivor SL to combined breakeven level (with 5% buffer and 5 min grace period).

#### Priority 2: Combined Checks (both legs active)

**2a. Combined Decay Exit**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COMBINED_DECAY_ENABLED` | `true` | Exit when combined premium decayed enough |
| `COMBINED_DECAY_DTE_MAP` | `{0:60, 1:65, 2:60, 3:50, 4:50}` | DTE-specific targets |

**2b. Asymmetric Leg Booking**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ASYMMETRIC_ENABLED` | `true` | Book deeply decayed winner when loser is intact |
| `ASYMMETRIC_WINNER_DECAY_PCT` | `40.0` | Winner at/below 40% of entry |
| `ASYMMETRIC_LOSER_INTACT_PCT` | `80.0` | Loser at/above 80% of entry |

**2c. Combined Profit Trailing**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COMBINED_TRAIL_ENABLED` | `true` | Trail combined decay, exit on retracement |
| `COMBINED_TRAIL_ACTIVATE_PCT` | `30.0` | Start trailing at 30% decay |
| `COMBINED_TRAIL_PCT` | `40.0` | Exit if decay retraces 40 points from peak |

#### Priority 3: Winner Booking (single survivor)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WINNER_BOOKING_ENABLED` | `true` | Book surviving leg when deeply decayed |
| `WINNER_BOOKING_DECAY_PCT` | `30.0` | Book when LTP <= 30% of entry |

#### Priority 5: VIX Spike Exit

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VIX_SPIKE_ENABLED` | `true` | Exit on mid-session VIX spike |
| `VIX_SPIKE_THRESHOLD` | `15.0` | % rise from entry VIX |
| `VIX_SPIKE_ABS_FLOOR` | `18.0` | Min absolute VIX to confirm spike |
| `VIX_SPIKE_INTERVAL_S` | `300` | Check every 5 minutes |

Dual condition: fires ONLY when relative spike >= 15% AND absolute VIX >= 18.

#### Priority 5b: Spot Move Exit

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SPOT_MOVE_EXIT_ENABLED` | `true` | Exit if underlying moved beyond premium collected |
| `SPOT_MOVE_MULTIPLIER` | `1.0` | threshold = combined_premium x multiplier |

#### Priority 6: Daily P&L Limits

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAILY_TARGET` | `10000` | Per-lot profit target |
| `DAILY_LOSS_LIMIT` | `-6000` | Per-lot loss limit |

---

### Re-Entry (DISABLED)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REENTRY_ENABLED` | `true` | Logic exists but disabled via max_per_day=0 |
| `REENTRY_MAX_PER_DAY` | `0` | 0 = no re-entries allowed |
| `REENTRY_COOLDOWN_MIN` | `45` | Cooldown between close and re-entry |
| `REENTRY_MAX_LOSS` | `2000` | Skip re-entry if previous loss exceeds this |

Backtest showed re-entry trades have negative expected value. Disabled improves all metrics.

---

## Strategy Isolation

All orders are tagged with `strategy="Short Straddle"`:
- `optionsmultiorder` (entry) — strategy-tagged
- `placesmartorder` (exit) — strategy-tagged
- `placeorder` (exchange SL-M) — strategy-tagged
- `cancelorder` (exchange SL cancel) — strategy-tagged
- `orderstatus` (all calls) — strategy-tagged
- `closeposition` (safety net) — strategy-tagged

**Your other option positions and strategies will NOT be affected.**

---

## API Rate Limits

| Scenario | API Calls per 5s Tick | Notes |
|----------|----------------------|-------|
| Monitoring (WS active) | ~2 (orderstatus x2) | Exchange SL status checks only |
| Monitoring (WS down) | ~6 (LTP x3, VIX, orderstatus x2) | REST fallback + SL checks |
| Entry burst | ~5 (multiorder, orderstatus, placeorder x2) | Spread over 2-3s |
| Exit (both legs) | ~10 (LTP, placeorder x2, cancelorder x2) | Sequential, under 10/s limit |

WebSocket handles all live pricing — monitoring loop only makes `orderstatus()` calls for exchange SL detection in normal operation. Well within the 10 req/s rate limit.

---

## Infrastructure

### WebSocket Feed
- Daemon thread with auto-reconnect (exponential backoff 1s -> 30s)
- Subscribes to option legs + NIFTY spot + India VIX
- LTP cache with 60s staleness threshold — falls back to REST API
- Handles auth, subscribe, ping/pong

### State Persistence
- Atomic writes: temp file + `os.replace()` — never corrupts on crash
- On startup, Reconciler compares saved state vs broker positions
- Cross-day stale state auto-cleaned
- Exchange SL order IDs (`exchange_sl_oid_ce/pe`) persisted — survive script restarts

| Saved State | Broker Position | Action |
|-------------|----------------|--------|
| None | Flat | Clean start |
| Saved | Confirmed | Restore + resume monitoring (exchange SL detection resumes from saved OIDs) |
| Saved | Flat | Externally closed, reset |
| Stale (prev day) | Any | Reset |

### Thread Safety
- `_monitor_lock` (RLock) protects all shared `state` dict writes
- Entry, exit, daily reset, and fill capture all acquire lock before state mutation

### Telegram Notifications
Sent for: entry, partial exit, full exit, margin issues, quote failures, VIX spikes, IV filter skips, exchange SL placed/triggered/cancel-failed, strategy start/stop.

Background daemon thread with 3-retry backoff. Non-blocking.

### Trade Logging
Every position close appends a JSON record to `trades.jsonl`:
- Entry/exit prices, P&L, duration, exit reason
- Market context (VIX, spot, DTE)
- SL events, re-entry flag, trade count
- `exchange_sl_triggered` flag — whether exchange SL-M fired for any leg

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENALGO_HOST` | OpenAlgo server URL |
| `OPENALGO_WS_URL` | WebSocket URL for live feed |
| `OPENALGO_APIKEY` | OpenAlgo API key |
| `OPENALGO_USERNAME` | OpenAlgo username (for Telegram) |

---

## Running

```bash
# Production
python nifty_short_straddle.py

# Manual utilities (uncomment in script)
# strategy.check_connection()
# strategy.manual_entry()
# strategy.manual_exit()
# strategy.show_state()
```

**Pre-deployment checklist:**
1. Run in OpenAlgo Analyzer Mode (sandbox) for 1-2 days
2. Monitor Telegram alerts for correct entry filter behavior
3. Check `trades.jsonl` after sandbox day to verify P&L logging
4. Confirm environment variables are set
