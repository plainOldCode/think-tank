"""probe 판정 규칙(decide) — TMXJ-S3.

_decide_* 규칙군·_DecideCtx·decide()와 그 보조(트랙 점수·예산 게이트·에스컬레이션·
리스 판정). 순수 판정 — 실행은 exec_rules(S4), 접근은 http, 마커/라우팅/카드는
각 모듈. tt_lifecycle.plan에는 core 계약 모듈(sys.modules["probe.core"])을 넘긴다
— 재수출 표면이 그대로 계약이다."""
import os
import re
import sys
import time
from datetime import datetime, timedelta

import failure_rules
import policy

DAEMON_AUTHOR = "tt-embedding-daemon"  # decide 보고 author — core에서 이동(S3)

from .cards import REPO, card_repo, ci_passed, pr_card_id
from .markers import (_AUTO_CLASSIFIED_RE, _ESC_FMT, _RELAY_OBSERVERS, _comment_after,
                      _comment_ts, _ev_open, _iso, _probe_marker, _repeat_count, _suppressed)
from .routing import NON_EXECUTORS, _assignable, effective_review_map


AC_PRIO_WEIGHT = 0.5  # score→우선순위 보정 상한(±0.5) — 사람이 지정한 priority를 넘지 않게


def _prio(i):
    p = i.get("priority")
    return (p is None, p if p is not None else 999, i["id"])


def _auto_todo(issues, exclude=()):
    return [i for i in issues if i["state"] == "todo"
            and "auto" in (i.get("labels") or []) and i["id"] not in exclude]


def _auto_classified_label(i):
    """카드의 [auto-classified] 마커 라벨(데몬 코멘트만). 없으면 None(1KFE)."""
    for c in (i.get("comments") or []):
        if (c.get("author") or "") != DAEMON_AUTHOR:
            continue
        m = _AUTO_CLASSIFIED_RE.search(c.get("body") or "")
        if m:
            return m.group(1)
    return None


def _label_track(issues, label, exclude_id):
    """동일 라벨 선행 카드 성적(1KFE) — 라벨 '필드' 기준(마커 아님, 오염 방지).
    score = done비율 − needs-human비율 − min(.5, max(0, 평균 attempt−1)×.1) ∈ [−1.5, 1].
    선행 카드가 없으면 None — 미매칭 라벨은 무시(기존 순위 유지)."""
    priors = [x for x in issues
              if x["id"] != exclude_id and label.lower() in policy._labels(x)]
    if not priors:
        return None
    n = len(priors)
    done_r = sum(1 for x in priors if x["state"] == "done") / n
    nh_r = sum(1 for x in priors if x.get("waiting_for") == "human") / n
    # M5BNMHB7-RBQE: 라벨 트랙 점수도 claim 횟수 기준 — 본문 수정은 노이스다.
    atts = [float(x.get("claim_count") if x.get("claim_count") is not None
                  else x.get("execution_attempt") or 0) for x in priors]
    avg_att = sum(atts) / n
    score = done_r - nh_r - min(0.5, max(0.0, avg_att - 1) * 0.1)
    return {"n": n, "done_r": done_r, "nh_r": nh_r, "avg_att": avg_att, "score": score}


def _budget_blocked(i):
    # TT 개선#3b: 예산 판정은 policy 단일 모듈로 — reason code도 policy 것을 씀
    return policy.budget_reason(i)


def _escalate(actions, i, code, scope, count):
    """사람 통보 1회(기존 level4-notify/needs-human 경로) — 마커 dedup."""
    esc = _ESC_FMT.format(code, scope)
    if any(esc in (c.get("body") or "") for c in (i.get("comments") or [])):
        return
    actions.append({"agent": "dispatchd", "issue": i["id"], "action": "needs-human",
                    "note": f"{esc} 반복 사건 {count}회 도달 — 사람 확인 전까지 "
                            "이 키의 자동 대응 중단(§6). 해제: [unblocked (코드, 범위)] 코멘트"})


def _lease_held(i, now=None):
    """유효 임대 보유 여부 — lease_expires가 now(기본 실시간) 이후면 활동 중.

    dispatch는 임대를 만들지 않고(라우터 summary: Never changes issue state)
    claim만 만들므로, 이 가드는 '수령해 일하는 중' 카드만 막는다(Q05B claude 등
    busy_agents 미집계 로컬 워커 포함, 리뷰 R2)."""
    lex = i.get("lease_expires") or ""
    if not lex:
        return False
    try:
        lt = datetime.fromisoformat(lex)
    except ValueError:
        return False
    if now:
        try:
            nt = datetime.fromisoformat(now)
        except ValueError:
            return False
    else:
        nt = datetime.now(lt.tzinfo) if lt.tzinfo else datetime.now()
    try:
        return nt < lt
    except TypeError:  # aware/naive 혼합(이형 입력) — 보수적으로 활동 중으로 본다
        return True


