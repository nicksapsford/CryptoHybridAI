"""
CryptoHybrid AI -- regime.py
Market-regime reader for the momentum-scalping reset (System 2 Review, 18 Jul 2026).

Reads the weekly crypto market context published by Gaius and classifies the
regime for BTC & ETH scalping. Shared by the BTC and ETH engines.

  Bull regime : BTC above 200MA AND Fear & Greed >= 40
  Bear regime : BTC below 200MA OR  Fear & Greed <  40

The file is refreshed weekly by Gaius (Sunday 20:00 UTC); between updates the
cached values are used. We additionally cache in-process for 30 minutes so we do
not re-read the file on every candle. All times UTC. Relative paths only.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("CryptoHybrid.Regime")

BASE_DIR = Path(__file__).resolve().parent
# Relative to the desk root: .../Desktop/TideTraderAI -> .../Desktop/GaiusAI/logs
MARKET_CONTEXT_PATH = BASE_DIR.parent / "GaiusAI" / "logs" / "market_context.json"

FG_BULL_MIN   = 40      # Fear & Greed floor for a bull regime
CACHE_SECONDS = 30 * 60  # re-read the file at most every 30 minutes

_cache = {"at": 0.0, "data": None}

# Latest BTC 5m ATR (GBP), published by the BTC engine each tick and read by BOTH
# the BTC and ETH volatility gates (System 2 Review, Change 3B): the volatility
# regime is BTC-led and shared, mirroring the BTC-led market regime above.
_btc_atr = {"value": None}


def set_btc_atr(value) -> None:
    """Publish the latest BTC 5m ATR (GBP) for the shared volatility gate."""
    try:
        _btc_atr["value"] = None if value is None else float(value)
    except (TypeError, ValueError):
        _btc_atr["value"] = None


def get_btc_atr():
    """Latest BTC 5m ATR (GBP), or None if not yet published this run."""
    return _btc_atr["value"]


def _classify(btc_above_200ma: bool, fear_greed: float) -> str:
    """Bull only when BOTH conditions hold; otherwise Bear (Change 2 spec)."""
    if btc_above_200ma and fear_greed is not None and fear_greed >= FG_BULL_MIN:
        return "BULL"
    return "BEAR"


def _read_file() -> dict:
    """Parse the latest crypto block from Gaius market_context.json.
    Returns a regime dict; on any failure falls back to a safe BEAR regime
    (cautious: LONGs elevated bar, SHORTs still gated by Morgan)."""
    try:
        raw = json.loads(MARKET_CONTEXT_PATH.read_text(encoding="utf-8"))
        entries = raw if isinstance(raw, list) else [raw]
        latest = entries[-1]
        crypto = latest.get("crypto", {})
        above = bool(crypto.get("btc_above_200ma", False))
        fg = crypto.get("fear_greed_score")
        fg = float(fg) if fg is not None else None
        regime = _classify(above, fg)
        return {
            "regime":            regime,
            "btc_above_200ma":   above,
            "fear_greed_score":  fg,
            "fear_greed_label":  crypto.get("fear_greed_label", ""),
            "week_commencing":   latest.get("week_commencing", ""),
            "source":            "market_context.json",
        }
    except Exception as exc:
        log.warning("Regime read failed (%s) -- defaulting to BEAR (cautious)", exc)
        return {
            "regime":           "BEAR",
            "btc_above_200ma":  False,
            "fear_greed_score": None,
            "fear_greed_label": "UNKNOWN",
            "week_commencing":  "",
            "source":           "fallback",
        }


def get_regime(force: bool = False) -> dict:
    """Return the current regime dict, cached in-process for 30 minutes."""
    now = time.monotonic()
    if (not force) and _cache["data"] is not None and (now - _cache["at"] < CACHE_SECONDS):
        return _cache["data"]
    data = _read_file()
    _cache.update({"at": now, "data": data})
    log.info("Regime: %s | BTC above 200MA=%s | Fear&Greed=%s (%s) | wc=%s",
             data["regime"], data["btc_above_200ma"], data["fear_greed_score"],
             data["fear_greed_label"], data["week_commencing"])
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    print(json.dumps(get_regime(force=True), indent=2))
