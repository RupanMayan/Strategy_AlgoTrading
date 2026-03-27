"""
Post-Trade Analytics & Chart Generation
Uses VectorBT for equity curves, drawdown analysis, and statistical metrics.
Generates all charts and summary for the results folder.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns


def generate_all(
    trades_df: pd.DataFrame,
    results_dir: Path,
    config_dict: dict | None = None,
):
    """Generate all analytics, charts, and reports.

    Args:
        trades_df: DataFrame from BacktestEngine.run()
        results_dir: timestamped results directory
        config_dict: raw config dict for snapshot
    """
    charts_dir = results_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    if trades_df.empty:
        print("  No trades to analyze!")
        return

    # Ensure date column is proper type
    trades_df["date"] = pd.to_datetime(trades_df["date"])

    # Generate all outputs
    summary = compute_summary(trades_df)
    save_summary(summary, results_dir)
    save_trades(trades_df, results_dir)

    plot_equity_curve(trades_df, charts_dir)
    plot_drawdown(trades_df, charts_dir)
    plot_monthly_heatmap(trades_df, charts_dir)
    plot_dte_breakdown(trades_df, charts_dir)
    plot_exit_reasons(trades_df, charts_dir)
    plot_yearly_summary(trades_df, charts_dir)

    generate_report(trades_df, summary, results_dir)

    print(f"\n  Results saved to: {results_dir}")
    print(f"  Total Trades: {summary['total_trades']}")
    print(f"  Net P&L: Rs {summary['net_pnl']:,.2f}")
    print(f"  Win Rate: {summary['win_rate']:.1f}%")
    print(f"  Profit Factor: {summary['profit_factor']:.2f}")
    print(f"  Max Drawdown: Rs {summary['max_drawdown']:,.2f}")


# ── Summary Metrics ──────────────────────────────────────────────────────────

def compute_summary(df: pd.DataFrame) -> dict:
    """Compute comprehensive backtest metrics."""
    total_trades = len(df)
    winners = df[df["net_pnl"] > 0]
    losers = df[df["net_pnl"] <= 0]

    gross_profit = winners["net_pnl"].sum() if len(winners) > 0 else 0
    gross_loss = abs(losers["net_pnl"].sum()) if len(losers) > 0 else 0

    # Equity curve for drawdown
    equity = df["net_pnl"].cumsum()
    peak = equity.cummax()
    drawdown = equity - peak
    max_dd = drawdown.min()
    max_dd_idx = drawdown.idxmin() if len(drawdown) > 0 else None
    max_dd_date = str(df.loc[max_dd_idx, "date"].date()) if max_dd_idx is not None else ""

    # Daily aggregation
    daily_pnl = df.groupby(df["date"].dt.date)["net_pnl"].sum()
    profitable_days = (daily_pnl > 0).sum()
    total_days = len(daily_pnl)

    # Monthly returns
    df_copy = df.copy()
    df_copy["year_month"] = df_copy["date"].dt.to_period("M")
    monthly_pnl = df_copy.groupby("year_month")["net_pnl"].sum()

    # Sharpe ratio (annualized, daily)
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = (daily_pnl.mean() / daily_pnl.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Calmar ratio
    calmar = (equity.iloc[-1] / abs(max_dd)) if max_dd != 0 else 0.0

    return {
        "total_trades": total_trades,
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": (len(winners) / total_trades * 100) if total_trades > 0 else 0,
        "gross_pnl": round(df["gross_pnl"].sum(), 2),
        "total_charges": round(df["charges"].sum(), 2),
        "net_pnl": round(df["net_pnl"].sum(), 2),
        "avg_win": round(winners["net_pnl"].mean(), 2) if len(winners) > 0 else 0,
        "avg_loss": round(losers["net_pnl"].mean(), 2) if len(losers) > 0 else 0,
        "largest_win": round(winners["net_pnl"].max(), 2) if len(winners) > 0 else 0,
        "largest_loss": round(losers["net_pnl"].min(), 2) if len(losers) > 0 else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_date": max_dd_date,
        "sharpe_ratio": round(sharpe, 2),
        "calmar_ratio": round(calmar, 2),
        "avg_trade_duration_min": round(df["duration_min"].mean(), 1),
        "total_trading_days": total_days,
        "profitable_days": str(profitable_days),
        "profitable_days_pct": round(profitable_days / total_days * 100, 1) if total_days > 0 else 0,
        "avg_daily_pnl": round(daily_pnl.mean(), 2),
        "best_month": str(monthly_pnl.idxmax()) if len(monthly_pnl) > 0 else "",
        "worst_month": str(monthly_pnl.idxmin()) if len(monthly_pnl) > 0 else "",
        "best_month_pnl": round(monthly_pnl.max(), 2) if len(monthly_pnl) > 0 else 0,
        "worst_month_pnl": round(monthly_pnl.min(), 2) if len(monthly_pnl) > 0 else 0,
        "reentry_trades": int(df["is_reentry"].sum()),
        "avg_combined_premium": round(df["combined_premium"].mean(), 2),
    }


# ── Save Functions ───────────────────────────────────────────────────────────

def save_summary(summary: dict, results_dir: Path):
    with open(results_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)


def save_trades(df: pd.DataFrame, results_dir: Path):
    df.to_csv(results_dir / "trades.csv", index=False)


# ── Charts ───────────────────────────────────────────────────────────────────

def plot_equity_curve(df: pd.DataFrame, charts_dir: Path):
    """Cumulative P&L equity curve."""
    fig, ax = plt.subplots(figsize=(14, 6))
    equity = df["net_pnl"].cumsum()
    dates = df["date"]

    ax.plot(dates, equity, linewidth=1.5, color="#2196F3")
    ax.fill_between(dates, 0, equity, where=equity >= 0, alpha=0.15, color="#4CAF50")
    ax.fill_between(dates, 0, equity, where=equity < 0, alpha=0.15, color="#F44336")
    ax.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")

    ax.set_title("Equity Curve (Net P&L after charges)", fontsize=14, fontweight="bold")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative P&L (Rs)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"Rs {x:,.0f}"))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "equity_curve.png", dpi=150)
    plt.close(fig)


def plot_drawdown(df: pd.DataFrame, charts_dir: Path):
    """Drawdown chart."""
    fig, ax = plt.subplots(figsize=(14, 5))
    equity = df["net_pnl"].cumsum()
    peak = equity.cummax()
    drawdown = equity - peak
    dates = df["date"]

    ax.fill_between(dates, 0, drawdown, color="#F44336", alpha=0.4)
    ax.plot(dates, drawdown, color="#D32F2F", linewidth=0.8)

    ax.set_title("Drawdown", fontsize=14, fontweight="bold")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (Rs)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"Rs {x:,.0f}"))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "drawdown.png", dpi=150)
    plt.close(fig)


def plot_monthly_heatmap(df: pd.DataFrame, charts_dir: Path):
    """Monthly P&L heatmap (year x month)."""
    df_copy = df.copy()
    df_copy["year"] = df_copy["date"].dt.year
    df_copy["month"] = df_copy["date"].dt.month

    pivot = df_copy.groupby(["year", "month"])["net_pnl"].sum().unstack(fill_value=0)

    # Ensure all months are present
    for m in range(1, 13):
        if m not in pivot.columns:
            pivot[m] = 0.0
    pivot = pivot[sorted(pivot.columns)]

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pivot.columns = month_names

    fig, ax = plt.subplots(figsize=(14, 6))
    sns.heatmap(
        pivot, annot=True, fmt=",.0f", cmap="RdYlGn", center=0,
        linewidths=0.5, ax=ax, cbar_kws={"label": "Net P&L (Rs)"},
    )
    ax.set_title("Monthly P&L Heatmap", fontsize=14, fontweight="bold")
    ax.set_ylabel("Year")
    ax.set_xlabel("Month")
    fig.tight_layout()
    fig.savefig(charts_dir / "monthly_heatmap.png", dpi=150)
    plt.close(fig)


def plot_dte_breakdown(df: pd.DataFrame, charts_dir: Path):
    """P&L breakdown by DTE."""
    dte_stats = df.groupby("dte").agg(
        count=("net_pnl", "count"),
        total_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
        win_rate=("net_pnl", lambda x: (x > 0).mean() * 100),
    ).reset_index()

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in dte_stats["total_pnl"]]
    axes[0].bar(dte_stats["dte"], dte_stats["total_pnl"], color=colors)
    axes[0].set_title("Total P&L by DTE")
    axes[0].set_xlabel("DTE")
    axes[0].set_ylabel("Net P&L (Rs)")
    axes[0].axhline(y=0, color="gray", linewidth=0.5)

    axes[1].bar(dte_stats["dte"], dte_stats["win_rate"], color="#2196F3")
    axes[1].set_title("Win Rate by DTE")
    axes[1].set_xlabel("DTE")
    axes[1].set_ylabel("Win Rate (%)")
    axes[1].set_ylim(0, 100)

    axes[2].bar(dte_stats["dte"], dte_stats["count"], color="#FF9800")
    axes[2].set_title("Trade Count by DTE")
    axes[2].set_xlabel("DTE")
    axes[2].set_ylabel("Trades")

    fig.suptitle("DTE Breakdown", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(charts_dir / "dte_breakdown.png", dpi=150)
    plt.close(fig)


def plot_exit_reasons(df: pd.DataFrame, charts_dir: Path):
    """Exit reason distribution."""
    # Normalize exit reasons (remove dynamic values in parens)
    reasons = df["exit_reason"].apply(lambda x: x.split("(")[0].strip() if "(" in str(x) else str(x))
    reason_counts = reasons.value_counts()

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Pie chart
    axes[0].pie(reason_counts.values, labels=reason_counts.index, autopct="%1.1f%%",
                startangle=90)
    axes[0].set_title("Exit Reason Distribution")

    # P&L by exit reason
    df_copy = df.copy()
    df_copy["reason_clean"] = reasons
    reason_pnl = df_copy.groupby("reason_clean")["net_pnl"].sum().sort_values()
    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in reason_pnl.values]
    axes[1].barh(reason_pnl.index, reason_pnl.values, color=colors)
    axes[1].set_title("P&L by Exit Reason")
    axes[1].set_xlabel("Net P&L (Rs)")

    fig.suptitle("Exit Analysis", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(charts_dir / "exit_reasons.png", dpi=150)
    plt.close(fig)


def plot_yearly_summary(df: pd.DataFrame, charts_dir: Path):
    """Year-wise summary bar chart."""
    df_copy = df.copy()
    df_copy["year"] = df_copy["date"].dt.year

    yearly = df_copy.groupby("year").agg(
        net_pnl=("net_pnl", "sum"),
        trades=("net_pnl", "count"),
        win_rate=("net_pnl", lambda x: (x > 0).mean() * 100),
        charges=("charges", "sum"),
    ).reset_index()

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))

    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in yearly["net_pnl"]]
    axes[0].bar(yearly["year"].astype(str), yearly["net_pnl"], color=colors)
    axes[0].set_title("Net P&L by Year")
    axes[0].set_ylabel("Net P&L (Rs)")
    for i, v in enumerate(yearly["net_pnl"]):
        axes[0].text(i, v, f"Rs {v:,.0f}", ha="center",
                     va="bottom" if v >= 0 else "top", fontsize=8)

    axes[1].bar(yearly["year"].astype(str), yearly["win_rate"], color="#2196F3")
    axes[1].set_title("Win Rate by Year")
    axes[1].set_ylabel("Win Rate (%)")
    axes[1].set_ylim(0, 100)

    axes[2].bar(yearly["year"].astype(str), yearly["charges"], color="#FF9800")
    axes[2].set_title("Total Charges by Year")
    axes[2].set_ylabel("Charges (Rs)")

    fig.suptitle("Yearly Summary", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(charts_dir / "yearly_summary.png", dpi=150)
    plt.close(fig)


# ── Report Generation ────────────────────────────────────────────────────────

def generate_report(df: pd.DataFrame, summary: dict, results_dir: Path):
    """Generate human-readable markdown report."""
    df_copy = df.copy()
    df_copy["year"] = df_copy["date"].dt.year
    df_copy["month"] = df_copy["date"].dt.month

    # Capital info
    capital = 250000  # default
    has_lot_info = "lot_size" in df.columns and "number_of_lots" in df.columns

    # Yearly table with gross, charges, net, lot info
    yearly = df_copy.groupby("year").agg(
        trades=("net_pnl", "count"),
        gross_pnl=("gross_pnl", "sum"),
        charges=("charges", "sum"),
        net_pnl=("net_pnl", "sum"),
        win_rate=("net_pnl", lambda x: f"{(x > 0).mean() * 100:.1f}%"),
        avg_pnl=("net_pnl", "mean"),
    ).reset_index()

    if has_lot_info:
        lot_info_yearly = df_copy.groupby("year").agg(
            avg_lot_size=("lot_size", "mean"),
            avg_lots=("number_of_lots", "mean"),
            avg_qty=("qty", "mean"),
        ).reset_index()
        yearly = yearly.merge(lot_info_yearly, on="year")
        yearly_table = "| Year | Trades | Gross P&L | Charges | Net P&L | Win Rate | Avg Lots | Lot Size | Avg Qty |\n"
        yearly_table += "|------|--------|-----------|---------|---------|----------|----------|----------|----------|\n"
        for _, row in yearly.iterrows():
            yearly_table += (f"| {row['year']} | {row['trades']} | "
                            f"Rs {row['gross_pnl']:,.0f} | Rs {row['charges']:,.0f} | "
                            f"Rs {row['net_pnl']:,.0f} | {row['win_rate']} | "
                            f"{row['avg_lots']:.1f} | {row['avg_lot_size']:.0f} | {row['avg_qty']:.0f} |\n")
    else:
        yearly_table = "| Year | Trades | Gross P&L | Charges | Net P&L | Win Rate | Avg P&L |\n"
        yearly_table += "|------|--------|-----------|---------|---------|----------|----------|\n"
        for _, row in yearly.iterrows():
            yearly_table += (f"| {row['year']} | {row['trades']} | "
                            f"Rs {row['gross_pnl']:,.0f} | Rs {row['charges']:,.0f} | "
                            f"Rs {row['net_pnl']:,.0f} | {row['win_rate']} | "
                            f"Rs {row['avg_pnl']:,.0f} |\n")

    # Monthly detailed table
    monthly_detail = df_copy.groupby(["year", "month"]).agg(
        trades=("net_pnl", "count"),
        gross_pnl=("gross_pnl", "sum"),
        charges=("charges", "sum"),
        net_pnl=("net_pnl", "sum"),
        win_rate=("net_pnl", lambda x: f"{(x > 0).mean() * 100:.1f}%"),
    ).reset_index()

    month_names = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                   7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}

    monthly_table = "| Year | Month | Trades | Gross P&L | Charges | Net P&L | Win Rate |\n"
    monthly_table += "|------|-------|--------|-----------|---------|---------|----------|\n"
    for _, row in monthly_detail.iterrows():
        mname = month_names.get(row["month"], str(row["month"]))
        monthly_table += (f"| {row['year']} | {mname} | {row['trades']} | "
                         f"Rs {row['gross_pnl']:,.0f} | Rs {row['charges']:,.0f} | "
                         f"Rs {row['net_pnl']:,.0f} | {row['win_rate']} |\n")

    # Capital allocation section
    capital_section = f"""## Capital & Lot Allocation

