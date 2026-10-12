#!/usr/bin/env python3
"""tt-dispatchd — TT 자율 스케줄러. decide()는 판정 순수 함수, 실행기는 public API만 소비.

30KP 설계 docs/dispatchd.md. 서브프로세스·서버 수정 없음. 상태 비저장: 근거는 카드·이력.
launchd com.tt.dispatchd (mini), TT_AUTO_DISPATCH=1 이어야 동작.
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

import failure_rules
import judge

from service import REVIEW_CONTRACT
import policy

PULL_HINT = "tt claim ID {agent} 후 dispatch — pull(풀) 사용 금지(30KP/W1DP)"


def _prio(i):
    p = i.get("priority")
    return (p is None, p if p is not None else 999, i["id"])


def _auto_todo(issues, exclude=()):
    return [i for i in issues if i["state"] == "todo"
            and "auto" in (i.get("labels") or []) and i["id"] not in exclude]


def _budget_blocked(i):
    # TT 개선#3b: 예산 판정은 policy 단일 모듈로 — reason code도 policy 것을 씀
    return policy.budget_reason(i)


def ci_passed(pr):
    """CI green(순수): check-runs 1+ 전부 SUCCESS.
    (9dbba0c 실사례 — gh run list의 headSha 반영이 체크 완료보다 늦어 사냥 실패;
    gh pr checks는 HEAD check-runs 기준이라 이 병목이 없다 — run list는 병목이며 중복.)"""
    checks = pr.get("checks") or []
    return bool(checks) and all(c.get("state") == "SUCCESS" for c in checks)


CARD_IN_BRANCH = re.compile(r"tt/(M[A-Z0-9]{6,9}-[A-Z0-9]{4})")


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


def _review_grace_min():
    """review 전이 후 'PR 없음' 판정 유예(분). TT_REVIEW_GRACE_MIN(기본 20)."""
    try:
        return max(0, int(os.getenv("TT_REVIEW_GRACE_MIN", "20")))
    except ValueError:
        return 20


def _stale_act(i, att, now):
    """'PR 없음' review 카드 스탈 공지 액션 — 연령 기반(attempt당 마커 1회). 기본 24h.
    (needs-merge 노트가 attempt당 1회 dedup이라 사이클 카운트는 불가 → 연령으로 판정)"""
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


REVIEW_LINE = re.compile(r"^review:\s*(approve|request-changes)\b", re.I)
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
        v = REVIEW_LINE.search(body.strip())
        if v:
            verdict = v.group(1).lower()
    return verdict


def _requested_reviewers(i, pr_no, sha8):
    """이 PR/head에 대해 probe가 리뷰를 요청한(수령시킨) 에이전트 집합.

    [review-req #pr/sha8] 마커 코멘트의 '→ <agent>'를 파싱 — 교차 수령자의
    판정이 다음 사이클에서 인정되게 한다(TT 개선#3c R1).
    """
    out = set()
    for c in i.get("comments") or []:
        if c.get("author") != "probe":
            continue
        body = c.get("body") or ""
        if f"#{pr_no}/" not in body or (sha8 or "") not in body:
            continue
        m = re.search(r"→\s*(\S+)", body)
        if m:
            out.add(m.group(1).rstrip("."))
    return out


# --- 카운터파트 리뷰어 라우팅 (M4JTJ970-WS3C, docs/review-gate.md 룰셋) ---

# 자기 리뷰 방지용 고정 가족 어휘 — TT_REVIEW_MAP 확장과 무관하게 이 어휘로만 제외한다
# (매핑 키 codex-labs->codex로 가족 경유 자기 리뷰를 만들 수 없다)
REVIEW_FAMILIES = ("codex", "claude", "hermes", "opencode")
# 어댑터 별칭 — kanban-adapter는 hermes의 리뷰 대행자다(발령자 결정 2026-10-10)
FAMILY_ALIASES = {"kanban-adapter": "hermes"}
# host 별칭 — assignee가 user@host 형식이고 base로 가족을 못 찾을 때 host로 추정한다(F2).
# 실제 보드 assignee 2위 형식(skshim@tp-13)은 등록 에이전트명과 달라 가족 정규화에
# 걸리지 않는다. 카드 표 "opencode (tp-13 포함)" — env TT_HOST_FAMILY_MAP으로 확장.
HOST_FAMILY_ALIASES = {"tp-13": "opencode"}
# 발령자 결정(2026-10-10) 기본 카운터파트 표 — TT_REVIEW_MAP env로 override·확장
DEFAULT_REVIEW_MAP = {"codex": "claude", "claude": "codex",
                      "hermes": "codex", "opencode": "codex"}
# 리뷰어 미가용 폴백 순서 — codex는 최후순위, hermes 자리는 kanban 어댑터가 대행
REVIEW_FALLBACK = ("claude", "kanban-adapter", "codex")
# 진행 중 run 상태(runner run_state.ACTIVE 동기) — 이 상태의 dispatch가 있으면
# 같은 카드에 리뷰/수정 dispatch를 추가로 스폰하지 않는다(M4N469TR-JWW0).
_ACTIVE_RUN_STATES = frozenset({"queued", "running", "stalled", "finalizing", "held"})
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


def parse_host_family_map(raw):
    """TT_HOST_FAMILY_MAP='tp-13->opencode,foo->claude' → dict. 빈 칸·불량 항목은 무시."""
    out = {}
    for part in (raw or "").split(","):
        if "->" not in part:
            continue
        k, _, v = part.partition("->")
        k, v = k.strip().lower(), v.strip().lower()
        if k and v:
            out[k] = v
    return out


def effective_host_family_map():
    """기본 host 별칭 + env 확장 — agent_family가 host로 가족을 추정할 때 사용(F2)."""
    return {**HOST_FAMILY_ALIASES, **parse_host_family_map(os.getenv("TT_HOST_FAMILY_MAP", ""))}


def agent_family(name):
    """에이전트 가족 정규화(자기 리뷰 방지 기준) — host 접미사 흡수 + 고정 어휘 prefix.

    어댑터 별칭 우선(kanban-adapter→hermes 대행). base에서 가족을 못 찾고 user@host
    형식이면 host 별칭으로 가족을 추정한다(F2 — skshim@tp-13 → opencode). 미매칭
    (None)은 가족 미지 에이전트(qw-flash, agy 등) — 본인 신원 제외만 적용되고
    가족 제외는 없다.
    """
    s = (name or "").strip().lower()
    base, _, host = s.partition("@")
    if base in FAMILY_ALIASES:
        return FAMILY_ALIASES[base]
    for fam in sorted(REVIEW_FAMILIES, key=len, reverse=True):
        if base.startswith(fam):
            return fam
    if host:
        hmap = effective_host_family_map()
        if host in hmap:
            return hmap[host]
    return None


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


def _resolve_agent(cand, agents):
    """후보 → 실제 dispatch 대상 등록 에이전트 이름. 정확 일치 우선, 다음 가족 매칭.

    등록 에이전트 스냅샷(decide의 enabled+base_url 필터 통과 집합)에 없으면
    None — 오프라인 판정의 기준.
    """
    fam = agent_family(cand)
    for a in agents or []:
        if a.get("name") == cand:
            return cand
    for a in agents or []:
        if fam and agent_family(a.get("name")) == fam:
            return a.get("name")
    return None


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
    fam = agent_family(cand)
    for a in agents or []:
        if a.get("name") in NON_EXECUTORS:
            continue
        if fam and agent_family(a.get("name")) == fam:
            return a.get("name")
    return None


def pick_reviewer(assignee, agents, busy, rmap):
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
    for cand in [primary] + [c for c in REVIEW_FALLBACK if c != primary]:
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
# dispatch 후 스트림 오류로 사망 → review-req 마커 dedup에 걸려 영구 정체).
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


def _review_retry_round(i, pr_no, sha8):
    """이 head(pr@sha8)의 재발령 회차 — probe 코멘트의 회차 마커에서 산출.
    head가 바뀌면 마커의 sha8이 달라져 자동 리셋(0)된다."""
    r = 0
    for c in (i.get("comments") or []):
        if c.get("author") != "probe":
            continue
        m = re.search(r"\[review-retry a(\d+) #%s/%s\]" % (pr_no, sha8),
                      c.get("body") or "")
        if m:
            r = max(r, int(m.group(1)))
    return r


def _reviewer_session_stalled(i, pr_no, sha8, now):
    """발령 이력 head의 세션 사망 정체 판정(기계적) — 12MZ.

    즉시 트리거: runner 신원(author runner* — 실측 runner@machine 형식, 본문은
    runner:machine 접두) 코멘트의 `dispatch#N failed exit!=0` — 단 ①최신 발령
    마커 이후 접수 ②마커가 기록한 dispatch id와 일치(구 회차·작업 dispatch의
    늦은 실패·동초 접수가 새 세션 관망을 깨지 않게 — 리뷰 R3 잔여).
    이외: 리뷰어 점유 흔적(release되지 않은 reviewer) + 최신 발령 마커
    (review-req/review-retry)가 TT_REVIEW_STALL_MIN 창 경과.
    """
    comments = i.get("comments") or []
    latest, latest_id, latest_did = "", 0, ""
    for c in comments:
        if c.get("author") != "probe":
            continue
        body = c.get("body") or ""
        if f"#{pr_no}/{sha8}" not in body:
            continue
        if ("[review-req #" in body or "[review-retry a" in body) and _comment_ts(c):
            # 동초 마커는 댓글 id로 최신 선택(리뷰 4차 R3) — 첫 행 고정이 구 실패를 오인
            cid = c.get("id") or 0
            ts = _comment_ts(c)
            if ts > latest or (ts == latest and cid > latest_id):
                latest, latest_id = ts, cid
                m = _DISPATCH_ID_RE.search(body)
                latest_did = m.group(1) if m else ""
    if not latest or not now:
        return False
    for c in comments:
        if not str(c.get("author") or "").startswith("runner"):
            continue
        m = _RUNNER_FAIL_RE.search(c.get("body") or "")
        # 발령 마커의 dispatch id와 일치할 때만 즉시 트리거 — id 미기록(구형)·불일치
        # (구 회차·작업 dispatch) 실패는 창 경과로만 판정(보수적, 리뷰 R3 잔여)
        if not m or not latest_did or m.group(1) != latest_did:
            continue
        if _comment_ts(c) >= latest:
            return True
    if not i.get("reviewer"):
        return False
    win = _stall_window_min()
    if win <= 0:
        return True
    try:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(latest)
        return age >= timedelta(minutes=win)
    except ValueError:
        return False  # 시각 해석 불가 — 보수적으로 관망


def _draft_stalled(i, pr_no, now):
    """draft 병합 반복 사람 통보 판정(규칙표 §4 draft_merge_loop) — draft-flagged
    마커가 TT_DRAFT_STALL_H 시간 경과했으면 True. 0이면 통보 없음."""
    h = failure_rules.draft_stall_hours()
    if h <= 0:
        return False
    marker = f"[draft-flagged #{pr_no}]"
    for c in (i.get("comments") or []):
        if c.get("author") == "probe" and marker in (c.get("body") or ""):
            age = _age_min(now, c.get("ts") or c.get("created_at") or "")
            return age is not None and age >= h * 60
    return False


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


def _fix_stall_fields(body):
    """데몬 [fix-stall] 첫 행의 key=value 필드 — BG97 소비 입력."""
    m = re.match(r"^\[fix-stall\]\s+(.*)$", (body or "").split("\n", 1)[0])
    if not m:
        return None
    out = {}
    for tok in m.group(1).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out or None


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


def _maybe_review_retry(actions, i, p, pr_repo, now):
    """12MZ: 세션 사망 정체 감지 → 회차 마커로 재발령 1회, 상한 도달 시 에스컬레이션.

    기존 review-req 마커 dedup은 유지하되, 그 뒤에 무음으로 갇히지 않게 하는 층.
    재발령은 회차 마커([review-retry a<n>])로 dedup — probe dedup과 충돌 없음.
    정체 판정이 에스컬레이션보다 먼저다(리뷰 R7) — 마지막 회차도 관망 창을 온전히 받는다.
    """
    pr_no, sha8 = p["number"], (p.get("head_sha") or "")[:8]
    if not _reviewer_session_stalled(i, pr_no, sha8, now):
        return  # 점유 흔적·실패 기록 없음, 또는 관망 창 내 — 기존 dedup 그대로
    rnd = _review_retry_round(i, pr_no, sha8)
    if rnd >= REVIEW_RETRY_MAX:
        nh = f"[needs-human-review #{pr_no}/{sha8}]"
        # executor가 dispatchd로 기록하므로 author 무관 dedup(리뷰 R6)
        if not any(nh in (c.get("body") or "") for c in (i.get("comments") or [])):
            actions.append({"agent": "probe", "issue": i["id"], "action": "needs-human",
                            "note": f"{nh} 리뷰 {REVIEW_RETRY_MAX}회차 재발령 후에도 판정 없음 "
                                    f"(PR#{pr_no} @{sha8}, reviewer_session_failed) — 사람 판단 대기"})
        return
    retry_marker = f"[review-retry a{rnd + 1} #{pr_no}/{sha8}]"
    if _probe_marker(i, retry_marker):
        return  # 경합 방어 — 같은 회차 재발령은 1회만
    actions.append({"agent": "probe", "issue": i["id"], "action": "review-retry",
                    "pr": pr_no, "repo": pr_repo, "branch": p.get("branch", ""),
                    "head_sha": p.get("head_sha", ""), "marker": retry_marker,
                    "round": rnd + 1,
                    **({"reviewer": i["reviewer"]} if i.get("reviewer") else {}),
                    "reason": f"PR#{pr_no} 리뷰 세션 사망 정체(reviewer_session_failed) — "
                              f"{rnd + 1}회차 재발령, 새 세션에서 시작(이전 세션 재사용 금지)"})


def _claim_refused_note(url, act, cur, code):
    """리뷰 수령 거부 기록 — review-req 마커를 남기지 않아 다음 사이클 재판정·재시도(R3).

    같은 head당 1회만 공지(review-claim-fail 마커 dedup) — 반복 실패 사이클의 소음 방지.
    """
    sha8 = (act.get("head_sha") or "")[:8]
    marker = f"[review-claim-fail #{act.get('pr')}/{sha8}]"
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return
    try:
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} 리뷰어 수령 거부(HTTP {code}) — "
                                        "다음 사이클 재시도"})
    except Exception:
        pass


def _guard_skipped_note(url, act, cur):
    """자기 리뷰 가드로 후보가 전부 제외된 기록 — 실제 요청은 없었음(F4).

    HTTP 409로 기록하면 요청조차 안 한 사실을 오독하므로 사유 코드를 분리한다.
    review-req 마커를 남기지 않아 다음 사이클 재판정(R3) — head당 1회 dedup.
    """
    sha8 = (act.get("head_sha") or "")[:8]
    marker = f"[review-guard-skip #{act.get('pr')}/{sha8}]"
    if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
           if c.get("author") == "probe"):
        return
    try:
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} 리뷰 후보 전원 자기 리뷰 가드로 제외"
                                        "(요청 없음) — 다음 사이클 재판정"})
    except Exception:
        pass


def _reviewer_lease_release(url, issue, reviewer):
    """claim 성공 후 dispatch 실패 시 리뷰어 lease 반납(F4).

    lease 잔존은 만료까지 타 리뷰어 claim-review를 409로 막아 재발령을 방해한다.
    점유가 방금 claim한 리뷰어 본인일 때만 지운다 — 남이 재claim한 새 lease는
    보존한다(release-reviewer와 동일 가드, R5).
    """
    try:
        cur = api(url, f"/issues/{issue}") or {}
        if (cur.get("reviewer") or "") != (reviewer or ""):
            return
        fields: dict = {"reviewer": ""}
        if cur.get("lease_by") == reviewer:
            fields.update({"lease_by": "", "lease_expires": None})
        api(url, f"/issues/{issue}", "PATCH", fields)
    except Exception:
        pass


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


_C1_SEEN = {}  # C1 카드별 최신 판정 입력 해시(프로세스 내 dedup)


def decide(snap):
    if not snap.get("auto"):
        return []
    issues = snap.get("issues") or []
    # RQRT: 수신 계약 미선언 등록 에이전트는 배정 후보 제외 — claim 409 반복으로
    # 사이클 전체가 중단되는 것을 배정 단계에서 원천 차단. 비등록 세션은
    # 스냅샷 agents에 없으므로 영향 없다(암묵 relay).
    agents = [a for a in (snap.get("agents") or [])
              if a.get("enabled") and a.get("base_url")
              and (a.get("receive_mode") or "").strip()]
    now = snap.get("now") or ""
    actions = []
    claimed = set()
    # TT 개선#3c R3: 유휴 판정은 작업 lease뿐 아니라 리뷰 점유 lease도 포함 +
    # 이번 사이클에 이미 배정된(작업·리뷰) 에이전트는 재배정하지 않는다.
    now0 = now
    busy_agents = {i.get("lease_by") for i in issues
                   if i.get("lease_by") and i.get("lease_expires")
                   and (not now0 or i["lease_expires"] > now0)}
    busy_agents.discard(None)
    # TT 개선#3b: 배정 못 한 카드의 reason code — run_once가 로그로 남긴다.
    # 에이전트 유무와 무관하게 전체 카드를 정책으로 스캔(상태 조합 매트릭스의 로그 실측).
    snap["probe_skips"] = {}
    skip = snap["probe_skips"]
    for i in issues:
        ok, code = policy.eligible(i, now=now, auto=True)
        if not ok and code:
            skip[i["id"]] = code

    def idle(name):
        return not any(i["state"] == "in_progress" and i.get("assignee") == name
                       and (i.get("lease_expires") or "9999") > now for i in issues)

    def pick(cands):
        """가장 낮은 우선순위의 배정 가능 후보(동률 id순). 없으면 None.
        TT 개선#3b: 기각 시 policy reason code를 probe_skips에 남긴다."""
        for i in sorted(cands, key=_prio):
            ok, code = policy.eligible(i, now=now, auto=True)
            if ok:
                return i
            if code and i["id"] not in skip:
                skip[i["id"]] = code
        return None

    # ① release-ready 재개(수동 재개 규약의 기계 대행 — 서버 release_ready 판정 근거)
    resumable = [i for i in issues if i["state"] == "blocked"
                 and i.get("waiting_for") == "dependency" and i.get("release_ready")]
    actor = next((a["name"] for a in agents
                  if a.get("name") not in NON_EXECUTORS and idle(a["name"])), None)
    if resumable and actor:
        t = resumable[0]
        actions.append({"agent": actor, "issue": t["id"], "action": "resume",
                        "reason": "release-ready"})
        return actions

    # ② agent별 작업 대상: continuation(자식→형제) → auto 풀 — 실행 불가 에이전트 제외(PXCJ)
    for a in agents:
        name = a["name"]
        if name in NON_EXECUTORS or not idle(name):
            continue
        dones = [i for i in issues if i["state"] == "done" and i.get("assignee") == name]
        target = reason = None
        if dones:
            last = max(dones, key=lambda i: i.get("updated_at") or "")
            kids = _auto_todo(issues, claimed | {last["id"]})
            kids = [k for k in kids if k.get("parent_id") == last["id"]]
            target, reason = pick(kids), "continuation-child"
            if target is None:
                sibs = [s for s in _auto_todo(issues, claimed | {last["id"]})
                        if last.get("parent_id") and s.get("parent_id") == last["parent_id"]]
                target, reason = pick(sibs), "continuation-sibling"
        if target is None:
            pool = [p for p in _auto_todo(issues, claimed)
                    if not (dones and p["id"] == dones[0]["id"])]
            target, reason = pick(pool), "pool"
        if target is None:
            continue
        claimed.add(target["id"])
        claimed.add(name)  # TT 개선#3c R3: 배정받은 에이전트는 이번 사이클 리뷰 금지
        actions.append({"agent": name, "issue": target["id"], "action": "work",
                        "reason": reason})

    # ⓪(사실상 우선) probe: CI green PR + 카드 연결/수령/계약 검증 → gh pr merge
    # 리뷰 게이트(N0AN, docs/review-gate.md): TT_REVIEW_AGENT 설정 시 승인 코멘트 필요.
    by_id = {i["id"]: i for i in issues}
    reviewer = os.environ.get("TT_REVIEW_AGENT", "").strip()
    rmap = effective_review_map()  # 카운터파트 라우팅(M4JTJ970-WS3C) — env override 포함
    gated = set()  # 리뷰 진행 중 카드 — ⓪b needs-merge 소음 제외
    for p in snap.get("prs") or []:
        iid = pr_card_id(p)
        i = by_id.get(iid) if iid else None
        if not iid or i is None or i["state"] in ("done", "cancelled"):
            continue
        # 저위험 클래스(M4DEK6WC-MQYS): docs/i18n 라벨 PR — 계약·회차 없어도 제출
        # (review) 상태면 독립 리뷰+CI green 후 무인 병합 대상. server/runner 경로는
        # execute에서 최종 차단(사람 승인 필요).
        pr_labels = {x.strip().lower() for x in (p.get("labels") or [])}
        card_labels = i.get("labels")
        if isinstance(card_labels, str):
            try:
                card_labels = json.loads(card_labels)
            except (ValueError, TypeError):
                card_labels = []
        low_risk = bool({x.strip().lower() for x in (card_labels or [])}
                        & set(policy.LOW_RISK_LABELS)) or bool(pr_labels & set(policy.LOW_RISK_LABELS))
        if (not i.get("work_contract") or (i.get("execution_attempt") or 0) < 1) and not low_risk:
            continue
        if low_risk and not i.get("work_contract") and i["state"] != "review":
            continue  # 저위험 무계약 병합은 '제출된' 카드만 — todo/backlog는 대상 아님
        if p.get("isDraft"):
            continue  # draft 병합 시도 금지(HHP5) — ⓪b에서 ready 요청 1회
        want = card_repo(i)
        pr_repo = p.get("repo") or REPO_CARDS["think-tank"]
        if want is None:
            # 카드 repo 표기/라벨 없음 — ID가 찍힌 PR의 repo를 추론 채택(발견 자체가 근거).
            # repo 명시 카드는 불일치 계속 차단(오repo 사고 방지) — GBE5 교훈 유지.
            want = pr_repo
        if want != pr_repo:
            continue
        # ⓪c JXYS(M4MGV3BY) — in_progress 자동화: 리뷰 전용 자동화의 사각지대.
        # in_progress+PR에서도 CI 실패는 위임, green 무보고는 독려→무반응 승격.
        # 전이는 probe가, 완료(done)는 보고서 의존 유지 — probe가 보고를 위조하지 않는다.
        if i["state"] == "in_progress" and not i.get("busy_agents"):
            att_ip = i.get("execution_attempt") or 0
            sha8_ip = (p.get("head_sha") or "")[:8]
            checks_ip = p.get("checks") or []
            states_ip = [c.get("state") for c in checks_ip]
            fails_ip = [x for x in states_ip if x == "FAILURE"]
            # 1) CI 실패 → 기존 ci-fix 흐름(위임 대상 동일) — 실패 PR은 미완성이어서 안전
            if fails_ip and os.environ.get("TT_CI_FIX_AGENT", "kanban-adapter"):
                marker_ip = f"[ci-fix-ip a{att_ip} #{p['number']}/{sha8_ip}]"
                sup_ip, since_ip = _suppressed(i, "ci_failed_ip",
                                               f"#{p['number']}/{sha8_ip}")
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
                                        "cycle_id": snap.get("cycle_id"),
                                        "checks_failed": [c.get("name") or "?" for c in checks_ip
                                                          if c.get("state") == "FAILURE"]})
                continue
            # 2) green + 무보고 → 독려 1회 → 무반응 승격. head가 바뀌면 새 sha의
            #    독려 마커가 새로 필요 — 타이머가 자연 리셋(점진 push 납치 방지).
            if ci_passed(p):
                smarker = f"[submit-remind a{att_ip} #{p['number']}/{sha8_ip}]"
                amarker = f"[submission-adopt a{att_ip} #{p['number']}/{sha8_ip}]"
                if _probe_marker(i, amarker):
                    continue  # 이미 승격됨 — 승격 후에도 보고 제출은 가능
                if not _probe_marker(i, smarker):
                    actions.append({"agent": "probe", "issue": iid,
                                    "action": "submit-remind", "marker": smarker,
                                    "att": att_ip, "pr": p["number"], "repo": pr_repo,
                                    "head_sha": p.get("head_sha", ""),
                                    "reason": f"PR#{p['number']} CI green — 완료 보고 제출"
                                              "(tt done)이 review 진입"})
                    continue
                sc = next((c for c in (i.get("comments") or [])
                           if c.get("author") == "probe"
                           and smarker in (c.get("body") or "")), None)
                sdt = _iso(_comment_ts(sc)) if sc else None
                if not sdt or _lease_held(i, now):
                    continue  # 유효 임대 = 활동 중 — 무반응이 아님
                if (datetime.fromisoformat(now) - sdt).total_seconds() <= \
                        _relay_timer_s(i.get("assignee") or "", agents):
                    continue  # 독려 타이머 미경과(hook 1h / relay 6h)
                if any((x.get("author") or "") not in _RELAY_OBSERVERS
                       and not str(x.get("author") or "").startswith("runner")
                       and _comment_after(x, sdt, sc.get("id") or 0)
                       for x in (i.get("comments") or [])):
                    continue  # 응답·활동 중 — 승격 금지
                actions.append({"agent": "probe", "issue": iid,
                                "action": "submission-adopt", "marker": amarker,
                                "att": att_ip, "pr": p["number"], "repo": pr_repo,
                                "head_sha": p.get("head_sha", ""),
                                "remind_ts": _comment_ts(sc),
                                "remind_id": sc.get("id") or 0,
                                "expected_version": i["version"],
                                "reason": f"PR#{p['number']} green 무보고 — 기계적 승격"
                                          "(review). 완료 보고는 승격 후에도 유효"})
                continue
        # pr_no_checks(규칙표 §4): 확인된 checks 0건([]) PR만 판정 대상 — 유효 리뷰
        # 판정이 있으면 그 판정을 따르고(approve→병합 후보, request-changes→review-fix),
        # 무판정이면 리뷰 요청. 기존은 무응답(needs-merge 오표기)이었다.
        # checks=None(조회 실패, collect_prs)은 관측 불가 — 0건 취급 금지(리뷰 R1:
        # CI 조회 오류의 병합 우회 방지). 게이트 off 레거시 모드는 기존 무응답 유지.
        no_checks = p.get("checks") == []
        if ci_passed(p) or (no_checks and (reviewer or low_risk)):
            if reviewer or low_risk:
                if i["state"] != "review":
                    # 게이트 on: 제출 전 카드는 리뷰·병합 대상 아님 — 움직이는 대상 리뷰 낭비 방지.
                    # agent가 보고 제출(state=review)하면 다음 라운드에서 판정.
                    continue
                gated.add(iid)
                sha8 = (p.get("head_sha") or "")[:8]
                # TT 개선#3c R1: 교차 리뷰어의 판정도 인정 — env 리뷰어 + 카드 점유
                # 리뷰어 + 마커 코멘트로 수령시킨 에이전트
                verdict = review_verdict(i.get("comments"),
                                         ({reviewer, i.get("reviewer")}
                                          | _requested_reviewers(i, p["number"], sha8)) - {None},
                                         p["number"], sha8)
                if verdict in ("approve", "request-changes") and i.get("reviewer"):
                    # 리뷰어 claim 해제 — 판정 기록됐으면 점유 반납(review-fix 시 작업자 assignee 보존)
                    actions.append({"agent": "probe", "issue": iid, "action": "release-reviewer",
                                    "reviewer": i["reviewer"], "pr": p["number"],
                                    "head_sha": p.get("head_sha", ""),
                                    "expected_version": i["version"],
                                    "reason": "판정 기록 — 리뷰어 점유 반납"})
                if verdict == "request-changes":
                    marker = f"[review-fix #{p['number']}/{sha8}]"
                    if not _probe_marker(i, marker) and not i.get("busy_agents"):
                        # 진행 중 dispatch가 있으면 수정 스폰도 보류(JWW0) — 마커를
                        # 남기지 않아 다음 사이클 재판정.
                        actions.append({"agent": "probe", "issue": iid, "action": "review-fix",
                                        "pr": p["number"], "repo": pr_repo,
                                        "branch": p.get("branch", ""),
                                        "head_sha": p.get("head_sha", ""), "marker": marker,
                                        "reason": f"PR#{p['number']} 리뷰 request-changes — "
                                                  "review 반납+수정 위임"})
                    continue
                if verdict is None:  # 미리뷰 또는 stale — (재)요청
                    marker = f"[review-req #{p['number']}/{sha8}]"
                    if _probe_marker(i, marker):
                        # 12MZ reviewer_session_failed — 발령 이력 head에 판정 없음:
                        # 세션 사망 정체 감지·재발령(W S3C 영구 정체 사고의 미커버 구간).
                        _maybe_review_retry(actions, i, p, pr_repo, now)
                    else:
                        # 진행 중 dispatch가 있으면 새 리뷰 스폰을 보류한다(JWW0) —
                        # 수동 재리뷰+자동 리뷰+자동 fix가 같은 카드에 동시 도는
                        # 사고(PR#87: 325/326/327/328)의 재발 방지. 마커 없이
                        # 건너뛰어 다음 사이클 재판정(진행 종료 후 정상 스폰).
                        if i.get("busy_agents"):
                            continue
                        # 카운터파트 라우팅 — 구현 에이전트(assignee) 기준 리뷰어 결정.
                        # 수령 방식(review-claim 선점)은 #3c 그대로, 대상 선정만 양 경로가
                        # 같은 카운터파트/가족 정책을 따른다(R1).
                        routed = pick_reviewer(i.get("assignee"), agents,
                                               busy_agents | claimed, rmap)
                        # F1: 교차 수령 후보는 pick_reviewer 결과(라우팅 1순위 + 3a
                        # 폴백 체인)로 한정한다 — 전원 미가용이면 일반 유휴 스캔으로
                        # 확장하지 않는다(발령자가 지정하지 않은 에이전트의 수령 방지).
                        # pick_reviewer가 자기 리뷰 가드·오프라인·리스 보유를 이미
                        # 반영하므로, 남은 카드 수준 자격(review_eligible)만 재확인한다.
                        cross = None
                        if routed and routed not in busy_agents and routed not in claimed \
                                and routed != i.get("assignee") \
                                and policy.review_eligible(i, now=now, agent=routed,
                                                           low_risk=low_risk)[0]:
                            cross = routed
                        if cross:
                            actions.append({"agent": cross, "issue": iid, "action": "review-claim",
                                            "pr": p["number"], "repo": pr_repo,
                                            "branch": p.get("branch", ""),
                                            "head_sha": p.get("head_sha", ""), "marker": marker,
                                            "reason": f"PR#{p['number']} 리뷰 대기 — 유휴 에이전트 자동 수령(교차리뷰)"})
                            claimed.add(cross)  # R3: 한 사이클에 리뷰 1건
                        else:
                            # 활성 점유(타인 리뷰 lease)면 요청하지 않는다 — 마커도 없어
                            # lease 만료 후 다음 사이클 재판정(R3: 수령 거부 뒤 영구 차단 방지).
                            _ok, _why = policy.review_eligible(i, now=now, agent=routed or "~")
                            if _why != "review_occupied":
                                # 전원 미가용이면 reviewer 키 없이 액션만(실행측 레거시 폴백).
                                actions.append({"agent": "probe", "issue": iid,
                                                "action": "review-request",
                                                "pr": p["number"], "repo": pr_repo,
                                                "branch": p.get("branch", ""),
                                                "head_sha": p.get("head_sha", ""),
                                                "marker": marker,
                                                "reason": f"PR#{p['number']} CI green — 리뷰 요청 → "
                                                          f"{routed or reviewer or 'kanban-adapter'}",
                                                **({"reviewer": routed} if routed else {})})
                    continue
                # approve — 아래 merge로 통과
                gated.discard(iid)
            merge_reason = (f"리뷰 판정 대체(checks 0건) + 카드 계약/수령 검증 — gh pr merge ({pr_repo})"
                            if no_checks else
                            f"CI green + 카드 계약/수령 검증 — gh pr merge ({pr_repo})")
            actions.append({"agent": "probe", "issue": iid, "action": "merge", "pr": p["number"],
                            "head_sha": p.get("head_sha", ""),
                            "repo": pr_repo, "low_risk": bool(low_risk),
                            **({"no_ci": True} if no_checks else {}),
                            "reason": merge_reason})

    # ⓪b review 카드 중 병합 불가 후보 — 사람 판단 요청 코멘트 (M3R7M0ZR-YF99).
    # merge 후보(⓪)에 없는 review 카드만. 회차 마커 '[needs-merge a<n>'로 회차당 1회.
    # 리뷰 게이트로 보류 중인 카드(gated)도 제외 — review-request/review-fix가 담당.
    merged_targets = {a["issue"] for a in actions if a["action"] == "merge"} | gated
    for i in issues:
        if i["state"] != "review" or i["id"] in merged_targets:
            continue
        if not i.get("work_contract") or (i.get("execution_attempt") or 0) < 1:
            continue
        att = i.get("execution_attempt") or 0
        prs_for = [p for p in snap.get("prs") or [] if pr_card_id(p) == i["id"]]
        # 코멘트 PR URL 역기입(HG5S) — needs-merge 마커와 독립 판정(codex P1: 마커가
        # 채택·재시도를 차단해서는 안 된다). 본문 변경 없이 collect_repos가 풀에 편입.
        if not prs_for and not card_repo(i):
            for repo, prn in _comment_pr_repos(i):
                amarker = f"[pr-adopted {prn}/{repo}]"
                if _probe_marker(i, amarker):
                    continue  # 이미 채택 기록 — 다음 후보 검사(codex P2)
                actions.append({"agent": "probe", "issue": i["id"], "action": "pr-adopt",
                                "repo": repo, "pr": prn, "marker": amarker,
                                "reason": f"코멘트 PR URL 역기입 — {repo}#{prn} 채택"})
                break
        # draft 판정은 needs-merge dedup보다 먼저 — 구 needs-merge 마커가 draft의
        # ready 요청·48h 사람 통보를 가리지 않게(리뷰 R2).
        drafts = [p for p in prs_for if p.get("isDraft")]
        if drafts and len(drafts) == len(prs_for):
            # 반복 사건 억제(§6): (draft_merge_loop, #pr) — ready 요청이 N번 반복되면
            # 사람 통보 후 스킵. 억제 중이면 48h 통보도 포함해 모든 자동 대응 중단.
            dscope = f"#{drafts[0]['number']}"
            dsup, dsince = _suppressed(i, "draft_merge_loop", dscope)
            dn = failure_rules.suppress_n()
            if dsup:
                print(time.strftime("%F %T"),
                      f"억제 중 (draft_merge_loop, {dscope}) — draft 대응 스킵(§6)", flush=True)
                continue
            # 전부 draft — 병합·ci-fix 판정 불가. ready 요청 1회(마커 dedup) 후 무음.
            # draft_merge_loop(규칙표 §4): ready 요청 후에도 경과하면 사람 1회 통보(dedup) 후 중지.
            marker = f"[draft-flagged {dscope}]"
            if dn and _repeat_count(i, rf"\[draft-flagged {re.escape(dscope)}\]",
                                    dsince) >= dn:
                _escalate(actions, i, "draft_merge_loop", dscope, dn)
                continue
            if not any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
                       if c.get("author") == "probe"):
                actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                                "marker": marker,
                                "reason": f"draft PR#{drafts[0]['number']} — ready for review "
                                          "요청(준비되면 draft 해제)"})
                continue
            if _draft_stalled(i, drafts[0]["number"], snap.get("now") or ""):
                dh = f"[needs-human-draft #{drafts[0]['number']}]"
                if not any(dh in (c.get("body") or "") for c in (i.get("comments") or [])):
                    actions.append({"agent": "probe", "issue": i["id"], "action": "needs-human",
                                    "note": f"{dh} draft PR#{drafts[0]['number']} ready 전환 없이 "
                                            f"병합 판정 반복(TT_DRAFT_STALL_H 경과) — 사람 판단 대기"})
            continue
        marker = f"[needs-merge a{att}]"
        # 반복 사건 억제(§6): (no_pr, 카드) — 'PR 없음' 노트가 N회차면 사람 통보 후 스킵.
        nscope = i["id"]
        nsup, nsince = _suppressed(i, "no_pr", nscope)
        nn = failure_rules.suppress_n()
        if nsup:
            print(time.strftime("%F %T"),
                  f"억제 중 (no_pr, {nscope}) — needs-merge 노트 스킵(§6)", flush=True)
            continue
        if nn and _repeat_count(i, r"\[needs-merge a\d+\]", nsince) >= nn:
            _escalate(actions, i, "no_pr", nscope, nn)
            continue
        if any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe"):
            # 이미 needs-merge 노트 발행 — PR이 여전히 없을 때만 스탈 재평가 후 무음
            # (codex P2: PENDING/FAILURE/draft PR 관측 중엔 'PR 없음' 공지 금지).
            if not prs_for:
                sa = _stale_act(i, att, snap.get("now") or "")
                if sa:
                    actions.append(sa)
            continue
        if prs_for:
            states = [c.get("state") for p in prs_for for c in p.get("checks") or []]
            fails = [x for x in states if x == "FAILURE"]
            if states and all(x == "SUCCESS" for x in states):
                reason = "PR green — 병합 판정 제외됨(repo 불일치 등) — 확인 필요"
            elif fails and os.environ.get("TT_CI_FIX_AGENT", "kanban-adapter"):
                # CI 실패 = 수정 목적 위임(사람 needs-merge 대신 agy 경유). 사용자 지시 0930.
                p0 = next(p2 for p2 in prs_for
                          if any(c.get("state") == "FAILURE" for c in p2.get("checks") or []))
                att = i.get("execution_attempt") or 0
                sha8 = (p0.get("head_sha") or "")[:8]
                marker = f"[ci-fix a{att} #{p0['number']}/{sha8}]"
                # 반복 사건 억제(§6): (ci_failed, #pr/sha8) — 위임이 N번 반복되면 사람
                # 통보 후 스킵. same-head dedup보다 먼저(회차가 바뀌어 같은 head 반복).
                scope = f"#{p0['number']}/{sha8}"
                sup, since = _suppressed(i, "ci_failed", scope)
                if sup:
                    print(time.strftime("%F %T"),
                          f"억제 중 (ci_failed, {scope}) — ci-fix 스킵(§6)", flush=True)
                    continue
                n = failure_rules.suppress_n()
                if n and _repeat_count(i, rf"\[ci-fix a\d+ {re.escape(scope)}\]", since) >= n:
                    _escalate(actions, i, "ci_failed", scope, n)
                    continue
                if any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
                       if c.get("author") == "probe"):
                    continue  # same head 재위임 금지 — 새 커밋(head 변경) 시에만 재판정
                actions.append({"agent": "dispatchd", "issue": i["id"], "action": "ci-fix",
                                "pr": p0["number"], "repo": p0.get("repo", ""),
                                "cycle_id": snap.get("cycle_id"),
                                "branch": p0.get("branch", ""), "head_sha": p0.get("head_sha", ""),
                                "checks_failed": [c.get("name") or "?" for p3 in prs_for
                                                  for c in p3.get("checks") or []
                                                  if c.get("state") == "FAILURE"]})
                continue
            elif fails:
                reason = "CI 실패"
            elif not states:
                reason = "PR 있음 — checks 없음(CI 워크플로 부재?)"
            else:
                continue  # PENDING/진행 중 — 판정 보류, 다음 라운드 재확인
        else:
            age = _age_min(snap.get("now") or "", i.get("updated_at") or "")
            if age is None or age < _review_grace_min():
                continue  # PR 생성 유예 — 성급한 'PR 없음' 코멘트 금지
            # 스탈 공지: 'PR 없음' review 카드가 N시간 지속 — 사일런트 반복 방지.
            # (needs-merge 노트는 attempt당 1회 dedup이라 사이클 카운트 불가 → 연령 기반)
            sa = _stale_act(i, att, snap.get("now") or "")
            if sa:
                actions.append(sa)
            reason = "PR 없음"
        # C2(gate-failure-rules.md §5.2) — 라벨·repo 마커로 분류 불가(둘 다 없음/
        # 충돌)일 때만 판단기 rank 제안(shadow — 동작 불변, 로그만). 규칙 우선.
        _c2_labeled = bool({x.strip().lower() for x in (i.get("labels") or [])
                            if isinstance(x, str)} & set(policy.LOW_RISK_LABELS))
        _c2_repo = bool(card_repo(i))
        # 규칙으로 분류 가능한 것은 '정확히 하나'일 때 — 둘 다 없음/충돌(둘 다 존재)만
        # 판단기 rank 대상(§5.2, 리뷰 R6)
        if _c2_labeled == _c2_repo:
            judge.run_advisory("C2", {"card": i["id"], "input": {
                "labels": i.get("labels"), "repo_marker": bool(card_repo(i)),
                "title": i.get("title", "")}},
                f"review-note:{reason}", cycle_id=snap.get("cycle_id"))
        actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                        "reason": f"{reason} — 사람 판단 대기"})

    # ②b BG97(M4MGV3B7) — 재작업 전달 보장. 데몬 [fix-stall] 소비(원 워커 재발령
    # 1회 재시도)와 relay 마커 무반응 양도(todo 회귀). dedup은 (사유,pr,sha,attempt) —
    # docs/embedding-daemon-design.md §2 소비 계약. 소비자 재확인 의무(§2): 마커의
    # PR·head·attempt가 현재값과 일치할 때만 유효 — 과거 이벤트를 현재 회차로
    # 재태깅하지 않는다(리뷰 R1).
    for p in snap.get("prs") or []:
        iid = pr_card_id(p)
        i = by_id.get(iid) if iid else None
        if not iid or i is None or i["state"] in ("done", "cancelled"):
            continue
        pr_no, sha8 = p["number"], (p.get("head_sha") or "")[:8]
        pr_repo = p.get("repo") or REPO_CARDS.get("think-tank", "")
        comments = i.get("comments") or []
        att = i.get("execution_attempt") or 0
        rmarker = f"[rework-relay #{pr_no}/{sha8} a{att}]"
        # 1) [fix-stall] 소비 — 위임 후 미실행 정체. 같은 (pr,sha,attempt) 재시도 1회.
        #    유효 임대는 활동 중 — 재발령 금지(리뷰 R2: 임대 검사는 양쪽 액션 공통 자격).
        if i["state"] == "review" and not i.get("busy_agents") and \
                not _lease_held(i, now) and not _probe_marker(i, rmarker):
            stalls = [c for c in comments if c.get("author") == "tt-embedding-daemon"
                      and (c.get("body") or "").startswith("[fix-stall]")]
            for c in reversed(stalls):
                fs = _fix_stall_fields(c.get("body") or "")
                if not fs or fs.get("src_kind") != "review-fix":
                    continue
                if fs.get("src_pr") != str(pr_no) or fs.get("src_head") != sha8:
                    continue  # 과거 head의 정체 — 현재 head는 재판정됐으므로 무시
                if str(fs.get("attempt") or "") != str(att):
                    continue  # 과거 회차의 정체 — 현재 회차로 재태깅 금지(리뷰 R1)
                _sdt = _comment_ts(c)
                _sdtv = _iso(_sdt)
                _sid = c.get("id") or 0
                if _sdtv and any(
                        (x.get("author") or "") not in _RELAY_OBSERVERS
                        and not str(x.get("author") or "").startswith("runner")
                        and _comment_after(x, _sdtv, _sid)
                        for x in comments):
                    break  # stall 뒤 워커 응답·진행 — 이미 해소된 정체(리뷰 R2)
                # stall 이후 최신 판정이 approve면 재작업은 이미 통과 — 재발령 금지(리뷰 R2)
                _revs = ({reviewer, i.get("reviewer")}
                         | _requested_reviewers(i, pr_no, sha8)) - {None, ""}
                if review_verdict(comments, _revs, pr_no, sha8) == "approve":
                    break
                actions.append({"agent": "probe", "issue": iid, "action": "review-fix-relay",
                                "pr": pr_no, "repo": pr_repo,
                                "branch": p.get("branch", ""),
                                "head_sha": p.get("head_sha", ""), "att": att,
                                "stall_ts": _sdt, "stall_id": _sid,
                                "reason": f"PR#{pr_no} fix 위임 미실행 정체 — 원 워커 재발령"})
                break
            if any(a["issue"] == iid and a["action"] == "review-fix-relay" for a in actions):
                continue  # 이번 회차 재발령 중 — 양도 판정은 다음 사이클
        # 2) 무반응 양도 — 현재 (pr,head,attempt)의 relay 마커만 소비한다(리뷰 R1).
        #    과거 회차·head의 relay는 새 워커를 해치므로 회귀 입력에서 제외.
        relays = [c for c in comments if c.get("author") == "probe"
                  and "[rework-relay #" in (c.get("body") or "")]
        if not relays or i.get("busy_agents"):
            continue
        rc = None
        for c in reversed(relays):
            m = re.search(r"\[rework-relay #(\d+)/([0-9a-f]{8}) a(\d+)\]",
                          c.get("body") or "")
            if m and (int(m.group(1)), m.group(2).lower(), int(m.group(3))) == \
                    (pr_no, sha8, att):
                rc = c
                break
        if rc is None:
            continue  # 현재 작업의 relay 없음 — 과거 이벤트로 회귀 금지(리뷰 R1)
        # 재작업 가능한 state만 — blocked(사람 판단 대기)는 todo로 되돌리지 않는다(리뷰 R2)
        if i["state"] != "review" or i.get("waiting_for") == "human":
            continue
        rpr, rsha, ratt = pr_no, sha8, att
        if _probe_marker(i, f"[rework-fallback a{ratt}]"):
            continue
        # §6 공용 억제 계약 — 해제 마커 이후만 재집계, 사람 [unblocked]로 재개 가능(리뷰 R6)
        sup, since_id = _suppressed(i, "rework_relay", f"#{rpr}/{rsha}")
        if sup:
            continue
        n = failure_rules.suppress_n()
        if n and _repeat_count(i, r"\[rework-fallback a\d+\]", since_id) >= n:
            _escalate(actions, i, "rework_relay", f"#{rpr}/{rsha}", n)
            continue
        rts = _comment_ts(rc)
        if _lease_held(i, now):
            continue  # 유효 임대 = 활동 중(Q05B claude 케이스) — 무반응이 아님
        try:
            rdt = datetime.fromisoformat(rts)
        except ValueError:
            continue
        if (datetime.fromisoformat(now) - rdt).total_seconds() <= \
                _relay_timer_s(i.get("assignee") or "", agents):
            continue
        # head 불변 — relay 이후 같은 PR의 다른 head 위임·요청 마커가 있으면 회귀 금지
        if any(x.get("author") == "probe" and _comment_after(x, rdt, rc.get("id") or 0)
               and f"#{rpr}/" in (x.get("body") or "")
               and rsha not in (x.get("body") or "")
               and ("[review-fix #" in (x.get("body") or "")
                    or "[review-req #" in (x.get("body") or "")
                    or "[ci-fix a" in (x.get("body") or ""))
               for x in comments):
            continue
        # 무활동 — relay 이후 관측자(probe·데몬·서버·보드·runner 계열) 외 코멘트만
        # 활동으로 센다. runner 기록은 세션 수명 관측일 뿐 워커 진행 증거가 아니다(리뷰 R5).
        if any((x.get("author") or "") not in _RELAY_OBSERVERS
               and not str(x.get("author") or "").startswith("runner")
               and _comment_after(x, rdt, rc.get("id") or 0)
               for x in comments):
            continue
        actions.append({"agent": "probe", "issue": iid, "action": "rework-fallback",
                        "pr": rpr, "repo": pr_repo, "head_sha": p.get("head_sha", ""),
                        "relay_ts": rts, "relay_id": rc.get("id") or 0,
                        "att": ratt, "marker": f"[rework-fallback a{ratt}]",
                        "reason": f"PR#{rpr} relay 무반응 — assignee 해제+todo 회귀"})

    # ③ 예산 소진 카드 — dispatch 대신 needs-human (auto 풀 전수)
    for i in _auto_todo(issues, claimed):
        why = _budget_blocked(i)
        if why:
            actions.append({"agent": actor or "dispatchd", "issue": i["id"],
                            "action": "needs-human", "reason": why})
    # ④ 자동 아카이브 스윕(M3ZRGTSF-N95X) — TT_ARCHIVE_AFTER_DAYS>0일 때 사이클당 1회
    try:
        _ad = int(os.environ.get("TT_ARCHIVE_AFTER_DAYS", "0") or 0)
    except ValueError:
        _ad = 0
    if _ad > 0:
        actions.append({"agent": "probe", "action": "archive-sweep", "days": _ad})
    # ⑤ evidence_rejected(규칙표 §4) — done 거부 시스템 기록(tt-server DONE_GATE_MSG
    # 코멘트)이 있는 비완료 카드: 담당 assignee 재제출 요청 노트(회차당 1회 dedup).
    # 서버는 거부 시 state=review로 돌리므로 review 포함 — 실제 발생 상태(리뷰 R3).
    for i in issues:
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
                            "marker": emarker, "attempt": att, "cycle_id": snap.get("cycle_id"),
                            "reason": f"done 거부(증거 없음/실패) 또는 무보고 병합 — "
                                      f"assignee 재제출 요청(계약 v2 3단계 보고, "
                                      f"tt done {i['id']} --report)"})
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
    # ⑥ C1 unknown 그림자(gate-failure-rules.md §5.1/§7) — 규칙표 어느 행에도
    # 매칭되지 않는 tt-server 실패 기록: 판단기 rank 제안(shadow — 동작 불변, 로그만,
    # 카드당 최신 1건). 사람이 로그를 검토해 표에 행을 추가하는 것이 유일한 반영 경로.
    # 판단기 미구성이면 아무 일도 일어나지 않는다(규칙만 — 로그 접근도 없음, 리뷰 R1).
    if judge.get_judge() is not None:
        for i in issues:
            if i.get("archived"):
                continue
            latest = None  # (ts, id, body) — 카드당 최신 미매칭 사건(리뷰 R7)
            for c in (i.get("comments") or []):
                if c.get("author") != "tt-server":
                    continue
                body = c.get("body") or ""
                if body.startswith(("done 증거 없음", "보고 접수")):
                    continue  # 규칙표 행 존재(evidence_rejected) — C1 아님
                if "verify 422" in body:
                    continue  # verify_422 정식 행의 기계적 표식만 제외(리뷰 R8)
                if not re.search(r"실패|오류|거부|거절|4\d\d", body):
                    continue
                k = (c.get("ts") or c.get("created_at") or "", c.get("id") or 0)
                if latest is None or k > latest[:2]:
                    latest = (k[0], k[1], body)
            if latest is None:
                continue
            view = {"card": i["id"], "input": {"comment": latest[2][:800],
                                               "state": i["state"]}}
            ih = judge.input_hash("C1", view)
            if _C1_SEEN.get(i["id"]) == ih:
                continue  # 같은 입력 재판정 방지(프로세스 내 dedup)
            _C1_SEEN[i["id"]] = ih
            if judge.JudgeLog().seen(i["id"], "C1", ih):
                continue  # 로그에 이미 판정된 입력(재시작 후에도 dedup)
            judge.run_advisory("C1", view, "unknown", cycle_id=snap.get("cycle_id"))
    return actions


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
    """카드 코멘트의 github PR URL → [(owner/repo, pr_no)] — 등장 순 중복 제거."""
    seen = []
    for c in issue.get("comments") or []:
        for m in PR_URL.finditer(c.get("body") or ""):
            t = (m.group(1), int(m.group(2)))
            if t not in seen:
                seen.append(t)
    return seen


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


