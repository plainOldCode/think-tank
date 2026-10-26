"""Headless runner.

It claims, spawns one process per session, resumes that session when the
board has an answer or a request-changes verdict, and is the only caller
that commits, opens the PR, and submits. It does not attach a terminal
and it does not inject keystrokes.
"""

import os
import time
import uuid

from . import drivers, scope, service
from .clock import parse_stamp
from .constants import QUESTION_GRACE_S
from .errors import TurboError


class DirectAPI:
    """In-process calls, used by tests and by a runner in the same program."""

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


class Runner:
    def __init__(self, api, proc, git, actor: str, agent: str, harnesses,
                 state_dir: str, clock, webhook_url: str = "", bearer: str = "",
                 webhook_post=None):
        self.api = api
        self.proc = proc
        self.git = git
        self.actor = actor
        self.agent = agent
        self.harnesses = set(harnesses)
        self.state_dir = state_dir
        self.clock = clock
        self.webhook_url = webhook_url
        self.bearer = bearer
        self.webhook_post = webhook_post
        self.handles: dict[str, dict] = {}
        self.logs: list[str] = []

    def system_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        path = os.path.join(self.state_dir, "system-prompt.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(drivers.system_text())
        return path

    def _log(self, message: str) -> None:
        secret = self.bearer
        if secret and secret in message:
            message = message.replace(secret, "[redacted]")
        self.logs.append(message)

    def start_card(self, card_id: str, agent: str | None = None) -> dict:
        claimed = self.api.claim(card_id, self.actor, agent or self.agent)
        if claimed["harness"] not in self.harnesses and claimed["harness"] not in drivers.HARNESSES:
            raise TurboError(409, "runner cannot start this harness")
        session_id = str(uuid.uuid4()) if claimed["harness"] in {"claude", "grok"} else None
        prompt = drivers.initial_prompt(claimed)
        plan = drivers.start_plan(claimed["harness"], session_id, prompt, self.system_file())
        self._launch(card_id, claimed["harness"], plan, prompt, phase="start", attempt="start")
        return {"mode": plan.mode, "argv": plan.argv, "prompt": prompt, "session_id": plan.session_id}

    def start_resume(self, card: dict) -> dict:
        fresh = self.api.get(card["id"], reveal=True)
        prompt = card["resume"]["prompt"]
        if fresh.get("nonce"):
            prompt += "nonce: " + fresh["nonce"] + "\n"
        session = fresh.get("session") or {}
        sid = session.get("session_id") or ""
        plan = drivers.resume_plan(
            fresh["harness"], sid, prompt, self.system_file(), attempt="resume")
        self.api.resume_started(card["id"], self.actor, card["resume"]["token"], sid or None, plan.mode)
        self._launch(card["id"], fresh["harness"], plan, prompt, phase="resume", attempt=plan.mode)
        return {"mode": plan.mode, "argv": plan.argv}

    def _launch(self, card_id, harness, plan, prompt, phase, attempt) -> None:
        if plan.mode == "handoff":
            self.api.attach_session(card_id, self.actor, "", harness, "handoff")
            self._log(f"handoff {harness} {card_id}")
            self.handles.pop(card_id, None)
            return
        if plan.mode == "webhook":
            self._webhook(card_id, harness, prompt)
            return
        if plan.argv is None:
            raise TurboError(409, "no command for " + plan.mode)
        if plan.mode == "create-chat":
            phase = "create-chat"
        handle = self.proc.spawn(plan.argv)
        self._log("spawn " + plan.argv[0] + " " + plan.mode)
        self.handles[card_id] = {
            "handle": handle,
            "phase": phase,
            "attempt": attempt,
            "prompt": prompt,
            "harness": harness,
            "session_id": plan.session_id,
        }
        if plan.session_id and plan.mode != "create-chat":
            self.api.attach_session(card_id, self.actor, plan.session_id, harness, plan.mode)

    def _webhook(self, card_id, harness, prompt) -> None:
        if not self.webhook_url or self.webhook_post is None:
            raise TurboError(409, "grok-bot needs a webhook URL")
        body = drivers.webhook_body(card_id, prompt)
        headers = {"content-type": "application/json"}
        if self.bearer:
            headers["authorization"] = "Bearer " + self.bearer
        status = self.webhook_post(self.webhook_url, body, headers)
        self._log(f"webhook status={status}")
        self.api.attach_session(card_id, self.actor, "", harness, "webhook")
        self.handles.pop(card_id, None)

    def reap(self, card_id: str) -> dict | None:
        ent = self.handles.get(card_id)
        if not ent:
            return None
        code = self.proc.poll(ent["handle"])
        card = self.api.get(card_id)
        now = self.clock()
        if code is None:
            if card["state"] == "question" and card.get("question_at"):
                if now >= parse_stamp(card["question_at"]) + QUESTION_GRACE_S:
                    self.proc.interrupt(ent["handle"])
                    self.api.turn_closed(card_id, self.actor)
                    self.handles.pop(card_id, None)
                    self._log(f"interrupt {card_id}")
                    return {"running": False, "interrupted": True}
            return {"running": True}
        output = self.proc.output(ent["handle"])
        if ent["phase"] == "create-chat":
            return self._after_create_chat(card_id, ent, code, output)
        if (code != 0 and card["state"] == "working" and ent["phase"] == "resume"
                and ent["attempt"] == "resume" and drivers.can_fork(ent["harness"])):
            self._fallback(card, ent, "fork")
            return {"running": True, "fallback": "fork"}
        if code != 0 and ent["attempt"] == "fork":
            self._fallback(card, ent, "handoff")
            return {"running": False, "fallback": "handoff"}
        sid = drivers.parse_session(ent["harness"], output, ent.get("session_id"))
        if sid:
            self.api.attach_session(card_id, self.actor, sid, ent["harness"], ent["attempt"])
        if card["state"] == "question":
            self.api.turn_closed(card_id, self.actor)
        self.handles.pop(card_id, None)
        return {"running": False, "code": code, "session_id": sid}

    def _after_create_chat(self, card_id, ent, code, output) -> dict:
        if code != 0:
            self.api.attach_session(card_id, self.actor, "", ent["harness"], "handoff")
            self.handles.pop(card_id, None)
            return {"running": False, "fallback": "handoff"}
        sid = drivers.parse_session("cursor", output, None)
        argv = drivers.cursor_resume_argv(sid or "", ent["prompt"])
        handle = self.proc.spawn(argv)
        self.handles[card_id] = {
            "handle": handle, "phase": "start", "attempt": "start",
            "prompt": ent["prompt"], "harness": "cursor", "session_id": sid,
        }
        self.api.attach_session(card_id, self.actor, sid or "", "cursor", "resume")
        self._log("spawn agent resume")
        return {"running": True, "session_id": sid}

    def _fallback(self, card, ent, attempt: str) -> None:
        session = (self.api.get(card["id"]).get("session") or {})
        sid = session.get("session_id") or ent.get("session_id") or ""
        prompt = ent["prompt"]
        plan = drivers.resume_plan(ent["harness"], sid, prompt, self.system_file(), attempt=attempt)
        self._launch(card["id"], ent["harness"], plan, prompt, phase="resume", attempt=plan.mode)

    def submit_ready(self, card_id: str, base: str = "main") -> dict:
        card = self.api.get(card_id, reveal=True)
        if not card.get("work_token"):
            raise TurboError(403, "ack required before submit")
        paths = self.git.changed_paths()
        scope.check_changed(card["scope"], paths)
        sha = self.git.commit_all(f"turbo {card_id}: {card['title']}")
        pr = self.git.open_pr(card["title"], card["body"], base)
        pr["sha"] = sha
        return self.api.submit(card_id, self.actor, sha, pr, paths, card["work_token"])

    def merge_card(self, card_id: str) -> dict:
        card = self.api.get(card_id)
        number = (card.get("pr") or {}).get("number")
        # The server is the authority. A rejected merge must not reach gh.
        result = self.api.merge(card_id, self.actor)
        if number:
            self.git.merge_pr(int(number), card.get("sha") or "")
        return result

    def tick(self) -> None:
        now = self.clock()
        self.api.apply_defaults(self.actor, now)
        for card_id in list(self.handles):
            self.reap(card_id)
        if self.handles:
            return
        pending = self.api.pending(self.actor)
        if pending:
            self.start_resume(pending[0])
            return
        nxt = self.api.next_open(self.actor)
        if nxt and self.agent:
            self.start_card(nxt["id"])


def _post_webhook(url: str, body: dict, headers: dict) -> int:
    import json
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=30) as res:
        return int(res.status)


