"""
CryptoHybrid AI -- agent_brain_eth.py
Sends ETH/GBP indicator data to Claude and gets back a trading decision.
Pre-checks (including ETH_SHORT_ONLY_MODE) must pass before this is called.
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

from pre_checks_eth import check_exit_confirmation

log = logging.getLogger("CryptoHybrid.AgentBrain.ETH")

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ──────────────────────────────────────────────────────────────────────────────
# Claude system prompt -- ETH/GBP specific
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are CryptoHybrid AI, a momentum SCALPING agent trading
ETH/GBP spot on Kraken (paper trading). You decide whether to ENTER LONG,
ENTER SHORT, HOLD an existing position, EXIT, or STAY OUT.

PHILOSOPHY -- MOMENTUM SCALPING
CryptoHybrid is a momentum scalping system. The strategy is quick entry on a
momentum burst, quick exit via a tight trailing stop or the take-profit target.
Target small, consistent gains -- do NOT hold hoping for a large move. A 2%
target hit is a successful trade. Exit discipline matters as much as entry
discipline. ETH/GBP is MORE VOLATILE than BTC -- bursts are sharper; respect the
tight stop and take the quick win.

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

SESSION CONTEXT (win rate by session, from backtest -- current session is given below)
  NY session     (13:00-21:00 UTC): 58.7% -- highest quality, slightly lower bar.
  Asian session  (00:00-08:00 UTC): 54.2% -- good.
  London session (08:00-16:00 UTC): 51.8% -- moderate, slightly higher bar.

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

DIRECTION SYMMETRY (24 Jul 2026 -- fully bidirectional)
  There is NO SHORT gate. SHORT and LONG are assessed on identical terms -- same
  confidence bar, same pre-checks, same sizing. Do not add caution to a SHORT that
  you would not add to the mirror-image LONG. Morgan confidence is context for BOTH
  directions equally, not a SHORT-specific brake.
  Direction is driven by the 1h SSL (primary), NOT the daily SSL. ETH LONG/SHORT
  win rates are on a neutral 50% baseline (reset 11 Jul 2026, clean data rebuilding);
  apply the regime-appropriate confidence bar.

WHALE DATA (if provided):
  Use whale data as additional context.
  Large exchange inflows = selling pressure (bearish).
  Large exchange outflows = accumulation (bullish).
  Funding rate strongly positive = overleveraged longs (bearish risk).
  Funding rate strongly negative = overleveraged shorts (bullish risk).

LIQUIDATION ZONE INTELLIGENCE (if provided):
  HUNT PROBABILITY SCORES (0-100 per direction):
    0-29   LOW  -- no meaningful hunt risk. Normal entry criteria apply.
    30-59  MEDIUM -- some hunt risk. Raise conviction bar slightly.
    60-100 HIGH -- strong hunt signal. AVOID entering in this direction.

  HOW TO USE:
  - If DOWN hunt score >= 60: do NOT enter LONG.
  - If UP hunt score >= 60: do NOT enter SHORT.
  - If PRIMARY RISK is BALANCED: prefer STAY_OUT until clearer picture emerges.

  ETH-specific note: ETH liquidation cascades can be faster and more severe
  than BTC. A hunt that sweeps 2% in BTC may sweep 4-6% in ETH.
  Be especially cautious when hunt scores are elevated.

ECONOMIC EVENTS (if provided):
  WITHIN 30 MINUTES OF EVENT:
    Strongly consider STAY_OUT. ETH reacts sharply to US macro events.
    Mark confidence MEDIUM or LOW even on strong setups.

  WITHIN 2 HOURS OF EVENT:
    Require stronger confluence. Prefer STAY_OUT on borderline setups.

  POST-EVENT, 15-60 MINUTES AFTER:
    Hard block lifted. Wait for indicator confirmation before entering.
    ETH often has extended moves after macro events -- confirm direction first.

SELF PERFORMANCE AWARENESS (Morgan) -- CONTEXT ONLY
  Morgan is CONTEXT; it does NOT change your entry threshold. Assess every ETH burst
  the SAME way at any Morgan score of 30 or above -- do NOT raise the bar, demand
  "exceptional" setups, or switch into a "conservative" posture when Morgan is low.
  The three-zone model governs Morgan:
  Zone 1 NORMAL (>= 50):    trade as usual.
  Zone 2 WARNING (30-49):   trading CONTINUES exactly as normal -- a heads-up for Nick
    (dashboard warning + manual reset), NOT a cue for you to tighten entries.
  Zone 3 HARD BLOCK (< 30): the SYSTEM suspends new entries and Gaius intervenes, so you
    will not be asked to enter here; existing positions are still managed/exited.

HARD RULES -- NEVER VIOLATE
1.  The 1-hour SSL is the primary direction; the 5m confirms the burst.
2.  Enter WITH momentum, not against it -- 1h and 5m SSL must agree on direction.
3.  Scalp: quick in on the burst, let the tight stop / 2% target manage the exit.
4.  Respect the regime -- elevated bar for LONGs in BEAR; SHORTs on identical terms.
5.  Never chase a missed burst -- another one comes along; wait for the next setup.
6.  Do NOT hold hoping for a big move -- this is scalping, not trend-riding.
7.  Exit only when BOTH 5m SSL AND 5m RSI confirm reversal (the system also exits).
8.  When genuinely unclear -- STAY OUT. But a valid burst in-regime is a trade.
9.  ETH is highly volatile -- the 1% stop is tight; sizing and stop are handled for you.
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
  NEVER use fixed point values. NEVER multiply a percentage by 100.
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
  "warnings": ["list any concerns or cautions -- always warn if LONG entry given ETH backtest"],
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
    """Primary trading session for the given UTC hour (backtest win-rate bands)."""
    if 13 <= hour_utc < 21:
        return "NY (13:00-21:00 UTC, 58.7% -- highest quality)"
    if 0 <= hour_utc < 8:
        return "Asian (00:00-08:00 UTC, 54.2% -- good)"
    if 8 <= hour_utc < 16:
        return "London (08:00-16:00 UTC, 51.8% -- moderate)"
    return "Off-session (21:00-24:00 UTC -- thin, higher bar)"


def _format_regime_block(regime: Optional[dict], hour_utc: int) -> str:
    """Build the live REGIME / SESSION block for Arthur (ETH, bidirectional, no SHORT gate)."""
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
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    regime_block = _format_regime_block(regime, now_dt.hour)

    candle_colour = "GREEN" if bar_5m.get("close", 0) >= bar_5m.get("open", 0) else "RED"

    position_text = "None -- no open position"
    if current_trade is not None:
        position_text = (
            f"OPEN {current_trade.direction} | "
            f"entry=GBP {current_trade.entry_price:.4f} | "
            f"current=GBP {current_price:.4f} | "
            f"stop=GBP {current_trade.stop_loss:.4f} | "
            f"target=GBP {current_trade.take_profit:.4f} | "
            f"size=GBP {current_trade.position_size_gbp:.2f}"
        )

    whale_text = "No whale data available."
    if whale_data:
        whale_text = "\n".join([
            f"  {k}: {v}" for k, v in whale_data.items()
        ])

    return f"""Please analyse the current ETH/GBP market conditions.

