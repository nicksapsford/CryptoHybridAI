"""
CryptoHybrid AI -- performance_btc.py
Ace's self-learning performance analytics engine.

Reads trades.csv, identifies patterns, calculates a confidence score,
and formats insights that are passed to every Claude trading decision.
Every 50 trades it generates a detailed milestone review saved to logs/.

Public API:
    analyse_trade_history(path)          -> full stats dict
    get_performance_context(path)        -> formatted str for Claude
    get_perf_dashboard_dict(path)        -> compact dict for dashboard
    generate_milestone_review(path, num) -> saves logs/ace_review_NN.txt
    invalidate_cache()                   -> call after each trade closes
"""

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── ALBION STANDING RULE: ALL TIMESTAMPS ARE UTC ────────────────────────────
# Every timestamp this module reads or writes (trades.csv, morgan_confidence.csv,
# phantom verdicts, log lines) is UTC — written via datetime.now(timezone.utc)
# and read back as UTC. NEVER interpret any Albion timestamp as BST/local.
# Confirm UTC before analysing. (Nick's standing rule, baked in 12 Jul 2026.)

import phantom_tracker

log = logging.getLogger("CryptoHybrid.Performance")

# ──────────────────────────────────────────────────────────────────────────────
# Morgan — persistent individual-confidence store + phantom verdict feedback
# ──────────────────────────────────────────────────────────────────────────────

_MORGAN_STATE_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'morgan_confidence.json')
_morgan_lock = threading.Lock()
_morgan_confidence = None

# THREE-ZONE MORGAN MODEL (desk-wide, 24 Jul 2026 -- Nick's direct order):
#   Zone 1 NORMAL   (>= 50) : trade as usual.
#   Zone 2 WARNING  (30-49) : trading CONTINUES; dashboard shows a warning + manual
#                             RESET button. Informational only -- no restriction.
#   Zone 3 HARD BLOCK (< 30): NO new entries (existing positions still managed/exited);
#                             Gaius System Intervention Protocol triggers.
# The old "exceptional setups only" (<50) / "conservative STAY_OUT" (<25) postures are
# REMOVED: entry threshold is the SAME at any Morgan score above 30. Morgan may fall
# freely -- a dashboard warning + a MANUAL reset (/api/reset-morgan -> confidence_lift
# .json, applied live by the engine) keep Nick in control; there is NO automatic clamp.
# NOTE: the perf `conservative_mode` (and `conservative`) flag is REDEFINED from the old
# <25 VERY_LOW meaning to the Zone-3 hard block (<30) so Gaius fires at <30. Shared across
# BTC and ETH (single performance module for the dual-asset desk).
MORGAN_FLOOR      = 50   # WARNING threshold (Zone 2 starts below this) -- NOT a clamp
MORGAN_HARD_BLOCK = 30   # HARD BLOCK threshold (Zone 3: no new entries below this)
_MORGAN_LAST_RESET_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'morgan_last_reset.json')


def morgan_below_floor(score) -> bool:
    """True when Morgan is below the 50 WARNING threshold (Zone 2 or 3). Trading
    continues in Zone 2 (30-49); the dashboard shows a warning + manual reset."""
    try:
        return float(score) < MORGAN_FLOOR
    except (TypeError, ValueError):
        return False


def morgan_hard_block(score) -> bool:
    """True when Morgan is below the 30 HARD BLOCK threshold (Zone 3). No new entries;
    Gaius intervention fires. Existing open positions are still managed/exited."""
    try:
        return float(score) < MORGAN_HARD_BLOCK
    except (TypeError, ValueError):
        return False


def last_morgan_reset():
    """UTC timestamp string of the last manual Morgan reset, or None."""
    try:
        with open(_MORGAN_LAST_RESET_PATH) as f:
            return json.load(f).get('reset_utc')
    except Exception:
        return None

# Morgan SHORT confidence (System 2 Review, Change 2/4, 18 Jul 2026) -- a SEPARATE
# confidence that a SHORT scalp must clear (>= 65) before it can execute. Distinct
# from the general Morgan confidence above (which Change 7 left at ~45). Initialised
# at 30 so SHORTs stay blocked until real evidence builds it up to 65. Shared across
# BTC and ETH (crypto SHORT posture is desk-wide, mirroring the BTC-led regime).
_MORGAN_SHORT_STATE_PATH = os.path.join(os.path.dirname(__file__), 'logs',
                                        'morgan_short_confidence.json')
_MORGAN_SHORT_INIT = 30.0
_morgan_short_confidence = None


def get_short_confidence():
    """Return the persisted Morgan SHORT confidence (float), defaulting to 30."""
    global _morgan_short_confidence
    with _morgan_lock:
        if _morgan_short_confidence is None:
            try:
                with open(_MORGAN_SHORT_STATE_PATH) as f:
                    _morgan_short_confidence = float(json.load(f).get('confidence',
                                                                      _MORGAN_SHORT_INIT))
            except Exception:
                _morgan_short_confidence = _MORGAN_SHORT_INIT
        return _morgan_short_confidence


