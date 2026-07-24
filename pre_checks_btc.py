"""
CryptoHybrid AI -- pre_checks_btc.py
Hard filter rules that run BEFORE Claude is called.
If any check fails, we stay out immediately -- Claude never gets called.
This mirrors the TrendSurfer AI approach but adapted for BTC/GBP 24/7 trading.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd

log = logging.getLogger("CryptoHybrid.PreChecks")

# ──────────────────────────────────────────────────────────────────────────────
# Thresholds -- adjust these to make the system more or less strict
# ──────────────────────────────────────────────────────────────────────────────

MIN_TMO_FOR_ENTRY       = 0.21   # TMO entry threshold -- lowered ~30% for crypto
                                 # scalping (18 Jul 2026): momentum bursts are
                                 # shorter/sharper than the old 0.3 assumed.
# Volatility-range gate (System 2 Review, Change 3B, 18 Jul 2026). Replaces the
# old "choppy market" check. Uses the shared BTC 5m ATR (GBP): too flat = nothing
# to scalp; too extreme = flash crash/pump, too risky. BACKTEST-PROVISIONAL --
# Nick sign-off 18 Jul (thresholds 50/800); review after 2 weeks.
ATR_VOL_FLOOR_GBP       = 50.0   # below this BTC 5m ATR -> flat, block (nothing to scalp)
ATR_VOL_CEILING_GBP     = 800.0  # above this BTC 5m ATR -> extreme, block (too risky)
CHOPPY_RSI_THRESHOLD    = 5.0    # RSI near zero = choppy
CHOPPY_TMO_THRESHOLD    = 0.5    # TMO near zero = choppy
CHOPPY_CHANDE_THRESHOLD = 10.0   # Chande near zero = choppy
CHOPPY_SIGNALS_REQUIRED = 2      # how many choppy signals before we stay out
COOLDOWN_MINUTES        = 30     # minutes to wait after a losing trade
DAILY_LOSS_LIMIT_GBP    = 36.0   # kill switch -- max daily loss in GBP
MAX_CONSECUTIVE_LOSSES  = 6      # kill switch -- max losing trades in a row


# ──────────────────────────────────────────────────────────────────────────────
# Result builders -- standardised pass/fail responses
# ──────────────────────────────────────────────────────────────────────────────

def _pass() -> dict:
    """Pre-check passed -- continue to next check."""
    return {"passed": True, "reason": None}


def _fail(reason: str, block_direction: str = "BOTH") -> dict:
    """
    Pre-check failed -- stay out.
    block_direction: 'LONG', 'SHORT', or 'BOTH'
    """
    log.info("  PRE-CHECK FAILED: %s", reason)
    return {
        "passed":          False,
        "reason":          reason,
        "block_direction": block_direction,
        "decision":        "STAY_OUT",
    }


def _trigger_kill_switch(account: dict, reason: str) -> dict:
    """
    Record a kill switch event, determine tier from 48-hour history,
    set wait duration accordingly, and return the _fail result.

    Tier 1 (1st trigger in 48h): 6 hour wait
    Tier 2 (2nd trigger in 48h): 12 hour wait
    Tier 3 (3+ triggers in 48h): 24 hour wait
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)

    # Purge entries older than 48 hours
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
        "KILL SWITCH triggered (Tier %d) -- %s | auto-resume in %d hours",
        tier, reason, wait_hours,
    )
    return _fail(reason)


# ──────────────────────────────────────────────────────────────────────────────
# Individual pre-checks
# ──────────────────────────────────────────────────────────────────────────────

def check_kill_switch(account: dict) -> dict:
    """
    Block all trading if the kill switch has been triggered.
    Kill switch fires when:
    - Daily loss exceeds DAILY_LOSS_LIMIT_GBP, OR
    - Consecutive losses reach MAX_CONSECUTIVE_LOSSES
    """
    if account.get("killed", False):
        reason = account.get("kill_reason", "Kill switch active")
        return _fail(f"KILL SWITCH ACTIVE -- {reason}")
    return _pass()


