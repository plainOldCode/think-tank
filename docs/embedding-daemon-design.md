# 임베딩 데몬 v2 설계 — probe 보조 계약 고정

카드: M4NHG57N-DADG (설계) · M4NHG5DF-5YM3 (구현) · 전신: M4MTW9RG-EG9D (v1, done 2026-10-11) · 기준: main 5e2fbf6

## 0. 결론 요약

1. **역할 분리는 v1에서 확정된 그대로다.** 데몬은 관측·마커 생성만, 액션은 probe 단일 실행자. state PATCH 경로는 코드에 존재하지 않으며 정적 검사 테스트가 보장한다. 서버 코드 변경 0.
2. **v2의 유일한 변경은 마커 스키마 구조화다.** v1 마커는 사람 가독형 문장이라 probe·JXYS·BG97의 행동부가 기계적으로 파싱할 수 없었다. v2는 6종 마커에 정규화된 필드를 넣어 행동부 계약으로 만든다.
3. **probe 소비는 이번 사이클에 구현하지 않는다.** [rework-stuck]→BG97, [submit-remind]→JXYS — 행동부는 각 카드 구현 범위다. 본 문서는 계약만 고정한다.
4. **기존 계약 보존**: probe 코멘트 마커 7종(`[review-req #` 등), author=probe dedup, 409 코드는 건드리지 않는다. 데몬 마커는 author=tt-embedding-daemon으로 무충돌.

## 1. 실측 → 파라미터 매핑

v1 운영·실험 실측(2026-10-11, mini)이 그대로 근거다.

| 실측 | 파라미터 |
| --- | --- |
| 제목만 임베딩하면 top-2 간격 붕괴 — M4NDBRX3-XVY7이 armour로 오분류 위험(재실측 확인) | 임베딩 입력 고정: 제목+본문 400자 |
| 같은 주제 카드 0.75–0.81 / 무관 텍스트 0.55–0.60 (demo.py) | 분류 참고선 0.7, 중복 판정 0.93 |
| census 잔해 13건 중 4 제안·9 보류, 오탐 0 | top-2 라벨 간격 < 0.01이면 제안 없음(조용한 추측 금지) |
| 라벨 카드 49건에서 4/13 제안 발화 | 참표 = 라벨 카드 전수, 매 폴링 재학습(라벨 드리프트 자동 흡수) |
| 폴링 60s, 임베딩 62벡터 1.9s | POLL=60, MRL 256d, 타임아웃 60s — 유지 |

