"""
CryptoHybrid AI -- paper_trader_btc.py
The accountant -- records every trade, tracks capital, saves logs.
Works with strategy_btc.py and data_feed_btc.py
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import pandas as pd

from strategy_btc import BTCStrategy, Trade

log = logging.getLogger("CryptoHybrid.PaperTrader")

BASE_DIR = Path(__file__).resolve().parent

STARTING_CAPITAL_GBP = 1_000.0
LOG_DIR              = BASE_DIR / "logs"
TRADES_LOG           = LOG_DIR / "trades.csv"
SUMMARY_LOG          = LOG_DIR / "summary.txt"

CSV_HEADERS = [
    "date", "time", "direction",
    "entry_price", "exit_price",
    "position_size_gbp", "pnl_gbp", "pnl_pct",
    "exit_reason", "capital_after",
    "entry_time", "exit_time",
    "mae_pts", "mae_gbp", "mfe_pts", "mfe_gbp",
]


class PaperTrader:

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not TRADES_LOG.exists():
            self._init_csv()
            log.info("Created new trades log: %s", TRADES_LOG)
        else:
            log.info("Using existing trades log: %s", TRADES_LOG)
        self._migrate_csv(TRADES_LOG)
        self.strategy = BTCStrategy(capital_gbp=STARTING_CAPITAL_GBP)
        previous_capital = self._load_last_capital()
        if previous_capital:
            self.strategy.capital_gbp = previous_capital
            log.info("Resumed from previous session | capital=GBP %.2f", previous_capital)
        else:
            log.info("Starting fresh | capital=GBP %.2f", STARTING_CAPITAL_GBP)
        log.info("CryptoHybrid AI | PaperTrader ready")

    def _init_csv(self) -> None:
        with open(TRADES_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()

    def _load_last_capital(self) -> Optional[float]:
        if not TRADES_LOG.exists():
            return None
        try:
            df = pd.read_csv(TRADES_LOG)
            if df.empty:
                return None
            return float(df["capital_after"].iloc[-1])
        except Exception:
            return None

    def _migrate_csv(self, path) -> None:
        """One-time: if an existing trades.csv predates the MAE/MFE columns, rewrite it with
        the full header (old rows get blank MAE/MFE cells) so DictWriter stays aligned."""
        try:
            if not path.exists():
                return
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            if not rows:
                return
            header = rows[0]
            if all(h in header for h in CSV_HEADERS):
                return
            data = [dict(zip(header, r)) for r in rows[1:]]
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                w.writeheader()
                for d in data:
                    w.writerow({k: d.get(k, "") for k in CSV_HEADERS})
            log.info("Migrated trades log to MAE/MFE schema: %s", path)
        except Exception as e:
            log.warning("trades.csv migration skipped: %s", e)

    def _log_trade(self, trade: Trade) -> None:
        if trade.exit_price is None:
            return
        now = trade.exit_time or datetime.now(timezone.utc)
        row = {
            "date":               now.strftime("%Y-%m-%d"),
            "time":               now.strftime("%H:%M:%S"),
            "direction":          trade.direction,
            "entry_price":        f"{trade.entry_price:.2f}",
            "exit_price":         f"{trade.exit_price:.2f}",
            "position_size_gbp":  f"{trade.position_size_gbp:.2f}",
            "pnl_gbp":            f"{trade.pnl_gbp:+.2f}",
            "pnl_pct":            f"{trade.pnl_pct * 100:+.2f}",
            "exit_reason":        trade.exit_reason,
            "capital_after":      f"{self.strategy.capital_gbp:.2f}",
            "entry_time":         trade.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time":          now.strftime("%Y-%m-%d %H:%M:%S"),
            "mae_pts":            f"{trade.mae_pts:.2f}",
            "mae_gbp":            f"{trade.mae_gbp:.2f}",
            "mfe_pts":            f"{trade.mfe_pts:.2f}",
            "mfe_gbp":            f"{trade.mfe_gbp:.2f}",
        }
        with open(TRADES_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writerow(row)
        log.info("Trade saved to log: %s", TRADES_LOG)

    def _save_summary(self) -> None:
        s = self.strategy
        lines = [
            "=" * 50,
            "CryptoHybrid AI -- Trading Summary",
            "Generated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "=" * 50,
            f"Starting capital:  GBP {STARTING_CAPITAL_GBP:.2f}",
            f"Current capital:   GBP {s.capital_gbp:.2f}",
            f"Total P&L:         GBP {s.total_pnl:+.2f}",
            f"Total return:      {(s.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100:+.2f}%",
            "",
            f"Total trades:      {s.total_trades}",
            f"Winning trades:    {s.winning_trades}",
            f"Win rate:          {s.win_rate:.0f}%",
            "",
        ]
        if s.trade_history:
            lines.append("Recent trades:")
            lines.append("-" * 50)
            for t in s.trade_history[-10:]:
                result = "WIN " if t.pnl_gbp >= 0 else "LOSS"
                lines.append(
                    f"  [{result} {t.direction}] "
                    f"entry=GBP {t.entry_price:.2f} "
                    f"exit=GBP {t.exit_price:.2f} "
                    f"P&L=GBP {t.pnl_gbp:+.2f} "
                    f"({t.pnl_pct*100:+.2f}%) "
                    f"reason={t.exit_reason}"
                )
        lines.append("=" * 50)
        with open(SUMMARY_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def update(self, bar_1h: pd.Series, bar_5m: pd.Series, current_price: float) -> str:
        trades_before = self.strategy.total_trades
        action = self.strategy.evaluate(bar_1h, bar_5m, current_price)
        trades_after = self.strategy.total_trades
        if trades_after > trades_before:
            last_trade = self.strategy.trade_history[-1]
            self._log_trade(last_trade)
            self._save_summary()
            pnl    = last_trade.pnl_gbp
            result = "PROFIT" if pnl >= 0 else "LOSS"
            log.info(
                "[%s] TRADE COMPLETE | %s | P&L=GBP %+.2f | Capital=GBP %.2f",
                result, last_trade.direction, pnl, self.strategy.capital_gbp,
            )
        if action in ("LONG_OPEN", "SHORT_OPEN"):
            trade = self.strategy.current_trade
            log.info(
                "[OPEN] TRADE OPENED | %s | entry=GBP %.2f | "
                "stop=GBP %.2f | target=GBP %.2f | size=GBP %.2f",
                trade.direction, trade.entry_price,
                trade.stop_loss, trade.take_profit, trade.position_size_gbp,
            )
        return action

    def print_status(self) -> None:
        s = self.strategy
        log.info("=" * 60)
        log.info("CryptoHybrid AI -- Paper Trader Status")
        log.info("-" * 60)
        log.info("  Starting capital:  GBP %.2f", STARTING_CAPITAL_GBP)
        log.info("  Current capital:   GBP %.2f", s.capital_gbp)
        log.info("  Total P&L:         GBP %+.2f", s.total_pnl)
        log.info("  Total return:      %+.2f%%",
                 (s.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100)
        log.info("-" * 60)
        log.info("  Total trades:      %d", s.total_trades)
        log.info("  Winning trades:    %d", s.winning_trades)
        log.info("  Win rate:          %.0f%%", s.win_rate)
        log.info("-" * 60)
        if s.in_trade:
            trade = s.current_trade
            log.info(
                "  Open trade:  [OPEN %s] entry=GBP %.2f stop=GBP %.2f "
                "target=GBP %.2f size=GBP %.2f",
                trade.direction, trade.entry_price,
                trade.stop_loss, trade.take_profit, trade.position_size_gbp,
            )
        else:
            log.info("  Open trade:  None -- watching for signal...")
        if s.trade_history:
            log.info("  Recent trades:")
            for t in s.trade_history[-3:]:
                result = "WIN " if t.pnl_gbp >= 0 else "LOSS"
                log.info(
                    "    [%s %s] entry=GBP %.2f exit=GBP %.2f "
                    "P&L=GBP %+.2f (%+.2f%%) reason=%s",
                    result, t.direction,
                    t.entry_price, t.exit_price,
                    t.pnl_gbp, t.pnl_pct * 100,
                    t.exit_reason,
                )
        log.info("  Trade log:   %s", TRADES_LOG)
        log.info("  Summary:     %s", SUMMARY_LOG)
        log.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("CryptoHybrid AI -- PaperTrader self-test starting...")
    trader = PaperTrader()
    bar_1h = pd.Series({
        "ssl_bull":       True,
        "rsi":            62.0,
        "macd_histogram": 120.0,
        "tmo_main":       2.1,
        "tmo_smooth":     1.5,
        "chande_mo":      45.0,
        "money_flow":     8000.0,
    })
    bar_5m = pd.Series({
        "ssl_bull":       True,
        "rsi":            58.0,
        "macd_histogram": 50.0,
        "tmo_main":       1.2,
        "tmo_smooth":     0.8,
        "chande_mo":      30.0,
        "money_flow":     5000.0,
    })
    log.info("--- Test 1: Open a LONG trade ---")
    trader.update(bar_1h, bar_5m, current_price=45000.0)
    trader.print_status()
    log.info("--- Test 2: Price rises ---")
    trader.update(bar_1h, bar_5m, current_price=46000.0)
    log.info("--- Test 3: Price hits take profit ---")
    trader.update(bar_1h, bar_5m, current_price=46800.0)
    trader.print_status()
    log.info("Check your logs folder for trades.csv and summary.txt!")
    log.info("CryptoHybrid AI -- PaperTrader self-test complete.")