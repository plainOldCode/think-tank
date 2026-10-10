# 게이트 실패 사유 매트릭스 기반 probe 규칙표 설계 — LLM 판단 최소화

카드: M4JYTVXP-WVBY (설계 문서 — 구현은 별도 카드) · 기준: think-tank main 5a90bad · 2026-10-10

## 0. 결론 요약

1. **매트릭스 15행 전부를 "규칙표(데이터)"로 확정한다.** LLM이 관여하는 행은 3곳뿐이다 — unknown 분류, no_pr의 문서/코드 구분, ci_failed·evidence_rejected의 사유 요약. 나머지 12행은 규칙만(R), 사람(H), 또는 probe 수정(BUG)으로 처리한다.
2. **처리 구분(R/R+S/R+C/H/BUG)을 판단기 출력 타입과 정합시킨다.** LLM이 낼 수 있는 출력은 `annotate`(요약)·`rank`(제안)뿐이고, `hold`는 규칙(반복 억제)도 내며, `allow`·`merge`는 규칙 전용이다 — probe-restructure-design.md §5.2 단조성 원칙을 그대로 계승한다.
3. **반복 사건 억제 규칙**: (사유 코드, 범위) 키 카운터가 임계 N에 도달하면 사람에게 1회 통보 후 해당 조합을 스킵한다. 해제는 사람 전용 — probe가 자동 해제하지 않는다. draft_merge_loop(260건/1카드 이상치)의 재발을 막는 주 장치다.
4. **소형 9B급 모델은 unknown 행 그림자 모드 전용이다.** 제안을 로그로 쌓고 사람이 검토해 표에 정식 반영하는 것이 유일한 반영 경로다. 평가는 count 기반만이고 accuracy류 지표는 측정 불가로 명시한다.
5. **기존 계약은 보존된다.** policy 안정 코드(`[policy: …]`), probe 코멘트 마커 7종 형식, 409 코드(own_work/lease_held/review_occupied)는 변경하지 않는다. 새 유형 대응은 코드가 아니라 표에 행을 추가하는 방식이다.
6. **fix_dispatch_404(42건)는 규칙 문제가 아니라 BUG다.** review-fix dispatch 수신자 레지스터리 재확인 + 재시도 + 사람 통보로 제거하는 별도 구현 카드가 필요하다.

## 1. 배경과 범위

- 발신: M4JYTVXP-WVBY(본 카드) — "게이트 실패 사유 매트릭스를 중심으로 LLM 판단이 최소화되는 지점의 규칙 설계".
- 근거 자료:
  - **게이트 실패 사유 매트릭스 초안** — M4JMYZAX-2BTP 코멘트(2026-10-10, author probe-design@box, 2,058자). 본 문서는 이 초안의 15행 구조·집계 수치를 그대로 출발점으로 쓴다.
  - **막힘 집계 원본** `/workspace/tt-block-census.md` (413장, 2026-09-21~10-10) — box 워크스페이스에 있어 본 문서에서는 코멘트 초안의 집계를 인용만 한다. 원본 미대조(확인 안 됨).
  - **PR #78 리뷰 판정** — TT 코멘트 + GitHub PR 코멘트(kanban-adapter, `review: approve` `PR#78@3ccd38ef`, 2,107자). 두 독립 리뷰(내용 리뷰 t_5d6b7aef + 계약·보안 감사 t_a35b5747)의 집계 결과다.
  - **probe 코드 실측** — server/probe/core.py(origin/main 5a90bad)의 마커 발행 지점, server/policy.py의 reason 코드.
- 범위: 설계 문서 1개 파일 추가. 코드·기존 문서 무수정. 카드 본문 제약("기존 계약 변경 금지", "'확인 안 됨' 표기")을 따른다.

## 2. M4JMYZAX-2BTP 리뷰 파일 분석

### 2.1 리뷰 구조가 증명한 것

리뷰는 내용 리뷰와 계약·보안 감사 **두 독립 리뷰의 집계**로 판정됐다(approve@3ccd38ef, 차단 이슈 0건, 두 판정 일치). 이 구조가 앞으로의 리뷰 품질 기준선이며, 본 문서의 규칙표도 같은 방식 — "기계적 검증(팩트·시크릿·테스트)은 규칙, 종합 판단은 리뷰어" — 로 나뉜다. 리뷰가 독립 재현한 검증 결과는 다음과 같다.

