"""
Dhan Historical Data Fetcher — Bull Put Spread
Downloads Nifty spot, OTM PE options (sell + buy legs), and India VIX 1-min data.
Saves to backtest/data/ as Parquet files.

Data required:
  - Nifty spot 1-min (for ATM calculation context)
  - PE at ATM-2 (sell leg: ATM - 100 pts)
  - PE at ATM-6 (buy leg: ATM - 300 pts)
  - India VIX 1-min (optional, for VIX analysis)
"""
from __future__ import annotations
import os, time
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
ROLLING_OPT_MAX_DAYS = 30
API_RATE_LIMIT_SLEEP = 0.5


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": DHAN_ACCESS_TOKEN,
        "client-id": DHAN_CLIENT_ID,
    }


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    chunks = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def fetch_intraday(
    security_id: str,
    exchange_segment: str,
    instrument: str,
    from_date: date,
    to_date: date,
    interval: str = "1",
) -> pd.DataFrame:
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


def fetch_rolling_options(
    option_type: str,
    from_date: date,
    to_date: date,
    strike: str = "ATM",
) -> pd.DataFrame:
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
            "expiryCode": 1,
            "strike": strike,
            "drvOptionType": drv_option,
            "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv", "spot"],
            "fromDate": chunk_start.strftime("%Y-%m-%d"),
            "toDate": (chunk_end + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        print(f"  [{i+1}/{len(chunks)}] {option_type} {strike} {chunk_start} to {chunk_end} ...", end=" ", flush=True)

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


def _parse_rolling_option_response(data: dict, option_type: str) -> pd.DataFrame | None:
    if not isinstance(data, dict):
        return None
    if "errorCode" in data:
        return None

    inner = data.get("data", data)
    if not isinstance(inner, dict):
        return None

    key = "ce" if option_type.upper() == "CE" else "pe"
    leg_data = inner.get(key)
    if not leg_data or not isinstance(leg_data, dict):
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

    for col, dtype in [("iv", float), ("oi", int), ("spot", float), ("strike", float)]:
        vals = leg_data.get(col, [])
        if vals and len(vals) == len(timestamps):
            df[col] = [dtype(x) for x in vals]

    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
    return df


def _parse_candle_response(data: dict | list) -> pd.DataFrame | None:
    if isinstance(data, dict):
        if "errorCode" in data or data.get("status") == "failure":
            return None

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
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
        return df

    elif isinstance(data, list) and len(data) > 0:
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    print(f"\n{'='*60}")
    print(f"  Bull Put Spread Data Fetch: {start} to {end}")
    print(f"{'='*60}\n")

    # 1. Nifty Spot 1-min
    spot_file = DATA_DIR / "nifty_spot_1min.parquet"
    if spot_file.exists():
        print(f"[SKIP] Nifty spot already exists: {spot_file}")
    else:
        print("[1/4] Fetching Nifty 50 spot 1-min candles...")
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

    # 2. Sell leg: PE at ATM-2 (ATM - 100 pts)
    sell_pe_file = DATA_DIR / "nifty_pe_atm_minus2_1min.parquet"
    if sell_pe_file.exists():
        print(f"\n[SKIP] Sell PE (ATM-2) already exists: {sell_pe_file}")
    else:
        print("\n[2/4] Fetching PE at ATM-2 (sell leg, ATM-100 pts)...")
        sell_pe_df = fetch_rolling_options("PE", start, end, strike="ATM-2")
        if len(sell_pe_df) > 0:
            sell_pe_df.to_parquet(sell_pe_file, index=False)
            print(f"  Saved: {len(sell_pe_df)} candles -> {sell_pe_file}")
        else:
            print("  WARNING: No sell PE data fetched!")

    # 3. Buy leg: PE at ATM-6 (ATM - 300 pts)
    buy_pe_file = DATA_DIR / "nifty_pe_atm_minus6_1min.parquet"
    if buy_pe_file.exists():
        print(f"\n[SKIP] Buy PE (ATM-6) already exists: {buy_pe_file}")
    else:
        print("\n[3/4] Fetching PE at ATM-6 (buy leg, ATM-300 pts)...")
        buy_pe_df = fetch_rolling_options("PE", start, end, strike="ATM-6")
        if len(buy_pe_df) > 0:
            buy_pe_df.to_parquet(buy_pe_file, index=False)
            print(f"  Saved: {len(buy_pe_df)} candles -> {buy_pe_file}")
        else:
            print("  WARNING: No buy PE data fetched!")

    # 4. India VIX 1-min
    vix_file = DATA_DIR / "india_vix_1min.parquet"
    if vix_file.exists():
        print(f"\n[SKIP] VIX already exists: {vix_file}")
    else:
        print("\n[4/4] Fetching India VIX 1-min candles...")
        vix_df = fetch_intraday(
            security_id=VIX_SECURITY_ID,
            exchange_segment="IDX_I",
            instrument="INDEX",
            from_date=start,
            to_date=end,
        )
        if len(vix_df) > 0:
            vix_df.to_parquet(vix_file, index=False)
            print(f"  Saved: {len(vix_df)} candles -> {vix_file}")
        else:
            print("  WARNING: No VIX data fetched!")

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
