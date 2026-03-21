"""
Nifty Short Straddle Strategy Optimizer

Systematic grid search across all tunable parameters to find the
optimal configuration. Tests combinations in priority order:

Phase 1: High-impact parameters (breakeven direction, spot multiplier,
         recovery trail, DTE filter)
Phase 2: SL parameters (base SL%, dynamic SL, trailing trigger)
Phase 3: IV-based entry filter (use available ce_iv/pe_iv data)
Phase 4: Fine-tune daily limits & decay thresholds

Usage:
    python optimizer.py              # Run full optimization
    python optimizer.py --phase 1    # Run specific phase
    python optimizer.py --quick      # Quick scan (fewer combos)
"""

import sys
import json
import copy
import argparse
import logging
from datetime import datetime
from pathlib import Path
from itertools import product

import pandas as pd
import numpy as np
from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from nifty_straddle_bt import load_config, run_backtest

logging.basicConfig(
    level=logging.WARNING,  # Suppress INFO during grid search
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def evaluate(config: dict) -> dict:
    """Run backtest and return key metrics."""
    trades_df = run_backtest(config)
    total_pnl = trades_df["total_pnl"].sum()
    num_trades = len(trades_df)
    winners = (trades_df["total_pnl"] > 0).sum()
    losers = (trades_df["total_pnl"] < 0).sum()
    win_rate = winners / num_trades * 100 if num_trades > 0 else 0
    avg_win = trades_df.loc[trades_df["total_pnl"] > 0, "total_pnl"].mean() if winners > 0 else 0
    avg_loss = trades_df.loc[trades_df["total_pnl"] < 0, "total_pnl"].mean() if losers > 0 else 0

    gross_wins = trades_df.loc[trades_df["total_pnl"] > 0, "total_pnl"].sum()
    gross_losses = abs(trades_df.loc[trades_df["total_pnl"] < 0, "total_pnl"].sum())
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    cumulative = trades_df["total_pnl"].cumsum()
    max_drawdown = (cumulative - cumulative.cummax()).min()

    daily_returns = trades_df["total_pnl"]
    sharpe = (
        (daily_returns.mean() / daily_returns.std()) * np.sqrt(num_trades)
        if daily_returns.std() > 0 else 0
    )

    # Per-DTE breakdown
    dte_pnl = trades_df.groupby("dte")["total_pnl"].sum().to_dict()

    # Exit reason summary
    if "exit_reason" in trades_df.columns:
        def categorize(r):
            for prefix in ["recovery_lock", "spot_move", "combined_decay",
                           "winner_booking", "asymmetric_book", "combined_trail"]:
                if r.startswith(prefix):
                    return prefix
            return r
        exit_cats = trades_df["exit_reason"].apply(categorize).value_counts().to_dict()
    else:
        exit_cats = {}

    return {
        "total_pnl": round(total_pnl, 2),
        "num_trades": num_trades,
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_drawdown, 2),
        "sharpe": round(sharpe, 2),
        "dte_pnl": dte_pnl,
        "exit_reasons": exit_cats,
    }


def run_grid(base_config: dict, param_grid: dict, label: str) -> pd.DataFrame:
    """Run grid search over parameter combinations."""
    # Generate all combinations
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(product(*values))

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Parameters: {', '.join(keys)}")
    print(f"  Combinations: {len(combos)}")
    print(f"{'='*70}\n")

    results = []
    for combo in tqdm(combos, desc=label):
        config = copy.deepcopy(base_config)
        params = dict(zip(keys, combo))

        # Apply parameters to config
        for key, value in params.items():
            apply_param(config, key, value)

        try:
            metrics = evaluate(config)
            row = {**params, **metrics}
            results.append(row)
        except Exception as e:
            log.warning(f"Failed combo {params}: {e}")
            continue

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("total_pnl", ascending=False).reset_index(drop=True)
        print(f"\n{'─'*70}")
        print(f"  TOP 10 RESULTS — {label}")
        print(f"{'─'*70}")
        display_cols = [k for k in keys] + ["total_pnl", "win_rate", "profit_factor", "max_drawdown", "sharpe", "num_trades"]
        display_cols = [c for c in display_cols if c in df.columns]
        print(df[display_cols].head(10).to_string(index=False))
        print(f"{'─'*70}")
        print(f"  BOTTOM 5 RESULTS")
        print(f"{'─'*70}")
        print(df[display_cols].tail(5).to_string(index=False))
        print()

    return df


