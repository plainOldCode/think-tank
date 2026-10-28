# think-tank turbo

Turbo is a headless runtime. The runner owns the process and the session id. An agent that needs a decision writes `blocked_question`, `blocked_options`, and `blocked_default`, then ends its turn. A person answers on the board. The runner resumes the same session with that answer as the next prompt. Interactive terminals are outside this path.

Turbo is the board. A legacy issues file is migrated into `TURBO_DB` once, then turbo is authoritative. The archive view of that file stays read-only.

## Trust model

This is a single-machine trial, not production authentication.

- The server binds `127.0.0.1` only. It is not a network service.
- Actor rows are client-supplied. `register_actor` trusts the body. `merge` and the other role checks trust those rows.
- Anyone with a shell on the machine can register a probe, or call any route as any actor that has been registered.
- A shell user can also mint a work token by reading the sqlite file. The token gate stops a different agent on the same board. It does not stop the person at the keyboard.

## Keep, remove, and why

| Piece | Decision | Why |
|---|---|---|
| `turbo/` server, runner, drivers, token gate, ink UI | Keep | This is the runtime. |
| Read-only archive (`turbo/legacy.py`) | Keep | Opens `TT_LEGACY_DB` with `mode=ro` and `query_only`. Lists issues, children, comments, and `completion_report`. The archive route does not write. |
| `turbo/legacy_schema.py` | Keep, tests only | A snapshot of the issues and comments columns, used to build a throwaway file. The server never executes it against `TT_LEGACY_DB`. |
| Galmuri Mono and its OFL text, under `turbo/static/fonts/` | Keep | The ink page. Copied here so `server/static` can go. The page URL is still `/fonts/GalmuriMono11.woff2`. |
| `scripts/secret-scan.sh` | Keep | CI still refuses the same secret patterns. |
| `deploy/com.tt.server.plist` and `scripts/deploy.sh` | Start turbo, do not deploy | The plist runs `python3 -m turbo` on `127.0.0.1` via the app. `scripts/deploy.sh` prints the start and migrate commands and exits. It does not tag, curl, or copy files. |
| `server/` (app, routes, probe, board, work contract) | Remove | Turbo does not import it. The columns the archive reads are copied into `turbo/legacy_schema.py`. |
| `runner/tt-runner.py` and the tmux runner tests | Remove | The file types into a pane and attaches a session. Turbo resumes with a headless command. |
| `skills/tt/` and the work-contract, receive-mode, and stall prose | Remove | The contract is the token gate and `scope`, not a skill or `AGENTS.md`. |
| `cli/tt` | Remove | Turbo does not call it. There is no `tt show` or `tt wait`. |
| `daemon/`, `docs/`, `metrics/`, legacy tests, and the other scripts | Remove | Nothing in `turbo/` imports them. |
| Agent `sleep` / `tt show` polling, mid-turn steer, keystroke injection | Do not add | The runner notices board events. |

## Implemented and still open

Implemented in `turbo/`:

