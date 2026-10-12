#!/usr/bin/env python3
"""Refresh market data for The AI Domino artifact.

Writes two JSON documents into ../data/:
  snapshot.json  -> artifact db  market/snapshot  (price, market cap, TTM revenue per ticker)
  history.json   -> artifact db  market/history   (6 months of daily closes per ticker)

Prices: Yahoo Finance v8 chart endpoint (server-side only).
Market cap: price x share count implied by the Oct 12, 2026 baseline (Nasdaq market cap / close).
TTM revenue: sum of the last four reported quarters from SEC XBRL companyfacts (fiscal Q4 = FY - Q1..Q3).
TSMC files no XBRL, so its TTM stays at the baseline until updated by hand.
Any ticker that fails keeps its previous values; the script exits non-zero only if every price fails.
"""
import datetime as dt
import json
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data")
UA_YAHOO = {"User-Agent": "Mozilla/5.0 (Macintosh) AIDomino/1.0"}
UA_SEC = {"User-Agent": "AIDomino personal-research refresh/1.0", "Accept-Encoding": "identity"}

# Baseline: Oct 12, 2026 close. mcap and ttm in $B.
BASE = {
    "CRWV": {"price": 80.26, "mcap": 44.27, "ttm": 7.590, "q": "Q3'25–Q2'26", "cik": 1769628},
    "ORCL": {"price": 137.39, "mcap": 416.5, "ttm": 71.776, "q": "FQ2'26–FQ1'27", "cik": 1341439},
    "MSFT": {"price": 541.15, "mcap": 4018, "ttm": 331.839, "q": "FY26", "cik": 789019},
    "GOOGL": {"price": 352.72, "mcap": 4314, "ttm": 445.866, "q": "Q3'25–Q2'26", "cik": 1652044},
    "NVDA": {"price": 226.32, "mcap": 5454, "ttm": 302.969, "q": "FQ3'26–FQ2'27", "cik": 1045810},
    "AMD": {"price": 594.24, "mcap": 970.1, "ttm": 41.305, "q": "Q3'25–Q2'26", "cik": 2488},
    "AVGO": {"price": 354.73, "mcap": 1693, "ttm": 89.104, "q": "FQ4'25–FQ3'26", "cik": 1730168},
    "ARM": {"price": 255.24, "mcap": 271.6, "ttm": 5.156, "q": "FQ2'26–FQ1'27", "cik": 1973239},
    "TSM": {"price": 444.35, "mcap": 2306, "ttm": 142.93, "q": "Q3'25–Q2'26", "cik": None},
    "DELL": {"price": 576.76, "mcap": 366.5, "ttm": 151.197, "q": "FQ3'26–FQ2'27", "cik": 1571996},
    "ANET": {"price": 210.88, "mcap": 266.0, "ttm": 10.541, "q": "Q3'25–Q2'26", "cik": 1596532},
}
REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "Revenue"]


def get_json(url, headers):
    # curl uses the macOS system trust store; python.org builds of Python often ship without CA certs
    cmd = ["curl", "-sS", "--fail", "--max-time", "30", "--compressed"]
    for k, v in headers.items():
        if k.lower() != "accept-encoding":
            cmd += ["-H", f"{k}: {v}"]
    out = subprocess.run(cmd + [url], capture_output=True, check=True).stdout
    return json.loads(out)


def yahoo(t):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=6mo&interval=1d", UA_YAHOO)
    res = d["chart"]["result"][0]
    price = float(res["meta"]["regularMarketPrice"])
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    hist = {}
    for s, c in zip(ts, closes):
        if c is not None:
            hist[dt.datetime.utcfromtimestamp(s).date().isoformat()] = round(float(c), 2)
    return price, int(res["meta"]["regularMarketTime"]), hist


