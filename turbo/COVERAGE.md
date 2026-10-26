# Scenario coverage

What `python -m turbo.scenarios run turbo/scenarios` executes inside `turbo/`, and what it never reaches. Nothing here was deleted. The scenarios were not weakened.

The measurement is line and branch coverage from coverage.py. The scenario engine (`turbo/scenarios/`) and `tests/` are outside the target. The server is a thread in the same process as the scenario runner, so one `coverage run` traces it. `turbo/service.py` at 76% is the check that the thread was included.

`tests/test_turbo.py` and `tests/test_turbo_runner.py` are the other tests. A mark says **only unit-tested** when one of those files executes the line, and **dead** when neither the scenarios nor those tests do. `tests/test_scenarios.py` only launches the scenario set, so it is not a separate hit.

## Result

| | |
|---|---|
| Statements | 1049 / 1337 (78%) |
| Branches | 219 / 386 (57%), 167 missed, 115 partial |
| Combined (the Cover column) | **74%** |

| File | Cover |
|---|---|
| `turbo/__init__.py` | 100% |
| `turbo/__main__.py` | 0% (module entry; not marked) |
| `turbo/app.py` | 82% |
| `turbo/clock.py` | 100% |
| `turbo/constants.py` | 100% |
| `turbo/drivers.py` | 45% |
| `turbo/errors.py` | 100% |
| `turbo/gate.py` | 83% |
| `turbo/http_api.py` | 96% |
| `turbo/legacy.py` | 74% |
| `turbo/legacy_schema.py` | 86% |
| `turbo/proc.py` | 62% |
| `turbo/runner.py` | 67% |
| `turbo/scope.py` | 70% |
| `turbo/service.py` | 76% |
| `turbo/state.py` | 100% |
| `turbo/store.py` | 92% |
| **Total** | **74%** |

`clock.py`, `constants.py`, `errors.py`, `state.py`, and `__init__.py` have no candidates. `__main__.py` and the `if __name__ == "__main__"` guard in `runner.py` are uncovered and were left unmarked: they are the process entry, not product logic. Imports and type-only lines were not marked.

## How a mark reads

```python
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch)
# TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch, only unit-tested)
```

A mark sits on the function when the scenarios never call it, and on the block otherwise. **Untaken branch** means the line ran and one side did not. There are 178 marks.

## Removal candidates

### `turbo/app.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 19 | `_text` | Bytes, str, and empty decoding. Only the timeout path would call it, and that path does not run. | dead |
| 34 | `subprocess_verify` | Empty verify command. Every scenario command has an argv. | dead |
| 41, 44 | `subprocess_verify` | `TimeoutExpired` and `OSError`. Commands exit 0 or 1. | dead |
| 92 | `metrics` | `GET /turbo/metrics`. | only unit-tested |
| 112 | `get_cards` | `GET /turbo/cards` list. Scenarios fetch one card. | dead |
| 201 | `legacy_list` | 404 when no legacy database is configured. | only unit-tested |
| 208 | `legacy_one` | Same 404 on the detail route. | dead |
| 214 | `index` | `GET /` HTML. | only unit-tested |
| 218, 221 | `create_app` | False side of mounting `/assets` and `/fonts`. Both directories exist. | dead |
| 228 | `main` | Process entry that reads `TURBO_DB` and calls uvicorn. Scenarios call `create_app` directly. | dead |

### `turbo/drivers.py`

The set starts and resumes Claude, and hands off OpenCode. Codex, Grok, Cursor, and Grok Bot command lines are not in a scenario.

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 53 | `_toml_string` | Codex developer-instruction quoting. | only unit-tested |
| 76 | `handoff_prompt` | Body never called. Handoff uses `initial_prompt`. | dead |
| 90 | `start_plan` | Codex start argv. | only unit-tested |
| 98 | `start_plan` | Grok start argv. | only unit-tested |
| 105 | `start_plan` | Cursor `create-chat`. | only unit-tested |
| 108 | `start_plan` | Grok Bot webhook plan. | only unit-tested |
| 114 | `cursor_resume_argv` | Cursor resume argv. | only unit-tested |
| 125 | `resume_plan` | Grok Bot resume returns a webhook plan. | dead |
| 136 | `resume_plan` | Codex fork argv. | only unit-tested |
| 143, 149 | `resume_plan` | Grok fork, and fork of a harness that cannot fork. | dead |
| 158 | `resume_plan` | Codex resume argv. | only unit-tested |
| 165, 172, 174 | `resume_plan` | Grok resume, Cursor resume, and the final handoff return. | dead |
| 184 | `parse_session` | Cursor output is the session id. Claude scenarios preassign the id, so this branch is skipped. | only unit-tested |
| 190, 202 | `parse_session` | Codex `thread.started` scan, including the hit. | only unit-tested |
| 194, 199, 205 | `parse_session` | Empty Codex line, bad JSON line, and no thread id. | dead |
| 204 | `parse_session` | False side of the `thread.started` check (loop continues). | dead |
| 208 | `parse_session` | JSON object parse used for Claude and Grok. | only unit-tested |
| 211, 214, 217, 224 | `parse_session` | Bad JSON, non-object, Claude `session_id` field, and the final `None`. | dead |
| 221 | `parse_session` | Grok `sessionId` field. | only unit-tested |
| 229 | `webhook_body` | Grok Bot payload. | only unit-tested |

