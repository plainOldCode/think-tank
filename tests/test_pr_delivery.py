"""PR#124 regression: TT approval must not abandon the selected PR delivery."""
import copy

import pytest

import probe.core as core
from test_tt_lifecycle import submitted, approve, snap

HEAD = "a" * 40
SPEC = {"mode": "pr", "provider": "github", "repo": "example/project",
        "number": 124, "head_sha": HEAD, "base": "main"}


def resubmit(client, issue, delivery=None, commands=None):
    proof = copy.deepcopy(issue["completion_report"])
    if delivery is not None:
        proof["delivery"] = delivery
    if commands is not None:
        proof["implementation"]["commands"] = commands
    r = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text
    return client.get(f"/issues/{issue['id']}").json()


def observation(state="OPEN", **overrides):
    return {**SPEC, "state": state, "isDraft": False, "checks": [{"state": "SUCCESS"}],
            "merge_commit": "b" * 40 if state == "MERGED" else None,
            "merged_at": "2026-10-13T04:00:00Z" if state == "MERGED" else None, **overrides}


def actions(issue, obs):
    return core.decide({**snap(issue), "delivery_context": {issue["id"]: obs}})


def test_pr124_legacy_report_link_cannot_be_treated_as_local_completion(client):
    issue = resubmit(client, submitted(client), commands=
                     "commit bbe3494e; PR https://github.com/example/project/pull/124")
    issue = approve(client, issue)
    pr = {"number": 124, "repo": "example/project", "title": issue["id"],
          "head_sha": "c" * 40, "checks": [{"state": "SUCCESS"}]}
    acts = core.decide(snap(issue, [pr]))
    assert not any(a["action"] in ("tt-verify", "merge", "tt-deliver") for a in acts)
    assert any(a["action"] == "tt-delivery-note" for a in acts)


def test_current_pr_approval_requests_delivery_not_direct_verify(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    assert [a["action"] for a in actions(issue, observation())] == ["tt-deliver"]


@pytest.mark.parametrize("overrides", [
    {"state": "UNKNOWN"}, {"state": "CLOSED"}, {"head_sha": "c" * 40},
    {"base": "other"}, {"repo": "other/project"}, {"number": 125},
    {"isDraft": True}, {"checks": None}, {"checks": [{"state": "FAILURE"}]},
    {"checks": [{"state": "PENDING"}]},
])
def test_unready_or_changed_pr_cannot_close(client, overrides):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    acts = actions(issue, observation(**overrides))
    assert not any(a["action"] in ("tt-verify", "tt-deliver", "merge") for a in acts)


def test_explicit_local_work_still_finishes_without_pr(client):
    issue = approve(client, resubmit(client, submitted(client), {"mode": "local"}))
    assert issue["completion_report"]["delivery"]["mode"] == "local"
    assert [a["action"] for a in actions(issue, None)] == ["tt-verify"]


def test_고위험_tt카드는_probe_병합_휴면_사람승인_대기(client, monkeypatch):
    # FHH8 — tt-deliver 경로도 ⓪과 동일하게 휴면: OPEN PR 병합 금지 + 노트 1회.
    # 사람이 GitHub에서 직접 병합한 뒤(MERGED 관측) 완결되는 경로는 유지된다.
    import probe.providers as providers
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    assert client.patch(f"/issues/{issue['id']}",
                        json={"labels": ["risk:security"]}).status_code == 200
    issue = client.get(f"/issues/{issue['id']}").json()
    monkeypatch.setattr(providers, "inspect_delivery", lambda t, g: observation("OPEN"))
    merged = []
    monkeypatch.setattr(providers, "merge_delivery", lambda t, g: merged.append(1))

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", {"issue": issue["id"], "subject": issue["review_subject"],
                               "action": "tt-deliver", "agent": "probe"})
    assert not merged
    got = client.get(f"/issues/{issue['id']}").json()
    assert got["state"] == "review"
    assert any("tt-high-risk-hold" in (c.get("body") or "") for c in got["comments"])


def test_explicit_report_work_finishes_without_pr(client):
    issue = approve(client, resubmit(client, submitted(client), {"mode": "report"}))
    assert issue["completion_report"]["delivery"]["mode"] == "report"
    assert [a["action"] for a in actions(issue, None)] == ["tt-verify"]


def test_bound_verify_cannot_bypass_selected_pr_delivery(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": issue["completion_report"],
        "review_subject": issue["review_subject"],
    })
    assert r.status_code == 409
    assert "delivery" in r.json()["detail"]


def test_merge_is_head_guarded_and_receipt_is_persisted(client, monkeypatch):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    act = actions(issue, observation())[0]
    merged = []

    def query(*args):
        if args[:2] == ("pr", "checks"):
            return [{"state": "SUCCESS"}]
        return {"state": "MERGED" if merged else "OPEN", "headRefOid": HEAD,
                "baseRefName": "main", "isDraft": False,
                "mergeCommit": {"oid": "b" * 40} if merged else None,
                "mergedAt": "2026-10-13T04:00:00Z" if merged else None}

    def merge(*args):
        assert "--match-head-commit" in args and HEAD in args
        merged.append(args)

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "gh_json", query)
    monkeypatch.setattr(core, "gh_exec", merge)
    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    got = client.get(f"/issues/{issue['id']}").json()
    assert len(merged) == 1
    assert got["state"] == "done"
    assert got["delivery_receipt"]["head_sha"] == HEAD
    assert got["delivery_receipt"]["merge_commit"] == "b" * 40
    assert any("PR delivery:" in c["body"] and HEAD in c["body"] for c in got["comments"])


