# Nifty Short Straddle Dhan Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backtest the core Nifty short straddle strategy using 1 year of Dhan expired options data with VectorBT analytics.

**Architecture:** Two-phase pipeline — Phase 1 fetches and caches NIFTY ATM weekly options data from Dhan's rolling option API into Parquet files. Phase 2 runs a custom pandas day-by-day simulation (entry, per-leg SL with DTE overrides, dynamic time-of-day SL tightening, hard exit) and feeds results into VectorBT for analytics and charting.

**Tech Stack:** Python 3.10+, requests, pandas, pyarrow, vectorbt, tomli, tqdm, matplotlib

**Spec:** `backtest_results/dhan_backtest/docs/2026-03-21-dhan-backtest-design.md`

**All files created in:** `backtest_results/dhan_backtest/`

---

## File Structure

```
backtest_results/dhan_backtest/
├── dhan_data_fetcher.py      # Phase 1: Dhan API → Parquet
├── nifty_straddle_bt.py      # Phase 2: Simulation + VectorBT analytics
├── config_backtest.toml      # All configurable parameters
├── nse_holidays.py           # NSE holiday calendar + DTE/expiry utilities
├── docs/
│   ├── 2026-03-21-dhan-backtest-design.md
│   └── 2026-03-21-implementation-plan.md
├── data/                     # Cached Parquet (created by fetcher)
└── output/                   # Results (created by backtest)
    ├── bt_trades.csv
    ├── bt_summary.json
    └── charts/
```

---

### Task 1: Install Dependencies

**Files:** None (pip install only)

- [ ] **Step 1: Install required packages**

```bash
source ~/Developer/ShareMarket_Automation/algo_trading/bin/activate
pip install vectorbt pyarrow tomli requests
```

- [ ] **Step 2: Verify installations**

```bash
python -c "import vectorbt, pyarrow, tomli, requests; print('All OK')"
```

---

### Task 2: Create NSE Holiday Calendar + DTE Utilities

**Files:**
- Create: `backtest_results/dhan_backtest/nse_holidays.py`

This module provides:
1. NSE holiday list for 2025-2026
2. Function to find the correct weekly expiry date (Tuesday, or Monday if Tuesday is holiday)
3. Function to compute DTE (trading days to expiry)

- [ ] **Step 1: Create nse_holidays.py**

```python
"""NSE holiday calendar and DTE/expiry utilities for backtesting."""

from datetime import date, timedelta
from typing import Optional

# NSE market holidays 2025-2026 (manually curated from NSE circulars)
# Excludes weekends (Saturday/Sunday) — only lists weekday closures
NSE_HOLIDAYS_2025_2026: set[date] = {
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Eid)
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 6, 7),    # Bakrid / Eid-Ul-Adha
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 16),   # Parsi New Year
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurab Sri Guru Nanak Dev
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Mahashivratri
    date(2026, 3, 4),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr (Eid) — tentative
    date(2026, 3, 30),   # Idul Fitr
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakrid / Eid-Ul-Adha
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 9),   # Diwali Laxmi Pujan
    date(2026, 11, 24),  # Prakash Gurpurab Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(d: date) -> bool:
    """Check if a date is an NSE trading day (weekday + not holiday)."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS_2025_2026


def next_trading_day(d: date) -> date:
    """Return the next trading day on or after the given date."""
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    """Return the previous trading day on or before the given date."""
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def get_weekly_expiry(d: date) -> date:
    """
    Get the NIFTY weekly expiry date for the week containing date `d`.

    NIFTY weekly expiry is Tuesday. If Tuesday is a holiday,
    expiry moves to the previous trading day (typically Monday).
    """
    # Find the Tuesday of the current week (weekday 1 = Tuesday)
    days_ahead = 1 - d.weekday()  # 1 = Tuesday
    if days_ahead < 0:
        days_ahead += 7
    tuesday = d + timedelta(days=days_ahead)

    # If Tuesday is a holiday, expiry is the previous trading day
    if is_trading_day(tuesday):
        return tuesday
    return prev_trading_day(tuesday)


def compute_dte(d: date) -> int:
    """
    Compute DTE (Days To Expiry) as trading days from `d` to its weekly expiry.

    Returns 0 on expiry day, 1 on the day before, etc.
    """
    expiry = get_weekly_expiry(d)

    # If d is after this week's expiry, look at next week
    if d > expiry:
        next_tue = d + timedelta(days=(1 - d.weekday() + 7) % 7)
        if next_tue == d:
            next_tue += 7
        expiry = get_weekly_expiry(next_tue)

    # Count trading days between d and expiry (exclusive of d, inclusive of expiry)
    count = 0
    current = d
    while current < expiry:
        current += timedelta(days=1)
        if is_trading_day(current):
            count += 1
    return count


def get_trading_days_in_range(start: date, end: date) -> list[date]:
    """Return all trading days in [start, end] inclusive."""
    days = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days
```

