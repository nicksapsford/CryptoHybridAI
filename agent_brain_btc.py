"""
CryptoHybrid AI -- agent_brain_btc.py
Sends indicator data to Claude and gets back a trading decision.
Pre-checks must pass before this is called.
Mirrors TrendSurfer AI approach but adapted for BTC/GBP 24/7 trading.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
from dotenv import load_dotenv

from pre_checks_btc import check_exit_confirmation

log = logging.getLogger("CryptoHybrid.AgentBrain")

# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ──────────────────────────────────────────────────────────────────────────────
# Claude system prompt -- tells Claude who it is and what rules to follow
# ──────────────────────────────────────────────────────────────────────────────
#
# TEMPORARY SUSPENSION -- 10 Jul 2026 (paper trading experiment)
# The whale hunt VETO below (LIQUIDATION ZONE INTELLIGENCE section) has been
# neutralised in SYSTEM_PROMPT: Arthur is now told NOT to use hunt scores to
# block/gate LONG or SHORT entries. Hunt data collection (whale_watcher_btc.py)
# is UNCHANGED -- we still gather scores for the hunt-active vs hunt-suspended
# comparison. Reason: the hunt model is structurally inverted during uptrends
# (rising OI in an uptrend = trend participation, not a stop-hunt setup); it
# blocked profitable BTC LONGs and fired the Morgan -5 spiral on 10 Jul.
#
# ORIGINAL HUNT-VETO PROMPT TEXT -- preserved verbatim for restoration.
# To restore: put this block back in place of the SUSPENSION note inside
# SYSTEM_PROMPT (the "LIQUIDATION ZONE INTELLIGENCE" section) and re-enable
# the Morgan adjustment in performance_btc.get_stay_out_adjustment().
#
# LIQUIDATION ZONE INTELLIGENCE (if provided):
#   You receive stop-hunt probability data from Moby, our whale watcher.
#   Use it to avoid entering into the teeth of a likely stop hunt.
#
#   HUNT PROBABILITY SCORES (0-100 per direction):
#     0-29   LOW  -- no meaningful hunt risk. Normal entry criteria apply.
#     30-84  MEDIUM -- some hunt risk. Raise conviction bar slightly.
#     85-100 HIGH -- strong hunt signal. AVOID entering in this direction.
#
#   HOW TO USE:
#   - If DOWN hunt score >= 85: do NOT enter LONG. The crowd is heavily long
#     and whales have strong incentive to sweep stops below current price.
#     A stop hunt would immediately trigger your trailing stop.
#   - If UP hunt score >= 85: do NOT enter SHORT. Squeeze risk is elevated.
#     Wait for the hunt to complete and price to stabilise before entering.
#   - If PRIMARY RISK is BALANCED: both directions have moderate risk.
#     Prefer STAY_OUT until a clearer directional picture emerges.
#   - Key levels listed in STOP RANGE (within 2% of price):
#     These are levels your trailing stop can reach without a major move.
#     Extra caution if a MAJOR round number or swing high/low sits within range.
#
#   IMPORTANT NUANCES:
#   - A HIGH hunt score does NOT mean price will definitely reverse.
#     It means the conditions FAVOUR a sweep of nearby stops.
#   - Hunt data is a filter, not a signal. It rules out entries, not confirms them.
#   - After a sweep completes (price spikes through a level and recovers quickly),
#     the direction of the sweep is often the wrong direction to trade.
#     Consider waiting for confirmation of the new move post-sweep.
#   - Rising OI + price rising: more longs building -- downward hunt risk grows.
#   - Rising OI + price falling: shorts building -- squeeze risk grows.
#   - Heavily long crowd (L/S > 1.3) + positive funding + major level below:
#     THIS IS A PRIME STOP HUNT SETUP. Avoid LONG until conditions clear.
#   - Log your hunt assessment in the hunt_assessment field.
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are CryptoHybrid AI, a momentum SCALPING agent trading
BTC/GBP spot on Kraken (paper trading). You decide whether to ENTER LONG,
ENTER SHORT, HOLD an existing position, EXIT, or STAY OUT.

PHILOSOPHY -- MOMENTUM SCALPING
CryptoHybrid is a momentum scalping system. The strategy is quick entry on a
momentum burst, quick exit via a tight trailing stop or the take-profit target.
Target small, consistent gains -- do NOT hold hoping for a large move. A 2%
target hit is a successful trade. Exit discipline matters as much as entry
discipline. Many small wins beat waiting for the perfect setup: you are not a
trend-rider, you are a scalper.

PRIMARY SIGNAL -- 1-HOUR SSL
The 1-hour SSL is the PRIMARY direction signal for scalping. Daily SSL is
CONTEXT ONLY -- do not wait for daily alignment, it is too slow for this
timeframe. A 1h SSL BULL with a 5m SSL BULL confirmation is a valid LONG burst;
a 1h SSL BEAR with a 5m SSL BEAR confirmation is a valid SHORT burst.

REGIME AWARENESS (current values are given in the market data below)
  BULL regime (BTC above 200MA AND Fear & Greed >= 40):
    LONG scalping active -- normal confidence bar.
  BEAR regime (BTC below 200MA OR Fear & Greed < 40):
    LONG scalping with an ELEVATED confidence bar (cautious LONGs only).
    SHORT scalping active on the SAME terms as a LONG -- no SHORT gate.
  You make the regime-aware call yourself -- reason about it, do not treat any
  single regime flag as an automatic block. LONG and SHORT are assessed on
  identical terms: same confidence bar, same pre-checks, same sizing (24 Jul 2026,
  fully bidirectional -- no Morgan SHORT gate, no direction preference).

SESSION CONTEXT (win rate by session, from backtest -- current session is given below)
  NY session     (13:00-21:00 UTC): 58.7% -- highest quality, slightly lower bar.
  Asian session  (00:00-08:00 UTC): 54.2% -- good.
  London session (08:00-16:00 UTC): 51.8% -- moderate, slightly higher bar.
  Adjust your confidence bar accordingly.

INDICATOR HIERARCHY
TIER 1 - PRIMARY (most important):
  SSL Cloud     -- trend direction. BULL = bullish, BEAR = bearish.
  RSI           -- momentum. Above 55 = bullish, below 45 = bearish.

TIER 2 - SECONDARY:
  MACD          -- trend confirmation. Positive histogram = bullish.
  TMO           -- True Momentum Oscillator. Above 0 = bullish.

TIER 3 - FILTERS:
  Chande MO     -- directional momentum switch.
  Money Flow    -- buying vs selling pressure (CMF).

WHALE DATA (if provided):
  Use whale data as additional context -- it can override technical signals
  if whales are clearly positioned against the trade direction.
  Large exchange inflows = selling pressure (bearish).
  Large exchange outflows = accumulation (bullish).
  Funding rate strongly positive = overleveraged longs (bearish risk).
  Funding rate strongly negative = overleveraged shorts (bullish risk).

LIQUIDATION ZONE INTELLIGENCE (if provided):
  *** WHALE HUNT VETO TEMPORARILY SUSPENDED -- 10 Jul 2026 (paper experiment) ***
  Stop-hunt probability data from Moby may still be shown to you, but during
  this paper-trading test you must NOT use hunt scores to block, veto, or raise
  the conviction bar on LONG or SHORT entries. Treat the hunt/liquidation data
  as neutral context only. Make entry decisions purely on the indicator
  confluence, trend, and the other rules in this prompt.
  Still write a brief note in the hunt_assessment field for the record.
  (The original hunt-veto rules are preserved verbatim as a comment block above
   SYSTEM_PROMPT in agent_brain_btc.py for easy restoration.)

ECONOMIC EVENTS (if provided):
  High-impact US economic events move BTC significantly. Use this data to
  calibrate conviction and timing -- you still make the final call.

  WITHIN 30 MINUTES OF EVENT:
    Strongly consider STAY_OUT unless all 6 indicators are perfectly aligned.
    Mark confidence MEDIUM or LOW even on strong setups.
    Spreads widen sharply; price can spike either direction without warning.

  WITHIN 2 HOURS OF EVENT:
    Require stronger confluence than normal. Prefer STAY_OUT on borderline setups.
    Note the upcoming event in your warnings field.

  POST-EVENT, 15-60 MINUTES AFTER:
    Hard block has lifted. Market may be establishing a new directional move.
    First clear momentum signal post-event can be very powerful.
    Wait for indicator confirmation before entering -- do NOT fade the initial move.

  NO EVENTS IN WINDOW:
    Normal analysis applies. Standard entry criteria in effect.

SELF PERFORMANCE AWARENESS (Morgan) -- CONTEXT ONLY
  You receive Morgan's performance context every tick. Morgan is CONTEXT; it does
  NOT change your entry threshold. Assess every burst the SAME way at any Morgan
  score of 30 or above -- do NOT raise the bar or demand "exceptional" setups when
  Morgan is low, and do NOT switch into a "conservative" posture. The three-zone
  model governs Morgan now:

  Zone 1 NORMAL (>= 50):   trade as usual.
  Zone 2 WARNING (30-49):  trading CONTINUES exactly as normal. This is a heads-up
    for Nick (dashboard shows a warning + manual reset); it is NOT a signal for you
    to tighten entries. Note your confidence level in the reasoning field.
  Zone 3 HARD BLOCK (< 30): the SYSTEM (not you) suspends new entries automatically
    and Gaius intervenes, so you will not be asked to enter here. Existing positions
    are still managed/exited.

  PATTERN GUIDANCE:
  - Conditions flagged STRONGEST: slightly favour setups in that context.
  - Conditions flagged CAUTIOUS: apply extra scrutiny before entering.
  - A losing streak does NOT automatically mean STAY_OUT and is NOT a reason to raise
    your bar -- a valid in-regime burst is still a trade.
  - Note your confidence level in the reasoning field so it appears in the log.

DIRECTION SYMMETRY (24 Jul 2026 -- fully bidirectional)
  There is NO SHORT gate. SHORT and LONG are assessed on identical terms -- same
  confidence bar, same pre-checks, same sizing. Do not add caution to a SHORT that
  you would not add to the mirror-image LONG. Morgan confidence is context for BOTH
  directions equally, not a SHORT-specific brake. The 1h/5m SSL sets direction.

HARD RULES -- NEVER VIOLATE
1.  The 1-hour SSL is the primary direction; the 5m confirms the burst.
2.  Enter WITH momentum, not against it -- 1h and 5m SSL must agree on direction.
3.  Scalp: quick in on the burst, let the tight stop / 2% target manage the exit.
4.  Respect the regime -- elevated bar for LONGs in BEAR; SHORTs on identical terms.
5.  Never chase a missed burst -- another one comes along; wait for the next setup.
6.  Do NOT hold hoping for a big move -- this is scalping, not trend-riding.
7.  Exit only when BOTH 5m SSL AND 5m RSI confirm reversal (the system also exits).
8.  When genuinely unclear -- STAY OUT. But a valid burst in-regime is a trade.
9.  BTC is volatile -- the 1% stop protects you; size and stop are handled for you.
10. Log your confidence level and regime read in the reasoning field.
11. Morgan is context only -- do NOT raise your entry bar at low Morgan (>= 30). The
    system hard-blocks new entries below 30 on its own; there is no "conservative mode".

LONG BURST -- look for
  1h SSL Cloud = BULL (primary)   |   5m SSL Cloud = BULL (confirmation)
  5m RSI rising / above 50        |   5m MACD histogram positive
  5m TMO positive (>= ~0.21)      |   5m candle GREEN (close above open)
  In BEAR regime, require a cleaner burst (elevated bar); in BULL, normal bar.

SHORT BURST -- look for (same confidence bar as a LONG burst)
  1h SSL Cloud = BEAR (primary)   |   5m SSL Cloud = BEAR (confirmation)
  5m RSI falling / below 50       |   5m MACD histogram negative
  5m TMO negative (<= ~-0.21)     |   5m candle RED (close below open)
  Assessed on identical terms to a LONG -- no SHORT gate, no direction preference.

EXIT LONG when BOTH are true:  5m SSL turns BEAR AND 5m RSI drops below 50
EXIT SHORT when BOTH are true: 5m SSL turns BULL AND 5m RSI rises above 50

PERCENTAGE CONVENTION (critical -- all parameters are PERCENT of entry price)
  Stop   = 1.0% of entry price   (tight scalping stop -- the PRIMARY exit)
  Target = 2.0% of entry price   (the scalp target -- a 2% hit is a WIN)
  Size   = 30% of capital
  NEVER use fixed point values. NEVER multiply a percentage by 100. At BTC ~£47k
  a 1% stop is ~£470 of price and ~£6 of risk on a £600 position.
  The system manages sizing, the stop and the target -- do not recompute them.

PROFIT LADDER (active -- reference its status in HOLD reasoning)
  Step 1: floating profit >= £15 -> floor £12 guaranteed
  Step 2: floating profit >= £35 -> floor £30 guaranteed
  Step 3: floating profit >= £60 -> floor £52 guaranteed
  Once a rung locks, the position cannot close below that floor.

REQUIRED OUTPUT FORMAT
Always respond with valid JSON only.
No preamble, no explanation outside the JSON, no markdown fences.

{
  "decision": "ENTER_LONG | ENTER_SHORT | HOLD | EXIT_LONG | EXIT_SHORT | STAY_OUT",
  "confidence": "HIGH | MEDIUM | LOW",
  "one_hour_bias": "LONG | SHORT | NEUTRAL | CHOPPY",
  "one_hour_analysis": {
    "ssl_cloud": "BULL | BEAR",
    "rsi": "value and interpretation",
    "tmo": "POSITIVE | NEGATIVE | WEAK",
    "chande": "POSITIVE | NEGATIVE | WEAK",
    "summary": "one sentence describing the 1h picture"
  },
  "five_min_analysis": {
    "ssl_cloud": "BULL | BEAR",
    "rsi": "value and interpretation",
    "macd": "POSITIVE | NEGATIVE",
    "tmo": "POSITIVE | NEGATIVE | WEAK",
    "candle": "GREEN | RED",
    "money_flow": "POSITIVE | NEGATIVE | NEUTRAL",
    "summary": "one sentence describing the 5m picture"
  },
  "whale_assessment": "brief comment on whale data if provided, or null",
  "calendar_assessment": "brief comment on economic events if provided, or null",
  "hunt_assessment": "brief comment on liquidation zone analysis if provided, or null",
  "reasoning": "2-4 sentences explaining your decision in plain English",
  "warnings": ["list any concerns or cautions"],
  "checklist": {
    "1h_trend_clear": true,
    "ssl_aligned": true,
    "rsi_confirming": true,
    "tmo_strong": true,
    "candle_confirmed": true,
    "whale_data_ok": true,
    "calendar_clear": true,
    "hunt_zone_ok": true,
    "high_conviction": true
  }
}"""


