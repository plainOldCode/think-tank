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

`lease_by / lease_expires / heartbeat_at`. pull·claim 시 1h TTL 부여, `POST /issues/{id}/lease` heartbeat로 1h 연장(버전 미변경). **만료 lease는 pull이 atomic steal로 회수**(크론 크래시 시 1h 뒤 자연 해금). done/cancelled/todo/backlog 전이 시 lease 소거. `pull`의 `require_label` 게이트로 자동화 크론은 `auto` 라벨 카드만 수령(agent당 활성 lease 2한도). UI: 에이전트명 hash→hue 테두리, 만료는 주황 점선 ⌛.

## CLI
`tt new|pull|list|search|show|tree|claim|note|done|state|label|labels|edit|unassign|archive|unarchive|push|health` — `TT_URL`/`TT_AGENT` 환경변수. 전역 `--json`(어디서나) 서버 원 JSON 그대로 출력. pull/claim/heartbeat `--hours N`(1~6).
`tt archive ID|auto` (auto=완료 30일 경과분 자동 아카이브)



## Agent registry + dispatch (hook/callback 대화)

- `GET/POST /agents`, `PATCH/DELETE /agents/{name}` — `{name, base_url(http/s), secret?, enabled?}`
- `POST /issues/{id}/dispatch {agent, message, author?}`:
  1. `message`를 issue 댓글로 기록 (author=지시자)
  2. agent `base_url`로 webhook POST (timeout 10s, header `Authorization: Bearer <secret>`, `X-TT-Dispatch`)
     payload: `{dispatch_id, issue_id, issue_title, agent, author, message, context, comments:[최근 20], tt_url, work_contract, execution_attempt}`
  3. agent는 즉시 `200 {}` 또는 `200 {"context":"resume-token"}` 회신 — context는 같은 (issue,agent) 다음 dispatch에 그대로 실려 감 (agent측 세션/스레드 이어붙이기용)
  4. 실패(비2xx/타임아웃)는 `⚠ hook dispatch #N ... 실패` 시스템 댓글(author=tt-server) 생성 — 보드에 즉시 표시
- **대화 루프**: agent 회신/추가질문은 기존 `POST /issues/{id}/comments {author:agent명}` → 보드 상세가 5s 폴링으로 실시간 표시. 사용자가 보드 입력창에 답하면 dispatch(전달+context 유지) 또는 note(로그만)로 재전달
- CLI: `tt agents`, `tt agent add NAME URL [secret]`, `tt agent rm NAME`, `tt agent enable|disable NAME`, `tt dispatch ID -A AGENT "지시" [-a author]`
- agent 수신기 구현 요령: webhook은 즉시 200만 받고 작업은 백그라운드(세션 resume은 context에 저장된 토큰 사용). 처리 결과·질문은 comments로.
- `GET /issues/{id}/dispatches` — 발송 이력(status: queued|ok|error, detail, context)

## Changelog (append-only)
- 2026-09-25: **done≠verified 게이트 + blocked→human Level4 알림 (M3BZV172-9F0S)** — review 상태 신설(증거 없는 done 강등지), POST verify(review→done 확정), 코멘트 증거(SHA/링크/테스트/마커) 자동 연결+verified 필드, close 라벨/force_done 승인 우회(bridge close 규약 정합), TT_DONE_GATE=gate|warn|off. agents.notify_hook capability: blocked(waiting_for=human) 1회 Level4(A/B+recommendation) X-TT-Command:notify 발송(기존 webhook 패턴→Hermes Telegram 주입, 자체 APNs 없음), blocked_notified_at dedup, legacy `waiting_for=human` 코멘트 마커 호환. CLI tt verify/tt agent notify, UI review 열+verify 버튼
- 2026-09-25: **blocked 지능화 (M3BZS1FS-5722)** — waiting_for/waiting_actor/blocked_detail 필드(하위 호환 migration), GET why-blocked, reconcile release(terminalize→runner kill, agents.release_hook capability), 의존 종료 시 release-ready 표시+댓글(자동 재dispatch 없음), CLI tt block/tt why, UI ⏸ 배지
- 2026-09-24: **agent registry + /dispatch hook/callback** — agents/dispatches 테이블, webhook 발송+context resume, 실패 시스템 댓글, UI Agents 패널/지시 폼, CLI tt agents/agent/dispatch
- 2026-09-23: CLI `done` 실패 시 서버 409 detail을 그대로 출력(부모 가드 메시지 등이 사용자에게 보이도록)
- 2026-09-23: **부모 done 가드** — `PATCH state:"done"` 시 미완료(done/cancelled 아님) 자식 존재 시 409. 자식 종결 후 부모 done 가능
- 2026-09-23: PATCH `assignee:""` 시 lease_by/lease_expires 동시 소거 (unassign = lease 해제 세트). CLI: `unassign`, `labels set`, `edit --assignee/--labels/--state`, `--json`, `label add|rm`, `search`, `--hours`, `list --archived`, claim도 lease 2한도 적용
- 2026-09-23: lease 시스템(`lease_by/lease_expires/heartbeat_at`, TTL 1h, require_label, steal), `GET /install.sh`(자기 CLI를 요청 base_url로 구워 서빙), `archived` 컬럼+필터, `GET /issues?q=`

- 2026-09-26: **버전 고정 작업 계약과 완료 보고** — `/work-contract`, claim/pull/dispatch 계약 전달, runner 신규·재개 프롬프트 주입, `TT_REQUIRE_REPORT=1` opt-in 보고 필수 모드. `completion_report`의 TDD/대체 검증·성공 여부·회차·버전 검사, `verification_status`로 보고/승인/과거 기록 구분. 오류 보정: 실패/키워드만 있는 증거의 완료 판정, 재오픈/범위 변경/새 수령의 오래된 증거 재사용, 생성 시 상태 우회. CLI done 중복 PATCH 제거, 코멘트 실패 시 중단, review도 `--json` 준수. 라우트와 기존 승인 예외는 유지.

- 2026-09-26: **업그레이드 중 기존 작업 호환성** — 운영 SQLite 복제 검증에서 빈 work_contract에 현재 서버의 보고 필수 정책이 소급 적용되는 문제를 재현·수정. 기존 진행/검토 회차는 호환 마감 가능하며 새 claim/pull/범위 변경부터 고정 계약 적용. 회귀 116건과 복제본 HTTP 시나리오 30개 통과; 상세는 [배포 전 검증 기록](predeployment-sqlite-validation-20260926.md).
