"""
CryptoHybrid AI -- pre_checks_eth.py
Hard filter rules for ETH/GBP trading -- run BEFORE Claude is called.
Identical to pre_checks_btc.py with one addition:
  ETH direction follows the DAILY SSL trend dynamically --
  daily BULL allows LONG, daily BEAR is SHORT-only, NEUTRAL allows both
  (via check_eth_short_only_mode / ETH_SHORT_ONLY_MODE gate).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd

log = logging.getLogger("CryptoHybrid.PreChecks.ETH")

# ──────────────────────────────────────────────────────────────────────────────
# Thresholds
# ──────────────────────────────────────────────────────────────────────────────

MIN_TMO_FOR_ENTRY       = 0.21   # lowered ~30% for crypto scalping (18 Jul 2026)
# Volatility-range gate (System 2 Review, Change 3B). Uses the SHARED BTC 5m ATR
# (GBP) for both engines -- crypto volatility is BTC-led, mirroring the BTC-led
# regime. BACKTEST-PROVISIONAL (thresholds 50/800); review after 2 weeks.
# COMMISSION 016 (27 Jul 2026, Nick-approved): the floor/ceiling are now a PERCENTAGE
# of the current BTC price so they auto-scale as BTC re-prices. Per bar:
#     atr_floor_gbp   = btc_price * ETH_VOLATILITY_FLOOR_PCT / 100
#     atr_ceiling_gbp = btc_price * VOLATILITY_CEILING_PCT   / 100
# ETH uses a slightly higher floor % than BTC (its ATR%-of-price distribution sits
# higher). NOTE: the "ETH blocked 100%" figure in Commission 015/016 was a HARNESS
# artifact (the backtest applied the floor to ETH's OWN ATR); in production this gate
# reads the SHARED BTC 5m ATR for BOTH engines, so ETH was never blocked in the live
# system. Floor/ceiling both scale off the BTC price accordingly.
ETH_VOLATILITY_FLOOR_PCT = 0.125  # BTC 5m ATR floor as % of BTC price for ETH (was fixed £50)
VOLATILITY_CEILING_PCT   = 1.65   # BTC 5m ATR ceiling as % of BTC price (was fixed £800)
# Legacy fixed GBP thresholds -- fallback ONLY when the BTC price is unavailable.
ATR_VOL_FLOOR_GBP       = 50.0
ATR_VOL_CEILING_GBP     = 800.0
CHOPPY_RSI_THRESHOLD    = 5.0
CHOPPY_TMO_THRESHOLD    = 0.5
CHOPPY_CHANDE_THRESHOLD = 10.0
CHOPPY_SIGNALS_REQUIRED = 2
COOLDOWN_MINUTES        = 30
DAILY_LOSS_LIMIT_GBP    = 36.0
MAX_CONSECUTIVE_LOSSES  = 6

# ETH direction gating. When True, ETH entries follow the DAILY SSL trend:
#   daily BULL -> LONG allowed, daily BEAR -> SHORT only, NEUTRAL -> both.
# Set to False to disable gating entirely (both directions always allowed).
# DISABLED 18 Jul 2026 (System 2 Review, Change 2/3C): direction is now driven by
# the 1h SSL (primary) plus the regime + Morgan-SHORT gate in main_tidetrader; the
# daily-SSL short-only mode is too slow for scalping and is superseded.
ETH_SHORT_ONLY_MODE = False


# ──────────────────────────────────────────────────────────────────────────────
# Result builders
# ──────────────────────────────────────────────────────────────────────────────

def _pass() -> dict:
    return {"passed": True, "reason": None}


def _fail(reason: str, block_direction: str = "BOTH") -> dict:
    log.info("  [ETH] PRE-CHECK FAILED: %s", reason)
    return {
        "passed":          False,
        "reason":          reason,
        "block_direction": block_direction,
        "decision":        "STAY_OUT",
    }


def _trigger_kill_switch(account: dict, reason: str) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)

    history = account.get("kill_history", [])
    history = [
        t for t in history
        if datetime.fromisoformat(t).replace(tzinfo=timezone.utc) > cutoff
    ]
    history.append(now.isoformat())

    count = len(history)
    if count == 1:
        tier, wait_hours = 1, 6
    elif count == 2:
        tier, wait_hours = 2, 12
    else:
        tier, wait_hours = 3, 24

    account["kill_history"]    = history
    account["killed"]          = True
    account["kill_reason"]     = reason
    account["kill_time"]       = now.isoformat()
    account["kill_wait_hours"] = wait_hours
    account["kill_tier"]       = tier

    log.warning(
        "[ETH] KILL SWITCH triggered (Tier %d) -- %s | auto-resume in %d hours",
        tier, reason, wait_hours,
    )
    return _fail(reason)


# ──────────────────────────────────────────────────────────────────────────────
# ETH-specific check
# ──────────────────────────────────────────────────────────────────────────────

def check_eth_short_only_mode(bar_1d: pd.Series, bar_1h: pd.Series = None) -> dict:
    """
    Dynamic ETH direction mode, driven by the DAILY SSL trend:
      Daily SSL BULL    -> LONG entries allowed
      Daily SSL BEAR    -> SHORT entries only (LONG blocked)
      Daily SSL NEUTRAL -> both directions allowed

    Replaces the old static ETH_SHORT_ONLY_MODE=True hard block, so ETH now
    follows the prevailing daily trend automatically -- it allows LONGs while
    the daily trend is bullish and reverts to SHORT-only when it turns bearish.
    Set ETH_SHORT_ONLY_MODE = False to disable this gating entirely.
    """
    if not ETH_SHORT_ONLY_MODE:
        return _pass()
    ssl_daily = bar_1d.get("ssl_bull") if bar_1d is not None else None
    if ssl_daily is None or pd.isna(ssl_daily):
        return _pass()                      # NEUTRAL / unknown -> both allowed
    if bool(ssl_daily):
        return _pass()                      # daily BULL -> LONG allowed
    # daily BEAR -> SHORT only. Block a LONG setup (1h SSL bullish).
    ssl_1h = bar_1h.get("ssl_bull") if bar_1h is not None else None
    if ssl_1h is not None and pd.notna(ssl_1h) and bool(ssl_1h):
        return _fail(
            "ETH daily SSL is BEAR -- SHORT only today. LONG entry blocked "
            "(dynamic mode follows the daily trend).",
            block_direction="LONG",
        )
    return _pass()


# ──────────────────────────────────────────────────────────────────────────────
# Individual pre-checks (identical to BTC)
# ──────────────────────────────────────────────────────────────────────────────

def check_kill_switch(account: dict) -> dict:
    if account.get("killed", False):
        reason = account.get("kill_reason", "Kill switch active")
        return _fail(f"KILL SWITCH ACTIVE -- {reason}")
    return _pass()


def check_daily_loss_limit(account: dict) -> dict:
    daily_pnl = account.get("daily_pnl_gbp", 0.0)
    if daily_pnl <= -DAILY_LOSS_LIMIT_GBP:
        reason = f"Daily loss limit hit (GBP {daily_pnl:.2f} / limit GBP {DAILY_LOSS_LIMIT_GBP:.2f})"
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_consecutive_losses(account: dict) -> dict:
    consecutive = account.get("consecutive_losses", 0)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{consecutive} consecutive losses reached"
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_kill_switch_reset(account: dict) -> bool:
    if not account.get("killed", False):
        return False
    kill_time_str = account.get("kill_time")
    if not kill_time_str:
        return False
    wait_hours = account.get("kill_wait_hours", 6)
    tier       = account.get("kill_tier", 1)
    kill_time  = datetime.fromisoformat(kill_time_str)
    if kill_time.tzinfo is None:
        kill_time = kill_time.replace(tzinfo=timezone.utc)
    elapsed_hours = (datetime.now(timezone.utc) - kill_time).total_seconds() / 3600
    if elapsed_hours < wait_hours:
        return False
    account["killed"]             = False
    account["kill_reason"]        = ""
    account["kill_time"]          = None
    account["consecutive_losses"] = 0
    account["kill_last_log"]      = None
    msg = f"[ETH] Kill switch reset (Tier {tier}) -- resuming after {wait_hours} hour cooldown"
    if tier >= 3:
        msg += ". Manual review recommended."
    log.info(msg)
    return True


def check_cooldown(account: dict) -> dict:
    last_loss_time = account.get("last_loss_time")
    if not last_loss_time:
        return _pass()
    try:
        if isinstance(last_loss_time, str):
            last_loss = datetime.fromisoformat(last_loss_time)
        else:
            last_loss = last_loss_time
        now = datetime.now(timezone.utc)
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=timezone.utc)
        minutes_since = (now - last_loss).total_seconds() / 60
        if minutes_since < COOLDOWN_MINUTES:
            remaining = int(COOLDOWN_MINUTES - minutes_since)
            return _fail(
                f"Cooldown active -- {remaining} minutes remaining after last loss. "
                f"Prevents revenge trading."
            )
    except Exception as e:
        log.warning("[ETH] Cooldown check error: %s", e)
    return _pass()


def check_ssl_agreement(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    ssl_1h = bar_1h.get("ssl_bull")
    ssl_5m = bar_5m.get("ssl_bull")
    if pd.isna(ssl_1h) or pd.isna(ssl_5m):
        return _fail("SSL Cloud data not available -- cannot confirm direction")
    if ssl_1h != ssl_5m:
        direction_1h = "BULL" if ssl_1h else "BEAR"
        direction_5m = "BULL" if ssl_5m else "BEAR"
        return _fail(
            f"SSL Cloud conflict -- 1h is {direction_1h} but 5m is {direction_5m}. "
            f"Market is in transition, no clear trend.",
            block_direction="BOTH"
        )
    return _pass()


def check_1h_chande_agrees_with_ssl(bar_1h: pd.Series) -> dict:
    ssl_bull  = bar_1h.get("ssl_bull")
    chande_1h = bar_1h.get("chande_mo")
    if pd.isna(ssl_bull) or pd.isna(chande_1h):
        return _pass()
    if ssl_bull and chande_1h < 0:
        return _fail(
            f"1h SSL is BULL but 1h Chande is negative ({chande_1h:.1f}) -- "
            f"momentum conflict, no long entry.",
            block_direction="LONG"
        )
    if not ssl_bull and chande_1h > 0:
        return _fail(
            f"1h SSL is BEAR but 1h Chande is positive ({chande_1h:.1f}) -- "
            f"momentum conflict, no short entry.",
            block_direction="SHORT"
        )
    return _pass()


def check_1h_tmo_agrees_with_ssl(bar_1h: pd.Series) -> dict:
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_1h   = bar_1h.get("tmo_main")
    if pd.isna(ssl_bull) or pd.isna(tmo_1h):
        return _pass()
    if ssl_bull and tmo_1h < 0:
        return _fail(
            f"1h SSL is BULL but 1h TMO is negative ({tmo_1h:.3f}) -- "
            f"hourly momentum not confirmed, no long entry.",
            block_direction="LONG"
        )
    if not ssl_bull and tmo_1h > 0:
        return _fail(
            f"1h SSL is BEAR but 1h TMO is positive ({tmo_1h:.3f}) -- "
            f"hourly momentum not confirmed, no short entry.",
            block_direction="SHORT"
        )
    return _pass()


def check_candle_colour(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    ssl_bull = bar_1h.get("ssl_bull")
    if pd.isna(ssl_bull):
        return _pass()
    open_price  = bar_5m.get("open")
    close_price = bar_5m.get("close")
    if pd.isna(open_price) or pd.isna(close_price):
        return _pass()
    candle_green = close_price >= open_price
    if ssl_bull and not candle_green:
        return _fail(
            "1h bias is BULL but 5m candle is RED -- "
            "waiting for a green candle to confirm upward momentum.",
            block_direction="LONG"
        )
    if not ssl_bull and candle_green:
        return _fail(
            "1h bias is BEAR but 5m candle is GREEN -- "
            "waiting for a red candle to confirm downward momentum.",
            block_direction="SHORT"
        )
    return _pass()


def check_5m_tmo_momentum(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_5m   = bar_5m.get("tmo_main")
    if pd.isna(ssl_bull) or pd.isna(tmo_5m):
        return _pass()
    if ssl_bull and tmo_5m < MIN_TMO_FOR_ENTRY:
        return _fail(
            f"Bullish setup but 5m TMO is only {tmo_5m:.3f} -- "
            f"need at least {MIN_TMO_FOR_ENTRY} for meaningful momentum.",
            block_direction="LONG"
        )
    if not ssl_bull and tmo_5m > -MIN_TMO_FOR_ENTRY:
        return _fail(
            f"Bearish setup but 5m TMO is only {tmo_5m:.3f} -- "
            f"need at least -{MIN_TMO_FOR_ENTRY} for meaningful downward momentum.",
            block_direction="SHORT"
        )
    return _pass()


def check_volatility_range(btc_atr, btc_price=None) -> dict:
    """Volatility-range gate (Change 3B; Commission 016 auto-scaling, 27 Jul 2026).
    btc_atr is the SHARED BTC 5m ATR in GBP (crypto volatility is BTC-led); btc_price
    is the current BTC price (GBP). The floor/ceiling are a % of btc_price so they
    auto-scale with price; when btc_price is unavailable (backtest / first tick) we
    fall back to the legacy fixed GBP thresholds. Blocks when too flat (nothing to
    scalp) or too extreme (flash crash/pump). None ATR -> allow (no data yet)."""
    if btc_atr is None or pd.isna(btc_atr):
        return _pass()
    atr = float(btc_atr)
    if btc_price is not None and not pd.isna(btc_price) and float(btc_price) > 0:
        floor   = float(btc_price) * ETH_VOLATILITY_FLOOR_PCT / 100.0
        ceiling = float(btc_price) * VOLATILITY_CEILING_PCT   / 100.0
    else:
        floor, ceiling = ATR_VOL_FLOOR_GBP, ATR_VOL_CEILING_GBP
    if atr < floor:
        return _fail(
            f"Volatility too low -- BTC 5m ATR GBP {atr:.1f} < floor GBP "
            f"{floor:.1f}. Flat market, nothing to scalp.",
            block_direction="BOTH",
        )
    if atr > ceiling:
        return _fail(
            f"Volatility too high -- BTC 5m ATR GBP {atr:.1f} > ceiling GBP "
            f"{ceiling:.1f}. Extreme conditions, too risky to scalp.",
            block_direction="BOTH",
        )
    return _pass()


def check_choppy_market(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    choppy_signals = []
    rsi_5m = bar_5m.get("rsi")
    if pd.notna(rsi_5m) and abs(rsi_5m - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy_signals.append(f"5m RSI near 50 ({rsi_5m:.1f})")
    tmo_5m = bar_5m.get("tmo_main")
    if pd.notna(tmo_5m) and abs(tmo_5m) <= CHOPPY_TMO_THRESHOLD:
        choppy_signals.append(f"5m TMO near zero ({tmo_5m:.3f})")
    chande_5m = bar_5m.get("chande_mo")
    if pd.notna(chande_5m) and abs(chande_5m) <= CHOPPY_CHANDE_THRESHOLD:
        choppy_signals.append(f"5m Chande near zero ({chande_5m:.1f})")
    rsi_1h = bar_1h.get("rsi")
    if pd.notna(rsi_1h) and abs(rsi_1h - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy_signals.append(f"1h RSI near 50 ({rsi_1h:.1f})")
    if len(choppy_signals) >= CHOPPY_SIGNALS_REQUIRED:
        return _fail(
            f"Choppy market detected -- {len(choppy_signals)} signals near zero: "
            f"{', '.join(choppy_signals)}. Best trade is no trade.",
            block_direction="BOTH"
        )
    return _pass()


def check_rsi_agrees_with_ssl(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    ssl_bull = bar_1h.get("ssl_bull")
    rsi_1h   = bar_1h.get("rsi")
    rsi_5m   = bar_5m.get("rsi")
    if pd.isna(ssl_bull):
        return _pass()
    if pd.notna(rsi_1h):
        if ssl_bull and rsi_1h < 50:
            return _fail(
                f"1h SSL is BULL but 1h RSI is {rsi_1h:.1f} (below 50) -- "
                f"conflicting momentum signals.",
                block_direction="LONG"
            )
        if not ssl_bull and rsi_1h > 50:
            return _fail(
                f"1h SSL is BEAR but 1h RSI is {rsi_1h:.1f} (above 50) -- "
                f"conflicting momentum signals.",
                block_direction="SHORT"
            )
    if pd.notna(rsi_5m):
        if ssl_bull and rsi_5m < 45:
            return _fail(
                f"1h bias is LONG but 5m RSI is {rsi_5m:.1f} -- "
                f"5m momentum not confirming.",
                block_direction="LONG"
            )
        if not ssl_bull and rsi_5m > 55:
            return _fail(
                f"1h bias is SHORT but 5m RSI is {rsi_5m:.1f} -- "
                f"5m momentum not confirming.",
                block_direction="SHORT"
            )
    return _pass()


def check_daily_trend_filter(
    bar_1d: Optional[pd.Series],
    bar_1h: pd.Series,
    current_trade=None,
) -> dict:
    if current_trade is not None:
        return _pass()
    if bar_1d is None:
        return _pass()
    ssl_1d = bar_1d.get("ssl_bull")
    ssl_1h = bar_1h.get("ssl_bull")
    if pd.isna(ssl_1d) or pd.isna(ssl_1h):
        return _pass()
    if ssl_1d and not ssl_1h:
        return _fail(
            f"Daily SSL is BULL (only LONG entries today) but 1h SSL is BEAR -- "
            f"daily and hourly trends conflict. Waiting for alignment.",
            block_direction="BOTH"
        )
    if not ssl_1d and ssl_1h:
        return _fail(
            f"Daily SSL is BEAR (only SHORT entries today) but 1h SSL is BULL -- "
            f"daily and hourly trends conflict. Waiting for alignment.",
            block_direction="BOTH"
        )
    daily_dir = "BULL" if ssl_1d else "BEAR"
    log.info("  [ETH] Daily SSL %s agrees with 1h direction", daily_dir)
    return _pass()


def check_exit_confirmation(
    proposed_exit: str,
    bar_5m: pd.Series,
    current_trade,
) -> dict:
    if current_trade is None:
        return _pass()
    if proposed_exit not in ("EXIT_LONG", "EXIT_SHORT"):
        return _pass()
    ssl_bull = bar_5m.get("ssl_bull")
    rsi_5m   = bar_5m.get("rsi")
    if pd.isna(ssl_bull) or pd.isna(rsi_5m):
        return _pass()
    if proposed_exit == "EXIT_LONG":
        ssl_confirmed = not ssl_bull
        rsi_confirmed = rsi_5m < 50
        if ssl_confirmed and rsi_confirmed:
            log.info("[ETH] EXIT LONG confirmed -- SSL=BEAR and RSI=%.1f (below 50)", rsi_5m)
            return _pass()
        if ssl_confirmed and not rsi_confirmed:
            return _fail(
                f"SSL turned BEAR but RSI still {rsi_5m:.1f} (above 50) -- "
                f"holding long, waiting for RSI confirmation.",
                block_direction="EXIT"
            )
        if not ssl_confirmed and rsi_confirmed:
            return _fail(
                f"RSI dropped to {rsi_5m:.1f} but SSL still BULL -- "
                f"holding long, waiting for SSL to turn.",
                block_direction="EXIT"
            )
        return _fail(
            "Neither SSL nor RSI confirming exit -- holding long position.",
            block_direction="EXIT"
        )
    if proposed_exit == "EXIT_SHORT":
        ssl_confirmed = ssl_bull
        rsi_confirmed = rsi_5m > 50
        if ssl_confirmed and rsi_confirmed:
            log.info("[ETH] EXIT SHORT confirmed -- SSL=BULL and RSI=%.1f (above 50)", rsi_5m)
            return _pass()
        if ssl_confirmed and not rsi_confirmed:
            return _fail(
                f"SSL turned BULL but RSI still {rsi_5m:.1f} (below 50) -- "
                f"holding short, waiting for RSI confirmation.",
                block_direction="EXIT"
            )
        if not ssl_confirmed and rsi_confirmed:
            return _fail(
                f"RSI rose to {rsi_5m:.1f} but SSL still BEAR -- "
                f"holding short, waiting for SSL to turn.",
                block_direction="EXIT"
            )
        return _fail(
            "Neither SSL nor RSI confirming exit -- holding short position.",
            block_direction="EXIT"
        )
    return _pass()


# ──────────────────────────────────────────────────────────────────────────────
# Master pre-check runner
# ──────────────────────────────────────────────────────────────────────────────

def run_all_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account: dict,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    btc_atr=None,
    btc_price=None,
) -> dict:
    """
    Run every ETH pre-check in order.
    Direction is driven by the 1h SSL + regime/Morgan-SHORT gating in main; the
    daily-SSL direction gates were removed (Change 2/3C, 18 Jul 2026).
    """
    log.info("--- [ETH] Running pre-checks ---")

    checks = [
        ("Kill switch",           lambda: check_kill_switch(account)),
        ("Daily loss limit",      lambda: check_daily_loss_limit(account)),
        ("Consecutive losses",    lambda: check_consecutive_losses(account)),
        ("Cooldown period",       lambda: check_cooldown(account)),
    ]

    if current_trade is None:
        checks += [
            # CRYPTOHYBRID CHANGE (v1.0.0, 24 Jul 2026): entry now requires only
            # Daily + 1h SSL agreement -- the 5m SSL agreement requirement is
            # DROPPED so more candidates reach Arthur (who still gates
            # ENTER / STAY_OUT, so more signals != more trades). check_ssl_agreement
            # (1h vs 5m) is retained as a helper/indicator but no longer gates
            # entries; the 5m SSL cloud is still visible to Arthur via the bar
            # data. Daily+1h agreement is enforced by check_daily_trend_filter
            # (passes through when daily data is unavailable).
            ("Daily+1h SSL agreement", lambda: check_daily_trend_filter(bar_1d, bar_1h)),
            ("1h Chande vs SSL",      lambda: check_1h_chande_agrees_with_ssl(bar_1h)),
            ("1h TMO vs SSL",         lambda: check_1h_tmo_agrees_with_ssl(bar_1h)),
            ("Candle colour",         lambda: check_candle_colour(bar_1h, bar_5m)),
            ("5m TMO momentum",       lambda: check_5m_tmo_momentum(bar_1h, bar_5m)),
            ("Volatility range",      lambda: check_volatility_range(btc_atr, btc_price)),
            ("RSI agreement",         lambda: check_rsi_agrees_with_ssl(bar_1h, bar_5m)),
        ]

    for name, check_fn in checks:
        result = check_fn()
        if not result["passed"]:
            log.info("  [ETH] [FAIL] %s -- %s", name, result["reason"])
            return result
        else:
            log.info("  [ETH] [PASS] %s", name)

    log.info("  [ETH] All pre-checks passed -- ready for Claude decision")
    return _pass()
