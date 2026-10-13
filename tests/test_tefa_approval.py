"""TEFA: force_done/close no longer complete; report is the non-PR delivery name."""
import pytest
from pydantic import ValidationError

from conftest import mk
from delivery import Delivery


def _claimed(client):
    issue = mk(client, title="승인 경로", state="todo")
    assert client.post(f"/issues/{issue['id']}/claim", json={"agent": "w@t"}).status_code == 200
    return issue["id"]


def test_force_done_is_rejected_and_state_unchanged(client):
    iid = _claimed(client)
    r = client.patch(f"/issues/{iid}", json={"state": "done", "force_done": True})
    assert r.status_code == 422
    assert "human" in r.json()["detail"]
    assert client.get(f"/issues/{iid}").json()["state"] == "in_progress"


def test_close_label_cannot_approve_done(client):
    iid = _claimed(client)
    client.patch(f"/issues/{iid}", json={"labels": ["close"]})
    r = client.patch(f"/issues/{iid}", json={"state": "done"})
    assert r.status_code == 422
    assert client.get(f"/issues/{iid}").json()["state"] == "in_progress"


def test_human_verify_still_approves(client):
    iid = _claimed(client)
    r = client.post(f"/issues/{iid}/verify", json={
        "verifier": "person", "human": True, "evidence": "분석 산출물 승인",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "done" and body["verification_status"] == "approved"
    assert "분석 산출물 승인" in body["verified_evidence"]


def test_report_delivery_is_canonical_and_local_is_accepted_alias():
    report = Delivery(mode="report")
    alias = Delivery(mode="local")
    assert report.mode == "report" and alias.mode == "local"
    with pytest.raises(ValidationError):
        Delivery(mode="report", repo="owner/repo")
