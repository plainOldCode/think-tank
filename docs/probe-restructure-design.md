# probe 판단 재구성 설계 — 온톨로지 투영 · 교체 가능 판단기 · 정책 게이트

- 카드: TT M4JMYZAX-2BTP (`[TT 개선] probe 온톨로지+판단기 재구성 설계 문서 docs 정리 — PR`)
- 원문 분석: TT M4J9V1EW-VV45 코멘트 전문(작성 claude, 24,879자, 영구 경로 `~/Work/analysis/tt-probe-ontology/README.md`, af5aec8 기준). 본 문서는 그 설계안을 **복사가 아니라 설계 문서로 재구성**한 것이다.
- 기준 소스: `origin/main` @ `545d0c0` — 본 문서의 모든 코드 참조는 이 커밋 체크아웃에서 직접 재대조했다. 원문(af5aec8) 이후 자동 아카이브 커밋 5건(net +52줄)으로 `server/probe/core.py`는 934 → 986줄이 되었고, 이에 따라 action은 12종 → 13종(`archive-sweep` 추가)이다(§1.3).
- 표기 규칙: 운영 호스트는 역할명(mini = TT 서버 호스트, dgx-local = GPU 서빙 호스트)으로만 쓰고 주소·hostname은 적지 않는다 — 서버 접근은 `TT_URL` 환경변수로만 한다. 확인하지 못한 주장은 **(확인 안 됨)**, 사람이 정답을 만들어야 하는 지표는 §8.3 측정 불가 선언으로 구분한다. 아직 실행하지 않은 실험은 결과가 아니라 **실측 설계**로 적는다.
- 문서 상태: 설계 제안. 코드·기존 문서는 수정하지 않았고 이 파일 하나만 추가했다.

## 0. 결론 요약

| # | 결론 | 근거 |
| --- | --- | --- |
| 1 | 병합·verify·예산·409 계약은 규칙으로 유지한다. probe의 "승인"은 판단이 아니라 리뷰어 에이전트 판정(`review: approve` + `PR#n@sha8`)의 **집행**이다. 병합 판단에 LLM을 하나 더 얹으면 더 약한 모델이 판단을 한 번 더 하는 셈이다 | §2 J5·J8, 부록 A.2 |
| 2 | 코드가 불어난 원인은 판단 로직이 아니라 **관계를 문자열에서 매번 다시 파싱하는 코드**다. 그래서 온톨로지(관계 투영)가 효과 대부분을 내고, LLM으로 줄일 수 있는 코드는 거의 없다 | §3 비용 실측(HEAD 545d0c0), §4 |
| 3 | LLM은 **보수 방향 전용 · advisory 우선** 판단기로만 들인다. 출력은 `hold`/`escalate`/`annotate`/`rank` 4값뿐이고 `allow`·`merge`는 규칙만 낸다. 우회·승인·배정 강행은 타입 수준에서 없다 | §5.2 단조성, §6 |
| 4 | 런타임은 dgx-local shadow로 시작한다. mini 상주는 TT 서버 응답시간 회귀 count 실측(W0~W2)을 통과한 다음에만 검토한다 | §7.2 (실측 설계) |
| 5 | 평가는 **count 기반만** 설계한다. accuracy·정확률 등 사람 정답이 필요한 지표는 측정 불가로 명시하고 제외한다 | §8.3 |
| 6 | 구현 순서는 P0 판단 로그 → P1 온톨로지 투영 → P2 판단기+게이트 → P3 LLM shadow → P4 조건부 enforce. 각각 별도 카드다 | §9 |

## 1. 배경과 범위

### 1.1 probe의 현재 구조

probe 한 사이클은 `run_once`가 담당한다(`server/probe/core.py:938-958`):

```
snapshot (GET /issues?limit=500, /agents, 카드별 /dispatches, review 카드 코멘트 — core.py:905-927)
  → collect_repos (스캔 repo 풀 — core.py:69-84)
  → collect_prs  (repo마다 gh pr list + PR마다 gh pr checks — core.py:532-559)
  → hydrate_reviews (병합 후보 카드 코멘트 취득 — core.py:164-176)
  → decide (판정 순수 함수 — core.py:189-460)
  → execute (action마다 API/gh 호출 — core.py:636-902)
```

action은 13종이다: `work, resume, merge, release-reviewer, review-claim, review-request, review-fix, review-note, pr-adopt, stale-notify, ci-fix, needs-human, archive-sweep`. 마지막 `archive-sweep`은 원문 분석 이후 추가된 13번째다(`decide` ④ core.py:453-459, `execute` 657-659, `_archive_sweep` 596-613).

### 1.2 이 문서가 원문 분석에서 바꾼 것

1. **줄 참조를 HEAD(`545d0c0`)에서 재유도했다.** 원문의 af5aec8 줄 번호는 자동 아카이브 커밋 5건으로 이동했다. 심볼은 불변이다.
2. **원문의 외부 출처 중 2차 요약만 확인했던 수치를 1차 확인으로 교체했다** — 대조표는 §12.2다. 본문 인용도 교체 후 수치를 쓴다.
3. **미실시 실험을 실측 설계로만 적는다.** 원문 §C의 mini 상주 회귀 설계는 실행된 적 없는 설계이며 본 문서도 결과를 주장하지 않는다(§7.2).
4. **정밀도 보정**: 원문의 일부 카운트를 grep 패턴 수준으로 명시했다(§3.2).

### 1.3 af5aec8 → 545d0c0 사이의 변화 (자동 아카이브)

- action 12종 → 13종: `archive-sweep` 신규. `TT_ARCHIVE_AFTER_DAYS`(기본 0 = 비활성)가 생겼다.
- `core.py` 934 → 986줄. 데드코드 헬퍼 `_now_dt`/`_parse_iso`(core.py:616-633)가 생겼다 — 호출부가 없으며 P1 정리 후보다(부록 A.1).
- `issues.py`의 일부 줄 참조가 이동했다(claim 409 `[policy: code]` 131-138 등) — 부록 A.2는 HEAD 기준이다.

### 1.4 범위 밖

- 서버 스키마 변경(lease kind 컬럼 등) — 별도 결정 사항으로 §10 Q3에만 옵션으로 둔다.
- 관계형 그래프 DB(RDF/OWL, Neo4j) 도입 — 불필요하다고 판단한다. 투영은 현 snapshot `limit=500`(core.py:906) 규모의 in-memory 구조면 충분하다.
- 코드 수정. 본 문서는 설계만 담는다.

## 2. probe 판정 지점 J1~J12 요약

| # | 판정 지점 | 코드(HEAD 545d0c0) | 출력 | 현재 규칙 |
| --- | --- | --- | --- | --- |
| J1 | 배정 불가 사유 스캔 | `decide` 스캔 루프 core.py:209-212 → `policy.eligible`(policy.py:84-87) | `probe_skips{id:code}` | `archived` → terminal/backlog/blocked/review → in_progress+유효 lease=`lease_held` → todo `not_auto`/`budget:*`. 안정 문자열 코드(policy.py:59-81) |
| J2 | release-ready 재개 | core.py:229-237 | `resume`(PATCH todo) | blocked + `waiting_for=dependency` + 서버 `release_ready`. **이 분기는 core.py:237에서 즉시 반환 — 그 사이클의 배정·병합·리뷰 판정 전체를 건너뛴다**(관찰. 의도 여부는 확인 안 됨) |
| J3 | 작업 자동배정 | core.py:239-264 | `work`(claim → dispatch) | 에이전트당 1건. continuation-child → continuation-sibling → pool. `_prio`(priority, id) 순(core.py:23-25). 배정받은 에이전트는 이번 사이클 리뷰 제외(core.py:262) |
| J4 | 병합 후보 필터 | core.py:266-361 | 다음 단계 진행 / skip | 카드 연결 필수(`pr_card_id` core.py:59-66), 종결 카드 제외, 계약+attempt≥1 또는 저위험+review, draft 제외, repo 일치, `ci_passed`=checks 1개 이상 전부 SUCCESS(core.py:38-43) |
| J5 | 리뷰 판정 해석 | `review_verdict` core.py:119-137 | approve / request-changes / None | author가 리뷰어 집합에 있고 `PR#n@sha8`이 현재 head와 일치하는 코멘트만 유효. 최신 우선. 리뷰어 집합은 env ∪ 카드 reviewer ∪ `[review-req]` 마커 3원천(core.py:313-315) |
| J6 | 교차리뷰 자동 수령 | core.py:333-348 | `review-claim` / `review-request` | assignee 아님, 유휴, 이번 사이클 미배정, review 상태, 타인 리뷰 lease 없음, 계약 또는 저위험(`policy.review_eligible` policy.py:90-116). head당 마커 1회 |
| J7 | request-changes 처리 | core.py:323-332 | `release-reviewer` + `review-fix` | `[review-fix #pr/sha8]` 마커 1회 |
| J8 | **병합 승인·집행** | decide core.py:356-361, execute 660-743 | `gh pr merge --squash` → 보고 있으면 `/verify` | 저위험 `server/`·`runner/` 경로 거부(685-688), 파일 목록 불완전 거부(681-684), head 변경 폐기(689-701). 병합 후 done은 verify 경로로만(715-727) |
| J9 | 병합 불가 review 카드 분류 | core.py:363-445 | `review-note`, `pr-adopt`, `ci-fix`, `stale-notify` | 전부 draft면 ready 요청 1회(395-405). CI FAILURE면 ci-fix 위임(head당 1회, 411-427). green인데 제외되면 "확인 필요"(409-410). PR 없음은 20분 유예(434-437, `TT_REVIEW_GRACE_MIN` 기본 20) 후 노트, 24h 지속 시 stale 공지(`TT_PROBE_STALE_HOURS` 기본 24) |
| J10 | **needs-human (예산 소진)** | core.py:447-452, `policy.budget_reason`(policy.py:50-56) | `needs-human`(코멘트 + 라벨, `auto` 제거) | `dispatches>=2` 또는 `execution_attempt>=2` |
| J11 | 집행 장애 에스컬레이션 | `_probe_flag` core.py:562-576 | 코멘트. 2회째면 review 반납(572-574) | **사유 구분 없이 'probe merge skip' 문자열 총개수**(core.py:568,570). 경로차단·head변경·gh오류 등 모든 스킵 사유가 같은 문자열로 집계된다 |
| J12 | 리뷰어 점유 반납 | decide 316-322, execute 638-656 | PATCH `reviewer=""` | 판정이 현재 head에 여전히 유효한지 재확인(643-645), `expected_version` 불일치 폐기(648-649), lease는 리뷰어 본인 것일 때만 해제(652-654) |

