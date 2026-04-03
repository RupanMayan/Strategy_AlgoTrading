"""
Run V2 optimization backtests — Greeks / Option Chain / IV enhancements.
All tests use iv_entry_12 as the baseline and add one or more additional filters.

Tests 5 categories:
  1. Data IV (pre-computed IV from option chain, 2 variants)
  2. IV Skew (CE-PE IV difference, 3 variants)
  3. OI Entry Filter (combined open interest, 3 variants)
  4. Volume Entry Filter (2 variants)
  5. Put-Call OI Ratio (2 variants)
  6. Combined filters (3 variants)

Usage:
    python scripts/run_v2_optimization.py
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
    # Category 1: Data IV (pre-computed IV from option chain)
    ("data_iv_12",          "config/opt_v2_data_iv12.toml"),
    ("data_iv_15",          "config/opt_v2_data_iv15.toml"),
    # Category 2: IV Skew
    ("skew_max_5",          "config/opt_v2_skew5.toml"),
    ("skew_max_8",          "config/opt_v2_skew8.toml"),
    ("skew_max_10",         "config/opt_v2_skew10.toml"),
    # Category 3: OI Entry Filter
    ("oi_min_500k",         "config/opt_v2_oi500k.toml"),
    ("oi_min_1m",           "config/opt_v2_oi1m.toml"),
    ("oi_min_2m",           "config/opt_v2_oi2m.toml"),
    # Category 4: Volume Entry Filter
    ("vol_min_500",         "config/opt_v2_vol500.toml"),
    ("vol_min_1000",        "config/opt_v2_vol1000.toml"),
    # Category 5: Put-Call OI Ratio
    ("pcr_0.7_1.3",         "config/opt_v2_pcr_0.7_1.3.toml"),
    ("pcr_0.8_1.2",         "config/opt_v2_pcr_0.8_1.2.toml"),
    # Category 6: Combined Filters
    ("comb_skew_oi",        "config/opt_v2_combined_skew_oi.toml"),
    ("comb_skew_pcr",       "config/opt_v2_combined_skew_pcr.toml"),
    ("comb_all",            "config/opt_v2_combined_all.toml"),
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
    """Run iv_entry_12 as baseline for comparison."""
    return run_single(
        "BASELINE_iv12",
        "config/opt_iv_entry12.toml",
        spot_df, ce_df, pe_df, vix_df,
        results_base,
    )


def run_production(spot_df, ce_df, pe_df, vix_df, results_base):
    """Run production config for reference."""
    return run_single(
        "PRODUCTION",
        "config/config_production.toml",
        spot_df, ce_df, pe_df, vix_df,
        results_base,
    )


def print_comparison(production, baseline, results, results_base):
    """Print and save comparison table."""
    header = f"{'Test':<20} {'Trades':>7} {'Net P&L':>14} {'Δ vs IV12':>12} {'Win%':>7} {'PF':>6} {'Sharpe':>7} {'Max DD':>12} {'Calmar':>8}"
    separator = "─" * len(header)

    lines = []
    lines.append("")
    lines.append(f"{'═' * len(header)}")
    lines.append(f"  V2 GREEKS / OPTION CHAIN OPTIMIZATION COMPARISON")
    lines.append(f"{'═' * len(header)}")
    lines.append(header)
    lines.append(separator)

    baseline_pnl = baseline["net_pnl"] if baseline else 0

    all_rows = []

    # Production reference
    if production:
        row = {
            "test": "PRODUCTION",
            "trades": production["total_trades"],
            "net_pnl": production["net_pnl"],
            "delta_pnl": production["net_pnl"] - baseline_pnl,
            "win_rate": production["win_rate"],
            "profit_factor": production["profit_factor"],
            "sharpe_ratio": production["sharpe_ratio"],
            "max_drawdown": production["max_drawdown"],
            "calmar_ratio": production["calmar_ratio"],
        }
        all_rows.append(row)
        lines.append(
            f"{'PRODUCTION':<20} {production['total_trades']:>7} "
            f"{production['net_pnl']:>14,.2f} {production['net_pnl'] - baseline_pnl:>+12,.0f} "
            f"{production['win_rate']:>6.1f}% {production['profit_factor']:>6.2f} "
            f"{production['sharpe_ratio']:>7.2f} {production['max_drawdown']:>12,.2f} "
            f"{production['calmar_ratio']:>8.2f}"
        )

    # Baseline (iv_entry_12)
    if baseline:
        row = {
            "test": "BASELINE_iv12",
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
            f"{'BASELINE_iv12':<20} {baseline['total_trades']:>7} "
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
    print(f"Report saved to: {txt_path}")


def main():
    spot_df, ce_df, pe_df, vix_df = load_data()

    date_str = datetime.now().strftime("%Y-%m-%d")
    results_base = BACKTEST_DIR / "results" / date_str / "v2_optimization"
    results_base.mkdir(parents=True, exist_ok=True)

    # Run production for reference
    print(f"\n{'='*60}")
    print(f"  Running PRODUCTION (config_production.toml)")
    print(f"{'='*60}")
    production = run_production(spot_df, ce_df, pe_df, vix_df, results_base)

    # Run iv_entry_12 baseline
    print(f"\n{'='*60}")
    print(f"  Running BASELINE (iv_entry_12)")
    print(f"{'='*60}")
    baseline = run_baseline(spot_df, ce_df, pe_df, vix_df, results_base)

    # Run all test variants
    results = {}
    for name, config_file in TESTS:
        print(f"\n{'='*60}")
        print(f"  V2 Test: {name}")
        print(f"  Config: {config_file}")
        print(f"{'='*60}")

        summary = run_single(name, config_file, spot_df, ce_df, pe_df, vix_df, results_base)
        if summary:
            results[name] = summary

    # Print and save comparison
    print_comparison(production, baseline, results, results_base)


if __name__ == "__main__":
    main()
