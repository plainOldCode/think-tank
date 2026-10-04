# 리뷰 게이트 (review gate) 스펙 — POC (N0AN)

v0.1 · 2026-10-04 · 상위: M425EVXW-N0AN · 구현: probe(M428RMBY-XC0K) + 리뷰어 실행기(M428RMG7-2HZF)

## 목적

CI green만으로 머지되던 경로에 **리뷰어 승인 조건**을 추가한다. 게이트는 TT 코멘트에서
강제한다(POC — GitHub 브랜치 프로텍션 미사용).

## 역할

| 역할 | 담당 | 비고 |
|---|---|---|
| 작성 에이전트 | 카드 claim 에이전트 | 브랜치 push, completion report |
| 리뷰어 | TT_REVIEW_AGENT로 선택 | fast path(codex/claude 직접 실행) 또는 심층(hermes kanban triage MoA) |
| 후속 수정 에이전트 | **agent-agnostic** | context가 repo+PR 자체이므로 TT dispatch 표준 인터페이스만 지키면 누구든 |

리뷰어 엔진:

| 엔진 | 경로 | 특성 |
|---|---|---|
| codex | tt-runner 에이전트 `codex` (gpt-6.1-sol xhigh) | fast path — 수 분, 직접 실행 |
| claude | tt-runner 에이전트 `claude` (opus-5.5 medium) | fast path — 교차 리뷰용 |
| hermes kanban | tt-reviewer 수신기 → tt-review 보드 (triage MoA) | 심층 — DAG 분해, ~80분 실측 |

## 리뷰 코멘트 형식 (계약)

리뷰어는 **두 곳에 동일 내용**을 남긴다: GitHub PR 코멘트 + TT 카드 코멘트.

TT 코멘트 (author = 리뷰 에이전트명):

```
review: approve | review: request-changes
PR#<번호>@<head sha 8자리>
<발견사항·사유>
```

- 1행: 판정 마커. 정확히 `review: approve` 또는 `review: request-changes`
- 2행: 판정 대상 커밋 명시. probe가 PR 현재 head와 비교해 **불일치면 무효(stale)**
- 3행 이하: 자유 서술 (체크리스트 결과)
- GitHub 쪽은 `gh pr comment`(코멘트만). self-approve가 GitHub상 불가하므로
  승인 API(`gh pr review --approve`)는 POC에서 미사용 — 승인 판정의 유일한
  근거는 **TT 코멘트**다

## probe 게이트 (decide ⓪ 변경)

merge 후보(카드 연결 + work_contract + attempt≥1 + non-draft + repo 일치 + CI green)에 대해:

1. **미리뷰** (현재 head에 유효한 리뷰 코멘트 없음):
   merge 대신 `review-request` 액션 — 리뷰어 에이전트에 dispatch.
   마커 `[review-req #<pr>/<sha8>]`로 PR·커밋 단위 dedup(회차 1회).
2. **request-changes** (현재 head에 유효):
   merge 보류 + 카드 review 반납 + 수정 지시를 배정 에이전트에 dispatch.
   마커 `[review-fix #<pr>/<sha8>]`.
3. **approve** (현재 head에 유효):
   기존 merge 경로 그대로 — 병합 후 verify-in-merge(M3R7M0ZR-YF99) 포함.
4. 새 커밋 push → head 변경 → 기존 리뷰는 stale → 1로 회귀(재리뷰).

- 리뷰 코멘트 유효성: TT 코멘트 중 author가 리뷰어와 일치하고
  `PR#<n>@<sha8>`의 n·sha8이 현재 PR과 일치하는 것만 인정.
- 리뷰어: env `TT_REVIEW_AGENT` 단일 에이전트명. **미설정이면 게이트 off**
  (기존 동작 — TT_PROBE_INTERVAL과 같은 기본 off 안전 패턴). mini 활성화는
  launchd plist env 추가 시점부터(리뷰어 실행기 2HZF 준비 후).
- 리뷰 게이트로 merge 제외된 카드는 ⓪b needs-merge 코멘트 대상에서 **제외**(소음 방지).
- CI red는 기존 ci-fix가 우선 — 리뷰는 green 이후에만.

## POC 제약 (의도된 단순화)

- 별도 GitHub 계정 없음: 리뷰 코멘트도 plainOldCode 토큰으로 남김.
- 게이트 강제 주체는 probe 하나 — probe 비활성/버그 시 게이트 없음(사람이 직접
  머지하는 경로는 항상 열려 있음). 하드 게이트(브랜치 프로텍션+리뷰 계정)는
  POC 검증 후 후속.

## 리뷰어 dispatch 지시 템플릿

```
[auto review] PR #<n> (<repo>) @ <sha8> — 카드 <card-id> 리뷰 요청.
리뷰 방식: working copy에서 git diff 기준 변경 파일 통독 + 변경 심볼 grep으로
호출자 확인. 공용 모듈(service/probe/routers) 변경 시 호출자 추적 필수.
판정 기준: 계약 v2.1 준수, 시크릿 노출, 테스트 적절성, 놓친 엣지.
결과 제출: GitHub PR 코멘트 + TT 카드 코멘트(포맷: docs/review-gate.md).
첫 줄 'review: approve' 또는 'review: request-changes', 둘째 줄 'PR#<n>@<sha8>'.
```

- 리뷰어 working copy: m2max shallow clone + `git fetch` 유지. 리뷰어 LLM은
  /opt/homebrew/bin/{codex,claude} — 코어 건 교차 리뷰, 사소한 건 단일.

## 리뷰어 판정 기준 (체크리스트)

1. 계약 v2.1 준수 — 보고 3블록(design/implementation/verification)
2. 시크릿·키·토큰 노출 여부
3. 테스트 유무·적절성 (사이드이펙트 1차 방어는 CI 200 테스트 — 리뷰어는 그 밖)
4. 변경 심볼의 호출자 영향 (에이전트 자율 탐색)
5. 스펙·카드 본문 대비 동작 일치

## 상태 흐름 요약

```
PR push → CI green → probe: 리뷰 없음 → [review-req] dispatch → 리뷰어 리뷰
  ├─ approve (TT+PR 코멘트) → probe: merge → verify → done
  └─ request-changes → probe: 카드 review 반납 + [review-fix] dispatch
       → 수정 에이전트 push → head 변경 → 재리뷰 [review-req] …
```
