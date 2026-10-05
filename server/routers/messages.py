"""에이전트 간 메시지 보드 API (M4580A48-573W).

- POST /messages: 작성. thread_id로 답글(답글에 답글은 루트로 평탄화). 본문 @토큰 중
  등록 에이전트명만 mentions로 저장(콤마 패딩 — 정확 매칭).
- GET /messages: 최신순 목록. thread=는 루트+답글 시간순, mentions=는 멘션 필터,
  since=는 증분 폴링. 각 메시지에 reads(읽은 에이전트 목록) 포함.
- POST /messages/{id}/read: 읽음 표시(멱등). GET /messages/unread?agent=: 안 읽은 수.
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import db as dbmod
from service import Ctx
from routers.agents import get_ctx

router = APIRouter()


class MessageIn(BaseModel):
    author: str
    body: str
    thread_id: str | None = None

    @field_validator("author", "body")
    @classmethod
    def _nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("author와 body는 비어 있을 수 없다")
        return v.strip()


class ReadIn(BaseModel):
    agent: str

    @field_validator("agent")
    @classmethod
    def _nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError("agent는 비어 있을 수 없다")
        return v.strip()


def _parse_mentions(con, body):
    """@토큰에서 등록 에이전트명 추출. 조사 결합(@agy도) 허용 — 이름 뒤에 ASCII 식별자가
    붙지 않으면 멘션으로 인정. 긴 이름 우선(codex vs codex-read-only)."""
    names = sorted((r["name"] for r in con.execute("SELECT name FROM agents").fetchall()),
                   key=len, reverse=True)
    seen, out = set(), []
    for tok in re.findall(r"@([^\s@,]+)", body):
        for name in names:
            if tok.startswith(name):
                rest = tok[len(name):]
                if not rest or not re.search(r"[A-Za-z0-9_-]", rest):
                    if name not in seen:
                        seen.add(name)
                        out.append(name)
                break
    return out


def _to_dict(r, reads):
    d = dict(r)
    d["reads"] = reads
    return d


def _reads_of(con, ids):
    reads = {}
    if ids:
        q = ",".join("?" for _ in ids)
        for row in con.execute(f"SELECT message_id, agent FROM message_reads WHERE message_id IN ({q})", list(ids)):
            reads.setdefault(row["message_id"], []).append(row["agent"])
    return reads


@router.post("/messages", status_code=201)
def post_message(p: MessageIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        thread_id = p.thread_id
        if thread_id is not None:
            root = c.execute(
                "SELECT id, thread_id FROM messages WHERE id=?", (thread_id,)).fetchone()
            if root is None:
                raise HTTPException(422, f"unknown thread_id {thread_id}")
            thread_id = root["thread_id"] or root["id"]  # 답글에 답글은 루트로 평탄화
        mentions = _parse_mentions(c, p.body)
        mid = dbmod.new_id()
        c.execute("INSERT INTO messages (id, thread_id, author, body, mentions, created_at) VALUES (?,?,?,?,?,?)",
                  (mid, thread_id, p.author, p.body, f",{','.join(mentions)}," if mentions else "", dbmod.now()))
        c.commit()
        return _to_dict(c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone(), [])


@router.get("/messages")
def list_messages(thread: str | None = None, mentions: str | None = None,
                  since: str | None = None, limit: int = 200,
                  ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        if thread is not None:
            root = c.execute("SELECT id, thread_id FROM messages WHERE id=?", (thread,)).fetchone()
            if root is None:
                raise HTTPException(404, f"unknown thread {thread}")
            rid = root["thread_id"] or root["id"]
            rows = c.execute("SELECT * FROM messages WHERE id=? OR thread_id=? ORDER BY created_at, id",
                             (rid, rid)).fetchall()
        else:
            sql = "SELECT * FROM messages WHERE 1=1"
            args: list = []
            if mentions is not None:
                sql += " AND mentions LIKE ?"
                args.append(f"%,{mentions},%")
            if since:
                sql += " AND created_at > ?"
                args.append(since)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            args.append(max(1, min(limit, 1000)))
            rows = c.execute(sql, args).fetchall()
        reads = _reads_of(c, [r["id"] for r in rows])
        return [_to_dict(r, reads.get(r["id"], [])) for r in rows]


@router.post("/messages/{mid}/read")
def mark_read(mid: str, p: ReadIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        if c.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone() is None:
            raise HTTPException(404, f"unknown message {mid}")
        c.execute("INSERT OR IGNORE INTO message_reads (message_id, agent, ts) VALUES (?,?,?)",
                  (mid, p.agent, dbmod.now()))
        c.commit()
        return {"ok": True}


@router.get("/messages/unread")
def unread(agent: str, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        n = c.execute(
            "SELECT COUNT(*) n FROM messages WHERE author != ? AND id NOT IN "
            "(SELECT message_id FROM message_reads WHERE agent=?)", (agent, agent)).fetchone()["n"]
        return {"count": n}
