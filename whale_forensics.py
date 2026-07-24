"""
Whale Signal Forensic Analysis Tool — CryptoHybrid AI
Parses cryptohybrid.log and analyses every tick where Moby scored 70+ in either direction.
"""

import re
import os
import csv
import sys
import io
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

# Force UTF-8 output on Windows so pound signs and em-dashes print correctly
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent

LOG_PATH  = BASE_DIR / "logs" / "cryptohybrid.log"
OUT_TXT   = BASE_DIR / "logs" / "whale_forensics.txt"
OUT_CSV   = BASE_DIR / "logs" / "whale_signals.csv"

# ──────────────────────────────────────────────
# REGEX PATTERNS
# ──────────────────────────────────────────────
RE_TICK_HEADER = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+"
    r"CryptoHybrid AI -- Candle Tick #(\d+)"
)
RE_1D = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+\[1d\] \d+ candles.*?"
    r"close: GBP ([\d.]+).*?rsi=([\d.]+).*?ssl=(UP|DOWN)"
)
RE_1H = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+\[1h\] \d+ candles.*?"
    r"close: GBP ([\d.]+).*?rsi=([\d.]+).*?ssl=(UP|DOWN)"
)
RE_5M = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+\[5m\] \d+ candles.*?"
    r"close: GBP ([\d.]+).*?rsi=([\d.]+).*?ssl=(UP|DOWN)"
)
RE_PRICE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+BTC/GBP = GBP ([\d.]+)"
)
RE_HUNT = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+Whale data ready.*?hunt=\((\d+)/(\d+) (\w+)\)"
)
RE_PRECHECKS_PASS = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+All pre-checks passed -- calling Claude"
)
RE_CLAUDE_DECISION = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+Claude decision: (\w+) \| Confidence: (\w+)"
)
RE_ACTION = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+Action: (\S+)"
)
RE_BLOCKED = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+INFO\s+PRE-CHECK BLOCKED"
)


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────────────
# PASS 1 — parse the whole log into ticks
# ──────────────────────────────────────────────
def parse_log(log_path):
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    ticks = []          # list of dicts, one per candle tick
    price_series = []   # (datetime, price) for every 5m candle

    current_tick = None

    for line in lines:
        # ── new candle tick boundary ──
        m = RE_TICK_HEADER.search(line)
        if m:
            if current_tick:
                ticks.append(current_tick)
            current_tick = {
                "tick_num":         int(m.group(2)),
                "timestamp":        parse_dt(m.group(1)),
                "price":            None,
                "daily_close":      None,
                "daily_rsi":        None,
                "daily_ssl":        None,
                "h1_close":         None,
                "h1_rsi":           None,
                "h1_ssl":           None,
                "h5m_close":        None,
                "h5m_rsi":          None,
                "h5m_ssl":          None,
                "hunt_down":        None,
                "hunt_up":          None,
                "hunt_verdict":     None,
                "prechecks_passed": False,
                "precheck_blocked": False,
                "claude_called":    False,
                "claude_decision":  None,
                "claude_confidence":None,
                "action":           None,
            }
            continue

        if current_tick is None:
            continue

        # ── 1d data ──
        m = RE_1D.search(line)
        if m:
            current_tick["daily_close"] = float(m.group(2))
            current_tick["daily_rsi"]   = float(m.group(3))
            current_tick["daily_ssl"]   = "BULL" if m.group(4) == "UP" else "BEAR"
            continue

        # ── 1h data ──
        m = RE_1H.search(line)
        if m:
            current_tick["h1_close"] = float(m.group(2))
            current_tick["h1_rsi"]   = float(m.group(3))
            current_tick["h1_ssl"]   = "BULL" if m.group(4) == "UP" else "BEAR"
            continue

        # ── 5m data ──
        m = RE_5M.search(line)
        if m:
            current_tick["h5m_close"] = float(m.group(2))
            current_tick["h5m_rsi"]   = float(m.group(3))
            current_tick["h5m_ssl"]   = "BULL" if m.group(4) == "UP" else "BEAR"
            continue

        # ── live price ──
        m = RE_PRICE.search(line)
        if m:
            current_tick["price"] = float(m.group(2))
            continue

        # ── whale / hunt ──
        m = RE_HUNT.search(line)
        if m:
            current_tick["hunt_down"]    = int(m.group(2))
            current_tick["hunt_up"]      = int(m.group(3))
            current_tick["hunt_verdict"] = m.group(4)
            continue

        # ── pre-checks blocked ──
        if RE_BLOCKED.search(line):
            current_tick["precheck_blocked"] = True
            continue

        # ── pre-checks passed ──
        if RE_PRECHECKS_PASS.search(line):
            current_tick["prechecks_passed"] = True
            current_tick["claude_called"]    = True
            continue

        # ── Claude decision ──
        m = RE_CLAUDE_DECISION.search(line)
        if m:
            current_tick["claude_decision"]   = m.group(2)
            current_tick["claude_confidence"] = m.group(3)
            continue

        # ── final action ──
        m = RE_ACTION.search(line)
        if m:
            current_tick["action"] = m.group(2).rstrip(".,;")
            continue

    if current_tick:
        ticks.append(current_tick)

    # build price time-series from all ticks
    for t in ticks:
        if t["price"] and t["timestamp"]:
            price_series.append((t["timestamp"], t["price"]))

    price_series.sort(key=lambda x: x[0])
    return ticks, price_series


