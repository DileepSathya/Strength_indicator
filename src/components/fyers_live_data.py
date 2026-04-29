import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime

import yaml
from fyers_apiv3.FyersWebsocket import data_ws
from src.components import stream_server

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

SYMBOLS        = ["NSE:ASIANPAINT-EQ", "NSE:HEROMOTOCO-EQ"]
CANDLE_MINUTES = 5                                   # ← change to 1, 3, 5, 15 freely
ARTIFACTS_PATH = Path("artifacts/candle_data.json")
SUMMARY_FILE   = Path("artifacts/live_summary.txt")

BULL = "[BUY]"
BEAR = "[SEL]"
NEUT = "[NEU]"
OK   = "[CLOSED]"
ERR  = "[ERROR]"
CONN = "[DISCONNECTED]"


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
        """Re-anchor live lines after reconnect."""
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
# SUMMARY DISPLAY — compact table written to file + optional TTY
# ══════════════════════════════════════════════════════════════════════════════

class SummaryDisplay:
    """
    Always writes to artifacts/live_summary.txt (plain text, no escape codes).
    Watch with:
      Linux/Mac : tail -f artifacts/live_summary.txt
      Windows   : Get-Content artifacts/live_summary.txt -Wait

    Optional: pass tty_path='/dev/pts/1' (run `tty` in second terminal on Unix)
    to also write directly to that terminal with screen-clear.
    """

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
        # Bug 7 fix — depth not yet received
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
        # Bug 2 fix — separate file output (no escape codes) from TTY output
        rows = [self.HEADER, self.SEP]
        for s in self.symbols:
            rows.append(self.rows.get(s, ""))
        rows.append(self.SEP)

        file_output = "\n".join(rows) + "\n"
        tty_output  = "\033[H\033[J" + file_output  # screen clear only for TTY

        # Plain text to file
        try:
            with open(self.summary_file, "w", encoding="utf-8") as f:
                f.write(file_output)
        except Exception:
            pass

        # With screen clear to TTY (Unix only)
        if self.tty_path:
            try:
                with open(self.tty_path, "w", encoding="utf-8") as f:
                    f.write(tty_output)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE STORE — persists closed candles as a proper JSON array
# ══════════════════════════════════════════════════════════════════════════════

class CandleStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: list[dict] = []
        # Bug 1 fix — load as proper JSON array
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                try:
                    self._data = json.load(f)
                except json.JSONDecodeError:
                    self._data = []

    def append(self, candle: dict):
        self._data.append(candle)
        # Rewrite full array — keeps file as valid JSON
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def all(self) -> list[dict]:
        return self._data


# ══════════════════════════════════════════════════════════════════════════════
# CANDLE BUILDER — configurable N-minute candles
# ══════════════════════════════════════════════════════════════════════════════

class CandleBuilder:
    def __init__(self, candle_minutes: int = 5):
        self.candle_minutes     = candle_minutes
        self.candle_seconds     = candle_minutes * 60
        self.ticks              = []
        self.closed_candles     = []
        self.current_bucket     = None
        self.prev_ltp           = None
        self.last_direction     = None
        self.last_vol_traded    = None   # Bug 3 fix — track across candle boundaries

    def _candle_bucket(self, ts: int) -> int:
        """Floor timestamp to N-minute boundary. e.g. 10:07:34 → 10:05:00 for 5min."""
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

        # Bug 3 fix — vol_delta uses persistent last_vol_traded across candles
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
        """Returns elapsed time in current candle e.g. '3m12s / 5m00s'"""
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
    """
    summary_tty : optional Unix TTY path e.g. '/dev/pts/1'
                  Run `tty` in a second terminal to get the path.
                  If None, summary is written to artifacts/live_summary.txt only.

    Monitor summary:
      Linux/Mac : tail -f artifacts/live_summary.txt
      Windows   : Get-Content artifacts/live_summary.txt -Wait
    """
    token        = _load_access_token()
    depth_store  = DepthStore()
    display      = LiveDisplay(SYMBOLS)
    summary      = SummaryDisplay(SYMBOLS, SUMMARY_FILE, tty_path=summary_tty)
    candle_store = CandleStore(ARTIFACTS_PATH)

    # One CandleBuilder per symbol with configured timeframe
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

        # ── DepthUpdate → silent store ─────────────────────────────────────
        if msg_type == "dp":
            depth_store.update(message)
            return

        # ── SymbolUpdate → candle building ─────────────────────────────────
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

        # Bug 5 fix — warn if unexpected symbol creates new builder
        builder = builders.get(symbol)
        if builder is None:
            display.print_closed(f"\n[WARN] New builder created for {symbol} mid-session")
            builders[symbol] = CandleBuilder(candle_minutes=CANDLE_MINUTES)
            builder = builders[symbol]

        closed = builder.push(tick)

        # ── Closed candle → save + print ───────────────────────────────────
        if closed:
            candle_store.append(closed)
            display.print_closed(
                f"\n{OK} [{symbol}]  "
                f"[{closed['candle_open']} - {closed['candle_close']}]  "
                f"({closed['timeframe']})\n"
                f"{json.dumps(closed, indent=2)}\n"
                f"{'-' * 72}"
            )

            # Broadcast closed candle to the dashboard (SSE)
            stream_server.broadcast({**closed, "type": "closed"})

        # ── Live candle → update both displays ─────────────────────────────
        live = builder.current_candle_live()
        if live:
            agg      = live["aggression"]
            ohlcv    = live["ohlcv"]
            limits   = live["limit_orders"]
            score    = agg["score"]
            arrow    = BULL if score > 0.3 else BEAR if score < -0.3 else NEUT
            progress = builder.candle_progress()

            # Main terminal — full OHLCV + candle progress
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

            # Summary terminal — compact metrics only
            summary.update(
                symbol       = symbol,
                ts           = tick["last_traded_time"],
                agg          = score,
                qty_ratio    = limits["qty_ratio"],
                ord_sz_ratio = limits["order_size_ratio"],
            )

            # Broadcast live candle to the dashboard (SSE)
            stream_server.broadcast({
                **live,
                "type": "live",
                "progress": progress,
            })

    # Bug 4 fix — safe error/close message handling
    def on_error(message):
        msg = json.dumps(message, indent=2) if isinstance(message, dict) else str(message)
        display.print_closed(f"\n{ERR}\n{msg}")

    def on_close(message):
        msg = json.dumps(message, indent=2) if isinstance(message, dict) else str(message)
        display.print_closed(f"\n{CONN}\n{msg}")

    def on_open():
        # Bug 6 fix — reset display anchor on reconnect
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
    # Option A — second terminal (Unix):
    #   1. Open second terminal → run: tty → e.g. /dev/pts/1
    #   2. run_live_market_stream(summary_tty="/dev/pts/1")

    # Option B — file watch:
    #   Linux/Mac : tail -f artifacts/live_summary.txt
    #   Windows   : Get-Content artifacts/live_summary.txt -Wait

    run_live_market_stream(summary_tty=None)