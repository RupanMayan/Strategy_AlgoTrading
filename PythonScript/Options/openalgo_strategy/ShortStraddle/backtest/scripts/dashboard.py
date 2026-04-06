"""
Nifty Short Straddle — Interactive HTML Dashboard Generator
Generates a single self-contained index.html with all charts and trade data.
Uses Plotly.js (CDN) for interactive charts and vanilla JS for trade table.
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd


def generate_dashboard(trades_df: pd.DataFrame, summary: dict, results_dir: Path,
                       config_dict: dict | None = None):
    """Generate interactive HTML dashboard from backtest results."""
    df = trades_df.copy()
    df["date"] = pd.to_datetime(df["date"])

    # ── Prepare data series ──────────────────────────────────────────────────
    # Equity curve
    equity = df["net_pnl"].cumsum().tolist()
    trade_dates = df["date"].dt.strftime("%Y-%m-%d").tolist()

    # Drawdown
    eq_series = pd.Series(equity)
    peak = eq_series.cummax()
    drawdown = (eq_series - peak).tolist()

    # Daily P&L
    daily = df.groupby(df["date"].dt.date)["net_pnl"].sum().reset_index()
    daily.columns = ["date", "pnl"]
    daily_dates = [str(d) for d in daily["date"]]
    daily_pnl = daily["pnl"].round(2).tolist()
    daily_colors = ["#22c55e" if p > 0 else "#ef4444" for p in daily_pnl]

    # Monthly heatmap data
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    monthly = df.groupby(["year", "month"])["net_pnl"].sum().reset_index()
    years = sorted(monthly["year"].unique())
    months = list(range(1, 13))
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Total"]

    def _fmt_heatmap_val(val):
        abs_val = abs(float(val))
        sign = "-" if val < 0 else ""
        if abs_val >= 100_000:
            return f"{sign}{abs_val/100_000:.1f}L"
        elif abs_val >= 1000:
            return f"{sign}{abs_val/1000:.1f}K"
        else:
            return f"{sign}{abs_val:.0f}"

    heatmap_z = []
    heatmap_text = []
    for y in years:
        row = []
        text_row = []
        year_total = 0.0
        for m in months:
            val = monthly[(monthly["year"] == y) & (monthly["month"] == m)]["net_pnl"].sum()
            if val != 0:
                row.append(round(float(val), 2))
                text_row.append(_fmt_heatmap_val(val))
                year_total += float(val)
            else:
                row.append(None)
                text_row.append("")
        # Append yearly total column
        row.append(round(year_total, 2))
        text_row.append(_fmt_heatmap_val(year_total))
        heatmap_z.append(row)
        heatmap_text.append(text_row)

    # Exit reasons
    exit_counts = df["exit_reason"].apply(
        lambda x: x.split("(")[0].strip() if "(" in str(x) else str(x)
    ).value_counts()
    exit_labels = exit_counts.index.tolist()
    exit_values = exit_counts.values.tolist()

    # DTE breakdown
    dte_stats = df.groupby("dte").agg(
        trades=("net_pnl", "count"),
        net_pnl=("net_pnl", "sum"),
        win_rate=("net_pnl", lambda x: (x > 0).mean() * 100),
    ).reset_index()
    dte_labels = [f"DTE {int(d)}" for d in dte_stats["dte"]]
    dte_pnl = dte_stats["net_pnl"].round(2).tolist()
    dte_trades = dte_stats["trades"].tolist()
    dte_winrate = dte_stats["win_rate"].round(1).tolist()

    # Yearly summary
    yearly = df.groupby("year").agg(
        trades=("net_pnl", "count"),
        gross=("gross_pnl", "sum"),
        charges=("charges", "sum"),
        net=("net_pnl", "sum"),
        win_rate=("net_pnl", lambda x: (x > 0).mean() * 100),
    ).reset_index()
    yearly_labels = [str(int(y)) for y in yearly["year"]]
    yearly_net = yearly["net"].round(2).tolist()
    yearly_gross = yearly["gross"].round(2).tolist()
    yearly_charges = yearly["charges"].round(2).tolist()

    # Trade table (JSON for DataTable)
    trade_rows = []
    for _, row in df.iterrows():
        trade_rows.append({
            "date": str(row["date"].date()),
            "entry_time": str(row["entry_time"])[11:16] if len(str(row["entry_time"])) > 11 else str(row["entry_time"]),
            "exit_time": str(row["exit_time"])[11:16] if len(str(row["exit_time"])) > 11 else str(row["exit_time"]),
            "duration": round(row["duration_min"], 0),
            "ce_entry": round(row["entry_price_ce"], 2),
            "pe_entry": round(row["entry_price_pe"], 2),
            "ce_exit": round(row["exit_price_ce"], 2),
            "pe_exit": round(row["exit_price_pe"], 2),
            "premium": round(row["combined_premium"], 2),
            "gross": round(row["gross_pnl"], 2),
            "charges": round(row["charges"], 2),
            "net": round(row["net_pnl"], 2),
            "exit_reason": str(row["exit_reason"]),
            "dte": int(row["dte"]),
            "lots": int(row["number_of_lots"]),
            "lot_size": int(row["lot_size"]),
            "qty": int(row["qty"]),
            "capital": round(row["capital_used"], 0),
            "reentry": bool(row["is_reentry"]),
            "underlying": round(row["underlying_at_entry"], 2),
            "vix": round(row["vix_at_entry"], 2),
        })

    # ── Extra metrics ────────────────────────────────────────────────────────
    # Max DD duration (days)
    eq_s = pd.Series(equity)
    pk = eq_s.cummax()
    in_dd = eq_s < pk
    dd_groups = (~in_dd).cumsum()
    if in_dd.any():
        dd_durations = in_dd.groupby(dd_groups).sum()
        max_dd_duration = int(dd_durations.max())
    else:
        max_dd_duration = 0

    # Max consecutive wins/losses
    is_win = (df["net_pnl"] > 0).astype(int)
    streaks = is_win.diff().ne(0).cumsum()
    win_streaks = is_win.groupby(streaks).sum()
    loss_groups = (1 - is_win).groupby(streaks).sum()
    max_consec_wins = int(win_streaks.max()) if len(win_streaks) > 0 else 0
    max_consec_losses = int(loss_groups.max()) if len(loss_groups) > 0 else 0

    # Avg trade P&L
    avg_trade_pnl = float(df["net_pnl"].mean())

    # Expectancy
    win_rate_frac = summary["win_rate"] / 100
    expectancy = (win_rate_frac * summary["avg_win"]) + ((1 - win_rate_frac) * summary["avg_loss"])

    # Recovery factor
    recovery_factor = abs(summary["net_pnl"] / summary["max_drawdown"]) if summary["max_drawdown"] != 0 else 0

    extra_metrics = {
        "calmar_ratio": summary.get("calmar_ratio", 0),
        "max_dd_duration": max_dd_duration,
        "avg_trade_pnl": round(avg_trade_pnl, 2),
        "expectancy": round(expectancy, 2),
        "max_consec_wins": max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "recovery_factor": round(recovery_factor, 2),
    }

    # Config info
    is_compounded = config_dict and config_dict.get("instrument", {}).get("compound_capital", False)
    mode = "Compounded" if is_compounded else "Fixed Capital"
    capital = config_dict.get("instrument", {}).get("capital", 250000) if config_dict else 250000

    # ── Build HTML ───────────────────────────────────────────────────────────
    html = _build_html(
        summary=summary,
        mode=mode,
        capital=capital,
        equity=equity,
        trade_dates=trade_dates,
        drawdown=drawdown,
        daily_dates=daily_dates,
        daily_pnl=daily_pnl,
        daily_colors=daily_colors,
        heatmap_z=heatmap_z,
        heatmap_text=heatmap_text,
        heatmap_years=[str(int(y)) for y in years],
        month_names=month_names,
        exit_labels=exit_labels,
        exit_values=exit_values,
        dte_labels=dte_labels,
        dte_pnl=dte_pnl,
        dte_trades=dte_trades,
        dte_winrate=dte_winrate,
        yearly_labels=yearly_labels,
        yearly_net=yearly_net,
        yearly_gross=yearly_gross,
        yearly_charges=yearly_charges,
        trade_rows=trade_rows,
        extra=extra_metrics,
    )

    out_path = results_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  Dashboard: {out_path}")


def _build_html(**data) -> str:
    s = data["summary"]

    # Format helper
    def fmt(val, prefix="Rs ", decimals=0):
        if val is None:
            return "—"
        sign = "-" if val < 0 else ""
        abs_val = abs(val)
        if abs_val >= 10_000_000:
            return f"{sign}{prefix}{abs_val/10_000_000:,.2f} Cr"
        elif abs_val >= 100_000:
            return f"{sign}{prefix}{abs_val/100_000:,.2f}L"
        else:
            return f"{sign}{prefix}{abs_val:,.{decimals}f}"

    net_pnl = s["net_pnl"]
    net_class = "positive" if net_pnl > 0 else "negative"
    dd_val = s["max_drawdown"]
    capital = data["capital"]
    roi_pct = (net_pnl / capital * 100) if capital > 0 else 0
    cagr_pct = ((1 + net_pnl / capital) ** (1 / 5) - 1) * 100 if capital > 0 else 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nifty Short Straddle — Backtest Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root {{
    --bg: #0f1117;
    --card: #1a1d28;
    --border: #2a2d3a;
    --text: #e1e4ea;
    --muted: #8b8fa3;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --purple: #a855f7;
    --orange: #f59e0b;
    --cyan: #06b6d4;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 16px;
}}
h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
.subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; }}
.metrics-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
}}
.metric-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
}}
.metric-card .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
.metric-card .value {{ font-size: 20px; font-weight: 700; margin-top: 2px; }}
.metric-card .value.positive {{ color: var(--green); }}
.metric-card .value.negative {{ color: var(--red); }}
.metric-card .value.neutral {{ color: var(--blue); }}
.charts-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 16px;
}}
.chart-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    min-height: 300px;
}}
.chart-card.full {{ grid-column: 1 / -1; }}
.chart-card h3 {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.trade-section {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 16px;
}}
.trade-section h3 {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.controls {{ display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; align-items: center; }}
.controls input, .controls select {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
}}
.controls input {{ width: 200px; }}
.controls select {{ min-width: 120px; }}
.controls label {{ font-size: 12px; color: var(--muted); }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
thead th {{
    position: sticky; top: 0;
    background: var(--bg);
    color: var(--muted);
    font-weight: 600;
    text-align: left;
    padding: 6px 8px;
    border-bottom: 2px solid var(--border);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}}
thead th:hover {{ color: var(--text); }}
thead th.sorted-asc::after {{ content: ' ▲'; }}
thead th.sorted-desc::after {{ content: ' ▼'; }}
tbody td {{
    padding: 5px 8px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}}
tbody tr:hover {{ background: rgba(59, 130, 246, 0.08); }}
.pnl-pos {{ color: var(--green); font-weight: 600; }}
.pnl-neg {{ color: var(--red); font-weight: 600; }}
.tag {{
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: 600;
}}
.tag-reentry {{ background: rgba(168, 85, 247, 0.2); color: var(--purple); }}
.table-wrap {{ max-height: 500px; overflow-y: auto; }}
.page-info {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
.footer {{ text-align: center; color: var(--muted); font-size: 11px; padding: 16px 0; }}
@media (max-width: 768px) {{
    .charts-grid {{ grid-template-columns: 1fr; }}
    .metrics-grid {{ grid-template-columns: repeat(3, 1fr); }}
}}
</style>
</head>
<body>

<h1>Nifty Short Straddle — Backtest Dashboard</h1>
<div class="subtitle">{data['mode']} | Starting Capital: Rs {data['capital']:,.0f} | Period: {s.get('best_month', '2021-04')[:4]} - {s.get('worst_month', '2026-03')[:4]} | {s['total_trades']} Trades</div>

<div class="metrics-grid">
    <div class="metric-card">
        <div class="label">Net P&L</div>
        <div class="value {net_class}">{fmt(net_pnl)}</div>
    </div>
    <div class="metric-card">
        <div class="label">Total Return (ROI)</div>
        <div class="value {'positive' if roi_pct > 0 else 'negative'}">{roi_pct:.1f}%</div>
    </div>
    <div class="metric-card">
        <div class="label">CAGR (5yr)</div>
        <div class="value {'positive' if cagr_pct > 0 else 'negative'}">{cagr_pct:.1f}%</div>
    </div>
    <div class="metric-card">
        <div class="label">Win Rate</div>
        <div class="value neutral">{s['win_rate']:.1f}%</div>
    </div>
    <div class="metric-card">
        <div class="label">Profit Factor</div>
        <div class="value neutral">{s['profit_factor']:.2f}</div>
    </div>
    <div class="metric-card">
        <div class="label">Sharpe Ratio</div>
        <div class="value neutral">{s['sharpe_ratio']:.2f}</div>
    </div>
    <div class="metric-card">
        <div class="label">Max Drawdown</div>
        <div class="value negative">{fmt(dd_val)}</div>
    </div>
    <div class="metric-card">
        <div class="label">Gross P&L</div>
        <div class="value positive">{fmt(s['gross_pnl'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Total Charges</div>
        <div class="value negative">{fmt(-s['total_charges'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Avg Daily P&L</div>
        <div class="value {'positive' if s['avg_daily_pnl'] > 0 else 'negative'}">{fmt(s['avg_daily_pnl'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Profitable Days</div>
        <div class="value neutral">{s['profitable_days']}/{s['total_trading_days']} ({s['profitable_days_pct']:.0f}%)</div>
    </div>
    <div class="metric-card">
        <div class="label">Avg Win / Loss</div>
        <div class="value neutral">{fmt(s['avg_win'])} / {fmt(s['avg_loss'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Best Month</div>
        <div class="value positive">{s['best_month']} ({fmt(s['best_month_pnl'])})</div>
    </div>
    <div class="metric-card">
        <div class="label">Worst Month</div>
        <div class="value negative">{s['worst_month']} ({fmt(s['worst_month_pnl'])})</div>
    </div>
    <div class="metric-card">
        <div class="label">Calmar Ratio</div>
        <div class="value neutral">{data['extra']['calmar_ratio']:.2f}</div>
    </div>
    <div class="metric-card">
        <div class="label">Recovery Factor</div>
        <div class="value neutral">{data['extra']['recovery_factor']:.2f}</div>
    </div>
    <div class="metric-card">
        <div class="label">Avg Trade P&L</div>
        <div class="value {'positive' if data['extra']['avg_trade_pnl'] > 0 else 'negative'}">{fmt(data['extra']['avg_trade_pnl'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Expectancy / Trade</div>
        <div class="value {'positive' if data['extra']['expectancy'] > 0 else 'negative'}">{fmt(data['extra']['expectancy'])}</div>
    </div>
    <div class="metric-card">
        <div class="label">Max Consec Wins</div>
        <div class="value positive">{data['extra']['max_consec_wins']}</div>
    </div>
    <div class="metric-card">
        <div class="label">Max Consec Losses</div>
        <div class="value negative">{data['extra']['max_consec_losses']}</div>
    </div>
    <div class="metric-card">
        <div class="label">Max DD Duration</div>
        <div class="value negative">{data['extra']['max_dd_duration']} trades</div>
    </div>
</div>

<div class="charts-grid">
    <div class="chart-card full"><h3>Equity Curve</h3><div id="equity"></div></div>
    <div class="chart-card full"><h3>Drawdown</h3><div id="drawdown"></div></div>
    <div class="chart-card full"><h3>Daily P&L</h3><div id="daily"></div></div>
    <div class="chart-card full"><h3>Monthly P&L Heatmap</h3><div id="heatmap"></div></div>
    <div class="chart-card"><h3>Exit Reasons</h3><div id="exits"></div></div>
    <div class="chart-card"><h3>DTE Breakdown — Net P&L</h3><div id="dte"></div></div>
    <div class="chart-card full"><h3>Yearly Summary</h3><div id="yearly"></div></div>
</div>

<div class="trade-section">
    <h3>Trade Log ({s['total_trades']} trades)</h3>
    <div class="controls">
        <label>From</label><input type="date" id="dateFrom">
        <label>To</label><input type="date" id="dateTo">
        <input type="text" id="search" placeholder="Search (date, exit reason...)">
        <select id="filterExit"><option value="">All Exit Reasons</option></select>
        <select id="filterDTE"><option value="">All DTE</option></select>
        <select id="filterPnl">
            <option value="">All Trades</option>
            <option value="win">Winners Only</option>
            <option value="loss">Losers Only</option>
        </select>
        <label><input type="checkbox" id="filterReentry"> Re-entries only</label>
    </div>
    <div class="table-wrap" id="tableWrap">
        <table>
            <thead><tr>
                <th data-col="date">Date</th>
                <th data-col="entry_time">Entry</th>
                <th data-col="exit_time">Exit</th>
                <th data-col="duration">Dur(m)</th>
                <th data-col="ce_entry">CE In</th>
                <th data-col="pe_entry">PE In</th>
                <th data-col="ce_exit">CE Out</th>
                <th data-col="pe_exit">PE Out</th>
                <th data-col="premium">Premium</th>
                <th data-col="lots">Lots</th>
                <th data-col="qty">Qty</th>
                <th data-col="gross">Gross</th>
                <th data-col="charges">Charges</th>
                <th data-col="net">Net P&L</th>
                <th data-col="exit_reason">Exit Reason</th>
                <th data-col="dte">DTE</th>
                <th data-col="underlying">Spot</th>
                <th data-col="vix">VIX</th>
                <th data-col="capital">Capital</th>
            </tr></thead>
            <tbody id="tbody"></tbody>
        </table>
    </div>
    <div class="page-info" id="pageInfo"></div>
</div>

<div class="footer">
    Generated by Nifty Short Straddle Backtest Engine — {data['mode']}
</div>

<script>
const PLOTLY_LAYOUT = {{
    paper_bgcolor: '#1a1d28',
    plot_bgcolor: '#1a1d28',
    font: {{ color: '#8b8fa3', size: 11 }},
    margin: {{ l: 50, r: 20, t: 10, b: 40 }},
    xaxis: {{ gridcolor: '#2a2d3a', zerolinecolor: '#2a2d3a' }},
    yaxis: {{ gridcolor: '#2a2d3a', zerolinecolor: '#2a2d3a' }},
    hoverlabel: {{ bgcolor: '#1a1d28', bordercolor: '#3b82f6', font: {{ color: '#e1e4ea' }} }},
}};
const CFG = {{ responsive: true, displayModeBar: false }};

// Equity Curve
Plotly.newPlot('equity', [{{
    x: {json.dumps(data['trade_dates'])},
    y: {json.dumps(data['equity'])},
    type: 'scatter', mode: 'lines',
    fill: 'tozeroy',
    fillcolor: 'rgba(34,197,94,0.1)',
    line: {{ color: '#22c55e', width: 1.5 }},
    hovertemplate: '%{{x}}<br>Cumulative P&L: Rs %{{y:,.0f}}<extra></extra>',
}}], {{...PLOTLY_LAYOUT, yaxis: {{...PLOTLY_LAYOUT.yaxis, title: 'Cumulative P&L (Rs)'}}}}, CFG);

// Drawdown
Plotly.newPlot('drawdown', [{{
    x: {json.dumps(data['trade_dates'])},
    y: {json.dumps(data['drawdown'])},
    type: 'scatter', mode: 'lines',
    fill: 'tozeroy',
    fillcolor: 'rgba(239,68,68,0.15)',
    line: {{ color: '#ef4444', width: 1.5 }},
    hovertemplate: '%{{x}}<br>Drawdown: Rs %{{y:,.0f}}<extra></extra>',
}}], {{...PLOTLY_LAYOUT, yaxis: {{...PLOTLY_LAYOUT.yaxis, title: 'Drawdown (Rs)'}}}}, CFG);

// Daily P&L
Plotly.newPlot('daily', [{{
    x: {json.dumps(data['daily_dates'])},
    y: {json.dumps(data['daily_pnl'])},
    type: 'bar',
    marker: {{ color: {json.dumps(data['daily_colors'])} }},
    hovertemplate: '%{{x}}<br>Daily P&L: Rs %{{y:,.0f}}<extra></extra>',
}}], {{...PLOTLY_LAYOUT, yaxis: {{...PLOTLY_LAYOUT.yaxis, title: 'Daily Net P&L (Rs)', zeroline: true, zerolinewidth: 1}}}}, CFG);

// Monthly Heatmap
// Split monthly data (cols 0-11) from total (col 12) for independent color scales
const hmZ = {json.dumps(data['heatmap_z'])};
const hmText = {json.dumps(data['heatmap_text'])};
const hmMonthZ = hmZ.map(r => r.slice(0, 12));
const hmMonthText = hmText.map(r => r.slice(0, 12));
const hmTotalZ = hmZ.map(r => [r[12]]);
const hmTotalText = hmText.map(r => [r[12]]);

// Monthly cells — color scale based on monthly range only
const monthFlat = hmMonthZ.flat().filter(v => v !== null);
const monthMax = Math.max(...monthFlat.map(Math.abs), 1);

Plotly.newPlot('heatmap', [
    {{
        z: hmMonthZ,
        x: {json.dumps(data['month_names'][:12])},
        y: {json.dumps(data['heatmap_years'])},
        text: hmMonthText,
        texttemplate: '%{{text}}',
        textfont: {{ size: 11, color: '#e1e4ea' }},
        type: 'heatmap',
        colorscale: [[0, '#dc2626'], [0.35, '#fca5a5'], [0.5, '#1e2030'], [0.65, '#86efac'], [1, '#16a34a']],
        zmin: -monthMax, zmax: monthMax,
        hovertemplate: '%{{y}} %{{x}}<br>Net P&L: Rs %{{z:,.0f}}<extra></extra>',
        colorbar: {{ title: 'P&L', tickformat: ',.0f', len: 0.9 }},
        xaxis: 'x',
    }},
    {{
        z: hmTotalZ,
        x: ['Total'],
        y: {json.dumps(data['heatmap_years'])},
        text: hmTotalText,
        texttemplate: '<b>%{{text}}</b>',
        textfont: {{ size: 12, color: '#e1e4ea' }},
        type: 'heatmap',
        colorscale: [[0, '#dc2626'], [0.5, '#1e2030'], [1, '#16a34a']],
        zmid: 0,
        hovertemplate: '%{{y}} Total<br>Net P&L: Rs %{{z:,.0f}}<extra></extra>',
        showscale: false,
        xaxis: 'x2',
    }},
], {{
    ...PLOTLY_LAYOUT,
    grid: {{ rows: 1, columns: 2, pattern: 'independent' }},
    xaxis: {{ ...PLOTLY_LAYOUT.xaxis, type: 'category', domain: [0, 0.88] }},
    xaxis2: {{ ...PLOTLY_LAYOUT.xaxis, type: 'category', domain: [0.90, 1.0] }},
    yaxis: {{ ...PLOTLY_LAYOUT.yaxis, autorange: 'reversed', type: 'category' }},
    yaxis2: {{ ...PLOTLY_LAYOUT.yaxis, autorange: 'reversed', type: 'category', showticklabels: false }},
}}, CFG);

// Exit Reasons Pie
Plotly.newPlot('exits', [{{
    labels: {json.dumps(data['exit_labels'])},
    values: {json.dumps(data['exit_values'])},
    type: 'pie',
    hole: 0.45,
    textinfo: 'percent+label',
    textfont: {{ size: 10, color: '#e1e4ea' }},
    marker: {{ colors: ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7', '#06b6d4', '#ec4899', '#8b5cf6', '#14b8a6', '#f97316'] }},
    hovertemplate: '%{{label}}<br>%{{value}} trades (%{{percent}})<extra></extra>',
}}], {{...PLOTLY_LAYOUT, margin: {{ l: 10, r: 10, t: 10, b: 10 }}, showlegend: false}}, CFG);

// DTE Breakdown
Plotly.newPlot('dte', [{{
    x: {json.dumps(data['dte_labels'])},
    y: {json.dumps(data['dte_pnl'])},
    type: 'bar',
    marker: {{ color: {json.dumps(data['dte_pnl'])}.map(v => v > 0 ? '#22c55e' : '#ef4444') }},
    text: {json.dumps(data['dte_trades'])}.map((t, i) => t + ' trades, ' + {json.dumps(data['dte_winrate'])}[i] + '% WR'),
    hovertemplate: '%{{x}}<br>Net P&L: Rs %{{y:,.0f}}<br>%{{text}}<extra></extra>',
}}], {{...PLOTLY_LAYOUT, xaxis: {{...PLOTLY_LAYOUT.xaxis, type: 'category'}}, yaxis: {{...PLOTLY_LAYOUT.yaxis, title: 'Net P&L (Rs)'}}}}, CFG);

// Yearly Summary
Plotly.newPlot('yearly', [
    {{
        x: {json.dumps(data['yearly_labels'])},
        y: {json.dumps(data['yearly_gross'])},
        name: 'Gross P&L',
        type: 'bar',
        marker: {{ color: 'rgba(59,130,246,0.7)' }},
    }},
    {{
        x: {json.dumps(data['yearly_labels'])},
        y: {json.dumps(data['yearly_charges'])}.map(v => -v),
        name: 'Charges',
        type: 'bar',
        marker: {{ color: 'rgba(239,68,68,0.5)' }},
    }},
    {{
        x: {json.dumps(data['yearly_labels'])},
        y: {json.dumps(data['yearly_net'])},
        name: 'Net P&L',
        type: 'scatter',
        mode: 'lines+markers',
        line: {{ color: '#22c55e', width: 2 }},
        marker: {{ size: 8 }},
    }},
], {{...PLOTLY_LAYOUT, barmode: 'group', xaxis: {{...PLOTLY_LAYOUT.xaxis, type: 'category'}}, yaxis: {{...PLOTLY_LAYOUT.yaxis, title: 'P&L (Rs)'}}, legend: {{font: {{color: '#8b8fa3'}}}}}}, CFG);

// ── Trade Table ──────────────────────────────────────────────────────────
const trades = {json.dumps(data['trade_rows'])};
let sortCol = null, sortAsc = true;
let filtered = [...trades];

const exitReasons = [...new Set(trades.map(t => t.exit_reason.split('(')[0].trim()))].sort();
const dteValues = [...new Set(trades.map(t => t.dte))].sort((a, b) => a - b);

const selExit = document.getElementById('filterExit');
exitReasons.forEach(r => {{ const o = document.createElement('option'); o.value = r; o.textContent = r; selExit.appendChild(o); }});
const selDTE = document.getElementById('filterDTE');
dteValues.forEach(d => {{ const o = document.createElement('option'); o.value = d; o.textContent = 'DTE ' + d; selDTE.appendChild(o); }});

function applyFilters() {{
    const q = document.getElementById('search').value.toLowerCase();
    const exitF = selExit.value;
    const dteF = selDTE.value;
    const pnlF = document.getElementById('filterPnl').value;
    const reentryF = document.getElementById('filterReentry').checked;
    const dateFrom = document.getElementById('dateFrom').value;
    const dateTo = document.getElementById('dateTo').value;

    filtered = trades.filter(t => {{
        if (dateFrom && t.date < dateFrom) return false;
        if (dateTo && t.date > dateTo) return false;
        if (q && !t.date.includes(q) && !t.exit_reason.toLowerCase().includes(q)) return false;
        if (exitF && !t.exit_reason.startsWith(exitF)) return false;
        if (dteF !== '' && t.dte !== parseInt(dteF)) return false;
        if (pnlF === 'win' && t.net <= 0) return false;
        if (pnlF === 'loss' && t.net > 0) return false;
        if (reentryF && !t.reentry) return false;
        return true;
    }});

    if (sortCol) {{
        filtered.sort((a, b) => {{
            let va = a[sortCol], vb = b[sortCol];
            if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
            return sortAsc ? va - vb : vb - va;
        }});
    }}
    renderTable();
}}

function renderTable() {{
    const tbody = document.getElementById('tbody');
    const rows = filtered.map(t => {{
        const cls = t.net > 0 ? 'pnl-pos' : 'pnl-neg';
        const re = t.reentry ? '<span class="tag tag-reentry">RE</span>' : '';
        return `<tr>
            <td>${{t.date}}</td>
            <td>${{t.entry_time}}</td>
            <td>${{t.exit_time}}</td>
            <td>${{t.duration}}</td>
            <td>${{t.ce_entry.toFixed(1)}}</td>
            <td>${{t.pe_entry.toFixed(1)}}</td>
            <td>${{t.ce_exit.toFixed(1)}}</td>
            <td>${{t.pe_exit.toFixed(1)}}</td>
            <td>${{t.premium.toFixed(1)}}</td>
            <td>${{t.lots}}</td>
            <td>${{t.qty}}</td>
            <td class="${{cls}}">Rs ${{t.gross.toLocaleString('en-IN')}}</td>
            <td>Rs ${{t.charges.toFixed(0)}}</td>
            <td class="${{cls}}">Rs ${{t.net.toLocaleString('en-IN')}}</td>
            <td>${{t.exit_reason}} ${{re}}</td>
            <td>${{t.dte}}</td>
            <td>${{t.underlying.toLocaleString('en-IN')}}</td>
            <td>${{t.vix}}</td>
            <td>Rs ${{t.capital.toLocaleString('en-IN')}}</td>
        </tr>`;
    }}).join('');
    tbody.innerHTML = rows;

    const total = filtered.reduce((s, t) => s + t.net, 0);
    const wins = filtered.filter(t => t.net > 0).length;
    const totalCls = total > 0 ? 'pnl-pos' : 'pnl-neg';
    document.getElementById('pageInfo').innerHTML =
        `Showing ${{filtered.length}} / ${{trades.length}} trades | ` +
        `Filtered Net P&L: <span class="${{totalCls}}">Rs ${{total.toLocaleString('en-IN', {{maximumFractionDigits: 0}})}}</span> | ` +
        `Win Rate: ${{filtered.length > 0 ? (wins / filtered.length * 100).toFixed(1) : 0}}%`;
}}

document.querySelectorAll('thead th').forEach(th => {{
    th.addEventListener('click', () => {{
        const col = th.dataset.col;
        if (sortCol === col) {{ sortAsc = !sortAsc; }}
        else {{ sortCol = col; sortAsc = true; }}
        document.querySelectorAll('thead th').forEach(t => t.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
        applyFilters();
    }});
}});

document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('dateFrom').addEventListener('change', applyFilters);
document.getElementById('dateTo').addEventListener('change', applyFilters);
selExit.addEventListener('change', applyFilters);
selDTE.addEventListener('change', applyFilters);
document.getElementById('filterPnl').addEventListener('change', applyFilters);
document.getElementById('filterReentry').addEventListener('change', applyFilters);

applyFilters();
</script>
</body>
</html>"""