**서버 쪽 동일 계약**(probe 밖이지만 판단의 경계 — 상세는 부록 A.2): claim 409 `[policy: <code>]`, claim-review 교차리뷰 409, done→review 강제, verify 상태 제한, `expected_version` 낙관적 잠금.

## 3. 관계 유물과 파싱 비용

### 3.1 기존 계약 유물은 사실상 관계 데이터다

TT에는 별도 관계 테이블이 없다. probe가 쓰는 관계 대부분은 코멘트 본문·브랜치명·카드 본문 문자열에 들어 있고 사이클마다 regex로 다시 만들어진다.

| 유물 | 위치 | 인코딩된 관계 (주어 —술어→ 목적어) | 파싱 방식(HEAD) |
| --- | --- | --- | --- |
| `[policy: <code>]` | claim 409 본문(issues.py:131-138), probe_skips 로그, needs-human 사유 | Card —ineligibleBecause→ ReasonCode(안정 enum) | 문자열(안정 계약 — `policy.py` docstring 6-15, 형식 변경 금지) |
| `[review-req #pr/sha8] … → agent` | probe 코멘트 | Agent —requestedToReview→ PR@sha8 (for Card) | `_requested_reviewers` core.py:140-156 |
| `review: approve` 또는 `request-changes` + `PR#n@sha8` | 리뷰어 코멘트 | Verdict(by Agent) —on→ PR@sha8 | `REVIEW_LINE`/`PR_SHA` core.py:115-116 + author 필터 |
| `[review-fix #pr/sha8]` | probe 코멘트 | Card —fixRequestedFor→ PR@sha8 (dedup) | 부분 문자열(core.py:324) |
| `[needs-merge a<n>]` | probe 코멘트 | Card@attempt —escalatedToHuman | 부분 문자열(core.py:385) |
| `[ci-fix a<n> #pr/sha8]` | probe 코멘트 | Card@attempt —ciFixDelegated→ PR@sha8 | 부분 문자열(core.py:417) |
| `[draft-flagged #pr]`, `[stale-notify a<n>]`, `[pr-adopted pr/repo]` | probe 코멘트 | PR —flaggedDraft / Card@attempt —staleNotified / Card —linkedTo→ PR(채택) | 부분 문자열(core.py:398, 104, 378) |
| `probe merge skip …` | probe 코멘트 | Card —mergeFailure (횟수 = 문자열 총개수, 사유 구분 없음) | 부분 문자열 개수(core.py:568,570) |
| 브랜치 `tt/<ID>-…` / PR 제목 ID | GitHub | PR —implements→ Card | `CARD_IN_ANY` regex(core.py:49) |
| `repo: owner/name` / 라벨 | 카드 본문·라벨 | Card —targetsRepo→ Repo | `CARD_REPO` regex + `REPO_CARDS` dict(core.py:481-510) |
| `reviewer`, `lease_by`, `lease_expires` | issues 컬럼 | Agent —holdsLease(kind?)→ Card | 컬럼. **슬롯 1개를 작업·리뷰가 공유**하며 kind는 `reviewer == lease_by`로 추론한다(policy.py:107-110, core.py:653) |
| done≠verified 게이트 | `issues.py` done/verify | Report —for→ (Card, attempt, contract_version), Verification —by→ verifier, status reported/approved | 컬럼 + `check_report` 409 |
| `/dispatches` 개수 | dispatches 테이블 | Dispatch —for→ Card —to→ Agent, (attempt) | 개수 = 예산 입력(J10) |

### 3.2 관찰된 비용 (HEAD 545d0c0 실측)

- dedup 관용구 `c.get("author") == "probe"`가 grep 10곳, `_probe_marker`가 def 1 + 호출 5 = 6곳이다. decide와 execute가 같은 마커 검사를 두 번 한다(review-claim 341↔774-776, review-request 349↔794-796, review-fix 325↔814-816, review-note/draft 399↔847-849, needs-merge 386↔855-857) — 경합 방어 목적이다. 관계가 데이터였다면 "edge 존재 여부" 조회 하나로 끝난다.
- 코멘트 POST는 전체 grep 21곳이고 이 중 act 기반 `execute` 내부가 19곳, `_probe_flag`가 2곳이다(core.py:569,573).
- 리뷰어 집합은 env(`TT_REVIEW_AGENT` core.py:269), 카드 reviewer 컬럼, `[review-req]` 마커(`_requested_reviewers` 140-156)의 **세 원천 합성**이다(J5). 같은 사실 "누가 이 PR@sha의 리뷰어인가"의 근거가 셋이다.
- 카드↔PR 연결 경로는 조인에 쓰이는 것이 셋(브랜치 `pr_card_id` 59-66 / 제목 / 코멘트 PR URL `_comment_pr_repos` 492-500) + 채택 기록(`[pr-adopted]` 378-384)이 하나다. 채택 마커는 기록 전용이고 파싱 역참조는 없다(core.py:861-871).
- 마커의 범위 규칙 — attempt 키(`a<n>`) vs head 키(`#pr/sha8`) — 는 코드 곳곳의 문자열 조립에만 존재한다(예: core.py:324, 385, 398, 417). 스키마가 아니라 관용구다.

이전 내부 설계 검토 M3M0GF1K-G1HT(온톨로지 업무플로우)의 결론 — "판단을 1급 객체로 둔다", "대기는 상태가 아니라 계산값(뷰)" — 을 그대로 적용한다. 마커는 probe 판단의 기록(판단 1급 객체)이고, 온톨로지는 이 기록 위에서 계산한 뷰다.

## 4. 온톨로지 스키마 (엔티티 · 관계 · 마커 정규화)

### 4.1 원칙: 새 원천 데이터를 만들지 않는다

모든 노드와 edge는 기존 스냅샷(issues, comments, dispatches, agents, gh PR)에서 계산한 **투영(read-only)** 이다. 원천은 계속 카드, 코멘트, 마커다 — 이것들이 이미 감사 기록이다. 이벤트 로그를 1급 기록으로 두고 뷰를 파생 계산하는 구조다(§12.4의 Event Sourcing/CQRS가 같은 패턴).

```
Card ──implements◄── PR ──at──► Head(sha)                 Repo
 │  └─targetsRepo──► Repo ◄──in── PR                        ▲
 │──childOf──► Card     │──dependsOn──► Card(release_ready) │
 │──hasAttempt──► Attempt(n, contract_version) ──reportedBy──► Report(result)
 │                                  └──verifiedBy──► Verification(probe|human, reported|approved)
 │──dispatched──► Dispatch(attempt, status) ──to──► Agent
 │──leasedBy──► Lease(kind=work|review, holder:Agent, expires)
 │──ineligibleBecause──► ReasonCode   (policy 안정 코드 — 계산값)
Agent ──reviews──► ReviewAssignment(Card, PR@Head, source=env|card|marker)
Verdict(approve|request-changes) ──by──► Agent ──on──► PR@Head
Marker(kind, scope=attempt|head, author=probe, comment_id) ──records──► Decision
Budget = f(count(Dispatch for Card), Attempt.n)   ← 노드 아님, 계산 속성
```

### 4.2 엔티티와 식별 키

