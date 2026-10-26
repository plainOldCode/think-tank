"""Turbo's own sqlite file. It never opens the legacy issue database."""

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager

from .errors import TurboError

_ALPH = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
  name TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  harnesses TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  contract TEXT NOT NULL,
  contract_version INTEGER NOT NULL DEFAULT 1,
  scope_json TEXT NOT NULL,
  harness TEXT NOT NULL,
  state TEXT NOT NULL,
  assignee TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '',
  session_json TEXT NOT NULL DEFAULT '',
  work_token TEXT NOT NULL DEFAULT '',
  token_digest TEXT NOT NULL DEFAULT '',
  nonce TEXT NOT NULL DEFAULT '',
  nonce_used INTEGER NOT NULL DEFAULT 0,
  blocked_question TEXT NOT NULL DEFAULT '',
  blocked_options TEXT NOT NULL DEFAULT '[]',
  blocked_default TEXT NOT NULL DEFAULT '',
  answer TEXT NOT NULL DEFAULT '',
  answer_by TEXT NOT NULL DEFAULT '',
  answered_at TEXT NOT NULL DEFAULT '',
  question_at TEXT NOT NULL DEFAULT '',
  default_deadline TEXT NOT NULL DEFAULT '',
  ready INTEGER NOT NULL DEFAULT 0,
  sha TEXT NOT NULL DEFAULT '',
  pr_json TEXT NOT NULL DEFAULT '',
  verify_level TEXT NOT NULL DEFAULT '',
  verify_log TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL DEFAULT '',
  rc_count INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  prompt TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cards_state ON cards(state);
CREATE INDEX IF NOT EXISTS idx_resumes_card ON resumes(card_id, consumed);
"""

CARD_COLUMNS = {
    "title", "body", "contract", "contract_version", "scope_json", "harness",
    "state", "assignee", "agent", "session_json", "work_token", "token_digest",
    "nonce", "nonce_used", "blocked_question", "blocked_options", "blocked_default",
    "answer", "answer_by", "answered_at", "question_at", "default_deadline",
    "ready", "sha", "pr_json", "verify_level", "verify_log", "verdict",
    "rc_count", "started_at", "created_at", "updated_at",
}


def new_id() -> str:
    return "T" + "".join(secrets.choice(_ALPH) for _ in range(8))


def card_from_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["scope"] = json.loads(d.pop("scope_json"))
    d["blocked_options"] = json.loads(d["blocked_options"] or "[]")
    d["session"] = json.loads(d.pop("session_json")) if d.get("session_json") else None
    d["pr"] = json.loads(d.pop("pr_json")) if d.get("pr_json") else None
    d["ready"] = bool(d["ready"])
    d["nonce_used"] = bool(d["nonce_used"])
    return d


def public_card(card: dict, reveal_token: bool = False) -> dict:
    hidden = {"nonce", "work_token", "token_digest"}
    out = {k: v for k, v in card.items() if k not in hidden}
    if reveal_token:
        out["work_token"] = card.get("work_token") or ""
    return out


class Store:
    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        self.con.commit()
        self.lock = threading.RLock()
        self._depth = 0

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self._depth += 1
            try:
                yield
                if self._depth == 1:
                    self.con.commit()
            except Exception:
                if self._depth == 1:
                    self.con.rollback()
                raise
            finally:
                self._depth -= 1

    def count_actors(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM actors").fetchone()[0])

    def insert_actor(self, name: str, role: str, harnesses: list[str]) -> None:
        self.con.execute(
            "INSERT INTO actors (name, role, harnesses) VALUES (?, ?, ?)",
            (name, role, ",".join(harnesses)),
        )

    def actor(self, name: str) -> dict | None:
        row = self.con.execute("SELECT * FROM actors WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        harnesses = [h for h in (row["harnesses"] or "").split(",") if h]
        return {"name": row["name"], "role": row["role"], "harnesses": harnesses}

    def require_actor(self, name: str) -> dict:
        found = self.actor(name)
        if not found:
            raise TurboError(403, "unknown actor")
        return found

    def require_role(self, name: str, *roles: str) -> dict:
        found = self.require_actor(name)
        if found["role"] not in roles:
            raise TurboError(403, f"{name} is not a {' or '.join(roles)}")
        return found

    def insert_card(self, fields: dict) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.con.execute(f"INSERT INTO cards ({cols}) VALUES ({marks})", list(fields.values()))

    def update_card(self, card_id: str, **fields) -> None:
        unknown = set(fields) - CARD_COLUMNS
        if unknown:
            raise RuntimeError("unknown card columns: " + ", ".join(sorted(unknown)))
        sets = ", ".join(f"{key}=?" for key in fields)
        self.con.execute(
            f"UPDATE cards SET {sets} WHERE id=?",
            [*fields.values(), card_id],
        )

    def get(self, card_id: str) -> dict:
        row = self.con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            raise TurboError(404, "card not found")
        return card_from_row(row)

    def list_cards(self, state: str | None = None, harness: str | None = None) -> list[dict]:
        sql = "SELECT * FROM cards WHERE 1=1"
        args: list[str] = []
        if state:
            sql += " AND state=?"
            args.append(state)
        if harness:
            sql += " AND harness=?"
            args.append(harness)
        sql += " ORDER BY created_at, id"
        return [card_from_row(row) for row in self.con.execute(sql, args).fetchall()]

    def busy_card(self, assignee: str) -> dict | None:
        row = self.con.execute(
            "SELECT * FROM cards WHERE assignee=? AND state IN "
            "('working','question','awaiting-human','submit','review') "
            "ORDER BY updated_at LIMIT 1",
            (assignee,),
        ).fetchone()
        return card_from_row(row) if row else None

    def add_event(self, card_id: str, kind: str, payload: dict, ts: str) -> None:
        self.con.execute(
            "INSERT INTO events (card_id, kind, payload, ts) VALUES (?, ?, ?, ?)",
            (card_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), ts),
        )

    def events(self, card_id: str, limit: int = 50) -> list[dict]:
        rows = self.con.execute(
            "SELECT id, kind, payload, ts FROM events WHERE card_id=? ORDER BY id DESC LIMIT ?",
            (card_id, limit),
        ).fetchall()
        out = []
        for row in reversed(rows):
            out.append({
                "id": row["id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "ts": row["ts"],
            })
        return out

    def add_resume(self, card_id: str, token: str, kind: str, prompt: str, ts: str) -> None:
        self.con.execute(
            "INSERT INTO resumes (card_id, token, kind, prompt, created_at) VALUES (?, ?, ?, ?, ?)",
            (card_id, token, kind, prompt, ts),
        )

    def pending_resume(self, card_id: str) -> dict | None:
        row = self.con.execute(
            "SELECT * FROM resumes WHERE card_id=? AND consumed=0 ORDER BY id LIMIT 1",
            (card_id,),
        ).fetchone()
        return dict(row) if row else None

    def resume_by_token(self, token: str) -> dict | None:
        row = self.con.execute("SELECT * FROM resumes WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def consume_resume(self, token: str) -> None:
        self.con.execute("UPDATE resumes SET consumed=1 WHERE token=?", (token,))

    def awaiting_deadline(self, now_stamp: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM cards WHERE state='awaiting-human' AND answer='' "
            "AND default_deadline!='' AND default_deadline<=?",
            (now_stamp,),
        ).fetchall()
        return [card_from_row(row) for row in rows]
