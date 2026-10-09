"""think-tank 서버 — 앱 조립만 담당 (카드 M3ZW8DY7-M14N).

구조: 라우팅은 routers/(meta, issues, agents), 스키마는 models.py,
공유 로직은 service.py, 상수·env는 config.py.
"""
import os
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import hashlib
import re as _re

import config
import probe
import service
from routers import agents, issues, messages, meta
from service import Ctx
from work_contract import current_contract


def _asset_version(rel: str, static_dir: str | None = None) -> str:
    """정적 파일 내용 해시(?v=) — 내용이 바뀌면 URL이 바뀌어 낡은 캐시와 섞이지 않는다(M43KDKWQ-6Y7Z)."""
    base = static_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    try:
        with open(os.path.join(base, rel), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:8]
    except OSError:
        return "0"


def _versioned_html(html: str, static_dir: str | None = None) -> str:
    """로컬 js/css 참조에 내용 해시 ?v= 부착 — 이미 저장된 구버전 JS 갱신(codex 2차 지적)."""
    base = static_dir

    def sub(m):
        attr, url = m.group(1), m.group(2)
        return f'{attr}="{url}?v={_asset_version(url.lstrip("/"), base)}"'

    return _re.sub(r'(src|href)="(/(?:js|css)/[^"?]+)"', sub, html)


from service import _notify_workers, _notify_reg_lock  # noqa: F401 (테스트가 app 경유 참조)


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="think-tank")
    app.state.db_path = db_path  # 하위호환: 테스트/운영 스크립트가 직접 읽음
    app.state.ctx = Ctx(db_path, current_contract())
    app.include_router(meta.router)
    app.include_router(issues.router)
    app.include_router(agents.router)
    app.include_router(messages.router)

    # probe 내장화 (M3ZW8E8A-ZK3G): TT_PROBE_INTERVAL>0 → 데몬 스레드로 자율 스케줄러 구동.
    # 미설정이면 off(테스트·로컬 안전). mini 운영은 launchd com.tt.dispatchd 대체 — 단일 프로세스.
    interval = config.probe_interval()
    if interval > 0:
        stop = threading.Event()
        app.state.probe_stop = stop

        @app.on_event("shutdown")
        def _stop_probe():
            stop.set()

        threading.Thread(target=probe.loop, daemon=True, name="tt-probe",
                         args=(os.environ.get("TT_URL", "http://127.0.0.1:7800"), interval, stop)).start()

    # 알림 전달 워커 (리뷰 R4a): 이벤트 저장(트랜잭션)과 웹훅 전송(커밋 후 락 밖) 분리.
    # TT_NOTIFY_WORKER=0로 끈다(테스트 격리용). 전달 실패는 로그만 — 저장을 되돌리지 않는다.
    # 수명은 앱에 연결하되 참조 카운트로 공유(리뷰 R5 4차): 같은 DB의 살아있는 앱이
    # 남아 있으면 워커가 계속 전달하고, 마지막 앱 shutdown의 stop을 워커 본인이
    # 확인해 자기 등록을 치운다 — shutdown의 선삭제가 만드는 전송 중 재생성 중복/
    # 공유 앱 전달 중단이 없다. 전송 자체는 이벤트별 CAS 선점(notified 0→2)이라
    # 동시 워커가 있어도 중복 발송이 불가능.
    if os.environ.get("TT_NOTIFY_WORKER", "1") != "0":
        with _notify_reg_lock:
            entry = _notify_workers.get(db_path)
            if entry is not None and not entry["stop"].is_set():
                entry["refs"] += 1  # 살아있는 워커 인계 (공유 DB 앱)
            else:
                # 없거나 죽어가는(stop set) 워커 — 새 등록 + 새 스레드
                entry = {"stop": threading.Event(), "refs": 1, "thread": None, "polls": 0}
                _notify_workers[db_path] = entry
                t = threading.Thread(target=service.notify_loop, daemon=True,
                                     name="tt-notify", args=(db_path, entry))
                entry["thread"] = t
                t.start()
        app.state.notify_entry = entry

        @app.on_event("shutdown")
        def _stop_notify():
            # 등록 삭제는 워커 본인이 한다(전송 중 종료 경계) — 여기선 참조만 감
            with _notify_reg_lock:
                app.state.notify_entry["refs"] -= 1
                if app.state.notify_entry["refs"] <= 0:
                    app.state.notify_entry["stop"].set()

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @app.get("/m", include_in_schema=False)
    def mobile():
        path = os.path.join(static_dir, "mobile.html")
        html = _versioned_html(open(path, encoding="utf-8").read().replace("__TT_BUILD__", str(int(os.path.getmtime(path)))), static_dir)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # M43KDKWQ-6Y7Z: 정적 자산 no-cache — JS에 캐시 헤더가 없어 구버전 tt-util.js( setTheme 이전)가
    # 신규 index.html과 섞여 닫기·테마 선택이 ReferenceError로 죽는 혼합 캐시를 방지. ETag 재검증으로 304 유지.
    @app.middleware("http")
    async def static_no_cache(request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p == "/" or p.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/", include_in_schema=False)
    def board_index():
        path = os.path.join(static_dir, "index.html")
        html = _versioned_html(open(path, encoding="utf-8").read(), static_dir)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/index.html", include_in_schema=False)
    def board_index2():
        return board_index()

    # M4580A48-573W: 에이전트 간 메시지 보드 — StaticFiles 마운트 전 등록(6Y7Z 학습).
    @app.get("/agent-board", include_in_schema=False)
    def agent_board():
        path = os.path.join(static_dir, "agent-board.html")
        html = _versioned_html(open(path, encoding="utf-8").read(), static_dir)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app(os.environ.get("TT_DB", os.path.expanduser("~/.local/share/think-tank/tt.db")))
