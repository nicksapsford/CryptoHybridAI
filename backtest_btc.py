"""
CryptoHybrid AI -- backtest_btc.py
Dual-backtest engine: STRICT vs RELAXED rules with realistic trading costs.

Runs two independent backtests on the same historical data then prints a
side-by-side comparison.
"""

import csv
import logging
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

# ── CryptoHybrid AI imports ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from data_feed_btc import add_indicators
import strategy_btc
from strategy_btc import _score_bar, Trade, POSITION_SIZE_PCT
from pre_checks_btc import (
    run_all_pre_checks,
    check_kill_switch,
    check_daily_loss_limit,
    check_consecutive_losses,
    check_cooldown,
    check_daily_trend_filter,
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("CryptoHybrid.Backtest")

# Silence per-bar noise from sub-modules -- backtest iterates 17k+ bars
logging.getLogger("CryptoHybrid.PreChecks").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.Strategy").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.DataFeed").setLevel(logging.WARNING)


# ── Constants ──────────────────────────────────────────────────────────────────

STARTING_CAPITAL_GBP = 1_000.0
LOGS_DIR             = Path(__file__).parent / "logs"
YF_TICKER            = "BTC-GBP"
BACKTEST_DAYS        = 90     # 5m data capped at 60 days by Yahoo Finance

# Realistic round-trip trading costs per trade
ENTRY_FEE_PCT  = 0.0040   # 0.40% Kraken taker fee on entry
EXIT_FEE_PCT   = 0.0040   # 0.40% Kraken taker fee on exit
SPREAD_PCT     = 0.0002   # 0.02% estimated bid/offer spread
TOTAL_COST_PCT = ENTRY_FEE_PCT + EXIT_FEE_PCT + SPREAD_PCT   # 0.82%

# Limit order cost scenario (maker fee on Kraken Pro)
LIMIT_ENTRY_FEE_PCT = 0.0016   # 0.16% limit maker fee
LIMIT_EXIT_FEE_PCT  = 0.0016   # 0.16% limit maker fee
LIMIT_COST_PCT      = LIMIT_ENTRY_FEE_PCT + LIMIT_EXIT_FEE_PCT + SPREAD_PCT  # 0.34%


# ── Backtest configurations ────────────────────────────────────────────────────

CONFIGS = {
    "STRICT": {
        "name":           "STRICT",
        "min_5m":         5,         # 5 of 6 indicators must agree on 5m
        "min_1h":         4,         # 4 of 6 indicators must agree on 1h
        "quality_checks": True,      # all pre-checks including SSL, candle colour etc.
        "output_file":    "backtest_strict.txt",
        "csv_file":       "backtest_strict_trades.csv",
    },
    "RELAXED": {
        "name":           "RELAXED",
        "min_5m":         4,         # 4 of 6 indicators on 5m
        "min_1h":         3,         # 3 of 6 indicators on 1h
        "quality_checks": False,     # safety + daily filter only
        "output_file":    "backtest_relaxed.txt",
        "csv_file":       "backtest_relaxed_trades.csv",
    },
}


# ── Data fetching (Yahoo Finance) ──────────────────────────────────────────────
# Kraken's OHLC API is capped at 720 candles per call.
# Yahoo Finance provides 60 days of 5m, 730 days of 1h, years of 1d.

