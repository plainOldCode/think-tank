"""Real CLI + local HTTP server; no deployed board or agent is contacted."""
import json
import os
import pathlib
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from app import create_app

CLI = pathlib.Path(__file__).resolve().parents[1] / "cli" / "tt"


@pytest.fixture
def cli(tmp_path):
    app = create_app(str(tmp_path / "cli.db"))
    writes = []

    @app.middleware("http")
    async def track(request, call_next):
        if request.method == "PATCH":
            writes.append(request.url.path)
        return await call_next(request)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    env = {**os.environ, "TT_URL": f"http://127.0.0.1:{sock.getsockname()[1]}", "TT_AGENT": "cli-test"}

    def run(*args):
        return subprocess.run(["bash", str(CLI), *args], env=env, capture_output=True, text=True, timeout=15)

    try:
        yield run, writes
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


def new(run):
    return json.loads(run("new", "CLI task", "--json").stdout)["id"]


def test_done_json_stays_json_when_demoted(cli):
    run, _ = cli
    iid = new(run)
    run("claim", iid)
    response = run("done", iid, "--json")
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["state"] == "review"


def test_failed_done_is_sent_once_and_keeps_server_reason(cli):
    run, writes = cli
    iid = new(run)
    response = run("done", iid)
    assert response.returncode != 0
    assert "illegal transition" in response.stderr
    assert writes == [f"/issues/{iid}"]


def test_comment_failure_prevents_done_request(cli):
    run, writes = cli
    response = run("done", "nonexistent", "report")
    assert response.returncode != 0
    assert writes == []


def test_contract_reaches_human_and_json_cli(cli):
    run, _ = cli
    contract = json.loads(run("contract", "--json").stdout)
    iid = new(run)
    claimed = run("claim", iid)
    assert contract["version"] in claimed.stderr
    assert contract["instructions"] in claimed.stderr
    run("state", iid, "todo")
    pulled = json.loads(run("pull", "--json").stdout)
    assert pulled["work_contract"] == contract


@pytest.mark.parametrize("via_verify", [False, True])
def test_report_file_roundtrip(cli, tmp_path, via_verify):
    run, _ = cli
    iid = new(run)
    issue = json.loads(run("claim", iid, "--json").stdout)
    # Execute a real validation command and attach its output as alternative evidence.
    validation = subprocess.run(["bash", "-n", str(CLI)], capture_output=True, text=True)
    assert validation.returncode == 0
    proof = {"contract_version": issue["work_contract"]["version"], "attempt": issue["execution_attempt"],
             "method": "alternative", "reason": "syntax-only validation fixture",
             "command": "bash -n cli/tt", "result": "passed", "evidence": "exit=0"}
    path = tmp_path / "report with spaces.json"
    path.write_text(json.dumps(proof))
    if via_verify:
        run("done", iid)
    result = run("verify" if via_verify else "done", iid, "--report", str(path), "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["state"] == "done" and data["verification_status"] == "reported"
    assert data["completion_report"]["evidence"] == "exit=0"