| 엔티티 | 식별 키 | 원천 | 비고 |
| --- | --- | --- | --- |
| Card | issue id | `/issues` | state, labels, priority, version |
| PR | (repo, number) | `gh pr list` | branch, isDraft, labels, checks |
| Head | (repo, number, sha8) | headRefOid | 리뷰·마커 범위의 단위 |
| Agent | name | `/agents` | enabled, base_url. **Reviewer는 엔티티가 아니라 역할(ReviewAssignment)** |
| Dispatch | dispatch id | `/issues/{id}/dispatches` | attempt, status |
| Lease | (card, kind) | `lease_by/expires` + `reviewer` | **kind 명시화가 정규화의 핵심.** 현행은 kind를 추론한다(§10.3) |
| Attempt | (card, n) | execution_attempt, work_contract | 마커 `a<n>` 범위 |
| Verdict | comment id | 리뷰어 코멘트 | Head에 묶이고, Head가 바뀌면 stale |
| Marker | comment id | probe 코멘트 | 7종 kind(§3.1). 판단 기록 = Decision의 영속 흔적 |
| ReasonCode | 문자열 | `policy.py` | 안정 enum. 변경 금지 |
| Budget | (계산) | Dispatch 개수, Attempt | 노드화하지 않는다 |

### 4.3 마커 정규화 규칙

probe 코멘트 마커 7종의 범위 키를 표 하나로 고정한다. 지금은 이 규칙이 코드 곳곳의 문자열 조립에 흩어져 있다(§3.2).

| kind | 범위(scope) | 형식 |
| --- | --- | --- |
| review-req | head (`#pr/sha8`) | `[review-req #pr/sha8] … → agent` |
| review-fix | head | `[review-fix #pr/sha8]` |
| ci-fix | attempt + head | `[ci-fix a<n> #pr/sha8]` |
| needs-merge | attempt | `[needs-merge a<n>]` |
| stale-notify | attempt | `[stale-notify a<n>]` |
| draft-flagged | head | `[draft-flagged #pr]` |
| pr-adopted | (pr, repo) | `[pr-adopted pr/repo]` |

### 4.4 정규화로 생기는 이득

정량 효과는 구현 후 측정해야 하므로 **(확인 안 됨)** 이다 — P1에서 측정한다(§9).

- `has_edge(Card, Marker(kind, scope))` 조회 하나가 dedup 관용구 16곳(10+6)을 대체한다.
- `reviewers(PR@Head)`가 세 원천 합성을 한 곳으로 모은다.
- `linked_prs(Card)`가 조인 경로 셋에 우선순위를 명시한다(지금은 코드 순서가 곧 우선순위다).
- Lease의 kind가 데이터에 드러난다. 작업·리뷰 lease가 한 슬롯을 공유해서 생기는 R5류 경합 방어 코드의 근거가 사라진다(서버 스키마 변경은 §10.3의 별도 결정 — 이 설계는 투영에서 kind를 계산하는 데까지만 다룬다).
- "관계를 엣지로 두면 질의가 traversal이 된다"는 데이터 모델의 직접 공개 사례는 **발견되지 않았다**(§12.2) — 이 설계의 검증은 외부 사례가 아니라 P1 내부 실측에 맡긴다.

## 5. 판단기 인터페이스와 게이트 분리

### 5.1 3층 재구성

```
┌──────────────────────────────────────────────────────────────┐
│ ① 온톨로지 층 (데이터, read-only 투영)                         │
│   build_graph(snapshot, prs) -> Graph                         │
│   - 노드/edge는 §4. 원천은 카드·코멘트·마커·gh (새 원천 없음)    │
│   - 질의: linked_prs, reviewers(pr@head), has_marker(kind,     │
│     scope), lease(card, kind), budget(card), verdict(pr@head)  │
│   - 마커/코드 파서는 이 층에만 존재 (regex 단일화)               │
├──────────────────────────────────────────────────────────────┤
│ ③ 판단기 층 (교체 가능)                                         │
│   Judge.propose(point, view) -> Proposal                       │
│   - RuleJudge: 현행 decide 로직을 지점별로 분해 (기본, enforce) │
│   - LlmJudge: §6 advisory 지점만, shadow → (조건부) enforce     │
│     출력 타입 = {hold, escalate, annotate, rank}만              │
├──────────────────────────────────────────────────────────────┤
│ ② 정책게이트 층 (규칙, 최종 결정)                               │
│   gate(point, proposal, graph) -> Decision(allow|deny|escalate)│
│   - policy.py(eligible / review_eligible / budget) 그대로 흡수  │
│   - 병합 사실검사(head·경로·파일목록·CI·verdict) = 규칙 전용     │
│   - 단조성: LLM proposal은 deny→allow 불가                      │
│   - 모든 Decision → 판단 로그. 외부효과 → 기존 마커 코멘트       │
└──────────────────────────────────────────────────────────────┘
        executor (현 execute — API/gh 호출, 경합 재확인은 그대로)
```

흐름: `snapshot → ① build_graph → 지점별 ③ propose → ② gate → actions → execute`. `decide`의 순수 함수 성질(테스트 용이성)은 유지된다. ①②③ 모두 입력→출력 순수 함수다.

### 5.2 불변 원칙 (정책게이트에 하드코딩)

1. **단조성(monotonic safety)**: LLM 출력은 `hold`, `escalate`, `annotate`, `rank`만 가능하다. `allow`와 `merge`는 규칙만 낸다. LLM이 규칙의 deny를 뒤집는 경로를 타입 수준에서 없앤다. "기본은 거부, 허용은 명시적 허용만 규칙이 낸다"는 fail-safe defaults 원칙의 직접 적용이며(§12.3), 정책 문법에서 forbid가 permit을 이기는 구성과 같은 모양이다(§12.3).
2. **fail-to-rules**: 판단기가 timeout이나 오류를 내면 그 결정은 RuleJudge 결과로 대체하고 `fallback` count를 1 올린다. probe 사이클은 판단기 때문에 막히지 않는다.
3. **기록 우선**: LLM이 기여한 모든 출력은 판단 로그 + (외부 효과가 있으면) 감사 코멘트를 남긴다. 기존 마커 형식과 `[policy: code]`는 바꾸지 않는다. LLM 주석은 **새 마커 kind**(예: `[judge-note <decision_id>]`)로 분리해 기존 파서와 충돌하지 않게 한다.
4. **교차 원칙**: 판단기 모델 계열이 해당 카드의 assignee나 리뷰어 엔진과 같으면 그 결정은 advisory로만 둔다 — LLM 평가자의 자기선호(self-preference)는 인간 기준 동품질 쌍에서도 관측된다는 1차 연구 근거가 있다(§12.2, arXiv 2404.13076). shadow count로 관찰한다.

### 5.3 유지할 계약 (변경 금지)

- `policy.py` reason code 문자열과 `[policy: <code>]` 409 접미사 — 로그·코멘트 파싱 전제(policy.py:6-15).
- probe 코멘트 마커 7종의 형식과 범위 규칙(§4.3). 서버 notify가 `[review-req`·`[release-ready]` 부분 문자열에 의존한다(service.py:164, 377).
- 리뷰 판정 형식(`review: …` + `PR#n@sha8`, core.py:115-116), 교차리뷰 409, done→review 강제, verify 상태 제한, `expected_version` 낙관적 잠금.
- execute의 경합 재확인(마커 재조회, head 재조회, version 비교 — core.py:744-751, 689-701, 648-649) — 투영과 무관하게 집행 직전 원천에서 재확인한다.

## 6. 위험도별 LLM 적용 지점

판정 기준은 세 가지다: ① 오판이 **되돌리기 어려운 외부 효과**(병합, done, 라벨 변경, 카드 선점)로 이어지는가 ② 입력이 **구조화**돼 있어 규칙으로 완전히 기술되는가 ③ 이미 **독립 판단자**(리뷰어, 사람, 서버 409)가 있는가.

