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
# • Pull last 60 days of 1H candles
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

# ==============================
# CONFIGURATION
# ==============================

INTERVAL = "1h"
EXCHANGE = "NSE"
PRODUCT = "MIS"
PRICE_TYPE = "MARKET"
HOLDING_PRODUCT = os.getenv("HOLDING_PRODUCT", PRODUCT)

QUANTITY = 1
LOOKBACK_DAYS = 60
MAX_BUY_PRICE = 2000

SHORT_EMA = 20
LONG_EMA = 50
DELAY_BARS = 1

MAX_WORKERS = 8
STRATEGY_NAME = "EMA_CROSS_DELAY"
print("🔁 OpenAlgo Python Bot is running.")

API_KEY = os.getenv("OPENALGO_APIKEY")
HOST = os.getenv("HOST_SERVER")

if not API_KEY:
    print("❌ OPENALGO_APIKEY not set")
    exit(1)

if not HOST:
    print("❌ HOST_SERVER not set")
    exit(1)

# ==============================
# SIGNAL MEMORY (Prevent Duplicate Signals)
# ==============================
last_signal_memory = {}

client = api(api_key=API_KEY, host=HOST)


def compute_ema(series, period):
    """Compute EMA with OpenAlgo TA when available; fallback to pandas."""
    if hasattr(oai, "ema"):
        return oai.ema(series, period=period)
    if hasattr(oai, "EMA"):
        return oai.EMA(series, period=period)
    return series.ewm(span=period, adjust=False).mean()


def get_holding_qty(symbol):
    """
    Best-effort holding quantity resolver.
    Returns 0 when holdings API is unavailable or symbol not found.
    """
    if not hasattr(client, "holdings"):
        return 0

    try:
        resp = client.holdings()
    except Exception:
        return 0

    if isinstance(resp, dict):
        rows = resp.get("data", [])
    elif isinstance(resp, list):
        rows = resp
    else:
        return 0

    qty = 0
    for row in rows:
        if not isinstance(row, dict):
            continue

        row_symbol = (
            row.get("symbol")
            or row.get("tradingsymbol")
            or row.get("trading_symbol")
            or row.get("tsym")
            or row.get("scrip")
        )
        if row_symbol != symbol:
            continue

        for key in ("quantity", "qty", "net_qty", "holdingqty", "holding_qty"):
            if key in row:
                try:
                    qty = max(qty, int(float(row.get(key, 0))))
                except Exception:
                    pass

        # Fallback for APIs that split long/short or available qty fields
        if qty == 0:
            try:
                t1 = float(row.get("t1_quantity", 0))
                pledged = float(row.get("pledged_quantity", 0))
                qty = max(qty, int(max(t1 - pledged, 0)))
            except Exception:
                pass

    return qty

# ==============================
# FULL NIFTY 200 LIST
# ==============================

