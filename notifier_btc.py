"""
CryptoHybrid AI -- notifier_btc.py
Push notifications via Pushover. All calls are silent failures.
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)

log = logging.getLogger("CryptoHybrid.Notifier")

_PUSHOVER_API = "https://api.pushover.net/1/messages.json"
_USER         = os.getenv("PUSHOVER_USER_KEY",  "")
_TOKEN        = os.getenv("PUSHOVER_API_TOKEN", "")

# Pushover priority constants
_P_NORMAL = 0
_P_HIGH   = 1


def _send(title: str, message: str, priority: int = _P_NORMAL) -> None:
    """Core Pushover send -- silently swallows all errors."""
    if not _USER or not _TOKEN:
        log.debug("Pushover credentials not configured -- skipping notification")
        return
    try:
        resp = requests.post(
            _PUSHOVER_API,
            data={
                "token":    _TOKEN,
                "user":     _USER,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=5,
        )
        if resp.status_code == 200:
            log.debug("Notification sent: %s", title)
        else:
            log.warning("Pushover returned HTTP %d for: %s", resp.status_code, title)
    except Exception as exc:
        log.warning("Pushover notification failed (%s): %s", title, exc)


# ──────────────────────────────────────────────────────────────────────────────
# Public notification functions
# ──────────────────────────────────────────────────────────────────────────────

def notify_trade_opened(direction: str, entry_price: float, stop_loss: float,
                        take_profit: float, size_gbp: float,
                        pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Trade Opened",
        message=(
            f"{direction} opened at GBP {entry_price:,.2f} | "
            f"Stop: GBP {stop_loss:,.2f} | "
            f"Target: GBP {take_profit:,.2f} | "
            f"Size: GBP {size_gbp:.2f}"
        ),
    )


def notify_trade_closed_win(direction: str, close_price: float, pnl_gbp: float,
                             pnl_pct: float, capital: float, reason: str,
                             pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Trade Won!",
        message=(
            f"{direction} closed at GBP {close_price:,.2f} | "
            f"P&L: +GBP {pnl_gbp:.2f} (+{pnl_pct:.2f}%) | "
            f"Capital: GBP {capital:.2f} | "
            f"Reason: {reason}"
        ),
    )


def notify_trade_closed_loss(direction: str, close_price: float, pnl_gbp: float,
                              pnl_pct: float, capital: float, reason: str,
                              pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Trade Lost",
        message=(
            f"{direction} closed at GBP {close_price:,.2f} | "
            f"P&L: -GBP {abs(pnl_gbp):.2f} (-{abs(pnl_pct):.2f}%) | "
            f"Capital: GBP {capital:.2f} | "
            f"Reason: {reason}"
        ),
    )


def notify_kill_switch_triggered(tier: int, reason: str, wait_hours: int,
                                  daily_pnl: float, capital: float,
                                  pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] KILL SWITCH (Tier {tier})",
        message=(
            f"{reason} | "
            f"Daily P&L: GBP {daily_pnl:+.2f} | "
            f"Resuming in {wait_hours} hours | "
            f"Capital: GBP {capital:.2f}"
        ),
        priority=_P_HIGH,
    )


def notify_kill_switch_reset(tier: int, wait_hours: int, capital: float,
                              pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Resuming Trading",
        message=(
            f"Kill switch reset after {wait_hours} hour cooldown (Tier {tier}). "
            f"Capital: GBP {capital:.2f}. Watching for setups."
        ),
    )


def notify_tier3_urgent(pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] URGENT: Manual Review Needed",
        message=(
            f"[{pair}] Kill switch triggered 3 times in 48 hours. "
            "System paused 24 hours. Please review performance."
        ),
        priority=_P_HIGH,
    )


def notify_system_startup(capital: float, pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] System Started",
        message=(
            f"CryptoHybrid AI is live. "
            f"Capital: GBP {capital:.2f}. "
            f"Watching {pair}/GBP on Kraken."
        ),
    )


def notify_economic_block(event_name: str, remain_mins: int) -> None:
    _send(
        title="CryptoHybrid AI - Trading Paused",
        message=(
            f"Post-event volatility window: {event_name}. "
            f"{remain_mins} min remaining. Trading resumes automatically."
        ),
    )


def notify_milestone_review(milestone_number: int, pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Milestone Review Complete",
        message=(
            f"[{pair}] Ace has completed his {milestone_number * 50}-trade milestone review. "
            f"Check logs/ace_review_{milestone_number:02d}.txt for insights and suggestions."
        ),
    )


def notify_daily_summary(date_str: str, trades: int, pnl_gbp: float,
                          capital: float, win_rate: float,
                          pair: str = "BTC") -> None:
    _send(
        title=f"CryptoHybrid AI - [{pair}] Daily Summary",
        message=(
            f"[{pair}] Date: {date_str} | "
            f"Trades: {trades} | "
            f"P&L: GBP {pnl_gbp:+.2f} | "
            f"Capital: GBP {capital:.2f} | "
            f"Win rate: {win_rate:.0f}%"
        ),
    )
