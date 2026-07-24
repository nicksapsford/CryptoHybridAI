"""
CryptoHybrid AI -- dashboard_btc.py
Two-page browser dashboard at http://localhost:5041
Page 1: Live trading view (fits 1920x1080, no scroll).
Page 2: P&L, performance detail, monthly breakdown, full trade history.
Uses Response() to avoid Jinja2 template conflicts.
All JS uses string concatenation -- no template literals.
"""

import csv
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Semantic version -- read from the VERSION file so the header always shows the
# current version without a code change.
_VER = BASE_DIR / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"


def get_git_hash():
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        return result.stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


VERSION_STRING = "v" + str(APP_VERSION) + " (" + get_git_hash() + ")"


def get_stay_out_quality():
    # ALBION RULE: phantom_trades.csv timestamps are UTC — never BST/local.
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'phantom_trades.csv')
    if not os.path.exists(csv_path):
        return {'status': 'No data yet', 'decisions': [], 'quality_score': None,
                'net_saved': None, 'correct': 0, 'wrong': 0, 'neutral': 0}
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        last_20 = rows[-50:]
        correct = sum(1 for r in last_20 if r.get('verdict') == 'CORRECT')
        wrong   = sum(1 for r in last_20 if r.get('verdict') == 'WRONG')
        neutral = sum(1 for r in last_20 if r.get('verdict') == 'NEUTRAL')
        total   = (correct + wrong + neutral)
        quality_score = round((correct / total) * 100) if total else 0
        net_saved  = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_20 if r.get('verdict') == 'CORRECT')
        net_missed = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_20 if r.get('verdict') == 'WRONG')
        return {'status': 'ok', 'decisions': last_20, 'quality_score': quality_score,
                'net_saved': net_saved, 'net_missed': net_missed, 'correct': correct, 'wrong': wrong, 'neutral': neutral}
    except Exception as e:
        return {'status': 'Error: ' + str(e), 'decisions': []}


load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)

log = logging.getLogger("CryptoHybrid.Dashboard")
# ALBION STANDING RULE: all log timestamps are UTC (never BST/local). See main_cryptohybrid.py.
logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.WARNING)

PORT             = 5041
REFRESH_SECONDS  = 30
LOG_DIR          = BASE_DIR / "logs"
TRADES_LOG       = LOG_DIR / "trades.csv"
ETH_TRADES_LOG   = LOG_DIR / "eth_trades.csv"
SHUTDOWN_FLAG    = LOG_DIR / "shutdown.flag"
STARTING_CAPITAL = 1_000.0
LOGO_PNG         = BASE_DIR / "tidetrader_logo.png"

app      = Flask(__name__)
_state:     dict = {"panel_mode": "pre_checks"}
_eth_state: dict = {"panel_mode": "pre_checks", "pair": "ETH"}
_lock  = threading.Lock()


def _try_convert_logo() -> None:
    if LOGO_PNG.exists():
        return
    candidates = [
        BASE_DIR / "tidetrader.ico",
    ]
    ico_path = next((p for p in candidates if p.exists()), None)
    if not ico_path:
        log.info("tidetrader.ico not found -- logo will not display")
        return
    try:
        from PIL import Image
        img = Image.open(ico_path).convert("RGBA")
        img = img.resize((64, 64), Image.LANCZOS)
        img.save(str(LOGO_PNG), "PNG")
        log.info("Logo converted: %s", LOGO_PNG)
    except ImportError:
        log.warning("Pillow not installed -- logo unavailable (pip install pillow)")
    except Exception as e:
        log.warning("Logo conversion failed: %s", e)


