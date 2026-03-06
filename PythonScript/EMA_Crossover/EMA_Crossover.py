"""
OpenAlgo EMA 20 / EMA 50 LONG Strategy
1 Candle Delay Confirmation
Using OpenAlgo Indicator Functions
Full NIFTY 200 Universe
Production Grade
Runs Every Hour at HH:01 IST
"""

#============================
#logic
# ========================================
# 🔁 OpenAlgo Python Bot is running.
# ========================================
# Strategy: EMA_CROSS_DELAY
# Universe: NIFTY 200
# Timeframe: 1 Hour
# Confirmation: 1 Candle Delay
# ========================================


# STEP 1 → FETCH DATA
# ----------------------------------------
# • Pull last 15 days of 1H candles
# • Remove current forming candle
# • Ensure minimum bars > (50 EMA + Delay)


# STEP 2 → CALCULATE INDICATORS
# ----------------------------------------
# EMA20 = Exponential Moving Average (20)
# EMA50 = Exponential Moving Average (50)


# STEP 3 → DETECT CROSSOVERS
# ----------------------------------------
#
# BULL CROSS condition:
#     EMA20 > EMA50
# AND Previous candle:
#     EMA20 <= EMA50
#
# BEAR CROSS condition:
#     EMA20 < EMA50
# AND Previous candle:
#     EMA20 >= EMA50


# STEP 4 → APPLY 1 CANDLE DELAY CONFIRMATION
# ----------------------------------------
#
# We check window:
#     df[-2:-1]
#
# Meaning:
#     Cross must have happened exactly 1 candles ago.
#
# BUY SIGNAL LOGIC:
#
# IF
#     • Bull cross happened exactly 1 candles ago
# AND
#     • No bear cross occurred after that
# AND
#     • Current EMA20 > EMA50
# THEN
#     → SIGNAL = BUY
#
#
# EXIT SIGNAL LOGIC:
#
# IF
#     • Latest candle shows bear cross
# THEN
#     → SIGNAL = EXIT


# STEP 5 → POSITION CHECK
# ----------------------------------------
#
# Check current position:
#     client.openposition()
#
# IF signal == BUY
#     AND no open position
#         → Place MARKET BUY order
#
# IF signal == EXIT
#     AND position exists
#         → Close position


# STEP 6 → NEXT SYMBOL
# ----------------------------------------
# Move to next stock in NIFTY 200


# ========================================
# ⏰ Scheduler
# ----------------------------------------
# Runs every hour at HH:01 IST
# ========================================

# ==============================
# IMPORTS
# ==============================

from openalgo import api
try:
    # OpenAlgo docs use: from openalgo import ta
    from openalgo import ta as oai
except Exception:
    # Backward compatibility for older layouts
    from openalgo import indicators as oai
import pandas as pd
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
import requests
import threading

print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==============================
# CONFIGURATION
# ==============================

INTERVAL = "1h"
EXCHANGE = "NSE"
PRODUCT = "CNC"
PRICE_TYPE = "MARKET"
HOLDING_PRODUCT = os.getenv("HOLDING_PRODUCT", PRODUCT)

QUANTITY = 1
LOOKBACK_DAYS = 15
MAX_BUY_PRICE = 2000

SHORT_EMA = 20
LONG_EMA = 50
DELAY_BARS = 1

MAX_WORKERS = 5
STRATEGY_NAME = "EMA_CROSS_DELAY"
safe_print("🔁 OpenAlgo Python Bot is running.")

API_KEY = os.getenv("OPENALGO_APIKEY")
HOST = os.getenv("HOST_SERVER")

if not API_KEY:
    safe_print("❌ OPENALGO_APIKEY not set")
    exit(1)

if not HOST:
    safe_print("❌ HOST_SERVER not set")
    exit(1)

def send_telegram_message(message):
    if not TELEGRAM_ENABLED:
        return
        
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        safe_print("⚠️ Telegram credentials not set")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        safe_print("Telegram Error:", e)

