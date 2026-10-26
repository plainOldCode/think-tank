# Scenario coverage

What `python -m turbo.scenarios run turbo/scenarios` executes inside `turbo/`, and what it still misses. Measured with `scripts/scenario-coverage.sh` (coverage.py, branch coverage, `--source=turbo`, scenarios omitted). The server runs in a thread of that process.

`tests/test_turbo.py` and `tests/test_turbo_runner.py` are the other tests. A mark says **only unit-tested** when one of those files executes the line, and **dead** when neither suite does. `tests/test_scenarios.py` only launches the scenario set, so it is not a separate hit.

The scenario set is `01` through `54`, with no gap. `16` is the OpenCode handoff. `48` through `54` drive Codex, Grok CLI, Cursor, the Grok Bot webhook, Hermes, and mixed-harness affinity. Harness argv is captured. The webhook post is an in-process stub.

## Result

| | Before (`663d757`) | Now |
|---|---|---|
| Statements | 1144 / 1333 (86%) | **1215 / 1334 (91%)** |
| Branches | 303 / 386 (78%), 83 missed, 39 partial | **339 / 386 (88%)**, 47 missed, 33 partial |
| Combined (the Cover column) | 84% | **90%** |
| `# TURBO-UNCOVERED` marks | 89 | **61** |

| File | Cover |
|---|---|
| `turbo/__init__.py` | 100% |
| `turbo/__main__.py` | 0% (module entry; not marked) |
| `turbo/app.py` | 83% |
| `turbo/clock.py` | 100% |
| `turbo/constants.py` | 100% |
| `turbo/drivers.py` | 92% |
| `turbo/errors.py` | 100% |
| `turbo/gate.py` | 100% |
| `turbo/http_api.py` | 96% |
| `turbo/legacy.py` | 82% |
| `turbo/legacy_schema.py` | 86% |
| `turbo/proc.py` | 64% |
| `turbo/runner.py` | 80% |
| `turbo/scope.py` | 100% |
| `turbo/service.py` | 99% |
| `turbo/state.py` | 100% |
| `turbo/store.py` | 97% |
| **Total** | **90%** |

`clock.py`, `constants.py`, `errors.py`, `gate.py`, `scope.py`, `state.py`, and `__init__.py` have no candidates. `__main__.py` and the `if __name__ == "__main__"` guard in `runner.py` are uncovered and unmarked: they are the process entry, not product logic.

## How a mark reads

```python
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
```

A mark sits on the statement the scenarios miss. **Untaken branch** means the line ran and one side did not. There are 61 marks. `scripts/mutate-guards.py` checks that removing a guarded line fails its scenario, including the new argv, fork, and webhook-redaction checks.

## Remaining candidates

### Dead

| File | Line | Why it stays |
|---|---|---|
| `turbo/drivers.py` | 158 | `resume_plan` falls through to a handoff plan. Every harness in `STATUS` returns earlier. |
| `turbo/service.py` | 58 | `register_actor` raises if the row it just inserted cannot be read back. |

### Untaken by both suites

| File | Line | Why |
|---|---|---|
| `turbo/app.py` | 217, 220 | False side of mounting `/assets` and `/fonts`. Both directories exist. |
| `turbo/service.py` | 246 | `_queue_resume` is only called with `answer` or `verdict`. |

### Only unit-tested

| File | Lines | What |
|---|---|---|
| `turbo/app.py` | 20, 35, 42, 45 | `_text`, empty verify command, verify timeout, verify `OSError` |
| `turbo/app.py` | 200, 207, 213, 227 | Legacy unconfigured 404s, `GET /`, `main` |
| `turbo/drivers.py` | 136 | Fork of a harness that is not Claude, Codex, or Grok. `can_fork` is false for the others, so the runner never asks. |
| `turbo/drivers.py` | 187, 190, 193, 199 | JSON session parsing for a harness that is not Codex or Cursor, and the Claude `session_id` field. Codex and Grok output are parsed by their own branches. |
| `turbo/http_api.py` | 32 | Non-JSON HTTP error body |
| `turbo/legacy.py` | 31, 54, 81, 109, 116, 125, 150 | Read-only open failure, list labels, missing table, thin issues table, missing brief columns |
| `turbo/legacy_schema.py` | 74 | `create_throwaway` when the file exists |
| `turbo/proc.py` | 18, 22, 32–68, 97 | Short porcelain lines, renames, `SubprocessProc`, `open_pr` with no stdout |
| `turbo/runner.py` | 24–74 | `DirectAPI`. Scenarios talk HTTP. |
| `turbo/runner.py` | 113, 144, 164, 179 | Unknown harness, a plan with no argv, webhook with no URL, reap of an unknown handle |
| `turbo/runner.py` | 162, 168 | False side of the webhook URL check, and a webhook with no bearer. The Grok Bot scenario always sets both. |
| `turbo/runner.py` | 271, 284 | `_post_webhook` and `runner.main`. The scenario stub does not open a socket. |
| `turbo/service.py` | 363 | `submit` rejects a non-object `pr`. The HTTP route turns that into `{}` first. |
| `turbo/store.py` | 112, 132, 136, 183 | Missing parent directory, nested transaction, unknown column |
