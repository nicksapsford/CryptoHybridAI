## [1.1.1] - 2026-07-27  —  ETH volatility floor matched to BTC (Nick's decision)
### Changed
- `ETH_VOLATILITY_FLOOR_PCT` 0.125 → **0.10** to match `BTC_VOLATILITY_FLOOR_PCT`. The gate reads
  the SHARED BTC 5m ATR for both engines, so equal % = equal selectivity; a higher ETH floor
  would make ETH strictly more selective than BTC on the identical signal. ETH floor now £48.85
  = BTC floor at live price. COMMISSION_016_REPORT.md correction note updated to record this.

## [1.1.0] - 2026-07-27  —  Commission 016: BTC-ATR volatility floor/ceiling auto-scaling
### Changed — volatility-range gate thresholds are now a % of BTC price (was fixed GBP)
- Gaius Commission 016 (Nick-approved) — this is the hybrid where evidence-based changes land.
  The shared BTC-ATR volatility floor/ceiling (`ATR_VOL_FLOOR_GBP=50`, `ATR_VOL_CEILING_GBP=800`)
  are now computed per bar as a **percentage of the current BTC price** so they auto-scale:
  `atr_floor_gbp = btc_price * FLOOR_PCT/100`, `atr_ceiling_gbp = btc_price * CEILING_PCT/100`.
- New constants: `BTC_VOLATILITY_FLOOR_PCT=0.10`, `ETH_VOLATILITY_FLOOR_PCT=0.125`, shared
  `VOLATILITY_CEILING_PCT=1.65`. Gate still reads the SHARED BTC 5m ATR for both engines.
- `regime.py`: new `set_btc_price()`/`get_btc_price()`; `check_volatility_range()` +
  `run_all_pre_checks()` gained a `btc_price` param (legacy-GBP fallback when price unavailable).
- Behaviour-neutral at today's price (BTC floor £48.85 vs old £50, ceiling £806 vs £800; ETH
  floor £61.06 vs £50). At the live 5m ATR (£47.1) both gates classify identically.
- CORRECTION appended to COMMISSION_015_REPORT.md and COMMISSION_016_REPORT.md: the
  "ETH blocked 100%" finding was a HARNESS artifact (backtest applied the floor to ETH's OWN
  ATR); production reads the shared BTC ATR for both engines, so ETH was never blocked live.

## [1.9.0] - 2026-07-23  —  Ladder rescale + MAE/MFE logging (Commission 009)
### Fixed — Profit Protection Ladder rescaled for the scalping regime
- Gaius Commission 009 found the ladder had been INACTIVE since the 18 Jul scalping
  rebuild: a 2%-target trade on a ~£300 position floats ~£6 max, but the ladder's first
  step triggered at £15 (trend-era values) so it could never fire. Rescaled BTC + ETH to
  **£2→£1.50, £3.50→£3, £5→£4.50** (~33/58/83% of the ~£6 target, £0.50 buffers).
### Added — per-trade MAE/MFE logging (Stanley, pilot)
- `strategy_btc.py`/`strategy_eth.py`: `Trade.update_excursions(price)` tracks peak
  favourable (MFE) and worst adverse (MAE) excursion each monitor tick (as a fraction of
  entry); `mae_pts/mae_gbp/mfe_pts/mfe_gbp` properties. Analysis only — never affects
  stops/exits/ladder.
- `paper_trader_btc.py`/`paper_trader_eth.py`: new `mae_pts, mae_gbp, mfe_pts, mfe_gbp`
  columns in trades.csv + a one-time `_migrate_csv()` that back-fills the header on
  existing logs (old rows get blank MAE/MFE).
- `dashboard_btc.py`: MAE + MFE columns added to the BTC and ETH trade-history tables on
  the P&L page.
- Wired into the live monitor (`main_tidetrader.py` BTC + ETH legs) and the strategy monitor.

## [1.7.5] - 2026-07-20
### Added -- Snag 19: recent phantom rows in the Archie Brief
- The Archie Brief now lists the **last 5 phantom rows** (newest first) directly under
  the STAY OUT QUALITY summary, so Archie sees overnight phantom activity inline without
  a separate PHANTOM-page screenshot. Columns: Date/Time (UTC) | Market, Direction, Confidence,
  1hr Move, Verdict. PENDING rows shown as PENDING; empty -> "No phantom data yet".
  Display only -- reads the same stay_out_quality decisions; no logic/threshold change.
