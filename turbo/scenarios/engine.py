"""Run JSON scenarios against a real turbo server and the real runner.

Harness commands are built by turbo.drivers and then captured. Nothing is
executed: claude, codex, grok, and the cursor agent binary are never
started. git and gh are the real GitCli argument lists with a stubbed
runner, so no remote is contacted.
"""

import argparse
import json
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from turbo.app import create_app
from turbo.drivers import HARNESSES
from turbo.errors import TurboError
from turbo.gate import digest
from turbo.http_api import HttpAPI
from turbo.legacy import open_ro
from turbo.legacy_schema import create_throwaway
from turbo.proc import GitCli
from turbo.runner import Runner

_HARNESS_BINARIES = {"claude", "codex", "grok", "agent"}
_PASS_SHA = "a" * 40


class Clock:
    def __init__(self, t: float = 1_700_000_000):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeProc:
    """Records harness argv and returns scripted exits. Never execs."""

    def __init__(self):
        self.queue: list[dict] = []
        self.spawns: list[list[str]] = []
        self.live: dict[int, dict] = {}
        self.n = 0
        self.interrupts = 0

    def arm(self, spec: dict) -> None:
        self.queue.append(spec)

    def spawn(self, argv: list[str]) -> int:
        if not argv or argv[0] not in _HARNESS_BINARIES:
            raise RuntimeError("refusing to spawn " + " ".join(argv[:3]))
        if not self.queue:
            raise RuntimeError("no scripted harness behavior for: " + " ".join(argv[:6]))
        spec = self.queue.pop(0)
        self.n += 1
        self.spawns.append(list(argv))
        self.live[self.n] = {
            "code": int(spec.get("exit", 0)),
            "output": spec.get("output") or "",
            "alive": bool(spec.get("alive", False)),
        }
        self._note_nonce(argv)
        return self.n

    def poll(self, handle: int) -> int | None:
        item = self.live[handle]
        return None if item["alive"] else item["code"]

    def interrupt(self, handle: int) -> None:
        self.interrupts += 1
        self.live[handle]["alive"] = False
        self.live[handle]["code"] = -15

    def output(self, handle: int) -> str:
        return self.live[handle]["output"]

    def release(self, code: int, output: str | None) -> None:
        released = False
        for item in self.live.values():
            if item["alive"]:
                item["alive"] = False
                item["code"] = code
                if output is not None:
                    item["output"] = output
                released = True
        if not released:
            raise RuntimeError("no live harness to release")

    def _note_nonce(self, argv: list[str]) -> None:
        for arg in argv:
            for line in str(arg).splitlines():
                if line.startswith("nonce: "):
                    self.last_nonce = line.split(" ", 1)[1].strip()
                    return

    last_nonce: str = ""


class GitStub:
    """GitCli with a local run() so push, commit, and gh stay in-process."""

    def __init__(self, cwd: str, changed: list[str], sha: str, pr_number: int):
        self.changed = list(changed)
        self.sha = sha
        self.pr_number = pr_number
        self.calls: list[list[str]] = []
        self.cli = GitCli(cwd, run=self.run)

    def run(self, argv, cwd=None, capture_output=False, text=False, check=False):
        del cwd, capture_output, text, check
        self.calls.append(list(argv))
        if not argv or argv[0] not in {"git", "gh"}:
            raise RuntimeError("refusing to run " + " ".join(argv[:3]))
        stdout = ""
        if argv[:2] == ["git", "status"]:
            stdout = "".join(f" M {path}\n" for path in self.changed)
        elif argv[:2] == ["git", "rev-parse"]:
            stdout = self.sha + "\n"
        elif argv[:3] == ["gh", "pr", "create"]:
            stdout = f"https://example.invalid/think-tank/pull/{self.pr_number}\n"
        elif argv[:3] == ["gh", "pr", "merge"]:
            stdout = ""
        elif argv[0] == "git" and argv[1] in {"add", "commit", "push"}:
            stdout = ""
        else:
            raise RuntimeError("unexpected git command: " + " ".join(argv))
        return subprocess.CompletedProcess(argv, 0, stdout, "")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, app):
        port = _free_port()
        self.base = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=0.2) as res:
                    if res.status == 200:
                        return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("turbo server did not start")

    def __exit__(self, exc_type, exc, tb):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        return False


