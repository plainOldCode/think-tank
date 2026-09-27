# 운영 SQLite 복제본 배포 전 검증 — 2026-09-26

## 대상과 판정

`feat/verification-first-contract`의 `faccc79`를 운영 DB 복제본으로 검증했다.
업그레이드 전 진행·검토 카드가 보고 필수 정책에 막히는 문제 1건을 발견해,
회귀 테스트를 먼저 실패시킨 뒤 이 문서와 함께 커밋한 수정으로 해결했다.
수정 후 데이터 보존, API/CLI, 재시작 및 로컬 화면 검증을 통과했다. 운영 배포는 수행하지 않았다.

## 스냅샷과 격리

- 운영 소스 HEAD: `938c4f8e95b9df0ae0f64389e363ee1bd53fd906`.
- 스냅샷: `20260926T111141`, SQLite 온라인 backup API. WAL이 활성화된 운영 파일을 단순 복사하지 않았다.
- 크기: 602,112 bytes. SCP 전후 이름·크기·SHA-256 일치.
- SHA-256: `e00facb1efcd22d8f6b3559e2070631a382a190928503d3a888a1cee627f9383`.
- 원본 내용: 이슈 162, 댓글 430, 에이전트 5, dispatch 28. `sqlite_sequence` 2행도 비교했다.
- 로컬 보관: `~/.local/share/think-tank-predeploy/20260926T111141/`. 디렉터리 0700, 원본 `snapshot.db` 0400.
- 원본 스냅샷은 보존하고 `compat.db`, `required.db`에 각각 마이그레이션했다. 두 번 실행해 멱등성을 확인했다.
- 모든 기존 컬럼·행의 정렬된 내용 해시가 스냅샷과 같았다. 새 계약 관련 컬럼 5개만 추가됐다.
- 보존 비교 후 **검증용 DB에만** 복제된 에이전트의 활성·hook 플래그를 끄고 secret을 비웠으며, endpoint를 비활성 localhost 주소로 바꿨다.
- 두 서버는 loopback으로만 바인딩하고 macOS `sandbox-exec`로 외부 연결을 차단했다. 실제 API dispatch로 TEST-NET 주소 연결이 `Operation not permitted`로 거부되는 것까지 확인했다. 알림·release 테스트는 임시 로컬 HTTP 수신기에만 전달했다.
- 종료 확인 시 운영의 다섯 테이블 내용 해시가 최초 스냅샷과 모두 같았고, 원격 HEAD도 그대로였다. 운영 DB·서비스·에이전트에 테스트 변경을 적용하지 않았다.

## 런타임

| 항목 | 운영 | 로컬 검증 |
|---|---|---|
| Python | 3.14.7 | 3.14.7 |
| FastAPI / Pydantic | 0.141.1 / 2.13.5 | 동일 |
| Starlette / Uvicorn | 1.6.0 / 0.53.0 | 동일 |
| SQLite | 3.53.4 | 서버 실행 3.53.1; 별도 마이그레이션 보존 검사는 Python 3.13.15 + SQLite 3.53.4에서도 수행 |

Python과 주요 서버 패키지 버전은 맞췄다. Python 배포판/빌드 및 SQLite patch 버전까지 완전히 같은 환경은 아니다.
실제 LLM이나 외부 에이전트를 실행한 검증도 아니다.

## 실행 결과

| 검사 | 결과 |
|---|---|
| SQLite integrity / foreign key | 원본 및 두 작업 DB `ok` / 위반 0 |
| 기존 데이터 보존 | 모든 기존 필드·행 해시 일치, 반복 migration 통과 |
| API 전체 읽기 | 두 모드 각각 이슈 162개·댓글 430개·루트 트리 95개 및 blocked 상세 정상 |
| 기존 verified 기록 | 두 모드 각각 2건을 `legacy`로 노출 |
| 문서·설치 스크립트 | `/api.md`, `/api.html`, `/openapi.json`, `/install.sh` 및 계약 버전 확인 |
| 실제 HTTP·CLI 시나리오 | 호환 모드 15개 + 보고 필수 모드 15개 통과 |
| 전체 회귀 | Python 3.14.7에서 **116 passed, 2 warnings, 18.65s** |
| 재시작 | 두 DB 각각 168개 이슈의 필드·보고·계약과 agent 목록이 재시작 전후 일치 |
| Chrome | 복제 보드, 기존 상세·댓글·자식, 방법론 및 업그레이드 호환 조항 렌더 확인 |
| 운영 영향 | 운영 테이블 해시 모두 동일, 외부 연결 차단 확인, 운영 배포 없음 |