def _yf_download(interval: str, period: str) -> pd.DataFrame:
    """Download BTC-GBP and normalise MultiIndex columns to lowercase."""
    df = yf.download(YF_TICKER, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(
            f"yfinance returned no data for {YF_TICKER} ({interval}, {period})"
        )
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.sort_index(inplace=True)
    return df


def fetch_historical_data() -> tuple:
    """Fetch and indicator-enrich 1d, 1h, 5m OHLC data."""
    log.info("Fetching historical data from Yahoo Finance (%s)...", YF_TICKER)

    df_1d = add_indicators(_yf_download("1d", "2y"))
    log.info("  1d : %d candles  [%s -> %s]",
             len(df_1d),
             df_1d.index[0].strftime("%Y-%m-%d"),
             df_1d.index[-1].strftime("%Y-%m-%d"))

    df_1h = add_indicators(_yf_download("1h", f"{BACKTEST_DAYS}d"))
    log.info("  1h : %d candles  [%s -> %s]",
             len(df_1h),
             df_1h.index[0].strftime("%Y-%m-%d %H:%M"),
             df_1h.index[-1].strftime("%Y-%m-%d %H:%M"))

    days_5m = min(BACKTEST_DAYS, 60)
    df_5m = add_indicators(_yf_download("5m", f"{days_5m}d"))
    log.info("  5m : %d candles  [%s -> %s]",
             len(df_5m),
             df_5m.index[0].strftime("%Y-%m-%d %H:%M"),
             df_5m.index[-1].strftime("%Y-%m-%d %H:%M"))

    return df_1d, df_1h, df_5m


def fetch_historical_data_1h() -> tuple:
    """Fetch 730 days of 1h and 2 years of daily OHLC for the long backtest."""
    log.info("Fetching 730-day 1h data from Yahoo Finance (%s)...", YF_TICKER)

    df_1d = add_indicators(_yf_download("1d", "2y"))
    log.info("  1d : %d candles  [%s -> %s]",
             len(df_1d),
             df_1d.index[0].strftime("%Y-%m-%d"),
             df_1d.index[-1].strftime("%Y-%m-%d"))

    df_1h = add_indicators(_yf_download("1h", "730d"))
    log.info("  1h : %d candles  [%s -> %s]",
             len(df_1h),
             df_1h.index[0].strftime("%Y-%m-%d %H:%M"),
             df_1h.index[-1].strftime("%Y-%m-%d %H:%M"))

    return df_1d, df_1h


# ── Bar lookup ─────────────────────────────────────────────────────────────────

def _last_bar_at(df: pd.DataFrame, ts) -> Optional[pd.Series]:
    """Last closed bar in df at or before timestamp ts."""
    mask = df.index <= ts
    if not mask.any():
        return None
    return df[mask].iloc[-1]


# ── Trend helpers (configurable thresholds) ────────────────────────────────────

def _1h_trend(bar_1h: pd.Series, min_indicators: int) -> str:
    result = _score_bar(bar_1h)
    if result["long_count"]  >= min_indicators:
        return "LONG"
    if result["short_count"] >= min_indicators:
        return "SHORT"
    return "NEUTRAL"


def _daily_trend(bar_1d: Optional[pd.Series]) -> str:
    if bar_1d is None:
        return "NEUTRAL"
    ssl = bar_1d.get("ssl_bull")
    if pd.isna(ssl):
        return "NEUTRAL"
    return "LONG" if ssl else "SHORT"


def _combined_trend(bar_1h: pd.Series, bar_1d: Optional[pd.Series],
                    min_1h: int) -> str:
    """1h and daily SSL must agree; NEUTRAL daily means only 1h required."""
    t1h = _1h_trend(bar_1h, min_1h)
    t1d = _daily_trend(bar_1d)
    if t1h == "NEUTRAL":
        return "NEUTRAL"
    if t1d == "NEUTRAL":
        return t1h
    return t1h if t1h == t1d else "NEUTRAL"


# ── Pre-check dispatch ─────────────────────────────────────────────────────────

def _run_pre_checks(bar_1h, bar_5m, account, bar_1d, config: dict) -> dict:
    """
    STRICT: full run_all_pre_checks (all quality checks + daily filter).
    RELAXED: safety checks + daily trend filter only -- quality checks skipped.
    """
    if config["quality_checks"]:
        # Pass the 5m ATR so the volatility-range gate (Change 3B) applies in the
        # STRICT backtest too (it replaced the old choppy-market check).
        return run_all_pre_checks(
            bar_1h=bar_1h, bar_5m=bar_5m,
            account=account, current_trade=None, bar_1d=bar_1d,
            btc_atr=bar_5m.get("atr"),
        )
    # RELAXED -- safety only + daily trend filter
    for result in [
        check_kill_switch(account),
        check_daily_loss_limit(account),
        check_consecutive_losses(account),
        check_cooldown(account),
        check_daily_trend_filter(bar_1d, bar_1h, current_trade=None),
    ]:
        if not result["passed"]:
            return result
    return {"passed": True, "reason": None}


def _run_pre_checks_1h(bar_1h, account, bar_1d) -> dict:
    """Safety + daily trend pre-checks for 1h backtest -- no 5m quality checks."""
    for result in [
        check_kill_switch(account),
        check_daily_loss_limit(account),
        check_consecutive_losses(account),
        check_cooldown(account),
        check_daily_trend_filter(bar_1d, bar_1h, current_trade=None),
    ]:
        if not result["passed"]:
            return result
    return {"passed": True, "reason": None}


# ── Trade management ───────────────────────────────────────────────────────────

def _manage_bar(trade: Trade, bar: pd.Series) -> Optional[str]:
    """Update trailing stop and check SL/TP using bar high/low."""
    high = float(bar["high"])
    low  = float(bar["low"])
    if trade.direction == "LONG":
        trade.update_trailing_stop(high)
        if low  <= trade.stop_loss:   return "STOP_LOSS"
        if high >= trade.take_profit: return "TAKE_PROFIT"
    else:
        trade.update_trailing_stop(low)
        if high >= trade.stop_loss:   return "STOP_LOSS"
        if low  <= trade.take_profit: return "TAKE_PROFIT"
    return None


def _fill_price(trade: Trade, reason: str) -> float:
    if reason == "STOP_LOSS":   return trade.stop_loss
    if reason == "TAKE_PROFIT": return trade.take_profit
    return float("nan")


# ── Single backtest runner ─────────────────────────────────────────────────────

def run_single_backtest(config: dict, df_1d, df_1h, df_5m,
                        starting_capital: float = STARTING_CAPITAL_GBP,
                        cost_pct: float = TOTAL_COST_PCT,
                        trailing_stop_pct: Optional[float] = None,
                        take_profit_pct:   Optional[float] = None) -> dict:
    """
    Replay df_5m bar by bar using config rules.
    Returns a stats dict suitable for _compute_stats / _save_txt / _save_csv.
    """
    # Patch strategy module so Trade.__post_init__ and update_trailing_stop
    # use the requested TS/TP percentages; restored before return.
    _orig_ts = strategy_btc.TRAILING_STOP_PCT
    _orig_tp = strategy_btc.TAKE_PROFIT_PCT
    _ts_used = trailing_stop_pct if trailing_stop_pct is not None else _orig_ts
    _tp_used = take_profit_pct   if take_profit_pct   is not None else _orig_tp
    strategy_btc.TRAILING_STOP_PCT = _ts_used
    strategy_btc.TAKE_PROFIT_PCT   = _tp_used

    name = config["name"]
    log.info("")
    log.info("=" * 60)
    log.info("  Running %s backtest (%d 5m bars)...", name, len(df_5m))
    log.info("=" * 60)

    capital       = starting_capital
    current_trade: Optional[Trade] = None
    completed     = []

    account = {
        "killed":             False,
        "kill_reason":        "",
        "daily_pnl_gbp":      0.0,
        "consecutive_losses": 0,
        "last_loss_time":     None,
        "session_start":      datetime.now(timezone.utc).isoformat(),
    }

    current_day      = None
    bars_total       = len(df_5m)
    bars_checked     = 0
    pre_check_blocks = 0

    # Per-trade peak-profit tracking (reset on every new trade)
    trade_peak_gbp = 0.0
    trade_tp_hit   = False

    for ts, bar_5m in df_5m.iterrows():

        # ── Daily reset ────────────────────────────────────────────────────
        bar_date = ts.date()
        if bar_date != current_day:
            current_day = bar_date
            account["daily_pnl_gbp"] = 0.0
            if account["killed"] and "daily loss" in account["kill_reason"].lower():
                account["killed"]      = False
                account["kill_reason"] = ""

        bar_1h = _last_bar_at(df_1h, ts)
        bar_1d = _last_bar_at(df_1d, ts)

        if bar_1h is None:
            continue

        # ── Manage open trade ──────────────────────────────────────────────
        if current_trade is not None:
            high = float(bar_5m["high"])
            low  = float(bar_5m["low"])

            # Track best unrealized profit and whether TP level was ever touched
            if current_trade.direction == "LONG":
                unrealized = ((high - current_trade.entry_price)
                              / current_trade.entry_price
                              * current_trade.position_size_gbp)
                trade_peak_gbp = max(trade_peak_gbp, unrealized)
                if high >= current_trade.take_profit:
                    trade_tp_hit = True
            else:
                unrealized = ((current_trade.entry_price - low)
                              / current_trade.entry_price
                              * current_trade.position_size_gbp)
                trade_peak_gbp = max(trade_peak_gbp, unrealized)
                if low <= current_trade.take_profit:
                    trade_tp_hit = True

            # SL / TP only -- trend reversal blocks new entries but never closes a trade
            exit_reason = _manage_bar(current_trade, bar_5m)

            if exit_reason is not None:
                exit_px = _fill_price(current_trade, exit_reason)
                current_trade.close(exit_px, exit_reason)
                current_trade.exit_time = ts

                # Attach tracking data as extra attributes
                current_trade._peak_profit_gbp = trade_peak_gbp
                current_trade._tp_hit          = trade_tp_hit

                # Costs and net P&L
                cost = current_trade.position_size_gbp * cost_pct
                current_trade._cost_gbp = cost
                current_trade._net_pnl  = current_trade.pnl_gbp - cost

                capital                   += current_trade._net_pnl
                account["daily_pnl_gbp"]  += current_trade._net_pnl

                # Consecutive loss counter uses GROSS P&L -- fees don't count as strategy losses
                if current_trade.pnl_gbp < 0:
                    account["consecutive_losses"] += 1
                    account["last_loss_time"]      = ts.isoformat()
                else:
                    account["consecutive_losses"] = 0
                    account["last_loss_time"]     = None

                log.info(
                    "  CLOSED | %s | gross=GBP %+.2f | cost=-GBP %.2f"
                    " | net=GBP %+.2f | %s | capital=GBP %.2f",
                    current_trade.direction,
                    current_trade.pnl_gbp,
                    cost,
                    current_trade._net_pnl,
                    exit_reason,
                    capital,
                )
                completed.append(current_trade)
                current_trade  = None
                trade_peak_gbp = 0.0
                trade_tp_hit   = False

            continue   # never look for a new entry on a bar where trade is active

        # ── Entry gate ─────────────────────────────────────────────────────
        bars_checked += 1
        pre = _run_pre_checks(bar_1h, bar_5m, account, bar_1d, config)
        if not pre["passed"]:
            pre_check_blocks += 1
            continue

        score_5m = _score_bar(bar_5m)
        trend    = _combined_trend(bar_1h, bar_1d, config["min_1h"])

        entry_dir = None
        if trend == "LONG"  and score_5m["long_count"]  >= config["min_5m"]:
            entry_dir = "LONG"
        elif trend == "SHORT" and score_5m["short_count"] >= config["min_5m"]:
            entry_dir = "SHORT"

        if entry_dir is None:
            continue

        position_gbp  = capital * POSITION_SIZE_PCT
        current_trade = Trade(
            direction         = entry_dir,
            entry_price       = float(bar_5m["close"]),
            position_size_gbp = position_gbp,
            entry_time        = ts,
        )
        log.info(
            "  OPENED | %s | entry=GBP %.2f | size=GBP %.2f"
            " | stop=GBP %.2f | target=GBP %.2f",
            entry_dir,
            current_trade.entry_price,
            position_gbp,
            current_trade.stop_loss,
            current_trade.take_profit,
        )

    # ── Close any trade still open at end of data ──────────────────────────
    if current_trade is not None:
        last_close = float(df_5m.iloc[-1]["close"])
        current_trade.close(last_close, "BACKTEST_END")
        current_trade.exit_time        = df_5m.index[-1]
        current_trade._peak_profit_gbp = trade_peak_gbp
        current_trade._tp_hit          = trade_tp_hit
        cost = current_trade.position_size_gbp * cost_pct
        current_trade._cost_gbp = cost
        current_trade._net_pnl  = current_trade.pnl_gbp - cost
        capital += current_trade._net_pnl
        completed.append(current_trade)
        log.info("  Backtest end -- open trade closed at GBP %.2f", last_close)

    stats = _compute_stats(
        completed, capital, starting_capital, bars_total, bars_checked, pre_check_blocks, config
    )
    stats["kill_switch_triggered"] = account["killed"]
    stats["cost_pct"]              = cost_pct
    stats["trailing_stop_pct"]     = _ts_used
    stats["take_profit_pct"]       = _tp_used

    _save_csv(completed, config)
    _save_txt(stats, completed, df_5m, config)

    strategy_btc.TRAILING_STOP_PCT = _orig_ts
    strategy_btc.TAKE_PROFIT_PCT   = _orig_tp
    return stats


# ── Statistics ─────────────────────────────────────────────────────────────────

def _compute_stats(completed, final_capital, starting_capital, bars_total,
                   bars_checked, pre_check_blocks, config) -> dict:
    total  = len(completed)
    longs  = [t for t in completed if t.direction == "LONG"]
    shorts = [t for t in completed if t.direction == "SHORT"]

    # Use net P&L (after costs) for win/loss classification
    net_wins   = [t for t in completed if getattr(t, "_net_pnl", 0) > 0]
    net_losses = [t for t in completed if getattr(t, "_net_pnl", 0) <= 0]

    gross_pnl   = sum(t.pnl_gbp         for t in completed if t.pnl_gbp   is not None)
    total_costs = sum(getattr(t, "_cost_gbp", 0)    for t in completed)
    net_pnl     = sum(getattr(t, "_net_pnl",  0)    for t in completed)
    win_rate    = len(net_wins) / total * 100 if total else 0.0

    # Averages
    avg_net_win  = (sum(getattr(t, "_net_pnl", 0) for t in net_wins)
                    / len(net_wins) if net_wins else 0.0)
    avg_net_loss = (sum(getattr(t, "_net_pnl", 0) for t in net_losses)
                    / len(net_losses) if net_losses else 0.0)
    best_trade   = max((getattr(t, "_net_pnl", 0) for t in completed), default=0.0)
    worst_trade  = min((getattr(t, "_net_pnl", 0) for t in completed), default=0.0)

    # Break-even win rate: |avg_loss| / (avg_win + |avg_loss|)
    avg_loss_abs = abs(avg_net_loss)
    break_even   = (avg_loss_abs / (avg_net_win + avg_loss_abs) * 100
                    if (avg_net_win + avg_loss_abs) > 0 else 0.0)

    # Max consecutive losses (based on net P&L)
    max_consec = streak = 0
    for t in completed:
        if getattr(t, "_net_pnl", 0) < 0:
            streak += 1
            max_consec = max(max_consec, streak)
        else:
            streak = 0

    # Monthly net P&L
    monthly: dict = defaultdict(float)
    for t in completed:
        if t.exit_time is not None:
            monthly[t.exit_time.strftime("%Y-%m")] += getattr(t, "_net_pnl", 0)
    best_month  = max(monthly.items(), key=lambda x: x[1], default=("--", 0.0))
    worst_month = min(monthly.items(), key=lambda x: x[1], default=("--", 0.0))

    # Exit reason counts
    exit_counts: dict = defaultdict(int)
    for t in completed:
        exit_counts[t.exit_reason or "UNKNOWN"] += 1

    # Peak profit analysis -- stop-loss trades only
    sl_trades       = [t for t in completed if t.exit_reason == "STOP_LOSS"]
    avg_peak_before_sl = (
        sum(getattr(t, "_peak_profit_gbp", 0) for t in sl_trades)
        / len(sl_trades) if sl_trades else 0.0
    )
    tp_hit_before_sl = sum(1 for t in sl_trades if getattr(t, "_tp_hit", False))

    def _dir_stats(subset):
        w = [t for t in subset if getattr(t, "_net_pnl", 0) > 0]
        return {
            "count":    len(subset),
            "wins":     len(w),
            "win_rate": len(w) / len(subset) * 100 if subset else 0.0,
            "net_pnl":  sum(getattr(t, "_net_pnl", 0) for t in subset),
        }

    return {
        "config":            config,
        "bars_total":        bars_total,
        "bars_checked":      bars_checked,
        "pre_check_blocks":  pre_check_blocks,
        "total":             total,
        "wins":              len(net_wins),
        "losses":            len(net_losses),
        "win_rate":          win_rate,
        "gross_pnl":         gross_pnl,
        "total_costs":       total_costs,
        "net_pnl":           net_pnl,
        "starting_capital":  starting_capital,
        "final_capital":     final_capital,
        "total_return_pct":  (final_capital - starting_capital) / starting_capital * 100,
        "avg_net_win":       avg_net_win,
        "avg_net_loss":      avg_net_loss,
        "best_trade":        best_trade,
        "worst_trade":       worst_trade,
        "break_even_wr":     break_even,
        "max_consec_loss":   max_consec,
        "best_month":        best_month,
        "worst_month":       worst_month,
        "monthly":           dict(monthly),
        "exit_counts":       dict(exit_counts),
        "sl_count":          len(sl_trades),
        "avg_peak_before_sl": avg_peak_before_sl,
        "tp_hit_before_sl":  tp_hit_before_sl,
        "long":              _dir_stats(longs),
        "short":             _dir_stats(shorts),
    }


# ── Output: CSV ────────────────────────────────────────────────────────────────

def _save_csv(trades, config: dict) -> None:
    path = LOGS_DIR / config["csv_file"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "trade_no", "direction", "entry_time", "exit_time",
            "entry_price", "exit_price", "position_gbp",
            "gross_pnl", "cost_gbp", "net_pnl", "pnl_pct",
            "peak_profit_gbp", "tp_hit_before_stop", "exit_reason",
        ])
        for i, t in enumerate(trades, 1):
            w.writerow([
                i,
                t.direction,
                t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
                t.exit_time.strftime("%Y-%m-%d %H:%M")  if t.exit_time  else "",
                f"{t.entry_price:.2f}",
                f"{t.exit_price:.2f}" if t.exit_price is not None else "",
                f"{t.position_size_gbp:.2f}",
                f"{t.pnl_gbp:.2f}"    if t.pnl_gbp   is not None else "",
                f"{getattr(t, '_cost_gbp', 0):.2f}",
                f"{getattr(t, '_net_pnl',  0):.2f}",
                f"{t.pnl_pct * 100:.2f}%" if t.pnl_pct is not None else "",
                f"{getattr(t, '_peak_profit_gbp', 0):.2f}",
                "YES" if getattr(t, "_tp_hit", False) else "no",
                t.exit_reason or "",
            ])
    log.info("%s trade log saved -> %s", config["name"], path)