def set_short_confidence(value, reason='update'):
    """Persist a new Morgan SHORT confidence (clamped 0-100)."""
    global _morgan_short_confidence
    with _morgan_lock:
        _morgan_short_confidence = max(0.0, min(100.0, float(value)))
        try:
            os.makedirs(os.path.dirname(_MORGAN_SHORT_STATE_PATH), exist_ok=True)
            with open(_MORGAN_SHORT_STATE_PATH, 'w') as f:
                json.dump({'confidence': _morgan_short_confidence}, f)
        except Exception as e:
            log.warning("Morgan SHORT: could not persist confidence: %s", e)
        log.info("Morgan SHORT: confidence set to %.1f (%s)", _morgan_short_confidence, reason)
        return _morgan_short_confidence

CONFIDENCE_LOG = os.path.join(os.path.dirname(__file__), 'logs', 'morgan_confidence.csv')
CONFIDENCE_FIELDNAMES = ['timestamp', 'confidence', 'level', 'reason']


def save_confidence(confidence, reason='tick'):
    """Append a confidence reading to the persistent CSV history."""
    try:
        os.makedirs(os.path.dirname(CONFIDENCE_LOG), exist_ok=True)
        level = 'HIGH' if confidence >= 65 else ('LOW' if confidence <= 35 else 'MEDIUM')
        row = {'timestamp': datetime.now(timezone.utc).isoformat(),
               'confidence': round(float(confidence), 2), 'level': level, 'reason': reason}
        file_exists = os.path.exists(CONFIDENCE_LOG)
        with open(CONFIDENCE_LOG, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=CONFIDENCE_FIELDNAMES)
            if not file_exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.warning("Morgan: could not save confidence: %s", e)


def load_confidence():
    """Return the last saved confidence (float) or None."""
    if not os.path.exists(CONFIDENCE_LOG):
        return None
    try:
        with open(CONFIDENCE_LOG, 'r') as f:
            rows = list(csv.DictReader(f))
        if rows:
            conf = float(rows[-1]['confidence'])
            log.info("Morgan: restored confidence %.2f from %s", conf, rows[-1]['timestamp'])
            return conf
    except Exception as e:
        log.warning("Morgan: could not load confidence: %s", e)
    return None


def _load_morgan_confidence():
    global _morgan_confidence
    if _morgan_confidence is None:
        try:
            with open(_MORGAN_STATE_PATH) as f:
                _morgan_confidence = float(json.load(f).get('confidence', 50.0))
        except Exception:
            _morgan_confidence = 50.0
    return _morgan_confidence


def get_confidence():
    with _morgan_lock:
        return _load_morgan_confidence()


def set_confidence(value, reason='update'):
    global _morgan_confidence
    with _morgan_lock:
        _morgan_confidence = max(0.0, min(100.0, float(value)))
        try:
            os.makedirs(os.path.dirname(_MORGAN_STATE_PATH), exist_ok=True)
            with open(_MORGAN_STATE_PATH, 'w') as f:
                json.dump({'confidence': _morgan_confidence}, f)
        except Exception as e:
            log.warning("Morgan: could not persist confidence: %s", e)
        log.info("Morgan: confidence set to %.1f", _morgan_confidence)
        save_confidence(_morgan_confidence, reason)
        return _morgan_confidence


def apply_phantom_verdict_feedback(verdict, pnl_1hr, current_confidence):
    """Translate a single phantom verdict into a bounded confidence delta.

    NEUTRAL              -> 0.0 (no individual signal).
    Otherwise the raw magnitude scales with the size of the missed/avoided
    move: raw = clamp(abs(pnl_1hr) / 50, 0.5, 2.0).
      CORRECT (right to stay out) -> +raw
      WRONG   (missed a winner)   -> -raw
    Returns (adjustment, reason).
    """
    verdict = (verdict or '').upper()
    if verdict == 'NEUTRAL':
        reason = "NEUTRAL verdict -- no individual confidence impact"
        log.info("Morgan: %s", reason)
        return 0.0, reason

    try:
        magnitude = abs(float(pnl_1hr))
    except (TypeError, ValueError):
        magnitude = 0.0
    raw = max(0.5, min(2.0, magnitude / 50.0))

    if verdict == 'CORRECT':
        adjustment = raw
        reason = ("CORRECT stay-out (avoided a %.1f move) -> confidence +%.2f"
                  % (magnitude, raw))
    elif verdict == 'WRONG':
        adjustment = -raw
        reason = ("WRONG stay-out (missed a %.1f move) -> confidence -%.2f"
                  % (magnitude, raw))
    else:
        reason = "Unknown verdict '%s' -- no confidence impact" % verdict
        log.info("Morgan: %s", reason)
        return 0.0, reason

    new_conf = max(0.0, min(100.0, current_confidence + adjustment))
    log.info("Morgan: %s (%.1f -> %.1f)", reason, current_confidence, new_conf)
    return adjustment, reason


