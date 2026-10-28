#!/bin/sh
# Scenario-only coverage of turbo/. Fails if statements or branches drop below 100%.
# Child processes (python -m turbo, python -m turbo.runner) are measured too.
# The scenario engine and tests are omitted. Nothing else is.
set -eu
cd "$(dirname "$0")/.."
data="${TMPDIR:-/tmp}/turbo-scenario-coverage"
rm -rf "$data"
mkdir -p "$data"
cat > "$data/coveragerc" << EOF
[run]
branch = True
parallel = True
source = turbo
omit = */turbo/scenarios/*
data_file = $data/scenarios
EOF
export COVERAGE_PROCESS_START="$data/coveragerc"
python3 -m coverage run --rcfile="$data/coveragerc" -m turbo.scenarios run turbo/scenarios
python3 -m coverage combine --rcfile="$data/coveragerc"
python3 -m coverage report --rcfile="$data/coveragerc" --show-missing --fail-under=100
echo
echo "Unit tests, not part of the scenario-only gate:"
python3 -m coverage run --branch \
  --data-file="$data/units" \
  --source=turbo \
  --omit='*/turbo/scenarios/*' \
  -m pytest tests/test_turbo.py tests/test_turbo_runner.py -q
echo
echo "TURBO-UNCOVERED marks: $(grep -h TURBO-UNCOVERED turbo/*.py 2>/dev/null | wc -l | tr -d ' ')"
