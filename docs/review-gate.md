# Review gate spec — POC (N0AN)

v0.1 · 2026-10-04 · Parent: M425EVXW-N0AN · Implementation: probe (M428RMBY-XC0K) + reviewer runner (M428RMG7-2HZF)

## Purpose

Add a **reviewer-approval condition** to the path that used to merge on CI green alone. The gate is enforced via TT comments (POC — no GitHub branch protection).

## Roles

| role | who | notes |
|---|---|---|
| Authoring agent | the card's claim agent | branch push, completion report |
| Reviewer | hermes kanban triage (MoA: gpt-6.1-sol + astra + opus-5.5) | reviews the PR diff + repo context |
| Follow-up fix agent | **agent-agnostic** | the context is the repo+PR itself, so anyone obeying the standard TT dispatch interface works |

## Review comment format (contract)

The reviewer leaves **the same content in two places**: a GitHub PR comment + a TT card comment.

TT comment (author = the review agent's name):

```
review: approve | review: request-changes
PR#<number>@<head sha, 8 chars>
<findings and reasons>
```

- Line 1: the verdict marker. Exactly `review: approve` or `review: request-changes`
- Line 2: names the commit the verdict applies to. probe compares it with the PR's current head — on **mismatch the verdict is void (stale)**
- Line 3 and below: free text (checklist results)
- On the GitHub side, only `gh pr comment` (comments only). Since self-approval is impossible on GitHub, the approval API (`gh pr review --approve`) is unused in the POC — the sole basis for an approval verdict is the **TT comment**

## probe gate (decide ⓪ change)

For merge candidates (card link + work_contract + attempt≥1 + non-draft + repo match + CI green):

1. **Unreviewed** (no valid review comment on the current head):
   instead of merging, a `review-request` action — dispatch to the reviewer agent.
   Deduplicated per PR+commit with the marker `[review-req #<pr>/<sha8>]` (once per round).
2. **request-changes** (valid on the current head):
   hold the merge + return the card to review + dispatch a fix instruction to the assigned agent.
   Marker `[review-fix #<pr>/<sha8>]`.
3. **approve** (valid on the current head):
   the existing merge path as-is — including verify-in-merge after the merge (M3R7M0ZR-YF99).
4. A new commit push → head changes → the existing review is stale → back to 1 (re-review).

- Review comment validity: among TT comments, only those whose author matches the reviewer AND whose `PR#<n>@<sha8>` n and sha8 match the current PR count.
- Reviewer: a single agent name in env `TT_REVIEW_AGENT`. **Unset = gate off**
  (existing behavior — the same default-off safety pattern as TT_PROBE_INTERVAL). mini activation starts
  when the launchd plist env is added (after the reviewer runner 2HZF is ready).
  Since M4JTJ970-WS3C the env is the **legacy fallback only** — see the routing ruleset below.
- Cards excluded from merge by the review gate are **excluded** from the ⓪b needs-merge comment targets (noise prevention).
- CI red is handled by the existing ci-fix first — review only comes after green.

## Reviewer routing ruleset — counterpart per implementing agent (M4JTJ970-WS3C)

The reviewer is the **implementing agent's counterpart** — nobody reviews their own family's work.
The implementing agent is the card's `assignee` — the same source the server's own-work rule
(claim-review 409) uses. Unmarked/unmatched defaults to **codex**; if the implementing family is
`codex*` (any prefix variant), the fallback reviewer is **claude** instead — self-review
prevention takes priority over the codex default (issuer decision 2026-10-10).

| implementing agent | reviewer |
|---|---|
| codex (incl. `codex@host`, `codex-*` prefix variants) | claude |
| hermes (incl. `hermes@host`) | codex |
| opencode (incl. `opencode-tp13`) | codex |
| claude | codex |
| unmarked / unmatched (incl. qw-flash, agy, tt-reviewer) | **codex (default)** — but a `codex*` implementing family routes to claude |
| CI bots (`doc-check`, `e2e`) | not implementing agents — treated as unmarked |

- **Family normalization**: agent IDs drift by host/variant suffix (`codex` vs `codex@host` are
  mixed in verdict comments). Matching is by family prefix: strip `@host`, then longest known
  family prefix wins. If the base matches no family and the ID is `user@host`, the **host alias**
  map is consulted (`tp-13` → opencode — the board's second-most-common assignee form
  `skshim@tp-13` is an opencode-family implementer, F2); extendable via
  `TT_HOST_FAMILY_MAP=host->family`. `TT_REVIEW_MAP=codex->claude,claude->codex,...` overrides/extends the
  default table (parsed as `family->reviewer`, comma separated). The self-review guard uses a
  **fixed** family vocabulary plus the adapter alias (`kanban-adapter` ≡ hermes, its standing
  delegate) and the author's exact identity — mapping extensions cannot route around it.
- **Unavailable reviewer fallback** (issuer decision 2026-10-10): if the routed reviewer is
  unavailable — offline (not a registered+enabled agent) or holding an active lease (judged
  **per resolved agent name, not per family** — one busy family member must not block an idle
  sibling, F3) — or refusing the claim (`claim-review` 409), walk **claude → kanban-adapter
  (hermes's board adapter) → codex**. codex is last because its availability is the flakiest.
  The author's identity and implementing family stay excluded through the whole chain (self-review
  prevention holds). If nobody in the chain is claimable, the probe does **not** widen to a general
  idle scan (F1 — an unassigned agent would end up reviewing); the legacy fallback applies:
  `TT_REVIEW_AGENT` env, or the `kanban-adapter` default — and a legacy reviewer that is the
  author (or their family) is not dispatched either.
- **Routing scope — both dispatch paths**: the auto-claim (`review-claim`) targets **only** the
  `pick_reviewer` result — the routed counterpart walked down the fallback chain (F1). The
  `review-request` dispatch carries the routed reviewer (absent when the whole chain is
  unavailable — the executor then resolves the legacy reviewer). Verdict format, the
  `PR#<n>@<sha8>` staleness rule, and the dispatch template are unchanged. The
  server's own-work 409 (exact `assignee` match) is unchanged — probe-side selection excludes
  the author's identity and family so it never nominates one.
- **Claim handoff** (refusal recovery): the probe claims the review on the target's behalf
  before dispatching and only then records the `[review-req …]` marker. A 409 refusal walks to
  the next candidate; if every candidate refuses (or the card's review is actively held by
  another reviewer's lease), no request marker is left — the next cycle re-judges and retries
  instead of being blocked permanently. Refusals are noted once per PR+head
  (`[review-claim-fail …]`); candidates eliminated entirely by the self-review guard (no request
  sent at all) are noted separately (`[review-guard-skip …]`) instead of being logged as 409 (F4).
  If the claim succeeded but the dispatch itself fails, the probe **releases the reviewer lease**
  before noting — a stranded lease would 409 every other reviewer until expiry (F4, observed
  live on M4JTJ970-WS3C PR#80).

## POC constraints (intentional simplifications)

- No separate GitHub account: review comments are also left with the plainOldCode token.
- probe is the sole enforcing subject — if probe is disabled/buggy there is no gate (the path where a human
  merges directly is always open). A hard gate (branch protection + a review account) is a follow-up
  after POC validation.

## Reviewer dispatch instruction template

(Server-emitted literal; kept in Korean. The live template now lives in `server/probe/core.py`.)

```
[auto review] PR #<n> (<repo>) @ <sha8> — 카드 <card-id> 리뷰 요청.
리뷰 방식: working copy에서 git diff 기준 변경 파일 통독 + 변경 심볼 grep으로
호출자 확인. 공용 모듈(service/probe/routers) 변경 시 호출자 추적 필수.
판정 기준: 계약 v2.1 준수, 시크릿 노출, 테스트 적절성, 놓친 엣지.
결과 제출: GitHub PR 코멘트 + TT 카드 코멘트(포맷: docs/review-gate.md).
첫 줄 'review: approve' 또는 'review: request-changes', 둘째 줄 'PR#<n>@<sha8>'.
```

- Reviewer working copy: a shallow clone on the review host (hostname redacted per secret-scan — see the Korean original), kept updated with `git fetch`. Reviewer LLMs are
  /opt/homebrew/bin/{codex,claude} — cross-review for core changes, single for minor ones.

## Reviewer verdict criteria (checklist)

1. Contract v2.1 compliance — the three report blocks (design/implementation/verification)
2. Exposure of secrets, keys, tokens
3. Presence and adequacy of tests (the first line of defense against side effects is the CI's 200 tests — the reviewer covers what lies beyond)
4. Caller impact of changed symbols (agent-autonomous exploration)
5. Behavior matches the spec and the card body

## State flow summary

```
PR push → CI green → probe: no review → [review-req] dispatch → reviewer reviews
  ├─ approve (TT+PR comments) → probe: merge → verify → done
  └─ request-changes → probe: card back to review + [review-fix] dispatch
       → fix agent pushes → head changes → re-review [review-req] …
```

## Reviewer engine measurements (2026-10-04)

| engine | time | notes |
| --- | --- | --- |
| codex (tt-runner) | ~12 min | pointed out a real defect (template re-review fetch non-fast-forward) |
| claude (tt-runner) | ~4 min | deep verdict — identified duplicates, CONFLICTING, regressions |
| hermes MoA | ~80 min | built for work, too slow as a reviewer |

Gate operation: `TT_REVIEW_AGENT=codex` (legacy single-value mode — superseded by the
counterpart routing ruleset above; the env value now serves as the last-resort fallback).
claude is the cross-review engine; hermes is excluded from review dispatch targets.
