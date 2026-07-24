"""
CryptoHybrid AI -- backtest_grid.py
Backtesting engine for GridTrader: a grid trading strategy designed to complement
CryptoHybrid AI by trading sideways/choppy BTC/GBP conditions.

GridTrader activates when CryptoHybrid stays out (choppy market confirmed for 30+ min),
placing buy orders at evenly-spaced grid levels below current price and closing each
LONG position when price rises one grid level.

BACKTEST ONLY -- no integration with main_cryptohybrid.py.
"""

import csv
import logging
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

sys.path.insert(0, str(Path(__file__).parent))

# ── CryptoHybrid AI imports ──────────────────────────────────────────────────────
from backtest_btc import _yf_download, _last_bar_at
from data_feed_btc import add_indicators
from pre_checks_btc import check_choppy_market
from whale_watcher_btc import detect_swing_levels, detect_round_number_levels

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("CryptoHybrid.GridBacktest")

logging.getLogger("CryptoHybrid.PreChecks").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.WhaleWatcher").setLevel(logging.WARNING)
logging.getLogger("CryptoHybrid.DataFeed").setLevel(logging.WARNING)


# ── Default constants (all overridable as function parameters) ─────────────────

STARTING_CAPITAL_GBP   = 1_000.0
LOGS_DIR               = Path(__file__).parent / "logs"

GRID_LEVELS_DEFAULT    = 8       # number of price levels in the grid
GRID_ALLOCATION_PCT    = 0.30    # 30% of capital allocated to the grid
BREAKOUT_BUFFER_PCT    = 0.01    # 1% beyond boundary triggers forced exit
MIN_RANGE_PCT          = 0.01    # range must be at least 1% wide
MAX_RANGE_PCT          = 0.06    # range must be no wider than 6%
MIN_LEVEL_SPACING_PCT  = 0.005   # minimum grid gap (must exceed 0.34% cost; 0.5% default)
RANGING_BARS_REQUIRED  = 6       # 6 consecutive choppy 5m bars = 30 min to activate

# Limit order costs -- same as CryptoHybrid's optimal setting (0.34% per cycle)
GRID_ENTRY_FEE_PCT = 0.0016
GRID_EXIT_FEE_PCT  = 0.0016
GRID_SPREAD_PCT    = 0.0002
GRID_COST_PCT      = GRID_ENTRY_FEE_PCT + GRID_EXIT_FEE_PCT + GRID_SPREAD_PCT

BACKTEST_DAYS_5M   = 60


# ── Sweep parameters ───────────────────────────────────────────────────────────

SWEEP_NUM_LEVELS  = [4, 5, 6]
SWEEP_MIN_RANGES  = [0.03, 0.04, 0.05]
SWEEP_BUFFERS     = [0.01, 0.015, 0.02]
SWEEP_MIN_SPACING = 0.005     # fixed at 0.5% for all sweep runs


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class GridPosition:
    """One buy-sell pair at a single grid level."""
    level_idx:         int
    buy_price:         float
    sell_price:        float
    position_size_gbp: float
    status:            str            = "PENDING"
    entry_time:        Optional[datetime] = None
    exit_time:         Optional[datetime] = None
    exit_reason:       Optional[str]   = None
    exit_price:        Optional[float] = None
    pnl_gbp:           Optional[float] = None
    cost_gbp:          Optional[float] = None
    net_pnl:           Optional[float] = None


@dataclass
class GridSession:
    """One ranging period from activation to deactivation."""
    session_id:        int
    start_time:        datetime
    upper_boundary:    float
    lower_boundary:    float
    grid_levels:       list
    capital_at_start:  float
    pending:           list = field(default_factory=list)
    filled:            list = field(default_factory=list)
    completed:         list = field(default_factory=list)
    end_time:          Optional[datetime] = None
    end_reason:        Optional[str]      = None   # BREAKOUT / BACKTEST_END
    total_cycles:      int   = 0
    breakout_loss_gbp: float = 0.0


# ── Range detection ────────────────────────────────────────────────────────────

def detect_ranging_market(bar_1h: Optional[pd.Series], bar_5m: pd.Series,
                           choppy_streak: int) -> tuple:
    """
    Uses pre_checks_btc.check_choppy_market() to identify ranging conditions.
    Returns (is_ranging: bool, new_streak: int).
    Requires RANGING_BARS_REQUIRED consecutive choppy bars before activating.
    """
    if bar_1h is None:
        return False, 0
    result = check_choppy_market(bar_1h, bar_5m)
    is_choppy_now = not result["passed"]
    if not is_choppy_now:
        return False, 0
    new_streak = choppy_streak + 1
    return new_streak >= RANGING_BARS_REQUIRED, new_streak


# ── Grid boundary detection ────────────────────────────────────────────────────

def _find_grid_boundaries(df_5m_slice: pd.DataFrame,
                           current_price: float) -> tuple:
    """
    Use swing and round number detectors to find grid upper/lower boundaries.
    Returns (upper, lower) floats, or (None, None) if no usable boundary found.
    """
    swings = detect_swing_levels(df_5m_slice, current_price)
    rounds = detect_round_number_levels(current_price)

    above_candidates, below_candidates = [], []
    for src in [swings, rounds]:
        if not src.get("available"):
            continue
        for lv in src.get("above", []):
            above_candidates.append((float(lv["price"]), lv.get("sig_score", 3)))
        for lv in src.get("below", []):
            below_candidates.append((float(lv["price"]), lv.get("sig_score", 3)))

    if not above_candidates or not below_candidates:
        return None, None

    def _relevance(price, sig):
        dist_pct = abs(price - current_price) / current_price * 100
        return sig / (dist_pct + 0.1)

    upper = max(above_candidates, key=lambda x: _relevance(x[0], x[1]))[0]
    lower = min(below_candidates, key=lambda x: _relevance(x[0], x[1]))[0]
    return upper, lower


