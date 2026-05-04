from collections import defaultdict
from datetime import datetime


def classify_aggression(ticks: list[dict]) -> list[dict]:
    results = []
    last_direction = None

    for i, tick in enumerate(ticks):
        if i == 0:
            results.append({**tick, "aggressor": "UNKNOWN", "vol_delta": 0})
            continue

        prev_ltp = ticks[i - 1]["ltp"]
        curr_ltp = tick["ltp"]
        vol_delta = tick["vol_traded_today"] - ticks[i - 1]["vol_traded_today"]

        if curr_ltp > prev_ltp:
            aggressor = "BUY"
            last_direction = "UP"
        elif curr_ltp < prev_ltp:
            aggressor = "SELL"
            last_direction = "DOWN"
        else:
            aggressor = (
                "BUY"
                if last_direction == "UP"
                else "SELL"
                if last_direction == "DOWN"
                else "UNKNOWN"
            )

        results.append({**tick, "aggressor": aggressor, "vol_delta": vol_delta})

    return results


def build_1min_candles(ticks: list[dict]) -> list[dict]:
    """
    Builds 1-minute OHLCV candles from raw tick data.
    Candle boundary: HH:MM:00 -> HH:MM:59 based on last_traded_time.
    """
    classified = classify_aggression(ticks)

    # -- Group ticks by 1-min bucket ----------------------------------------
    buckets = defaultdict(list)
    for tick in classified:
        ts = tick["last_traded_time"]
        # Floor to minute -> 10:00:45 becomes 10:00:00
        minute_key = (ts // 60) * 60
        buckets[minute_key].append(tick)

    candles = []

    for minute_ts in sorted(buckets.keys()):
        bucket = buckets[minute_ts]

        # -- OHLCV -----------------------------------------------------------
        open_price = bucket[0]["ltp"]
        close_price = bucket[-1]["ltp"]
        high_price = max(t["ltp"] for t in bucket)
        low_price = min(t["ltp"] for t in bucket)
        volume = sum(t["vol_delta"] for t in bucket)

        # -- Aggression (only classified ticks) ------------------------------
        valid = [t for t in bucket if t["aggressor"] != "UNKNOWN"]
        buy_vol = sum(t["vol_delta"] for t in valid if t["aggressor"] == "BUY")
        sell_vol = sum(t["vol_delta"] for t in valid if t["aggressor"] == "SELL")
        total_aggr_vol = buy_vol + sell_vol
        aggression_score = (
            round((buy_vol - sell_vol) / total_aggr_vol, 4)
            if total_aggr_vol > 0
            else 0.0
        )
        if volume <= 0:
            buy_pct, sell_pct = 0.0, 0.0
        else:
            buy_pct = round(buy_vol * 100 / volume, 4)
            sell_pct = round(sell_vol * 100 / volume, 4)

        # -- Limit Order Book (last tick is most recent snapshot) -----------
        last_tick = bucket[-1]
        tot_buy_qty = last_tick["tot_buy_qty"]
        tot_sell_qty = last_tick["tot_sell_qty"]
        limit_order_ratio = round(tot_buy_qty / tot_sell_qty, 4) if tot_sell_qty > 0 else 0.0

        candle_open = datetime.fromtimestamp(minute_ts).strftime("%H:%M:%S")
        candle_close = datetime.fromtimestamp(minute_ts + 59).strftime("%H:%M:%S")

        candles.append(
            {
                "symbol": last_tick["symbol"],
                "candle_open": candle_open,
                "candle_close": candle_close,
                "ohlcv": {
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                },
                "aggression": {
                    "score": aggression_score,
                    "buy_volume": buy_vol,
                    "sell_volume": sell_vol,
                    "buy_percentage": buy_pct,
                    "sell_percentage": sell_pct,
                },
                "limit_orders": {
                    "total_buy_qty": tot_buy_qty,
                    "total_sell_qty": tot_sell_qty,
                    "buy_sell_ratio": limit_order_ratio,
                },
                "signal": "0",
            }
        )

        if len(candles) > 1:
            prev = candles[-2]
            curr = candles[-1]
            curr_agg = curr["aggression"]
            prev_agg = prev["aggression"]
            curr_buy_pct = curr_agg.get("buy_percentage", 0.0)
            curr_sell_pct = curr_agg.get("sell_percentage", 0.0)
            prev_buy_pct = prev_agg.get("buy_percentage", 0.0)
            prev_sell_pct = prev_agg.get("sell_percentage", 0.0)

            if (
                curr["ohlcv"]["volume"] > prev["ohlcv"]["volume"]
                and curr_buy_pct > curr_sell_pct
                and curr_buy_pct > 60
                and curr_agg.get("buy_volume", 0) > prev_buy_pct
            ):
                curr["signal"] = "1"
            elif (
                curr["ohlcv"]["volume"] > prev["ohlcv"]["volume"]
                and curr_sell_pct > curr_buy_pct
                and curr_sell_pct > 60
                and curr_agg.get("sell_volume", 0) > prev_sell_pct
            ):
                curr["signal"] = "-1"

    return candles
