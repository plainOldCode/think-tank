"""probe 발령 실행 규칙(execute) — TMXJ-S4.

_EXEC_HANDLERS 디스패치 테이블과 _exec_* 핸들러, 그리고 execute() 게이트
(재확인 후 핸들러 호출). 접근은 probe.http, 마커/증거는 markers, 리스 타이머는
decide_rules 보조를 쓴다."""
import os
import sys
import time
import urllib.error

from .markers import _RELAY_OBSERVERS, _comment_after, _ev_open, _iso
from .decide_rules import _lease_held, _relay_timer_s


def _core_attr(name):
    """probe.core 표면 지연 조회 — 테스트가 probe.core의 심볼을 monkeypatch하는
    계약 유지(TMXJ-S4). 핸들러 본문은 원문 그대로 두고 이름 해석만 core로 돌린다."""
    from . import core
    return getattr(core, name)


def api(url, path, method="GET", body=None):
    return _core_attr("api")(url, path, method, body)


def get_version(url, iid):
    return _core_attr("get_version")(url, iid)


def gh_json(*args):
    return _core_attr("gh_json")(*args)


def _gh_pr_head(repo, pr_no):
    return _core_attr("_gh_pr_head")(repo, pr_no)


def _occupies_card(row, execution_attempt=None):
    return _core_attr("_occupies_card")(row, execution_attempt)


def _archive_sweep(url, days):
    """서버 내장 스윕 호출 — 후보 선정·나이·조상 체인 검증은 전부 서버가 나이순으로
    원자 수행(M3ZRGTSF-N95X). probe는 사이클당 한 번 걸어줄 뿐이다."""
    if days <= 0:
        return
    try:
        res = api(url, "/maintenance/archive-sweep", "POST", {})
    except urllib.error.HTTPError as e:
        if e.code not in (404, 409):
            print(time.strftime("%F %T"), f"[archive] 스윕 실패: HTTP {e.code}", flush=True)
        return
    except Exception as e:
        print(time.strftime("%F %T"), f"[archive] 스윕 실패: {e}", flush=True)
        return
    if isinstance(res, dict) and (res.get("archived") or res.get("preserved")):
        print(time.strftime("%F %T"),
              f"[archive] {res.get('archived', 0)}카드 아카이브, "
              f"{res.get('preserved', 0)}보존 (done+{days}일, 조상 done)", flush=True)


def _exec_archive_sweep(url, act):
    _archive_sweep(url, int(act.get("days") or 0))
    return


def _exec_ci_fix(url, act):
    cur = api(url, f"/issues/{act['issue']}")
    if act.get("subject"):
        from review_subject import subject
        from delivery import TARGET_KEYS
        from .providers import inspect_delivery
        if cur.get("state") != "review" or subject(cur) != act["subject"]:
            return
        observed = inspect_delivery(act["delivery"], gh_json)
        if (observed.get("state") != "OPEN" or observed.get("isDraft")
                or any(act["delivery"].get(k) != observed.get(k) for k in TARGET_KEYS)
                or not any(c.get("state") == "FAILURE" for c in observed.get("checks") or [])):
            return
    att = cur.get("execution_attempt") or 0
    sha8 = (act.get("head_sha") or "")[:8]
    # decide가 액션에 동봉한 마커 우선 — ip 변형([ci-fix-ip a{n} …])도
    # 저장 마커와 판정 키가 일치해야 다음 사이클 재판정이 막힌다(JXYS R2).
    marker = act.get("marker") or f"[ci-fix a{att} #{act['pr']}/{sha8}]"
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return  # 경합 방어 — decide 판정 후 재확인
    agent = os.environ.get("TT_CI_FIX_AGENT", "kanban-adapter")
    target = os.environ.get("TT_CI_FIX_TARGET", "agy")
    msg = (f"[auto probe] PR #{act['pr']} ({act.get('repo','')}) CI 실패"
           f"(failing: {','.join(act.get('checks_failed') or [])}). 임무: PR을 읽고 **수정**할 것"
           f" — 단순 review 금지, 목적은 CI red 해소. 브랜치 {act.get('branch','')} 위에서 커밋 계속, "
           f"범위는 CI 실패 수정만(리팩·스킵·assertion 약화 금지). push 후 즉시 종료 — "
           f"CI 재판정·병합은 probe가 자동 진행하니 CI·리뷰를 기다리거나 폴링하지 말 것"
            f"(다음 단계 필요 시 보드가 재호출). 인수: {target} 담당.")
    if act.get("subject"):
        msg += (" 수정 push 후 delivery.head_sha를 새 전체 SHA로 갱신하고 "
                "tt submit --report로 같은 회차 보고를 재제출해야 재리뷰·병합된다. "
                "PR 제목/브랜치와 변경 산출물을 보고에 명시하라.")
    try:
        api(url, f"/issues/{act['issue']}/dispatch", "POST", {"agent": agent, "message": msg})
    except Exception as e:
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} dispatch 실패({str(e)[:80]}) — "
                                        "사람 판단 대기 — 재위임 실패"})
        return
    # 작업자 wake(M4T4WW0N-7FQH): assignee가 있으면 첫 줄에 [CI:fail] — relay가
    # 이 신호로 터미널을 깨운다. preview가 120자로 잘리므로 첫 줄이어야 한다.
    # 마커 dedup은 본문 전체 스캔이라 줄 순서는 자유.
    assignee = cur.get("assignee") or ""
    wake = (f"[CI:fail] @{assignee} #{act['pr']} — "
            f"{', '.join(act.get('checks_failed') or ['?'])} 확인하라\n") if assignee else ""
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "probe", "body": f"{wake}"
                                    f"{marker} PR#{act['pr']} CI 실패 — {agent}({target}) "
                                    "review+수정 위임"})
    return


