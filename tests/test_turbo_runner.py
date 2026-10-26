"""Headless runner: command lines, fallback, git, and the question grace."""

import json
import subprocess
import sys
import time
from pathlib import Path

from turbo.app import create_app
from turbo.drivers import parse_session, resume_plan, start_plan
from turbo.http_api import HttpAPI
from turbo.proc import GitCli, SubprocessProc, parse_porcelain
from turbo.runner import DirectAPI, Runner
from turbo import service

BODY = "Implement the gate."
CONTRACT = "The runner submits."
SCOPE = {
    "paths": ["turbo/"],
    "forbidden": [],
    "verify": [{"cmd": "pytest -q", "kind": "unit"}],
}


class Clock:
    def __init__(self, t=1_700_000_000):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedProc:
    def __init__(self, script):
        self.script = list(script)
        self.spawns = []
        self.live = {}
        self.n = 0

    def spawn(self, argv):
        spec = self.script.pop(0)
        self.n += 1
        self.spawns.append(list(argv))
        self.live[self.n] = {
            "code": spec.get("code", 0),
            "output": spec.get("output", ""),
            "alive": spec.get("alive", False),
        }
        return self.n

    def poll(self, handle):
        item = self.live[handle]
        return None if item["alive"] else item["code"]

    def interrupt(self, handle):
        self.live[handle]["alive"] = False
        self.live[handle]["code"] = -15

    def output(self, handle):
        return self.live[handle]["output"]


class FakeGit:
    def __init__(self):
        self.calls = []
        self.paths = ["turbo/app.py"]
        self.sha = "ab" * 20

    def changed_paths(self):
        self.calls.append(["git", "status", "--porcelain"])
        return list(self.paths)

    def commit_all(self, message):
        self.calls.append(["git", "commit", "-m", message])
        return self.sha

    def open_pr(self, title, body, base):
        self.calls.append(["gh", "pr", "create", "--title", title, "--base", base])
        return {"provider": "github", "url": "https://example.com/org/repo/pull/4",
                "number": 4, "base": base, "sha": self.sha}

    def merge_pr(self, number, sha):
        self.calls.append(["gh", "pr", "merge", str(number), "--merge", sha])


def world(tmp_path, proc, harnesses, verify=None):
    clock = Clock()
    app = create_app(str(tmp_path / "turbo.db"), verify=verify or (lambda cmd, cwd: (0, "ok")), clock=clock)
    store = app.state.store
    service.register_actor(store, "ada", "human", None, None)
    service.register_actor(store, "run-1", "runner", harnesses, "ada")
    service.register_actor(store, "worker", "agent", None, "ada")
    service.register_actor(store, "probe", "probe", None, "ada")
    api = DirectAPI(store, app.state.verify, clock)
    runner = Runner(
        api, proc, FakeGit(), "run-1", "worker", harnesses,
        str(tmp_path / "state"), clock,
    )
    return app, api, runner, clock


def new_card(api_store, harness):
    return service.create_card(
        api_store, "ada", "gate", BODY, CONTRACT, harness, SCOPE, Clock(),
    )


def test_claude_and_codex_commands(tmp_path):
    proc = ScriptedProc([
        {"code": 0, "output": json.dumps({"session_id": "from-output"})},
        {"code": 0, "output": json.dumps({"type": "thread.started", "thread_id": "th_9"}) + "\n"},
    ])
    _app, api, runner, _clock = world(
        tmp_path, proc, ["claude", "codex", "cursor", "grok", "grok-bot", "opencode", "hermes"],
    )
    claude = new_card(api.store, "claude")
    runner.start_card(claude["id"])
    argv = proc.spawns[0]
    assert argv[:2] == ["claude", "-p"]
    assert "--session-id" in argv
    assert "--append-system-prompt-file" in argv
    assert "--output-format" in argv and "json" in argv
    assert BODY in argv[2]
    assert "nonce:" in argv[2]
    runner.reap(claude["id"])
    sid = argv[argv.index("--session-id") + 1]
    assert api.get(claude["id"])["session"]["session_id"] == sid

    service.register_actor(api.store, "run-2", "runner", ["codex"], "ada")
    codex_runner = Runner(
        api, proc, runner.git, "run-2", "worker", ["codex"], runner.state_dir, runner.clock,
    )
    codex = new_card(api.store, "codex")
    codex_runner.start_card(codex["id"])
    cx = proc.spawns[1]
    assert cx[:3] == ["codex", "exec", "--json"]
    assert any(part.startswith('developer_instructions="') for part in cx)
    codex_runner.reap(codex["id"])
    assert api.get(codex["id"])["session"]["session_id"] == "th_9"


