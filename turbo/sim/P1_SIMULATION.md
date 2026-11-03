# P1 offline simulation

Verification level: planned-only. This is a simulation. It does not re-execute the work, and it does not call the live board, a harness CLI, GitHub, or Telegram.

The simulator is `scripts/p1_replay.py`. It is outside the `turbo` package, so `scripts/scenario-coverage.sh` does not measure it. The gates it calls are `turbo.service` and `turbo.proof`.

Input: `turbo/sim/p1_baseline_150.json`, 150 done cards, metadata only.

## Headline

| Variant | Accepted | Rejected | Ask human | Exempt applied | Awaiting-human pings | Stuck pings |
|---|---:|---:|---:|---:|---:|---:|
| A-strict | 0 | 150 | 0 | 0 | 0 | 150 |
| B-figma-ui | 0 | 150 | 0 | 0 | 0 | 150 |
| S-no-red | 5 | 145 | 0 | 150 | 0 | 0 |
| L-level | 149 | 1 | 0 | 150 | 0 | 0 |

## Top rejection reasons

### A-strict

- 150 `red-green`: scope.scenario is required unless scope.exempt is true

Main rework (`claim_count` 2): 24. Turbo `rc_count` summed after the real verify gate: 0. Notification total if both triggers are on: 150.

### B-figma-ui

- 150 `red-green`: scope.scenario is required unless scope.exempt is true

Main rework (`claim_count` 2): 24. Turbo `rc_count` summed after the real verify gate: 0. Notification total if both triggers are on: 150.

### S-no-red

- 145 `verdict`: approve requires a passed server verify

Main rework (`claim_count` 2): 24. Turbo `rc_count` summed after the real verify gate: 145. Notification total if both triggers are on: 0.

### L-level

- 1 `merge`: merge requires an approve verdict

Main rework (`claim_count` 2): 24. Turbo `rc_count` summed after the real verify gate: 0. Notification total if both triggers are on: 0.

## What each variant changes

- `A-strict`: no card is exempt. Verify kind comes from `method`. A planned card stays kind `planned` even when main recorded `live-verified`.
- `B-figma-ui`: the same as A, except cards in `--figma-ids` are `scope.exempt`. This baseline has no title, label, or path, and the id list shipped with the sim is empty, so zero cards match.
- `S-no-red`: every card with `has_red_command` false is exempt. This is a sensitivity bound, not an identification of figma or UI cards. Kind still comes from `method`, so the planned-plus-passed contradiction is the next gate.
- `L-level`: the same exemption, but verify kind comes from main's `verification_level`. Cards main called live-verified or unit-test-verified can pass verify. The evidence-only epic then reaches `merge` without an approve.

## Assumptions

- No scenario file is written, because `has_red_command` is false on every card. `prove` rejects a non-exempt card before verify runs.
- `result=passed` makes the in-process verify stub return exit 0. The stub does not execute a command. `level_of` inside `submit` decides the level from the kind, not from main's recorded level.
- Changed path is `replay/card.txt` under scope `replay/`. The baseline has no paths.
- Title and body stored in the temp database are the legacy id and the word `replay`. They are not the private card text, and they are not written to the output.
- Harness is `opencode` because the assignee name is an opencode worker. The replay does not spawn it. OpenCode stays handoff-only.
- A missing assignee is still claimed by the replay runner, so a later gate can run. Main's epic was not claimed.
- `delivery_mode=pr` becomes a pr object with `number` null. No PR number from main is stored.
- Independent review maps the reviewer name to a probe actor and calls `verdict(approve)` then `merge`. Evidence-only does not call approve.
- `claim_count` is reported as main's rework count. It is not turned into a question. `would-ask-human` is only the real `awaiting-human` state. Nothing in the metadata is a question, so that count stays 0.
- Awaiting-human pings are the planned Hermes trigger. Stuck pings are the optional trigger: `metrics().stuck` with an empty live set, one per card left `working`. A failed `submit` rolls back, so the card stays `working` and counts as stuck. No message is sent.

## Limitations

The baseline cannot say which cards are figma or UI sync. Variant B therefore cannot show that sensitivity. Variant S shows the upper bound of exempting every card that has no red command.

Parent links are not a turbo field. The epic is replayed as its own card.

This does not compare diffs, tokens, or lead time. Those fields are not in the baseline.
