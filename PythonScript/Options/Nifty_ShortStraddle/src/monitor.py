"""
src/monitor.py  —  Monitor class
═══════════════════════════════════════════════════════════════════════
Intraday P&L / SL monitor — one instance, lives for the session.

Execution order per tick:
  1. Per-leg SL loop: LTP fetch → trailing SL update → SL check
  2. Broker connectivity escalation
  3. Combined premium decay exit
  4. Winner-leg early booking
  5. Combined P&L update
  6. VIX spike check (throttled)
  7. Spot-move / breakeven breach check (throttled)
  8. Daily profit target check
  9. Daily loss limit check
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src._shared import (
    cfg, state,
    info, warn, error, debug,
    telegram,
    _get_client,
    INDEX_EXCH,
    _monitor_lock,
    now_ist, active_legs, sl_level, _dynamic_sl_percent, qty,
)

# Module-level globals shared with other modules via _shared
import src._shared as _shared

if TYPE_CHECKING:
    from src.order_engine import OrderEngine
    from src.risk import TrailingSLEngine
    from src.vix_manager import VIXManager


class Monitor:
    """
    Intraday P&L / SL monitor — one instance, lives for the session.

    Parameters
    ----------
    order_engine    : OrderEngine  — for close_one_leg() / close_all()
    trailing_sl     : TrailingSLEngine — for update()
    vix_manager     : VIXManager  — for fetch_vix() inside _check_vix_spike()
    """

    def __init__(
        self,
        order_engine: "OrderEngine",
        trailing_sl: "TrailingSLEngine",
        vix_manager: "VIXManager",
    ) -> None:
        self._oe  = order_engine
        self._tsl = trailing_sl
        self._vm  = vix_manager

    # ── LTP helpers ───────────────────────────────────────────────────────────

    def _fetch_spot_ltp(self) -> float:
        """
        Fetch live NIFTY spot LTP via quotes() on NSE_INDEX exchange.

        Used by:
          • job_orb_capture()   — capture opening reference price at 09:17
          • _check_spot_move()  — throttled intraday spot-move guard
          • FilterEngine.orb_filter_ok() — entry-time ORB deviation check

        Returns float > 0 on success, 0.0 on failure (caller handles gracefully).
        """
        try:
            client = _get_client()
            q = client.quotes(symbol=cfg.UNDERLYING, exchange=INDEX_EXCH)
            if isinstance(q, dict) and q.get("status") == "success":
                ltp = float(q.get("data", {}).get("ltp", 0) or 0)
                return ltp if ltp > 0 else 0.0
            warn(
                f"_fetch_spot_ltp: quotes({cfg.UNDERLYING}) failed: "
                f"{q.get('message', '') if isinstance(q, dict) else str(q)}"
            )
            return 0.0
        except Exception as exc:
            warn(f"_fetch_spot_ltp: exception: {exc}")
            return 0.0

    # ── VIX spike check ───────────────────────────────────────────────────────

    def _check_vix_spike(self) -> None:
        """
        Intraday VIX spike detector — called from run_tick() under lock.

        Throttled to run at most once per VIX_SPIKE_CHECK_INTERVAL_S seconds so
        a 15-second monitor loop does not hammer the VIX quotes API.

        Logic:
          1. Guard: feature enabled + position open + entry VIX captured
          2. Throttle: skip if last check was < VIX_SPIKE_CHECK_INTERVAL_S ago
          3. Fetch current live VIX
          4. Compute spike_pct = (current_vix − vix_at_entry) / vix_at_entry × 100
          5. Dual condition (FIX-V v5.5.0): relative % >= threshold AND
             absolute current_vix >= VIX_SPIKE_ABS_FLOOR — both must be true.
          6. If spike confirmed → Telegram alert + close_all() (RLock re-entry safe)

        VIX fetch failures are treated as a non-event: we never close on inability
        to fetch VIX; only on confirmed spike.
        """
        if not cfg.VIX_SPIKE_MONITOR_ENABLED:
            return
        if not state["in_position"]:
            return

        entry_vix = state.get("vix_at_entry", 0.0)
        if entry_vix <= 0:
            return  # Entry VIX not captured — skip silently

        # ── Throttle ──────────────────────────────────────────────────────────
        now = now_ist()
        if (
            _shared._last_vix_spike_check_time is not None
            and (now - _shared._last_vix_spike_check_time).total_seconds()
                < cfg.VIX_SPIKE_CHECK_INTERVAL_S
        ):
            return
        _shared._last_vix_spike_check_time = now

        # ── Fetch live VIX ────────────────────────────────────────────────────
        current_vix = self._vm.fetch_vix()
        if current_vix <= 0:
            warn(
                f"VIX spike monitor: VIX unavailable — check skipped "
                f"(will retry in {cfg.VIX_SPIKE_CHECK_INTERVAL_S}s)"
            )
            return

        spike_pct = round((current_vix - entry_vix) / entry_vix * 100.0, 2)
        debug(
            f"VIX spike monitor: "
            f"entry={entry_vix:.2f}  current={current_vix:.2f}  "
            f"change={spike_pct:+.2f}%  threshold={cfg.VIX_SPIKE_THRESHOLD_PCT}%"
        )

        relative_triggered = spike_pct >= cfg.VIX_SPIKE_THRESHOLD_PCT
        absolute_triggered = current_vix >= cfg.VIX_SPIKE_ABS_FLOOR

        if not relative_triggered:
            info(
                f"VIX spike check OK: {entry_vix:.2f} → {current_vix:.2f} "
                f"({spike_pct:+.1f}% | threshold {cfg.VIX_SPIKE_THRESHOLD_PCT}%)"
            )
            return

        if not absolute_triggered:
            info(
                f"VIX spike check OK (below abs floor): {entry_vix:.2f} → {current_vix:.2f} "
                f"({spike_pct:+.1f}% ≥ threshold but current VIX {current_vix:.2f} "
                f"< floor {cfg.VIX_SPIKE_ABS_FLOOR} — not dangerous at this level)"
            )
            return

        # ── Both conditions met — spike confirmed ────────────────────────────
        warn(
            f"VIX SPIKE DETECTED: {entry_vix:.2f} → {current_vix:.2f} "
            f"({spike_pct:+.1f}% ≥ threshold {cfg.VIX_SPIKE_THRESHOLD_PCT}%, "
            f"VIX {current_vix:.2f} ≥ floor {cfg.VIX_SPIKE_ABS_FLOOR}) — "
            f"closing all positions"
        )
        telegram(
            f"🚨 VIX SPIKE EXIT\n"
            f"Entry VIX   : {entry_vix:.2f}\n"
            f"Current VIX : {current_vix:.2f}\n"
            f"Change      : {spike_pct:+.1f}%  (threshold: +{cfg.VIX_SPIKE_THRESHOLD_PCT}%)\n"
            f"Abs floor   : {cfg.VIX_SPIKE_ABS_FLOOR}  (both conditions met)\n"
            f"Active legs : {active_legs()}\n"
            f"Action      : Closing all positions immediately.\n"
            f"Rationale   : Short vega position in a rising-IV environment — "
            f"IV expansion erodes premium collected even on flat NIFTY."
        )
        self._oe.close_all(
            reason=f"VIX Spike Exit ({entry_vix:.1f}→{current_vix:.1f}, {spike_pct:+.1f}%)"
        )

    # ── Spot-move / breakeven breach check ─────────────────────────────────

    def _check_spot_move(self) -> None:
        """
        Intraday breakeven/spot-move guard — called from run_tick() under lock.

        WHAT IT CHECKS:
          The straddle's theoretical breakeven = entry_spot ± combined_premium.
          If NIFTY moves >= combined_premium × BREAKEVEN_SPOT_MULTIPLIER from
          the entry spot, the position is at or past theoretical breakeven and
          holding further increases loss without a mean-revert mechanism.

        CALCULATION (FIX-XVI v5.9.0 — active-only premium):
          combined_premium = sum of ACTIVE legs' entry prices only.
            Both legs active → CE_entry + PE_entry
            One leg closed   → surviving leg's entry price only
          move_threshold   = combined_premium × BREAKEVEN_SPOT_MULTIPLIER
          move_abs         = |current_NIFTY − spot_at_entry|
          if move_abs >= move_threshold → close_all()

        Throttled to SPOT_CHECK_INTERVAL_S (default 60s).
        Fails-open on LTP unavailability — normal SL monitoring continues.
        """
        if not cfg.BREAKEVEN_SPOT_EXIT_ENABLED:
            return

        spot_at_entry = state.get("underlying_ltp", 0.0)
        if spot_at_entry <= 0:
            return  # Entry spot not captured yet

        ce_entry = state.get("entry_price_ce", 0.0)
        pe_entry = state.get("entry_price_pe", 0.0)

        # FIX-XVI (v5.9.0): Use only the ACTIVE legs' combined premium.
        active_combined_premium = (
            (ce_entry if state.get("ce_active", False) else 0.0)
            + (pe_entry if state.get("pe_active", False) else 0.0)
        )
        combined_premium = (
            active_combined_premium if active_combined_premium > 0 else (ce_entry + pe_entry)
        )
        if combined_premium <= 0:
            return  # Fill prices not yet captured

        # ── Throttle ──────────────────────────────────────────────────────────
        now = now_ist()
        if (
            _shared._last_spot_check_time is not None
            and (now - _shared._last_spot_check_time).total_seconds() < cfg.SPOT_CHECK_INTERVAL_S
        ):
            return
        _shared._last_spot_check_time = now

        current_spot = self._fetch_spot_ltp()
        if current_spot <= 0:
            warn("Breakeven/spot guard: NIFTY LTP unavailable — skipping this check")
            return

        move_abs       = abs(current_spot - spot_at_entry)
        move_threshold = combined_premium * cfg.BREAKEVEN_SPOT_MULTIPLIER
        direction      = "↑" if current_spot > spot_at_entry else "↓"

        debug(
            f"Spot-move check: NIFTY {direction} Rs.{current_spot:.2f} "
            f"(moved Rs.{move_abs:.2f} vs threshold Rs.{move_threshold:.2f} "
            f"= premium Rs.{combined_premium:.2f} × {cfg.BREAKEVEN_SPOT_MULTIPLIER})"
        )

        if move_abs >= move_threshold:
            info(
                f"BREAKEVEN BREACH EXIT: NIFTY {direction} Rs.{current_spot:.2f} "
                f"(moved Rs.{move_abs:.2f} from entry Rs.{spot_at_entry:.2f}) "
                f">= threshold Rs.{move_threshold:.2f} "
                f"(premium Rs.{combined_premium:.2f} × {cfg.BREAKEVEN_SPOT_MULTIPLIER}) — closing all"
            )
            telegram(
                f"⚠️ BREAKEVEN BREACH EXIT\n"
                f"NIFTY {direction} Rs.{current_spot:.2f} — moved Rs.{move_abs:.2f} "
                f"from entry Rs.{spot_at_entry:.2f}\n"
                f"Combined premium collected: Rs.{combined_premium:.2f}/unit\n"
                f"Threshold: Rs.{move_threshold:.2f} ({cfg.BREAKEVEN_SPOT_MULTIPLIER}× premium)\n"
                f"Position at/past theoretical breakeven — closing all."
            )
            self._oe.close_all(
                reason=(
                    f"Breakeven Breach — NIFTY {direction} Rs.{move_abs:.2f} "
                    f"vs premium Rs.{combined_premium:.2f}"
                )
            )

    # ── Inner tick ────────────────────────────────────────────────────────────

    def run_tick(self) -> None:
        """
        Inner monitor logic — called from monitor_pnl() under _monitor_lock.

        Execution order (per tick):
          1. Per-leg SL loop: LTP fetch → trailing SL update → SL check
          2. Broker connectivity escalation (consecutive LTP-fail counter)
          3. Combined premium decay exit (both legs active)
          4. Winner-leg early booking (one leg surviving, deeply decayed)
          5. Combined P&L update (closed_pnl + open MTM)
          6. VIX spike check (throttled)
          7. Spot-move / breakeven breach check (throttled)
          8. Daily profit target check
          9. Daily loss limit check

        TRAILING SL INTEGRATION:
          _update_trailing_sl() is called BEFORE sl_level() so the freshest
          effective SL is always used for the breach test.

        PARTIAL LOGIC:
          Each leg has its own independent SL. Hitting SL on one leg calls
          close_one_leg() — the other leg keeps running.
          combined_pnl = closed_pnl (realised) + open_mtm (unrealised).
        """
        _shared._first_tick_fired = True  # Used by _close_all_locked() no-tick heuristic

        open_mtm     = 0.0
        legs_checked = 0   # Active legs we attempted to fetch LTP for
        legs_ltp_ok  = 0   # Active legs where LTP fetch succeeded
        leg_ltps: dict[str, float] = {}  # Valid LTPs for post-loop decay checks

        for leg in ["CE", "PE"]:
            active_key = f"{leg.lower()}_active"
            if not state[active_key]:
                continue

            legs_checked += 1
            entry_px = state[f"entry_price_{leg.lower()}"]

            # ── Fetch live LTP — must come before sl_level() when trailing on ──
            ltp = self._oe._fetch_ltp(leg)

            # Never fire SL on bad data
            if ltp <= 0:
                warn(f"LTP unavailable for {leg} this cycle — skipping SL check")
                continue

            legs_ltp_ok += 1
            leg_ltps[leg] = ltp

            leg_mtm = (entry_px - ltp) * qty() if entry_px > 0 else 0.0

            # ── Update trailing SL BEFORE evaluating sl_level() ──────────────
            if entry_px > 0:
                self._tsl.update(leg, ltp, entry_px)

            # ── Effective SL: trailing > breakeven > dynamic/fixed ────────────
            sl_lvl      = sl_level(leg)
            is_trailing = cfg.TRAILING_SL_ENABLED and state.get(f"trailing_active_{leg.lower()}", False)

            _be_sl_val    = state.get(f"breakeven_sl_{leg.lower()}", 0.0)
            _is_breakeven = (
                not is_trailing
                and cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED
                and state.get(f"breakeven_active_{leg.lower()}", False)
                and _be_sl_val > 0
                and sl_lvl == _be_sl_val
            )
            _is_dynamic = not is_trailing and not _is_breakeven and cfg.DYNAMIC_SL_ENABLED

            if is_trailing:
                sl_mode_label = "[TRAIL]"
            elif _is_breakeven:
                sl_mode_label = "[BREAKEVEN]"
            elif _is_dynamic:
                sl_mode_label = f"[DYNAMIC {_dynamic_sl_percent():.0f}%]"
            else:
                sl_mode_label = "[FIXED]"

            debug(
                f"  {leg} | entry Rs.{entry_px:.2f} | ltp Rs.{ltp:.2f} | "
                f"mtm Rs.{leg_mtm:.0f} | sl @ Rs.{sl_lvl:.2f} {sl_mode_label}"
            )

            # ── PER-LEG SL CHECK ──────────────────────────────────────────────
            if cfg.LEG_SL_PERCENT > 0 and sl_lvl > 0 and ltp >= sl_lvl:
                if is_trailing:
                    sl_type = "Trailing SL"
                elif _is_breakeven:
                    sl_type = "Breakeven SL"
                elif _is_dynamic:
                    sl_type = f"Dynamic SL {_dynamic_sl_percent():.0f}%"
                else:
                    sl_type = f"Fixed SL {cfg.LEG_SL_PERCENT}%"
                warn(
                    f"SL HIT [{sl_type}]: {leg}  |  "
                    f"LTP Rs.{ltp:.2f} >= SL Rs.{sl_lvl:.2f}  "
                    f"(entry Rs.{entry_px:.2f})"
                )
                self._oe.close_one_leg(
                    leg,
                    reason=f"{leg} {sl_type} Hit @ Rs.{sl_lvl:.2f}",
                    current_ltp=ltp,
                )
                continue  # This leg's P&L is now in closed_pnl, not open_mtm

            # ── No SL hit — accumulate to open MTM ───────────────────────────
            open_mtm += leg_mtm

        # ── Broker connectivity escalation ────────────────────────────────────
        if legs_checked > 0 and legs_ltp_ok == 0:
            _shared._consecutive_quote_fail_ticks += 1
            if _shared._consecutive_quote_fail_ticks == cfg.QUOTE_FAIL_ALERT_THRESHOLD:
                _shared._quote_fail_alerted = True
                elapsed_s = _shared._consecutive_quote_fail_ticks * cfg.MONITOR_INTERVAL_S
                warn(
                    f"Broker quotes UNREACHABLE for {_shared._consecutive_quote_fail_ticks} consecutive ticks "
                    f"(~{elapsed_s}s) — SL monitoring paused. Sending alert."
                )
                telegram(
                    f"⚠️ BROKER QUOTES UNREACHABLE\n"
                    f"LTP fetch has failed for {_shared._consecutive_quote_fail_ticks} consecutive monitor ticks "
                    f"(~{elapsed_s}s).\n"
                    f"Active legs : {active_legs()}\n"
                    f"SL monitoring is PAUSED until quotes recover.\n"
                    f"Check OpenAlgo / broker connection. Consider manual intervention."
                )
        elif legs_ltp_ok > 0 and _shared._quote_fail_alerted:
            warn(f"Broker quotes RESTORED after {_shared._consecutive_quote_fail_ticks} failed ticks")
            telegram(
                f"✅ BROKER QUOTES RESTORED\n"
                f"LTP fetch is working again after {_shared._consecutive_quote_fail_ticks} failed ticks.\n"
                f"SL monitoring resuming normally."
            )
            _shared._consecutive_quote_fail_ticks = 0
            _shared._quote_fail_alerted           = False
        elif legs_ltp_ok > 0:
            _shared._consecutive_quote_fail_ticks = 0

        # ── If both legs closed by SL(s) this tick → already flat ────────────
        if not state["in_position"]:
            return

        # ── COMBINED PREMIUM DECAY EXIT ───────────────────────────────────────
        # When BOTH legs are active and their combined LTP has decayed by
        # COMBINED_DECAY_TARGET_PCT, close the entire position.
        if cfg.COMBINED_DECAY_EXIT_ENABLED and state["ce_active"] and state["pe_active"]:
            ce_entry = state["entry_price_ce"]
            pe_entry = state["entry_price_pe"]
            if (
                ce_entry > 0
                and pe_entry > 0
                and "CE" in leg_ltps
                and "PE" in leg_ltps
            ):
                combined_entry   = ce_entry + pe_entry
                combined_current = leg_ltps["CE"] + leg_ltps["PE"]
                decay_pct        = round((1.0 - combined_current / combined_entry) * 100.0, 2)
                if decay_pct >= cfg.COMBINED_DECAY_TARGET_PCT:
                    info(
                        f"COMBINED DECAY EXIT: {decay_pct:.1f}% decay "
                        f"(CE Rs.{leg_ltps['CE']:.2f} + PE Rs.{leg_ltps['PE']:.2f} = "
                        f"Rs.{combined_current:.2f} vs entry Rs.{combined_entry:.2f}) "
                        f">= target {cfg.COMBINED_DECAY_TARGET_PCT:.0f}% — closing all"
                    )
                    telegram(
                        f"🎯 COMBINED DECAY EXIT\n"
                        f"Combined premium decayed {decay_pct:.1f}% "
                        f"(target {cfg.COMBINED_DECAY_TARGET_PCT:.0f}%)\n"
                        f"CE ltp Rs.{leg_ltps['CE']:.2f}  |  PE ltp Rs.{leg_ltps['PE']:.2f}\n"
                        f"Combined: Rs.{combined_current:.2f} vs entry Rs.{combined_entry:.2f}\n"
                        f"Locking in profits — closing both legs now."
                    )
                    self._oe.close_all(reason=f"Combined Premium {decay_pct:.1f}% Decay Target Reached")
                    return

        # ── WINNER-LEG EARLY BOOKING ──────────────────────────────────────────
        # One surviving leg, deeply decayed → close it to lock profits.
        # Uses a FRESH LTP fetch — never relies on the SL loop's LTP cache.
        if cfg.WINNER_LEG_EARLY_EXIT_ENABLED and state["in_position"]:
            active = active_legs()
            if len(active) == 1:
                winner       = active[0]
                winner_entry = state[f"entry_price_{winner.lower()}"]
                if winner_entry > 0:
                    winner_ltp = self._oe._fetch_ltp(winner)
                    if winner_ltp > 0:
                        decay_pct = (winner_ltp / winner_entry) * 100.0
                        if decay_pct <= cfg.WINNER_LEG_DECAY_THRESHOLD_PCT:
                            info(
                                f"WINNER LEG EARLY BOOKING: {winner} decayed to "
                                f"Rs.{winner_ltp:.2f} ({decay_pct:.1f}% of entry "
                                f"Rs.{winner_entry:.2f}) <= {cfg.WINNER_LEG_DECAY_THRESHOLD_PCT:.0f}% "
                                f"threshold — booking now to lock profit"
                            )
                            telegram(
                                f"💰 WINNER LEG EARLY BOOKING\n"
                                f"Surviving {winner} leg deeply profitable\n"
                                f"LTP Rs.{winner_ltp:.2f} = {decay_pct:.1f}% of entry "
                                f"Rs.{winner_entry:.2f}\n"
                                f"Booking at <= {cfg.WINNER_LEG_DECAY_THRESHOLD_PCT:.0f}% threshold "
                                f"to lock gains and remove gamma risk."
                            )
                            self._oe.close_one_leg(
                                winner,
                                reason=(
                                    f"Winner Leg Early Booking "
                                    f"({decay_pct:.1f}% of entry Rs.{winner_entry:.2f})"
                                ),
                                current_ltp=winner_ltp,
                            )
                            return

        # ── Combined P&L = closed (realised) + open (unrealised) ─────────────
        combined_pnl       = state["closed_pnl"] + open_mtm
        state["today_pnl"] = combined_pnl

        active = active_legs()
        info(
            f"MONITOR | Active: {active} | "
            f"Closed P&L: Rs.{state['closed_pnl']:.0f} | "
            f"Open MTM: Rs.{open_mtm:.0f} | "
            f"Combined: Rs.{combined_pnl:.0f} | "
            f"Target: Rs.{cfg.DAILY_PROFIT_TARGET} | "
            f"Limit: Rs.{cfg.DAILY_LOSS_LIMIT}"
        )

        # ── VIX spike check (throttled) ───────────────────────────────────────
        # Must run BEFORE daily target/limit — a spike exit fires even on profit.
        self._check_vix_spike()
        if not state["in_position"]:
            return  # VIX spike exit fired

        # ── Spot-move / breakeven breach check (throttled) ────────────────────
        self._check_spot_move()
        if not state["in_position"]:
            return  # Breakeven breach exit fired

        # ── DAILY PROFIT TARGET ───────────────────────────────────────────────
        if cfg.DAILY_PROFIT_TARGET > 0 and combined_pnl >= cfg.DAILY_PROFIT_TARGET:
            info(f"DAILY PROFIT TARGET Rs.{cfg.DAILY_PROFIT_TARGET} REACHED — closing all")
            self._oe.close_all(reason=f"Daily Profit Target Rs.{cfg.DAILY_PROFIT_TARGET} Reached")
            return

        # ── DAILY LOSS LIMIT ──────────────────────────────────────────────────
        if cfg.DAILY_LOSS_LIMIT < 0 and combined_pnl <= cfg.DAILY_LOSS_LIMIT:
            warn(f"DAILY LOSS LIMIT Rs.{cfg.DAILY_LOSS_LIMIT} BREACHED — closing all")
            self._oe.close_all(reason=f"Daily Loss Limit Rs.{cfg.DAILY_LOSS_LIMIT} Breached")
            return

    # ── Public monitor entry-point ────────────────────────────────────────────

    def monitor_pnl(self) -> None:
        """
        Monitor tick — runs every MONITOR_INTERVAL_S seconds (APScheduler job).

        Non-blocking lock ensures overlapping ticks are safely skipped rather
        than queued.

        FIX-X (v5.9.0): Tracks consecutive skipped ticks and sends a Telegram
        alert after 3 consecutive skips so the operator knows SL monitoring has
        been paused.  Resets the counter on the first tick that runs normally.
        """
        if not state["in_position"]:
            _shared._consecutive_monitor_skips = 0
            return

        acquired = _monitor_lock.acquire(blocking=False)
        if not acquired:
            _shared._consecutive_monitor_skips += 1
            warn(
                f"Monitor tick skipped — previous tick still running "
                f"(lock contention, skip #{_shared._consecutive_monitor_skips})"
            )
            if _shared._consecutive_monitor_skips == 3:
                elapsed_s = _shared._consecutive_monitor_skips * cfg.MONITOR_INTERVAL_S
                error(
                    f"Monitor BLOCKED for {_shared._consecutive_monitor_skips} consecutive ticks "
                    f"(~{elapsed_s}s) — SL protection is paused!"
                )
                telegram(
                    f"🚨 MONITOR BLOCKED\n"
                    f"Previous monitor tick has been running for ~{elapsed_s}s.\n"
                    f"SL checks are PAUSED — {_shared._consecutive_monitor_skips} ticks skipped.\n"
                    f"Active legs: {active_legs()}\n"
                    f"Check logs immediately. Consider manual monitoring."
                )
            return

        _shared._consecutive_monitor_skips = 0  # Successful lock — reset skip counter
        try:
            self.run_tick()
        finally:
            _monitor_lock.release()
