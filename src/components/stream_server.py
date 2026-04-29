import os
import sys
import time
import json
import queue
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

import yaml
from fyers_apiv3.FyersWebsocket import data_ws

# ── Force UTF-8 stdout/stderr on Windows (without re-wrapping streams) ───────
# Re-wrapping sys.stderr can trigger "lost sys.stderr" during interpreter shutdown.
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # If reconfigure isn't supported, leave streams as-is.
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

SYMBOLS        = ["NSE:ASIANPAINT-EQ", "NSE:HEROMOTOCO-EQ"]   # ← edit freely
CANDLE_MINUTES = 5                                              # ← 1/3/5/15
ARTIFACTS_PATH = Path("artifacts/candle_data.json")
SUMMARY_FILE   = Path("artifacts/live_summary.txt")
DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"
DASHBOARD_PORT = 5050

# Backward-compatible fallback if someone starts from a different CWD
if not DASHBOARD_HTML.exists():
    DASHBOARD_HTML = Path("dashboard.html")

BULL = "[BUY]"
BEAR = "[SEL]"
NEUT = "[NEU]"
OK   = "[CLOSED]"
ERR  = "[ERROR]"
CONN = "[DISCONNECTED]"


# ══════════════════════════════════════════════════════════════════════════════
# SSE BROADCAST HUB
# Keeps a set of per-client queues; push() fans out to all connected browsers.
# ══════════════════════════════════════════════════════════════════════════════

class SSEHub:
    def __init__(self):
        # Use queue.Queue so we can both:
        #  - put_nowait() from producer thread
        #  - get(timeout=...) from the SSE HTTP handler thread
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def add(self) -> queue.Queue:
        # Small buffer so slow browsers don't stall the producer.
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.add(q)
        return q

    def remove(self, q: queue.Queue):
        with self._lock:
            self._clients.discard(q)

    def push(self, payload: dict):
        data = "data: " + json.dumps(payload) + "\n\n"
        dead: list[queue.Queue] = []
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                # Browser fell behind; drop this message but keep the connection.
                continue
            except Exception:
                dead.append(q)
        for q in dead:
            self.remove(q)


# Global hub shared between HTTP server and WebSocket thread
_hub = SSEHub()


def broadcast(payload: dict):
    """Send a JSON-serializable payload to all connected dashboard browsers."""
    _hub.push(payload)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP + SSE SERVER
# GET /        → serves dashboard.html
# GET /events  → SSE stream
# ══════════════════════════════════════════════════════════════════════════════

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress access log noise

    def do_GET(self):
        if self.path == "/events":
            self._handle_sse()
        elif self.path in ("/", "/index.html"):
            self._serve_html()
        else:
            self.send_error(404)

    def _serve_html(self):
        html_path = DASHBOARD_HTML
        if not html_path.exists():
            self.send_error(404, "dashboard.html not found")
            return
        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = _hub.add()
        try:
            while True:
                try:
                    msg = q.get(timeout=20)    # 20 s keepalive
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Send SSE comment as heartbeat so connection stays alive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _hub.remove(q)


def start_dashboard_server():
    """Launch the HTTP/SSE server in a background daemon thread."""
    server = HTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[DASHBOARD] http://localhost:{DASHBOARD_PORT}  (opens automatically)")
    # Try to open browser automatically
    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")
    except Exception:
        pass

    return server


def start(host: str = "localhost", port: int = DASHBOARD_PORT):
    """Compatibility wrapper expected by `main.py`."""
    # `start_dashboard_server()` currently binds to 0.0.0.0 and uses `DASHBOARD_PORT`.
    return start_dashboard_server()


# ══════════════════════════════════════════════════════════════════════════════
# LIVE DISPLAY — one fixed line per symbol (main terminal)
# ══════════════════════════════════════════════════════════════════════════════