def _relay_timer_s(assignee, agents):
    """무반응 양도 타이머(초) — receive_mode: hook 1h / relay(기본) 6h.

    watcher 모드는 선언 주기×3(RQRT) — 주기 없는 watcher는 relay 기본값.
    미선언 에이전트는 relay 기본값(카드 정책)."""
    mode = ""
    for a in agents or []:
        if a.get("name") == assignee:
            mode = (a.get("receive_mode") or "").strip()
            iv = a.get("watcher_interval")
            if mode == "watcher" and iv and int(iv) >= 1:
                return int(iv) * 3 * 60  # 주기×3 — watcher 폴링 계약 정합
            break
    return int({"hook": 1.0, "relay": 6.0}.get(mode, 6.0) * 3600)


class _DecideCtx:
    """decide() 단계 공유 문맥(AK87-1) — 750줄 단일 함수를 위임형으로 분할한다.

    계약: skip은 snap["probe_skips"]와 동일 객체(로그 계약), actions/claimed/
    gated는 단계 간 누적 상태. 각 단계는 actions에만 기록하고 판정 입력을
    변경하지 않는다(동작 불변)."""

    def __init__(self, snap):
        self.snap = snap
        self.issues = snap.get("issues") or []
        # RQRT: 수신 계약 미선언 등록 에이전트는 배정 후보 제외 — claim 409 반복으로
        # 사이클 전체가 중단되는 것을 배정 단계에서 원천 차단. 비등록 세션은
        # 스냅샷 agents에 없으므로 영향 없다(암묵 relay).
        self.agents = [a for a in (snap.get("agents") or []) if _assignable(a)]
        self.now = snap.get("now") or ""
        self.actions = []
        self.claimed = set()
        self.ac_logged = set()  # [auto-classified] 보정 로그 — 카드당 1줄(1KFE 수용기준 1)
        self.by_id = {i["id"]: i for i in self.issues}
        # TT 개선#3c R3: 유휴 판정은 작업 lease뿐 아니라 리뷰 점유 lease도 포함 +
        # 이번 사이클에 이미 배정된(작업·리뷰) 에이전트는 재배정하지 않는다.
        self.busy_agents = {i.get("lease_by") for i in self.issues
                            if i.get("lease_by") and i.get("lease_expires")
                            and (not self.now or i["lease_expires"] > self.now)}
        self.busy_agents.discard(None)
        # NB5T(M58SPX0G-NB5T) — 관계 그래프 투영(사이클당 1회): decide의 문자열
        # 재파싱 지점을 질의로 대체한다. 동작 불변 — golden 9종이 하드 조건.
        from ontology.build import build_graph
        self.graph = build_graph(snap)
        rmap = effective_review_map()  # 카운터파트 라우팅(M4JTJ970-WS3C) — 저장 설정이 덮는다
        # 런타임 설정(XV84) — 저장 설정이 코드 기본을 덮는다. decide는 순수 함수라
        # snap에 조달된다(snapshot에서 GET /settings/review-routing).
        routing = snap.get("review_routing") or {}
        self.rmap = {**rmap, **(routing.get("review_map") or {})}
        self.fb = routing.get("review_fallback")
        self.tt_cards = set()   # ⓪ TT-submitted 카드 — _decide_tt_lifecycle이 채움
        self.actor = None       # ①에서 산출 — ③ budget 통보 대행자


def _decide_idle(name, issues, now):
    return not any(i["state"] == "in_progress" and i.get("assignee") == name
                   and (i.get("lease_expires") or "9999") > now for i in issues)


