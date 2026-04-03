"""
Run IV/Premium optimization backtests — tests 4 improvement categories:
  1. Minimum Premium Filter (4 variants)
  2. VIX Lower Bound tightening (1 variant)
  3. IV Entry Filter via Black-76 (3 variants)
  4. IV Spike Exit via Black-76 (3 variants)

Usage:
    python scripts/run_iv_optimization.py
"""
from __future__ import annotations
import csv
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
from analytics import generate_all


TESTS = [
    # Feature 1: Minimum Premium Filter
    ("min_premium_100", "config/opt_min_premium100.toml"),
    ("min_premium_125", "config/opt_min_premium125.toml"),
    ("min_premium_150", "config/opt_min_premium150.toml"),
    ("min_premium_175", "config/opt_min_premium175.toml"),
    # Feature 2: VIX Lower Bound
    ("vix_min_12",      "config/opt_vix_min12.toml"),
    # Feature 3: IV Entry Filter (Black-76)
    ("iv_entry_8",      "config/opt_iv_entry8.toml"),
    ("iv_entry_10",     "config/opt_iv_entry10.toml"),
    ("iv_entry_12",     "config/opt_iv_entry12.toml"),
    # Feature 4: IV Spike Exit (Black-76)
    ("iv_spike_15",     "config/opt_iv_spike15.toml"),
    ("iv_spike_20",     "config/opt_iv_spike20.toml"),
    ("iv_spike_25",     "config/opt_iv_spike25.toml"),
]


def load_data():
    """Load data once, reuse for all tests."""
    data_dir = BACKTEST_DIR / "data"
    print("Loading data...")
    spot_df = pd.read_parquet(data_dir / "nifty_spot_1min.parquet")
    ce_df = pd.read_parquet(data_dir / "nifty_atm_ce_1min.parquet")
    pe_df = pd.read_parquet(data_dir / "nifty_atm_pe_1min.parquet")
    vix_file = data_dir / "india_vix_1min.parquet"
    if not vix_file.exists():
        vix_file = data_dir / "india_vix_daily.parquet"
    vix_df = pd.read_parquet(vix_file)
    print(f"  Spot: {len(spot_df):,} | CE: {len(ce_df):,} | PE: {len(pe_df):,} | VIX: {len(vix_df):,}")
    return spot_df, ce_df, pe_df, vix_df


def run_single(name, config_path, spot_df, ce_df, pe_df, vix_df, results_base):
    """Run a single optimization backtest."""
    config_path = BACKTEST_DIR / config_path
    config = load_config(config_path)
    raw_config = toml.load(str(config_path))

    start_date = raw_config.get("backtest", {}).get("start_date", "2021-04-01")
    end_date = raw_config.get("backtest", {}).get("end_date", "2026-03-28")

    results_dir = results_base / name
    results_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, results_dir / "config_snapshot.toml")

    print(f"  Running backtest...")
    engine = BacktestEngine(config, spot_df, ce_df, pe_df, vix_df)
    trades_df = engine.run(start_date, end_date)

    if trades_df.empty:
        print(f"  No trades generated!")
        return None

    generate_all(trades_df, results_dir, raw_config)

    summary = json.loads((results_dir / "summary.json").read_text())
    return summary


def run_baseline(spot_df, ce_df, pe_df, vix_df, results_base):
    """Run the production baseline for comparison."""
    return run_single(
        "BASELINE",
        "config/config_production.toml",
        spot_df, ce_df, pe_df, vix_df,
        results_base,
    )


def print_comparison(baseline, results, results_base):
    """Print and save comparison table."""
    header = f"{'Test':<20} {'Trades':>7} {'Net P&L':>14} {'Δ P&L':>12} {'Win%':>7} {'PF':>6} {'Sharpe':>7} {'Max DD':>12} {'Calmar':>8}"
    separator = "─" * len(header)

    lines = []
    lines.append("")
    lines.append(f"{'═' * len(header)}")
    lines.append(f"  IV / PREMIUM OPTIMIZATION COMPARISON")
    lines.append(f"{'═' * len(header)}")
    lines.append(header)
    lines.append(separator)

    baseline_pnl = baseline["net_pnl"] if baseline else 0

    all_rows = []
    if baseline:
        row = {
            "test": "BASELINE",
            "trades": baseline["total_trades"],
            "net_pnl": baseline["net_pnl"],
            "delta_pnl": 0,
            "win_rate": baseline["win_rate"],
            "profit_factor": baseline["profit_factor"],
            "sharpe_ratio": baseline["sharpe_ratio"],
            "max_drawdown": baseline["max_drawdown"],
            "calmar_ratio": baseline["calmar_ratio"],
        }
        all_rows.append(row)
        lines.append(
            f"{'BASELINE':<20} {baseline['total_trades']:>7} "
            f"{baseline['net_pnl']:>14,.2f} {'—':>12} "
            f"{baseline['win_rate']:>6.1f}% {baseline['profit_factor']:>6.2f} "
            f"{baseline['sharpe_ratio']:>7.2f} {baseline['max_drawdown']:>12,.2f} "
            f"{baseline['calmar_ratio']:>8.2f}"
        )

    lines.append(separator)

    for name, s in results.items():
        delta = s["net_pnl"] - baseline_pnl
        delta_str = f"{delta:>+12,.0f}"
        row = {
            "test": name,
            "trades": s["total_trades"],
            "net_pnl": s["net_pnl"],
            "delta_pnl": delta,
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "sharpe_ratio": s["sharpe_ratio"],
            "max_drawdown": s["max_drawdown"],
            "calmar_ratio": s["calmar_ratio"],
        }
        all_rows.append(row)
        lines.append(
            f"{name:<20} {s['total_trades']:>7} "
            f"{s['net_pnl']:>14,.2f} {delta_str} "
            f"{s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} "
            f"{s['sharpe_ratio']:>7.2f} {s['max_drawdown']:>12,.2f} "
            f"{s['calmar_ratio']:>8.2f}"
        )

    lines.append(f"{'═' * len(header)}")

    output = "\n".join(lines)
    print(output)

    # Save comparison CSV
    csv_path = results_base / "comparison.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nComparison saved to: {csv_path}")

    # Save comparison text
    txt_path = results_base / "comparison.txt"
    txt_path.write_text(output)


def main():
    spot_df, ce_df, pe_df, vix_df = load_data()

    date_str = datetime.now().strftime("%Y-%m-%d")
    results_base = BACKTEST_DIR / "results" / date_str / "iv_optimization"
    results_base.mkdir(parents=True, exist_ok=True)

    # Run baseline first
    print(f"\n{'='*60}")
    print(f"  Running BASELINE (config_production.toml)")
    print(f"{'='*60}")
    baseline = run_baseline(spot_df, ce_df, pe_df, vix_df, results_base)

    # Run all test variants
    results = {}
    for name, config_file in TESTS:
        print(f"\n{'='*60}")
        print(f"  Optimization: {name}")
        print(f"  Config: {config_file}")
        print(f"{'='*60}")

        summary = run_single(name, config_file, spot_df, ce_df, pe_df, vix_df, results_base)
        if summary:
            results[name] = summary

    # Print and save comparison
    print_comparison(baseline, results, results_base)


if __name__ == "__main__":
    main()