| 위험 | 지점 | 결정 | 근거 |
| --- | --- | --- | --- |
| **고** | J8 병합 승인·집행 | **규칙 유지 (LLM 금지)** | 되돌리기 어렵다(main 반영, done). 판단은 이미 리뷰어 LLM + CI가 하고 probe는 집행만 한다. LLM judge의 비결정성이 blocking 게이트에 맞지 않는다는 사례·연구는 §12.1. head sha 재확인·경로 차단·파일 목록 완전성은 순수 사실 검사 |
| **고** | done/verify (merge 후 `/verify`) | **규칙 유지** | 서버 계약(check_report 409, review 경유)이다. probe 재량이 아니다 |
| **고** | J10 needs-human (예산) | **규칙 유지 + LLM은 '추가 에스컬레이션' 제안만** | 폭주를 막는 안전장치(30KP). 완화 방향 판단은 금지. 강화 방향("예산 미소진이지만 사람에게 보여라")만 advisory 허용 |
| **고** | J11 장애 2회 반납 | **규칙 유지** | 카운트 기반 사실. 단, 카운트 대상이 '사유 구분 없는 문자열 총개수'라는 정밀화는 §2 J11 |
| 중 | J9 ⓪b 분류 (needs-merge / ci-fix / stale) | **규칙으로 분류 + LLM은 사람용 요약 주석만 (advisory)** | 분류 입력(checks state, draft, 연령)이 구조화돼 있어 규칙이 완전하다. 사람이 읽는 "왜 막혔나" 설명은 자유 텍스트라 LLM이 보탤 수 있다. 오판 비용이 코멘트 1건이다 |
| 중 | ci-fix 위임 내용 | 규칙 유지 + LLM이 실패 로그를 분류(flaky 의심 / 실패 원인 요약)해 dispatch 본문에 첨부 (advisory) | 위임 여부는 규칙(FAILURE + head 마커)이 정한다. 위임 자체는 이미 env로 위임된다(`TT_CI_FIX_AGENT`/`TT_CI_FIX_TARGET`, core.py:752-753) |
| 중 | J6 교차리뷰 수령 대상 선택 | **규칙 유지** | 자격(own_work, 점유, 계약)은 서버 409와 같은 계약이다. 리뷰 엔진 실측(codex 약 12분, claude 약 4분, hermes 약 80분 — review-gate.md)을 반영한 규칙 가중치가 LLM보다 싸고 결정적이다 |
| 저 | J3 작업 배정 후보 순위 | **규칙 유지 + LLM 순위 제안 shadow** | 자격은 policy가 정한다(불변). 자격 통과 후보 사이의 "에이전트-카드 적합도"만 LLM이 shadow로 제안하고 규칙 선택과의 불일치를 count한다. 오판해도 claim 409/예산 게이트가 하한을 보장한다 |
| 저 | J1 스킵 코드, J2 resume, J5 판정 해석, J12 반납 | **규칙 유지** | 순수 사실 판정이고 안정 코드 계약이다. LLM이 대신할 판단 내용이 없다 |
| 저 | 사람 대기 공지 문안(stale-notify, review-note) | LLM 문안 생성 가능 (advisory) | 정보 전달만. 마커는 규칙이 붙인다 |

## 7. 런타임 비교와 mini 상주 회귀 실측 설계

### 7.1 전제 (코드 근거)

- probe는 서버 프로세스 내부 데몬 스레드다 — `TT_PROBE_INTERVAL>0`일 때 가동하고 미설정이면 off다(app.py의 probe 내장화 주석, `config.probe_interval` config.py:55-62). standalone dispatchd는 `TT_DISPATCH_INTERVAL`(기본 30초, core.py:982)을 쓴다. **운영 중인 실제 주기 값은 확인 안 됨.**
- 한 사이클은 `gh pr checks`를 PR마다 한 번씩 순차 호출한다(core.py:549-553). 네트워크 왕복 N회라 사이클 시간이 수 초 단위일 것으로 추정되지만 **실측값은 확인 안 됨** — §8의 `cycle_ms`로 측정한다.
- LLM 판단 대상(§6)은 사이클당 0~수 건이다. advisory 지점은 비동기(다음 사이클에 반영)로 처리할 수 있어서 판단 지연이 사이클을 막을 필요가 없다.

### 7.2 후보 비교

| 후보 | 장점 | 위험·비용 | 이 설계에서의 위치 |
| --- | --- | --- | --- |
| **규칙만** (현행) | 결정적. 지연 약 0. 130 테스트로 고정. 감사가 쉽다 | 자유 텍스트 판단(요약, 적합도)은 불가 | **모든 고위험 지점의 영구 기본값** + 모든 LLM 경로의 fallback |
| **dgx-local** (원모델, 이미 서빙) | 추가 상주 자원이 0. 큰 모델이라 요약·분류 품질이 상대적으로 높을 것(확인 안 됨). TT 서버와 자원 분리 | tailnet 1홉 RTT와 대기열. 실험 카드 M4GQ1HSK-P7KQ 기준 GPU 약 95% 상시 사용, parallel 8 슬롯을 실험과 공유라 대기 지연이 생긴다. 다른 호스트 가용성에 의존 | **P3 shadow의 1순위.** advisory·비동기라 지연을 견딜 수 있다. 장애 시 fail-to-rules |
| **mini 상주 경량 모델** (TT 서버와 같은 호스트, 별도 서비스) | 네트워크 홉 제거. dgx 대기열과 무관 | **TT 서버와 메모리·CPU·디스크 경합.** probe가 서버 프로세스 안의 스레드라 judge 대기가 길어지면 서버 스레드 자원도 같이 묶일 위험이 있다(→ 별도 프로세스 + 비동기 호출 필수). 판단기 OOM이 서버를 죽이는 역방향이 더 위험하다. 경량 모델의 요약 품질은 **확인 안 됨**. 호스트의 칩셋·여유 메모리도 **확인 안 됨** → 런타임(MLX/Ollama/llama.cpp) 선택은 실측 이후 | **조건부 P4 후보.** 아래 회귀 실측을 통과하고 dgx shadow에서 지연이 문제로 확인됐을 때만 |
| **jev류 프리필터** (~20 ms 의도 분류) | 매우 빠름 | 특성·학습 대상·정확도 모두 **확인 안 됨**(TT·로컬 문서에 jev 기록 없음. ~20ms는 카드 본문 인용). probe 입력은 대부분 구조화돼 있어 "의도 분류"할 자유 텍스트가 거의 없다 | **비채택.** 사람 코멘트 의도 분류를 probe 입력으로 쓰겠다는 결정이 나오면 재검토 |

### 7.3 mini 상주 시 TT 서버 응답시간 회귀 실측 설계 (count 기반)

**이 절은 실측 설계다. 아직 실행된 적 없으며, 아래 수치 중 어느 것도 실측 결과가 아니다.**

- 프로브: mini 외부의 별도 클라이언트(측정 자체의 경합을 피한다)가 `GET /health`, `GET /issues?limit=50`을 10초 간격으로 호출해 `latency_ms`를 기록한다.
- 창 3종 × 각 24h(같은 요일·시간대 권장. 하루 부하 패턴은 확인 안 됨): **W0** 판단기 미설치, **W1** 상주하지만 유휴(모델 로드만), **W2** shadow 가동(실제 probe 판단 요청 + 인위 부하 1회/분).
- count 지표:
  - `slow_req` = latency > W0 p95 × 1.5인 요청 수
  - `err_req` = 5xx/timeout 수
  - `cycle_over` = probe 사이클 시간 > 주기인 사이클 수
  - `judge_timeout`, `fallback` 수
  - 호스트 메모리 압력 이벤트 횟수(관측 방법은 런타임 선정 시 확정)
- 통과 기준(발령자 비준 대상, 초안): W2의 `slow_req`·`err_req`가 W0 대비 증가 0~소수 건, `cycle_over` = 0. **기준치는 W0 무부하 창 실측 후 확정한다** — 임계치를 미리 문서에 굳히지 않는다(§12.1의 무변경 입력 임계치 측정 절차와 같은 취지).
- 실패 시: mini 상주는 보류하고 dgx-local shadow만 유지한다. 실패해도 probe 동작은 규칙 fallback으로 유지된다(fail-to-rules).

## 8. count 기반 평가

#3e 수집기(`scripts/metrics_collect.py`)와 같은 원칙을 쓴다 — **상태 복원은 로그 처음부터, 카운트만 창 안에서**(docs/metrics-2week-experiment.md §1). 모든 지표는 count 또는 지연 분포다.

### 8.1 판단 로그 레코드 (P0 — 지표 전부의 원천)

```json
{"decision_id": "…", "cycle_id": "…", "ts": "…", "point": "J3|J6|J8|J9|J10|…",
 "card": "M…", "pr": "repo#n@sha8|null", "input_hash": "sha256(정규화 입력)",
 "judge": "rule@<git sha>|llm:<model>@<prompt ver>", "mode": "enforce|shadow",
 "output": "work:agentX|skip:budget:attempt>=2|merge|hold|escalate|…",
 "rule_output": "…(shadow 비교용)", "gate": "allow|deny|escalate", "latency_ms": 12,
 "fallback": false}
```

이 스키마는 OPA Decision Logs의 필드 구성(decision_id, 판단에 쓰인 정책 버전, input, result, metrics)과 같은 모양이며 §12.5에서 필드 대응을 둔다. 저장 위치는 열린 질문 Q2(§10)로 구현 카드에서 결정한다. 사이클마다 이벤트가 늘어나므로 저장 비용 측정을 P0 수용 조건에 넣는다(§9).

### 8.2 지표 (전부 count 또는 지연 분포)

