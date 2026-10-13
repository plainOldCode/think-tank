"""Q614: TT submissions, rather than hosting-provider PRs, identify reviews."""
import copy

import pytest

import probe.core as core
from conftest import mk


def submitted(client):
    issue = mk(client, title="local deliverable")
    issue = client.post(f"/issues/{issue['id']}/claim", json={"agent": "worker"}).json()
    proof = {
        "contract_version": issue["work_contract"]["version"],
        "attempt": issue["execution_attempt"], "method": "planned", "result": "passed",
        "design": {"criteria": "local acceptance", "verification": "run local tests"},
        "implementation": {"summary": "local artifact at /workspace/result", "commands": "test"},
        "verification": {"commands": "test", "evidence": "1 passed"},
    }
    r = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text
    return client.get(f"/issues/{issue['id']}").json()


def approve(client, issue, verdict="approve"):
    base = f"/issues/{issue['id']}"
    assert client.post(base + "/claim-review", json={"agent": "codex"}).status_code == 200
    client.post(base + "/comments", json={
        "author": "codex", "body": f"review: {verdict}\n{issue['review_subject']}\nlocal tests checked",
    })
    return client.get(base).json()


def snap(issue, prs=()):
    return {"auto": True, "now": "2026-10-13T12:00:00+0900", "issues": [issue],
            "agents": [{"name": "codex", "enabled": True, "base_url": "http://x/hook",
                        "receive_mode": "hook"}], "prs": list(prs)}


def test_local_submission_has_review_target_and_is_dispatched(client):
    issue = submitted(client)
    assert issue["review_subject"].startswith(f"TT#{issue['id']}@a1/")
    acts = core.decide(snap(issue))
    assert [a["action"] for a in acts] == ["tt-review"]
    assert acts[0]["agent"] == "codex"


def test_approve_without_pr_verifies_via_public_api(client, monkeypatch):
    issue = approve(client, submitted(client))
    acts = core.decide(snap(issue))
    assert [a["action"] for a in acts] == ["tt-verify"]

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", acts[0])
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "done"
    assert got["completion_report"] == issue["completion_report"]


def test_same_attempt_resubmit_invalidates_approval_and_old_action(client):
    issue = approve(client, submitted(client))
    r = client.patch(f"/issues/{issue['id']}", json={
        "state": "done", "completion_report": issue["completion_report"],
    })
    newer = r.json()
    assert newer["execution_attempt"] == issue["execution_attempt"]
    assert newer["review_subject"] != issue["review_subject"]
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": issue["completion_report"],
        "review_subject": issue["review_subject"],
    })
    assert r.status_code == 409
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "review"


def test_bound_verify_requires_current_independent_verdict(client):
    issue = submitted(client)
    base = f"/issues/{issue['id']}"
    payload = {"verifier": "probe", "completion_report": issue["completion_report"],
               "review_subject": issue["review_subject"]}
    # Knowing a token does not confer reviewer authority.
    client.post(base + "/comments", json={"author": "worker", "body":
                f"review: approve\n{issue['review_subject']}"})
    assert client.post(base + "/verify", json=payload).status_code == 409
    approve(client, issue)
    approve(client, issue, "request-changes")
    assert client.post(base + "/verify", json=payload).status_code == 409


def test_scope_and_evidence_changes_change_review_target(client):
    issue = submitted(client)
    changed = client.patch(f"/issues/{issue['id']}", json={"acceptance": "new criterion"}).json()
    assert changed["review_subject"] != issue["review_subject"]


def test_missing_or_failed_report_never_auto_verifies(client):
    issue = submitted(client)
    issue["completion_report"]["result"] = "failed"
    assert not any(a["action"] == "tt-verify" for a in core.decide(snap(issue)))


@pytest.mark.parametrize("provider", ["local", "gitea", "github"])
def test_provider_is_optional_for_tt_review(client, provider):
    issue = approve(client, submitted(client))
    issue["labels"].append(provider)
    assert [a["action"] for a in core.decide(snap(issue))] == ["tt-verify"]


def test_merged_pr_does_not_create_false_no_pr_notice(client):
    issue = approve(client, submitted(client))
    pr = {"number": 108, "repo": "example/project", "title": issue["id"],
          "state": "MERGED", "head_sha": "a" * 40, "checks": []}
    # PR completion needs an explicitly reviewed head even if hosting says MERGED.
    acts = core.decide(snap(issue, [pr]))
    assert [a["action"] for a in acts] == ["tt-delivery-note"]
    assert "PR 없음" not in acts[0]["reason"]


def test_pending_submission_cannot_reuse_pr_only_verdict(client):
    issue = submitted(client)
    issue["comments"] = [{"author": "codex", "body": "review: approve\nPR#108@aaaaaaaa"}]
    assert not any(a["action"] == "tt-verify" for a in core.decide(snap(issue)))


def test_dispatch_claim_contract_delivery_and_dedup(client, monkeypatch):
    issue = submitted(client)
    act = core.decide(snap(issue))[0]
    deliveries = []

    def api(url, path, method="GET", body=None):
        if path.endswith("/dispatch"):
            deliveries.append(body)
            return {"id": 1, "status": "ok"}
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    core.execute("http://tt", act)
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery["work_contract"]["role"] == "reviewer"
    assert delivery["work_contract"]["version"] == "review-v2"
    assert issue["review_subject"] in delivery["message"]
    assert "선택적 미러" in delivery["message"]
    assert "git fetch origin pull/" not in delivery["message"]
    assert client.get(f"/issues/{issue['id']}").json()["reviewer"] == "codex"


