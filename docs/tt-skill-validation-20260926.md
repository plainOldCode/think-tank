# 9YRD 클라이언트 스킬 준비와 검증

대상: M3CE450N-NMFK / M3CE4SA2-9YRD, `feat/verification-first-contract`.
이 문서는 서버·CLI·runner 커밋 `faccc79`와 운영 DB 복제 검증/수정 `c06be88` 이후의 작업이다.
앞선 두 커밋에는 각 머신에 설치된 클라이언트 스킬 갱신이 포함되지 않았다.

## 구현한 범위

- `skills/tt/SKILL.md`: v1.6.0 클라이언트 지침. 조회와 수령 구분, lease, 수령 계약 읽기,
  TDD/대체 검증, 완료 보고, stale 회차, 구버전 서버와 승인 예외를 다룬다.
- `skills/tt/references/api.md`: 현재 브랜치 `server/static/api.md`와 바이트 단위로 같은 사본.
  서버 문서를 변경하면 사본을 함께 갱신한다. 원문은 서버 파일이며 다른 계약을 독립 편집하지 않는다.
- README에 스킬 패키지와 적용 범위를 연결했다. 애플리케이션 실행 코드는 변경하지 않았다.

주석 `tt-work-contract:start/end`는 `server/work_contract.py`가 지침을 추출하는 경계다.
지침과 보고 필수 정책을 해시해 버전을 만들고, claim/pull 때 이슈에 고정한다.
dispatch payload에도 전달되며 저장소 runner는 신규·재개 프롬프트에 넣는다.
일반 클라이언트는 SKILL.md를 발견·선택·읽은 다음 수령 응답의 지침을 읽고 적용해야 한다.
주석만으로 스킬 설치, 자동 호출, 외부 어댑터의 프롬프트 주입이 일어나지는 않는다.

스킬은 수행 지침, runner는 전달 경로, 서버는 보고·전이 검사다.
`TT_REQUIRE_REPORT=1`이어도 실제 테스트 선작성이나 증거 진위는 별도 검증 대상이다.
보고서에는 이 한계를 유지하며 독립 LLM 행동 평가를 통과했다고 주장하지 않는다.

## 대체 검증 결과

이번 변경은 문서/스킬 패키지이므로 TDD 회귀를 새로 작성하는 대신 격리 패키지 검사와
실제 로컬 CLI 보고 왕복을 수행했다. 두 Uvicorn은 운영 DB 복제본이며 외부 송신 차단이 유지됐다.

- 공식 `quick_validate.py`: `Skill is valid!`. PyYAML은 `uv --with pyyaml`의 임시 환경으로 제공했다.
- 분리된 디렉터리에 패키지를 복사한 뒤 reference 링크 존재와 서버 원문 동일성을 확인했다.
- 두 로컬 서버 `/api.md`와 번들 reference 일치, 호환/필수 정책 및 수령 계약 일치를 확인했다.
- 실제 `tt done/verify --report ... --json`: 정상 alternative 보고 접수와 저장 필드 확인,
  보고 누락 review, 재수령 후 오래된 보고 거부, 재검증 후 새 회차 접수,
  실제 잘못된 스킬 파일의 validator 실패를 failed 보고로 제출 → review,
  failed verify 거부 → 올바른 패키지 재검증 후 done을 확인했다.
- 위 검사 **30개 통과**. 이는 검사 항목 수이며 독립 LLM 평가나 별도 pytest 30개를 뜻하지 않는다.
- `runner/test_tt_runner.py`의 신규·재개 계약 입력 및 이전 payload 호환: **2 passed, 27 deselected**.
  subprocess를 대체하는 단위 검사이며 실제 LLM은 실행하지 않았다.
- 최종 실행의 합성 카드 4개와 검증 도구 보정 중 만든 카드 2개는 로컬에서 모두 archive했다.
  도구는 최초에 서버가 보충한 선택 필드 때문에 전체 JSON 동등 비교를 실패했고,
  이어 archive CLI의 사람이 읽는 출력을 JSON으로 가정했다. 제출 필드별 비교 및 archive 후
  `show --json` 재조회로 고쳤다. 애플리케이션 수정 없이 위 최종 검사를 통과했다.

재현 명령:

```sh
uv run --no-project --with pyyaml python <skill-creator>/scripts/quick_validate.py skills/tt
cmp server/static/api.md skills/tt/references/api.md
python -m pytest -q -p no:cacheprovider runner/test_tt_runner.py \
  -k 'contract_reaches_new_and_resumed_agent_prompts or legacy_payload_keeps_its_prompt'
```

실행 스크립트·패키지 사본·보고 JSON·results.json은 로컬 임시 디렉터리
`tt-skill-eval-20260926-i75iso70`에 보관했다. `/private/tmp/tt-skill-eval-current-path`가 전체 경로를 담는다.
기존 116개 전체 회귀는 `c06be88`의 실행 코드 검증이며 이번 문서 변경에서 다시 실행한 수치가 아니다.

## 설치 현황과 남은 완료 조건

확인 시 운영 서버 `/work-contract`는 404이며 방법론 브랜치가 아직 배포되지 않았다.
9YRD의 완료 조건인 **실서버와 동일한 계약으로 각 머신 설치**는 아직 충족하지 않았다.

| 대상 | 이번 조회 결과 | 이번 변경 |
|---|---|---|
| 현재 Mac `~/.agents/skills/tt/` | v1.5.0, reference 있음 | 설치 파일 유지, 저장소에서 갱신본 준비 |
| 현재 Mac `~/.agents/skills/think-tank/` | 동일 이름의 별도 v1.5.0 파일, reference 없음 | 설치 파일 유지; 동기화 시 reference도 필요 |
| mini `~/.agents/skills/{tt,think-tank}/`, `~/.codex/skills/{tt,think-tank,think-tank-board}/` | 조회한 경로에 없음 | 미설치; 실제 소비 agent의 탐색 경로 확인 필요 |
| mini `~/.local/bin/tt` | 없음 | 미설치 |
| studio 등 다른 머신 | 이번 작업에서 미조회 | 완료로 추정하지 않음 |

후속 담당자의 진행 순서:

1. 3C0X 문서·소스 최종 검토 후 DJJY에서 배포할 커밋을 확정한다.
2. mini 배포 후 `/api.md`, `/api.html`, `/work-contract`, health와 적용 보고 정책을 검증한다.
3. 각 머신의 실제 skill 탐색 경로와 공유/별도 디렉터리 여부를 확인한다. 기존 SKILL.md,
   references, CLI를 백업하고 이 패키지와 배포 서버의 CLI를 적용한다. 패키지의 `SKILL.md`는
   가상 주소(`base_url`)를 포함하므로 설치/동기화 시 실제 `TT_HOST`로 치환하거나 `TT_URL`을
   지정하도록 안내한다. 별도 alias 디렉터리도 동일한 reference를 갖추게 하되 사용자 고유 지침을 보존한다.
4. 설치 reference와 **실서버** `/api.md`의 동일성을 확인한다. 로컬 합성 이슈로 CLI 보고,
   구버전 호환, 해당 클라이언트의 스킬 로드/runner 입력을 검증한다.
5. 머신별 변경 경로·파일 해시·적용 계약 버전·CLI 검증 결과를 9YRD에 기록한다.
   실제 미적용 머신이 남으면 범위를 명시하고 done으로 추정하지 않는다.

이번 작업은 준비와 로컬 검증까지이며 운영 서비스·각 머신의 설치 파일·runner는 갱신하지 않았다.
9YRD는 준비 커밋과 근거를 남기고 DJJY 의존 blocked로 유지한다.
