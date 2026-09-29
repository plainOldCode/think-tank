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
        if ci_passed(p):
            actions.append({"agent": "probe", "issue": iid, "action": "merge", "pr": p["number"],
                            "head_sha": p.get("head_sha", ""),
                            "reason": "CI green + 카드 계약/수령 검증 — gh pr merge"})

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


REPO = os.environ.get("TT_REPO_SLUG", "plainOldCode/think-tank")


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


def collect_prs():
    """open PR + checks — PR 단위 실패는 체크 없음으로 취급(개 PR은 계속 본업).

    실측(t_501e6ec3, 2026-09-29): gh pr list --head 는 exact prefix 필터 —
    'tt/' 같은 접두어는 []를 돌려 후보 0건(probe 실명)이 된다. 전체 open 목록을
    받고 브랜치 역참조는 decide에서. checks 없는 PR(실 PR#3 'no checks
    reported' 예외)은 [] 격리 후 ci_passed(빈 checks)=False 가 자연 차단.
    """
    out = []
    try:
        prs = gh_json("pr", "list", "--repo", REPO, "--state", "open",
                      "--json", "number,headRefName,headRefOid") or []
    except Exception:
        return []
    for p in prs:
        try:
            checks = gh_json("pr", "checks", p["number"], "--repo", REPO, "--json", "name,state") or []
        except Exception:
            checks = []
        out.append({"number": p["number"], "branch": p.get("headRefName", ""),
                    "head_sha": p.get("headRefOid", ""), "checks": checks})
    return out


def execute(url, act):
    kind = act["action"]
    if kind == "merge":
        # 판정-집행 경합 흡수(t_501e6ec3 통합 회귀): collect 시점 head_sha와
        # 병합 직전 현재 headRefOid가 다르면 폐기(다음 라운드 자연 재시도).
        # 재시도 안전성: gh가 non-mergeable이면 merge 자체를 거부(405)하므로
        # 스킵해도 중복 병합은 구조적으로 발생하지 않는다.
        # 실측: gh pr view 는 번호 위치 인자, 단일 필드도 JSON 객체
        # '{"headRefOid":"<sha>"}' 로 온다(문자열 strip('"')면 영구 불일치).
        expected = act.get("head_sha") or ""
        if expected:
            try:
                cur = gh_json("pr", "view", act["pr"], "--repo", REPO,
                              "--json", "headRefOid") or {}
                current = cur.get("headRefOid") or ""
            except Exception as e:
                print(time.strftime("%F %T"),
                      f"merge skip PR#{act['pr']}: head 재확인 실패({e}) — 안전 스킵", flush=True)
                return
            if current != expected:
                print(time.strftime("%F %T"),
                      f"merge skip PR#{act['pr']}: head 변경됨 "
                      f"({expected[:8]}→{current[:8]})", flush=True)
                return
        gh_exec("pr", "merge", act["pr"], "--repo", REPO, "--squash", "--delete-branch")
        i = api(url, f"/issues/{act['issue']}")
        target = i["state"]
        if i["state"] == "in_progress":
            target = "done" if i.get("completion_report") else "review"
        if target != i["state"]:
            try:
                api(url, f"/issues/{act['issue']}", "PATCH",
                    {"version": i["version"], "state": target})
            except Exception:
                i2 = api(url, f"/issues/{act['issue']}")
                api(url, f"/issues/{act['issue']}", "PATCH",
                    {"version": i2["version"], "state": "review"})
        api(url, f"/issues/{act['issue']}/comments", "POST",
            {"author": "probe", "body": f"probe: PR#{act['pr']} merged (CI green) — 상태 {target}"})
        return
    v = get_version(url, act["issue"])
    if kind == "resume":
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
            snap["prs"] = collect_prs()
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