def test_head_push_after_planning_aborts_without_merge_or_verify(client, monkeypatch):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    act = actions(issue, observation())[0]
    writes = []
    monkeypatch.setattr(core, "api", lambda url, path, method="GET", body=None:
                        client.get(path).json() if method == "GET" else writes.append(body))
    monkeypatch.setattr(core, "gh_json", lambda *args: [] if args[1] == "checks" else {
        "state": "OPEN", "headRefOid": "c" * 40, "baseRefName": "main", "isDraft": False})
    monkeypatch.setattr(core, "gh_exec", lambda *args: pytest.fail("must not merge changed head"))
    core.execute("http://tt", act)
    assert not writes
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "review"


def test_changed_delivery_invalidates_old_approval(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    changed = resubmit(client, issue, {**SPEC, "head_sha": "c" * 40})
    assert changed["review_subject"] != issue["review_subject"]
    assert not any(a["action"] in ("tt-deliver", "tt-verify") for a in
                   actions(changed, observation(head_sha="c" * 40)))


@pytest.mark.parametrize("bad", [{"head_sha": "c" * 40}, {"base": "other"}, {"repo": "bad/repo"}])
def test_server_rejects_receipt_for_other_target(client, bad):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    receipt = {k: v for k, v in observation("MERGED").items()
               if k in ("provider", "repo", "number", "base", "head_sha", "merge_commit", "merged_at")}
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": issue["completion_report"],
        "review_subject": issue["review_subject"], "delivery_receipt": {**receipt, **bad},
    })
    assert r.status_code == 409


def test_already_merged_pr_can_finish_without_second_merge(client, monkeypatch):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    act = actions(issue, observation("MERGED"))[0]
    monkeypatch.setattr(core, "gh_json", lambda *args: [] if args[1] == "checks" else {
        "state": "MERGED", "headRefOid": HEAD, "baseRefName": "main", "isDraft": False,
        "mergeCommit": {"oid": "b" * 40}, "mergedAt": "2026-10-13T04:00:00Z"})
    monkeypatch.setattr(core, "gh_exec", lambda *args: pytest.fail("already merged"))

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "done"


@pytest.mark.parametrize("failure", ["error", "queued", "head-change"])
def test_merge_error_or_unconfirmed_result_keeps_review(client, monkeypatch, failure):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    act = actions(issue, observation())[0]
    attempted = []

    def query(*args):
        if args[1] == "checks":
            return [{"state": "SUCCESS"}]
        return {"state": "OPEN", "headRefOid": "c" * 40 if attempted and failure == "head-change" else HEAD,
                "baseRefName": "main", "isDraft": False, "mergeCommit": None, "mergedAt": None}

    def merge(*args):
        attempted.append(args)
        if failure == "error":
            raise RuntimeError("merge rejected")

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "gh_json", query)
    monkeypatch.setattr(core, "gh_exec", merge)
    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    assert attempted
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "review"


def test_provider_disabled_cannot_turn_selected_pr_into_local(client, monkeypatch):
    from probe.providers import inspect_delivery
    monkeypatch.setenv("TT_PR_PROVIDER", "none")
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    observed = inspect_delivery(SPEC, lambda *args: pytest.fail("provider disabled"))
    assert observed["state"] == "UNKNOWN"
    assert not any(a["action"] in ("tt-verify", "tt-deliver") for a in actions(issue, observed))


def test_legacy_local_report_hash_preserved_after_bound_verify(client):
    import db
    import json
    issue = submitted(client)
    # Simulate a persisted pre-delivery report without the new optional field.
    proof = copy.deepcopy(issue["completion_report"])
    proof.pop("delivery", None)
    with db.connect(client.app.state.db_path) as con:
        con.execute("UPDATE issues SET completion_report=? WHERE id=?", (json.dumps(proof), issue["id"]))
    issue = approve(client, client.get(f"/issues/{issue['id']}").json())
    r = client.post(f"/issues/{issue['id']}/verify", json={
        "verifier": "probe", "completion_report": proof, "review_subject": issue["review_subject"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["review_subject"] == issue["review_subject"]


def test_rejection_arriving_during_merge_prevents_done(client, monkeypatch):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    act = actions(issue, observation())[0]
    merged = []

    def query(*args):
        if args[1] == "checks":
            return [{"state": "SUCCESS"}]
        return {"state": "MERGED" if merged else "OPEN", "headRefOid": HEAD,
                "baseRefName": "main", "isDraft": False,
                "mergedAt": "2026-10-13T04:00:00Z" if merged else None,
                "mergeCommit": {"oid": "b" * 40} if merged else None}

    def merge(*args):
        merged.append(True)
        approve(client, issue, "request-changes")

    def api(url, path, method="GET", body=None):
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "gh_json", query)
    monkeypatch.setattr(core, "gh_exec", merge)
    monkeypatch.setattr(core, "api", api)
    core.execute("http://tt", act)
    assert merged
    assert client.get(f"/issues/{issue['id']}").json()["state"] == "review"


def test_failed_ci_is_delegated_and_changed_head_is_not_repaired(client):
    issue = approve(client, resubmit(client, submitted(client), SPEC))
    failed = observation(checks=[{"state": "FAILURE", "name": "browser-e2e"}])
    assert [a["action"] for a in actions(issue, failed)] == ["ci-fix"]
    assert [a["action"] for a in actions(issue, {**failed, "head_sha": "c" * 40})] == ["tt-delivery-note"]


def test_invalid_pr_target_is_rejected_on_submit(client):
    issue = submitted(client)
    proof = copy.deepcopy(issue["completion_report"])
    proof["delivery"] = {**SPEC, "head_sha": "abcdef12"}
    r = client.patch(f"/issues/{issue['id']}", json={"state": "done", "completion_report": proof})
    assert r.status_code == 422