def _request(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return res.status, parsed if isinstance(parsed, dict) else {"value": parsed}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": raw}
        if not isinstance(parsed, dict):
            parsed = {"error": raw}
        return exc.code, parsed


def _snapshot(path: str) -> tuple:
    con = sqlite3.connect(path)
    try:
        issues = [tuple(row) for row in con.execute("SELECT * FROM issues ORDER BY id")]
        comments = [tuple(row) for row in con.execute("SELECT * FROM comments ORDER BY id")]
        schema = [tuple(row) for row in con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
        return issues, comments, schema
    finally:
        con.close()


def _seed_legacy(path: str, fixture: dict) -> tuple:
    create_throwaway(path)
    con = sqlite3.connect(path)
    try:
        issue_cols = {row[1] for row in con.execute("PRAGMA table_info(issues)")}
        for issue in fixture.get("issues") or []:
            data = {key: value for key, value in issue.items() if key in issue_cols}
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            con.execute(f"INSERT INTO issues ({cols}) VALUES ({marks})", list(data.values()))
        for comment in fixture.get("comments") or []:
            con.execute(
                "INSERT INTO comments (issue_id, author, body, ts) VALUES (?, ?, ?, ?)",
                (comment["issue_id"], comment["author"], comment["body"], comment["ts"]),
            )
        con.commit()
    finally:
        con.close()
    return _snapshot(path)


def load_scenarios(paths: list[Path]) -> list[tuple[Path, dict]]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(item for item in path.glob("*.json") if item.is_file()))
        else:
            files.append(path)
    out = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError(f"{file} has a scenario without a name")
            out.append((file, item))
    return out


class Run:
    def __init__(self, base: str, scenario: dict, proc: FakeProc, git: GitStub, runner: Runner, legacy_path: str | None):
        self.base = base
        self.scenario = scenario
        self.proc = proc
        self.git = git
        self.runner = runner
        self.legacy_path = legacy_path
        self.cards: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.old_tokens: dict[str, str] = {}
        self.nonces: dict[str, str] = {}
        self.status: int | None = None
        self.error = ""
        self.body: dict = {}
        self.reason = ""
        self._call_mark = 0
        self._interrupt_mark = 0

    def seed(self) -> None:
        human = ""
        for index, actor in enumerate(self.scenario.get("actors") or []):
            body = {"name": actor["name"], "role": actor["role"]}
            if actor.get("harnesses"):
                body["harnesses"] = actor["harnesses"]
            if index:
                body["actor"] = human
            else:
                human = actor["name"]
                if actor["role"] != "human":
                    raise RuntimeError("the first actor must be a human")
            status, payload = _request(self.base, "POST", "/turbo/actors", body)
            if status != 200:
                raise RuntimeError(f"actor {actor['name']}: {status} {payload}")
        for card in self.scenario.get("cards") or []:
            status, payload = _request(self.base, "POST", "/turbo/cards", {
                "actor": human,
                "title": card["title"],
                "body": card["body"],
                "contract": card["contract"],
                "harness": card["harness"],
                "scope": card["scope"],
            })
            if status != 200:
                raise RuntimeError(f"card {card.get('ref')}: {status} {payload}")
            self.cards[card.get("ref") or card["title"]] = payload["id"]

    def card_id(self, step: dict) -> str:
        ref = step.get("card") or (self.scenario.get("cards") or [{}])[0].get("ref")
        if not ref or ref not in self.cards:
            raise RuntimeError("step needs a known card ref")
        return self.cards[ref]

    def card_ref(self, step: dict) -> str:
        return step.get("card") or (self.scenario.get("cards") or [{}])[0].get("ref")

    def _http(self, method: str, path: str, body: dict | None = None) -> None:
        self.status, self.body = _request(self.base, method, path, body)
        self.error = str(self.body.get("error") or "")

    def _catch_runner(self, fn) -> None:
        try:
            result = fn()
            self.status = 200
            self.body = result if isinstance(result, dict) else {}
            self.error = ""
        except TurboError as exc:
            self.status = exc.status
            self.body = {"error": exc.message}
            self.error = exc.message

    def nonce_for(self, ref: str) -> str:
        if self.proc.last_nonce:
            self.nonces.setdefault(ref, self.proc.last_nonce)
        nonce = self.nonces.get(ref) or ""
        if not nonce:
            raise RuntimeError("no nonce captured for " + ref)
        return nonce

    def ack(self, step: dict) -> None:
        ref = self.card_ref(step)
        cid = self.card_id(step)
        status, card = _request(self.base, "GET", f"/turbo/cards/{cid}")
        if status != 200:
            self.status, self.body, self.error = status, card, str(card.get("error") or "")
            return
        given = step.get("digest")
        if given == "bad":
            given = "0" * 64
        else:
            given = digest(card["body"], card["contract"], card["contract_version"], card["scope"])
        self._http("POST", f"/turbo/cards/{cid}/ack", {
            "actor": step.get("actor") or "worker",
            "digest": given,
            "nonce": self.nonce_for(ref),
        })
        if self.status == 200 and self.body.get("work_token"):
            self.tokens[ref] = self.body["work_token"]

    def step(self, spec: dict) -> None:
        self.reason = ""
        self._call_mark = len(self.git.calls)
        self._interrupt_mark = self.proc.interrupts
        if "git_changed" in spec:
            self.git.changed = list(spec["git_changed"])
        if "arm" in spec:
            self.proc.arm(spec["arm"])
        if "release" in spec:
            rel = spec["release"]
            self.proc.release(int(rel.get("exit", 0)), rel.get("output"))
        op = spec.get("op")
        if op == "tick":
            self.runner.tick()
            self.status, self.error = None, ""
        elif op == "advance":
            self.runner.clock.t += float(spec["seconds"])
            self.status = None
        elif op == "ack":
            self.ack(spec)
        elif op == "ready":
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/ready", {
                "actor": spec.get("actor") or "worker",
                "note": spec.get("note") or "",
            })
        elif op == "ask":
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/question", {
                "actor": spec.get("actor") or "worker",
                "blocked_question": spec["blocked_question"],
                "blocked_options": spec["blocked_options"],
                "blocked_default": spec["blocked_default"],
            })
        elif op == "answer":
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/answer", {
                "actor": spec.get("actor") or "ada",
                "choice": spec["choice"],
            })
        elif op == "edit":
            ref = self.card_ref(spec)
            if ref in self.tokens:
                self.old_tokens[ref] = self.tokens[ref]
            body: dict = {"actor": spec.get("actor") or "ada"}
            for key in ("body", "contract", "scope"):
                if key in spec:
                    body[key] = spec[key]
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/edit", body)
            if self.status == 200 and self.body.get("nonce"):
                self.nonces[ref] = self.body["nonce"]
        elif op == "verdict":
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/verdict", {
                "actor": spec.get("actor") or "probe",
                "result": spec["result"],
                "findings": spec.get("findings") or "",
            })
        elif op == "submit":
            cid = self.card_id(spec)
            self._catch_runner(lambda: self.runner.submit_ready(cid))
        elif op == "submit-raw":
            ref = self.card_ref(spec)
            token = spec.get("work_token", "")
            if token == "$old":
                token = self.old_tokens.get(ref, "")
            elif token == "$token":
                token = self.tokens.get(ref, "")
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/submit", {
                "actor": spec.get("actor") or self.runner.actor,
                "sha": spec.get("sha") or self.git.sha,
                "pr": spec.get("pr") or {},
                "changed_paths": spec.get("changed_paths") or list(self.git.changed),
                "work_token": token,
            })
        elif op == "merge":
            self._merge(spec)
        elif op == "create-card":
            human = self.scenario["actors"][0]["name"]
            self._http("POST", "/turbo/cards", {
                "actor": human,
                "title": spec["title"],
                "body": spec.get("body") or "Implement the gate.",
                "contract": spec.get("contract") or "The runner submits. The agent does not.",
                "harness": spec.get("harness") or "claude",
                "scope": spec["scope"],
            })
            if self.status == 200:
                self.cards[spec["ref"]] = self.body["id"]
        elif op == "claim":
            self._http("POST", f"/turbo/cards/{self.card_id(spec)}/claim", {
                "actor": spec["actor"],
                "agent": spec["agent"],
            })
        elif op == "claim-race":
            self._race(spec)
        elif op == "legacy-write":
            self._legacy_write()
        elif op == "legacy-get":
            self._http("GET", "/legacy/issues/" + spec.get("issue", "PARENT01"))
        elif op == "noop":
            self.status = None
        else:
            raise RuntimeError("unknown op " + str(op))
        self._expect(spec.get("expect") or {})

    def _merge(self, spec: dict) -> None:
        actor = spec.get("actor") or self.runner.actor
        via = spec.get("via") or ("runner" if actor == self.runner.actor else "http")
        cid = self.card_id(spec)
        if via == "runner":
            self._catch_runner(lambda: self.runner.merge_card(cid))
            return
        self._http("POST", f"/turbo/cards/{cid}/merge", {"actor": actor})

    def _race(self, spec: dict) -> None:
        cid = self.card_id(spec)
        claims = spec["claims"]
        barrier = threading.Barrier(len(claims))
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def one(claim: dict) -> None:
            barrier.wait()
            status, body = _request(self.base, "POST", f"/turbo/cards/{cid}/claim", {
                "actor": claim["actor"],
                "agent": claim["agent"],
            })
            with lock:
                results.append((status, body))

        threads = [threading.Thread(target=one, args=(claim,)) for claim in claims]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.status = None
        self.body = {"results": [{"status": status, "error": body.get("error", "")} for status, body in results]}
        self.error = ""
        self._race_results = results

    def _legacy_write(self) -> None:
        if not self.legacy_path:
            raise RuntimeError("scenario has no legacy fixture")
        con = open_ro(self.legacy_path)
        try:
            try:
                con.execute(
                    "INSERT INTO issues (id, title, body, created_at, updated_at) VALUES ('NOWRITE','t','b','t','t')"
                )
                self.status, self.error = 200, "legacy insert succeeded"
            except sqlite3.OperationalError as exc:
                self.status, self.error = 403, str(exc)
            self.body = {"error": self.error}
        finally:
            con.close()
        post_status, post_body = _request(self.base, "POST", "/legacy/issues", {"id": "NOWRITE"})
        self.body["post_status"] = post_status
        self.body["post_error"] = post_body.get("error", "")

    def _card(self, spec: dict) -> dict:
        expect = spec
        ref = expect.get("card")
        if not ref:
            cards = self.scenario.get("cards") or []
            ref = cards[0]["ref"] if cards else ""
        cid = self.cards[ref]
        status, body = _request(self.base, "GET", f"/turbo/cards/{cid}")
        if status != 200:
            raise RuntimeError(f"GET card {ref}: {status} {body}")
        return body

    def _expect(self, expect: dict) -> None:
        if not expect:
            return
        problems = []
        if "status" in expect and self.status != expect["status"]:
            problems.append(f"status {self.status} != {expect['status']} ({self.error})")
        if "error" in expect and expect["error"] not in self.error:
            problems.append(f"error {self.error!r} does not contain {expect['error']!r}")
        if "wins" in expect:
            results = getattr(self, "_race_results", [])
            wins = sum(1 for status, _body in results if status == 200)
            losses = sum(1 for status, _body in results if status == 409)
            if wins != expect["wins"] or losses != len(results) - expect["wins"]:
                problems.append(f"race results {[(s, b.get('error')) for s, b in results]}")
        if "post_status" in expect and self.body.get("post_status") != expect["post_status"]:
            problems.append(f"post status {self.body.get('post_status')}")
        card_keys = {
            "state", "verdict", "verify_level", "answer", "answer_by", "contract_version",
            "ready", "session_mode", "resume_contains", "same_session", "sha", "pr_number",
            "assignee", "agent", "rc_count",
        }
        card = None
        if card_keys & set(expect) or expect.get("card"):
            if "state" in expect or card_keys & set(expect):
                card = self._card(expect)
        if card is not None:
            for key in ("state", "verdict", "verify_level", "answer", "answer_by", "assignee", "agent", "sha"):
                if key in expect and card.get(key) != expect[key]:
                    problems.append(f"{key} {card.get(key)!r} != {expect[key]!r}")
            if "contract_version" in expect and card.get("contract_version") != expect["contract_version"]:
                problems.append(f"contract_version {card.get('contract_version')}")
            if "ready" in expect and bool(card.get("ready")) != bool(expect["ready"]):
                problems.append(f"ready {card.get('ready')}")
            if "rc_count" in expect and card.get("rc_count") != expect["rc_count"]:
                problems.append(f"rc_count {card.get('rc_count')}")
            if "session_mode" in expect:
                mode = (card.get("session") or {}).get("mode")
                if mode != expect["session_mode"]:
                    problems.append(f"session mode {mode!r} != {expect['session_mode']!r}")
            if "resume_contains" in expect:
                prompt = (card.get("resume") or {}).get("prompt") or ""
                if expect["resume_contains"] not in prompt:
                    problems.append(f"resume prompt missing {expect['resume_contains']!r}: {prompt!r}")
            if "pr_number" in expect:
                number = (card.get("pr") or {}).get("number")
                if number != expect["pr_number"]:
                    problems.append(f"pr number {number}")
            if expect.get("same_session"):
                sid = (card.get("session") or {}).get("session_id") or ""
                argv = self.proc.spawns[-1] if self.proc.spawns else []
                if not sid or sid not in argv:
                    problems.append(f"resume argv does not repeat session {sid!r}")
        if "spawns" in expect and len(self.proc.spawns) != expect["spawns"]:
            problems.append(f"spawns {len(self.proc.spawns)} != {expect['spawns']}")
        if "argv_contains" in expect:
            argv = self.proc.spawns[-1] if self.proc.spawns else []
            missing = [item for item in expect["argv_contains"] if not any(item in part for part in argv)]
            if missing:
                problems.append(f"argv missing {missing}: {argv[:8]}")
        if "argv0" in expect:
            argv0 = self.proc.spawns[-1][0] if self.proc.spawns else ""
            if argv0 != expect["argv0"]:
                problems.append(f"argv0 {argv0!r}")
        new_calls = self.git.calls[self._call_mark:]
        if expect.get("no_commit") and any("commit" in call for call in new_calls):
            problems.append("git commit ran")
        if expect.get("no_merge") and any(call[:3] == ["gh", "pr", "merge"] for call in new_calls):
            problems.append("gh pr merge ran")
        if "git_calls" in expect:
            for want in expect["git_calls"]:
                if not any(all(part in call for part in want) for call in new_calls):
                    problems.append(f"git calls missing {want}: {new_calls}")
        if expect.get("interrupted") and self.proc.interrupts == self._interrupt_mark:
            problems.append("harness was not interrupted")
        if "legacy_body" in expect:
            self._expect_legacy(expect, problems)
        if problems:
            self.reason = "; ".join(problems)

    def _expect_legacy(self, expect: dict, problems: list[str]) -> None:
        issue_id = expect.get("legacy_issue") or "PARENT01"
        status, payload = _request(self.base, "GET", "/legacy/issues/" + issue_id)
        if status != 200:
            problems.append(f"legacy GET {status}")
            return
        issue = payload.get("issue") or {}
        if issue.get("body") != expect["legacy_body"]:
            problems.append(f"legacy body {issue.get('body')!r}")
        if expect.get("legacy_read_only") and payload.get("read_only") is not True:
            problems.append("legacy payload is not marked read-only")
        children = [child.get("id") for child in payload.get("children") or []]
        if "legacy_children" in expect and children != expect["legacy_children"]:
            problems.append(f"children {children}")
        if "legacy_comment" in expect:
            texts = [item.get("body") for item in payload.get("comments") or []]
            if expect["legacy_comment"] not in texts:
                problems.append(f"comments {texts}")
        if "legacy_child_report" in expect:
            reports = [(child.get("report") or {}).get("summary") for child in payload.get("children") or []]
            if expect["legacy_child_report"] not in reports:
                problems.append(f"reports {reports}")
        if "legacy_ids" in expect:
            listed, listing = _request(self.base, "GET", "/legacy/issues")
            ids = sorted(item["id"] for item in (listing.get("issues") or [])) if listed == 200 else []
            if ids != sorted(expect["legacy_ids"]):
                problems.append(f"legacy ids {ids}")


