"""
src/filters.py  —  FilterEngine class
═══════════════════════════════════════════════════════════════════════
Owns ALL entry gates evaluated inside StrategyCore._job_entry().

Gates (short-circuit order, cheapest first):
  1. DTE + weekend + month filter  (dte_filter_ok)
  2. VIX range filter              (vix_ok)
  3. IVR / IVP filter              (delegated to VIXManager.ivr_ivp_ok)
  4. Opening Range (ORB) filter    (orb_filter_ok)

Expiry helpers (used by job_entry and StrategyCore):
  _nearest_tuesday_date(), nearest_tuesday_expiry(),
  get_expiry(), _get_expiry_date_silent(), get_dte()
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import TYPE_CHECKING, Optional

from src._shared import (
    cfg, state,
    info, warn, debug,
    telegram,
    _get_client,
    INDEX_EXCH,
    DAY_NAMES, MONTH_NAMES,
    now_ist,
    is_api_success,
)

if TYPE_CHECKING:
    from src.vix_manager import VIXManager


class FilterEngine:
    """
    Owns ALL entry gates evaluated inside job_entry().

    Gates are evaluated in short-circuit order (cheapest first):
      1. DTE + weekend + month filter
      2. VIX range filter              (vix_ok)
      3. IVR / IVP filter              (delegated to VIXManager)
      4. Opening Range (ORB) filter    (orb_filter_ok)

    Expiry helpers (used by job_entry and StrategyCore) also live here:
      _nearest_tuesday_date(), nearest_tuesday_expiry(),
      get_expiry(), _get_expiry_date_silent(), get_dte()
    """

    def __init__(self, vix_manager: "VIXManager") -> None:
        self._vix = vix_manager

    # ── Expiry helpers ────────────────────────────────────────────────────────

    def _nearest_tuesday_date(self) -> date:
        """
        Compute nearest NIFTY weekly expiry date (Tuesday) silently — no logging.

        NSE EXPIRY: NIFTY 50 weekly options expire every TUESDAY
        (effective September 2, 2025 per SEBI F&O restructuring).

        Logic:
          • Today IS Tuesday AND time < 15:30 IST  → use today
          • Today IS Tuesday AND time >= 15:30 IST → use next Tuesday
          • Any other weekday                       → next upcoming Tuesday
        """
        now        = now_ist()
        today      = now.date()
        days_ahead = (1 - today.weekday()) % 7   # 0 if today is already Tuesday

        if days_ahead == 0 and (now.hour, now.minute) >= (15, 30):
            days_ahead = 7

        return today + timedelta(days=days_ahead)

    def nearest_tuesday_expiry(self) -> str:
        """
        Return nearest NIFTY weekly expiry (TUESDAY) as DDMMMYY string, with logging.
        Called only in get_expiry() — once per entry flow.
        """
        expiry = self._nearest_tuesday_date()
        result = expiry.strftime("%d%b%y").upper()
        info(f"Auto expiry: {result}  (date: {expiry}, {expiry.strftime('%A')})")
        return result

    def get_expiry(self) -> str:
        """Return the active expiry string based on cfg.AUTO_EXPIRY setting."""
        if cfg.AUTO_EXPIRY:
            return self.nearest_tuesday_expiry()
        info(f"Manual expiry: {cfg.MANUAL_EXPIRY}")
        return cfg.MANUAL_EXPIRY

    def _get_expiry_date_silent(self) -> date:
        """
        Return expiry as a date object WITHOUT logging.
        Used by get_dte() and _print_banner() to avoid log noise on every tick.
        """
        if cfg.AUTO_EXPIRY:
            return self._nearest_tuesday_date()
        try:
            return datetime.strptime(cfg.MANUAL_EXPIRY, "%d%b%y").date()
        except Exception:
            return self._nearest_tuesday_date()

    def get_dte(self) -> int:
        """
        Compute DTE (Days To Expiry) = TRADING days from today to nearest expiry.

        Counts Mon–Fri only (AlgoTest-compatible). Does NOT log expiry resolution
        to avoid noise on every 15-second monitor tick.

        NIFTY Tuesday expiry mapping:
          DTE0 = Tuesday (expiry day)  DTE1 = Monday  DTE2 = Friday
          DTE3 = Thursday              DTE4 = Wednesday
        """
        today       = now_ist().date()
        expiry_date = self._get_expiry_date_silent()

        dte     = 0
        current = today
        while current < expiry_date:
            current += timedelta(days=1)
            if current.weekday() < 5:
                dte += 1

        debug(
            f"DTE: {dte}  "
            f"(today: {today} {DAY_NAMES[today.weekday()]}  "
            f"expiry: {expiry_date.strftime('%d%b%y').upper()} {expiry_date.strftime('%A')})"
        )
        return dte

    # ── DTE + month filter ────────────────────────────────────────────────────

    def dte_filter_ok(self, dte: Optional[int] = None) -> bool:
        """
        Return True if today passes: weekend guard → month filter → DTE filter.

        Parameters
        ----------
        dte : optional precomputed DTE to avoid a redundant get_dte() call
              when job_entry() already computed DTE for the DTE-map time guard.

        Weekend guard:
          Sat/Sun compute the same DTE as the preceding Friday (trading-day
          counting). This guard makes manual_entry() on weekends fail cleanly.

        Month filter:
          Skip if now_ist().month is in cfg.SKIP_MONTHS.

        DTE filter:
          Allow trade only if DTE is in cfg.TRADE_DTE.
        """
        now     = now_ist()
        weekday = now.weekday()
        month   = now.month

        if weekday >= 5:
            info(
                f"{DAY_NAMES[weekday]} is not a trading day — skipping "
                f"(scheduler never fires on weekends; this path via manual_entry only)"
            )
            return False

        if month in cfg.SKIP_MONTHS:
            info(f"{MONTH_NAMES[month]} is in SKIP_MONTHS — skipping")
            telegram(f"Skipping — {MONTH_NAMES[month]} is a configured skip month")
            return False

        if dte is None:
            dte = self.get_dte()

        if dte not in cfg.TRADE_DTE:
            info(
                f"DTE{dte} ({DAY_NAMES[weekday]}) not in "
                f"TRADE_DTE {['DTE' + str(d) for d in sorted(cfg.TRADE_DTE)]} — skipping"
            )
            return False

        info(
            f"DTE filter OK: DTE{dte} ({DAY_NAMES[weekday]}) "
            f"| month: {MONTH_NAMES[month]} {now.year}"
        )
        return True

    # ── VIX range filter ──────────────────────────────────────────────────────

    def vix_ok(self) -> bool:
        """
        VIX range gate.

        When VIX_FILTER_ENABLED = False, VIX is still fetched if any of
        IVR/IVP filter or VIX spike monitor is enabled — they all need a
        valid vix_at_entry baseline.

        Returns True = OK to trade, False = skip.
        Stores validated VIX in state["vix_at_entry"] on success.
        """
        if not cfg.VIX_FILTER_ENABLED:
            needs_vix = (
                cfg.IVR_FILTER_ENABLED or
                cfg.IVP_FILTER_ENABLED or
                cfg.VIX_SPIKE_MONITOR_ENABLED
            )
            if needs_vix:
                vix = self._vix.fetch_vix()
                if vix > 0:
                    state["vix_at_entry"] = vix
                    reasons = []
                    if cfg.IVR_FILTER_ENABLED or cfg.IVP_FILTER_ENABLED:
                        reasons.append("IVR/IVP filter")
                    if cfg.VIX_SPIKE_MONITOR_ENABLED:
                        reasons.append("VIX spike monitor")
                    info(
                        f"VIX filter disabled — fetched VIX {vix:.2f} "
                        f"for: {', '.join(reasons)}"
                    )
                else:
                    warn(
                        "VIX filter disabled but VIX fetch failed — "
                        "IVR/IVP filter will receive VIX=0.0 and likely skip trade; "
                        "VIX spike monitor cannot establish a baseline."
                    )
            else:
                info("VIX filter disabled")
            return True

        vix = self._vix.fetch_vix()

        if vix < 0:
            warn("VIX fetch failed — skipping trade (precaution)")
            telegram("VIX unavailable — no trade today (precaution)")
            return False
        if vix < cfg.VIX_MIN:
            warn(f"VIX {vix:.2f} < {cfg.VIX_MIN} — premiums too thin, skipping")
            telegram(f"VIX {vix:.2f} &lt; {cfg.VIX_MIN} — thin premiums, no trade today")
            return False
        if vix > cfg.VIX_MAX:
            warn(f"VIX {vix:.2f} > {cfg.VIX_MAX} — danger zone, skipping")
            telegram(f"VIX {vix:.2f} &gt; {cfg.VIX_MAX} — DANGER ZONE, no trade today!")
            return False

        info(f"VIX {vix:.2f} within [{cfg.VIX_MIN}–{cfg.VIX_MAX}] — OK to trade")
        state["vix_at_entry"] = vix
        return True

    # ── Opening range filter ──────────────────────────────────────────────────

    def orb_filter_ok(self) -> bool:
        """
        Opening Range Breakout filter.

        Compares live NIFTY spot at entry time to the reference price captured
        by job_orb_capture() at cfg.ORB_CAPTURE_TIME (default 09:17 IST).

        FAIL-OPEN: if ORB price not captured OR entry-time LTP fetch fails,
        returns True — a missing ORB check never silently blocks all trades.

        Returns True (OK to trade) / False (skip).
        """
        if not cfg.ORB_FILTER_ENABLED:
            return True

        orb_px = state.get("orb_price", 0.0)
        if orb_px <= 0:
            warn(
                "ORB filter: opening reference price not captured "
                f"(check that ORB capture job ran at {cfg.ORB_CAPTURE_TIME}) — bypassed (fail-open)"
            )
            return True

        try:
            q = _get_client().quotes(symbol=cfg.UNDERLYING, exchange=INDEX_EXCH)
            if is_api_success(q):
                current_spot = float(q.get("data", {}).get("ltp", 0) or 0)
            else:
                current_spot = 0.0
        except Exception as exc:
            warn(f"ORB filter: spot LTP exception — {exc} — bypassed (fail-open)")
            return True

        if current_spot <= 0:
            warn("ORB filter: NIFTY LTP fetch failed — bypassed (fail-open)")
            return True

        move_pct  = abs(current_spot - orb_px) / orb_px * 100.0
        direction = "↑" if current_spot > orb_px else "↓"

        if move_pct > cfg.ORB_MAX_MOVE_PCT:
            warn(
                f"ORB filter: NIFTY {direction} {move_pct:.2f}% "
                f"(ORB Rs.{orb_px:.2f} → now Rs.{current_spot:.2f}) "
                f"> {cfg.ORB_MAX_MOVE_PCT}% — directional open, skipping trade"
            )
            telegram(
                f"📊 ORB FILTER — Trade SKIPPED\n"
                f"NIFTY {direction} {move_pct:.2f}% since {cfg.ORB_CAPTURE_TIME}\n"
                f"ORB ref: Rs.{orb_px:.2f}  |  Now: Rs.{current_spot:.2f}\n"
                f"Threshold: {cfg.ORB_MAX_MOVE_PCT}% — trending open, straddle risk too high."
            )
            return False

        info(
            f"ORB filter: NIFTY {direction} {move_pct:.2f}% from ORB Rs.{orb_px:.2f} "
            f"(Rs.{current_spot:.2f}) ≤ {cfg.ORB_MAX_MOVE_PCT}% — OK to trade"
        )
        return True