- [ ] **Step 2: Verify with a quick test**

```bash
cd backtest_results/dhan_backtest
python -c "
from nse_holidays import compute_dte, get_weekly_expiry, is_trading_day
from datetime import date
# Tuesday 2026-03-24 should be expiry (DTE=0)
print(f'2026-03-24 expiry: {get_weekly_expiry(date(2026,3,24))} DTE: {compute_dte(date(2026,3,24))}')
# Monday 2026-03-23 should be DTE=1
print(f'2026-03-23 DTE: {compute_dte(date(2026,3,23))}')
# Friday 2026-03-20 should be DTE=2
print(f'2026-03-20 DTE: {compute_dte(date(2026,3,20))}')
"
```

Expected: DTE=0 for Tuesday, DTE=1 for Monday, DTE=2 for Friday.

- [ ] **Step 3: Commit**

```bash
git add backtest_results/dhan_backtest/nse_holidays.py
git commit -m "feat(backtest): add NSE holiday calendar and DTE utilities"
```

---

### Task 3: Create Backtest Config

**Files:**
- Create: `backtest_results/dhan_backtest/config_backtest.toml`

- [ ] **Step 1: Create config_backtest.toml**

```toml
[instrument]
underlying     = "NIFTY"
lot_size       = 65
number_of_lots = 1

[timing]
entry_time         = "09:30"
exit_time          = "15:15"
use_dte_entry_map  = true

[timing.dte_entry_time_map]
"0" = "09:30"
"1" = "09:30"
"2" = "09:35"
"3" = "09:40"
"4" = "09:45"

[risk]
leg_sl_percent              = 20.0
daily_profit_target_per_lot = 5000
daily_loss_limit_per_lot    = -4000

[risk.dte_sl_override]
"2" = 25.0
"3" = 28.0
"4" = 30.0

[risk.dynamic_sl]
enabled = true

[[risk.dynamic_sl.schedule]]
time = "14:30"
sl_pct = 7.0

[[risk.dynamic_sl.schedule]]
time = "13:30"
sl_pct = 10.0

[[risk.dynamic_sl.schedule]]
time = "12:00"
sl_pct = 15.0

[backtest]
from_date    = "2025-03-21"
to_date      = "2026-03-21"
skip_months  = [11]
trade_dte    = [0, 1, 2, 3, 4]
slippage_pct = 0.0

[dhan]
# Credentials from env: DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
security_id      = 13
exchange_segment = "NSE_FNO"
instrument       = "OPTIDX"
expiry_flag      = "WEEK"
expiry_code      = 0
interval         = "1"
```

- [ ] **Step 2: Commit**

```bash
git add backtest_results/dhan_backtest/config_backtest.toml
git commit -m "feat(backtest): add backtest configuration"
```

---

### Task 4: Build Dhan Data Fetcher (Phase 1)

**Files:**
- Create: `backtest_results/dhan_backtest/dhan_data_fetcher.py`

This is the largest task. The fetcher:
1. Reads config for date range and Dhan API params
2. Splits date range into 30-day batches
3. Fetches ATM CALL and PUT data for each batch
4. Merges CE+PE on timestamp into unified DataFrames
5. Saves to Parquet with resume capability

- [ ] **Step 1: Create dhan_data_fetcher.py**