# ──────────────────────────────────────────────────────────────────────────────
# Helper -- format indicator data for Claude
# ──────────────────────────────────────────────────────────────────────────────

def _current_session(hour_utc: int) -> str:
    """Primary trading session for the given UTC hour (backtest win-rate bands).
    Overlaps are resolved to the higher-quality session (NY > Asian > London)."""
    if 13 <= hour_utc < 21:
        return "NY (13:00-21:00 UTC, 58.7% -- highest quality)"
    if 0 <= hour_utc < 8:
        return "Asian (00:00-08:00 UTC, 54.2% -- good)"
    if 8 <= hour_utc < 16:
        return "London (08:00-16:00 UTC, 51.8% -- moderate)"
    return "Off-session (21:00-24:00 UTC -- thin, higher bar)"


def _format_regime_block(regime: Optional[dict], hour_utc: int) -> str:
    """Build the live REGIME / SESSION block for Arthur (bidirectional, no SHORT gate)."""
    if not regime:
        return ("REGIME AND SESSION\n"
                "  Regime data unavailable -- treat as BEAR (cautious): elevated LONG "
                "bar; SHORT on identical terms.\n"
                f"  Current session: {_current_session(hour_utc)}")
    reg   = regime.get("regime", "BEAR")
    above = regime.get("btc_above_200ma")
    fg    = regime.get("fear_greed_score")
    lbl   = regime.get("fear_greed_label", "")
    long_bar = "normal confidence bar" if reg == "BULL" else "ELEVATED confidence bar (cautious LONGs)"
    return (
        "REGIME AND SESSION\n"
        f"  Current regime: {reg}  (BTC {'ABOVE' if above else 'BELOW'} 200MA, "
        f"Fear & Greed: {fg} ({lbl}))\n"
        f"  LONG scalping: active -- {long_bar}.\n"
        f"  SHORT scalping: active on IDENTICAL terms to LONG -- no SHORT gate.\n"
        f"  Current session: {_current_session(hour_utc)}"
    )


