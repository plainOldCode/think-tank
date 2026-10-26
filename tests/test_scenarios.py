"""The virtual scenario set runs through a real turbo server."""

import json
from pathlib import Path

from turbo.scenarios.engine import run_scenarios

ROOT = Path(__file__).resolve().parents[1] / "turbo" / "scenarios"


def test_scenario_set():
    report = run_scenarios([ROOT])
    failed = [item for item in report["scenarios"] if not item["ok"]]
    assert not failed, json.dumps(failed, indent=2)
    assert report["passed"] == len(list(ROOT.glob("*.json")))
