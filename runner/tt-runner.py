#!/usr/bin/env python3
"""TT dispatch -> tmux agent runner (TT issue M3B6TNYJ-JJCM, spec t_27ca1582).

tt-dispatch-adapter.py의 수신 골격(Bearer+X-Tt-Dispatch 검증, 즉시 200, idempotency
장부)을 그대로 계승하되, dispatch의 처리 대상을 Kanban 카드 생성/steer에서
tmux agent 실행으로 바꾼 단일 파일 러너다. 표준 라이브러리만 쓴다.

규칙 요약 (RUNTIME-SPEC 기준):
- secret 파일(~/.hermes/tt-runner.secret) 부재 = 기동 실패 (fail-closed, 어댑터와 다르게).
- 바인딩: tailnet IP 또는 127.0.0.1만. 0.0.0.0 거부.
- 장부 키 "<issue_id>#<dispatch_id>" 단일 실행 게이트 — webhook/polling 공통 진입점,
  선점 실패는 DUP-SKIP.
- message는 셸 인자로 절대 interpolate하지 않는다: 첫 실행은 stdin 파일, 계속 재지시는
  메시지 파일을 쓰고 Pane 안에서 "$(cat 파일)" 형태로만 읽는다.
- 파괴적 패턴이면 실행 보류(held) + TT 승인 요청 코멘트, '승인' 응답까지 시작하지 않는다.
- 로그/코멘트에 secret 값 미기재(mask 처리).
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = os.path.expanduser(os.environ.get("TT_RUNNER_STATE",
                                              "~/.local/state/tt-runner"))
RUNS_PATH = os.path.join(STATE_DIR, "runs.json")
RUNTIME_DIR = os.path.join(STATE_DIR, "runs")
INBOX_DIR = os.path.join(STATE_DIR, "inbox")
LOG_PATH = os.path.join(STATE_DIR, "runner.log")
SECRET_PATH = os.path.expanduser(os.environ.get("TT_RUNNER_SECRET",
                                                "~/.hermes/tt-runner.secret"))
REGISTRY_PATH = os.path.expanduser(os.environ.get("TT_RUNNER_REGISTRY",
                                                  "~/.config/tt-runner/agents.json"))
BIND = os.environ.get("TT_RUNNER_BIND", "127.0.0.1")
PORT = int(os.environ.get("TT_RUNNER_PORT", "7796"))
TT = os.environ.get("TT_URL", "").rstrip("/")
RUNNER_NAME = os.environ.get("TT_RUNNER_NAME", "runner")
RUNNER_SELF = os.environ.get("TT_RUNNER_SELF", os.path.abspath(__file__))
PY = sys.executable or "/usr/bin/python3"
POLL_INTERVAL_S = int(os.environ.get("TT_RUNNER_POLL", "60"))
WATCH_INTERVAL_S = 5
DEFAULT_TIMEOUT_S = 1800
OVERRIDE_KEYS = {"model", "reasoning", "workspace", "continue_session"}
APPROVE_WORDS = {"승인", "approve"}
# P7EK 원문 목록(tp-13, skshim 소유)은 미확보 — 아래는 그 전까지의 최소 초안 목록이며
# 후속 작업에서 원문과 대조·대체해야 한다(미실증 항목으로 전달).
DESTRUCTIVE = [
    r"rm\s+(-[a-z]*\s+)*-?[rRf]", r"\bgit\s+push\b", r"\bsudo\b",
    r"\bdrop\s+(table|database|schema)\b", r"\bmkfs\b", r"\bkillall\b",
    r"\blaunchctl\s+(unload|remove|bootout)\b", r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f", r">\s*/dev/disk", r"\bshutdown\b", r"\breboot\b",
    r"\bchmod\s+-R\s+777\b", r"\bDELETE\s+FROM\b", r"\bdocker\s+(system\s+prune|volume\s+prune)\b",
]
# 입력/승인 요구 시그니처 (M3BZS1G3-VNQH ①): exit!=0일 때만 판정 — 성공 출력에
# 단어가 그냥 등장하는 오검지를 막는다. 기본값 초안: 사용자 승인 후 확정.
INPUT_SIGNATURES = [
    r"\brequires? (your )?approval\b",
    r"\bapproval (required|needed)\b",
    r"\bwaiting for (your )?(input|approval|response|answer)\b",
    r"\bneeds? (your )?(input|approval|confirmation)\b",
    r"\bpermission (required|needed)\b",
    r"\bdo you want to (allow|proceed|continue)\b",
    r"\bplease (approve|confirm|respond|answer)\b",
    r"\[[Yy]/[Nn]\]|\([Yy]/[Nn]\)|\[[Yy]es/[Nn]o\]",
    r"\bpress (any key|enter) to (continue|proceed)\b",
    r"\bneeds_input\b|\bneeds_approval\b|\bexternal_permission\b",
]
# stall 무음 임계 (M3BZS1G3-VNQH ②): wall-clock TIMEOUT과 별개.
# 기본 600s 무음 → STALL 코멘트(+pane tail), 거기서 600s 더 무음 → kill.
STALL_SILENCE_S = int(os.environ.get("TT_STALL_SILENCE_S", "600"))
STALL_KILL_AFTER_S = int(os.environ.get("TT_STALL_KILL_AFTER_S", "600"))
# credential 격리 (M3BZS1G3-VNQH ④): agent CLI 자식 env에서 제거할 이름 패턴 —
# 기본값 초안. 특정 CLI가 실제로 필요로 하는 키는 레지스트리 env_extra로 opt-in.
SECRET_ENV_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY)", re.I)
SECRET_ENV_DROP_PREFIX = ("TT_RUNNER_",)
# tmux 서버 socket 분리(smoke가 실 서버 세션과 충돌하지 않게); 미설정=기본 서버.
TMUX_SOCK = os.environ.get("TT_TMUX_SOCKET") or ""
_secret_cache = None


def tmux_cmd(*args):
    return ["tmux"] + (["-L", TMUX_SOCK] if TMUX_SOCK else []) + [str(a) for a in args]


def tmux_run(*args, **kw):
    return subprocess.run(tmux_cmd(*args), **kw)


# ---------- 기본 유틸 ----------

def ensure_dirs():
    for d in (STATE_DIR, RUNTIME_DIR, INBOX_DIR):
        os.makedirs(d, exist_ok=True)


def load_secret(fail_closed=True):
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    try:
        with open(SECRET_PATH) as f:
            v = f.read().strip()
    except FileNotFoundError:
        v = ""
    if not v and fail_closed:
        log("FATAL secret missing at %s — refusing to start (fail-closed)" % SECRET_PATH)
        sys.exit(3)
    _secret_cache = v
    return v


def mask(text):
    """어떤 출력에도 secret 값을 남기지 않는다."""
    sec = load_secret(fail_closed=False)
    if sec and text and sec in text:
        text = text.replace(sec, "***")
    return text


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), mask(str(msg)))
    try:
        ensure_dirs()
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def machine_name():
    try:
        return load_registry().get("machine") or socket.gethostname().split(".")[0]
    except Exception:
        return socket.gethostname().split(".")[0]


# ---------- TT 클라이언트 (브리지 미import, 자체 최소) ----------

def tt_http(method, path, payload=None, timeout=15):
    if not TT:
        raise RuntimeError("TT_URL not set")
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(TT + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


def tt_http_raw(method, path, payload=None):
    try:
        return tt_http(method, path, payload)
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code, "detail": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"HTTP": 0, "detail": "%s: %s" % (type(e).__name__, e)}


def tt_comment(issue_id, body):
    author = os.environ.get("TT_AGENT", "runner@" + machine_name())
    r = tt_http_raw("POST", "/issues/%s/comments" % issue_id,
                    {"author": author, "body": mask(body)[:4000]})
    if isinstance(r, dict) and r.get("HTTP") not in (None, 0):
        log("TT-COMMENT-FAIL #%s HTTP %s" % (issue_id, r["HTTP"]))
    return r


# ---------- 레지스트리 ----------

def load_registry():
    with open(REGISTRY_PATH) as f:
        reg = json.load(f)
    if not isinstance(reg.get("agents"), dict) or not reg["agents"]:
        raise RuntimeError("registry has no agents: " + REGISTRY_PATH)
    return reg


_tailnet_cache = None


def tailscale_ok():
    """permission all 승인용 tailnet 소속 검사(결과 캐시). tailscale 없는 머신=비-tailnet."""
    global _tailnet_cache
    if _tailnet_cache is not None:
        return _tailnet_cache
    ok = False
    if shutil.which("tailscale"):
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0 and any(l.strip().startswith("100.") for l in r.stdout.splitlines())
    _tailnet_cache = ok
    return ok


def resolve_profile(payload):
    """dispatch.payload.agent -> 레지스트리 프로필. 미등록이면 None.
    permission all은 tailnet 확인 머신에서만 성립, 아니면 ask(auto)로 강등."""
    reg = load_registry()
    name = str(payload.get("agent") or "")
    prof = reg["agents"].get(name)
    if prof is None:
        return None
    prof = dict(prof)
    prof["profile_name"] = name
    if prof.get("binary") and not shutil.which(os.path.expanduser(prof["binary"])):
        log("NO-BINARY profile=%s binary=%s (미설치 CLI는 실행 금지)" % (name, prof["binary"]))
        return None
    if prof.get("permission_mode") == "all" and not tailscale_ok():
        prof["permission_mode"] = "auto"
        log("PERM-DOWNGRADED profile=%s all->auto (tailnet 미확인)" % name)
    return prof


CODEX_PERM_ARGS = {
    "read": ["-s", "read-only"],
    "auto": ["-s", "workspace-write", "--approve-for-me"],
    "all": ["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"],
}


def build_argv(prof, opts, out_file):
    """message는 argv에 넣지 않는다(stdin 파일 또는 pane 파일 읽기). 반환: argv 리스트."""
    binary = os.path.expanduser(prof.get("binary", ""))
    driver = prof.get("driver", "codex")
    argv = [binary]
    if driver == "codex":
        argv += ["exec", "-"]  # prompt는 stdin('-')으로 — 셸 interpolate 없음
        perm = prof.get("permission_mode", "read")
        argv += CODEX_PERM_ARGS.get(perm, CODEX_PERM_ARGS["read"])
        if opts.get("model") or prof.get("model"):
            argv += ["-m", str(opts.get("model") or prof["model"])]
        rs = opts.get("reasoning") or prof.get("reasoning")
        if rs:
            argv += ["-c", "model_reasoning_effort=%s" % rs]
        if out_file:
            argv += ["--output-last-message", out_file]
        argv += ["--skip-git-repo-check"]
    else:  # plain/opencode류: auto_args + stdin 파일은 미지원 → 메시지는 마지막 argv(무셸)
        argv += list(prof.get("auto_args", []))
    return argv


# ---------- 장부 (원자적 check-and-set) ----------

@contextmanager
def ledger_lock():
    ensure_dirs()
    f = open(os.path.join(STATE_DIR, "runs.lock"), "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def load_runs():
    try:
        with open(RUNS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_runs(runs):
    tmp = RUNS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(runs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RUNS_PATH)


def run_key(p):
    return "%s#%s" % (p["issue_id"], p["dispatch_id"])


def claim_dispatch(p, session):
    """webhook/polling 공통 단일 게이트: 성공 시 True(진입 권한 확보)."""
    key = run_key(p)
    with ledger_lock():
        runs = load_runs()
        if key in runs:
            log("DUP-SKIP dispatch#%s (key=%s status=%s)" % (p["dispatch_id"], key, runs[key].get("status")))
            return False
        runs[key] = {"status": "queued", "issue_id": str(p["issue_id"]),
                     "dispatch_id": p["dispatch_id"], "agent": str(p.get("agent") or ""),
                     "message": p.get("message", ""), "context_in": p.get("context", ""),
                     "work_contract": p.get("work_contract"),
                     "execution_attempt": p.get("execution_attempt", 0),
                     "opts": p.get("_opts") or {},
                     "session": session, "ts": time.time()}
        save_runs(runs)
    return True


def update_run(key, **fields):
    with ledger_lock():
        runs = load_runs()
        if key in runs:
            runs[key].update(fields)
            save_runs(runs)


def get_run(key):
    with ledger_lock():
        return load_runs().get(key)


def latest_held(issue_id):
    with ledger_lock():
        runs = load_runs()
        cand = [k for k, r in runs.items()
                if r.get("status") == "held" and r.get("issue_id") == str(issue_id)]
        return sorted(cand)[-1] if cand else None


# ---------- 오버라이드·게이트 ----------

def parse_overrides(message):
    """선두 '#opts {json}' 한 줄만 허용 키 오버라이드로 해석, 본문에서 제거."""
    if not message:
        return {}, message
    lines = message.split("\n", 1)
    head = lines[0].strip()
    if head.startswith("#opts"):
        try:
            opts = json.loads(head[len("#opts"):].strip())
            if not isinstance(opts, dict):
                raise ValueError("not an object")
        except Exception as e:
            log("OVERRIDE-BAD %s" % e)
            return {}, message
        clean = {}
        for k, v in opts.items():
            if k in OVERRIDE_KEYS:
                clean[k] = v
            else:
                log("OVERRIDE-IGNORED k=%s" % k)
        rest = lines[1] if len(lines) > 1 else ""
        return clean, rest.lstrip("\n")
    return {}, message


def destructive_hit(message):
    for pat in DESTRUCTIVE:
        if re.search(pat, message or "", re.IGNORECASE):
            return pat
    return None


def input_needed(text):
    """실패 출력에서 '입력/승인 요구' 시그니처를 찾는다 (M3BZS1G3 ①).

    exit!=0일 때만 부르는 게 계약 — 성공 출력의 단어 우연을 오검지하지 않는다.
    hit → crashed와 구분되는 BLOCKED(waiting_for=human)로 finalize한다.
    """
    for pat in INPUT_SIGNATURES:
        if re.search(pat, text or "", re.IGNORECASE):
            return pat
    return None


def sanitize_workspace_value(ws):
    """dispatch 오버라이드 값 1차_SANITIZE: 제어문자·NUL·개행 차단 (M3BZS1G3 ③)."""
    s = str(ws or "")
    if not s or any(ord(c) < 32 or ord(c) == 127 for c in s):
        return None
    if "\u202e" in s or len(s) > 512:
        return None
    return s


def guard_workspace(prof, ws):
    """Symphony SPEC §9.5 불변식 (M3BZS1G3 ③): 실행 cwd는 허용 root 실경로 안에 있어야 한다.

    반환 (real_path, reason): reason None=통과. override allowlist와 별개로,
    symlink/`..` 어떤 경로 표기든 realpath 해석 후 root(prefix) 안에 있는지 검사.
    위반은 실행 전 거부 — run_agent 안에서도 2차로 동일 검사(방어 다층).
    """
    s = sanitize_workspace_value(ws)
    if s is None:
        return None, "bad-value"
    real = os.path.realpath(os.path.expanduser(s))
    if not os.path.isdir(real):
        return None, "not-a-dir"
    roots = [os.path.realpath(os.path.expanduser(w))
             for w in (prof.get("allowed_workspaces") or [])]
    if prof.get("workspace"):
        roots.append(os.path.realpath(os.path.expanduser(prof["workspace"])))
    if not roots:
        roots = [os.path.realpath(os.path.expanduser("~"))]
    for r in roots:
        if real == r or real.startswith(r + os.sep):
            return real, None
    return None, "outside-root"


def agent_env(prof):
    """credential 격리 (M3BZS1G3 ④): agent CLI 자식/pane 셸 env에서
    runner가 가진 자격증류를 제거한다. 이름 패턴(SECRET/TOKEN/...) +
    TT_RUNNER_* prefix + 값에 TT secret을 포함하는 어떤 키든 낙찰제거.
    특정 CLI가 꼭 필요한 키는 레지스트리 env_extra 로 opt-in (기본 없음).
    한계: same-user 파일(~/.hermes/... secret file)까지 막지는 못한다 — env 격리 범위만."""
    sec = load_secret(fail_closed=False)
    env = {}
    for k, v in os.environ.items():
        if k.startswith(SECRET_ENV_DROP_PREFIX) or SECRET_ENV_RE.search(k):
            continue
        if sec and sec in str(v):
            continue
        env[k] = v
    for k, v in (prof.get("env_extra") or {}).items():
        env[str(k)] = str(v)
    return env


def apply_workspace_override(prof, opts):
    """workspace 오버라이드는 레지스트리에 사전 등재된 allowed_workspaces 값만.
    (M3BZS1G3 ③) 값은 sanitize 후 비교 — 통과한 값도 최종 실행 전 guard_workspace가
    root 실경로 검사를 한 번 더 통과해야 한다."""
    ws = opts.get("workspace")
    if not ws:
        return os.path.expanduser(prof.get("workspace", os.path.expanduser("~")))
    ws = sanitize_workspace_value(ws)
    if ws is None:
        log("OVERRIDE-REJECT workspace (제어문자/빈값)")
        return os.path.expanduser(prof.get("workspace", os.path.expanduser("~")))
    allowed = [os.path.expanduser(w) for w in prof.get("allowed_workspaces", [])]
    if os.path.expanduser(ws) in allowed:
        return os.path.expanduser(ws)
    log("OVERRIDE-IGNORED k=workspace v=%s (미등재)" % ws)
    return os.path.expanduser(prof.get("workspace", os.path.expanduser("~")))


# ---------- tmux ----------

def sanitize(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(s))[:60]


def new_session_name(payload):
    return "tt-%s-%s" % (sanitize(payload.get("agent") or "agent"),
                         sanitize(payload.get("dispatch_id")))


CTX_RE = re.compile(r"^runner:[A-Za-z0-9_.-]+:tmux:([A-Za-z0-9_.-]+)$")


def ctx_token(session):
    return "runner:%s:tmux:%s" % (machine_name(), session)


def parse_ctx(context):
    m = CTX_RE.match((context or "").strip())
    return m.group(1) if m else None


def session_alive(name):
    r = tmux_run("has-session", "-t", name, capture_output=True)
    return r.returncode == 0


def session_tail(name, limit=200):
    try:
        r = tmux_run("capture-pane", "-p", "-t", name, "-S", "-80",
                     capture_output=True, text=True, timeout=10)
        return mask((r.stdout or "")[-limit:])
    except Exception:
        return ""


def set_runtime(state_dir=None, secret=None, registry=None, tt_url=None):
    """tmux pane는 tmux 서버 환경만 상속 — env 전파에 의존하지 않고 경로를 argv로 받는다."""
    global STATE_DIR, RUNS_PATH, RUNTIME_DIR, INBOX_DIR, LOG_PATH
    global SECRET_PATH, REGISTRY_PATH, TT, _secret_cache
    if state_dir:
        STATE_DIR = state_dir
        RUNS_PATH = os.path.join(STATE_DIR, "runs.json")
        RUNTIME_DIR = os.path.join(STATE_DIR, "runs")
        INBOX_DIR = os.path.join(STATE_DIR, "inbox")
        LOG_PATH = os.path.join(STATE_DIR, "runner.log")
    if secret:
        SECRET_PATH = secret
        _secret_cache = None
    if registry:
        REGISTRY_PATH = registry
    if tt_url:
        TT = tt_url
    ensure_dirs()


def task_prompt(ent):
    contract = ent.get("work_contract")
    message = ent.get("message", "")
    if not isinstance(contract, dict) or not contract.get("instructions") or not contract.get("version"):
        return message
    return ("[TT work contract " + str(contract["version"]) + "]\n"
            + str(contract["instructions"]) + "\n\n"
            + "completion_report_required=" + str(bool(contract.get("report_required"))).lower() + "\n"
            + "issue_id=" + str(ent["issue_id"]) + " execution_attempt="
            + str(ent.get("execution_attempt", 0)) + "\n"
            + "[Task]\n" + message)


def start_session(key, prof, session, workspace):
    msgfile = os.path.join(RUNTIME_DIR, key.replace("#", "_") + ".msg")
    with open(msgfile, "w") as f:
        f.write(task_prompt(get_run(key) or {}))
    os.chmod(msgfile, 0o600)
    argv = tmux_cmd("new-session", "-d", "-s", session, "-c", workspace,
                    PY, RUNNER_SELF, "run-agent", key, STATE_DIR, SECRET_PATH, REGISTRY_PATH, TT)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("tmux new-session rc=%d %s" % (r.returncode, mask(r.stderr)[:200]))
    return msgfile


def continue_session(prof, session, message, key=None):
    """pane의 대화형 셸에 파일 읽기 형태만으로 메시지를 전달한다.
    계속 모드도 신규 모드와 동일한 종료 마커 프로토콜을 쓴다 — pane 셸이 rc/done을
    쓰고 감시자가 이를 주시한다 (t_0d90b8eb 실측: 마커 없어 running 고립)."""
    cmd = prof.get("continue_cmd")
    if not cmd:
        return False
    msgfile = os.path.join(INBOX_DIR, sanitize(session) + ".msg")
    with open(msgfile, "w") as f:
        f.write(task_prompt({**(get_run(key) or {}), "message": message}) if key else message)
    os.chmod(msgfile, 0o600)
    if key:
        base = os.path.join(RUNTIME_DIR, key.replace("#", "_"))
        line = ('%s "$(cat \'%s\')" > \'%s.log\' 2>&1; __rc=$?; '
                'printf \'%%s\' "$__rc" > \'%s.exit\'; : > \'%s.done\' '
                % (cmd, msgfile, base, base, base))
    else:
        line = '%s "$(cat \'%s\')" ' % (cmd, msgfile)
    subprocess.run(tmux_cmd("send-keys", "-t", session, "-l", line), check=True)
    subprocess.run(tmux_cmd("send-keys", "-t", session, "Enter"), check=True)
    return True


# ---------- 실행 진입 (execute_once) ----------

def decide_session(p):
    """claim 없는 세션 결정(동기 200의 context용). prepare와 동일 규칙."""
    ctx_session = parse_ctx(p.get("context"))
    if ctx_session and session_alive(ctx_session):
        return ctx_session
    return new_session_name(p)


def prepare(p):
    """백그라운드 전용: dup 판정+장부 선점. (session, fresh) 반환."""
    key = run_key(p)
    session = decide_session(p)
    opts, body = parse_overrides(p.get("message", ""))
    if not claim_dispatch({**p, "message": body, "_opts": opts}, session):
        with ledger_lock():
            ent = load_runs().get(key) or {}
        return ent.get("session") or session, False
    return session, True


def execute_once(p, session):
    """백그라운드 실행: 게이트 -> (held | send-keys 계속 | tmux 신규)."""
    key = run_key(p)
    ent = get_run(key) or {}
    if ent.get("status") != "queued":
        return
    message = ent.get("message", "")
    prof = resolve_profile(p)
    if prof is None:
        update_run(key, status="failed", detail="profile-unknown")
        log("REJECT dispatch#%s agent=%s (레지스트리 미등록/미설치)" % (p["dispatch_id"], p.get("agent")))
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s 거부 — agent '%s' 미등록이거나 CLI 미설치 (장부:%s)"
                   % (machine_name(), p["dispatch_id"], p.get("agent"), key))
        return
    pat = destructive_hit(message)
    if pat and not ent.get("approved"):
        update_run(key, status="held", gate=pat)
        tt_comment(ent["issue_id"],
                   "runner:%s dispatch#%s 보류 — 파괴적 패턴(%s) 감지. 계속하려면 '승인'으로 재-dispatch. (장부:%s)"
                   % (machine_name(), p["dispatch_id"], pat, key))
        log("GATE-HOLD dispatch#%s pattern=%s" % (p["dispatch_id"], pat))
        return
    workspace = apply_workspace_override(prof, ent.get("opts") or {})
    # (M3BZS1G3 ③) 1차 관문: 허용 root 밖/이상 값은 실행 시작 전 거부.
    real_ws, why = guard_workspace(prof, workspace)
    if why:
        update_run(key, status="failed", detail="workspace-guard:" + why, ended=time.time())
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s 거부 — 워크스페이스 불변식 위반(%s: %s) (장부:%s)"
                   % (machine_name(), p["dispatch_id"], why, workspace, key))
        log("WORKSPACE-GUARD dispatch#%s rejected=%s (%s)" % (p["dispatch_id"], workspace, why))
        return
    workspace = real_ws
    try:
        os.makedirs(workspace, exist_ok=True)
    except OSError:
        pass
    ctx_session = parse_ctx(ent.get("context_in"))
    if ctx_session and ctx_session == ent.get("session") and session_alive(ctx_session) \
            and continue_session(prof, ctx_session, message, key):
        update_run(key, status="running", mode="continue", started=time.time())
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s → tmux %s 계속 (send-keys)"
                   % (machine_name(), p["dispatch_id"], ctx_session))
        log("CONTINUE dispatch#%s session=%s" % (p["dispatch_id"], ctx_session))
        return
    # 신규 세션 (계속 실패/세션 소멸 시 자동 폴백)
    try:
        start_session(key, prof, ent["session"], workspace)
    except Exception as e:
        update_run(key, status="failed", detail=str(e))
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s 시작 실패: %s" % (machine_name(), p["dispatch_id"], mask(str(e))))
        log("START-FAIL dispatch#%s %s" % (p["dispatch_id"], e))
        return
    update_run(key, status="running", mode="new", started=time.time(), workspace=workspace,
               timeout_s=prof.get("timeout_s", DEFAULT_TIMEOUT_S), profile=prof.get("profile_name"))
    tt_comment(ent["issue_id"], "runner:%s dispatch#%s → tmux %s (queued, ws=%s)"
               % (machine_name(), p["dispatch_id"], ent["session"], workspace))
    log("START dispatch#%s session=%s ws=%s" % (p["dispatch_id"], ent["session"], workspace))


def maybe_approve_release(p):
    """held 런: message가 '승인'이면 대기 중이던 원 메시지를 통과시킨다."""
    if (p.get("message") or "").strip().lower() not in APPROVE_WORDS:
        return False
    held = latest_held(p["issue_id"])
    if not held:
        return False
    ent = get_run(held)
    if not ent:
        return False
    orig = {"dispatch_id": ent["dispatch_id"], "issue_id": ent["issue_id"],
            "agent": ent["agent"], "message": ent["message"], "context": ent.get("context_in", "")}
    update_run(held, status="queued", gate=None, approved=True)
    tt_comment(ent["issue_id"], "runner:%s dispatch#%s 승인 — 원 메시지 실행 시작 (장부:%s)"
               % (machine_name(), ent["dispatch_id"], held))
    log("GATE-RELEASED %s by dispatch#%s" % (held, p["dispatch_id"]))
    execute_once(orig, ent["session"])
    return True


# ---------- run-agent (tmux 안의 자기 자신) ----------

def run_agent(key):
    ent = get_run(key) or {}
    try:
        prof = resolve_profile({"agent": ent.get("agent")})
    except Exception:
        prof = None
    if prof is None:
        prof = {"binary": "echo", "driver": "plain"}
    base = key.replace("#", "_")
    out_file = os.path.join(RUNTIME_DIR, base + ".out")
    log_file = os.path.join(RUNTIME_DIR, base + ".log")
    msg_file = os.path.join(RUNTIME_DIR, base + ".msg")
    opts = ent.get("opts") or {}
    workspace = ent.get("workspace") or os.path.expanduser(prof.get("workspace", "~"))
    # (M3BZS1G3 ③) 2차 방어: execute_once에서 통과한 값도 실행 직전 root 실경로 재검사.
    real_ws, why = guard_workspace(prof, workspace)
    if why:
        with open(log_file, "a") as fout:
            fout.write("workspace-guard rejected: %s (%s)\n" % (workspace, why))
        with open(os.path.join(RUNTIME_DIR, base + ".exit"), "w") as f:
            f.write("78")
        open(os.path.join(RUNTIME_DIR, base + ".done"), "w").close()
        log("WORKSPACE-GUARD key=%s rejected=%s (%s)" % (key, workspace, why))
        return 78
    workspace = real_ws
    argv = build_argv(prof, opts, out_file)
    driver = prof.get("driver", "codex")
    # (M3BZS1G3 ④) credential 격리: 이 프로세스 env 자체를 거른 뒤 자식/pane 셸로 승계.
    filtered = agent_env(prof)
    os.environ.clear()
    os.environ.update(filtered)
    try:
        with open(msg_file) as fin, open(log_file, "ab") as fout:
            if driver == "codex":
                # prompt는 stdin 파일('-'): argv에 message 없음
                proc = subprocess.run(argv, cwd=workspace, stdin=fin,
                                      stdout=fout, stderr=subprocess.STDOUT)
            else:
                rc_argv = argv + [task_prompt(ent)]  # 리스트 전달 — 셸 없음
                proc = subprocess.run(rc_argv, cwd=workspace, stdout=fout,
                                      stderr=subprocess.STDOUT)
        rc = proc.returncode
    except Exception as e:
        with open(log_file, "a") as fout:
            fout.write("runner exec error: %s\n" % e)
        rc = 127
    with open(os.path.join(RUNTIME_DIR, base + ".exit"), "w") as f:
        f.write(str(rc))
    open(os.path.join(RUNTIME_DIR, base + ".done"), "w").close()
    if prof.get("keep_shell", driver == "codex"):
        # 관찰·계속 재지시(send-keys)를 위해 pane에 대화형 셸 유지
        shell = os.environ.get("SHELL", "/bin/zsh")
        os.execv(shell, [shell, "-i"])
    sys.exit(rc)


# ---------- 감시자 ----------

def finalize(runs_dir_name, key, ent, exit_code, tail=""):
    issue, did = ent["issue_id"], ent["dispatch_id"]
    session = ent.get("session", "?")
    summary = ""
    base = key.replace("#", "_")
    for suffix in (".out", ".log"):  # codex는 .out, plain 드라이버는 .log가 stdout 수신
        cand = os.path.join(RUNTIME_DIR, base + suffix)
        if os.path.exists(cand):
            try:
                with open(cand) as f:
                    summary = f.read()[-2000:]
            except OSError:
                pass
            if suffix == ".out":
                break
    if exit_code == 0:
        tt_comment(issue, "runner:%s dispatch#%s done exit=0 session=%s\n%s"
                   % (machine_name(), did, session, mask(summary)))
        log("DONE dispatch#%s exit=0 session=%s" % (did, session))
        update_run(key, status="done", exit=exit_code, ended=time.time())
        return "done"
    # (M3BZS1G3 ①) 실패 출력에서 입력/승인 요구 시그니처 → crashed가 아니라 BLOCKED.
    # 재시도 하지 않고 사람에게 결정 지점을 넘긴다(TT측 waiting_for=human 연결).
    combined = (tail or "") + "\n" + (summary or "")
    sig = input_needed(combined)
    if sig:
        tt_comment(issue, "runner:%s dispatch#%s BLOCKED — 입력/승인 요구 감지(%s) exit=%s session=%s "
                   "waiting_for=human. 재지시('승인' 또는 지시)를 기다림; 자동 재시도 없음.\n%s"
                   % (machine_name(), did, sig, exit_code, session, mask(combined[-1500:])))
        log("BLOCKED dispatch#%s signature=%s exit=%s session=%s" % (did, sig, exit_code, session))
        update_run(key, status="blocked", exit=exit_code, blocked_on=sig, ended=time.time())
        return "blocked"
    detail = tail or summary
    tt_comment(issue, "runner:%s dispatch#%s failed exit=%s session=%s\n%s"
               % (machine_name(), did, exit_code, session, mask(detail)))
    log("FAILED dispatch#%s exit=%s" % (did, exit_code))
    update_run(key, status="failed", exit=exit_code, ended=time.time())
    return "failed"


def pane_fingerprint(name, key=None):
    """무음 탐지용 지문: pane 마지막 10행 텍스트 + run 산출 파일(.log/.out) mtime.
    codex exec는 pane이 아니라 파일로 진행 출력을 써서 pane만 보면 건전한 장시간
    실행도 무음으로 보인다 — 파일 mtime 진행도 liveness로 친다. 판정 없으면 None."""
    try:
        r = tmux_run("capture-pane", "-p", "-t", name, "-S", "-10",
                     capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        h = hashlib.md5((r.stdout or "").rstrip().encode())
        if key:
            base = os.path.join(RUNTIME_DIR, key.replace("#", "_"))
            for suffix in (".log", ".out"):
                try:
                    h.update(("%s:%d" % (suffix, int(os.path.getmtime(base + suffix)))).encode())
                except OSError:
                    pass
        return h.hexdigest()
    except Exception:
        return None


def stall_check(key, ent, now):
    """(M3BZS1G3 ②) 무음 stall: pane 출력이 임계 이상 그대로면 STALL 코멘트(+pane tail),
    그 후에도 계속 무음이면 kill. wall-clock TIMEOUT과 구분되는 이벤트.
    반환 True면 이번 순회 추가 판정 생략."""
    session = ent.get("session", "")
    fp = pane_fingerprint(session, key)
    if fp is None:
        return False
    if fp != ent.get("pane_fp"):
        update_run(key, pane_fp=fp, pane_ts=now)
        return False
    silent_for = now - (ent.get("pane_ts") or ent.get("started") or now)
    if silent_for > STALL_SILENCE_S + STALL_KILL_AFTER_S:
        try:
            tmux_run("send-keys", "-t", session, "C-c", capture_output=True)
            time.sleep(10)
            tmux_run("kill-session", "-t", session, capture_output=True)
        except Exception:
            pass
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s STALL-killed — %ds 이상 pane 무음(exit와 무관) session=%s"
                   % (machine_name(), ent["dispatch_id"], int(silent_for), session))
        log("STALL-KILL dispatch#%s silent=%ds session=%s" % (ent["dispatch_id"], int(silent_for), session))
        update_run(key, status="failed", detail="stall-killed", ended=time.time())
        return True
    if silent_for > STALL_SILENCE_S and not ent.get("stall_notified"):
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s STALL — %ds째 pane 출력 무음(TIMEOUT 아님, %ds 더 무음 시 kill). "
                   "waiting_for=agent. pane tail:\n%s"
                   % (machine_name(), ent["dispatch_id"], int(silent_for), STALL_KILL_AFTER_S,
                      session_tail(session, 800)))
        log("STALL dispatch#%s silent=%ds session=%s" % (ent["dispatch_id"], int(silent_for), session))
        update_run(key, stall_notified=True)
        return True
    return False


def watch_once():
    runs = load_runs()
    for key, ent in list(runs.items()):
        if ent.get("status") != "running":
            continue
        base = key.replace("#", "_")
        done = os.path.join(RUNTIME_DIR, base + ".done")
        session = ent.get("session", "")
        if os.path.exists(done):
            try:
                with open(os.path.join(RUNTIME_DIR, base + ".exit")) as f:
                    rc = int(f.read().strip())
            except Exception:
                rc = 0
            finalize(None, key, ent, rc)
            continue
        if session and not session_alive(session):
            finalize(None, key, ent, 143, "session dead without marker; pane tail: " + session_tail(session))
            continue
        if session and stall_check(key, ent, time.time()):
            continue
        started = ent.get("started") or ent.get("ts") or 0
        timeout = ent.get("timeout_s") or DEFAULT_TIMEOUT_S
        if started and time.time() - started > timeout:
            try:
                tmux_run("send-keys", "-t", session, "C-c", capture_output=True)
                time.sleep(10)
                tmux_run("kill-session", "-t", session, capture_output=True)
            except Exception:
                pass
            tt_comment(ent["issue_id"], "runner:%s dispatch#%s TIMEOUT killed at %s session=%s"
                       % (machine_name(), ent["dispatch_id"], time.strftime("%F %T"), session))
            log("TIMEOUT dispatch#%s killed session=%s" % (ent["dispatch_id"], session))
            update_run(key, status="failed", detail="timeout", ended=time.time())


def watcher_loop(stop):
    while not stop.is_set():
        try:
            watch_once()
        except Exception as e:
            log("WATCH-ERROR %s: %s" % (type(e).__name__, e))
        stop.wait(WATCH_INTERVAL_S)


# ---------- polling (보상 경로; M3B5JHGP 인박스 = 실측 404) ----------

_inbox_warned = [0.0]


def poll_once():
    r = tt_http_raw("GET", "/agents/%s/pending" % urllib.parse.quote(RUNNER_NAME))
    if isinstance(r, dict) and r.get("HTTP") == 404:
        if time.time() - _inbox_warned[0] > 3600:
            _inbox_warned[0] = time.time()
            log("INBOX-NOT-READY /agents/%s/pending 404 — M3B5JHGP 미구현, webhook 경로만 유효" % RUNNER_NAME)
        return
    if not isinstance(r, list):
        return
    for item in r:
        try:
            deliver_item(item)
        except Exception as e:
            log("POLL-ITEM-FAIL %s" % e)


def deliver_item(p):
    """polling로 들어온 dispatch도 webhook과 동일한 execute_once 게이트를 통과한다."""
    if not all(k in p for k in ("dispatch_id", "issue_id", "message")):
        return
    if (p.get("message") or "").strip().lower() in APPROVE_WORDS and maybe_approve_release(p):
        return
    session, fresh = prepare(p)
    if fresh:
        execute_once(p, session)
    # ACK는 장부 done과 별개 — 실패해도 실행을 되돌리지 않는다.
    ack = tt_http_raw("POST", "/agents/%s/ack" % urllib.parse.quote(RUNNER_NAME),
                      {"dispatch_id": p["dispatch_id"]})
    if isinstance(ack, dict) and ack.get("HTTP") not in (None, 0):
        log("ACK-FAIL dispatch#%s HTTP %s (실행 유지)" % (p["dispatch_id"], ack["HTTP"]))


def poll_loop(stop):
    while not stop.is_set():
        try:
            poll_once()
        except Exception as e:
            log("POLL-ERROR %s: %s" % (type(e).__name__, e))
        stop.wait(POLL_INTERVAL_S)


# ---------- HTTP 수신기 (어댑터 골격 계승) ----------

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "name": "tt-runner", "machine": machine_name(),
                                    "bind": BIND, "port": PORT})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path != "/hook":
            return self._send(404, {"detail": "not found"})
        sec = load_secret()  # 기동 시 이미 fail-closed 확인
        auth = (self.headers.get("authorization") or "").replace("Bearer ", "")
        if auth != sec:
            log("REJECT 401 from %s" % self.client_address[0])
            return self._send(401, {"detail": "bad secret"})
        # reconcile release 명령 (서버 M3BZS1FS-5722 ③): dispatch가 아니라 x-tt-command로 온다.
        if self.headers.get("x-tt-command") == "release":
            try:
                n = int(self.headers.get("content-length", 0))
                cmd = json.loads(self.rfile.read(n).decode())
                if cmd.get("command") != "release" or "issue_id" not in cmd:
                    raise ValueError("not a release command")
            except Exception as e:
                return self._send(400, {"detail": "bad command payload: %s" % e})
            killed = release_issue(cmd["issue_id"], cmd.get("reason", ""))
            return self._send(200, {"released": killed})
        if not self.headers.get("x-tt-dispatch"):
            return self._send(400, {"detail": "missing X-Tt-Dispatch header"})
        try:
            n = int(self.headers.get("content-length", 0))
            p = json.loads(self.rfile.read(n).decode())
            for k in ("dispatch_id", "issue_id", "message"):
                if k not in p:
                    raise ValueError("missing " + k)
        except Exception as e:
            return self._send(400, {"detail": "bad payload: %s" % e})
        threading.Thread(target=self._safe, args=(p,), daemon=True).start()
        # 즉시 200: context용 세션 결정은 무결정(side-effect free) — claim은 백그라운드만
        return self._send(200, {"context": ctx_token(decide_session(p))})

    def _safe(self, p):
        try:
            msg = (p.get("message") or "").strip().lower()
            if msg in APPROVE_WORDS:
                if not maybe_approve_release(p):
                    log("APPROVE-NOTHING dispatch#%s — 대기 중인 held 런 없음" % p.get("dispatch_id"))
                return
            session, fresh = prepare(p)
            if fresh:
                execute_once(p, session)
        except Exception as e:
            log("ERROR dispatch#%s: %s: %s" % (p.get("dispatch_id"), type(e).__name__, e))

    def log_message(self, format, *args):  # http.server access log 억제
        pass


# ---------- CLI ----------

def cmd_status(key=None):
    runs = load_runs()
    if key:
        for k, v in runs.items():
            if k == key or k.endswith("#" + str(key)):
                print(json.dumps({k: v}, ensure_ascii=False, indent=1))
        return
    for k, v in sorted(runs.items()):
        print("%s  %-8s  %s  %s" % (k, v.get("status"), v.get("session"), v.get("exit", "")))


def cmd_attach(key):
    ent = find_by_key(key)
    if not ent:
        print("run not found: %s" % key)
        return 1
    os.execv("tmux", tmux_cmd("attach", "-t", ent["session"]))


def cmd_stop(key):
    ent = find_by_key(key)
    if not ent:
        print("run not found: %s" % key)
        return 1
    tmux_run("kill-session", "-t", ent["session"], capture_output=True)
    update_run(ent["_key"], status="failed", detail="stopped-by-cli", ended=time.time())
    print("killed %s" % ent["session"])


def find_by_key(key):
    runs = load_runs()
    for k, v in runs.items():
        if k == key or k.endswith("#" + str(key)):
            return dict(v, _key=k)
    return None


def release_issue(issue_id, reason=""):
    """서버 reconcile release 명령(M3BZS1FS-5722 ③): 이 이슈의 살아있는 런을 중지한다.
    순서: 장부를 먼저 cancelled(execute_once의 queued 게이트가 이후 시작을 차단) →
    tmux kill-session. 없으면 no-op. 반환: 종료한 세션 목록."""
    killed = []
    with ledger_lock():
        runs = load_runs()
        targets = [k for k, r in runs.items()
                   if str(r.get("issue_id")) == str(issue_id)
                   and r.get("status") in ("queued", "running", "held")]
        for k in targets:
            runs[k]["status"] = "cancelled"
            runs[k]["detail"] = "release-command"
            runs[k]["ended"] = time.time()
        if targets:
            save_runs(runs)
    for k in targets:
        ent = runs[k]
        session = ent.get("session") or ""
        if session:
            try:
                subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
            except Exception:
                pass
            killed.append(session)
        tt_comment(ent["issue_id"], "runner:%s dispatch#%s cancelled — 카드 terminalize로 실행 중지"
                   " (session=%s, %s)" % (machine_name(), ent.get("dispatch_id"), session, reason))
        log("RELEASE issue=%s key=%s session=%s (%s)" % (issue_id, k, session, reason))
    return killed


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    ensure_dirs()
    if cmd == "serve":
        load_secret()  # fail-closed
        if BIND == "0.0.0.0" or BIND == "":
            log("FATAL bind=0.0.0.0 금지 (tailnet/loopback만)")
            sys.exit(3)
        if not TT:
            log("FATAL TT_URL required")
            sys.exit(3)
        stop = threading.Event()
        threading.Thread(target=watcher_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=poll_loop, args=(stop,), daemon=True).start()
        log("START bind=%s:%d machine=%s name=%s" % (BIND, PORT, machine_name(), RUNNER_NAME))
        ThreadingHTTPServer((BIND, PORT), H).serve_forever()
    elif cmd == "poll":
        load_secret()
        poll_once()
    elif cmd == "status":
        cmd_status(argv[2] if len(argv) > 2 else None)
    elif cmd == "attach":
        return cmd_attach(argv[2])
    elif cmd == "stop":
        return cmd_stop(argv[2])
    elif cmd == "run-agent":
        # argv: run-agent <key> <state_dir> <secret_path> <registry> <tt_url>
        a = argv[2:7] + ["", "", "", "", ""]
        set_runtime(a[1] or None, a[2] or None, a[3] or None, a[4] or None)
        return run_agent(a[0])
    else:
        print("usage: tt-runner.py [serve|poll|status [key]|attach <key>|stop <key>]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
