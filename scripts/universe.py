#!/usr/bin/env python3
"""Build the per-ticker "domino" dataset for The AI Domino artifact.

For every ticker in the universe it writes out/universe/<T>.json with:
  - quote + valuation: price, market cap, TTM revenue / net income, P/S, P/E, 52-week range, analyst target
  - 1-year daily closes, 200-day average, drawdowns, 60-day volatility
  - bubble gauge (0-100): five capped heuristics, see GAUGE below
  - top 5 institutional holders from 13F filings (Nasdaq institutional-holdings)
  - nearest 5 tickers: highest 1-year daily-return correlation inside the universe, with beta

Sources: Yahoo Finance chart API (prices), Nasdaq summary / financials / institutional-holdings APIs.
Extra tickers can be listed one per line in scripts/extra_tickers.txt.
"""
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"

BASE_UNIVERSE = [
    # OpenAI network
    "CRWV", "ORCL", "MSFT", "GOOGL", "NVDA", "AMD", "AVGO", "ARM", "TSM", "DELL", "ANET",
    # screened-out AI infrastructure
    "SMCI", "VRT", "ETN",
    # mega caps and AI-adjacent
    "AAPL", "AMZN", "META", "TSLA", "NFLX", "PLTR", "MU", "INTC", "QCOM", "ADBE", "CRM", "IBM", "CSCO",
    "NOW", "SNOW", "UBER", "SHOP", "ASML", "LRCX", "AMAT",
    # rest of the market, for contrast
    "JPM", "GS", "BAC", "V", "MA", "WMT", "COST", "LLY", "UNH", "XOM", "KO", "PG", "HD", "DIS",
]

# 13F filers that are themselves listed. Asset managers hold mostly client money.
HOLDER_TICKERS = [
    ("blackrock", "BLK", True), ("state street", "STT", True), ("morgan stanley", "MS", False),
    ("jpmorgan", "JPM", False), ("goldman sachs", "GS", False), ("bank of america", "BAC", False),
    ("northern trust", "NTRS", True), ("price t rowe", "TROW", True), ("t. rowe", "TROW", True),
    ("berkshire hathaway", "BRK-B", False), ("invesco", "IVZ", True), ("wells fargo", "WFC", False),
    ("charles schwab", "SCHW", False), ("ameriprise", "AMP", True), ("ubs ", "UBS", False),
    ("franklin resources", "BEN", True), ("bank of new york", "BK", True), ("royal bank of canada", "RY", False),
]
SNAP = {}
PRIVATE_MANAGERS = ["vanguard", "fmr", "geode", "capital world", "capital research", "capital international",
                    "norges", "jane street", "susquehanna", "citadel", "two sigma", "renaissance", "wellington"]


