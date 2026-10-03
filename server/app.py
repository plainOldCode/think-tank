"""think-tank 서버 — 앱 조립만 담당 (카드 M3ZW8DY7-M14N).

구조: 라우팅은 routers/(meta, issues, agents), 스키마는 models.py,
공유 로직은 service.py, 상수·env는 config.py.
"""
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from routers import agents, issues, meta
from service import Ctx
from work_contract import current_contract


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="think-tank")
    app.state.db_path = db_path  # 하위호환: 테스트/운영 스크립트가 직접 읽음
    app.state.ctx = Ctx(db_path, current_contract())
    app.include_router(meta.router)
    app.include_router(issues.router)
    app.include_router(agents.router)

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @app.get("/m", include_in_schema=False)
    def mobile():
        path = os.path.join(static_dir, "mobile.html")
        html = open(path, encoding="utf-8").read().replace("__TT_BUILD__", str(int(os.path.getmtime(path))))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app(os.environ.get("TT_DB", os.path.expanduser("~/.local/share/think-tank/tt.db")))
