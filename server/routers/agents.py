"""에이전트·디스패치 라우터 — agent registry, hook dispatch, runner progress."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import config
import db as dbmod
import service
from models import AgentIn, AgentPatch, DispatchIn, DispatchProgress
from service import Ctx

router = APIRouter()


DELIVERY_RECOVER_S = 30  # webhook 타임아웃(10s)의 3배 — 전달 lease 만료 기준 (R4)


def _claim_redelivery(c, row):
    """복구 재전달 선점 (리뷰 R4) — 재전달 자격(status ∈ error/queued)과 전달
    lease 신선도를 조건부 갱신 하나로 원자 검사한다. 성공 시 행에 보존된 resume
    context를 반환(R5 — 이후 다른 dispatch 유입과 무관), 실패 시 None(기수락:
    이미 전달됐거나 다른 caller가 전달 진행 중). 신규·충돌·기존 모든 재전달
    진입점이 이 함수 하나를 통과해야 동일 did 이중 웹훅이 불가능하다."""
    res = c.execute(
        "UPDATE dispatches SET delivery_lease=? WHERE id=? AND "
        "status IN ('error','queued') AND "
        "(delivery_lease IS NULL OR delivery_lease < ?)",
        (dbmod.now(), row["id"],
         (datetime.now().astimezone() - timedelta(seconds=DELIVERY_RECOVER_S))
         .strftime("%Y-%m-%dT%H:%M:%S%z")))
    if res.rowcount != 1:
        return None
    return {"context": row["context"] or ""}


def _redeliver_wanted(row) -> bool:
    """기수락 예외 판정 (R4): 재전달 자격(status ∈ error/queued)과 전달 lease
    신선도를 함께 본다. lease는 "전달 소유권" — 신선한 lease(복구 전달 진행 중인
    error 포함)는 기수락, 무 lease·만료(전달 전 크래시 잔재)만 복구 대상이다.
    선점 UPDATE와 동일 조건이라 경합에서도 한쪽만 웹훅을 친다."""
    if row["status"] not in ("error", "queued"):
        return False
    lease = row["delivery_lease"]
    if not lease:
        return True
    try:
        from datetime import datetime
        age = (datetime.now().astimezone() - datetime.fromisoformat(lease)).total_seconds()
    except ValueError:
        return True
    return age > DELIVERY_RECOVER_S


def _idem_key(issue_id: str, agent: str, attempt: int, message: str) -> str:
    """기수락 멱등키 (TT 개선#1 요구 3): issue+agent+attempt+본문 해시.

    실행 회차 ID(execution_attempt)와 멱등키는 구분 개념이다 — 키는 attempt를
    재료로 파생할 뿐이고, attempt가 바뀌면(재회차) 동일 본문이라도 새 키가 되어
    정상 신규 dispatch로 기록된다. 서버가 파생하므로 클라이언트 합의 불필요.
    """
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return f"{issue_id}:{agent}:{attempt}:{digest}"


def _dispatch_payload(issue, ag, p, did, issue_id, request, ctx, prev, tail):
    return {
        "dispatch_id": did, "issue_id": issue_id, "issue_title": issue["title"],
        "agent": p.agent, "author": p.author, "message": p.message,
        "context": prev["context"] if prev else "", "comments": tail,
        "tt_url": str(request.base_url).rstrip("/"),
        "model": ag["model"] or "",  # RZ20: 감사 추적 — 이 회차가 어떤 모델로 실행되는지 선언값
        "work_contract": (p.work_contract if p.work_contract is not None else
                          (json.loads(issue["work_contract"]) if issue["work_contract"] else ctx.contract)),
        "execution_attempt": issue["execution_attempt"],
    }


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
        c.execute("INSERT INTO agents (name, base_url, secret, enabled, release_hook, notify_hook, model, reasoning, tier, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (name, p.base_url, p.secret, 1 if p.enabled else 0, 1 if p.release_hook else 0,
                   1 if p.notify_hook else 0, p.model, p.reasoning, p.tier, dbmod.now()))
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
    # M3ER6G3S-RZ20: model/reasoning은 자유 문자열, tier는 모델에서 정규화 완료.
    # RZ20-F1: model_fields_set으로 미지정과 명시적 삭제를 구분 — ""/null은 NULL 기록.
    for k in ("model", "reasoning", "tier"):
        if k in p.model_fields_set:
            fields[k] = getattr(p, k) or None
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
        # 기수락 멱등키 (TT 개선#1 요구 3): 동일 issue+agent+attempt+본문 재전송은
        # 코멘트·dispatch 행 없이 기존 행을 반환한다. error 상태 행은 재전달 대상 —
        # 동일 did로 재시도(러너 장부 키 안정성 유지), 신규 코멘트는 다시 만들지 않는다.
        idem = _idem_key(issue_id, p.agent, issue["execution_attempt"], p.message)
        existing = c.execute("SELECT * FROM dispatches WHERE idem_key=? "
                             "ORDER BY id DESC LIMIT 1", (idem,)).fetchone()
        if existing is not None and not _redeliver_wanted(existing):
            return JSONResponse(status_code=200, content=dict(existing))
        redeliver = existing is not None
        if not redeliver:
            # resume context는 신규 행 INSERT 전에 조회해야 이전 회차를 가리킨다
            # (INSERT 후 조회하면 방금 만든 빈 context 행이 최신이 되어버림).
            prev = c.execute("SELECT context FROM dispatches WHERE issue_id=? AND agent=? "
                             "ORDER BY id DESC LIMIT 1", (issue_id, p.agent)).fetchone()
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, p.author, p.message, dbmod.now()))
            # R5: 행 생성 시점부터 resume context를 상속해 둔다 — 전달 실패(error)나
            # 전달 전 크래시로 끝나도 재전달 payload가 원래 context를 잃지 않는다.
            # 전달 성공 시에만 웹훅 응답 token으로 갱신된다.
            inh_ctx = prev["context"] if prev else ""
            try:
                did = c.execute("INSERT INTO dispatches (issue_id, agent, author, message, context, status, ts, model, idem_key, attempt, delivery_lease) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, p.agent, p.author, p.message, inh_ctx, "queued", dbmod.now(),
                                 ag["model"] or "", idem, issue["execution_attempt"],
                                 dbmod.now())).lastrowid
            except sqlite3.IntegrityError:
                # 동시 중복 POST — 유니크 인덱스가 원자적으로 승자를 결정한다.
                c.rollback()
                row = c.execute("SELECT * FROM dispatches WHERE idem_key=? "
                                "ORDER BY id DESC LIMIT 1", (idem,)).fetchone()
                if row is None:
                    raise HTTPException(409, "dispatch idempotency race")
                if not _redeliver_wanted(row):
                    return JSONResponse(status_code=200, content=dict(row))
                # 충돌 승자 행도 동일 선점 경로로 (5차 리뷰 R4): lease 선점 없이
                # 진행하면 다른 caller의 신선한 복구 lease를 우회해 이중 웹훅이 된다.
                claimed = _claim_redelivery(c, row)
                if claimed is None:
                    row = c.execute("SELECT * FROM dispatches WHERE id=?",
                                    (row["id"],)).fetchone()
                    return JSONResponse(status_code=200, content=dict(row))
                prev = claimed
                did = row["id"]  # 크래시 잔재 queued — 재전달 경로로 계속
        else:
            did = existing["id"]
            # R4/R5: 재전달 선점은 _claim_redelivery 단일 경로 — 자격(status)과
            # lease 신선도를 원자 검사하고, resume context는 이 행에 보존된 접수
            # 시점 값(이후 다른 dispatch 유입과 무관). 선점 실패 = 이미 전달됐거나
            # 전달 진행 중 → 현재 행으로 기수락 200.
            claimed = _claim_redelivery(c, existing)
            if claimed is None:
                row = c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
                return JSONResponse(status_code=200, content=dict(row))
            prev = claimed
        tail = dbmod.comments_of(c, issue_id)[-20:]
        c.commit()
    payload = _dispatch_payload(issue, ag, p, did, issue_id, request, ctx, prev, tail)
    status, detail, dctx = "ok", "", ""
    try:
        code, dctx = service.deliver(ag["base_url"], ag["secret"], payload)
        if code >= 300:
            status, detail = "error", f"HTTP {code}"
    except Exception as e:
        status, detail = "error", f"{type(e).__name__}: {e}"[:300]
    with ctx.con() as c:
        # 실패(error) 시 context는 생성 시 상속값을 보존(R5) — 성공 시에만 응답 token으로 갱신
        if status == "ok":
            c.execute("UPDATE dispatches SET status=?, detail=?, context=?, delivery_lease=NULL WHERE id=?",
                      (status, detail, dctx, did))
        else:
            c.execute("UPDATE dispatches SET status=?, detail=?, delivery_lease=NULL WHERE id=?",
                      (status, detail, did))
        c.execute("UPDATE agents SET last_ok=?, last_err=? WHERE name=?",
                  (dbmod.now() if status == "ok" else ag["last_ok"],
                   "" if status == "ok" else detail, p.agent))
        if status == "error":
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, "tt-server", f"⚠ hook dispatch #{did} → {p.agent} 실패: {detail}", dbmod.now()))
        c.commit()
        row = c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    if redeliver:
        return JSONResponse(status_code=200, content=dict(row))
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
            # 완료 전환 최종 판정 — 서버 attempt CAS (TT 개선#1 요구 2, if-match).
            # 종료 상태(finished/failed)는 비교와 갱신을 단일 조건부 UPDATE로 원자화한다
            # (R2: SELECT 후 UPDATE 사이 claim 끼워들기 창 제거) — dispatch가 생성된
            # 회차(attempt 스냅샷)보다 이슈의 현재 execution_attempt가 앞서면 갱신
            # 자체가 이뤄지지 않고 409로 거부된다. "이전 회차가 현재 회차를 덮는" 부류가
            # 구조적으로 사라진다. attempt 스냅샷이 없는 구형 행(NULL)은 기존 동작 유지.
            # running/stalled 등 비종료 진행은 last-write-wins 그대로(러너 stall_check
            # 소유 — 서버 재계산 금지).
            if p.state in ("finished", "failed"):
                # 이미 접수된 동일 보고(동일 dispatch+session)의 재시도는 투영 갱신
                # 없이 기수락한다(5차 리뷰 R13) — 끼어든 세션의 응답 유실 재시도가
                # 새 회차의 running 투영을 종료로 덮지 않게 한다. 코멘트 유무와
                # 무관하게 run_state/ended_at/session을 그대로 둔다.
                if p.comment and p.session and c.execute(
                        "SELECT 1 FROM dispatch_reports WHERE dispatch_id=? AND session=?",
                        (dispatch_id, p.session)).fetchone():
                    row = c.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
                    return dict(row)
                # report_session은 코멘트 접수와만 연결한다(5차 리뷰 R11) — STALL-kill·
                # TIMEOUT·release 등 코멘트 없는 종료 투영은 접수 증거가 아니므로
                # 기록하면 _terminal_recorded가 오패정한다.
                sets = ("run_state=?, ended_at=?, report_session=?"
                        if (p.comment and p.session) else "run_state=?, ended_at=?")
                args = ((p.state, ts, p.session) if (p.comment and p.session)
                        else (p.state, ts))
                res = c.execute(
                    "UPDATE dispatches SET %s WHERE id=? AND "
                    "(attempt IS NULL OR attempt >= "
                    "(SELECT i.execution_attempt FROM issues i WHERE i.id=dispatches.issue_id))" % sets,
                    (*args, dispatch_id))
                if res.rowcount != 1:
                    raise HTTPException(
                        409, f"stale round report: dispatch#{dispatch_id} attempt={row['attempt']} "
                             f"< issue current execution_attempt — 이전 회차 종료 보고 거부")
                if p.comment:
                    # 원자적 dedup (R10): 접수 이력을 (dispatch, session) 유니크 키로
                    # 보존한다 — 마지막 세션만 기억하면 A→B-r2→A 재시도에서 A 보고가
                    # 다시 접수된다(4차 리뷰 R10). INSERT OR IGNORE의 rowcount로 한 번만
                    # 접수: 동일 회차 재시도는 200 멱등, 새 회차는 새 이력으로 정상 접수.
                    dup = None
                    if p.session:  # 세션 없는 보고는 회차 식별 불가 — dedup 없음
                        dup = c.execute(
                            "INSERT OR IGNORE INTO dispatch_reports (dispatch_id, session, ts) "
                            "VALUES (?,?,?)", (dispatch_id, p.session, dbmod.now()))
                    if dup is None or dup.rowcount:
                        # 종료 투영과 완료 보고를 같은 트랜잭션에 접수 (R3: CAS 통과 후
                        # 별도 /comments 사이 회차 변경 창 제거 — 409면 코멘트도 없다).
                        # 접수 코어 공유로 버전 갱신·blocked 알림 부수효과 보존 (R9)
                        service.record_comment(c, issue_id,
                                               (p.author or "runner").strip() or "runner",
                                               p.comment[:4000])
            else:
                fields["run_state"] = p.state
                if p.state in ("running", "stalled"):
                    # 진행 병기 허용 상태: ts/tail 유무와 무관하게 liveness 시각 갱신
                    # (동일 상태 재전송도 진행으로 봄 — 러너는 변경/주기 모두 허용)
                    fields["last_progress_at"] = ts
                    if p.tail is not None:
                        fields["last_tail"] = p.tail[:500]  # 서버 클램프 (422 아님)
                    if not row["started_at"]:
                        fields["started_at"] = ts
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
