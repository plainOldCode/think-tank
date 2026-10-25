"""probe 공용 접근 계층 — TT API·gh CLI(TMXJ-S1 리프 모듈).

probe 내부 의존이 0인 최하위 모듈이다. 다른 probe 모듈은 이 쪽을 import한다 —
순환 방지 방향: http ← cards ← routing/markers ← decide/exec ← core."""
import json
import os
import shutil
import subprocess
import urllib.request


def _post_json(url, payload, timeout=5):
    """90Y9 — 외부 hook으로 JSON POST(fire-and-forget). 실패는 호출부에서 생략."""
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def api(url, path, method="GET", body=None):
    req = urllib.request.Request(
        url.rstrip("/") + path, method=method,
        headers={"content-type": "application/json", "x-agent": os.getenv("TT_AGENT", "dispatchd")})
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=15) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else None


def get_version(url, iid):
    return api(url, f"/issues/{iid}").get("version")


GH = (shutil.which("gh")
      or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh"))


def _gh(args):
    r = subprocess.run([GH, *[str(a) for a in args]], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(map(str, args))}: {r.stderr[:300]}")
    return r.stdout


def gh_json(*args):
    return json.loads(_gh(list(args)) or "null")


def gh_exec(*args):
    return _gh(list(args))