def collect_prs(repos=None):
    """open PR + checks — repo 단위·PR 단위 실패는 그 PR만 체크 없음으로 취급.
    repo 목록: TT_REPO_SLUG(쉼표) 기본 + auto/todo/blocked 카드의 card_repo 표기(동적).

    실측(t_501e6ec3, 2026-09-29): gh pr list --head 는 exact prefix 필터 —
    'tt/' 같은 접두어는 []를 돌려 후보 0건(probe 실명)이 된다. 전체 open 목록을
    받고 브랜치 역참조는 decide에서. checks 없는 PR(실 PR#3 'no checks
    reported' 예외)은 [] 격리 후 ci_passed(빈 checks)=False 가 자연 차단.
    """
    out = []
    pool = list(repos) if repos else _repos_default()
    for repo in dict.fromkeys(pool):
        try:
            prs = gh_json("pr", "list", "--repo", repo, "--state", "open",
                          "--json", "number,headRefName,headRefOid,title,isDraft,labels") or []
        except Exception:
            continue
        for p in prs:
            try:
                checks = gh_json("pr", "checks", p["number"], "--repo", repo, "--json", "name,state")
                if checks is None:
                    checks = []  # 정상 응답에 check-run 부재 = 확인된 0건
                else:
                    checks = list(checks)
            except Exception:
                # 조회 실패(API 오류·rate limit·네트워크) — 빈 목록과 구분(리뷰 R1).
                # None은 '관측 불가': decide는 0건 확인으로 취급하지 않는다(병합 우회 방지).
                checks = None
            out.append({"number": p["number"], "repo": repo, "branch": p.get("headRefName", ""),
                        "title": p.get("title", ""), "head_sha": p.get("headRefOid", ""),
                        "isDraft": bool(p.get("isDraft")),
                        "labels": [l.get("name", "") for l in (p.get("labels") or [])],
                        "checks": checks})
    return out


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


