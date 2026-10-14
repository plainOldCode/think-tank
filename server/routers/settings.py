"""런타임 설정 라우터 — 서버 재배포 없이 웹·API에서 편집(XV84).

review-routing만 지원: {"review_fallback": [...], "review_map": {...}}.
코드 상수(probe DEFAULT_REVIEW_MAP·REVIEW_FALLBACK)는 부팅 기본값이고,
저장 설정이 있으면 probe가 사이클마다 읽어 덮는다."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

import db as dbmod
from service import Ctx

router = APIRouter()

_SETTINGS = {"review-routing"}
_FALLBACK_MAX = 8


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


class ReviewRoutingIn(BaseModel):
    review_fallback: list[str] = Field(..., min_length=1, max_length=_FALLBACK_MAX)
    review_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("review_fallback")
    @classmethod
    def _nonempty_strings(cls, v):
        if any((not isinstance(x, str)) or (not x.strip()) for x in v):
            raise ValueError("fallback 항목은 공백 아닌 문자열이어야 한다")
        if len(set(x.strip().lower() for x in v)) != len(v):
            raise ValueError("fallback에 중복 항목 금지")
        return [x.strip() for x in v]

    @field_validator("review_map")
    @classmethod
    def _str_values(cls, v):
        for k, val in v.items():
            if not isinstance(k, str) or not isinstance(val, str) or not val.strip():
                raise ValueError("map 값은 공백 아닌 문자열이어야 한다")
        return {k.strip().lower(): val.strip() for k, val in v.items()}


def _default_payload():
    from probe.core import DEFAULT_REVIEW_MAP, REVIEW_FALLBACK
    return {"review_fallback": list(REVIEW_FALLBACK),
            "review_map": dict(DEFAULT_REVIEW_MAP), "source": "default"}


def get_stored(con, key):
    row = con.execute("SELECT value, updated_at, updated_by FROM settings WHERE key=?",
                      (key,)).fetchone()
    if not row:
        return None
    try:
        val = json.loads(row[0])
    except ValueError:
        return None
    val["updated_at"], val["updated_by"] = row[1], row[2]
    val["source"] = "stored"
    return val


@router.get("/settings/{key}")
def get_setting(key: str, ctx: Ctx = Depends(get_ctx)):
    if key not in _SETTINGS:
        raise HTTPException(404, f"알 수 없는 설정 키: {key}")
    if key != "review-routing":
        raise HTTPException(404, "미지원 설정")
    with ctx.con() as c:
        stored = get_stored(c, key)
    return stored or _default_payload()


@router.put("/settings/{key}")
def put_setting(key: str, p: ReviewRoutingIn, ctx: Ctx = Depends(get_ctx)):
    if key != "review-routing":
        raise HTTPException(404, f"알 수 없는 설정 키: {key}")
    payload = {"review_fallback": p.review_fallback, "review_map": p.review_map}
    with ctx.con() as c:
        c.execute("INSERT INTO settings(key, value, updated_at, updated_by) "
                  "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "value=excluded.value, updated_at=excluded.updated_at, "
                  "updated_by=excluded.updated_by",
                  (key, json.dumps(payload, ensure_ascii=False), dbmod.now(), "web"))
    return {"ok": True, "key": key}


@router.delete("/settings/{key}")
def delete_setting(key: str, ctx: Ctx = Depends(get_ctx)):
    if key != "review-routing":
        raise HTTPException(404, f"알 수 없는 설정 키: {key}")
    with ctx.con() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))
    return {"ok": True, "key": key, "source": "default"}
