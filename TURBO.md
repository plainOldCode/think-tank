# think-tank turbo

Turbo is a headless runtime on this branch. The runner owns the process and the session id. An agent that needs a decision writes `blocked_question`, `blocked_options`, and `blocked_default`, then ends its turn. A person answers on the board. The runner resumes the same session with that answer as the next prompt. Interactive terminals are outside this path.

The legacy server and its tmux runner are still in the tree. Turbo does not import them. See the table below for why they were not deleted in this cut.

## Keep, remove, and why

| Piece | Decision | Why |
|---|---|---|
| Issue schema, comments, `completion_report`, the ink board (`server/`, `server/static/`) | Keep | This is the current tt data. Turbo opens that sqlite file read-only and renders it. The existing CI job still runs `pytest tests/` and the board smokes against this code, and CI settings were not changed. |
| `runner/tt-runner.py` and its tests | Keep the file, off the turbo path | The file is a tmux session runner: `continue_session` types into a pane, and `cmd_attach` is for watching. CI runs `pytest runner/test_tt_runner.py` plus other tests that import it. Turbo's runner never imports it. Deleting it would fail that untouched job. |
| `skills/tt/`, work-contract prose, receive-mode, stall kill, claim budget | Keep as legacy only | Those exist so agents will notice a contract. Turbo does not enforce through a skill or `AGENTS.md`. They are not on the turbo path. |
| `scripts/tt-wake-relay.py` and `tests/test_wake_relay.py` | Remove | The script subscribed to `/events/stream` and POSTed into the opencode TUI (`/tui/append-prompt` and `/tui/submit-prompt`). That is interactive injection. Nothing else imported it. |
| Agent `sleep` / `tt show` polling, `tt wait`, mid-turn steer, keystroke injection | Do not add | The design's open question about `tt wait` is closed for turbo: there is no wait API. The runner notices board events. Agents have nothing to poll. |

## Implemented and still open

Implemented in `turbo/`:

- State machine: `open → working → question → awaiting-human → working → submit → review → merged`, plus `review → working` on request-changes.
- Board question. The agent posts the three existing blocked fields and ends the turn. The runner moves the card to `awaiting-human` when the process exits, or after 120 seconds by interrupting the child. A person answers on the board. If nobody answers for 24 hours, `blocked_default` is applied. The answer is a card event and the resume prompt is `[tt resume] wait_token=… kind=answer|verdict`.
- Token gate. Claim returns the body, contract, scope, version, and a one-time nonce. Ack must send `sha256` of the canonical JSON of those fields. Submit without the work token is rejected. An edit bumps `contract_version`, clears the token, and requires a new ack.
- Machine-readable `scope`: `paths`, `forbidden`, `verify[{cmd, kind}]`, optional `timebox_s`. Submit rejects paths outside `paths` or inside `forbidden`.
- The server reruns `scope.verify` and sets the level from the exit codes. The agent's own level is not an input. `failed` and `planned-only` become request-changes. Approve is rejected unless the level is `live-verified`, `unit-test-verified`, or `type-check-only`.
- Merge is allowed only for a probe actor or the assignee runner, and only after approve plus a passed verify and a stored sha.
- Runner-owned submit. The runner commits, pushes, opens the PR, then calls submit with the fixed sha and the work token. An agent submit is rejected.
- Headless drivers. Claude and Codex are the confirmed resume commands (`claude -p … --resume`, `codex exec resume … --json`), with system text via `--append-system-prompt-file` and `-c developer_instructions`. Grok CLI uses `grok -r … -p` and `--rules`. Cursor does `agent create-chat` then `agent -p --resume --trust` (partial: flags only). Grok Bot is a webhook POST whose body is the card id and the event; a 200 only means the call was accepted. OpenCode and Hermes record a handoff prompt and do not spawn a process. Resume failure falls back to fork (Claude, Codex, Grok) and then to handoff. One session has one live child.
- Read-only archive of the current tt database: issues, children, comments, and `completion_report`.
- Counters for repeated request-changes, scope completeness, and how many legacy cards have all four brief fields filled. Nothing can write a score.

Open, with the choice this cut made:

