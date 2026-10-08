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
- Cards excluded from merge by the review gate are **excluded** from the ⓪b needs-merge comment targets (noise prevention).
- CI red is handled by the existing ci-fix first — review only comes after green.

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

Gate operation: `TT_REVIEW_AGENT=codex`. claude is the cross-review engine; hermes is excluded from review dispatch targets.
