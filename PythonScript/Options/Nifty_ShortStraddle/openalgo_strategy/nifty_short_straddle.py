"""
Nifty Short Straddle — OpenAlgo Python Strategy (Single File)
SELL ATM CE + PE | Weekly Expiry | Intraday MIS | Independent Per-Leg SL
"""

from __future__ import annotations
import os, json, threading, time, signal
from datetime import datetime, timedelta

import pytz
from openalgo import api as OpenAlgo

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION — All backtest-optimised parameters
# ─────────────────────────────────────────────────────────────────────────────

OPENALGO_HOST    = os.environ.get("OPENALGO_HOST", "https://myalgo.algotradings.in")
OPENALGO_WS_URL  = os.environ.get("OPENALGO_WS_URL", "wss://myalgo.algotradings.in/ws")
OPENALGO_API_KEY = os.environ.get("OPENALGO_APIKEY", "")
TELEGRAM_USER    = os.environ.get("OPENALGO_USERNAME", "rupanmayan")

UNDERLYING       = "NIFTY"
EXCHANGE         = "NSE_INDEX"
OPTION_EXCH      = "NFO"
LOT_SIZE         = 65
NUMBER_OF_LOTS   = 1
PRODUCT          = "MIS"
STRATEGY_NAME    = "Short Straddle"
STRIKE_ROUNDING  = 50

ENTRY_TIME       = "09:17"
EXIT_TIME        = "15:15"
MONITOR_INTERVAL = 5

TRADE_DTE        = [0, 1, 2, 3, 4]
SKIP_MONTHS      = [11]

LEG_SL_PERCENT   = 30.0
DAILY_TARGET     = 10000
DAILY_LOSS_LIMIT = -6000
NET_PNL_GUARD_MAX_DEFER_MIN = 15

MARGIN_GUARD_ENABLED = True
MARGIN_BUFFER        = 1.20
MARGIN_FAIL_OPEN     = True

VIX_SPIKE_ENABLED      = True
VIX_SPIKE_THRESHOLD    = 15.0
VIX_SPIKE_ABS_FLOOR    = 18.0
VIX_SPIKE_INTERVAL_S   = 300

COMBINED_DECAY_ENABLED = True
COMBINED_DECAY_DEFAULT = 60.0
COMBINED_DECAY_DTE_MAP = {0: 60.0, 1: 65.0, 2: 60.0, 3: 50.0, 4: 50.0}

WINNER_BOOKING_ENABLED       = True
WINNER_BOOKING_DECAY_PCT     = 30.0

ASYMMETRIC_ENABLED           = True
ASYMMETRIC_WINNER_DECAY_PCT  = 40.0
ASYMMETRIC_LOSER_INTACT_PCT  = 80.0

COMBINED_TRAIL_ENABLED       = True
COMBINED_TRAIL_ACTIVATE_PCT  = 30.0
COMBINED_TRAIL_PCT           = 40.0

BREAKEVEN_ENABLED            = True
BREAKEVEN_GRACE_MIN          = 5
BREAKEVEN_BUFFER_PCT         = 5.0

REENTRY_ENABLED              = True
REENTRY_COOLDOWN_MIN         = 45
REENTRY_MAX_PER_DAY          = 2
REENTRY_MAX_LOSS             = 2000

WS_ENABLED             = True
WS_STALENESS_S         = 60
WS_RECONNECT_MAX_S     = 30

TELEGRAM_ENABLED       = True
QUOTE_FAIL_THRESHOLD   = 3
STATE_FILE             = "strategy_state.json"
TRADE_LOG_FILE         = "trades.jsonl"

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)

def qty() -> int:
    return NUMBER_OF_LOTS * LOT_SIZE

def parse_hhmm(t: str) -> tuple[int, int]:
    h, m = t.strip().split(":")
    return int(h), int(m)

def plog(msg: str, level: str = "INFO"):
    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level:<8}] {msg}", flush=True)

def plog_sep():
    plog("─" * 60)

# ─────────────────────────────────────────────────────────────────────────────
#  BROKER CLIENT (lazy singleton)
# ─────────────────────────────────────────────────────────────────────────────

class BrokerClient:
    def __init__(self):
        self._instance = None
        self._lock = threading.Lock()

    def get(self) -> OpenAlgo:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = OpenAlgo(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)
        return self._instance

broker = BrokerClient()

def api_ok(resp) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "success"

def api_err(resp) -> str:
    return resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)

# ─────────────────────────────────────────────────────────────────────────────
#  LTP CACHE (populated by WebSocket, consumed by fetch_ltp)
# ─────────────────────────────────────────────────────────────────────────────

_ltp_cache: dict[str, tuple[float, datetime]] = {}
_ltp_cache_lock = threading.Lock()

def update_ltp_cache(symbol: str, exchange: str, ltp: float):
    key = f"{symbol}.{exchange}"
    with _ltp_cache_lock:
        _ltp_cache[key] = (ltp, now_ist())

def get_ltp_cache(symbol: str, exchange: str) -> float:
    key = f"{symbol}.{exchange}"
    with _ltp_cache_lock:
        entry = _ltp_cache.get(key)
    if entry is None:
        return 0.0
    ltp, ts = entry
    if (now_ist() - ts).total_seconds() > WS_STALENESS_S:
        return 0.0
    return ltp

def fetch_ltp(symbol: str, exchange: str) -> float:
    cached = get_ltp_cache(symbol, exchange)
    if cached > 0:
        return cached
    try:
        resp = broker.get().quotes(symbol=symbol, exchange=exchange)
        if api_ok(resp):
            ltp = float(resp.get("data", {}).get("ltp", 0) or 0)
            return ltp if ltp > 0 else 0.0
    except Exception as exc:
        plog(f"fetch_ltp({symbol}): {exc}", "DEBUG")
    return 0.0

# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFIER (async background queue)
# ─────────────────────────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self):
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._has_msg = threading.Event()
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = OpenAlgo(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)
        return self._client

    def start(self):
        if not TELEGRAM_ENABLED or not TELEGRAM_USER:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True, name="telegram")
        self._thread.start()

    def notify(self, msg: str):
        if not TELEGRAM_ENABLED or not TELEGRAM_USER:
            return
        with self._lock:
            self._queue.append(msg[:4096])
        self._has_msg.set()

    def _worker(self):
        while not self._stop.is_set():
            self._has_msg.wait(timeout=5)
            self._has_msg.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    msg = self._queue.pop(0)
                self._send(msg)

    def _send(self, msg: str):
        for attempt in range(3):
            try:
                client = self._get_client()
                resp = client.telegram(
                    message=msg,
                    username=TELEGRAM_USER,
                )
                if api_ok(resp):
                    return
                plog(f"Telegram attempt {attempt+1}: {api_err(resp)}", "WARNING")
            except Exception as exc:
                plog(f"Telegram attempt {attempt+1}: {exc}", "WARNING")
            time.sleep(1 * (attempt + 1))

    def send_sync(self, msg: str):
        if not TELEGRAM_ENABLED or not TELEGRAM_USER:
            return
        self._send(msg)

    def flush(self):
        for _ in range(50):
            with self._lock:
                if not self._queue:
                    return
            time.sleep(0.1)

    def stop(self):
        self._stop.set()
        self._has_msg.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

telegram = TelegramNotifier()

