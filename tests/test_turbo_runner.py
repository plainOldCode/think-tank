"""Headless runner: command lines, fallback, git, and the question grace."""

import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

from turbo.app import create_app
from turbo.drivers import can_fork, parse_session, resume_plan, start_plan
from turbo.errors import TurboError
from turbo.http_api import HttpAPI
from turbo.proc import GitCli, SubprocessProc, parse_porcelain
from turbo.runner import Runner, _post_webhook, main
from turbo import service


class DirectAPI:
    """In-process calls for these tests. Scenarios use HttpAPI."""

    def __init__(self, store, verify, clock):
        self.store = store
        self.verify = verify
        self.clock = clock

    def claim(self, card_id, actor, agent):
        return service.claim(self.store, card_id, actor, agent, self.clock)

    def get(self, card_id, reveal=False):
        return service.get_card(self.store, card_id, actor=None if not reveal else self._runner(card_id))

    def _runner(self, card_id):
        card = self.store.get(card_id)
        return card["assignee"] or None

    def apply_defaults(self, actor, now):
        return service.apply_defaults(self.store, actor, lambda: now)

    def attach_session(self, card_id, actor, session_id, driver, mode):
        return service.attach_session(
            self.store, card_id, actor, session_id, driver, mode, self.clock)

    def turn_closed(self, card_id, actor):
        return service.turn_closed(self.store, card_id, actor, self.clock)

    def resume_started(self, card_id, actor, token, session_id, mode):
        return service.resume_started(
            self.store, card_id, actor, token, session_id, mode, self.clock)

    def submit(self, card_id, actor, sha, pr, changed, token):
        return service.submit(
            self.store, card_id, actor, sha, pr, changed, token, self.verify, self.clock)

    def merge(self, card_id, actor):
        return service.merge(self.store, card_id, actor, self.clock)

    def next_open(self, actor):
        return service.next_open(self.store, actor)

    def pending(self, actor):
        return service.pending_work(self.store, actor)

    def live(self, card_ids):
        self.live_cards = list(card_ids)
        return {"cards": list(card_ids)}

