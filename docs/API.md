# think-tank (tt) API

서버: 상주 머신 `http://<TT_HOST>:7800` (신뢰망 주소/localhost 바인딩, launchd `com.tt.server`)
DB: `~/think-tank/data/tt.db` (SQLite WAL) — 주기 백업 권장(`VACUUM INTO` 스냅샷)

## 상태 기계
backlog → todo → in_progress → blocked/review/done. review → todo/blocked/done/cancelled. todo→done 금지(claim 필수). todo→backlog 강등 가능. done→todo 재오픈. cancelled→todo.
`in_progress→todo` release(assignee 초기화).

## 엔드포인트
| | |
|---|---|
| `GET /work-contract` | `{version, instructions, report_required}` — 수령/dispatch에도 전달 |
| `POST /issues` | `{title, body?, parent_id?, priority?(1-4), labels?[], state?=todo|backlog}` |
| `GET /issues?state=&parent=&label=&assignee=&q=&limit=&archived=` | parent=none → 루트만. archived: `no`(기본·숨김) `all` `only` |
| `GET /issues/{id}` | + children, comments |
| `GET /issues/{id}/tree` | 재귀 트리 |
| `POST /issues/{id}/claim` | `{agent}` — todo+미배정만, 409 |
| `POST /pull` | `{agent}` — 우선순위(p1..p4)→생성순으로 하나 atomic claim, 없으면 null |
| `PATCH /issues/{id}` | 전이 가드 + `expected_version` 낙관적 락, `clear_parent`/`clear_priority`, `archived:true/false`, `waiting_for`(blocked 전용: dependency|human|gate|external)+`waiting_actor`/`blocked_detail`, `force_done`(승인 예외), `completion_report`(구조화된 완료 보고) |
| `POST /issues/{id}/verify` | `{verifier, evidence?, completion_report?, expected_version?}` — review→done 보고 접수. 비-review/회차 충돌 409, 보고 오류 422 |
| `POST /issues/{id}/comments` | `{author, body}` |
| `GET /issues/{id}/why-blocked` | blocked 사유 투영: gate·기준·dependencies·missing·release_ready·evidence·next_commands. 비-blocked 409 |

동시 수령은 `version` 컬럼 낙관적 락 — `pull` 동시 호출이 서로 다른 이슈를 받도록 설계·테스트됨.

## 방법론과 완료 보고

공통 지침과 JSON 예시는 [AI 작업 계약서](../server/static/api.md)의 기본 방법론·완료 보고 절을 따른다.
`TT_REQUIRE_REPORT=1`은 새 수령 회차에 구조화된 TDD/대체 검증 보고를 필수로 한다(기본 0: 코멘트 호환).
`work_contract`는 버전·지침·보고 정책 스냅샷이며 `execution_attempt`와 함께 완료 보고에 연결된다.
`verification_status=reported`는 보고 접수, `approved`는 force_done/close 승인 예외다. 둘 다 독립 실행 검증이 아니다.
재오픈·새 수령·범위 변경은 이전 증거를 무효화한다. 기존 DB는 컬럼 추가로 보존하며 과거 완료는 `legacy`로 표시한다.
CLI: `tt contract`, `tt done ID --report FILE`, `tt verify ID --report FILE`. 구형 CLI/runner는 자동 갱신되지 않는다.

## blocked 지능화 (M3BZS1FS-5722)

- `waiting_for`/`waiting_actor`/`blocked_detail` 컬럼(backwards-compatible migration, 미입력 시 기존 동작). blocked 이탈 시 자동 소거. CLI: `tt block ID KIND`, `tt why ID`
- **reconcile release**: dispatch 이력 있는 카드가 done/cancelled로 terminalize되면 서버가 `release_hook=true` agent에게 `X-TT-Command: release` 명령을 base_url로 발송 → runner는 장부 cancelled + tmux kill-session. 결과는 tt-server 시스템 댓글, 전이는 되돌리지 않음(best-effort)
- **의존 종료 표시**: dependency blocked 카드는 의존 이슈 done/cancelled 시 `release_ready:true`+`[release-ready]` 댓글(1회). 자동 재dispatch 없음 — 재개는 수동


## lease (실행 점유 — 라이프사이클과 직교)