def set_state(data: dict) -> None:
    with _lock:
        if data.get("pair") == "ETH":
            _eth_state.update(data)
            _eth_state["last_update_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        else:
            _state.update(data)
            _state["last_update_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def get_state() -> dict:
    with _lock:
        return dict(_state)


def get_eth_state() -> dict:
    with _lock:
        return dict(_eth_state)


def compute_status_fields(state: dict) -> dict:
    """Derive the flat Lancelot / Arthur / locked_pnl summary fields from a
    pushed state dict (works for both the BTC top-level state and the ETH
    nested state). Wrapped in try/except with safe defaults so /api/state
    can never 500 on a malformed payload."""
    defaults = {
        "lancelot_status":       "--",
        "lancelot_fails":        0,
        "lancelot_fail_reasons": [],
        "arthur_decision":       "---",
        "arthur_confidence":     None,
        "arthur_consulted":      False,
        "locked_pnl":            None,
    }
    try:
        panel_mode   = state.get("panel_mode", "pre_checks")
        raw_decision = state.get("decision", "STAY_OUT")
        pre_checks   = state.get("pre_checks", {}) or {}
        block_reason = state.get("block_reason", "")
        position     = state.get("position") or state.get("current_trade")

        # decision is normally a string in the pushed state, but handle a raw
        # Arthur decision dict defensively too.
        if isinstance(raw_decision, dict):
            decision_code = str(raw_decision.get("decision", "STAY_OUT"))
            raw_conf      = raw_decision.get("confidence")
        else:
            decision_code = str(raw_decision)
            raw_conf      = state.get("confidence")

        # arthur_consulted: pre-checks passed and Arthur was reached. The main
        # loop sets panel_mode="claude" once it hands off to Arthur; a hard
        # Lancelot block leaves panel_mode="pre_checks" with a block reason.
        consulted = (panel_mode == "claude")
        if not consulted and isinstance(raw_decision, dict) and not block_reason:
            consulted = True

        # Lancelot pre-check fails (values are True / False / None).
        fails   = [name for name, val in pre_checks.items() if val is False]
        n_fails = len(fails)

        if consulted:
            lancelot_status = "CLEAR"
        elif n_fails > 0:
            lancelot_status = f"{n_fails} FAILS"
        else:
            lancelot_status = "BLOCKED"

        # Arthur decision label.
        if not consulted:
            arthur_decision = "---"
        elif position:
            arthur_decision = "HOLD"
        else:
            code = decision_code.upper()
            _map = {
                "ENTER_LONG":  "LONG",
                "ENTER_SHORT": "SHORT",
                "STAY_OUT":    "STAY OUT",
                "HOLD":        "HOLD",
            }
            if code in _map:
                arthur_decision = _map[code]
            elif code.startswith("EXIT"):
                arthur_decision = "STAY OUT"
            else:
                arthur_decision = "STAY OUT"

        # Arthur confidence -> int, mapping level strings if numeric unavailable.
        arthur_confidence = None
        if consulted and raw_conf not in (None, "", "--"):
            try:
                arthur_confidence = int(float(raw_conf))
            except (TypeError, ValueError):
                _cmap = {"HIGH": 75, "MEDIUM": 50, "LOW": 30}
                arthur_confidence = _cmap.get(str(raw_conf).strip().upper())

        # locked_pnl: realised P&L if the open trade is stopped out, using the
        # same percentage-of-entry convention that produces trade.pnl_gbp
        # (pnl_gbp = position_size_gbp * pct).
        locked_pnl = None
        if position:
            try:
                entry = float(position.get("entry_price"))
                stop  = float(position.get("stop_loss"))
                stake = float(position.get("position_size_gbp",
                                           position.get("stake", 0.0)) or 0.0)
                direction = str(position.get("direction", "LONG")).upper()
                # Bug C: only surface a Locked figure once the trailing stop has
                # trailed to break-even (genuine secured profit); until then None -> "---".
                if entry and ((direction == "LONG" and stop >= entry) or
                              (direction == "SHORT" and stop <= entry)):
                    pct = ((stop - entry) if direction == "LONG"
                           else (entry - stop)) / entry
                    locked_pnl = round(stake * pct, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                locked_pnl = None

        return {
            "lancelot_status":       lancelot_status,
            "lancelot_fails":        n_fails,
            "lancelot_fail_reasons": fails,
            "arthur_decision":       arthur_decision,
            "arthur_confidence":     arthur_confidence,
            "arthur_consulted":      consulted,
            "locked_pnl":            locked_pnl,
        }
    except Exception:
        return dict(defaults)


def _fmt_exc(row, col: str) -> str:
    """Format a MAE/MFE cell (GBP) from a trade row; '--' if absent/blank (old rows)."""
    try:
        v = row.get(col)
        if v is None or str(v).strip() in ("", "nan"):
            return "--"
        return f"{float(v):+.2f}"
    except Exception:
        return "--"


def load_trades() -> list:
    """Load ALL trades from CSV, most recent first."""
    if not TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return []
        trades = []
        for _, row in df.iterrows():
            pnl = float(row["pnl_gbp"])
            trades.append({
                "direction":   row["direction"],
                "entry_time":  row["entry_time"],
                "exit_time":   row["exit_time"],
                "entry_price": f"{float(row['entry_price']):,.2f}",
                "exit_price":  f"{float(row['exit_price']):,.2f}",
                "pnl":         f"{pnl:+.2f}",
                "pnl_class":   "win" if pnl >= 0 else "loss",
                "reason":      row["exit_reason"],
                "size":        f"{float(row['position_size_gbp']):,.2f}",
                "mae":         _fmt_exc(row, "mae_gbp"),
                "mfe":         _fmt_exc(row, "mfe_gbp"),
            })
        return list(reversed(trades))
    except Exception:
        return []


def load_account_stats() -> dict:
    empty = {
        "capital": STARTING_CAPITAL, "total_pnl": 0.0,
        "total_return": 0.0, "total_trades": 0,
        "winners": 0, "losers": 0, "win_rate": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0, "daily_pnl": 0.0,
        "killed": False, "kill_reason": "",
    }
    if not TRADES_LOG.exists():
        return empty
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return empty
        capital      = float(df["capital_after"].iloc[-1])
        pnls         = df["pnl_gbp"].astype(float)
        total_pnl    = capital - STARTING_CAPITAL
        total_return = (capital / STARTING_CAPITAL - 1) * 100
        winners      = len(pnls[pnls > 0])
        losers       = len(pnls[pnls < 0])
        total        = len(pnls)
        win_rate     = (winners / total * 100) if total > 0 else 0
        today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_df     = df[df["date"] == today] if "date" in df.columns else df
        daily_pnl    = today_df["pnl_gbp"].astype(float).sum() if not today_df.empty else 0.0
        return {
            "capital": capital, "total_pnl": total_pnl,
            "total_return": total_return, "total_trades": total,
            "winners": winners, "losers": losers, "win_rate": win_rate,
            "best_trade": float(pnls.max()) if total > 0 else 0.0,
            "worst_trade": float(pnls.min()) if total > 0 else 0.0,
            "daily_pnl": daily_pnl,
            "killed": False, "kill_reason": "",
        }
    except Exception:
        return empty


def load_monthly_stats() -> list:
    """Group trades by calendar month for the Page 2 breakdown table."""
    if not TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return []
        df["pnl_gbp"] = df["pnl_gbp"].astype(float)
        df["_dt"]     = pd.to_datetime(df["entry_time"], errors="coerce")
        df["_mk"]     = df["_dt"].dt.strftime("%Y-%m")
        df["_ml"]     = df["_dt"].dt.strftime("%b %Y")
        monthly = []
        for mk, grp in df.groupby("_mk"):
            pnls  = grp["pnl_gbp"]
            wins  = int(len(pnls[pnls > 0]))
            total = int(len(pnls))
            gross = round(float(pnls.sum()), 2)
            monthly.append({
                "month":     grp["_ml"].iloc[0],
                "trades":    total,
                "wins":      wins,
                "win_rate":  round(wins / total * 100, 1) if total > 0 else 0.0,
                "gross_pnl": gross,
                "net_pnl":   gross,
            })
        monthly.sort(key=lambda x: x["month"])
        return monthly
    except Exception:
        return []


def load_direction_session_stats() -> dict:
    """Win rates split by direction (LONG/SHORT) and session (Asian/European/US)."""
    empty = {"direction": {}, "session": {}}
    if not TRADES_LOG.exists():
        return empty
    try:
        df = pd.read_csv(TRADES_LOG)
        if df.empty:
            return empty
        df["pnl_gbp"] = df["pnl_gbp"].astype(float)
        df["_dt"]     = pd.to_datetime(df["entry_time"], errors="coerce")
        df["_hour"]   = df["_dt"].dt.hour

        def _session(h):
            if pd.isna(h):
                return "Unknown"
            h = int(h)
            return "Asian" if h < 8 else ("European" if h < 16 else "US")

        df["_session"] = df["_hour"].apply(_session)

        direction = {}
        for d_name in ["LONG", "SHORT"]:
            sub = df[df["direction"] == d_name]
            if len(sub) == 0:
                continue
            wins = int(len(sub[sub["pnl_gbp"] > 0]))
            direction[d_name] = {
                "trades":   int(len(sub)),
                "wins":     wins,
                "win_rate": round(wins / len(sub) * 100, 1),
            }

        session = {}
        for s_name in ["Asian", "European", "US"]:
            sub = df[df["_session"] == s_name]
            if len(sub) == 0:
                continue
            wins = int(len(sub[sub["pnl_gbp"] > 0]))
            session[s_name] = {
                "trades":   int(len(sub)),
                "wins":     wins,
                "win_rate": round(wins / len(sub) * 100, 1),
            }

        return {"direction": direction, "session": session}
    except Exception:
        return empty


def load_eth_trades() -> list:
    """Load ETH trades from eth_trades.csv, most recent first."""
    if not ETH_TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(ETH_TRADES_LOG)
        if df.empty:
            return []
        trades = []
        for _, row in df.iterrows():
            pnl = float(row["pnl_gbp"])
            trades.append({
                "direction":   row["direction"],
                "entry_time":  row["entry_time"],
                "exit_time":   row["exit_time"],
                "entry_price": f"{float(row['entry_price']):,.4f}",
                "exit_price":  f"{float(row['exit_price']):,.4f}",
                "pnl":         f"{pnl:+.2f}",
                "pnl_class":   "win" if pnl >= 0 else "loss",
                "reason":      row["exit_reason"],
                "size":        f"{float(row['position_size_gbp']):,.2f}",
                "mae":         _fmt_exc(row, "mae_gbp"),
                "mfe":         _fmt_exc(row, "mfe_gbp"),
            })
        return list(reversed(trades))
    except Exception:
        return []


def load_eth_account_stats() -> dict:
    empty = {
        "capital": STARTING_CAPITAL, "total_pnl": 0.0,
        "total_return": 0.0, "total_trades": 0,
        "winners": 0, "losers": 0, "win_rate": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0, "daily_pnl": 0.0,
    }
    if not ETH_TRADES_LOG.exists():
        return empty
    try:
        df = pd.read_csv(ETH_TRADES_LOG)
        if df.empty:
            return empty
        capital      = float(df["capital_after"].iloc[-1])
        pnls         = df["pnl_gbp"].astype(float)
        total_pnl    = capital - STARTING_CAPITAL
        total_return = (capital / STARTING_CAPITAL - 1) * 100
        winners      = len(pnls[pnls > 0])
        losers       = len(pnls[pnls < 0])
        total        = len(pnls)
        win_rate     = (winners / total * 100) if total > 0 else 0
        today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_df     = df[df["date"] == today] if "date" in df.columns else df
        daily_pnl    = today_df["pnl_gbp"].astype(float).sum() if not today_df.empty else 0.0
        return {
            "capital": capital, "total_pnl": total_pnl,
            "total_return": total_return, "total_trades": total,
            "winners": winners, "losers": losers, "win_rate": win_rate,
            "best_trade": float(pnls.max()) if total > 0 else 0.0,
            "worst_trade": float(pnls.min()) if total > 0 else 0.0,
            "daily_pnl": daily_pnl,
        }
    except Exception:
        return empty


def load_eth_monthly_stats() -> list:
    if not ETH_TRADES_LOG.exists():
        return []
    try:
        df = pd.read_csv(ETH_TRADES_LOG)
        if df.empty:
            return []
        df["pnl_gbp"] = df["pnl_gbp"].astype(float)
        df["_dt"]     = pd.to_datetime(df["entry_time"], errors="coerce")
        df["_mk"]     = df["_dt"].dt.strftime("%Y-%m")
        df["_ml"]     = df["_dt"].dt.strftime("%b %Y")
        monthly = []
        for mk, grp in df.groupby("_mk"):
            pnls  = grp["pnl_gbp"]
            wins  = int(len(pnls[pnls > 0]))
            total = int(len(pnls))
            gross = round(float(pnls.sum()), 2)
            monthly.append({
                "month":     grp["_ml"].iloc[0],
                "trades":    total,
                "wins":      wins,
                "win_rate":  round(wins / total * 100, 1) if total > 0 else 0.0,
                "gross_pnl": gross,
                "net_pnl":   gross,
            })
        monthly.sort(key=lambda x: x["month"])
        return monthly
    except Exception:
        return []


# ---------------------------------------------------------------------------
# HTML -- two-page dashboard
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoHybrid A.I. &mdash; BTC &amp; ETH / GBP</title>
<style>
:root{
  --bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;
  --teal:#00b4d8;--green:#3fb950;--red:#f85149;--yellow:#d29922;
  --text:#e6edf3;--muted:#8b949e;--purple:#bc8cff;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;display:flex;flex-direction:column;}

/* HEADER */
.header{background:var(--bg2);border-bottom:2px solid var(--teal);padding:7px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;height:46px;}
.header-brand{display:flex;align-items:center;gap:8px;}
.logo{font-size:17px;font-weight:700;color:var(--teal);letter-spacing:1px;}
.logo span{color:var(--text);}
.subtitle{color:var(--muted);font-size:10px;margin-top:1px;}
.header-right{display:flex;align-items:center;gap:10px;}
.clock{font-size:15px;font-weight:600;color:var(--teal);font-family:monospace;}
.feed-status{font-size:10px;color:var(--muted);display:flex;align-items:center;gap:4px;}
.header-price{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 20px;border-left:1px solid var(--border);border-right:1px solid var(--border);}
.hdr-price-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:1px;}
.hdr-price-val{font-size:22px;font-weight:700;color:var(--teal);font-family:monospace;letter-spacing:1px;}
.feed-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.dot-ok  {background:var(--green);box-shadow:0 0 4px var(--green);animation:pulse 2s infinite;}
.dot-warn{background:var(--yellow);box-shadow:0 0 4px var(--yellow);}
.dot-err {background:var(--red);box-shadow:0 0 4px var(--red);}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* BUTTONS */
.shutdown-btn{background:rgba(248,81,73,0.08);border:1px solid var(--red);color:var(--red);padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;transition:background 0.15s;}
.shutdown-btn:hover{background:rgba(248,81,73,0.25);}
.nav-btn{background:rgba(0,180,216,0.15);border:1px solid var(--teal);color:var(--teal);padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;letter-spacing:0.3px;transition:background 0.15s;}
.nav-btn:hover{background:rgba(0,180,216,0.32);}
/* Phantom Trades page (page 3) + compact Stay Out Quality (Job 2) */
.phantom-page{flex:1;overflow-y:auto;max-width:900px;width:100%;margin:0 auto;padding:16px 20px;display:flex;flex-direction:column;gap:14px;}
.phantom-head{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.phantom-summary{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:13px;line-height:1.9;}
.phantom-summary .ps-q{color:var(--teal);font-weight:700;}
.phantom-scroll{max-height:600px;overflow:auto;}
.phantom-table td.ph-na{color:var(--muted,#888);}
.phantom-table{width:100%;border-collapse:collapse;font-size:12px;}
.phantom-table th{text-align:left;color:var(--muted);font-weight:600;padding:6px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
.phantom-table td{padding:6px 10px;border-bottom:1px solid rgba(255,255,255,0.05);white-space:nowrap;}
.phantom-table tr:hover td{background:rgba(255,255,255,0.02);}
.v-correct{color:var(--green);font-weight:700;}
.v-wrong{color:var(--red);font-weight:700;}
.v-neutral{color:var(--muted);font-weight:700;}
.v-pending{color:var(--yellow);font-weight:700;}
#soqCompact{cursor:pointer;transition:background 0.15s;}
#soqCompact:hover{background:rgba(255,255,255,0.03);}
.soq-hint{margin-top:6px;font-size:9px;color:var(--muted);letter-spacing:0.4px;}

/* SHUTDOWN MODAL */
.modal-overlay{display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.78);justify-content:center;align-items:center;}
.modal-overlay.open{display:flex;}
.modal{background:var(--bg2);border:2px solid var(--red);border-radius:10px;padding:22px 28px;max-width:380px;text-align:center;}
.modal h3{color:var(--red);font-size:15px;margin-bottom:10px;}
.modal p{color:var(--muted);font-size:12px;line-height:1.5;margin-bottom:6px;}
.modal-trade-warn{background:rgba(248,81,73,0.1);border:1px solid var(--red);border-radius:5px;padding:8px;margin:10px 0;color:var(--red);font-size:11px;font-weight:600;}
.modal-btns{display:flex;gap:10px;justify-content:center;margin-top:14px;}
.btn-cancel {background:var(--bg3);border:1px solid var(--border);color:var(--teal);padding:6px 16px;border-radius:4px;cursor:pointer;font-size:11px;}
.btn-confirm{background:rgba(248,81,73,0.1);border:1px solid var(--red);color:var(--red);padding:6px 16px;border-radius:4px;cursor:pointer;font-size:11px;}
.btn-cancel:hover {background:rgba(0,180,216,0.15);}
.btn-confirm:hover{background:rgba(248,81,73,0.25);}

/* PAGE WRAPPERS */
.page-wrap{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;}
#page2{overflow-y:auto;}

/* PAGE 1 GRID */
.main{flex:1;display:grid;grid-template-columns:190px 190px 1fr 240px;gap:7px;padding:7px 7px 5px;overflow:hidden;min-height:0;}
.col{display:flex;flex-direction:column;gap:7px;overflow:hidden;min-height:0;}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:7px 9px;overflow:hidden;min-height:0;}
.card-title{font-size:9px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;padding-bottom:4px;border-bottom:1px solid var(--border);flex-shrink:0;}
.card-title.teal  {color:var(--teal);  border-color:var(--teal);}
.card-title.purple{color:var(--purple);border-color:var(--purple);}

/* TREND BADGES */
.trend-badge{font-size:16px;font-weight:700;text-align:center;padding:4px 8px;border-radius:5px;margin-bottom:4px;letter-spacing:1px;}
.trend-long   {background:rgba(63,185,80,0.12); color:var(--green); border:1px solid var(--green);}
.trend-short  {background:rgba(248,81,73,0.12); color:var(--red);   border:1px solid var(--red);}
.trend-neutral{background:rgba(210,153,34,0.12);color:var(--yellow);border:1px solid var(--yellow);}

/* INDICATOR ROWS */
.ind-row{display:flex;justify-content:space-between;align-items:center;padding:2px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.ind-row:last-child{border-bottom:none;}
.ind-label{color:var(--muted);}
.ind-val{font-weight:600;}
.bull{color:var(--green);}.bear{color:var(--red);}.neut{color:var(--yellow);}.teal{color:var(--teal);}.purple{color:var(--purple);}

/* DECISION */
.decision-big{font-size:32px;font-weight:800;text-align:center;padding:10px;border-radius:7px;letter-spacing:3px;margin-bottom:7px;}
.dec-long {background:rgba(63,185,80,0.1); color:var(--green); border:2px solid var(--green);}
.dec-short{background:rgba(248,81,73,0.1); color:var(--red);   border:2px solid var(--red);}
.dec-hold {background:rgba(0,180,216,0.1); color:var(--teal);  border:2px solid var(--teal);}
.dec-stay {background:rgba(139,148,158,0.1);color:var(--muted);border:2px solid var(--border);}
.dec-meta{text-align:center;color:var(--muted);font-size:11px;margin-bottom:7px;}
.dec-meta span{color:var(--text);font-weight:600;}
.reasoning{background:var(--bg3);border-left:3px solid var(--teal);padding:7px 9px;border-radius:0 5px 5px 0;font-size:11px;line-height:1.45;margin-bottom:5px;}
.block-reason{background:rgba(248,81,73,0.07);border-left:3px solid var(--red);padding:7px 9px;border-radius:0 5px 5px 0;font-size:11px;line-height:1.45;color:var(--red);margin-bottom:5px;}
.warnings{display:flex;flex-direction:column;gap:3px;margin-top:4px;}
.warn-item{background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.3);border-radius:3px;padding:3px 7px;font-size:10px;color:var(--yellow);}

/* PERFORMANCE */
.score-bar{background:var(--bg3);border-radius:3px;height:6px;flex:1;}
.score-fill{height:100%;border-radius:3px;transition:width 0.4s;}
.score-high{background:var(--green);}.score-med{background:var(--yellow);}.score-low{background:var(--red);}
.perf-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 2px;}
.perf-win{background:var(--green);}.perf-loss{background:var(--red);}

/* POSITION */
.pos-card{background:var(--bg3);border-radius:5px;padding:7px;font-size:11px;}
.pos-long {border-left:3px solid var(--green);}
.pos-short{border-left:3px solid var(--red);}
.pos-none {border-left:3px solid var(--border);color:var(--muted);text-align:center;padding:9px;}
.pos-row{display:flex;gap:6px;padding:2px 5px;}
.pos-row>span:first-child{flex:0 0 120px;text-align:left;}
.pos-row>span:last-child{flex:0 0 auto;text-align:right;}

/* CHECK ITEMS */
.check-item{display:flex;align-items:center;gap:6px;padding:2px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.check-item:last-child{border-bottom:none;}
.check-pass{color:var(--green);font-weight:700;min-width:30px;font-size:10px;}
.check-fail{color:var(--red);  font-weight:700;min-width:30px;font-size:10px;}
.check-na  {color:var(--muted);font-weight:700;min-width:30px;font-size:10px;}
.check-lbl {color:var(--text);}

/* DUAL-MARKET SPLIT (BTC | ETH) */
.dual-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;column-gap:10px;}
.dual-grid > div{min-width:0;}
.dual-head{font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:3px;padding-bottom:2px;border-bottom:1px solid var(--bg3);}
.dual-grid .check-lbl,.dual-grid .compact-key,.dual-grid .liq-lbl{font-size:10px;}

/* KILL STATUS */
.kill-ok    {background:rgba(63,185,80,0.08); border:1px solid rgba(63,185,80,0.3); border-radius:4px;padding:3px 8px;color:var(--green);font-size:10px;text-align:center;flex-shrink:0;}
.kill-active{background:rgba(248,81,73,0.1);  border:1px solid var(--red);          border-radius:4px;padding:4px 8px;color:var(--red);  font-size:10px;font-weight:700;text-align:center;flex-shrink:0;}

/* WHALE & LIQ */
.compact-row{display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.compact-row:last-child{border-bottom:none;}
.compact-key{color:var(--muted);}
.compact-val{font-weight:600;}
.liq-row{display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid var(--bg3);font-size:11px;}
.liq-row:last-child{border-bottom:none;}
.liq-lbl{color:var(--muted);}
.hunt-badge{font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px;}
.hunt-down{background:rgba(248,81,73,0.15); color:var(--red);   border:1px solid rgba(248,81,73,0.4);}
.hunt-up  {background:rgba(63,185,80,0.15); color:var(--green); border:1px solid rgba(63,185,80,0.4);}
.hunt-bal {background:rgba(139,148,158,0.1);color:var(--muted); border:1px solid var(--border);}

/* PAGE 2 */
.p2-content{padding:8px 12px 20px;display:flex;flex-direction:column;gap:10px;}
.p2-card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px 16px;}
.p2-card .card-title{font-size:9px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.p2-account-bar{display:grid;grid-template-columns:repeat(7,1fr);gap:6px 6px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 16px;text-align:center;}
.acc-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:2px;}
.acc-val{font-size:14px;font-weight:700;}
.acc-bal{color:var(--teal);font-size:16px;}
.win{color:var(--green);font-weight:600;}.loss{color:var(--red);font-weight:600;}
.dir-long{color:var(--green);font-weight:700;}.dir-short{color:var(--red);font-weight:700;}
.p2-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-bottom:10px;}
.p2-stat-box{background:var(--bg3);border-radius:5px;padding:9px 12px;text-align:center;}
.p2-stat-label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;}
.p2-stat-val{font-size:16px;font-weight:700;}
.p2-stat-sub{font-size:10px;color:var(--muted);margin-top:3px;}
.p2-section-hdr{font-size:9px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin:10px 0 5px;padding-bottom:3px;border-bottom:1px solid var(--bg3);}
.p2-table{width:100%;border-collapse:collapse;font-size:12px;}
.p2-table th{text-align:left;padding:5px 8px;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);}
.p2-table td{padding:5px 8px;border-bottom:1px solid var(--bg3);font-family:monospace;}
.p2-table tr:last-child td{border-bottom:none;}
.p2-table tr.tr-win td{background:rgba(63,185,80,0.04);}
.p2-table tr.tr-loss td{background:rgba(248,81,73,0.04);}
.month-best td{background:rgba(63,185,80,0.09)!important;}
.month-worst td{background:rgba(248,81,73,0.07)!important;}
.cons-warn{margin-top:8px;padding:5px 9px;background:rgba(248,81,73,0.1);border:1px solid var(--red);border-radius:3px;font-size:10px;color:var(--red);font-weight:700;}
</style>
</head>
<body>

<!-- SHUTDOWN MODAL -->
<div class="modal-overlay" id="shutdownModal">
  <div class="modal">
    <h3>Shut Down CryptoHybrid AI?</h3>
    <p>This will stop the trading engine and close the dashboard.</p>
    <div class="modal-trade-warn" id="tradeWarn" style="display:none">
      WARNING: A trade is currently OPEN!<br>
      You must manually close this position on Kraken.<br>
      The system will NOT close it automatically.
    </div>
    <p>Are you sure you want to shut down?</p>
    <div class="modal-btns">
      <button class="btn-cancel"  onclick="closeModal()">Cancel &mdash; Keep Running</button>
      <button class="btn-confirm" onclick="confirmShutdown()">Yes &mdash; Shut Down</button>
    </div>
  </div>
</div>

<!-- SHARED HEADER -->
<div class="header">
  <div class="header-brand">
    <img src="/logo" height="32" style="margin-right:2px;border-radius:4px;" onerror="this.style.display='none'" alt="">
    <span class="feed-dot dot-err" id="feedDot"></span>
    <div>
      <div class="logo">CRYPTO<span>HYBRID</span> A.I. <span style="font-size:11px;color:var(--muted);font-weight:400;letter-spacing:0;margin-left:6px;">__VERSION_STRING__</span></div>
      <div class="subtitle">BTC &amp; ETH -- Kraken Exchange</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;border-left:1px solid var(--border);border-right:1px solid var(--border);">
    <div class="header-price" style="border-left:none;border-right:none;">
      <div class="hdr-price-lbl">BTC / GBP</div>
      <div class="hdr-price-val" id="hdrPrice">--</div>
    </div>
    <div class="header-price" style="border-left:1px solid var(--border);border-right:none;">
      <div class="hdr-price-lbl" style="color:var(--purple)">ETH / GBP</div>
      <div class="hdr-price-val" id="hdrEthPrice" style="color:var(--purple)">--</div>
    </div>
  </div>
  <div class="header-right">
    <div class="feed-status"><span id="feedLabel">Connecting...</span></div>
    <button class="nav-btn" id="btnToP2" onclick="showPage(2)">P&amp;L &rarr;</button>
    <button class="nav-btn" id="btnToP3" onclick="showPage(3)">PHANTOM &rarr;</button>
    <button class="nav-btn" id="btnToP1" onclick="showPage(1)" style="display:none;">&larr; Trading</button>
    <button class="shutdown-btn" onclick="openModal()">&#9211; Shutdown</button>
    <div class="clock" id="clock">--:--:-- UTC</div>
  </div>
</div>

<!-- TEMPORARY: whale hunt suspension banner (10 Jul 2026) -- remove when veto restored -->
<div style="background:rgba(210,153,34,0.15);border-bottom:1px solid rgba(210,153,34,0.5);color:#d29922;padding:8px 16px;text-align:center;font-size:12px;font-weight:600;letter-spacing:0.3px;">
  &#9888; WHALE HUNT VETO: SUSPENDED (10 Jul 2026) &mdash; running without hunt filter, paper test mode &mdash; collecting comparison data vs hunt-active period
</div>

<!-- PAGE 1: TRADING DASHBOARD -->
<div id="page1" class="page-wrap">
  <div class="main" id="main-grid">
    <div style="grid-column:1/-1;color:var(--muted);padding:40px;text-align:center">Loading CryptoHybrid A.I....</div>
  </div>
</div>

<!-- PAGE 2: PERFORMANCE & P&L -->
<div id="page2" class="page-wrap" style="display:none;">
  <div class="p2-content">
    <div class="p2-account-bar" id="p2-account-bar">
      <div style="color:var(--muted);font-size:11px;grid-column:1/-1;text-align:center;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-perf-detail">
      <div class="card-title purple">Ace Self-Performance &mdash; Detail</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-monthly">
      <div class="card-title">Monthly Breakdown</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-trades">
      <div class="card-title">BTC Trade History</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
    <div class="p2-card" id="p2-eth-trades">
      <div class="card-title purple">ETH Trade History</div>
      <div style="color:var(--muted);font-size:11px;">Loading...</div>
    </div>
  </div>
</div>

<!-- PAGE 3: PHANTOM TRADES -->
<div id="page3" class="page-wrap" style="display:none;">
  <div class="phantom-page">
    <div class="phantom-head">
      <div class="card-title" style="border:none;margin:0;padding:0;font-size:14px;">PHANTOM TRADES &mdash; Stay Out Quality</div>
      <button class="nav-btn" onclick="showPage(1)">&larr; Back to Dashboard</button>
    </div>
    <div id="phantomBody"><div style="color:var(--muted);font-size:12px;">Loading phantom trades...</div></div>
  </div>
</div>

<script>
var _currentPage = 1;
var hasOpenPosition = false;

/* ── Clock ──────────────────────────────────────────────────────────────── */
function updateClock(){
  var t = new Date();
  document.getElementById('clock').textContent =
    String(t.getUTCHours()).padStart(2,'0') + ':' +
    String(t.getUTCMinutes()).padStart(2,'0') + ':' +
    String(t.getUTCSeconds()).padStart(2,'0') + ' UTC';
}
setInterval(updateClock, 1000);
updateClock();

/* ── Page switching ─────────────────────────────────────────────────────── */
function showPage(n){
  var pages = {1:'page1', 2:'page2', 3:'page3'};
  for(var k in pages){
    var el = document.getElementById(pages[k]);
    if(el){ el.style.display = (Number(k) === n) ? 'flex' : 'none'; }
  }
  var b1 = document.getElementById('btnToP1');
  var b2 = document.getElementById('btnToP2');
  var b3 = document.getElementById('btnToP3');
  if(b1){ b1.style.display = (n === 1) ? 'none' : 'inline-block'; }
  if(b2){ b2.style.display = (n === 2) ? 'none' : 'inline-block'; }
  if(b3){ b3.style.display = (n === 3) ? 'none' : 'inline-block'; }
  _currentPage = n;
}

/* ── Feed status ────────────────────────────────────────────────────────── */
function updateFeed(lastUpdate){
  var dot = document.getElementById('feedDot');
  var lbl = document.getElementById('feedLabel');
  if(!lastUpdate){ dot.className='feed-dot dot-err'; lbl.textContent='No data'; return; }
  dot.className   = 'feed-dot dot-ok';
  lbl.textContent = 'Feed OK — ' + lastUpdate;
}

/* ── Shutdown modal ─────────────────────────────────────────────────────── */
function openModal(){
  document.getElementById('tradeWarn').style.display = hasOpenPosition ? 'block' : 'none';
  document.getElementById('shutdownModal').classList.add('open');
}
function closeModal(){
  document.getElementById('shutdownModal').classList.remove('open');
}
function confirmShutdown(){
  fetch('/api/shutdown', {method:'POST'})
    .then(function(){
      document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;background:#0d1117;color:#00b4d8;font-family:monospace;font-size:18px;">CryptoHybrid A.I. shut down. You may close this window.</div>';
    })
    .catch(function(){ closeModal(); });
}

/* ── Helpers ────────────────────────────────────────────────────────────── */
function fmt(v, dp){
  dp = (dp === undefined) ? 2 : dp;
  if(v === null || v === undefined || v !== v) return '--';
  return parseFloat(v).toFixed(dp);
}
function fmtPnl(v){
  if(v === null || v === undefined || v !== v) return '--';
  var n = parseFloat(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2);
}
function trendClass(t){
  if(!t) return 'trend-neutral'; t = t.toUpperCase();
  if(t.indexOf('LONG') >= 0 || t.indexOf('BULL') >= 0) return 'trend-long';
  if(t.indexOf('SHORT') >= 0 || t.indexOf('BEAR') >= 0) return 'trend-short';
  return 'trend-neutral';
}
function trendLabel(t){
  if(!t) return 'NEUTRAL'; t = t.toUpperCase();
  if(t.indexOf('LONG') >= 0 || t.indexOf('BULL') >= 0) return 'LONG';
  if(t.indexOf('SHORT') >= 0 || t.indexOf('BEAR') >= 0) return 'SHORT';
  return 'NEUTRAL';
}
function decClass(d){
  if(!d) return 'dec-stay';
  if(d.indexOf('LONG') >= 0) return 'dec-long';
  if(d.indexOf('SHORT') >= 0) return 'dec-short';
  if(d === 'HOLD') return 'dec-hold';
  return 'dec-stay';
}
function indCls(v, thresh){
  thresh = thresh || 0; var n = parseFloat(v);
  if(isNaN(n)) return 'neut';
  return n > thresh ? 'bull' : n < thresh ? 'bear' : 'neut';
}
function sslCls(v){ return v ? 'bull' : 'bear'; }
function sslLbl(v){ return v ? 'BULL' : 'BEAR'; }

/* ── Right panel (Page 1) — dual-market BTC | ETH ────────────────────────── */

/* One pre-check / checklist column for a single market */
function buildCheckCol(pairLabel, headCls, pmode, checks, checklist){
  var isClaude = (pmode === 'claude');
  var src  = isClaude ? (checklist || {}) : (checks || {});
  var keys = Object.keys(src);
  var body = keys.length
    ? keys.map(function(k){
        var v = src[k]; var cls, icon;
        if(v === true){cls='check-pass';icon='PASS';}
        else if(v === false){cls='check-fail';icon='FAIL';}
        else{cls='check-na';icon='N/A';}
        var lbl = isClaude ? k.replace(/_/g,' ') : k;
        return '<div class="check-item"><span class="' + cls + '">' + icon + '</span><span class="check-lbl">' + lbl + '</span></div>';
      }).join('')
    : '<div style="color:var(--muted);font-size:11px;">Waiting for first tick...</div>';
  var tag = isClaude ? ' <span style="font-size:8px;color:var(--purple)">(Arthur)</span>' : '';
  return '<div><div class="dual-head ' + headCls + '">' + pairLabel + tag + '</div>' + body + '</div>';
}

/* One whale-summary column for a single market */
function buildWhaleCol(pairLabel, headCls, whale){
  whale = whale || {};
  var wshow = [
    ['Order book pressure','OB Pressure'],
    ['Whale sentiment','Whale Sentiment'],
    ['Funding sentiment','Funding'],
    ['Volatility level','Volatility'],
  ];
  var wrows = '';
  wshow.forEach(function(pair){
    var v = whale[pair[0]]; if(!v) return;
    var vl = v.toString().toUpperCase();
    var cls = (vl.indexOf('BULL')>=0||vl.indexOf('BUY')>=0||vl.indexOf('ACCUM')>=0) ? 'bull'
            : (vl.indexOf('BEAR')>=0||vl.indexOf('SELL')>=0||vl.indexOf('DIST')>=0) ? 'bear' : 'neut';
    wrows += '<div class="compact-row"><span class="compact-key">' + pair[1] + '</span><span class="compact-val ' + cls + '">' + v + '</span></div>';
  });
  return '<div><div class="dual-head ' + headCls + '">' + pairLabel + '</div>' +
    (wrows || '<div style="color:var(--muted);font-size:11px;">Waiting for data...</div>') + '</div>';
}

/* One liquidation-zone column for a single market */
function buildLiqCol(pairLabel, headCls, liq, priceDecimals){
  var h = '<div><div class="dual-head ' + headCls + '">' + pairLabel + '</div>';
  if(liq && liq.available){
    var verdict = (liq.hunt_verdict || 'BALANCED').toUpperCase();
    var bCls = verdict==='DOWNWARD' ? 'hunt-down' : verdict==='UPWARD' ? 'hunt-up' : 'hunt-bal';
    var ds = (liq.hunt_down !== null && liq.hunt_down !== undefined) ? liq.hunt_down : '--';
    var us = (liq.hunt_up   !== null && liq.hunt_up   !== undefined) ? liq.hunt_up   : '--';
    h += '<div class="liq-row"><span class="liq-lbl">Hunt</span>' +
      '<span><span class="hunt-badge ' + bCls + '">' + verdict + '</span>' +
      ' <span style="font-size:10px;color:var(--muted)">↓' + ds + ' ↑' + us + '</span></span></div>';
    if(liq.key_level_price){
      var kdist = (liq.key_level_dist !== null && liq.key_level_dist !== undefined) ? liq.key_level_dist.toFixed(1) + '%' : '--';
      var ktype = (liq.key_level_type || '').replace(/_/g,' ');
      var kdec  = (priceDecimals === undefined) ? 0 : priceDecimals;
      h += '<div class="liq-row"><span class="liq-lbl">Level</span>' +
        '<span>£' + Number(liq.key_level_price).toLocaleString('en-GB',{maximumFractionDigits:kdec}) +
        ' <span style="color:var(--muted);font-size:10px">(' + ktype + ', ' + kdist + ')</span></span></div>';
    }
    if(liq.ls_ratio !== null && liq.ls_ratio !== undefined){
      var lsl = liq.ls_label || '';
      var lslCls = (lsl.toUpperCase().indexOf('BEAR')>=0||lsl.toUpperCase().indexOf('SELL')>=0) ? 'bear'
                 : (lsl.toUpperCase().indexOf('BULL')>=0||lsl.toUpperCase().indexOf('BUY')>=0) ? 'bull' : 'neut';
      h += '<div class="liq-row"><span class="liq-lbl">L/S</span>' +
        '<span class="' + lslCls + '">' + liq.ls_ratio.toFixed(2) +
        ' <span style="font-size:10px;color:var(--muted)">(' + lsl + ')</span></span></div>';
    }
  } else {
    h += '<div style="color:var(--muted);font-size:11px;">Waiting for data...</div>';
  }
  return h + '</div>';
}

function renderRightPanel(d){
  var eth  = d.eth || {};
  var acc  = d.account || {};

  var killHTML = acc.killed
    ? '<div class="kill-active">KILL SWITCH ACTIVE<br><small>' + (acc.kill_reason || '') + '</small></div>'
    : '<div class="kill-ok">System OK — Trading Active</div>';

  /* PRE-CHECK RESULTS — BTC | ETH side by side */
  var tokFoot = '';
  if(d.panel_mode === 'claude' || eth.panel_mode === 'claude'){
    tokFoot = '<div style="margin-top:6px;padding-top:5px;border-top:1px solid var(--border);font-size:9px;color:var(--purple)">' +
      'Tokens — BTC: ' + (d.tokens_used || '--') + ' &nbsp;·&nbsp; ETH: ' + (eth.tokens_used || '--') + '</div>';
  }
  var panelHTML = '<div class="card" style="flex:1;display:flex;flex-direction:column;">' +
    '<div class="card-title teal">Pre-Check Results</div>' +
    '<div class="dual-grid">' +
      buildCheckCol('BTC', 'teal',   d.panel_mode,   d.pre_checks,   d.checklist) +
      buildCheckCol('ETH', 'purple', eth.panel_mode, eth.pre_checks, eth.checklist) +
    '</div>' + tokFoot +
    '</div>';

  /* WHALE SUMMARY — BTC | ETH side by side */
  var whaleHTML = '<div class="card" style="flex-shrink:0"><div class="card-title">Whale Summary</div>' +
    '<div class="dual-grid">' +
      buildWhaleCol('BTC', 'teal',   d.whale) +
      buildWhaleCol('ETH', 'purple', eth.whale) +
    '</div></div>';

  /* LIQUIDATION ZONE — BTC | ETH side by side */
  var liqHTML = '<div class="card" style="flex-shrink:0"><div class="card-title">Liquidation Zone</div>' +
    '<div class="dual-grid">' +
      buildLiqCol('BTC', 'teal',   d.liq_compact,   0) +
      buildLiqCol('ETH', 'purple', eth.liq_compact, 0) +
    '</div></div>';

  return killHTML + panelHTML + whaleHTML + liqHTML;
}

/* ── Manual Morgan reset (three-zone model, 24 Jul 2026) ────────────────── */
function resetMorgan(){
  if(!confirm('Reset Morgan confidence to 50? Do this only after reviewing the phantom data and trade history.')) return;
  fetch('/api/reset-morgan', {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(res){
      alert(res.confirmation || ('Morgan reset requested (to ' + (res.to || 50) + ').'));
      if(typeof refreshDashboard === 'function') refreshDashboard();
    })
    .catch(function(){ alert('Morgan reset request failed.'); });
}

/* ── Performance card (Page 1, compact) ────────────────────────────────── */
function renderPerfCard(perf){
  var total = perf ? (perf.total_trades || 0) : 0;
  if(total === 0){
    return '<div class="card"><div class="card-title purple">Ace Self-Performance</div>' +
      '<div style="color:var(--muted);font-size:11px;text-align:center;padding:8px 0">No trades yet — system ready</div></div>';
  }
  var score  = (perf.confidence_score != null ? perf.confidence_score : 50);
  var level  = perf.confidence_level || 'MEDIUM';
  var sc     = level==='HIGH' ? 'score-high' : (level==='LOW'||level==='VERY_LOW') ? 'score-low' : 'score-med';
  var lc     = level==='HIGH' ? 'bull'       : (level==='LOW'||level==='VERY_LOW') ? 'bear'      : 'neut';
  var stType = perf.streak_type  || '';
  var stCnt  = perf.streak_count || 0;
  var stCol  = stType==='WIN' ? 'var(--green)' : stType==='LOSS' ? 'var(--red)' : 'var(--muted)';
  var stStr  = stCnt > 0 ? (stCnt + ' ' + (stType==='WIN'?'WIN':'LOSS') + (stCnt>1?'S':'')) : '--';
  var r5     = perf.recent_5 || [];
  var dots   = r5.map(function(r){ return '<span class="perf-dot ' + (r==='WIN'?'perf-win':'perf-loss') + '"></span>'; }).join('');
  // Three-zone Morgan panel (24 Jul 2026): CRITICAL (<30, hard block) / WARNING
  // (30-49, trading continues) / normal (>=50). Reset button in both non-normal zones.
  var mScore = (perf.morgan_raw != null ? perf.morgan_raw : score);
  var lastReset = perf.morgan_last_reset
    ? '<div style="margin-top:3px;font-weight:400;color:var(--muted);font-size:9px;">Morgan last reset: ' + perf.morgan_last_reset + '</div>'
    : '';
  var resetBtn = '<button onclick="resetMorgan()" style="margin-top:5px;padding:3px 9px;background:var(--red);color:#fff;border:none;border-radius:3px;font-size:10px;font-weight:700;cursor:pointer;">RESET MORGAN TO 50</button>';
  var cons = '';
  var floor;
  if(perf.morgan_hard_block){
    floor = '<div style="margin-top:4px;padding:5px 7px;background:rgba(248,81,73,0.18);border:1px solid var(--red);border-radius:3px;font-size:10px;color:var(--red);font-weight:700;">' +
        '&#128680; MORGAN CRITICAL — Score: ' + mScore + '/100<br>' +
        '<span style="font-weight:400;color:var(--muted)">Entry suspended. Gaius intervention active. Existing positions still managed.</span><br>' +
        resetBtn + lastReset +
      '</div>';
  } else if(perf.morgan_below_floor){
    floor = '<div style="margin-top:4px;padding:5px 7px;background:rgba(210,153,34,0.16);border:1px solid var(--amber,#d29922);border-radius:3px;font-size:10px;color:var(--amber,#d29922);font-weight:700;">' +
        '&#9888; MORGAN WARNING — Score: ' + mScore + '/100<br>' +
        '<span style="font-weight:400;color:var(--muted)">Performance under review. Trading continues. Manual reset available.</span><br>' +
        resetBtn + lastReset +
      '</div>';
  } else {
    floor = lastReset;
  }
  return '<div class="card"><div class="card-title purple">Ace Self-Performance</div>' +
    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">' +
    '<span style="font-size:10px;color:var(--muted);min-width:60px">Confidence</span>' +
    '<div class="score-bar"><div class="score-fill ' + sc + '" style="width:' + score + '%"></div></div>' +
    '<span class="' + lc + '" style="font-size:12px;font-weight:700;min-width:80px;text-align:right">' + score + '/100 ' + level + '</span></div>' +
    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">' +
    '<span style="font-size:10px;color:var(--muted);min-width:60px">Last ' + r5.length + '</span>' +
    (dots || '<span style="color:var(--muted);font-size:10px">No trades</span>') + '</div>' +
    '<div style="display:flex;gap:14px;font-size:11px;color:var(--muted);">' +
    '<span>Streak: <strong style="color:' + stCol + '">' + stStr + '</strong></span>' +
    '<span>Trades: <strong style="color:var(--teal)">' + total + '</strong></span>' +
    '<span>WR: <strong style="color:var(--text)">' + fmt(perf.win_rate,1) + '%</strong></span>' +
    '</div>' + cons + floor + '</div>';
}

/* ── Indicator column builder ────────────────────────────────────────────── */
function buildIndCol(pair, titleCls, trend1d, trend1h, signal5m, ind1d, ind1h, ind5m){
  return '<div class="col">' +
    '<div class="card" style="flex-shrink:0"><div class="card-title ' + titleCls + '">' + pair + ' Daily</div>' +
    '<div class="trend-badge ' + trendClass(trend1d) + '">' + trendLabel(trend1d) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL</span><span class="ind-val ' + sslCls(ind1d.ssl_bull) + '">' + sslLbl(ind1d.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind1d.rsi,50) + '">' + fmt(ind1d.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Mode</span><span class="ind-val ' + (ind1d.ssl_bull===true?'bull':ind1d.ssl_bull===false?'bear':'neut') + '">' +
    (ind1d.ssl_bull===true?'LONG allowed (Daily BULL)':ind1d.ssl_bull===false?'SHORT only (Daily BEAR)':'Both (Daily NEUTRAL)') + '</span></div>' +
    '</div>' +
    '<div class="card" style="flex-shrink:0"><div class="card-title ' + titleCls + '">' + pair + ' 1-Hour</div>' +
    '<div class="trend-badge ' + trendClass(trend1h) + '">' + trendLabel(trend1h) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL Cloud</span><span class="ind-val ' + sslCls(ind1h.ssl_bull) + '">' + sslLbl(ind1h.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind1h.rsi,50) + '">' + fmt(ind1h.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">MACD</span><span class="ind-val ' + indCls(ind1h.macd) + '">' + fmt(ind1h.macd,2) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">TMO</span><span class="ind-val ' + indCls(ind1h.tmo_main) + '">' + fmt(ind1h.tmo_main,3) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Chande MO</span><span class="ind-val ' + indCls(ind1h.chande_mo) + '">' + fmt(ind1h.chande_mo,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Money Flow</span><span class="ind-val ' + indCls(ind1h.money_flow) + '">' + fmt(ind1h.money_flow,4) + '</span></div>' +
    '</div>' +
    '<div class="card" style="flex:1"><div class="card-title ' + titleCls + '">' + pair + ' 5-Min</div>' +
    '<div class="trend-badge ' + trendClass(signal5m) + '">' + trendLabel(signal5m) + '</div>' +
    '<div class="ind-row"><span class="ind-label">SSL Cloud</span><span class="ind-val ' + sslCls(ind5m.ssl_bull) + '">' + sslLbl(ind5m.ssl_bull) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">RSI</span><span class="ind-val ' + indCls(ind5m.rsi,50) + '">' + fmt(ind5m.rsi,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">MACD</span><span class="ind-val ' + indCls(ind5m.macd) + '">' + fmt(ind5m.macd,2) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">TMO</span><span class="ind-val ' + indCls(ind5m.tmo_main) + '">' + fmt(ind5m.tmo_main,3) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Chande MO</span><span class="ind-val ' + indCls(ind5m.chande_mo) + '">' + fmt(ind5m.chande_mo,1) + '</span></div>' +
    '<div class="ind-row"><span class="ind-label">Money Flow</span><span class="ind-val ' + indCls(ind5m.money_flow) + '">' + fmt(ind5m.money_flow,4) + '</span></div>' +
    '</div>' +
    '</div>';
}

/* ── Page 1: trading dashboard ──────────────────────────────────────────── */
/* ── STAY OUT QUALITY panel ─────────────────────────────────────────────── */
/* Compact Stay Out Quality summary (Job 2) -- full detail lives on the Phantom
   Trades page (page 3); this card opens it on click. */
function renderStayOut(sq){
  sq = sq || {};
  var html = '<div class="card" id="soqCompact" onclick="showPage(3)"><div class="card-title">STAY OUT QUALITY</div>';
  if(!sq.status || sq.status === 'No data yet'){
    html += '<div class="reasoning">Awaiting first decisions</div>' +
            '<div class="soq-hint">PHANTOM &rarr;</div></div>';
    return html;
  }
  if(sq.status !== 'ok'){
    html += '<div class="block-reason">' + sq.status + '</div></div>';
    return html;
  }
  var q = (sq.quality_score == null) ? '--' : (sq.quality_score + '%');
  var saved  = (sq.net_saved  == null) ? 0 : sq.net_saved;
  var missed = (sq.net_missed == null) ? 0 : sq.net_missed;
  html += '<div class="dec-meta">Quality: <span>' + q + '</span> &nbsp;|&nbsp; Last 50</div>';
  html += '<div style="font-size:11px;margin:4px 0;">' +
          '✅ Correct: ' + (sq.correct || 0) + ' &nbsp; ' +
          '❌ Wrong: ' + (sq.wrong || 0) + ' &nbsp; ' +
          '➖ Neutral: ' + (sq.neutral || 0) + '</div>';
  html += '<div style="font-size:11px;margin-bottom:2px;">' +
          'Net Saved: <span class="bull">+£' + Math.abs(saved).toFixed(2) + '</span> &nbsp; ' +
          'Net Missed: <span class="bear">-£' + Math.abs(missed).toFixed(2) + '</span></div>';
  html += '<div class="soq-hint">CLICK FOR FULL PHANTOM TRADES &rarr;</div>';
  html += '</div>';
  return html;
}

/* Format an ISO-8601 UTC timestamp as "YYYY-MM-DD HH:MM" (UTC). */
function fmtPhantomTs(ts){
  if(!ts){ return '--'; }
  var s = String(ts).replace('T', ' ');
  return (s.length >= 16) ? s.substring(0, 16) : s;
}
function fmtPhantomGBP(v){
  var n = parseFloat(v);
  if(isNaN(n)){ return '--'; }
  return '£' + n.toLocaleString('en-GB', {maximumFractionDigits: 0});
}

/* Full Phantom Trades page body (page 3): summary + last-20 table, newest first. */
function phMoveCell(v){
  var n = parseFloat(v);
  if(isNaN(n)){ return '<td class="ph-na">--</td>'; }
  var cls = (n>=0)?'bull':'bear';
  return '<td class="'+cls+'">'+(n>=0?'+£':'-£')+Math.abs(n).toFixed(2)+'</td>';
}
function renderPhantomBody(sq){
  sq = sq || {};
  if(!sq.status || sq.status === 'No data yet'){
    return '<div style="color:var(--muted);font-size:12px;">Awaiting first phantom decisions</div>';
  }
  if(sq.status !== 'ok'){
    return '<div class="block-reason">' + sq.status + '</div>';
  }
  var q = (sq.quality_score == null) ? '--' : (sq.quality_score + '%');
  var saved  = (sq.net_saved  == null) ? 0 : sq.net_saved;
  var missed = (sq.net_missed == null) ? 0 : sq.net_missed;
  var html = '<div class="phantom-summary">' +
    '<div>Last 50 decisions &nbsp;|&nbsp; Quality: <span class="ps-q">' + q + '</span></div>' +
    '<div>✅ Correct: ' + (sq.correct || 0) + ' &nbsp;&nbsp; ❌ Wrong: ' + (sq.wrong || 0) +
      ' &nbsp;&nbsp; ➖ Neutral: ' + (sq.neutral || 0) + '</div>' +
    '<div>Net Saved: <span class="bull">+£' + Math.abs(saved).toFixed(2) + '</span> &nbsp;&nbsp; ' +
      'Net Missed: <span class="bear">-£' + Math.abs(missed).toFixed(2) + '</span></div>' +
    '</div>';
  var decs = (sq.decisions || []).slice();
  decs.reverse();   /* newest first */
  html += '<div class="phantom-scroll"><table class="phantom-table"><thead><tr>' +
    '<th>Date/Time (UTC)</th><th>Market</th><th>Direction</th><th>Entry Price</th>' +
    '<th>Confidence</th><th>5min</th><th>10min</th><th>15min</th><th>30min</th><th>1hr</th><th>2hr</th><th>Verdict</th></tr></thead><tbody>';
  for(var i = 0; i < decs.length; i++){
    var r = decs[i] || {};
    var mkt = r.market || '--';
    var dir = r.direction_blocked || r.direction || '--';
    var entry = fmtPhantomGBP(r.price_at_decision);
    var conf = r.confidence || '--';
    var pnl = parseFloat(r.pnl_1hr);
    var pnlStr = isNaN(pnl) ? '--' : ((pnl >= 0 ? '+£' : '-£') + Math.abs(pnl).toFixed(2));
    var pnlCls = isNaN(pnl) ? '' : (pnl >= 0 ? 'bull' : 'bear');
    var v = r.verdict || 'PENDING';
    var vCls = (v === 'CORRECT') ? 'v-correct' : (v === 'WRONG') ? 'v-wrong' :
               (v === 'NEUTRAL') ? 'v-neutral' : 'v-pending';
    html += '<tr>' +
      '<td>' + fmtPhantomTs(r.timestamp) + '</td>' +
      '<td>' + mkt + '</td>' +
      '<td>' + dir + '</td>' +
      '<td>' + entry + '</td>' +
      '<td>' + conf + '</td>' +
      phMoveCell(r.pnl_5min) + phMoveCell(r.pnl_10min) + phMoveCell(r.pnl_15min) + phMoveCell(r.pnl_30min) + phMoveCell(r.pnl_1hr) + phMoveCell(r.pnl_2hr) +
      '<td><span class="' + vCls + '">' + v + '</span></td>' +
      '</tr>';
  }
  html += '</tbody></table></div>';
  return html;
}

function renderPage1(d){
  var eth      = d.eth         || {};
  var trend1d  = d.trend_1d   || 'NEUTRAL';
  var trend1h  = d.trend_1h   || 'NEUTRAL';
  var signal5m = d.signal_5m  || 'NEUTRAL';
  var decision = d.decision   || 'STAY_OUT';
  var pos      = d.position   || null;
  var ind1h    = d.indicators_1h || {};
  var ind5m    = d.indicators_5m || {};
  var ind1d    = d.indicators_1d || {};
  var warnings = d.warnings   || [];
  var mode     = d.panel_mode || 'pre_checks';

  hasOpenPosition = !!(pos || eth.position);
  updateFeed(d.last_update_utc || '');

  /* Header prices */
  var hdrEl = document.getElementById('hdrPrice');
  if(hdrEl){ hdrEl.textContent = 'GBP ' + (d.price||0).toLocaleString('en-GB',{minimumFractionDigits:2}); }
  var hdrEthEl = document.getElementById('hdrEthPrice');
  if(hdrEthEl){ hdrEthEl.textContent = 'GBP ' + (eth.price||0).toLocaleString('en-GB',{minimumFractionDigits:4}); }

  var decText = decision.replace('ENTER_','').replace('EXIT_','EXIT ').replace(/_/g,' ');
  if(decision === 'STAY_OUT') decText = 'STAY OUT';

  var reasoning   = d.reasoning   || 'Waiting for next analysis cycle...';
  var blockReason = d.block_reason || '';
  var reasonBox = (blockReason && mode === 'pre_checks')
    ? '<div class="block-reason">' + blockReason + '</div>'
    : '<div class="reasoning">' + reasoning + '</div>';

  var warnHTML = (warnings.length > 0 && mode === 'claude')
    ? '<div class="warnings">' + warnings.map(function(w){ return '<div class="warn-item">'+w+'</div>'; }).join('') + '</div>'
    : '';

  /* Position card -- shows BTC or ETH if open, else both empty */
  function buildPosHTML(p, priceDecimals, currentPrice){
    if(!p) return '<div class="pos-card pos-none">No open position<br><span style="font-size:10px">Watching for setup...</span></div>';
    var pc = p.direction==='LONG' ? 'pos-long' : 'pos-short';
    var dc = p.direction==='LONG' ? 'bull' : 'bear';
    var dp = priceDecimals || 2;
    /* Guarded numeric field: 'None'/missing/non-finite -> --- (never NaN) */
    function g(v, d){
      var n = parseFloat(v);
      if(v === null || v === undefined || v === 'None' || !isFinite(n)) return '---';
      return n.toFixed(d === undefined ? 2 : d);
    }
    /* Floating Points from the LIVE price -- never the stringified realised field */
    var entry = parseFloat(p.entry_price || p.entry);
    var cur   = parseFloat(currentPrice);
    var dir   = (p.direction||'').toUpperCase();
    var points = (isNaN(entry)||isNaN(cur)||cur===0) ? null
               : (dir==='SHORT' ? entry-cur : cur-entry);
    var pointsStr = (points===null || !isFinite(points)) ? '---'
               : (points>=0?'+':'') + points.toFixed(dp);
    var pointsCls = (points===null || !isFinite(points)) ? '' : (points>=0 ? 'bull' : 'bear');
    return '<div class="pos-card ' + pc + '">' +
      '<div class="pos-row"><span class="' + dc + '" style="font-weight:700">' + p.direction + '</span>' +
      '<span style="color:var(--muted)">' + (p.entry_time||'') + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Entry</span><span>GBP ' + g(p.entry_price,dp) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Stop</span><span class="bear">GBP ' + g(p.stop_loss,dp) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Target</span><span class="bull">GBP ' + g(p.take_profit,dp) + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Points</span><span class="' + pointsCls + '">' + pointsStr + '</span></div>' +
      '<div class="pos-row"><span style="color:var(--muted)">Size</span><span>GBP ' + g(p.position_size_gbp,2) + '</span></div>' +
      '</div>';
  }

  /* ETH position and reasoning */
  var ethPos         = eth.position || null;
  var ethDecision    = eth.decision    || 'STAY_OUT';
  var ethMode        = eth.panel_mode  || 'pre_checks';
  var ethDecText     = ethDecision.replace('ENTER_','').replace('EXIT_','EXIT ').replace(/_/g,' ');
  if(ethDecision === 'STAY_OUT') ethDecText = 'STAY OUT';
  var ethReasoning   = eth.reasoning   || 'Waiting for ETH analysis...';
  var ethBlockReason = eth.block_reason || '';
  var ethReasonBox   = (ethBlockReason && ethMode === 'pre_checks')
    ? '<div class="block-reason">' + ethBlockReason + '</div>'
    : '<div class="reasoning" style="border-left-color:var(--purple)">' + ethReasoning + '</div>';
  var ethWarnings    = eth.warnings || [];
  var ethWarnHTML    = (ethWarnings.length > 0 && ethMode === 'claude')
    ? '<div class="warnings">' + ethWarnings.map(function(w){ return '<div class="warn-item">'+w+'</div>'; }).join('') + '</div>'
    : '';

  /* BTC LEFT COLUMN */
  var leftCol = buildIndCol('BTC', 'teal', trend1d, trend1h, signal5m, ind1d, ind1h, ind5m);

  /* ETH LEFT COLUMN */
  var ethInd1d = eth.indicators_1d || {};
  var ethInd1h = eth.indicators_1h || {};
  var ethInd5m = eth.indicators_5m || {};
  var ethLeftCol = buildIndCol('ETH', 'purple',
    eth.trend_1d || 'NEUTRAL', eth.trend_1h || 'NEUTRAL', eth.signal_5m || 'NEUTRAL',
    ethInd1d, ethInd1h, ethInd5m);

  /* CENTRE COLUMN -- BTC decision on top, ETH below */
  var centreCol = '<div class="col">' +
    '<div class="card"><div class="card-title teal">BTC &mdash; Claude Decision</div>' +
    '<div class="decision-big ' + decClass(decision) + '" style="font-size:22px;padding:6px">' + decText + '</div>' +
    '<div class="dec-meta">Confidence: <span>' + (d.confidence||'--') + '</span> &nbsp;|&nbsp; 1h Bias: <span>' + (d.one_hour_bias||'--') + '</span></div>' +
    reasonBox + warnHTML +
    '</div>' +
    '<div class="card"><div class="card-title purple">ETH &mdash; Claude Decision</div>' +
    '<div class="decision-big ' + decClass(ethDecision) + '" style="font-size:22px;padding:6px;border-color:var(--purple)">' + ethDecText + '</div>' +
    '<div class="dec-meta">Confidence: <span>' + (eth.confidence||'--') + '</span> &nbsp;|&nbsp; 1h Bias: <span>' + (eth.one_hour_bias||'--') + '</span></div>' +
    ethReasonBox + ethWarnHTML +
    '</div>' +
    renderPerfCard(d.performance || {}) +
    '<div class="card"><div class="card-title">Open Positions</div>' +
    '<div style="font-size:9px;color:var(--teal);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px">BTC</div>' +
    buildPosHTML(pos, 2, d.price) +
    '<div style="font-size:9px;color:var(--purple);text-transform:uppercase;letter-spacing:1px;margin:5px 0 3px">ETH</div>' +
    buildPosHTML(ethPos, 4, eth.price) +
    '</div>' +
    renderStayOut(d.stay_out_quality) +
    '</div>';

  /* RIGHT COLUMN */
  var rightCol = '<div class="col">' + renderRightPanel(d) + '</div>';

  document.getElementById('main-grid').innerHTML = leftCol + ethLeftCol + centreCol + rightCol;

  /* Keep the Phantom Trades page (page 3) in sync each poll (Job 2). */
  var pb = document.getElementById('phantomBody');
  if(pb){ pb.innerHTML = renderPhantomBody(d.stay_out_quality); }
}

/* ── Page 2: P&L and performance ────────────────────────────────────────── */
function renderPage2(d){
  var acc      = d.account       || {};
  var perf     = d.performance   || {};
  var trades   = d.trades        || [];
  var monthly  = d.monthly_stats || [];
  var breakdown= d.breakdown     || {};
  var dirStats = breakdown.direction || {};
  var sesStats = breakdown.session   || {};
  var pnl      = acc.total_pnl   || 0;
  var dpnl     = acc.daily_pnl   || 0;
  var eacc     = d.account_eth   || {};
  var epnl     = eacc.total_pnl  || 0;
  var edpnl    = eacc.daily_pnl  || 0;
  var combo    = d.combined_capital || ((acc.capital||1000) + (eacc.capital||1000));

  function accBar(a, pnl_, dpnl_, titleCls, labelText){
    return '<div style="grid-column:1/-1;font-size:9px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--' + titleCls + ');padding-bottom:3px;border-bottom:1px solid var(--border);margin-bottom:4px">' + labelText + '</div>' +
    '<div><div class="acc-lbl">Balance</div>' +
    '<div class="acc-val ' + titleCls + '">GBP ' + (a.capital||1000).toLocaleString('en-GB',{minimumFractionDigits:2}) + '</div></div>' +
    '<div><div class="acc-lbl">Total P&amp;L</div>' +
    '<div class="acc-val ' + (pnl_>=0?'win':'loss') + '">GBP ' + fmtPnl(pnl_) + '</div></div>' +
    '<div><div class="acc-lbl">Return</div>' +
    '<div class="acc-val ' + (pnl_>=0?'win':'loss') + '">' + (a.total_return>=0?'+':'') + fmt(a.total_return) + '%</div></div>' +
    '<div><div class="acc-lbl">Today P&amp;L</div>' +
    '<div class="acc-val ' + (dpnl_>=0?'win':'loss') + '">GBP ' + fmtPnl(dpnl_) + '</div></div>' +
    '<div><div class="acc-lbl">Trades</div>' +
    '<div class="acc-val ' + titleCls + '">' + (a.total_trades||0) + '</div></div>' +
    '<div><div class="acc-lbl">W / L</div>' +
    '<div class="acc-val"><span class="win">' + (a.winners||0) + '</span> / <span class="loss">' + (a.losers||0) + '</span></div></div>' +
    '<div><div class="acc-lbl">Win Rate</div>' +
    '<div class="acc-val ' + ((a.win_rate||0)>=50?'win':'loss') + '">' + fmt(a.win_rate,1) + '%</div></div>';
  }

  var comboPnl = (acc.total_pnl||0) + (eacc.total_pnl||0);

  /* Account summary bar -- BTC row, ETH row, combined total */
  document.getElementById('p2-account-bar').innerHTML =
    accBar(acc,  pnl,  dpnl,  'teal',   'BTC / GBP') +
    accBar(eacc, epnl, edpnl, 'purple', 'ETH / GBP') +
    '<div style="grid-column:1/-1;border-top:1px solid var(--border);margin-top:4px;padding-top:4px;display:flex;gap:24px;align-items:center;font-size:11px;">' +
    '<span style="color:var(--muted)">Combined Capital:</span>' +
    '<span style="color:var(--teal);font-weight:700;font-size:14px">GBP ' + combo.toLocaleString('en-GB',{minimumFractionDigits:2}) + '</span>' +
    '<span style="color:var(--muted)">Combined P&L:</span>' +
    '<span class="' + (comboPnl>=0?'win':'loss') + '" style="font-size:13px;font-weight:700">GBP ' + fmtPnl(comboPnl) + '</span>' +
    '</div>';

  /* Ace performance detail */
  var total = perf.total_trades || 0;
  var perfHTML = '';
  if(total === 0){
    perfHTML = '<div style="color:var(--muted);font-size:12px;padding:16px 0;text-align:center">No trades yet — system ready</div>';
  } else {
    var score  = (perf.confidence_score != null ? perf.confidence_score : 50);
    var level  = perf.confidence_level || 'MEDIUM';
    var sc     = level==='HIGH' ? 'score-high' : (level==='LOW'||level==='VERY_LOW') ? 'score-low' : 'score-med';
    var lc     = level==='HIGH' ? 'bull'       : (level==='LOW'||level==='VERY_LOW') ? 'bear'      : 'neut';
    var stType = perf.streak_type  || '';
    var stCnt  = perf.streak_count || 0;
    var stCol  = stType==='WIN' ? 'var(--green)' : stType==='LOSS' ? 'var(--red)' : 'var(--muted)';
    var stStr  = stCnt > 0 ? (stCnt + ' ' + (stType==='WIN'?'WIN':'LOSS') + (stCnt>1?'S':'')) : '--';

    /* Confidence bar */
    perfHTML += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">' +
      '<span style="font-size:11px;color:var(--muted);min-width:80px">Confidence</span>' +
      '<div class="score-bar"><div class="score-fill ' + sc + '" style="width:' + score + '%"></div></div>' +
      '<span class="' + lc + '" style="font-size:14px;font-weight:700;min-width:110px;text-align:right">' + score + '/100 ' + level + '</span></div>';

    /* Last 10 dots derived from trades array */
    var last10 = trades.slice(0, 10);
    var dots10 = last10.map(function(t){
      return '<span class="perf-dot ' + (t.pnl_class==='win'?'perf-win':'perf-loss') + '"></span>';
    }).join('');
    perfHTML += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">' +
      '<span style="font-size:11px;color:var(--muted);min-width:80px">Last ' + last10.length + '</span>' +
      (dots10 || '<span style="color:var(--muted);font-size:11px">No trades</span>') + '</div>';

    /* Headline stats */
    perfHTML += '<div style="display:flex;gap:24px;font-size:12px;color:var(--muted);margin-bottom:14px;flex-wrap:wrap;">' +
      '<span>Streak: <strong style="color:' + stCol + '">' + stStr + '</strong></span>' +
      '<span>Total trades: <strong style="color:var(--teal)">' + total + '</strong></span>' +
      '<span>Win rate: <strong style="color:var(--text)">' + fmt(perf.win_rate,1) + '%</strong></span>' +
      '</div>';

    /* Direction breakdown */
    var dirKeys = Object.keys(dirStats);
    if(dirKeys.length > 0){
      perfHTML += '<div class="p2-section-hdr">Direction Breakdown</div><div class="p2-stat-grid">';
      dirKeys.forEach(function(dk){
        var ds  = dirStats[dk];
        var dcl = dk==='LONG' ? 'bull' : 'bear';
        var wcl = ds.win_rate >= 50 ? 'bull' : 'bear';
        perfHTML += '<div class="p2-stat-box">' +
          '<div class="p2-stat-label ' + dcl + '">' + dk + '</div>' +
          '<div class="p2-stat-val ' + wcl + '">' + ds.win_rate + '%</div>' +
          '<div class="p2-stat-sub">' + ds.wins + ' W / ' + (ds.trades-ds.wins) + ' L — ' + ds.trades + ' trades</div>' +
          '</div>';
      });
      perfHTML += '</div>';
    }

    /* Session breakdown */
    var sesKeys = Object.keys(sesStats);
    if(sesKeys.length > 0){
      var sesRange = {'Asian':'00:00–08:00', 'European':'08:00–16:00', 'US':'16:00–00:00'};
      perfHTML += '<div class="p2-section-hdr">Session Breakdown (UTC)</div><div class="p2-stat-grid">';
      sesKeys.forEach(function(sk){
        var ss  = sesStats[sk];
        var wcl = ss.win_rate >= 50 ? 'bull' : 'bear';
        perfHTML += '<div class="p2-stat-box">' +
          '<div class="p2-stat-label">' + sk + '</div>' +
          '<div class="p2-stat-val ' + wcl + '">' + ss.win_rate + '%</div>' +
          '<div class="p2-stat-sub">' + ss.wins + ' W / ' + (ss.trades-ss.wins) + ' L — ' + (sesRange[sk]||'') + '</div>' +
          '</div>';
      });
      perfHTML += '</div>';
    }

    /* Strongest / weakest conditions */
    var strongest = perf.strongest_conditions || [];
    var weakest   = perf.weakest_conditions   || [];
    perfHTML += '<div class="p2-section-hdr">Conditions</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
      '<div style="background:rgba(63,185,80,0.06);border:1px solid rgba(63,185,80,0.2);border-radius:4px;padding:8px 10px;">' +
      '<div style="font-size:9px;color:var(--green);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;">Strongest</div>' +
      (strongest.length > 0
        ? strongest.map(function(s){ return '<div style="font-size:11px;color:var(--text);padding:1px 0">• '+s+'</div>'; }).join('')
        : '<div style="font-size:11px;color:var(--muted)">Insufficient data (need 10+ trades)</div>') +
      '</div>' +
      '<div style="background:rgba(248,81,73,0.06);border:1px solid rgba(248,81,73,0.2);border-radius:4px;padding:8px 10px;">' +
      '<div style="font-size:9px;color:var(--red);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;">Weakest / Avoid</div>' +
      (weakest.length > 0
        ? weakest.map(function(s){ return '<div style="font-size:11px;color:var(--text);padding:1px 0">• '+s+'</div>'; }).join('')
        : '<div style="font-size:11px;color:var(--muted)">Insufficient data (need 10+ trades)</div>') +
      '</div></div>';

    // Three-zone Morgan panel (24 Jul 2026): CRITICAL (<30) hard-blocks new entries;
    // WARNING (30-49) trading continues; both offer a manual reset to 50.
    var mScore2 = (perf.morgan_raw != null ? perf.morgan_raw : score);
    var reset2  = '<button onclick="resetMorgan()" style="margin-top:6px;padding:4px 11px;background:var(--red);color:#fff;border:none;border-radius:3px;font-size:11px;font-weight:700;cursor:pointer;">RESET MORGAN TO 50</button>';
    var lr2     = perf.morgan_last_reset
      ? '<div style="margin-top:4px;font-size:10px;color:var(--muted);">Morgan last reset: ' + perf.morgan_last_reset + '</div>'
      : '';
    if(perf.morgan_hard_block){
      perfHTML += '<div class="cons-warn">&#128680; MORGAN CRITICAL — Score: ' + mScore2 + '/100<br>' +
        '<span style="font-weight:400;color:var(--muted)">New entries suspended. Gaius intervention active. Existing positions still managed.</span><br>' +
        reset2 + lr2 + '</div>';
    } else if(perf.morgan_below_floor){
      perfHTML += '<div style="margin-top:8px;padding:5px 9px;background:rgba(210,153,34,0.14);border:1px solid var(--amber,#d29922);border-radius:3px;font-size:10px;color:var(--amber,#d29922);font-weight:700;">' +
        '&#9888; MORGAN WARNING — Score: ' + mScore2 + '/100<br>' +
        '<span style="font-weight:400;color:var(--muted)">Performance under review (zone 30-49). Trading continues. Manual reset available.</span><br>' +
        reset2 + lr2 + '</div>';
    } else if(lr2){
      perfHTML += lr2;
    }
  }

  document.getElementById('p2-perf-detail').innerHTML =
    '<div class="card-title purple">Ace Self-Performance — Detail</div>' + perfHTML;

  /* Monthly breakdown */
  var monthHTML = '';
  if(monthly.length === 0){
    monthHTML = '<div style="color:var(--muted);font-size:12px;padding:14px 0;text-align:center">No trade data yet</div>';
  } else {
    var allPnls  = monthly.map(function(m){ return m.gross_pnl; });
    var bestPnl  = Math.max.apply(null, allPnls);
    var worstPnl = Math.min.apply(null, allPnls);
    monthHTML = '<table class="p2-table"><thead><tr>' +
      '<th>Month</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>Gross P&amp;L</th><th>Net P&amp;L</th>' +
      '</tr></thead><tbody>';
    monthly.slice().reverse().forEach(function(m){
      var rowCls = '';
      if(monthly.length > 1){
        if(m.gross_pnl === bestPnl)       rowCls = ' class="month-best"';
        else if(m.gross_pnl === worstPnl) rowCls = ' class="month-worst"';
      }
      monthHTML += '<tr' + rowCls + '>' +
        '<td>' + m.month + '</td>' +
        '<td>' + m.trades + '</td>' +
        '<td>' + m.wins + '</td>' +
        '<td><span class="' + (m.win_rate>=50?'win':'loss') + '">' + m.win_rate + '%</span></td>' +
        '<td><span class="' + (m.gross_pnl>=0?'win':'loss') + '">GBP ' + fmtPnl(m.gross_pnl) + '</span></td>' +
        '<td><span class="' + (m.net_pnl>=0?'win':'loss') + '">GBP ' + fmtPnl(m.net_pnl) + '</span></td>' +
        '</tr>';
    });
    monthHTML += '</tbody></table>';
  }
  document.getElementById('p2-monthly').innerHTML =
    '<div class="card-title">Monthly Breakdown</div>' + monthHTML;

  /* Full trade history */
  var tradeHTML = '';
  if(trades.length === 0){
    tradeHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:14px 0">No trades yet — watching for setups</div>';
  } else {
    tradeHTML = '<table class="p2-table"><thead><tr>' +
      '<th>Dir</th><th>Entry Time</th><th>Entry GBP</th>' +
      '<th>Exit Time</th><th>Exit GBP</th><th>Size</th><th>P&amp;L</th>' +
      '<th title="Max Adverse Excursion (GBP)">MAE</th><th title="Max Favourable Excursion (GBP)">MFE</th>' +
      '<th>Reason</th></tr></thead><tbody>';
    tradeHTML += trades.map(function(t){
      var rowCls = t.pnl_class==='win' ? ' class="tr-win"' : ' class="tr-loss"';
      return '<tr' + rowCls + '>' +
        '<td class="dir-' + t.direction.toLowerCase() + '">' + t.direction + '</td>' +
        '<td>' + t.entry_time + '</td>' +
        '<td>GBP ' + t.entry_price + '</td>' +
        '<td>' + t.exit_time + '</td>' +
        '<td>GBP ' + t.exit_price + '</td>' +
        '<td>GBP ' + t.size + '</td>' +
        '<td class="' + t.pnl_class + '">' + t.pnl + '</td>' +
        '<td style="color:var(--muted)">' + (t.mae || '--') + '</td>' +
        '<td style="color:var(--muted)">' + (t.mfe || '--') + '</td>' +
        '<td style="color:var(--muted)">' + t.reason + '</td>' +
        '</tr>';
    }).join('');
    tradeHTML += '</tbody></table>';
  }
  document.getElementById('p2-trades').innerHTML =
    '<div class="card-title">BTC Trade History</div>' + tradeHTML;

  /* ETH full trade history */
  var ethTrades = d.trades_eth || [];
  var ethTradeHTML = '';
  if(ethTrades.length === 0){
    ethTradeHTML = '<div style="color:var(--muted);font-size:12px;text-align:center;padding:14px 0">No ETH trades yet</div>';
  } else {
    ethTradeHTML = '<table class="p2-table"><thead><tr>' +
      '<th>Dir</th><th>Entry Time</th><th>Entry GBP</th>' +
      '<th>Exit Time</th><th>Exit GBP</th><th>Size</th><th>P&amp;L</th>' +
      '<th title="Max Adverse Excursion (GBP)">MAE</th><th title="Max Favourable Excursion (GBP)">MFE</th>' +
      '<th>Reason</th></tr></thead><tbody>';
    ethTradeHTML += ethTrades.map(function(t){
      var rowCls = t.pnl_class==='win' ? ' class="tr-win"' : ' class="tr-loss"';
      return '<tr' + rowCls + '>' +
        '<td class="dir-' + t.direction.toLowerCase() + '">' + t.direction + '</td>' +
        '<td>' + t.entry_time + '</td>' +
        '<td>GBP ' + t.entry_price + '</td>' +
        '<td>' + t.exit_time + '</td>' +
        '<td>GBP ' + t.exit_price + '</td>' +
        '<td>GBP ' + t.size + '</td>' +
        '<td class="' + t.pnl_class + '">' + t.pnl + '</td>' +
        '<td style="color:var(--muted)">' + (t.mae || '--') + '</td>' +
        '<td style="color:var(--muted)">' + (t.mfe || '--') + '</td>' +
        '<td style="color:var(--muted)">' + t.reason + '</td>' +
        '</tr>';
    }).join('');
    ethTradeHTML += '</tbody></table>';
  }
  var ethTradesEl = document.getElementById('p2-eth-trades');
  if(ethTradesEl) ethTradesEl.innerHTML = '<div class="card-title purple">ETH Trade History</div>' + ethTradeHTML;
}

/* ── Main refresh loop ──────────────────────────────────────────────────── */
function refreshDashboard(){
  fetch('/api/state')
    .then(function(r){ return r.json(); })
    .then(function(d){
      renderPage1(d);
      renderPage2(d);
    })
    .catch(function(e){ console.error('Refresh error:', e); });
}

refreshDashboard();
setInterval(refreshDashboard, 30000);
</script>
<!-- ARCHIE BRIEF (Job 5) -->
<script>
(function(){
  var ARCHIE_LABEL = '&#9993; Archie Brief';
  function fallback(txt, done){
    var ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-2000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta); done();
  }
  function copyText(txt, btn){
    function done(){
      btn.classList.add('archie-copied');
      btn.textContent='Copied!';
      setTimeout(function(){ btn.classList.remove('archie-copied'); btn.innerHTML=ARCHIE_LABEL; },2000);
    }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done, function(){ fallback(txt, done); });
    } else { fallback(txt, done); }
  }
  window.archieBrief=function(btn){
    btn.textContent='...';
    fetch('/api/archie-brief').then(function(r){return r.text();}).then(function(txt){
      copyText(txt, btn);
    }).catch(function(){ btn.textContent='Error'; setTimeout(function(){ btn.innerHTML=ARCHIE_LABEL; },2000); });
  };
  function inject(){
    if(document.getElementById('archieBtn')) return;
    var st=document.createElement('style');
    st.textContent='.archie-btn{background:rgba(52,152,219,0.10);border:1px solid #3498db;color:#3498db;padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;transition:background 0.15s;}.archie-btn:hover{background:rgba(52,152,219,0.25);}.archie-btn.archie-copied{background:rgba(46,204,113,0.22);border-color:#2ecc71;color:#2ecc71;}';
    document.head.appendChild(st);
    var btn=document.createElement('button');
    btn.id='archieBtn'; btn.className='archie-btn'; btn.type='button';
    btn.innerHTML=ARCHIE_LABEL; btn.setAttribute('onclick','archieBrief(this)');
    var sd=document.querySelector('.shutdown-btn');
    if(sd && sd.parentNode){ sd.parentNode.insertBefore(btn, sd); }
    else { var hr=document.querySelector('.header-right')||document.querySelector('.header'); if(hr){ hr.appendChild(btn); } }
  }
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', inject); }
  else { inject(); }
})();
</script>
<!-- PHANTOM BRIEF -->
<script>
(function(){
  var L='&#9993; PHANTOM BRIEF';
  function fb(txt,done){var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.top='-2000px';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);done();}
  function cp(txt,btn){function done(){btn.classList.add('archie-copied');btn.textContent='Copied!';setTimeout(function(){btn.classList.remove('archie-copied');btn.innerHTML=L;},2000);}if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,function(){fb(txt,done);});}else{fb(txt,done);}}
  window.phantomBrief=function(btn){btn.textContent='...';fetch('/api/phantom-brief').then(function(r){return r.text();}).then(function(txt){cp(txt,btn);}).catch(function(){btn.textContent='Error';setTimeout(function(){btn.innerHTML=L;},2000);});};
  function inject(){
    if(document.getElementById('phantomBriefBtn'))return;
    var head=document.querySelector('.phantom-head');if(!head)return;
    if(!document.getElementById('phBriefStyle')){var st=document.createElement('style');st.id='phBriefStyle';st.textContent='.archie-btn{background:rgba(52,152,219,0.10);border:1px solid #3498db;color:#3498db;padding:4px 9px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:0.5px;text-transform:uppercase;}.archie-btn:hover{background:rgba(52,152,219,0.25);}.archie-btn.archie-copied{background:rgba(46,204,113,0.22);border-color:#2ecc71;color:#2ecc71;}';document.head.appendChild(st);}
    var btn=document.createElement('button');btn.id='phantomBriefBtn';btn.className='archie-btn';btn.type='button';btn.innerHTML=L;btn.setAttribute('onclick','phantomBrief(this)');
    head.appendChild(btn);
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',inject);}else{inject();}
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(
        HTML.replace("__VERSION_STRING__", VERSION_STRING).replace("__APP_VERSION__", APP_VERSION),
        mimetype="text/html")


@app.route("/logo")
def serve_logo():
    if LOGO_PNG.exists():
        try:
            return Response(LOGO_PNG.read_bytes(), mimetype="image/png")
        except Exception:
            pass
    return Response(b"", status=404)


def _phantom_verdict(pnl, thr):
    if pnl is None:
        return None
    if pnl > thr:
        return 'WRONG'
    if pnl < -thr:
        return 'CORRECT'
    return 'NEUTRAL'


def build_phantom_brief():
    """Plain-text phantom-trades brief for pasting to Archie (Phantom Page
    Enhancements, 21 Jul 2026). Multi-horizon moves + 30min/2hr verdict
    distributions computed on the fly -- the stored 1hr verdict is unchanged."""
    import phantom_tracker as _pt
    from datetime import datetime, timezone
    name = "CryptoHybrid"
    has_market = True
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'phantom_trades.csv')
    rows = []
    if os.path.exists(csv_path):
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
        except Exception:
            rows = []
    recent = rows[-50:]

    def fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def thr_for(r):
        t = _pt.VERDICT_THRESHOLD
        if isinstance(t, dict):
            m = (r.get('market') or '').upper()
            if 'ETH' in m:
                return t.get('ETH', 4.0)
            if 'BTC' in m:
                return t.get('BTC', 14.0)
            return getattr(_pt, 'VERDICT_THRESHOLD_DEFAULT', 10.0)
        return t

    def mv(v):
        n = fnum(v)
        if n is None:
            return '--'
        return ('+£%.2f' % n) if n >= 0 else ('-£%.2f' % abs(n))

    correct = sum(1 for r in recent if r.get('verdict') == 'CORRECT')
    wrong = sum(1 for r in recent if r.get('verdict') == 'WRONG')
    neutral = sum(1 for r in recent if r.get('verdict') == 'NEUTRAL')
    total = correct + wrong + neutral
    quality = round(correct / total * 100) if total else 0
    net_saved = sum(fnum(r.get('pnl_1hr')) or 0 for r in recent if r.get('verdict') == 'CORRECT')
    net_missed = sum(fnum(r.get('pnl_1hr')) or 0 for r in recent if r.get('verdict') == 'WRONG')

    def dist(col):
        c = w = n = 0
        for r in recent:
            v = _phantom_verdict(fnum(r.get(col)), thr_for(r))
            if v == 'CORRECT':
                c += 1
            elif v == 'WRONG':
                w += 1
            elif v == 'NEUTRAL':
                n += 1
        return c, w, n

    c30, w30, n30 = dist('pnl_30min')
    c2h, w2h, n2h = dist('pnl_2hr')

    flips = both = wc = cw = 0
    for r in recent:
        v1 = _phantom_verdict(fnum(r.get('pnl_1hr')), thr_for(r))
        v2 = _phantom_verdict(fnum(r.get('pnl_2hr')), thr_for(r))
        if v1 and v2:
            both += 1
            if v1 != v2:
                flips += 1
                if v1 == 'WRONG' and v2 == 'CORRECT':
                    wc += 1
                elif v1 == 'CORRECT' and v2 == 'WRONG':
                    cw += 1
    flip_rate = round(flips / both * 100) if both else 0
    common = 'WRONG->CORRECT' if wc >= cw else 'CORRECT->WRONG'

    bar = '=' * 64
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    L = []
    L.append(bar)
    L.append('ARCHIE BRIEF -- %s PHANTOM TRADES' % name.upper())
    L.append('Generated: %s UTC' % ts)
    L.append(bar)
    L.append('')
    L.append('SUMMARY')
    L.append('  Quality: %d%% | Last %d decisions' % (quality, len(recent)))
    L.append('  Correct: %d | Wrong: %d | Neutral: %d' % (correct, wrong, neutral))
    L.append('  Net Saved: GBP +%.2f | Net Missed: GBP -%.2f' % (abs(net_saved), abs(net_missed)))
    L.append('')
    L.append('TIME HORIZON ANALYSIS (from available data)')
    L.append('  30min verdict distribution:')
    L.append('    Correct: %d | Wrong: %d | Neutral: %d' % (c30, w30, n30))
    L.append('  2hr verdict distribution:')
    L.append('    Correct: %d | Wrong: %d | Neutral: %d' % (c2h, w2h, n2h))
    L.append('  Verdict flip rate (1hr->2hr): %d%% of rows change verdict' % flip_rate)
    L.append('  Most common flip: %s (%d WRONG->CORRECT, %d CORRECT->WRONG)' % (common, wc, cw))
    L.append('')
    L.append('RECENT PHANTOM TRADES (last 10)')
    for r in reversed(recent[-10:]):
        tsr = (r.get('timestamp') or '')[:16].replace('T', ' ')
        mkt = ('%s | ' % (r.get('market') or '--')) if has_market else ''
        L.append('  %s | %s%s | conf %s | 5m:%s 10m:%s 15m:%s 30m:%s 1hr:%s 2hr:%s | %s' % (
            tsr, mkt, (r.get('direction_blocked') or '--'), (r.get('confidence') or '--'),
            mv(r.get('pnl_5min')), mv(r.get('pnl_10min')), mv(r.get('pnl_15min')),
            mv(r.get('pnl_30min')), mv(r.get('pnl_1hr')), mv(r.get('pnl_2hr')),
            (r.get('verdict') or 'PENDING')))
    L.append('')
    L.append(bar)
    L.append('End of %s Phantom Archie Brief' % name)
    L.append(bar)
    return '\n'.join(L)


