"""J2NJ(M54V6KY6-J2NJ) — 고위험 카드 이중 판정: 계열 상이 2인 리뷰+집계.

하이브리드 C: dual_review 라벨 카드는 모델 계열(gpt/qwen/gemini/claude/cursor…)
이 다른 리뷰어 2인에게 동시 요청하고, 판정이 갈리면 제3계열 중재자 1인(카드당 1회).
단일 리뷰 흐름은 기존 경로 그대로(동작 불변) — test_dispatchd·test_failure_rules가 잠근다.
"""
import pytest

import probe.core
from dispatchd import decide
from verdicts import verdicts_all

NOW = "2026-10-18T09:00:00+0900"
SHA = "b" * 40
SHA8 = SHA[:8]


def issue(iid="M4ABCDEF-GH12", **kw):
    base = {"id": iid, "state": "review", "archived": 0, "version": 3,
            "assignee": "impl@x", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": ["auto", "dual_review"], "priority": None, "execution_attempt": 1,
            "dispatches": 1, "release_ready": False, "waiting_for": None,
            "title": iid, "brief_verify": "pytest -q", "comments": [],
            "updated_at": "2026-10-18T08:00:00+0900",
            "work_contract": {"version": "v2"}}
    base.update(kw)
    return base


def agent(name, model, **kw):
    base = {"name": name, "enabled": True, "base_url": "http://x",
            "release_hook": True, "receive_mode": "hook", "model": model}
    base.update(kw)
    return base


def pr(number=9):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": [{"state": "SUCCESS"}], "isDraft": False,
            "branch": "tt/M4ABCDEF-GH12-x", "title": "", "head_sha": SHA}


def pc(body, author="probe", ts="2026-10-18T08:30:00+0900"):
    return {"id": 1, "author": author, "body": body, "ts": ts}


def snap(issues, agents, prs=(pr(),), routing=None):
    s = {"auto": True, "now": NOW, "agents": list(agents),
         "issues": list(issues), "prs": list(prs)}
    if routing:
        s["review_routing"] = routing
    return s


def verdict_comment(author, verdict):
    return pc(f"review: {verdict}\nPR#9@{SHA8} — 판정", author=author)


def req_comment(author):
    return pc(f"[review-req #9/{SHA8}] PR#9 CI green — 리뷰 요청 → {author}")


ROUTING = {"review_fallback": ["rev-a@t", "rev-b@t", "rev-c@t"]}
AGENTS = [agent("rev-a@t", "gpt-6.1-sol"), agent("rev-b@t", "qwen3.8"),
          agent("rev-c@t", "gemini-3")]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")


def test_dual_카드는_계열이_다른_리뷰어_2인에게_요청한다():
    acts = decide(snap([issue()], AGENTS, routing=ROUTING))
    reqs = [a for a in acts if a["action"] == "review-request"]
    assert len(reqs) == 2 and all(a.get("dual") for a in reqs)
    reviewers = {a["reviewer"] for a in reqs}
    assert reviewers == {"rev-a@t", "rev-b@t"}  # gpt·qwen — 계열 상이
    assert not [a for a in acts if a["action"] == "merge"]


def test_계열_2곳_미확보면_단일_요청으로_폴백한다():
    agents = [agent("rev-a@t", "gpt-6.1-sol"), agent("rev-b@t", "gpt-5")]  # 같은 계열
    acts = decide(snap([issue()], agents, routing=ROUTING))
    reqs = [a for a in acts if a["action"] == "review-request"]
    assert len(reqs) == 1 and "후보 부족" in reqs[0]["reason"]


def test_dual_단일_요청_마커는_비dual과_동일하게_dedup된다():
    i = issue(comments=[pc(f"[review-req #9/{SHA8}] 기존 요청")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert not [a for a in acts if a["action"] == "review-request"]


def test_불일치면_제3계열_중재자에게_1회_요청한다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    tb = [a for a in acts if a["action"] == "review-request" and "dual-tiebreak" in a["marker"]]
    assert len(tb) == 1 and tb[0]["reviewer"] == "rev-c@t"  # gemini — 제3계열
    assert not [a for a in acts if a["action"] in ("merge", "review-fix")]


def test_중재_요청은_마커로_1회_dedup된다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        pc(f"[dual-tiebreak #9/{SHA8}] 기존 중재 요청"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    # 중재 마커 있음 — 재요청 없음. 이후 판정은 latest-wins(RC가 최신) → review-fix
    assert not [a for a in acts if a["action"] == "review-request"]


def test_중재자가_없으면_사람_판단_노트다():
    agents = [agent("rev-a@t", "gpt-6.1-sol"), agent("rev-b@t", "qwen3.8")]
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], agents, routing=ROUTING))
    notes = [a for a in acts if a["action"] == "review-note"]
    assert len(notes) == 1 and "중재자 없음" in notes[0]["reason"]


