"""공유 서비스 로직 — 라우터 간 공통 헬퍼 (라우팅 routers/와 분리).

관례: 함수는 DB 커넥션 `c`를 명시 인자로 받는다. 앱 인스턴스별 상태(DB 경로,
작업 계약 스냅샷)는 Ctx로 전달한다.
"""
import json
import os
import re
import sqlite3
import threading
import urllib.request

from fastapi import HTTPException

import config
import db as dbmod
import identity
from verification import legacy_result

_notify_reg_lock = threading.Lock()
# db_path → {"stop": Event, "refs": 살아있는 앱 수, "thread": Thread, "polls": 폴링 수}
# 소유자는 notify_loop — 등록 제거도 워커 본인이 한다(리뷰 R5 전송 중 종료 경계).
_notify_workers: dict = {}

ISSUE_ID_RE = config.ISSUE_ID_RE

# 리뷰 계약 (M42KC1XR-2DD1 R2): 리뷰 dispatch·claim-review는 구현 계약 대신 이것을 전달 —
# 판정이 목적임을 명시해 에이전트가 구현을 시도하지 않게 한다.
REVIEW_CONTRACT = {
    "version": "review-v2",
    "role": "reviewer",
    "purpose": "구현이 아니라 판정이 목적 — TT 제출 보고와 산출물을 독립 검증한다. 코드를 고치지 않는다.",
    "instructions": "리뷰 방식: 변경 파일 통독 + 변경 심볼 grep으로 호출자 확인(공용 모듈은 필수). "
                    "판정 기준: 계약 준수·시크릿 노출·테스트 적절성·놓친 엣지. "
                    "판정 정본은 TT 카드 코멘트, PR 코멘트는 선택적 미러다. "
                    "첫 줄 'review: approve' 또는 'review: request-changes', 둘째 줄은 "
                    "GET /issues/ID의 review_subject(TT#ID@aN/hash)를 그대로 쓴다. "
                    "현재 보고와 완료 기준 및 산출물 위치를 확인하고 접근 불가·미검증을 승인하지 않는다. "
                    "delivery.mode=pr이면 보고의 전체 head_sha와 PR 최신 head가 일치해야 승인한다. "
                    "head가 달라졌으면 수정 보고 재제출을 요구한다. PR 선택 작업은 승인 후 병합 확인이 종결 조건이다. "
                    "PR 없는 local/Gitea 작업도 동일하다. 레거시 PR 전용 요청만 PR#<n>@<sha8>를 쓴다. "
                    "TT 코멘트 author는 자기 에이전트명.",
}


class Ctx:
    """앱 인스턴스 컨텍스트: DB 경로 + 스냅샷된 작업 계약."""

    def __init__(self, db_path: str, contract: dict):
        self.db_path = db_path
        self.contract = contract

    def con(self):
        return dbmod.connect(self.db_path)


def get_issue(c, issue_id):
    row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"issue {issue_id} not found")
    return row


def resolve_agent_identity(c, cand: str) -> str | None:
    """DX88(M5283J3A-DX88) — 수령자 정규화: 등록명 정확 일치 우선, Z1HP 별칭
    사전(agents.aliases) 매칭 시 등록명 반환. 미매칭 None. 공백은 호출부에서 정리.

    3QMV — 규칙 본체는 identity.resolve 단일 소스. 서버 규약은 별칭까지만
    해석(가족 폴백 없음)이므로 family_fallback=False로 위임한다."""
    s = (cand or "").strip()
    if not s:
        return None
    rows = [{"name": r["name"],
             "aliases": json.loads(r["aliases"] or "[]")}
            for r in c.execute("SELECT name, aliases FROM agents").fetchall()]
    return identity.resolve(s, rows, family_fallback=False)


def bump(c, issue_id, fields, expected_version=None):
    row = get_issue(c, issue_id)
    if expected_version is not None and row["version"] != expected_version:
        raise HTTPException(409, f"version conflict: have {row['version']}, expected {expected_version}")
    fields = {**fields, "version": row["version"] + 1, "updated_at": dbmod.now()}
    sets = ", ".join(f"{k}=?" for k in fields)
    res = c.execute(f"UPDATE issues SET {sets} WHERE id=? AND version=?", (*fields.values(), issue_id, row["version"]))
    if res.rowcount == 1:
        log_event(c, "issue.updated", "issue", issue_id,
                  {"state": fields.get("state", row["state"]),
                   "fields": sorted(fields)})
    if res.rowcount != 1:
        raise HTTPException(409, "concurrent update, retry")
    finish_issue_dispatches(c, issue_id)
    close_superseded_dispatches(c, issue_id)
    c.commit()


