"""SRJY(M5283M00-SRJY) — PR 연결 제출 시점 자동화 + patch-id 재사용.

1) 보고에 delivery가 없어도 카드에 조인되는 open PR이 정확히 1개면 probe가 그
   PR을 관측값으로 연결한다(tt-deliver 가능). 둘 이상이면 기존대로(unbound-pr
   노트), 명시적 delivery(report/local)는 존중한다.
2) rebase로 head가 바뀌어도 git patch-id가 같으면 같은 산출물 — 재제출·재리뷰
   없이 병합한다. patch-id가 다르거나 base가 바뀌면 기존대로 보류.
"""
import copy

import pytest

import probe.core as core
import probe.providers as providers
from test_pr_delivery import SPEC, observation, resubmit
from test_tt_lifecycle import approve, snap, submitted

REPO = SPEC["repo"]
H1 = "a" * 40
H2 = "d" * 40  # 리베이스된 head
PID = "e" * 40


def pr_join(issue, **kw):
    """브랜치 조인되는 open PR 관측값(collect 형태)."""
    return {"number": 124, "repo": REPO, "branch": f"tt/{issue['id']}-x",
            "title": f"(TT {issue['id']})", "head_sha": H1, "isDraft": False,
            "labels": [], "checks": [{"state": "SUCCESS"}], **kw}


def spec(issue, **kw):
    return {"mode": "pr", "provider": "github", "repo": REPO, "number": 124,
            "head_sha": H1, "base": "main", **kw}


def bound_actions(issue, discovered, observed, patch_ids=None):
    s = snap(issue)
    s["discovered_delivery"] = {issue["id"]: discovered}
    s["delivery_context"] = {issue["id"]: observed}
    if patch_ids:
        s["patch_ids"] = patch_ids
    return core.decide(s)


# --- 1) 제출 시점 PR 자동 연결 ---

def test_보고에_delivery가_없어도_단일_PR이_자동_연결된다(client):
    issue = approve(client, submitted(client))  # 보고에 delivery 없음
    obs = {**spec(issue), "state": "OPEN", "isDraft": False,
           "checks": [{"state": "SUCCESS"}]}
    acts = bound_actions(issue, spec(issue), obs)
    assert [a["action"] for a in acts] == ["tt-deliver"], acts
    assert acts[0]["delivery"]["mode"] == "pr"
    assert acts[0]["delivery"]["head_sha"] == H1


def test_PR가_둘이면_자동_연결하지_않는다(client):
    issue = approve(client, submitted(client))
    p1, p2 = pr_join(issue), pr_join(issue, number=125, branch=f"tt/{issue['id']}-y")
    acts = core.decide({**snap(issue, [p1, p2]),
                        "discovered_delivery": {}, "delivery_context": {}})
    assert not any(a["action"] in ("tt-deliver", "tt-verify") for a in acts)
    assert any(a["action"] == "tt-delivery-note" for a in acts)


def test_명시_local_보고는_PR이_조인돼도_존중한다(client):
    issue = approve(client, resubmit(client, submitted(client), {"mode": "local"}))
    obs = {**spec(issue), "state": "OPEN", "isDraft": False,
           "checks": [{"state": "SUCCESS"}]}
    acts = bound_actions(issue, spec(issue), obs)
    assert [a["action"] for a in acts] == ["tt-verify"], acts


def test_자동_연결_PR의_CI_실패는_ci_fix로_위임된다(client):
    issue = approve(client, submitted(client))
    obs = {**spec(issue), "state": "OPEN", "isDraft": False,
           "checks": [{"state": "FAILURE", "name": "ci"}]}
    acts = bound_actions(issue, spec(issue), obs)
    assert [a["action"] for a in acts] == ["ci-fix"], acts
    assert acts[0]["delivery"]["head_sha"] == H1


# --- 2) patch-id 재사용 ---

