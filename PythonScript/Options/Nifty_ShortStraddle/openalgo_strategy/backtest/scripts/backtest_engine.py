"""
Nifty Short Straddle — Backtest Engine
1:1 mirror of production nifty_short_straddle.py exit logic.
Replays 1-min candles day-by-day with all 13 exit modules.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import toml

IST = pytz.timezone("Asia/Kolkata")

from charges import ChargesConfig, calc_trade_charges

# ── NSE Holidays (2021-2026) ────────────────────────────────────────────────
# Source: NSE circulars. Update as needed.
NSE_HOLIDAYS = {
    # 2021
    "2021-01-26", "2021-03-11", "2021-03-29", "2021-04-02", "2021-04-14",
    "2021-04-21", "2021-05-13", "2021-07-21", "2021-08-19", "2021-09-10",
    "2021-10-15", "2021-11-04", "2021-11-05", "2021-11-19",
    # 2022
    "2022-01-26", "2022-03-01", "2022-03-18", "2022-04-14", "2022-04-15",
    "2022-05-03", "2022-08-09", "2022-08-15", "2022-08-31", "2022-10-05",
    "2022-10-24", "2022-10-26", "2022-11-08",
    # 2023
    "2023-01-26", "2023-03-07", "2023-03-30", "2023-04-04", "2023-04-07",
    "2023-04-14", "2023-05-01", "2023-06-28", "2023-08-15", "2023-09-19",
    "2023-10-02", "2023-10-24", "2023-11-14", "2023-11-27", "2023-12-25",
    # 2024
    "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11",
    "2024-04-14", "2024-04-17", "2024-04-21", "2024-05-20", "2024-05-23",
    "2024-06-17", "2024-07-17", "2024-08-15", "2024-09-16", "2024-10-02",
    "2024-10-12", "2024-11-01", "2024-11-15", "2024-11-20", "2024-12-25",
    # 2025
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
    "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27",
    "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05", "2025-11-26",
    "2025-12-25",
    # 2026
    "2026-01-26", "2026-02-17", "2026-03-10", "2026-03-20", "2026-03-26",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-25", "2026-07-07",
    "2026-08-15", "2026-08-18", "2026-10-02", "2026-10-09", "2026-10-20",
    "2026-10-26",
}


# ── Weekly Expiry Calendar ───────────────────────────────────────────────────
# Thursday expiry until ~July 2024, then Tuesday from ~Aug 2024
# ── SEBI Lot Size History ─────────────────────────────────────────────────────
# NIFTY 50 F&O lot size as per SEBI/NSE circulars
# Sources: NSE FAOP70616, Zerodha, Groww, Angel One, ScanX
NIFTY_LOT_SIZE_HISTORY = [
    # (effective_from, lot_size)
    (date(2021, 4, 1),   25),  # Lot size was 25 at backtest start
    (date(2024, 11, 20), 75),  # SEBI min contract value ₹15L → tripled from 25
    (date(2026, 1, 6),   65),  # NSE periodic revision based on Sep 2025 avg prices
]

STRADDLE_MARGIN_PCT = 0.09   # ~9% of notional for short straddle (SPAN benefit)
MARGIN_BUFFER = 1.20          # 20% buffer (matches production MARGIN_BUFFER)


def get_lot_size_for_date(d: date) -> int:
    """Get SEBI-mandated NIFTY lot size for a given date."""
    lot_size = 25  # default for our backtest range (pre-Nov 2024)
    for effective_from, size in NIFTY_LOT_SIZE_HISTORY:
        if d >= effective_from:
            lot_size = size
    return lot_size


def calc_lots_for_capital(capital: float, spot: float, lot_size: int) -> int:
    """Calculate number of lots affordable with given capital.

    Uses approximate SPAN margin for short straddle:
    margin_per_lot = spot × lot_size × margin_pct × buffer
    """
    notional = spot * lot_size
    margin_per_lot = notional * STRADDLE_MARGIN_PCT * MARGIN_BUFFER
    lots = int(capital // margin_per_lot)
    return max(lots, 1)  # minimum 1 lot


EXPIRY_SHIFT_DATE = date(2024, 7, 1)  # Approximate date when expiry moved to Tuesday


def get_weekly_expiry(d: date) -> date:
    """Get the nearest weekly expiry for a given trading day."""
    if d >= EXPIRY_SHIFT_DATE:
        # Tuesday expiry
        expiry_weekday = 1  # Tuesday
    else:
        # Thursday expiry
        expiry_weekday = 3  # Thursday

    days_ahead = (expiry_weekday - d.weekday()) % 7
    candidate = d + timedelta(days=days_ahead)

    # If candidate is a holiday, move to previous trading day
    while candidate.strftime("%Y-%m-%d") in NSE_HOLIDAYS or candidate.weekday() >= 5:
        candidate -= timedelta(days=1)

    # If candidate is before today, move to next week
    if candidate < d:
        candidate = d + timedelta(days=(expiry_weekday - d.weekday()) % 7 + 7)
        while candidate.strftime("%Y-%m-%d") in NSE_HOLIDAYS or candidate.weekday() >= 5:
            candidate -= timedelta(days=1)

    return candidate


def compute_dte(today: date, expiry: date) -> int:
    """Count trading days from today to expiry (inclusive of today's position)."""
    trading_days = 0
    d = today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS:
            trading_days += 1
    return trading_days


# ── Config Loader ────────────────────────────────────────────────────────────

@dataclass
class Config:
    # Instrument
    lot_size: int = 65          # current lot size (overridden per-day by SEBI history)
    number_of_lots: int = 1     # overridden per-day by capital allocation
    strike_rounding: int = 50

    # Capital
    capital: float = 250000.0   # starting capital in Rs
    dynamic_lot_sizing: bool = True  # use SEBI lot size + capital-based allocation
    compound_capital: bool = False   # reinvest profits into capital for lot sizing
    max_lots: int = 50               # cap lot count (liquidity/margin realism)

    # Timing
    entry_time: time = field(default_factory=lambda: time(9, 17))
    exit_time: time = field(default_factory=lambda: time(15, 15))

    # Filters
    trade_dte: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    skip_months: list[int] = field(default_factory=lambda: [11])

    # Entry filters
    min_combined_premium: float = 0.0  # Skip entry if combined premium < this (0 = disabled)

    # Per-leg SL
    leg_sl_pct: float = 30.0
    leg_sl_dte_map: dict[int, float] = field(default_factory=dict)  # DTE-specific SL override

    # Combined SL (alternative to per-leg)
    combined_sl_enabled: bool = False   # Use combined premium SL instead of per-leg
    combined_sl_pct: float = 30.0       # Exit if combined premium rises by this %

    # Daily limits
    daily_target: float = 10000.0
    daily_loss_limit: float = -6000.0

    # Net P&L guard
    net_pnl_guard_max_defer_min: float = 15.0

    # Combined decay
    combined_decay_enabled: bool = True
    combined_decay_default: float = 60.0
    combined_decay_dte_map: dict[int, float] = field(
        default_factory=lambda: {0: 60.0, 1: 65.0, 2: 60.0, 3: 50.0, 4: 50.0}
    )

    # Winner booking
    winner_booking_enabled: bool = True
    winner_booking_decay_pct: float = 30.0

    # Asymmetric
    asymmetric_enabled: bool = True
    asymmetric_winner_decay_pct: float = 40.0
    asymmetric_loser_intact_pct: float = 80.0

    # Combined trail
    combined_trail_enabled: bool = True
    combined_trail_activate_pct: float = 30.0
    combined_trail_pct: float = 40.0

    # Breakeven
    breakeven_enabled: bool = True
    breakeven_grace_min: float = 5.0
    breakeven_buffer_pct: float = 5.0

    # Scaled entry (pyramiding)
    scaled_entry_enabled: bool = False
    scaled_entry_max_lots: int = 3       # Total lots including initial
    scaled_entry_interval_min: int = 15  # Minutes between each scaling step
    scaled_entry_require_profit: bool = True  # Only add lot if current P&L > 0

    # Re-entry
    reentry_enabled: bool = True
    reentry_cooldown_min: float = 45.0
    reentry_max_per_day: int = 2
    reentry_max_loss: float = 2000.0
    reentry_only_on_profit: bool = False  # Only re-enter if last trade was profitable

    # VIX spike
    vix_spike_enabled: bool = True
    vix_spike_threshold_pct: float = 15.0
    vix_spike_abs_floor: float = 18.0
    vix_spike_interval_sec: int = 300

    # Charges
    charges: ChargesConfig = field(default_factory=ChargesConfig)

    # Backtest
    slippage_points: float = 1.0

    @property
    def qty(self) -> int:
        return self.lot_size * self.number_of_lots

    @property
    def effective_target(self) -> float:
        return self.daily_target * self.number_of_lots

    @property
    def effective_loss_limit(self) -> float:
        return self.daily_loss_limit * self.number_of_lots


def load_config(path: str | Path) -> Config:
    """Load config from TOML file."""
    raw = toml.load(str(path))
    c = Config()

    inst = raw.get("instrument", {})
    c.lot_size = inst.get("lot_size", c.lot_size)
    c.number_of_lots = inst.get("number_of_lots", c.number_of_lots)
    c.strike_rounding = inst.get("strike_rounding", c.strike_rounding)
    c.capital = inst.get("capital", c.capital)
    c.dynamic_lot_sizing = inst.get("dynamic_lot_sizing", c.dynamic_lot_sizing)
    c.compound_capital = inst.get("compound_capital", c.compound_capital)
    c.max_lots = inst.get("max_lots", c.max_lots)

    timing = raw.get("timing", {})
    if "entry_time" in timing:
        h, m = timing["entry_time"].split(":")
        c.entry_time = time(int(h), int(m))
    if "exit_time" in timing:
        h, m = timing["exit_time"].split(":")
        c.exit_time = time(int(h), int(m))

    filt = raw.get("filters", {})
    c.trade_dte = filt.get("trade_dte", c.trade_dte)
    c.skip_months = filt.get("skip_months", c.skip_months)
    c.min_combined_premium = filt.get("min_combined_premium", c.min_combined_premium)

    risk = raw.get("risk", {})
    sl = risk.get("per_leg_sl", {})
    c.leg_sl_pct = sl.get("sl_percent", c.leg_sl_pct)
    if "sl_dte_map" in sl:
        c.leg_sl_dte_map = {int(k): float(v) for k, v in sl["sl_dte_map"].items()}

    csl = risk.get("combined_sl", {})
    c.combined_sl_enabled = csl.get("enabled", c.combined_sl_enabled)
    c.combined_sl_pct = csl.get("sl_percent", c.combined_sl_pct)

    dl = risk.get("daily_limits", {})
    c.daily_target = dl.get("profit_target", c.daily_target)
    c.daily_loss_limit = dl.get("loss_limit", c.daily_loss_limit)

    npg = risk.get("net_pnl_guard", {})
    c.net_pnl_guard_max_defer_min = npg.get("max_defer_min", c.net_pnl_guard_max_defer_min)

    cd = risk.get("combined_decay", {})
    c.combined_decay_enabled = cd.get("enabled", c.combined_decay_enabled)
    c.combined_decay_default = cd.get("default_pct", c.combined_decay_default)
    if "dte_map" in cd:
        c.combined_decay_dte_map = {int(k): float(v) for k, v in cd["dte_map"].items()}

    wb = risk.get("winner_booking", {})
    c.winner_booking_enabled = wb.get("enabled", c.winner_booking_enabled)
    c.winner_booking_decay_pct = wb.get("decay_pct", c.winner_booking_decay_pct)

    asym = risk.get("asymmetric", {})
    c.asymmetric_enabled = asym.get("enabled", c.asymmetric_enabled)
    c.asymmetric_winner_decay_pct = asym.get("winner_decay_pct", c.asymmetric_winner_decay_pct)
    c.asymmetric_loser_intact_pct = asym.get("loser_intact_pct", c.asymmetric_loser_intact_pct)

    ct = risk.get("combined_trail", {})
    c.combined_trail_enabled = ct.get("enabled", c.combined_trail_enabled)
    c.combined_trail_activate_pct = ct.get("activate_pct", c.combined_trail_activate_pct)
    c.combined_trail_pct = ct.get("trail_pct", c.combined_trail_pct)

    be = risk.get("breakeven", {})
    c.breakeven_enabled = be.get("enabled", c.breakeven_enabled)
    c.breakeven_grace_min = be.get("grace_min", c.breakeven_grace_min)
    c.breakeven_buffer_pct = be.get("buffer_pct", c.breakeven_buffer_pct)

    se = risk.get("scaled_entry", {})
    c.scaled_entry_enabled = se.get("enabled", c.scaled_entry_enabled)
    c.scaled_entry_max_lots = se.get("max_lots", c.scaled_entry_max_lots)
    c.scaled_entry_interval_min = se.get("interval_min", c.scaled_entry_interval_min)
    c.scaled_entry_require_profit = se.get("require_profit", c.scaled_entry_require_profit)

    re = risk.get("reentry", {})
    c.reentry_enabled = re.get("enabled", c.reentry_enabled)
    c.reentry_cooldown_min = re.get("cooldown_min", c.reentry_cooldown_min)
    c.reentry_max_per_day = re.get("max_per_day", c.reentry_max_per_day)
    c.reentry_max_loss = re.get("max_loss", c.reentry_max_loss)
    c.reentry_only_on_profit = re.get("only_on_profit", c.reentry_only_on_profit)

    vs = risk.get("vix_spike", {})
    c.vix_spike_enabled = vs.get("enabled", c.vix_spike_enabled)
    c.vix_spike_threshold_pct = vs.get("threshold_pct", c.vix_spike_threshold_pct)
    c.vix_spike_abs_floor = vs.get("abs_floor", c.vix_spike_abs_floor)
    c.vix_spike_interval_sec = vs.get("check_interval_sec", c.vix_spike_interval_sec)

    ch = raw.get("charges", {})
    c.charges = ChargesConfig(
        brokerage_per_order=ch.get("brokerage_per_order", 20.0),
        stt_sell_pct=ch.get("stt_sell_pct", 0.0625),
        exchange_txn_pct=ch.get("exchange_txn_pct", 0.053),
        sebi_pct=ch.get("sebi_pct", 0.0001),
        gst_pct=ch.get("gst_pct", 18.0),
        stamp_duty_buy_pct=ch.get("stamp_duty_buy_pct", 0.003),
    )

    bt = raw.get("backtest", {})
    c.slippage_points = bt.get("slippage_points", c.slippage_points)

    return c


# ── Trade State (mirrors production state dict) ─────────────────────────────

@dataclass
class LegState:
    active: bool = False
    symbol: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    breakeven_active: bool = False
    breakeven_sl: float = 0.0
    breakeven_activated_at: datetime | None = None
    net_pnl_defer_start: datetime | None = None


@dataclass
class TradeState:
    in_position: bool = False
    ce: LegState = field(default_factory=LegState)
    pe: LegState = field(default_factory=LegState)
    entry_time: datetime | None = None
    entry_date: date | None = None
    vix_at_entry: float = 0.0
    underlying_at_entry: float = 0.0
    current_dte: int | None = None
    closed_pnl: float = 0.0
    combined_trail_active: bool = False
    combined_decay_peak: float = 0.0
    is_reentry: bool = False
    exit_reason: str = ""
    sl_events: list[dict] = field(default_factory=list)
    # Dynamic lot sizing per trade
    lot_size: int = 65
    number_of_lots: int = 1
    qty: int = 65
    # Scaled entry tracking
    scaled_lots_added: int = 0        # How many lots added so far (including initial)
    next_scale_time: datetime | None = None  # When next lot can be added
    scale_entries: list[dict] = field(default_factory=list)  # Log of each scaling step


@dataclass
class DayState:
    cumulative_pnl: float = 0.0
    trade_count: int = 0
    reentry_count: int = 0
    last_close_time: datetime | None = None
    last_trade_pnl: float = 0.0  # Net P&L of the most recent trade (for re-entry check)
    target_hit: bool = False
    loss_limit_hit: bool = False


# ── Trade Record ─────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    date: date = None
    entry_time: str = ""
    exit_time: str = ""
    duration_min: float = 0.0
    entry_price_ce: float = 0.0
    entry_price_pe: float = 0.0
    exit_price_ce: float = 0.0
    exit_price_pe: float = 0.0
    combined_premium: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    dte: int = -1
    vix_at_entry: float = 0.0
    underlying_at_entry: float = 0.0
    is_reentry: bool = False
    trade_number: int = 0
    lot_size: int = 65
    number_of_lots: int = 1
    qty: int = 65
    capital_used: float = 250000.0
    sl_events: list[dict] = field(default_factory=list)
    charges_breakdown: dict = field(default_factory=dict)
    scale_entries: list[dict] = field(default_factory=list)  # Scaled entry log


# ── Backtest Engine ──────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(
        self,
        config: Config,
        spot_df: pd.DataFrame,
        ce_df: pd.DataFrame,
        pe_df: pd.DataFrame,
        vix_df: pd.DataFrame,
    ):
        self.cfg = config
        self.spot = self._index_by_timestamp(spot_df)
        self.ce = self._index_by_timestamp(ce_df)
        self.pe = self._index_by_timestamp(pe_df)
        self.vix = self._index_by_timestamp(vix_df)
        self.trades: list[TradeRecord] = []
        self.running_capital: float = config.capital  # tracks compounded capital

    @staticmethod
    def _index_by_timestamp(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df = df.set_index("timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        return df.sort_index()

    def run(self, start_date: str = "2021-04-01", end_date: str = "2026-03-28") -> pd.DataFrame:
        """Run the backtest and return trades DataFrame."""
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        trading_days = self._get_trading_days(start, end)
        print(f"\nBacktest: {start} to {end} ({len(trading_days)} trading days)")
        compound_tag = " + compounding" if self.cfg.compound_capital else ""
        lot_info = (f"Dynamic (capital=₹{self.cfg.capital:,.0f}{compound_tag})"
                    if self.cfg.dynamic_lot_sizing
                    else f"Fixed (lots={self.cfg.number_of_lots}, qty={self.cfg.qty})")
        print(f"Config: SL={self.cfg.leg_sl_pct}%, Target=+{self.cfg.daily_target}, "
              f"Loss={self.cfg.daily_loss_limit}, Lot sizing: {lot_info}")

        for i, day in enumerate(trading_days):
            if (i + 1) % 50 == 0:
                print(f"  Processing day {i+1}/{len(trading_days)}: {day}")
            self._process_day(day)

        return self._trades_to_dataframe()

    def _get_trading_days(self, start: date, end: date) -> list[date]:
        """Get all trading days in the range."""
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS:
                days.append(d)
            d += timedelta(days=1)
        return days

    def _get_candle(self, df: pd.DataFrame, dt: datetime) -> dict | None:
        """Get OHLCV candle at a specific timestamp."""
        if df.empty:
            return None
        # Find nearest candle within 1 minute
        try:
            loc = df.index.get_indexer([dt], method="nearest")[0]
            if loc < 0 or loc >= len(df):
                return None
            row = df.iloc[loc]
            # Check the found candle is within 2 minutes of requested time
            if abs((df.index[loc] - dt).total_seconds()) > 120:
                return None
            return {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        except Exception:
            return None

    def _get_vix_at(self, dt: datetime) -> float:
        """Get VIX value at a given time. Falls back to daily open."""
        candle = self._get_candle(self.vix, dt)
        if candle and candle["close"] > 0:
            return candle["close"]
        # Fallback: find any VIX data for this date
        day = dt.date()
        mask = self.vix.index.date == day
        day_vix = self.vix[mask]
        if not day_vix.empty:
            return float(day_vix.iloc[0]["open"])
        return 0.0

    def _get_spot_at(self, dt: datetime) -> float:
        candle = self._get_candle(self.spot, dt)
        return candle["open"] if candle else 0.0

    def _get_day_candles(self, df: pd.DataFrame, day: date,
                         from_time: time, to_time: time) -> pd.DataFrame:
        """Get all 1-min candles for a day within time range."""
        if df.empty:
            return pd.DataFrame()
        mask = df.index.date == day
        day_data = df[mask]
        if day_data.empty:
            return pd.DataFrame()
        # Filter by time
        from_dt = IST.localize(datetime.combine(day, from_time))
        to_dt = IST.localize(datetime.combine(day, to_time))
        return day_data[(day_data.index >= from_dt) & (day_data.index <= to_dt)]

    # ── Day Processing ───────────────────────────────────────────────────────

    def _process_day(self, day: date):
        """Process a single trading day — entry, monitoring, exits, re-entry."""
        # Skip month filter
        if day.month in self.cfg.skip_months:
            return

        # DTE filter
        expiry = get_weekly_expiry(day)
        dte = compute_dte(day, expiry)
        if dte not in self.cfg.trade_dte:
            return

        # Get entry time candles
        entry_dt = IST.localize(datetime.combine(day, self.cfg.entry_time))

        spot = self._get_spot_at(entry_dt)
        if spot <= 0:
            return  # No spot data for this day

        # Initialize day state
        day_state = DayState()

        # First entry
        trade = self._try_entry(day, dte, entry_dt, spot, day_state, is_reentry=False)
        if trade is None:
            return

        # Monitor and handle exits + re-entries
        self._monitor_day(day, trade, day_state, dte, entry_dt)

    def _try_entry(self, day: date, dte: int, entry_dt: datetime,
                   spot: float, day_state: DayState, is_reentry: bool) -> TradeState | None:
        """Attempt a straddle entry. Returns TradeState or None."""
        ce_candle = self._get_candle(self.ce, entry_dt)
        pe_candle = self._get_candle(self.pe, entry_dt)

        if not ce_candle or not pe_candle:
            return None

        # Slippage: SELL fills lower than theoretical (adverse for short)
        ce_entry = ce_candle["open"] - self.cfg.slippage_points
        pe_entry = pe_candle["open"] - self.cfg.slippage_points

        if ce_entry <= 0 or pe_entry <= 0:
            return None

        # Min combined premium filter
        if self.cfg.min_combined_premium > 0:
            if (ce_entry + pe_entry) < self.cfg.min_combined_premium:
                return None

        vix = self._get_vix_at(entry_dt)

        # Dynamic lot sizing: SEBI lot size for the date + capital-based allocation
        if self.cfg.dynamic_lot_sizing:
            lot_size = get_lot_size_for_date(day)
            cap = self.running_capital if self.cfg.compound_capital else self.cfg.capital
            num_lots = min(calc_lots_for_capital(cap, spot, lot_size), self.cfg.max_lots)
        else:
            lot_size = self.cfg.lot_size
            num_lots = self.cfg.number_of_lots

        # Scaled entry: start with 1 lot, add more during monitoring
        if self.cfg.scaled_entry_enabled and not is_reentry:
            target_lots = min(num_lots, self.cfg.scaled_entry_max_lots)
            initial_lots = 1
            qty = lot_size * initial_lots
        else:
            target_lots = num_lots
            initial_lots = num_lots
            qty = lot_size * num_lots

        trade = TradeState(
            in_position=True,
            ce=LegState(active=True, entry_price=ce_entry),
            pe=LegState(active=True, entry_price=pe_entry),
            entry_time=entry_dt,
            entry_date=day,
            vix_at_entry=vix,
            underlying_at_entry=spot,
            current_dte=dte,
            is_reentry=is_reentry,
            lot_size=lot_size,
            number_of_lots=initial_lots,
            qty=qty,
            scaled_lots_added=initial_lots,
        )
        # Store target lots for scaling and set next scale time
        trade._target_lots = target_lots
        if self.cfg.scaled_entry_enabled and initial_lots < target_lots:
            trade.next_scale_time = entry_dt + timedelta(minutes=self.cfg.scaled_entry_interval_min)
        trade.scale_entries.append({
            "lot": 1, "time": entry_dt.isoformat(),
            "ce_price": ce_entry, "pe_price": pe_entry, "lots": initial_lots,
        })

        day_state.trade_count += 1
        if is_reentry:
            day_state.reentry_count += 1

        return trade

    def _monitor_day(self, day: date, trade: TradeState, day_state: DayState,
                     dte: int, start_dt: datetime):
        """Monitor positions through the day, handling exits and re-entries."""
        exit_dt = IST.localize(datetime.combine(day, self.cfg.exit_time))

        # Get all candles for the day from entry to exit
        ce_candles = self._get_day_candles(self.ce, day, self.cfg.entry_time, self.cfg.exit_time)
        pe_candles = self._get_day_candles(self.pe, day, self.cfg.entry_time, self.cfg.exit_time)

        if ce_candles.empty and pe_candles.empty:
            return

        # Merge timestamps from both legs
        all_times = sorted(set(
            list(ce_candles.index) + list(pe_candles.index)
        ))

        last_vix_check = None

        for tick_time in all_times:
            if not trade.in_position:
                # Check re-entry eligibility
                if self._can_reenter(day_state, trade, tick_time, exit_dt):
                    spot = self._get_spot_at(tick_time)
                    if spot > 0:
                        new_trade = self._try_entry(day, dte, tick_time, spot,
                                                    day_state, is_reentry=True)
                        if new_trade is None:
                            continue  # No data for re-entry, keep scanning
                        trade = new_trade
                        last_vix_check = None
                        continue
                    else:
                        continue
                else:
                    continue

            # Get current candles
            ce_candle = self._get_candle_at_index(ce_candles, tick_time)
            pe_candle = self._get_candle_at_index(pe_candles, tick_time)

            # Scaled entry: try adding lots at intervals
            if (self.cfg.scaled_entry_enabled and trade.next_scale_time is not None
                    and tick_time >= trade.next_scale_time
                    and trade.ce.active and trade.pe.active
                    and ce_candle and pe_candle):
                self._try_scale_lot(trade, ce_candle, pe_candle, tick_time)

            # Run exit checks (mirrors Monitor._tick_inner)
            exit_reason = self._check_exits(
                trade, ce_candle, pe_candle, tick_time, day_state,
                last_vix_check, dte,
            )

            # Update VIX check time
            if (last_vix_check is None or
                    (tick_time - last_vix_check).total_seconds() >= self.cfg.vix_spike_interval_sec):
                last_vix_check = tick_time

            if not trade.in_position:
                # Trade was closed — record it
                self._record_trade(trade, day_state)
                continue

        # Time exit at 15:15 — close any remaining position
        if trade is not None and trade.in_position:
            self._close_all(trade, "Time Exit (15:15)", exit_dt, ce_candles, pe_candles)
            self._record_trade(trade, day_state)

    @staticmethod
    def _get_candle_at_index(candles: pd.DataFrame, dt: datetime) -> dict | None:
        if candles.empty:
            return None
        if dt in candles.index:
            row = candles.loc[dt]
            return {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        return None

    # ── Scaled Entry ──────────────────────────────────────────────────────────

    def _try_scale_lot(self, trade: TradeState, ce_candle: dict, pe_candle: dict,
                       tick_time: datetime):
        """Try to add another lot via scaled entry (pyramiding)."""
        target_lots = getattr(trade, '_target_lots', trade.number_of_lots)
        if trade.scaled_lots_added >= target_lots:
            trade.next_scale_time = None
            return

        # Check profit condition: current position must be profitable
        if self.cfg.scaled_entry_require_profit:
            ce_mtm = (trade.ce.entry_price - ce_candle["close"]) * trade.qty
            pe_mtm = (trade.pe.entry_price - pe_candle["close"]) * trade.qty
            open_pnl = ce_mtm + pe_mtm
            if open_pnl <= 0:
                # Not profitable — skip this interval, try again next interval
                trade.next_scale_time = tick_time + timedelta(
                    minutes=self.cfg.scaled_entry_interval_min)
                return

        # Add 1 lot: update weighted average entry price
        old_qty = trade.qty
        add_qty = trade.lot_size * 1  # Add 1 lot at a time
        new_qty = old_qty + add_qty

        # New lot enters at current candle open (with slippage)
        new_ce_entry = ce_candle["open"] - self.cfg.slippage_points
        new_pe_entry = pe_candle["open"] - self.cfg.slippage_points

        if new_ce_entry <= 0 or new_pe_entry <= 0:
            return

        # Weighted average entry price
        trade.ce.entry_price = round(
            (trade.ce.entry_price * old_qty + new_ce_entry * add_qty) / new_qty, 2)
        trade.pe.entry_price = round(
            (trade.pe.entry_price * old_qty + new_pe_entry * add_qty) / new_qty, 2)

        trade.qty = new_qty
        trade.number_of_lots += 1
        trade.scaled_lots_added += 1

        trade.scale_entries.append({
            "lot": trade.scaled_lots_added, "time": tick_time.isoformat(),
            "ce_price": new_ce_entry, "pe_price": new_pe_entry,
            "lots": trade.number_of_lots, "avg_ce": trade.ce.entry_price,
            "avg_pe": trade.pe.entry_price,
        })

        # Schedule next scaling or stop
        if trade.scaled_lots_added >= target_lots:
            trade.next_scale_time = None
        else:
            trade.next_scale_time = tick_time + timedelta(
                minutes=self.cfg.scaled_entry_interval_min)

    # ── Exit Check Hierarchy (mirrors Monitor._tick_inner) ───────────────────

    def _check_exits(
        self,
        trade: TradeState,
        ce_candle: dict | None,
        pe_candle: dict | None,
        tick_time: datetime,
        day_state: DayState,
        last_vix_check: datetime | None,
        dte: int,
    ) -> str | None:
        """Run all exit checks in production priority order."""

        # --- Priority 1: Per-Leg SL Check (skip if combined SL is active with both legs) ---
        skip_per_leg_sl = self.cfg.combined_sl_enabled and trade.ce.active and trade.pe.active
        for leg_name, leg, candle in [("CE", trade.ce, ce_candle), ("PE", trade.pe, pe_candle)]:
            if not leg.active or candle is None:
                continue
            if skip_per_leg_sl:  # Combined SL handles both legs; per-leg SL still runs for single survivor
                continue

            ltp = candle["high"]  # Worst case for sold option
            if ltp <= 0:
                continue

            sl = self._sl_level(leg, trade, tick_time)
            if sl <= 0:
                continue

            if ltp >= sl:
                # Net P&L guard: defer SL if net position profitable
                if trade.closed_pnl != 0 and self._active_leg_count(trade) == 1:
                    open_mtm = (leg.entry_price - candle["close"]) * trade.qty
                    net = trade.closed_pnl + open_mtm
                    if net > 0:
                        if leg.net_pnl_defer_start is None:
                            leg.net_pnl_defer_start = tick_time
                            continue
                        elapsed = (tick_time - leg.net_pnl_defer_start).total_seconds() / 60
                        if elapsed < self.cfg.net_pnl_guard_max_defer_min:
                            continue

                sl_type = "Breakeven SL" if leg.breakeven_active else "Fixed SL"
                reason = f"{sl_type} hit ({leg_name})"
                # SL fill: use max(close, sl) — when SL triggers on candle high,
                # a market order fills at or above SL level, not at candle close
                sl_exit_price = max(candle["close"], sl)
                self._close_leg(trade, leg_name, reason, sl_exit_price, tick_time)

                if not trade.in_position:
                    return reason

                # Activate breakeven on survivor
                other_name = "PE" if leg_name == "CE" else "CE"
                other_leg = trade.pe if leg_name == "CE" else trade.ce
                other_candle = pe_candle if leg_name == "CE" else ce_candle
                if other_leg.active:
                    self._activate_breakeven(trade, other_leg, other_name, other_candle, tick_time)

        if self._active_leg_count(trade) == 0:
            return trade.exit_reason

        # --- Combined SL Check (alternative to per-leg SL) ---
        if self.cfg.combined_sl_enabled and trade.ce.active and trade.pe.active and ce_candle and pe_candle:
            combined_entry = trade.ce.entry_price + trade.pe.entry_price
            combined_current = ce_candle["high"] + pe_candle["high"]  # Worst case
            combined_rise_pct = ((combined_current - combined_entry) / combined_entry) * 100
            if combined_rise_pct >= self.cfg.combined_sl_pct:
                reason = f"Combined SL ({combined_rise_pct:.1f}%)"
                self._close_all_with_candles(trade, reason, tick_time, ce_candle, pe_candle)
                return reason

        # --- Priority 2: Combined Checks (both legs active) ---
        if trade.ce.active and trade.pe.active and ce_candle and pe_candle:
            ce_ltp = ce_candle["close"]
            pe_ltp = pe_candle["close"]
            ce_entry = trade.ce.entry_price
            pe_entry = trade.pe.entry_price

            if ce_ltp > 0 and pe_ltp > 0 and ce_entry > 0 and pe_entry > 0:
                combined_entry = ce_entry + pe_entry
                combined_current = ce_ltp + pe_ltp
                decay_pct = (1 - combined_current / combined_entry) * 100

                # 2a. Combined decay exit
                if self.cfg.combined_decay_enabled:
                    target = self.cfg.combined_decay_dte_map.get(
                        dte, self.cfg.combined_decay_default
                    )
                    if decay_pct >= target:
                        reason = f"Combined Decay Exit ({decay_pct:.1f}%)"
                        self._close_all_with_candles(trade, reason, tick_time,
                                                     ce_candle, pe_candle)
                        return reason

                # 2b. Asymmetric leg booking
                if self.cfg.asymmetric_enabled:
                    ce_pct = (ce_ltp / ce_entry) * 100
                    pe_pct = (pe_ltp / pe_entry) * 100

                    if (ce_pct <= self.cfg.asymmetric_winner_decay_pct and
                            pe_pct >= self.cfg.asymmetric_loser_intact_pct):
                        reason = f"Asymmetric Booking (CE={ce_pct:.1f}%)"
                        self._close_leg(trade, "CE", reason, ce_candle["close"], tick_time)
                        if not trade.in_position:
                            return reason
                        self._activate_breakeven(trade, trade.pe, "PE", pe_candle, tick_time)

                    elif (pe_pct <= self.cfg.asymmetric_winner_decay_pct and
                          ce_pct >= self.cfg.asymmetric_loser_intact_pct):
                        reason = f"Asymmetric Booking (PE={pe_pct:.1f}%)"
                        self._close_leg(trade, "PE", reason, pe_candle["close"], tick_time)
                        if not trade.in_position:
                            return reason
                        self._activate_breakeven(trade, trade.ce, "CE", ce_candle, tick_time)

                # 2c. Combined profit trailing
                if self.cfg.combined_trail_enabled:
                    if decay_pct >= self.cfg.combined_trail_activate_pct:
                        if not trade.combined_trail_active:
                            trade.combined_trail_active = True
                            trade.combined_decay_peak = decay_pct
                        else:
                            if decay_pct > trade.combined_decay_peak:
                                trade.combined_decay_peak = decay_pct
                            retracement = trade.combined_decay_peak - decay_pct
                            if retracement >= self.cfg.combined_trail_pct:
                                reason = f"Combined Trail Exit (retrace {retracement:.1f}%)"
                                self._close_all_with_candles(trade, reason, tick_time,
                                                             ce_candle, pe_candle)
                                return reason

        # --- Priority 3: Winner Booking (single survivor) ---
        active_legs = self._get_active_legs(trade)
        if len(active_legs) == 1 and self.cfg.winner_booking_enabled:
            leg_name, leg = active_legs[0]
            candle = ce_candle if leg_name == "CE" else pe_candle
            if leg.entry_price > 0 and candle:
                ltp = candle["close"]
                if ltp > 0:
                    decay = (ltp / leg.entry_price) * 100
                    if decay <= self.cfg.winner_booking_decay_pct:
                        reason = f"Winner Booking ({decay:.1f}%)"
                        self._close_leg(trade, leg_name, reason, candle["close"], tick_time)
                        return reason

        # --- Priority 4: P&L Calculation ---
        open_mtm = 0.0
        for leg_name, leg in self._get_active_legs(trade):
            candle = ce_candle if leg_name == "CE" else pe_candle
            if leg.entry_price > 0 and candle and candle["close"] > 0:
                open_mtm += (leg.entry_price - candle["close"]) * trade.qty
        combined_pnl = trade.closed_pnl + open_mtm

        # --- Priority 5: VIX Spike ---
        if (self.cfg.vix_spike_enabled and trade.vix_at_entry > 0 and
                (last_vix_check is None or
                 (tick_time - last_vix_check).total_seconds() >= self.cfg.vix_spike_interval_sec)):
            current_vix = self._get_vix_at(tick_time)
            if current_vix > 0:
                spike_pct = ((current_vix - trade.vix_at_entry) / trade.vix_at_entry) * 100
                if spike_pct >= self.cfg.vix_spike_threshold_pct and current_vix >= self.cfg.vix_spike_abs_floor:
                    reason = f"VIX Spike Exit ({spike_pct:.1f}%)"
                    self._close_all_with_candles(trade, reason, tick_time, ce_candle, pe_candle)
                    return reason

        # --- Priority 6: Daily P&L Limits ---
        # Production checks per-trade P&L only (not cumulative daily)
        effective_target = self.cfg.daily_target * trade.number_of_lots
        effective_loss = self.cfg.daily_loss_limit * trade.number_of_lots
        if effective_target > 0 and combined_pnl >= effective_target:
            reason = f"Daily Target ({combined_pnl:,.0f})"
            self._close_all_with_candles(trade, reason, tick_time, ce_candle, pe_candle)
            day_state.target_hit = True
            return reason

        if effective_loss < 0 and combined_pnl <= effective_loss:
            reason = f"Daily Loss Limit ({combined_pnl:,.0f})"
            self._close_all_with_candles(trade, reason, tick_time, ce_candle, pe_candle)
            day_state.loss_limit_hit = True
            return reason

        return None

    def _sl_level(self, leg: LegState, trade: TradeState, tick_time: datetime = None) -> float:
        """Calculate SL level for a leg — mirrors production sl_level()."""
        entry = leg.entry_price
        if entry <= 0:
            return 0.0

        # DTE-specific SL override
        sl_pct = self.cfg.leg_sl_dte_map.get(trade.current_dte, self.cfg.leg_sl_pct) if self.cfg.leg_sl_dte_map else self.cfg.leg_sl_pct
        fixed_sl = round(entry * (1.0 + sl_pct / 100.0), 2)

        if self.cfg.breakeven_enabled and leg.breakeven_active:
            be_sl = leg.breakeven_sl
            if be_sl > 0 and be_sl < fixed_sl:
                if not self.cfg.breakeven_grace_min:
                    return be_sl
                if leg.breakeven_activated_at is None:
                    return be_sl
                if tick_time is not None:
                    elapsed = (tick_time - leg.breakeven_activated_at).total_seconds() / 60
                    if elapsed >= self.cfg.breakeven_grace_min:
                        return be_sl
                    return fixed_sl  # Grace period not elapsed, use fixed SL
                return be_sl
        return fixed_sl

    def _activate_breakeven(self, trade: TradeState, survivor: LegState,
                            survivor_name: str, survivor_candle: dict | None,
                            tick_time: datetime):
        """Activate breakeven SL on survivor — mirrors production logic."""
        if not self.cfg.breakeven_enabled:
            return
        if trade.closed_pnl >= 0:
            return
        if survivor.entry_price <= 0:
            return

        # Check if survivor is winning (skip breakeven if so)
        if survivor_candle and survivor_candle["close"] > 0:
            if survivor_candle["close"] < survivor.entry_price:
                return  # Survivor is winning — skip

        raw_be = survivor.entry_price + (trade.closed_pnl / trade.qty)
        be_price = raw_be * (1 + self.cfg.breakeven_buffer_pct / 100)

        if be_price <= 0 or be_price >= survivor.entry_price:
            return

        survivor.breakeven_active = True
        survivor.breakeven_sl = round(be_price, 2)
        survivor.breakeven_activated_at = tick_time

    def _active_leg_count(self, trade: TradeState) -> int:
        return sum(1 for l in [trade.ce, trade.pe] if l.active)

    def _get_active_legs(self, trade: TradeState) -> list[tuple[str, LegState]]:
        legs = []
        if trade.ce.active:
            legs.append(("CE", trade.ce))
        if trade.pe.active:
            legs.append(("PE", trade.pe))
        return legs

    # ── Close Operations ─────────────────────────────────────────────────────

    def _close_leg(self, trade: TradeState, leg_name: str, reason: str,
                   exit_ltp: float, tick_time: datetime):
        """Close a single leg."""
        leg = trade.ce if leg_name == "CE" else trade.pe
        if not leg.active:
            return

        # Slippage: BUY fills higher than theoretical (adverse for short)
        exit_price = exit_ltp + self.cfg.slippage_points
        leg_pnl = (leg.entry_price - exit_price) * trade.qty

        leg.active = False
        leg.exit_price = exit_price
        trade.closed_pnl += leg_pnl
        trade.sl_events.append({
            "leg": leg_name, "reason": reason,
            "entry": leg.entry_price, "exit": exit_price,
            "pnl": round(leg_pnl, 2), "time": tick_time.isoformat(),
        })

        # Check if fully flat
        if not trade.ce.active and not trade.pe.active:
            trade.in_position = False
            trade.exit_reason = reason

    def _close_all_with_candles(self, trade: TradeState, reason: str,
                                tick_time: datetime,
                                ce_candle: dict | None, pe_candle: dict | None):
        """Close all active legs using current candle prices."""
        trade.exit_reason = reason
        if trade.ce.active and ce_candle:
            self._close_leg(trade, "CE", reason, ce_candle["close"], tick_time)
        if trade.pe.active and pe_candle:
            self._close_leg(trade, "PE", reason, pe_candle["close"], tick_time)
        trade.in_position = False

    def _close_all(self, trade: TradeState, reason: str, tick_time: datetime,
                   ce_candles: pd.DataFrame, pe_candles: pd.DataFrame):
        """Close all legs using last available candle."""
        trade.exit_reason = reason
        for leg_name, leg, candles in [("CE", trade.ce, ce_candles),
                                        ("PE", trade.pe, pe_candles)]:
            if leg.active and not candles.empty:
                last_candle = candles.iloc[-1]
                self._close_leg(trade, leg_name, reason,
                                float(last_candle["close"]), tick_time)
        trade.in_position = False

    # ── Re-Entry Logic ───────────────────────────────────────────────────────

    def _can_reenter(self, day_state: DayState, last_trade: TradeState,
                     tick_time: datetime, exit_dt: datetime) -> bool:
        """Check if re-entry is allowed — mirrors production reentry_ok()."""
        if not self.cfg.reentry_enabled:
            return False

        # Production checks cumulative daily P&L against daily limits
        effective_target = self.cfg.daily_target * max(self.cfg.number_of_lots, 1)
        if effective_target > 0 and day_state.cumulative_pnl >= effective_target:
            return False
        effective_limit = self.cfg.daily_loss_limit * max(self.cfg.number_of_lots, 1)
        if effective_limit < 0 and day_state.cumulative_pnl <= effective_limit:
            return False

        if day_state.reentry_count >= self.cfg.reentry_max_per_day:
            return False
        if day_state.last_close_time is None:
            return False

        # Cooldown check
        elapsed = (tick_time - day_state.last_close_time).total_seconds() / 60
        if elapsed < self.cfg.reentry_cooldown_min:
            return False

        # Only re-enter after profitable trade (optional)
        if self.cfg.reentry_only_on_profit and day_state.last_trade_pnl <= 0:
            return False

        # Max loss check — production checks LAST trade P&L
        effective_max_loss = self.cfg.reentry_max_loss * max(self.cfg.number_of_lots, 1)
        if day_state.last_trade_pnl < -effective_max_loss:
            return False

        return True

    # ── Trade Recording ──────────────────────────────────────────────────────

    def _record_trade(self, trade: TradeState, day_state: DayState):
        """Record a completed trade and update day state."""
        gross_pnl = trade.closed_pnl

        # Calculate charges
        charges_detail = calc_trade_charges(
            entry_ce=trade.ce.entry_price,
            entry_pe=trade.pe.entry_price,
            exit_ce=trade.ce.exit_price,
            exit_pe=trade.pe.exit_price,
            qty=trade.qty,
            cfg=self.cfg.charges,
            ce_exited=trade.ce.exit_price > 0,
            pe_exited=trade.pe.exit_price > 0,
        )
        charges_total = charges_detail["total"]

        # Scaled entry: extra brokerage for additional entry orders
        # Each scaling step adds 2 orders (CE + PE sell)
        extra_scale_orders = max(0, trade.scaled_lots_added - 1) * 2 if self.cfg.scaled_entry_enabled else 0
        if extra_scale_orders > 0:
            extra_brokerage = extra_scale_orders * self.cfg.charges.brokerage_per_order
            # GST on extra brokerage
            extra_gst = extra_brokerage * self.cfg.charges.gst_pct / 100
            charges_total += extra_brokerage + extra_gst
            charges_detail["extra_scale_brokerage"] = round(extra_brokerage + extra_gst, 2)
            charges_detail["total"] = round(charges_total, 2)

        net_pnl = gross_pnl - charges_total

        exit_time = trade.sl_events[-1]["time"] if trade.sl_events else ""
        duration = 0.0
        if trade.entry_time and exit_time:
            try:
                exit_dt = datetime.fromisoformat(exit_time)
                duration = (exit_dt - trade.entry_time).total_seconds() / 60
            except (ValueError, TypeError):
                pass

        record = TradeRecord(
            date=trade.entry_date,
            entry_time=trade.entry_time.isoformat() if trade.entry_time else "",
            exit_time=exit_time,
            duration_min=round(duration, 1),
            entry_price_ce=trade.ce.entry_price,
            entry_price_pe=trade.pe.entry_price,
            exit_price_ce=trade.ce.exit_price,
            exit_price_pe=trade.pe.exit_price,
            combined_premium=trade.ce.entry_price + trade.pe.entry_price,
            gross_pnl=round(gross_pnl, 2),
            charges=round(charges_total, 2),
            net_pnl=round(net_pnl, 2),
            exit_reason=trade.exit_reason,
            dte=trade.current_dte if trade.current_dte is not None else -1,
            vix_at_entry=trade.vix_at_entry,
            underlying_at_entry=trade.underlying_at_entry,
            is_reentry=trade.is_reentry,
            trade_number=day_state.trade_count,
            lot_size=trade.lot_size,
            number_of_lots=trade.number_of_lots,
            qty=trade.qty,
            capital_used=round(self.running_capital, 2),
            sl_events=trade.sl_events,
            charges_breakdown=charges_detail,
            scale_entries=trade.scale_entries,
        )
        self.trades.append(record)

        # Update day state and running capital
        # Production uses gross P&L (closed_pnl) for daily tracking, not net
        day_state.cumulative_pnl += gross_pnl
        day_state.last_trade_pnl = gross_pnl
        day_state.last_close_time = datetime.fromisoformat(exit_time) if exit_time else None
        if self.cfg.compound_capital:
            self.running_capital += net_pnl

    def _trades_to_dataframe(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()

        records = []
        for t in self.trades:
            records.append({
                "date": t.date,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "duration_min": t.duration_min,
                "entry_price_ce": t.entry_price_ce,
                "entry_price_pe": t.entry_price_pe,
                "exit_price_ce": t.exit_price_ce,
                "exit_price_pe": t.exit_price_pe,
                "combined_premium": t.combined_premium,
                "gross_pnl": t.gross_pnl,
                "charges": t.charges,
                "net_pnl": t.net_pnl,
                "exit_reason": t.exit_reason,
                "dte": t.dte,
                "vix_at_entry": t.vix_at_entry,
                "underlying_at_entry": t.underlying_at_entry,
                "is_reentry": t.is_reentry,
                "trade_number": t.trade_number,
                "lot_size": t.lot_size,
                "number_of_lots": t.number_of_lots,
                "qty": t.qty,
                "capital_used": t.capital_used,
                "num_sl_events": len(t.sl_events),
                "scaled_lots": len(t.scale_entries),
            })

        return pd.DataFrame(records)