def original_instruction(message):
    """첨부(standing orders·brief) 이전 원본 지시문 복원(2NHN) — 게이트 스코프.

    러너의 파괴적 패턴 게이트·승인 판정은 첨부가 섞인 본문이 아니라 원본을 봐야
    한다. DB 스키마 변경 없이 전달 본문에서 섹션 마커 앞부분으로 복원한다."""
    cut = len(message or "")
    for marker in ("\n\n[standing orders]", "\n\n[brief]"):
        i = (message or "").find(marker)
        if i != -1 and i < cut:
            cut = i
    return (message or "")[:cut]


def finish_issue_dispatches(c, issue_id):
    """Close active projections in the caller's issue transaction; no hook required.

    held(게이트 보류) 행도 종료 카드에서는 닫는다(2NHN) — ACTIVE_RUN_STATES는
    그대로(활성 정의 불변)이고 여기서만 확장한다."""
    states = (*config.ACTIVE_RUN_STATES, "held")
    ph = ",".join("?" * len(states))
    rows = c.execute(
        f"SELECT id FROM dispatches WHERE issue_id=? AND run_state IN ({ph}) "
        "AND EXISTS (SELECT 1 FROM issues i WHERE i.id=dispatches.issue_id "
        "AND (i.state IN ('done','cancelled') OR i.archived=1))",
        (issue_id, *states)).fetchall()
    ts = dbmod.now()
    for row in rows:
        c.execute("UPDATE dispatches SET run_state='finished', ended_at=? WHERE id=?",
                  (ts, row["id"]))
        log_event(c, "dispatch.updated", "dispatch", row["id"],
                  {"run_state": "finished", "via": "issue-terminal"})
    # 9PDD(M54X0X19-9PDD): 카드 종료 시 전달 상태 error/queued 행도 닫는다 —
    # 안 그러면 낡은 error 행이 재전달 대상으로 남아 종료 카드를 재청구시킨다(QN0H).
    # 주의: bump()가 활성 카드 PATCH마다 호출되므로 종료 상태 EXISTS 가드가 필수다
    # (a1 리뷰 R1) — 없으면 poll 에이전트의 queued 행이 픽업 전에 닫혀 영원히
    # 전달되지 않고, error 행도 재전달 대기 중 조기 종료된다.
    rows = c.execute(
        "SELECT id FROM dispatches WHERE issue_id=? AND status IN ('error','queued') "
        "AND EXISTS (SELECT 1 FROM issues i WHERE i.id=dispatches.issue_id "
        "AND (i.state IN ('done','cancelled') OR i.archived=1))",
        (issue_id,)).fetchall()
    for row in rows:
        c.execute("UPDATE dispatches SET status='superseded', ended_at=? WHERE id=?",
                  (ts, row["id"]))
        log_event(c, "dispatch.updated", "dispatch", row["id"],
                  {"status": "superseded", "via": "issue-terminal"})


def close_superseded_dispatches(c, issue_id):
    """Finish active dispatches from earlier rounds. No comment, receipt, or report_session.

    Round advance (claim, pull, resubmit, scope change) used to leave the previous
    round's queued/running/stalled row in place. A later kill/timeout was then
    rejected, and the probe treated that leftover as busy. Closing it here does
    not record completion evidence. NULL attempt rows are legacy and stay as they are.

    9PDD: 전달 상태 error/queued도 회차 기준으로 함께 닫는다 — run_state만 보면
    전달 실패 행이 다음 회차 재전달 대상으로 살아남는다.
    """
    ph = ",".join("?" * len(config.ACTIVE_RUN_STATES))
    rows = c.execute(
        f"SELECT d.id FROM dispatches d JOIN issues i ON i.id=d.issue_id "
        f"WHERE d.issue_id=? AND d.run_state IN ({ph}) "
        f"AND d.attempt IS NOT NULL AND d.attempt < i.execution_attempt",
        (issue_id, *config.ACTIVE_RUN_STATES)).fetchall()
    ts = dbmod.now()
    for row in rows:
        c.execute("UPDATE dispatches SET run_state='finished', ended_at=? WHERE id=?",
                  (ts, row["id"]))
        log_event(c, "dispatch.updated", "dispatch", row["id"],
                  {"run_state": "finished", "via": "stale-round"})
    rows = c.execute(
        "SELECT d.id FROM dispatches d JOIN issues i ON i.id=d.issue_id "
        "WHERE d.issue_id=? AND d.status IN ('error','queued') "
        "AND d.attempt IS NOT NULL AND d.attempt < i.execution_attempt",
        (issue_id,)).fetchall()
    for row in rows:
        c.execute("UPDATE dispatches SET status='superseded', ended_at=? WHERE id=?",
                  (ts, row["id"]))
        log_event(c, "dispatch.updated", "dispatch", row["id"],
                  {"status": "superseded", "via": "stale-round"})


