"""
Nifty Bull Put Spread — Backtest Engine
Replays 1-min candles day-by-day.

Strategy:
  SELL PE at ATM - sell_offset (e.g., ATM-100)
  BUY  PE at ATM - buy_offset  (e.g., ATM-300)
  Max profit = net credit received
  Max loss   = spread_width - credit (capped, defined risk)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import toml

IST = pytz.timezone("Asia/Kolkata")

from charges import ChargesConfig, calc_order_charges

# ── NSE Holidays (2021-2026) ────────────────────────────────────────────────
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

# ── Weekly Expiry Calendar ──────────────────────────────────────────────────
EXPIRY_SHIFT_DATE = date(2024, 7, 1)

# ── SEBI Lot Size History ───────────────────────────────────────────────────
NIFTY_LOT_SIZE_HISTORY = [
    (date(2021, 4, 1),   25),
    (date(2024, 11, 20), 75),
    (date(2026, 1, 6),   65),
]

# Credit spread margin is lower than naked selling
# Approximate: spread_width × lot_size × lots (max loss = margin required)
# Plus buffer for exchange margin requirements
SPREAD_MARGIN_BUFFER = 1.30  # 30% buffer over max loss


def get_lot_size_for_date(d: date) -> int:
    lot_size = 25
    for effective_from, size in NIFTY_LOT_SIZE_HISTORY:
        if d >= effective_from:
            lot_size = size
    return lot_size


def calc_lots_for_capital(capital: float, spread_width_pts: float, lot_size: int) -> int:
    """Calculate lots affordable based on max loss per lot.

    For credit spreads, margin ~ max_loss = spread_width × lot_size.
    """
    max_loss_per_lot = spread_width_pts * lot_size
    margin_per_lot = max_loss_per_lot * SPREAD_MARGIN_BUFFER
    lots = int(capital // margin_per_lot)
    return max(lots, 1)


def get_weekly_expiry(d: date) -> date:
    if d >= EXPIRY_SHIFT_DATE:
        expiry_weekday = 1  # Tuesday
    else:
        expiry_weekday = 3  # Thursday

    days_ahead = (expiry_weekday - d.weekday()) % 7
    candidate = d + timedelta(days=days_ahead)

    while candidate.strftime("%Y-%m-%d") in NSE_HOLIDAYS or candidate.weekday() >= 5:
        candidate -= timedelta(days=1)

    if candidate < d:
        candidate = d + timedelta(days=(expiry_weekday - d.weekday()) % 7 + 7)
        while candidate.strftime("%Y-%m-%d") in NSE_HOLIDAYS or candidate.weekday() >= 5:
            candidate -= timedelta(days=1)

    return candidate


def compute_dte(today: date, expiry: date) -> int:
    trading_days = 0
    d = today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS:
            trading_days += 1
    return trading_days


# ── Config ──────────────────────────────────────────────────────────────────

@dataclass
class Config:
    lot_size: int = 65
    number_of_lots: int = 1
    strike_rounding: int = 50

    capital: float = 250000.0
    dynamic_lot_sizing: bool = True
    compound_capital: bool = False
    max_lots: int = 50

    # Spread parameters
    sell_offset: int = 100   # ATM - sell_offset = sell strike
    buy_offset: int = 300    # ATM - buy_offset = buy strike
    # spread_width = buy_offset - sell_offset (auto-calculated)

    entry_time: time = field(default_factory=lambda: time(9, 17))
    exit_time: time = field(default_factory=lambda: time(15, 15))

    trade_dte: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    skip_months: list[int] = field(default_factory=list)

    # Risk: SL on spread
    spread_sl_pct: float = 70.0  # Exit if loss reaches X% of max possible loss

    # Daily limits
    daily_target: float = 5000.0
    daily_loss_limit: float = -5000.0

    charges: ChargesConfig = field(default_factory=ChargesConfig)
    slippage_points: float = 1.0

    @property
    def spread_width(self) -> int:
        return self.buy_offset - self.sell_offset

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

    spread = raw.get("spread", {})
    c.sell_offset = spread.get("sell_offset", c.sell_offset)
    c.buy_offset = spread.get("buy_offset", c.buy_offset)

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

    risk = raw.get("risk", {})
    c.spread_sl_pct = risk.get("spread_sl_pct", c.spread_sl_pct)

    dl = risk.get("daily_limits", {})
    c.daily_target = dl.get("profit_target", c.daily_target)
    c.daily_loss_limit = dl.get("loss_limit", c.daily_loss_limit)

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


# ── Trade State ─────────────────────────────────────────────────────────────

@dataclass
class TradeState:
    in_position: bool = False
    # Sell leg (short PE, closer to ATM)
    sell_entry: float = 0.0
    sell_exit: float = 0.0
    # Buy leg (long PE, further OTM — protection)
    buy_entry: float = 0.0
    buy_exit: float = 0.0
    # Credit received at entry
    net_credit: float = 0.0
    # Max possible loss = spread_width_pts × qty - net_credit_total
    max_loss: float = 0.0
    # Metadata
    entry_time: datetime | None = None
    entry_date: date | None = None
    underlying_at_entry: float = 0.0
    vix_at_entry: float = 0.0
    current_dte: int | None = None
    exit_reason: str = ""
    lot_size: int = 65
    number_of_lots: int = 1
    qty: int = 65


@dataclass
class DayState:
    cumulative_pnl: float = 0.0
    trade_count: int = 0
    target_hit: bool = False
    loss_limit_hit: bool = False


@dataclass
class TradeRecord:
    date: date = None
    entry_time: str = ""
    exit_time: str = ""
    duration_min: float = 0.0
    sell_entry: float = 0.0
    sell_exit: float = 0.0
    buy_entry: float = 0.0
    buy_exit: float = 0.0
    net_credit: float = 0.0
    spread_width_pts: int = 0
    gross_pnl: float = 0.0
    charges: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    dte: int = -1
    vix_at_entry: float = 0.0
    underlying_at_entry: float = 0.0
    trade_number: int = 0
    lot_size: int = 65
    number_of_lots: int = 1
    qty: int = 65
    capital_used: float = 250000.0
    charges_breakdown: dict = field(default_factory=dict)


# ── Backtest Engine ─────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(
        self,
        config: Config,
        spot_df: pd.DataFrame,
        sell_pe_df: pd.DataFrame,
        buy_pe_df: pd.DataFrame,
        vix_df: pd.DataFrame,
    ):
        self.cfg = config
        self.spot = self._index_by_timestamp(spot_df)
        self.sell_pe = self._index_by_timestamp(sell_pe_df)
        self.buy_pe = self._index_by_timestamp(buy_pe_df)
        self.vix = self._index_by_timestamp(vix_df)
        self.trades: list[TradeRecord] = []
        self.running_capital: float = config.capital

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
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        trading_days = self._get_trading_days(start, end)
        print(f"\nBacktest: {start} to {end} ({len(trading_days)} trading days)")
        compound_tag = " + compounding" if self.cfg.compound_capital else ""
        lot_info = (f"Dynamic (capital=₹{self.cfg.capital:,.0f}{compound_tag})"
                    if self.cfg.dynamic_lot_sizing
                    else f"Fixed (lots={self.cfg.number_of_lots}, qty={self.cfg.qty})")
        print(f"Config: Spread={self.cfg.sell_offset}/{self.cfg.buy_offset} "
              f"(width={self.cfg.spread_width}pts), SL={self.cfg.spread_sl_pct}% of max loss, "
              f"Lot sizing: {lot_info}")

        for i, day in enumerate(trading_days):
            if (i + 1) % 50 == 0:
                print(f"  Processing day {i+1}/{len(trading_days)}: {day}")
            self._process_day(day)

        return self._trades_to_dataframe()

    def _get_trading_days(self, start: date, end: date) -> list[date]:
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS:
                days.append(d)
            d += timedelta(days=1)
        return days

    def _get_candle(self, df: pd.DataFrame, dt: datetime) -> dict | None:
        if df.empty:
            return None
        try:
            loc = df.index.get_indexer([dt], method="nearest")[0]
            if loc < 0 or loc >= len(df):
                return None
            row = df.iloc[loc]
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
        candle = self._get_candle(self.vix, dt)
        if candle and candle["close"] > 0:
            return candle["close"]
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
        if df.empty:
            return pd.DataFrame()
        mask = df.index.date == day
        day_data = df[mask]
        if day_data.empty:
            return pd.DataFrame()
        from_dt = IST.localize(datetime.combine(day, from_time))
        to_dt = IST.localize(datetime.combine(day, to_time))
        return day_data[(day_data.index >= from_dt) & (day_data.index <= to_dt)]

    # ── Day Processing ──────────────────────────────────────────────────────

    def _process_day(self, day: date):
        if day.month in self.cfg.skip_months:
            return

        expiry = get_weekly_expiry(day)
        dte = compute_dte(day, expiry)
        if dte not in self.cfg.trade_dte:
            return

        entry_dt = IST.localize(datetime.combine(day, self.cfg.entry_time))
        spot = self._get_spot_at(entry_dt)
        if spot <= 0:
            return

        day_state = DayState()
        trade = self._try_entry(day, dte, entry_dt, spot)
        if trade is None:
            return

        self._monitor_day(day, trade, day_state, dte)

    def _try_entry(self, day: date, dte: int, entry_dt: datetime,
                   spot: float) -> TradeState | None:
        sell_candle = self._get_candle(self.sell_pe, entry_dt)
        buy_candle = self._get_candle(self.buy_pe, entry_dt)

        if not sell_candle or not buy_candle:
            return None

        # Sell PE: slippage adverse (fill lower)
        sell_entry = sell_candle["open"] - self.cfg.slippage_points
        # Buy PE: slippage adverse (fill higher)
        buy_entry = buy_candle["open"] + self.cfg.slippage_points

        if sell_entry <= 0 or buy_entry <= 0:
            return None

        # Net credit = what we receive (sell) - what we pay (buy)
        net_credit_per_unit = sell_entry - buy_entry
        if net_credit_per_unit <= 0:
            return None  # No credit = no trade (inverted spread)

        vix = self._get_vix_at(entry_dt)

        # Dynamic lot sizing
        if self.cfg.dynamic_lot_sizing:
            lot_size = get_lot_size_for_date(day)
            cap = self.running_capital if self.cfg.compound_capital else self.cfg.capital
            num_lots = min(
                calc_lots_for_capital(cap, self.cfg.spread_width, lot_size),
                self.cfg.max_lots,
            )
        else:
            lot_size = self.cfg.lot_size
            num_lots = self.cfg.number_of_lots

        qty = lot_size * num_lots
        net_credit_total = net_credit_per_unit * qty
        max_loss_total = (self.cfg.spread_width - net_credit_per_unit) * qty

        trade = TradeState(
            in_position=True,
            sell_entry=sell_entry,
            buy_entry=buy_entry,
            net_credit=net_credit_per_unit,
            max_loss=max_loss_total,
            entry_time=entry_dt,
            entry_date=day,
            underlying_at_entry=spot,
            vix_at_entry=vix,
            current_dte=dte,
            lot_size=lot_size,
            number_of_lots=num_lots,
            qty=qty,
        )
        return trade

    def _monitor_day(self, day: date, trade: TradeState, day_state: DayState, dte: int):
        """Monitor spread through the day on 1-min candles."""
        exit_dt = IST.localize(datetime.combine(day, self.cfg.exit_time))

        # Get 1-min candles for both legs
        sell_candles = self._get_day_candles(self.sell_pe, day, self.cfg.entry_time, self.cfg.exit_time)
        buy_candles = self._get_day_candles(self.buy_pe, day, self.cfg.entry_time, self.cfg.exit_time)

        if sell_candles.empty or buy_candles.empty:
            # No intraday data — use entry prices as exit (flat)
            self._record_trade(trade, trade.entry_time, "no_data")
            return

        # Walk through each minute
        for ts in sell_candles.index:
            if ts <= trade.entry_time:
                continue

            sell_candle = self._safe_row(sell_candles, ts)
            buy_candle = self._safe_row(buy_candles, ts)
            if sell_candle is None or buy_candle is None:
                continue

            sell_ltp = sell_candle["close"]
            buy_ltp = buy_candle["close"]

            # Current spread P&L per unit:
            # Sell leg P&L = sell_entry - sell_ltp (profit if sell_ltp drops)
            # Buy leg P&L = buy_ltp - buy_entry (profit if buy_ltp rises)
            # Net P&L = (sell_entry - sell_ltp) + (buy_ltp - buy_entry)
            #         = net_credit - (sell_ltp - buy_ltp)
            spread_pnl_per_unit = (trade.sell_entry - sell_ltp) + (buy_ltp - trade.buy_entry)
            spread_pnl_total = spread_pnl_per_unit * trade.qty

            # 1. Spread SL check: exit if loss exceeds threshold
            if trade.max_loss > 0:
                loss_pct = max(0, -spread_pnl_total) / trade.max_loss * 100
                if loss_pct >= self.cfg.spread_sl_pct:
                    trade.sell_exit = sell_ltp + self.cfg.slippage_points  # Buy back sell leg (adverse)
                    trade.buy_exit = buy_ltp - self.cfg.slippage_points   # Sell buy leg (adverse)
                    self._record_trade(trade, ts, "spread_sl")
                    day_state.cumulative_pnl += self.trades[-1].net_pnl
                    return

            # 2. Daily limits check
            current_day_pnl = day_state.cumulative_pnl + spread_pnl_total
            if current_day_pnl >= self.cfg.effective_target:
                trade.sell_exit = sell_ltp + self.cfg.slippage_points
                trade.buy_exit = buy_ltp - self.cfg.slippage_points
                self._record_trade(trade, ts, "daily_target")
                day_state.cumulative_pnl += self.trades[-1].net_pnl
                return
            if current_day_pnl <= self.cfg.effective_loss_limit:
                trade.sell_exit = sell_ltp + self.cfg.slippage_points
                trade.buy_exit = buy_ltp - self.cfg.slippage_points
                self._record_trade(trade, ts, "daily_loss_limit")
                day_state.cumulative_pnl += self.trades[-1].net_pnl
                return

            # 3. Time exit
            if ts >= exit_dt:
                trade.sell_exit = sell_ltp + self.cfg.slippage_points
                trade.buy_exit = buy_ltp - self.cfg.slippage_points
                self._record_trade(trade, ts, "time_exit")
                day_state.cumulative_pnl += self.trades[-1].net_pnl
                return

        # If we ran out of candles without exiting, use last available
        last_sell = sell_candles.iloc[-1]
        last_buy = buy_candles.iloc[-1]
        trade.sell_exit = float(last_sell["close"]) + self.cfg.slippage_points
        trade.buy_exit = float(last_buy["close"]) - self.cfg.slippage_points
        self._record_trade(trade, sell_candles.index[-1], "time_exit")
        day_state.cumulative_pnl += self.trades[-1].net_pnl

    @staticmethod
    def _safe_row(df: pd.DataFrame, ts) -> dict | None:
        try:
            row = df.loc[ts]
            return {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        except KeyError:
            return None

    def _record_trade(self, trade: TradeState, exit_ts: datetime, exit_reason: str):
        """Record a completed trade with charges."""
        qty = trade.qty

        # If no exit prices set, use entry (flat trade)
        sell_exit = trade.sell_exit if trade.sell_exit > 0 else trade.sell_entry
        buy_exit = trade.buy_exit if trade.buy_exit > 0 else trade.buy_entry

        # Gross P&L:
        # Sell leg: (entry - exit) × qty  (short: profit when price drops)
        # Buy leg: (exit - entry) × qty   (long: profit when price rises)
        sell_pnl = (trade.sell_entry - sell_exit) * qty
        buy_pnl = (buy_exit - trade.buy_entry) * qty
        gross_pnl = sell_pnl + buy_pnl

        # Charges: 4 orders (sell entry, buy entry, sell exit for buy leg, buy exit for sell leg)
        charges_total = 0.0
        charges_breakdown = {"brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.0,
                             "sebi": 0.0, "gst": 0.0, "stamp_duty": 0.0, "total": 0.0, "num_orders": 4}

        orders = [
            (trade.sell_entry, qty, True),   # Entry: SELL PE (sell leg)
            (trade.buy_entry, qty, False),   # Entry: BUY PE (buy leg)
            (sell_exit, qty, False),          # Exit: BUY back sell leg
            (buy_exit, qty, True),           # Exit: SELL buy leg
        ]

        for premium, q, is_sell in orders:
            if premium > 0:
                c = calc_order_charges(premium, q, is_sell, self.cfg.charges)
                for key in ["brokerage", "stt", "exchange_txn", "sebi", "gst", "stamp_duty", "total"]:
                    charges_breakdown[key] += c[key]

        charges_total = charges_breakdown["total"]
        for key in charges_breakdown:
            if isinstance(charges_breakdown[key], float):
                charges_breakdown[key] = round(charges_breakdown[key], 2)

        net_pnl = gross_pnl - charges_total

        # Update compounded capital
        if self.cfg.compound_capital:
            self.running_capital += net_pnl

        duration = (exit_ts - trade.entry_time).total_seconds() / 60 if trade.entry_time else 0

        cap = self.running_capital if self.cfg.compound_capital else self.cfg.capital

        rec = TradeRecord(
            date=trade.entry_date,
            entry_time=trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
            exit_time=exit_ts.strftime("%H:%M") if exit_ts else "",
            duration_min=round(duration, 1),
            sell_entry=trade.sell_entry,
            sell_exit=sell_exit,
            buy_entry=trade.buy_entry,
            buy_exit=buy_exit,
            net_credit=trade.net_credit,
            spread_width_pts=self.cfg.spread_width,
            gross_pnl=round(gross_pnl, 2),
            charges=round(charges_total, 2),
            net_pnl=round(net_pnl, 2),
            exit_reason=exit_reason,
            dte=trade.current_dte if trade.current_dte is not None else -1,
            vix_at_entry=trade.vix_at_entry,
            underlying_at_entry=trade.underlying_at_entry,
            trade_number=len(self.trades) + 1,
            lot_size=trade.lot_size,
            number_of_lots=trade.number_of_lots,
            qty=trade.qty,
            capital_used=cap,
            charges_breakdown=charges_breakdown,
        )
        self.trades.append(rec)

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
                "sell_entry": t.sell_entry,
                "sell_exit": t.sell_exit,
                "buy_entry": t.buy_entry,
                "buy_exit": t.buy_exit,
                "net_credit": t.net_credit,
                "spread_width_pts": t.spread_width_pts,
                "gross_pnl": t.gross_pnl,
                "charges": t.charges,
                "net_pnl": t.net_pnl,
                "exit_reason": t.exit_reason,
                "dte": t.dte,
                "vix_at_entry": t.vix_at_entry,
                "underlying_at_entry": t.underlying_at_entry,
                "trade_number": t.trade_number,
                "lot_size": t.lot_size,
                "number_of_lots": t.number_of_lots,
                "qty": t.qty,
                "capital_used": t.capital_used,
            })

        df = pd.DataFrame(records)
        df["cumulative_pnl"] = df["net_pnl"].cumsum()
        return df