### `turbo/gate.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 48 | `_eq` | Length mismatch before `compare_digest`. Compared strings in the set are the same length. | dead |
| 55 | `check_ack` | Nonce already used. | only unit-tested |
| 58 | `check_ack` | Nonce missing or different. The bad-digest scenario keeps the real nonce. | dead |
| 71 | `check_token` | Digest stored at ack no longer matches the card. | only unit-tested |

### `turbo/http_api.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 31 | `HttpAPI._req` | Error body that is not JSON. The server always returns JSON. | dead |

### `turbo/legacy.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 25, 31 | `open_ro` | Missing file, and sqlite refusing the read-only URI. | dead |
| 49 | `_parse_json` | Invalid JSON kept as the raw string. | dead |
| 55, 59 | `_labels` | Value that is already a list, and a comma-separated string. The fixture labels are empty, which hits `return []`. | dead |
| 83 | `list_issues` | No `id` or `title` column. | dead |
| 111, 115 | `issue_detail` | No `id` column, and unknown issue id. | dead |
| 119, 128 | `issue_detail` | False side of `parent_id` and of the comments table. The fixture has both. | dead |
| 148 | `brief_stats` | Whole function. Scenarios never call `/turbo/metrics`. | only unit-tested |
| 154 | `brief_stats` | Brief columns absent, so the counts stay zero. | dead |

### `turbo/legacy_schema.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 73 | `create_throwaway` | Refusing a path that already exists. The runner always uses a new file. | dead |

### `turbo/proc.py`

Scenarios use `GitCli` with a stubbed `run`. They do not use `SubprocessProc`.

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 17 | `parse_porcelain` | A status line shorter than 4 characters. | dead |
| 21 | `parse_porcelain` | A rename (`->`). | only unit-tested |
| 31, 37, 52 | `SubprocessProc` | `__init__`, `spawn`, and `poll`. | only unit-tested |
| 56 | `SubprocessProc.interrupt` | SIGINT of a real child. The grace scenario interrupts `FakeProc`. | dead |
| 62 | `SubprocessProc.output` | Reading the captured file. | only unit-tested |
| 67 | `SubprocessProc.output` | `OSError` while reading it. | dead |
| 96 | `GitCli.open_pr` | False side of `if created.stdout`. The stub always prints a URL. | dead |
| 113 | `GitCli.merge_pr` | Empty sha. | dead |

### `turbo/runner.py`