- Question grace is 120 seconds. The default answer waits 24 hours. Both are constants in `turbo/constants.py`.
- Turbo uses its own sqlite file (`TURBO_DB`). The legacy file (`TT_LEGACY_DB`) is opened with `mode=ro` and `query_only`. Turbo does not run the legacy migrations.
- Actors are rows (`human`, `runner`, `agent`, `probe`). The first actor must be a human; later actors are added by a human. This is not the legacy webhook secret and it is not production authentication.
- Driver order is Claude, Codex, then Grok, Cursor, Grok Bot, with OpenCode and Hermes on the handoff path only.
- Cursor's continued turn and Grok Bot's same-conversation context were not confirmed. The code builds the command or the webhook and labels the status.
- OpenCode and Hermes resume commands were not investigated, so there is no port relay and no guessed CLI.
- There is no push channel. `python -m turbo.runner` ticks on an interval (default 5 seconds). That loop is the runner's schedule, not an agent wait.
- A handoff or a turn that exits without a question or a ready tree stays `working`, so that runner does not claim another card.
- The frozen tmux runner is still in the repository because CI still executes it.

## How to run

From the repository root, with the process environment pointed at databases. Do not put secrets in the shell history of a shared log; `TURBO_GROK_BOT_BEARER` is read from the environment at runtime and is not written into the repo.

```bash
pip install fastapi uvicorn pydantic
export TURBO_DB=data/turbo.db
export TT_LEGACY_DB=/path/to/the/existing/tt.sqlite
export TURBO_PORT=7810
python -m turbo
```

The ink board is `http://127.0.0.1:7810/`. The archive column is read-only. Answers apply only to turbo cards in `awaiting-human`.

Create the human, a runner, and an agent, then a card whose `scope` is JSON (`paths`, `verify`). Claim and ack:

```bash
# actor names below are examples, not credentials
curl -s -X POST localhost:7810/turbo/actors -H 'content-type: application/json' \
  -d '{"name":"ada","role":"human"}'
```

One runner tick, in the work tree it should commit:

```bash
export TURBO_URL=http://127.0.0.1:7810
export TURBO_ACTOR=run-1
export TURBO_AGENT=worker
export TURBO_HARNESSES=claude,codex
export TURBO_WORKSPACE=.
python -m turbo.runner --once
```

`TURBO_GROK_BOT_WEBHOOK_URL` is optional. When it is set, the runner POSTs the card id and the event. The bearer, if any, comes from `TURBO_GROK_BOT_BEARER` and is not logged.

Tests:

```bash
python -m pytest tests/test_turbo.py tests/test_turbo_runner.py -q
node tests/smoke_turbo_ui.mjs
```

## Verification level

Commands run on this branch. Claude, Codex, Cursor, and Grok were not executed. No live tt server, production database, board card, or deploy target was contacted.

```bash
python3 -m pytest tests/test_turbo.py tests/test_turbo_runner.py -q
python3 -m pytest tests/ runner/test_tt_runner.py -q
python3 -m mypy turbo --ignore-missing-imports
node tests/smoke_turbo_ui.mjs
node tests/smoke_mobile.mjs
node tests/smoke_theme_wiring.mjs
node tests/smoke_agent_board.mjs
python3 -m compileall -q turbo
bash scripts/secret-scan.sh .
```

`mypy` is not part of CI. The secret scan runs on tracked files, so it was run after these files were staged.

A throwaway `python -m turbo` on port 7810, with `TURBO_DB` and `TT_LEGACY_DB` under `/tmp`, served `/`, the ink CSS, the board script, and the Galmuri font. The same process listed and detailed a seeded legacy issue (parent, child, comment, `completion_report`), rejected an agent submit, and recorded a human answer. The legacy file was opened read-only and its rows did not change. A browser then opened that page, opened the archive parent and child, set the actor field, clicked the turbo card's `no` answer, reloaded, and checked a 390px width. The only console error was a missing favicon.

- Board page and the HTTP question/answer path, against that temporary database: live-verified.
- Token gate, state machine, scope checks, driver argv, git/gh command sequence, and the read-only archive assertions: unit-test-verified.
- `SubprocessProc` was exercised with a local `python -c` child. That does not verify a harness resume.
- Live harness resume, webhook delivery, and `gh pr create` against a real repository: planned-only.
