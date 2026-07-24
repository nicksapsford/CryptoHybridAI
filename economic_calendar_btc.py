"""
CryptoHybrid AI -- economic_calendar_btc.py
Fetches high-impact economic events from Finnhub and formats them as context
for Claude. Enforces exactly one automated rule: a 15-minute trading pause
immediately after a high-impact event fires (spreads are chaotic). Everything
else is passed to Claude as intelligence, not as a block.

Timezone note: Finnhub economic calendar times are in UTC.
The fallback schedule also uses UTC (13:30 UTC = 08:30 AM ET for most releases).
If events appear off by 4-5 hours, change _parse_finnhub_time() to use ET.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)

log = logging.getLogger("CryptoHybrid.EconCal")

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

UTC = timezone.utc
ET  = ZoneInfo("America/New_York")

_FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
_FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"

POST_BLOCK_MINS  = 15    # hard block window after event fires
UPCOMING_HOURS   = 4     # look-ahead window shown to Claude
RECENT_HOURS     = 2     # look-back window shown to Claude
CACHE_TTL_SECS   = 3600  # refresh from Finnhub at most once per hour

# Expected BTC average absolute price move per event type (rough historical %)
_BTC_MOVES = {
    "FOMC_RATE":     3.5,
    "CPI":           2.5,
    "NFP":           1.5,
    "FOMC_MINUTES":  2.0,
    "GDP":           1.2,
    "PPI":           1.5,
    "JACKSON_HOLE":  3.0,
    "BTC_ETF":       8.0,
    "OTHER_HIGH":    1.5,
}

# ──────────────────────────────────────────────────────────────────────────────
# FOMC hardcoded dates (decision day only; minutes are ~3 Wed after each)
# Used both as a direct fallback and to cross-check Finnhub results.
# Announcement time: 19:00 UTC (2:00 PM ET)
# ──────────────────────────────────────────────────────────────────────────────

_FOMC_RATE_DATES = {
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027 (approximate)
    "2027-02-03", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
}

_FOMC_MINUTES_DATES = {
    # 2025
    "2025-02-19", "2025-04-09", "2025-05-28", "2025-07-09",
    "2025-08-20", "2025-10-08", "2025-11-19",
    # 2026
    "2026-02-18", "2026-04-08", "2026-05-27", "2026-07-08",
    "2026-08-19", "2026-10-07", "2026-11-18",
}

# ──────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ──────────────────────────────────────────────────────────────────────────────

_cache: dict = {"events": [], "fetched_at": None}
_finnhub_restricted: bool = False   # set True on first 403; stops pointless hourly retries


# ──────────────────────────────────────────────────────────────────────────────
# Event helpers
# ──────────────────────────────────────────────────────────────────────────────

def _classify(name: str) -> tuple:
    """Return (event_type, expected_btc_move_pct) from an event name."""
    n = name.lower()
    if ("fomc" in n or "federal open market" in n) and "minute" in n:
        return "FOMC_MINUTES", _BTC_MOVES["FOMC_MINUTES"]
    if ("fomc" in n or "federal fund" in n or "interest rate decision" in n
            or "fed rate" in n):
        return "FOMC_RATE", _BTC_MOVES["FOMC_RATE"]
    if "cpi" in n or "consumer price" in n:
        return "CPI", _BTC_MOVES["CPI"]
    if "nonfarm" in n or "non-farm" in n or "payroll" in n:
        return "NFP", _BTC_MOVES["NFP"]
    if "gdp" in n or "gross domestic" in n:
        return "GDP", _BTC_MOVES["GDP"]
    if "ppi" in n or "producer price" in n:
        return "PPI", _BTC_MOVES["PPI"]
    if "jackson hole" in n or "economic policy symposium" in n:
        return "JACKSON_HOLE", _BTC_MOVES["JACKSON_HOLE"]
    if "bitcoin etf" in n or "btc etf" in n or "crypto etf" in n or "spot etf" in n:
        return "BTC_ETF", _BTC_MOVES["BTC_ETF"]
    return "OTHER_HIGH", _BTC_MOVES["OTHER_HIGH"]


def _make_event(name: str, event_time_utc: datetime) -> dict:
    etype, move = _classify(name)
    return {
        "name":           name,
        "type":           etype,
        "event_time_utc": event_time_utc.replace(tzinfo=UTC) if event_time_utc.tzinfo is None
                          else event_time_utc.astimezone(UTC),
        "btc_move_pct":   move,
    }


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _utc(d: date, hour: int, minute: int) -> datetime:
    """Build a UTC-aware datetime for a given date and time."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


# ──────────────────────────────────────────────────────────────────────────────
# Fallback schedule (when Finnhub is unavailable)
# All times in UTC: US economic data mostly releases at 13:30 UTC (08:30 ET)
# ──────────────────────────────────────────────────────────────────────────────