```python
"""
Dhan Expired Options Data Fetcher

Fetches NIFTY ATM weekly options data (1-min candles) from Dhan's
/v2/charts/rollingoption API and caches to Parquet files.

Usage:
    export DHAN_ACCESS_TOKEN="your_token"
    python dhan_data_fetcher.py
"""

import os
import sys
import time
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

# Resolve paths relative to this script
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────
DHAN_API_URL = "https://api.dhan.co/v2/charts/rollingoption"
BATCH_DAYS = 30  # Max days per API call
RETRY_COUNT = 3
RETRY_BACKOFF = [1, 2, 4]  # seconds
API_DELAY = 0.5  # delay between calls


def load_config() -> dict:
    """Load config_backtest.toml."""
    config_path = SCRIPT_DIR / "config_backtest.toml"
    with open(config_path, "rb") as f:
        return tomli.load(f)


def get_date_batches(from_date: str, to_date: str) -> list[tuple[str, str]]:
    """Split date range into 30-day batches."""
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    batches = []
    current = start
    while current < end:
        batch_end = min(current + timedelta(days=BATCH_DAYS - 1), end)
        batches.append((current.strftime("%Y-%m-%d"), batch_end.strftime("%Y-%m-%d")))
        current = batch_end + timedelta(days=1)
    return batches


def fetch_rolling_option(
    access_token: str,
    security_id: int,
    exchange_segment: str,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    interval: str,
    strike: str,
    option_type: str,
    from_date: str,
    to_date: str,
) -> dict | None:
    """Make a single API call to Dhan's rolling option endpoint."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
    }
    payload = {
        "exchangeSegment": exchange_segment,
        "interval": interval,
        "securityId": security_id,
        "instrument": instrument,
        "expiryFlag": expiry_flag,
        "expiryCode": expiry_code,
        "strike": strike,
        "drvOptionType": option_type,
        "requiredData": ["open", "high", "low", "close", "iv", "volume", "oi", "spot"],
        "fromDate": from_date,
        "toDate": to_date,
    }

    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.post(DHAN_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data and "data" in data:
                    return data["data"]
                log.warning(f"Empty data for {option_type} {from_date}-{to_date}")
                return None
            elif resp.status_code == 429:
                wait = RETRY_BACKOFF[attempt] * 2
                log.warning(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                log.error(f"API error {resp.status_code}: {resp.text[:200]}")
                if attempt < RETRY_COUNT - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
        except requests.RequestException as e:
            log.error(f"Request failed (attempt {attempt+1}): {e}")
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_BACKOFF[attempt])

    return None


def parse_option_data(data: dict, option_type: str) -> pd.DataFrame:
    """Parse Dhan API response into a DataFrame."""
    key = "ce" if option_type == "CALL" else "pe"
    prefix = "ce" if option_type == "CALL" else "pe"

    if not data or key not in data or data[key] is None:
        return pd.DataFrame()

    leg_data = data[key]
    timestamps = leg_data.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()

    df = pd.DataFrame({"timestamp": timestamps})

    # Convert epoch to IST datetime
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")

    # Add OHLCV + IV + OI + spot
    for field in ["open", "high", "low", "close", "iv", "volume", "oi"]:
        values = leg_data.get(field, [])
        if values:
            col_name = f"{prefix}_{field}"
            df[col_name] = values[: len(timestamps)]
        else:
            df[f"{prefix}_{field}"] = None

    # Spot is common — only add once
    spot = leg_data.get("spot", [])
    if spot:
        df["spot"] = spot[: len(timestamps)]

    return df


def merge_ce_pe(ce_df: pd.DataFrame, pe_df: pd.DataFrame) -> pd.DataFrame:
    """Merge CE and PE DataFrames on timestamp."""
    if ce_df.empty and pe_df.empty:
        return pd.DataFrame()
    if ce_df.empty:
        return pe_df
    if pe_df.empty:
        return ce_df

    # Drop spot from PE if it exists in CE (avoid duplicate)
    pe_cols = [c for c in pe_df.columns if c != "spot"]
    merged = pd.merge(ce_df, pe_df[pe_cols], on="timestamp", how="outer")
    merged = merged.sort_values("timestamp").reset_index(drop=True)
    return merged


def main():
    """Main entry point: fetch all data and save to Parquet."""
    config = load_config()
    dhan_cfg = config["dhan"]
    bt_cfg = config["backtest"]

    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not access_token:
        log.error("DHAN_ACCESS_TOKEN environment variable not set")
        sys.exit(1)

    # Create data directory
    data_dir = SCRIPT_DIR / "data" / "nifty_options_2025"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Output parquet path
    output_path = data_dir / "nifty_atm_weekly_1min.parquet"

    # Check for existing data (resume capability)
    existing_df = None
    last_fetched_date = None
    if output_path.exists():
        existing_df = pd.read_parquet(output_path)
        if not existing_df.empty:
            last_fetched_date = existing_df["timestamp"].max().date()
            log.info(f"Existing data found up to {last_fetched_date}")

    # Generate date batches
    from_date = bt_cfg["from_date"]
    to_date = bt_cfg["to_date"]

    # If resuming, start from last fetched date
    if last_fetched_date:
        resume_date = (last_fetched_date + timedelta(days=1)).strftime("%Y-%m-%d")
        if resume_date >= to_date:
            log.info("Data already complete. Nothing to fetch.")
            return
        from_date = resume_date
        log.info(f"Resuming from {from_date}")

    batches = get_date_batches(from_date, to_date)
    log.info(f"Fetching {len(batches)} batches ({from_date} to {to_date})")

    all_dfs = []
    for batch_from, batch_to in tqdm(batches, desc="Fetching batches"):
        # Fetch CALL data
        ce_data = fetch_rolling_option(
            access_token=access_token,
            security_id=dhan_cfg["security_id"],
            exchange_segment=dhan_cfg["exchange_segment"],
            instrument=dhan_cfg["instrument"],
            expiry_flag=dhan_cfg["expiry_flag"],
            expiry_code=dhan_cfg["expiry_code"],
            interval=dhan_cfg["interval"],
            strike="ATM",
            option_type="CALL",
            from_date=batch_from,
            to_date=batch_to,
        )
        time.sleep(API_DELAY)

        # Fetch PUT data
        pe_data = fetch_rolling_option(
            access_token=access_token,
            security_id=dhan_cfg["security_id"],
            exchange_segment=dhan_cfg["exchange_segment"],
            instrument=dhan_cfg["instrument"],
            expiry_flag=dhan_cfg["expiry_flag"],
            expiry_code=dhan_cfg["expiry_code"],
            interval=dhan_cfg["interval"],
            strike="ATM",
            option_type="PUT",
            from_date=batch_from,
            to_date=batch_to,
        )
        time.sleep(API_DELAY)

        # Parse and merge
        ce_df = parse_option_data(ce_data, "CALL")
        pe_df = parse_option_data(pe_data, "PUT")
        merged = merge_ce_pe(ce_df, pe_df)

        if not merged.empty:
            all_dfs.append(merged)
            log.info(f"  Batch {batch_from} → {batch_to}: {len(merged)} rows")
        else:
            log.warning(f"  Batch {batch_from} → {batch_to}: NO DATA")

    if not all_dfs:
        log.error("No data fetched at all!")
        sys.exit(1)

    # Combine all batches
    new_df = pd.concat(all_dfs, ignore_index=True)

    # Merge with existing data if resuming
    if existing_df is not None and not existing_df.empty:
        new_df = pd.concat([existing_df, new_df], ignore_index=True)

    # Remove duplicates and sort
    new_df = new_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Save to Parquet
    new_df.to_parquet(output_path, index=False)
    log.info(f"Saved {len(new_df)} rows to {output_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"Data Fetch Complete")
    print(f"{'='*60}")
    print(f"  Rows:       {len(new_df):,}")
    print(f"  Date range: {new_df['timestamp'].min()} → {new_df['timestamp'].max()}")
    print(f"  File:       {output_path}")
    print(f"  Size:       {output_path.stat().st_size / 1024 / 1024:.1f} MB")
    if "spot" in new_df.columns:
        print(f"  Spot range: {new_df['spot'].min():.0f} → {new_df['spot'].max():.0f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test with a small date range (dry run)**

```bash
export DHAN_ACCESS_TOKEN="your_token"
# Temporarily change config to fetch just 5 days to validate API works
python dhan_data_fetcher.py
```

Verify: Parquet file created, rows > 0, spot prices look reasonable (NIFTY ~23000-24000 range).

- [ ] **Step 3: Commit**

```bash
git add backtest_results/dhan_backtest/dhan_data_fetcher.py
git commit -m "feat(backtest): add Dhan expired options data fetcher"
```

---

### Task 5: Build Backtest Engine (Phase 2)

**Files:**
- Create: `backtest_results/dhan_backtest/nifty_straddle_bt.py`

The simulation engine:
1. Loads cached Parquet data
2. Groups by trading day
3. For each day: applies entry logic, runs per-leg SL monitoring, records exit
4. Feeds daily P&L into VectorBT for analytics

- [ ] **Step 1: Create nifty_straddle_bt.py**

```python
"""
Nifty Short Straddle Backtest Engine

Runs a day-by-day simulation of the core short straddle strategy
using cached Dhan options data, then generates VectorBT analytics.

Usage:
    python nifty_straddle_bt.py
"""

