#!/usr/bin/env python3
"""
Basis Points — an automated small-cap growth rating system.

The published product is the ranked growth screen (see smallcap.py for the
model). This pipeline supplies it: news feeds and press-release wires are
collected purely as analysis inputs matched against the small-cap universe
(there is no client-facing news product), market data provides a slim macro
context strip, and everything renders to one page:

  site/index.html            the screen (also served as site/smallcap.html)
  data/latest.json           structured data from the run

Standard library only — no packages to install. Safe to run on a schedule.

Usage:
  python3 pipeline.py               # full run: fetch + render
  python3 pipeline.py --render-only # re-render from data/latest.json
"""

import argparse
import gzip
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import decision
import portfolio
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

# Hard ceilings on untrusted input. A feed host can serve anything it likes;
# without limits one hostile response can exhaust the unattended runner.
MAX_FETCH_BYTES = 8 * 1024 * 1024        # far above any real feed
MAX_UNZIPPED_BYTES = 32 * 1024 * 1024    # a small gzip can expand to gigabytes
MAX_TITLE_CHARS = 300                    # also bounds the filing-title regex

# The published page runs no JavaScript at all, so forbidding scripts outright
# costs nothing and neutralises any escaping mistake, present or future.
CSP = ("default-src 'none'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'")

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
    # press-release wires: small companies announce directly here, no journalist
    # required — matched against the band as signals, never shown as headlines
    ("GlobeNewswire",   "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies", "wire", 0.8),
    ("PR Newswire",     "https://www.prnewswire.com/rss/news-releases-list.rss", "wire",      0.8),
]

# SEC EDGAR "current filings" atom feeds, all matched against the small-cap band
# as signals (never scored). The type filter is unreliable for schedule forms,
# so activist stakes use the low-volume type=SC prefix and are sorted out by
# form in smallcap.record_filings.
SEC_FILING_FEEDS = [
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC&company=&dateb=&owner=include&count=100&output=atom",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-1&company=&dateb=&owner=include&count=100&output=atom",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B&company=&dateb=&owner=include&count=100&output=atom",
]

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


def _scrub(msg):
    """Strip any API key out of text bound for a public file. Harmless today —
    urllib's errors don't quote the URL — but the FRED and Finnhub request URLs
    carry the key in their query string, so one refactor could leak it."""
    out = str(msg)[:200]
    for env, fname in (("FINNHUB_API_KEY", "finnhub.key"),
                       ("FRED_API_KEY", "fred.key")):
        k = smallcap.read_key(env, fname)
        if k and len(k) >= 8:
            out = out.replace(k, "***")
    return out[:120]


def _gunzip(raw, limit=MAX_UNZIPPED_BYTES):
    """Decompress with a ceiling: a few kilobytes of hostile gzip can expand to
    gigabytes and take the runner out of memory."""
    out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, limit + 1)
    if len(out) > limit:
        raise ValueError("compressed response expands past the size limit")
    return out


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
                raw = resp.read(MAX_FETCH_BYTES + 1)
            if len(raw) > MAX_FETCH_BYTES:
                raise ValueError("response exceeds the size limit")
            if raw[:2] == b"\x1f\x8b":
                raw = _gunzip(raw)
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
    # unescape FIRST: stripping tags first would let "&lt;script&gt;" survive
    # the regex and become live markup in the stored value
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
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


