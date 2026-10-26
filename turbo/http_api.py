"""HTTP client the runner uses against a turbo server. Stdlib only."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import TurboError


class HttpAPI:
    def __init__(self, base: str, actor: str, opener=None):
        self.base = base.rstrip("/")
        self.actor = actor
        self.opener = opener or urllib.request.urlopen

    def _req(self, method: str, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with self.opener(req, timeout=60) as res:
                raw = res.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw).get("error") or raw
            except json.JSONDecodeError:
                message = raw
            raise TurboError(exc.code, message) from None

    def claim(self, card_id, actor, agent):
        return self._req("POST", f"/turbo/cards/{card_id}/claim", {"actor": actor, "agent": agent})

    def get(self, card_id, reveal=False):
        path = f"/turbo/cards/{card_id}"
        if reveal:
            path += "?actor=" + urllib.parse.quote(self.actor)
        return self._req("GET", path)

    def apply_defaults(self, actor, now):
        del now
        return self._req("POST", "/turbo/runner/defaults", {"actor": actor})

    def attach_session(self, card_id, actor, session_id, driver, mode):
        return self._req("POST", f"/turbo/cards/{card_id}/session", {
            "actor": actor, "session_id": session_id, "driver": driver, "mode": mode,
        })

    def turn_closed(self, card_id, actor):
        return self._req("POST", f"/turbo/cards/{card_id}/turn-closed", {"actor": actor})

    def resume_started(self, card_id, actor, token, session_id, mode):
        return self._req("POST", f"/turbo/cards/{card_id}/resume", {
            "actor": actor, "token": token, "session_id": session_id or "", "mode": mode,
        })

    def submit(self, card_id, actor, sha, pr, changed, token):
        return self._req("POST", f"/turbo/cards/{card_id}/submit", {
            "actor": actor, "sha": sha, "pr": pr, "changed_paths": changed, "work_token": token,
        })

    def merge(self, card_id, actor):
        return self._req("POST", f"/turbo/cards/{card_id}/merge", {"actor": actor})

    def next_open(self, actor):
        found = self._req("GET", "/turbo/runner/next?actor=" + urllib.parse.quote(actor))
        return found.get("card")

    def pending(self, actor):
        found = self._req("GET", "/turbo/runner/pending?actor=" + urllib.parse.quote(actor))
        return found.get("cards") or []