| 항목 | 리뷰 실측 | 설계 반영 |
| --- | --- | --- |
| 코드 팩트 | 문서 대조 105/105 일치 | 규칙표의 코드 참조도 동일 방식(file:symbol:line)으로 검증 |
| 테스트 | probe 8파일 130 passed (Python 3.14.7/3.13.13 재현) | 규칙표 구현 카드의 회귀 기준선 |
| 시크릿 | 9종·20종 패턴 0건 | 본 문서도 동일 스캔 통과(§8) |
| 외부 출처 | 16건 1차 원문 재확인 일치 | 본 문서의 TT 카드/PR 인용은 ID 고정 |

### 2.2 발견사항 5건의 설계 반영 여부

| # | 리뷰 발견사항 | 처리 |
| --- | --- | --- |
| 1 | probe-restructure-design.md:535 마크다운 자기 카운트 불일치(펜스 8개 vs 실측 10개·절 16 vs 17) | **본 PR 범위 밖** — 병합 후 소형 정정 카드 권고. 본 문서의 자기 점검 수치는 커밋 시점 실측으로 기록(§8) |
| 2 | 마커 7종(probe 코멘트) vs 9종(원문 분석 §A) 정의 차이 | 본 문서는 **probe 코멘트 마커 7종 표**(probe-restructure-design.md §4.3: review-req, review-fix, ci-fix, needs-merge, stale-notify, draft-flagged, pr-adopted)를 따른다. 마커 재정의는 하지 않는다 |
| 3 | 판단 로그 스키마에 trace_id 필드 없음(§8.1) vs §12.5 매핑표 행 존재 | §5에서 로그를 인용할 때 trace_id를 **선택 필드**로 명시한다 |
| 4 | "GPU 약 95% 상시 사용"의 출처 뉘앙스 | 소형 모델 런타임 논의(§7)에서 출처 카드(M4GQ1HSK-P7KQ) 표기를 계승한다 |
| 5 | 보고서 "단일 파일 555줄" vs 실제 554줄 | 본 문서는 main 문서 554줄(5a90bad 기준 실측)로 표기한다 |

### 2.3 리뷰가 남긴 여지

리뷰는 차단 이슈 0건이었으나 "미검증 영역"을 명시했다(arXiv 2406.07791 구판 실수치, 일부 내부 카드의 직접 대조 한정 등). 이는 규칙표 설계와 직접 관련 없으나, "리뷰가 검증 가능한 것과 불가한 것을 분리한다"는 원칙 자체가 본 문서의 확인 안 됨 표기 방식(§8)과 같은 철학임을 확인시킨다.

## 3. 처리 구분 정의와 단조성 정합

### 3.1 처리 구분 (매트릭스 초안 계승)

| 구분 | 뜻 | LLM 개입 |
| --- | --- | --- |
| R | 규칙만 | 없음 |
| R+S | 규칙 + LLM 요약(advisory) | 사유 요약 등 사람 가독성 보조 |
| R+C | 규칙 우선, 표에 없을 때 소형 모델 분류 | 분류 제안(advisory) |
| H | 사람 | 없음 (통보만 자동) |
| BUG | probe 수정으로 제거 | 없음 |

### 3.2 출력 타입 매핑 (단조성 원칙 정합)

probe-restructure-design.md §5.2의 단조성 — LLM 출력은 `hold`/`escalate`/`annotate`/`rank` 4값뿐이고 `allow`·`merge`는 규칙만 낸다 — 을 처리 구분에 대응시키면:

| 처리 구분 | 규칙이 내는 출력 | LLM이 낼 수 있는 출력 |
| --- | --- | --- |
| R | annotate/escalate/hold (규칙) | — |
| R+S | 기본 대응(규칙) | annotate (사유 요약, 사람 가독용) |
| R+C | 기본 대응(규칙) | rank (가까운 코드 제안) |
| H | escalate (사람 통보) | — |
| BUG | — (probe 수정으로 제거) | — |