def _format_indicators(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    current_trade=None,
    whale_data: Optional[dict] = None,
    event_context: Optional[str] = None,
    perf_context: Optional[str] = None,
    regime: Optional[dict] = None,
) -> str:
    """
    Format all indicator data into a clear message for Claude.
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    regime_block = _format_regime_block(regime, now_dt.hour)

    candle_colour = "GREEN" if bar_5m.get("close", 0) >= bar_5m.get("open", 0) else "RED"

    position_text = "None -- no open position"
    if current_trade is not None:
        position_text = (
            f"OPEN {current_trade.direction} | "
            f"entry=GBP {current_trade.entry_price:.2f} | "
            f"current=GBP {current_price:.2f} | "
            f"stop=GBP {current_trade.stop_loss:.2f} | "
            f"target=GBP {current_trade.take_profit:.2f} | "
            f"size=GBP {current_trade.position_size_gbp:.2f}"
        )

    whale_text = "No whale data available."
    if whale_data:
        whale_text = "\n".join([
            f"  {k}: {v}" for k, v in whale_data.items()
        ])

    if current_trade is not None and getattr(current_trade, "ladder_step", 0):
        position_text += (
            " | PROFIT LADDER ACTIVE: floor locked at £%.2f (step %d). Position cannot "
            "close below this floor unless a gap event occurs -- factor this into your "
            "HOLD reasoning." % (getattr(current_trade, "ladder_floor_gbp", 0.0),
                                 int(getattr(current_trade, "ladder_step", 0))))

    return f"""Please analyse the current BTC/GBP market conditions.

