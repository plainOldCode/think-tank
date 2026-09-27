import json
import os
import pathlib
import re
import urllib.request
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db as dbmod
from work_contract import current_contract
from verification import CompletionReport, legacy_result

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent / "cli" / "tt"

STATE_ENUM = sorted(dbmod.STATES)

# done≠verified 게이트 모드 (M3BZV172-9F0S A): gate(기본, 증거 없으면 review 강등) |
# warn(경고 댓글만, done 허용) | off. TT_NOTIFY_BASE: notify agent base_url 접두어 —
# 상대경로(/hook)를 실 Telegram 알림 주입 경로(Hermes 어댑터)로 해석할 때 쓴다.
DONE_GATE = os.environ.get("TT_DONE_GATE", "gate").lower()
NOTIFY_BASE = os.environ.get("TT_NOTIFY_BASE", "").rstrip("/")

DONE_GATE_MSG = ("done 증거 없음 또는 실패/미확정 보고 — 현재 회차의 성공 결과가 필요합니다. "
                 "tt done {iid} --report report.json 또는 tt verify {iid} --report report.json. "
                 "재작업: tt edit {iid} --state todo 후 claim. force_done/close는 승인 예외입니다.")

ISSUE_ID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{8}-[0-9A-HJKMNP-TV-Z]{4}\b")

# blocked 해제 기준 문구 (M3BZS1FS-5722 ② 왜-blocked 투영용; 자동 생성 아님)
WAIT_CRITERIA = {
    "human": "책임 액터(waiting_actor)의 결정·코멘트가 있어야 재개 가능",
    "gate": "'승인'/'approve' dispatch(파괴적 패턴 게이트 해제)가 있어야 재개 가능",
    "dependency": "blocked_detail에 명시한 의존 이슈가 모두 done/cancelled이어야 재개 가능",
    "external": "blocked_detail에 명시한 외부 조건이 충족되어야 재개 가능",
}


class IssueCreate(BaseModel):
    title: str
    body: str = ""
    parent_id: str | None = None
    priority: int | None = None
    labels: list[str] = []
    state: str = "todo"


class ClaimIn(BaseModel):
    agent: str
    require_label: str | None = None
    hours: int = 1

    def safe_hours(self):
        return max(1, min(6, self.hours))


class LeaseIn(BaseModel):
    agent: str
    hours: int = 1

    def safe_hours(self):
        return max(1, min(6, self.hours))


class IssuePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    state: str | None = None
    parent_id: str | None = None
    clear_parent: bool = False
    priority: int | None = None
    clear_priority: bool = False
    labels: list[str] | None = None
    assignee: str | None = None
    expected_version: int | None = None
    archived: bool | None = None
    # blocked 사족 (M3BZS1FS-5722 ①): 미입력 시 기존 동작 유지
    waiting_for: str | None = None
    waiting_actor: str | None = None
    blocked_detail: str | None = None
    # 완료 보고 게이트: 성공 결과 없으면 review. force_done/close는 명시적 승인 예외.
    force_done: bool = False
    completion_report: CompletionReport | None = None


class VerifyIn(BaseModel):
    verifier: str
    evidence: str = ""
    completion_report: CompletionReport | None = None
    expected_version: int | None = None


class CommentIn(BaseModel):
    author: str
    body: str


class AgentIn(BaseModel):
    name: str
    base_url: str
    secret: str = ""
    enabled: bool = True
    release_hook: bool = False  # reconcile release 명령 수신 능력 (M3BZS1FS-5722 ③)
    notify_hook: bool = False  # blocked(human) Level4 알림 수신 능력 (M3BZV172-9F0S B)


class AgentPatch(BaseModel):
    base_url: str | None = None
    secret: str | None = None
    enabled: bool | None = None
    release_hook: bool | None = None
    notify_hook: bool | None = None