NIFTY_200 = [
"ABB","ABBOTINDIA","ABCAPITAL","ABFRL","ACC",
"ADANIENSOL","ADANIENT","ADANIGREEN","ADANIPORTS","ADANIPOWER",
"ATGL","AMBUJACEM","APOLLOHOSP","APOLLOTYRE","ASHOKLEY",
"ASIANPAINT","ASTRAL","AUROPHARMA","DMART","AXISBANK",
"BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BALKRISIND","BALRAMCHIN",
"BANDHANBNK","BANKBARODA","BANKINDIA","BATAINDIA","BERGEPAINT",
"BEL","BHARATFORG","BHEL","BPCL","BHARTIARTL",
"BIOCON","BOSCHLTD","BRITANNIA","CANBK","CHOLAFIN",
"CIPLA","COALINDIA","COFORGE","COLPAL","CONCOR",
"COROMANDEL","CROMPTON","CUMMINSIND","DABUR","DALBHARAT",
"DEEPAKNTR","DELHIVERY","DIVISLAB","DIXON","DLF",
"DRREDDY","EICHERMOT","ESCORTS","EXIDEIND","FEDERALBNK",
"GAIL","GLAND","GLENMARK","GMRINFRA","GNFC",
"GODREJCP","GODREJPROP","GRANULES","GRASIM","GUJGASLTD",
"HAL","HAVELLS","HCLTECH","HDFCAMC","HDFCBANK",
"HDFCLIFE","HEROMOTOCO","HINDALCO","HINDCOPPER","HINDPETRO",
"HINDUNILVR","ICICIBANK","ICICIGI","ICICIPRULI","IDEA",
"IDFCFIRSTB","IGL","INDHOTEL","INDIAMART","INDIGO",
"INDUSINDBK","INDUSTOWER","INFY","IOC","IRCTC",
"ITC","JINDALSTEL","JKCEMENT","JSWENERGY","JSWSTEEL",
"JUBLFOOD","KOTAKBANK","LALPATHLAB","LAURUSLABS","LICHSGFIN",
"LT","LTIM","LTTS","LUPIN","M&M",
"M&MFIN","MANAPPURAM","MARICO","MARUTI","MCDOWELL-N",
"MCX","METROPOLIS","MFSL","MGL","MOTHERSON",
"MPHASIS","MRF","MUTHOOTFIN","NATIONALUM","NAUKRI",
"NAVINFLUOR","NESTLEIND","NHPC","NMDC","NTPC",
"OBEROIRLTY","OFSS","ONGC","PAGEIND","PATANJALI",
"PAYTM","PEL","PERSISTENT","PETRONET","PFC",
"PIDILITIND","PIIND","PNB","POLYCAB","POWERGRID",
"PRESTIGE","PVRINOX","RAMCOCEM","RBLBANK","RECLTD",
"RELIANCE","SAIL","SBICARD","SBILIFE","SBIN",
"SHREECEM","SIEMENS","SRF","SUNPHARMA","SYNGENE",
"TATACHEM","TATACOMM","TATACONSUM","TATAELXSI","TATAMOTORS",
"TATAPOWER","TATASTEEL","TCS","TECHM","TIINDIA",
"TORNTPHARM","TORNTPOWER","TRENT","TVSMOTOR","UBL",
"ULTRACEMCO","UNIONBANK","UPL","VBL","VEDL",
"VOLTAS","WHIRLPOOL","WIPRO","YESBANK","ZEEL"
]

# ==============================
# PROCESS SYMBOL
# ==============================