def finish_submit_dispatches(c, issue_id, submitter):
    """90BH(M568PH4D-90BH) — 제출 시점에 제출자 소유 poll 행을 finished로.

    review-fix를 받은 poll 에이전트가 재제출해도 그 행이 running으로 남아
    probe의 같은 에이전트 재발령 가드(_occupies_card)에 걸린다. 제출(리뷰
    진입) 시 제출자(카드 assignee) 소유 poll 행을 닫는다 — 리뷰어 등 타
    에이전트 행은 건드리지 않는다."""
    if not submitter:
        return
    ph = ",".join("?" * len(config.ACTIVE_RUN_STATES))
    rows = c.execute(
        f"SELECT d.id FROM dispatches d JOIN agents a ON a.name=d.agent "
        f"WHERE d.issue_id=? AND d.agent=? AND a.receive_mode='poll' "
        f"AND d.run_state IN ({ph})",
        (issue_id, submitter, *config.ACTIVE_RUN_STATES)).fetchall()
    ts = dbmod.now()
    for r in rows:
        # 3D5T RC — 미픽업(status='queued') 행은 배달 큐에서도 빼야 한다: run_state만
        # finished로 두면 다음 폴링에 되살아난다(pending_inbox는 status='queued'를 배달).
        # 픽업된 행(status='ok')은 status를 유지하고 run_state만 종결한다.
        c.execute("UPDATE dispatches SET run_state='finished', ended_at=?, "
                  "status=CASE WHEN status='queued' THEN 'superseded' ELSE status END "
                  "WHERE id=?", (ts, r["id"]))
        log_event(c, "dispatch.updated", "dispatch", r["id"],
                  {"run_state": "finished", "via": "submit"})


def reset_evidence(c, issue_id):
    cursor = c.execute("SELECT COALESCE(MAX(id),0) FROM comments WHERE issue_id=?", (issue_id,)).fetchone()[0]
    return {"verified": 0, "verified_at": None, "verified_evidence": "",
            "verification_status": "unverified", "completion_report": "", "completed_at": None,
            "evidence_after_comment_id": cursor, "delivery_receipt": "",
            "approval_path": ""}


def log_event(c, kind: str, entity: str, entity_id, payload=None):
    """변경 이벤트 outbox 기록 (TT 개선#2) — 반드시 변경과 같은 트랜잭션 안에서
    호출한다. 소비자(보드·에이전트)는 GET /events?after_seq 또는 SSE로 수신.

    알림성 이벤트는 notified=0으로만 표기한다. 웹훅 전송은 여기서 하지 않는다
    (리뷰 R4a: 트랜잭션 안 동기 HTTP는 미커밋 원문 뒤에 쓰기 락을 잡아 무관한
    API까지 막는다) — 커밋 후 notify 워커가 락 밖에서 전달한다."""
    notify_worthy = kind in ("message.posted", "comment.added")
    c.execute("INSERT INTO events (kind, entity, entity_id, payload, ts, notified) "
              "VALUES (?,?,?,?,?,?)",
              (kind, entity, str(entity_id), json.dumps(payload or {}, ensure_ascii=False),
               dbmod.now(), 0 if notify_worthy else 1))


