"""공유 서비스 로직 — 라우터 간 공통 헬퍼 (라우팅 routers/와 분리).

관례: 함수는 DB 커넥션 `c`를 명시 인자로 받는다. 앱 인스턴스별 상태(DB 경로,
작업 계약 스냅샷)는 Ctx로 전달한다.
"""
import json
import urllib.request

from fastapi import HTTPException

import config
import db as dbmod
from verification import legacy_result

ISSUE_ID_RE = config.ISSUE_ID_RE

# 리뷰 계약 (M42KC1XR-2DD1 R2): 리뷰 dispatch·claim-review는 구현 계약 대신 이것을 전달 —
# 판정이 목적임을 명시해 에이전트가 구현을 시도하지 않게 한다.
REVIEW_CONTRACT = {
    "version": "review-v1",
    "role": "reviewer",
    "purpose": "구현이 아니라 판정이 목적 — PR diff를 검토해 승인 여부만 결정한다. 코드를 고치지 않는다.",
    "instructions": "리뷰 방식: 변경 파일 통독 + 변경 심볼 grep으로 호출자 확인(공용 모듈은 필수). "
                    "판정 기준: 계약 준수·시크릿 노출·테스트 적절성·놓친 엣지. "
                    "결과 게시: GitHub PR 코멘트와 TT 카드 코멘트 양쪽 — 첫 줄 'review: approve' 또는 "
                    "'review: request-changes', 둘째 줄 'PR#<n>@<sha8>'. TT 코멘트 author는 자기 에이전트명.",
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


def bump(c, issue_id, fields, expected_version=None):
    row = get_issue(c, issue_id)
    if expected_version is not None and row["version"] != expected_version:
        raise HTTPException(409, f"version conflict: have {row['version']}, expected {expected_version}")
    fields = {**fields, "version": row["version"] + 1, "updated_at": dbmod.now()}
    sets = ", ".join(f"{k}=?" for k in fields)
    res = c.execute(f"UPDATE issues SET {sets} WHERE id=? AND version=?", (*fields.values(), issue_id, row["version"]))
    if res.rowcount != 1:
        raise HTTPException(409, "concurrent update, retry")
    c.commit()


def reset_evidence(c, issue_id):
    cursor = c.execute("SELECT COALESCE(MAX(id),0) FROM comments WHERE issue_id=?", (issue_id,)).fetchone()[0]
    return {"verified": 0, "verified_at": None, "verified_evidence": "",
            "verification_status": "unverified", "completion_report": "", "completed_at": None,
            "evidence_after_comment_id": cursor}


def record_comment(c, issue_id, author, body, notify_human=True):
    """코멘트 접수 코어 (리뷰 R9) — /comments 라우터와 통합 종료 접수(progress
    comment)가 공유한다. 버전·updated_at 갱신과 legacy blocked human 알림
    (waiting_for=human 마커 + blocked 카드)까지 동일 의미 보존."""
    c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
              (issue_id, author, body, dbmod.now()))
    c.execute("UPDATE issues SET updated_at=?, version=version+1 WHERE id=?",
              (dbmod.now(), issue_id))
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
    c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
              (issue_id, "tt-server",
               f"[level4-notify] human 대기 알림 발송({source}): {', '.join(statuses) or 'notify_hook agent 없음'}\n{text}",
               dbmod.now()))
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
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (issue_id, "tt-server",
                   f"reconcile release → {ag['name']}: {status} {detail}"
                   f" (by {by}, {reason})", dbmod.now()))
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
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (b["id"], "tt-server",
                   f"{marker} {end_state} — {head}. 재개: tt edit {b['id']} --state todo "
                   f"(자동 재dispatch 없음 — 결정 후 수동 재개)", dbmod.now()))
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
