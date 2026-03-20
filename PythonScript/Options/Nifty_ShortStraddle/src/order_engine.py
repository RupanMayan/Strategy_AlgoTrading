"""
src/order_engine.py  —  OrderEngine class
═══════════════════════════════════════════════════════════════════════
Owns ALL broker order operations and trade lifecycle management.

  Entry   : place_entry()  — optionsmultiorder(), state init, fill capture
  Partial : close_one_leg() — BUY MARKET, breakeven SL, partial vs full exit
  Full    : close_all(), _close_all_locked() — atomic closeposition() + fallback
  Safety  : emergency_close_all() — best-effort close for orphan positions
  Log     : _append_trade_log(), _mark_fully_flat() — trade completion
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import TYPE_CHECKING

from src._shared import (
    cfg, state, save_state, clear_state_file,
    info, warn, error, debug, sep,
    telegram,
    _get_client,
    OPTION_EXCH, IST,
    _monitor_lock,
    now_ist, qty, active_legs, sl_level, _dynamic_sl_percent,
    is_api_success, get_api_error, parse_ist_datetime,
)

# Module-level globals shared with other modules via _shared
import src._shared as _shared

if TYPE_CHECKING:
    from src.risk import TrailingSLEngine


class OrderEngine:
    """
    Owns ALL broker order operations and trade lifecycle management.

    Responsibilities:
      Entry   : place_entry()  — optionsmultiorder(), state init, fill capture
      Partial : close_one_leg() — BUY MARKET, breakeven SL, partial vs full exit
      Full    : close_all(), _close_all_locked() — atomic closeposition() + fallback
      Safety  : emergency_close_all() — best-effort close for orphan positions
      Log     : _append_trade_log(), _mark_fully_flat() — trade completion

    Depends on TrailingSLEngine for SL state — passed at construction.
    """

    def __init__(self, trailing_sl: "TrailingSLEngine") -> None:
        self._trailing_sl = trailing_sl

    # ── Entry ─────────────────────────────────────────────────────────────────

    def place_entry(self, expiry: str) -> bool:
        """
        Place both legs atomically via optionsmultiorder().

        Parameters
        ----------
        expiry : expiry string already resolved by StrategyCore.job_entry()
                 e.g. "25MAR26" — passed to avoid a double get_expiry() call.

        Success path:
          • Both legs filled → ce_active=True, pe_active=True
          • Initial state saved immediately (fills pending)
          • Fill capture + entry notification launched in background thread
          • Returns True

        Failure paths:
          • API exception / both legs rejected → Returns False
          • One leg filled, other failed → emergency close, Returns False
        """
        sep()
        info("PLACING ENTRY — Short ATM Straddle  [PARTIAL SQUARE OFF]")
        info(f"  Underlying  : {cfg.UNDERLYING}  |  Exchange  : {cfg.EXCHANGE}")
        info(f"  Expiry      : {expiry}  |  Offset : {cfg.STRIKE_OFFSET}")
        info(f"  Product     : {cfg.PRODUCT}  |  Qty/leg : {qty()}")
        info(f"  CE SL will  = entry_price_CE × {1 + cfg.LEG_SL_PERCENT / 100:.2f}")
        info(f"  PE SL will  = entry_price_PE × {1 + cfg.LEG_SL_PERCENT / 100:.2f}")
        info("  Each leg managed INDEPENDENTLY — partial exit when one SL fires")
        sep()

        try:
            resp = _get_client().optionsmultiorder(
                strategy   = cfg.STRATEGY_NAME,
                underlying = cfg.UNDERLYING,
                exchange   = cfg.EXCHANGE,
                legs       = [
                    {
                        "offset"      : cfg.STRIKE_OFFSET,
                        "option_type" : "CE",
                        "action"      : "SELL",
                        "quantity"    : qty(),
                        "expiry_date" : expiry,
                        "product"     : cfg.PRODUCT,
                        "pricetype"   : "MARKET",
                        "splitsize"   : 0,
                    },
                    {
                        "offset"      : cfg.STRIKE_OFFSET,
                        "option_type" : "PE",
                        "action"      : "SELL",
                        "quantity"    : qty(),
                        "expiry_date" : expiry,
                        "product"     : cfg.PRODUCT,
                        "pricetype"   : "MARKET",
                        "splitsize"   : 0,
                    },
                ],
            )
        except Exception as exc:
            error(f"optionsmultiorder exception: {exc}")
            telegram(f"ENTRY EXCEPTION\n{exc}")
            return False

        if not is_api_success(resp):
            error(f"Entry FAILED: {get_api_error(resp)}")
            telegram(f"ENTRY FAILED\n{get_api_error(resp)}")
            return False

        # ── Parse per-leg results ─────────────────────────────────────────────
        results     = resp.get("results", [])
        filled_legs = {}

        for leg in results:
            opt = leg.get("option_type", "")
            if leg.get("status") == "success":
                filled_legs[opt] = {
                    "symbol"  : leg.get("symbol",  ""),
                    "orderid" : leg.get("orderid", ""),
                    "mode"    : leg.get("mode",    "live"),
                }
                info(
                    f"  LEG {opt} OK  | {leg.get('symbol')} "
                    f"| orderid: {leg.get('orderid')} "
                    f"| mode: {leg.get('mode', 'live').upper()}"
                )
            else:
                error(f"  LEG {opt} FAILED: {leg.get('message', 'Unknown error')}")

        # ── Partial / zero fill → emergency close ────────────────────────────
        if "CE" not in filled_legs or "PE" not in filled_legs:
            n_filled = len(filled_legs)
            if n_filled == 0:
                error("ENTRY FAILED — both legs rejected. No positions opened.")
                telegram(
                    "ENTRY FAILED — both legs rejected "
                    "(check lot size / funds). No positions opened."
                )
            else:
                filled_leg  = next(iter(filled_legs))
                missing_leg = "PE" if filled_leg == "CE" else "CE"
                error(
                    f"PARTIAL ENTRY FILL — {filled_leg} placed but {missing_leg} failed. "
                    f"Emergency close triggered."
                )
                telegram(
                    f"PARTIAL ENTRY FILL — {filled_leg} placed, {missing_leg} failed. "
                    f"Emergency close triggered. Check logs."
                )
                close_ok = self.emergency_close_all()
                if not close_ok:
                    error(
                        f"CRITICAL: Emergency close FAILED for {filled_leg}. "
                        f"Position is ORPHANED at broker with no monitoring. "
                        f"MANUAL CLOSE REQUIRED immediately in broker terminal."
                    )
            return False

        # ── Populate state — BOTH legs now active ─────────────────────────────
        _shared._monitor_state.first_tick_fired             = False
        _shared._monitor_state.consecutive_quote_fail_ticks = 0
        _shared._monitor_state.quote_fail_alerted           = False

        now_dt = now_ist()
        state["in_position"]        = True
        state["ce_active"]          = True
        state["pe_active"]          = True
        state["symbol_ce"]          = filled_legs["CE"]["symbol"]
        state["symbol_pe"]          = filled_legs["PE"]["symbol"]
        state["orderid_ce"]         = filled_legs["CE"]["orderid"]
        state["orderid_pe"]         = filled_legs["PE"]["orderid"]
        # FIX-XXII: Prefer fresh NIFTY spot fetch over API response value.
        # The optionsmultiorder response's underlying_ltp can be stale (pre-fill
        # snapshot) on volatile opens, skewing the spot-move exit threshold.
        _api_spot = float(resp.get("underlying_ltp", 0))
        try:
            _fresh_q = _get_client().quotes(symbol=cfg.UNDERLYING, exchange="NSE_INDEX")
            if is_api_success(_fresh_q):
                _fresh_spot = float(_fresh_q.get("data", {}).get("ltp", 0) or 0)
                if _fresh_spot > 0:
                    state["underlying_ltp"] = _fresh_spot
                    if abs(_fresh_spot - _api_spot) > 5:
                        debug(
                            f"  Spot corrected: API response had Rs.{_api_spot:.2f}, "
                            f"fresh quote Rs.{_fresh_spot:.2f}"
                        )
                else:
                    state["underlying_ltp"] = _api_spot
            else:
                state["underlying_ltp"] = _api_spot
        except Exception:
            state["underlying_ltp"] = _api_spot
        state["entry_time"]         = now_dt.isoformat()
        state["entry_date"]         = now_dt.strftime("%Y-%m-%d")
        # FIX-XVII: Preserve cumulative daily P&L set by strategy_core on re-entry.
        # On first trade, these are already 0.0. On re-entry, strategy_core set
        # them to the carry-forward cumulative value — do NOT reset to 0.
        # Only reset exit_reason (new trade starts fresh).
        state["exit_reason"]        = ""
        state["trailing_active_ce"] = False
        state["trailing_active_pe"] = False
        state["trailing_sl_ce"]     = 0.0
        state["trailing_sl_pe"]     = 0.0
        # FIX-XVIII: Clear stale breakeven state from previous trade (critical for re-entry).
        # Without this, a re-entry could inherit breakeven SL from the prior trade and
        # trigger an immediate SL hit on the new position.
        state["breakeven_active_ce"]       = False
        state["breakeven_active_pe"]       = False
        state["breakeven_sl_ce"]           = 0.0
        state["breakeven_sl_pe"]           = 0.0
        state["breakeven_activated_at_ce"] = None
        state["breakeven_activated_at_pe"] = None
        # FIX-XX: Clear Net P&L Guard deferral timestamps from previous trade
        state["net_pnl_defer_start_ce"]    = None
        state["net_pnl_defer_start_pe"]    = None

        # FIX-I (v5.5.0): save BEFORE fill capture — crash during fill capture
        # still leaves a valid in_position=True state file.
        save_state()
        info(f"  Initial state saved → {cfg.STATE_FILE}  (fills pending, SL arms on next tick)")

        # Launch fill capture + entry notification in background daemon thread.
        # place_entry() returns immediately so the monitor can start protecting
        # within MONITOR_INTERVAL_S seconds.
        threading.Thread(
            target = self._capture_fills_and_notify,
            args   = (expiry, results),
            daemon = True,
        ).start()

        return True

    # ── Fill capture ──────────────────────────────────────────────────────────

    def _capture_fill_one_leg(self, leg: str, oid: str) -> bool:
        """
        Fetch average fill price for one leg with linear back-off retry.
        Writes state["entry_price_{leg}"] on success. Returns True if filled.
        Called concurrently for CE and PE (ThreadPoolExecutor).
        """
        MAX_ATTEMPTS = 5
        RETRY_DELAYS = [1.0, 2.0, 3.0, 4.0]

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = _get_client().orderstatus(
                    order_id = oid,
                    strategy = cfg.STRATEGY_NAME,
                )
                if is_api_success(resp):
                    avg_px = float(resp.get("data", {}).get("average_price", 0) or 0)
                    if avg_px > 0:
                        state[f"entry_price_{leg.lower()}"] = avg_px
                        info(
                            f"  Fill [{leg}]: Rs.{avg_px:.2f}  "
                            f"(orderid: {oid}  attempt: {attempt})"
                        )
                        return True
                    else:
                        debug(f"  Fill [{leg}]: avg_px=0 on attempt {attempt}, retrying...")
                else:
                    debug(f"  orderstatus [{leg}] attempt {attempt} failed: {get_api_error(resp)}")
            except Exception as exc:
                debug(f"  orderstatus [{leg}] attempt {attempt} exception: {exc}")

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAYS[attempt - 1])

        return False

    def _capture_fill_prices(self) -> None:
        """
        Fetch average fill prices for both legs concurrently via ThreadPoolExecutor.

        Wall-clock time ≈ max(ce_time, pe_time) instead of sequential ce + pe.
        Failure leaves entry_price at 0.0 — SL disabled for that leg until fixed.
        """
        legs = [("CE", state["orderid_ce"]), ("PE", state["orderid_pe"])]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(self._capture_fill_one_leg, leg, oid): leg
                for leg, oid in legs
            }
            results = {futures[f]: f.result() for f in as_completed(futures)}

        for leg, oid in legs:
            if not results.get(leg, False):
                warn(
                    f"  Fill price [{leg}] unavailable after all attempts "
                    f"— SL DISABLED for this leg. MANUAL MONITORING REQUIRED."
                )
                telegram(
                    f"⚠️ FILL PRICE CAPTURE FAILED — {leg} leg\n"
                    f"SL is disabled for this leg.\n"
                    f"Order ID: {oid}\n"
                    f"MANUAL MONITORING REQUIRED until position is closed."
                )

    def _capture_fills_and_notify(self, expiry: str, results: list) -> None:
        """
        Background daemon thread target — runs after place_entry() returns.

        Captures fills, saves state, then sends the entry Telegram notification.
        Thread-safe: state["entry_price_*"] are GIL-atomic float assignments.
        """
        self._capture_fill_prices()
        save_state()

        trade_mode = (results[0].get("mode", "live") if results else "live").upper()
        sl_ce      = sl_level("CE")
        sl_pe      = sl_level("PE")

        info("ENTRY COMPLETE (fills captured)")
        info(
            f"  Mode      : {trade_mode}  "
            f"NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']:.2f}"
        )
        if cfg.IVR_FILTER_ENABLED or cfg.IVP_FILTER_ENABLED:
            _ivr = "N/A" if state["ivr_at_entry"] < 0 else f"{state['ivr_at_entry']:.1f}"
            _ivp = "N/A" if state["ivp_at_entry"] < 0 else f"{state['ivp_at_entry']:.1f}%"
            info(f"  IVR       : {_ivr}  |  IVP: {_ivp}")
        info(
            f"  CE        : {state['symbol_ce']}  "
            f"fill Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_ce:.2f}"
        )
        info(
            f"  PE        : {state['symbol_pe']}  "
            f"fill Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_pe:.2f}"
        )
        info(
            f"  Margin used : Rs.{state['margin_required']:,.0f}  |  "
            f"Available was: Rs.{state['margin_available']:,.0f}"
        )
        info(f"  State persisted → {cfg.STATE_FILE}")
        sep()

        ivr_ivp_line = ""
        if cfg.IVR_FILTER_ENABLED or cfg.IVP_FILTER_ENABLED:
            _ivr_tg = "N/A" if state["ivr_at_entry"] < 0 else f"{state['ivr_at_entry']:.1f}"
            _ivp_tg = "N/A" if state["ivp_at_entry"] < 0 else f"{state['ivp_at_entry']:.1f}%"
            ivr_ivp_line = f"IVR: {_ivr_tg}  |  IVP: {_ivp_tg}\n"

        telegram(
            f"✅ ENTRY PLACED [{trade_mode}]\n"
            f"NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']:.2f}\n"
            f"{ivr_ivp_line}"
            f"CE : {state['symbol_ce']}\n"
            f"  Fill Rs.{state['entry_price_ce']:.2f}  |  SL @ Rs.{sl_ce:.2f}\n"
            f"PE : {state['symbol_pe']}\n"
            f"  Fill Rs.{state['entry_price_pe']:.2f}  |  SL @ Rs.{sl_pe:.2f}\n"
            f"Expiry: {expiry}  Qty/leg: {qty()}\n"
            f"Margin used: Rs.{state['margin_required']:,.0f}"
        )

    # ── Single-leg close ──────────────────────────────────────────────────────

    def _fetch_close_fill_price(self, orderid: str) -> float:
        """
        Fetch actual average fill price of a close (BUY MARKET) order.

        FIX-IV (v5.5.0): close_one_leg() previously used current_ltp as fill
        price for P&L. In fast markets BUY MARKET fills Rs.5–15 above trigger LTP,
        understating losses by Rs.325–975 per lot.

        Returns avg fill price (> 0) on success, 0.0 on failure (caller falls
        back to current_ltp approximation).  2 attempts, 1s gap.
        """
        for attempt in range(1, 3):
            try:
                resp = _get_client().orderstatus(
                    order_id = orderid,
                    strategy = cfg.STRATEGY_NAME,
                )
                if is_api_success(resp):
                    avg_px = float(resp.get("data", {}).get("average_price", 0) or 0)
                    if avg_px > 0:
                        return avg_px
            except Exception as exc:
                debug(f"  close fill fetch attempt {attempt} exception: {exc}")
            if attempt < 2:
                time.sleep(1.0)
        return 0.0

    def close_one_leg(
        self,
        leg: str,
        reason: str,
        current_ltp: float = 0.0,
    ) -> None:
        """
        Close a single leg (CORE OF PARTIAL SQUARE OFF).

        Parameters
        ----------
        leg         : 'CE' or 'PE'
        reason      : human-readable close reason for logs and Telegram
        current_ltp : last known LTP for approximate P&L (0.0 = unavailable)

        On order failure: logs + Telegram alert, returns WITHOUT changing state.
        Position remains open — operator must intervene manually.

        On success:
          • Records actual fill price (via orderstatus retry) for accurate P&L
          • Updates closed_pnl, marks leg inactive
          • If other leg still active: activates breakeven SL if applicable,
            saves state, sends partial-exit Telegram
          • If other leg also closed: calls _mark_fully_flat()
        """
        leg_upper  = leg.upper()
        active_key = f"{leg.lower()}_active"
        symbol_key = f"symbol_{leg.lower()}"
        entry_key  = f"entry_price_{leg.lower()}"

        if not state[active_key]:
            warn(f"close_one_leg({leg_upper}) — already closed, skipping")
            return

        symbol     = state[symbol_key]
        entry_px   = state[entry_key]
        approx_pnl = (
            (entry_px - current_ltp) * qty()
            if (entry_px > 0 and current_ltp > 0) else 0.0
        )

        sep()
        info(f"CLOSING {leg_upper} LEG  |  Reason: {reason}")
        info(f"  Symbol      : {symbol}")
        info(f"  Entry       : Rs.{entry_px:.2f}  |  LTP (approx): Rs.{current_ltp:.2f}")
        info(f"  Approx P&L  : Rs.{approx_pnl:.0f}")

        # ── Place BUY MARKET with retry (FIX-XXI) ────────────────────────────
        # SL exits are critical — retry up to 3 times with 1s backoff before
        # falling back to manual intervention. Prevents missed close when the
        # first attempt fails due to transient network/broker issue.
        MAX_CLOSE_ATTEMPTS = 3
        CLOSE_RETRY_DELAY  = 1.0
        resp = None
        last_error = ""

        for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
            try:
                resp = _get_client().placeorder(
                    strategy  = cfg.STRATEGY_NAME,
                    symbol    = symbol,
                    exchange  = OPTION_EXCH,
                    action    = "BUY",
                    quantity  = qty(),
                    pricetype = "MARKET",
                    product   = cfg.PRODUCT,
                    price     = 0,
                )
                if is_api_success(resp):
                    if attempt > 1:
                        info(f"  Close order succeeded on attempt {attempt}")
                    break  # Success — exit retry loop
                last_error = get_api_error(resp)
                warn(f"close_one_leg({leg_upper}) attempt {attempt}/{MAX_CLOSE_ATTEMPTS} rejected: {last_error}")
            except Exception as exc:
                last_error = str(exc)
                warn(f"close_one_leg({leg_upper}) attempt {attempt}/{MAX_CLOSE_ATTEMPTS} exception: {exc}")
                resp = None

            if attempt < MAX_CLOSE_ATTEMPTS:
                time.sleep(CLOSE_RETRY_DELAY)

        if resp is None or not is_api_success(resp):
            error(f"close_one_leg({leg_upper}) FAILED after {MAX_CLOSE_ATTEMPTS} attempts: {last_error}")
            error(f"*** MANUAL ACTION REQUIRED — close {symbol} in broker terminal ***")
            telegram(
                f"🚨 EXIT FAILED — {leg_upper} after {MAX_CLOSE_ATTEMPTS} retries\n"
                f"MANUAL ACTION REQUIRED\n"
                f"Symbol : {symbol}\n"
                f"Error  : {last_error}"
            )
            return

        # ── Fetch actual close fill price ─────────────────────────────────────
        close_orderid  = resp.get("orderid", "") if isinstance(resp, dict) else ""
        actual_fill_px = self._fetch_close_fill_price(close_orderid) if close_orderid else 0.0

        if actual_fill_px > 0:
            realised_pnl = (entry_px - actual_fill_px) * qty() if entry_px > 0 else 0.0
            info(
                f"  Actual fill  : Rs.{actual_fill_px:.2f}  "
                f"(trigger LTP was Rs.{current_ltp:.2f})"
            )
        else:
            realised_pnl = approx_pnl
            debug(f"  Actual fill unavailable — using trigger LTP Rs.{current_ltp:.2f} for P&L")

        # ── Update state ──────────────────────────────────────────────────────
        exit_px_key = f"exit_price_{leg_upper.lower()}"
        state[exit_px_key] = (
            actual_fill_px if actual_fill_px > 0
            else current_ltp if current_ltp > 0
            else entry_px
        )
        state[active_key]   = False
        state["closed_pnl"] = state["closed_pnl"] + realised_pnl

        info(f"  {leg_upper} LEG CLOSED  |  Reason: {reason}")
        info(f"  Realised this-leg P&L  : Rs.{realised_pnl:.0f}")
        info(f"  Cumulative closed_pnl  : Rs.{state['closed_pnl']:.0f}")

        # ── Inspect surviving leg ─────────────────────────────────────────────
        other_leg      = "PE" if leg_upper == "CE" else "CE"
        other_active   = state[f"{other_leg.lower()}_active"]
        other_symbol   = state[f"symbol_{other_leg.lower()}"]
        other_entry_px = state[f"entry_price_{other_leg.lower()}"]

        if other_active:
            # ── Partial exit — surviving leg continues ────────────────────────
            # FIX-XXIV: Context-aware breakeven SL activation.
            # After partial exit at a loss, check whether the surviving leg is
            # currently WINNING (LTP < entry for short) or LOSING (LTP >= entry).
            #
            # • Winning survivor: skip breakeven SL — it would kill a profitable
            #   leg.  Trailing SL / fixed SL / winner-leg booking handle protection.
            # • Losing survivor:  activate breakeven SL to cap total day's loss.
            be_activated = False
            be_price     = 0.0
            if (
                cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED
                and other_entry_px > 0
                and state["closed_pnl"] < 0
            ):
                # Fetch surviving leg's current LTP to determine win/loss status
                survivor_ltp = self._fetch_ltp(other_leg)
                survivor_is_winner = (
                    survivor_ltp > 0 and survivor_ltp < other_entry_px
                )

                if survivor_is_winner:
                    # Surviving leg is profitable for short — breakeven SL would
                    # kill the profit.  Let trailing / fixed / winner-booking manage it.
                    survivor_mtm = round((other_entry_px - survivor_ltp) * qty(), 0)
                    info(
                        f"  BREAKEVEN SL SKIPPED for {other_leg}: survivor is WINNING "
                        f"(LTP Rs.{survivor_ltp:.2f} < entry Rs.{other_entry_px:.2f}, "
                        f"unrealised +Rs.{survivor_mtm:.0f})"
                    )
                    info(
                        f"  Protection: trailing SL / fixed-dynamic SL / "
                        f"winner-leg booking will manage this leg"
                    )
                else:
                    # Surviving leg is at/above entry — losing or flat.
                    # Activate breakeven SL to cap the combined position's loss.
                    raw_be_price = round(other_entry_px + state["closed_pnl"] / qty(), 2)
                    be_price = round(raw_be_price * (1.0 + cfg.BREAKEVEN_BUFFER_PCT / 100.0), 2)
                    if 0 < be_price < other_entry_px:
                        other_lower = other_leg.lower()
                        state[f"breakeven_active_{other_lower}"]       = True
                        state[f"breakeven_sl_{other_lower}"]           = be_price
                        state[f"breakeven_activated_at_{other_lower}"] = now_ist().isoformat()
                        be_activated = True
                        if cfg.BREAKEVEN_GRACE_PERIOD_MIN > 0:
                            from datetime import timedelta
                            arm_time = now_ist() + timedelta(minutes=cfg.BREAKEVEN_GRACE_PERIOD_MIN)
                            grace_str = (
                                f"  Grace period : {cfg.BREAKEVEN_GRACE_PERIOD_MIN} min "
                                f"(SL arms at ~{arm_time.strftime('%H:%M:%S')} IST)"
                            )
                        else:
                            grace_str = "  Grace period : NONE (armed immediately)"
                        info(
                            f"  BREAKEVEN SL activated for {other_leg}: "
                            f"Rs.{be_price:.2f}  "
                            f"(raw breakeven Rs.{raw_be_price:.2f} + "
                            f"{cfg.BREAKEVEN_BUFFER_PCT}% buffer)"
                        )
                        info(grace_str)
                    elif survivor_ltp > 0:
                        info(
                            f"  BREAKEVEN SL not viable for {other_leg}: "
                            f"computed be_price Rs.{be_price:.2f} out of range "
                            f"(entry Rs.{other_entry_px:.2f}, LTP Rs.{survivor_ltp:.2f})"
                        )

            other_sl = sl_level(other_leg)
            state["in_position"] = True
            save_state()

            # Determine SL type label for Telegram
            _other_lower = other_leg.lower()
            if be_activated:
                _sl_type_label = "breakeven protection"
            elif cfg.TRAILING_SL_ENABLED and state.get(f"trailing_active_{_other_lower}", False):
                _sl_type_label = "trailing SL"
            elif cfg.DYNAMIC_SL_ENABLED:
                _sl_type_label = f"dynamic {_dynamic_sl_percent():.0f}%"
            else:
                _sl_type_label = f"fixed {cfg.LEG_SL_PERCENT}%"

            info(f"  {other_leg} leg still ACTIVE — continues with independent SL")
            info(f"  {other_leg} symbol    : {other_symbol}")
            info(f"  {other_leg} entry     : Rs.{other_entry_px:.2f}")
            info(f"  {other_leg} SL level  : Rs.{other_sl:.2f}  ({_sl_type_label})")
            info(f"  {other_leg} hard exit : {cfg.EXIT_TIME} IST")
            sep()

            telegram(
                f"⚡ PARTIAL EXIT — {leg_upper} LEG CLOSED\n"
                f"Reason     : {reason}\n"
                f"Symbol     : {symbol}\n"
                f"Realised P&L: Rs.{realised_pnl:.0f}\n"
                f"───────────────────\n"
                f"{other_leg} STILL ACTIVE\n"
                f"Symbol     : {other_symbol}\n"
                f"Entry      : Rs.{other_entry_px:.2f}\n"
                f"SL @       : Rs.{other_sl:.2f} ({_sl_type_label})\n"
                f"Hard exit  : {cfg.EXIT_TIME} IST"
            )

        else:
            # ── Full exit — both legs now closed ──────────────────────────────
            self._mark_fully_flat(reason=reason)

        sep()

    # ── Close all ─────────────────────────────────────────────────────────────

    def close_all(self, reason: str = "Scheduled Exit") -> None:
        """
        Close ALL currently active legs.

        Acquires _monitor_lock (RLock) so this call is serialised with any
        running monitor tick.  RLock allows _close_all_locked() →
        close_one_leg() re-entry without deadlocking.
        """
        with _monitor_lock:
            self._close_all_locked(reason)

    def _close_all_locked(self, reason: str) -> None:
        """Inner close_all() — called while _monitor_lock is held."""
        if not state["in_position"]:
            info(f"close_all() — no open position ({reason!r}), nothing to do")
            return

        active = active_legs()
        if not active:
            info(f"close_all() — no active legs ({reason!r}), nothing to do")
            return

        sep()
        info(f"CLOSE ALL REMAINING LEGS  |  Active: {active}  |  Reason: {reason}")

        if len(active) == 2:
            # Both legs open — attempt atomic closeposition()
            info("Both legs active → closeposition() (atomic)")
            closepos_ok  = False
            closepos_err = ""
            try:
                resp = _get_client().closeposition(strategy=cfg.STRATEGY_NAME)
                if is_api_success(resp):
                    closepos_ok = True
                else:
                    closepos_err = get_api_error(resp)
            except Exception as exc:
                closepos_err = str(exc)

            if closepos_ok:
                # FIX-3 / FIX-B: compute open_mtm for final P&L
                ce_px      = state["entry_price_ce"]
                pe_px      = state["entry_price_pe"]
                no_tick_yet = (
                    not _shared._monitor_state.first_tick_fired
                    and ce_px > 0
                    and pe_px > 0
                )

                if no_tick_yet:
                    info("No monitor tick yet — fetching live LTPs for P&L estimate")
                    ltp_ce = self._fetch_ltp("CE")
                    ltp_pe = self._fetch_ltp("PE")
                    if ltp_ce > 0 and ltp_pe > 0:
                        open_mtm_snapshot = (
                            (ce_px - ltp_ce) * qty() +
                            (pe_px - ltp_pe) * qty()
                        )
                        info(
                            f"  Live LTPs: CE Rs.{ltp_ce:.2f}  PE Rs.{ltp_pe:.2f}  "
                            f"→ open_mtm Rs.{open_mtm_snapshot:.0f}"
                        )
                    else:
                        warn("LTP fetch failed in no-tick path — P&L summary will show Rs.0")
                        open_mtm_snapshot = 0.0
                else:
                    open_mtm_snapshot = state["today_pnl"] - state["closed_pnl"]

                state["closed_pnl"] += open_mtm_snapshot
                state["ce_active"]   = False
                state["pe_active"]   = False
                self._mark_fully_flat(reason=reason)

            else:
                # FIX-II (v5.5.0): closeposition() failed — fall back to per-leg
                warn(
                    f"closeposition() FAILED ({closepos_err}) — "
                    f"falling back to per-leg close"
                )
                telegram(
                    f"⚠️ closeposition() FAILED — falling back to per-leg close\n"
                    f"CE: {state['symbol_ce']}\n"
                    f"PE: {state['symbol_pe']}\n"
                    f"Error: {closepos_err}"
                )
                for fallback_leg in list(active_legs()):
                    ltp = self._fetch_ltp(fallback_leg)
                    if ltp <= 0:
                        warn(
                            f"  LTP fetch failed for {fallback_leg} fallback — "
                            f"P&L estimate will be Rs.0"
                        )
                    self.close_one_leg(
                        fallback_leg,
                        reason=f"{reason} [closepos fallback]",
                        current_ltp=ltp,
                    )

        elif len(active) == 1:
            leg = active[0]
            info(f"Only {leg} active → fetching LTP then close_one_leg({leg})")
            ltp = self._fetch_ltp(leg)
            if ltp <= 0:
                warn(f"  LTP fetch failed for {leg} — P&L estimate will be Rs.0")
            self.close_one_leg(leg, reason=reason, current_ltp=ltp)

    def emergency_close_all(self) -> bool:
        """
        Best-effort close for emergency scenarios (partial fill, orphan positions).

        Uses closeposition(). Does NOT update state — caller is responsible.
        Returns True on success, False on failure.
        Sends a Telegram alert on failure so the operator is immediately notified.
        """
        info("Emergency close via closeposition()...")
        try:
            resp = _get_client().closeposition(strategy=cfg.STRATEGY_NAME)
            if is_api_success(resp):
                info("Emergency close: SUCCESS")
                return True
            else:
                err = get_api_error(resp)
                error(f"Emergency close FAILED: {err}")
                error("*** MANUAL ACTION REQUIRED in broker terminal ***")
                telegram(
                    f"🚨 EMERGENCY CLOSE FAILED\n"
                    f"closeposition() returned an error: {err}\n"
                    f"Strategy: {cfg.STRATEGY_NAME}\n"
                    f"MANUAL ACTION REQUIRED — close positions in broker terminal immediately."
                )
                return False
        except Exception as exc:
            error(f"Emergency close EXCEPTION: {exc}")
            error("*** MANUAL ACTION REQUIRED in broker terminal ***")
            telegram(
                f"🚨 EMERGENCY CLOSE EXCEPTION\n"
                f"closeposition() raised: {exc}\n"
                f"Strategy: {cfg.STRATEGY_NAME}\n"
                f"MANUAL ACTION REQUIRED — close positions in broker terminal immediately."
            )
            return False

    # ── LTP helper (used by close_all / _close_all_locked) ─────────────────

    def _fetch_ltp(self, leg: str) -> float:
        """
        Fetch live LTP for a single leg via quotes() on NFO exchange.
        Returns float > 0 on success, 0.0 on failure.
        """
        symbol = state[f"symbol_{leg.lower()}"]
        if not symbol:
            return 0.0
        try:
            q = _get_client().quotes(symbol=symbol, exchange=OPTION_EXCH)
            if is_api_success(q):
                ltp = float(q.get("data", {}).get("ltp", 0) or 0)
                return ltp if ltp > 0 else 0.0
            warn(f"quotes() failed [{leg}]: {get_api_error(q)}")
            return 0.0
        except Exception as exc:
            warn(f"quotes() exception [{leg}]: {exc}")
            return 0.0

    # ── Trade log + flat ──────────────────────────────────────────────────────

    def _append_trade_log(self, reason: str, final_pnl: float, exit_dt: datetime) -> None:
        """
        Append one JSON line to cfg.TRADE_LOG_FILE for post-session analysis.

        Called from _mark_fully_flat() BEFORE state is reset, so all entry
        context (prices, symbols, VIX, IVR, margin) is still readable.
        A corrupt or incomplete final line is safe to ignore on read (JSONL format).
        """
        if not cfg.TRADE_LOG_FILE:
            return

        try:
            entry_dt  = parse_ist_datetime(state.get("entry_time"))
            held_mins = (
                int((exit_dt - entry_dt).total_seconds() // 60)
                if entry_dt else None
            )

            record = {
                "date"            : exit_dt.strftime("%Y-%m-%d"),
                "entry_time"      : entry_dt.isoformat() if entry_dt else None,
                "exit_time"       : exit_dt.isoformat(),
                "duration_min"    : held_mins,
                "symbol_ce"       : state.get("symbol_ce", ""),
                "symbol_pe"       : state.get("symbol_pe", ""),
                "entry_price_ce"  : state.get("entry_price_ce", 0.0),
                "entry_price_pe"  : state.get("entry_price_pe", 0.0),
                "exit_price_ce"   : state.get("exit_price_ce", 0.0),
                "exit_price_pe"   : state.get("exit_price_pe", 0.0),
                "closed_pnl"      : round(final_pnl, 2),
                "exit_reason"     : reason,
                "trade_count"     : state.get("trade_count", 0) + 1,
                "vix_at_entry"    : state.get("vix_at_entry", 0.0),
                "ivr_at_entry"    : state.get("ivr_at_entry", 0.0),
                "ivp_at_entry"    : state.get("ivp_at_entry", 0.0),
                "underlying_ltp"  : state.get("underlying_ltp", 0.0),
                "margin_required" : state.get("margin_required", 0.0),
            }

            with open(cfg.TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            debug(f"Trade log appended → {cfg.TRADE_LOG_FILE}")

        except Exception as exc:
            warn(f"Trade log write failed (non-critical): {exc}")

    def _mark_fully_flat(self, reason: str) -> None:
        """
        Reset all position fields, delete state file, send final summary.

        Uses state["closed_pnl"] as authoritative final P&L (accumulated by
        each leg close and the close_all() open_mtm snapshot).

        FIX-10 (v5.0.0): today_pnl explicitly reset to 0.0 — previously it
        retained the previous trade's value after going flat.
        """
        final_pnl    = state["closed_pnl"]
        exit_dt      = now_ist()
        duration_str = ""

        entry_dt = parse_ist_datetime(state.get("entry_time"))
        if entry_dt:
            held_mins    = int((exit_dt - entry_dt).total_seconds() // 60)
            duration_str = f"  |  Held: {held_mins} min"

        self._append_trade_log(reason, final_pnl, exit_dt)

        # ── Store last-trade info for re-entry logic BEFORE resetting ─────────
        state["last_close_time"]     = exit_dt.isoformat()
        state["last_trade_pnl"]      = final_pnl
        # Increment reentry counter only if this is a re-entry (trade_count > 0 for current day)
        today_str = exit_dt.strftime("%Y-%m-%d")
        if state.get("entry_date") == today_str:
            state["reentry_count_today"] = state.get("reentry_count_today", 0)
        # (reentry_count_today is incremented by strategy_core on re-entry, not here)

        # Reset ALL position-related state
        state["in_position"]              = False
        state["ce_active"]                = False
        state["pe_active"]                = False
        state["symbol_ce"]                = ""
        state["symbol_pe"]                = ""
        state["orderid_ce"]               = ""
        state["orderid_pe"]               = ""
        state["entry_price_ce"]           = 0.0
        state["entry_price_pe"]           = 0.0
        state["exit_price_ce"]            = 0.0
        state["exit_price_pe"]            = 0.0
        state["closed_pnl"]               = 0.0
        state["today_pnl"]                = 0.0   # FIX-10
        state["underlying_ltp"]           = 0.0
        state["vix_at_entry"]             = 0.0
        state["ivr_at_entry"]             = 0.0
        state["ivp_at_entry"]             = 0.0
        state["trailing_active_ce"]       = False
        state["trailing_active_pe"]       = False
        state["trailing_sl_ce"]           = 0.0
        state["trailing_sl_pe"]           = 0.0
        state["breakeven_active_ce"]      = False
        state["breakeven_active_pe"]      = False
        state["breakeven_sl_ce"]          = 0.0
        state["breakeven_sl_pe"]          = 0.0
        state["breakeven_activated_at_ce"] = None
        state["breakeven_activated_at_pe"] = None
        state["orb_price"]                = 0.0
        state["entry_time"]               = None
        state["entry_date"]               = None
        state["margin_required"]          = 0.0
        state["margin_available"]         = 0.0
        state["exit_reason"]              = reason
        state["trade_count"]             += 1

        clear_state_file()

        sign = "+" if final_pnl >= 0 else ""
        info(
            f"POSITION FULLY CLOSED  |  Reason: {reason}  |  "
            f"Final P&L ≈ Rs.{sign}{final_pnl:.0f}{duration_str}"
        )
        info(f"Session trade count: {state['trade_count']}")
        sep()

        emoji = "🟢" if final_pnl >= 0 else "🔴"
        telegram(
            f"{emoji} POSITION FULLY CLOSED\n"
            f"Reason        : {reason}\n"
            f"Final P&L ≈   : Rs.{sign}{final_pnl:.0f}{duration_str}\n"
            f"Session trades: {state['trade_count']}"
        )
