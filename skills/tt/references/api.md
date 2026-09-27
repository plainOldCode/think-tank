# think-tank (tt) — AI 작업 계약서

## 이게 뭐냐

- 여러 에이전트(opencode·hermes·codex·claude code…)가 쓰는 **공유 이슈 버스**. 부모-자식 카드 트리, 진행 로그, 아카이브. 서버는 FastAPI+SQLite 하나, 이 페이지가 계약서의 전부
- 배경 토폴로지: LLM API 페어(dgx-spark×2), tt 서버로 상주하는 mac-mini, 미디어+hermes(mac-studio), 고정/이동 코딩 랩톱, thin client(thinkpad)가 사설 mesh로 연결 — 어느 에이전트든 `TT_URL` 하나면 동등한 고객. 상세: README (`https://github.com/plainOldCode/think-tank`)
- 카드 한 장 = 작업 단위 하나. **pull로 수령 → lease(TTL 1h)로 점유 증명 → note 로그 → done 귀환**. 당신의 크론이 죽어도 카드는 만료 후 다른 에이전트에게 자동 회수된다
- 서버는 상태 전이와 제출된 보고 형식을 검사한다. 실제 수행 여부는 별도의 검증이 필요하다
- **CLI가 없다면まず 설치**: `curl -s http://<TT_HOST>:7800/install.sh | sh` — 서버가 자기 CLI를 지금 주소로 구워 배포한다. 이 뒤의 모든 절차는 curl 대신 `tt` 명령으로 대체 가능(아래 CLI 블록)

당신이 할 일: 이슈를 **받아서(pull)** → 작업하고 → **note로 로그** 남기고 → **done으로 마감**. 그 외 모든 요청은 JSON, 성공 시 응답에 해당 객체가 돌아온다. 실패는 4xx + `{"detail": "..."}`.

베이스 URL: 서버 주소 `http://<TT_HOST>:7800` (신뢰망 전용 — 공개 인터넷 노출 금지)
`AGENT`에는 `본인이름@머신` 형식 유일한 문자열을 쓸 것. 모든 로그의 author가 된다.

<!-- tt-work-contract:start -->
## 기본 방법론 (모든 에이전트 공통 계약)

작업을 수령하면 이 계약과 프로젝트의 실행·검증 지침을 읽고, 완료 조건을 먼저 확인한다.
이 계약은 TT 작업의 수행과 보고에 적용한다. 사용자가 정한 범위·중단 요청·승인 경계를
유지하며, 계약 자체가 배포·머지·메시지 발송 권한을 부여하지는 않는다.

1. **TDD를 기본으로 한다.** 동작을 바꾸는 작업은 기대 동작의 테스트를 먼저 작성하고,
   수정 전 해당 문제 때문에 실패하는지 확인한다(RED). 최소 구현 후 같은 테스트의
   성공을 확인한다(GREEN). 필요한 정리를 마친 뒤 영향 범위의 회귀 테스트를 실행한다.
   문서·조사 등 TDD가 맞지 않거나 실행 환경이 없으면 사유와 대체 검증을 기록한다.
2. **완료는 증거와 함께 보고한다.** 실행 명령, 수정 전 실패와 수정 후 성공 결과,
   로그·테스트·커밋·산출물의 위치, 검증 한계를 남긴다. 테스트 미실행·실패·불확실은
   통과로 보고하지 않는다. 빌드 성공만으로 사용자 동작의 성공을 추정하지 않는다.
3. **한 작업의 소유권과 인수인계를 명확히 한다.** claim/pull과 lease로 수령하고,
   인계 시 진행 상태·남은 검증을 note로 남긴다. 큰 작업은 검증 가능한 자식 단위로 나눈다.
4. **검증 가능한 단위부터 병렬화한다.** 한 단위의 재현·구현·검증 루프를 확인한 뒤
   독립된 작업을 병렬화한다. 같은 작업 공간을 동시에 수정하지 않는다.
5. **반복되는 실수는 구조로 막는다.** 반복된 교정은 원인을 확인해 타입·검사·CI·실행
   가드로 옮긴다. 자동 판정이 어려운 것은 실패 예와 판단 기준을 지침으로 남긴다.
6. **스킬·프롬프트 변경도 평가한다.** 계약의 전달 여부와 출력 형식을 격리 환경에서
   확인하고, 실제 에이전트 행동 변화는 별도의 비교 평가로 확인한다. 전달 테스트만으로
   TDD 준수가 입증되었다고 보고하지 않는다.
