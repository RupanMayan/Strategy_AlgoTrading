# Nifty Short Straddle — Strategy Guide

Single-file OpenAlgo Python Strategy for NIFTY weekly short straddle with independent per-leg SL, intraday MIS.

**Script:** `nifty_short_straddle.py` (~750 lines)
**Deployment:** OpenAlgo Python Strategy (handles scheduling, holidays, log capture)

---

## How It Works

SELL ATM CE + SELL ATM PE at the same strike on the nearest weekly expiry. Profit comes from theta decay when NIFTY stays range-bound. Each leg has an independent stop-loss — if one leg gets stopped out, the other continues running.

---

## Configuration Parameters

All parameters are defined as constants at the top of the script. No external config file needed.

### Connection

| Parameter | Default | Description |
|-----------|---------|-------------|
| `OPENALGO_HOST` | `http://127.0.0.1:5000` | OpenAlgo server URL (env: `OPENALGO_HOST`) |
| `OPENALGO_API_KEY` | `""` | API key (env: `OPENALGO_APIKEY`) |
| `TELEGRAM_USER` | `""` | OpenAlgo username for Telegram (env: `OPENALGO_USERNAME`) |

### Instrument

| Parameter | Default | Description |
|-----------|---------|-------------|
| `UNDERLYING` | `NIFTY` | Index to trade |
| `LOT_SIZE` | `65` | NIFTY lot size (verify current NSE lot size) |
| `NUMBER_OF_LOTS` | `1` | Lots per leg |
| `PRODUCT` | `MIS` | MIS (intraday) or NRML (carry forward) |
| `STRIKE_ROUNDING` | `50` | Strike interval for ATM calculation |

### Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENTRY_TIME` | `09:17` | Fixed entry time for all DTEs. Captures maximum opening IV premium (15-25% inflated in first 2 min after open). Backtest-optimised: beats DTE-based entry by +29% P&L |
| `EXIT_TIME` | `15:15` | Hard square-off — closes ALL legs. MIS auto-liquidation is at 15:30 |
| `MONITOR_INTERVAL` | `5` | Seconds between monitor ticks |

### Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TRADE_DTE` | `[0,1,2,3,4]` | Allowed DTEs. 0=expiry day (Tue), 4=Wed. All 5 trading days |
| `SKIP_MONTHS` | `[11]` | Skip November — consistent loss month across all backtest years |

---

## Risk Management Modules

### 1. Per-Leg Independent SL

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LEG_SL_PERCENT` | `30.0` | % of entry premium. CE and PE have independent SLs |

**How it works:**
- CE SL = `CE_entry_price x (1 + 30/100)` = 130% of entry
- PE SL = `PE_entry_price x (1 + 30/100)` = 130% of entry
- When one leg hits SL, only that leg closes. The other continues

**Why 30%:** Wider SL lets trades breathe for theta capture. At 20% (previous), false SL hits were common on morning volatility spikes especially DTE2+. 30% reduces premature stops while daily loss limit (-6K) caps total risk.

### 2. Daily Profit Target & Loss Limit

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAILY_TARGET` | `10000` | Per-lot Rs. profit target. Effective = per_lot x lots |
| `DAILY_LOSS_LIMIT` | `-6000` | Per-lot Rs. loss limit (negative). Effective = per_lot x lots |

**How it works:** Every monitor tick, `combined_pnl = closed_leg_pnl + open_leg_mtm`. If it hits target or limit, ALL remaining legs close immediately.

### 3. Breakeven SL After Partial Exit

| Parameter | Default | Description |
|-----------|---------|-------------|
| `BREAKEVEN_ENABLED` | `true` | Tighten surviving leg SL to combined breakeven |
| `BREAKEVEN_GRACE_MIN` | `5` | Minutes to wait before arming breakeven SL |
| `BREAKEVEN_BUFFER_PCT` | `5.0` | % buffer above mathematical breakeven price |

**How it works:**
After one leg hits SL (e.g., CE closes at loss of -Rs.1300):
1. Calculate breakeven price for survivor: `be_sl = PE_entry + (closed_pnl / qty)`
2. Apply buffer: `be_sl = be_sl x (1 + 5/100)`
3. Wait grace period (5 min) before arming
4. If surviving leg LTP hits breakeven SL → close to prevent net loss

**Context-aware (FIX-XXIV):** If the surviving leg is already WINNING (LTP < entry for short), breakeven SL is SKIPPED. Only arms when survivor is LOSING — where it's genuinely needed.

**SL Priority Chain:**
1. Breakeven SL (if active, grace elapsed, AND tighter than fixed SL)
2. Fixed SL (entry x 1.30)

