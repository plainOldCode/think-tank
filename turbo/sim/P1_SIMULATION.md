# P1 offline simulation

Verification level: planned-only. This is a simulation. It does not re-execute the work, and it does not call the live board, a harness CLI, GitHub, or Telegram.

The simulator is `scripts/p1_replay.py`, outside the covered `turbo` package. It calls the real submit, verdict, and merge gates.

Input: `turbo/sim/p1_baseline_150.json`, 150 done cards, metadata only, including `card_kind` and `rejection_reasons`.

## Before and after

Before is the previous simulation, when every card was treated as a behavior card with no scenario. After is this run. Stuck is `metrics().stuck` with an empty live set. Ask-human is the `awaiting-human` state.

| | Accepted | Rejected | Ask human | Stuck |
|---|---:|---:|---:|---:|
| Before | 0 | 150 | 0 | 150 |
| After | 130 | 20 | 0 | 0 |

Before, the only rejection was 150 red-green: scope.scenario is required unless scope.exempt is true.

## After, by gate

These counts are the gate the real code fired. They are not a second copy of the measured reason labels.

- 12 `evidence`: evidence missing at head: replay/missing.txt
- 3 `mergeability`: not mergeable against base
- 2 `head-sha`: head sha does not match the pull request
- 2 `red-green`: scope.scenario is required unless scope.exempt is true
- 1 `epic`: an epic closes when its children are merged

## Request-changes caught before review

Measured on main: 27 request-changes across 19 cards.

Modeled: a card is caught before review when its measured reasons include evidence missing, a merge conflict, a regression outside scope, or a head sha mismatch, and this replay rejected it before `review`. Pixel and report mismatches are not an automatic gate.

Caught: 25 of 27 request-changes, on 17 of 19 cards. A card with several reasons hits the first gate only. Evidence is checked before mergeability and head sha, so a conflict that also missed evidence is counted under evidence. The three measured regressions were on cards that already failed one of those earlier gates.

The 4 post-merge defects are measured as visual. None of them is a functional or API failure. A figma pixel mismatch still needs a person.

## Assumptions

- `card_kind` `visual` skips the scenario gate. `other` is mapped to `feature`, so it still needs a scenario. The epic is submitted once and rejected; children are not replayed in one database, so automatic epic close is not part of this count.
- For a visual card, CI kind comes from main's `verification_level`, because CI already ran. `method=planned` means there was no red-first test.
- Evidence, mergeability, head sha, and rebase paths are not in the metadata. They are set from `rejection_reasons`: missing evidence is a path not in the commit, a merge conflict sets `mergeable` false, a head mismatch sets a different `head_sha`, and a regression sets a rebase path outside `replay/`. A visual card with none of those reasons is given an evidence path that is in the commit. That mapping is modeled.
- `has_red_command` is false for every card, so no scenario file is written.
- The verify stub returns exit 0 when `result` is `passed`. It does not run a command.
- A rejected submit now returns the card to `open`, so it is not stuck.
- No notification is sent. Awaiting-human pings are 0.

Main rework (`claim_count` 2) is 24. It is not turned into a question.
