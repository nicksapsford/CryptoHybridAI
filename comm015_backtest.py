"""
GAIUS COMMISSION 015 -- Crypto alternative-indicator SIGNAL backtest.

Question: do Donchian Channel breakout / Bollinger squeeze / RSI divergence
generate MORE qualifying signals than the SSL alignment gate WITHOUT losing the
signal quality that makes CryptoTrader the desk's best system?

This is a SIGNAL-EVALUATION backtest (not a stop/target trade sim): for every 1h
bar we decide whether each strategy fires a fresh (rising-edge) signal, then score
the forward 1-hour directional move, net of Kraken taker fees. It REUSES production
indicator + pre-check code so the baseline matches the live gate exactly:
  - add_indicators (data_feed_btc)                -> SSL/TMO/Chande/RSI/ATR/MACD
  - pre_checks_{btc,eth}.run_all_pre_checks        -> the live CryptoHybrid gate
  - + check_ssl_agreement for the 5001 triple-SSL baseline
CryptoTrader (5001) and CryptoHybrid (5041) are NOT modified -- research only.
"""

import logging
import sys
from datetime import timezone

import numpy as np
import pandas as pd
import yfinance as yf

from data_feed_btc import add_indicators
import pre_checks_btc
import pre_checks_eth

logging.disable(logging.CRITICAL)  # silence per-check pre-check logging

# ── Cost + sizing assumptions ────────────────────────────────────────────────
ENTRY_FEE_PCT = 0.0040
EXIT_FEE_PCT  = 0.0040
SPREAD_PCT    = 0.0002
TOTAL_COST_PCT = ENTRY_FEE_PCT + EXIT_FEE_PCT + SPREAD_PCT   # 0.82% taker round-trip
NOTIONAL_GBP  = 300.0   # nominal per-signal position (Comm 009: ~£300 scalp float); £ = notional*return

DONCHIAN_NS   = [10, 14, 20]
BB_PERIOD     = 20
BB_STD        = 2.0
RSI_DIV_LOOKBACK = 14
FWD_BARS_FALSE = 3      # "false signal" horizon


def _download(ticker, interval, period):
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"yfinance empty for {ticker} {interval} {period}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    df.sort_index(inplace=True)
    return df


def fetch(ticker):
    df_1d = add_indicators(_download(ticker, "1d", "2y"))
    df_1h = add_indicators(_download(ticker, "1h", "90d"))
    df_5m = add_indicators(_download(ticker, "5m", "60d"))
    return df_1d, df_1h, df_5m


def _last_bar_at(df, ts):
    mask = df.index <= ts
    return df[mask].iloc[-1] if mask.any() else None


def _clean_account():
    return {"killed": False, "kill_reason": "", "daily_pnl_gbp": 0.0,
            "consecutive_losses": 0, "last_loss_time": None,
            "session_start": "2026-01-01T00:00:00+00:00"}


def _bb_bandwidth(close):
    mid = close.rolling(BB_PERIOD).mean()
    sd  = close.rolling(BB_PERIOD).std()
    return ((mid + BB_STD * sd) - (mid - BB_STD * sd)) / mid   # (upper-lower)/mid


def _direction_from_ssl(bar):
    v = bar.get("ssl_bull")
    if pd.isna(v):
        return None
    return "LONG" if v else "SHORT"


