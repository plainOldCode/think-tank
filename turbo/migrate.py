"""Copy a legacy issues database into a turbo database.

The legacy file is opened read-only. Re-running skips ids that are already
cards, so a second run does not duplicate issues or comments. ``--dry-run``
reports the same mapping and does not create a turbo file that is absent.

In-flight legacy work is not left on a dead assignee. ``in_progress`` becomes
``open``. A blocked card with a question becomes ``awaiting-human`` and
``confirm`` so the 24 hour default cannot apply it. Cancelled rows are
skipped: turbo has no cancelled state.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .clock import stamp
from .errors import TurboError
from .legacy import open_ro
from .store import Store

_OPEN = {"backlog", "todo", "in_progress"}
_FALLBACK_STAMP = "1970-01-01T00:00:00+0000"
_SCOPE = {
    "paths": ["*"],
    "forbidden": [],
    "verify": [{
        "cmd": "python3 -c \"import sys; raise SystemExit(0)\"",
        "kind": "planned",
        "timeout_s": 120,
    }],
    "timebox_s": None,
    "exempt": True,
}


def _tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _raw(row: sqlite3.Row, key: str) -> object:
    if key not in row.keys():
        return None
    return row[key]


def _cell(row: sqlite3.Row, key: str) -> str:
    value = _raw(row, key)
    if isinstance(value, str):
        return value.strip()
    return ""


def _stamp_of(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _FALLBACK_STAMP


def _options(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    text = value.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    if text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _load_comments(con: sqlite3.Connection, tables: set[str]) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    if "comments" not in tables:
        return found
    rows = con.execute(
        "SELECT issue_id, author, body, ts FROM comments ORDER BY id"
    ).fetchall()
    for row in rows:
        found.setdefault(str(row["issue_id"]), []).append({
            "author": str(row["author"]),
            "body": str(row["body"]),
            "ts": row["ts"],
        })
    return found


def _notes(assignee: str, lease: str, detail: str) -> str:
    lines = []
    if assignee:
        lines.append("previous assignee: " + assignee)
    if lease:
        lines.append("previous lease: " + lease)
    if detail:
        lines.append("blocked_detail: " + detail)
    return "\n".join(lines)


def _contract(row: sqlite3.Row, title: str) -> str:
    work = _cell(row, "work_contract")
    if work:
        return work
    acceptance = _cell(row, "acceptance")
    if acceptance:
        return acceptance
    return title


def _destination(state: str, question: str) -> tuple[str, str]:
    if state in _OPEN:
        return "open", ""
    if state == "blocked":
        if question:
            return "awaiting-human", ""
        return "open", ""
    if state == "review":
        return "review", ""
    if state == "done":
        return "merged", ""
    if state == "cancelled":
        return "skip", "cancelled is not a turbo state"
    return "skip", "unknown state"


def _existing_ids(path: str) -> set[str]:
    file = Path(path)
    if not file.is_file():
        return set()
    store = Store(str(file))
    try:
        return {card["id"] for card in store.list_cards()}
    finally:
        store.con.close()


def _write(path: str, records: list[dict]) -> None:
    store = Store(path)
    try:
        for record in records:
            fields = record["fields"]
            with store.transaction():
                store.insert_card(fields)
                for event in record["events"]:
                    store.add_event(fields["id"], event["kind"], event["payload"], event["ts"])
    finally:
        store.con.close()


def _one(row: sqlite3.Row, existing: set[str], comments: dict[str, list[dict]]) -> tuple[dict, dict | None]:
    issue_id = _cell(row, "id")
    if issue_id in existing:
        return {"id": issue_id, "action": "skipped", "reason": "already imported"}, None
    title = _cell(row, "title")
    if not title:
        return {"id": issue_id, "action": "skipped", "reason": "empty title"}, None
    legacy_state = _cell(row, "state")
    question = _cell(row, "blocked_question")
    new_state, reason = _destination(legacy_state, question)
    if new_state == "skip":
        return {"id": issue_id, "action": "skipped", "reason": reason}, None
    assignee = _cell(row, "assignee")
    lease = _cell(row, "lease_by")
    detail = _cell(row, "blocked_detail") if not question else ""
    note = _notes(assignee, lease, detail)
    body = _cell(row, "body") or "(no body)"
    if note:
        body = body + "\n\n" + note
    created = _stamp_of(_raw(row, "created_at"))
    updated = _stamp_of(_raw(row, "updated_at"))
    waiting = new_state == "awaiting-human"
    fields = {
        "id": issue_id,
        "title": title,
        "body": body,
        "contract": _contract(row, title),
        "contract_version": 1,
        "scope_json": json.dumps(_SCOPE, sort_keys=True),
        "harness": "hermes",
        "state": new_state,
        "assignee": "",
        "confirm": 1 if waiting else 0,
        "created_at": created,
        "updated_at": updated,
    }
    if waiting:
        fields["blocked_question"] = question
        fields["blocked_options"] = json.dumps(_options(_raw(row, "blocked_options")))
        fields["blocked_default"] = _cell(row, "blocked_default")
    events = [{
        "kind": "migrated",
        "payload": {"from": legacy_state, "to": new_state},
        "ts": created,
    }]
    imported = comments.get(issue_id) or []
    for comment in imported:
        events.append({
            "kind": "comment",
            "payload": {"author": comment["author"], "body": comment["body"]},
            "ts": _stamp_of(comment["ts"]),
        })
    item = {
        "id": issue_id,
        "action": "mapped",
        "state": new_state,
        "label": issue_id + ":" + new_state,
    }
    if note:
        item["note"] = note
    if imported:
        item["comment"] = imported[0]["body"]
    return item, {"fields": fields, "events": events, "comments": len(imported)}


def migrate(legacy: str, turbo: str, dry_run: bool) -> dict:
    con = open_ro(legacy)
    try:
        tables = _tables(con)
        if "issues" not in tables:
            raise TurboError(422, "legacy database has no issues table")
        rows = con.execute("SELECT * FROM issues ORDER BY id").fetchall()
        comments = _load_comments(con, tables)
    finally:
        con.close()
    existing = _existing_ids(turbo)
    items = []
    records = []
    mapped = 0
    skipped = 0
    comment_count = 0
    for row in rows:
        item, record = _one(row, existing, comments)
        items.append(item)
        if record is None:
            skipped += 1
            continue
        mapped += 1
        comment_count += int(record["comments"])
        records.append(record)
    if not dry_run:
        if records:
            _write(turbo, records)
    return {
        "dry_run": dry_run,
        "mapped": mapped,
        "skipped": skipped,
        "comments": comment_count,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="turbo.migrate")
    parser.add_argument("--legacy", required=True)
    parser.add_argument("--turbo", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.legacy, args.turbo, args.dry_run)
    except TurboError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
