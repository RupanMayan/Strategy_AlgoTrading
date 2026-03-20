# NIFTY Short Straddle — Partial Square Off Strategy

**Version 6.3.0** | OpenAlgo + Dhan API | Restart-Safe | Production Grade

---

## Table of Contents

1. [Strategy Overview](#1-strategy-overview)
2. [Architecture & Module Map](#2-architecture--module-map)
3. [Complete Lifecycle — Entry to Exit](#3-complete-lifecycle--entry-to-exit)
4. [Entry Filter Chain](#4-entry-filter-chain)
5. [Stop-Loss Priority Chain](#5-stop-loss-priority-chain)
6. [Partial Square Off Logic](#6-partial-square-off-logic)
7. [Breakeven SL — Context-Aware + Grace Period + Buffer](#7-breakeven-sl--context-aware--grace-period--buffer)
8. [Trailing Stop-Loss Engine](#8-trailing-stop-loss-engine)
9. [DTE-Aware SL Widening](#9-dte-aware-sl-widening)
10. [Dynamic Time-of-Day SL Tightening](#10-dynamic-time-of-day-sl-tightening)
11. [Combined Net-P&L Guard](#11-combined-net-pl-guard)
12. [Re-Entry After Early Close](#12-re-entry-after-early-close)
12A. [Momentum Filter (Re-Entry)](#12a-momentum-filter-re-entry)
12B. [Post-Partial Recovery Lock](#12b-post-partial-recovery-lock)
12C. [Asymmetric Leg Booking](#12c-asymmetric-leg-booking)
12D. [Combined Profit Trailing](#12d-combined-profit-trailing)
13. [Risk Controls Summary](#13-risk-controls-summary)
14. [Worked Examples with Sample Data](#14-worked-examples-with-sample-data)
15. [Edge Cases & Fail-Safe Behaviour](#15-edge-cases--fail-safe-behaviour)
16. [State Keys Reference](#16-state-keys-reference)
17. [Configuration Reference](#17-configuration-reference)
18. [Telegram Notifications](#18-telegram-notifications)

---

## 1. Strategy Overview

### What Is a Short Straddle?

A **short straddle** sells both an ATM Call (CE) and an ATM Put (PE) option on NIFTY
with the same strike and expiry. The strategy collects premium from both legs and
profits when NIFTY stays range-bound — the sold options decay toward zero via theta.

```
Profit zone:   NIFTY stays near the strike price
Max profit:    CE premium + PE premium collected (both expire worthless)
Risk:          Unlimited on either side if NIFTY moves sharply
```

### Why Partial Square Off?

Instead of closing both legs on any single SL hit, each leg has its **own independent SL**.
When one leg's SL fires, only that leg closes. The surviving leg continues running
with its own SL — capturing additional theta decay from the winning side.

```
Traditional straddle:  PE SL hit → close BOTH CE and PE
Partial square off:    PE SL hit → close PE only → CE continues with its own SL
```

### Key Parameters (Default Config)

| Parameter | Value | Description |
|-----------|-------|-------------|
| Underlying | NIFTY | NSE NIFTY 50 Index |
| Lot size | 65 | Contracts per lot |
| Product | MIS | Intraday (auto square-off by exchange) |
| Strike | ATM | At-the-money nearest to NIFTY spot |
| Expiry | Weekly Tuesday | Auto-resolved each day |
| Base SL | 20% | Per-leg stop-loss (entry × 1.20) |
| Daily target | Rs.5,000/lot | Close all when combined P&L reaches this |
| Daily limit | Rs.-4,000/lot | Close all when combined loss exceeds this |
| Monitor interval | 15 seconds | How often SL/P&L is checked |

---

## 2. Architecture & Module Map

```
main.py                     ← Entry point: creates StrategyCore and calls .run()
│
├── src/
│   ├── __init__.py          ← Package init (exports BrokerClient, MonitorState)
│   ├── _shared.py           ← Shared constants, lazy broker client, SL helpers
│   ├── strategy_core.py     ← Top-level orchestrator (scheduler, jobs, banner)
│   ├── filters.py           ← Entry filter chain (DTE, VIX, IVR/IVP, ORB)
│   ├── risk.py              ← MarginGuard + TrailingSLEngine
│   ├── order_engine.py      ← All broker order operations (entry, close, P&L)
│   ├── monitor.py           ← Intraday SL/P&L monitor (runs every 15s)
│   ├── reconciler.py        ← Startup state vs broker reconciliation
│   └── vix_manager.py       ← VIX fetch, IVR/IVP computation, history mgmt
│
├── util/
│   ├── __init__.py          ← Package init
│   ├── config_util.py       ← TOML config loader + validation + Config dataclass
│   ├── logger.py            ← IST-aware structured logging with rotation
│   ├── notifier.py          ← Telegram notifications (background daemon thread)
│   └── state.py             ← Atomic JSON state persistence (crash-safe)
│
└── config.toml              ← All user-configurable parameters
```

### Dependency Flow

```
StrategyCore
├── VIXManager         (VIX fetch, IVR/IVP)
├── FilterEngine       (entry gates) ← uses VIXManager
├── MarginGuard        (pre-trade margin check)
├── TrailingSLEngine   (per-leg trailing SL state machine)
├── OrderEngine        (broker operations) ← uses TrailingSLEngine
├── Monitor            (intraday SL/P&L) ← uses OrderEngine, TrailingSLEngine, VIXManager
└── StartupReconciler  (state recovery) ← uses OrderEngine
```

---

## 3. Complete Lifecycle — Entry to Exit

### Timeline of a Typical Trading Day

```
09:00  Strategy starts → validate config → print banner → test connection
       → reconcile state with broker → check VIX history

09:17  ORB CAPTURE: Fetch NIFTY spot price as opening reference
       Store in state["orb_price"]

09:30  ENTRY JOB fires (DTE0/DTE1 time)
09:35  ENTRY JOB fires (DTE2 time) ← for Friday
09:40  ENTRY JOB fires (DTE3 time)
09:45  ENTRY JOB fires (DTE4 time)

       For the matching DTE entry time:
       ┌─ Filter chain runs (7 gates)
       ├─ If ALL pass → place_entry() → SELL CE + SELL PE
       ├─ Fill capture in background thread
       └─ Monitor starts on next 15s tick

09:35+ MONITOR LOOP (every 15 seconds):
       ┌─ Fetch LTP for each active leg
       ├─ Update trailing SL (if applicable)
       ├─ Check per-leg SL (fixed/dynamic/trailing/breakeven)
       ├─ Check combined premium decay exit
       ├─ Check asymmetric leg booking [v6.3.0]
       ├─ Check combined profit trailing [v6.3.0]
       ├─ Check winner-leg early booking
       ├─ Update combined P&L
       ├─ Check post-partial recovery lock [v6.3.0]
       ├─ Check VIX spike (throttled, every 5 min)
       ├─ Check spot-move breach (throttled, every 60s)
       ├─ Check daily profit target
       └─ Check daily loss limit

15:15  HARD EXIT: Close ALL remaining active legs
       Log trade → reset state → delete state file

15:30  VIX HISTORY UPDATE: Append today's closing VIX to vix_history.csv
```

### State Transitions

```
                    ┌──────────────┐
                    │   NO POSITION │ ←──────────────────────────────┐
                    │ in_position=F │                                 │
                    └──────┬───────┘                                 │
                           │ place_entry()                           │
                           ▼                                         │
                    ┌──────────────┐                                 │
                    │ BOTH LEGS    │                                  │
                    │ ACTIVE       │                                  │
                    │ ce_active=T  │                                  │
                    │ pe_active=T  │                                  │
                    └──────┬───────┘                                 │
                           │ One leg SL hit                          │
                           ▼                                         │
                    ┌──────────────┐                                 │
                    │ ONE LEG      │                                  │
                    │ ACTIVE       │──── SL/target/limit/exit ──────┘
                    │ (partial)    │
                    └──────────────┘
```

---

## 4. Entry Filter Chain

The entry job runs a **sequential filter chain** — short-circuits on the first failure.

```
┌─────────────────────────────────────────────────────────┐
│  FILTER CHAIN (executed in order)                        │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  0. DTE-AWARE ENTRY TIME GUARD                           │
│     ├─ USE_DTE_ENTRY_MAP = true                          │
│     ├─ Get current DTE (trading days to expiry)          │
│     ├─ Look up effective entry time for this DTE         │
│     └─ Skip if current HH:MM ≠ effective entry time     │
│                                                          │
│  1. DUPLICATE GUARD                                      │
│     └─ Skip if already in_position                       │
│                                                          │
│  1a. RE-ENTRY GUARD (new in v6.0.0)                      │
│     ├─ Is this the first trade of the day? → proceed     │
│     ├─ REENTRY_ENABLED = false? → skip                   │
│     ├─ reentry_count >= MAX_PER_DAY? → skip              │
│     ├─ Cooldown elapsed? (≥ 30 min since last close)     │
│     └─ Previous loss ≤ MAX_LOSS_FOR_REENTRY?             │
│                                                          │
│  2. DTE FILTER + MONTH FILTER                            │
│     ├─ Today's DTE in TRADE_DTE list? [0,1,2,3,4]       │
│     ├─ Weekend guard (Sat/Sun → skip)                    │
│     └─ Current month in SKIP_MONTHS? [11=November]       │
│                                                          │
│  3. VIX FILTER                                           │
│     ├─ Fetch India VIX from OpenAlgo                     │
│     ├─ VIX < 14.0 → skip (premiums too thin)            │
│     ├─ VIX > 28.0 → skip (danger zone)                  │
│     └─ Store vix_at_entry in state                       │
│                                                          │
│  4. IVR / IVP FILTER                                     │
│     ├─ Load 252 days of VIX history from CSV             │
│     ├─ IVR = (VIX - 52wk_Low) / (52wk_High - 52wk_Low) │
│     ├─ IVP = % of days with lower VIX than today        │
│     ├─ IVR < 30.0 → skip (IV not rich enough)           │
│     ├─ IVP < 40.0% → skip (IV below 40th percentile)    │
│     └─ fail_open = false → skip if history unavailable   │
│                                                          │
│  5. OPENING RANGE (ORB) FILTER                           │
│     ├─ Fetch current NIFTY spot                          │
│     ├─ Compare to ORB reference (captured at 09:17)      │
│     ├─ move_pct = |spot - orb_price| / orb_price × 100  │
│     └─ move_pct > 0.5% → skip (directional gap)         │
│                                                          │
│  5B. MOMENTUM FILTER (RE-ENTRY ONLY) [FIX-XXVI v6.3.0]  │
│     ├─ Only applies to re-entries (not first trade)      │
│     ├─ drift = |current_spot - orb_ref| / orb_ref × 100 │
│     └─ drift > 0.5% → block (market trending)           │
│                                                          │
│  6. MARGIN GUARD                                         │
│     ├─ Fetch available cash + collateral via funds()     │
│     ├─ Fetch basket margin for CE+PE SELL MIS            │
│     ├─ Check: available >= required × 1.20 (20% buffer) │
│     └─ Insufficient → skip trade                         │
│                                                          │
│  7. PLACE ENTRY                                          │
│     ├─ Reset daily counters                              │
│     ├─ Store current_dte in state (for DTE SL override)  │
│     ├─ optionsmultiorder(SELL CE + SELL PE)              │
│     ├─ Background fill capture thread                    │
│     └─ Monitor starts on next tick                       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Sample: Filter Chain Execution (from actual log)

```
[09:35:00] DTE: 2  (today: Friday, expiry: 24MAR26 Tuesday)
[09:35:00] DTE filter OK: DTE2 (Friday) | month: March 2026
[09:35:00] India VIX (OpenAlgo): 22.00
[09:35:00] VIX 22.00 within [14.0–28.0] — OK to trade
[09:35:00] IVR: 90.4  (threshold: >= 30.0) → PASS ✓
[09:35:00] IVP: 98.4% (threshold: >= 40.0%) → PASS ✓
[09:35:03] ORB filter: NIFTY ↑ 0.28% from ORB Rs.23250.70 ≤ 0.5% — OK
[09:35:04] MARGIN CHECK: PASS ✓  Available Rs.495,148 | Required Rs.348,477
[09:35:11] ENTRY COMPLETE
           CE: NIFTY24MAR2623300CE  fill Rs.255.35  SL @ Rs.319.19
           PE: NIFTY24MAR2623300PE  fill Rs.230.55  SL @ Rs.288.19
```

> Note: With DTE2 SL override at 25%, SL levels are now wider than the old 20%.

---

## 5. Stop-Loss Priority Chain

Every 15 seconds, the monitor evaluates the effective SL for each active leg.
The SL system has a **strict priority order** — the highest-priority active SL wins.

```
┌────────────────────────────────────────────────────────────┐
│  sl_level(leg) — Effective SL Priority Chain                │
├────────────────────────────────────────────────────────────┤
│                                                             │
│  PRIORITY 1: TRAILING SL (highest)                          │
│  ├─ Condition: TRAILING_SL_ENABLED = true                   │
│  │             AND trailing_active_{leg} = true              │
│  │             AND trailing_sl_{leg} > 0                     │
│  └─ Returns: trailing_sl_{leg}                               │
│     (only moves DOWN — tighter over time)                    │
│                                                             │
│  PRIORITY 2: BREAKEVEN SL (after grace period)              │
│  ├─ Condition: BREAKEVEN_AFTER_PARTIAL_ENABLED = true       │
│  │             AND breakeven_active_{leg} = true             │
│  │             AND breakeven_sl_{leg} > 0                    │
│  │             AND breakeven_sl < fixed_sl (tighter)         │
│  │             AND grace period has ELAPSED (≥ 5 min)        │
│  └─ Returns: breakeven_sl_{leg} (with buffer applied)        │
│                                                             │
│  PRIORITY 3: FIXED / DYNAMIC SL (lowest — default)         │
│  ├─ Base SL % = DTE_SL_OVERRIDE[current_dte] or LEG_SL_PCT │
│  ├─ Dynamic = min(time_schedule_pct, base)                   │
│  └─ Returns: entry_price × (1 + dynamic_sl_pct / 100)       │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

### SL Calculation Example

```
Entry: CE sold at Rs.255.35 on DTE2 (Friday)

FIXED SL (morning, before 12:00):
  base_sl_pct = DTE_SL_OVERRIDE[2] = 25%     ← DTE-aware widening
  dynamic_sl_pct = min(schedule, base) = 25%  ← no time entry before 12:00
  SL = 255.35 × 1.25 = Rs.319.19

FIXED SL (after 12:00):
  time_schedule → 15% (from dynamic_sl schedule)
  dynamic_sl_pct = min(15%, 25%) = 15%
  SL = 255.35 × 1.15 = Rs.293.65

FIXED SL (after 14:30):
  time_schedule → 7%
  dynamic_sl_pct = min(7%, 25%) = 7%
  SL = 255.35 × 1.07 = Rs.273.22

TRAILING SL (when CE decays to Rs.127.68 = 50% of entry):
  trailing_sl = 127.68 × 1.15 = Rs.146.83    ← lock_pct = 15%
  Guaranteed profit = 255.35 - 146.83 = Rs.108.52/unit
```

---

## 6. Partial Square Off Logic

### How It Works

```
                    BOTH LEGS ACTIVE
                    CE: Rs.255.35  SL @ Rs.319.19
                    PE: Rs.230.55  SL @ Rs.288.19
                           │
                    NIFTY drops 80 points
                           │
                           ▼
              ┌────────────────────────────┐
              │ PE rises to Rs.289.00      │
              │ PE SL Rs.288.19 → HIT!     │
              │                            │
              │ close_one_leg("PE")         │
              │ PE realised P&L: Rs.-3799   │
              └────────────┬───────────────┘
                           │
                           ▼
              ┌────────────────────────────┐
              │ CE STILL ACTIVE             │
              │ CE entry: Rs.255.35         │
              │ CE LTP: Rs.200.00 (profit)  │
              │                            │
              │ Breakeven SL activated:     │
              │   raw_be = 255.35 + (-3799  │
              │            / 65) = Rs.196.89│
              │   buffered = 196.89 × 1.10  │
              │           = Rs.216.58       │
              │                            │
              │ Grace period: 5 minutes     │
              │ CE uses fixed SL until then │
              └────────────────────────────┘
                           │
              After 5 minutes grace period:
                           │
                           ▼
              ┌────────────────────────────┐
              │ CE SL = Rs.216.58 (BE+buf) │
              │ CE LTP must RISE above     │
              │ Rs.216.58 to trigger exit   │
              │                            │
              │ If CE keeps decaying → OK   │
              │ If CE reverses → closes at  │
              │   ~Rs.216, net loss ~Rs.0   │
              └────────────────────────────┘
```

### State Changes During Partial Exit

```python
# BEFORE partial close (PE SL hit):
state["in_position"]  = True
state["ce_active"]    = True
state["pe_active"]    = True
state["closed_pnl"]   = 0.0

# AFTER close_one_leg("PE"):
state["pe_active"]    = False                    # PE closed
state["closed_pnl"]   = -3799.0                  # PE loss realised
state["exit_price_pe"] = 289.00                  # Actual fill price
state["breakeven_active_ce"] = True              # Breakeven armed
state["breakeven_sl_ce"]     = 216.58            # With 10% buffer
state["breakeven_activated_at_ce"] = "2026-03-20T09:46:20+05:30"
state["in_position"]  = True                     # CE still running
state["ce_active"]    = True
```

---

## 7. Breakeven SL — Context-Aware + Grace Period + Buffer

### Context-Aware Activation (FIX-XXIV, v6.2.0)

Before checking grace period or buffer, the breakeven SL now checks whether the
surviving leg is **winning or losing** at the moment of partial exit:

```
PE SL hit at loss → close_one_leg("PE") → inspect surviving CE:

  CE LTP < CE entry?  →  CE is WINNING (profitable for short)
    ├─ SKIP breakeven SL entirely
    ├─ CE continues with trailing SL / fixed-dynamic SL / winner booking
    └─ Reason: breakeven price is BELOW winning LTP → would kill profit

  CE LTP >= CE entry?  →  CE is LOSING (losing for short)
    ├─ ACTIVATE breakeven SL (with grace + buffer)
    └─ Reason: protects against combined position loss exceeding breakeven
```

This prevents the v5.9.0 scenario where breakeven SL immediately killed a
profitable surviving leg after partial exit.

### The Problem (v5.9.0)

In v5.9.0, the breakeven SL fired **immediately** after a partial close because:

```
PE closed at Rs.278.80 → loss Rs.-3136
Breakeven SL for CE = 255.35 + (-3136 / 65) = Rs.207.10

CE LTP was Rs.215.40 → check: 215.40 >= 207.10 → TRUE → CE CLOSED!

Result: Both legs closed in 12 seconds. Net loss Rs.-517.
The partial square-off feature was completely bypassed.
```

### The Fix (v6.0.0)

Two new safeguards prevent instant triggering:

#### A) Grace Period (5 minutes)

```
breakeven_grace_period_min = 5

Timeline:
  09:46:20  PE SL hit → breakeven activated for CE
  09:46:20  CE uses FIXED/DYNAMIC SL (not breakeven) ← grace active
  09:47:00  CE still uses fixed SL                    ← grace active
  09:48:00  CE still uses fixed SL                    ← grace active
  09:51:20  Grace period expires → breakeven SL arms
  09:51:20  CE LTP checked against breakeven SL Rs.227.70

  During the 5-minute grace, CE may have decayed from Rs.215 → Rs.190
  making the breakeven SL irrelevant (net P&L already positive).
```

#### B) Buffer (10% above mathematical breakeven)

```
breakeven_buffer_pct = 10.0

Without buffer:  be_sl = Rs.207.10  (fires when CE >= 207.10)
With 10% buffer: be_sl = Rs.207.10 × 1.10 = Rs.227.70

CE must bounce from Rs.215 all the way to Rs.227.70 to trigger.
This gives substantial breathing room for normal intraday noise.
```

#### How sl_level() Evaluates During Grace Period

```python
# During grace period (first 5 min after breakeven activation):
sl_level("CE") returns:
    → Fixed/dynamic SL (e.g., Rs.273.22 at 7% dynamic)
    → breakeven_sl is IGNORED

# After grace period:
sl_level("CE") returns:
    → Rs.227.70 (breakeven + buffer)
    → Only if 227.70 < fixed_sl (it's tighter = better protection)
```

---

## 8. Trailing Stop-Loss Engine

### Two-Phase State Machine (per leg, independently)

```
PHASE 1 — ACTIVATION (fires once per leg per trade)
═══════════════════════════════════════════════════

Condition: LTP ≤ entry_price × (trigger_pct / 100)
           i.e., LTP ≤ 255.35 × 0.50 = Rs.127.68

Action:
  initial_trail_sl = LTP × (1 + lock_pct / 100)
                   = 127.68 × 1.15 = Rs.146.83
  Safety cap: if trail_sl >= fixed_sl → cap at fixed_sl
  Save: trailing_active_ce = True
        trailing_sl_ce     = 146.83


PHASE 2 — TIGHTENING (fires every tick while trailing active)
══════════════════════════════════════════════════════════════

Condition: new_trail_sl < current_trail_sl

  Tick 1: LTP Rs.100 → new_sl = 100 × 1.15 = Rs.115.00 (< 146.83 → UPDATE)
  Tick 2: LTP Rs.80  → new_sl = 80 × 1.15  = Rs.92.00  (< 115.00 → UPDATE)
  Tick 3: LTP Rs.90  → new_sl = 90 × 1.15  = Rs.103.50 (> 92.00  → SKIP)
  Tick 4: LTP Rs.93  → 93 >= 92.00 → SL HIT → close leg

SL only moves DOWN (tighter) — never moves up (wider).
```

### Walk-Through Example

```
CE entry: Rs.255.35 | trigger_pct: 50% | lock_pct: 15%
Fixed SL: Rs.319.19 (25% on DTE2)

Time     LTP       Trailing SL   Status        Locked Profit/unit
─────    ────────  ────────────  ──────────    ─────────────────
09:45    Rs.240    -             Not triggered  -
10:15    Rs.200    -             Not triggered  -
10:45    Rs.150    -             Not triggered  -
11:00    Rs.127    Rs.146.05     ACTIVATED!     Rs.109.30
11:15    Rs.110    Rs.126.50     Tightened      Rs.128.85
11:30    Rs.90     Rs.103.50     Tightened      Rs.151.85
11:45    Rs.70     Rs.80.50      Tightened      Rs.174.85
12:00    Rs.50     Rs.57.50      Tightened      Rs.197.85
12:15    Rs.40     Rs.46.00      Tightened      Rs.209.35
12:30    Rs.50     Rs.46.00      (no change)    Rs.209.35
12:35    Rs.46     Rs.46.00      SL HIT         Rs.209.35
         EXIT at Rs.46 → profit = (255.35 - 46) × 65 = Rs.13,608
```

---

## 9. DTE-Aware SL Widening

### Why Different SL Per DTE?

On DTE2+ days (Friday/Thursday/Wednesday), option premiums are thinner and
morning volatility can easily breach a tight SL. A 20% move on a Rs.230
premium is only Rs.46 — a 50-point NIFTY move can trigger this in minutes.

### Configuration

```toml
[risk]
leg_sl_percent = 20.0     # Default base SL for DTE0/DTE1

[risk.dte_sl_override]
"2" = 25.0    # DTE2 = Friday  (wider — moderate premium)
"3" = 28.0    # DTE3 = Thursday (wider — low premium)
"4" = 30.0    # DTE4 = Wednesday (widest — thin premium)
```

### How It Works

```python
def _base_sl_percent():
    # 1. Check DTE override
    dte = state.get("current_dte")
    if dte in cfg.DTE_SL_OVERRIDE:
        return DTE_SL_OVERRIDE[dte]   # e.g., 25% for DTE2
    # 2. Fall back to default
    return LEG_SL_PERCENT             # 20%
```

### SL Levels Per DTE (CE entry Rs.255.35, PE entry Rs.230.55)

| DTE | Day | Base SL% | CE SL | PE SL |
|-----|-----|----------|-------|-------|
| 0 | Tuesday | 20% | Rs.306.42 | Rs.276.66 |
| 1 | Monday | 20% | Rs.306.42 | Rs.276.66 |
| 2 | Friday | **25%** | **Rs.319.19** | **Rs.288.19** |
| 3 | Thursday | **28%** | **Rs.326.85** | **Rs.295.10** |
| 4 | Wednesday | **30%** | **Rs.331.96** | **Rs.299.72** |

> On Friday (DTE2), the PE SL would be Rs.288.19 instead of Rs.276.66.
> PE peaked at Rs.278.50 in the log — it would NOT have hit the wider SL!

---

## 10. Dynamic Time-of-Day SL Tightening

As the day progresses and theta has been captured, the SL tightens to protect gains.

### Schedule

```
Before 12:00  → base SL (20% or DTE override)
After 12:00   → 15%
After 13:30   → 10%
After 14:30   → 7%
```

### Interaction with DTE Override

The dynamic schedule uses `min(schedule_pct, base_pct)` — it can only **tighten**,
never widen beyond the DTE-aware base.

```
DTE2 (Friday):
  09:35  base=25%  dynamic=N/A  → effective 25%
  12:01  base=25%  dynamic=15%  → effective min(15, 25) = 15%
  13:31  base=25%  dynamic=10%  → effective 10%
  14:31  base=25%  dynamic=7%   → effective 7%

DTE0 (Tuesday):
  09:30  base=20%  dynamic=N/A  → effective 20%
  12:01  base=20%  dynamic=15%  → effective 15%
  14:31  base=20%  dynamic=7%   → effective 7%
```

---

## 11. Combined Net-P&L Guard

### The Problem

After a partial close, the surviving leg may hit its fixed SL on a momentary bounce
even though the **combined** position (closed P&L + open MTM) is still profitable.

### The Fix

Before executing a per-leg SL close, the monitor checks the net P&L:

```python
# In monitor.run_tick(), when per-leg SL fires:
if (
    not is_trailing          # Don't skip trailing SL hits
    and not is_breakeven     # Don't skip breakeven SL hits
    and closed_pnl != 0      # Only in partial mode (one leg already closed)
):
    net_pnl = closed_pnl + leg_mtm
    if net_pnl > 0:
        # Net position is profitable → defer SL
        # Daily loss limit will catch genuine deterioration
        continue
```

### Example

```
PE closed at loss: closed_pnl = Rs.-3136
CE LTP Rs.210: CE MTM = (255.35 - 210) × 65 = Rs.2947
Net P&L = -3136 + 2947 = Rs.-189

CE fixed SL fires (LTP Rs.210 hit some threshold)
BUT net P&L is Rs.-189 (not positive) → SL proceeds normally

Alternative scenario:
CE LTP Rs.195: CE MTM = (255.35 - 195) × 65 = Rs.3923
Net P&L = -3136 + 3923 = Rs.+787  ← POSITIVE

CE fixed SL fires → but net P&L > 0 → SL DEFERRED
CE continues running. Daily loss limit (Rs.-4000) protects downside.
```

### When the Guard Does NOT Apply

| Scenario | Guard Active? | Why |
|----------|--------------|-----|
| Trailing SL hit | NO | Trailing is already locking profit |
| Breakeven SL hit | NO | Breakeven is a protection mechanism |
| First SL hit (both legs active) | NO | closed_pnl = 0 (not partial mode) |
| Daily loss limit | NO | This is a global circuit breaker |

---

## 12. Re-Entry After Early Close

### The Problem

When the position closes early with a small loss (e.g., Rs.-517 at 09:46),
the strategy sits idle for ~6 hours. If the early close was caused by transient
morning volatility, re-entering after stabilisation can recover the loss.

### Configuration

```toml
[risk.reentry]
enabled               = true
cooldown_min          = 30      # Wait 30 min after close
max_loss_per_lot      = 2000    # Per-lot Rs. threshold (effective = per_lot × number_of_lots)
max_reentries_per_day = 1       # Maximum 1 re-entry per day
```

### How It Works

```
09:46  Position closes: P&L = Rs.-517
       state["last_close_time"] = "09:46:33"
       state["last_trade_pnl"]  = -517

10:16  Next entry job fires (cooldown elapsed: 30 min ✓)
       ├─ Is re-entry enabled? → YES
       ├─ Reentry count today: 0 < max 1 → OK
       ├─ Cooldown: 30 min elapsed ≥ 30 min required → OK
       ├─ Previous loss: Rs.517 ≤ Rs.2000 max → OK
       ├─ Run FULL filter chain (VIX, IVR/IVP, ORB, margin)
       │   └─ If any filter fails → NO re-entry
       └─ All pass → place_entry() → new straddle
           state["reentry_count_today"] = 1
```

### Re-Entry Decision Tree

```
                Is first trade of the day?
                     │
              YES ───┤─── NO
              │      │      │
              │      │      ├─ REENTRY_ENABLED = false? → SKIP
              │      │      ├─ reentry_count >= MAX_PER_DAY? → SKIP
              │      │      ├─ Cooldown not elapsed? → SKIP
              │      │      ├─ Previous loss > MAX_LOSS? → SKIP
              │      │      └─ All checks pass → RUN FILTER CHAIN
              │      │
              └──────┴──→ Run filter chain → place_entry()
```

### Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Loss Rs.-3500 (> Rs.2000 threshold) | No re-entry — loss too large |
| First trade was a profit exit (target) | `last_trade_pnl > 0` → re-entry allowed (loss check passes) |
| VIX spiked after first trade | VIX filter blocks re-entry |
| NIFTY gapped after first close | ORB filter blocks re-entry |
| Two re-entries in one day | MAX_PER_DAY=1 blocks the second |
| Script restarted mid-day | `last_close_time` is in state file → cooldown works |

---

## 13. Risk Controls Summary

### Layered Defence Model

```
LAYER 1 — ENTRY GATES (prevent bad trades)
├─ DTE filter       : trade only on configured DTE days
├─ Month filter     : skip November (historically worst month)
├─ VIX filter       : VIX must be 14–28 (not too thin, not dangerous)
├─ IVR/IVP filter   : IV must be historically rich (IV-crush tailwind)
├─ ORB filter       : skip if NIFTY gapped > 0.5% at open
├─ Momentum filter  : block re-entry when NIFTY trending > 0.5% [v6.3.0]
└─ Margin guard     : sufficient funds with 20% buffer

LAYER 2 — PER-LEG PROTECTION (limit individual leg losses)
├─ Fixed SL         : 20–30% based on DTE (entry × 1.20–1.30)
├─ Dynamic SL       : tightens through the day (15% → 10% → 7%)
├─ Trailing SL      : locks profit when leg decays to 50% of entry
└─ Breakeven SL     : context-aware — only on losing survivor [v6.2.0]

LAYER 3 — POSITION-LEVEL PROTECTION (limit overall exposure)
├─ Combined decay   : exit when both legs decayed 60% combined
├─ Asymmetric book  : book deeply decayed winner when legs diverge [v6.3.0]
├─ Combined trail   : exit if combined decay retraces from peak [v6.3.0]
├─ Winner booking   : book deeply profitable surviving leg (< 30% of entry)
├─ Recovery lock    : trail combined P&L recovery after partial exit [v6.3.0]
├─ Net P&L guard    : defer SL when combined P&L is positive
├─ Daily target     : close all at Rs.5000 profit
└─ Daily loss limit : close all at Rs.-4000 loss

LAYER 4 — SYSTEMIC PROTECTION (market-wide events)
├─ VIX spike exit   : close all if VIX rises 15%+ from entry
├─ Spot-move exit   : close all if NIFTY moves beyond breakeven
├─ Hard exit        : close all at 15:15 IST regardless
├─ Emergency close  : close on script crash or SIGTERM
└─ Startup reconcile: detect and handle orphan positions
```

---

## 14. Worked Examples with Sample Data

### Example 1: Perfect Day (Both Legs Expire Worthless-ish)

```
Day: Tuesday (DTE0) | VIX: 18 | IVR: 45 | NIFTY: 23,300

09:30  ENTRY
       CE: NIFTY24MAR2623300CE  sell @ Rs.180.00  SL @ Rs.216.00 (20%)
       PE: NIFTY24MAR2623300PE  sell @ Rs.170.00  SL @ Rs.204.00 (20%)
       Combined premium: Rs.350/unit = Rs.22,750 total (65 qty)

10:00  CE Rs.160, PE Rs.155 → Open MTM = (180-160 + 170-155) × 65 = Rs.2,275
11:00  CE Rs.120, PE Rs.110 → Open MTM = (180-120 + 170-110) × 65 = Rs.7,800
       Combined decay = (1 - 230/350) × 100 = 34.3% → not yet 60%

12:00  Dynamic SL tightens to 15%
       CE SL: 180 × 1.15 = Rs.207.00
       PE SL: 170 × 1.15 = Rs.195.50

12:30  CE Rs.90 → trailing SL ACTIVATES (90 ≤ 180 × 0.50)
       CE trail_sl = 90 × 1.15 = Rs.103.50
       Locked profit: (180 - 103.50) × 65 = Rs.4,972

13:00  CE Rs.60, PE Rs.55 → Combined decay = (1 - 115/350) × 100 = 67.1%
       ≥ 60% → COMBINED DECAY EXIT fires!
       Close both legs: P&L = (180-60 + 170-55) × 65 = Rs.15,275

       ✅ Trade P&L: +Rs.15,275 | Held: 3h 30m | Exit: Combined Decay
```

### Example 2: One Leg SL Hit, Surviving Leg Profits (Partial Success)

```
Day: Friday (DTE2) | VIX: 22 | IVR: 90 | NIFTY: 23,300

09:35  ENTRY
       CE: sell @ Rs.255.35  SL @ Rs.319.19 (25% DTE2 override)
       PE: sell @ Rs.230.55  SL @ Rs.288.19 (25% DTE2 override)

10:05  NIFTY drops 120 points to 23,180
       CE Rs.180 (profit Rs.4,897)  |  PE Rs.295 (loss)
       PE LTP Rs.295 >= SL Rs.288.19 → PE SL HIT

       close_one_leg("PE"):
         PE fill at Rs.295.50
         PE realised P&L: (230.55 - 295.50) × 65 = Rs.-4,222
         closed_pnl = Rs.-4,222

       Breakeven SL for CE:
         raw_be = 255.35 + (-4222 / 65) = Rs.190.38
         buffered = 190.38 × 1.10 = Rs.209.42
         Grace: 5 minutes (arms at ~10:10)

10:05–10:10  CE uses fixed SL Rs.319.19 (grace period)
             CE decays: Rs.180 → Rs.160 → Rs.140

10:10  Grace expires. CE SL = Rs.209.42 (breakeven + buffer)
       CE LTP Rs.140 — well below Rs.209.42 → NO trigger

11:00  CE LTP Rs.90 → trailing ACTIVATES
       trail_sl = 90 × 1.15 = Rs.103.50
       (trailing > breakeven priority → trailing SL used)

12:00  CE LTP Rs.50  → trail_sl = 57.50
13:00  CE LTP Rs.30  → trail_sl = 34.50
13:30  CE bounces to Rs.35 → still < 34.50 → NO HIT
14:00  CE LTP Rs.25  → trail_sl = 28.75
14:30  CE LTP Rs.15  → trail_sl = 17.25
15:00  CE LTP Rs.18  → 18 >= 17.25 → TRAILING SL HIT

       close_one_leg("CE"):
         CE fill at Rs.18.20
         CE realised P&L: (255.35 - 18.20) × 65 = Rs.15,415

       Final P&L = -4,222 + 15,415 = Rs.+11,193

       ✅ Trade P&L: +Rs.11,193 | Despite PE SL hit!
```

### Example 3: Both Legs SL Hit (Bad Day — Mitigated by Wider DTE SL)

```
Day: Wednesday (DTE4) | VIX: 25 | NIFTY: 23,300

09:45  ENTRY
       CE: sell @ Rs.120.00  SL @ Rs.156.00 (30% DTE4 override)
       PE: sell @ Rs.115.00  SL @ Rs.149.50 (30% DTE4 override)

10:30  NIFTY rallies 200 points (unexpected event)
       CE Rs.160 → LTP Rs.160 >= SL Rs.156 → CE SL HIT
       close_one_leg("CE"):
         CE P&L: (120 - 160) × 65 = Rs.-2,600
         Breakeven SL for PE: raw = 115 + (-2600/65) = Rs.75.00
         buffered = 75 × 1.10 = Rs.82.50

10:30–10:35  PE uses fixed SL Rs.149.50 (grace)
10:35        Grace expires. PE SL = Rs.82.50

11:00  NIFTY stabilises. PE Rs.70 (profitable)
       Net P&L = -2600 + (115-70) × 65 = -2600 + 2925 = Rs.+325

12:00  NIFTY reverses back down 100 points
       PE rises from Rs.70 → Rs.85
       PE LTP Rs.85 >= breakeven SL Rs.82.50 → BREAKEVEN SL HIT

       close_one_leg("PE"):
         PE P&L: (115 - 85) × 65 = Rs.+1,950

       Final P&L = -2,600 + 1,950 = Rs.-650

       ⚠️ Trade P&L: -Rs.650 | Capped loss via breakeven SL
       Without breakeven: PE could have run to Rs.149.50 → loss Rs.-4,843
```

### Example 4: Re-Entry Recovers Early Loss

```
Day: Friday (DTE2) | VIX: 20

09:35  First entry
       CE: sell @ Rs.200  |  PE: sell @ Rs.190
       NIFTY gaps down → PE hits 25% SL at Rs.237.50 → loss Rs.-3,088

09:40  Breakeven + grace → CE closes at Rs.180
       CE P&L: (200-180) × 65 = Rs.+1,300
       Final P&L: -3,088 + 1,300 = Rs.-1,788

       state["last_close_time"] = "09:40"
       state["last_trade_pnl"]  = -1788

10:10  Entry job fires. Re-entry check:
       ├─ REENTRY_ENABLED = true ✓
       ├─ reentry_count = 0 < max 1 ✓
       ├─ Cooldown: 30 min elapsed ✓
       ├─ Loss Rs.1,788 ≤ Rs.2,000 max ✓
       └─ Filter chain: VIX OK, IVR OK, ORB OK, margin OK ✓

10:10  RE-ENTRY #1
       CE: sell @ Rs.150  |  PE: sell @ Rs.145
       NIFTY stays flat → both legs decay

14:30  Combined decay 65% → COMBINED DECAY EXIT
       CE Rs.50, PE Rs.55 → P&L: (150-50 + 145-55) × 65 = Rs.12,350

       Day total: -1,788 + 12,350 = Rs.+10,562

       ✅ Recovery! Without re-entry: day ends at Rs.-1,788
```

---

## 15. Edge Cases & Fail-Safe Behaviour

### Broker API Failures

| Scenario | Behaviour | Fail Mode |
|----------|-----------|-----------|
| funds() API fails | Margin guard: skip or allow trade | `fail_open=true` → allow |
| margin() API fails | Same as above | `fail_open=true` → allow |
| VIX fetch fails | VIX spike check skipped | Non-event (no close) |
| LTP fetch fails for a leg | SL check skipped for that leg | Non-event |
| LTP fails for ALL legs | Counter increments; alert at 3 ticks (~45s) | Telegram alert |
| placeorder() fails | Error logged + Telegram alert | Position unchanged |
| closeposition() fails | Falls back to per-leg close_one_leg() | Best effort |
| orderstatus() fails | Uses LTP as fill approximation | Slightly inaccurate P&L |

### State Persistence & Crash Recovery

```
SCENARIO: Script crashes mid-trade

1. State file exists with in_position=True
2. On restart: StartupReconciler runs
3. Cases:
   A. State file says position + broker confirms position
      → Resume monitoring (fills restored from state)
   B. State file says position + broker says NO position
      → External close detected → mark flat, log warning
   C. NO state file + broker has position
      → Orphan detected → emergency_close_all()
   D. NO state file + broker flat
      → Clean start
```

### Partial Entry Fill

```
SCENARIO: CE order fills but PE order fails

1. place_entry() detects partial fill
2. emergency_close_all() fires immediately
3. Attempts closeposition() for the filled CE leg
4. If emergency close fails: Telegram alert + "MANUAL ACTION REQUIRED"
5. Returns False — no position opened
```

### Monitor Lock Contention

```
SCENARIO: Previous monitor tick still running when next tick fires

1. Non-blocking lock attempt → fails
2. Skip counter increments
3. After 3 consecutive skips (~45s): Telegram alert
   "MONITOR BLOCKED — SL checks are PAUSED"
4. First successful tick: counter resets
```

### Weekend / Holiday Guard

```
SCENARIO: Script running on Saturday (somehow)

1. ORB capture: weekday >= 5 → skip
2. Entry job: DTE filter → weekend guard → skip
3. VIX update: weekday >= 5 → skip
4. Monitor: no position → nothing to do
```

### Breakeven SL Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Closed leg was profitable (winner booking) | `closed_pnl > 0` → breakeven NOT activated |
| Breakeven price > entry price | `0 < be_price < entry` check fails → NOT activated |
| Breakeven price ≤ 0 | Same check fails → NOT activated |
| Grace period timestamp unparseable | Fail-safe: apply breakeven immediately |
| Trailing SL activates during grace period | Trailing takes priority over breakeven |

### Re-Entry Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Script restarted after first trade | `last_close_time` in state → cooldown works |
| New day (entry_date ≠ today) | `reentry_count_today` resets to 0 |
| First trade was profit target hit | `last_trade_pnl > 0` → loss check passes |
| VIX spikes between trades | VIX filter blocks re-entry |
| No DTE-map entry time remaining | No entry job fires → no re-entry |
| NIFTY drifted >0.5% from ORB | Momentum filter blocks re-entry [v6.3.0] |

---

## 12A. Momentum Filter (Re-Entry) — FIX-XXVI, v6.3.0

### The Problem

After an early exit from a one-sided move (e.g., NIFTY drops 100 points, PE SL hit),
re-entering a fresh ATM straddle into the **same trend** is dangerous — the new losing
leg starts under pressure immediately.

### How It Works

On **re-entries only** (not first trade), checks NIFTY intraday drift from ORB reference:

```
drift_pct = |current_spot - orb_reference| / orb_reference × 100

If drift_pct > max_drift_pct (0.5%) → BLOCK re-entry
```

### Configuration

```toml
[filters.momentum]
enabled       = true
max_drift_pct = 0.5   # Block when NIFTY drifted > 0.5% (~115 points) from ORB
```

### Example

```
09:17  ORB capture: NIFTY = 23,300
09:35  First entry. NIFTY drops → PE SL hit → CE winner booked
10:05  Position fully closed. P&L = -1,500

10:35  Re-entry check:
       Current NIFTY: 23,150 (dropped 150 points from ORB)
       drift = |23150 - 23300| / 23300 × 100 = 0.64%
       0.64% > 0.5% → MOMENTUM FILTER BLOCKS RE-ENTRY
       Market is trending down — straddle is too risky
```

---

## 12B. Post-Partial Recovery Lock — FIX-XXV, v6.3.0

### The Problem

After FIX-XXIV lets the winning survivor continue running, the combined P&L
(negative `closed_pnl` + growing winner MTM) can cross from negative to positive —
the position has **recovered** from the SL loss. But there's no mechanism to lock
this recovery. A reversal can erase it.

### How It Works

```
After partial exit (closed_pnl < 0, one leg active):

1. Monitor tracks combined_pnl = closed_pnl + open_mtm each tick
2. When combined_pnl first crosses positive AND >= min threshold → ACTIVATE
   └─ min_recovery = 500 Rs/lot × number_of_lots
3. Track recovery_peak_pnl = max(previous peak, current combined_pnl)
4. If combined_pnl retraces trail_pct% from peak → EXIT
   └─ Locks remaining recovery profit
5. If combined_pnl drops back below zero → EXIT immediately
   └─ Recovery lost — stop bleeding
```

### Configuration

```toml
[risk.recovery_lock]
enabled                 = true
min_recovery_rs_per_lot = 500    # Min Rs.500/lot recovery before trail starts
trail_pct               = 50.0   # Exit if recovery retraces 50% from peak
```

### Example

```
09:46  PE SL hit → closed_pnl = -3,136
       CE at Rs.215 (winning) → open MTM = +2,619
       Combined = -517 (negative — recovery lock NOT active)

10:15  CE decays to Rs.180 → open MTM = +4,881
       Combined = +1,745 → POSITIVE → recovery lock ACTIVATES (>= Rs.500/lot)
       Peak = 1,745

10:45  CE decays to Rs.150 → open MTM = +6,848
       Combined = +3,712 → peak updated to 3,712

11:15  Market reverses, CE rises to Rs.200 → open MTM = +3,598
       Combined = +462 → retracement = (3712-462)/3712 = 87.6%
       87.6% > 50% trail → RECOVERY LOCK EXIT
       Locks Rs.462 profit instead of potentially giving back everything
```

---

## 12C. Asymmetric Leg Booking — FIX-XXVII, v6.3.0

### The Problem

When both legs are active but severely skewed (one deeply decayed, other barely moved),
the position is effectively a **naked short** on the non-decayed side. Combined
decay exit won't fire because total decay is below target.

### How It Works

```
Every monitor tick when BOTH legs are active:

  winner_pct = winner_ltp / winner_entry × 100
  loser_pct  = loser_ltp / loser_entry × 100

  If winner_pct <= 40% AND loser_pct >= 80%:
    → Book the deeply decayed winner
    → Surviving loser continues with normal SL management
```

### Configuration

```toml
[risk.asymmetric_booking]
enabled          = true
winner_decay_pct = 40.0   # Book when LTP <= 40% of entry (60%+ profit)
loser_intact_pct = 80.0   # Only if other leg >= 80% of entry
```

---

## 12D. Combined Profit Trailing — FIX-XXVIII, v6.3.0

### The Problem

When both legs are actively decaying and the combined position is profitable,
a sudden spike on one side can erase combined gains before the combined decay
exit target is reached.

### How It Works

```
When BOTH legs are active:

  combined_decay = (1 - combined_current / combined_entry) × 100

  Phase 1 — ACTIVATE: when combined_decay first reaches 30% → track peak
  Phase 2 — TRAIL: if combined_decay drops (peak - 40 points) → EXIT
```

### Configuration

```toml
[risk.combined_profit_trail]
enabled      = true
activate_pct = 30.0   # Start trailing after 30% combined decay
trail_pct    = 40.0   # Exit if decay retraces 40 points from peak
```

### Interaction with Combined Decay Exit

- Combined Decay Exit = TARGET — exit WHEN decayed enough (profit booking)
- Combined Profit Trail = FLOOR — exit if LOSING decay progress (reversal guard)

Both can coexist. Target fires first if reached; trail catches reversal.

---

## 16. State Keys Reference

### Position State

| Key | Type | Description |
|-----|------|-------------|
| `in_position` | bool | True when any leg is active |
| `ce_active` | bool | True when CE leg is open |
| `pe_active` | bool | True when PE leg is open |
| `symbol_ce` | str | Full symbol e.g. "NIFTY24MAR2623300CE" |
| `symbol_pe` | str | Full symbol e.g. "NIFTY24MAR2623300PE" |
| `orderid_ce` | str | Broker order ID for CE |
| `orderid_pe` | str | Broker order ID for PE |
| `entry_price_ce` | float | CE fill price |
| `entry_price_pe` | float | PE fill price |
| `exit_price_ce` | float | CE close fill price |
| `exit_price_pe` | float | PE close fill price |
| `entry_time` | str (ISO) | Position open timestamp |
| `entry_date` | str | YYYY-MM-DD of entry |
| `underlying_ltp` | float | NIFTY spot at entry time |
| `current_dte` | int | DTE at entry (for SL override) |

### P&L State

| Key | Type | Description |
|-----|------|-------------|
| `closed_pnl` | float | Realised P&L from closed legs |
| `today_pnl` | float | Combined P&L (closed + open MTM) |
| `trade_count` | int | Session trade counter |

### SL State

| Key | Type | Description |
|-----|------|-------------|
| `trailing_active_ce` | bool | Trailing SL activated for CE |
| `trailing_active_pe` | bool | Trailing SL activated for PE |
| `trailing_sl_ce` | float | Current trailing SL price for CE |
| `trailing_sl_pe` | float | Current trailing SL price for PE |
| `breakeven_active_ce` | bool | Breakeven SL activated for CE |
| `breakeven_active_pe` | bool | Breakeven SL activated for PE |
| `breakeven_sl_ce` | float | Breakeven SL price for CE (buffered) |
| `breakeven_sl_pe` | float | Breakeven SL price for PE (buffered) |
| `breakeven_activated_at_ce` | str (ISO) | When breakeven was activated for CE |
| `breakeven_activated_at_pe` | str (ISO) | When breakeven was activated for PE |
| `recovery_lock_active` | bool | Recovery trailing active after partial exit [v6.3.0] |
| `recovery_peak_pnl` | float | Peak combined P&L during recovery [v6.3.0] |
| `combined_trail_active` | bool | Combined profit trailing active [v6.3.0] |
| `combined_decay_peak` | float | Peak combined decay % [v6.3.0] |

### Filter State

| Key | Type | Description |
|-----|------|-------------|
| `vix_at_entry` | float | VIX value when position was opened |
| `ivr_at_entry` | float | IVR at entry time |
| `ivp_at_entry` | float | IVP at entry time |
| `orb_price` | float | NIFTY reference price from 09:17 capture |
| `margin_required` | float | Margin consumed by the straddle |
| `margin_available` | float | Account capital at entry time |

### Re-Entry State

| Key | Type | Description |
|-----|------|-------------|
| `last_close_time` | str (ISO) | When last position fully closed |
| `last_trade_pnl` | float | P&L of last completed trade |
| `reentry_count_today` | int | Number of re-entries today |

---

## 17. Configuration Reference

### Section 1 — Connection

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `http://127.0.0.1:5000` | OpenAlgo server URL |
| `api_key` | (required) | OpenAlgo API key (or env `OPENALGO_APIKEY`) |

### Section 2 — Instrument

| Key | Default | Description |
|-----|---------|-------------|
| `underlying` | `NIFTY` | Index name |
| `exchange` | `NSE_INDEX` | Exchange for order entry |
| `lot_size` | `65` | Contracts per lot |
| `number_of_lots` | `1` | Lots per leg |
| `product` | `MIS` | MIS (intraday) or NRML (carry) |
| `strike_offset` | `ATM` | ATM, OTM1–5, ITM1–5 |

### Section 3 — Timing

| Key | Default | Description |
|-----|---------|-------------|
| `entry_time` | `09:30` | Fallback entry time |
| `exit_time` | `15:15` | Hard square-off time |
| `monitor_interval_s` | `15` | Seconds between SL checks |
| `use_dte_entry_map` | `true` | Use DTE-specific entry times |
| `dte_entry_time_map` | See below | DTE → entry time |

```toml
[timing.dte_entry_time_map]
"0" = "09:30"   # DTE0 = Tuesday
"1" = "09:30"   # DTE1 = Monday
"2" = "09:35"   # DTE2 = Friday
"3" = "09:40"   # DTE3 = Thursday
"4" = "09:45"   # DTE4 = Wednesday
```

### Section 4 — DTE Filter

| Key | Default | Description |
|-----|---------|-------------|
| `trade_dte` | `[0,1,2,3,4]` | Which DTEs to trade on |

### Section 5 — Month Filter

| Key | Default | Description |
|-----|---------|-------------|
| `skip_months` | `[11]` | Skip November (1=Jan, 12=Dec) |

### Section 6 — VIX Filter

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable VIX range check |
| `vix_min` | `14.0` | Minimum VIX (below = premiums too thin) |
| `vix_max` | `28.0` | Maximum VIX (above = danger zone) |

### Section 6A — IVR/IVP Filter

| Key | Default | Description |
|-----|---------|-------------|
| `ivr_filter_enabled` | `true` | Enable IV Rank check |
| `ivr_min` | `30.0` | Minimum IVR (0–100 scale) |
| `ivp_filter_enabled` | `true` | Enable IV Percentile check |
| `ivp_min` | `40.0` | Minimum IVP (0–100%) |
| `ivr_fail_open` | `false` | Allow trade if history unavailable |
| `vix_history_file` | `vix_history.csv` | Daily VIX data file |
| `vix_history_min_rows` | `100` | Min rows for accurate IVR/IVP |
| `vix_update_time` | `15:30` | Auto-append today's VIX |

### Section 6B — ORB Filter

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable opening range filter |
| `capture_time` | `09:17` | When to capture NIFTY reference |
| `max_move_pct` | `0.5` | Max allowed move from ORB (%) |

### Section 7 — Risk Management

| Key | Default | Description |
|-----|---------|-------------|
| `leg_sl_percent` | `20.0` | Base per-leg SL % |
| `daily_profit_target_per_lot` | `5000` | Rs. target per lot (0=disabled) |
| `daily_loss_limit_per_lot` | `-4000` | Rs. limit per lot (negative, 0=disabled) |

### Section 7 — DTE SL Override

```toml
[risk.dte_sl_override]
"2" = 25.0   # DTE2 = Friday
"3" = 28.0   # DTE3 = Thursday
"4" = 30.0   # DTE4 = Wednesday
```

### Section 7A — Margin Guard

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable pre-trade margin check |
| `margin_buffer` | `1.20` | 20% headroom required |
| `fail_open` | `true` | Allow trade if API fails |
| `strike_rounding` | `50` | NIFTY=50, BANKNIFTY=100 |

### Section 7B — VIX Spike Monitor

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable intraday VIX spike exit |
| `threshold_pct` | `15.0` | % rise from entry VIX to trigger |
| `check_interval_s` | `300` | Seconds between checks (5 min) |
| `abs_floor` | `18.0` | Min absolute VIX to confirm spike |

### Section 7C — Trailing SL

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable trailing stop-loss |
| `trigger_pct` | `50.0` | Activate when LTP = this % of entry |
| `lock_pct` | `15.0` | Trail SL = LTP × (1 + lock_pct/100) |

### Section 7D — Dynamic SL

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable time-of-day SL tightening |
| `schedule` | See below | Time → SL% entries (descending) |

```toml
schedule = [
    { time = "14:30", sl_pct = 7.0  },
    { time = "13:30", sl_pct = 10.0 },
    { time = "12:00", sl_pct = 15.0 },
]
```

### Section 7E — Combined Decay Exit

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable combined premium decay exit |
| `decay_target_pct` | `60.0` | Exit when combined decay reaches this % |

### Section 7F — Winner-Leg Booking

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable winner-leg early booking |
| `decay_threshold_pct` | `30.0` | Book when LTP ≤ this % of entry |

### Section 7G — Breakeven SL

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable breakeven SL after partial exit |
| `grace_period_min` | `5` | Minutes before breakeven SL arms |
| `buffer_pct` | `10.0` | % buffer above mathematical breakeven |

### Section 7G — Spot-Move Exit

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable spot-move breach exit |
| `spot_multiplier` | `1.0` | Exit when move ≥ this × combined premium |
| `check_interval_s` | `60` | Seconds between spot checks |

### Section 7H — Re-Entry

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable re-entry after early close |
| `cooldown_min` | `30` | Minutes to wait after close |
| `max_loss_per_lot` | `2000` | Per-lot Rs. threshold (effective = per_lot × number_of_lots) |
| `max_reentries_per_day` | `1` | Max re-entries per day |

---

## 18. Telegram Notifications

The strategy sends alerts for every significant event:

| Event | Emoji | Sample Message |
|-------|-------|----------------|
| Strategy started | 🚀 | Strategy STARTED v6.0.0 [PARTIAL] |
| Entry placed | ✅ | ENTRY PLACED [ANALYZE] CE Rs.255 PE Rs.230 |
| Partial exit (SL hit) | ⚡ | PARTIAL EXIT — PE LEG CLOSED, CE still active |
| Full close (profit) | 🟢 | POSITION FULLY CLOSED, P&L Rs.+5200 |
| Full close (loss) | 🔴 | POSITION FULLY CLOSED, P&L Rs.-1500 |
| Trailing SL activated | 🔒 | TRAILING SL ACTIVATED — CE leg, locked Rs.4972 |
| Combined decay exit | 🎯 | COMBINED DECAY EXIT — 65% decay |
| Winner-leg booking | 💰 | WINNER LEG EARLY BOOKING — PE 25% of entry |
| VIX spike exit | 🚨 | VIX SPIKE EXIT — 18→22 (+22%) |
| Breakeven breach | ⚠️ | BREAKEVEN BREACH EXIT — NIFTY moved 500pts |
| Margin insufficient | ⚠️ | MARGIN INSUFFICIENT — trade SKIPPED |
| Broker quotes lost | ⚠️ | BROKER QUOTES UNREACHABLE for 45s |
| Monitor blocked | 🚨 | MONITOR BLOCKED — SL checks PAUSED |
| Emergency close fail | 🚨 | EMERGENCY CLOSE FAILED — MANUAL ACTION REQUIRED |
| Strategy stopped | - | Strategy STOPPED by operator |
| Strategy crashed | 🚨 | Strategy CRASHED — check logs |

---

## Changelog: v5.9.0 → v6.0.0

| Fix | Problem | Solution |
|-----|---------|----------|
| Breakeven SL instant-fire | Closed both legs in 12 seconds | 5-min grace period + 10% buffer |
| TRAIL_LOCK_PCT too high | 30% made trailing useless (capped at fixed SL) | Lowered to 15% |
| Uniform 20% SL on all DTEs | DTE2+ days hit SL on normal morning vol | DTE-aware: 25%/28%/30% |
| No re-entry after early close | Strategy idle 6 hours after Rs.-500 loss | Re-entry with 30-min cooldown |
| Per-leg SL ignores net P&L | Surviving leg SL fires when net position is profitable | Net P&L guard defers SL |
