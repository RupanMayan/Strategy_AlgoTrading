# Hybrid Exchange SL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exchange-level SL-M orders at 45% as a catastrophic safety net to the Nifty Short Straddle production script.

**Architecture:** After entry, place SL-M BUY orders on the exchange for both CE and PE at 45% above entry price. Script continues monitoring for smart exits (30% SL, decay, trail, etc.) as before. On any script exit, cancel the exchange SL. Each tick, check if exchange SL triggered independently. This is purely additive — no existing logic changes.

**Tech Stack:** Python, OpenAlgo API (placeorder, cancelorder, orderstatus)

**Spec:** `docs/superpowers/specs/2026-04-03-hybrid-exchange-sl-design.md`

---

### File Map

- **Modify:** `ShortStraddle/nifty_short_straddle.py`
  - Config section (~line 109): Add 2 constants
  - INITIAL_STATE (~line 284): Add 2 state fields
  - OrderEngine class (~line 708): Add 3 new methods, modify 3 existing methods

No new files. Single-file strategy.

---

### Task 1: Add Config Constants and State Fields

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py:107-110` (config section)
- Modify: `ShortStraddle/nifty_short_straddle.py:284-310` (INITIAL_STATE)

- [ ] **Step 1: Add config constants after COMBINED_SL_PCT (line 109)**

```python
# Fix 7: Combined SL — use combined premium SL when both legs active
COMBINED_SL_ENABLED          = True
COMBINED_SL_PCT              = 30.0

# ── Hybrid Exchange SL (Layer 2 catastrophic protection) ───────────────
# Places SL-M BUY orders on exchange after entry. Acts as safety net for
# flash crashes and API/internet failures. Script's own 30% SL handles
# normal exits; exchange SL at 45% only fires when script can't.
EXCHANGE_SL_ENABLED          = True
EXCHANGE_SL_PCT              = 45.0    # SL-M trigger at 45% above entry per leg
```

- [ ] **Step 2: Add state fields to INITIAL_STATE (after line 309, before closing brace)**

Add these two fields to the INITIAL_STATE dict:

```python
    "exchange_sl_oid_ce": "", "exchange_sl_oid_pe": "",
```

Insert after the `"exit_reason": "",` line.

- [ ] **Step 3: Verify script loads without errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('OK')" `

Expected: `OK` (no import errors)

- [ ] **Step 4: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add exchange SL config and state fields"
```

---

### Task 2: Add `_place_exchange_sl()` Method

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py` — OrderEngine class

- [ ] **Step 1: Add `_place_exchange_sl()` method to OrderEngine**

Add this method after the `_capture_fills()` method (after ~line 938). This method is called from `_capture_fills()` once fill prices are confirmed.

```python
    def _place_exchange_sl(self):
        """Place SL-M BUY orders on exchange as catastrophic protection (Layer 2).
        Called after fill prices are captured. Fail-open: if placement fails,
        strategy continues with script-only monitoring (same as before this feature).
        """
        if not EXCHANGE_SL_ENABLED:
            return

        for leg, price_key, oid_key in [
            ("CE", "entry_price_ce", "exchange_sl_oid_ce"),
            ("PE", "entry_price_pe", "exchange_sl_oid_pe"),
        ]:
            entry_price = state[price_key]
            if entry_price <= 0:
                plog(f"Exchange SL {leg}: no entry price — skipping", "WARNING")
                continue

            trigger = round(entry_price * (1 + EXCHANGE_SL_PCT / 100), 1)
            symbol = state[f"symbol_{leg.lower()}"]

            for attempt in range(3):
                try:
                    resp = broker.get().placeorder(
                        strategy=STRATEGY_NAME,
                        symbol=symbol,
                        exchange=OPTION_EXCH,
                        action="BUY",
                        quantity=str(qty()),
                        price_type="SL-M",
                        product=PRODUCT,
                        trigger_price=str(trigger),
                    )
                    if api_ok(resp):
                        oid = str(resp.get("orderid", ""))
                        with _monitor_lock:
                            state[oid_key] = oid
                            save_state()
                        plog(f"Exchange SL {leg}: placed SL-M BUY at trigger ₹{trigger:.1f} (order {oid})")
                        break
                    else:
                        plog(f"Exchange SL {leg} attempt {attempt+1}: {api_err(resp)}", "WARNING")
                except Exception as exc:
                    plog(f"Exchange SL {leg} attempt {attempt+1} error: {exc}", "WARNING")
                time.sleep(1)
            else:
                plog(f"Exchange SL {leg}: FAILED after 3 attempts — continuing without", "ERROR")
                telegram.notify(f"⚠️ Exchange SL {leg} placement failed — no Layer 2 protection for this leg")
```

