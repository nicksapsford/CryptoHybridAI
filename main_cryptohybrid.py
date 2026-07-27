"""
CryptoHybrid AI -- main_cryptohybrid.py
Complete intelligent trading loop with:
  - Pre-checks (11 hard filters)
  - Claude AI decision making
  - Whale data analysis
  - 5-minute candle checks when watching
  - 30-second price monitoring when in a trade
  - Kill switch and safety limits
  - Dashboard state pushing
Press Ctrl+C to stop safely.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import krakenex
import requests

from data_feed_btc import BTCDataFeed, fetch_latest_1m_bar
from data_feed_eth import ETHDataFeed, fetch_latest_1m_bar as eth_fetch_latest_1m_bar
from paper_trader_btc import PaperTrader, TRADES_LOG
from paper_trader_eth import PaperTraderETH, ETH_TRADES_LOG
import performance_btc
import phantom_tracker
from pre_checks_btc import (
    run_all_pre_checks,
    check_kill_switch,
    check_kill_switch_reset,
    check_daily_loss_limit,
    check_consecutive_losses,
    check_cooldown,
    check_daily_trend_filter,
    check_ssl_agreement,
    check_1h_chande_agrees_with_ssl,
    check_1h_tmo_agrees_with_ssl,
    check_candle_colour,
    check_5m_tmo_momentum,
    check_choppy_market,
    check_volatility_range,
    check_rsi_agrees_with_ssl,
)
from pre_checks_eth import (
    run_all_pre_checks as eth_run_all_pre_checks,
    check_kill_switch as eth_check_kill_switch,
    check_kill_switch_reset as eth_check_kill_switch_reset,
    check_daily_loss_limit as eth_check_daily_loss_limit,
    check_consecutive_losses as eth_check_consecutive_losses,
    check_cooldown as eth_check_cooldown,
    check_daily_trend_filter as eth_check_daily_trend_filter,
    check_ssl_agreement as eth_check_ssl_agreement,
    check_1h_chande_agrees_with_ssl as eth_check_1h_chande_agrees_with_ssl,
    check_1h_tmo_agrees_with_ssl as eth_check_1h_tmo_agrees_with_ssl,
    check_candle_colour as eth_check_candle_colour,
    check_5m_tmo_momentum as eth_check_5m_tmo_momentum,
    check_choppy_market as eth_check_choppy_market,
    check_volatility_range as eth_check_volatility_range,
    check_rsi_agrees_with_ssl as eth_check_rsi_agrees_with_ssl,
    check_eth_short_only_mode,
)
from agent_brain_btc import get_trading_decision, format_decision_for_display
from agent_brain_eth import get_trading_decision as eth_get_trading_decision
from agent_brain_eth import format_decision_for_display as eth_format_decision_for_display
from whale_watcher_btc import get_whale_data
import economic_calendar_btc as economic_calendar
import notifier_btc as notifier
import regime as regime_mod   # market-regime reader (System 2 Review, 18 Jul 2026)

BASE_DIR      = Path(__file__).resolve().parent
LOG_FILE      = BASE_DIR / "logs" / "cryptohybrid.log"
SHUTDOWN_FLAG = LOG_FILE.parent / "shutdown.flag"
LIFT_FLAG     = LOG_FILE.parent / "confidence_lift.json"   # manual Morgan confidence lift (live)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── ALBION STANDING RULE: ALL LOG TIMESTAMPS ARE UTC ────────────────────────
# Force Python's logging to emit %(asctime)s in UTC, not BST/local. Without this
# line, logging defaults to local time and every log line is +1h vs the UTC CSV
# artefacts (phantom_trades.csv etc.) — the exact BST/UTC mismatch that caused a
# misread on 11 Jul 2026. Never interpret an Albion log timestamp as local time;
# confirm UTC before analysing. (Baked in per Nick's directive, 12 Jul 2026.)
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    force=True,   # overrides any basicConfig already called by imported modules
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
log = logging.getLogger("CryptoHybrid.Main")

# Main decision-loop cadence. 1-minute polling since 24 Jul 2026 (was 300s / 5min)
# to cut the 10-15min entry lag on fast BTC moves. This is POLLING cadence ONLY --
# SSL/indicator timeframes are unchanged (daily/1h/5m candles are still requested
# from the Kraken feed via INTERVALS in data_feed_*.py). The 5m candle-colour
# confirmation ("Candle Confirmed") remains a 5m signal-quality gate by design.
CANDLE_SECONDS       = 60
POSITION_CHECK_SECS  = 30
CANDLE_CLOSE_BUFFER  = 3
STARTUP_WAIT         = 5
DASHBOARD_URL        = "http://localhost:5041/api/update"


def push_to_dashboard(state: dict) -> None:
    try:
        requests.post(DASHBOARD_URL, json=state, timeout=2)
    except Exception:
        pass


def run_individual_pre_checks(bar_1h, bar_5m, account, current_trade, bar_1d=None) -> dict:
    checks = {}
    checks["Kill Switch OK"]        = check_kill_switch(account)["passed"]
    checks["Daily Loss OK"]         = check_daily_loss_limit(account)["passed"]
    checks["Consecutive Losses OK"] = check_consecutive_losses(account)["passed"]
    checks["Cooldown OK"]           = check_cooldown(account)["passed"]
    if current_trade is None:
        checks["Daily Trend OK"]   = check_daily_trend_filter(bar_1d, bar_1h)["passed"]
        checks["SSL Aligned"]      = check_ssl_agreement(bar_1h, bar_5m)["passed"]
        checks["1h Chande OK"]     = check_1h_chande_agrees_with_ssl(bar_1h)["passed"]
        checks["1h TMO OK"]        = check_1h_tmo_agrees_with_ssl(bar_1h)["passed"]
        checks["Candle Confirmed"] = check_candle_colour(bar_1h, bar_5m)["passed"]
        checks["Momentum Strong"]  = check_5m_tmo_momentum(bar_1h, bar_5m)["passed"]
        checks["Volatility OK"]    = check_volatility_range(regime_mod.get_btc_atr(), regime_mod.get_btc_price())["passed"]
        checks["RSI Confirming"]   = check_rsi_agrees_with_ssl(bar_1h, bar_5m)["passed"]
    else:
        checks["Daily Trend OK"]   = None
        checks["SSL Aligned"]      = None
        checks["1h Chande OK"]     = None
        checks["1h TMO OK"]        = None
        checks["Candle Confirmed"] = None
        checks["Momentum Strong"]  = None
        checks["Volatility OK"]    = None
        checks["RSI Confirming"]   = None
    return checks


def build_pre_check_state(pre_checks, block_reason, whale_data, trader, account, perf_dict=None):
    current_trade = trader.strategy.current_trade
    return {
        "panel_mode":    "pre_checks",
        "decision":      "STAY_OUT",
        "confidence":    "--",
        "one_hour_bias": "--",
        "reasoning":     f"Pre-check blocked: {block_reason}",
        "warnings":      [block_reason] if block_reason else [],
        "tokens_used":   "--",
        "pre_checks":    pre_checks,
        "block_reason":  block_reason,
        "checklist":     {},
        "whale":         whale_data,
        "position":      _build_position(current_trade),
        "performance":   perf_dict or {},
    }


def build_claude_state(decision, pre_checks, whale_data, trader, account, perf_dict=None):
    current_trade = trader.strategy.current_trade
    return {
        "panel_mode":    "claude",
        "decision":      decision.get("decision", "STAY_OUT"),
        "confidence":    decision.get("confidence", "--"),
        "one_hour_bias": decision.get("one_hour_bias", "--"),
        "reasoning":     decision.get("reasoning", ""),
        "warnings":      decision.get("warnings", []),
        "tokens_used":   decision.get("tokens_used", "--"),
        "pre_checks":    pre_checks,
        "block_reason":  "",
        "checklist":     decision.get("checklist", {}),
        "whale":         whale_data,
        "position":      _build_position(current_trade),
        "performance":   perf_dict or {},
    }


def run_individual_pre_checks_eth(bar_1h, bar_5m, account, current_trade, bar_1d=None) -> dict:
    checks = {}
    checks["Kill Switch OK"]        = eth_check_kill_switch(account)["passed"]
    checks["Daily Loss OK"]         = eth_check_daily_loss_limit(account)["passed"]
    checks["Consecutive Losses OK"] = eth_check_consecutive_losses(account)["passed"]
    checks["Cooldown OK"]           = eth_check_cooldown(account)["passed"]
    if current_trade is None:
        checks["Short Only Mode"]  = check_eth_short_only_mode(bar_1d, bar_1h)["passed"]
        checks["Daily Trend OK"]   = eth_check_daily_trend_filter(bar_1d, bar_1h)["passed"]
        checks["SSL Aligned"]      = eth_check_ssl_agreement(bar_1h, bar_5m)["passed"]
        checks["1h Chande OK"]     = eth_check_1h_chande_agrees_with_ssl(bar_1h)["passed"]
        checks["1h TMO OK"]        = eth_check_1h_tmo_agrees_with_ssl(bar_1h)["passed"]
        checks["Candle Confirmed"] = eth_check_candle_colour(bar_1h, bar_5m)["passed"]
        checks["Momentum Strong"]  = eth_check_5m_tmo_momentum(bar_1h, bar_5m)["passed"]
        checks["Volatility OK"]    = eth_check_volatility_range(regime_mod.get_btc_atr(), regime_mod.get_btc_price())["passed"]
        checks["RSI Confirming"]   = eth_check_rsi_agrees_with_ssl(bar_1h, bar_5m)["passed"]
    else:
        checks["Short Only Mode"]  = None
        checks["Daily Trend OK"]   = None
        checks["SSL Aligned"]      = None
        checks["1h Chande OK"]     = None
        checks["1h TMO OK"]        = None
        checks["Candle Confirmed"] = None
        checks["Momentum Strong"]  = None
        checks["Volatility OK"]    = None
        checks["RSI Confirming"]   = None
    return checks


def _safe_ind(v) -> float | None:
    return None if pd.isna(v) else float(v)


def _bar_to_ind1h(bar_1h) -> dict:
    return {
        "ssl_bull":   bool(bar_1h.get("ssl_bull", False)),
        "rsi":        _safe_ind(bar_1h.get("rsi")),
        "macd":       _safe_ind(bar_1h.get("macd")),
        "tmo_main":   _safe_ind(bar_1h.get("tmo_main")),
        "chande_mo":  _safe_ind(bar_1h.get("chande_mo")),
        "money_flow": _safe_ind(bar_1h.get("money_flow")),
    }


def _bar_to_ind5m(bar_5m) -> dict:
    return {
        "ssl_bull":   bool(bar_5m.get("ssl_bull", False)),
        "rsi":        _safe_ind(bar_5m.get("rsi")),
        "macd":       _safe_ind(bar_5m.get("macd")),
        "tmo_main":   _safe_ind(bar_5m.get("tmo_main")),
        "chande_mo":  _safe_ind(bar_5m.get("chande_mo")),
        "money_flow": _safe_ind(bar_5m.get("money_flow")),
    }


def _bar_to_ind(bar) -> dict:
    """Plain-dict indicator snapshot for the phantom tracker (Job 1, 19 Jul 2026).
    latest_bar() returns a pandas Series; phantom_tracker.build_snapshot() reads via
    isinstance(dict) and ind.get(), so a Series silently yields blank columns (and
    _ssl() raises on a Series' ambiguous truth value). Converting to a dict here --
    same pattern as GoldTrader/OilTrader _indicator_snapshot -- lets every indicator
    column populate. Guards a None daily bar (bar_1d can be absent)."""
    if bar is None:
        return {}
    return {
        "ssl_bull":   bool(bar.get("ssl_bull", False)),
        "rsi":        _safe_ind(bar.get("rsi")),
        "macd":       _safe_ind(bar.get("macd")),
        "tmo_main":   _safe_ind(bar.get("tmo_main")),
        "chande_mo":  _safe_ind(bar.get("chande_mo")),
        "money_flow": _safe_ind(bar.get("money_flow")),
    }


def build_eth_pre_check_state(pre_checks, block_reason, whale_data, eth_trader, account,
                               bar_1h, bar_5m, bar_1d=None, perf_dict=None):
    from data_feed_eth import get_composite_signal as eth_composite
    current_trade = eth_trader.strategy.current_trade
    return {
        "pair":          "ETH",
        "panel_mode":    "pre_checks",
        "decision":      "STAY_OUT",
        "confidence":    "--",
        "one_hour_bias": "--",
        "reasoning":     f"[ETH] Pre-check blocked: {block_reason}",
        "warnings":      [block_reason] if block_reason else [],
        "tokens_used":   "--",
        "pre_checks":    pre_checks,
        "block_reason":  block_reason,
        "checklist":     {},
        "whale":         whale_data,
        "position":      _build_position(current_trade),
        "performance":   perf_dict or {},
        "price":         float(bar_5m.get("close", 0)),
        "indicators_1h": _bar_to_ind1h(bar_1h),
        "indicators_5m": _bar_to_ind5m(bar_5m),
        "indicators_1d": {"ssl_bull": bool(bar_1d.get("ssl_bull", False)), "rsi": _safe_ind(bar_1d.get("rsi"))} if bar_1d is not None else {},
        "trend_1h":      "LONG" if bar_1h.get("ssl_bull") else "SHORT",
        "signal_5m":     eth_composite(bar_5m),
        "trend_1d":      "LONG" if (bar_1d is not None and bar_1d.get("ssl_bull")) else "SHORT",
    }


def build_eth_claude_state(decision, pre_checks, whale_data, eth_trader, account,
                            bar_1h, bar_5m, bar_1d=None, perf_dict=None):
    from data_feed_eth import get_composite_signal as eth_composite
    current_trade = eth_trader.strategy.current_trade
    return {
        "pair":          "ETH",
        "panel_mode":    "claude",
        "decision":      decision.get("decision", "STAY_OUT"),
        "confidence":    decision.get("confidence", "--"),
        "one_hour_bias": decision.get("one_hour_bias", "--"),
        "reasoning":     decision.get("reasoning", ""),
        "warnings":      decision.get("warnings", []),
        "tokens_used":   decision.get("tokens_used", "--"),
        "pre_checks":    pre_checks,
        "block_reason":  "",
        "checklist":     decision.get("checklist", {}),
        "whale":         whale_data,
        "position":      _build_position(current_trade),
        "performance":   perf_dict or {},
        "price":         float(bar_5m.get("close", 0)),
        "indicators_1h": _bar_to_ind1h(bar_1h),
        "indicators_5m": _bar_to_ind5m(bar_5m),
        "indicators_1d": {"ssl_bull": bool(bar_1d.get("ssl_bull", False)), "rsi": _safe_ind(bar_1d.get("rsi"))} if bar_1d is not None else {},
        "trend_1h":      "LONG" if bar_1h.get("ssl_bull") else "SHORT",
        "signal_5m":     eth_composite(bar_5m),
        "trend_1d":      "LONG" if (bar_1d is not None and bar_1d.get("ssl_bull")) else "SHORT",
    }


def _build_position(current_trade):
    if current_trade is None:
        return None
    return {
        "direction":         current_trade.direction,
        "entry_price":       current_trade.entry_price,
        "stop_loss":         current_trade.stop_loss,
        "take_profit":       current_trade.take_profit,
        "ladder_step":       getattr(current_trade, "ladder_step", 0),
        "ladder_floor_gbp":  getattr(current_trade, "ladder_floor_gbp", 0.0),
        "position_size_gbp": current_trade.position_size_gbp,
        "entry_time":        current_trade.entry_time.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _fresh_account(capital: float) -> dict:
    return {
        "killed":             False,
        "kill_reason":        "",
        "daily_pnl_gbp":      0.0,
        "consecutive_losses": 0,
        "last_loss_time":     None,
        "session_start":      datetime.now(timezone.utc).isoformat(),
        # Tiered kill switch fields
        "kill_history":       [],   # ISO timestamps of triggers in last 48h
        "kill_time":          None, # when the current kill was triggered
        "kill_wait_hours":    6,    # wait duration for current tier
        "kill_tier":          0,    # current tier (0 = not killed)
        "kill_last_log":      None, # last countdown log timestamp
    }


def _update_account_after_trade(account: dict, pnl_gbp: float) -> dict:
    account["daily_pnl_gbp"] = round(account["daily_pnl_gbp"] + pnl_gbp, 2)
    if pnl_gbp < 0:
        account["consecutive_losses"] += 1
        account["last_loss_time"]      = datetime.now(timezone.utc).isoformat()
        log.warning(
            "Loss recorded -- consecutive losses: %d | daily P&L: GBP %.2f",
            account["consecutive_losses"],
            account["daily_pnl_gbp"],
        )
    else:
        account["consecutive_losses"] = 0
        log.info(
            "Win recorded -- consecutive losses reset | daily P&L: GBP %.2f",
            account["daily_pnl_gbp"],
        )
    return account


def _log_kill_countdown(account: dict) -> None:
    """Log kill switch countdown -- at most once per hour."""
    kill_time_str = account.get("kill_time")
    if not kill_time_str:
        return
    now = datetime.now(timezone.utc)
    last_log = account.get("kill_last_log")
    if last_log:
        last_log_dt = datetime.fromisoformat(last_log).replace(tzinfo=timezone.utc)
        if (now - last_log_dt).total_seconds() < 3600:
            return
    wait_hours = account.get("kill_wait_hours", 6)
    tier       = account.get("kill_tier", 1)
    kill_time  = datetime.fromisoformat(kill_time_str).replace(tzinfo=timezone.utc)
    elapsed    = (now - kill_time).total_seconds() / 3600
    remaining  = max(0.0, wait_hours - elapsed)
    resume_at  = kill_time + timedelta(hours=wait_hours)
    log.info(
        "KILL SWITCH ACTIVE (Tier %d) -- %.1f hours remaining"
        " | auto-resume at %s UTC | system is alive",
        tier, remaining, resume_at.strftime("%Y-%m-%d %H:%M"),
    )
    account["kill_last_log"] = now.isoformat()


def _notify_trade_closed(trade, capital: float, pair: str = "BTC") -> None:
    """Send win or loss notification for a closed trade."""
    pnl     = getattr(trade, "pnl_gbp", 0.0) or 0.0
    size    = getattr(trade, "position_size_gbp", 1.0) or 1.0
    price   = getattr(trade, "exit_price", 0.0) or 0.0
    reason  = getattr(trade, "exit_reason", "UNKNOWN") or "UNKNOWN"
    pnl_pct = abs(pnl) / size * 100 if size else 0.0
    if pnl >= 0:
        notifier.notify_trade_closed_win(
            direction=trade.direction,
            close_price=price,
            pnl_gbp=pnl,
            pnl_pct=pnl_pct,
            capital=capital,
            reason=reason,
            pair=pair,
        )
    else:
        notifier.notify_trade_closed_loss(
            direction=trade.direction,
            close_price=price,
            pnl_gbp=pnl,
            pnl_pct=pnl_pct,
            capital=capital,
            reason=reason,
            pair=pair,
        )


def get_current_price(client: krakenex.API) -> float | None:
    try:
        result = client.query_public("Ticker", {"pair": "XXBTZGBP"})
        if result.get("error"):
            return None
        pair_key = next((k for k in result["result"]), None)
        if not pair_key:
            return None
        return float(result["result"][pair_key]["c"][0])
    except Exception as e:
        log.warning("BTC price fetch failed: %s", e)
        return None


def get_current_price_eth(client: krakenex.API) -> float | None:
    try:
        result = client.query_public("Ticker", {"pair": "XETHZGBP"})
        if result.get("error"):
            return None
        pair_key = next((k for k in result["result"]), None)
        if not pair_key:
            return None
        return float(result["result"][pair_key]["c"][0])
    except Exception as e:
        log.warning("ETH price fetch failed: %s", e)
        return None


def seconds_until_next_candle() -> float:
    now     = datetime.now(timezone.utc)
    elapsed = (now.minute * 60 + now.second) % CANDLE_SECONDS
    return CANDLE_SECONDS - elapsed + CANDLE_CLOSE_BUFFER


def next_candle_time_str() -> str:
    secs     = seconds_until_next_candle()
    mins     = int(secs // 60)
    secs_rem = int(secs % 60)
    return f"{mins}m {secs_rem}s"


def run_candle_tick(feed, trader, account, tick_count):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("=" * 60)
    log.info("CryptoHybrid AI -- Candle Tick #%d | %s", tick_count, now)
    log.info("=" * 60)

    try:
        feed.refresh()
        bar_1h        = feed.latest_bar("1h")
        bar_5m        = feed.latest_bar("5m")
        current_price = float(bar_5m["close"])
        log.info("BTC/GBP = GBP %.2f", current_price)
    except Exception as exc:
        log.error("Data feed error: %s -- skipping tick", exc)
        return account

    # Publish the BTC 5m ATR (GBP) for the shared volatility gate used by BOTH the
    # BTC and ETH engines (System 2 Review, Change 3B). BTC runs before ETH each cycle.
    btc_atr = bar_5m.get("atr")
    regime_mod.set_btc_atr(btc_atr)
    # Publish the BTC price too (Commission 016): the shared volatility floor/ceiling
    # are now a % of this price, read by BOTH the BTC and ETH gates.
    regime_mod.set_btc_price(current_price)

    # Economic calendar hard block -- 15-min post-event volatility window
    hard_blocked, block_reason, ev_name, remain_mins = economic_calendar.is_hard_blocked()
    if hard_blocked:
        log.info("ECONOMIC CALENDAR BLOCK: %s", block_reason)
        notifier.notify_economic_block(ev_name, remain_mins)
        return account

    bar_1d = None
    try:
        bar_1d = feed.latest_bar("1d")
    except Exception as exc:
        log.warning("Daily bar unavailable: %s -- skipping daily trend filter", exc)

    whale_data = {}
    try:
        whale_data = get_whale_data(feed.client, feed.get("5m"))
    except Exception as exc:
        log.warning("Whale data failed: %s", exc)
        whale_data = {"note": "Whale data unavailable this tick"}

    perf_context = None
    perf_dict    = {}
    try:
        perf_context = performance_btc.get_performance_context(TRADES_LOG)
        perf_dict    = performance_btc.get_perf_dashboard_dict(TRADES_LOG)
    except Exception as exc:
        log.warning("Performance context failed: %s", exc)

    current_trade = trader.strategy.current_trade
    pre_checks    = run_individual_pre_checks(bar_1h, bar_5m, account, current_trade, bar_1d)

    was_killed    = account.get("killed", False)
    pre_result    = run_all_pre_checks(bar_1h, bar_5m, account, current_trade, bar_1d, btc_atr, current_price)
    is_killed_now = account.get("killed", False)

    # Detect a freshly triggered kill switch and notify
    if not was_killed and is_killed_now:
        tier     = account.get("kill_tier", 1)
        wait_h   = account.get("kill_wait_hours", 6)
        notifier.notify_kill_switch_triggered(
            tier=tier,
            reason=account["kill_reason"],
            wait_hours=wait_h,
            daily_pnl=account["daily_pnl_gbp"],
            capital=trader.strategy.capital_gbp,
        )
        if tier >= 3:
            notifier.notify_tier3_urgent()

    if not pre_result["passed"]:
        log.info("PRE-CHECK BLOCKED: %s", pre_result["reason"])
        push_to_dashboard(build_pre_check_state(
            pre_checks=pre_checks,
            block_reason=pre_result["reason"],
            whale_data=whale_data,
            trader=trader,
            account=account,
            perf_dict=perf_dict,
        ))
        log.info("Action: STAY_OUT")
        log.info(
            "Capital: GBP %.2f | Trades: %d | Win rate: %.0f%%",
            trader.strategy.capital_gbp,
            trader.strategy.total_trades,
            trader.strategy.win_rate,
        )
        return account

    # ── Zone-3 Morgan HARD BLOCK (three-zone model, 24 Jul 2026 -- BTC) ────────
    # Below 30 the system suspends NEW entries and Gaius intervenes. Gated on
    # "no open position" so a live BTC trade always still runs its exit/HOLD logic.
    # Does NOT touch the Morgan SHORT gate (that is a separate bidirectional sweep).
    if current_trade is None:
        _morgan = (perf_dict or {}).get("confidence_score", 50)
        if performance_btc.morgan_hard_block(_morgan):
            log.warning(
                "MORGAN HARD BLOCK -- confidence %s < 30 (BTC). New entries suspended; "
                "Gaius intervention active. Skipping Arthur consult this tick.", _morgan,
            )
            push_to_dashboard(build_pre_check_state(
                pre_checks=pre_checks,
                block_reason="MORGAN HARD BLOCK (<30) -- new entries suspended (BTC)",
                whale_data=whale_data,
                trader=trader,
                account=account,
                perf_dict=perf_dict,
            ))
            return account

    log.info("All pre-checks passed -- calling Claude...")
    try:
        event_context = economic_calendar.get_event_context()
    except Exception as exc:
        log.warning("Economic calendar context failed: %s", exc)
        event_context = None

    # Regime context for Arthur (System 2 Review, Change 1/2). Direction is the 1h
    # SSL (primary for scalping); regime awareness goes INTO Arthur's prompt so it
    # reasons rather than being hard-gated before it thinks. Fully bidirectional
    # (24 Jul 2026): no Morgan SHORT gate -- SHORT and LONG on identical terms.
    regime = regime_mod.get_regime()
    proposed_direction = "LONG" if bar_1h.get("ssl_bull") else "SHORT"

    decision = get_trading_decision(
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        current_price=current_price,
        current_trade=current_trade,
        whale_data=whale_data,
        event_context=event_context,
        perf_context=perf_context,
        regime=regime,
    )

    print(format_decision_for_display(decision))

    push_to_dashboard(build_claude_state(
        decision=decision,
        pre_checks=pre_checks,
        whale_data=whale_data,
        trader=trader,
        account=account,
        perf_dict=perf_dict,
    ))

    decision_text  = decision.get("decision", "STAY_OUT")
    confidence     = decision.get("confidence", "LOW")
    trades_before  = trader.strategy.total_trades
    in_trade_before = trader.strategy.in_trade

    if decision_text == "ENTER_LONG" and not trader.strategy.in_trade:
        if confidence in ("HIGH", "MEDIUM"):
            action = trader.update(bar_1h, bar_5m, current_price)
            log.info("Action taken: %s", action)
        else:
            log.info("LOW confidence -- skipping entry")
    elif decision_text == "ENTER_SHORT" and not trader.strategy.in_trade:
        # Fully bidirectional (24 Jul 2026): a SHORT scalp takes the SAME confidence
        # bar as a LONG -- no Morgan SHORT gate, no regime asymmetry.
        if confidence in ("HIGH", "MEDIUM"):
            action = trader.update(bar_1h, bar_5m, current_price)
            log.info("Action taken: %s", action)
        else:
            log.info("LOW confidence -- skipping entry")
    elif decision_text in ("EXIT_LONG", "EXIT_SHORT") and trader.strategy.in_trade:
        action = trader.update(bar_1h, bar_5m, current_price)
        log.info("Action taken: %s", action)
    else:
        log.info("Action: HOLD -- watching market")

    # Phantom tracker: record Arthur/Claude STAY_OUT decisions (post pre-checks)
    if decision_text == "STAY_OUT":
        try:
            _dir = "LONG" if bar_1h.get("ssl_bull") else "SHORT"
            try:
                _snap = phantom_tracker.build_snapshot(
                    _bar_to_ind(bar_1d), _bar_to_ind(bar_1h), _bar_to_ind(bar_5m),
                    morgan_score=performance_btc.get_confidence(),
                    now_utc=datetime.now(timezone.utc),
                )
            except Exception as _se:
                log.warning("phantom indicator snapshot failed: %s", _se)
                _snap = None
            phantom_tracker.record_decision(
                market="BTC",
                direction_blocked=_dir,
                price_at_decision=current_price,
                confidence=confidence,
                reason_for_stay_out="ARTHUR_STAY_OUT",
                get_price_fn=lambda m: get_current_price(feed.client),
                indicators=_snap,
            )
        except Exception as _exc:
            log.warning("phantom_tracker record failed: %s", _exc)

    # Notify if a new trade was just opened
    if not in_trade_before and trader.strategy.in_trade:
        t = trader.strategy.current_trade
        if t is not None:
            notifier.notify_trade_opened(
                direction=t.direction,
                entry_price=t.entry_price,
                stop_loss=t.stop_loss,
                take_profit=t.take_profit,
                size_gbp=t.position_size_gbp,
            )

    trades_after = trader.strategy.total_trades
    if trades_after > trades_before:
        last_trade = trader.strategy.trade_history[-1]
        account    = _update_account_after_trade(account, last_trade.pnl_gbp)
        _notify_trade_closed(last_trade, trader.strategy.capital_gbp)
        performance_btc.invalidate_cache()
        if trades_after % 50 == 0:
            milestone_num = trades_after // 50
            try:
                performance_btc.generate_milestone_review(TRADES_LOG, milestone_num)
                notifier.notify_milestone_review(milestone_num)
            except Exception as exc:
                log.warning("Milestone review failed: %s", exc)

    log.info(
        "Capital: GBP %.2f | Daily P&L: GBP %+.2f | Trades: %d | Win rate: %.0f%%",
        trader.strategy.capital_gbp,
        account["daily_pnl_gbp"],
        trader.strategy.total_trades,
        trader.strategy.win_rate,
    )
    return account


def _check_1m_exit_signal(trade, bar_1m) -> str | None:
    """
    Check 1m SSL and RSI for an early exit signal.
    Both must agree before we exit -- same two-signal rule as the 5m exit confirmation.
    Returns an exit reason string or None if no signal.
    """
    ssl_1m = bar_1m.get("ssl_bull")
    rsi_1m = bar_1m.get("rsi")
    if pd.isna(ssl_1m) or pd.isna(rsi_1m):
        return None
    if trade.direction == "LONG" and not bool(ssl_1m) and float(rsi_1m) < 50:
        return "1M_REVERSAL_LONG"
    if trade.direction == "SHORT" and bool(ssl_1m) and float(rsi_1m) > 50:
        return "1M_REVERSAL_SHORT"
    return None


def _close_and_record(trader, account, price, reason, pair: str = "BTC",
                       trades_log=None) -> dict:
    """Close the current trade, persist it, and update account P&L."""
    if trades_log is None:
        trades_log = TRADES_LOG
    trades_before = trader.strategy.total_trades
    trader.strategy._close_trade(price, reason)
    if trader.strategy.total_trades > trades_before:
        last_trade = trader.strategy.trade_history[-1]
        trader._log_trade(last_trade)
        trader._save_summary()
        account = _update_account_after_trade(account, last_trade.pnl_gbp)
        log.info(
            "[%s] Trade closed | P&L=GBP %+.2f | Capital=GBP %.2f",
            pair, last_trade.pnl_gbp,
            trader.strategy.capital_gbp,
        )
        _notify_trade_closed(last_trade, trader.strategy.capital_gbp, pair=pair)
        performance_btc.invalidate_cache()
        total_trades = trader.strategy.total_trades
        if total_trades % 50 == 0:
            milestone_num = total_trades // 50
            try:
                performance_btc.generate_milestone_review(trades_log, milestone_num)
                notifier.notify_milestone_review(milestone_num, pair=pair)
            except Exception as exc:
                log.warning("[%s] Milestone review failed: %s", pair, exc)
    return account


def monitor_open_position(feed, trader, account):
    price = get_current_price(feed.client)
    if price is None:
        log.warning("Could not fetch price -- skipping monitor check")
        return account

    trade = trader.strategy.current_trade
    if trade is None:
        return account

    trade.update_trailing_stop(price)
    trade.update_excursions(price)             # MAE/MFE tracking (Commission 009)
    _rung = trade.apply_profit_ladder(price)   # Profit ladder (Variant 2)
    if _rung:
        _log_ladder_step("CryptoHybrid-BTC", trade, _rung)
    exit_reason = trade.check_exit(price)

    if exit_reason:
        log.info("POSITION MONITOR: %s triggered at GBP %.2f", exit_reason, price)
        return _close_and_record(trader, account, price, exit_reason)

    # Smart 1m exit check -- detect reversals faster than waiting for 5m candle
    try:
        bar_1m = fetch_latest_1m_bar(feed.client)
        if bar_1m is not None:
            exit_1m = _check_1m_exit_signal(trade, bar_1m)
            if exit_1m:
                log.info(
                    "1m EXIT SIGNAL: %s at GBP %.2f (1m ssl=%s rsi=%.1f) -- closing early",
                    exit_1m, price,
                    "BEAR" if not bool(bar_1m.get("ssl_bull")) else "BULL",
                    float(bar_1m.get("rsi", 0)),
                )
                return _close_and_record(trader, account, price, exit_1m)
    except Exception as exc:
        log.warning("1m exit check failed: %s", exc)

    log.info(
        "Monitor | BTC/GBP=GBP %.2f | %s | stop=GBP %.2f | target=GBP %.2f",
        price, trade.direction, trade.stop_loss, trade.take_profit,
    )
    return account


def run_candle_tick_eth(eth_feed, eth_trader, eth_account, tick_count):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info("[ETH] Candle Tick #%d | %s", tick_count, now)

    try:
        eth_feed.refresh()
        bar_1h        = eth_feed.latest_bar("1h")
        bar_5m        = eth_feed.latest_bar("5m")
        current_price = float(bar_5m["close"])
        log.info("[ETH] ETH/GBP = GBP %.4f", current_price)
    except Exception as exc:
        log.error("[ETH] Data feed error: %s -- skipping tick", exc)
        return eth_account

    hard_blocked, block_reason, ev_name, remain_mins = economic_calendar.is_hard_blocked()
    if hard_blocked:
        log.info("[ETH] ECONOMIC CALENDAR BLOCK: %s", block_reason)
        return eth_account

    bar_1d = None
    try:
        bar_1d = eth_feed.latest_bar("1d")
    except Exception as exc:
        log.warning("[ETH] Daily bar unavailable: %s", exc)

    whale_data = {}
    try:
        whale_data = get_whale_data(eth_feed.client, eth_feed.get("5m"))
    except Exception as exc:
        log.warning("[ETH] Whale data failed: %s", exc)
        whale_data = {"note": "Whale data unavailable this tick"}

    eth_perf_context = None
    eth_perf_dict    = {}
    try:
        eth_perf_context = performance_btc.get_performance_context(ETH_TRADES_LOG)
        eth_perf_dict    = performance_btc.get_perf_dashboard_dict(ETH_TRADES_LOG)
    except Exception as exc:
        log.warning("[ETH] Performance context failed: %s", exc)

    current_trade = eth_trader.strategy.current_trade
    pre_checks    = run_individual_pre_checks_eth(bar_1h, bar_5m, eth_account, current_trade, bar_1d)

    was_killed    = eth_account.get("killed", False)
    # Shared BTC-led volatility gate: ETH uses the BTC 5m ATR published by the BTC
    # tick earlier this cycle (System 2 Review, Change 3B).
    pre_result    = eth_run_all_pre_checks(bar_1h, bar_5m, eth_account, current_trade,
                                           bar_1d, regime_mod.get_btc_atr(),
                                           regime_mod.get_btc_price())
    is_killed_now = eth_account.get("killed", False)

    if not was_killed and is_killed_now:
        tier   = eth_account.get("kill_tier", 1)
        wait_h = eth_account.get("kill_wait_hours", 6)
        notifier.notify_kill_switch_triggered(
            tier=tier,
            reason=eth_account["kill_reason"],
            wait_hours=wait_h,
            daily_pnl=eth_account["daily_pnl_gbp"],
            capital=eth_trader.strategy.capital_gbp,
            pair="ETH",
        )
        if tier >= 3:
            notifier.notify_tier3_urgent(pair="ETH")

    if not pre_result["passed"]:
        log.info("[ETH] PRE-CHECK BLOCKED: %s", pre_result["reason"])
        push_to_dashboard(build_eth_pre_check_state(
            pre_checks=pre_checks,
            block_reason=pre_result["reason"],
            whale_data=whale_data,
            eth_trader=eth_trader,
            account=eth_account,
            bar_1h=bar_1h,
            bar_5m=bar_5m,
            bar_1d=bar_1d,
            perf_dict=eth_perf_dict,
        ))
        log.info("[ETH] Action: STAY_OUT")
        log.info(
            "[ETH] Capital: GBP %.2f | Trades: %d | Win rate: %.0f%%",
            eth_trader.strategy.capital_gbp,
            eth_trader.strategy.total_trades,
            eth_trader.strategy.win_rate,
        )
        return eth_account

    # ── Zone-3 Morgan HARD BLOCK (three-zone model, 24 Jul 2026 -- ETH) ────────
    # Below 30 the system suspends NEW entries and Gaius intervenes. Gated on
    # "no open position" so a live ETH trade always still runs its exit/HOLD logic.
    # Does NOT touch the Morgan SHORT gate (that is a separate bidirectional sweep).
    if current_trade is None:
        _morgan = (eth_perf_dict or {}).get("confidence_score", 50)
        if performance_btc.morgan_hard_block(_morgan):
            log.warning(
                "[ETH] MORGAN HARD BLOCK -- confidence %s < 30. New entries suspended; "
                "Gaius intervention active. Skipping Arthur consult this tick.", _morgan,
            )
            push_to_dashboard(build_eth_pre_check_state(
                pre_checks=pre_checks,
                block_reason="MORGAN HARD BLOCK (<30) -- new entries suspended (ETH)",
                whale_data=whale_data,
                eth_trader=eth_trader,
                account=eth_account,
                bar_1h=bar_1h,
                bar_5m=bar_5m,
                bar_1d=bar_1d,
                perf_dict=eth_perf_dict,
            ))
            return eth_account

    log.info("[ETH] All pre-checks passed -- calling Claude...")
    try:
        event_context = economic_calendar.get_event_context()
    except Exception as exc:
        log.warning("[ETH] Economic calendar context failed: %s", exc)
        event_context = None

    # Regime context for Arthur (System 2 Review, Change 1/2). Fully bidirectional
    # (24 Jul 2026): no Morgan SHORT gate -- SHORT and LONG on identical terms.
    regime = regime_mod.get_regime()
    proposed_direction = "LONG" if bar_1h.get("ssl_bull") else "SHORT"

    decision = eth_get_trading_decision(
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        current_price=current_price,
        current_trade=current_trade,
        whale_data=whale_data,
        event_context=event_context,
        perf_context=eth_perf_context,
        regime=regime,
    )

    print(eth_format_decision_for_display(decision))

    push_to_dashboard(build_eth_claude_state(
        decision=decision,
        pre_checks=pre_checks,
        whale_data=whale_data,
        eth_trader=eth_trader,
        account=eth_account,
        bar_1h=bar_1h,
        bar_5m=bar_5m,
        bar_1d=bar_1d,
        perf_dict=eth_perf_dict,
    ))

    decision_text   = decision.get("decision", "STAY_OUT")
    confidence      = decision.get("confidence", "LOW")
    trades_before   = eth_trader.strategy.total_trades
    in_trade_before = eth_trader.strategy.in_trade

    if decision_text == "ENTER_LONG" and not eth_trader.strategy.in_trade:
        if confidence in ("HIGH", "MEDIUM"):
            action = eth_trader.update(bar_1h, bar_5m, current_price)
            log.info("[ETH] Action taken: %s", action)
        else:
            log.info("[ETH] LOW confidence -- skipping entry")
    elif decision_text == "ENTER_SHORT" and not eth_trader.strategy.in_trade:
        # Fully bidirectional (24 Jul 2026): a SHORT scalp takes the SAME confidence
        # bar as a LONG -- no Morgan SHORT gate, no regime asymmetry.
        if confidence in ("HIGH", "MEDIUM"):
            action = eth_trader.update(bar_1h, bar_5m, current_price)
            log.info("[ETH] Action taken: %s", action)
        else:
            log.info("[ETH] LOW confidence -- skipping entry")
    elif decision_text in ("EXIT_LONG", "EXIT_SHORT") and eth_trader.strategy.in_trade:
        action = eth_trader.update(bar_1h, bar_5m, current_price)
        log.info("[ETH] Action taken: %s", action)
    else:
        log.info("[ETH] Action: HOLD -- watching market")

    # Phantom tracker: record Arthur/Claude STAY_OUT decisions (post pre-checks)
    if decision_text == "STAY_OUT":
        try:
            _dir = "LONG" if bar_1h.get("ssl_bull") else "SHORT"
            try:
                _snap = phantom_tracker.build_snapshot(
                    _bar_to_ind(bar_1d), _bar_to_ind(bar_1h), _bar_to_ind(bar_5m),
                    morgan_score=performance_btc.get_confidence(),
                    now_utc=datetime.now(timezone.utc),
                )
            except Exception as _se:
                log.warning("phantom indicator snapshot failed: %s", _se)
                _snap = None
            phantom_tracker.record_decision(
                market="ETH",
                direction_blocked=_dir,
                price_at_decision=current_price,
                confidence=confidence,
                reason_for_stay_out="ARTHUR_STAY_OUT",
                get_price_fn=lambda m: get_current_price_eth(eth_feed.client),
                indicators=_snap,
            )
        except Exception as _exc:
            log.warning("phantom_tracker record failed: %s", _exc)

    if not in_trade_before and eth_trader.strategy.in_trade:
        t = eth_trader.strategy.current_trade
        if t is not None:
            notifier.notify_trade_opened(
                direction=t.direction,
                entry_price=t.entry_price,
                stop_loss=t.stop_loss,
                take_profit=t.take_profit,
                size_gbp=t.position_size_gbp,
                pair="ETH",
            )

    trades_after = eth_trader.strategy.total_trades
    if trades_after > trades_before:
        last_trade  = eth_trader.strategy.trade_history[-1]
        eth_account = _update_account_after_trade(eth_account, last_trade.pnl_gbp)
        _notify_trade_closed(last_trade, eth_trader.strategy.capital_gbp, pair="ETH")
        performance_btc.invalidate_cache()
        if trades_after % 50 == 0:
            milestone_num = trades_after // 50
            try:
                performance_btc.generate_milestone_review(ETH_TRADES_LOG, milestone_num)
                notifier.notify_milestone_review(milestone_num, pair="ETH")
            except Exception as exc:
                log.warning("[ETH] Milestone review failed: %s", exc)

    log.info(
        "[ETH] Capital: GBP %.2f | Daily P&L: GBP %+.2f | Trades: %d | Win rate: %.0f%%",
        eth_trader.strategy.capital_gbp,
        eth_account["daily_pnl_gbp"],
        eth_trader.strategy.total_trades,
        eth_trader.strategy.win_rate,
    )
    return eth_account


def monitor_open_position_eth(eth_feed, eth_trader, eth_account):
    price = get_current_price_eth(eth_feed.client)
    if price is None:
        log.warning("[ETH] Could not fetch price -- skipping monitor check")
        return eth_account

    trade = eth_trader.strategy.current_trade
    if trade is None:
        return eth_account

    trade.update_trailing_stop(price)
    trade.update_excursions(price)             # MAE/MFE tracking (Commission 009)
    _rung = trade.apply_profit_ladder(price)   # Profit ladder (Variant 2)
    if _rung:
        _log_ladder_step("CryptoHybrid-ETH", trade, _rung)
    exit_reason = trade.check_exit(price)

    if exit_reason:
        log.info("[ETH] POSITION MONITOR: %s triggered at GBP %.4f", exit_reason, price)
        return _close_and_record(eth_trader, eth_account, price, exit_reason,
                                  pair="ETH", trades_log=ETH_TRADES_LOG)

    try:
        bar_1m = eth_fetch_latest_1m_bar(eth_feed.client)
        if bar_1m is not None:
            exit_1m = _check_1m_exit_signal(trade, bar_1m)
            if exit_1m:
                log.info(
                    "[ETH] 1m EXIT SIGNAL: %s at GBP %.4f -- closing early",
                    exit_1m, price,
                )
                return _close_and_record(eth_trader, eth_account, price, exit_1m,
                                          pair="ETH", trades_log=ETH_TRADES_LOG)
    except Exception as exc:
        log.warning("[ETH] 1m exit check failed: %s", exc)

    log.info(
        "[ETH] Monitor | ETH/GBP=GBP %.4f | %s | stop=GBP %.4f | target=GBP %.4f",
        price, trade.direction, trade.stop_loss, trade.take_profit,
    )
    return eth_account


def _log_ladder_step(system, trade, rung):
    """Append a profit-ladder rung trigger to logs/profit_ladder.csv (Variant 2)."""
    import csv
    path = LOG_FILE.parent / "profit_ladder.csv"
    try:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp_utc", "system", "direction",
                "entry_price", "trigger_float_gbp", "floor_gbp", "step_number",
                "stop_before", "stop_after"])
            if new:
                w.writeheader()
            w.writerow({"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "system": system, "direction": trade.direction, "entry_price": trade.entry_price,
                "trigger_float_gbp": rung["trigger_float_gbp"], "floor_gbp": rung["floor_gbp"],
                "step_number": rung["step"], "stop_before": rung["stop_before"],
                "stop_after": rung["stop_after"]})
    except Exception as exc:
        log.warning("Could not log ladder step: %s", exc)


def _apply_confidence_lift() -> None:
    """Apply a pending manual confidence lift (logs/confidence_lift.json) in-process
    so a Gaius/dashboard lift takes effect LIVE -- Morgan's persisted baseline is
    otherwise cached in this process until restart. Written by the dashboard
    /api/lift-confidence endpoint (or Gaius --lift); consumed here via the existing
    set_confidence() and the flag deleted. Does not change the confidence algorithm."""
    import json
    try:
        if not LIFT_FLAG.exists():
            return
        data = json.loads(LIFT_FLAG.read_text(encoding="utf-8"))
        val = max(0.0, min(100.0, float(data.get("confidence", 50.0))))
        reason = data.get("reason") or "CONFIDENCE LIFT -- manual override"
        prior = performance_btc.get_confidence()
        performance_btc.set_confidence(val, reason=reason)
        LIFT_FLAG.unlink(missing_ok=True)
        log.warning("Morgan CONFIDENCE LIFT applied live: %.1f -> %.1f (%s)", prior, val, reason)
    except Exception as _exc:
        log.warning("Confidence lift apply failed: %s", _exc)
        try:
            LIFT_FLAG.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    log.info("*" * 60)
    log.info("  CryptoHybrid AI -- Starting up")
    log.info("  BTC/GBP + ETH/GBP Dual-Pair Paper Trading System")
    log.info("  Claude AI + Whale Watching + 30s Position Monitoring")
    log.info("  Press Ctrl+C at any time to stop safely")
    log.info("*" * 60)

    # ── BTC feed ─────────────────────────────────────────────────────────────
    log.info("Loading live BTC/GBP data from Kraken...")
    try:
        feed = BTCDataFeed()
        feed.initialise()
    except Exception as exc:
        log.error("FATAL: Could not initialise BTC data feed: %s", exc)
        return

    # ── ETH feed ─────────────────────────────────────────────────────────────
    log.info("Loading live ETH/GBP data from Kraken...")
    try:
        eth_feed = ETHDataFeed()
        eth_feed.initialise()
    except Exception as exc:
        log.error("FATAL: Could not initialise ETH data feed: %s", exc)
        return

    # ── Traders ──────────────────────────────────────────────────────────────
    log.info("Initialising BTC paper trader...")
    try:
        trader = PaperTrader()
    except Exception as exc:
        log.error("FATAL: Could not initialise BTC paper trader: %s", exc)
        return

    log.info("Initialising ETH paper trader...")
    try:
        eth_trader = PaperTraderETH()
    except Exception as exc:
        log.error("FATAL: Could not initialise ETH paper trader: %s", exc)
        return

    account     = _fresh_account(trader.strategy.capital_gbp)
    eth_account = _fresh_account(eth_trader.strategy.capital_gbp)

    feed.print_status()
    trader.print_status()
    eth_feed.print_status()
    eth_trader.print_status()

    # ── Historical price lookup for phantom resolution (BTC + ETH) ────────────
    def get_historical_price(market, timestamp_utc):
        """Return the close of the 5-minute candle at/just before timestamp_utc
        for the given market ('BTC' or 'ETH'). Never raises -- returns None on
        any problem. Prefers the in-memory 5m frame; falls back to a Kraken OHLC
        query around the timestamp."""
        try:
            mkt = (market or "").upper()
            if mkt == "BTC":
                target_feed, pair = feed, "XXBTZGBP"
            elif mkt == "ETH":
                target_feed, pair = eth_feed, "XETHZGBP"
            else:
                return None

            # Normalise timestamp to a tz-aware UTC pandas Timestamp
            ts = pd.Timestamp(timestamp_utc)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

            # PREFERRED: nearest 5m candle at/before ts from the cached frame
            try:
                df = target_feed.get("5m")
                if df is not None and not df.empty and "close" in df.columns:
                    at_or_before = df.loc[df.index <= ts]
                    if not at_or_before.empty:
                        return float(at_or_before["close"].iloc[-1])
            except Exception:
                pass

            # FALLBACK: query Kraken OHLC (5m) around the timestamp
            try:
                since = int(ts.timestamp()) - 3600  # 1h earlier -> plenty of 5m candles
                resp = target_feed.client.query_public(
                    "OHLC", {"pair": pair, "interval": 5, "since": since}
                )
                if resp.get("error"):
                    return None
                result = resp["result"]
                pair_key = next((k for k in result if k != "last"), None)
                if pair_key is None:
                    return None
                target_epoch = ts.timestamp()
                best_close = None
                for candle in result[pair_key]:
                    if int(candle[0]) <= target_epoch:
                        best_close = float(candle[4])  # close column
                    else:
                        break
                return best_close
            except Exception:
                return None
        except Exception:
            return None

    # Resolve any phantom PENDING verdicts that survived a restart (BTC + ETH),
    # then start a continuous watchdog so new stale rows resolve every 15 min.
    try:
        phantom_tracker.resolve_stale_pending(get_historical_price_fn=get_historical_price)
        phantom_tracker.start_watchdog(get_historical_price_fn=get_historical_price, interval_minutes=15)
    except Exception as _exc:
        log.warning("phantom resolve/watchdog startup failed: %s", _exc)

    # Start Morgan's individual phantom-verdict feedback poller (5-min interval).
    try:
        performance_btc.process_new_phantom_verdicts()
    except Exception as _exc:
        log.warning("Morgan phantom verdict poller startup failed: %s", _exc)

    # Restore Morgan's confidence from the persistent CSV history on startup.
    try:
        _saved_conf = performance_btc.load_confidence()
        if _saved_conf is not None:
            performance_btc.set_confidence(_saved_conf, reason='restore')
            log.info("Morgan: confidence restored to %.2f", _saved_conf)
        else:
            log.info("Morgan: no saved confidence -- using baseline 50")
    except Exception as _exc:
        log.warning("Morgan confidence restore failed: %s", _exc)

    # Apply any confidence lift requested while the engine was down (Step 4).
    _apply_confidence_lift()

    combined_capital = trader.strategy.capital_gbp + eth_trader.strategy.capital_gbp

    log.info("*" * 60)
    log.info("  CryptoHybrid AI is LIVE -- DUAL PAIR MODE")
    log.info("  BTC capital: GBP %.2f", trader.strategy.capital_gbp)
    log.info("  ETH capital: GBP %.2f", eth_trader.strategy.capital_gbp)
    log.info("  Combined:    GBP %.2f", combined_capital)
    log.info("  Watching:    Every 5 minutes (candle close)")
    log.info("  In trade:    Every 30 seconds (price monitor)")
    log.info("  Dashboard:   http://localhost:5041")
    log.info("  Log file:    %s", LOG_FILE)
    log.info("  Press Ctrl+C to stop")
    log.info("*" * 60)

    notifier.notify_system_startup(trader.strategy.capital_gbp, pair="BTC")
    notifier.notify_system_startup(eth_trader.strategy.capital_gbp, pair="ETH")

    time.sleep(STARTUP_WAIT)

    tick_count             = 0
    next_candle_in         = seconds_until_next_candle()
    last_summary_date      = datetime.now(timezone.utc).date()
    btc_daily_trades_start = trader.strategy.total_trades
    eth_daily_trades_start = eth_trader.strategy.total_trades

    # Clear any stale shutdown flag left over from a previous session so we don't
    # exit immediately. During a run the flag is written by the dashboard and
    # consumed (deleted) by the watchdog -- see the shutdown check below.
    SHUTDOWN_FLAG.unlink(missing_ok=True)

    while True:
        try:
            # ── Dashboard shutdown check ──────────────────────────────────────
            if SHUTDOWN_FLAG.exists():
                # Leave the flag on disk so the watchdog (Galahad) sees it,
                # stops itself, and does NOT relaunch us. The watchdog deletes
                # the flag on its way out. (Previously we deleted it here, which
                # hid the shutdown from the watchdog -- it then restarted us.)
                log.info("Shutdown requested via dashboard -- stopping (flag left for watchdog)")
                break

            # Apply a pending manual confidence lift live (Gaius intervention Step 4).
            _apply_confidence_lift()

            # ── Midnight daily reset and summary ──────────────────────────────
            today = datetime.now(timezone.utc).date()
            if today > last_summary_date:
                notifier.notify_daily_summary(
                    date_str=last_summary_date.strftime("%d/%m/%Y"),
                    trades=trader.strategy.total_trades - btc_daily_trades_start,
                    pnl_gbp=account["daily_pnl_gbp"],
                    capital=trader.strategy.capital_gbp,
                    win_rate=trader.strategy.win_rate,
                    pair="BTC",
                )
                notifier.notify_daily_summary(
                    date_str=last_summary_date.strftime("%d/%m/%Y"),
                    trades=eth_trader.strategy.total_trades - eth_daily_trades_start,
                    pnl_gbp=eth_account["daily_pnl_gbp"],
                    capital=eth_trader.strategy.capital_gbp,
                    win_rate=eth_trader.strategy.win_rate,
                    pair="ETH",
                )
                account["daily_pnl_gbp"]     = 0.0
                eth_account["daily_pnl_gbp"] = 0.0

                for acct, label in [(account, "BTC"), (eth_account, "ETH")]:
                    if (acct.get("killed")
                            and "daily loss" in acct.get("kill_reason", "").lower()):
                        acct["killed"]      = False
                        acct["kill_reason"] = ""
                        acct["kill_time"]   = None
                        log.info("[%s] Daily reset -- daily loss kill switch cleared", label)

                last_summary_date      = today
                btc_daily_trades_start = trader.strategy.total_trades
                eth_daily_trades_start = eth_trader.strategy.total_trades

            # ── Kill switch countdown and auto-reset (BTC) ────────────────────
            if account.get("killed", False):
                _log_kill_countdown(account)
                if check_kill_switch_reset(account):
                    notifier.notify_kill_switch_reset(
                        tier=account.get("kill_tier", 1),
                        wait_hours=account.get("kill_wait_hours", 6),
                        capital=trader.strategy.capital_gbp,
                        pair="BTC",
                    )

            # ── Kill switch countdown and auto-reset (ETH) ────────────────────
            if eth_account.get("killed", False):
                _log_kill_countdown(eth_account)
                if eth_check_kill_switch_reset(eth_account):
                    notifier.notify_kill_switch_reset(
                        tier=eth_account.get("kill_tier", 1),
                        wait_hours=eth_account.get("kill_wait_hours", 6),
                        capital=eth_trader.strategy.capital_gbp,
                        pair="ETH",
                    )

            # ── Position monitoring (30s) when either pair has an open trade ──
            if trader.strategy.in_trade or eth_trader.strategy.in_trade:
                log.info(
                    "Position(s) open -- monitoring every %ds | next candle in %s",
                    POSITION_CHECK_SECS, next_candle_time_str(),
                )
                elapsed = 0
                while elapsed < next_candle_in:
                    time.sleep(POSITION_CHECK_SECS)
                    elapsed        += POSITION_CHECK_SECS
                    next_candle_in  = max(0, next_candle_in - POSITION_CHECK_SECS)
                    if trader.strategy.in_trade:
                        account = monitor_open_position(feed, trader, account)
                    if eth_trader.strategy.in_trade:
                        eth_account = monitor_open_position_eth(eth_feed, eth_trader, eth_account)
            else:
                log.info("Watching for setups -- next candle in %s", next_candle_time_str())
                time.sleep(next_candle_in)

            tick_count    += 1
            account        = run_candle_tick(feed, trader, account, tick_count)
            eth_account    = run_candle_tick_eth(eth_feed, eth_trader, eth_account, tick_count)
            next_candle_in = seconds_until_next_candle()

            combined = trader.strategy.capital_gbp + eth_trader.strategy.capital_gbp
            log.info(
                "Combined capital: GBP %.2f (BTC: GBP %.2f | ETH: GBP %.2f)",
                combined, trader.strategy.capital_gbp, eth_trader.strategy.capital_gbp,
            )

        except KeyboardInterrupt:
            log.info("")
            log.info("*" * 60)
            log.info("  CryptoHybrid AI -- Shutting down cleanly")
            log.info("  Ticks completed:  %d", tick_count)
            log.info("  BTC capital:      GBP %.2f", trader.strategy.capital_gbp)
            log.info("  ETH capital:      GBP %.2f", eth_trader.strategy.capital_gbp)
            combined = trader.strategy.capital_gbp + eth_trader.strategy.capital_gbp
            log.info("  Combined:         GBP %.2f", combined)
            for t, label in [(trader, "BTC"), (eth_trader, "ETH")]:
                if t.strategy.in_trade:
                    tr = t.strategy.current_trade
                    log.warning("  [%s] WARNING: Trade still open!", label)
                    log.warning(
                        "  [%s] %s entered at GBP %.2f | stop=GBP %.2f",
                        label, tr.direction, tr.entry_price, tr.stop_loss,
                    )
            log.info("  BTC log:   logs/trades.csv")
            log.info("  ETH log:   logs/eth_trades.csv")
            log.info("  Full log:  logs/cryptohybrid.log")
            log.info("  Goodbye!")
            log.info("*" * 60)
            break

        except Exception as exc:
            log.error("Unexpected error: %s", exc)
            log.error("Continuing in 60 seconds...")
            time.sleep(60)
            next_candle_in = seconds_until_next_candle()


if __name__ == "__main__":
    main()