| 지표 | 정의(count) | 원천 | 비고 |
| --- | --- | --- | --- |
| 배정 히트 | `work` 결정 중 claim 200 + dispatch 성공 + 같은 attempt에서 review 도달 | 판단 로그 + events(state→review) | |
| 배정 미스 | `work` 결정 중 claim 409 / dispatch 실패 / 같은 attempt에서 review→todo 반납 / 이후 J10 예산 소진 | 판단 로그 + events | 사유별 분리 count |
| 오판(proxy) — 잘못된 스킵 | probe가 코드 X로 스킵한 카드를 창 안에서 사람이 직접 claim하거나 상태를 바꿔 진행시킨 건수 | 판단 로그(skip) + events(사람 액터) | **proxy** — 사람이 다른 이유로 개입했을 수 있다 |
| 오판(proxy) — 잘못된 승인 | probe merge 이후 같은 카드 ID를 참조하는 revert PR 수 / merge 후 verify 실패 코멘트 수 / merge 후 같은 카드 review→todo 수 | gh + 코멘트 + events | **proxy** |
| 오판(proxy) — 잘못된 에스컬레이션 | needs-human 라벨을 사람이 제거하고 **재작업 없이**(attempt 불변) done된 건수 | events(labels) | **proxy** |
| shadow 불일치 | `mode=shadow`에서 `output ≠ rule_output`인 건수(지점별) | 판단 로그 | "누가 맞았나"는 측정 불가. 불일치 사례 목록만 사람 검토용으로 보존 |
| LLM 일관성 | 같은 `input_hash`에서 출력이 달라진 횟수 / 후보 순서를 섞었을 때 순위가 뒤바뀐 횟수 | 판단 로그 | judge 흔들림·위치 편향 관측(§12.1 — 이 정의는 해당 연구의 측정법과 같은 모양이다). 반복 호출의 출력 정합은 다수결(k회)로 본다(§12.1 방법론) |
| 사람 개입 | #3e 정의 재사용(사람 액터 코멘트/메시지 + blocked 실진입, 창 안) + `verify --human`/force_done 수 + probe가 손댄 카드의 사람 라벨 변경 수 | events | #3e와 같은 proxy 표기 |
| 판단 지연 | 지점별 `latency_ms` p50(+p90), `cycle_ms` p50, `judge_timeout` 수, `fallback` 수 | 판단 로그 | |
| 서버 회귀 | §7.3의 `slow_req`, `err_req`, `cycle_over` | 외부 프로브 | mini 상주 후보일 때만 |

### 8.3 측정 불가로 명시 (지표에서 제외)

accuracy, precision/recall, "올바른 배정률", "리뷰 품질" 등 **사람이 라벨링한 정답**이 있어야 하는 지표는 측정 불가다 — 이 문서는 그런 지표를 설계하지 않는다. §8.2의 오판 지표는 모두 관찰 가능한 사후 사건 기반의 proxy count이며 정확도의 대체물이 아니다. 불일치 사례 목록은 사람이 필요할 때 읽을 수 있게 보존만 한다.

## 9. 단계별 계획과 수용 조건 (각각 별도 구현 카드)

| 단계 | 내용 | 수용 조건 |
| --- | --- | --- |
| P0 | 판단 로그(§8.1)를 현행 decide에 덧붙임 — 동작 무변경 | 기존 130 테스트 green. 로그 1 사이클 = 결정 수 일치. 저장 비용 측정 기록 |
| P1 | ① 온톨로지 투영 + 파서 단일화. decide가 graph 질의를 쓰도록 치환 | 동작 무변경. 기존 테스트 + 같은 스냅샷 입력으로 신구 actions 일치 비교(골든 테스트). core.py 줄 수 변화 보고(목표치는 정하지 않음 — 확인 안 됨) |
| P2 | ③ Judge 인터페이스 + RuleJudge, ② gate 분리(policy 흡수) | 동작 무변경. 단조성 위반 케이스(LLM이 allow 제안 → gate가 거부) 단위 테스트 |
| P3 | LlmJudge shadow — dgx-local, advisory 지점(J3 순위, J9 요약, ci-fix 분류)만. 비동기, timeout → fallback | 2주 창 count 수집(§8). 외부효과 0(코멘트도 남기지 않음 — 로그만) |
| P4 | 조건부: shadow count를 근거로 발령자가 지점별로 enforce 승인. 이때만 `[judge-note …]` 코멘트 허용. mini 상주는 §7.3 회귀 실측 통과 시 | 지점별 발령자 비준 기록 |

전환 절차(shadow → enforce)는 "advisory로 시작해 자체 오류율을 count로 측정한 뒤 required로 전환"이라는 AI 코드리뷰 운영 관행과 같은 모양이다. 단, 전환 기준치는 커뮤니티 수치를 인용하지 않고 TT 자체 count로 정한다(§12.1).

## 10. 열린 질문 4건 — 옵션과 권고

발령자 결정 필요. 각 항목은 확인된 코드 사실 → 옵션 → 권고 순서다.

### Q1. J2 `resume` 분기의 조기 반환

- 사실: blocked + `waiting_for=dependency` + `release_ready` 카드가 있으면 첫 유휴 에이전트에게 `resume`을 내고 **core.py:237에서 즉시 `return actions`** — 그 사이클의 배정(J3)·병합(J8)·리뷰(J6·J9) 판정 전체를 건너뛴다. 건너뛴다는 것 자체는 코드 사실이고, 의도 여부는 코드만으로 판단 불가다(원문 표기 유지). 영향은 1사이클 지연이다.
- (a) **P1 골든 테스트 전에 별도 카드로 의도를 확인하고 유지/수정을 결정한다. (권고)** — 신구 비교 기준선을 깨끗하게 유지하기 위해서다. 기준선이 오염된 상태의 골든 테스트는 이후 모든 비교를 오염시킨다.
- (b) 골든 테스트에서 현행 동작을 그대로 기준선화한다 — 의미가 명확해질 때까지 동결.

### Q2. 판단 로그 저장처

- 사실: probe는 서버 내 스레드(app.py)다. 서버 이벤트는 `service.log_event` → events 테이블이고 수집기가 `GET /events`로 소비한다(metrics-2week-experiment.md §1).
- (a) **probe 로그 파일** — 서버 스키마 무접촉, P0에 안전. 수집기 연동은 별도 읽기가 필요하다. **(권고)**
- (b) events 테이블의 새 kind — SSE·수집기 즉시 연동. 대신 서버 스키마 접촉 + 사이클당 이벤트 증가율 비용.
- 권고: P0은 (a), P2 이후 (b) 재검토 — 로그의 소비 패턴(사람이 읽나, 수집기가 읽나)이 P3에서 명확해진 뒤에 정한다.

### Q3. Lease kind 명시화 범위

- 사실: lease 슬롯 1개를 작업·리뷰가 공유하고 kind는 `reviewer == lease_by`로 추론한다(policy.py:107-110, core.py:653). 이 추론이 맞는 한 투영에서 kind를 계산할 수 있다.
- (a) **투영에서 kind를 계산한다(서버 무변경). (권고 — 먼저)** 원문 §A 비고와 같다.
- (b) 서버 스키마에 kind를 둔다 — 경합 방어 코드(R5류) 제거가 가능하지만 스키마 변경이라 본 설계 범위 밖. 별도 카드로 분리한다.

### Q4. P3 shadow 대상 지점의 우선순위

- 사실: ci-fix 위임은 이미 env(`TT_CI_FIX_AGENT` 기본 kanban-adapter, `TT_CI_FIX_TARGET` 기본 agy, core.py:752-753)로 위임된다. shadow 주입점은 "위임 여부"가 아니라 dispatch 본문에 붙는 컨텍스트다.
- 권고: 원문 제안(J9 요약 → ci-fix 분류 → J3 순위)을 그대로 유지한다. 재검증에서 새 근거는 나오지 않았다. ci-fix는 dispatch 본문 첨부가 자연스러운 주입점이고, J3 순위는 후보 수가 많은 사이클에만 의미가 있어 마지막이 적절하다.

## 11. 이 설계가 주장하지 않는 것

- "LLM이 코드보다 유지보수하기 쉽다": **확인 안 됨.** 프롬프트와 모델 버전도 버전 관리·회귀 테스트 대상이고, 비결정성 때문에 테스트 비용은 오히려 늘 수 있다.
- "온톨로지화로 core.py가 N줄 줄어든다": 수치는 **확인 안 됨** — P1에서 측정한다. 감소 근거는 §3의 중복 패턴 개수뿐이다.
- "정확도가 올라간다": 본 문서는 accuracy류 지표를 설계하지 않는다(§8.3). 이 설계가 검증하는 것은 동작 무변경(P1·P2)과 count 변화(P3)뿐이다.
- 관계형 그래프 DB(RDF/OWL, Neo4j) 도입: 불필요하다고 판단한다. 투영은 현 snapshot 규모(limit=500)의 in-memory 구조로 충분하다.
- 서버 스키마 변경(lease kind 컬럼): 본 설계 범위 밖 — Q3(§10)에서 별도 카드로만 검토한다.
- 기존 계약의 변경: reason code, 마커 7종, 리뷰 판정 형식, 409 계약은 전부 유지한다(§5.3).

## 12. 선행 사례와 외부 근거 (웹 재조사)

이 장은 M4JMYZAX-2BTP 웹조사 카드(2026-10-10)의 결과를 본 설계에 맞게 재구성한 것이다. 모든 출처를 검색 결과 제목이 아니라 원문 텍스트로 확인했다. 표기: [1차] = 공식 문서·논문 원문 직접 확인, [2차] = 블로그·커뮤니티 글, [확인 안 됨] = 원문을 직접 확인하지 못함. 외부 소스의 성능 수치는 "출처가 보고한 값"이며 TT 환경 실측이 아니다. 검색은 무료 폴백 백엔드로 수행됐으므로 같은 쿼리의 재검색은 결과가 달라질 수 있다.

### 12.1 LLM-as-judge의 비결정성과 게이트 위치