- State machine: `open → working → question → awaiting-human → working → submit → review → merged`, plus `review → working` on request-changes.
- Board question. The agent posts the three blocked fields and ends the turn. `confirm: true` marks an approval-shaped question: the 24 hour default never applies it. Only a question with `confirm` false and a `blocked_default` is auto-applied after 24 hours. The runner moves the card to `awaiting-human` when the process exits, or after 120 seconds by sending SIGINT. That move does not wait for the child. If the child is still alive after `TURBO_KILL_WAIT_S` (default `KILL_WAIT_S`, 5 seconds), the next tick sends SIGKILL. The resume prompt is `[tt resume] wait_token=… kind=answer|verdict`.
- Token gate. Claim returns the body, contract, scope, version, and a one-time nonce. Ack must send `sha256` of the canonical JSON of those fields. Submit without the work token is rejected. An edit bumps `contract_version`, clears the token, and requires a new ack.
- Machine-readable `scope`: `paths`, `forbidden`, `verify[{cmd, kind, timeout_s?}]`, optional `timebox_s`, optional `scenario`, optional `exempt`. The server reruns verify. Each command is killed after its `timeout_s` (1–120, default 120). `failed` and `planned-only` become request-changes. Approve is rejected unless the level is `live-verified`, `unit-test-verified`, or `type-check-only`.
- Scenario red-green, not unit-test TDD. A bug or feature card must name `scope.scenario`, a JSON file with a `cmd`. Submit runs that command with `TURBO_REV=base` (must exit non-zero) and `TURBO_REV=head` (must exit 0) before the card can enter review. The command is not a shell. A card with no reproducible failure (docs, config, imported history) sets `scope.exempt` true and carries no scenario. Unit tests are the exception, for a pure function with many edge cases, and they still sit behind `exempt` or behind a scenario that fails on base.
- Merge is allowed only for a probe actor or the assignee runner, after approve, a passed verify, and a stored sha.
- Runner-owned submit. The runner commits, pushes, opens the PR, then calls submit. An agent submit is rejected.
- Headless drivers. Claude and Codex use the confirmed resume commands. Grok CLI uses `grok -r`. Cursor is partial: `agent create-chat` then `agent -p --resume --trust` (flags only). Grok Bot is a webhook whose 200 means the call was accepted. OpenCode and Hermes are handoff-only. Their `STATUS` value is `handoff-only`. They have no resume CLI, the runner does not spawn them, and the question-answer-same-session path does not reach them. Resume failure on a forkable harness falls back to fork, then handoff. This is not full harness coverage.
- Read-only archive of an existing tt database, plus a one-shot migrator (`python -m turbo.migrate`) that reads that file read-only and copies issues into `TURBO_DB`.
- Counters for repeated request-changes, scope completeness, how many legacy cards have all four brief fields filled, and `stuck`: working cards with no live runner handle. A handoff is stuck for a different reason than a turn whose process is gone. The ink page lists them. Nothing writes a score.

Open, with the choice this cut made:

- Question grace is 120 seconds. The kill wait is 5 seconds unless `TURBO_KILL_WAIT_S` is set to a number. The default answer waits 24 hours and does not apply to `confirm` questions. The numbers live in `turbo/constants.py`.
- Actors are rows (`human`, `runner`, `agent`, `probe`). The first actor must be a human. See the trust model above.
- Cursor's continued turn and Grok Bot's same-conversation context were not confirmed. OpenCode and Hermes stay handoff-only. No CLI is invented for them.
- There is no push channel. `python -m turbo.runner` ticks on an interval (default 5 seconds). That loop is the runner's schedule, not an agent wait.
- A handoff or a turn that exits without a question or a ready tree stays `working`, so that runner does not claim another card. `metrics.stuck` is how that card stays visible.

## Planned

Not implemented. Verification level: planned-only.

Awaiting-human notification via Hermes to Telegram. A remote session on the main think-tank left a selection prompt waiting for hours, which is why a question belongs on the board and why the person needs a ping when the board is waiting.

The board stays the only place a question lives and the only place an answer is accepted. The runner still resumes from that board answer. The Telegram message carries the card id, the title, a one-line summary of the question, and a link to the card. It does not carry the answer buttons. `confirm: true` questions and the interrupt path (SIGINT, then SIGKILL, card already in `awaiting-human`) are included. An optional second trigger is a card appearing in `metrics().stuck`.

Delivery uses the Hermes agent's existing Telegram channel. Hermes stays handoff-only as a harness: the runner does not spawn it, and this notifier is not a session. The send is fire-and-forget. If it fails, the card state does not change and the failure is logged.

One notification per question, keyed by the question id (the planned `idempotency_key` on that event). A later tick does not send it again. Stuck notices are deduplicated the same way, once per stuck episode, and the sender is rate-limited.

Off unless configured. `TURBO_NOTIFY` enables it. `TURBO_NOTIFY_BOT_TOKEN` and `TURBO_NOTIFY_CHAT_ID` are read from the host environment. Those values are not written into the repo, the database, or the message log. In a shadow run the notifier stays off even when `TURBO_NOTIFY` is set, so the person is not paged twice. See [turbo/SHADOW_RUN.md](turbo/SHADOW_RUN.md).

