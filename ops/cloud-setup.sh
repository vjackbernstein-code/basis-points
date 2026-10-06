#!/usr/bin/env bash
# Setup for a Claude cloud environment (or any fresh checkout).
#
# This project depends on nothing outside the Python standard library, so there
# is nothing to install and this script installs nothing. What it does instead
# is fail loudly if the checkout cannot actually run, because the alternative
# has already cost a run: a cloud environment whose default python3 was 3.11
# met a line of 3.12-only syntax, every test errored on import, and the only
# visible symptom was an agent apparently unable to review anything.
#
# Point the environment's setup command at this file:
#     bash ops/cloud-setup.sh
#
# It makes no network calls and writes nothing outside __pycache__.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Basis Points — checking this environment can run the project"
echo

PY="${PYTHON:-python3}"
echo "interpreter: $("$PY" -V 2>&1) at $(command -v "$PY")"

# The floor the test suite asserts. Checked here too, so a bad environment
# says so in one line instead of in a hundred import errors.
"$PY" - <<'PYEOF'
import sys
MIN = (3, 9)
if sys.version_info[:2] < MIN:
    sys.exit(f"this project needs Python {MIN[0]}.{MIN[1]} or newer; "
             f"this is {sys.version_info.major}.{sys.version_info.minor}")
PYEOF

echo
echo "running the test suite"
"$PY" -m unittest discover tests -q

echo
if [ -n "${FINNHUB_API_KEY:-}" ] || [ -f data/finnhub.key ]; then
  echo "a Finnhub key is present: 'python3 pipeline.py' will fetch live data."
else
  echo "no Finnhub key here, which is expected and fine."
  echo "  A full 'python3 pipeline.py' is REFUSED, because without a key it"
  echo "  does not fail — it quietly rebuilds from cached measurements and"
  echo "  would overwrite good published data with a degraded copy."
  echo "  Use 'python3 pipeline.py --render-only' to rebuild the pages from"
  echo "  the committed data instead."
fi

echo
echo "ready. Read CLAUDE.md first — the scoring rules are frozen and the"
echo "conventions there are not guessable from the code."
