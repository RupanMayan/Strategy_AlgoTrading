"""
src/vix_manager.py  —  VIXManager class
═══════════════════════════════════════════════════════════════════════
Owns all VIX-related operations:
  • Fetch live India VIX (OpenAlgo primary + NSE direct fallback)
  • Load / parse vix_history.csv for IVR / IVP computation
  • Compute IV Rank (IVR) and IV Percentile (IVP)
  • Append today's closing VIX to history (daily maintenance job)
  • Auto-bootstrap history from NSE when the file is missing
  • Startup advisory check on history file quality
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, date, timedelta

import requests

from src._shared import (
    cfg, state,
    info, warn, error, debug, sep,
    telegram,
    _get_client,
    VIX_SYMBOL, INDEX_EXCH,
    now_ist,
    is_api_success,
)


class VIXManager:
    """
    Owns all VIX-related operations:
      • Fetch live India VIX (OpenAlgo primary + NSE direct fallback)
      • Load / parse vix_history.csv for IVR / IVP computation
      • Compute IV Rank (IVR) and IV Percentile (IVP)
      • Append today's closing VIX to history (daily maintenance job)
      • Auto-bootstrap history from NSE when the file is missing
      • Startup advisory check on history file quality

    All methods are intentionally stateless with respect to instance variables —
    they read from cfg and write only to the CSV file or to state["vix_at_entry"].
    This makes VIXManager safe to call from background threads and multiple jobs.
    """

    _NSE_HEADERS = {
        "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept"          : "application/json, text/plain, */*",
        "Accept-Language" : "en-US,en;q=0.9",
        "Referer"         : "https://www.nseindia.com/",
    }

    def __init__(self) -> None:
        # Persistent session for NSE fallback — avoids creating a new session per call
        self._nse_session: requests.Session | None = None

    def _get_nse_session(self) -> requests.Session:
        """Return a reusable NSE session with cookies pre-initialized."""
        if self._nse_session is None:
            self._nse_session = requests.Session()
            self._nse_session.headers.update(self._NSE_HEADERS)
            try:
                self._nse_session.get("https://www.nseindia.com", timeout=8)
            except Exception:
                pass  # Cookie pre-auth is best-effort
        return self._nse_session

    # ── Live VIX fetch ────────────────────────────────────────────────────────

    def fetch_vix(self) -> float:
        """
        Fetch India VIX LTP.

        Primary  : OpenAlgo SDK quotes()
        Fallback : NSE direct API (with cookie pre-auth)

        Returns float > 0 on success, -1.0 on total failure.
        """
        # Primary: OpenAlgo SDK
        try:
            resp = _get_client().quotes(symbol=VIX_SYMBOL, exchange=INDEX_EXCH)
            if is_api_success(resp):
                ltp = float(resp.get("data", {}).get("ltp", -1))
                if ltp > 0:
                    info(f"India VIX (OpenAlgo): {ltp:.2f}")
                    return ltp
        except Exception as exc:
            warn(f"OpenAlgo VIX exception: {exc}")

        # Fallback: NSE direct API (reuses persistent session)
        try:
            sess = self._get_nse_session()
            r = sess.get("https://www.nseindia.com/api/allIndices", timeout=8)
            r.raise_for_status()
            for item in r.json().get("data", []):
                if item.get("index", "").replace(" ", "").upper() == "INDIAVIX":
                    vix = float(item["last"])
                    info(f"India VIX (NSE fallback): {vix:.2f}")
                    return vix
        except Exception as exc:
            # Reset session on failure so next call re-initializes cookies
            self._nse_session = None
            warn(f"NSE VIX fallback exception: {exc}")

        error("India VIX unavailable from all sources")
        return -1.0

    # ── History CSV helpers ───────────────────────────────────────────────────

    def load_history_raw(self) -> list[tuple[str, float]]:
        """
        Load all rows from cfg.VIX_HISTORY_FILE.

        Returns a list of (date_str, vix_float) tuples sorted chronologically.
        Returns [] if the file does not exist, is empty, or cannot be parsed.
        Malformed individual rows are skipped silently.
        Duplicate dates are deduplicated — last occurrence per date is kept.
        """
        if not os.path.exists(cfg.VIX_HISTORY_FILE):
            return []
        try:
            rows: list[tuple[str, float]] = []
            with open(cfg.VIX_HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.lower().startswith("date"):
                        continue
                    parts = line.split(",")
                    if len(parts) < 2:
                        continue
                    try:
                        date_str = parts[0].strip()
                        vix_val  = float(parts[1].strip())
                        if vix_val > 0:
                            rows.append((date_str, vix_val))
                    except (ValueError, IndexError):
                        continue

            rows.sort(key=lambda x: x[0])

            # Deduplicate by date — keep last occurrence per date
            seen: dict[str, float] = {}
            for d, v in rows:
                seen[d] = v
            return list(seen.items())

        except Exception as exc:
            warn(f"VIX history raw load failed: {exc}")
            return []

    def load_history(self) -> list[float] | None:
        """
        Load the last 252 VIX closing values for IVR/IVP calculation.

        Returns list of floats (oldest first) when sufficient data exists.
        Returns None when row count < cfg.VIX_HISTORY_MIN_ROWS — caller applies
        the IVR_FAIL_OPEN policy.
        """
        rows = self.load_history_raw()
        n    = len(rows)

        if n < cfg.VIX_HISTORY_MIN_ROWS:
            warn(
                f"VIX history: {n} rows — need at least {cfg.VIX_HISTORY_MIN_ROWS} "
                f"for IVR/IVP. "
                f"{'Bootstrap the file from NSE historical data.' if n == 0 else 'Collecting daily data...'}"
            )
            return None

        recent = rows[-252:]
        return [v for _, v in recent]

    # ── IVR / IVP computation ─────────────────────────────────────────────────

    def compute_ivr(self, current_vix: float, history_values: list[float]) -> float:
        """
        IV Rank = (current − 52wk_low) / (52wk_high − 52wk_low) × 100.

        Returns 50.0 when high == low (degenerate flat-VIX history edge case).
        Clamped to [0, 100].
        """
        low  = min(history_values)
        high = max(history_values)
        if high == low:
            warn(f"compute_ivr: 52wk high == low ({high:.2f}) — returning neutral 50.0")
            return 50.0
        ivr = (current_vix - low) / (high - low) * 100.0
        return round(max(0.0, min(100.0, ivr)), 1)

    def compute_ivp(self, current_vix: float, history_values: list[float]) -> float:
        """
        IV Percentile = count(days VIX < current) / total × 100.

        Uses strict less-than (standard definition).
        Returns 50.0 on empty history (safe neutral).
        """
        if not history_values:
            return 50.0
        days_below = sum(1 for v in history_values if v < current_vix)
        return round(days_below / len(history_values) * 100.0, 1)

    def ivr_ivp_ok(self, current_vix: float) -> bool:
        """
        IVR / IVP filter gate — called from FilterEngine after vix_ok() passes.

        Receives current_vix already fetched by vix_ok() — no duplicate API call.

        Logic:
          1. Both filters disabled → return True immediately
          2. Load 252-day VIX history → apply IVR_FAIL_OPEN on missing data
          3. Compute IVR and IVP (always, for analytics even if filter disabled)
          4. IVR gate (if enabled)
          5. IVP gate (if enabled)
          6. Store ivr_at_entry / ivp_at_entry in state
        """
        if not cfg.IVR_FILTER_ENABLED and not cfg.IVP_FILTER_ENABLED:
            info("IVR/IVP filter: both disabled — skipping check")
            return True

        sep()
        info("IVR/IVP FILTER CHECK")
        info(f"  Current VIX : {current_vix:.2f}")

        history_values = self.load_history()

        if history_values is None:
            if cfg.IVR_FAIL_OPEN:
                state["ivr_at_entry"] = -1.0
                state["ivp_at_entry"] = -1.0
                warn(
                    "IVR/IVP: VIX history insufficient — fail-open policy, "
                    "proceeding. Bootstrap vix_history.csv for full protection."
                )
                telegram(
                    "⚠️ IVR/IVP filter: VIX history insufficient\n"
                    "Proceeding (fail-open). Bootstrap vix_history.csv."
                )
                sep()
                return True
            else:
                warn(
                    "IVR/IVP: VIX history insufficient — fail-closed policy, "
                    "skipping trade. Bootstrap vix_history.csv to enable this filter."
                )
                telegram(
                    "IVR/IVP filter: VIX history insufficient — trade SKIPPED (fail-closed).\n"
                    "Bootstrap vix_history.csv from NSE historical VIX data."
                )
                sep()
                return False

        n    = len(history_values)
        low  = min(history_values)
        high = max(history_values)

        ivr = self.compute_ivr(current_vix, history_values)
        ivp = self.compute_ivp(current_vix, history_values)

        info(f"  History     : {n} days  |  52wk Low: {low:.2f}  52wk High: {high:.2f}")
        info(f"  IVR         : {ivr:.1f}  (threshold: >= {cfg.IVR_MIN}  |  enabled: {cfg.IVR_FILTER_ENABLED})")
        info(f"  IVP         : {ivp:.1f}%  (threshold: >= {cfg.IVP_MIN}%  |  enabled: {cfg.IVP_FILTER_ENABLED})")

        if cfg.IVR_FILTER_ENABLED:
            if ivr < cfg.IVR_MIN:
                warn(
                    f"  IVR CHECK: FAIL ✗  "
                    f"IVR {ivr:.1f} < {cfg.IVR_MIN} — "
                    f"IV in bottom {ivr:.0f}% of 52-week range, not rich enough to sell"
                )
                sep()
                telegram(
                    f"IVR filter: SKIP today\n"
                    f"IVR {ivr:.1f} &lt; {cfg.IVR_MIN} — IV not historically rich\n"
                    f"VIX: {current_vix:.2f}  |  52wk range: {low:.2f}–{high:.2f}"
                )
                return False
            info(f"  IVR CHECK: PASS ✓  IVR {ivr:.1f} >= {cfg.IVR_MIN}")

        if cfg.IVP_FILTER_ENABLED:
            if ivp < cfg.IVP_MIN:
                warn(
                    f"  IVP CHECK: FAIL ✗  "
                    f"IVP {ivp:.1f}% < {cfg.IVP_MIN}% — "
                    f"VIX below {ivp:.0f}% of past {n} trading days"
                )
                sep()
                telegram(
                    f"IVP filter: SKIP today\n"
                    f"IVP {ivp:.1f}% &lt; {cfg.IVP_MIN}% — IV below historical median\n"
                    f"VIX: {current_vix:.2f}  |  Days below today's VIX: {int(ivp * n / 100)}/{n}"
                )
                return False
            info(f"  IVP CHECK: PASS ✓  IVP {ivp:.1f}% >= {cfg.IVP_MIN}%")

        state["ivr_at_entry"] = ivr
        state["ivp_at_entry"] = ivp
        info("  IVR/IVP filter: PASS ✓ — IV is historically rich")
        sep()
        return True

    # ── Daily history update ──────────────────────────────────────────────────

    def update_history(self) -> None:
        """
        Append today's closing VIX to cfg.VIX_HISTORY_FILE.

        Called once at cfg.VIX_UPDATE_TIME (default 15:30 IST).
        Duplicate-safe (idempotent), atomic write (temp + rename).
        """
        now_dt = now_ist()

        if now_dt.weekday() >= 5:
            debug("VIX history update: weekend — skipping")
            return

        today_str = now_dt.date().isoformat()

        rows = self.load_history_raw()
        if rows and rows[-1][0] == today_str:
            debug(
                f"VIX history: {today_str} already recorded "
                f"(VIX {rows[-1][1]:.2f}) — no update needed"
            )
            return

        vix = self.fetch_vix()
        if vix <= 0:
            warn(f"VIX history update: VIX unavailable for {today_str} — skipping")
            telegram(
                f"⚠️ VIX history: daily update FAILED for {today_str}\n"
                f"IVR/IVP data will be 1 day stale tomorrow.\n"
                f"Check OpenAlgo / NSE connectivity."
            )
            return

        rows.append((today_str, vix))
        if len(rows) > 300:
            rows = rows[-300:]

        try:
            hist_dir     = os.path.dirname(os.path.abspath(cfg.VIX_HISTORY_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(dir=hist_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("date,vix_close\n")
                    for d, v in rows:
                        f.write(f"{d},{v:.2f}\n")
                os.replace(tmp_path, cfg.VIX_HISTORY_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            info(
                f"VIX history updated: {today_str} → VIX {vix:.2f}  "
                f"({len(rows)} rows in {cfg.VIX_HISTORY_FILE})"
            )
        except Exception as exc:
            warn(f"VIX history write failed: {exc}")
            telegram(f"⚠️ VIX history write FAILED: {exc}")

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def bootstrap_history(self) -> bool:
        """
        Fetch 2 years of NSE historical VIX data and write cfg.VIX_HISTORY_FILE.

        NSE API caps each response at ~70 records, so the 2-year window is split
        into 90-day calendar chunks. A 1.5s delay between chunks respects NSE
        rate limiting (FIX-VI v5.5.0).

        Returns True on success (file written with >= 1 row), False on failure.
        Never raises — all errors are logged as warnings.
        """
        sep()
        info("VIX HISTORY AUTO-BOOTSTRAP — fetching 2 years from NSE")

        today       = now_ist().date()
        start_date  = today - timedelta(days=730)
        chunks      = []
        chunk_start = start_date
        while chunk_start < today:
            chunk_end = min(chunk_start + timedelta(days=89), today)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        info(
            f"  Date range  : {chunks[0][0].strftime('%d-%m-%Y')} → "
            f"{chunks[-1][1].strftime('%d-%m-%Y')}  ({len(chunks)} chunks)"
        )
        info(f"  Target file : {os.path.abspath(cfg.VIX_HISTORY_FILE)}")

        raw: list = []
        try:
            sess = requests.Session()
            sess.headers.update(self._NSE_HEADERS)
            sess.get("https://www.nseindia.com", timeout=10)
            sess.get(
                "https://www.nseindia.com/reports-indices-historical-vix",
                timeout=10,
            )
            for i, (chunk_from, chunk_to) in enumerate(chunks):
                if i > 0:
                    time.sleep(1.5)
                from_str = chunk_from.strftime("%d-%m-%Y")
                to_str   = chunk_to.strftime("%d-%m-%Y")
                url = (
                    f"https://www.nseindia.com/api/historicalOR/vixhistory"
                    f"?from={from_str}&to={to_str}"
                )
                r = sess.get(url, timeout=20)
                r.raise_for_status()
                chunk_data = r.json().get("data", [])
                info(f"  Chunk {from_str} → {to_str}: {len(chunk_data)} records")
                raw.extend(chunk_data)

        except Exception as exc:
            warn(f"  Bootstrap: NSE request failed — {exc}")
            sep()
            return False

        if not raw:
            warn("  Bootstrap: NSE returned empty data array")
            sep()
            return False

        rows: list[tuple[str, float]] = []
        for item in raw:
            date_raw = (
                item.get("EOD_TIMESTAMP") or item.get("Date") or
                item.get("date")          or ""
            )
            close_raw = (
                item.get("EOD_CLOSE_INDEX_VAL") or item.get("EOD_INDEX_VALUE") or
                item.get("CLOSE")               or item.get("Close") or
                item.get("close")               or ""
            )
            if not date_raw or not close_raw:
                continue

            parsed_date = None
            for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    parsed_date = datetime.strptime(str(date_raw).strip(), fmt).date()
                    break
                except ValueError:
                    continue
            if parsed_date is None:
                continue

            try:
                vix_val = float(str(close_raw).replace(",", "").strip())
                if vix_val > 0:
                    rows.append((parsed_date.isoformat(), vix_val))
            except (ValueError, TypeError):
                continue

        if not rows:
            warn("  Bootstrap: parsed 0 valid rows from NSE response")
            warn("  NSE API may have changed — download manually from nseindia.com")
            sep()
            return False

        rows.sort(key=lambda x: x[0])

        try:
            hist_dir     = os.path.dirname(os.path.abspath(cfg.VIX_HISTORY_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(dir=hist_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("date,vix_close\n")
                    for d, v in rows:
                        f.write(f"{d},{v:.2f}\n")
                os.replace(tmp_path, cfg.VIX_HISTORY_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            warn(f"  Bootstrap: file write failed — {exc}")
            sep()
            return False

        info(f"  Bootstrap SUCCESS: {len(rows)} rows written to {cfg.VIX_HISTORY_FILE}")
        info(f"  Range: {rows[0][0]} → {rows[-1][0]}  |  Latest VIX: {rows[-1][1]:.2f}")
        telegram(
            f"✅ VIX history bootstrapped from NSE\n"
            f"{len(rows)} rows  |  {rows[0][0]} → {rows[-1][0]}\n"
            f"IVR/IVP filter is now active."
        )
        sep()
        return True

    # ── Startup advisory check ────────────────────────────────────────────────

    def check_on_startup(self) -> None:
        """
        Validate VIX history file at startup and log actionable status.

        Checks: file exists, row count >= minimum, staleness.
        Auto-bootstraps if file is missing.
        Does NOT block startup — advisory logging only.
        """
        if not cfg.IVR_FILTER_ENABLED and not cfg.IVP_FILTER_ENABLED:
            info("IVR/IVP filter disabled — skipping VIX history startup check")
            return

        sep()
        info("VIX HISTORY STARTUP CHECK")

        if not os.path.exists(cfg.VIX_HISTORY_FILE):
            warn(f"  VIX history file NOT FOUND: {os.path.abspath(cfg.VIX_HISTORY_FILE)}")
            info("  Auto-bootstrapping from NSE historical VIX data...")
            success = self.bootstrap_history()
            if not success:
                warn("  Auto-bootstrap FAILED.")
                warn("  IVR/IVP filter will SKIP trades (fail-closed) until file is created.")
                warn("  Manual fix: call manual_bootstrap_vix() and run once.")
                sep()
                return

        rows = self.load_history_raw()
        n    = len(rows)

        if n == 0:
            warn(f"  VIX history file EXISTS but has 0 valid rows: {cfg.VIX_HISTORY_FILE}")
            warn("  Check format: header must be 'date,vix_close', values must be numeric")
            sep()
            return

        latest_date_str = rows[-1][0]
        latest_vix      = rows[-1][1]

        info(f"  File        : {os.path.abspath(cfg.VIX_HISTORY_FILE)}")
        info(f"  Rows        : {n}  (need >= {cfg.VIX_HISTORY_MIN_ROWS} for full accuracy)")
        info(f"  Latest entry: {latest_date_str}  VIX {latest_vix:.2f}")

        if n < cfg.VIX_HISTORY_MIN_ROWS:
            warn(
                f"  Row count {n} < {cfg.VIX_HISTORY_MIN_ROWS} minimum. "
                f"{'Add more history from NSE data.' if n < 50 else 'Growing — will improve over time.'}"
            )

        try:
            latest_dt = date.fromisoformat(latest_date_str)
            today     = now_ist().date()
            days_old  = (today - latest_dt).days
            if days_old > 5:
                warn(
                    f"  ⚠ VIX history is {days_old} calendar days stale "
                    f"(last: {latest_date_str}). "
                    f"The {cfg.VIX_UPDATE_TIME} auto-update job will fix this today."
                )
            else:
                info(f"  Freshness   : {days_old} calendar day(s) old — OK")
        except (ValueError, TypeError):
            warn(f"  Could not parse latest date: {latest_date_str!r}")

        sep()