def evaluate(ticker, pre_checks):
    df_1d, df_1h, df_5m = fetch(ticker)
    close = df_1h["close"]
    bw = _bb_bandwidth(close)
    n = len(df_1h)

    # evaluation window: 5m data exists AND room for N-history + forward bars
    start_ts = df_5m.index[0]
    first_i = max(21, int((df_1h.index < start_ts).sum()))
    last_i  = n - (FWD_BARS_FALSE + 1)   # need i+1 (1h fwd) and i+3 (false horizon)

    # rising-edge state per strategy
    prev = {}
    records = {k: [] for k in
              ["baseline_triple", "baseline_hybrid",
               "donchian_10", "donchian_14", "donchian_20",
               "donbb_10", "donbb_14", "donbb_20"]}
    # RSI-div tags attached to hybrid-baseline signals
    div_tags = []

    def fwd(i, direction):
        entry = float(close.iloc[i]); exit1h = float(close.iloc[i + 1])
        g = (exit1h - entry) / entry if direction == "LONG" else (entry - exit1h) / entry
        net = g - TOTAL_COST_PCT
        # false-signal: directional move at +3 bars <= 0
        exit3 = float(close.iloc[min(i + FWD_BARS_FALSE, n - 1)])
        g3 = (exit3 - entry) / entry if direction == "LONG" else (entry - exit3) / entry
        return g, net, (g3 <= 0)

    def rec(key, i, direction):
        g, net, false_sig = fwd(i, direction)
        t = df_1h.index[i]
        b5 = _last_bar_at(df_5m, t)
        atr = float(b5.get("atr")) if b5 is not None and pd.notna(b5.get("atr")) else np.nan
        records[key].append({
            "t": t, "dir": direction, "gross": g, "net": net,
            "gbp": NOTIONAL_GBP * net, "win": g > 0, "false": false_sig,
            "hour": t.hour, "weekend": t.weekday() >= 5, "atr": atr})

    for i in range(first_i, last_i):
        bar_1h = df_1h.iloc[i]
        t = df_1h.index[i]
        bar_1d = _last_bar_at(df_1d, t)
        bar_5m = _last_bar_at(df_5m, t)
        if bar_5m is None:
            continue
        acct = _clean_account()

        # ---- production baseline gate (CryptoHybrid = daily+1h + quality) ----
        try:
            base = pre_checks.run_all_pre_checks(
                bar_1h=bar_1h, bar_5m=bar_5m, account=acct,
                current_trade=None, bar_1d=bar_1d, btc_atr=bar_5m.get("atr"))
            base_pass = bool(base.get("passed"))
        except Exception:
            base_pass = False
        base_dir = _direction_from_ssl(bar_1h)
        hybrid_ok = base_pass and base_dir is not None

        # triple = hybrid + 5m SSL agreement (the 5001 original gate)
        try:
            ssl5 = pre_checks.check_ssl_agreement(bar_1h, bar_5m).get("passed")
        except Exception:
            ssl5 = False
        triple_ok = hybrid_ok and bool(ssl5)

        # ---- Donchian breakout (close-based) on 1h + daily-trend filter ----
        daily_dir = _direction_from_ssl(bar_1d) if bar_1d is not None else None
        don = {}
        for N in DONCHIAN_NS:
            hi = close.iloc[i - N:i].max(); lo = close.iloc[i - N:i].min()
            c = float(close.iloc[i])
            d = None
            if c > hi:   d = "LONG"
            elif c < lo: d = "SHORT"
            # daily-trend alignment required (commission)
            if d is not None and daily_dir is not None and d == daily_dir:
                don[N] = d
            else:
                don[N] = None

        # ---- Bollinger squeeze->expansion filter ----
        bw_now = bw.iloc[i]
        recent_squeeze = (pd.notna(bw_now) and i >= 20 and
                          bw.iloc[i - 5:i + 1].min() == bw.iloc[i - 20:i + 1].min())
        expanding = pd.notna(bw_now) and pd.notna(bw.iloc[i - 3]) and bw_now > bw.iloc[i - 3]
        bb_ok = bool(recent_squeeze and expanding)

        # ---- fire rising-edge signals ----
        def edge(key, cond, direction):
            fired = cond and (not prev.get(key, False))
            prev[key] = cond
            if fired and direction is not None:
                rec(key, i, direction)

        edge("baseline_hybrid", hybrid_ok, base_dir)
        edge("baseline_triple", triple_ok, base_dir)
        for N in DONCHIAN_NS:
            edge(f"donchian_{N}", don[N] is not None, don[N])
            edge(f"donbb_{N}", (don[N] is not None and bb_ok), don[N])

        # ---- RSI divergence tag on hybrid-baseline signals (Comparison 4) ----
        if hybrid_ok and (not prev.get("_div_seen_edge", False)):
            pass
        # tag whenever a hybrid signal just fired (align with its record)
        if records["baseline_hybrid"] and records["baseline_hybrid"][-1]["t"] == t:
            L = RSI_DIV_LOOKBACK
            div = "NONE"
            if i >= L and pd.notna(bar_1h.get("rsi")) and pd.notna(df_1h["rsi"].iloc[i - L]):
                p_now, p_then = float(close.iloc[i]), float(close.iloc[i - L])
                r_now, r_then = float(bar_1h["rsi"]), float(df_1h["rsi"].iloc[i - L])
                if p_now < p_then and r_now > r_then:   div = "BULLISH"
                elif p_now > p_then and r_now < r_then: div = "BEARISH"
            div_tags.append({"t": t, "dir": base_dir, "div": div,
                             "win": records["baseline_hybrid"][-1]["win"],
                             "net": records["baseline_hybrid"][-1]["net"]})

    span_days = max(1.0, (df_1h.index[last_i - 1] - df_1h.index[first_i]).total_seconds() / 86400.0)
    return records, div_tags, span_days, df_5m


def _stats(recs, span_days):
    n = len(recs)
    if n == 0:
        return {"n": 0}
    nets = np.array([r["net"] for r in recs])
    gbp  = np.array([r["gbp"] for r in recs])
    wins = np.array([r["win"] for r in recs])
    win_gbp = gbp[gbp > 0]; loss_gbp = gbp[gbp <= 0]
    # max consecutive losses (net<0)
    mc = c = 0
    for x in nets:
        if x < 0: c += 1; mc = max(mc, c)
        else: c = 0
    ov = [r for r in recs if 13 <= r["hour"] < 17]
    wk = [r for r in recs if r["weekend"]]
    return {
        "n": n,
        "per_day": n / span_days,
        "win_rate": 100.0 * wins.mean(),
        "avg_win_gbp": win_gbp.mean() if len(win_gbp) else 0.0,
        "avg_loss_gbp": loss_gbp.mean() if len(loss_gbp) else 0.0,
        "ev_gbp": gbp.mean(),
        "ev_pct": 100.0 * nets.mean(),
        "max_consec_loss": mc,
        "false_rate": 100.0 * np.mean([r["false"] for r in recs]),
        "overlap_share": 100.0 * len(ov) / n,
        "overlap_wr": 100.0 * np.mean([r["win"] for r in ov]) if ov else float("nan"),
        "weekend_share": 100.0 * len(wk) / n,
    }