def check_daily_loss_limit(account: dict) -> dict:
    """Trigger tiered kill switch if daily loss exceeds the limit."""
    daily_pnl = account.get("daily_pnl_gbp", 0.0)
    if daily_pnl <= -DAILY_LOSS_LIMIT_GBP:
        reason = f"Daily loss limit hit (GBP {daily_pnl:.2f} / limit GBP {DAILY_LOSS_LIMIT_GBP:.2f})"
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_consecutive_losses(account: dict) -> dict:
    """Trigger tiered kill switch after too many consecutive losses."""
    consecutive = account.get("consecutive_losses", 0)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{consecutive} consecutive losses reached"
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_kill_switch_reset(account: dict) -> bool:
    """
    Auto-reset kill switch once the tier wait duration has elapsed.
    Call this at the start of each candle tick from the main loop.
    Returns True if the kill switch was just reset, False otherwise.
    """
    if not account.get("killed", False):
        return False

    kill_time_str = account.get("kill_time")
    if not kill_time_str:
        return False

    wait_hours = account.get("kill_wait_hours", 6)
    tier       = account.get("kill_tier", 1)

    kill_time = datetime.fromisoformat(kill_time_str)
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

    msg = f"Kill switch reset (Tier {tier}) -- resuming after {wait_hours} hour cooldown"
    if tier >= 3:
        msg += ". Manual review recommended."
    log.info(msg)
    return True


def check_cooldown(account: dict) -> dict:
    """
    Block new entries for COOLDOWN_MINUTES after a losing trade.
    Stops revenge trading -- gives the market time to settle.
    """
    last_loss_time = account.get("last_loss_time")
    if not last_loss_time:
        return _pass()

    try:
        if isinstance(last_loss_time, str):
            last_loss = datetime.fromisoformat(last_loss_time)
        else:
            last_loss = last_loss_time

        now = datetime.now(timezone.utc)

        # Make sure both are timezone-aware
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
        log.warning("Cooldown check error: %s", e)

    return _pass()


