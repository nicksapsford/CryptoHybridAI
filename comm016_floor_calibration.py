"""
GAIUS COMMISSION 016 -- Crypto ATR volatility-floor recalibration.

Commission 015 found the fixed GBP50 5m-ATR floor silences ETH entirely (ETH 5m
ATR ~ GBP2 vs a GBP50 floor = 3.5% of price) and blocks ~34% of BTC. This analysis:
  Q1 distribution of ATR-as-%-of-price for BTC & ETH (+ by session) -> recommend a % floor
  Q2 signal-frequency impact of a %-floor vs the fixed GBP50 (win% / EV preserved?)
  Q3 time-of-day: does the new floor still block dead markets?
Reuses comm015 helpers + production run_all_pre_checks. NO live systems changed.
"""
import logging
import numpy as np
import pandas as pd

import comm015_backtest as bt
import pre_checks_btc
import pre_checks_eth

logging.disable(logging.CRITICAL)
OUT = []
def p(s=""):
    OUT.append(s); print(s)

OVERLAP = range(13, 17)          # London/NY overlap (UTC)
ASIAN   = range(0, 6)            # Asian doldrums (UTC)
FLOOR_CANDIDATES = [0.05, 0.08, 0.103, 0.125, 0.15]   # % of price


def session_bucket(ts):
    if ts.weekday() >= 5:
        return "weekend"
    if ts.hour in OVERLAP:
        return "overlap"
    if ts.hour in ASIAN:
        return "asian"
    return "other"


def q1_distribution(label, df5):
    atr_pct = (df5["atr"] / df5["close"] * 100).dropna()
    price = float(df5["close"].iloc[-1])
    cur_floor_pct = 50.0 / price * 100
    p(f"\n  [{label}] price~GBP{price:,.0f}   fixed GBP50 floor = {cur_floor_pct:.3f}% of price")
    qs = [10, 25, 50, 75, 90]
    p("    ATR%-of-price percentiles: " +
      "  ".join(f"p{q}={np.percentile(atr_pct, q):.3f}%" for q in qs))
    blocked = (atr_pct < cur_floor_pct).mean() * 100
    p(f"    fixed GBP50 floor blocks {blocked:.1f}% of bars")
    # by session -- align buckets to atr_pct's (NaN-dropped) index
    buckets = pd.Series([session_bucket(ts) for ts in atr_pct.index], index=atr_pct.index)
    p("    median ATR% by session: " + "  ".join(
        f"{b}={np.nanmedian(atr_pct[buckets == b]):.3f}%" for b in ["overlap", "other", "asian", "weekend"]))
    return atr_pct, price, buckets


def q1_thresholds(label, atr_pct, buckets):
    p(f"\n  [{label}] % of bars PASSING at each candidate floor (overall / overlap / asian / weekend):")
    for f in FLOOR_CANDIDATES:
        overall = (atr_pct >= f).mean() * 100
        ov = (atr_pct[buckets == "overlap"] >= f).mean() * 100
        As = (atr_pct[buckets == "asian"] >= f).mean() * 100
        wk = (atr_pct[buckets == "weekend"] >= f).mean() * 100
        p(f"    floor {f:.3f}% :  {overall:5.1f}%   overlap {ov:5.1f}%   asian {As:5.1f}%   weekend {wk:5.1f}%")


def eval_baseline(ticker, pc, floor_pct=None, ceil_pct=1.646):
    """Baseline hybrid (daily+1h SSL + quality gates) rising-edge signals + fwd 1h outcome.
    floor_pct=None -> use production fixed GBP50/GBP800. Else patch per-bar to price*pct."""
    df_1d, df_1h, df_5m = bt.fetch(ticker)
    close = df_1h["close"]; n = len(df_1h)
    start_ts = df_5m.index[0]
    first_i = max(21, int((df_1h.index < start_ts).sum()))
    last_i = n - 4
    orig_floor, orig_ceil = pc.ATR_VOL_FLOOR_GBP, pc.ATR_VOL_CEILING_GBP
    recs = []; prev = False
    by_hour_pass = {}
    for i in range(first_i, last_i):
        bar_1h = df_1h.iloc[i]; t = df_1h.index[i]
        bar_1d = bt._last_bar_at(df_1d, t); bar_5m = bt._last_bar_at(df_5m, t)
        if bar_5m is None:
            continue
        px5 = float(bar_5m["close"])
        if floor_pct is not None:
            pc.ATR_VOL_FLOOR_GBP = px5 * floor_pct / 100.0
            pc.ATR_VOL_CEILING_GBP = px5 * ceil_pct / 100.0
        try:
            ok = bool(pc.run_all_pre_checks(bar_1h=bar_1h, bar_5m=bar_5m, account=bt._clean_account(),
                                            current_trade=None, bar_1d=bar_1d,
                                            btc_atr=bar_5m.get("atr")).get("passed"))
        except Exception:
            ok = False
        d = bt._direction_from_ssl(bar_1h)
        cond = ok and d is not None
        by_hour_pass.setdefault(t.hour, [0, 0]); by_hour_pass[t.hour][1] += 1
        if cond:
            by_hour_pass[t.hour][0] += 1
        if cond and not prev:
            entry = float(close.iloc[i]); ex = float(close.iloc[i + 1])
            g = (ex - entry) / entry if d == "LONG" else (entry - ex) / entry
            recs.append({"t": t, "gross": g, "net": g - bt.TOTAL_COST_PCT,
                         "win": g > 0, "bucket": session_bucket(t)})
        prev = cond
    pc.ATR_VOL_FLOOR_GBP, pc.ATR_VOL_CEILING_GBP = orig_floor, orig_ceil
    span = max(1.0, (df_1h.index[last_i - 1] - df_1h.index[first_i]).total_seconds() / 86400.0)
    return recs, span, by_hour_pass


