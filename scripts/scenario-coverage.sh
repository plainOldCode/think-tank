#!/bin/sh
# Reprint scenario-only coverage of turbo/. Does not edit source or turbo/COVERAGE.md.
# The server runs in a thread of this process, so one coverage run measures it.
set -eu
cd "$(dirname "$0")/.."
data="${TMPDIR:-/tmp}/turbo-scenario-coverage"
rm -rf "$data"
mkdir -p "$data"
python3 -m coverage run --branch \
  --data-file="$data/scenarios" \
  --source=turbo \
  --omit='*/turbo/scenarios/*' \
  -m turbo.scenarios run turbo/scenarios
python3 -m coverage report --data-file="$data/scenarios" --show-missing
echo
echo "Unit tests, for the only-unit-tested column in turbo/COVERAGE.md:"
python3 -m coverage run --branch \
  --data-file="$data/units" \
  --source=turbo \
  --omit='*/turbo/scenarios/*' \
  -m pytest tests/test_turbo.py tests/test_turbo_runner.py -q
echo
echo "TURBO-UNCOVERED marks: $(grep -h TURBO-UNCOVERED turbo/*.py | wc -l | tr -d ' ')"
