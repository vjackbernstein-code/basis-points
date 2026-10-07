#!/usr/bin/env python3
"""Tests for the Sharadar (Nasdaq Data Link) backtest data access.

Entirely offline: the HTTP layer is replaced with canned responses. Nothing
here needs a key, a subscription or a network, which is the point — the
module has to be trustworthy before it is ever pointed at a paid service, and
most of what can go wrong here is not about the data at all. It is about a
licensed dataset in a public repository, and a key in a query string.

Run:  python3 -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sharadar  # noqa: E402


def canned(pages):
    """A fake transport. `pages` is a list of response bodies to return."""
    seq = list(pages)
    seen = []

    def _get(url):
        seen.append(url)
        return seq.pop(0) if seq else {}

    return _get, seen


def body(rows, cols, next_cursor=None):
    return {"datatable": {"data": rows,
                          "columns": [{"name": c} for c in cols]},
            "meta": {"next_cursor_id": next_cursor}}


class ClientFor(sharadar.Client):
    """A client whose transport is canned and whose spacing is instant."""

    def __init__(self, pages):
        super().__init__("TESTKEY-0123456789", interval=0.0)
        self._get, self.urls = canned(pages)


# -------------------------------------------------- the dangerous parts ------

class LicenceAndSecrecyTests(unittest.TestCase):
    """The two ways this module could do real harm.

    This dataset is licensed to one subscriber and must not be redistributed;
    this repository is public. And the key travels in the query string, so any
    error that quotes a URL quotes the key — which is not hypothetical here,
    because two of this project's keys have already been committed publicly
    once and had to be rotated.
    """

    def test_the_cache_cannot_be_committed(self):
        import subprocess
        # asks git itself rather than re-reading the ignore rules, because the
        # question is what git would actually do
        r = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore",
             "data/sharadar/SEP.csv", "data/sharadar/_cache.json",
             "data/sharadar.key"],
            capture_output=True, text=True)
        ignored = set(r.stdout.split())
        for p in ("data/sharadar/SEP.csv", "data/sharadar/_cache.json",
                  "data/sharadar.key"):
            self.assertIn(p, ignored,
                          f"{p} is NOT ignored — licensed data or a key could "
                          f"be committed to a public repository")

    def test_the_cache_lives_under_the_allowlisted_directory(self):
        # moving it out of data/ would put it somewhere the allowlist does not
        # cover, and the protection above would silently stop applying
        self.assertEqual(sharadar.CACHE.parent.name, "data")

    def test_the_key_is_scrubbed_out_of_messages(self):
        real = sharadar.read_key
        try:
            sharadar.read_key = lambda: "SECRETKEY123456"
            msg = sharadar.scrub(
                "HTTP 403 for https://x/y.json?api_key=SECRETKEY123456&t=1")
            self.assertNotIn("SECRETKEY123456", msg)
            self.assertIn("***", msg)
        finally:
            sharadar.read_key = real

    def test_a_key_shaped_parameter_is_scrubbed_even_if_unrecognised(self):
        # the key may arrive from the environment of another process, or the
        # URL may hold a different one; the pattern is stripped regardless
        real = sharadar.read_key
        try:
            sharadar.read_key = lambda: None
            msg = sharadar.scrub("failed: ...?api_key=someoneelseskey&x=2")
            self.assertNotIn("someoneelseskey", msg)
        finally:
            sharadar.read_key = real

    def test_the_live_pipeline_does_not_import_this(self):
        # a slow download or a vendor outage must not be able to reach the
        # published site. Backtesting is a separate activity on purpose.
        for name in ("pipeline.py", "smallcap.py", "portfolio.py",
                     "decision.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("import sharadar", src,
                             f"{name} imports the backtest data module")


class KeySourceTests(unittest.TestCase):
    """Where the key comes from, and refusing the placeholder.

    These never touch the project's real .env: they point the loader at a
    temporary file. A test that read the live file would both depend on
    whether a key happens to be pasted and risk putting it in output.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._real_env_file = sharadar.ENV_FILE
        sharadar.ENV_FILE = self.dir / ".env"

    def tearDown(self):
        sharadar.ENV_FILE = self._real_env_file

    def _write(self, text):
        sharadar.ENV_FILE.write_text(text, encoding="utf-8")

    def test_a_plain_assignment_is_read(self):
        self._write("NASDAQ_DATA_LINK_API_KEY=abc123\n")
        self.assertEqual(
            sharadar._from_env_file("NASDAQ_DATA_LINK_API_KEY"), "abc123")

    def test_comments_and_blank_lines_are_ignored(self):
        self._write("# a comment\n\n  \nNASDAQ_DATA_LINK_API_KEY=abc123\n")
        self.assertEqual(
            sharadar._from_env_file("NASDAQ_DATA_LINK_API_KEY"), "abc123")

    def test_quotes_and_surrounding_space_are_stripped(self):
        for raw in ('"abc123"', "'abc123'", "  abc123  "):
            self._write(f"NASDAQ_DATA_LINK_API_KEY={raw}\n")
            self.assertEqual(
                sharadar._from_env_file("NASDAQ_DATA_LINK_API_KEY"), "abc123",
                f"{raw!r} was not read cleanly")

    def test_another_key_in_the_same_file_is_not_confused_for_it(self):
        self._write("FINNHUB_API_KEY=wrongone\n"
                    "NASDAQ_DATA_LINK_API_KEY=rightone\n")
        self.assertEqual(
            sharadar._from_env_file("NASDAQ_DATA_LINK_API_KEY"), "rightone")

    def test_an_absent_file_is_not_an_error(self):
        self.assertIsNone(
            sharadar._from_env_file("NASDAQ_DATA_LINK_API_KEY",
                                    self.dir / "nope.env"))

    def test_the_loader_does_not_alter_the_process_environment(self):
        # reading a file for one key must not quietly reconfigure everything
        # else running in this process
        import os
        self._write("SOME_OTHER_THING=xyz\n")
        sharadar._from_env_file("SOME_OTHER_THING")
        self.assertNotIn("SOME_OTHER_THING", os.environ)

    def test_placeholder_text_counts_as_no_key(self):
        # the shipped .env holds a placeholder; sending it to the service
        # returns an authentication error, which reads like a broken
        # subscription rather than like "you have not pasted it yet"
        for ph in ("PASTE_YOUR_KEY_HERE", "your_key_here", "<your key here>",
                   "changeme", "", "   ", "TODO"):
            self.assertTrue(sharadar._is_placeholder(ph), repr(ph))

    def test_a_real_looking_key_is_not_mistaken_for_a_placeholder(self):
        for real in ("xY3k9QpLm2", "abc123def456", "A1b2C3d4E5f6G7h8"):
            self.assertFalse(sharadar._is_placeholder(real), repr(real))

    def test_a_still_unfilled_slot_is_distinguished_from_having_none(self):
        self._write("NASDAQ_DATA_LINK_API_KEY=PASTE_YOUR_KEY_HERE\n")
        real_file = sharadar._read_key_file
        real_env = sharadar.os.environ.get
        try:
            sharadar._read_key_file = lambda: None
            sharadar.os.environ = dict(sharadar.os.environ)
            sharadar.os.environ.pop("NASDAQ_DATA_LINK_API_KEY", None)
            self.assertIsNone(sharadar.read_key())
            self.assertTrue(sharadar.key_waiting_to_be_filled_in())
        finally:
            sharadar._read_key_file = real_file
            import os as _os
            sharadar.os = _os

    def test_the_env_file_sits_where_git_ignores_it(self):
        import subprocess
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", ".env"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         ".env is NOT ignored — a key could be committed to a "
                         "public repository")