def _decide_pick(cands, issues, now, skip, ac_logged):
    """가장 낮은 우선순위의 배정 가능 후보(동률 id순). 없으면 None.
    TT 개선#3b: 기각 시 policy reason code를 probe_skips에 남긴다.
    1KFE: [auto-classified] 마커 카드는 동일 라벨 선행 카드 성적 score로
    우선순위를 ±AC_PRIO_WEIGHT 보정한다. 마커 없음·미매칭 라벨은 기존 _prio."""
    def key(i):
        p_none, p, iid = _prio(i)
        lab = _auto_classified_label(i)
        if lab is None:
            return (p_none, p, iid)
        tr = _label_track(issues, lab, i["id"])
        if tr is None:
            return (p_none, p, iid)
        if i["id"] not in ac_logged:
            ac_logged.add(i["id"])
            print(time.strftime("%F %T"),
                  f"[auto-classified] {i['id']} label={lab} 선행 {tr['n']}건 "
                  f"(done {tr['done_r']:.0%}, nh {tr['nh_r']:.0%}, "
                  f"att {tr['avg_att']:.1f}) score {tr['score']:+.2f} "
                  f"→ prio 보정 {-tr['score'] * AC_PRIO_WEIGHT:+.2f}", flush=True)
        return (p_none, p - tr["score"] * AC_PRIO_WEIGHT, iid)
    for i in sorted(cands, key=key):
        ok, code = policy.eligible(i, now=now, auto=True)
        if ok:
            return i
        if code and i["id"] not in skip:
            skip[i["id"]] = code
    return None


def _decide_assign(ctx):
    """① release-ready 재개 + ② 에이전트별 작업 배정.

    True 반환 = ① 재개 발생 — decide는 즉시 종료한다(수동 재개 규약의 기계 대행)."""
    issues, agents, now = ctx.issues, ctx.agents, ctx.now
    actions, claimed, skip, ac_logged = ctx.actions, ctx.claimed, ctx.skip, ctx.ac_logged
    resumable = [i for i in issues if i["state"] == "blocked"
                 and i.get("waiting_for") == "dependency" and i.get("release_ready")]
    ctx.actor = next((a["name"] for a in agents
                      if a.get("name") not in NON_EXECUTORS and _decide_idle(a["name"], issues, now)), None)
    if resumable and ctx.actor:
        t = resumable[0]
        actions.append({"agent": ctx.actor, "issue": t["id"], "action": "resume",
                        "reason": "release-ready"})
        return True

    # ② agent별 작업 대상: continuation(자식→형제) → auto 풀 — 실행 불가 에이전트 제외(PXCJ)
    for a in agents:
        name = a["name"]
        if name in NON_EXECUTORS or not _decide_idle(name, issues, now):
            continue
        dones = [i for i in issues if i["state"] == "done" and i.get("assignee") == name]
        target = reason = None
        if dones:
            last = max(dones, key=lambda i: i.get("updated_at") or "")
            kids = _auto_todo(issues, claimed | {last["id"]})
            kids = [k for k in kids if k.get("parent_id") == last["id"]]
            target, reason = _decide_pick(kids, issues, now, skip, ac_logged), "continuation-child"
            if target is None:
                sibs = [s for s in _auto_todo(issues, claimed | {last["id"]})
                        if last.get("parent_id") and s.get("parent_id") == last["parent_id"]]
                target, reason = _decide_pick(sibs, issues, now, skip, ac_logged), "continuation-sibling"
        if target is None:
            pool = [p for p in _auto_todo(issues, claimed)
                    if not (dones and p["id"] == dones[0]["id"])]
            target, reason = _decide_pick(pool, issues, now, skip, ac_logged), "pool"
        if target is None:
            continue
        claimed.add(target["id"])
        claimed.add(name)  # TT 개선#3c R3: 배정받은 에이전트는 이번 사이클 리뷰 금지
        actions.append({"agent": name, "issue": target["id"], "action": "work",
                        "reason": reason})
    return False


def _decide_tt_lifecycle(ctx):
    """⓪ 전단 — TT-submitted 카드 라이프사이클 계획. tt_cards를 채운다."""
    from . import tt_lifecycle
    issues, agents, now = ctx.issues, ctx.agents, ctx.now
    actions, claimed, busy_agents = ctx.actions, ctx.claimed, ctx.busy_agents
    ctx.tt_cards = {i["id"] for i in issues if tt_lifecycle.eligible(i)}
    for i in issues:
        if i["id"] in ctx.tt_cards:
            tt_actions = tt_lifecycle.plan(sys.modules["probe.core"], i, agents,
                                           busy_agents | claimed, now,
                                           delivery_context=(ctx.snap.get("delivery_context") or {}).get(i["id"]),
                                           linked=any(pr_card_id(p) == i["id"] for p in ctx.snap.get("prs") or []),
                                           discovered=(ctx.snap.get("discovered_delivery") or {}).get(i["id"]),
                                           patch_ids=ctx.snap.get("patch_ids"),
                                           rmap=ctx.rmap, fallback=ctx.fb)
            actions.extend(tt_actions)
            claimed.update(a["agent"] for a in tt_actions if a["agent"] != "probe")


