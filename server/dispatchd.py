#!/usr/bin/env python3
"""tt-dispatchd — TT 자율 스케줄러. decide()는 판정 순수 함수, 실행기는 public API만 소비.

30KP 설계 docs/dispatchd.md. 서브프로세스·서버 수정 없음. 상태 비저장: 근거는 카드·이력.
launchd com.tt.dispatchd (mini), TT_AUTO_DISPATCH=1 이어야 동작.
"""
import json
import os
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


def execute(url, act):
    kind = act["action"]
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