- [ ] **Step 2: Call `_place_exchange_sl()` from `_capture_fills()` after fill prices are saved**

In the `_capture_fills()` method, at the very end (after `save_state()` on ~line 938), add the call:

Find this block at the end of `_capture_fills()`:
```python
            save_state()
```

Replace with:
```python
            save_state()

        # Place exchange SL-M orders now that we have fill prices
        self._place_exchange_sl()
```

- [ ] **Step 3: Add telegram notification for successful placement**

After both legs are placed, notify. Add at the end of `_place_exchange_sl()`:

```python
        # Summary notification
        ce_oid = state.get("exchange_sl_oid_ce", "")
        pe_oid = state.get("exchange_sl_oid_pe", "")
        if ce_oid or pe_oid:
            ce_trigger = round(state["entry_price_ce"] * (1 + EXCHANGE_SL_PCT / 100), 1) if state["entry_price_ce"] > 0 else 0
            pe_trigger = round(state["entry_price_pe"] * (1 + EXCHANGE_SL_PCT / 100), 1) if state["entry_price_pe"] > 0 else 0
            telegram.notify(
                f"🛡️ Exchange SL placed (Layer 2)\n"
                f"CE: trigger ₹{ce_trigger:.1f} ({ce_oid or 'FAILED'})\n"
                f"PE: trigger ₹{pe_trigger:.1f} ({pe_oid or 'FAILED'})"
            )
```

- [ ] **Step 4: Verify script loads without errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add _place_exchange_sl() — place SL-M on exchange after entry"
```

---

### Task 3: Add `_cancel_exchange_sl()` Method and Wire Into Exits

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py` — OrderEngine class

- [ ] **Step 1: Add `_cancel_exchange_sl()` method to OrderEngine**

Add after `_place_exchange_sl()`:

```python
    def _cancel_exchange_sl(self, leg: str):
        """Cancel exchange SL-M order for a specific leg.
        Called when script closes a leg via any of the 13 exit modules.
        Fail-safe: if cancel fails, log and continue — MIS auto-square at 15:15 is backstop.
        """
        if not EXCHANGE_SL_ENABLED:
            return

        leg_l = leg.lower()
        oid_key = f"exchange_sl_oid_{leg_l}"
        oid = state.get(oid_key, "")
        if not oid:
            return

        for attempt in range(3):
            try:
                resp = broker.get().cancelorder(
                    order_id=oid,
                    strategy=STRATEGY_NAME,
                )
                if api_ok(resp):
                    plog(f"Exchange SL {leg}: cancelled order {oid}")
                    with _monitor_lock:
                        state[oid_key] = ""
                        save_state()
                    return
                else:
                    err = api_err(resp)
                    # Order already completed or cancelled — not an error
                    if "complete" in err.lower() or "cancel" in err.lower() or "traded" in err.lower():
                        plog(f"Exchange SL {leg}: order {oid} already closed/cancelled")
                        with _monitor_lock:
                            state[oid_key] = ""
                            save_state()
                        return
                    plog(f"Exchange SL {leg} cancel attempt {attempt+1}: {err}", "WARNING")
            except Exception as exc:
                plog(f"Exchange SL {leg} cancel attempt {attempt+1} error: {exc}", "WARNING")
            time.sleep(0.5)

        plog(f"Exchange SL {leg}: CANCEL FAILED after 3 attempts (order {oid})", "ERROR")
        telegram.notify(f"🚨 Exchange SL {leg} cancel failed — order {oid} may still be active!")
        # Clear the OID anyway to avoid repeated cancel attempts
        with _monitor_lock:
            state[oid_key] = ""
            save_state()
```

