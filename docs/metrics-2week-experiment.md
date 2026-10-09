# TT 지표 2주 실험 설계 + 대시보드 초안

- 카드: TT M4FFCB9J-2AAB ([TT 개선#3e] 2주 실험 설계 + 지표 대시보드 초안)
- 작성: 2026-10-09, 수령자 hermes@m2max (attempt 2)
- 계약: tt-tdd-v2.1 고정 — 본 문서는 method=planned 설계 산출물이며, 수집기 초안(`scripts/metrics_collect.py`)은 라이브 서버 실측으로 검증한다.
- 원칙: **카드 생성 수는 지표가 아니다**(카드 본문 기준). 운영 품질 지표 5종만 다룬다.

## 0. 결론

| 항목 | 상태 |
| --- | --- |
| 지표 5종 정의·수집 방법 | 본 문서 §2 — 원시 소스는 서버 실측으로 매핑 완료 |
| 측정 창구 | `scripts/metrics_collect.py` 초안 — 라이브 TT 서버 실측 성공(§4) |
| 대시보드 초안 | §4.2 (markdown 표) — 파일 적재 후 추세 비교로 확장 |
| 2주 실험 설계 | §5 — 기간·측정 주기·판정 기준(발령자 비준 대상)·중단 기준 |
| 기준선 (2026-10-09 실측) | 착수 p50 5.1시간 / 리뷰통과 95.9% / 재작업 28.6% / stall 2·3 / 사람 코멘트 2 |

## 1. 원시 데이터 소스 (서버 실측 매핑)

TT 서버 API에서 측정 가능한 것이 무엇인지 2026-10-09 라이브 실측으로 확정했다.

| 소스 | 실측 내용 | 지표 연결 |
| --- | --- | --- |
| `GET /issues?limit=N` | 카드 스냅샷 — `created_at` `started_at` `completed_at` `verified_at` `execution_attempt` `todo_since` `heartbeat_at` `blocked_notified_at` `lease_expires` `waiting_actor` | 착수시간·리뷰통과율·재작업률·stall |
| `GET /events?after_seq=&limit=` | 이벤트 로그(커서 페이지네이션) — `issue.created` `issue.updated` `comment.added` `dispatch.created` `dispatch.updated` `message.posted` 6종. `issue.updated` payload는 `state`(후값) + `fields`(변경 필드 목록) | 재작업 반납·개입·stall 복구 신호 |
| `GET /issues/{id}/dispatches` | `agent` `attempt` `status` `run_state` `started_at` `ended_at` `last_progress_at` | stall 복구·재작업 정밀화 |

이벤트 로그는 **2026-10-09 13:38 (#2 배포 직후)부터 축적**된다(실측 seq 1 확인). 따라서 이벤트 기반 지표는 실험 창(2주) 안에서만 유효하고, 그 이전 이력은 스냅샷 한계(마지막 수령의 `started_at`만 보존)를 문서화한다.

## 2. 지표 5종 — 정의와 수집 방법

### 2.1 착수시간 (time-to-start)

- **정의**: 카드가 todo가 된 시점(`created_at`)부터 첫 수령(`started_at`)까지의 시간. 분포로 보고한다(p50/p90).
- **수집**: 스냅샷 `started_at - created_at`. 재수령으로 `started_at`이 갱신되므로, 2회 이상 수령 카드의 정확한 이력은 이벤트 로그(`issue.updated` fields에 `started_at`)로 보완한다.
- **카드 생성 수와의 관계**: 생성 수는 세지 않는다. 대기열이 몰려도 "생성 후 얼마나 빨리 시작되는가"만 본다.

### 2.2 리뷰통과율 (review pass rate)

- **정의**: 완료 보고를 제출한 카드(`completed_at`≠null) 중 검증까지 통과한 카드(`verified_at`≠null)의 비율. 보조 지표로 verify 지연(`verified_at - completed_at`)의 p50.
- **수집**: 스냅샷 비율 + 이벤트 `issue.updated`(fields에 `verified_at`)로 시계열화.
- **해석 주의**: 통과율이 100%면 게이트가 너무 느슨하다는 신호일 수 있다 — 재작업률(§2.3)과 함께 읽는다.

### 2.3 재작업률 (rework rate)

- **정의**: 수령 이력이 있는 카드 중 `execution_attempt ≥ 2` 비율. 보조 지표로 review→todo 반납(재작업 위임) 횟수 — 이벤트에서 `issue.updated` payload `state=="todo"` && `"state" in fields` 로 센다.
- **수집**: 스냅샷(`execution_attempt`) + 이벤트 카운트.
- **참고**: request-changes 리뷰 → probe 반납 → 재수령 흐름이 이 지표에 잡힌다. probe review-fix dispatch 404(미해결 버그)로 반납이 사람 몫이 되는 기간의 왜곡은 limitations에 기록한다.

### 2.4 stall복구율 (stall recovery)

- **정의**: 진행 중(`in_progress`/`review`)인데 heartbeat가 기준 시간(초안 60분) 이상 정지했거나 lease가 만료된 카드를 stall로 판정한다. 복구 = 이후 `issue.updated` 이벤트에서 `heartbeat_at` 갱신 또는 상태 전진(review/done)이 관측되는 것.
- **수집**: 스냅샷으로 현재 stall 카드 식별 + 이벤트로 복구 신호 추적.
- **한계(초안)**: 사람이 보유한 lease(예: 발령자 작업 카드)는 stall에서 제외하는 예외 규칙이 필요하다 — 실측에서 `M4DEFW03-QTEN`(lease skshim@tp-13)이 이에 해당. 실험 1주차에 예외 규칙을 확정한다.

### 2.5 개입횟수 (human intervention)

- **정의**: 카드 진행에서 사람(발령자)이 직접 개입한 횟수. v1 원시 카운트: ①사람 액터(`skshim*`)의 `comment.added` ②사람 `message.posted` ③`blocked` 전이.
- **수집**: 이벤트만으로 가능(코멘트 author가 payload에 실측 확인됨).
- **정밀화(실험 기간)**: 발령자의 정상 발령(`dispatch.created` by skshim)은 개입에서 제외. "지시가 아닌 개입" 판정은 1주차에 규칙화한다.

## 3. 측정 창구 — 수집기 초안

`scripts/metrics_collect.py` (stdlib-only, 의존성 없음):

- `GET /issues?limit=1000` 스냅샷 + `GET /events?after_seq=` 커서 전수 수집.
- `--json`: 기계 판독용 JSON. 기본: markdown 대시보드. `--out`: 파일 저장.
- cron 제안: **6시간 주기**(00/06/12/18시), 스냅샷 JSON을 수집기 실행 호스트의 Git 밖 디렉터리에 적재(예: `~/tt-metrics/YYYYMMDD-HH.json`) — 원시 운영 데이터는 레포에 커밋하지 않는다.
- 데이터 공백 감시: 스냅샷 간 `events` 최대 seq가 정체되면(48시간+) 실험 중단 기준(§5)에 걸린다.

## 4. 라이브 실측 (2026-10-09 20:40 KST, 기준선)

### 4.1 수집기 실행 결과

- 스캔: 카드 293 / 이벤트 219 (seq 1~219, 로그 시작일 2026-10-09)
- 착수시간: **p50 5.1시간 / p90 30.0시간** (n=220)
- 리뷰통과율: **95.9%** (186/194) — verify 지연 p50 0분 (probe 병합 직후 자동 verify)
- 재작업률: **28.6%** (63/220) — review→todo 반납 6회 (당일분, 이벤트)
- stall: **2/3 active** (기준 60분) — `M4DEFW03-QTEN`(사람 lease, 예외 대상) · `M32909TN-SJ11`(lease 없음, 레거시)
- 개입: 사람 코멘트 2 / 사람 메시지 0 / blocked 0

### 4.2 대시보드 초안 (수집기 출력 그대로)

```
## TT 지표 대시보드 (초안)
- 생성: 2026-10-09T20:40:13+09:00 | 스캔: 카드 293 / 이벤트 219

| 지표 | 값 | 근거 수 |
|---|---|---|
| 착수시간 p50 | 5.1시간 | n=220 |
| 착수시간 p90 | 30.0시간 | n=220 |
| 리뷰통과율 | 95.9% | 186/194 |
| verify 지연 p50 | 0분 | n=186 |
| 재작업률 | 28.6% | 63/220 |
| review→todo 반납(재작업 위임) | 6회 | events |
| stall 카드(기준 60분) | 2/3 active | snapshot |
| 개입: 사람 코멘트/메시지/blocked | 2/0/0 | events |
```

확장 방향: 스냅샷 파일 적재 → 주간 추세(대비 이전 스냅샷의 증감) → 리뷰통과율·재작업률을 워커별 분해. 초안 단계에서는 단일 시점 표로 충분하다.

## 5. 2주 실험 설계

- **기간**: 2026-10-10 00:00 KST → 2026-10-24 00:00 KST (14일).
- **측정 주기**: 수집기 6시간 크론(00/06/12/18) + 이벤트 커서 연속성 확인.
- **기준선**: §4.1 (2026-10-09 실측) — 실험 시작 전 마지막 스냅샷을 기준선으로 보존.
- **판정 기준 (발령자 비준 대상 — 제안)**:
  1. 착수시간 p50: 기준선 5.1시간 대비 **개선 또는 4시간 이하 유지**. p90 30시간은 오래된 백로그 카드 왜곡이므로 실험 창 내 생성 카드로 한정해 재측정.
  2. 리뷰통과율: **90% 이상 유지**. 100%에 수렴하면 게이트 강도를 재점검.
  3. 재작업률: attempt 기반 28.6%는 과거 누적 포함 — 실험 창 내 **review→todo 반납 주당 ≤ 3회**를 목표치로 제안.
  4. stall복구율: stall 발생의 **80%가 2시간 내 복구**. 사람 lease 예외 규칙 1주차 확정 후 적용.
  5. 개입횟수: 1주차는 관측만(정의 정밀화), 2주차에 주당 기준 확정.
- **중단 기준**: 측정 공백 48시간 초과, 또는 소스 스키마 변경(events/issues 필드)으로 수집기가 실패할 때 — 재설계 후 재개.
- **종료 산출물**: 스냅샷 시계열 요약 보고(카드 코멘트) + 대시보드 개선안 → TT 개선 #3(M4DEFW03-QTEN) 종결 판단 자료.

## 6. 검증

- 수집기 라이브 실측: §4.1 — 실서버 293카드·219이벤트 스캔 성공, 지표 5종 산출.
- 시크릿: 수집기는 자격증명 없는 공개 엔드포인트만 호출, 문서에 시크릿 없음(secret-scan 통과 대상).
- 이 문서의 수치는 2026-10-09 실측 기준선이며, 실험 기간 데이터는 Git 밖 스냅샷으로 별도 적재한다.
