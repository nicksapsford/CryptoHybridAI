"""
CryptoHybrid AI -- backtest_eth_gbp.py
ETH/GBP backtest using the same proven strategy settings as BTC/GBP.
Runs STRICT + RELAXED on ETH, also re-runs BTC STRICT for same-period comparison.

Goal: answer whether ETH/GBP fires more frequently and whether profitability holds.
"""

import io
import sys
import logging
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

# Force UTF-8 on Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── CryptoHybrid AI imports ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from data_feed_btc import add_indicators
import strategy_btc
from strategy_btc import _score_bar, Trade, POSITION_SIZE_PCT

# Import all the reusable backtest infrastructure from backtest_btc
from backtest_btc import (
    LIMIT_COST_PCT,
    STARTING_CAPITAL_GBP,
    _last_bar_at,
    _1h_trend,
    _daily_trend,
    _combined_trend,
    _run_pre_checks,
    _manage_bar,
    _fill_price,
    _compute_stats,
    _save_csv,
    _save_txt,
    run_single_backtest,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("CryptoHybrid.ETH.Backtest")

logging.getLogger("CryptoHybrid.PreChecks").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.Strategy").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.DataFeed").setLevel(logging.WARNING)

# ── Constants ──────────────────────────────────────────────────────────────────
LOGS_DIR      = Path(__file__).parent / "logs"
ETH_TICKER    = "ETH-GBP"
BTC_TICKER    = "BTC-GBP"
TRAILING_STOP = 0.02    # 2%  — proven optimal
TAKE_PROFIT   = 0.10    # 10% — ceiling

ETH_CONFIGS = {
    "ETH_STRICT": {
        "name":           "ETH-STRICT",
        "min_5m":         5,
        "min_1h":         4,
        "quality_checks": True,
        "output_file":    "backtest_eth_strict.txt",
        "csv_file":       "backtest_eth_strict_trades.csv",
    },
    "ETH_RELAXED": {
        "name":           "ETH-RELAXED",
        "min_5m":         4,
        "min_1h":         3,
        "quality_checks": False,
        "output_file":    "backtest_eth_relaxed.txt",
        "csv_file":       "backtest_eth_relaxed_trades.csv",
    },
}

BTC_CONFIG_STRICT = {
    "name":           "BTC-STRICT",
    "min_5m":         5,
    "min_1h":         4,
    "quality_checks": True,
    "output_file":    "backtest_btc_strict_comparison.txt",
    "csv_file":       "backtest_btc_strict_comparison_trades.csv",
}


# ── Data fetching ──────────────────────────────────────────────────────────────