import sys
import json
import logging
from datetime import datetime, time, date
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import tomli
except ImportError:
    import tomllib as tomli

from nse_holidays import compute_dte, is_trading_day, get_weekly_expiry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Config Loading ─────────────────────────────────────────────────────
def load_config() -> dict:
    config_path = SCRIPT_DIR / "config_backtest.toml"
    with open(config_path, "rb") as f:
        return tomli.load(f)


# ── Dynamic SL Tightening ─────────────────────────────────────────────
def get_dynamic_sl_pct(
    current_time: time,
    base_sl_pct: float,
    schedule: list[dict],
) -> float:
    """
    Get the effective SL% based on time-of-day schedule.
    Schedule entries are checked from latest to earliest.
    Returns the tightest applicable SL%.
    """
    # Sort schedule by time descending
    sorted_sched = sorted(
        schedule,
        key=lambda s: datetime.strptime(s["time"], "%H:%M").time(),
        reverse=True,
    )
    for entry in sorted_sched:
        sched_time = datetime.strptime(entry["time"], "%H:%M").time()
        if current_time >= sched_time:
            return min(entry["sl_pct"], base_sl_pct)
    return base_sl_pct


# ── Single Day Simulation ─────────────────────────────────────────────
def simulate_day(
    day_df: pd.DataFrame,
    trading_date: date,
    config: dict,
) -> dict | None:
    """
    Simulate one trading day of short straddle.

    Returns a dict with trade details or None if no trade taken.
    """
    dte = compute_dte(trading_date)
    expiry = get_weekly_expiry(trading_date)
    bt_cfg = config["backtest"]
    risk_cfg = config["risk"]
    timing_cfg = config["timing"]
    instrument_cfg = config["instrument"]

    # ── Filter: Skip if DTE not in allowed list ──
    if dte not in bt_cfg["trade_dte"]:
        return None

    # ── Filter: Skip if month in skip_months ──
    if trading_date.month in bt_cfg["skip_months"]:
        return None

    # ── Determine entry time ──
    if timing_cfg.get("use_dte_entry_map", False):
        dte_map = timing_cfg.get("dte_entry_time_map", {})
        entry_str = dte_map.get(str(dte), timing_cfg["entry_time"])
    else:
        entry_str = timing_cfg["entry_time"]

    entry_time = datetime.strptime(entry_str, "%H:%M").time()
    exit_time = datetime.strptime(timing_cfg["exit_time"], "%H:%M").time()

    # ── Find entry candle ──
    day_df = day_df.copy()
    day_df["time"] = day_df["timestamp"].dt.time

    entry_candles = day_df[day_df["time"] >= entry_time]
    if entry_candles.empty:
        return None  # No candle at/after entry time

    entry_row = entry_candles.iloc[0]

    # Check we have valid CE and PE prices
    ce_entry = entry_row.get("ce_close")
    pe_entry = entry_row.get("pe_close")
    spot_at_entry = entry_row.get("spot")

    if pd.isna(ce_entry) or pd.isna(pe_entry) or ce_entry <= 0 or pe_entry <= 0:
        log.warning(f"{trading_date}: Invalid entry prices CE={ce_entry} PE={pe_entry}")
        return None

    combined_premium = ce_entry + pe_entry

    # ── SL Parameters ──
    base_sl_pct = risk_cfg.get("dte_sl_override", {}).get(
        str(dte), risk_cfg["leg_sl_percent"]
    )
    dynamic_sl_cfg = risk_cfg.get("dynamic_sl", {})
    dynamic_enabled = dynamic_sl_cfg.get("enabled", False)
    schedule = dynamic_sl_cfg.get("schedule", [])
    slippage_pct = bt_cfg.get("slippage_pct", 0.0)

    lot_size = instrument_cfg["lot_size"]
    num_lots = instrument_cfg["number_of_lots"]
    qty = lot_size * num_lots

    # Daily limits
    profit_target = risk_cfg["daily_profit_target_per_lot"] * num_lots
    loss_limit = risk_cfg["daily_loss_limit_per_lot"] * num_lots

    # ── Monitor candles (after entry, up to exit time) ──
    monitor_df = day_df[
        (day_df["timestamp"] > entry_row["timestamp"])
        & (day_df["time"] <= exit_time)
    ]

    # Track leg state
    ce_active = True
    pe_active = True
    ce_exit_price = None
    pe_exit_price = None
    ce_exit_time = None
    pe_exit_time = None
    ce_sl_hit = False
    pe_sl_hit = False

    for _, row in monitor_df.iterrows():
        current_time = row["time"]

        # Compute effective SL% for this minute
        if dynamic_enabled:
            eff_sl_pct = get_dynamic_sl_pct(current_time, base_sl_pct, schedule)
        else:
            eff_sl_pct = base_sl_pct

        # ── Check CE leg SL ──
        if ce_active:
            ce_sl_level = ce_entry * (1 + eff_sl_pct / 100)
            ce_high = row.get("ce_high", 0)
            if not pd.isna(ce_high) and ce_high >= ce_sl_level:
                ce_exit_price = ce_sl_level * (1 + slippage_pct / 100)
                ce_exit_time = row["timestamp"]
                ce_active = False
                ce_sl_hit = True

        # ── Check PE leg SL ──
        if pe_active:
            pe_sl_level = pe_entry * (1 + eff_sl_pct / 100)
            pe_high = row.get("pe_high", 0)
            if not pd.isna(pe_high) and pe_high >= pe_sl_level:
                pe_exit_price = pe_sl_level * (1 + slippage_pct / 100)
                pe_exit_time = row["timestamp"]
                pe_active = False
                pe_sl_hit = True

        # ── Check daily limits (combined P&L) — every candle, not just after leg close ──
        # Calculate running P&L using exit price for closed legs, current LTP for open legs
        ce_current = ce_exit_price if not ce_active else row.get("ce_close", ce_entry)
        pe_current = pe_exit_price if not pe_active else row.get("pe_close", pe_entry)
        running_pnl = (ce_entry - ce_current) * qty + (pe_entry - pe_current) * qty

            if running_pnl >= profit_target:
                # Close remaining legs at current price
                if ce_active:
                    ce_exit_price = row.get("ce_close", ce_entry)
                    ce_exit_time = row["timestamp"]
                    ce_active = False
                if pe_active:
                    pe_exit_price = row.get("pe_close", pe_entry)
                    pe_exit_time = row["timestamp"]
                    pe_active = False
                break

            if running_pnl <= loss_limit:
                if ce_active:
                    ce_exit_price = row.get("ce_close", ce_entry)
                    ce_exit_time = row["timestamp"]
                    ce_active = False
                if pe_active:
                    pe_exit_price = row.get("pe_close", pe_entry)
                    pe_exit_time = row["timestamp"]
                    pe_active = False
                break

        # Both legs closed — stop monitoring
        if not ce_active and not pe_active:
            break

    # ── Hard exit at 15:15 for any remaining legs ──
    if ce_active or pe_active:
        exit_candles = day_df[day_df["time"] >= exit_time]
        if not exit_candles.empty:
            exit_row = exit_candles.iloc[0]
            if ce_active:
                ce_exit_price = exit_row.get("ce_close", ce_entry)
                ce_exit_time = exit_row["timestamp"]
                ce_active = False
            if pe_active:
                pe_exit_price = exit_row.get("pe_close", pe_entry)
                pe_exit_time = exit_row["timestamp"]
                pe_active = False
        else:
            # Use last candle if no 15:15 candle
            last_row = day_df.iloc[-1]
            if ce_active:
                ce_exit_price = last_row.get("ce_close", ce_entry)
                ce_exit_time = last_row["timestamp"]
            if pe_active:
                pe_exit_price = last_row.get("pe_close", pe_entry)
                pe_exit_time = last_row["timestamp"]

    # ── Calculate P&L (short position: profit = entry - exit) ──
    ce_pnl = (ce_entry - ce_exit_price) * qty
    pe_pnl = (pe_entry - pe_exit_price) * qty
    total_pnl = ce_pnl + pe_pnl

    return {
        "date": trading_date.isoformat(),
        "expiry": expiry.isoformat(),
        "dte": dte,
        "spot_at_entry": round(spot_at_entry, 2),
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "combined_premium": round(combined_premium, 2),
        "ce_exit": round(ce_exit_price, 2),
        "pe_exit": round(pe_exit_price, 2),
        "ce_sl_hit": ce_sl_hit,
        "pe_sl_hit": pe_sl_hit,
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "entry_time": entry_str,
        "ce_exit_time": str(ce_exit_time) if ce_exit_time else None,
        "pe_exit_time": str(pe_exit_time) if pe_exit_time else None,
    }


