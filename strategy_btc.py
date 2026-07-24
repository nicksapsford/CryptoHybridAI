"""
CryptoHybrid AI — strategy_btc.py
Conservative dual-timeframe strategy for BTC/GBP
- 1h chart sets the trend direction
- 5m chart triggers entry when 5 of 6 indicators agree
- 30% position size, 2% trailing stop, 10% take profit ceiling
- Trades both LONG and SHORT
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import pandas as pd

log = logging.getLogger("CryptoHybrid.Strategy")

# ──────────────────────────────────────────────────────────────────────────────
# Settings — change these to tune the strategy
# ──────────────────────────────────────────────────────────────────────────────

POSITION_SIZE_PCT   = 0.30   # 30% of current capital per trade
TRAILING_STOP_PCT   = 0.01   # 1% trailing stop (momentum scalping -- backtest winner)
TAKE_PROFIT_PCT     = 0.02   # 2% take profit (scalping target -- quick in, quick out)
SPREAD_PCT          = 0.0002 # 0.02% Kraken bid/ask -- modelled on entry fills (Stanley)
MIN_INDICATORS      = 5      # minimum indicators that must agree (out of 6)
MIN_1H_INDICATORS   = 4      # minimum 1h indicators for trend confirmation


# ──────────────────────────────────────────────────────────────────────────────
# Trade record — stores everything about one trade
# ──────────────────────────────────────────────────────────────────────────────

PROFIT_LADDER = [
    # Rescaled for the momentum-scalping regime (Gaius Commission 009, 23 Jul 2026):
    # a 2%-target trade on a ~GBP300 position floats ~GBP6 max, so the old GBP15/35/60
    # ladder (trend era) could NEVER fire. Steps at ~33/58/83% of the ~GBP6 target.
    {"trigger_gbp": 2.00, "floor_gbp": 1.50},
    {"trigger_gbp": 3.50, "floor_gbp": 3.00},
    {"trigger_gbp": 5.00, "floor_gbp": 4.50},
]


@dataclass
class Trade:
    """
    Represents a single paper trade.
    Everything about the trade is stored here — entry, exit, profit/loss.
    """
    direction: str          # 'LONG' or 'SHORT'
    entry_price: float      # price we bought/sold at
    position_size_gbp: float  # how much £ we put in
    entry_time: datetime    # when we entered

    # These update as the trade runs
    highest_price: float = field(init=False)   # for LONG trailing stop
    lowest_price: float  = field(init=False)   # for SHORT trailing stop
    stop_loss: float     = field(init=False)   # current stop loss level
    take_profit: float   = field(init=False)   # target price to close at

    # Set when trade closes
    exit_price: Optional[float]    = field(default=None)
    exit_time: Optional[datetime]  = field(default=None)
    exit_reason: Optional[str]     = field(default=None)
    pnl_gbp: Optional[float]       = field(default=None)
    pnl_pct: Optional[float]       = field(default=None)

    def __post_init__(self):
        """Called automatically after __init__ — sets up stop and target."""
        self.highest_price = self.entry_price
        self.lowest_price  = self.entry_price

        if self.direction == "LONG":
            self.stop_loss   = self.entry_price * (1 - TRAILING_STOP_PCT)
            self.take_profit = self.entry_price * (1 + TAKE_PROFIT_PCT)
        else:  # SHORT
            self.stop_loss   = self.entry_price * (1 + TRAILING_STOP_PCT)
            self.take_profit = self.entry_price * (1 - TAKE_PROFIT_PCT)

    def apply_profit_ladder(self, price: float):
        """Profit Protection Ladder (Variant 2), percentage-based -- crypto sizes by
        position_size_gbp (not stake/pt). Tighten the stop to guarantee a minimum GBP
        floor as floating profit builds -- only ever tightens, never on a floating
        loss; trailing stop + take-profit ceiling unaffected. Idempotent (survives
        reload). Returns a dict describing a NEWLY triggered rung, else None."""
        if not PROFIT_LADDER or self.position_size_gbp <= 0:
            return None
        if not hasattr(self, "ladder_step"):
            self.ladder_step, self.ladder_floor_gbp = 0, 0.0
        pnl_pct = ((price - self.entry_price) / self.entry_price) if self.direction == "LONG" \
            else ((self.entry_price - price) / self.entry_price)
        float_gbp = self.position_size_gbp * pnl_pct
        if float_gbp <= 0:                       # never engage on a floating loss
            return None
        idx, floor = 0, 0.0
        for i, s in enumerate(PROFIT_LADDER, start=1):
            if float_gbp >= s["trigger_gbp"]:
                idx, floor = i, s["floor_gbp"]
        if idx == 0:
            return None
        new_rung = idx > self.ladder_step
        if new_rung:
            self.ladder_step = idx
            self.ladder_floor_gbp = floor
        if self.ladder_floor_gbp <= 0:
            return None
        floor_frac = self.ladder_floor_gbp / self.position_size_gbp
        stop_before = self.stop_loss
        if self.direction == "LONG":
            floor_stop = self.entry_price * (1 + floor_frac)
            if floor_stop > self.stop_loss:      # tighten only
                self.stop_loss = floor_stop
        else:
            floor_stop = self.entry_price * (1 - floor_frac)
            if floor_stop < self.stop_loss:      # tighten only
                self.stop_loss = floor_stop
        if new_rung:
            log.info("  PROFIT LADDER step %d: float GBP %.2f -> floor GBP %.2f | stop %.2f -> %.2f",
                     idx, float_gbp, self.ladder_floor_gbp, stop_before, self.stop_loss)
            return {"step": idx, "floor_gbp": self.ladder_floor_gbp,
                    "trigger_float_gbp": round(float_gbp, 2),
                    "stop_before": round(stop_before, 2), "stop_after": round(self.stop_loss, 2)}
        return None

    def update_trailing_stop(self, current_price: float) -> None:
        """
        Move the stop loss up (for LONG) or down (for SHORT)
        as price moves in our favour. It never moves against us.
        """
        if self.direction == "LONG":
            if current_price > self.highest_price:
                self.highest_price = current_price
                new_stop = current_price * (1 - TRAILING_STOP_PCT)
                # Only move stop UP, never down
                if new_stop > self.stop_loss:
                    self.stop_loss = new_stop
                    log.info(
                        "  Trailing stop moved UP to £%.2f (price=£%.2f)",
                        self.stop_loss, current_price
                    )

        else:  # SHORT
            if current_price < self.lowest_price:
                self.lowest_price = current_price
                new_stop = current_price * (1 + TRAILING_STOP_PCT)
                # Only move stop DOWN, never up
                if new_stop < self.stop_loss:
                    self.stop_loss = new_stop
                    log.info(
                        "  Trailing stop moved DOWN to £%.2f (price=£%.2f)",
                        self.stop_loss, current_price
                    )

    def update_excursions(self, price: float) -> None:
        """MAE/MFE tracking (Gaius Commission 009). Records the peak FAVOURABLE (mfe) and
        worst ADVERSE (mae) excursion, as a fraction of entry, reached while the trade is
        open. Analysis only -- never affects stops, exits or the ladder."""
        if not hasattr(self, "mfe_pct"):
            self.mfe_pct = 0.0
            self.mae_pct = 0.0
        fav = ((price - self.entry_price) / self.entry_price) if self.direction == "LONG" \
            else ((self.entry_price - price) / self.entry_price)
        if fav > self.mfe_pct:
            self.mfe_pct = fav
        if -fav > self.mae_pct:
            self.mae_pct = -fav

    @property
    def mfe_gbp(self) -> float:
        return round(self.position_size_gbp * getattr(self, "mfe_pct", 0.0), 2)

    @property
    def mae_gbp(self) -> float:
        return round(self.position_size_gbp * getattr(self, "mae_pct", 0.0), 2)

    @property
    def mfe_pts(self) -> float:
        return round(self.entry_price * getattr(self, "mfe_pct", 0.0), 2)

    @property
    def mae_pts(self) -> float:
        return round(self.entry_price * getattr(self, "mae_pct", 0.0), 2)

    def check_exit(self, current_price: float) -> Optional[str]:
        """
        Check if the current price has hit our stop loss or take profit.
        Returns the exit reason as a string, or None if still running.
        """
        if self.direction == "LONG":
            if current_price <= self.stop_loss:
                return "STOP_LOSS"
            if current_price >= self.take_profit:
                return "TAKE_PROFIT"

        else:  # SHORT
            if current_price >= self.stop_loss:
                return "STOP_LOSS"
            if current_price <= self.take_profit:
                return "TAKE_PROFIT"

        return None  # still running, no exit yet

    def close(self, exit_price: float, reason: str) -> None:
        """Record the exit details and calculate profit/loss."""
        self.exit_price  = exit_price
        self.exit_time   = datetime.now(timezone.utc)
        self.exit_reason = reason

        # Calculate P&L
        # For LONG:  profit = (exit - entry) / entry
        # For SHORT: profit = (entry - exit) / entry
        if self.direction == "LONG":
            self.pnl_pct = (exit_price - self.entry_price) / self.entry_price
        else:
            self.pnl_pct = (self.entry_price - exit_price) / self.entry_price

        self.pnl_gbp = self.position_size_gbp * self.pnl_pct

    def summary(self) -> str:
        """Return a one-line summary of this trade."""
        if self.exit_price is None:
            # Trade still open
            return (
                f"[OPEN {self.direction}] entry=£{self.entry_price:.2f} "
                f"stop=£{self.stop_loss:.2f} target=£{self.take_profit:.2f} "
                f"size=£{self.position_size_gbp:.2f}"
            )
        else:
            # Trade closed
            emoji = "✓" if self.pnl_gbp >= 0 else "✗"
            return (
                f"[{emoji} {self.direction}] "
                f"entry=£{self.entry_price:.2f} exit=£{self.exit_price:.2f} "
                f"P&L=£{self.pnl_gbp:+.2f} ({self.pnl_pct*100:+.2f}%) "
                f"reason={self.exit_reason}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Indicator scoring — counts how many indicators agree on direction
# ──────────────────────────────────────────────────────────────────────────────

def _score_bar(bar: pd.Series) -> dict:
    """
    Score a single candle bar against all 6 indicators.
    Returns a dict with 'long_count', 'short_count', and individual scores.

    Each indicator votes +1 for LONG or -1 for SHORT.
    We count the votes and see if enough agree.
    """
    scores = {}

    # 1. SSL Cloud — is the trend up or down?
    ssl_bull = bar.get("ssl_bull")
    if pd.notna(ssl_bull):
        scores["ssl"] = 1 if ssl_bull else -1

    # 2. RSI — above 55 = bullish momentum, below 45 = bearish
    rsi = bar.get("rsi")
    if pd.notna(rsi):
        if rsi > 55:
            scores["rsi"] = 1
        elif rsi < 45:
            scores["rsi"] = -1
        else:
            scores["rsi"] = 0  # neutral zone — counts as neither

    # 3. MACD histogram — positive = buyers winning, negative = sellers
    hist = bar.get("macd_histogram")
    if pd.notna(hist):
        scores["macd"] = 1 if hist > 0 else -1

    # 4. TMO — main line above smooth = accelerating up
    tmo_main   = bar.get("tmo_main")
    tmo_smooth = bar.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        scores["tmo"] = 1 if tmo_main > tmo_smooth else -1

    # 5. Chande Momentum — above 0 = positive momentum
    cmo = bar.get("chande_mo")
    if pd.notna(cmo):
        scores["chande"] = 1 if cmo > 0 else -1

    # 6. Money Flow — positive = money flowing in (accumulation)
    mf = bar.get("money_flow")
    if pd.notna(mf):
        scores["money_flow"] = 1 if mf > 0 else -1

    # Count votes
    long_count  = sum(1 for v in scores.values() if v == 1)
    short_count = sum(1 for v in scores.values() if v == -1)

    return {
        "scores":      scores,
        "long_count":  long_count,
        "short_count": short_count,
        "total":       len(scores),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main Strategy class
# ──────────────────────────────────────────────────────────────────────────────

class BTCStrategy:
    """
    CryptoHybrid AI — Conservative BTC/GBP Strategy

    How to use:
        strategy = BTCStrategy(capital_gbp=1000.0)
        signal = strategy.evaluate(bar_1h, bar_5m, current_price)
        # signal is 'LONG', 'SHORT', 'CLOSE', or 'HOLD'
    """

    def __init__(self, capital_gbp: float = 1000.0) -> None:
        self.capital_gbp   = capital_gbp        # current paper capital
        self.current_trade: Optional[Trade] = None   # the open trade (if any)
        self.trade_history: list[Trade]     = []     # all completed trades

        log.info(
            "CryptoHybrid AI | Strategy ready | capital=£%.2f | "
            "position=%.0f%% | trailing_stop=%.0f%% | take_profit=%.0f%%",
            self.capital_gbp,
            POSITION_SIZE_PCT * 100,
            TRAILING_STOP_PCT * 100,
            TAKE_PROFIT_PCT   * 100,
        )

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def in_trade(self) -> bool:
        """True if we currently have an open position."""
        return self.current_trade is not None

    @property
    def total_trades(self) -> int:
        return len(self.trade_history)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trade_history if t.pnl_gbp and t.pnl_gbp > 0)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_gbp for t in self.trade_history if t.pnl_gbp is not None)

    # ── Core logic ───────────────────────────────────────────────────────────

    def _get_1h_trend(self, bar_1h: pd.Series) -> str:
        """
        Determine the 1-hour trend direction.
        Requires MIN_1H_INDICATORS to agree before calling a trend.
        Returns 'LONG', 'SHORT', or 'NEUTRAL'
        """
        result = _score_bar(bar_1h)

        if result["long_count"] >= MIN_1H_INDICATORS:
            return "LONG"
        elif result["short_count"] >= MIN_1H_INDICATORS:
            return "SHORT"
        else:
            return "NEUTRAL"

    def _get_5m_signal(self, bar_5m: pd.Series) -> str:
        """
        Determine the 5-minute entry signal.
        Requires MIN_INDICATORS (5 of 6) to agree.
        Returns 'LONG', 'SHORT', or 'NEUTRAL'
        """
        result = _score_bar(bar_5m)

        log.info(
            "  5m indicators: LONG=%d SHORT=%d | scores=%s",
            result["long_count"],
            result["short_count"],
            {k: ("+1" if v == 1 else ("-1" if v == -1 else " 0"))
             for k, v in result["scores"].items()}
        )

        if result["long_count"] >= MIN_INDICATORS:
            return "LONG"
        elif result["short_count"] >= MIN_INDICATORS:
            return "SHORT"
        else:
            return "NEUTRAL"

    def _open_trade(self, direction: str, price: float) -> None:
        """Open a new paper trade.

        Spread modelling (Stanley): the fill is the adverse side of the Kraken
        bid/ask, so a LONG buys at the ask (price * (1 + SPREAD_PCT)) and a SHORT
        sells at the bid (price * (1 - SPREAD_PCT)). Stop/target derive from the
        real fill, so the spread is a genuine entry cost -- previously unmodelled.
        """
        position_size = self.capital_gbp * POSITION_SIZE_PCT
        fill_price = price * (1 + SPREAD_PCT) if direction == "LONG" \
            else price * (1 - SPREAD_PCT)

        self.current_trade = Trade(
            direction         = direction,
            entry_price       = fill_price,
            position_size_gbp = position_size,
            entry_time        = datetime.now(timezone.utc),
        )

        log.info(
            ">>> TRADE OPENED | %s | mid=£%.2f fill=£%.2f (spread %.2f%%) | size=£%.2f | "
            "stop=£%.2f | target=£%.2f",
            direction,
            price,
            fill_price,
            SPREAD_PCT * 100,
            position_size,
            self.current_trade.stop_loss,
            self.current_trade.take_profit,
        )

    def _close_trade(self, price: float, reason: str) -> None:
        """Close the current trade and update capital."""
        if not self.current_trade:
            return

        self.current_trade.close(exit_price=price, reason=reason)
        pnl = self.current_trade.pnl_gbp

        # Update capital
        self.capital_gbp += pnl

        log.info(
            "<<< TRADE CLOSED | %s | P&L=£%+.2f | capital=£%.2f | reason=%s",
            self.current_trade.direction,
            pnl,
            self.capital_gbp,
            reason,
        )

        # Move to history
        self.trade_history.append(self.current_trade)
        self.current_trade = None

    # ── Main evaluate method — call this on every new 5m bar ─────────────────

    def evaluate(
        self,
        bar_1h: pd.Series,
        bar_5m: pd.Series,
        current_price: float,
    ) -> str:
        """
        The main strategy brain. Call this every time a new 5m candle closes.

        Steps:
        1. If in a trade — check trailing stop and take profit first
        2. Check if 1h trend has reversed — if so, close trade
        3. If not in a trade — look for a new entry signal
        4. Return what action was taken

        Returns: 'LONG_OPEN', 'SHORT_OPEN', 'CLOSE', or 'HOLD'
        """
        log.info(
            "=== Strategy evaluate | price=£%.2f | in_trade=%s ===",
            current_price, self.in_trade
        )

        # ── Step 1: Manage open trade ────────────────────────────────────────
        if self.in_trade:
            trade = self.current_trade

            # Update trailing stop
            trade.update_trailing_stop(current_price)
            trade.update_excursions(current_price)   # MAE/MFE (Commission 009)

            # Check for exit conditions (stop loss or take profit)
            exit_reason = trade.check_exit(current_price)
            if exit_reason:
                self._close_trade(current_price, exit_reason)
                return "CLOSE"

            # Check if 1h trend has reversed against our trade
            trend_1h = self._get_1h_trend(bar_1h)
            if trade.direction == "LONG" and trend_1h == "SHORT":
                log.info("  1h trend reversed to SHORT — closing LONG trade")
                self._close_trade(current_price, "TREND_REVERSAL")
                return "CLOSE"
            elif trade.direction == "SHORT" and trend_1h == "LONG":
                log.info("  1h trend reversed to LONG — closing SHORT trade")
                self._close_trade(current_price, "TREND_REVERSAL")
                return "CLOSE"

            # Trade still running fine
            log.info("  Trade running | %s", trade.summary())
            return "HOLD"

        # ── Step 2: Look for a new entry ─────────────────────────────────────
        trend_1h = self._get_1h_trend(bar_1h)
        log.info("  1h trend: %s", trend_1h)

        # Only look for entries when the 1h has a clear direction
        if trend_1h == "NEUTRAL":
            log.info("  1h trend neutral — no trade")
            return "HOLD"

        # Check 5m for entry signal
        signal_5m = self._get_5m_signal(bar_5m)

        # Entry only when 5m agrees with 1h trend
        if trend_1h == "LONG" and signal_5m == "LONG":
            self._open_trade("LONG", current_price)
            return "LONG_OPEN"

        elif trend_1h == "SHORT" and signal_5m == "SHORT":
            self._open_trade("SHORT", current_price)
            return "SHORT_OPEN"

        else:
            log.info(
                "  No entry — 1h=%s but 5m=%s (waiting for alignment)",
                trend_1h, signal_5m
            )
            return "HOLD"

    # ── Status & reporting ───────────────────────────────────────────────────

    def print_status(self) -> None:
        """Print a full strategy status to the terminal."""
        log.info("=" * 60)
        log.info("CryptoHybrid AI — Strategy Status")
        log.info("  Capital:      £%.2f", self.capital_gbp)
        log.info("  Total P&L:    £%+.2f", self.total_pnl)
        log.info("  Trades:       %d total | %d wins | %.0f%% win rate",
                 self.total_trades, self.winning_trades, self.win_rate)

        if self.in_trade:
            log.info("  Open trade:   %s", self.current_trade.summary())
        else:
            log.info("  Open trade:   None")

        if self.trade_history:
            log.info("  Recent trades:")
            for t in self.trade_history[-5:]:   # show last 5
                log.info("    %s", t.summary())
        log.info("=" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# Quick test — run this file directly to check it works
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("CryptoHybrid AI — Strategy self-test starting...")

    # Create a strategy with £1,000 capital
    strategy = BTCStrategy(capital_gbp=1000.0)

    # Simulate a bullish 1h bar (all indicators pointing up)
    bar_1h = pd.Series({
        "ssl_bull":      True,
        "rsi":           62.0,
        "macd_histogram": 120.0,
        "tmo_main":      2.1,
        "tmo_smooth":    1.5,
        "chande_mo":     45.0,
        "money_flow":    8000.0,
    })

    # Simulate a bullish 5m bar (5 of 6 agree)
    bar_5m_long = pd.Series({
        "ssl_bull":       True,
        "rsi":            58.0,
        "macd_histogram":  50.0,
        "tmo_main":        1.2,
        "tmo_smooth":      0.8,
        "chande_mo":      30.0,
        "money_flow":   5000.0,
    })

    # Test 1 — should open a LONG trade
    log.info("\n--- Test 1: Should open LONG ---")
    result = strategy.evaluate(bar_1h, bar_5m_long, current_price=45000.0)
    log.info("Result: %s", result)
    strategy.print_status()

    # Test 2 — price moves up, trailing stop should move up
    log.info("\n--- Test 2: Price rises to £46,000 ---")
    result = strategy.evaluate(bar_1h, bar_5m_long, current_price=46000.0)
    log.info("Result: %s", result)
    log.info("Stop loss is now: £%.2f", strategy.current_trade.stop_loss)

    # Test 3 — price hits take profit at 4% above entry (45000 * 1.04 = 46800)
    log.info("\n--- Test 3: Price hits take profit at £46,800 ---")
    result = strategy.evaluate(bar_1h, bar_5m_long, current_price=46800.0)
    log.info("Result: %s", result)
    strategy.print_status()

    log.info("Strategy self-test complete.")