def curl_json(url, headers=None, tries=3):
    # Yahoo resets connections for full browser user agents; Nasdaq requires one
    ua = "Mozilla/5.0 (Macintosh) AIDomino/1.0" if "yahoo.com" in url else UA
    cmd = ["curl", "-sS", "--fail", "--max-time", "30", "--compressed", "-A", ua, "-H", "Accept: application/json"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    for i in range(tries):
        try:
            return json.loads(subprocess.run(cmd + [url], capture_output=True, check=True).stdout)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def money(s):
    """'$221,858,461' -> 221858461.0 ; '--' -> None"""
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") or s.startswith("-")
    s = s.strip("()-")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def yahoo_1y(t):
    d = curl_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=1y&interval=1d")
    r = d["chart"]["result"][0]
    meta = r["meta"]
    out = []
    for s, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out.append((dt.datetime.utcfromtimestamp(s).date().isoformat(), round(float(c), 2)))
    return meta, out


def nasdaq_symbol(t):
    return t.replace("-", ".")


def nasdaq_summary(t):
    d = curl_json(f"https://api.nasdaq.com/api/quote/{nasdaq_symbol(t)}/summary?assetclass=stocks")["data"]["summaryData"]
    g = lambda k: (d.get(k) or {}).get("value")
    hl = (g("FiftTwoWeekHighLow") or "").split("/")
    return {"mcap": money(g("MarketCap")), "sector": g("Sector"), "industry": g("Industry"),
            "target": money(g("OneYrTarget")), "hi52": money(hl[0]) if len(hl) == 2 else None,
            "lo52": money(hl[1]) if len(hl) == 2 else None}


def nasdaq_ttm(t):
    d = curl_json(f"https://api.nasdaq.com/api/company/{nasdaq_symbol(t)}/financials?frequency=2")["data"]
    rows = {r["value1"]: r for r in d["incomeStatementTable"]["rows"]}
    hdr = d["incomeStatementTable"]["headers"]
    def ttm(label):
        r = rows.get(label)
        if not r:
            return None
        vals = [money(r.get(f"value{i}")) for i in range(2, 6)]
        return sum(vals) * 1000 if all(v is not None for v in vals) else None
    return {"rev": ttm("Total Revenue"), "ni": ttm("Net Income"), "lastQ": hdr.get("value2")}


def nasdaq_holders(t):
    d = curl_json(f"https://api.nasdaq.com/api/company/{nasdaq_symbol(t)}/institutional-holdings"
                  "?limit=5&type=TOTAL&sortColumn=marketValue&sortOrder=DESC")["data"]
    summ = d.get("ownershipSummary") or {}
    inst_pct = money(((summ.get("SharesOutstandingPCT") or {}).get("value") or "").replace("%", ""))
    shares_out = money((summ.get("ShareoutstandingTotal") or {}).get("value"))
    rows = []
    for r in d["holdingsTransactions"]["table"]["rows"][:5]:
        name = r["ownerName"]
        low = name.lower()
        tk, mgr = None, False
        for key, sym, is_mgr in HOLDER_TICKERS:
            if key in low:
                tk, mgr = sym, is_mgr
                break
        if tk is None and any(k in low for k in PRIVATE_MANAGERS):
            mgr = True
        shares = money(r["sharesHeld"])
        rows.append({
            "name": name.title().replace("Llc", "LLC").replace("Inc.", "Inc").replace("Fmr", "FMR"),
            "ticker": tk, "manager": mgr, "asOf": r["date"],
            "shares": shares, "valueB": round(money(r["marketValue"]) * 1000 / 1e9, 2),
            "chgPct": money((r.get("sharesChangePCT") or "").replace("%", "")),
            "pctOut": round(shares / (shares_out * 1e6) * 100, 2) if shares and shares_out else None,
        })
    return {"instPct": inst_pct, "holders": rows}


def lerp_cap(x, lo, hi, log=False):
    if x is None:
        return None
    if log:
        if x <= 0:
            return 0.0
        x, lo, hi = math.log(x), math.log(lo), math.log(hi)
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def gauge(m):
    """Bubble gauge: five components, 0-20 each. A heuristic for stretch, not a forecast."""
    parts = {}
    parts["ps"] = lerp_cap(m["ps"], 3, 30, log=True) if m["ps"] else 0.5
    if m["pe"] is None:
        parts["pe"] = 1.0 if (m["ps"] or 0) > 10 else 0.5
    else:
        parts["pe"] = lerp_cap(m["pe"], 15, 90, log=True)
    parts["ret1y"] = lerp_cap(m["ret1y"], 0, 150)
    parts["vs200"] = lerp_cap(m["vs200"], 0, 50)
    parts["vol60"] = lerp_cap(m["vol60"], 20, 80)
    score = round(sum(v * 20 for v in parts.values()))
    zone = "Bubble risk" if score >= 75 else "Frothy" if score >= 55 else "Stretched" if score >= 30 else "Normal"
    return score, zone, {k: round(v * 20, 1) for k, v in parts.items()}


def metrics(closes, summ, fin):
    px = [c for _, c in closes]
    last = px[-1]
    ma200 = sum(px[-200:]) / min(200, len(px))
    peak, mdd = px[0], 0.0
    for p in px:
        peak = max(peak, p)
        mdd = min(mdd, p / peak - 1)
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    r60 = rets[-60:]
    mu = sum(r60) / len(r60)
    vol60 = math.sqrt(sum((r - mu) ** 2 for r in r60) / (len(r60) - 1)) * math.sqrt(252) * 100
    mcap = summ["mcap"]
    ps = mcap / fin["rev"] if mcap and fin["rev"] else None
    pe = mcap / fin["ni"] if mcap and fin["ni"] and fin["ni"] > 0 else None
    hi = max(px)
    return {
        "price": last, "mcapB": round(mcap / 1e9, 2) if mcap else None,
        "revB": round(fin["rev"] / 1e9, 3) if fin["rev"] else None,
        "niB": round(fin["ni"] / 1e9, 3) if fin["ni"] is not None else None, "lastQ": fin["lastQ"],
        "ps": round(ps, 2) if ps else None, "pe": round(pe, 1) if pe else None,
        "ret1y": round((last / px[0] - 1) * 100, 1), "ma200": round(ma200, 2),
        "vs200": round((last / ma200 - 1) * 100, 1), "mdd1y": round(mdd * 100, 1),
        "fromHigh": round((last / hi - 1) * 100, 1), "vol60": round(vol60, 1),
        "hi52": summ["hi52"], "lo52": summ["lo52"], "target": summ["target"],
        "upside": round((summ["target"] / last - 1) * 100, 1) if summ["target"] else None,
    }


def correlations(series):
    """series: {t: {date: close}} -> {t: [(other, corr, beta_other_on_t), ...]}"""
    dates = sorted(set.intersection(*[set(s) for s in series.values()]))
    rets = {}
    for t, s in series.items():
        p = [s[d] for d in dates]
        rets[t] = [math.log(p[i] / p[i - 1]) for i in range(1, len(p))]
    stats = {}
    for t, r in rets.items():
        m = sum(r) / len(r)
        stats[t] = (m, math.sqrt(sum((x - m) ** 2 for x in r)))
    out = {}
    for t in rets:
        mt, st = stats[t]
        row = []
        for o in rets:
            if o == t:
                continue
            mo, so = stats[o]
            cov = sum((a - mt) * (b - mo) for a, b in zip(rets[t], rets[o]))
            corr = cov / (st * so) if st and so else 0
            beta = cov / (st * st) if st else 0  # move in o per 1.0 move in t
            row.append((o, round(corr, 3), round(beta, 3)))
        row.sort(key=lambda x: -x[1])
        out[t] = row[:5]
    return out, len(dates)


def main():
    os.makedirs(DATA, exist_ok=True)
    global SNAP
    try:
        SNAP = json.load(open(os.path.join(DATA, "snapshot.json")))["quotes"]
    except Exception:
        SNAP = {}
    universe = list(BASE_UNIVERSE)
    extra = os.path.join(HERE, "extra_tickers.txt")
    if os.path.exists(extra):
        for line in open(extra):
            t = line.strip().upper()
            if t and not t.startswith("#") and t not in universe:
                universe.append(t)
    docs, series, log = {}, {}, []
    for t in universe:
        try:
            meta, closes = yahoo_1y(t)
            if len(closes) < 120:
                raise ValueError("under 120 trading days of history")
            try:
                summ = nasdaq_summary(t)
            except Exception as e:
                summ = {"mcap": None, "sector": None, "industry": None, "target": None, "hi52": None, "lo52": None}
                log.append(f"{t}: summary failed ({e})")
            try:
                fin = nasdaq_ttm(t)
            except Exception:
                fin = {"rev": None, "ni": None, "lastQ": None}
            snap = SNAP.get(t) or {}
            if summ["mcap"] is None and snap.get("mcap"):
                summ["mcap"] = snap["mcap"] / snap["price"] * closes[-1][1] * 1e9  # baseline share count x price
            if fin["rev"] is None and snap.get("ttm"):
                fin["rev"] = snap["ttm"] * 1e9
                fin["lastQ"] = snap.get("q")
                log.append(f"{t}: TTM revenue from refresh snapshot ({snap.get('q')})")
            try:
                hold = nasdaq_holders(t)
            except Exception as e:
                hold = {"instPct": None, "holders": []}
                log.append(f"{t}: holders failed ({e})")
            m = metrics(closes, summ, fin)
            score, zone, parts = gauge(m)
            docs[t] = {
                "t": t, "name": meta.get("longName") or meta.get("shortName") or t,
                "sector": summ["sector"], "industry": summ["industry"],
                "asOf": closes[-1][0], **m,
                "gauge": score, "zone": zone, "gaugeParts": parts,
                "instPct": hold["instPct"], "holders": hold["holders"],
                "dates": [d for d, _ in closes], "closes": [c for _, c in closes],
            }
            series[t] = dict(closes)
        except Exception as e:
            log.append(f"{t}: skipped ({e})")
        time.sleep(0.25)
    near, ndays = correlations(series)
    for t, rows in near.items():
        docs[t]["nearest"] = [{"t": o, "corr": c, "beta": b} for o, c, b in rows]
        docs[t]["corrDays"] = ndays
    built = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # one document for the artifact db: a shared trading calendar, closes aligned to it
    cal = sorted({d for doc in docs.values() for d in doc["dates"]})
    pos = {d: i for i, d in enumerate(cal)}
    packed = {}
    for t, d in docs.items():
        row = [None] * len(cal)
        for day, c in zip(d.pop("dates"), d.pop("closes")):
            row[pos[day]] = c
        d["closes"] = row
        packed[t] = d
    doc = {"builtAt": built, "corrDays": ndays, "dates": cal, "tickers": packed}
    path = os.path.join(DATA, "universe_all.json")
    json.dump(doc, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path)
    if size > 250_000:
        print(f"WARN universe_all.json is {size} bytes; the artifact db limit is 256 KiB per document")
    print(f"OK {len(docs)}/{len(universe)} tickers, correlations over {ndays} common days")
    for line in log:
        print("WARN", line)


if __name__ == "__main__":
    main()