def _exec_submit_remind(url, act):
    # JXYS 2 — green 무보고 독려 1회. receive_mode=relay(기본)면 멘션이 닿지
    # 않으므로 워치독 다이제스트(보드)에 릴레이 플래그.
    cur = api(url, f"/issues/{act['issue']}")
    marker = act.get("marker") or ""
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return
    who = cur.get("assignee") or ""
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "probe",
         "body": f"{marker} @{who} PR#{act['pr']} CI green — 완료 보고 제출"
                 "(tt submit --report)이 review 진입. 무반응 시 타이머 경과 후 "
                 "자동 승격됩니다(점진 push는 head 변경으로 타이머 리셋)."})
    agents = [a for a in api(url, "/agents") or []]
    if _relay_timer_s(who, agents) >= 6 * 3600:
        api(url, "/messages", "POST",
            {"author": "probe",
             "body": (f"@{who} " if who else "")
                     + f"[submit-remind] 카드 {act['issue']} PR#{act['pr']} — "
                       "CI green 완료 보고 제출 요망 — tt submit --report(독려: 자동 승격 예정)"})
    return


def _exec_submission_adopt(url, act):
    # JXYS 3 — 무반응 승격: in_progress → review. 완료(done)는 보고 의존 유지.
    cur = api(url, f"/issues/{act['issue']}")
    marker = act.get("marker") or ""
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return
    # 실행 직전 재확인 — decide 이후 상태가 바뀌었으면 폐기
    if cur.get("state") != "in_progress" \
            or (cur.get("execution_attempt") or 0) != (act.get("att") or 0) \
            or _lease_held(cur) or cur.get("waiting_for") == "human":
        return
    try:
        _dfx = api(url, f"/issues/{act['issue']}/dispatches") or []
    except Exception:
        _dfx = []
    if any(_occupies_card(x, cur.get("execution_attempt")) for x in _dfx):
        return
    _rmdt = _iso(act.get("remind_ts") or "")
    if _rmdt and any((x.get("author") or "") not in _RELAY_OBSERVERS
                     and not str(x.get("author") or "").startswith("runner")
                     and _comment_after(x, _rmdt, act.get("remind_id") or 0)
                     for x in (cur.get("comments") or [])):
        return  # decide 이후 응답·활동 — 승격 취소
    if act.get("repo") and act.get("pr") and \
            _gh_pr_head(act.get("repo"), act.get("pr")) != \
            (act.get("head_sha") or "")[:8]:
        return  # 실제 head 변경 — 타이머 리셋이므로 승격 폐기
    try:
        api(url, f"/issues/{act['issue']}", "PATCH",
            {"state": "review",
             "expected_version": act.get("expected_version")
             or cur.get("version")})
    except Exception:
        return  # CAS 경합 — 다음 사이클 재판정
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "probe",
         "body": f"{marker} 기계적 승격 — green 무보고 독려 후 무반응"
                 "(타이머 경과, head 불변). in_progress → review. "
                 "완료 보고는 승격 후에도 유효 — 병합 verify는 유효 보고 필요"})
    return