# ─────────────────────────────────────────────────────────────────────────────
#  ATOMIC STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

INITIAL_STATE = {
    "in_position": False,
    "ce_active": False, "pe_active": False,
    "symbol_ce": "", "symbol_pe": "",
    "orderid_ce": "", "orderid_pe": "",
    "entry_price_ce": 0.0, "entry_price_pe": 0.0,
    "exit_price_ce": 0.0, "exit_price_pe": 0.0,
    "closed_pnl": 0.0, "today_pnl": 0.0,
    "underlying_ltp": 0.0,
    "entry_time": "", "entry_date": "",
    "vix_at_entry": 0.0,
    "margin_required": 0.0, "margin_available": 0.0,
    "trailing_active_ce": False, "trailing_active_pe": False,
    "trailing_sl_ce": 0.0, "trailing_sl_pe": 0.0,
    "breakeven_active_ce": False, "breakeven_active_pe": False,
    "breakeven_sl_ce": 0.0, "breakeven_sl_pe": 0.0,
    "breakeven_activated_at_ce": None, "breakeven_activated_at_pe": None,
    "recovery_lock_active": False, "recovery_peak_pnl": 0.0,
    "combined_trail_active": False, "combined_decay_peak": 0.0,
    "net_pnl_defer_start_ce": None, "net_pnl_defer_start_pe": None,
    "sl_events": [], "filters_passed": [],
    "is_reentry": False, "current_dte": None,
    "cumulative_daily_pnl": 0.0,
    "last_close_time": None, "last_trade_pnl": 0.0,
    "reentry_count_today": 0, "trade_count": 0,
    "exit_reason": "",
}

state: dict = dict(INITIAL_STATE)

def save_state():
    try:
        data = {}
        for k, v in state.items():
            if isinstance(v, datetime):
                data[k] = v.isoformat()
            else:
                data[k] = v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        plog(f"save_state failed: {exc}", "ERROR")

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in state:
                state[k] = v
        for key in ["entry_time", "last_close_time", "breakeven_activated_at_ce",
                     "breakeven_activated_at_pe", "net_pnl_defer_start_ce", "net_pnl_defer_start_pe"]:
            raw = state.get(key)
            if raw and isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw)
                    state[key] = parsed if parsed.tzinfo else IST.localize(parsed)
                except (ValueError, TypeError):
                    pass
        plog(f"State loaded: in_position={state['in_position']}")
    except Exception as exc:
        plog(f"load_state failed: {exc}", "ERROR")

def clear_state_file():
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass

def reset_state():
    for k, v in INITIAL_STATE.items():
        if isinstance(v, (list, dict)):
            state[k] = type(v)()
        else:
            state[k] = v

# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET FEED (daemon thread, auto-reconnect)
# ─────────────────────────────────────────────────────────────────────────────

class WebSocketFeed:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribed: set[str] = set()
        self._ws = None

    def start(self):
        if not WS_ENABLED:
            return
        self._thread = threading.Thread(target=self._connection_loop, daemon=True, name="ws-feed")
        self._thread.start()
        plog("WebSocket feed started")

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                plog("WS thread did not stop in 5s", "WARNING")
        self._ws = None
        plog("WebSocket feed stopped")

    def subscribe(self, symbol: str, exchange: str):
        key = f"{symbol}.{exchange}"
        self._subscribed.add(key)
        if self._ws:
            try:
                self._ws.send(json.dumps({
                    "action": "subscribe", "mode": 1,
                    "symbols": [{"exchange": exchange, "symbol": symbol}],
                }))
            except Exception:
                pass

    def unsubscribe(self, symbol: str, exchange: str):
        key = f"{symbol}.{exchange}"
        self._subscribed.discard(key)
        if self._ws:
            try:
                self._ws.send(json.dumps({
                    "action": "unsubscribe", "mode": 1,
                    "symbols": [{"exchange": exchange, "symbol": symbol}],
                }))
            except Exception:
                pass

    def subscribe_position_symbols(self):
        if state["ce_active"] and state["symbol_ce"]:
            self.subscribe(state["symbol_ce"], OPTION_EXCH)
        if state["pe_active"] and state["symbol_pe"]:
            self.subscribe(state["symbol_pe"], OPTION_EXCH)

    def subscribe_market_symbols(self):
        self.subscribe(UNDERLYING, EXCHANGE)
        self.subscribe("INDIAVIX", EXCHANGE)

    def unsubscribe_position_symbols(self):
        if state["symbol_ce"]:
            self.unsubscribe(state["symbol_ce"], OPTION_EXCH)
        if state["symbol_pe"]:
            self.unsubscribe(state["symbol_pe"], OPTION_EXCH)

    def _connection_loop(self):
        try:
            import websockets.sync.client as ws_sync  # noqa: F811
        except ImportError:
            plog("websockets package not installed — WS feed disabled", "WARNING")
            return

        delay = 1
        while not self._stop.is_set():
            try:
                ws_url = OPENALGO_WS_URL if OPENALGO_WS_URL else (OPENALGO_HOST.replace("http", "ws") + "/ws/market-data")
                plog(f"WS connecting to {ws_url}")
                with ws_sync.connect(ws_url, close_timeout=5) as ws:
                    self._ws = ws
                    delay = 1
                    auth_msg = json.dumps({"action": "authenticate", "api_key": OPENALGO_API_KEY})
                    ws.send(auth_msg)
                    auth_resp = json.loads(ws.recv(timeout=10))
                    if auth_resp.get("type") != "auth" or auth_resp.get("status") != "success":
                        plog(f"WS auth failed: {auth_resp}", "WARNING")
                        time.sleep(delay)
                        continue
                    plog("WebSocket authenticated")
                    symbols = []
                    for key in list(self._subscribed):
                        sym, exch = key.split(".", 1)
                        symbols.append({"exchange": exch, "symbol": sym})
                    if symbols:
                        ws.send(json.dumps({"action": "subscribe", "mode": 1, "symbols": symbols}))
                        plog(f"WS subscribed to {len(symbols)} symbols")
                    self._receive_loop(ws)
            except Exception as exc:
                if not self._stop.is_set():
                    plog(f"WS connection error: {exc} — reconnecting in {delay}s", "WARNING")
            time.sleep(min(delay, WS_RECONNECT_MAX_S))
            delay = min(delay * 2, WS_RECONNECT_MAX_S)

    def _receive_loop(self, ws):
        msg_count = 0
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=30)
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                if msg_type in ("market_data", "ltp", "quote"):
                    data = msg.get("data", {})
                    sym = data.get("symbol", "") or msg.get("symbol", "")
                    exch = data.get("exchange", "") or msg.get("exchange", "")
                    ltp = float(data.get("ltp", 0) or msg.get("ltp", 0))
                    if sym and ltp > 0:
                        update_ltp_cache(sym, exch, ltp)
                        msg_count += 1
                        if msg_count == 1:
                            plog(f"WS first tick: {sym} ₹{ltp:.2f}")
                elif msg_type == "subscribe":
                    pass  # subscription confirmation — expected
                elif msg_type == "ping":
                    ws.send(json.dumps({"type": "pong"}))
                elif msg_count == 0:
                    plog(f"WS unexpected message: {str(raw)[:200]}", "DEBUG")
            except Exception as exc:
                plog(f"WS receive loop ended: {exc}", "DEBUG")
                break