def safe_link(url):
    """Only ordinary web addresses may ever become an href.

    Escaping is the wrong tool here: html.escape turns the quotes in
    `javascript:fetch('...')` into entities, and the browser decodes them back
    before using the address — so the payload survives intact. A feed can put
    anything in <link>, so the scheme is checked against an allowlist instead.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


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
            "title": re.sub(r"\s+", " ", title).strip()[:MAX_TITLE_CHARS],
            "link": safe_link(link),
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
                errors.append((name, _scrub(e)))

    filings, seen_links = [], set()
    for url in SEC_FILING_FEEDS:
        try:
            raw = fetch(url, ua=SEC_UA)
            for f in parse_feed(raw, "SEC EDGAR", "filings", 1.0):
                if f["link"] not in seen_links:
                    seen_links.add(f["link"])
                    filings.append(f)
        except Exception as e:  # noqa: BLE001
            errors.append(("SEC EDGAR", _scrub(e)))
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
        if not label:
            continue
        try:                    # a vendor date is untrusted; a string compare
            day = datetime.fromisoformat(d).date()   # would pass "2026-09-17T"
        except (TypeError, ValueError):              # and blow up at render
            continue
        if not (today <= day <= end):
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
  --bk-a: #0b0b0b; --bk-b: #2a78d6; --bk-c: #c47510;
  --bk-d: #7b46bd; --bk-e: #0a8a5f;
  --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --hair: #2c2c2a; --border: rgba(255,255,255,.10);
    --accent: #3987e5; --spark: #1c5cab;
    --bk-a: #ffffff; --bk-b: #5aa2f0; --bk-c: #e8a33f;
    --bk-d: #b18ae8; --bk-e: #2fc58c;
    --up: #0ca30c; --down: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
  --hair: #2c2c2a; --border: rgba(255,255,255,.10);
  --accent: #3987e5; --spark: #1c5cab;
  --bk-a: #ffffff; --bk-b: #5aa2f0; --bk-c: #e8a33f;
  --bk-d: #b18ae8; --bk-e: #2fc58c;
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
/* a ticker link following its BUY/SELL label stays on the same line — the
   list's default block links are for headlines, which do want their own */
.item strong + a { display: inline; }
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
/* On a phone a twelve-column table is a sideways scroll nobody reads. Below
   700px the screen table becomes one card per company: same markup, restacked
   in CSS, each cell labelled from its data-l attribute. No duplicated rows. */
@media (max-width: 700px) {
  table.screen.cards, table.screen.cards tbody, table.screen.cards tr,
  table.screen.cards td { display: block; width: 100%; }
  /* hide the header ROW, not just its cells — hiding only the cells leaves an
     empty bordered card sitting above the first company */
  table.screen.cards thead, table.screen.cards tr:has(th) { display: none; }
  /* a company with no flags should not get an empty labelled row */
  table.screen.cards td:empty { display: none; }
  table.screen.cards tr { border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px; margin-bottom: 10px; background: var(--surface); }
  table.screen.cards td { border: 0; padding: 2px 0; text-align: left !important;
    white-space: normal; display: flex; justify-content: space-between;
    gap: 14px; font-variant-numeric: tabular-nums; }
  table.screen.cards td::before { content: attr(data-l); color: var(--muted);
    font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
    text-transform: uppercase; flex: none; padding-top: 2px; }
  /* the identifying cells read as a heading, not as another labelled row */
  table.screen.cards td.rank, table.screen.cards td.tick,
  table.screen.cards td.nm { display: block; }
  table.screen.cards td.rank::before, table.screen.cards td.tick::before,
  table.screen.cards td.nm::before { content: none; }
  table.screen.cards td.rank { float: right; color: var(--muted);
    font-size: 12px; font-family: "IBM Plex Mono", ui-monospace, monospace; }
  table.screen.cards td.tick { font-size: 17px; font-weight: 600; }
  table.screen.cards td.nm { font-size: 13px; color: var(--ink2);
    margin-bottom: 7px; padding-bottom: 7px;
    border-bottom: 1px solid var(--hair); }
  .tblwrap:has(table.cards) { overflow-x: visible; }
}
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
.flag.act { color: var(--up); border-color: var(--up); }
.flag.offer { color: var(--down); border-color: var(--down); }
.chart { margin: 4px 0 10px; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; }
.axl { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
  fill: var(--muted); }
.endlab { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
  font-weight: 500; }
@media (max-width: 720px) { .endlab { font-size: 18px; } }
@media (max-width: 460px) { .endlab { font-size: 21px; } }
/* the chart scales down with the page, so its labels are enlarged in SVG user
   units on small screens — otherwise they render at about four real pixels */
@media (max-width: 720px) { .axl { font-size: 17px; } }
@media (max-width: 460px) { .axl { font-size: 20px; } }
.legend { display: flex; flex-wrap: wrap; gap: 5px 18px; margin-top: 8px;
  font-size: 12px; color: var(--ink2); }
.legend span { display: inline-flex; align-items: center; gap: 6px;
  white-space: nowrap; }
.legend i { width: 14px; height: 3px; border-radius: 2px; flex: none; }
.legend b { font-family: "IBM Plex Mono", ui-monospace, monospace; font-weight: 500; }
.legend em { font-style: normal; font-variant-numeric: tabular-nums; }

.alarm { border: 1px solid var(--down); border-left-width: 4px; border-radius: 6px;
  padding: 13px 16px; margin: 14px 0 6px; font-size: 13.5px; color: var(--ink); }
.alarm strong { color: var(--down); }
.alarm ul { margin: 7px 0 0 18px; }
.alarm li { padding: 2px 0; }
.alarm li strong { color: var(--ink); }
.alarm p { margin-top: 9px; color: var(--ink2); font-size: 12.5px; }

.tabs { display: flex; flex-wrap: wrap; gap: 0 4px; margin: 0 0 24px;
  border-bottom: 1px solid var(--hair); }
.tab { display: inline-block; padding: 8px 12px 9px; font-size: 13px;
  font-weight: 600; color: var(--muted); border-bottom: 2px solid transparent;
  margin-bottom: -1px; white-space: nowrap; }
a.tab:hover { color: var(--ink); text-decoration: none;
  border-bottom-color: var(--hair); }
.tab.cur { color: var(--ink); border-bottom-color: var(--accent); }
.pager { display: flex; justify-content: space-between; gap: 16px;
  margin-top: 38px; border-top: 1px solid var(--hair); padding-top: 13px;
  font-size: 13px; font-weight: 600; }
.pager a { color: var(--ink2); }
.pager a:only-child:last-child { margin-left: auto; }
.sechead { font-family: "Besley", Georgia, serif; font-weight: 700; font-size: 21px;
  letter-spacing: -0.01em; border-top: 2px solid var(--ink); padding-top: 11px;
  margin-bottom: 4px; }
.secsub { font-size: 12.5px; color: var(--muted); max-width: 80ch;
  margin-bottom: 14px; }

.tracker { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 20px 20px; margin: 22px 0 30px; }
.tr-head { font-family: "Besley", Georgia, serif; font-weight: 700; font-size: 20px;
  letter-spacing: -0.01em; }
.tr-sub { font-size: 12.5px; color: var(--muted); margin: 3px 0 15px; max-width: 78ch; }
.trgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
  gap: 15px 22px; border-top: 1px solid var(--hair); padding-top: 15px; }
.trl { font-size: 10.5px; font-weight: 700; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--muted); }
.trv { font-family: "Besley", Georgia, serif; font-size: 26px; font-weight: 700;
  line-height: 1.2; font-variant-numeric: tabular-nums; }
.trn { font-size: 11.5px; color: var(--ink2); line-height: 1.35; }
.bars { display: grid; gap: 13px; max-width: 64ch; margin-top: 20px; }
.barlab { display: flex; justify-content: space-between; gap: 12px; font-size: 12px;
  color: var(--ink2); margin-bottom: 5px; }
.barlab b { font-family: "IBM Plex Mono", ui-monospace, monospace; font-weight: 500;
  color: var(--ink); white-space: nowrap; }
.bar { height: 8px; background: var(--hair); border-radius: 4px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
.mstones { list-style: none; margin-top: 20px; font-size: 13px; max-width: 86ch;
  columns: 2; column-gap: 34px; }
.mstones li { padding: 3.5px 0; color: var(--muted); break-inside: avoid; }
.mstones li b { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px; font-weight: 500; margin-right: 8px; color: var(--hair); }
.mstones li.done { color: var(--ink2); }
.mstones li.done b { color: var(--up); }
.trsay { font-size: 13px; color: var(--ink2); max-width: 80ch; margin-top: 18px;
  border-top: 1px solid var(--hair); padding-top: 14px; }
@media (max-width: 560px) { .mstones { columns: 1; } }
table.screen td.path { padding: 2px 8px; width: 118px; }
table.screen td.path .spark { height: 26px; margin: 0; }
.retired { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11.5px;
  color: var(--muted); margin: -14px 0 24px; max-width: 90ch; line-height: 1.6; }

.wrap.co { max-width: 900px; }
.backlink { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
  color: var(--muted); display: inline-block; margin-bottom: 18px; }
.co-tick { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 34px;
  font-weight: 500; letter-spacing: -0.01em; line-height: 1.1; }
.co-name { font-family: "Besley", Georgia, serif; font-weight: 700; font-size: 23px;
  line-height: 1.2; margin-top: 2px; }
.co-sub { font-size: 13px; color: var(--muted); margin: 5px 0 4px; }
.cogrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(146px, 1fr));
  gap: 14px 22px; margin: 12px 0 4px; }
.cofv { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 17px;
  font-variant-numeric: tabular-nums; }
.cofoot { font-size: 12.5px; color: var(--muted); max-width: 82ch; margin: 10px 0 26px; }
.plain { max-width: 74ch; font-size: 15px; }
.plain h3 { font-family: "Besley", Georgia, serif; font-weight: 700;
  font-size: 17px; margin: 24px 0 7px; }
.plain p { margin-bottom: 11px; color: var(--ink2); }
.plain .rules { margin: 8px 0 14px; font-size: 14.5px; }

.rules { list-style: none; margin: 14px 0 4px; font-size: 13.5px;
  color: var(--ink2); max-width: 80ch; }
.rules li { padding: 3px 0 3px 16px; position: relative; }
.rules li::before { content: "·"; position: absolute; left: 3px;
  color: var(--accent); font-weight: 700; }

.expl { border-top: 1px solid var(--hair); padding: 12px 0 13px; max-width: 84ch; }
.explhead { display: flex; align-items: baseline; gap: 6px 14px; flex-wrap: wrap; }
.expltag { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 13px;
  font-weight: 500; min-width: 16ch; }
.explval { font-family: "Besley", Georgia, serif; font-size: 22px; font-weight: 700;
  font-variant-numeric: tabular-nums; }
.explk { font-size: 10.5px; font-weight: 700; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--muted); }
.explbody { font-size: 13.5px; color: var(--ink2); margin-top: 4px; }
.screen a { text-decoration: underline; text-decoration-color: var(--border);
  text-underline-offset: 3px; }
.screen a:hover { text-decoration-color: var(--accent); }
.co .section-head { margin-top: 30px; }

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


def spark_svg(closes, label="one-month trend", lo=None, hi=None):
    """`lo`/`hi` force a SHARED vertical scale. Without one, every sparkline is
    stretched to its own range, and two curves printed side by side look alike
    whether one moved 0.4% or 24% — which is exactly the comparison a whole
    column of them invites a reader to make."""
    if len(closes) < 2:
        return ""
    lo = min(closes) if lo is None else min(lo, min(closes))
    hi = max(closes) if hi is None else max(hi, max(closes))
    span = (hi - lo) or 1.0
    w, h, pad = 120, 34, 4
    pts = []
    for i, c in enumerate(closes):
        x = pad + (w - 2 * pad) * i / (len(closes) - 1)
        y = pad + (h - 2 * pad) * (1 - (c - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    ex, ey = pts[-1].split(",")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'role="img" aria-label="{esc(label)}">'
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


def masthead_html(date_line):
    return ('<header class="masthead"><div>'
            '<div class="brand">Basis<span class="tick">/</span>Points</div>'
            '<div class="kicker">An automated small-cap growth rating system.</div></div>'
            f'<div class="mastright"><div class="updated">updated {esc(date_line)}</div>'
            '</div></header>')


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


def render_econ_column(rows):
    lis = []
    for r in rows:
        day = datetime.fromisoformat(r["date"]).strftime("%a %b %-d")
        lis.append(f'<div class="item"><strong>{esc(r["label"])}</strong>'
                   f'<div class="meta">{esc(day)}</div></div>')
    return ('<section class="col"><h2 class="section-head">Economic calendar</h2>'
            f'{"".join(lis)}</section>')


def _fmt_adv(adv):
    """Average daily volume arrives in MILLIONS of shares (smallcap.ADV_MIN is
    0.05 = fifty thousand). Printing it as though it were thousands understated
    every company on the page by a factor of a thousand."""
    try:
        m = float(adv)
    except (TypeError, ValueError):
        return "—"
    return f"{m:,.1f}M shares" if m >= 1 else f"{m * 1000:,.0f}k shares"


def expl(label, value, kicker, body):
    """A labelled figure with its reasoning underneath. Deliberately NOT a table:
    a column of explanatory prose has no width at which a table reads well."""
    return (f'<div class="expl"><div class="explhead">'
            f'<span class="expltag">{label}</span>'
            f'<span class="explval">{value}</span>'
            f'<span class="explk">{kicker}</span></div>'
            f'<p class="explbody">{body}</p></div>')


def co_link(ticker, prefix="co/", known=None):
    """Link a ticker to its own page — but only if it is a ticker, and only if
    that page exists.

    Validation, because an unvalidated ticker would put attacker-chosen text
    into an href on a public page. And `known`, because company pages are
    written for the CURRENT screen: a name the books still hold after it left
    the screen has no page, and linking to one would be a dead link on exactly
    the holding a reader is most likely to click."""
    t = safe_ticker(ticker)
    if not t:
        return esc(ticker or "")
    if known is not None and t not in known:
        return esc(t)
    return f'<a href="{prefix}{t}.html">{esc(t)}</a>'


FRICTION_MIN_DAYS = 45          # below this an annualised rate is an artefact


def fmt_friction(b):
    """Cost of trading, as a rate only once a rate means something.

    friction_yr scales the cost paid so far up to a full year. Three days after
    the books opened that turns one rebalance into '48%/yr' — a number nobody
    will ever pay, printed next to real ones. Until the record is long enough,
    report what was actually spent."""
    spent = ((b.get("costs_paid") or 0.0)
             / max(portfolio.START_CAPITAL, 1) * 100)
    if (b.get("days") or 0) < FRICTION_MIN_DAYS:
        return f'{spent:.2f}% spent', f'over {b.get("days", 0)} days'
    return f'{b["friction_yr"]:.1f}%/yr', "annualised"


def _fmt_mcap(musd):
    return f"${musd / 1000:.1f}B" if musd >= 1000 else f"${musd:.0f}M"



BOOK_STROKE = {"A": "var(--bk-a)", "B": "var(--bk-b)", "C": "var(--bk-c)",
               "D": "var(--bk-d)", "E": "var(--bk-e)"}
# A second signal besides colour. Amber and green — books C and E, the pair
# that differs only by its stop — are a common confusion, and a chart whose
# whole point is comparison must not rest that comparison on hue alone.
BOOK_DASH = {"A": "", "B": "7 3", "C": "2 3", "D": "10 3 2 3", "E": "4 3"}


def _nice_step(span, want=4):
    """A gridline interval a person would have chosen. Ticks at 3.7% intervals
    are technically correct and unreadable."""
    if span <= 0:
        return 1.0
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if span / step <= want:
            return float(step)
    return 1000.0


def equity_chart(pf):
    """The headline chart: every book's value against the benchmark, rebased so
    the start of each is 0%.

    Rebasing to a common start is what makes six lines comparable at a glance.
    The alternative — plotting dollars — would let a book that began later look
    like an outperformer purely because it started from a different place."""
    pf = pf or {}
    series = [(b["key"], b["label"], b["curve"], BOOK_STROKE.get(b["key"], "var(--ink)"),
               2.4 if b["key"] == "A" else 1.6, BOOK_DASH.get(b["key"], ""))
              for b in (pf.get("books") or []) if len(b.get("curve") or []) >= 2]
    bench = pf.get("bench_curve") or []
    if len(bench) >= 2:
        series.append(("IWO", "Russell 2000 Growth ETF", bench,
                       "var(--muted)", 1.6, "5 4"))
    if not series:
        return ('<div class="note-box"><strong>No return chart yet.</strong> '
                'The simulated books have not opened, so there is nothing to '
                'plot. They begin trading at the first weekly rebalance that '
                'falls inside US market hours, and this chart appears with '
                'them. An empty chart is shown as empty rather than as a flat '
                'line at zero, which would look like a result.</div>')

    pts = [v for _, _, c, _, _, _ in series for v in c]
    lo_p, hi_p = min(pts) - 100.0, max(pts) - 100.0
    step = _nice_step(max(hi_p - lo_p, 1.0))
    lo_p = math.floor(lo_p / step) * step
    hi_p = math.ceil(hi_p / step) * step
    if hi_p - lo_p < step:                      # a dead-flat record still needs an axis
        hi_p = lo_p + step
    # Labels sit INSIDE the plot, just above their gridline, rather than in a
    # left gutter. On a phone this chart is scaled to ~40% of its authored
    # width, and a gutter sized for 10px text clips the moment the text is
    # enlarged enough to stay readable at that scale.
    W, H, PL, PR, PT, PB = 760, 292, 6, 34, 18, 40
    iw, ih = W - PL - PR, H - PT - PB

    def y_of(pct):
        return PT + ih * (1 - (pct - lo_p) / (hi_p - lo_p))

    grid, tick = [], lo_p
    while tick <= hi_p + 1e-9:
        y = y_of(tick)
        zero = abs(tick) < 1e-9
        grid.append(
            f'<line x1="{PL}" y1="{y:.1f}" x2="{W - PR}" y2="{y:.1f}" '
            f'stroke="var({"--border" if zero else "--hair"})" '
            f'stroke-width="{1.2 if zero else 1}"/>'
            f'<text x="{PL + 1}" y="{y - 3:.1f}" class="axl">{tick:+.0f}%</text>')
        tick += step

    paths, legend, ends = [], [], []
    for key, label, curve, colour, width, dash in series:
        n = len(curve)
        pl = " ".join(f'{PL + iw * i / (n - 1):.1f},{y_of(v - 100.0):.1f}'
                      for i, v in enumerate(curve))
        paths.append(
            f'<polyline points="{pl}" fill="none" stroke="{colour}" '
            f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'
            f'{f" stroke-dasharray=\"{dash}\"" if dash else ""}/>')
        ends.append([y_of(curve[-1] - 100.0), key, colour])
        ret = curve[-1] - 100.0
        # the swatch must match how the line is actually drawn — a legend that
        # shows a solid key for a dashed line is a legend to be checked twice
        if dash:
            on, off = (dash.split() + ["3"])[:2]
            swatch = (f'background:repeating-linear-gradient(90deg,{colour} 0 '
                      f'{on}px,transparent {on}px {int(on) + int(off)}px)')
        else:
            swatch = f'background:{colour}'
        legend.append(
            f'<span><i style="{swatch}"></i>'
            f'<b>{esc(key)}</b> {esc(label)} '
            f'<em class="{delta_class(ret)}">{ret:+.1f}%</em></span>')

    # Label each line where it ends. Five lines told apart by colour alone
    # exclude anyone who cannot separate the amber from the green — which is
    # books C and E, the two that differ only by their stop. Direct labels are
    # also simply easier to read than a legend, for everybody.
    ends.sort(key=lambda e: e[0])
    for i in range(1, len(ends)):                 # push apart where they collide
        if ends[i][0] - ends[i - 1][0] < 11:
            ends[i][0] = ends[i - 1][0] + 11
    tags = "".join(
        f'<text x="{W - PR + 3}" y="{y + 3.5:.1f}" class="endlab" '
        f'fill="{colour}">{esc(key)}</text>' for y, key, colour in ends)

    span = pf.get("span") or []
    axis = ""
    if len(span) == 2 and all(span):
        axis = (f'<text x="{PL}" y="{H - 7}" class="axl">{esc(span[0])}</text>'
                f'<text x="{W - PR}" y="{H - 7}" text-anchor="end" '
                f'class="axl">{esc(span[1])}</text>')

    return (
        f'<figure class="chart"><svg viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="Simulated value of each paper book and the benchmark, '
        f'rebased so each starts at zero percent">'
        f'{"".join(grid)}{"".join(paths)}{tags}{axis}</svg>'
        f'<figcaption class="legend">{"".join(legend)}</figcaption></figure>'
        '<p class="cofoot">Every line starts at 0%, so they can be compared '
        'directly. <strong>Book A is the control</strong> — a clever book that '
        'does not finish above it has not earned its extra trading. The dashed '
        'line is the index all five are trying to beat. These are simulated '
        'results; no money is invested.</p>')

def render_progress(data):
    """The tracker panel. Progress toward the goal is measured in EVIDENCE, not
    in profit: a book up 8% after a fortnight has proved nothing, and the bars
    here are sized so that reading them cannot leave the opposite impression.
    Everything in it is derived from published state — nothing is asserted."""
    sc = data.get("smallcap") or {}
    pf = data.get("portfolio") or {}
    ev = sc.get("evaluation") or {}
    books = pf.get("books") or []
    by_key = {b["key"]: b for b in books}
    ctrl = by_key.get("A")
    tgt = smallcap.FREEZE_TARGET
    got = {h: ((ev.get(h) or {}).get("indep") or 0) for h in ("1w", "4w")}
    try:
        today = datetime.fromisoformat(data["generated_at"]).date()
        to_go = (date.fromisoformat(smallcap.FREEZE_REVIEW_DATE) - today).days
    except (ValueError, KeyError):
        to_go = None

    tiles = []

    def tile(label, value, note):
        tiles.append(f'<div><div class="trl">{label}</div>'
                     f'<div class="trv">{value}</div>'
                     f'<div class="trn">{note}</div></div>')

    if ctrl:
        tile("Days on the record", f'{ctrl["days"]}',
             f'first mark {esc(ctrl["started"] or "—")} · '
             f'{len(books)} book{"s" if len(books) != 1 else ""} live')
    else:
        tile("Days on the record", "0", "the books have not opened yet")

    if ctrl:
        tile("Control book", f'{ctrl["ret"]:+.1f}%',
             'equal weight, no overlays — the number every clever rule has to beat')
    else:
        tile("Control book", "—", "equal weight, no overlays")

    # the best of several is biased upward, and saying so is the whole point of
    # printing it next to the control rather than on its own
    gaps = [(by_key[k]["ret"] - ctrl["ret"], by_key[k]) for k in "BCDE" if k in by_key] \
        if ctrl else []
    if gaps:
        gap, best = max(gaps, key=lambda t: t[0])
        tile("Best overlay, vs control", f'{gap:+.1f}%',
             f'{esc(best["key"])} {esc(best["label"])} · highest of {len(gaps)}, '
             'so it flatters itself')
    else:
        tile("Best overlay, vs control", "—", "needs a running control to compare against")

    if to_go is not None:
        tile("Days to the review", f'{to_go:,}' if to_go > 0 else "due",
             f'evidence is judged {esc(smallcap.FREEZE_REVIEW_DATE)}, '
             'on whatever it says')

    bars = []
    for h, lab, why in (
            ("1w", "Independent 1-week readings",
             "non-overlapping, so a single good week cannot be counted twelve times"),
            ("4w", "Independent 4-week readings",
             "the slower cadence, where a genuine edge should still be visible")):
        n, need = got[h], tgt[h]
        pct = min(100.0, 100.0 * n / need) if need else 0.0
        bars.append(f'<div><div class="barlab"><span>{lab} — {why}</span>'
                    f'<b>{n} of {need}</b></div>'
                    f'<div class="bar"><i style="width:{pct:.0f}%"></i></div></div>')

    stones = [
        (bool(sc.get("screen")), "A ranked screen is published every run"),
        (bool(books), f'{len(books) or "No"} paper books trading the screen'),
        (got["1w"] >= tgt["1w"],
         f'{got["1w"]} of {tgt["1w"]} independent 1-week readings'),
        (got["4w"] >= tgt["4w"],
         f'{got["4w"]} of {tgt["4w"]} independent 4-week readings'),
        (to_go is not None and to_go <= 0,
         f'Scheduled review of the evidence, {esc(smallcap.FREEZE_REVIEW_DATE)}'),
        (False, "Verdict: trade it, change it, or abandon it"),
    ]
    lis = "".join(
        f'<li class="{"done" if ok else ""}"><b>{"&#10003;" if ok else "&#9675;"}</b>'
        f'{txt}</li>' for ok, txt in stones)

    if not books:
        say = ('The books have not opened, so there is nothing to judge yet. '
               'They trade only while the US market is open.')
    elif got["1w"] >= tgt["1w"] and got["4w"] >= tgt["4w"]:
        say = ('The evidence bar is met. The scoring rules may now be revised, and '
               'the scheduled review has a real sample to judge — including the '
               'possibility that it says this does not work.')
    else:
        say = (f'Too early to judge, by design. {got["1w"]} of {tgt["1w"]} '
               f'independent 1-week readings are in. Until that bar is cleared the '
               f'scoring rules stay frozen, because a model tuned while its record '
               f'is being written will always look good and will always be lying. '
               f'The returns above are simulated and short; the friction column in '
               f'the table below is the number most likely to decide the answer.')

    return (f'<section class="tracker">'
            f'<h2 class="tr-head">Where the trading system stands '
            f'<span class="flag offer">SIMULATED</span></h2>'
            f'<p class="tr-sub">The goal is to find out whether this screen is '
            f'worth trading. Progress is counted in independent forward readings, '
            f'not in the size of a simulated gain. Model '
            f'{esc(str(sc.get("v") or smallcap.MODEL_VERSION))}.</p>'
            f'{equity_chart(pf)}'
            f'<div class="trgrid">{"".join(tiles)}</div>'
            f'<div class="bars">{"".join(bars)}</div>'
            f'<ul class="mstones">{lis}</ul>'
            f'<p class="trsay">{say} No money is invested and nothing here is a '
            f'recommendation.</p></section>')


def trade_list(trades, prefix="co/", known=None):
    """Trades, showing WHICH books acted. Two books selling on a stop while
    three hold is the most informative thing the simulation produces, and an
    unlabelled list throws it away."""
    out = []
    for t in trades:
        # tolerate an ungrouped trade: between a deploy and the next data
        # refresh the committed file is still in the previous shape, and a page
        # that crashes on it freezes the whole site at its last version
        books = t.get("books") or []
        who = ("all five books" if t.get("all_books") else
               f"book {books[0]}" if len(books) == 1 else
               "books " + ", ".join(books) if books else "")
        lo = t.get("shares_lo", t.get("shares"))
        hi = t.get("shares_hi", t.get("shares"))
        if lo is None or hi is None:
            sh = ""
        else:
            sh = f'{lo:,.0f} sh' if lo == hi else f'{lo:,.0f}–{hi:,.0f} sh'
        bits = " · ".join(x for x in (f'${t["px"]:,.2f}', sh, who,
                                      t.get("why") or "") if x)
        side = t["side"].upper()
        out.append(
            f'<div class="item"><strong class="{"down" if side == "SELL" else ""}">'
            f'{esc(side)}</strong> {co_link(t["ticker"], prefix, known)}'
            f'<div class="meta">{esc(t["date"])} · {esc(bits)}</div></div>')
    return "".join(out)


def render_portfolio(pf, known=None):
    """The paper portfolios. Labelled unmistakably: these are simulations, and
    a rising number on a public page must never read as a real return."""
    if not pf or pf.get("status") != "running":
        return ('<div class="note-box"><strong>Not trading yet — waiting for market '
                'hours.</strong> The books only trade while the US market '
                'is actually open, because a price fetched outside those hours '
                'carries the previous close — and buying at exactly the price that '
                'put a name on the screen would hand the simulation a free day of '
                'gains it could never have earned. No money is involved at any '
                'point.</div>')
    a = pf["assumptions"]
    note = (
        '<div class="note-box"><strong>No money is invested. These are '
        'hypothetical results.</strong> Five books run over the same screen, the '
        'same prices and the same frictions, differing only in their rules, so '
        'that the effect of each rule can be seen rather than assumed. '
        f'Each starts from a notional ${a["capital"]:,.0f}, rebalances '
        f'{esc(a["cadence"])}, and pays {a["cost_bps"]:.0f} basis points per side; '
        f'a stop exit pays a further {a["stop_slippage_bps"]:.0f} because real '
        'stops gap through in thin small-caps. The <em>path</em> column shares one '
        'vertical scale across all six rows, so the shapes can be compared '
        'directly. <strong>Watch the friction column '
        'before the return column</strong> — this screen turns over its holdings '
        'many times a year, and at that rate the cost of trading may be the whole '
        'story rather than a rounding error. Each book&rsquo;s name links to its '
        'own page — what it holds, at what size, and where every stop sits. '
        '<strong>Book A is the control</strong> '
        '— if the cleverer books do not beat it, the cleverness is not earning its '
        'keep. Simulated results still omit what hurts real traders most: the '
        'market moving against a real order, taxes, and the nerve to follow a '
        'system through a losing stretch.</div>')
    head = ('<tr><th class="l">Book</th><th class="l">Rules</th><th class="l">Path</th>'
            '<th>Value</th>'
            '<th>Return</th><th>vs IWO</th><th>Worst dip</th><th>Cost of trading</th>'
            '<th>Stops</th><th>Held</th></tr>')
    # one vertical scale across every path in the column, benchmark included
    allpts = [v for b in pf["books"] for v in (b.get("curve") or [])]
    allpts += list(pf.get("bench_curve") or [])
    lo, hi = (min(allpts), max(allpts)) if allpts else (None, None)
    rows = []
    for b in pf["books"]:
        exc = b.get("excess")
        exc_td = (f'<td class="{delta_class(exc)}">{exc:+.2f}%</td>'
                  if exc is not None else "<td>—</td>")
        # the shape of the path, not just its endpoint — two books can finish in
        # the same place having been very different things to hold
        spark = spark_svg(b.get("curve") or [],
                          f'{b["label"]} book, value over time', lo, hi)
        rows.append(
            f'<tr><td class="l tick">'
            f'<a href="book/{esc(b["key"])}.html">{esc(b["key"])} '
            f'{esc(b["label"])}</a></td>'
            f'<td class="l">{esc(b["note"])}</td>'
            f'<td class="path">{spark or "&mdash;"}</td>'
            f'<td>${b["value"]:,.0f}</td>'
            f'<td class="{delta_class(b["ret"])}">{b["ret"]:+.2f}%</td>'
            f'{exc_td}<td>{b["max_drawdown"]:+.1f}%</td>'
            f'<td class="down">{fmt_friction(b)[0]}</td>'
            f'<td>{b["stops_hit"]}</td><td>{b["positions"]}</td></tr>')
    bench_spark = spark_svg(pf.get("bench_curve") or [],
                            "Russell 2000 Growth ETF over the same period", lo, hi)
    if bench_spark:
        rows.append(
            '<tr><td class="l tick">IWO benchmark</td>'
            '<td class="l">the index these books are trying to beat</td>'
            f'<td class="path">{bench_spark}</td>'
            '<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>'
            '<td>—</td><td>—</td></tr>')
    table = f'<div class="tblwrap"><table class="screen">{head}{"".join(rows)}</table></div>'
    # a restart that quietly erased its own bad run would leave a record made
    # only of good stretches, so closed books stay visible
    ret = pf.get("retired") or []
    if ret:
        bits = "; ".join(
            f'{esc(str(r.get("v") or "?"))} book {esc(str(r.get("key") or "?"))} '
            f'{esc(str(r.get("label") or ""))} closed {esc(str(r.get("retired_on") or ""))} '
            f'at {r["ret"]:+.1f}% after {r.get("days", 0)} days'
            for r in ret if r.get("ret") is not None)
        if bits:
            table += (f'<div class="retired">closed books, kept on the record: '
                      f'{bits} — a model change restarts the books, and the old '
                      f'runs are shown so that restarting cannot be used to '
                      f'forget a bad one.</div>')
    cols = []
    if pf.get("holdings"):
        lis = "".join(
            f'<div class="item"><strong>{co_link(h["ticker"], known=known)}</strong> '
            f'<span class="{delta_class(h["ret"])}">{h["ret"]:+.1f}%</span>'
            f'<div class="meta">${h["value"]:,.0f} · since {esc(h["entry_date"] or "")}'
            f'</div></div>' for h in pf["holdings"])
        cols.append('<section class="col"><h2 class="section-head">Baseline book '
                    f'holdings</h2>{lis}</section>')
    if pf.get("trades"):
        cols.append('<section class="col"><h2 class="section-head">Recent simulated '
                    f'trades</h2>{trade_list(pf["trades"], known=known)}'
                    '<div class="meta" style="padding-top:8px">Grouped by the books '
                    'that made each trade. A name bought by all five is one entry, '
                    'not five — but the share counts differ, which is the conviction '
                    'sizing doing its work.</div></section>')
    return note + table + (f'<div class="duo">{"".join(cols)}</div>' if cols else "")



TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


def safe_ticker(t):
    """A ticker becomes a FILE NAME, so it is validated, never sanitised.
    Anything that is not plainly a ticker is refused outright rather than
    stripped into something that merely looks safe."""
    t = (t or "").strip().upper()
    return t if TICKER_RE.match(t) and ".." not in t else None


def _n(v, fmt="{:,.1f}", suffix="", dash="—"):
    """Format a number, or say plainly that it is missing. A blank where a
    figure belongs reads as zero; this never does."""
    try:
        return fmt.format(float(v)) + suffix
    except (TypeError, ValueError):
        return dash


def render_company_page(row, rank, screen, pf, sc, date_line):
    """One company, explained: what it is, why it scores what it does, what
    size the rules give it and where its stop would sit — each shown as the
    arithmetic that produced it.

    The sizing and stop figures are obtained by CALLING the live functions in
    portfolio.py, never by restating their formulas here. A page that restated
    them would drift from the code the moment either changed, and would then be
    describing a system that no longer exists."""
    t = row["ticker"]
    w = row.get("why") or {}
    sub = row.get("sub") or {}
    px = row.get("px")

    head = (f'<a class="backlink" href="../screen.html">&larr; back to the screen</a>'
            f'<h1 class="co-tick">{esc(t)}</h1>'
            f'<p class="co-name">{esc(row.get("name") or t)}</p>'
            f'<p class="co-sub">{esc(row.get("ind") or "—")}'
            f'{" · " + esc(w["exch"]) if w.get("exch") else ""} · '
            f'{_fmt_mcap(row["mcap"])} market value · ranked '
            f'<strong>{rank}</strong> of {len(screen)} on today&rsquo;s screen</p>')

    intro = ('<div class="note-box"><strong>This page explains a rule, not a '
             'recommendation.</strong> Every number below was produced '
             'mechanically from public data. Nothing here is a judgement about '
             'the company, nobody has read its filings, and no position '
             'described on this page is real — the portfolios are simulations. '
             'It is published so that the reasoning can be checked and '
             'disagreed with, which is the only thing that makes an automated '
             'system worth trusting.</div>')

    # ---- what the company is, strictly from fetched fields ----
    shares = w.get("shares")
    rev = (w["rps"] * shares) if (w.get("rps") and shares) else None
    facts = [
        ("Market value", _fmt_mcap(row["mcap"])),
        ("Revenue, trailing 12 months", f'${rev:,.0f}M' if rev else "—"),
        ("Price", _n(px, "${:,.2f}")),
        ("Enterprise value / revenue", _n(row.get("ev_rev"), "{:,.1f}", "×")),
        ("Gross margin", _n(w.get("gm_t"), "{:,.1f}", "%")),
        ("Operating margin", _n(w.get("om_t"), "{:+,.1f}", "%")),
        ("Debt to equity", _n(w.get("dte"), "{:,.2f}")),
        ("Cash per share", _n(w.get("cashps"), "${:,.2f}")),
        ("Average daily volume", _fmt_adv(w.get("adv"))),
        ("Shares outstanding", _n(shares, "{:,.0f}", "M")),
        ("Annual volatility", _n(w.get("vol"), "{:,.0f}", "%")),
        ("Below its 52-week high", _n(row.get("from_high"), "{:+,.1f}", "%")),
    ]
    fact_html = "".join(f'<div><div class="trl">{k}</div><div class="cofv">{v}</div></div>'
                        for k, v in facts)
    company = (
        '<h2 class="section-head">The company, from the data</h2>'
        f'<div class="cogrid">{fact_html}</div>'
        '<p class="cofoot">Assembled from the data feed — an industry label, a '
        'set of filed figures and a price history. It is not a description of '
        'what the business does, because nothing in this system reads about '
        'the business.</p>')

    # ---- why it scores what it does ----
    score_rows = [
        ("Growth", 40, sub.get("g"),
         f'revenue up {_n(row.get("rev_g"), "{:+,.1f}", "%")} over the last twelve '
         f'months, {_n(w.get("rg3"), "{:+,.1f}", "%")} a year over three years, and '
         f'the latest quarter running {_n(row.get("accel"), "{:+,.1f}", " points")} '
         f'against that trend. Ranked against its own industry group '
         f'({esc(row.get("group") or "—")}), not against the whole market, so a '
         f'sector where everyone grows fast earns nobody a high mark.'),
        ("Momentum", 40, sub.get("m"),
         f'up {_n(row.get("r13"), "{:+,.1f}", "%")} over 13 weeks and '
         f'{_n(w.get("r26"), "{:+,.1f}", "%")} over 26, divided by its volatility of '
         f'{_n(w.get("vol"), "{:,.0f}", "%")} — a big move in a jumpy stock counts '
         f'for less than the same move in a steady one.'),
        ("Quality", 20, sub.get("q"),
         f'debt to equity {_n(w.get("dte"), "{:,.2f}")}, operating margin moving from '
         f'{_n(w.get("om_a"), "{:+,.1f}", "%")} to {_n(w.get("om_t"), "{:+,.1f}", "%")}, '
         f'cash of {_n(w.get("cashps"), "${:,.2f}")} a share, and whether the share '
         f'count has been growing. Leverage is judged against its own industry.'),
    ]
    trs = "".join(expl(k, _n(v, "{:,.0f}"), f"{pct}% of the score", txt)
                  for k, pct, v, txt in score_rows)
    scoring = (
        f'<h2 class="section-head">Why it scores {row["score"]:.1f}</h2>'
        f'{trs}'
        '<p class="cofoot">Each mark is a percentile against the other eligible '
        'companies measured today — 80 means it beat four out of five of them on '
        'that part, not that it scored 80 out of 100. The set it is ranked against '
        'changes daily, so a mark can move without the company changing at all.</p>')

    # ---- what size the rules give it ----
    eq = 100.0 / max(len(screen), 1)
    conv = portfolio.target_weights(screen, {"sizing": "score"}).get(t)
    conv_pct = conv * 100 if conv else None
    held = ((pf or {}).get("weights") or {}).get(t) or {}
    held_html = (" · ".join(f'{esc(k)} {v:.2f}%' for k, v in sorted(held.items()))
                 if held else
                 'not currently held in any book — a name can rank well today and '
                 'still be bought only at the next weekly rebalance, or be held '
                 'back by a rule')
    sizing = (
        '<h2 class="section-head">What size the rules give it, and why</h2>'
        + expl('Books A, C, D', f'{eq:.2f}%', 'equal weight',
               'one twenty-fifth of the book, the same as every other holding. '
               'The company&rsquo;s identity never enters.')
        + expl('Books B, E', _n(conv_pct, "{:,.2f}", "%"), 'conviction weight',
               f'from its <strong>rank</strong> alone — {rank} of {len(screen)} — '
               f'on a straight ramp from {portfolio.CONVICTION_MAX:g}× equal '
               f'weight at the top to {portfolio.CONVICTION_MIN:g}× at the '
               f'bottom, then clamped back inside those bounds. The <em>size</em> '
               f'of its score lead is deliberately ignored: the ranking is what a '
               f'score of this kind can honestly assert; the gaps between scores '
               f'are not.')
        + f'<p class="cofoot"><strong>Simulated holding right now:</strong> '
          f'{held_html}.</p>')

    # ---- where the stop sits ----
    vol = w.get("vol")
    dist = portfolio.stop_distance({"metrics": {t: {"vol": vol}}}, t)
    weekly = (float(vol) / 100.0 / (52 ** 0.5) * 100) if vol else None
    raw = weekly * portfolio.STOP_SIGMA if weekly else None
    clamped = (raw is not None
               and abs(raw / 100 - dist) > 1e-9)
    level = px * (1 - dist) if px else None
    stop = (
        '<h2 class="section-head">Where its stop sits, and why</h2>'
        + expl('Its own volatility', _n(vol, "{:,.1f}", "%"), 'step 1',
               'measured from this company&rsquo;s own price history'
               + ('' if vol else ' — <strong>missing</strong>, so the default '
                  'stop distance is used instead'))
        + expl('One week of it', _n(weekly, "{:,.1f}", "%"), 'step 2',
               'the annual figure divided by the square root of 52')
        + expl(f'Multiplied by {portfolio.STOP_SIGMA:g}', _n(raw, "{:,.1f}", "%"),
               'step 3', 'far enough out that ordinary weekly noise does not '
               'trigger it')
        + expl(f'Held within {portfolio.STOP_MIN:.0%}–{portfolio.STOP_MAX:.0%}',
               f'{dist:.1%}', 'the stop',
               'clamped — the raw figure fell outside the bounds' if clamped
               else 'inside the bounds, so it stands unchanged')
        + f'<p class="cofoot">Books C and E sell it if it falls <strong>{dist:.1%}</strong> '
        f'below its highest close since purchase — from today&rsquo;s '
        f'{_n(px, "${:,.2f}")} that would be {_n(level, "${:,.2f}")}, and the '
        f'level rises with the price but never falls. Books A, B and D hold it '
        f'through anything, on purpose: they are the control that shows whether '
        f'stopping out helped or simply sold the dips. A simulated stop is '
        f'optimistic — real ones gap through in stocks this thin, which is why an '
        f'extra {portfolio.STOP_SLIPPAGE_BPS:.0f} basis points is charged on every '
        f'stop exit and books C and E should still be read as a best case.</p>')

    # ---- how it fits ----
    fit = (
        '<h2 class="section-head">How it fits the whole</h2>'
        '<div class="note-box">It is one of twenty-five, and it is meant to be. '
        'No single holding is supposed to carry the result, and the system has no '
        'view about this company that survives its leaving the screen — when it '
        'drops out, it is sold, whatever anyone thinks of it. The purpose of the '
        'whole exercise is narrow: to find out, on forward evidence rather than '
        'backtest, whether ranking small companies this way beats simply owning '
        'the small-cap growth index after the cost of all that trading. That '
        'question is still open, and the trading cost is the part most likely to '
        'settle it. Nothing here is a recommendation and no money is invested.</div>')

    # ---- anything filed or reported about this ticker today ----
    ev_bits = []
    for key, lab in (("filings_material", "material event (8-K)"),
                     ("filings_activist", "5%+ stake disclosed (13D/13G)"),
                     ("filings_offering", "share offering filed (S-1/424B)")):
        for f in (sc.get(key) or []):
            if f.get("ticker") == t:
                ev_bits.append(f'<div class="item"><a href="{esc(f["link"])}" '
                               f'target="_blank" rel="noopener">{lab}</a>'
                               f'<div class="meta">{esc(f.get("form") or "")} · '
                               f'filed {esc(f["date"])}</div></div>')
    for n in (sc.get("news") or []):
        if n.get("ticker") == t:
            ev_bits.append(f'<div class="item"><a href="{esc(n["link"])}" '
                           f'target="_blank" rel="noopener">{esc(n["title"])}</a>'
                           f'<div class="meta">{esc(n["source"])} · headline matched '
                           f'by ticker, never scored</div></div>')
    events = (('<h2 class="section-head">Filed or reported recently</h2>'
               + "".join(ev_bits[:8])
               + '<p class="cofoot">Matched from SEC EDGAR and public feeds. These '
                 'are context for a human reader and are never scored — a headline '
                 'cannot move this company up the screen.</p>')
              if ev_bits else "")

    body = (f'<div class="wrap co">{head}{intro}{company}{scoring}{sizing}{stop}'
            f'{fit}{events}'
            f'<footer><p><strong>Not investment advice.</strong> Facts produced by '
            f'fixed, published rules from public data; positions described are '
            f'simulated. Generated {esc(date_line)}. This page is a snapshot — the '
            f'screen is rebuilt every run and this company may not be on the next '
            f'one. <a href="../screen.html">Back to the screen</a>.</p></footer></div>')
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<title>{esc(t)} — {esc(row.get("name") or "")} — Basis Points</title>'
            f'{FONTS_LINK}<style>{CSS}</style></head><body>{body}</body></html>')


# How old each input may be before the page says so. These are deliberately
# generous: the job is throttled to roughly six runs a day, so a few hours of
# age is normal operation, not a fault.
FRESH_LIMITS = (
    ("last_full_h", 26, "the full measurement sweep",
     "every company measure on the page is carried over from that sweep"),
    ("quote_median_h", 30, "prices",
     "today's moves, the screen's momentum ranks and the simulated book "
     "valuations all rest on these"),
    ("bench_h", 72, "the benchmark",
     "past this the books STOP marking themselves against it, so every "
     "excess-return figure freezes where it was"),
    ("earnings_h", 96, "the earnings calendar",
     "the 'reports in N days' flags go stale first"),
    ("universe_h", 360, "the company list from the SEC",
     "new listings stop being discovered"),
)


def staleness_alerts(data):
    """What is too old, stated in terms of what it breaks.

    NOTE the limit of a static page: it is rendered once and served unchanged
    until the next run, so it cannot know how long it has been sitting in front
    of a reader. What it can do — and what this does — is report the age of the
    data it was BUILT from. That is where this system's characteristic failure
    actually lives: the job keeps running and publishing on schedule while a
    source behind it has been failing for days."""
    f = ((data.get("smallcap") or {}).get("freshness")) or {}
    out = []
    for key, limit, what, why in FRESH_LIMITS:
        age = f.get(key)
        if age is None or age <= limit:
            continue
        out.append({"what": what, "why": why, "age_h": age, "limit_h": limit})
    return out


def render_alert_banner(alerts):
    if not alerts:
        return ""
    def line(a):
        d = a["age_h"] / 24
        old = f'{a["age_h"]:.0f} hours' if a["age_h"] < 48 else f'{d:.1f} days'
        return (f'<li><strong>{esc(a["what"])}</strong> — {old} old '
                f'(expected under {a["limit_h"]}h). {esc(a["why"])}.</li>')
    return ('<div class="alarm" role="alert"><strong>This page is built on stale '
            'data.</strong><ul>' + "".join(line(a) for a in alerts) +
            '</ul><p>The page itself rebuilt on schedule — which is exactly how '
            'this kind of failure hides. Numbers below are shown as they stand; '
            'treat them as of the ages above, not as of now.</p></div>')


def render_freshness(data):
    """The age of every input, always shown — not only when something is wrong.
    A panel that appears only on failure teaches nobody what normal looks
    like."""
    f = ((data.get("smallcap") or {}).get("freshness")) or {}
    if not f:
        return ""
    rows = []
    for key, limit, what, why in FRESH_LIMITS:
        age = f.get(key)
        if age is None:
            rows.append(f'<tr><td class="l">{esc(what)}</td><td>—</td>'
                        f'<td class="l">not recorded</td></tr>')
            continue
        bad = age > limit
        shown = f'{age:.1f}h' if age < 48 else f'{age / 24:.1f} days'
        rows.append(
            f'<tr><td class="l">{esc(what)}</td>'
            f'<td class="{"down" if bad else "up"}">{shown}</td>'
            f'<td class="l">{"too old — " if bad else ""}{esc(why)}</td></tr>')
    st = data.get("stats") or {}
    errs = (st.get("feed_errors") or []) + (st.get("market_errors") or [])
    err_html = ""
    if errs:
        err_html = ('<p class="cofoot">Sources that failed on the most recent '
                    'run: ' + "; ".join(f'<strong>{esc(str(n))}</strong> '
                                        f'({esc(str(m))})' for n, m in errs[:8]) +
                    '. A failed source does not blank the page — the previous '
                    'value is carried, which is why the ages above matter.</p>')
    return (
        '<h2 class="section-head">How fresh the data is</h2>'
        f'<div class="tblwrap"><table class="screen">'
        '<tr><th class="l">Input</th><th>Age</th>'
        '<th class="l">What it affects</th></tr>'
        f'{"".join(rows)}</table></div>'
        '<p class="cofoot"><strong>This page cannot tell you how old it is.</strong> '
        'It is written once and served unchanged until the next run, so it has no '
        'way to know whether you are reading it one minute or one month later. '
        'The ages above are of the DATA it was built from, measured when it was '
        'built. Check the timestamp in the header for that. If it is more than a '
        'few hours behind the current time, the publishing job has stopped and '
        'nothing on these pages will say so.</p>' + err_html)


def render_changes(data, known=None):
    """What moved since the previous published screen."""
    sc = data.get("smallcap") or {}
    ch = sc.get("changes") or {}
    pf = data.get("portfolio") or {}
    parts = []
    if ch.get("prev_day"):
        ent, left = ch.get("entered") or [], ch.get("left") or []
        parts.append(
            f'<p class="secsub">Against the previous published screen, '
            f'<strong>{esc(ch["prev_day"])}</strong>. {ch.get("held", 0)} names '
            f'held their place, {len(ent)} entered, {len(left)} dropped out.</p>')
        cols = []
        if ent:
            lis = "".join(
                f'<div class="item"><strong>{co_link(e["ticker"], known=known)}</strong> · '
                f'{esc(e["name"])}<div class="meta">{esc(e.get("ind") or "")} · '
                f'score {e["score"]:.1f}</div></div>' for e in ent)
            cols.append('<section class="col"><h2 class="section-head">Entered the '
                        f'screen</h2>{lis}</section>')
        if left:
            lis = "".join(
                f'<div class="item"><strong>{esc(t)}</strong>'
                '<div class="meta">no longer in the top 25</div></div>'
                for t in left)
            cols.append('<section class="col"><h2 class="section-head">Dropped out'
                        f'</h2>{lis}'
                        '<div class="meta" style="padding-top:8px">A name leaving '
                        'the screen is sold at the next rebalance, whatever anyone '
                        'thinks of it.</div></section>')
        if not ent and not left:
            parts.append('<div class="note-box">The screen is unchanged since '
                         'the previous publication. That is common — the '
                         'measures behind it move slowly, and the books only '
                         'rebalance weekly in any case.</div>')
        if cols:
            parts.append(f'<div class="duo">{"".join(cols)}</div>')
    if pf.get("trades"):
        parts.append('<h2 class="section-head">What the simulated books did</h2>'
                     + trade_list(pf["trades"], known=known)
                     + '<p class="cofoot">Grouped by which books acted. A name '
                       'bought by all five is one entry, not five — but the share '
                       'counts differ across them, and that spread is the '
                       'conviction sizing at work. Two books selling while three '
                       'hold is a stop firing, and is the most informative thing '
                       'this simulation produces.</p>')
    stops = sum(b.get("stops_hit", 0) for b in (pf.get("books") or []))
    if pf.get("books"):
        parts.append(
            f'<div class="note-box"><strong>Stops fired so far: {stops}.</strong> '
            'Only books C and E use them; A, B and D hold through everything on '
            'purpose, so that the difference between them is what a stop is '
            'actually worth.</div>')
    return "".join(parts)


def render_book_page(key, pf, date_line, sections, known=None):
    """One simulated book in full: what it holds, at what size, where each stop
    sits, and what it has actually done.

    Books live one directory down, so every link out of here needs `../`. The
    tab bar marks Portfolios as current — a book is a part of that section, not
    a seventh tab."""
    books = {b["key"]: b for b in (pf.get("books") or [])}
    b = books.get(key)
    det = ((pf.get("detail") or {}).get(key)) or {}
    if not b:
        return None
    ctrl = books.get("A")
    up = "../"

    rules = []
    if det.get("uses_conviction"):
        rules.append("sized by <strong>rank</strong>, from 1.5&times; equal "
                     "weight at the top of the screen down to 0.6&times; at the "
                     "bottom")
    else:
        rules.append("<strong>equal weight</strong> — every holding the same "
                     "size, whatever its rank")
    if det.get("uses_stops"):
        rules.append("a <strong>trailing stop</strong> at 3&times; each name&rsquo;s "
                     "own weekly volatility, checked every day")
    else:
        rules.append("<strong>no stop</strong> — it holds through everything, "
                     "on purpose")
    if det.get("uses_regime"):
        rules.append("<strong>exposure cut</strong> when the small-cap tape "
                     "falls, down to 65% invested in a correction")
    else:
        rules.append("<strong>always fully invested</strong>, whatever the tape "
                     "is doing")

    head = (f'<a class="backlink" href="{up}portfolios.html">&larr; all five '
            f'books</a>'
            f'<h1 class="co-tick">Book {esc(key)}</h1>'
            f'<p class="co-name">{esc(b["label"])} '
            f'<span class="flag offer">SIMULATED</span></p>'
            f'<p class="co-sub">{esc(b["note"])} · opened '
            f'{esc(b.get("started") or "—")}</p>'
            '<ul class="rules">' + "".join(f'<li>{r}</li>' for r in rules) +
            '</ul>')

    exc = b.get("excess")
    facts = [
        ("Value", f'${b["value"]:,.0f}'),
        ("Return", f'{b["ret"]:+.2f}%'),
        ("vs the index", f'{exc:+.2f}%' if exc is not None else "—"),
        ("Worst dip", f'{b["max_drawdown"]:+.1f}%'),
        ("Cost of trading", fmt_friction(b)[0]),
        ("Stops fired", f'{b["stops_hit"]}' if det.get("uses_stops") else "n/a"),
        ("Holdings", f'{b["positions"]}'),
        ("Days running", f'{b["days"]}'),
    ]
    facts_html = "".join(f'<div><div class="trl">{k}</div>'
                         f'<div class="cofv">{v}</div></div>' for k, v in facts)

    chart = equity_chart({"books": [b], "bench_curve": pf.get("bench_curve"),
                          "span": pf.get("span")})

    # how it differs from the control, stated as the only question that matters
    if ctrl and key != "A":
        gap = b["ret"] - ctrl["ret"]
        cost_gap = ((b.get("costs_paid") or 0) - (ctrl.get("costs_paid") or 0))
        verdict = (
            f'<div class="note-box"><strong>Against the control: '
            f'{gap:+.2f}%.</strong> Book A runs the same screen with none of '
            f'this book&rsquo;s rules, and is {ctrl["ret"]:+.2f}%. This book has '
            f'spent ${b.get("costs_paid", 0):,.0f} on trading against the '
            f'control&rsquo;s ${ctrl.get("costs_paid", 0):,.0f} — '
            f'${abs(cost_gap):,.0f} {"more" if cost_gap > 0 else "less"}. '
            f'The rules have to beat '
            f'the control by more than they cost, and after {b["days"]} day'
            f'{"s" if b["days"] != 1 else ""} this number is noise. It is here so '
            f'that it cannot be quietly dropped later if it turns out '
            f'unflattering.</div>')
    else:
        verdict = ('<div class="note-box"><strong>This is the control.</strong> '
                   'It runs the screen with no cleverness at all: equal weight, '
                   'no stop, always fully invested. Every other book has to beat '
                   'it to justify its extra rules and extra trading. If none of '
                   'them do, the honest conclusion is that the overlays are not '
                   'worth running.</div>')

    rows = []
    for h in det.get("holdings") or []:
        if det.get("uses_stops") and h.get("stop_px") is not None:
            stop = (f'<td>${h["stop_px"]:,.2f}</td>'
                    f'<td class="{delta_class(h["room_pct"])}">'
                    f'{h["room_pct"]:+.0f}%</td>')
        else:
            stop = '<td>—</td><td>—</td>'
        rows.append(
            f'<tr><td class="l tick">{co_link(h["ticker"], up + "co/", known)}</td>'
            f'<td>{h["weight"]:.2f}%</td><td>{h["shares"]:,.0f}</td>'
            f'<td>${h["entry_px"]:,.2f}</td><td>${h["px"]:,.2f}</td>'
            f'<td class="{delta_class(h["ret"])}">{h["ret"]:+.1f}%</td>'
            f'{stop}<td>${h["value"]:,.0f}</td></tr>')
    holdings = ""
    if rows:
        holdings = (
            '<h2 class="section-head">What it holds</h2>'
            '<div class="tblwrap"><table class="screen">'
            '<tr><th class="l">Ticker</th><th>Weight</th><th>Shares</th>'
            '<th>Bought at</th><th>Now</th><th>Return</th><th>Stop at</th>'
            '<th>Room</th><th>Value</th></tr>'
            f'{"".join(rows)}</table></div>'
            '<p class="cofoot"><em>Bought at</em> is the average price paid — a '
            'position topped up later carries a blended cost, not the price of '
            'its first share. <em>Room</em> is how far the price can fall before '
            'the stop fires, measured from its highest close since purchase, so '
            'it shrinks on the way down and never widens on the way up. '
            + ('' if det.get("uses_stops") else
               'This book runs no stop, so those two columns are empty by '
               'design rather than for want of data. ')
            + 'All of it is simulated.</p>')

    trades = ""
    if det.get("trades"):
        lis = "".join(
            f'<div class="item"><strong class="{"down" if t["side"] == "sell" else ""}">'
            f'{esc(t["side"].upper())}</strong> '
            f'{co_link(t["ticker"], up + "co/", known)}'
            f'<div class="meta">{esc(t["date"])} · {t["shares"]:,.0f} sh @ '
            f'${t["px"]:,.2f} · cost ${t.get("cost", 0):,.2f}'
            f'{" · " + esc(t["why"]) if t.get("why") else ""}</div></div>'
            for t in det["trades"][:20])
        trades = (f'<h2 class="section-head">What it has done</h2>{lis}'
                  '<p class="cofoot">This book&rsquo;s own trades, newest first, '
                  'each with the friction it paid. Every one of these is '
                  'hypothetical.</p>')

    body = (f'<div class="wrap co">{masthead_html(date_line)}'
            f'{tab_bar("portfolios", sections, up)}'
            f'{head}<div class="cogrid">{facts_html}</div>{chart}{verdict}'
            f'{render_attribution(det.get("attribution"))}'
            f'{holdings}{trades}'
            f'<nav class="pager"><a href="{up}portfolios.html">&larr; all five '
            f'books</a></nav>'
            '<footer><p><strong>Not investment advice.</strong> This book is a '
            'simulation. No money is invested, no order was ever placed, and '
            'hypothetical results omit what hurts real traders most: the market '
            'moving against a real order, taxes, and the nerve to follow a system '
            f'through a losing stretch. Generated {esc(date_line)}.</p>'
            f'</footer></div>')
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<title>Book {esc(key)}, {esc(b["label"])} — Basis Points</title>'
            f'{FONTS_LINK}<style>{CSS}</style></head><body>{body}</body></html>')


def render_decision(data):
    """The pre-registered rule, and where it currently stands.

    Rendered on every build rather than written up in December. A rule that
    only appears on the day it is applied is a rule that can be adjusted on
    the day it is applied."""
    try:
        a = decision.assess(data)
    except Exception:  # noqa: BLE001 — never let the rule break the page
        return ""
    rows = []
    for g in a["gates"]:
        if g.get("passed"):
            mark, cls, state = "&#10003;", "up", "met"
        elif g.get("failed"):
            mark, cls, state = "&#10007;", "down", "not met"
        else:
            mark, cls, state = "&#9675;", "", "too early to say"
        if g["name"].startswith("A signal"):
            got = (f'{g["n"]} of {decision.MIN_INDEP_1W} readings'
                   + (f', mean {g["mean"]:+.2f}%' if g.get("mean") is not None else "")
                   + (f', t = {g["t"]:.2f}' if g.get("t") is not None else ""))
        elif g["name"].startswith("Survives"):
            got = (f'best is book {esc(str(g.get("best")))} at '
                   f'{g["excess"]:+.2f}%' if g.get("excess") is not None
                   else "no book has a full reading yet")
        else:
            got = (f'book {esc(str(g.get("book")))} would be '
                   f'{g["ex_top"]:+.2f}% against the index&rsquo;s '
                   f'{g["bench"]:+.2f}%'
                   if g.get("ex_top") is not None else "needs a running book")
        rows.append(
            f'<tr><td class="l"><span class="{cls}">{mark}</span> '
            f'{esc(g["name"])}</td><td class="l">{esc(g["asks"])}</td>'
            f'<td class="l">{got}</td><td class="l {cls}">{state}</td></tr>')

    # whether the bar can still be met AT ALL, said the day it stops being
    # possible rather than discovered in December
    reach = ((data.get("smallcap") or {}).get("reach")) or {}
    warn = ""
    dead = [h for h, r in reach.items() if not r.get("reachable")]
    if dead:
        bits = "; ".join(
            f'{h}: {reach[h]["have"]} frozen, at most {reach[h]["possible"]} '
            f'possible by then, {reach[h]["target"]} needed' for h in dead)
        warn = (f'<div class="alarm" role="alert"><strong>The bar can no longer '
                f'be met by {esc(a["review_date"])}.</strong> {bits}. A reading '
                f'was missed and the schedule has no spare — so the review will '
                f'return &ldquo;not enough evidence&rdquo; for a data reason, not '
                f'because the strategy failed. The bar is NOT being lowered to '
                f'fit; the shortfall is shown instead.</div>')
    else:
        tight = [h for h, r in reach.items() if r.get("slack", 9) <= 1]
        if tight:
            bits = "; ".join(
                f'{h} has {reach[h]["have"]} of {reach[h]["target"]} with room '
                f'for {reach[h]["slack"]} more missed' for h in tight)
            warn = (f'<p class="cofoot"><strong>The schedule has almost no '
                    f'spare:</strong> {bits}. Readings run end to end from the '
                    f'first published screen to the review date, so a single one '
                    f'skipped makes the bar unreachable. If that happens this '
                    f'panel will say so that day rather than in December.</p>')

    return (
        '<h2 class="section-head">The December decision, decided in advance</h2>'
        f'<div class="note-box"><strong>Written {esc(a["written_on"])}, to be '
        f'applied {esc(a["review_date"])}.</strong> This rule was fixed while '
        'the record was eight readings old and the books three days old — '
        'before anyone knew how it would come out. That is the only time such a '
        'rule can be written honestly. Without one, the review is a person '
        'looking at a number they have already seen and deciding what it means, '
        'which is how a project runs forever and never concludes anything. '
        '<strong>No outcome below authorises real money.</strong> Twelve weeks '
        'cannot tell a real edge from a lucky one, and saying so now prevents '
        'the claim being made later.</div>'
        '<div class="tblwrap"><table class="screen">'
        '<tr><th class="l">Gate</th><th class="l">What it asks</th>'
        '<th class="l">Where it stands</th><th class="l">Status</th></tr>'
        f'{"".join(rows)}</table></div>'
        f'{warn}'
        f'<div class="note-box"><strong>As things stand: {esc(a["title"])}.</strong> '
        f'{esc(a["body"])}</div>'
        '<p class="cofoot">One clause matters more than the gates. <strong>A '
        'disappointing result does not authorise changing the scoring rules.</strong> '
        'The only outcomes are to continue unchanged or to stop. Tuning the model '
        'after seeing its record is how a system is made to look good in hindsight, '
        'and it is the exact thing the freeze exists to prevent — so it is ruled out '
        'here, in writing, in advance. Rules may change only to fix a defect: '
        'something that does not do what it is documented to do.</p>')


def render_attribution(a, label=""):
    """Where a book's return came from — arithmetic that must add up."""
    if not a:
        return ""
    if not a.get("reconciles"):
        note = (f'<div class="alarm" role="alert"><strong>These parts do not add '
                f'up.</strong> They come to {a["total_pct"]:+.2f}% against an '
                f'actual {a["actual_pct"]:+.2f}% — a gap of '
                f'{a["residual_pct"]:+.4f}. Something is unaccounted for and the '
                f'breakdown below should not be trusted until it is found.</div>')
    else:
        note = ""
    parts = [("Holdings, unrealised", a["unrealised_pct"]),
             ("Realised on sales", a["realised_pct"]),
             ("Cost of trading", a["cost_pct"])]
    bars = "".join(
        f'<tr><td class="l">{esc(k)}</td>'
        f'<td class="{delta_class(v)}">{v:+.2f}%</td></tr>' for k, v in parts)
    top = "".join(
        f'<span><b>{esc(r["ticker"])}</b> '
        f'<em class="{delta_class(r["pct"])}">{r["pct"]:+.2f}%</em></span>'
        for r in (a.get("contributors") or [])[:5])
    return (
        f'<h2 class="section-head">Where the return came from</h2>{note}'
        '<div class="tblwrap"><table class="screen">'
        '<tr><th class="l">Part</th><th>Of starting capital</th></tr>'
        f'{bars}'
        f'<tr><td class="l"><strong>Total</strong></td>'
        f'<td class="{delta_class(a["total_pct"])}"><strong>'
        f'{a["total_pct"]:+.2f}%</strong></td></tr></table></div>'
        f'<p class="cofoot">{a["winners"]} holdings up, {a["losers"]} down. '
        f'Best {a.get("top_n", 3)} contributed {a["top_pct"]:+.2f}%; '
        f'<strong>without them the book would be {a["ex_top_pct"]:+.2f}%</strong>. '
        'That second figure is the one that matters: a book ahead only because '
        'of its best few names has shown that a few names went up, not that the '
        'screen works. These parts are checked against the book&rsquo;s actual '
        'change in value on every build, and the difference is published above '
        'rather than absorbed.</p>'
        + (f'<div class="legend">{top}</div>' if top else ""))


