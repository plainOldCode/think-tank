# TT 지표 2주 실험 설계 + 대시보드 (v4)

- 카드: TT M4FFCB9J-2AAB ([TT 개선#3e] 2주 실험 설계 + 지표 대시보드 초안)
- 작성: 2026-10-09, 수령자 hermes@m2max
- 계약: tt-tdd-v2.1 고정 — 본 문서는 method=planned 설계 산출물이며, 수집기(`scripts/metrics_collect.py`)는 라이브 서버 실측으로 검증한다.
- v4: 3차 독립 리뷰(PR#66@45eb208, R1~R4)의 창-경계·집계 결함을 반영했다 — 제출 회차 종료의 창 무관화, 120분 복구율 분모 수정, 반납 카운트 창 필터, todo 진입 시각의 메타데이터 덮어쓰기 방지. 1차(측정 계약)·2차(이벤트 재구성) 리뷰 반영분은 §2에 흡수됐다.
- 원칙: **카드 생성 수는 지표가 아니다**(카드 본문 기준). 운영 품질 지표 5종만 다룬다.

## 0. 결론

| 항목 | 상태 |
| --- | --- |
| 지표 5종 정의·수집 방법 | 본 문서 §2 — 원시 소스는 서버 실측(코드+API)으로 매핑 완료 |
| 측정 창구 | `scripts/metrics_collect.py` v4 — 라이브 TT 서버 실측 성공(§4), 리뷰어 독립 재구성과 정확히 일치(§6) |
| 대시보드 초안 | §4.2 (markdown 표) — 스냅샷 파일 적재 후 추세 비교로 확장 |
| 2주 실험 설계 | §5 — 기간·측정 주기(30분)·판정 기준(발령자 비준 대상)·중단 기준 |
| 기준선 (2026-10-10 v4 실측) | 착수 p50 7.8분(p90 3.8시간, 코호트 13) / 리뷰통과 64.7%(11/17) / 재작업 27.6% / 반납 4회 / stall 5열림 |

## 1. 원시 데이터 소스 (서버 실측 매핑)

TT 서버의 무엇이 측정 가능한지 2026-10-09 라이브 실측(서버 코드 대조 포함)으로 확정했다.

| 소스 | 실측 내용 | 지표 연결 |
| --- | --- | --- |
| `GET /issues?limit=N` | 카드 스냅샷 — `created_at` `started_at` `completed_at` `verified_at` `execution_attempt` `todo_since` `heartbeat_at` `blocked_notified_at` `lease_expires` `waiting_actor` | stall 판정·재작업 attempt·코호트 보조 |
| `GET /events?after_seq=&limit=` | 이벤트 로그(커서 페이지네이션) — `issue.created`(payload에 state 포함) `issue.updated` `comment.added` `dispatch.created` `dispatch.updated` `message.posted`. `issue.updated` payload는 `state`(**변경 후 전체 상태**) + `fields`(변경 필드 목록). **단, `/pull` 수령 이벤트는 `{state:in_progress, assignee, via:pull}`로 fields가 없다**(server/routers/issues.py:228-253 실측) | 제출/검증 페어링·반납 출발 상태·blocked 진입·개입·**pull 수령** |
| `GET /issues/{id}/dispatches` | `agent` `attempt` `status` `run_state` `started_at` `ended_at` `last_progress_at` | 재작업 정밀화(실험 기간 확장) |
| 서버 코드 계약 | 완료 보고 제출 = `state→review` + `completed_at` **필드 변경**(값은 null) + `verified_at` 기록(server/routers/issues.py:309-329). 최종 verify = `state→done` + `completed_at` 기록(동문:508). `/lease`·`/ping`은 SQL만 갱신하고 **이벤트를 남기지 않음**(issues.py:181-203, 라이브 event delta=0 실측) | 페어링·스냅샷 기반 복구 추적 |

이벤트 로그는 **2026-10-09 13:38 (#2 배포 직후)부터 축적**된다(실측 seq 1). 이벤트 기반 지표는 실험 창(2주) 안에서만 유효하고, 로그 시작 이전 이력은 원시 근거가 없어 집계에서 제외하고 별도 표기한다.

**재구성과 집계의 분리(3차 리뷰 확정 원칙)**: 상태 복원·제출 회차 관리는 **항상 로그 처음(seq 1)부터** 수행하고, **카운트만** 창(`--window-start`)으로 제한한다. 창 이전 이벤트를 건너뛰면 직전 상태·제출 회차가 왜곡되고, 회차 종료를 창 조건 안에 넣으면 "창 이전 제출→반납→재수령→force_done"이 이전 제출과 오페어링된다.

## 2. 지표 5종 — 정의와 수집 방법 (v4 계약)

### 2.1 착수시간 (time-to-start)

- **정의**: 카드가 **todo에 진입한 시점 → 첫 수령 시점**.
- **첫 수령 인식(v3)**: 첫 `state→in_progress` 이벤트 — **claim과 /pull 경로 공통**. v2는 started_at 필드 이벤트만 봐서 정상 /pull 수령이 코호트에서 빠졌다(2차 리뷰 R4, 로컬 재현: todo 생성 30분 후 /pull → cohort_n=0).
- **페어링 동결(v3)**: 첫 수령 시점 이후 todo_entry를 갱신하지 않는다 — 첫 pull→반납→재claim 시나리오에서 v2가 나중 claim을 첫 수령으로, 반납 시각을 todo 진입으로 잘못 잡은 결함의 수정.
- **todo 진입 갱신 조건(v4)**: todo_entry는 **실제 todo 진입**(fields에 state가 있는 전이)과 **todo 생성**에서만 갱신한다. v3는 갱신을 fields 조건 밖으로 빼서 todo 카드의 priority/title 수정(payload.state=todo, fields=[priority])이 진입 시각을 덮어썼다 — "생성 0분→수정 20분→pull 30분"이 30분이 아니라 10분으로 집계되는 회귀(3차 리뷰 R4).
- **todo 진입 시각의 원시 소스**: ① `state→todo` 실제 전이 이벤트, ② 생성 시 state=todo였으면 `issue.created` 이벤트 ts(payload에 state 실측 확인), ③ 로그 시작 이전 출생 카드는 created_at **추정치**로 별도 표기(source=pre_log_estimated).
- **코호트**: 실험 창 시작 이후 todo에 진입한 카드만 p50/p90 대상. 코호트 원시 행(id·진입·수령·분·소스)을 JSON에 보존해 사후 재집계 가능하게 한다.
- **카드 생성 수와의 관계**: 생성 수는 세지 않는다.

### 2.2 리뷰통과율 (review pass rate)

- **정의**: **완료 보고 제출**(state→review + completed_at 필드) 대비 **최종 검증 통과**(state→done + completed_at 필드) — 이벤트를 시간순으로 **인스턴스 단위 페어링**해 계산한다.
- **제출 인스턴스 FIFO(v3)**: 카드별 미검증 제출을 `(seq, ts)` 목록으로 보존한다. v2는 `submissions[eid]=ts`로 마지막 제출만 남겨 "제출→반납→재제출→검증" 사이클의 이전 제출·통과를 삭제했다(2차 리뷰 R1).
- **검증 페어링**: state→done+completed_at 이벤트가 오면 **가장 오래된 미검증 제출**과 짝짓는다.
- **회차 종료 규칙(v3·v4)**: ① 반납(state→todo 실제 전이) 시 해당 카드의 미검증 제출을 전부 종료(reworked), ② **대체 제출**(review 상태에서 재제출) 시 이전 회차를 종료(superseded). **종료는 창 무관** — v3는 종료를 창 조건 안에 넣어 "창 이전 제출→반납→재수령→force_done"이 이전 제출과 페어링되어 passed=1/직행=0 오분류를 만들었다(3차 리뷰 R1). 집계 카운트만 창으로 제한한다.
- **동초 이벤트(v3)**: 제출/검증 구별을 (id, ts) 집합이 아니라 **payload 상태값(st)**으로 한다 — 같은 초의 제출+검증 쌍(라이브 seq 51/54, 112/116 등 7쌍 실측)도 상태값이 다르므로 정확히 분류된다.
- **창 필터(v3·v4)**: 제출·통과·반납·**직행** 카운트 모두 이벤트 ts ≥ 창 시작만 분모에 넣는다(v4에서 직행도 창 필터). 창 이전 제출이 창 내 검증되면 passed에 세되 `passed_with_pre_window_submit`로 분리 표기.
- **보조 분해**: 대기(제출 후 미검증, 스냅샷 review), 반납(reworked), 대체(superseded), 직행(제출 없는 done — close 라벨/force_done 승인 경로)을 별도 카운트. 직행은 통과율 분자·분모에서 제외.
- **검증 지연**: 페어링된 (검증 ts − 제출 ts)의 p50 — 라이브에서 동초 쌍이 다수라 0분이 나올 수 있다(실측).

### 2.3 재작업률 (rework rate)

- **정의**: 수령 이력이 있는 카드 중 `execution_attempt ≥ 2` 비율(스냅샷) + **review→todo 반납** 횟수(이벤트).
- **이전 상태 추적(v3)**: `last_state`를 **fields 유무와 무관하게 모든 state-bearing 이벤트에서 갱신**한다. v2는 fields에 state가 있을 때만 갱신해 `/pull` 이벤트(fields 없음, §1)를 상태 관측에서 누락했다(2차 리뷰 R3).
- **전이 카운트**: `fields`에 state가 있는 **실제 전이 이벤트만** 카운트하고 출발 상태는 직전 관측값으로 판정. 로그 시작 이전 이력으로 직전 상태를 모르면 unknown_prev로 분리.
- **창 필터(v4)**: 반납·출발 상태 **카운트 자체**를 창 안에서만 누적한다. v3는 todo_into 누적이 창 검사 밖에 있어 "이전 주 반납 1회+창 내 1회 → 2회", "이전 주 review 반납+창 내 in_progress→todo → review 반납 1"이 나왔다(3차 리뷰 R3). 상태 복원은 로그 전체(§1 원칙), 카운트만 창 내.
- **한계**: probe review-fix dispatch 404(미해결 버그)로 반납이 사람 몫이 되는 기간의 왜곡은 limitations에 기록한다.

### 2.4 stall복구율 (stall recovery)

- **stall 판정(스냅샷)**: in_progress에서 heartbeat 정지(기준 60분) 또는 lease 만료. **review는 병합 대기 정상 상태라 heartbeat으로 오탐하지 않고 lease 실제 만료만** (review 카드는 heartbeat이 갱신되지 않는다 — 실측).
- **episode 추적**: `/lease`·`/ping`은 이벤트를 남기지 않으므로(실측) 수집기가 **영속 상태 파일**로 episode를 누적한다: stall로 관측되면 episode 개시(first_seen), 이후 실행에서 stall 집합에서 빠지면 해소.
- **해소 결과 분류(v3)**: "카드가 존재하기만 하면 복구"가 아니라 해소 시점 카드 상태로 결과를 분류한다: **recovered**(in_progress/review/done — 진행 재개 또는 정상 종료), **returned**(todo 반납), **blocked**, **missing**(관측 소실), **other**(backlog/cancelled 등). 복구율 분자는 recovered뿐. 해소 행에 이슈 식별자를 보존한다.
- **이중 복구율(v3·v4)**: 대시보드에 ①전체 해소분 복구율과 ②**§5 판정용 120분 이내 복구율**을 함께 출력한다. **분모는 시간값이 유효한 전체 해소분, 분자는 recovered이면서 120분 이내** — v3는 분모를 `<=120` 필터로 좁혀 느린 복구(180분)가 판정에서 사라져 "30분 복구 1+180분 복구 1 → 100%"로 과대평가됐다(기대 50% — 3차 리뷰 R2). 열린 episode censoring(`episodes_open`)은 별도 표기.
- **관측 주기**: 판정 목표(80%가 2시간 내 복구)보다 짧은 **30분 크론**. 6시간 스냅샷으로는 그 사이 발생·복구된 episode를 놓친다.
- **censoring**: 실험 종료까지 열려있는 episode는 우절단 — 분모에서 제외하고 `episodes_open`으로 별도 표기.
- **보유 lease 예외**: 사람 작업 카드에 더해, **오케스트레이터·브리지가 보유한 lease**(예: Kanban 그래프 루트 클레임)도 TT heartbeat이 없어 stall 오탐이다 — 라이브에서 M4GD1NPN(오케스트레이터 클레임)·M4GB403A·M4GAW92V가 오탐된 실측. 1주차에 예외 규칙(lease_by 화이트리스트)을 확정한다.

### 2.5 개입횟수 (human intervention)

- **정의**: 사람 액터(`skshim*`)의 comment.added / message.posted 원시 카운트 + **blocked 실제 진입** 횟수.
- **blocked 진입**: payload의 state는 변경 후 전체 상태라 blocked 카드의 priority/title 수정도 state=blocked다 → `fields`에 state가 있고 **직전 상태가 blocked가 아닐 때**만 센다 (라이브 재현: 진입 1회+메타데이터 수정 2회 → 1회로 집계 확인).
- **v1 proxy 표기**: 자동 blocked와 사람 개입을 구별할 수 없어 카운트는 proxy다 — 출력과 문서에 명시. 발령자의 정상 발령(dispatch.created)은 개입에서 제외.

## 3. 측정 창구 — 수집기 v4

`scripts/metrics_collect.py` (stdlib-only, 의존성 없음):

- `GET /issues?limit=1000` 스냅샷 + `GET /events?after_seq=` 커서 전수 수집(seq 정렬 보장).
- **Python 3.9 호환**: `parse_ts`가 'Z' 접미와 **콜론 없는 ±HHMM 오프셋**(`+0900` — 서버 db.now()/future()의 %z 형식, server/db.py:139-144)을 모두 정규화한다. 3.9의 `fromisoformat`은 둘 다 ValueError라 cron 배포가 죽는다 — 3.9.6 라이브 실행 확인(303카드·373이벤트 exit 0). cron은 3.9+ 어디서든 실행 가능.
- `--state-file`: stall episode 영속 상태 파일(실행 호스트, Git 밖). 행에 이슈 식별자와 해소 결과가 보존된다 — **전체 원시 상태 파일이 복구율 재현의 1차 근거**다(resolved 행의 outcome·minutes_open).
- `--window-start`: 실험 창 시작(기본 이벤트 로그 시작). 재구성은 로그 전체, 집계는 창 내(§1 원칙).
- `--json`: 코호트 원시 행·episode 해소 행 포함 기계 판독 출력. 기본: markdown 대시보드.
- 운영 서버 주소는 레포에 기록하지 않는다 — `TT_URL` 환경변수 또는 `--base`로 전달(secret-scan 통과).
- cron 제안: **30분 주기** — 스냅샷 JSON을 실행 호스트의 Git 밖 디렉터리에 적재(예: `~/tt-metrics/YYYYMMDD-HHMM.json`). 데이터 공백 48시간 초과 시 실험 중단 기준(§5).

## 4. 라이브 실측 (2026-10-10 00:50 KST, v4 기준선)

### 4.1 수집기 실행 결과

- 스캔: 카드 303 / 이벤트 359 (창 시작 = 로그 시작 13:38:33)
- 착수시간: **p50 7.8분 / p90 3.8시간** — 코호트 n=13(실험 창 내 **실제 todo 진입** 코호트, claim·pull 경로 공통). v3의 n=14는 todo 카드 메타데이터 수정이 진입 시각을 덮어써 창 내로 잘못 포함시킨 1건을 포함한 수치였다(3차 리뷰 R4).
- 리뷰통과율: **64.7% (11/17)** — 반납 4, 대체 1, 직행 0(창 내), 대기 0, 검증 지연 p50 0분(동초 쌍 다수 — 실측). v2의 33.3%는 제출 인스턴스 소실로 과소 계산된 수치였다.
- 재작업률: **27.6% (63/228, attempt≥2)** — review→todo 반납 **4회**(창 내), todo 진입 출발 상태 {in_progress 2, backlog 6, unknown_prev 2}.
- stall: **5 episode 열림**(M4GD1NPN-QFDE·M4GB403A-E4XC·M4GAW92V-DW9A는 보유 lease 오탐 — §2.4 예외 규칙 1주차 확정 대상), 복구 0(누적 시작).
- 개입: 사람 코멘트 18 / 메시지 0 / blocked 진입 0 (v1 proxy).

### 4.2 대시보드 초안 (수집기 출력 그대로)

```
## TT 지표 대시보드 (v4)
- 생성: 2026-10-10T00:50 KST | 창 시작: 2026-10-09T13:38:33+09:00 | 스캔: 카드 303 / 이벤트 359

| 지표 | 값 | 근거 |
|---|---|---|
| 착수시간 p50 (todo진입→첫수령, 창 내 코호트) | 7.8분 | n=13 (제외 0) |
| 착수시간 p90 | 3.8시간 | n=13 |
| 리뷰통과율 (제출→검증 인스턴스 페어링) | 64.7% | 11/17 (반납 4, 대체 1, 직행 0, 대기 0) |
| 검증 지연 p50 | 0분 | n=11 (동초 쌍 다수) |
| 재작업률 (attempt≥2) | 27.6% | 63/228 |
| review→todo 반납 (창 내) | 4회 | events(이전 상태 추적) |
| todo 진입 출발 상태 | {"in_progress": 2, "backlog": 6, "unknown_prev": 2} | events |
| stall 현재/episode | 5열림/5 active | 복구 0 · 미복구 0 |
| stall복구율 전체 / 120분 이내 (판정 기준) | - / - | 상태 파일 누적 |
| 개입: 사람 코멘트/메시지/blocked 진입 | 18/0/0 | events (v1 proxy) |

stall 상세: M4GD1NPN-QFDE, M4GB403A-E4XC, M4GAW92V-DW9A, M4DEFW03-QTEN, M32909TN-SJ11
```

확장 방향: 스냅샷 파일 적재 → 주간 추세(증감) → 리뷰통과율·재작업률을 워커별 분해. 초안 단계에서는 단일 시점 표로 충분하다.

## 5. 2주 실험 설계

- **기간**: 2026-10-10 00:00 KST → 2026-10-24 00:00 KST (14일).
- **측정 주기**: 수집기 **30분 크론** — episode 누적 상태 파일 유지(같은 호스트·같은 --state-file). 6시간 주기는 stall 복구 판정에 불충분.
- **기준선**: §4.1 (2026-10-10 v4 실측).
- **판정 기준 (발령자 비준 대상 — 제안)**:
  1. 착수시간: 창 내 코호트 p50 **30분 이하 유지**(기준선 7.8분 — #3c 교차리뷰 효과 관측). p90 3.8시간도 함께 추적.
  2. 리뷰통과율: 제출 대비 **80% 이상**(기준선 64.7% — 2주 누적으로 안정화). 직행(done) 비중은 10% 이하 권장.
  3. 재작업: **review→todo 반납 주당 ≤ 3회**(기준선 4회/1일 — 초기 지표). attempt 기반 비율은 추세 참고.
  4. stall복구율: **120분 이내 복구율 80% 이상** — 분모는 시간값이 유효한 전체 해소분, 분자는 recovered ∧ ≤120분(§2.4). 관측 주기 30분 전제. 사람·오케스트레이터 보유 lease 예외 규칙 1주차 확정 후 적용.
  5. 개입횟수: 1주차 관측만(정의 정밀화), 2주차에 주당 기준 확정.
- **중단 기준**: 측정 공백 48시간 초과, 또는 소스 스키마 변경(events/issues 필드)으로 수집기 실패 시 — 재설계 후 재개.
- **종료 산출물**: 스냅샷 시계열 요약 보고(카드 코멘트) + 대시보드 개선안 → TT 개선 #3(M4DEFW03-QTEN) 종결 판단 자료.

## 6. 검증

- **리뷰어 독립 재구성과의 정합(최우선 근거)**: 2차 리뷰(PR#66@b4575cc)가 라이브 seq 1~327로 독립 재구성한 값 — 제출 15건 / 페어링 검증 11 / 반납 3 / 대체 1 / 직행 0, todo 출발 상태 {in_progress 2, unknown 2, backlog 6}. v3·v4 수집기를 동일 이벤트 캡(seq≤327)으로 실행한 결과 **전 항목 정확히 일치**한다. 3차 리뷰(PR#66@45eb208)도 v3에서 이 정합을 독립 확인했다("문서 §6 대조는 정확합니다"). v2는 같은 데이터에서 제출 12/통과 4/반납 1, 출발 {in_progress 1, unknown 3}로 어긋났다.
- **3차 리뷰 경계 재현(v4 자체 검증)**: 리뷰가 제시한 4개 결정적 시나리오를 합성 이벤트로 재현해 전부 통과 — ①창 이전 제출→반납→재수령→force_done: passed=0/직행=1, ②30분 복구+180분 반납: 120분 복구율 50%(분모 2), ③이전 주 반납+창 내 반납: returns=1(출발 분리 정확), ④생성→메타데이터 수정→pull: 30분(덮어쓰기 없음). 동초 제출·검증 쌍 회귀도 통과.
- 수집기 v4 라이브 실측: §4.1 — 실서버 303카드·359이벤트 스캔, 5종 지표 산출.
- Python 3.9 파싱: `/usr/bin/python3`(3.9.6)에서 'Z' 접미·**콜론 없는 +0900 오프셋**(서버 %z 형식)·분수 초 파싱 확인 + **3.9 라이브 수집기 실행 exit 0**(303카드·373이벤트) — cron 배포 블로커 해소(4차 리뷰 Python3.9_compat).
- 4차 리뷰 R1~R4: **전부 resolved** — 리뷰어가 합성 12경계+로컬 API 2건+이전 24재현 38/38 통과로 독립 확인.
- 3차 리뷰 R1~R4 해소 대응: R1 회차 종료 창 무관화+직행 창 필터, R2 120분 분모=시간 유효 전체 해소분, R3 반납·출발 카운트 창 필터, R4 todo_entry 실제 진입만 갱신. 1·2차 리뷰 반영분(R5 포함) 유지.
- 시크릿: 자격증명 없는 공개 엔드포인트만 호출, 운영 주소 미기록(secret-scan 통과).
- 이 문서의 수치는 2026-10-10 기준선이며, 실험 데이터는 Git 밖 스냅샷으로 적재한다.