# ==============================
# SIGNAL MEMORY (Prevent Duplicate Signals)
# ==============================
last_signal_memory = {}

# ==============================
# HOLDINGS CACHE
# ==============================
holdings_cache = {}

client = api(api_key=API_KEY, host=HOST)

positions_cache = {}

def load_positions_cache():

    global positions_cache
    positions_cache = {}

    try:
        resp = client.positionbook()
    except Exception as e:
        safe_print("⚠️ Error fetching positions:", e)
        return

    rows = []

    if isinstance(resp, dict):
        rows = resp.get("data", [])

    elif isinstance(resp, list):
        rows = resp

    for row in rows:

        symbol = row.get("symbol")

        if not symbol:
            continue

        qty = int(float(row.get("quantity", 0)))

        positions_cache[symbol] = qty

    symbols = list(positions_cache.keys())
    safe_print(f"📊 Positions cached: {len(symbols)} symbols -> {symbols}")

def load_holdings_cache():

    global holdings_cache
    holdings_cache = {}

    try:
        resp = client.holdings()
    except Exception as e:
        safe_print("⚠️ Error fetching holdings:", e)
        return

    rows = []

    if isinstance(resp, dict):

        data = resp.get("data", {})

        if isinstance(data, dict):
            rows = data.get("holdings", [])

        elif isinstance(data, list):
            rows = data

    elif isinstance(resp, list):
        rows = resp

    for row in rows:

        symbol = row.get("symbol")

        if not symbol:
            continue

        qty = int(float(row.get("quantity", 0)))

        holdings_cache[symbol] = qty

    symbols = list(holdings_cache.keys())
    safe_print(f"📦 Holdings cached: {len(symbols)} symbols -> {symbols}")

def compute_ema(series, period):
    """Compute EMA with OpenAlgo TA when available; fallback to pandas."""
    if hasattr(oai, "ema"):
        return oai.ema(series, period=period)
    if hasattr(oai, "EMA"):
        return oai.EMA(series, period=period)
    return series.ewm(span=period, adjust=False).mean()


def interval_to_timedelta(value):
    """Convert interval like 1m/1h/1d into timedelta."""
    text = str(value).strip().lower()
    if text.endswith("m") and text[:-1].isdigit():
        return timedelta(minutes=int(text[:-1]))
    if text.endswith("h") and text[:-1].isdigit():
        return timedelta(hours=int(text[:-1]))
    if text.endswith("d") and text[:-1].isdigit():
        return timedelta(days=int(text[:-1]))
    return timedelta(hours=1)


def get_holding_qty(symbol):
    """
    Return cached holdings quantity.
    """
    return holdings_cache.get(symbol, 0)

# ==============================
# FULL NIFTY 200 LIST
# ==============================

