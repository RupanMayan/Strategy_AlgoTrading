"""
Run optimization backtests — each config variant saves to its own results folder.

Usage:
    python scripts/run_optimization.py
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
from analytics import generate_all


# Optimization configs: (name, config_file)
TESTS = [
    ("no_reentry",  "config/opt_no_reentry.toml"),
    ("sl25",        "config/opt_sl25.toml"),
    ("no_dte1",     "config/opt_no_dte1.toml"),
    ("loss5000",    "config/opt_loss5000.toml"),
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


def run_single(name, config_path, spot_df, ce_df, pe_df, vix_df):
    """Run a single optimization backtest."""
    config_path = BACKTEST_DIR / config_path
    config = load_config(config_path)
    raw_config = toml.load(str(config_path))

    start_date = raw_config.get("backtest", {}).get("start_date", "2021-04-01")
    end_date = raw_config.get("backtest", {}).get("end_date", "2026-03-28")

    date_str = datetime.now().strftime("%Y-%m-%d")
    results_dir = BACKTEST_DIR / "results" / date_str / "optimization" / name
    results_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, results_dir / "config_snapshot.toml")

    print(f"\n  Running backtest...")
    engine = BacktestEngine(config, spot_df, ce_df, pe_df, vix_df)
    trades_df = engine.run(start_date, end_date)

    if trades_df.empty:
        print(f"  No trades generated!")
        return None

    generate_all(trades_df, results_dir, raw_config)

    summary = json.loads((results_dir / "summary.json").read_text())
    return summary


def main():
    spot_df, ce_df, pe_df, vix_df = load_data()

    results = {}
    for name, config_file in TESTS:
        print(f"\n{'='*60}")
        print(f"  Optimization: {name}")
        print(f"  Config: {config_file}")
        print(f"{'='*60}")

        summary = run_single(name, config_file, spot_df, ce_df, pe_df, vix_df)
        if summary:
            results[name] = summary

    # Print comparison table
    print(f"\n\n{'='*90}")
    print(f"  OPTIMIZATION COMPARISON")
    print(f"{'='*90}")
    print(f"{'Test':<15} {'Trades':>7} {'Net P&L':>12} {'Win%':>7} {'PF':>6} {'Sharpe':>7} {'Max DD':>12} {'Calmar':>8}")
    print(f"{'-'*90}")

    # Baseline
    baseline_path = BACKTEST_DIR / "results" / datetime.now().strftime("%Y-%m-%d") / "fixed" / "summary.json"
    if baseline_path.exists():
        bl = json.loads(baseline_path.read_text())
        print(f"{'BASELINE':<15} {bl['total_trades']:>7} {bl['net_pnl']:>12,.2f} {bl['win_rate']:>6.1f}% {bl['profit_factor']:>6.2f} {bl['sharpe_ratio']:>7.2f} {bl['max_drawdown']:>12,.2f} {bl['calmar_ratio']:>8.2f}")

    for name, s in results.items():
        print(f"{name:<15} {s['total_trades']:>7} {s['net_pnl']:>12,.2f} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['sharpe_ratio']:>7.2f} {s['max_drawdown']:>12,.2f} {s['calmar_ratio']:>8.2f}")

    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
