"""BG97(M4MGV3B7) — 재작업 전달 보장: [fix-stall] 소비 + relay 마커 + 무반응 양도."""
import pytest
from fastapi.testclient import TestClient

import service
from app import create_app
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
        self.runs = []  # 진행 중 dispatch 스냅샷 (M5283HM4-0SZP JWW0 테스트용)
        self.dispatches = []
        self.commented = []
        self.patched = []
        self.messages = []
        self.fail = dispatch_fail

    def __call__(self, url, path, method="GET", body=None):
        if method == "GET" and path.endswith("/agents"):
            return [dict(a) for a in self.agents]
        if path.endswith("/dispatches") and method == "GET":
            return list(self.runs)
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
                             "base_url": "http://x", "receive_mode": "hook"}])
    monkeypatch.setattr(probe_core, "api", rec)
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: SHA8)
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


# --- [rework-stuck] 소비(4J4S) — dispatch 실패의 review-fix 재라우팅 ---

RWS = {"author": "tt-embedding-daemon", "id": 60, "ts": "2026-10-04T10:00:00+0900",
       "body": f"[rework-stuck] src_kind=review-fix src_att=- src_pr=9 src_head={SHA8} "
               "attempt=1 target=a@t kind=404 — review-fix dispatch 실패 재라우팅 검토"}


def _rws_with_fx(**kw):
    return _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} PR#9 request-changes — review 반납+수정 위임"}, RWS], **kw)


def test_rework_stuck_round_trip_무손실():
    # 설계 §3-2 — probe 마커 원문 ↔ src_* 필드 역파싱 무손실
    for orig in ("[review-fix #93/c661baaa]", "[ci-fix a1 #93/ed574468]"):
        fs = probe_core._probe_marker_to_src(orig)
        assert fs == {"src_kind": orig.strip("[]").split()[0],
                      "src_att": orig.strip("[]").split()[1] if " a" in orig else "-",
                      "src_pr": orig.strip("[]").split("#")[1].split("/")[0],
                      "src_head": orig.strip("[]").split("/")[1].rstrip("]")}
        assert probe_core._rework_src_marker(fs) == orig


def test_rework_stuck을_소비해_재라우팅한다():
    acts = dispatchd.decide(_snap([_rws_with_fx()], _pr()))
    r = [a for a in acts if a["action"] == "review-fix-relay"]
    assert len(r) == 1 and r[0]["pr"] == 9 and r[0]["att"] == 1
    assert r[0].get("rs_kind") == "404"


def test_rework_stuck_동일_이벤트는_재발화_0():
    # relay 마커((pr,sha,att)당 1회) 존재 시 같은 회차 이벤트 재소비 없음
    card = _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} 위임"},
        {"author": "probe", "id": 2, "ts": "2026-10-04T10:05:00+0900",
         "body": f"[rework-relay #9/{SHA8} a1] 원 워커 재발령 (dispatch#77)"},
        RWS])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "review-fix-relay"]


def test_rework_stuck_과거_head_미소비():
    stale = dict(RWS, body=RWS["body"].replace(SHA8, "c" * 8))
    card = _issue(comments=[{"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
                             "body": f"{_fx_marker()} 위임"}, stale])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "review-fix-relay"]


def test_rework_stuck_과거_회차_미소비():
    stale = dict(RWS, body=RWS["body"].replace("attempt=1", "attempt=2"))
    card = _issue(comments=[{"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
                             "body": f"{_fx_marker()} 위임"}, stale])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "review-fix-relay"]


def test_rework_stuck_유효_임대면_재발령_금지():
    card = _rws_with_fx(lease_by="a@t", lease_expires="2026-10-04T23:00:00+0900")
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "review-fix-relay"]


def test_rework_stuck_위임_원문_부재면_소비_안한다():
    # src_* 역파싱 원문([review-fix #pr/sha])이 카드에 없음 — 근거 없는 마커
    assert not [a for a in dispatchd.decide(_snap([_issue(comments=[RWS])], _pr()))
                if a["action"] == "review-fix-relay"]