def price_at(price_series, target_dt, tolerance_mins=7):
    """Return closest price to target_dt within tolerance, or None."""
    best, best_delta = None, timedelta(days=999)
    for dt, px in price_series:
        delta = abs(dt - target_dt)
        if delta < best_delta and delta <= timedelta(minutes=tolerance_mins):
            best, best_delta = px, delta
    return best


# ──────────────────────────────────────────────
# PASS 2 — filter high-conviction signals
# ──────────────────────────────────────────────
def is_high_conviction(tick):
    d = tick["hunt_down"]
    u = tick["hunt_up"]
    if d is None or u is None:
        return False
    return d >= 70 or u >= 70


def signal_direction(tick):
    d, u = tick["hunt_down"], tick["hunt_up"]
    if d >= 70 and u >= 70:
        return "BOTH"
    if d >= 70:
        return "DOWN"
    return "UP"


def score_band(score):
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    return "70-79"


# ──────────────────────────────────────────────
# PASS 3 — cross-reference future prices
# ──────────────────────────────────────────────
def enrich_with_future_prices(signals, price_series):
    for sig in signals:
        ts    = sig["timestamp"]
        entry = sig["price"] or sig["h5m_close"]
        sig["entry_price"] = entry

        p30  = price_at(price_series, ts + timedelta(minutes=30))
        p60  = price_at(price_series, ts + timedelta(minutes=60))
        p120 = price_at(price_series, ts + timedelta(minutes=120))

        sig["price_30m"]  = p30
        sig["price_60m"]  = p60
        sig["price_120m"] = p120

        # changes
        def chg(p):
            if entry and p:
                return round(p - entry, 2), round((p - entry) / entry * 100, 3)
            return None, None

        sig["chg_30m_gbp"],  sig["chg_30m_pct"]  = chg(p30)
        sig["chg_60m_gbp"],  sig["chg_60m_pct"]  = chg(p60)
        sig["chg_120m_gbp"], sig["chg_120m_pct"] = chg(p120)

        # max/min in 2-hour window
        window = [px for dt, px in price_series
                  if ts < dt <= ts + timedelta(minutes=120)]
        sig["max_2h"] = max(window) if window else None
        sig["min_2h"] = min(window) if window else None

        if entry:
            sig["max_fav_up"]   = round(sig["max_2h"] - entry, 2) if sig["max_2h"] else None
            sig["max_fav_down"] = round(entry - sig["min_2h"], 2) if sig["min_2h"] else None
        else:
            sig["max_fav_up"] = sig["max_fav_down"] = None

        # direction correctness
        direction = sig["signal_direction"]

        def correct(chg_gbp, dir_wanted):
            if chg_gbp is None:
                return None
            if dir_wanted == "DOWN":
                return chg_gbp < 0
            if dir_wanted == "UP":
                return chg_gbp > 0
            return None  # BOTH — ambiguous

        primary = direction if direction != "BOTH" else sig["hunt_verdict"].replace("WARD", "").replace("UPWARD", "UP").replace("DOWNWARD", "DOWN")
        sig["correct_30m"]  = correct(sig["chg_30m_gbp"],  primary)
        sig["correct_60m"]  = correct(sig["chg_60m_gbp"],  primary)
        sig["correct_120m"] = correct(sig["chg_120m_gbp"], primary)

    return signals


