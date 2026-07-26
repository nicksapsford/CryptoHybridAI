# GAIUS COMMISSION 015 — Crypto Alternative-Indicator Signal Backtest
**Report — 2026-07-26 UTC · built & run by Cody · for Nick & Archie**

## Method (what was measured, and what was NOT)
A **signal-evaluation** backtest over the last **~60 days** (5m-data window; span 59.7d),
BTC-GBP and ETH-GBP, reusing **production code** so the baseline matches the live gate:
`add_indicators` (SSL/TMO/Chande/RSI/ATR) + `pre_checks_{btc,eth}.run_all_pre_checks`
(+ `check_ssl_agreement` for the 5001 triple-SSL gate). Script: `comm015_backtest.py`
(reproducible; raw output `comm015_results.txt`).

For every 1h bar, each strategy either fires a **fresh (rising-edge) signal** or not; we then
score the **forward 1-hour directional move**, net of the **0.82% Kraken taker** round-trip,
at a **£300 notional**. A "signal" = condition true now, false on the prior bar.

**Critical framing:** this measures the *trigger*, not the system. It does **not** include
Arthur's ENTER/STAY_OUT filtering or the stop/target/ladder management — which Commission 012
showed is where Crypto's edge lives (+£3,674 net saved). So **raw-signal EV is expected to be
negative** and the meaningful comparison is *relative* (frequency & quality vs baseline), per
the commission's own framing ("more signals of equal quality for Arthur to assess").

## Results — BTC (n, signals/day, win% = directional move at 1h, EV net of fees)

| Strategy | n | sig/day | win % | EV/sig | false % | max consec loss |
|---|---|---|---|---|---|---|
| Baseline 3-SSL (5001) | 61 | 1.02 | 47.5 | −0.871% | 43 | 19 |
| Baseline 2-SSL (5041 hybrid) | 73 | 1.22 | 47.9 | −0.843% | 45 | 22 |
| Donchian N=10 | 135 | 2.26 | 43.0 | −0.872% | 55 | 48 |
| Donchian N=14 | 115 | 1.93 | 47.0 | −0.829% | 56 | 41 |
| Donchian N=20 | 96 | 1.61 | 50.0 | −0.821% | 56 | 41 |
| Donchian+BB N=10 | 33 | 0.55 | 51.5 | −0.821% | 48 | 25 |
| Donchian+BB N=14 | 29 | 0.49 | 58.6 | −0.815% | 48 | 24 |
| Donchian+BB N=20 | 27 | 0.45 | 55.6 | −0.860% | 48 | 27 |

## Results — ETH
**Baseline = 0 signals (both 3-SSL and 2-SSL).** Root cause verified: the volatility gate
`check_volatility_range` rejects **100%** of ETH bars — ETH 5m ATR median **£2.17** (price
~£1,444) vs an **ATR floor of £50** inherited from BTC. Donchian (no vol gate) fired:
N=10 2.24/day (44.0% win), N=14 1.81/day (45.4%), N=20 1.57/day (43.6%); Donchian+BB
0.52–0.64/day (55–57% win). All ETH EV ≈ −0.84% to −0.88%.

## Comparison 4 — RSI divergence as Arthur context (BTC hybrid signals, n=73)
| Bucket | n | win % |
|---|---|---|
| No divergence | 59 | 51 |
| Bullish divergence | 12 | 42 |
| Bearish divergence | 2 | 0 |
| **Signal direction CONFLICTS with divergence** | 13 | **31** |
| Signal direction aligns with divergence | 1 | 100 |

Suggestive: a signal whose direction **conflicts** with RSI divergence wins 31% vs 51% baseline
— i.e. divergence-conflict looks like a useful *negative* context flag. **But n=13 (<20) — below
the confidence bar; directional only.** ETH: no baseline signals to tag.

## Findings
1. **Donchian raises frequency but not quality.** ~1.6–2.3 sig/day vs baseline ~1.0–1.2 (≈1.5–2×),
   but false-breakout rate jumps to **55–56%** (vs 43–45%) and max-consecutive-losses to **41–48**
   (vs 19–22). Win rate / EV are **within sample noise** of baseline — not better. This is the
   classic Donchian whipsaw. **Fails "more signals of *equal* quality."**
2. **Bollinger squeeze filter improves quality but destroys frequency.** Don+BB win rate 57–59%,
   lower false rate — but only **0.45–0.64 sig/day, fewer than baseline**. It worsens the exact
   problem the commission is trying to solve.
3. **The real frequency constraint is the volatility floor, not the entry indicator.** ETH is
   **100% blocked** by a £50 ATR floor calibrated for BTC; BTC itself is blocked **34%** of bars.
   Rescaling the floor to a %-of-price basis (ETH-appropriate ≈ £1.5, or ~0.1% of price) would
   unlock ETH entirely and lift BTC — a larger, **quality-neutral** frequency gain than any SSL→
   Donchian swap.
4. **Everything is −EV on raw signals after fees** (~−0.8%/signal at 1h, taker). Expected — this
   is pre-Arthur, pre-trade-management. On **maker/limit fills (0.34%)** EV improves ≈ +0.48%/signal;
   the fee model dominates. Any go/no-go must be judged on Arthur-filtered, stop/target-managed
   outcomes, not raw signals.

## Sample-size caveats
- BTC baseline (61–73) and Donchian (96–135): adequate (>20).
- Don+BB (27–35): marginal. RSI-divergence buckets (1–13): **insufficient**, directional only.
- ETH baseline: **0** (gated out). One ~60-day regime only — no regime diversity.

## Recommendation → **Option D + a floor recalibration (a targeted Option E)**
- **Do NOT replace SSL with Donchian (Option B)** — more signals but noisier, not equal quality.
- **Do NOT adopt Donchian+BB (Option C)** — improves quality but cuts frequency below baseline.
- **Option D (RSI divergence as Arthur context) — cautiously yes, lowest risk.** Inject
  "RSI divergence [BULLISH/BEARISH]; conflicts with signal direction" as context for Arthur on
  CryptoHybrid (5041) only. It doesn't touch signal frequency and the conflict signal looks
  informative — but collect ~15–20 forward signals to confirm before trusting it.
- **Priority frequency fix (biggest lever found):** recalibrate the **volatility ATR floor to a
  %-of-price basis**, especially ETH (currently 100% blocked). This is the cleanest way to get
  "more signals of equal quality" and belongs in a short follow-up commission — it needs its own
  before/after evidence and Nick/Archie sign-off (Rule 4). CryptoTrader (5001) stays the control;
  any change lands on CryptoHybrid (5041).

*No live systems were changed. Findings for Nick & Archie to decide on.*
