"""State changes for one turbo database.

The runner is the only submitter. Probe and the assignee runner are the
only mergers. An agent ack is what makes a work token exist.
"""

import json

from . import gate, scope, state
from .clock import parse_stamp, stamp
from .constants import DEFAULT_ANSWER_S
from .drivers import HARNESSES
from .errors import TurboError
from .proof import prove
from .store import Store, new_id, public_card

ROLES = {"human", "runner", "agent", "probe"}
_SUMMARY = (
    "id", "title", "state", "harness", "assignee", "agent",
    "blocked_question", "blocked_options", "blocked_default", "answer",
    "answer_by", "verdict", "verify_level", "rc_count", "ready",
    "sha", "updated_at", "contract_version",
)


def _now(clock) -> str:
    return stamp(clock())


def register_actor(store: Store, name: str, role: str, harnesses: list | None,
                   by: str | None) -> dict:
    if not isinstance(name, str) or not name.strip():
        raise TurboError(422, "actor name is required")
    name = name.strip()
    if role not in ROLES:
        raise TurboError(422, "role must be human, runner, agent, or probe")
    harnesses = harnesses or []
    if not isinstance(harnesses, list) or not all(isinstance(item, str) for item in harnesses):
        raise TurboError(422, "harnesses must be a list of strings")
    unknown = [item for item in harnesses if item not in HARNESSES]
    if unknown:
        raise TurboError(422, "unknown harness: " + ", ".join(unknown))
    if role == "runner" and not harnesses:
        raise TurboError(422, "a runner needs at least one harness")
    if role != "runner" and harnesses:
        raise TurboError(422, "only a runner has harnesses")
    with store.transaction():
        if store.actor(name):
            raise TurboError(409, "actor already exists")
        if store.count_actors() == 0:
            if role != "human":
                raise TurboError(403, "the first actor must be a human")
        else:
            store.require_role(by or "", "human")
        store.insert_actor(name, role, [item for item in harnesses if isinstance(item, str)])
    return store.require_actor(name)


def create_card(store: Store, actor: str, title: str, body: str, contract: str,
                harness: str, raw_scope: object, clock) -> dict:
    store.require_role(actor, "human")
    if not isinstance(title, str) or not title.strip():
        raise TurboError(422, "title is required")
    if not isinstance(body, str) or not body.strip():
        raise TurboError(422, "body is required")
    if not isinstance(contract, str) or not contract.strip():
        raise TurboError(422, "contract is required")
    if harness not in HARNESSES:
        raise TurboError(422, "unknown harness")
    parsed = scope.parse_scope(raw_scope)
    now = _now(clock)
    card_id = new_id()
    with store.transaction():
        store.insert_card({
            "id": card_id,
            "title": title.strip(),
            "body": body.strip(),
            "contract": contract.strip(),
            "contract_version": 1,
            "scope_json": json.dumps(parsed, sort_keys=True, ensure_ascii=False),
            "harness": harness,
            "state": "open",
            "created_at": now,
            "updated_at": now,
        })
        store.add_event(card_id, "created", {"actor": actor, "harness": harness}, now)
    return _view(store, card_id, reveal=False)


def _view(store: Store, card_id: str, reveal: bool) -> dict:
    card = store.get(card_id)
    out = public_card(card, reveal_token=reveal)
    if reveal and card.get("nonce") and not card.get("nonce_used"):
        out["nonce"] = card["nonce"]
    out["events"] = store.events(card_id)
    pending = store.pending_resume(card_id)
    out["resume"] = None if not pending else {
        "token": pending["token"],
        "kind": pending["kind"],
        "prompt": pending["prompt"],
    }
    return out


def get_card(store: Store, card_id: str, actor: str | None = None) -> dict:
    reveal = False
    if actor:
        who = store.actor(actor)
        card = store.get(card_id)
        if who and who["role"] == "runner" and card["assignee"] == actor:
            reveal = True
    return _view(store, card_id, reveal)


def list_cards(store: Store, state: str | None = None, harness: str | None = None) -> dict:
    cards = []
    for card in store.list_cards(state, harness):
        cards.append({key: card.get(key) for key in _SUMMARY})
    return {"cards": cards}


