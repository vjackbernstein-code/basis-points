#!/usr/bin/env python3
"""Regression tests for smallcap.py — the frozen v3 rating model.

The model is deliberately frozen while a live track record accumulates, so
these tests pin behaviour *exactly as it stands today*. They are a net to catch
accidental change, not an endorsement: a few tests deliberately record quirks
(noted in comments) so that a future deliberate fix shows up as a failing test
rather than a silent drift.

Everything here is offline and deterministic: the clock is pinned via
smallcap._now, state paths are repointed into a temp dir, and the four network
fetchers are replaced with counting stubs.

Run:  python3 -m unittest discover tests
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import smallcap  # noqa: E402


FIXED_NOW = datetime(2026, 9, 15, 16, 0, 0, tzinfo=timezone.utc)
ISO_NOW = FIXED_NOW.isoformat(timespec="seconds")
TODAY = FIXED_NOW.strftime("%Y-%m-%d")

METRIC_KEYS = ("rev_g", "rev_gq", "r13", "r26", "vol", "hi52", "adv", "rps",
               "cfps", "cashps", "gm_t", "gm_a", "om_t", "om_a", "rg5", "rsg5",
               "rg3", "dte", "ev_rev")


def hours_ago(h):
    return (FIXED_NOW - timedelta(hours=h)).isoformat(timespec="seconds")


def days_ago(n):
    return (FIXED_NOW - timedelta(days=n)).strftime("%Y-%m-%d")


def make_cache(**overrides):
    """An empty cache with the same shape load_cache() guarantees."""
    cache = {"universe": {}, "universe_fetched": None, "profiles": {},
             "metrics": {}, "quotes": {}, "insider": {}, "earn_map": {},
             "earnings": [], "earnings_fetched": None, "last_screen": [],
             "bench": {}}
    cache.update(overrides)
    return cache


def add_name(cache, ticker, name=None, mcap=800.0, shares=20.0,
             exch="NASDAQ NMS - GLOBAL MARKET", ind="Technology", px=10.0,
             dp=1.0, quote_t=ISO_NOW, profile_t=ISO_NOW, metric_t=ISO_NOW,
             shist=None, **metrics):
    """A fully eligible company (rev_ttm = rps x shares = $100M), overridable."""
    m = {k: None for k in METRIC_KEYS}
    m.update({"rev_g": 30.0, "r13": 10.0, "rps": 5.0, "adv": 1.0, "vol": 30.0,
              "ev_rev": 2.0})
    m.update(metrics)
    m["t"] = metric_t
    name = name or f"{ticker} Industries Inc"
    cache["universe"][ticker] = name
    cache["profiles"][ticker] = {"mcap": mcap, "shares": shares, "exch": exch,
                                 "ind": ind, "name": name,
                                 "shist": shist or [], "t": profile_t}
    cache["metrics"][ticker] = m
    cache["quotes"][ticker] = {"px": px, "dp": dp, "t": quote_t}
    return cache


def log_entry(tickers, px0=10.0, bench_iwo=100.0, version=None):
    return {"v": smallcap.MODEL_VERSION if version is None else version,
            "pub": [[t, 80.0, px0] for t in tickers],
            "cand": list(tickers),
            "bench": {"iwo": bench_iwo,
                      "iwm": None if bench_iwo is None else bench_iwo * 0.8}}


class StubFetchers:
    """Counting stand-ins for the four network fetchers.

    They also write a plausible cache entry, so the later stages of
    _spend_budget see the same state a real run would.
    """

    def __init__(self, iso=ISO_NOW):
        self.calls = []
        self.iso = iso

    def install(self, test):
        test.patch("_fetch_profile", self.profile)
        test.patch("_fetch_metrics", self.metrics)
        test.patch("_fetch_quote", self.quote)
        test.patch("_fetch_insider", self.insider)
        return self

    def profile(self, fh, cache, ticker):
        self.calls.append(("profile", ticker))
        cache["profiles"][ticker] = {"mcap": None, "shares": None, "exch": "",
                                     "ind": "", "name": ticker, "shist": [],
                                     "t": self.iso}

    def metrics(self, fh, cache, ticker):
        self.calls.append(("metrics", ticker))
        entry = {k: None for k in METRIC_KEYS}
        entry["t"] = self.iso
        cache["metrics"][ticker] = entry

    def quote(self, fh, cache, ticker):
        self.calls.append(("quote", ticker))
        cache["quotes"][ticker] = {"px": 5.0, "dp": 0.0, "t": self.iso}

    def insider(self, fh, cache, ticker):
        self.calls.append(("insider", ticker))
        cache["insider"][ticker] = {"net30": 0, "t": self.iso}

    def of(self, kind):
        return [t for k, t in self.calls if k == kind]


class SmallcapTestCase(unittest.TestCase):
    """Pins the clock and redirects the two state files at a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.now = FIXED_NOW
        self.patch("_now", lambda: self.now)
        self.patch("CACHE_PATH", self.tmpdir / "smallcap.json")
        self.patch("LOG_PATH", self.tmpdir / "screen_log.json")

    def patch(self, attr, value):
        original = getattr(smallcap, attr)
        setattr(smallcap, attr, value)
        self.addCleanup(setattr, smallcap, attr, original)


# --------------------------------------------------------- SEC filings -------