def _exec_review_note(url, act):
    cur = api(url, f"/issues/{act['issue']}")
    if "draft" in act.get("reason", ""):
        # draft ready 요청 — decide가 넘긴 마커로 PR 단위 dedup
        marker = act.get("marker") or ""
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} {act['reason']}"})
        return
    if act.get("marker"):
        # 전용 마커 노트(evidence-rejected 등) — needs-merge 마커와 독립 dedup.
        # decide가 넘긴 마커를 그대로 영속화한다(리뷰 R3: 전용 마커 유지).
        marker = act["marker"]
        if "evidence-rejected" in marker:
            # 액션의 대상 회차가 현재와 다르면 폐기(리뷰 R5: 재수령 뒤 옛
            # 회차 액션 집행 금지 — 회차별 dedup 유지).
            if (act.get("attempt") or 0) != (cur.get("execution_attempt") or 0):
                return
            # 판정과 같은 미해결 조건(JXYS R1 잔여) — done 거부 또는 무보고
            # 병합 미접수. 후자는 _evidence_unresolved만으로 못 본다.
            if not _ev_open(cur):
                return  # 재조회 시점에 이미 해소(성공 보고 접수) — 리뷰 R5
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} {act['reason']}"})
        return
    att = cur.get("execution_attempt") or 0
    # U4 퇴역 — needs-merge 기본 마커는 제거. 마커 없는 review-note는
    # 회차 마커로 영속화한다(evidence-rejected 등은 위 전용 마커 분기).
    marker = act.get("marker") or f"[review-note a{att}]"
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "probe", "body": f"{marker} {act['reason']} — "
                                    "tt verify로 done 확정 또는 재작업"})
    return act


def _exec_resume(url, act):
    v = get_version(url, act["issue"])
    api(url, f"/issues/{act['issue']}", "PATCH", {"state": "todo", "version": v})


def _exec_needs_human(url, act):
    suffix = "" if act.get("note") else "(30KP 예산 게이트)"
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "dispatchd",
         "body": f"[auto] {act.get('note') or act['reason']} — 라벨 needs-human 자동화 중단{suffix}"})
    cur = api(url, f"/issues/{act['issue']}")
    labels = sorted((set(cur.get("labels") or []) | {"needs-human"}) - {"auto"})
    api(url, f"/issues/{act['issue']}", "PATCH", {"labels": labels})
    return act


def _exec_work(url, act):
    # claim 409(선점·수신 계약 위반 등)는 경합 — 사이클 전체 중단 대신 폐기.
    # 다음 사이클 판정이 다시 대상을 고른다(JXYS R1 패턴과 동일).
    try:
        api(url, f"/issues/{act['issue']}/claim", "POST", {"agent": act["agent"]})
    except Exception as e:
        print(time.strftime("%F %T"), f"[dispatchd] work claim 폐기 {act['issue']}"
              f" → {act['agent']}: {str(e)[:120]}", flush=True)
        return act
    msg = (f"[auto dispatchd] 작업 {act['issue']}: {act['reason']}. 수령(claim)·lease heartbeat·"
           f"3단계 완료 보고(계약 v2)를 준수할 것. tt pull(풀) 금지. "
           f"자기 단계(구현·push·보고)가 끝나면 즉시 종료 — CI·리뷰 결과를 기다리거나 "
           f"폴링하지 말 것(후속 전이는 probe 소유, 필요 시 보드가 재호출). "
           f"코드 작업은 브랜치 tt/{act['issue']}-<slug> 등 해당 저장소 규약을 따르고 "
           f"산출물·검증 증거를 TT에 제출하라. PR은 선택적 보조 수단(GitHub/Gitea/local 지원). "
           f"병합·배포 권한은 사용자 지시를 따른다. tt show {act['issue']} 로 상세 확인 후 시작.")
    api(url, f"/issues/{act['issue']}/dispatch", "POST",
        {"agent": act["agent"], "message": msg})
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "dispatchd", "body": f"[auto] dispatch → {act['agent']} ({act['reason']})"})

    return act


_EXEC_HANDLERS = {
    "archive-sweep": _exec_archive_sweep,
    "ci-fix": _exec_ci_fix,
    "submit-remind": _exec_submit_remind,
    "submission-adopt": _exec_submission_adopt,
    "review-note": _exec_review_note,
    "resume": _exec_resume,
    "needs-human": _exec_needs_human,
    "work": _exec_work,
}


def execute(url, act):
    """액션 집행 — kind별 핸들러 위임(AK87-2 dispatch table).

    핸들러는 None(기록 완료) 또는 act(폐기·완료 신호)를 반환한다. 미지정 kind는
    원 구현과 동일하게 tail 공유 get_version 후 act 반환(관측 동작 유지)."""
    kind = act["action"]
    if kind in {"tt-review", "tt-review-fix", "tt-verify", "tt-review-stalled", "tt-deliver", "tt-delivery-note"}:
        from . import tt_lifecycle
        try:
            tt_lifecycle.execute(sys.modules["probe.core"], url, act)
        except Exception as e:
            # Failed claims, stale reports and transport failures must not kill
            # the whole cycle or write a success/dedup marker.
            print(time.strftime("%F %T"), f"[tt-review] {act['issue']}: {e}", flush=True)
        return
    handler = _EXEC_HANDLERS.get(kind)
    if handler is not None:
        return handler(url, act)
    # U4 퇴역 — 레거시 액션(kind가 핸들러에서 빠짐)은 무시하고 로그만 남긴다.
    print(time.strftime("%F %T"),
          f"[execute] {act['issue']}: 알 수 없는 액션 {kind!r} — 무시", flush=True)
    return act