# ──────────────────────────────────────────────
# PASS 4 — pattern analysis
# ──────────────────────────────────────────────
def pct_correct(signals, timepoint="30m"):
    key = f"correct_{timepoint}"
    rated = [s for s in signals if s.get(key) is not None]
    if not rated:
        return None, 0
    correct = sum(1 for s in rated if s[key])
    return round(correct / len(rated) * 100, 1), len(rated)


def accuracy_by_band(signals, timepoint="30m"):
    bands = {"70-79": [], "80-89": [], "90-100": []}
    for sig in signals:
        dir_ = sig["signal_direction"]
        if dir_ == "DOWN":
            score = sig["hunt_down"]
        elif dir_ == "UP":
            score = sig["hunt_up"]
        else:
            score = max(sig["hunt_down"], sig["hunt_up"])
        band = score_band(score)
        bands[band].append(sig)

    result = {}
    for band, sigs in bands.items():
        acc, n = pct_correct(sigs, timepoint)
        result[band] = (acc, n)
    return result


def avg_move(signals, correct_only=True, timepoint="120m"):
    key_gbp = f"chg_{timepoint}_gbp"
    key_ok  = f"correct_{timepoint}"
    subset = [s for s in signals if s.get(key_ok) is not None]
    if correct_only:
        subset = [s for s in subset if s[key_ok]]
    else:
        subset = [s for s in subset if not s[key_ok]]
    vals = [abs(s[key_gbp]) for s in subset if s.get(key_gbp) is not None]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 2), len(vals)


# ──────────────────────────────────────────────
# FORMAT HELPERS
# ──────────────────────────────────────────────
def pct_str(acc, n):
    if acc is None:
        return f"N/A (n={n})"
    return f"{acc}% (n={n})"


def gbp(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"£{sign}{v:.2f}"


def pct(v):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.3f}%"


def yn(v):
    if v is None:
        return "?"
    return "YES" if v else "NO"


def fmt_price(v):
    if v is None:
        return "N/A"
    return "£" + f"{v:,.2f}"


# ──────────────────────────────────────────────
# CSV MERGE (append new, skip existing by ts)
# ──────────────────────────────────────────────
CSV_FIELDS = [
    "timestamp", "tick_num", "entry_price",
    "hunt_down", "hunt_up", "hunt_verdict", "signal_direction",
    "daily_rsi", "daily_ssl", "h1_ssl",
    "prechecks_passed", "claude_called", "claude_decision", "claude_confidence",
    "price_30m", "chg_30m_gbp", "chg_30m_pct", "correct_30m",
    "price_60m", "chg_60m_gbp", "chg_60m_pct", "correct_60m",
    "price_120m", "chg_120m_gbp", "chg_120m_pct", "correct_120m",
    "max_2h", "min_2h", "max_fav_up", "max_fav_down",
]