def summ(recs, span):
    if not recs:
        return {"n": 0, "per_day": 0.0, "win": float("nan"), "ev": float("nan")}
    nets = np.array([r["net"] for r in recs]); wins = np.array([r["win"] for r in recs])
    return {"n": len(recs), "per_day": len(recs) / span,
            "win": 100 * wins.mean(), "ev": 100 * nets.mean()}


def main():
    dfs = {}
    p("=" * 72); p("  COMMISSION 016 -- ATR VOLATILITY FLOOR RECALIBRATION"); p("=" * 72)

    p("\n### Q1 -- ATR%-of-price distribution")
    for label, ticker in [("BTC", "BTC-GBP"), ("ETH", "ETH-GBP")]:
        _, _, df5 = bt.fetch(ticker)
        dfs[label] = df5
        atr_pct, price, buckets = q1_distribution(label, df5)
        q1_thresholds(label, atr_pct, buckets)

    p("\n### Q2 -- signal-frequency impact (hybrid baseline)")
    hdr = f"  {'variant':<20}| {'BTC/day':>8} {'BTCwin':>7} {'BTC_EV':>8} | {'ETH/day':>8} {'ETHwin':>7} {'ETH_EV':>8} | {'comb/day':>8}"
    variants = [("fixed GBP50 (current)", None)] + [(f"%-floor {f:.3f}%", f) for f in [0.08, 0.103, 0.125]]
    rows = []
    for name, fp in variants:
        b_recs, b_span, b_hour = eval_baseline("BTC-GBP", pre_checks_btc, floor_pct=fp)
        e_recs, e_span, e_hour = eval_baseline("ETH-GBP", pre_checks_eth, floor_pct=fp)
        bs, es = summ(b_recs, b_span), summ(e_recs, e_span)
        rows.append((name, bs, es, b_hour, e_hour))
    p(hdr); p("  " + "-" * (len(hdr) - 2))
    for name, bs, es, _, _ in rows:
        comb = bs["per_day"] + es["per_day"]
        def f(s, k, fmt): return "n/a" if s["n"] == 0 else format(s[k], fmt)
        p(f"  {name:<20}| {f(bs,'per_day','.2f'):>8} {f(bs,'win','.0f'):>7} {f(bs,'ev','+.3f'):>8} | "
          f"{f(es,'per_day','.2f'):>8} {f(es,'win','.0f'):>7} {f(es,'ev','+.3f'):>8} | {comb:>8.2f}")

    p("\n### Q3 -- time-of-day pass-rate under recommended 0.103% floor")
    for label, ticker, pc in [("BTC", "BTC-GBP", pre_checks_btc), ("ETH", "ETH-GBP", pre_checks_eth)]:
        _, _, by_hour = eval_baseline(ticker, pc, floor_pct=0.103)
        active = sum(by_hour.get(h, [0, 0])[0] for h in OVERLAP)
        active_tot = sum(by_hour.get(h, [0, 0])[1] for h in OVERLAP)
        asian = sum(by_hour.get(h, [0, 0])[0] for h in ASIAN)
        asian_tot = sum(by_hour.get(h, [0, 0])[1] for h in ASIAN)
        ap = 100 * active / active_tot if active_tot else 0
        asp = 100 * asian / asian_tot if asian_tot else 0
        p(f"  [{label}] gate-pass rate: overlap(13-17 UTC)={ap:.0f}%   asian(00-06 UTC)={asp:.0f}%")

    with open("comm016_results.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))


if __name__ == "__main__":
    main()
