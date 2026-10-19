"""JXYS(M4MGV3BY) — in_progress 자동화: ci-fix 확장 + green 무보고 독려→승격."""
import pytest

from probe import core as dispatchd
from probe import core as probe_core

SHA8 = "b" * 8
CARD = "M3PXXXXX-9ABC"
TS_NOW = "2026-10-11T16:30:00+0900"
TS_OLD = "2026-10-11T08:00:00+0900"  # 8.5h 전 — relay(6h) 타이머 경과


def issue(iid=CARD, state="in_progress", assignee="codex", att=1, comments=(), **kw):
    base = {"id": iid, "state": state, "assignee": assignee, "version": 7,
            "lease_expires": None, "lease_by": None, "labels": [], "priority": None,
            "execution_attempt": att, "dispatches": 1, "release_ready": False,
            "waiting_for": None, "busy_agents": [],
            "work_contract": {"version": "tt-tdd-v2:x"}, "comments": list(comments),
            "updated_at": TS_OLD, "title": iid, "body": ""}
    base.update(kw)
    return base


def snap(issues, prs=()):
    return {"auto": True, "now": TS_NOW, "agents": [], "issues": list(issues),
            "prs": list(prs)}


def pr(number=3, sha="b" * 40, states=("SUCCESS",), branch=None):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": [{"state": x} for x in states],
            "branch": branch or f"tt/{CARD}-x", "title": "",
            "head_sha": sha}


GREEN = pr()
FAIL = pr(states=("FAILURE", "FAILURE"))


def _mk(comments=(), **kw):
    return issue(comments=comments, **kw)


# --- 1) CI 실패 → ci-fix-ip 위임 ---

def test_in_progress_카드의_CI_실패는_ci_fix_ip로_위임한다():
    acts = dispatchd.decide(snap([_mk()], [FAIL]))
    c = [a for a in acts if a["action"] == "ci-fix"]
    assert len(c) == 1
    assert c[0]["marker"] == f"[ci-fix-ip a1 #3/{SHA8}]"
    assert not [a for a in acts if a["action"] == "submit-remind"]


def test_ci_fix_ip는_마커_dedup으로_1회다():
    from probe.core import _probe_marker
    card = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                          "body": f"[ci-fix-ip a1 #3/{SHA8}] 위임"}])
    assert not [a for a in dispatchd.decide(snap([card], [FAIL]))
                if a.get("action") == "ci-fix"]


# --- 2) green + 무보고 → 독려 1회 ---

def test_green_무보고는_독려_1회다():
    card = _mk()
    acts = dispatchd.decide(snap([card], [GREEN]))
    r = [a for a in acts if a["action"] == "submit-remind"]
    assert len(r) == 1 and r[0]["marker"] == f"[submit-remind a1 #3/{SHA8}]"
    # 독려 마커 존재 → 재독려 없음
    card2 = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                           "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}])
    assert not [a for a in dispatchd.decide(snap([card2], [GREEN]))
                if a.get("action") == "submit-remind"]


def test_독려_후_타이머_경과시_기계적_승격한다():
    card = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                          "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}])
    acts = dispatchd.decide(snap([card], [GREEN]))
    a = [x for x in acts if x["action"] == "submission-adopt"]
    assert len(a) == 1 and a[0]["att"] == 1 and a[0]["expected_version"] == 7


def test_head_변경되면_타이머가_리셋된다():
    """점진 push 납치 방지 — 새 head는 새 독려 마커를 요구(기존 독려는 승격 근거 아님)."""
    card = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                          "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}])
    acts = dispatchd.decide(snap([card], [pr(sha="c" * 40)]))
    assert not [a for a in acts if a["action"] == "submission-adopt"]
    r = [a for a in acts if a["action"] == "submit-remind"]
    assert len(r) == 1 and f"#3/{'c' * 8}" in r[0]["marker"]


