#!/usr/bin/env python3
"""tt probe — TT 자율 스케줄러. decide()는 판정 순수 함수, 실행기는 public API만 소비.

30KP 설계 docs/dispatchd.md. 서버 프로세스 내장 스레드(TT_PROBE_INTERVAL, app.py)로만
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

from service import REVIEW_CONTRACT
from config import PROBE_ACTIVE_RUN_STATES  # W0DF — 활성 run 상태 단일 정의
import policy

def _prio(i):
    p = i.get("priority")
    return (p is None, p if p is not None else 999, i["id"])


def _auto_todo(issues, exclude=()):
    return [i for i in issues if i["state"] == "todo"
            and "auto" in (i.get("labels") or []) and i["id"] not in exclude]


DAEMON_AUTHOR = "tt-embedding-daemon"
_AUTO_CLASSIFIED_RE = re.compile(r"^\[auto-classified\]\s+label=(\S+)", re.MULTILINE)
AC_PRIO_WEIGHT = 0.5  # score→우선순위 보정 상한(±0.5) — 사람이 지정한 priority를 넘지 않게


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
    atts = [float(x.get("execution_attempt") or 0) for x in priors]
    avg_att = sum(atts) / n
    score = done_r - nh_r - min(0.5, max(0.0, avg_att - 1) * 0.1)
    return {"n": n, "done_r": done_r, "nh_r": nh_r, "avg_att": avg_att, "score": score}


def _budget_blocked(i):
    # TT 개선#3b: 예산 판정은 policy 단일 모듈로 — reason code도 policy 것을 씀
    return policy.budget_reason(i)


def ci_passed(pr):
    """CI green(순수): check-runs 1+ 전부 SUCCESS.
    (9dbba0c 실사례 — gh run list의 headSha 반영이 체크 완료보다 늦어 사냥 실패;
    gh pr checks는 HEAD check-runs 기준이라 이 병목이 없다 — run list는 병목이며 중복.)"""
    checks = pr.get("checks") or []
    return bool(checks) and all(c.get("state") == "SUCCESS" for c in checks)


CARD_IN_ANY = re.compile(r"\bM[A-Z0-9]{6,9}-[A-Z0-9]{4}\b")


def card_from_branch(branch):
    # tt/ 접두어에 한정하지 않고 브랜치 전체에서 카드 ID 스캔 — 재작업 브랜치
    # (hermes/…, fix/…) 흡수. 매칭은 8-4 ULID 실루엣(무작위 오탐 사실상 없음).
    m = CARD_IN_ANY.search(branch or "")
    return m.group(0) if m else None


def pr_card_id(pr):
    """PR→카드 조인 키. 브랜치(tt/<ID>-) 우선, 없으면 제목의 카드 ID(재작업으로
    브랜치명이 규약 밖이 되는 경우 흡수 — 사용자 지시 2026-09-30)."""
    iid = card_from_branch(pr.get("branch", ""))
    if iid:
        return iid
    m = CARD_IN_ANY.search(pr.get("title", "") or "")
    return m.group(0) if m else None


def collect_repos(issues):
    """PR 수집 repo 풀 — 알려진 repo(REPO_CARDS+TT_REPO_SCAN_EXTRA)는 항시, 카드 repo 표기는 동적.
    review 카드 포함(GBE5 사고) + repo 미표기 카드도 known repo에서 브랜치/제목 ID로 발견된다(사용자 지시).
    review 카드 코멘트의 PR URL도 편입(HG5S) — 스캔은 무해하고 실제 카드 연결은 pr_card_id 조인이 검증."""
    out = sorted(set(REPO_CARDS.values()))
    for extra in os.environ.get("TT_REPO_SCAN_EXTRA", "").split(","):
        if extra.strip():
            out.append(extra.strip())
    for i in issues or []:
        # in_progress 포함(JXYS R3) — 자동화 진입부 자체가 PR 수집을 전제로 한다.
        if i["state"] in ("auto", "todo", "blocked", "review", "in_progress") or (i.get("labels") and "auto" in i["labels"]):
            r = card_repo(i)
            if r:
                out.append(r)
        if i["state"] == "review" and i.get("comments"):
            out.extend(repo for repo, _ in _comment_pr_repos(i))
    return out


def _stale_act(i, att, now):
    """'PR 없음' review 카드 스탈 공지 액션 — 연령 기반(attempt당 마커 1회). 기본 24h.
    (U4 퇴역 전 needs-merge 노트가 attempt당 1회 dedup이라 사이클 카운트는 불가 → 연령 판정)"""
    try:
        stale_h = max(0, float(os.getenv("TT_PROBE_STALE_HOURS", "24")))
    except ValueError:
        stale_h = 24.0
    if not stale_h:
        return None
    smarker = f"[stale-notify a{att}]"
    if _probe_marker(i, smarker):
        return None
    age = _age_min(now, i.get("updated_at") or "")
    if age is None or age < stale_h * 60:
        return None
    return {"agent": "probe", "issue": i["id"], "action": "stale-notify",
            "marker": smarker,
            "reason": f"PR 없음 {int(age // 60)}시간 지속 — 사람 판단 대기 공지"}


PR_SHA = re.compile(r"PR#(\d+)@([0-9a-fA-F]{8})")


def review_verdict(comments, reviewer, pr_no, sha8):
    """현재 PR head(pr_no@sha8)에 유효한 리뷰어 판정. 없거나 stale이면 None.

    리뷰어 코멘트만 인정(author 일치), 'PR#<n>@<sha8>' 불일치는 stale. 최신 판정 우선.
    """
    if isinstance(reviewer, str):
        reviewer = [reviewer]
    verdict = None
    for c in comments or []:
        if c.get("author") not in reviewer:
            continue
        body = c.get("body") or ""
        m = PR_SHA.search(body)
        if not m or int(m.group(1)) != pr_no or m.group(2).lower() != (sha8 or "").lower():
            continue
        v = first_line_verdict(body)
        if v:
            verdict = v
    return verdict


# --- 카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C, docs/review-gate.md 룰셋) ---

# 자기 리뷰 방지용 고정 가족 어휘 — TT_REVIEW_MAP 확장과 무관하게 이 어휘로만 제외한다
# (매핑 키 codex-labs->codex로 가족 경유 자기 리뷰를 만들 수 없다)
# cursor: cursor-review(읽기전용 리뷰 프로필)는 가족 동반 — 구현 cursor의 자기 리뷰 방지(XV84)
# 3QMV — 가족 어휘·해석 규칙은 server/identity.py 단일 소스로 이동. 아래
# 임포트가 기존 소비처(core 내부·테스트·지표 스크립트)의 이름을 그대로 유지한다.
from identity import (FAMILY_ALIASES, REVIEW_FAMILIES, agent_family,  # noqa: F401
                      effective_host_family_map, parse_host_family_map)
from identity import alias_resolve as _identity_alias_resolve
from identity import resolve as _identity_resolve
# 발령자 결정(2026-10-10) 기본 카운터파트 표 — TT_REVIEW_MAP env로 override·확장
DEFAULT_REVIEW_MAP = {"codex": "claude", "claude": "codex",
                      "hermes": "codex", "opencode": "codex"}
# 리뷰어 미가용 폴백 순서 — 발령자 결정 갱신(2026-10-14, XV84): agy→cursor-review→
# codex→claude. cursor-review는 cursor의 읽기전용 리뷰 프로필. 런타임 순서 조정은
# 설정(settings/review-routing, 웹 편집)이 이 기본값을 덮는다 — 재배포 불필요.
# 1순위는 카운터파트 표(DEFAULT_REVIEW_MAP)가 정하고, 이 체인은 그 뒤를 잇는다.
REVIEW_FALLBACK = ("agy", "cursor-review", "codex", "claude")
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
NON_EXECUTORS = ("kanban-adapter", "codex-read-only", "tt-reviewer")
# CI 봇은 구현 에이전트가 아니다 — 미표기 취급(기본 라우팅)
CI_BOTS = ("doc-check", "e2e")


def parse_review_map(raw):
    """TT_REVIEW_MAP='codex->claude,claude->codex' → dict. 빈 칸·불량 항목은 무시."""
    out = {}
    for part in (raw or "").split(","):
        if "->" not in part:
            continue
        k, _, v = part.partition("->")
        k, v = k.strip().lower(), v.strip()
        if k and v:
            out[k] = v
    return out


def effective_review_map():
    """기본 카운터파트 표 + env override — decide가 사이클당 1회 산출."""
    return {**DEFAULT_REVIEW_MAP, **parse_review_map(os.getenv("TT_REVIEW_MAP", ""))}


def _receivable(a):
    """등록 에이전트가 dispatch 전달 가능한 상태인지(전달 사다리·재위임용).

    hook/watcher/relay는 base_url(관찰·전달 기준 주소)가 필요하고, poll(M3B5JHGP)은
    runner_name 선언만으로 러너 pending inbox를 통해 수령한다 — 포트 없는
    CLI 에이전트(codex·claude류)의 배정 배제를 막는다. receive_mode 미선언
    hook 에이전트는 구 등록분 호환으로 base_url만으로 전달 가능(선언 강제는 배정 단계).
    """
    if not a.get("enabled"):
        return False
    if (a.get("receive_mode") or "").strip() == "poll":
        return bool((a.get("runner_name") or "").strip())
    return bool(a.get("base_url"))


def _assignable(a):
    """자동 배정 후보 — RQRT 수신 계약 선언까지 요구(미선언 claim 409 루프 예방)."""
    if not (a.get("receive_mode") or "").strip():
        return False
    return _receivable(a)


# agent_family는 identity.py로 이동 — 위 임포트로 재수출된다.


def _routing_family(name, rmap):
    """라우팅 lookup용 가족 — 매핑 키(최장 prefix) 우선, 다음 고정 가족."""
    base = (name or "").strip().lower().split("@", 1)[0]
    for fam in sorted(set(rmap), key=len, reverse=True):
        if base.startswith(fam):
            return fam
    return agent_family(name)


def _impl_family(assignee):
    """구현 에이전트의 가족 — CI 봇은 구현 에이전트가 아니라 None."""
    a = (assignee or "").strip()
    return None if a.lower() in CI_BOTS else agent_family(a)


def route_reviewer(assignee, rmap):
    """구현 에이전트 → 카운터파트 리뷰어(순수 — 가용성 무관).

    미표기(CI 봇 포함)·미매칭 가족 → codex 기본. codex* 가족 구현은 매핑값(claude)으로 —
    자기 리뷰 방지 원칙이 codex 기본보다 우선(발령자 결정 2026-10-10).
    구현 에이전트 소스는 카드 assignee — 서버 own_work 판정(claim-review 409)과 동일.
    """
    a = (assignee or "").strip()
    if a.lower() in CI_BOTS:
        a = ""
    fam = _routing_family(a, rmap)
    if fam and fam in rmap:
        return rmap[fam]
    return "codex"


def _alias_resolve(cand, agents):
    """별칭 사전 해석 — identity.alias_resolve 위임(3QMV 단일 소스)."""
    return _identity_alias_resolve(cand, agents)


def _resolve_agent(cand, agents):
    """후보 → 실제 dispatch 대상 등록 에이전트 이름. 정확 일치 → 별칭 → 가족 매칭.

    등록 에이전트 스냅샷(decide의 enabled+base_url 필터 통과 집합)에 없으면
    None — 오프라인 판정의 기준. 규칙 본체는 identity.resolve(3QMV).
    """
    return _identity_resolve(cand, agents)


def _resolve_executor(cand, agents):
    """review-fix 발령 대상 해석 — 정확 일치 우선, 가족 매칭은 실행 가능 에이전트 한정.

    PXCJ 리뷰 블로커: _resolve_agent의 가족 매칭은 등록 순서 첫 에이전트를 돌려
    hermes@m2max·hermes가 kanban-adapter(실행 불가)로 해석될 수 있다 — hermes-m2max가
    등록돼 있어도 수정 정체(#343 동형). 정확 일치가 NON_EXECUTOR면 그대로 돌려 실행
    불가 통보 분기로 보내고, 가족 매칭은 NON_EXECUTORS를 제외한 후보만 본다. 리뷰어
    라우팅(pick_reviewer)은 _resolve_agent를 그대로 쓴다 — 리뷰 대행은 의도된 동작.
    """
    for a in agents or []:
        if a.get("name") == cand:
            return cand
    hit = _alias_resolve(cand, agents)
    if hit:
        return hit
    fam = agent_family(cand)
    for a in agents or []:
        if a.get("name") in NON_EXECUTORS:
            continue
        if fam and agent_family(a.get("name")) == fam:
            return a.get("name")
    return None


# --- 369Y(M5283JJH-369Y): 같은 지적 반복은 구조로 막기 — /correct 루프 차단 ---

# 반려 사유 버킷(키워드 분류 — 대소문자 무시, 순서 민감: migration이 contract의
# '스키마' 키워드보다 우선). 미분류는 general — 구조화 대상에서 뺀다.
def _rejection_verdicts(cur):
    """카드의 request-changes 판정 코멘트(시간순). TT·레거시 PR 경로 공용 형식.

    71NK RC — 기계 author(runner 에코 등)의 첫 줄은 반려 집계에서 제외
    (verdicts.is_machine_author — /rejections/summary와 동일 규칙)."""
    out = []
    for c in cur.get("comments") or []:
        if first_line_verdict(c.get("body")) == "request-changes" \
                and not _is_machine_author(c.get("author")):
            out.append(c)
    return out


def _correct_loop_sections(cur):
    """369Y — 발령문에 붙일 섹션 목록. 반복 유형 구조화 + 3회 반려 premise 강제.

    같은 유형 2회 이상 → 구조화 체크 승격 지시. 카드 누적 반려 3회 이상 →
    premise 재검토("같은 premise의 수정이 두 번 실패하면 premise부터 시험").
    """
    verdicts = _rejection_verdicts(cur)
    if not verdicts:
        return []
    from collections import Counter
    buckets = Counter(_classify_rejection(c.get("body") or "") for c in verdicts)
    sections = []
    repeated = sorted(((b, n) for b, n in buckets.items() if n >= 2 and b != "general"),
                      key=lambda kv: (-kv[1], kv[0]))
    if repeated:
        lines = [f"- {b} 지적 {n}회 반복 — 수정 후 이 유형은 lint/테스트/계약 체크로 "
                 "전환해 같은 지적이 재발하지 않게 한다" for b, n in repeated]
        sections.append("--- 구조화 체크 승격 (369Y) ---\n" + "\n".join(lines))
    if len(verdicts) >= 3:
        sections.append(
            "--- premise 재검토 (369Y) ---\n"
            f"반려 {len(verdicts)}회 — 같은 premise의 수정이 두 번 실패했다면 premise부터 시험하라. "
            "acceptance 정의와 접근 방식 자체를 재검토하고, 유지 근거를 카드 노트로 남긴다.")
    return sections


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


def pick_reviewer(assignee, agents, busy, rmap, fallback=None):
    """라우팅 + 미가용 폴백(순수). 전부 미가용이면 None — 레거시 폴백으로.

    agents: 등록+활성 에이전트 스냅샷(오프라인 판정), busy: 활성 lease 보유
    에이전트 이름 집합. 리스 보유는 에이전트 단위로만 판정한다(F3 — 가족 한 명의
    lease가 가족 전체 후보를 막지 않는다, 검증기준 ①). 가족 매칭은 자기 리뷰
    가드에만 쓴다. 작성자 본인 신원과 구현 가족(고정 어휘·어댑터 별칭·host 별칭
    포함)은 매핑 확장과 무관하게 끝까지 제외된다 — 자기 리뷰 방지 유지.
    """
    a = (assignee or "").strip()
    impl_fam = _impl_family(a)
    primary = route_reviewer(assignee, rmap)
    # 런타임 설정 우선(XV84) — fallback 인자가 snap["review_routing"]에서 온다
    chain = [primary] + [c for c in (fallback or REVIEW_FALLBACK) if c != primary]
    for cand in chain:
        cand_fam = agent_family(cand)
        if cand == a or (impl_fam and cand_fam == impl_fam):
            continue  # 자기 리뷰 방지 — 본인 신원·구현 가족 후보 제외
        name = _resolve_agent(cand, agents)
        if name is None:
            continue  # 오프라인
        if name in (busy or []):
            continue  # 리스 보유 — 에이전트 단위 판정(F3)
        return name
    return None


def _probe_marker(i, marker):
    return any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe")


# 12MZ reviewer_session_failed — 판정 없는 리뷰 정체(W S3C 실측: codex 세션이
# dispatch 후 스트림 오류로 사망 → 같은 마커 dedup에 걸려 영구 정체).
REVIEW_RETRY_MAX = 3
# BG97 relay 관측자 — 이 author들의 코멘트는 '활동'으로 세지 않는다(관측·시스템 기록).
# runner 계열(runner@host)도 활동에서 제외한다 — 세션 수명 관측(queued/done/failed/
# STALL-killed)일 뿐 워커 진행 증거가 아니므로, 실패 기록 하나가 무반응 양도를
# 영구 차단해선 안 된다(리뷰 R5).
_RELAY_OBSERVERS = {"probe", "tt-embedding-daemon", "tt-server", "board"}  # (사유,pr,sha) 카운터 한도 — 도달 시 needs-human 에스컬레이션
_RUNNER_FAIL_RE = re.compile(r"runner:\S+ dispatch#(\d+) failed exit=[1-9]")
_DISPATCH_ID_RE = re.compile(r"\(dispatch#(\d+)\)")


def _stall_window_min():
    """정체 판정 관망 창(분) — 최신 발령 마커가 이 창 내면 새 세션이 살아있을 수
    있어 관망한다. 0이면 창 없음(즉시 판정). runner 실패 코멘트는 창과 무관하게 즉시."""
    try:
        return int(os.environ.get("TT_REVIEW_STALL_MIN", "60") or "0")
    except ValueError:
        return 60


def _comment_ts(c):
    """댓글 시각 필드 — wire 형식은 ts(db.comments_of), 테스트/구형 호환 created_at."""
    return c.get("ts") or c.get("created_at") or ""


def _evidence_unresolved(i):
    """현재 회차의 done 거부가 아직 해소되지 않았는지(리뷰 R5).

    증거 경계(evidence_after_comment_id — 회차 리셋 시 갱신) 이후의 시스템
    코멘트만 대상으로 한다. 경계 이전 거부는 이전 회차의 것 — 새 회차에서
    재사용하지 않는다. 거부(DONE_GATE_MSG)와 접수('보고 접수') 중 최신
    (ts, id 순)이 거부면 미해결. 접수된 성공 보고 뒤에 남은 과거 거부는
    재요청하지 않는다. 접수 코멘트 이전 카드 호환으로 reported도 해소로 본다."""
    b = i.get("evidence_after_comment_id") or 0
    cands = []
    for c in (i.get("comments") or []):
        if c.get("author") != "tt-server" or (c.get("id") or 0) <= b:
            continue
        body = c.get("body") or ""
        if body.startswith("done 증거 없음"):
            cands.append(("reject", c.get("ts") or c.get("created_at") or "", c.get("id") or 0))
        elif body.startswith("보고 접수"):
            cands.append(("accept", c.get("ts") or c.get("created_at") or "", c.get("id") or 0))
    if i.get("verification_status") == "reported":
        return False
    if not cands:
        return False
    return max(cands, key=lambda t: (t[1], t[2]))[0] == "reject"


# ⑥ 반복 사건 억제(§6, TT M4K87YR0-HN7E) — 같은 (사유코드, 범위) 반복 N회 도달 시
# 사람 통보 1회 후 해당 키의 자동 대응 스킵. 카운터는 probe 마커 코멘트에서 집계
# (서버 스키마 변경 없음). 해제는 사람 전용 — [unblocked (코드, 범위)]를 probe/
# dispatchd가 아닌 author가 남겼을 때만 카운터가 그 시점 이후로 재집계된다.
# H 처리(budget_exhausted 등)와 reviewer_session_failed(자체 N=3)는 억제하지 않는다.

_ESC_FMT = "[escalated ({}, {})]"
_UNBLOCK_FMT = "[unblocked ({}, {})]"


def _suppressed(i, code, scope):
    """(억제 여부, 카운터 재집계 시작 comment id) — 해제 마커가 에스컬레이션보다
    최신이면 해제된 것으로 보고 그 이후만 카운트한다."""
    esc = _ESC_FMT.format(code, scope)
    if not any(esc in (c.get("body") or "") for c in (i.get("comments") or [])):
        return False, 0
    since = 0
    latest = None
    for c in (i.get("comments") or []):
        if c.get("author") in ("probe", "dispatchd"):
            continue  # 사람 전용 해제 — probe 자동 초기화 금지(§6)
        if _UNBLOCK_FMT.format(code, scope) in (c.get("body") or ""):
            k = (c.get("ts") or c.get("created_at") or "", c.get("id") or 0)
            if latest is None or k > latest[0]:
                latest = (k, c.get("id") or 0)
    if latest is None:
        return True, 0
    return False, latest[1]


def _ev_open(i):
    """⑤ 미해결 조건 — tt-server done 거부 미해소 또는 무보고 병합 미접수(JXYS R1).
    판정(decide)과 집행(execute 재확인)이 같은 조건을 써야 독려가 실제 저장된다."""
    cs = i.get("comments") or []
    if any("done 증거 없음" in (c.get("body") or "") for c in cs
           if c.get("author") == "tt-server") and _evidence_unresolved(i):
        return True
    if i.get("state") != "review":
        return False  # 재작업 회귀 카드 — 옛 병합 기록으로 재독려 금지
    mc = max((c for c in cs if c.get("author") == "probe"
              and "merged — 완료 보고 없음" in (c.get("body") or "")),
             key=lambda c: (_comment_ts(c), c.get("id") or 0), default=None)
    if not mc:
        return False
    mt, mid = _comment_ts(mc), mc.get("id") or 0
    return not any(c.get("author") == "tt-server"
                   and "보고 접수" in (c.get("body") or "")
                   and (_comment_ts(c), c.get("id") or 0) > (mt, mid) for c in cs)


def _repeat_count(i, regex, since_id=0):
    """probe 마커 코멘트 중 regex 일치 수(해제 시점 이후만) — (코드, 범위) 카운터."""
    return sum(1 for c in (i.get("comments") or [])
               if c.get("author") == "probe" and (c.get("id") or 0) > since_id
               and re.search(regex, c.get("body") or ""))


def _post_json(url, payload, timeout=5):
    """90Y9 — 외부 hook으로 JSON POST(fire-and-forget). 실패는 호출부에서 생략."""
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


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


def _gh_pr_head(repo, pr_no):
    """실행 직전 실제 PR head(sha8) — 카드 CAS는 GitHub push를 못 본다(리뷰 R3).

    조회 실패는 None — 호출부는 폐기한다(오작동보다 침묵)."""
    try:
        # -q 필터는 원문 sha를 출력하므로 json.loads가 깨진다 — JSON 객체로 읽는다.
        h = gh_json("pr", "view", str(pr_no), "--repo",
                    repo or REPO_CARDS.get("think-tank", ""), "--json", "headRefOid")
        return (str((h or {}).get("headRefOid") or "").strip()[:8]) or None
    except Exception:
        return None


def _comment_after(x, dt, mid=0):
    """(ts,id) 순서 판별 — DB 시각이 초 단위라 동초 접수도 id로 구분한다(리뷰 R2)."""
    t = _iso(_comment_ts(x)) if dt else None
    if t is None:
        return False
    if t > dt:
        return True
    if t == dt:
        return (x.get("id") or 0) > (mid or 0)
    return False


def _iso(ts):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


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


def _escalate(actions, i, code, scope, count):
    """사람 통보 1회(기존 level4-notify/needs-human 경로) — 마커 dedup."""
    esc = _ESC_FMT.format(code, scope)
    if any(esc in (c.get("body") or "") for c in (i.get("comments") or [])):
        return
    actions.append({"agent": "dispatchd", "issue": i["id"], "action": "needs-human",
                    "note": f"{esc} 반복 사건 {count}회 도달 — 사람 확인 전까지 "
                            "이 키의 자동 대응 중단(§6). 해제: [unblocked (코드, 범위)] 코멘트"})


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


def _age_min(now, updated):
    """iso(±TZ) 두 시각 차(분). 해석 불가 시 None(코멘트 보류)."""
    from datetime import datetime
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    try:
        return (datetime.strptime(now, fmt) - datetime.strptime(updated, fmt)).total_seconds() / 60
    except ValueError:
        return None


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
        for p in snap.get("prs") or []:
            _iid = pr_card_id(p)
            if _iid:
                self.graph.link_pr(_iid, p.get("repo") or "", p.get("number") or 0,
                                   sha=(p.get("head_sha") or "") or None)
        rmap = effective_review_map()  # 카운터파트 라우팅(M4JTJ970-WS3C) — env override 포함
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
            tt_actions = tt_lifecycle.plan(sys.modules[__name__], i, agents,
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


def api(url, path, method="GET", body=None):
    req = urllib.request.Request(
        url.rstrip("/") + path, method=method,
        headers={"content-type": "application/json", "x-agent": os.getenv("TT_AGENT", "dispatchd")})
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=15) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else None


def get_version(url, iid):
    return api(url, f"/issues/{iid}").get("version")


def _repos_default():
    return [r.strip() for r in os.environ.get("TT_REPO_SLUG", "plainOldCode/think-tank").split(",") if r.strip()]


REPO_CARDS = {
    "think-tank": "plainOldCode/think-tank",
    "armour-wiki": "plainOldCode/armour-service-ops",
    "armour": "plainOldCode/armour-service-ops",  # GBE5: hermes 표기 라벨 실측(2026-09-30)
}
REPO = os.environ.get("TT_REPO_SLUG", "plainOldCode/think-tank")

CARD_REPO = re.compile(r"repo:\s*([\w.-]+/[\w.-]+)")
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def _comment_pr_repos(issue):
    """카드 코멘트의 github PR URL → [(owner/repo, pr_no)] — 등장 순 중복 제거.

    NB5T: 정본 파서는 ontology.build — decide는 그래프 질의를 쓴다(단계 치환)."""
    from ontology.build import parse_comment_pr_repos
    return parse_comment_pr_repos(issue.get("comments") or [])


def card_repo(issue):
    m = CARD_REPO.search((issue.get("body") or "") + " " + (issue.get("title") or ""))
    if m:
        return m.group(1)
    for lab in issue.get("labels") or []:
        if lab in REPO_CARDS:
            return REPO_CARDS[lab]
    return None


GH = (shutil.which("gh")
      or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh"))


def _gh(args):
    r = subprocess.run([GH, *[str(a) for a in args]], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(map(str, args))}: {r.stderr[:300]}")
    return r.stdout


def gh_json(*args):
    return json.loads(_gh(list(args)) or "null")


def gh_exec(*args):
    return _gh(list(args))


# SRJY — patch-id 프로세스 캐시: (repo, base, sha) → patch-id. 프로브 주기마다
# 같은 head를 재계산하지 않게 한다(서버 재시작 시 재계산 — 무해).
_PATCH_ID_CACHE = {}


def patch_id_for(repo, base, sha):
    """리뷰/병합 판정에 쓰는 git patch-id. 관측 실패는 None — 보류가 기본이다."""
    from .providers import patch_id_of
    if not repo or not base or not sha:
        return None
    key = (repo, base, sha)
    if key not in _PATCH_ID_CACHE:
        _PATCH_ID_CACHE[key] = patch_id_of(repo, base, sha, gh_exec)
    return _PATCH_ID_CACHE[key]


def patch_ids_for(repo, base, shas):
    """readiness에 넘길 {(repo, sha): patch-id} 맵 — 실패 head는 생략(보류 유지)."""
    out = {}
    for sha in shas or []:
        if not sha:
            continue
        pid = patch_id_for(repo, base, sha)
        if pid:
            out[(repo, sha)] = pid
    return out


REVIEW_STATUS_CTX = "tt/review-approved"


def collect_prs(repos=None):
    """Optional provider observations; TT_PR_PROVIDER=none disables GitHub I/O.

    T3YT(M58SPT6D-T3YT): repo 단위 관측 결과(ok|failed)를 함수 속성에 실어
    되돌린다 — 반환은 기존 PR 리스트 계약 유지(monkeypatch 호환). run_once가
    snap["pr_observed"]로 승격한다."""
    from .providers import collect
    prs, observed = collect(list(repos) if repos else _repos_default(), gh_json)
    collect_prs.observed = observed
    return prs


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


def _probe_flag(url, act, msg):
    """probe 판단 불가(경합·실패) 관측 기록. 같은 사유 2회 지속 시 review로 반납(사람 신호)."""
    iid = act["issue"]
    print(time.strftime("%F %T"), msg, flush=True)
    try:
        cur = api(url, f"/issues/{iid}")
        prior = [c for c in (cur.get("comments") or []) if "probe merge skip" in (c.get("body") or "")]
        api(url, f"/issues/{iid}/comments", "POST", {"author": "probe", "body": msg})
        if len(prior) >= 1:
            cur2 = api(url, f"/issues/{iid}")
            api(url, f"/issues/{iid}", "PATCH", {"version": cur2["version"], "state": "review"})
            api(url, f"/issues/{iid}/comments", "POST",
                {"author": "probe", "body": f"PR#{act['pr']}: 장애 2회 지속 — review로 반납(사람 판단)"})
    except Exception:
        pass


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


def _exec_release_reviewer(url, act):
    cur = api(url, f"/issues/{act['issue']}")
    if cur.get("reviewer") == act.get("reviewer"):
        # 이 판정이 현재 head에 여전히 유효한지 — decide와 동일 필터(review_verdict)로 재확인.
        # R7: author·PR 무관한 임의 판정 비교는 유효 반납까지 막는다 — 리뷰어 판정만 인정.
        if review_verdict(cur.get("comments") or [], act.get("reviewer"),
                          act.get("pr"), (act.get("head_sha") or "")[:8]) is None:
            return
        # decide 이후 카드가 변했다면(재claim·코멘트 등) 이번 라운드 폐기 — 다음 라운드 재판정.
        # R5: 같은 판정 구간에서 리뷰어가 재claim한 새 lease를 오래된 액션이 지우지 않게.
        if act.get("expected_version") is not None and cur.get("version") != act["expected_version"]:
            return
        # reviewer만 지우고, lease는 리뷰어 본인 것이 확실할 때만 —
        # 재작업자가 새 lease를 잡은 뒤 오래된 반납 액션이 도착해도 보존(R5).
        fields = {"reviewer": "", "expected_version": cur["version"]}
        if cur.get("lease_by") == act.get("reviewer"):
            fields.update({"lease_by": "", "lease_expires": None})
        api(url, f"/issues/{act['issue']}", "PATCH", fields)
    return

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

def _exec_stale_notify(url, act):
    # 'PR 없음' 지속 review 카드 — 메시지 보드 공지(멘션은 assignee 등록 에이전트에만 기록됨) + 카드 마커.
    cur = api(url, f"/issues/{act['issue']}")
    marker = act.get("marker") or ""
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])):
        return
    who = cur.get("assignee") or ""
    api(url, "/messages", "POST",
        {"author": "probe",
         "body": (f"@{who} " if who else "") + f"[stale] 카드 {act['issue']} — {act['reason']}"})
    api(url, f"/issues/{act['issue']}/comments", "POST",
        {"author": "probe", "body": f"{marker} {act['reason']}"})
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
    "release-reviewer": _exec_release_reviewer,
    "archive-sweep": _exec_archive_sweep,
    "ci-fix": _exec_ci_fix,
    "submit-remind": _exec_submit_remind,
    "submission-adopt": _exec_submission_adopt,
    "review-note": _exec_review_note,
    "stale-notify": _exec_stale_notify,
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


