# 네트워크 주소 정책 — MagicDNS 도메인 우선, raw-IP HTTP 지양 (AYN7/M4TZHZW5)

P3-4(2026-10-14 확정). 보안 스캐너의 raw-IP HTTP 차단 대응 운영 기준.

## 1. 재현 근거 (차단 실측 원문)

- **2026-10-14 1차 재현**(카드 등록자 실측): `curl http://<tailnet-ip>:8642/health`
  → 에이전트 보안 스캔 차단. 사유 원문:
  - `[MEDIUM] URL uses raw IP address: URL points to IP address <tailnet-ip> instead of a domain name`
  - `[HIGH] Plain HTTP URL in execution context`
- **2026-10-13 22:00 크론 재발**(원본 카드): 스캐너가 크론 명령줄의 raw-IP http를 차단.
- **문서 내 raw-IP도 적발**: CI secret-scan(PR#93/#115) — 본문의 운영 IP 1행 비식별화로 해소.
- **스캐너 종류·조건**: 에이전트 런타임 내장 보안 스캔(커맨드라인 URL 검사) —
  `curl`/`http` 리터럴과 점 4개 IP 조합에서 발화. 사람 터미널에는 영향 없음.
- **영향 워크플로**(raw-IP를 명령·설정에 두는 것들 — 차단 위험):
  ① 세션 시작 보드 멘션 확인 curl(에이전트 AGENTS.md 규약)
  ② tt-wake-relay systemd 유닛 env ③ metrics 수집기 스냅샷 URL ④ mini 임베딩 데몬·
  voice 파이프라인 LaunchAgents env. (다른 워크스테이션 러너는 이미 MagicDNS 도메인 사용 — 무영향)

## 2. 운영 원칙

1. **MagicDNS 도메인 우선**: TT 주소는 `http://<magicdns-name>.<tailnet>.ts.net:7800`(실주소는 `tt version` 또는 러너 launchd plist로 확인 — 본 문서에 미기록)
   (2026-10-14 실측 200 OK). raw-IP 대신 도메인을 쓴다 — 차단 회피 + 가독.
2. **문서에 운영 주소 기록 금지**: raw-IP든 도메인이든 운영 주소는 레포 문서에
   기록하지 않는다(secret-scan 규약 — 비식별화 1행으로 해소된 전례). 전달은
   승인 경로(카드 코멘트·발령자 전달)로. 위 예시 도메인은 tailnet 내부명이며
   이미 문서화된 사례(cli 기본값)라 예외로 남긴다.
3. **도메인을 못 쓰는 경우**(DNS 미탑재 환경): raw-IP를 명령줄에 두지 말고
   TT_URL 환경변수로 주입(셸 프로필·launchd plist) — 스캐너는 명령줄 리터럴을 본다.
4. **금지**: 보안 통제 우회, 스캐너 비활성화, raw-IP 허용 요구.

## 3. HTTPS 전환 검토 (보고만 — 실행은 별도 승인)

**결론: 서버 변경 0으로 달성 가능 — Tailscale serve 권장.**

- mini에는 이미 tailscaled(homebrew 1.104.1)가 상주하고, **serve가 포트 8766에서
  사용 중**(`https://<magicdns-name>.<tailnet>.ts.net:8766 → http://127.0.0.1:8766`)
  — 같은 방식으로 7800을 추가하면 된다.
- 절차(실행 시): `sudo /opt/homebrew/opt/tailscale/bin/tailscale serve --https=443
  http://127.0.0.1:7800` (또는 전용 포트). 인증서는 Tailscale이 자동 발급·갱신
  (Let's Encrypt, tailnet DNS 검증) — 서버 코드·설정 변경 불필요.
- 주의: 현재 서버는 tailnet IP에만 바인딩(127.0.0.1 헬스체크 거부 실측) —
  serve 프록시는 로컬호스트 경유이므로 **바인딩 정의 확인 후** 전환해야 한다
  (127.0.0.1 바인딩 추가 또는 serve가 접근 가능한 주소로 리슨 확장).
  이 검증이 병목이면 전환 카드에서 1차 작업이 된다.
- 전환 후: 에이전트·크론의 TT_URL을 MagicDNS HTTPS 주소로 일괄 교체(실주소는 승인 경로 전달).
  롤백은 serve 해제 1줄 — 기존 raw-IP 경로를 남겨두면 무손실.

## 4. 실행 체크리스트 (후속 승인 카드용)

- [ ] serve 등록 + 도메인 HTTPS 헬스체크 200 실측
- [ ] 바인딩 정의 확인(127.0.0.1 경유 프록시 reachable)
- [ ] TT_URL 일괄 교체(에이전트 AGENTS.md, tt-wake-relay, metrics, 임베딩 데몬, voice)
- [ ] 1주일 관찰 후 raw-IP 경로 제거