def test_리베이스_head_변경도_patch_id가_같으면_병합_진행한다(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    rebased = observation(head_sha=H2)
    patch_ids = {(REPO, H1): PID, (REPO, H2): PID}
    acts = core.decide({**snap(issue), "delivery_context": {issue["id"]: rebased},
                        "patch_ids": patch_ids})
    assert [a["action"] for a in acts] == ["tt-deliver"], acts


def test_patch_id가_다르면_보류다(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    rebased = observation(head_sha=H2)
    patch_ids = {(REPO, H1): PID, (REPO, H2): "f" * 40}
    acts = core.decide({**snap(issue), "delivery_context": {issue["id"]: rebased},
                        "patch_ids": patch_ids})
    assert any(a["action"] == "tt-delivery-note" for a in acts)


def test_patch_id가_같아도_base가_바뀌면_보류다(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    rebased = observation(head_sha=H2, base="develop")
    patch_ids = {(REPO, H1): PID, (REPO, H2): PID}
    acts = core.decide({**snap(issue), "delivery_context": {issue["id"]: rebased},
                        "patch_ids": patch_ids})
    assert any(a["action"] == "tt-delivery-note" for a in acts)


def test_patch_id는_비교_diff에서_계산한다():
    diff = ("diff --git a/x.txt b/x.txt\n"
            "index 1234567..89abcde 100644\n"
            "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-a\n+b\n")
    p1 = providers.patch_id_of(REPO, "main", H1, lambda *a: diff)
    p2 = providers.patch_id_of(REPO, "main", H2, lambda *a: diff)
    p3 = providers.patch_id_of(REPO, "main", H2, lambda *a: diff + diff)
    assert p1 and p1 == p2 and p1 != p3  # 같은 diff면 head 무관 동일


def test_리베이스된_PR_병합은_현재_head로_가드된다(client, monkeypatch):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    obs_open = observation(head_sha=H2)
    obs_merged = observation(state="MERGED", head_sha=H2)
    seq = iter([obs_open, obs_merged])
    monkeypatch.setattr(providers, "inspect_delivery",
                        lambda s, q: dict(next(seq, obs_merged)))
    monkeypatch.setattr(core, "patch_id_for", lambda repo, base, sha: PID)
    merged = {}

    def fake_merge(spec, run, head=None):
        merged["head"] = head or spec["head_sha"]
        return None

    monkeypatch.setattr(providers, "merge_delivery", fake_merge)

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    monkeypatch.setattr(core, "gh_json", lambda *a, **k: {})
    act = {"issue": issue["id"], "subject": issue["review_subject"], "agent": "probe",
           "action": "tt-deliver", "delivery": SPEC,
           "patch_ids": {(REPO, H1): PID, (REPO, H2): PID}}
    core.execute("http://tt", act)
    assert merged["head"] == H2  # CAS는 관측된 현재 head로
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "done"
    rc = got["delivery_receipt"]
    assert rc["head_sha"] == H2  # receipt는 실제 병합된 head


# --- 자동 연결 병합의 완료 E2E (agy 리뷰: verify 409 결함 재발 방지) ---

def test_자동_연결_병합은_보고_delivery가_없어도_완료된다(client, monkeypatch):
    """보고 delivery 없음 + 단일 PR 자동 연결 → 병합 → verify까지 done.
    이 E2E가 없으면 /verify 409("unexpected delivery receipt") 결함을 못 본다."""
    issue = approve(client, submitted(client))
    obs_open = {**spec(issue), "state": "OPEN", "isDraft": False,
                "checks": [{"state": "SUCCESS"}]}
    obs_merged = {**obs_open, "state": "MERGED", "merge_commit": "b" * 40,
                  "merged_at": "2026-10-17T00:00:00Z"}
    seq = iter([obs_open, obs_merged])
    monkeypatch.setattr(providers, "inspect_delivery",
                        lambda s, q: dict(next(seq, obs_merged)))
    monkeypatch.setattr(providers, "merge_delivery", lambda s, run, head=None: None)

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            print("API ERR", method, path, r.status_code, r.text[:200])
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    monkeypatch.setattr(core, "gh_json", lambda *a, **k: {})
    acts = bound_actions(issue, spec(issue), obs_open)
    assert [a["action"] for a in acts] == ["tt-deliver"], acts
    core.execute("http://tt", acts[0])
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "done", got.get("verification_status")
    rc = got["delivery_receipt"]
    assert rc["head_sha"] == H1 and rc["number"] == 124