def test_유효_임대나_활동_중이면_승격하지_않는다():
    remind = [{"author": "probe", "id": 1, "ts": TS_OLD,
               "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}]
    held = _mk(comments=remind, lease_by="claude@workstation",
               lease_expires="2026-10-12T05:29:18+0900")
    assert not [a for a in dispatchd.decide(snap([held], [GREEN]))
                if a["action"] == "submission-adopt"]
    active = _mk(comments=remind + [{"author": "codex", "id": 5,
                                     "ts": "2026-10-11T12:00:00+0900",
                                     "body": "보고 작성 중입니다."}])
    assert not [a for a in dispatchd.decide(snap([active], [GREEN]))
                if a["action"] == "submission-adopt"]


# --- execute: 승격 ---

def _rec(card, agents=()):
    class Rec:
        def __init__(self):
            self.commented, self.patched, self.messages, self.dispatches = [], [], [], []

        def __call__(self, url, path, method="GET", body=None):
            if method == "GET" and path.endswith("/agents"):
                return [dict(a) for a in agents]
            if method == "GET":
                return dict(card)
            if method == "PATCH":
                self.patched.append(body)
                return dict(card)
            if path == "/messages" and method == "POST":
                self.messages.append(body)
                return {"ok": True}
            if path.endswith("/dispatch") and method == "POST":
                self.dispatches.append(body)
                return {"ok": True}
            self.commented.append(body)
            return {"ok": True}
    return Rec()


def _monkey_gh(monkeypatch, sha=f"{SHA8}"):
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: sha)


def test_승격_execute는_review로_옮기고_사유를_기록한다(monkeypatch):
    card = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                          "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}])
    rec = _rec(card)
    monkeypatch.setattr(probe_core, "api", rec)
    _monkey_gh(monkeypatch)
    probe_core.execute("u", {"agent": "probe", "issue": CARD, "action": "submission-adopt",
                             "marker": f"[submission-adopt a1 #3/{SHA8}]", "att": 1,
                             "pr": 3, "repo": "plainOldCode/think-tank",
                             "head_sha": SHA8, "remind_ts": TS_OLD, "remind_id": 1,
                             "expected_version": 7})
    assert {"state": "review", "expected_version": 7} in rec.patched
    assert any("[submission-adopt a1 #3/%s]" % SHA8 in (c.get("body") or "")
               for c in rec.commented if c.get("author") == "probe")


def test_승격_execute는_직전_상태를_재확인한다(monkeypatch):
    """decide 이후 회차 승격·임대·활동이 생기면 폐기."""
    remind = [{"author": "probe", "id": 1, "ts": TS_OLD,
               "body": f"[submit-remind a1 #3/{SHA8}] 완료 보고 제출"}]
    act = {"agent": "probe", "issue": CARD, "action": "submission-adopt",
           "marker": f"[submission-adopt a1 #3/{SHA8}]", "att": 1, "pr": 3,
           "repo": "plainOldCode/think-tank", "head_sha": SHA8,
           "remind_ts": TS_OLD, "remind_id": 1, "expected_version": 7}
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: SHA8)
    # 새 워커 수령(attempt 2)
    rec = _rec(_mk(comments=remind, att=2))
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", dict(act))
    assert not rec.patched
    # decide 이후 워커 응답
    rec2 = _rec(_mk(comments=remind + [{"author": "codex", "id": 9,
                                        "ts": "2026-10-11T09:00:00+0900",
                                        "body": "곧 보고합니다."}]))
    monkeypatch.setattr(probe_core, "api", rec2)
    probe_core.execute("u", dict(act))
    assert not rec2.patched
    # 실제 head 변경
    rec3 = _rec(_mk(comments=remind))
    monkeypatch.setattr(probe_core, "api", rec3)
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: "c" * 8)
    probe_core.execute("u", dict(act))
    assert not rec3.patched


# --- execute: 독려(보드 릴레이 플래그) ---

def test_독려는_등록되지_않은_assignee에게_보드_릴레이를_남긴다(monkeypatch):
    card = _mk(assignee="ghost@nowhere")
    rec = _rec(card)
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", {"agent": "probe", "issue": CARD, "action": "submit-remind",
                             "marker": f"[submit-remind a1 #3/{SHA8}]", "att": 1,
                             "pr": 3, "repo": "plainOldCode/think-tank", "head_sha": SHA8})
    assert any("[submit-remind a1 #3/%s]" % SHA8 in (c.get("body") or "")
               for c in rec.commented if c.get("author") == "probe")
    assert rec.messages and "완료 보고 제출 요망" in rec.messages[0]["body"]


