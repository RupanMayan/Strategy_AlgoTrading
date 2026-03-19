# Nifty Short Straddle — Partial Square Off v5.8.0

An intraday algorithmic trading strategy for NIFTY weekly options using OpenAlgo broker API. Sells ATM Call and Put simultaneously, monitors each leg independently, and exits partially or fully based on multiple configurable risk/exit conditions.

---

## Table of Contents

- [Strategy Overview](#strategy-overview)
- [Backtest Results](#backtest-results)
- [Core Logic — Partial Square Off](#core-logic--partial-square-off)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entry Filters](#entry-filters)
- [Exit Conditions](#exit-conditions)
- [State Management & Crash Recovery](#state-management--crash-recovery)
- [Monitoring & Alerts](#monitoring--alerts)
- [Running the Script](#running-the-script)
- [Manual Controls](#manual-controls)
- [Trade Log](#trade-log)
- [Version History](#version-history)

---

## Strategy Overview

| Parameter | Value |
|---|---|
| **Underlying** | NIFTY (configurable: BANKNIFTY, FINNIFTY) |
| **Expiry** | Weekly (auto-resolves nearest Tuesday) |
| **Product** | MIS (intraday auto square-off) |
| **Position** | SELL ATM CE + SELL ATM PE simultaneously |
| **Lot Size** | 65 (NIFTY) |
| **Entry Style** | Atomic multi-leg via `optionsmultiorder()` |
| **Exit Style** | Per-leg independent SL + multiple exit conditions |
| **Hard Exit** | 15:15 IST |

---

## Backtest Results

*AlgoTest 2019–2026 | 1,746 trades | PARTIAL mode*

| Metric | Value |
|---|---|
| **Total P&L** | Rs. 5,04,192 (qty 65) → ~Rs. 5,81,000 (qty 75) |
| **Win Rate** | 66.71% |
| **Average P&L / Trade** | Rs. 289 |
| **Max Drawdown** | Rs. 34,179 |
| **Return / MDD** | 1.38 |
| **Reward : Risk** | 1.09 |

**Statistical breakdown from 1,746-trade CSV:**
- **61.6%** of trades: one leg SL fires, other leg survives to hard exit (15:15)
- **35.4%** of trades: both legs SL hit at different times (independent)
- **3.0%** of trades: both legs exit at hard exit (15:15)
- **99.6%** of SL exits: hit at exactly 20.0% of leg's entry premium
- **Median SL hit:** 22 min after entry | 40% of SLs hit within 15 min

---

## Core Logic — Partial Square Off

### Entry

Both legs placed atomically in a single API call at ATM strike. Entry time varies by DTE (days to expiry):

| DTE | Day | Entry Time |
|---|---|---|
| DTE 0 | Tuesday (Expiry) | 09:30 IST |
| DTE 1 | Monday | 09:30 IST |
| DTE 2 | Friday | 09:35 IST |
| DTE 3 | Thursday | 09:40 IST |
| DTE 4 | Wednesday | 09:45 IST |

### Per-Leg Independent SL

Each leg has its own SL level computed at entry and monitored independently:

```
CE_SL = CE_entry_price × (1 + LEG_SL_PERCENT / 100)   → e.g. Rs.100 × 1.20 = Rs.120
PE_SL = PE_entry_price × (1 + LEG_SL_PERCENT / 100)   → e.g. Rs.100 × 1.20 = Rs.120
```

Closing one leg does **not** close the other. The surviving leg continues with its own SL.

### P&L Accounting

```
closed_pnl    = realized P&L from all closed legs (updated each time a leg closes)
open_mtm      = unrealized MTM of all active legs (recomputed each monitor tick)
combined_pnl  = closed_pnl + open_mtm
```

### Example Trade Flow

```
Entry:  SELL CE @ Rs.100,  SELL PE @ Rs.100
        CE_SL = Rs.120,    PE_SL = Rs.120

Tick 5: PE LTP = Rs.126 >= PE_SL Rs.120  →  Close PE only
        Realized P&L on PE = (100 - 126) × 65 = -Rs.1,690
        CE remains open with its own SL at Rs.120
        Breakeven SL activates on CE: be_sl = 100 + (-1690/65) = Rs.74

Tick 12: CE LTP = Rs.121 >= CE_SL Rs.120  →  Close CE
         Realized P&L on CE = (100 - 121) × 65 = -Rs.1,365

Final combined_pnl = -1,690 + -1,365 = -Rs.3,055
```

---

## Prerequisites

### Software

- Python 3.10+
- OpenAlgo running locally (default: `http://127.0.0.1:5000`)
- Master contracts synced before 09:00 IST

### Python Packages

```bash
pip install openalgo apscheduler pytz requests
```

### OpenAlgo Setup

1. Install and start OpenAlgo
2. Register strategy name `Short Straddle` in the dashboard
3. Enable Analyze Mode for paper trading, or toggle off for live
4. Note your API key from the dashboard

### Telegram Setup (Optional)

1. Create a bot via BotFather → get `BOT_TOKEN`
2. Get your `CHAT_ID` by messaging `@userinfobot`

---

## Installation

```bash
git clone <repo>
cd PythonScript/Options

# Set environment variables
export OPENALGO_APIKEY="your_api_key"
export TELEGRAM_BOT_TOKEN="your_bot_token"    # optional
export TELEGRAM_CHAT_ID="your_chat_id"        # optional
```

### Bootstrap VIX History (Required for IVR/IVP filter)

Run once before first trade to download historical VIX data:

```python
# Uncomment in __main__ block:
manual_bootstrap_vix()
```

This creates `vix_history.csv` (format: `date,vix_close`) used for IV Rank and IV Percentile calculations. The script auto-appends each day's closing VIX at 15:30 IST thereafter.

---

## Configuration

All parameters are at the top of the script, organized by section.

### Section 1 — OpenAlgo Connection

| Parameter | Default | Description |
|---|---|---|
| `OPENALGO_HOST` | `http://127.0.0.1:5000` | OpenAlgo API endpoint |
| `OPENALGO_API_KEY` | env `OPENALGO_APIKEY` | Broker authentication key |

### Section 2 — Instrument

| Parameter | Default | Description |
|---|---|---|
| `UNDERLYING` | `"NIFTY"` | Index symbol (NIFTY / BANKNIFTY / FINNIFTY) |
| `EXCHANGE` | `"NSE_INDEX"` | Exchange for index quotes |
| `LOT_SIZE` | `65` | NIFTY=65, BANKNIFTY=35, FINNIFTY=40 |
| `NUMBER_OF_LOTS` | `1` | Number of lots per leg |
| `PRODUCT` | `"MIS"` | MIS (intraday) or NRML (overnight) |
| `STRIKE_OFFSET` | `"ATM"` | ATM / OTM1–5 / ITM1–5 |

### Section 3 — Timing

| Parameter | Default | Description |
|---|---|---|
| `ENTRY_TIME` | `"09:30"` | Fallback entry time (HH:MM IST) |
| `EXIT_TIME` | `"15:15"` | Hard exit time — closes all remaining legs |
| `MONITOR_INTERVAL_S` | `15` | Seconds between P&L / SL checks |
| `USE_DTE_ENTRY_MAP` | `True` | Enable DTE-aware staggered entry times |
| `DTE_ENTRY_TIME_MAP` | `{0:"09:30", 1:"09:30", 2:"09:35", 3:"09:40", 4:"09:45"}` | Entry time per DTE |

### Section 4 — DTE Filter

| Parameter | Default | Description |
|---|---|---|
| `TRADE_DTE` | `[0, 1, 2, 3, 4]` | Allowed DTEs (0=expiry/Tue, 1=Mon, 2=Fri, 3=Thu, 4=Wed) |

DTE is calculated using trading days only (weekends excluded), matching AlgoTest methodology.

### Section 5 — Month Filter

| Parameter | Default | Description |
|---|---|---|
| `SKIP_MONTHS` | `[11]` | Months to skip (1=Jan … 12=Dec). Default skips November. |

### Section 6 — VIX Filter

| Parameter | Default | Description |
|---|---|---|
| `VIX_FILTER_ENABLED` | `True` | Enable VIX range filter |
| `VIX_MIN` | `14.0` | Skip if VIX below (premiums too thin) |
| `VIX_MAX` | `28.0` | Skip if VIX above (danger zone) |

### Section 6A — IV Rank / IV Percentile Filter

| Parameter | Default | Description |
|---|---|---|
| `IVR_FILTER_ENABLED` | `True` | Enable IV Rank filter |
| `IVR_MIN` | `30.0` | Skip if IVR < 30 (IV in bottom 30% of 52-week range) |
| `IVP_FILTER_ENABLED` | `True` | Enable IV Percentile filter |
| `IVP_MIN` | `40.0` | Skip if IVP < 40% |
| `IVR_FAIL_OPEN` | `False` | `False` = skip trade if VIX history unavailable (production-safe) |
| `VIX_HISTORY_FILE` | `"vix_history.csv"` | Path to daily VIX history CSV |
| `VIX_HISTORY_MIN_ROWS` | `100` | Minimum rows for meaningful IVR/IVP |
| `VIX_UPDATE_TIME` | `"15:30"` | Time to auto-append today's closing VIX |

**Formulas:**
```
IVR = (Today_VIX - 52wk_Low) / (52wk_High - 52wk_Low) × 100
IVP = (Days where VIX < Today_VIX) / 252 × 100
```

### Section 6B — Opening Range Filter (ORB)

| Parameter | Default | Description |
|---|---|---|
| `ORB_FILTER_ENABLED` | `True` | Enable opening range breakout filter |
| `ORB_CAPTURE_TIME` | `"09:17"` | Time to capture NIFTY spot as ORB reference |
| `ORB_MAX_MOVE_PCT` | `0.5` | Skip if NIFTY moved > 0.5% from ORB reference |

**Logic:** Rejects trades on trending opens where one leg is immediately deep ITM.

```
move_pct = |entry_spot - ORB_spot| / ORB_spot × 100
if move_pct > ORB_MAX_MOVE_PCT → skip trade
```

### Section 7 — Risk Management (Per-Leg)

| Parameter | Default | Description |
|---|---|---|
| `LEG_SL_PERCENT` | `20.0` | SL as % of entry premium, applied independently per leg |
| `DAILY_PROFIT_TARGET_PER_LOT` | `5000` | Rs. per lot; 0 = disabled |
| `DAILY_LOSS_LIMIT_PER_LOT` | `-4000` | Rs. per lot (negative); 0 = disabled |

Effective targets auto-scale: `DAILY_PROFIT_TARGET = DAILY_PROFIT_TARGET_PER_LOT × NUMBER_OF_LOTS`

### Section 7A — Pre-Trade Margin Guard

| Parameter | Default | Description |
|---|---|---|
| `MARGIN_GUARD_ENABLED` | `True` | Enable pre-trade margin check |
| `MARGIN_BUFFER` | `1.20` | Required headroom: available must be >= required × 1.20 |
| `MARGIN_GUARD_FAIL_OPEN` | `True` | Allow trade if margin API call fails |
| `ATM_STRIKE_ROUNDING` | `50` | Strike rounding for margin basket (NIFTY=50, BANKNIFTY=100) |

### Section 7B — Intraday VIX Spike Monitor

| Parameter | Default | Description |
|---|---|---|
| `VIX_SPIKE_MONITOR_ENABLED` | `True` | Exit if VIX spikes during trade |
| `VIX_SPIKE_THRESHOLD_PCT` | `15.0` | % rise from entry VIX |
| `VIX_SPIKE_CHECK_INTERVAL_S` | `300` | Seconds between intraday VIX checks (throttled) |
| `VIX_SPIKE_ABS_FLOOR` | `18.0` | Minimum absolute VIX for spike exit to fire |

**Dual condition (both must be true):**
```
spike_pct = (current_vix - entry_vix) / entry_vix × 100
Exit fires ONLY when: spike_pct >= 15.0  AND  current_vix >= 18.0
```

This prevents false exits at low VIX (e.g., VIX 14→16.1 = 15% spike but not dangerous).

### Section 7C — Trailing Stop-Loss / Profit Lock-In

| Parameter | Default | Description |
|---|---|---|
| `TRAILING_SL_ENABLED` | `True` | Enable trailing SL per leg |
| `TRAIL_TRIGGER_PCT` | `50.0` | Activate trailing when LTP ≤ 50% of entry price |
| `TRAIL_LOCK_PCT` | `30.0` | Trailing SL = current_LTP × (1 + 30%/100) |

**Phase 1 — Activation (first time LTP crosses trigger):**
```
Trigger: LTP ≤ entry_px × (TRAIL_TRIGGER_PCT / 100)
Initial trailing SL = LTP × (1 + TRAIL_LOCK_PCT / 100)
Safety cap: trailing SL cannot be worse than fixed SL
```

**Phase 2 — Update (every monitor tick while trailing active):**
```
new_trail_sl = LTP × (1 + TRAIL_LOCK_PCT / 100)
Update only if new_trail_sl < current_trailing_sl  (SL can only tighten, never loosen)
```

State is persisted on every tightening — crash recovery resumes exact trailing level.

### Section 7D — Dynamic SL Tightening

| Parameter | Default | Description |
|---|---|---|
| `DYNAMIC_SL_ENABLED` | `True` | Enable time-graduated SL |
| `DYNAMIC_SL_SCHEDULE` | `[("14:30", 7.0), ("13:30", 10.0), ("12:00", 15.0)]` | Time thresholds → SL % |

**Schedule (evaluated in descending order; first match wins):**

| Time | SL % | Rationale |
|---|---|---|
| Before 12:00 | 20.0% (default) | Morning session — full risk justified |
| 12:00 – 13:30 | 15.0% | Mid-session tightening |
| 13:30 – 14:30 | 10.0% | Afternoon — theta harvested, protect gains |
| After 14:30 | 7.0% | Last 45 min — max theta, minimal room needed |

Dynamic SL only applies to legs not yet in trailing SL phase. Trailing SL always takes precedence.

### Section 7E — Combined Premium Decay Exit

| Parameter | Default | Description |
|---|---|---|
| `COMBINED_DECAY_EXIT_ENABLED` | `True` | Exit when combined premium decays sufficiently |
| `COMBINED_DECAY_TARGET_PCT` | `60.0` | Close all when combined LTP decays 60% from entry |

**Formula:**
```
combined_entry   = CE_entry_price + PE_entry_price
combined_current = CE_ltp + PE_ltp
decay_pct        = (1 - combined_current / combined_entry) × 100
if decay_pct >= 60% → close_all()
```

Fires only when both legs are active and valid LTPs are available.

### Section 7F — Winner-Leg Early Booking

| Parameter | Default | Description |
|---|---|---|
| `WINNER_LEG_EARLY_EXIT_ENABLED` | `True` | Book profitable surviving leg early |
| `WINNER_LEG_DECAY_THRESHOLD_PCT` | `30.0` | Book when LTP falls to ≤ 30% of entry price |

**Logic:** Fires when exactly ONE leg is active (other leg already closed):
```
remaining_pct = (survivor_ltp / survivor_entry) × 100
if remaining_pct ≤ 30% → close_one_leg()   (70%+ profit captured, gamma risk removed)
```

### Section 7G — Breakeven SL After Partial Exit

| Parameter | Default | Description |
|---|---|---|
| `BREAKEVEN_AFTER_PARTIAL_ENABLED` | `True` | Tighten surviving leg SL to breakeven |

**Activates only when a leg closes at a net loss (`closed_pnl < 0`):**
```
be_sl = other_leg_entry_px + closed_pnl / qty()

Example: PE entry Rs.100, closed_pnl = -Rs.1,300 (CE SL hit loss)
  be_sl = 100 + (-1300/65) = 100 - 20 = Rs.80
  At Rs.80 exit: combined position P&L = Rs.0 (no additional loss)
```

### Section 7G (continued) — Spot-Move / Breakeven Breach Exit

| Parameter | Default | Description |
|---|---|---|
| `BREAKEVEN_SPOT_EXIT_ENABLED` | `True` | Exit when NIFTY move breaches straddle breakeven |
| `BREAKEVEN_SPOT_MULTIPLIER` | `1.0` | Exit when move >= combined_premium × 1.0 |
| `SPOT_CHECK_INTERVAL_S` | `60` | Seconds between NIFTY spot fetches (throttled) |

**Formula:**
```
combined_premium  = CE_entry_price + PE_entry_price
move_threshold    = combined_premium × BREAKEVEN_SPOT_MULTIPLIER
move_abs          = |current_NIFTY - entry_NIFTY|
if move_abs >= move_threshold → close_all()
```

### Section 8 — Expiry

| Parameter | Default | Description |
|---|---|---|
| `AUTO_EXPIRY` | `True` | Auto-resolve nearest Tuesday expiry |
| `MANUAL_EXPIRY` | `"25MAR26"` | Used only when AUTO_EXPIRY=False |

Format for MANUAL_EXPIRY: `DDMMMYY` uppercase, must be a Tuesday.

### Section 9 — Strategy Name

| Parameter | Default | Description |
|---|---|---|
| `STRATEGY_NAME` | `"Short Straddle"` | Must match OpenAlgo dashboard registration exactly |

### Section 10 — Telegram Alerts

| Parameter | Default | Description |
|---|---|---|
| `TELEGRAM_ENABLED` | `True` | Send real-time alerts via Telegram |
| `TELEGRAM_BOT_TOKEN` | env `TELEGRAM_BOT_TOKEN` | Bot API token |
| `TELEGRAM_CHAT_ID` | env `TELEGRAM_CHAT_ID` | Destination chat ID |

### Section 11 — State & Logging

| Parameter | Default | Description |
|---|---|---|
| `STATE_FILE` | `"strategy_state.json"` | Crash-safe atomic state persistence |
| `TRADE_LOG_FILE` | `"trades.jsonl"` | Per-trade JSON log; set `""` to disable |
| `QUOTE_FAIL_ALERT_THRESHOLD` | `3` | Consecutive failed LTP ticks before Telegram alert |

---

## Entry Filters

Filters are evaluated in this order inside `job_entry()`. Any failure skips the trade.

```
1. DTE-Aware Entry Time Guard   → correct time slot for today's DTE?
2. Duplicate Guard              → already in position?
3. DTE Filter                   → today's DTE in TRADE_DTE? weekend? skip month?
4. VIX Filter                   → VIX_MIN ≤ VIX ≤ VIX_MAX?
5. IVR / IVP Filter             → IVR >= 30 AND IVP >= 40?
6. Opening Range Filter (ORB)   → NIFTY move <= 0.5% from 09:17 reference?
7. Margin Guard                 → available funds >= required × 1.20?
8. Reset counters + place entry
```

---

## Exit Conditions

All exits call `close_one_leg()` or `close_all()` via BUY MARKET order(s). Priority order during each monitor tick:

| Priority | Exit Condition | Trigger |
|---|---|---|
| 1 | **Per-Leg Fixed SL** | `ltp >= entry × (1 + LEG_SL_PERCENT/100)` |
| 1 | **Per-Leg Trailing SL** | `ltp >= trailing_sl` (overrides fixed SL once active) |
| 1 | **Per-Leg Breakeven SL** | `ltp >= be_sl` (tighter than fixed, activated after partial loss) |
| 1 | **Per-Leg Dynamic SL** | Same check using time-graduated % instead of 20% |
| 2 | **Combined Decay Exit** | Both legs active; combined premium decayed >= 60% |
| 3 | **Winner Leg Booking** | One leg active; LTP remaining <= 30% of entry |
| 4 | **VIX Spike Exit** | Throttled check; spike >= 15% AND VIX >= 18 |
| 5 | **Spot-Move Exit** | Throttled check; NIFTY moved >= combined premium |
| 6 | **Daily Profit Target** | `combined_pnl >= DAILY_PROFIT_TARGET` |
| 7 | **Daily Loss Limit** | `combined_pnl <= DAILY_LOSS_LIMIT` |
| 8 | **Hard Exit** | Scheduled job at 15:15 IST |

---

## State Management & Crash Recovery

### Atomic State Persistence

Every state mutation is saved to `strategy_state.json` via atomic write (temp file + `os.replace()`) — a partial write never corrupts the saved state.

### Restart Reconciliation

On startup, the script compares saved state with live broker positions:

| Case | Saved State | Broker | Action |
|---|---|---|---|
| **A** | No file / flat | Flat | Clean start — do nothing |
| **B** | In position | Confirms positions | **Restore** — resume monitoring from exact state |
| **C** | In position | Flat | Externally closed — clear state, log exit |
| **D** | No file | Has NFO positions | **Orphan** — emergency close all, alert operator |

**Case B details:**
- Restores all fields including trailing SL levels, breakeven SL, closed P&L
- If entry prices missing (crash during fill capture): re-fetches via `orderstatus()`
- Stale state detection: if `entry_date != today` → MIS auto-SQ already happened → clear state

---

## Monitoring & Alerts

### Monitor Loop

- Runs every `MONITOR_INTERVAL_S` (15s) while `in_position = True`
- Non-blocking: if previous tick still running, current tick is skipped
- Thread-safe: RLock prevents concurrent monitor ticks

### Broker Connectivity Monitoring

- Tracks consecutive ticks where ALL active legs fail LTP fetch
- After 3 failed ticks (~45s): Telegram alert "BROKER QUOTES UNREACHABLE, SL monitoring paused"
- On first successful tick after outage: Telegram "QUOTES RESTORED, SL monitoring resuming"

### Telegram Alert Events

| Event | Alert |
|---|---|
| Strategy startup | Version, config summary, entry/exit times, filters |
| Trade entry | Fills, SL levels, VIX, IVR, IVP, margin used |
| Leg SL hit | Closed leg P&L, reason (Fixed/Trailing/Dynamic/Breakeven SL) |
| Trailing SL activated | Trigger price, profit locked, fixed vs trailing comparison |
| Combined decay exit | Decay %, combined LTP vs entry, reason |
| Winner leg booking | Remaining %, entry price, threshold |
| VIX spike exit | Entry/current VIX, spike %, both conditions |
| Spot-move exit | Direction, move distance, combined premium threshold |
| Daily target/limit | P&L vs threshold, reason |
| Full exit | Final P&L, duration, session trade count |
| Entry filter skip | VIX/IVR/IVP/ORB/margin reason with values |
| Quote fail / restore | Consecutive ticks, active legs |
| Reconciliation | Case A/B/C/D, state restored or cleared |
| Errors | Fill capture fail, close order fail, crash |

---

## Running the Script

### Environment Variables

```bash
export OPENALGO_APIKEY="your_api_key"
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

### Run Directly

```bash
python Nifty_ShortStraddle_Partial.py
```

### Run as Systemd Service (Recommended for Production)

```ini
[Unit]
Description=NIFTY Short Straddle Strategy
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/strategy
Environment="OPENALGO_APIKEY=your_key"
Environment="TELEGRAM_BOT_TOKEN=your_token"
Environment="TELEGRAM_CHAT_ID=your_chat_id"
ExecStart=/usr/bin/python3 Nifty_ShortStraddle_Partial.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable straddle
sudo systemctl start straddle
sudo journalctl -u straddle -f
```

### Scheduled Jobs (Auto-Registered)

| Job | Time | Frequency | Description |
|---|---|---|---|
| ORB Capture | 09:17 IST | Mon–Fri | Capture NIFTY spot as opening range reference |
| Entry Job(s) | DTE-based | Mon–Fri | One job per unique entry time in DTE_ENTRY_TIME_MAP |
| Monitor | Every 15s | Continuous | P&L, SL, decay, VIX, spot checks |
| VIX History | 15:30 IST | Mon–Fri | Append today's closing VIX to history CSV |
| Hard Exit | 15:15 IST | Mon–Fri | Close all remaining active legs |

---

## Manual Controls

Uncomment in the `__main__` block at the bottom of the script to run one-off operations:

```python
check_connection()       # Test OpenAlgo connection + show available funds
check_margin_now()       # Test margin guard without placing a trade
manual_entry()           # Force entry now (bypasses all filters)
manual_exit()            # Close all open legs immediately
show_state()             # Print full state, effective SL levels, DTE, expiry
manual_bootstrap_vix()   # Download VIX history from NSE (run once before first trade)
```

---

## Trade Log

When `TRADE_LOG_FILE = "trades.jsonl"`, every completed trade appends a JSON line:

```json
{
  "date": "2026-03-20",
  "entry_time": "09:30:12",
  "exit_time": "10:45:33",
  "duration_min": 75.3,
  "symbol_ce": "NIFTY25MAR2623000CE",
  "symbol_pe": "NIFTY25MAR2623000PE",
  "entry_price_ce": 102.50,
  "entry_price_pe": 98.75,
  "exit_price_ce": 123.00,
  "exit_price_pe": 51.25,
  "qty": 65,
  "closed_pnl": -1991.25,
  "exit_reason": "CE Fixed SL 20% Hit / PE Winner Leg Early Booking",
  "vix_at_entry": 16.8,
  "ivr_at_entry": 42.3,
  "ivp_at_entry": 55.1,
  "trade_count": 3
}
```

---

## Version History

### v5.8.0 — Phase 2 Market-Movement Enhancements *(current)*
- **ENH-D:** Opening Range Filter (ORB) — skip trades on trending opens > 0.5% move from 09:17 reference
- **ENH-E:** Breakeven SL After Partial Exit — surviving leg SL moved to breakeven price after partial loss
- **ENH-F:** Spot-Move / Breakeven Breach Exit — exit when NIFTY moves >= combined premium from entry

### v5.7.0 — Phase 1 Market-Movement Enhancements
- **ENH-A:** Dynamic SL Tightening — time-graduated SL schedule (20%→15%→10%→7% through the day)
- **ENH-B:** Combined Premium Decay Exit — close all when both legs combined premium decays 60%
- **ENH-C:** Winner-Leg Early Booking — close surviving leg when LTP falls to ≤30% of entry (lock gamma risk)

### v5.6.0 — Post-Audit Improvements
- **ENH-I:** Structured Trade Log (`trades.jsonl`) — one JSON per line with full trade context
- **ENH-II:** Concurrent Fill Capture (`ThreadPoolExecutor`) — parallel CE/PE `orderstatus()` calls
- **ENH-III:** Broker Connectivity Escalation — alert on 3+ consecutive quote-fail ticks (~45s)

### v5.5.1 — Post-Audit Third Pass
- **FIX-VII:** Case B reconciliation regression — detect `entry_price=0.0` after crash, re-fetch fills
- **FIX-VIII:** VIX spike abs floor validation — guard against negative floor configuration
- **FIX-IX:** VIX spike abs floor display — added to startup banner and Telegram startup message

### v5.5.0 — Logical Fixes (Post-Audit Second Pass)
- **FIX-I (CRITICAL):** Fill capture blocked `place_entry()` for up to 20s — moved to background daemon thread
- **FIX-II (CRITICAL):** `closeposition()` failure had no fallback — added per-leg fallback on rejection
- **FIX-III:** Trailing SL Phase 2 persistence — save state on every tightening (prevent loose revert on crash)
- **FIX-IV:** Close fill price accuracy — fetch actual fill via `orderstatus()`, not trigger LTP (prevents P&L miscount)
- **FIX-V:** VIX spike absolute floor — dual condition (spike_pct >= 15% AND current_vix >= 18) blocks false exits
- **FIX-VI:** NSE VIX bootstrap rate-limiting — add 1.5s inter-chunk pause (prevent session drop on bulk download)

### v5.2.0 — Production-Grade Hardening
- **FIX-I:** Timezone safety — replace `date.today()` with `now_ist().date()` (UTC server fix)
- **FIX-II:** Thread safety — `Lock` → `RLock` for safe re-entrancy in `close_all()` → `close_one_leg()` chain
- **FIX-III:** NSE holiday handling — delegate to OpenAlgo scheduler
- **FIX-IV:** Fill capture retries — 3×1s → 5×(1–4s) linear back-off
- **FIX-V:** Config validation — `_validate_config()` at startup with 15+ checks, raises `ValueError`
- **FIX-VI:** SIGTERM handler — graceful shutdown on systemd/docker stop signal

### v5.1.0 — Second-Pass Audit Fixes
- **FIX-A:** Double `get_expiry()` call eliminated — expiry resolved once in `job_entry()`, passed to all callers
- **FIX-B:** No-tick edge case — detect and fetch live LTPs in 0–15s window between entry and first monitor tick

### v5.0.0 — Full Code Audit (from v4.2.1)
- **FIX-1:** Expiry logging spam — silent `_compute_expiry_date()` helper, log only on entry path
- **FIX-2:** Banner expiry O(n) calls — compute once, pass to DTE label helper
- **FIX-3:** `closeposition()` P&L missing — snapshot `open_mtm` before `_mark_fully_flat()`
- **FIX-8:** Single-leg close P&L — fetch LTP before `close_one_leg()` in single-leg path
- **FIX-9:** NSE VIX fallback resilience — explicit cookie-grab step + headers for NSE API
- **FIX-10:** `today_pnl` reset missing — add to reset block in `_mark_fully_flat()`

---

## Key Design Principles

1. **Per-leg independence:** Each leg has its own SL, P&L tracking, and closure. One leg closing never forces the other.
2. **Crash safety:** Atomic state saves after every mutation. Restart resumes from exact position and SL levels.
3. **API resilience:** Configurable fail-open / fail-closed per filter. Network errors never crash the monitor loop.
4. **Thread safety:** RLock prevents concurrent monitor ticks and double-close scenarios.
5. **AlgoTest compatibility:** DTE uses trading-day count (weekends excluded) — exact match with backtest methodology.
6. **Transparency:** Telegram alerts for every critical event including entry, each exit, errors, and reconciliation.