### 4. Combined Premium Decay Exit

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COMBINED_DECAY_ENABLED` | `true` | Exit when both legs have collectively decayed |
| `COMBINED_DECAY_DEFAULT` | `60.0` | Default decay target % |
| `COMBINED_DECAY_DTE_MAP` | `{0:60, 1:65, 2:60, 3:50, 4:50}` | DTE-specific targets |

**How it works:**
When BOTH legs are active:
```
decay_pct = (1 - (CE_ltp + PE_ltp) / (CE_entry + PE_entry)) x 100
```
If `decay_pct >= target` for current DTE → close all.

**DTE-aware targets:**
- DTE0 (Tue/expiry): 60% — captures most theta, avoids late gamma
- DTE1 (Mon): 65% — strong theta, slightly higher target
- DTE2 (Fri): 60% — standard
- DTE3 (Thu): 50% — lower premium, exit earlier
- DTE4 (Wed): 50% — thin premium, exit earlier

### 5. Winner-Leg Early Booking

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WINNER_BOOKING_ENABLED` | `true` | Book surviving leg when deeply decayed |
| `WINNER_BOOKING_DECAY_PCT` | `30.0` | Book when LTP <= 30% of entry (70%+ profit) |

**How it works:**
After one leg closes (partial exit), the surviving "winner" leg often has large unrealised profit. If its LTP drops to 30% of entry (70% decay), book it immediately to lock profit and remove gamma risk.

### 6. Asymmetric Leg Booking

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ASYMMETRIC_ENABLED` | `true` | Book deeply decayed leg when other is intact |
| `ASYMMETRIC_WINNER_DECAY_PCT` | `40.0` | Winner must be at/below 40% of entry |
| `ASYMMETRIC_LOSER_INTACT_PCT` | `80.0` | Loser must be at/above 80% of entry |

**How it works:**
When both legs active but heavily diverged — one deeply profitable, other barely moved — the position is effectively a naked short on the losing side. Book the winner to lock profit and reduce gamma exposure.

Example: CE at 35% of entry (deep profit), PE at 85% (barely moved) → Book CE.

### 7. Combined Profit Trailing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COMBINED_TRAIL_ENABLED` | `true` | Trail combined decay, exit on retracement |
| `COMBINED_TRAIL_ACTIVATE_PCT` | `30.0` | Start trailing at 30% combined decay |
| `COMBINED_TRAIL_PCT` | `40.0` | Exit if decay retraces 40 points from peak |

**How it works:**
When both legs active:
1. Once combined decay reaches 30%, start tracking the peak decay
2. If decay retraces 40 points from peak → close all

This protects combined gains before the decay target is reached. Example: decay peaks at 55%, then drops to 15% (40-point retrace) → exit.

### 8. Re-Entry After Early Close

| Parameter | Default | Description |
|-----------|---------|-------------|
| `REENTRY_ENABLED` | `true` | Allow re-entry after early SL exit |
| `REENTRY_COOLDOWN_MIN` | `45` | Minutes to wait after close before re-entry |
| `REENTRY_MAX_PER_DAY` | `2` | Maximum re-entries per day |
| `REENTRY_MAX_LOSS` | `2000` | Per-lot Rs. — skip re-entry if previous loss exceeds this |

**How it works:**
After a full close with a manageable loss (< Rs.2000/lot), the strategy waits 45 minutes for conditions to stabilise, then re-enters a fresh straddle. All entry filters are re-checked.

**Safety guards:**
- 45-min cooldown lets market settle after adverse move
- Rs.2000/lot loss cap blocks re-entry after large SL losses
- Max 2 re-entries/day prevents runaway loops
- Full filter chain runs on every re-entry
- Cumulative daily P&L carries forward (FIX-XVII)

### 9. VIX Spike Monitor

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VIX_SPIKE_ENABLED` | `true` | Exit on mid-session VIX spike |
| `VIX_SPIKE_THRESHOLD` | `15.0` | % rise from entry VIX triggers exit |
| `VIX_SPIKE_ABS_FLOOR` | `18.0` | Minimum absolute VIX to confirm spike |
| `VIX_SPIKE_INTERVAL_S` | `300` | Seconds between VIX checks (5 min) |

**How it works:**
Short straddle is short vega — rising VIX increases both legs' value even if NIFTY stays flat. Every 5 minutes, compares current VIX to entry VIX.

**Dual condition (FIX-V):** Exit fires ONLY when BOTH:
1. Relative spike >= 15% from entry VIX
2. Current VIX >= 18 (absolute floor)

This prevents false exits at low absolute VIX levels (e.g., 14→16.1 = 15% spike but VIX 16 is normal).

### 10. Margin Guard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MARGIN_GUARD_ENABLED` | `true` | Pre-trade margin check |
| `MARGIN_BUFFER` | `1.20` | 20% headroom above required margin |
| `MARGIN_FAIL_OPEN` | `true` | Allow trade if margin API fails |