- [ ] **Step 2: Wire into `close_one_leg()` — cancel exchange SL after exit order is sent**

In `close_one_leg()` (~line 940), find the block after a successful exit order:

```python
        if not order_sent:
            plog(f"CRITICAL: Could not close {leg} after 3 attempts — leg still open", "ERROR")
```

BEFORE this block (after the for loop's `break`), the order was sent. Now find the section AFTER `order_sent` is confirmed and BEFORE `_mark_fully_flat`, where the leg's state is updated. Specifically, right after line:

```python
        if fill_price <= 0:
            fill_price = current_ltp if current_ltp > 0 else fetch_ltp(symbol, OPTION_EXCH)
```

Add immediately after:

```python
        # Cancel exchange SL for this leg (script handled the exit)
        self._cancel_exchange_sl(leg)
```

- [ ] **Step 3: Wire into `_close_all_locked()` — cancel both exchange SLs**

Find `_close_all_locked()` (~line 1062):

```python
    def _close_all_locked(self, reason: str):
        state["exit_reason"] = reason
        legs = active_legs()

        for leg in legs:
            ltp = fetch_ltp(state[f"symbol_{leg.lower()}"], OPTION_EXCH)
            self.close_one_leg(leg, reason, current_ltp=ltp)
```

The `close_one_leg` calls will each cancel their own exchange SL via Step 2. No additional change needed here — the cancel is already wired into `close_one_leg`.

- [ ] **Step 4: Wire into `_mark_fully_flat()` — belt-and-suspenders cleanup**

Find `_mark_fully_flat()` (~line 1070). Add at the very start of the method, before `ws_feed.unsubscribe`:

```python
    def _mark_fully_flat(self, reason: str):
        # Belt-and-suspenders: cancel any remaining exchange SL orders
        for leg in ["CE", "PE"]:
            self._cancel_exchange_sl(leg)

        ws_feed.unsubscribe_position_symbols()
```

- [ ] **Step 5: Verify script loads without errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('OK')"`

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add _cancel_exchange_sl() and wire into all exit paths"
```

---

### Task 4: Add `_check_exchange_sl_status()` — Detect Exchange-Triggered Exits

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py` — OrderEngine and Monitor classes

- [ ] **Step 1: Add `_check_exchange_sl_status()` method to OrderEngine**

Add after `_cancel_exchange_sl()`:

```python
    def _check_exchange_sl_status(self) -> bool:
        """Check if exchange SL-M orders were triggered by the exchange.
        Called at the TOP of each monitor tick, before any exit logic.
        Returns True if any leg was closed by exchange SL (caller should re-check state).
        """
        if not EXCHANGE_SL_ENABLED:
            return False

        any_triggered = False

        for leg, oid_key, active_key in [
            ("CE", "exchange_sl_oid_ce", "ce_active"),
            ("PE", "exchange_sl_oid_pe", "pe_active"),
        ]:
            oid = state.get(oid_key, "")
            if not oid or not state.get(active_key):
                continue

            try:
                resp = broker.get().orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                if not api_ok(resp):
                    continue

                data = resp.get("data", {})
                order_status = data.get("order_status", "").lower()

                if order_status == "complete":
                    # Exchange SL triggered — leg was exited by exchange
                    fill_price = float(data.get("average_price", 0) or 0)
                    if fill_price <= 0:
                        fill_price = fetch_ltp(state[f"symbol_{leg.lower()}"], OPTION_EXCH)

                    plog(f"EXCHANGE SL TRIGGERED: {leg} filled at ₹{fill_price:.2f} (order {oid})", "WARNING")

                    leg_l = leg.lower()
                    entry_px = state[f"entry_price_{leg_l}"]
                    leg_pnl = (entry_px - fill_price) * qty() if entry_px > 0 and fill_price > 0 else 0.0

                    with _monitor_lock:
                        state[active_key] = False
                        state[f"exit_price_{leg_l}"] = fill_price
                        state["closed_pnl"] += leg_pnl
                        state[oid_key] = ""
                        state["sl_events"].append({
                            "leg": leg, "reason": "Exchange SL-M (45%)",
                            "entry": entry_px, "exit": fill_price,
                            "pnl": round(leg_pnl, 2),
                            "time": now_ist().isoformat(),
                        })

                    telegram.notify(
                        f"🛡️ EXCHANGE SL TRIGGERED — {leg}\n"
                        f"Entry: ₹{entry_px:.2f} → Exit: ₹{fill_price:.2f}\n"
                        f"Leg P&L: ₹{leg_pnl:,.2f}\n"
                        f"Closed P&L: ₹{state['closed_pnl']:,.2f}"
                    )

                    any_triggered = True

                    # Cancel the other leg's exchange SL
                    other_leg = "PE" if leg == "CE" else "CE"
                    other_oid_key = f"exchange_sl_oid_{other_leg.lower()}"
                    if state.get(other_oid_key):
                        self._cancel_exchange_sl(other_leg)

                    # Check if fully flat
                    other_active = state.get(f"{other_leg.lower()}_active", False)
                    if not other_active:
                        state["in_position"] = False
                        state["exit_reason"] = "Exchange SL-M (45%)"
                        self._mark_fully_flat("Exchange SL-M (45%)")
                    else:
                        # Activate breakeven on survivor
                        self._activate_breakeven_if_needed(other_leg.lower())
                        save_state()

                elif order_status in ("rejected", "cancelled"):
                    plog(f"Exchange SL {leg}: order {oid} was {order_status} by broker", "WARNING")
                    telegram.notify(f"⚠️ Exchange SL {leg} order {order_status} — no Layer 2 protection")
                    with _monitor_lock:
                        state[oid_key] = ""
                        save_state()

            except Exception as exc:
                # Don't block the monitor loop — retry next tick
                plog(f"Exchange SL {leg} status check error: {exc}", "WARNING")

        return any_triggered
```

- [ ] **Step 2: Call `_check_exchange_sl_status()` at the TOP of Monitor `_tick_inner()`**

Find `_tick_inner()` in the Monitor class (~line 1165):

```python
    def _tick_inner(self):
        legs = active_legs()
        if not legs:
            return
```

Replace with:

```python
    def _tick_inner(self):
        # Priority 0 (pre-check): Detect if exchange SL-M triggered independently
        if self.engine._check_exchange_sl_status():
            if not state["in_position"]:
                return
            # Refresh legs list — some legs may have been closed by exchange
        
        legs = active_legs()
        if not legs:
            return
```

- [ ] **Step 3: Verify script loads without errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: add _check_exchange_sl_status() — detect exchange-triggered exits"
```

---

### Task 5: Handle State Persistence and Script Restart

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py`

- [ ] **Step 1: Verify exchange SL order IDs survive in state file**

The `exchange_sl_oid_ce` and `exchange_sl_oid_pe` fields are in `INITIAL_STATE` (Task 1), which means they are part of `state` dict, which is saved via `save_state()` to `strategy_state.json`. On restart, `load_state()` restores them. Verify by checking `save_state` and `load_state`:

Find `save_state` and `load_state`:

```python
# These should already serialize all state keys including the new ones.
# No changes needed if save_state writes the full state dict.
```

Run: `cd ShortStraddle && python -c "
import nifty_short_straddle as ns
ns.state['exchange_sl_oid_ce'] = 'TEST123'
ns.save_state()
ns.state['exchange_sl_oid_ce'] = ''
ns.load_state()
assert ns.state['exchange_sl_oid_ce'] == 'TEST123', 'State persistence failed'
print('State persistence OK')
"`

Expected: `State persistence OK`

- [ ] **Step 2: Add exchange SL order IDs to the entry state block**

In `place_entry()` (~line 842), where state is populated after entry, add the exchange SL fields to the reset block so they start clean:

Find:
```python
            state["exit_reason"] = ""
            state["trailing_active_ce"] = False
```

Add before `state["trailing_active_ce"]`:
```python
            state["exchange_sl_oid_ce"] = ""
            state["exchange_sl_oid_pe"] = ""
```

- [ ] **Step 3: Add exchange SL OIDs to trade log**

In `_append_trade_log()` (~line 1107), add the exchange SL info to the trade record so we can track how often it triggers:

Find:
```python
                "lots": NUMBER_OF_LOTS,
            }
```

Replace with:
```python
                "lots": NUMBER_OF_LOTS,
                "exchange_sl_triggered": any(
                    e.get("reason", "").startswith("Exchange SL")
                    for e in state.get("sl_events", [])
                ),
            }
```

- [ ] **Step 4: Verify script loads without errors**

Run: `cd ShortStraddle && python -c "import nifty_short_straddle; print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: exchange SL state persistence and trade log tracking"
```

---

### Task 6: Final Integration Test and Production Verification

**Files:**
- Modify: `ShortStraddle/nifty_short_straddle.py` (if any fixes needed)

- [ ] **Step 1: Verify all new methods exist and are wired correctly**

Run:
```bash
cd ShortStraddle && python -c "
import nifty_short_straddle as ns

# Check config
assert ns.EXCHANGE_SL_ENABLED == True
assert ns.EXCHANGE_SL_PCT == 45.0
print(f'Config OK: SL={ns.EXCHANGE_SL_PCT}%')

# Check state fields
assert 'exchange_sl_oid_ce' in ns.INITIAL_STATE
assert 'exchange_sl_oid_pe' in ns.INITIAL_STATE
print('State fields OK')

# Check methods exist on OrderEngine
engine = ns.OrderEngine
assert hasattr(engine, '_place_exchange_sl')
assert hasattr(engine, '_cancel_exchange_sl')
assert hasattr(engine, '_check_exchange_sl_status')
print('Methods OK')

print('All checks passed')
"
```

Expected: All checks passed

- [ ] **Step 2: Verify feature can be disabled**

The `EXCHANGE_SL_ENABLED = False` should skip all exchange SL logic. Verify each method has the guard:

```bash
cd ShortStraddle && grep -n "EXCHANGE_SL_ENABLED" nifty_short_straddle.py
```

Expected: Should show guards in `_place_exchange_sl`, `_cancel_exchange_sl`, `_check_exchange_sl_status`, and the config line.

- [ ] **Step 3: Review the complete change with git diff**

```bash
git diff HEAD~5 -- ShortStraddle/nifty_short_straddle.py
```

Review for:
- No existing logic modified (only additions)
- All 3 new methods have `EXCHANGE_SL_ENABLED` guard
- Cancel is called in `close_one_leg`, `_mark_fully_flat`
- Detection is called at top of `_tick_inner`
- Placement is called at end of `_capture_fills`

- [ ] **Step 4: Final commit with all changes**

```bash
git add ShortStraddle/nifty_short_straddle.py
git commit -m "feat: hybrid exchange SL — complete implementation with 45% SL-M safety net

Places SL-M BUY orders on exchange after entry at 45% above entry price.
Acts as catastrophic protection for flash crashes and API/internet failures.
Script's 13 exit modules handle normal exits; exchange SL is Layer 2 insurance.

New methods: _place_exchange_sl(), _cancel_exchange_sl(), _check_exchange_sl_status()
Feature toggle: EXCHANGE_SL_ENABLED (default True)"
```

---

## Execution Order

```
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6
  │         │         │         │         │        │
Config   Place SL  Cancel SL  Detect   State    Verify
& State            on exit    trigger  persist
```

Each task is independently committable and the script remains functional after each commit.