class FilingParserTests(SmallcapTestCase):
    """_FILING_RE / _categorize_form / the filer-vs-subject party rule."""

    def test_hyphenated_form_codes_are_not_split_at_their_internal_hyphen(self):
        cases = {
            "8-K - ACME CORP (0001234567) (Filer)": ("8-K", "ACME CORP", "Filer"),
            "S-1 - FOO INC (0001234567) (Filer)": ("S-1", "FOO INC", "Filer"),
            "S-1/A - FOO INC (0001234567) (Filer)": ("S-1/A", "FOO INC", "Filer"),
            "424B3 - FOO INC (0001234567) (Filer)": ("424B3", "FOO INC", "Filer"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                m = smallcap._FILING_RE.match(title)
                self.assertIsNotNone(m, "title should parse")
                self.assertEqual(m.groups(), expected)

    def test_company_names_may_contain_hyphens_and_the_split_stays_at_the_spaced_dash(self):
        m = smallcap._FILING_RE.match(
            "8-K - SMITH-BARNEY HOLDINGS CO (0001234567) (Filer)")
        self.assertEqual(m.group(1), "8-K")
        self.assertEqual(m.group(2), "SMITH-BARNEY HOLDINGS CO")

    def test_titles_without_a_ten_digit_cik_do_not_parse(self):
        self.assertIsNone(smallcap._FILING_RE.match("8-K - ACME CORP (Filer)"))
        self.assertIsNone(smallcap._FILING_RE.match("8-K ACME CORP (0001234567) (Filer)"))

    def test_categorize_form_maps_prefixes_and_ignores_ownership_forms(self):
        self.assertEqual(smallcap._categorize_form("8-K"), "material")
        self.assertEqual(smallcap._categorize_form("8-K/A"), "material")
        self.assertEqual(smallcap._categorize_form("SCHEDULE 13D"), "activist")
        self.assertEqual(smallcap._categorize_form("SCHEDULE 13D/A"), "activist")
        self.assertEqual(smallcap._categorize_form("SCHEDULE 13G"), "activist")
        # the short EDGAR form codes are accepted too, so a feed-format change
        # cannot silently zero out the activist flag
        self.assertEqual(smallcap._categorize_form("SC 13D"), "activist")
        self.assertEqual(smallcap._categorize_form("SC 13G/A"), "activist")
        self.assertEqual(smallcap._categorize_form("S-1"), "offering")
        self.assertEqual(smallcap._categorize_form("424B4"), "offering")
        # insider Form 4 and Rule 144 notices are pure noise here
        self.assertIsNone(smallcap._categorize_form("4"))
        self.assertIsNone(smallcap._categorize_form("4/A"))
        self.assertIsNone(smallcap._categorize_form("144"))
        self.assertIsNone(smallcap._categorize_form("10-Q"))

    def _seed_band_cache(self):
        cache = make_cache()
        add_name(cache, "ACME", name="Acme Widgets Inc")
        add_name(cache, "TGT", name="Target Holdings Corp")
        smallcap.save_cache(cache)

    def test_activist_filings_record_the_subject_company_not_the_filing_fund(self):
        self._seed_band_cache()
        matched = smallcap.record_filings([
            # the 5%-owner's own copy names the fund as filer: must be ignored
            {"title": "SCHEDULE 13D - BIG FUND LP (0009999999) (Filed by)",
             "published": ISO_NOW, "link": "fund"},
            # the subject copy names the band company: this is the one we keep
            {"title": "SCHEDULE 13D - TARGET HOLDINGS CORP (0001234567) (Subject)",
             "published": ISO_NOW, "link": "subject"},
        ])
        store = smallcap.load_cache()["sec_filings"]
        self.assertEqual(matched["activist"], 1)
        self.assertEqual(list(store["activist"]), ["TGT"])
        self.assertEqual(store["activist"]["TGT"]["link"], "subject")

    def test_a_filed_by_copy_of_an_activist_filing_is_ignored_even_when_it_names_a_band_company(self):
        self._seed_band_cache()
        smallcap.record_filings([
            {"title": "SCHEDULE 13G - ACME WIDGETS INC (0001234567) (Filed by)",
             "published": ISO_NOW, "link": "x"},
        ])
        self.assertEqual(smallcap.load_cache()["sec_filings"]["activist"], {})

    def test_filer_categories_ignore_the_subject_party(self):
        self._seed_band_cache()
        smallcap.record_filings([
            {"title": "8-K - ACME WIDGETS INC (0001234567) (Subject)",
             "published": ISO_NOW, "link": "x"},
            {"title": "S-1 - ACME WIDGETS INC (0001234567) (Filer)",
             "published": ISO_NOW, "link": "y"},
        ])
        store = smallcap.load_cache()["sec_filings"]
        self.assertEqual(store["material"], {})
        self.assertEqual(list(store["offering"]), ["ACME"])

    def test_ownership_forms_and_unknown_companies_are_dropped(self):
        self._seed_band_cache()
        matched = smallcap.record_filings([
            {"title": "4 - SMITH JOHN Q (0001234567) (Reporting)",
             "published": ISO_NOW, "link": "a"},
            {"title": "144 - ACME WIDGETS INC (0001234567) (Filer)",
             "published": ISO_NOW, "link": "b"},
            {"title": "8-K - SOME MEGACAP INC (0007654321) (Filer)",
             "published": ISO_NOW, "link": "c"},
            {"not a title": True},
        ])
        self.assertEqual(matched, {"material": 0, "activist": 0, "offering": 0})
        store = smallcap.load_cache()["sec_filings"]
        self.assertEqual([store[c] for c in smallcap.FILING_CATS], [{}, {}, {}])

    def test_the_newest_filing_per_ticker_and_category_wins(self):
        self._seed_band_cache()
        smallcap.record_filings([
            {"title": "8-K - ACME WIDGETS INC (0001234567) (Filer)",
             "published": days_ago(2), "link": "older"},
            {"title": "8-K - ACME WIDGETS INC (0001234567) (Filer)",
             "published": days_ago(1), "link": "newer"},
        ])
        entry = smallcap.load_cache()["sec_filings"]["material"]["ACME"]
        self.assertEqual(entry["date"], days_ago(1))
        self.assertEqual(entry["link"], "newer")

    def test_filings_older_than_their_category_window_are_pruned(self):
        self._seed_band_cache()
        # material keeps 3 days, activist 7
        smallcap.record_filings([
            {"title": "8-K - ACME WIDGETS INC (0001234567) (Filer)",
             "published": days_ago(1), "link": "fresh-8k"},
            {"title": "8-K - TARGET HOLDINGS CORP (0001234567) (Filer)",
             "published": days_ago(9), "link": "stale-8k"},
            {"title": "SCHEDULE 13D - TARGET HOLDINGS CORP (0001234567) (Subject)",
             "published": days_ago(5), "link": "fresh-13d"},
        ])
        store = smallcap.load_cache()["sec_filings"]
        self.assertEqual(list(store["material"]), ["ACME"])
        self.assertEqual(list(store["activist"]), ["TGT"])

    def test_filings_only_match_companies_inside_the_market_cap_band(self):
        cache = make_cache()
        add_name(cache, "BIG", name="Megacap Systems Inc", mcap=9000.0)
        smallcap.save_cache(cache)
        matched = smallcap.record_filings([
            {"title": "8-K - MEGACAP SYSTEMS INC (0001234567) (Filer)",
             "published": ISO_NOW, "link": "x"},
        ])
        self.assertEqual(matched["material"], 0)


# ------------------------------------------------------------ budget ---------


class SpendBudgetTests(SmallcapTestCase):
    """_spend_budget: what a run's finite call budget is spent on, in order."""

    def _cache_with(self, band=0, unprofiled=0, band_quote_age=20.0, **kw):
        cache = make_cache(**kw)
        for i in range(band):
            add_name(cache, f"B{i:03d}", quote_t=hours_ago(band_quote_age))
        for i in range(unprofiled):
            cache["universe"][f"U{i:03d}"] = f"Unknown {i} Inc"
        return cache

    def test_reserved_discovery_slice_survives_pending_quote_upkeep(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=40, unprofiled=60)
        smallcap._spend_budget(None, cache, 20)
        # int(20 * 0.55) = 11 calls fenced off for never-profiled companies,
        # taken before the 40 pending band quote refreshes
        self.assertEqual(len(stubs.of("profile")), 11)
        self.assertTrue(all(t.startswith("U") for t in stubs.of("profile")))
        self.assertEqual(len(stubs.of("quote")), 9)
        self.assertEqual(len(stubs.calls), 20)

    def test_discovery_never_reprofiles_a_company_twice_in_one_run(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=0, unprofiled=40)
        smallcap._spend_budget(None, cache, 30)
        got = stubs.of("profile")
        self.assertEqual(len(got), 30)
        self.assertEqual(len(set(got)), 30)

    def test_published_screen_quotes_are_refreshed_before_anything_else(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=30, unprofiled=30)
        add_name(cache, "AAA", quote_t=hours_ago(5))
        add_name(cache, "BBB", quote_t=hours_ago(5))
        cache["last_screen"] = ["AAA", "BBB"]
        smallcap._spend_budget(None, cache, 2)
        self.assertEqual(stubs.calls, [("quote", "AAA"), ("quote", "BBB")])

    def test_screen_quotes_fresher_than_four_hours_are_left_alone(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=0, unprofiled=0)
        add_name(cache, "AAA", quote_t=hours_ago(3.9))
        cache["last_screen"] = ["AAA"]
        smallcap._spend_budget(None, cache, 5)
        self.assertEqual(stubs.of("quote"), [])

    def test_recently_published_cohort_quotes_outrank_discovery(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=0, unprofiled=30)
        add_name(cache, "CCC", quote_t=hours_ago(20))
        add_name(cache, "OLD", quote_t=hours_ago(20))
        smallcap.save_log({days_ago(3): log_entry(["CCC"]),
                           days_ago(40): log_entry(["OLD"])})
        smallcap._spend_budget(None, cache, 1)
        # OLD's screen is outside COHORT_LOOKBACK_DAYS, so it is not protected
        self.assertEqual(stubs.calls, [("quote", "CCC")])

    def test_metrics_backfill_cannot_spend_into_the_discovery_reserve(self):
        # The reserve is fenced off from EVERY maintenance step, not just quote
        # upkeep. A large metrics backfill used to run first and uncapped and
        # could swallow a whole run, recreating the starvation the reserve
        # exists to prevent.
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=30, unprofiled=30)
        for t in list(cache["metrics"]):
            del cache["metrics"][t]
        smallcap._spend_budget(None, cache, 10)
        reserve = int(10 * smallcap.DISCOVERY_RESERVE)
        self.assertEqual(len(stubs.of("metrics")), 10 - reserve)
        self.assertEqual(len(stubs.of("profile")), reserve)

    def test_total_calls_never_exceed_the_budget(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=50, unprofiled=50)
        cache["last_screen"] = ["B000", "B001", "B002"]
        smallcap.save_log({days_ago(2): log_entry([f"B{i:03d}" for i in range(5)])})
        smallcap._spend_budget(None, cache, 7)
        self.assertEqual(len(stubs.calls), 7)

    def test_a_zero_budget_makes_no_calls_at_all(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=20, unprofiled=20)
        smallcap._spend_budget(None, cache, 0)
        self.assertEqual(stubs.calls, [])

    def test_insider_and_slow_metric_refreshes_run_on_leftover_budget(self):
        stubs = StubFetchers().install(self)
        cache = self._cache_with(band=1, band_quote_age=0.0)
        cache["last_screen"] = ["B000"]
        cache["insider"]["B000"] = {"net30": 0, "t": hours_ago(48)}
        cache["metrics"]["B000"]["t"] = hours_ago(100)
        smallcap._spend_budget(None, cache, 5)
        self.assertEqual(stubs.calls,
                         [("insider", "B000"), ("metrics", "B000")])


# -------------------------------------------------------- track record -------


class TrackRecordTests(SmallcapTestCase):
    """update_log + evaluate: the live, versioned record."""

    def _priceable_cache(self, tickers, px=11.0, bench_iwo=105.0, quote_age=1.0):
        cache = make_cache(bench={"iwo": bench_iwo, "iwm": 90.0, "t": ISO_NOW})
        for t in tickers:
            cache["quotes"][t] = {"px": px, "dp": 0.0, "t": hours_ago(quote_age)}
        return cache

    @staticmethod
    def _names(n, prefix="T"):
        return [f"{prefix}{i:03d}" for i in range(n)]

    def test_evaluate_reports_cohort_excess_over_the_benchmark(self):
        names = self._names(25)
        cache = self._priceable_cache(names, px=11.0, bench_iwo=105.0)
        log = {days_ago(7): log_entry(names, px0=10.0, bench_iwo=100.0)}
        smallcap.snapshot_readings(cache, log)
        out = smallcap.evaluate(cache, log)
        # cohort +10%, benchmark +5% => +5 points of excess
        self.assertEqual(out["1w"]["excess"], 5.0)
        self.assertEqual(out["1w"]["days"], 1)
        self.assertEqual(out["1w"]["dropped"], 0)
        self.assertNotIn("4w", out)

    def test_evaluate_ignores_entries_from_a_different_model_version(self):
        names = self._names(25)
        cache = self._priceable_cache(names)
        log = {days_ago(7): log_entry(names, version="v2")}
        self.assertEqual(smallcap.evaluate(cache, log), {})

    def test_evaluate_only_reads_entries_inside_the_horizon_window(self):
        names = self._names(25)
        cache = self._priceable_cache(names)
        log = {days_ago(5): log_entry(names),    # too young for 1w (6-9)
               days_ago(6): log_entry(names),
               days_ago(9): log_entry(names),
               days_ago(10): log_entry(names),   # too old for 1w
               days_ago(28): log_entry(names)}   # inside 4w (25-31)
        smallcap.snapshot_readings(cache, log)
        out = smallcap.evaluate(cache, log)
        self.assertEqual(out["1w"]["days"], 2)
        self.assertEqual(out["4w"]["days"], 1)

    def test_evaluate_skips_readings_with_fewer_than_twenty_priceable_names(self):
        names = self._names(25)
        cache = self._priceable_cache(names)
        for t in names[:6]:                       # 19 priceable names left
            cache["quotes"][t]["t"] = hours_ago(40)   # stale beyond 30h
        log = {days_ago(7): log_entry(names)}
        smallcap.snapshot_readings(cache, log)
        self.assertEqual(smallcap.evaluate(cache, log), {})

        cache["quotes"][names[0]]["t"] = hours_ago(1)  # back to 20
        smallcap.snapshot_readings(cache, log)
        out = smallcap.evaluate(cache, log)
        self.assertEqual(out["1w"]["days"], 1)
        self.assertEqual(out["1w"]["dropped"], 5)

    def _frozen(self, names, age, excess):
        return dict(log_entry(names),
                    read_1w={"excess": excess, "age": 7, "dropped": 0})

    def test_independent_readings_accumulate_across_the_whole_record(self):
        # The point of the snapshot design: frozen readings pile up over time,
        # so cohorts spread across weeks count as separate evidence. Before,
        # evaluate only ever saw one 3-day window, so `indep` was stuck at 1
        # forever and the freeze criterion built on it was unsatisfiable.
        names = self._names(25)
        cache = self._priceable_cache(names)
        log = {days_ago(age): self._frozen(names, age, 2.0)
               for age in (7, 14, 21, 28)}
        out = smallcap.evaluate(cache, log)
        self.assertEqual(out["1w"]["days"], 4)
        self.assertEqual(out["1w"]["indep"], 4)
        self.assertEqual(out["1w"]["excess"], 2.0)

    def test_readings_closer_than_the_gap_count_as_one_observation(self):
        names = self._names(25)
        cache = self._priceable_cache(names)
        log = {days_ago(age): self._frozen(names, age, 1.0)
               for age in (6, 7, 9)}          # all within one week
        out = smallcap.evaluate(cache, log)
        self.assertEqual(out["1w"]["days"], 3)
        self.assertEqual(out["1w"]["indep"], 1)

    def test_a_frozen_reading_is_never_recomputed(self):
        # Once frozen, a reading must not move when prices move — that drift
        # was what made the published number wander daily without new evidence.
        names = self._names(25)
        cache = self._priceable_cache(names, px=11.0, bench_iwo=105.0)
        log = {days_ago(7): log_entry(names, px0=10.0, bench_iwo=100.0)}
        smallcap.snapshot_readings(cache, log)
        first = smallcap.evaluate(cache, log)["1w"]["excess"]
        for t in names:                       # prices move a lot afterwards
            cache["quotes"][t]["px"] = 20.0
        smallcap.snapshot_readings(cache, log)
        self.assertEqual(smallcap.evaluate(cache, log)["1w"]["excess"], first)

    def test_evaluate_needs_both_benchmark_readings(self):
        names = self._names(25)
        log = {days_ago(7): log_entry(names, bench_iwo=None)}
        cache = self._priceable_cache(names)
        smallcap.snapshot_readings(cache, log)
        self.assertEqual(smallcap.evaluate(cache, log), {})

        log = {days_ago(7): log_entry(names)}
        no_bench = self._priceable_cache(names)
        no_bench["bench"] = {}
        smallcap.snapshot_readings(no_bench, log)
        self.assertEqual(smallcap.evaluate(no_bench, log), {})

    def test_update_log_writes_no_duplicate_when_the_benchmark_has_not_moved(self):
        smallcap.save_log({days_ago(1): log_entry(["AAA"], bench_iwo=361.32)})
        cache = make_cache(bench={"iwo": 361.32, "iwm": 285.11, "t": ISO_NOW})
        published = [{"ticker": "AAA", "score": 80.0, "px": 10.0}]
        returned = smallcap.update_log(cache, published, published)
        self.assertNotIn(TODAY, returned)
        self.assertNotIn(TODAY, smallcap.load_log())   # nothing written to disk

    def test_update_log_records_the_screen_when_the_benchmark_moved(self):
        smallcap.save_log({days_ago(1): log_entry(["AAA"], bench_iwo=361.32)})
        cache = make_cache(bench={"iwo": 362.00, "iwm": 285.11, "t": ISO_NOW})
        published = [{"ticker": "AAA", "score": 80.0, "px": 10.0}]
        candidates = published + [{"ticker": "BBB", "score": 70.0, "px": 4.0}]
        smallcap.update_log(cache, published, candidates)
        entry = smallcap.load_log()[TODAY]
        self.assertEqual(entry["v"], smallcap.MODEL_VERSION)
        self.assertEqual(entry["pub"], [["AAA", 80.0, 10.0]])
        self.assertEqual(entry["cand"], ["AAA", "BBB"])
        self.assertEqual(entry["bench"], {"iwo": 362.00, "iwm": 285.11})

    def test_update_log_writes_when_the_previous_entry_used_the_old_bench_format(self):
        old = log_entry(["AAA"])
        old["bench"] = 361.32                     # v2 logged a bare float
        smallcap.save_log({days_ago(1): old})
        cache = make_cache(bench={"iwo": 361.32, "iwm": 285.11, "t": ISO_NOW})
        published = [{"ticker": "AAA", "score": 80.0, "px": 10.0}]
        smallcap.update_log(cache, published, published)
        self.assertIn(TODAY, smallcap.load_log())

    def test_update_log_keeps_a_year_of_history(self):
        log = {(FIXED_NOW - timedelta(days=d)).strftime("%Y-%m-%d"):
               log_entry(["AAA"], bench_iwo=100.0 + d) for d in range(1, 380)}
        smallcap.save_log(log)
        cache = make_cache(bench={"iwo": 1.0, "iwm": 1.0, "t": ISO_NOW})
        published = [{"ticker": "AAA", "score": 80.0, "px": 10.0}]
        smallcap.update_log(cache, published, published)
        self.assertEqual(len(smallcap.load_log()), 370)


# -------------------------------------------------- eligibility & screen -----


class EligibilityTests(SmallcapTestCase):

    def test_revenue_floor_is_fifty_million_trailing(self):
        cache = make_cache()
        add_name(cache, "LOW", rps=2.45, shares=20.0)     # $49.0M
        add_name(cache, "EDGE", rps=2.50, shares=20.0)    # $50.0M exactly
        self.assertFalse(smallcap._eligible(cache, "LOW"))
        self.assertTrue(smallcap._eligible(cache, "EDGE"))
        self.assertTrue(smallcap._base_eligible(cache, "LOW"))

    def test_names_under_the_revenue_floor_are_listed_separately_by_momentum(self):
        cache = make_cache()
        add_name(cache, "LOW", rps=1.0, shares=20.0, r13=5.0)
        add_name(cache, "LOWER", rps=1.0, shares=20.0, r13=25.0)
        add_name(cache, "BIG", rps=5.0, shares=20.0)
        rows = smallcap.below_floor(cache)
        self.assertEqual([r["ticker"] for r in rows], ["LOWER", "LOW"])
        self.assertEqual(rows[0]["rev_ttm"], 20.0)

    def test_market_cap_band_is_inclusive_at_both_ends(self):
        cache = make_cache()
        add_name(cache, "UNDER", mcap=299.9)
        add_name(cache, "LOWEDGE", mcap=300.0)
        add_name(cache, "HIEDGE", mcap=2000.0)
        add_name(cache, "OVER", mcap=2000.1)
        add_name(cache, "NOMCAP", mcap=None)
        self.assertEqual([t for t in cache["profiles"]
                          if smallcap.in_band(cache["profiles"][t])],
                         ["LOWEDGE", "HIEDGE"])

    def test_otc_listings_are_excluded_from_the_band(self):
        cache = make_cache()
        add_name(cache, "OTCX", exch="OTC MARKETS")
        add_name(cache, "LIST", exch="NEW YORK STOCK EXCHANGE, INC.")
        self.assertFalse(smallcap.in_band(cache["profiles"]["OTCX"]))
        self.assertTrue(smallcap.in_band(cache["profiles"]["LIST"]))

    def test_price_and_volume_minimums_are_enforced(self):
        cache = make_cache()
        add_name(cache, "CHEAP", px=1.99)
        add_name(cache, "OKPX", px=2.00)
        add_name(cache, "THIN", adv=0.049)
        add_name(cache, "OKADV", adv=0.05)
        self.assertFalse(smallcap._eligible(cache, "CHEAP"))
        self.assertTrue(smallcap._eligible(cache, "OKPX"))
        self.assertFalse(smallcap._eligible(cache, "THIN"))
        self.assertTrue(smallcap._eligible(cache, "OKADV"))

    def test_a_blank_industry_tag_excludes_closed_end_funds_and_shells(self):
        cache = make_cache()
        add_name(cache, "CEF", ind="")
        add_name(cache, "SHELL", ind="N/A")
        add_name(cache, "REAL", ind="Technology")
        self.assertFalse(smallcap._base_eligible(cache, "CEF"))
        self.assertFalse(smallcap._base_eligible(cache, "SHELL"))
        self.assertTrue(smallcap._base_eligible(cache, "REAL"))

    def test_missing_growth_or_momentum_measures_make_a_name_ineligible(self):
        cache = make_cache()
        add_name(cache, "NOG", rev_g=None)
        add_name(cache, "NOM", r13=None)
        add_name(cache, "NOPX", px=None)
        for t in ("NOG", "NOM", "NOPX"):
            with self.subTest(ticker=t):
                self.assertFalse(smallcap._base_eligible(cache, t))

    def test_one_security_per_company_keeps_the_shortest_ticker(self):
        cache = make_cache()
        add_name(cache, "ACME", name="Acme Widgets Inc")
        add_name(cache, "ACMEA", name="Acme Widgets Inc")      # class A share
        add_name(cache, "BETA", name="Beta Systems Corp")
        add_name(cache, "ALFA", name="Beta Systems Corp")      # same length: A wins
        kept = smallcap._dedupe_by_company(cache, list(cache["profiles"]))
        self.assertEqual(kept, ["ACME", "ALFA"])


class ComputeScreenTests(SmallcapTestCase):

    def _grouped_cache(self, per_group, groups=("Technology", "Banking")):
        cache = make_cache()
        for gi, ind in enumerate(groups):
            for i in range(per_group):
                add_name(cache, f"{chr(65 + gi)}{i:02d}", ind=ind,
                         rev_g=30.0 + i, r13=10.0 + i, rg3=20.0 + i,
                         rev_gq=35.0 + i, cfps=1.0 + i * 0.1, dte=50.0 - i,
                         gm_t=40.0 + i, gm_a=38.0, rg5=10.0, rsg5=8.0)
        return cache

    def test_published_list_caps_each_industry_group_at_five(self):
        cache = self._grouped_cache(per_group=8)
        published, candidates = smallcap.compute_screen(cache)
        self.assertEqual(len(candidates), 16)
        by_group = {}
        for r in published:
            by_group[r["group"]] = by_group.get(r["group"], 0) + 1
        self.assertEqual(by_group, {"Technology": 5, "Financials": 5})
        self.assertEqual(len(published), 10)

    def test_published_list_stops_at_the_screen_size(self):
        groups = ("Technology", "Banking", "Energy", "Retail", "Machinery",
                  "Chemicals", "Utilities", "Real Estate")
        cache = self._grouped_cache(per_group=5, groups=groups)
        published, candidates = smallcap.compute_screen(cache)
        self.assertEqual(len(published), smallcap.SCREEN_SIZE)
        self.assertEqual(len(candidates), smallcap.CANDIDATES)

    def test_only_revenue_growers_are_published(self):
        cache = self._grouped_cache(per_group=3)
        cache["metrics"]["A00"]["rev_g"] = 0.0      # flat: not a grower
        cache["metrics"]["A01"]["rev_g"] = -5.0
        published, candidates = smallcap.compute_screen(cache)
        self.assertNotIn("A00", [r["ticker"] for r in published])
        self.assertNotIn("A01", [r["ticker"] for r in published])
        self.assertIn("A00", [r["ticker"] for r in candidates])

    def test_rows_are_ordered_by_descending_score(self):
        cache = self._grouped_cache(per_group=4)
        published, candidates = smallcap.compute_screen(cache)
        scores = [r["score"] for r in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([r["ticker"] for r in published],
                         [r["ticker"] for r in candidates
                          if r["ticker"] in {p["ticker"] for p in published}])

    def test_a_name_absent_from_the_previous_candidates_takes_the_newcomer_penalty(self):
        cache = self._grouped_cache(per_group=6)
        base, _ = smallcap.compute_screen(cache)
        base_scores = {r["ticker"]: r["score"] for r in base}
        newcomer = base[0]["ticker"]
        prev = [t for t in cache["metrics"] if t != newcomer]

        penalized, _ = smallcap.compute_screen(cache, prev_candidates=prev)
        scores = {r["ticker"]: r["score"] for r in penalized}
        self.assertLess(scores[newcomer], base_scores[newcomer])
        self.assertAlmostEqual(
            scores[newcomer],
            base_scores[newcomer] * smallcap.NEWCOMER_PENALTY, delta=0.1)
        for t, s in scores.items():
            if t != newcomer:
                self.assertEqual(s, base_scores[t])

    def test_an_empty_previous_candidate_list_penalises_nobody(self):
        cache = self._grouped_cache(per_group=4)
        base, _ = smallcap.compute_screen(cache)
        same, _ = smallcap.compute_screen(cache, prev_candidates=[])
        self.assertEqual([r["score"] for r in same], [r["score"] for r in base])

    def test_new_flag_marks_names_missing_from_the_previous_published_list(self):
        cache = self._grouped_cache(per_group=3)
        published, _ = smallcap.compute_screen(cache, prev_published={"A00"})
        flags = {r["ticker"]: r["flags"] for r in published}
        self.assertNotIn("new", flags["A00"])
        self.assertEqual(flags["A01"][0], "new")

        # bootstrap (no previous screen at all) flags nobody
        published, _ = smallcap.compute_screen(cache, prev_published=None)
        self.assertEqual([r["flags"] for r in published], [[]] * len(published))

    def test_screen_flags_come_from_earnings_insider_and_filings(self):
        cache = self._grouped_cache(per_group=2)
        cache["earn_map"] = {"A00": days_ago(-3)}          # reports in 3 days
        cache["insider"] = {"A00": {"net30": 5000, "t": hours_ago(2)}}
        cache["sec_filings"] = {
            "material": {"A00": {"date": days_ago(1)}},
            "activist": {"A00": {"date": days_ago(5)}},
            "offering": {"A00": {"date": days_ago(30)}},   # outside the window
        }
        published, _ = smallcap.compute_screen(cache)
        flags = {r["ticker"]: r["flags"] for r in published}
        self.assertEqual(flags["A00"], ["E-3d", "ins+", "8-K", "act+"])
        self.assertEqual(flags["A01"], [])

    def test_a_stale_insider_reading_does_not_raise_the_flag(self):
        cache = self._grouped_cache(per_group=2)
        cache["insider"] = {"A00": {"net30": 5000, "t": hours_ago(49)}}
        published, _ = smallcap.compute_screen(cache)
        self.assertEqual([r for r in published if r["ticker"] == "A00"][0]["flags"], [])

    def test_an_empty_universe_screens_to_nothing(self):
        self.assertEqual(smallcap.compute_screen(make_cache()), ([], []))


# ------------------------------------------------------------- ranking -------


class RankingTests(unittest.TestCase):

    def test_missing_values_rank_neutral(self):
        self.assertEqual(smallcap._percentile_ranks([None, None]), [0.5, 0.5])
        ranks = smallcap._percentile_ranks([10.0, None, 30.0, 20.0])
        self.assertEqual(ranks[1], 0.5)

    def test_ranks_span_zero_to_one_lowest_first(self):
        self.assertEqual(smallcap._percentile_ranks([30.0, 10.0, 20.0]),
                         [1.0, 0.0, 0.5])

    def test_a_lone_known_value_is_neutral(self):
        self.assertEqual(smallcap._percentile_ranks([7.0]), [0.5])
        self.assertEqual(smallcap._percentile_ranks([None, 7.0, None]),
                         [0.5, 0.5, 0.5])

    def test_tied_values_share_the_average_of_the_ranks_they_span(self):
        # identical inputs must score identically: rank used to be decided by
        # alphabetical position, which gave equal companies unequal sub-scores
        self.assertEqual(smallcap._percentile_ranks([5.0, 5.0, 5.0]),
                         [0.5, 0.5, 0.5])
        # a tie at the bottom of four values spans ranks 0 and 1/3 -> 1/6
        ranks = smallcap._percentile_ranks([1.0, 1.0, 2.0, 3.0])
        self.assertEqual(ranks[0], ranks[1])
        self.assertAlmostEqual(ranks[0], (0 + 1) / 2 / 3)
        self.assertAlmostEqual(ranks[3], 1.0)

    def test_untied_values_still_span_zero_to_one(self):
        self.assertEqual(smallcap._percentile_ranks([1.0, 2.0, 3.0]),
                         [0.0, 0.5, 1.0])

    def test_grouped_ranks_use_the_group_once_it_has_min_group_members(self):
        values = list(range(16))
        groups = ["A"] * 8 + ["B"] * 8
        ranks = smallcap._grouped_ranks(values, groups)
        # each group of 8 is ranked within itself: 0..1 twice over
        self.assertEqual(ranks[:8], ranks[8:])
        self.assertEqual(ranks[0], 0.0)
        self.assertEqual(ranks[7], 1.0)
        self.assertEqual(ranks[8], 0.0)
        self.assertEqual(ranks[15], 1.0)

    def test_groups_smaller_than_min_group_fall_back_to_global_ranks(self):
        values = [float(v) for v in range(11)]
        groups = ["A"] * 8 + ["B"] * 3
        ranks = smallcap._grouped_ranks(values, groups)
        global_ranks = smallcap._percentile_ranks(values)
        self.assertEqual(ranks[8:], global_ranks[8:])
        self.assertNotEqual(ranks[:8], global_ranks[:8])

    def test_grouped_ranks_keep_missing_values_neutral(self):
        values = [None] * 8 + [1.0, 2.0, 3.0]
        groups = ["A"] * 8 + ["B"] * 3
        self.assertEqual(smallcap._grouped_ranks(values, groups)[:8], [0.5] * 8)


class DistributedOrderTests(unittest.TestCase):

    TICKERS = [a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCD"]

    def test_order_is_deterministic_and_independent_of_input_order(self):
        first = smallcap._distributed(self.TICKERS)
        again = smallcap._distributed(list(reversed(self.TICKERS)))
        self.assertEqual(first, again)
        self.assertEqual(first, smallcap._distributed(self.TICKERS))
        self.assertCountEqual(first, self.TICKERS)

    def test_order_spreads_across_the_alphabet_instead_of_front_loading(self):
        head = smallcap._distributed(self.TICKERS)[:26]
        self.assertGreaterEqual(len(set(t[0] for t in head)), 12)
        # alphabetical order would cover at most 7 initials in 26 names
        self.assertLessEqual(len(set(t[0] for t in sorted(self.TICKERS)[:26])), 7)


# ------------------------------------------------------------- helpers -------


class HelperTests(SmallcapTestCase):

    def test_industry_group_maps_vendor_tags_to_coarse_groups(self):
        cases = {
            "Banking": "Financials", "Financial Services": "Financials",
            "Insurance": "Financials", "Biotechnology": "Health",
            "Pharmaceuticals": "Health", "Health Care": "Health",
            "Life Sciences Tools & Servic": "Health",
            "Telecommunication": "Telecom", "Semiconductors": "Technology",
            "Technology": "Technology", "Energy": "Energy",
            "Metals & Mining": "Materials", "Machinery": "Industrials",
            "Aerospace & Defense": "Industrials", "Airlines": "Industrials",
            "Retail": "Consumer", "Media": "Consumer", "Automobiles": "Consumer",
            "Real Estate": "Real Estate", "Utilities": "Utilities",
        }
        for ind, group in cases.items():
            with self.subTest(industry=ind):
                self.assertEqual(smallcap.industry_group(ind), group)

    def test_unmatched_and_blank_industries_fall_into_other(self):
        for ind in ("", None, "N/A", "Conglomerate"):
            with self.subTest(industry=ind):
                self.assertEqual(smallcap.industry_group(ind), "Other")

    def test_communications_companies_are_grouped_with_telecom(self):
        # a live vendor label that matched no keyword, so those companies were
        # ranked against the catch-all bucket instead of real peers
        self.assertEqual(smallcap.industry_group("Communications"), "Telecom")
        self.assertEqual(smallcap.industry_group("Telecommunication"), "Telecom")

    def test_name_key_strips_punctuation_and_case_and_truncates(self):
        self.assertEqual(smallcap._name_key("Acme Widgets, Inc."), "acmewidgetsinc")
        self.assertEqual(smallcap._name_key("ACME WIDGETS INC"), "acmewidgetsinc")
        self.assertEqual(smallcap._name_key(None), "")
        self.assertEqual(len(smallcap._name_key("A" * 40)), 24)

    def test_rev_ttm_multiplies_revenue_per_share_by_shares_outstanding(self):
        cache = make_cache()
        add_name(cache, "AAA", rps=4.0, shares=25.0)
        self.assertEqual(smallcap.rev_ttm(cache, "AAA"), 100.0)
        cache["profiles"]["AAA"]["shares"] = None
        self.assertIsNone(smallcap.rev_ttm(cache, "AAA"))
        self.assertIsNone(smallcap.rev_ttm(cache, "MISSING"))

    def test_a_genuinely_revenue_less_company_lands_below_the_floor(self):
        # zero must mean zero, not "unknown": pre-revenue companies used to
        # vanish from the page entirely — unscored AND unlisted
        cache = make_cache()
        add_name(cache, "AAA", rps=0.0, shares=25.0)
        self.assertEqual(smallcap.rev_ttm(cache, "AAA"), 0.0)
        self.assertEqual([r["ticker"] for r in smallcap.below_floor(cache)], ["AAA"])

    def test_missing_revenue_data_still_reads_as_unknown(self):
        cache = make_cache()
        add_name(cache, "BBB", rps=None, shares=25.0)
        self.assertIsNone(smallcap.rev_ttm(cache, "BBB"))

    def test_a_price_above_the_recorded_52_week_high_reads_as_unknown(self):
        cache = make_cache()
        add_name(cache, "STALE", px=13.0, hi52=12.5)
        add_name(cache, "BELOW", px=10.0, hi52=12.5)
        add_name(cache, "ATHIGH", px=12.5, hi52=12.5)
        add_name(cache, "NOHIGH", px=10.0, hi52=None)
        self.assertIsNone(smallcap._factors(cache, "STALE")["from_high"])
        self.assertAlmostEqual(smallcap._factors(cache, "BELOW")["from_high"], -20.0)
        self.assertEqual(smallcap._factors(cache, "ATHIGH")["from_high"], 0.0)
        self.assertIsNone(smallcap._factors(cache, "NOHIGH")["from_high"])

    def test_funding_ranks_profitable_names_above_every_cash_burner(self):
        cache = make_cache()
        add_name(cache, "PROF", cfps=2.0, rps=10.0)          # 20% cash margin
        add_name(cache, "RICH", cfps=20.0, rps=10.0)         # capped at 0.6
        add_name(cache, "BURN", cfps=-1.0, cashps=1.0)       # 12 months runway
        add_name(cache, "DRY", cfps=-1.0, cashps=None)       # no cash reading
        add_name(cache, "UNKNOWN", cfps=None)
        f = {t: smallcap._factors(cache, t)["funding"] for t in
             ("PROF", "RICH", "BURN", "DRY", "UNKNOWN")}
        self.assertAlmostEqual(f["PROF"], 0.2)
        self.assertAlmostEqual(f["RICH"], 0.6)
        self.assertAlmostEqual(f["BURN"], 12.0 / 36.0 - 1.05)
        self.assertAlmostEqual(f["DRY"], -1.1)
        self.assertIsNone(f["UNKNOWN"])
        self.assertLess(f["BURN"], 0.0)
        self.assertLess(f["DRY"], f["BURN"])

    def test_dilution_prefers_our_own_share_history_once_60_days_apart(self):
        cache = make_cache()
        add_name(cache, "OWN", rg5=10.0, rsg5=4.0,
                 shist=[["2026-06-17", 20.0], ["2026-09-15", 22.0]])
        add_name(cache, "TOOSOON", rg5=10.0, rsg5=4.0,
                 shist=[["2026-09-01", 20.0], ["2026-09-15", 22.0]])
        # 10% more shares over 90 days, annualised
        self.assertAlmostEqual(smallcap._factors(cache, "OWN")["dilution"],
                               47.19, delta=0.01)
        # under 60 days apart: falls back to the 5-year revenue/share proxy
        self.assertAlmostEqual(smallcap._factors(cache, "TOOSOON")["dilution"], 6.0)

    def test_momentum_is_volatility_scaled_with_a_floor(self):
        cache = make_cache()
        add_name(cache, "CALM", r13=30.0, r26=20.0, vol=10.0)   # vol below floor
        add_name(cache, "WILD", r13=30.0, r26=20.0, vol=60.0)
        add_name(cache, "NO26", r13=30.0, r26=None, vol=30.0)
        blended = 0.6 * 30.0 + 0.4 * 20.0
        self.assertAlmostEqual(smallcap._factors(cache, "CALM")["momo"],
                               blended / smallcap.VOL_FLOOR)
        self.assertAlmostEqual(smallcap._factors(cache, "WILD")["momo"],
                               blended / 60.0)
        self.assertAlmostEqual(smallcap._factors(cache, "NO26")["momo"], 1.0)

    def test_extreme_growth_and_momentum_readings_are_clamped(self):
        cache = make_cache()
        add_name(cache, "MOON", rev_g=900.0, rg3=500.0, rev_gq=None, r13=900.0)
        add_name(cache, "CRASH", rev_g=-90.0, rg3=-90.0, r13=-90.0)
        moon = smallcap._factors(cache, "MOON")
        crash = smallcap._factors(cache, "CRASH")
        self.assertEqual((moon["g_ttm"], moon["g_3y"]), (150.0, 100.0))
        self.assertEqual((crash["g_ttm"], crash["g_3y"]), (-20.0, -20.0))
        self.assertAlmostEqual(moon["momo"], 150.0 / 30.0)   # clamped r13, vol 30

    def test_match_news_tags_headlines_by_ticker_or_full_company_name(self):
        cache = make_cache()
        add_name(cache, "ACME", name="Acme Widgets Inc")
        add_name(cache, "BETA", name="Beta Co")                   # name too short
        add_name(cache, "HUGE", name="Megacap Systems Inc", mcap=9000.0)
        hits = smallcap.match_news([
            {"title": "Acme Widgets Inc wins a contract", "link": "1"},
            {"title": "Something about (NASDAQ: BETA) today", "link": "2"},
            {"title": "Beta Co announces nothing", "link": "3"},
            {"title": "Megacap Systems Inc buys a rival", "link": "4"},
        ], cache)
        self.assertEqual([(h["ticker"], h["link"]) for h in hits],
                         [("ACME", "1"), ("BETA", "2")])

    def test_match_news_dedupes_by_link_and_stops_at_eight_hits(self):
        cache = make_cache()
        for i in range(10):
            add_name(cache, f"T{chr(65 + i)}", name=f"Company {chr(65 + i)} Inc")
        items = [{"title": f"(T{chr(65 + i)}) reports results", "link": f"l{i}"}
                 for i in range(10)]
        items.insert(1, {"title": "(TA) reports results again", "link": "l0"})
        hits = smallcap.match_news(items, cache)
        self.assertEqual(len(hits), 8)
        self.assertEqual(len({h["link"] for h in hits}), 8)

    def test_movers_lists_the_freshest_biggest_gainers_and_losers(self):
        cache = make_cache()
        for i, dp in enumerate([9.0, 7.0, 5.0, 3.0, 1.0, -1.0, -8.0]):
            add_name(cache, f"M{i}", dp=dp)
        add_name(cache, "STALEQ", dp=99.0, quote_t=hours_ago(30))
        up, down = smallcap.movers(cache)
        self.assertEqual([r["ticker"] for r in up], ["M0", "M1", "M2", "M3", "M4"])
        self.assertEqual([r["ticker"] for r in down][0], "M6")
        self.assertNotIn("STALEQ", [r["ticker"] for r in up])
        # the lists must be disjoint: the same stock used to be shown as both
        # a top gainer and a top loser when few names were fresh
        self.assertEqual(set(r["ticker"] for r in up)
                         & set(r["ticker"] for r in down), set())
        self.assertEqual([r["ticker"] for r in down], ["M6", "M5"])

    def test_movers_reports_no_losers_when_five_or_fewer_names_are_fresh(self):
        cache = make_cache()
        for i, dp in enumerate([3.0, 1.0, -2.0]):
            add_name(cache, f"M{i}", dp=dp)
        up, down = smallcap.movers(cache)
        self.assertEqual(len(up), 3)
        self.assertEqual(down, [])


if __name__ == "__main__":
    unittest.main()
