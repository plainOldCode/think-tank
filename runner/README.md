# tt-runner — TT dispatch → tmux agent runner (prototype)

A single-file (stdlib only) listener that receives TT dispatch webhooks, runs the registry profile's agent CLI in a tmux session,
watches for the exit marker → replies as a TT comment.
It coexists with tt-dispatch-adapter.py (7795) and uses port 7796 (on the primary runner host) / 7797 (proposed for mini).
Spec: RUNTIME-SPEC.md (kanban t_27ca1582), follow-up proof: t_0d90b8eb.

> Hostname note: the example host name from the Korean original (and agent names derived from it) is redacted below as `<host>` to satisfy the secret-scan gate. See git history for the original values.

## Installation (based on the primary runner host; for mini substitute only the IP/port) — the procedure to run in follow-up t_0d90b8eb

1. person-terminal: generate a secret (do not paste the value anywhere)
   `python3 -c "import secrets;print(secrets.token_hex(16))" > ~/.hermes/tt-runner.secret && chmod 600 ~/.hermes/tt-runner.secret`
2. Registry `~/.config/tt-runner/agents.json` (example below; register only CLIs verified by a real install)
3. launchd `~/Library/LaunchAgents/com.tt.runner.plist`:
   ProgramArguments=[/usr/bin/python3, <repo>/runner/tt-runner.py, serve],
   EnvironmentVariables: TT_URL=<TT server url>,
   TT_RUNNER_BIND=<this machine's tailnet IP>, TT_RUNNER_PORT=7796, TT_RUNNER_NAME=runner-<host>,
   TT_AGENT=runner@<host>, RunAtLoad+KeepAlive
   `launchctl load ~/Library/LaunchAgents/com.tt.runner.plist`
4. TT registration: not possible via `tt` — POST to the server `POST /agents {"name":"runner-<host>","base_url":"http://<this machine's tailnet IP>:7796/hook","secret":<file value>}`
5. Verify: `curl http://<tailnet ip>:7796/health`, bad-secret POST→401, missing header→400,
   then one real dispatch E2E (owned by t_0d90b8eb)

Non-tailnet binding or a missing secret file fails startup itself (rc=3, fail-closed).

## Registry schema

```json
{
  "machine": "<host>",
  "agents": {
    "runner-<host>": {
      "driver": "codex",
      "binary": "/Applications/ChatGPT.app/Contents/Resources/codex",
      "permission_mode": "read",            // read | auto | all (all only on tailscale-verified machines)
      "model": null, "reasoning": null,     // codex: -m / model_reasoning_effort
      "workspace": "~/Work",
      "allowed_workspaces": ["~/Work"],     // workspace override allowlist
      "timeout_s": 1800, "keep_shell": true,
      "continue_cmd": "/Applications/ChatGPT.app/Contents/Resources/codex exec --sandbox read-only --skip-git-repo-check"
    }
  }
}
```

Registration rules (from real-world lessons):
- The registry key must equal the agent name in TT `/agents` — the dispatch payload's
  `agent` field is used for the key lookup as-is. On mismatch: REJECT (profile-unknown) + a TT rejection comment.
- `binary` must be the app-bundled codex absolute path. Homebrew codex-cli 0.156.0 fails every file/command
  tool of shell/codex exec with `timed out negotiating with the code-mode host`
  (measured in t_0d90b8eb — clean CODEX_HOME, mcp disable, and features.code_mode=false all failed;
  the ChatGPT.app bundle 0.155.0-alpha.16 works). Check the version when ChatGPT.app updates.
- `--skip-git-repo-check` in continue_cmd is required — the workspace (~/Work) is not a git repo,
  so without it every continuation instruction fails immediately (measured).
- The launchd plist PATH must include /opt/homebrew/bin — the default env lacks it and tmux/tailscale lookups fail.

A dispatch override recognizes only a single leading `#opts {"model":...,"reasoning":...,"workspace":...,"continue_session":true}`
JSON line in the message (keys outside the allowlist: OVERRIDE-IGNORED). binary/permission promotion is impossible.

## CLI

- `tt-runner.py serve` — launchd resident (watchdog and polling threads built in)
- `tt-runner.py status [dispatch_id]` / `attach <dispatch_id>` / `stop <dispatch_id>` / `poll`
- Run ledger: `~/.local/state/tt-runner/runs.json` (key `<issue_id>#<dispatch_id>`)

