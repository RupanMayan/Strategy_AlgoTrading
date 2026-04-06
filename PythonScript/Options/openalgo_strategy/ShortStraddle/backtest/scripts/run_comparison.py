"""
Nifty Short Straddle — Risk Management Comparison Runner
Runs baseline vs enhanced risk config side-by-side and outputs comparison table.

Usage:
    python scripts/run_comparison.py
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


def load_data(config):
    """Load all required data files."""
    data_dir = BACKTEST_DIR / "data"

    spot_df = pd.read_parquet(data_dir / "nifty_spot_1min.parquet")
    ce_df = pd.read_parquet(data_dir / "nifty_atm_ce_1min.parquet")
    pe_df = pd.read_parquet(data_dir / "nifty_atm_pe_1min.parquet")

    vix_file = data_dir / "india_vix_1min.parquet"
    if not vix_file.exists():
        vix_file = data_dir / "india_vix_daily.parquet"
    vix_df = pd.read_parquet(vix_file)

    otm_ce_df = pd.DataFrame()
    otm_pe_df = pd.DataFrame()

    return spot_df, ce_df, pe_df, vix_df, otm_ce_df, otm_pe_df


def run_backtest(config_path: Path, label: str, spot_df, ce_df, pe_df, vix_df,
                 otm_ce_df, otm_pe_df) -> tuple[dict, pd.DataFrame]:
    """Run a single backtest and return summary + trades."""
    config = load_config(config_path)
    raw_config = toml.load(str(config_path))

    start_date = raw_config.get("backtest", {}).get("start_date", "2021-04-01")
    end_date = raw_config.get("backtest", {}).get("end_date", "2026-03-28")

    print(f"\n{'─'*60}")
    print(f"  Running: {label}")
    print(f"  Config:  {config_path.name}")
    print(f"{'─'*60}")

    engine = BacktestEngine(config, spot_df, ce_df, pe_df, vix_df, otm_ce_df, otm_pe_df)
    trades_df = engine.run(start_date, end_date)

    if trades_df.empty:
        print(f"  WARNING: No trades for {label}!")
        return {}, trades_df

    trades_df["date"] = pd.to_datetime(trades_df["date"])
    summary = compute_summary(trades_df)

    # Add extra risk metrics
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

    # Recovery time from max drawdown (in trading days)
    max_dd_idx = drawdown.idxmin()
    recovery_days = 0
    if max_dd_idx < len(equity) - 1:
        dd_peak = peak.iloc[max_dd_idx]
        for i in range(max_dd_idx + 1, len(equity)):
            if equity.iloc[i] >= dd_peak:
                recovery_days = i - max_dd_idx
                break

    # Tail risk: worst 5% of trades
    worst_5pct = trades_df.nsmallest(max(1, len(trades_df) // 20), "net_pnl")

    summary["max_consec_loss_days"] = max_consec_loss
    summary["dd_recovery_days"] = recovery_days if recovery_days > 0 else "Not recovered"
    summary["worst_5pct_avg"] = round(worst_5pct["net_pnl"].mean(), 2)
    summary["trades_skipped_by_filters"] = ""  # filled in comparison

    return summary, trades_df


def print_comparison(baseline: dict, enhanced: dict):
    """Print side-by-side comparison table."""
    print(f"\n{'='*80}")
    print(f"  COMPARISON: Baseline vs Enhanced Risk Management")
    print(f"{'='*80}\n")

    metrics = [
        ("PERFORMANCE", None),
        ("Total Trades", "total_trades", "", False),
        ("Net P&L", "net_pnl", "Rs ", True),
        ("Gross P&L", "gross_pnl", "Rs ", True),
        ("Total Charges", "total_charges", "Rs ", False),
        ("Win Rate", "win_rate", "%", True),
        ("Profit Factor", "profit_factor", "", True),
        ("Avg Daily P&L", "avg_daily_pnl", "Rs ", True),

        ("RISK METRICS", None),
        ("Max Drawdown", "max_drawdown", "Rs ", False),  # Less negative = better
        ("Max DD Date", "max_drawdown_date", "", None),
        ("Sharpe Ratio", "sharpe_ratio", "", True),
        ("Calmar Ratio", "calmar_ratio", "", True),
        ("Largest Loss", "largest_loss", "Rs ", False),  # Less negative = better
        ("Largest Win", "largest_win", "Rs ", True),
        ("Avg Loss", "avg_loss", "Rs ", False),  # Less negative = better
        ("Avg Win", "avg_win", "Rs ", True),
        ("Max Consec Loss Days", "max_consec_loss_days", "", False),
        ("DD Recovery (trades)", "dd_recovery_days", "", False),
        ("Worst 5% Avg", "worst_5pct_avg", "Rs ", False),

        ("TRADE PROFILE", None),
        ("Avg Combined Premium", "avg_combined_premium", "Rs ", None),
        ("Avg Duration (min)", "avg_trade_duration_min", "", None),
        ("Profitable Days %", "profitable_days_pct", "%", True),
        ("Re-entry Trades", "reentry_trades", "", None),
        ("Best Month", "best_month", "", None),
        ("Best Month P&L", "best_month_pnl", "Rs ", None),
        ("Worst Month", "worst_month", "", None),
        ("Worst Month P&L", "worst_month_pnl", "Rs ", None),
    ]

    header = f"{'Metric':<28} {'Baseline':>16} {'Enhanced':>16} {'Delta':>14} {'Better?':>8}"
    print(header)
    print("─" * len(header))

    for item in metrics:
        if item[1] is None:
            # Section header
            print(f"\n  {item[0]}")
            print(f"  {'─'*74}")
            continue

        label, key, prefix, higher_is_better = item
        b_val = baseline.get(key, "N/A")
        e_val = enhanced.get(key, "N/A")

        # Format values
        if isinstance(b_val, float):
            b_str = f"{prefix}{b_val:,.2f}" if prefix else f"{b_val:,.2f}"
            e_str = f"{prefix}{e_val:,.2f}" if prefix else f"{e_val:,.2f}"
            delta = e_val - b_val
            if key in ("win_rate", "profitable_days_pct"):
                delta_str = f"{delta:+.1f}pp"
            else:
                delta_str = f"{prefix}{delta:+,.2f}" if prefix else f"{delta:+,.2f}"

            if higher_is_better is not None:
                if higher_is_better:
                    better = "YES" if delta > 0 else ("--" if delta == 0 else "no")
                else:
                    # For negative metrics (drawdown, loss), less negative = better
                    better = "YES" if delta > 0 else ("--" if delta == 0 else "no")
            else:
                better = ""
        elif isinstance(b_val, int):
            b_str = f"{prefix}{b_val:,}" if prefix else f"{b_val:,}"
            e_str = f"{prefix}{e_val:,}" if prefix else f"{e_val:,}"
            delta = e_val - b_val
            delta_str = f"{delta:+,}"
            better = ""
        else:
            b_str = str(b_val)
            e_str = str(e_val)
            delta_str = ""
            better = ""

        print(f"  {label:<26} {b_str:>16} {e_str:>16} {delta_str:>14} {better:>8}")

    # Trades filtered summary
    b_trades = baseline.get("total_trades", 0)
    e_trades = enhanced.get("total_trades", 0)
    filtered = b_trades - e_trades
    print(f"\n  {'Trades Filtered Out':<26} {'':>16} {'':>16} {filtered:>14}")

    # Risk-reward summary
    print(f"\n{'='*80}")
    print(f"  VERDICT")
    print(f"{'='*80}")

    b_rr = abs(baseline.get("net_pnl", 0) / baseline.get("max_drawdown", -1)) if baseline.get("max_drawdown", 0) != 0 else 0
    e_rr = abs(enhanced.get("net_pnl", 0) / enhanced.get("max_drawdown", -1)) if enhanced.get("max_drawdown", 0) != 0 else 0

    print(f"  Return/MaxDD Ratio:  Baseline={b_rr:.1f}x  |  Enhanced={e_rr:.1f}x")

    b_edge = baseline.get("avg_win", 0) / abs(baseline.get("avg_loss", -1)) if baseline.get("avg_loss", 0) != 0 else 0
    e_edge = enhanced.get("avg_win", 0) / abs(enhanced.get("avg_loss", -1)) if enhanced.get("avg_loss", 0) != 0 else 0
    print(f"  Win/Loss Ratio:      Baseline={b_edge:.2f}  |  Enhanced={e_edge:.2f}")

    pnl_delta = enhanced.get("net_pnl", 0) - baseline.get("net_pnl", 0)
    dd_delta = enhanced.get("max_drawdown", 0) - baseline.get("max_drawdown", 0)
    print(f"  P&L Change:          Rs {pnl_delta:+,.0f}")
    print(f"  Drawdown Change:     Rs {dd_delta:+,.0f} ({'improved' if dd_delta > 0 else 'worse'})")
    print(f"  Largest Loss Change: Rs {enhanced.get('largest_loss', 0) - baseline.get('largest_loss', 0):+,.0f}")
    print()


def main():
    config_dir = BACKTEST_DIR / "config"
    baseline_path = config_dir / "config.toml"
    enhanced_path = config_dir / "config_enhanced_risk.toml"

    if not baseline_path.exists() or not enhanced_path.exists():
        print("ERROR: Config files not found")
        sys.exit(1)

    # Load data once (shared across both runs)
    print("Loading data...")
    config = load_config(baseline_path)
    spot_df, ce_df, pe_df, vix_df, otm_ce_df, otm_pe_df = load_data(config)

    # Run both backtests
    baseline_summary, baseline_trades = run_backtest(
        baseline_path, "BASELINE (current config)", spot_df, ce_df, pe_df, vix_df, otm_ce_df, otm_pe_df
    )
    enhanced_summary, enhanced_trades = run_backtest(
        enhanced_path, "ENHANCED RISK (all 7 fixes)", spot_df, ce_df, pe_df, vix_df, otm_ce_df, otm_pe_df
    )

    if not baseline_summary or not enhanced_summary:
        print("ERROR: One or both backtests produced no results")
        sys.exit(1)

    # Print comparison
    print_comparison(baseline_summary, enhanced_summary)

    # Save results
    date_str = datetime.now().strftime("%Y-%m-%d")
    results_dir = BACKTEST_DIR / "results" / date_str / "comparison"
    results_dir.mkdir(parents=True, exist_ok=True)

    with open(results_dir / "baseline_summary.json", "w") as f:
        json.dump(baseline_summary, f, indent=2, default=str)
    with open(results_dir / "enhanced_summary.json", "w") as f:
        json.dump(enhanced_summary, f, indent=2, default=str)

    baseline_trades.to_csv(results_dir / "baseline_trades.csv", index=False)
    enhanced_trades.to_csv(results_dir / "enhanced_trades.csv", index=False)

    shutil.copy2(baseline_path, results_dir / "baseline_config.toml")
    shutil.copy2(enhanced_path, results_dir / "enhanced_config.toml")

    print(f"  Results saved to: {results_dir}")


if __name__ == "__main__":
    main()