def notify_loop(db_path, entry, interval=0.3):
    """알림 전달 워커 (create_app 데몬 스레드). 수명은 entry와 공유(리뷰 R5 4차).

    entry: {"stop": Event, "refs": 살아있는 앱 수, "thread": Thread, "polls": 폴링 수}.
    종료 규약: stop은 "refs==0 확정"일 때만 set된다(마지막 앱 shutdown). 워커는
    stop+refs<=0을 원자적으로 확인하고 그때 자기 등록을 직접 지운다 — shutdown이
    등록을 선삭제하지 않으므로 '전송 중 종료 → 재생성'에서 새 워커가 같은 이벤트를
    다시 잡는 일이 없다. stop set + refs>0이면 인계로 보고 계속한다.

    전송 선점(리뷰 R5 4차): 행마다 notified 0→2 CAS로 소유권을 원자적으로 가져가
    전송 후 1로 마감한다 — 두 워커가 같은 이벤트를 전송하는 중복이 구조적으로 불가능.
    프로세스 크래시로 notified=2 잔행은 유실(프로세스 수명 best-effort — 이벤트 원장
    자체는 유실 0). 영구 연결 1개 폴링 — 틱마다 connect+migrate를 반복하면 스레드
    수만큼 스키마 컴파일이 쌓여 프로세스 전체가 질질 끈다(실측)."""
    con = None
    try:
        while True:
            with _notify_reg_lock:
                if entry["stop"].is_set():
                    if entry["refs"] <= 0:
                        if _notify_workers.get(db_path) is entry:
                            del _notify_workers[db_path]
                        break  # 종료 확정 — 등록은 워커 본인이 치웠다
                    entry["stop"].clear()  # 재생성 앱이 인계 — 계속
            try:
                if con is None:
                    if not os.path.exists(db_path):  # 테스트 tmp 정리 후 부활 방지
                        entry["stop"].wait(interval)
                        continue
                    con = dbmod.connect(db_path)
                con.row_factory = sqlite3.Row
                entry["polls"] = entry.get("polls", 0) + 1
                rows = con.execute("SELECT seq, kind, entity_id, payload FROM events "
                                   "WHERE notified=0 ORDER BY seq LIMIT ?", (200,)).fetchall()
                for r in rows:
                    # 선점 CAS — 같은 행을 두 워커가 잡지 못한다
                    cur = con.execute("UPDATE events SET notified=2 WHERE seq=? AND notified=0",
                                      (r["seq"],))
                    con.commit()
                    if cur.rowcount != 1:
                        continue  # 남이 이미 전송 중
                    try:
                        _notify_event(con, r["kind"], r["entity_id"], json.loads(r["payload"]))
                    except Exception as e:
                        print(f"[tt-server] notify_event 실패({r['kind']}): {str(e)[:120]}", flush=True)
                    con.execute("UPDATE events SET notified=1 WHERE seq=?", (r["seq"],))
                    con.commit()
            except Exception as e:
                print(f"[tt-server] notify 루프 오류: {str(e)[:120]}", flush=True)
                con = None  # 다음 틱에 재접속
            entry["stop"].wait(interval)
    finally:
        if con is not None:
            con.close()


def _notify_event(c, kind, entity_id, payload):
    """이벤트 → notify_hook 에이전트 웹훅 (리뷰 R4). 실행 권한은 claim/lease 그대로 —
    여기는 관찰 가능성 전달일 뿐이다. 대상·활성화 모두 기존 플래그가 결정한다."""
    if kind == "message.posted":
        targets = payload.get("mentions") or []
        if not targets:
            return
        rows = c.execute("SELECT * FROM agents WHERE enabled=1 AND notify_hook=1 AND "
                         "name IN (%s)" % ",".join("?" * len(targets)), targets).fetchall()
        for ag in rows:
            send_command(ag, "notify", payload.get("thread_id") or entity_id,
                         f"멘션 from {payload.get('author', '?')}",
                         text=(payload.get("body") or "")[:300])
    elif kind == "comment.added":
        body = payload.get("preview") or ""
        if "[release-ready]" not in body:
            return
        row = c.execute("SELECT a.* FROM issues i JOIN agents a ON a.name=i.assignee "
                        "WHERE i.id=? AND a.enabled=1 AND a.notify_hook=1",
                        (entity_id,)).fetchone()
        if row:
            send_command(row, "notify", entity_id,
                         "release-ready",
                         text=body[:300])


def record_comment(c, issue_id, author, body, notify_human=True):
    """코멘트 접수 코어 (리뷰 R9) — /comments 라우터와 통합 종료 접수(progress
    comment)가 공유한다. 버전·updated_at 갱신과 legacy blocked human 알림
    (waiting_for=human 마커 + blocked 카드)까지 동일 의미 보존."""
    c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
              (issue_id, author, body, dbmod.now()))
    c.execute("UPDATE issues SET updated_at=?, version=version+1 WHERE id=?",
              (dbmod.now(), issue_id))
    log_event(c, "comment.added", "issue", issue_id,
              {"author": author, "preview": (body or "")[:120]})
    if notify_human:
        row = c.execute("SELECT state FROM issues WHERE id=?", (issue_id,)).fetchone()
        if row and row["state"] == "blocked" and "waiting_for=human" in (body or ""):
            escalate_blocked_human(c, issue_id, source="comment")


def check_report(row, report):
    pinned = json.loads(row["work_contract"]) if row["work_contract"] else {}
    if report.attempt != row["execution_attempt"] or report.contract_version != pinned.get("version"):
        raise HTTPException(409, "stale completion report: refresh issue contract and execution_attempt")


def reported_fields(evidence):
    return {"verified": 1, "verified_at": dbmod.now(), "verified_evidence": evidence[:500],
            "verification_status": "reported"}


def requires_report(row):
    # Pre-upgrade work has no pinned policy. Enforce the new policy only
    # after claim/pull or a scope change establishes a contract.
    pinned = json.loads(row["work_contract"]) if row["work_contract"] else {}
    return pinned.get("report_required", False)