Scenarios talk to the server through `HttpAPI`. `DirectAPI` is the in-process client.

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 23, 29, 33, 37, 46, 51, 55, 60, 65, 73 | `DirectAPI` | `__init__`, `claim`, `get`, `_runner`, `attach_session`, `turn_closed`, `resume_started`, `submit`, `merge`, `pending`. | only unit-tested |
| 42, 69 | `DirectAPI` | `apply_defaults` (passes the caller's clock) and `next_open`. | dead |
| 105 | `Runner._log` | Replacing the bearer when it appears in a log line. | dead |
| 112 | `Runner.start_card` | Harness that is neither configured nor known. | dead |
| 140, 147 | `Runner._launch` | Webhook mode, and setting the create-chat phase. | only unit-tested |
| 144 | `Runner._launch` | Plan with no argv and no handoff or webhook mode. | dead |
| 163, 171 | `Runner._webhook` | Posting when a URL and a poster exist, and attaching the bearer. | only unit-tested |
| 165 | `Runner._webhook` | Missing URL or poster. | dead |
| 169 | `Runner._webhook` | False side of `if self.bearer` (post with no bearer). | dead |
| 181 | `Runner.reap` | No handle for the card. | dead |
| 194 | `Runner.reap` | Process still alive and the question grace does not fire. The grace scenario always interrupts. | dead |
| 198 | `Runner.reap` | Create-chat phase hands off to `_after_create_chat`. | only unit-tested |
| 208 | `Runner.reap` | False side of `if sid` (parsed session id was empty, so attach is skipped). | dead |
| 217 | `Runner._after_create_chat` | Whole method. | only unit-tested |
| 219 | `Runner._after_create_chat` | Create-chat exits non-zero and becomes a handoff. | dead |
| 257 | `Runner.merge_card` | False side of `if number` (merge with no pull-request number). | dead |
| 279 | `_post_webhook` | The real urllib POST. Tests inject `webhook_post`. | dead |
| 292 | `main` | CLI loop (`TURBO_ACTOR`, `--once`, sleep). | dead |

### `turbo/scope.py`

Scenarios send a valid scope and a real `changed_paths` list. Verify kinds in the set are `unit` and a failing command.

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 12, 16, 20, 24, 29, 33, 38 | `parse_scope` | Rejecting a non-object, bad paths, bad forbidden, empty verify, a step without cmd, a bad kind, and a bad timebox. | dead |
| 54 | `matches` | A glob rule (`*`, `?`, or `[`). Rules in the set are prefixes. | dead |
| 62 | `check_changed` | `changed_paths` missing, empty, or not a list of strings. | dead |
| 81 | `level_of` | `live-verified`. | dead |
| 86 | `level_of` | `type-check-only`. | only unit-tested |
| 88 | `level_of` | `planned-only`. | dead |

### `turbo/service.py`

These are guards. The happy path of each function does run.

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 32, 36, 40, 44, 47, 50, 54, 65 | `register_actor` | Empty name, bad role, bad harness list, unknown harness, runner with no harness, non-runner with harnesses, duplicate actor, and the actor row missing after insert. | dead |
| 58 | `register_actor` | First actor is not a human. | only unit-tested |
| 74, 77, 80, 83 | `create_card` | Empty title, body, or contract, and an unknown harness. | dead |
| 125 | `get_card` | False side of the reveal check (actor is set but is not the assignee runner). | dead |
| 132 | `list_cards` | Whole function. No scenario lists cards. | dead |
| 146 | `claim` | Runner affinity does not include the card harness. | only unit-tested |
| 178, 181 | `ack` | Wrong agent, and ack from a state that is not open for it. | dead |
| 200, 206, 210, 213, 219 | `ask` | Empty question, non-string options, duplicate options, default not in the options, wrong agent. | dead |
| 203 | `ask` | Option list shorter than 2 or longer than 4. | only unit-tested |
| 249 | `turn_closed` | A runner who is not the assignee. | dead |
| 269 | `_queue_resume` | False side of `elif kind == "verdict"` (kind is neither answer nor verdict). | dead |
| 283, 290, 293, 296 | `answer` | Empty choice, card not awaiting a person, a second answer, choice outside the options. | dead |
| 328, 331 | `ready` | Wrong agent, and ready from a state other than working. | dead |
| 345 | `attach_session` | A runner who is not the assignee. | dead |
| 360, 366, 369, 373 | `resume_started` | Wrong assignee, state that is not awaiting or review, review without request-changes, and a resume token that does not match. | dead |
| 363 | `resume_started` | Resume while a turn is already working. | only unit-tested |
| 377 | `resume_started` | False side of `if session_id` (resume with no session id). | only unit-tested |
| 397, 400, 406, 409, 412 | `submit` | Bad sha, non-object pr, wrong assignee, submit from a state other than working, and submit before ready. | dead |
| 458, 464 | `verdict` | A result other than approve or request-changes, and a verdict off a card that is not in review. | dead |
| 488, 493, 496 | `merge` | Merge from a state other than review, a verify level that cannot merge, and a missing sha. | dead |
| 511, 517, 520 | `edit_card` | Edit after the card has left the editable states, and an empty body or contract. | dead |
| 544 | `metrics` | Whole function. | only unit-tested |
| 548 | `metrics` | The per-state counter. The unit test counts an empty database, so the loop body never runs. | dead |
| 557 | `metrics` | False side of `if legacy_path` (metrics with no legacy file). | dead |
| 563 | `metrics` | `brief_stats` raising `TurboError`. | dead |
| 574 | `next_open` | False side of the harness check (an open card this runner cannot take). | dead |
| 577 | `next_open` | No open card left. | dead |
| 595 | `pending_work` | False side of the request-changes arm (a pending resume in some other state). | dead |

### `turbo/store.py`

| Lines | Symbol | Why the scenarios miss it | Other tests |
|---|---|---|---|
| 111 | `Store.__init__` | False side of `if parent` (database path with no directory). | dead |
| 126 | `Store.close` | Body never called. | dead |
| 135 | `transaction` | False side of the outer commit (a nested transaction does not commit). | dead |
| 139 | `transaction` | False side of the outer rollback (a nested transaction raises). | dead |
| 168 | `require_actor` | Unknown actor name. | dead |
| 187 | `update_card` | A column name that is not on the card. | dead |
| 200 | `get` | Missing card. | dead |
| 211 | `list_cards` | Filter by harness. State filter does run. | dead |

## Reproduce

From the repository root. coverage.py must be importable as `python3 -m coverage`.

```bash
python3 -m coverage run --branch \
  --source=turbo \
  --omit='*/turbo/scenarios/*' \
  -m turbo.scenarios run turbo/scenarios
python3 -m coverage report --show-missing
```

The other-test column comes from a second run that does not include the scenario set:

```bash
python3 -m coverage run --branch \
  --source=turbo \
  --omit='*/turbo/scenarios/*' \
  -m pytest tests/test_turbo.py tests/test_turbo_runner.py -q
```

`bash scripts/scenario-coverage.sh` runs both and prints the report. It does not rewrite this file. Data files go under `$TMPDIR` (or `/tmp`), not into the tree.

Percentages above are the Cover column of that report on this source. Comment lines are not statements, so the percentages do not move when the marks move.