| 출처 | 등급 | 확인된 내용(출처가 보고한 값) | 이 설계에의 반영 |
| --- | --- | --- | --- |
| dev.to — "We gated CI on six open-source LLM eval frameworks …" (약 8개월 CI 머지큐 회고) | [2차·원문 직접 확인] | 변경 없는 같은 입력을 금요일 0.83, 월요일 0.78로 채점해 주말에 PR 14건+릴리스 1건 차단. "결정적 검사만 blocking, judge 점수는 advisory" 구성(Promptfoo·DeepEval)만 생존. "한 번이라도 무변경 입력에서 판정을 뒤집은 지표는 blocking 경로에 넣지 않는다" | J8 병합 게이트를 규칙 전용으로 유지하는 근거. judge 흔들림 관측은 §8.2 LLM 일관성 지표 |
| OpenRouter — "How to Gate Pull Requests on LLM Evals in CI" (2026-10-01) | [1차·벤더 가이드] | 고정 eval 세트를 실패하는 유닛테스트처럼 취급. 임계치는 "무변경 브랜치에서 반복 실행해 잡음을 측정한 뒤" 산정. 반복 샘플링+다수결(majority vote)은 모든 모델에서 가능한 비결정성 완화법 | §7.3 W0 기준치를 무부하 창에서 먼저 측정하는 절차, §8.2 다수결 옵션의 방법론 근거 |
| arXiv 2406.07791 — Judging the Judges: Position Bias in LLM-as-a-Judge (IJCNLP 2025) | [1차·논문] | 최신판(v9) 기준: judge 15종, 22 태스크, 15만+ 평가. 지표 3종 — repetition stability, position consistency, preference fairness. 위치 편향은 무작위가 아니며 품질 격차에 강하게 좌우됨. (버전별 수치 상이 — v5/v6은 12 judge/10만+. 인용 시 버전 명시) | §8.2 "같은 input_hash 출력 비교 / 후보 순서 교체" 지표 설계의 1차 근거 |
| arXiv 2306.05685 — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena | [1차·논문] | GPT-4 판정 85% vs 인간-인간 일치 81%(비동점 투표, 첫 턴) | judge 성능의 상한선 참고점. 단, 대형 상용 모델·영어 대화 도메인 수치라 TT의 소형 로컬 모델·코드 도메인 전이는 **확인 안 됨** |
| AI 코드리뷰 도구 운영 관행 (CodeRabbit·Greptile·Graphite Diamond 비교 글 다수) | [2차] | "AI 리뷰는 advisory로 시작해 자체 false-positive rate를 수 주간 측정한 뒤 required로 전환" 공통 권고. "같은 diff를 두 번 돌리면 코멘트가 달라진다 — required status check로 쓸 수 없다". "FP rate ~10% 이하에서 전환"은 한 글의 경험치(인용하지 않음) | §9 shadow→enforce 전환 절차의 관행 근거. 전환 기준치는 TT 자체 count로 정함 |
| GitHub Copilot code review 공식 문서 | [1차·공식문서] | 기본은 "Comment" 리뷰 — required approvals에 불가산. 승인 리뷰는 opt-in(off by default). 승인 후 새 커밋 push 시 승인 해제(dismiss) | probe 리뷰 계약 — 승인은 리뷰어 에이전트의 명시적 `review: approve` 마커로만 받고 head 불일치는 폐기(J5) — 과 같은 구조의 1차 선례 |
| Seldon Core shadow 배치 문서 | [1차·ML인프라] | 그림자 배치 응답은 버려지며 라이브 트래픽에 영향 없음 | P3 shadow 모드의 동작 원형(외부 효과 0) |

**찾지 못한 것**: LLM이 머지·권한 판단을 직접 집행(enforce)하는 안정 운영 사례. 원문 조사와 이번 재조사 모두에서 발견된 구성은 전부 advisory/soft-gate였다. 본 설계는 이 관찰을 J8 규칙 유지의 근거로 쓴다.

### 12.2 온톨로지/그래프 라우팅과 self-preference 정정

- **직접 사례 미발견**: "관계를 1급 데이터(엣지)로 두어 파싱/dedup 코드를 줄였다"는 공개 사례·회고는 검색 범위(arXiv, 기술 블로그, GitHub)에서 **발견되지 않았다.** §4의 효과 검증은 외부 사례가 아니라 P1 내부 실측(골든 테스트 + core.py 줄 수 보고)으로만 한다.
- arXiv 2510.05445 AgentRouter [1차·논문]: 질의·문맥 엔티티·에이전트를 지식그래프 노드로 합치고 학습 가능 엣지 + 이기종 GNN으로 태스크 인지 라우팅. 학습·감독 신호가 전제라 TT 규모(에이전트 수 개, 카드 하루 수십 건)에서 이득은 **확인 안 됨** — 채택하지 않는다. "ACL 2026 수록" 표기도 **확인 안 됨**(arXiv 사실만 확인).
- arXiv 2511.18194 Agent-as-a-Graph [1차·논문]: 도구·상위 에이전트를 KG 동급 노드로 두고 벡터 검색 → 가중 재정렬 → 엣지 traversal로 에이전트를 얻는 검색 기법(Recall@5 +14.9%는 논문 보고치). 본 설계가 취하는 것은 성능이 아니라 **"관계를 엣지로 두면 질의가 traversal이 된다"는 데이터 모델**이다. 단, MCP 도구 검색 도메인이며 오케스트레이션 상태 관리가 아니다.
- **정정**: 원문 분석 §3이 인용한 self-preference "+73.3pp"는 2차 요약 사이트의 수치로 원논문에서 확인되지 않는다. 1차 수치는 arXiv 2404.13076(NeurIPS 2024)의 "GPT-4 자기인식 정확도 73.5%"이며, 자기인식-자기선호 선형 상관과 "인간 기준 동품질 쌍에서도 자기선호"를 본문에서 직접 확인했다. §5.2 원칙 4(교차 원칙)의 근거는 이 1차 논문이다.

### 12.3 하이브리드 규칙+LLM 게이트

- Saltzer & Schroeder 1975 [1차·설계원칙]: "Fail-safe defaults: 접근 결정은 배제가 아니라 허용에 기반하라 — 명시적 허용을 주는 메커니즘의 실수는 허용을 거부하는 안전한 방향으로 실패한다." §5.2 원칙 1·2의 원전이다.
- NVIDIA NeMo Guardrails [1차·공식문서]: 입력 레일(LLM 호출 전 allow/alter/reject)·출력 레일(생성 후 allow/edit/block)·실행 레일(도구 동작 검증). "LLM 제안 → 결정적 레일 검증 → 외부 효과" 구조의 공식 선례다.
- AWS Cedar [1차 — 이번 조사에서 원문 미확인, **확인 안 됨** 유지]: forbid가 permit을 이긴다는 기술은 xslyl 개인 블로그 [2차]에 근거한다. "안전 규칙은 우연히 뒤집힐 수 없다"는 표현 수준으로만 쓴다.
- Obex 0.1.0 [1차·패키지 문서]: zero-LLM 결정적 가드레일 + OPA식 감사 트레일(decision_id, policy_version, decision, blocking_rule, human_override, trace_id). 실사용 성숙도·커뮤니티 규모는 **확인 안 됨** — §8.1 감사 필드의 참고 예로만 쓴다.
- Ordo, OpenAPPA: **확인 안 됨**(이번 재조사에서도 원문 미확보 — rate limit / 출처 미발견). allow/deny/ask 3값 구성 주장은 인용하지 않는다. 본 설계의 gate 3값(allow/deny/escalate)은 TT 자체 요구에서 유래한다.
- 보조 [2차]: orkes.io("LLM guardrail은 의미 검사로 다뤄야지 결정적 규칙의 대체가 아니다"), codilime.com("에이전트가 허용을 결정하지 않는다; 정책엔진이 결정한다"), dev.to/aws("시스템 프롬프트는 강제가 아니라 문맥이다") — 같은 방향의 2차 보강.

### 12.4 판단 로그·감사 트레일과 파생 뷰

- OPA Decision Logs [1차·공식문서]: 이벤트마다 decision_id, labels, bundles[].revision(판단에 쓰인 정책 번들), path, query, input, result, requested_by, timestamp, metrics, erased/masked, trace_id/span_id. 목적은 "감사와 오프라인 디버깅"이다 — §8.1 스키마의 원형(필드 대응은 §12.5).
- EU AI Act 제12조 [1차·법령 독해 사이트, 원전 EUR-Lex 32024R1689]: 고위험 AI 시스템은 자동 로깅 기능 의무 + 최소 6개월 보존(제19·26조). **TT는 EU 고위험 AI 시스템이 아니므로 적용 대상이 아니다** — "판단 로그가 부가 기능이 아니라 시스템 기능이라는 규제 기대치의 선례"로만 인용한다.
- Event Sourcing / CQRS (Fowler) [1차]: 이벤트 로그가 1급 기록이고 상태·뷰는 파생 계산이다("Application state is purely derivable from the event log"). §4.1의 "마커 = 기록, 온톨로지 = 파생 뷰"가 새 패러다임이 아니라 20년 검증된 파생-데이터 패턴의 적용임을 보강하고, 그래프 DB 없이 in-memory 투영이면 충분하다는 판단의 근거가 된다.