def standing_orders(c, issue):
    """DW0P(M5283NC3-DW0P) — 부모 epic 본문의 "## Standing Orders" 섹션 텍스트.

    claim 응답과 dispatch 메시지에 그대로 붙는다(번호 목록 원문). 부모 없음·
    섹션 없음·부모가 아카이브면 "" — 호출자는 빈 값을 그대로 둔다.
    """
    pid = issue.get("parent_id") if isinstance(issue, dict) else issue["parent_id"]
    if not pid:
        return ""
    row = c.execute("SELECT body, archived FROM issues WHERE id=?", (pid,)).fetchone()
    if not row or row["archived"] or not row["body"]:
        return ""
    m = re.search(r"^## +standing orders *\n(.*?)(?=^## |\Z)", row["body"],
                  re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def brief_block(issue):
    """Y5EG(M53PBECP-Y5EG) — 카드의 brief 4칸을 작업자 지시문용 섹션으로. 순수 함수.

    dict와 sqlite3.Row 모두 받는다. 비어 있으면 빈 문자열 — 섹션 자체를 붙이지 않는다."""
    def _v(row, key):
        try:
            return row[key] or ""
        except (KeyError, IndexError):
            return ""
    fields = (("SCOPE", "brief_scope"), ("VERIFY", "brief_verify"),
              ("FORBIDDEN", "brief_forbidden"), ("TIMEBOX", "brief_timebox"))
    lines = [f"- {label}: {_v(issue, key)}" for label, key in fields]
    lines = [ln for ln in lines if ln.rsplit(": ", 1)[1].strip()]
    if not lines:
        return ""
    return "[brief]\n" + "\n".join(lines)


def find_evidence(c, issue_id, row) -> str:
    # Failed structured submissions require a fresh report or explicit verify evidence.
    if row["completion_report"]:
        proof = json.loads(row["completion_report"])
        if proof["result"] != "passed":
            return ""
    for cm in reversed(dbmod.comments_of(c, issue_id)):
        if cm["id"] <= row["evidence_after_comment_id"] or cm["author"] == "tt-server":
            continue
        result = legacy_result(cm["body"])
        if result == "failed":
            return ""
        if result == "passed":
            return f"{cm['ts']} {cm['author']}: {cm['body']}"
    return ""


def level4_template(c, d: dict) -> dict:
    """§11 Level4 포맷: A/B 선택지 + Reasoner recommendation.
    recommendation은 기계 생성(human→B 승인, dependency→의존 확인) — 두뇌 계층은 범위 밖."""
    iid = d["id"]
    wf = d["waiting_for"] or "legacy"
    detail = (d["blocked_detail"] or "")[:200]
    if wf == "human":
        opts = [{"key": "A", "label": "중단 — 카드를 todo로 되돌려 담당 변경",
                 "command": f"tt edit {iid} --state todo"},
                {"key": "B", "label": "답변/결정 후 재개 — tt note로 결정 기록 후 todo",
                 "command": f'tt note {iid} "<결정>" && tt edit {iid} --state todo'}]
        rec = "B"
        rec_why = "waiting_for=human: 책임 액터의 결정이 필요 — 답변 기록 후 재개 권장"
    elif wf == "dependency":
        deps = dep_states(c, dep_ids(d))
        missing = [x["id"] for x in deps if x["state"] not in ("done", "cancelled")]
        ready = bool(deps) and not missing
        opts = [{"key": "A", "label": f"의존 {','.join(missing) or '-'} 진행 기다림",
                 "command": f"tt show {missing[0] if missing else iid}"},
                {"key": "B", "label": "의존 없이 재개 (카테고리 재조정)", "command": f"tt edit {iid} --state todo"}]
        rec = "A" if not ready else "B"
        rec_why = "release_ready=%s — 의존 상태에 따라 자동 권고" % ready
    else:
        opts = [{"key": "A", "label": "현재 blocked 유지", "command": "tt show " + iid},
                {"key": "B", "label": "강제로 재개", "command": f"tt edit {iid} --state todo"}]
        rec, rec_why = "A", f"waiting_for={wf}: 기본은 대기 유지"
    return {"level": 4, "issue_id": iid, "title": d["title"],
            "waiting_for": wf, "waiting_actor": d["waiting_actor"] or "미지정",
            "detail": detail, "options": opts,
            "recommendation": {"choice": rec, "reason": rec_why}}


def render_level4(t: dict) -> str:
    lines = [f"§11 Level4 — {t['title']} ({t['issue_id']})",
             f"대기: {t['waiting_for']} / 액터: {t['waiting_actor']}"]
    if t["detail"]:
        lines.append(f"사유: {t['detail']}")
    for o in t["options"]:
        lines.append(f"  [{o['key']}] {o['label']}  → `{o['command']}`")
    r = t["recommendation"]
    lines.append(f"권장: {r['choice']} — {r['reason']}")
    return "\n".join(lines)


def escalate_blocked_human(c, issue_id, source):
    """blocked(waiting_for=human) 1회 알림: notify_hook agent(기존 webhook 패턴,
    TT→Hermes 알림 주입)로 Level4 payload 발송 + 카드에 시스템 댓글. 자체 APNs 없음.
    best-effort: 발송 실패도 blocked 전이를 되돌리지 않는다. blocked_notified_at으로 dedup."""
    row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    d = dbmod.to_dict(row)
    if d.get("blocked_notified_at"):
        return
    t = level4_template(c, d)
    text = render_level4(t)
    agents = c.execute("SELECT * FROM agents WHERE enabled=1 AND notify_hook=1 ORDER BY name").fetchall()
    statuses = []
    for ag in agents:
        url = ag["base_url"]
        if config.notify_base() and url.startswith("/"):
            url = config.notify_base() + url
        status, detail = send_command(ag, "notify", issue_id, f"blocked(waiting_for=human) — {d['title']}",
                                      text=text)
        statuses.append(f"{ag['name']}:{status}")
        if status != "ok":
            print(f"[tt-server] notify → {ag['name']} 실패: {detail}", flush=True)
    c.execute("UPDATE issues SET blocked_notified_at=? WHERE id=?", (dbmod.now(), issue_id))
    body = (f"[level4-notify] human 대기 알림 발송({source}): "
            f"{', '.join(statuses) or 'notify_hook agent 없음'}\n{text}")
    c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
              (issue_id, "tt-server", body, dbmod.now()))
    log_event(c, "comment.added", "issue", issue_id,
              {"author": "tt-server", "preview": body[:120]})
    c.commit()