def render_explainer(data):
    """The whole thing in plain English, GENERATED rather than written.

    There was a hand-written version of this. It was accurate on the 5th of
    September and obsolete by the 21st: it described a news desk that had been
    retired, and had not heard of the paper portfolios, the stops or the
    December decision. A hand-written explainer of a system that changes weekly
    will always be out of date, and an explanation that is quietly wrong is
    worse than none. So the numbers here come from the same data as every other
    page, and the prose describes only things the code actually does."""
    sc = data.get("smallcap") or {}
    pf = data.get("portfolio") or {}
    cov = sc.get("coverage") or {}
    ev = sc.get("evaluation") or {}
    reach = sc.get("reach") or {}
    books = pf.get("books") or []
    a = ((pf.get("detail") or {}).get("A") or {}).get("attribution") or {}
    ctrl = next((b for b in books if b["key"] == "A"), None)

    uni = cov.get("universe") or 0
    scored = cov.get("scored") or 0
    band = cov.get("in_band") or 0
    one = ev.get("1w") or {}
    got1 = one.get("indep") or 0
    need1 = smallcap.FREEZE_TARGET["1w"]

    if ctrl:
        where = (f'The books have been running for {ctrl["days"]} day'
                 f'{"s" if ctrl["days"] != 1 else ""}. The plain one is '
                 f'{ctrl["ret"]:+.2f}%, of which '
                 f'{abs(a.get("cost_pct", 0)):.2f} percentage points went on the '
                 f'cost of trading alone.')
    else:
        where = 'The books have not started trading yet.'

    return (
        '<h2 class="section-head">In plain English</h2>'
        '<div class="plain">'

        '<h3>What this is</h3>'
        '<p>A machine that reads public financial data about small American '
        'companies, scores them by a fixed set of rules, and publishes the '
        'twenty-five that score highest. Nobody chooses the companies. Nobody '
        'reads about them. The rules are written down, they do not change, and '
        'the same rules applied to the same data would produce the same list '
        'tomorrow.</p>'

        '<h3>What it does, each time it runs</h3>'
        f'<p>It starts from every company filed with the American financial '
        f'regulator &mdash; about {uni:,} of them &mdash; and throws almost all '
        f'of them away. Too big, too small, too cheap, too rarely traded, too '
        f'little revenue: roughly {band:,} survive as the right size, and about '
        f'{scored:,} have enough measured about them to be scored at all. Those '
        f'are ranked on three things, in these proportions:</p>'
        '<ul class="rules">'
        '<li><strong>Growth (40%)</strong> &mdash; is revenue rising, and is it '
        'rising faster than it was? Compared against other companies in the '
        'same industry, so a good year for mining does not make every miner '
        'look clever.</li>'
        '<li><strong>Momentum (40%)</strong> &mdash; has the share price been '
        'rising, divided by how jumpy that price is. A steady climb counts for '
        'more than a violent one.</li>'
        '<li><strong>Quality (20%)</strong> &mdash; debt, cash, whether margins '
        'are improving, and whether the company keeps issuing new shares.</li>'
        '</ul>'
        '<p>Price is deliberately <em>not</em> part of the score. This looks for '
        'companies that are growing, not companies that are cheap &mdash; those '
        'are different questions and mixing them produces an answer to '
        'neither.</p>'

        '<h3>The five portfolios, and why there are five</h3>'
        f'<p>Five imaginary pots of money, each with a notional '
        f'${(pf.get("assumptions") or {}).get("capital", 100000):,.0f} '
        f'&mdash; these are American shares, priced in dollars &mdash; all '
        f'buying the same twenty-five companies and all paying '
        f'the same trading costs. They differ only in their rules. One holds '
        'everything in equal amounts and never sells early &mdash; that is the '
        '<strong>control</strong>. The others add one idea each: bigger bets on '
        'higher-ranked companies; an automatic sell if a holding falls far '
        'enough; holding back cash when the market is falling; and all three '
        'together.</p>'
        '<p>The point of the plain one is that it is the thing to beat. Any '
        'clever rule has to earn back the extra trading it causes. If the '
        'clever books do not finish ahead of the simple one, the cleverness is '
        f'costing money for nothing. {where}</p>'
        '<p><strong>No money is involved anywhere in this.</strong> No order '
        'has ever been placed. These are simulations, and simulations leave out '
        'the things that hurt real traders most: the price moving against you '
        'as you buy, tax, and the nerve required to keep following a system '
        'through a bad stretch.</p>'

        '<h3>What it is trying to find out</h3>'
        '<p>One question: <em>does ranking small companies this way beat simply '
        'buying the whole small-company market, after paying for all the '
        'trading it takes?</em> That last clause is where most systems like '
        'this quietly fail.</p>'
        f'<p>To answer it, every published list is checked again about a week '
        f'later and the result is written down and never touched again. '
        f'{got1} of the {need1} separate weekly checks needed '
        f'{"is" if got1 == 1 else "are"} done. The answer gets judged on '
        f'{esc(decision.REVIEW_DATE)}, against a rule written in advance on '
        f'{esc(decision.WRITTEN_ON)}, before anyone knew how it would turn out. '
        f'That rule can say &ldquo;stop&rdquo;, and none of its outcomes allows '
        f'real money to be risked.</p>'

        '<h3>Things worth distrusting</h3>'
        '<ul class="rules">'
        '<li>It is young. A few weeks of results tell you almost nothing, and '
        'anyone reading a short run of numbers as skill is fooling '
        'themselves.</li>'
        '<li>All the company data comes from one free supplier. If that '
        'supplier is wrong about a company, so is this.</li>'
        '<li>Nothing here understands any business. It has an industry label, '
        'some filed figures and a price history. It cannot tell a genuine '
        'growth company from an accounting artefact.</li>'
        '<li>Buying companies that have already risen is a well-known approach '
        'that works until it stops, usually suddenly.</li>'
        '<li>The trading costs are estimates. Real ones in companies this small '
        'are often worse.</li>'
        '</ul>'
        '<p><strong>None of this is investment advice</strong>, and it is not '
        'written by anyone qualified to give any. It is a record of what a set '
        'of fixed rules did, published so that it can be checked.</p>'
        '</div>')