ws_feed = WebSocketFeed()

# ─────────────────────────────────────────────────────────────────────────────
#  VIX MANAGER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_vix() -> float:
    cached = get_ltp_cache("INDIAVIX", EXCHANGE)
    if cached > 0:
        return cached
    try:
        resp = broker.get().quotes(symbol="INDIAVIX", exchange=EXCHANGE)
        if api_ok(resp):
            return float(resp.get("data", {}).get("ltp", 0) or 0)
    except Exception:
        pass
    return 0.0

# ─────────────────────────────────────────────────────────────────────────────
#  EXPIRY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_expiry(raw: str) -> str:
    return raw.replace("-", "").upper()

def resolve_expiry() -> str | None:
    try:
        resp = broker.get().expiry(symbol=UNDERLYING, exchange=OPTION_EXCH, instrumenttype="options")
        if api_ok(resp):
            expiries = resp.get("data", [])
            if expiries:
                return _normalize_expiry(expiries[0])
    except Exception as exc:
        plog(f"Expiry API failed: {exc}", "WARNING")

    today = now_ist().date()
    tue = 1
    days_ahead = (tue - today.weekday()) % 7
    if days_ahead == 0 and today.weekday() == tue:
        days_ahead = 0
    elif days_ahead == 0:
        days_ahead = 7
    nxt = today + timedelta(days=days_ahead)
    return nxt.strftime("%d%b%y").upper()

_holiday_cache: dict[int, set[str]] = {}

def _load_holidays(year: int) -> set[str]:
    if year in _holiday_cache:
        return _holiday_cache[year]
    holidays_set: set[str] = set()
    try:
        resp = broker.get().holidays(year=year)
        if api_ok(resp):
            for h in resp.get("data", []):
                if h.get("holiday_type") == "TRADING_HOLIDAY":
                    closed = [e.upper() for e in h.get("closed_exchanges", [])]
                    if "NSE" in closed or "NFO" in closed:
                        holidays_set.add(h["date"])
        _holiday_cache[year] = holidays_set
    except Exception as exc:
        plog(f"Holiday API error: {exc}", "WARNING")
        _holiday_cache[year] = holidays_set
    return holidays_set

