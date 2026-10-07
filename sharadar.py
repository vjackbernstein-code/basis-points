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

ENV_FILE = BASE / ".env"

# Text that means "nobody has filled this in yet". The .env ships with a
# placeholder, and sending one to the service would come back as an
# authentication error — which reads like a broken subscription rather than
# like the actual problem, so it is caught here instead.
PLACEHOLDERS = {
    "your_key_here", "paste_your_key_here", "paste-your-key-here",
    "changeme", "change_me", "xxx", "xxxxx", "todo", "none", "null",
}


def _is_placeholder(value):
    # separators are normalised because the same placeholder gets written
    # "YOUR_KEY_HERE", "your key here" and "<your-key-here>" depending on who
    # typed it, and all three mean the same thing
    v = (value or "").strip().strip("<>").strip("\"'").strip().lower()
    v = v.replace(" ", "_").replace("-", "_")
    return (not v) or v in PLACEHOLDERS


def _from_env_file(name, path=None):
    """Read NAME=value out of the git-ignored .env at the project root.

    A deliberately small parser: `KEY=value`, `#` comments, optional quotes,
    nothing else. It does NOT put anything into os.environ — a file read for
    one key should not quietly change the environment of everything else
    running in this process."""
    try:
        text = (path or ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != name:
            continue
        return v.strip().strip('"').strip("'") or None
    return None


def read_key():
    """The key, from the environment, a .env, or a key file. Never printed.

    Checked in that order so a value exported in a shell wins over a file, and
    placeholder text counts as no key at all."""
    for candidate in (os.environ.get(KEY_ENV, ""),
                      _from_env_file(KEY_ENV),
                      _read_key_file()):
        if candidate and not _is_placeholder(candidate):
            return candidate.strip()
    return None


def _read_key_file():
    path = BASE / "data" / KEY_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def key_waiting_to_be_filled_in():
    """True when a key slot exists but still holds placeholder text.

    Worth distinguishing from "no key anywhere": one means you have not
    subscribed, the other means you have not pasted."""
    for candidate in (os.environ.get(KEY_ENV, ""),
                      _from_env_file(KEY_ENV), _read_key_file()):
        if candidate is not None and candidate != "" and _is_placeholder(candidate):
            return True
    return False


def scrub(msg, extra=None):
    """Remove the key from anything on its way to a terminal or a file.

    The key is a query-string parameter, so a urllib error that quotes the URL
    quotes the key with it. Everything this module prints goes through here.

    `extra` exists because reading the key back out of the environment only
    covers the key that happens to be configured NOW. A client given a key
    directly — a test, a one-off, a rotation in progress — would otherwise
    have that one printed in full. A test catches exactly that."""
    out = str(msg)
    for secret in (read_key(), extra):
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
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

# The service says "exceeded the API speed limit and your account has
# temporarily been disabled" under code QELx06. That is an account state, not
# a property of this request, and it is the one 429 that must not be retried.
ACCOUNT_DISABLED_CODE = "QELx06"


def _account_disabled(body):
    text = (body or "").lower()
    return (ACCOUNT_DISABLED_CODE.lower() in text
            or "account has temporarily been disabled" in text
            or "account has been disabled" in text)


class Client:
    """A deliberately small, polite datatables client."""

    def __init__(self, key, interval=CALL_INTERVAL):
        _assert(bool(key), "no API key is available, so nothing can be fetched")
        self._key = key
        self._interval = interval
        self._last = 0.0
        self.calls = 0

    def _scrub(self, msg):
        """Scrub with this client's own key, not merely the configured one."""
        return scrub(msg, self._key)

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
                finally:
                    # an HTTPError is itself an open response; reading the
                    # body without closing it leaks the connection
                    e.close()
                # 429 covers two different situations that must not be
                # treated the same. A plain speed limit is transient and
                # worth waiting out. But the service also returns 429 when
                # the ACCOUNT itself has been disabled for past overuse, and
                # retrying that is the single worst response available: the
                # requests still count, so a retry loop is what keeps an
                # account disabled. Observed here on 2026-10-07 — the first
                # request of the session came back in 0.5s, so the block
                # predated it entirely.
                if e.code == 429 and _account_disabled(body):
                    raise RuntimeError(
                        "the data service has disabled this ACCOUNT, not "
                        "merely rate-limited this request, so retrying will "
                        "not help and may prolong it. A new API key does not "
                        "clear an account-level block. "
                        f"The service said: {self._scrub(body)}") from None
                if e.code == 429 and attempt < 2:
                    time.sleep(20 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"HTTP {e.code} from the data service. {self._scrub(body)}"
                ) from None
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"could not reach the data service: {self._scrub(e)}") from None

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
            # the client's own scrubber, so a key passed in directly is
            # stripped and not only the one configured in the environment
            out[table] = {"ok": False, "error": client._scrub(e)}
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