def sec_ttm(cik):
    d = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", UA_SEC)
    best = None
    for ns in ("us-gaap", "ifrs-full"):
        for tag in REV_TAGS:
            f = d["facts"].get(ns, {}).get(tag)
            if not f:
                continue
            for unit, vals in f["units"].items():
                if unit != "USD":
                    continue
                q, fy = {}, {}
                for v in vals:
                    if "start" not in v:
                        continue
                    a = dt.date.fromisoformat(v["start"]); b = dt.date.fromisoformat(v["end"])
                    days = (b - a).days
                    if 80 <= days <= 100:
                        q[b] = (a, v["val"])
                    elif 350 <= days <= 380:
                        fy[b] = (a, v["val"])
                # derive fiscal Q4 = FY minus the three quarters inside it
                for e, (s, val) in fy.items():
                    if e in q:
                        continue
                    inside = [k for k in q if s < k < e]
                    if len(inside) == 3:
                        q[e] = (max(inside) + dt.timedelta(days=1), val - sum(q[k][1] for k in inside))
                ends = sorted(q)
                if len(ends) < 4:
                    continue
                last4 = ends[-4:]
                gaps = [(last4[i + 1] - last4[i]).days for i in range(3)]
                if not all(80 <= g <= 100 for g in gaps):
                    continue
                if best is None or last4[-1] > best[0]:
                    best = (last4[-1], sum(q[k][1] for k in last4) / 1e9)
    return best  # (last quarter end, ttm $B) or None


def main():
    os.makedirs(OUT, exist_ok=True)
    prev_path = os.path.join(OUT, "snapshot.json")
    prev = {}
    if os.path.exists(prev_path):
        try:
            prev = json.load(open(prev_path)).get("quotes", {})
        except Exception:
            prev = {}
    quotes, history, ok, latest_ts, log = {}, {}, 0, 0, []
    for t, b in BASE.items():
        p = dict(prev.get(t) or {k: b[k] for k in ("price", "mcap", "ttm", "q")})
        shares = b["mcap"] / b["price"]  # $B per $1 of price
        try:
            price, ts, hist = yahoo(t)
            p["price"] = round(price, 2)
            p["mcap"] = round(price * shares, 2)
            history[t] = hist
            latest_ts = max(latest_ts, ts)
            ok += 1
        except Exception as e:
            log.append(f"{t}: price failed ({e})")
        if b["cik"]:
            try:
                r = sec_ttm(b["cik"])
                if r and r[1] > 0:
                    end, ttm = r
                    if abs(ttm - b["ttm"]) / b["ttm"] > 0.005:
                        p["ttm"] = round(ttm, 3)
                        p["q"] = "4Q to " + end.strftime("%b %Y")
                    else:
                        p["ttm"], p["q"] = b["ttm"], b["q"]
            except Exception as e:
                log.append(f"{t}: SEC revenue failed ({e}); kept {p['ttm']}")
            time.sleep(0.2)
        quotes[t] = p
    if ok == 0:
        print("All price requests failed:\n" + "\n".join(log))
        sys.exit(1)
    when = dt.datetime.fromtimestamp(latest_ts, dt.timezone.utc)
    et = when - dt.timedelta(hours=4)
    snapshot = {
        "asOf": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": et.strftime("%b %-d, %Y") + (" close" if et.hour >= 16 else " " + et.strftime("%-I:%M %p") + " ET"),
        "source": "Yahoo chart (price); market cap = price x baseline share count; SEC XBRL (TTM revenue)",
        "refreshedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quotes": quotes,
    }
    dates = sorted({d for h in history.values() for d in h})
    hist_doc = {"asOf": snapshot["asOf"], "dates": dates,
                "closes": {t: [h.get(d) for d in dates] for t, h in history.items()}}
    json.dump(snapshot, open(prev_path, "w"), ensure_ascii=False, indent=1)
    json.dump(hist_doc, open(os.path.join(OUT, "history.json"), "w"), ensure_ascii=False)
    print(f"OK {ok}/{len(BASE)} prices, {len(dates)} history days, label: {snapshot['label']}")
    for line in log:
        print("WARN", line)
    for t, p in quotes.items():
        print(f"  {t:6} ${p['price']:>9.2f}  mcap {p['mcap']:>9.1f}B  ttm {p['ttm']:>8.3f}B  ({p['q']})")


if __name__ == "__main__":
    main()