TIME AND PRICE
  Time (UTC):     {now}
  ETH/GBP Price:  GBP {current_price:,.4f}

{regime_block}

1-HOUR CHART (Primary Direction)
  SSL Cloud:      {'BULL' if bar_1h.get('ssl_bull') else 'BEAR'}
  RSI:            {bar_1h.get('rsi', 0):.1f}
  MACD:           {bar_1h.get('macd', 0):.6f}
  MACD Signal:    {bar_1h.get('macd_signal', 0):.6f}
  MACD Histogram: {bar_1h.get('macd_histogram', 0):.6f}
  TMO Main:       {bar_1h.get('tmo_main', 0):.3f}
  TMO Smooth:     {bar_1h.get('tmo_smooth', 0):.3f}
  Chande MO:      {bar_1h.get('chande_mo', 0):.1f}
  Money Flow:     {bar_1h.get('money_flow', 0):.4f}

5-MINUTE CHART (Entry Timing)
  SSL Cloud:      {'BULL' if bar_5m.get('ssl_bull') else 'BEAR'}
  RSI:            {bar_5m.get('rsi', 0):.1f}
  MACD:           {bar_5m.get('macd', 0):.6f}
  MACD Signal:    {bar_5m.get('macd_signal', 0):.6f}
  MACD Histogram: {bar_5m.get('macd_histogram', 0):.6f}
  TMO Main:       {bar_5m.get('tmo_main', 0):.3f}
  TMO Smooth:     {bar_5m.get('tmo_smooth', 0):.3f}
  Chande MO:      {bar_5m.get('chande_mo', 0):.1f}
  Money Flow:     {bar_5m.get('money_flow', 0):.4f}
  Candle Colour:  {candle_colour}

