"""Headless resume command lines.

Confirmed commands (Claude, Codex) follow the resume notes. Cursor is
partial: the flags parse, and a real continued turn was not confirmed.
Grok CLI resume is confirmed; ``-s`` is used only to name a new session.
OpenCode and Hermes are handoff-only. They have no resume CLI here, and
this module does not invent one. A handoff records the card and does not
spawn a process. This module does not invent a TUI port or a keystroke
injection.
"""

import json
from typing import NamedTuple

HARNESSES = ("claude", "codex", "grok", "cursor", "grok-bot", "opencode", "hermes")

STATUS = {
    "claude": "confirmed",
    "codex": "confirmed",
    "grok": "confirmed",
    "cursor": "partial",
    "grok-bot": "context-unconfirmed",
    "opencode": "handoff-only",
    "hermes": "handoff-only",
}

FORKABLE = {"claude", "codex", "grok"}
HANDOFF_ONLY = {"opencode", "hermes", "cursor"}

SYSTEM = (
    "The runner owns the process, the session, git, the pull request, and submit. "
    "Waiting is a server state. A decision is blocked_question, blocked_options, "
    "and blocked_default on the card, then the turn ends. "
    "The agent does not sleep, poll, merge, or submit."
)


class Plan(NamedTuple):
    mode: str
    argv: list[str] | None
    prompt: str
    session_id: str | None
    status: str


def system_text() -> str:
    return SYSTEM


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _toml_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def initial_prompt(card: dict) -> str:
    scope = json.dumps(card["scope"], sort_keys=True, ensure_ascii=False)
    nonce = card.get("nonce") or ""
    nonce_line = f"nonce: {nonce}\n" if nonce else ""
    return (
        f"card_id: {card['id']}\n"
        f"contract_version: {card['contract_version']}\n"
        f"scope: {scope}\n"
        f"{nonce_line}"
        f"contract:\n{card['contract']}\n"
        f"body:\n{card['body']}\n"
        "A decision goes on the card as blocked_question, blocked_options, "
        "and blocked_default, then the turn ends.\n"
        "The runner commits, opens the pull request, and submits.\n"
    )


def start_plan(harness: str, session_id: str | None, prompt: str, system_file: str) -> Plan:
    status = STATUS[harness]
    rules = _one_line(SYSTEM)
    if harness == "claude":
        argv = [
            "claude", "-p", prompt, "--session-id", session_id or "",
            "--output-format", "json", "--append-system-prompt-file", system_file,
        ]
        return Plan("start", argv, prompt, session_id, status)
    if harness == "codex":
        argv = [
            "codex", "exec", "--json",
            "-c", "developer_instructions=" + _toml_string(rules),
            prompt,
        ]
        return Plan("start", argv, prompt, None, status)
    if harness == "grok":
        argv = [
            "grok", "-p", prompt, "-s", session_id or "",
            "--output-format", "json", "--rules", rules,
        ]
        return Plan("start", argv, prompt, session_id, status)
    if harness == "cursor":
        return Plan("create-chat", ["agent", "create-chat"], prompt, None, status)
    if harness == "grok-bot":
        return Plan("webhook", None, prompt, None, status)
    return Plan("handoff", None, prompt, None, status)


def cursor_resume_argv(session_id: str, prompt: str) -> list[str]:
    return ["agent", "-p", "--resume", session_id, "--trust", prompt]


def resume_plan(harness: str, session_id: str | None, prompt: str,
                system_file: str, attempt: str = "resume") -> Plan:
    status = STATUS[harness]
    rules = _one_line(SYSTEM)
    if harness in ("opencode", "hermes") or attempt == "handoff" or harness == "grok-bot" and attempt != "resume":
        return Plan("handoff", None, prompt, None, status)
    if harness == "grok-bot":
        return Plan("webhook", None, prompt, session_id, status)
    if attempt == "fork":
        if harness == "claude":
            argv = [
                "claude", "-p", prompt, "--resume", session_id or "",
                "--fork-session", "--output-format", "json",
                "--append-system-prompt-file", system_file,
            ]
            return Plan("fork", argv, prompt, None, status)
        if harness == "codex":
            argv = [
                "codex", "exec", "fork", session_id or "", prompt, "--json",
                "-c", "developer_instructions=" + _toml_string(rules),
            ]
            return Plan("fork", argv, prompt, None, status)
        argv = [
            "grok", "-r", session_id or "", "-p", prompt, "--fork-session",
            "--output-format", "json", "--rules", rules,
        ]
        return Plan("fork", argv, prompt, None, status)
    if harness == "claude":
        argv = [
            "claude", "-p", prompt, "--resume", session_id or "",
            "--output-format", "json", "--append-system-prompt-file", system_file,
        ]
        return Plan("resume", argv, prompt, session_id, status)
    if harness == "codex":
        argv = [
            "codex", "exec", "resume", session_id or "", prompt, "--json",
            "-c", "developer_instructions=" + _toml_string(rules),
        ]
        return Plan("resume", argv, prompt, session_id, status)
    if harness == "grok":
        argv = [
            "grok", "-r", session_id or "", "-p", prompt,
            "--output-format", "json", "--rules", rules,
        ]
        return Plan("resume", argv, prompt, session_id, status)
    return Plan("resume", cursor_resume_argv(session_id or "", prompt), prompt, session_id, status)


def can_fork(harness: str) -> bool:
    return harness in FORKABLE


def parse_session(harness: str, output: str, preassigned: str | None = None) -> str | None:
    if harness == "cursor":
        text = (output or "").strip()
        return text or preassigned or None
    if preassigned:
        return preassigned
    if harness == "codex":
        for line in (output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "thread.started" and data.get("thread_id"):
                return str(data["thread_id"])
        return None
    try:
        data = json.loads(output or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if harness == "claude":
        value = data.get("session_id")
        return str(value) if value else None
    value = data.get("sessionId")
    return str(value) if value else None


def webhook_body(card_id: str, prompt: str) -> dict:
    return {"card_id": card_id, "event": prompt}
