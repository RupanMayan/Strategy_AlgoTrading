"""
Nifty Short Straddle — Fixed vs Compounding Capital Comparison
Runs production config with compound_capital=false vs compound_capital=true.

Usage:
    python scripts/run_fixed_vs_compound.py
"""
from __future__ import annotations
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import toml

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKTEST_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from backtest_engine import BacktestEngine, load_config
from analytics import compute_summary


def load_data():
    """Load all required data files."""
    data_dir = BACKTEST_DIR / "data"
    spot_df = pd.read_parquet(data_dir / "nifty_spot_1min.parquet")
    ce_df = pd.read_parquet(data_dir / "nifty_atm_ce_1min.parquet")
    pe_df = pd.read_parquet(data_dir / "nifty_atm_pe_1min.parquet")

    vix_file = data_dir / "india_vix_1min.parquet"
    if not vix_file.exists():
        vix_file = data_dir / "india_vix_daily.parquet"
    vix_df = pd.read_parquet(vix_file)

    return spot_df, ce_df, pe_df, vix_df


def run_backtest(config_path: Path, label: str, spot_df, ce_df, pe_df, vix_df):
    """Run a single backtest and return summary + trades."""
    config = load_config(config_path)
    raw_config = toml.load(str(config_path))

    start_date = raw_config.get("backtest", {}).get("start_date", "2021-04-01")
    end_date = raw_config.get("backtest", {}).get("end_date", "2026-03-28")

    print(f"\n{'─'*60}")
    print(f"  Running: {label}")
    print(f"  Config:  {config_path.name}")
    print(f"  Compound capital: {config.compound_capital}")
    print(f"{'─'*60}")

    engine = BacktestEngine(config, spot_df, ce_df, pe_df, vix_df)
    trades_df = engine.run(start_date, end_date)

    if trades_df.empty:
        print(f"  WARNING: No trades for {label}!")
        return {}, trades_df, engine

    trades_df["date"] = pd.to_datetime(trades_df["date"])
    summary = compute_summary(trades_df)

    # Extra risk metrics
    equity = trades_df["net_pnl"].cumsum()
    peak = equity.cummax()
    drawdown = equity - peak

    # Consecutive losing days
    daily_pnl = trades_df.groupby(trades_df["date"].dt.date)["net_pnl"].sum()
    max_consec_loss = 0
    current_streak = 0
    for pnl in daily_pnl:
        if pnl < 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0

    summary["max_consec_loss_days"] = max_consec_loss

    # Worst 5% of trades
    worst_5pct = trades_df.nsmallest(max(1, len(trades_df) // 20), "net_pnl")
    summary["worst_5pct_avg"] = round(worst_5pct["net_pnl"].mean(), 2)

    return summary, trades_df, engine


def print_comparison(fixed: dict, compound: dict, fixed_trades: pd.DataFrame, compound_trades: pd.DataFrame):
    """Print side-by-side comparison table."""
    print(f"\n{'='*90}")
    print(f"  COMPARISON: Fixed Capital vs Compounding Capital")
    print(f"{'='*90}\n")

    metrics = [
        ("PERFORMANCE", None),
        ("Total Trades", "total_trades", "", None),
        ("Net P&L", "net_pnl", "Rs ", True),
        ("Gross P&L", "gross_pnl", "Rs ", True),
        ("Total Charges", "total_charges", "Rs ", False),
        ("Win Rate", "win_rate", "%", True),
        ("Profit Factor", "profit_factor", "", True),
        ("Avg Daily P&L", "avg_daily_pnl", "Rs ", True),

        ("RISK METRICS", None),
        ("Max Drawdown", "max_drawdown", "Rs ", False),
        ("Max DD Date", "max_drawdown_date", "", None),
        ("Sharpe Ratio", "sharpe_ratio", "", True),
        ("Calmar Ratio", "calmar_ratio", "", True),
        ("Largest Loss", "largest_loss", "Rs ", False),
        ("Largest Win", "largest_win", "Rs ", True),
        ("Avg Loss", "avg_loss", "Rs ", False),
        ("Avg Win", "avg_win", "Rs ", True),
        ("Max Consec Loss Days", "max_consec_loss_days", "", False),
        ("Worst 5% Avg", "worst_5pct_avg", "Rs ", False),

        ("LOT SIZING", None),
        ("Avg Lots/Trade", "avg_lots", "", True),
        ("Max Lots/Trade", "max_lots", "", None),
        ("Avg Qty/Trade", "avg_qty", "", None),

        ("TRADE PROFILE", None),
        ("Avg Combined Premium", "avg_combined_premium", "Rs ", None),
        ("Avg Duration (min)", "avg_trade_duration_min", "", None),
        ("Profitable Days %", "profitable_days_pct", "%", True),
        ("Best Month", "best_month", "", None),
        ("Best Month P&L", "best_month_pnl", "Rs ", None),
        ("Worst Month", "worst_month", "", None),
        ("Worst Month P&L", "worst_month_pnl", "Rs ", None),
    ]

    # Compute lot sizing stats from trades
    for label_key, trades in [("fixed", fixed_trades), ("compound", compound_trades)]:
        summary = fixed if label_key == "fixed" else compound
        if "number_of_lots" in trades.columns:
            summary["avg_lots"] = round(trades["number_of_lots"].mean(), 2)
            summary["max_lots"] = int(trades["number_of_lots"].max())
            summary["avg_qty"] = round(trades["qty"].mean(), 1)
        else:
            summary["avg_lots"] = 1
            summary["max_lots"] = 1
            summary["avg_qty"] = 65

    header = f"{'Metric':<28} {'Fixed':>16} {'Compound':>16} {'Delta':>14} {'Better?':>8}"
    print(header)
    print("─" * len(header))

    for item in metrics:
        if item[1] is None:
            print(f"\n  {item[0]}")
            print(f"  {'─'*80}")
            continue

        label, key, prefix, higher_is_better = item
        f_val = fixed.get(key, "N/A")
        c_val = compound.get(key, "N/A")

        if isinstance(f_val, float):
            f_str = f"{prefix}{f_val:,.2f}" if prefix else f"{f_val:,.2f}"
            c_str = f"{prefix}{c_val:,.2f}" if prefix else f"{c_val:,.2f}"
            delta = c_val - f_val
            if key in ("win_rate", "profitable_days_pct"):
                delta_str = f"{delta:+.1f}pp"
            else:
                delta_str = f"{prefix}{delta:+,.2f}" if prefix else f"{delta:+,.2f}"

            if higher_is_better is not None:
                if higher_is_better:
                    better = "YES" if delta > 0 else ("--" if delta == 0 else "no")
                else:
                    better = "YES" if delta > 0 else ("--" if delta == 0 else "no")
            else:
                better = ""
        elif isinstance(f_val, int):
            f_str = f"{prefix}{f_val:,}" if prefix else f"{f_val:,}"
            c_str = f"{prefix}{c_val:,}" if prefix else f"{c_val:,}"
            delta = c_val - f_val
            delta_str = f"{delta:+,}"
            better = ""
        else:
            f_str = str(f_val)
            c_str = str(c_val)
            delta_str = ""
            better = ""

        print(f"  {label:<26} {f_str:>16} {c_str:>16} {delta_str:>14} {better:>8}")

    # Capital growth summary
    print(f"\n{'='*90}")
    print(f"  CAPITAL GROWTH")
    print(f"{'='*90}")

    f_net = fixed.get("net_pnl", 0)
    c_net = compound.get("net_pnl", 0)
    capital = 250000

    print(f"  Starting Capital:        Rs {capital:,.0f}")
    print(f"  Fixed Final Capital:     Rs {capital + f_net:,.0f}  ({f_net/capital*100:+.1f}%)")
    print(f"  Compound Final Capital:  Rs {capital + c_net:,.0f}  ({c_net/capital*100:+.1f}%)")
    print(f"  Compounding Advantage:   Rs {c_net - f_net:+,.0f}  ({(c_net - f_net)/f_net*100:+.1f}% more)")
    print()

    # Year-wise breakdown
    print(f"  YEAR-WISE BREAKDOWN")
    print(f"  {'─'*80}")
    for trades, lbl in [(fixed_trades, "Fixed"), (compound_trades, "Compound")]:
        print(f"\n  {lbl}:")
        yearly = trades.groupby(trades["date"].dt.year).agg(
            trades=("net_pnl", "count"),
            net_pnl=("net_pnl", "sum"),
            avg_lots=("number_of_lots", "mean") if "number_of_lots" in trades.columns else ("net_pnl", "count"),
        )
        for year, row in yearly.iterrows():
            lots_str = f"avg {row['avg_lots']:.1f} lots" if "number_of_lots" in trades.columns else ""
            print(f"    {year}: {row['trades']:>4} trades, Rs {row['net_pnl']:>12,.2f}  {lots_str}")

    # Risk-reward verdict
    print(f"\n{'='*90}")
    print(f"  VERDICT")
    print(f"{'='*90}")

    f_rr = abs(f_net / fixed.get("max_drawdown", -1)) if fixed.get("max_drawdown", 0) != 0 else 0
    c_rr = abs(c_net / compound.get("max_drawdown", -1)) if compound.get("max_drawdown", 0) != 0 else 0
    print(f"  Return/MaxDD:  Fixed={f_rr:.1f}x  |  Compound={c_rr:.1f}x")

    f_dd_pct = abs(fixed.get("max_drawdown", 0)) / capital * 100
    c_dd_pct = abs(compound.get("max_drawdown", 0)) / capital * 100
    print(f"  Max DD as % of Capital:  Fixed={f_dd_pct:.1f}%  |  Compound={c_dd_pct:.1f}%")
    print()


def main():
    config_dir = BACKTEST_DIR / "config"
    fixed_path = config_dir / "config_production.toml"
    compound_path = config_dir / "config_production_compound.toml"

    if not fixed_path.exists() or not compound_path.exists():
        print("ERROR: Config files not found")
        sys.exit(1)

    print("Loading data...")
    spot_df, ce_df, pe_df, vix_df = load_data()

    fixed_summary, fixed_trades, _ = run_backtest(
        fixed_path, "FIXED CAPITAL (production)", spot_df, ce_df, pe_df, vix_df
    )
    compound_summary, compound_trades, _ = run_backtest(
        compound_path, "COMPOUNDING CAPITAL", spot_df, ce_df, pe_df, vix_df
    )

    if not fixed_summary or not compound_summary:
        print("ERROR: One or both backtests produced no results")
        sys.exit(1)

    print_comparison(fixed_summary, compound_summary, fixed_trades, compound_trades)

    # Save results
    date_str = datetime.now().strftime("%Y-%m-%d")
    results_dir = BACKTEST_DIR / "results" / date_str / "fixed_vs_compound"
    results_dir.mkdir(parents=True, exist_ok=True)

    with open(results_dir / "fixed_summary.json", "w") as f:
        json.dump(fixed_summary, f, indent=2, default=str)
    with open(results_dir / "compound_summary.json", "w") as f:
        json.dump(compound_summary, f, indent=2, default=str)

    fixed_trades.to_csv(results_dir / "fixed_trades.csv", index=False)
    compound_trades.to_csv(results_dir / "compound_trades.csv", index=False)

    shutil.copy2(fixed_path, results_dir / "fixed_config.toml")
    shutil.copy2(compound_path, results_dir / "compound_config.toml")

    print(f"  Results saved to: {results_dir}")


if __name__ == "__main__":
    main()
