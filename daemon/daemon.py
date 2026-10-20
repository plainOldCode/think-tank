#!/usr/bin/env python3
"""TT 임베딩 데몬 — 관측 전용(M4MTW9RG-EG9D).

원칙(카드 명세): 데몬은 관측·기록만 하고 액션이 필요하면 probe 규칙표의
입력(마커·라벨·카운터)을 만든다. state PATCH 경로를 갖지 않는다 —
tests가 이를 보장한다(검증기준 3).

기능 4종(단일 폴링 루프):
  1. 분류기       — 라벨 없는 카드 → 인덱스 top-k 투표 → [auto-classified] 제안
  2. fix 미실행   — [ci-fix] 위임 후 무진행/즉시실패 → [fix-stall](BG97 감지부)
  3. 판정 워처    — 신규 판정 코멘트 → [verdict-seen] 다이제스트
  4. 중복 감지    — 라벨 없는 신규 카드가 기존 카드와 0.93+ → [possible-duplicate]

  D2(M5BH92HC-9RAE, 사용자 결정): 제출 대기 감시는 제거했다 — probe가 유일한
  submit-remind 생산자(PR CI green 트리거, D2 단일화 결정).

  D1(M5BH925Y-G09E, 사용자 결정 a): 생산자가 사라진 레거시 마커 감시 2종과
  fix 위임의 레거시 마커 분기는 제거했다 — 라이브 마지막 출현 10-13, 현행
  생산자는 tt-review 계열과 ci-fix뿐이다.

마커 dedup: (마커, 카드, 키) 재게시 금지. 임베딩 실패 시 루프 생존, 1·6만 보류.
"""
import datetime
import json
import math
import os
import sqlite3
import re
import sys
import time
import urllib.request

TT_URL = os.environ.get("TT_URL", "http://127.0.0.1:7800")
OLLAMA = os.environ.get("TT_EMBED_URL", "http://localhost:11434")
MODEL = "embeddinggemma-2:270m-mxfp8-text"
DIM = 256
POLL = int(os.environ.get("TT_DAEMON_POLL", "60"))
DB_PATH = os.environ.get("TT_DAEMON_DB",
                         os.path.expanduser("~/tt-embedding-daemon/index.db"))
DAEMON = "tt-embedding-daemon"
FIX_STALL_MIN = float(os.environ.get("TT_DAEMON_FIX_STALL_MIN", "45"))
DUP_THRESHOLD = 0.93
LABEL_GAP = 0.01
LABEL_TOPK = 3


def http_json(url, path, method="GET", body=None):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def embed(texts, dim=DIM):
    """Ollama /api/embed → MRL 절단·재정규화. 실패 시 None(graceful degradation)."""
    try:
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": MODEL, "input": list(texts)}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
        vecs = []
        for v in out["embeddings"]:
            v = v[:dim]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / n for x in v])
        return vecs
    except Exception:
        return None


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def parse_ts(s):
    """ISO8601 파싱 — 콜론 없는 타임존(+0900)도 수용(Python 3.9 호환). 실패 시 0이 아니라 None."""
    if not s:
        return None
    t = s.strip()
    m = re.search(r"([+-]\d{2}):?(\d{2})$", t)
    if m:
        t = t[:m.start()] + f"{m.group(1)}:{m.group(2)}"
    try:
        return datetime.datetime.fromisoformat(t).timestamp()
    except Exception:
        return None