class LiveDisplay:
    def __init__(self, symbols: list[str]):
        self.symbols      = symbols
        self.index        = {s: i for i, s in enumerate(symbols)}
        self.num_lines    = len(symbols)
        self._initialized = False

    def _init(self):
        for _ in self.symbols:
            print()
        self._initialized = True

    def reset(self):
        self._initialized = False

    def update_live(self, symbol: str, text: str):
        if not self._initialized:
            self._init()
        row      = self.index.get(symbol, 0)
        lines_up = self.num_lines - row
        print(f"\033[{lines_up}A\r\033[K{text}\033[{lines_up}B", end="", flush=True)

    def print_closed(self, text: str):
        if not self._initialized:
            self._init()
        print(f"\033[{self.num_lines}B", end="")
        print(text)
        print(f"\033[{self.num_lines}A", end="", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

class SummaryDisplay:
    HEADER = (
        f"{'Time':<10}"
        f"{'Symbol':<24}"
        f"{'Aggression':>12}"
        f"{'QtyRatio':>12}"
        f"{'OrdSzRatio':>12}"
        f"  {'Signal'}"
    )
    SEP = "-" * 78

    def __init__(self, symbols: list[str], summary_file: Path, tty_path: str | None = None):
        self.symbols      = symbols
        self.summary_file = summary_file
        self.tty_path     = tty_path
        self.rows: dict[str, str] = {s: "" for s in symbols}
        self.summary_file.parent.mkdir(parents=True, exist_ok=True)

    def _signal(self, agg: float, qty: float, ord_sz: float) -> str:
        if qty == 0.0 or ord_sz == 0.0:
            return "[LOADING  ]"
        bullish = sum([agg > 0.3,  qty > 1.2, ord_sz > 1.2])
        bearish = sum([agg < -0.3, qty < 0.8, ord_sz < 0.8])
        if bullish >= 2:
            return "[BUY  BIAS]"
        if bearish >= 2:
            return "[SELL BIAS]"
        return "[NEUTRAL  ]"

    def update(self, symbol: str, ts: int, agg: float, qty_ratio: float, ord_sz_ratio: float):
        t      = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        signal = self._signal(agg, qty_ratio, ord_sz_ratio)
        self.rows[symbol] = (
            f"{t:<10}"
            f"{symbol:<24}"
            f"{agg:>+12.4f}"
            f"{qty_ratio:>12.4f}"
            f"{ord_sz_ratio:>12.4f}"
            f"  {signal}"
        )
        self._render()

    def _render(self):
        rows = [self.HEADER, self.SEP]
        for s in self.symbols:
            rows.append(self.rows.get(s, ""))
        rows.append(self.SEP)
        file_output = "\n".join(rows) + "\n"
        tty_output  = "\033[H\033[J" + file_output
        try:
            with open(self.summary_file, "w", encoding="utf-8") as f:
                f.write(file_output)
        except Exception:
            pass
        if self.tty_path:
            try:
                with open(self.tty_path, "w", encoding="utf-8") as f:
                    f.write(tty_output)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE STORE
# ══════════════════════════════════════════════════════════════════════════════

class CandleStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: list[dict] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                try:
                    self._data = json.load(f)
                except json.JSONDecodeError:
                    self._data = []

    def append(self, candle: dict):
        self._data.append(candle)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def all(self) -> list[dict]:
        return self._data


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class CandleBuilder:
    def __init__(self, candle_minutes: int = 5):
        self.candle_minutes  = candle_minutes
        self.candle_seconds  = candle_minutes * 60
        self.ticks           = []
        self.closed_candles  = []
        self.current_bucket  = None
        self.prev_ltp        = None
        self.last_direction  = None
        self.last_vol_traded = None

    def _candle_bucket(self, ts: int) -> int:
        return (ts // self.candle_seconds) * self.candle_seconds

    def _candle_close_ts(self, bucket_ts: int) -> int:
        return bucket_ts + self.candle_seconds - 1

    def _classify(self, ltp: float) -> str:
        if self.prev_ltp is None:
            return "UNKNOWN"
        if ltp > self.prev_ltp:
            self.last_direction = "UP"
            return "BUY"
        elif ltp < self.prev_ltp:
            self.last_direction = "DOWN"
            return "SELL"
        else:
            if self.last_direction == "UP":
                return "BUY"
            elif self.last_direction == "DOWN":
                return "SELL"
            return "UNKNOWN"

    def _build_candle(self, bucket_ts: int, ticks: list) -> dict:
        open_price  = ticks[0]["ltp"]
        close_price = ticks[-1]["ltp"]
        high_price  = max(t["ltp"] for t in ticks)
        low_price   = min(t["ltp"] for t in ticks)
        volume      = sum(t["vol_delta"] for t in ticks)

        valid      = [t for t in ticks if t["aggressor"] != "UNKNOWN"]
        buy_vol    = sum(t["vol_delta"] for t in valid if t["aggressor"] == "BUY")
        sell_vol   = sum(t["vol_delta"] for t in valid if t["aggressor"] == "SELL")
        total_aggr = buy_vol + sell_vol
        score      = round((buy_vol - sell_vol) / total_aggr, 4) if total_aggr > 0 else 0.0

        last            = ticks[-1]
        tot_buy_qty     = last.get("tot_buy_qty",     0)
        tot_sell_qty    = last.get("tot_sell_qty",    0)
        tot_buy_orders  = last.get("tot_buy_orders",  0)
        tot_sell_orders = last.get("tot_sell_orders", 0)

        qty_ratio   = round(tot_buy_qty    / tot_sell_qty,    4) if tot_sell_qty    > 0 else 0.0
        order_ratio = round(tot_buy_orders / tot_sell_orders, 4) if tot_sell_orders > 0 else 0.0

        avg_bid_order_size = round(tot_buy_qty  / tot_buy_orders,  2) if tot_buy_orders  > 0 else 0.0
        avg_ask_order_size = round(tot_sell_qty / tot_sell_orders, 2) if tot_sell_orders > 0 else 0.0
        order_size_ratio   = round(avg_bid_order_size / avg_ask_order_size, 4) if avg_ask_order_size > 0 else 0.0

        close_ts = self._candle_close_ts(bucket_ts)

        return {
            "symbol":       last["symbol"],
            "candle_open":  datetime.fromtimestamp(bucket_ts).strftime("%H:%M:%S"),
            "candle_close": datetime.fromtimestamp(close_ts).strftime("%H:%M:%S"),
            "timeframe":    f"{self.candle_minutes}min",
            "ohlcv": {
                "open":   open_price,
                "high":   high_price,
                "low":    low_price,
                "close":  close_price,
                "volume": volume,
            },
            "aggression": {
                "score":       score,
                "buy_volume":  buy_vol,
                "sell_volume": sell_vol,
            },
            "limit_orders": {
                "total_bid_qty":      tot_buy_qty,
                "total_ask_qty":      tot_sell_qty,
                "qty_ratio":          qty_ratio,
                "total_bid_orders":   tot_buy_orders,
                "total_ask_orders":   tot_sell_orders,
                "order_ratio":        order_ratio,
                "avg_bid_order_size": avg_bid_order_size,
                "avg_ask_order_size": avg_ask_order_size,
                "order_size_ratio":   order_size_ratio,
            },
        }

    def push(self, raw_tick: dict) -> dict | None:
        ts     = raw_tick["last_traded_time"]
        bucket = self._candle_bucket(ts)

        closed_candle = None
        if self.current_bucket is not None and bucket != self.current_bucket:
            closed_candle = self._build_candle(self.current_bucket, self.ticks)
            self.closed_candles.append(closed_candle)
            self.ticks = []

        self.current_bucket = bucket
        ltp                 = raw_tick["ltp"]
        aggressor           = self._classify(ltp)

        vol_delta = 0
        if self.last_vol_traded is not None:
            vol_delta = raw_tick["vol_traded_today"] - self.last_vol_traded

        self.last_vol_traded = raw_tick["vol_traded_today"]
        self.prev_ltp        = ltp

        self.ticks.append({
            **raw_tick,
            "aggressor": aggressor,
            "vol_delta": max(vol_delta, 0),
        })

        return closed_candle

    def current_candle_live(self) -> dict | None:
        if not self.ticks or self.current_bucket is None:
            return None
        return self._build_candle(self.current_bucket, self.ticks)

    def candle_progress(self) -> str:
        if not self.ticks:
            return ""
        elapsed = self.ticks[-1]["last_traded_time"] - self.current_bucket
        total   = self.candle_seconds
        return f"{elapsed // 60}m{elapsed % 60:02d}s/{total // 60}m00s"


# ══════════════════════════════════════════════════════════════════════════════
# DEPTH STORE
# ══════════════════════════════════════════════════════════════════════════════

class DepthStore:
    def __init__(self):
        self._store: dict[str, dict] = {}

    def update(self, message: dict):
        symbol = message.get("symbol")
        if not symbol:
            return

        bids, asks = [], []
        for i in range(1, 6):
            bid_price = message.get(f"bid_price{i}")
            bid_size  = message.get(f"bid_size{i}",  0)
            bid_order = message.get(f"bid_order{i}", 0)
            ask_price = message.get(f"ask_price{i}")
            ask_size  = message.get(f"ask_size{i}",  0)
            ask_order = message.get(f"ask_order{i}", 0)
            if bid_price:
                bids.append({"price": bid_price, "qty": int(bid_size or 0), "orders": int(bid_order or 0)})
            if ask_price:
                asks.append({"price": ask_price, "qty": int(ask_size or 0), "orders": int(ask_order or 0)})

        total_bid_qty    = sum(b["qty"]    for b in bids)
        total_ask_qty    = sum(a["qty"]    for a in asks)
        total_bid_orders = sum(b["orders"] for b in bids)
        total_ask_orders = sum(a["orders"] for a in asks)

        qty_ratio   = round(total_bid_qty    / total_ask_qty,    4) if total_ask_qty    > 0 else 0.0
        order_ratio = round(total_bid_orders / total_ask_orders, 4) if total_ask_orders > 0 else 0.0

        self._store[symbol] = {
            "symbol":               symbol,
            "bids":                 bids,
            "asks":                 asks,
            "best_bid":             bids[0] if bids else None,
            "best_ask":             asks[0] if asks else None,
            "spread":               round(asks[0]["price"] - bids[0]["price"], 2) if bids and asks else None,
            "total_bid_qty":        total_bid_qty,
            "total_ask_qty":        total_ask_qty,
            "qty_ratio":            qty_ratio,
            "total_bid_orders":     total_bid_orders,
            "total_ask_orders":     total_ask_orders,
            "order_ratio":          order_ratio,
            "qty_order_divergence": round(qty_ratio - order_ratio, 4),
            "updated_at":           int(time.time()),
        }

    def get(self, symbol: str) -> dict | None:
        return self._store.get(symbol)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _load_access_token() -> str:
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}

    input_params = config_data.get("Input_params", {})
    client_id    = input_params.get("client_id")
    access_token = input_params.get("auth_code")

    if client_id and access_token:
        return f"{client_id}:{access_token}"

    env_token = os.getenv("FYERS_ACCESS_TOKEN")
    if env_token:
        return env_token

    raise ValueError(
        "Missing Fyers credentials. Add Input_params.client_id and "
        "Input_params.auth_code to config/config.yaml or set FYERS_ACCESS_TOKEN."
    )


# ══════════════════════════════════════════════════════════════════════════════
# STREAM
# ══════════════════════════════════════════════════════════════════════════════

def run_live_market_stream(summary_tty: str | None = None) -> None:
    token        = _load_access_token()
    depth_store  = DepthStore()
    display      = LiveDisplay(SYMBOLS)
    summary      = SummaryDisplay(SYMBOLS, SUMMARY_FILE, tty_path=summary_tty)
    candle_store = CandleStore(ARTIFACTS_PATH)

    builders: dict[str, CandleBuilder] = {
        s: CandleBuilder(candle_minutes=CANDLE_MINUTES) for s in SYMBOLS
    }

    def on_message(message):
        if not isinstance(message, dict):
            return

        msg_type = message.get("type", "")
        symbol   = message.get("symbol")

        if not symbol or symbol not in SYMBOLS:
            return

        # ── DepthUpdate ────────────────────────────────────────────────────
        if msg_type == "dp":
            depth_store.update(message)
            return

        if msg_type != "sf":
            return

        ltp = message.get("ltp", message.get("last_traded_price"))
        if ltp is None:
            return

        depth           = depth_store.get(symbol)
        tot_buy_qty     = depth["total_bid_qty"]    if depth else 0
        tot_sell_qty    = depth["total_ask_qty"]    if depth else 0
        tot_buy_orders  = depth["total_bid_orders"] if depth else 0
        tot_sell_orders = depth["total_ask_orders"] if depth else 0

        tick = {
            "ltp":              ltp,
            "vol_traded_today": message.get("vol_traded_today", 0),
            "last_traded_qty":  message.get("last_traded_qty",  0),
            "symbol":           symbol,
            "last_traded_time": message.get("last_traded_time", int(time.time())),
            "tot_buy_qty":      tot_buy_qty,
            "tot_sell_qty":     tot_sell_qty,
            "tot_buy_orders":   tot_buy_orders,
            "tot_sell_orders":  tot_sell_orders,
        }

        builder = builders.get(symbol)
        if builder is None:
            display.print_closed(f"\n[WARN] New builder created for {symbol} mid-session")
            builders[symbol] = CandleBuilder(candle_minutes=CANDLE_MINUTES)
            builder = builders[symbol]

        closed = builder.push(tick)

        # ── Closed candle → save + push to dashboard ───────────────────────
        if closed:
            candle_store.append(closed)
            display.print_closed(
                f"\n{OK} [{symbol}]  "
                f"[{closed['candle_open']} - {closed['candle_close']}]  "
                f"({closed['timeframe']})\n"
                f"{json.dumps(closed, indent=2)}\n"
                f"{'-' * 72}"
            )
            # ★ Push closed candle to all dashboard browsers
            _hub.push({**closed, "type": "closed"})

        # ── Live candle → update terminal + push to dashboard ──────────────
        live = builder.current_candle_live()
        if live:
            agg      = live["aggression"]
            ohlcv    = live["ohlcv"]
            limits   = live["limit_orders"]
            score    = agg["score"]
            arrow    = BULL if score > 0.3 else BEAR if score < -0.3 else NEUT
            progress = builder.candle_progress()

            # Terminal
            display.update_live(symbol,
                f"{arrow} [{symbol:<22}]  "
                f"{live['candle_open']}  "
                f"O:{ohlcv['open']:<10}  "
                f"H:{ohlcv['high']:<10}  "
                f"L:{ohlcv['low']:<10}  "
                f"C:{ohlcv['close']:<10}  "
                f"V:{ohlcv['volume']:<8}  "
                f"AGG:{score:+.4f}  "
                f"B:{agg['buy_volume']:<8}  "
                f"S:{agg['sell_volume']:<8}  "
                f"[{progress}]"
            )

            summary.update(
                symbol       = symbol,
                ts           = tick["last_traded_time"],
                agg          = score,
                qty_ratio    = limits["qty_ratio"],
                ord_sz_ratio = limits["order_size_ratio"],
            )

            # ★ Push live candle to all dashboard browsers
            _hub.push({
                **live,
                "type":     "live",
                "progress": progress,
            })

    def on_error(message):
        msg = json.dumps(message, indent=2) if isinstance(message, dict) else str(message)
        display.print_closed(f"\n{ERR}\n{msg}")

    def on_close(message):
        msg = json.dumps(message, indent=2) if isinstance(message, dict) else str(message)
        display.print_closed(f"\n{CONN}\n{msg}")

    def on_open():
        display.reset()
        display.print_closed(
            f"[CONNECTED] {CANDLE_MINUTES}-min candles | "
            f"Subscribing to {SYMBOLS}\n"
        )
        fyers.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
        fyers.subscribe(symbols=SYMBOLS, data_type="DepthUpdate")
        fyers.keep_running()

    fyers = data_ws.FyersDataSocket(
        access_token=token,
        log_path="",
        litemode=False,
        write_to_file=False,
        reconnect=True,
        on_connect=on_open,
        on_close=on_close,
        on_error=on_error,
        on_message=on_message,
    )

    fyers.connect()


if __name__ == "__main__":
    start_dashboard_server()          # ← starts HTTP+SSE on port 5050
    run_live_market_stream(summary_tty=None)