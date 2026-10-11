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

## 2. 마커 스키마 (v2)

공통 규칙:

- author는 항상 `tt-embedding-daemon`. probe의 dedup은 author=probe 코멘트만 보므로 무충돌.
- 첫 행이 마커 헤더 `[<kind>]`, 이후 필드는 `key=value` 공백 구분. 사람 가독 문장은 헤더 다음 줄에 둔다.
- dedup 키는 v1과 동일하게 마커 prefix substring 매칭(`_marker_present`). v1 마커가 남아 있어도 같은 이벤트로 취급해 이중 기록이 없다.

```
[auto-classified] label=<label> gap=<Δ> refs=<id1,id2>
[possible-duplicate] ref=<card_id> sim=<x.xx>
[submit-remind] agent=<lease_by> lease_expires=<iso8601>
[review-stall] reviewer=<agent> since_hours=<n> req_ts=<iso8601>
[rework-stuck] src=<probe마커토큰> target=<agent> kind=<404|non-executor|failed>
[verdict-seen] key=<card_id>:<comment_id>
```

행동부 소비 계약(구현은 각 카드):

| 마커 | 소비자 | 계약 |
| --- | --- | --- |
| [rework-stuck] | BG97 | src·target을 review-fix 재라우팅 힌트로 사용 |
| [submit-remind] | JXYS | agent·lease_expires로 무반응 승격 카운터 입력 |
| [auto-classified] | 사람/트리아지 | label 채택·기각 — 채택되면 라벨 카드가 되어 다음 폴링에 참표 편입(자동) |
| [possible-duplicate] | 사람/트리아지 | ref 중복 확인 — 자동 취소 없음 |
| [review-stall] | 12MZ | 감지 보조 기록 — probe 자체 감지와 중복돼도 무해(dedup 키 상이) |
| [verdict-seen] | 없음 | 관측 다이제스트. 액션 없음 |

## 3. 검증 기준 (M4NHG5DF-5YM3)

1. 6종 마커 전부 필드 정규식 통과 — 회귀 테스트로 증명.
2. v1 마커 잔재 카드에서 v2가 이중 기록하지 않는다 — `_marker_present` prefix 매칭 회귀.
3. state PATCH 경로 0 — 정적 검사 테스트 유지.
4. mini 배포 후 kickstart 재기동 — 소음 0(신규 마커 발화는 조건 충족분만), 마커 필드 실측 확인.
5. 서버 코드 diff 0 — PR에서 server/ 변경 없음.

## 4. 한계

- 재부팅 자동 기동은 bootout→bootstrap→kickstart 실측으로 대체(실제 재부팅 미실측).
- TT 서버는 루프백에 바인딩하지 않는다( tailscale 인터페이스만) — 데몬 TT_URL은 mini 로컬 plist env로 전달, 레포에 주소 기록 없음.
- 제출 감시 v1은 lease 만료 잠박 기준 — PR+CI green 매칭은 probe 권한 유지(JXYS 본체에서 확장).
- 분류 참표는 라벨 관례에 종속 — 라벨 어휘가 바뀌면 그대로 따라간다(드리프트는 학습으로 흡수, 어휘 설계는 사람 몫).