# ── Run Full Backtest ──────────────────────────────────────────────────
def run_backtest(config: dict) -> pd.DataFrame:
    """Run backtest across all trading days."""
    data_path = SCRIPT_DIR / "data" / "nifty_options_2025" / "nifty_atm_weekly_1min.parquet"
    if not data_path.exists():
        log.error(f"Data file not found: {data_path}")
        log.error("Run dhan_data_fetcher.py first to download data.")
        sys.exit(1)

    df = pd.read_parquet(data_path)
    log.info(f"Loaded {len(df):,} rows from {data_path}")

    # Ensure timestamp is tz-aware
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")

    # Group by trading date
    df["trading_date"] = df["timestamp"].dt.date
    grouped = df.groupby("trading_date")

    trades = []
    for trading_date, day_df in tqdm(grouped, desc="Backtesting"):
        if not is_trading_day(trading_date):
            continue

        result = simulate_day(day_df, trading_date, config)
        if result:
            trades.append(result)

    if not trades:
        log.error("No trades generated!")
        sys.exit(1)

    trades_df = pd.DataFrame(trades)
    log.info(f"Generated {len(trades_df)} trades")
    return trades_df


# ── VectorBT Analytics ─────────────────────────────────────────────────
def generate_analytics(trades_df: pd.DataFrame, config: dict):
    """Generate VectorBT analytics and save results."""
    import vectorbt as vbt
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend
    import matplotlib.pyplot as plt

    output_dir = SCRIPT_DIR / "output"
    charts_dir = output_dir / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    lot_size = config["instrument"]["lot_size"]
    num_lots = config["instrument"]["number_of_lots"]

    # ── Save trade log ──
    trades_df.to_csv(output_dir / "bt_trades.csv", index=False)
    log.info(f"Trade log saved to {output_dir / 'bt_trades.csv'}")

    # ── Compute equity curve ──
    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    trades_df["cumulative_pnl"] = trades_df["total_pnl"].cumsum()

    # ── Summary Statistics ──
    total_pnl = trades_df["total_pnl"].sum()
    num_trades = len(trades_df)
    winners = trades_df[trades_df["total_pnl"] > 0]
    losers = trades_df[trades_df["total_pnl"] < 0]
    win_rate = len(winners) / num_trades * 100 if num_trades > 0 else 0
    avg_win = winners["total_pnl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["total_pnl"].mean() if len(losers) > 0 else 0
    profit_factor = (
        abs(winners["total_pnl"].sum() / losers["total_pnl"].sum())
        if len(losers) > 0 and losers["total_pnl"].sum() != 0
        else float("inf")
    )

    # Max drawdown
    cumulative = trades_df["cumulative_pnl"]
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max
    max_drawdown = drawdown.min()
    max_drawdown_pct = (max_drawdown / running_max[drawdown.idxmin()] * 100
                        if running_max[drawdown.idxmin()] != 0 else 0)

    # Sharpe ratio (using actual trade count for annualization)
    daily_returns = trades_df["total_pnl"]
    trades_per_year = len(daily_returns)  # actual trades in backtest period
    sharpe = (
        (daily_returns.mean() / daily_returns.std()) * np.sqrt(trades_per_year)
        if daily_returns.std() > 0
        else 0
    )

    # Sortino ratio
    downside = daily_returns[daily_returns < 0]
    sortino = (
        (daily_returns.mean() / downside.std()) * np.sqrt(trades_per_year)
        if len(downside) > 0 and downside.std() > 0
        else 0
    )

    # SL hit analysis
    ce_sl_hits = trades_df["ce_sl_hit"].sum()
    pe_sl_hits = trades_df["pe_sl_hit"].sum()
    both_sl_hits = ((trades_df["ce_sl_hit"]) & (trades_df["pe_sl_hit"])).sum()
    no_sl_hits = ((~trades_df["ce_sl_hit"]) & (~trades_df["pe_sl_hit"])).sum()

    # Per-DTE breakdown
    dte_stats = trades_df.groupby("dte").agg(
        count=("total_pnl", "count"),
        total_pnl=("total_pnl", "sum"),
        avg_pnl=("total_pnl", "mean"),
        win_rate=("total_pnl", lambda x: (x > 0).sum() / len(x) * 100),
    ).to_dict(orient="index")

    summary = {
        "total_pnl": round(total_pnl, 2),
        "num_trades": num_trades,
        "win_rate_pct": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "ce_sl_hits": int(ce_sl_hits),
        "pe_sl_hits": int(pe_sl_hits),
        "both_sl_hits": int(both_sl_hits),
        "no_sl_days": int(no_sl_hits),
        "per_dte": {str(k): {kk: round(vv, 2) for kk, vv in v.items()} for k, v in dte_stats.items()},
        "lot_size": lot_size,
        "num_lots": num_lots,
        "backtest_period": f"{config['backtest']['from_date']} to {config['backtest']['to_date']}",
    }

    # Save summary
    with open(output_dir / "bt_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info(f"Summary saved to {output_dir / 'bt_summary.json'}")

    # ── Charts ──

    # 1. Equity Curve
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(trades_df["date"], trades_df["cumulative_pnl"], linewidth=1.5, color="blue")
    ax.fill_between(trades_df["date"], trades_df["cumulative_pnl"], alpha=0.15, color="blue")
    ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax.set_title(f"Equity Curve — Nifty Short Straddle (Total P&L: ₹{total_pnl:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative P&L (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "equity_curve.png", dpi=150)
    plt.close(fig)

    # 2. Drawdown Chart
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(trades_df["date"], drawdown, color="red", alpha=0.4)
    ax.set_title(f"Drawdown (Max: ₹{max_drawdown:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "drawdown.png", dpi=150)
    plt.close(fig)

    # 3. Monthly Returns Heatmap
    trades_df["year"] = trades_df["date"].dt.year
    trades_df["month"] = trades_df["date"].dt.month
    monthly = trades_df.groupby(["year", "month"])["total_pnl"].sum().unstack(fill_value=0)

    fig, ax = plt.subplots(figsize=(14, 4))
    im = ax.imshow(monthly.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(monthly.columns)))
    ax.set_xticklabels([f"M{m}" for m in monthly.columns])
    ax.set_yticks(range(len(monthly.index)))
    ax.set_yticklabels(monthly.index)
    # Add text annotations
    for i in range(len(monthly.index)):
        for j in range(len(monthly.columns)):
            val = monthly.values[i, j]
            ax.text(j, i, f"₹{val:,.0f}", ha="center", va="center", fontsize=8,
                    color="black" if abs(val) < monthly.values.max() * 0.5 else "white")
    ax.set_title("Monthly P&L Heatmap")
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(charts_dir / "monthly_heatmap.png", dpi=150)
    plt.close(fig)

    # 4. Per-DTE Performance
    dte_df = trades_df.groupby("dte").agg(
        total=("total_pnl", "sum"),
        avg=("total_pnl", "mean"),
        count=("total_pnl", "count"),
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].bar(dte_df["dte"], dte_df["total"], color=["green" if x > 0 else "red" for x in dte_df["total"]])
    axes[0].set_title("Total P&L by DTE")
    axes[0].set_xlabel("DTE")
    axes[0].set_ylabel("Total P&L (₹)")
    axes[0].grid(True, alpha=0.3)

    axes[1].bar(dte_df["dte"], dte_df["avg"], color=["green" if x > 0 else "red" for x in dte_df["avg"]])
    axes[1].set_title("Avg P&L per Trade by DTE")
    axes[1].set_xlabel("DTE")
    axes[1].set_ylabel("Avg P&L (₹)")
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("Performance by Days-to-Expiry")
    fig.tight_layout()
    fig.savefig(charts_dir / "dte_breakdown.png", dpi=150)
    plt.close(fig)

    # ── Print Summary to Console ──
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — Nifty Short Straddle (Core)")
    print(f"{'='*60}")
    print(f"  Period:         {config['backtest']['from_date']} → {config['backtest']['to_date']}")
    print(f"  Total Trades:   {num_trades}")
    print(f"  Total P&L:      ₹{total_pnl:,.2f}")
    print(f"  Win Rate:       {win_rate:.1f}%")
    print(f"  Avg Win:        ₹{avg_win:,.2f}")
    print(f"  Avg Loss:       ₹{avg_loss:,.2f}")
    print(f"  Profit Factor:  {profit_factor:.2f}")
    print(f"  Max Drawdown:   ₹{max_drawdown:,.2f}")
    print(f"  Sharpe Ratio:   {sharpe:.2f}")
    print(f"  Sortino Ratio:  {sortino:.2f}")
    print(f"{'─'*60}")
    print(f"  CE SL Hits:     {ce_sl_hits} ({ce_sl_hits/num_trades*100:.1f}%)")
    print(f"  PE SL Hits:     {pe_sl_hits} ({pe_sl_hits/num_trades*100:.1f}%)")
    print(f"  Both SL Hit:    {both_sl_hits} ({both_sl_hits/num_trades*100:.1f}%)")
    print(f"  No SL (decay):  {no_sl_hits} ({no_sl_hits/num_trades*100:.1f}%)")
    print(f"{'─'*60}")
    print(f"  Per-DTE:")
    for dte_val, stats in sorted(dte_stats.items()):
        print(f"    DTE {dte_val}: {stats['count']:.0f} trades, ₹{stats['total_pnl']:,.0f} total, "
              f"₹{stats['avg_pnl']:,.0f} avg, {stats['win_rate']:.0f}% win")
    print(f"{'='*60}")
    print(f"\n  Output: {output_dir}/")
    print(f"  Charts: {charts_dir}/")


# ── Main ───────────────────────────────────────────────────────────────
def main():
    config = load_config()
    trades_df = run_backtest(config)
    generate_analytics(trades_df, config)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the backtest** (after data is fetched)

```bash
cd backtest_results/dhan_backtest
python nifty_straddle_bt.py
```

Expected: Console prints summary stats, files created in `output/`.

- [ ] **Step 3: Verify outputs exist**

```bash
ls -la output/bt_trades.csv output/bt_summary.json output/charts/
```

- [ ] **Step 4: Commit**

```bash
git add backtest_results/dhan_backtest/nifty_straddle_bt.py
git commit -m "feat(backtest): add short straddle simulation engine with VectorBT analytics"
```

---

### Task 6: End-to-End Test Run

- [ ] **Step 1: Set environment variables**

```bash
export DHAN_ACCESS_TOKEN="your_token"
```

- [ ] **Step 2: Fetch data (Phase 1)**

```bash
cd backtest_results/dhan_backtest
python dhan_data_fetcher.py
```

Verify: `data/nifty_options_2025/nifty_atm_weekly_1min.parquet` exists with data.

- [ ] **Step 3: Run backtest (Phase 2)**

```bash
python nifty_straddle_bt.py
```

Verify:
- Console shows summary with reasonable numbers
- `output/bt_trades.csv` has one row per trading day
- `output/bt_summary.json` has all metrics
- `output/charts/` has 4 PNG files

- [ ] **Step 4: Sanity checks**

```bash
python -c "
import pandas as pd
df = pd.read_csv('output/bt_trades.csv')
print(f'Trades: {len(df)}')
print(f'Date range: {df.date.min()} to {df.date.max()}')
print(f'Total P&L: {df.total_pnl.sum():,.0f}')
print(f'CE SL hits: {df.ce_sl_hit.sum()} / {len(df)}')
print(f'PE SL hits: {df.pe_sl_hit.sum()} / {len(df)}')
print(f'Avg combined premium: {df.combined_premium.mean():.0f}')
"
```

- [ ] **Step 5: Final commit**

```bash
git add backtest_results/dhan_backtest/
git commit -m "feat(backtest): complete Dhan-based short straddle backtest pipeline"
```