# ── Output: text results file ──────────────────────────────────────────────────

def _save_txt(stats: dict, trades, df_5m, config: dict) -> None:
    path = LOGS_DIR / config["output_file"]
    period_start = df_5m.index[0].strftime("%Y-%m-%d")  if len(df_5m) else "--"
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d") if len(df_5m) else "--"
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ec           = stats["exit_counts"]

    bar_lbl = config.get("bar_label", "5m")
    if config.get("min_5m") is not None:
        rules_desc = (
            f"min 5m indicators={config['min_5m']}"
            f"  min 1h indicators={config['min_1h']}"
            f"  quality_checks={config.get('quality_checks', False)}"
        )
    else:
        rules_desc = (
            f"min 1h indicators={config['min_1h']}"
            f"  (1h primary bar, no 5m timeframe)"
        )

    lines = [
        "=" * 64,
        f"  CryptoHybrid AI -- Backtest Results ({config['name']})",
        f"  Period  : {period_start} to {period_end}",
        f"  Rules   : {rules_desc}",
        f"  Costs   : {stats['cost_pct']*100:.2f}% per trade total"
               f"  (TS={stats['trailing_stop_pct']*100:.0f}%"
               f"  TP={stats['take_profit_pct']*100:.0f}%)",
        f"  Ran     : {now_str}",
        "=" * 64,
        "",
        "-- Overview ---------------------------------------------------",
        f"  Starting capital   : GBP {stats['starting_capital']:>10.2f}",
        f"  Final capital      : GBP {stats['final_capital']:>10.2f}",
        f"  Gross P&L          : GBP {stats['gross_pnl']:>+10.2f}",
        f"  Trading costs      : GBP {-stats['total_costs']:>+10.2f}",
        f"  Net P&L            : GBP {stats['net_pnl']:>+10.2f}",
        f"  Net return         :     {stats['total_return_pct']:>+9.2f}%",
        "",
        "-- Trade summary ----------------------------------------------",
        f"  Total trades       : {stats['total']}",
        f"  Wins (net)         : {stats['wins']}",
        f"  Losses (net)       : {stats['losses']}",
        f"  Win rate           : {stats['win_rate']:.1f}%",
        f"  Break-even win rate: {stats['break_even_wr']:.1f}%",
        f"  Avg net win        : GBP {stats['avg_net_win']:>+.2f}",
        f"  Avg net loss       : GBP {stats['avg_net_loss']:>+.2f}",
        f"  Best trade (net)   : GBP {stats['best_trade']:>+.2f}",
        f"  Worst trade (net)  : GBP {stats['worst_trade']:>+.2f}",
        f"  Max consec. losses : {stats['max_consec_loss']}",
        "",
        "-- Monthly breakdown (net P&L) --------------------------------",
        f"  Best month  : {stats['best_month'][0]}   GBP {stats['best_month'][1]:>+.2f}",
        f"  Worst month : {stats['worst_month'][0]}   GBP {stats['worst_month'][1]:>+.2f}",
        "",
        "  Month-by-month:",
    ]
    for month in sorted(stats["monthly"]):
        lines.append(f"    {month}   GBP {stats['monthly'][month]:>+8.2f}")

    lines += [
        "",
        "-- Exit reason breakdown --------------------------------------",
        f"  Stop loss          : {ec.get('STOP_LOSS', 0)}",
        f"  Take profit        : {ec.get('TAKE_PROFIT', 0)}",
        f"  Trend reversal     : {ec.get('TREND_REVERSAL', 0)}",
        f"  End of data        : {ec.get('BACKTEST_END', 0)}",
        "",
        "-- Peak profit analysis (stop-loss trades only) ---------------",
        f"  Stop-loss trades   : {stats['sl_count']}",
        f"  Avg peak profit    : GBP {stats['avg_peak_before_sl']:>+.2f}",
        f"  TP level hit first : {stats['tp_hit_before_sl']} trades"
               f"  (price reached TP then reversed to SL on same bar)",
        "",
        "-- By direction -----------------------------------------------",
        f"  LONG  : {stats['long']['count']} trades"
               f"  |  {stats['long']['wins']} wins"
               f"  |  {stats['long']['win_rate']:.1f}% win rate"
               f"  |  net GBP {stats['long']['net_pnl']:>+.2f}",
        f"  SHORT : {stats['short']['count']} trades"
               f"  |  {stats['short']['wins']} wins"
               f"  |  {stats['short']['win_rate']:.1f}% win rate"
               f"  |  net GBP {stats['short']['net_pnl']:>+.2f}",
        "",
        "-- Filter activity --------------------------------------------",
        f"  {bar_lbl} bars total          : {stats['bars_total']}",
        f"  Reached pre-check      : {stats['bars_checked']}",
        f"  Blocked by pre-checks  : {stats['pre_check_blocks']}",
        f"  Entries taken          : {stats['total']}",
        "",
        "-- Trade log --------------------------------------------------",
    ]

    for i, t in enumerate(trades, 1):
        entry_s  = t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "--"
        exit_s   = t.exit_time.strftime("%Y-%m-%d %H:%M")  if t.exit_time  else "--"
        net_s    = f"GBP {getattr(t, '_net_pnl', 0):>+.2f}"
        peak_s   = f"peak=GBP {getattr(t, '_peak_profit_gbp', 0):>+.2f}"
        tp_flag  = " [TP-HIT]" if getattr(t, "_tp_hit", False) else ""
        lines.append(
            f"  #{i:>3}  {t.direction:<5}  {entry_s} - {exit_s}"
            f"  {net_s}  {peak_s}  {t.exit_reason}{tp_flag}"
        )

    lines += ["", "=" * 64]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("%s results saved -> %s", config["name"], path)


