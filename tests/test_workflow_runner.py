"""runner-pick 셸을 두 이벤트에 대해 실제 실행해 fromJSON 소비 가능성 검증 (리뷰 #4)."""
import json
import os
from pathlib import Path
import subprocess

import pytest


WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_runner_output_can_be_consumed_by_fromjson(tmp_path, event):
    text = WORKFLOW.read_text()
    assert "fromJSON(needs.runner-pick.outputs.runner)" in text
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.lstrip().startswith('if [ "${{ github.event_name }}"'))
    end = next(i for i, l in enumerate(lines) if i > start and l.lstrip().startswith("fi >>"))
    script = "\n".join(lines[start:end + 1])
    script = script.replace("${{ github.event_name }}", event)
    output_file = tmp_path / "output"
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, env={**os.environ, "GITHUB_OUTPUT": str(output_file)})
    assert result.returncode == 0, result.stderr
    value = output_file.read_text().strip().split("=", 1)[1]
    try:
        runner = json.loads(value)
    except json.JSONDecodeError:
        pytest.fail(f"{event}: runner-pick emitted non-JSON {value!r} for fromJSON")
    labels = [runner] if isinstance(runner, str) else runner
    assert isinstance(labels, list)
    if event == "pull_request":
        assert labels == ["ubuntu-latest"]
    else:
        assert "m2max" in labels
