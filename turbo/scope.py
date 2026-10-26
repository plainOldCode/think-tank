"""Machine-readable scope. Prose briefs are not a scope."""

import fnmatch

from .errors import TurboError

VERIFY_KINDS = {"unit", "type", "live", "planned"}


def parse_scope(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise TurboError(422, "scope must be an object with paths, forbidden, and verify")
    paths = raw.get("paths")
    if not isinstance(paths, list) or not paths or not all(_text(p) for p in paths):
        raise TurboError(422, "scope.paths must be a non-empty list of strings")
    forbidden = raw.get("forbidden") or []
    if not isinstance(forbidden, list) or not all(isinstance(p, str) for p in forbidden):
        raise TurboError(422, "scope.forbidden must be a list of strings")
    verify = raw.get("verify")
    if not isinstance(verify, list) or not verify:
        raise TurboError(422, "scope.verify must be a non-empty list of {cmd, kind}")
    steps = []
    for step in verify:
        if not isinstance(step, dict) or not _text(step.get("cmd")):
            raise TurboError(422, "each scope.verify item needs a cmd string")
        kind = step.get("kind") or "unit"
        if kind not in VERIFY_KINDS:
            raise TurboError(422, "verify kind must be unit, type, live, or planned")
        timeout = step.get("timeout_s", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise TurboError(422, "timeout_s must be an integer from 1 to 120")
        steps.append({"cmd": step["cmd"].strip(), "kind": kind, "timeout_s": timeout})
    timebox = raw.get("timebox_s")
    if timebox is not None and (isinstance(timebox, bool) or not isinstance(timebox, int) or timebox < 0):
        raise TurboError(422, "timebox_s must be a non-negative integer")
    return {
        "paths": [p.strip() for p in paths],
        "forbidden": [p.strip() for p in forbidden if p.strip()],
        "verify": steps,
        "timebox_s": timebox,
    }


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def matches(path: str, rule: str) -> bool:
    if any(ch in rule for ch in "*?["):
        return fnmatch.fnmatch(path, rule)
    rule = rule.rstrip("/")
    return path == rule or path.startswith(rule + "/")


def check_changed(scope: dict, changed: object) -> None:
    if not isinstance(changed, list) or not changed or not all(isinstance(p, str) and p for p in changed):
        raise TurboError(422, "changed_paths must list the files in the commit")
    outside = [p for p in changed if not any(matches(p, rule) for rule in scope["paths"])]
    blocked = [p for p in changed if any(matches(p, rule) for rule in scope["forbidden"])]
    if outside:
        raise TurboError(409, "paths outside scope: " + ", ".join(outside))
    if blocked:
        raise TurboError(409, "forbidden paths: " + ", ".join(blocked))


def level_of(steps: list[dict], results: list[dict]) -> str:
    """Map commands the server actually ran to a verification level.

    The agent's own claim about the level is not an input.
    """
    if any(item["code"] != 0 for item in results):
        return "failed"
    kinds = {step["kind"] for step in steps}
    if "live" in kinds:
        return "live-verified"
    if "unit" in kinds:
        return "unit-test-verified"
    if kinds == {"type"}:
        return "type-check-only"
    return "planned-only"