def test_양측_승인이면_병합_후보다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "approve")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert len([a for a in acts if a["action"] == "merge"]) == 1


def test_한쪽만_RC면_즉시_review_fix다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert len([a for a in acts if a["action"] == "review-fix"]) == 1
    assert not [a for a in acts if a["action"] == "merge"]


def test_dual_요청은_claim_없이_발송된다(monkeypatch):
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        if path.endswith("/dispatches"):
            return []
        return {"comments": [], "version": 3, "execution_attempt": 1,
                "state": "review", "assignee": "impl@x"}

    monkeypatch.setattr(probe.core, "api", fake_api)
    act = {"action": "review-request", "issue": "M4ABCDEF-GH12",
           "marker": f"[review-req #9/{SHA8}]", "dual": True,
           "reviewer": "rev-b@t", "pr": 9, "repo": "plainOldCode/think-tank"}
    probe.core.execute("http://x", act)
    assert not [b for m, p, b in calls if p.endswith("/claim-review")]
    assert [b for m, p, b in calls if p.endswith("/dispatch")]


def test_verdicts_all은_리뷰어별_최신_판정을_개별_보관한다():
    comments = [verdict_comment("rev-a@t", "approve"),
                verdict_comment("rev-b@t", "request-changes"),
                verdict_comment("rev-a@t", "request-changes"),  # rev-a 최신
                pc("review: approve\nPR#9@deadbeef — stale head", author="rev-c@t")]
    out = verdicts_all(comments, ["rev-a@t", "rev-b@t", "rev-c@t"], 9, SHA8)
    assert out == {"rev-a@t": "request-changes", "rev-b@t": "request-changes"}


def test_non_dual_카드는_기존_단일_경로다():
    i = issue(labels=["auto"])  # dual_review 없음
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert acts and not any(a.get("dual") for a in acts)  # 기존 경로 유지


def test_단독_승인은_나머지_리뷰어를_기다린다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert not [a for a in acts if a["action"] in ("merge", "review-fix", "review-request")]


def test_중재자_판정이_없으면_기다린다():
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        pc(f"[dual-tiebreak #9/{SHA8}] PR#9 이중 판정 불일치 — 제3계열 중재 → rev-c@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], AGENTS, routing=ROUTING))
    assert not [a for a in acts if a["action"] in ("merge", "review-fix")]


def test_중재자_판정_도착시_최종_반영된다():
    base = [req_comment("rev-a@t"), req_comment("rev-b@t"),
            pc(f"[dual-tiebreak #9/{SHA8}] 중재 → rev-c@t"),
            verdict_comment("rev-a@t", "approve"),
            verdict_comment("rev-b@t", "request-changes")]
    i = issue(comments=base + [verdict_comment("rev-c@t", "approve")])
    assert len([a for a in decide(snap([i], AGENTS, routing=ROUTING))
                if a["action"] == "merge"]) == 1
    i2 = issue(comments=base + [verdict_comment("rev-c@t", "request-changes")])
    assert len([a for a in decide(snap([i2], AGENTS, routing=ROUTING))
                if a["action"] == "review-fix"]) == 1


def test_기판정자와_같은_계열은_중재자에서_제외된다():
    agents = [agent("rev-a@t", "gpt-6.1-sol"), agent("rev-b@t", "qwen3.8"),
              agent("rev-d@t", "gpt-5")]  # rev-d = gpt — 기판정자 계열 중복
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], agents, routing=ROUTING))
    tb = [a for a in acts if "dual-tiebreak" in (a.get("marker") or "")]
    assert len(tb) == 1 and "review-note" == tb[0]["action"]  # 제3계열 없음 → 사람 노트


def test_제3계열이_있으면_그_중에서_중재자를_고른다():
    agents = [agent("rev-a@t", "gpt-6.1-sol"), agent("rev-b@t", "qwen3.8"),
              agent("rev-d@t", "gpt-5"), agent("rev-c@t", "gemini-3")]
    i = issue(comments=[req_comment("rev-a@t"), req_comment("rev-b@t"),
                        verdict_comment("rev-a@t", "approve"),
                        verdict_comment("rev-b@t", "request-changes")])
    acts = decide(snap([i], agents, routing=ROUTING))
    tb = [a for a in acts if a["action"] == "review-request" and "dual-tiebreak" in a["marker"]]
    assert len(tb) == 1 and tb[0]["reviewer"] == "rev-c@t"