def _review_dispatch_msg(act, agent):
    """리뷰 dispatch 본문 — review-request/review-claim이 공유(형식 동일 유지)."""
    sha8 = (act.get("head_sha") or "")[:8]
    repo = act.get("repo") or "plainOldCode/think-tank"
    return (f"[auto review] PR #{act['pr']} ({repo}) @ {sha8} — 카드 {act['issue']} "
            f"리뷰 요청. 먼저 TT API로 POST /issues/{act['issue']}/claim-review "
            f"({{\"agent\": \"{agent}\"}})를 호출해 리뷰어 점유를 표기한 뒤 진행. "
            f"repo는 https://github.com/{repo} — 기존 로컬 clone 재사용 우선"
            f"(없으면 clone), git fetch origin pull/{act['pr']}/head 후 "
            f"git diff origin/main...FETCH_HEAD(로컬 ref를 만들지 않으니 재리뷰에도 안전). "
            "리뷰 방식: 변경 파일 통독 + 변경 심볼 grep으로 호출자 확인(공용 모듈은 필수). "
            "판정 기준: 계약 v2.1 준수·시크릿 노출·테스트 적절성·놓친 엣지. "
            "결과 제출: GitHub PR 코멘트와 TT 카드 코멘트 양쪽(docs/review-gate.md 형식) — "
            f"첫 줄 'review: approve' 또는 'review: request-changes', 둘째 줄 'PR#{act['pr']}@{sha8}'. "
            f"TT 코멘트 author는 '{agent}'로 게시.")


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