# ------------------------------------------------------- local database -----
#
# The bulk tables, already downloaded and loaded into SQLite by another of the
# owner's projects, are a complete substitute for the API: 45M daily price
# rows from 1997, 3.2M fundamental rows with filing dates from 1990, and daily
# market caps, which is what this screen's eligibility band needs. There is
# nothing the REST endpoint could add that matters for a backtest, and the
# account is blocked anyway.
#
# It is opened READ-ONLY and never copied. One licensed 16GB dataset in one
# place is the whole of the licence story; a second copy inside a PUBLIC
# repository is how that story ends badly.

DB_ENV = "SHARADAR_DB"
DEFAULT_DB = CACHE / "sharadar.db"

# The column Sharadar calls `datekey` — the date a figure was actually filed,
# and so the first date it could honestly be used. The loader that built this
# database named it `date`, which is the sort of rename that quietly turns a
# point-in-time dataset into a lookahead one, so the name is resolved rather
# than assumed and `verify_store` proves whichever it finds really is a filing
# date before anything trusts it.
FILED_COLUMNS = ("datekey", "date")


def db_path():
    """Where the local database is, from .env, the environment, or default."""
    for candidate in (os.environ.get(DB_ENV, ""), _from_env_file(DB_ENV)):
        if candidate and candidate.strip():
            return Path(candidate.strip()).expanduser()
    return DEFAULT_DB


def open_store(path=None):
    """A read-only connection. Read-only is not a convention here: this file
    belongs to another project, which may be using it."""
    import sqlite3
    path = Path(path) if path else db_path()
    if not path.exists():
        raise RuntimeError(
            f"no local Sharadar database at {path}. Point {DB_ENV} at one in "
            f".env, or download the tables with --bulk.")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def filed_column(con, table="fundamentals"):
    """Whichever column holds the filing date, by name."""
    cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    for name in FILED_COLUMNS:
        if name in cols:
            return name
    raise RuntimeError(
        f"{table} has no filing-date column (looked for "
        f"{', '.join(FILED_COLUMNS)}). Without one, every figure would be "
        f"used from the period it describes rather than the date it became "
        f"public, which is lookahead and makes a backtest worthless.")


