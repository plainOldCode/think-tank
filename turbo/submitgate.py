"""Submit rules that come from the measured P1 board, not from a red-test default.

Behavior cards (bug, feature) still prove a scenario fails on base and passes
on head. Visual work proves measurement files exist at the submitted head and
that CI passed. Report work does not pretend to be a failing test. An epic
closes only when its children have merged.
"""

from .errors import TurboError
from .scope import matches

BEHAVIOR = {"bug", "feature"}
REPORT = {"report", "docs", "research"}
KINDS = BEHAVIOR | REPORT | {"visual", "epic"}

_TODO = (
    "outside scope",
    "forbidden paths",
    "scenario",
    "evidence missing",
    "visual work",
    "not mergeable",
    "head sha does not match",
    "an epic closes",
)


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise TurboError(422, "card kind must be bug, feature, visual, report, docs, research, or epic")
    return kind


def needs_scenario(kind: str, exempt: bool) -> bool:
    if exempt:
        return False
    return kind in BEHAVIOR


def check_evidence(kind: str, evidence: list, changed: list) -> None:
    if kind == "visual" and not evidence:
        raise TurboError(409, "visual work needs measurement evidence")
    missing = [path for path in evidence if path not in changed]
    if missing:
        raise TurboError(409, "evidence missing at head: " + ", ".join(missing))


def check_head(sha: str, pr: dict) -> None:
    head = pr.get("head_sha", pr.get("sha", ""))
    if isinstance(head, str) and head and head != sha:
        raise TurboError(409, "head sha does not match the pull request")


def check_mergeable(pr: dict) -> None:
    if "mergeable" not in pr:
        return
    flag = pr["mergeable"]
    if flag is not True and flag is not False:
        raise TurboError(422, "pr.mergeable must be true or false")
    if flag is False:
        raise TurboError(409, "not mergeable against base")


def rebase_outside(paths: list, rebase: object) -> bool:
    if rebase is None:
        return False
    if not isinstance(rebase, list) or not all(isinstance(item, str) for item in rebase):
        raise TurboError(422, "pr.rebase_paths must be a list of paths")
    return any(
        path and not any(matches(path, rule) for rule in paths)
        for path in rebase
    )


def check_approve_head(card: dict, head_sha: str) -> None:
    if head_sha:
        if head_sha != card["sha"]:
            raise TurboError(409, "head sha does not match the pull request")
        return
    pr = card.get("pr") or {}
    stored = pr.get("head_sha") or pr.get("sha") or ""
    if isinstance(stored, str) and stored and stored != card["sha"]:
        raise TurboError(409, "head sha does not match the pull request")


def returns_to_todo(status: int, message: str) -> bool:
    if status not in (409, 422):
        return False
    return any(piece in message for piece in _TODO)
