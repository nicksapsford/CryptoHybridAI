"""
CryptoHybrid AI -- paper_trader_eth.py
The accountant for ETH/GBP -- records every trade, tracks capital, saves logs.
Operates independently from BTC with its own GBP 1,000 capital pool.
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import pandas as pd

from strategy_eth import ETHStrategy, Trade

log = logging.getLogger("CryptoHybrid.PaperTrader.ETH")

BASE_DIR = Path(__file__).resolve().parent

STARTING_CAPITAL_GBP = 1_000.0
LOG_DIR              = BASE_DIR / "logs"
ETH_TRADES_LOG       = LOG_DIR / "eth_trades.csv"
ETH_SUMMARY_LOG      = LOG_DIR / "eth_summary.txt"

CSV_HEADERS = [
    "date", "time", "direction",
    "entry_price", "exit_price",
    "position_size_gbp", "pnl_gbp", "pnl_pct",
    "exit_reason", "capital_after",
    "entry_time", "exit_time",
    "mae_pts", "mae_gbp", "mfe_pts", "mfe_gbp",
]


class PaperTraderETH:

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not ETH_TRADES_LOG.exists():
            self._init_csv()
            log.info("Created new ETH trades log: %s", ETH_TRADES_LOG)
        else:
            log.info("Using existing ETH trades log: %s", ETH_TRADES_LOG)
        self._migrate_csv(ETH_TRADES_LOG)
        self.strategy = ETHStrategy(capital_gbp=STARTING_CAPITAL_GBP)
        previous_capital = self._load_last_capital()
        if previous_capital:
            self.strategy.capital_gbp = previous_capital
            log.info("ETH resumed from previous session | capital=GBP %.2f", previous_capital)
        else:
            log.info("ETH starting fresh | capital=GBP %.2f", STARTING_CAPITAL_GBP)
        log.info("CryptoHybrid AI | ETH PaperTrader ready")

    def _init_csv(self) -> None:
        with open(ETH_TRADES_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()

    def _load_last_capital(self) -> Optional[float]:
        if not ETH_TRADES_LOG.exists():
            return None
        try:
            df = pd.read_csv(ETH_TRADES_LOG)
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
            "entry_price":        f"{trade.entry_price:.4f}",
            "exit_price":         f"{trade.exit_price:.4f}",
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
        with open(ETH_TRADES_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writerow(row)
        log.info("[ETH] Trade saved to log: %s", ETH_TRADES_LOG)

    def _save_summary(self) -> None:
        s = self.strategy
        lines = [
            "=" * 50,
            "CryptoHybrid AI -- ETH/GBP Trading Summary",
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
                    f"entry=GBP {t.entry_price:.4f} "
                    f"exit=GBP {t.exit_price:.4f} "
                    f"P&L=GBP {t.pnl_gbp:+.2f} "
                    f"({t.pnl_pct*100:+.2f}%) "
                    f"reason={t.exit_reason}"
                )
        lines.append("=" * 50)
        with open(ETH_SUMMARY_LOG, "w", encoding="utf-8") as f:
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
                "[ETH] [%s] TRADE COMPLETE | %s | P&L=GBP %+.2f | Capital=GBP %.2f",
                result, last_trade.direction, pnl, self.strategy.capital_gbp,
            )
        if action in ("LONG_OPEN", "SHORT_OPEN"):
            trade = self.strategy.current_trade
            log.info(
                "[ETH] [OPEN] TRADE OPENED | %s | entry=GBP %.4f | "
                "stop=GBP %.4f | target=GBP %.4f | size=GBP %.2f",
                trade.direction, trade.entry_price,
                trade.stop_loss, trade.take_profit, trade.position_size_gbp,
            )
        return action

    def print_status(self) -> None:
        s = self.strategy
        log.info("=" * 60)
        log.info("CryptoHybrid AI -- ETH Paper Trader Status")
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
                "  Open trade:  [OPEN %s] entry=GBP %.4f stop=GBP %.4f "
                "target=GBP %.4f size=GBP %.2f",
                trade.direction, trade.entry_price,
                trade.stop_loss, trade.take_profit, trade.position_size_gbp,
            )
        else:
            log.info("  Open trade:  None -- watching for ETH signal...")
        if s.trade_history:
            log.info("  Recent trades:")
            for t in s.trade_history[-3:]:
                result = "WIN " if t.pnl_gbp >= 0 else "LOSS"
                log.info(
                    "    [%s %s] entry=GBP %.4f exit=GBP %.4f "
                    "P&L=GBP %+.2f (%+.2f%%) reason=%s",
                    result, t.direction,
                    t.entry_price, t.exit_price,
                    t.pnl_gbp, t.pnl_pct * 100,
                    t.exit_reason,
                )
        log.info("  Trade log:   %s", ETH_TRADES_LOG)
        log.info("  Summary:     %s", ETH_SUMMARY_LOG)
        log.info("=" * 60)
