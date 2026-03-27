"""
Dhan Historical Data Fetcher
Downloads Nifty spot, ATM CE/PE options, and India VIX 1-min data.
Saves to backtest/data/ as Parquet files.
"""
from __future__ import annotations
import os, time, json
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import requests

# ── Dhan API Config ──────────────────────────────────────────────────────────

DHAN_BASE_URL = "https://api.dhan.co/v2"
DHAN_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Dhan security IDs
NIFTY_SECURITY_ID = "13"
VIX_SECURITY_ID = "26"

# API limits
INTRADAY_MAX_DAYS = 90
ROLLING_OPT_MAX_DAYS = 30  # Rolling option API: max 30 days per call
DAILY_MAX_DAYS = 365
API_RATE_LIMIT_SLEEP = 0.5  # seconds between calls


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID,
    }


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split a date range into chunks of max_days."""
    chunks = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


# ── Intraday Data (1-min candles) ────────────────────────────────────────────

def fetch_intraday(
    security_id: str,
    exchange_segment: str,
    instrument: str,
    from_date: date,
    to_date: date,
    interval: str = "1",
) -> pd.DataFrame:
    """Fetch intraday candles from Dhan in 90-day chunks."""
    url = f"{DHAN_BASE_URL}/charts/intraday"
    chunks = _date_chunks(from_date, to_date, INTRADAY_MAX_DAYS)
    all_frames = []

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": interval,
            "fromDate": chunk_start.strftime("%Y-%m-%d"),
            "toDate": chunk_end.strftime("%Y-%m-%d"),
        }
        print(f"  [{i+1}/{len(chunks)}] {chunk_start} to {chunk_end} ...", end=" ", flush=True)

        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:200]}")
            time.sleep(API_RATE_LIMIT_SLEEP)
            continue

        data = resp.json()
        df = _parse_candle_response(data)
        if df is not None and len(df) > 0:
            all_frames.append(df)
            print(f"{len(df)} candles")
        else:
            print("0 candles")

        time.sleep(API_RATE_LIMIT_SLEEP)

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)
    result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return result


def fetch_daily(
    security_id: str,
    exchange_segment: str,
    instrument: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    """Fetch daily OHLC candles from Dhan."""
    url = f"{DHAN_BASE_URL}/charts/historical"
    chunks = _date_chunks(from_date, to_date, DAILY_MAX_DAYS)
    all_frames = []

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        payload = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "expiryCode": 0,
            "fromDate": chunk_start.strftime("%Y-%m-%d"),
            "toDate": chunk_end.strftime("%Y-%m-%d"),
        }
        print(f"  [{i+1}/{len(chunks)}] {chunk_start} to {chunk_end} ...", end=" ", flush=True)

        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:200]}")
            time.sleep(API_RATE_LIMIT_SLEEP)
            continue

        data = resp.json()
        df = _parse_candle_response(data)
        if df is not None and len(df) > 0:
            all_frames.append(df)
            print(f"{len(df)} candles")
        else:
            print("0 candles")

        time.sleep(API_RATE_LIMIT_SLEEP)

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)
    result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return result


# ── Rolling Option Data (ATM CE/PE 1-min candles) ───────────────────────────

def fetch_rolling_options(
    option_type: str,  # "CE" or "PE"
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    """Fetch ATM rolling option 1-min candles from Dhan.

    The rolling option API returns candles for the nearest weekly expiry
    ATM option, rolling automatically on expiry.
    """
    url = f"{DHAN_BASE_URL}/charts/rollingoption"
    chunks = _date_chunks(from_date, to_date, ROLLING_OPT_MAX_DAYS)
    all_frames = []

    drv_option = "CALL" if option_type.upper() == "CE" else "PUT"

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        payload = {
            "securityId": int(NIFTY_SECURITY_ID),
            "exchangeSegment": "NSE_FNO",
            "instrument": "OPTIDX",
            "interval": 1,
            "expiryFlag": "WEEK",
            "expiryCode": 1,  # 1=nearest weekly expiry
            "strike": "ATM",
            "drvOptionType": drv_option,
            "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv", "spot"],
            "fromDate": chunk_start.strftime("%Y-%m-%d"),
            "toDate": (chunk_end + timedelta(days=1)).strftime("%Y-%m-%d"),  # toDate is non-inclusive
        }
        print(f"  [{i+1}/{len(chunks)}] {option_type} {chunk_start} to {chunk_end} ...", end=" ", flush=True)

        resp = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:200]}")
            time.sleep(API_RATE_LIMIT_SLEEP)
            continue

        data = resp.json()
        df = _parse_rolling_option_response(data, option_type)
        if df is not None and len(df) > 0:
            all_frames.append(df)
            print(f"{len(df)} candles")
        else:
            print("0 candles")

        time.sleep(API_RATE_LIMIT_SLEEP)

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)
    result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return result


# ── Response Parsing ─────────────────────────────────────────────────────────

def _parse_rolling_option_response(data: dict, option_type: str) -> pd.DataFrame | None:
    """Parse Dhan rolling option API response.

    Response format: {"data": {"ce": {...}, "pe": null}} or {"data": {"ce": null, "pe": {...}}}
    Inner dict has: open, high, low, close, volume, iv, oi, strike, spot, timestamp arrays.
    """
    if not isinstance(data, dict):
        return None
    if "errorCode" in data:
        return None

    inner = data.get("data", data)
    if not isinstance(inner, dict):
        return None

    # Pick the right sub-key based on option type
    key = "ce" if option_type.upper() == "CE" else "pe"
    leg_data = inner.get(key)
    if not leg_data or not isinstance(leg_data, dict):
        # Try the other key or the data itself
        leg_data = inner

    timestamps = leg_data.get("timestamp", [])
    if not timestamps:
        return None

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
        "open": [float(x) for x in leg_data.get("open", [0] * len(timestamps))],
        "high": [float(x) for x in leg_data.get("high", [0] * len(timestamps))],
        "low": [float(x) for x in leg_data.get("low", [0] * len(timestamps))],
        "close": [float(x) for x in leg_data.get("close", [0] * len(timestamps))],
        "volume": [int(x) for x in leg_data.get("volume", [0] * len(timestamps))],
    })

    # Add optional fields if present and non-empty
    for col, dtype in [("iv", float), ("oi", int), ("spot", float), ("strike", float)]:
        vals = leg_data.get(col, [])
        if vals and len(vals) == len(timestamps):
            df[col] = [dtype(x) for x in vals]

    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
    return df


def _parse_candle_response(data: dict | list) -> pd.DataFrame | None:
    """Parse Dhan candle API response into DataFrame.

    Dhan returns either:
    - Dict with arrays: {"open": [...], "high": [...], ...}
    - List of lists: [[ts, o, h, l, c, v], ...]
    """
    if isinstance(data, dict):
        # Check for error response
        if "errorCode" in data or "status" in data:
            if data.get("status") == "failure" or data.get("errorCode"):
                return None

        # Dict-of-arrays format
        timestamps = data.get("timestamp", data.get("start_Time", []))
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])

        if not timestamps:
            return None

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open": [float(x) for x in opens],
            "high": [float(x) for x in highs],
            "low": [float(x) for x in lows],
            "close": [float(x) for x in closes],
            "volume": [int(x) for x in volumes] if volumes else [0] * len(timestamps),
        })
        # Convert UTC to IST
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
        return df

    elif isinstance(data, list) and len(data) > 0:
        # List-of-lists format
        if isinstance(data[0], list):
            cols = ["timestamp", "open", "high", "low", "close"]
            if len(data[0]) > 5:
                cols.append("volume")
            df = pd.DataFrame(data, columns=cols)
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
            if "volume" not in df.columns:
                df["volume"] = 0
            return df

    return None


# ── Main Fetch Pipeline ──────────────────────────────────────────────────────

def fetch_all(start_date: str = "2021-04-01", end_date: str = "2026-03-28"):
    """Fetch all required data and save to Parquet.

    Args:
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    print(f"\n{'='*60}")
    print(f"  Dhan Data Fetch: {start} to {end}")
    print(f"{'='*60}\n")

    # 1. Nifty Spot 1-min
    spot_file = DATA_DIR / "nifty_spot_1min.parquet"
    if spot_file.exists():
        print(f"[SKIP] Nifty spot already exists: {spot_file}")
        spot_df = pd.read_parquet(spot_file)
    else:
        print("[1/5] Fetching Nifty 50 spot 1-min candles...")
        spot_df = fetch_intraday(
            security_id=NIFTY_SECURITY_ID,
            exchange_segment="IDX_I",
            instrument="INDEX",
            from_date=start,
            to_date=end,
        )
        if len(spot_df) > 0:
            spot_df.to_parquet(spot_file, index=False)
            print(f"  Saved: {len(spot_df)} candles -> {spot_file}")
        else:
            print("  WARNING: No spot data fetched!")

    # 2. ATM CE 1-min (rolling option)
    ce_file = DATA_DIR / "nifty_atm_ce_1min.parquet"
    if ce_file.exists():
        print(f"\n[SKIP] ATM CE already exists: {ce_file}")
    else:
        print("\n[2/5] Fetching Nifty ATM CE 1-min candles (rolling option)...")
        ce_df = fetch_rolling_options("CE", start, end)
        if len(ce_df) > 0:
            ce_df.to_parquet(ce_file, index=False)
            print(f"  Saved: {len(ce_df)} candles -> {ce_file}")
        else:
            print("  WARNING: No CE data fetched!")

    # 3. ATM PE 1-min (rolling option)
    pe_file = DATA_DIR / "nifty_atm_pe_1min.parquet"
    if pe_file.exists():
        print(f"\n[SKIP] ATM PE already exists: {pe_file}")
    else:
        print("\n[3/5] Fetching Nifty ATM PE 1-min candles (rolling option)...")
        pe_df = fetch_rolling_options("PE", start, end)
        if len(pe_df) > 0:
            pe_df.to_parquet(pe_file, index=False)
            print(f"  Saved: {len(pe_df)} candles -> {pe_file}")
        else:
            print("  WARNING: No PE data fetched!")

    # 4. India VIX 1-min (intraday)
    vix_intra_file = DATA_DIR / "india_vix_1min.parquet"
    if vix_intra_file.exists():
        print(f"\n[SKIP] VIX intraday already exists: {vix_intra_file}")
    else:
        print("\n[4/5] Fetching India VIX 1-min candles...")
        vix_df = fetch_intraday(
            security_id=VIX_SECURITY_ID,
            exchange_segment="IDX_I",
            instrument="INDEX",
            from_date=start,
            to_date=end,
        )
        if len(vix_df) > 0:
            vix_df.to_parquet(vix_intra_file, index=False)
            print(f"  Saved: {len(vix_df)} candles -> {vix_intra_file}")
        else:
            print("  WARNING: No VIX intraday data fetched!")

    # 5. India VIX daily — not available via Dhan API for VIX security ID
    # VIX intraday (step 4) covers ~1 year. For older dates, VIX spike exit
    # will be auto-disabled (no data = no trigger).
    print("\n[5/5] VIX daily: skipped (not available via Dhan; using intraday VIX where available)")

    # Summary
    print(f"\n{'='*60}")
    print("  Data fetch complete. Files in:", DATA_DIR)
    for f in sorted(DATA_DIR.glob("*.parquet")):
        df = pd.read_parquet(f)
        date_range = ""
        if "timestamp" in df.columns and len(df) > 0:
            date_range = f" ({df['timestamp'].min().date()} to {df['timestamp'].max().date()})"
        print(f"  {f.name}: {len(df):,} rows{date_range}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if not DHAN_ACCESS_TOKEN:
        print("ERROR: Set DHAN_ACCESS_TOKEN environment variable")
        exit(1)
    fetch_all()