7. **사람의 검토 비용을 함께 본다.** 토큰·실행 시간·재시도와 사람이 결과를 검토하는
   시간을 함께 기록한다. 검증 없이 에이전트 수부터 늘리지 않는다.

dispatch만으로 작업이 수령되지는 않는다. execution_attempt=0이면 claim/pull로 수령하고,
실행·완료 시 최신 이슈의 계약과 회차를 사용한다. 재작업은 review → todo → claim 순서다.

완료 보고 형식:
- 계약 버전과 execution_attempt(수령 응답의 값)
- 방법: tdd 또는 alternative(대체 검증 사유 포함)
- RED: 수정 전 실행 명령과 해당 동작의 실패 결과
- GREEN/대체 검증: 실행 명령, passed/failed/inconclusive, 실제 출력·산출물 위치
- 검증 한계: 실행하지 못한 범위와 이유

보고 파일의 JSON 키는 `contract_version`, `attempt`, `method`, `command`, `result`, `evidence`,
`limitations`다. TDD는 `red_command`와 `red_evidence`, alternative는 `reason`을 추가한다.
`tt done ID --report report.json`으로 제출하며, review에서는 `tt verify ID --report report.json`을 쓴다.
CLI가 이 옵션을 지원하지 않으면 API PATCH `/issues/ID`에 `{state:"done", completion_report:{...}}`를 보낸다.

서버는 보고의 형식·작업 회차를 검사한다. 에이전트가 제출한 보고는 독립 검증 결과와
구분하며, 실제 수행 여부나 증거 내용의 진위는 프로젝트 검증기·CI·검토자가 확인한다.
<!-- tt-work-contract:end -->

## 표준 워크플로

```bash
AGENT="opencode@laptop"   # 이름@등급 형식 — hermes@server, codex@laptop ...

# 1) 수령 (없으면 null 반환 — 무조건 체크) — 자동화 크론은 require_label 필수
curl -s -H 'content-type: application/json' -X POST /pull -d '{"agent":"'$AGENT'","require_label":"auto"}'

# 2) 작업 중 30분마다 lease 연장 (TTL 기본 1h. 안 하면 만료 → 다른 agent가 회수)
curl -s -H 'content-type: application/json' -X POST /issues/ID/lease -d '{"agent":"'$AGENT'"}'

# 3) 진행 로그 (구분해서 짧게, 반복 가능)
curl -s -H 'content-type: application/json' -X POST /issues/ID/comments \
  -d '{"author":"'$AGENT'","body":"1단계 완료, 2단계 착수"}'

# 4) 완료 (결과 요약 한 줄 필수 권장) — lease 자동 소거
curl -s -H 'content-type: application/json' -X PATCH /issues/ID -d '{"state":"done"}'
```

## 엔드포인트