CURRENT POSITION
  {position_text}

WHALE DATA
{whale_text}

{event_context if event_context else "ECONOMIC EVENTS\n  No economic calendar data available."}

{perf_context if perf_context else "SELF PERFORMANCE AWARENESS\n  No ETH performance data yet -- first trading session."}

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
    log.info("[ETH] Sending indicators to Claude for analysis...")

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

            if response.stop_reason == "max_tokens":
                log.warning(
                    "[ETH] Claude response hit max_tokens limit -- JSON may be truncated."
                )

            raw_text = response.content[0].text.strip()

            if raw_text.startswith("```"):
                lines    = raw_text.split("\n")
                lines    = [l for l in lines if not l.strip().startswith("```")]
                raw_text = "\n".join(lines).strip()

            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError as e:
                log.error("[ETH] Claude returned invalid JSON (attempt %d/2): %s", attempt + 1, e)
                if attempt == 0:
                    log.warning("[ETH] Retrying Claude call once...")
                    continue
                return _safe_stay_out("[ETH] Claude returned invalid JSON after retry")

            decision_text = decision.get("decision", "STAY_OUT")
            if decision_text in ("EXIT_LONG", "EXIT_SHORT") and current_trade:
                exit_check = check_exit_confirmation(decision_text, bar_5m, current_trade)
                if not exit_check["passed"]:
                    log.info("[ETH] Exit held -- %s", exit_check["reason"])
                    decision["decision"]  = "HOLD"
                    decision["reasoning"] = exit_check["reason"]
                    decision["warnings"]  = decision.get("warnings", []) + [exit_check["reason"]]

            decision["timestamp"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            decision["tokens_used"]   = response.usage.input_tokens + response.usage.output_tokens
            decision["current_price"] = current_price

            log.info(
                "[ETH] Claude decision: %s | Confidence: %s | Tokens: %d",
                decision.get("decision"),
                decision.get("confidence"),
                decision.get("tokens_used", 0),
            )
            _log_decision_detail(decision)

            return decision

        except anthropic.APIError as e:
            log.error("[ETH] Anthropic API error: %s", e)
            return _safe_stay_out(f"[ETH] API error: {str(e)}")

        except Exception as e:
            log.error("[ETH] Unexpected error calling Claude: %s", e)
            return _safe_stay_out(f"[ETH] Unexpected error: {str(e)}")

    return _safe_stay_out("[ETH] Claude failed after all attempts -- staying out for safety")


def _safe_stay_out(reason: str) -> dict:
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
    the log so every ETH decision is auditable after the fact (not just verdict).
    """
    reasoning = decision.get("reasoning")
    if reasoning:
        log.info("[ETH]   Reasoning: %s", reasoning)
    hunt = decision.get("hunt_assessment")
    if hunt and str(hunt).strip().lower() not in ("", "null", "none"):
        log.info("[ETH]   Hunt assessment: %s", hunt)
    warnings = decision.get("warnings") or []
    if isinstance(warnings, (list, tuple)):
        for w in warnings:
            log.info("[ETH]   Warning: %s", w)
    elif warnings:
        log.info("[ETH]   Warning: %s", warnings)
    checklist = decision.get("checklist")
    if isinstance(checklist, dict) and checklist:
        log.info("[ETH]   Checklist: %s", ", ".join("%s=%s" % (k, v) for k, v in checklist.items()))


def format_decision_for_display(decision: dict) -> str:
    if decision is None:
        return "No ETH decision available"

    d         = decision.get("decision", "UNKNOWN")
    c         = decision.get("confidence", "UNKNOWN")
    bias      = decision.get("one_hour_bias", "UNKNOWN")
    reasoning = decision.get("reasoning", "No reasoning provided")
    warnings  = decision.get("warnings", [])
    tokens    = decision.get("tokens_used", 0)
    timestamp = decision.get("timestamp", "")

    lines = [
        "=" * 60,
        "  CryptoHybrid AI -- [ETH] Claude Decision",
        f"  {timestamp}",
        "=" * 60,
        "",
        f"  Decision:       {d}",
        f"  Confidence:     {c}",
        f"  1-Hour Bias:    {bias}",
        f"  ETH/GBP:        GBP {decision.get('current_price', 0):,.4f}",
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

    lines.append(f"  API tokens used: {tokens}")
    lines.append("=" * 60)

    return "\n".join(lines)
