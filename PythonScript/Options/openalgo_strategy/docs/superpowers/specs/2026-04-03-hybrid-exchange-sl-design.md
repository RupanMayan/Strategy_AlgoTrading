# Hybrid Exchange SL — Design Spec

## Summary

Add a second protection layer to the Nifty Short Straddle production script by placing SL-M (stop-loss market) orders on the exchange immediately after entry. These orders act as a catastrophic safety net — they only fire when the script's monitoring loop fails to exit in time (flash crash, API failure, internet down).

## Decisions Made

- **SL percentage:** 45% above entry price per leg
- **Order type:** SL-M (market order on trigger — guaranteed fill)
- **Detection method:** `orderstatus()` check each monitor tick
- **API:** `placeorder()` for placing, `cancelorder()` for cancelling, `orderstatus()` for detection

## What Changes

### New Config Constants

```python
EXCHANGE_SL_ENABLED     = True
EXCHANGE_SL_PCT         = 45.0      # Trigger at 45% above entry per leg
```

### New State Fields

```python
state["exchange_sl_oid_ce"] = ""    # Exchange SL order ID for CE leg
state["exchange_sl_oid_pe"] = ""    # Exchange SL order ID for PE leg
```

### What Does NOT Change

All 13 existing exit modules remain identical. The exchange SL is purely additive.

## Lifecycle

### Phase 1: Place SL After Entry

**When:** Immediately after `optionsmultiorder()` succeeds and fill prices are captured.

**Why after fills:** We need the actual entry price to calculate the 45% trigger. Entry prices come from `_capture_fills()` which runs in a background thread with 2-3s delay.

**Flow:**
1. Entry fills captured (CE=120, PE=105)
2. Calculate triggers: CE = 120 × 1.45 = 174, PE = 105 × 1.45 = 152.25 → round to 152.3
3. Place SL-M BUY CE at trigger=174 via `placeorder()`
4. Place SL-M BUY PE at trigger=152.3 via `placeorder()`
5. Save order IDs to state
6. If SL placement fails → log + telegram alert, continue without (fail-open)

**API call:**
```python
resp = broker.get().placeorder(
    strategy=STRATEGY_NAME,
    symbol=state["symbol_ce"],
    exchange=OPTION_EXCH,
    action="BUY",
    quantity=str(qty()),
    price_type="SL-M",
    product=PRODUCT,
    trigger_price=str(trigger_price),
)
```

### Phase 2: Detect Exchange SL Trigger (Each Monitor Tick)

**When:** Every `_tick_inner()` call, BEFORE running any exit logic.

**Why first:** If the exchange already exited a leg, all other checks on that leg are invalid.

**Flow:**
1. If `exchange_sl_oid_ce` is set and `ce_active` is True:
   - Call `orderstatus(exchange_sl_oid_ce)`
   - If status = "complete" → exchange exited CE
     - Get `average_price` from response as fill price
     - Mark CE as closed with reason "Exchange SL-M (45%)"
     - Cancel PE's exchange SL order (will be re-evaluated by script)
     - Set `exchange_sl_oid_ce = ""`
     - Activate breakeven on PE survivor (existing logic)
   - If status = "rejected" or "cancelled" → SL order died
     - Log warning, set `exchange_sl_oid_ce = ""`
     - Script's own SL continues to protect
2. Same for PE leg

**Throttling:** Check orderstatus every tick (5s). This adds 2 API calls per tick. Rate limit is 10 req/s — we're well within limits.

**Error handling:** If orderstatus call fails, skip and retry next tick. Don't block the monitor loop.

### Phase 3: Cancel Exchange SL on Script Exit

**When:** Every time the script closes a leg (any of the 13 exit reasons).

**Where:** Inside `close_one_leg()` and `_close_all_locked()`.

**Flow:**
1. Script decides to close CE (e.g., combined decay exit)
2. Script sends BUY MARKET order for CE (existing flow)
3. Script cancels exchange SL for CE: `cancelorder(exchange_sl_oid_ce)`
4. Set `exchange_sl_oid_ce = ""`

**Critical:** Cancel MUST happen AFTER the script's exit order is sent. If we cancel first and the exit order fails, we'd have no protection.

**Error handling:** If cancel fails (order already triggered or already cancelled), log and continue. This is not critical — a cancel on a completed order is harmless.

### Phase 4: Cleanup at Day End / Full Flat