def calculate_grid_levels(upper: float, lower: float,
                           df_5m_slice: pd.DataFrame,
                           current_price: float,
                           num_levels: int) -> list:
    """
    Hybrid grid: anchor at significant swing/round levels within the range,
    fill remaining slots with evenly-spaced levels.
    Returns sorted price levels (lowest to highest), capped at num_levels.
    """
    swings = detect_swing_levels(df_5m_slice, current_price)
    rounds = detect_round_number_levels(current_price)

    anchor_prices = set()
    for src in [swings, rounds]:
        if not src.get("available"):
            continue
        for direction in ["above", "below"]:
            for lv in src.get(direction, []):
                p = float(lv["price"])
                if lower < p < upper:
                    anchor_prices.add(p)

    step = (upper - lower) / (num_levels - 1)
    base_levels = {lower + i * step for i in range(num_levels)}
    all_levels  = sorted(anchor_prices | base_levels)

    if len(all_levels) <= num_levels:
        return all_levels

    middle   = [p for p in all_levels if lower < p < upper]
    n_middle = num_levels - 2
    kept_mid = middle[::max(1, len(middle) // n_middle)][:n_middle] if n_middle > 0 else []
    return sorted({lower, upper} | set(kept_mid))


def check_range_breakout(close_price: float, upper: float, lower: float,
                          buffer_pct: float) -> bool:
    """Returns True if price closes beyond the buffer zone outside the range."""
    return (close_price > upper * (1 + buffer_pct) or
            close_price < lower * (1 - buffer_pct))


# ── Data fetch ─────────────────────────────────────────────────────────────────

def fetch_grid_data() -> tuple:
    """Fetch 1d, 1h, 5m historical data once for reuse across multiple runs."""
    log.info("Fetching data from Yahoo Finance...")
    df_1d = add_indicators(_yf_download("1d", "2y"))
    df_1h = add_indicators(_yf_download("1h", "90d"))
    df_5m = add_indicators(_yf_download("5m", f"{BACKTEST_DAYS_5M}d"))
    log.info("  1d: %d bars  1h: %d bars  5m: %d bars",
             len(df_1d), len(df_1h), len(df_5m))
    return df_1d, df_1h, df_5m


# ── Session activation ─────────────────────────────────────────────────────────

def _activate_grid_session(session_id: int, ts: datetime, bar_5m: pd.Series,
                             df_5m_slice: pd.DataFrame, capital: float,
                             num_levels: int, allocation_pct: float,
                             min_range_pct: float, max_range_pct: float,
                             min_level_spacing_pct: float,
                             logv) -> Optional[GridSession]:
    """
    Validates range, checks minimum level spacing, creates GridSession.
    logv: logging function (log.info in verbose mode, no-op in quiet mode).
    Returns None if validation fails.
    """
    current_price = float(bar_5m["close"])
    upper, lower  = _find_grid_boundaries(df_5m_slice, current_price)

    if upper is None or lower is None:
        logv("  Grid #%d: no valid boundaries at %.2f", session_id, current_price)
        return None

    range_pct = (upper - lower) / lower
    if range_pct < min_range_pct:
        logv("  Grid #%d: range too tight (%.2f%% < %.0f%%)",
             session_id, range_pct * 100, min_range_pct * 100)
        return None
    if range_pct > max_range_pct:
        logv("  Grid #%d: range too wide (%.2f%% > %.0f%%)",
             session_id, range_pct * 100, max_range_pct * 100)
        return None

    grid_levels = calculate_grid_levels(upper, lower, df_5m_slice, current_price, num_levels)
    if len(grid_levels) < 2:
        logv("  Grid #%d: only %d levels calculated", session_id, len(grid_levels))
        return None

    # Minimum level spacing check -- reject if any gap is narrower than cost+margin
    gaps = [grid_levels[i + 1] - grid_levels[i] for i in range(len(grid_levels) - 1)]
    min_gap_pct = min(gaps) / current_price
    if min_gap_pct < min_level_spacing_pct:
        logv("  Grid #%d: spacing too narrow (%.3f%% < %.1f%%) -- skipped",
             session_id, min_gap_pct * 100, min_level_spacing_pct * 100)
        return None

    buy_levels = [lv for lv in grid_levels[:-1] if lv < current_price]
    if not buy_levels:
        logv("  Grid #%d: no levels below price %.2f (lowest=%.2f)",
             session_id, current_price, grid_levels[0])
        return None

    size_per_level = (capital * allocation_pct) / num_levels
    pending = [
        GridPosition(
            level_idx         = i,
            buy_price         = lv,
            sell_price        = grid_levels[i + 1],
            position_size_gbp = size_per_level,
            status            = "PENDING",
        )
        for i, lv in enumerate(grid_levels[:-1])
        if lv < current_price
    ]

    session = GridSession(
        session_id       = session_id,
        start_time       = ts,
        upper_boundary   = upper,
        lower_boundary   = lower,
        grid_levels      = grid_levels,
        capital_at_start = capital,
        pending          = pending,
    )
    log.info(
        "  GRID #%d ACTIVATED | price=%.2f | range=%.2f-%.2f (%.1f%%)"
        " | %d levels | min gap=%.2f%% | %d pending | size=GBP %.2f/level",
        session_id, current_price, lower, upper, range_pct * 100,
        len(grid_levels), min_gap_pct * 100, len(pending), size_per_level,
    )
    return session


# ── Main simulation loop ───────────────────────────────────────────────────────

def run_grid_backtest(
    df_1d=None,
    df_1h=None,
    df_5m=None,
    num_levels:            int   = GRID_LEVELS_DEFAULT,
    allocation_pct:        float = GRID_ALLOCATION_PCT,
    breakout_buffer:       float = BREAKOUT_BUFFER_PCT,
    min_range_pct:         float = MIN_RANGE_PCT,
    max_range_pct:         float = MAX_RANGE_PCT,
    min_level_spacing_pct: float = MIN_LEVEL_SPACING_PCT,
    starting_capital:      float = STARTING_CAPITAL_GBP,
    quiet:                 bool  = False,
    save_files:            bool  = True,
) -> dict:
    """
    Replay 5m bars, activating GridTrader during confirmed ranging periods.
    Accepts pre-fetched data frames (for sweep reuse); fetches if not provided.
    quiet=True suppresses fill/close logging. save_files=False skips CSV/TXT output.
    Returns a stats dict for reporting.
    """
    if df_1d is None:
        df_1d, df_1h, df_5m = fetch_grid_data()

    logv = (lambda *a, **k: None) if quiet else log.info

    if not quiet:
        log.info("")
        log.info("=" * 60)
        log.info("  GridTrader | levels=%d  min_range=%.0f%%  buffer=%.0f%%"
                 "  min_spacing=%.1f%%",
                 num_levels, min_range_pct * 100,
                 breakout_buffer * 100, min_level_spacing_pct * 100)
        log.info("=" * 60)

    capital            = starting_capital
    choppy_streak      = 0
    active_session     = None
    completed_sessions = []
    bars_ranging       = 0
    bars_trending      = 0
    session_counter    = 0

    for ts, bar_5m_row in df_5m.iterrows():
        bar_1h = _last_bar_at(df_1h, ts)
        high   = float(bar_5m_row["high"])
        low    = float(bar_5m_row["low"])
        close  = float(bar_5m_row["close"])

        # ── Manage active grid session ─────────────────────────────────────────
        if active_session is not None:
            bars_ranging += 1
            s = active_session

            # Step 1: Fill pending buy orders (bar low touches buy level)
            still_pending = []
            for pos in s.pending:
                if low <= pos.buy_price:
                    pos.status     = "FILLED"
                    pos.entry_time = ts
                    s.filled.append(pos)
                    logv("  FILL  session=%d level=%d buy=%.2f -> sell=%.2f",
                         s.session_id, pos.level_idx, pos.buy_price, pos.sell_price)
                else:
                    still_pending.append(pos)
            s.pending = still_pending

            # Step 2: Close filled positions (bar high touches sell level)
            still_filled = []
            for pos in s.filled:
                if high >= pos.sell_price:
                    pos.status      = "CLOSED"
                    pos.exit_time   = ts
                    pos.exit_reason = "TAKE_PROFIT"
                    pos.exit_price  = pos.sell_price
                    pos.pnl_gbp     = (pos.position_size_gbp
                                       * (pos.sell_price - pos.buy_price)
                                       / pos.buy_price)
                    pos.cost_gbp    = pos.position_size_gbp * GRID_COST_PCT
                    pos.net_pnl     = pos.pnl_gbp - pos.cost_gbp
                    capital        += pos.net_pnl
                    s.total_cycles += 1
                    s.completed.append(pos)
                    logv("  CLOSE session=%d level=%d net=GBP %+.4f cycles=%d",
                         s.session_id, pos.level_idx, pos.net_pnl, s.total_cycles)
                    # Reset: queue a new pending buy at the same level
                    s.pending.append(GridPosition(
                        level_idx         = pos.level_idx,
                        buy_price         = pos.buy_price,
                        sell_price        = pos.sell_price,
                        position_size_gbp = pos.position_size_gbp,
                        status            = "PENDING",
                    ))
                else:
                    still_filled.append(pos)
            s.filled = still_filled

            # Step 3: Check for breakout
            if check_range_breakout(close, s.upper_boundary, s.lower_boundary,
                                     breakout_buffer):
                log.info(
                    "  BREAKOUT session=%d close=%.2f outside %.2f-%.2f",
                    s.session_id, close, s.lower_boundary, s.upper_boundary,
                )
                for pos in s.filled:
                    pos.status      = "CLOSED"
                    pos.exit_time   = ts
                    pos.exit_reason = "BREAKOUT_EXIT"
                    pos.exit_price  = close
                    pos.pnl_gbp     = (pos.position_size_gbp
                                       * (close - pos.buy_price)
                                       / pos.buy_price)
                    pos.cost_gbp    = pos.position_size_gbp * GRID_COST_PCT
                    pos.net_pnl     = pos.pnl_gbp - pos.cost_gbp
                    capital        += pos.net_pnl
                    s.breakout_loss_gbp += min(0.0, pos.net_pnl)
                    s.completed.append(pos)
                for pos in s.pending:
                    pos.status = "CANCELLED"
                    s.completed.append(pos)
                s.filled   = []
                s.pending  = []
                s.end_time   = ts
                s.end_reason = "BREAKOUT"
                completed_sessions.append(s)
                active_session = None
                choppy_streak  = 0
            continue

        # ── No active session: detect ranging mode ─────────────────────────────
        bars_trending += 1
        is_ranging, choppy_streak = detect_ranging_market(bar_1h, bar_5m_row, choppy_streak)

        if is_ranging:
            df_5m_slice  = df_5m.loc[:ts]
            session_counter += 1
            new_session = _activate_grid_session(
                session_counter, ts, bar_5m_row, df_5m_slice,
                capital, num_levels, allocation_pct,
                min_range_pct, max_range_pct, min_level_spacing_pct, logv,
            )
            if new_session is not None:
                active_session = new_session
            choppy_streak = 0

    # ── Close any session still open at end of data ────────────────────────────
    if active_session is not None:
        last_close = float(df_5m.iloc[-1]["close"])
        last_ts    = df_5m.index[-1]
        s          = active_session
        for pos in s.filled:
            pos.status      = "CLOSED"
            pos.exit_time   = last_ts
            pos.exit_reason = "BACKTEST_END"
            pos.exit_price  = last_close
            pos.pnl_gbp     = (pos.position_size_gbp
                               * (last_close - pos.buy_price)
                               / pos.buy_price)
            pos.cost_gbp    = pos.position_size_gbp * GRID_COST_PCT
            pos.net_pnl     = pos.pnl_gbp - pos.cost_gbp
            capital        += pos.net_pnl
            s.completed.append(pos)
        for pos in s.pending:
            pos.status = "CANCELLED"
            s.completed.append(pos)
        s.end_time   = last_ts
        s.end_reason = "BACKTEST_END"
        completed_sessions.append(s)
        log.info("  Backtest end -- open session closed at GBP %.2f", last_close)

    stats = _compute_grid_stats(
        completed_sessions, capital, starting_capital, df_5m,
        bars_ranging, bars_trending,
        num_levels, allocation_pct, breakout_buffer,
        min_range_pct, max_range_pct, min_level_spacing_pct,
    )

    if save_files:
        LOGS_DIR.mkdir(exist_ok=True)
        _save_grid_csv(completed_sessions)
        _save_grid_txt(stats, completed_sessions, df_5m)

    return stats


# ── Statistics ─────────────────────────────────────────────────────────────────

def _compute_grid_stats(sessions, final_capital, starting_capital, df_5m,
                         bars_ranging, bars_trending,
                         num_levels, allocation_pct, breakout_buffer,
                         min_range_pct, max_range_pct,
                         min_level_spacing_pct) -> dict:
    all_closed   = [p for s in sessions for p in s.completed if p.status == "CLOSED"]
    cycles       = [p for p in all_closed if p.exit_reason == "TAKE_PROFIT"]
    breakout_ex  = [p for p in all_closed if p.exit_reason == "BREAKOUT_EXIT"]
    n_natural    = sum(1 for s in sessions if s.end_reason != "BREAKOUT")
    n_breakout_s = sum(1 for s in sessions if s.end_reason == "BREAKOUT")

    gross_pnl   = sum(p.pnl_gbp  or 0.0 for p in all_closed)
    total_costs = sum(p.cost_gbp or 0.0 for p in all_closed)
    net_pnl     = sum(p.net_pnl  or 0.0 for p in all_closed)
    total_return_pct = (final_capital - starting_capital) / starting_capital * 100

    period_days = max((df_5m.index[-1] - df_5m.index[0]).days, 1)
    bars_total  = bars_ranging + bars_trending
    pct_ranging = bars_ranging / bars_total * 100 if bars_total else 0.0

    session_pnls = [sum(p.net_pnl or 0.0 for p in s.completed if p.status == "CLOSED")
                    for s in sessions]
    total_breakout_loss = sum(s.breakout_loss_gbp for s in sessions)
    avg_cycles  = (sum(s.total_cycles for s in sessions) / len(sessions)
                   if sessions else 0.0)
    natural_close_rate = n_natural / len(sessions) * 100 if sessions else 0.0

    # Average gross profit per TAKE_PROFIT cycle as % of position size
    avg_gross_pct = 0.0
    if cycles:
        avg_gross_pct = (sum(p.pnl_gbp / p.position_size_gbp for p in cycles)
                         / len(cycles) * 100)

    monthly: dict = defaultdict(float)
    for p in all_closed:
        if p.exit_time and p.net_pnl is not None:
            monthly[p.exit_time.strftime("%Y-%m")] += p.net_pnl

    return {
        # Parameters
        "num_levels":              num_levels,
        "allocation_pct":          allocation_pct,
        "breakout_buffer":         breakout_buffer,
        "min_range_pct":           min_range_pct,
        "max_range_pct":           max_range_pct,
        "min_level_spacing_pct":   min_level_spacing_pct,
        # P&L
        "starting_capital":        starting_capital,
        "final_capital":           final_capital,
        "gross_pnl":               gross_pnl,
        "total_costs":             total_costs,
        "net_pnl":                 net_pnl,
        "total_return_pct":        total_return_pct,
        "ann_return_pct":          total_return_pct * 365 / period_days,
        "period_days":             period_days,
        # Time
        "bars_total":              bars_total,
        "bars_ranging":            bars_ranging,
        "pct_ranging":             pct_ranging,
        "pct_trending":            100.0 - pct_ranging,
        # Sessions
        "n_sessions":              len(sessions),
        "n_natural_closes":        n_natural,
        "n_breakout_sessions":     n_breakout_s,
        "natural_close_rate":      natural_close_rate,
        # Cycles
        "n_completed_cycles":      len(cycles),
        "n_breakout_exits":        len(breakout_ex),
        "avg_cycles_per_session":  avg_cycles,
        "avg_gross_pct_per_cycle": avg_gross_pct,
        # Risk
        "best_session_pnl":        max(session_pnls, default=0.0),
        "worst_session_pnl":       min(session_pnls, default=0.0),
        "total_breakout_loss":     total_breakout_loss,
        "monthly":                 dict(monthly),
    }


# ── Output: single-run CSV ─────────────────────────────────────────────────────

def _save_grid_csv(sessions) -> None:
    path = LOGS_DIR / "backtest_grid_trades.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "session_id", "level_idx", "buy_price", "sell_price",
            "position_gbp", "status", "exit_reason",
            "entry_time", "exit_time", "exit_price",
            "gross_pnl", "cost_gbp", "net_pnl",
        ])
        for s in sessions:
            for pos in s.completed:
                w.writerow([
                    s.session_id, pos.level_idx,
                    f"{pos.buy_price:.2f}", f"{pos.sell_price:.2f}",
                    f"{pos.position_size_gbp:.2f}",
                    pos.status, pos.exit_reason or "",
                    pos.entry_time.strftime("%Y-%m-%d %H:%M") if pos.entry_time else "",
                    pos.exit_time.strftime("%Y-%m-%d %H:%M")  if pos.exit_time  else "",
                    f"{pos.exit_price:.2f}" if pos.exit_price is not None else "",
                    f"{pos.pnl_gbp:.4f}"   if pos.pnl_gbp   is not None else "",
                    f"{pos.cost_gbp:.4f}"  if pos.cost_gbp  is not None else "",
                    f"{pos.net_pnl:.4f}"   if pos.net_pnl   is not None else "",
                ])
    log.info("Grid trade log -> %s", path)


