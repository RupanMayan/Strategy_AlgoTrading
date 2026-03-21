"""
Nifty Short Straddle Backtest Engine

Runs a day-by-day simulation of the core short straddle strategy
using cached Dhan options data, then generates VectorBT analytics.

Usage:
    python nifty_straddle_bt.py
"""

import sys
import json
import logging
from datetime import datetime, time, date
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import tomli
except ImportError:
    import tomllib as tomli

from nse_holidays import compute_dte, is_trading_day, get_weekly_expiry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Config Loading ─────────────────────────────────────────────────────
def load_config() -> dict:
    config_path = SCRIPT_DIR / "config_backtest.toml"
    with open(config_path, "rb") as f:
        return tomli.load(f)


# ── Dynamic SL Tightening ─────────────────────────────────────────────
def get_dynamic_sl_pct(
    current_time: time,
    base_sl_pct: float,
    schedule: list[dict],
) -> float:
    """
    Get the effective SL% based on time-of-day schedule.
    Schedule entries are checked from latest to earliest.
    Returns the tightest applicable SL%.
    """
    # Sort schedule by time descending
    sorted_sched = sorted(
        schedule,
        key=lambda s: datetime.strptime(s["time"], "%H:%M").time(),
        reverse=True,
    )
    for entry in sorted_sched:
        sched_time = datetime.strptime(entry["time"], "%H:%M").time()
        if current_time >= sched_time:
            return min(entry["sl_pct"], base_sl_pct)
    return base_sl_pct


