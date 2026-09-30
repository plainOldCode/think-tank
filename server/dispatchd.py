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


def card_from_branch(branch):
    m = CARD_IN_BRANCH.search(branch or "")
    return m.group(1) if m else None


def _review_grace_min():
    """review 전이 후 'PR 없음' 판정 유예(분). TT_REVIEW_GRACE_MIN(기본 20)."""
    try:
        return max(0, int(os.getenv("TT_REVIEW_GRACE_MIN", "20")))
    except ValueError:
        return 20


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
    by_id = {i["id"]: i for i in issues}
    for p in snap.get("prs") or []:
        iid = card_from_branch(p.get("branch", ""))
        i = by_id.get(iid) if iid else None
        if not iid or i is None or i["state"] in ("done", "cancelled"):
            continue
        if not i.get("work_contract") or (i.get("execution_attempt") or 0) < 1:
            continue
        want = card_repo(i) or REPO_CARDS["think-tank"]
        pr_repo = p.get("repo") or REPO_CARDS["think-tank"]
        if want != pr_repo:
            continue
        if ci_passed(p):
            actions.append({"agent": "probe", "issue": iid, "action": "merge", "pr": p["number"],
                            "head_sha": p.get("head_sha", ""),
                            "repo": pr_repo,
                            "reason": f"CI green + 카드 계약/수령 검증 — gh pr merge ({pr_repo})"})

    # ⓪b review 카드 중 병합 불가 후보 — 사람 판단 요청 코멘트 (M3R7M0ZR-YF99).
    # merge 후보(⓪)에 없는 review 카드만. 회차 마커 '[needs-merge a<n>'로 회차당 1회.
    merged_targets = {a["issue"] for a in actions if a["action"] == "merge"}
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
        prs_for = [p for p in snap.get("prs") or []
                   if card_from_branch(p.get("branch", "")) == i["id"]]
        if prs_for:
            states = [c.get("state") for p in prs_for for c in p.get("checks") or []]
            if states and all(x == "SUCCESS" for x in states):
                reason = "PR green — 병합 판정 제외됨(repo 불일치 등) — 확인 필요"
            elif any(x == "FAILURE" for x in states):
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
                          "--json", "number,headRefName,headRefOid") or []
        except Exception:
            continue
        for p in prs:
            try:
                checks = gh_json("pr", "checks", p["number"], "--repo", repo, "--json", "name,state") or []
            except Exception:
                checks = []
            out.append({"number": p["number"], "repo": repo, "branch": p.get("headRefName", ""),
                        "head_sha": p.get("headRefOid", ""), "checks": checks})
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
    v = get_version(url, act["issue"])
    if kind == "review-note":
        cur = api(url, f"/issues/{act['issue']}")
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


def main():
    url = os.getenv("TT_URL", "http://127.0.0.1:7800")
    interval = int(os.getenv("TT_DISPATCH_INTERVAL", "30"))
    dry = os.getenv("TT_DISPATCH_DRYRUN") == "1"
    fails = 0
    while True:
        try:
            snap = snapshot(url)
            repos = list(_repos_default())
            for i in snap.get("issues") or []:
                if i["state"] in ("auto", "todo", "blocked") or (i.get("labels") and "auto" in i["labels"]):
                    r = card_repo(i)
                    if r:
                        repos.append(r)
            snap["prs"] = collect_prs(repos)
            acts = decide(snap)
            for a in acts:
                print(time.strftime("%F %T"), a, flush=True)
                if not dry:
                    execute(url, a)
            fails = 0
        except Exception as e:  # 서버 장애 — 백오프. 쓰기가 없으면 lease는 TTL로 자연 해금.
            fails += 1
            print(time.strftime("%F %T"), "error:", e, flush=True)
            time.sleep(min(interval * 2 ** min(fails, 4), 300))
            continue
        time.sleep(interval)


if __name__ == "__main__":
    main()
