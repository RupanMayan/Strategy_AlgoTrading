# Backtest Report — Nifty Short Straddle (Production Config)

Generated: 2026-03-29
Config: `config_production.toml` (7 institutional risk fixes enabled)
Mode: Fixed capital (Rs 2,50,000, no compounding)

---

## Key Metrics

| Metric | Value |
|--------|-------|
| Period | Apr 2021 - Mar 2026 (1,226 trading days) |
| Total Trades | 1,101 |
| Win Rate | 85.2% |
| Gross P&L | Rs 28,30,520 |
| Total Charges (Brokerage + Tax) | Rs 1,45,833 |
| Net P&L (after charges) | Rs 26,84,686 |
| Profit Factor | 9.38 |
| Max Drawdown | Rs -9,516 |
| Max DD Date | 2025-01-29 |
| Max DD as % of Capital | 3.8% |
| Sharpe Ratio | 12.84 |
| Calmar Ratio | 282.13 |
| Avg Trade Duration | 301 min |
| Profitable Days | 938/1,101 (85.2%) |
| Re-entry Trades | 0 |
| Max Consecutive Loss Days | 3 |
| Worst 5% Avg Loss | Rs -3,842 |

## Capital & Lot Allocation (Fixed)

| Parameter | Value |
|-----------|-------|
| Starting Capital | Rs 2,50,000 |
| Final Capital | Rs 29,34,686 |
| Total Return | 1,074% |
| CAGR (approx) | ~63.7% |
| Total Charges (Tax+Brokerage) | Rs 1,45,833 |
| Charges as % of Gross | 5.2% |
| Avg Daily P&L | Rs 2,438 |

**SEBI Lot Size History:**
- Apr 2021 - Nov 2024: Lot size = 25
- Nov 20, 2024 - Jan 2026: Lot size = 75
- Jan 2026 onwards: Lot size = 65 (current production)

**Dynamic Allocation:** Capital-based lot sizing with 9% SPAN margin + 20% buffer

## Risk Management — 7 Institutional Fixes

All fixes enabled in this backtest, matching production:

| # | Fix | Config Value | Purpose |
|---|-----|-------------|---------|
| 1 | Max Trade Loss | Rs 15,000/lot | Absolute rupee cap per trade |
| 2 | Margin Fail-Closed | `fail_open = false` | Skip entry if margin API fails |
| 3 | VIX Entry Filter | 11.0 - 25.0 | Avoid thin premiums / gamma risk |
| 4 | Spot-Move Exit | 1.0x premium | Close if spot moves beyond premium |
| 5 | Weekly Drawdown Guard | Rs -20,000/lot | Pause after sustained losses |
| 6 | ORB Filter | 0.5% threshold | Skip entry on gap-up/gap-down |
| 7 | Combined SL | 30% rise | Replace per-leg SL when both legs active |

## Yearly Breakdown

| Year | Trades | Gross P&L | Charges | Net P&L | Win Rate | Avg Lots | Lot Size | Avg Qty |
|------|--------|-----------|---------|---------|----------|----------|----------|----------|
| 2021 | 180 | Rs 5,28,852 | Rs 24,434 | Rs 5,04,418 | 86.7% | 5.2 | 25 | 130 |
| 2022 | 229 | Rs 7,10,789 | Rs 32,867 | Rs 6,77,922 | 87.3% | 5.0 | 25 | 124 |
| 2023 | 217 | Rs 5,24,595 | Rs 27,237 | Rs 4,97,358 | 90.3% | 4.4 | 25 | 111 |
| 2024 | 234 | Rs 6,02,876 | Rs 30,719 | Rs 5,72,157 | 82.9% | 3.2 | 31 | 85 |
| 2025 | 198 | Rs 4,10,662 | Rs 24,897 | Rs 3,85,766 | 81.3% | 1.0 | 75 | 75 |
| 2026 | 43 | Rs 52,744 | Rs 5,679 | Rs 47,066 | 72.1% | 1.0 | 65 | 65 |

## Monthly Breakdown