def test_resume_falls_back_to_fork(tmp_path):
    proc = ScriptedProc([
        {"code": 1, "output": "no rollout"},
        {"code": 0, "output": json.dumps({"type": "thread.started", "thread_id": "th_fork"})},
    ])
    _app, api, runner, _clock = world(tmp_path, proc, ["codex"])
    card = new_card(api.store, "codex")
    service.claim(api.store, card["id"], "run-1", "worker", runner.clock)
    service.ask(api.store, card["id"], "worker", "Pick?", ["a", "b"], "a", runner.clock)
    service.turn_closed(api.store, card["id"], "run-1", runner.clock)
    service.answer(api.store, card["id"], "ada", "b", runner.clock)
    pending = api.pending("run-1")
    runner.start_resume(pending[0])
    assert proc.spawns[0][:4] == ["codex", "exec", "resume", proc.spawns[0][3]]
    assert "--json" in proc.spawns[0]
    assert proc.spawns[0][4].startswith("[tt resume]")
    runner.reap(card["id"])
    assert "fork" in proc.spawns[1]
    runner.reap(card["id"])
    assert api.get(card["id"])["session"]["session_id"] == "th_fork"


def test_question_grace_interrupts_without_tmux(tmp_path):
    proc = ScriptedProc([{"code": 0, "output": "", "alive": True}])
    _app, api, runner, clock = world(tmp_path, proc, ["claude"])
    card = new_card(api.store, "claude")
    service.claim(api.store, card["id"], "run-1", "worker", clock)
    service.ask(api.store, card["id"], "worker", "Pick?", ["a", "b"], "a", clock)
    handle = proc.spawn(["claude", "-p", "still going"])
    runner.handles[card["id"]] = {
        "handle": handle, "phase": "start", "attempt": "start",
        "prompt": "still going", "harness": "claude", "session_id": "s",
    }
    clock.t += 121
    result = runner.reap(card["id"])
    assert result["interrupted"] is True
    assert api.get(card["id"])["state"] == "awaiting-human"
    assert proc.live[handle]["code"] == -15


def test_cursor_create_chat_then_resume_and_uninvestigated_handoff(tmp_path):
    proc = ScriptedProc([
        {"code": 0, "output": "chat_123\n"},
        {"code": 0, "output": ""},
    ])
    _app, api, runner, _clock = world(tmp_path, proc, ["cursor", "opencode", "hermes"])
    cursor = new_card(api.store, "cursor")
    runner.start_card(cursor["id"])
    assert proc.spawns[0] == ["agent", "create-chat"]
    runner.reap(cursor["id"])
    assert proc.spawns[1][:4] == ["agent", "-p", "--resume", "chat_123"]
    assert "--trust" in proc.spawns[1]
    runner.reap(cursor["id"])
    assert api.get(cursor["id"])["session"]["session_id"] == "chat_123"

    service.register_actor(api.store, "run-2", "runner", ["opencode"], "ada")
    other = Runner(
        api, proc, runner.git, "run-2", "worker", ["opencode"], runner.state_dir, runner.clock,
    )
    opencode = new_card(api.store, "opencode")
    started = other.start_card(opencode["id"])
    assert started["mode"] == "handoff"
    assert started["argv"] is None
    assert api.get(opencode["id"])["session"]["mode"] == "handoff"
    assert len(proc.spawns) == 2


def test_grok_bot_webhook_does_not_log_the_bearer(tmp_path):
    proc = ScriptedProc([])
    _app, api, runner, _clock = world(tmp_path, proc, ["grok-bot"])
    posts = []

    def post(url, body, headers):
        posts.append((url, body, headers))
        return 200

    runner.webhook_url = "http://127.0.0.1:9/hook"
    runner.bearer = "local-test-bearer"
    runner.webhook_post = post
    card = new_card(api.store, "grok-bot")
    runner.start_card(card["id"])
    assert posts[0][0] == "http://127.0.0.1:9/hook"
    assert posts[0][2]["authorization"] == "Bearer local-test-bearer"
    assert posts[0][1]["card_id"] == card["id"]
    assert all("local-test-bearer" not in line for line in runner.logs)
    assert proc.spawns == []
    assert api.get(card["id"])["session"]["mode"] == "webhook"