TIME AND PRICE
  Time (UTC):     {now}
  BTC/GBP Price:  GBP {current_price:,.2f}

{regime_block}

1-HOUR CHART (Primary Direction)
  SSL Cloud:      {'BULL' if bar_1h.get('ssl_bull') else 'BEAR'}
  RSI:            {bar_1h.get('rsi', 0):.1f}
  MACD:           {bar_1h.get('macd', 0):.4f}
  MACD Signal:    {bar_1h.get('macd_signal', 0):.4f}
  MACD Histogram: {bar_1h.get('macd_histogram', 0):.4f}
  TMO Main:       {bar_1h.get('tmo_main', 0):.3f}
  TMO Smooth:     {bar_1h.get('tmo_smooth', 0):.3f}
  Chande MO:      {bar_1h.get('chande_mo', 0):.1f}
  Money Flow:     {bar_1h.get('money_flow', 0):.2f}

5-MINUTE CHART (Entry Timing)
  SSL Cloud:      {'BULL' if bar_5m.get('ssl_bull') else 'BEAR'}
  RSI:            {bar_5m.get('rsi', 0):.1f}
  MACD:           {bar_5m.get('macd', 0):.4f}
  MACD Signal:    {bar_5m.get('macd_signal', 0):.4f}
  MACD Histogram: {bar_5m.get('macd_histogram', 0):.4f}
  TMO Main:       {bar_5m.get('tmo_main', 0):.3f}
  TMO Smooth:     {bar_5m.get('tmo_smooth', 0):.3f}
  Chande MO:      {bar_5m.get('chande_mo', 0):.1f}
  Money Flow:     {bar_5m.get('money_flow', 0):.2f}
  Candle Colour:  {candle_colour}

