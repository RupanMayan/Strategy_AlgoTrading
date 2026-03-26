"""
util/market_calendar.py  —  NSE Market Calendar & Trading Day Check
═══════════════════════════════════════════════════════════════════════
Determines whether the NSE market is open on a given date.

Two-layer check:
  1. Weekend guard    — Saturday/Sunday are always closed
  2. Holiday calendar — NSE-published weekday closures

The holiday list is maintained from official NSE circulars.
Update annually when NSE publishes the next year's calendar
(typically released in December for the following year).

Usage:
  from util.market_calendar import is_market_open, get_holiday_name

  if not is_market_open(today):
      print(f"Market closed: {get_holiday_name(today)}")
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import date


# ═══════════════════════════════════════════════════════════════════════════════
#  NSE HOLIDAY CALENDAR
#
#  Source: NSE circulars (https://www.nseindia.com/regulations/holiday-list)
#  Excludes weekends — only lists WEEKDAY closures.
#  Update this dict when NSE publishes the next year's schedule.
# ═══════════════════════════════════════════════════════════════════════════════

NSE_HOLIDAYS: dict[date, str] = {
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
    date(2026, 3, 30):  "Idul Fitr",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 4, 14):  "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1):   "Maharashtra Day",
    date(2026, 5, 28):  "Bakrid / Eid-Ul-Adha",
    date(2026, 8, 15):  "Independence Day",
    date(2026, 10, 2):  "Mahatma Gandhi Jayanti",
    date(2026, 10, 9):  "Diwali Laxmi Pujan",
    date(2026, 11, 24): "Prakash Gurpurab Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}


def is_market_open(d: date) -> bool:
    """
    Check if NSE market is open on the given date.

    Returns False for weekends (Saturday/Sunday) and NSE holidays.
    Returns True for all other weekdays.
    """
    if d.weekday() >= 5:
        return False
    return d not in NSE_HOLIDAYS


def get_holiday_name(d: date) -> str:
    """
    Return the reason the market is closed on the given date.

    Returns the holiday name for NSE holidays, "Saturday"/"Sunday" for
    weekends, or "Trading Day" if the market is open.
    """
    if d.weekday() == 5:
        return "Saturday"
    if d.weekday() == 6:
        return "Sunday"
    return NSE_HOLIDAYS.get(d, "Trading Day")
