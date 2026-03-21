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
import logging
from datetime import datetime, timedelta
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