def _now_dt():
    try:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)
    except Exception:
        return None


def _parse_iso(s):
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def execute(url, act):
    kind = act["action"]
    if kind == "release-reviewer":
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
    if kind == "archive-sweep":
        _archive_sweep(url, int(act.get("days") or 0))
        return
    if kind == "merge":
        repo = act.get("repo") or REPO
        # 판정-집행 경합 흡수(t_501e6ec3 통합 회귀): collect 시점 head_sha와
        # 병합 직전 현재 headRefOid가 다르면 폐기(다음 라운드 자연 재시도).
        # 재시도 안전성: gh가 non-mergeable이면 merge 자체를 거부(405)하므로
        # 스킵해도 중복 병합은 구조적으로 발생하지 않는다.
        # 실측: gh pr view 는 번호 위치 인자, 단일 필드도 JSON 객체
        # '{"headRefOid":"<sha>"}' 로 온다(문자열 strip('"')면 영구 불일치).
        if act.get("low_risk"):
            # 저위험 무계약 병합의 최종 경로 차단(M4DEK6WC-MQYS) — server/runner
            # 변경은 사람 승인 대상이다. 라벨만으로 우회하지 않는다.
            try:
                info = gh_json("pr", "view", act["pr"], "--repo", repo,
                               "--json", "files,changedFiles") or []
                fl = info.get("files") or []
                paths = [f.get("path", "") for f in fl]
            except Exception as e:
                _probe_flag(url, act, f"probe merge skip: 저위험 경로 확인 실패({e}) — 관측")
                return
            # gh files는 100개 컷오프(페이지네이션 없음) — changedFiles와 불일치면
            # 목록이 불완전: 보수적으로 병합 거부(P76 리뷰 R2).
            if len(paths) < (info.get("changedFiles") or 0):
                _probe_flag(url, act, f"probe merge skip: 저위험 파일 목록 불완전 "
                            f"({len(paths)}/{info.get('changedFiles')}) — 관측")
                return
            if any(p.startswith(("server/", "runner/")) for p in paths):
                _probe_flag(url, act, "probe merge skip: 저위험 경로 아님 "
                            "(server/runner 포함) — 표준 게이트로")
                return
        expected = act.get("head_sha") or ""
        if expected:
            try:
                cur = gh_json("pr", "view", act["pr"], "--repo", repo,
                              "--json", "headRefOid") or {}
                current = cur.get("headRefOid") or ""
            except Exception as e:
                _probe_flag(url, act, f"probe merge skip: head 재확인 실패({e}) — 관측")
                return
            if current != expected:
                _probe_flag(url, act, f"probe merge skip: head 변경됨 "
                               f"({expected[:8]}→{current[:8]}) — 관측")
                return
        try:
            gh_exec("pr", "merge", act["pr"], "--repo", repo, "--squash", "--delete-branch")
        except Exception as e:
            _probe_flag(url, act, f"probe merge skip: gh 오류 {str(e)[:120]} — 관측")
            return
        i = api(url, f"/issues/{act['issue']}")
        st = i["state"]
        cr = i.get("completion_report") or ""
        if isinstance(cr, str) and cr:
            try:
                cr = json.loads(cr)
            except Exception:
                cr = None
        if st == "review" and cr:
            # 병합 성사 + 유효 보고 → verify 경로로만 done (M3R7M0ZR-YF99: done은 확인 직후)
            ci_note = "" if act.get("no_ci") else "(CI green) "
            try:
                api(url, f"/issues/{act['issue']}/verify", "POST",
                    {"verifier": "probe", "completion_report": cr})
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"probe: PR#{act['pr']} merged {ci_note}— "
                                                "verify 통과 → done"})
            except Exception as e:
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"probe: PR#{act['pr']} merged — verify 실패"
                                                f"({str(e)[:100]}). review 유지, 사람 판단 필요"})
            return
        if st == "review":
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"probe: PR#{act['pr']} merged — 완료 보고 없음. "
                                            "review 유지 (tt verify 또는 재작업 review→todo)"})
            return
        if st == "in_progress":
            # 보고 제출(→review) 전에는 done 없음 — 브랜치만 반영되고 카드는 제출 대기
            try:
                api(url, f"/issues/{act['issue']}", "PATCH",
                    {"version": i["version"], "state": "review"})
            except Exception:
                pass
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"probe: PR#{act['pr']} merged — agent 완료 보고 "
                                            "대기(PATCH state=done + 보고 제출로 review 정지)"})
        return
    if kind == "ci-fix":
        cur = api(url, f"/issues/{act['issue']}")
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
        # S1(§5.3) — 실패 요약 annotate(advisory): 판단기 있으면 dispatch 본문에 첨부,
        # 없으면 본문 불변. 판정·위임 여부는 규칙이 이미 정했다.
        _s1 = judge.run_advisory("S1", {"card": act["issue"],
                                        "pr": f"{act.get('repo','')}#{act['pr']}"
                                              f"@{(act.get('head_sha') or '')[:8]}",
                                        "input": {"checks_failed": act.get("checks_failed") or [],
                                                  "branch": act.get("branch", "")}},
                                 "ci-fix dispatch", cycle_id=act.get("cycle_id"))
        _s1_txt = ""
        if _s1 and isinstance(_s1.value, str) and _s1.value.strip():
            # [judge-note <id>] — 기존 파서가 제어 정보로 읽지 않는 새 주석 kind(§5.2).
            # 값은 소독(advisory가 마커 시퀀스를 운반하지 않음) + 한 줄 블록.
            _s1_txt = (f"\n\n[judge-note {_s1.decision_id}] 실패 요약(참고용 — 판정 아님): "
                       f"{judge.sanitize(_s1.value.strip()[:500].replace(chr(10), ' '))}")
        msg = (f"[auto probe] PR #{act['pr']} ({act.get('repo','')}) CI 실패"
               f"(failing: {','.join(act.get('checks_failed') or [])}). 임무: PR을 읽고 **수정**할 것"
               f" — 단순 review 금지, 목적은 CI red 해소. 브랜치 {act.get('branch','')} 위에서 커밋 계속, "
               f"범위는 CI 실패 수정만(리팩·스킵·assertion 약화 금지). 카드 {act['issue']} review 대기 — "
               f"CI green이면 probe가 자동 병합·done. 인수: {target} 담당.{_s1_txt}")
        try:
            api(url, f"/issues/{act['issue']}/dispatch", "POST", {"agent": agent, "message": msg})
        except Exception as e:
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"{marker} dispatch 실패({str(e)[:80]}) — "
                                            "사람 판단 대기(needs-merge 회귀)"})
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} PR#{act['pr']} CI 실패 — {agent}({target}) "
                                        "review+수정 위임"})
        return
    if kind == "review-claim":
        # TT 개선#3c — 교차리뷰 자동 수령: 서버 claim-review로 점유 표기 후 리뷰 dispatch
        # R2: review-request와 동일한 마커 경합 재확인 + 영속화 — 재수령 루프 방지
        cur = api(url, f"/issues/{act['issue']}")
        if any(act["marker"] in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return  # 경합 방어 — decide 판정 후 재확인
        try:
            api(url, f"/issues/{act['issue']}/claim-review", "POST", {"agent": act["agent"]})
        except urllib.error.HTTPError as e:
            # 수령 거부(점유 경합 등) — 사이클 전멸 대신 기록만 남기고 다음 사이클 재시도.
            # review-req 마커를 남기지 않으므로 decide는 재판정한다(R3).
            _claim_refused_note(url, act, cur, e.code)
            return
        try:
            api(url, f"/issues/{act['issue']}/dispatch", "POST",
                {"agent": act["agent"], "message": _review_dispatch_msg(act, act["agent"]),
                 "work_contract": REVIEW_CONTRACT})
        except Exception as e:
            # F4 — claim 점유만 남기고 죽으면 만료까지 타 리뷰어가 409다: 반납 후 기록
            _reviewer_lease_release(url, act["issue"], act["agent"])
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"{act['marker']} 리뷰 dispatch 실패({str(e)[:80]}) — "
                                            "리뷰어 lease 반납, 사람 판단 대기"})
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{act['marker']} PR#{act['pr']} CI green — "
                                        f"자동 수령 → {act['agent']}"})
        return
    if kind in ("review-request", "review-retry"):
        # 리뷰 게이트 1단계 — 리뷰어 에이전트에 리뷰 요청 (docs/review-gate.md)
        # review-retry(12MZ): 세션 사망 정체 재발령 — 회차 마커 dedup + 새 세션 지시문
        cur = api(url, f"/issues/{act['issue']}")
        if any(act["marker"] in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return  # 경합 방어 — decide 판정 후 재확인
        # M4JTJ970-WS3C: decide가 라우팅한 카운터파트 리뷰어 우선 — env는 레거시 폴백.
        # 작성자 본인 신원·구현 가족(고정 어휘·어댑터 별칭)은 매핑 확장과 무관하게 제외(R2).
        author = (cur.get("assignee") or "").strip()
        author_fam = _impl_family(author)
        if act.get("reviewer"):
            cands = [act["reviewer"]] + [c for c in REVIEW_FALLBACK if c != act["reviewer"]]
        else:
            legacy = os.environ.get("TT_REVIEW_AGENT", "").strip() or "kanban-adapter"
            cands = [legacy]
        cands = [c for c in cands
                 if c != author and not (author_fam and agent_family(c) == author_fam)]
        # JWW0 — 진행 중 dispatch가 후보 에이전트를 점유하면 제외(동시 리뷰 방지).
        # decide 이후 시간이 지났을 수 있어 실행 직전 재확인한다.
        # review-retry(12MZ 정체 재발령)에는 적용하지 않는다 — 정체 세션은 판정 없이
        # 끝난 좀비이고 재발령은 fresh: True 새 세션이라 동시 실행이 아니다. 여기서
        # 막으면 회차 마커가 남지 않아 REVIEW_RETRY_MAX·[needs-human-review]
        # 에스컬레이션까지 소리 없이 멈춘다(정체 dispatch도 _ACTIVE_RUN_STATES에
        # 걸리는 함정 — 리뷰 블로커). 진짜 점유 충돌은 claim-review 409 폴백이 흡수.
        if kind == "review-request":
            try:
                _d = api(url, f"/issues/{act['issue']}/dispatches") or []
            except Exception:
                # 조회 실패는 [] fail-open — decide 가드가 1차 방어(의도된 동작)
                _d = []
            _busy = {x.get("agent") for x in _d if isinstance(x, dict)
                     and x.get("run_state") in _ACTIVE_RUN_STATES and x.get("agent")}
            _had_cands = bool(cands)
            cands = [c for c in cands if c not in _busy]
            if _had_cands and not cands:
                return  # 진행 중 스폰 금지 — 마커 없이 다음 사이클 재판정
        # 동기 수령 — claim-review 거부(409)면 다음 후보로 폴백(R3). 성공 전에는
        # review-req 마커를 남기지 않아 전원 거부 시 다음 사이클이 재판정한다.
        agent = None
        for cand in cands:
            try:
                api(url, f"/issues/{act['issue']}/claim-review", "POST", {"agent": cand})
                agent = cand
                break
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        if agent is None:
            # F4 — 가드로 후보가 전부 걸러진 경우와 실제 거부(409)는 사유가 다르다
            if cands:
                _claim_refused_note(url, act, cur, 409)
            else:
                _guard_skipped_note(url, act, cur)
            return
        msg = _review_dispatch_msg(act, agent)
        if kind == "review-retry":
            # 리뷰 R4: 회차 마커를 본문에 포함 — 서버 멱등키(issue+agent+attempt+message
            # 해시)가 회차를 구분하게 해 a2+ 재발령이 a1 행으로 기수락되지 않는다.
            msg += ("\n\n[12MZ] 이전 리뷰 세션이 판정 없이 종료됐다(reviewer_session_failed). "
                    "**새 세션에서 시작**하고 이전 세션 재사용 금지 — 컨텍스트 누적이 부수 원인이었다.\n"
                    + act["marker"])
        dresp = None
        try:
            dresp = api(url, f"/issues/{act['issue']}/dispatch", "POST",
                        {"agent": agent, "message": msg,
                         # 12MZ 리뷰 R5: 재발령은 이전 세션 context 상속을 끊는다 — 새 세션 강제
                         "work_contract": REVIEW_CONTRACT, "fresh": True} if kind == "review-retry" else
                        {"agent": agent, "message": msg, "work_contract": REVIEW_CONTRACT})
        except Exception as e:
            # F4 — claim 점유만 남기고 죽으면 만료까지 타 리뷰어가 409다: 반납 후 기록
            _reviewer_lease_release(url, act["issue"], agent)
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"{act['marker']} 리뷰 dispatch 실패({str(e)[:80]}) — "
                                            "리뷰어 lease 반납, 사람 판단 대기"})
            return
        # 12MZ 리뷰 R3 잔여: 발령 마커에 dispatch id를 남겨 runner 실패 코멘트의
        # dispatch#N과 대조 — 구 회차·작업 dispatch의 늦은 실패가 새 세션 관망을 깨지 않게.
        did = ""
        if isinstance(dresp, dict):
            did = str(dresp.get("id") or "")
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{act['marker']} PR#{act['pr']} CI green — "
                                        f"리뷰 요청 → {agent}" + (f" (dispatch#{did})" if did else "")})
        return
    if kind == "submit-remind":
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
                     "(tt done --report)이 review 진입. 무반응 시 타이머 경과 후 "
                     "자동 승격됩니다(점진 push는 head 변경으로 타이머 리셋)."})
        agents = [a for a in api(url, "/agents") or []]
        if _relay_timer_s(who, agents) >= 6 * 3600:
            api(url, "/messages", "POST",
                {"author": "probe",
                 "body": (f"@{who} " if who else "")
                         + f"[submit-remind] 카드 {act['issue']} PR#{act['pr']} — "
                           "CI green 완료 보고 제출 요망(독려: 자동 승격 예정)"})
        return
    if kind == "submission-adopt":
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
        if any(isinstance(x, dict) and x.get("run_state") in _ACTIVE_RUN_STATES
               for x in _dfx):
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
    if kind == "review-fix-relay":
        # BG97 — [fix-stall] 소비: 위임 후 미실행 정체의 원 워커 재발령 1회.
        # dedup 마커 [rework-relay #pr/sha a{att}]는 성공·실패 모두 남겨
        # 같은 (사유,pr,sha,attempt) 재시도를 막는다(실패면 사람 릴레이로 종결).
        cur = api(url, f"/issues/{act['issue']}")
        att = act.get("att") or cur.get("execution_attempt") or 0
        rmarker = f"[rework-relay #{act['pr']}/{(act.get('head_sha') or '')[:8]} a{att}]"
        if any(rmarker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        # R3 — decide 이후 현재 상태 재확인: 회차 변화·종결·임대·blocked·승인·
        # 워커 응답이 하나라도 있으면 폐기(실행 직전 자격 재검증).
        if cur.get("state") in ("done", "cancelled") \
                or (cur.get("execution_attempt") or 0) != (act.get("att") or 0) \
                or cur.get("state") != "review" or cur.get("waiting_for") == "human" \
                or _lease_held(cur):
            return
        _revs = ({os.environ.get("TT_REVIEW_AGENT", "").strip(), cur.get("reviewer")}
                 | _requested_reviewers(cur, act["pr"], (act.get("head_sha") or "")[:8])
                 ) - {None, ""}
        if review_verdict(cur.get("comments") or [], _revs, act["pr"],
                          (act.get("head_sha") or "")[:8]) == "approve":
            return  # stall 뒤 승인 — 재작업 대상 아님
        _sdtv = _iso(act.get("stall_ts") or "")
        if _sdtv and any(
                (x.get("author") or "") not in _RELAY_OBSERVERS
                and not str(x.get("author") or "").startswith("runner")
                and _comment_after(x, _sdtv, act.get("stall_id") or 0)
                for x in (cur.get("comments") or [])):
            return  # decide 이후 워커 응답·진행 — 폐기(R2/R3)
        if _gh_pr_head(act.get("repo"), act["pr"]) != (act.get("head_sha") or "")[:8]:
            return  # 실제 PR head 변경·조회 불가 — 구 head 대상 스폰 금지(R3)
        _dfix_all = api(url, f"/issues/{act['issue']}/dispatches") or [] if True else []
        assignee = cur.get("assignee") or ""
        agents = [a for a in api(url, "/agents") or []
                  if a.get("enabled") and a.get("base_url")]
        # 전달 사다리(리뷰 R4) — 정확명 우선, hook 전달 실패 시 실행 가능한
        # 같은 가족 수신처 재시도, 전부 실패면 사람 릴레이로 종결.
        target = _resolve_executor(assignee, agents)
        ladder = []
        if target and target not in NON_EXECUTORS:
            ladder.append(target)
            fam = agent_family(target)
            if fam:
                ladder += [a.get("name") for a in agents
                           if a.get("name") != target
                           and a.get("name") not in NON_EXECUTORS
                           and agent_family(a.get("name")) == fam]
        ok = False
        _dfix = []
        if ladder:
            try:
                _dfix = api(url, f"/issues/{act['issue']}/dispatches") or []
            except Exception:
                _dfix = []  # 조회 실패 fail-open — decide 가드가 1차 방어(JWW0)
        # 후보별이 아니라 카드 전체 가드 — 다른 실행자의 수동 dispatch도 동시 스폰 금지(R3)
        if _dfix and ladder and any(isinstance(x, dict)
                                    and x.get("run_state") in _ACTIVE_RUN_STATES
                                    for x in _dfix):
            return
        for cand in ladder:
            if any(isinstance(x, dict) and x.get("agent") == cand
                   and x.get("run_state") in _ACTIVE_RUN_STATES for x in _dfix):
                if cand == target:
                    return  # 진행 중 — 다음 사이클(JWW0)
                continue  # 가족 후보 진행 중 — 다음 후보
            try:
                _resp = api(url, f"/issues/{act['issue']}/dispatch", "POST",
                            {"agent": cand,
                             "message": (f"[auto review-fix] PR #{act['pr']} 리뷰 request-changes(재발령) — "
                                         f"리뷰 코멘트(PR/카드)를 읽고 같은 브랜치 "
                                         f"{act.get('branch', '')} 위에서 수정 커밋. "
                                         f"head {(act.get('head_sha') or '')[:8]}. "
                                         "push하면 probe가 재리뷰 요청.")})
            except Exception:
                continue  # 전달 실패 — 가족 후보로, 없으면 사람 릴레이(R4)
            if isinstance(_resp, dict) and _resp.get("status") == "error":
                continue  # hook 전달 실패(HTTP 5xx·연결 오류) — 성공으로 기록하지 않는다(R4)
            ok = True
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe",
                 "body": f"{rmarker} 원 워커 재발령 — {cand} 세션 실행 중"
                         + ("" if cand == target else "(가족 대행)")})
            break
        if not ok:
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe",
                 "body": f"{rmarker} 원 워커 재발령 불가(assignee: {assignee or '없음'}) — "
                         "사람 세션 전달 요망"})
            api(url, "/messages", "POST",
                {"author": "probe",
                 "body": (f"@{assignee} " if assignee else "")
                         + f"[rework-relay] 카드 {act['issue']} PR#{act['pr']} — 재작업 전달 필요"
                         "(dispatch 미실행 정체[fix-stall], 사람이 세션에 전달)"})
        return
    if kind == "rework-fallback":
        # BG97 2순위 — relay 무반응 양도: assignee 해제+todo 회귀(판정 전문은 코멘트 보존).
        cur = api(url, f"/issues/{act['issue']}")
        marker = act.get("marker") or ""
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        # R3 — decide 이후 현재 상태 재확인: 새 워커가 수령했다면(회차 승격·state·
        # 임대·진행 dispatch) 양도하지 않는다. 판정 대상과 현재 값의 일치 필수.
        if cur.get("state") != "review" \
                or (cur.get("execution_attempt") or 0) != (act.get("att") or 0) \
                or cur.get("waiting_for") == "human" or _lease_held(cur):
            return
        # busy_agents는 스냅샷 파생값(GET에 없음) — 실제 dispatch 상태로 재확인(R3)
        try:
            _dfb = api(url, f"/issues/{act['issue']}/dispatches") or []
        except Exception:
            _dfb = []
        if any(isinstance(x, dict) and x.get("run_state") in _ACTIVE_RUN_STATES
               for x in _dfb):
            return
        _rdtv = _iso(act.get("relay_ts") or "")
        _rid = act.get("relay_id") or 0
        _rcmts = cur.get("comments") or []
        if _rdtv and any(
                (x.get("author") or "") not in _RELAY_OBSERVERS
                and not str(x.get("author") or "").startswith("runner")
                and _comment_after(x, _rdtv, _rid)
                for x in _rcmts):
            return  # decide 이후 워커 응답·활동 — 양도 취소(R3)
        # 실제 head 마커 변화 — relay 시 head와 다른 probe 마커가 그 사이 생겼으면 회귀 금지
        _rsha = (act.get("head_sha") or "")[:8]
        if _rsha and any(x.get("author") == "probe" and _comment_ts(x)
                         and _iso(_comment_ts(x)) is not None and _rdtv
                         and _iso(_comment_ts(x)) > _rdtv
                         and f"#{act.get('pr')}/" in (x.get("body") or "")
                         and _rsha not in (x.get("body") or "")
                         and ("[review-fix #" in (x.get("body") or "")
                              or "[review-req #" in (x.get("body") or "")
                              or "[ci-fix a" in (x.get("body") or ""))
                         for x in _rcmts):
            return
        if act.get("repo") and act.get("pr") and \
                _gh_pr_head(act.get("repo"), act.get("pr")) != \
                (act.get("head_sha") or "")[:8]:
            return  # 실제 PR head 변경·조회 불가 — 회귀 폐기(R3)
        try:
            # CAS — fetch·PATCH 사이 경합은 서버 409로 거절, 다음 사이클 재판정
            labels = sorted(set(cur.get("labels") or []) | {"auto"})  # 자동 풀 재수령 경로(R7)
            api(url, f"/issues/{act['issue']}", "PATCH",
                {"assignee": "", "state": "todo", "labels": labels,
                 "expected_version": cur.get("version")})
        except Exception:
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe",
             "body": f"{marker} relay 무반응(타이머 경과, head 불변) — assignee 해제+todo 회귀. "
                     "판정 전문은 코멘트 보존 — 누구나 재수령 가능"})
        return
    if kind == "review-fix":
        # 리뷰 게이트 2단계 — request-changes: 카드 review 반납 + 배정 에이전트에 수정 위임
        cur = api(url, f"/issues/{act['issue']}")
        if any(act["marker"] in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        assignee = cur.get("assignee") or ""
        delegated = False
        if assignee:
            # R4(6XKC): assignee 발령자 식별코드(skshim@tp-13류)는 등록 러너명과 달라
            # dispatch 404가 누적됐다(42건) — PR#80 가족 해석(_resolve_agent)으로
            # 등록명을 찾아 재시도한다. 정확명 우선은 해석기 내부 규칙 그대로.
            # 해석 실패 시 사람 통보 노트(마커 포함)로 종결 — 같은 head 무음 반복 방지.
            # PXCJ: review-fix 발령은 실행자 한정 해석(_resolve_executor) — 가족 매칭이
            # 어댑터를 돌려 수정 정체를 만들지 않는다.
            agents = [a for a in api(url, "/agents") or []
                      if a.get("enabled") and a.get("base_url")]
            target = _resolve_executor(assignee, agents)
            msg = (f"[auto review-fix] PR #{act['pr']} 리뷰 request-changes — 리뷰 코멘트(PR/카드)를 "
                   f"읽고 같은 브랜치 {act.get('branch', '')} 위에서 수정 커밋. "
                   f"head {(act.get('head_sha') or '')[:8]}. push하면 probe가 재리뷰 요청.")
            if target is None:
                _ratt = cur.get("execution_attempt") or 0
                _rm = f"[rework-relay #{act['pr']}/{(act.get('head_sha') or '')[:8]} a{_ratt}]"
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"{act['marker']} 수정 dispatch 실패"
                                                f"(에이전트 미등록: {assignee}) — 사람 판단 대기"
                                                f"\n{_rm} assignee={assignee} receive_mode=relay"
                                                " — 세션 전달 요망"})
                api(url, "/messages", "POST",
                    {"author": "probe",
                     "body": (f"@{assignee} " if assignee else "")
                             + f"[rework-relay] 카드 {act['issue']} PR#{act['pr']} — "
                               "재작업 전달 필요(등록 에이전트 없음, 사람이 세션에 전달)"})
            elif target in NON_EXECUTORS:
                # PXCJ — 어댑터·리드온리는 코드 작업 불가: dispatch 낭비 대신 사람 통보.
                # 원 워커 돌려주기 등 재라우팅은 M4MGV3B7-BG97 영역.
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"{act['marker']} 수정 dispatch 대상이 실행 불가"
                                                f" 에이전트({target}) — 사람/감독관 판단 대기"})
            else:
                # JWW0 — 대상 에이전트의 진행 중 dispatch가 있으면 스폰 금지(동시 실행 방지)
                try:
                    _dfix = api(url, f"/issues/{act['issue']}/dispatches") or []
                except Exception:
                    # 조회 실패는 [] fail-open — decide 가드가 1차 방어(의도된 동작)
                    _dfix = []
                if any(isinstance(x, dict) and x.get("agent") == target
                       and x.get("run_state") in _ACTIVE_RUN_STATES for x in _dfix):
                    return
                try:
                    _resp = api(url, f"/issues/{act['issue']}/dispatch", "POST",
                                {"agent": target, "message": msg})
                    # R4 — hook 전달 실패 레코드(status=error)를 위임 성공으로 기록하지 않는다
                    if isinstance(_resp, dict) and _resp.get("status") == "error":
                        api(url, f"/issues/{act['issue']}/comments", "POST",
                            {"author": "probe",
                             "body": f"{act['marker']} 수정 dispatch 실패(hook: "
                                     f"{str(_resp.get('detail') or '전달 오류')[:80]}) — 사람 판단 대기"})
                    else:
                        delegated = True
                except Exception as e:
                    api(url, f"/issues/{act['issue']}/comments", "POST",
                        {"author": "probe", "body": f"{act['marker']} 수정 dispatch 실패({str(e)[:80]}) — "
                                                    "사람 판단 대기"})
        else:
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"{act['marker']} 리뷰 request-changes — "
                                            "배정 에이전트 없음, 사람 판단 대기"})
        try:
            api(url, f"/issues/{act['issue']}", "PATCH",
                {"version": get_version(url, act["issue"]), "state": "review"})
        except Exception:
            pass
        # PXCJ 리뷰 R1 — 위임 불가·실패인데 "수정 위임"으로 남기면 직전 통보와 모순
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{act['marker']} PR#{act['pr']} request-changes — "
                                        + ("review 반납+수정 위임" if delegated
                                           else "review 반납 — 수정 위임 없음, 사람 판단 대기")})
        return
    v = get_version(url, act["issue"])
    if kind == "review-note":
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
            # S2(§5.3) — 거부 사유 요약 annotate(advisory): 마커 노트와 분리한 별도
            # [judge-note] 코멘트로(리뷰 R2 — 요약이 기존 마커로 재해석되는 것 방지).
            # 대응(재제출 요청)은 규칙이 이미 정했다.
            _s2 = judge.run_advisory("S2", {"card": act["issue"],
                                            "input": {"reason": act.get("reason", "")}},
                                     "evidence-rejected note", cycle_id=act.get("cycle_id"))
            if _s2 and isinstance(_s2.value, str) and _s2.value.strip():
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"[judge-note {_s2.decision_id}] "
                                                f"사유 요약(참고용 — 판정 아님): "
                                                f"{judge.sanitize(_s2.value.strip()[:300].replace(chr(10), ' '))}"})
            return
        att = cur.get("execution_attempt") or 0
        marker = f"[needs-merge a{att}]"
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} {act['reason']} — "
                                        "tt verify로 done 확정 또는 review→todo 재작업"})
    elif kind == "pr-adopt":
        # 채택 기록만 남긴다. 본문 PATCH는 scope_changed로 처리되어 완료 보고를
        # 삭제하고 attempt를 올린다(codex P1-2) — 하지 않는다. 실제 스캔 풀 편입은
        # collect_repos가 review 카드 코멘트의 PR URL을 읽는다.
        cur = api(url, f"/issues/{act['issue']}")
        marker = act.get("marker") or ""
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])):
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe",
             "body": f"{marker} 코멘트의 PR URL 채택 — 스캔 풀 편입(다음 사이클 관측)"})
    elif kind == "stale-notify":
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
    elif kind == "resume":
        api(url, f"/issues/{act['issue']}", "PATCH", {"state": "todo", "version": v})
    elif kind == "needs-human":
        suffix = "" if act.get("note") else "(30KP 예산 게이트)"
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "dispatchd",
             "body": f"[auto] {act.get('note') or act['reason']} — 라벨 needs-human 자동화 중단{suffix}"})
        cur = api(url, f"/issues/{act['issue']}")
        labels = sorted((set(cur.get("labels") or []) | {"needs-human"}) - {"auto"})
        api(url, f"/issues/{act['issue']}", "PATCH", {"labels": labels})
    elif kind == "work":
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
               f"코드 작업은 브랜치 tt/{act['issue']}-<slug>에서 커밋 후 GitHub PR로만 main에 올려라"
               f"(메인 직push 금지 — 예외는 message 명시 시만). tt issue {act['issue']} 로 상세 확인 후 시작.")
        api(url, f"/issues/{act['issue']}/dispatch", "POST",
            {"agent": act["agent"], "message": msg})
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "dispatchd", "body": f"[auto] dispatch → {act['agent']} ({act['reason']})"})
    return act


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
                                       if isinstance(x, dict)
                                       and x.get("run_state") in _ACTIVE_RUN_STATES
                                       and x.get("agent")})
        if i["state"] == "review":
            # review-note dedup 판정용 — review 카드는 상세(코멘트 포함)를 별도 취득
            try:
                i["comments"] = api(url, f"/issues/{i['id']}").get("comments") or []
            except Exception:
                i["comments"] = []
    return {"auto": os.getenv("TT_AUTO_DISPATCH") == "1",
            "now": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "agents": agents, "issues": issues}