### 12.5 판단 로그 필드 대응 (§8.1 ↔ OPA Decision Logs)

| 본 설계 §8.1 | OPA Decision Logs | 비고 |
| --- | --- | --- |
| `decision_id` | `decision_id` | 상관 추적용 고유 ID |
| `judge`(git sha / model+prompt ver) | `bundles[].revision` | "무엇으로 판정했나"의 버전 고정 |
| `input_hash` | `input` | 본 설계는 해시만 남긴다 — 원천은 카드·코멘트에 이미 존재 |
| `output` / `rule_output` | `result` | `rule_output`는 shadow 비교용 확장 |
| `gate`(allow/deny/escalate) | (없음 — 확장) | OPA는 결정 하나만 기록 |
| `mode`(enforce/shadow) | (없음 — 확장) | |
| `latency_ms` | `metrics` | |
| `trace_id` | `trace_id`/`span_id` | W3C trace-context |
| (해당 없음) | `erased`/`masked` | 민감 필드 마스킹 — TT는 기록 자체가 공개 카드라 불필요 |

### 12.6 원문 분석 수치 대조·정정

| 원문 주장 | 재조사 결과 | 본 문서 취급 |
| --- | --- | --- |
| dev.to judge 0.83→0.78, PR 14건 차단 | **원문 직접 확인** — 수치·맥락 일치 | §12.1 그대로 (개인 회고 한계 명시) |
| self-preference "+73.3pp" (2차 요약 인용) | **원논문 미확인 수치** | **arXiv 2404.13076의 73.5%(자기인식 정확도)로 교체** |
| arXiv 2406.07791 15 judge/22 태스크/15만+ | **최신판(v9) 초록으로 확인** — 구판은 12 judge/10만+ | 인용 시 버전 명시 |
| AgentRouter "ACL 2026 수록" | **확인 안 됨** (arXiv 사실만 확인) | "arXiv 2510.05445"로만 표기 |
| Obex/OpenAPPA/Ordo 성숙도 | Obex만 존재·내용 확인(v0.1.0). Ordo rate limit, OpenAPPA 미발견 | Obex는 감사 필드 예로만, 나머지는 인용하지 않음 |
| "LLM이 머지·권한 판정을 직접 집행하는 안정 운영 사례 없음" | **재확인** — 이번에도 발견 없음(찾은 것은 전부 advisory/soft-gate) | §12.1 서두 서술 |

### 12.7 think-tank 공개 소스

- 레포는 공개(public)다. `docs/`는 8종이 존재하고 본 문서가 추가되면 9종이 된다. 조사 시점에 `probe-restructure-design.md`는 존재하지 않았다 — 신규 파일 요건과 정합.
- probe 관련 공개 PR: #22(서버 내장화 TT_PROBE_INTERVAL), #23/#24(ZK3G e2e 문서), #50(코멘트 PR URL 채택), #63(busy/eligible policy 모듈), #15(CI 실패 위임), #16(draft 병합 중단) 등.
- "judge OR LLM OR ontology" 키워드의 공개 PR·이슈 검색: **결과 없음** — 이 설계의 논의는 TT 카드(M4J9V1EW-VV45, M4JMYZAX-2BTP)에만 존재한다.

## 부록 A. 판정 지점 상세 (HEAD 545d0c0)

### A.1 J1~J12 코드 위치

| 지점 | 코드 위치(HEAD) |
| --- | --- |
| J1 | decide 스캔 루프 core.py:209-212; `policy.eligible` policy.py:84-87; reason 코드 policy.py:59-81 |
| J2 | core.py:229-237 (조기 반환 237) |
| J3 | core.py:239-264; `idle` 214-216; `pick` 218-227; `_prio` 23-25; 리뷰 제외 262 |
| J4 | core.py:266-361; `pr_card_id` 59-66; 저위험 279-291; draft 292; repo 294-301; `ci_passed` 38-43 |
| J5 | `review_verdict` 119-137; `REVIEW_LINE`/`PR_SHA` 115-116; 리뷰어 집합 313-315; `_requested_reviewers` 140-156 |
| J6 | core.py:333-348; `policy.review_eligible` policy.py:90-116; 마커 334 |
| J7 | core.py:323-332 |
| J8 | decide 356-361; execute merge 660-743 (경로 685-688, 파일목록 681-684, head 689-701, verify 715-727) |
| J9 | core.py:363-445 (draft-flagged 395-405, ci-fix 411-427, green 제외 409-410, checks 없음 430-431, 유예 434-437, stale 440-442); `_stale_act` 95-112 |
| J10 | core.py:447-452; `_budget_blocked` 33-35; `policy.budget_reason` policy.py:50-56 |
| J11 | `_probe_flag` 562-576 (문자열 카운트 568/570, review 반납 572-574) |
| J12 | decide 316-322; execute `release-reviewer` 638-656 |
| ④ archive-sweep | decide 453-459; execute 657-659; `_archive_sweep` 596-613 |
| 데드코드 | `_now_dt` 616-621, `_parse_iso` 624-633 — 호출부 없음(grep 실측). P1 정리 후보 |

### A.2 서버 쪽 동일 계약

| 계약 | 위치(HEAD) | 내용 |
| --- | --- | --- |
| claim 409 `[policy: <code>]` | issues.py:131-138 | todo가 아니면 거부 응답에 단일 정책의 reason code를 실는다. 타인 assignee 409 |
| claim-review 제한 | issues.py:262-276 | review 상태만(270). 본인 작업 409(`cross-review`). 유효한 타 리뷰어 lease 409. `BEGIN IMMEDIATE`로 한도 검사(268) |
| done→review 강제 | issues.py:391, 428-438 | done 제출은 review가 종착지 — done은 probe verify 또는 사람 verify/force_done/close로만 |
| verify 상태 제한 | issues.py:560-572 | agent는 review만, human은 in_progress도 허용(567-571). `check_report` 409 |
| 낙관적 잠금 | `bump` service.py:56 | `expected_version` 불일치·동시 갱신 409 |
| notify 마커 의존 | service.py:164, 377 | `_notify_event`가 `[review-req`·`[release-ready]` 부분 문자열에 의존 — 마커 형식 변경 금지의 서버 측 근거 |
| 리뷰 계약 상수 | `REVIEW_CONTRACT` service.py:27 | 리뷰 dispatch에 주입되는 계약 본문 |

### A.3 probe 환경변수 (HEAD)

| env | 기본 | 위치 | 역할 |
| --- | --- | --- | --- |
| TT_URL | (레포 코드 참조) | core.py:981 | 서버 주소 — 본 문서에서는 이름으로만 표기 |
| TT_AUTO_DISPATCH | unset | core.py:926 | 스냅샷 auto 플래그(=1일 때만 decide 동작) |
| TT_PROBE_INTERVAL | 0 (off) | config.py:55-62 | 내장 probe 데몬 스레드 주기 |
| TT_DISPATCH_INTERVAL | 30 | core.py:982 | standalone 루프 주기(초) |
| TT_DISPATCH_DRYRUN | unset | core.py:964 | execute 생략 |
| TT_REVIEW_AGENT | "" | core.py:269 | 리뷰 게이트 on + env 리뷰어 |
| TT_REVIEW_GRACE_MIN | 20 | core.py:87-92 | review 전이 후 'PR 없음' 판정 유예(분) |
| TT_PROBE_STALE_HOURS | 24 | core.py:95-112 | stale 공지 연령 기준 |
| TT_CI_FIX_AGENT | kanban-adapter | core.py:411, 752 | ci-fix 위임 에이전트 |
| TT_CI_FIX_TARGET | agy | core.py:753 | ci-fix 인수 표기 |
| TT_REPO_SLUG | plainOldCode/think-tank | core.py:478, 486 | 스캔 repo(쉼표 나열) |
| TT_REPO_SCAN_EXTRA | "" | core.py:74 | 추가 스캔 repo |
| TT_ARCHIVE_AFTER_DAYS | 0 (비활성) | core.py:453-457 | 자동 아카이브 스윕 (신규) |
| TT_AGENT | dispatchd | core.py:466 | API x-agent 헤더 |

## 부록 B. 참고자료

상태 표기는 §12와 같다([1차]/[2차]/[확인 안 됨]). 외부 수치는 "출처가 보고한 값"이며 TT 환경 실측이 아니다.

**1차(공식 문서·논문·원전)**