def test_runner_commits_opens_pr_and_submits(tmp_path):
    proc = ScriptedProc([])
    _app, api, runner, clock = world(tmp_path, proc, ["claude"])
    card = new_card(api.store, "claude")
    claimed = service.claim(api.store, card["id"], "run-1", "worker", clock)
    service.ack(
        api.store, card["id"], "worker",
        __import__("turbo.gate", fromlist=["digest"]).digest(
            claimed["body"], claimed["contract"], claimed["contract_version"], claimed["scope"]),
        claimed["nonce"], clock,
    )
    service.ready(api.store, card["id"], "worker", "", clock)
    done = runner.submit_ready(card["id"])
    assert done["state"] == "review"
    assert done["verify_level"] == "unit-test-verified"
    assert done["sha"] == "ab" * 20
    flat = [" ".join(call) if isinstance(call, list) else str(call) for call in runner.git.calls]
    assert any(item.startswith("git commit") for item in flat)
    assert any(item.startswith("gh pr create") for item in flat)
    service.verdict(api.store, card["id"], "probe", "approve", "", clock)
    merged = runner.merge_card(card["id"])
    assert merged["state"] == "merged"
    assert any(item.startswith("gh pr merge 4") for item in
               [" ".join(call) for call in runner.git.calls])


def test_parse_and_plans():
    assert parse_session("codex", '{"type":"thread.started","thread_id":"th_1"}\n') == "th_1"
    assert parse_session("claude", '{"session_id":"s1"}', "pre") == "pre"
    assert parse_session("grok", '{"sessionId":"g1"}') == "g1"
    assert parse_session("cursor", " chat \n") == "chat"
    plan = resume_plan("claude", "s1", "hello", "/tmp/system.txt")
    assert plan.argv[:4] == ["claude", "-p", "hello", "--resume"]
    assert "--fork-session" not in plan.argv
    forked = resume_plan("claude", "s1", "hello", "/tmp/system.txt", attempt="fork")
    assert "--fork-session" in forked.argv
    grok = start_plan("grok", "s2", "hello", "/tmp/system.txt")
    assert grok.argv[0] == "grok" and "-s" in grok.argv and "--rules" in grok.argv
    assert resume_plan("hermes", None, "hello", "/tmp/x").mode == "handoff"
    assert resume_plan("opencode", "sid", "hello", "/tmp/x").argv is None


def test_git_cli_command_sequence(tmp_path):
    calls = []

    def run(argv, cwd, capture_output, text, check):
        calls.append(argv)
        stdout = ""
        if argv[:2] == ["git", "status"]:
            stdout = " M turbo/app.py\n?? turbo/new.py\nR  old.py -> turbo/renamed.py\n"
        elif argv[:2] == ["git", "rev-parse"]:
            stdout = ("cd" * 20) + "\n"
        elif argv[:3] == ["gh", "pr", "create"]:
            stdout = "https://example.com/org/repo/pull/12\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    git = GitCli(str(tmp_path), run=run)
    assert git.changed_paths() == ["turbo/app.py", "turbo/new.py", "turbo/renamed.py"]
    assert git.commit_all("turbo card") == "cd" * 20
    pr = git.open_pr("title", "body", "main")
    assert pr["number"] == 12
    assert pr["sha"] == "cd" * 20
    git.merge_pr(12, pr["sha"])
    assert calls[0] == ["git", "status", "--porcelain"]
    assert ["git", "add", "-A"] in calls
    assert ["git", "push", "-u", "origin", "HEAD"] in calls
    assert ["gh", "pr", "merge", "12", "--merge"] in calls
    assert parse_porcelain(' M a.py\n') == ["a.py"]


def test_http_api_shapes_requests():
    seen = []

    class Resp:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/next?actor=run-1"):
            return Resp({"card": {"id": "T1", "harness": "claude"}})
        return Resp({"cards": [{"id": "T2"}]})

    api = HttpAPI("http://127.0.0.1:9", "run-1", opener=opener)
    assert api.next_open("run-1")["id"] == "T1"
    assert api.pending("run-1")[0]["id"] == "T2"
    assert seen[0][0] == "GET"


def test_subprocess_proc_runs_a_local_command():
    proc = SubprocessProc()
    handle = proc.spawn([sys.executable, "-c", "print('turbo-ok')"])
    for _ in range(100):
        if proc.poll(handle) is not None:
            break
        time.sleep(0.02)
    assert proc.poll(handle) == 0
    assert "turbo-ok" in proc.output(handle)


def test_turbo_package_has_no_tmux_or_wake_relay():
    text = "\n".join(path.read_text(encoding="utf-8") for path in Path("turbo").glob("*.py"))
    assert "tmux" not in text
    assert "send-keys" not in text
    assert not Path("scripts/tt-wake-relay.py").exists()