def dep_ids(d: dict) -> list:
    """의존 이슈 ID 목록: blocked_detail의 이슈-ID 토큰, 없으면 부모 카드. (④ 규약)"""
    ids = ISSUE_ID_RE.findall(d.get("blocked_detail") or "")
    if not ids and d.get("parent_id"):
        ids = [d["parent_id"]]
    out: list = []
    for i in ids:
        if i != d["id"] and i not in out:
            out.append(i)
    return out


def dep_states(c, ids) -> list:
    res = []
    for i in ids:
        r = c.execute("SELECT state FROM issues WHERE id=?", (i,)).fetchone()
        res.append({"id": i, "state": r["state"] if r else "missing"})
    return res


def enrich_blocked(c, d: dict) -> dict:
    """blocked+dependency 카드에 release_ready 표시 투영 (자동 재dispatch 없음 — ④)."""
    if d.get("state") == "blocked" and d.get("waiting_for") == "dependency":
        ds = dep_states(c, dep_ids(d))
        d["dependencies"] = ds
        d["release_ready"] = bool(ds) and all(x["state"] in ("done", "cancelled") for x in ds)
    return d


def send_command(ag, command, issue_id, reason, dispatch_id=None, text=None, url=None):
    """러너 제어/알림 명령을 hook 채널(base_url)로 발행. x-tt-command 헤더 → dispatch와 분리.
    url 지정 시 base_url 대신 그 주소로 발송(notify의 TT_NOTIFY_BASE 상대경로 해석용)."""
    payload = {"command": command, "issue_id": issue_id, "reason": reason, "ts": dbmod.now()}
    if dispatch_id is not None:
        payload["dispatch_id"] = dispatch_id
    if text is not None:
        payload["text"] = text
    req = urllib.request.Request(
        url or ag["base_url"], data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"content-type": "application/json", "x-tt-command": command},
        method="POST")
    if ag["secret"]:
        req.add_header("authorization", "Bearer " + ag["secret"])
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(4096)
            return "ok", f"HTTP {resp.status} {body.decode('utf-8', 'replace')[:120]}"
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"[:200]


def reconcile_release(c, issue_id, reason, by):
    """③ 카드 terminalize → 실행 중지 명령을 release_hook 수신 가능 agent에 발행.
    실패해도 전이 자체를 되돌리지 않는다(best-effort + 시스템 댓글로 관찰 가능하게)."""
    agents = c.execute(
        "SELECT DISTINCT a.* FROM dispatches d JOIN agents a ON a.name=d.agent "
        "WHERE d.issue_id=? AND d.status='ok' AND a.enabled=1 AND a.release_hook=1",
        (issue_id,)).fetchall()
    results = []
    for ag in agents:
        # 마지막 성공 dispatch = 러너 장부 key의 dispatch_id (있으면 그대로 전달)
        last = c.execute("SELECT id FROM dispatches WHERE issue_id=? AND agent=? AND status='ok' "
                         "ORDER BY id DESC LIMIT 1", (issue_id, ag["name"])).fetchone()
        did = last["id"] if last else None
        status, detail = send_command(ag, "release", issue_id, reason, did)
        results.append((ag["name"], status, detail))
        body = (f"reconcile release → {ag['name']}: {status} {detail} (by {by}, {reason})")
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (issue_id, "tt-server", body, dbmod.now()))
        log_event(c, "comment.added", "issue", issue_id,
                  {"author": "tt-server", "preview": body[:120]})
    return results


