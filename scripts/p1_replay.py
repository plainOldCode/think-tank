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

VARIANTS = (
    {"id": "A-strict", "exempt": "none", "kind_from": "method"},
    {"id": "B-figma-ui", "exempt": "figma-ui", "kind_from": "method"},
    {"id": "S-no-red", "exempt": "no-red", "kind_from": "method"},
    {"id": "L-level", "exempt": "no-red", "kind_from": "level"},
)


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


def verify_kind(card: dict, kind_from: str) -> str:
    """Map metadata onto a turbo verify kind. The claimed level is not trusted
    when kind_from is method: a planned card stays planned."""
    level = card.get("verification_level")
    if kind_from == "level":
        return {
            "live-verified": "live",
            "unit-test-verified": "unit",
            "type-check-only": "type",
        }.get(level, "planned")
    if card.get("method") == "tdd":
        if level == "live-verified":
            return "live"
        if level == "type-check-only":
            return "type"
        return "unit"
    return "planned"


def is_exempt(card: dict, mode: str, figma_ids: set[str]) -> bool:
    if mode == "none":
        return False
    if mode == "figma-ui":
        # The baseline has no title, label, or path. Only an explicit id list
        # can mark a card. An empty list matches nothing.
        return card["legacy_id"] in figma_ids
    if mode == "no-red":
        return not card.get("has_red_command")
    raise SystemExit("unknown exempt mode: " + mode)


def scope_for(card: dict, *, exempt: bool, kind_from: str) -> dict:
    # has_red_command false means there is no failing-on-base scenario.
    # The scenario key is omitted. prove() then requires exempt.
    return {
        "paths": ["replay/"],
        "forbidden": [],
        "verify": [{
            "cmd": "replay-verify",
            "kind": verify_kind(card, kind_from),
            "timeout_s": 30,
        }],
        "exempt": exempt,
    }


def sha_of(legacy_id: str) -> str:
    return hashlib.sha256(b"p1-replay:" + legacy_id.encode("utf-8")).hexdigest()[:40]


def pr_for(card: dict, sha: str) -> dict:
    # No real PR number. submit() only requires an object.
    number = None
    if card.get("delivery_mode") == "pr":
        number = None
    return {"provider": "github", "url": "", "number": number, "base": "main", "sha": sha}


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


