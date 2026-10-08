# probe PR 연결 계약 (server/probe/core.py 실측 2026-10-06)

probe가 PR을 카드에 연결하고 병합하는 조건 — 서버 worktree ~/tt-review/think-tank 기준.

## PR 수집 풀 (스캔 대상 repo)
- 서버 상시 known repo(REPO_CARDS): plainOldCode/think-tank, plainOldCode/armour-service-ops **뿐**
- 카드 본문(또는 제목)의 `repo: owner/name` 표기로 풀 추가 — **auto/todo/blocked/review 상태 카드만 기여**, done/cancelled/in_progress는 제외
- 카드당 첫 매치 1개 repo만 (정규표현식 search) → 1카드에 여러 repo 불가. repo마다 카드 분리 필요
- 코멘트의 PR 링크·tt# ID는 풀/매칭 어디에도 안 쓰임 (카드 본문+제목만)

## PR→카드 조인
- 브랜치명의 카드 ID 우선, 없으면 **PR 제목**의 카드 ID (형식 `M[A-Z0-9]{6,9}-[A-Z0-9]{4}`)
- PR 본문의 tt# ID는 매칭에 불인정 → **PR 제목에 (TT <카드ID>) 필수**

## 병합 전제 (전부 충족)
- 카드: work_contract 존재 + execution_attempt ≥ 1 + state not done/cancelled
- PR: non-draft + repo 일치(카드에 repo 표기 있으면 일치 필요) + checks 전부 SUCCESS
- 리뷰 게이트: TT_REVIEW_AGENT 설정 시, 현재 head에 유효한 리뷰어 판정 필요
  - 판정 코멘트 계약(TT 카드, author=리뷰어): 1행 `review: approve|request-changes`, 2행 `PR#<n>@<sha8>` — 불일치·부재는 stale로 재요청 (docs/review-gate.md)
  - 미리뷰→review-request, request-changes→review-fix(반납+수정 위임), approve→merge
- 병합 후 서버 데몬이 verify→done 자동화 (ZK3G)
- review 카드가 병합 불가로 남으면 `[needs-merge a<attempt>]` 사람 판단 요청 1회 (회차당 dedup)
- 리뷰어를 dispatch할 때 TT 코멘트 계약 형식(1행 `review: …`, 2행 `PR#<n>@<sha8>`)을 프롬프트에 명시한다 — 형식 없는 판정은 probe가 무효로 보고 재요청한다

## probe 판정 코멘트 = 소스를 읽어야 할 신호

probe가 `[needs-merge]`·`[review-fix]` 등 판정을 남기면 같은 패턴을 재시도하기 전에
서버 worktree의 `server/probe/core.py` 판정 경로를 먼저 읽는다. 판정은 회차마다 반복되며
사라지지 않는다 — 소스를 확인하지 않고 카드만 다시 만들면 같은 오판정이 반복된다.

## 재검토 재디스패치 (리뷰 STALL 복구, 2026-10-06 실측)
probe의 review-req 마커는 sha별 1회 dedup이라 재요청이 안 된다. 리뷰 dispatch가 STALL-killed되면:
1. ~/.local/state/tt-runner/runs.json에서 실패 dispatch의 payload를 읽는다 (message에 sha 포함).
2. 같은 payload로 dispatch_id를 새 번호로 바꿔 재전송 — **context_in/session은 생략**(continue가 죽은
   세션에 send-keys하면 무음 stall의 원인):
   curl -X POST http://<RUNNER_HOOK_HOST>:7796/hook -H "Authorization: Bearer $(cat ~/.hermes/tt-runner.secret)" \
        -H "X-Tt-Dispatch: 1" -H "Content-Type: application/json" --data-binary @payload.json
3. runner가 tt-codex-<새id> 신규 세션을 만들고 완료 시 harvest가 판정을 TT에 게시한다.

## 브랜치 재작성(rebase/force-push) 규칙

- 선행 PR 병합 후 PR 브랜치를 재베이스할 때는 전용 워크트리(`git worktree add --detach`)에서
  하고, **복구 지점 브랜치를 먼저 찍는다** — 다른 에이전트/사용자 체크아웃을 건드리지 않는다.
- WORKLOG·체인지로그 같은 꼬리-추가 저널은 매 rebase마다 충돌한다 — **양쪽 항목을 모두 보존
  (union)하고 순서만 정리한다.** 한쪽을 지우면 다른 브랜치의 기록이 사라지고 다음 rebase도 깨진다.
- `--force-with-lease=<ref>:<sha>`의 기대 SHA는 **remote-tracking ref에서 `git rev-parse`로
  가져온다** — 직접 쓰거나 축약하면 `stale info` 거부(무해하지만 실패), 그릇된 값은 최신 push를
  덮을 수 있다. 값 생략(`--force-with-lease=<ref>`)은 마지막 fetch 기준 원격추적 ref에 위임.
- push 후: 원격 ref == 재베이스 헤드 검증, PR `mergeable` 재계산, 체크 재실행 확인. PR 브랜치에
  CI 워크플로가 없어도 base(main)에 워크플로가 생기면 merge ref가 상속해 체크가 뜬다 —
  워크플로 파일을 cherry-pick할 필요 없다.
- 재베이스로 head sha가 바뀌면 기존 리뷰 판정(`PR#n@sha8`)은 stale이고 probe가 새 마커로
  재요청한다 — 재베이스가 게이트 흐름을 깨지 않는 이유.

## 실측 사고 (2026-10-06)
1카드에 PR 3개(서로 다른 repo)를 물면 풀 진입 자체가 안 돼 "PR 없음" 판정. 1카드=1PR로 분할하고 각 본문 첫 줄에 `repo:` 표기 + PR 제목에 (TT ID)로 해결.
