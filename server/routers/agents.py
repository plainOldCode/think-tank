"""에이전트·디스패치 라우터 — agent registry, hook dispatch, runner progress."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

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


def _stale_dispatch(row, issue) -> bool:
    """9PDD: 재전달 자격이 있어도 카드가 종료됐거나 행이 이전 회차면 안 된다.
    (status ∈ error/queued는 _redeliver_wanted가 이미 검사.)"""
    if issue["state"] in ("done", "cancelled") or issue["archived"]:
        return True
    at = row["attempt"]
    return at is not None and at < issue["execution_attempt"]


def _close_superseded_row(c, row, via: str) -> None:
    """9PDD: 낡은 행을 superseded로 닫고 이벤트를 남긴다(증거 코멘트 없음)."""
    c.execute("UPDATE dispatches SET status='superseded' WHERE id=?", (row["id"],))
    service.log_event(c, "dispatch.updated", "dispatch", row["id"],
                      {"status": "superseded", "via": via})


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


def _dispatch_payload(issue, ag, p, did, issue_id, request, ctx, prev, tail,
                      agent_name=None):
    return {
        "dispatch_id": did, "issue_id": issue_id, "issue_title": issue["title"],
        # 러너가 페이로드의 agent로 프로필을 정확 매칭한다 — 등록명(폴백 해석 후)을
        # 실어 보내야 codex@m2max → codex 폴백이 실제 세션을 연다(WGT4 리뷰 R1).
        "agent": agent_name or p.agent, "author": p.author, "message": p.message,
        "context": "" if p.fresh else (prev["context"] if prev else ""),
        "comments": tail,
        "tt_url": str(request.base_url).rstrip("/"),
        "model": ag["model"] or "",  # RZ20: 감사 추적 — 이 회차가 어떤 모델로 실행되는지 선언값
        "work_contract": (p.work_contract if p.work_contract is not None else
                          (json.loads(issue["work_contract"]) if issue["work_contract"] else ctx.contract)),
        "execution_attempt": issue["execution_attempt"],
    }


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def _agent_out(r):
    """K4Q7(M53XP2ZA-K4Q7) — 에이전트 직렬화에서 secret을 완전히 제거.

    webhook secret은 소유자(에이전트·러너 설정)만 보유한다. 읽기 경로가 없으므로
    마스킹이 아니라 키 자체를 뺀다 — 오탈 유출·로그·캐시 경로를 동시에 끊는다."""
    d = {**dict(r), "aliases": _aliases_out(r)}
    d.pop("secret", None)
    # M3B5JHGP: poll 에이전트의 빈 base_url은 '없음'으로 직렬화(구 스키마 NOT NULL 호환)
    if d.get("base_url") == "":
        d["base_url"] = None
    return d


@router.get("/agents")
def list_agents(ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    return [_agent_out(r) for r in rows]


def _validate_receive_mode(mode, interval, current_interval=None, runner_name=None):
    """RQRT(M4MGV3CM) — 수신 계약 선언 검증. mode가 None이면 판정도 없다."""
    if mode is None:
        return
    m = (mode or "").strip().lower()
    if m and m not in ("hook", "watcher", "relay", "poll"):
        raise HTTPException(422, "receive_mode은 hook|watcher|relay|poll 중 하나")
    if m == "watcher" and not ((interval and interval >= 1) or (current_interval and current_interval >= 1)):
        raise HTTPException(422, "watcher 모드는 watcher_interval(분) 필수 — 주기 폴링 계약의 명시 의무(RQRT)")
    if m == "poll" and not (runner_name or "").strip():
        raise HTTPException(422, "poll 모드는 runner_name 필수 — pending inbox를 폴링할 소속 러너 선언(M3B5JHGP)")


def _normalize_aliases(aliases):
    """Z1HP(M4CKEJGX) — 별칭 형식 검증. 값 자체만 판정(충돌은 con 있는 callers에서).
    DX88(M5283J3A-DX88): 내부 @는 허용(codex@m2pro류 수령자 표기→등록명 매핑용).
    공백·쉼표·선행 @만 금지 — 멘션 토큰(@word)과의 충돌 방지가 원래 취지."""
    out = []
    for a in aliases or []:
        a = a.strip()
        if not a:
            raise HTTPException(422, "별칭은 비어 있을 수 없다")
        if a.startswith("@") or re.search(r"[\s,]", a):
            raise HTTPException(422, f"별칭 '{a}' — 공백·쉼표·선행 @는 사용할 수 없다")
        if a in out:
            raise HTTPException(422, f"별칭 '{a}' 중복")
        out.append(a)
    return out


def _check_alias_conflicts(c, aliases, self_name=None):
    """다른 에이전트의 이름·별칭과 충돌하는 별칭은 422 — 사전 매핑의 결정론 보장.
    자기 이름과 동일한 별칭도 중복으로 거부(등록명이 언제나 후보라 중복 선언은 노이즈)."""
    for a in aliases:
        if self_name is not None and a == self_name:
            raise HTTPException(422, f"별칭 '{a}'은(는) 자기 이름과 동일하다")
        row = c.execute("SELECT name FROM agents WHERE name=?", (a,)).fetchone()
        if row:
            raise HTTPException(422, f"별칭 '{a}'은(는) 이미 에이전트 이름이다")
        # 전수 디코딩 비교 — LIKE 사전필터는 JSON 이스케이프(따옴표·역슬래시) 값에서
        # 후보 행을 놓친다(리뷰 P2). agents 테이블은 소규모라 전수 스캔 비용이 무의미.
        for r in c.execute("SELECT name, aliases FROM agents").fetchall():
            if a in _aliases_out(r) and (self_name is None or r["name"] != self_name):
                raise HTTPException(422, f"별칭 '{a}'은(는) {r['name']}의 별칭이다")


def _aliases_out(row):
    try:
        return json.loads(row["aliases"] or "[]")
    except ValueError:
        return []


@router.post("/agents", status_code=201, summary="Register a webhook agent (receives dispatch POSTs at base_url)", description="base_url must be a reachable HTTP endpoint (hook mode). Agents without a listener cannot receive dispatch — see /api.md integration table. release_hook=true 선언 시 카드 terminalize 시 release 명령(x-tt-command: release)도 수신 — runner류만 켠다. notify_hook=true 시 blocked(waiting_for=human) Level4 알림(x-tt-command: notify) 수신 — 알림 주입 어댑터(Hermes)용. RQRT: receive_mode 필수 선언(hook|watcher+watcher_interval|relay) — 미선언 claim 409.")
def add_agent(p: AgentIn, ctx: Ctx = Depends(get_ctx)):
    name = p.name.strip()
    _validate_receive_mode(p.receive_mode, p.watcher_interval, runner_name=p.runner_name)
    # M3B5JHGP: poll 에이전트는 base_url이 없다(러너 inbox로 수령).
    is_poll = ((p.receive_mode or "").strip().lower() == "poll")
    if not name or (not is_poll and not service.valid_url(p.base_url)) or (p.base_url and not service.valid_url(p.base_url)):
        raise HTTPException(422, "name 필수, base_url은 http(s)만 허용(poll 모드는 생략)")
    with ctx.con() as c:
        if c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"agent {name} 이미 등록됨")
        # 새 등록명도 기존 별칭과 충돌하면 거부(리뷰 P2) — 안 거부하면 별칭 소유자가
        # 멘션을 계속 가져가 새 에이전트 본명 멘션이 다른 에이전트로 간다.
        for r in c.execute("SELECT name, aliases FROM agents").fetchall():
            if name in _aliases_out(r):
                raise HTTPException(422, f"이름 '{name}'은(는) {r['name']}의 별칭이다")
        aliases = _normalize_aliases(p.aliases)
        _check_alias_conflicts(c, aliases, self_name=name)
        c.execute("INSERT INTO agents (name, base_url, secret, enabled, release_hook, notify_hook, model, reasoning, tier, receive_mode, watcher_interval, runner_name, aliases, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (name, p.base_url or ("" if is_poll else None), p.secret, 1 if p.enabled else 0, 1 if p.release_hook else 0,
                   1 if p.notify_hook else 0, p.model, p.reasoning, p.tier,
                   (p.receive_mode or "").strip().lower() or None, p.watcher_interval,
                   (p.runner_name or "").strip() or None,
                   json.dumps(aliases, ensure_ascii=False) if aliases else "", dbmod.now()))
        c.commit()
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    # K4Q7(M53XP2ZA-K4Q7): 등록 응답도 직렬화 관문 — secret 원문 반환 차단
    return _agent_out(row)


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
    # Z1HP: aliases — None=유지, []=전면 삭제(빈 목록이 유효한 '클리어'라 fields_set 불필요)
    new_aliases = _normalize_aliases(p.aliases) if p.aliases is not None else None
    with ctx.con() as c:
        if not c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            raise HTTPException(404, f"agent {name} not found")
        if new_aliases is not None:
            _check_alias_conflicts(c, new_aliases, self_name=name)
            fields["aliases"] = json.dumps(new_aliases, ensure_ascii=False) if new_aliases else ""
        # RQRT(M4MGV3CM): 수신 계약 — watcher로 바꿀 때 기존 간격이 있으면 유지.
        # 명시적 삭제는 fields_set으로 구분(RZ20-F1 패턴) — None PATCH = 선언 취소.
        if p.receive_mode is not None or "receive_mode" in p.model_fields_set \
                or p.watcher_interval is not None or "runner_name" in p.model_fields_set:
            row = c.execute("SELECT watcher_interval FROM agents WHERE name=?",
                            (name,)).fetchone()
            _validate_receive_mode(p.receive_mode, p.watcher_interval,
                                   current_interval=(row or {}).get("watcher_interval")
                                   if isinstance(row, dict) else
                                   (row["watcher_interval"] if row else None),
                                   runner_name=(p.runner_name if "runner_name" in p.model_fields_set
                                                else None) or "")
            if "receive_mode" in p.model_fields_set:
                fields["receive_mode"] = (p.receive_mode or "").strip().lower() or None
            if p.watcher_interval is not None:
                if p.watcher_interval < 1:
                    raise HTTPException(422, "watcher_interval은 1분 이상")
                fields["watcher_interval"] = p.watcher_interval
            if "runner_name" in p.model_fields_set:
                fields["runner_name"] = (p.runner_name or "").strip() or None
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE agents SET {sets} WHERE name=?", (*fields.values(), name))
            c.commit()
        row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    # K4Q7(M53XP2ZA-K4Q7): PATCH 응답도 직렬화 관문 — 비-secret 변경만으로도
    # 저장된 secret 원문이 응답에 실리는 우회를 막는다(agy P1).
    return _agent_out(row)


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
        # 9PDD: 종료 카드에 대한 신규 dispatch도 차단 — 멱등키에 회차가 포함돼
        # 기존 행 조회로는 못 걸러낸다(QN0H: 재전달이 신규 행으로 카드 재개장).
        if issue["state"] in ("done", "cancelled") or issue["archived"]:
            raise HTTPException(409, f"issue {issue_id} 종료 상태 — dispatch 불가")
        ag = c.execute("SELECT * FROM agents WHERE name=?", (p.agent,)).fetchone()
        if not ag and "@" in p.agent:
            # 본명@호스트 형식 assignee(tt CLI 기본 신원)를 등록 에이전트 단축명으로
            # 폴백 — probe review-fix가 assignee에 재dispatch할 때 404로 무한 실패했던
            # 버그(M4CJG2A5-WGT4). 정확명이 있으면 항상 우선한다.
            ag = c.execute("SELECT * FROM agents WHERE name=?",
                           (p.agent.split("@", 1)[0],)).fetchone()
        if not ag:
            raise HTTPException(404, f"agent {p.agent} 미등록 — POST /agents")
        if not ag["enabled"]:
            raise HTTPException(409, f"agent {p.agent} 비활성")
        # 기수락 멱등키 (TT 개선#1 요구 3): 동일 issue+agent+attempt+본문 재전송은
        # 코멘트·dispatch 행 없이 기존 행을 반환한다. error 상태 행은 재전달 대상 —
        # 동일 did로 재시도(러너 장부 키 안정성 유지), 신규 코멘트는 다시 만들지 않는다.
        agent_name = ag["name"]  # 폴백 해석 후 실제 등록명 — 장부·이벤트는 등록명 기준
        idem = _idem_key(issue_id, p.agent, issue["execution_attempt"], p.message)
        existing = c.execute("SELECT * FROM dispatches WHERE idem_key=? "
                             "ORDER BY id DESC LIMIT 1", (idem,)).fetchone()
        # 9PDD(M54X0X19-9PDD): 종료 카드(done/cancelled/archived)나 이전 회차 행은
        # 재전달하지 않는다 — 받은 에이전트가 claim하면 종료 카드가 다시 열린다
        # (QN0H 사건). 행은 superseded로 닫아 재전달 대상에서 제거하고 409로 응답.
        if existing is not None and _stale_dispatch(existing, issue):
            _close_superseded_row(c, existing, "stale-redeliver")
            c.commit()
            raise HTTPException(409, "dispatch superseded: 카드 종료 또는 회차 전진")
        if existing is not None and not _redeliver_wanted(existing):
            return JSONResponse(status_code=200, content=dict(existing))
        redeliver = existing is not None
        if not redeliver:
            # DW0P(M5283NC3-DW0P): 부모 epic의 standing orders를 메시지에 그대로
            # 첨부 — 코멘트와 러너 payload가 같은 본문을 받는다(재전송 멱등키는
            # 원본 메시지 기준 유지).
            _so = service.standing_orders(c, issue)
            if _so:
                p.message = p.message + "\n\n[standing orders]\n" + _so
            # Y5EG(M53PBECP-Y5EG): brief 4칸도 지시문에 — 멱등키는 원본 메시지 기준
            _bf = service.brief_block(issue)
            if _bf:
                p.message = p.message + "\n\n" + _bf
            # resume context는 신규 행 INSERT 전에 조회해야 이전 회차를 가리킨다
            # (INSERT 후 조회하면 방금 만든 빈 context 행이 최신이 되어버림).
            prev = c.execute("SELECT context FROM dispatches WHERE issue_id=? AND agent=? "
                             "ORDER BY id DESC LIMIT 1", (issue_id, agent_name)).fetchone()
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, p.author, p.message, dbmod.now()))
            service.log_event(c, "comment.added", "issue", issue_id,
                              {"author": p.author, "preview": p.message[:120]})
            # R5: 행 생성 시점부터 resume context를 상속해 둔다 — 전달 실패(error)나
            # 전달 전 크래시로 끝나도 재전달 payload가 원래 context를 잃지 않는다.
            # 전달 성공 시에만 웹훅 응답 token으로 갱신된다.
            # 12MZ 리뷰 R5: fresh=True면 상속을 끊는다 — 세션 사망 재발령이 죽은/과거
            # 세션을 계속(reuse)하지 않게(러너는 빈 context에서 신규 세션으로 폴백).
            inh_ctx = (prev["context"] if prev else "") if not p.fresh else ""
            try:
                did = c.execute("INSERT INTO dispatches (issue_id, agent, author, message, context, status, ts, model, idem_key, attempt, delivery_lease) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (issue_id, agent_name, p.author, p.message, inh_ctx, "queued", dbmod.now(),
                                 ag["model"] or "", idem, issue["execution_attempt"],
                                 dbmod.now())).lastrowid
                service.log_event(c, "dispatch.created", "dispatch", did,
                                  {"agent": agent_name, "issue_id": issue_id,
                                   "preview": p.message[:120]})
            except sqlite3.IntegrityError:
                # 동시 중복 POST — 유니크 인덱스가 원자적으로 승자를 결정한다.
                c.rollback()
                row = c.execute("SELECT * FROM dispatches WHERE idem_key=? "
                                "ORDER BY id DESC LIMIT 1", (idem,)).fetchone()
                if row is None:
                    raise HTTPException(409, "dispatch idempotency race")
                if _stale_dispatch(row, issue):
                    _close_superseded_row(c, row, "stale-redeliver")
                    c.commit()
                    raise HTTPException(409, "dispatch superseded: 카드 종료 또는 회차 전진")
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
    payload = _dispatch_payload(issue, ag, p, did, issue_id, request, ctx, prev, tail,
                                agent_name=agent_name)
    # M3B5JHGP: poll 에이전트는 훅 POST가 없다 — 큐에 남고 소속 러너의 pending
    # 폴링이 꺼내 간다(단일 채널, 이중 전달 없음).
    poll_mode = (ag["receive_mode"] or "").strip().lower() == "poll"
    status, detail, dctx = ("queued", "", "") if poll_mode else ("ok", "", "")
    if not poll_mode:
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
        elif status == "error":
            c.execute("UPDATE dispatches SET status=?, detail=?, delivery_lease=NULL WHERE id=?",
                      (status, detail, did))
        if not poll_mode:
            c.execute("UPDATE agents SET last_ok=?, last_err=? WHERE name=?",
                      (dbmod.now() if status == "ok" else ag["last_ok"],
                       "" if status == "ok" else detail, ag["name"]))
        if status == "error":
            fail_body = f"⚠ hook dispatch #{did} → {p.agent} 실패: {detail}"
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, "tt-server", fail_body, dbmod.now()))
            service.log_event(c, "comment.added", "issue", issue_id,
                              {"author": "tt-server", "preview": fail_body[:120]})
        c.commit()
        row = c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
    if redeliver:
        return JSONResponse(status_code=200, content=dict(row))
    return dict(row)


@router.get("/agents/{runner_name}/pending",
            summary="M3B5JHGP: poll-runner inbox — 소속 poll 에이전트의 queued dispatch를 훅 없이 꺼낸다",
            description="러너가 자기 RUNNER_NAME으로 폴링(60s). 인증은 소속 poll 에이전트의 "
                        "secret 중 하나와 일치하는 Bearer. 반환과 동시에 status=ok로 마킹해 "
                        "이중 전달을 원천 차단한다. payload는 webhook과 동일한 형태.")
def pending_inbox(runner_name: str, request: Request, ctx: Ctx = Depends(get_ctx)):
    auth = (request.headers.get("authorization") or "").replace("Bearer ", "")
    with ctx.con() as c:
        agents = c.execute("SELECT name, secret FROM agents WHERE runner_name=? "
                           "AND enabled=1 AND receive_mode='poll'", (runner_name,)).fetchall()
        if not agents or not any((a["secret"] or "") and auth == a["secret"] for a in agents):
            raise HTTPException(401, "pending inbox: 소속 러너 시크릿 불일치")
        names = [a["name"] for a in agents]
        qs = ",".join("?" * len(names))
        rows = c.execute(f"SELECT * FROM dispatches WHERE agent IN ({qs}) AND status='queued' "
                         "ORDER BY id", names).fetchall()
        out = []
        for d in rows:
            issue = service.get_issue(c, d["issue_id"])
            ag = c.execute("SELECT * FROM agents WHERE name=?", (d["agent"],)).fetchone()
            tail = dbmod.comments_of(c, d["issue_id"])[-20:]
            p = SimpleNamespace(author=d["author"], message=d["message"],
                                fresh=False, work_contract=None)
            out.append(_dispatch_payload(issue, ag, p, d["id"], d["issue_id"], request,
                                         ctx, d, tail, agent_name=d["agent"]))
            c.execute("UPDATE dispatches SET status='ok', detail='poll-delivered (M3B5JHGP)', "
                      "delivery_lease=NULL WHERE id=?", (d["id"],))
        if rows:
            c.execute(f"UPDATE agents SET last_ok=? WHERE name IN ({qs})", (dbmod.now(), *names))
            c.commit()
    return out


@router.get("/issues/{issue_id}/dispatches")
def list_dispatches(issue_id: str, ctx: Ctx = Depends(get_ctx)):
    """Dispatch rows for one card.

    Old-round active rows are finished in this read (M54E72RQ-SXGS). Cards already
    stuck before the round-advance close shipped are healed the next time the probe
    or the board lists dispatches — no manual edit. The response is the healed set.
    Comments and report receipts are not created.
    """
    with ctx.con() as c:
        c.execute("BEGIN IMMEDIATE")
        service.get_issue(c, issue_id)
        service.close_superseded_dispatches(c, issue_id)
        rows = c.execute("SELECT * FROM dispatches WHERE issue_id=? ORDER BY id",
                         (issue_id,)).fetchall()
        c.commit()
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
        c.execute("BEGIN IMMEDIATE")
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
        issue = service.get_issue(c, issue_id)
        closed = issue["state"] in ("done", "cancelled") or issue["archived"]
        current_attempt = row["attempt"] is None or row["attempt"] >= issue["execution_attempt"]
        # Closed issues may accept old physical cleanup, but historical completion
        # comments must never be added to the current attempt's evidence.
        accept_comment = bool(p.comment) and current_attempt
        if closed and p.state in config.ACTIVE_RUN_STATES:
            service.finish_issue_dispatches(c, issue_id)
            return dict(c.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone())
        fields: dict = {}
        ts = p.ts if p.ts else dbmod.now()
        if p.state is not None:
            # 완료 전환 최종 판정 — 서버 attempt CAS (TT 개선#1 요구 2, if-match).
            # 종료 상태(finished/failed)는 비교와 갱신을 단일 조건부 UPDATE로 원자화한다
            # (R2: SELECT 후 UPDATE 사이 claim 끼워들기 창 제거) — dispatch가 생성된
            # 회차(attempt 스냅샷)보다 이슈의 현재 execution_attempt가 앞서면
            # 투영은 닫되(kill/timeout/failed/done) 코멘트·report_session·
            # dispatch_reports는 남기지 않는다. 현재 회차 보고가 CAS에 지면 409.
            # 이미 done/cancelled/archived인 카드의 물리 종료도 같은 예외이며
            # 구회차 코멘트는 받지 않는다.
            # attempt 스냅샷이 없는 구형 행(NULL)은 기존 동작 유지.
            # running/stalled 등 비종료 진행은 last-write-wins 그대로(러너 stall_check
            # 소유 — 서버 재계산 금지).
            if p.state in ("finished", "failed"):
                if not current_attempt and p.comment and p.session:
                    receipt = c.execute(
                        "SELECT 1 FROM dispatch_reports WHERE dispatch_id=? AND session=?",
                        (dispatch_id, p.session)).fetchone()
                    if receipt:
                        return dict(row)  # previously accepted retry, projection unchanged
                if not current_attempt:
                    # M54E72RQ-SXGS: a kill/timeout/failed/done from the round this
                    # dispatch was created in must close the projection. The comment
                    # is not evidence for the current round — no comment, no
                    # report_session, no dispatch_reports row. A different live
                    # session already on this row is a newer projection; an older
                    # kill must not overwrite it.
                    if (p.session and row["session"] and p.session != row["session"]
                            and row["run_state"] in config.ACTIVE_RUN_STATES):
                        return dict(row)
                    res = c.execute(
                        "UPDATE dispatches SET run_state=?, ended_at=? WHERE id=?",
                        (p.state, ts, dispatch_id))
                    if res.rowcount == 1:
                        service.log_event(c, "dispatch.updated", "dispatch", dispatch_id,
                                          {"run_state": p.state, "via": "stale-round"})
                else:
                    # 보고 선점 선(先)결제 (6차 리뷰 R13 — 원자성): 접수 이력 (dispatch,
                    # session) 유니크 INSERT OR IGNORE가 그 자체로 게이트다. 이력 SELECT와
                    # 투영 갱신을 분리하면 경합 창(이력 부재 판독 → 타 요청 접수 → 재개)에서
                    # 이전 세션 재시도가 새 회차 running 투영을 종료로 덮는다. 승자만 아래
                    # 투영 갱신·코멘트 접수로 진행하고, 패자는 이미 접수된 보고의 재시도로
                    # 투영(run_state/ended_at/session) 무변경 기수락 200. 세션 없는 보고는
                    # 회차 식별 불가 — 선점 없이 기존 동작(CAS + 매 접수).
                    if accept_comment and p.session:
                        claimed = c.execute(
                            "INSERT OR IGNORE INTO dispatch_reports (dispatch_id, session, ts) "
                            "VALUES (?,?,?)", (dispatch_id, p.session, dbmod.now()))
                        if claimed.rowcount == 0:
                            row = c.execute("SELECT * FROM dispatches WHERE id=?",
                                            (dispatch_id,)).fetchone()
                            return dict(row)
                    # report_session은 코멘트 접수와만 연결한다(5차 리뷰 R11) — STALL-kill·
                    # TIMEOUT·release 등 코멘트 없는 종료 투영은 접수 증거가 아니므로
                    # 기록하면 _terminal_recorded가 오패정한다.
                    sets = ("run_state=?, ended_at=?, report_session=?"
                            if (accept_comment and p.session) else "run_state=?, ended_at=?")
                    args = ((p.state, ts, p.session) if (accept_comment and p.session)
                            else (p.state, ts))
                    res = c.execute(
                        "UPDATE dispatches SET %s WHERE id=? AND "
                        "(attempt IS NULL OR attempt >= "
                        "(SELECT i.execution_attempt FROM issues i WHERE i.id=dispatches.issue_id) "
                        "OR EXISTS (SELECT 1 FROM issues i WHERE i.id=dispatches.issue_id "
                        "AND (i.state IN ('done','cancelled') OR i.archived=1)))" % sets,
                        (*args, dispatch_id))
                    if res.rowcount == 1:
                        service.log_event(c, "dispatch.updated", "dispatch", dispatch_id,
                                          {"run_state": p.state})
                    if res.rowcount != 1:
                        # 409 — with 블록 예외 롤백으로 방금 선점한 보고 이력도 함께 취소된다
                        # (미접수 보고의 이력 잔재 방지). 코멘트도 접수되지 않는다.
                        raise HTTPException(
                            409, f"stale round report: dispatch#{dispatch_id} attempt={row['attempt']} "
                                 f"< issue current execution_attempt — 이전 회차 종료 보고 거부")
                    if accept_comment:
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
