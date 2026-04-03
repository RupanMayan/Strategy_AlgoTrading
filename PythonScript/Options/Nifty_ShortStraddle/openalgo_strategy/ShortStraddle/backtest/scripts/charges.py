"""
Brokerage & Statutory Charges Calculator — Dhan
Mirrors actual Dhan F&O options charges for accurate P&L.

STT regime history for options (sell-side):
  Before Oct 1, 2024  → 0.0625%
  Oct 1, 2024 onwards → 0.1%    (Budget 2024)
  Apr 1, 2026 onwards → 0.15%   (Budget 2026)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date

# STT rate schedule: (effective_date, rate_pct) — must be sorted ascending
_STT_SCHEDULE: list[tuple[date, float]] = [
    (date(2024, 10, 1), 0.1),    # Budget 2024
    (date(2026, 4, 1),  0.15),   # Budget 2026
]
_STT_BASE_RATE = 0.0625  # Rate before first schedule entry


def stt_rate_for_date(trade_date: date | None = None) -> float:
    """Return the correct STT sell-side % for a given trade date."""
    if trade_date is None:
        return _STT_BASE_RATE
    rate = _STT_BASE_RATE
    for effective, pct in _STT_SCHEDULE:
        if trade_date >= effective:
            rate = pct
    return rate


@dataclass
class ChargesConfig:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.0625       # Base STT (used when no trade_date given)
    exchange_txn_pct: float = 0.053    # NSE F&O exchange transaction
    sebi_pct: float = 0.0001           # SEBI turnover fee
    gst_pct: float = 18.0              # GST on (brokerage + exchange + SEBI)
    stamp_duty_buy_pct: float = 0.003  # Stamp duty on buy-side


def calc_order_charges(
    premium: float,
    qty: int,
    is_sell: bool,
    cfg: ChargesConfig = ChargesConfig(),
    trade_date: date | None = None,
) -> dict[str, float]:
    """Calculate charges for a single order (one leg, one side).

    Args:
        premium: per-unit option premium (LTP at fill)
        qty: number of units (lot_size * lots)
        is_sell: True for SELL order, False for BUY order
        cfg: charges config
        trade_date: date of trade (for correct STT rate)
    Returns:
        dict with itemised charges and total
    """
    turnover = premium * qty

    brokerage = cfg.brokerage_per_order
    stt_pct = stt_rate_for_date(trade_date) if trade_date else cfg.stt_sell_pct
    stt = turnover * stt_pct / 100 if is_sell else 0.0
    exchange_txn = turnover * cfg.exchange_txn_pct / 100
    sebi = turnover * cfg.sebi_pct / 100
    gst = (brokerage + exchange_txn + sebi) * cfg.gst_pct / 100
    stamp = turnover * cfg.stamp_duty_buy_pct / 100 if not is_sell else 0.0

    total = brokerage + stt + exchange_txn + sebi + gst + stamp

    return {
        "turnover": round(turnover, 2),
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi": round(sebi, 2),
        "gst": round(gst, 2),
        "stamp_duty": round(stamp, 2),
        "total": round(total, 2),
    }


def calc_trade_charges(
    entry_ce: float,
    entry_pe: float,
    exit_ce: float,
    exit_pe: float,
    qty: int,
    cfg: ChargesConfig = ChargesConfig(),
    ce_exited: bool = True,
    pe_exited: bool = True,
    trade_date: date | None = None,
) -> dict[str, float]:
    """Calculate total charges for a full straddle round-trip.

    Entry = 2 SELL orders (CE + PE).
    Exit  = up to 2 BUY orders (CE + PE).

    Args:
        entry_ce/pe: entry premium per unit
        exit_ce/pe: exit premium per unit (0 if leg not exited)
        qty: quantity per leg
        cfg: charges config
        ce_exited/pe_exited: whether each leg was exited
        trade_date: date of trade (for correct STT rate)
    Returns:
        dict with total charges breakdown
    """
    total = {
        "brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.0,
        "sebi": 0.0, "gst": 0.0, "stamp_duty": 0.0, "total": 0.0,
        "num_orders": 0,
    }

    orders = []
    # Entry SELL orders
    if entry_ce > 0:
        orders.append((entry_ce, True))
    if entry_pe > 0:
        orders.append((entry_pe, True))
    # Exit BUY orders
    if ce_exited and exit_ce > 0:
        orders.append((exit_ce, False))
    if pe_exited and exit_pe > 0:
        orders.append((exit_pe, False))

    for premium, is_sell in orders:
        c = calc_order_charges(premium, qty, is_sell, cfg, trade_date)
        for key in ["brokerage", "stt", "exchange_txn", "sebi", "gst", "stamp_duty", "total"]:
            total[key] += c[key]
        total["num_orders"] += 1

    # Round totals
    for key in total:
        if isinstance(total[key], float):
            total[key] = round(total[key], 2)

    return total