def build_sections(data):
    """Build each section's inner HTML, keyed by the page it will become.

    Sections are built ONCE and handed to the page renderer, so the tab bar and
    the pages that exist can never disagree: a tab is shown only for a section
    that actually produced content."""
    sc = data.get("smallcap") or {}
    cov = sc.get("coverage") or {}
    # exactly the tickers write_company_pages() will write a page for
    known = {t for t in (safe_ticker(r.get("ticker"))
                         for r in (sc.get("screen") or [])) if t}
    # The page is assembled as NAMED SECTIONS rather than one long scroll.
    # Each carries an anchor, so the nav can jump to it and a reader can link
    # to the part they care about instead of describing where to scroll.
    secs = []                      # (page slug, html)

    # the experiment's state leads: the goal is to settle whether this screen is
    # worth trading, so the answer-so-far outranks today's list of companies
    secs.append(("index", render_progress(data) + render_decision(data)))

    # ---- the market backdrop, kept as context and never scored ----
    ctx_labels = {"Russell 2000", "VIX", "10-yr Treasury", "WTI crude"}
    ctx = [t for t in (data.get("market") or []) if t["label"] in ctx_labels]
    mkt = [render_tiles(ctx)] if ctx else []

    parts = []                     # the screen section
    if sc.get("note") == "waiting-for-key":
        parts.append(
            '<div class="note-box"><strong>Scorecard pending.</strong> The small-cap '
            'engine is installed and the company universe is loaded; measurement begins '
            'automatically once the Finnhub key is added.</div>')
    reg = sc.get("regime") or {}
    if reg.get("label"):
        r26 = (f', {reg["r26"]:+.1f}% over 26 weeks' if reg.get("r26") is not None else "")
        mkt.append(
            f'<div class="coverage">market context: small-cap growth tape '
            f'<strong>{esc(reg["label"])}</strong> — Russell 2000 Growth ETF '
            f'{reg["r13"]:+.1f}% over 13 weeks{r26} · context only, never affects scores</div>')
    if cov:
        parts.append(
            f'<div class="coverage">universe {cov.get("universe", 0):,} companies · '
            f'profiled {cov.get("profiled", 0):,} · in size band {cov.get("in_band", 0):,} · '
            f'fully measured {cov.get("measured", 0):,} · '
            f'passing filters {cov.get("scored", 0):,} · '
            f'below revenue floor {cov.get("below_floor", 0):,} · '
            f'scores are relative to today’s eligible set</div>')
        if (cov.get("universe") and not sc.get("note")
                and cov.get("profiled", 0) < 0.9 * cov["universe"]):
            parts.append(
                '<div class="note-box"><strong>Bootstrap in progress.</strong> '
                'Company coverage is still building, so today’s screen is drawn from '
                'a partial, alphabetically skewed slice of the universe and is not '
                'yet representative.</div>')

    screen = sc.get("screen") or []
    ev = sc.get("evaluation") or {}
    if ev:
        bits = []
        for h, lab in (("1w", "1 week out"), ("4w", "4 weeks out")):
            if h in ev:
                e = ev[h]
                dropped = (f'; {e["dropped"]} name-readings dropped'
                           if e.get("dropped") else "")
                peer = (f'; <strong>{e["vs_peers"]:+.2f}% against {e.get("peer_n", 100)} '
                        f'randomly chosen eligible names</strong>, equally '
                        f'weighted like the screen itself'
                        if e.get("vs_peers") is not None else "")
                bits.append(f'{lab}: {e["excess"]:+.2f}% vs the Russell 2000 Growth '
                            f'ETF ({e["days"]} frozen cohort reading'
                            f'{"s" if e["days"] != 1 else ""} / '
                            f'{e.get("indep", "?")} independent{dropped}){peer}')
        parts.append('<div class="note-box"><strong>Live track record.</strong> '
                     'Average forward return of published screens minus the benchmark — '
                     + "; ".join(bits) +
                     '. Each published screen’s return is measured once, about a week '
                     'after publication, then frozen — never recomputed and never '
                     'backfilled — so readings accumulate as real forward evidence. '
                     'Cohorts published within the same week overlap, so the '
                     '<em>independent</em> count, not the total, is the honest '
                     'sample size. <strong>Two yardsticks, deliberately.</strong> '
                     'The index tells you whether the whole package beat simply '
                     'buying small-cap growth. The random draw tells you '
                     'something narrower and more important: whether the '
                     '<em>ranking</em> is doing anything, with the universe and '
                     'the equal weighting held constant. Equal-weighting a '
                     'small-cap universe has historically paid something on its '
                     'own, so a lead over the index alone could be the weighting '
                     'rather than the picking. The draw is seeded from the date, '
                     'so it cannot be re-rolled until it flatters.</div>')
    elif screen:
        parts.append('<div class="note-box"><strong>Live track record:</strong> '
                     'collecting. Each day’s screen is logged; the first 1-week '
                     'reading appears once a logged screen is a week old. '
                     'A real forward record, not a backtest.</div>')
    if screen:
        head = ('<tr><th class="l">#</th><th class="l">Ticker</th>'
                '<th class="l">Company</th><th class="l">Industry</th><th>Mkt cap</th>'
                '<th>EV/Rev</th><th>Rev growth</th><th>13-wk</th><th>vs 52w high</th>'
                '<th>Today</th><th>Score</th><th class="l">Flags</th></tr>')
        trs = []
        for i, r in enumerate(screen, 1):
            dp = r.get("dp")
            dp_td = (f'<td class="{delta_class(dp)}" data-l="Today">{dp:+.1f}%</td>'
                     if dp is not None else '<td data-l="Today">—</td>')
            fh52 = (f'{r["from_high"]:+.1f}%' if r.get("from_high") is not None else "—")
            sub = r.get("sub") or {}
            sub_t = (f'growth {sub.get("g", "?")} · momentum {sub.get("m", "?")} · '
                     f'quality {sub.get("q", "?")}')
            flag_cls = {"new": "new", "ins+": "ins", "act+": "act", "offer": "offer"}
            flags = "".join(
                f'<span class="flag {flag_cls.get(f, "")}">{esc(f)}</span>'
                for f in (r.get("flags") or []))
            ev_rev = (f'{r["ev_rev"]:.1f}×' if r.get("ev_rev") is not None else "—")
            # every cell carries its own column name. On a phone the table
            # becomes a stack of cards and the header row is gone, so a bare
            # "+81.5%" would have nothing to say what it measures.
            trs.append(
                f'<tr title="{esc(sub_t)}">'
                f'<td class="l rank" data-l="Rank">{i}</td>'
                f'<td class="l tick" data-l="Ticker">'
                f'{co_link(r["ticker"], known=known)}</td>'
                f'<td class="l nm" data-l="Company">{esc(r["name"])}</td>'
                f'<td class="l" data-l="Industry">{esc(r["ind"])}</td>'
                f'<td data-l="Mkt cap">{_fmt_mcap(r["mcap"])}</td>'
                f'<td data-l="EV/Rev">{ev_rev}</td>'
                f'<td data-l="Rev growth">{r["rev_g"]:+.1f}%</td>'
                f'<td data-l="13-wk">{r["r13"]:+.1f}%</td>'
                f'<td data-l="vs 52w high">{fh52}</td>'
                f'{dp_td}'
                f'<td data-l="Score"><strong>{r["score"]:.1f}</strong></td>'
                f'<td class="l" data-l="Flags">{flags}</td></tr>')
        parts.append(f'<div class="tblwrap"><table class="screen cards">'
                     f'{head}{"".join(trs)}</table></div>')
    elif not sc.get("note") and cov:
        parts.append('<div class="note-box">The scorecard is still building coverage — '
                     'the ranked screen appears once enough companies are fully '
                     'measured. Check back within a day.</div>')

    # the books come before the screen that feeds them: the books are the
    # subject of the experiment, the screen is one of its inputs
    secs.append(("portfolios", render_portfolio(data.get("portfolio"), known)))
    secs.append(("screen", "".join(parts)))
    if mkt:
        secs.append(("market", "".join(mkt)))

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
    def filing_col(key, heading, tail):
        rows = sc.get(key) or []
        if not rows:
            return
        lis = "".join(
            f'<div class="item"><a href="{esc(f["link"])}" target="_blank" rel="noopener">'
            f'<strong>{esc(f["ticker"])}</strong> · {esc(f["name"])}</a>'
            f'<div class="meta">{esc(f.get("form") or "")} {tail} {esc(f["date"])}</div></div>'
            for f in rows)
        cols.append(f'<section class="col"><h2 class="section-head">{heading}</h2>{lis}</section>')

    filing_col("filings_material", "Material events (8-K)", "filed")
    filing_col("filings_activist", "Activist stakes (13D/13G)", "filed")
    filing_col("filings_offering", "Offering filings (dilution)", "filed")
    if data.get("econ_calendar"):
        cols.append(render_econ_column(data["econ_calendar"]))
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
        secs.append(("signals", f'<div class="duo">{"".join(cols)}</div>'))

    method = (
        '<div class="method"><strong>Methodology (model v3.1, Sep 17, 2026).</strong> '
        '<em>v3.1 corrected two scoring defects and restarted the record: '
        'companies labelled “Communications” had been ranked against no peer '
        'group, and tied factor values had been ordered by alphabetical '
        'position. Corrected one week in, while a restart was still cheap.</em> '
        'Eligibility: U.S. listed common stocks (one security per company — the common '
        'ticker), market value $300M–$2B, price ≥ $2, 10-day average volume ≥ 50k shares, '
        'trailing-12-month revenue ≥ $50M, no over-the-counter listings, no closed-end '
        'funds. Composite score = 40% growth (0.5 × trailing revenue growth + 0.3 × '
        '3-year growth + 0.2 × acceleration, each ranked <em>within industry group</em> so '
        'cyclical windfalls compete with their own kind) + 40% momentum (blended '
        '13/26-week return divided by 3-month volatility, so calm advances outrank violent '
        'spikes) + 20% quality (graded cash-flow funding or months of runway; leverage, '
        'ranked within industry; margin direction; low shareholder dilution — measured '
        'from our own share-count history once it is old enough, a 5-year proxy until '
        'then). Percentile ranks; missing sub-measures rank neutral; scores are relative '
        'to the day’s eligible set. Publication requires positive trailing revenue '
        'growth; at most 5 names per industry group; newcomers to the candidate list '
        'carry a one-day 3% score penalty. EV/Rev (enterprise value ÷ revenue) is shown '
        'for context and deliberately <em>not scored</em> — this is a growth screen, not '
        'a value screen. Hover a row for its factor breakdown. Flags: <em>new</em> = '
        'entered the list today; <em>E-Nd</em> = reports earnings in N days; '
        '<em>ins+</em> = net insider open-market buying in the last 30 days; '
        '<em>8-K</em> = filed a material-event report with the SEC in the last 3 days; '
        '<em>act+</em> = an investor disclosed a 5%+ stake (13D/13G) in the last 7 days; '
        '<em>offer</em> = filed a securities registration/prospectus (S-1/424B, a '
        'potential dilution event) in the last 7 days. These filing flags are matched '
        'from SEC EDGAR and, like all news signals, inform context only — they are '
        'never scored. '
        '<strong>Freeze:</strong> scoring rules do not change until the live record '
        'holds 12 independent 1-week and 3 independent 4-week readings; the record is '
        'kept per model version and never backfilled. Known limitations: static factor '
        'weights; momentum strategies historically suffer sudden reversals in small '
        'caps; all measures come from one free data vendor and quotes may be a few '
        'hours old. Data: SEC (universe), Finnhub (measures). Facts by fixed rules — '
        '<strong>not investment advice</strong>.</div>')
    secs.append(("changes", render_changes(data, known)))
    secs.append(("method",
                 render_explainer(data) + method + render_freshness(data)))
    return {slug: html for slug, html in secs if html}