def replay_one(card: dict, *, exempt_mode: str, kind_from: str, figma_ids: set[str]) -> dict:
    exempt = is_exempt(card, exempt_mode, figma_ids)
    kind = verify_kind(card, kind_from)
    sha = sha_of(card["legacy_id"])
    with tempfile.TemporaryDirectory(prefix="p1-replay-") as tmp:
        store = open_store(tmp)
        reviewer = card.get("reviewer")
        if isinstance(reviewer, str) and reviewer.strip():
            register_actor(store, reviewer, "probe", None, HUMAN)
        created = create_card(
            store, HUMAN, card["legacy_id"], "replay", "replay", HARNESS,
            scope_for(card, exempt=exempt, kind_from=kind_from), CLOCK,
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
                ["replay/card.txt"], token, verify_stub(card), CLOCK,
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
            "class": "exempt" if exempt and outcome == "accepted" else outcome,
            "gate": gate,
            "reason": reason,
            "state": final["state"],
            "verify_level": final.get("verify_level") or "",
            "verify_kind": kind,
            "exempt_applied": exempt,
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


def summarize(rows: list[dict], variant: dict, figma_ids: set[str]) -> dict:
    classes = Counter(row["class"] for row in rows)
    reasons = Counter((row["gate"], row["reason"]) for row in rows if row["class"] == "rejected")
    top = [
        {"gate": gate, "reason": reason, "count": count}
        for (gate, reason), count in reasons.most_common(3)
    ]
    return {
        "variant": variant["id"],
        "exempt": variant["exempt"],
        "kind_from": variant["kind_from"],
        "figma_ids": len(figma_ids),
        "cards": len(rows),
        "accepted": classes.get("accepted", 0) + classes.get("exempt", 0),
        "accepted_via_exempt": classes.get("exempt", 0),
        "rejected": classes.get("rejected", 0),
        "would_ask_human": classes.get("would-ask-human", 0),
        "exempt_applied": sum(1 for row in rows if row["exempt_applied"]),
        "notifications_awaiting_human": sum(row["notifications_awaiting_human"] for row in rows),
        "notifications_stuck": sum(row["notifications_stuck"] for row in rows),
        "rework_claim_count_2": sum(1 for row in rows if row["claim_count"] == 2),
        "turbo_rc_count": sum(row["rc_count"] for row in rows),
        "top_rejections": top,
    }


def write_report(path: Path, summaries: list[dict]) -> None:
    lines = [
        "# P1 offline simulation",
        "",
        "Verification level: planned-only. This is a simulation. It does not re-execute the work, and it does not call the live board, a harness CLI, GitHub, or Telegram.",
        "",
        "The simulator is `scripts/p1_replay.py`. It is outside the `turbo` package, so `scripts/scenario-coverage.sh` does not measure it. The gates it calls are `turbo.service` and `turbo.proof`.",
        "",
        "Input: `turbo/sim/p1_baseline_150.json`, 150 done cards, metadata only.",
        "",
        "## Headline",
        "",
        "| Variant | Accepted | Rejected | Ask human | Exempt applied | Awaiting-human pings | Stuck pings |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            "| {variant} | {accepted} | {rejected} | {would_ask_human} | {exempt_applied} | {notifications_awaiting_human} | {notifications_stuck} |".format(**item)
        )
    lines += ["", "## Top rejection reasons", ""]
    for item in summaries:
        lines.append(f"### {item['variant']}")
        lines.append("")
        if not item["top_rejections"]:
            lines.append("No rejections.")
        for reason in item["top_rejections"]:
            lines.append(f"- {reason['count']} `{reason['gate']}`: {reason['reason']}")
        lines.append("")
        lines.append(
            f"Main rework (`claim_count` 2): {item['rework_claim_count_2']}. "
            f"Turbo `rc_count` summed after the real verify gate: {item['turbo_rc_count']}. "
            f"Notification total if both triggers are on: "
            f"{item['notifications_awaiting_human'] + item['notifications_stuck']}."
        )
        lines.append("")
    lines += [
        "## What each variant changes",
        "",
        "- `A-strict`: no card is exempt. Verify kind comes from `method`. A planned card stays kind `planned` even when main recorded `live-verified`.",
        "- `B-figma-ui`: the same as A, except cards in `--figma-ids` are `scope.exempt`. This baseline has no title, label, or path, and the id list shipped with the sim is empty, so zero cards match.",
        "- `S-no-red`: every card with `has_red_command` false is exempt. This is a sensitivity bound, not an identification of figma or UI cards. Kind still comes from `method`, so the planned-plus-passed contradiction is the next gate.",
        "- `L-level`: the same exemption, but verify kind comes from main's `verification_level`. Cards main called live-verified or unit-test-verified can pass verify. The evidence-only epic then reaches `merge` without an approve.",
        "",
        "## Assumptions",
        "",
        "- No scenario file is written, because `has_red_command` is false on every card. `prove` rejects a non-exempt card before verify runs.",
        "- `result=passed` makes the in-process verify stub return exit 0. The stub does not execute a command. `level_of` inside `submit` decides the level from the kind, not from main's recorded level.",
        "- Changed path is `replay/card.txt` under scope `replay/`. The baseline has no paths.",
        "- Title and body stored in the temp database are the legacy id and the word `replay`. They are not the private card text, and they are not written to the output.",
        "- Harness is `opencode` because the assignee name is an opencode worker. The replay does not spawn it. OpenCode stays handoff-only.",
        "- A missing assignee is still claimed by the replay runner, so a later gate can run. Main's epic was not claimed.",
        "- `delivery_mode=pr` becomes a pr object with `number` null. No PR number from main is stored.",
        "- Independent review maps the reviewer name to a probe actor and calls `verdict(approve)` then `merge`. Evidence-only does not call approve.",
        "- `claim_count` is reported as main's rework count. It is not turned into a question. `would-ask-human` is only the real `awaiting-human` state. Nothing in the metadata is a question, so that count stays 0.",
        "- Awaiting-human pings are the planned Hermes trigger. Stuck pings are the optional trigger: `metrics().stuck` with an empty live set, one per card left `working`. A failed `submit` rolls back, so the card stays `working` and counts as stuck. No message is sent.",
        "",
        "## Limitations",
        "",
        "The baseline cannot say which cards are figma or UI sync. Variant B therefore cannot show that sensitivity. Variant S shows the upper bound of exempting every card that has no red command.",
        "",
        "Parent links are not a turbo field. The epic is replayed as its own card.",
        "",
        "This does not compare diffs, tokens, or lead time. Those fields are not in the baseline.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p1_replay")
    parser.add_argument("--baseline", type=Path, default=Path("turbo/sim/p1_baseline_150.json"))
    parser.add_argument("--out", type=Path, default=Path("turbo/sim/out"))
    parser.add_argument("--report", type=Path, default=Path("turbo/sim/P1_SIMULATION.md"))
    parser.add_argument("--figma-ids", type=Path, default=None)
    args = parser.parse_args(argv)
    cards = load_baseline(args.baseline)
    figma_ids: set[str] = set()
    if args.figma_ids is not None:
        figma_ids = {line.strip() for line in args.figma_ids.read_text(encoding="utf-8").splitlines() if line.strip()}
    args.out.mkdir(parents=True, exist_ok=True)
    summaries = []
    for variant in VARIANTS:
        rows = [
            replay_one(card, exempt_mode=variant["exempt"], kind_from=variant["kind_from"], figma_ids=figma_ids)
            for card in cards
        ]
        summary = summarize(rows, variant, figma_ids)
        summaries.append(summary)
        payload = {"summary": summary, "cards": rows}
        (args.out / f"{variant['id']}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(variant["id"], summary["accepted"], summary["rejected"], summary["would_ask_human"], summary["exempt_applied"])
    write_report(args.report, summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
