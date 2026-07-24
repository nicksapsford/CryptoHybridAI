# CryptoTrader A.I. — Albion Trading Desk
**Version:** 1.5.2 | **Port:** 5001 | **Status:** Paper Trading

Part of the Albion Trading Desk — a multi-system AI paper trading operation built by Nick, running on a dedicated Dell Optiplex (Windows 11 Pro).

**Market:** BTC & ETH — Kraken Exchange
**Broker:** Kraken (paper trading)
**Theme:** Teal

CryptoTrader A.I. trades BTC/GBP and ETH/GBP on Kraken in paper-trading mode. It uses a multi-timeframe (daily / 1h / 5m) confluence system with whale/liquidation intelligence, and only enters when Arthur (the Claude AI brain) confirms a setup that has passed Lancelot's hard pre-checks. Dashboard on http://localhost:5001.

## The Team (Arthurian Naming)
| Role | Name | Function |
|------|------|----------|
| AI Brain | Arthur | Claude AI decision engine |
| Data Feed | Merlin | Market data and indicators |
| Pre-checks | Lancelot | Entry validation |
| Broker | Excalibur | Kraken connector |
| Calendar | Guinevere | Economic calendar filter |
| Performance | Morgan | P&L tracker + confidence |
| Watchdog | Galahad | Auto-restart |
| Notifier | Percival | Pushover alerts |
| Trader | Stanley | Paper trade execution |

## Phantom P&L Tracker
Records every STAY OUT decision with hindsight scoring. Feeds the STAY OUT QUALITY panel and Morgan's confidence. Data saved to: logs/phantom_trades.csv