NIFTY_200 = [
"360ONE","ABB","ACC","APLAPOLLO","AUBANK",
"ADANIENSOL","ADANIENT","ADANIGREEN","ADANIPORTS","ADANIPOWER",
"ATGL","ABCAPITAL","ALKEM","AMBUJACEM","APOLLOHOSP",
"ASHOKLEY","ASIANPAINT","ASTRAL","AUROPHARMA","DMART",
"AXISBANK","BSE","BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV",
"BAJAJHLDNG","BAJAJHFL","BANKBARODA","BANKINDIA","BDL",
"BEL","BHARATFORG","BHEL","BPCL","BHARTIARTL",
"BHARTIHEXA","BIOCON","BLUESTARCO","BOSCHLTD","BRITANNIA",
"CGPOWER","CANBK","CHOLAFIN","CIPLA","COALINDIA",
"COCHINSHIP","COFORGE","COLPAL","CONCOR","COROMANDEL",
"CUMMINSIND","DLF","DABUR","DIVISLAB","DIXON",
"DRREDDY","EICHERMOT","ETERNAL","EXIDEIND","NYKAA",
"FEDERALBNK","FORTIS","GAIL","GMRAIRPORT","GLENMARK",
"GODFRYPHLP","GODREJCP","GODREJPROP","GRASIM","HCLTECH",
"HDFCAMC","HDFCBANK","HDFCLIFE","HAVELLS","HEROMOTOCO",
"HINDALCO","HAL","HINDPETRO","HINDUNILVR","HINDZINC",
"POWERINDIA","HUDCO","HYUNDAI","ICICIBANK","ICICIGI",
"IDFCFIRSTB","IRB","ITCHOTELS","ITC","INDIANB",
"INDHOTEL","IOC","IRCTC","IRFC","IREDA",
"IGL","INDUSTOWER","INDUSINDBK","NAUKRI","INFY",
"INDIGO","JSWENERGY","JSWSTEEL","JINDALSTEL","JIOFIN",
"JUBLFOOD","KEI","KPITTECH","KALYANKJIL","KOTAKBANK",
"LTF","LICHSGFIN","LTM","LT","LICI",
"LODHA","LUPIN","MRF","M&MFIN","M&M",
"MANKIND","MARICO","MARUTI","MFSL","MAXHEALTH",
"MAZDOCK","MOTILALOFS","MPHASIS","MUTHOOTFIN","NHPC",
"NMDC","NTPCGREEN","NTPC","NATIONALUM","NESTLEIND",
"OBEROIRLTY","ONGC","OIL","PAYTM","OFSS",
"POLICYBZR","PIIND","PAGEIND","PATANJALI","PERSISTENT",
"PHOENIXLTD","PIDILITIND","POLYCAB","PFC","POWERGRID",
"PREMIERENE","PRESTIGE","PNB","RECLTD","RVNL",
"RELIANCE","SBICARD","SBILIFE","SRF","MOTHERSON",
"SHREECEM","SHRIRAMFIN","ENRIN","SIEMENS","SOLARINDS",
"SONACOMS","SBIN","SAIL","SUNPHARMA","SUPREMEIND",
"SUZLON","SWIGGY","TVSMOTOR","TATACOMM","TCS",
"TATACONSUM","TATAELXSI","TMPV","TATAPOWER","TATASTEEL",
"TATATECH","TECHM","TITAN","TORNTPHARM","TORNTPOWER",
"TRENT","TIINDIA","UPL","ULTRACEMCO","UNIONBANK",
"UNITDSPR","VBL","VEDL","VMM","IDEA",
"VOLTAS","WAAREEENER","WIPRO","YESBANK","ZYDUSLIFE"
]
# ==============================
# PROCESS SYMBOL
# ==============================

