"""이슈 라우터 — 생성/조회/claim/pull/patch/verify/comments/why-blocked."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request

import config
import db as dbmod
import service
from models import ClaimIn, CommentIn, IssueCreate, IssuePatch, LeaseIn, ReviewClaimIn, VerifyIn
from service import Ctx
from verification import legacy_result

router = APIRouter()


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


@router.post("/issues", status_code=201)
def create_issue(p: IssueCreate, ctx: Ctx = Depends(get_ctx)):
    if p.state not in {"todo", "backlog"}:
        raise HTTPException(422, "new issues must start in todo or backlog")
    iid = dbmod.new_id()
    ts = dbmod.now()
    with ctx.con() as c:
        if p.parent_id:
            service.get_issue(c, p.parent_id)
        c.execute(
            "INSERT INTO issues (id,title,body,acceptance,state,priority,labels,assignee,parent_id,created_at,updated_at,todo_since) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, p.title, p.body, p.acceptance, p.state, p.priority, ",".join(p.labels), "", p.parent_id,
             ts, ts, ts if p.state == "todo" else None),
        )
        service.log_event(c, "issue.created", "issue", iid,
                          {"title": p.title[:80], "state": p.state, "parent": p.parent_id,
                           "has_acceptance": bool(p.acceptance.strip())})
        c.commit()
        row = service.get_issue(c, iid)
    return dbmod.to_dict(row)


@router.get("/issues")
def list_issues(state: str | None = None, parent: str | None = None, label: str | None = None,
                assignee: str | None = None, q: str | None = None, limit: int = 200,
                archived: str = "no",
                ctx: Ctx = Depends(get_ctx)):
    sql = "SELECT * FROM issues WHERE 1=1"
    args: list = []
    if archived == "no":
        sql += " AND archived=0"
    elif archived == "only":
        sql += " AND archived=1"
    if state:
        sql += " AND state=?"
        args.append(state)
    if parent == "none":
        sql += " AND parent_id IS NULL"
    elif parent:
        sql += " AND parent_id=?"
        args.append(parent)
    if label:
        sql += " AND (labels LIKE ? OR labels LIKE ? OR labels LIKE ? OR labels= ?)"
        args += [f"%,{label},%", f"{label},%", f"%,{label}", label]
    if assignee is not None:
        sql += " AND assignee=?"
        args.append(assignee)
    if q:
        sql += " AND (title LIKE ? OR body LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    with ctx.con() as c:
        rows = c.execute(sql, args).fetchall()
        return [service.enrich_blocked(c, dbmod.to_dict(r)) for r in rows]


@router.get("/issues/{issue_id}")
def get_issue_full(issue_id: str, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        row = service.get_issue(c, issue_id)
        children = c.execute("SELECT * FROM issues WHERE parent_id=? ORDER BY created_at", (issue_id,)).fetchall()
        out = service.enrich_blocked(c, dbmod.to_dict(row))
        out["children"] = [service.enrich_blocked(c, dbmod.to_dict(r)) for r in children]
        out["comments"] = dbmod.comments_of(c, issue_id)
    return out


@router.get("/issues/{issue_id}/tree")
def get_tree(issue_id: str, ctx: Ctx = Depends(get_ctx)):
    def build(iid, depth, c):
        row = service.get_issue(c, iid)
        node = dbmod.to_dict(row)
        node["depth"] = depth
        kids = c.execute("SELECT id FROM issues WHERE parent_id=? ORDER BY created_at", (iid,)).fetchall()
        node["tree"] = [build(k["id"], depth + 1, c) for k in kids]
        return node

    with ctx.con() as c:
        return build(issue_id, 0, c)


@router.post("/issues/{issue_id}/claim")
def claim(issue_id: str, p: ClaimIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        row = service.get_issue(c, issue_id)
        if row["state"] != "todo":
            raise HTTPException(409, f"cannot claim: state is {row['state']}")
        if row["assignee"] and row["assignee"] != p.agent:
            raise HTTPException(409, f"already claimed by {row['assignee']}")
        held = c.execute("SELECT COUNT(*) n FROM issues WHERE lease_by=? AND lease_expires>?",
                         (p.agent, dbmod.now())).fetchone()["n"]
        if held >= config.max_leases():
            raise HTTPException(409, f"lease limit: active leases={held} (max={config.max_leases()}) — heartbeat or done first")
        service.bump(c, issue_id, {**service.reset_evidence(c, issue_id), "state": "in_progress", "assignee": p.agent,
                                   "reviewer": None,  # R6: 재작업 진입 시 이전 리뷰어 표기 제거
                                   "started_at": dbmod.now(),
                                   "work_contract": json.dumps(ctx.contract, ensure_ascii=False),
                                   "execution_attempt": row["execution_attempt"] + 1,
                                   "lease_by": p.agent, "lease_expires": dbmod.future(p.safe_hours()),
                                   "heartbeat_at": dbmod.now(),
                                   "todo_since": None}, row["version"])
        row = service.get_issue(c, issue_id)
    return dbmod.to_dict(row)


@router.post("/issues/{issue_id}/claim-review")
def claim_review(issue_id: str, p: ReviewClaimIn, ctx: Ctx = Depends(get_ctx)):
    """리뷰어 claim — review 상태에서만. 상태·계약·attempt는 건드리지 않고 reviewer/lease만 기록."""
    with ctx.con() as c:
        # R4: 한도 검사와 기록을 같은 write 트랜잭션으로 — 동시 claim 이중 통과 방지
        c.execute("BEGIN IMMEDIATE")
        row = service.get_issue(c, issue_id)
        if row["state"] != "review":
            raise HTTPException(409, f"claim-review is for review-state cards only (state is {row['state']})")
        if row["reviewer"] and row["reviewer"] != p.agent:
            # R3: lease가 만료된 점유는 인계 가능 — 리뷰어 교체·장애 인계 차단 방지
            if (row["lease_expires"] or "") > dbmod.now():
                raise HTTPException(409, f"already claimed by {row['reviewer']}")
        # R4: 현재 카드의 기존 리뷰어 lease는 제외 — 같은 카드 재claim이 한도로 거부되지 않게
        held = c.execute("SELECT COUNT(*) n FROM issues WHERE lease_by=? AND lease_expires>? AND id<>?",
                         (p.agent, dbmod.now(), issue_id)).fetchone()["n"]
        if held >= config.max_leases():
            raise HTTPException(409, f"lease limit: active leases={held} (max={config.max_leases()})")
        service.bump(c, issue_id, {"reviewer": p.agent, "lease_by": p.agent,
                                   "lease_expires": dbmod.future(p.safe_hours()),
                                   "heartbeat_at": dbmod.now()}, row["version"])
        row = service.get_issue(c, issue_id)
    d = dbmod.to_dict(row)
    d["work_contract"] = service.REVIEW_CONTRACT  # 리뷰어는 리뷰 계약을 본다(R2)
    return d


@router.post("/issues/{issue_id}/lease")
def heartbeat(issue_id: str, p: LeaseIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        row = service.get_issue(c, issue_id)
        if row["lease_by"] != p.agent:
            raise HTTPException(409, f"lease held by {row['lease_by'] or 'nobody'}")
        c.execute("UPDATE issues SET lease_expires=?, heartbeat_at=?, updated_at=? WHERE id=?",
                  (dbmod.future(p.safe_hours()), dbmod.now(), dbmod.now(), issue_id))
        c.commit()
        row = service.get_issue(c, issue_id)
    return dbmod.to_dict(row)


@router.post("/issues/{issue_id}/ping", summary="Low-cost alive ping: refresh heartbeat_at only (no lease extension, no version bump). Agents: ping every ≤3min while working → green pulse on board.",
             include_in_schema=True)
def ping(issue_id: str, p: LeaseIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        row = service.get_issue(c, issue_id)
        if row["lease_by"] != p.agent:
            raise HTTPException(409, f"lease held by {row['lease_by'] or 'nobody'}")
        c.execute("UPDATE issues SET heartbeat_at=?, updated_at=? WHERE id=?",
                  (dbmod.now(), dbmod.now(), issue_id))
        c.commit()
        row = service.get_issue(c, issue_id)
    return dbmod.to_dict(row)


@router.post("/pull")
def pull(p: ClaimIn, ctx: Ctx = Depends(get_ctx)):
    ts = dbmod.now()
    with ctx.con() as c:
        held = c.execute("SELECT COUNT(*) n FROM issues WHERE lease_by=? AND lease_expires>?",
                         (p.agent, ts)).fetchone()["n"]
        if held >= config.max_leases():
            raise HTTPException(409, f"lease limit: active leases={held} (max={config.max_leases()}) — heartbeat or done first")
        sql = ("SELECT id, state FROM issues WHERE archived=0 AND ("
               "(state='todo' AND assignee='') OR "
               "(state='in_progress' AND lease_expires IS NOT NULL AND lease_expires<?))")
        args: list = [ts]
        if p.require_label:
            sql += " AND (labels LIKE ? OR labels LIKE ? OR labels LIKE ? OR labels= ?)"
            args += [f"%,{p.require_label},%", f"{p.require_label},%", f"%,{p.require_label}", p.require_label]
        sql += " ORDER BY priority IS NULL, priority, created_at LIMIT 10"
        for cand in c.execute(sql, args).fetchall():
            iid = cand["id"]
            if cand["state"] == "todo":
                res = c.execute(
                    "UPDATE issues SET state='in_progress', assignee=?, reviewer=NULL, started_at=?, lease_by=?, lease_expires=?, "
                    "heartbeat_at=?, updated_at=?, work_contract=?, execution_attempt=execution_attempt+1, "
                    "version=version+1 WHERE id=? AND state='todo' AND assignee=''",
                    (p.agent, ts, p.agent, dbmod.future(p.safe_hours()), ts, ts,
                     json.dumps(ctx.contract, ensure_ascii=False), iid),
                )
            else:
                res = c.execute(
                    "UPDATE issues SET assignee=?, lease_by=?, lease_expires=?, heartbeat_at=?, updated_at=?, "
                    "work_contract=?, execution_attempt=execution_attempt+1, version=version+1 "
                    "WHERE id=? AND state='in_progress' AND lease_expires<?",
                    (p.agent, p.agent, dbmod.future(p.safe_hours()), ts, ts,
                     json.dumps(ctx.contract, ensure_ascii=False), iid, ts),
                )
            if res.rowcount == 1:
                reset = service.reset_evidence(c, iid)
                c.execute("UPDATE issues SET " + ", ".join(f"{k}=?" for k in reset) + " WHERE id=?",
                          (*reset.values(), iid))
                service.log_event(c, "issue.updated", "issue", iid,
                                  {"state": "in_progress", "assignee": p.agent, "via": "pull"})
                c.commit()
                return dbmod.to_dict(service.get_issue(c, iid))
    return None


@router.patch("/issues/{issue_id}")
def patch_issue(issue_id: str, p: IssuePatch, ctx: Ctx = Depends(get_ctx)):
    fields: dict = {}
    gate_warn = False
    gate = config.done_gate()
    with ctx.con() as c:
        c.execute("BEGIN IMMEDIATE")
        row = service.get_issue(c, issue_id)
        if p.expected_version is not None and p.expected_version != row["version"]:
            raise HTTPException(409, "version conflict")
        scope_changed = any(value is not None and value != row[key]
                            for key, value in (("title", p.title), ("body", p.body)))
        restarting = p.state in {"todo", "backlog", "in_progress"} and p.state != row["state"]
        if scope_changed or restarting:
            fields.update(service.reset_evidence(c, issue_id))
            if scope_changed or p.state == "in_progress":
                fields.update(work_contract=json.dumps(ctx.contract, ensure_ascii=False),
                              execution_attempt=row["execution_attempt"] + 1)
        if scope_changed and row["state"] == "done" and p.state in {None, "done"}:
            fields["state"] = "review"
        effective = {**dict(row), **fields}
        if p.completion_report:
            if p.state != "done" or row["state"] not in {"in_progress", "review"}:
                raise HTTPException(422, "completion_report requires a transition to done")
            service.check_report(effective, p.completion_report)
        if p.state and p.state != row["state"]:
            if p.state not in dbmod.STATES:
                raise HTTPException(422, f"bad state {p.state}")
            if not dbmod.can_transition(row["state"], p.state):
                raise HTTPException(409, f"illegal transition {row['state']} -> {p.state}")
            fields["state"] = p.state
            fields["todo_since"] = dbmod.now() if p.state == "todo" else None
            if p.state == "in_progress" and not row["started_at"]:
                fields["started_at"] = dbmod.now()
            if p.state == "done":
                n_open = c.execute(
                    "SELECT COUNT(*) n FROM issues WHERE parent_id=? AND state NOT IN ('done','cancelled')",
                    (issue_id,)).fetchone()["n"]
                if n_open:
                    raise HTTPException(409, f"child 미완료 {n_open}건 — 자식을 done/cancelled로 먼저 종결하세요")
                labels_now = [s for s in (row["labels"] or "").split(",") if s]
                proof = p.completion_report
                ev = service.find_evidence(c, issue_id, effective) if not proof and not service.requires_report(effective) else ""
                fields.update(verified=0, verified_at=None, verified_evidence="",
                              verification_status="unverified", completed_at=None)
                if proof:
                    fields["completion_report"] = proof.model_dump_json()
                    if proof.result == "passed":
                        fields.update(service.reported_fields(proof.evidence))
                    # 제출의 종착지는 review (M3R7M0ZR-YF99): done은 probe 병합 확인(verify)
                    # 또는 사람 verify/force_done/close만 가능 — agent 직행 경로 없음.
                    fields["state"] = "review"
                elif "close" in labels_now or p.force_done:
                    fields.update({"verified": 1, "verified_at": dbmod.now(),
                                   "verification_status": "approved",
                                   "verified_evidence": "close 라벨/force_done 승인 경로"})
                elif ev:
                    fields.update(service.reported_fields(ev))
                elif gate == "gate" or service.requires_report(effective):
                    fields["state"] = "review"
                else:
                    gate_warn = gate == "warn"
                if fields["state"] == "done":
                    fields["completed_at"] = dbmod.now()
            if p.state in ("todo", "backlog", "done", "cancelled", "blocked"):
                fields["assignee"] = "" if p.state in ("todo", "backlog") else row["assignee"]
                fields.update({"lease_by": "", "lease_expires": None})
                # blocked 사족은 blocked 이탈 시 자동 소거 (재-blocked 시 재입력)
                fields.update({"waiting_for": "", "waiting_actor": "", "blocked_detail": "",
                               "blocked_notified_at": None})
            if fields.get("state") == "review":
                # review = done 강등 대기: worker lease 반납 + blocked 사족 리셋(재진입 시 재알림)
                fields.update({"lease_by": "", "lease_expires": None,
                               "waiting_for": "", "waiting_actor": "", "blocked_detail": "",
                               "blocked_notified_at": None})
        if p.acceptance is not None:
            # 기존 카드 점진 보완 경로 — 생성 게이트와 달리 PATCH는 강제하지 않는다
            if not p.acceptance.strip():
                raise HTTPException(422, "acceptance는 빈 값으로 되돌릴 수 없다")
            fields["acceptance"] = p.acceptance.strip()
        if p.title is not None:
            fields["title"] = p.title
        if p.body is not None:
            fields["body"] = p.body
        if p.priority is not None:
            fields["priority"] = p.priority
        if p.clear_priority:
            fields["priority"] = None
        if p.labels is not None:
            fields["labels"] = ",".join(p.labels)
        if p.assignee is not None:
            fields["assignee"] = p.assignee
            if p.assignee == "":
                fields.update({"lease_by": "", "lease_expires": None})
        if p.reviewer is not None:
            # lease는 probe가 조건부로 명시(R5) — reviewer 반납이 재작업자 lease를 덮지 않게 분리
            fields["reviewer"] = p.reviewer or None
            if p.reviewer == "" and row["lease_by"] == (row["reviewer"] or ""):
                fields.update({"lease_by": "", "lease_expires": None})
        if p.archived is not None:
            fields["archived"] = 1 if p.archived else 0
        # blocked 사족 (①): waiting_for는 4종 enum만, 상태가 blocked일 때만 유효.
        # 미입력(미지정)이면 기존 동작 그대로 — 하위 호환.
        if p.waiting_for is not None:
            wf = p.waiting_for
            if wf not in dbmod.WAITING_FOR:
                raise HTTPException(422, f"waiting_for는 {sorted(dbmod.WAITING_FOR)} 중 하나")
            if (fields.get("state") or row["state"]) != "blocked":
                raise HTTPException(409, "waiting_for는 blocked 전이/blocked 상태에서만 설정 가능")
            fields["waiting_for"] = wf
            if p.waiting_actor is not None:
                fields["waiting_actor"] = p.waiting_actor
            if p.blocked_detail is not None:
                fields["blocked_detail"] = p.blocked_detail
        else:
            if p.waiting_actor is not None:
                fields["waiting_actor"] = p.waiting_actor
            if p.blocked_detail is not None:
                fields["blocked_detail"] = p.blocked_detail
        if p.clear_parent:
            fields["parent_id"] = None
        elif p.parent_id is not None:
            if p.parent_id == issue_id:
                raise HTTPException(422, "parent cannot be self")
            service.get_issue(c, p.parent_id)
            node = p.parent_id
            while node:
                if node == issue_id:
                    raise HTTPException(422, "cycle detected")
                node = c.execute("SELECT parent_id FROM issues WHERE id=?", (node,)).fetchone()["parent_id"]
            fields["parent_id"] = p.parent_id
        if not fields:
            return service.enrich_blocked(c, dbmod.to_dict(row))
        service.bump(c, issue_id, fields, p.expected_version)
        new_state = fields.get("state")
        if p.state == "done" and new_state == "review":
            accepted = (fields.get("completion_report")
                        and fields.get("verification_status") == "reported")
            if accepted:
                # 유효보고 접수 = 정상 정지. 병합 확인(probe) 또는 사람 verify가 done 확정.
                body = ("보고 접수 — review에 정지(병합 대기). PR 병합 확인 후 probe verify 또는 "
                        f"tt verify {issue_id} --report/증거로 done 확정. 재작업은 review → todo → claim.")
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", body, dbmod.now()))
                service.log_event(c, "comment.added", "issue", issue_id,
                                  {"author": "tt-server", "preview": body[:120]})
            else:
                # done 강등 사유를 관찰 가능하게: 무엇이 증거로 인정되는지 안내
                body = config.DONE_GATE_MSG.format(iid=issue_id)
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", body, dbmod.now()))
                service.log_event(c, "comment.added", "issue", issue_id,
                                  {"author": "tt-server", "preview": body[:120]})
            c.commit()
        elif p.state == "done" and new_state == "done" and gate_warn:
            body = (f"⚠ done 됐지만 완료증거 코멘트가 없음 (TT_DONE_GATE=warn — M3BZV172-9F0S). "
                    f"나중에라도: tt verify {issue_id} <증거>")
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, "tt-server", body, dbmod.now()))
            service.log_event(c, "comment.added", "issue", issue_id,
                              {"author": "tt-server", "preview": body[:120]})
            c.commit()
        if new_state in ("done", "cancelled"):
            # ③ reconcile: terminalize된 카드의 실행 중인 agent에게 release 명령 (best-effort)
            service.reconcile_release(c, issue_id, f"카드 {new_state} 전이", "board")
            # ④ 이 카드를 의존하던 blocked 카드들에 '해제 가능' 알림 (자동 재dispatch 없음)
            service.notify_blocked_dependents(c, issue_id, new_state)
            c.commit()
        if new_state == "blocked" and fields.get("waiting_for") == "human":
            # blocked(waiting_for=human) → Level4 알림 주입 (M3BZV172-9F0S B)
            service.escalate_blocked_human(c, issue_id, source="field")
        elif fields.get("waiting_for") == "human" and row["state"] == "blocked":
            # 이미 blocked인 카드에 사족 보강으로 human이 된 경우에도 1회 (dedup은 필드)
            service.escalate_blocked_human(c, issue_id, source="field")
        row = service.get_issue(c, issue_id)
        return service.enrich_blocked(c, dbmod.to_dict(row))


@router.post("/issues/{issue_id}/verify")
def verify_issue(issue_id: str, p: VerifyIn, ctx: Ctx = Depends(get_ctx)):
    """Accept a completion report; this does not attest external execution."""
    with ctx.con() as c:
        c.execute("BEGIN IMMEDIATE")
        row = service.get_issue(c, issue_id)
        if p.human and row["state"] not in ("in_progress", "review"):
            raise HTTPException(409, f"state is {row['state']} — human verify는 in_progress/review에서만")
        if not p.human and row["state"] != "review":
            raise HTTPException(409, f"state is {row['state']} — review만 verify 가능")
        n_open = c.execute(
            "SELECT COUNT(*) n FROM issues WHERE parent_id=? AND state NOT IN ('done','cancelled')",
            (issue_id,)).fetchone()["n"]
        if n_open:
            raise HTTPException(409, f"child 미완료 {n_open}건 — 자식을 done/cancelled로 먼저 종결하세요")
        if p.human:
            # 약한 합의(THNJ): 사람 자기선언 완료 — 토큰·보고 불요, 한 줄 노트 필수.
            # in_progress/review 모두 허용(사람은 에이전트 제출 전에도 즉시 승인 가능).
            # approved = 사람 승인(close/force_done과 동일 등급), agent verify(reported)와 구분.
            ev = p.evidence.strip()
            if not ev:
                raise HTTPException(422, "human verify requires a one-line attestation note")
            if p.expected_version is not None and p.expected_version != row["version"]:
                raise HTTPException(409, "version conflict")
            verify_body = (f"verify → done (사람 승인 — 자기선언). 노트: {ev[:300]} "
                           f"| 작업: @{row['assignee'] or '-'} 승인: {p.verifier}")
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, p.verifier, verify_body, dbmod.now()))
            service.log_event(c, "comment.added", "issue", issue_id,
                              {"author": p.verifier, "preview": verify_body[:120]})
            c.execute("UPDATE issues SET verified=1, verified_at=?, verified_evidence=?, "
                      "verification_status='approved', state='done', completed_at=?, version=version+1 "
                      "WHERE id=? AND version=?",
                      (dbmod.now(), f"사람 승인: {ev[:300]}", dbmod.now(), issue_id, row["version"]))
            # 리뷰 R2 2차: 사람 승인 분기도 상태 전이 이벤트 기록 — done 소비자가 놓치지 않게
            service.log_event(c, "issue.updated", "issue", issue_id,
                              {"state": "done", "via": "human-verify", "verifier": p.verifier})
            row2 = service.get_issue(c, issue_id)
            return dbmod.to_dict(row2)
        proof = p.completion_report
        if service.requires_report(row) and not proof:
            raise HTTPException(422, "completion_report required by this attempt's work contract")
        if proof:
            service.check_report(row, proof)
            if proof.result != "passed":
                raise HTTPException(422, "completion report must pass before verify")
            ev = proof.evidence
        elif p.evidence.strip():
            ev = p.evidence.strip()
            if legacy_result(ev) != "passed":
                raise HTTPException(422, "evidence must contain a successful result; prefer completion_report")
        else:
            ev = service.find_evidence(c, issue_id, row)
            if not ev:
                raise HTTPException(422, "successful current-attempt evidence or completion_report required")
        if p.expected_version is not None and p.expected_version != row["version"]:
            raise HTTPException(409, "version conflict")
        verify_body = (f"verify → done (reported). evidence: {ev[:300]}"
                       + ("" if (proof is None or isinstance(proof.verification.evidence, str))
                          else f" [구조화 증거 {len(proof.verification.evidence)}블록]"))
        c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                  (issue_id, p.verifier, verify_body, dbmod.now()))
        service.log_event(c, "comment.added", "issue", issue_id,
                          {"author": p.verifier, "preview": verify_body[:120]})
        fields = {**service.reported_fields(ev), "state": "done", "completed_at": dbmod.now(),
                  "completion_report": proof.model_dump_json() if proof else "",
                  "lease_by": "", "lease_expires": None, "waiting_for": "", "waiting_actor": "",
                  "blocked_detail": "", "blocked_notified_at": None}
        service.bump(c, issue_id, fields, row["version"])
        service.reconcile_release(c, issue_id, "verify로 done 확정", p.verifier)
        service.notify_blocked_dependents(c, issue_id, "done")
        c.commit()
        row = service.get_issue(c, issue_id)
    return service.enrich_blocked(c, dbmod.to_dict(row))


@router.post("/issues/{issue_id}/comments", status_code=201)
def add_comment(issue_id: str, p: CommentIn, ctx: Ctx = Depends(get_ctx)):
    with ctx.con() as c:
        service.get_issue(c, issue_id)
        # 접수 코어는 통합 종료 접수(progress comment)와 공유 (R9)
        service.record_comment(c, issue_id, p.author, p.body)
        c.commit()
    return {"ok": True}


@router.get("/issues/{issue_id}/why-blocked")
def why_blocked(issue_id: str, ctx: Ctx = Depends(get_ctx)):
    """blocked 사유의 기계 판독 투영: 게이트·기준·누락 증거·권장 명령. (②)
    자동 생성이 아니라 waiting_for/blocked_detail/코멘트 기반 투영 — 필드 없으면 legacy 코멘트."""
    with ctx.con() as c:
        row = service.get_issue(c, issue_id)
        if row["state"] != "blocked":
            raise HTTPException(409, f"state is {row['state']} — blocked가 아님")
        d = dbmod.to_dict(row)
        comments = dbmod.comments_of(c, issue_id)
        wf = d["waiting_for"]
        wf_source = "field"
        if not wf:  # legacy: runner BLOCKED/waiting_for= 코멘트에서 추정
            for cm in reversed(comments):
                m = re.search(r"waiting_for=(dependency|human|gate|external)", cm["body"])
                if m:
                    wf, wf_source = m.group(1), "comment"
                    break
        deps = service.dep_states(c, service.dep_ids(d)) if (wf == "dependency") else []
        missing = [x["id"] for x in deps if x["state"] not in ("done", "cancelled")]
        evidence = ""
        for cm in reversed(comments):
            if cm["author"] == "tt-server":  # 시스템 통보는 사유 근거에서 제외
                continue
            if any(k in cm["body"] for k in ("BLOCKED", "보류", "거부", "waiting_for")):
                evidence = f"{cm['ts']} {cm['author']}: {cm['body'][:300]}"
                break
        last_disp = c.execute("SELECT id FROM dispatches WHERE issue_id=? ORDER BY id DESC LIMIT 1",
                              (issue_id,)).fetchone()
        if wf == "human":
            crit = config.WAIT_CRITERIA[wf] + (f" (액터: {d['waiting_actor'] or '미지정'})")
            cmds = [f'tt note {issue_id} "<결정/답변>"', f"tt edit {issue_id} --state todo"]
        elif wf == "gate":
            crit = config.WAIT_CRITERIA[wf]
            cmds = [f'tt dispatch {issue_id} -A <agent> "승인"', f"tt edit {issue_id} --state todo"]
        elif wf == "dependency":
            crit = config.WAIT_CRITERIA[wf]
            cmds = [f"tt show {i}" for i in missing] + \
                   ([f"tt edit {issue_id} --state todo"] if not missing else [])
        elif wf == "external":
            crit = config.WAIT_CRITERIA[wf]
            cmds = ["blocked_detail의 외부 조건 충족 확인", f"tt edit {issue_id} --state todo"]
        else:
            crit = "waiting_for 미입력(legacy) — 마지막 사유 코멘트 참고; tt block으로 사족 입력 권장"
            cmds = [f"tt edit {issue_id} --state todo"]
        return {
            "issue_id": issue_id, "gate": {"kind": wf or "legacy", "issue": issue_id,
                                           "dispatch": last_disp["id"] if last_disp else None},
            "waiting_for": wf, "waiting_for_source": wf_source,
            "waiting_actor": d["waiting_actor"], "blocked_detail": d["blocked_detail"],
            "criteria": crit, "dependencies": deps, "missing": missing,
            "release_ready": bool(deps) and not missing,
            "evidence": evidence, "next_commands": cmds,
        }
