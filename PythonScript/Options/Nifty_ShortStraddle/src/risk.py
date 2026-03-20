"""
src/risk.py  —  MarginGuard + TrailingSLEngine
═══════════════════════════════════════════════════════════════════════
Two risk-control classes with no cross-dependency:

  MarginGuard      — Pre-trade capital / margin sufficiency check.
                     Calls funds() + margin() before every entry.

  TrailingSLEngine — Per-leg trailing SL state machine.
                     Phase 1: activation when LTP decays to trigger %.
                     Phase 2: tighten on every tick while trailing active.
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from src._shared import (
    cfg, state, save_state,
    info, warn, debug, sep,
    telegram,
    _get_client,
    INDEX_EXCH, OPTION_EXCH,
    qty, now_ist,
    is_api_success, get_api_error,
)


class MarginGuard:
    """
    Pre-trade capital and margin sufficiency check.

    Called once per entry attempt (after all entry filters pass) to verify
    the account has enough funds to absorb the straddle margin requirement
    plus a configurable buffer (cfg.MARGIN_BUFFER).

    Two API calls:
      1. client.funds()  → available cash + collateral
      2. client.margin() → basket margin for SELL CE + SELL PE MIS

    Both calls respect cfg.MARGIN_GUARD_FAIL_OPEN — on API failure the guard
    either blocks (fail-closed, production default) or permits (fail-open,
    paper-trade / testing mode).
    """

    def _get_atm_strike(self, expiry: str) -> str:
        """
        Fetch NIFTY spot LTP and return the nearest ATM strike as a string.

        Used only to construct the margin basket symbols — NOT for order entry.
        Falls back to "23000" on any failure; a slightly-off strike produces a
        close-enough margin estimate for the sufficiency check.
        """
        try:
            q = _get_client().quotes(symbol=cfg.UNDERLYING, exchange=INDEX_EXCH)
            if is_api_success(q):
                ltp = float(q.get("data", {}).get("ltp", 0))
                if ltp > 0:
                    atm = round(ltp / cfg.ATM_STRIKE_ROUNDING) * cfg.ATM_STRIKE_ROUNDING
                    debug(f"ATM strike for margin check: {atm}  (LTP: {ltp:.2f})")
                    return str(int(atm))
        except Exception as exc:
            warn(f"ATM LTP fetch failed: {exc}")
        return "23000"

    def check(self, expiry: str) -> bool:
        """
        Pre-trade margin guard.

        Step 1 : GET available capital via client.funds()
        Step 2 : GET basket margin via client.margin() for CE + PE SELL MIS
        Step 3 : Sufficiency check — total_available >= required × cfg.MARGIN_BUFFER

        Returns True (proceed to entry) or False (skip trade).
        Stores margin_required and margin_available in state for Telegram logging.
        """
        if not cfg.MARGIN_GUARD_ENABLED:
            info("Margin guard disabled — skipping pre-trade margin check")
            return True

        sep()
        info("PRE-TRADE MARGIN CHECK")

        # ── Step 1: Available capital (cash + collateral) ─────────────────────
        available_cash = 0.0
        collateral     = 0.0
        utilised       = 0.0

        try:
            funds_resp = _get_client().funds()
            if is_api_success(funds_resp):
                data           = funds_resp.get("data", {})
                available_cash = float(data.get("availablecash",  0) or 0)
                collateral     = float(data.get("collateral",     0) or 0)
                utilised       = float(data.get("utiliseddebits", 0) or 0)
                info(f"  Available cash  : Rs.{available_cash:,.2f}")
                info(f"  Collateral      : Rs.{collateral:,.2f}  (pledged securities)")
                info(f"  Utilised debits : Rs.{utilised:,.2f}  (existing margin)")
            else:
                warn(f"funds() failed: {get_api_error(funds_resp)}")
                if cfg.MARGIN_GUARD_FAIL_OPEN:
                    warn("Margin guard fail-open: proceeding despite funds() failure")
                    return True
                warn("Margin guard fail-closed: skipping trade due to funds() failure")
                telegram("Margin guard: funds() API failed — trade SKIPPED (fail-closed mode)")
                return False
        except Exception as exc:
            warn(f"funds() exception: {exc}")
            if cfg.MARGIN_GUARD_FAIL_OPEN:
                warn("Margin guard fail-open: proceeding despite exception")
                return True
            telegram(f"Margin guard: funds() exception — trade SKIPPED\n{exc}")
            return False

        total_available = available_cash + collateral
        info(f"  Total available : Rs.{total_available:,.2f}  (cash + collateral)")

        # ── Step 2: Basket margin for straddle ───────────────────────────────
        required_margin = 0.0
        span_margin     = 0.0
        exposure_margin = 0.0
        atm_strike      = self._get_atm_strike(expiry)

        ce_symbol = f"{cfg.UNDERLYING}{expiry}{atm_strike}CE"
        pe_symbol = f"{cfg.UNDERLYING}{expiry}{atm_strike}PE"

        info(f"  Margin symbols  : {ce_symbol} + {pe_symbol}")
        info(f"  Qty/leg: {qty()}  |  Product: {cfg.PRODUCT}")

        try:
            margin_resp = _get_client().margin(
                positions=[
                    {
                        "symbol"    : ce_symbol,
                        "exchange"  : OPTION_EXCH,
                        "action"    : "SELL",
                        "product"   : cfg.PRODUCT,
                        "pricetype" : "MARKET",
                        "quantity"  : str(qty()),
                        "price"     : "0",
                    },
                    {
                        "symbol"    : pe_symbol,
                        "exchange"  : OPTION_EXCH,
                        "action"    : "SELL",
                        "product"   : cfg.PRODUCT,
                        "pricetype" : "MARKET",
                        "quantity"  : str(qty()),
                        "price"     : "0",
                    },
                ]
            )

            if is_api_success(margin_resp):
                margin_data     = margin_resp.get("data", {})
                required_margin = float(margin_data.get("total_margin_required", 0) or 0)
                span_margin     = float(margin_data.get("span_margin",     0) or 0)
                exposure_margin = float(margin_data.get("exposure_margin", 0) or 0)
                info(f"  SPAN margin     : Rs.{span_margin:,.2f}")
                info(f"  Exposure margin : Rs.{exposure_margin:,.2f}")
                info(f"  Required total  : Rs.{required_margin:,.2f}")
            else:
                warn(f"margin() failed: {get_api_error(margin_resp)}")
                if cfg.MARGIN_GUARD_FAIL_OPEN:
                    warn("Margin guard fail-open: proceeding despite margin() failure")
                    return True
                telegram("Margin guard: margin() API failed — trade SKIPPED")
                return False

        except Exception as exc:
            warn(f"margin() exception: {exc}")
            if cfg.MARGIN_GUARD_FAIL_OPEN:
                warn("Margin guard fail-open: proceeding despite exception")
                return True
            telegram(f"Margin guard: margin() exception — trade SKIPPED\n{exc}")
            return False

        # ── Step 3: Sufficiency check ─────────────────────────────────────────
        if required_margin <= 0:
            warn("Margin API returned zero — treating as unavailable, proceeding")
            return True

        required_with_buffer = required_margin * cfg.MARGIN_BUFFER
        sufficient           = total_available >= required_with_buffer
        surplus_or_shortfall = total_available - required_with_buffer

        state["margin_required"]  = required_margin
        state["margin_available"] = total_available

        buffer_pct = int((cfg.MARGIN_BUFFER - 1) * 100)

        if sufficient:
            info(
                f"  MARGIN CHECK: PASS ✓  "
                f"Available Rs.{total_available:,.0f}  |  "
                f"Required Rs.{required_margin:,.0f} "
                f"(+{buffer_pct}% = Rs.{required_with_buffer:,.0f})  |  "
                f"Surplus Rs.{surplus_or_shortfall:,.0f}"
            )
            sep()
            return True
        else:
            warn(
                f"  MARGIN CHECK: FAIL ✗  "
                f"Available Rs.{total_available:,.0f}  |  "
                f"Required Rs.{required_margin:,.0f} "
                f"(+{buffer_pct}% = Rs.{required_with_buffer:,.0f})  |  "
                f"Shortfall Rs.{abs(surplus_or_shortfall):,.0f}"
            )
            sep()
            telegram(
                f"⚠️ MARGIN INSUFFICIENT — trade SKIPPED\n"
                f"Available  : Rs.{total_available:,.0f}\n"
                f"  Cash     : Rs.{available_cash:,.0f}\n"
                f"  Collateral: Rs.{collateral:,.0f}\n"
                f"Required   : Rs.{required_margin:,.0f}\n"
                f"  +{buffer_pct}% buffer = Rs.{required_with_buffer:,.0f}\n"
                f"Shortfall  : Rs.{abs(surplus_or_shortfall):,.0f}\n"
                f"Action: Add funds or reduce NUMBER_OF_LOTS."
            )
            return False


class TrailingSLEngine:
    """
    Per-leg trailing stop-loss state machine.

    Called from Monitor.run_tick() AFTER LTP is confirmed valid and BEFORE
    sl_level() is evaluated — guaranteeing the monitor always uses the freshest
    effective SL.

    Two phases per leg per trade:

    Phase 1 — Activation (fires once):
      Condition : trailing not yet active AND ltp <= entry_px × (TRAIL_TRIGGER_PCT/100)
      Action    :
        1. Compute initial trailing SL = round(ltp × (1 + TRAIL_LOCK_PCT/100), 2)
        2. Safety cap: if initial_trail_sl >= fixed_sl → cap at fixed_sl
        3. Set state["trailing_active_{leg}"] = True
        4. Set state["trailing_sl_{leg}"]     = initial_trail_sl
        5. Send Telegram alert (one-time)
        6. save_state() — persist immediately (critical state)

    Phase 2 — Tightening (fires every tick while trailing is active):
      Condition : trailing already active AND new_trail_sl < current_trail_sl
      Action    :
        1. Compute new_trail_sl = round(ltp × (1 + TRAIL_LOCK_PCT/100), 2)
        2. Update state["trailing_sl_{leg}"]
        3. save_state() — FIX-III (v5.5.0): persist every tightening
    """

    def update(self, leg: str, ltp: float, entry_px: float) -> None:
        """
        Update trailing SL state for one leg based on current LTP.

        Parameters
        ----------
        leg      : 'CE' or 'PE'
        ltp      : confirmed live LTP (caller guarantees ltp > 0)
        entry_px : entry fill price for this leg (caller guarantees entry_px > 0)
        """
        if not cfg.TRAILING_SL_ENABLED:
            return

        leg_key      = leg.lower()
        leg_label    = leg.upper()
        active_key   = f"trailing_active_{leg_key}"
        trail_sl_key = f"trailing_sl_{leg_key}"

        is_trailing   = state.get(active_key, False)
        trigger_price = round(entry_px * (cfg.TRAIL_TRIGGER_PCT / 100.0), 2)

        if not is_trailing:
            # ── Phase 1: Activation check ─────────────────────────────────────
            if ltp > trigger_price:
                return   # Not yet in profit zone

            initial_trail_sl = round(ltp * (1.0 + cfg.TRAIL_LOCK_PCT / 100.0), 2)

            # Safety cap: trailing SL must never be above (worse than) fixed SL
            fixed_sl = round(entry_px * (1.0 + cfg.LEG_SL_PERCENT / 100.0), 2)
            if initial_trail_sl >= fixed_sl:
                warn(
                    f"Trailing SL [{leg_label}]: initial trail SL Rs.{initial_trail_sl:.2f} "
                    f">= fixed SL Rs.{fixed_sl:.2f} — "
                    f"TRAIL_LOCK_PCT={cfg.TRAIL_LOCK_PCT}% may be too high. "
                    f"Capping at fixed SL Rs.{fixed_sl:.2f}."
                )
                telegram(
                    f"⚠️ TRAILING SL CONFIG WARNING — {leg_label} leg\n"
                    f"Initial trail SL Rs.{initial_trail_sl:.2f} was WORSE than fixed SL "
                    f"Rs.{fixed_sl:.2f}.\n"
                    f"Capped at fixed SL.\n"
                    f"TRAIL_LOCK_PCT={cfg.TRAIL_LOCK_PCT}% appears too high — "
                    f"review Section 7C configuration."
                )
                initial_trail_sl = fixed_sl

            locked_profit_per_unit = round(entry_px - initial_trail_sl, 2)
            locked_profit_total    = round(locked_profit_per_unit * qty(), 0)
            decay_pct              = round((entry_px - ltp) / entry_px * 100.0, 1)

            state[active_key]   = True
            state[trail_sl_key] = initial_trail_sl

            lock_multiplier = 1.0 + cfg.TRAIL_LOCK_PCT / 100.0
            info(
                f"TRAILING SL ACTIVATED [{leg_label}]  "
                f"LTP Rs.{ltp:.2f} ≤ trigger Rs.{trigger_price:.2f} "
                f"({cfg.TRAIL_TRIGGER_PCT}% of entry Rs.{entry_px:.2f}, "
                f"{decay_pct}% decayed)"
            )
            info(
                f"  Trailing SL set : Rs.{initial_trail_sl:.2f}  "
                f"(Rs.{ltp:.2f} × {lock_multiplier:.2f})"
            )
            info(
                f"  Profit locked   : Rs.{locked_profit_per_unit:.2f}/unit  "
                f"× {qty()} = Rs.{locked_profit_total:.0f}  "
                f"(min guaranteed if SL fires)"
            )
            info(
                f"  Fixed SL was    : Rs.{fixed_sl:.2f}  — "
                f"trailing SL is now Rs.{fixed_sl - initial_trail_sl:.2f} more favourable"
            )

            telegram(
                f"🔒 TRAILING SL ACTIVATED — {leg_label} leg\n"
                f"Entry price : Rs.{entry_px:.2f}\n"
                f"LTP now     : Rs.{ltp:.2f}  ({decay_pct}% decayed)\n"
                f"Trigger was : Rs.{trigger_price:.2f}  ({cfg.TRAIL_TRIGGER_PCT}% of entry)\n"
                f"Trailing SL : Rs.{initial_trail_sl:.2f}  (LTP × {lock_multiplier:.2f})\n"
                f"Fixed SL was: Rs.{fixed_sl:.2f}\n"
                f"Min profit locked: Rs.{locked_profit_per_unit:.2f}/unit "
                f"× {qty()} = Rs.{locked_profit_total:.0f}\n"
                f"SL will tighten further as premium continues to decay."
            )

            save_state()   # Activation is critical state — persist immediately

        else:
            # ── Phase 2: Tighten trailing SL (SL only moves DOWN) ─────────────
            new_trail_sl     = round(ltp * (1.0 + cfg.TRAIL_LOCK_PCT / 100.0), 2)
            current_trail_sl = state.get(trail_sl_key, 0.0)

            if new_trail_sl >= current_trail_sl:
                return   # SL would move up (worse) or stay — do nothing

            locked_profit_per_unit = round(entry_px - new_trail_sl, 2)
            state[trail_sl_key]    = new_trail_sl

            debug(
                f"  Trailing SL [{leg_label}] tightened: "
                f"Rs.{current_trail_sl:.2f} → Rs.{new_trail_sl:.2f}  "
                f"(LTP Rs.{ltp:.2f} × {1.0 + cfg.TRAIL_LOCK_PCT / 100.0:.2f})  "
                f"min profit now Rs.{locked_profit_per_unit:.2f}/unit"
            )

            # FIX-III (v5.5.0): Persist every Phase 2 tightening.
            save_state()
