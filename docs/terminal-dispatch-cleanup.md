# Terminal dispatch cleanup (TT M4R6TJ9K-DTD5)

## Design

The 2026-10-12 card records 11 active dispatch projections on completed issues
(oldest 79 hours), rejected cleanup requests with HTTP 409, and 120 shell-only
tmux sessions removed manually. Dispatch delivery status and execution status are
separate: release hooks are best effort and do not close the server projection.
The terminal progress CAS also rejects old attempts even when the issue is closed.
On the runner, a surviving interactive shell passes `has-session`; failed pane
observations skip stall detection, and terminal ledger entries are never swept.

Alternatives:

| Choice | Benefit | Cost / decision |
| --- | --- | --- |
| Probe-only sweep | Repairs old rows periodically | Delayed cleanup and dependency on probe; not chosen |
| Filter `/agents/active` | Hides stale rows | Leaves incorrect persisted execution state; not chosen |
| Transactional transition cascade | Immediate persisted cleanup with outbox evidence | Must cover common bump, human verify and auto archive; chosen |
| Kill every terminal session | Simple | Can interrupt resumed or manually started work; rejected |
| Inspect all pane process trees | Preserves living work, removes idle shells/dead panes | Unknown observations preserve sessions; chosen |

Close only `queued/running/stalled` projections as `finished`, with `ended_at`
and `dispatch.updated` in the issue transaction. Preserve finished/failed records,
delivery status, session identity and report receipt history. Allow an old attempt
to send terminal progress only for done/cancelled/archived issues, using a single
conditional SQL update. Old-attempt comments remain suppressed so historical
reports cannot become completion evidence for a later attempt. Late active
progress on closed issues must not resurrect execution; reopening restores CAS.

The runner checks physical session state before heartbeat-based stall detection.
Missing sessions, dead panes and idle shells without descendants are failures
without an exit marker (never infer success). The existing finalization and pending
report paths retain round ownership and retry behavior. Terminal ledger entries
are swept on watch ticks, restricted to recorded `tt-*` sessions. Live work in any
pane, another active ledger owner, or inspection failure prevents cleanup. Queued
entries are left to the launch path to avoid treating session creation as death.

## Verification plan

Write failing pytest regressions before implementation for all three active states
across PATCH done/cancelled/archive, agent verify, human verify and auto archive;
test old-attempt finished/failed acceptance, comment suppression, reopen rejection,
authentication, event evidence and late active progress. Runner tests cover missing
sessions without heartbeat, dead/idle panes, live descendants, unknown observation,
multiple panes and round changes. Run the same tests after implementation, then
the server and runner suites. An isolated database/TestClient, local hook receiver
and private tmux socket reproduce dispatch creation -> issue termination -> zero
active rows and zero idle sessions, plus a live-process preservation control.

Production deployment/restart and production cleanup are excluded. The local
experiment substitutes for production verification until a separately authorized
post-merge deployment. Installed runner sources were inspected read-only; changes
are confined to the designated clone. No schema migration is needed.

## Implementation and measured results

`service.bump` closes projections before committing issue changes. The two raw
SQL paths (human verify and auto archive) call the same helper. Each closed
projection emits an outbox event. Progress requests serialize their issue snapshot
and report receipt check; the terminal UPDATE retains the atomic attempt predicate
with a closed-issue exception. Old accepted retries preserve current projections,
and newly arriving old-attempt cleanup does not create report receipts/comments.

The runner observes all panes and shell child processes, finalizes missing work
without heartbeat, and sweeps terminal sessions under the ledger ownership guard.
Failed observation, active ownership or living work prevents idle cleanup.

Measured locally on the updated clone:

- RED before implementation: **25 failed, 4 passed**, including missing cascades,
  old-attempt HTTP 409 and missing runner cleanup. The preliminary sandbox run
  could not bind local hooks; the recorded RED rerun used a local hook receiver.
- Full regression: `ulimit -n 4096; .venv/bin/python -m pytest tests/ runner/test_tt_runner.py -q`
  -> **768 passed, 17 warnings**. The process-local file limit avoids SQLite open
  errors at the default 256 handles. The existing capture-failure assertion now
  permits observation while continuing to prohibit send-keys/kill.
- Reproducible real tmux measurement:
  `.venv/bin/python -m pytest tests/test_runner_terminal_cleanup.py -q -s -k isolated_dispatch_terminal_tmux_measurement`
  -> **2 passed, 18 deselected**. Idle case: `active_before=1 active_after=0
  tmux_remaining=0`. Live-work control: `active_after=0 tmux_remaining=1`.

Limits: production API/runner behavior after deployment is unverified; no server
or installed runner was deployed/restarted. Real tmux was measured on the local
macOS host, not a Linux host. Test warnings include HTTP client deprecation,
serializer warnings and a uvicorn test teardown thread warning. Unknown process
observations intentionally retain sessions. Queued launch/held/finalizing recovery
and existing explicit release/STALL/TIMEOUT cancellation policies are unchanged.
Cleanup covers ledger-recorded sessions, not unrelated untracked tmux sessions.
