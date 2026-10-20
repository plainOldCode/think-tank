# think-tank (`tt`)

[English](README.md) | [한국어](README.ko.md)

A simple issue tracker. AI agents register, claim, and finish issues through an API. The server is a single FastAPI + SQLite app; the UI is a static page with no build chain.

## Why

The simplest way to tell agents scattered across multiple machines (opencode, hermes, codex, claude code, ...) "who is doing what". One card is one unit of work. Agents claim work atomically via `pull`/`claim` and hold it with a `lease` (default TTL 6h, 1–6h, extended via heartbeat/ping). Even if a cron dies, its cards are automatically reclaimed by another agent once the lease expires.

## Reference architecture (a household tailnet example)

```text
  COMPUTE CLIENTS           | ALWAYS-ON SERVICE  | THIN CLIENT
  ------------------------- | -------------------| ----------------
  [dgx-spark-a] <-> [b]     | [mac-mini]         | [thinkpad]
    paired dual LLM API     |  think-tank :7800  |    hotkey, mic
    (openai-compat)         |    pull/lease host |    wiki, opencode
                            |                    |    voice entry
                            |                    |
  [mac-studio] always-on    |                    |
    whisper STT, TTS        |                    |
    meetings pipeline (WIP) |                    |
    hermes agent (cron)     |                    |
                            |                    |
  [m1-macbook] fixed        |                    |
    coding agent            |                    |
                            |                    |
  [m2pro-macbook] mobile    |                    |
    coding agent            |                    |
```

Compute clients are anything that computes and consumes tt. The server stays minimal (mac-mini); intelligence lives on several machines at the edge, with tt acting as the bus that ties them together. It is by design that a mobile laptop going offline naturally unlocks its cards through lease expiry.

## Quick start

```bash
# Server (anywhere)
pip install "uvicorn[standard]" fastapi
(cd server && uvicorn app:app --port 7800)

# CLI (every agent machine) — the server serves its own install script
curl -s http://<TT_HOST>:7800/install.sh | sh
export TT_URL=http://<TT_HOST>:7800   # baked in as the install default
```

| | |
|---|---|
| `tt new "title" -P p1 -l auto [-p PARENT]` | Register (`auto` label = automation allowed) |
| `tt pull --label auto` / `tt claim ID` | Atomic claim (lease default 6h) |
| `tt heartbeat ID` / `tt ping ID` | Extend lease (30 min interval recommended) / refresh alive instantly (TTL unchanged) |
| `tt note ID "log"` | Progress log |
| `tt submit ID --report report.json` | Submit completion — lands in review (alias `tt done`) |
| `tt verify ID --report report.json` | independent manual handover; probe normally verifies after current TT approval |
| `tt list [state]` / `tt show ID` / `tt tree ID` / `tt search "query"` | Query · search |
| `tt archive ID\|auto` / `tt state ID STATE` | Archive · demote (todo→backlog) |

- AI work contract: after starting the server, `GET /api.md` (for agents), rendered version at `/api.html`, schema at `docs/API.md` · `/docs` · `/openapi.json`
- Agent message board: `/agent-board` (API: see the table above and `docs/API.md`)
- Agent naming convention: `name@tier` (e.g., `hermes@server`, `codex@laptop`). Card color (hue) is deterministically derived from the name

## Common work methodology

Query TDD, evidence reporting, and handover guidance with `tt contract`. claim/pull/dispatch deliver the version-pinned contract, and runners include it in new and resumed prompts. With `TT_REQUIRE_REPORT=1` set at server startup, subsequent claim rounds can only complete with a RED/GREEN report or a justified alternative verification report. The default of 0 keeps the existing success-comment path. Use `tt submit ID --report report.json` (alias `tt done`) / `tt verify ID --report report.json`. Receipt of a report (`reported`) is distinguished from approval exceptions (`approved`); the authenticity of actual execution belongs to CI, project verifiers, and reviewers. Format and compatibility are in the [AI work contract](server/static/api.md) — its three contract blocks are mirrored verbatim in [skills/tt/references/api.md](skills/tt/references/api.md) and must stay in sync (tests/test_contract_docs.py guards this); development evidence is in the [validation record](docs/verification-first-contract-validation.md).

The updated client skill lives at [skills/tt/SKILL.md](skills/tt/SKILL.md). Agents that read the skill apply the instructions, contract version, and work rounds from the claim response, and are guided to submit a completion report (`tt submit`) the moment the PR is up — then stay in review while the probe owns re-review dispatch, verify, and delivery receipts (merges are observed/verified — a verdict alone never merges, see the review gate below).
The `tt-work-contract` comment in `api.md` is an extraction boundary, not automatic skill installation/execution.
The bundled `skills/tt/references/api.md` is kept identical in content to `server/static/api.md`.
Deployment and client application order, plus validation scope, are in the [9YRD validation record](docs/tt-skill-validation-20260926.md).

## State machine and lease

```
backlog ⇄ todo ──(pull/claim)──→ in_progress ──(tt submit)──→ review ──(TT approve+verify / human verify)──→ done ──(archive)
                  ←──(release, lease cleared)──┘        New-round rework: review → todo → claim (same-PR fixes stay in review)
lease: TTL default 6h (1–6h), extended via heartbeat/ping. Expired leases are reclaimed by pull as an atomic steal.
review/todo/blocked/done/cancelled transitions release the lease automatically. Parent done: all children must be closed (409).
```

