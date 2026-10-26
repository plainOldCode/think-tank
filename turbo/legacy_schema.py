"""Throwaway copy of the think-tank issues and comments tables.

Tests use this to build an empty sqlite file with the columns the live
database has. Opening TT_LEGACY_DB never executes this SQL. The reader
does not create, alter, or backfill that file.
"""

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE issues (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  acceptance TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'todo',
  priority INTEGER,
  labels TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '',
  nominee TEXT NOT NULL DEFAULT '',
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
  blocked_question TEXT NOT NULL DEFAULT '',
  blocked_options TEXT NOT NULL DEFAULT '',
  blocked_default TEXT NOT NULL DEFAULT '',
  brief_scope TEXT NOT NULL DEFAULT '',
  brief_verify TEXT NOT NULL DEFAULT '',
  brief_forbidden TEXT NOT NULL DEFAULT '',
  brief_timebox TEXT NOT NULL DEFAULT '',
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  verified_evidence TEXT NOT NULL DEFAULT '',
  blocked_notified_at TEXT,
  work_contract TEXT NOT NULL DEFAULT '',
  execution_attempt INTEGER NOT NULL DEFAULT 0,
  claim_count INTEGER NOT NULL DEFAULT 0,
  evidence_after_comment_id INTEGER NOT NULL DEFAULT 0,
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  completion_report TEXT NOT NULL DEFAULT '',
  submission_revision INTEGER NOT NULL DEFAULT 0,
  delivery_receipt TEXT NOT NULL DEFAULT '',
  premise_rework INTEGER NOT NULL DEFAULT 0,
  todo_since TEXT,
  reviewer TEXT,
  approval_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  ts TEXT NOT NULL
);
"""


def create_throwaway(path: str) -> None:
    """Create a new empty issues/comments file. Refuses an existing path."""
    file = Path(path)
    if file.exists():
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
        raise FileExistsError(path)
    con = sqlite3.connect(path)
    try:
        con.executescript(DDL)
        con.commit()
    finally:
        con.close()
