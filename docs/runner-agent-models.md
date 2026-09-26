# 러너 에이전트 모델 pinning (2026-09-26)

TT dispatch 러너(`runner/tt-runner.py`, launchd `com.tt.runner` @ m2max)의 에이전트별 모델/추론 고정표.
원본 소스는 머신 로컬 `~/.config/tt-runner/agents.json` — 이 문서는 그 스냅샷이며, 두 곳이 어긋나면 agents.json이 정본.

## 계층 배역 (TT 규약 M3EHE8C3-DEQ1)

| dispatch agent | 계층 | 모델 | 추론 | 권한 | 메커니즘 |
|---|---|---|---|---|---|
| `codex` | 판정(설계/리뷰) | gpt-6-sol | xhigh | read→(dispatch 오버라이드 가능) | 프로필 `model`→`-m`, `reasoning`→`-c model_reasoning_effort=` |
| `codex-read-only` | 판정(read-only) | gpt-6-sol | xhigh | read (ws=`~/Work` 한정) | 동일 |
| `agy` | 실행/판독 | gemini-3.8-flash | high | skip-permissions+sandbox | plain 드라이버는 model/reasoning 키 **무시** → `auto_args`의 `--model gemini-3.8-flash-high`로 pin |
| `opencode` | 구현 | gx10 qwen3.8-flash-next | (모델 고정값) | auto | `~/Work/opencode.json`이 gx10 endpoint 지정 |

## 주의 사항 (실측 근거)

- **plain 드라이버 함정**: 프로필의 `model`/`reasoning` 필드는 codex 드라이버 전용. agy/opencode류는 CLI 인자(auto_args)로만 전달 가능. agy 모델 tier는 모델 ID 내장(high|medium|low).
- **codex 전역 기본값과 무관**: `~/.codex/config.toml`은 `gpt-6-luna`/`max` — 터미널에서 직접 치는 codex는 그대로이고, **러너 경로만 pin**(사용자 결정 2026-09-26). dispatch 회차가 luna로 도는 걸 막으려면 agents.json의 model 필드가 유일한 방어선.
- **codex 바이너리**: ChatGPT.app 동봉본(0.155.0-alpha.16) 사용 — homebrew codex-cli 0.157.0은 read-only 샌드박스 파일읽기 결함(note 참조).
- dispatch message의 `#opts` 오버라이드는 model/reasoning/workspace/continue_session만 허용(sanitize 2중 검사).
- 2026-09-26 이전 dispatch(codex 회차들, 설계 #32 포함)는 pinning 전이라 config.toml 기본값(luna/max)으로 실행된 기록.

## TT 등록 확장 (예정, 별도 이슈)

TT `/agents`에도 모델/추론 메타데이터를 등록해 보드에서 계층이 보이게 하는 개선 — 이 표가 그 데이터 시드.