| Parameter | Value |
|-----------|-------|
| Starting Capital | Rs {capital:,.0f} |
| Net P&L (5 years) | Rs {summary['net_pnl']:,.2f} |
| Total Return | {(summary['net_pnl'] / capital * 100):.1f}% |
| CAGR (approx) | {((1 + summary['net_pnl'] / capital) ** (1/5) - 1) * 100:.1f}% |
| Total Charges (Tax+Brokerage) | Rs {summary['total_charges']:,.2f} |
| Charges as % of Gross | {(summary['total_charges'] / summary['gross_pnl'] * 100):.1f}% |
"""

    if has_lot_info:
        capital_section += f"""
**SEBI Lot Size History:**
- Apr 2021 - Nov 2024: Lot size = 25
- Nov 20, 2024 - Jan 2026: Lot size = 75
- Jan 2026 onwards: Lot size = 65 (current production)

**Dynamic Allocation:** Capital-based lot sizing with 9% SPAN margin + 20% buffer
"""

    report = f"""# Backtest Report — Nifty Short Straddle

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## Key Metrics

| Metric | Value |
|--------|-------|
| Total Trades | {summary['total_trades']} |
| Win Rate | {summary['win_rate']:.1f}% |
| Gross P&L | Rs {summary['gross_pnl']:,.2f} |
| Total Charges (Brokerage + Tax) | Rs {summary['total_charges']:,.2f} |
| Net P&L (after charges) | Rs {summary['net_pnl']:,.2f} |
| Profit Factor | {summary['profit_factor']:.2f} |
| Max Drawdown | Rs {summary['max_drawdown']:,.2f} |
| Max DD Date | {summary['max_drawdown_date']} |
| Sharpe Ratio | {summary['sharpe_ratio']:.2f} |
| Calmar Ratio | {summary['calmar_ratio']:.2f} |
| Avg Trade Duration | {summary['avg_trade_duration_min']:.0f} min |
| Profitable Days | {summary['profitable_days']}/{summary['total_trading_days']} ({summary['profitable_days_pct']:.1f}%) |
| Re-entry Trades | {summary['reentry_trades']} |