def process_symbol(symbol):

    try:
        global client

        end_date = datetime.now(ist)
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)

        df = client.history(
            symbol=symbol,
            exchange=EXCHANGE,
            interval=INTERVAL,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            source="db"
        )

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            safe_print(f"{symbol} | No Data")
            return

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if len(df) <= LONG_EMA + DELAY_BARS + 5:
            safe_print(f"{symbol} | Not Enough Bars")
            return

        # Remove current forming candle safely
        if len(df) > 1:
            df = df.iloc[:-1]
        
        # =============================
        # CALCULATE EMAs
        # =============================
        df["ema20"] = compute_ema(df["close"], SHORT_EMA)
        df["ema50"] = compute_ema(df["close"], LONG_EMA)

        df["bull_cross"] = (
            (df["ema20"] > df["ema50"]) &
            (df["ema20"].shift(1) <= df["ema50"].shift(1))
        )

        df["bear_cross"] = (
            (df["ema20"] < df["ema50"]) &
            (df["ema20"].shift(1) >= df["ema50"].shift(1))
        )

        latest = df.iloc[-1]

        ema20_latest = round(latest["ema20"], 2)
        ema50_latest = round(latest["ema50"], 2)

        last_close = float(latest["close"])
        last_close_time = df.index[-1]
        last_close_time_text = (
            last_close_time.strftime("%Y-%m-%dT%H:%M:%S")
            if hasattr(last_close_time, "strftime")
            else str(last_close_time)
        )

        # =============================
        # DYNAMIC DELAY CROSS LOGIC
        # =============================

        signal = None

        # ---------------------------------
        # DELAY = 0 → Immediate Crossover
        # ---------------------------------
        if DELAY_BARS == 0:
        
            # BUY → current candle bull cross
            if latest["bull_cross"]:
                safe_print(f"{symbol} | Immediate Bull Cross")
                signal = "BUY"

            # EXIT → current candle bear cross
            elif latest["bear_cross"]:
                safe_print(f"{symbol} | Immediate Bear Cross")
                signal = "EXIT"

        # ---------------------------------
        # DELAY >= 1 → Confirmed Crossover
        # ---------------------------------
        else:
        
            # Index of cross candle
            cross_index = -(DELAY_BARS + 1)

            cross_row = df.iloc[cross_index]
            candles_after_cross = df.iloc[cross_index + 1:]

            # ===== BUY CONDITION =====
            if cross_row["bull_cross"]:
            
                # Ensure no bear cross after original cross
                no_opposite_after = candles_after_cross["bear_cross"].sum() == 0

                # Ensure trend still bullish
                trend_valid = latest["ema20"] > latest["ema50"]

                if no_opposite_after and trend_valid:
                    safe_print(f"{symbol} | Bull Cross {DELAY_BARS} candle(s) ago")
                    signal = "BUY"

            # ===== EXIT CONDITION =====
            if cross_row["bear_cross"]:
            
                no_opposite_after = candles_after_cross["bull_cross"].sum() == 0
                trend_valid = latest["ema20"] < latest["ema50"]

                if no_opposite_after and trend_valid:
                    safe_print(f"{symbol} | Bear Cross {DELAY_BARS} candle(s) ago")
                    signal = "EXIT"

        # =============================
        # POSITION CHECK
        # =============================
        current_qty = abs(int(positions_cache.get(symbol, 0)))

        holding_qty = get_holding_qty(symbol)
        has_exitable = (current_qty > 0) or (holding_qty > 0)

        signal_text = signal if signal else "NONE"
        order_status = "NO_SIGNAL" if signal is None else "PENDING"
        # Restart-safe memory sync
        if has_exitable:
            last_signal_memory.setdefault(symbol, "BUY")

        # =============================
        # EXECUTION
        # =============================
        if signal == "BUY" and current_qty == 0:
            if last_close >= MAX_BUY_PRICE:
                safe_print(f"{symbol} | BUY Skipped (Close {round(last_close, 2)} >= {MAX_BUY_PRICE})")
                order_status = "INVALID_PRICE"
                safe_print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}"
                )
                return
        
            # Prevent duplicate BUY
            if last_signal_memory.get(symbol) == "BUY":
                safe_print(f"{symbol} | BUY Skipped (Duplicate Signal)")
                order_status = "DUPLICATE_SIGNAL"
                safe_print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}"
                )
                return

            response = client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                action="BUY",
                exchange=EXCHANGE,
                price_type=PRICE_TYPE,
                product=PRODUCT,
                quantity=QUANTITY,
            )

            safe_print(f"{symbol} | BUY | {response}")

            if response.get("status") == "success":
                last_signal_memory[symbol] = "BUY"
                order_status = "TRIGGERED"
            else:
                order_status = "FAILED"


        elif signal == "EXIT" and has_exitable:
        
            # Prevent duplicate EXIT
            if last_signal_memory.get(symbol) == "EXIT":
                safe_print(f"{symbol} | EXIT Skipped (Duplicate Signal)")
                order_status = "DUPLICATE_SIGNAL"
                safe_print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}"
                )
                return

            if current_qty > 0:
                response = client.closeposition(
                    strategy=STRATEGY_NAME,
                    symbol=symbol,
                    exchange=EXCHANGE,
                    product=PRODUCT,
                )
            else:
                response = client.placeorder(
                    strategy=STRATEGY_NAME,
                    symbol=symbol,
                    action="SELL",
                    exchange=EXCHANGE,
                    price_type=PRICE_TYPE,
                    product=HOLDING_PRODUCT,
                    quantity=holding_qty,
                )

            safe_print(f"{symbol} | EXIT | {response}")

            if response.get("status") == "success":
                last_signal_memory[symbol] = "EXIT"
                order_status = "TRIGGERED"
            else:
                order_status = "FAILED"
        elif signal == "BUY" and current_qty > 0:
            order_status = "ALREADY_IN_POSITION"
        elif signal == "EXIT" and not has_exitable:
            order_status = "NO_POSITION_TO_EXIT"

        safe_print(
            f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
            f"ema20={ema20_latest} ema50={ema50_latest} "
            f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}"
        )

        time.sleep(0.15)

    except Exception as e:
        safe_print(f"{symbol} | ERROR: {e}")

