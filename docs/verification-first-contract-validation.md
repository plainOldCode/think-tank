# Methodology contract — implementation and validation record

- Work date: 2026-09-26 (Asia/Seoul)
- Branch: `feat/verification-first-contract`, base `938c4f8e95b9df0ae0f64389e363ee1bd53fd906`
- Target: delivery of the common TDD guidance and the completion-report gate defined in the TT M3CE450N-NMFK review.
- Implementation-phase scope: local implementation and validation. During implementation, no production TT issues were claimed/done and no production services were changed. Progress comments and states of the parent/child issues were synchronized afterwards at the user's request.

## Completion criteria and results

| criterion | result |
|---|---|
| The same common contract delivered on claim/pull/dispatch | API regression tests pass. Version hashed from the text and policy, work round stored |
| Guidance included in the actual input of new and resumed runners | Confirmed via unit tests and the output of an isolated tmux process. Original message and `#opts` preserved |
| No success judgment from failures, non-runs, empty markers, or URLs only | Regression tests pass. The compatible text path is a conservative heuristic |
| TDD/alternative-verification report validation in report-required mode | Required fields, result, round, and contract version validated. The report-required policy holds even under `warn/off` |
| Stale evidence invalidated on reopen/scope change/new claim | Regression tests pass. Includes expired-lease steal and a scope change within a single PATCH |
| Existing DB data preserved | Repeated migration of a previous-schema fixture, values preserved, integrity_check=ok |
| Reports distinguished from approval exceptions | `reported`/`approved`/`legacy`/`unverified`. Display confirmed on the Chrome local board |
| CLI JSON and error paths | Local real HTTP+CLI tests: review JSON, a single PATCH, abort on comment failure, a whitespace path to the report file |

## RED → GREEN

Tests were written first, the following failures confirmed, and only then the implementation added.

| stage | before implementation | after implementation |
|---|---|---|
| API contract and completion regressions, 25 | 25 failed (`tt-contract-red.log`) | 25 passed |
| Runner new/resumed contract delivery/legacy, 2 | 1 failed, 1 passed (`tt-contract-runner-red.log`) | 4 delivery-related passed (incl. API) |
| Completion judgment isolated after contract delivery | 23 failed (`tt-completion-red.log`) | included in the 25 API passed above |
| Real CLI and adjacent regressions | 7 failed, 30 passed (`tt-cli-red.log`) | 37 passed |
| Report-required policy and migration | 1 failed, 2 passed (`tt-strict-red.log`) | 3 passed |

The failure causes were unimplemented response fields, a missing prompt contract, inaccurate completion judgment, duplicate PATCHes, and JSON format violations.
The log files were kept on the development machine under `/private/tmp/`. Those files are temporary material; the table above is the in-repo summary.
Test runs used an isolated venv on Python 3.13.15. Before/after logs and artifacts were kept separate from the production DB.

## Final verification

```sh
python -m pytest -q -p no:cacheprovider tests runner/test_tt_runner.py
# 111 passed, 2 warnings, 18.18s (full regression)

python -m pytest -q -p no:cacheprovider tests/test_work_contract.py tests/test_api.py::test_transitions
# 38 passed (including additional validation of report-required warn/off and policy pinning after restart)

SMOKE_TELEGRAM=0 python scripts/smoke_9f0s.py
# PASS: real Uvicorn + CLI + loopback notification listener; no Telegram sends

TT_REPO="$PWD" TT_TMUX_SOCKET=tt-contract-smoke-20260926 python runner/smoke_tt_runner.py
# PASS: 22/22 observations true. New/resumed input delivery, dedup, gates,
# failure/waiting classification, env isolation, stall termination included. Uses an echo/fake CLI instead of a real LLM.

bash -n cli/tt
# PASS
# index.html script extracted then node --check: PASS
# git diff --check: PASS
```

After the full regression, three more policy-pinning and warn/off combination cases were added. The test composition at the time totaled 114,
and the changed test modules were validated by the separate 38-test run above. The two warnings were
httpx/BlockingPortal deprecation notices from the test libraries; no functional failures occurred.

The `result report` and `approval exception` displays of the local fixture board were confirmed in Chrome.
The local server and the isolated tmux were shut down after validation.

## Enforceable scope and remaining limits

- The default is common-guidance delivery + compatibility with the existing success-comment path. `TT_REQUIRE_REPORT=1` makes structured reports mandatory for new claim rounds.
- What can be enforced is the submission format, the reported result, the contract version and round, and state transitions. Whether the agent actually wrote tests first, whether the output is real, and whether code quality improved are not proven by this feature.
- `force_done`/`close` are compatibility approval exceptions with no actor authentication. They must not be interpreted as identity-verified human approval. Trusted CI verifiers, authentication, and separation of exception authority are follow-up scope.
- External webhook listeners must put the added contract fields into the execution input. Only this repo's runner guarantees that injection. The installed CLI/runner/skill on each machine is not auto-updated.
- Comparing real-LLM methodology compliance rates, per-project test gates, and measuring improvements in tokens/human review time were not performed. This validation covers API/CLI/runner behavior and guidance delivery.
- The implementation and this validation record are stored together in the commits of the `feat/verification-first-contract` branch. Remote push and production deployment are separate steps. The changes and execution guidance are in the [work contract](../server/static/api.md) and the [API change history](API.md).

## Production DB replica validation follow-up

On 2026-09-26, migration and real-HTTP validation of both modes were performed on a production SQLite snapshot.
The policy-retroactivity problem for existing work was reproduced in a regression test and fixed; the final full regression passed 116 tests.
Environment, preservation hashes, isolation, restarts, and the local addresses used are in the [pre-deployment validation record](predeployment-sqlite-validation-20260926.md).
