#!/usr/bin/env python3
"""The README must describe the rules the code actually follows.

This file exists because it did not. On 2026-10-06 the README still said five
books when there were six, `COST_BPS` (25 bps) when the code charged 40, a
conviction cap of "0.5x-2x" when the ramp ran 1.5x down to 0.6x, and a
`STOP_PCT` (20%) stop from entry — a constant that does not exist, describing a
mechanism that was replaced by a volatility-scaled trailing stop. Two of those
had been wrong for weeks.

Stale documentation is not a cosmetic fault here. This repository is public and
the README is where a reader, a reviewer or a fresh session goes to learn what
the rules are. A reader who checks the published numbers against it and finds
they disagree has no way to tell which one is lying, and is right not to trust
either. Worse, a reviewer briefed from it will test the system against rules it
does not have, and report sound behaviour as a defect.

So the numbers are not trusted to prose. Anything the README states as
`CONSTANT` (value) is read back out of it and compared against the module, and
any `CONSTANT` it names must exist. Changing a constant now means changing the
README in the same commit, which is the only arrangement that has ever kept
documentation honest.

Run:  python3 -m unittest discover tests
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import decision  # noqa: E402
import pipeline  # noqa: E402
import sharadar  # noqa: E402
import portfolio  # noqa: E402
import smallcap  # noqa: E402

MODULES = (portfolio, smallcap, decision, pipeline, sharadar)

# Names that are environment variables, not module constants. Anything else
# the README puts in backticks in capitals is expected to be a real constant.
NOT_CONSTANTS = {"FINNHUB_API_KEY", "FRED_API_KEY",
                 "NASDAQ_DATA_LINK_API_KEY"}

README = (ROOT / "README.md").read_text(encoding="utf-8")


def _resolve(name):
    for m in MODULES:
        if hasattr(m, name):
            return getattr(m, name)
    return None


class ReadmeNamesRealThingsTests(unittest.TestCase):

    def test_every_constant_the_readme_names_exists(self):
        # `STOP_PCT` (20%) sat in the README for weeks describing a stop the
        # code had stopped using. A name that resolves to nothing is the
        # cheapest possible signal that a paragraph has gone stale.
        named = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", README))
        for name in sorted(named - NOT_CONSTANTS):
            self.assertIsNotNone(
                _resolve(name),
                f"README names `{name}`, which no module defines — either the "
                f"constant was renamed or removed and the prose was left behind")

    def test_stated_values_match_the_code(self):
        """Every "`CONSTANT` (number...)" in the README is checked."""
        pairs = re.findall(
            r"`([A-Z][A-Z0-9_]{2,})`\s*\((-?[\d.]+)\s*(%?)", README)
        self.assertGreater(len(pairs), 3, "the pattern stopped matching")
        for name, stated, pct in pairs:
            if name in NOT_CONSTANTS:
                continue
            actual = _resolve(name)
            self.assertIsNotNone(actual, f"`{name}` does not exist")
            if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                continue
            # a constant held as a fraction is routinely documented as a
            # percentage, which is the clearer way to write it for a reader;
            # either reading of the stated number counts as agreement
            ok = {float(actual)}
            if pct:
                ok.add(float(actual) * 100.0)
            self.assertTrue(
                any(abs(float(stated) - v) < 1e-4 for v in ok),
                f"README says `{name}` is {stated}{pct}, code says {actual}")


class ReadmeDescribesTheRealRulesTests(unittest.TestCase):

    def test_the_book_count_is_right(self):
        n = len(portfolio.STRATEGIES)
        word = {5: "five", 6: "six", 7: "seven"}[n]
        wrong = {5: "six", 6: "five", 7: "six"}[n]
        self.assertIn(f"{word} books", README.lower(),
                      f"there are {n} books and the README does not say so")
        # A quotation of the former wording is not a stale claim: the README
        # recounts that the page once said "five books" in nine places, which
        # is the story of the fix and has to survive the test for the fix.
        unquoted = re.sub(r'"[^"\n]*"', " ", README).lower()
        self.assertNotIn(f"{wrong} books", unquoted,
                         f"the README still claims {wrong} books")

    def test_the_stop_is_not_described_as_a_fixed_distance_from_entry(self):
        # the old rule was a flat 20% below the ENTRY price; the current one
        # trails the day's high and scales with the name's own volatility, so
        # "from entry" is not a wording quibble, it is a different mechanism
        self.assertNotRegex(
            README, r"down `?STOP_\w+`?[^.\n]{0,40}from entry",
            "the README describes the retired fixed stop")
        self.assertIn("STOP_SIGMA", README,
                      "the volatility-scaled stop is not documented")

    def test_the_stop_bounds_are_stated(self):
        lo = int(round(portfolio.STOP_MIN * 100))
        hi = int(round(portfolio.STOP_MAX * 100))
        self.assertRegex(
            README, rf"{lo}\s*[–-]\s*{hi}%",
            f"the clamp of {lo}-{hi}% is not stated, so a reader cannot tell "
            f"why a page shows a stop at exactly one of those two numbers")

    def test_conviction_is_described_as_rank_not_score(self):
        # the spec key is still called "score", which is what misled the
        # README; the weight comes from rank alone and the gaps between
        # scores are deliberately ignored
        m = re.search(r"\*\*B Conviction\*\*\s*\|([^|]*)\|", README)
        self.assertIsNotNone(m, "the book table changed shape")
        cell = m.group(1).lower()
        self.assertIn("rank", cell)
        self.assertNotIn("weighted by score", cell)


if __name__ == "__main__":
    unittest.main()