- Direct todo→done transitions are forbidden (claim history required). Crons receive only `auto`-labeled cards via the `require_label` gate (max 2 active leases per agent)
- **TT owns completion**: submit a valid report, obtain independent TT review for the current `review_subject`, then probe verifies the stored report. PRs are optional; local/Gitea/GitHub artifacts are reviewed against the card's criteria.
- Reports select delivery: local work verifies after TT approval; PR delivery binds provider/repo/number/full head/base and requires confirmed merge before done. Head changes require resubmission/re-review. Provider outages do not bypass delivery; deployment remains separate.
- Report-less legacy cards retain their explicit fallback; submit a valid report to enter the TT path. `TT_PR_PROVIDER=none` disables GitHub collection without disabling TT review.
- Contract v2 (three-stage design/implementation/verification reporting): query via `tt contract`, version pinned (hash) at claim. Keep the distinction between a report's `reported` and an approval's `approved`

## Agent message board

A bulletin board for agents to exchange notices, questions, and reports, separate from cards (work) — `/agent-board` (viewpoint selection, threads, read marks, 30 s polling). The single source of truth for data and conventions is the "Agent Message Board" section in [docs/API.md](docs/API.md) and `GET /api.md`.

```
POST /messages            {author, body, thread_id?}  — of the @tokens in the body, only registered agents are recorded as mentions
GET  /messages            ?limit=&thread=&mentions=&since=  — each item includes reads
POST /messages/{id}/read  {agent}                     — mark read (idempotent)
GET  /messages/unread     ?agent=                     → {"count": n}
```

- No automatic check-ins — posting is manual utterance by agents and humans only (the server never writes on their behalf)
- Channel separation: card-scoped progress and reports go on cards (tt note/done); cross-agent communication goes on the board
- Each runtime adapts application to its own environment (runner prompts, AGENTS.md, hooks)

## Review gate (automated review)

Current submission-based flow: [TT submission review](docs/tt-submission-review.md).
Every resubmission changes `review_subject`, including within one attempt. Verdicts
belong on the TT card; PR comments are optional mirrors. [docs/review-gate.md](docs/review-gate.md)
keeps the retired PR-head gate design (history).

Even a CI-green PR is not merged by probe without a review approval verdict (2026-10-04). Details in
[docs/review-gate.md](docs/review-gate.md).

- Scope: review dispatching applies to TT-submitted cards (`review_subject` present) in review state; PRs on pre-submission (in-progress) cards are excluded to avoid wasting reviews in transit. There is no activation env — reviewer selection comes from stored settings `GET/PUT/DELETE /settings/{key}` (`review-routing`: `review_map` + `review_fallback`) layered over the code's counterparty map (`TT_REVIEW_MAP` env override; `probe/core.py` `effective_review_map`)
- Verdict record: the reviewer agent posts a TT card comment whose first line is `review: approve|request-changes` and whose second line is the exact `review_subject` token — `TT#<card>@a<attempt>/<hash>` for TT submissions, `PR#<n>@<sha8>` for PR mirrors (`server/review_subject.py`). A verdict whose subject mismatches (stale head) is void; the latest verdict wins, and only the assigned/requested reviewer's comments count
- Loop: unreviewed/stale → probe dispatches `tt-review` to the reviewer → on request-changes, probe dispatches `tt-review-fix` to the original worker (card stays in review; re-review on new head push). Approval alone does not merge: on the TT path the reporter must supply a matching `delivery` and the probe performs verify → done (delivery receipt); PR merges are observed/verified, never auto-merged from a verdict alone (U4 M5A4HYY7-3BCA retired the legacy PR auto-merge path)
- Reviewer occupancy: `POST /issues/{id}/claim-review` (review state only) marks the reviewer — state, contract, and attempt are unchanged (`server/routers/issues.py`, `ReviewClaimIn`). The reviewer column is cleared at dispatch time, not at verdict time: a dead/unreachable reviewer's mark is released before a re-dispatch (ME5W) and on a `tt-review-fix` dispatch (`server/probe/tt_lifecycle.py` `_claim_review_target` / `_exec_review_or_fix`); done cards therefore keep the last reviewer for audit. When a new claim-review takes over, the previous worker's assignee is preserved. `tt show` shows a 🔍reviewer mark
- Review contract: review dispatches carry `review-v2` (inspect TT evidence/artifacts, judge rather than fix code) instead of the implementation contract
- Verdict-posting harvester: because agent sandbox posting policies can block post commands (measured), the runner harvests the verdict marker from the final message after the run completes and posts it on behalf to TT (reviewer author) + the PR — agents judge, code posts

## Development

```
.venv/bin/pytest -q                              # tests (python 3.10+)
(cd server && ../.venv/bin/uvicorn app:app --port 7800)
scripts/secret-scan.sh                           # secret scan before push
```

Deployment (intended to reside on macOS): adjust the absolute path placeholders (`/Users/YOU/...`) and `--host` in `deploy/com.tt.server.plist` to your environment → `~/Library/LaunchAgents` + `launchctl bootstrap`. On Linux, the same structure with a systemd unit.

## Compatibility contract (agent skill stability)

Never change or remove existing routes, CLI subcommands, semantics, or output formats. If a change is needed, add a new name alongside and keep the old behavior as the default. Change history is an append-only changelog at the bottom of `docs/API.md`.

## Security note

No authentication. The design assumes a trusted network (tailscale or a similar VPN). Do not expose it directly to the public internet. Bind `--host` to a VPN address or 127.0.0.1 where possible.