def notify_blocked_dependents(c, closed_id, end_state):
    """④ 의존 카드 종료 시 blocked 쪽에 '해제 가능' 댓글 1회 (자동 재dispatch 금지)."""
    marker = f"[release-ready] 의존 {closed_id}"
    for b in c.execute("SELECT * FROM issues WHERE state='blocked' AND waiting_for='dependency' "
                       "AND archived=0").fetchall():
        d = dbmod.to_dict(b)
        if closed_id not in dep_ids(d):
            continue
        dup = c.execute("SELECT 1 FROM comments WHERE issue_id=? AND author='tt-server' "
                        "AND body LIKE ? LIMIT 1", (b["id"], f"%{marker}%{end_state}%")).fetchone()
        if dup:
            continue
        open_deps = dep_states(c, dep_ids(d))
        still = [x["id"] for x in open_deps if x["state"] not in ("done", "cancelled")]
        head = "해제 가능" if not still else f"의존 {closed_id} {end_state} — 잔여 미완료 {','.join(still)}"
        body = (f"{marker} {end_state} — {head}. 재개: tt edit {b['id']} --state todo "
                f"(자동 재dispatch 없음 — 결정 후 수동 재개)")
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (b["id"], "tt-server", body, dbmod.now()))
        log_event(c, "comment.added", "issue", b["id"],
                  {"author": "tt-server", "preview": body[:120]})
    return


def valid_url(u):
    # "/hook" 같은 상대경로는 TT_NOTIFY_BASE 접두어로 해석되는 notify 전용 주소 (M3BZV172-9F0S B)
    return u.startswith("http://") or u.startswith("https://") or (config.notify_base() and u.startswith("/"))


def deliver(url, secret, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"content-type": "application/json",
                 "x-tt-dispatch": str(payload["dispatch_id"])},
        method="POST")
    if secret:
        req.add_header("authorization", "Bearer " + secret)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read(65536)
        code = resp.status
    ctx = ""
    try:
        ctx = str(json.loads(body or b"{}").get("context") or "")[:512]
    except Exception:
        pass
    return code, ctx


# ── dispatch 배달·투영 (AK87-4 — 라우터는 입출력만, 로직은 service로) ──

def insert_dispatch_row(c, issue_id, p, ag, agent_name, idem, attempt, inh_ctx):
    """신규 dispatch 행 생성 — 코멘트 기록 + run_state 투영(3A0B) + 이벤트.

    동시 중복 POST(유니크 idem_key)는 sqlite3.IntegrityError로 그대로 올린다 —
    경합 승자 판정과 재전달 선점은 호출자(라우터)의 단일 선점 경로(_claim_redelivery)가
    담당한다(5차 리뷰 R4)."""
    c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
              (issue_id, p.author, p.message, dbmod.now()))
    log_event(c, "comment.added", "issue", issue_id,
              {"author": p.author, "preview": p.message[:120]})
    # 3A0B(M54X0XDB-3A0B): poll 에이전트는 픽업 전까지 run_state=''라
    # /agents/active·probe busy·stall·지표가 전부 못 봤다 — 발송 시점부터
    # queued로 기록해 생명주기 추적을 시작한다.
    _poll = (ag["receive_mode"] or "").strip().lower() == "poll"
    did = c.execute("INSERT INTO dispatches (issue_id, agent, author, message, context, status, ts, model, idem_key, attempt, delivery_lease, run_state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (issue_id, agent_name, p.author, p.message, inh_ctx, "queued", dbmod.now(),
                     ag["model"] or "", idem, attempt,
                     dbmod.now(), "queued" if _poll else "")).lastrowid
    log_event(c, "dispatch.created", "dispatch", did,
              {"agent": agent_name, "issue_id": issue_id,
               "preview": p.message[:120]})
    return did