# Each section is its own page, and the tabs are ordinary links. The page runs
# no JavaScript, so that is not a workaround — it is the better form: every
# section gets an address that can be linked, bookmarked, shared and reached
# with the back button, and nothing depends on a script to be readable.
SECTIONS = (
    ("index", "Progress", "Where the trading system stands",
     "The simulated books, the evidence they have produced so far, and how far "
     "that is from enough to judge."),
    ("portfolios", "Portfolios",
     'Paper portfolios <span class="flag offer">SIMULATED</span>',
     "Five books over the same screen, the same prices and the same costs, "
     "differing only in their rules. No money is invested."),
    ("changes", "Changes", "What changed",
     "Which companies entered and left the screen, and what the simulated "
     "books did about it."),
    ("screen", "Screen", "Today’s growth screen",
     "The ranked list the books trade. Click any ticker for the arithmetic "
     "behind its score, its position size and its stop."),
    ("market", "Market", "Market context",
     "Background only. None of this enters a score."),
    ("signals", "Signals", "Company signals",
     "Company events matched against the small-cap band. These feed the "
     "analysis as context and are never scored — a headline cannot move a "
     "company up the screen."),
    ("method", "Method", "How it works",
     "The rules in full, including what they are known to get wrong."),
)


def _href(slug, prefix=""):
    return f"{prefix}{slug}.html"


