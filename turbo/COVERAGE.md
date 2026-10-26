# Scenario coverage

`scripts/scenario-coverage.sh` measures `turbo/` with coverage.py branch coverage while `python -m turbo.scenarios run turbo/scenarios` drives a real server and the real `Runner`. The scenario engine and the tests are omitted (`--omit='*/turbo/scenarios/*'`). Nothing else is omitted, and there is no `# pragma: no cover`.

`python -m turbo` and `python -m turbo.runner` run as child processes. The script sets `COVERAGE_PROCESS_START` so those processes are included. The server is stopped with SIGINT, which still writes the coverage file.

## Result

| | |
|---|---|
| Statements | **1276 / 1276 (100%)** |
| Branches | **352 / 352 (100%)** |
| Combined | **100%** |
| Scenarios | **77** (`01` through `77`) |
| `# TURBO-UNCOVERED` marks | **0** (the marker is gone) |

The script passes `--fail-under=100`. CI runs it on pull requests and on pushes to `turbo`. A drop in statements or branches fails that job.

## What the new scenarios drive

`55`–`77` are the paths that were only unit-tested before. Harness binaries are stub scripts on `PATH` when a scenario sets `"proc": "subprocess"`. `git` and `gh` stay in-process. Webhooks that the scenario runner posts are an in-process stub; `python -m turbo.runner` posts to a local HTTP stub.

| Scenarios | What they hit |
|---|---|
| `55`, `56` | Verify `OSError`, and a `timeout_s` that expires with stdout, stderr, and no output |
| `57`–`61` | Claude and Grok session parsing: a real id, bad JSON, a non-object, an empty id |
| `62`, `63` | Grok Bot with no webhook URL, and a webhook with no bearer |
| `64` | `pr` that is not an object |
| `65` | `GET /`, `/assets`, `/fonts`, and a database path with no parent directory |
| `66`–`71` | Legacy unconfigured, unreadable, no issues table, a thin table, `create_throwaway` on an existing file, brief stats |
| `72` | Porcelain short lines and renames, and `gh pr create` with empty stdout |
| `73` | `HttpAPI` reading a non-JSON error body |
| `74`–`76` | `SubprocessProc` spawn, poll, output, SIGINT, and a child that deletes its output file |
| `77` | `python -m turbo` and `python -m turbo.runner` (`--once`, the sleep loop, and a real webhook POST) |

## Code removed or restructured

Dead fallbacks were deleted instead of excluded:

- `register_actor` no longer raises when the row it just inserted cannot be read back. It returns `require_actor`.
- `resume_plan` no longer has a final handoff for an unknown harness, or a handoff for a fork of a harness that is not Claude, Codex, or Grok. A fork attempt is only defined for those three, and Grok is the remaining one. Cursor is not forkable (`can_fork` is false), so the runner never asks.
- `parse_session` no longer has a final `return None` for a harness that is not Claude or Grok. A non-Claude object is read as Grok's `sessionId`.
- The runner no longer rejects an unknown harness after claim, and no longer rejects a plan with no argv. Claim and the drivers already make those states impossible. `reap` of a missing handle is a `KeyError`.
- `_webhook` checks the URL only. The constructors set the poster whenever the URL is set.
- `_queue_resume` treats every non-answer as a verdict. Callers pass only those two kinds.
- Submit passes `pr` through. A non-object is rejected by `service.submit`.
- Verify no longer special-cases an empty command. `parse_scope` already requires a command. `timeout_s` on a verify step (1–120, default 120) is the subprocess timeout.
- Static `/assets` and `/fonts` are always mounted. Both directories ship with the package.
- `DirectAPI` moved to the unit tests. Scenarios use `HttpAPI`.
- `update_card` no longer checks column names. A bad key is a SQL error; no caller passes one.
- Nested `transaction()` uses a savepoint per level, so there is no depth check with an untaken side.
- Labels from sqlite are text. A list is not a label value.
- `SubprocessProc.interrupt` always signals the child. The runner only interrupts a live poll.

`scripts/mutate-guards.py` flips 110 guards. Each one fails its scenario.