| Year | Month | Trades | Gross P&L | Charges | Net P&L | Win Rate |
|------|-------|--------|-----------|---------|---------|----------|
| 2021 | Apr | 17 | Rs 83,812 | Rs 2,651 | Rs 81,161 | 82.4% |
| 2021 | May | 20 | Rs 69,698 | Rs 2,900 | Rs 66,798 | 95.0% |
| 2021 | Jun | 22 | Rs 53,862 | Rs 2,825 | Rs 51,038 | 81.8% |
| 2021 | Jul | 20 | Rs 42,516 | Rs 2,459 | Rs 40,058 | 95.0% |
| 2021 | Aug | 21 | Rs 37,038 | Rs 2,650 | Rs 34,388 | 85.7% |
| 2021 | Sep | 21 | Rs 58,906 | Rs 2,737 | Rs 56,170 | 90.5% |
| 2021 | Oct | 19 | Rs 50,308 | Rs 2,615 | Rs 47,693 | 89.5% |
| 2021 | Nov | 17 | Rs 54,312 | Rs 2,335 | Rs 51,978 | 82.4% |
| 2021 | Dec | 23 | Rs 78,400 | Rs 3,264 | Rs 75,136 | 78.3% |
| 2022 | Jan | 19 | Rs 52,548 | Rs 2,811 | Rs 49,737 | 84.2% |
| 2022 | Feb | 15 | Rs 74,006 | Rs 2,308 | Rs 71,698 | 86.7% |
| 2022 | Mar | 11 | Rs 47,225 | Rs 1,616 | Rs 45,609 | 81.8% |
| 2022 | Apr | 19 | Rs 61,912 | Rs 2,711 | Rs 59,202 | 84.2% |
| 2022 | May | 20 | Rs 58,612 | Rs 3,103 | Rs 55,509 | 90.0% |
| 2022 | Jun | 22 | Rs 79,215 | Rs 3,320 | Rs 75,895 | 90.9% |
| 2022 | Jul | 21 | Rs 62,725 | Rs 2,958 | Rs 59,767 | 95.2% |
| 2022 | Aug | 20 | Rs 60,672 | Rs 2,786 | Rs 57,887 | 85.0% |
| 2022 | Sep | 21 | Rs 60,894 | Rs 3,145 | Rs 57,749 | 81.0% |
| 2022 | Oct | 18 | Rs 55,919 | Rs 2,542 | Rs 53,376 | 88.9% |
| 2022 | Nov | 21 | Rs 45,742 | Rs 2,713 | Rs 43,029 | 95.2% |
| 2022 | Dec | 22 | Rs 51,318 | Rs 2,854 | Rs 48,463 | 81.8% |
| 2023 | Jan | 21 | Rs 74,900 | Rs 2,911 | Rs 71,989 | 85.7% |
| 2023 | Feb | 20 | Rs 86,844 | Rs 2,677 | Rs 84,167 | 100.0% |
| 2023 | Mar | 21 | Rs 62,156 | Rs 2,775 | Rs 59,382 | 85.7% |
| 2023 | Apr | 16 | Rs 44,262 | Rs 1,942 | Rs 42,321 | 93.8% |
| 2023 | May | 22 | Rs 36,114 | Rs 2,727 | Rs 33,387 | 90.9% |
| 2023 | Jun | 19 | Rs 39,063 | Rs 2,234 | Rs 36,829 | 100.0% |
| 2023 | Jul | 15 | Rs 37,140 | Rs 1,834 | Rs 35,306 | 93.3% |
| 2023 | Aug | 19 | Rs 33,505 | Rs 2,279 | Rs 31,226 | 100.0% |
| 2023 | Sep | 15 | Rs 29,925 | Rs 1,815 | Rs 28,110 | 86.7% |
| 2023 | Oct | 12 | Rs 15,516 | Rs 1,463 | Rs 14,053 | 91.7% |
| 2023 | Nov | 17 | Rs 20,990 | Rs 2,015 | Rs 18,975 | 82.4% |
| 2023 | Dec | 20 | Rs 44,180 | Rs 2,566 | Rs 41,614 | 75.0% |
| 2024 | Jan | 20 | Rs 42,650 | Rs 2,673 | Rs 39,977 | 85.0% |
| 2024 | Feb | 21 | Rs 71,965 | Rs 2,916 | Rs 69,049 | 90.5% |
| 2024 | Mar | 18 | Rs 56,560 | Rs 2,399 | Rs 54,161 | 88.9% |
| 2024 | Apr | 16 | Rs 37,785 | Rs 2,111 | Rs 35,674 | 81.2% |
| 2024 | May | 20 | Rs 64,140 | Rs 2,857 | Rs 61,283 | 85.0% |
| 2024 | Jun | 17 | Rs 55,175 | Rs 2,253 | Rs 52,922 | 82.4% |
| 2024 | Jul | 22 | Rs 35,962 | Rs 2,790 | Rs 33,172 | 86.4% |
| 2024 | Aug | 20 | Rs 36,094 | Rs 2,482 | Rs 33,612 | 85.0% |
| 2024 | Sep | 20 | Rs 18,806 | Rs 2,479 | Rs 16,328 | 70.0% |
| 2024 | Oct | 22 | Rs 70,781 | Rs 2,791 | Rs 67,991 | 86.4% |
| 2024 | Nov | 17 | Rs 48,334 | Rs 2,213 | Rs 46,121 | 76.5% |
| 2024 | Dec | 21 | Rs 64,624 | Rs 2,756 | Rs 61,868 | 76.2% |
| 2025 | Jan | 23 | Rs 54,758 | Rs 3,043 | Rs 51,715 | 73.9% |
| 2025 | Feb | 19 | Rs 43,451 | Rs 2,435 | Rs 41,016 | 78.9% |
| 2025 | Mar | 18 | Rs 23,805 | Rs 2,222 | Rs 21,583 | 83.3% |
| 2025 | Apr | 18 | Rs 37,766 | Rs 2,269 | Rs 35,497 | 66.7% |
| 2025 | May | 19 | Rs 49,800 | Rs 2,547 | Rs 47,253 | 78.9% |
| 2025 | Jun | 21 | Rs 44,685 | Rs 2,730 | Rs 41,955 | 85.7% |
| 2025 | Jul | 21 | Rs 36,349 | Rs 2,532 | Rs 33,817 | 85.7% |
| 2025 | Aug | 19 | Rs 44,738 | Rs 2,279 | Rs 42,459 | 84.2% |
| 2025 | Sep | 7 | Rs 10,361 | Rs 823 | Rs 9,538 | 71.4% |
| 2025 | Oct | 11 | Rs 13,365 | Rs 1,361 | Rs 12,004 | 81.8% |
| 2025 | Nov | 18 | Rs 40,114 | Rs 2,196 | Rs 37,918 | 94.4% |
| 2025 | Dec | 4 | Rs 11,471 | Rs 460 | Rs 11,011 | 100.0% |
| 2026 | Jan | 13 | Rs 21,947 | Rs 1,638 | Rs 20,309 | 76.9% |
| 2026 | Feb | 18 | Rs 25,538 | Rs 2,217 | Rs 23,321 | 77.8% |
| 2026 | Mar | 12 | Rs 5,258 | Rs 1,823 | Rs 3,436 | 58.3% |