def process_new_phantom_verdicts(get_confidence_fn=None, set_confidence_fn=None):
    """Start the Morgan phantom-verdict poller.

    Every 5 minutes it drains phantom_tracker.get_unprocessed_verdicts(),
    applies apply_phantom_verdict_feedback() to each, updates the persistent
    Morgan confidence, and marks the rows processed. Runs on a daemon thread.
    """
    import phantom_tracker
    getc = get_confidence_fn or get_confidence
    setc = set_confidence_fn or set_confidence

    def _poll_loop():
        while True:
            try:
                rows = phantom_tracker.get_unprocessed_verdicts()  # CORRECT/WRONG, unprocessed, thread-safe
                if rows:
                    cur = getc()
                    done = []
                    for r in rows:
                        try:
                            pnl = float(r.get('pnl_1hr') or 0)
                        except (ValueError, TypeError):
                            pnl = 0.0
                        adj, _reason = apply_phantom_verdict_feedback(r.get('verdict', ''), pnl, cur)
                        if adj:
                            cur = max(0.0, min(100.0, cur + adj))
                            setc(cur)
                        done.append(r.get('timestamp'))
                    phantom_tracker.mark_processed(done)   # thread-safe, shares _csv_lock
            except Exception as e:
                log.error("Morgan phantom poll error: %s", e)
            time.sleep(300)

    t = threading.Thread(target=_poll_loop, daemon=True, name="MorganPhantomPoller")
    t.start()
    log.info("Morgan: phantom verdict poller started (5-min interval)")
    return t

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).resolve().parent
TRADES_LOG   = BASE_DIR / "logs" / "trades.csv"
LOGS_DIR     = TRADES_LOG.parent
MIN_TRADES   = 5     # minimum trades in a category to report on it
CACHE_TTL    = 300   # re-read trades.csv at most once every 5 minutes

# Trading sessions in UTC hours (inclusive start, exclusive end)
SESSIONS = {
    "Asian":    (0,  8),
    "European": (8,  16),
    "US":       (13, 21),
}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ──────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {"data": None, "path": None, "at": 0.0}


def invalidate_cache() -> None:
    """Force a re-read next time analyse_trade_history() is called."""
    _cache["data"] = None
    _cache["at"]   = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────