def check_ssl_agreement(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """
    The 1-hour and 5-minute SSL clouds must agree on direction.
    If they conflict, the market is in transition -- no trade.
    This is one of TrendSurfer's most important rules.
    """
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
    """
    The 1-hour Chande Momentum must agree with the 1-hour SSL direction.
    SSL BULL but Chande negative = momentum conflict, no long entry.
    SSL BEAR but Chande positive = momentum conflict, no short entry.
    """
    ssl_bull  = bar_1h.get("ssl_bull")
    chande_1h = bar_1h.get("chande_mo")

    if pd.isna(ssl_bull) or pd.isna(chande_1h):
        return _pass()  # no data, let Claude decide

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
    """
    The 1-hour TMO must agree with the 1-hour SSL direction.
    Ensures hourly momentum is confirmed before we even look at 5m.
    """
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_1h   = bar_1h.get("tmo_main")

    if pd.isna(ssl_bull) or pd.isna(tmo_1h):
        return _pass()

    if ssl_bull and tmo_1h < 0:
        # Fix 3 (backtest-validated v1.5.5): a negative 1h TMO that is RISING
        # signals a trending recovery -- allow the long instead of hard-blocking.
        tmo_prev = bar_1h.get("tmo_prev")
        if tmo_prev is not None and not pd.isna(tmo_prev) and tmo_1h > tmo_prev:
            log.info(
                "  1h TMO negative (%.3f) but rising (prev %.3f) -- "
                "allowing long entry (trending recovery).",
                tmo_1h, tmo_prev,
            )
        else:
            return _fail(
                f"1h SSL is BULL but 1h TMO is negative and not rising ({tmo_1h:.3f}) -- "
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
    """
    The most recent 5m candle colour must match the trade direction.
    Green candle for LONG entry, red candle for SHORT entry.
    This ensures we enter WITH momentum, not against it.
    """
    ssl_bull = bar_1h.get("ssl_bull")

    if pd.isna(ssl_bull):
        return _pass()

    # Determine candle colour from open vs close
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
    """
    The 5-minute TMO must show meaningful momentum in the right direction.
    Weak TMO = weak momentum = likely to fail.
    """
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


def check_volatility_range(btc_atr) -> dict:
    """Volatility-range gate (Change 3B). btc_atr is the shared BTC 5m ATR in GBP.
    Blocks when volatility is too low (flat -- nothing to scalp) or too high
    (flash crash/pump -- too risky). None -> no data, allow (e.g. backtest, or the
    first ETH tick before the BTC engine has published an ATR)."""
    if btc_atr is None or pd.isna(btc_atr):
        return _pass()
    atr = float(btc_atr)
    if atr < ATR_VOL_FLOOR_GBP:
        return _fail(
            f"Volatility too low -- BTC 5m ATR GBP {atr:.1f} < floor GBP "
            f"{ATR_VOL_FLOOR_GBP:.0f}. Flat market, nothing to scalp.",
            block_direction="BOTH",
        )
    if atr > ATR_VOL_CEILING_GBP:
        return _fail(
            f"Volatility too high -- BTC 5m ATR GBP {atr:.1f} > ceiling GBP "
            f"{ATR_VOL_CEILING_GBP:.0f}. Extreme conditions (flash crash/pump), "
            f"too risky to scalp.",
            block_direction="BOTH",
        )
    return _pass()


def check_choppy_market(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """
    Detect choppy, directionless market conditions.
    If 2 or more indicators are near zero, the market has no clear direction.
    Best trade in a choppy market is no trade.
    """
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
    """
    RSI on both timeframes must agree with the SSL direction.
    1h RSI above 50 = bullish. Below 50 = bearish.
    5m RSI must also confirm direction before entry.
    """
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


# ──────────────────────────────────────────────────────────────────────────────
# Daily trend filter -- highest timeframe determines allowed direction
# ──────────────────────────────────────────────────────────────────────────────

def check_daily_trend_filter(
    bar_1d: Optional[pd.Series],
    bar_1h: pd.Series,
    current_trade=None,
) -> dict:
    """
    Daily SSL Cloud is the highest timeframe filter.
    Daily BULL + 1h BEAR → block (only LONG entries today, but 1h wants SHORT).
    Daily BEAR + 1h BULL → block (only SHORT entries today, but 1h wants LONG).
    When daily and hourly agree, or no daily data, pass through.
    """
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
    log.info("  Daily SSL %s agrees with 1h direction", daily_dir)
    return _pass()


# ──────────────────────────────────────────────────────────────────────────────
# Exit confirmation -- requires TWO signals before exiting a trade
# ──────────────────────────────────────────────────────────────────────────────

def check_exit_confirmation(
    proposed_exit: str,
    bar_5m: pd.Series,
    current_trade,
) -> dict:
    """
    Before exiting a trade, BOTH SSL AND RSI must confirm.
    This stops us getting shaken out by temporary counter-moves.

    proposed_exit: 'EXIT_LONG' or 'EXIT_SHORT'
    Returns _pass() if exit is confirmed, _fail() to hold the trade.
    """
    if current_trade is None:
        return _pass()

    if proposed_exit not in ("EXIT_LONG", "EXIT_SHORT"):
        return _pass()

    ssl_bull = bar_5m.get("ssl_bull")
    rsi_5m   = bar_5m.get("rsi")

    if pd.isna(ssl_bull) or pd.isna(rsi_5m):
        return _pass()  # no data -- allow exit for safety

    if proposed_exit == "EXIT_LONG":
        ssl_confirmed = not ssl_bull          # SSL must have turned BEAR
        rsi_confirmed = rsi_5m < 50          # RSI must be below 50

        if ssl_confirmed and rsi_confirmed:
            log.info("EXIT LONG confirmed -- SSL=BEAR and RSI=%.1f (below 50)", rsi_5m)
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
        ssl_confirmed = ssl_bull              # SSL must have turned BULL
        rsi_confirmed = rsi_5m > 50          # RSI must be above 50

        if ssl_confirmed and rsi_confirmed:
            log.info("EXIT SHORT confirmed -- SSL=BULL and RSI=%.1f (above 50)", rsi_5m)
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
# Master pre-check runner -- runs ALL checks in the right order
# ──────────────────────────────────────────────────────────────────────────────

def run_all_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account: dict,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    btc_atr=None,
) -> dict:
    """
    Run every pre-check in order.
    Returns the first failure found, or a pass if all checks clear.

    This is the gatekeeper -- Claude only gets called if this returns passed=True.

    Order matters -- safety checks first, then quality checks.
    """
    log.info("--- Running pre-checks ---")

    # Safety checks first -- these protect capital
    checks = [
        ("Kill switch",           lambda: check_kill_switch(account)),
        ("Daily loss limit",      lambda: check_daily_loss_limit(account)),
        ("Consecutive losses",    lambda: check_consecutive_losses(account)),
        ("Cooldown period",       lambda: check_cooldown(account)),
    ]

    # Quality checks -- only run when looking for new entries
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
            # Volatility range (Change 3B) replaces the old "Choppy market" check.
            ("Volatility range",      lambda: check_volatility_range(btc_atr)),
            ("RSI agreement",         lambda: check_rsi_agrees_with_ssl(bar_1h, bar_5m)),
        ]

    # Run each check
    for name, check_fn in checks:
        result = check_fn()
        if not result["passed"]:
            log.info("  [FAIL] %s -- %s", name, result["reason"])
            return result
        else:
            log.info("  [PASS] %s", name)

    log.info("  All pre-checks passed -- ready for Claude decision")
    return _pass()


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("CryptoHybrid AI -- Pre-checks self-test")

    # Healthy account
    account_ok = {
        "killed":              False,
        "daily_pnl_gbp":       -5.0,
        "consecutive_losses":  1,
        "last_loss_time":      None,
    }

    # Bullish bars -- all indicators agree
    bar_1h_bull = pd.Series({
        "ssl_bull":    True,
        "rsi":         62.0,
        "macd":        120.0,
        "tmo_main":    2.1,
        "tmo_smooth":  1.5,
        "chande_mo":   45.0,
        "money_flow":  8000.0,
        "open":        45000.0,
        "close":       45500.0,
    })

    bar_5m_bull = pd.Series({
        "ssl_bull":       True,
        "rsi":            58.0,
        "macd_histogram": 50.0,
        "tmo_main":       1.2,
        "tmo_smooth":     0.8,
        "chande_mo":      30.0,
        "money_flow":     5000.0,
        "open":           45400.0,
        "close":          45500.0,
    })

    log.info("\n--- Test 1: All bullish, should PASS all checks ---")
    result = run_all_pre_checks(bar_1h_bull, bar_5m_bull, account_ok)
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")

    log.info("\n--- Test 2: SSL conflict -- 1h BULL but 5m BEAR ---")
    bar_5m_bear = bar_5m_bull.copy()
    bar_5m_bear["ssl_bull"] = False
    bar_5m_bear["open"]     = 45600.0
    bar_5m_bear["close"]    = 45400.0
    result = run_all_pre_checks(bar_1h_bull, bar_5m_bear, account_ok)
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")

    log.info("\n--- Test 3: Kill switch active ---")
    account_killed = {**account_ok, "killed": True, "kill_reason": "Daily loss limit hit"}
    result = run_all_pre_checks(bar_1h_bull, bar_5m_bull, account_killed)
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")

    log.info("\n--- Test 4: Choppy market ---")
    bar_1h_choppy = bar_1h_bull.copy()
    bar_5m_choppy = bar_5m_bull.copy()
    bar_1h_choppy["rsi"]    = 51.0   # near 50
    bar_5m_choppy["rsi"]    = 50.5   # near 50
    bar_5m_choppy["tmo_main"] = 0.1  # near zero
    result = run_all_pre_checks(bar_1h_choppy, bar_5m_choppy, account_ok)
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")

    log.info("\nPre-checks self-test complete.")