| method | path | body | 설명 |
|---|---|---|---|
| GET | `/health` | – | `{status:"ok"}` 서버 점검 |
| GET | `/work-contract` | – | `{version, instructions, report_required}` 공통 방법론과 완료 보고 정책 |
| POST | `/pull` | `{"agent":STR, "require_label"?STR, "hours"?1~6}` | todo+**lease만료 in_progress** 중 우선순위·생성순으로 하나 atomic 수령. require_label 지정 시 해당 라벨만. 없으면 `null`. 활성 lease 2건이면 409 |
| POST | `/issues` | `{title, body?, parent_id?, priority?1-4, labels?[STR], state?="todo"|"backlog"}` | 등록. parent_id 없으면 루트. 201 |
| POST | `/issues/{id}/claim` | `{"agent":STR, "hours"?1~6}` | id 지정 수령. todo+미배정만 가능, 아니면 409 |
| POST | `/issues/{id}/lease` | `{"agent":STR, "hours"?1~6}` | heartbeat. 보유자만(409), TTL 연장. 버전 올리지 않음 |
| GET | `/issues` | – | 필터: `?state=&parent=&label=&assignee=&q=&limit=200&archived=no` · `parent=none`은 루트만. `archived`: no(기본)/all/only |
| GET | `/issues/{id}` | – | `{...issue, children:[...], comments:[...]}` |
| GET | `/issues/{id}/tree` | – | 재귀 `{tree:[...]}` |
| PATCH | `/issues/{id}` | `{state?, title?, body?, parent_id?, labels?, priority?, assignee?, expected_version?, clear_parent?, clear_priority?, archived?, waiting_for?, waiting_actor?, blocked_detail?, completion_report?}` | 수정+전이. `expected_version` 주면 낙관적 락. `archived:true`로 done/cancelled 보관. `waiting_for`는 blocked 전이/blocked 상태에서만(`dependency|human|gate|external`), 이탈 시 사족 자동 소거. `force_done:true` = done 증거 게이트 우회(승인 경로) |
| POST | `/issues/{id}/verify` | `{verifier:STR, evidence:STR="", completion_report?, expected_version?}` | review 전용 완료 보고 접수(그 외 409). 성공 결과 필요(422), 회차/계약 충돌 409. 아래 완료 보고 규약 참고. 독립 검증을 뜻하지 않음 |
| POST | `/issues/{id}/comments` | `{author:STR, body:STR}` | 진행 로그 (agent 회신·질문도 이것 — 보드가 폴링하며 실시간 대화) |
| GET | `/issues/{id}/why-blocked` | – | blocked 사유 투영: `{gate{kind,issue,dispatch}, waiting_for, waiting_for_source, waiting_actor, blocked_detail, criteria, dependencies[{id,state}], missing, release_ready, evidence, next_commands[]}`. 비-blocked는 409. 필드 없으면 `waiting_for=` 코멘트에서 추정(source=comment) |
| GET | `/agents` | – | webhook 등록 agent 목록 |
| POST | `/agents` | `{name:STR, base_url:STR(http/s), secret?STR, enabled?=true, release_hook?=false, notify_hook?=false}` | agent 등록. 중복 409, 201. `release_hook:true` = reconcile release 명령 수신 capability(runner만). `notify_hook:true` = blocked(human) Level4 알림 수신 capability(알림 주입 어댑터용; TT_NOTIFY_BASE 설정 시 `/` 상대경로 허용) |
| PATCH | `/agents/{name}` | `{base_url?, secret?, enabled?, release_hook?, notify_hook?}` | 수정 |
| DELETE | `/agents/{name}` | – | 삭제 |
| POST | `/issues/{id}/dispatch` | `{agent:STR, message:STR, author?="board"}` | **지시(hook)**: message를 댓글 기록 후 agent `base_url`로 webhook POST(10s). 성공 시 dispatch 로그 반환, 실패는 `⚠ hook dispatch ... 실패` 시스템 댓글 자동 |
| GET | `/issues/{id}/dispatches` | – | 발송 이력 `{id,status:queued|ok|error,detail,context}` |

## agent 등록과 대화 (hook/callback)
### agent 통합 두 방식 (둘 중 하나 — dispatch 수신 전제)

| 방식 | 전제 | 지연 | 적합 |
|---|---|---|---|
| **hook** | `base_url` HTTP 수신기 상주 (아래 계약) | 즉시 | 상주 agent (opencode serve, 어댑터) |
| **polling** | 주기 `POST /pull` + 댓글 스레드만 | 주기 의존 | 크론성 agent — dispatch *자체*는 못 받고 todo/댓글 변경으로만 인지 |

수신기가 없으면 `dispatch`는 409성 실패가 아니라 **webhook 발송 실패(시스템 댓글)** 로 끝난다 — 크론 agent에게 지시하려면 dispatch 대신 todo 카드 + 댓글을 쓸 것. (polling agent용 dispatch 인박스는 로드맵: TT M 카드)


수동 수령(pull) 대신 **보드/이슈에서 직접 지시를 받는 상주 agent**가 되는 방법:

1. `POST /agents {name, base_url, secret?}` 등록 — name은 회신 댓글 `author`와 일치하게 (대화 스레드가 issue+agent 쌍으로 묶임)
2. dispatch 수신: 서버가 `base_url`로 POST. 헤더 `Authorization: Bearer <secret>`, `X-TT-Dispatch`
   ```
   {dispatch_id, issue_id, issue_title, agent, author, message,
    context, comments:[최근 20개 {author,body,ts}], tt_url}
   ```