# ==============================
# MAIN STRATEGY
# ==============================

JOB_ID = "hourly_nifty200_scan"


def print_next_run():
    job = scheduler.get_job(JOB_ID) if "scheduler" in globals() else None
    if not job:
        safe_print("🕒 Next Run: unavailable")
        return
    next_run = getattr(job, "next_run_time", None)
    if next_run is None and hasattr(job, "trigger"):
        try:
            next_run = job.trigger.get_next_fire_time(None, datetime.now(ist))
        except Exception:
            next_run = None
    if not next_run:
        safe_print("🕒 Next Run: unavailable")
        return
    next_run_ist = next_run.astimezone(ist)
    safe_print(f"🕒 Next Run: {next_run_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def run_strategy():

    start_time = datetime.now(ist)

    send_telegram_message(
        f"🚀 <b>EMA 20 / EMA 50 LONG Strategy Started</b>\n"
        f"⏰ Time: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"📊 Universe: NIFTY 200\n"
        f"🕐 Timeframe: {INTERVAL}"
    )

    safe_print("\n=======================================")
    safe_print("⚡ Starting NIFTY 200 Scan")
    safe_print("=======================================\n")

    # Load holdings once
    load_holdings_cache()
    load_positions_cache()

    for sym in set(list(holdings_cache.keys()) + list(positions_cache.keys())):
        last_signal_memory[sym] = "BUY"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_symbol, s) for s in NIFTY_200]
        for _ in as_completed(futures):
            pass
    job = scheduler.get_job(JOB_ID)
    next_run = job.next_run_time.astimezone(ist)
    completion_message = (
        f"✅ <b>EMA 20 / EMA 50 LONG Strategy Completed</b>\n"
        f"🕒 Next Run: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    send_telegram_message(completion_message)
    safe_print("\n✅ Scan Completed\n")
    print_next_run()

# ==============================
# SCHEDULER (IST)
# ==============================

ist = pytz.timezone("Asia/Kolkata")
scheduler = BlockingScheduler(timezone=ist)
SCHEDULE_DELAY_MINUTES = 1

schedule_interval = interval_to_timedelta(INTERVAL)
now_ist = datetime.now(ist).replace(second=0, microsecond=0)
first_scheduled_run = now_ist + schedule_interval + timedelta(minutes=SCHEDULE_DELAY_MINUTES)
scheduler.add_job(
    run_strategy,
    trigger="interval",
    seconds=int(schedule_interval.total_seconds()),
    next_run_time=first_scheduled_run,
    id=JOB_ID,
)

safe_print(f"🕒 Scheduler Running – Every {INTERVAL} (+{SCHEDULE_DELAY_MINUTES}m after each run)")
print_next_run()
run_strategy()
scheduler.start()