class DispatchIn(BaseModel):
    agent: str
    message: str
    author: str = "board"


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="think-tank")
    app.state.db_path = db_path
    contract = current_contract()

    def con():
        return dbmod.connect(app.state.db_path)

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

    @app.get("/health")
    def health():
        return {"status": "ok", "name": "think-tank"}

    @app.get("/work-contract")
    def work_contract():
        return contract

    @app.get("/install.sh", include_in_schema=False)
    def install_script(request: Request):
        base = str(request.base_url).rstrip("/")
        src = CLI_PATH.read_text().replace("${TT_URL:-http://127.0.0.1:7800}", "${TT_URL:-" + base + "}")
        script = (
            "#!/bin/sh\n# think-tank tt CLI installer (served by the tt server itself)\n"
            "mkdir -p \"$HOME/.local/bin\"\n"
            "cat > \"$HOME/.local/bin/tt\" <<'TT_EOF'\n" + src + "\nTT_EOF\n"
            "chmod +x \"$HOME/.local/bin/tt\"\n"
            "echo \"installed: ~/.local/bin/tt (default TT_URL=" + base + "; env TT_URL overrides)\"\n"
            "case \":$PATH:\" in *\":$HOME/.local/bin:\"*) ;; *) echo 'add to PATH: export PATH=\"$HOME/.local/bin:$PATH\"' ;; esac\n"
        )
        return Response(script, media_type="text/x-shellscript")

    @app.post("/issues", status_code=201)
    def create_issue(p: IssueCreate):
        if p.state not in {"todo", "backlog"}:
            raise HTTPException(422, "new issues must start in todo or backlog")
        iid = dbmod.new_id()
        ts = dbmod.now()
        with con() as c:
            if p.parent_id:
                get_issue(c, p.parent_id)
            c.execute(
                "INSERT INTO issues (id,title,body,state,priority,labels,assignee,parent_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iid, p.title, p.body, p.state, p.priority, ",".join(p.labels), "", p.parent_id, ts, ts),
            )
            c.commit()
            row = get_issue(c, iid)
        return dbmod.to_dict(row)

    @app.get("/issues")
    def list_issues(state: str | None = None, parent: str | None = None, label: str | None = None,
                    assignee: str | None = None, q: str | None = None, limit: int = 200,
                    archived: str = "no"):
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
        with con() as c:
            rows = c.execute(sql, args).fetchall()
            return [enrich_blocked(c, dbmod.to_dict(r)) for r in rows]

    @app.get("/issues/{issue_id}")
    def get_issue_full(issue_id: str):
        with con() as c:
            row = get_issue(c, issue_id)
            children = c.execute("SELECT * FROM issues WHERE parent_id=? ORDER BY created_at", (issue_id,)).fetchall()
            out = enrich_blocked(c, dbmod.to_dict(row))
            out["children"] = [enrich_blocked(c, dbmod.to_dict(r)) for r in children]
            out["comments"] = dbmod.comments_of(c, issue_id)
        return out

    @app.get("/issues/{issue_id}/tree")
    def get_tree(issue_id: str):
        def build(iid, depth):
            row = get_issue(c, iid)
            node = dbmod.to_dict(row)
            node["depth"] = depth
            kids = c.execute("SELECT id FROM issues WHERE parent_id=? ORDER BY created_at", (iid,)).fetchall()
            node["tree"] = [build(k["id"], depth + 1) for k in kids]
            return node

        with con() as c:
            return build(issue_id, 0)

    @app.post("/issues/{issue_id}/claim")
    def claim(issue_id: str, p: ClaimIn):
        with con() as c:
            row = get_issue(c, issue_id)
            if row["state"] != "todo":
                raise HTTPException(409, f"cannot claim: state is {row['state']}")
            if row["assignee"] and row["assignee"] != p.agent:
                raise HTTPException(409, f"already claimed by {row['assignee']}")
            held = c.execute("SELECT COUNT(*) n FROM issues WHERE lease_by=? AND lease_expires>?",
                             (p.agent, dbmod.now())).fetchone()["n"]
            if held >= 2:
                raise HTTPException(409, "lease limit: active leases=2 — heartbeat or done first")
            bump(c, issue_id, {**reset_evidence(c, issue_id), "state": "in_progress", "assignee": p.agent, "started_at": dbmod.now(),
                               "work_contract": json.dumps(contract, ensure_ascii=False),
                               "execution_attempt": row["execution_attempt"] + 1,
                               "lease_by": p.agent, "lease_expires": dbmod.future(p.safe_hours()), "heartbeat_at": dbmod.now()}, row["version"])
            row = get_issue(c, issue_id)
        return dbmod.to_dict(row)

    @app.post("/issues/{issue_id}/lease")
    def heartbeat(issue_id: str, p: LeaseIn):
        with con() as c:
            row = get_issue(c, issue_id)
            if row["lease_by"] != p.agent:
                raise HTTPException(409, f"lease held by {row['lease_by'] or 'nobody'}")
            c.execute("UPDATE issues SET lease_expires=?, heartbeat_at=?, updated_at=? WHERE id=?",
                      (dbmod.future(p.safe_hours()), dbmod.now(), dbmod.now(), issue_id))
            c.commit()
            row = get_issue(c, issue_id)
        return dbmod.to_dict(row)

    @app.post("/pull")
    def pull(p: ClaimIn):
        ts = dbmod.now()
        with con() as c:
            held = c.execute("SELECT COUNT(*) n FROM issues WHERE lease_by=? AND lease_expires>?",
                             (p.agent, ts)).fetchone()["n"]
            if held >= 2:
                raise HTTPException(409, "lease limit: active leases=2 — heartbeat or done first")
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
                        "UPDATE issues SET state='in_progress', assignee=?, started_at=?, lease_by=?, lease_expires=?, "
                        "heartbeat_at=?, updated_at=?, work_contract=?, execution_attempt=execution_attempt+1, "
                        "version=version+1 WHERE id=? AND state='todo' AND assignee=''",
                        (p.agent, ts, p.agent, dbmod.future(p.safe_hours()), ts, ts, json.dumps(contract, ensure_ascii=False), iid),
                    )
                else:
                    res = c.execute(
                        "UPDATE issues SET assignee=?, lease_by=?, lease_expires=?, heartbeat_at=?, updated_at=?, "
                        "work_contract=?, execution_attempt=execution_attempt+1, version=version+1 "
                        "WHERE id=? AND state='in_progress' AND lease_expires<?",
                        (p.agent, p.agent, dbmod.future(p.safe_hours()), ts, ts, json.dumps(contract, ensure_ascii=False), iid, ts),
                    )
                if res.rowcount == 1:
                    reset = reset_evidence(c, iid)
                    c.execute("UPDATE issues SET " + ", ".join(f"{k}=?" for k in reset) + " WHERE id=?",
                              (*reset.values(), iid))
                    c.commit()
                    return dbmod.to_dict(get_issue(c, iid))
        return None

    @app.patch("/issues/{issue_id}")
    def patch_issue(issue_id: str, p: IssuePatch):
        fields: dict = {}
        gate_warn = False
        with con() as c:
            c.execute("BEGIN IMMEDIATE")
            row = get_issue(c, issue_id)
            if p.expected_version is not None and p.expected_version != row["version"]:
                raise HTTPException(409, "version conflict")
            scope_changed = any(value is not None and value != row[key]
                                for key, value in (("title", p.title), ("body", p.body)))
            restarting = p.state in {"todo", "backlog", "in_progress"} and p.state != row["state"]
            if scope_changed or restarting:
                fields.update(reset_evidence(c, issue_id))
                if scope_changed or p.state == "in_progress":
                    fields.update(work_contract=json.dumps(contract, ensure_ascii=False),
                                  execution_attempt=row["execution_attempt"] + 1)
            if scope_changed and row["state"] == "done" and p.state in {None, "done"}:
                fields["state"] = "review"
            effective = {**dict(row), **fields}
            if p.completion_report:
                if p.state != "done" or row["state"] not in {"in_progress", "review"}:
                    raise HTTPException(422, "completion_report requires a transition to done")
                check_report(effective, p.completion_report)
            if p.state and p.state != row["state"]:
                if p.state not in dbmod.STATES:
                    raise HTTPException(422, f"bad state {p.state}")
                if not dbmod.can_transition(row["state"], p.state):
                    raise HTTPException(409, f"illegal transition {row['state']} -> {p.state}")
                fields["state"] = p.state
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
                    ev = find_evidence(c, issue_id, effective) if not proof and not requires_report(effective) else ""
                    fields.update(verified=0, verified_at=None, verified_evidence="",
                                  verification_status="unverified", completed_at=None)
                    if proof:
                        fields["completion_report"] = proof.model_dump_json()
                        if proof.result == "passed":
                            fields.update(reported_fields(proof.evidence))
                        else:
                            fields["state"] = "review"
                    elif "close" in labels_now or p.force_done:
                        fields.update({"verified": 1, "verified_at": dbmod.now(),
                                       "verification_status": "approved",
                                       "verified_evidence": "close 라벨/force_done 승인 경로"})
                    elif ev:
                        fields.update(reported_fields(ev))
                    elif DONE_GATE == "gate" or requires_report(effective):
                        fields["state"] = "review"
                    else:
                        gate_warn = DONE_GATE == "warn"
                    if fields["state"] == "done":
                        fields["completed_at"] = dbmod.now()
                if p.state in ("todo", "backlog", "done", "cancelled"):
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
                get_issue(c, p.parent_id)
                node = p.parent_id
                while node:
                    if node == issue_id:
                        raise HTTPException(422, "cycle detected")
                    node = c.execute("SELECT parent_id FROM issues WHERE id=?", (node,)).fetchone()["parent_id"]
                fields["parent_id"] = p.parent_id
            if not fields:
                return enrich_blocked(c, dbmod.to_dict(row))
            bump(c, issue_id, fields, p.expected_version)
            new_state = fields.get("state")
            if p.state == "done" and new_state == "review":
                # done 강등 사유를 관찰 가능하게: 무엇이 증거로 인정되는지 안내
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", DONE_GATE_MSG.format(iid=issue_id), dbmod.now()))
                c.commit()
            elif p.state == "done" and new_state == "done" and gate_warn:
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server",
                           f"⚠ done 됐지만 완료증거 코멘트가 없음 (TT_DONE_GATE=warn — M3BZV172-9F0S). "
                           f"나중에라도: tt verify {issue_id} <증거>", dbmod.now()))
                c.commit()
            if new_state in ("done", "cancelled"):
                # ③ reconcile: terminalize된 카드의 실행 중인 agent에게 release 명령 (best-effort)
                reconcile_release(c, issue_id, f"카드 {new_state} 전이", "board")
                # ④ 이 카드를 의존하던 blocked 카드들에 '해제 가능' 알림 (자동 재dispatch 없음)
                notify_blocked_dependents(c, issue_id, new_state)
                c.commit()
            if new_state == "blocked" and fields.get("waiting_for") == "human":
                # blocked(waiting_for=human) → Level4 알림 주입 (M3BZV172-9F0S B)
                escalate_blocked_human(c, issue_id, source="field")
            elif fields.get("waiting_for") == "human" and row["state"] == "blocked":
                # 이미 blocked인 카드에 사족 보강으로 human이 된 경우에도 1회 (dedup은 필드)
                escalate_blocked_human(c, issue_id, source="field")
            row = get_issue(c, issue_id)
            return enrich_blocked(c, dbmod.to_dict(row))

    @app.post("/issues/{issue_id}/verify")
    def verify_issue(issue_id: str, p: VerifyIn):
        """Accept a completion report; this does not attest external execution."""
        with con() as c:
            c.execute("BEGIN IMMEDIATE")
            row = get_issue(c, issue_id)
            if row["state"] != "review":
                raise HTTPException(409, f"state is {row['state']} — review만 verify 가능")
            n_open = c.execute(
                "SELECT COUNT(*) n FROM issues WHERE parent_id=? AND state NOT IN ('done','cancelled')",
                (issue_id,)).fetchone()["n"]
            if n_open:
                raise HTTPException(409, f"child 미완료 {n_open}건 — 자식을 done/cancelled로 먼저 종결하세요")
            proof = p.completion_report
            if requires_report(row) and not proof:
                raise HTTPException(422, "completion_report required by this attempt's work contract")
            if proof:
                check_report(row, proof)
                if proof.result != "passed":
                    raise HTTPException(422, "completion report must pass before verify")
                ev = proof.evidence
            elif p.evidence.strip():
                ev = p.evidence.strip()
                if legacy_result(ev) != "passed":
                    raise HTTPException(422, "evidence must contain a successful result; prefer completion_report")
            else:
                ev = find_evidence(c, issue_id, row)
                if not ev:
                    raise HTTPException(422, "successful current-attempt evidence or completion_report required")
            if p.expected_version is not None and p.expected_version != row["version"]:
                raise HTTPException(409, "version conflict")
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, p.verifier, f"verify → done (reported). evidence: {ev[:300]}", dbmod.now()))
            fields = {**reported_fields(ev), "state": "done", "completed_at": dbmod.now(),
                      "completion_report": proof.model_dump_json() if proof else "",
                      "lease_by": "", "lease_expires": None, "waiting_for": "", "waiting_actor": "",
                      "blocked_detail": "", "blocked_notified_at": None}
            bump(c, issue_id, fields, row["version"])
            reconcile_release(c, issue_id, "verify로 done 확정", p.verifier)
            notify_blocked_dependents(c, issue_id, "done")
            c.commit()
            row = get_issue(c, issue_id)
        return enrich_blocked(c, dbmod.to_dict(row))

    @app.post("/issues/{issue_id}/comments", status_code=201)
    def add_comment(issue_id: str, p: CommentIn):
        with con() as c:
            row = get_issue(c, issue_id)
            c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                      (issue_id, p.author, p.body, dbmod.now()))
            c.execute("UPDATE issues SET updated_at=?, version=version+1 WHERE id=?", (dbmod.now(), issue_id))
            c.commit()
            # legacy 러너 호환 (M3BZV172-9F0S B): blocked 카드에 waiting_for=human 마커 코멘트가
            # 도착하면(runner BLOCKED 서식) 필드 없이도 human 알림 1회 발동.
            if row["state"] == "blocked" and "waiting_for=human" in p.body:
                escalate_blocked_human(c, issue_id, source="comment")
        return {"ok": True}

    # ---- done≠verified + blocked→human escalation (M3BZV172-9F0S) ----

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
        lines = [f"⏸ TT blocked(Level4) — {t['title']}",
                 f"이슈: {t['issue_id']}  waiting_for={t['waiting_for']}  액터: {t['waiting_actor']}"]
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
            if NOTIFY_BASE and url.startswith("/"):
                url = NOTIFY_BASE + url
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

    @app.get("/issues/{issue_id}/why-blocked")
    def why_blocked(issue_id: str):
        """blocked 사유의 기계 판독 투영: 게이트·기준·누락 증거·권장 명령. (②)
        자동 생성이 아니라 waiting_for/blocked_detail/코멘트 기반 투영 — 필드 없으면 legacy 코멘트."""
        with con() as c:
            row = get_issue(c, issue_id)
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
            deps = dep_states(c, dep_ids(d)) if (wf == "dependency") else []
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
                crit = WAIT_CRITERIA[wf] + (f" (액터: {d['waiting_actor'] or '미지정'})")
                cmds = [f'tt note {issue_id} "<결정/답변>"', f"tt edit {issue_id} --state todo"]
            elif wf == "gate":
                crit = WAIT_CRITERIA[wf]
                cmds = [f'tt dispatch {issue_id} -A <agent> "승인"', f"tt edit {issue_id} --state todo"]
            elif wf == "dependency":
                crit = WAIT_CRITERIA[wf]
                cmds = [f"tt show {i}" for i in missing] + \
                       ([f"tt edit {issue_id} --state todo"] if not missing else [])
            elif wf == "external":
                crit = WAIT_CRITERIA[wf]
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

    # ---- agent registry + hook/callback dispatch ----

    def valid_url(u):
        # "/hook" 같은 상대경로는 TT_NOTIFY_BASE 접두어로 해석되는 notify 전용 주소 (M3BZV172-9F0S B)
        return u.startswith("http://") or u.startswith("https://") or (NOTIFY_BASE and u.startswith("/"))

    @app.get("/agents")
    def list_agents():
        with con() as c:
            rows = c.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    @app.post("/agents", status_code=201, summary="Register a webhook agent (receives dispatch POSTs at base_url)", description="base_url must be a reachable HTTP endpoint (hook mode). Agents without a listener cannot receive dispatch — see /api.md integration table. release_hook=true 선언 시 카드 terminalize 시 release 명령(x-tt-command: release)도 수신 — runner류만 켠다. notify_hook=true 시 blocked(waiting_for=human) Level4 알림(x-tt-command: notify) 수신 — 알림 주입 어댑터(Hermes)용.")
    def add_agent(p: AgentIn):
        name = p.name.strip()
        if not name or not valid_url(p.base_url):
            raise HTTPException(422, "name 필수, base_url은 http(s)만 허용")
        with con() as c:
            if c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
                raise HTTPException(409, f"agent {name} 이미 등록됨")
            c.execute("INSERT INTO agents (name, base_url, secret, enabled, release_hook, notify_hook, created_at) VALUES (?,?,?,?,?,?,?)",
                      (name, p.base_url, p.secret, 1 if p.enabled else 0, 1 if p.release_hook else 0,
                       1 if p.notify_hook else 0, dbmod.now()))
            c.commit()
            row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        return dict(row)

    @app.patch("/agents/{name}")
    def patch_agent(name: str, p: AgentPatch):
        fields: dict = {}
        if p.base_url is not None:
            if not valid_url(p.base_url):
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
        with con() as c:
            if not c.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
                raise HTTPException(404, f"agent {name} not found")
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE agents SET {sets} WHERE name=?", (*fields.values(), name))
                c.commit()
            row = c.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        return dict(row)

    @app.delete("/agents/{name}")
    def delete_agent(name: str):
        with con() as c:
            res = c.execute("DELETE FROM agents WHERE name=?", (name,))
            c.commit()
            if res.rowcount != 1:
                raise HTTPException(404, f"agent {name} not found")
        return {"ok": True}

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

    @app.post("/issues/{issue_id}/dispatch", status_code=201,
            summary="Dispatch: deliver an instruction to a registered agent via webhook (agent needs a listening base_url; poll-only agents should use todo+comments instead)",
            description="Records message as a comment, POSTs the payload to the agent webhook (10s timeout), stores optional resume context, and files a tt-server system comment on delivery failure. Never changes issue state.")
    def dispatch(issue_id: str, p: DispatchIn, request: Request):
        if not p.message.strip():
            raise HTTPException(422, "message 비어 있음")
        with con() as c:
            issue = get_issue(c, issue_id)
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
            "work_contract": json.loads(issue["work_contract"]) if issue["work_contract"] else contract,
            "execution_attempt": issue["execution_attempt"],
        }
        status, detail, ctx = "ok", "", ""
        try:
            code, ctx = deliver(ag["base_url"], ag["secret"], payload)
            if code >= 300:
                status, detail = "error", f"HTTP {code}"
        except Exception as e:
            status, detail = "error", f"{type(e).__name__}: {e}"[:300]
        with con() as c:
            c.execute("UPDATE dispatches SET status=?, detail=?, context=? WHERE id=?", (status, detail, ctx, did))
            c.execute("UPDATE agents SET last_ok=?, last_err=? WHERE name=?",
                      (dbmod.now() if status == "ok" else ag["last_ok"],
                       "" if status == "ok" else detail, p.agent))
            if status == "error":
                c.execute("INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
                          (issue_id, "tt-server", f"⚠ hook dispatch #{did} → {p.agent} 실패: {detail}", dbmod.now()))
            c.commit()
            row = c.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone()
        return dict(row)

    @app.get("/issues/{issue_id}/dispatches")
    def list_dispatches(issue_id: str):
        with con() as c:
            get_issue(c, issue_id)
            rows = c.execute("SELECT * FROM dispatches WHERE issue_id=? ORDER BY id", (issue_id,)).fetchall()
        return [dict(r) for r in rows]

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app(os.environ.get("TT_DB", os.path.expanduser("~/.local/share/think-tank/tt.db")))