def _fallback_events(from_dt: datetime, to_dt: datetime) -> list:
    events = []
    d = from_dt.date()
    while d <= to_dt.date():
        ds   = d.strftime("%Y-%m-%d")
        year = d.year
        month = d.month

        # FOMC rate decision
        if ds in _FOMC_RATE_DATES:
            events.append(_make_event("FOMC Interest Rate Decision", _utc(d, 19, 0)))

        # FOMC minutes
        if ds in _FOMC_MINUTES_DATES:
            events.append(_make_event("FOMC Meeting Minutes", _utc(d, 18, 0)))

        # NFP: first Friday of each month at 13:30 UTC
        if d == _first_friday(year, month):
            events.append(_make_event("Non-Farm Payrolls (NFP)", _utc(d, 13, 30)))

        # CPI: typically released on Tuesday or Wednesday, between the 10th-18th
        if d.weekday() in (1, 2) and 10 <= d.day <= 18:
            events.append(_make_event("CPI Release (est. date)", _utc(d, 13, 30)))

        # GDP: roughly last week of Jan, Apr, Jul, Oct on a Wednesday
        if month in (1, 4, 7, 10) and d.weekday() == 2 and d.day >= 23:
            events.append(_make_event("GDP Growth Rate (est. date)", _utc(d, 13, 30)))

        # PPI: typically Thursday, 11th-19th (1-2 weeks after CPI)
        if d.weekday() == 3 and 11 <= d.day <= 19:
            events.append(_make_event("PPI Release (est. date)", _utc(d, 13, 30)))

        d += timedelta(days=1)

    return events


# ──────────────────────────────────────────────────────────────────────────────
# Finnhub fetch
# ──────────────────────────────────────────────────────────────────────────────

