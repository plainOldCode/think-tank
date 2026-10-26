#!/usr/bin/env python3
"""Prove a scenario fails when one guard is removed, then restore the source.

Each entry names a raise (or a return) and the scenario that should reject it.
The script replaces that line with a no-op, runs that one scenario, and expects
a failure. The tree is restored even when a check does not fail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, text that identifies the line, replacement, scenario, note)
# `which` selects among identical lines, top to bottom.
CHECKS: list[tuple[str, str, str, str, int]] = [
    # A short nonce executes the length check in _eq. On this Python,
    # hmac.compare_digest also returns False for unequal lengths, so deleting
    # only that early return does not open the ack. The raise below is the
    # rejection the scenario proves.
    ("turbo/gate.py", "nonce does not match this claim", "pass", "17-wrong-nonce.json", 0),
    ("turbo/gate.py", "nonce already used", "pass", "18-nonce-reused.json", 0),
    ("turbo/gate.py", "card changed after ack", "pass", "19-digest-mismatch.json", 0),
    ("turbo/service.py", "runner affinity does not include", "pass", "20-harness-affinity.json", 0),
    ("turbo/service.py", "the first actor must be a human", "pass", "21-first-actor.json", 0),
    ("turbo/service.py", "only the claimed agent can ack", "pass", "22-claimed-agent-only.json", 0),
    ("turbo/service.py", "only the claimed agent can leave a question", "pass", "22-claimed-agent-only.json", 0),
    ("turbo/service.py", "only the claimed agent can mark the tree ready", "pass", "22-claimed-agent-only.json", 0),
    ("turbo/service.py", "only the assignee runner can close the turn", "pass", "23-assignee-runner-only.json", 0),
    ("turbo/service.py", "only the assignee runner records the session", "pass", "23-assignee-runner-only.json", 0),
    ("turbo/service.py", "only the assignee runner can resume", "pass", "23-assignee-runner-only.json", 0),
    ("turbo/service.py", "only the assignee runner can submit", "pass", "23-assignee-runner-only.json", 0),
    ("turbo/service.py", "resume token does not match", "pass", "24-resume-token.json", 0),
    ("turbo/service.py", "has not marked the tree ready", "pass", "25-submit-before-ready.json", 0),
    ("turbo/service.py", "sha must be the full", "pass", "25-submit-before-ready.json", 0),
    ("turbo/service.py", "review resumes only after request-changes", "pass", "26-review-without-changes.json", 0),
    ("turbo/store.py", "unknown actor", "pass", "27-unknown-actor-missing-card.json", 0),
    ("turbo/store.py", "card not found", "pass", "27-unknown-actor-missing-card.json", 0),
    ("turbo/service.py", "verdict must be approve or request-changes", "pass", "28-verdict-shape.json", 0),
    ("turbo/service.py", "verdict belongs on a card in review", "pass", "28-verdict-shape.json", 0),
    ("turbo/service.py", "can no longer be edited", "pass", "29-edit-locked.json", 0),
    ("turbo/service.py", "ack is not open in this state", "pass", "29-edit-locked.json", 0),
    ("turbo/scope.py", "scope must be an object", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "scope.paths must be", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "scope.forbidden must be", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "scope.verify must be", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "needs a cmd string", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "verify kind must be", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "timebox_s must be", "pass", "30-scope-shape.json", 0),
    ("turbo/service.py", "title is required", "pass", "30-scope-shape.json", 0),
    ("turbo/service.py", "body is required", "pass", "30-scope-shape.json", 0),
    ("turbo/service.py", "contract is required", "pass", "30-scope-shape.json", 0),
    ("turbo/scope.py", "return fnmatch.fnmatch(path, rule)", "return False", "31-glob-rules.json", 0),
    ("turbo/scope.py", "changed_paths must list", "pass", "32-empty-changed-paths.json", 0),
    ("turbo/scope.py", 'return "planned-only"', 'return "unit-test-verified"', "33-planned-only.json", 0),
    ("turbo/scope.py", 'return "live-verified"', 'return "failed"', "34-live-verified.json", 0),
    ("turbo/service.py", 'raise TurboError(422, "unknown harness")', "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "unknown harness:", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "actor name is required", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "role must be human", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "harnesses must be a list of strings", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "at least one harness", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "only a runner has harnesses", "pass", "35-unknown-harness.json", 0),
    ("turbo/service.py", "actor already exists", "pass", "35-unknown-harness.json", 0),
    ("turbo/proc.py", "merge requires the submitted sha", "pass", "36-empty-sha.json", 0),
    ("turbo/service.py", "merge requires the submitted sha", "pass", "36-empty-sha.json", 0),
    ("turbo/service.py", "blocked_question is required", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "blocked_options must be 2 to 4 strings", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "blocked_options must be 2 to 4 strings", "pass", "37-question-shape.json", 1),
    ("turbo/service.py", "blocked_options must be unique", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "blocked_default must be one of the options", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "choice is required", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "choice must be one of the options", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "not waiting for a person", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "already has an answer", "pass", "37-question-shape.json", 0),
    ("turbo/service.py", "only while working", "pass", "45-actor-ready-state.json", 0),
    ("turbo/service.py", "a turn is already running", "pass", "45-actor-ready-state.json", 0),
    ("turbo/service.py", "resume is not waiting on this card", "pass", "45-actor-ready-state.json", 0),
    ("turbo/service.py", 'state.require(card["state"], "merged")', "pass", "29-edit-locked.json", 0),
    ("turbo/service.py", 'state.require(card["state"], "submit")', "pass", "29-edit-locked.json", 0),
    ("turbo/service.py", "merge requires a passed server verify", "pass", "34-live-verified.json", 0),
    ("turbo/service.py", "body is required", "pass", "30-scope-shape.json", 1),
    ("turbo/service.py", "contract is required", "pass", "30-scope-shape.json", 1),
    ("turbo/service.py", "merge requires an approve verdict", "pass", "26-review-without-changes.json", 0),
    ("turbo/service.py", "only the assignee runner can merge", "pass", "08-merge-authority.json", 0),
    ("turbo/scope.py", 'return "type-check-only"', 'return "planned-only"', "46-type-check-only.json", 0),
    (
        "turbo/service.py",
        'elif card["state"] == "review" and card["verdict"] == "request-changes":',
        'elif card["state"] == "review":',
        "47-approved-not-resumed.json",
        0,
    ),
    ("turbo/drivers.py", 'if harness == "codex":', "if False:", "48-codex-resume-fork.json", 0),
    ("turbo/drivers.py", 'if harness == "codex":', "if False:", "48-codex-resume-fork.json", 1),
    ("turbo/drivers.py", 'if harness == "codex":', "if False:", "48-codex-resume-fork.json", 2),
    ("turbo/drivers.py", "return '\"' + escaped + '\"'", "return text", "48-codex-resume-fork.json", 0),
    ("turbo/drivers.py", "return str(data[\"thread_id\"])", "pass", "48-codex-resume-fork.json", 0),
    ("turbo/drivers.py", "return None", 'return "forced"', "48-codex-resume-fork.json", 0),
    # which=2 skips the word "continued" in the module docstring and the blank-line continue.
    ("turbo/drivers.py", "continue", 'raise RuntimeError("bad json")', "48-codex-resume-fork.json", 2),
    ("turbo/runner.py", 'self._fallback(card, ent, "fork")', "pass", "48-codex-resume-fork.json", 0),
    ("turbo/drivers.py", 'if harness == "grok":', "if False:", "49-grok-resume-fork.json", 0),
    ("turbo/drivers.py", 'if harness == "grok":', "if False:", "49-grok-resume-fork.json", 1),
    ("turbo/drivers.py", 'if harness == "grok":', "if False:", "49-grok-resume-fork.json", 2),
    ("turbo/drivers.py", "return str(value) if value else None", "return None", "49-grok-resume-fork.json", 1),
    ("turbo/drivers.py", 'if harness == "cursor":', "if False:", "50-cursor-create-chat.json", 0),
    ("turbo/drivers.py", 'if harness == "cursor":', "if False:", "50-cursor-create-chat.json", 1),
    ("turbo/drivers.py", 'return ["agent", "-p", "--resume", session_id, "--trust", prompt]', 'return ["agent", "create-chat"]', "50-cursor-create-chat.json", 0),
    ("turbo/runner.py", 'phase = "create-chat"', "pass", "50-cursor-create-chat.json", 0),
    ("turbo/runner.py", "if code != 0:", "if False:", "51-cursor-create-chat-fails.json", 0),
    ("turbo/drivers.py", 'return Plan("webhook", None, prompt, None, status)', 'return Plan("handoff", None, prompt, None, status)', "52-grok-bot-webhook.json", 0),
    ("turbo/drivers.py", 'return Plan("webhook", None, prompt, session_id, status)', 'return Plan("handoff", None, prompt, None, status)', "52-grok-bot-webhook.json", 0),
    ("turbo/runner.py", 'message = message.replace(secret, "[redacted]")', "pass", "52-grok-bot-webhook.json", 0),
    ("turbo/drivers.py", 'return Plan("handoff", None, prompt, None, status)', 'return Plan("start", ["claude", "-p", prompt], prompt, None, "confirmed")', "53-hermes-handoff.json", 0),
]


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _find(lines: list[str], needle: str, which: int) -> int:
    hits = [i for i, line in enumerate(lines) if needle in line and not line.strip().startswith("#")]
    if len(hits) <= which:
        raise SystemExit(f"found {len(hits)} lines containing {needle!r}, wanted index {which}")
    return hits[which]


def _apply(path: Path, needle: str, replacement: str, which: int) -> str:
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    index = _find(lines, needle, which)
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    lines[index] = f"{indent}{replacement}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return original


def _run(scenario: str) -> bool:
    # A restored file can keep the previous mtime, so Python would run the last
    # mutation's bytecode. Drop the cache before every check.
    cache = ROOT / "turbo" / "__pycache__"
    if cache.is_dir():
        for item in cache.glob("*.pyc"):
            item.unlink()
    result = subprocess.run(
        [sys.executable, "-m", "turbo.scenarios", "run", f"turbo/scenarios/{scenario}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def main() -> int:
    failed_open: list[str] = []
    print(f"{'guard':<52} {'scenario':<36} result")
    checks = CHECKS
    for path, needle, replacement, scenario, which in checks:
        file = ROOT / path
        original = _apply(file, needle, replacement, which)
        try:
            broke = _run(scenario)
        finally:
            file.write_text(original, encoding="utf-8")
        mark = "FAILS" if broke else "STILL PASSES"
        label = needle if len(needle) <= 50 else needle[:47] + "..."
        print(f"{label:<52} {scenario:<36} {mark}")
        if not broke:
            failed_open.append(f"{path}: {needle} ({scenario})")
    print(f"{len(checks) - len(failed_open)} / {len(checks)} mutations failed a scenario")
    if failed_open:
        print("guards that stayed open:")
        for item in failed_open:
            print(" ", item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