# ------------------------------------------------------------- the client ----

class PagingTests(unittest.TestCase):

    def test_rows_follow_the_cursor_to_the_end(self):
        c = ClientFor([
            body([["A", 1], ["B", 2]], ["ticker", "n"], next_cursor="c2"),
            body([["C", 3]], ["ticker", "n"], next_cursor=None),
        ])
        rows = list(c.rows("SEP"))
        self.assertEqual([r["ticker"] for r in rows], ["A", "B", "C"])

    def test_the_cursor_is_sent_back_on_the_next_request(self):
        c = ClientFor([
            body([["A", 1]], ["ticker", "n"], next_cursor="abc123"),
            body([["B", 2]], ["ticker", "n"]),
        ])
        list(c.rows("SEP"))
        self.assertIn("qopts.cursor_id=abc123", c.urls[1])

    def test_rows_are_dicts_keyed_by_column_name(self):
        c = ClientFor([body([["AAPL", "2020-01-02", 75.0]],
                            ["ticker", "date", "closeadj"])])
        row = next(iter(c.rows("SEP")))
        self.assertEqual(row["ticker"], "AAPL")
        self.assertEqual(row["closeadj"], 75.0)

    def test_columns_changing_between_pages_is_refused(self):
        # silently zipping rows against the wrong headers would mislabel every
        # field from the second page on
        c = ClientFor([
            body([["A", 1]], ["ticker", "n"], next_cursor="c2"),
            body([["B", 2]], ["ticker", "other"]),
        ])
        with self.assertRaises(RuntimeError) as caught:
            list(c.rows("SEP"))
        self.assertIn("columns changed", str(caught.exception))

    def test_endless_paging_stops_instead_of_looping(self):
        # a cursor that never clears would otherwise download for a day
        c = ClientFor([body([["A", 1]], ["ticker", "n"], next_cursor="same")]
                      * 40)
        with self.assertRaises(RuntimeError) as caught:
            list(c.rows("SEP", max_pages=5))
        self.assertIn("paging passed", str(caught.exception))

    def test_the_table_name_and_key_are_in_the_url(self):
        c = ClientFor([body([["A"]], ["ticker"])])
        list(c.rows("TICKERS"))
        self.assertIn("SHARADAR/TICKERS.json", c.urls[0])
        self.assertIn("api_key=", c.urls[0])


