"""Isolated runner imports keep installed state, registry and tmux untouched."""
import importlib.util
from pathlib import Path
import subprocess
import shutil
import time
import uuid

import pytest


@pytest.fixture
def runner(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1] / "runner"
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("TT_RUNNER_STATE", str(tmp_path / "runner"))
    monkeypatch.setenv("TT_RUNNER_SECRET", str(tmp_path / "secret"))
    monkeypatch.setenv("TT_RUNNER_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("TT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TT_TMUX_SOCKET", "tt-cleanup-" + uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location("isolated_runner", root / "tt-runner.py")
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    r.ensure_dirs()
    monkeypatch.setattr(r, "tt_comment", lambda *a: None)
    monkeypatch.setattr(r, "tt_progress", lambda *a: True)
    return r


def entry(r, status="running"):
    e = {"issue_id": "TEST", "dispatch_id": 1, "session": "tt-test-1", "round": 1,
         "status": status, "started": time.time()}
    r.save_runs({"TEST#1": e})
    return e


@pytest.mark.parametrize("physical", ["missing", "idle"])
def test_stall_check_detects_gone_work_without_heartbeat(runner, monkeypatch, physical):
    e = entry(runner)
    calls = []
    monkeypatch.setattr(runner, "session_work_state", lambda s: physical, raising=False)
    monkeypatch.setattr(runner, "tt_progress", lambda i, d, p: calls.append(p) or True)
    assert runner.stall_check("TEST#1", e, time.time())
    assert runner.get_run("TEST#1")["status"] == "failed"
    assert [p["state"] for p in calls] == ["failed"]


@pytest.mark.parametrize("physical,kill", [("idle", True), ("busy", False), ("unknown", False)])
def test_watch_sweeps_only_idle_terminal_sessions(runner, monkeypatch, physical, kill):
    entry(runner, "done")
    calls = []
    monkeypatch.setattr(runner, "session_work_state", lambda s: physical, raising=False)
    monkeypatch.setattr(runner, "tmux_run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0))
    runner.watch_once()
    assert any(a[0] == "kill-session" for a in calls) == kill


def test_cleanup_preserves_session_with_another_active_owner(runner, monkeypatch):
    e = entry(runner, "done")
    runner.save_runs({"TEST#1": e, "OTHER#2": {**e, "issue_id": "OTHER", "dispatch_id": 2, "status": "queued"}})
    calls = []
    monkeypatch.setattr(runner, "session_work_state", lambda s: "idle", raising=False)
    monkeypatch.setattr(runner, "tmux_run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0))
    runner.watch_once()
    assert not any(a[0] == "kill-session" for a in calls)


@pytest.mark.parametrize("panes,processes,expected", [
    ("11\t0\n", "11 1 zsh\n", "idle"),
    ("11\t1\n", "", "idle"),
    ("11\t0\n", "11 1 zsh\n12 11 sleep\n", "busy"),
    ("11\t0\n", "11 1 python3\n", "busy"),
    ("11\t1\n22\t0\n", "22 1 bash\n23 22 python3\n", "busy"),
    ("11\t0\n", "", "unknown"),
    ("bad-output", "", "unknown"),
])
def test_session_process_observation(runner, monkeypatch, panes, processes, expected):
    monkeypatch.setattr(runner, "session_alive", lambda s: True)
    monkeypatch.setattr(runner, "tmux_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, panes, ""))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, processes, ""))
    assert runner.session_work_state("tt-test-1") == expected


def test_gone_work_observation_cannot_finalize_next_round(runner, monkeypatch):
    e = entry(runner)
    def observe(s):
        runner.save_runs({"TEST#1": {**e, "round": 2, "session": "tt-test-1-r2"}})
        return "idle"
    calls = []
    monkeypatch.setattr(runner, "session_work_state", observe, raising=False)
    monkeypatch.setattr(runner, "tt_progress", lambda *a: calls.append(a) or True)
    runner.stall_check("TEST#1", e, time.time())
    assert runner.get_run("TEST#1")["status"] == "running"
    assert calls == []


@pytest.mark.parametrize("stderr,expected", [
    ("can't find session: tt-test-1", "missing"),
    ("no server running on socket", "missing"),
    ("permission denied", "unknown"),
])
def test_missing_session_is_distinct_from_inspection_failure(runner, monkeypatch, stderr, expected):
    monkeypatch.setattr(runner, "tmux_run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", stderr))
    assert runner.session_work_state("tt-test-1") == expected


def test_cleanup_refuses_unmanaged_session_name(runner, monkeypatch):
    e = entry(runner, "done")
    e["session"] = "user-shell"
    runner.save_runs({"TEST#1": e})
    monkeypatch.setattr(runner, "session_work_state", lambda s: pytest.fail("must not inspect unmanaged session"))
    assert not runner.cleanup_terminal_session("TEST#1", e)


@pytest.mark.parametrize("busy", [False, True])
def test_isolated_dispatch_terminal_tmux_measurement(client, hook_server, runner, monkeypatch, busy):
    """Real private tmux + local hook + isolated server API; no installed services."""
    if not shutil.which("tmux"):
        pytest.skip("tmux unavailable; production/real tmux remains unverified")
    from conftest import mk
    from test_api import reg_agent, make_dispatch, progress_post

    i = mk(client, title="isolated terminal cleanup")
    reg_agent(client, hook_server)
    did = make_dispatch(client, i["id"])
    key = f"{i['id']}#{did}"
    session = f"tt-test-{did}"
    command = "/bin/sh -c 'sleep 60'" if busy else "/bin/sh -i"
    try:
        r = runner.tmux_run("new-session", "-d", "-s", session, command,
                            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        expected = "busy" if busy else "idle"
        deadline = time.time() + 5
        while runner.session_work_state(session) != expected and time.time() < deadline:
            time.sleep(0.05)
        assert runner.session_work_state(session) == expected
        e = {"status": "running", "issue_id": i["id"], "dispatch_id": did,
             "session": session, "round": 1, "started": time.time()}
        runner.save_runs({key: e})
        assert progress_post(client, i["id"], did, {"state": "running", "session": session}).status_code == 200
        assert len(client.get("/agents/active").json()) == 1
        # Move to a later issue round and close it while dispatch is still running.
        for state in ("in_progress", "review", "cancelled"):
            assert client.patch(f"/issues/{i['id']}", json={"state": state}).status_code == 200
        assert client.get("/agents/active").json() == []
        monkeypatch.setattr(runner, "tt_progress", lambda iid, d, p:
                            progress_post(client, iid, d, p).status_code == 200)
        if busy:
            runner.save_runs({key: {**e, "status": "done"}})
        runner.watch_once()
        assert runner.session_alive(session) == busy
        assert client.get("/agents/active").json() == []
        if not busy:
            assert runner.get_run(key)["status"] == "failed"
        print(f"isolated experiment: active_before=1 active_after=0 tmux_remaining={int(busy)} live_work={busy}")
    finally:
        runner.tmux_run("kill-server", capture_output=True)