def _git_of(scenario: dict, cwd: str) -> GitStub:
    git = scenario.get("git") or {}
    return GitStub(
        cwd,
        list(git.get("changed") or ["turbo/app.py"]),
        git.get("sha") or _PASS_SHA,
        int(git.get("pr_number") or 4),
    )


def _runner_of(scenario: dict, base: str, proc: FakeProc, git: GitStub, clock: Clock, state_dir: str) -> Runner:
    actors = scenario.get("actors") or []
    runner_actor = next(item for item in actors if item["role"] == "runner")
    agent = next(item for item in actors if item["role"] == "agent")
    spec = scenario.get("runner") or {}
    harnesses = spec.get("harnesses") or runner_actor.get("harnesses") or ["claude"]
    unknown = [item for item in harnesses if item not in HARNESSES]
    if unknown:
        raise RuntimeError("unknown harness " + ", ".join(unknown))
    return Runner(
        HttpAPI(base, spec.get("actor") or runner_actor["name"]),
        proc,
        git.cli,
        spec.get("actor") or runner_actor["name"],
        spec.get("agent") or agent["name"],
        harnesses,
        state_dir,
        clock,
    )


def run_one(file: Path, scenario: dict) -> dict:
    steps_out = []
    ok = True
    error = ""
    try:
        with tempfile.TemporaryDirectory(prefix="turbo-scenario-") as tmp:
            clock = Clock()
            legacy_fixture = scenario.get("legacy")
            legacy_path = None
            before = None
            if legacy_fixture:
                legacy_path = str(Path(tmp) / "legacy.db")
                before = _seed_legacy(legacy_path, legacy_fixture)
            app = create_app(str(Path(tmp) / "turbo.db"), legacy_path=legacy_path, clock=clock)
            proc = FakeProc()
            git = _git_of(scenario, tmp)
            with Server(app) as server:
                runner = _runner_of(scenario, server.base, proc, git, clock, str(Path(tmp) / "state"))
                run = Run(server.base, scenario, proc, git, runner, legacy_path)
                run.seed()
                for spec in scenario.get("steps") or []:
                    name = spec.get("name") or spec.get("op")
                    try:
                        run.step(spec)
                        step_ok = not run.reason
                        detail = run.reason
                    except Exception as exc:
                        step_ok = False
                        detail = f"{type(exc).__name__}: {exc}"
                    steps_out.append({"name": name, "ok": step_ok, "detail": detail})
                    if not step_ok:
                        ok = False
                        break
                if ok and before is not None and legacy_path is not None:
                    after = _snapshot(legacy_path)
                    if after != before:
                        ok = False
                        steps_out.append({
                            "name": "legacy unchanged",
                            "ok": False,
                            "detail": "legacy sqlite rows or schema changed",
                        })
                    else:
                        steps_out.append({"name": "legacy unchanged", "ok": True, "detail": ""})
    except Exception as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    return {"file": str(file), "name": scenario["name"], "ok": ok, "error": error, "steps": steps_out}


def run_scenarios(paths: list[Path]) -> dict:
    scenarios = load_scenarios(paths)
    results = [run_one(file, scenario) for file, scenario in scenarios]
    passed = sum(1 for item in results if item["ok"])
    return {"ok": passed == len(results), "passed": passed, "failed": len(results) - passed, "scenarios": results}


def _print_report(report: dict) -> None:
    for scenario in report["scenarios"]:
        mark = "PASS" if scenario["ok"] else "FAIL"
        print(f"{mark}  {scenario['name']}")
        if scenario.get("error"):
            print(f"  FAIL  setup: {scenario['error']}")
        for step in scenario["steps"]:
            step_mark = "PASS" if step["ok"] else "FAIL"
            print(f"  {step_mark}  {step['name']}")
            if step.get("detail"):
                print(f"        {step['detail']}")
    print(f"{report['passed']} passed, {report['failed']} failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="turbo.scenarios")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--json", dest="json_path", type=Path)
    args = parser.parse_args(argv)
    paths = args.paths or [Path(__file__).resolve().parent]
    report = run_scenarios(paths)
    _print_report(report)
    if args.json_path:
        args.json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1