# ── Single Day Simulation ─────────────────────────────────────────────
def simulate_day(
    day_df: pd.DataFrame,
    trading_date: date,
    config: dict,
) -> dict | None:
    """
    Simulate one trading day of short straddle.

    Returns a dict with trade details or None if no trade taken.
    """
    dte = compute_dte(trading_date)
    expiry = get_weekly_expiry(trading_date)
    bt_cfg = config["backtest"]
    risk_cfg = config["risk"]
    timing_cfg = config["timing"]
    instrument_cfg = config["instrument"]

    # ── Filter: Skip if DTE not in allowed list ──
    if dte not in bt_cfg["trade_dte"]:
        return None

    # ── Filter: Skip if month in skip_months ──
    if trading_date.month in bt_cfg["skip_months"]:
        return None

    # ── Determine entry time ──
    if timing_cfg.get("use_dte_entry_map", False):
        dte_map = timing_cfg.get("dte_entry_time_map", {})
        entry_str = dte_map.get(str(dte), timing_cfg["entry_time"])
    else:
        entry_str = timing_cfg["entry_time"]

    entry_time = datetime.strptime(entry_str, "%H:%M").time()
    exit_time = datetime.strptime(timing_cfg["exit_time"], "%H:%M").time()

    # ── Find entry candle ──
    day_df = day_df.copy()
    day_df["time"] = day_df["timestamp"].dt.time

    entry_candles = day_df[day_df["time"] >= entry_time]
    if entry_candles.empty:
        return None  # No candle at/after entry time

    entry_row = entry_candles.iloc[0]

    # Check we have valid CE and PE prices
    ce_entry = entry_row.get("ce_close")
    pe_entry = entry_row.get("pe_close")
    spot_at_entry = entry_row.get("spot")

    if pd.isna(ce_entry) or pd.isna(pe_entry) or ce_entry <= 0 or pe_entry <= 0:
        log.warning(f"{trading_date}: Invalid entry prices CE={ce_entry} PE={pe_entry}")
        return None

    combined_premium = ce_entry + pe_entry

    # ── SL Parameters ──
    base_sl_pct = risk_cfg.get("dte_sl_override", {}).get(
        str(dte), risk_cfg["leg_sl_percent"]
    )
    dynamic_sl_cfg = risk_cfg.get("dynamic_sl", {})
    dynamic_enabled = dynamic_sl_cfg.get("enabled", False)
    schedule = dynamic_sl_cfg.get("schedule", [])
    slippage_pct = bt_cfg.get("slippage_pct", 0.0)

    lot_size = instrument_cfg["lot_size"]
    num_lots = instrument_cfg["number_of_lots"]
    qty = lot_size * num_lots

    # Daily limits
    profit_target = risk_cfg["daily_profit_target_per_lot"] * num_lots
    loss_limit = risk_cfg["daily_loss_limit_per_lot"] * num_lots

    # ── Monitor candles (after entry, up to exit time) ──
    monitor_df = day_df[
        (day_df["timestamp"] > entry_row["timestamp"])
        & (day_df["time"] <= exit_time)
    ]

    # Track leg state
    ce_active = True
    pe_active = True
    ce_exit_price = None
    pe_exit_price = None
    ce_exit_time = None
    pe_exit_time = None
    ce_sl_hit = False
    pe_sl_hit = False

    for _, row in monitor_df.iterrows():
        current_time = row["time"]

        # Compute effective SL% for this minute
        if dynamic_enabled:
            eff_sl_pct = get_dynamic_sl_pct(current_time, base_sl_pct, schedule)
        else:
            eff_sl_pct = base_sl_pct

        # ── Check CE leg SL ──
        if ce_active:
            ce_sl_level = ce_entry * (1 + eff_sl_pct / 100)
            ce_high = row.get("ce_high", 0)
            if not pd.isna(ce_high) and ce_high >= ce_sl_level:
                ce_exit_price = ce_sl_level * (1 + slippage_pct / 100)
                ce_exit_time = row["timestamp"]
                ce_active = False
                ce_sl_hit = True

        # ── Check PE leg SL ──
        if pe_active:
            pe_sl_level = pe_entry * (1 + eff_sl_pct / 100)
            pe_high = row.get("pe_high", 0)
            if not pd.isna(pe_high) and pe_high >= pe_sl_level:
                pe_exit_price = pe_sl_level * (1 + slippage_pct / 100)
                pe_exit_time = row["timestamp"]
                pe_active = False
                pe_sl_hit = True

        # ── Check daily limits (combined P&L) — every candle, not just after leg close ──
        # Calculate running P&L using exit price for closed legs, current LTP for open legs
        ce_current = ce_exit_price if not ce_active else row.get("ce_close", ce_entry)
        pe_current = pe_exit_price if not pe_active else row.get("pe_close", pe_entry)
        running_pnl = (ce_entry - ce_current) * qty + (pe_entry - pe_current) * qty

        if running_pnl >= profit_target:
            # Close remaining legs at current price
            if ce_active:
                ce_exit_price = row.get("ce_close", ce_entry)
                ce_exit_time = row["timestamp"]
                ce_active = False
            if pe_active:
                pe_exit_price = row.get("pe_close", pe_entry)
                pe_exit_time = row["timestamp"]
                pe_active = False
            break

        if running_pnl <= loss_limit:
            if ce_active:
                ce_exit_price = row.get("ce_close", ce_entry)
                ce_exit_time = row["timestamp"]
                ce_active = False
            if pe_active:
                pe_exit_price = row.get("pe_close", pe_entry)
                pe_exit_time = row["timestamp"]
                pe_active = False
            break

        # Both legs closed — stop monitoring
        if not ce_active and not pe_active:
            break

    # ── Hard exit at 15:15 for any remaining legs ──
    if ce_active or pe_active:
        exit_candles = day_df[day_df["time"] >= exit_time]
        if not exit_candles.empty:
            exit_row = exit_candles.iloc[0]
            if ce_active:
                ce_exit_price = exit_row.get("ce_close", ce_entry)
                ce_exit_time = exit_row["timestamp"]
                ce_active = False
            if pe_active:
                pe_exit_price = exit_row.get("pe_close", pe_entry)
                pe_exit_time = exit_row["timestamp"]
                pe_active = False
        else:
            # Use last candle if no 15:15 candle
            last_row = day_df.iloc[-1]
            if ce_active:
                ce_exit_price = last_row.get("ce_close", ce_entry)
                ce_exit_time = last_row["timestamp"]
            if pe_active:
                pe_exit_price = last_row.get("pe_close", pe_entry)
                pe_exit_time = last_row["timestamp"]

    # ── Calculate P&L (short position: profit = entry - exit) ──
    ce_pnl = (ce_entry - ce_exit_price) * qty
    pe_pnl = (pe_entry - pe_exit_price) * qty
    total_pnl = ce_pnl + pe_pnl

    return {
        "date": trading_date.isoformat(),
        "expiry": expiry.isoformat(),
        "dte": dte,
        "spot_at_entry": round(spot_at_entry, 2),
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "combined_premium": round(combined_premium, 2),
        "ce_exit": round(ce_exit_price, 2),
        "pe_exit": round(pe_exit_price, 2),
        "ce_sl_hit": ce_sl_hit,
        "pe_sl_hit": pe_sl_hit,
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "entry_time": entry_str,
        "ce_exit_time": str(ce_exit_time) if ce_exit_time else None,
        "pe_exit_time": str(pe_exit_time) if pe_exit_time else None,
    }


