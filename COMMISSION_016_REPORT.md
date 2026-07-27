# GAIUS COMMISSION 016 — Crypto ATR Volatility-Floor Recalibration
**Report — 2026-07-26 UTC · built & run by Cody · for Nick & Archie**
Script `comm016_floor_calibration.py` · raw output `comm016_results.txt` · **no live systems changed.**

## 1. Executive summary
The fixed **£50** 5m-ATR floor is a percentage of price only by accident of BTC's price.
On BTC (~£48,800) it equals **0.102%** and blocks a reasonable 33% of bars; on ETH (~£1,450)
it equals **3.45%** of price and blocks **100%** — ETH has never produced a signal. Replacing
it with a **%-of-price floor** auto-scales to both instruments and is quality-neutral for BTC.
Backtest (60d): a **0.10% floor leaves BTC essentially unchanged** (1.22→1.24 signals/day, win
48→46%, EV flat) and **unlocks ETH from 0 to ~1.4 signals/day at 50% win / −0.75% EV — quality
equal-to-better than BTC.** Combined crypto signal frequency roughly **doubles** (1.22 → ~2.6/day).
Recommend per-instrument floors **BTC 0.10% / ETH 0.125%** (ETH's distribution sits higher, so a
touch more selectivity keeps its dead-market blocking equivalent). Low risk; ETH is unproven live.

## 2. Floor calibration analysis (Q1)
ATR as % of price, 5m bars, 60 days:

| | p10 | p25 | **p50** | p75 | p90 | fixed £50 = | blocks |
|---|---|---|---|---|---|---|---|
| **BTC** (~£48,800) | 0.067 | 0.092 | **0.127** | 0.183 | 0.255 | 0.102% | 33% |
| **ETH** (~£1,450) | 0.097 | 0.123 | **0.163** | 0.234 | 0.317 | **3.448%** | **100%** |

**ETH is more volatile on a % basis than BTC** (median 0.163% vs 0.127%) — confirming the
commission's premise. Median ATR% by session shows the floor's real job (blocking dead markets):

| | overlap (13-17 UTC) | other | asian (00-06) | weekend |
|---|---|---|---|---|
| BTC | 0.221% | 0.138% | 0.131% | 0.080% |
| ETH | 0.275% | 0.174% | 0.167% | 0.113% |

**% of bars passing at each candidate floor** (overall / overlap / asian / weekend):

| floor | BTC overall | BTC overlap | BTC weekend | ETH overall | ETH overlap | ETH weekend |
|---|---|---|---|---|---|---|
| 0.080% | 83% | 100% | 50% | 96% | 100% | 87% |
| **0.103%** | 67% | 99.9% | **27%** | 87% | 100% | **62%** |
| **0.125%** | 52% | 97.8% | 17% | 74% | 99.9% | **41%** |
| 0.150% | 38% | 92% | 13% | 58% | 98% | 27% |

Reading: at 0.103%, BTC keeps ~all overlap bars and blocks most weekend (27% pass) — good
separation, ~matches today. ETH at 0.103% still lets 62% of weekend through — because ETH's
whole distribution is higher, the **same** % blocks **less** of ETH's dead time. To give ETH
BTC-equivalent selectivity (~block the bottom third of its own distribution ≈ its ~p30 ≈ 0.13%),
ETH wants a **slightly higher** floor. Hence per-instrument: **BTC ≈ p30 ≈ 0.10%, ETH ≈ p30 ≈ 0.125%.**

## 3. Signal-frequency impact (Q2) — hybrid baseline, net of 0.82% taker

| Floor | BTC/day | BTC win | BTC EV | ETH/day | ETH win | ETH EV | **Combined/day** |
|---|---|---|---|---|---|---|---|
| **£50 fixed (current)** | 1.22 | 48% | −0.843% | **0** | — | — | **1.22** |
| %-floor 0.080% | 1.49 | 47% | −0.843% | 1.47 | 48% | −0.789% | 2.96 |
| **%-floor 0.103%** | 1.24 | 46% | −0.855% | 1.41 | 50% | −0.750% | **2.64** |
| **%-floor 0.125%** | 1.10 | 47% | −0.858% | 1.32 | 51% | −0.744% | 2.43 |

- **BTC is preserved** at 0.10–0.103% (freq +2%, win/EV within noise) — quality-neutral, as designed.
- **ETH unlocks to ~1.3–1.4/day** and its raw-signal quality is **equal-to-better than BTC**
  (win 50–51% vs 46–48%; EV −0.75% vs −0.85%). So unlocking ETH adds **genuine signal, not noise** —
  the commission's key test passes.
- **Combined frequency ~doubles** (1.22 → ~2.4–2.6/day).
- (All raw EVs negative — pre-Arthur, pre-stop/target, exactly as Commission 015; the floor change
  is a frequency/dead-market fix, not an EV claim.)

## 4. Time-of-day verification (Q3)
From the §2 by-session pass rates (vol floor in isolation) at the recommended floors: BTC 0.10%
passes 99.9% of London/NY-overlap bars and blocks 73% of weekend bars; ETH 0.125% passes ~100%
of overlap and blocks ~59% of weekend. **The floor still blocks dead markets while opening active
ones.** (The script's full-gate pass rates are dominated by the SSL/momentum gates, so the §2
vol-only numbers are the authoritative read here.)

