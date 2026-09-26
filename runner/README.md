# tt-runner — TT dispatch → tmux agent 러너 (프로토타입)

TT dispatch webhook을 받아 레지스트리 프로필의 agent CLI를 tmux 세션에서 실행하고,
종료 마커 감시 → TT 코멘트 회신까지 하는 단일 파일(stdlib only) 수신기.
tt-dispatch-adapter.py(7795)와 공존하며 포트 7796(m2max)/7797(mini 제안)을 쓴다.
스펙: RUNTIME-SPEC.md (kanban t_27ca1582), 후속 실증: t_0d90b8eb.

## 설치 (m2max 기준, mini는 IP/포트만 치환) — 후속 t_0d90b8eb에서 실행할 절차

1. person-terminal: secret 생성 (value를 어디에도 붙여넣지 말 것)
   `python3 -c "import secrets;print(secrets.token_hex(16))" > ~/.hermes/tt-runner.secret && chmod 600 ~/.hermes/tt-runner.secret`
2. 레지스트리 `~/.config/tt-runner/agents.json` (아래 예, 실설치 확인된 CLI만 등록)
3. launchd `~/Library/LaunchAgents/com.tt.runner.plist`:
   ProgramArguments=[/usr/bin/python3, <repo>/runner/tt-runner.py, serve],
   EnvironmentVariables: TT_URL=<TT server url>,
   TT_RUNNER_BIND=<이 머신 tailnet IP>, TT_RUNNER_PORT=7796, TT_RUNNER_NAME=runner-m2max,
   TT_AGENT=runner@m2max, RunAtLoad+KeepAlive
   `launchctl load ~/Library/LaunchAgents/com.tt.runner.plist`
4. TT 등록: `tt`로는 불가 — 서버에 `POST /agents {"name":"runner-m2max","base_url":"http://<이 머신 tailnet IP>:7796/hook","secret":<파일 값>}`
5. 검증: `curl http://<tailnet ip>:7796/health`, bad-secret POST→401, 헤더 누락→400,
   그 다음 실제 dispatch 1건 E2E (t_0d90b8eb 소관)

비-tailnet 바인딩·secret 파일 부재는 기동 자체가 실패한다(rc=3, fail-closed).

## 레지스트리 스키마

```json
{
  "machine": "m2max",
  "agents": {
    "runner-m2max": {
      "driver": "codex",
      "binary": "/Applications/ChatGPT.app/Contents/Resources/codex",
      "permission_mode": "read",            // read | auto | all (all은 tailnet 확인 머신만)
      "model": null, "reasoning": null,     // codex: -m / model_reasoning_effort
      "workspace": "~/Work",
      "allowed_workspaces": ["~/Work"],     // workspace 오버라이드 허용목록
      "timeout_s": 1800, "keep_shell": true,
      "continue_cmd": "/Applications/ChatGPT.app/Contents/Resources/codex exec --sandbox read-only --skip-git-repo-check"
    }
  }
}
```

등록 규칙(실측 반영):
- 레지스트리 키는 TT `/agents`의 agent 이름과 동일해야 한다 — dispatch payload의
  `agent` 필드가 그대로 키 조회에 쓰인다. 불일치 시 REJECT(profile-unknown) + TT 거부 코멘트.
- binary는 앱 번들 codex 절대경로를 쓴다. homebrew codex-cli 0.156.0은
  `timed out negotiating with the code-mode host`로 shell/codex exec 모든 파일·명령
  도구가 실패한다(t_0d90b8eb 실측 — clean CODEX_HOME, mcp disable, features.code_mode=false
  모두 실패; ChatGPT.app 번들 0.155.0-alpha.16은 정상). ChatGPT.app 업데이트 시 버전 확인.
- continue_cmd에 `--skip-git-repo-check` 필수 — workspace(~/Work)가 git 저장소가
  아니므로 없으면 계속 재지시가 즉시 실패한다(실측).
- launchd plist의 PATH에 /opt/homebrew/bin 포함 — 기본 env에는 없어 tmux/tailscale 조회가 실패한다.

dispatch 오버라이드는 message 선두 `#opts {"model":...,"reasoning":...,"workspace":...,"continue_session":true}`
한 줄 JSON만 인식(허용 키 외 OVERRIDE-IGNORED). binary/permission 승격 불가.

## CLI

- `tt-runner.py serve` — launchd 상주 (감시자·polling 스레드 내장)
- `tt-runner.py status [dispatch_id]` / `attach <dispatch_id>` / `stop <dispatch_id>` / `poll`
- run 장부: `~/.local/state/tt-runner/runs.json` (key `<issue_id>#<dispatch_id>`)

## 검증 상태 (2026-09-25, 이 커밋 시점)

- unittest 23 green (runner/test_tt_runner.py — M3BZS1G3 확장 8: BLOCKED/크래시 분리,
  stall 임계·리셋·kill, guard root/sanitize, agent_env 낙찰·opt-in)