def claim(store: Store, card_id: str, actor: str, agent: str, clock) -> dict:
    runner = store.require_role(actor, "runner")
    worker = store.require_role(agent, "agent")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["harness"] not in runner["harnesses"]:
            raise TurboError(409, "runner affinity does not include this harness")
        busy = store.busy_card(actor)
        if busy and busy["id"] != card_id:
            raise TurboError(409, "runner already holds " + busy["id"])
        state.require(card["state"], "working")
        nonce = gate.new_nonce()
        store.update_card(
            card_id,
            state="working",
            assignee=actor,
            agent=worker["name"],
            nonce=nonce,
            nonce_used=0,
            work_token="",
            token_digest="",
            ready=0,
            started_at=now,
            updated_at=now,
        )
        store.add_event(card_id, "claim", {"runner": actor, "agent": agent}, now)
    viewed = _view(store, card_id, reveal=False)
    viewed["nonce"] = nonce
    return viewed


def ack(store: Store, card_id: str, actor: str, given_digest: str, nonce: str, clock) -> dict:
    store.require_role(actor, "agent")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["agent"] != actor:
            raise TurboError(403, "only the claimed agent can ack")
        if card["state"] not in {"working", "question", "awaiting-human", "review"}:
            raise TurboError(409, "ack is not open in this state")
        expect = gate.check_ack(card, given_digest, nonce)
        token = gate.new_token()
        store.update_card(
            card_id,
            nonce_used=1,
            work_token=token,
            token_digest=expect,
            updated_at=now,
        )
        store.add_event(card_id, "ack", {"actor": actor, "contract_version": card["contract_version"]}, now)
    return {"work_token": token, "contract_version": card["contract_version"]}


def ask(store: Store, card_id: str, actor: str, question: str, options: list,
        default: str, clock, confirm: bool = False) -> dict:
    store.require_role(actor, "agent")
    if confirm is not True and confirm is not False:
        raise TurboError(422, "confirm must be true or false")
    if not isinstance(question, str) or not question.strip():
        raise TurboError(422, "blocked_question is required")
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise TurboError(422, "blocked_options must be 2 to 4 strings")
    if not all(isinstance(item, str) and item.strip() for item in options):
        raise TurboError(422, "blocked_options must be 2 to 4 strings")
    cleaned = [item.strip() for item in options]
    if len(set(cleaned)) != len(cleaned):
        raise TurboError(422, "blocked_options must be unique")
    if not isinstance(default, str) or default.strip() not in cleaned:
        raise TurboError(422, "blocked_default must be one of the options")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["agent"] != actor:
            raise TurboError(403, "only the claimed agent can leave a question")
        state.require(card["state"], "question")
        store.update_card(
            card_id,
            state="question",
            blocked_question=question.strip(),
            blocked_options=json.dumps(cleaned, ensure_ascii=False),
            blocked_default=default.strip(),
            confirm=1 if confirm else 0,
            answer="",
            answer_by="",
            answered_at="",
            question_at=now,
            updated_at=now,
        )
        store.add_event(card_id, "question", {
            "blocked_question": question.strip(),
            "blocked_options": cleaned,
            "blocked_default": default.strip(),
            "confirm": confirm,
        }, now)
    return _view(store, card_id, reveal=False)


def turn_closed(store: Store, card_id: str, actor: str, clock) -> dict:
    store.require_role(actor, "runner")
    now_epoch = clock()
    now = stamp(now_epoch)
    with store.transaction():
        card = store.get(card_id)
        if card["assignee"] != actor:
            raise TurboError(403, "only the assignee runner can close the turn")
        state.require(card["state"], "awaiting-human")
        deadline = stamp(now_epoch + DEFAULT_ANSWER_S)
        store.update_card(
            card_id,
            state="awaiting-human",
            default_deadline=deadline,
            updated_at=now,
        )
        store.add_event(card_id, "turn-closed", {"default_deadline": deadline}, now)
    return _view(store, card_id, reveal=False)


