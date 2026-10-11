"""BG97(M4MGV3B7) — 재작업 전달 보장: [fix-stall] 소비 + relay 마커 + 무반응 양도."""
import pytest

import dispatchd
from probe import core as probe_core

SHA8 = "b" * 8
CARD = "M428RMBY-XC0K"
TS_RELAY = "2026-10-04T09:00:00+0900"     # relay 마커 시각
NOW = "2026-10-04T16:30:00+0900"          # 7.5h 후 — relay(6h) 타이머 경과
STALL = {"author": "tt-embedding-daemon", "id": 50, "ts": "2026-10-04T10:00:00+0900",
         "body": f"[fix-stall] src_kind=review-fix src_att=- src_pr=9 src_head={SHA8} "
                 "attempt=1 minutes=47 kind=no-progress — review-fix 재발령 검토(BG97)"}


def _issue(state="review", iid=CARD, attempt=1, comments=(), assignee="a@t", **kw):
    base = {"id": iid, "state": state, "assignee": assignee, "version": 3,
            "lease_expires": None, "lease_by": None, "labels": [], "priority": None,
            "execution_attempt": attempt, "dispatches": 1, "release_ready": False,
            "waiting_for": None, "busy_agents": [],
            "work_contract": {"version": "v2"}, "comments": list(comments),
            "updated_at": "2026-10-04T09:00:00+0900", "title": iid}
    base.update(kw)
    return base


def _snap(issues, prs=()):
    return {"auto": True, "now": NOW, "agents": [], "issues": list(issues),
            "prs": list(prs)}


def _pr(number=9, sha="b" * 40, head_sha=None):
    return [{"number": number, "repo": "plainOldCode/think-tank",
             "checks": [{"state": "SUCCESS"}], "branch": f"tt/{CARD}-x",
             "title": "", "head_sha": head_sha or sha}]


def _fx_marker(att=None):
    return f"[review-fix #9/{SHA8}]"


class Rec:
    def __init__(self, card, agents=(), dispatch_fail=False):
        self.card = card
        self.agents = list(agents)
        self.dispatches = []
        self.commented = []
        self.patched = []
        self.messages = []
        self.fail = dispatch_fail

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET" and path.endswith("/agents"):
            return [dict(a) for a in self.agents]
        if method == "GET":
            return dict(self.card)
        if method == "PATCH":
            self.patched.append(body)
            return {**self.card, "state": body.get("state")}
        if path == "/messages" and method == "POST":
            self.messages.append(body)
            return {"ok": True}
        if path.endswith("/dispatch") and method == "POST":
            if self.fail:
                raise RuntimeError("no receiver")
            self.dispatches.append(body)
            return {"id": 77, "ok": True}
        self.commented.append(body)
        return {"ok": True}


def _review_fix_act(att=1):
    return {"agent": "probe", "issue": CARD, "action": "review-fix", "pr": 9,
            "repo": "plainOldCode/think-tank", "branch": "tt/x", "head_sha": SHA8,
            "marker": _fx_marker()}


# --- 1순위: 미등록 assignee → relay 마커 + 보드 플래그, 할당 유지 ---

def test_미등록_assignee는_relay_마커와_보드_플래그를_남긴다(monkeypatch):
    card = {"id": CARD, "state": "in_progress", "version": 5, "assignee": "ghost@nowhere",
            "execution_attempt": 1, "comments": []}
    rec = Rec(card, agents=[])
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", _review_fix_act(att=1))
    relayed = [c for c in rec.commented if c.get("author") == "probe"
               and f"[rework-relay #9/{SHA8} a1]" in (c.get("body") or "")]
    assert relayed and "세션 전달 요망" in relayed[0]["body"]
    assert rec.messages and "[rework-relay]" in rec.messages[0]["body"]
    assert not any("assignee" in (p or {}) for p in rec.patched)  # 할당 유지


# --- [fix-stall] 소비: 위임 후 미실행 정체 재시도 ---

def test_fix_stall을_소비해_원_워커를_재발령한다(monkeypatch):
    """PR#93 발단 — [review-fix] 위임 후 dispatch 미실행 → [fix-stall] → 재발령."""
    card = _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} PR#9 request-changes — review 반납+수정 위임"},
        STALL])
    acts = dispatchd.decide(_snap([card], _pr()))
    r = [a for a in acts if a["action"] == "review-fix-relay"]
    assert len(r) == 1 and r[0]["pr"] == 9 and r[0]["att"] == 1
    card = card | {"assignee": "opencode-tp13"}  # 정확명 — 가족 해석 경로는 R4 테스트가 담당
    rec = Rec(card, agents=[{"name": "opencode-tp13", "enabled": True,
                             "base_url": "http://x"}])
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", dict(r[0]))
    assert rec.dispatches[0]["agent"] == "opencode-tp13"
    assert any("[rework-relay #9/%s a1]" % SHA8 in (c.get("body") or "")
               and "재발령" in (c.get("body") or "")
               for c in rec.commented if c.get("author") == "probe")