A future scenario drives a stub notifier. Tests do not call Telegram or Hermes.

## Cutover

Turbo replaces the legacy board. The archive does not keep taking claims.

The proof stage before a human chooses this cutover is a planned shadow run: [turbo/SHADOW_RUN.md](turbo/SHADOW_RUN.md).

1. Freeze legacy claims. Stop the old server. Do not claim, lease, or answer on the legacy file after this.
2. Dry-run: `python -m turbo.migrate --legacy <legacy.db> --turbo <turbo.db> --dry-run`. The legacy file is opened read-only. A missing turbo file is not created.
3. Import: the same command without `--dry-run`. Re-running does not duplicate ids or comments.
4. Start turbo (`python -m turbo`, bound to `127.0.0.1`). Turbo is authoritative. New claims happen only there.

In-flight legacy cards are not left on a dead assignee:

| Legacy state | Turbo state |
|---|---|
| `backlog`, `todo`, `in_progress` | `open`, assignee cleared. The previous assignee and lease are noted on the body. A turbo runner claims fresh. |
| `blocked` with a question | `awaiting-human`, question copied, `confirm` set so the 24 hour default cannot apply it |
| `blocked` without a question | `open`, `blocked_detail` noted on the body |
| `review` | `review`, assignee and verdict cleared |
| `done` | `merged` |
| `cancelled`, empty title, unknown state | skipped and listed in the report |

Imported cards use harness `hermes` (handoff-only) and `scope.exempt` until a person edits the harness. Comments become `comment` events. The legacy file is not modified.

## How to run

From the repository root. `TURBO_GROK_BOT_BEARER`, if used, comes from the environment and is not written into the repo.

```bash
pip install fastapi uvicorn
export TURBO_DB=data/turbo.db
export TT_LEGACY_DB=/path/to/the/existing/tt.sqlite
export TURBO_PORT=7810
python -m turbo
```

Copy a frozen legacy file once. The legacy path is read-only. Repeat the command to confirm it maps nothing new.

```bash
python -m turbo.migrate --legacy /path/to/the/existing/tt.sqlite --turbo data/turbo.db --dry-run
python -m turbo.migrate --legacy /path/to/the/existing/tt.sqlite --turbo data/turbo.db
```

The ink board is `http://127.0.0.1:7810/`. The archive column is the read-only legacy file. Turbo is the board. Answers apply only to turbo cards in `awaiting-human`. `confirm` questions say the default will not auto-apply. Working cards with no live handle are listed under stuck.

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
| `cards` | `{ref, title, body, contract, harness, scope, proof?}`. `scope` is `{paths, forbidden, verify: [{cmd, kind, timeout_s?}], scenario?, exempt?}`. `proof: false` leaves the scenario unset. `exempt: true` is the no-failure card. Other seeded cards receive a throwaway scenario that fails on base and passes on head. |
| `git` | Optional `{changed, sha, pr_number, porcelain, pr_stdout}` for the stub. `sha` is 40 hex characters. `porcelain` is raw `git status` text. `pr_stdout` replaces the fake pull-request URL. |
| `legacy` | Optional `{issues, comments}` inserted into a throwaway copy of the issues and comments tables. The runner checks the file is unchanged at the end. |
| `legacy_kind` | Optional `empty` or `thin`, instead of the full throwaway schema. |
| `legacy_unconfigured` | No legacy path is passed to the server. |
| `legacy_chmod` | Mode applied to the legacy file before the steps, such as `0`. |
| `db` | `flat` opens the turbo database as a file in the working directory, with no parent. |
| `proc` | `subprocess` runs harness argv through `SubprocessProc`. The binary is a stub on `PATH`, not the real CLI. Stub modes: `ok`, `sleep`, `unlink`, `ignore-int` (SIGINT is ignored). The `ignore-int` spawn returns only after that disposition is installed. |
| `kill_wait_s` | Sets `TURBO_KILL_WAIT_S` for this scenario. The clock advances it. The test does not sleep. |
| `steps` | Ordered. Each step is one line in the report. |