def compute_dte(expiry_str: str) -> int:
    try:
        for fmt in ("%d-%b-%y", "%d%b%y", "%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
            try:
                exp_date = datetime.strptime(expiry_str.upper(), fmt).date()
                break
            except ValueError:
                continue
        else:
            return -1
        today = now_ist().date()
        holidays = _load_holidays(today.year)
        if exp_date.year != today.year:
            holidays = holidays | _load_holidays(exp_date.year)
        trading_days = 0
        d = today
        while d < exp_date:
            d += timedelta(days=1)
            if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays:
                trading_days += 1
        return trading_days
    except Exception:
        return -1

# ─────────────────────────────────────────────────────────────────────────────
#  SL CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def parse_ist_dt(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else IST.localize(raw)
    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else IST.localize(parsed)
    except (ValueError, TypeError):
        return None

def sl_level(leg: str) -> float:
    leg_l = leg.lower()
    entry = state[f"entry_price_{leg_l}"]
    if entry <= 0:
        other = "ce" if leg_l == "pe" else "pe"
        other_entry = state.get(f"entry_price_{other}", 0.0)
        if other_entry > 0:
            return round(other_entry * (1.0 + LEG_SL_PERCENT / 100.0), 2)
        return 0.0

    fixed_sl = round(entry * (1.0 + LEG_SL_PERCENT / 100.0), 2)

    if BREAKEVEN_ENABLED and state.get(f"breakeven_active_{leg_l}", False):
        be_sl = state.get(f"breakeven_sl_{leg_l}", 0.0)
        if be_sl > 0 and be_sl < fixed_sl:
            if not BREAKEVEN_GRACE_MIN:
                return be_sl
            be_at = parse_ist_dt(state.get(f"breakeven_activated_at_{leg_l}"))
            if be_at is None:
                return be_sl
            elapsed = (now_ist() - be_at).total_seconds() / 60.0
            if elapsed >= BREAKEVEN_GRACE_MIN:
                return be_sl

    return fixed_sl

def active_legs() -> list[str]:
    return [leg for leg, key in (("CE", "ce_active"), ("PE", "pe_active")) if state[key]]

# ─────────────────────────────────────────────────────────────────────────────
#  MARGIN GUARD
# ─────────────────────────────────────────────────────────────────────────────

def margin_check(symbol_ce: str, symbol_pe: str) -> bool:
    if not MARGIN_GUARD_ENABLED:
        return True
    try:
        funds_resp = broker.get().funds()
        if not api_ok(funds_resp):
            plog(f"Funds API error: {api_err(funds_resp)}", "WARNING")
            return MARGIN_FAIL_OPEN

        data = funds_resp.get("data", {})
        available = float(data.get("availablecash", 0) or 0) + float(data.get("collateral", 0) or 0)

        basket = [
            {"symbol": symbol_ce, "exchange": OPTION_EXCH, "action": "SELL",
             "quantity": qty(), "pricetype": "MARKET", "product": PRODUCT},
            {"symbol": symbol_pe, "exchange": OPTION_EXCH, "action": "SELL",
             "quantity": qty(), "pricetype": "MARKET", "product": PRODUCT},
        ]
        margin_resp = broker.get().margin(positions=basket)
        if not api_ok(margin_resp):
            plog(f"Margin API error: {api_err(margin_resp)}", "WARNING")
            return MARGIN_FAIL_OPEN

        required = float(margin_resp.get("data", {}).get("total_margin_required", 0) or 0)
        buffered = required * MARGIN_BUFFER

        state["margin_required"] = required
        state["margin_available"] = available

        if available < buffered:
            plog(f"Margin INSUFFICIENT: available ₹{available:,.0f} < required ₹{buffered:,.0f}", "WARNING")
            telegram.notify(f"⚠️ Margin insufficient\nAvailable: ₹{available:,.0f}\nRequired: ₹{buffered:,.0f}")
            return False

        plog(f"Margin OK: ₹{available:,.0f} available, ₹{required:,.0f} required ({MARGIN_BUFFER:.0%} buffer)")
        return True

    except Exception as exc:
        plog(f"Margin check exception: {exc}", "ERROR")
        return MARGIN_FAIL_OPEN

# ─────────────────────────────────────────────────────────────────────────────
#  ORDER ENGINE
# ─────────────────────────────────────────────────────────────────────────────

_monitor_lock = threading.RLock()

class OrderEngine:
    def __init__(self):
        self._first_tick_fired = False
        self._consecutive_quote_fails = 0
        self._quote_fail_alerted = False

    def reset_entry_state(self):
        self._first_tick_fired = False
        self._consecutive_quote_fails = 0
        self._quote_fail_alerted = False

    def place_entry(self, expiry: str) -> bool:
        spot = fetch_ltp(UNDERLYING, EXCHANGE)
        if spot <= 0:
            plog("Cannot fetch NIFTY spot — skipping entry", "ERROR")
            return False

        atm_strike = round(spot / STRIKE_ROUNDING) * STRIKE_ROUNDING
        plog(f"Entry: ATM={atm_strike} | expiry={expiry}")

        # Pre-compute symbols for margin check (still needed for margin API)
        symbol_ce = f"{UNDERLYING}{expiry}{atm_strike}CE"
        symbol_pe = f"{UNDERLYING}{expiry}{atm_strike}PE"

        if not margin_check(symbol_ce, symbol_pe):
            return False

        legs = [
            {"offset": "ATM", "option_type": "CE", "action": "SELL",
             "quantity": qty(), "pricetype": "MARKET", "product": PRODUCT,
             "expiry_date": expiry},
            {"offset": "ATM", "option_type": "PE", "action": "SELL",
             "quantity": qty(), "pricetype": "MARKET", "product": PRODUCT,
             "expiry_date": expiry},
        ]

        try:
            resp = broker.get().optionsmultiorder(
                strategy=STRATEGY_NAME,
                underlying=UNDERLYING,
                exchange=EXCHANGE,
                legs=legs,
            )
            if not api_ok(resp):
                plog(f"Options multi-order failed: {api_err(resp)}", "ERROR")
                telegram.notify(f"❌ Options multi-order failed: {api_err(resp)}")
                return False

            results = resp.get("results", [])
            if len(results) < 2:
                plog(f"Multi-order returned {len(results)} results, expected 2", "ERROR")
                return False

            # Check both legs succeeded
            ce_result, pe_result = results[0], results[1]
            ce_ok = ce_result.get("status") == "success"
            pe_ok = pe_result.get("status") == "success"

            if not ce_ok or not pe_ok:
                failed = []
                if not ce_ok: failed.append(f"CE: {ce_result}")
                if not pe_ok: failed.append(f"PE: {pe_result}")
                plog(f"Multi-order partial failure: {', '.join(failed)}", "ERROR")
                telegram.notify(f"❌ Multi-order partial failure\n{chr(10).join(failed)}")
                broker.get().closeposition(strategy=STRATEGY_NAME)
                return False

            # Extract resolved symbols and order IDs from response
            symbol_ce = ce_result.get("symbol", symbol_ce)
            symbol_pe = pe_result.get("symbol", symbol_pe)
            orderid_ce = str(ce_result.get("orderid", ""))
            orderid_pe = str(pe_result.get("orderid", ""))

            # Use underlying_ltp from response if available
            resp_spot = resp.get("underlying_ltp", 0)
            if resp_spot > 0:
                spot = resp_spot

            plog(f"Multi-order OK: CE={symbol_ce} ({orderid_ce}), PE={symbol_pe} ({orderid_pe})")

            # Verify order status at broker — catch rejections early
            time.sleep(2)
            for leg_name, oid in [("CE", orderid_ce), ("PE", orderid_pe)]:
                if not oid:
                    continue
                try:
                    os_resp = broker.get().orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                    if api_ok(os_resp):
                        os_data = os_resp.get("data", {})
                        os_status = os_data.get("order_status", "")
                        if os_status in ("rejected", "cancelled"):
                            plog(f"Entry {leg_name} order {oid} was {os_status} by broker", "ERROR")
                            telegram.notify(f"❌ Entry {leg_name} order {os_status} by broker\nOrder ID: {oid}")
                            broker.get().closeposition(strategy=STRATEGY_NAME)
                            return False
                        plog(f"Entry {leg_name} order verified: {os_status}")
                except Exception as vexc:
                    plog(f"Order verification {leg_name} failed: {vexc}", "WARNING")

        except Exception as exc:
            plog(f"Order placement exception: {exc}", "ERROR")
            return False

        now_dt = now_ist()
        fresh_spot = fetch_ltp(UNDERLYING, EXCHANGE) or spot

        with _monitor_lock:
            state["in_position"] = True
            state["ce_active"] = True
            state["pe_active"] = True
            state["symbol_ce"] = symbol_ce
            state["symbol_pe"] = symbol_pe
            state["orderid_ce"] = orderid_ce
            state["orderid_pe"] = orderid_pe
            state["underlying_ltp"] = fresh_spot
            state["entry_time"] = now_dt.isoformat()
            state["entry_date"] = now_dt.strftime("%Y-%m-%d")
            state["exit_reason"] = ""
            state["trailing_active_ce"] = False
            state["trailing_active_pe"] = False
            state["trailing_sl_ce"] = 0.0
            state["trailing_sl_pe"] = 0.0
            state["breakeven_active_ce"] = False
            state["breakeven_active_pe"] = False
            state["breakeven_sl_ce"] = 0.0
            state["breakeven_sl_pe"] = 0.0
            state["breakeven_activated_at_ce"] = None
            state["breakeven_activated_at_pe"] = None
            state["net_pnl_defer_start_ce"] = None
            state["net_pnl_defer_start_pe"] = None
            state["recovery_lock_active"] = False
            state["recovery_peak_pnl"] = 0.0
            state["combined_trail_active"] = False
            state["combined_decay_peak"] = 0.0
            state["sl_events"] = []
            state["filters_passed"] = []
            save_state()

        self.reset_entry_state()
        ws_feed.subscribe_position_symbols()

        threading.Thread(target=self._capture_fills, daemon=True, name="fill-capture").start()

        vix = fetch_vix()
        state["vix_at_entry"] = vix

        entry_msg = (
            f"📈 ENTRY — {STRATEGY_NAME}\n"
            f"CE: {symbol_ce}\nPE: {symbol_pe}\n"
            f"Spot: {fresh_spot:.2f} | ATM: {atm_strike}\n"
            f"VIX: {vix:.2f} | DTE: {state.get('current_dte', '?')}\n"
            f"Lots: {NUMBER_OF_LOTS} × {LOT_SIZE} = {qty()}"
        )
        telegram.notify(entry_msg)
        plog(entry_msg.replace("\n", " | "))

        return True

    def _capture_fills(self):
        time.sleep(3)
        for leg, oid_key, price_key in [("CE", "orderid_ce", "entry_price_ce"),
                                          ("PE", "orderid_pe", "entry_price_pe")]:
            order_id = state.get(oid_key, "")
            if not order_id:
                plog(f"Fill capture {leg}: no order ID — skipping", "WARNING")
                continue
            for attempt in range(5):
                try:
                    resp = broker.get().orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                    if api_ok(resp):
                        data = resp.get("data", {})
                        avg_price = float(data.get("average_price", 0) or 0)
                        order_status = data.get("order_status", "")
                        if avg_price > 0 and order_status == "complete":
                            with _monitor_lock:
                                if state[price_key] <= 0:
                                    state[price_key] = avg_price
                                    plog(f"Fill captured {leg}: ₹{avg_price:.2f} (orderstatus)")
                            break
                        elif order_status in ("rejected", "cancelled"):
                            plog(f"Fill capture {leg}: order {order_status}", "ERROR")
                            break
                except Exception as exc:
                    plog(f"Fill capture {leg} attempt {attempt+1} error: {exc}", "WARNING")
                time.sleep(2)

        with _monitor_lock:
            ce_px = state["entry_price_ce"]
            pe_px = state["entry_price_pe"]
            if ce_px > 0 and pe_px > 0:
                combined = ce_px + pe_px
                plog(f"Combined premium: ₹{combined:.2f} (CE={ce_px:.2f} + PE={pe_px:.2f})")
            save_state()

    def close_one_leg(self, leg: str, reason: str, current_ltp: float = 0.0):
        leg_l = leg.lower()
        sym_key = f"symbol_{leg_l}"
        entry_key = f"entry_price_{leg_l}"
        exit_key = f"exit_price_{leg_l}"
        active_key = f"{leg_l}_active"

        if not state[active_key]:
            return

        symbol = state[sym_key]
        plog(f"Closing {leg} ({symbol}): {reason}")

        fill_price = 0.0
        order_sent = False
        exit_order_id = ""
        for attempt in range(3):
            try:
                resp = broker.get().placesmartorder(
                    symbol=symbol, exchange=OPTION_EXCH,
                    action="BUY", quantity=qty(),
                    price_type="MARKET", product=PRODUCT,
                    position_size=0,
                )
                if api_ok(resp):
                    order_sent = True
                    exit_order_id = str(resp.get("data", {}).get("orderid", "") or resp.get("orderid", ""))
                    time.sleep(2)
                    if exit_order_id:
                        try:
                            os_resp = broker.get().orderstatus(order_id=exit_order_id, strategy=STRATEGY_NAME)
                            if api_ok(os_resp):
                                fill_price = float(os_resp.get("data", {}).get("average_price", 0) or 0)
                        except Exception:
                            pass
                    if fill_price <= 0:
                        fill_price = fetch_ltp(symbol, OPTION_EXCH) or current_ltp
                    break
                plog(f"Close {leg} attempt {attempt+1} failed: {api_err(resp)}", "WARNING")
            except Exception as exc:
                plog(f"Close {leg} attempt {attempt+1} error: {exc}", "WARNING")
            time.sleep(1)

        if not order_sent:
            plog(f"CRITICAL: Could not close {leg} after 3 attempts — leg still open", "ERROR")
            telegram.notify(f"🚨 CRITICAL: Failed to close {leg} ({symbol}) after 3 attempts. Position still open!")
            return

        if fill_price <= 0:
            fill_price = current_ltp if current_ltp > 0 else fetch_ltp(symbol, OPTION_EXCH)

        entry_px = state[entry_key]
        leg_pnl = (entry_px - fill_price) * qty() if entry_px > 0 and fill_price > 0 else 0.0

        with _monitor_lock:
            state[active_key] = False
            state[exit_key] = fill_price
            state["closed_pnl"] += leg_pnl
            state["sl_events"].append({
                "leg": leg, "reason": reason, "entry": entry_px,
                "exit": fill_price, "pnl": round(leg_pnl, 2),
                "time": now_ist().isoformat(),
            })

            other_leg = "pe" if leg_l == "ce" else "ce"
            other_active = state[f"{other_leg}_active"]

            if other_active:
                ws_feed.unsubscribe(symbol, OPTION_EXCH)
                self._activate_breakeven_if_needed(other_leg)
                save_state()

                partial_msg = (
                    f"🔶 PARTIAL EXIT — {leg} closed\n"
                    f"Reason: {reason}\n"
                    f"Entry: ₹{entry_px:.2f} → Exit: ₹{fill_price:.2f}\n"
                    f"Leg P&L: ₹{leg_pnl:,.2f}\n"
                    f"Closed P&L: ₹{state['closed_pnl']:,.2f}\n"
                    f"Survivor: {other_leg.upper()}"
                )
                telegram.notify(partial_msg)
                plog(partial_msg.replace("\n", " | "))
            else:
                self._mark_fully_flat(reason)

    def _activate_breakeven_if_needed(self, survivor_leg: str):
        if not BREAKEVEN_ENABLED:
            return
        if state["closed_pnl"] >= 0:
            return

        survivor_entry = state[f"entry_price_{survivor_leg}"]
        if survivor_entry <= 0:
            return

        survivor_ltp = fetch_ltp(state[f"symbol_{survivor_leg}"], OPTION_EXCH)
        if survivor_ltp > 0 and survivor_ltp < survivor_entry:
            plog(f"Survivor {survivor_leg.upper()} is WINNING — skipping breakeven SL")
            return

        raw_be = survivor_entry + (state["closed_pnl"] / qty())
        be_price = raw_be * (1 + BREAKEVEN_BUFFER_PCT / 100)

        if be_price <= 0 or be_price >= survivor_entry:
            plog(f"Breakeven price ₹{be_price:.2f} out of range — not activating")
            return

        state[f"breakeven_active_{survivor_leg}"] = True
        state[f"breakeven_sl_{survivor_leg}"] = round(be_price, 2)
        state[f"breakeven_activated_at_{survivor_leg}"] = now_ist().isoformat()
        plog(f"Breakeven SL armed for {survivor_leg.upper()}: ₹{be_price:.2f} (grace {BREAKEVEN_GRACE_MIN}min)")

    def close_all(self, reason: str):
        with _monitor_lock:
            self._close_all_locked(reason)

    def _close_all_locked(self, reason: str):
        state["exit_reason"] = reason
        legs = active_legs()

        for leg in legs:
            ltp = fetch_ltp(state[f"symbol_{leg.lower()}"], OPTION_EXCH)
            self.close_one_leg(leg, reason, current_ltp=ltp)

    def _mark_fully_flat(self, reason: str):
        ws_feed.unsubscribe_position_symbols()

        final_pnl = state["closed_pnl"]
        state["cumulative_daily_pnl"] += final_pnl
        state["last_close_time"] = now_ist().isoformat()
        state["last_trade_pnl"] = final_pnl
        state["trade_count"] += 1

        self._append_trade_log(reason)

        exit_msg = (
            f"{'🟢' if final_pnl >= 0 else '🔴'} EXIT — {reason}\n"
            f"P&L: ₹{final_pnl:,.2f}\n"
            f"Day cumulative: ₹{state['cumulative_daily_pnl']:,.2f}\n"
            f"Trade #{state['trade_count']}"
        )
        telegram.notify(exit_msg)
        plog(exit_msg.replace("\n", " | "))
        plog_sep()

        tc = state["trade_count"]
        reentry_ct = state["reentry_count_today"]
        cum_pnl = state["cumulative_daily_pnl"]
        last_close = state["last_close_time"]
        last_pnl = state["last_trade_pnl"]

        reset_state()
        state["trade_count"] = tc
        state["reentry_count_today"] = reentry_ct
        state["cumulative_daily_pnl"] = cum_pnl
        state["last_close_time"] = last_close
        state["last_trade_pnl"] = last_pnl

        clear_state_file()

    def _append_trade_log(self, reason: str):
        if not TRADE_LOG_FILE:
            return
        try:
            now = now_ist()
            entry_t = parse_ist_dt(state.get("entry_time"))
            duration = (now - entry_t).total_seconds() / 60 if entry_t else 0

            record = {
                "date": state.get("entry_date", now.strftime("%Y-%m-%d")),
                "entry_time": state.get("entry_time", ""),
                "exit_time": now.isoformat(),
                "duration_min": round(duration, 1),
                "symbol_ce": state["symbol_ce"], "symbol_pe": state["symbol_pe"],
                "entry_price_ce": state["entry_price_ce"],
                "entry_price_pe": state["entry_price_pe"],
                "exit_price_ce": state["exit_price_ce"],
                "exit_price_pe": state["exit_price_pe"],
                "combined_premium": state["entry_price_ce"] + state["entry_price_pe"],
                "closed_pnl": round(state["closed_pnl"], 2),
                "exit_reason": reason,
                "vix_at_entry": state.get("vix_at_entry", 0),
                "underlying_ltp": state.get("underlying_ltp", 0),
                "dte": state.get("current_dte"),
                "is_reentry": state.get("is_reentry", False),
                "sl_events": state.get("sl_events", []),
                "trade_count": state.get("trade_count", 0),
                "lots": NUMBER_OF_LOTS,
            }
            with open(TRADE_LOG_FILE, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            plog(f"Trade log write failed: {exc}", "WARNING")

# ─────────────────────────────────────────────────────────────────────────────
#  MONITOR — exit signal hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self, engine: OrderEngine):
        self.engine = engine
        self._last_vix_check: datetime | None = None
        self._consecutive_skips = 0

    def run_tick(self):
        if not state["in_position"]:
            return
        if not _monitor_lock.acquire(blocking=False):
            self._consecutive_skips += 1
            if self._consecutive_skips >= 3:
                plog(f"Monitor skipped {self._consecutive_skips} ticks (lock contention)", "WARNING")
            return
        try:
            self._consecutive_skips = 0
            self._tick_inner()
        finally:
            _monitor_lock.release()

    def _tick_inner(self):
        legs = active_legs()
        if not legs:
            return

        # 1. Per-leg SL check
        for leg in list(legs):
            leg_l = leg.lower()
            sym = state[f"symbol_{leg_l}"]
            ltp = fetch_ltp(sym, OPTION_EXCH)
            if ltp <= 0:
                self.engine._consecutive_quote_fails += 1
                if (self.engine._consecutive_quote_fails >= QUOTE_FAIL_THRESHOLD
                        and not self.engine._quote_fail_alerted):
                    telegram.notify(f"⚠️ Quote failure: {sym} — {self.engine._consecutive_quote_fails} ticks")
                    self.engine._quote_fail_alerted = True
                continue

            self.engine._consecutive_quote_fails = 0
            self.engine._quote_fail_alerted = False

            sl = sl_level(leg)
            if sl <= 0:
                continue

            if ltp >= sl:
                entry_px = state[f"entry_price_{leg_l}"]

                if state["closed_pnl"] != 0 and len(active_legs()) == 1:
                    open_mtm = (entry_px - ltp) * qty() if entry_px > 0 else 0
                    net = state["closed_pnl"] + open_mtm
                    if net > 0:
                        defer_key = f"net_pnl_defer_start_{leg_l}"
                        if state.get(defer_key) is None:
                            state[defer_key] = now_ist().isoformat()
                            plog(f"Net P&L guard: deferring {leg} SL (net ₹{net:,.2f} positive)")
                            save_state()
                            continue
                        defer_start = parse_ist_dt(state[defer_key])
                        if defer_start:
                            elapsed = (now_ist() - defer_start).total_seconds() / 60
                            if elapsed < NET_PNL_GUARD_MAX_DEFER_MIN:
                                continue

                sl_type = "Breakeven SL" if state.get(f"breakeven_active_{leg_l}") else "Fixed SL"
                reason = f"{sl_type} hit ({leg})"
                plog(f"SL HIT: {leg} LTP={ltp:.2f} >= SL={sl:.2f} | {sl_type}")
                self.engine.close_one_leg(leg, reason, current_ltp=ltp)
                if not state["in_position"]:
                    return

        legs = active_legs()
        if not legs:
            return

        # 2. Combined checks (both legs active)
        if len(legs) == 2:
            ce_ltp = fetch_ltp(state["symbol_ce"], OPTION_EXCH)
            pe_ltp = fetch_ltp(state["symbol_pe"], OPTION_EXCH)
            ce_entry = state["entry_price_ce"]
            pe_entry = state["entry_price_pe"]

            if ce_ltp > 0 and pe_ltp > 0 and ce_entry > 0 and pe_entry > 0:
                combined_entry = ce_entry + pe_entry
                combined_current = ce_ltp + pe_ltp
                decay_pct = (1 - combined_current / combined_entry) * 100

                # 2a. Combined decay exit
                if COMBINED_DECAY_ENABLED:
                    dte = state.get("current_dte")
                    target = COMBINED_DECAY_DTE_MAP.get(dte, COMBINED_DECAY_DEFAULT) if dte is not None else COMBINED_DECAY_DEFAULT
                    if decay_pct >= target:
                        plog(f"Combined decay exit: {decay_pct:.1f}% >= {target:.1f}% target")
                        self.engine.close_all(f"Combined Decay Exit ({decay_pct:.1f}%)")
                        return

                # 2b. Asymmetric leg booking
                if ASYMMETRIC_ENABLED:
                    ce_pct = (ce_ltp / ce_entry) * 100
                    pe_pct = (pe_ltp / pe_entry) * 100
                    if ce_pct <= ASYMMETRIC_WINNER_DECAY_PCT and pe_pct >= ASYMMETRIC_LOSER_INTACT_PCT:
                        plog(f"Asymmetric booking: CE at {ce_pct:.1f}%, PE at {pe_pct:.1f}%")
                        self.engine.close_one_leg("CE", f"Asymmetric Booking (CE={ce_pct:.1f}%)")
                        if not state["in_position"]:
                            return
                    elif pe_pct <= ASYMMETRIC_WINNER_DECAY_PCT and ce_pct >= ASYMMETRIC_LOSER_INTACT_PCT:
                        plog(f"Asymmetric booking: PE at {pe_pct:.1f}%, CE at {ce_pct:.1f}%")
                        self.engine.close_one_leg("PE", f"Asymmetric Booking (PE={pe_pct:.1f}%)")
                        if not state["in_position"]:
                            return

                # 2c. Combined profit trailing
                if COMBINED_TRAIL_ENABLED:
                    if decay_pct >= COMBINED_TRAIL_ACTIVATE_PCT:
                        if not state["combined_trail_active"]:
                            state["combined_trail_active"] = True
                            state["combined_decay_peak"] = decay_pct
                            plog(f"Combined trail activated at {decay_pct:.1f}%")
                        else:
                            if decay_pct > state["combined_decay_peak"]:
                                state["combined_decay_peak"] = decay_pct
                            retracement = state["combined_decay_peak"] - decay_pct
                            if retracement >= COMBINED_TRAIL_PCT:
                                plog(f"Combined trail exit: peak={state['combined_decay_peak']:.1f}%, "
                                     f"current={decay_pct:.1f}%, retrace={retracement:.1f}%")
                                self.engine.close_all(f"Combined Trail Exit (retrace {retracement:.1f}%)")
                                return

        # 3. Winner-leg early booking (single survivor)
        if len(legs) == 1 and WINNER_BOOKING_ENABLED:
            leg = legs[0]
            leg_l = leg.lower()
            entry_px = state[f"entry_price_{leg_l}"]
            if entry_px > 0:
                ltp = fetch_ltp(state[f"symbol_{leg_l}"], OPTION_EXCH)
                if ltp > 0:
                    decay = (ltp / entry_px) * 100
                    if decay <= WINNER_BOOKING_DECAY_PCT:
                        plog(f"Winner booking: {leg} at {decay:.1f}% of entry")
                        self.engine.close_one_leg(leg, f"Winner Booking ({decay:.1f}%)", current_ltp=ltp)
                        return

        # 4. Combined P&L update + recovery lock check
        closed_pnl = state["closed_pnl"]
        open_mtm = 0.0
        for leg in legs:
            leg_l = leg.lower()
            entry_px = state[f"entry_price_{leg_l}"]
            ltp = fetch_ltp(state[f"symbol_{leg_l}"], OPTION_EXCH)
            if entry_px > 0 and ltp > 0:
                open_mtm += (entry_px - ltp) * qty()
        combined_pnl = closed_pnl + open_mtm
        state["today_pnl"] = round(combined_pnl, 2)

        # 5. VIX spike check (throttled)
        if VIX_SPIKE_ENABLED and state.get("vix_at_entry", 0) > 0:
            now = now_ist()
            if (self._last_vix_check is None or
                    (now - self._last_vix_check).total_seconds() >= VIX_SPIKE_INTERVAL_S):
                self._last_vix_check = now
                current_vix = fetch_vix()
                if current_vix > 0:
                    entry_vix = state["vix_at_entry"]
                    spike_pct = ((current_vix - entry_vix) / entry_vix) * 100
                    if spike_pct >= VIX_SPIKE_THRESHOLD and current_vix >= VIX_SPIKE_ABS_FLOOR:
                        plog(f"VIX SPIKE: {entry_vix:.2f} → {current_vix:.2f} ({spike_pct:.1f}%)")
                        self.engine.close_all(f"VIX Spike Exit ({spike_pct:.1f}%)")
                        return

        # 6. Daily profit target / loss limit
        effective_target = DAILY_TARGET * NUMBER_OF_LOTS
        effective_limit = DAILY_LOSS_LIMIT * NUMBER_OF_LOTS
        if effective_target > 0 and combined_pnl >= effective_target:
            plog(f"Daily target hit: ₹{combined_pnl:,.2f} >= ₹{effective_target:,.2f}")
            self.engine.close_all(f"Daily Target (₹{combined_pnl:,.2f})")
            return
        if effective_limit < 0 and combined_pnl <= effective_limit:
            plog(f"Daily loss limit hit: ₹{combined_pnl:,.2f} <= ₹{effective_limit:,.2f}")
            self.engine.close_all(f"Daily Loss Limit (₹{combined_pnl:,.2f})")
            return

        self.engine._first_tick_fired = True

# ─────────────────────────────────────────────────────────────────────────────
#  RECONCILER — startup crash recovery
# ─────────────────────────────────────────────────────────────────────────────

class Reconciler:
    def __init__(self, engine: OrderEngine):
        self.engine = engine

    def reconcile(self) -> str:
        """Returns: 'clean', 'restored', or 'externally_closed'"""
        load_state()
        if not state["in_position"]:
            if os.path.exists(STATE_FILE):
                plog("Stale state file found — removing")
                clear_state_file()
            plog("Reconciler: no position — clean start")
            return "clean"

        plog("Reconciler: saved state has position — checking broker")
        today_str = now_ist().strftime("%Y-%m-%d")
        if state.get("entry_date") and state["entry_date"] != today_str:
            plog("Reconciler: position from previous day — resetting")
            reset_state()
            clear_state_file()
            return "clean"

        positions = self._fetch_broker_positions()

        ce_sym = state["symbol_ce"]
        pe_sym = state["symbol_pe"]
        ce_live = ce_sym in positions
        pe_live = pe_sym in positions

        if ce_live or pe_live:
            state["ce_active"] = ce_live
            state["pe_active"] = pe_live
            ws_feed.subscribe_position_symbols()
            plog(f"Reconciler: RESTORED — CE={ce_live}, PE={pe_live}")
            if state["entry_price_ce"] <= 0 or state["entry_price_pe"] <= 0:
                self._refetch_fills()
            save_state()
            return "restored"
        else:
            plog("Reconciler: broker is flat — externally closed")
            reset_state()
            clear_state_file()
            return "externally_closed"

    def _fetch_broker_positions(self) -> set[str]:
        try:
            resp = broker.get().positionbook()
            if api_ok(resp):
                positions = set()
                for pos in resp.get("data", []):
                    if (pos.get("exchange") == OPTION_EXCH and
                            abs(int(pos.get("quantity", 0) or 0)) > 0):
                        positions.add(pos.get("symbol", ""))
                return positions
        except Exception as exc:
            plog(f"Reconciler: positionbook error: {exc}", "ERROR")
        return set()

    def _refetch_fills(self):
        pos_map = {}
        try:
            resp = broker.get().positionbook()
            if api_ok(resp):
                for pos in resp.get("data", []):
                    pos_map[pos.get("symbol", "")] = pos
        except Exception as exc:
            plog(f"Reconciler: positionbook error during refetch: {exc}", "WARNING")

        for leg, sym_key, px_key in [("CE", "symbol_ce", "entry_price_ce"),
                                       ("PE", "symbol_pe", "entry_price_pe")]:
            if state[px_key] <= 0 and state[f"{leg.lower()}_active"]:
                sym = state[sym_key]
                avg_price = 0.0
                if sym in pos_map:
                    avg_price = float(pos_map[sym].get("average_price", 0) or 0)
                if avg_price > 0:
                    state[px_key] = avg_price
                    plog(f"Reconciler: recovered {leg} fill ₹{avg_price:.2f} (positionbook)")
                else:
                    ltp = fetch_ltp(sym, OPTION_EXCH)
                    if ltp > 0:
                        state[px_key] = ltp
                        plog(f"Reconciler: recovered {leg} fill ₹{ltp:.2f} (LTP fallback)", "WARNING")

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY FILTER CHAIN
# ─────────────────────────────────────────────────────────────────────────────

def dte_filter_ok(dte: int) -> bool:
    if dte not in TRADE_DTE:
        plog(f"DTE filter: DTE={dte} not in {TRADE_DTE} — skip")
        return False
    month = now_ist().month
    if month in SKIP_MONTHS:
        plog(f"Month filter: {month} in skip list — skip")
        return False
    return True

def reentry_ok() -> bool:
    if not REENTRY_ENABLED:
        return False
    if state["reentry_count_today"] >= REENTRY_MAX_PER_DAY:
        plog(f"Re-entry blocked: {state['reentry_count_today']}/{REENTRY_MAX_PER_DAY} used")
        return False
    last_close = parse_ist_dt(state.get("last_close_time"))
    if last_close:
        elapsed = (now_ist() - last_close).total_seconds() / 60
        if elapsed < REENTRY_COOLDOWN_MIN:
            plog(f"Re-entry cooldown: {elapsed:.0f}/{REENTRY_COOLDOWN_MIN} min")
            return False
    last_pnl = state.get("last_trade_pnl", 0)
    max_loss = REENTRY_MAX_LOSS * NUMBER_OF_LOTS
    if last_pnl < -max_loss:
        plog(f"Re-entry blocked: last loss ₹{last_pnl:,.2f} > cap ₹{-max_loss:,.2f}")
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
#  STRATEGY CORE — scheduling & main loop
# ─────────────────────────────────────────────────────────────────────────────

class NiftyShortStraddle:
    def __init__(self):
        self.engine = OrderEngine()
        self.monitor = Monitor(self.engine)
        self.reconciler = Reconciler(self.engine)
        self._stop_event = threading.Event()
        self._entry_done_today = False
        self._daily_reset_done = False
        self._shutdown_done = False

    def run(self):
        plog_sep()
        plog(f"Nifty Short Straddle v8.0 — OpenAlgo Strategy")
        plog(f"Entry: {ENTRY_TIME} | Exit: {EXIT_TIME} | SL: {LEG_SL_PERCENT}%")
        plog(f"Lots: {NUMBER_OF_LOTS} × {LOT_SIZE} = {qty()}")
        plog_sep()

        telegram.start()

        try:
            az_resp = broker.get().analyzerstatus()
            if api_ok(az_resp):
                az_data = az_resp.get("data", {})
                if az_data.get("analyze_mode", False):
                    mode_str = "ANALYZER MODE (sandbox — orders will NOT reach broker)"
                    plog(f"⚠️  {mode_str}", "WARNING")
                    telegram.notify(f"⚠️ {mode_str}")
                else:
                    plog("Broker mode: LIVE")
        except Exception as exc:
            plog(f"Analyzer status check failed: {exc}", "WARNING")

        telegram.notify("Strategy Started — Nifty Short Straddle v8.0\n"
                       f"Entry: {ENTRY_TIME} | Exit: {EXIT_TIME} | SL: {LEG_SL_PERCENT}%\n"
                       f"Lots: {NUMBER_OF_LOTS} × {LOT_SIZE} = {qty()}")
        ws_feed.subscribe_market_symbols()
        ws_feed.start()
        reconcile_result = self.reconciler.reconcile()

        if state["in_position"]:
            self._entry_done_today = True
            plog("Resumed with open position — monitoring")
        elif reconcile_result == "externally_closed":
            self._entry_done_today = True
            plog("Positions were closed externally — no new entry today")
            telegram.notify("Positions were closed externally while script was stopped.\nNo automatic re-entry. Restart before entry time for fresh entry.")
        else:
            plog(f"Waiting for entry at {ENTRY_TIME}")

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self._main_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _main_loop(self):
        entry_h, entry_m = parse_hhmm(ENTRY_TIME)
        exit_h, exit_m = parse_hhmm(EXIT_TIME)

        while not self._stop_event.is_set():
            now = now_ist()
            hhmm = now.strftime("%H:%M")
            weekday = now.weekday()

            if weekday >= 5:
                self._stop_event.wait(60)
                continue

            if hhmm < "09:15" or hhmm >= "15:30":
                if hhmm >= "15:30" and not self._daily_reset_done:
                    self._daily_reset()
                self._stop_event.wait(10)
                continue

            if not self._daily_reset_done and hhmm >= "09:15":
                self._daily_reset_done = True
                self._entry_done_today = False
                state["reentry_count_today"] = 0
                state["cumulative_daily_pnl"] = 0.0
                state["trade_count"] = 0

            if hhmm >= f"{exit_h:02d}:{exit_m:02d}" and state["in_position"]:
                plog("Hard exit time reached")
                self.engine.close_all("Hard Exit (15:15)")
                self._entry_done_today = True

            elif not state["in_position"] and hhmm >= f"{entry_h:02d}:{entry_m:02d}" and hhmm < f"{exit_h:02d}:{exit_m:02d}":
                if not self._entry_done_today or (state.get("last_close_time") and reentry_ok()):
                    self._try_entry()

            if state["in_position"]:
                self.monitor.run_tick()

            self._stop_event.wait(MONITOR_INTERVAL)

    def _try_entry(self):
        expiry = resolve_expiry()
        if not expiry:
            plog("Could not resolve expiry — skip", "ERROR")
            return

        dte = compute_dte(expiry)
        state["current_dte"] = dte

        if not dte_filter_ok(dte):
            self._entry_done_today = True
            return

        is_reentry = state.get("last_close_time") is not None and state.get("trade_count", 0) > 0
        state["is_reentry"] = is_reentry
        if is_reentry:
            if not reentry_ok():
                return
            state["reentry_count_today"] += 1
            plog(f"Re-entry #{state['reentry_count_today']} — cumulative P&L ₹{state['cumulative_daily_pnl']:,.2f}")

        plog_sep()
        plog(f"Entry attempt: expiry={expiry}, DTE={dte}, reentry={is_reentry}")

        if self.engine.place_entry(expiry):
            if not is_reentry:
                self._entry_done_today = True
        else:
            if not is_reentry:
                self._entry_done_today = True

    def _daily_reset(self):
        self._daily_reset_done = True
        if state["cumulative_daily_pnl"] != 0:
            plog(f"Day end — cumulative P&L: ₹{state['cumulative_daily_pnl']:,.2f}")
        state["reentry_count_today"] = 0
        state["cumulative_daily_pnl"] = 0.0
        state["trade_count"] = 0
        state["last_close_time"] = None
        state["last_trade_pnl"] = 0.0

    def _handle_signal(self, signum, _frame):
        plog(f"Signal {signum} received — shutting down")
        self._stop_event.set()

    def _shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        plog("Shutdown initiated...")
        if state["in_position"]:
            plog("Positions still open — NOT closing (manual action required)")
            save_state()
            try:
                telegram.send_sync(
                    "⚠️ Strategy STOPPED with OPEN positions\n"
                    f"CE: {state['symbol_ce']} (active={state['ce_active']})\n"
                    f"PE: {state['symbol_pe']} (active={state['pe_active']})\n"
                    f"P&L so far: ₹{state['closed_pnl']:,.2f}\n"
                    "⚠️ Take manual action or restart script to resume monitoring"
                )
            except Exception as exc:
                plog(f"Telegram stop message failed: {exc}", "WARNING")
        else:
            telegram.flush()
            try:
                telegram.send_sync("Strategy Stopped — Nifty Short Straddle (no open positions)")
            except Exception as exc:
                plog(f"Telegram stop message failed: {exc}", "WARNING")
        ws_feed.stop()
        telegram.stop()
        plog("Shutdown complete")

    # ── Manual utilities ──

    def check_connection(self):
        plog("Checking OpenAlgo connection...")
        try:
            resp = broker.get().funds()
            if api_ok(resp):
                data = resp.get("data", {})
                plog(f"Connected: cash={data.get('availablecash')}, collateral={data.get('collateral')}")
            else:
                plog(f"Connection failed: {api_err(resp)}", "ERROR")
        except Exception as exc:
            plog(f"Connection error: {exc}", "ERROR")

    def manual_entry(self):
        expiry = resolve_expiry()
        if expiry:
            dte = compute_dte(expiry)
            state["current_dte"] = dte
            self.engine.place_entry(expiry)

    def manual_exit(self):
        if state["in_position"]:
            self.engine.close_all("Manual Exit")
        else:
            plog("No position to exit")

    def show_state(self):
        plog(f"Position: {state['in_position']}")
        for leg in ["ce", "pe"]:
            if state[f"{leg}_active"]:
                ltp = fetch_ltp(state[f"symbol_{leg}"], OPTION_EXCH)
                sl = sl_level(leg.upper())
                plog(f"  {leg.upper()}: {state[f'symbol_{leg}']} entry={state[f'entry_price_{leg}']:.2f} "
                     f"ltp={ltp:.2f} sl={sl:.2f}")
        plog(f"  Closed P&L: ₹{state['closed_pnl']:,.2f}")
        plog(f"  Today P&L: ₹{state['today_pnl']:,.2f}")


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    strategy = NiftyShortStraddle()
    strategy.run()

    # Testing utilities (uncomment one):
    # strategy.check_connection()
    # strategy.manual_entry()
    # strategy.manual_exit()
    # strategy.show_state()
