# dispatchd — TT autonomous dispatch design (M3PEVTDH-5CCS)

Status: design finalized v1 (2026-09-29, M3PG5Q9W-30KP). Implementation is under the 5R5B-or-below cards.

## Decision summary
- A mini launchd job `com.tt.dispatchd` (co-resident with tt-server, TT_URL=http://127.0.0.1:7800), 30-second loop.
- **Consumes only the public HTTP API, no server changes, no private state** — all judgment grounds land on cards and dispatch history (replayable).
- Execution rounds are **non-resident one-shots** (the current dispatch protocol as-is). "Continuity" is guaranteed by the card graph (sibling/child first) and three-stage reporting. Resident LLM sessions are out of scope (a separate decision after observation).
- dispatchd is not a state machine but a **judgment function + executor**: round input (a snapshot) → a list of decisions. Policy is TDD'd as pure functions.

## API surface (measured assertions, 2026-09-29 app.py/openapi)
| purpose | real route | convention · caveats |
|---|---|---|
| Candidate query | `GET /issues?state=todo&label=auto&limit=200` | Filters: state/parent/label/assignee/q app.py:242-256. Same conditions as pull's atomic SQL (`state='todo' AND assignee=''`, app.py:350-352) |
| Claim a specific card | `POST /issues/{id}/claim {agent}` | state≠todo 409 (app.py:302), someone else's assignee 409, 2-lease limit per agent (app.py:348 — max_leases shared) — **on claim: attempt+1, contract pinned, in_progress** |
| Dispatch delivery | `POST /issues/{id}/dispatch {agent, message}` | message required (422), agent registered+enabled required (902-911). dispatch is **delivery**, not claiming — must be separated from claim (app.py:902, api.md convention) |
| Manual claim (alternative) | `POST /pull {agent, require_label}` | Cannot target an ID (pull-style). dispatchd **prefers claim** — avoids the W1DP pitfall |
| Round history | `GET /issues/{id}/dispatches` | The epicenter of attempt counting (app.py:729) |
| release_ready | `GET /issues/{id}` (why-blocked projection) | For blocked+dependency, when all deps are done/cancelled → release_ready (app.py:619-622, 695-696). **blocked is never a pull/claim candidate (app.py:350) — resumption requires a todo transition** |
| Agent list | `GET /agents` | base_url (hook), enabled, release_hook/notify_hook — dispatch targets are enabled runners only (app.py:826-836) |
| Run observation (optional) | `GET /agents/active` | FXWQ — assists round-progress judgment |

## Round algorithm (the judgment function decide(snapshot) → actions[])
1. Kill: if env `TT_AUTO_DISPATCH != 1` → do nothing (log once per 10 minutes).
2. Idle judgment: per registered agent, active leases (`GET /issues?assignee=<agent>&state=in_progress`) — only idle agents. dispatchd policy is **1 card per agent** (serial), more conservative than the server limit (2).
3. Target-search priority:
   a. The **todo children** (unfinished, deps clear) of the agent's last completed card (`assignee=<agent>`, state=done) → continuity of the same flow.
   b. **todo siblings** of that card's parent.
   c. **blocked+dependency+release_ready** → (approval basis: the server's machine judgment app.py:619-622) PATCH state=todo, then go to 4.
   d. From the candidate pool `state=todo&label=auto`, those with **clear dependencies**, lowest priority first (numeric priority sorting is an extension point).
4. Budget gate (blocks failure loops): if the target card has ≥ 2 dispatch records and state≠done → do not dispatch; add a `needs-human` label + note. `execution_attempt ≥ 2` is the same.
5. Execute: claim → dispatch (message: card title/body + claim/heartbeat/three-stage-report instructions) → note `[auto] dispatch`. A claim 409 (preempted) is silently skipped.
6. On failure: dispatch webhook 5xx/timeout → exponential backoff (30→300 s cap), card state untouched (one-shot retry happens next round, within the budget gate).
7. Skips are silent (log only) — only dispatch/needs-human/resume decisions become card notes (noise control).

## Failure criteria (a table that is refuted if wrong)
- No session/memory across rounds — state is only the snapshot right before a request. No cache.
- pull (pool-style) is unused — specific cards are claim-only.
- No re-dispatch of a twice-attempted card (not done) — needs-human. Blocks automation runaway at the source.
- dispatchd only performs dispatch/note/(for release_ready cards) todo transitions. It cannot change done/review/cancelled.
- On server failure: zero writes (backoff); leases unlock naturally via TTL (on expiry another agent steals — a one-round card cost).
- The first dispatchd itself is implemented and registered by a human (bootstrap exception). One real-runner E2E in BKXA.

## Implementation card mapping
- 5R5B: the core (decide pure function + executor + launchd plist + backoff) — TDD with an 8–10 row judgment table for the decision function.
- Z1CY: refining the graph walk of the (a)(b) continuation rules — an extension after 5R5B's basic implementation.
- TKXA: the budget gate (4) and kill switch (1) — absorbed into the decision table, so it can be merged/cancelled into 5R5B (5R5B decides).
- MCW3: log/note formats — expected to be absorbed into 5R5B (duplication can be judged).
- BKXA: a real-runner E2E.