def main(argv: list[str] | None = None) -> None:
    """One runner process. It ticks on its own clock. Agents do not poll."""
    import argparse

    from .http_api import HttpAPI
    from .proc import GitCli, SubprocessProc

    parser = argparse.ArgumentParser(prog="turbo.runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    actor = os.environ["TURBO_ACTOR"]
    agent = os.environ.get("TURBO_AGENT", "")
    harnesses = [item for item in os.environ.get("TURBO_HARNESSES", "claude,codex").split(",") if item]
    runner = Runner(
        HttpAPI(os.environ.get("TURBO_URL", "http://127.0.0.1:7810"), actor),
        SubprocessProc(),
        GitCli(os.environ.get("TURBO_WORKSPACE", ".")),
        actor,
        agent,
        harnesses,
        os.environ.get("TURBO_STATE", os.path.join("data", "runner")),
        time.time,
        webhook_url=os.environ.get("TURBO_GROK_BOT_WEBHOOK_URL", ""),
        bearer=os.environ.get("TURBO_GROK_BOT_BEARER", ""),
        webhook_post=_post_webhook if os.environ.get("TURBO_GROK_BOT_WEBHOOK_URL") else None,
    )
    runner.tick()
    if args.once:
        return
    while True:
        time.sleep(args.interval)
        runner.tick()


if __name__ == "__main__":
    main()