def apply_param(config: dict, key: str, value):
    """Apply a named parameter to the config dict."""
    # Breakeven buffer direction
    if key == "be_buffer_direction":
        config["_be_buffer_direction"] = value  # Custom key handled in backtest

    # Breakeven buffer %
    elif key == "be_buffer_pct":
        config["risk"]["breakeven_sl"]["buffer_pct"] = value

    # Breakeven enabled
    elif key == "be_enabled":
        config["risk"]["breakeven_sl"]["enabled"] = value

    # Spot multiplier
    elif key == "spot_multiplier":
        if value == 0:
            config["risk"]["spot_move_exit"]["enabled"] = False
        else:
            config["risk"]["spot_move_exit"]["enabled"] = True
            config["risk"]["spot_move_exit"]["spot_multiplier"] = value

    # Recovery trail %
    elif key == "recovery_trail_pct":
        if value == 0:
            config["risk"]["recovery_lock"]["enabled"] = False
        else:
            config["risk"]["recovery_lock"]["enabled"] = True
            config["risk"]["recovery_lock"]["trail_pct"] = value

    # Recovery min Rs
    elif key == "recovery_min_rs":
        config["risk"]["recovery_lock"]["min_recovery_rs_per_lot"] = value

    # Trade DTEs
    elif key == "trade_dte":
        config["backtest"]["trade_dte"] = value

    # Base SL %
    elif key == "base_sl_pct":
        config["risk"]["leg_sl_percent"] = value

    # DTE SL overrides (scaled relative to base)
    elif key == "dte_sl_scale":
        base = config["risk"]["leg_sl_percent"]
        config["risk"]["dte_sl_override"]["2"] = base * value
        config["risk"]["dte_sl_override"]["3"] = base * value * 1.12
        config["risk"]["dte_sl_override"]["4"] = base * value * 1.2

    # Dynamic SL enabled
    elif key == "dynamic_sl":
        config["risk"]["dynamic_sl"]["enabled"] = value

    # Trailing SL trigger
    elif key == "trail_trigger_pct":
        if value == 0:
            config["risk"]["trailing_sl"]["enabled"] = False
        else:
            config["risk"]["trailing_sl"]["enabled"] = True
            config["risk"]["trailing_sl"]["trigger_pct"] = value

    # Trailing lock %
    elif key == "trail_lock_pct":
        config["risk"]["trailing_sl"]["lock_pct"] = value

    # Daily profit target
    elif key == "profit_target":
        config["risk"]["daily_profit_target_per_lot"] = value

    # Daily loss limit
    elif key == "loss_limit":
        config["risk"]["daily_loss_limit_per_lot"] = value

    # Combined decay target
    elif key == "decay_target_pct":
        config["risk"]["combined_decay_exit"]["decay_target_pct"] = value

    # Combined decay DTE 0 override
    elif key == "decay_dte0":
        config["risk"]["combined_decay_exit"]["dte_override"]["0"] = value

    # Net P&L guard
    elif key == "npg_defer_min":
        config["risk"]["net_pnl_guard"]["max_defer_min"] = value

    # Slippage
    elif key == "slippage_pct":
        config["backtest"]["slippage_pct"] = value

    # IV filter (custom — high/low IV threshold for entry)
    elif key == "iv_entry_max":
        config["_iv_entry_max"] = value

    # Entry time shift
    elif key == "entry_delay_min":
        config["_entry_delay_min"] = value

    # Re-entry enabled
    elif key == "reentry_enabled":
        config["risk"]["reentry"]["enabled"] = value

    # Re-entry max per day
    elif key == "reentry_max_per_day":
        config["risk"]["reentry"]["enabled"] = True
        config["risk"]["reentry"]["max_per_day"] = value

    # Re-entry cooldown
    elif key == "reentry_cooldown_min":
        config["risk"]["reentry"]["cooldown_min"] = value

    # Re-entry max loss per lot
    elif key == "reentry_max_loss_per_lot":
        config["risk"]["reentry"]["max_loss_per_lot"] = value

    else:
        raise ValueError(f"Unknown parameter: {key}")


def phase1_high_impact(base_config: dict) -> pd.DataFrame:
    """Phase 1: Test highest-impact parameters."""
    grid = {
        "be_buffer_direction": ["down", "up"],
        "spot_multiplier": [0, 1.0, 1.5, 2.0],  # 0 = disabled
        "recovery_trail_pct": [0, 50, 70, 90],   # 0 = disabled
        "trade_dte": [
            [0, 1, 2, 3, 4],  # All
            [0, 3, 4],        # Skip loss-making DTE 1,2
            [0, 4],           # Best DTEs only
            [3, 4],           # Most consistent
        ],
    }
    return run_grid(base_config, grid, "Phase 1: High-Impact Parameters")


def phase2_sl_tuning(base_config: dict) -> pd.DataFrame:
    """Phase 2: Tune SL parameters with best Phase 1 settings."""
    grid = {
        "base_sl_pct": [15.0, 20.0, 25.0, 30.0],
        "dynamic_sl": [True, False],
        "trail_trigger_pct": [0, 40.0, 50.0, 60.0],  # 0 = disabled
        "trail_lock_pct": [10.0, 15.0, 20.0],
    }
    return run_grid(base_config, grid, "Phase 2: SL Tuning")


