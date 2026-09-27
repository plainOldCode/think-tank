# 방법론 계약 구현·검증 기록

- 작업일: 2026-09-26 (Asia/Seoul)
- 브랜치: `feat/verification-first-contract`, 기준 `938c4f8e95b9df0ae0f64389e363ee1bd53fd906`
- 대상: TT M3CE450N-NMFK 검토에서 정한 공통 TDD 지침 전달과 완료 보고 게이트.
- 구현 단계 범위: 로컬 구현·검증. 구현 중에는 운영 TT 이슈를 claim/done하거나 운영 서비스를 변경하지 않았다. 이후 사용자 요청으로 부모·하위 이슈의 진행 댓글과 상태를 동기화했다.

## 완료 조건과 결과

| 조건 | 결과 |
|---|---|
| 동일한 공통 계약을 claim/pull/dispatch에서 전달 | API 회귀 테스트 통과. 원문과 정책을 해시한 버전, 작업 회차 저장 |
| 신규·재개 러너의 실제 입력에 지침 포함 | 단위 테스트 및 격리 tmux 프로세스의 출력으로 확인. 원래 메시지·`#opts` 보존 |
| 실패·미실행·빈 마커·URL만으로 성공 판정하지 않음 | 회귀 테스트 통과. 호환 텍스트 경로는 보수적 휴리스틱 |
| 보고 필수 모드에서 TDD/대체 검증 보고 검사 | 필수 필드·결과·회차·계약 버전 검사. `warn/off`에서도 보고 필수 정책 유지 |
| 재오픈/범위 변경/새 수령에서 과거 증거 무효화 | 회귀 테스트 통과. 만료 lease 회수 및 한 PATCH 안의 범위 변경 포함 |
| 기존 DB 데이터 보존 | 이전 스키마 fixture의 반복 migration, 기존 값 보존, integrity_check=ok |
| 보고와 승인 예외 구분 | `reported`/`approved`/`legacy`/`unverified`. Chrome 로컬 보드의 표시 확인 |
| CLI JSON과 오류 경로 | 로컬 실제 HTTP+CLI 테스트: review JSON, 단일 PATCH, 코멘트 실패 시 중단, 공백 경로 report 파일 |

## RED → GREEN

테스트를 먼저 작성하고 다음 실패를 확인한 뒤 해당 구현을 추가했다.

| 단계 | 구현 전 | 구현 후 |
|---|---|---|
| API 계약·완료 회귀 25건 | 25 failed (`tt-contract-red.log`) | 25 passed |
| 러너 신규·재개 계약 전달/레거시 2건 | 1 failed, 1 passed (`tt-contract-runner-red.log`) | 전달 관련 4 passed(API 포함) |
| 계약 전달 구현 후 완료 판정만 분리 | 23 failed (`tt-completion-red.log`) | 위 API 25 passed에 포함 |
| 실제 CLI와 인접 회귀 | 7 failed, 30 passed (`tt-cli-red.log`) | 37 passed |
| 보고 필수 정책·마이그레이션 | 1 failed, 2 passed (`tt-strict-red.log`) | 3 passed |

실패 원인은 미구현 응답 필드, 누락된 프롬프트 계약, 부정확한 완료 판정, 중복 PATCH 및 JSON 형식 위반이었다.
로그 파일은 개발 머신 `/private/tmp/`에 보관했다. 이 파일들은 임시 자료이며 위 표가 저장소 내 요약이다.
테스트 실행은 Python 3.13.15의 격리 venv에서 수행했다. 실행 전후 로그·산출물은 운영 DB와 분리했다.

## 최종 검증

```sh
python -m pytest -q -p no:cacheprovider tests runner/test_tt_runner.py
# 111 passed, 2 warnings, 18.18s (전체 회귀)

python -m pytest -q -p no:cacheprovider tests/test_work_contract.py tests/test_api.py::test_transitions
# 38 passed (보고 필수 warn/off 및 재시작 후 정책 고정 추가 검증 포함)

SMOKE_TELEGRAM=0 python scripts/smoke_9f0s.py
# PASS: 실제 Uvicorn + CLI + 루프백 알림 수신기; Telegram 발송 없음

TT_REPO="$PWD" TT_TMUX_SOCKET=tt-contract-smoke-20260926 python runner/smoke_tt_runner.py
# PASS: 22개 관찰 항목 true. 신규·재개 입력 전달, 중복 방지, 게이트,
# 실패/대기 분류, 환경변수 격리, stall 종료 포함. 실제 LLM 대신 echo/fake CLI 사용.

bash -n cli/tt
# PASS
# index.html script 추출 후 node --check: PASS
# git diff --check: PASS
```

전체 회귀 뒤 정책 고정·warn/off 조합 3건을 추가했다. 당시 테스트 구성은 총 114건이며,
변경된 테스트 모듈은 위 38건의 별도 실행으로 검증했다. 두 warning은 테스트 라이브러리의
httpx/BlockingPortal 사용 중단 예고이며 기능 실패는 없었다.

Chrome에서 로컬 fixture 보드의 `결과 보고`와 `승인 예외` 표시를 확인했다.
로컬 서버·격리 tmux는 검증 후 종료했다.

## 강제 범위와 남은 한계

- 기본은 공통 지침 전달 + 기존 성공 코멘트 호환. `TT_REQUIRE_REPORT=1`로 새 수령 회차의 구조화 보고를 필수로 한다.
- 강제할 수 있는 것은 제출 형식, 보고된 결과, 계약 버전·회차, 상태 전이다. 에이전트가 실제로 테스트를 먼저 작성했는지, 출력이 진짜인지, 코드 품질이 개선됐는지는 이 기능으로 입증하지 않는다.
- `force_done`/`close`는 호환 승인 예외이며 actor 인증이 없다. 이것을 사람의 신원이 확인된 승인으로 해석하면 안 된다. 신뢰된 CI 검증기·인증·예외 권한 분리는 후속 범위다.
- 외부 webhook 수신기는 추가된 계약 필드를 실행 입력에 넣어야 한다. 이 저장소의 runner만 그 주입을 보장한다. 각 머신의 설치된 CLI/runner/skill은 자동 갱신되지 않는다.
- 실제 LLM의 방법론 준수율 비교, 프로젝트별 테스트 게이트, 토큰·사람 검토 시간의 개선 측정은 수행하지 않았다. 이번 검증은 API/CLI/러너 동작과 지침 전달 검증이다.
- 구현과 이 검증 기록은 `feat/verification-first-contract` 브랜치의 커밋에 함께 보관한다. 원격 push와 운영 배포는 별도 단계다. 변경 내용과 실행 지침은 [작업 계약서](../server/static/api.md)와 [API 변경 이력](API.md)에 있다.

## 운영 DB 복제본 검증 후속

2026-09-26 운영 SQLite 스냅샷으로 마이그레이션·두 모드의 실제 HTTP 검증을 수행했다.
기존 작업의 정책 소급 적용 문제를 회귀 테스트로 재현하고 수정했으며, 최종 전체 회귀는 116건 통과했다.
환경·보존 해시·격리·재시작·실행 중인 로컬 주소는 [배포 전 검증 기록](predeployment-sqlite-validation-20260926.md)에 있다.