- **allow·merge는 어떤 처리 구분에서도 LLM 출력이 아니다.** 매트릭스 어느 행도 병합 경로를 LLM에 두지 않는다.
- **LLM 부재 시에도 각 행의 기본 대응은 완결되야 한다**(graceful degrade). 소형 모델은 그림자 모드(§7)로 시작하므로 상시 존재가 아니다. R+S·R+C 행의 규칙 부분은 LLM 없이 동일하게 동작해야 한다.
- 판단 로그: 모든 LLM 개입은 로그 필수(probe-restructure-design.md §8.1 스키마; trace_id는 선택 필드).

## 4. 사유 코드 규칙표

매트릭스 초안 15행을 현행 probe 코드(origin/main 5a90bad) 처리 위치와 정합시킨 규칙표다. "현행 처리"는 이미 코드에 있는 동작, "규칙 반영 후"는 이 설계로 바뀌는 동작이다. 집계는 413장 코멘트 패턴 기반 추정치(§8 한계 참조)다.

| 사유 코드 | 판정 입력(기계적 조건) | 기본 대응(규칙) | 처리 | 집계 | 비고 |
| --- | --- | --- | --- | --- | --- |
| no_pr | 카드 review/완료 후보, 연결 PR 없음 | needs-merge 노트(회차당 1회 dedup, core.py:385) — 저위험 라벨(LOW_RISK_LABELS: docs/documentation/i18n/문서/번역)이면 verify 후보 표시, 코드 카드면 PR 요청 dispatch | R (+C: 문서/코드 구분 애매 시 §5 C2) | 20 | 1카드=1PR 계약(repo 마커)과 연동. verify→done 10건 |
| pr_no_checks | PR 있음, checks 0건 | 리뷰 approve로 대체 가능한지 규칙 판정(독립 리뷰 판정 존재 여부), 불가면 리뷰 요청(review-req) | R | 14 | verify 2건, 나머지 리뷰 대응·수정 |
| ci_failed | PR checks FAILURE/비green | 현행 ci-fix dispatch 유지, 실패 로그 분류는 advisory | R+S (요약: §5 S1) | 3 | 모두 9/30, 다수 자가 해소 |
| merged_no_report | PR merged, 완료 보고/verify 없음 | 자동 verify 요청 또는 담당 agent 보고 요청 | R | 22 | verify 17건 — 가장 단순한 go |
| fix_dispatch_404 | review-fix dispatch가 HTTP 404 | 수신자 레지스터리 재확인 → 재시도 → 사람 통보 | **BUG** | 42(19카드) | 판단 아님. probe 수정 대상(§10 R4) |
| evidence_rejected | 서버 `done 증거 없음/실패` 응답 | 사유 요약해서 담당 agent에 재작성 요청 | R+S (요약: §5 S2) | 45(27카드) | verify 13건은 재제출 성공 |
| verify_422 | verify 422 | 증거 형식 오류 안내 후 재시도 | R | 1 | v2.1 3단계 스키마 안내 |
| runner_stall | runner STALL / STALL-killed | 재dispatch, 반복 시 사람 통보(§6 억제 규칙 적용) | R | 약 31 | 사람 조치 여부 확인 안 됨 |
| budget_exhausted | dispatch-tries>=2 (policy 안정 코드) | needs-human 유지 — **완화 금지** | R+H | 2 | 현행 안전장치(policy.py budget_reason) 그대로 |
| needs_decision | level4-notify human 대기 | 사람에게 올림 | H | 2~3 | RBAC, 멘션 설계 등. go/stop 아님 |
| lease_conflict_401 | claim 409 lease_held, hook 401 | 중복 작업 중단 | R | 4 | 409 코드 자체는 변경 금지(§9) |
| draft_merge_loop | draft PR merge skip 반복 | 같은 사유 N회 반복 시 사람 1회 통보 후 중지(§6) | R | 260(1카드) | 이상치. 현행은 draft-flagged 1회 후 무음(core.py:397) — 통보·스킵 추가 |
| dependency | waiting_for=dependency | 기록 없음 | 확인 안 됨 | 0 | 정식 기록 없음 — §8 |
| marker_mismatch / stale_base | 마커 불일치, stale head | 기록 없음 — stale은 리뷰 문맥 55회 언급 | 확인 안 됨 | 미집계 | 분리 집계 필요(§8) |
| unknown | 위 어느 조건에도 안 맞음 | 소형 모델이 가장 가까운 코드 제안(advisory) + 사람 확인, 로그 | C | 모호 약 30+ | §5 C1 + §7 그림자 모드 |

