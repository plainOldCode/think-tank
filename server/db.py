import sqlite3
import time
import os
import secrets
import json

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32

STATES = {"backlog", "todo", "in_progress", "review", "blocked", "done", "cancelled"}
TRANSITIONS = {
    "backlog": {"todo", "cancelled"},
    "todo": {"in_progress", "blocked", "cancelled", "backlog"},
    "in_progress": {"todo", "blocked", "done", "review"},
    "review": {"todo", "blocked", "done", "cancelled"},  # done-게이트 강등 (M3BZV172-9F0S A)
    "blocked": {"todo", "in_progress", "cancelled"},
    "done": {"todo"},
    "cancelled": {"todo"},
}
TERMINAL_FIELDS = {"done", "cancelled"}

# blocked 사족 (TT M3BZS1FS-5722 ①): waiting_for 종류와 책임 액터.
# 미입력 시 기존 동작 그대로(빈 값) — backwards-compatible migration.
WAITING_FOR = {"dependency", "human", "gate", "external"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'todo',
  priority INTEGER,
  labels TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  parent_id TEXT REFERENCES issues(id),
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  lease_by TEXT NOT NULL DEFAULT '',
  lease_expires TEXT,
  heartbeat_at TEXT,
  waiting_for TEXT NOT NULL DEFAULT '',
  waiting_actor TEXT NOT NULL DEFAULT '',
  blocked_detail TEXT NOT NULL DEFAULT '',
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  verified_evidence TEXT NOT NULL DEFAULT '',
  blocked_notified_at TEXT,
  work_contract TEXT NOT NULL DEFAULT '',
  execution_attempt INTEGER NOT NULL DEFAULT 0,
  evidence_after_comment_id INTEGER NOT NULL DEFAULT 0,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  completion_report TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent_id);
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY,
  base_url TEXT NOT NULL,
  secret TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_ok TEXT,
  last_err TEXT,
  release_hook INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ts TEXT NOT NULL
);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def future(hours):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + hours * 3600))


def new_id():
    ms = int(time.time() * 1000)
    chars = []
    for _ in range(8):
        chars.append(ALPHABET[ms & 31])
        ms >>= 5
    chars.reverse()
    return "".join(chars) + "-" + "".join(ALPHABET[secrets.randbelow(32)] for _ in range(4))


def connect(path):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    for col in ("archived INTEGER NOT NULL DEFAULT 0",
                "lease_by TEXT NOT NULL DEFAULT ''",
                "lease_expires TEXT",
                "heartbeat_at TEXT",
                "waiting_for TEXT NOT NULL DEFAULT ''",
                "waiting_actor TEXT NOT NULL DEFAULT ''",
                "blocked_detail TEXT NOT NULL DEFAULT ''",
                # done≠verified 게이트 + blocked(human) 알림 (TT M3BZV172-9F0S)
                "verified INTEGER NOT NULL DEFAULT 0",
                "verified_at TEXT",
                "verified_evidence TEXT NOT NULL DEFAULT ''",
                "blocked_notified_at TEXT",
                "work_contract TEXT NOT NULL DEFAULT ''",
                "execution_attempt INTEGER NOT NULL DEFAULT 0",
                "evidence_after_comment_id INTEGER NOT NULL DEFAULT 0",
                "verification_status TEXT NOT NULL DEFAULT 'unverified'",
                "completion_report TEXT NOT NULL DEFAULT ''"):
        try:
            con.execute(f"ALTER TABLE issues ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("release_hook INTEGER NOT NULL DEFAULT 0",
                "notify_hook INTEGER NOT NULL DEFAULT 0"):
        try:
            con.execute(f"ALTER TABLE agents ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def to_dict(row):
    d = dict(row)
    d["labels"] = [s for s in d["labels"].split(",") if s]
    d["work_contract"] = json.loads(d["work_contract"]) if d.get("work_contract") else None
    d["completion_report"] = json.loads(d["completion_report"]) if d.get("completion_report") else None
    if d.get("verified") and d["verification_status"] == "unverified":
        d["verification_status"] = "legacy"
    return d


def comments_of(con, issue_id):
    rows = con.execute(
        "SELECT id, issue_id, author, body, ts FROM comments WHERE issue_id=? ORDER BY id",
        (issue_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def can_transition(from_state, to_state):
    return to_state in TRANSITIONS.get(from_state, set())