- Also folds in the previously-unstaged Guinevere Part-2 tweak to `_news_line()`
  (skip headlines with |score| < 1; empty message -> "No significant headlines in
  current period") -- brings this repo's archie_brief.py up to the desk standard.

## [1.7.4] - 2026-07-19
### Changed -- BTC phantom verdict threshold 40 -> 14 (scalping recalibration)
- `VERDICT_THRESHOLD['BTC']` 40.0 -> **14.0** (GBP) in `phantom_tracker.py`. The 18 Jul
  momentum-scalping rebuild produces much smaller BTC 1hr moves than the old LONG_ONLY
  trend strategy (new-regime median |1hr move| £13.90 vs £73.20 old). 40 was calibrated on
  the old-strategy-dominated blend (median £38.40) and under-classified the new regime
  (~23%% on the 18 Jul+ rows). 14 (~= the new-regime median) restores the desk-standard
  ~50%% classification: on the 26 new-regime BTC rows it scores CORRECT 4 / WRONG 9 /
  NEUTRAL 13 = 50.0%% (was 23.1%%). ETH stays 4 -- only 1 new-regime ETH row; recalibrate
  when 30+ exist (Gaius monthly check).
- Verdict/analytics only -- no trading-behaviour change; phantom stays Arthur-STAY_OUT only.
- Data (gitignored): pre-18 Jul phantom rows (112) archived to
  `logs/phantom_pre_scalping_archive.csv`; live `logs/phantom_trades.csv` retains the 27
  rows from 18 Jul onward so the median/PHANTOM page reflect only the scalping strategy.

## [1.7.3] - 2026-07-19
### Added -- Job 2: dedicated Phantom Trades page
- New **PHANTOM &rarr;** header button (same pattern as P&L &rarr;) opens a dedicated
  page 3: "PHANTOM TRADES -- Stay Out Quality" with a summary (Quality %, Correct/Wrong/
  Neutral, Net Saved/Missed) and a clean last-20 table (newest first):
  Date/Time UTC | Market (BTC/ETH) | Direction | Entry Price | Confidence | 1hr Move |
  Verdict (colour-coded CORRECT/WRONG/NEUTRAL/PENDING). Back to Dashboard + Trading nav.
- The right-panel Stay Out Quality card is now a **compact** summary that opens the full
  page on click -- fixes off-screen rows, cramped layout, and the missing market column
  (old code read r.symbol/r.pair; the CSV column is `market`). Full timestamps now shown.
- Display only -- reads the same get_stay_out_quality() decisions; no data/logic change.

## [1.7.2] - 2026-07-19
### Fixed -- Job 1: phantom indicator snapshot now populates (BTC & ETH)
- CryptoTrader's phantom rows had all 17 indicator columns blank. `main_tidetrader`
  passed raw pandas Series (from `latest_bar()`) to `phantom_tracker.build_snapshot()`,
  which reads via `isinstance(dict)` + `ind.get()` and whose `_ssl()` does `if not ind:`
  -- a Series raises "truth value is ambiguous", so the whole snapshot threw and was
  dropped (`_snap=None`), blanking every column.
- New `_bar_to_ind(bar)` converts each bar to a plain indicator dict (same pattern as
  GoldTrader/OilTrader `_indicator_snapshot`) before `build_snapshot`, for BOTH the BTC
  and ETH ticks -- each row captures its own instrument's indicators. Populates ssl_/rsi_/
  tmo_/macd_/chande_mo_/money_flow_ (1d/1h/5m), morgan_score and session (UTC sub-session);
  guinevere_score stays blank (no Guinevere module). Existing blank rows left as-is.
- Display/data-logging only -- no trading-logic change; phantom stays Arthur-STAY_OUT only.

## [1.7.1] - 2026-07-18
### Changed -- phantom verdict threshold (System 5 Review desk-wide, Rec 1 pattern)
- **`VERDICT_THRESHOLD` now PER-MARKET: BTC 40 / ETH 4** (GBP, 1hr window) -- a code change
  (`_calculate_verdict` gained a `market` arg + `_threshold_for` helper). A single flat 10
  could not serve BTC (~£47k, median |1hr| ~£38) and ETH (~£1.3k, median ~£3.8): it was too
  LOW for BTC (~74%% classified = noise) and too HIGH for ETH (~23%%). Re-scored 139 rows per
  instrument: BTC 47.1%% classified (16C/17W/37N), ETH 49.3%% (20C/14W/35N). Requires a restart
  to load the per-market logic. Data re-score is in logs/ (gitignored).
  No trading-rule change; no backtest required. Verdict threshold mis-scaling was assessed
  desk-wide on 18 Jul; see the OilTrader v1.1.18 fix that established this pattern.

## [1.7.0] - 2026-07-18
### Changed -- CryptoTrader System 2 Review: full recalibration to momentum scalping
Backtest-provisional; review after 2 weeks. Nick sign-off confirmed. Applies to
both BTC and ETH. Direction is now the 1h SSL (primary), regime-aware, bidirectional.

- **Risk params (Change 4):** trailing stop 2%/2.5% -> **1%**, take profit 10% -> **2%**
  (scalping). Position size unchanged (30%). Added **spread modelling** to Stanley:
  `SPREAD_PCT = 0.0002` applied to the entry fill (LONG buys the ask, SHORT sells the
  bid) in `_open_trade` -- previously the paper trader modelled no spread.
- **Profit ladder (Change 5):** recalibrated to 15/12, 35/30, 60/52. **Added the ladder
  to ETH** (`strategy_eth.py`) -- it was previously absent, so `main` called
  `apply_profit_ladder` on a method that did not exist (latent AttributeError, never
  fired because ETH had no trades). Now present and mirrored from BTC.
- **Bidirectional + regime (Change 1/2):** direction driven by the **1h SSL** (daily SSL
  is context only). New `regime.py` reads Gaius `market_context.json` (BTC 200MA +
  Fear & Greed), 30-min cache, BTC data used for both BTC and ETH. Bull = BTC above
  200MA AND F&G >= 40; else Bear. Regime awareness is passed **into Arthur's prompt**
  (Arthur reasons about it -- no hard pre-Arthur gate; Arthur is consulted whenever
  Lancelot passes, preserving the phantom-tracker invariant). SHORTs require a BEAR
  regime AND **Morgan SHORT confidence >= 65**, enforced as a post-Arthur backstop.
- **Morgan SHORT confidence (Change 4/7):** new **separate** SHORT confidence
  (`performance_btc.get/set_short_confidence`, `logs/morgan_short_confidence.json`),
  **initialised at 30** so SHORTs stay blocked until evidence builds it to 65. The
  general Morgan confidence is left untouched (Change 7 -- no reset).
- **Lancelot loosened (Change 3):** `MIN_TMO_FOR_ENTRY` 0.3 -> **0.21** (~30%, sharper
  crypto bursts). **Daily-SSL direction gates removed** from the chain (BTC daily trend
  filter; ETH daily trend filter + `check_eth_short_only_mode`, now `ETH_SHORT_ONLY_MODE
  = False`) -- direction is the 1h+5m SSL agreement. **Volatility-range gate (Change 3B)**
  replaces the choppy-market check: shared **BTC 5m ATR-14 (GBP)**, block below **50**
  (flat) or above **800** (extreme). ATR-14 added to `data_feed_btc.add_indicators`;
  the BTC engine publishes the ATR each tick (`regime.set_btc_atr`) and both engines gate
  on it. Backtest-provisional (50/800); review after 2 weeks.
- **Arthur prompt rewrite (Change 6):** both brains rewritten for momentum scalping --
  philosophy, 1h-SSL primary, regime awareness, session bands (NY/Asian/London), the
  percentage convention (1% stop / 2% target, never fixed points), profit-ladder status,
  and SHORT gating. Live regime/session/Morgan-SHORT values injected per tick via the
  user message; `get_trading_decision` gained `regime` + `morgan_short` params.

## [1.6.7] - 2026-07-16
### Fixed
- Snag 9: confidence bar could display 50 when the real Morgan score was 0. The
  dashboard read `perf.confidence_score || 50`, and JS treats 0 as falsy, so a
  legitimate 0 was replaced by the 50 fallback. Changed to
  `(perf.confidence_score != null ? perf.confidence_score : 50)` -- 0 now shows as
  0; 50 is used only when the value is genuinely absent. In practice only GasTrader
  showed the wrong value (the only system with a 0 score, from a 5-loss streak); the
  latent bug was in all 6 dashboards. RoundTable was already correct.

## [1.6.6] - 2026-07-16
### Added
- Job 1 (Gaius Commission 001, Priority 1): indicator snapshot at signal time in
  phantom_trades.csv. 17 columns APPENDED to the right of the existing 14-col schema
  (existing positions unchanged): ssl_daily/1hr/5min, rsi_daily/1hr/5min,
  tmo_1hr/5min, macd_1hr/5min, chande_mo_1hr/5min, money_flow_1hr/5min, morgan_score,
  session, guinevere_score. Captured from values Merlin already fetched for Arthur
  (no new data fetch) via phantom_tracker.build_snapshot() -> record_decision(indicators=).
  The snapshot build is wrapped in its own try/except so a failure can never stop a
  phantom row being written. phantom_tracker now migrates an older 14-col file in place
  on first use (old rows keep positions; new columns blank). Chronicle & Gaius read by
  column name and are unaffected. (guinevere_score currently blank pending a safe cached
  source -- column reserved.)

# CryptoTrader AI -- Changelog

## [1.6.5] - 2026-07-13
### Fixed
- Bug C (desk-wide): "Locked P&L" now only shows once the trailing stop trails to break-even (genuine secured profit); until then "---" instead of an if-stopped loss figure.

## [1.6.4] - 2026-07-12
### Fixed
- Log timestamps now emitted in UTC (logging.Formatter.converter = time.gmtime; datefmt suffixed " UTC") across main, watchdog and dashboard. Previously local/BST, causing a +1h mismatch vs the UTC CSV artefacts (phantom_trades.csv etc.).
### Added
- ALBION STANDING RULE comment blocks baked into the logging setup and the log/analysis modules (phantom_tracker.py, performance_btc.py, dashboard stay-out reader): all timestamps are UTC, never BST/local.

## [1.6.3] - 2026-07-12
### Improved
- Pre-check results split into BTC | ETH columns
- Whale summary split into BTC | ETH columns
- Liquidation zone split into BTC | ETH columns
- Stay Out Quality increased from last 10 to last 20 decisions (dual-market system)

## [1.6.2] - 2026-07-11
### Added
- Silent launcher (pythonw -- no console windows); output to logs/console.log with daily rotation (7 days kept)
- Launcher now starts the dashboard + watchdog silently (was cmd windows)

## [1.6.1] - 2026-07-11
### Added
- Morgan confidence persistence to logs/morgan_confidence.csv (append-only
  timestamp/confidence/level/reason history via save_confidence)
- Confidence restored on restart: main loads the last CSV reading on startup
  and re-applies it via set_confidence(value, reason='restore')

## [1.6.0] - 2026-07-11
### Added
- Morgan individual phantom-verdict feedback: each judged STAY OUT verdict now
  nudges a persistent Morgan confidence (logs/morgan_confidence.json)
- CORRECT stay-out adds +0.5..+2.0, WRONG stay-out subtracts -0.5..-2.0, scaled
  by the size of the avoided/missed 1hr move (raw = clamp(|pnl_1hr|/50, 0.5, 2.0))
- NEUTRAL verdicts have no confidence impact; per-verdict adjustment capped at ±2.0
- morgan_processed field on phantom rows so each verdict is applied exactly once
- MorganPhantomPoller daemon thread (5-min interval) drains unprocessed verdicts
- Individual feedback layered onto the reported confidence as (get_confidence()-50);
  the aggregate STAY OUT circuit breaker (get_stay_out_adjustment) is unchanged
### Fixed
- ETH SHORT win rate reset from an unsourced 51% "profitable" figure to a neutral
  50% baseline (no provenance/backtest source; rebuilding on clean phantom data)

## [1.5.9] - 2026-07-11
### Fixed
- ETH LONG win rate reset from contaminated 29% to neutral 50% baseline
- Previous figure was artificially depressed by whale hunt false signals blocking Asian session LONGs
- Clean phantom tracker data now being collected to establish real ETH win rate
- Paper trading phase — safe to reset and learn

## [1.5.8] - 2026-07-11
### Added
- lancelot_status/lancelot_fails/lancelot_fail_reasons/arthur_decision/arthur_confidence/arthur_consulted/locked_pnl in /api/state
### Fixed
- compact Open Position panel layout

## [1.5.7] - 2026-07-10
### Temporary (paper trading experiment)
- Whale hunt veto SUSPENDED for BTC LONGs (neutralised in agent_brain_btc SYSTEM_PROMPT)
- Morgan -5/+5 penalty SUSPENDED (performance_btc.get_stay_out_adjustment returns 0.0)
- Confidence returns to 50/100 MEDIUM baseline (computed fresh each tick; no persistent store)
- Dashboard shows a visible "WHALE HUNT VETO: SUSPENDED" banner
- Collecting 48-72hr comparison data; hunt data collection continues unchanged
- All suspended logic preserved verbatim in comments for easy restoration
### Investigation finding
- Hunt model confirmed structurally inverted during uptrends: rising OI in an uptrend is trend participation, not a stop-hunt setup
- 10 Jul: 8+ profitable BTC LONGs blocked, ~£715.90 missed, Morgan -5 spiral engaged (quality 11.1%)
### TODO (weekend fix)
- Context-gate the hunt veto: only block when price is actually flushing downward
- Backtest the context-gated version before restoring the veto and the Morgan penalty

## [1.5.6] - 2026-07-09
### Added
- phantom_tracker.start_watchdog() — continuous daemon thread that runs resolve_stale_pending() every 15 min, so stale PENDING rows resolve dynamically without a restart. Idempotent (single thread per process). Started in main after startup resolution.

## [1.5.5] - 2026-07-09
### Fixed
- Morgan quality score now excludes NEUTRAL decisions from the denominator (only CORRECT/WRONG judged)
- Morgan penalty minimum raised from 5 to 8 judged decisions before firing
- CryptoTrader: Morgan now counts only ARTHUR_STAY_OUT rows (hard Lancelot blocks excluded via reason filter)
### Changed
- CryptoTrader: rising 1h TMO now allowed even if marginally negative (backtest-validated)

## [1.5.2] - 2026-07-08
### Changed
- Internal rebrand: TideTrader -> CryptoTrader across display text and logger names (repo/folder name, git remote and log files unchanged)
- README rewritten with Albion Trading Desk branding and team roster
### Fixed
- STAY OUT QUALITY panel now ignores PENDING rows in the quality score (matches Morgan's get_summary)

## [1.5.1] - 2026-07-08
### Added
- phantom_tracker.py — STAY OUT decision recorder
- Morgan STAY OUT quality integration
- Main loop hook for STAY OUT recording

## [1.5.0] - 2026-07-08
### Changed
- Rebranded dashboard to CryptoTrader A.I.
- Updated subtitle to "BTC & ETH -- Kraken Exchange"
- Added version + git commit hash to header
- Added STAY OUT QUALITY panel (reads phantom_trades.csv)

## v1.4.1 -- 8 Jul 2026
### Fixed
- Clean shutdown: watchdog_btc.py now honours logs/shutdown.flag and stops
  instead of restarting the engine (previously it had no flag awareness)
- main_tidetrader.py leaves the shutdown flag for the watchdog (was deleting it,
  hiding the shutdown) and clears any stale flag at startup
- start_tidetrader.bat now kills any existing dashboard/watchdog/engine before
  launching -- root cause of the duplicate CryptoTrader instances

## v1.4.0 -- 8 Jul 2026
### Added
- Full Claude decision logging (BTC + ETH): reasoning, warnings, checklist and
  hunt assessment are now written to the log after every decision, not just the
  verdict/confidence -- decisions are auditable after the fact

## v1.3.0 -- 7 Jul 2026
### Changed
- Whale block threshold raised 60->85
- Relative paths (portable across any PC)
- Whale label threshold updated for consistency
- ETH SHORT_ONLY_MODE made dynamic (follows daily SSL trend)

## v1.2.0 -- 5 Jul 2026
### Added
- ETH/GBP second trading pair
- Dual pair dashboard (BTC + ETH panels)
- ETH SHORT_ONLY_MODE
- Purple ETH theme

## v1.1.0 -- 4 Jul 2026
### Fixed
- max_tokens 1000->2000 (was truncating JSON)
- Log file basicConfig conflict
- Yahoo Finance caching (6000 bars->10-20 per tick)

## v1.0.0 -- 3 Jul 2026
### Added
- Initial build
- BTC/GBP paper trading on Kraken
- Ace (Claude AI), Moby (whale watcher)
- Tiered kill switch
- Two-page dashboard port 5001
