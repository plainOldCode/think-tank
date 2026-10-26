"""Server-side token gate.

claim returns the card body and a one-time nonce. ack must present
sha256 of the canonical body, contract, version, and scope. submit is
rejected without the work token those steps issued. Editing the card
changes the digest, so the old token stops working until a new ack.
"""

import hashlib
import hmac
import json
import secrets

from .errors import TurboError


def digest(body: str, contract: str, contract_version: int, scope: dict) -> str:
    raw = json.dumps(
        {
            "body": body,
            "contract": contract,
            "contract_version": contract_version,
            "scope": scope,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return "wt_" + secrets.token_urlsafe(24)


def new_resume_token() -> str:
    return "r-" + secrets.token_hex(8)


def _eq(left: str, right: str) -> bool:
    a = left.encode("utf-8")
    b = right.encode("utf-8")
    if len(a) != len(b):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
        return False
    return hmac.compare_digest(a, b)


def check_ack(card: dict, given_digest: str, nonce: str) -> str:
    if card.get("nonce_used"):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        raise TurboError(409, "nonce already used; claim again")
    if not card.get("nonce") or not _eq(card["nonce"], nonce or ""):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios)
        raise TurboError(409, "nonce does not match this claim")
    expect = digest(card["body"], card["contract"], card["contract_version"], card["scope"])
    if not given_digest or not _eq(expect, given_digest):
        raise TurboError(409, "digest does not match the card body")
    return expect


def check_token(card: dict, token: str) -> None:
    if not card.get("work_token") or not token or not _eq(card["work_token"], token):
        raise TurboError(403, "submit requires the work token from ack")
    expect = digest(card["body"], card["contract"], card["contract_version"], card["scope"])
    if not card.get("token_digest") or not _eq(card["token_digest"], expect):
        # TURBO-UNCOVERED: removal candidate (not exercised by scenarios; only unit-tested)
        raise TurboError(409, "card changed after ack; re-ack required")