def phase3_daily_limits(base_config: dict) -> pd.DataFrame:
    """Phase 3: Tune daily limits and decay thresholds."""
    grid = {
        "profit_target": [4000, 5000, 6000, 8000],
        "loss_limit": [-3000, -4000, -5000, -6000],
        "decay_dte0": [60.0, 70.0, 80.0],
        "be_buffer_pct": [5.0, 10.0, 15.0, 20.0],
    }
    return run_grid(base_config, grid, "Phase 3: Daily Limits & Decay")


def phase4_slippage_stress(best_config: dict) -> pd.DataFrame:
    """Phase 4: Stress test best config with slippage."""
    grid = {
        "slippage_pct": [0.0, 0.5, 1.0, 1.5, 2.0],
    }
    return run_grid(best_config, grid, "Phase 4: Slippage Stress Test")


def save_results(results: dict, output_dir: Path):
    """Save optimization results to JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for phase_name, df in results.items():
        if df is not None and not df.empty:
            csv_path = output_dir / f"optimizer_{phase_name}_{timestamp}.csv"
            df.to_csv(csv_path, index=False)
            print(f"  Saved: {csv_path}")


def main():
    parser = argparse.ArgumentParser(description="Strategy Parameter Optimizer")
    parser.add_argument("--phase", type=int, default=0, help="Run specific phase (1-4), 0=all")
    parser.add_argument("--quick", action="store_true", help="Quick scan with fewer combos")
    args = parser.parse_args()

    base_config = load_config()
    output_dir = SCRIPT_DIR / "output" / "optimizer"
    results = {}

    print("\n" + "=" * 70)
    print("  NIFTY SHORT STRADDLE — PARAMETER OPTIMIZER")
    print("  Data: 2025-03-21 to 2026-03-21 (224 trading days)")
    print("=" * 70)

    # ── Phase 1: High-impact parameters ──
    if args.phase in [0, 1]:
        p1_df = phase1_high_impact(base_config)
        results["phase1"] = p1_df

        if not p1_df.empty:
            best = p1_df.iloc[0]
            print(f"\n  BEST Phase 1: P&L=₹{best['total_pnl']:,.0f}, "
                  f"WR={best['win_rate']:.1f}%, PF={best['profit_factor']:.2f}, "
                  f"DD=₹{best['max_drawdown']:,.0f}")

            # Apply best Phase 1 settings for Phase 2
            best_config = copy.deepcopy(base_config)
            for key in ["be_buffer_direction", "spot_multiplier", "recovery_trail_pct", "trade_dte"]:
                if key in best:
                    apply_param(best_config, key, best[key])
        else:
            best_config = base_config

    # ── Phase 2: SL tuning ──
    if args.phase in [0, 2]:
        p2_base = best_config if "best_config" in dir() else base_config
        p2_df = phase2_sl_tuning(p2_base)
        results["phase2"] = p2_df

        if not p2_df.empty:
            best2 = p2_df.iloc[0]
            print(f"\n  BEST Phase 2: P&L=₹{best2['total_pnl']:,.0f}, "
                  f"WR={best2['win_rate']:.1f}%, PF={best2['profit_factor']:.2f}")

            # Apply best Phase 2 settings
            for key in ["base_sl_pct", "dynamic_sl", "trail_trigger_pct", "trail_lock_pct"]:
                if key in best2:
                    apply_param(best_config, key, best2[key])

    # ── Phase 3: Daily limits ──
    if args.phase in [0, 3]:
        p3_base = best_config if "best_config" in dir() else base_config
        p3_df = phase3_daily_limits(p3_base)
        results["phase3"] = p3_df

        if not p3_df.empty:
            best3 = p3_df.iloc[0]
            print(f"\n  BEST Phase 3: P&L=₹{best3['total_pnl']:,.0f}, "
                  f"WR={best3['win_rate']:.1f}%, PF={best3['profit_factor']:.2f}")

            for key in ["profit_target", "loss_limit", "decay_dte0", "be_buffer_pct"]:
                if key in best3:
                    apply_param(best_config, key, best3[key])

    # ── Phase 4: Slippage stress ──
    if args.phase in [0, 4]:
        p4_base = best_config if "best_config" in dir() else base_config
        p4_df = phase4_slippage_stress(p4_base)
        results["phase4"] = p4_df

    # ── Save all results ──
    save_results(results, output_dir)

    # ── Final summary ──
    print(f"\n{'='*70}")
    print(f"  OPTIMIZATION COMPLETE")
    print(f"{'='*70}")
    if "best_config" in dir():
        print(f"\n  Best configuration found. Results saved to {output_dir}/")
    print()


if __name__ == "__main__":
    main()
