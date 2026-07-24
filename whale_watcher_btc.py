"""
CryptoHybrid AI -- whale_watcher_btc.py
Fetches whale, market microstructure, and liquidation-zone intelligence data.
Uses free public APIs -- no additional accounts or keys needed.

Sources:
  Kraken:  order book, recent large trades
  Binance: funding rate, open interest, long/short ratio
  Local:   volatility, round number levels, swing level detection, hunt probability
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import krakenex
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("CryptoHybrid.WhaleWatcher")

# ──────────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────────

PAIR_KRAKEN       = "XXBTZGBP"    # Kraken BTC/GBP
PAIR_BINANCE      = "BTCUSDT"     # Binance BTC/USDT (for funding, OI, L/S ratio)
LARGE_TRADE_GBP   = 50_000        # whale trade threshold
ORDER_BOOK_DEPTH  = 10
REQUEST_TIMEOUT   = 8             # seconds
CACHE_SECONDS     = 60

TRAILING_STOP_PCT = 2.0           # our trailing stop -- used for hunt zone warnings

# Funding rate thresholds (%)
FUNDING_VERY_POSITIVE =  0.05
FUNDING_VERY_NEGATIVE = -0.05
FUNDING_MILD_POSITIVE =  0.01
FUNDING_MILD_NEGATIVE = -0.01

# Round number step sizes (GBP)
MAJOR_STEP = 5_000
MINOR_STEP = 1_000
KEY_STEP   = 500

SWING_WINDOW   = 3    # bars each side for swing detection
SWING_BARS     = 200  # how many 5m bars to scan for swings

# ──────────────────────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {}

def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry["at"]) < CACHE_SECONDS:
        return entry["data"]
    return None

def _set_cache(key: str, data) -> None:
    _cache[key] = {"data": data, "at": time.monotonic()}


# ──────────────────────────────────────────────────────────────────────────────
# Open Interest history (module-level, tracks last 3 calls for trend)
# ──────────────────────────────────────────────────────────────────────────────

_oi_history: list = []   # [(oi_btc, approx_usdt_price), ...]
_MAX_OI_HIST = 3


def _update_oi_history(oi_btc: float, price_approx: float = 0.0) -> None:
    global _oi_history
    _oi_history.append((oi_btc, price_approx))
    if len(_oi_history) > _MAX_OI_HIST:
        _oi_history.pop(0)


def _oi_trend() -> str:
    if len(_oi_history) < 2:
        return "STABLE"
    first, last = _oi_history[0][0], _oi_history[-1][0]
    change_pct  = (last - first) / max(first, 1) * 100
    if change_pct >  1.0:
        return "RISING"
    if change_pct < -1.0:
        return "FALLING"
    return "STABLE"


def _oi_context() -> str:
    """Interpret OI trend alongside price direction."""
    if len(_oi_history) < 2:
        return "insufficient history for trend"
    trend       = _oi_trend()
    first_price = _oi_history[0][1]
    last_price  = _oi_history[-1][1]
    if first_price <= 0 or last_price <= 0:
        return f"OI {trend.lower()}"
    price_rising = last_price > first_price * 1.001
    price_falling = last_price < first_price * 0.999
    if trend == "RISING"  and price_rising:
        return "longs building -- more stops to hunt on reversal"
    if trend == "RISING"  and price_falling:
        return "shorts building -- short squeeze risk on recovery"
    if trend == "FALLING":
        return "positions closing -- trend may be weakening"
    return "stable positioning"


# ──────────────────────────────────────────────────────────────────────────────
# Existing sources: order book, large trades, funding rate, volatility
# ──────────────────────────────────────────────────────────────────────────────

def get_order_book_analysis(client: krakenex.API) -> dict:
    cached = _get_cached("order_book")
    if cached:
        return cached
    try:
        result = client.query_public("Depth", {"pair": PAIR_KRAKEN, "count": ORDER_BOOK_DEPTH})
        if result.get("error"):
            raise ValueError(f"Kraken error: {result['error']}")
        pair_key = next((k for k in result["result"] if k != "last"), None)
        book     = result["result"][pair_key]
        bids     = [(float(p), float(v)) for p, v, _ in book["bids"]]
        asks     = [(float(p), float(v)) for p, v, _ in book["asks"]]
        if not bids or not asks:
            return _ob_unavail()
        best_bid  = bids[0][0]
        best_ask  = asks[0][0]
        spread    = best_ask - best_bid
        spread_pct = (spread / best_ask) * 100
        bid_value = sum(p * v for p, v in bids)
        ask_value = sum(p * v for p, v in asks)
        ratio     = bid_value / ask_value if ask_value > 0 else 1.0
        buy_wall  = max(p * v for p, v in bids)
        sell_wall = max(p * v for p, v in asks)
        buy_wall_price  = max(bids, key=lambda x: x[0] * x[1])[0]
        sell_wall_price = min(asks, key=lambda x: x[0] * x[1])[0]
        if ratio > 1.5:
            pressure = "Strong buying pressure -- significantly more bids than asks"
        elif ratio > 1.2:
            pressure = "Moderate buying pressure -- more bids than asks"
        elif ratio < 0.67:
            pressure = "Strong selling pressure -- significantly more asks than bids"
        elif ratio < 0.83:
            pressure = "Moderate selling pressure -- more asks than bids"
        else:
            pressure = "Balanced order book -- no clear pressure"
        data = {
            "bid_ask_ratio": round(ratio, 3), "bid_value_gbp": round(bid_value, 0),
            "ask_value_gbp": round(ask_value, 0), "buy_wall_gbp": round(buy_wall, 0),
            "buy_wall_price": round(buy_wall_price, 2), "sell_wall_gbp": round(sell_wall, 0),
            "sell_wall_price": round(sell_wall_price, 2), "spread_pct": round(spread_pct, 4),
            "best_bid": round(best_bid, 2), "best_ask": round(best_ask, 2),
            "assessment": pressure, "available": True,
        }
        _set_cache("order_book", data)
        return data
    except Exception as e:
        log.warning("Order book fetch failed: %s", e)
        return _ob_unavail()

def _ob_unavail() -> dict:
    return {"available": False, "assessment": "Order book data unavailable"}


def get_large_trades(client: krakenex.API) -> dict:
    cached = _get_cached("large_trades")
    if cached:
        return cached
    try:
        result   = client.query_public("Trades", {"pair": PAIR_KRAKEN})
        if result.get("error"):
            raise ValueError(f"Kraken error: {result['error']}")
        pair_key = next((k for k in result["result"] if k != "last"), None)
        trades   = result["result"][pair_key]
        large_buys = large_sells = []
        large_buys, large_sells = [], []
        for t in trades:
            price = float(t[0]); vol = float(t[1]); side = t[3]
            val   = price * vol
            if val >= LARGE_TRADE_GBP:
                (large_buys if side == "b" else large_sells).append(val)
        nb, ns = len(large_buys), len(large_sells)
        tb, ts = sum(large_buys), sum(large_sells)
        if nb == 0 and ns == 0:
            assessment = f"No whale trades (threshold GBP {LARGE_TRADE_GBP:,})"
            sentiment  = "NEUTRAL"
        elif nb > ns * 2:
            assessment = f"{nb} large buys vs {ns} large sells -- whales buying"
            sentiment  = "BULLISH"
        elif ns > nb * 2:
            assessment = f"{ns} large sells vs {nb} large buys -- whales selling"
            sentiment  = "BEARISH"
        else:
            assessment = f"{nb} large buys, {ns} large sells -- mixed whale activity"
            sentiment  = "MIXED"
        data = {
            "large_buy_count": nb, "large_sell_count": ns,
            "large_buy_value": round(tb, 0), "large_sell_value": round(ts, 0),
            "whale_sentiment": sentiment, "assessment": assessment,
            "threshold_gbp": LARGE_TRADE_GBP, "available": True,
        }
        _set_cache("large_trades", data)
        return data
    except Exception as e:
        log.warning("Large trades fetch failed: %s", e)
        return {"available": False, "assessment": "Trade data unavailable"}


def get_funding_rate() -> dict:
    cached = _get_cached("funding_rate")
    if cached:
        return cached
    try:
        resp     = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": PAIR_BINANCE, "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        raw      = resp.json()
        if not raw or isinstance(raw, dict):
            raise ValueError("Unexpected funding rate response")
        rate_pct = float(raw[0]["fundingRate"]) * 100
        if rate_pct >= FUNDING_VERY_POSITIVE:
            assessment = (f"Funding very positive ({rate_pct:.4f}%) -- "
                          f"overleveraged longs at risk. Bearish signal.")
            sentiment = "BEARISH"
        elif rate_pct >= FUNDING_MILD_POSITIVE:
            assessment = (f"Funding mildly positive ({rate_pct:.4f}%) -- "
                          f"slight long bias. Neutral to slightly bearish.")
            sentiment = "NEUTRAL"
        elif rate_pct <= FUNDING_VERY_NEGATIVE:
            assessment = (f"Funding very negative ({rate_pct:.4f}%) -- "
                          f"overleveraged shorts at risk. Squeeze risk. Bullish.")
            sentiment = "BULLISH"
        elif rate_pct <= FUNDING_MILD_NEGATIVE:
            assessment = (f"Funding mildly negative ({rate_pct:.4f}%) -- "
                          f"slight short bias. Neutral to slightly bullish.")
            sentiment = "NEUTRAL"
        else:
            assessment = (f"Funding near zero ({rate_pct:.4f}%) -- "
                          f"balanced positioning. No squeeze risk.")
            sentiment = "NEUTRAL"
        data = {"funding_rate_pct": round(rate_pct, 4), "sentiment": sentiment,
                "assessment": assessment, "available": True}
        _set_cache("funding_rate", data)
        return data
    except Exception as e:
        log.warning("Funding rate fetch failed: %s", e)
        return {"available": False, "assessment": "Funding rate unavailable"}


def get_volatility_assessment(df_5m: pd.DataFrame) -> dict:
    try:
        if df_5m is None or df_5m.empty or len(df_5m) < 12:
            return {"available": False, "assessment": "Not enough data for volatility"}
        recent    = df_5m.tail(12)
        returns   = recent["close"].pct_change().dropna()
        vol_pct   = returns.std() * 100
        avg_range = (recent["high"] - recent["low"]).mean()
        avg_price = recent["close"].mean()
        range_pct = (avg_range / avg_price) * 100
        if range_pct > 1.0:
            assessment = f"HIGH volatility -- avg candle range {range_pct:.2f}%. Wider stops may be needed."
            level = "HIGH"
        elif range_pct > 0.5:
            assessment = f"NORMAL volatility -- avg candle range {range_pct:.2f}%. Good conditions."
            level = "NORMAL"
        else:
            assessment = f"LOW volatility -- avg candle range {range_pct:.2f}%. Market may be coiling."
            level = "LOW"
        return {"volatility_pct": round(vol_pct, 4), "avg_range_pct": round(range_pct, 4),
                "level": level, "assessment": assessment, "available": True}
    except Exception as e:
        log.warning("Volatility calculation failed: %s", e)
        return {"available": False, "assessment": "Volatility data unavailable"}


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 1 -- Binance Open Interest and Long/Short Ratio
# ──────────────────────────────────────────────────────────────────────────────

def get_open_interest() -> dict:
    """
    Fetch BTC total open interest from Binance futures.
    Tracks last 3 readings for trend detection.
    OI rising + price rising = longs building (hunt risk on reversal).
    OI rising + price falling = shorts building (squeeze risk on recovery).
    """
    cached = _get_cached("open_interest")
    if cached:
        return cached
    try:
        resp    = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": PAIR_BINANCE},
            timeout=REQUEST_TIMEOUT,
        )
        raw     = resp.json()
        oi_btc  = float(raw["openInterest"])
        _update_oi_history(oi_btc)
        trend   = _oi_trend()
        context = _oi_context()
        data    = {
            "oi_btc":    round(oi_btc, 2),
            "trend":     trend,
            "context":   context,
            "available": True,
        }
        _set_cache("open_interest", data)
        return data
    except Exception as e:
        log.warning("Open interest fetch failed: %s", e)
        return {"available": False}


def get_long_short_ratio() -> dict:
    """
    Fetch global long/short account ratio from Binance.
    > 1.5: crowd heavily long -- contrarian bearish (downward hunt likely).
    < 0.67: crowd heavily short -- contrarian bullish (squeeze likely).
    """
    cached = _get_cached("long_short_ratio")
    if cached:
        return cached
    try:
        resp = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": PAIR_BINANCE, "period": "5m", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        raw  = resp.json()
        if not raw:
            raise ValueError("Empty L/S response")
        ratio     = float(raw[0]["longShortRatio"])
        long_pct  = float(raw[0]["longAccount"]) * 100
        short_pct = float(raw[0]["shortAccount"]) * 100
        if ratio > 1.5:
            label     = "HEAVILY LONG"
            sentiment = "CONTRARIAN_BEARISH"
            note      = f"crowd {long_pct:.0f}% long -- contrarian bearish, downward hunt likely"
        elif ratio > 1.2:
            label     = "LEANING LONG"
            sentiment = "MILDLY_BEARISH"
            note      = f"crowd leaning long ({long_pct:.0f}%) -- slight contrarian bearish bias"
        elif ratio < 0.67:
            label     = "HEAVILY SHORT"
            sentiment = "CONTRARIAN_BULLISH"
            note      = f"crowd {short_pct:.0f}% short -- contrarian bullish, squeeze likely"
        elif ratio < 0.83:
            label     = "LEANING SHORT"
            sentiment = "MILDLY_BULLISH"
            note      = f"crowd leaning short ({short_pct:.0f}%) -- slight contrarian bullish bias"
        else:
            label     = "BALANCED"
            sentiment = "NEUTRAL"
            note      = f"balanced ({long_pct:.0f}% long / {short_pct:.0f}% short)"
        data = {
            "ratio": round(ratio, 3), "long_pct": round(long_pct, 1),
            "short_pct": round(short_pct, 1), "label": label,
            "sentiment": sentiment, "note": note, "available": True,
        }
        _set_cache("long_short_ratio", data)
        return data
    except Exception as e:
        log.warning("Long/short ratio fetch failed: %s", e)
        return {"available": False}


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 2 -- Round Number Proximity Detector
# ──────────────────────────────────────────────────────────────────────────────

def detect_round_number_levels(current_price: float) -> dict:
    """
    Find the nearest round number levels above and below current price.
    Major = £5,000 increments, Minor = £1,000, Key = £500.
    Returns 3 nearest above and 3 nearest below, plus a near-major flag.
    """
    if current_price <= 0:
        return {"available": False, "above": [], "below": [], "near_major_level": False}

    levels = []

    # Major (£5,000)
    base_maj = (int(current_price) // MAJOR_STEP) * MAJOR_STEP
    for i in range(-3, 4):
        p = base_maj + i * MAJOR_STEP
        if p > 0:
            levels.append({
                "price":        p,
                "distance_pct": round((p - current_price) / current_price * 100, 2),
                "significance": "MAJOR",
                "description":  f"£{p:,.0f} major round number",
                "sig_score":    8,
            })

    # Minor (£1,000) -- skip if already a major
    base_min = (int(current_price) // MINOR_STEP) * MINOR_STEP
    for i in range(-5, 6):
        p = base_min + i * MINOR_STEP
        if p > 0 and p % MAJOR_STEP != 0:
            levels.append({
                "price":        p,
                "distance_pct": round((p - current_price) / current_price * 100, 2),
                "significance": "MINOR",
                "description":  f"£{p:,.0f} minor round number",
                "sig_score":    5,
            })

    # Key (£500) -- skip if already minor or major
    base_key = (int(current_price) // KEY_STEP) * KEY_STEP
    for i in range(-7, 8):
        p = base_key + i * KEY_STEP
        if p > 0 and p % MINOR_STEP != 0:
            levels.append({
                "price":        p,
                "distance_pct": round((p - current_price) / current_price * 100, 2),
                "significance": "KEY",
                "description":  f"£{p:,.0f} key level",
                "sig_score":    3,
            })

    above = sorted([l for l in levels if l["distance_pct"] > 0],
                   key=lambda x: x["distance_pct"])[:3]
    below = sorted([l for l in levels if l["distance_pct"] < 0],
                   key=lambda x: x["distance_pct"], reverse=True)[:3]

    near_major = any(
        abs(l["distance_pct"]) < 0.5 and l["significance"] == "MAJOR"
        for l in levels
    )

    return {
        "above":            above,
        "below":            below,
        "near_major_level": near_major,
        "available":        True,
    }


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 3 -- Recent Swing Level Detector
# ──────────────────────────────────────────────────────────────────────────────

def detect_swing_levels(df_5m: pd.DataFrame, current_price: float) -> dict:
    """
    Analyse last SWING_BARS five-minute candles for significant price levels:
      SWING_HIGH / SWING_LOW -- local maxima / minima
      PREV_DAY_HIGH / PREV_DAY_LOW -- institutional reference levels
      HIGH_VOLUME -- where most volume traded in last 24h
    Returns 3 nearest above and 3 nearest below current price.
    """
    try:
        if df_5m is None or df_5m.empty or len(df_5m) < 20 or current_price <= 0:
            return {"available": False, "above": [], "below": []}

        # Work on a copy with positional index for safe .iloc / numpy slicing
        work   = df_5m.tail(SWING_BARS).reset_index(drop=True)
        n      = len(work)
        highs  = work["high"].to_numpy(dtype=float)
        lows   = work["low"].to_numpy(dtype=float)
        W      = SWING_WINDOW
        levels = []

        # Swing highs
        for i in range(W, n - W):
            h    = highs[i]
            left = highs[i - W: i]
            rght = highs[i + 1: i + W + 1]
            if h > left.max() and h > rght.max():
                local_range = highs[i - W: i + W + 1].max() - lows[i - W: i + W + 1].min()
                second      = max(left.max(), rght.max())
                prominence  = (h - second) / max(local_range, 1.0) * 10
                levels.append({
                    "price":     int(round(h)),
                    "type":      "SWING_HIGH",
                    "sig_score": min(10, max(1, int(prominence * 2.5 + 3))),
                })

        # Swing lows
        for i in range(W, n - W):
            lo   = lows[i]
            left = lows[i - W: i]
            rght = lows[i + 1: i + W + 1]
            if lo < left.min() and lo < rght.min():
                local_range = highs[i - W: i + W + 1].max() - lows[i - W: i + W + 1].min()
                second      = min(left.min(), rght.min())
                prominence  = (second - lo) / max(local_range, 1.0) * 10
                levels.append({
                    "price":     int(round(lo)),
                    "type":      "SWING_LOW",
                    "sig_score": min(10, max(1, int(prominence * 2.5 + 3))),
                })

        # Previous day high and low (using the original DatetimeIndex if available)
        try:
            orig = df_5m.tail(SWING_BARS)
            idx  = orig.index
            if hasattr(idx, 'date') or (hasattr(idx, 'dtype') and 'datetime' in str(idx.dtype)):
                date_list = [ts.date() for ts in idx]
                unique_d  = sorted(set(date_list), reverse=True)
                if len(unique_d) >= 2:
                    yesterday = unique_d[1]
                    prev_rows = orig[[d == yesterday for d in date_list]]
                    if len(prev_rows) >= 5:
                        ph = int(round(float(prev_rows["high"].max())))
                        pl = int(round(float(prev_rows["low"].min())))
                        levels.append({"price": ph, "type": "PREV_DAY_HIGH", "sig_score": 8})
                        levels.append({"price": pl, "type": "PREV_DAY_LOW",  "sig_score": 8})
        except Exception:
            pass

        # Highest volume price level in last 24h
        if "volume" in work.columns:
            try:
                last_24h = work.tail(min(288, n))
                mids     = ((last_24h["high"] + last_24h["low"]) / 2).to_numpy(dtype=float)
                vols     = last_24h["volume"].to_numpy(dtype=float)
                if len(mids) > 0 and mids.max() > mids.min():
                    bins: dict = {}
                    for p_m, v in zip(mids, vols):
                        b = int(round(p_m / KEY_STEP)) * KEY_STEP
                        bins[b] = bins.get(b, 0.0) + v
                    if bins:
                        hvl = max(bins, key=bins.get)
                        levels.append({"price": hvl, "type": "HIGH_VOLUME", "sig_score": 7})
            except Exception:
                pass

        # Attach distance from current price
        for lv in levels:
            lv["distance_pct"] = round((lv["price"] - current_price) / current_price * 100, 2)

        # Deduplicate levels that are within 0.4% of each other (keep highest sig)
        def _dedup(lst):
            lst_s = sorted(lst, key=lambda x: -x["sig_score"])
            kept  = []
            for lv in lst_s:
                if not any(abs(lv["price"] - k["price"]) / max(k["price"], 1) * 100 < 0.4
                           for k in kept):
                    kept.append(lv)
            return kept

        above_raw = _dedup([lv for lv in levels if lv["distance_pct"] >  0.1])
        below_raw = _dedup([lv for lv in levels if lv["distance_pct"] < -0.1])

        above = sorted(above_raw, key=lambda x: x["distance_pct"])[:3]
        below = sorted(below_raw, key=lambda x: x["distance_pct"], reverse=True)[:3]

        return {
            "above":     above,
            "below":     below,
            "available": True,
        }

    except Exception as e:
        log.warning("Swing level detection failed: %s", e)
        return {"available": False, "above": [], "below": []}


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 4 -- Hunt Probability Scorer
# ──────────────────────────────────────────────────────────────────────────────

def calculate_hunt_probability(
    current_price: float,
    ob_data: dict,
    funding_data: dict,
    ls_data: dict,
    oi_data: dict,
    round_levels: dict,
    swing_levels: dict,
) -> dict:
    """
    Score the probability of stop hunts in each direction (0-100).
    Combines funding rate, L/S ratio, OI context, order book, and level proximity.
    """
    down_score = 0   # probability of downward hunt (hunting long stops)
    up_score   = 0   # probability of upward hunt (hunting short stops)
    key_levels = []  # levels within trailing stop range

    # ── Funding rate ──────────────────────────────────────────────────────────
    if funding_data.get("available"):
        rate = funding_data.get("funding_rate_pct", 0)
        if rate >= FUNDING_VERY_POSITIVE:
            down_score += 25
        elif rate >= FUNDING_MILD_POSITIVE:
            down_score += 10
        elif rate <= FUNDING_VERY_NEGATIVE:
            up_score += 25
        elif rate <= FUNDING_MILD_NEGATIVE:
            up_score += 10

    # ── Long/Short ratio (use 1.3 / 0.77 as scoring thresholds per spec) ──────
    if ls_data.get("available"):
        ratio = ls_data.get("ratio", 1.0)
        if ratio > 1.3:
            down_score += 25
        elif ratio > 1.1:
            down_score += 10
        elif ratio < 0.77:
            up_score += 25
        elif ratio < 0.91:
            up_score += 10

    # ── OI context ────────────────────────────────────────────────────────────
    if oi_data.get("available"):
        ctx = oi_data.get("context", "")
        if "longs building" in ctx:
            down_score += 10
        elif "shorts building" in ctx:
            up_score += 10

    # ── Order book bias ───────────────────────────────────────────────────────
    if ob_data.get("available"):
        ob_ratio = ob_data.get("bid_ask_ratio", 1.0)
        if ob_ratio < 0.67:      # heavy selling in book
            down_score += 10
        elif ob_ratio > 1.5:     # heavy buying in book
            up_score += 10

    # ── Level proximity: combine round + swing levels ─────────────────────────
    all_below = (
        (round_levels.get("below", []) if round_levels.get("available") else []) +
        (swing_levels.get("below", [])  if swing_levels.get("available") else [])
    )
    all_above = (
        (round_levels.get("above", []) if round_levels.get("available") else []) +
        (swing_levels.get("above", [])  if swing_levels.get("available") else [])
    )

    for lv in all_below:
        dist    = abs(lv.get("distance_pct", 99))
        sig_n   = _level_sig_score(lv)
        if dist <= TRAILING_STOP_PCT:
            key_levels.append({**lv, "direction": "BELOW", "in_stop_range": True})
        if dist <= 2.0:
            down_score += 20 if sig_n >= 7 else 10 if sig_n >= 4 else 5

    for lv in all_above:
        dist    = abs(lv.get("distance_pct", 99))
        sig_n   = _level_sig_score(lv)
        if dist <= TRAILING_STOP_PCT:
            key_levels.append({**lv, "direction": "ABOVE", "in_stop_range": True})
        if dist <= 2.0:
            up_score += 20 if sig_n >= 7 else 10 if sig_n >= 4 else 5

    # Near major round number -- ambiguous pressure both ways
    if round_levels.get("near_major_level"):
        down_score += 5
        up_score   += 5

    down_score = min(100, down_score)
    up_score   = min(100, up_score)

    if down_score >= up_score + 20:
        primary_risk = "DOWNWARD"
    elif up_score >= down_score + 20:
        primary_risk = "UPWARD"
    else:
        primary_risk = "BALANCED"

    recommendation = _build_recommendation(
        down_score, up_score, primary_risk, key_levels, current_price,
        round_levels, swing_levels, ls_data, funding_data,
    )

    return {
        "down_hunt_score":    down_score,
        "up_hunt_score":      up_score,
        "primary_risk":       primary_risk,
        "key_levels_at_risk": key_levels,
        "recommendation":     recommendation,
        "available":          True,
    }


def _level_sig_score(lv: dict) -> int:
    """Return a numeric significance score for any level dict."""
    if "sig_score" in lv:
        return int(lv["sig_score"])
    sig_map = {"MAJOR": 8, "MINOR": 5, "KEY": 3}
    return sig_map.get(lv.get("significance", ""), 4)


def _build_recommendation(
    down_score: int, up_score: int, primary_risk: str,
    key_levels: list, current_price: float,
    round_levels: dict, swing_levels: dict,
    ls_data: dict, funding_data: dict,
) -> str:
    parts = []

    # Levels in stop range
    if key_levels:
        for lv in key_levels[:2]:
            dist  = abs(lv.get("distance_pct", 0))
            dirn  = lv.get("direction", "").lower()
            price = lv.get("price", 0)
            label = _level_display_name(lv)
            parts.append(
                f"£{price:,.0f} ({label}) sits {dist:.1f}% {dirn} -- "
                f"within {TRAILING_STOP_PCT:.0f}% trailing stop range."
            )

    # Primary directional risk
    if primary_risk == "DOWNWARD" and down_score >= 50:
        parts.append(
            f"HIGH downward hunt risk ({down_score}/100) -- "
            f"long positions may be targeted. Extra caution on LONG entries."
        )
    elif primary_risk == "UPWARD" and up_score >= 50:
        parts.append(
            f"HIGH upward hunt risk ({up_score}/100) -- "
            f"short squeeze risk. Extra caution on SHORT entries."
        )
    elif primary_risk == "BALANCED" and (down_score >= 30 or up_score >= 30):
        parts.append(
            f"Moderate two-way hunt risk ({down_score}/100 down, {up_score}/100 up). "
            f"Be alert in both directions."
        )
    else:
        parts.append(
            f"Low hunt risk ({down_score}/100 down, {up_score}/100 up). "
            f"No major levels in danger zone. Normal entry criteria apply."
        )

    return " ".join(parts)


def _level_display_name(lv: dict) -> str:
    """Return a short human-readable name for any level dict."""
    if "significance" in lv:
        return f"{lv['significance']} round number"
    type_labels = {
        "SWING_HIGH":    "swing high",
        "SWING_LOW":     "swing low",
        "PREV_DAY_HIGH": "prev day high",
        "PREV_DAY_LOW":  "prev day low",
        "HIGH_VOLUME":   "high volume",
    }
    return type_labels.get(lv.get("type", ""), "level")


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 5 -- Format liquidation zone section
# ──────────────────────────────────────────────────────────────────────────────

def _format_liq_section(
    oi_data: dict,
    ls_data: dict,
    round_levels: dict,
    swing_levels: dict,
    hunt_prob: dict,
) -> str:
    """Build the LIQUIDATION ZONE ANALYSIS multi-line block."""
    lines = [""]  # leading newline so key: and block are on separate lines

    # OI
    if oi_data.get("available"):
        oi_btc  = oi_data["oi_btc"]
        trend   = oi_data["trend"]
        context = oi_data.get("context", "")
        lines.append(f"  Open Interest:    {oi_btc:,.0f} BTC total (OI {trend})")
        if context:
            lines.append(f"                    {context}")
    else:
        lines.append("  Open Interest:    Unavailable")

    # L/S ratio
    if ls_data.get("available"):
        ratio = ls_data["ratio"]
        label = ls_data["label"]
        note  = ls_data.get("note", "")
        lines.append(f"  Long/Short ratio: {ratio:.2f} ({label})")
        if note:
            lines.append(f"                    {note}")
    else:
        lines.append("  Long/Short ratio: Unavailable")

    # Combine and sort all levels
    all_below = (
        (round_levels.get("below", []) if round_levels.get("available") else []) +
        (swing_levels.get("below", [])  if swing_levels.get("available") else [])
    )
    all_above = (
        (round_levels.get("above", []) if round_levels.get("available") else []) +
        (swing_levels.get("above", [])  if swing_levels.get("available") else [])
    )
    all_below.sort(key=lambda x: abs(x.get("distance_pct", 99)))
    all_above.sort(key=lambda x: abs(x.get("distance_pct", 99)))

    def _sig_text(lv: dict) -> str:
        s = _level_sig_score(lv)
        return "HIGH" if s >= 7 else "MEDIUM" if s >= 4 else "LOW"

    if all_below:
        lines.append("")
        lines.append("  LEVELS BELOW (potential long liquidations):")
        for lv in all_below[:3]:
            name = _level_display_name(lv)
            dist = abs(lv.get("distance_pct", 0))
            sig  = _sig_text(lv)
            lines.append(f"    £{lv['price']:,.0f} -- {name}, {dist:.1f}% below ({sig} significance)")

    if all_above:
        lines.append("")
        lines.append("  LEVELS ABOVE (potential short liquidations):")
        for lv in all_above[:3]:
            name = _level_display_name(lv)
            dist = abs(lv.get("distance_pct", 0))
            sig  = _sig_text(lv)
            lines.append(f"    £{lv['price']:,.0f} -- {name}, {dist:.1f}% above ({sig} significance)")

    if round_levels.get("near_major_level"):
        lines.append("  ** Price is sitting on or very near a major round number **")

    # Hunt probability
    if hunt_prob.get("available"):
        ds     = hunt_prob["down_hunt_score"]
        us     = hunt_prob["up_hunt_score"]
        risk   = hunt_prob["primary_risk"]
        d_lbl  = "HIGH" if ds >= 85 else "MEDIUM" if ds >= 30 else "LOW"
        u_lbl  = "HIGH" if us >= 85 else "MEDIUM" if us >= 30 else "LOW"
        recom  = hunt_prob.get("recommendation", "")
        lines += [
            "",
            "  HUNT PROBABILITY:",
            f"    Downward hunt: {ds}/100 ({d_lbl})",
            f"    Upward hunt:   {us}/100 ({u_lbl})",
            f"    Primary risk:  {risk}",
        ]
        if recom:
            lines += ["", "  RECOMMENDATION:", f"    {recom}"]

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# ENHANCEMENT 5 -- Master get_whale_data (updated)
# ──────────────────────────────────────────────────────────────────────────────

def get_whale_data(
    client: krakenex.API,
    df_5m: pd.DataFrame,
) -> dict:
    """
    Fetch all whale data sources and package them into a dict for Claude.
    Each source is independent -- if one fails, others still work.
    Now includes LIQUIDATION ZONE ANALYSIS with OI, L/S ratio, round/swing levels,
    and hunt probability scoring.
    """
    log.info("Fetching whale and liquidation zone data...")

    # ── Existing sources ──────────────────────────────────────────────────────
    order_book   = get_order_book_analysis(client)
    large_trades = get_large_trades(client)
    funding      = get_funding_rate()
    volatility   = get_volatility_assessment(df_5m)

    # ── New sources ───────────────────────────────────────────────────────────
    oi_data = get_open_interest()
    ls_data = get_long_short_ratio()

    # Derive current price from df_5m
    current_price = 0.0
    try:
        if df_5m is not None and not df_5m.empty:
            current_price = float(df_5m["close"].iloc[-1])
    except Exception:
        pass

    round_levels = (detect_round_number_levels(current_price)
                    if current_price > 0 else {"available": False, "above": [], "below": []})
    swing_levels = (detect_swing_levels(df_5m, current_price)
                    if current_price > 0 and df_5m is not None else {"available": False, "above": [], "below": []})

    hunt_prob = calculate_hunt_probability(
        current_price=current_price,
        ob_data=order_book,
        funding_data=funding,
        ls_data=ls_data,
        oi_data=oi_data,
        round_levels=round_levels,
        swing_levels=swing_levels,
    )

    # ── Package for Claude ────────────────────────────────────────────────────
    whale_data = {}

    if order_book.get("available"):
        whale_data["Order book pressure"]  = order_book["assessment"]
        whale_data["Bid/Ask ratio"]        = (f"{order_book['bid_ask_ratio']:.2f} "
                                              f"(>1 = more buyers, <1 = more sellers)")
        whale_data["Largest buy wall"]     = (f"GBP {order_book['buy_wall_gbp']:,.0f} "
                                              f"at GBP {order_book['buy_wall_price']:,.2f}")
        whale_data["Largest sell wall"]    = (f"GBP {order_book['sell_wall_gbp']:,.0f} "
                                              f"at GBP {order_book['sell_wall_price']:,.2f}")
        whale_data["Spread"]               = f"{order_book['spread_pct']:.4f}%"
    else:
        whale_data["Order book"] = "Unavailable"

    if large_trades.get("available"):
        whale_data["Large trades (whale activity)"] = large_trades["assessment"]
        whale_data["Whale sentiment"]               = large_trades["whale_sentiment"]
    else:
        whale_data["Large trades"] = "Unavailable"

    if funding.get("available"):
        whale_data["Futures funding rate"] = funding["assessment"]
        whale_data["Funding sentiment"]    = funding["sentiment"]
    else:
        whale_data["Funding rate"] = "Unavailable"

    if volatility.get("available"):
        whale_data["Market volatility"] = volatility["assessment"]
        whale_data["Volatility level"]  = volatility["level"]
    else:
        whale_data["Volatility"] = "Unavailable"

    # ── Liquidation zone block ────────────────────────────────────────────────
    whale_data["LIQUIDATION ZONE ANALYSIS"] = _format_liq_section(
        oi_data, ls_data, round_levels, swing_levels, hunt_prob
    )

    whale_data["Data timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Compact structured data for dashboard liquidation zone display
    _near_above = round_levels.get("above", [])[:1]
    _near_below = round_levels.get("below", [])[:1]
    _near_all   = _near_above + _near_below
    _near_lv    = min(_near_all, key=lambda x: x.get("dist_pct", 999)) if _near_all else None
    whale_data["_liq_compact"] = {
        "hunt_down":       hunt_prob.get("down_hunt_score", 0) if hunt_prob.get("available") else None,
        "hunt_up":         hunt_prob.get("up_hunt_score",   0) if hunt_prob.get("available") else None,
        "hunt_verdict":    hunt_prob.get("primary_risk", "BALANCED"),
        "ls_ratio":        round(float(ls_data["ratio"]), 2) if ls_data.get("available") and ls_data.get("ratio") is not None else None,
        "ls_label":        ls_data.get("sentiment", ""),
        "key_level_price": _near_lv["price"]                                    if _near_lv else None,
        "key_level_type":  _near_lv.get("type", "")                             if _near_lv else None,
        "key_level_dist":  round(float(_near_lv.get("dist_pct", 0)), 1)         if _near_lv else None,
        "available":       hunt_prob.get("available", False),
    }

    log.info(
        "Whale data ready -- OB=%s trades=%s funding=%s vol=%s OI=%s LS=%s "
        "round=%s swing=%s hunt=(%d/%d %s)",
        "OK" if order_book.get("available") else "FAIL",
        "OK" if large_trades.get("available") else "FAIL",
        "OK" if funding.get("available") else "FAIL",
        "OK" if volatility.get("available") else "FAIL",
        "OK" if oi_data.get("available") else "FAIL",
        "OK" if ls_data.get("available") else "FAIL",
        "OK" if round_levels.get("available") else "FAIL",
        "OK" if swing_levels.get("available") else "FAIL",
        hunt_prob.get("down_hunt_score", 0),
        hunt_prob.get("up_hunt_score", 0),
        hunt_prob.get("primary_risk", "?"),
    )

    return whale_data


def format_whale_data_for_display(whale_data: dict) -> str:
    lines = ["=" * 60, "  CryptoHybrid AI -- Whale Data", "=" * 60]
    for key, value in whale_data.items():
        lines.append(f"  {key}:")
        for sub in str(value).split("\n"):
            lines.append(f"    {sub}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("CryptoHybrid AI -- Whale Watcher (enhanced) self-test")

    from data_feed_btc import BTCDataFeed

    feed = BTCDataFeed()
    feed.initialise()
    df_5m = feed.get("5m")

    whale_data = get_whale_data(feed.client, df_5m)
    print(format_whale_data_for_display(whale_data))

    log.info("Whale watcher self-test complete.")