{capital_section}

## Yearly Breakdown

{yearly_table}

## Monthly Breakdown

{monthly_table}

## Win/Loss Stats

| Metric | Value |
|--------|-------|
| Avg Win | Rs {summary['avg_win']:,.2f} |
| Avg Loss | Rs {summary['avg_loss']:,.2f} |
| Largest Win | Rs {summary['largest_win']:,.2f} |
| Largest Loss | Rs {summary['largest_loss']:,.2f} |
| Avg Combined Premium | Rs {summary['avg_combined_premium']:,.2f} |
| Best Month | {summary['best_month']} (Rs {summary['best_month_pnl']:,.2f}) |
| Worst Month | {summary['worst_month']} (Rs {summary['worst_month_pnl']:,.2f}) |

## Charges Breakdown

| Component | Description |
|-----------|-------------|
| Brokerage | Rs 20 per order (Dhan flat fee) |
| STT | 0.0625% on sell side |
| Exchange Txn | 0.053% (NSE F&O) |
| SEBI Fee | 0.0001% turnover |
| GST | 18% on brokerage + exchange + SEBI |
| Stamp Duty | 0.003% on buy side |

## Charts

- [Equity Curve](charts/equity_curve.png)
- [Drawdown](charts/drawdown.png)
- [Monthly Heatmap](charts/monthly_heatmap.png)
- [DTE Breakdown](charts/dte_breakdown.png)
- [Exit Reasons](charts/exit_reasons.png)
- [Yearly Summary](charts/yearly_summary.png)
"""

    with open(results_dir / "report.md", "w") as f:
        f.write(report)
