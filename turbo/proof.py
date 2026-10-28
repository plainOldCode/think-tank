"""Scenario red-green check. The server runs the card's scenario, not a unit suite.

A bug or feature card names a JSON file in ``scope.scenario``. Submit runs
that file's ``cmd`` with ``TURBO_REV=base`` (must exit non-zero) and
``TURBO_REV=head`` (must exit 0). ``scope.exempt`` is the only skip, for a
change that has no reproducible failure. Commands are not a shell.
"""

import json
import os
from pathlib import Path

from .errors import TurboError


def prove(scope: dict, changed: list) -> None:
    if scope.get("exempt"):
        return
    path = scope.get("scenario") or ""
    if not path:
        raise TurboError(422, "scope.scenario is required unless scope.exempt is true")
    if path not in changed:
        raise TurboError(409, "the scenario must be part of the submit")
    file = Path(path)
    if not file.is_file():
        raise TurboError(422, "scenario JSON could not be read")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise TurboError(422, "scenario JSON could not be read") from None
    cmd = _scenario_cmd(raw)
    from .app import subprocess_verify

    for rev in ("base", "head"):
        env = os.environ.copy()
        env["TURBO_REV"] = rev
        code, _output = subprocess_verify(cmd, None, 30, env)
        if rev == "base" and code == 0:
            raise TurboError(409, "scenario passed on base")
        if rev == "head" and code != 0:
            raise TurboError(409, "scenario failed on head")


def _scenario_cmd(raw: object) -> str:
    cmd = ""
    if isinstance(raw, dict):
        value = raw.get("cmd")
        if isinstance(value, str):
            cmd = value.strip()
    if not cmd:
        raise TurboError(422, "scenario JSON needs a cmd")
    return cmd
