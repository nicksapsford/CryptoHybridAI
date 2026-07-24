"""
CryptoHybrid AI -- data_feed_eth.py
ETH/GBP live & historical data feed via Kraken (krakenex)
Dual-timeframe: 1h (strategic) + 5m (tactical)
Starting capital: GBP 1,000 paper-trading mode
"""

import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import krakenex
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Config & Constants
# ──────────────────────────────────────────────────────────────────────────────

PAIR          = "XETHZGBP"
PAIR_DISPLAY  = "ETH/GBP"

INTERVALS = {
    "5m":  5,
    "1h":  60,
    "1d":  1440,
}

STARTING_CAPITAL_GBP = 1_000.0

MAX_RETRIES    = 3
RETRY_DELAY_S  = 5
RATE_LIMIT_S   = 1.5

log = logging.getLogger("CryptoHybrid.DataFeed.ETH")

BASE_DIR = Path(__file__).resolve().parent

# ──────────────────────────────────────────────────────────────────────────────
# Kraken client
# ──────────────────────────────────────────────────────────────────────────────

def _load_env() -> tuple[str, str]:
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        log.info("Loaded .env from %s", env_path)
    else:
        load_dotenv()
        log.warning(".env not found at expected path; falling back to env vars")

    api_key    = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")

    if not api_key or not api_secret:
        log.warning("API keys not set -- public endpoints only")
    return api_key, api_secret


def build_client() -> krakenex.API:
    api_key, api_secret = _load_env()
    k = krakenex.API(key=api_key, secret=api_secret)
    log.info("Kraken client ready -- pair: %s", PAIR_DISPLAY)
    return k


# ──────────────────────────────────────────────────────────────────────────────
# Raw OHLC fetch
# ──────────────────────────────────────────────────────────────────────────────

def _query_ohlc(
    client: krakenex.API,
    interval: int,
    since: Optional[int] = None,
) -> dict:
    params: dict = {"pair": PAIR, "interval": interval}
    if since:
        params["since"] = since

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.query_public("OHLC", params)
            if resp.get("error"):
                raise ValueError(f"Kraken API error: {resp['error']}")
            return resp["result"]
        except Exception as exc:
            log.warning("OHLC query attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_S)
    raise RuntimeError(f"Failed to fetch OHLC after {MAX_RETRIES} attempts")


# ──────────────────────────────────────────────────────────────────────────────
# Parse OHLC into a DataFrame
# ──────────────────────────────────────────────────────────────────────────────

_OHLC_COLS = ["timestamp", "open", "high", "low", "close", "vwap", "volume", "count"]

def _parse_ohlc(raw: dict) -> pd.DataFrame:
    pair_key = next((k for k in raw if k != "last"), None)
    if pair_key is None:
        raise ValueError("No candle data found in Kraken OHLC response")

    df = pd.DataFrame(raw[pair_key], columns=_OHLC_COLS)
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
    for col in ["open", "high", "low", "close", "vwap", "volume"]:
        df[col] = df[col].astype(float)
    df["count"] = df["count"].astype(int)
    df = df.iloc[:-1].reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────────────────

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd":      macd_line,
        "signal":    signal_line,
        "histogram": macd_line - signal_line,
    })