def tab_bar(current, available, prefix=""):
    """The tabs. A tab is rendered only for a section that actually produced
    content, so the bar can never offer a page that was not written."""
    out = []
    for slug, label, _title, _sub in SECTIONS:
        if slug not in available:
            continue
        if slug == current:
            out.append(f'<span class="tab cur" aria-current="page">'
                       f'{esc(label)}</span>')
        else:
            out.append(f'<a class="tab" href="{_href(slug, prefix)}">'
                       f'{esc(label)}</a>')
    return f'<nav class="tabs" aria-label="Sections">{"".join(out)}</nav>'


def render_page(slug, sections, date_line, alerts=(), prefix=""):
    """One section, as a standalone page."""
    meta = {s[0]: s for s in SECTIONS}[slug]
    _slug, label, title, sub = meta
    # the progress panel is a self-contained card carrying its own heading
    head = ("" if slug == "index" else
            f'<h2 class="sechead">{title}</h2>'
            + (f'<p class="secsub">{sub}</p>' if sub else ""))

    order = [s[0] for s in SECTIONS if s[0] in sections]
    i = order.index(slug)
    steps = []
    if i > 0:
        pv = {s[0]: s for s in SECTIONS}[order[i - 1]]
        steps.append(f'<a href="{_href(pv[0], prefix)}">&larr; {esc(pv[1])}</a>')
    if i < len(order) - 1:
        nx = {s[0]: s for s in SECTIONS}[order[i + 1]]
        steps.append(f'<a href="{_href(nx[0], prefix)}">{esc(nx[1])} &rarr;</a>')
    walk = f'<nav class="pager">{"".join(steps)}</nav>' if steps else ""

    # the alarm sits ABOVE the tabs, on every page. Stale data does not become
    # less stale because the reader happened to open a different section
    body = (f'<div class="wrap">{masthead_html(date_line)}'
            f'{render_alert_banner(alerts)}'
            f'{tab_bar(slug, sections, prefix)}'
            f'<main class="psec">{head}{sections[slug]}</main>{walk}'
            '<footer><p><strong>Not investment advice.</strong> Facts by fixed, '
            'published rules from public data. Inputs: SEC EDGAR (company universe '
            f'and filings), Finnhub (measures), and {len(FEEDS)} news and '
            'press-release feeds matched against the small-cap band as signals — '
            'headlines link to and belong to their publishers. Generated '
            f'{esc(date_line)} · refreshes on a schedule.</p></footer></div>')
    plain = re.sub(r"<[^>]+>", "", title).strip()
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<meta http-equiv="refresh" content="900">'
            f'<title>{esc(plain)} — Basis Points</title>{FONTS_LINK}'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def render_site(data):
    """Every page of the site, keyed by file name."""
    date_line = datetime.fromisoformat(
        data["generated_at"]).astimezone().strftime("%A, %B %-d, %Y · %-I:%M %p %Z")
    sections = build_sections(data)
    alerts = staleness_alerts(data)
    pages = {f"{slug}.html": render_page(slug, sections, date_line, alerts)
             for slug in sections}
    # smallcap.html was the original address and may be linked from elsewhere;
    # it keeps working rather than becoming a dead link
    if "index.html" in pages:
        pages["smallcap.html"] = pages["index.html"]
    return pages, sections, date_line

