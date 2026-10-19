"""90Y9(M58SPVD8-90Y9): 리뷰 반복 억제 개정 — 리뷰 반복(request-changes 누적)은
경계 확장의 정상 진행으로 스킵하지 않고 경고 가설 1건+hermes 위임.
진전 없는 같은 동작 반복(draft_merge_loop)만 dedup — tests/test_failure_rules.py
test_draft_경과시_사람_1회_통보한다가 기존 커버."""
import json

import probe.core as core
from test_tt_lifecycle import snap, submitted


def _agents(s):
    s["agents"] = [{"name": "worker", "enabled": True, "receive_mode": "hook",
                    "base_url": "http://w/hook"},
                   {"name": "codex", "enabled": True, "receive_mode": "hook",
                    "base_url": "http://x/hook"},
                   {"name": "hermes-m2max", "enabled": True, "receive_mode": "hook",
                    "base_url": "http://h/hook"}]
    return s


def _api(client):
    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()
    return api


def _rc(client, issue, agent="codex"):
    """request-changes 판정을 남긴다."""
    base = f"/issues/{issue['id']}"
    assert client.post(base + "/claim-review", json={"agent": agent}).status_code == 200
    client.post(base + "/comments", json={
        "author": agent, "body": f"review: request-changes\n{issue['review_subject']}\n이유"})
    return client.get(base).json()


def _resubmit(client, issue):
    """같은 회차 보고 재제출 — 새 review_subject(리뷰 반복 시나리오)."""
    proof = dict(issue["completion_report"])
    proof["implementation"] = dict(proof["implementation"], commands="test v2")
    r = client.patch(f"/issues/{issue['id']}",
                     json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text
    return client.get(f"/issues/{issue['id']}").json()


def test_first_fix_round_has_no_warn(client):
    issue = _rc(client, submitted(client))
    fix = [a for a in core.decide(_agents(snap(issue))) if a["action"] == "tt-review-fix"]
    assert len(fix) == 1
    assert "warn" not in fix[0]  # 1회차는 정상 review-fix


def test_repeat_fix_warns_and_hooks_without_skip(client, tmp_path, monkeypatch, hook_server):
    issue = _rc(client, submitted(client))
    assert client.post("/agents", json={"name": "worker", "receive_mode": "hook",
                                        "base_url": hook_server}).status_code == 201
    assert client.post("/agents", json={"name": "hermes-m2max", "receive_mode": "hook",
                                        "base_url": "http://h/hook"}).status_code == 201
    monkeypatch.setattr(core, "api", _api(client))
    posts = []
    monkeypatch.setattr(core, "_post_json",
                        lambda url, payload, timeout=5: posts.append((url, payload)) or 200)
    monkeypatch.setenv("TT_JUDGE_LOG", str(tmp_path / "jl.jsonl"))
    core.execute("http://tt", core.decide(_agents(snap(issue)))[0])  # 1회차 발령
    issue2 = _rc(client, _resubmit(client, client.get(f"/issues/{issue['id']}").json()))
    fix = [a for a in core.decide(_agents(snap(issue2))) if a["action"] == "tt-review-fix"]
    assert len(fix) == 1
    assert fix[0].get("warn") is True and fix[0]["warn_count"] == 2
    core.execute("http://tt", fix[0])  # 스킵 없이 발령된다
    assert any(d["agent"] == "worker"
               for d in client.get(f"/issues/{issue['id']}/dispatches").json())
    rows = [json.loads(l) for l in open(tmp_path / "jl.jsonl", encoding="utf-8") if l.strip()]
    warns = [r for r in rows if r["point"] == "review-repeat" and r["output"] == "warn"]
    assert len(warns) == 1  # 경고 가설 1건
    assert warns[0]["confidence"] == "high"
    assert any(i.get("reason_code") == "suggested_next=refactor-right"
               for i in warns[0]["evidence"])  # right 단계 후속 권고(사용자 결정)
    assert len(posts) == 1 and posts[0][0] == "http://h/hook"  # hermes 위임 1회
    assert posts[0][1]["type"] == "review-repeat-warn" and posts[0][1]["repeats"] == 2


def test_hook_failure_does_not_break_dispatch(client, tmp_path, monkeypatch, hook_server):
    issue = _rc(client, submitted(client))
    assert client.post("/agents", json={"name": "worker", "receive_mode": "hook",
                                        "base_url": hook_server}).status_code == 201
    monkeypatch.setattr(core, "api", _api(client))
    monkeypatch.setenv("TT_JUDGE_LOG", str(tmp_path / "jl.jsonl"))
    core.decide(_agents(snap(issue)))[0]  # 1회차 발령 픽스처
    issue2 = _rc(client, _resubmit(client, client.get(f"/issues/{issue['id']}").json()))
    monkeypatch.setattr(core, "_post_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    act = [a for a in core.decide(_agents(snap(issue2))) if a["action"] == "tt-review-fix"][0]
    core.execute("http://tt", act)  # hook 실패해도 발령은 완료된다
    assert any(d["agent"] == "worker"
               for d in client.get(f"/issues/{issue['id']}/dispatches").json())