# ── Output: single-run TXT ─────────────────────────────────────────────────────

def _save_grid_txt(stats: dict, sessions, df_5m) -> None:
    path         = LOGS_DIR / "backtest_grid_results.txt"
    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "=" * 64,
        "  CryptoHybrid AI -- GridTrader Backtest Results",
        f"  Period  : {period_start} to {period_end}  ({stats['period_days']} days)",
        f"  Config  : {stats['num_levels']} grid levels"
        f"  |  {stats['allocation_pct']*100:.0f}% allocation"
        f"  |  {stats['breakout_buffer']*100:.0f}% breakout buffer"
        f"  |  {stats['min_range_pct']*100:.0f}% min range"
        f"  |  {stats['min_level_spacing_pct']*100:.1f}% min spacing",
        f"  Costs   : {GRID_COST_PCT*100:.2f}% per completed cycle (limit orders)",
        f"  Ran     : {now_str}",
        "=" * 64,
        "",
        "-- P&L overview -----------------------------------------------",
        f"  Starting capital       : GBP {stats['starting_capital']:>10.2f}",
        f"  Final capital          : GBP {stats['final_capital']:>10.2f}",
        f"  Gross P&L              : GBP {stats['gross_pnl']:>+10.4f}",
        f"  Trading costs          : GBP {-stats['total_costs']:>+10.4f}",
        f"  Net P&L                : GBP {stats['net_pnl']:>+10.4f}",
        f"  Net return             :     {stats['total_return_pct']:>+9.2f}%",
        f"  Annualised estimate    :     {stats['ann_return_pct']:>+9.1f}%",
        "",
        "-- Time in market ---------------------------------------------",
        f"  Total 5m bars          : {stats['bars_total']:,}",
        f"  Bars in ranging mode   : {stats['bars_ranging']:,}"
        f"  ({stats['pct_ranging']:.1f}%)",
        f"  Bars in trending mode  : {stats['bars_total'] - stats['bars_ranging']:,}"
        f"  ({stats['pct_trending']:.1f}%)",
        "",
        "-- Grid sessions ----------------------------------------------",
        f"  Sessions activated     : {stats['n_sessions']}",
        f"  Natural closes         : {stats['n_natural_closes']}"
        f"  ({stats['natural_close_rate']:.0f}%)",
        f"  Breakout events        : {stats['n_breakout_sessions']}",
        f"  Completed cycles       : {stats['n_completed_cycles']}",
        f"  Avg cycles per session : {stats['avg_cycles_per_session']:.1f}",
        f"  Avg gross % per cycle  : {stats['avg_gross_pct_per_cycle']:>+.3f}%"
        f"  (cost = {GRID_COST_PCT*100:.2f}%)",
        f"  Total breakout loss    : GBP {stats['total_breakout_loss']:>+.4f}",
        f"  Best session net P&L   : GBP {stats['best_session_pnl']:>+.4f}",
        f"  Worst session net P&L  : GBP {stats['worst_session_pnl']:>+.4f}",
        "",
        "-- Monthly net P&L --------------------------------------------",
    ]
    for month in sorted(stats["monthly"]):
        lines.append(f"    {month}   GBP {stats['monthly'][month]:>+8.4f}")

    lines += ["", "-- Session log ------------------------------------------------"]
    for s in sessions:
        s_net = sum(p.net_pnl or 0.0 for p in s.completed if p.status == "CLOSED")
        dur   = ""
        if s.end_time:
            mins = (s.end_time - s.start_time).total_seconds() / 60
            dur  = f"{mins:.0f}m"
        lines.append(
            f"  #{s.session_id:>3}  {s.start_time.strftime('%Y-%m-%d %H:%M')}"
            f"  range={s.lower_boundary:.0f}-{s.upper_boundary:.0f}"
            f"  cycles={s.total_cycles}"
            f"  net=GBP {s_net:>+.4f}"
            f"  end={s.end_reason or '?'}  dur={dur}"
        )

    lines += ["", "=" * 64]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Grid results -> %s", path)