def _decide_in_progress(ctx, i, p, iid, pr_repo):
    """⓪c JXYS(M4MGV3BY) — in_progress 자동화: 리뷰 전용 자동화의 사각지대.

    in_progress+PR에서도 CI 실패는 위임, green 무보고는 독려→무반응 승격.
    전이는 probe가, 완료(done)는 보고서 의존 유지 — probe가 보고를 위조하지 않는다.
    True 반환 = 이 PR 판정 종료(호출자 continue). False = 통과(게이트 판정 계속)."""
    now, agents, actions = ctx.now, ctx.agents, ctx.actions
    att_ip = i.get("execution_attempt") or 0
    sha8_ip = (p.get("head_sha") or "")[:8]
    checks_ip = p.get("checks") or []
    states_ip = [c.get("state") for c in checks_ip]
    fails_ip = [x for x in states_ip if x == "FAILURE"]
    # 1) CI 실패 → 기존 ci-fix 흐름(위임 대상 동일) — 실패 PR은 미완성이어서 안전
    if fails_ip and os.environ.get("TT_CI_FIX_AGENT", "kanban-adapter"):
        marker_ip = f"[ci-fix-ip a{att_ip} #{p['number']}/{sha8_ip}]"
        sup_ip, since_ip = _suppressed(i, "ci_failed_ip", f"#{p['number']}/{sha8_ip}")
        if not sup_ip and not _probe_marker(i, marker_ip):
            n_ip = failure_rules.suppress_n()
            scope_ip = f"#{p['number']}/{sha8_ip}"
            if not (n_ip and _repeat_count(
                    i, rf"\[ci-fix-ip a\d+ {re.escape(scope_ip)}\]",
                    since_ip) >= n_ip):
                actions.append({"agent": "dispatchd", "issue": iid,
                                "action": "ci-fix", "marker": marker_ip,
                                "att": att_ip, "pr": p["number"], "repo": pr_repo,
                                "branch": p.get("branch", ""),
                                "head_sha": p.get("head_sha", ""),
                                "cycle_id": ctx.snap.get("cycle_id"),
                                "checks_failed": [c.get("name") or "?" for c in checks_ip
                                                  if c.get("state") == "FAILURE"]})
        return True
    # 2) green + 무보고 → 독려 1회 → 무반응 승격. head가 바뀌면 새 sha의
    #    독려 마커가 새로 필요 — 타이머가 자연 리셋(점진 push 납치 방지).
    if not ci_passed(p):
        return False
    smarker = f"[submit-remind a{att_ip} #{p['number']}/{sha8_ip}]"
    amarker = f"[submission-adopt a{att_ip} #{p['number']}/{sha8_ip}]"
    if _probe_marker(i, amarker):
        return True  # 이미 승격됨 — 승격 후에도 보고 제출은 가능
    if not _probe_marker(i, smarker):
        actions.append({"agent": "probe", "issue": iid,
                        "action": "submit-remind", "marker": smarker,
                        "att": att_ip, "pr": p["number"], "repo": pr_repo,
                        "head_sha": p.get("head_sha", ""),
                        "reason": f"PR#{p['number']} CI green — 완료 보고 제출"
                                  "(tt submit --report)이 review 진입"})
        return True
    sc = next((c for c in (i.get("comments") or [])
               if c.get("author") == "probe"
               and smarker in (c.get("body") or "")), None)
    sdt = _iso(_comment_ts(sc)) if sc else None
    if not sdt or _lease_held(i, now):
        return True  # 유효 임대 = 활동 중 — 무반응이 아님
    if (datetime.fromisoformat(now) - sdt).total_seconds() <= \
            _relay_timer_s(i.get("assignee") or "", agents):
        return True  # 독려 타이머 미경과(hook 1h / relay 6h)
    if any((x.get("author") or "") not in _RELAY_OBSERVERS
           and not str(x.get("author") or "").startswith("runner")
           and _comment_after(x, sdt, sc.get("id") or 0)
           for x in (i.get("comments") or [])):
        return True  # 응답·활동 중 — 승격 금지
    actions.append({"agent": "probe", "issue": iid,
                    "action": "submission-adopt", "marker": amarker,
                    "att": att_ip, "pr": p["number"], "repo": pr_repo,
                    "head_sha": p.get("head_sha", ""),
                    "remind_ts": _comment_ts(sc),
                    "remind_id": sc.get("id") or 0,
                    "expected_version": i["version"],
                    "reason": f"PR#{p['number']} green 무보고 — 기계적 승격"
                              "(review). 완료 보고는 승격 후에도 유효"})
    return True


