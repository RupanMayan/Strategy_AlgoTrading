"""
src/reconciler.py  —  StartupReconciler class
═══════════════════════════════════════════════════════════════════════
Startup reconciliation — one call to reconcile() before the scheduler runs.

Cases handled:
  Stale  — State from a prior trading day (MIS auto sq-off by broker)
  A      — No state + broker flat                → clean start
  B      — State: IN POS + broker confirms       → restore + resume
  C      — State: IN POS + broker flat           → externally closed
  D      — No state + broker has open NFO positions → orphan, emergency close
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from src._shared import (
    cfg, state, save_state, load_state, clear_state_file,
    info, warn, error, debug, sep,
    telegram,
    _get_client,
    OPTION_EXCH, IST,
    now_ist, active_legs, sl_level,
    is_api_success, parse_ist_datetime,
)

if TYPE_CHECKING:
    from src.order_engine import OrderEngine


class StartupReconciler:
    """
    Startup reconciliation — one call to reconcile() before the scheduler runs.

    Parameters
    ----------
    order_engine : OrderEngine  — for _capture_fill_prices() and emergency_close_all()
    """

    def __init__(self, order_engine: "OrderEngine") -> None:
        self._oe = order_engine

    # ── Broker position fetch ─────────────────────────────────────────────────

    def _fetch_broker_positions(self) -> list:
        """
        Fetch open NFO positions for THIS underlying via positionbook().

        Returns a list of non-zero-qty NFO positions whose symbol starts with
        cfg.UNDERLYING (e.g. "NIFTY"), or [] on failure.

        FIX-X (v5.9.0): Symbol prefix filter prevents positions from other
        strategies on the same account (e.g. BANKNIFTY Iron Butterfly) from
        being mistaken for our positions and triggering a Case D emergency close.
        """
        try:
            client = _get_client()
            resp = client.positionbook()
            if not is_api_success(resp):
                warn(f"positionbook() failed: {resp}")
                return []

            all_pos  = resp.get("data", [])
            open_nfo = [
                p for p in all_pos
                if p.get("exchange", "") == OPTION_EXCH
                and int(p.get("quantity", 0) or 0) != 0
                and str(p.get("symbol", "")).upper().startswith(cfg.UNDERLYING.upper())
            ]

            if open_nfo:
                info(f"Broker: {len(open_nfo)} open NFO position(s):")
                for p in open_nfo:
                    info(f"  {p.get('symbol')} | qty: {p.get('quantity')} | avg: {p.get('average_price')}")
            else:
                info("Broker: no open NFO positions")

            return open_nfo

        except Exception as exc:
            warn(f"_fetch_broker_positions exception: {exc}")
            return []

    # ── Main reconciliation entry-point ───────────────────────────────────────

    def reconcile(self) -> None:
        """
        Reconcile persisted state with live broker positions.

        Called once at startup before the APScheduler begins.  Prints a clear
        separator section with the reconciliation outcome so the operator can
        immediately see what happened on restart.

        Cases handled:
          Stale  — State from a prior trading day (MIS auto sq-off by broker)
          A      — No state + broker flat                → clean start
          B      — State: IN POS + broker confirms       → restore + resume
          C      — State: IN POS + broker flat           → externally closed
          D      — No state + broker has open NFO positions → orphan, emergency close
        """
        sep()
        info("STARTUP RECONCILIATION — saved state vs live broker positions")
        sep()

        saved            = load_state()
        broker_positions = self._fetch_broker_positions()

        today_str    = now_ist().date().isoformat()  # IST date — not OS date
        saved_date   = saved.get("entry_date", "")
        saved_in_pos = saved.get("in_position", False)

        # ── Stale state from previous trading day ─────────────────────────────
        if saved_in_pos and saved_date and saved_date != today_str:
            warn(f"Stale state from {saved_date} (today: {today_str})")
            warn("MIS auto sq-off by broker — clearing stale state")
            state["in_position"] = False
            clear_state_file()
            telegram(
                f"RESTART: Stale state from {saved_date} cleared.\n"
                f"Starting fresh for today ({today_str})."
            )
            sep()
            return

        # ── Case A — no position anywhere ─────────────────────────────────────
        if not saved_in_pos and not broker_positions:
            info("Case A: No saved position + broker flat → clean start")
            sep()
            return

        # ── Case B — saved state matches broker ───────────────────────────────
        if saved_in_pos and broker_positions:
            info("Case B: Saved position + broker confirms → RESTORING STATE")

            for key in state:
                if key in saved:
                    state[key] = saved[key]

            # Restore entry_time as IST-aware datetime
            parsed_et = parse_ist_datetime(state.get("entry_time"))
            state["entry_time"] = parsed_et if parsed_et else now_ist()

            # ── Symbol verification ────────────────────────────────────────────
            # Confirm that the saved leg symbols are actually open at the broker.
            # Without this, Case B fires when broker closed the position
            # externally while other NFO positions are still open.
            broker_symbols = {p.get("symbol", "") for p in broker_positions}
            symbol_ce      = state.get("symbol_ce", "")
            symbol_pe      = state.get("symbol_pe", "")
            ce_active      = state.get("ce_active", False)
            pe_active      = state.get("pe_active", False)

            mismatch = []
            if ce_active and symbol_ce and symbol_ce not in broker_symbols:
                mismatch.append(f"CE ({symbol_ce}) not found at broker")
            if pe_active and symbol_pe and symbol_pe not in broker_symbols:
                mismatch.append(f"PE ({symbol_pe}) not found at broker")

            if mismatch:
                warn(f"Case B symbol mismatch: {'; '.join(mismatch)}")
                warn("Saved symbols not confirmed at broker — treating as externally closed (Case C)")
                state["in_position"] = False
                state["exit_reason"] = "Symbol mismatch on restart — positions closed externally"
                clear_state_file()
                telegram(
                    f"⚠️ RESTART — Symbol Mismatch\n"
                    f"State showed open position but broker symbols not found:\n"
                    + "\n".join(f"  {m}" for m in mismatch)
                    + f"\nState cleared. Verify positions manually in broker terminal."
                )
                sep()
                return

            # FIX-VII (v5.5.1): Re-fetch fills if crash occurred during fill capture.
            # entry_price_ce/pe may be 0.0 in saved state — re-fetch using order IDs.
            prices_missing = (
                (state.get("ce_active", False) and state.get("entry_price_ce", 0.0) <= 0.0)
                or (state.get("pe_active", False) and state.get("entry_price_pe", 0.0) <= 0.0)
            )
            if prices_missing and (state.get("orderid_ce") or state.get("orderid_pe")):
                warn("Entry prices missing (crash during fill capture) — re-fetching fills now")
                self._oe._capture_fill_prices()
                save_state()
                info(
                    f"  Fill re-fetch : CE Rs.{state['entry_price_ce']:.2f}  "
                    f"PE Rs.{state['entry_price_pe']:.2f}"
                )

                # FIX-X (v5.9.0): If re-capture also fails, SL cannot be armed.
                # Emergency-close the affected legs — safer than unprotected exposure.
                still_missing = (
                    (state.get("ce_active", False) and state.get("entry_price_ce", 0.0) <= 0.0)
                    or (state.get("pe_active", False) and state.get("entry_price_pe", 0.0) <= 0.0)
                )
                if still_missing:
                    error(
                        "CRITICAL: Fill re-capture FAILED on restart — "
                        "SL CANNOT be armed. Emergency closing position."
                    )
                    telegram(
                        f"🚨 CRITICAL — FILL CAPTURE FAILED ON RESTART\n"
                        f"CE fill: Rs.{state.get('entry_price_ce', 0.0):.2f}  "
                        f"PE fill: Rs.{state.get('entry_price_pe', 0.0):.2f}\n"
                        f"SL cannot be computed — emergency closing ALL legs\n"
                        f"to prevent unprotected exposure.\n"
                        f"Check broker terminal and re-enter manually if needed."
                    )
                    self._oe.emergency_close_all()
                    state["in_position"] = False
                    state["exit_reason"] = "Emergency close: fill capture failed on restart"
                    clear_state_file()
                    sep()
                    return

            active = active_legs()
            info(f"  Active legs   : {active}")
            info(f"  CE symbol     : {state['symbol_ce']}  active={state['ce_active']}")
            info(f"  PE symbol     : {state['symbol_pe']}  active={state['pe_active']}")
            info(f"  CE fill       : Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_level('CE'):.2f}")
            info(f"  PE fill       : Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_level('PE'):.2f}")
            info(f"  Closed P&L    : Rs.{state['closed_pnl']:.2f}")
            info(f"  Entry time    : {state['entry_time']}")
            info("  State restored — monitor resumes from next tick")
            sep()

            telegram(
                f"♻️ RESTARTED — STATE RESTORED\n"
                f"Active legs : {active}\n"
                f"CE: {state['symbol_ce']}\n"
                f"  Fill Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_level('CE'):.2f}\n"
                f"PE: {state['symbol_pe']}\n"
                f"  Fill Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_level('PE'):.2f}\n"
                f"Closed P&L so far: Rs.{state['closed_pnl']:.0f}\n"
                f"Monitor resuming."
            )
            return

        # ── Case C — saved state but broker is flat ──────────────────────────
        if saved_in_pos and not broker_positions:
            warn("Case C: State=IN POSITION but broker=FLAT")
            warn("Position was closed externally (broker SQ-OFF / manual close)")
            state["in_position"] = False
            state["exit_reason"] = "Closed externally before restart"
            clear_state_file()
            telegram(
                f"⚠️ RESTART WARNING\n"
                f"State showed open position but broker is FLAT.\n"
                f"CE: {saved.get('symbol_ce', '?')}\n"
                f"PE: {saved.get('symbol_pe', '?')}\n"
                f"Position was closed externally. State cleared."
            )
            sep()
            return

        # ── Case D — no state but broker has NFO positions (orphan) ───────────
        if not saved_in_pos and broker_positions:
            error("Case D: No state file but broker shows open NFO positions (ORPHAN)")
            for p in broker_positions:
                error(f"  {p.get('symbol')} | qty: {p.get('quantity')} | avg: {p.get('average_price')}")
            error("Attempting emergency close")
            telegram(
                f"🚨 CRITICAL: Orphan NFO positions on restart!\n"
                + "\n".join(
                    f"{p.get('symbol')} qty:{p.get('quantity')}"
                    for p in broker_positions
                )
                + "\nAttempting emergency close."
            )
            self._oe.emergency_close_all()
            sep()
            return

        sep()