# ── Terminal summary (single run) ──────────────────────────────────────────────

def _print_grid_summary(stats: dict) -> None:
    sep = "=" * 64
    print()
    print(sep)
    print("  GRIDTRADER BACKTEST RESULTS")
    print(f"  Period  : {stats['period_days']} days"
          f"  |  {stats['bars_total']:,} x 5m bars")
    print(f"  Config  : {stats['num_levels']} levels"
          f"  |  {stats['allocation_pct']*100:.0f}% allocation"
          f"  |  {stats['breakout_buffer']*100:.0f}% buffer"
          f"  |  {stats['min_range_pct']*100:.0f}% min range"
          f"  |  {stats['min_level_spacing_pct']*100:.1f}% min spacing"
          f"  |  {GRID_COST_PCT*100:.2f}% cost/cycle")
    print(sep)

    def row(label, value):
        print(f"  {label:<42} {value}")

    row("Starting capital",   f"GBP {stats['starting_capital']:>10.2f}")
    row("Final capital",      f"GBP {stats['final_capital']:>10.2f}")
    row("Gross P&L",          f"GBP {stats['gross_pnl']:>+10.4f}")
    row("Trading costs",      f"GBP {-stats['total_costs']:>+10.4f}")
    row("Net P&L",            f"GBP {stats['net_pnl']:>+10.4f}")
    row("Net return",         f"    {stats['total_return_pct']:>+10.2f}%")
    row("Annualised estimate",f"    {stats['ann_return_pct']:>+10.1f}%")
    print()
    row("Bars in ranging/grid mode",
        f"{stats['bars_ranging']:,}  ({stats['pct_ranging']:.1f}%)")
    row("Bars in trending mode",
        f"{stats['bars_total'] - stats['bars_ranging']:,}  ({stats['pct_trending']:.1f}%)")
    print()
    row("Grid sessions activated",    str(stats['n_sessions']))
    row("Natural closes",             f"{stats['n_natural_closes']} ({stats['natural_close_rate']:.0f}%)")
    row("Breakout events",            str(stats['n_breakout_sessions']))
    row("Completed buy-sell cycles",  str(stats['n_completed_cycles']))
    row("Avg cycles per session",     f"{stats['avg_cycles_per_session']:.1f}")
    row("Avg gross % per cycle",      f"{stats['avg_gross_pct_per_cycle']:>+.3f}% (cost={GRID_COST_PCT*100:.2f}%)")
    row("Total breakout losses",      f"GBP {stats['total_breakout_loss']:>+.4f}")
    row("Best session net P&L",       f"GBP {stats['best_session_pnl']:>+.4f}")
    row("Worst session net P&L",      f"GBP {stats['worst_session_pnl']:>+.4f}")
    print()
    print("  -- CryptoHybrid / GridTrader coverage analysis ----------")
    print(f"  GridTrader active (ranging/choppy) : {stats['pct_ranging']:.1f}% of bars")
    print(f"  CryptoHybrid active (trending)       : {stats['pct_trending']:.1f}% of bars")
    combined = min(100.0, stats['pct_ranging'] + stats['pct_trending'])
    print(f"  Combined coverage estimate         : {combined:.1f}% of bars")
    print()
    print(f"  Full results : logs/backtest_grid_results.txt")
    print(f"  Trade log    : logs/backtest_grid_trades.csv")
    print(sep)