class AssumptionTests(unittest.TestCase):
    """Every assumption about the service is asserted, not hoped for.

    None of the request or response shapes in this module have been confirmed
    against the live service, because that needs a paid key. So the failure
    mode has to be a sentence saying what was expected, rather than a cache
    full of wrongly-labelled numbers.
    """

    def test_a_response_without_a_datatable_is_refused(self):
        c = ClientFor([{"error": "nope"}])
        with self.assertRaises(RuntimeError) as caught:
            c.page("SEP")
        self.assertIn("no 'datatable'", str(caught.exception))

    def test_rows_that_are_not_a_list_are_refused(self):
        c = ClientFor([{"datatable": {"data": "oops", "columns": []}}])
        with self.assertRaises(RuntimeError):
            c.page("SEP")

    def test_unnamed_columns_are_refused(self):
        c = ClientFor([{"datatable": {"data": [[1]],
                                      "columns": [{"type": "Integer"}]}}])
        with self.assertRaises(RuntimeError) as caught:
            c.page("SEP")
        self.assertIn("every column a name", str(caught.exception))

    def test_a_failure_says_nothing_was_cached(self):
        # the worst outcome is a half-written cache believed to be complete
        c = ClientFor([{"error": "nope"}])
        with self.assertRaises(RuntimeError) as caught:
            c.page("SEP")
        self.assertIn("Nothing has been cached", str(caught.exception))

    def test_a_client_without_a_key_refuses_to_exist(self):
        with self.assertRaises(RuntimeError):
            sharadar.Client(None)


# ------------------------------------------------------ survivorship ---------

