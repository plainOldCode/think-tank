# CHANGELOG — think-tank 배포 장부

규약(XV84 이후, NCWZ 확정): **서버 배포 시마다 이 파일에 1행 추가** —
`YYYY-MM-DD cli X.Y.Z openapi <8자리> — 내용 (커밋)`.
배포 지점은 annotated tag `deploy-cli<버전>-openapi<해시>`로 고정한다
(grep 가능성 우선). openapi 해시는 `tt version` 또는
`curl -s $URL/openapi.json | shasum -a 256 | cut -c1-8`으로 측정한다.

## 배포 이력

- 2026-10-14: cli 2.5.3, openapi **e66cc1b6** — XV84(M4W99A6Z) 병합 `89ef962`: `/settings/review-routing` 엔드포인트 신설(런타임 리뷰어 라우팅), 웹 agents 패널 편집기. **8a3a18af → e66cc1b6 전환**. 태그 `deploy-cli2.5.3-openapie66cc1b6`.
- 2026-10-14: cli 2.5.3, openapi **8a3a18af** — RQHD(M4W0VHZB) 병합 `dc1c850`: approval_path 컬럼(응답 필드 추가 — openapi 불변), ZDNH(M4TW2J3V) 병합 `1bf630f`: probe 병합 게이트(스키마 불변). 태그 `deploy-cli2.5.3-openapi8a3a18af` — **기준 시점**.
- 2026-10-13: cli **2.5.3** 재설치 — `--head` 전체 SHA 강제 등 보고 계약 정합. openapi 8a3a18af 유지.
- 2026-10-13: cli **2.5.1** 재설치 — 이전 설치 대체. openapi 8a3a18af.
- 2026-10-12: cli **2.4.0** — 최초 버전 고정 이전 설치. openapi 8a3a18af.

## 롤백 기준 (P3-2 게이트 연동)

**발동 조건**: `tt version`의 openapi 해시가 이 CHANGELOG 최신 배포 행과 불일치
(스키마 드리프트 — 서버·CLI 버전 엇갈림, P3-2 게이트가 이를 감지한다).

**절차**:
1. 롤백 대상 = CHANGELOG 최신 행의 **이전 행**에 대응하는 태그.
   (`git tag -l "deploy-*"` → 태그 메시지의 cli 버전·openapi 해시·배포일 확인)
2. mini 운영 서버 롤백:
   ```bash
   ssh mini 'cd ~/tt-repo && git fetch --tags origin && git checkout deploy-cli<버전>-openapi<해시>'
   ssh mini 'git archive deploy-cli<버전>-openapi<해시> | tar -x -C /tmp/rollback && rsync -a --delete /tmp/rollback/server/ ~/think-tank/server/ && launchctl kickstart -k gui/$(id -u)/com.tt.server'
   ```
   (DB는 무손상 — 미지 컬럼·엔드포인트는 이전 서버가 무시한다. 스키마 마이그레이션은
   전진 전용이므로 롤백 후 재배포 시 재적용된다.)
3. **롤백 검증**:
   - `tt version` → cli 버전 + openapi 해시가 롤백 태그와 일치
   - `tt health` → `ok: think-tank ok`
   - `tt list` 1건 조회 — 읽기 경로 정상
4. 롤백 후 이 CHANGELOG에 롤백 사유·시각을 1행 추가하고, 원인 커밋은 revert PR로 정리한다.

**현재 참조 지점**: 롤백 시 `deploy-cli2.5.3-openapi8a3a18af`(dc1c850) →
openapi 8a3a18af 상태로 복귀. 현재 배포는 `deploy-cli2.5.3-openapie66cc1b6`(89ef962).