# ── Side-by-side comparison ────────────────────────────────────────────────────

def _print_comparison(s: dict, r: dict, df_5m) -> None:
    """Print STRICT vs RELAXED comparison to terminal."""

    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    ec_s = s["exit_counts"]
    ec_r = r["exit_counts"]

    W = 16   # column width

    def row(label, sv, rv, *, fmt=None):
        if fmt:
            sv = fmt.format(sv)
            rv = fmt.format(rv)
        print(f"  {label:<38} {str(sv):>{W}} {str(rv):>{W}}")

    def rowf(label, sv, rv):
        row(label, f"GBP {sv:+.2f}", f"GBP {rv:+.2f}")

    def divider():
        print("  " + "-" * (38 + W * 2 + 2))

    sep = "=" * (40 + W * 2)
    print()
    print(sep)
    print("  BACKTEST COMPARISON: STRICT vs RELAXED")
    print(f"  Period  : {period_start} to {period_end}  ({len(df_5m):,} x 5m bars)")
    print(f"  Capital : GBP {s['starting_capital']:.2f} start  |  "
          f"Costs: {s['cost_pct']*100:.2f}% per trade (entry + exit + spread)")
    print(sep)
    print(f"  {'':38} {'STRICT':>{W}} {'RELAXED':>{W}}")
    divider()

    row("Trades taken",          s["total"],          r["total"])
    row("Win rate",              f"{s['win_rate']:.1f}%",   f"{r['win_rate']:.1f}%")
    divider()
    rowf("Gross P&L",            s["gross_pnl"],      r["gross_pnl"])
    rowf("Trading costs",        -s["total_costs"],   -r["total_costs"])
    rowf("Net P&L",              s["net_pnl"],        r["net_pnl"])
    row("Net return",            f"{s['total_return_pct']:+.2f}%",
                                 f"{r['total_return_pct']:+.2f}%")
    divider()
    row("Best month",
        f"{s['best_month'][0]} GBP {s['best_month'][1]:+.0f}",
        f"{r['best_month'][0]} GBP {r['best_month'][1]:+.0f}")
    row("Worst month",
        f"{s['worst_month'][0]} GBP {s['worst_month'][1]:+.0f}",
        f"{r['worst_month'][0]} GBP {r['worst_month'][1]:+.0f}")
    row("Max consecutive losses", s["max_consec_loss"],  r["max_consec_loss"])
    row("Break-even win rate",    f"{s['break_even_wr']:.1f}%",
                                  f"{r['break_even_wr']:.1f}%")
    divider()
    row("Exit: Stop loss",       ec_s.get("STOP_LOSS", 0),       ec_r.get("STOP_LOSS", 0))
    row("Exit: Take profit",     ec_s.get("TAKE_PROFIT", 0),     ec_r.get("TAKE_PROFIT", 0))
    row("Exit: Trend reversal",  ec_s.get("TREND_REVERSAL", 0),  ec_r.get("TREND_REVERSAL", 0))
    row("Exit: End of data",     ec_s.get("BACKTEST_END", 0),    ec_r.get("BACKTEST_END", 0))
    divider()
    row("Avg net win",           f"GBP {s['avg_net_win']:+.2f}",
                                 f"GBP {r['avg_net_win']:+.2f}")
    row("Avg net loss",          f"GBP {s['avg_net_loss']:+.2f}",
                                 f"GBP {r['avg_net_loss']:+.2f}")
    divider()
    row("LONG  trades",          s["long"]["count"],     r["long"]["count"])
    row("LONG  win rate",        f"{s['long']['win_rate']:.0f}%",
                                 f"{r['long']['win_rate']:.0f}%")
    rowf("LONG  net P&L",        s["long"]["net_pnl"],   r["long"]["net_pnl"])
    row("SHORT trades",          s["short"]["count"],    r["short"]["count"])
    row("SHORT win rate",        f"{s['short']['win_rate']:.0f}%",
                                 f"{r['short']['win_rate']:.0f}%")
    rowf("SHORT net P&L",        s["short"]["net_pnl"],  r["short"]["net_pnl"])
    divider()
    row("SL trades analysed",    s["sl_count"],          r["sl_count"])
    row("Avg peak before SL",
        f"GBP {s['avg_peak_before_sl']:+.2f}",
        f"GBP {r['avg_peak_before_sl']:+.2f}")
    row("TP hit before SL stop", s["tp_hit_before_sl"],  r["tp_hit_before_sl"])
    print(sep)
    print(f"  Full results: logs/{s['config']['output_file']}")
    print(f"                logs/{r['config']['output_file']}")
    print(f"  Trade logs  : logs/{s['config']['csv_file']}")
    print(f"                logs/{r['config']['csv_file']}")
    print(sep)