def _decide_pr_pipeline(ctx):
    """⓪ probe 파이프라인 — U4(M5A4HYY7-3BCA) 퇴역 후 잔여 자동화.

    레거시 PR 경로(PR-only 자동 병합·리뷰 게이트·J2NJ·재작업 relay)는 제거됐다.
    리뷰·병합의 유일한 경로는 TT 제출물(tt_lifecycle)이다. 남는 것:
    - in_progress 자동화(JXYS): CI 실패 위임(ci-fix-ip)·green 무보고 독려·승격.
    - T3YT: review 카드의 PR 관측 실패는 'PR 없음' 판정 대신 observe-failed."""
    for p in ctx.snap.get("prs") or []:
        iid = pr_card_id(p)
        i = ctx.by_id.get(iid) if iid else None
        if not iid or i is None or i["state"] in ("done", "cancelled"):
            continue
        if iid in ctx.tt_cards or p.get("isDraft"):
            continue
        if i["state"] == "in_progress" and not i.get("busy_agents"):
            _decide_in_progress(ctx, i, p, iid, p.get("repo") or REPO)
    # T3YT(M58SPT6D-T3YT) — TT가 아닌 review 카드에서 PR 후보 관측 실패 시
    # observe_failed(§4). 'PR 없음' 판정(needs-merge)은 레거시 퇴역으로 함께 제거.
    for i in ctx.issues:
        if i["state"] != "review" or i["id"] in ctx.tt_cards:
            continue
        att = i.get("execution_attempt") or 0
        if not att or not i.get("work_contract"):
            continue
        prs_for = [p for p in ctx.snap.get("prs") or [] if pr_card_id(p) == i["id"]]
        if not prs_for:
            failed = _pr_list_failed(ctx, i)
            if failed:
                _decide_observe_failed(ctx, i, att, failed)


def _pr_list_failed(ctx, i):
    """T3YT(M58SPT6D-T3YT) — 이 카드의 'PR 없음' 판정이 관측 실패로 오염될 수 있는지.

    반환: 실패한 후보 repo 집합(빈 set=판정 가능). 후보는 repo 마커·코멘트 PR URL·
    pr_context의 repo. 후보를 전혀 모르면 전체 degraded일 때 보수적으로 실패로 본다
    — 카드의 PR이 실패한 repo에 숨어 있을 수 있기 때문."""
    observed = ctx.snap.get("pr_observed") or {}
    if not observed:
        return set()
    cands = {card_repo(i)} | {repo for repo, _ in ctx.graph.comment_pr_repos(i["id"])} | {
        p.get("repo") for p in (ctx.snap.get("pr_context") or {}).get(i["id"], [])}
    cands.discard(None)
    failed = {r for r in cands if observed.get(r) == "failed"}
    if failed:
        return failed
    if not cands and ctx.snap.get("degraded"):
        return {"unknown"}
    return set()


def _decide_observe_failed(ctx, i, att, failed):
    """observe_failed(§4): 관측 실패 사이클 — 다음 사이클 재시도, 반복 시 경고(§6).

    상태 변경·needs-merge 노트 없음. 마커 [observe-failed a<n>]로 회차당 1회 dedup."""
    actions = ctx.actions
    scope = "+".join(sorted(failed))
    osup, osince = _suppressed(i, "observe_failed", scope)
    on = failure_rules.suppress_n()
    if osup:
        print(time.strftime("%F %T"),
              f"억제 중 (observe_failed, {scope}) — 관측 재시도 스킵(§6)", flush=True)
        return
    if on and _repeat_count(i, r"\[observe-failed a\d+\]", osince) >= on:
        _escalate(actions, i, "observe_failed", scope, on)
        return
    marker = f"[observe-failed a{att}]"
    if not any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe"):
        actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                        "marker": marker,
                        "reason": f"PR 관측 실패({scope}) — 조회 실패와 PR 없음을 구분. "
                                  "다음 사이클 재시도"})


def _decide_budget(ctx):
    """③ 예산 소진 카드 — dispatch 대신 needs-human (auto 풀 전수)."""
    for i in _auto_todo(ctx.issues, ctx.claimed):
        why = _budget_blocked(i)
        if why:
            ctx.actions.append({"agent": ctx.actor or "dispatchd", "issue": i["id"],
                                "action": "needs-human", "reason": why})