### 4.1 표 운영 규칙

- **새 유형은 코드가 아니라 표에 행을 추가한다.** 행 추가는 데이터 변경이지 계약 변경이 아니다(§9).
- 판정 입력은 반드시 기계적 조건(스냅샷 필드·이벤트 패턴)으로 기술한다 — "대충 이런 상황"류 서술 금지. 기계적 조건으로 못 쓰면 unknown 행으로 간다.
- 각 행의 기본 대응은 LLM 없이 완결되야 한다(§3.2 graceful degrade).
- 집계 열은 출처(코멘트 패턴 추정)와 함께 유지한다 — 숫자가 정식 이벤트 기록으로 대체되기 전까지는 추정치로 명시한다.

## 5. LLM 잔여 지점 — 3곳으로 한정

매트릭스 전체에서 LLM이 관여하는 지점은 다음 3곳뿐이다. 모두 advisory이고, 출력 타입은 §3.2 매핑을 따른다.

### 5.1 C1 — unknown 분류 (R+C)

- 입력: 어떤 행에도 매칭되지 않은 이벤트 스니펫(코멘트/응답 코드 원문 일부).
- 출력: `rank` — 가장 가까운 사유 코드 후보와 근거.
- 후처리: 제안은 로그로 쌓이고 **사람이 검토 후 표에 행을 추가/수정하는 것이 정식 반영 경로**다. 모델 제안이 곧바로 규칙을 바꾸지 않는다(그림자 모드, §7).

### 5.2 C2 — no_pr의 문서/코드 구분 (R)

- 입력: 카드 라벨(LOW_RISK_LABELS 정합), 본문 `repo:` 마커 존재.
- 규칙 우선: 라벨이 문서 계열이면 verify 후보 표시, repo 마커가 있으면 코드 카드로 PR 요청 — 이 두 조건만으로 분류 가능하면 모델 불필요.
- 모델 분류는 **두 조건이 충돌하거나 둘 다 없을 때만** rank 제안으로 개입한다. 제안 결과도 로그로 남기고 사람이 확인한다.

### 5.3 S1/S2 — ci_failed·evidence_rejected 사유 요약 (R+S)

- 입력: 실패 로그/서버 응답 원문.
- 출력: `annotate` — 사람이 읽는 사유 요약(로그 인용).
- 판정·대응은 바뀌지 않는다: ci_failed는 여전히 ci-fix dispatch, evidence_rejected는 여전히 담당 agent 재작성 요청. 요약은 사람 가독성 보조일 뿐이다.

### 5.4 공통 제약

- 모든 LLM 출력은 advisory다. 어떤 출력도 선점·병합·예산 완화로 직결되지 않는다 — 오판이 계약 우회가 되는 경로는 타입 수준에서 없다(단조성).
- LLM 미응답/오류 시: 규칙 기본 동작 그대로 + 로그에 무응답 count. 재시도는 그 사이클에서 1회 한정(사이클 정지 방지 — draft_merge_loop 교훈).

## 6. 반복 사건 억제 규칙

### 6.1 문제

매트릭스 집계에서 draft_merge_loop이 **1카드에서 260건** — probe가 같은 사유를 매 사이클 반복 기록하며 소음과 기록 부기를 만든 실례다(현행 draft-flagged는 1회 후 무음이지만, 무음이 되기 전까지의 반복과 다른 사유에서 같은 패턴이 재발할 수 있다).

### 6.2 규칙

