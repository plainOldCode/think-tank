"""메타 라우터 — health, work-contract, install.sh, 이벤트 로그(개선#2)."""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse

import config
import db as dbmod
import service
from service import Ctx

router = APIRouter()


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


@router.get("/health")
def health():
    return {"status": "ok", "name": "think-tank"}


@router.get("/work-contract")
def work_contract(request: Request):
    return request.app.state.ctx.contract


@router.get("/install.sh", include_in_schema=False)
def install_script(request: Request):
    base = str(request.base_url).rstrip("/")
    src = config.CLI_PATH.read_text().replace("${TT_URL:-http://127.0.0.1:7800}", "${TT_URL:-" + base + "}")
    script = (
        "#!/bin/sh\n# think-tank tt CLI installer (served by the tt server itself)\n"
        "mkdir -p \"$HOME/.local/bin\"\n"
        "cat > \"$HOME/.local/bin/tt\" <<'TT_EOF'\n" + src + "\nTT_EOF\n"
        "chmod +x \"$HOME/.local/bin/tt\"\n"
        "echo \"installed: ~/.local/bin/tt (default TT_URL=" + base + "; env TT_URL overrides)\"\n"
        "case \":$PATH:\" in *\":$HOME/.local/bin:\"*) ;; *) echo 'add to PATH: export PATH=\"$HOME/.local/bin:$PATH\"' ;; esac\n"
    )
    return Response(script, media_type="text/x-shellscript")


@router.get("/events",
            summary="Change event log with a monotonic seq cursor (TT improvement #2)",
            description="Outbox of issue/comment/message/dispatch changes recorded in the same "
                        "transaction as the mutation. Consumers pass after_seq to resume without "
                        "loss. Response: events + cursor (last delivered seq — pass it back as "
                        "after_seq to continue) + last_seq (table high-water mark).")
def list_events(after_seq: int = 0, limit: int = Query(100, ge=1, le=1000),
                kind: str | None = None, ctx=Depends(get_ctx)):
    with ctx.con() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE seq>? AND (? IS NULL OR kind=?) "
            "ORDER BY seq LIMIT ?", (after_seq, kind, kind, limit)).fetchall()
        last = c.execute("SELECT COALESCE(MAX(seq),0) AS m FROM events").fetchone()["m"]
    # R1: 커서는 "마지막으로 전달한 seq" — 페이지 크기와 무관하게 누락이 없다.
    # last_seq는 전체 high-water mark(참고용) — 재개 커서로 쓰면 미전달을 건너뛴다.
    return {"events": [dict(r) for r in rows],
            "cursor": rows[-1]["seq"] if rows else after_seq,
            "last_seq": last}


def _sse(rows):
    out = []
    for r in rows:
        out.append("id: %d\nevent: %s\ndata: %s\n"
                   % (r["seq"], r["kind"], json.dumps(dict(r), ensure_ascii=False)))
    return "\n".join(out) + "\n\n"


async def events_stream(after_seq: int = 0, ctx=None, poll_s: float = 0.5,
                        max_idle: float = 15.0, db_path: str | None = None):
    """SSE 이벤트 스트림 (TT 개선#2, 리뷰 R3) — 비동기 제너레이터. 커서 이후 이벤트를
    단조 seq로 밀어낸다.

    재접속: 클라이언트가 마지막 수신 seq(id: 또는 Last-Event-ID)을 after_seq로 넘기면
    유실 없이 이어받는다. 느린 소비자: 소스가 events 테이블이라 아무것도 유실되지
    않는다 — 생성기는 틱당 최대 500건씩 따라잡고, 정체 시 max_idle마다 ': ping'
    주석(reconcile keepalive)을 보낸다. 클라이언트 연결 해제 시 제너레이터가 취소된다.
    """
    seq = after_seq
    path = db_path or (ctx.db_path if ctx else None)
    last_emit = time.monotonic()

    def _fetch():
        con = dbmod.connect(path)
        try:
            return con.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 500", (seq,)).fetchall()
        finally:
            con.close()

    while True:
        # R3: 유휴 대기는 asyncio.sleep(취소 가능) — 공용 스레드풀을 점유하지 않는다
        # (동기 sleep 폴링은 연결당 워커를 영구 점유해 40연결에서 쓰기까지 막혔다).
        # DB 조회는 to_thread로 짧게 넘겨 이벤트 루프를 막지 않는다.
        rows = await asyncio.to_thread(_fetch)
        if rows:
            yield _sse(rows)
            seq = rows[-1]["seq"]
            last_emit = time.monotonic()
            continue  # 뒤따잡기 중 — 즉시 다음 틱
        if time.monotonic() - last_emit > max_idle:
            yield ": ping\n\n"
            last_emit = time.monotonic()
        await asyncio.sleep(poll_s)


@router.get("/events/stream",
            summary="SSE event stream (TT improvement #2)",
            description="Server-Sent Events of the change log. Resume with ?after_seq=N or the "
                        "standard Last-Event-ID header; periodic ': ping' comments keep the "
                        "connection alive and reconcile slow consumers.")
def events_stream_endpoint(request: Request, after_seq: int = 0,
                           last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                           ctx=Depends(get_ctx)):
    start = int(last_event_id) if (last_event_id or "").isdigit() else after_seq
    gen = events_stream(start, ctx)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