# ── Parameter sweep ────────────────────────────────────────────────────────────

def run_sweep() -> list:
    """
    Fetch data once, then run all 27 parameter combinations.
    Returns results list sorted by net P&L descending.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    df_1d, df_1h, df_5m = fetch_grid_data()

    total_combos = len(SWEEP_NUM_LEVELS) * len(SWEEP_MIN_RANGES) * len(SWEEP_BUFFERS)
    log.info("")
    log.info("=" * 60)
    log.info("  GridTrader Parameter Sweep -- %d combinations", total_combos)
    log.info("  MIN_LEVEL_SPACING_PCT fixed at %.1f%%", SWEEP_MIN_SPACING * 100)
    log.info("=" * 60)

    results = []
    combo   = 0
    for num_levels in SWEEP_NUM_LEVELS:
        for min_range in SWEEP_MIN_RANGES:
            for buffer in SWEEP_BUFFERS:
                combo += 1
                log.info(
                    "  [%2d/%d] levels=%d  min_range=%.0f%%  buffer=%.1f%%",
                    combo, total_combos, num_levels, min_range * 100, buffer * 100,
                )
                stats = run_grid_backtest(
                    df_1d                = df_1d,
                    df_1h                = df_1h,
                    df_5m                = df_5m,
                    num_levels           = num_levels,
                    allocation_pct       = GRID_ALLOCATION_PCT,
                    breakout_buffer      = buffer,
                    min_range_pct        = min_range,
                    max_range_pct        = MAX_RANGE_PCT,
                    min_level_spacing_pct= SWEEP_MIN_SPACING,
                    starting_capital     = STARTING_CAPITAL_GBP,
                    quiet                = True,
                    save_files           = False,
                )
                results.append(stats)

    results.sort(key=lambda r: r["net_pnl"], reverse=True)
    _print_sweep_table(results)
    _save_sweep_csv(results)
    _save_sweep_txt(results, df_5m)
    _print_sweep_verdict(results)
    return results


def _print_sweep_table(results: list) -> None:
    """Print 27-row comparison table sorted by net P&L."""
    sep = "=" * 100
    print()
    print(sep)
    print("  GRIDTRADER PARAMETER SWEEP -- ALL 27 COMBINATIONS")
    print(f"  Min level spacing fixed at {SWEEP_MIN_SPACING*100:.1f}%"
          f"  |  Trading cost {GRID_COST_PCT*100:.2f}% per cycle  |  Capital GBP {STARTING_CAPITAL_GBP:.0f}")
    print(sep)
    hdr = (f"  {'Rank':<4} {'Lvl':>3} {'MinRng':>6} {'Buf':>5}"
           f" {'Sessions':>8} {'NatCls%':>8} {'Cycles':>7}"
           f" {'AvgGrs%':>8} {'MaxLoss':>9} {'NetP&L':>10} {'Return%':>8}")
    print(hdr)
    print("  " + "-" * 96)
    for rank, r in enumerate(results, 1):
        flag = ""
        if rank == 1: flag = " *** BEST"
        elif rank == 2: flag = " **"
        elif rank == 3: flag = " *"
        nat_cls_flag = " !" if r["natural_close_rate"] < 50.0 else ""
        print(
            f"  {rank:<4} {r['num_levels']:>3} {r['min_range_pct']*100:>5.0f}%"
            f" {r['breakout_buffer']*100:>4.1f}%"
            f" {r['n_sessions']:>8}"
            f" {r['natural_close_rate']:>7.0f}%{nat_cls_flag:<1}"
            f" {r['n_completed_cycles']:>7}"
            f" {r['avg_gross_pct_per_cycle']:>+7.3f}%"
            f" {r['worst_session_pnl']:>+9.4f}"
            f" {r['net_pnl']:>+10.4f}"
            f" {r['total_return_pct']:>+7.2f}%"
            f"{flag}"
        )
    print(sep)
    print("  ! = natural close rate below 50% (structural concern)")
    print()

    # Best combination detail
    best = results[0]
    print(f"  TOP COMBINATION DETAIL")
    print(f"  Config  : {best['num_levels']} levels"
          f"  |  {best['min_range_pct']*100:.0f}% min range"
          f"  |  {best['breakout_buffer']*100:.1f}% breakout buffer"
          f"  |  {SWEEP_MIN_SPACING*100:.1f}% min spacing")
    print(f"  Net P&L : GBP {best['net_pnl']:>+.4f}"
          f"  ({best['total_return_pct']:>+.2f}% over {best['period_days']} days)")
    print(f"  Ann.est.: {best['ann_return_pct']:>+.1f}%")
    print(f"  Sessions: {best['n_sessions']}  |  Cycles: {best['n_completed_cycles']}"
          f"  |  Avg gross/cycle: {best['avg_gross_pct_per_cycle']:>+.3f}%")
    print(f"  Natural close rate: {best['natural_close_rate']:.0f}%", end="")
    if best["natural_close_rate"] < 50.0:
        print("  <<< BELOW 50% -- structural concern: most sessions end in forced breakout")
    else:
        print("  (above 50%)")
    print()


def _save_sweep_csv(results: list) -> None:
    path = LOGS_DIR / "backtest_grid_sweep.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "rank", "num_levels", "min_range_pct", "breakout_buffer",
            "min_spacing_pct", "n_sessions", "n_natural_closes",
            "natural_close_rate_pct", "n_completed_cycles",
            "avg_gross_pct_per_cycle", "worst_session_pnl",
            "gross_pnl", "total_costs", "net_pnl",
            "total_return_pct", "ann_return_pct",
        ])
        for rank, r in enumerate(results, 1):
            w.writerow([
                rank,
                r["num_levels"],
                f"{r['min_range_pct']*100:.0f}%",
                f"{r['breakout_buffer']*100:.1f}%",
                f"{r['min_level_spacing_pct']*100:.1f}%",
                r["n_sessions"],
                r["n_natural_closes"],
                f"{r['natural_close_rate']:.1f}%",
                r["n_completed_cycles"],
                f"{r['avg_gross_pct_per_cycle']:>+.3f}%",
                f"{r['worst_session_pnl']:>+.4f}",
                f"{r['gross_pnl']:>+.4f}",
                f"{r['total_costs']:>+.4f}",
                f"{r['net_pnl']:>+.4f}",
                f"{r['total_return_pct']:>+.2f}%",
                f"{r['ann_return_pct']:>+.1f}%",
            ])
    log.info("Sweep CSV -> %s", path)


def _save_sweep_txt(results: list, df_5m) -> None:
    path         = LOGS_DIR / "backtest_grid_sweep.txt"
    period_start = df_5m.index[0].strftime("%Y-%m-%d")
    period_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    period_days  = max((df_5m.index[-1] - df_5m.index[0]).days, 1)
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "=" * 80,
        "  CryptoHybrid AI -- GridTrader Parameter Sweep Results",
        f"  Period  : {period_start} to {period_end}  ({period_days} days)",
        f"  Sweep   : {len(results)} combinations"
        f"  |  Min spacing fixed at {SWEEP_MIN_SPACING*100:.1f}%"
        f"  |  Cost per cycle {GRID_COST_PCT*100:.2f}%",
        f"  Ran     : {now_str}",
        "=" * 80,
        "",
        f"  {'Rank':<4} {'Lvl':>3} {'MinRng':>6} {'Buf':>5}"
        f" {'Sess':>5} {'NatCls%':>8} {'Cyc':>5}"
        f" {'AvgGrs%':>8} {'MaxLoss':>9} {'NetP&L':>10} {'Ret%':>7}",
        "  " + "-" * 76,
    ]
    for rank, r in enumerate(results, 1):
        flag = " BEST" if rank == 1 else ("  **" if rank == 2 else ("   *" if rank == 3 else ""))
        lines.append(
            f"  {rank:<4} {r['num_levels']:>3} {r['min_range_pct']*100:>5.0f}%"
            f" {r['breakout_buffer']*100:>4.1f}%"
            f" {r['n_sessions']:>5}"
            f" {r['natural_close_rate']:>7.0f}%"
            f" {r['n_completed_cycles']:>5}"
            f" {r['avg_gross_pct_per_cycle']:>+7.3f}%"
            f" {r['worst_session_pnl']:>+9.4f}"
            f" {r['net_pnl']:>+10.4f}"
            f" {r['total_return_pct']:>+6.2f}%"
            f"{flag}"
        )

    lines += ["", "=" * 80]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Sweep results -> %s", path)


def _print_sweep_verdict(results: list) -> None:
    """Print a plain-English conclusion about GridTrader viability."""
    best        = results[0]
    any_profit  = any(r["net_pnl"] > 0 for r in results)
    n_profitable= sum(1 for r in results if r["net_pnl"] > 0)
    breakout_dominant = best["natural_close_rate"] < 50.0
    best_avg_gross = best["avg_gross_pct_per_cycle"]

    sep = "=" * 64
    print()
    print(sep)
    print("  PLAIN-ENGLISH VERDICT")
    print(sep)

    if any_profit:
        print(f"  {n_profitable} of {len(results)} combinations produced a positive"
              " net P&L.")
        print()
        print(f"  Best result: {best['num_levels']} levels / "
              f"{best['min_range_pct']*100:.0f}% min range / "
              f"{best['breakout_buffer']*100:.1f}% buffer")
        print(f"  Net P&L GBP {best['net_pnl']:>+.4f}"
              f"  ({best['total_return_pct']:>+.2f}% / {best['ann_return_pct']:>+.1f}% ann.)")
        print(f"  Avg gross per cycle: {best_avg_gross:>+.3f}%"
              f"  vs {GRID_COST_PCT*100:.2f}% cost -- margin:"
              f" {best_avg_gross - GRID_COST_PCT*100:>+.3f}%")
    else:
        print(f"  All {len(results)} combinations produced a NEGATIVE net P&L.")
        print(f"  Best (least bad): {best['num_levels']} levels / "
              f"{best['min_range_pct']*100:.0f}% min range / "
              f"{best['breakout_buffer']*100:.1f}% buffer")
        print(f"  Net P&L GBP {best['net_pnl']:>+.4f}"
              f"  ({best['total_return_pct']:>+.2f}%)")

    if breakout_dominant:
        print()
        print("  STRUCTURAL CONCERN: Even the best configuration has a natural")
        print(f"  close rate of {best['natural_close_rate']:.0f}% -- the majority of"
              " grid sessions end in a")
        print("  forced breakout exit rather than completing their grid cycles.")
        print("  This means BTC/GBP is not staying range-bound long enough for")
        print("  the grid to harvest its cycles before price escapes.")
        print()
        print("  CONCLUSION: The problem is not parameter tuning -- it is the")
        print("  underlying premise. BTC/GBP ranges identified by the choppy")
        print("  filter consistently break out before the grid can generate enough")
        print("  cycle profit to offset the cost of the forced close. Wider")
        print("  buffers delay but do not eliminate this. GridTrader in its current")
        print("  long-only form is not viable on this asset/timeframe combination.")
        print()
        print("  POSSIBLE PATHS FORWARD:")
        print("  1. Bidirectional grid (long + short) -- captures the breakout")
        print("     direction instead of being hurt by it.")
        print("  2. Dynamic exit: close the grid when trend indicators fire,")
        print("     before price reaches the breakout threshold.")
        print("  3. Different asset or timeframe (higher-frequency grid on")
        print("     a naturally choppier pair).")
    else:
        print()
        if any_profit:
            print(f"  Natural close rate {best['natural_close_rate']:.0f}% is above 50% --")
            print("  sessions are completing as designed more often than breaking out.")
            if best["total_return_pct"] > 0:
                print("  CONCLUSION: A viable GridTrader configuration EXISTS within")
                print("  the tested parameter space. The best combination above")
                print("  generates positive net P&L with structurally sound behaviour.")
            else:
                print("  CONCLUSION: Sessions complete correctly but P&L is marginal.")
                print("  Consider increasing range requirements further or reducing levels.")

    print(sep)


# ── Standalone single-run entry point ──────────────────────────────────────────

def run_backtest() -> None:
    """Run a single configuration and print results (backward-compatible entry point)."""
    LOGS_DIR.mkdir(exist_ok=True)
    stats = run_grid_backtest(
        num_levels            = GRID_LEVELS_DEFAULT,
        allocation_pct        = GRID_ALLOCATION_PCT,
        breakout_buffer       = BREAKOUT_BUFFER_PCT,
        min_range_pct         = MIN_RANGE_PCT,
        max_range_pct         = MAX_RANGE_PCT,
        min_level_spacing_pct = MIN_LEVEL_SPACING_PCT,
        starting_capital      = STARTING_CAPITAL_GBP,
    )
    _print_grid_summary(stats)


if __name__ == "__main__":
    run_sweep()
