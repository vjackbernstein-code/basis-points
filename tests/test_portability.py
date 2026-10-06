#!/usr/bin/env python3
"""The project must run on the Python versions it will actually meet.

`pipeline.py` once contained a nested f-string with escaped quotes, which is
valid only from Python 3.12. The scheduled job pins 3.12, so the published site
was never affected and nothing looked wrong — but the test suite would not even
import on anything older, and a cloud environment whose default `python3` was
3.11 hit it. The reviewer running there spent part of its run working out why
every test errored before it could review anything.

That is the shape of the fault worth guarding: not a broken site, but a broken
environment that only some people meet, for a reason that looks nothing like
the symptom. Syntax is checked against the floor rather than trusted to the
one interpreter that happens to be installed, because the interpreter running
these tests is by definition not the one that would catch it.

Run:  python3 -m unittest discover tests
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The oldest Python the source must parse on. The scheduled job pins 3.12 and
# may be raised freely; this floor exists so that the project stays runnable
# wherever someone opens it, and so that reaching for a brand-new syntax
# feature is a deliberate decision with a test to change, not an accident.
MIN_PYTHON = (3, 9)


def _sources():
    return sorted(list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py")))


class SyntaxFloorTests(unittest.TestCase):

    def test_there_are_sources_to_check(self):
        # a glob that quietly matches nothing would make every test below pass
        self.assertGreater(len(_sources()), 5)

    def test_every_module_parses_on_the_oldest_supported_python(self):
        failed = []
        for path in _sources():
            try:
                ast.parse(path.read_text(encoding="utf-8"), str(path),
                          feature_version=MIN_PYTHON)
            except SyntaxError as e:
                failed.append(f"{path.name}:{e.lineno} {e.msg}")
        self.assertEqual(
            [], failed,
            f"these need Python newer than "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}: {failed}")

    def test_the_scheduled_job_pins_a_version_at_or_above_the_floor(self):
        wf = (ROOT / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8")
        import re
        m = re.search(r'python-version:\s*"?(\d+)\.(\d+)', wf)
        self.assertIsNotNone(m, "the workflow no longer pins a Python version")
        pinned = (int(m.group(1)), int(m.group(2)))
        self.assertGreaterEqual(
            pinned, MIN_PYTHON,
            f"the job pins {pinned}, below this suite's floor {MIN_PYTHON}")


if __name__ == "__main__":
    unittest.main()