class Index:
    """카드 벡터 인덱스 — SQLite 단일 파일. TT issues 테이블이 아니라 관측 캐시다."""

    def __init__(self, path):
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE IF NOT EXISTS vectors (
            card TEXT PRIMARY KEY, version INTEGER, dim INTEGER, vec TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS labels (
            card TEXT, label TEXT, PRIMARY KEY (card, label))""")
        con.commit()
        self.con = con

    def upsert(self, card, version, vec):
        self.con.execute(
            "INSERT OR REPLACE INTO vectors(card, version, dim, vec) VALUES(?,?,?,?)",
            (card, version, len(vec), json.dumps(vec)))
        self.con.commit()

    def known(self, card, version):
        row = self.con.execute("SELECT version FROM vectors WHERE card=?",
                               (card,)).fetchone()
        return row and row[0] >= version

    def all(self):
        return [(c, json.loads(v)) for c, v in self.con.execute(
            "SELECT card, vec FROM vectors")]

    def labeled(self):
        """참표 — 라벨 매겨진 인덱스 항목(카드, 벡터, 라벨들)."""
        out = []
        for c, v in self.all():
            rows = self.con.execute("SELECT label FROM labels WHERE card=?",
                                    (c,)).fetchall()
            if rows:
                out.append((c, v, [r[0] for r in rows]))
        return out

    def set_labels(self, card, labels):
        self.con.execute("DELETE FROM labels WHERE card=?", (card,))
        self.con.executemany("INSERT INTO labels(card, label) VALUES(?,?)",
                             [(card, l) for l in labels])
        self.con.commit()


def classify(index, card, vec, topk=LABEL_TOPK, gap=LABEL_GAP):
    """top-k 라벨 투표 → (label, gap, refs). 상위 2 라벨 점수차 < gap*topk면 (None,None,[])."""
    votes = [(cos(vec, v), c, labels) for c, v, labels in index.labeled() if c != card]
    votes.sort(reverse=True)
    counts = {}
    for s, _, labels in votes[:topk]:
        for l in labels:
            counts[l] = counts.get(l, 0.0) + s
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if not ranked:
        return None, None, []
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < gap * topk:
        return None, None, []
    gap_v = ranked[0][1] - ranked[1][1] if len(ranked) > 1 else ranked[0][1]
    refs = [c for _, c, _ in votes[:topk]]
    return ranked[0][0], gap_v, refs


def post_note(card, body, poster):
    """관측 기록 게시 — 코멘트만, 상태 전이 없음. 실패는 로그만."""
    try:
        http_json(TT_URL, f"/issues/{card}/comments", "POST",
                  {"author": poster, "body": body})
        return True
    except Exception as e:
        print(f"[warn] note fail {card}: {e}", file=sys.stderr)
        return False


def _comments(card, fetcher):
    try:
        return (fetcher(f"/issues/{card}") or {}).get("comments") or []
    except Exception:
        return []


def _fetch(path):
    return http_json(TT_URL, path)


def _boundary_doc_path():
    """docs/automation-boundary.md 탐색 — env > daemon.py 기준 리포 레이아웃 > mini 미러."""
    p = os.environ.get("TT_BOUNDARY_DOC")
    if p:
        return p
    here = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         os.pardir, "docs", "automation-boundary.md"))
    if os.path.exists(here):
        return here
    return os.path.expanduser("~/tt-repo/docs/automation-boundary.md")


def load_boundary_predicates(path=None):
    """automation-boundary.md §3 JSON 블록의 boundary_predicates 로드.

    문서가 정본(스키마 이원화 금지) — 파일 부재·파싱 실패 시 [] (동작 불변)."""
    p = path or _boundary_doc_path()
    try:
        text = open(p, encoding="utf-8").read()
    except OSError:
        return []
    src = None
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        if "boundary_predicates" in block:
            src = block
            break
    if not src:
        return []
    try:
        data = json.loads(src, strict=False)  # 문서 note 필드에 원시 개행 허용
    except ValueError:
        return []
    return [pr for pr in (data.get("boundary_predicates") or [])
            if isinstance(pr, dict) and pr.get("id")]


def _dispatch_counts(iids, fetcher):
    """B3 dispatch 신호 조달(M3DP) — prefix 매칭 비종결 카드에 한해
    GET /issues/ID/dispatches(읽기 전용)로 건수를 채운다. 실제 응답은
    list(runner/tt-runner.py) 또는 {"dispatches":[...]} 래퍼 — 둘 다 수용.
    개별 조회 실패는 무시(해당 카드만 신호 비발화, 사이클 계속)."""
    out = {}
    for iid in iids:
        try:
            d = fetcher(f"/issues/{iid}/dispatches")
        except Exception:
            continue
        if isinstance(d, dict):
            d = d.get("dispatches") or []
        if isinstance(d, list):
            out[iid] = len(d)
    return out


def _boundary_out_ids(issues, preds):
    """술어 평가 — 데이터 기반: title_prefix·evidence_contains·
    dispatchs_zero_with_manual_claim(TT_HUMAN_ACTOR_PREFIX 설정 시 활성).

    B3의 dispatchs는 cycle()이 prefix 매칭 비종결 카드에 한해
    /issues/ID/dispatches로 조달해 주입한다(M3DP). 알 수 없는 signal 키는
    무시 — 문서 선진화 대응."""
    prefix = os.environ.get("TT_HUMAN_ACTOR_PREFIX")
    out = {}
    for i in issues:
        title = i.get("title") or ""
        ev = i.get("verified_evidence") or ""
        who = i.get("assignee") or i.get("lease_by") or ""
        for pred in preds:
            sig = pred.get("signals") or {}
            hit = any(title.startswith(s) for s in (sig.get("title_prefix") or []))
            hit = hit or any(n in ev for n in (sig.get("evidence_contains") or []))
            if sig.get("dispatches_zero_with_manual_claim") and prefix \
                    and i.get("dispatches") == 0 and who.startswith(prefix):
                hit = True
            if hit:
                out.setdefault(i["id"], pred["id"])
                break
    return out


def cycle(index, seen, fetcher=None, poster=None, boundary_predicates=None):
    """1회 폴링. fetcher/poster 주입(테스트) — 기본은 TT_URL 단일 원천."""
    fetcher = fetcher or _fetch
    poster = poster or (lambda card, body: post_note(card, body, DAEMON))
    issues = fetcher("/issues?limit=500") or []
    if not issues:
        return
    now = time.time()

    # --- 0: 참표 구축 — 라벨 있는 카드 + 사람 승인 카드(K91X: 라벨 없어도 참표 편입).
    # 승인 노트(verified_evidence)는 임베딩 입력에 포함 — 사람이 왜 승인했는지가
    # 학습 입력에서 증발하지 않게 한다. 용도는 경계 감지이지 자동 verify가 아니다.
    def _card_text(i):
        ev = (i.get("verified_evidence") or "").strip()
        return (f"{i.get('title') or ''}\n{(i.get('body') or '')[:400]}"
                + (f"\n승인 노트: {ev}" if ev else ""))

    def _human_approved(i):
        return (i.get("verification_status") == "approved"
                and "사람 승인" in (i.get("verified_evidence") or ""))

    labeled_cards = [i for i in issues if i["state"] not in ("done", "cancelled")
                     and [l for l in (i.get("labels") or []) if l != "auto"]]
    approved_ids = {i["id"] for i in issues if _human_approved(i)}
    labeled_ids = {i["id"] for i in labeled_cards}
    approved_refs = [i for i in issues
                     if i["id"] in approved_ids and i["id"] not in labeled_ids]
    # --- 0b: 경계 밖 감지(EM37) — 문서 §3 술어 로드 → 표시·제외만. verify·라벨·
    # 상태 변경 없음(K91X 금지 계약 유지). 술어 하나라도 참이면 경계 밖.
    preds = boundary_predicates if boundary_predicates is not None \
        else load_boundary_predicates()
    # --- 0b-전: B3 dispatch 조달(M3DP) — GET 최소화: prefix 매칭·비종결만.
    prefix = os.environ.get("TT_HUMAN_ACTOR_PREFIX")
    if prefix and preds:
        cand = [i for i in issues
                if i["state"] not in ("done", "cancelled")
                and (i.get("assignee") or i.get("lease_by") or "").startswith(prefix)]
        counts = _dispatch_counts([i["id"] for i in cand], fetcher)
        for i in cand:
            if i["id"] in counts:
                i["dispatches"] = counts[i["id"]]
    boundary_ids = _boundary_out_ids(issues, preds) if preds else {}
    for i in issues:
        if i["id"] in boundary_ids and i["state"] not in ("done", "cancelled"):
            comments = _comments(i["id"], fetcher)
            pid = boundary_ids[i["id"]]
            pred = next(p for p in preds if p["id"] == pid)
            if not _event_key_present(comments, "boundary-out", {"id": pid}):
                poster(i["id"], f"[boundary-out] id={pid} "
                                f"name={pred.get('name', '')} — "
                                f"{pred.get('effect', '자동화 대상 제외')} "
                                f"(docs/automation-boundary.md §3)")
    # --- 1·6: 분류기 + 중복 감지(임베딩 필요 — 실패 시 이 구간만 보류) ---
    # 사람 승인(경계 밖) 카드는 제안 대상에서 제외 — 오염 방지(K91X 금지 조항).
    unclassified = [i for i in issues if i["state"] in ("todo", "backlog")
                    and not [l for l in (i.get("labels") or []) if l != "auto"]
                    and i["id"] not in approved_ids
                    and i["id"] not in boundary_ids]
    embeds = embed([_card_text(i) for i in labeled_cards + approved_refs + unclassified]) \
        if (labeled_cards or approved_refs or unclassified) else None
    if embeds:
        offset = len(labeled_cards)
        offset2 = offset + len(approved_refs)
        for j, i in enumerate(labeled_cards):
            index.upsert(i["id"], i.get("version") or 0, embeds[j])
            index.set_labels(i["id"], [l for l in (i.get("labels") or []) if l != "auto"])
        # 사람 승인 참표 — 인덱스에만 편입. 라벨 자동 부착 금지(카드 labels 불변,
        # 데몬 내부 labels 테이블도 카드 라벨이 있을 때만 기록).
        for j, i in enumerate(approved_refs):
            index.upsert(i["id"], i.get("version") or 0, embeds[offset + j])
            if [l for l in (i.get("labels") or []) if l != "auto"]:
                index.set_labels(i["id"], [l for l in (i.get("labels") or []) if l != "auto"])
        for j, i in enumerate(unclassified):
            vec = embeds[offset2 + j]
            index.upsert(i["id"], i.get("version") or 0, vec)
            label, label_gap, refs = classify(index, i["id"], vec)
            comments = _comments(i["id"], fetcher)
            if label and not _event_key_present(comments, "auto-classified", {"label": label}):
                poster(i["id"], f"[auto-classified] label={label} "
                                f"gap={label_gap:.3f} refs={','.join(refs)} — "
                                f"채택·기각은 사람/트리아지.")
            for c, v in index.all():
                if c != i["id"] and cos(vec, v) >= DUP_THRESHOLD:
                    if not _event_key_present(comments, "possible-duplicate", {"ref": c}):
                        poster(i["id"], f"[possible-duplicate] ref={c} "
                                        f"sim={cos(vec, v):.2f} — 중복 확인 요망")
                    break

    for i in issues:
        iid, st = i["id"], i["state"]
        comments = _comments(iid, fetcher)

        # --- 7: fix 위임 미실행 감시(BG97 감지부) — [ci-fix] 위임 후 무진행 또는 즉시 실패 ---
        if st not in ("done", "cancelled"):
            fix_c = _fix_marker(comments)
            if fix_c:
                fb = fix_c.get("body") or ""
                parts = _decompose_src(act_key(fb))
                if parts:
                    sk, satt, spr, shead = parts
                    fts = parse_ts(fix_c.get("ts") or "")
                    att_card = str(i.get("execution_attempt") or 1)
                    immediate = "실패" in fb
                    minutes = ((now - fts) / 60) if fts else 0.0
                    if (immediate or (fts and minutes > FIX_STALL_MIN)) and \
                            not _progressed(comments, fix_c, i) and \
                            not _event_key_present(comments, "fix-stall",
                                                   {"src_kind": sk, "src_pr": spr,
                                                    "src_head": shead, "attempt": att_card}):
                        poster(iid, f"[fix-stall] src_kind={sk} src_att={satt} "
                                    f"src_pr={spr} src_head={shead} attempt={att_card} "
                                    f"minutes={minutes:.0f} "
                                    f"kind={'dispatch-failed' if immediate else 'no-progress'} "
                                    f"— tt-review-fix 재발령 검토(BG97)")

        # --- 5: 판정 워처 ---
        for c in comments:
            b = c.get("body") or ""
            head = b.split("\n", 1)[0].strip()
            if head.startswith("review: ") and \
                    ("approve" in head or "request-changes" in head):
                key = f"{iid}:{c.get('id')}"
                if key not in seen:
                    seen.add(key)
                    if not _event_key_present(comments, "verdict-seen", {"key": key}):
                        poster(iid, f"[verdict-seen] key={key}")
                break


def _marker_fields(body):
    """마커 첫 행의 key=value 필드 dict — 이벤트 키 dedup용."""
    line = (body or "").split("\n", 1)[0]
    m = re.match(r"^\[[a-z-]+\]\s+(.*)$", line)
    if not m:
        return {}
    out = {}
    for tok in m.group(1).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _event_key_present(comments, kind, key_fields):
    """v2 이벤트 키 dedup — 같은 kind에 같은 키 필드값을 가진 데몬 코멘트 존재 여부.

    v1 코멘트는 key=value 필드가 없어 매치되지 않는다 — v1은 억제하지 않고
    v2로 1회 전환한다(설계 §2). """
    for c in comments:
        if c.get("author") != DAEMON:
            continue
        b = c.get("body") or ""
        if not b.startswith(f"[{kind}]"):
            continue
        f = _marker_fields(b)
        if all(f.get(k) == v for k, v in key_fields.items()):
            return True
    return False


def _decompose_src(token):
    """probe 원본 마커 [kind [aN] #PR/sha] → (kind, att, pr, head) — 역파싱 가능 정규형."""
    m = re.match(r"^\[([a-z-]+)(?:\s+(a\d+))?\s+#(\d+)/([0-9a-f]{6,8})\]$",
                 (token or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2) or "-", m.group(3), m.group(4)


def _fix_marker(comments):
    """최신 probe fix 위임 마커 코멘트 — [ci-fix aN #n/sha].

    D1: 레거시 review- 마커는 생산자가 없어(마지막 10-12) 감시 대상에서 제외했다."""
    for c in reversed(comments):
        b = c.get("body") or ""
        if c.get("author") == "probe" and re.match(r"^\[ci-fix\b", b):
            return c
    return None


def _progressed(comments, fix_c, issue):
    """fix 마커 이후 진행 증거 — dispatch 실행·재리뷰·판정·수령."""
    fts = parse_ts(fix_c.get("ts") or "")
    for c in comments:
        if c is fix_c:
            continue
        cts = parse_ts(c.get("ts") or "")
        if cts is None or fts is None or cts <= fts:
            continue
        b = c.get("body") or ""
        if c.get("author", "").startswith("runner@") and "dispatch#" in b:
            return True
        if b.split("\n", 1)[0].startswith("review: "):
            return True
    if issue["state"] == "in_progress" and issue.get("lease_by"):
        return True
    return False


def act_key(body):
    m = re.search(r"\[[^\]]+\]", body or "")
    if m:
        return m.group(0)
    return (body or "")[:24]


def loop(poll=POLL, stop=None, index=None, fetcher=None, poster=None):
    index = index or Index(DB_PATH)
    seen = set()
    fetcher = fetcher or _fetch
    while True:
        if stop and stop():
            return
        try:
            cycle(index, seen, fetcher=fetcher, poster=poster)
        except Exception as e:
            print(f"[warn] cycle fail: {e}", file=sys.stderr)
        time.sleep(poll)


def once():
    idx_dir = os.path.dirname(DB_PATH)
    os.makedirs(idx_dir, exist_ok=True)
    cycle(Index(DB_PATH), set())


if __name__ == "__main__":
    if "--once" in sys.argv:
        once()
    else:
        loop()