BODY = "Implement the gate."
CONTRACT = "The runner submits."
SCOPE = {
    "paths": ["turbo/"],
    "forbidden": [],
    "verify": [{"cmd": "pytest -q", "kind": "unit"}],
    "exempt": True,
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
    app = create_app(str(tmp_path / "turbo.db"), verify=verify or (lambda cmd, cwd, timeout=120: (0, "ok")), clock=clock)
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


def test_cursor_create_chat_failure_hands_off(tmp_path):
    proc = ScriptedProc([{"code": 1, "output": "no chat"}])
    _app, api, runner, _clock = world(tmp_path, proc, ["cursor"])
    cursor = new_card(api.store, "cursor")
    runner.start_card(cursor["id"])
    assert runner.reap(cursor["id"]) == {"running": False, "fallback": "handoff"}
    assert api.get(cursor["id"])["session"]["mode"] == "handoff"
    assert cursor["id"] not in runner.handles


def test_reap_without_a_session_id(tmp_path):
    proc = ScriptedProc([{"code": 0, "output": ""}])
    _app, api, runner, _clock = world(tmp_path, proc, ["codex"])
    card = new_card(api.store, "codex")
    runner.start_card(card["id"])
    result = runner.reap(card["id"])
    assert result["session_id"] is None
    assert not (api.get(card["id"]).get("session") or {}).get("session_id")


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
    assert parse_session("codex", "\nnot-json\n{\"type\":\"other\"}\n") is None
    assert parse_session("claude", "not json") is None
    assert parse_session("claude", "[]") is None
    assert parse_session("claude", '{"session_id":"s9"}') == "s9"
    assert parse_session("hermes", "{}") is None
    grok_fork = resume_plan("grok", "s", "hi", "/tmp/system.txt", attempt="fork")
    assert grok_fork.argv[0] == "grok" and "--fork-session" in grok_fork.argv
    grok_resume = resume_plan("grok", "s", "hi", "/tmp/system.txt")
    assert "-r" in grok_resume.argv
    cursor = resume_plan("cursor", "s", "hi", "/tmp/system.txt")
    assert cursor.argv[:4] == ["agent", "-p", "--resume", "s"]
    assert can_fork("cursor") is False
    assert resume_plan("grok-bot", "s", "hi", "/tmp/system.txt").mode == "webhook"
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
    assert parse_porcelain("x\n M a.py\n") == ["a.py"]
    def quiet_run(argv, cwd, capture_output, text, check):
        del cwd, capture_output, text, check
        return subprocess.CompletedProcess(argv, 0, "", "")

    quiet = GitCli(str(tmp_path), run=quiet_run)
    assert quiet.open_pr("title", "body", "main")["number"] is None
    with pytest.raises(RuntimeError):
        quiet.merge_pr(1, "")


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

    class Plain(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://127.0.0.1:9/turbo/cards/nope", 500, "err", {}, None)

        def read(self):
            return b"plain text"

    def plain_opener(_req, timeout=60):
        del timeout
        raise Plain()

    plain = HttpAPI("http://127.0.0.1:9", "run-1", opener=plain_opener)
    with pytest.raises(TurboError) as caught:
        plain.get("nope")
    assert caught.value.status == 500
    assert caught.value.message == "plain text"


def test_subprocess_proc_runs_a_local_command():
    proc = SubprocessProc()
    handle = proc.spawn([sys.executable, "-c", "print('turbo-ok')"])
    for _ in range(100):
        if proc.poll(handle) is not None:
            break
        time.sleep(0.02)
    assert proc.poll(handle) == 0
    assert "turbo-ok" in proc.output(handle)
    sleeper = proc.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.interrupt(sleeper)
    for _ in range(50):
        if proc.poll(sleeper) is not None:
            break
        time.sleep(0.02)
    assert proc.poll(sleeper) is not None
    os.remove(proc._files[sleeper])
    assert proc.output(sleeper) == ""


def test_turbo_package_has_no_tmux_or_wake_relay():
    text = "\n".join(path.read_text(encoding="utf-8") for path in Path("turbo").glob("*.py"))
    assert "tmux" not in text
    assert "send-keys" not in text
    assert not Path("scripts/tt-wake-relay.py").exists()
    for gone in ("server", "runner", "skills", "cli", "daemon", "docs", "metrics"):
        assert not Path(gone).exists(), gone
    assert Path("deploy/com.tt.server.plist").is_file()
    assert Path("scripts/deploy.sh").is_file()
    assert Path("scripts/secret-scan.sh").is_file()


def test_runner_refuses_a_harness_it_does_not_know(tmp_path):
    class API:
        def claim(self, card_id, actor, agent):
            del actor, agent
            return {
                "id": card_id, "harness": "nope", "body": "b", "contract": "c",
                "contract_version": 1, "scope": {}, "nonce": "n",
            }

    runner = Runner(API(), ScriptedProc([]), None, "run-1", "worker", ["claude"], str(tmp_path), Clock())
    with pytest.raises(KeyError):
        runner.start_card("card")

    with pytest.raises(KeyError):
        runner.reap("missing")
    with pytest.raises(TurboError) as caught:
        runner._webhook("card", "grok-bot", "hi")
    assert "webhook" in caught.value.message

    posted = {}

    def post(url, body, headers):
        posted["url"] = url
        posted["headers"] = dict(headers)
        return 204

    class Attach:
        def attach_session(self, *_args):
            return {}

    runner.api = Attach()
    runner.webhook_url = "http://example.invalid/hook"
    runner.webhook_post = post
    runner.bearer = ""
    runner._webhook("card", "grok-bot", "hi")
    assert "authorization" not in posted["headers"]


def test_direct_api_defaults_and_next_open(tmp_path):
    app = create_app(str(tmp_path / "turbo.db"))
    store = app.state.store
    service.register_actor(store, "ada", "human", None, None)
    service.register_actor(store, "run-1", "runner", ["claude"], "ada")
    service.create_card(store, "ada", "Other", BODY, CONTRACT, "grok", SCOPE, Clock())
    api = DirectAPI(store, lambda *_args: (0, ""), Clock())
    assert api.apply_defaults("run-1", Clock()())["applied"] == []
    assert api.next_open("run-1") is None


def test_runner_main_once_and_the_sleep_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("TURBO_ACTOR", "run-1")
    monkeypatch.setenv("TURBO_AGENT", "worker")
    monkeypatch.setenv("TURBO_HARNESSES", "claude")
    monkeypatch.setenv("TURBO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TURBO_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TURBO_GROK_BOT_WEBHOOK_URL", raising=False)
    ticks = []
    monkeypatch.setattr(Runner, "tick", lambda self: ticks.append(self.actor))
    main(["--once"])
    assert ticks == ["run-1"]

    monkeypatch.setattr(Runner, "tick", lambda self: None)

    def sleep(seconds):
        raise SystemExit(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    with pytest.raises(SystemExit) as caught:
        main(["--interval", "0.25"])
    assert caught.value.code == 0.25


def test_post_webhook_sends_the_body(monkeypatch):
    seen = {}

    class Res:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(req, timeout=30):
        del timeout
        seen["data"] = req.data
        seen["auth"] = req.get_header("Authorization")
        return Res()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    status = _post_webhook(
        "http://example.invalid/hook",
        {"card_id": "c"},
        {"authorization": "Bearer secret"},
    )
    assert status == 202
    assert b"card_id" in seen["data"]
    assert seen["auth"] == "Bearer secret"