def verify_store(con, sample_from="2015-01-01"):
    """Prove the two claims a backtest stands on, before it is built.

    Neither is checked once and assumed forever, because both can be broken by
    a reload, a schema change or a well-meaning rename, and neither failure is
    visible in a result — a backtest on survivorship-biased or lookahead data
    does not crash, it just looks good."""
    out = {}
    filed = filed_column(con)
    out["filed_column"] = filed

    # 1. is that column really a FILING date, or the period it describes?
    row = con.execute(f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN {filed} = reportperiod THEN 1 ELSE 0 END) AS same,
               AVG(julianday({filed}) - julianday(reportperiod)) AS mean_gap
        FROM fundamentals
        WHERE dimension='ARQ' AND reportperiod >= ?
    """, (sample_from,)).fetchone()
    n, same, gap = row["n"], row["same"] or 0, row["mean_gap"]
    out["filing_rows"] = n
    out["filed_equals_period"] = same
    out["mean_filing_lag_days"] = round(gap, 1) if gap is not None else None
    # a real filing lag is weeks; a column that merely copies the period end
    # would sit at zero
    out["point_in_time"] = bool(n and gap and gap > 5 and same < 0.01 * n)

    # 2. does the spine keep companies that no longer exist?
    t = con.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN isdelisted='Y' THEN 1 ELSE 0 END)"
        " AS dead FROM tickers WHERE category LIKE '%Common Stock%'"
    ).fetchone()
    out["common_stocks"] = t["n"]
    out["delisted"] = t["dead"] or 0
    out["survivorship_free"] = bool(t["n"] and (t["dead"] or 0) > 0.2 * t["n"])

    cov = con.execute("SELECT MIN(date) AS lo, MAX(date) AS hi "
                      "FROM stocks").fetchone()
    out["prices_from"], out["prices_to"] = cov["lo"], cov["hi"]
    out["ok"] = out["point_in_time"] and out["survivorship_free"]
    return out


def fundamentals_asof(con, ticker, asof, dimension="ARQ", filed=None):
    """The most recent figures that were PUBLIC on `asof`. None if there were
    none.

    This is the function the whole backtest's honesty rests on, so what it
    does is worth stating exactly: of the rows for this company whose FILING
    date is on or before `asof`, take the one filed most recently. That is
    what a person reading filings on that day would have had in front of them.

    It handles restatements correctly as a consequence rather than as a
    special case. A quarter that was filed and later refiled appears twice; on
    a date between the two, only the original has been filed, so the original
    is what comes back — which is the version anyone acting on that date would
    have acted on, even though we now know it was wrong."""
    filed = filed or filed_column(con)
    return con.execute(
        f"SELECT * FROM fundamentals WHERE ticker=? AND dimension=? "
        f"AND {filed} <= ? ORDER BY {filed} DESC, reportperiod DESC LIMIT 1",
        (ticker, dimension, asof)).fetchone()


# ------------------------------------------------------------------ cli ------

def _need_client():
    key = read_key()
    if key:
        return Client(key)
    if key_waiting_to_be_filled_in():
        print(f"The key slot still holds placeholder text.\n"
              f"  Open .env and replace the placeholder after "
              f"{KEY_ENV}= with the real key, then run this again.\n"
              f"  Nothing was sent to the service.", file=sys.stderr)
    else:
        print(f"No Sharadar key found.\n"
              f"  Put it in .env as {KEY_ENV}=..., export it in your shell, "
              f"or write it to data/{KEY_FILE}. All three are git-ignored.\n"
              f"  Do not paste it into a chat or a commit.", file=sys.stderr)
    sys.exit(2)


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
    ap.add_argument("--verify-db", action="store_true",
                    help="prove the local database is survivorship-free and "
                         "point-in-time before trusting it")
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

    if args.verify_db:
        try:
            con = open_store()
        except RuntimeError as e:
            print(f"  {e}", file=sys.stderr)
            return 2
        r = verify_store(con)
        print(f"  database        {db_path()}")
        print(f"  prices          {r['prices_from']} -> {r['prices_to']}")
        print(f"  filing date in  '{r['filed_column']}' column")
        print(f"  filing lag      mean {r['mean_filing_lag_days']} days over "
              f"{r['filing_rows']:,} quarters "
              f"({r['filed_equals_period']} equal to the period end)")
        print(f"  common stocks   {r['common_stocks']:,}, of which "
              f"{r['delisted']:,} delisted "
              f"({100 * r['delisted'] / max(r['common_stocks'], 1):.0f}%)")
        print()
        print(f"  point-in-time     {'YES' if r['point_in_time'] else 'NO'}")
        print(f"  survivorship-free {'YES' if r['survivorship_free'] else 'NO'}")
        if not r["ok"]:
            print("\n  NOT usable for a backtest. A backtest on data failing "
                  "either test does not crash — it just looks good.",
                  file=sys.stderr)
        con.close()
        return 0 if r["ok"] else 1

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
