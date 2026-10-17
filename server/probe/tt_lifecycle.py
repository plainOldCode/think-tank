"""TT owns submission/review/verification; providers supply optional context."""
from datetime import datetime
import hashlib

from review_subject import subject, verdict
from verification import CompletionReport
from delivery import requirement, readiness, TARGET_KEYS
from . import providers


def eligible(issue):
    if issue.get("state") != "review" or issue.get("archived"):
        return False
    report = issue.get("completion_report")
    contract = issue.get("work_contract") or {}
    if not report or not subject(issue):
        return False
    try:
        proof = CompletionReport.model_validate(report)
    except (ValueError, TypeError):
        return False
    return (proof.result == "passed" and proof.attempt == issue.get("execution_attempt")
            and proof.contract_version == contract.get("version"))


def marker(issue, kind):
    return f"[{kind} {subject(issue)}]"


def marked(issue, key):
    return any(c.get("author") == "probe" and (c.get("body") or "").startswith(key)
               for c in issue.get("comments") or [])


def plan(core, issue, agents, busy, now, delivery_context=None, linked=False,
         discovered=None, patch_ids=None):
    """No gh dependency. Only the stored submission and its TT verdict authorize done."""
    token = subject(issue)
    common = {"issue": issue["id"], "subject": token, "agent": "probe"}
    decision = verdict(issue)
    target = requirement(issue, linked)
    # SRJY(M5283M00-SRJY): 보고에 delivery를 명시하지 않았고 조인되는 open PR이
    # 정확히 1개면(run_once가 관측·검사해 전달), 그 PR을 산출물로 연결한다.
    # 명시적 delivery(report/local/pr)는 절대 덮지 않는다.
    if discovered and not (issue.get("completion_report") or {}).get("delivery"):
        target = discovered
    delivery_problem = readiness(target, delivery_context, patch_ids)
    if delivery_problem:
        # Keep the existing CI-fix delegation for a bound, unchanged open PR.
        # A failed check is repairable work, not permission to finish locally.
        observed = delivery_context or {}
        if (target["mode"] == "pr" and observed.get("state") == "OPEN"
                and not observed.get("isDraft") and not issue.get("busy_agents")
                and all(target.get(k) == observed.get(k) for k in TARGET_KEYS)
                and any(c.get("state") == "FAILURE" for c in observed.get("checks") or [])
                and core.os.getenv("TT_CI_FIX_AGENT", "kanban-adapter")):
            key = marker(issue, "tt-ci-fix")
            if not marked(issue, key):
                return [{**common, "action": "ci-fix", "pr": target["number"],
                         "repo": target["repo"], "head_sha": target["head_sha"],
                         "marker": key, "delivery": target,
                         "checks_failed": [c.get("name") or "?" for c in observed["checks"]
                                           if c.get("state") == "FAILURE"]}]
        tag = hashlib.sha256(delivery_problem.encode()).hexdigest()[:12]
        key = marker(issue, f"tt-delivery-hold-{tag}")
        if marked(issue, key):
            return []
        return [{**common, "action": "tt-delivery-note", "marker": key, "reason": delivery_problem}]
    # Same-family exclusion also applies to manually claimed TT reviews.
    reviewer = issue.get("reviewer")
    if reviewer and core._impl_family(issue.get("assignee")) and \
            core.agent_family(reviewer) == core._impl_family(issue.get("assignee")):
        return []
    if decision == "approve":
        return [{**common, "action": "tt-deliver" if target["mode"] == "pr" else "tt-verify",
                 "reason": "current TT submission approved",
                 **({"delivery": target, "patch_ids": dict(patch_ids or {})}
                    if target["mode"] == "pr" else {})}]
    if issue.get("busy_agents"):
        return []
    if decision == "request-changes":
        key = marker(issue, "tt-review-fix")
        if marked(issue, key):
            return []
        executor = core._resolve_executor(issue.get("assignee"), agents)
        if not executor or executor in core.NON_EXECUTORS or executor in busy:
            return []
        return [{**common, "action": "tt-review-fix", "agent": executor, "marker": key}]
    key = marker(issue, "tt-review")
    previous = [c for c in issue.get("comments") or []
                if c.get("author") == "probe" and (c.get("body") or "").startswith(key)]
    if previous:
        # Bounded retry after a dead reviewer; no new head/PR is needed for local work.
        try:
            elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(previous[-1]["ts"])).total_seconds()
        except (ValueError, TypeError, KeyError):
            return []
        if elapsed < 3600 or (issue.get("lease_expires") or "") > now:
            return []
        if len(previous) >= 2:
            notice = marker(issue, "tt-review-stalled")
            if marked(issue, notice):
                return []
            return [{**common, "action": "tt-review-stalled", "marker": notice}]
    target = core.pick_reviewer(issue.get("assignee"), agents, busy, core.effective_review_map())
    if not target:
        return []
    if not core.policy.review_eligible(issue, now=now, agent=target)[0]:
        return []
    return [{**common, "action": "tt-review", "agent": target, "marker": key,
             "retry": len(previous)}]