# ── Capital scaling table ──────────────────────────────────────────────────────

def _print_capital_table(results: list, df_5m) -> None:
    """Show STRICT strategy at GBP 1k / 2k / 5k / 10k side by side."""

    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")

    W = 13   # column width

    labels = [f"GBP {int(r['starting_capital']):,}" for r in results]
    n = len(results)
    sep_len = 40 + W * n
    sep = "=" * sep_len

    def divider():
        print("  " + "-" * (38 + W * n))

    def row(label, values):
        line = f"  {label:<38}"
        for v in values:
            line += f"{str(v):>{W}}"
        print(line)

    print()
    print(sep)
    print("  STRICT STRATEGY -- CAPITAL SCALING ANALYSIS")
    print(f"  Period : {period_start} to {period_end}")
    print(f"  Rules  : {CONFIGS['STRICT']['min_5m']}/6 on 5m, "
          f"{CONFIGS['STRICT']['min_1h']}/6 on 1h, all quality checks")
    print("  Note   : Kill switch daily loss limit = GBP 36 (fixed amount).")
    print("           At higher capital, each losing trade costs more in GBP,")
    print("           so the daily limit triggers sooner -- see trades taken.")
    print(sep)
    # Header row
    print(f"  {'':38}" + "".join(f"{lbl:>{W}}" for lbl in labels))
    divider()

    row("Position size (30% of capital)",
        [f"GBP {r['starting_capital'] * 0.3:,.0f}" for r in results])
    row("Avg cost per trade",
        [f"GBP {r['total_costs'] / r['total']:.2f}" if r["total"] else "n/a"
         for r in results])
    divider()
    row("Trades taken", [r["total"] for r in results])
    row("Win rate",     [f"{r['win_rate']:.1f}%" for r in results])
    divider()
    row("Gross P&L",
        [f"GBP {r['gross_pnl']:>+.2f}" for r in results])
    row("Trading costs",
        [f"GBP {-r['total_costs']:>+.2f}" for r in results])
    row("Net P&L",
        [f"GBP {r['net_pnl']:>+.2f}" for r in results])
    row("Net return",
        [f"{r['total_return_pct']:>+.2f}%" for r in results])
    divider()
    row("Max consec. losses (net)",
        [r["max_consec_loss"] for r in results])
    row("Kill switch active at end",
        ["YES" if r.get("kill_switch_triggered") else "no" for r in results])
    print(sep)
    print("  Full results: " + ", ".join(
        f"logs/{r['config']['output_file']}" for r in results))
    print(sep)


# ── Market vs Limit cost comparison ───────────────────────────────────────────

def _print_market_vs_limit(market_s: dict, limit_s: dict, df_5m) -> None:
    """Print STRICT market-order costs vs limit-order costs side by side."""
    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    ec_m = market_s["exit_counts"]
    ec_l = limit_s["exit_counts"]

    W = 16
    sep = "=" * (40 + W * 2)

    def row(label, mv, lv):
        print(f"  {label:<38} {str(mv):>{W}} {str(lv):>{W}}")

    def divider():
        print("  " + "-" * (38 + W * 2 + 2))

    print()
    print(sep)
    print("  STRICT STRATEGY -- MARKET ORDERS vs LIMIT ORDERS")
    print(f"  Period  : {period_start} to {period_end}  ({len(df_5m):,} x 5m bars)")
    print(f"  Rules   : {CONFIGS['STRICT']['min_5m']}/6 on 5m, "
          f"{CONFIGS['STRICT']['min_1h']}/6 on 1h, all quality checks")
    print(f"  Capital : GBP {market_s['starting_capital']:.2f}  |  "
          f"TS={int(market_s['trailing_stop_pct']*100)}%  "
          f"TP={int(market_s['take_profit_pct']*100)}%")
    print(sep)
    print(f"  {'':38} {'MARKET ORDERS':>{W}} {'LIMIT ORDERS':>{W}}")
    print(f"  {'':38} {'0.82%/trade':>{W}} {'0.34%/trade':>{W}}")
    divider()

    row("Trades taken",
        market_s["total"],          limit_s["total"])
    row("Win rate",
        f"{market_s['win_rate']:.1f}%",  f"{limit_s['win_rate']:.1f}%")
    divider()
    row("Gross P&L",
        f"GBP {market_s['gross_pnl']:>+.2f}",   f"GBP {limit_s['gross_pnl']:>+.2f}")
    row("Trading costs",
        f"GBP {-market_s['total_costs']:>+.2f}", f"GBP {-limit_s['total_costs']:>+.2f}")
    row("Net P&L",
        f"GBP {market_s['net_pnl']:>+.2f}",     f"GBP {limit_s['net_pnl']:>+.2f}")
    row("Net return",
        f"{market_s['total_return_pct']:>+.2f}%",
        f"{limit_s['total_return_pct']:>+.2f}%")
    divider()
    row("Exit: Stop loss",
        ec_m.get("STOP_LOSS", 0),    ec_l.get("STOP_LOSS", 0))
    row("Exit: Take profit",
        ec_m.get("TAKE_PROFIT", 0),  ec_l.get("TAKE_PROFIT", 0))
    row("Avg peak before SL",
        f"GBP {market_s['avg_peak_before_sl']:>+.2f}",
        f"GBP {limit_s['avg_peak_before_sl']:>+.2f}")
    print(sep)
    print(f"  Savings from limit orders: GBP "
          f"{limit_s['net_pnl'] - market_s['net_pnl']:>+.2f} net "
          f"({limit_s['total_return_pct'] - market_s['total_return_pct']:>+.2f}% return)")
    print(sep)


# ── TS/TP combination table ────────────────────────────────────────────────────

