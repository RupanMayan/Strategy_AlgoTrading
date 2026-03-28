"""
Nifty Short Straddle — Backtest Runner
Orchestrates: data load -> simulation -> analytics -> report

Usage:
    # First time: fetch data
    python scripts/data_fetcher.py

    # Run backtest
    python scripts/run_backtest.py

    # Or with custom config
    python scripts/run_backtest.py --config config/config.toml

    # Custom date range
    python scripts/run_backtest.py --start 2023-01-01 --end 2024-12-31
"""
from __future__ import annotations
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import toml

# Add scripts dir to path for imports
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKTEST_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from backtest_engine import BacktestEngine, load_config
from analytics import generate_all


def main():
    parser = argparse.ArgumentParser(description="Run Nifty Short Straddle Backtest")
    parser.add_argument("--config", default=str(BACKTEST_DIR / "config" / "config.toml"),
                        help="Path to config TOML file")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip data fetch (assume data exists)")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    raw_config = toml.load(str(config_path))

    # Date range from args or config
    start_date = args.start or raw_config.get("backtest", {}).get("start_date", "2021-04-01")
    end_date = args.end or raw_config.get("backtest", {}).get("end_date", "2026-03-28")

    print(f"\n{'='*60}")
    print(f"  Nifty Short Straddle Backtest")
    print(f"  Config: {config_path}")
    print(f"  Period: {start_date} to {end_date}")
    print(f"{'='*60}")

    # Create results folder: date / mode
    date_str = datetime.now().strftime("%Y-%m-%d")
    mode = "compounded" if config.compound_capital else "fixed"
    results_dir = BACKTEST_DIR / "results" / date_str / mode
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "charts").mkdir(exist_ok=True)

    # Snapshot config
    shutil.copy2(config_path, results_dir / "config_snapshot.toml")

    # Load data
    data_dir = BACKTEST_DIR / "data"
    print("\nLoading data...")

    spot_df = _load_parquet(data_dir / "nifty_spot_1min.parquet", "Nifty Spot")
    ce_df = _load_parquet(data_dir / "nifty_atm_ce_1min.parquet", "ATM CE")
    pe_df = _load_parquet(data_dir / "nifty_atm_pe_1min.parquet", "ATM PE")

    # VIX: prefer intraday, fall back to daily
    vix_file = data_dir / "india_vix_1min.parquet"
    if not vix_file.exists():
        vix_file = data_dir / "india_vix_daily.parquet"
    vix_df = _load_parquet(vix_file, "India VIX")

    if spot_df.empty or ce_df.empty or pe_df.empty:
        print("\nERROR: Missing required data files. Run data_fetcher.py first:")
        print("  python scripts/data_fetcher.py")
        sys.exit(1)

    # Run backtest
    print("\nRunning backtest engine...")
    engine = BacktestEngine(config, spot_df, ce_df, pe_df, vix_df)
    trades_df = engine.run(start_date, end_date)

    if trades_df.empty:
        print("\nNo trades generated! Check data coverage and filters.")
        sys.exit(1)

    # Generate analytics
    print("\nGenerating analytics and charts...")
    generate_all(trades_df, results_dir, raw_config)

    print(f"\n{'='*60}")
    print(f"  Backtest complete!")
    print(f"  Results: {results_dir}")
    print(f"{'='*60}\n")


def _load_parquet(path: Path, label: str) -> pd.DataFrame:
    """Load a parquet file with status message."""
    if not path.exists():
        print(f"  WARNING: {label} not found: {path}")
        return pd.DataFrame()

    df = pd.read_parquet(path)
    date_range = ""
    if "timestamp" in df.columns and len(df) > 0:
        date_range = f" ({df['timestamp'].min()} to {df['timestamp'].max()})"
    print(f"  {label}: {len(df):,} candles{date_range}")
    return df


if __name__ == "__main__":
    main()
