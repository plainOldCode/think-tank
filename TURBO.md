# think-tank turbo

Turbo is a headless runtime. The runner owns the process and the session id. An agent that needs a decision writes `blocked_question`, `blocked_options`, and `blocked_default`, then ends its turn. A person answers on the board. The runner resumes the same session with that answer as the next prompt. Interactive terminals are outside this path.

## Keep, remove, and why

| Piece | Decision | Why |
|---|---|---|
| `turbo/` server, runner, drivers, token gate, ink UI | Keep | This is the runtime. |
| Read-only archive (`turbo/legacy.py`) | Keep | Opens `TT_LEGACY_DB` with `mode=ro` and `query_only`. Lists issues, children, comments, and `completion_report`. Does not migrate or write. |
| `turbo/legacy_schema.py` | Keep, tests only | A snapshot of the issues and comments columns, used to build a throwaway file. The server never executes it against `TT_LEGACY_DB`. |
| Galmuri Mono and its OFL text, under `turbo/static/fonts/` | Keep | The ink page. Copied here so `server/static` can go. The page URL is still `/fonts/GalmuriMono11.woff2`. |
| `scripts/secret-scan.sh` | Keep | CI still refuses the same secret patterns. |
| `deploy/com.tt.server.plist` and `scripts/deploy.sh` | Left byte-for-byte | Deploy settings were not edited on this cut. They are not part of the turbo runtime. |
| `server/` (app, routes, probe, board, work contract) | Remove | Turbo does not import it. The columns the archive reads are copied into `turbo/legacy_schema.py`. |
| `runner/tt-runner.py` and the tmux runner tests | Remove | The file types into a pane and attaches a session. Turbo resumes with a headless command. |
| `skills/tt/` and the work-contract, receive-mode, and stall prose | Remove | The contract is the token gate and `scope`, not a skill or `AGENTS.md`. |
| `cli/tt` | Remove | Turbo does not call it. There is no `tt show` or `tt wait`. |
| `daemon/`, `docs/`, `metrics/`, legacy tests, and the other scripts | Remove | Nothing in `turbo/` imports them. |
| Agent `sleep` / `tt show` polling, mid-turn steer, keystroke injection | Do not add | The runner notices board events. |

## Implemented and still open

Implemented in `turbo/`:

- State machine: `open → working → question → awaiting-human → working → submit → review → merged`, plus `review → working` on request-changes.
- Board question. The agent posts the three blocked fields and ends the turn. The runner moves the card to `awaiting-human` when the process exits, or after 120 seconds by interrupting the child. A person answers on the board. If nobody answers for 24 hours, `blocked_default` is applied. The resume prompt is `[tt resume] wait_token=… kind=answer|verdict`.
- Token gate. Claim returns the body, contract, scope, version, and a one-time nonce. Ack must send `sha256` of the canonical JSON of those fields. Submit without the work token is rejected. An edit bumps `contract_version`, clears the token, and requires a new ack.
- Machine-readable `scope`: `paths`, `forbidden`, `verify[{cmd, kind, timeout_s?}]`, optional `timebox_s`. The server reruns verify. Each command is killed after its `timeout_s` (1–120, default 120). `failed` and `planned-only` become request-changes. Approve is rejected unless the level is `live-verified`, `unit-test-verified`, or `type-check-only`.
- Merge is allowed only for a probe actor or the assignee runner, after approve, a passed verify, and a stored sha.
- Runner-owned submit. The runner commits, pushes, opens the PR, then calls submit. An agent submit is rejected.
- Headless drivers. Claude and Codex use the confirmed resume commands. Grok CLI uses `grok -r`. Cursor does `agent create-chat` then `agent -p --resume --trust` (flags only). Grok Bot is a webhook whose 200 means the call was accepted. OpenCode and Hermes record a handoff and do not spawn a process. Resume failure falls back to fork, then handoff.
- Read-only archive of an existing tt database.
- Counters for repeated request-changes, scope completeness, and how many legacy cards have all four brief fields filled. Nothing writes a score.

Open, with the choice this cut made:

- Question grace is 120 seconds. The default answer waits 24 hours. Both are constants in `turbo/constants.py`.
- Turbo uses its own sqlite file (`TURBO_DB`). The legacy file is never passed to a migrator.
- Actors are rows (`human`, `runner`, `agent`, `probe`). The first actor must be a human. This is not production authentication.
- Cursor's continued turn and Grok Bot's same-conversation context were not confirmed. OpenCode and Hermes have no guessed CLI.
- There is no push channel. `python -m turbo.runner` ticks on an interval (default 5 seconds). That loop is the runner's schedule, not an agent wait.
- A handoff or a turn that exits without a question or a ready tree stays `working`, so that runner does not claim another card.
- `deploy/com.tt.server.plist` and `scripts/deploy.sh` still describe the old server. They were not updated and they are not how turbo starts.

## How to run

From the repository root. `TURBO_GROK_BOT_BEARER`, if used, comes from the environment and is not written into the repo.

```bash
pip install fastapi uvicorn
export TURBO_DB=data/turbo.db
export TT_LEGACY_DB=/path/to/the/existing/tt.sqlite
export TURBO_PORT=7810
python -m turbo
```

The ink board is `http://127.0.0.1:7810/`. The archive column is read-only. Answers apply only to turbo cards in `awaiting-human`.

```bash
curl -s -X POST localhost:7810/turbo/actors -H 'content-type: application/json' \
  -d '{"name":"ada","role":"human"}'
```

One runner tick:

```bash
export TURBO_URL=http://127.0.0.1:7810
export TURBO_ACTOR=run-1
export TURBO_AGENT=worker
export TURBO_HARNESSES=claude,codex
export TURBO_WORKSPACE=.
python -m turbo.runner --once
```

Tests:

```bash
python -m pytest tests/ -q
node tests/smoke_turbo_ui.mjs
```

`httpx` is installed for the FastAPI test client. CI does not install a separate `pydantic` package; FastAPI pulls it in.

## Scenarios

`turbo/scenarios/*.json` is a reusable set. Each file is one scenario (a file may also be a JSON list). `python -m turbo.scenarios run` starts a real turbo server on a throwaway port and temporary sqlite files, then drives the steps through HTTP and `turbo.runner.Runner`. Harness argv comes from `turbo.drivers` and is captured, not executed. `git` and `gh` are the real `GitCli` argument lists with a stubbed runner, so no remote is contacted. The clock is the server clock: `advance` moves it, and the runner does not sleep for the 24 hour default.

```bash
python -m turbo.scenarios run                  # every json file next to the runner
python -m turbo.scenarios run turbo/scenarios/01-happy-path.json
python -m turbo.scenarios run turbo/scenarios --json /tmp/turbo-scenarios.json
```

Exit status is non-zero when a scenario or a step fails. `--json` writes `{ok, passed, failed, scenarios: [{name, ok, steps}]}`. `pytest tests/test_scenarios.py` runs the same set, and CI runs `pytest tests/`.

A scenario object:

| Field | Meaning |
|---|---|
| `name` | Required. Printed in the report. |
| `description` | What the scenario is checking. |
| `actors` | `{name, role, harnesses?}`. The first actor is a human. Roles: `human`, `runner`, `agent`, `probe`. |
| `runner` | Optional `{actor, agent, harnesses}`. Defaults to the first runner and the first agent. |
| `webhook` | Optional URL. The runner posts Grok Bot launches here through an in-process stub. Nothing is sent on the network. |
| `bearer` | Optional. Redacted from runner logs. Sent as `Authorization` when `webhook` is set. |
| `cards` | `{ref, title, body, contract, harness, scope}`. `scope` is `{paths, forbidden, verify: [{cmd, kind, timeout_s?}]}`. |
| `git` | Optional `{changed, sha, pr_number, porcelain, pr_stdout}` for the stub. `sha` is 40 hex characters. `porcelain` is raw `git status` text. `pr_stdout` replaces the fake pull-request URL. |
| `legacy` | Optional `{issues, comments}` inserted into a throwaway copy of the issues and comments tables. The runner checks the file is unchanged at the end. |
| `legacy_kind` | Optional `empty` or `thin`, instead of the full throwaway schema. |
| `legacy_unconfigured` | No legacy path is passed to the server. |
| `legacy_chmod` | Mode applied to the legacy file before the steps, such as `0`. |
| `db` | `flat` opens the turbo database as a file in the working directory, with no parent. |
| `proc` | `subprocess` runs harness argv through `SubprocessProc`. The binary is a stub on `PATH`, not the real CLI. |
| `steps` | Ordered. Each step is one line in the report. |

