# Scenario coverage

What `python -m turbo.scenarios run turbo/scenarios` executes inside `turbo/`, and what it still misses. Measured with `scripts/scenario-coverage.sh` (coverage.py, branch coverage, `--source=turbo`, scenarios omitted). The server runs in a thread of that process.

`tests/test_turbo.py` and `tests/test_turbo_runner.py` are the other tests. A mark says **only unit-tested** when one of those files executes the line, and **dead** when neither suite does. `tests/test_scenarios.py` only launches the scenario set, so it is not a separate hit.

Two functions that nothing called were deleted: `drivers.handoff_prompt` (handoff already uses `initial_prompt`) and `Store.close` (no caller). Boundary handlers and the documented entry points stayed, and the new scenarios cover the guards that used to be silent holes.

## Result

| | |
|---|---|
| Statements | 1144 / 1333 (86%) |
| Branches | 303 / 386 (78%), 83 missed, 39 partial |
| Combined (the Cover column) | **84%** |

Previously, on the same base: 1049 / 1337 statements (78%), 219 / 386 branches (57%), combined 74%, 178 marks.

| File | Cover |
|---|---|
| `turbo/__init__.py` | 100% |
| `turbo/__main__.py` | 0% (module entry; not marked) |
| `turbo/app.py` | 83% |
| `turbo/clock.py` | 100% |
| `turbo/constants.py` | 100% |
| `turbo/drivers.py` | 45% |
| `turbo/errors.py` | 100% |
| `turbo/gate.py` | 100% |
| `turbo/http_api.py` | 96% |
| `turbo/legacy.py` | 82% |
| `turbo/legacy_schema.py` | 86% |
| `turbo/proc.py` | 64% |
| `turbo/runner.py` | 69% |
| `turbo/scope.py` | 100% |
| `turbo/service.py` | 99% |
| `turbo/state.py` | 100% |
| `turbo/store.py` | 97% |
| **Total** | **84%** |

`clock.py`, `constants.py`, `errors.py`, `gate.py`, `scope.py`, `state.py`, and `__init__.py` have no candidates. `__main__.py` and the `if __name__ == "__main__"` guard in `runner.py` are uncovered and unmarked: they are the process entry, not product logic.

## How a mark reads

```python
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
```

A mark sits on the statement the scenarios miss. **Untaken branch** means the line ran and one side did not. There are 89 marks. `scripts/mutate-guards.py` checks that the security guards still fail a scenario when the check is removed.

## Remaining candidates

### Dead

Nothing in either suite reaches these. They stay because each is a safety return for a state the current callers cannot build.

| File | Line | Why it stays |
|---|---|---|
| `turbo/drivers.py` | 170 | `resume_plan` falls through to a handoff plan. Every harness in `STATUS` returns earlier. |
| `turbo/service.py` | 58 | `register_actor` raises if the row it just inserted cannot be read back. |

### Untaken by both suites

| File | Line | Why |
|---|---|---|
| `turbo/app.py` | 217, 220 | False side of mounting `/assets` and `/fonts`. Both directories exist, so neither suite takes the skip. |
| `turbo/service.py` | 246 | `_queue_resume` is only called with `answer` or `verdict`. The `elif kind == "verdict"` false side has no third kind. |

### Only unit-tested

The scenarios drive Claude and one OpenCode handoff. Codex, Grok, Cursor, and Grok Bot command lines, session parsers, and the webhook client are covered from `tests/test_turbo_runner.py`. `DirectAPI` is the in-process runner client those tests use; the scenario runner talks HTTP, so these methods stay unit-only. `app.main` and `runner.main` are the documented `python -m turbo` and `python -m turbo.runner` entries.

| File | Lines | What |
|---|---|---|
| `turbo/app.py` | 20, 35, 42, 45 | `_text`, empty verify command, verify timeout, verify `OSError` |
| `turbo/app.py` | 200, 207, 213, 227 | Legacy unconfigured 404s, `GET /`, `main` |
| `turbo/drivers.py` | 54, 86, 94, 101, 104, 110 | Codex quoting, Codex/Grok/Cursor/Grok Bot start, Cursor resume argv |
| `turbo/drivers.py` | 121, 132, 139, 145, 154, 161, 168 | Resume plans other than Claude |
| `turbo/drivers.py` | 180–224 | `parse_session` and `webhook_body` |
| `turbo/http_api.py` | 32 | Non-JSON HTTP error body |
| `turbo/legacy.py` | 31, 54, 81, 109, 150 | Read-only open failure, list labels, missing table, missing brief columns |
| `turbo/legacy.py` | 116, 125 | False side of `parent_id` and `comments` on a thin issues table |
| `turbo/legacy_schema.py` | 74 | `create_throwaway` when the file exists |
| `turbo/proc.py` | 18, 22, 32–68 | Short porcelain lines, renames, `SubprocessProc` |
| `turbo/proc.py` | 97 | `open_pr` when `gh` returns no stdout |
| `turbo/runner.py` | 24–74 | `DirectAPI` |
| `turbo/runner.py` | 112, 140–171, 181, 197, 216, 218, 277, 290 | Unknown harness, webhook launch, missing argv, create-chat, reap of an unknown handle, create-chat failure, `_post_webhook`, `main` |
| `turbo/runner.py` | 207 | `reap` when the harness output has no session id. Scenarios always take the Claude side, which has one. |
| `turbo/service.py` | 363 | `submit` rejects a non-object `pr`. The HTTP route turns a non-object into `{}` before this check. |
| `turbo/store.py` | 112, 132, 136, 183 | Missing parent directory, nested transaction, unknown column |

`GET /turbo/cards` stays. The ink board calls it, and `38-board-list.json` covers the route.
