# NIFTY Short Straddle — Partial Square Off Strategy

**Version 7.2.0** | OpenAlgo + Dhan API | Restart-Safe | Production Grade | Backtest-Optimised

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
16A. [Enriched Trade Log](#16a-enriched-trade-log-tradesjsonl-v640)
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
| Expiry | Weekly Tuesday | Auto-resolved via OpenAlgo expiry API (holiday-aware) |
| Base SL | 30% | Per-leg stop-loss (entry × 1.30) |
| Daily target | Rs.10,000/lot | Close all when combined P&L reaches this |
| Daily limit | Rs.-6,000/lot | Close all when combined loss exceeds this |
| Monitor interval | 15 seconds | How often SL/P&L is checked |
| WebSocket feed | Enabled | Real-time LTP via OpenAlgo WebSocket (REST fallback) |

---

## 2. Architecture & Module Map

```
main.py                     ← Entry point: creates StrategyCore and calls .run()
│
├── src/
│   ├── __init__.py          ← Package init (exports BrokerClient, MonitorState)
│   ├── _shared.py           ← Shared constants, lazy broker client, LTP cache, SL helpers
│   ├── strategy_core.py     ← Top-level orchestrator (scheduler, jobs, banner)
│   ├── filters.py           ← Entry filter chain (DTE, VIX, IVR/IVP, ORB)
│   ├── risk.py              ← MarginGuard + TrailingSLEngine
│   ├── order_engine.py      ← All broker order operations (entry, close, P&L)
│   ├── monitor.py           ← Intraday SL/P&L monitor (runs every tick)
│   ├── reconciler.py        ← Startup state vs broker reconciliation
│   ├── vix_manager.py       ← VIX fetch, IVR/IVP computation, history mgmt
│   └── ws_feed.py           ← WebSocket live feed (real-time LTP streaming)
│
├── util/
│   ├── __init__.py          ← Package init
│   ├── config_util.py       ← TOML config loader + validation + Config dataclass
│   ├── logger.py            ← IST-aware structured logging with rotation
│   ├── notifier.py          ← Telegram notifications (background daemon thread)
│   └── state.py             ← Atomic JSON state persistence (crash-safe)
│
├── config.toml              ← All user-configurable parameters
├── .env                     ← Secrets: API keys, Telegram tokens (git-ignored)
└── .env.example             ← Template for .env (safe to commit)
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
├── WebSocketFeed      (real-time LTP streaming) ← feeds LTP cache in _shared.py
└── StartupReconciler  (state recovery) ← uses OrderEngine
```

---

## 3. Complete Lifecycle — Entry to Exit

### Timeline of a Typical Trading Day

```
09:00  Strategy starts → validate config → print banner → test connection
       → reconcile state with broker → check VIX history
       → start WebSocket feed (subscribe NIFTY spot + INDIAVIX)

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
       ├─ Subscribe CE + PE symbols to WebSocket for real-time LTP
       └─ Monitor starts on next tick

09:35+ MONITOR LOOP (every MONITOR_INTERVAL_S seconds):
       ┌─ Read LTP from WebSocket cache (instant, no API call)
       │  └─ Falls back to REST API if cache is stale or WS disconnected
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
│     ├─ reentry_count >= MAX_PER_DAY (2)? → skip          │
│     ├─ Cooldown elapsed? (≥ 45 min since last close)     │
│     └─ Previous loss ≤ MAX_LOSS_PER_LOT (Rs.2000)?       │
│                                                          │
│  2. DTE FILTER + MONTH FILTER                            │
│     ├─ Today's DTE in TRADE_DTE list? [0,1,2,3,4]       │
│     ├─ Weekend guard (Sat/Sun → skip)                    │
│     └─ Current month in SKIP_MONTHS? [11=November]       │
│                                                          │
│  3. VIX FILTER  ◀ DISABLED (v7.0.0)                      │
│     ├─ 5yr backtest: no DD reduction, costs 47% net P&L  │
│     ├─ Intraday VIX spike monitor (7B) provides          │
│     │  real-time protection instead                       │
│     └─ VIX still fetched + stored for analytics          │
│                                                          │
│  4. IVR / IVP FILTER  ◀ DISABLED (v7.0.0)               │
│     ├─ 5yr backtest: skipped 63% of profitable days      │
│     ├─ Win rate +6% but net P&L -47%                     │
│     └─ IVR/IVP still computed for analytics logging      │
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
│     ├─ Store current_dte in state                        │
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
           CE: NIFTY24MAR2623300CE  fill Rs.255.35  SL @ Rs.331.96
           PE: NIFTY24MAR2623300PE  fill Rs.230.55  SL @ Rs.299.72
```

> Note: All DTEs use a flat 30% SL (v7.0.0 — DTE overrides removed after backtest optimisation).

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
│  ⚠️  v7.0.0: DISABLED (backtest showed trailing rarely      │
│     activates and cuts winners short when it does)           │
│                                                             │
│  PRIORITY 2: BREAKEVEN SL (after grace period)              │
│  ├─ Condition: BREAKEVEN_AFTER_PARTIAL_ENABLED = true       │
│  │             AND breakeven_active_{leg} = true             │
│  │             AND breakeven_sl_{leg} > 0                    │
│  │             AND breakeven_sl < fixed_sl (tighter)         │
│  │             AND grace period has ELAPSED (≥ 5 min)        │
│  └─ Returns: breakeven_sl_{leg} (with 5% buffer applied)     │
│                                                             │
│  PRIORITY 3: FIXED SL (lowest — default)                    │
│  ├─ Base SL % = LEG_SL_PCT (30% flat for all DTEs)          │
│  └─ Returns: entry_price × (1 + 30 / 100)                   │
│  ⚠️  v7.0.0: Dynamic SL DISABLED (time-of-day tightening    │
│     hurt performance — position captures theta to hard exit) │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

### SL Calculation Example

```
Entry: CE sold at Rs.255.35 on DTE2 (Friday)

FIXED SL (v7.0.0 — flat 30% for all DTEs, no dynamic tightening):
  base_sl_pct = LEG_SL_PCT = 30%
  SL = 255.35 × 1.30 = Rs.331.96

  Note: Dynamic SL (time-of-day tightening) is DISABLED in v7.0.0.
  The 30% fixed SL remains constant throughout the day.
  Backtest showed the wider SL lets trades breathe and capture
  full theta decay to hard exit at 15:15.

TRAILING SL (DISABLED in v7.0.0 — shown for reference):
  Would activate when CE decays to Rs.127.68 = 50% of entry
  trailing_sl = 127.68 × 1.15 = Rs.146.83    ← lock_pct = 15%
  Backtest: only activated 3/224 trades, cut winners short.
```

---

## 6. Partial Square Off Logic

### How It Works

```
                    BOTH LEGS ACTIVE
                    CE: Rs.255.35  SL @ Rs.331.96 (30%)
                    PE: Rs.230.55  SL @ Rs.299.72 (30%)
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
              │   buffered = 196.89 × 1.05  │
              │           = Rs.206.73       │
              │                            │
              │ Grace period: 5 minutes     │
              │ CE uses fixed SL until then │
              └────────────────────────────┘
                           │
              After 5 minutes grace period:
                           │
                           ▼
              ┌────────────────────────────┐
              │ CE SL = Rs.206.73 (BE+buf) │
              │ CE LTP must RISE above     │
              │ Rs.206.73 to trigger exit   │
              │                            │
              │ If CE keeps decaying → OK   │
              │ If CE reverses → closes at  │
              │   ~Rs.207, net loss ~Rs.0   │
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
state["breakeven_sl_ce"]     = 206.73            # With 5% buffer
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

#### B) Buffer (5% above mathematical breakeven)

```
breakeven_buffer_pct = 5.0    # v7.0.0: tightened from 10% to 5%

Without buffer:  be_sl = Rs.207.10  (fires when CE >= 207.10)
With 5% buffer:  be_sl = Rs.207.10 × 1.05 = Rs.217.46

CE must bounce from Rs.215 all the way to Rs.217.46 to trigger.
Tighter buffer (v7.0.0) locks in breakeven protection more aggressively.
```

#### How sl_level() Evaluates During Grace Period

```python
# During grace period (first 5 min after breakeven activation):
sl_level("CE") returns:
    → Fixed SL (e.g., Rs.331.96 at 30%)
    → breakeven_sl is IGNORED

# After grace period:
sl_level("CE") returns:
    → Rs.217.46 (breakeven + 5% buffer)
    → Only if 217.46 < fixed_sl (it's tighter = better protection)
```

---

## 8. Trailing Stop-Loss Engine

> **v7.0.0 Status: DISABLED** — Backtest optimisation showed trailing SL activated on only 3/224 trades, and when it did, it cut winners short. With 30% fixed SL and breakeven SL protecting partial exits, trailing adds no value. The engine remains in code for future use.

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
Fixed SL: Rs.331.96 (30%)

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

> **v7.0.0 Status: DTE overrides REMOVED** — Backtest optimisation showed the base 30% SL is optimal for ALL DTEs. Per-DTE overrides added no value over a single wider SL.

### Configuration (v7.0.0)

```toml
[risk]
leg_sl_percent = 30.0     # Flat 30% SL for all DTEs

[risk.dte_sl_override]
# No overrides — all DTEs use base leg_sl_percent (30%)
```

### How It Works

```python
def _base_sl_percent():
    # 1. Check DTE override (empty in v7.0.0)
    dte = state.get("current_dte")
    if dte in cfg.DTE_SL_OVERRIDE:
        return DTE_SL_OVERRIDE[dte]
    # 2. Fall back to default
    return LEG_SL_PERCENT             # 30%
```

### SL Levels Per DTE (CE entry Rs.255.35, PE entry Rs.230.55)

| DTE | Day | Base SL% | CE SL | PE SL |
|-----|-----|----------|-------|-------|
| 0 | Tuesday | 30% | Rs.331.96 | Rs.299.72 |
| 1 | Monday | 30% | Rs.331.96 | Rs.299.72 |
| 2 | Friday | 30% | Rs.331.96 | Rs.299.72 |
| 3 | Thursday | 30% | Rs.331.96 | Rs.299.72 |
| 4 | Wednesday | 30% | Rs.331.96 | Rs.299.72 |

> v7.0.0: All DTEs use a flat 30% SL. The wider SL gives trades room to
> breathe through morning volatility on all days, not just higher DTEs.

---

## 10. Dynamic Time-of-Day SL Tightening

> **v7.0.0 Status: DISABLED** — Backtest optimisation showed time-of-day SL tightening hurts performance. Without dynamic tightening and trailing, the position naturally captures theta all the way to hard exit at 15:15. The fixed 30% SL provides adequate protection throughout the day.

As the day progresses and theta has been captured, the SL tightens to protect gains.

### Schedule (reference — disabled in v7.0.0)

```
Before 12:00  → base SL (30%)
After 12:00   → 15%
After 13:30   → 10%
After 14:30   → 7%
```

### Example (if re-enabled)

The dynamic schedule uses `min(schedule_pct, base_pct)` — it can only **tighten**,
never widen beyond the base.

```
All DTEs (base=30%):
  09:30  base=30%  dynamic=N/A  → effective 30%
  12:01  base=30%  dynamic=15%  → effective min(15, 30) = 15%
  13:31  base=30%  dynamic=10%  → effective 10%
  14:31  base=30%  dynamic=7%   → effective 7%
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
CE continues running. Daily loss limit (Rs.-6000) protects downside.
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
cooldown_min          = 45      # Wait 45 min after close (v7.0.0: was 30)
max_loss_per_lot      = 2000    # Per-lot Rs. threshold (effective = per_lot × number_of_lots)
max_reentries_per_day = 2       # Maximum 2 re-entries per day (v7.0.0: was 1)
```

### How It Works

```
09:46  Position closes: P&L = Rs.-517
       state["last_close_time"] = "09:46:33"
       state["last_trade_pnl"]  = -517

10:31  Next entry job fires (cooldown elapsed: 45 min ✓)
       ├─ Is re-entry enabled? → YES
       ├─ Reentry count today: 0 < max 2 → OK
       ├─ Cooldown: 45 min elapsed ≥ 45 min required → OK
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
| Three re-entries in one day | MAX_PER_DAY=2 blocks the third |
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
├─ Fixed SL         : 30% flat for all DTEs (entry × 1.30)
├─ Dynamic SL       : DISABLED v7.0.0 (was: tightens through day)
├─ Trailing SL      : DISABLED v7.0.0 (was: locks profit at 50% decay)
└─ Breakeven SL     : context-aware, 5% buffer — only on losing survivor [v6.2.0]

LAYER 3 — POSITION-LEVEL PROTECTION (limit overall exposure)
├─ Combined decay   : exit when both legs decayed 60% combined
├─ Asymmetric book  : book deeply decayed winner when legs diverge [v6.3.0]
├─ Combined trail   : exit if combined decay retraces from peak [v6.3.0]
├─ Winner booking   : book deeply profitable surviving leg (< 30% of entry)
├─ Recovery lock    : DISABLED v7.0.0 (was: trail combined P&L recovery)
├─ Net P&L guard    : defer SL when combined P&L is positive
├─ Daily target     : close all at Rs.10,000 profit
└─ Daily loss limit : close all at Rs.-6,000 loss

LAYER 4 — SYSTEMIC PROTECTION (market-wide events)
├─ VIX spike exit   : close all if VIX rises 15%+ from entry
├─ Spot-move exit   : DISABLED v7.0.0 (was: close if NIFTY moves beyond BE)
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
       CE: NIFTY24MAR2623300CE  sell @ Rs.180.00  SL @ Rs.234.00 (30%)
       PE: NIFTY24MAR2623300PE  sell @ Rs.170.00  SL @ Rs.221.00 (30%)
       Combined premium: Rs.350/unit = Rs.22,750 total (65 qty)

10:00  CE Rs.160, PE Rs.155 → Open MTM = (180-160 + 170-155) × 65 = Rs.2,275
11:00  CE Rs.120, PE Rs.110 → Open MTM = (180-120 + 170-110) × 65 = Rs.7,800
       Combined decay = (1 - 230/350) × 100 = 34.3% → not yet 60%

       Note: Dynamic SL and trailing SL are DISABLED in v7.0.0.
       Fixed 30% SL stays constant. Position captures theta naturally.

13:00  CE Rs.60, PE Rs.55 → Combined decay = (1 - 115/350) × 100 = 67.1%
       ≥ 60% → COMBINED DECAY EXIT fires!
       Close both legs: P&L = (180-60 + 170-55) × 65 = Rs.15,275

       ✅ Trade P&L: +Rs.15,275 | Held: 3h 30m | Exit: Combined Decay
```

### Example 2: One Leg SL Hit, Surviving Leg Profits (Partial Success)

```
Day: Friday (DTE2) | VIX: 22 | IVR: 90 | NIFTY: 23,300

09:35  ENTRY
       CE: sell @ Rs.255.35  SL @ Rs.331.96 (30%)
       PE: sell @ Rs.230.55  SL @ Rs.299.72 (30%)

10:05  NIFTY drops 120 points to 23,180
       CE Rs.180 (profit Rs.4,897)  |  PE Rs.305 (loss)
       PE LTP Rs.305 >= SL Rs.299.72 → PE SL HIT

       close_one_leg("PE"):
         PE fill at Rs.305.50
         PE realised P&L: (230.55 - 305.50) × 65 = Rs.-4,872
         closed_pnl = Rs.-4,872

       Breakeven SL for CE:
         raw_be = 255.35 + (-4872 / 65) = Rs.180.39
         buffered = 180.39 × 1.05 = Rs.189.41    (5% buffer)
         Grace: 5 minutes (arms at ~10:10)

10:05–10:10  CE uses fixed SL Rs.331.96 (grace period)
             CE decays: Rs.180 → Rs.160 → Rs.140

10:10  Grace expires. CE SL = Rs.189.41 (breakeven + 5% buffer)
       CE LTP Rs.140 — well below Rs.189.41 → NO trigger

       Note: Trailing SL is DISABLED in v7.0.0, so CE continues
       with fixed 30% SL and breakeven SL until hard exit.

13:00  CE Rs.30 → deeply profitable, winner booking checks
14:00  CE Rs.15 → holds through to hard exit
15:15  HARD EXIT: CE closes at Rs.10
       CE realised P&L: (255.35 - 10) × 65 = Rs.15,948

       Final P&L = -4,872 + 15,948 = Rs.+11,076

       ✅ Trade P&L: +Rs.11,076 | Despite PE SL hit!
```

### Example 3: Both Legs SL Hit (Bad Day — Mitigated by Wider DTE SL)

```
Day: Wednesday (DTE4) | VIX: 25 | NIFTY: 23,300

09:45  ENTRY
       CE: sell @ Rs.120.00  SL @ Rs.156.00 (30%)
       PE: sell @ Rs.115.00  SL @ Rs.149.50 (30%)

10:30  NIFTY rallies 200 points (unexpected event)
       CE Rs.160 → LTP Rs.160 >= SL Rs.156 → CE SL HIT
       close_one_leg("CE"):
         CE P&L: (120 - 160) × 65 = Rs.-2,600
         Breakeven SL for PE: raw = 115 + (-2600/65) = Rs.75.00
         buffered = 75 × 1.05 = Rs.78.75

10:30–10:35  PE uses fixed SL Rs.149.50 (grace)
10:35        Grace expires. PE SL = Rs.78.75

11:00  NIFTY stabilises. PE Rs.70 (profitable)
       Net P&L = -2600 + (115-70) × 65 = -2600 + 2925 = Rs.+325

12:00  NIFTY reverses back down 100 points
       PE rises from Rs.70 → Rs.80
       PE LTP Rs.80 >= breakeven SL Rs.78.75 → BREAKEVEN SL HIT

       close_one_leg("PE"):
         PE P&L: (115 - 80) × 65 = Rs.+2,275

       Final P&L = -2,600 + 2,275 = Rs.-325

       ⚠️ Trade P&L: -Rs.325 | Capped loss via breakeven SL (5% buffer)
       Without breakeven: PE could have run to Rs.149.50 → loss Rs.-4,843
```

### Example 4: Re-Entry Recovers Early Loss

```
Day: Friday (DTE2) | VIX: 20

09:35  First entry
       CE: sell @ Rs.200  |  PE: sell @ Rs.190
       NIFTY drops → PE hits 30% SL at Rs.247.00 → loss Rs.-3,705

09:40  Breakeven + grace → CE closes at Rs.170
       CE P&L: (200-170) × 65 = Rs.+1,950
       Final P&L: -3,705 + 1,950 = Rs.-1,755

       state["last_close_time"] = "09:40"
       state["last_trade_pnl"]  = -1755

10:25  Entry job fires. Re-entry check:
       ├─ REENTRY_ENABLED = true ✓
       ├─ reentry_count = 0 < max 2 ✓
       ├─ Cooldown: 45 min elapsed ✓
       ├─ Loss Rs.1,755 ≤ Rs.2,000 max ✓
       └─ Filter chain: VIX OK, IVR OK, ORB OK, margin OK ✓

10:25  RE-ENTRY #1
       CE: sell @ Rs.150  |  PE: sell @ Rs.145
       NIFTY stays flat → both legs decay

14:30  Combined decay 65% → COMBINED DECAY EXIT
       CE Rs.50, PE Rs.55 → P&L: (150-50 + 145-55) × 65 = Rs.12,350

       Day total: -1,755 + 12,350 = Rs.+10,595

       ✅ Recovery! Without re-entry: day ends at Rs.-1,755
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

> **v7.0.0 Status: DISABLED** — Backtest optimisation showed recovery lock was cutting profits short by exiting too early during recoveries.

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

### Enriched Trade Log State [v6.4.0]

| Key | Type | Description |
|-----|------|-------------|
| `sl_events` | list[dict] | Partial close events — leg, trigger, time, prices, P&L |
| `filters_passed` | list[str] | Entry filters that passed (e.g. "dte", "vix", "orb") |
| `is_reentry` | bool | Whether this trade is a re-entry after early close |

### Re-Entry State

| Key | Type | Description |
|-----|------|-------------|
| `last_close_time` | str (ISO) | When last position fully closed |
| `last_trade_pnl` | float | P&L of last completed trade |
| `reentry_count_today` | int | Number of re-entries today |

---

## 16A. Enriched Trade Log (trades.jsonl) [v6.4.0]

Each completed trade appends one JSON line to `data/trades.jsonl` with full decision
metadata for post-session analysis and parameter tuning.

### Record Structure

```json
{
  "date": "2026-03-21",
  "entry_time": "2026-03-21T09:35:00+05:30",
  "exit_time": "2026-03-21T14:22:15+05:30",
  "duration_min": 287,
  "symbol_ce": "NIFTY25MAR2623300CE",
  "symbol_pe": "NIFTY25MAR2623300PE",
  "entry_price_ce": 102.5,
  "entry_price_pe": 97.3,
  "exit_price_ce": 45.2,
  "exit_price_pe": 12.0,
  "combined_premium": 199.8,
  "ce_pnl": 3724,
  "pe_pnl": 5545,
  "closed_pnl": 9269,
  "exit_reason": "Combined Premium 62.0% Decay Target Reached",
  "trade_count": 1,

  "vix_at_entry": 16.8,
  "ivr_at_entry": 45.2,
  "ivp_at_entry": 62.0,
  "underlying_ltp": 23465,
  "orb_price": 23450,
  "margin_required": 125000,
  "margin_available": 250000,

  "vix_at_exit": 17.2,
  "spot_at_exit": 23490,
  "spot_move_pct": 0.11,

  "dte": 2,
  "number_of_lots": 1,
  "lot_size": 65,
  "is_reentry": false,

  "filters_passed": ["dte", "vix", "ivr_ivp", "orb", "margin"],

  "sl_events": [
    {
      "leg": "CE",
      "trigger": "Trailing SL Hit",
      "time": "2026-03-21T13:10:22+05:30",
      "ltp": 45.2,
      "entry_px": 102.5,
      "fill_px": 45.5,
      "pnl": 3705
    }
  ],

  "trailing_activated_ce": true,
  "trailing_activated_pe": false,
  "trailing_sl_ce": 46.8,
  "trailing_sl_pe": 0.0,
  "breakeven_activated_ce": false,
  "breakeven_activated_pe": false,
  "breakeven_sl_ce": 0.0,
  "breakeven_sl_pe": 0.0,
  "recovery_lock_fired": false,
  "recovery_peak_pnl": 0.0,
  "combined_trail_fired": false,
  "combined_decay_peak": 0.0
}
```

### What Each Section Enables

| Section | Fields | Analysis Use |
|---------|--------|-------------|
| **Core trade** | entry/exit prices, P&L, duration | Basic win/loss tracking |
| **Per-leg P&L** | ce_pnl, pe_pnl, combined_premium | Which leg contributed, premium quality |
| **Entry context** | VIX, IVR, IVP, ORB, margin | Correlate market regime with outcomes |
| **Exit context** | vix_at_exit, spot_at_exit, spot_move_pct | How market moved during trade |
| **DTE + lots** | dte, number_of_lots, is_reentry | Performance by DTE, re-entry success rate |
| **Filters** | filters_passed | Which filters contributed to entries |
| **SL events** | sl_events[] | Partial close timing, which triggers fire |
| **Risk layers** | trailing/breakeven/recovery/trail states | Which risk layer was active at exit |

---

## 17. Configuration Reference

### Section 1 — Connection

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `http://127.0.0.1:5000` | OpenAlgo server URL |
| `api_key` | `""` (from `.env`) | OpenAlgo API key — loaded from `OPENALGO_APIKEY` in `.env` |

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
| `enabled` | `false` | Enable VIX range check — **DISABLED v7.0.0** |
| `vix_min` | `14.0` | Minimum VIX (below = premiums too thin) |
| `vix_max` | `28.0` | Maximum VIX (above = danger zone) |

### Section 6A — IVR/IVP Filter

| Key | Default | Description |
|-----|---------|-------------|
| `ivr_filter_enabled` | `false` | Enable IV Rank check — **DISABLED v7.0.0** |
| `ivr_min` | `30.0` | Minimum IVR (0–100 scale) |
| `ivp_filter_enabled` | `false` | Enable IV Percentile check — **DISABLED v7.0.0** |
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
| `leg_sl_percent` | `30.0` | Base per-leg SL % (v7.0.0: was 20) |
| `daily_profit_target_per_lot` | `10000` | Rs. target per lot (v7.0.0: was 5000) |
| `daily_loss_limit_per_lot` | `-6000` | Rs. limit per lot (v7.0.0: was -4000) |

### Section 7 — DTE SL Override

```toml
[risk.dte_sl_override]
# No overrides — all DTEs use base leg_sl_percent (30%) in v7.0.0
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
| `enabled` | `false` | Enable trailing stop-loss (v7.0.0: DISABLED) |
| `trigger_pct` | `50.0` | Activate when LTP = this % of entry |
| `lock_pct` | `15.0` | Trail SL = LTP × (1 + lock_pct/100) |

### Section 7D — Dynamic SL

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable time-of-day SL tightening (v7.0.0: DISABLED) |
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
| `buffer_pct` | `5.0` | % buffer above mathematical breakeven (v7.0.0: was 10) |

### Section 7G — Spot-Move Exit

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable spot-move breach exit (v7.0.0: DISABLED) |
| `spot_multiplier` | `1.0` | Exit when move ≥ this × combined premium |
| `check_interval_s` | `60` | Seconds between spot checks |

### Section 7H — Re-Entry

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable re-entry after early close |
| `cooldown_min` | `45` | Minutes to wait after close (v7.0.0: was 30) |
| `max_loss_per_lot` | `2000` | Per-lot Rs. threshold (effective = per_lot × number_of_lots) |
| `max_reentries_per_day` | `2` | Max re-entries per day (v7.0.0: was 1) |

### Section 8 — Expiry

| Key | Default | Description |
|-----|---------|-------------|
| `auto_expiry` | `true` | Fetch next expiry from OpenAlgo expiry API (with Tuesday fallback) |
| `manual_expiry` | `"25MAR26"` | Used only when `auto_expiry = false` (must be a Tuesday, DDMMMYY format) |

### Section 9A — WebSocket Live Feed [v7.2.0]

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable real-time LTP via OpenAlgo WebSocket |
| `staleness_timeout_s` | `60` | Cache entries older than this (seconds) trigger REST fallback |
| `reconnect_max_delay_s` | `30` | Maximum backoff delay between reconnect attempts |

**How it works:**

```
WebSocket daemon thread (single connection)
    ↓
Subscribes: CE symbol, PE symbol, NIFTY spot, INDIAVIX
    ↓
Server pushes LTP updates → stored in thread-safe cache (_shared._ltp_cache)
    ↓
fetch_ltp() reads cache first (instant) → REST API fallback if stale
    ↓
Monitor loop reads fresh prices every tick with zero API calls
```

**Fallback behaviour:** If WebSocket disconnects, auto-reconnect with exponential
backoff (1s → 2s → 4s → ... → 30s cap). After 3 consecutive failures, a Telegram
alert is sent. During disconnection, `fetch_ltp()` seamlessly falls back to REST
API polling (existing behaviour preserved).

---

## 18. Telegram Notifications

The strategy sends alerts for every significant event:

| Event | Emoji | Sample Message |
|-------|-------|----------------|
| Strategy started | 🚀 | Strategy STARTED v7.1.0 [PARTIAL] |
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
| WebSocket feed down | ⚠️ | WebSocket feed DOWN — falling back to REST polling |
| Monitor blocked | 🚨 | MONITOR BLOCKED — SL checks PAUSED |
| Emergency close fail | 🚨 | EMERGENCY CLOSE FAILED — MANUAL ACTION REQUIRED |
| Strategy stopped | - | Strategy STOPPED by operator |
| Strategy crashed | 🚨 | Strategy CRASHED — check logs |

---

## Changelog: v7.1.0 → v7.2.0 (WebSocket Live Feed + Expiry API)

| Change | Description |
|--------|-------------|
| WebSocket live feed | New `src/ws_feed.py` — production WebSocket client streams real-time LTP from OpenAlgo, replacing 15s REST polling with sub-second price updates. Single daemon thread, single connection for all symbols (CE, PE, spot, VIX). |
| LTP cache in `_shared.py` | Thread-safe `_ltp_cache` dict populated by WebSocket. `fetch_ltp()` reads cache first (instant), falls back to REST API if stale or disconnected. |
| Auto-reconnect | Exponential backoff (1s → 30s cap) on disconnect. Telegram alert after 3 consecutive failures. All subscriptions automatically restored on reconnect. |
| Graceful shutdown | `ws_feed.stop()` unsubscribes all symbols and closes the WebSocket connection cleanly on Ctrl+C, SIGTERM, or crash. |
| Expiry API | `auto_expiry` now fetches actual expiry dates from OpenAlgo `/api/v1/expiry` endpoint instead of hardcoding next Tuesday. Handles holidays correctly. Falls back to Tuesday calculation if API is unavailable. 5-minute cache. |
| `[websocket]` config | New config section: `enabled`, `staleness_timeout_s`, `reconnect_max_delay_s`. |
| Monitor interval | With WebSocket feeding cached LTP, `monitor_interval_s` can be reduced to 2-5s for faster SL execution without API rate limit concerns. |

---

## Changelog: v7.0.0 → v7.1.0 (Code Quality + Security Hardening)

| Change | Description |
|--------|-------------|
| Secrets to `.env` | Moved OpenAlgo API key and Telegram credentials from `config.toml` to `.env` (git-ignored). Added `.env.example` template and `python-dotenv` loader. |
| Validation hardening | Replaced all `assert` statements with proper `if not: raise ValueError` + specific `except` clauses across `strategy_core.py` and `config_util.py`. |
| Mutable default fix | `state.py` `reset()` now uses `copy.deepcopy(INITIAL_STATE)` — prevents `sl_events: []` and `filters_passed: []` from leaking across resets. |
| Notifier flush fix | Fixed `flush(timeout)` — replaced blocking `queue.join()` with deadline-based polling loop that respects the timeout parameter. |
| Shared LTP helper | Extracted `fetch_ltp(symbol, exchange)` to `_shared.py` — eliminates 6-line quote+check pattern duplicated in `filters.py`, available for all modules. |
| Modern typing | Replaced `Optional[X]` → `X \| None` and `List`/`Tuple` → `list`/`tuple` across all modules. Removed `typing` imports where no longer needed. |
| Dead code removal | Removed unused `__getattr__` backward-compatibility alias in `_shared.py` and stale `import tempfile` in `strategy_core.py`. |

---

## Changelog: v6.4.0 → v7.0.0 (Backtest-Optimised)

Based on 5-phase grid search optimisation across 600+ parameter combinations
on 1-year Dhan expired options data (2025-03-21 to 2026-03-21, 224 trading days).

**Results:** ₹190,936 P&L | 64.8% win rate | PF 2.18 | Max DD -₹12,812

| Change | Old Value | New Value | Why |
|--------|-----------|-----------|-----|
| Base SL | 20% | 30% | Wider SL lets trades breathe through normal volatility |
| Dynamic SL | enabled | **disabled** | Time-of-day tightening hurts — exits before full theta capture |
| Trailing SL | enabled | **disabled** | Only activated 3/224 trades, cut winners short |
| Spot-move exit | enabled | **disabled** | Was exiting profitable positions prematurely |
| Recovery lock | enabled | **disabled** | Was cutting profits short after partial exits |
| Breakeven buffer | 10% | 5% | Tighter buffer locks in breakeven protection faster |
| Daily profit target | Rs.5,000/lot | Rs.10,000/lot | Capture full theta on big winning days |
| Daily loss limit | Rs.-4,000/lot | Rs.-6,000/lot | Fewer premature daily stops |
| DTE SL overrides | 25%/28%/30% | removed | Base 30% handles all DTEs optimally |
| Re-entry cooldown | 30 min | 45 min | More stabilisation time after early close |
| Re-entry max/day | 1 | 2 | Allows additional recovery opportunities |
| DTE0 decay target | 70% | 60% | Consistent decay target across all DTEs |

---

## Changelog: v5.9.0 → v6.0.0

| Fix | Problem | Solution |
|-----|---------|----------|
| Breakeven SL instant-fire | Closed both legs in 12 seconds | 5-min grace period + buffer (now 5% in v7.0.0) |
| TRAIL_LOCK_PCT too high | 30% made trailing useless (capped at fixed SL) | Lowered to 15% |
| Uniform 20% SL on all DTEs | DTE2+ days hit SL on normal morning vol | DTE-aware widening (now flat 30% in v7.0.0) |
| No re-entry after early close | Strategy idle 6 hours after Rs.-500 loss | Re-entry with 30-min cooldown |
| Per-leg SL ignores net P&L | Surviving leg SL fires when net position is profitable | Net P&L guard defers SL |
