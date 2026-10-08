# probe PR-linking contract (measured against server/probe/core.py, 2026-10-06)

How probe links PRs to cards and merges them — based on the server worktree ~/tt-review/think-tank.

## PR collection pool (scanned repos)
- The server's permanent known repos (REPO_CARDS) are **only** plainOldCode/think-tank and plainOldCode/armour-service-ops
- A `repo: owner/name` marker in the card body (or title) adds to the pool — **only auto/todo/blocked/review cards contribute**; done/cancelled/in_progress are excluded
- Only the first matching repo per card (regex search) → one card cannot span multiple repos. Split cards per repo
- PR links or tt# IDs in comments are used nowhere in pooling/matching (only the card body + title)

## PR→card join
- The card ID in the branch name takes priority; otherwise the card ID in the **PR title** (format `M[A-Z0-9]{6,9}-[A-Z0-9]{4}`)
- tt# IDs in the PR body are not accepted for matching → **(TT <cardID>) in the PR title is required**

## Merge preconditions (all required)
- Card: work_contract exists + execution_attempt ≥ 1 + state not done/cancelled
- PR: non-draft + repo match (required when the card names a repo) + all checks SUCCESS
- Review gate: when TT_REVIEW_AGENT is set, a valid reviewer verdict on the current head is required
  - Verdict comment contract (TT card, author=reviewer): line 1 `review: approve|request-changes`, line 2 `PR#<n>@<sha8>` — mismatches or absence are treated as stale and re-requested (docs/review-gate.md)
  - Unreviewed→review-request, request-changes→review-fix (return + delegate fixing), approve→merge
- After merging, the server daemon automates verify→done (ZK3G)
- If a review card stays unmergeable, a `[needs-merge a<attempt>]` human-judgment request is posted once (deduplicated per round)
- When dispatching the reviewer, the TT comment contract format (line 1 `review: …`, line 2 `PR#<n>@<sha8>`) is stated in the prompt — a verdict without the format is voided by probe and re-requested

## probe verdict comments = a signal to read the source

When probe leaves a verdict such as `[needs-merge]` or `[review-fix]`, read the verdict path in the server worktree's
`server/probe/core.py` before retrying the same pattern. Verdicts repeat every round and do not go away —
re-creating the card without checking the source repeats the same misjudgment.

## Re-review re-dispatch (review STALL recovery, measured 2026-10-06)
probe's review-req marker is deduplicated once per sha, so it cannot re-request. If a review dispatch is STALL-killed:
1. Read the failed dispatch's payload from ~/.local/state/tt-runner/runs.json (the message contains the sha).
2. Re-send the same payload with a new dispatch_id — **omit context_in/session** (a continue that send-keys into a dead
   session is the cause of silent stalls):
   curl -X POST http://<RUNNER_HOOK_HOST>:7796/hook -H "Authorization: Bearer $(cat ~/.hermes/tt-runner.secret)" \
        -H "X-Tt-Dispatch: 1" -H "Content-Type: application/json" --data-binary @payload.json
3. The runner creates a new tt-codex-<new-id> session, and on completion the harvest posts the verdict to TT.

## Branch-rewrite (rebase/force-push) rules

- When rebasing a PR branch after a preceding PR merges, use a dedicated worktree (`git worktree add --detach`) and
  **stamp a recovery-point branch first** — do not touch other agents'/users' checkouts.
- Tail-append journals such as WORKLOG and changelogs conflict on every rebase — **preserve both sides (union) and only
  normalize the order.** Deleting one side erases another branch's records and breaks the next rebase too.
- The expected SHA for `--force-with-lease=<ref>:<sha>` is **taken via `git rev-parse` from the remote-tracking ref** —
  writing it directly or abbreviated gets a `stale info` rejection (harmless but failing), and a wrong value can overwrite
  the latest push. Omitting the value (`--force-with-lease=<ref>`) delegates to the remote-tracking ref as of the last fetch.
- After pushing: verify remote ref == rebased head, re-check the PR `mergeable`, and confirm checks re-run. Even if the PR
  branch lacks a CI workflow, once the base (main) has one the merge ref inherits it and checks appear —
  no need to cherry-pick the workflow file.
- When a rebase changes the head sha, existing review verdicts (`PR#n@sha8`) go stale and probe re-requests with a new
  marker — that is why a rebase does not break the gate flow.

## Measured incident (2026-10-06)
Attaching 3 PRs (different repos) to one card prevented pool entry entirely, producing a "no PR" verdict. Solved by splitting into 1 card = 1 PR with a `repo:` marker on each body's first line and (TT ID) in each PR title.