def dispatch_delivery_projection(ctx, ag, p, did, issue_id, payload, poll_mode, notify_fails):
    """배달(webhook/poll) + 행·에이전트 투영 + TPRQ 실패 추적.

    poll 에이전트는 훅 POST가 없다 — 큐에 남고 소속 러너의 pending 폴링이 꺼내 간다
    (단일 채널, 이중 전달 없음). error 시 context는 생성 시 상속값 보존(R5) —
    성공 시에만 응답 token으로 갱신. TPRQ: 같은 did의 재시도마다 댓글을 쌓지 않는다 —
    첫 실패만 타임라인에 남기고 이후는 행의 fail_count·detail로 추적한다."""
    status, detail, dctx = ("queued", "", "") if poll_mode else ("ok", "", "")
    if not poll_mode:
        try:
            code, dctx = deliver(ag["base_url"], ag["secret"], payload)
            if code >= 300:
                status, detail = "error", f"HTTP {code}"
        except Exception as e:
            status, detail = "error", f"{type(e).__name__}: {e}"[:300]
    with ctx.con() as c:
        if status == "ok":
            # TPRQ: 성공 시 실패 카운터 초기화 — 다음 실패가 다시 첫 실패로 기록된다
            c.execute("UPDATE dispatches SET status=?, detail=?, context=?, delivery_lease=NULL, "
                      "fail_count=0, last_fail_at=NULL WHERE id=?", (status, detail, dctx, did))
        elif status == "error":
            fail_n = c.execute("SELECT COALESCE(fail_count,0)+1 FROM dispatches WHERE id=?",
                               (did,)).fetchone()[0]
            c.execute("UPDATE dispatches SET status=?, detail=?, delivery_lease=NULL, "
                      "fail_count=?, last_fail_at=? WHERE id=?",
                      (status, detail, fail_n, dbmod.now(), did))
        else:
            fail_n = 0
        if not poll_mode:
            c.execute("UPDATE agents SET last_ok=?, last_err=? WHERE name=?",
                      (dbmod.now() if status == "ok" else ag["last_ok"],
                       "" if status == "ok" else detail, ag["name"]))
        if status == "error":
            if fail_n == 1:
                fail_body = f"⚠ hook dispatch #{did} → {p.agent} 실패: {detail}"
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", fail_body, dbmod.now()))
                log_event(c, "comment.added", "issue", issue_id,
                          {"author": "tt-server", "preview": fail_body[:120]})
            if fail_n == notify_fails:
                human_body = (f"⚠ hook dispatch #{did} → {p.agent} {fail_n}회 연속 실패 — "
                              f"사람 확인 필요: {detail}")
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", human_body, dbmod.now()))
                log_event(c, "comment.added", "issue", issue_id,
                          {"author": "tt-server", "preview": human_body[:120]})
        c.commit()


def project_progress_fields(c, row, p, ts, dispatch_id):
    """비종료 진행 투영 — run_state·liveness 시각·tail·started_at(§2.2).

    running/stalled: 진행 병기 허용 상태 — ts/tail 유무와 무관하게 liveness 시각
    갱신(동일 상태 재전송도 진행으로 봄 — 러너는 변경/주기 모두 허용).
    queued: run_state(+machine/session)만 — ts/tail 진행 미반영."""
    fields: dict = {"run_state": p.state}
    if p.state in ("running", "stalled"):
        fields["last_progress_at"] = ts
        if p.tail is not None:
            fields["last_tail"] = p.tail[:500]  # 서버 클램프 (422 아님)
        if not row["started_at"]:
            fields["started_at"] = ts
    else:
        if not row["started_at"]:
            fields["started_at"] = ts
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE dispatches SET {sets} WHERE id=?", (*fields.values(), dispatch_id))
    c.commit()


def project_terminal_report(c, issue_id, dispatch_id, p, row, current_attempt, accept_comment, ts):
    """종료 전환 최종 판정 — 서버 attempt CAS (TT 개선#1 요구 2, if-match).

    종료 상태(finished/failed)는 비교와 갱신을 단일 조건부 UPDATE로 원자화한다
    (R2) — dispatch가 생성된 회차(attempt 스냅샷)보다 이슈의 현재 execution_attempt가
    앞서면 투영은 닫되 코멘트·report_session·dispatch_reports는 남기지 않는다.
    현재 회차 보고가 CAS에 지면 409. 이미 done/cancelled/archived인 카드의 물리
    종료도 같은 예외이며 구회차 코멘트는 받지 않는다. attempt 스냅샷이 없는 구형
    행(NULL)은 기존 동작 유지.

    즉시 반환할 dict(row)이면 라우터가 그대로 반환, None이면 진행 투영 계속."""
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
            log_event(c, "dispatch.updated", "dispatch", dispatch_id,
                      {"run_state": p.state, "via": "stale-round"})
        return None
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
            return dict(c.execute("SELECT * FROM dispatches WHERE id=?",
                                  (dispatch_id,)).fetchone())
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
        log_event(c, "dispatch.updated", "dispatch", dispatch_id,
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
        record_comment(c, issue_id,
                       (p.author or "runner").strip() or "runner",
                       p.comment[:4000])
    return None
