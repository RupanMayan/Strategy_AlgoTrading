"""
util/market_calendar.py  —  NSE Market Calendar & Trading Day Check
═══════════════════════════════════════════════════════════════════════
Determines whether the NSE/NFO market is open on a given date.

Three-layer check:
  1. Weekend guard    — Saturday/Sunday are always closed
  2. OpenAlgo API     — fetches holiday list from /api/v1/market/holidays
  3. Static fallback  — hardcoded NSE holiday calendar (if API unavailable)

The API is called once at startup and cached for the entire session.

Usage:
  from util.market_calendar import is_market_open, get_holiday_name

  if not is_market_open(today):
      print(f"Market closed: {get_holiday_name(today)}")
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date, datetime

from util.config_util import cfg
from util.logger import info, warn, debug


# ═══════════════════════════════════════════════════════════════════════════════
#  HOLIDAY CACHE — populated once from API, or falls back to static list
#
#  Key = date, Value = holiday description string
#  Populated by _load_holidays() on first call to is_market_open().
# ═══════════════════════════════════════════════════════════════════════════════

_holidays_cache: dict[int, dict[date, str]] = {}


def _fetch_holidays_from_api(year: int) -> dict[date, str] | None:
    """
    Fetch NSE/NFO trading holidays from OpenAlgo holidays API.

    Calls: POST /api/v1/market/holidays with year parameter.
    Filters for holidays where "NFO" is in closed_exchanges
    (since we trade F&O options).

    Returns dict of {date: description}, or None on failure.
    Returns None (not empty dict) when API returns zero NFO holidays,
    so the caller falls back to the static list.
    """
    from openalgo import api as OpenAlgoClient

    try:
        client = OpenAlgoClient(
            api_key=cfg.OPENALGO_API_KEY,
            host=cfg.OPENALGO_HOST,
        )
        resp = client.holidays(year=year)

        if resp.get("status") != "success" or not resp.get("data"):
            warn(f"Holidays API: unexpected response: {resp}")
            return None

        holidays: dict[date, str] = {}
        for entry in resp["data"]:
            # Only consider holidays where NFO (F&O) is closed
            closed = entry.get("closed_exchanges", [])
            if "NFO" not in closed:
                continue

            date_str = entry.get("date", "")
            description = entry.get("description", "Market Holiday")
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                holidays[d] = description
            except (ValueError, TypeError):
                debug(f"Holidays API: skipping unparseable date: {date_str}")

        if not holidays:
            warn(f"Holidays API: returned 0 NFO holidays for {year} — treating as failure")
            return None

        info(f"Holidays API: loaded {len(holidays)} NFO holidays for {year}")
        return holidays

    except Exception as exc:
        warn(f"Holidays API call failed: {exc}")
        return None


def _load_holidays(year: int) -> dict[date, str]:
    """
    Load holidays for the given year. Tries API first, falls back to static list.
    """
    api_holidays = _fetch_holidays_from_api(year)
    if api_holidays is not None:
        return api_holidays

    # Fallback to static list
    warn("Holidays API unavailable — using static holiday calendar as fallback")
    return {d: name for d, name in _STATIC_HOLIDAYS.items() if d.year == year}


def _get_holidays(year: int) -> dict[date, str]:
    """Return cached holidays for the given year, loading on first call per year."""
    if year not in _holidays_cache:
        _holidays_cache[year] = _load_holidays(year)
    return _holidays_cache[year]


def is_market_open(d: date) -> bool:
    """
    Check if NSE/NFO market is open on the given date.

    Returns False for weekends (Saturday/Sunday) and trading holidays.
    Returns True for all other weekdays.
    """
    if d.weekday() >= 5:
        return False
    holidays = _get_holidays(d.year)
    return d not in holidays


def get_holiday_name(d: date) -> str:
    """
    Return the reason the market is closed on the given date.

    Returns the holiday name for trading holidays, "Saturday"/"Sunday"
    for weekends, or "Trading Day" if the market is open.
    """
    if d.weekday() == 5:
        return "Saturday"
    if d.weekday() == 6:
        return "Sunday"
    holidays = _get_holidays(d.year)
    return holidays.get(d, "Trading Day")


# ═══════════════════════════════════════════════════════════════════════════════
#  STATIC FALLBACK — used only when OpenAlgo holidays API is unavailable
#
#  Source: NSE circulars (https://www.nseindia.com/regulations/holiday-list)
#  Excludes weekends — only lists weekday closures.
# ═══════════════════════════════════════════════════════════════════════════════

_STATIC_HOLIDAYS: dict[date, str] = {
    # ── 2025 ─────────────────────────────────────────────────────────────
    date(2025, 2, 26):  "Mahashivratri",
    date(2025, 3, 14):  "Holi",
    date(2025, 3, 31):  "Id-Ul-Fitr (Eid)",
    date(2025, 4, 10):  "Shri Mahavir Jayanti",
    date(2025, 4, 14):  "Dr. Baba Saheb Ambedkar Jayanti",
    date(2025, 4, 18):  "Good Friday",
    date(2025, 5, 1):   "Maharashtra Day",
    date(2025, 6, 7):   "Bakrid / Eid-Ul-Adha",
    date(2025, 8, 15):  "Independence Day",
    date(2025, 8, 16):  "Parsi New Year",
    date(2025, 8, 27):  "Ganesh Chaturthi",
    date(2025, 10, 2):  "Mahatma Gandhi Jayanti",
    date(2025, 10, 21): "Diwali Laxmi Pujan",
    date(2025, 10, 22): "Diwali Balipratipada",
    date(2025, 11, 5):  "Prakash Gurpurab Sri Guru Nanak Dev",
    date(2025, 12, 25): "Christmas",
    # ── 2026 ─────────────────────────────────────────────────────────────
    date(2026, 1, 26):  "Republic Day",
    date(2026, 2, 17):  "Mahashivratri",
    date(2026, 3, 4):   "Holi",
    date(2026, 3, 20):  "Id-Ul-Fitr (Eid)",
    date(2026, 3, 26):  "Shri Ram Navami",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 4, 14):  "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1):   "Maharashtra Day",
    date(2026, 5, 28):  "Bakrid / Eid-Ul-Adha",
    date(2026, 8, 6):   "Parsi New Year",
    date(2026, 8, 15):  "Independence Day",
    date(2026, 8, 18):  "Ganesh Chaturthi",
    date(2026, 10, 2):  "Mahatma Gandhi Jayanti",
    date(2026, 10, 9):  "Diwali Laxmi Pujan",
    date(2026, 10, 12): "Diwali Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurab Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}