def process_symbol(symbol):

    try:
        global client

        ist = pytz.timezone("Asia/Kolkata")
        end_date = datetime.now(ist)
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)

        df = client.history(
            symbol=symbol,
            exchange=EXCHANGE,
            interval=INTERVAL,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
        )

        if not isinstance(df, pd.DataFrame) or df.empty:
            print(f"{symbol} | No Data")
            return

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if len(df) <= LONG_EMA + DELAY_BARS + 5:
            print(f"{symbol} | Not Enough Bars")
            return

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
        recent_window = df.iloc[-DELAY_BARS-1:-1]
        cross_candle = recent_window.index[0] if not recent_window.empty else None

        ema20_latest = round(latest["ema20"], 2)
        ema50_latest = round(latest["ema50"], 2)

        trend = "BULLISH" if ema20_latest > ema50_latest else "BEARISH"

        last_close = float(latest["close"])
        last_close_time = df.index[-1]
        last_close_time_text = (
            last_close_time.strftime("%Y-%m-%dT%H:%M:%S")
            if hasattr(last_close_time, "strftime")
            else str(last_close_time)
        )
        signal = None

        # =============================
        # BUY LOGIC
        # =============================
        if (
            not recent_window.empty and
            recent_window["bull_cross"].iloc[0] and
            recent_window["bear_cross"].sum() == 0 and
            latest["ema20"] > latest["ema50"]
        ):
            print(f"{symbol} | Bull Cross at {cross_candle}")
            print(f"{symbol} | BUY Signal Confirmed")
            signal = "BUY"

        # =============================
        # EXIT LOGIC
        # =============================
        if latest["bear_cross"]:
            bear_cross_time = df.index[-1]
            print(f"{symbol} | Bear Cross at {bear_cross_time}")
            print(f"{symbol} | EXIT Signal Confirmed")
            signal = "EXIT"

        # =============================
        # POSITION CHECK
        # =============================
        pos = client.openposition(
            strategy=STRATEGY_NAME,
            symbol=symbol,
            exchange=EXCHANGE,
            product=PRODUCT,
        )

        current_qty = 0
        if pos and pos.get("status") == "success":
            current_qty = abs(int(pos.get("quantity", 0)))

        holding_qty = get_holding_qty(symbol)
        has_exitable = (current_qty > 0) or (holding_qty > 0)

        signal_text = signal if signal else "NONE"
        order_status = "NO_SIGNAL" if signal is None else "PENDING"
        # Restart-safe memory sync
        if has_exitable:
            last_signal_memory[symbol] = "BUY"

        # =============================
        # EXECUTION
        # =============================
        if signal == "BUY" and current_qty == 0:
            if last_close >= MAX_BUY_PRICE:
                print(f"{symbol} | BUY Skipped (Close {round(last_close, 2)} >= {MAX_BUY_PRICE})")
                order_status = "INVALID_PRICE"
                print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}",
                    flush=True,
                )
                return
        
            # Prevent duplicate BUY
            if last_signal_memory.get(symbol) == "BUY":
                print(f"{symbol} | BUY Skipped (Duplicate Signal)")
                order_status = "DUPLICATE_SIGNAL"
                print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}",
                    flush=True,
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

            print(f"{symbol} | BUY | {response}")

            if response.get("status") == "success":
                last_signal_memory[symbol] = "BUY"
                order_status = "TRIGGERED"
            else:
                order_status = "FAILED"


        elif signal == "EXIT" and has_exitable:
        
            # Prevent duplicate EXIT
            if last_signal_memory.get(symbol) == "EXIT":
                print(f"{symbol} | EXIT Skipped (Duplicate Signal)")
                order_status = "DUPLICATE_SIGNAL"
                print(
                    f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
                    f"ema20={ema20_latest} ema50={ema50_latest} "
                    f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}",
                    flush=True,
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

            print(f"{symbol} | EXIT | {response}")

            if response.get("status") == "success":
                last_signal_memory[symbol] = "EXIT"
                order_status = "TRIGGERED"
            else:
                order_status = "FAILED"
        elif signal == "BUY" and current_qty > 0:
            order_status = "ALREADY_IN_POSITION"
        elif signal == "EXIT" and not has_exitable:
            order_status = "NO_POSITION_TO_EXIT"

        print(
            f"{symbol} | close_time={last_close_time_text} close={round(last_close, 2)} "
            f"ema20={ema20_latest} ema50={ema50_latest} "
            f"signal={signal_text} position={current_qty} holdings={holding_qty} order_status={order_status}",
            flush=True,
        )

        time.sleep(0.05)

    except Exception as e:
        print(f"{symbol} | ERROR: {e}")

# ==============================
# MAIN STRATEGY
# ==============================

JOB_ID = "hourly_nifty200_scan"


def print_next_run():
    job = scheduler.get_job(JOB_ID) if "scheduler" in globals() else None
    if not job or not job.next_run_time:
        print("🕒 Next Run: unavailable")
        return
    next_run_ist = job.next_run_time.astimezone(ist)
    print(f"🕒 Next Run: {next_run_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def run_strategy():

    print("\n=======================================")
    print("⚡ Starting NIFTY 200 Scan")
    print("=======================================\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_symbol, s) for s in NIFTY_200]
        for _ in as_completed(futures):
            pass

    print("\n✅ Scan Completed\n")
    print_next_run()

# ==============================
# SCHEDULER (IST)
# ==============================

ist = pytz.timezone("Asia/Kolkata")
scheduler = BlockingScheduler(timezone=ist)

scheduler.add_job(
    run_strategy,
    trigger="cron",
    minute=1,
    id=JOB_ID,
)

print("🕒 Scheduler Running – Every Hour at HH:01 IST")
print_next_run()
run_strategy() 
scheduler.start()