def _print_ts_tp_table(results: list, df_5m) -> None:
    """Print TS/TP combination analysis under limit order costs. Handles N options."""
    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    period_days  = max((df_5m.index[-1] - df_5m.index[0]).days, 1)

    _letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    option_labels = [
        f"{_letters[i]} {int(r['trailing_stop_pct']*100)}%/{int(r['take_profit_pct']*100)}%"
        for i, r in enumerate(results)
    ]

    # Column width: wide enough for data values ("GBP +54.92" = 10 chars) and labels
    W = max(max(len(l) for l in option_labels) + 1, 11)
    n = len(results)
    sep = "=" * (40 + W * n)

    def divider():
        print("  " + "-" * (38 + W * n))

    def row(label, values):
        print(f"  {label:<38}" + "".join(f"{str(v):>{W}}" for v in values))

    print()
    print(sep)
    print("  STRICT (LIMIT ORDERS) -- TRAILING STOP / TAKE PROFIT COMBINATIONS")
    print(f"  Period  : {period_start} to {period_end}  "
          f"({period_days} days, {len(df_5m):,} x 5m bars)")
    print(f"  Costs   : {LIMIT_COST_PCT*100:.2f}% per trade  "
          f"({LIMIT_ENTRY_FEE_PCT*100:.2f}% entry + "
          f"{LIMIT_EXIT_FEE_PCT*100:.2f}% exit + {SPREAD_PCT*100:.2f}% spread)")
    print(sep)
    print(f"  {'':38}" + "".join(f"{lbl:>{W}}" for lbl in option_labels))
    divider()

    row("Trades taken",  [r["total"] for r in results])
    row("Win rate",      [f"{r['win_rate']:.1f}%" for r in results])
    divider()
    row("Gross P&L",     [f"GBP {r['gross_pnl']:>+.2f}" for r in results])
    row("Trading costs", [f"GBP {-r['total_costs']:>+.2f}" for r in results])
    row("Net P&L",       [f"GBP {r['net_pnl']:>+.2f}" for r in results])
    row("Net return",    [f"{r['total_return_pct']:>+.2f}%" for r in results])
    row("Annualised est.",
        [f"{r['total_return_pct'] * 365 / period_days:>+.1f}%" for r in results])
    divider()
    row("TP exits",      [r["exit_counts"].get("TAKE_PROFIT", 0) for r in results])
    row("SL exits",      [r["exit_counts"].get("STOP_LOSS", 0)   for r in results])
    row("Avg peak bef. SL",
        [f"GBP {r['avg_peak_before_sl']:>+.2f}" for r in results])
    print(sep)

    # Optimal combination
    best     = max(results, key=lambda r: r["net_pnl"])
    best_idx = results.index(best)
    best_lbl = option_labels[best_idx]
    best_ann = best["total_return_pct"] * 365 / period_days

    print()
    print(f"  OPTIMAL COMBINATION: {best_lbl}")
    print(f"  Net P&L : GBP {best['net_pnl']:>+.2f}  "
          f"({best['total_return_pct']:>+.2f}% over {period_days} days)")
    print(f"  Annualised return estimate : {best_ann:>+.1f}%  "
          f"(x{365/period_days:.1f} extrapolation from {period_days}-day sample)")
    print(f"  Trades : {best['total']}  |  "
          f"Win rate : {best['win_rate']:.1f}%  |  "
          f"TP hits : {best['exit_counts'].get('TAKE_PROFIT', 0)}")
    print(f"  Caveat : 2-month sample. Treat as directional signal, not a guarantee.")
    print()

    # Progression analysis grouped by trailing stop size
    ts_vals = sorted({r["trailing_stop_pct"] for r in results})
    hdr_row = (f"  {'Opt':<5} {'TP':>4}  {'Trades':>6}  {'Win rate':>8}  "
               f"{'Net P&L':>10}  {'Ann.est.':>8}  {'TP hits':>7}  {'Avg peak SL':>12}")
    div68 = "  " + "-" * 68

    print()
    for ts_val in ts_vals:
        group = sorted(
            [(i, r) for i, r in enumerate(results)
             if abs(r["trailing_stop_pct"] - ts_val) < 0.001],
            key=lambda ir: ir[1]["take_profit_pct"],
        )
        print(f"  {int(ts_val*100)}% TRAILING STOP -- TP PROGRESSION")
        print(hdr_row)
        print(div68)
        for idx, r in group:
            ann = r["total_return_pct"] * 365 / period_days
            print(f"  {_letters[idx]:<5} {int(r['take_profit_pct']*100):>3}%  "
                  f"{r['total']:>6}  "
                  f"{r['win_rate']:>7.1f}%  "
                  f"  GBP {r['net_pnl']:>+7.2f}  "
                  f"{ann:>+7.1f}%  "
                  f"{r['exit_counts'].get('TAKE_PROFIT', 0):>7}  "
                  f"GBP {r['avg_peak_before_sl']:>+.2f}")
        print()

    # Best per TS level
    print(f"  BEST NET P&L BY TRAILING STOP SIZE")
    print(div68)
    best_per_ts = {}
    for ts_val in ts_vals:
        group_r  = [r for r in results if abs(r["trailing_stop_pct"] - ts_val) < 0.001]
        best_r   = max(group_r, key=lambda r: r["net_pnl"])
        best_idx = results.index(best_r)
        ann      = best_r["total_return_pct"] * 365 / period_days
        best_per_ts[ts_val] = best_r
        print(f"  {int(ts_val*100)}% TS   "
              f"Option {_letters[best_idx]}"
              f" ({int(ts_val*100)}%TS/{int(best_r['take_profit_pct']*100)}%TP)  "
              f"Net: GBP {best_r['net_pnl']:>+.2f}  ({ann:>+.1f}% ann.)")

    # 1% vs 2% verdict
    ts1_best = best_per_ts.get(0.01)
    ts2_best = best_per_ts.get(0.02)
    if ts1_best and ts2_best:
        if ts1_best["net_pnl"] >= ts2_best["net_pnl"]:
            winner, loser = "1%", "2%"
            w_r, l_r = ts1_best, ts2_best
        else:
            winner, loser = "2%", "1%"
            w_r, l_r = ts2_best, ts1_best
        diff  = abs(w_r["net_pnl"] - l_r["net_pnl"])
        w_ann = w_r["total_return_pct"] * 365 / period_days
        l_ann = l_r["total_return_pct"] * 365 / period_days
        print()
        print(f"  1% vs 2% TRAILING STOP: VERDICT")
        print(div68)
        print(f"  Winner : {winner} trailing stop")
        print(f"  Margin : GBP {diff:.2f}  "
              f"({w_ann:>+.1f}% vs {l_ann:>+.1f}% annualised)")
        if winner == "1%":
            print(f"  Why    : Tighter stop locks in gains sooner. Fewer")
            print(f"           bars spent in drawdown, higher win rate more")
            print(f"           than compensates for smaller average gain.")
        else:
            print(f"  Why    : 1% stop triggers on normal 5m noise, cutting")
            print(f"           winners before they have run. 2% gives trades")
            print(f"           room to breathe through minor retracements.")
            print(f"           The extra GBP captured per winner outweighs")
            print(f"           the slightly larger loss when the stop fires.")
    print(sep)


# ── 12-month 1h backtest runner ────────────────────────────────────────────────

