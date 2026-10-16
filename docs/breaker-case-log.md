# 타이브레이커 케이스 로그 (breaker case log) — 규약

작성: 2026-10-14 (TT M4NDBRX3-XVY7) / 정본: 이 문서 + mini `~/tt-cases/breakers.jsonl`

## 1. 목적·배경

TT의 층별 필터 — PXCJ(규칙) → EG9D(감지) → 판단기(advisory LLM) — 를 통과하지 못하고
사람/감독관에게 도달하는 **타이브레이커 케이스**를 **해소 시점에** 구조화 기록한다.

- 소급 추출보다 **캡처 우선**: 해소 주체가 그 순간 남긴다.
- 이 로그가 온톨로지화의 **2층 원천** — 1층은 `events`(서버), 2층은 본 로그.
  그래프 뷰는 두 원천에서 빌드 스크립트로 파생한다 — **원천 추가 금지**.
- 서버 스키마 변경 0. probe·데몬은 본 로그를 **참조만** 한다(자동 기록·자동 상태 변경 없음).

## 2. 위치·형식

- 파일: mini `~/tt-cases/breakers.jsonl` — append-only JSONL, 1줄 1레코드.
- 경로 override: 환경변수 `TT_CASE_LOG` (server/judge.py의 `TT_JUDGE_LOG` 패턴 준용).
- 쓰기 장애는 해소 절차를 중단시키지 않는다(기록 우선 — judge 로그와 동일 원칙).

## 3. 레코드 규약 (6필드 + 옵션)

| 필드 | 타입 | 정의 |
|---|---|---|
| `trigger_seq` | int | 정체 트리거 `events.seq` — 정체 에피소드를 시작시킨 이벤트(키의 절반) |
| `card` | str | 카드 ID (키의 절반) |
| `reason_code` | str | `server/failure_rules.py` FAILURE_RULES의 코드 재사용 — **코드 신설 금지**. 새 유형은 표(§4.1, docs/gate-failure-rules.md §4)에 행 추가로 확장(별도 변경) |
| `detector` | str | `probe` \| `daemon` \| `agent:<이름>` \| `human:<신원>` — 정체를 드러낸 주체 |
| `decider` | str | `issuer`(발령자) \| `supervisor-agent` \| `llm-advisory` — 해소 결정 주체 |
| `decision` | str | 결정 한 줄 + 근거(필수, 비어 있으면 무효) |
| `followup_card` | str\|null (옵션) | 후속 카드 링크 |

- **dedup: `(trigger_seq, card)` 유니크** — 중복 기록 거부(검증 스크립트가 검출).
- 작성 주체: **해소 주체**(기본 = 감독관 에이전트 opencode-tp13). 카드 해소·개입 직후 append.
- 일반형 확장 노트(2026-10-11 9G00 코멘트): 본 스키마는 어휘(`card`/`seq`/사유코드)를
  think-tank에 결합하되, 타 도메인 승격 시 `subject`/`ref` 어휘 주입으로 일반화할 여지를 둔다.

## 4. 검증

```
python3 scripts/tt-case-log-check.py [PATH]   # 기본: $TT_CASE_LOG 또는 ~/tt-cases/breakers.jsonl
```
- 스키마 검증 + 사유코드 사전(FAILURE_RULES) 대조 + dedup 검출. exit 0=OK, 1=스키마 오류, 2=파일/사용 오류.

## 5. 시드 worked examples (백필, 2026-10-11 사건)

```jsonl
{"trigger_seq": 1640, "card": "M40MP7EX-RTZ1", "reason_code": "unknown", "detector": "probe", "decider": "supervisor-agent", "decision": "review-fix(#343)가 실행 능력을 갖지 못한 kanban-adapter로 재발령돼 정체 — 거짓 progress 없음. opencode-tp13이 수령해 PR#91 cf140d1 수정 push → 재리뷰 approve → 병합 6f6560c. ※표에 정확 유형 없음: dispatch 수신자 실행 능력 검증(신규 행 후보 — §4.1 행 추가 별도 검토), 잠정 unknown.", "followup_card": null}
{"trigger_seq": 1624, "card": "M4CNE298-XF9W", "reason_code": "no_pr", "detector": "probe", "decider": "supervisor-agent", "decision": "PR#51 병합(10-08)된 작업의 보고가 병합 증거를 연결하지 않고 접수(review 정지) → probe [needs-merge a1] PR 없음(seq 1654) 사람 판단 대기. opencode-tp13이 병합 실측(28ebb7ad) 후 verify → done(10-14). no_pr 기본 대응(사람 판단)과 정합.", "followup_card": null}
```

- RTZ1 trigger_seq=1640: review-fix `dispatch.created`(정체 시작점). XF9W trigger_seq=1624:
  "보고 접수 — review에 정지" comment(정체 시작점). 대안 해석(probe 에스컬레이션 seq 1654)은
  기록 시점에 결정 텍스트로 명시한다.
- 시드의 상세 경위: 카드 코멘트 및 events seq 1565~1710 참조(실험 창 1주차 사건).