def main():
    out = []
    def p(s=""):
        out.append(s); print(s)

    results = {}
    for ticker, pc, label in [("BTC-GBP", pre_checks_btc, "BTC"),
                              ("ETH-GBP", pre_checks_eth, "ETH")]:
        p(f"\n{'='*70}\n  {label}  ({ticker})\n{'='*70}")
        recs, divs, span, df5 = evaluate(ticker, pc)
        results[label] = {}
        for key in ["baseline_triple", "baseline_hybrid",
                    "donchian_10", "donchian_14", "donchian_20",
                    "donbb_10", "donbb_14", "donbb_20"]:
            s = _stats(recs[key], span)
            results[label][key] = s
            if s["n"] == 0:
                p(f"  {key:<18} : 0 signals")
                continue
            p(f"  {key:<18} : n={s['n']:<4d} {s['per_day']:.2f}/day  "
              f"win={s['win_rate']:.1f}%  EV=£{s['ev_gbp']:+.3f} ({s['ev_pct']:+.3f}%)  "
              f"avgW=£{s['avg_win_gbp']:+.2f} avgL=£{s['avg_loss_gbp']:+.2f}  "
              f"maxCL={s['max_consec_loss']}  false={s['false_rate']:.0f}%  "
              f"overlap={s['overlap_share']:.0f}%")
        p(f"  span={span:.1f} days")

        # Comparison 4: RSI divergence value on hybrid-baseline signals
        p(f"\n  -- Comparison 4: RSI divergence on {label} hybrid-baseline signals --")
        for dv in ["NONE", "BULLISH", "BEARISH"]:
            sub = [d for d in divs if d["div"] == dv]
            if sub:
                wr = 100.0 * np.mean([d["win"] for d in sub])
                ev = 100.0 * np.mean([d["net"] for d in sub])
                p(f"     {dv:<8}: n={len(sub):<3d} win={wr:.0f}%  EV={ev:+.3f}%")
            else:
                p(f"     {dv:<8}: n=0")
        # directional-divergence-conflict test: LONG signal with BEARISH div
        conflict = [d for d in divs if (d["dir"] == "LONG" and d["div"] == "BEARISH") or
                    (d["dir"] == "SHORT" and d["div"] == "BULLISH")]
        align = [d for d in divs if (d["dir"] == "LONG" and d["div"] == "BULLISH") or
                 (d["dir"] == "SHORT" and d["div"] == "BEARISH")]
        if conflict:
            p(f"     signal-vs-div CONFLICT: n={len(conflict)} win={100.0*np.mean([d['win'] for d in conflict]):.0f}%")
        if align:
            p(f"     signal-vs-div ALIGNED : n={len(align)} win={100.0*np.mean([d['win'] for d in align]):.0f}%")

    # ── Comparison table ──
    p(f"\n{'='*70}\n  COMPARISON TABLE (Donchian/Don+BB shown at N=14)\n{'='*70}")
    cols = [("baseline_triple", "Baseline(3SSL)"), ("baseline_hybrid", "Hybrid(2SSL)"),
            ("donchian_14", "Donchian14"), ("donbb_14", "Don+BB14")]
    def cell(s, field, fmt):
        return "n/a" if s.get("n", 0) == 0 else format(s[field], fmt)
    p("  " + "Metric".ljust(16) + "".join("| " + c[1].rjust(14) + " " for c in cols))
    p("  " + "-" * 78)
    for label in ["BTC", "ETH"]:
        r = results[label]
        p(f"  [{label}]")
        for field, fmt, name in [("per_day", ".2f", "Signals/day"),
                                 ("win_rate", ".1f", "Win rate %"),
                                 ("ev_gbp", "+.3f", "EV/signal £"),
                                 ("ev_pct", "+.3f", "EV/signal %"),
                                 ("max_consec_loss", "d", "Max consec loss"),
                                 ("false_rate", ".0f", "False signal %")]:
            row = "  " + name.ljust(16)
            for key, _ in cols:
                row += "| " + cell(r[key], field, fmt).rjust(14) + " "
            p(row)
    p("\n  NOTE: win rate = price moved in signal direction at 1h (gross); EV = net of "
      f"{TOTAL_COST_PCT*100:.2f}% Kraken taker round-trip at £{NOTIONAL_GBP:.0f} notional.")
    p("  Signal = rising edge (condition true now, false prior bar). Donchian is close-based + daily-SSL filter.")

    with open("comm015_results.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    main()