def run_backtest_1h(df_1d_long, df_1h_long) -> tuple:
    """
    Replay 730 days of 1h bars under STRICT rules.
    Entry: 5/6 indicators agree on the 1h bar + daily SSL filter.
    No 5m timeframe. Limit order costs. TS=2%, TP=10% ceiling.
    Returns (stats dict, completed trades list).
    """
    CONFIG_1H = {
        "name":           "STRICT-1H-12M",
        "min_1h":         5,
        "min_5m":         None,
        "quality_checks": False,
        "output_file":    "backtest_1h_12month.txt",
        "csv_file":       "backtest_1h_12month_trades.csv",
        "bar_label":      "1h",
    }

    cost_pct          = LIMIT_COST_PCT
    trailing_stop_pct = 0.02
    take_profit_pct   = 0.10
    starting_capital  = STARTING_CAPITAL_GBP

    _orig_ts = strategy_btc.TRAILING_STOP_PCT
    _orig_tp = strategy_btc.TAKE_PROFIT_PCT
    strategy_btc.TRAILING_STOP_PCT = trailing_stop_pct
    strategy_btc.TAKE_PROFIT_PCT   = take_profit_pct

    log.info("")
    log.info("=" * 60)
    log.info("  Running STRICT-1H 12-month backtest (%d 1h bars)...", len(df_1h_long))
    log.info("=" * 60)

    capital       = starting_capital
    current_trade = None
    completed     = []

    account = {
        "killed":             False,
        "kill_reason":        "",
        "daily_pnl_gbp":      0.0,
        "consecutive_losses": 0,
        "last_loss_time":     None,
        "session_start":      datetime.now(timezone.utc).isoformat(),
    }

    current_day      = None
    bars_total       = len(df_1h_long)
    bars_checked     = 0
    pre_check_blocks = 0
    trade_peak_gbp   = 0.0
    trade_tp_hit     = False

    for ts, bar_1h in df_1h_long.iterrows():

        bar_date = ts.date()
        if bar_date != current_day:
            current_day = bar_date
            account["daily_pnl_gbp"] = 0.0
            if account["killed"] and "daily loss" in account["kill_reason"].lower():
                account["killed"]      = False
                account["kill_reason"] = ""

        bar_1d = _last_bar_at(df_1d_long, ts)

        if current_trade is not None:
            high = float(bar_1h["high"])
            low  = float(bar_1h["low"])

            if current_trade.direction == "LONG":
                unrealized = ((high - current_trade.entry_price)
                              / current_trade.entry_price
                              * current_trade.position_size_gbp)
                trade_peak_gbp = max(trade_peak_gbp, unrealized)
                if high >= current_trade.take_profit:
                    trade_tp_hit = True
            else:
                unrealized = ((current_trade.entry_price - low)
                              / current_trade.entry_price
                              * current_trade.position_size_gbp)
                trade_peak_gbp = max(trade_peak_gbp, unrealized)
                if low <= current_trade.take_profit:
                    trade_tp_hit = True

            exit_reason = _manage_bar(current_trade, bar_1h)

            if exit_reason is not None:
                exit_px = _fill_price(current_trade, exit_reason)
                current_trade.close(exit_px, exit_reason)
                current_trade.exit_time        = ts
                current_trade._peak_profit_gbp = trade_peak_gbp
                current_trade._tp_hit          = trade_tp_hit
                cost = current_trade.position_size_gbp * cost_pct
                current_trade._cost_gbp = cost
                current_trade._net_pnl  = current_trade.pnl_gbp - cost
                capital                   += current_trade._net_pnl
                account["daily_pnl_gbp"]  += current_trade._net_pnl
                if current_trade.pnl_gbp < 0:
                    account["consecutive_losses"] += 1
                    account["last_loss_time"]      = ts.isoformat()
                else:
                    account["consecutive_losses"] = 0
                    account["last_loss_time"]      = None
                log.info(
                    "  CLOSED | %s | gross=GBP %+.2f | cost=-GBP %.2f"
                    " | net=GBP %+.2f | %s | capital=GBP %.2f",
                    current_trade.direction, current_trade.pnl_gbp,
                    cost, current_trade._net_pnl, exit_reason, capital,
                )
                completed.append(current_trade)
                current_trade  = None
                trade_peak_gbp = 0.0
                trade_tp_hit   = False

            continue

        bars_checked += 1
        pre = _run_pre_checks_1h(bar_1h, account, bar_1d)
        if not pre["passed"]:
            pre_check_blocks += 1
            continue

        score     = _score_bar(bar_1h)
        daily_dir = _daily_trend(bar_1d)

        entry_dir = None
        if score["long_count"]  >= CONFIG_1H["min_1h"] and daily_dir in ("LONG",  "NEUTRAL"):
            entry_dir = "LONG"
        elif score["short_count"] >= CONFIG_1H["min_1h"] and daily_dir in ("SHORT", "NEUTRAL"):
            entry_dir = "SHORT"

        if entry_dir is None:
            continue

        position_gbp  = capital * POSITION_SIZE_PCT
        current_trade = Trade(
            direction         = entry_dir,
            entry_price       = float(bar_1h["close"]),
            position_size_gbp = position_gbp,
            entry_time        = ts,
        )
        log.info(
            "  OPENED | %s | entry=GBP %.2f | size=GBP %.2f"
            " | stop=GBP %.2f | target=GBP %.2f",
            entry_dir, current_trade.entry_price, position_gbp,
            current_trade.stop_loss, current_trade.take_profit,
        )

    if current_trade is not None:
        last_close = float(df_1h_long.iloc[-1]["close"])
        current_trade.close(last_close, "BACKTEST_END")
        current_trade.exit_time        = df_1h_long.index[-1]
        current_trade._peak_profit_gbp = trade_peak_gbp
        current_trade._tp_hit          = trade_tp_hit
        cost = current_trade.position_size_gbp * cost_pct
        current_trade._cost_gbp = cost
        current_trade._net_pnl  = current_trade.pnl_gbp - cost
        capital += current_trade._net_pnl
        completed.append(current_trade)
        log.info("  Backtest end -- open trade closed at GBP %.2f", last_close)

    stats = _compute_stats(
        completed, capital, starting_capital,
        bars_total, bars_checked, pre_check_blocks, CONFIG_1H,
    )
    stats["kill_switch_triggered"] = account["killed"]
    stats["cost_pct"]              = cost_pct
    stats["trailing_stop_pct"]     = trailing_stop_pct
    stats["take_profit_pct"]       = take_profit_pct

    _save_csv(completed, CONFIG_1H)
    _save_txt(stats, completed, df_1h_long, CONFIG_1H)

    strategy_btc.TRAILING_STOP_PCT = _orig_ts
    strategy_btc.TAKE_PROFIT_PCT   = _orig_tp
    return stats, completed


# ── 12-month 1h results printer ────────────────────────────────────────────────

