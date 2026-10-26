"""Read-only view of an existing think-tank sqlite file.

This module does not import the legacy server and does not run its
migrations. The connection is opened with mode=ro and query_only.
"""

import json
import sqlite3
from pathlib import Path

from .errors import TurboError

_ISSUE_FIELDS = (
    "id", "title", "body", "state", "priority", "labels", "assignee",
    "parent_id", "acceptance", "created_at", "updated_at", "archived",
    "blocked_question", "blocked_options", "blocked_default",
    "brief_scope", "brief_verify", "brief_forbidden", "brief_timebox",
    "completion_report", "verification_status", "verified",
)


def open_ro(path: str) -> sqlite3.Connection:
    file = Path(path)
    if not file.is_file():
        raise TurboError(404, "legacy database not found")
    uri = file.resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        raise TurboError(404, "legacy database cannot be opened read-only") from exc
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _parse_json(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _labels(value: object) -> list[str]:
    if isinstance(value, list):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        return value
    if not isinstance(value, str) or not value:
        return []
    return [part for part in value.split(",") if part]


def _options(value: object) -> list:
    parsed = _parse_json(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, list) else ([] if not value else [value])


def _issue(row: sqlite3.Row, cols: set[str]) -> dict:
    raw = dict(row)
    out = {key: raw.get(key) for key in _ISSUE_FIELDS if key in raw or key in cols}
    out["labels"] = _labels(raw.get("labels"))
    out["blocked_options"] = _options(raw.get("blocked_options"))
    out["report"] = _parse_json(raw.get("completion_report")) if "completion_report" in cols else None
    out["read_only"] = True
    return out


def list_issues(path: str, limit: int = 500) -> dict:
    con = open_ro(path)
    try:
        cols = _columns(con, "issues")
        if "id" not in cols or "title" not in cols:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            raise TurboError(404, "legacy database has no issues table")
        order = "updated_at DESC" if "updated_at" in cols else "rowid DESC"
        wanted = [name for name in (
            "id", "title", "state", "parent_id", "assignee", "labels",
            "blocked_question", "blocked_options", "blocked_default", "updated_at",
        ) if name in cols]
        rows = con.execute(
            f"SELECT {', '.join(wanted)} FROM issues ORDER BY {order} LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        issues = []
        for row in rows:
            item = dict(row)
            item["labels"] = _labels(item.get("labels"))
            item["blocked_options"] = _options(item.get("blocked_options"))
            item["read_only"] = True
            issues.append(item)
        return {"read_only": True, "issues": issues}
    finally:
        con.close()


def issue_detail(path: str, issue_id: str) -> dict:
    con = open_ro(path)
    try:
        cols = _columns(con, "issues")
        if "id" not in cols:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            raise TurboError(404, "legacy database has no issues table")
        row = con.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            raise TurboError(404, "legacy issue not found")
        issue = _issue(row, cols)
        children = []
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
        if "parent_id" in cols:
            child_rows = con.execute(
                "SELECT * FROM issues WHERE parent_id=? ORDER BY created_at, id" if "created_at" in cols
                else "SELECT * FROM issues WHERE parent_id=? ORDER BY id",
                (issue_id,),
            ).fetchall()
            children = [_issue(child, cols) for child in child_rows]
        comments = []
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
        if _columns(con, "comments"):
            comment_rows = con.execute(
                "SELECT id, issue_id, author, body, ts FROM comments WHERE issue_id=? ORDER BY id",
                (issue_id,),
            ).fetchall()
            comments = [dict(item) for item in comment_rows]
        return {
            "read_only": True,
            "issue": issue,
            "children": children,
            "comments": comments,
            "report": issue.get("report"),
        }
    finally:
        con.close()


def brief_stats(path: str) -> dict:
    """How many legacy cards have all four brief fields filled. Read-only."""
    con = open_ro(path)
    try:
        cols = _columns(con, "issues")
        needed = {"brief_scope", "brief_verify", "brief_forbidden", "brief_timebox"}
        if not needed <= cols:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            return {"legacy_cards": 0, "legacy_brief_complete": 0}
        total = int(con.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
        complete = int(con.execute(
            "SELECT COUNT(*) FROM issues WHERE brief_scope!='' AND brief_verify!='' "
            "AND brief_forbidden!='' AND brief_timebox!=''"
        ).fetchone()[0])
        return {"legacy_cards": total, "legacy_brief_complete": complete}
    finally:
        con.close()