# ── Run Full Backtest ──────────────────────────────────────────────────
def run_backtest(config: dict) -> pd.DataFrame:
    """Run backtest across all trading days."""
    data_path = SCRIPT_DIR / "data" / "nifty_options_2025" / "nifty_atm_weekly_1min.parquet"
    if not data_path.exists():
        log.error(f"Data file not found: {data_path}")
        log.error("Run dhan_data_fetcher.py first to download data.")
        sys.exit(1)

    df = pd.read_parquet(data_path)
    log.info(f"Loaded {len(df):,} rows from {data_path}")

    # Ensure timestamp is tz-aware
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")

    # Group by trading date
    df["trading_date"] = df["timestamp"].dt.date
    grouped = df.groupby("trading_date")

    trades = []
    for trading_date, day_df in tqdm(grouped, desc="Backtesting"):
        if not is_trading_day(trading_date):
            continue

        result = simulate_day(day_df, trading_date, config)
        if result:
            trades.append(result)

    if not trades:
        log.error("No trades generated!")
        sys.exit(1)

    trades_df = pd.DataFrame(trades)
    log.info(f"Generated {len(trades_df)} trades")
    return trades_df


# ── VectorBT Analytics ─────────────────────────────────────────────────
def generate_analytics(trades_df: pd.DataFrame, config: dict):
    """Generate analytics and save results."""
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend
    import matplotlib.pyplot as plt

    output_dir = SCRIPT_DIR / "output"
    charts_dir = output_dir / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    lot_size = config["instrument"]["lot_size"]
    num_lots = config["instrument"]["number_of_lots"]

    # ── Save trade log ──
    trades_df.to_csv(output_dir / "bt_trades.csv", index=False)
    log.info(f"Trade log saved to {output_dir / 'bt_trades.csv'}")

    # ── Compute equity curve ──
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    trades_df["cumulative_pnl"] = trades_df["total_pnl"].cumsum()

    # ── Summary Statistics ──
    total_pnl = trades_df["total_pnl"].sum()
    num_trades = len(trades_df)
    winners = trades_df[trades_df["total_pnl"] > 0]
    losers = trades_df[trades_df["total_pnl"] < 0]
    win_rate = len(winners) / num_trades * 100 if num_trades > 0 else 0
    avg_win = winners["total_pnl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["total_pnl"].mean() if len(losers) > 0 else 0
    profit_factor = (
        abs(winners["total_pnl"].sum() / losers["total_pnl"].sum())
        if len(losers) > 0 and losers["total_pnl"].sum() != 0
        else float("inf")
    )

    # Max drawdown
    cumulative = trades_df["cumulative_pnl"]
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max
    max_drawdown = drawdown.min()
    max_drawdown_pct = (max_drawdown / running_max[drawdown.idxmin()] * 100
                        if running_max[drawdown.idxmin()] != 0 else 0)

    # Sharpe ratio (using actual trade count for annualization)
    daily_returns = trades_df["total_pnl"]
    trades_per_year = len(daily_returns)  # actual trades in backtest period
    sharpe = (
        (daily_returns.mean() / daily_returns.std()) * np.sqrt(trades_per_year)
        if daily_returns.std() > 0
        else 0
    )

    # Sortino ratio
    downside = daily_returns[daily_returns < 0]
    sortino = (
        (daily_returns.mean() / downside.std()) * np.sqrt(trades_per_year)
        if len(downside) > 0 and downside.std() > 0
        else 0
    )

    # Calmar ratio (annualized return / max drawdown)
    total_pnl = trades_df["total_pnl"].sum()
    calmar = abs(total_pnl / max_drawdown) if max_drawdown != 0 else 0

    # SL hit analysis
    ce_sl_hits = trades_df["ce_sl_hit"].sum()
    pe_sl_hits = trades_df["pe_sl_hit"].sum()
    both_sl_hits = ((trades_df["ce_sl_hit"]) & (trades_df["pe_sl_hit"])).sum()
    no_sl_hits = ((~trades_df["ce_sl_hit"]) & (~trades_df["pe_sl_hit"])).sum()

    # Per-DTE breakdown
    dte_stats = trades_df.groupby("dte").agg(
        count=("total_pnl", "count"),
        total_pnl=("total_pnl", "sum"),
        avg_pnl=("total_pnl", "mean"),
        win_rate=("total_pnl", lambda x: (x > 0).sum() / len(x) * 100),
    ).to_dict(orient="index")

    summary = {
        "total_pnl": round(total_pnl, 2),
        "num_trades": num_trades,
        "win_rate_pct": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "ce_sl_hits": int(ce_sl_hits),
        "pe_sl_hits": int(pe_sl_hits),
        "both_sl_hits": int(both_sl_hits),
        "no_sl_days": int(no_sl_hits),
        "per_dte": {str(k): {kk: round(vv, 2) for kk, vv in v.items()} for k, v in dte_stats.items()},
        "lot_size": lot_size,
        "num_lots": num_lots,
        "backtest_period": f"{config['backtest']['from_date']} to {config['backtest']['to_date']}",
    }

    # Save summary
    with open(output_dir / "bt_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info(f"Summary saved to {output_dir / 'bt_summary.json'}")

    # ── Charts ──

    # 1. Equity Curve
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(trades_df["date"], trades_df["cumulative_pnl"], linewidth=1.5, color="blue")
    ax.fill_between(trades_df["date"], trades_df["cumulative_pnl"], alpha=0.15, color="blue")
    ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax.set_title(f"Equity Curve — Nifty Short Straddle (Total P&L: ₹{total_pnl:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative P&L (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "equity_curve.png", dpi=150)
    plt.close(fig)

    # 2. Drawdown Chart
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(trades_df["date"], drawdown, color="red", alpha=0.4)
    ax.set_title(f"Drawdown (Max: ₹{max_drawdown:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "drawdown.png", dpi=150)
    plt.close(fig)

    # 3. Monthly Returns Heatmap
    trades_df["year"] = trades_df["date"].dt.year
    trades_df["month"] = trades_df["date"].dt.month
    monthly = trades_df.groupby(["year", "month"])["total_pnl"].sum().unstack(fill_value=0)

    fig, ax = plt.subplots(figsize=(14, 4))
    im = ax.imshow(monthly.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(monthly.columns)))
    ax.set_xticklabels([f"M{m}" for m in monthly.columns])
    ax.set_yticks(range(len(monthly.index)))
    ax.set_yticklabels(monthly.index)
    # Add text annotations
    for i in range(len(monthly.index)):
        for j in range(len(monthly.columns)):
            val = monthly.values[i, j]
            ax.text(j, i, f"₹{val:,.0f}", ha="center", va="center", fontsize=8,
                    color="black" if abs(val) < monthly.values.max() * 0.5 else "white")
    ax.set_title("Monthly P&L Heatmap")
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(charts_dir / "monthly_heatmap.png", dpi=150)
    plt.close(fig)

    # 4. Per-DTE Performance
    dte_df = trades_df.groupby("dte").agg(
        total=("total_pnl", "sum"),
        avg=("total_pnl", "mean"),
        count=("total_pnl", "count"),
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].bar(dte_df["dte"], dte_df["total"], color=["green" if x > 0 else "red" for x in dte_df["total"]])
    axes[0].set_title("Total P&L by DTE")
    axes[0].set_xlabel("DTE")
    axes[0].set_ylabel("Total P&L (₹)")
    axes[0].grid(True, alpha=0.3)

    axes[1].bar(dte_df["dte"], dte_df["avg"], color=["green" if x > 0 else "red" for x in dte_df["avg"]])
    axes[1].set_title("Avg P&L per Trade by DTE")
    axes[1].set_xlabel("DTE")
    axes[1].set_ylabel("Avg P&L (₹)")
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("Performance by Days-to-Expiry")
    fig.tight_layout()
    fig.savefig(charts_dir / "dte_breakdown.png", dpi=150)
    plt.close(fig)

    # ── Print Summary to Console ──
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — Nifty Short Straddle (Core)")
    print(f"{'='*60}")
    print(f"  Period:         {config['backtest']['from_date']} → {config['backtest']['to_date']}")
    print(f"  Total Trades:   {num_trades}")
    print(f"  Total P&L:      ₹{total_pnl:,.2f}")
    print(f"  Win Rate:       {win_rate:.1f}%")
    print(f"  Avg Win:        ₹{avg_win:,.2f}")
    print(f"  Avg Loss:       ₹{avg_loss:,.2f}")
    print(f"  Profit Factor:  {profit_factor:.2f}")
    print(f"  Max Drawdown:   ₹{max_drawdown:,.2f}")
    print(f"  Sharpe Ratio:   {sharpe:.2f}")
    print(f"  Sortino Ratio:  {sortino:.2f}")
    print(f"  Calmar Ratio:   {calmar:.2f}")
    print(f"{'─'*60}")
    print(f"  CE SL Hits:     {ce_sl_hits} ({ce_sl_hits/num_trades*100:.1f}%)")
    print(f"  PE SL Hits:     {pe_sl_hits} ({pe_sl_hits/num_trades*100:.1f}%)")
    print(f"  Both SL Hit:    {both_sl_hits} ({both_sl_hits/num_trades*100:.1f}%)")
    print(f"  No SL (decay):  {no_sl_hits} ({no_sl_hits/num_trades*100:.1f}%)")
    print(f"{'─'*60}")
    print(f"  Per-DTE:")
    for dte_val, stats in sorted(dte_stats.items()):
        print(f"    DTE {dte_val}: {stats['count']:.0f} trades, ₹{stats['total_pnl']:,.0f} total, "
              f"₹{stats['avg_pnl']:,.0f} avg, {stats['win_rate']:.0f}% win")
    print(f"{'='*60}")
    print(f"\n  Output: {output_dir}/")
    print(f"  Charts: {charts_dir}/")


# ── Main ───────────────────────────────────────────────────────────────
def main():
    config = load_config()
    trades_df = run_backtest(config)
    generate_analytics(trades_df, config)


if __name__ == "__main__":
    main()