def _print_1h_12month_results(stats_1h: dict, completed_1h: list, df_1h_long,
                               stats_5m_best: dict) -> None:
    """Monthly P&L breakdown for 12-month 1h backtest plus comparison vs 5m."""
    period_start = df_1h_long.index[0].strftime("%Y-%m-%d")
    period_end   = df_1h_long.index[-1].strftime("%Y-%m-%d")
    period_days  = max((df_1h_long.index[-1] - df_1h_long.index[0]).days, 1)
    ann_1h       = stats_1h["total_return_pct"] * 365 / period_days

    # Per-month stats from trade list
    monthly_detail = defaultdict(lambda: {"trades": 0, "wins": 0, "net_pnl": 0.0})
    for t in completed_1h:
        if t.exit_time is not None:
            m   = t.exit_time.strftime("%Y-%m")
            net = getattr(t, "_net_pnl", 0)
            monthly_detail[m]["trades"] += 1
            monthly_detail[m]["net_pnl"] += net
            if net > 0:
                monthly_detail[m]["wins"] += 1

    sep = "=" * 72

    print()
    print(sep)
    print("  STRICT 12-MONTH HOURLY BACKTEST  (1h bars, limit orders)")
    print(f"  Period  : {period_start} to {period_end}"
          f"  ({period_days} days, {len(df_1h_long):,} x 1h bars)")
    print(f"  Rules   : 5/6 indicators on 1h bar  |  daily SSL filter"
          f"  |  no 5m timeframe")
    print(f"  Costs   : {LIMIT_COST_PCT*100:.2f}% per trade  "
          f"|  TS=2%  TP=10% ceiling  |  GBP {stats_1h['starting_capital']:.2f} start")
    print(sep)

    print(f"  {'Month':<9}  {'Trades':>6}  {'Win%':>5}  "
          f"{'Net P&L':>10}  {'Cumulative':>11}")
    print("  " + "-" * 50)
    cumulative = 0.0
    for month in sorted(monthly_detail):
        md  = monthly_detail[month]
        cumulative += md["net_pnl"]
        wr  = md["wins"] / md["trades"] * 100 if md["trades"] > 0 else 0.0
        tag = "+" if md["net_pnl"] >= 0 else "-"
        print(f"  {month}   {md['trades']:>6}  {wr:>4.0f}%"
              f"  GBP {md['net_pnl']:>+8.2f}"
              f"  GBP {cumulative:>+9.2f}  [{tag}]")
    print("  " + "-" * 50)
    print(f"  {'TOTAL':<9}  {stats_1h['total']:>6}  "
          f"{stats_1h['win_rate']:>4.0f}%"
          f"  GBP {stats_1h['net_pnl']:>+8.2f}"
          f"  GBP {stats_1h['net_pnl']:>+9.2f}")

    all_months  = sorted(monthly_detail)
    prof_months = sum(1 for m in all_months if monthly_detail[m]["net_pnl"] >= 0)
    print()
    print(f"  Profitable months : {prof_months} / {len(all_months)}"
          f"   |   Loss months : {len(all_months) - prof_months} / {len(all_months)}")

    ec = stats_1h["exit_counts"]
    print()
    print("  " + "-" * 50)
    print(f"  Gross P&L          : GBP {stats_1h['gross_pnl']:>+.2f}")
    print(f"  Trading costs      : GBP {-stats_1h['total_costs']:>+.2f}")
    print(f"  Net P&L            : GBP {stats_1h['net_pnl']:>+.2f}")
    print(f"  Net return         : {stats_1h['total_return_pct']:>+.2f}%  "
          f"over {period_days} days")
    print(f"  Annualised est.    : {ann_1h:>+.1f}%")
    print(f"  Win rate           : {stats_1h['win_rate']:.1f}%")
    print(f"  Max consec. losses : {stats_1h['max_consec_loss']}")
    print(f"  Stop loss exits    : {ec.get('STOP_LOSS', 0)}")
    print(f"  Take profit exits  : {ec.get('TAKE_PROFIT', 0)}")
    print(f"  Avg peak before SL : GBP {stats_1h['avg_peak_before_sl']:>+.2f}")
    print(f"  LONG  trades       : {stats_1h['long']['count']}"
          f"  |  {stats_1h['long']['win_rate']:.0f}% win rate"
          f"  |  net GBP {stats_1h['long']['net_pnl']:>+.2f}")
    print(f"  SHORT trades       : {stats_1h['short']['count']}"
          f"  |  {stats_1h['short']['win_rate']:.0f}% win rate"
          f"  |  net GBP {stats_1h['short']['net_pnl']:>+.2f}")

    # Compare with 5m optimal (Option F: 2%TS/10%TP, 59-day run)
    days_5m = max((stats_5m_best.get("bars_total", 1) // 288), 1)  # approx from 5m bars
    # Use the stored period_days from the 5m backtest if available, else fall back
    ann_5m  = stats_5m_best["total_return_pct"] * 365 / 59   # 59-day sample

    W = 18
    def row2(label, v1, v2):
        print(f"  {label:<38} {str(v1):>{W}} {str(v2):>{W}}")
    def div2():
        print("  " + "-" * (40 + W * 2))

    ec5 = stats_5m_best["exit_counts"]
    print()
    print(sep)
    print("  12-MONTH 1h  vs  60-DAY 5m"
          "  (both: STRICT, limit orders, 2% TS / 10% TP)")
    print(sep)
    print(f"  {'':38} {'1h / 730 days':>{W}} {'5m / 59 days':>{W}}")
    div2()
    row2("Trades taken",          stats_1h["total"],       stats_5m_best["total"])
    row2("Win rate",              f"{stats_1h['win_rate']:.1f}%",
                                  f"{stats_5m_best['win_rate']:.1f}%")
    div2()
    row2("Gross P&L",             f"GBP {stats_1h['gross_pnl']:>+.2f}",
                                  f"GBP {stats_5m_best['gross_pnl']:>+.2f}")
    row2("Trading costs",         f"GBP {-stats_1h['total_costs']:>+.2f}",
                                  f"GBP {-stats_5m_best['total_costs']:>+.2f}")
    row2("Net P&L",               f"GBP {stats_1h['net_pnl']:>+.2f}",
                                  f"GBP {stats_5m_best['net_pnl']:>+.2f}")
    row2("Net return",            f"{stats_1h['total_return_pct']:>+.2f}%",
                                  f"{stats_5m_best['total_return_pct']:>+.2f}%")
    row2("Annualised estimate",   f"{ann_1h:>+.1f}%",   f"{ann_5m:>+.1f}%")
    div2()
    row2("Exit: Stop loss",       ec.get("STOP_LOSS", 0),
                                  ec5.get("STOP_LOSS", 0))
    row2("Exit: Take profit",     ec.get("TAKE_PROFIT", 0),
                                  ec5.get("TAKE_PROFIT", 0))
    row2("Avg peak before SL",    f"GBP {stats_1h['avg_peak_before_sl']:>+.2f}",
                                  f"GBP {stats_5m_best['avg_peak_before_sl']:>+.2f}")
    row2("Max consec. losses",    stats_1h["max_consec_loss"],
                                  stats_5m_best["max_consec_loss"])
    print(sep)
    print(f"  Full results : logs/{stats_1h['config']['output_file']}")
    print(f"  Trade log    : logs/{stats_1h['config']['csv_file']}")
    print(sep)


# ── Entry point ────────────────────────────────────────────────────────────────

CAPITAL_LEVELS = [2_000.0, 5_000.0, 10_000.0]


def run_backtest() -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    df_1d, df_1h, df_5m = fetch_historical_data()

    strict_stats  = run_single_backtest(CONFIGS["STRICT"],  df_1d, df_1h, df_5m)
    relaxed_stats = run_single_backtest(CONFIGS["RELAXED"], df_1d, df_1h, df_5m)

    _print_comparison(strict_stats, relaxed_stats, df_5m)

    # STRICT at higher capital levels -- shows how costs and kill switch scale
    scaling_stats = [strict_stats]
    for cap in CAPITAL_LEVELS:
        label = f"{int(cap // 1000)}k"
        cfg = {
            **CONFIGS["STRICT"],
            "name":        f"STRICT-{label}",
            "output_file": f"backtest_strict_{label}.txt",
            "csv_file":    f"backtest_strict_{label}_trades.csv",
        }
        scaling_stats.append(
            run_single_backtest(cfg, df_1d, df_1h, df_5m, starting_capital=cap)
        )
    _print_capital_table(scaling_stats, df_5m)

    # ── Improvement 1: Market vs Limit order costs ─────────────────────────────
    limit_a_stats = run_single_backtest(
        {**CONFIGS["STRICT"],
         "name":        "STRICT-LIMIT",
         "output_file": "backtest_strict_limit.txt",
         "csv_file":    "backtest_strict_limit_trades.csv"},
        df_1d, df_1h, df_5m,
        cost_pct=LIMIT_COST_PCT,
    )
    _print_market_vs_limit(strict_stats, limit_a_stats, df_5m)

    # ── Improvement 2: TS/TP combinations under limit order costs ──────────────
    TS_TP_OPTIONS = [
        {"ts": 0.03, "tp": 0.06, "slug": "b"},   # Option B
        {"ts": 0.02, "tp": 0.06, "slug": "c"},   # Option C
        {"ts": 0.03, "tp": 0.04, "slug": "d"},   # Option D
        {"ts": 0.02, "tp": 0.08, "slug": "e"},   # Option E
        {"ts": 0.02, "tp": 0.10, "slug": "f"},   # Option F
        {"ts": 0.02, "tp": 0.12, "slug": "g"},   # Option G
        {"ts": 0.01, "tp": 0.10, "slug": "h"},   # Option H
    ]
    ts_tp_results = [limit_a_stats]  # Option A already computed above
    for opt in TS_TP_OPTIONS:
        ts_tp_results.append(run_single_backtest(
            {**CONFIGS["STRICT"],
             "name":        f"STRICT-LMT-{opt['slug'].upper()}",
             "output_file": f"backtest_strict_lmt_{opt['slug']}.txt",
             "csv_file":    f"backtest_strict_lmt_{opt['slug']}_trades.csv"},
            df_1d, df_1h, df_5m,
            cost_pct=LIMIT_COST_PCT,
            trailing_stop_pct=opt["ts"],
            take_profit_pct=opt["tp"],
        ))
    _print_ts_tp_table(ts_tp_results, df_5m)

    # ── 12-month 1h backtest ───────────────────────────────────────────────────
    optimal_5m = max(ts_tp_results, key=lambda r: r["net_pnl"])
    df_1d_long, df_1h_long = fetch_historical_data_1h()
    stats_1h, completed_1h = run_backtest_1h(df_1d_long, df_1h_long)
    _print_1h_12month_results(stats_1h, completed_1h, df_1h_long, optimal_5m)


if __name__ == "__main__":
    run_backtest()
