"""NSE holiday calendar and DTE/expiry utilities for backtesting."""

from datetime import date, timedelta
from typing import Optional

# NSE market holidays 2025-2026 (manually curated from NSE circulars)
# Excludes weekends (Saturday/Sunday) — only lists weekday closures
NSE_HOLIDAYS_2025_2026: set[date] = {
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Eid)
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 6, 7),    # Bakrid / Eid-Ul-Adha
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 16),   # Parsi New Year
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurab Sri Guru Nanak Dev
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Mahashivratri
    date(2026, 3, 4),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr (Eid) — tentative
    date(2026, 3, 30),   # Idul Fitr
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakrid / Eid-Ul-Adha
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 9),   # Diwali Laxmi Pujan
    date(2026, 11, 24),  # Prakash Gurpurab Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(d: date) -> bool:
    """Check if a date is an NSE trading day (weekday + not holiday)."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS_2025_2026


def next_trading_day(d: date) -> date:
    """Return the next trading day on or after the given date."""
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    """Return the previous trading day on or before the given date."""
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def get_weekly_expiry(d: date) -> date:
    """
    Get the NIFTY weekly expiry date for the week containing date `d`.

    NIFTY weekly expiry is Tuesday. If Tuesday is a holiday,
    expiry moves to the previous trading day (typically Monday).
    """
    # Find the Tuesday of the current week (weekday 1 = Tuesday)
    days_ahead = 1 - d.weekday()  # 1 = Tuesday
    if days_ahead < 0:
        days_ahead += 7
    tuesday = d + timedelta(days=days_ahead)

    # If Tuesday is a holiday, expiry is the previous trading day
    if is_trading_day(tuesday):
        return tuesday
    return prev_trading_day(tuesday)


def compute_dte(d: date) -> int:
    """
    Compute DTE (Days To Expiry) as trading days from `d` to its weekly expiry.

    Returns 0 on expiry day, 1 on the day before, etc.
    """
    expiry = get_weekly_expiry(d)

    # If d is after this week's expiry, look at next week
    if d > expiry:
        next_tue = d + timedelta(days=(1 - d.weekday() + 7) % 7)
        if next_tue == d:
            next_tue += 7
        expiry = get_weekly_expiry(next_tue)

    # Count trading days between d and expiry (exclusive of d, inclusive of expiry)
    count = 0
    current = d
    while current < expiry:
        current += timedelta(days=1)
        if is_trading_day(current):
            count += 1
    return count


def get_trading_days_in_range(start: date, end: date) -> list[date]:
    """Return all trading days in [start, end] inclusive."""
    days = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days