def _calc_ssl_cloud(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    sma_high = df["high"].rolling(period).mean()
    sma_low  = df["low"].rolling(period).mean()
    hlv      = pd.Series(
        np.where(df["close"] > sma_high, 1,
        np.where(df["close"] < sma_low, -1, np.nan)),
        index=df.index
    )
    hlv      = hlv.ffill()
    ssl_up   = np.where(hlv < 0, sma_low,  sma_high)
    ssl_down = np.where(hlv < 0, sma_high, sma_low)
    return pd.DataFrame({
        "ssl_up":   ssl_up,
        "ssl_down": ssl_down,
        "ssl_bull": ssl_up > ssl_down,
    }, index=df.index)


def _calc_tmo(
    df: pd.DataFrame,
    length: int = 14,
    calc_length: int = 5,
) -> pd.DataFrame:
    mom    = np.sign(df["close"] - df["open"]).rolling(length).sum()
    main   = mom.ewm(span=calc_length, adjust=False).mean()
    smooth = main.ewm(span=calc_length, adjust=False).mean()
    return pd.DataFrame({"tmo_main": main, "tmo_smooth": smooth}, index=df.index)


def _calc_chande(df: pd.DataFrame, period: int = 20) -> pd.Series:
    diff   = df["close"].diff()
    up_sum = diff.clip(lower=0).rolling(period).sum()
    dn_sum = (-diff.clip(upper=0)).rolling(period).sum()
    denom  = up_sum + dn_sum
    return (100 * (up_sum - dn_sum) / denom.replace(0, np.nan)).rename("chande_mo")


def _calc_money_flow(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    mfv = tp * df["volume"] * np.sign(df["close"] - df["open"])
    return (mfv.rolling(period).sum() / df["volume"].rolling(period).sum()).rename("money_flow")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all CryptoHybrid AI indicators on a full OHLCV DataFrame."""
    if df.empty:
        return df

    df["rsi"]            = _calc_rsi(df["close"])
    macd_df              = _calc_macd(df["close"])
    df["macd"]           = macd_df["macd"]
    df["macd_signal"]    = macd_df["signal"]
    df["macd_histogram"] = macd_df["histogram"]
    ssl_df               = _calc_ssl_cloud(df)
    df["ssl_up"]         = ssl_df["ssl_up"]
    df["ssl_down"]       = ssl_df["ssl_down"]
    df["ssl_bull"]       = ssl_df["ssl_bull"]
    tmo_df               = _calc_tmo(df)
    df["tmo_main"]       = tmo_df["tmo_main"]
    df["tmo_smooth"]     = tmo_df["tmo_smooth"]
    df["chande_mo"]      = _calc_chande(df)
    df["money_flow"]     = _calc_money_flow(df)

    return df


# ──────────────────────────────────────────────────────────────────────────────
# Composite signal
# ──────────────────────────────────────────────────────────────────────────────

def get_composite_signal(row: pd.Series) -> str:
    """Returns LONG, SHORT, or NEUTRAL based on all indicators combined."""
    signals = []

    if pd.notna(row.get("ssl_bull")):
        signals.append(1 if row["ssl_bull"] else -1)

    rsi = row.get("rsi")
    if pd.notna(rsi):
        signals.append(1 if rsi > 55 else (-1 if rsi < 45 else 0))

    hist = row.get("macd_histogram")
    if pd.notna(hist):
        signals.append(1 if hist > 0 else -1)

    tmo_main, tmo_smooth = row.get("tmo_main"), row.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        signals.append(1 if tmo_main > tmo_smooth else -1)

    cmo = row.get("chande_mo")
    if pd.notna(cmo):
        signals.append(1 if cmo > 0 else -1)

    mf = row.get("money_flow")
    if pd.notna(mf):
        signals.append(1 if mf > 0 else -1)

    if not signals:
        return "NEUTRAL"
    score = sum(signals) / len(signals)
    return "LONG" if score >= 0.5 else ("SHORT" if score <= -0.5 else "NEUTRAL")


# ──────────────────────────────────────────────────────────────────────────────
# Main ETHDataFeed class
# ──────────────────────────────────────────────────────────────────────────────

class ETHDataFeed:
    """
    CryptoHybrid AI -- ETH/GBP dual-timeframe data feed.

    Usage:
        feed = ETHDataFeed()
        feed.initialise()        # fetch full history + compute indicators
        df_1h = feed.get("1h")  # enriched 1-hour DataFrame
        df_5m = feed.get("5m")  # enriched 5-minute DataFrame
        feed.refresh()           # call every 5 mins -- merges new candles
    """

    def __init__(self) -> None:
        self.client      = build_client()
        self._frames: dict[str, pd.DataFrame] = {}
        self._last_since: dict[str, Optional[int]] = {"1d": None, "5m": None, "1h": None}
        self.capital_gbp = STARTING_CAPITAL_GBP
        log.info(
            "CryptoHybrid AI | ETHDataFeed ready | capital=GBP %.2f",
            self.capital_gbp,
        )

    def _fetch_raw(self, tf_label: str) -> pd.DataFrame:
        interval = INTERVALS[tf_label]
        log.info("Fetching %s OHLC (%dm) ...", PAIR_DISPLAY, interval)
        raw = _query_ohlc(self.client, interval, since=self._last_since[tf_label])
        df  = _parse_ohlc(raw)
        return df

    def initialise(self) -> None:
        log.info("=== CryptoHybrid AI -- Initialising ETH data feed ===")
        for tf in ("1d", "1h", "5m"):
            df = self._fetch_raw(tf)
            df = add_indicators(df)
            self._frames[tf] = df

            if not df.empty:
                self._last_since[tf] = int(df.index[-1].timestamp())
                log.info(
                    "  [%s] %d candles | latest close: GBP %.4f | rsi=%.1f | ssl=%s",
                    tf,
                    len(df),
                    df["close"].iloc[-1],
                    df["rsi"].iloc[-1] if pd.notna(df["rsi"].iloc[-1]) else 0,
                    "UP" if df["ssl_bull"].iloc[-1] else "DOWN",
                )
            time.sleep(RATE_LIMIT_S)

        log.info("ETH data feed ready -- all timeframes loaded with full indicators.")

    def refresh(self) -> None:
        log.info("=== CryptoHybrid AI -- Refreshing ETH ===")
        for tf in ("1d", "1h", "5m"):
            new_df = self._fetch_raw(tf)

            if not new_df.empty and tf in self._frames:
                combined = pd.concat([self._frames[tf], new_df])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                combined = add_indicators(combined)
                self._frames[tf] = combined

            elif not new_df.empty:
                new_df = add_indicators(new_df)
                self._frames[tf] = new_df

            if tf in self._frames and not self._frames[tf].empty:
                df = self._frames[tf]
                self._last_since[tf] = int(df.index[-1].timestamp())
                log.info(
                    "  [%s] %d candles total | latest close: GBP %.4f | rsi=%.1f | ssl=%s",
                    tf,
                    len(df),
                    df["close"].iloc[-1],
                    df["rsi"].iloc[-1] if pd.notna(df["rsi"].iloc[-1]) else 0,
                    "UP" if df["ssl_bull"].iloc[-1] else "DOWN",
                )

            time.sleep(RATE_LIMIT_S)

    def get(self, timeframe: str = "5m") -> pd.DataFrame:
        if timeframe not in self._frames:
            raise KeyError(
                f"Timeframe '{timeframe}' not loaded. Call .initialise() first."
            )
        return self._frames[timeframe].copy()

    def latest_bar(self, timeframe: str = "5m") -> pd.Series:
        return self.get(timeframe).iloc[-1]

    def composite_signal(self, timeframe: str = "5m") -> str:
        return get_composite_signal(self.latest_bar(timeframe))

    def print_status(self) -> None:
        log.info("-" * 60)
        log.info(
            "CryptoHybrid AI -- ETH Status  [%s]",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        log.info("ETH paper capital: GBP %.2f", self.capital_gbp)
        for tf in ("1d", "1h", "5m"):
            if tf not in self._frames:
                continue
            bar = self.latest_bar(tf)
            sig = get_composite_signal(bar)
            log.info(
                "  [%s] close=GBP %.4f  rsi=%.1f  macd=%.4f  ssl=%s  "
                "tmo=%.3f  cmo=%.1f  cmf=%.4f  -> %s",
                tf,
                bar["close"],
                bar.get("rsi", 0) if pd.notna(bar.get("rsi")) else 0,
                bar.get("macd", 0) if pd.notna(bar.get("macd")) else 0,
                "UP" if bar.get("ssl_bull") else "DOWN",
                bar.get("tmo_main", 0) if pd.notna(bar.get("tmo_main")) else 0,
                bar.get("chande_mo", 0) if pd.notna(bar.get("chande_mo")) else 0,
                bar.get("money_flow", 0) if pd.notna(bar.get("money_flow")) else 0,
                sig,
            )
        log.info("-" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# 1m bar for fast exit detection
# ──────────────────────────────────────────────────────────────────────────────

def fetch_latest_1m_bar(client: krakenex.API) -> Optional[pd.Series]:
    try:
        resp = client.query_public("OHLC", {"pair": PAIR, "interval": 1})
        if resp.get("error"):
            return None
        df = _parse_ohlc(resp["result"])
        if df.empty or len(df) < 14:
            return None
        df = add_indicators(df)
        return df.iloc[-1]
    except Exception as exc:
        log.warning("ETH 1m bar fetch failed: %s", exc)
        return None


if __name__ == "__main__":
    log.info("CryptoHybrid AI -- ETH data feed test starting...")
    feed = ETHDataFeed()
    feed.initialise()
    feed.print_status()