## Win/Loss Stats

| Metric | Value |
|--------|-------|
| Avg Win | Rs 3,204 |
| Avg Loss | Rs -1,966 |
| Largest Win | Rs 20,570 |
| Largest Loss | Rs -6,719 |
| Avg Combined Premium | Rs 207 |
| Best Month | 2023-02 (Rs 84,167) |
| Worst Month | 2026-03 (Rs 3,436) |
| Avg Lots/Trade | 3.65 |
| Max Lots/Trade | 6 |

## Fixed vs Compounding Comparison

| Metric | Fixed | Compound | Delta |
|--------|-------|----------|-------|
| Net P&L | Rs 26,84,686 | Rs 4,24,37,949 | +Rs 3,97,53,263 |
| Total Return | 1,074% | 16,975% | +15,901% |
| Win Rate | 85.2% | 85.6% | +0.4pp |
| Profit Factor | 9.38 | 7.24 | -2.14 |
| Max Drawdown | Rs -9,516 | Rs -3,96,903 | -Rs 3,87,388 |
| Max DD % Capital | 3.8% | 158.8% | +155.0pp |
| Sharpe Ratio | 12.84 | 8.30 | -4.54 |
| Calmar Ratio | 282.13 | 106.92 | -175.21 |
| Largest Loss | Rs -6,719 | Rs -3,10,298 | -Rs 3,03,580 |
| Return/MaxDD | 282.1x | 106.9x | -175.2x |

**Verdict:** Fixed capital is recommended for production. Compounding amplifies both gains and losses — at 50 lots, a single bad trade can lose Rs 3.1L. Risk-adjusted metrics (Sharpe, Calmar) strongly favor fixed.

## Charges Breakdown

| Component | Description |
|-----------|-------------|
| Brokerage | Rs 20 per order (Dhan flat fee) |
| STT | 0.0625% on sell side |
| Exchange Txn | 0.053% (NSE F&O) |
| SEBI Fee | 0.0001% turnover |
| GST | 18% on brokerage + exchange + SEBI |
| Stamp Duty | 0.003% on buy side |

## Caveats

- **85% win rate may be optimistic for live:** Backtest uses 1-min candles; live trading with 5-second monitoring may trigger more SLs
- **Slippage is conservative:** 1pt per leg may underestimate real-world slippage, especially at higher lot counts
- **VIX data gap:** Apr-Jul 2021 (86 days) has no VIX data — VIX entry filter can't fire for those days
- **No position sizing risk:** Fixed capital mode uses Rs 2.5L throughout; actual margin requirements vary
- **Past performance:** 5-year backtest does not guarantee future results; market microstructure changes (e.g., SEBI lot size changes, expiry day shifts from Thursday to Tuesday) affect strategy behavior

## Dashboard

Interactive HTML dashboard: [index.html](index.html)