- loopback fake-TT smoke 20 항목 green (runner/smoke_tt_runner.py, TT_TMUX_SOCKET 격리):
  즉시200+context 세션토큰, 신규 tmux 실행→마커→done 코멘트, DUP-SKIP(중복 dispatch_id),
  401/400, 파괴적 게이트 hold→'승인' 방출 실행, context 계속 재지시(send-keys mode),
  INBOX-NOT-READY(pending 404), 시크릿 로그 마스킹, +M3BZS1G3: 가짜 CLI 'approval
  required' exit=1 → BLOCKED(waiting_for=human)/크래시 exit=1 → failed 구분,
  등재됐으나 없는 workspace → 시작 전 workspace-guard 거부, env-print CLI에서
  시크릿 canary 미노출·benign 유지, 무음 CLI → STALL 코멘트 → stall-killed(임계 6s/4s 주입)
- codex 실실행 스모크 PASS (runner/smoke_tt_runner_codex.py — ChatGPT.app 번들,
  stdin 프롬프트→note.txt 판독→done, codex-canary env 미노출 확인)
- 미실증: 실제 mini TT 서버 대상 dispatch 왕복, launchd 설치 후 재부팅 생존,
  context 왕복(서버 코드상으로는 deliver→dispatches.context→다음 payload 실림을
  app.py로 확인, 실서버 E2E는 후속), P7EK 파괴적 패턴 목록 원문(초안 목록으로 대용),
  opencode 세션 드라이버(plain 드라이버만 존재), mini/m1 설치(ssh 차단)

## 보안 규칙

secret 값은 파일·plist env·TT 코멘트·git·로그 어디에도 기록하지 않는다.
mask()가 로그/코멘트를 통과시키며, 바인딩은 tailnet IP 또는 127.0.0.1만 허용.
permission all은 tailscale 확인 머신에서 강등 없이, 아니면 auto로 강등(PERM-DOWNGRADED 로그).
파괴적 패턴(rm -rf, git push, sudo, drop, ...) 감지 시 GATE-HOLD — TT에 '승인' dispatch가
올 때까지 실행하지 않는다.

## 실행 감시 (M3BZS1G3-VNQH) — 기본값 초안, 카드 코멘트 승인 후 확정

- 입력필요 vs 크래시: exit!=0일 때만 출력/pane tail을 INPUT_SIGNATURES와 대조 —
  hit이면 failed가 아니라 BLOCKED(코멘트에 waiting_for=human, 장부 status=blocked,
  blocked_on=시그니처). 자동 재시도 없음 — '승인' 또는 새 지시 dispatch를 기다린다.
  기본 시그니처 10종: requires approval / approval required|needed / waiting for input…/
  needs input|approval|confirmation / permission required|needed / do you want to allow…/
  please approve|confirm…/ [y/N]·[yes/no] / press enter to continue / needs_input·
  needs_approval·external_permission. exit=0 출력은 판정하지 않아 오검지를 줄인다.
- stall 무음 탐지: pane 마지막 10행 지문 + run 파일(.log/.out) mtime이
  TT_STALL_SILENCE_S(기본 600s) 그대로면 STALL 코멘트(pane tail 첨부, TIMEOUT과 구분,
  stall_notified 1회), 거기서 TT_STALL_KILL_AFTER_S(기본 600s) 더 무음이면 C-c→kill +
  stall-killed 장부. pane만 보면 codex처럼 파일로 쓰는 실행은 오탐하므로 파일 mtime을
  liveness에 포함. 출력 재개(지문 변경) 시 카운터 리셋. wall-clock TIMEOUT(기본 1800s)은
  별도 경로로 유지.
- 워크스페이스 불변식 3종(Symphony SPEC §9.5): ① dispatch 오버라이드 값 sanitize
  (제어문자/NUL/개행/RTL/OVERLENGTH → OVERRIDE-REJECT, 프로필 기본값으로 폴백)
  ② 허용 root(allowed_workspaces∪workspace, 없으면 ~) realpath 검사 — execute_once에서
  1차, run_agent 실행 직전 2차(방어 다층), 위반은 실행 시작 전 거부
  (status=failed, detail=workspace-guard:<reason>, TT 거부 코멘트).
  ③ 실행 cwd는 guard가 통과시킨 realpath로 강제.
- credential 격리: run-agent가 자기 프로세스 env를 agent_env()로 필터링 후 자식/pane 셸로
  승계 — 이름 패턴(SECRET|TOKEN|PASSWORD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY) +
  TT_RUNNER_* prefix + 값에 TT secret을 포함하는 키 전부 낙찰. 꼭 필요한 키는 레지스트리
  `env_extra`(프로필 필드)로만 opt-in. 한계: same-user 파일(secret file 자체)은 못 막는다.
- smoke 격리: TT_TMUX_SOCKET 설정 시 tmux -L 전용 서버 사용(실 세션과 충돌 없음).

## TT 작업 계약 전달

dispatch payload의 `work_contract`와 `execution_attempt`를 장부에 저장하고, 신규 실행(stdin/argv)과
재개 실행의 프롬프트에 주입한다. `#opts`는 원래 메시지에서 먼저 해석한다. 계약이 없는 구형 payload는
그대로 실행한다. 계약 전달 테스트는 실제 LLM의 TDD 준수 증거가 아니다. 상세 계약: [api.md](../server/static/api.md).