def test_소비_후_동일_회차는_재시도하지_않는다(monkeypatch):
    """(사유,pr,sha,attempt) dedup — relay 마커 존재 시 재발령 액션 없음."""
    card = _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} 위임"},
        {"author": "probe", "id": 2, "ts": "2026-10-04T10:05:00+0900",
         "body": f"[rework-relay #9/{SHA8} a1] 원 워커 재발령 — opencode-tp13 세션 실행 중 (dispatch#77)"},
        STALL])
    acts = dispatchd.decide(_snap([card], _pr()))
    assert not [a for a in acts if a["action"] == "review-fix-relay"]


def test_과거_head의_stall은_소비하지_않는다(monkeypatch):
    stale = dict(STALL, body=STALL["body"].replace(SHA8, "c" * 8)
                 .replace(f"src_pr=9", "src_pr=9"))
    card = _issue(comments=[{"author": "probe", "id": 1, "ts": TS_RELAY,
                             "body": f"{_fx_marker()} 위임"}, stale])
    acts = dispatchd.decide(_snap([card], _pr()))
    assert not [a for a in acts if a["action"] == "review-fix-relay"]


# --- 2순위: relay 무반응 양도 ---

def _relayed_card(extra=(), att=1, relay_ts=TS_RELAY, **kw):
    return _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} PR#9 request-changes — review 반납 — 수정 위임 없음"},
        {"author": "probe", "id": 2, "ts": relay_ts,
         "body": f"[rework-relay #9/{SHA8} a{att}] 수정 dispatch 실패(에이전트 미등록: a@t) — "
                 "사람 판단 대기\n세션 전달 요망"}, *extra], **kw)


def test_relay_무반응_타이머_경과시_todo_회귀한다(monkeypatch):
    card = _relayed_card()
    acts = dispatchd.decide(_snap([card], _pr()))
    fb = [a for a in acts if a["action"] == "rework-fallback"]
    assert len(fb) == 1 and fb[0]["att"] == 1
    rec = Rec(card)
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", dict(fb[0]))
    assert any(p.get("state") == "todo" and p.get("assignee") == ""
               for p in rec.patched)
    assert any("[rework-fallback a1]" in (c.get("body") or "")
               for c in rec.commented if c.get("author") == "probe")


def test_relay_후_활동이_있으면_침묵한다(monkeypatch):
    card = _relayed_card(extra=[{"author": "codex", "id": 9,
                                 "ts": "2026-10-04T12:00:00+0900",
                                 "body": "수정 진행 중입니다."}])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "rework-fallback"]


def test_유효_임대_카드는_양도하지_않는다(monkeypatch):
    """Q05B 가드 — claude 활동 중(유효 임대+하트비트) 카드는 무반응이 아니다."""
    card = _relayed_card(lease_by="claude@workstation",
                         lease_expires="2026-10-12T05:29:18+0900")
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "rework-fallback"]


def test_head_변경되면_양도하지_않는다(monkeypatch):
    card = _relayed_card(extra=[{"author": "probe", "id": 9,
                                 "ts": "2026-10-04T12:00:00+0900",
                                 "body": f"[review-fix #9/{'c' * 8}] 새 head 재위임"}])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "rework-fallback"]


def test_fallback_3회면_needs_human로_승격한다(monkeypatch):
    card = _relayed_card(att=4, extra=[
        {"author": "probe", "id": 7, "ts": "2026-10-04T07:00:00+0900",
         "body": "[rework-fallback a1] 원 워커 무반응 — assignee 해제+todo 회귀"},
        {"author": "probe", "id": 8, "ts": "2026-10-04T08:00:00+0900",
         "body": "[rework-fallback a2] 원 워커 무반응 — assignee 해제+todo 회귀"},
        {"author": "probe", "id": 9, "ts": "2026-10-04T09:00:00+0900",
         "body": "[rework-fallback a3] 원 워커 무반응 — assignee 해제+todo 회귀"}])
    acts = dispatchd.decide(_snap([card], _pr()))
    assert not [a for a in acts if a["action"] == "rework-fallback"]
    nh = [a for a in acts if a["action"] == "needs-human"]
    assert nh and "rework_relay" in nh[0]["note"]


def test_타이머_미경과는_침묵한다(monkeypatch):
    card = _relayed_card(relay_ts="2026-10-04T15:00:00+0900")  # 1.5h — relay(6h) 미경과
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "rework-fallback"]