A step has `name`, `op`, optional `card` (a ref), optional `arm` (`{exit, output, alive}` for a captured harness, or `{mode, output}` for a `subprocess` stub: `ok`, `sleep`, `unlink`, `ignore-int`), optional `release` (`{exit, output}` to finish the live harness), optional `git_changed` (paths the stub reports), and optional `expect`.

Ops: `tick` (one runner loop; a `TurboError` is the step status when `expect.status` is set), `advance` (`seconds`), `ack` (`actor`, `digest: "bad"`, `nonce: "wrong"` or `"short"`), `ready`, `ask` (`blocked_question`, `blocked_options`, `blocked_default`, optional `confirm`), `answer` (`choice`), `edit`, `verdict` (`result`), `submit` (runner commit, stubbed `gh pr create`, server verify, and the scenario red-green check), `submit-raw` (`work_token` `$token` or `$old`, explicit `changed_paths` including `[]`, `sha`, `pr`, `omit_proof`, `include_proof`), `proof-write` (replace the injected scenario file, or `missing: true` to unlink it, or `text: "default"`), `merge` (`via`: `runner` or `http`), `claim`, `claim-race` (`claims`, one winner), `create-card`, `register` (`actor_name`, `role`, `harnesses`, `by`), `turn-closed`, `session`, `resume` (`token`, including `$resume`), `get` (`actor`), `http` (`method`, `path`, `body`), `tamper` (`set` of `body`, `sha`, `verify_level`, or `harness`), `git-merge` (`number`, `sha`), `legacy-get`, `legacy-write`, `legacy-recreate`, `plain-http`, `module` (`module` `turbo`, `turbo.runner`, or `turbo.migrate`; `args` may use `$legacy`, `$turbo`, `$empty`, `$thin`, `$out2`; `env`, `stop`, `webhook`), `noop`.

`expect` also checks `has_nonce`, `card_count`, `metric_cards`, `legacy_cards`, `legacy_brief_complete`, `log_contains`, `log_excludes`, `legacy_labels`, `legacy_report`, `webhook_posts`, `webhook_contains`, `webhook_auth`, `verify_contains`, `text_contains` (a string or a list of strings), `bytes`, `posts`, `auth`, `confirm`, `kills`, `stuck` (`[{card, reason}]`), and `turbo_exists`. A scenario may set `bearer` (redacted from runner logs) or `legacy_missing` (the archive path is configured and the file is absent). `actors` may be empty when the steps register the first actor themselves.

`expect` can check `status`, `error` (substring), card `state`, `verdict`, `verify_level`, `answer`, `answer_by`, `ready`, `contract_version`, `session_mode`, `session_id`, `resume_contains`, `same_session`, `sha`, `pr_number`, `confirm`, `spawns`, `argv0`, `argv_contains`, `git_calls`, `no_commit`, `no_merge`, `interrupted`, `kills`, `wins`, and the legacy body, children, comment, and report. `expect.card` selects which card those fields read when a scenario has more than one.

## Verification level

Commands run on this branch. Claude, Codex, Cursor, and Grok were not executed. No live tt server, production database, board card, or deploy target was contacted. `deploy/com.tt.server.plist` starts `python3 -m turbo`. `scripts/deploy.sh` does not deploy.

```bash
python3 -m turbo.scenarios run turbo/scenarios
bash scripts/scenario-coverage.sh
python3 -m pytest tests/ -q
python3 -m mypy turbo --ignore-missing-imports
node tests/smoke_turbo_ui.mjs
python3 -m compileall -q turbo tests
bash scripts/secret-scan.sh .
```

CI runs secret-scan, pytest, mypy, the UI smoke, and `scripts/scenario-coverage.sh`. The coverage script fails if scenario-only statement or branch coverage drops below 100%. The scenario command starts a real server per scenario. Harness CLIs and `gh` are not executed.

- Scenario set, including the archive read and the rejected legacy write, against temporary sqlite files: live-verified. Harness binaries and `gh` were not executed.
- Token gate, state machine, and driver argv in the smaller unit tests: unit-test-verified.
- Live harness resume and `gh pr create` against a real repository: planned-only.
- Awaiting-human notification via Hermes to Telegram: planned-only. No scenario calls Telegram or Hermes.
