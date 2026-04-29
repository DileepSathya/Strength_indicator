from datetime import datetime, timedelta


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
            if last_direction == "UP":
                aggressor = "BUY"
            elif last_direction == "DOWN":
                aggressor = "SELL"
            else:
                aggressor = "UNKNOWN"

        results.append({**tick, "aggressor": aggressor, "vol_delta": vol_delta})

    return results


def aggression_score(ticks: list[dict], window_minutes: int = 1) -> dict:
    """
    Aggregates aggression over a rolling time window.
    Returns a score from -1 (full sell) to +1 (full buy).

    Parameters
    ----------
    ticks          : list of tick dicts (must have 'last_traded_time' as Unix timestamp)
    window_minutes : lookback window in minutes (default 5, change freely)
    """
    classified = classify_aggression(ticks)

    # -- Filter to window ---------------------------------------------------
    latest_time = classified[-1]["last_traded_time"]
    cutoff_time = latest_time - (window_minutes * 60)

    window_ticks = [
        t
        for t in classified
        if t["last_traded_time"] >= cutoff_time and t["aggressor"] != "UNKNOWN"
    ]

    if not window_ticks:
        return {
            "score": 0.0,
            "buy_vol": 0,
            "sell_vol": 0,
            "total_vol": 0,
            "window_minutes": window_minutes,
        }

    # -- Aggregate ----------------------------------------------------------
    buy_vol = sum(t["vol_delta"] for t in window_ticks if t["aggressor"] == "BUY")
    sell_vol = sum(t["vol_delta"] for t in window_ticks if t["aggressor"] == "SELL")
    total_vol = buy_vol + sell_vol

    score = round((buy_vol - sell_vol) / total_vol, 4) if total_vol > 0 else 0.0

    return {
        "score": score,
        "buy_vol": buy_vol,
        "sell_vol": sell_vol,
        "total_vol": total_vol,
        "window_minutes": window_minutes,
        "from_time": datetime.fromtimestamp(cutoff_time).strftime("%H:%M:%S"),
        "to_time": datetime.fromtimestamp(latest_time).strftime("%H:%M:%S"),
    }
