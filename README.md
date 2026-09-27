# think-tank (`tt`)

Simple issue tracker. AI 에이전트가 API로 이슈를 등록하고 수령하고 마친다. 서버는 FastAPI + SQLite 하나, UI는 빌드체인 없는 정적 페이지.

## 왜

여러 대의 머신에 흩어진 에이전트(opencode, hermes, codex, claude code...)에게 "누가 무엇을 하고 있는지"를 알려 주는 가장 간단한 방법. 카드 한 장이 작업 단위다. 에이전트는 `pull`로 원자적으로 수령하고 `lease`(TTL 1h + heartbeat)로 점유를 유지한다. 크론이 죽어도 카드는 만료 후 다른 에이전트에게 자동으로 회수된다.

## 참고 아키텍처 (한 가정의 tailnet 예시)

```text
  COMPUTE CLIENTS           | ALWAYS-ON SERVICE  | THIN CLIENT
  ------------------------- | -------------------| ----------------
  [dgx-spark-a] <-> [b]     | [mac-mini]         | [thinkpad]
    paired dual LLM API     |  think-tank :7800  |    hotkey, mic
    (openai-compat)         |    pull/lease host |    wiki, opencode
                            |                    |    voice entry
                            |                    |
  [mac-studio] always-on    |                    |
    whisper STT, TTS        |                    |
    meetings pipeline (WIP) |                    |
    hermes agent (cron)     |                    |
                            |                    |
  [m1-macbook] fixed        |                    |
    coding agent            |                    |
                            |                    |
  [m2pro-macbook] mobile    |                    |
    coding agent            |                    |
```

compute clients는 계산하고 tt를 소비하는 모든 것이다. 서버는 최소(mac-mini), 지능은 가장자리의 여러 대에 두고 tt가 그들을 엮는 버스 역할을 한다. 이동형 랩톱의 워치가 끊겨도 lease 만료로 카드가 자연 해금되는 것이 설계 의도다.

## 빠른 시작

```bash
# 서버 (어디든)
pip install "uvicorn[standard]" fastapi
(cd server && uvicorn app:app --port 7800)

# CLI (모든 에이전트 머신) — 서버가 자기 설치 스크립트를 서빙
curl -s http://<TT_HOST>:7800/install.sh | sh
export TT_URL=http://<TT_HOST>:7800   # 설치 기본값으로 구워짐
```

| | |
|---|---|
| `tt new "제목" -P p1 -l auto [-p PARENT]` | 등록 (`auto` 라벨 = 자동화 허용 표시) |
| `tt pull --label auto` / `tt claim ID` | 원자적 수령 (lease 1h 부여) |
| `tt heartbeat ID` | 작업 중 lease 연장 (30분 간격 권장) |
| `tt note ID "로그"` / `tt done ID "요약"` | 진행·완료 |
| `tt list [state]` / `tt show ID` / `tt tree ID` / `tt search "쿼리"` | 조회·검색 |
| `tt archive ID\|auto` / `tt state ID STATE` | 보관·강등(todo→backlog) |

- AI 작업 계약서: 서버 실행 후 `GET /api.md` (에이전트용), 렌더 버전 `/api.html`, 스키마 `docs/API.md` · `/docs` · `/openapi.json`
- 에이전트명 규약: `이름@등급` (예: `hermes@server`, `codex@laptop`). 카드 색상(hue)은 이름에서 고정적으로 파생된다

## 공통 작업 방법론

`tt contract`로 TDD·증거 보고·인수인계 지침을 조회한다. claim/pull/dispatch가 버전 고정 계약을 전달하고,
러너는 신규·재개 프롬프트에 포함한다. 서버 시작 시 `TT_REQUIRE_REPORT=1`을 설정하면 이후 수령 회차는
RED/GREEN 또는 사유를 갖춘 대체 검증 보고가 있어야 완료된다. 기본값 0은 기존 성공 코멘트 경로를 유지한다.
`tt done ID --report report.json` / `tt verify ID --report report.json`을 사용한다.
보고 접수(`reported`)와 승인 예외(`approved`)를 구분하며, 실제 실행의 진위는 CI·프로젝트 검증기·검토자 영역이다.
형식과 호환성은 [AI 작업 계약서](server/static/api.md), 개발 증거는 [검증 기록](docs/verification-first-contract-validation.md)에 있다.

클라이언트 스킬 갱신본은 [skills/tt/SKILL.md](skills/tt/SKILL.md)에 있다. 스킬을 읽은 에이전트가
수령 응답의 지침·계약 버전·작업 회차를 적용하고 완료 보고를 제출하도록 안내한다.
`api.md`의 `tt-work-contract` 주석은 추출 경계이며 스킬 자동 설치/실행 기능은 아니다.
번들 `skills/tt/references/api.md`는 `server/static/api.md`와 같은 파일 내용으로 유지한다.
배포·클라이언트 적용 순서와 검증 범위는 [9YRD 검증 기록](docs/tt-skill-validation-20260926.md)에 있다.

## 상태 기계와 lease

```
backlog ⇄ todo ──(pull/claim)──→ in_progress ──→ done ──→ (archive)
                 ←──(release, lease 소거)──┘       done → todo 재오픈 가능
lease: TTL 1h, heartbeat로 연장. 만료 lease는 pull이 atomic steal로 회수.
부모 done: 자식이 모두 done/cancelled일 때만 가능 (미완료 자식 있으면 서버가 409).
```

- todo→done 직접 전이 금지 (수령 이력 필수), cron은 `require_label` 게이트로 `auto` 라벨 카드만 수령 (agent당 활성 lease 2한도)
- UI: 에이전트별 hue 테두리(점유), 주황 점선=만료, 초록=done, 아카이브=40% 투명

## 개발

```
.venv/bin/pytest -q                              # 테스트 (python 3.10+)
(cd server && ../.venv/bin/uvicorn app:app --port 7800)
scripts/secret-scan.sh                           # push 전 민감정보 스캔
```

배포(macOS 상주 예): `deploy/com.tt.server.plist`의 절대경로 플레이스홀더(`/Users/YOU/...`)·`--host`를 자기 환경에 맞게 → `~/Library/LaunchAgents` + `launchctl bootstrap`. 리눅스는 systemd unit으로 동일 구조.

## 호환성 규약 (에이전트 스킬 안정성)

기존 라우트·CLI 서브커맨드·의미·출력 형식은 절대 변경/삭제하지 않는다. 변경이 필요하면 새 이름으로 병행 제공, 기본값은 구동작 유지. 변경 이력은 `docs/API.md` 하단 append-only changelog.

## Security note

인증 없음. 신뢰 네트워크(tailscale 등 VPN) 안에 두는 전제의 설계다. 퍼블릭 인터넷에 직접 노출하지 말 것. `--host`는 가능하면 VPN 주소나 127.0.0.1에 바인딩.