HTTP·CLI 시나리오는 계약 전달, 일반 댓글/필수 보고 정책, JSON 출력, 재오픈·범위 변경의 증거 무효화,
오래된 회차 거부, 실패·미확정 보고 거부, 부모/자식 완료 가드, 승인 예외 표시,
필터 지정 pull·heartbeat·만료 lease 회수, 비활성 에이전트 dispatch 거부,
로컬 dispatch·notify·release 및 외부 송신 차단을 포함한다.
각 모드의 합성 테스트 카드 6개는 검증 후 archive 처리했다. 따라서 DB 전체는 168개이며 기본 보드에는 테스트 카드가 나오지 않는다.
보고 필수 복제본의 기존 review 카드 1개는 업그레이드 재현 과정에서 로컬에서만 완료됐다.
두 warning은 테스트 라이브러리의 httpx/BlockingPortal 사용 중단 예고다.

## 발견한 문제와 수정

`work_contract=null`, `execution_attempt=0`인 기존 `in_progress`/`review` 행에는 새 보고 계약이 없다.
그런데 `requires_report()`가 빈 계약 대신 서버의 현재 정책을 사용하여,
`TT_REQUIRE_REPORT=1`로 시작하면 기존 검토 카드의 verify가 422로 막혔다.
새 계약 버전에 맞는 보고를 제출할 회차도 없어 재수령 없이는 마감할 수 없었다.

- 실제 복제 카드에서 수정 전 verify **422**, 수정 후 **200/done**을 확인했다.
- `test_upgrade_keeps_unpinned_work_compatible_until_next_claim` 2건을 먼저 추가: **2 failed**.
- 빈 계약은 기존 코멘트 호환 정책을 사용하도록 최소 수정: 관련 정책 검사 **6 passed**.
- todo로 되돌린 뒤 claim하면 새 보고 필수 계약이 적용되고, 일반 성공 코멘트만으로 완료할 수 없는 것도 검사했다.
- 최종 전체 회귀 116건 통과. 새로 수령한 작업의 강제 범위나 기존 승인 예외는 변경하지 않았다.

재시작 검증 도구에서 종료 직후 포트를 bare bind해 TIME_WAIT를 종료 실패로 오인하는 문제가 있었다.
서버 로그의 정상 종료와 listener 부재를 확인한 뒤 검증 도구의 포트 확인에 SO_REUSEADDR를 적용했다.
애플리케이션 종료 결함은 아니며 수정한 도구로 재시작·데이터 유지 검증을 마쳤다.

## 로컬 확인과 증거

- 호환 모드: `http://127.0.0.1:17800/`
- 보고 필수 모드: `http://127.0.0.1:17801/`
- 두 서버는 검증 종료 후에도 사용자 확인을 위해 실행해 둔다. 자동 시작 서비스는 설치하지 않았다.
- 상태/중지/재실행: `python3 ~/.local/share/think-tank-predeploy/20260926T111141/manage.py status|stop|start`에서 원하는 명령 하나를 사용한다.
- 같은 디렉터리의 `manifest.json`, `migration-report.json`, `readback-report.json`, `integration-report.json`,
  `restart-report.json`, `production-after-check.json`, `upgrade-red.log`, `upgrade-green.log`, `pytest-full-python314.log`에 근거가 있다.
- 스냅샷·작업 DB·개인 경로가 담긴 실행 도구·원본 로그는 저장소 밖에 보관했다. 이 문서는 공개 가능한 결과 요약이며 DB 내용을 포함하지 않는다.
- 이 결과는 로컬 배포 전 검증이다. 운영 배포·실제 외부 agent의 방법론 준수·CI 검증기·승인자 인증을 입증하지 않는다.
