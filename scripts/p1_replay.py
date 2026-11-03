#!/usr/bin/env python3
"""Offline P1 shadow replay. No harness, network, Telegram, or live board.

The simulator lives in scripts/ so scenario coverage of the turbo package
does not measure it. It calls turbo.service, turbo.scope, and turbo.gate
against a temporary sqlite file. It does not reimplement those rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from turbo.errors import TurboError
from turbo.submitgate import KINDS
from turbo.gate import digest
from turbo.service import (
    ack,
    claim,
    create_card,
    merge,
    metrics,
    ready,
    register_actor,
    submit,
    verdict,
)
from turbo.store import Store

CLOCK = lambda: 1_700_000_000.0
HUMAN = "ada"
RUNNER = "run-1"
AGENT = "worker"
PROBE = "probe"
HARNESS = "opencode"
FORBIDDEN_KEYS = {
    "title", "body", "repo", "pr", "pr_number", "label", "labels",
    "comments", "comment",
}

# The previous offline run, before these gates, rejected every card at red-green.
BEFORE = {
    "accepted": 0,
    "rejected": 150,
    "would_ask_human": 0,
    "stuck": 150,
    "top": "150 red-green: scope.scenario is required unless scope.exempt is true",
}

AUTO_REASONS = {"evidence_missing", "merge_conflict", "regression", "head_sha_mismatch"}


def load_baseline(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cards = data["cards"]
    if not isinstance(cards, list) or len(cards) != 150:
        raise SystemExit("baseline must contain 150 cards")
    for card in cards:
        hit = FORBIDDEN_KEYS & set(card)
        if hit:
            raise SystemExit("baseline has forbidden fields: " + ", ".join(sorted(hit)))
    return cards


def card_kind(card: dict) -> str:
    kind = card.get("card_kind") or "bug"
    if kind in KINDS:
        return kind
    # "other" is not a turbo kind. Treat it as feature so red-green still applies.
    return "feature"


def verify_kind(card: dict, kind: str) -> str:
    level = card.get("verification_level")
    mapped = {
        "live-verified": "live",
        "unit-test-verified": "unit",
        "type-check-only": "type",
    }.get(level, "planned")
    if kind == "visual":
        # Main already ran CI. planned means there was no red-first test.
        return mapped
    if card.get("method") == "tdd":
        return mapped
    return "planned"


def scope_for(card: dict, kind: str) -> tuple[dict, list[str]]:
    reasons = set(card.get("rejection_reasons") or [])
    evidence: list[str] = []
    changed = ["replay/card.txt"]
    if kind == "visual" or "evidence_missing" in reasons:
        if "evidence_missing" in reasons:
            evidence = ["replay/missing.txt"]
        elif kind == "visual":
            evidence = ["replay/card.txt"]
    return {
        "paths": ["replay/"],
        "forbidden": [],
        "verify": [{
            "cmd": "replay-verify",
            "kind": verify_kind(card, kind),
            "timeout_s": 30,
        }],
        "exempt": False,
        "evidence": evidence,
    }, changed


def sha_of(legacy_id: str) -> str:
    return hashlib.sha256(b"p1-replay:" + legacy_id.encode("utf-8")).hexdigest()[:40]


def pr_for(card: dict, sha: str) -> dict:
    reasons = set(card.get("rejection_reasons") or [])
    # No real PR number. submit() only requires an object.
    pr: dict = {"provider": "github", "base": "main", "sha": sha, "number": None}
    if card.get("delivery_mode") == "pr":
        pr["mergeable"] = "merge_conflict" not in reasons
        pr["head_sha"] = ("b" * 40) if "head_sha_mismatch" in reasons else sha
    if "regression" in reasons:
        pr["rebase_paths"] = ["outside/other.txt"]
    return pr


def verify_stub(card: dict):
    def run(cmd: str, cwd, timeout) -> tuple[int, str]:
        del cwd, timeout
        if cmd != "replay-verify":
            return 127, "unexpected cmd"
        if card.get("result") == "passed":
            return 0, "passed"
        return 1, card.get("result") or "failed"
    return run


def gate_of(message: str) -> str:
    if "scenario" in message:
        return "red-green"
    if "evidence" in message or message.startswith("visual work"):
        return "evidence"
    if "not mergeable" in message:
        return "mergeability"
    if "head sha" in message:
        return "head-sha"
    if "epic" in message:
        return "epic"
    if "re-review" in message:
        return "rereview"
    if "approve requires" in message or message.startswith("verdict "):
        return "verdict"
    if message.startswith("merge ") or message.startswith("cannot move"):
        return "merge"
    if "scope" in message or "paths outside" in message or "changed_paths" in message:
        return "scope"
    return "submit"


def open_store(directory: str) -> Store:
    store = Store(str(Path(directory) / "turbo.db"))
    register_actor(store, HUMAN, "human", None, None)
    register_actor(store, RUNNER, "runner", [HARNESS], HUMAN)
    register_actor(store, AGENT, "agent", None, HUMAN)
    register_actor(store, PROBE, "probe", None, HUMAN)
    return store


def notifications(store: Store, card_id: str) -> dict:
    card = store.get(card_id)
    awaiting = 1 if card["state"] == "awaiting-human" else 0
    stuck_ids = {item["id"] for item in metrics(store, None, set(), CLOCK())["stuck"]}
    stuck = 1 if card_id in stuck_ids else 0
    return {"awaiting_human": awaiting, "stuck": stuck}


def replay_one(card: dict) -> dict:
    kind = card_kind(card)
    scope, changed = scope_for(card, kind)
    sha = sha_of(card["legacy_id"])
    with tempfile.TemporaryDirectory(prefix="p1-replay-") as tmp:
        store = open_store(tmp)
        reviewer = card.get("reviewer")
        if isinstance(reviewer, str) and reviewer.strip():
            register_actor(store, reviewer, "probe", None, HUMAN)
        created = create_card(
            store, HUMAN, card["legacy_id"], "replay", "replay", HARNESS,
            scope, CLOCK, kind,
        )
        card_id = created["id"]
        claimed = claim(store, card_id, RUNNER, AGENT, CLOCK)
        token = ack(
            store, card_id, AGENT,
            digest(claimed["body"], claimed["contract"], claimed["contract_version"], claimed["scope"]),
            claimed["nonce"], CLOCK,
        )["work_token"]
        ready(store, card_id, AGENT, "", CLOCK)
        outcome = "rejected"
        gate = "submit"
        reason = ""
        try:
            submit(
                store, card_id, RUNNER, sha, pr_for(card, sha),
                changed, token, verify_stub(card), CLOCK,
            )
        except TurboError as exc:
            gate = gate_of(exc.message)
            reason = exc.message
        else:
            current = store.get(card_id)
            if current["verdict"] == "request-changes" or current["verify_level"] in {"", "failed", "planned-only"}:
                try:
                    verdict(store, card_id, PROBE, "approve", "", CLOCK)
                except TurboError as exc:
                    gate = gate_of(exc.message)
                    reason = exc.message
                else:
                    outcome = "accepted"
                    gate = ""
                    reason = ""
            elif card.get("approval_path") != "independent-review":
                try:
                    merge(store, card_id, PROBE, CLOCK)
                except TurboError as exc:
                    gate = gate_of(exc.message)
                    reason = exc.message
                else:
                    outcome = "accepted"
                    gate = ""
                    reason = ""
            else:
                actor = reviewer if isinstance(reviewer, str) and reviewer.strip() else PROBE
                try:
                    verdict(store, card_id, actor, "approve", "", CLOCK)
                    merge(store, card_id, actor, CLOCK)
                except TurboError as exc:
                    gate = gate_of(exc.message)
                    reason = exc.message
                else:
                    outcome = "accepted"
                    gate = ""
                    reason = ""
        final = store.get(card_id)
        if final["state"] == "awaiting-human":
            outcome = "would-ask-human"
        notes = notifications(store, card_id)
        return {
            "legacy_id": card["legacy_id"],
            "parent_id": card.get("parent_id"),
            "class": outcome,
            "gate": gate,
            "reason": reason,
            "state": final["state"],
            "verify_level": final.get("verify_level") or "",
            "verify_kind": scope["verify"][0]["kind"],
            "kind": kind,
            "exempt_applied": False,
            "rejection_reasons": list(card.get("rejection_reasons") or []),
            "review_request_changes": card.get("review_request_changes") or 0,
            "rc_count": final.get("rc_count") or 0,
            "claim_count": card.get("claim_count"),
            "method": card.get("method"),
            "main_level": card.get("verification_level"),
            "result": card.get("result"),
            "has_red_command": card.get("has_red_command"),
            "approval_path": card.get("approval_path"),
            "delivery_mode": card.get("delivery_mode"),
            "merged_on_main": card.get("merged"),
            "notifications_awaiting_human": notes["awaiting_human"],
            "notifications_stuck": notes["stuck"],
        }


def summarize(rows: list[dict]) -> dict:
    classes = Counter(row["class"] for row in rows)
    reasons = Counter((row["gate"], row["reason"]) for row in rows if row["class"] == "rejected")
    top = [
        {"gate": gate, "reason": reason, "count": count}
        for (gate, reason), count in reasons.most_common()
    ]
    rc_cards = [row for row in rows if row["review_request_changes"]]
    caught = [
        row for row in rc_cards
        if AUTO_REASONS & set(row["rejection_reasons"]) and row["class"] == "rejected" and row["state"] != "review"
    ]
    return {
        "cards": len(rows),
        "accepted": classes.get("accepted", 0) + classes.get("exempt", 0),
        "rejected": classes.get("rejected", 0),
        "would_ask_human": classes.get("would-ask-human", 0),
        "notifications_stuck": sum(row["notifications_stuck"] for row in rows),
        "notifications_awaiting_human": sum(row["notifications_awaiting_human"] for row in rows),
        "rework_claim_count_2": sum(1 for row in rows if row["claim_count"] == 2),
        "measured_rc_cards": len(rc_cards),
        "measured_rc_events": sum(row["review_request_changes"] for row in rc_cards),
        "caught_cards": len(caught),
        "caught_events": sum(row["review_request_changes"] for row in caught),
        "top_rejections": top,
    }


def write_report(path: Path, summary: dict) -> None:
    lines = [
        "# P1 offline simulation",
        "",
        "Verification level: planned-only. This is a simulation. It does not re-execute the work, and it does not call the live board, a harness CLI, GitHub, or Telegram.",
        "",
        "The simulator is `scripts/p1_replay.py`, outside the covered `turbo` package. It calls the real submit, verdict, and merge gates.",
        "",
        "Input: `turbo/sim/p1_baseline_150.json`, 150 done cards, metadata only, including `card_kind` and `rejection_reasons`.",
        "",
        "## Before and after",
        "",
        "Before is the previous simulation, when every card was treated as a behavior card with no scenario. After is this run. Stuck is `metrics().stuck` with an empty live set. Ask-human is the `awaiting-human` state.",
        "",
        "| | Accepted | Rejected | Ask human | Stuck |",
        "|---|---:|---:|---:|---:|",
        f"| Before | {BEFORE['accepted']} | {BEFORE['rejected']} | {BEFORE['would_ask_human']} | {BEFORE['stuck']} |",
        f"| After | {summary['accepted']} | {summary['rejected']} | {summary['would_ask_human']} | {summary['notifications_stuck']} |",
        "",
        f"Before, the only rejection was {BEFORE['top']}.",
        "",
        "## After, by gate",
        "",
        "These counts are the gate the real code fired. They are not a second copy of the measured reason labels.",
        "",
    ]
    for reason in summary["top_rejections"]:
        lines.append(f"- {reason['count']} `{reason['gate']}`: {reason['reason']}")
    lines += [
        "",
        "## Request-changes caught before review",
        "",
        f"Measured on main: {summary['measured_rc_events']} request-changes across {summary['measured_rc_cards']} cards.",
        "",
        "Modeled: a card is caught before review when its measured reasons include evidence missing, a merge conflict, a regression outside scope, or a head sha mismatch, and this replay rejected it before `review`. Pixel and report mismatches are not an automatic gate.",
        "",
        f"Caught: {summary['caught_events']} of {summary['measured_rc_events']} request-changes, on {summary['caught_cards']} of {summary['measured_rc_cards']} cards. A card with several reasons hits the first gate only. Evidence is checked before mergeability and head sha, so a conflict that also missed evidence is counted under evidence. The three measured regressions were on cards that already failed one of those earlier gates.",
        "",
        "The 4 post-merge defects are measured as visual. None of them is a functional or API failure. A figma pixel mismatch still needs a person.",
        "",
        "## Assumptions",
        "",
        "- `card_kind` `visual` skips the scenario gate. `other` is mapped to `feature`, so it still needs a scenario. The epic is submitted once and rejected; children are not replayed in one database, so automatic epic close is not part of this count.",
        "- For a visual card, CI kind comes from main's `verification_level`, because CI already ran. `method=planned` means there was no red-first test.",
        "- Evidence, mergeability, head sha, and rebase paths are not in the metadata. They are set from `rejection_reasons`: missing evidence is a path not in the commit, a merge conflict sets `mergeable` false, a head mismatch sets a different `head_sha`, and a regression sets a rebase path outside `replay/`. A visual card with none of those reasons is given an evidence path that is in the commit. That mapping is modeled.",
        "- `has_red_command` is false for every card, so no scenario file is written.",
        "- The verify stub returns exit 0 when `result` is `passed`. It does not run a command.",
        "- A rejected submit now returns the card to `open`, so it is not stuck.",
        "- No notification is sent. Awaiting-human pings are "
        f"{summary['notifications_awaiting_human']}.",
        "",
        f"Main rework (`claim_count` 2) is {summary['rework_claim_count_2']}. It is not turned into a question.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p1_replay")
    parser.add_argument("--baseline", type=Path, default=Path("turbo/sim/p1_baseline_150.json"))
    parser.add_argument("--out", type=Path, default=Path("turbo/sim/out"))
    parser.add_argument("--report", type=Path, default=Path("turbo/sim/P1_SIMULATION.md"))
    args = parser.parse_args(argv)
    cards = load_baseline(args.baseline)
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [replay_one(card) for card in cards]
    summary = summarize(rows)
    (args.out / "after.json").write_text(
        json.dumps({"summary": summary, "cards": rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for stale in ("A-strict.json", "B-figma-ui.json", "S-no-red.json", "L-level.json"):
        path = args.out / stale
        if path.exists():
            path.unlink()
    write_report(args.report, summary)
    print(summary["accepted"], summary["rejected"], summary["would_ask_human"], summary["notifications_stuck"], summary["caught_events"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