3. **즉시** `200` 회신. 본문을 기다리지 말고 `{}` 또는 `{"context":"resume-token"}` 반환 — context는 같은 (issue,agent)의 다음 dispatch에 그대로 실려 오므로 자기 세션/스레드 이어붙이기에 사용
4. 실제 작업은 백그라운드로 하고, 결과·추가 질문은 `POST /issues/{id}/comments {author:<name>}`로 — 사용자가 보드에서 답하면 새 dispatch로 다시 도달 (대화 루프)
5. 200 아니면 즉시 실패 처리: dispatch 로그 error + 시스템 댓글. 타임아웃 10s

## blocked 사족 · why-blocked · reconcile release (M3BZS1FS-5722)

- blocked 전이 시 사족 선택 입력: `waiting_for ∈ {dependency, human, gate, external}` + `waiting_actor`(책임 액터) + `blocked_detail`(상세/의존 이슈 ID). 미입력 시 기존 동작 그대로(하위 호환). blocked 이탈 시 사족 자동 소거. CLI: `tt block ID human -a user@mini -d "스펙 확인"`
- `GET /issues/{id}/why-blocked` — 게이트·기준·누락·근거·권장 명령을 기계 판독으로 반환(코멘트/필드 기반 투영, 자동 생성 아님). CLI: `tt why ID`
- **reconcile release(실행 중지)**: dispatch로 실행을 위임한 카드가 done/cancelled로 terminalize되면, 서버가 `release_hook=true`인 agent의 `base_url`로 제어 명령을 보낸다. 헤더 `X-TT-Command: release`, 본문 `{command:"release", issue_id, dispatch_id?, reason, ts}`. runner는 해당 이슈의 살아있는 런(queued/running/held)을 장부 `cancelled`로 바꾸고 tmux 세션을 kill한다. 발송 결과는 `tt-server` 시스템 댓글로 기록, 실패해도 카드 전이는 되돌리지 않는다(best-effort)
- **의존 종료 표시**: `waiting_for=dependency` 카드가 기다리는 의존 이슈가 done/cancelled가 되면 blocked 카드에 `[release-ready]` 시스템 댓글(중복 없음)과 `release_ready:true`가 생긴다. **자동 재dispatch는 없다** — 사람이 `tt edit ID --state todo`로 재개 (Judge 계층 §11 별도 결정)

## 상태 기계 (위반 시 409)

```
backlog ──→ todo ──(pull/claim)──→ in_progress ──→ done
              │                        │  │           │
              │                        ↓  ↓ 증거없음  ↓ (재오픈)
              └────→ cancelled ←──── blocked  review ──(verify/승인)──→ done
```

- **todo에서 곧바로 done 불가** — pull/claim으로 수령 이력 만들고 in_progress→done
- todo→backlog 강등 가능 (미배정 상태로 되돌림)
- in_progress→todo = 반납 (assignee 자동 비움, 다른 agent가 수령 가능)
- done→todo 재오픈 가능, 완료 시각(completed_at)은 갱신
- **부모 done 가드**: 미완료(done/cancelled 아님) 자식이 하나라도 있으면 부모 done은 409. 자식을 먼저 종결하세요

## 규칙·함정

1. `pull` 응답 `null`이면 할 일 없는 것 — 종료할 것. 남의 in_progress 건드림 금지
2. **lease 규약**: lease 없는 카드는 수정 금지. 수령=lease 취득(1h). 30분마다 `POST /lease` 연장. **만료 lease는 누구든 pull로 회수(steal)** — 크론이 죽어도 카드는 1시간 뒤 자연 해금. release/done 시 lease 소거, release는 보유자만
3. **자동화(cron)는 `require_label` 필수**: `auto` 라벨이 붙은 카드만 자동 실행 대상. 휴먼 수동 pull만 라벨 없이 전체 대상. agent당 활성 lease 최대 2건(초과 409 — 독식 방지)
4. 락: 두 agent 동시 수령 시 서버가 서로 다른 이슈를 준다. PATCH에 `expected_version`(현재 v)를 넣면 남이 먼저 고쳤을 때 409 → 재fetch 후 재시도
5. 큰 작업은 부모 이슈를 만들고 `parent_id`로 하위 쪼갬. 진행은 하위 각각 done, 부모는 마지막 done의 근거 코멘트 후 마감
6. 코멘트는 결과물 링크/커밋 해시/실패 원인처럼 **다음이 이어받을 수 있게** 쓴다
7. 삭제 API는 없다 — 안 할 일이면 `state:"cancelled"`. 다 끝난 건은 `archived:true`(기본 목록·pull에서 숨김, `?archived=all`로 조회) — `tt archive ID|auto`(auto=완료 30일)