def _decide_archive_sweep(ctx):
    """④ 자동 아카이브 스윕(M3ZRGTSF-N95X) — TT_ARCHIVE_AFTER_DAYS>0일 때 사이클당 1회."""
    try:
        _ad = int(os.environ.get("TT_ARCHIVE_AFTER_DAYS", "0") or 0)
    except ValueError:
        _ad = 0
    if _ad > 0:
        ctx.actions.append({"agent": "probe", "action": "archive-sweep", "days": _ad})


def _decide_evidence_rejected(ctx):
    """⑤ evidence_rejected(규칙표 §4) — done 거부 시스템 기록(tt-server DONE_GATE_MSG
    코멘트)이 있는 비완료 카드: 담당 assignee 재제출 요청 노트(회차당 1회 dedup).
    서버는 거부 시 state=review로 돌리므로 review 포함 — 실제 발생 상태(리뷰 R3)."""
    now, agents, actions = ctx.now, ctx.agents, ctx.actions
    for i in ctx.issues:
        if i.get("archived") or i["state"] in ("done", "cancelled"):
            continue
        att = i.get("execution_attempt") or 0
        emarker = f"[evidence-rejected a{att}]"
        cs = i.get("comments") or []
        ev_open = _ev_open(i)
        sup_ev, _ = _suppressed(i, "evidence_rejected", i["id"])
        if not ev_open or sup_ev:
            continue
        if not _probe_marker(i, emarker):
            actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                            "marker": emarker, "attempt": att, "cycle_id": ctx.snap.get("cycle_id"),
                            "reason": f"done 거부(증거 없음/실패) 또는 무보고 병합 — "
                                      f"assignee 재제출 요청(계약 v2 3단계 보고, "
                                      f"tt submit {i['id']} --report)"})
            continue
        # 독려 후 무반응 — 수신 방식별 타이머(hook 1h/relay 6h) 경과+활동 없음 →
        # needs-human 1회(§6 마커 dedup). 카운터가 아니라 시각 기반(JXYS R1):
        # 같은 회차 반복 거부는 마커 dedup으로 카운터가 멈추므로.
        ec = max((c for c in cs if c.get("author") == "probe"
                  and emarker in (c.get("body") or "")),
                 key=lambda c: (_comment_ts(c), c.get("id") or 0), default=None)
        edt = _iso(_comment_ts(ec)) if ec else None
        if not edt:
            continue
        if (datetime.fromisoformat(now) - edt).total_seconds() \
                <= _relay_timer_s(i.get("assignee") or "", agents):
            continue
        if any((x.get("author") or "") not in _RELAY_OBSERVERS
               and not str(x.get("author") or "").startswith("runner")
               and _comment_after(x, edt, ec.get("id") or 0) for x in cs):
            continue  # 응답·활동 중
        _escalate(actions, i, "evidence_rejected", i["id"], 0)


def decide(snap):
    """probe 판정 엔진 — 스냅샷을 읽어 액션 목록을 반환한다(AK87-1 위임형).

    각 단계는 _decide_* 핸들러로 분리돼 있다: ①재개+②배정 → ⓪TT 라이프사이클
    → ⓪PR 파이프라인(in_progress·T3YT) → ③예산 → ④아카이브 → ⑤evidence
    레거시 PR 경로(병합·needs-merge·재작업 relay)는
    U4(M5A4HYY7-3BCA)로 퇴역했다. probe_skips는 snap에 기록(로그 계약),
    actions/claimed는 단계 간 누적 상태다."""
    if not snap.get("auto"):
        return []
    ctx = _DecideCtx(snap)
    # TT 개선#3b: 배정 못 한 카드의 reason code — run_once가 로그로 남긴다.
    # 에이전트 유무와 무관하게 전체 카드를 정책으로 스캔(상태 조합 매트릭스의 로그 실측).
    snap["probe_skips"] = {}
    ctx.skip = snap["probe_skips"]
    for i in ctx.issues:
        ok, code = policy.eligible(i, now=ctx.now, auto=True)
        if not ok and code:
            ctx.skip[i["id"]] = code
    if _decide_assign(ctx):
        return ctx.actions  # ① release-ready 재개 — 사이클 즉시 종료
    _decide_tt_lifecycle(ctx)
    _decide_pr_pipeline(ctx)
    _decide_budget(ctx)
    _decide_archive_sweep(ctx)
    _decide_evidence_rejected(ctx)
    return ctx.actions