A step has `name`, `op`, optional `card` (a ref), optional `arm` (`{exit, output, alive}` for a captured harness, or `{mode, output}` for a `subprocess` stub: `ok`, `sleep`, `unlink`), optional `release` (`{exit, output}` to finish the live harness), optional `git_changed` (paths the stub reports), and optional `expect`.

Ops: `tick` (one runner loop; a `TurboError` is the step status when `expect.status` is set), `advance` (`seconds`), `ack` (`actor`, `digest: "bad"`, `nonce: "wrong"` or `"short"`), `ready`, `ask` (`blocked_question`, `blocked_options`, `blocked_default`), `answer` (`choice`), `edit`, `verdict` (`result`), `submit` (runner commit, stubbed `gh pr create`, server verify), `submit-raw` (`work_token` `$token` or `$old`, explicit `changed_paths` including `[]`, `sha`, `pr`), `merge` (`via`: `runner` or `http`), `claim`, `claim-race` (`claims`, one winner), `create-card`, `register` (`actor_name`, `role`, `harnesses`, `by`), `turn-closed`, `session`, `resume` (`token`, including `$resume`), `get` (`actor`), `http` (`method`, `path`, `body`), `tamper` (`set` of `body`, `sha`, `verify_level`, or `harness`), `git-merge` (`number`, `sha`), `legacy-get`, `legacy-write`, `legacy-recreate`, `plain-http`, `module` (`module` `turbo` or `turbo.runner`, `args`, `env`, `stop`, `webhook`), `noop`.

`expect` also checks `has_nonce`, `card_count`, `metric_cards`, `legacy_cards`, `legacy_brief_complete`, `log_contains`, `log_excludes`, `legacy_labels`, `legacy_report`, `webhook_posts`, `webhook_contains`, `webhook_auth`, `verify_contains`, `text_contains`, `bytes`, `posts`, and `auth`. A scenario may set `bearer` (redacted from runner logs) or `legacy_missing` (the archive path is configured and the file is absent). `actors` may be empty when the steps register the first actor themselves.

`expect` can check `status`, `error` (substring), card `state`, `verdict`, `verify_level`, `answer`, `answer_by`, `ready`, `contract_version`, `session_mode`, `session_id`, `resume_contains`, `same_session`, `sha`, `pr_number`, `spawns`, `argv0`, `argv_contains`, `git_calls`, `no_commit`, `no_merge`, `interrupted`, `wins`, and the legacy body, children, comment, and report. `expect.card` selects which card those fields read when a scenario has more than one.

## Verification level

Commands run on this branch. Claude, Codex, Cursor, and Grok were not executed. No live tt server, production database, board card, or deploy target was contacted. `deploy/com.tt.server.plist` and `scripts/deploy.sh` were not edited.

```bash
python3 -m turbo.scenarios run turbo/scenarios
bash scripts/scenario-coverage.sh
python3 -m pytest tests/ -q
python3 -m mypy turbo --ignore-missing-imports
node tests/smoke_turbo_ui.mjs
python3 -m compileall -q turbo tests
bash scripts/secret-scan.sh .
```

`mypy` is not part of CI. CI runs `scripts/scenario-coverage.sh`, which fails if scenario-only statement or branch coverage drops below 100%. The scenario command starts a real server per scenario. Harness CLIs and `gh` are not executed.

- Scenario set, including the archive read and the rejected legacy write, against temporary sqlite files: live-verified. Harness binaries and `gh` were not executed.
- Token gate, state machine, and driver argv in the smaller unit tests: unit-test-verified.
- Live harness resume and `gh pr create` against a real repository: planned-only.
