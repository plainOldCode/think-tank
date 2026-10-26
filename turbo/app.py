"""HTTP server for turbo cards, the token gate, and the read-only archive."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import legacy, service
from .errors import TurboError
from .store import Store

_STATIC = Path(__file__).resolve().parent / "static"
_FONTS = _STATIC / "fonts"


def _text(value: object) -> str:
    # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return ""


def subprocess_verify(cmd: str, cwd) -> tuple[int, str]:
    """Run one verify command. No shell. The scope author chose the command."""
    import shlex
    import subprocess

    argv = shlex.split(cmd)
    if not argv:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        return 1, "empty verify command"
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        return 124, (_text(exc.stdout) + _text(exc.stderr))[-2000:]
    except OSError as exc:
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        return 127, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-2000:]


def _s(body: dict, key: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) else ""


def _opt_s(body: dict, key: str) -> str | None:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    return value if isinstance(value, str) else ""


def _obj(body: dict, key: str) -> dict:
    value = body.get(key)
    return value if isinstance(value, dict) else {}


def _items(body: dict, key: str) -> list:
    value = body.get(key)
    return value if isinstance(value, list) else []


def create_app(db_path: str, legacy_path: str | None = None, verify=None, clock=None) -> FastAPI:
    app = FastAPI(title="think-tank turbo")
    store = Store(db_path)
    app.state.store = store
    app.state.legacy_path = legacy_path
    app.state.verify = verify or subprocess_verify
    app.state.clock = clock or __import__("time").time

    @app.exception_handler(TurboError)
    async def turbo_error(_request: Request, exc: TurboError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    def now() -> float:
        return float(app.state.clock())

    @app.get("/health")
    def health():
        return {"ok": True, "legacy": bool(app.state.legacy_path)}

    @app.get("/turbo/metrics")
    def metrics():
        return service.metrics(store, app.state.legacy_path)

    @app.post("/turbo/actors")
    def post_actor(body: dict):
        raw = body.get("harnesses")
        harnesses: list | None = raw if isinstance(raw, list) else None
        by = body.get("actor")
        who = by if isinstance(by, str) else None
        return service.register_actor(store, _s(body, "name"), _s(body, "role"), harnesses, who)

    @app.post("/turbo/cards")
    def post_card(body: dict):
        return service.create_card(
            store, _s(body, "actor"), _s(body, "title"), _s(body, "body"),
            _s(body, "contract"), _s(body, "harness"), body.get("scope"), now,
        )

    @app.get("/turbo/cards")
    def get_cards(state: str | None = None, harness: str | None = None):
        return service.list_cards(store, state, harness)

    @app.get("/turbo/cards/{card_id}")
    def get_card(card_id: str, actor: str | None = None):
        return service.get_card(store, card_id, actor)

    @app.post("/turbo/cards/{card_id}/claim")
    def post_claim(card_id: str, body: dict):
        return service.claim(store, card_id, _s(body, "actor"), _s(body, "agent"), now)

    @app.post("/turbo/cards/{card_id}/ack")
    def post_ack(card_id: str, body: dict):
        return service.ack(store, card_id, _s(body, "actor"), _s(body, "digest"), _s(body, "nonce"), now)

    @app.post("/turbo/cards/{card_id}/question")
    def post_question(card_id: str, body: dict):
        return service.ask(
            store, card_id, _s(body, "actor"), _s(body, "blocked_question"),
            _items(body, "blocked_options"), _s(body, "blocked_default"), now,
        )

    @app.post("/turbo/cards/{card_id}/turn-closed")
    def post_turn_closed(card_id: str, body: dict):
        return service.turn_closed(store, card_id, _s(body, "actor"), now)

    @app.post("/turbo/cards/{card_id}/answer")
    def post_answer(card_id: str, body: dict):
        return service.answer(store, card_id, _s(body, "actor"), _s(body, "choice"), now)

    @app.post("/turbo/cards/{card_id}/ready")
    def post_ready(card_id: str, body: dict):
        return service.ready(store, card_id, _s(body, "actor"), _s(body, "note"), now)

    @app.post("/turbo/cards/{card_id}/session")
    def post_session(card_id: str, body: dict):
        return service.attach_session(
            store, card_id, _s(body, "actor"), _s(body, "session_id"),
            _s(body, "driver"), _s(body, "mode"), now,
        )

    @app.post("/turbo/cards/{card_id}/resume")
    def post_resume(card_id: str, body: dict):
        return service.resume_started(
            store, card_id, _s(body, "actor"), _s(body, "token"),
            _s(body, "session_id") or None, _s(body, "mode") or "resume", now,
        )

    @app.post("/turbo/cards/{card_id}/submit")
    def post_submit(card_id: str, body: dict):
        return service.submit(
            store, card_id, _s(body, "actor"), _s(body, "sha"),
            _obj(body, "pr"), _items(body, "changed_paths"),
            _s(body, "work_token"), app.state.verify, now,
        )

    @app.post("/turbo/cards/{card_id}/verdict")
    def post_verdict(card_id: str, body: dict):
        return service.verdict(
            store, card_id, _s(body, "actor"), _s(body, "result"),
            _s(body, "findings"), now,
        )

    @app.post("/turbo/cards/{card_id}/merge")
    def post_merge(card_id: str, body: dict):
        return service.merge(store, card_id, _s(body, "actor"), now)

    @app.post("/turbo/cards/{card_id}/edit")
    def post_edit(card_id: str, body: dict):
        return service.edit_card(
            store, card_id, _s(body, "actor"), _opt_s(body, "body"),
            _opt_s(body, "contract"), body.get("scope") if "scope" in body else None, now,
        )

    @app.post("/turbo/runner/defaults")
    def post_defaults(body: dict):
        return service.apply_defaults(store, _s(body, "actor"), now)

    @app.get("/turbo/runner/next")
    def get_next(actor: str):
        return {"card": service.next_open(store, actor)}

    @app.get("/turbo/runner/pending")
    def get_pending(actor: str):
        return {"cards": service.pending_work(store, actor)}

    @app.get("/legacy/issues")
    def legacy_list(limit: int = 500):
        if not app.state.legacy_path:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            raise TurboError(404, "no legacy database configured")
        return legacy.list_issues(app.state.legacy_path, limit)

    @app.get("/legacy/issues/{issue_id}")
    def legacy_one(issue_id: str):
        if not app.state.legacy_path:
            # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
            raise TurboError(404, "no legacy database configured")
        return legacy.issue_detail(app.state.legacy_path, issue_id)

    @app.get("/", include_in_schema=False)
    def index():
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch)
    if _STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=_STATIC), name="turbo-assets")
    # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; untaken branch)
    if _FONTS.is_dir():
        app.mount("/fonts", StaticFiles(directory=_FONTS), name="turbo-fonts")
    return app


def main() -> None:
    # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
    import uvicorn

    db_path = os.environ.get("TURBO_DB", os.path.join("data", "turbo.db"))
    legacy_path = os.environ.get("TT_LEGACY_DB") or None
    port = int(os.environ.get("TURBO_PORT", "7810"))
    uvicorn.run(create_app(db_path, legacy_path), host="127.0.0.1", port=port)