def test_review_changes_dispatch_to_executor_and_resubmit(client, monkeypatch):
    issue = approve(client, submitted(client), "request-changes")
    s = snap(issue)
    s["agents"].append({"name": "worker", "enabled": True, "base_url": "http://x/hook",
                        "receive_mode": "hook"})
    act = core.decide(s)[0]
    assert act["action"] == "tt-review-fix"
    deliveries = []

    def api(url, path, method="GET", body=None):
        if path.endswith("/dispatch"):
            deliveries.append(body)
            return {"id": 1, "status": "ok"}
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    core.execute("http://tt", act)
    assert len(deliveries) == 1
    assert "tt submit" in deliveries[0]["message"]
    current = client.get(f"/issues/{issue['id']}").json()
    assert current["state"] == "review" and not current["reviewer"]
    assert not core.decide({**s, "issues": [current]})


def test_planned_approval_rechecked_after_rejection(client, monkeypatch):
    issue = approve(client, submitted(client))
    act = core.decide(snap(issue))[0]
    approve(client, issue, "request-changes")
    writes = []

    def api(url, path, method="GET", body=None):
        if method != "GET":
            writes.append((path, body))
        return client.get(path).json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    assert not writes


def test_verification_cannot_replace_reviewed_evidence(client):
    issue = approve(client, submitted(client))
    proof = copy.deepcopy(issue["completion_report"])
    proof["verification"]["evidence"] = "different evidence"
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": proof, "review_subject": issue["review_subject"],
    })
    assert r.status_code == 409


def test_structured_evidence_bound_verification(client):
    issue = submitted(client)
    proof = copy.deepcopy(issue["completion_report"])
    proof["verification"]["evidence"] = [{"command": "test", "exit_code": 0, "output_snippet": "1 passed"}]
    updated = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert updated.status_code == 200, updated.text
    issue = approve(client, updated.json())
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": issue["completion_report"],
        "review_subject": issue["review_subject"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "done"


@pytest.mark.parametrize("provider", ["none", "local", "gitea"])
def test_provider_disabled_does_not_call_github(monkeypatch, provider):
    monkeypatch.setenv("TT_PR_PROVIDER", provider)

    def forbidden(*args):
        pytest.fail("TT-only operation must not query GitHub")

    monkeypatch.setattr(core, "gh_json", forbidden)
    assert core.collect_prs(["example/project"]) == []


def test_provider_failure_does_not_suppress_tt_review(client, monkeypatch):
    monkeypatch.setenv("TT_PR_PROVIDER", "github")

    def unavailable(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(core, "gh_json", unavailable)
    s = snap(submitted(client), core.collect_prs(["example/project"]))
    assert core.decide(s)[0]["action"] == "tt-review"


def test_valid_report_with_busy_dispatch_does_not_spawn_another(client):
    issue = submitted(client)
    issue["busy_agents"] = ["codex"]
    assert core.decide(snap(issue)) == []


@pytest.mark.parametrize("status", ["MERGED", "CLOSED", "UNKNOWN"])
def test_linked_pr_observation_distinguishes_closed_and_unavailable(client, monkeypatch, status):
    from probe.providers import observe_linked
    monkeypatch.setenv("TT_PR_PROVIDER", "github")
    issue = submitted(client)
    issue["completion_report"] = None  # legacy card needs a fresh report, not guessed verification
    issue["comments"].append({"author": "worker", "body": "https://github.com/example/project/pull/12"})
    issue["updated_at"] = "2026-10-13T01:00:00+0900"

    def query(*args):
        if status == "UNKNOWN":
            raise RuntimeError("network down")
        return {"state": status, "mergedAt": "2026-10-13" if status == "MERGED" else None}

    context = observe_linked([issue], [], query)
    assert context[issue["id"]][0]["state"] == status
    acts = core.decide({**snap(issue), "pr_context": context})
    reasons = " ".join(a.get("reason", "") for a in acts)
    assert "PR 없음" not in reasons
    if status == "MERGED":
        assert "병합 확인" in reasons
    elif status == "CLOSED":
        assert "PR 닫힘" in reasons
    else:
        assert "PR 미확인" in reasons
    assert not any(a["action"] in ("merge", "tt-verify") for a in acts)


def test_real_dispatch_envelope_uses_review_contract(client, monkeypatch):
    import service
    issue = submitted(client)
    assert client.post("/agents", json={"name": "codex", "base_url": "http://localhost/hook",
                                       "receive_mode": "hook"}).status_code < 300
    envelopes = []

    def deliver(url, secret, payload):
        envelopes.append(payload)
        return 200, "test-session"

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(service, "deliver", deliver)
    monkeypatch.setattr(core, "api", api)
    act = core.decide(snap(issue))[0]
    core.execute("http://tt", act)
    assert len(envelopes) == 1
    assert envelopes[0]["work_contract"]["version"] == "review-v2"
    assert envelopes[0]["execution_attempt"] == issue["execution_attempt"]
    assert issue["review_subject"] in envelopes[0]["message"]


def test_failed_hook_has_no_success_marker(client, monkeypatch):
    issue = submitted(client)
    act = core.decide(snap(issue))[0]

    def api(url, path, method="GET", body=None):
        if path.endswith("/dispatch"):
            return {"status": "error", "detail": "unreachable"}
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    current = client.get(f"/issues/{issue['id']}").json()
    assert not any(c["author"] == "probe" for c in current["comments"])
    assert current["state"] == "review"


def test_old_submission_execute_does_not_dispatch(client, monkeypatch):
    issue = submitted(client)
    act = core.decide(snap(issue))[0]
    client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": issue["completion_report"]})
    writes = []

    def api(url, path, method="GET", body=None):
        if method != "GET":
            writes.append((path, body))
        return client.get(path).json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    assert not writes
