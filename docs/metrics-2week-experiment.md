# TT 지표 2주 실험 설계 + 대시보드 (v2)

- 카드: TT M4FFCB9J-2AAB ([TT 개선#3e] 2주 실험 설계 + 지표 대시보드 초안)
- 작성: 2026-10-09, 수령자 hermes@m2max (attempt 4)
- 계약: tt-tdd-v2.1 고정 — 본 문서는 method=planned 설계 산출물이며, 수집기(`scripts/metrics_collect.py`)는 라이브 서버 실측으로 검증한다.
- v2: 독립 리뷰(TT 코멘트 PR#66@5d2d2de 결정표 R1~R5)의 측정-계약 결함 5건을 반영한 재설계다. v1의 오류(스냅샷 completed_at 분모, created_at 기반 착수시간, 이전 상태 미확인 반납 카운트, stall 복구 미구현, blocked 오탐)와 수정 내용은 §2 각 항목에 기록했다.
- 원칙: **카드 생성 수는 지표가 아니다**(카드 본문 기준). 운영 품질 지표 5종만 다룬다.

## 0. 결론

| 항목 | 상태 |
| --- | --- |
| 지표 5종 정의·수집 방법 | 본 문서 §2 — 원시 소스는 서버 실측(코드+API)으로 매핑 완료 |
| 측정 창구 | `scripts/metrics_collect.py` v2 — 라이브 TT 서버 실측 성공(§4) |
| 대시보드 초안 | §4.2 (markdown 표) — 스냅샷 파일 적재 후 추세 비교로 확장 |
| 2주 실험 설계 | §5 — 기간·측정 주기(30분)·판정 기준(발령자 비준 대상)·중단 기준 |
| 기준선 (2026-10-09 v2 실측) | 착수 p50 8분(p90 3.8시간, 코호트 13) / 리뷰통과 33.3%(4/12, 대기 1) / 재작업 27.6% / 반납 2회 / stall 3·6 |

## 1. 원시 데이터 소스 (서버 실측 매핑)

TT 서버의 무엇이 측정 가능한지 2026-10-09 라이브 실측(서버 코드 대조 포함)으로 확정했다.

| 소스 | 실측 내용 | 지표 연결 |
| --- | --- | --- |
| `GET /issues?limit=N` | 카드 스냅샷 — `created_at` `started_at` `completed_at` `verified_at` `execution_attempt` `todo_since` `heartbeat_at` `blocked_notified_at` `lease_expires` `waiting_actor` | stall 판정·재작업 attempt·코호트 보조 |
| `GET /events?after_seq=&limit=` | 이벤트 로그(커서 페이지네이션) — `issue.created`(payload에 state 포함) `issue.updated` `comment.added` `dispatch.created` `dispatch.updated` `message.posted`. `issue.updated` payload는 `state`(**변경 후 전체 상태**) + `fields`(변경 필드 목록) | 제출/검증 페어링·반납 출발 상태·blocked 진입·개입 |
| `GET /issues/{id}/dispatches` | `agent` `attempt` `status` `run_state` `started_at` `ended_at` `last_progress_at` | 재작업 정밀화(실험 기간 확장) |
| 서버 코드 계약 | 완료 보고 제출 = `state→review` + `completed_at` **필드 변경**(값은 null) + `verified_at` 기록(server/routers/issues.py:309-329). 최종 verify = `state→done` + `completed_at` 기록(동문:508). `/lease`·`/ping`은 SQL만 갱신하고 **이벤트를 남기지 않음**(issues.py:181-203, 라이브 event delta=0 실측) | R1 페어링·R2 스냅샷 기반 복구 추적 |

이벤트 로그는 **2026-10-09 13:38 (#2 배포 직후)부터 축적**된다(실측 seq 1). 이벤트 기반 지표는 실험 창(2주) 안에서만 유효하고, 로그 시작 이전 이력은 원시 근거가 없어 집계에서 제외하고 별도 표기한다.

## 2. 지표 5종 — 정의와 수집 방법 (v2 계약)

### 2.1 착수시간 (time-to-start)

- **정의**: 카드가 **todo에 진입한 시점 → 첫 수령 시점**. (v1 오류: created_at→started_at은 backlog 체류를 포함해 #3d self 카드의 대기를 착수 지연으로 왜곡했다 — R4.)
- **todo 진입 시각의 원시 소스**: ① `state→todo` 이벤트(승격·반납), ② 생성 시 state=todo였으면 `issue.created` 이벤트 ts(payload에 state 실측 확인), ③ 로그 시작 이전 출생 카드는 created_at **추정치**로 별도 표기(source=pre_log_estimated).
- **첫 수령 시각**: 첫 `started_at` 필드 이벤트. (`todo_since`는 claim에서 소거되므로 사용 불가 — 실측.)
- **코호트**: 실험 창 시작 이후 todo에 진입한 카드만 p50/p90 대상. 코호트 원시 행(id·진입·수령·분·소스)을 JSON에 보존해 사후 재집계 가능하게 한다.
- **카드 생성 수와의 관계**: 생성 수는 세지 않는다.

### 2.2 리뷰통과율 (review pass rate)

- **정의**: **완료 보고 제출**(state→review + completed_at 필드, §1 서버 계약) 대비 **최종 검증 통과**(state→done + completed_at 필드) — 이벤트를 시간순으로 페어링해 계산한다. (v1 오류: 스냅샷 completed_at을 분모로 써서 대기 보고가 누락되고 verify 지연이 0분으로 나왔다 — R1. verified_at은 보고 접수 때도 기록되므로 스냅샷으로 접수/통과를 구별할 수 없다.)
- **보조 분해**: 대기(제출 후 아직 done 아님), 반납(제출 후 review→todo로 미검증 종료), 직행(done인데 제출 이벤트 없음 — close 라벨/force_done 승인 경로)을 별도 카운트. 직행은 통과율 분자·분모에서 제외하고 투명하게 표기한다.
- **검증 지연**: 페어링된 (검증 ts − 제출 ts)의 p50.

### 2.3 재작업률 (rework rate)

- **정의**: 수령 이력이 있는 카드 중 `execution_attempt ≥ 2` 비율(스냅샷) + **review→todo 반납** 횟수(이벤트).
- **이전 상태 추적**(R3): 이벤트 seq 순서로 카드별 직전 상태를 추적해, 직전이 review일 때만 반납으로 센다. v1은 모든 state=todo 변경을 세서 backlog 승격·blocked 해제·in_progress 반납까지 재작업으로 잘못 셌다(라이브 6회 중 실제 review 반납은 2회 — 리뷰 독립 재구성과 일치). 로그 시작 이전 이력으로 직전 상태를 모르면 unknown_prev로 분리한다.
- **한계**: probe review-fix dispatch 404(미해결 버그)로 반납이 사람 몫이 되는 기간의 왜곡은 limitations에 기록한다.

### 2.4 stall복구율 (stall recovery)

- **stall 판정(스냅샷)**: in_progress에서 heartbeat 정지(기준 60분) 또는 lease 만료. **review는 병합 대기 정상 상태라 heartbeat으로 오탐하지 않고 lease 실제 만료만** (v2 실측에서 드러난 규칙 — review 카드는 heartbeat이 갱신되지 않는다).
- **episode 추적**(R2): `/lease`·`/ping`은 이벤트를 남기지 않으므로(실측) 이벤트만으로 복구를 볼 수 없다 → 수집기가 **영속 상태 파일**로 episode를 누적한다: stall로 관측되면 episode 개시(first_seen), 이후 실행에서 stall 집합에서 빠지면 해소 — 해소 시점에 카드가 진행/종료 상태면 **복구**. 복구 소요는 관측 주기 상한(±run interval)을 가진다.
- **관측 주기**: 판정 목표(80%가 2시간 내 복구)보다 짧은 **30분 크론**. 6시간 스냅샷으로는 그 사이 발생·복구된 episode를 놓친다(R2 지적).
- **censoring**: 실험 종료까지 열려있는 episode는 우절단 — 복구율 분모에서 제외하고 `episodes_open`으로 별도 표기. 복구율 = recovered/(recovered+closed_unrecovered).
- **사람 보유 lease 예외**: 발령자 작업 카드(예: 에픽 루트)는 stall 오류다 — 1주차에 예외 규칙 확정.

### 2.5 개입횟수 (human intervention)

- **정의**: 사람 액터(`skshim*`)의 comment.added / message.posted 원시 카운트 + **blocked 실제 진입** 횟수.
- **blocked 진입**(R5): payload의 state는 변경 후 전체 상태라 blocked 카드의 priority/title 수정도 state=blocked다 → `fields`에 state가 있고 **직전 상태가 blocked가 아닐 때**만 센다 (라이브 재현: 진입 1회+메타데이터 수정 2회 → v1은 3회 오류).
- **v1 proxy 표기**: 자동 blocked와 사람 개입을 구별할 수 없어 카운트는 proxy다 — 출력과 문서에 명시. 발령자의 정상 발령(dispatch.created)은 개입에서 제외.

## 3. 측정 창구 — 수집기 v2

`scripts/metrics_collect.py` (stdlib-only, 의존성 없음):

- `GET /issues?limit=1000` 스냅샷 + `GET /events?after_seq=` 커서 전수 수집(seq 정렬 보장).
- `--state-file`: stall episode 영속 상태 파일(실행 호스트, Git 밖). `--window-start`: 실험 창 시작(기본 이벤트 로그 시작).
- `--json`: 코호트 원시 행·episode 해소 행 포함 기계 판독 출력. 기본: markdown 대시보드.
- 운영 서버 주소는 레포에 기록하지 않는다 — `TT_URL` 환경변수 또는 `--base`로 전달(secret-scan 통과).
- cron 제안: **30분 주기** — 스냅샷 JSON을 실행 호스트의 Git 밖 디렉터리에 적재(예: `~/tt-metrics/YYYYMMDD-HHMM.json`). 데이터 공백 48시간 초과 시 실험 중단 기준(§5).

## 4. 라이브 실측 (2026-10-09 23:12 KST, v2 기준선)

### 4.1 수집기 실행 결과

- 스캔: 카드 302 / 이벤트 317 (창 시작 = 로그 시작 13:38:33)
- 착수시간: **p50 8분 / p90 3.8시간** — 코호트 n=13(실험 창 내 todo 진입), 제외 0. (v1의 5.1시간/30시간은 backlog 체류를 포함한 왜곡치였다 — R4)
- 리뷰통과율: **33.3% (4/12)** — 대기 1(진행 중 보고), 반납 0, 직행 0, 검증 지연 p50 2분. (v1의 95.9%는 스냅샷 필드 오용 — R1)
- 재작업률: **27.6% (63/228, attempt≥2)** — review→todo 반납 **2회**, todo 진입 출발 상태 {backlog 6, in_progress 1, unknown 3}. (v1의 "6회 전부 반납"은 오류 — R3)
- stall: 현재 3/6 active — episode 열림 3, 복구/미복구 0(누적 시작). (review 병합 대기 오탐 제거 — §2.4)
- 개입: 사람 코멘트 17 / 메시지 0 / blocked 진입 0 (v1 proxy).

### 4.2 대시보드 초안 (수집기 출력 그대로)

```
## TT 지표 대시보드 (v2)
- 생성: 2026-10-09T23:12:18+09:00 | 창 시작: 2026-10-09T13:38:33+09:00 | 스캔: 카드 302 / 이벤트 317

| 지표 | 값 | 근거 |
|---|---|---|
| 착수시간 p50 (todo진입→첫수령, 창 내 코호트) | 8분 | n=13 (제외 0) |
| 착수시간 p90 | 3.8시간 | n=13 |
| 리뷰통과율 (제출→검증 페어링) | 33.3% | 4/12 (대기 1, 반납 0, 직행 0) |
| 검증 지연 p50 | 2분 | n=4 |
| 재작업률 (attempt≥2) | 27.6% | 63/228 |
| review→todo 반납 | 2회 | events(이전 상태 추적) |
| todo 진입 출발 상태 | {"unknown_prev": 3, "in_progress": 1, "backlog": 6} | events |
| stall 현재/episode | 3/6 active | 열림 3 · 복구 0 · 미복구 0 |
| stall복구율 (해소분 기준) | - | 상태 파일 누적 |
| 개입: 사람 코멘트/메시지/blocked 진입 | 17/0/0 | events (v1 proxy) |

stall 상세: M4FFCB9J-2AAB, M4DEFW03-QTEN, M32909TN-SJ11
```

확장 방향: 스냅샷 파일 적재 → 주간 추세(증감) → 리뷰통과율·재작업률을 워커별 분해. 초안 단계에서는 단일 시점 표로 충분하다.

## 5. 2주 실험 설계

- **기간**: 2026-10-10 00:00 KST → 2026-10-24 00:00 KST (14일).
- **측정 주기**: 수집기 **30분 크론** — episode 누적 상태 파일 유지(같은 호스트·같은 --state-file). 6시간 주기는 stall 복구 판정에 불충분(R2).
- **기준선**: §4.1 (2026-10-09 v2 실측).
- **판정 기준 (발령자 비준 대상 — 제안)**:
  1. 착수시간: 창 내 코호트 p50 **30분 이하 유지**(기준선 8분 — #3c 교차리뷰 효과 관측). p90 3.8시간도 함께 추적.
  2. 리뷰통과율: 제출 대비 **80% 이상**(기준선 33.3%는 로그 시작 직후 소표본+대기 1건 포함 — 2주 누적으로 안정화). 직행(done) 비중은 10% 이하 권장.
  3. 재작업: **review→todo 반납 주당 ≤ 3회**(기준선 2회/당일). attempt 기반 비율은 추세 참고.
  4. stall복구율: 해소 episode의 **80%가 2시간 내 복구**(관측 주기 30분 전제). 사람 lease 예외 규칙 1주차 확정 후 적용.
  5. 개입횟수: 1주차 관측만(정의 정밀화), 2주차에 주당 기준 확정.
- **중단 기준**: 측정 공백 48시간 초과, 또는 소스 스키마 변경(events/issues 필드)으로 수집기 실패 시 — 재설계 후 재개.
- **종료 산출물**: 스냅샷 시계열 요약 보고(카드 코멘트) + 대시보드 개선안 → TT 개선 #3(M4DEFW03-QTEN) 종결 판단 자료.

## 6. 검증

- 수집기 v2 라이브 실측: §4.1 — 실서버 302카드·317이벤트 스캔, 5종 지표 산출. R3 수치(반납 2회)는 리뷰의 독립 재구성과 일치.
- 독립 리뷰: PR#66@5d2d2de 결정표 R1~R5 전건을 v2에서 해소 — R1 페어링(서버 코드 계약 대조), R2 episode+30분 주기+censoring, R3 이전 상태 추적, R4 todo 진입 코호트, R5 blocked 진입 판정. 재푸시 후 동일 계약으로 재리뷰.
- 시크릿: 자격증명 없는 공개 엔드포인트만 호출, 운영 주소 미기록(secret-scan 통과).
- 이 문서의 수치는 2026-10-09 기준선이며, 실험 데이터는 Git 밖 스냅샷으로 적재한다.