## Validation status (2026-09-25, as of this commit)

- unittest 23 green (runner/test_tt_runner.py — M3BZS1G3 extension 8: BLOCKED/crash separation,
  stall threshold/reset/kill, guard root/sanitize, agent_env hits and opt-in)
- Loopback fake-TT smoke 20 items green (runner/smoke_tt_runner.py, TT_TMUX_SOCKET isolation):
  instant 200 + context session token, new tmux run→marker→done comment, DUP-SKIP (duplicate dispatch_id),
  401/400, destructive gate hold→an 'approval' emitting run, context continuation instruction (send-keys mode),
  INBOX-NOT-READY (pending 404), secret log masking, +M3BZS1G3: a fake CLI 'approval
  required' exit=1 → BLOCKED(waiting_for=human) / crash exit=1 → failed distinction,
  a registered-but-missing workspace → pre-start workspace-guard rejection, a secret canary
  not exposed and benign vars kept in an env-print CLI, a silent CLI → STALL comment → stall-killed (injected 6 s/4 s thresholds)
- codex real-run smoke PASS (runner/smoke_tt_runner_codex.py — ChatGPT.app bundle,
  stdin prompt→note.txt read→done, codex-canary env non-exposure confirmed)
- Not yet proven: a dispatch round-trip against the real mini TT server, reboot survival after launchd install,
  context round-trip (confirmed in app.py that deliver→dispatches.context→next payload works; a real-server E2E is a follow-up),
  the destructive-pattern list verbatim (substituted with a draft list), an opencode session driver (only the plain driver exists), mini/m1 installs (ssh blocked)

## Re-review re-run (think-tank#46, 2026-10-06)

Even for a re-send of the same (issue_id, dispatch_id), if the existing run is **done** and the review target sha in the message's
first line (`PR #n (repo) @ sha8`, compatible with the harvest marker `PR#n@sha8`) changed, it **re-runs as a new round** — a fix for the re-review chain stalling. Round distinction:

- The session name is round-unique (`tt-<agent>-<dispatch>-<sha8>-r<round>`) — avoids collision with a previous round's session (keep_shell
  survival) and does not overlap past rounds even on sha revisits (A→B→C→B) (PR#47 review R2).
- Re-round judgment uses the same conditions as the ledger/prepare return value/instant 200 context — a follow-up dispatch carrying the server-stored
  context continues in the current round's session, not a previous round's (PR#47 review R1, blocking a measured STALL cause).
- **Round preemption happens once, synchronously** — /hook runs `preempt_round` (containing only ledger I/O) before computing the 200 and shares its result (the preempted session) between the 200 context and the background run. If the response decision and claim each judged at separate locks/moments, a completion transition/claim after the response decision would remain (PR#47 review R1, 3rd). Network side effects (TT comments and harvest finalize reports) are deferred to keep the instant-200 contract (10 s).
- **Completion-watcher transition absorption** — when a `.done` marker already exists but the ledger says running (waiting for a watcher tick, a window ≤ WATCH_INTERVAL_S), absorb it as done before the preemption decision. An absorbed termination only reports (output is caught before erasure) and does not rewrite the ledger. rc!=0 requires tail analysis for blocked/failed classification, so it is the watcher's job — only finalize-identical conditions are absorbed.
- **Completion transition and report ownership is one atomic gate** — if a previous round's watcher is waiting on finalize's report network call and the next round preempts, the late return would either overwrite the current round's entry as done/failed (no session created, zero reports) or the watcher+absorption paths would report the same completion twice (PR#47 review R3). Therefore, right before reporting, `claim_report()` re-verifies the observed session/round inside the lock and check-and-sets report ownership (entry `claimed_report`). If the entry has already been replaced by the next round, an rc=0 completion report belongs to the absorption path (watcher skips) and a failure report is the watcher's. The ledger transition is `update_run_if_round` — it writes only when the observed round is the current round and running. Network calls (TT comments, projection) happen outside the lock.
- The 200 context for the same key is based on the **preemption result** (the session confirmed in the ledger) — an approval message does not preempt (only releases held), and only on synchronous preemption failure does it respond with an indecisive estimate (`decide_session`). For queued/running/done and duplicate re-sends, it does not reclaim the previous round's session from the input ctx.
- It deletes the previous round's runtime artifacts (.done/.exit/.out/.log/.msg) — since the key is shared, a leftover .done would make watch_once instantly finalize the new run.
- The ledger entry records `target_sha` (current target) and `prev_sha` (previous target), and the start comment notes "re-review re-run: target a→b".

DUP-SKIP is kept, but the reason now states the target comparison (`same target x` / `mismatch a→b` / `no target sha`). If a re-send with a mismatched target arrives in a non-done state, it is surfaced via a TT comment once per round (a `mismatch_notified` flag). If the server probe issues a new dispatch id per round (proposal B, unimplemented), this compensation path never triggers in normal operation.

Validation: unittest 57 green (previous round's 53 + 4 ownership-gate regressions: preemption during a finalize's internal wait→late return no-overwrite with 1 report and the retry response preserved, a B-completion report, no-overwrite of a return after B ran, absorption-ownership late-finalize skip, the gate-unit contract) + RED comparison (3 new failures at 8e2e43f5: B queued/running overwritten as done, late finalize re-reporting) + an isolated tmux E2E SMOKE PASS (round 1
done → a re-send with a new sha RE-ROUND re-runs to done, round 1 output non-leaked, a same-sha round-3 skip,
an A→B→C→B revisit with the previous round done, a follow-up dispatch continuing the current round).

## Security rules

The secret value is never written to files, plist env, TT comments, git, or logs.
mask() passes over logs/comments; binding allows only a tailnet IP or 127.0.0.1.
permission all runs un-downgraded only on tailscale-verified machines, otherwise it downgrades to auto (PERM-DOWNGRADED log).
On detecting a destructive pattern (rm -rf, git push, sudo, drop, ...), GATE-HOLD — it does not run until an 'approval' dispatch arrives on TT.

## Run watchdog (M3BZS1G3-VNQH) — default draft, finalized after card-comment approval

- Needs-input vs crash: only when exit!=0, the output/pane tail is compared against INPUT_SIGNATURES —
  on a hit it becomes BLOCKED rather than failed (a comment with waiting_for=human, ledger status=blocked,
  blocked_on=the signature). No automatic retries — it waits for an 'approval' or a new instruction dispatch.
  The 10 default signatures: requires approval / approval required|needed / waiting for input…/
  needs input|approval|confirmation / permission required|needed / do you want to allow…/
  please approve|confirm…/ [y/N]·[yes/no] / press enter to continue / needs_input·
  needs_approval·external_permission. exit=0 output is not judged, reducing false positives.
- Stall silence detection: if the pane's last-10-lines fingerprint and the run files' (.log/.out) mtime stay unchanged for
  TT_STALL_SILENCE_S (default 600 s), a STALL comment (with the pane tail attached, distinguished from TIMEOUT,
  stall_notified once); after a further TT_STALL_KILL_AFTER_S (default 600 s) of silence, C-c→kill +
  a stall-killed ledger. Watching only the pane misjudges runs that write to files like codex, so file mtime is
  included in liveness. The counter resets when output resumes (fingerprint changes). The wall-clock TIMEOUT (default 1800 s) remains a separate path.
- Three workspace invariants (Symphony SPEC §9.5): ① dispatch override values are sanitized
  (control chars/NUL/newlines/RTL/OVERLENGTH → OVERRIDE-REJECT, falling back to the profile defaults)
  ② the allowed roots (allowed_workspaces∪workspace, or ~ if none) are realpath-checked — once in execute_once
  and again right before run_agent executes (layered defense); violations are rejected before the run starts
  (status=failed, detail=workspace-guard:<reason>, a TT rejection comment).
  ③ the execution cwd is forced to the guard-passed realpath.
- Credential isolation: run-agent filters its own process env via agent_env() before passing it to the child/pane shell —
  name patterns (SECRET|TOKEN|PASSWORD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY) + the TT_RUNNER_* prefix + any key whose value contains the TT secret are all dropped. Genuinely needed keys opt in only via the registry's
  `env_extra` (a profile field). Limitation: it cannot stop same-user files (the secret file itself).
- Smoke isolation: when TT_TMUX_SOCKET is set, a dedicated `tmux -L` server is used (no clash with real sessions).

## TT work-contract delivery

The dispatch payload's `work_contract` and `execution_attempt` are stored in the ledger and injected into the prompts of new (stdin/argv) and resumed runs. `#opts` is parsed from the original message first. Legacy payloads without a contract run as-is. Contract-delivery tests are not evidence of a real LLM's TDD compliance. Full contract: [api.md](../server/static/api.md).
