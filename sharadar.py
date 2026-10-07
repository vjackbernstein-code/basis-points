#!/usr/bin/env python3
"""Sharadar (Nasdaq Data Link) access — for BACKTESTING, nothing else.

WHY THIS EXISTS
    The live screen is judged on forward evidence: it publishes 25 names and
    the record measures what they then did. That is the honest way round, and
    it is slow — the first real verdict is months away. A backtest cannot
    replace it and is not meant to. What a backtest can do is say whether the
    scoring rules have ever worked at all, before another year is spent
    finding out forward.

    Doing that needs two things this project has never had:

    SURVIVORSHIP-FREE PRICES. A price history containing only companies that
    still exist today is the most flattering dataset in finance. Every firm
    that went to zero has been quietly removed, so any strategy tested on it
    appears to avoid disasters it in fact walked into. `SHARADAR/SEP` carries
    delisted companies, and `SHARADAR/TICKERS` carries every ticker that has
    ever existed with the date and reason it stopped — which is the whole
    reason this source was chosen and paid for.

    POINT-IN-TIME FUNDAMENTALS. A revenue figure for Q1 is not knowable in
    Q1; it is knowable when it is filed, weeks later. Scoring a historical
    date with figures filed after it is lookahead, and it is the single
    easiest way to produce a backtest that looks wonderful and means nothing.
    `SHARADAR/SF1` carries `datekey`, the date a figure was actually filed.
    Only rows whose `datekey` is on or before the date being scored may be
    used. There is no shortcut around this and no version of it that is
    "close enough".

WHAT THIS MODULE DOES AND DOES NOT DO
    It fetches and caches. It computes nothing, scores nothing, and is never
    imported by the live pipeline — a test asserts that, so a slow download
    or a vendor outage can never touch the published site.

LICENCE, AND WHY THE CACHE IS GIT-IGNORED
    Sharadar data is licensed to one subscriber and must not be redistributed.
    This repository is PUBLIC. The cache therefore lives under `data/`, which
    is an allowlist — only four named state files are committable and
    everything else there is ignored by default. Do not add an exception for
    it, do not move it out of `data/`, and do not commit a derived file that
    reproduces the underlying rows. A test checks the ignore still holds.

THE KEY
    Read from the environment (`NASDAQ_DATA_LINK_API_KEY`) or a git-ignored
    local file (`data/sharadar.key`), exactly like the other two keys. It
    travels in the query string, so every error message out of this module
    goes through `scrub()` first. One of this project's keys has already been
    committed to a public repository once.

USAGE
    python3 sharadar.py --probe              # does the key work, and what shape?
    python3 sharadar.py --tickers            # the survivorship spine (small)
    python3 sharadar.py --bulk SEP           # whole price history (large)
    python3 sharadar.py --status             # what is cached, how old

ON THE API SHAPE
    The request and response shapes below are the documented ones for Nasdaq
    Data Link's datatables endpoint. They have NOT been verified against the
    live service from this machine, because that needs a working key, so
    `--probe` exists to settle it in one cheap call and every assumption is
    asserted with a message naming what it expected. If the service differs,
    the failure says so in a sentence rather than producing a wrong cache
    quietly. Assume nothing here is confirmed until `--probe` has passed.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "data" / "sharadar"

API = "https://data.nasdaq.com/api/v3/datatables"
KEY_ENV = "NASDAQ_DATA_LINK_API_KEY"
KEY_FILE = "sharadar.key"
UA = "BasisPointsBacktest/1.0"

ROWS_PER_PAGE = 10_000      # the service's documented ceiling per request
CALL_INTERVAL = 0.4         # polite spacing; the published limit is far higher
MAX_PAGES = 2_000           # a stop, so a paging bug cannot loop for a day
EXPORT_POLL_S = 15          # the bulk file is generated asynchronously
EXPORT_TRIES = 40           # ~10 minutes; a whole-table export is not quick

# The three tables this project needs, and why each one.
TABLES = {
    "TICKERS": ("every ticker that has ever existed, with the date and reason "
                "it stopped — the survivorship spine"),
    "SEP": ("daily prices including delisted companies, split- and "
            "dividend-adjusted"),
    "SF1": ("fundamentals stamped with `datekey`, the date each figure was "
            "actually filed — the only defence against lookahead"),
}


# ------------------------------------------------------------------ key ------

def read_key():
    """The key, from the environment or a git-ignored file. Never printed."""
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    path = BASE / "data" / KEY_FILE
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def scrub(msg):
    """Remove the key from anything on its way to a terminal or a file.

    The key is a query-string parameter, so a urllib error that quotes the URL
    quotes the key with it. Everything this module prints goes through here."""
    out = str(msg)
    key = read_key()
    if key and len(key) >= 8:
        out = out.replace(key, "***")
    # belt and braces: catch any api_key=... that arrived another way
    import re
    return re.sub(r"(api_key=)[^&\s\"']+", r"\1***", out)[:400]


def _assert(cond, what):
    """A failed assumption about the service, reported as a sentence."""
    if not cond:
        raise RuntimeError(
            f"the service did not behave as this module assumes: {what}. "
            f"Nothing has been cached. Run --probe and compare against the "
            f"vendor's current documentation before trusting any download.")


# --------------------------------------------------------------- client ------

class Client:
    """A deliberately small, polite datatables client."""

    def __init__(self, key, interval=CALL_INTERVAL):
        _assert(bool(key), "no API key is available, so nothing can be fetched")
        self._key = key
        self._interval = interval
        self._last = 0.0
        self.calls = 0

    def _get(self, url):
        wait = self._interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        ctx = ssl.create_default_context()
        for attempt in (0, 1, 2):
            try:
                with urllib.request.urlopen(req, timeout=120,
                                            context=ctx) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                # 429 is a rate limit and worth waiting out. It is ALSO what a
                # key being used by two things at once returns, with
                # "temporarily disabled" in the body — which happened the
                # first time this was set up, so the body is quoted.
                if e.code == 429 and attempt < 2:
                    time.sleep(20 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"HTTP {e.code} from the data service. {scrub(body)}"
                ) from None
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"could not reach the data service: {scrub(e)}") from None

    def page(self, table, params=None, cursor=None):
        """One page of a table. Returns (rows, column_names, next_cursor)."""
        q = dict(params or {})
        q["api_key"] = self._key
        if cursor:
            q["qopts.cursor_id"] = cursor
        url = f"{API}/SHARADAR/{table}.json?{urllib.parse.urlencode(q)}"
        body = self._get(url)

        _assert(isinstance(body, dict) and "datatable" in body,
                "the response has no 'datatable' key")
        dt = body["datatable"]
        _assert(isinstance(dt.get("data"), list),
                "'datatable.data' is not a list of rows")
        cols = [c.get("name") for c in (dt.get("columns") or [])]
        _assert(all(cols), "'datatable.columns' did not give every column a name")
        nxt = ((body.get("meta") or {}).get("next_cursor_id")) or None
        return dt["data"], cols, nxt

    def rows(self, table, params=None, max_pages=MAX_PAGES):
        """Every row of a filtered query, following the cursor. Yields dicts."""
        cursor, pages, cols = None, 0, None
        while True:
            data, page_cols, cursor = self.page(table, params, cursor)
            cols = cols or page_cols
            _assert(page_cols == cols,
                    "the columns changed between pages, so rows from "
                    "different pages cannot be combined")
            for r in data:
                yield dict(zip(cols, r))
            pages += 1
            if not cursor:
                return
            _assert(pages < max_pages,
                    f"paging passed {max_pages} pages without finishing, "
                    f"which is likelier a cursor bug than a table that large")

    def export_url(self, table):
        """Ask for a whole-table zip and wait for it. Returns a download URL.

        A full price history is tens of millions of rows; fetching it 10,000
        at a time is both slow and rude. The service generates the file
        asynchronously, so this polls until it reports itself ready."""
        q = {"api_key": self._key, "qopts.export": "true"}
        url = f"{API}/SHARADAR/{table}.json?{urllib.parse.urlencode(q)}"
        for attempt in range(EXPORT_TRIES):
            body = self._get(url)
            bulk = (body or {}).get("datatable_bulk_download") or {}
            f = bulk.get("file") or {}
            status = (f.get("status") or "").lower()
            if status == "fresh" and f.get("link"):
                return f["link"]
            _assert(status in ("creating", "regenerating", "fresh", ""),
                    f"the export reported an unrecognised status {status!r}")
            if attempt == 0:
                print(f"  the service is generating the {table} file; "
                      f"this takes a few minutes", flush=True)
            time.sleep(EXPORT_POLL_S)
        raise RuntimeError(
            f"the {table} export was still not ready after "
            f"{EXPORT_TRIES * EXPORT_POLL_S // 60} minutes. Nothing was "
            f"cached; try again later.")


# ---------------------------------------------------------------- cache ------

def _meta_path():
    return CACHE / "_cache.json"


def _meta():
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record(table, path, rows):
    CACHE.mkdir(parents=True, exist_ok=True)
    m = _meta()
    m[table] = {"file": path.name, "rows": rows,
                "fetched": datetime.now(timezone.utc).isoformat(
                    timespec="seconds")}
    _meta_path().write_text(json.dumps(m, indent=1), encoding="utf-8")


def save_rows(table, rows):
    """Write rows as CSV. Returns (path, n). The cache is never committed."""
    import csv
    rows = list(rows)
    _assert(bool(rows), f"{table} returned no rows at all")
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{table}.csv"
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    _record(table, path, len(rows))
    return path, len(rows)


def download_zip(url, table):
    """Stream a bulk export to disk and unpack it. Returns (path, bytes)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    zpath = CACHE / f"{table}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    total = 0
    with urllib.request.urlopen(req, timeout=600) as resp, \
            zpath.open("wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    _assert(total > 0, f"the {table} download was empty")
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        _assert(len(names) == 1,
                f"the {table} archive held {len(names)} CSV files, not one")
        z.extract(names[0], CACHE)
        extracted = CACHE / names[0]
    final = CACHE / f"{table}.csv"
    if extracted != final:
        extracted.replace(final)
    zpath.unlink(missing_ok=True)
    _record(table, final, None)
    return final, total


# ----------------------------------------------------------------- probe -----

PROBE_COLUMNS = {
    # the columns this project actually depends on, per table. A probe that
    # only checks the key works would miss the thing that matters: whether
    # the fields the backtest is built on are present and named as expected.
    "TICKERS": ("ticker", "name", "exchange", "category",
                "firstpricedate", "lastpricedate", "isdelisted"),
    "SEP": ("ticker", "date", "close", "closeadj", "volume"),
    "SF1": ("ticker", "dimension", "datekey", "calendardate", "revenue"),
}


def probe(client):
    """Check the key works and the fields the backtest needs are really there.

    Deliberately cheap: one small request per table. Returns a report rather
    than raising, so a partial entitlement (prices but not fundamentals, say)
    is visible as exactly that instead of as a flat failure."""
    out = {}
    for table, needed in PROBE_COLUMNS.items():
        try:
            params = {"qopts.per_page": 1}
            if table == "SEP":
                params["ticker"] = "AAPL"
            elif table == "SF1":
                params.update({"ticker": "AAPL", "dimension": "ARQ"})
            rows, cols, _ = client.page(table, params)
            missing = [c for c in needed if c not in cols]
            out[table] = {"ok": not missing, "columns": len(cols),
                          "rows_seen": len(rows), "missing": missing}
        except Exception as e:  # noqa: BLE001 — a report, not a crash
            out[table] = {"ok": False, "error": scrub(e)}
    return out


def survivorship_check(path=None):
    """Does the ticker spine actually contain dead companies?

    The entire reason for paying for this source is that it has not quietly
    dropped the failures. That is a claim, and claims get checked: if this
    reports no delisted names, the download is not what it is supposed to be
    and no backtest built on it means anything."""
    import csv
    path = path or (CACHE / "TICKERS.csv")
    if not path.exists():
        return None
    total = dead = 0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            total += 1
            if (row.get("isdelisted") or "").strip().upper() in ("Y", "TRUE", "1"):
                dead += 1
    return {"tickers": total, "delisted": dead,
            "share_delisted": round(dead / total, 4) if total else None}


# ------------------------------------------------------------------ cli ------

def _need_client():
    key = read_key()
    if not key:
        print(f"No Sharadar key found.\n"
              f"  Put it in the environment as {KEY_ENV}, or in a file at "
              f"data/{KEY_FILE} (git-ignored).\n"
              f"  Do not paste it into a chat or a commit.", file=sys.stderr)
        sys.exit(2)
    return Client(key)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--probe", action="store_true",
                    help="check the key and the fields the backtest needs")
    ap.add_argument("--tickers", action="store_true",
                    help="download the ticker spine (small)")
    ap.add_argument("--bulk", metavar="TABLE",
                    help=f"whole-table export; one of {', '.join(TABLES)}")
    ap.add_argument("--status", action="store_true",
                    help="what is cached and how old it is")
    args = ap.parse_args(argv)

    if args.status:
        m = _meta()
        if not m:
            print("nothing cached yet")
        for table, info in sorted(m.items()):
            rows = f"{info['rows']:,} rows" if info.get("rows") else "bulk file"
            size = ""
            f = CACHE / info.get("file", "")
            if f.exists():
                size = f" · {f.stat().st_size / 1e6:.1f} MB"
            print(f"  {table:<8} {rows}{size} · fetched {info['fetched']}")
        chk = survivorship_check()
        if chk:
            print(f"  survivorship: {chk['delisted']:,} of {chk['tickers']:,} "
                  f"tickers are delisted "
                  f"({100 * (chk['share_delisted'] or 0):.1f}%)")
        return 0

    if args.probe:
        rep = probe(_need_client())
        for table, r in rep.items():
            if r.get("ok"):
                print(f"  {table:<8} OK · {r['columns']} columns")
            elif r.get("missing"):
                print(f"  {table:<8} reachable but MISSING the fields this "
                      f"project needs: {', '.join(r['missing'])}")
            else:
                print(f"  {table:<8} FAILED · {r.get('error')}")
        print()
        ok = [t for t, r in rep.items() if r.get("ok")]
        print(f"usable tables: {', '.join(ok) if ok else 'none'}")
        for table in TABLES:
            if table not in ok:
                print(f"  without {table}: {TABLES[table]}")
        return 0 if len(ok) == len(PROBE_COLUMNS) else 1

    if args.tickers:
        c = _need_client()
        path, n = save_rows("TICKERS", c.rows(
            "TICKERS", {"table": "SEP", "qopts.per_page": ROWS_PER_PAGE}))
        print(f"  {n:,} tickers -> {path.relative_to(BASE)} "
              f"({c.calls} requests)")
        chk = survivorship_check(path)
        if chk:
            print(f"  of these, {chk['delisted']:,} are delisted "
                  f"({100 * (chk['share_delisted'] or 0):.1f}%)")
            if not chk["delisted"]:
                print("  WARNING: no delisted companies. This is supposed to "
                      "be a survivorship-free source; do not backtest on "
                      "this until that is explained.", file=sys.stderr)
                return 1
        return 0

    if args.bulk:
        table = args.bulk.upper()
        if table not in TABLES:
            print(f"unknown table {table!r}; one of {', '.join(TABLES)}",
                  file=sys.stderr)
            return 2
        c = _need_client()
        url = c.export_url(table)
        path, nbytes = download_zip(url, table)
        print(f"  {table} -> {path.relative_to(BASE)} "
              f"({nbytes / 1e6:.0f} MB)")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