# --------------------------------------------------------------- main --------


def build_data():
    now = datetime.now(timezone.utc)
    items, filings, feed_errors = fetch_all_feeds()
    tiles, market_errors = fetch_markets()
    fresh = prepare_items(items, now)

    # wires are analysis inputs matched against the band, never headline material
    top = [it for it in fresh if it["category"] != "wire"][:TOP_COUNT]

    # pass raw filing titles through (CIK + party intact) so record_filings can
    # parse form type and the filed-by/subject party itself
    filings_fresh = [f for f in filings
                     if not (f["published"]
                             and (now - f["published"]) > timedelta(hours=48))]

    def slim(it):
        return {
            "title": it["title"], "link": it["link"], "source": it["source"],
            "published": it["published"].isoformat() if it["published"] else None,
            "summary": it.get("summary", ""),
        }

    try:
        smallcap.record_filings([slim(f) for f in filings_fresh])
        sc_summary, sc_calls = smallcap.update()
        sc_summary["news"] = smallcap.match_news([slim(i) for i in fresh[:400]])
    except Exception as e:  # noqa: BLE001 — the page must render even if this fails
        sc_summary, sc_calls = None, 0
        market_errors.append(("Small-cap engine", _scrub(e)))

    try:
        paper = portfolio.summarize(
            portfolio.update(smallcap.load_cache(),
                             (sc_summary or {}).get("screen") or []))
    except Exception as e:  # noqa: BLE001 — a simulation must never break the page
        paper = None
        market_errors.append(("Paper portfolio", _scrub(e)))

    try:
        econ = fred_calendar()
    except Exception as e:  # noqa: BLE001
        econ = []
        market_errors.append(("FRED calendar", _scrub(e)))

    data = {
        "generated_at": now.isoformat(),
        "market": tiles,
        "tape_line": tape_line(tiles),
        "top": [slim(i) for i in top],
        "smallcap": sc_summary,
        "portfolio": paper,
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


def write_book_pages(data):
    """One page per simulated book, under site/book/."""
    pf = data.get("portfolio") or {}
    if not pf.get("books"):
        return 0
    date_line = datetime.fromisoformat(
        data["generated_at"]).astimezone().strftime("%A, %B %-d, %Y · %-I:%M %p %Z")
    sections = build_sections(data)
    known = {t for t in (safe_ticker(r.get("ticker"))
                         for r in ((data.get("smallcap") or {}).get("screen") or []))
             if t}
    bdir = SITE / "book"
    bdir.mkdir(exist_ok=True)
    written = set()
    for b in pf["books"]:
        key = b.get("key")
        if not (isinstance(key, str) and key.isalnum() and len(key) <= 3):
            continue
        html = render_book_page(key, pf, date_line, sections, known)
        if html:
            (bdir / f"{key}.html").write_text(html, encoding="utf-8")
            written.add(f"{key}.html")
    # a retired book must not leave a page the tables no longer link to
    for stale in bdir.glob("*.html"):
        if stale.name not in written:
            stale.unlink()
    return len(written)


def write_company_pages(data):
    """One page per company on today's screen, under site/co/.

    Stale pages are DELETED, not left behind. A page for a company that dropped
    off the screen weeks ago still carries a confident-looking analysis with an
    old date on it, and a public URL that nobody revisits is exactly where a
    wrong number survives longest."""
    sc = data.get("smallcap") or {}
    screen = sc.get("screen") or []
    if not screen:
        return 0
    pf = data.get("portfolio") or {}
    date_line = datetime.fromisoformat(
        data["generated_at"]).astimezone().strftime("%A, %B %-d, %Y · %-I:%M %p %Z")
    codir = SITE / "co"
    codir.mkdir(exist_ok=True)
    written = set()
    for rank, row in enumerate(screen, 1):
        t = safe_ticker(row.get("ticker"))
        if not t:
            continue
        try:
            html = render_company_page(row, rank, screen, pf, sc, date_line)
        except Exception as e:  # noqa: BLE001 — one bad row must not lose the rest
            print(f"  warn: {t} page skipped: {_scrub(e)}", file=sys.stderr)
            continue
        (codir / f"{t}.html").write_text(html, encoding="utf-8")
        written.add(f"{t}.html")
    for stale in codir.glob("*.html"):
        if stale.name not in written:
            stale.unlink()
    return len(written)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render-only", action="store_true",
                    help="re-render pages from data/latest.json without fetching")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)

    if args.render_only:
        data = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    else:
        data = build_data()
        (DATA / "latest.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    # One page per section. Rendering is fenced so one malformed vendor field
    # degrades the site instead of aborting the job and freezing it at its last
    # state. Pages are written only if ALL of them rendered: a half-written set
    # would leave tabs pointing at yesterday's pages beside today's.
    n_pages = 0
    try:
        pages, _sections, _dl = render_site(data)
    except Exception as e:  # noqa: BLE001
        print(f"  warn: page render failed, previous pages kept: {_scrub(e)}",
              file=sys.stderr)
    else:
        for name, html in pages.items():
            (SITE / name).write_text(html, encoding="utf-8")
        n_pages = len(pages)
        # a section that stops producing content must not leave its page behind
        # for the tabs to no longer link to but the public to still reach
        for stale in SITE.glob("*.html"):
            if stale.name not in pages:
                stale.unlink()

    try:
        n_bk = write_book_pages(data)
    except Exception as e:  # noqa: BLE001 — a book page must not break the site
        n_bk = 0
        print(f"  warn: book pages skipped: {_scrub(e)}", file=sys.stderr)

    try:
        n_co = write_company_pages(data)
    except Exception as e:  # noqa: BLE001 — never let a detail page break the site
        n_co = 0
        print(f"  warn: company pages skipped: {_scrub(e)}", file=sys.stderr)

    s = data.get("stats", {})
    s["company_pages"] = n_co
    s["book_pages"] = n_bk
    s["pages"] = n_pages
    # A render failure leaves the site frozen at its last good version while
    # everything else succeeds — the job's own summary line said "ok" through
    # exactly that. It says so now, in the line anyone actually reads.
    if not n_pages:
        print("FAILED: no pages written — the published site is now STALE and "
              "will keep serving its previous version until this is fixed",
              file=sys.stderr)
    cov = (data.get("smallcap") or {}).get("coverage") or {}
    print(f"ok: {s.get('items', '?')} items, {len(data['market'])}/{len(INSTRUMENTS)} "
          f"instruments; smallcap: "
          f"{cov.get('profiled', 0):,}/{cov.get('universe', 0):,} profiled, "
          f"{cov.get('in_band', 0):,} in band, {cov.get('scored', 0):,} scored "
          f"({s.get('smallcap_calls', 0)} calls, "
          f"{(data.get('smallcap') or {}).get('budget_mode', 'n/a')} mode)")
    for name, err in s.get("feed_errors", []) + s.get("market_errors", []):
        print(f"  warn: {name}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