**How it works:**
Before entry: `(available_cash + collateral) >= required_margin x 1.20`. If insufficient, entry is blocked with a Telegram alert.

### 11. Net P&L Guard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `NET_PNL_GUARD_MAX_DEFER_MIN` | `15` | Max minutes to defer per-leg SL |

**How it works (FIX-XX):**
After partial exit, if the surviving leg hits its SL BUT the net position P&L (closed_pnl + open_mtm) is still positive, the SL exit is deferred. The leg is allowed to continue as long as the net is positive, up to a maximum of 15 minutes. This prevents stopping out when the closed leg's loss is already covered by the survivor's profit.

---

## Disabled Modules

These modules were tested in 5-year backtests and found to hurt more than help:

| Module | Why Disabled |
|--------|-------------|
| **Trailing SL** | Rarely activates (3/224 trades), cuts winners short |
| **Dynamic SL** (time-of-day) | Creates false SL hits on afternoon bounces with 30% base SL |
| **Spot-Move Exit** | 26% of exits were premature, positions naturally recover |
| **Recovery Lock** | #1 exit reason (42%), cuts profits before theta completes |
| **VIX Pre-Trade Filter** | No DD reduction, costs 47% net P&L |
| **IVR/IVP Filter** | Skips 63% of profitable days |
| **ORB Filter** | Costs Rs.65K/5yr for Rs.443 DD improvement |
| **Momentum Filter** | Redundant with re-entry cooldown + loss cap + max/day |

---

## Monitor Tick Execution Order

Every `MONITOR_INTERVAL` seconds (5s), the monitor runs this hierarchy:

```
1. Per-leg SL check (each leg independently)
   → Fetch LTP → check vs sl_level() → close if hit
   → Net P&L guard defers SL when net position is positive

2. Combined checks (both legs active):
   a. Combined decay exit — both legs decayed enough?
   b. Asymmetric booking — one deeply decayed, other intact?
   c. Combined profit trailing — decay retracing from peak?

3. Winner-leg booking (single survivor)
   → Book if survivor decayed to 30% of entry

4. Combined P&L update

5. VIX spike check (every 5 min)
   → Dual condition: relative + absolute

6. Daily target / loss limit check
```

---

## State Persistence

- **Atomic writes:** Write to temp file, then `os.replace()` — never corrupts on crash
- **Crash recovery:** On startup, `Reconciler` compares saved state vs broker positions
- **Type safety:** ISO datetime strings restored to IST-aware datetimes on load
- **State file:** `strategy_state.json` — deleted after position fully closes

### Reconciliation Cases

| Saved State | Broker Position | Action |
|-------------|----------------|--------|
| None | Flat | Clean start |
| Saved | Confirmed | Restore + resume monitoring |
| Saved | Flat | Externally closed, reset |
| Stale (prev day) | Any | Reset |

---

## WebSocket Feed

- Daemon thread with auto-reconnect (exponential backoff 1s → 30s)
- Subscribes to option legs + NIFTY spot + India VIX
- Updates LTP cache with sub-second prices
- Falls back to REST API if cache is stale (>60s) or WS disconnected
- Telegram alert on persistent connection failures

---

## Telegram Notifications

Sent for: entry, partial exit, full exit, margin issues, quote failures, VIX spikes.

Uses OpenAlgo's Telegram API. Messages are queued and delivered in a background daemon thread (non-blocking). 3 retry attempts with backoff.

---

## Trade Logging

Every position close appends a JSON record to `trades.jsonl`:
- Entry/exit prices, P&L, duration, exit reason
- Market context (VIX, spot, DTE)
- SL events, re-entry flag, trade count

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENALGO_HOST` | OpenAlgo server URL |
| `OPENALGO_APIKEY` | OpenAlgo API key |
| `OPENALGO_USERNAME` | OpenAlgo username (for Telegram) |

---

## Running

```bash
# Production (via OpenAlgo Python Strategy)
python nifty_short_straddle.py

# Manual utilities (uncomment in script)
# strategy.check_connection()
# strategy.manual_entry()
# strategy.manual_exit()
# strategy.show_state()
```

---

## Backtest Results (5-Year: 2021-2026)

| Metric | Value |
|--------|-------|
| Net P&L | Rs.13.15L |
| Win Rate | 69.7% |
| Max Drawdown | -Rs.11,516 |
| Calmar Ratio | 114.2 |
| Losing Months | 0 |
| Fixed Entry | 09:17 (all DTEs) |