`lease_by / lease_expires / heartbeat_at`. pull·claim 시 1h TTL 부여, `POST /issues/{id}/lease` heartbeat로 1h 연장(버전 미변경). `POST /issues/{id}/ping` `{agent}`는 `heartbeat_at`만 즉시 갱신(TTL·version 불변, 비보유자 409) — 선택적 수동 확인. 보드는 **유효 lease 보유 자체**를 초록 깜빡임으로 표시(주기 통신 요구 없음), lease 반납(done/cancelled/todo 전이·steal)로 자동 해소. UI 관측: 작업 중 30분 간격 heartbeat로 TTL만 유지. **만료 lease는 pull이 atomic steal로 회수**(크론 크래시 시 1h 뒤 자연 해금). done/cancelled/todo/backlog 전이 시 lease 소거. `pull`의 `require_label` 게이트로 자동화 크론은 `auto` 라벨 카드만 수령(agent당 활성 lease 2한도). UI: 에이전트명 hash→hue 테두리, 만료는 주황 점선 ⌛.

## CLI
`tt new|pull|list|search|show|tree|claim|note|done|state|label|labels|edit|unassign|archive|unarchive|push|health` — `TT_URL`/`TT_AGENT` 환경변수. 전역 `--json`(어디서나) 서버 원 JSON 그대로 출력. pull/claim/heartbeat `--hours N`(1~6).
`tt archive ID|auto` (auto=완료 30일 경과분 자동 아카이브)



## Agent registry + dispatch (hook/callback 대화)

- `GET/POST /agents`, `PATCH/DELETE /agents/{name}` — `{name, base_url(http/s), secret?, enabled?, model?, reasoning?, tier?}`
  - `model`/`reasoning`: 자유 문자열(검증 없음). **선언(declaration)이지 실행 보장(enforcement)이 아님** — runner가 실제로 다른 모델을 쓰면 그건 runner의 문제다. 2026-09-26 모델 pinning 사건(M3ER6G3S-RZ20)에서의 교훈: 판정 계층 규약이 TT 어디에도 없으면 사고처럼 보인다
  - `tier`: 계층 enum `sota|exec|impl|human` — 한글 별칭 `판정|실행|구형` 자동 정규화, 대소문자 무시, 무효값 422. sota=판정(설계/리뷰), exec=실행/판독, impl=구현, human=사람
  - CLI: `tt agent add NAME URL [secret] [--model M] [--reasoning R] [--tier T]`, `tt agent set NAME model=M reasoning=R tier=T`, `tt agents` 출력에 `model=x/y [tier]` 접미(기존 접두 포맷 유지 — 후방호환)
- `POST /issues/{id}/dispatch {agent, message, author?}`:
  1. `message`를 issue 댓글로 기록 (author=지시자)
  2. agent `base_url`로 webhook POST (timeout 10s, header `Authorization: Bearer <secret>`, `X-TT-Dispatch`)
     payload: `{dispatch_id, issue_id, issue_title, agent, author, message, context, comments:[최근 20], tt_url, work_contract, execution_attempt}`
  3. agent는 즉시 `200 {}` 또는 `200 {"context":"resume-token"}` 회신 — context는 같은 (issue,agent) 다음 dispatch에 그대로 실려 감 (agent측 세션/스레드 이어붙이기용)
  4. 실패(비2xx/타임아웃)는 `⚠ hook dispatch #N ... 실패` 시스템 댓글(author=tt-server) 생성 — 보드에 즉시 표시
- **대화 루프**: agent 회신/추가질문은 기존 `POST /issues/{id}/comments {author:agent명}` → 보드 상세가 5s 폴링으로 실시간 표시. 사용자가 보드 입력창에 답하면 dispatch(전달+context 유지) 또는 note(로그만)로 재전달
- CLI: `tt agents`, `tt agent add NAME URL [secret]`, `tt agent rm NAME`, `tt agent enable|disable NAME`, `tt dispatch ID -A AGENT "지시" [-a author]`
- agent 수신기 구현 요령: webhook은 즉시 200만 받고 작업은 백그라운드(세션 resume은 context에 저장된 토큰 사용). 처리 결과·질문은 comments로.
- `GET /issues/{id}/dispatches` — 발송 이력(status: queued|ok|error, detail, context) + 실행 투영 필드(run_state, machine, session, started_at, last_progress_at, last_tail, ended_at) + `model`(발송 시점 대상 agent 모델 스냅샷 — 감사 추적 선언값, 미등록은 빈 문자열). webhook payload에도 동일 `model` 필드 병기
- `POST /issues/{id}/dispatches/{did}/progress` — 러너→서버 진행 투영(dispatch 레코드만 갱신, 코멘트 무생성, last-write-wins): `{state:"queued|running|stalled|finished|failed", tail?, ts?, machine?, session?}`. 헤더 `x-tt-dispatch` + `Authorization: Bearer <agent secret>`(secret 빈 agent는 생략 허용 — dispatch deliver 규약 동일). running/stalled만 tail/ts 진행 반영(tail 서버 500자 클램프), finished/failed는 run_state·ended_at만. 미존재/issue 불일치 404, secret 불일치 403, state 누락/비enum 422. 상태 머신 가드 없음(과잉 차단 금지).
- `GET /agents/active` — 활성 실행(dispatch) 목록: `run_state ∈ {queued, running, stalled}`만. `[{dispatch_id, issue_id, issue_title, agent, machine, session, run_state, started_at, last_progress_at, elapsed_s, last_tail}]`. stalled는 러너 stall_check가 보낸 값 그대로 노출(서버 재계산 없음). 빈 결과 200 + [].