def execute(core, url, act):
    """Re-read before effects; /verify rechecks subject+verdict inside its transaction."""
    iid = act["issue"]
    cur = core.api(url, f"/issues/{iid}")
    if not eligible(cur) or subject(cur) != act["subject"]:
        return
    decision = verdict(cur)
    kind = act["action"]
    if kind == "tt-delivery-note":
        if not marked(cur, act["marker"]):
            core.api(url, f"/issues/{iid}/comments", "POST", {
                "author": "probe", "body": f"{act['marker']} {act['reason']}",
            })
        return
    if kind == "tt-review-stalled":
        if decision is None and not marked(cur, act["marker"]):
            core.api(url, f"/issues/{iid}/comments", "POST", {
                "author": "probe", "body": f"{act['marker']} 리뷰 재시도 후 무판정 — 리뷰어 인수계 필요",
            })
        return
    if kind in ("tt-verify", "tt-deliver"):
        if decision != "approve":
            return
        target = act.get("delivery") or requirement(cur)
        receipt = None
        if kind == "tt-deliver":
            if target["mode"] != "pr":
                return
            observed = providers.inspect_delivery(target, core.gh_json)
            pids = act.get("patch_ids") or {}  # SRJY — decide가 모은 patch-id 재사용
            if readiness(target, observed, pids):
                return
            # Fresh TT approval before the external write, then provider head CAS.
            fresh = core.api(url, f"/issues/{iid}")
            if not eligible(fresh) or subject(fresh) != act["subject"] or verdict(fresh) != "approve":
                return
            if observed["state"] == "OPEN":
                if core.policy.is_high_risk(fresh):
                    # FHH8 — 고위험 카드 probe 병합 휴면. 사람이 GitHub에서 직접 병합하면
                    # 다음 사이클의 MERGED 관측으로 완결된다.
                    key = marker(fresh, "tt-high-risk-hold")
                    if not marked(fresh, key):
                        core.api(url, f"/issues/{iid}/comments", "POST", {
                            "author": "probe",
                            "body": f"{key} 고위험(risk:*) 라벨 — probe 자동병합 휴면, "
                                    "사람 병합 승인 대기"})
                    return
                providers.merge_delivery(target, core.gh_exec, head=observed.get("head_sha"))
                observed = providers.inspect_delivery(target, core.gh_json)
            if observed.get("state") != "MERGED" or readiness(target, observed, pids):
                return  # queued/failed merge is not delivery evidence
            receipt = {k: observed[k] for k in (*TARGET_KEYS, "merge_commit", "merged_at")}
            cur = core.api(url, f"/issues/{iid}")
            if not eligible(cur) or subject(cur) != act["subject"] or verdict(cur) != "approve":
                return
        elif target["mode"] not in ("report", "local"):
            return
        core.api(url, f"/issues/{iid}/verify", "POST", {
            "verifier": "probe", "completion_report": cur["completion_report"],
            "review_subject": act["subject"], "expected_version": cur["version"],
            **({"delivery_receipt": receipt} if receipt else {}),
            # SRJY — 자동 연결 병합: 보고에 delivery가 없으므로 병합 대상을 증거로 제시
            **({"delivery": target} if receipt and target.get("mode") == "pr" else {}),
            **({"patch_ids": [{"repo": k[0], "sha": k[1], "patch_id": v}
                              for k, v in (act.get("patch_ids") or {}).items()]}
               if receipt and act.get("patch_ids") else {}),
        })
        return
    if kind == "tt-review" and decision is not None:
        return
    target_spec = requirement(cur)
    if target_spec["mode"] == "unbound-pr":
        return
    if target_spec["mode"] == "pr" and readiness(target_spec, providers.inspect_delivery(target_spec, core.gh_json)):
        return
    if kind == "tt-review":
        previous = [c for c in cur.get("comments") or [] if c.get("author") == "probe"
                    and (c.get("body") or "").startswith(act["marker"])]
        if len(previous) != act.get("retry", 0):
            return
    if kind == "tt-review-fix" and (decision != "request-changes" or marked(cur, act["marker"])):
        return
    dispatches = core.api(url, f"/issues/{iid}/dispatches") or []
    if any(d.get("run_state") in core._ACTIVE_RUN_STATES for d in dispatches):
        return
    target = act["agent"]
    if kind == "tt-review":
        claim = core.api(url, f"/issues/{iid}/claim-review", "POST", {"agent": target})
        # claim-review replaces work_contract in its response with the review
        # contract; the server-derived subject still names the implementation.
        if claim.get("review_subject") != act["subject"]:
            return
        message = (
            f"[auto review] TT 카드 {iid}, 제출물 {act['subject']}, 재시도={act.get('retry', 0)}. "
            f"리뷰어 점유 완료(agent={target}). GET /issues/{iid}의 completion_report와 "
            "acceptance를 읽고 보고가 가리키는 커밋/문서/로컬 산출물 및 실행 증거를 독립 검증하라. "
            "GitHub/Gitea PR은 있으면 보조 자료로 사용한다. 자료 접근 불가를 승인으로 간주하지 말라. "
            "delivery.mode=pr이면 보고의 head_sha와 실제 PR head가 같아야 한다. "
            "다른 head를 검사했다면 승인 대신 보고 재제출을 요구하라. 승인 후 probe가 병합을 확인한다. "
            "구현 코드는 수정하지 않는다. 판정 정본은 TT 카드 코멘트이며 PR 코멘트는 선택적 미러다. "
            f"author={target}, 첫 줄 'review: approve' 또는 'review: request-changes', "
            f"둘째 줄은 정확히 '{act['subject']}'. 근거와 검증 한계를 덧붙이고 종료하라."
        )
    else:
        if cur.get("reviewer"):
            core.api(url, f"/issues/{iid}", "PATCH", {
                "reviewer": "", "expected_version": cur["version"],
            })
        message = (
            f"[review-fix] TT 카드 {iid}, 제출물 {act['subject']} request-changes. "
            "TT 카드의 리뷰 지적을 수정하고 검증한 뒤 같은 회차에서 "
            f"tt submit {iid} --report report.json 으로 재제출하라. "
            "새 review_subject가 발급되므로 이전 승인은 재사용되지 않는다. "
            "PR은 선택적 보조 수단이며 저장소별 절차를 따른다. 제출 후 기다리지 말고 종료하라."
        )
        # 369Y(M5283JJH-369Y): 반복 지적 구조화 + 3회 반려 premise 재검토 강제
        _secs = core._correct_loop_sections(cur)
        if _secs:
            message = message + "\n\n" + "\n\n".join(_secs)
            _att = cur.get("execution_attempt") or 0
            act["loop_notes"] = [
                ("[correct-loop premise a%s]" % _att if "premise 재검토" in s
                 else "[correct-loop structure a%s]" % _att, s)
                for s in _secs]
            # RPXH(M53PBDWJ-RPXH): 3회+ 반려는 상태로 남긴다 — best-effort 기록,
            # 실패해도 dispatch는 계속(지시문 섹션이 1차 방어).
            if any("premise 재검토" in s for s in _secs):
                try:
                    core.api(url, f"/issues/{iid}", "PATCH", {"premise_rework": True})
                except Exception:
                    pass
        # RPXH — 카드 간 반복 유형 전환 권장(실패·미배포 서버는 조용히 생략)
        _cs = core._conversion_section(core._rejection_summary(url), cur)
        if _cs:
            message = message + "\n\n" + _cs
    # Re-read at the dispatch boundary, not just at the planning boundary.
    fresh = core.api(url, f"/issues/{iid}")
    if not eligible(fresh) or subject(fresh) != act["subject"]:
        return
    payload = {
        "agent": target, "message": message,
    }
    if kind == "tt-review":
        payload.update(work_contract=core.REVIEW_CONTRACT, fresh=True)
    delivered = core.api(url, f"/issues/{iid}/dispatch", "POST", payload)
    if (delivered or {}).get("status") == "error":
        return  # no success marker; server's idempotent redelivery can recover
    core.api(url, f"/issues/{iid}/comments", "POST", {
        "author": "probe", "body": f"{act['marker']} reviewer={target}",
    })
    # 369Y — 루프 신호를 카드에 기록(dedup: 같은 키는 회차당 1회). 발령 실패 시 미기록.
    for _key, _sec in act.get("loop_notes") or []:
        if not marked(cur, _key):
            core.api(url, f"/issues/{iid}/comments", "POST", {
                "author": "probe", "body": f"{_key} {_sec[:300]}"})