**When:** In `_mark_fully_flat()` and time-based exit.

**Flow:** Cancel any remaining exchange SL orders. Belt-and-suspenders — they should already be cancelled by Phase 3, but this catches any edge case.

## Edge Cases — All Scenarios

### 1. Entry succeeds but SL placement fails

**Cause:** API error, rate limit, network blip.
**Handling:** Log error + telegram alert. Continue without exchange SL. Script's own monitoring still active. This is fail-open — the strategy worked fine without exchange SL before, so missing it occasionally is acceptable.
**State:** `exchange_sl_oid_ce = ""` → skip detection in Phase 2.

### 2. Exchange SL triggers AND script detects SL in same tick

**Cause:** Price crosses both 30% (script SL) and 45% (exchange SL) within one 5s window.
**Risk:** Double BUY order for same leg → ends up long.
**Prevention:** In Phase 2, if exchange SL is detected as "complete", mark leg as `_active = False` BEFORE running exit logic. The existing SL check in `_tick_inner()` skips inactive legs. So the script won't try to close an already-closed leg.

### 3. Script closes leg but cancel fails

**Cause:** Network error during cancel call.
**Impact:** Exchange SL order still pending. But the leg is already closed (BUY filled). If exchange SL triggers later, it's a BUY on an already-flat position — creates an unwanted LONG.
**Prevention:**
- Retry cancel up to 3 times
- On final failure, send telegram CRITICAL alert
- In Phase 4 (day-end cleanup), try cancel again
- The MIS product auto-squares at 15:15 anyway — any accidental long gets closed

### 4. Exchange SL partially fills

**Cause:** Low liquidity (unlikely for ATM Nifty).
**Handling:** Check `orderstatus` quantity fields. If partial fill detected, close remaining qty via market order + cancel residual SL order. Log as anomaly.

### 5. Internet goes down after entry

**What works:** Exchange SL orders are already on the exchange. They trigger independently.
**What doesn't:** Script can't detect the trigger. Script can't cancel on exit.
**Outcome:** Exchange SL protects at 45%. When internet returns, script detects via orderstatus and reconciles state. Worst case: broker's MIS square-off at 15:15.

### 6. Script restarts mid-day (crash/restart)

**Current behavior:** Script loads state from `strategy_state.json`, resumes monitoring.
**With hybrid:** State file has `exchange_sl_oid_ce/pe`. On restart, Phase 2 detection resumes from saved order IDs. No action needed — the exchange SL orders survive script restart.

### 7. One entry leg fails (orphan)

**Current behavior:** Script closes the orphan leg and returns False.
**With hybrid:** No exchange SL placed since entry failed. No change needed.

### 8. Breakeven SL modifies script's SL level

**Current behavior:** After one leg exits, breakeven SL tightens the survivor's SL.
**With hybrid:** Exchange SL stays at 45%. Script's breakeven SL is tighter (e.g., 10-15%). Script exits first, cancels exchange SL. No conflict.

### 9. Combined SL (30%) triggers — closes both legs

**Current behavior:** `close_all()` sends BUY for both CE and PE.
**With hybrid:** After `close_all()`, cancel both exchange SL orders. Order: send exits first, then cancel SLs.

### 10. Daily loss limit triggers

**Current behavior:** Script closes all positions.
**With hybrid:** Same — close all, then cancel exchange SLs in cleanup.

### 11. Exchange SL order gets auto-cancelled by broker

**Cause:** Broker may cancel pending orders at end of day or due to margin issues.
**Handling:** Phase 2 detects status = "cancelled". Log it, clear the order ID. Script's own monitoring continues as sole protection.

## Implementation Checklist

1. Add `EXCHANGE_SL_ENABLED` and `EXCHANGE_SL_PCT` config constants
2. Add `exchange_sl_oid_ce/pe` to state dict and `reset_state()`
3. Create `_place_exchange_sl()` method in OrderEngine — called after fill capture
4. Create `_cancel_exchange_sl()` method — called on every leg close
5. Create `_check_exchange_sl_status()` method — called at top of `_tick_inner()`
6. Add cleanup in `_mark_fully_flat()` and time-exit
7. Add exchange SL order IDs to state file persistence
8. Telegram notifications for: SL placed, SL triggered, SL placement failed, cancel failed

## Files Modified

- `nifty_short_straddle.py` — all changes in this single file