# --- 병합 후 무보고: 독려 1회 → 타이머 경과 → needs-human (JXYS R1) ---

def _merged_card(comments, iid="M3EV-0001", att=1):
    return issue(iid, state="review", att=att, comments=comments)


MERGE_NB = {"author": "probe", "id": 10, "ts": TS_OLD,
            "body": "probe: PR#9 merged — 완료 보고 없음. review 유지"}


def test_무보고_병합은_독려_1회다():
    acts = dispatchd.decide(snap([_merged_card([MERGE_NB])]))
    r = [a for a in acts if a["action"] == "review-note" and "재제출" in a.get("reason", "")]
    assert len(r) == 1
    # 독려 마커 존재 → 중복 독려 없음(타이머 평가로 넘어감)
    card2 = _merged_card([MERGE_NB, {"author": "probe", "id": 11, "ts": TS_OLD,
                                     "body": "[evidence-rejected a1] 무보고 병합 — 재제출 요청"}])
    acts2 = dispatchd.decide(snap([card2]))
    assert not [a for a in acts2 if a["action"] == "review-note"
                and "재제출" in a.get("reason", "")]


def test_무보고_병합_독려_후_타이머_경과시_needs_human이다():
    card = _merged_card([MERGE_NB, {"author": "probe", "id": 11, "ts": TS_OLD,
                                    "body": "[evidence-rejected a1] 무보고 병합 — 재제출 요청"}])
    nh = [a for a in dispatchd.decide(snap([card]))
          if a["action"] == "needs-human"]
    assert nh and "evidence_rejected" in nh[0]["note"]


def test_독려_후_활동_중이면_승격하지_않는다():
    card = _merged_card([MERGE_NB, {"author": "probe", "id": 11, "ts": TS_OLD,
                                    "body": "[evidence-rejected a1] 무보고 병합 — 재제출 요청"},
                         {"author": "codex", "id": 12, "ts": "2026-10-11T12:00:00+0900",
                          "body": "보고 작성 중입니다."}])
    assert not [a for a in dispatchd.decide(snap([card]))
                if a["action"] == "needs-human"]


def test_병합_후_보고_접수되면_독려하지_않는다():
    card = _merged_card([MERGE_NB, {"author": "tt-server", "id": 12,
                                    "ts": "2026-10-11T12:00:00+0900",
                                    "body": "보고 접수 — 완료"}])
    acts = dispatchd.decide(snap([card]))
    assert not [a for a in acts if a["action"] in ("review-note", "needs-human")
                and "재제출" in (a.get("reason") or "") + (a.get("note") or "")]


def test_done_거부도_독려_1회_타이머_승격이다():
    card = _merged_card([{"author": "tt-server", "id": 1, "ts": TS_OLD,
                          "body": "done 증거 없음 또는 실패/미확정 보고"}])
    acts = dispatchd.decide(snap([card]))
    assert [a for a in acts if a["action"] == "review-note"
            and "재제출" in a.get("reason", "")]


# --- R2: ci-fix execute가 결정된 ip 마커를 저장 — 다음 decide 재판정 없음 ---

def test_ci_fix_execute는_결정된_ip_마커를_저장한다(monkeypatch):
    rec = _rec(_mk())
    monkeypatch.setattr(probe_core, "api", rec)
    monkeypatch.setenv("TT_CI_FIX_AGENT", "kanban-adapter")
    probe_core.execute("u", {"agent": "dispatchd", "issue": CARD, "action": "ci-fix",
                             "marker": f"[ci-fix-ip a1 #3/{SHA8}]", "att": 1, "pr": 3,
                             "repo": "plainOldCode/think-tank", "branch": "tt/x",
                             "head_sha": SHA8, "checks_failed": ["tests"]})
    assert any(f"[ci-fix-ip a1 #3/{SHA8}]" in (c.get("body") or "")
               for c in rec.commented if c.get("author") == "probe")
    assert rec.dispatches  # 위임은 발생


