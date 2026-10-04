"""에이전트·디스패치 라우터 — agent registry, hook dispatch, runner progress."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request

import config
import db as dbmod
import service
from models import AgentIn, AgentPatch, DispatchIn, DispatchProgress
from service import Ctx

router = APIRouter()


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


@router.get("/agents")
def list_agents(ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


@router.post("/agents", status_code=201, summary="Register a webhook agent (receives dispatch POSTs at base_url)", description="base_url must be a reachable HTTP endpoint (hook mode). Agents without a listener cannot receive dispatch — see /api.md integration table. release_hook=true 선언 시 카드 terminalize 시 release 명령(x-tt-command: release)도 수신 — runner류만 켠다. notify_hook=true 시 blocked(waiting_for=human) Level4 알림(x-tt-command: notify) 수신 — 알림 주입 어댑터(Hermes)용.")
def add_agent(p: AgentIn, ctx: Ctx = Depends(get_ctx)):
    name = p.name.strip()
    if not name or not service.valid_url(p.base_url):
        raise HTTPException(422, "name 필수, base_url은 http(s)만 허용")
    with ctx.con() as c:
        if c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"agent {name} 이미 등록됨")
        c.execute("INSERT INTO agents (name, base_url, secret, enabled, release_hook, notify_hook, created_at) VALUES (?,?,?,?,?,?,?)",
                  (name, p.base_url, p.secret, 1 if p.enabled else 0, 1 if p.release_hook else 0,
                   1 if p.notify_hook else 0, dbmod.now()))
        c.commit()
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    return dict(row)


@router.patch("/agents/{name}")
def patch_agent(name: str, p: AgentPatch, ctx: Ctx = Depends(get_ctx)):
    fields: dict = {}
    if p.base_url is not None:
        if not service.valid_url(p.base_url):
            raise HTTPException(422, "base_url은 http(s)만 허용")
        fields["base_url"] = p.base_url
    if p.secret is not None:
        fields["secret"] = p.secret
    if p.enabled is not None:
        fields["enabled"] = 1 if p.enabled else 0
    if p.release_hook is not None:
        fields["release_hook"] = 1 if p.release_hook else 0
    if p.notify_hook is not None:
        fields["notify_hook"] = 1 if p.notify_hook else 0
    with ctx.con() as c:
        if not c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise HTTPException(404, f"agent {name} not found")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE agents SET {sets} WHERE name=?", (*fields.values(), name))
            c.commit()
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    return dict(row)


@router.delete("/agents/{name}")
def delete_agent(name: str, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        res = c.execute("DELETE FROM agents WHERE name=?", (name,))
        c.commit()
        if res.rowcount != 1:
            raise HTTPException(404, f"agent {name} not found")
    return {"ok": True}


@router.post("/issues/{issue_id}/dispatch", status_code=201,
             summary="Dispatch: deliver an instruction to a registered agent via webhook (agent needs a listening base_url; poll-only agents should use todo+comments instead)",
             description="Records message as a comment, POSTs the payload to the agent webhook (10s timeout), stores optional resume context, and files a tt-server system comment on delivery failure. Never changes issue state.")
def dispatch(issue_id: str, p: DispatchIn, request: Request, ctx: Ctx = Depends(get_ctx)):
    if not p.message.strip():
        raise HTTPException(422, "message 비어 있음")
    with ctx.con() as c:
        issue = service.get_issue(c, issue_id)
        ag = c.execute("SELECT * FROM agents WHERE name=?", (p.agent,)).fetchone()
        if not ag:
            raise HTTPException(404, f"agent {p.agent} 미등록 — POST /agents")
        if not ag["enabled"]:
            raise HTTPException(409, f"agent {p.agent} 비활성")
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (issue_id, p.author, p.message, dbmod.now()))
        prev = c.execute("SELECT context FROM dispatches WHERE issue_id=? AND agent=? "
                         "ORDER BY id DESC LIMIT 1", (issue_id, p.agent)).fetchone()
        tail = dbmod.comments_of(c, issue_id)[-20:]
        did = c.execute("INSERT INTO dispatches (issue_id, agent, author, message, context, status, ts) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (issue_id, p.agent, p.author, p.message, "", "queued", dbmod.now())).lastrowid
        c.commit()
    payload = {
        "dispatch_id": did, "issue_id": issue_id, "issue_title": issue["title"],
        "agent": p.agent, "author": p.author, "message": p.message,
        "context": prev["context"] if prev else "", "comments": tail,
        "tt_url": str(request.base_url).rstrip("/"),
        "work_contract": (p.work_contract if p.work_contract is not None else
                          (json.loads(issue["work_contract"]) if issue["work_contract"] else ctx.contract)),
        "execution_attempt": issue["execution_attempt"],
    }
    status, detail, dctx = "ok", "", ""
    try:
        code, dctx = service.deliver(ag["base_url"], ag["secret"], payload)
        if code >= 300:
            status, detail = "error", f"HTTP {code}"
    except Exception as e:
        status, detail = "error", f"{type(e).__name__}: {e}"[:300]
    with ctx.con() as c:
        c.execute("UPDATE dispatches SET status=?, detail=?, context=? WHERE id=?", (status, detail, dctx, did))
        c.execute("UPDATE agents SET last_ok=?, last_err=? WHERE name=?",
                  (dbmod.now() if status == "ok" else ag["last_ok"],
                   "" if status == "ok" else detail, p.agent))
        if status == "error":
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, "tt-server", f"⚠ hook dispatch #{did} → {p.agent} 실패: {detail}", dbmod.now()))
        c.commit()
        row = c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    return dict(row)


@router.get("/issues/{issue_id}/dispatches")
def list_dispatches(issue_id: str, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        service.get_issue(c, issue_id)
        rows = c.execute("SELECT * FROM dispatches WHERE issue_id=? ORDER BY id", (issue_id,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/issues/{issue_id}/dispatches/{dispatch_id}/progress",
             summary="Runner progress projection (dispatch-record-only, no comments)",
             description="러너→서버 진행 투영: dispatch 레코드만 갱신한다(코멘트 무생성, last-write-wins). "
                         "인증은 deliver() 규약 상속 — Bearer agent secret(secret 빈 agent는 생략 허용). "
                         "stalled 판정은 러너 stall_check 소유(서버 재계산 금지).")
def dispatch_progress(issue_id: str, dispatch_id: int, p: DispatchProgress, request: Request,
                      ctx: Ctx = Depends(get_ctx)):
    if p.state is not None and p.state not in config.RUN_STATES:
        raise HTTPException(422, f"state enum: {'|'.join(sorted(config.RUN_STATES))}")
    with ctx.con() as c:
        row = c.execute("SELECT d.*, a.secret AS agent_secret FROM dispatches d "
                        "LEFT JOIN agents a ON a.name=d.agent WHERE d.id=?",
                        (dispatch_id,)).fetchone()
        if not row or row["issue_id"] != issue_id:
            raise HTTPException(404, f"dispatch {dispatch_id} not found for issue {issue_id}")
        secret = row["agent_secret"] or ""
        if secret:  # deliver()의 secret 옵션 규약 동일: 빈 secret은 생략 허용
            auth = (request.headers.get("authorization") or "").replace("Bearer ", "")
            if auth != secret:
                raise HTTPException(403, "secret 불일치")
        fields: dict = {}
        ts = p.ts if p.ts else dbmod.now()
        if p.state is not None:
            fields["run_state"] = p.state
            if p.state in ("running", "stalled"):
                # 진행 병기 허용 상태: ts/tail 유무와 무관하게 liveness 시각 갱신
                # (동일 상태 재전송도 진행으로 봄 — 러너는 변경/주기 모두 허용)
                fields["last_progress_at"] = ts
                if p.tail is not None:
                    fields["last_tail"] = p.tail[:500]  # 서버 클램프 (422 아님)
                if not row["started_at"]:
                    fields["started_at"] = ts
            elif p.state in ("finished", "failed"):
                fields["ended_at"] = ts  # tail 병기 없음 — 종료 상세는 done/failed 코멘트 소관
            else:
                # queued: run_state(+machine/session)만 — ts/tail 진행 미반영 (§2.2)
                if not row["started_at"]:
                    fields["started_at"] = ts
        elif p.tail is not None or p.ts or p.machine or p.session:
            raise HTTPException(422, "state 없이 진행/식별 갱신 불가")
        # machine/session: 전송 시에만 반영(공백 문자열은 전송으로 보지 않음 — 기존 값 유지)
        for k in ("machine", "session"):
            v = getattr(p, k)
            if v:
                fields[k] = v
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE dispatches SET {sets} WHERE id=?", (*fields.values(), dispatch_id))
            c.commit()
        row = c.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    return dict(row)


@router.get("/agents/active",
            summary="Active tmux dispatches (queued/running/stalled)",
            description="실행 종료(finished/failed)·미실행('') 제외. stalled는 러너가 보낸 값 그대로 노출 "
                        "(서버 시간 계산으로 새로 판정하지 않는다). 빈 결과 = 200 + [].")
def agents_active(ctx: Ctx = Depends(get_ctx)):
    ph = ",".join("?" * len(config.ACTIVE_RUN_STATES))
    with ctx.con() as c:
        rows = c.execute(
            f"SELECT d.id, d.issue_id, i.title AS issue_title, d.agent, d.machine, d.session, "
            f"d.run_state, d.started_at, d.last_progress_at, d.last_tail, d.ts "
            f"FROM dispatches d LEFT JOIN issues i ON i.id=d.issue_id "
            f"WHERE d.run_state IN ({ph}) ORDER BY d.id", config.ACTIVE_RUN_STATES).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["dispatch_id"] = d.pop("id")
        base = config.parse_iso(d.get("started_at")) or config.parse_iso(d["ts"])
        d.pop("ts")
        if base:
            from datetime import datetime
            now_dt = datetime.now(base.tzinfo) if base.tzinfo else datetime.now()
            d["elapsed_s"] = max(0, int((now_dt - base).total_seconds()))
        out.append(d)
    return out