class SurvivorshipTests(unittest.TestCase):
    """The one claim worth paying for, checked rather than assumed.

    A price history containing only companies that still exist is the most
    flattering dataset in finance, because every firm that went to zero has
    been removed. If the ticker spine arrives with no delisted names then
    whatever was downloaded is not what was paid for, and no backtest built on
    it would mean anything — so this is checked at download time, loudly.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _spine(self, rows):
        p = self.dir / "TICKERS.csv"
        with p.open("w", encoding="utf-8") as fh:
            fh.write("ticker,isdelisted\n")
            for t, d in rows:
                fh.write(f"{t},{d}\n")
        return p

    def test_delisted_companies_are_counted(self):
        p = self._spine([("A", "N"), ("B", "Y"), ("C", "Y"), ("D", "N")])
        chk = sharadar.survivorship_check(p)
        self.assertEqual(chk["tickers"], 4)
        self.assertEqual(chk["delisted"], 2)
        self.assertEqual(chk["share_delisted"], 0.5)

    def test_a_spine_with_no_dead_companies_is_visible_as_such(self):
        p = self._spine([("A", "N"), ("B", "N")])
        self.assertEqual(sharadar.survivorship_check(p)["delisted"], 0)

    def test_the_flag_is_read_tolerantly(self):
        # the vendor may write Y, TRUE or 1; reading only one of those would
        # report a survivorship-free dataset as survivorship-biased
        for mark in ("Y", "y", "TRUE", "true", "1"):
            p = self._spine([("A", "N"), ("B", mark)])
            self.assertEqual(sharadar.survivorship_check(p)["delisted"], 1,
                             f"{mark!r} was not read as delisted")

    def test_a_missing_file_is_not_an_error(self):
        self.assertIsNone(
            sharadar.survivorship_check(self.dir / "absent.csv"))


# ------------------------------------------------------------- the probe -----

class ProbeTests(unittest.TestCase):
    """The probe exists to settle the unverified assumptions in one call."""

    def test_it_names_the_fields_the_backtest_depends_on(self):
        # a probe that only proved the key works would miss the thing that
        # matters: whether `datekey` and the delisting fields are really there
        self.assertIn("datekey", sharadar.PROBE_COLUMNS["SF1"])
        self.assertIn("isdelisted", sharadar.PROBE_COLUMNS["TICKERS"])
        self.assertIn("closeadj", sharadar.PROBE_COLUMNS["SEP"])

    def test_a_missing_field_is_reported_rather_than_passed(self):
        c = ClientFor([body([["AAPL"]], ["ticker"])] * 3)
        rep = sharadar.probe(c)
        self.assertFalse(rep["SF1"]["ok"])
        self.assertIn("datekey", rep["SF1"]["missing"])

    def test_all_expected_fields_present_reads_as_ok(self):
        pages = [body([[1] * len(cols)], list(cols))
                 for cols in sharadar.PROBE_COLUMNS.values()]
        rep = sharadar.probe(ClientFor(pages))
        self.assertTrue(all(r["ok"] for r in rep.values()), rep)

    def test_one_unreachable_table_does_not_hide_the_others(self):
        # a subscription may cover prices but not fundamentals, and that is
        # worth knowing precisely rather than as a flat failure
        ok = [body([[1] * len(c)], list(c))
              for c in (sharadar.PROBE_COLUMNS["TICKERS"],)]
        c = ClientFor(ok + [{"error": "not entitled"}, {"error": "x"}])
        rep = sharadar.probe(c)
        self.assertTrue(rep["TICKERS"]["ok"])
        self.assertFalse(rep["SEP"]["ok"])
        self.assertIn("error", rep["SEP"])


class CliTests(unittest.TestCase):

    def test_status_works_with_nothing_cached(self):
        import contextlib, io
        out = io.StringIO()
        real = sharadar.CACHE
        try:
            sharadar.CACHE = Path(tempfile.mkdtemp()) / "sharadar"
            with contextlib.redirect_stdout(out):
                rc = sharadar.main(["--status"])
        finally:
            sharadar.CACHE = real
        self.assertEqual(rc, 0)
        self.assertIn("nothing cached", out.getvalue())

    def test_an_unknown_table_is_refused_before_any_request(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = sharadar.main(["--bulk", "NOSUCHTABLE"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown table", err.getvalue())

    def test_every_offered_table_is_explained(self):
        # the help text is where someone decides what to download; a bare
        # table name says nothing about what it is for
        for table, why in sharadar.TABLES.items():
            self.assertGreater(len(why), 20, table)


if __name__ == "__main__":
    unittest.main()
