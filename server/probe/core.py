#!/usr/bin/env python3
"""tt probe — TT 자율 스케줄러. decide()는 판정 순수 함수, 실행기는 public API만 소비.

30KP 설계 docs/archive/dispatchd.md. 서버 프로세스 내장 스레드(TT_PROBE_INTERVAL, app.py)로만
구동 — standalone dispatchd shim·plist·TT_DISPATCH_* 는 U6(M5A4HZQB-AT3N)으로 퇴역.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .providers import inspect_delivery  # noqa: F401 — _prepare_delivery_context 모듈 스코프용
from verdicts import classify_rejection as _classify_rejection, first_line_verdict  # 9WWK 단일 파서
from verdicts import is_machine_author as _is_machine_author  # 71NK RC — 반려 집계 기계 author 제외
from datetime import datetime, timedelta

import failure_rules
import judge

# TMXJ-S1 — 리프 심볼을 모듈로 이동했다. 아래 재수출로 기존 소비처
# (`from probe.core import X`, probe/__init__)의 import 표면이 그대로 유지된다.
from .http import GH, _gh, _post_json, api, get_version, gh_exec, gh_json  # noqa: F401
from .cards import (CARD_IN_ANY, CARD_REPO, PR_URL, REPO, REPO_CARDS,  # noqa: F401
                    _PATCH_ID_CACHE, _comment_pr_repos, _gh_pr_head, _repos_default,
                    card_from_branch, card_repo, ci_passed, collect_prs,
                    collect_repos, patch_id_for, patch_ids_for, pr_card_id)
# TMXJ-S2 — 마커·증거·억제 규칙과 라우팅 규칙을 모듈로 이동했다. 재수출 유지.
from .markers import (CI_BOTS, _AUTO_CLASSIFIED_RE, _DISPATCH_ID_RE, _ESC_FMT,  # noqa: F401
                      _RELAY_OBSERVERS, _RUNNER_FAIL_RE, _UNBLOCK_FMT, _comment_after,
                      _comment_ts, _correct_loop_sections, _evidence_unresolved,
                      _ev_open, _iso, _probe_marker, _rejection_verdicts,
                      _repeat_count, _suppressed)
from .routing import (DEFAULT_REVIEW_MAP, NON_EXECUTORS, REVIEW_FALLBACK,  # noqa: F401
                      _alias_resolve, _assignable, _impl_family, _receivable,
                      _resolve_agent, _resolve_executor, _routing_family,
                      effective_review_map, pick_reviewer, route_reviewer)
# TMXJ-S3 — 판정 규칙(decide)과 그 보조를 모듈로 이동했다. 재수출 유지.
from .decide_rules import (AC_PRIO_WEIGHT, _DecideCtx, _auto_classified_label,  # noqa: F401
                           _auto_todo, _budget_blocked, _decide_archive_sweep,
                           _decide_assign, _decide_budget, _decide_evidence_rejected,
                           _decide_idle, _decide_in_progress, _decide_observe_failed,
                           _decide_pick, _decide_pr_pipeline, _decide_tt_lifecycle,
                           _escalate, _label_track, _lease_held, _prio, _relay_timer_s,
                           DAEMON_AUTHOR, decide)

from service import REVIEW_CONTRACT
from config import PROBE_ACTIVE_RUN_STATES  # W0DF — 활성 run 상태 단일 정의
import policy





























# --- 카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C, docs/archive/review-gate.md 룰셋) ---

# 자기 리뷰 방지용 고정 가족 어휘 — 매핑 확장과 무관하게 이 어휘로만 제외한다
# (매핑 키 codex-labs->codex로 가족 경유 자기 리뷰를 만들 수 없다)
# cursor: cursor-review(읽기전용 리뷰 프로필)는 가족 동반 — 구현 cursor의 자기 리뷰 방지(XV84)
# 3QMV — 가족 어휘·해석 규칙은 server/identity.py 단일 소스로 이동. 아래
# 임포트가 기존 소비처(core 내부·테스트·지표 스크립트)의 이름을 그대로 유지한다.
from identity import (FAMILY_ALIASES, REVIEW_FAMILIES, agent_family,  # noqa: F401
                      effective_host_family_map, parse_host_family_map)
from identity import alias_resolve as _identity_alias_resolve
from identity import resolve as _identity_resolve
# 발령자 결정(2026-10-10) 기본 카운터파트 표 — 저장 설정(review-routing)으로 override·확장
# (D3: env 레이어 제거 — 웹 편집(XV84)으로 충분, mini에서 env 미사용 실측)
# 리뷰어 미가용 폴백 순서 — 발령자 결정 갱신(2026-10-14, XV84): agy→cursor-review→
# codex→claude. cursor-review는 cursor의 읽기전용 리뷰 프로필. 런타임 순서 조정은
# 설정(settings/review-routing, 웹 편집)이 이 기본값을 덮는다 — 재배포 불필요.
# 1순위는 카운터파트 표(DEFAULT_REVIEW_MAP)가 정하고, 이 체인은 그 뒤를 잇는다.
# 진행 중 run 상태 — config.PROBE_ACTIVE_RUN_STATES 단일 정의를 쓴다(W0DF).
# 서버 dispatch 투영(queued/running/stalled) + 러너 장부 상태(finalizing/held).
_ACTIVE_RUN_STATES = PROBE_ACTIVE_RUN_STATES


def _occupies_card(row, execution_attempt=None):
    """True when this dispatch should block a new review or fix spawn.

    Terminal rows do not. An attempt strictly behind the card's current
    execution_attempt is a leftover from an earlier round (M54E72RQ-SXGS) and
    does not either — a killed session whose close was rejected used to sit here
    forever and starve reviewer assignment. A missing attempt stays blocking.
    """
    if not isinstance(row, dict) or row.get("run_state") not in _ACTIVE_RUN_STATES:
        return False
    attempt = row.get("attempt")
    if execution_attempt is not None and attempt is not None and attempt < execution_attempt:
        return False
    return True
# 실행 불가 에이전트 — dispatch를 보드 트리아지 변환·판정 전용으로 소비한다(코드 작업 불가).
# work/resume/review-fix 배정 대상에서 제외(M4NCNNRW-PXCJ) — 2026-10-11 RTZ1: auto 풀이
# kanban-adapter에 review-fix를 배정해 트리아지 카드만 만들고 정체(#343).
# CI 봇은 구현 에이전트가 아니다 — 미표기 취급(기본 라우팅)








# agent_family는 identity.py로 이동 — 위 임포트로 재수출된다.














# --- 369Y(M5283JJH-369Y): 같은 지적 반복은 구조로 막기 — /correct 루프 차단 ---

# 반려 사유 버킷(키워드 분류 — 대소문자 무시, 순서 민감: migration이 contract의
# '스키마' 키워드보다 우선). 미분류는 general — 구조화 대상에서 뺀다.




def _rejection_summary(url, days=30):
    """RPXH(M53PBDWJ-RPXH) — 카드 간 반려 유형 집계. 실패·미배포 서버면 {} (369Y 카드 내 집계만)."""
    try:
        d = api(url, f"/rejections/summary?days={days}")
    except Exception:
        return {}
    return d if isinstance(d, dict) and d.get("buckets") is not None else {}


def _conversion_section(summary, cur):
    """RPXH — 발령문에 붙일 카드 간 전환 권장 섹션. 순수 함수.

    규칙: 최근 기간 count>=3 AND cards>=2 인 버킷만 — 단발 지적·한 카드 내 반복은 제외.
    369Y의 카드 내 구조화 승격과 짝을 이룬다(반복이 구조를 넘어 다른 카드에도 번지면 전환).
    """
    if not isinstance(summary, dict):
        return ""
    cur_buckets = {_classify_rejection(c.get("body") or "") for c in _rejection_verdicts(cur)}
    hot = [b for b in (summary.get("buckets") or [])
           if b.get("count", 0) >= 3 and b.get("cards", 0) >= 2 and b["bucket"] != "general"][:3]
    if not hot:
        return ""
    days = summary.get("days") or 30
    lines = [f"- {b['bucket']}: 최근 {days}일 {b['count']}회(카드 {b['cards']}장) 반복 — "
             "이 유형은 리뷰 지적으로 남기 말고 lint/테스트/계약 체크로 전환하라"
             for b in hot]
    touched = [b["bucket"] for b in hot if b["bucket"] in cur_buckets]
    if touched:
        lines.append(f"이 카드의 반려도 {'/'.join(touched)} 유형 — 수정과 함께 전환 작업을 노트로 남겨라.")
    return "--- 반복 유형 전환 (RPXH) ---\n" + "\n".join(lines)






# BG97 relay 관측자 — 이 author들의 코멘트는 '활동'으로 세지 않는다(관측·시스템 기록).
# runner 계열(runner@host)도 활동에서 제외한다 — 세션 수명 관측(queued/done/failed/
# STALL-killed)일 뿐 워커 진행 증거가 아니므로, 실패 기록 하나가 무반응 양도를
# 영구 차단해선 안 된다(리뷰 R5).








# ⑥ 반복 사건 억제(§6, TT M4K87YR0-HN7E) — 같은 (사유코드, 범위) 반복 N회 도달 시
# 사람 통보 1회 후 해당 키의 자동 대응 스킵. 카운터는 probe 마커 코멘트에서 집계
# (서버 스키마 변경 없음). 해제는 사람 전용 — [unblocked (코드, 범위)]를 probe/
# dispatchd가 아닌 author가 남겼을 때만 카운터가 그 시점 이후로 재집계된다.
# H 처리(budget_exhausted 등)와 reviewer_session_failed(자체 N=3)는 억제하지 않는다.











def review_repeat_warn(url, iid, repeats):
    """90Y9(M58SPVD8-90Y9) — 리뷰 반복 경고 가설 1건(판단 로그) + hermes hook 위임.

    리뷰 반복(request-changes 누적)은 경계 확장의 정상 진행이라 스킵하지 않는다
    (사용자 결정 2026-10-19). work 단계 산출물이라 품질이 낮으므로 승인 완료 후
    right 단계(리팩토링) 후속이 권장된다 — fast(병목 확인)는 별개.
    기록 실패가 review-fix dispatch를 취소하지 않는다(기록 우선)."""
    try:
        import judge as _judge  # 서버 루트 모듈 — probe 패키지 멤버가 아니다
        _judge.JudgeLog().record(
            point="review-repeat", judge="probe", mode="shadow", output="warn",
            rule_output="review-repeat", gate="allow", card=iid,
            confidence="high", rule_version=_judge.RULE_VERSION,
            schema_version=_judge.SCHEMA_VERSION,
            evidence=[{"card": iid, "reason_code": f"request-changes x{repeats}"},
                      {"reason_code": "suggested_next=refactor-right"}])
    except Exception as e:
        print(time.strftime("%F %T"), f"review-repeat 경고 기록 실패({e}) — 생략", flush=True)
    try:
        for ag in api(url, "/agents") or []:
            ag = ag if isinstance(ag, dict) else {}
            if ag.get("name") == "hermes-m2max" and str(ag.get("base_url") or "").startswith("http"):
                _post_json(ag["base_url"], {"type": "review-repeat-warn",
                                            "card": iid, "repeats": repeats})
                break
    except Exception as e:
        print(time.strftime("%F %T"), f"review-repeat hook 실패({e}) — 생략", flush=True)














def hydrate_reviews(url, snap):
    """게이트 판정용 코멘트 취득 — open PR에 묶인 후보 카드 중 코멘트가 아직 없는 것만.

    snapshot은 review 카드 코멘트만 취득하므로(in_progress 등 병합 후보 누락),
    run_once에서 decide 전에 채운다(판정 dedup 마커도 같은 코멘트를 읽음).
    """
    cand = {pr_card_id(p) for p in (snap.get("prs") or []) if pr_card_id(p)}
    for i in snap.get("issues") or []:
        if i["id"] in cand and "comments" not in i:
            try:
                i["comments"] = api(url, f"/issues/{i['id']}").get("comments") or []
            except Exception:
                i["comments"] = []


















































# SRJY — patch-id 프로세스 캐시: (repo, base, sha) → patch-id. 프로브 주기마다
# 같은 head를 재계산하지 않게 한다(서버 재시작 시 재계산 — 무해).








collect_prs.observed = {}


def _promote_pr_observation(snap):
    """T3YT(M58SPT6D-T3YT) — collect_prs가 실어 보낸 repo별 관측을 snap에 승격.

    '조회 실패'와 'PR 없음'을 판정이 구분하게 한다. degraded는 실패가 있던
    관측 단계 목록. 소비 후 속성을 초기화해 다음 사이클 오염을 막는다."""
    observed = dict(getattr(collect_prs, "observed", None) or {})
    snap["pr_observed"] = observed
    snap["degraded"] = ["pr_list"] if "failed" in set(observed.values()) else []
    collect_prs.observed = {}
    return snap




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


def execute(url, act):
    """액션 집행 — kind별 핸들러 위임(AK87-2 dispatch table).

    핸들러는 None(기록 완료) 또는 act(폐기·완료 신호)를 반환한다. 미지정 kind는
    원 구현과 동일하게 tail 공유 get_version 후 act 반환(관측 동작 유지)."""
    kind = act["action"]
    if kind in {"tt-review", "tt-review-fix", "tt-verify", "tt-review-stalled", "tt-deliver", "tt-delivery-note"}:
        from . import tt_lifecycle
        try:
            tt_lifecycle.execute(sys.modules[__name__], url, act)
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


# action kind → 실행 함수 (원 if/elif 체인의 1:1 이관 — 각 본문 불변)
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


def snapshot(url):
    issues = []
    while True:
        page = api(url, f"/issues?limit=1000&offset={len(issues)}")
        if not page:
            break
        issues.extend(page)
        # 실제 반환 수로 전진하고 빈 페이지에서 종료: 서버의 페이지 상한이
        # 요청한 1000보다 낮아도 다음 페이지의 오래된 카드를 놓치지 않는다.
    agents = api(url, "/agents")
    for i in issues:
        # 판정에 추가로 필요한 값만 해당 카드의 detail에서 (전수 GET 최소화)
        auto = "auto" in (i.get("labels") or [])
        if i["state"] == "blocked" and i.get("waiting_for") == "dependency":
            try:
                i["release_ready"] = api(url, f"/issues/{i['id']}/why-blocked").get(
                    "release_ready", False)
            except Exception:
                i["release_ready"] = False
        if auto or i.get("assignee"):
            d = api(url, f"/issues/{i['id']}/dispatches")
            i["dispatches"] = len(d) if isinstance(d, list) else 0
            # 진행 중 dispatch의 에이전트 집합 — decide가 리뷰/수정 중복 스폰을 막는다(JWW0)
            i["busy_agents"] = sorted({x.get("agent") for x in (d or [])
                                       if _occupies_card(x, i.get("execution_attempt"))
                                       and x.get("agent")})
        if i["state"] == "review":
            # review-note dedup 판정용 — review 카드는 상세(코멘트 포함)를 별도 취득
            try:
                i["comments"] = api(url, f"/issues/{i['id']}").get("comments") or []
            except Exception:
                i["comments"] = []
    snap = {"auto": os.getenv("TT_AUTO_DISPATCH") == "1",
            "now": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "agents": agents, "issues": issues}
    # 리뷰어 라우팅 런타임 설정(XV84) — 설정 서버가 없거나 키가 없으면 기본 동작
    try:
        s = api(url, "/settings/review-routing")
        if isinstance(s, dict) and s.get("source") == "stored":
            snap["review_routing"] = {"review_fallback": s.get("review_fallback"),
                                      "review_map": s.get("review_map")}
    except Exception:
        pass  # 설정 조회 실패는 기본 동작 회귀(의도된 degrade)
    return snap


def _sleep(seconds, stop=None):
    """stop 이벤트가 있으면 wait(즉시 깨움), 없으면 sleep. False=중단 요청."""
    if stop is None:
        time.sleep(seconds)
        return True
    return not stop.wait(seconds)


def _prepare_delivery_context(snap: dict, _req, gh_json) -> dict:
    """SRJY(M5283M00-SRJY) — 제출 시점 PR 자동 연결·delivery 관측·patch-id 선계산.

    snap을 갱신해 반환한다(discovered_delivery·delivery_context·patch_ids).
    """
    # SRJY(M5283M00-SRJY) — 제출 시점 PR 자동 연결: 보고에 delivery가 없는 review
    # 카드 중 카드 ID(브랜치/제목)로 조인되는 open 비드래프트 PR이 정확히 1개면
    # 그 PR을 산출물로 연결한다. 2개 이상이면 모호 — 기존 흐름(unbound-pr 노트) 유지.
    joiners = {}
    for p in snap.get("prs") or []:
        if p.get("isDraft"):
            continue
        iid = pr_card_id(p)
        if iid:
            joiners.setdefault(iid, []).append(p)
    snap["discovered_delivery"] = {}
    for i in snap.get("issues") or []:
        if i.get("state") != "review" or (i.get("completion_report") or {}).get("delivery"):
            continue
        cand = joiners.get(i["id"]) or []
        if len(cand) == 1 and cand[0].get("head_sha"):
            p = cand[0]
            snap["discovered_delivery"][i["id"]] = {
                "mode": "pr", "provider": "github", "repo": p["repo"],
                "number": p["number"], "head_sha": p["head_sha"], "base": p.get("base") or "",
            }
    snap["delivery_context"] = {
        i["id"]: inspect_delivery(_req(i), gh_json)
        for i in snap.get("issues") or []
        if i.get("state") == "review" and _req(i)["mode"] == "pr"
    }
    for iid, spec in snap["discovered_delivery"].items():
        snap["delivery_context"][iid] = inspect_delivery(spec, gh_json)
    # SRJY — patch-id 선계산: head 불일치가 의심되는 (repo, base, sha)만.
    snap["patch_ids"] = {}
    _pid_specs = [(i["id"], _req(i), snap["delivery_context"].get(i["id"]))
                  for i in snap.get("issues") or [] if i.get("state") == "review"]
    for iid, spec, obs in _pid_specs:
        if not spec or spec.get("mode") != "pr" or not obs:
            continue
        if spec.get("head_sha") and obs.get("head_sha") and \
                spec["head_sha"] != obs["head_sha"] and spec.get("base") == obs.get("base"):
            snap["patch_ids"].update(patch_ids_for(
                spec["repo"], spec["base"], (spec["head_sha"], obs["head_sha"])))
    return snap


def run_once(url):
    """1회 스캔→판정→집행 — 서버 내장 스레드 전용(U6로 standalone 퇴역)."""
    snap = snapshot(url)
    repos = list(_repos_default())
    for r in collect_repos(snap.get("issues") or []):
        repos.append(r)
    snap["prs"] = collect_prs(repos)
    _promote_pr_observation(snap)
    # 게이트 unset 여부와 무관 — in_progress 자동화(JXYS)도 코멘트 dedup이 필요.
    hydrate_reviews(url, snap)
    from .providers import observe_linked
    snap["pr_context"] = observe_linked(snap.get("issues") or [], snap["prs"], gh_json)
    from .providers import inspect_delivery
    from delivery import requirement

    def _req(i):
        return requirement(i, any(pr_card_id(p) == i["id"] for p in snap.get("prs") or []))

    _prepare_delivery_context(snap, _req, gh_json)

    snap["cycle_id"] = uuid.uuid4().hex  # §8.1 판단 로그 사이클 식별자(리뷰 R4)
    acts = decide(snap)
    judge.log_decide_actions(snap, acts)  # 7AMG — action마다 1행(shadow, 동작 불변)
    for a in acts:
        print(time.strftime("%F %T"), a, flush=True)
        execute(url, a)
    # 내장화 관측점: 서버 로그에서 사이클 생존을 확인 가능하게(빈 라운드도 1행)
    skips = snap.get("probe_skips") or {}
    print(time.strftime("%F %T"),
          f"[probe] cycle: issues={len(snap['issues'])} prs={len(snap['prs'])} actions={len(acts)}"
          + (f" skips={json.dumps(skips, ensure_ascii=False, sort_keys=True)}" if skips else ""),
          flush=True)
    return acts


def _acquire_probe_lock(db_path):
    """내장 probe 단일 구동 가드(BTRS) — DB 파일 옆 잠금파일 non-blocking flock.

    uvicorn --workers>1이면 워커당 probe가 중복 구동된다. 같은 머신·같은 DB라
    파일 잠금이면 충분한 리더 선출: 선점 실패 시 None — 호출부는 파킹 후 재시도
    (리더 프로세스가 죽으면 OS가 잠금을 즉시 해제해 자연 인수계).
    """
    import fcntl
    if not db_path:
        return None
    lock_path = db_path + ".probe.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except OSError:
        # 선점 실패 — fd를 닫지 않으면 5~30초마다 재시도하는 대기 워커가 누수한다
        os.close(fd)
        return None


def loop(url=None, interval=30, stop=None, db_path=None):
    """주기 실행 루프. stop 이벤트(선택)로 즉시 종료. 서버 장애 시 지수 백오프(≤300s).

    BTRS(하드닝): ①url=None이면 매 사이클 TT_URL을 재평가한다 — 내장 스레드가
    부팅 시 고정한 URL이라 멀티워커·URL 변경마다 재시작이 필요했던 것 제거.
    ②db_path가 주어지면 파일 잠금 단일 구동 가드 — 멀티워커에서 워커당 중복
    구동 방지(선점 실패 시 파킹, 리더 종료 시 인수계)."""
    lock_fd = None
    if db_path:
        lock_fd = _acquire_probe_lock(db_path)
        if lock_fd is None:
            print(time.strftime("%F %T"), "probe: 다른 워커가 구동 중 — 파킹(인수계 대기)",
                  flush=True)
            while _sleep(min(max(interval, 5), 30), stop):
                lock_fd = _acquire_probe_lock(db_path)
                if lock_fd is not None:
                    break  # 리더 종료 — 인수계
            else:
                return  # stop 요청
    try:
        fails = 0
        while not (stop is not None and stop.is_set()):
            cur = url or os.environ.get("TT_URL", "http://127.0.0.1:7800")
            try:
                run_once(cur)
                fails = 0
            except Exception as e:  # 서버 장애 — 백오프. 쓰기가 없으면 lease는 TTL로 자연 해금.
                fails += 1
                print(time.strftime("%F %T"), "error:", e, flush=True)
                if not _sleep(min(interval * 2 ** min(fails, 4), 300), stop):
                    break
                continue
            if not _sleep(interval, stop):
                break
    finally:
        # 정상 stop·예외·이미 설정된 stop 등 모든 종료에서 해제 — 같은 프로세스의
        # 후속 앱 생성이 파킹하지 않도록(리뷰 P2). os.close가 커널 flock을 푼다.
        if lock_fd is not None:
            os.close(lock_fd)