def _queue_resume(store: Store, card: dict, kind: str, now: str, extra: str = "") -> None:
    token = gate.new_resume_token()
    lines = [f"[tt resume] wait_token={token} kind={kind}"]
    if kind == "answer":
        lines.append(f"choice={card['answer']}")
        lines.append("next=continue")
    else:
        lines.append(f"result={card['verdict'] or 'request-changes'}")
        lines.append(f"sha={card['sha']}")
        lines.append("next=fix")
    lines.append(f"contract_version={card['contract_version']}")
    if extra:
        lines.append(extra)
    store.add_resume(card["id"], token, kind, "\n".join(lines) + "\n", now)


def answer(store: Store, card_id: str, actor: str, choice: str, clock) -> dict:
    store.require_role(actor, "human")
    if not isinstance(choice, str) or not choice.strip():
        raise TurboError(422, "choice is required")
    choice = choice.strip()
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["state"] != "awaiting-human":
            raise TurboError(409, "this card is not waiting for a person")
        if card["answer"]:
            raise TurboError(409, "this question already has an answer")
        if choice not in card["blocked_options"]:
            raise TurboError(422, "choice must be one of the options")
        store.update_card(card_id, answer=choice, answer_by=actor, answered_at=now, updated_at=now)
        card = store.get(card_id)
        _queue_resume(store, card, "answer", now)
        store.add_event(card_id, "answer", {"actor": actor, "choice": choice}, now)
    return _view(store, card_id, reveal=False)


def apply_defaults(store: Store, actor: str, clock) -> dict:
    store.require_role(actor, "runner")
    now = _now(clock)
    applied = []
    with store.transaction():
        for card in store.awaiting_deadline(now):
            if card["confirm"]:
                continue
            choice = card["blocked_default"]
            store.update_card(
                card["id"], answer=choice, answer_by="default", answered_at=now, updated_at=now,
            )
            fresh = store.get(card["id"])
            _queue_resume(store, fresh, "answer", now, extra="source=default")
            store.add_event(card["id"], "answer", {"actor": "default", "choice": choice}, now)
            applied.append(card["id"])
    return {"applied": applied}


def ready(store: Store, card_id: str, actor: str, note: str, clock) -> dict:
    store.require_role(actor, "agent")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["agent"] != actor:
            raise TurboError(403, "only the claimed agent can mark the tree ready")
        if card["state"] != "working":
            raise TurboError(409, "the tree can be marked ready only while working")
        store.update_card(card_id, ready=1, updated_at=now)
        store.add_event(card_id, "ready", {"note": (note or "")[:500]}, now)
    return _view(store, card_id, reveal=False)


def attach_session(store: Store, card_id: str, actor: str, session_id: str,
                   driver: str, mode: str, clock) -> dict:
    store.require_role(actor, "runner")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["assignee"] != actor:
            raise TurboError(403, "only the assignee runner records the session")
        payload = {"driver": driver, "session_id": session_id or "", "mode": mode}
        store.update_card(card_id, session_json=json.dumps(payload, ensure_ascii=False), updated_at=now)
        store.add_event(card_id, "session", {"driver": driver, "mode": mode, "has_id": bool(session_id)}, now)
    return _view(store, card_id, reveal=False)


def resume_started(store: Store, card_id: str, actor: str, token: str,
                   session_id: str | None, mode: str, clock) -> dict:
    store.require_role(actor, "runner")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["assignee"] != actor:
            raise TurboError(403, "only the assignee runner can resume")
        if card["state"] == "working":
            raise TurboError(409, "a turn is already running for this session")
        if card["state"] not in {"awaiting-human", "review"}:
            raise TurboError(409, "resume is not waiting on this card")
        if card["state"] == "review" and card["verdict"] != "request-changes":
            raise TurboError(409, "review resumes only after request-changes")
        row = store.resume_by_token(token or "")
        if not row or row["card_id"] != card_id or row["consumed"]:
            raise TurboError(409, "resume token does not match")
        state.require(card["state"], "working")
        session = card["session"] or {"driver": card["harness"], "session_id": "", "mode": mode}
        if session_id:
            session["session_id"] = session_id
        session["mode"] = mode
        store.consume_resume(token)
        store.update_card(
            card_id,
            state="working",
            ready=0,
            session_json=json.dumps(session, ensure_ascii=False),
            updated_at=now,
        )
        store.add_event(card_id, "resume", {"mode": mode, "kind": row["kind"]}, now)
    return _view(store, card_id, reveal=True)