def _parse_finnhub_time(time_str: str) -> datetime:
    """
    Parse a Finnhub time string ("YYYY-MM-DD HH:MM:SS") as UTC.
    Finnhub economic calendar times are documented as UTC.
    """
    return datetime.strptime(time_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def _fetch_finnhub(from_dt: datetime, to_dt: datetime) -> list:
    """Fetch high-impact US events from Finnhub. Returns [] on any failure."""
    global _finnhub_restricted
    if not _FINNHUB_KEY or _finnhub_restricted:
        return []
    try:
        resp = requests.get(
            _FINNHUB_URL,
            params={
                "from":  from_dt.date().strftime("%Y-%m-%d"),
                "to":    to_dt.date().strftime("%Y-%m-%d"),
                "token": _FINNHUB_KEY,
            },
            timeout=8,
        )
        if resp.status_code == 403:
            _finnhub_restricted = True
            log.info(
                "Finnhub economic calendar: 403 -- endpoint requires a paid plan "
                "(free tier does not include /calendar/economic). "
                "Switching to hardcoded fallback schedule for this session. "
                "No further Finnhub retries will be made."
            )
            return []
        if resp.status_code != 200:
            log.warning("Finnhub HTTP %d -- falling back to schedule", resp.status_code)
            return []

        events = []
        for item in resp.json().get("economicCalendar", []):
            if item.get("country") != "US":
                continue
            impact = str(item.get("impact", "")).lower()
            if impact not in ("high", "3"):
                continue
            time_str = item.get("time", "")
            if not time_str or len(time_str) < 16:
                continue
            try:
                event_time = _parse_finnhub_time(time_str)
            except ValueError:
                continue
            events.append(_make_event(item.get("event", "Unknown US Event"), event_time))

        log.info("Finnhub calendar: %d high-impact US events fetched", len(events))
        return events

    except Exception as exc:
        log.warning("Finnhub fetch failed (%s) -- using fallback schedule", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Cached event retrieval
# ──────────────────────────────────────────────────────────────────────────────

def _get_events() -> list:
    """
    Return the current event list. Refreshes at most once per hour.
    Scans a 3-day window (2 days back to 2 days forward) so that recent
    events are always visible for Claude's context.
    """
    global _cache
    now = datetime.now(UTC)

    if (_cache["fetched_at"] is not None
            and (now - _cache["fetched_at"]).total_seconds() < CACHE_TTL_SECS):
        return _cache["events"]

    from_dt = now - timedelta(days=2)
    to_dt   = now + timedelta(days=2)

    events = _fetch_finnhub(from_dt, to_dt)

    if not events:
        events = _fallback_events(from_dt, to_dt)
        log.info("Economic calendar using hardcoded fallback schedule (%d events)", len(events))
    else:
        # Always merge hardcoded FOMC dates -- Finnhub sometimes omits them
        known_fomc_days = {e["event_time_utc"].date() for e in events
                           if e["type"] == "FOMC_RATE"}
        for e in _fallback_events(from_dt, to_dt):
            if e["type"] in ("FOMC_RATE", "FOMC_MINUTES"):
                if e["event_time_utc"].date() not in known_fomc_days:
                    events.append(e)
                    log.debug("Added hardcoded FOMC: %s", e["name"])

    _cache = {"events": events, "fetched_at": now}
    return events


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def is_hard_blocked() -> tuple:
    """
    The ONLY automated block this module enforces.
    Returns (is_blocked, reason_str, event_name, remain_mins).
    When not blocked: (False, "", "", 0).
    """
    now    = datetime.now(UTC)
    events = _get_events()

    for e in events:
        t         = e["event_time_utc"]
        block_end = t + timedelta(minutes=POST_BLOCK_MINS)
        if t <= now <= block_end:
            fired_mins  = int((now - t).total_seconds() / 60)
            remain_mins = POST_BLOCK_MINS - fired_mins
            reason = (
                f"{e['name']} fired {fired_mins} min ago"
                f" -- {remain_mins} min remaining in volatility window"
            )
            return True, reason, e["name"], remain_mins

    return False, "", "", 0


def get_event_context() -> str:
    """
    Return a formatted economic event summary for Claude.
    Covers upcoming events (next UPCOMING_HOURS) and recent events (past RECENT_HOURS).
    """
    now    = datetime.now(UTC)
    events = _get_events()

    upcoming: list = []
    recent:   list = []

    for e in events:
        t         = e["event_time_utc"]
        delta_sec = (t - now).total_seconds()

        if 0 < delta_sec <= UPCOMING_HOURS * 3600:
            upcoming.append((delta_sec, e))
        elif -RECENT_HOURS * 3600 <= delta_sec <= 0:
            recent.append((abs(delta_sec), e))

    upcoming.sort(key=lambda x: x[0])
    recent.sort(key=lambda x: x[0])

    blocked, block_reason, _bname, _bmins = is_hard_blocked()
    lines = ["ECONOMIC EVENTS"]

    if blocked:
        lines.append(f"  *** POST-EVENT HARD BLOCK: {block_reason} ***")
    else:
        lines.append("  Post-event block: CLEAR")

    if upcoming:
        lines.append(f"  Upcoming (next {UPCOMING_HOURS}h):")
        for delta, e in upcoming:
            h, m    = divmod(int(delta / 60), 60)
            t_str   = e["event_time_utc"].strftime("%H:%M UTC")
            eta     = f"in {h}h {m}m" if h > 0 else f"in {m} min"
            lines.append(
                f"    {e['name']:<44} {eta:<12}  ({t_str})"
                f"  | Avg BTC move: ~{e['btc_move_pct']:.1f}%"
            )
    else:
        lines.append(f"  No high-impact events in next {UPCOMING_HOURS} hours.")

    if recent:
        lines.append(f"  Recent (last {RECENT_HOURS}h):")
        for delta, e in recent:
            h, m    = divmod(int(delta / 60), 60)
            t_str   = e["event_time_utc"].strftime("%H:%M UTC")
            ago     = f"{h}h {m}m ago" if h > 0 else f"{m} min ago"
            lines.append(
                f"    {e['name']:<44} {ago:<12}  ({t_str})"
                f"  | Avg BTC move: ~{e['btc_move_pct']:.1f}%"
            )
    else:
        lines.append(f"  No high-impact events in last {RECENT_HOURS} hours.")

    # Status line for Claude's guidance
    lines.append("")
    if blocked:
        status = "POST-EVENT VOLATILITY BLOCK (15-min window)"
    elif upcoming and upcoming[0][0] < 1800:
        status = "IMMINENT HIGH-IMPACT EVENT (within 30 minutes) -- strongly consider STAY_OUT"
    elif upcoming and upcoming[0][0] < 7200:
        status = "HIGH-IMPACT EVENT WITHIN 2 HOURS -- elevated caution required"
    elif recent and recent[0][0] < 3600:
        status = "POST-EVENT (within 1 hour) -- market may be repricing, watch for directional move"
    else:
        status = "CLEAR -- no high-impact events in immediate window"

    lines.append(f"  Calendar status: {status}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print("\n=== CryptoHybrid AI -- Economic Calendar Self-Test ===")
    print(f"Finnhub key configured: {'YES' if _FINNHUB_KEY else 'NO (using fallback)'}")
    print()
    blocked, reason, event_name, remain_mins = is_hard_blocked()
    if blocked:
        print(f"HARD BLOCK ACTIVE: {reason}")
    else:
        print("Hard block: CLEAR")
    print()
    print(get_event_context())