CURRENT POSITION
  {position_text}

WHALE DATA
{whale_text}

{event_context if event_context else "ECONOMIC EVENTS\n  No economic calendar data available."}

{perf_context if perf_context else "SELF PERFORMANCE AWARENESS\n  No performance data yet -- first trading session."}

Please provide your analysis and decision in the required JSON format."""


# ──────────────────────────────────────────────────────────────────────────────
# Main decision function
# ──────────────────────────────────────────────────────────────────────────────

def get_trading_decision(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    current_trade=None,
    whale_data: Optional[dict] = None,
    event_context: Optional[str] = None,
    perf_context: Optional[str] = None,
    regime: Optional[dict] = None,
) -> dict:
    """
    Send indicator data to Claude and get back a trading decision.

    This should only be called AFTER all pre-checks have passed.
    Returns a decision dict with at minimum:
      decision:    ENTER_LONG | ENTER_SHORT | HOLD | EXIT_LONG | EXIT_SHORT | STAY_OUT
      confidence:  HIGH | MEDIUM | LOW
      reasoning:   plain English explanation
      warnings:    list of concerns
    """
    log.info("Sending indicators to Claude for analysis...")

    user_message = _format_indicators(
        bar_1h, bar_5m, current_price, current_trade, whale_data, event_context,
        perf_context, regime
    )

    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}]
            )

            # Detect truncation -- if Claude hit the token ceiling the JSON will be cut off
            if response.stop_reason == "max_tokens":
                log.warning(
                    "Claude response hit max_tokens limit -- JSON is likely truncated. "
                    "Increase max_tokens if this recurs."
                )

            raw_text = response.content[0].text.strip()

            # Strip markdown fences if Claude adds them
            if raw_text.startswith("```"):
                lines    = raw_text.split("\n")
                lines    = [l for l in lines if not l.strip().startswith("```")]
                raw_text = "\n".join(lines).strip()

            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError as e:
                log.error("Claude returned invalid JSON (attempt %d/2): %s", attempt + 1, e)
                log.error("Raw Claude response: %s", raw_text[:800])
                if attempt == 0:
                    log.warning("Retrying Claude call once...")
                    continue
                return _safe_stay_out("Claude returned invalid JSON after retry -- staying out for safety")

            # Check exit confirmation -- require both SSL and RSI before exiting
            decision_text = decision.get("decision", "STAY_OUT")
            if decision_text in ("EXIT_LONG", "EXIT_SHORT") and current_trade:
                exit_check = check_exit_confirmation(decision_text, bar_5m, current_trade)
                if not exit_check["passed"]:
                    log.info("Exit held -- %s", exit_check["reason"])
                    decision["decision"]  = "HOLD"
                    decision["reasoning"] = exit_check["reason"]
                    decision["warnings"]  = decision.get("warnings", []) + [exit_check["reason"]]

            # Add metadata
            decision["timestamp"]       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            decision["tokens_used"]     = response.usage.input_tokens + response.usage.output_tokens
            decision["current_price"]   = current_price

            log.info(
                "Claude decision: %s | Confidence: %s | Tokens: %d",
                decision.get("decision"),
                decision.get("confidence"),
                decision.get("tokens_used", 0),
            )
            _log_decision_detail(decision)

            return decision

        except anthropic.APIError as e:
            log.error("Anthropic API error: %s", e)
            return _safe_stay_out(f"API error: {str(e)}")

        except Exception as e:
            log.error("Unexpected error calling Claude: %s", e)
            return _safe_stay_out(f"Unexpected error: {str(e)}")

    return _safe_stay_out("Claude failed after all attempts -- staying out for safety")


def _safe_stay_out(reason: str) -> dict:
    """Return a safe STAY_OUT decision when something goes wrong."""
    return {
        "decision":       "STAY_OUT",
        "confidence":     "HIGH",
        "one_hour_bias":  "UNCLEAR",
        "reasoning":      reason,
        "warnings":       [reason],
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tokens_used":    0,
    }


def _log_decision_detail(decision: dict) -> None:
    """
    Persist Claude's full reasoning, warnings, checklist and hunt assessment to
    the log so every decision is auditable after the fact (not just the verdict).
    """
    reasoning = decision.get("reasoning")
    if reasoning:
        log.info("  Reasoning: %s", reasoning)
    hunt = decision.get("hunt_assessment")
    if hunt and str(hunt).strip().lower() not in ("", "null", "none"):
        log.info("  Hunt assessment: %s", hunt)
    warnings = decision.get("warnings") or []
    if isinstance(warnings, (list, tuple)):
        for w in warnings:
            log.info("  Warning: %s", w)
    elif warnings:
        log.info("  Warning: %s", warnings)
    checklist = decision.get("checklist")
    if isinstance(checklist, dict) and checklist:
        log.info("  Checklist: %s", ", ".join("%s=%s" % (k, v) for k, v in checklist.items()))


# ──────────────────────────────────────────────────────────────────────────────
# Display helper
# ──────────────────────────────────────────────────────────────────────────────

def format_decision_for_display(decision: dict) -> str:
    """Format Claude's decision into a readable terminal display."""
    if decision is None:
        return "No decision available"

    d         = decision.get("decision", "UNKNOWN")
    c         = decision.get("confidence", "UNKNOWN")
    bias      = decision.get("one_hour_bias", "UNKNOWN")
    reasoning = decision.get("reasoning", "No reasoning provided")
    warnings  = decision.get("warnings", [])
    tokens    = decision.get("tokens_used", 0)
    timestamp = decision.get("timestamp", "")

    lines = [
        "=" * 60,
        "  CryptoHybrid AI -- Claude Decision",
        f"  {timestamp}",
        "=" * 60,
        "",
        f"  Decision:       {d}",
        f"  Confidence:     {c}",
        f"  1-Hour Bias:    {bias}",
        f"  BTC/GBP:        GBP {decision.get('current_price', 0):,.2f}",
        "",
        "  Reasoning:",
        f"  {reasoning}",
        "",
    ]

    if warnings:
        lines.append("  Warnings:")
        for w in warnings:
            lines.append(f"    - {w}")
        lines.append("")

    whale = decision.get("whale_assessment")
    if whale and whale != "null":
        lines.append(f"  Whale assessment: {whale}")
        lines.append("")

    checklist = decision.get("checklist", {})
    if checklist:
        lines.append("  Checklist:")
        for key, value in checklist.items():
            icon = "PASS" if value else "FAIL"
            lines.append(f"    [{icon}] {key.replace('_', ' ').title()}")
        lines.append("")

    lines.append(f"  API tokens used: {tokens}")
    lines.append("=" * 60)

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test -- calls Claude with a real bullish setup
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("CryptoHybrid AI -- Agent Brain self-test")
    log.info("Calling Claude with a bullish BTC/GBP setup...")

    # Bullish 1h bar
    bar_1h = pd.Series({
        "ssl_bull":       True,
        "rsi":            62.0,
        "macd":           143.0,
        "macd_signal":    120.0,
        "macd_histogram":  23.0,
        "tmo_main":         2.1,
        "tmo_smooth":       1.5,
        "chande_mo":       45.0,
        "money_flow":    8000.0,
        "open":         45800.0,
        "close":        46000.0,
    })

    # Bullish 5m bar
    bar_5m = pd.Series({
        "ssl_bull":        True,
        "rsi":             58.0,
        "macd":            50.0,
        "macd_signal":     35.0,
        "macd_histogram":  15.0,
        "tmo_main":         1.2,
        "tmo_smooth":       0.8,
        "chande_mo":       30.0,
        "money_flow":    5000.0,
        "open":         45900.0,
        "close":        46000.0,
    })

    # Some example whale data
    whale_data = {
        "Exchange inflow (24h)":  "Low -- accumulation signal",
        "Funding rate":           "+0.01% -- slightly positive, neutral",
        "Large transactions":     "3 large buys detected in last hour",
        "Order book":             "Buy wall at GBP 45,500 -- good support",
    }

    decision = get_trading_decision(
        bar_1h       = bar_1h,
        bar_5m       = bar_5m,
        current_price = 46000.0,
        whale_data   = whale_data,
    )

    print(format_decision_for_display(decision))
    log.info("Agent brain self-test complete.")