def _load_trades(path) -> list:
    """Parse trades.csv into a list of typed dicts. Silently skips bad rows."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < 50:
        return []
    rows = []
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    pnl      = float(row["pnl_gbp"])
                    entry_dt = datetime.strptime(row["entry_time"], "%Y-%m-%d %H:%M:%S")
                    exit_dt  = datetime.strptime(row["exit_time"],  "%Y-%m-%d %H:%M:%S")
                    rows.append({
                        "pnl":          pnl,
                        "win":          pnl >= 0,
                        "direction":    row["direction"],
                        "exit_reason":  row.get("exit_reason", "UNKNOWN"),
                        "entry_time":   entry_dt,
                        "exit_time":    exit_dt,
                        "hold_minutes": max(0.0, (exit_dt - entry_dt).total_seconds() / 60),
                        "entry_hour":   entry_dt.hour,
                        "day_of_week":  DAYS[entry_dt.weekday()],
                        "sessions":     _get_sessions(entry_dt.hour),
                    })
                except Exception:
                    continue
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
    return rows


def _get_sessions(hour: int) -> list:
    """Return list of sessions an hour belongs to (EU/US overlap 13-16 = both)."""
    out = [name for name, (s, e) in SESSIONS.items() if s <= hour < e]
    return out or ["Other"]


# ──────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ──────────────────────────────────────────────────────────────────────────────

def _cat_stats(subset: list) -> dict:
    """Win/loss statistics for an arbitrary subset of trade dicts."""
    n     = len(subset)
    wins  = sum(1 for t in subset if t["win"])
    pnl   = sum(t["pnl"] for t in subset)
    return {
        "trades":   n,
        "wins":     wins,
        "losses":   n - wins,
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "net_pnl":  round(pnl, 2),
    }


def _streaks(trades: list) -> dict:
    """Compute current streak and longest win/loss streaks with dates."""
    if not trades:
        return {
            "current_type": None, "current_count": 0,
            "longest_win":  0, "longest_win_start":  None, "longest_win_end":  None,
            "longest_loss": 0, "longest_loss_start": None, "longest_loss_end": None,
        }

    results = ["WIN" if t["win"] else "LOSS" for t in trades]

    # Current streak (scan backwards from last trade)
    cur_type  = results[-1]
    cur_count = 1
    for r in reversed(results[:-1]):
        if r == cur_type:
            cur_count += 1
        else:
            break

    # Longest streak for a given target
    def _longest(target: str) -> tuple:
        best_len = best_start = best_end = 0
        run_len = run_start = 0
        for i, r in enumerate(results):
            if r == target:
                if run_len == 0:
                    run_start = i
                run_len += 1
                if run_len > best_len:
                    best_len  = run_len
                    best_start = run_start
                    best_end  = i
            else:
                run_len = 0
        return best_len, best_start, best_end

    def _date(idx):
        return trades[idx]["entry_time"].strftime("%Y-%m-%d") if 0 <= idx < len(trades) else None

    wl, ws, we = _longest("WIN")
    ll, ls, le = _longest("LOSS")

    return {
        "current_type":       cur_type,
        "current_count":      cur_count,
        "longest_win":        wl,
        "longest_win_start":  _date(ws),
        "longest_win_end":    _date(we),
        "longest_loss":       ll,
        "longest_loss_start": _date(ls),
        "longest_loss_end":   _date(le),
    }


def _after_loss_stats(trades: list) -> dict:
    """Win rate on trades that immediately follow a losing trade."""
    after = [trades[i] for i in range(1, len(trades)) if not trades[i - 1]["win"]]
    return _cat_stats(after)


def _confidence_score(recent_wr: float, n_trades: int) -> tuple:
    """
    Returns (score 0-100, level_str, very_low_bool).
    With < MIN_TRADES data points, returns neutral (50, MEDIUM, False).

    NOTE (three-zone model, 24 Jul 2026): the third element is the legacy VERY_LOW
    flag and is NO LONGER used as the conservative/hard-block signal. Callers derive
    the hard-block flag from morgan_hard_block(final_score) (<30) after the phantom /
    stay-out adjustments are folded in.
    """
    if n_trades < MIN_TRADES:
        return 50, "MEDIUM", False

    if recent_wr >= 70:
        score = 75 + int((min(recent_wr, 100) - 70) / 30 * 25)
        level = "HIGH"
    elif recent_wr >= 50:
        score = 50 + int((recent_wr - 50) / 20 * 24)
        level = "MEDIUM"
    elif recent_wr >= 30:
        score = 25 + int((recent_wr - 30) / 20 * 24)
        level = "LOW"
    else:
        score = max(0, int(recent_wr / 30 * 24))
        level = "VERY_LOW"

    return min(100, score), level, level == "VERY_LOW"


def get_stay_out_adjustment():
    """Morgan self-improvement: nudge confidence by STAY OUT decision quality.
    >70% correct -> +5 ; <40% correct -> -5 ; 40-70% or <8 judged -> 0.
    Counts only ARTHUR_STAY_OUT rows (hard Lancelot blocks are excluded)."""
    # ── TEMPORARY SUSPENSION -- 10 Jul 2026 ──────────────────────────────
    # Morgan -5/+5 STAY OUT adjustment suspended while the whale hunt
    # false-alarm is investigated and fixed. The hunt model was blocking
    # profitable BTC LONGs during an uptrend (structural flaw confirmed),
    # driving WRONG phantom scores that fired the -5 penalty spiral.
    # Re-enable after the hunt veto is context-gated and validated.
    # TODO: restore the original logic below after the whale hunt fix.
    return 0.0

    # Original logic preserved for restoration:
    # summary = phantom_tracker.get_summary(last_n=10, reason="ARTHUR_STAY_OUT")
    # if summary.get('judged', 0) < 8:
    #     return 0.0
    # quality = summary['quality_score']
    # if quality is None:
    #     return 0.0
    # if quality > 70:
    #     log.info("Morgan: STAY OUT quality %s%% -> confidence +5", quality)
    #     return 5.0
    # if quality < 40:
    #     log.info("Morgan: STAY OUT quality %s%% -> confidence -5", quality)
    #     return -5.0
    # return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Part 1 -- analyse_trade_history
# ──────────────────────────────────────────────────────────────────────────────

def analyse_trade_history(trades_csv_path=None) -> dict:
    """
    Full performance analysis of the complete trade log.
    Results are cached for CACHE_TTL seconds (reset by invalidate_cache()).
    """
    path = Path(trades_csv_path or TRADES_LOG)
    now  = time.monotonic()

    if (_cache["data"] is not None
            and _cache["path"] == str(path)
            and now - _cache["at"] < CACHE_TTL):
        return _cache["data"]

    trades = _load_trades(path)
    n      = len(trades)

    if n == 0:
        result = _empty_stats()
        _cache.update({"data": result, "path": str(path), "at": now})
        return result

    pnls    = [t["pnl"] for t in trades]
    wins_v  = [p for p in pnls if p >= 0]
    losses_v = [p for p in pnls if p < 0]

    # Max consecutive losses
    max_cons = cur_cons = 0
    for p in pnls:
        if p < 0:
            cur_cons += 1
            max_cons  = max(max_cons, cur_cons)
        else:
            cur_cons = 0

    # Recent slices
    recent_10 = trades[-10:]
    r10_stats = _cat_stats(recent_10)
    r5_labels = ["WIN" if t["win"] else "LOSS" for t in trades[-5:]]

    # Confidence
    conf_score, conf_level, conservative = _confidence_score(r10_stats["win_rate"], n)
    conf_score = max(0, min(100, conf_score + get_stay_out_adjustment()))
    # Morgan individual phantom-verdict feedback (neutral 50 adds 0 on a fresh
    # system). Applied on top of the aggregate circuit breaker, not instead of it.
    conf_score = max(0, min(100, conf_score + (get_confidence() - 50.0)))

    # Pattern breakdown by direction
    by_direction = {d: _cat_stats([t for t in trades if t["direction"] == d])
                    for d in ("LONG", "SHORT")}

    # Pattern breakdown by session (trade can appear in 2 sessions if in overlap)
    by_session = {}
    for sess in list(SESSIONS.keys()) + ["Other"]:
        by_session[sess] = _cat_stats([t for t in trades if sess in t["sessions"]])

    # Pattern breakdown by day of week
    by_day = {day: _cat_stats([t for t in trades if t["day_of_week"] == day])
              for day in DAYS}

    # Pattern breakdown by exit reason
    reasons = sorted(set(t["exit_reason"] for t in trades))
    by_exit = {r: _cat_stats([t for t in trades if t["exit_reason"] == r])
               for r in reasons}

    result = {
        # Overall
        "total_trades":     n,
        "win_count":        len(wins_v),
        "loss_count":       len(losses_v),
        "win_rate":         round(len(wins_v) / n * 100, 1),
        "avg_win":          round(sum(wins_v) / len(wins_v), 2) if wins_v else 0.0,
        "avg_loss":         round(sum(losses_v) / len(losses_v), 2) if losses_v else 0.0,
        "best_trade":       round(max(pnls), 2),
        "worst_trade":      round(min(pnls), 2),
        "max_cons_losses":  max_cons,
        "avg_hold_minutes": round(sum(t["hold_minutes"] for t in trades) / n, 1),
        "total_pnl":        round(sum(pnls), 2),
        # Recent form
        "recent_10":        r10_stats,
        "recent_5_labels":  r5_labels,
        # Patterns
        "by_direction":     by_direction,
        "by_session":       by_session,
        "by_day":           by_day,
        "by_exit_reason":   by_exit,
        # Streaks
        "streaks":          _streaks(trades),
        "after_loss":       _after_loss_stats(trades),
        # Confidence
        "confidence_score": conf_score,
        "confidence_level": conf_level,
        # Three-zone Morgan model (24 Jul 2026): the hard-block flag is REDEFINED to
        # <30. Both `conservative_mode` (this module's historic key) and `conservative`
        # (read by Gaius/dashboard) mirror morgan_hard_block(final score) so Gaius fires
        # at <30. morgan_below_floor covers the 30-49 WARNING zone (trading continues).
        "conservative_mode":  morgan_hard_block(conf_score),
        "conservative":       morgan_hard_block(conf_score),
        "morgan_hard_block":  morgan_hard_block(conf_score),
        "morgan_below_floor": morgan_below_floor(conf_score),
        "morgan_raw":         conf_score,
        "morgan_last_reset":  last_morgan_reset(),
    }

    _cache.update({"data": result, "path": str(path), "at": now})
    return result


def _empty_stats() -> dict:
    _empty_score = max(0, min(100, 50 + get_stay_out_adjustment()
                              + (get_confidence() - 50.0)))
    return {
        "total_trades": 0, "win_count": 0, "loss_count": 0, "win_rate": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "max_cons_losses": 0, "avg_hold_minutes": 0.0, "total_pnl": 0.0,
        "recent_10": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0},
        "recent_5_labels": [],
        "by_direction": {}, "by_session": {}, "by_day": {}, "by_exit_reason": {},
        "streaks": {"current_type": None, "current_count": 0,
                    "longest_win": 0, "longest_loss": 0,
                    "longest_win_start": None, "longest_win_end": None,
                    "longest_loss_start": None, "longest_loss_end": None},
        "after_loss": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0},
        "confidence_score": _empty_score,
        "confidence_level": "MEDIUM",
        # Three-zone Morgan model (24 Jul 2026): hard-block flag redefined to <30.
        "conservative_mode":  morgan_hard_block(_empty_score),
        "conservative":       morgan_hard_block(_empty_score),
        "morgan_hard_block":  morgan_hard_block(_empty_score),
        "morgan_below_floor": morgan_below_floor(_empty_score),
        "morgan_raw":         _empty_score,
        "morgan_last_reset":  last_morgan_reset(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Part 2 -- get_performance_context (formatted string for Claude)
# ──────────────────────────────────────────────────────────────────────────────

def get_performance_context(trades_csv_path=None) -> str:
    """Return a formatted performance summary for Claude on every decision."""
    a = analyse_trade_history(trades_csv_path)
    n = a["total_trades"]

    if n == 0:
        return (
            "PERFORMANCE CONTEXT:\n"
            "  No trade history yet -- this is the first trading session.\n"
            "  Confidence score: 50/100 (MEDIUM -- insufficient data)\n"
            "  Apply normal entry criteria."
        )

    score  = a["confidence_score"]
    level  = a["confidence_level"]
    # Three-zone model (24 Jul 2026): Morgan is CONTEXT ONLY above 30 -- it does NOT
    # change the entry threshold. Below 30 the SYSTEM hard-blocks new entries.
    level_descs = {
        "HIGH":     "recent form strong -- normal entry threshold (Zone 1 NORMAL)",
        "MEDIUM":   "adequate form -- normal entry threshold (Zone 1 NORMAL)",
        "LOW":      "below-average form -- WARNING zone (30-49); trade normally, Nick reviews",
        "VERY_LOW": "very low -- at/near HARD BLOCK (<30): system suspends new entries; Gaius intervenes",
    }
    desc = level_descs[level]

    # Streak
    streak   = a["streaks"]
    st_type  = streak.get("current_type")
    st_count = streak.get("current_count", 0)
    streak_str = f"{st_count} consecutive {st_type.lower()}s" if st_type else "none"

    r5 = " ".join(a["recent_5_labels"]) if a["recent_5_labels"] else "n/a"

    zone = ("HARD BLOCK (<30) -- system suspends new entries" if morgan_hard_block(score)
            else "WARNING (30-49) -- trading continues" if morgan_below_floor(score)
            else "NORMAL (>=50)")
    lines = [
        "PERFORMANCE CONTEXT:",
        f"  Confidence score: {score}/100 ({level} -- {desc})",
        f"  Morgan zone:      {zone}",
        f"  Current streak:   {streak_str}",
        f"  Last {len(a['recent_5_labels'])} trades: {r5}",
        "  Morgan is CONTEXT ONLY -- do NOT raise your entry bar while >= 30. The system",
        "  hard-blocks new entries on its own below 30 (Gaius intervenes); existing",
        "  positions are still managed.",
        "",
    ]

    # Build ranked condition list
    overall_wr = a["win_rate"]
    all_conds  = _all_conditions(a)

    strong = [(wr, cnt, lbl) for wr, cnt, lbl in reversed(all_conds)
              if wr > overall_wr + 7][:3]
    weak   = [(wr, cnt, lbl) for wr, cnt, lbl in all_conds
              if wr < overall_wr - 7][:3]

    if strong:
        lines.append("  STRONGEST CONDITIONS IDENTIFIED:")
        for wr, cnt, lbl in strong:
            note = " -- system recovers well" if "after losing" in lbl.lower() and wr > 70 else ""
            lines.append(f"  - {lbl}: {wr:.0f}% win rate ({cnt} trades{note})")
        lines.append("")

    if weak:
        lines.append("  CONDITIONS TO BE CAUTIOUS:")
        for wr, cnt, lbl in weak:
            note = " -- avoid if borderline" if wr < 45 else ""
            lines.append(f"  - {lbl}: {wr:.0f}% win rate ({cnt} trades{note})")
        lines.append("")

    # Recent form vs overall
    r10       = a["recent_10"]
    r10_n     = r10["trades"]
    r10_wr    = r10["win_rate"]
    r10_wins  = r10["wins"]
    r10_losses = r10["losses"]

    if r10_n >= 5:
        diff = r10_wr - overall_wr
        if diff > 5:
            form_note = "Recent form ABOVE average -- maintain current approach"
        elif diff < -5:
            form_note = "Recent form BELOW average -- be more selective"
        else:
            form_note = "Recent form ON PAR with overall average"

        lines += [
            "  RECENT FORM NOTE:",
            f"  Last {r10_n} trades: {r10_wins} wins {r10_losses} losses ({r10_wr:.0f}% win rate)",
            f"  Overall win rate:  {overall_wr:.1f}%",
            f"  {form_note}",
        ]

    if a["conservative_mode"]:
        lines += [
            "",
            "  *** MORGAN HARD BLOCK (score < 30) ***",
            "  The SYSTEM suspends new entries and Gaius intervenes -- you will not be",
            "  asked to enter here. Existing positions are still managed/exited. This is",
            "  enforced automatically; do NOT otherwise change how you assess setups.",
        ]

    return "\n".join(lines)


def _all_conditions(a: dict) -> list:
    """Build sorted (ascending win_rate) list of (win_rate, count, label) tuples."""
    conds = []
    for d, s in a["by_direction"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{d} trades"))
    for sess, s in a["by_session"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{sess} session"))
    for day, s in a["by_day"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{day}s"))
    al = a["after_loss"]
    if al["trades"] >= MIN_TRADES:
        conds.append((al["win_rate"], al["trades"], "After losing trade"))
    conds.sort(key=lambda x: x[0])
    return conds


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard helper
# ──────────────────────────────────────────────────────────────────────────────

def get_perf_dashboard_dict(trades_csv_path=None) -> dict:
    """Return a compact dict for the dashboard performance widget."""
    a = analyse_trade_history(trades_csv_path)
    streak = a["streaks"]
    _score = a["confidence_score"]
    return {
        "confidence_score": _score,
        "confidence_level": a["confidence_level"],
        "streak_type":      streak.get("current_type"),
        "streak_count":     streak.get("current_count", 0),
        "recent_5":         a["recent_5_labels"],
        "win_rate":         a["win_rate"],
        "total_trades":     a["total_trades"],
        # Three-zone Morgan model (24 Jul 2026): hard-block flag is <30. Gaius and the
        # dashboard read "conservative"; expose "conservative_mode" too, plus the raw
        # score, WARNING-zone flag and last-reset stamp for the dashboard panel.
        "conservative":      morgan_hard_block(_score),
        "conservative_mode": morgan_hard_block(_score),
        "morgan_hard_block":  morgan_hard_block(_score),
        "morgan_below_floor": morgan_below_floor(_score),
        "morgan_raw":         _score,
        "morgan_last_reset":  last_morgan_reset(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Part 3 -- Milestone review (every 50 trades)
# ──────────────────────────────────────────────────────────────────────────────

def generate_milestone_review(trades_csv_path=None, milestone_number: int = 1) -> Optional[str]:
    """
    Generate a 50-trade milestone report and save to logs/ace_review_NN.txt.
    Returns the output file path, or None if no trades in the block.
    """
    path   = Path(trades_csv_path or TRADES_LOG)
    trades = _load_trades(path)

    block_start = (milestone_number - 1) * 50
    curr_block  = trades[block_start: block_start + 50]
    prev_block  = trades[max(0, block_start - 50): block_start] if block_start >= 50 else []

    if not curr_block:
        log.warning("No trades found for milestone %d block", milestone_number)
        return None

    curr     = _analyse_block(curr_block)
    prev     = _analyse_block(prev_block) if prev_block else None
    alltime  = _analyse_block(trades)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOGS_DIR / f"ace_review_{milestone_number:02d}.txt"
    lines    = _build_report(milestone_number, curr, prev, alltime)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    log.info("Milestone review #%d saved: %s", milestone_number, out_path)
    return str(out_path)


def _analyse_block(trades: list) -> dict:
    """Compute stats for a specific block of trades."""
    if not trades:
        return {}

    pnls    = [t["pnl"] for t in trades]
    wins_v  = [p for p in pnls if p >= 0]
    losses_v = [p for p in pnls if p < 0]
    n       = len(trades)

    by_dir  = {d: _cat_stats([t for t in trades if t["direction"] == d])
                for d in ("LONG", "SHORT")}
    by_sess = {s: _cat_stats([t for t in trades if s in t["sessions"]])
                for s in SESSIONS}
    by_day  = {day: _cat_stats([t for t in trades if t["day_of_week"] == day])
                for day in DAYS}

    return {
        "n":          n,
        "win_rate":   round(len(wins_v) / n * 100, 1),
        "avg_win":    round(sum(wins_v) / len(wins_v), 2) if wins_v else 0.0,
        "avg_loss":   round(sum(losses_v) / len(losses_v), 2) if losses_v else 0.0,
        "net_pnl":    round(sum(pnls), 2),
        "best":       round(max(pnls), 2),
        "worst":      round(min(pnls), 2),
        "by_dir":     by_dir,
        "by_sess":    by_sess,
        "by_day":     by_day,
        "after_loss": _after_loss_stats(trades),
        "date_start": trades[0]["entry_time"].strftime("%Y-%m-%d"),
        "date_end":   trades[-1]["exit_time"].strftime("%Y-%m-%d"),
    }


def _build_report(num: int, curr: dict, prev: Optional[dict], alltime: dict) -> list:
    """Build the milestone review text lines."""
    generated  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blk_start  = (num - 1) * 50 + 1
    blk_end    = num * 50
    hr = "=" * 60

    lines = [
        hr,
        f"  CryptoHybrid AI -- Ace's Milestone Review #{num}",
        f"  Trades {blk_start}-{blk_end}  |  Generated: {generated}",
        hr, "",
    ]

    # ── Performance summary ────────────────────────────────────────────────
    lines += [
        f"PERFORMANCE SUMMARY (Trades {blk_start}-{blk_end})",
        f"  Period:         {curr['date_start']}  to  {curr['date_end']}",
        f"  Total trades:   {curr['n']}",
        f"  Win rate:       {curr['win_rate']:.1f}%",
        f"  Average win:    GBP +{curr['avg_win']:.2f}",
        f"  Average loss:   GBP {curr['avg_loss']:.2f}",
        f"  Best trade:     GBP +{curr['best']:.2f}",
        f"  Worst trade:    GBP {curr['worst']:.2f}",
        f"  Net P&L:        GBP {curr['net_pnl']:+.2f}",
        "",
    ]

    # ── Comparison to previous block ───────────────────────────────────────
    if prev:
        prev_s = (num - 2) * 50 + 1
        prev_e = (num - 1) * 50
        wr_diff  = curr["win_rate"] - prev["win_rate"]
        pnl_diff = curr["net_pnl"]  - prev["net_pnl"]
        trend    = "IMPROVING" if wr_diff > 3 else "DECLINING" if wr_diff < -3 else "STABLE"
        lines += [
            f"COMPARISON TO PREVIOUS BLOCK (Trades {prev_s}-{prev_e})",
            f"  Win rate:  {prev['win_rate']:.1f}%  ->  {curr['win_rate']:.1f}%  ({wr_diff:+.1f}%)",
            f"  Net P&L:   GBP {prev['net_pnl']:+.2f}  ->  GBP {curr['net_pnl']:+.2f}",
            f"  Trend:     {trend}",
            "",
        ]
    else:
        lines += [
            "COMPARISON TO PREVIOUS BLOCK",
            "  First milestone block -- no prior comparison available.",
            "",
        ]

    # ── Ranked conditions for this block ────────────────────────────────────
    conds = []
    for d, s in curr["by_dir"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{d} trades"))
    for sess, s in curr["by_sess"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{sess} session"))
    for day, s in curr["by_day"].items():
        if s["trades"] >= MIN_TRADES:
            conds.append((s["win_rate"], s["trades"], f"{day}s"))
    al = curr["after_loss"]
    if al["trades"] >= MIN_TRADES:
        conds.append((al["win_rate"], al["trades"], "Trades after a loss"))
    conds.sort(key=lambda x: x[0], reverse=True)

    lines.append("TOP 3 STRONGEST PATTERNS:")
    if conds:
        for i, (wr, cnt, lbl) in enumerate(conds[:3], 1):
            lines.append(f"  {i}. {lbl}: {wr:.0f}% win rate ({cnt} trades)")
    else:
        lines.append("  Insufficient data (<5 trades in any single category)")
    lines.append("")

    lines.append("TOP 3 PATTERNS TO AVOID:")
    weak = [c for c in reversed(conds) if c[0] < curr["win_rate"] - 7]
    if weak:
        for i, (wr, cnt, lbl) in enumerate(weak[:3], 1):
            lines.append(f"  {i}. {lbl}: {wr:.0f}% win rate ({cnt} trades)")
    else:
        lines.append("  No conditions significantly below average this block.")
    lines.append("")

    # ── Ace's self-assessment ──────────────────────────────────────────────
    lines += _self_assessment(num, curr, prev, conds)
    lines.append("")

    # ── Parameter suggestions ──────────────────────────────────────────────
    lines += _parameter_suggestions(curr)
    lines += ["", hr, "  End of Milestone Review", hr]
    return lines


def _self_assessment(num: int, curr: dict, prev: Optional[dict], conds: list) -> list:
    wr  = curr["win_rate"]
    net = curr["net_pnl"]
    al  = curr["after_loss"]

    trend_note = ""
    if prev:
        diff = wr - prev["win_rate"]
        if diff > 3:
            trend_note = ", improving on my previous block"
        elif diff < -3:
            trend_note = ", declining compared to my previous block"
        else:
            trend_note = ", consistent with my previous block"

    lines = ["ACE'S SELF-ASSESSMENT:", ""]
    perf_qual = "strong" if wr >= 65 else "acceptable" if wr >= 50 else "below target"
    lines.append(
        f"In this {curr['n']}-trade period I achieved a {wr:.1f}% win rate "
        f"and a net P&L of GBP {net:+.2f}{trend_note}. "
        f"Overall I consider this {perf_qual} performance."
    )

    if conds:
        best  = conds[0]
        worst = conds[-1]
        lines.append(
            f"My strongest performance came from {best[2]} "
            f"({best[0]:.0f}% win rate, {best[1]} trades)."
        )
        if worst[0] < wr - 7:
            lines.append(
                f"I consistently underperformed with {worst[2]} "
                f"({worst[0]:.0f}% win rate), which I will factor into future entries."
            )

    if al["trades"] >= 5:
        if al["win_rate"] >= 70:
            lines.append(
                f"Notably, my recovery after losses was strong "
                f"({al['win_rate']:.0f}% win rate on {al['trades']} post-loss trades), "
                f"which suggests my system adapts well to adversity."
            )
        elif al["win_rate"] < 40:
            lines.append(
                f"My performance after a losing trade was poor "
                f"({al['win_rate']:.0f}% on {al['trades']} trades), "
                f"suggesting additional caution is warranted following losses."
            )

    lines.append("")
    lines.append("My confidence is highest when:")
    if conds:
        for wr_c, cnt, lbl in conds[:2]:
            lines.append(f"  - Trading {lbl} ({wr_c:.0f}% win rate)")
    else:
        lines.append("  - Data still building -- more trades needed for strong conclusions.")

    lines.append("")
    lines.append("I would recommend reviewing:")

    recoms = []
    if wr < 50:
        recoms.append(
            f"Entry criteria -- {wr:.0f}% win rate is below the 50% breakeven threshold. "
            f"Consider requiring stronger indicator confluence."
        )
    long_s  = curr["by_dir"].get("LONG",  {})
    short_s = curr["by_dir"].get("SHORT", {})
    if long_s.get("trades", 0) >= 5 and long_s.get("win_rate", 50) < 40:
        recoms.append(
            f"LONG entry conditions -- only {long_s['win_rate']:.0f}% win rate "
            f"on {long_s['trades']} long trades this block."
        )
    if short_s.get("trades", 0) >= 5 and short_s.get("win_rate", 50) < 40:
        recoms.append(
            f"SHORT entry conditions -- only {short_s['win_rate']:.0f}% win rate "
            f"on {short_s['trades']} short trades this block."
        )
    if not recoms:
        recoms.append(
            "Nothing critical this block. Current approach is working. "
            "Maintain discipline, avoid overtrading, and trust the indicators."
        )
    for r in recoms:
        lines.append(f"  - {r}")

    return lines


def _parameter_suggestions(curr: dict) -> list:
    """Generate specific parameter suggestions if data is compelling."""
    lines = ["PARAMETER SUGGESTIONS (based on this block's data):"]
    suggestions = []

    long_s  = curr["by_dir"].get("LONG",  {})
    short_s = curr["by_dir"].get("SHORT", {})

    # LONG/SHORT imbalance
    if (short_s.get("trades", 0) >= 10 and long_s.get("trades", 0) >= 10
            and short_s["win_rate"] > 75 and long_s["win_rate"] < 40):
        suggestions.append(
            f"Temporarily disable LONG entries. "
            f"Evidence: SHORT {short_s['win_rate']:.0f}% WR ({short_s['trades']} trades) "
            f"vs LONG {long_s['win_rate']:.0f}% WR ({long_s['trades']} trades). "
            f"The system is finding far better short opportunities this period."
        )

    # Asian session underperformance
    asian = curr["by_sess"].get("Asian", {})
    if asian.get("trades", 0) >= 10 and asian.get("win_rate", 50) < 35:
        suggestions.append(
            f"Avoid Asian session (00:00-08:00 UTC). "
            f"Evidence: {asian['win_rate']:.0f}% win rate over {asian['trades']} trades. "
            f"Low liquidity may be causing unfavourable entries."
        )

    # Strong post-loss recovery suggests cooldown is counterproductive
    al = curr["after_loss"]
    if al.get("trades", 0) >= 10 and al["win_rate"] > 75:
        suggestions.append(
            f"Consider reducing the post-loss cooldown period. "
            f"Evidence: {al['win_rate']:.0f}% win rate on {al['trades']} trades "
            f"immediately after losses. The system recovers strongly -- "
            f"a cooldown may be forfeiting winning entries."
        )

    if suggestions:
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  Suggestion {i}: {s}")
    else:
        lines.append("  No parameter changes indicated by this block.")
        lines.append("  Continue with current settings.")

    return lines


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _log
    _log.Formatter.converter = time.gmtime  # ALBION RULE: emit log timestamps in UTC
    _log.basicConfig(level=_log.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                     datefmt="%Y-%m-%d %H:%M:%S UTC")

    print("\n=== CryptoHybrid AI -- Performance Engine Self-Test ===\n")
    path = TRADES_LOG
    print(f"Trades log: {path}")
    print(f"Exists: {path.exists()}\n")

    a = analyse_trade_history(path)
    print(f"Total trades: {a['total_trades']}")
    print(f"Win rate:     {a['win_rate']:.1f}%")
    print(f"Confidence:   {a['confidence_score']}/100 ({a['confidence_level']})")
    print(f"Streak:       {a['streaks'].get('current_count', 0)} "
          f"{a['streaks'].get('current_type', 'N/A')}")
    print()
    print(get_performance_context(path))