def test_rework_stuck_스키마밖_kind는_무시():
    odd = dict(RWS, body=RWS["body"].replace("kind=404", "kind=no-progress"))
    card = _issue(comments=[{"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
                             "body": f"{_fx_marker()} 위임"}, odd])
    assert not [a for a in dispatchd.decide(_snap([card], _pr()))
                if a["action"] == "review-fix-relay"]


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
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: SHA8)
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
    card = _relayed_card(att=4, execution_attempt=4, extra=[
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


# --- 리뷰 경계 계약(PR#95 리뷰 R1~R6) — 소비자 재확인 의무(docs §2) ---

def _actions(card, prs=None):
    return [a for a in dispatchd.decide(_snap([card], _pr() if prs is None else prs))]


def test_old_attempt_stall_is_not_retagged_as_current_attempt():
    """R1 — 과거 회차의 [fix-stall]을 현재 attempt로 재태깅하지 않는다."""
    card = _issue(attempt=2, assignee="new-worker", comments=[STALL])
    assert not [a for a in _actions(card) if a["action"] == "review-fix-relay"]


def test_old_attempt_relay_cannot_unassign_new_worker():
    """R1 — 과거 회차 relay 마커는 새 워커(attempt 승격)를 양도하지 않는다."""
    card = _relayed_card(execution_attempt=2, assignee="new-worker")
    assert not [a for a in _actions(card) if a["action"] == "rework-fallback"]


def test_actual_head_change_without_a_probe_marker_blocks_fallback():
    """R1 — head 변경 후 probe 코멘트가 없어도 구 head relay로 회귀하지 않는다."""
    card = _relayed_card()
    assert not [a for a in _actions(card, _pr(sha="c" * 40))
                if a["action"] == "rework-fallback"]


def test_runner_failure_is_not_permanent_worker_activity():
    """R5 — runner 실패 기록은 워커 활동이 아니다 — 양도가 영구 차단되지 않는다."""
    card = _relayed_card(extra=[{
        "author": "runner@host-a", "id": 9,
        "ts": "2026-10-04T10:00:00+0900",
        "body": "runner:host-a dispatch#77 failed exit=1 session=dead",
    }])
    assert [a for a in _actions(card) if a["action"] == "rework-fallback"]


def test_stall_cannot_redispatch_an_active_local_lease():
    """R2 — 유효 임대 로컬 워커(claude)는 [fix-stall]로 재발령되지 않는다."""
    card = _issue(comments=[STALL], lease_by="claude",
                  lease_expires="2026-10-05T00:00:00+0900")
    assert not [a for a in _actions(card) if a["action"] == "review-fix-relay"]


def test_new_approval_makes_an_old_stall_ineligible_for_fix(monkeypatch):
    """R2 — stall 이후 같은 head에 approve 판정이 기록되면 재발령 자격이 없다."""
    monkeypatch.setenv("TT_REVIEW_AGENT", "codex")
    card = _issue(comments=[STALL, {
        "author": "codex", "id": 51, "ts": "2026-10-04T11:00:00+0900",
        "body": f"review: approve\nPR#9@{SHA8}\nindependent review passed",
    }])
    assert not [a for a in _actions(card) if a["action"] == "review-fix-relay"]


def test_blocked_for_human_decision_is_not_released_to_todo():
    """R2 — blocked(사람 판단 대기) 카드는 양도 회귀로 되돌리지 않는다."""
    card = _relayed_card(state="blocked", waiting_for="human",
                         blocked_detail="scope approval required")
    assert not [a for a in _actions(card) if a["action"] == "rework-fallback"]


def test_execute_fallback_rechecks_a_new_lease(monkeypatch):
    """R3 — execute 시점에 새 임대·회차가 보이면 PATCH하지 않는다."""
    act = next(a for a in _actions(_relayed_card())
               if a["action"] == "rework-fallback")
    fresh = _relayed_card(execution_attempt=2, state="in_progress",
                          assignee="claude", lease_by="claude",
                          lease_expires="2099-10-05T00:00:00+0900")
    rec = Rec(fresh)
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("mock", act)
    assert not rec.patched


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    monkeypatch.setenv("TT_PROBE_INTERVAL", "0")
    monkeypatch.setenv("TT_NOTIFY_WORKER", "0")
    with TestClient(create_app(str(tmp_path / "tt.db"))) as client:
        def api(url, path, method="GET", body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()
        monkeypatch.setattr(probe_core, "api", api)
        yield client


def _make_card(client):
    card = client.post("/issues", json={"title": "local review race",
        "acceptance": "independent review checks", "state": "todo"}).json()
    client.post(f"/issues/{card['id']}/claim", json={"agent": "opencode-exact"})
    client.patch(f"/issues/{card['id']}", json={"state": "review"})
    return client.get(f"/issues/{card['id']}").json()


def _relay_action(card):
    return {"agent": "probe", "issue": card["id"], "action": "review-fix-relay",
            "pr": 9, "repo": "plainOldCode/think-tank", "branch": "tt/test",
            "head_sha": "b" * 40, "att": card["execution_attempt"]}


def _register(client, name, url):
    response = client.post("/agents", json={"name": name, "base_url": url})
    assert response.status_code == 201


def test_real_api_hook_error_is_human_relay_not_executing(local_api, monkeypatch):
    """R4 — hook 전달 실패(status=error)는 '세션 실행 중'으로 기록하지 않는다."""
    card = _make_card(local_api)
    _register(local_api, "opencode-exact", "http://localhost.invalid/exact")
    monkeypatch.setattr(service, "deliver", lambda *args: (500, ""))
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: None or SHA8)
    probe_core.execute("local", _relay_action(card))
    comments = local_api.get(f"/issues/{card['id']}").json()["comments"]
    relay = next(c["body"] for c in comments if c["author"] == "probe"
                 and "[rework-relay #" in c["body"])
    assert "사람 세션 전달 요망" in relay, relay


def test_exact_hook_failure_retries_an_available_family(local_api, monkeypatch):
    """R4 — 정확명 hook 실패 후 실행 가능한 같은 가족 수신처를 재시도한다."""
    card = _make_card(local_api)
    _register(local_api, "opencode-exact", "http://localhost.invalid/exact")
    _register(local_api, "opencode-other", "http://localhost.invalid/family")
    delivered = []
    def deliver(url, secret, payload):
        delivered.append(payload["agent"])
        return (500, "") if url.endswith("exact") else (200, "")
    monkeypatch.setattr(service, "deliver", deliver)
    monkeypatch.setattr(probe_core, "_gh_pr_head", lambda repo, n: SHA8)
    probe_core.execute("local", _relay_action(card))
    assert delivered == ["opencode-exact", "opencode-other"], delivered


def test_real_api_fallback_cannot_clear_a_lease_acquired_after_decide(local_api):
    """R3 — decide 이후 수령된 임대를 fallback이 지우지 않는다(실제 API)."""
    card = _make_card(local_api)
    act = {"agent": "probe", "issue": card["id"], "action": "rework-fallback",
           "att": 1, "marker": "[rework-fallback a1]"}
    local_api.patch(f"/issues/{card['id']}", json={"state": "todo"})
    local_api.post(f"/issues/{card['id']}/claim", json={"agent": "claude"})
    before = local_api.get(f"/issues/{card['id']}").json()
    assert before["lease_by"] == "claude" and before["execution_attempt"] == 2
    probe_core.execute("local", act)
    after = local_api.get(f"/issues/{card['id']}").json()
    assert after["state"] == "in_progress" and after["lease_by"] == "claude", after


def test_human_unblock_resets_rework_relay_suppression():
    """R6 — 사람 [unblocked] 후 카운터는 해제 시점 이후만 재집계한다(§6 공용 계약)."""
    scope = f"#9/{SHA8}"
    comments = [{"author": "probe", "id": n, "ts": "2026-10-04T08:00:00+0900",
                 "body": f"[rework-fallback a{n}] old failure"} for n in (7, 8, 9)]
    comments += [{"author": "dispatchd", "id": 10, "ts": "2026-10-04T08:00:00+0900",
                  "body": f"[escalated (rework_relay, {scope})] human intervention"},
                 {"author": "human", "id": 11, "ts": "2026-10-04T08:30:00+0900",
                  "body": f"[unblocked (rework_relay, {scope})] retry allowed"}]
    card = _relayed_card(att=4, execution_attempt=4, extra=comments)
    assert [a for a in _actions(card) if a["action"] == "rework-fallback"]


def test_gh_head_조회_성공시_relay가_실제_진행된다(monkeypatch):
    """codex 재리뷰 — gh_json 파싱 경로 유지 + 성공 조회 시 dispatch 발생."""
    card = _issue(comments=[
        {"author": "probe", "id": 1, "ts": "2026-10-04T08:00:00+0900",
         "body": f"{_fx_marker()} 위임"},
        {"author": "tt-embedding-daemon", "id": 2, "ts": "2026-10-04T10:00:00+0900",
         "body": f"[fix-stall] src_kind=review-fix src_att=- src_pr=9 src_head={SHA8} "
                 "attempt=1 minutes=47 kind=no-progress — x"}],
        assignee="opencode-tp13")
    acts = dispatchd.decide(_snap([card], _pr()))
    r = [a for a in acts if a["action"] == "review-fix-relay"]
    assert len(r) == 1
    rec = Rec(card, agents=[{"name": "opencode-tp13", "enabled": True,
                             "base_url": "http://x", "receive_mode": "hook"}])
    monkeypatch.setattr(probe_core, "api", rec)
    monkeypatch.setattr(probe_core, "gh_json",
                        lambda *a: {"headRefOid": "b" * 40})  # CLI transport 모의
    probe_core.execute("u", dict(r[0]))
    assert rec.dispatches and rec.dispatches[0]["agent"] == "opencode-tp13"


def test_gh_head_조회_실패시_폐기한다(monkeypatch):
    card = _issue(comments=[
        {"author": "probe", "id": 1, "ts": TS_RELAY,
         "body": f"[rework-relay #9/{SHA8} a1] 원 워커 재발령 불가 — 사람 세션 전달 요망"}])
    acts = dispatchd.decide(_snap([card], _pr()))
    fb = [a for a in acts if a["action"] == "rework-fallback"]
    assert len(fb) == 1
    rec = Rec(card)
    monkeypatch.setattr(probe_core, "api", rec)
    monkeypatch.setattr(probe_core, "gh_json",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("gh 실패")))
    probe_core.execute("u", dict(fb[0]))
    assert not rec.patched  # 조회 불가 — 회귀 폐기


# --- M5283HM4-0SZP: 원작업자 고정 해제 — 등록 실행자 중 대체 선정 ---

def _agents_pool():
    return [
        {"name": "kanban-adapter", "tier": "exec", "enabled": True, "base_url": "http://k", "receive_mode": "hook"},
        {"name": "codex", "tier": "impl", "enabled": True, "base_url": "http://c", "receive_mode": "hook"},
        {"name": "cursor", "tier": "exec", "enabled": True, "base_url": "http://u", "receive_mode": "hook"},
        {"name": "agy", "tier": "sota", "enabled": True, "base_url": "http://a", "receive_mode": "hook"},
    ]


def _verdict_card(assignee, **kw):
    """request-changes 판정 + 이전 보고가 붙은 review 카드."""
    return _issue(state="review", assignee=assignee,
                  comments=[{"author": "agy", "id": 9, "ts": "2026-10-16T22:00:00+0900",
                             "body": f"review: request-changes\nTT#{CARD}@a1/x\n\n마이그레이션 누락"}],
                  completion_report="이전 보고: 979 passed", **kw)


def test_미등록_assignee는_등록_실행자에게_새_dispatch로_재위임된다(monkeypatch):
    """review-fix 404 낙하 대신 — 미해석 assignee는 impl 밴드 실행자에게 통합 brief로."""
    card = _verdict_card("skshim@skshimui-MacBookPro")
    rec = Rec(card, agents=_agents_pool())
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", _review_fix_act(att=1))
    assert rec.dispatches and rec.dispatches[0]["agent"] == "codex"
    body = rec.dispatches[0]["message"]
    assert "request-changes" in body and "이전 보고" in body and "pull/9" in body
    assert not any("[rework-relay" in (c.get("body") or "") for c in rec.commented)
    assert not rec.messages  # 사람 통보 없음 — 자동 재위임 성공
    assert not any("assignee" in (p or {}) for p in rec.patched)  # 할당 유지
    assert any("codex" in (c.get("body") or "") for c in rec.commented
               if c.get("author") == "probe")  # 재위임 기록


def test_실행불가_assignee는_동일_tier_실행자가_우선한다(monkeypatch):
    """정확명이 non-executor면 원 tier(exec) 실행자 → impl 순 — 어댑터 회귀 금지."""
    card = _verdict_card("kanban-adapter")
    rec = Rec(card, agents=_agents_pool())
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", _review_fix_act(att=1))
    assert rec.dispatches and rec.dispatches[0]["agent"] == "cursor"


def test_진행중_dispatch_보유_후보는_건너뛴다(monkeypatch):
    """JWW0 동일 판정 — 대체 후보도 이 카드에서 진행 중이면 다음 후보."""
    card = _verdict_card("kanban-adapter")
    rec = Rec(card, agents=_agents_pool())
    rec.runs = [{"agent": "cursor", "run_state": "running"}]
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", _review_fix_act(att=1))
    assert rec.dispatches and rec.dispatches[0]["agent"] == "codex"


def test_대체_후보가_없으면_기존대로_사람_통보한다(monkeypatch):
    """실행자 전원 부재 — relay 마커+보드 플래그 유지(하위 호환)."""
    card = _verdict_card("skshim@skshimui-MacBookPro")
    rec = Rec(card, agents=[{"name": "kanban-adapter", "tier": "exec",
                             "enabled": True, "base_url": "http://k", "receive_mode": "hook"}])
    monkeypatch.setattr(probe_core, "api", rec)
    probe_core.execute("u", _review_fix_act(att=1))
    assert not rec.dispatches
    relayed = [c for c in rec.commented if c.get("author") == "probe"
               and f"[rework-relay #9/{SHA8} a1]" in (c.get("body") or "")]
    assert relayed and rec.messages
