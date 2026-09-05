#!/usr/bin/env python3
"""
Basis Points — investor news & market data pipeline.

Fetches headlines from major financial news RSS feeds and live market data
(Yahoo Finance chart API, with Stooq and CoinGecko fallbacks), then renders:

  site/index.html            the live dashboard + morning brief
  site/artifact.html         same page, formatted for claude.ai Artifact publishing
  site/archive/YYYY-MM-DD.html   daily archive of the brief
  site/archive/index.html    archive listing
  data/latest.json           structured data (for commentary tooling)

Standard library only — no packages to install. Safe to run on a schedule.

Usage:
  python3 pipeline.py               # full run: fetch + render
  python3 pipeline.py --render-only # re-render from data/latest.json (e.g. after
                                    # editing data/take.md commentary)

Editorial commentary: if data/take.md exists and was modified in the last 24h,
its paragraphs are rendered as "The take". Otherwise an auto-generated theme
scan is shown.
"""

import argparse
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import smallcap

BASE = Path(__file__).resolve().parent
SITE = BASE / "site"
DATA = BASE / "data"

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SEC_UA = "BasisPointsAggregator/1.0 (personal research project)"
# FRED and similar data services stall browser-impersonating clients but serve
# honestly-identified tools instantly — so data endpoints get the plain UA.
BOT_UA = SEC_UA

MAX_AGE_HOURS = 48
PER_COLUMN = 8
TOP_COUNT = 7

# ---------------------------------------------------------------- feeds ------