## 5. Implementation recommendation (Q3)
Replace the fixed constant with a %-of-price floor+ceiling that auto-scales:
```
BTC_VOLATILITY_FLOOR_PCT   = 0.10    # was £50 (=0.102% at £48.8k) — preserves BTC
ETH_VOLATILITY_FLOOR_PCT   = 0.125   # ETH sits higher; equal selectivity to BTC
VOLATILITY_CEILING_PCT     = 1.65    # was £800 (=1.646% at £48.8k); ETH ceiling ~£24, never binds
# per bar:  atr_floor_gbp = current_price * FLOOR_PCT/100 ;  atr_ceiling_gbp = current_price * CEILING_PCT/100
```
- **Per-instrument** floors recommended (evidence supports it). A single shared **0.10%** is an
  acceptable simpler option but leaves more ETH dead-time through.
- **Apply to CryptoHybrid (5041)** — where evidence-based changes land.
- **CryptoTrader (5001):** has the same defect (ETH silenced since launch). Fixing it changes the
  scientific control — **Nick/Archie's call**: fix the defect, or preserve 5001 as the "BTC-only
  legacy" baseline and let 5041 carry the corrected dual-brain.
- **CryptoBenchmark (5021):** mirror whatever 5001 does, for comparability (per commission).
- **Expected impact:** ETH 0 → ~1.3–1.4 signals/day; combined crypto ~1.22 → ~2.4–2.6/day (~2×).
  Pre-Arthur — actual trades fewer, since Arthur still gates ENTER/STAY_OUT.

## 6. Honest limitations
- **Raw-signal EV is negative** for everything (pre-Arthur/pre-management); this backtest validates
  the floor as **quality-neutral and dead-market-correct**, not profitability. Profit still rests on
  Arthur + stop/target/ladder (Commission 012).
- **ETH has zero live history** — Arthur has never assessed a live ETH crypto signal, so its ETH
  prompt/calibration is unproven. Recommend unlocking on 5041 first and watching Arthur's early ETH
  decisions closely; the slightly-higher 0.125% ETH floor adds initial selectivity.
- **One ~60-day regime**; %-floor is regime-robust by construction, but win/EV figures are period-specific.
- Forward testing will confirm real ETH signal count, Arthur's ETH ENTER rate, and post-management EV.
```
```
*Evidence supports the recalibration. Awaiting Nick/Archie brief before any live change (Rule 4).*

---

## CORRECTION & IMPLEMENTATION NOTE — 2026-07-27 (Cody)
**IMPLEMENTED (Nick-approved).** The %-of-price floor/ceiling shipped to CryptoTrader (5001,
v1.14.0), CryptoBenchmark (5021, v1.3.0) and CryptoHybrid (5041, v1.1.0):
`BTC_VOLATILITY_FLOOR_PCT=0.10`, `ETH_VOLATILITY_FLOOR_PCT=0.125`, `VOLATILITY_CEILING_PCT=1.65`;
per bar `atr_floor_gbp = btc_price * FLOOR_PCT/100`, `atr_ceiling_gbp = btc_price * CEILING_PCT/100`.

**CORRECTION — the "ETH blocks 100%" premise was a HARNESS ARTIFACT, not a production defect.**
§1/§2 treat the fixed £50 floor as 3.448% of ETH's *own* price (£1,450) and conclude ETH is
blocked 100%. That is how the **backtest harness** evaluated it (ETH's own ATR vs the floor). The
**live/production code does NOT do this**: the volatility gate reads the **SHARED BTC 5m ATR** for
BOTH engines (BTC-led by design), so ETH passes/fails on the same BTC ATR reading as BTC and was
**never blocked in production**. Consequences:
- The headline "unlocks ETH from 0 to ~1.4 signals/day" (§1, §3, §5) is a harness projection on
  ETH's own ATR; it does **not** describe the live change. The real production change is a modest
  re-scaling of the shared BTC-ATR thresholds.
- Because the gate uses the BTC price, the ETH floor (0.125%) and ceiling (1.65%) are applied to
  the **BTC** price too — e.g. at BTC £48.8k the ETH floor is ~£61 (not ~£1.8), the ceiling ~£806
  (not ~£24). The "ETH ceiling ~£24 never binds" note in §5 reflects the harness (ETH-price) model.
- Verified live no-op at BTC £48,848.70 / 5m ATR £47.1 (07:42 UTC 27 Jul): BTC floor £48.85 (was
  £50), ceiling £806 (was £800) — negligible; **ETH floor £61.06 (was £50)** — a small tightening
  of ~£11 on the shared BTC ATR (blocks ETH only when the BTC ATR sits in the £50–£61 band).
  At the live ATR both gates classify identically to before.

**FLAG for Nick/Archie:** given the shared-BTC-ATR reality, setting the ETH floor (0.125%) *higher*
than the BTC floor (0.10%) makes ETH strictly **more** selective than BTC on the *identical* signal
— the opposite of the "ETH sits higher so needs a higher floor" rationale, which was based on ETH's
own (harness) ATR distribution. Shipped as specified (approved, and conservative), but if ETH-equal-
to-BTC selectivity is preferred, set `ETH_VOLATILITY_FLOOR_PCT=0.10` to match BTC. Dormant until
Nick restarts — his call at restart.