def submit(store: Store, card_id: str, actor: str, sha: str, pr: dict,
           changed: list, token: str, verify, clock) -> dict:
    store.require_role(actor, "runner")
    if not isinstance(sha, str) or len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise TurboError(422, "sha must be the full 40-character commit")
    if not isinstance(pr, dict):
        raise TurboError(422, "pr must be an object")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["assignee"] != actor:
            raise TurboError(403, "only the assignee runner can submit")
        if card["state"] != "working":
            state.require(card["state"], "submit")
        if not card["ready"]:
            raise TurboError(409, "the agent has not marked the tree ready")
        gate.check_token(card, token)
        scope.check_changed(card["scope"], changed)
        state.require(card["state"], "submit")
        prove(card["scope"], changed)
        store.update_card(
            card_id,
            state="submit",
            sha=sha,
            pr_json=json.dumps(pr, ensure_ascii=False, sort_keys=True),
            updated_at=now,
        )
        store.add_event(card_id, "submit", {"sha": sha}, now)
        results = []
        logs = []
        for step in card["scope"]["verify"]:
            code, output = verify(step["cmd"], None, step.get("timeout_s"))
            results.append({"cmd": step["cmd"], "kind": step["kind"], "code": int(code)})
            logs.append(f"$ {step['cmd']}\nexit {int(code)}\n{output}")
        level = scope.level_of(card["scope"]["verify"], results)
        verdict = ""
        rc_count = card["rc_count"]
        if level in {"failed", "planned-only"}:
            verdict = "request-changes"
            rc_count += 1
        state.require("submit", "review")
        store.update_card(
            card_id,
            state="review",
            verify_level=level,
            verify_log="\n".join(logs)[-4000:],
            verdict=verdict,
            rc_count=rc_count,
            updated_at=now,
        )
        store.add_event(card_id, "verify", {"level": level, "results": results}, now)
        if verdict == "request-changes":
            fresh = store.get(card_id)
            _queue_resume(store, fresh, "verdict", now, extra=f"level={level}")
            store.add_event(card_id, "verdict", {"result": verdict, "by": "server"}, now)
    return _view(store, card_id, reveal=True)


def verdict(store: Store, card_id: str, actor: str, result: str, findings: str, clock) -> dict:
    store.require_role(actor, "probe")
    if result not in {"approve", "request-changes"}:
        raise TurboError(422, "verdict must be approve or request-changes")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["state"] != "review":
            raise TurboError(409, "verdict belongs on a card in review")
        if result == "approve" and card["verify_level"] in {"", "failed", "planned-only"}:
            raise TurboError(409, "approve requires a passed server verify")
        rc_count = card["rc_count"] + (1 if result == "request-changes" and card["verdict"] != "request-changes" else 0)
        store.update_card(card_id, verdict=result, rc_count=rc_count, updated_at=now)
        store.add_event(card_id, "verdict", {"result": result, "by": actor, "findings": (findings or "")[:500]}, now)
        if result == "request-changes" and not store.pending_resume(card_id):
            fresh = store.get(card_id)
            _queue_resume(store, fresh, "verdict", now)
    return _view(store, card_id, reveal=False)


_MERGE_LEVELS = {"live-verified", "unit-test-verified", "type-check-only"}


def merge(store: Store, card_id: str, actor: str, clock) -> dict:
    who = store.require_role(actor, "probe", "runner")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if who["role"] == "runner" and card["assignee"] != actor:
            raise TurboError(403, "only the assignee runner can merge")
        if card["state"] != "review":
            state.require(card["state"], "merged")
        if card["verdict"] != "approve":
            raise TurboError(409, "merge requires an approve verdict")
        if card["verify_level"] not in _MERGE_LEVELS:
            raise TurboError(409, "merge requires a passed server verify")
        if not card["sha"]:
            raise TurboError(409, "merge requires the submitted sha")
        state.require(card["state"], "merged")
        store.update_card(card_id, state="merged", updated_at=now)
        store.add_event(card_id, "merge", {"actor": actor, "sha": card["sha"]}, now)
    return _view(store, card_id, reveal=False)