def _download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Download from Yahoo Finance and normalise columns/timezone."""
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker} ({interval}, {period})")
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


def fetch_eth_data() -> tuple:
    """Fetch ETH-GBP 1d / 1h / 5m with indicators."""
    log.info("Fetching ETH/GBP historical data from Yahoo Finance...")
    df_1d = add_indicators(_download(ETH_TICKER, "1d", "2y"))
    log.info("  ETH 1d : %d candles  [%s -> %s]",
             len(df_1d),
             df_1d.index[0].strftime("%Y-%m-%d"),
             df_1d.index[-1].strftime("%Y-%m-%d"))

    df_1h = add_indicators(_download(ETH_TICKER, "1h", "90d"))
    log.info("  ETH 1h : %d candles  [%s -> %s]",
             len(df_1h),
             df_1h.index[0].strftime("%Y-%m-%d %H:%M"),
             df_1h.index[-1].strftime("%Y-%m-%d %H:%M"))

    df_5m = add_indicators(_download(ETH_TICKER, "5m", "60d"))
    log.info("  ETH 5m : %d candles  [%s -> %s]",
             len(df_5m),
             df_5m.index[0].strftime("%Y-%m-%d %H:%M"),
             df_5m.index[-1].strftime("%Y-%m-%d %H:%M"))

    return df_1d, df_1h, df_5m


def fetch_btc_data() -> tuple:
    """Fetch BTC-GBP 1d / 1h / 5m with indicators (same period as ETH)."""
    log.info("Fetching BTC/GBP historical data from Yahoo Finance (same period)...")
    df_1d = add_indicators(_download(BTC_TICKER, "1d", "2y"))
    log.info("  BTC 1d : %d candles", len(df_1d))

    df_1h = add_indicators(_download(BTC_TICKER, "1h", "90d"))
    log.info("  BTC 1h : %d candles", len(df_1h))

    df_5m = add_indicators(_download(BTC_TICKER, "5m", "60d"))
    log.info("  BTC 5m : %d candles", len(df_5m))

    return df_1d, df_1h, df_5m


# ── 3-way comparison table ─────────────────────────────────────────────────────

def _three_way_comparison(btc_s: dict, eth_s: dict, eth_r: dict,
                          df_btc_5m, df_eth_5m) -> str:
    """Build the 3-way comparison table as a string."""

    def period_days(df):
        return max((df.index[-1] - df.index[0]).days, 1)

    btc_days = period_days(df_btc_5m)
    eth_days = period_days(df_eth_5m)
    btc_weeks = btc_days / 7
    eth_weeks = eth_days / 7

    # Trades per week
    btc_tpw = btc_s["total"] / btc_weeks if btc_weeks else 0
    eth_s_tpw = eth_s["total"] / eth_weeks if eth_weeks else 0
    eth_r_tpw = eth_r["total"] / eth_weeks if eth_weeks else 0

    # Annualised
    btc_ann  = btc_s["total_return_pct"] * 365 / btc_days
    eth_s_ann = eth_s["total_return_pct"] * 365 / eth_days
    eth_r_ann = eth_r["total_return_pct"] * 365 / eth_days

    btc_ec  = btc_s["exit_counts"]
    eth_s_ec = eth_s["exit_counts"]
    eth_r_ec = eth_r["exit_counts"]

    W = 17
    sep = "=" * (40 + W * 3)

    lines = []

    def row(label, v1, v2, v3):
        lines.append(f"  {label:<38} {str(v1):>{W}} {str(v2):>{W}} {str(v3):>{W}}")

    def divider():
        lines.append("  " + "-" * (38 + W * 3 + 2))

    lines.append("")
    lines.append(sep)
    lines.append("  BTC/GBP STRICT  vs  ETH/GBP STRICT  vs  ETH/GBP RELAXED")
    lines.append(f"  BTC period : {df_btc_5m.index[0].strftime('%Y-%m-%d')} to "
                 f"{df_btc_5m.index[-1].strftime('%Y-%m-%d')}  ({btc_days} days)")
    lines.append(f"  ETH period : {df_eth_5m.index[0].strftime('%Y-%m-%d')} to "
                 f"{df_eth_5m.index[-1].strftime('%Y-%m-%d')}  ({eth_days} days)")
    lines.append(f"  Capital : GBP {STARTING_CAPITAL_GBP:.2f} | "
                 f"TS=2% | TP=10% | Costs: {LIMIT_COST_PCT*100:.2f}%/trade")
    lines.append(sep)
    lines.append(f"  {'':38} {'BTC STRICT':>{W}} {'ETH STRICT':>{W}} {'ETH RELAXED':>{W}}")
    divider()

    row("Trades taken",
        btc_s["total"], eth_s["total"], eth_r["total"])
    row("Avg trades / week",
        f"{btc_tpw:.1f}", f"{eth_s_tpw:.1f}", f"{eth_r_tpw:.1f}")
    row("Win rate",
        f"{btc_s['win_rate']:.1f}%",
        f"{eth_s['win_rate']:.1f}%",
        f"{eth_r['win_rate']:.1f}%")
    divider()

    row("Gross P&L",
        f"GBP {btc_s['gross_pnl']:>+.2f}",
        f"GBP {eth_s['gross_pnl']:>+.2f}",
        f"GBP {eth_r['gross_pnl']:>+.2f}")
    row("Trading costs",
        f"GBP {-btc_s['total_costs']:>+.2f}",
        f"GBP {-eth_s['total_costs']:>+.2f}",
        f"GBP {-eth_r['total_costs']:>+.2f}")
    row("Net P&L",
        f"GBP {btc_s['net_pnl']:>+.2f}",
        f"GBP {eth_s['net_pnl']:>+.2f}",
        f"GBP {eth_r['net_pnl']:>+.2f}")
    row("Net return %",
        f"{btc_s['total_return_pct']:>+.2f}%",
        f"{eth_s['total_return_pct']:>+.2f}%",
        f"{eth_r['total_return_pct']:>+.2f}%")
    row("Annualised estimate",
        f"{btc_ann:>+.1f}%",
        f"{eth_s_ann:>+.1f}%",
        f"{eth_r_ann:>+.1f}%")
    divider()

    row("Max consecutive losses",
        btc_s["max_consec_loss"],
        eth_s["max_consec_loss"],
        eth_r["max_consec_loss"])
    row("Best month",
        f"{btc_s['best_month'][0]} £{btc_s['best_month'][1]:>+.0f}",
        f"{eth_s['best_month'][0]} £{eth_s['best_month'][1]:>+.0f}",
        f"{eth_r['best_month'][0]} £{eth_r['best_month'][1]:>+.0f}")
    row("Worst month",
        f"{btc_s['worst_month'][0]} £{btc_s['worst_month'][1]:>+.0f}",
        f"{eth_s['worst_month'][0]} £{eth_s['worst_month'][1]:>+.0f}",
        f"{eth_r['worst_month'][0]} £{eth_r['worst_month'][1]:>+.0f}")
    divider()

    row("Exit: Stop loss",
        btc_ec.get("STOP_LOSS", 0),
        eth_s_ec.get("STOP_LOSS", 0),
        eth_r_ec.get("STOP_LOSS", 0))
    row("Exit: Take profit",
        btc_ec.get("TAKE_PROFIT", 0),
        eth_s_ec.get("TAKE_PROFIT", 0),
        eth_r_ec.get("TAKE_PROFIT", 0))
    row("Exit: End of data",
        btc_ec.get("BACKTEST_END", 0),
        eth_s_ec.get("BACKTEST_END", 0),
        eth_r_ec.get("BACKTEST_END", 0))
    divider()

    row("Avg net win",
        f"GBP {btc_s['avg_net_win']:>+.2f}",
        f"GBP {eth_s['avg_net_win']:>+.2f}",
        f"GBP {eth_r['avg_net_win']:>+.2f}")
    row("Avg net loss",
        f"GBP {btc_s['avg_net_loss']:>+.2f}",
        f"GBP {eth_s['avg_net_loss']:>+.2f}",
        f"GBP {eth_r['avg_net_loss']:>+.2f}")
    row("Break-even win rate",
        f"{btc_s['break_even_wr']:.1f}%",
        f"{eth_s['break_even_wr']:.1f}%",
        f"{eth_r['break_even_wr']:.1f}%")
    divider()

    row("LONG  trades",
        btc_s["long"]["count"],
        eth_s["long"]["count"],
        eth_r["long"]["count"])
    row("LONG  win rate",
        f"{btc_s['long']['win_rate']:.0f}%",
        f"{eth_s['long']['win_rate']:.0f}%",
        f"{eth_r['long']['win_rate']:.0f}%")
    row("LONG  net P&L",
        f"GBP {btc_s['long']['net_pnl']:>+.2f}",
        f"GBP {eth_s['long']['net_pnl']:>+.2f}",
        f"GBP {eth_r['long']['net_pnl']:>+.2f}")
    row("SHORT trades",
        btc_s["short"]["count"],
        eth_s["short"]["count"],
        eth_r["short"]["count"])
    row("SHORT win rate",
        f"{btc_s['short']['win_rate']:.0f}%",
        f"{eth_s['short']['win_rate']:.0f}%",
        f"{eth_r['short']['win_rate']:.0f}%")
    row("SHORT net P&L",
        f"GBP {btc_s['short']['net_pnl']:>+.2f}",
        f"GBP {eth_s['short']['net_pnl']:>+.2f}",
        f"GBP {eth_r['short']['net_pnl']:>+.2f}")
    divider()

    row("Avg peak before SL",
        f"GBP {btc_s['avg_peak_before_sl']:>+.2f}",
        f"GBP {eth_s['avg_peak_before_sl']:>+.2f}",
        f"GBP {eth_r['avg_peak_before_sl']:>+.2f}")

    lines.append(sep)
    return "\n".join(lines)


# ── Monthly breakdown ──────────────────────────────────────────────────────────

def _monthly_table(stats: dict, label: str) -> str:
    lines = []
    lines.append(f"\n  Monthly net P&L breakdown -- {label}")
    lines.append(f"  {'Month':<9}  {'Trades':>6}  {'WR':>5}  {'Net P&L':>10}  {'Cumulative':>11}")
    lines.append("  " + "-" * 50)

    # Rebuild from monthly dict (only totals; we need per-month trade counts from stats)
    monthly = stats.get("monthly", {})
    cumulative = 0.0
    for month in sorted(monthly):
        pnl = monthly[month]
        cumulative += pnl
        sign = "+" if pnl >= 0 else "-"
        lines.append(f"  {month}   {'?':>6}  {'?':>4}%  GBP {pnl:>+8.2f}"
                     f"  GBP {cumulative:>+9.2f}  [{sign}]")

    lines.append("  " + "-" * 50)
    lines.append(f"  {'TOTAL':<9}  {stats['total']:>6}  "
                 f"{stats['win_rate']:>4.0f}%"
                 f"  GBP {stats['net_pnl']:>+8.2f}"
                 f"  GBP {stats['net_pnl']:>+9.2f}")
    return "\n".join(lines)


# ── Q&A verdict section ────────────────────────────────────────────────────────

def _verdict(btc_s: dict, eth_s: dict, eth_r: dict,
             df_btc_5m, df_eth_5m) -> str:

    def days(df):
        return max((df.index[-1] - df.index[0]).days, 1)

    btc_days  = days(df_btc_5m)
    eth_days  = days(df_eth_5m)
    btc_weeks = btc_days / 7
    eth_weeks = eth_days / 7

    btc_tpw   = btc_s["total"] / btc_weeks if btc_weeks else 0
    eth_s_tpw = eth_s["total"] / eth_weeks if eth_weeks else 0
    eth_r_tpw = eth_r["total"] / eth_weeks if eth_weeks else 0

    btc_ann   = btc_s["total_return_pct"] * 365 / btc_days
    eth_s_ann = eth_s["total_return_pct"] * 365 / eth_days
    eth_r_ann = eth_r["total_return_pct"] * 365 / eth_days

    freq_multiplier = eth_s_tpw / btc_tpw if btc_tpw > 0 else 0
    eth_more_freq   = freq_multiplier > 1.2   # >20% more frequent
    eth_wr_degrades = eth_s["win_rate"] < btc_s["win_rate"] - 5
    eth_profitable  = eth_s["net_pnl"] > 0

    W = 72
    lines = []
    sep = "=" * W

    lines.append("")
    lines.append(sep)
    lines.append("  PART 5 -- KEY QUESTIONS ANSWERED")
    lines.append(sep)

    # Q1
    lines.append("")
    lines.append("  Q1. Does ETH/GBP generate significantly more trade signals")
    lines.append("      than BTC/GBP with the same STRICT rules?")
    lines.append("")
    lines.append(f"  BTC STRICT : {btc_s['total']} trades over {btc_days} days"
                 f" = {btc_tpw:.1f} trades/week")
    lines.append(f"  ETH STRICT : {eth_s['total']} trades over {eth_days} days"
                 f" = {eth_s_tpw:.1f} trades/week")
    lines.append(f"  Frequency multiplier : {freq_multiplier:.2f}x")
    lines.append("")
    if eth_more_freq:
        lines.append(f"  ANSWER: YES -- ETH fires {freq_multiplier:.1f}x more frequently than BTC")
        lines.append("          under identical STRICT rules. ETH/GBP is the higher-activity")
        lines.append("          pair for this strategy.")
    else:
        lines.append(f"  ANSWER: NO -- ETH fires at roughly the same rate as BTC")
        lines.append(f"          ({freq_multiplier:.2f}x). The strategy frequency is similar.")

    # Q2
    lines.append("")
    lines.append("  Q2. Does the win rate and profitability hold up on ETH?")
    lines.append("")
    lines.append(f"  BTC STRICT win rate : {btc_s['win_rate']:.1f}%"
                 f"  |  net P&L: GBP {btc_s['net_pnl']:>+.2f}"
                 f"  ({btc_s['total_return_pct']:>+.2f}%  /  {btc_ann:>+.1f}% ann.)")
    lines.append(f"  ETH STRICT win rate : {eth_s['win_rate']:.1f}%"
                 f"  |  net P&L: GBP {eth_s['net_pnl']:>+.2f}"
                 f"  ({eth_s['total_return_pct']:>+.2f}%  /  {eth_s_ann:>+.1f}% ann.)")
    lines.append("")
    if eth_profitable and not eth_wr_degrades:
        lines.append("  ANSWER: YES -- Win rate holds up and ETH is profitable.")
        lines.append("          Strategy transfers well to ETH/GBP.")
    elif eth_profitable and eth_wr_degrades:
        lines.append("  ANSWER: PARTIALLY -- ETH is profitable but win rate is lower.")
        wr_diff = btc_s["win_rate"] - eth_s["win_rate"]
        lines.append(f"          Win rate drops {wr_diff:.1f}pp vs BTC. More frequent signals")
        lines.append("          but slightly lower quality on average.")
    else:
        lines.append("  ANSWER: NO -- ETH/GBP STRICT is not profitable in this sample.")
        lines.append("          The strategy does not transfer cleanly to ETH.")

    # Q3
    lines.append("")
    lines.append("  Q3. Trades per week: ETH STRICT vs BTC STRICT?")
    lines.append("      (Dedicated PC economics need a minimum signal frequency)")
    lines.append("")
    lines.append(f"  BTC STRICT  : {btc_tpw:.1f} trades/week  ({btc_s['total']} over {btc_days} days)")
    lines.append(f"  ETH STRICT  : {eth_s_tpw:.1f} trades/week  ({eth_s['total']} over {eth_days} days)")
    lines.append(f"  ETH RELAXED : {eth_r_tpw:.1f} trades/week  ({eth_r['total']} over {eth_days} days)")
    lines.append("")
    if eth_s_tpw >= 3:
        lines.append(f"  ANSWER: ETH STRICT at {eth_s_tpw:.1f}/week is viable for a dedicated")
        lines.append("          system. Running costs are justified at this frequency.")
    elif eth_s_tpw >= 1:
        lines.append(f"  ANSWER: ETH STRICT at {eth_s_tpw:.1f}/week is marginal. Combined")
        lines.append("          BTC+ETH on the same system would improve economics.")
    else:
        lines.append(f"  ANSWER: ETH STRICT at {eth_s_tpw:.1f}/week is too infrequent alone.")
        lines.append("          ETH RELAXED at {eth_r_tpw:.1f}/week is the better choice,")
        lines.append("          or combine BTC+ETH.")

    # Q4
    lines.append("")
    lines.append("  Q4. Is ETH/GBP RELAXED worth considering?")
    lines.append("")
    lines.append(f"  ETH STRICT  : {eth_s['total']} trades | {eth_s['win_rate']:.1f}% WR"
                 f" | net GBP {eth_s['net_pnl']:>+.2f} | {eth_s_ann:>+.1f}% ann.")
    lines.append(f"  ETH RELAXED : {eth_r['total']} trades | {eth_r['win_rate']:.1f}% WR"
                 f" | net GBP {eth_r['net_pnl']:>+.2f} | {eth_r_ann:>+.1f}% ann.")
    lines.append("")
    wr_drop = eth_s["win_rate"] - eth_r["win_rate"]
    pnl_diff = eth_r["net_pnl"] - eth_s["net_pnl"]
    trade_uplift = eth_r["total"] - eth_s["total"]
    if eth_r["net_pnl"] > eth_s["net_pnl"] and eth_r["net_pnl"] > 0:
        lines.append(f"  ANSWER: YES -- RELAXED adds {trade_uplift} more trades"
                     f" (+GBP {pnl_diff:.2f} net vs STRICT)")
        lines.append(f"          Win rate drops {wr_drop:.1f}pp but volume compensates.")
        lines.append("          RELAXED rules are worth using on ETH/GBP.")
    elif eth_r["net_pnl"] > 0 and eth_r["win_rate"] > 40:
        lines.append(f"  ANSWER: MARGINAL -- RELAXED is profitable but win rate drops")
        lines.append(f"          {wr_drop:.1f}pp vs STRICT. The extra trades do not")
        lines.append("          fully compensate for lower quality. Use with caution.")
    else:
        lines.append(f"  ANSWER: NO -- RELAXED win rate drops {wr_drop:.1f}pp and")
        lines.append("          does not produce better absolute returns.")
        lines.append("          Stick with STRICT rules on ETH/GBP if using it at all.")

    # Q5 — plain English verdict
    lines.append("")
    lines.append("  Q5. PLAIN ENGLISH VERDICT")
    lines.append("      Should we add ETH/GBP alongside BTC, replace BTC, or")
    lines.append("      stick with BTC only?")
    lines.append("")

    # Decision logic
    eth_s_beats_btc_ann = eth_s_ann > btc_ann
    eth_positive        = eth_s["net_pnl"] > 0

    if eth_positive and eth_s["win_rate"] >= 45:
        if eth_more_freq:
            lines.append("  VERDICT: ADD ETH/GBP ALONGSIDE BTC/GBP")
            lines.append("")
            lines.append(f"  ETH fires {freq_multiplier:.1f}x more frequently than BTC under")
            lines.append("  the same STRICT rules and is profitable in this sample.")
            lines.append("  Running both pairs on the same system improves capital")
            lines.append("  utilisation — the system sits idle less often.")
            lines.append("")
            lines.append("  How to implement:")
            lines.append("  - Run CryptoHybrid AI in parallel on ETH-GBP (separate")
            lines.append("    process, same capital, same pre-checks).")
            lines.append("  - Do NOT increase position size — treat each pair as an")
            lines.append("    independent strategy with its own risk budget.")
            lines.append("  - Monitor for 4+ more weeks before live capital.")
        else:
            lines.append("  VERDICT: STICK WITH BTC/GBP ONLY (for now)")
            lines.append("")
            lines.append("  ETH is profitable but doesn't fire significantly more")
            lines.append("  often than BTC. Adding it doubles implementation effort")
            lines.append("  without meaningfully improving trade frequency.")
            lines.append("  Revisit if ETH volatility picks up.")
    elif eth_positive and eth_s["win_rate"] < 45:
        lines.append("  VERDICT: STICK WITH BTC/GBP ONLY")
        lines.append("")
        lines.append(f"  ETH/GBP STRICT win rate ({eth_s['win_rate']:.1f}%) is below")
        lines.append("  the 45% minimum threshold for comfortable live trading.")
        lines.append("  The break-even win rate for this strategy is ~40%, so")
        lines.append(f"  {eth_s['win_rate']:.1f}% provides a thin margin that could easily")
        lines.append("  flip negative in a different market regime.")
        lines.append("  Accumulate more data on ETH before committing.")
    else:
        lines.append("  VERDICT: STICK WITH BTC/GBP ONLY")
        lines.append("")
        lines.append("  ETH/GBP is not profitable in this sample under STRICT rules.")
        lines.append("  The CryptoHybrid AI strategy was calibrated on BTC/GBP and")
        lines.append("  does not transfer cleanly to ETH in the current market.")
        lines.append("  Do not add ETH until the strategy shows positive expectancy")
        lines.append("  over at least 50 trades on ETH/GBP paper trading.")

    lines.append("")
    lines.append("  CAVEATS:")
    lines.append(f"  - This is a {eth_days}-day backtest sample. Results may not")
    lines.append("    persist in a different market regime.")
    lines.append("  - Indicators were computed identically for both pairs.")
    lines.append("    ETH/GBP is more volatile than BTC/GBP — the 2% trailing")
    lines.append("    stop may benefit from a slightly wider setting on ETH.")
    lines.append("  - No slippage or failed limit fill modelling is included.")
    lines.append("  - Paper trade ETH/GBP for 4+ weeks before any live capital.")
    lines.append("")
    lines.append(sep)

    return "\n".join(lines)


# ── Save combined report ───────────────────────────────────────────────────────

def _save_combined_report(comparison_table: str, verdict: str,
                           btc_stats: dict, eth_s_stats: dict, eth_r_stats: dict,
                           df_btc_5m, df_eth_5m) -> None:
    """Save the full combined report to logs/backtest_eth_gbp.txt."""
    path = LOGS_DIR / "backtest_eth_gbp.txt"

    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    eth_days = max((df_eth_5m.index[-1] - df_eth_5m.index[0]).days, 1)
    btc_days = max((df_btc_5m.index[-1] - df_btc_5m.index[0]).days, 1)

    header = [
        "=" * 72,
        "  CryptoHybrid AI -- ETH/GBP Backtest vs BTC/GBP",
        f"  Generated : {now_str}",
        f"  BTC/GBP   : {df_btc_5m.index[0].strftime('%Y-%m-%d')} to "
        f"{df_btc_5m.index[-1].strftime('%Y-%m-%d')} ({btc_days} days)",
        f"  ETH/GBP   : {df_eth_5m.index[0].strftime('%Y-%m-%d')} to "
        f"{df_eth_5m.index[-1].strftime('%Y-%m-%d')} ({eth_days} days)",
        f"  Settings  : TS=2%, TP=10%, position=30%, costs={LIMIT_COST_PCT*100:.2f}%/trade",
        f"  STRICT rules  : 5/6 on 5m, 4/6 on 1h, all quality checks",
        f"  RELAXED rules : 4/6 on 5m, 3/6 on 1h, safety + daily filter only",
        "=" * 72,
    ]

    monthly_btc  = _monthly_table(btc_stats,   "BTC/GBP STRICT")
    monthly_eth_s = _monthly_table(eth_s_stats, "ETH/GBP STRICT")
    monthly_eth_r = _monthly_table(eth_r_stats, "ETH/GBP RELAXED")

    full = "\n".join(header) + "\n"
    full += comparison_table + "\n"
    full += monthly_btc + "\n"
    full += monthly_eth_s + "\n"
    full += monthly_eth_r + "\n"
    full += verdict + "\n"

    path.write_text(full, encoding="utf-8")
    log.info("Combined report saved -> %s", path)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    # ── 1. Fetch data ──────────────────────────────────────────────────────────
    df_eth_1d, df_eth_1h, df_eth_5m = fetch_eth_data()
    df_btc_1d, df_btc_1h, df_btc_5m = fetch_btc_data()

    # ── 2. ETH STRICT ─────────────────────────────────────────────────────────
    log.info("")
    log.info("Running ETH/GBP STRICT backtest...")
    eth_strict_stats = run_single_backtest(
        ETH_CONFIGS["ETH_STRICT"],
        df_eth_1d, df_eth_1h, df_eth_5m,
        starting_capital   = STARTING_CAPITAL_GBP,
        cost_pct           = LIMIT_COST_PCT,
        trailing_stop_pct  = TRAILING_STOP,
        take_profit_pct    = TAKE_PROFIT,
    )

    # ── 3. ETH RELAXED ────────────────────────────────────────────────────────
    log.info("")
    log.info("Running ETH/GBP RELAXED backtest...")
    eth_relaxed_stats = run_single_backtest(
        ETH_CONFIGS["ETH_RELAXED"],
        df_eth_1d, df_eth_1h, df_eth_5m,
        starting_capital   = STARTING_CAPITAL_GBP,
        cost_pct           = LIMIT_COST_PCT,
        trailing_stop_pct  = TRAILING_STOP,
        take_profit_pct    = TAKE_PROFIT,
    )

    # ── 4. BTC STRICT (same period for fair comparison) ───────────────────────
    log.info("")
    log.info("Running BTC/GBP STRICT backtest (comparison baseline)...")
    btc_strict_stats = run_single_backtest(
        BTC_CONFIG_STRICT,
        df_btc_1d, df_btc_1h, df_btc_5m,
        starting_capital   = STARTING_CAPITAL_GBP,
        cost_pct           = LIMIT_COST_PCT,
        trailing_stop_pct  = TRAILING_STOP,
        take_profit_pct    = TAKE_PROFIT,
    )

    # ── 5. Build outputs ───────────────────────────────────────────────────────
    comparison = _three_way_comparison(
        btc_strict_stats, eth_strict_stats, eth_relaxed_stats,
        df_btc_5m, df_eth_5m,
    )
    verdict = _verdict(
        btc_strict_stats, eth_strict_stats, eth_relaxed_stats,
        df_btc_5m, df_eth_5m,
    )

    # Print to terminal
    print(comparison)
    print(verdict)

    # Save combined report
    _save_combined_report(
        comparison, verdict,
        btc_strict_stats, eth_strict_stats, eth_relaxed_stats,
        df_btc_5m, df_eth_5m,
    )

    print()
    print(f"  Combined report : {LOGS_DIR / 'backtest_eth_gbp.txt'}")
    print(f"  ETH STRICT CSV  : {LOGS_DIR / 'backtest_eth_strict_trades.csv'}")
    print(f"  ETH RELAXED CSV : {LOGS_DIR / 'backtest_eth_relaxed_trades.csv'}")
    print()


if __name__ == "__main__":
    main()
