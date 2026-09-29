# dispatchd — TT 자율 디스패치 설계 (M3PEVTDH-5CCS)

상태: 설계 확정 v1 (2026-09-29, M3PG5Q9W-30KP). 구현은 5R5B 이하 카드.

## 결정 요약
- mini launchd 잡 `com.tt.dispatchd` (tt-server와 동거, TT_URL=http://127.0.0.1:7800), 30초 루프.
- **public HTTP API만 소비, 서버 무수정, 사설 state 없음** — 판단 근거는 전부 카드·dispatches 이력에 반영(재생 산출 가능).
- 실행 라운드는 **비상주 원샷**(현 dispatch 프로토콜 그대로). "이어짐"은 카드 그래프(sibling/child 우선)와 3단계 보고로 보장. 상주 LLM 세션은 범위 밖(관측 후 별도 결정).
- dispatchd는 상태 머신이 아니라 **판순 함수 + 실행기**: 라운드 입력( 스냅샷 ) → 결정 목록. 정책은 순수 함수로 TDD.

## API 표면 (실측 어서션, 2026-09-29 app.py/openapi)
| 용도 | 실 라우트 | 규약·주의 |
|---|---|---|
| 후보 조회 | `GET /issues?state=todo&label=auto&limit=200` | 필터: state/parent/label/assignee/q/app.py:242-256. pull의 원자력 SQL과 동일 조건(`state='todo' AND assignee=''`, app.py:350-352) |
| 특정 카드 claim | `POST /issues/{id}/claim {agent}` | state≠todo 409(app.py:302), 타인 assignee 409, agent당 lease 2한도(app.py:348 — max_leases 공통) — **claim 시 attempt+1·계약 고정·in_progress** |
| dispatch 전달 | `POST /issues/{id}/dispatch {agent, message}` | message 필수(422), agent 등록+enabled 필수(902-911). dispatch는 수령이 아니라 **전달** — claim과 반드시 분리(app.py:902, api.md 규약) |
| 수동 수령(대체) | `POST /pull {agent, require_label}` | ID 지정 불가(풀 방식). dispatchd는 **claim 우선 사용** — W1DP 함정 회피 |
| 라운드 이력 | `GET /issues/{id}/dispatches` | 시도 카운트의 진원(app.py:729) |
| release_ready | `GET /issues/{id}`(why-blocked 투영) | blocked+dependency의 의존 전부 done/cancelled → release_ready(app.py:619-622, 695-696). **pull/claim 후보에 blocked는 없음(app.py:350) — 재개는 todo 전이가 필수** |
| agent 목록 | `GET /agents` | base_url(hook), enabled, release_hook/notify_hook — dispatch 대상은 enabled 러너만(app.py:826-836) |
| 진행 관찰(선택) | `GET /agents/active` | FXWQ — 라운드 진행 판단 보조 |

## 라운드 알고리즘 (판순 함수 decide(snapshot) → actions[])
1. kill: env `TT_AUTO_DISPATCH != 1` → 아무 동작도 하지 않는다(로그 1회/10분).
2. 유휴 판정: 등록 agent별 활성 lease(`GET /issues?assignee=<agent>&state=in_progress`) — 유휴 agent만. dispatchd 정책은 **agent당 1 카드**(직렬). 서버 한도(2)보다 보수적.
3. 대상 탐색 우선순위:
   a. 직전 완료 카드(`assignee=<agent>`, state=done)의 **todo 자식**(미완료, deps clear) → 같은 흐름 연속.
   b. 그 카드의 parent의 **todo 형제**.
   c. **blocked+dependency+release_ready** → (승인 근거: 서버 기계 판정 app.py:619-622) PATCH state=todo 후 4로.
   d. 후보 풀 `state=todo&label=auto` 중 **의존 clear** + 우선순위 낮은 순(숫자 우선순위 정렬은 확장점).
4. 예산 게이트(실패 루프 차단): 대상 카드의 dispatches 기록 ≥ 2이고 state≠done → dispatch하지 않고 `needs-human` 라벨 + note. `execution_attempt ≥ 2`도 동일.
5. 실행: claim → dispatch(message: 카드 제목/본문 + 수령· heartbeat·3단계 보고 지시) → note `[auto] dispatch`. claim 409(선점)은 조용히 스킵.
6. 실패 시: dispatch webhook 5xx/타임아웃 → exponential backoff(30→300s 상한), 카드 state는 건드리지 않음(원샷 재시도는 다음 라운드, 단 예산 게이트 내).
7. 스 킵은 침묵(로그만) — dispatch/needs-human/재개 결정만 카드 note(노이즈 통제).

## 실패 기준 (틀리면 반박되는 표)
- 라운드 간 세션/기억 없음 — state는 요청 직전 스냅샷만. cache 없음.
- pull(풀) 미사용 — 특정 카드는 claim만.
- 2회 시도 카드(done 아님) 재dispatch 없음 — needs-human. 자동화 runaway 원천 차단.
- dispatchd는 dispatch/note/(release_ready 카드의) todo 전이만 한다. done/review/cancelled 변경 불가.
- 서버 장애 시 쓰기 0(백오프), lease는 TTL로 자연 해금(만료 시 다른 agent가 steal — 카드 1회 비용).
- 첫 dispatchd 자체는 사람이 구현·등록(bootstrap 예외). BKXA에서 실 runner 1개 E2E.

## 구현 카드 매핑
- 5R5B: 코어(decide 순수 함수 + 실행기 + launchd plist + backoff) — 판순 함수 8~10 판정표 TDD.
- Z1CY: (a)(b) continuation 규칙의 그래프 탐색 정밀화 — 5R5B 기본 구현 후 확장.
- TKXA: 예산 게이트(4)·kill switch(1) — 결정표에 흡수되므로 5R5B에 병합 취소 가능(5R5B가 판정).
- MCW3: 로그/노트 포맷 — 5R5B에 흡수 예상(중복 판정 가능).
- BKXA: 실 runner E2E.