def _sleep(seconds, stop=None):
    """stop 이벤트가 있으면 wait(즉시 깨움), 없으면 sleep. False=중단 요청."""
    if stop is None:
        time.sleep(seconds)
        return True
    return not stop.wait(seconds)


def run_once(url, dry=False):
    """1회 스캔→판정→집행. 내장화(서버 스레드)와 standalone이 공유."""
    snap = snapshot(url)
    repos = list(_repos_default())
    for r in collect_repos(snap.get("issues") or []):
        repos.append(r)
    snap["prs"] = collect_prs(repos)
    # 게이트 unset 여부와 무관 — in_progress 자동화(JXYS)도 코멘트 dedup이 필요.
    hydrate_reviews(url, snap)
    snap["cycle_id"] = uuid.uuid4().hex  # §8.1 판단 로그 사이클 식별자(리뷰 R4)
    acts = decide(snap)
    for a in acts:
        print(time.strftime("%F %T"), a, flush=True)
        if not dry:
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
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except OSError:
        return None


def loop(url=None, interval=30, stop=None, dry=None, db_path=None):
    """주기 실행 루프. stop 이벤트(선택)로 즉시 종료. 서버 장애 시 지수 백오프(≤300s).

    BTRS(하드닝): ①url=None이면 매 사이클 TT_URL을 재평가한다 — 내장 스레드가
    부팅 시 고정한 URL이라 멀티워커·URL 변경마다 재시작이 필요했던 것 제거
    (standalone과 동일 계약). ②db_path가 주어지면 파일 잠금 단일 구동 가드 —
    멀티워커에서 워커당 중복 구동 방지(선점 실패 시 파킹, 리더 종료 시 인수계).
    """
    if dry is None:
        dry = os.getenv("TT_DISPATCH_DRYRUN") == "1"
    if db_path and _acquire_probe_lock(db_path) is None:
        print(time.strftime("%F %T"), "probe: 다른 워커가 구동 중 — 파킹(인수계 대기)",
              flush=True)
        while _sleep(min(max(interval, 5), 30), stop):
            if _acquire_probe_lock(db_path) is not None:
                break  # 리더 종료 — 인수계
        else:
            return  # stop 요청
    fails = 0
    while not (stop is not None and stop.is_set()):
        cur = url or os.environ.get("TT_URL", "http://127.0.0.1:7800")
        try:
            run_once(cur, dry=dry)
            fails = 0
        except Exception as e:  # 서버 장애 — 백오프. 쓰기가 없으면 lease는 TTL로 자연 해금.
            fails += 1
            print(time.strftime("%F %T"), "error:", e, flush=True)
            if not _sleep(min(interval * 2 ** min(fails, 4), 300), stop):
                break
            continue
        if not _sleep(interval, stop):
            break


def main():
    loop(None, int(os.getenv("TT_DISPATCH_INTERVAL", "30")))


if __name__ == "__main__":
    main()