def edit_card(store: Store, card_id: str, actor: str, body: str | None,
              contract: str | None, raw_scope: object, clock) -> dict:
    store.require_role(actor, "human")
    now = _now(clock)
    with store.transaction():
        card = store.get(card_id)
        if card["state"] not in {"open", "working", "question", "awaiting-human"}:
            raise TurboError(409, "this card can no longer be edited")
        new_body = card["body"] if body is None else body
        new_contract = card["contract"] if contract is None else contract
        new_scope = card["scope"] if raw_scope is None else scope.parse_scope(raw_scope)
        if not isinstance(new_body, str) or not new_body.strip():
            raise TurboError(422, "body is required")
        if not isinstance(new_contract, str) or not new_contract.strip():
            raise TurboError(422, "contract is required")
        version = card["contract_version"] + 1
        nonce = gate.new_nonce()
        store.update_card(
            card_id,
            body=new_body.strip(),
            contract=new_contract.strip(),
            scope_json=json.dumps(new_scope, sort_keys=True, ensure_ascii=False),
            contract_version=version,
            work_token="",
            token_digest="",
            nonce=nonce,
            nonce_used=0,
            ready=0,
            updated_at=now,
        )
        store.add_event(card_id, "edited", {"contract_version": version, "reack": True}, now)
    viewed = _view(store, card_id, reveal=False)
    viewed["nonce"] = nonce
    return viewed


def metrics(store: Store, legacy_path: str | None, live: set[str], now: float) -> dict:
    cards = store.list_cards()
    by_state: dict[str, int] = {}
    stuck: list[dict] = []
    for card in cards:
        name = str(card["state"])
        by_state[name] = by_state.get(name, 0) + 1
        if card["state"] != "working":
            continue
        if card["id"] in live:
            continue
        session = card.get("session") or {}
        reason = "handoff" if session.get("mode") == "handoff" else "no live handle"
        age_s = int(now - parse_stamp(card["updated_at"]))
        stuck.append({
            "id": card["id"],
            "title": card["title"],
            "harness": card["harness"],
            "age_s": age_s,
            "reason": reason,
        })
    out: dict[str, object] = {
        "cards": len(cards),
        "request_changes_repeat": sum(1 for card in cards if card["rc_count"] >= 2),
        "scope_complete": sum(1 for card in cards if card["scope"]["paths"] and card["scope"]["verify"]),
        "by_state": by_state,
        "stuck": stuck,
    }
    if legacy_path:
        from . import legacy
        try:
            out.update(legacy.brief_stats(legacy_path))
        except TurboError:
            out["legacy_cards"] = 0
            out["legacy_brief_complete"] = 0
    return out


def next_open(store: Store, actor: str) -> dict | None:
    runner = store.require_role(actor, "runner")
    if store.busy_card(actor):
        return None
    for card in store.list_cards(state="open"):
        if card["harness"] in runner["harnesses"]:
            return public_card(card)
    return None


def pending_work(store: Store, actor: str) -> list[dict]:
    """Cards this runner should resume. One writer: skip anything already working."""
    store.require_role(actor, "runner")
    out = []
    for card in store.list_cards():
        if card["assignee"] != actor:
            continue
        pending = store.pending_resume(card["id"])
        if not pending:
            continue
        if card["state"] == "awaiting-human" and card["answer"]:
            out.append({**public_card(card), "resume": {
                "token": pending["token"], "kind": pending["kind"], "prompt": pending["prompt"],
            }})
        elif card["state"] == "review" and card["verdict"] == "request-changes":
            out.append({**public_card(card), "resume": {
                "token": pending["token"], "kind": pending["kind"], "prompt": pending["prompt"],
            }})
    return out
