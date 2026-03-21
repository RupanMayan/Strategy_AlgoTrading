"""
Fetch India VIX historical data for backtesting.

Sources (in priority order):
  1. NSE direct API (same as production vix_manager.py bootstrap)
  2. Yahoo Finance (^INDIAVIX) as fallback

Merges with any existing vix_history.csv in the backtest data/ folder.

Usage:
    python fetch_vix_history.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

OUTPUT_PATH = PROJECT_DIR / "data" / "vix_history.csv"


def fetch_from_nse(start_date: date, end_date: date) -> list[tuple[str, float]]:
    """Fetch VIX history from NSE API in 90-day chunks."""
    log.info(f"Fetching VIX from NSE: {start_date} → {end_date}")

    chunks = []
    chunk_start = start_date
    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=89), end_date)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    log.info(f"  {len(chunks)} chunks to fetch")

    raw = []
    try:
        sess = requests.Session()
        sess.headers.update(NSE_HEADERS)
        sess.get("https://www.nseindia.com", timeout=10)
        sess.get("https://www.nseindia.com/reports-indices-historical-vix", timeout=10)

        for i, (chunk_from, chunk_to) in enumerate(chunks):
            if i > 0:
                time.sleep(1.5)
            from_str = chunk_from.strftime("%d-%m-%Y")
            to_str = chunk_to.strftime("%d-%m-%Y")
            url = f"https://www.nseindia.com/api/historicalOR/vixhistory?from={from_str}&to={to_str}"

            try:
                r = sess.get(url, timeout=20)
                r.raise_for_status()
                chunk_data = r.json().get("data", [])
                log.info(f"  Chunk {i+1}/{len(chunks)}: {from_str} → {to_str}: {len(chunk_data)} records")
                raw.extend(chunk_data)
            except Exception as exc:
                log.warning(f"  Chunk {i+1} failed: {exc}")
                # Re-init session on failure
                sess = requests.Session()
                sess.headers.update(NSE_HEADERS)
                try:
                    sess.get("https://www.nseindia.com", timeout=10)
                    sess.get("https://www.nseindia.com/reports-indices-historical-vix", timeout=10)
                except Exception:
                    pass
                time.sleep(3)

    except Exception as exc:
        log.warning(f"NSE session init failed: {exc}")
        return []

    # Parse NSE response
    rows = []
    for item in raw:
        date_raw = (
            item.get("EOD_TIMESTAMP") or item.get("Date") or
            item.get("date") or ""
        )
        close_raw = (
            item.get("EOD_CLOSE_INDEX_VAL") or item.get("EOD_INDEX_VALUE") or
            item.get("CLOSE") or item.get("Close") or
            item.get("close") or ""
        )
        if not date_raw or not close_raw:
            continue

        parsed_date = None
        for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                parsed_date = datetime.strptime(str(date_raw).strip(), fmt).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            continue

        try:
            vix_val = float(str(close_raw).replace(",", "").strip())
            if vix_val > 0:
                rows.append((parsed_date.isoformat(), vix_val))
        except (ValueError, TypeError):
            continue

    log.info(f"NSE: parsed {len(rows)} valid rows")
    return rows


def fetch_from_yahoo(start_date: date, end_date: date) -> list[tuple[str, float]]:
    """Fetch VIX history from Yahoo Finance as fallback."""
    log.info(f"Fetching VIX from Yahoo Finance: {start_date} → {end_date}")
    try:
        import yfinance as yf
        vix = yf.download(
            "^INDIAVIX",
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            progress=False,
        )
        if vix.empty:
            log.warning("Yahoo Finance returned empty data")
            return []

        rows = []
        for idx, row in vix.iterrows():
            dt = idx.date() if hasattr(idx, "date") else idx
            try:
                close_val = float(row[("Close", "^INDIAVIX")])
                if close_val > 0:
                    rows.append((dt.isoformat(), round(close_val, 2)))
            except (KeyError, TypeError, ValueError):
                try:
                    close_val = float(row["Close"])
                    if close_val > 0:
                        rows.append((dt.isoformat(), round(close_val, 2)))
                except (KeyError, TypeError, ValueError):
                    continue

        log.info(f"Yahoo Finance: {len(rows)} rows")
        return rows
    except ImportError:
        log.warning("yfinance not installed — run: pip install yfinance")
        return []
    except Exception as exc:
        log.warning(f"Yahoo Finance failed: {exc}")
        return []


def merge_and_save(rows: list[tuple[str, float]], output_path: Path) -> None:
    """Merge with existing data, deduplicate, and save."""
    # Load existing data if present
    existing_rows = []
    if output_path.exists():
        try:
            with open(output_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.lower().startswith("date"):
                        continue
                    parts = line.split(",")
                    if len(parts) >= 2:
                        try:
                            existing_rows.append((parts[0].strip(), float(parts[1].strip())))
                        except ValueError:
                            continue
            log.info(f"Existing data: {len(existing_rows)} rows")
        except Exception as exc:
            log.warning(f"Could not read existing file: {exc}")

    # Merge
    all_rows = existing_rows + rows

    # Deduplicate by date (keep latest value per date)
    seen = {}
    for d, v in all_rows:
        seen[d] = v
    final = sorted(seen.items(), key=lambda x: x[0])

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("date,vix_close\n")
        for d, v in final:
            f.write(f"{d},{v:.2f}\n")

    log.info(f"Saved {len(final)} rows to {output_path}")
    if final:
        log.info(f"  Range: {final[0][0]} → {final[-1][0]}")


def main():
    start_date = date(2021, 3, 22)
    end_date = date(2026, 3, 21)

    # Try NSE first
    rows = fetch_from_nse(start_date, end_date)

    if len(rows) < 100:
        log.warning(f"NSE returned only {len(rows)} rows — falling back to Yahoo Finance")
        yahoo_rows = fetch_from_yahoo(start_date, end_date)
        # Merge NSE + Yahoo (NSE takes priority for overlapping dates)
        nse_dates = {d for d, _ in rows}
        for d, v in yahoo_rows:
            if d not in nse_dates:
                rows.append((d, v))

    if not rows:
        log.error("No VIX data fetched from any source!")
        sys.exit(1)

    merge_and_save(rows, OUTPUT_PATH)

    # Also copy to production data folder for consistency
    prod_path = Path(__file__).resolve().parents[3] / "data" / "vix_history.csv"
    if prod_path.exists():
        log.info(f"Production vix_history.csv exists at {prod_path} — not overwriting")
    else:
        log.info(f"No production vix_history.csv — consider copying backtest data there")


if __name__ == "__main__":
    main()