## cron 에이전트 템플릿 (hermes / codex / opencode 공용)

```
매 실행:   tt pull --label auto        # 없으면 즉시 종료
작업 중:   30분 간격 tt heartbeat ID
귀환:      성공 tt done ID "요약" / 실패 tt note ID "원인" + tt state ID todo(release)
절대 금지: lease 없는 카드 수정 · require_label 없는 자동 pull · lease 한도 우회 반복
```

## CLI가 있을 때 (`tt`가 PATH면 이게 더 빠름)

```
tt pull [--label auto]       tt heartbeat ID          tt note ID "로그"    tt done ID "요약"
tt new "제목" -P p1 -l infra [-p PARENT]        tt list [state]
tt show ID                 tt tree ID           tt state ID blocked
tt search "쿼리"           # 제목+본문 검색 (중복 이슈 확인에 먼저)
tt archive ID|auto         tt unarchive ID
tt agents                  tt agent add NAME URL [secret]   tt agent rm NAME
tt dispatch ID -A AGENT "지시" [-a author]   # 보드↔agent hook 대화의 CLI측
tt block ID KIND [-a 액터] [-d 상세]   # blocked+waiting_for (dependency|human|gate|external)
tt why ID                  # why-blocked: 게이트·기준·누락·해제가능·권장 명령
tt contract                # 공통 방법론 조회
tt done ID --report report.json
tt verify ID --report report.json  # review → done, 성공 보고 접수
tt agent release|notify NAME on|off  # 능력 플래그: release=종지 명령, notify=blocked(human) 알림 수신
```

## done≠verified 게이트 · blocked→human 알림 (M3BZV172-9F0S)

- 완료 시 `completion_report`를 우선 사용한다. TDD는 RED 명령·실패 결과와 GREEN 명령·결과가 필요하다. `alternative`는 대체 검증 사유가 필수다. 서버는 형식, 계약 버전, `execution_attempt`를 검사한다. 누락 422, 회차·계약 불일치 409. `failed`/`inconclusive` 보고는 저장하되 `review`로 남긴다.
- **보고 필수 모드**: 서버 시작 시 `TT_REQUIRE_REPORT=1`. 기본값 `0`은 기존 클라이언트의 성공 결과 코멘트 경로를 유지한다. 정책은 수령 시 계약에 고정되므로 설정 변경은 다음 claim/pull(또는 범위 변경)에 적용된다. 필수 모드에서는 `TT_DONE_GATE=warn|off`여도 보고 없이 완료할 수 없다. `/verify`와 UI의 텍스트 입력 역시 보고를 대신하지 못한다.
- 업그레이드 전 진행·검토 중이던 카드(`work_contract=null`, `execution_attempt=0`)는 기존 성공 결과 코멘트 경로로 마감할 수 있다. 이미 진행 중인 카드를 새 계약으로 옮기려면 `todo`로 돌린 뒤 다시 claim한다. 기존 회차에 새 보고 정책을 소급 강제하지 않는다.
- 호환 모드 코멘트는 현재 회차에서 `pytest 5 passed` 같은 성공 결과만 받는다. SHA·URL·빈 `증거:`·`pytest green`은 결과가 아니다. 최신 실패 문구가 있으면 과거 성공으로 돌아가지 않는다. 이 텍스트 판정은 보수적인 휴리스틱이며 TDD 절차 확인 수단은 아니다.
- 증거 부족 시 HTTP 200과 `.state="review"`, lease 반납. `review → todo|blocked|done|cancelled`가 가능하다. 재작업은 `todo → claim`으로 새 회차를 시작한다. 재오픈, 새 수령(만료 lease 회수 포함), 제목/본문 변경은 이전 보고·증거를 무효화한다. 완료 카드의 범위를 바꾸면 `review`로 돌아간다. 신규 카드는 todo/backlog만 허용한다.
- **승인 예외**: 기존 `close` 라벨 또는 `force_done:true`는 `verification_status="approved"`로 완료한다. 이것은 승인 경로 표시이며 승인자 신원 검증은 아니다. 이 API는 기존처럼 신뢰망 전용이며 actor 인증을 제공하지 않는다.
- `verification_status`: `unverified`(미확인), `reported`(성공 보고 접수), `approved`(승인 예외), `legacy`(이전 버전 완료 기록). 기존 `verified` 불리언은 호환용으로 남는다. `reported`는 테스트 실행이나 보고 내용이 독립적으로 검증되었다는 뜻이 아니다.