운영 중 발견된 정체 유형이 하나 더 있다 — probe가 `[review-fix #n/sha]` 위임을 남기고 dispatch가 실행되지 않는 경우(PR#93, 2026-10-11 — 사람이 발견해 수동 진행)와 dispatch 자체가 실패로 기록되는 경우(PR#114, '수정 dispatch 실패(에이전트 미등록)')다. 후자는 마커에 근거가 남지만 전자는 아무 흔적이 없어 probe가 재발령할 근거를 얻지 못한다. 데몬의 7번 감시([fix-stall])가 이 공백을 메운다 — 소비자 재확인 의무는 BG97 계약과 같다.

## 2. 마커 스키마 (v2)

공통 규칙:

- author는 항상 `tt-embedding-daemon`. probe의 dedup은 author=probe 코멘트만 보므로 무충돌.
- 첫 행이 마커 헤더 `[<kind>]`, 이후 필드는 `key=value` 공백 구분. 사람 가독 문장은 헤더 다음 줄에 둔다.
- **필드값은 공백을 포함하지 않는다.** 공백이 포함된 원본 문자열을 필드에 그대로 넣지 않는다 — 실제 probe 마커는 공백을 갖는다(예: review-fix 마커 `[review-fix #93/c661baaa]`, server/probe/core.py). 이런 원본은 구성 요소로 분해해 복수 필드로 기록한다(아래 `src_*` 정규형). 분해→역파싱 round-trip으로 원본이 보존됨을 검증 기준 §3-2에서 요구한다. 값이 없는 자리는 `-`(예: PR 없는 카드의 head).
- **v2 dedup은 이벤트 키 기준이다.** v1식 prefix substring 매칭(`_marker_present`)은 종류 prefix만 보므로 같은 카드의 새 실행 회차·새 head까지 같은 이벤트로 뭉뚱그린다. 아래 이벤트 키(마커별 정의)가 같으면 재발화를 억제하고, 하나라도 다르면 새 이벤트로 기록한다.
- **v1 마커는 관측용 잔재다.** v1 댓글은 종류별로 필드 수준이 제각각이고 소비에 필요한 구조화 필드가 없다(실례: 본 카드의 v1 `[submit-remind] <구현자> — …` 코멘트 — agent·lease_expires 없음). 따라서 v1 prefix 존재는 v2 기록을 억제하지 않는다 — 관측 시점에 조건이 성립하면 v2로 1회 기록해 구조화 전환한다(전환 1회는 허용, 이후 동일 이벤트 키 재발화는 억제). v1 댓글은 삭제·수정하지 않는다.
- **소비자 재확인 의무**: 행동부는 마커를 소비할 때 현재 카드 state·assignee·lease·PR head를 재확인한다. 과거 마커(v1·v2 모두)로 새 회차의 카운터를 시작하지 않는다 — 마커의 head·lease가 현재값과 다르면 과거 사건이다. BG97 dedup 키 (사유, PR, SHA, attempt)는 현재값과 일치할 때만 유효하고, JXYS 승격 카운터는 현재 head 기준이며 head 변경 시 리셋한다.

```
[auto-classified] label=<label> gap=<Δ> refs=<id1,id2>
[possible-duplicate] ref=<card_id> sim=<x.xx>
[submit-remind] agent=<lease_by> lease_expires=<iso8601> head=<sha8|->
[review-stall] reviewer=<agent> since_hours=<n> req_ts=<iso8601>
[rework-stuck] src_kind=<probe마커kind> src_att=<a{n}|-> src_pr=<n> src_head=<sha8> attempt=<n> target=<agent> kind=<404|non-executor|failed>
[verdict-seen] key=<card_id>:<comment_id>
[fix-stall] src_kind=<probe마커kind> src_att=<a{n}|-> src_pr=<n> src_head=<sha8> attempt=<n> minutes=<n> kind=<no-progress|dispatch-failed>
```

마커별 이벤트 키 — dedup 단위(카드 스코프, 같은 키 재발화 억제):

| 마커 | 이벤트 키 |
| --- | --- |
| [rework-stuck] | src_kind + src_att + src_pr + src_head + attempt + target + kind |
| [submit-remind] | agent + lease_expires + head |
| [review-stall] | reviewer + req_ts |
| [auto-classified] | label |
| [possible-duplicate] | ref |
| [verdict-seen] | key (card:comment_id) |
| [fix-stall] | src_kind + src_pr + src_head + attempt |

`src_*` 정규형 — 공백 포함 probe 마커의 무손실 분해 (rework-stuck):

probe 원본 마커는 공백을 포함하므로(`[review-fix #93/c661baaa]`, `[ci-fix a1 #93/ed574468]`) `src=` 한 필드에 넣으면 공백 구분 파싱이 깨지고, kind만 남기면 PR/head 식별을 잃는다. src를 4필드로 분해한다:

- 분해: `[review-fix #93/c661baaa]` → `src_kind=review-fix src_att=- src_pr=93 src_head=c661baaa` (att 토큰이 없으면 `-`). `[ci-fix a1 #93/ed574468]` → `src_kind=ci-fix src_att=a1 src_pr=93 src_head=ed574468`.
- 완성 예시: `[rework-stuck] src_kind=review-fix src_att=- src_pr=93 src_head=c661baaa attempt=1 target=<agent> kind=404`
- `attempt`는 probe 원본의 회차 토큰(`src_att`)과 다르다 — **관측 시점 카드의 execution_attempt**다. probe review-fix 원본(`[review-fix #93/<sha8>]`, server/probe/core.py)에는 attempt가 없으므로 `src_att=-`가 되지만, 카드가 같은 PR·head에서 재수령되면 execution_attempt만 1→2로 바뀐다. attempt를 이벤트에 귀속시키지 않으면 새 회차의 실패 기록이 억제되고, 과거 마커에는 현재 회차 정보가 없어 소비자가 재확인할 수도 없다(재리뷰 R2 잔여). `attempt`는 이 둘을 분리한다: `src_att`는 원본 문자열 복원용, `attempt`는 현재 회차 귀속용.
- 역파싱: `src_att=-`면 `[<src_kind> #<src_pr>/<src_head>]`, 아니면 `[<src_kind> <src_att> #<src_pr>/<src_head>]` — probe 원본과 동일한 문자열로 재구성되므로 카드 코멘트 매칭에 그대로 쓴다. PR/head는 분해→재구성 round-trip에서 보존된다(§3-2). 이 형태를 벗어나는 probe 마커가 src가 되는 경우 스키마 확장으로 대응하되, 공백 포함 원문을 필드값에 넣는 예외는 두지 않는다.

행동부 소비 계약(구현은 각 카드):

| 마커 | 소비자 | 계약 |
| --- | --- | --- |
| [rework-stuck] | BG97 | src·target을 review-fix 재라우팅 힌트로 사용. BG97 dedup 키 (사유, PR, SHA, attempt)와 정합 — src_kind+kind가 사유, src_pr·src_head가 PR·SHA, 마커 `attempt`가 카드 회차에 대응. src_head ≠ 현재 head 또는 attempt ≠ 현재 execution_attempt면 과거 사건 — 현재 회차 입력에서 제외하고 새 이벤트로 재확인. 과거 원본 실패를 현재 attempt로 다시 태깅하지 않는다 |
| [submit-remind] | JXYS | agent·lease_expires·head로 무반응 승격 카운터 입력. 카운터는 현재 lease·현재 head 기준 — head 변경 시 리셋(JXYS 검증기준 3), lease_expires가 현재 lease와 다른 과거 마커는 카운터 입력으로 쓰지 않는다 |
| [auto-classified] | 사람/트리아지 | label 채택·기각 — 채택되면 라벨 카드가 되어 다음 폴링에 참표 편입(자동) |
| [possible-duplicate] | 사람/트리아지 | ref 중복 확인 — 자동 취소 없음 |
| [review-stall] | 12MZ | 감지 보조 기록 — probe 자체 감지와 중복돼도 무해(이벤트 키 상이). 이벤트 키당 1회, 장기 정체 재알림은 소비자 정책 |
| [verdict-seen] | 없음 | 관측 다이제스트. 액션 없음 |
| [fix-stall] | BG97 | review-fix 재발령 입력. minutes·kind로 무진행과 즉시실패 구분 — dispatch-failed는 즉시 발화(대기 없음), no-progress는 TT_DAEMON_FIX_STALL_MIN(기본 45분) 경과 후. 진행 증거(dispatch 실행 코멘트·재리뷰·판정·수령)가 마커 뒤에 있으면 침묵. 발단: PR#93 [review-fix] 위임 후 dispatch 미실행을 사람이 발견(2026-10-11), PR#114는 '수정 dispatch 실패(에이전트 미등록)'로 기록 |

## 3. 검증 기준 (M4NHG5DF-5YM3)

1. 6종 마커 전부 필드 정규식 통과 — 회귀 테스트로 증명.
2. **src round-trip**: 공백 포함 probe 원본 마커 `[review-fix #93/c661baaa]`, `[ci-fix a1 #93/ed574468]`을 `src_*` 필드로 분해했다가 역파싱하면 원본 문자열과 동일 — PR/head 보존 회귀 테스트.
3. **v1 전환**: v1 마커만 남은 카드에서 v2 기록이 억제되지 않고 조건 성립 시 1회 기록(구조화 전환) — 이후 동일 이벤트 키 재발화 0(이중 기록 없음).
4. **새 회차·새 담당자**: 동일 카드에서 agent·lease_expires가 바뀌면 submit-remind가 새 이벤트 키로 기록된다 — 과거 마커(v1 포함)로 억제되지 않음.
5. **head 변경**: PR head가 바뀌면 submit-remind·rework-stuck이 새 이벤트 키로 기록 — JXYS 승격 타이머 리셋·BG97 (사유, PR, SHA, attempt) 재확인의 입력이 된다.
6. **회차(attempt) 경계**: 같은 head·같은 target에서 카드 execution_attempt만 변경되면 rework-stuck은 새 이벤트로 기록된다(attempt가 이벤트 키에 포함). 이전 attempt의 마커는 현재 회차 입력에서 제외되고, 같은 attempt의 재관측은 1회만 기록된다.
7. state PATCH 경로 0 — 정적 검사 테스트 유지.
8. mini 배포 후 kickstart 재기동 — 소음 0(신규 마커 발화는 조건 충족분만), 마커 필드 실측 확인.
9. 서버 코드 diff 0 — PR에서 server/ 변경 없음.

## 4. 한계

- 재부팅 자동 기동은 bootout→bootstrap→kickstart 실측으로 대체(실제 재부팅 미실측).
- v1 잔재의 구조화 전환은 관측 시점에 조건이 성립할 때만 기록한다 — 과거 v1 댓글의 lease·req_ts·head는 소급 재구성하지 않는다(복원 불가 정보는 유실).
- TT 서버는 루프백에 바인딩하지 않는다( tailscale 인터페이스만) — 데몬 TT_URL은 mini 로컬 plist env로 전달, 레포에 주소 기록 없음.
- 제출 감시 v1은 lease 만료 잠박 기준 — PR+CI green 매칭은 probe 권한 유지(JXYS 본체에서 확장).
- 분류 참표는 라벨 관례에 종속 — 라벨 어휘가 바뀌면 그대로 따라간다(드리프트는 학습으로 흡수, 어휘 설계는 사람 몫).
