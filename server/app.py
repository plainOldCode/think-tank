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
from routers import agents, issues, meta
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


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="think-tank")
    app.state.db_path = db_path  # 하위호환: 테스트/운영 스크립트가 직접 읽음
    app.state.ctx = Ctx(db_path, current_contract())
    app.include_router(meta.router)
    app.include_router(issues.router)
    app.include_router(agents.router)

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

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app(os.environ.get("TT_DB", os.path.expanduser("~/.local/share/think-tank/tt.db")))