def test_ci_fix_ip_왕복은_다음_판정을_막는다():
    """decide(ci-fix-ip) → execute(저장) → decide — 실제 왕복(R2 재현)."""
    card1 = _mk()
    acts = dispatchd.decide(snap([card1], [FAIL]))
    assert [a for a in acts if a.get("action") == "ci-fix"]
    stored = _mk(comments=[{"author": "probe", "id": 1, "ts": TS_NOW,
                            "body": f"[ci-fix-ip a1 #3/{SHA8}] PR#3 CI 실패 — 위임"}])
    assert not [a for a in dispatchd.decide(snap([stored], [FAIL]))
                if a.get("action") == "ci-fix"]


# --- R3: in_progress 카드의 동적 repo가 실제 수집 풀에 들어가는가 ---

def test_in_progress_카드의_repo가_수집_풀에_있다():
    card = issue("M3REPO-01", body="repo: example-org/new-project")
    repos = probe_core.collect_repos([card])
    assert "example-org/new-project" in repos


# --- R4: 게이트 unset 상태의 실제 run_once에서도 코멘트가 로드되어 승격된다 ---

def test_게이트_끈_run_once도_독려를_하이드레이트해_승격한다(monkeypatch):
    card = _mk()
    del card["comments"]  # snapshot은 비-review 카드 코멘트를 아예 안 담는다
    prs = [GREEN]
    monkeypatch.delenv("TT_REVIEW_AGENT", raising=False)
    monkeypatch.setattr(probe_core, "snapshot",
                        lambda url: {"auto": True, "now": TS_NOW, "agents": [],
                                     "issues": [card], "probe_skips": {}})
    monkeypatch.setattr(probe_core, "collect_prs", lambda repos: prs)
    monkeypatch.setattr(probe_core, "execute",
                        lambda url, a: exec_acts.append(a))
    exec_acts = []
    monkeypatch.setattr(probe_core, "api",
                        lambda url, path, method="GET", body=None: (
                            dict(_mk(comments=[{"author": "probe", "id": 1, "ts": TS_OLD,
                                                "body": f"[submit-remind a1 #3/{SHA8}] 독려"}]))
                            if method == "GET" else {"ok": True}))
    probe_core.run_once("u")
    assert [a for a in exec_acts if a["action"] == "submission-adopt"]


# --- R1 잔여: 독려 액션이 execute에서 실제로 저장된다(마커 미주입) ---

def test_무보고_병합_독려_실제_집행(monkeypatch):
    c = _merged_card([MERGE_NB])
    rec = _rec(c)
    monkeypatch.setattr(probe_core, "api", rec)
    a = next(a for a in probe_core.decide(snap([c]))
             if a.get("marker") == "[evidence-rejected a1]")
    probe_core.execute("u", a)
    assert any("[evidence-rejected a1]" in (x.get("body") or "")
               for x in rec.commented if x.get("author") == "probe")


def test_무보고_병합_실제_왕복_저장_후_승격(monkeypatch):
    """merge→decide→execute→시간 경과→decide — 마커 주입 없는 실제 흐름."""
    c = _merged_card([MERGE_NB])
    rec = _rec(c)
    monkeypatch.setattr(probe_core, "api", rec)
    a = next(a for a in probe_core.decide(snap([c]))
             if a.get("marker") == "[evidence-rejected a1]")
    probe_core.execute("u", a)
    # 저장된 카드 그대로(서버가 ts/id를 채워 주는 것을 재현) — 타이머 미경과: 중복 독려 없음
    stored = dict(c)
    stored["comments"] = [MERGE_NB] + [dict(x, id=20 + n, ts=TS_NOW) for n, x in
                                       enumerate(x for x in rec.commented
                                                 if x.get("author") == "probe")]
    snap2 = snap([stored])
    snap2["now"] = TS_NOW
    assert not [a2 for a2 in probe_core.decide(snap2)
                if a2.get("action") == "review-note" and "재제출" in (a2.get("reason") or "")]
    # 동일 카드, snapshot now만 전진(8.5h) — 저장된 독려 시각 기준 타이머 경과 → 승격
    snap3 = snap([stored])
    snap3["now"] = "2026-10-12T01:00:00+0900"
    nh = [a2 for a2 in probe_core.decide(snap3) if a2.get("action") == "needs-human"]
    assert nh and "evidence_rejected" in nh[0]["note"]