- arXiv 2406.07791 — Judging the Judges: Position Bias in LLM-as-a-Judge. judge 15종·22 태스크·15만+ 평가(최신판 v9 — 버전별 수치 상이). https://arxiv.org/abs/2406.07791
- arXiv 2404.13076 — LLM Evaluators Recognize and Favor Their Own Generations (NeurIPS 2024). GPT-4 자기인식 정확도 73.5%, 자기인식-자기선호 선형 상관. https://arxiv.org/abs/2404.13076
- arXiv 2306.05685 — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. GPT-4 85% vs 인간-인간 81%(비동점 투표). https://arxiv.org/abs/2306.05685
- arXiv 2510.05445 — AgentRouter: KG-가이드 에이전트 라우팅(학습 기반, QA 도메인). https://arxiv.org/abs/2510.05445
- arXiv 2511.18194 — Agent-as-a-Graph: 도구·에이전트를 KG 노드로 둔 traversal 검색. https://arxiv.org/abs/2511.18194
- Saltzer & Schroeder — The Protection of Information in Computer Systems (1975). Fail-safe defaults 원문. https://www.cs.virginia.edu/~evans/cs551/saltzer/
- OPA Decision Logs — decision_id·input·result·bundle revision·metrics·trace_id 필드. https://openpolicyagent.org/docs/latest/management-decision-logs
- NVIDIA NeMo Guardrails — input/output/execution rails. https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/how-it-works
- GitHub Copilot code review — 기본 Comment 리뷰(required approvals 불가산), 승인 opt-in, head push 시 승인 해제. https://docs.github.com/en/copilot/how-tos/agents/copilot-code-review/using-copilot-code-review
- EU AI Act 제12조(기록 보존) — 자동 로깅 기능 의무, 최소 6개월 보존(제19·26조). https://artificialintelligenceact.eu/article/12/ (원전 EUR-Lex 32024R1689) — 직접 적용 대상 아님
- Obex 0.1.0 — zero-LLM 강제, OPA식 감사 트레일. https://pypi.org/project/obex/ (성숙도는 확인 안 됨)
- Martin Fowler — Event Sourcing / CQRS. 이벤트 로그 위의 파생 뷰. https://www.martinfowler.com/eaaDev/EventSourcing.html , https://martinfowler.com/bliki/CQRS.html
- Seldon Core shadow 배치 — 그림자 응답은 폐기되며 라이브에 영향 없음. https://docs.seldon.ai/seldon-core-1/tutorials/notebooks/ambassador_shadow

**2차(회고·벤더 가이드·커뮤니티)**

- dev.to — We gated CI on six open-source LLM eval frameworks (약 8개월 실측 회고: judge 0.83→0.78 드리프트로 PR 14건 차단). https://dev.to/ethanwritesai/we-gated-ci-on-six-open-source-llm-eval-frameworks-only-two-survived-the-merge-queue-5elf
- OpenRouter — How to Gate Pull Requests on LLM Evals in CI. 고정 eval 세트·임계치 측정법·다수결. https://openrouter.ai/blog/tutorials/how-to-gate-pull-requests-on-llm-evals-in-ci/
- AI 코드리뷰 advisory-우선 관행 — nisai.dev, buildbyzaki.space, dev.to(pickuma) 등 2차 비교 글 다수
- xslyl — Agent Policy Engine Design. PDP/PEP, Cedar forbid-우선(2차 경유), 정책엔진-감사 분리. https://xslyl.com/en/posts/agent-policy-engine.html
- orkes.io / codilime.com / dev.to/aws — "LLM은 의미 검사, 최종 결정은 정책엔진" 보조 근거

**확인 안 됨 (인용하지 않음)**

- Ordo(https://gitblind.noratr.app/Ordo-Engine/Ordo — rate limit), OpenAPPA(원출처 미확보), AWS Cedar 1차 문서(https://aws.github.io/cedar — 미확인), AgentRouter의 ACL 2026 수록 여부, self-preference "+73.3pp"(원논문 미확인 수치 — §12.6에서 교체), 9B급 소형 모델의 go/stop 판정 정확성, jev류 프리필터 특성(~20ms), mini 호스트의 칩셋·여유 메모리, 운영 중 probe 주기 실측값

**레포·공개 소스**

- plainOldCode/think-tank — docs 8종(본 문서 추가로 9종), probe 관련 PR #22/#23/#24/#50/#63/#15/#16. "judge/LLM/ontology" 공개 검색 결과 없음
- think-tank 내부 문서: docs/review-gate.md(리뷰어 엔진 실측), docs/dispatchd.md(30초 루프 설계), docs/metrics-2week-experiment.md(#3e 지표·수집기), docs/probe-e2e.md(실측 기록 포인터)
- 내부 카드: M4DEJVY1-XFH2(리팩토링 에픽), M3M0GF1K-G1HT(온톨로지 업무플로우 설계 리뷰), M4GQ1HSK-P7KQ(dgx-local 서빙 제약 기록), M4FFCB9J-2AAB(#3e 지표), M4J9V1EW-VV45(원문 분석)

## 부록 C. 기준 커밋 실측 정리 (HEAD 545d0c0)

본 문서의 정량 서술은 아래 실측에 근거한다. 전부 2026-10-10 체크아웃에서 직접 수행했다(재현 명령은 §검증).

| 항목 | 값 | 방법 |
| --- | --- | --- |
| server/probe/core.py | 986줄 (af5aec8 기준 934줄, +52) | `wc -l` |
| server/policy.py | 116줄 (불변) | `wc -l` |
| server/routers/issues.py | 700줄 | `wc -l` |
| action 종류 | 13종 (archive-sweep 포함) | `decide`/`execute` 전수 대독 |
| `c.get("author") == "probe"` | 10곳 | `grep -c` |
| `_probe_marker(` | 6곳 (def 1 + 호출 5) | `grep -c` |
| 코멘트 POST `/comments` | 21곳 전체 = execute 내 19 + `_probe_flag` 2 | `grep -c` (구간별) |
| `probe merge skip` 문자열 | 카운트 568/570, 스킵 사유 발생점 6곳(677, 682, 686, 696, 699, 705) | `grep -n` |
| `_now_dt`/`_parse_iso` | 정의만 존재(616-621, 624-633), 호출부 없음 | `grep -n` |
| probe 테스트 8파일 | **130 passed, 41 warnings** (Python 3.11.16, pytest 8.3.5) | `pytest -q` |

경고 41건은 fastapi `on_event` 비권장 안내 등 기존 경고다 — 본 문서와 무관하다.

## 검증

본 문서 작성 과정에서 실제로 수행한 검증이다(2026-10-10, `545d0c0` 체크아웃).

1. **코드 참조 재대조**: `server/probe/core.py`(986줄 전수), `server/policy.py`(116줄 전수), `server/routers/issues.py`(claim 131-138 / claim-review 262-276 / done→review 391·428-438 / verify 560-572 구간), `server/service.py`(27, 56, 149-171, 377), `server/app.py`(probe 내장화 구간), `server/config.py`(55-62)를 직접 대독했다. 본문의 모든 `file:symbol:line` 참조는 이 체크아웃에서 산출했다 — 원문(af5aec8)의 줄 번호를 그대로 쓰지 않았다(§1.2).
2. **grep 실측**: §3.2와 부록 C의 카운트(10곳/6곳/21곳/문자열 위치/데드코드)는 `grep -c`·`grep -n` 실측값이다.
3. **회귀 테스트**: probe 관련 8개 테스트 파일 — **130 passed, 41 warnings**(1.08s, Python 3.11.16 + pytest 8.3.5). 41건의 경고는 기존 것(fastapi `on_event` 비권장 등)으로 본 변경과 무관하다. 본 PR은 문서 추가뿐이라 코드 동작은 변하지 않는다.
4. **시크릿**: `bash scripts/secret-scan.sh` 통과. 추가로 본 문서에 대해 IPv4·hostname·토큰 패턴 grep을 직접 수행해 0건을 확인했다. 서버 주소는 `TT_URL`로만 표기했다.
5. **마크다운**: 코드펜스 8개(짝수, 언어 태그 json만), 표 16개 — 열 수 불일치 0행, `## ` 절 16개. 본문 상호 참조(§7.3↔§12.1, §8.1↔§12.5, §8.2↔§12.1)의 존재를 확인했다.
6. **변경 범위**: `git diff main --stat` — `docs/probe-restructure-design.md` 신규 1개 파일 외 변경 없음. 코드·기존 문서·설정은 건드리지 않았다.

재현 명령:

```sh
git clone https://github.com/plainOldCode/think-tank.git src && cd src && git checkout 545d0c0
wc -l server/probe/core.py server/policy.py server/routers/issues.py   # 986 / 116 / 700
grep -c 'c.get("author") == "probe"' server/probe/core.py              # 10
grep -c '_probe_marker(' server/probe/core.py                          # 6
grep -n 'probe merge skip' server/probe/core.py                        # 568/570 + 발생점 6곳
python3 -m venv .venv311 && .venv311/bin/pip install pytest fastapi httpx pydantic uvicorn
.venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_policy.py \
  tests/test_cross_review.py tests/test_review_gate.py tests/test_dispatchd.py \
  tests/test_probe_embed.py tests/test_probe_pr_adopt.py \
  tests/test_completion_via_merge.py tests/test_execute_merge_head_guard.py
# → 130 passed, 41 warnings (Python 3.11 기준; 3.9은 PEP 604 유니온으로 미지원)
bash scripts/secret-scan.sh
git diff main --stat   # docs/probe-restructure-design.md 1개 파일만
```
