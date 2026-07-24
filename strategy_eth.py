"""
CryptoHybrid AI -- strategy_eth.py
Conservative dual-timeframe strategy for ETH/GBP
- 1h chart sets the trend direction
- 5m chart triggers entry when 5 of 6 indicators agree
- 30% position size, 2.5% trailing stop (wider than BTC for ETH volatility), 10% take profit
- Trades both LONG and SHORT (LONG gated by pre_checks_eth.ETH_SHORT_ONLY_MODE)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import pandas as pd

log = logging.getLogger("CryptoHybrid.Strategy.ETH")

# ──────────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────────

POSITION_SIZE_PCT   = 0.30   # 30% of current capital per trade
TRAILING_STOP_PCT   = 0.01   # 1% trailing stop (momentum scalping -- backtest winner)
TAKE_PROFIT_PCT     = 0.02   # 2% take profit (scalping target -- quick in, quick out)
SPREAD_PCT          = 0.0002 # 0.02% Kraken bid/ask -- modelled on entry fills (Stanley)
MIN_INDICATORS      = 5      # minimum indicators that must agree (out of 6)
MIN_1H_INDICATORS   = 4      # minimum 1h indicators for trend confirmation


# ──────────────────────────────────────────────────────────────────────────────
# Trade record
# ──────────────────────────────────────────────────────────────────────────────

# Profit Protection Ladder (Variant 2) -- recalibrated for 1% stop scalping
# (18 Jul 2026, System 2 Review). Mirrors strategy_btc.py; previously absent on
# ETH (main called apply_profit_ladder on a method that did not exist).
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
    direction: str
    entry_price: float
    position_size_gbp: float
    entry_time: datetime

    highest_price: float = field(init=False)
    lowest_price: float  = field(init=False)
    stop_loss: float     = field(init=False)
    take_profit: float   = field(init=False)

    exit_price: Optional[float]    = field(default=None)
    exit_time: Optional[datetime]  = field(default=None)
    exit_reason: Optional[str]     = field(default=None)
    pnl_gbp: Optional[float]       = field(default=None)
    pnl_pct: Optional[float]       = field(default=None)

    def __post_init__(self):
        self.highest_price = self.entry_price
        self.lowest_price  = self.entry_price

        if self.direction == "LONG":
            self.stop_loss   = self.entry_price * (1 - TRAILING_STOP_PCT)
            self.take_profit = self.entry_price * (1 + TAKE_PROFIT_PCT)
        else:
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
            log.info("  [ETH] PROFIT LADDER step %d: float GBP %.2f -> floor GBP %.2f | "
                     "stop %.4f -> %.4f", idx, float_gbp, self.ladder_floor_gbp,
                     stop_before, self.stop_loss)
            return {"step": idx, "floor_gbp": self.ladder_floor_gbp,
                    "trigger_float_gbp": round(float_gbp, 2),
                    "stop_before": round(stop_before, 4), "stop_after": round(self.stop_loss, 4)}
        return None

    def update_trailing_stop(self, current_price: float) -> None:
        if self.direction == "LONG":
            if current_price > self.highest_price:
                self.highest_price = current_price
                new_stop = current_price * (1 - TRAILING_STOP_PCT)
                if new_stop > self.stop_loss:
                    self.stop_loss = new_stop
                    log.info(
                        "  [ETH] Trailing stop moved UP to GBP %.4f (price=GBP %.4f)",
                        self.stop_loss, current_price
                    )
        else:
            if current_price < self.lowest_price:
                self.lowest_price = current_price
                new_stop = current_price * (1 + TRAILING_STOP_PCT)
                if new_stop < self.stop_loss:
                    self.stop_loss = new_stop
                    log.info(
                        "  [ETH] Trailing stop moved DOWN to GBP %.4f (price=GBP %.4f)",
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
        if self.direction == "LONG":
            if current_price <= self.stop_loss:
                return "STOP_LOSS"
            if current_price >= self.take_profit:
                return "TAKE_PROFIT"
        else:
            if current_price >= self.stop_loss:
                return "STOP_LOSS"
            if current_price <= self.take_profit:
                return "TAKE_PROFIT"
        return None

    def close(self, exit_price: float, reason: str) -> None:
        self.exit_price  = exit_price
        self.exit_time   = datetime.now(timezone.utc)
        self.exit_reason = reason

        if self.direction == "LONG":
            self.pnl_pct = (exit_price - self.entry_price) / self.entry_price
        else:
            self.pnl_pct = (self.entry_price - exit_price) / self.entry_price

        self.pnl_gbp = self.position_size_gbp * self.pnl_pct

    def summary(self) -> str:
        if self.exit_price is None:
            return (
                f"[OPEN {self.direction}] entry=GBP {self.entry_price:.4f} "
                f"stop=GBP {self.stop_loss:.4f} target=GBP {self.take_profit:.4f} "
                f"size=GBP {self.position_size_gbp:.2f}"
            )
        else:
            result = "WIN" if self.pnl_gbp >= 0 else "LOSS"
            return (
                f"[{result} {self.direction}] "
                f"entry=GBP {self.entry_price:.4f} exit=GBP {self.exit_price:.4f} "
                f"P&L=GBP {self.pnl_gbp:+.2f} ({self.pnl_pct*100:+.2f}%) "
                f"reason={self.exit_reason}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Indicator scoring
# ──────────────────────────────────────────────────────────────────────────────

def _score_bar(bar: pd.Series) -> dict:
    scores = {}

    ssl_bull = bar.get("ssl_bull")
    if pd.notna(ssl_bull):
        scores["ssl"] = 1 if ssl_bull else -1

    rsi = bar.get("rsi")
    if pd.notna(rsi):
        if rsi > 55:
            scores["rsi"] = 1
        elif rsi < 45:
            scores["rsi"] = -1
        else:
            scores["rsi"] = 0

    hist = bar.get("macd_histogram")
    if pd.notna(hist):
        scores["macd"] = 1 if hist > 0 else -1

    tmo_main   = bar.get("tmo_main")
    tmo_smooth = bar.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        scores["tmo"] = 1 if tmo_main > tmo_smooth else -1

    cmo = bar.get("chande_mo")
    if pd.notna(cmo):
        scores["chande"] = 1 if cmo > 0 else -1

    mf = bar.get("money_flow")
    if pd.notna(mf):
        scores["money_flow"] = 1 if mf > 0 else -1

    long_count  = sum(1 for v in scores.values() if v == 1)
    short_count = sum(1 for v in scores.values() if v == -1)

    return {
        "scores":      scores,
        "long_count":  long_count,
        "short_count": short_count,
        "total":       len(scores),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main ETHStrategy class
# ──────────────────────────────────────────────────────────────────────────────

class ETHStrategy:
    """
    CryptoHybrid AI -- Conservative ETH/GBP Strategy
    Identical logic to BTC but with a 2.5% trailing stop to accommodate ETH volatility.
    """

    def __init__(self, capital_gbp: float = 1000.0) -> None:
        self.capital_gbp   = capital_gbp
        self.current_trade: Optional[Trade] = None
        self.trade_history: list[Trade]     = []

        log.info(
            "CryptoHybrid AI | ETH Strategy ready | capital=GBP %.2f | "
            "position=%.0f%% | trailing_stop=%.1f%% | take_profit=%.0f%%",
            self.capital_gbp,
            POSITION_SIZE_PCT * 100,
            TRAILING_STOP_PCT * 100,
            TAKE_PROFIT_PCT   * 100,
        )

    @property
    def in_trade(self) -> bool:
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

    def _get_1h_trend(self, bar_1h: pd.Series) -> str:
        result = _score_bar(bar_1h)
        if result["long_count"] >= MIN_1H_INDICATORS:
            return "LONG"
        elif result["short_count"] >= MIN_1H_INDICATORS:
            return "SHORT"
        else:
            return "NEUTRAL"

    def _get_5m_signal(self, bar_5m: pd.Series) -> str:
        result = _score_bar(bar_5m)

        log.info(
            "  [ETH] 5m indicators: LONG=%d SHORT=%d | scores=%s",
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
        # Spread modelling (Stanley): fill on the adverse side of the Kraken
        # bid/ask -- LONG buys the ask, SHORT sells the bid. Stop/target derive
        # from the real fill, so the spread is a genuine (previously unmodelled)
        # entry cost.
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
            "[ETH] >>> TRADE OPENED | %s | mid=GBP %.4f fill=GBP %.4f (spread %.2f%%) | "
            "size=GBP %.2f | stop=GBP %.4f | target=GBP %.4f",
            direction,
            price,
            fill_price,
            SPREAD_PCT * 100,
            position_size,
            self.current_trade.stop_loss,
            self.current_trade.take_profit,
        )

    def _close_trade(self, price: float, reason: str) -> None:
        if not self.current_trade:
            return

        self.current_trade.close(exit_price=price, reason=reason)
        pnl = self.current_trade.pnl_gbp

        self.capital_gbp += pnl

        log.info(
            "[ETH] <<< TRADE CLOSED | %s | P&L=GBP %+.2f | capital=GBP %.2f | reason=%s",
            self.current_trade.direction,
            pnl,
            self.capital_gbp,
            reason,
        )

        self.trade_history.append(self.current_trade)
        self.current_trade = None

    def evaluate(
        self,
        bar_1h: pd.Series,
        bar_5m: pd.Series,
        current_price: float,
    ) -> str:
        log.info(
            "[ETH] === Strategy evaluate | price=GBP %.4f | in_trade=%s ===",
            current_price, self.in_trade
        )

        if self.in_trade:
            trade = self.current_trade
            trade.update_trailing_stop(current_price)
            trade.update_excursions(current_price)   # MAE/MFE (Commission 009)
            exit_reason = trade.check_exit(current_price)
            if exit_reason:
                self._close_trade(current_price, exit_reason)
                return "CLOSE"

            trend_1h = self._get_1h_trend(bar_1h)
            if trade.direction == "LONG" and trend_1h == "SHORT":
                log.info("  [ETH] 1h trend reversed to SHORT -- closing LONG trade")
                self._close_trade(current_price, "TREND_REVERSAL")
                return "CLOSE"
            elif trade.direction == "SHORT" and trend_1h == "LONG":
                log.info("  [ETH] 1h trend reversed to LONG -- closing SHORT trade")
                self._close_trade(current_price, "TREND_REVERSAL")
                return "CLOSE"

            log.info("  [ETH] Trade running | %s", trade.summary())
            return "HOLD"

        trend_1h = self._get_1h_trend(bar_1h)
        log.info("  [ETH] 1h trend: %s", trend_1h)

        if trend_1h == "NEUTRAL":
            log.info("  [ETH] 1h trend neutral -- no trade")
            return "HOLD"

        signal_5m = self._get_5m_signal(bar_5m)

        if trend_1h == "LONG" and signal_5m == "LONG":
            self._open_trade("LONG", current_price)
            return "LONG_OPEN"
        elif trend_1h == "SHORT" and signal_5m == "SHORT":
            self._open_trade("SHORT", current_price)
            return "SHORT_OPEN"
        else:
            log.info(
                "  [ETH] No entry -- 1h=%s but 5m=%s (waiting for alignment)",
                trend_1h, signal_5m
            )
            return "HOLD"

    def print_status(self) -> None:
        log.info("=" * 60)
        log.info("CryptoHybrid AI -- ETH Strategy Status")
        log.info("  Capital:      GBP %.2f", self.capital_gbp)
        log.info("  Total P&L:    GBP %+.2f", self.total_pnl)
        log.info("  Trades:       %d total | %d wins | %.0f%% win rate",
                 self.total_trades, self.winning_trades, self.win_rate)
        if self.in_trade:
            log.info("  Open trade:   %s", self.current_trade.summary())
        else:
            log.info("  Open trade:   None")
        if self.trade_history:
            log.info("  Recent trades:")
            for t in self.trade_history[-5:]:
                log.info("    %s", t.summary())
        log.info("=" * 60)