1. **키**: (사유 코드, 범위). 범위는 마커 7종의 scope 개념을 재사용한다 — 예: (ci_failed, #pr/sha8), (runner_stall, 카드), (draft_merge_loop, #pr).
2. **카운터**: 판단 로그/이벤트에서 count를 집계한다(서버 스키마 변경 없이 — 코멘트 마커 dedup과 로컬 상태로 시작, §10 참고).
3. **임계 N=3(초안값)**: 같은 키가 N회 반복되면 ①사람에게 통보 1회(`escalate`, 기존 level4-notify 경로) ②이후 사이클에서 해당 키의 자동 대응을 **스킵**하고 로그만 축적한다.
4. **해제는 사람 전용**: 사람 확인 기록(코멘트) 후에만 카운터를 초기화한다. probe는 자동 초기화하지 않는다 — 반복 억제가 반복 재개로 이어지지 않게.
5. **예산 보호**: 억제 규칙은 budget_exhausted(R+H)를 우회하지 않는다. needs-human은 완화 금지 원칙 그대로다.
6. **N값**: 초안 3은 count 실측으로 조정한다(§11). 통보 1회 후 무음이므로 N을 낮게 잡아도 소음 위험은 없다.

## 7. 소형 모델 그림자 모드 — unknown 행 전용

- **적용 범위 한정**: C1(unknown 분류)만. C2·S1/S2는 규칙이 1차이고 모델은 보조라, 그림자 시작은 unknown 한 행부터다.
- **런타임**: dgx-local shadow로 시작(probe-restructure-design.md §7.2 계승 — 판단 로그·게이트 불변). mini 상주는 TT 서버 응답시간 회귀 count 실측(W0~W2)을 통과한 다음에만 검토. 9B급 후보의 구체 모델명·수치는 출처 카드(M4GQ1HSK-P7KQ)의 "계획 가정" 뉘앙스를 유지하며 표기한다 — **실효 성능은 확인 안 됨**.
- **평가는 count 기반만**: 제안 수 / 사람이 채택해 표에 반영한 수 / 무응답 수 / 매칭 실패 수. **accuracy·정확률 등 사람 정답이 필요한 지표는 측정 불가로 명시하고 제외한다** — 초안의 원칙(읽는 법)과 설계 문서 §8.3을 그대로 계승.
- **반영 경로**: 로그 → 사람 검토 → 규칙표 행 추가. 모델이 규칙표를 직접 수정하는 경로는 없다.

## 8. 데이터 출처와 한계

- **blocked 기록이 비어 있어** 413장 집계는 코멘트 패턴 기반 추정이다. §4 표의 집계 열은 추정치임이 유지된다.
- dependency(0건·기록 없음), marker_mismatch/stale_base(미집계 — stale은 리뷰 문맥 55회 언급, 분리 실패)는 **확인 안 됨** 그대로다. 정식 blocked 기록 체계로 재측정하는 별도 카드를 권한다(§11).
- census 원본(`/workspace/tt-block-census.md`)은 box 워크스페이스에 있어 본 문서는 대조하지 않았다 — 코멘트 초안 인용만.
- 본 문서 자기 점검(작성 시점 실측): 한글 본문 마크다운 표 6개, `## ` 절 14개(§0~§11+참고자료+검증), 코드펜스 0개. 커밋 시점과 불일치가 생기면 리뷰 지적 대상으로 갱신한다 — PR #78 리뷰 발견사항 1번의 재발을 막기 위한 기록.

## 9. 계약 보존 명시

- **policy 안정 문자열 코드**는 변경 금지: state:terminal, archived, state:backlog, state:blocked, state:review, lease_held, budget:dispatch-tries>=2, budget:attempt>=2, not_auto. 본 규칙표는 이 코드 위에서 읽히는 데이터다.
- **probe 코멘트 마커 7종** 형식(review-req, review-fix, ci-fix, needs-merge, stale-notify, draft-flagged, pr-adopted)은 변경 금지. 새 사유는 마커가 아니라 표 행으로 대응한다.
- **409 코드**(own_work/lease_held/review_occupied)는 리뷰 라우팅(카드 M4JTJ970-WS3C)과 정합 — 본 규칙표의 lease_conflict_401 행도 같은 코드를 참조한다.
- **1카드=1PR** — 본 설계도 단일 PR(브랜치 tt/M4JYTVXP-WVBY-gate-failure-rules)로 제출한다.

## 10. 구현 단계 제안 (각각 별도 카드)

| 단계 | 내용 | 검증기준 초안 |
| --- | --- | --- |
| R1 — 규칙표 데이터화 | §4 표를 상수/데이터 파일로 옮기고 미구현 행의 기본 동작을 decide()/사이클 후처리에 연결 | 각 행별 골든 테스트(입력→기대 출력), 기존 130 테스트 회귀 없음 |
| R2 — 반복 억제 | (사유코드, 범위) 카운터 + N 도달 시 통보 1회 + 스킵 + 사람 전용 해제 | 억제·해제·예산 보호 테스트, draft_merge_loop 재발 방지 시나리오 |
| R3 — advisory 연결 | C1/C2/S1/S2를 판단기 인터페이스(§5.2 인터페이스)로 연결, 로그 필수화 | LLM 부재 시 규칙만으로 완결(graceful), 로그 스키마 준수 |
| R4 — BUG 제거 | fix_dispatch_404: 수신자 레지스터리 재확인→재시도→사람 통보 | 404 재현→해결 테스트, 42건 사례 유형 커버 |

- R4(BUG)는 규칙과 무관하므로 독립적으로 가장 먼저 가능하다. R1→R2→R3 순서는 의존 방향(표 데이터 → 억제 → advisory)이다.
- 소형 모델 투입은 R3 이후 그림자 모드로 한정(§7) — 단계 자체가 새로운 LLM 의존을 만들지 않는다.

## 11. 열린 질문 (발령자 결정 필요)

1. **반복 억제 임계 N**: 초안 3. 영구 카운터인지 기간(예: 7일) 윈도우인지 — count 실측 후 조정 권고.
2. **unknown 제안 채택 주체**: 사람 단독 확인인지, 발령자가 반영 권한을 위임할지.
3. **no_pr 문서/코드 구분**: 라벨+repo 마커 규칙만으로 갈지, 모델 rank 개입을 허용할지(허용 시 그림자 기간).
4. **정식 blocked 기록 재측정**: 코멘트 패턴 추정을 이벤트 기록으로 대체하는 census 카드 필요성 — dependency·marker_mismatch·stale_base 건수가 정리되면 §4 표의 집계 열을 갱신한다.

## 참고자료

- M4JYTVXP-WVBY(본 카드) — 작업 사양·검증기준
- M4JMYZAX-2BTP 코멘트: 게이트 실패 사유 매트릭스 초안(probe-design@box, 2026-10-10), PR #78 리뷰 판정(kanban-adapter, 2026-10-10 21:35), 리뷰 점유 기록
- PR #78 — plainOldCode/think-tank, docs/probe-restructure-design.md +554, merged(3ccd38ef), checks 2/2 success
- probe-restructure-design.md(main, 554줄 @5a90bad) — §5.2 단조성, §4.3 마커 7종, §8.1 판단 로그, §8.3 count 평가
- server/policy.py(origin/main 5a90bad) — reason 코드 목록, LOW_RISK_LABELS
- server/probe/core.py(origin/main 5a90bad) — 마커 발행 지점(need-merge 385행대, draft-flagged 397-398행대, review-req/fix 324-350행대)
- M4JTJ970-WS3C — 리뷰어 카운터파트 라우팅(본 문서 lease_conflict_401 행과 정합)

## 검증

- 시크릿: 저장소 secret-scan 통과 — 운영 주소(Tailscale IP/hostname)·토큰 0건(TT_URL은 환경변수로만 언급).
- 코드 참조: core.py 행 번호는 origin/main 5a90bad 실측(2026-10-10 fetch). 커밋 후 이동 시 file:symbol 기준으로 재확인할 것.
- 수치: 집계는 모두 매트릭스 초안 코멘트 인용(코멘트 패턴 추정) — 원본 census 미대조로 확인 안 됨 항목은 §8에 그대로 보존.
- 기존 파일: 무수정(git status 기준 docs/ 신규 1개만).
- 최종 실측(커밋 ad64f4c blob): 205줄, 표 6개, `## ` 절 14개, 코드펜스 0개 — §8 자기 점검 수치와 일치. 집계 인용 수치(no_pr 20 등 15행 전체)는 매트릭스 초안 코멘트 원문 대조 확인.
