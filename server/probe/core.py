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
import urllib.request

from service import REVIEW_CONTRACT

PULL_HINT = "tt claim ID {agent} 후 dispatch — pull(풀) 사용 금지(30KP/W1DP)"


def _prio(i):
    p = i.get("priority")
    return (p is None, p if p is not None else 999, i["id"])


def _auto_todo(issues, exclude=()):
    return [i for i in issues if i["state"] == "todo"
            and "auto" in (i.get("labels") or []) and i["id"] not in exclude]


def _budget_blocked(i):
    if (i.get("dispatches") or 0) >= 2:
        return "dispatch-tries>=2"
    if (i.get("execution_attempt") or 0) >= 2:
        return "attempt>=2"
    return None


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
    review 카드 포함(GBE5 사고) + repo 미표기 카드도known repo에서 브랜치/제목 ID로 발견된다(사용자 지시)."""
    out = sorted(set(REPO_CARDS.values()))
    for extra in os.environ.get("TT_REPO_SCAN_EXTRA", "").split(","):
        if extra.strip():
            out.append(extra.strip())
    for i in issues or []:
        if i["state"] in ("auto", "todo", "blocked", "review") or (i.get("labels") and "auto" in i["labels"]):
            r = card_repo(i)
            if r:
                out.append(r)
    return out


def _review_grace_min():
    """review 전이 후 'PR 없음' 판정 유예(분). TT_REVIEW_GRACE_MIN(기본 20)."""
    try:
        return max(0, int(os.getenv("TT_REVIEW_GRACE_MIN", "20")))
    except ValueError:
        return 20


REVIEW_LINE = re.compile(r"^review:\s*(approve|request-changes)\b", re.I)
PR_SHA = re.compile(r"PR#(\d+)@([0-9a-fA-F]{8})")


def review_verdict(comments, reviewer, pr_no, sha8):
    """현재 PR head(pr_no@sha8)에 유효한 리뷰어 판정. 없거나 stale이면 None.

    리뷰어 코멘트만 인정(author 일치), 'PR#<n>@<sha8>' 불일치는 stale. 최신 판정 우선.
    """
    verdict = None
    for c in comments or []:
        if c.get("author") != reviewer:
            continue
        body = c.get("body") or ""
        m = PR_SHA.search(body)
        if not m or int(m.group(1)) != pr_no or m.group(2).lower() != (sha8 or "").lower():
            continue
        v = REVIEW_LINE.search(body.strip())
        if v:
            verdict = v.group(1).lower()
    return verdict


def _probe_marker(i, marker):
    return any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe")


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


def decide(snap):
    if not snap.get("auto"):
        return []
    issues = snap.get("issues") or []
    agents = [a for a in (snap.get("agents") or [])
              if a.get("enabled") and a.get("base_url")]
    now = snap.get("now") or ""
    actions = []
    claimed = set()

    def idle(name):
        return not any(i["state"] == "in_progress" and i.get("assignee") == name
                       and (i.get("lease_expires") or "9999") > now for i in issues)

    def pick(cands):
        """가장 낮은 우선순위의 예산 통과 후보(동률 id순). 없으면 None."""
        for i in sorted(cands, key=_prio):
            if not _budget_blocked(i):
                return i
        return None

    # ① release-ready 재개(수동 재개 규약의 기계 대행 — 서버 release_ready 판정 근거)
    resumable = [i for i in issues if i["state"] == "blocked"
                 and i.get("waiting_for") == "dependency" and i.get("release_ready")]
    actor = next((a["name"] for a in agents if idle(a["name"])), None)
    if resumable and actor:
        t = resumable[0]
        actions.append({"agent": actor, "issue": t["id"], "action": "resume",
                        "reason": "release-ready"})
        return actions

    # ② agent별 작업 대상: continuation(자식→형제) → auto 풀
    for a in agents:
        name = a["name"]
        if not idle(name):
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
        actions.append({"agent": name, "issue": target["id"], "action": "work",
                        "reason": reason})

    # ⓪(사실상 우선) probe: CI green PR + 카드 연결/수령/계약 검증 → gh pr merge
    # 리뷰 게이트(N0AN, docs/review-gate.md): TT_REVIEW_AGENT 설정 시 승인 코멘트 필요.
    by_id = {i["id"]: i for i in issues}
    reviewer = os.environ.get("TT_REVIEW_AGENT", "").strip()
    gated = set()  # 리뷰 진행 중 카드 — ⓪b needs-merge 소음 제외
    for p in snap.get("prs") or []:
        iid = pr_card_id(p)
        i = by_id.get(iid) if iid else None
        if not iid or i is None or i["state"] in ("done", "cancelled"):
            continue
        if not i.get("work_contract") or (i.get("execution_attempt") or 0) < 1:
            continue
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
        if ci_passed(p):
            if reviewer:
                if i["state"] != "review":
                    # 게이트 on: 제출 전 카드는 리뷰·병합 대상 아님 — 움직이는 대상 리뷰 낭비 방지.
                    # agent가 보고 제출(state=review)하면 다음 라운드에서 판정.
                    continue
                gated.add(iid)
                sha8 = (p.get("head_sha") or "")[:8]
                verdict = review_verdict(i.get("comments"), reviewer, p["number"], sha8)
                if verdict in ("approve", "request-changes") and i.get("reviewer"):
                    # 리뷰어 claim 해제 — 판정 기록됐으면 점유 반납(review-fix 시 작업자 assignee 보존)
                    actions.append({"agent": "probe", "issue": iid, "action": "release-reviewer",
                                    "reviewer": i["reviewer"], "reason": "판정 기록 — 리뷰어 점유 반납"})
                if verdict == "request-changes":
                    marker = f"[review-fix #{p['number']}/{sha8}]"
                    if not _probe_marker(i, marker):
                        actions.append({"agent": "probe", "issue": iid, "action": "review-fix",
                                        "pr": p["number"], "repo": pr_repo,
                                        "branch": p.get("branch", ""),
                                        "head_sha": p.get("head_sha", ""), "marker": marker,
                                        "reason": f"PR#{p['number']} 리뷰 request-changes — "
                                                  "review 반납+수정 위임"})
                    continue
                if verdict is None:  # 미리뷰 또는 stale — (재)요청
                    marker = f"[review-req #{p['number']}/{sha8}]"
                    if not _probe_marker(i, marker):
                        actions.append({"agent": "probe", "issue": iid, "action": "review-request",
                                        "pr": p["number"], "repo": pr_repo,
                                        "branch": p.get("branch", ""),
                                        "head_sha": p.get("head_sha", ""), "marker": marker,
                                        "reason": f"PR#{p['number']} CI green — 리뷰 요청 → {reviewer}"})
                    continue
                # approve — 아래 merge로 통과
                gated.discard(iid)
            actions.append({"agent": "probe", "issue": iid, "action": "merge", "pr": p["number"],
                            "head_sha": p.get("head_sha", ""),
                            "repo": pr_repo,
                            "reason": f"CI green + 카드 계약/수령 검증 — gh pr merge ({pr_repo})"})

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
        marker = f"[needs-merge a{att}]"
        if any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
               if c.get("author") == "probe"):
            continue
        prs_for = [p for p in snap.get("prs") or [] if pr_card_id(p) == i["id"]]
        drafts = [p for p in prs_for if p.get("isDraft")]
        if drafts and len(drafts) == len(prs_for):
            # 전부 draft — 병합·ci-fix 판정 불가. ready 요청 1회(마커 dedup) 후 무음.
            marker = f"[draft-flagged #{drafts[0]['number']}]"
            if not any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
                       if c.get("author") == "probe"):
                actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                                "marker": marker,
                                "reason": f"draft PR#{drafts[0]['number']} — ready for review "
                                          "요청(준비되면 draft 해제)"})
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
                if any(marker in (c.get("body") or "") for c in (i.get("comments") or [])
                       if c.get("author") == "probe"):
                    continue  # same head 재위임 금지 — 새 커밋(head 변경) 시에만 재판정
                actions.append({"agent": "dispatchd", "issue": i["id"], "action": "ci-fix",
                                "pr": p0["number"], "repo": p0.get("repo", ""),
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
            reason = "PR 없음"
        actions.append({"agent": "probe", "issue": i["id"], "action": "review-note",
                        "reason": f"{reason} — 사람 판단 대기"})

    # ③ 예산 소진 카드 — dispatch 대신 needs-human (auto 풀 전수)
    for i in _auto_todo(issues, claimed):
        why = _budget_blocked(i)
        if why:
            actions.append({"agent": actor or "dispatchd", "issue": i["id"],
                            "action": "needs-human", "reason": why})
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
                          "--json", "number,headRefName,headRefOid,title,isDraft") or []
        except Exception:
            continue
        for p in prs:
            try:
                checks = gh_json("pr", "checks", p["number"], "--repo", repo, "--json", "name,state") or []
            except Exception:
                checks = []
            out.append({"number": p["number"], "repo": repo, "branch": p.get("headRefName", ""),
                        "title": p.get("title", ""), "head_sha": p.get("headRefOid", ""),
                        "isDraft": bool(p.get("isDraft")), "checks": checks})
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


def execute(url, act):
    kind = act["action"]
    if kind == "release-reviewer":
        cur = api(url, f"/issues/{act['issue']}")
        if cur.get("reviewer") == act.get("reviewer"):
            # reviewer="" → 서버에서 None+lease 해제. expected_version으로 경합 방어
            # (t_501e6ec3: 판정-집행 경합 — collect 시점 version과 현재 다르면 409 후 다음 라운드 재시도)
            api(url, f"/issues/{act['issue']}", "PATCH",
                {"expected_version": cur["version"], "reviewer": ""})
        return
    if kind == "merge":
        repo = act.get("repo") or REPO
        # 판정-집행 경합 흡수(t_501e6ec3 통합 회귀): collect 시점 head_sha와
        # 병합 직전 현재 headRefOid가 다르면 폐기(다음 라운드 자연 재시도).
        # 재시도 안전성: gh가 non-mergeable이면 merge 자체를 거부(405)하므로
        # 스킵해도 중복 병합은 구조적으로 발생하지 않는다.
        # 실측: gh pr view 는 번호 위치 인자, 단일 필드도 JSON 객체
        # '{"headRefOid":"<sha>"}' 로 온다(문자열 strip('"')면 영구 불일치).
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
            try:
                api(url, f"/issues/{act['issue']}/verify", "POST",
                    {"verifier": "probe", "completion_report": cr})
                api(url, f"/issues/{act['issue']}/comments", "POST",
                    {"author": "probe", "body": f"probe: PR#{act['pr']} merged (CI green) — "
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
        marker = f"[ci-fix a{att} #{act['pr']}/{sha8}]"
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return  # 경합 방어 — decide 판정 후 재확인
        agent = os.environ.get("TT_CI_FIX_AGENT", "kanban-adapter")
        target = os.environ.get("TT_CI_FIX_TARGET", "agy")
        msg = (f"[auto probe] PR #{act['pr']} ({act.get('repo','')}) CI 실패"
               f"(failing: {','.join(act.get('checks_failed') or [])}). 임무: PR을 읽고 **수정**할 것"
               f" — 단순 review 금지, 목적은 CI red 해소. 브랜치 {act.get('branch','')} 위에서 커밋 계속, "
               f"범위는 CI 실패 수정만(리팩·스킵·assertion 약화 금지). 카드 {act['issue']} review 대기 — "
               f"CI green이면 probe가 자동 병합·done. 인수: {target} 담당.")
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
    if kind == "review-request":
        # 리뷰 게이트 1단계 — 리뷰어 에이전트에 리뷰 요청 (docs/review-gate.md)
        cur = api(url, f"/issues/{act['issue']}")
        if any(act["marker"] in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return  # 경합 방어 — decide 판정 후 재확인
        agent = os.environ.get("TT_REVIEW_AGENT", "").strip() or "kanban-adapter"
        sha8 = (act.get("head_sha") or "")[:8]
        repo = act.get("repo") or "plainOldCode/think-tank"
        msg = (f"[auto review] PR #{act['pr']} ({repo}) @ {sha8} — 카드 {act['issue']} "
               f"리뷰 요청. repo는 https://github.com/{repo} — 기존 로컬 clone 재사용 우선"
               f"(없으면 clone), git fetch origin pull/{act['pr']}/head 후 "
               f"git diff origin/main...FETCH_HEAD(로컬 ref를 만들지 않으니 재리뷰에도 안전). "
               "리뷰 방식: 변경 파일 통독 + 변경 심볼 grep으로 호출자 확인(공용 모듈은 필수). "
               "판정 기준: 계약 v2.1 준수·시크릿 노출·테스트 적절성·놓친 엣지. "
               "결과 제출: GitHub PR 코멘트와 TT 카드 코멘트 양쪽(docs/review-gate.md 형식) — "
               f"첫 줄 'review: approve' 또는 'review: request-changes', 둘째 줄 'PR#{act['pr']}@{sha8}'. "
               f"TT 코멘트 author는 '{agent}'로 게시.")
        try:
            api(url, f"/issues/{act['issue']}/dispatch", "POST",
                {"agent": agent, "message": msg, "work_contract": REVIEW_CONTRACT})
        except Exception as e:
            api(url, f"/issues/{act['issue']}/comments", "POST",
                {"author": "probe", "body": f"{act['marker']} 리뷰 dispatch 실패({str(e)[:80]}) — "
                                            "사람 판단 대기"})
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{act['marker']} PR#{act['pr']} CI green — "
                                        f"리뷰 요청 → {agent}"})
        return
    if kind == "review-fix":
        # 리뷰 게이트 2단계 — request-changes: 카드 review 반납 + 배정 에이전트에 수정 위임
        cur = api(url, f"/issues/{act['issue']}")
        if any(act["marker"] in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        assignee = cur.get("assignee") or ""
        if assignee:
            msg = (f"[auto review-fix] PR #{act['pr']} 리뷰 request-changes — 리뷰 코멘트(PR/카드)를 "
                   f"읽고 같은 브랜치 {act.get('branch', '')} 위에서 수정 커밋. push하면 probe가 재리뷰 요청.")
            try:
                api(url, f"/issues/{act['issue']}/dispatch", "POST",
                    {"agent": assignee, "message": msg})
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
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{act['marker']} PR#{act['pr']} request-changes — "
                                        "review 반납+수정 위임"})
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
        att = cur.get("execution_attempt") or 0
        marker = f"[needs-merge a{att}]"
        if any(marker in (c.get("body") or "") for c in (cur.get("comments") or [])
               if c.get("author") == "probe"):
            return
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"{marker} {act['reason']} — "
                                        "tt verify로 done 확정 또는 review→todo 재작업"})
    elif kind == "resume":
        api(url, f"/issues/{act['issue']}", "PATCH", {"state": "todo", "version": v})
    elif kind == "needs-human":
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "dispatchd", "body": f"[auto] {act['reason']} — 라벨 needs-human 자동화 중단(30KP 예산 게이트)"})
        cur = api(url, f"/issues/{act['issue']}")
        labels = sorted((set(cur.get("labels") or []) | {"needs-human"}) - {"auto"})
        api(url, f"/issues/{act['issue']}", "PATCH", {"labels": labels})
    elif kind == "work":
        r = api(url, f"/issues/{act['issue']}/claim", "POST", {"agent": act["agent"]})
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
    issues = api(url, "/issues?limit=500")
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
    if os.getenv("TT_REVIEW_AGENT", "").strip():
        hydrate_reviews(url, snap)
    acts = decide(snap)
    for a in acts:
        print(time.strftime("%F %T"), a, flush=True)
        if not dry:
            execute(url, a)
    # 내장화 관측점: 서버 로그에서 사이클 생존을 확인 가능하게(빈 라운드도 1행)
    print(time.strftime("%F %T"),
          f"[probe] cycle: issues={len(snap['issues'])} prs={len(snap['prs'])} actions={len(acts)}",
          flush=True)
    return acts


def loop(url, interval, stop=None, dry=None):
    """주기 실행 루프. stop 이벤트(선택)로 즉시 종료. 서버 장애 시 지수 백오프(≤300s)."""
    if dry is None:
        dry = os.getenv("TT_DISPATCH_DRYRUN") == "1"
    fails = 0
    while not (stop is not None and stop.is_set()):
        try:
            run_once(url, dry=dry)
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
    loop(os.getenv("TT_URL", "http://127.0.0.1:7800"),
         int(os.getenv("TT_DISPATCH_INTERVAL", "30")))


if __name__ == "__main__":
    main()