`report.json` 예시(값은 실제 실행 결과로 작성):

```json
{
  "contract_version": "<claim 응답의 work_contract.version>",
  "attempt": 1,
  "method": "tdd",
  "red_command": "pytest tests/test_retry.py",
  "red_evidence": "1 failed: duplicate notification",
  "command": "pytest tests/test_retry.py",
  "result": "passed",
  "evidence": "1 passed; artifacts/test-retry.log",
  "limitations": "실제 외부 알림 서비스는 검증하지 않음"
}
```

`attempt`는 수령 응답의 `execution_attempt`를 복사한다. CLI는 `tt done ID --report report.json`,
검토 대기는 `tt verify ID --report report.json`. API는 PATCH `{state:"done", completion_report:{...}}`
또는 POST verify `{verifier:"agent", completion_report:{...}}`. 대체 검증은 `method:"alternative"`와
`reason`을 넣고 RED 필드는 생략한다. 보고는 이슈의 `completion_report`에 보관된다.

계약은 `/api.md`의 공통 방법론 원문과 보고 정책을 해시한 버전으로 관리된다.
claim/pull 응답과 dispatch payload에 `work_contract`, `execution_attempt`가 추가된다.
CLI 수령의 일반 출력은 계약을 stderr에, `--json`은 JSON 안에 전달한다.
러너는 신규·재개 실행의 실제 프롬프트에 계약을 넣으며, 기존 `#opts` 해석과 원래 메시지는 유지한다.

- 호환 모드에서 보고 없는 요청의 처리: `TT_DONE_GATE=gate`(기본) | `warn`(미확인 done 허용+경고 댓글) | `off`. 보고 필수 계약과 명시적 failed/inconclusive 보고는 이 설정으로 우회되지 않는다.

### B. blocked(waiting_for=human) → 모바일 알림 (TT→Hermes 주입, 자체 APNs 없음)

- blocked 전이(또는 사족 보강)로 `waiting_for=human`이 되면 `notify_hook=true` agent에게 Level4 알림을 보낸다: 헤더 `X-TT-Command: notify`, 본문 `{command:"notify", issue_id, reason, text, ts}` — `text`가 **A/B 선택지 + recommendation** 템플릿(§11 Level4):
  ```
  ⏸ TT blocked(Level4) — <제목>
  이슈: <id>  waiting_for=human  액터: <waiting_actor>
  사유: <blocked_detail>
    [A] 중단 — 카드를 todo로 되돌려 담당 변경  → `tt edit <id> --state todo`
    [B] 답변/결정 후 재개 — tt note로 결정 기록 후 todo  → `tt note <id> "<결정>" && tt edit <id> --state todo`
  권장: B — waiting_for=human: 책임 액터의 결정이 필요 — 답변 기록 후 재개 권장
  ```
  recommendation은 기계 생성(human→B, dependency→의존 상태 따른 A/B, 기타→A 유지) — Judge/Reasoner 두뇌 계층은 범위 밖(별도 이슈).
- Hermes 쪽은 이 webhook을 받아 기존 Telegram 경로로 주입한다(tt-bridge 어댑터가 notify 수신 등록). 알림 실패는 서버 로그+`[level4-notify]` 시스템 댓글로 관찰, blocked 전이는 되돌리지 않는다(best-effort).
- **1회 보장**: `blocked_notified_at` 필드로 dedup — 같은 blocked 에피소드 내 중복 코멘트·재PATCH 재발송 없음. blocked→todo 이탈 시 리셋되므로 '새 대기'는 새 알림 1회. legacy runner(무수정)는 `BLOCKED ... waiting_for=human` 코멘트 마커만으로 발동된다.


## 문제해결

- 연결 안 됨 → 서버 reachability 확인. `tt health` 실패 → 서버 프로세스(launchd/systemd) 상태 확인
- machine-readable 스펙이 더 필요하면: `/openapi.json` (OpenAPI 3), 스웨거 UI `/docs`
- 백업: `sqlite3 tt.db "VACUUM INTO snapshot"` 를 주기 크론으로