## Changelog (append-only)
- 2026-10-04: **agent 모델 메타데이터 (M3ER6G3S-RZ20)** — agents에 model/reasoning(자유 문자열, 선언=declaration 비-enforcement)·tier(sota|exec|impl|human, 한글 별칭 정규화) 추가, dispatches에 발송 시점 model 스냅샷(감사 추적, webhook payload에도 병기), CLI tt agent add --model/--reasoning/--tier + tt agent set key=val + tt agents 접미 표기(후방호환), UI agent 카드 모델 표기. 시드: codex·codex-read-only=gpt-6.1-sol/xhigh[sota], agy=gemini-3.8-flash/high[exec], opencode=gx10 qwen3.8-flash-next[impl]

- 2026-09-29: **runner 진행 관찰 — 진행 투영·활성 조회 API (M3EREF97-FXWQ, dispatch#57)** — dispatches에 실행 상태 컬럼 7종(run_state/machine/session/started_at/last_progress_at/last_tail/ended_at, 기존 행 ''=비-tmux), POST dispatches/{did}/progress(코멘트 무생성 투영 쓰기, deliver 헤더 규약 상속, tail 500자 클램프), GET /agents/active(활성=queued/running/stalled, stalled 판정 소스는 러너 단독). 상태:''(미표시)|queued(회색)|running(blink=last_progress_at 임계 내, 보드 90s 권장)|stalled(적색·blink 정지)|finished(활성 소멸)|failed(적색; status=error와 다른 층위). 코멘트 왕복(시작/STALL/종료) 불변 — 진행 코멘트 0. 모델 병기·L1 진행률·L2 ETA·stalled 알림은 범위 외(후속). docs/API.md와 static/api.md 동시 갱신.
- 2026-09-25: **done≠verified 게이트 + blocked→human Level4 알림 (M3BZV172-9F0S)** — review 상태 신설(증거 없는 done 강등지), POST verify(review→done 확정), 코멘트 증거(SHA/링크/테스트/마커) 자동 연결+verified 필드, close 라벨/force_done 승인 우회(bridge close 규약 정합), TT_DONE_GATE=gate|warn|off. agents.notify_hook capability: blocked(waiting_for=human) 1회 Level4(A/B+recommendation) X-TT-Command:notify 발송(기존 webhook 패턴→Hermes Telegram 주입, 자체 APNs 없음), blocked_notified_at dedup, legacy `waiting_for=human` 코멘트 마커 호환. CLI tt verify/tt agent notify, UI review 열+verify 버튼
- 2026-09-25: **blocked 지능화 (M3BZS1FS-5722)** — waiting_for/waiting_actor/blocked_detail 필드(하위 호환 migration), GET why-blocked, reconcile release(terminalize→runner kill, agents.release_hook capability), 의존 종료 시 release-ready 표시+댓글(자동 재dispatch 없음), CLI tt block/tt why, UI ⏸ 배지
- 2026-09-24: **agent registry + /dispatch hook/callback** — agents/dispatches 테이블, webhook 발송+context resume, 실패 시스템 댓글, UI Agents 패널/지시 폼, CLI tt agents/agent/dispatch
- 2026-09-23: CLI `done` 실패 시 서버 409 detail을 그대로 출력(부모 가드 메시지 등이 사용자에게 보이도록)
- 2026-09-23: **부모 done 가드** — `PATCH state:"done"` 시 미완료(done/cancelled 아님) 자식 존재 시 409. 자식 종결 후 부모 done 가능
- 2026-09-23: PATCH `assignee:""` 시 lease_by/lease_expires 동시 소거 (unassign = lease 해제 세트). CLI: `unassign`, `labels set`, `edit --assignee/--labels/--state`, `--json`, `label add|rm`, `search`, `--hours`, `list --archived`, claim도 lease 2한도 적용
- 2026-09-23: lease 시스템(`lease_by/lease_expires/heartbeat_at`, TTL 1h, require_label, steal), `GET /install.sh`(자기 CLI를 요청 base_url로 구워 서빙), `archived` 컬럼+필터, `GET /issues?q=`

- 2026-09-29: **3-1 확장(저장소)** — 대상 저장소는 카드 명시(`repo: owner/name`, 생략 시 think-tank). 다른 저장소 카드는 그 저장소 CI 워크플로가 probe 병합의 전제(armour PR#5~#7 실증: 판정·사냥은 저장소 무관). probe = dispatchd 라운드(mini launchd, gh 절대경로).
- 2026-09-29: **CI + probe 병합**: `.github/workflows/ci.yml`(PR/push에 pytest+smoke). dispatchd에 probe 라운드 신설 — open PR 수집(gh pr list/checks/run list) → `ci_passed`(checks 전부 SUCCESS + head_sha run completed/success, 순수) → branch `tt/<카드ID>-slug` 역참조 + 카드 계약/attempt 검증 → `gh pr merge --squash` 실행. 전이: in_progress+유효보고는 서버가 이미 완료 처리(done 유지), 보고 없으면 review 강등 시도(불법 전이는 log 후 생략). gh 미인증 환경은 prs=[]로 무해. mini는 토큰 부재 — probe 가동 machine은 관측(35B4→B52W).
- 2026-09-29: **3-1 브랜치 규약 (DEQ1 v5)** — 코드 작업은 카드별 브랜치 `tt/<카드ID>-<slug>` → GitHub PR로만 main 병합(merge 권한·시점은 사용자). 메인 직push 금지. 예외는 dispatch message 명시분만(주 유지보수자 워크스테이션 = mini 릴레이 sync 경로). v2 블록 동일 문구 — 단 v1 블록도 같은 조항 추가(구 고정 카드는 claim 시 저장본 사용이라 안전).
- 2026-09-29: **`/m` 빌드 자동 갱신** — 응답 직전 `const BUILD`(파일 mtime) 주입, 클라이언트가 5분마다 HEAD성 fetch로 대조 후 불일치 시 state(DRAFT/OPEN)를 localStorage에 남기고 auto-reload. 오래 열어둔 모바일이 구판에 갇히는 문제의 구조 해결(5CCS 사례).
- 2026-09-29: **계약 v2 (3단계 필수)** — `TT_CONTRACT_VERSION=2` 전환 시 신규 claim/pull은 `tt-tdd-v2` 계약 고정: 보고 JSON이 `design`(criteria+verification+evidence)/`implementation`(summary+commands)/`verification`(commands+evidence) 3블록 전부 필수, method `tdd|planned`(alternative 폐지). 스키마는 단일 모델 버전 게이트 — v1 보고는 v1 고정 카드에서만 통과, 혼용 422. 유효 보고 done은 self-completion으로 done 유지(verification_status=reported), 보고 없는 done은 review 강등(기존과 동일).
- 2026-09-26: **버전 고정 작업 계약과 완료 보고** — `/work-contract`, claim/pull/dispatch 계약 전달, runner 신규·재개 프롬프트 주입, `TT_REQUIRE_REPORT=1` opt-in 보고 필수 모드. `completion_report`의 TDD/대체 검증·성공 여부·회차·버전 검사, `verification_status`로 보고/승인/과거 기록 구분. 오류 보정: 실패/키워드만 있는 증거의 완료 판정, 재오픈/범위 변경/새 수령의 오래된 증거 재사용, 생성 시 상태 우회. CLI done 중복 PATCH 제거, 코멘트 실패 시 중단, review도 `--json` 준수. 라우트와 기존 승인 예외는 유지.

- 2026-09-26: **업그레이드 중 기존 작업 호환성** — 운영 SQLite 복제 검증에서 빈 work_contract에 현재 서버의 보고 필수 정책이 소급 적용되는 문제를 재현·수정. 기존 진행/검토 회차는 호환 마감 가능하며 새 claim/pull/범위 변경부터 고정 계약 적용. 회귀 116건과 복제본 HTTP 시나리오 30개 통과; 상세는 [배포 전 검증 기록](predeployment-sqlite-validation-20260926.md).
