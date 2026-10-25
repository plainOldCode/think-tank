"""카드↔PR·저장소 매핑 규칙(TMXJ-S1).

repo 마커 파싱(card_repo)·브랜치/제목 카드 ID 조인(pr_card_id)·PR 수집
(collect_prs)·patch-id 캐시. 접근 계층은 probe.http를 쓴다."""
import os
import re

from .http import gh_exec, gh_json


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


def collect_prs(repos=None):
    """Optional provider observations; TT_PR_PROVIDER=none disables GitHub I/O.

    T3YT(M58SPT6D-T3YT): repo 단위 관측 결과(ok|failed)를 함수 속성에 실어
    되돌린다 — 반환은 기존 PR 리스트 계약 유지(monkeypatch 호환). run_once가
    snap["pr_observed"]로 승격한다."""
    from .providers import collect
    prs, observed = collect(list(repos) if repos else _repos_default(), gh_json)
    collect_prs.observed = observed
    return prs


# --- 진행 중 run 상태 — config.PROBE_ACTIVE_RUN_STATES 단일 정의(W0DF) ---

from config import PROBE_ACTIVE_RUN_STATES  # W0DF — 활성 run 상태 단일 정의

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
