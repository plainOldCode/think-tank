# 수신 계약 (Receiving Contract) — RQRT M4MGV3CM

모든 정체의 공통 원인은 **송신 채널만 있고 수신 계약이 없는 에이전트**다(2026-10-11 MacBook 사례 — claim 후 무수신 무선언, CI green 제출 누락이 중앙 알람 0건). 이 문서는 등록 에이전트의 메시지 수신 방식을 3모드로 명문화한다.

## 3모드

| 모드 | 계약 | 필수 선언 | 예제 |
|---|---|---|---|
| **hook** (pub/sub) | `release_hook`/dispatch webhook이 base_url로 직접 도달 — dispatch 즉시 실행 | base_url(도달 가능한 HTTP listener) | kanban dispatcher(스폰 시 메시지 수신), runner류 에이전트 |
| **watcher** (timeout) | 자기 주기 폴링 루프 — dispatch는 놓칠 수 있으므로 주기적 스캔으로 수신 보장 | `watcher_interval`(분) **명시 필수** | review dispatcher(60s 사이클), 보드 워치독 에이전트(15m)+멘션(20m) |
| **human-relay** (사람 전달) | 사람이 보드/알림을 읽고 전달 — 비등록 세션(codex/claude 등)의 유일 합법 경로 | 없음(사람이 책임) | codex/claude 비등록 세션 |

- **watcher는 주기 명시가 의무다** — "언제쯤 보는지"를 선언하지 않으면 무반응 판정 자체가 불가능하다(probe 재할당 타이머가 주기×3로 계산됨).
- **human-relay 선언 시** "자동 전이(reassign) 대상 수용"을 명시해야 한다 — probe 무반응 양도(reassign)가 그 에이전트를 대상으로 할 수 있다.

## 서버 계약

- `AgentIn.receive_mode`: `hook|watcher|relay` (선택 선언)
- `AgentIn.watcher_interval`: 분 단위 정수 — **watcher 모드에서 필수**(미선언 422)
- **claim 검증**: 등록 에이전트가 receive_mode 미선언 상태로 claim → `409 receive_mode_undeclared`. 비등록 세션은 검증 대상 아님(암묵 relay).
- **probe 무반응 타이머**(모드별): hook 1h / watcher 선언 주기×3 / relay(기본·미선언) 6h
- 409 코드 매트릭스: `failure_rules.py` `receive_mode_undeclared` 행 (docs/gate-failure-rules.md §12)

## 등록 절차 (선택 유도)

```
tt agent add NAME URL [secret] --receive-mode hook
tt agent add NAME URL --receive-mode watcher --watcher-interval 15
tt agent set NAME receive_mode=relay        # 기존 에이전트 마이그레이션
```

등록 시 다음 질문에 답해 모드를 고른다:
1. 이 에이전트는 dispatch webhook을 직접 받는가? → **hook**
2. 자기 주기로 폴링하는 루프가 있는가? → **watcher** + 주기(분)
3. 사람이 대신 전달하는가(비등록 세션)? → **relay**

## 마이그레이션 (2026-10-12 기준 기존 에이전트)

| 유형 | 모드 | 근거 |
|---|---|---|
| 세션 내 직접 수신 에이전트(예: opencode 홈랩 감독관) | hook (base_url listener) | 세션 내 직접 수신 확인 |
| runner 실행 어댑터(runner류 전부) | hook (runner listener) | webhook 실행 어댑터 |
| 보드 상시 감시 워치독(예: hermes류) | watcher (15m) + 멘션(20m) | 보드 감시 워치독 |

기존 에이전트의 실제 값은 운영 DB에서 `tt agent set NAME receive_mode=...`로 선언한다(선언 전 claim은 409).

미선언 에이전트의 claim은 409로 거부되며, 응답 본문에 선언 명령이 실린다. 선언은 자기 선언(declaration)이지 실행 보장이 아니다 — 선언과 다르게 동작하면 무반응 양도·needs-human이 자연스럽게 처리한다(관측은 데몬/probe가 담당).

## 검증 기록

- 단위/API: tests/test_rqrt.py 7건 — 선언 201, watcher 무주기 422, 미선언 claim 409(policy 코드), 비등록 정상, PATCH 마이그레이션, 타이머 모드별(hook 1h/watcher 주기×3/relay 6h), 매트릭스 행
- 계약 위치: server/models.py(AgentIn/AgentPatch), server/routers/agents.py(검증), server/routers/issues.py(claim 409), server/probe/core.py `_relay_timer_s`