def load_existing_csv_timestamps(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return {row["timestamp"] for row in reader}


def save_csv(signals, path):
    existing_ts = load_existing_csv_timestamps(path)
    new_signals = [s for s in signals
                   if s["timestamp"].strftime("%Y-%m-%d %H:%M:%S") not in existing_ts]

    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for sig in new_signals:
            row = {f: sig.get(f, "") for f in CSV_FIELDS}
            row["timestamp"] = sig["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            row["prechecks_passed"] = sig.get("prechecks_passed", False)
            row["claude_called"]    = sig.get("claude_called", False)
            writer.writerow(row)

    return len(new_signals), len(existing_ts)


# ──────────────────────────────────────────────
# REPORT BUILDER
# ──────────────────────────────────────────────
def build_report(signals, ticks):
    lines = []
    W = "=" * 72

    def h1(t):
        lines.append(W)
        lines.append(f"  {t}")
        lines.append(W)

    def h2(t):
        lines.append(f"\n{'-' * 60}")
        lines.append(f"  {t}")
        lines.append(f"{'-' * 60}")

    def row(label, val, pad=42):
        lines.append(f"  {label:<{pad}}{val}")

    # ────────────────────────────────────────────
    h1("WHALE SIGNAL FORENSIC ANALYSIS — CryptoHybrid AI")
    lines.append(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Log spans : {ticks[0]['timestamp']} to {ticks[-1]['timestamp']}")
    duration_days = (ticks[-1]["timestamp"] - ticks[0]["timestamp"]).total_seconds() / 86400
    lines.append(f"  Duration  : {duration_days:.1f} days")
    lines.append(f"  Total ticks analysed : {len(ticks)}")
    lines.append(f"  High-conviction signals (70+) : {len(signals)}")

    # ────────────────────────────────────────────
    h1("PART 1 — HIGH-CONVICTION SIGNAL TABLE")
    lines.append(
        f"  {'#':>3}  {'Timestamp':<20} {'Price':>9}  {'D':>3}{'U':>3}  "
        f"{'Verdict':<10} {'Dir':>4}  {'1h':>4}  {'1d':>4}  "
        f"{'RSI_d':>5}  {'Pass':>4}  {'Claude':<10}  {'Conf':<8}"
    )
    lines.append("  " + "-" * 94)

    for i, s in enumerate(signals, 1):
        lines.append(
            f"  {i:>3}  {s['timestamp'].strftime('%Y-%m-%d %H:%M'):<20} "
            f"£{s['entry_price']:>9,.2f}  "
            f"{s['hunt_down']:>3}{s['hunt_up']:>3}  "
            f"{s['hunt_verdict']:<10} "
            f"{s['signal_direction']:>4}  "
            f"{s.get('h1_ssl','?'):>4}  "
            f"{s.get('daily_ssl','?'):>4}  "
            f"{s.get('daily_rsi') or '?':>5}  "
            f"{'Y' if s['prechecks_passed'] else 'N':>4}  "
            f"{s.get('claude_decision') or 'N/A':<10}  "
            f"{s.get('claude_confidence') or '---':<8}"
        )

    # ────────────────────────────────────────────
    h1("PART 2 — PRICE MOVEMENT CROSS-REFERENCE")
    lines.append(
        f"  {'#':>3}  {'Dir':>4}  {'Entry':>9}  "
        f"{'30m':>9}  {'d30%':>8}  {'OK':>3}  "
        f"{'60m':>9}  {'d60%':>8}  {'OK':>3}  "
        f"{'120m':>9}  {'d120%':>9}  {'OK':>3}  "
        f"{'MaxFav':>8}"
    )
    lines.append("  " + "-" * 114)

    for i, s in enumerate(signals, 1):
        ep = s["entry_price"]
        dir_ = s["signal_direction"]
        max_fav = s["max_fav_down"] if dir_ == "DOWN" else s["max_fav_up"]
        lines.append(
            f"  {i:>3}  {dir_:>4}  "
            f"£{ep:>9,.2f}  "
            f"{fmt_price(s['price_30m']):>9}  "
            f"{pct(s['chg_30m_pct']):>8}  {yn(s['correct_30m']):>2}  "
            f"{fmt_price(s['price_60m']):>9}  "
            f"{pct(s['chg_60m_pct']):>8}  {yn(s['correct_60m']):>2}  "
            f"{fmt_price(s['price_120m']):>9}  "
            f"{pct(s['chg_120m_pct']):>9}  {yn(s['correct_120m']):>2}  "
            f"{gbp(max_fav):>8}"
        )

    # ────────────────────────────────────────────
    h1("PART 3 — PATTERN ANALYSIS")

    # overall accuracy
    h2("3a. Overall accuracy by timeframe")
    down_sigs = [s for s in signals if s["signal_direction"] == "DOWN"]
    up_sigs   = [s for s in signals if s["signal_direction"] == "UP"]
    both_sigs = [s for s in signals if s["signal_direction"] == "BOTH"]

    for label, subset in [("ALL signals", signals), ("DOWN only", down_sigs), ("UP only", up_sigs), ("BOTH (ambiguous)", both_sigs)]:
        acc30, n30   = pct_correct(subset, "30m")
        acc60, n60   = pct_correct(subset, "60m")
        acc120, n120 = pct_correct(subset, "120m")
        lines.append(f"\n  [{label}]  (n={len(subset)})")
        row(f"    Correct at 30m", pct_str(acc30, n30))
        row(f"    Correct at 60m", pct_str(acc60, n60))
        row(f"    Correct at 120m", pct_str(acc120, n120))

    # signal strength bands
    h2("3b. Accuracy by signal strength band")
    for tp in ["30m", "60m", "120m"]:
        lines.append(f"\n  Timepoint: {tp}")
        bands = accuracy_by_band(signals, tp)
        for band, (acc, n) in sorted(bands.items()):
            row(f"    Band {band}", pct_str(acc, n))

    # confluence with daily SSL
    h2("3c. Confluence with daily SSL")
    agree_sigs   = [s for s in signals
                    if (s["signal_direction"] == "DOWN" and s.get("daily_ssl") == "BEAR")
                    or (s["signal_direction"] == "UP"   and s.get("daily_ssl") == "BULL")]
    conflict_sigs = [s for s in signals
                     if (s["signal_direction"] == "DOWN" and s.get("daily_ssl") == "BULL")
                     or (s["signal_direction"] == "UP"   and s.get("daily_ssl") == "BEAR")]

    for label, subset in [("Whale agrees with daily SSL", agree_sigs),
                           ("Whale conflicts with daily SSL", conflict_sigs)]:
        acc30, n30   = pct_correct(subset, "30m")
        acc60, n60   = pct_correct(subset, "60m")
        acc120, n120 = pct_correct(subset, "120m")
        lines.append(f"\n  [{label}]  (n={len(subset)})")
        row(f"    Correct at 30m",  pct_str(acc30, n30))
        row(f"    Correct at 60m",  pct_str(acc60, n60))
        row(f"    Correct at 120m", pct_str(acc120, n120))

    # pre-checks passed
    h2("3d. Pre-checks all passed (Claude was called)")
    claude_sigs    = [s for s in signals if s["claude_called"]]
    no_claude_sigs = [s for s in signals if not s["claude_called"]]

    for label, subset in [("Claude was called", claude_sigs),
                           ("Pre-checks blocked", no_claude_sigs)]:
        acc30, n30   = pct_correct(subset, "30m")
        acc60, n60   = pct_correct(subset, "60m")
        acc120, n120 = pct_correct(subset, "120m")
        lines.append(f"\n  [{label}]  (n={len(subset)})")
        row(f"    Correct at 30m",  pct_str(acc30, n30))
        row(f"    Correct at 60m",  pct_str(acc60, n60))
        row(f"    Correct at 120m", pct_str(acc120, n120))

    # oversold problem
    h2("3e. Oversold filter: daily RSI < 35 vs >= 35 (DOWN signals only)")
    oversold_down   = [s for s in down_sigs if s.get("daily_rsi") is not None and s["daily_rsi"] < 35]
    normal_rsi_down = [s for s in down_sigs if s.get("daily_rsi") is not None and s["daily_rsi"] >= 35]

    for label, subset in [("DOWN signal + daily RSI < 35 (oversold)", oversold_down),
                           ("DOWN signal + daily RSI >= 35 (not oversold)", normal_rsi_down)]:
        acc30, n30   = pct_correct(subset, "30m")
        acc60, n60   = pct_correct(subset, "60m")
        acc120, n120 = pct_correct(subset, "120m")
        lines.append(f"\n  [{label}]  (n={len(subset)})")
        row(f"    Correct at 30m",  pct_str(acc30, n30))
        row(f"    Correct at 60m",  pct_str(acc60, n60))
        row(f"    Correct at 120m", pct_str(acc120, n120))

    # average move size
    h2("3f. Average move size (reward vs risk)")
    for tp in ["30m", "60m", "120m"]:
        avg_win, nw  = avg_move(signals, correct_only=True,  timepoint=tp)
        avg_loss, nl = avg_move(signals, correct_only=False, timepoint=tp)
        rr = round(avg_win / avg_loss, 2) if avg_win and avg_loss else None
        lines.append(f"\n  At {tp}:")
        row("    Avg correct move (abs)",  f"{gbp(avg_win)} (n={nw})")
        row("    Avg adverse move (abs)",  f"{gbp(avg_loss)} (n={nl})")
        row("    Reward/Risk ratio",        str(rr) if rr else "N/A")

    # ────────────────────────────────────────────
    h1("PART 4 — HONEST VERDICT")

    n = len(signals)
    # count how many have at least 120m follow-through data
    with_data = [s for s in signals if s.get("correct_120m") is not None]
    acc_all_120, _ = pct_correct(signals, "120m")

    lines.append("")
    lines.append("  1. SAMPLE SIZE")
    lines.append(f"     {n} high-conviction signals found over {duration_days:.1f} days.")
    lines.append(f"     {len(with_data)} of these have full 120-minute follow-through data.")
    if n < 30:
        lines.append("     *** VERDICT: Sample is SMALL. Patterns are indicative only — ")
        lines.append("         do NOT draw firm conclusions until n >= 30 with follow-through.")
    elif n < 100:
        lines.append("     VERDICT: Sample is MODERATE. Early trends are visible but")
        lines.append("     confidence intervals are wide. Treat as directional signal only.")
    else:
        lines.append("     VERDICT: Sample approaching MEANINGFUL. Statistical tests possible.")

    lines.append("")
    lines.append("  2. EARLY SIGNAL ON WHALE ACCURACY")
    acc30, n30   = pct_correct(signals, "30m")
    acc60, n60   = pct_correct(signals, "60m")
    acc120, n120 = pct_correct(signals, "120m")
    if acc120 is not None:
        if acc120 > 60:
            lines.append(f"     At 120 minutes: {acc120}% accuracy (n={n120}).")
            lines.append("     This is ABOVE the 50% random baseline — early positive signal.")
        elif acc120 > 45:
            lines.append(f"     At 120 minutes: {acc120}% accuracy (n={n120}).")
            lines.append("     Near the 50% baseline. No edge detected yet.")
        else:
            lines.append(f"     At 120 minutes: {acc120}% accuracy (n={n120}).")
            lines.append("     BELOW 50% baseline — signals may be anti-correlated at this horizon.")
    else:
        lines.append("     Insufficient follow-through data for 120m accuracy.")

    lines.append("")
    lines.append("  3. CONDITIONS AFFECTING RELIABILITY")
    if len(agree_sigs) > 0 and len(conflict_sigs) > 0:
        agree_acc, _   = pct_correct(agree_sigs, "60m")
        conflict_acc, _ = pct_correct(conflict_sigs, "60m")
        if agree_acc and conflict_acc:
            diff = agree_acc - conflict_acc
            lines.append(f"     Daily SSL confluence: {agree_acc}% vs conflicts: {conflict_acc}%")
            if diff > 10:
                lines.append("     STRONG confluence effect — daily SSL agreement significantly improves accuracy.")
            elif diff > 0:
                lines.append("     WEAK confluence effect — daily SSL alignment modestly helps.")
            else:
                lines.append("     No meaningful confluence effect detected yet.")
    if len(oversold_down) > 0:
        ov_acc, _ = pct_correct(oversold_down, "60m")
        nrm_acc, _ = pct_correct(normal_rsi_down, "60m")
        if ov_acc is not None and nrm_acc is not None:
            lines.append(f"     Oversold filter (RSI<35): DOWN signals = {ov_acc}% vs normal RSI = {nrm_acc}%")
            if ov_acc < nrm_acc:
                lines.append("     Oversold conditions DO reduce DOWN signal reliability (as expected).")
            else:
                lines.append("     Oversold conditions do NOT reduce reliability in current data.")

    lines.append("")
    lines.append("  4. MINIMUM CONVICTION THRESHOLD")
    bands_60 = accuracy_by_band(signals, "60m")
    best_band = max(bands_60.items(), key=lambda x: (x[1][0] or 0, x[1][1]))
    if best_band[1][0]:
        lines.append(f"     Best accuracy band at 60m: {best_band[0]} -> {pct_str(*best_band[1])}")
        if best_band[0] == "90-100":
            lines.append("     Only 90-100 scores show meaningful edge — consider 90+ as minimum threshold.")
        elif best_band[0] == "80-89":
            lines.append("     80-89 band shows best accuracy — 80 looks like a reasonable floor.")
        else:
            lines.append("     70-79 band leading — but this band is noisy; wait for more data.")
    else:
        lines.append("     Insufficient data to identify best threshold band.")

    lines.append("")
    lines.append("  5. DATA NEEDED FOR STATISTICAL SIGNIFICANCE")
    needed_signals = max(0, 30 - n)
    # at ~5 signals/day rough estimate
    hunt_rate = n / max(duration_days, 0.1)
    days_needed = needed_signals / hunt_rate if hunt_rate > 0 else 999
    lines.append(f"     Current: {n} signals over {duration_days:.1f} days (~{hunt_rate:.1f}/day)")
    lines.append(f"     Minimum for basic conclusions: 30 signals with follow-through")
    lines.append(f"     Minimum for meaningful statistics: 100 signals")
    if n >= 100:
        lines.append("     You are approaching meaningful statistics. Continue accumulating.")
    elif n >= 30:
        lines.append(f"     At current rate, ~{max(0,(100-n)/hunt_rate):.0f} more days to reach 100 signals.")
    else:
        lines.append(f"     At current rate, ~{days_needed:.0f} more days to reach 30 signals.")
        lines.append(f"     At current rate, ~{max(0,(100-n)/hunt_rate):.0f} more days to reach 100 signals.")

    lines.append("")
    lines.append("  6. RECOMMENDATION")
    if n < 20:
        lines.append("     DO NOT design whale-based trading rules yet.")
        lines.append("     Sample is too small. Keep accumulating data.")
        lines.append("     Action: run the system for at least 2-3 more weeks,")
        lines.append("     then re-run this forensic analysis.")
    elif n < 50:
        lines.append("     EARLY STAGE — tentative patterns visible but not trustworthy.")
        lines.append("     You can begin forming hypotheses about what conditions help,")
        lines.append("     but do NOT bake these into hard trading rules yet.")
        lines.append("     Action: accumulate more data; re-run in 1-2 weeks.")
    else:
        if acc60 and acc60 > 58:
            lines.append("     PROCEED CAUTIOUSLY — sample size is growing and accuracy exceeds")
            lines.append("     the random baseline. Consider designing paper-trade rules around")
            lines.append("     the strongest confluence conditions (high score + daily SSL agree).")
            lines.append("     Continue monitoring and running weekly forensics.")
        else:
            lines.append("     WAIT — sample size is OK but accuracy is near baseline.")
            lines.append("     Whale signals are not showing a reliable edge yet.")
            lines.append("     Continue accumulating data before designing rules.")

    # ────────────────────────────────────────────
    h1("END OF REPORT")
    lines.append(f"  Full signal table saved to: {OUT_CSV}")
    lines.append(f"  Report saved to            : {OUT_TXT}")
    lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# TERMINAL SUMMARY
# ──────────────────────────────────────────────
def print_summary(signals, ticks, new_csv, existing_csv):
    n = len(signals)
    down_n = sum(1 for s in signals if s["signal_direction"] == "DOWN")
    up_n   = sum(1 for s in signals if s["signal_direction"] == "UP")
    both_n = sum(1 for s in signals if s["signal_direction"] == "BOTH")

    acc30, n30   = pct_correct(signals, "30m")
    acc60, n60   = pct_correct(signals, "60m")
    acc120, n120 = pct_correct(signals, "120m")

    duration = (ticks[-1]["timestamp"] - ticks[0]["timestamp"]).total_seconds() / 86400

    print()
    print("=" * 60)
    print("  WHALE SIGNAL FORENSICS — COMPLETE")
    print("=" * 60)
    print(f"  Log period    : {ticks[0]['timestamp'].date()} to {ticks[-1]['timestamp'].date()}")
    print(f"  Duration      : {duration:.1f} days")
    print(f"  Total ticks   : {len(ticks)}")
    print()
    print(f"  HIGH-CONVICTION SIGNALS (70+):")
    print(f"    Total        : {n}")
    print(f"    DOWN signals : {down_n}")
    print(f"    UP signals   : {up_n}")
    print(f"    BOTH 70+     : {both_n}")
    print()
    print(f"  ACCURACY (direction correct?):")
    print(f"    At 30m  : {pct_str(acc30, n30)}")
    print(f"    At 60m  : {pct_str(acc60, n60)}")
    print(f"    At 120m : {pct_str(acc120, n120)}")
    print()
    print(f"  CSV: {new_csv} new rows added ({existing_csv} already existed)")
    print(f"  Full report : {OUT_TXT}")
    print(f"  CSV data    : {OUT_CSV}")
    print("=" * 60)
    print()


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    print(f"Reading log: {LOG_PATH}")
    ticks, price_series = parse_log(LOG_PATH)
    print(f"  Parsed {len(ticks)} candle ticks | {len(price_series)} price points")

    # filter high-conviction signals (de-duplicate consecutive same-direction runs)
    raw_signals = [t for t in ticks if is_high_conviction(t)]
    for sig in raw_signals:
        sig["signal_direction"] = signal_direction(sig)

    print(f"  High-conviction signals (raw): {len(raw_signals)}")

    # cross-reference future prices
    signals = enrich_with_future_prices(raw_signals, price_series)

    # build and save report
    report = build_report(signals, ticks)
    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        fh.write(report)

    # save/merge CSV
    new_csv, existing_csv = save_csv(signals, OUT_CSV)

    # terminal summary
    print_summary(signals, ticks, new_csv, existing_csv)

    # also print the full report to terminal
    print(report)


if __name__ == "__main__":
    main()
