from openalgo import api
import pandas as pd
from datetime import datetime, timedelta
import os

print("🔁 OpenAlgo Python Bot is running.")

# =============================
# CONFIG
# =============================

API_KEY = os.getenv("OPENALGO_APIKEY")
HOST = os.getenv("HOST_SERVER")

SYMBOL = "SBIN"
EXCHANGE = "NSE"
INTERVAL = "1m"

# =============================
# INIT CLIENT
# =============================

client = api(api_key=API_KEY, host=HOST)

# =============================
# DATE RANGE
# =============================

end_date = datetime.now()
start_date = end_date - timedelta(days=3)

# =============================
# HISTORY CALL (Historify)
# =============================

df = client.history(
    symbol=SYMBOL,
    exchange=EXCHANGE,
    interval=INTERVAL,
    start_date=start_date.strftime("%Y-%m-%d"),
    end_date=end_date.strftime("%Y-%m-%d"),
    source="db"
)

# =============================
# OUTPUT
# =============================

if isinstance(df, pd.DataFrame) and not df.empty:
    print("\n✅ Data Loaded From Historify")
    print("Rows:", len(df))
    print(df.tail())
else:
    print("\n❌ No data returned from Historify")