FEEDS = [
    # name, url, category, source weight
    ("CNBC",            "https://www.cnbc.com/id/100003114/device/rss/rss.html", "top",       1.5),
    ("CNBC Economy",    "https://www.cnbc.com/id/20910258/device/rss/rss.html",  "economy",   1.5),
    ("CNBC Earnings",   "https://www.cnbc.com/id/15839135/device/rss/rss.html",  "companies", 1.5),
    ("MarketWatch",     "https://feeds.content.dowjones.io/public/rss/mw_topstories",        "markets", 1.4),
    ("MarketWatch",     "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "markets", 1.2),
    ("WSJ Markets",     "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",       "markets", 2.0),
    ("WSJ Business",    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",     "companies", 2.0),
    ("WSJ World",       "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",         "global",  1.6),
    ("Yahoo Finance",   "https://finance.yahoo.com/news/rssindex",               "markets",   1.2),
    ("Financial Times", "https://www.ft.com/markets?format=rss",                 "markets",   2.0),
    ("The Economist",   "https://www.economist.com/finance-and-economics/rss.xml", "economy", 1.8),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",    "economy",   2.2),
    ("NYT Business",    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "companies", 1.5),
    ("NYT DealBook",    "https://rss.nytimes.com/services/xml/rss/nyt/Dealbook.xml", "companies", 1.5),
    ("Google News Business", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en", "top", 1.0),
    ("Seeking Alpha",   "https://seekingalpha.com/market_currents.xml",          "markets",   1.0),
    ("CoinDesk",        "https://www.coindesk.com/arc/outboundfeeds/rss/",       "crypto",    1.2),
    ("Cointelegraph",   "https://cointelegraph.com/rss",                         "crypto",    1.0),
]

SEC_FEED = ("SEC 8-K filings",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=24&output=atom")

CATEGORIES = [
    ("markets",   "Markets"),
    ("economy",   "Economy & policy"),
    ("companies", "Companies & earnings"),
    ("global",    "Global"),
    ("crypto",    "Crypto & digital assets"),
]

SCORE_WORDS = {
    "federal reserve": 6, "fed ": 5, "fomc": 6, "powell": 5, "rate cut": 6,
    "rate hike": 6, "interest rate": 5, "inflation": 5, "cpi": 6, "ppi": 4,
    "jobs report": 6, "payroll": 6, "unemployment": 4, "gdp": 4, "recession": 5,
    "earnings": 4, "guidance": 3, "forecast": 2, "treasury": 3, "yield": 3,
    "tariff": 5, "trade deal": 4, "china": 3, "opec": 4, "oil": 3, "crude": 3,
    "acquisition": 4, "merger": 4, "acquire": 4, "ipo": 4, "bankruptcy": 4,
    "antitrust": 3, "lawsuit": 2, "sec charges": 4, "stocks": 2, "s&p 500": 4,
    "nasdaq": 3, "dow": 3, "bitcoin": 3, "ethereum": 2, "etf": 3, "crypto": 2,
    "ai ": 3, "artificial intelligence": 3, "nvidia": 3, "chip": 2,
    "housing": 3, "mortgage": 3, "consumer": 2, "retail sales": 4, "dollar": 2,
    "gold": 2, "bond": 2, "stimulus": 3, "shutdown": 4, "default": 4,
}

THEMES = [
    ("Rates & the Fed",      ["fed", "fomc", "powell", "rate", "yield", "treasury", "central bank"]),
    ("Inflation & prices",   ["inflation", "cpi", "ppi", "prices", "cost of living"]),
    ("Jobs & growth",        ["jobs", "payroll", "unemployment", "gdp", "recession", "hiring", "layoff"]),
    ("Earnings & companies", ["earnings", "guidance", "revenue", "profit", "quarterly"]),
    ("AI & tech",            ["ai ", "artificial intelligence", "nvidia", "chip", "semiconductor", "openai"]),
    ("Energy & commodities", ["oil", "opec", "crude", "natural gas", "gold", "copper", "energy"]),
    ("Deals & IPOs",         ["merger", "acquisition", "acquire", "ipo", "buyout", "takeover"]),
    ("Trade & geopolitics",  ["tariff", "china", "sanction", "trade war", "export", "geopolit"]),
    ("Crypto",               ["bitcoin", "ethereum", "crypto", "stablecoin", "blockchain"]),
    ("Housing",              ["housing", "mortgage", "home price", "real estate"]),
]

# ------------------------------------------------------------- market --------

# label, yahoo symbol, kind
INSTRUMENTS = [
    ("S&P 500",       "^GSPC",     "index"),
    ("Nasdaq",        "^IXIC",     "index"),
    ("Dow",           "^DJI",      "index"),
    ("Russell 2000",  "^RUT",      "index"),
    ("VIX",           "^VIX",      "level"),
    ("10-yr Treasury","^TNX",      "yield"),
    ("Dollar index",  "DX-Y.NYB",  "level"),
    ("WTI crude",     "CL=F",      "dollar"),
    ("Gold",          "GC=F",      "dollar"),
    ("EUR/USD",       "EURUSD=X",  "fx"),
    ("Bitcoin",       "BTC-USD",   "crypto"),
    ("Ethereum",      "ETH-USD",   "crypto"),
]

# ------------------------------------------------------------- fetching ------


def fetch(url, ua=BROWSER_UA, timeout=15, retries=1):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw
        except Exception as e:  # noqa: BLE001 — any network failure is tolerated
            last_err = e
            if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                break
            time.sleep(1.5)
    raise last_err


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(el):
    return html.unescape("".join(el.itertext())).strip() if el is not None else ""


def _clean_summary(s, limit=230):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(re.sub(r"Z$", "+00:00", s))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(raw, source_name, category, weight):
    """Parse RSS 2.0 or Atom bytes into item dicts. Raises on malformed XML."""
    root = ET.fromstring(raw)
    rtag = _strip_ns(root.tag)
    items = []
    if rtag in ("rss", "RDF"):
        nodes = [n for n in root.iter() if _strip_ns(n.tag) == "item"]
    else:  # atom
        nodes = [n for n in root.iter() if _strip_ns(n.tag) == "entry"]
    for node in nodes:
        fields = {}
        link = ""
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "link":
                href = child.get("href")
                if href:
                    rel = child.get("rel", "alternate")
                    if rel == "alternate" or not link:
                        link = href
                else:
                    link = _text(child) or link
            else:
                fields.setdefault(tag, child)
        title = _text(fields.get("title"))
        if not title:
            continue
        src = source_name
        if "source" in fields:  # Google News carries the real outlet
            real = _text(fields["source"])
            if real:
                src = real
                suffix = " - " + real
                if title.endswith(suffix):
                    title = title[: -len(suffix)].rstrip()
        published = None
        for key in ("pubDate", "published", "updated", "date"):
            if key in fields:
                published = _parse_date(_text(fields[key]))
                if published:
                    break
        summary = ""
        for key in ("description", "summary", "content"):
            if key in fields:
                summary = _clean_summary(_text(fields[key]))
                if summary:
                    break
        items.append({
            "title": re.sub(r"\s+", " ", title).strip(),
            "link": link.strip(),
            "source": src,
            "category": category,
            "published": published,
            "summary": summary,
            "weight": weight,
        })
    return items


def fetch_all_feeds():
    results, errors = [], []

    def one(feed):
        name, url, cat, w = feed
        raw = fetch(url)
        return parse_feed(raw, name, cat, w)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(one, f): f for f in FEEDS}
        for fut in as_completed(futs):
            name = futs[fut][0]
            try:
                results.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                errors.append((name, str(e)[:120]))

    filings = []
    try:
        raw = fetch(SEC_FEED[1], ua=SEC_UA)
        filings = parse_feed(raw, "SEC EDGAR", "filings", 1.0)
    except Exception as e:  # noqa: BLE001
        errors.append(("SEC EDGAR", str(e)[:120]))
    return results, filings, errors


# ----------------------------------------------------------- selection -------


def classify(title):
    t = " " + title.lower() + " "
    checks = [
        ("crypto",    ["bitcoin", "ethereum", "crypto", "stablecoin", "blockchain", "coinbase"]),
        ("economy",   ["fed ", "federal reserve", "fomc", "powell", "inflation", "cpi", "ppi",
                       "jobs", "payroll", "unemployment", "gdp", "recession", "tariff",
                       "treasury", "central bank", "rate cut", "rate hike", "economy"]),
        ("global",    ["china", "europe", "ecb", "japan", "u.k.", "germany", "india",
                       "emerging market", "ukraine", "middle east", "global"]),
        ("companies", ["earnings", "ipo", "merger", "acquisition", "acquire", "ceo",
                       "guidance", "shares of", "stock jumps", "stock falls", "profit", "revenue"]),
    ]
    for cat, words in checks:
        if any(w in t for w in words):
            return cat
    return "markets"


def score(item, now):
    t = " " + item["title"].lower() + " "
    s = 1.0
    for word, w in SCORE_WORDS.items():
        if word in t:
            s += w
    s *= item["weight"]
    age_h = 24.0
    if item["published"]:
        age_h = max(0.0, (now - item["published"]).total_seconds() / 3600)
    s *= 0.5 ** (age_h / 8.0)  # halve every 8 hours
    return s


def prepare_items(items, now):
    fresh, seen = [], {}
    for it in items:
        if it["published"] and (now - it["published"]) > timedelta(hours=MAX_AGE_HOURS):
            continue
        if len(it["title"]) < 15 or not it["link"]:
            continue
        fp = " ".join(re.findall(r"[a-z0-9]+", it["title"].lower())[:10])
        if fp in seen:
            if it["weight"] > seen[fp]["weight"]:
                seen[fp] = it
            continue
        seen[fp] = it
        fresh.append(it)
    fresh = list(seen.values())
    for it in fresh:
        it["score"] = score(it, now)
        it["display_category"] = it["category"] if it["category"] != "top" else classify(it["title"])
    fresh.sort(key=lambda x: -x["score"])
    return fresh


def theme_scan(items):
    counts = []
    for name, words in THEMES:
        n = sum(1 for it in items
                if any(w in (" " + it["title"].lower() + " ") for w in words))
        if n:
            counts.append({"theme": name, "count": n})
    counts.sort(key=lambda x: -x["count"])
    return counts


# ------------------------------------------------------------- markets -------


def fmt_value(value, kind):
    if kind == "yield":
        return f"{value / 10:.2f}%"
    if kind == "fx":
        return f"{value:.4f}"
    if kind == "dollar":
        return f"${value:,.2f}"
    if kind == "crypto":
        return f"${value:,.0f}"
    return f"{value:,.2f}"


def make_tile(label, symbol, kind, last, prev, spark, asof=None):
    if kind == "yield":
        delta_txt = f"{(last - prev) * 10:+.0f} bp"
    else:
        delta_txt = f"{(last - prev) / prev * 100:+.2f}%"
    tile = {
        "label": label, "symbol": symbol, "kind": kind,
        "value": last, "prev": prev,
        "value_txt": fmt_value(last, kind),
        "delta": (last - prev) / prev * 100,
        "delta_txt": delta_txt,
        "spark": spark,
    }
    if asof:
        tile["asof"] = asof
    return tile


def try_yahoo():
    """Fetch instruments from Yahoo's chart API with one polite session.

    Primes a cookie the way any browser would, spaces requests out, and gives
    up for the whole run after two consecutive rate-limit responses so we never
    hammer a throttled endpoint. Fallback sources cover what's missed.
    """
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", BROWSER_UA), ("Accept", "*/*")]
    try:
        opener.open("https://fc.yahoo.com", timeout=10)
    except Exception:  # noqa: BLE001 — a 404 here is expected; we only want the cookie
        pass
    out, errors, strikes = {}, [], 0
    for label, ysym, kind in INSTRUMENTS:
        if strikes >= 2:
            errors.append(("Yahoo", "rate-limited; using fallback sources"))
            break
        time.sleep(0.7)
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ysym)}?range=1mo&interval=1d")
        try:
            with opener.open(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data["chart"]["result"][0]
            meta = result["meta"]
            closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
            last = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            if last is None or len(closes) < 2:
                raise ValueError("no price data")
            prev = meta.get("regularMarketPreviousClose") or meta.get("previousClose")
            if not prev:
                # if the final bar is the live session, the prior bar is the reference
                prev = closes[-2] if abs(last - closes[-1]) / last < 0.02 else closes[-1]
            out[label] = make_tile(label, ysym, kind, float(last), float(prev),
                                   [float(c) for c in closes[-23:]])
            strikes = 0
        except Exception as e:  # noqa: BLE001
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                strikes += 1
            else:
                errors.append((f"Yahoo {label}", str(e)[:100]))
    return out, errors


FRED_FALLBACK = {
    # label -> (FRED series id, multiplier to match Yahoo conventions, label override)
    "S&P 500":        ("SP500",      1.0, None),
    "Nasdaq":         ("NASDAQCOM",  1.0, None),
    "Dow":            ("DJIA",       1.0, None),
    "VIX":            ("VIXCLS",     1.0, None),
    "10-yr Treasury": ("DGS10",     10.0, None),   # DGS10 is e.g. 4.73; tiles use ^TNX-style x10
    "Dollar index":   ("DTWEXBGS",   1.0, "Dollar (broad)"),
    "WTI crude":      ("DCOILWTICO", 1.0, None),
}


def fred_series(series_id):
    raw = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                ua=BOT_UA).decode("utf-8")
    rows = []
    for line in raw.strip().splitlines()[1:]:
        date, _, val = line.partition(",")
        try:
            rows.append((date, float(val)))
        except ValueError:
            continue
    if len(rows) < 2:
        raise ValueError(f"no FRED data for {series_id}")
    return rows[-23:]


def frankfurter_eurusd():
    start = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    raw = fetch(f"https://api.frankfurter.dev/v1/{start}..?base=EUR&symbols=USD", ua=BOT_UA)
    rates = json.loads(raw.decode("utf-8"))["rates"]
    rows = [(d, v["USD"]) for d, v in sorted(rates.items())][-23:]
    if len(rows) < 2:
        raise ValueError("no frankfurter data")
    return rows


def coingecko_coin(coin_id):
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days=30&interval=daily")
    prices = json.loads(fetch(url, ua=BOT_UA).decode("utf-8"))["prices"]
    closes = [p[1] for p in prices][-23:]
    if len(closes) < 2:
        raise ValueError("no coingecko data")
    return closes


def _pretty_date(iso_day):
    return datetime.fromisoformat(iso_day).strftime("%b %-d")


def fetch_markets():
    tiles, errors = try_yahoo()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")

    for label, ysym, kind in INSTRUMENTS:
        if label in tiles:
            continue
        if label in FRED_FALLBACK:
            series_id, mult, override = FRED_FALLBACK[label]
            try:
                rows = fred_series(series_id)
                vals = [v * mult for _, v in rows]
                asof = rows[-1][0]
                tiles[label] = make_tile(override or label, series_id, kind,
                                         vals[-1], vals[-2], vals,
                                         asof=None if asof == today else _pretty_date(asof))
            except Exception as e:  # noqa: BLE001
                errors.append((f"FRED {label}", str(e)[:100]))
        elif label == "EUR/USD":
            try:
                rows = frankfurter_eurusd()
                vals = [v for _, v in rows]
                asof = rows[-1][0]
                tiles[label] = make_tile(label, "EURUSD", kind, vals[-1], vals[-2], vals,
                                         asof=None if asof == today else _pretty_date(asof))
            except Exception as e:  # noqa: BLE001
                errors.append(("Frankfurter EUR/USD", str(e)[:100]))
        elif kind == "crypto":
            coin = {"Bitcoin": "bitcoin", "Ethereum": "ethereum"}.get(label)
            try:
                closes = coingecko_coin(coin)
                tiles[label] = make_tile(label, coin, kind, closes[-1], closes[-2], closes)
            except Exception as e:  # noqa: BLE001
                errors.append((f"CoinGecko {label}", str(e)[:100]))

    ordered = [tiles[i[0]] for i in INSTRUMENTS if i[0] in tiles]
    return ordered, errors


FRED_RELEASE_LABELS = {
    "Consumer Price Index": "CPI (inflation)",
    "Producer Price Index": "PPI (producer prices)",
    "Employment Situation": "Jobs report",
    "Gross Domestic Product": "GDP",
    "Personal Income and Outlays": "PCE inflation & spending",
    "Advance Monthly Sales for Retail and Food Services": "Retail sales",
    "Job Openings and Labor Turnover Survey": "JOLTS job openings",
    "New Residential Construction": "Housing starts",
    "New Residential Sales": "New home sales",
    "Existing Home Sales": "Existing home sales",
    "Consumer Credit": "Consumer credit",
}


def fred_calendar():
    """Upcoming marquee economic releases (needs a FRED API key; else empty)."""
    key = smallcap.read_key("FRED_API_KEY", "fred.key")
    if not key:
        return []
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=10)
    url = ("https://api.stlouisfed.org/fred/releases/dates?"
           + urllib.parse.urlencode({
               "api_key": key, "file_type": "json",
               "include_release_dates_with_no_data": "true",
               "realtime_start": today.isoformat(),
               "realtime_end": end.isoformat(),
               "limit": "1000", "sort_order": "asc",
           }))
    rows = json.loads(fetch(url, ua=BOT_UA).decode("utf-8")).get("release_dates", [])
    out, seen = [], set()
    for r in rows:
        label = FRED_RELEASE_LABELS.get(r.get("release_name", ""))
        d = r.get("date", "")
        if not label or not (today.isoformat() <= d <= end.isoformat()):
            continue
        if (label, d) in seen:
            continue
        seen.add((label, d))
        out.append({"date": d, "label": label})
    out.sort(key=lambda x: x["date"])
    return out[:10]


def tape_line(tiles):
    by = {t["label"]: t for t in tiles}
    bits = []
    idx = [(l, by[l]) for l in ("S&P 500", "Nasdaq", "Dow") if l in by]
    if idx:
        bits.append(", ".join(f"{l} {t['delta_txt']}" for l, t in idx))
    if "10-yr Treasury" in by:
        t = by["10-yr Treasury"]
        bits.append(f"the 10-year Treasury at {t['value_txt']} ({t['delta_txt']})")
    if "WTI crude" in by:
        t = by["WTI crude"]
        bits.append(f"WTI crude {t['value_txt']} ({t['delta_txt']})")
    if "Gold" in by:
        t = by["Gold"]
        bits.append(f"gold {t['value_txt']} ({t['delta_txt']})")
    if "Bitcoin" in by:
        t = by["Bitcoin"]
        bits.append(f"bitcoin {t['value_txt']} ({t['delta_txt']})")
    return "; ".join(bits) + "." if bits else ""


# ------------------------------------------------------------- render --------

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --hair: #e1e0d9; --border: rgba(11,11,11,.10);
  --accent: #2a78d6; --spark: #9ec5f4;
  --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --hair: #2c2c2a; --border: rgba(255,255,255,.10);
    --accent: #3987e5; --spark: #1c5cab;
    --up: #0ca30c; --down: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
  --hair: #2c2c2a; --border: rgba(255,255,255,.10);
  --accent: #3987e5; --spark: #1c5cab;
  --up: #0ca30c; --down: #e66767;
}
body {
  background: var(--page); color: var(--ink);
  font-family: "Libre Franklin", -apple-system, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; text-decoration-color: var(--accent); }

.masthead { display: flex; align-items: baseline; justify-content: space-between;
  flex-wrap: wrap; gap: 8px 20px; padding-bottom: 14px; border-bottom: 2px solid var(--ink); }
.brand { font-family: "Besley", Georgia, serif; font-weight: 800;
  font-size: clamp(28px, 4vw, 40px); letter-spacing: -0.01em; }
.brand .tick { color: var(--accent); }
.kicker { color: var(--ink2); font-size: 13.5px; }
.updated { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 12px; color: var(--muted); }

.tape { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 10px; margin: 22px 0 8px; }
.tile { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 12px 8px; }
.tlabel { font-size: 11px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--muted); }
.tvalue { font-size: 21px; font-weight: 600; margin-top: 2px; }
.tdelta { font-size: 12.5px; font-weight: 600; margin-top: 1px;
  font-family: "IBM Plex Mono", ui-monospace, monospace; }
.up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--muted); }
.tasof { font-size: 10.5px; color: var(--muted); margin-top: 1px; }
.spark { display: block; width: 100%; height: 34px; margin-top: 6px; }

.section-head { font-size: 12px; font-weight: 700; letter-spacing: 0.09em;
  text-transform: uppercase; color: var(--ink2);
  border-top: 1px solid var(--hair); padding-top: 10px; margin-bottom: 6px; }

.brief { margin: 30px 0 6px; max-width: 74ch; }
.brief-title { font-family: "Besley", Georgia, serif; font-weight: 700;
  font-size: 24px; margin-bottom: 2px; }
.brief-date { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 12px; color: var(--muted); margin-bottom: 14px; }
.tape-line { font-size: 15px; color: var(--ink2); margin-bottom: 14px; }
.take { background: var(--surface); border: 1px solid var(--border);
  border-left: 3px solid var(--accent); border-radius: 8px;
  padding: 14px 16px; margin-bottom: 22px; }
.take-label { font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--accent); margin-bottom: 6px; }
.take p { margin-bottom: 8px; color: var(--ink); }
.take p:last-child { margin-bottom: 0; }
.take .attribution { font-size: 12px; color: var(--muted); margin-top: 6px; }

.story { padding: 12px 0; border-bottom: 1px solid var(--hair); }
.story h3 { font-family: "Besley", Georgia, serif; font-weight: 700;
  font-size: 17.5px; line-height: 1.35; }
.story p { color: var(--ink2); font-size: 14px; margin-top: 3px; }
.meta { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px; color: var(--muted); margin-top: 4px; }

.columns { display: grid; grid-template-columns: repeat(auto-fill, minmax(262px, 1fr));
  gap: 8px 30px; margin-top: 34px; }
.item { padding: 9px 0; border-bottom: 1px solid var(--hair); }
.item a { font-weight: 600; font-size: 14px; line-height: 1.4; display: block; }
.filing-list .item a { font-weight: 400;
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12.5px; }

.mastright { text-align: right; }
.nav { font-size: 12.5px; margin-top: 4px; }
.nav a { color: var(--accent); font-weight: 600; }
.navcur { color: var(--muted); font-weight: 600; }
.navsep { color: var(--muted); }

.coverage { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
  color: var(--muted); margin: 18px 0 10px; }
.note-box { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; margin: 18px 0; color: var(--ink2);
  max-width: 74ch; }
.tblwrap { overflow-x: auto; margin: 10px 0 26px; }
table.screen { width: 100%; border-collapse: collapse; font-size: 13px;
  font-variant-numeric: tabular-nums; }
table.screen th { font-size: 10.5px; font-weight: 700; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--muted); text-align: right;
  padding: 6px 8px; border-bottom: 2px solid var(--ink); }
table.screen td { padding: 7px 8px; border-bottom: 1px solid var(--hair);
  text-align: right; white-space: nowrap; }
table.screen th.l, table.screen td.l { text-align: left; }
table.screen td.tick { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-weight: 500; }
.duo { display: grid; grid-template-columns: repeat(auto-fill, minmax(262px, 1fr));
  gap: 8px 30px; margin-top: 10px; }
.flag { display: inline-block; font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 10px; border: 1px solid var(--border); border-radius: 4px;
  padding: 0 4px; margin-left: 4px; color: var(--ink2); }
.flag.new { color: var(--accent); border-color: var(--accent); }
.flag.ins { color: var(--up); border-color: var(--up); }
.method { font-size: 12.5px; color: var(--muted); max-width: 90ch;
  border-top: 1px solid var(--hair); padding-top: 12px; margin-top: 30px; }

footer { margin-top: 44px; border-top: 2px solid var(--ink); padding-top: 14px;
  font-size: 12.5px; color: var(--muted); max-width: 90ch; }
footer p { margin-bottom: 8px; }
"""

FONTS_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
              '?family=Besley:ital,wght@0,700;0,800;1,700'
              '&family=Libre+Franklin:wght@400;600;700'
              '&family=IBM+Plex+Mono:wght@400;500&display=swap">')


def esc(s):
    return html.escape(s or "", quote=True)


def spark_svg(closes):
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    w, h, pad = 120, 34, 4
    pts = []
    for i, c in enumerate(closes):
        x = pad + (w - 2 * pad) * i / (len(closes) - 1)
        y = pad + (h - 2 * pad) * (1 - (c - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    ex, ey = pts[-1].split(",")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'role="img" aria-label="one-month trend">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="var(--spark)" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{ex}" cy="{ey}" r="3.5" fill="var(--accent)" '
            f'stroke="var(--surface)" stroke-width="2"/></svg>')


def delta_class(delta):
    if delta > 0.005:
        return "up"
    if delta < -0.005:
        return "down"
    return "flat"


def delta_arrow(delta):
    if delta > 0.005:
        return "▲"
    if delta < -0.005:
        return "▼"
    return "▬"


def time_ago(iso, now):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    mins = int((now - dt).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}m ago"
    if mins < 60 * 24:
        return f"{mins // 60}h ago"
    return f"{mins // (60 * 24)}d ago"


def masthead_html(date_line, nav=None):
    nav_html = ""
    if nav:
        sep = ' <span class="navsep">·</span> '
        links = []
        for key, label, href in (("dashboard", "Dashboard", "index.html"),
                                 ("smallcap", "Small caps", "smallcap.html"),
                                 ("archive", "Archive", "archive/")):
            if key == nav:
                links.append(f'<span class="navcur">{label}</span>')
            else:
                links.append(f'<a href="{href}">{label}</a>')
        nav_html = f'<nav class="nav">{sep.join(links)}</nav>'
    return ('<header class="masthead"><div>'
            '<div class="brand">Basis<span class="tick">/</span>Points</div>'
            '<div class="kicker">Markets, distilled. An automated investor brief.</div></div>'
            f'<div class="mastright"><div class="updated">updated {esc(date_line)}</div>'
            f'{nav_html}</div></header>')


def render_tiles(tiles):
    out = []
    for t in tiles:
        cls = delta_class(t["delta"])
        asof = (f'<div class="tasof">as of {esc(t["asof"])}</div>'
                if t.get("asof") else "")
        out.append(
            f'<div class="tile"><div class="tlabel">{esc(t["label"])}</div>'
            f'<div class="tvalue">{esc(t["value_txt"])}</div>'
            f'<div class="tdelta {cls}">{delta_arrow(t["delta"])} {esc(t["delta_txt"])}</div>'
            f'{asof}{spark_svg(t.get("spark", []))}</div>')
    return f'<div class="tape">{"".join(out)}</div>' if out else ""


def render_take(take_paragraphs, themes, auto):
    if take_paragraphs:
        body = "".join(f"<p>{esc(p)}</p>" for p in take_paragraphs)
        label = "The take"
        note = '<div class="attribution">Written by the editor’s desk (Claude), from today’s data.</div>'
    else:
        if not themes:
            return ""
        tops = ", ".join(f'{t["theme"].lower()} ({t["count"]} stories)' for t in themes[:4])
        body = (f"<p>Automated scan of today’s coverage — the most-covered themes "
                f"across all sources right now: {esc(tops)}.</p>")
        label = "Signal scan"
        note = '<div class="attribution">Auto-generated from headline frequency; an edited take appears when the desk is in session.</div>'
    return (f'<div class="take"><div class="take-label">{label}</div>{body}{note}</div>')


def render_stories(stories, now):
    out = []
    for it in stories:
        blurb = f'<p>{esc(it["summary"])}</p>' if it.get("summary") else ""
        meta = " · ".join(x for x in [esc(it["source"]), time_ago(it.get("published"), now)] if x)
        out.append(
            f'<article class="story"><h3><a href="{esc(it["link"])}" target="_blank" '
            f'rel="noopener">{esc(it["title"])}</a></h3>{blurb}'
            f'<div class="meta">{meta}</div></article>')
    return "".join(out)


def render_column(title, items, now, filing=False):
    lis = []
    for it in items:
        meta = " · ".join(x for x in [esc(it["source"]), time_ago(it.get("published"), now)] if x)
        lis.append(f'<div class="item"><a href="{esc(it["link"])}" target="_blank" '
                   f'rel="noopener">{esc(it["title"])}</a><div class="meta">{meta}</div></div>')
    cls = "col filing-list" if filing else "col"
    return (f'<section class="{cls}"><h2 class="section-head">{esc(title)}</h2>'
            f'{"".join(lis)}</section>')


def render_page(data, mode="site"):
    """mode: 'site' (full html doc), 'artifact' (body-only + title), 'archive'."""
    now = datetime.fromisoformat(data["generated_at"])
    local = now.astimezone()
    date_line = local.strftime("%A, %B %-d, %Y · %-I:%M %p %Z")

    tiles_html = render_tiles(data["market"])
    tape = (f'<p class="tape-line"><strong>The tape:</strong> {esc(data["tape_line"])}</p>'
            if data.get("tape_line") else "")
    take_html = render_take(data.get("take_paragraphs"), data.get("themes"), True)
    stories_html = render_stories(data["top"], now)

    columns = []
    for key, title in CATEGORIES:
        items = data["columns"].get(key, [])
        if items:
            columns.append(render_column(title, items, now))
    if data.get("econ_calendar"):
        columns.append(render_econ_column(data["econ_calendar"]))
    if data.get("filings"):
        columns.append(render_column("Fresh SEC 8-K filings", data["filings"], now, filing=True))

    src_names = sorted({f[0] for f in FEEDS} | {"SEC EDGAR"})
    footer = (
        '<footer>'
        '<p><strong>Not investment advice.</strong> Basis Points is an automated news '
        'and data aggregator for general information only. Headlines and excerpts belong to '
        'their publishers and link to the original articles. Market data is delayed and '
        'provided as-is by public endpoints (Yahoo Finance, Stooq, CoinGecko); verify before '
        'acting on it.</p>'
        f'<p>Sources: {esc(", ".join(src_names))}.</p>'
        f'<p>Generated {esc(date_line)} · refreshes on a schedule.</p>'
        '</footer>')

    masthead = masthead_html(date_line, nav="dashboard" if mode == "site" else None)

    sc = data.get("smallcap") or {}
    sc_leaders = ""
    if sc.get("screen"):
        tops = ", ".join(f'{r["ticker"]} {r["score"]:.0f}' for r in sc["screen"][:3])
        link = ' · <a href="smallcap.html">full screen →</a>' if mode == "site" else ""
        sc_leaders = (f'<p class="tape-line"><strong>Small-cap screen leaders:</strong> '
                      f'{esc(tops)} (composite score){link}</p>')

    brief = (
        '<section class="brief"><h2 class="brief-title">The Brief</h2>'
        f'<div class="brief-date">{esc(date_line)}</div>'
        f'{tape}{sc_leaders}{take_html}{stories_html}</section>')

    if mode == "archive":
        body = f'<div class="wrap">{masthead}{tiles_html}{brief}{footer}</div>'
    else:
        body = (f'<div class="wrap">{masthead}{tiles_html}{brief}'
                f'<div class="columns">{"".join(columns)}</div>{footer}</div>')

    if mode == "artifact":
        return (f'<title>Basis Points</title>\n{FONTS_LINK}\n'
                f'<style>{CSS}</style>\n{body}\n')

    refresh = '<meta http-equiv="refresh" content="900">' if mode == "site" else ""
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{refresh}<title>Basis Points — investor brief</title>{FONTS_LINK}'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def render_econ_column(rows):
    lis = []
    for r in rows:
        day = datetime.fromisoformat(r["date"]).strftime("%a %b %-d")
        lis.append(f'<div class="item"><strong>{esc(r["label"])}</strong>'
                   f'<div class="meta">{esc(day)}</div></div>')
    return ('<section class="col"><h2 class="section-head">Economic calendar</h2>'
            f'{"".join(lis)}</section>')


def _fmt_mcap(musd):
    return f"${musd / 1000:.1f}B" if musd >= 1000 else f"${musd:.0f}M"


def render_smallcap_page(data):
    now = datetime.fromisoformat(data["generated_at"])
    date_line = now.astimezone().strftime("%A, %B %-d, %Y · %-I:%M %p %Z")
    sc = data.get("smallcap") or {}
    cov = sc.get("coverage") or {}
    parts = [masthead_html(date_line, nav="smallcap")]

    if sc.get("note") == "waiting-for-key":
        parts.append(
            '<div class="note-box"><strong>Scorecard pending.</strong> The small-cap '
            'engine is installed and the company universe is loaded; measurement begins '
            'automatically once the Finnhub key is added.</div>')
    if cov:
        parts.append(
            f'<div class="coverage">universe {cov.get("universe", 0):,} companies · '
            f'profiled {cov.get("profiled", 0):,} · in size band {cov.get("in_band", 0):,} · '
            f'fully measured {cov.get("measured", 0):,} · '
            f'passing filters {cov.get("scored", 0):,} · '
            f'below revenue floor {cov.get("below_floor", 0):,}</div>')

    screen = sc.get("screen") or []
    ev = sc.get("evaluation") or {}
    if ev:
        bits = []
        for h, lab in (("1w", "1 week out"), ("4w", "4 weeks out")):
            if h in ev:
                e = ev[h]
                bits.append(f'{lab}: {e["excess"]:+.2f}% vs the Russell 2000 ETF '
                            f'(over {e["days"]} screening day'
                            f'{"s" if e["days"] != 1 else ""})')
        parts.append('<div class="note-box"><strong>Live track record.</strong> '
                     'Average forward return of published screens minus the benchmark — '
                     + "; ".join(bits) +
                     '. Accumulated from real daily screens, not a backtest.</div>')
    elif screen:
        parts.append('<div class="note-box"><strong>Live track record:</strong> '
                     'collecting. Each day’s screen is logged; the first 1-week '
                     'reading appears once a logged screen is a week old. '
                     'A real forward record, not a backtest.</div>')
    if screen:
        head = ('<tr><th class="l">#</th><th class="l">Ticker</th>'
                '<th class="l">Company</th><th class="l">Industry</th><th>Mkt cap</th>'
                '<th>Rev growth</th><th>13-wk</th><th>vs 52w high</th>'
                '<th>Today</th><th>Score</th><th class="l">Flags</th></tr>')
        trs = []
        for i, r in enumerate(screen, 1):
            dp = r.get("dp")
            dp_td = (f'<td class="{delta_class(dp)}">{dp:+.1f}%</td>'
                     if dp is not None else "<td>—</td>")
            fh52 = (f'{r["from_high"]:+.1f}%' if r.get("from_high") is not None else "—")
            sub = r.get("sub") or {}
            sub_t = (f'growth {sub.get("g", "?")} · momentum {sub.get("m", "?")} · '
                     f'quality {sub.get("q", "?")}')
            flags = "".join(
                f'<span class="flag {"new" if f == "new" else "ins" if f == "ins+" else ""}">'
                f'{esc(f)}</span>' for f in (r.get("flags") or []))
            trs.append(
                f'<tr title="{esc(sub_t)}"><td class="l">{i}</td>'
                f'<td class="l tick">{esc(r["ticker"])}</td>'
                f'<td class="l">{esc(r["name"])}</td><td class="l">{esc(r["ind"])}</td>'
                f'<td>{_fmt_mcap(r["mcap"])}</td><td>{r["rev_g"]:+.1f}%</td>'
                f'<td>{r["r13"]:+.1f}%</td><td>{fh52}</td>'
                f'{dp_td}<td><strong>{r["score"]:.1f}</strong></td>'
                f'<td class="l">{flags}</td></tr>')
        parts.append('<h2 class="brief-title">Growth screen — top 25</h2>'
                     f'<div class="tblwrap"><table class="screen">{head}{"".join(trs)}'
                     '</table></div>')
    elif not sc.get("note") and cov:
        parts.append('<div class="note-box">The scorecard is still building coverage — '
                     'the ranked screen appears once enough companies are fully '
                     'measured. Check back within a day.</div>')

    cols = []

    def mover_col(title, rows):
        lis = "".join(
            f'<div class="item"><strong>{esc(r["ticker"])}</strong> · {esc(r["name"])}'
            f'<div class="meta"><span class="{delta_class(r["dp"])}">{r["dp"]:+.2f}%</span>'
            f' · ${r["px"]:,.2f}</div></div>' for r in rows)
        return f'<section class="col"><h2 class="section-head">{title}</h2>{lis}</section>'

    if sc.get("movers_up"):
        cols.append(mover_col("Movers — up", sc["movers_up"]))
    if sc.get("movers_down"):
        cols.append(mover_col("Movers — down", sc["movers_down"]))
    if sc.get("earnings"):
        hour_map = {"bmo": "before open", "amc": "after close", "dmh": "during hours"}
        lis = []
        for r in sc["earnings"]:
            day = datetime.fromisoformat(r["date"]).strftime("%a %b %-d")
            hour = f' · {hour_map[r["hour"]]}' if r.get("hour") in hour_map else ""
            lis.append(f'<div class="item"><strong>{esc(r["ticker"])}</strong> · '
                       f'{esc(r["name"])}<div class="meta">{esc(day)}{hour}</div></div>')
        cols.append('<section class="col"><h2 class="section-head">Earnings this week'
                    f'</h2>{"".join(lis)}</section>')
    if sc.get("news"):
        lis = "".join(
            f'<div class="item"><a href="{esc(n["link"])}" target="_blank" rel="noopener">'
            f'{esc(n["title"])}</a><div class="meta">{esc(n["ticker"])} · '
            f'{esc(n["source"])}</div></div>' for n in sc["news"])
        cols.append(f'<section class="col"><h2 class="section-head">In the news</h2>{lis}</section>')
    if sc.get("below_floor"):
        lis = []
        for r in sc["below_floor"]:
            rg = f'{r["rev_g"]:+.0f}%' if r.get("rev_g") is not None else "—"
            r13 = f'{r["r13"]:+.0f}%' if r.get("r13") is not None else "—"
            lis.append(f'<div class="item"><strong>{esc(r["ticker"])}</strong> · '
                       f'{esc(r["name"])}<div class="meta">rev ${r["rev_ttm"]:.0f}M · '
                       f'growth {rg} · 13-wk {r13}</div></div>')
        cols.append('<section class="col"><h2 class="section-head">Below the revenue '
                    f'floor (unranked)</h2>{"".join(lis)}'
                    '<div class="meta" style="padding-top:8px">Under $50M trailing '
                    'revenue — growth percentages on tiny bases are unreliable, so '
                    'these are listed, never scored.</div></section>')
    if cols:
        parts.append(f'<div class="duo">{"".join(cols)}</div>')

    parts.append(
        '<div class="method"><strong>Methodology (model v2).</strong> Eligibility: U.S. '
        'listed common stocks, market value $300M–$2B, price ≥ $2, 10-day average volume '
        '≥ 50k shares, trailing-12-month revenue ≥ $50M, no over-the-counter listings. '
        'Composite score = 40% growth (0.7 × trailing revenue growth + 0.3 × acceleration, '
        'the latest quarter’s growth vs the trailing year) + 40% momentum (blended 13/26-week '
        'return divided by 3-month volatility, so calm advances outrank violent spikes) + '
        '20% quality (cash-flow funding or months of runway, margin direction, and low '
        'shareholder dilution) — each factor percentile-ranked within the eligible set; '
        'missing sub-measures rank neutral. Publication: at most 5 names per industry; '
        'names new to the candidate list carry a one-day 3% score penalty to reduce churn. '
        'Hover a row for its factor breakdown. Flags: <em>new</em> = entered the list today; '
        '<em>E-Nd</em> = reports earnings in N days; <em>ins+</em> = net insider open-market '
        'buying in the last 30 days. The track record above accumulates forward — never '
        'backfilled. Data: SEC (universe), Finnhub (measures); quotes may be a few hours '
        'old. Facts by fixed rules — <strong>not investment advice</strong>.</div>')
    parts.append(f'<footer><p>Generated {esc(date_line)}.</p></footer>')

    body = f'<div class="wrap">{"".join(parts)}</div>'
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="900">'
            f'<title>Basis Points — small-cap growth screen</title>{FONTS_LINK}'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def render_archive_index(archive_dir):
    pages = sorted(archive_dir.glob("2*.html"), reverse=True)
    lis = "".join(
        f'<div class="item"><a href="{p.name}">{p.stem}</a></div>' for p in pages)
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Basis Points — archive</title>{FONTS_LINK}<style>{CSS}</style>'
            f'</head><body><div class="wrap"><header class="masthead">'
            f'<div class="brand">Basis<span class="tick">/</span>Points</div>'
            f'<div class="updated">archive</div></header>'
            f'<section class="col" style="max-width:40ch">{lis}</section>'
            f'</div></body></html>')


# --------------------------------------------------------------- main --------


def load_take():
    take = DATA / "take.md"
    if not take.exists():
        return None
    age = time.time() - take.stat().st_mtime
    if age > 24 * 3600:
        return None
    text = take.read_text(encoding="utf-8").strip()
    if not text:
        return None
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if p]


def build_data():
    now = datetime.now(timezone.utc)
    items, filings, feed_errors = fetch_all_feeds()
    tiles, market_errors = fetch_markets()
    fresh = prepare_items(items, now)

    top = fresh[:TOP_COUNT]
    top_links = {it["link"] for it in top}
    columns = {}
    for key, _title in CATEGORIES:
        cat_items = [it for it in fresh
                     if it["display_category"] == key and it["link"] not in top_links]
        columns[key] = cat_items[:PER_COLUMN]

    filings_fresh = []
    for f in filings:
        if f["published"] and (now - f["published"]) > timedelta(hours=24):
            continue
        f["title"] = re.sub(r"\s*\(\d{10}\)\s*", " ", f["title"]).strip()
        filings_fresh.append(f)

    def slim(it):
        return {
            "title": it["title"], "link": it["link"], "source": it["source"],
            "published": it["published"].isoformat() if it["published"] else None,
            "summary": it.get("summary", ""),
        }

    try:
        sc_summary, sc_calls = smallcap.update()
        sc_summary["news"] = smallcap.match_news([slim(i) for i in fresh[:150]])
    except Exception as e:  # noqa: BLE001 — the brief must render even if this fails
        sc_summary, sc_calls = None, 0
        market_errors.append(("Small-cap engine", str(e)[:120]))

    try:
        econ = fred_calendar()
    except Exception as e:  # noqa: BLE001
        econ = []
        market_errors.append(("FRED calendar", str(e)[:120]))

    data = {
        "generated_at": now.isoformat(),
        "market": tiles,
        "tape_line": tape_line(tiles),
        "themes": theme_scan(fresh),
        "top": [slim(i) for i in top],
        "columns": {k: [slim(i) for i in v] for k, v in columns.items()},
        "filings": [slim(f) for f in filings_fresh[:8]],
        "smallcap": sc_summary,
        "econ_calendar": econ,
        "stats": {
            "feeds_total": len(FEEDS) + 1,
            "feeds_failed": len(feed_errors) + len(market_errors),
            "items": len(fresh),
            "smallcap_calls": sc_calls,
            "feed_errors": feed_errors,
            "market_errors": market_errors,
        },
    }
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render-only", action="store_true",
                    help="re-render pages from data/latest.json without fetching")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)
    (SITE / "archive").mkdir(exist_ok=True)

    if args.render_only:
        data = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    else:
        data = build_data()
        (DATA / "latest.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    data["take_paragraphs"] = load_take()

    (SITE / "index.html").write_text(render_page(data, "site"), encoding="utf-8")
    (SITE / "artifact.html").write_text(render_page(data, "artifact"), encoding="utf-8")
    (SITE / "smallcap.html").write_text(render_smallcap_page(data), encoding="utf-8")

    day = datetime.fromisoformat(data["generated_at"]).astimezone().strftime("%Y-%m-%d")
    (SITE / "archive" / f"{day}.html").write_text(
        render_page(data, "archive"), encoding="utf-8")
    (SITE / "archive" / "index.html").write_text(
        render_archive_index(SITE / "archive"), encoding="utf-8")

    s = data.get("stats", {})
    cov = (data.get("smallcap") or {}).get("coverage") or {}
    print(f"ok: {s.get('items', '?')} headlines, {len(data['market'])}/{len(INSTRUMENTS)} "
          f"instruments, {len(data.get('filings', []))} filings; smallcap: "
          f"{cov.get('profiled', 0):,}/{cov.get('universe', 0):,} profiled, "
          f"{cov.get('in_band', 0):,} in band, {cov.get('scored', 0):,} scored "
          f"({s.get('smallcap_calls', 0)} calls)")
    for name, err in s.get("feed_errors", []) + s.get("market_errors", []):
        print(f"  warn: {name}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
