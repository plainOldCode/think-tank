# TT submission review (Q614)

TT owns the work contract, report and review verdict. GitHub, Gitea and local
artifact locations are optional evidence sources. A missing provider or PR is
not a reason to reject a valid TT submission.

## Flow and identity

`claim → implementation → tt submit → review → independent approve → probe verify → done`

`submit` is a command, not a stored state. Every accepted report increments
`submission_revision`, including identical re-submissions within one attempt.
GET/list responses expose `review_subject = TT#<ID>@a<attempt>/<sha256>`.
The hash covers ID, attempt, revision, title, body, acceptance, assignee, pinned
contract and report. Lease renewals, comments, reviewer claims and priority
edits do not change it. Scope/evidence changes and re-submissions do.

The reviewer must inspect the actual artifact references and verification
evidence, not merely the report's `passed` claim. Artifact changes require an
updated report with an immutable commit/digest reference and a resubmission.
TT cannot detect an unreported mutation of an external file.

Post to the **TT card**, author=the assigned reviewer:

```text
review: approve
TT#<ID>@a<attempt>/<copy the complete hash from review_subject>
Findings, commands, evidence and limits of independent verification.
```

Use `review: request-changes` for rejection. PR comments are optional mirrors.
The latest verdict for the exact subject wins; PR-only, unrelated, stale and
self-authored approvals cannot complete this path. A review claim or probe's
subject-specific request binds the reviewer identity. This is the existing
trusted-network identity convention, not authentication.

## Probe and atomic verification

`probe/tt_lifecycle.py` plans `tt-review`, `tt-review-fix` and `tt-verify` from
the stored valid passed report, independently of PR discovery. Registered
reviewers use the existing counterpart/fallback routing. Active dispatches and
leases prevent duplicate work. A review dispatch is retried once after an hour
without a verdict and without an active lease; exhaustion produces a TT
`tt-review-stalled` handover notice. Hook errors are not success markers.

The reviewer receives `review-v2`; implementation work receives its pinned
work contract. Review fixes stay in review and submit again. Reviewer absence
does not silently approve the card. The implementer or an operator can arrange
a separate reviewer through the existing claim-review interface.

Probe re-reads before execution and passes `review_subject`, `expected_version`
and the exact stored report to `/verify`. The server rechecks the subject,
latest independent TT approval and report equality inside the write
transaction. Existing report/attempt/children/self-verify guards still apply.
A changed verdict or submission between planning and execution cannot close
the card. Manual/human verify remains the existing explicit handover path.

## Hosting integration and migration

`probe/providers.py` isolates optional PR collection. `TT_PR_PROVIDER=github`
(default) keeps the legacy collector. `none`/`local` disables GitHub collection;
unsupported providers log that observation is unavailable and leave TT review
working. A Gitea API adapter is not implemented here. Explicit GitHub PR URLs
missing from the open list are queried separately and classified as merged,
closed or unknown; query errors never become proof of merge. Report-less cards
receive an accurate observation and a request for a valid TT submission.

Valid stored reports, including pre-migration reports with revision 0, enter
the TT path. Existing PR-only verdicts require a fresh TT-subject review; old
reports and pinned contracts are not rewritten. Report-less/legacy cards keep
the existing explicit fallback behavior; submit a valid report to adopt this
path. Known merged/closed PRs are supplemental observations, not verdicts.

TT completion does **not** claim that a PR merged, CI passed or a deployment
occurred. If any is required by acceptance, the report must contain that
evidence and the reviewer must check it before approving. TT approval does not
grant merge or deployment permission. Automatic PR merge remains a legacy
adapter behavior, not a prerequisite or side effect of TT-submission verify.

The three methodology blocks and their bundled copy are updated together.
New claims receive a new contract hash; in-flight claims retain their pinned
version/report schema. Runtime delivery of review-v2 is tested separately from
the wording parity check; live agent behavior still requires deployment and
an independent operational review.