@app.route("/api/phantom-brief")
def api_phantom_brief():
    return build_phantom_brief(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/archie-brief")
def api_archie_brief():
    """Plain-text snapshot of current dashboard state for pasting to Archie."""
    import json as _json
    import archie_brief
    try:
        state = _json.loads(api_state().get_data(as_text=True))
    except Exception:
        state = get_state()
    txt = archie_brief.build_system_brief(state, "CryptoHybrid", "BTC / ETH", str(LOG_DIR))
    return txt, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/state")
def api_state():
    from data_feed_btc import get_composite_signal

    try:
        # ── BTC data ─────────────────────────────────────────────────────────
        account   = load_account_stats()
        trades    = load_trades()
        monthly   = load_monthly_stats()
        breakdown = load_direction_session_stats()
        state     = get_state()

        whale_raw   = state.get("whale", {})
        liq_compact = whale_raw.get("_liq_compact")
        whale_clean = {k: v for k, v in whale_raw.items() if not k.startswith("_")}

        ind1h     = {}
        ind5m     = {}
        ind1d     = {}
        trend_1h  = "NEUTRAL"
        signal_5m = "NEUTRAL"
        trend_1d  = "NEUTRAL"
        price     = 0.0

        feed = getattr(app, "_feed", None)
        if feed:
            try:
                bar_1h = feed.latest_bar("1h")
                bar_5m = feed.latest_bar("5m")
                price  = float(bar_5m.get("close", 0))

                def safe(v):
                    return None if pd.isna(v) else float(v)

                ind1h = {
                    "ssl_bull":   bool(bar_1h.get("ssl_bull", False)),
                    "rsi":        safe(bar_1h.get("rsi")),
                    "macd":       safe(bar_1h.get("macd")),
                    "tmo_main":   safe(bar_1h.get("tmo_main")),
                    "chande_mo":  safe(bar_1h.get("chande_mo")),
                    "money_flow": safe(bar_1h.get("money_flow")),
                }
                ind5m = {
                    "ssl_bull":   bool(bar_5m.get("ssl_bull", False)),
                    "rsi":        safe(bar_5m.get("rsi")),
                    "macd":       safe(bar_5m.get("macd")),
                    "tmo_main":   safe(bar_5m.get("tmo_main")),
                    "chande_mo":  safe(bar_5m.get("chande_mo")),
                    "money_flow": safe(bar_5m.get("money_flow")),
                }
                trend_1h  = get_composite_signal(bar_1h)
                signal_5m = get_composite_signal(bar_5m)
                try:
                    bar_1d = feed.latest_bar("1d")
                    ind1d  = {
                        "ssl_bull": bool(bar_1d.get("ssl_bull", False)),
                        "rsi":      safe(bar_1d.get("rsi")),
                    }
                    trend_1d = "LONG" if bar_1d.get("ssl_bull") else "SHORT"
                except Exception:
                    pass
            except Exception:
                pass

        btc_data = {
            "price":           price,
            "trend_1d":        trend_1d,
            "indicators_1d":   ind1d,
            "trend_1h":        trend_1h,
            "signal_5m":       signal_5m,
            "indicators_1h":   ind1h,
            "indicators_5m":   ind5m,
            "panel_mode":      state.get("panel_mode", "pre_checks"),
            "decision":        state.get("decision", "STAY_OUT"),
            "confidence":      state.get("confidence", "--"),
            "one_hour_bias":   state.get("one_hour_bias", "--"),
            "reasoning":       state.get("reasoning", "Waiting for next analysis cycle..."),
            "warnings":        state.get("warnings", []),
            "tokens_used":     state.get("tokens_used", "--"),
            "pre_checks":      state.get("pre_checks", {}),
            "block_reason":    state.get("block_reason", ""),
            "checklist":       state.get("checklist", {}),
            "whale":           whale_clean,
            "liq_compact":     liq_compact,
            "position":        state.get("position"),
            "performance":     state.get("performance", {}),
            "last_update_utc": state.get("last_update_utc", ""),
        }

        # ── ETH data (from pushed state) ─────────────────────────────────────
        eth_state = get_eth_state()
        # ETH whale/liq is computed at source by get_whale_data(eth_feed, ...) and
        # pushed on eth_state["whale"] — extract the compact liq block the same way
        # BTC does (it was previously discarded).
        eth_whale_raw   = eth_state.get("whale", {}) or {}
        eth_liq_compact = eth_whale_raw.get("_liq_compact")
        eth_whale_clean = {k: v for k, v in eth_whale_raw.items() if not k.startswith("_")}
        eth_data = {
            "price":           eth_state.get("price", 0.0),
            "trend_1d":        eth_state.get("trend_1d", "NEUTRAL"),
            "indicators_1d":   eth_state.get("indicators_1d", {}),
            "trend_1h":        eth_state.get("trend_1h", "NEUTRAL"),
            "signal_5m":       eth_state.get("signal_5m", "NEUTRAL"),
            "indicators_1h":   eth_state.get("indicators_1h", {}),
            "indicators_5m":   eth_state.get("indicators_5m", {}),
            "panel_mode":      eth_state.get("panel_mode", "pre_checks"),
            "decision":        eth_state.get("decision", "STAY_OUT"),
            "confidence":      eth_state.get("confidence", "--"),
            "one_hour_bias":   eth_state.get("one_hour_bias", "--"),
            "reasoning":       eth_state.get("reasoning", "Waiting for ETH analysis..."),
            "warnings":        eth_state.get("warnings", []),
            "tokens_used":     eth_state.get("tokens_used", "--"),
            "pre_checks":      eth_state.get("pre_checks", {}),
            "block_reason":    eth_state.get("block_reason", ""),
            "checklist":       eth_state.get("checklist", {}),
            "whale":           eth_whale_clean,
            "liq_compact":     eth_liq_compact,
            "position":        eth_state.get("position"),
            "performance":     eth_state.get("performance", {}),
            "last_update_utc": eth_state.get("last_update_utc", ""),
        }
        eth_data.update(compute_status_fields(eth_state))

        # ── ETH account / trades ──────────────────────────────────────────────
        eth_account  = load_eth_account_stats()
        eth_trades   = load_eth_trades()
        eth_monthly  = load_eth_monthly_stats()

        combined_capital = account.get("capital", STARTING_CAPITAL) + eth_account.get("capital", STARTING_CAPITAL)

        btc_status = compute_status_fields(state)

        return jsonify({
            "version":         APP_VERSION,
            "version_string":  VERSION_STRING,
            "stay_out_quality": get_stay_out_quality(),
            # Legacy BTC flat fields (backwards-compatible for JS)
            "price":           btc_data["price"],
            "trend_1d":        btc_data["trend_1d"],
            "indicators_1d":   btc_data["indicators_1d"],
            "trend_1h":        btc_data["trend_1h"],
            "signal_5m":       btc_data["signal_5m"],
            "indicators_1h":   btc_data["indicators_1h"],
            "indicators_5m":   btc_data["indicators_5m"],
            "panel_mode":      btc_data["panel_mode"],
            "decision":        btc_data["decision"],
            "confidence":      btc_data["confidence"],
            "one_hour_bias":   btc_data["one_hour_bias"],
            "reasoning":       btc_data["reasoning"],
            "warnings":        btc_data["warnings"],
            "tokens_used":     btc_data["tokens_used"],
            "pre_checks":      btc_data["pre_checks"],
            "block_reason":    btc_data["block_reason"],
            "checklist":       btc_data["checklist"],
            "whale":           btc_data["whale"],
            "liq_compact":     btc_data["liq_compact"],
            "position":        btc_data["position"],
            "performance":     btc_data["performance"],
            "last_update_utc": btc_data["last_update_utc"],
            "account":         account,
            "trades":          trades,
            "monthly_stats":   monthly,
            "breakdown":       breakdown,
            # ETH nested data
            "eth":             eth_data,
            "account_eth":     eth_account,
            "trades_eth":      eth_trades,
            "monthly_stats_eth": eth_monthly,
            "combined_capital":  combined_capital,
            # Flat Lancelot / Arthur / locked_pnl summary fields (BTC top-level)
            **btc_status,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.get_json(silent=True) or {}
    set_state(data)
    return jsonify({"ok": True})


@app.route("/api/lift-confidence", methods=["POST"])
def api_lift_confidence():
    """Request a manual Morgan confidence lift (Gaius intervention Step 4). Writes
    logs/confidence_lift.json; the trading engine applies it in-process on its next
    cycle -- LIVE, no restart. Optional JSON body {"to": <0-100>} (default 50)."""
    import json
    to = 50.0
    try:
        body = request.get_json(force=True, silent=True) or {}
        if body.get("to") is not None:
            to = max(0.0, min(100.0, float(body["to"])))
    except Exception:
        to = 50.0
    ts = datetime.now(timezone.utc).isoformat()
    reason = ("CONFIDENCE LIFT -- Gaius intervention. Manual reset to %g via "
              "/api/lift-confidence. %s" % (to, ts))
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "confidence_lift.json").write_text(
            json.dumps({"confidence": to, "reason": reason, "requested_utc": ts}),
            encoding="utf-8")
        return jsonify({"status": "lift_requested", "to": to,
                        "note": "engine applies on next cycle (live, no restart)"})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/api/reset-morgan", methods=["POST"])
def api_reset_morgan():
    """Manual Morgan reset to 50 (Nick-controlled, three-zone model). Morgan is allowed
    to drop into the WARNING (30-49) or HARD BLOCK (<30) zones and a dashboard panel
    fires; Nick reviews the evidence and clicks RESET. Writes confidence_lift.json (engine
    applies live, no restart) + records the reset timestamp for the dashboard/Archie Brief."""
    import json
    ts = datetime.now(timezone.utc)
    ts_iso = ts.isoformat()
    ts_disp = ts.strftime("%Y-%m-%d %H:%M UTC")
    reason = "MANUAL MORGAN RESET to 50 via /api/reset-morgan (Nick). %s" % ts_disp
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "confidence_lift.json").write_text(
            json.dumps({"confidence": 50.0, "reason": reason, "requested_utc": ts_iso}),
            encoding="utf-8")
        (LOG_DIR / "morgan_last_reset.json").write_text(
            json.dumps({"reset_utc": ts_disp}), encoding="utf-8")
        return jsonify({"status": "reset_requested", "to": 50,
                        "confirmation": "Morgan reset to 50 at %s" % ts.strftime("%H:%M UTC"),
                        "note": "engine applies on next cycle (live, no restart)"})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Write shutdown flag for main trader, then kill this dashboard process."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SHUTDOWN_FLAG.write_text("shutdown requested\n", encoding="utf-8")
        log.info("Shutdown flag written -- main trader will exit on next check")
    except Exception as e:
        log.warning("Could not write shutdown flag: %s", e)

    def _kill():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"status": "shutting_down"})


def feed_thread():
    from data_feed_btc import BTCDataFeed
    feed = BTCDataFeed()
    feed.initialise()
    app._feed = feed
    while True:
        try:
            feed.refresh()
        except Exception as e:
            log.warning("Feed refresh error: %s", e)
        time.sleep(REFRESH_SECONDS)


def main():
    _try_convert_logo()
    print("=" * 60)
    print("  CryptoHybrid AI -- Browser Dashboard")
    print(f"  Open http://localhost:{PORT} in your browser")
    print("  Press Ctrl+C to close")
    print("=" * 60)
    t = threading.Thread(target=feed_thread, daemon=True)
    t.start()
    print("  Loading data feed...")
    time.sleep(8)
    print(f"  Dashboard ready at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
