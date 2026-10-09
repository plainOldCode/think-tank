import sqlite3
import time
import os
from datetime import datetime
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
  completion_report TEXT NOT NULL DEFAULT '',
  reviewer TEXT
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
  release_hook INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  reasoning TEXT,
  tier TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  mentions TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_reads (
  message_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  ts TEXT NOT NULL,
  PRIMARY KEY (message_id, agent)
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
  ts TEXT NOT NULL,
  run_state TEXT NOT NULL DEFAULT '',
  machine TEXT NOT NULL DEFAULT '',
  session TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  last_progress_at TEXT,
  last_tail TEXT NOT NULL DEFAULT '',
  ended_at TEXT,
  model TEXT NOT NULL DEFAULT '',
  idem_key TEXT,
  delivery_lease TEXT
);
CREATE TABLE IF NOT EXISTS events (
  -- 변경 이벤트 outbox (TT 개선#2): 단조 seq, 변경과 같은 트랜잭션에서 기록 —
  -- 소비자는 after_seq 커서로 재접속 시 유실 없이 이어받는다.
  -- notified: 웹훅 전달 추적(리뷰 R4a) — 저장은 트랜잭션 안, 전달은 커밋 후 워커.
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  ts TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS dispatch_reports (
  -- 종료 보고 접수 이력 (R10): (dispatch, session) 유니크 — 응답 유실 재시도
  -- 멱등 + 끼어든 세션 재시도에서도 동일 보고 재접수 방지
  dispatch_id INTEGER NOT NULL,
  session TEXT NOT NULL,
  ts TEXT NOT NULL,
  PRIMARY KEY (dispatch_id, session)
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
    for col in ("acceptance TEXT NOT NULL DEFAULT ''",  # TT 개선#3a: 완료 기준
                "archived INTEGER NOT NULL DEFAULT 0",
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
                "completion_report TEXT NOT NULL DEFAULT ''",
                # SRM1: 지연 표기 — todo 진입 시각(기존 todo 행은 created_at 백필)
                "todo_since TEXT",
                # M42KC1XR-2DD1: 리뷰어 점유 — review 카드에서 판정자 표기(assignee는 작업자 유지)
                "reviewer TEXT"):
        try:
            con.execute(f"ALTER TABLE issues ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # todo_since 백필 — 진입 시각 미상 기존 todo는 생성각으로(보수적 지연 판정, 사람 판단용)
    try:
        con.execute("UPDATE issues SET todo_since=created_at WHERE state='todo' AND todo_since IS NULL")
        con.commit()
    except sqlite3.OperationalError:
        pass
    for col in ("release_hook INTEGER NOT NULL DEFAULT 0",
                "notify_hook INTEGER NOT NULL DEFAULT 0",
                # M3ER6G3S-RZ20: 모델 메타데이터 — 선언(declaration)이지 실행 보장이 아님
                "model TEXT",
                "reasoning TEXT",
                "tier TEXT"):
        try:
            con.execute(f"ALTER TABLE agents ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in ("model TEXT NOT NULL DEFAULT ''",
                "idem_key TEXT",
                "attempt INTEGER",
                "delivery_lease TEXT",
                "report_session TEXT"):
        try:
            con.execute(f"ALTER TABLE dispatches ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 동시 중복 POST의 원자적 승자 결정용 부분 유니크 인덱스(NULL 다수 허용).
    # 기존 행은 전부 NULL이라 인덱스 생성이 안전하다.
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatches_idem "
                    "ON dispatches(idem_key) WHERE idem_key IS NOT NULL")
        con.commit()
    except sqlite3.OperationalError:
        pass
    # dispatch 실행 상태 투영 (TT M3EREF97-FXWQ): 기존 행은 '' = 비-tmux·러너 미수신.
    # status(웹훅 전달 상태)와 층위가 다르다 — 진행 상태의 단일 소스는 러너(서버 재계산 금지).
    for col in ("run_state TEXT NOT NULL DEFAULT ''",
                "machine TEXT NOT NULL DEFAULT ''",
                "session TEXT NOT NULL DEFAULT ''",
                "started_at TEXT",
                "last_progress_at TEXT",
                "last_tail TEXT NOT NULL DEFAULT ''",
                "ended_at TEXT"):
        try:
            con.execute(f"ALTER TABLE dispatches ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # 알림 전달 추적 (리뷰 R4a): 이벤트 저장은 트랜잭션 안, 웹훅 전달은 커밋 후
    # 워커가 락 밖에서 수행 — notified=0만 미전달. 기존 행은 1(처리 완료 간주).
    try:
        con.execute("ALTER TABLE events ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
        con.execute("UPDATE events SET notified=1 WHERE notified IS NULL")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_unnotified "
                    "ON events(seq) WHERE notified=0")
        con.commit()
    except sqlite3.OperationalError:
        pass
    return con


DELAYED_AFTER_H = 24  # SRM1: todo 무수령 지연 기준


def _delayed(d):
    if d.get("state") != "todo" or not d.get("todo_since"):
        return False
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        since = datetime.strptime(d["todo_since"], fmt)
        now = datetime.strptime(time.strftime(fmt), fmt)
        return (now - since).total_seconds() >= DELAYED_AFTER_H * 3600
    except ValueError:
        return False


def to_dict(row):
    d = dict(row)
    d["delayed"] = _delayed(d)
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
