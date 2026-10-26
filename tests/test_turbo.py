"""Turbo server: state, token gate, board question, legacy read-only archive."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from turbo.app import create_app, main, subprocess_verify
from turbo.errors import TurboError
from turbo.gate import check_token, digest
from turbo import service

BODY = "Implement the gate."
CONTRACT = "Submit is rejected without a work token."
SCOPE = {
    "paths": ["turbo/"],
    "forbidden": ["turbo/legacy_schema.py"],
    "verify": [{"cmd": "pytest -q", "kind": "unit"}],
}


class Clock:
    def __init__(self, t=1_700_000_000):
        self.t = t

    def __call__(self):
        return self.t


def boot(tmp_path, verify=None, legacy=None, clock=None):
    clock = clock or Clock()
    app = create_app(str(tmp_path / "turbo.db"), legacy_path=legacy, verify=verify, clock=clock)
    client = TestClient(app)
    client.post("/turbo/actors", json={"name": "ada", "role": "human"})
    client.post("/turbo/actors", json={
        "actor": "ada", "name": "run-1", "role": "runner", "harnesses": ["claude", "codex"],
    })
    client.post("/turbo/actors", json={"actor": "ada", "name": "worker", "role": "agent"})
    client.post("/turbo/actors", json={"actor": "ada", "name": "probe", "role": "probe"})
    return client, clock


def make_card(client, harness="claude", scope=None):
    res = client.post("/turbo/cards", json={
        "actor": "ada", "title": "gate", "body": BODY, "contract": CONTRACT,
        "harness": harness, "scope": scope or SCOPE,
    })
    assert res.status_code == 200, res.text
    return res.json()


def ok_verify(cmd, cwd):
    return 0, "passed " + cmd


def bad_verify(cmd, cwd):
    return 1, "failed " + cmd


def test_first_actor_must_be_human(tmp_path):
    app = create_app(str(tmp_path / "turbo.db"))
    client = TestClient(app)
    res = client.post("/turbo/actors", json={"name": "run-1", "role": "runner", "harnesses": ["claude"]})
    assert res.status_code == 403


def test_claim_ack_question_answer_and_runner_submit(tmp_path):
    client, _clock = boot(tmp_path, verify=ok_verify)
    card = make_card(client)
    claimed = client.post(f"/turbo/cards/{card['id']}/claim", json={"actor": "run-1", "agent": "worker"})
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["state"] == "working"
    assert body["body"] == BODY
    assert body["nonce"]
    assert "work_token" not in body

    bad = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker", "digest": "0" * 64, "nonce": body["nonce"],
    })
    assert bad.status_code == 409

    good = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker",
        "digest": digest(body["body"], body["contract"], body["contract_version"], body["scope"]),
        "nonce": body["nonce"],
    })
    assert good.status_code == 200, good.text
    token = good.json()["work_token"]
    again = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker",
        "digest": digest(body["body"], body["contract"], body["contract_version"], body["scope"]),
        "nonce": body["nonce"],
    })
    assert again.status_code == 409

    asked = client.post(f"/turbo/cards/{card['id']}/question", json={
        "actor": "worker",
        "blocked_question": "Which store?",
        "blocked_options": ["sqlite", "postgres"],
        "blocked_default": "sqlite",
    })
    assert asked.status_code == 200
    assert asked.json()["state"] == "question"

    closed = client.post(f"/turbo/cards/{card['id']}/turn-closed", json={"actor": "run-1"})
    assert closed.status_code == 200
    assert closed.json()["state"] == "awaiting-human"
    assert closed.json()["default_deadline"]

    answered = client.post(f"/turbo/cards/{card['id']}/answer", json={"actor": "ada", "choice": "sqlite"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["answer"] == "sqlite"
    assert answered.json()["state"] == "awaiting-human"
    assert answered.json()["resume"]["prompt"].startswith("[tt resume]")
    assert "kind=answer" in answered.json()["resume"]["prompt"]
    assert "choice=sqlite" in answered.json()["resume"]["prompt"]

    started = client.post(f"/turbo/cards/{card['id']}/resume", json={
        "actor": "run-1", "token": answered.json()["resume"]["token"], "mode": "resume",
    })
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "working"

    stale = client.post(f"/turbo/cards/{card['id']}/resume", json={
        "actor": "run-1", "token": answered.json()["resume"]["token"], "mode": "resume",
    })
    assert stale.status_code == 409

    ready = client.post(f"/turbo/cards/{card['id']}/ready", json={"actor": "worker", "note": "tree is ready"})
    assert ready.status_code == 200

    agent_submit = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "worker", "sha": "ab" * 20, "pr": {"number": 1},
        "changed_paths": ["turbo/app.py"], "work_token": token,
    })
    assert agent_submit.status_code == 403

    submitted = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "ab" * 20, "pr": {"provider": "github", "number": 7, "base": "main"},
        "changed_paths": ["turbo/app.py"], "work_token": token,
    })
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "review"
    assert submitted.json()["verify_level"] == "unit-test-verified"
    assert submitted.json()["sha"] == "ab" * 20

    early = client.post(f"/turbo/cards/{card['id']}/merge", json={"actor": "run-1"})
    assert early.status_code == 409
    agent_merge = client.post(f"/turbo/cards/{card['id']}/merge", json={"actor": "worker"})
    assert agent_merge.status_code == 403

    approved = client.post(f"/turbo/cards/{card['id']}/verdict", json={
        "actor": "probe", "result": "approve",
    })
    assert approved.status_code == 200, approved.text
    merged = client.post(f"/turbo/cards/{card['id']}/merge", json={"actor": "probe"})
    assert merged.status_code == 200, merged.text
    assert merged.json()["state"] == "merged"


def test_edited_card_rejects_the_old_token(tmp_path):
    client, _clock = boot(tmp_path, verify=ok_verify)
    card = make_card(client)
    claimed = client.post(f"/turbo/cards/{card['id']}/claim", json={"actor": "run-1", "agent": "worker"}).json()
    token = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker",
        "digest": digest(claimed["body"], claimed["contract"], claimed["contract_version"], claimed["scope"]),
        "nonce": claimed["nonce"],
    }).json()["work_token"]
    edited = client.post(f"/turbo/cards/{card['id']}/edit", json={"actor": "ada", "body": "Edited body."})
    assert edited.status_code == 200, edited.text
    assert edited.json()["contract_version"] == 2
    assert "nonce" in edited.json()
    rejected = client.post(f"/turbo/cards/{card['id']}/ready", json={"actor": "worker"})
    assert rejected.status_code == 200
    submitted = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "cd" * 20, "pr": {},
        "changed_paths": ["turbo/app.py"], "work_token": token,
    })
    assert submitted.status_code == 403


def test_check_token_sees_a_changed_digest():
    card = {
        "work_token": "wt_example",
        "token_digest": "0" * 64,
        "body": "b",
        "contract": "c",
        "contract_version": 1,
        "scope": {"paths": ["turbo/"], "forbidden": [], "verify": [{"cmd": "pytest", "kind": "unit"}], "timebox_s": None},
    }
    with pytest.raises(TurboError) as caught:
        check_token(card, "wt_example")
    assert caught.value.status == 409


def test_question_shape_and_default_answer(tmp_path):
    clock = Clock()
    client, clock = boot(tmp_path, clock=clock)
    card = make_card(client)
    client.post(f"/turbo/cards/{card['id']}/claim", json={"actor": "run-1", "agent": "worker"})
    bad = client.post(f"/turbo/cards/{card['id']}/question", json={
        "actor": "worker", "blocked_question": "Go?",
        "blocked_options": ["only"], "blocked_default": "only",
    })
    assert bad.status_code == 422
    client.post(f"/turbo/cards/{card['id']}/question", json={
        "actor": "worker", "blocked_question": "Go?",
        "blocked_options": ["yes", "no"], "blocked_default": "no",
    })
    client.post(f"/turbo/cards/{card['id']}/turn-closed", json={"actor": "run-1"})
    clock.t += 24 * 3600 + 5
    applied = client.post("/turbo/runner/defaults", json={"actor": "run-1"})
    assert applied.status_code == 200, applied.text
    assert card["id"] in applied.json()["applied"]
    shown = client.get(f"/turbo/cards/{card['id']}").json()
    assert shown["answer"] == "no"
    assert shown["answer_by"] == "default"
    assert "source=default" in shown["resume"]["prompt"]


def test_scope_and_verify_failure(tmp_path):
    client, _clock = boot(tmp_path, verify=bad_verify)
    card = make_card(client)
    claimed = client.post(f"/turbo/cards/{card['id']}/claim", json={"actor": "run-1", "agent": "worker"}).json()
    token = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker",
        "digest": digest(claimed["body"], claimed["contract"], claimed["contract_version"], claimed["scope"]),
        "nonce": claimed["nonce"],
    }).json()["work_token"]
    client.post(f"/turbo/cards/{card['id']}/ready", json={"actor": "worker"})
    outside = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "ab" * 20, "pr": {},
        "changed_paths": ["README.md"], "work_token": token,
    })
    assert outside.status_code == 409
    forbidden = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "ab" * 20, "pr": {},
        "changed_paths": ["turbo/app.py", "turbo/legacy_schema.py"], "work_token": token,
    })
    assert forbidden.status_code == 409
    failed = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "ab" * 20, "pr": {},
        "changed_paths": ["turbo/app.py"], "work_token": token,
    })
    assert failed.status_code == 200, failed.text
    assert failed.json()["verify_level"] == "failed"
    assert failed.json()["verdict"] == "request-changes"
    assert "kind=verdict" in failed.json()["resume"]["prompt"]
    approve = client.post(f"/turbo/cards/{card['id']}/verdict", json={"actor": "probe", "result": "approve"})
    assert approve.status_code == 409


def test_affinity_and_second_claim(tmp_path):
    client, _clock = boot(tmp_path)
    other = make_card(client, harness="grok")
    res = client.post(f"/turbo/cards/{other['id']}/claim", json={"actor": "run-1", "agent": "worker"})
    assert res.status_code == 409
    first = make_card(client)
    assert client.post(f"/turbo/cards/{first['id']}/claim", json={"actor": "run-1", "agent": "worker"}).status_code == 200
    second = make_card(client)
    again = client.post(f"/turbo/cards/{second['id']}/claim", json={"actor": "run-1", "agent": "worker"})
    assert again.status_code == 409


def test_typecheck_level(tmp_path):
    client, _clock = boot(tmp_path, verify=ok_verify)
    scope = {
        "paths": ["turbo/"],
        "verify": [{"cmd": "python -m compileall turbo", "kind": "type"}],
    }
    card = make_card(client, scope=scope)
    claimed = client.post(f"/turbo/cards/{card['id']}/claim", json={"actor": "run-1", "agent": "worker"}).json()
    token = client.post(f"/turbo/cards/{card['id']}/ack", json={
        "actor": "worker",
        "digest": digest(claimed["body"], claimed["contract"], claimed["contract_version"], claimed["scope"]),
        "nonce": claimed["nonce"],
    }).json()["work_token"]
    client.post(f"/turbo/cards/{card['id']}/ready", json={"actor": "worker"})
    done = client.post(f"/turbo/cards/{card['id']}/submit", json={
        "actor": "run-1", "sha": "ef" * 20, "pr": {},
        "changed_paths": ["turbo/app.py"], "work_token": token,
    })
    assert done.json()["verify_level"] == "type-check-only"


def test_legacy_archive_is_read_only(tmp_path):
    from turbo.legacy_schema import create_throwaway

    path = tmp_path / "tt.db"
    create_throwaway(str(path))
    con = sqlite3.connect(path)
    stamp = "2026-10-01T00:00:00+0000"
    con.execute(
        "INSERT INTO issues (id, title, body, state, created_at, updated_at, acceptance, "
        "brief_scope, brief_verify, brief_forbidden, brief_timebox, blocked_question, "
        "blocked_options, blocked_default) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("PARENT01", "parent", "parent body", "todo", stamp, stamp, "done when tested",
         "turbo/", "pytest", "no secrets", "1d", "Ship it?", '["yes","no"]', "yes"),
    )
    con.execute(
        "INSERT INTO issues (id, title, body, state, created_at, updated_at, parent_id, "
        "completion_report) VALUES (?,?,?,?,?,?,?,?)",
        ("CHILD001", "child", "child body", "in_progress", stamp, stamp, "PARENT01",
         json.dumps({"summary": "report text"})),
    )
    con.execute(
        "INSERT INTO comments (issue_id, author, body, ts) VALUES (?,?,?,?)",
        ("PARENT01", "ada", "a comment", stamp),
    )
    con.commit()
    before = [tuple(row) for row in con.execute(
        "SELECT id, title, updated_at FROM issues ORDER BY id")]
    schema = [tuple(row) for row in con.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
    con.close()

    client, _clock = boot(tmp_path, legacy=str(path))
    listed = client.get("/legacy/issues")
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()["issues"]}
    assert ids == {"PARENT01", "CHILD001"}
    detail = client.get("/legacy/issues/PARENT01")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["read_only"] is True
    assert payload["issue"]["body"] == "parent body"
    assert payload["issue"]["blocked_question"] == "Ship it?"
    assert payload["issue"]["blocked_options"] == ["yes", "no"]
    assert [child["id"] for child in payload["children"]] == ["CHILD001"]
    assert payload["comments"][0]["body"] == "a comment"
    child = client.get("/legacy/issues/CHILD001").json()
    assert child["report"]["summary"] == "report text"
    metrics = client.get("/turbo/metrics").json()
    assert metrics["legacy_cards"] == 2
    assert metrics["legacy_brief_complete"] == 1

    ro = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ro.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO issues (id, title, body, created_at, updated_at) VALUES ('X','t','b','t','t')")
    ro.close()

    con = sqlite3.connect(path)
    assert [tuple(row) for row in con.execute(
        "SELECT id, title, updated_at FROM issues ORDER BY id")] == before
    assert [tuple(row) for row in con.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")] == schema
    con.close()


def test_missing_legacy_is_404(tmp_path):
    client, _clock = boot(tmp_path)
    res = client.get("/legacy/issues")
    assert res.status_code == 404
    detail = client.get("/legacy/issues/ANY")
    assert detail.status_code == 404


def test_board_html_uses_the_ink_page(tmp_path):
    client, _clock = boot(tmp_path)
    html = client.get("/")
    assert html.status_code == 200
    text = html.text
    assert "think-tank" in text
    assert "read-only" in text
    assert "/assets/turbo.css" in text
    assert "tmux" not in text
    css = client.get("/assets/turbo.css").text
    assert "#f7f6f3" in css
    assert "#c23b22" in css
    assert "box-shadow" not in css


def test_verify_boundaries(monkeypatch):
    import subprocess

    assert subprocess_verify("", None) == (1, "empty verify command")
    assert subprocess_verify("   ", None) == (1, "empty verify command")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("cmd", 120, output=b"out-bytes", stderr="err-text")

    monkeypatch.setattr(subprocess, "run", timeout)
    code, text = subprocess_verify("python3 -c pass", None)
    assert code == 124
    assert "out-bytes" in text and "err-text" in text

    def missing(*_args, **_kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", missing)
    assert subprocess_verify("missing-bin", None) == (127, "no such binary")


def test_main_reads_the_documented_env(monkeypatch, tmp_path):
    seen = {}

    def fake_run(app, host, port):
        seen["host"] = host
        seen["port"] = port
        seen["title"] = app.title

    monkeypatch.setenv("TURBO_DB", str(tmp_path / "sub" / "turbo.db"))
    monkeypatch.setenv("TURBO_PORT", "7822")
    monkeypatch.setenv("TT_LEGACY_DB", str(tmp_path / "legacy.db"))
    monkeypatch.setattr("uvicorn.run", fake_run)
    main()
    assert seen == {"host": "127.0.0.1", "port": 7822, "title": "think-tank turbo"}


def test_submit_rejects_a_non_object_pr(tmp_path):
    client, clock = boot(tmp_path)
    with pytest.raises(TurboError) as caught:
        service.submit(
            client.app.state.store, "missing", "run-1", "ab" * 20, "nope", [], "wt",
            lambda _cmd, _cwd: (0, ""), clock,
        )
    assert caught.value.status == 422
    assert "pr" in caught.value.message


def test_legacy_boundaries(tmp_path):
    from turbo.legacy import _labels, brief_stats, issue_detail, list_issues, open_ro
    from turbo.legacy_schema import create_throwaway

    missing = tmp_path / "missing.db"
    with pytest.raises(TurboError) as caught:
        open_ro(str(missing))
    assert caught.value.status == 404

    locked = tmp_path / "locked.db"
    locked.write_bytes(b"not a database")
    locked.chmod(0)
    try:
        with pytest.raises(TurboError) as caught:
            open_ro(str(locked))
        assert "read-only" in caught.value.message
    finally:
        locked.chmod(0o600)

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(TurboError) as caught:
        list_issues(str(empty))
    assert caught.value.status == 404
    with pytest.raises(TurboError) as caught:
        issue_detail(str(empty), "T1")
    assert caught.value.status == 404

    thin = tmp_path / "thin.db"
    con = sqlite3.connect(thin)
    con.execute("CREATE TABLE issues (id TEXT, title TEXT, body TEXT)")
    con.execute("INSERT INTO issues (id, title, body) VALUES ('T1', 't', 'b')")
    con.commit()
    con.close()
    detail = issue_detail(str(thin), "T1")
    assert detail["children"] == []
    assert detail["comments"] == []
    assert brief_stats(str(thin)) == {"legacy_cards": 0, "legacy_brief_complete": 0}
    assert _labels(["ink", "gate"]) == ["ink", "gate"]

    fresh = tmp_path / "throwaway.db"
    create_throwaway(str(fresh))
    with pytest.raises(FileExistsError):
        create_throwaway(str(fresh))


def test_store_edges(tmp_path, monkeypatch):
    from turbo.store import Store

    monkeypatch.chdir(tmp_path)
    store = Store("plain.db")
    with pytest.raises(RuntimeError):
        store.update_card("missing", not_a_column=1)
    with store.transaction():
        with store.transaction():
            store.con.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with store.transaction():
            with store.transaction():
                raise RuntimeError("inner")
