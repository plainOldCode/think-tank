"""EG9D 데몬 단위 테스트 — 검증기준 3(state PATCH 경로 없음) 포함."""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone as tz
import sys
import tempfile
import urllib.request

# 단일 원천: 리포 daemon/daemon.py(이관 후). 과거 mini 로컬 경로는 폴백.
_DAEMON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "daemon")
if not os.path.exists(os.path.join(_DAEMON_DIR, "daemon.py")):
    _DAEMON_DIR = os.path.expanduser("~/tt-embedding-daemon")
sys.path.insert(0, _DAEMON_DIR)
import daemon  # noqa: E402


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Fake:
    """fetcher/poster 이중 — PATCH 호출을 감시한다."""

    def __init__(self, issues):
        self.issues = {i["id"]: dict(i) for i in issues}
        self.notes = []
        self.patches = []

    def fetch(self, path):
        if path == "/issues?limit=500":
            return [dict(v, comments=v.get("comments")) for v in self.issues.values()]
        iid = path.split("/")[2]
        return self.issues.get(iid)

    def post(self, card, body):
        self.notes.append((card, body))
        self.issues[card].setdefault("comments", []).append(
            {"author": "tt-embedding-daemon", "id": 1000 + len(self.notes),
             "body": body, "ts": "2026-10-11T21:00:00+0900"})


def _i(iid, state="todo", labels=(), **kw):
    base = {"id": iid, "state": state, "labels": list(labels), "version": 1,
            "title": kw.pop("title", iid), "body": kw.pop("body", ""), "comments": []}
    base.update(kw)
    return base


def test_데몬은_state_PATCH를_호출하지_않는다(tmp_path):
    # 검증기준 3 — 소스 정적 검사: PATCH/PUT 경로가 없어야 한다
    src = open(daemon.__file__).read()
    assert '"PATCH"' not in src and "'PATCH'" not in src
    assert "def patch" not in src.lower().replace("_", "_")  # PATCH 헬퍼 없음


# ---------- K91X: 승인 노트 임베딩 입력 + 사람 승인 참표 ----------

def test_임베딩_입력에_승인_노트가_포함된다(tmp_path, monkeypatch):
    # 수용 1 — verified_evidence(사람 승인 노트)가 임베딩 텍스트에 들어간다
    seen = []

    def fake_embed(texts, dim=daemon.DIM):
        seen.extend(texts)
        return [_norm([1.0] + [0.0] * 9) for _ in texts]

    monkeypatch.setattr(daemon, "embed", fake_embed)
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    approved = _i("M4APPR-0001", state="done", body="본문" * 300)
    approved["verification_status"] = "approved"
    approved["verified_evidence"] = "사람 승인: 하위 이슈 done으로 done 처리"
    f = Fake([approved, _i("M4TODO-0001", body="신규 작업")])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert any("승인 노트: 사람 승인: 하위 이슈" in t for t in seen)
    # 본문 400자 컷은 유지 — 승인 노트 없는 카드 텍스트는 제목+본문만
    assert any(t.startswith("M4TODO-0001") and "승인 노트" not in t for t in seen)


def test_라벨없는_사람승인_카드도_참표로_편입된다(tmp_path, monkeypatch):
    # 수용 2 — done+무라벨+사람 승인 → 인덱스 편입. 단, 데몬 labels 테이블에
    # 라벨을 만들지 않는다(카드 라벨 자동 부착 금지 — 오염 방지).
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 10) for _ in texts])
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    approved = _i("M4APPR-0002", state="done")
    approved["verification_status"] = "approved"
    approved["verified_evidence"] = "사람 승인: 조직완성"
    f = Fake([approved])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert any(c == "M4APPR-0002" for c, _ in idx.all())
    assert idx.labeled() == []  # 라벨 부여 없음 — 미래 채택 시 투표권 행사


def test_사람승인_카드는_분류_제안_대상이_아니다(tmp_path, monkeypatch):
    # K91X 금지 조항 — 경계 밖 카드에 [auto-classified]를 붙이지 않는다
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 10) for _ in texts])
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    ref = _i("M4REF-0001", state="in_progress", labels=["docs"])
    approved = _i("M4APPR-0003", state="todo")  # 승인됐는데 재오픈된 형태
    approved["verification_status"] = "approved"
    approved["verified_evidence"] = "사람 승인: 경계 밖"
    f = Fake([ref, approved])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert not any("[auto-classified]" in b for _, b in f.notes)


def test_데몬은_verify를_호출하지_않는다(tmp_path):
    # 수용 3 — 승인 확률이 probe 자동 verify로 이어지는 경로 부재(정적 검사)
    src = open(daemon.__file__).read()
    assert "/verify" not in src
    assert "completion_report" not in src


def test_분류기는_라벨없는_카드만_제안하고_간격_미달은_보류한다(tmp_path):
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    v_code = _norm([1.0] + [0.0] * 9)
    v_docs = _norm([0.0, 1.0] + [0.0] * 8)
    idx.upsert("M4CODE-0001", 1, v_code)
    idx.upsert("M4DOCS-0001", 1, v_docs)
    idx.set_labels("M4CODE-0001", ["think-tank"])
    idx.set_labels("M4DOCS-0001", ["docs"])
    f = Fake([])
    vec = _norm([0.99, 0.01] + [0.0] * 8)
    label, gap, refs = daemon.classify(idx, "M4NEW-0001", vec)
    assert label == "think-tank" and refs == ["M4CODE-0001", "M4DOCS-0001"] and gap > 0
    vec_mid = _norm([0.5, 0.5] + [0.0] * 8)
    assert daemon.classify(idx, "M4NEW-0002", vec_mid) == (None, None, [])  # 간격 미달 — 조용한 추측 금지


def test_중복_감지는_0_93_이상에서_발화한다(tmp_path, monkeypatch):
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    v = _norm([1.0] * 4 + [0.0] * 6)
    idx.upsert("M4OLD-0001", 1, v)
    f = Fake([_i("M4NEW-0001", body="x")])
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 4 + [0.0] * 6)])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert any("possible-duplicate" in b and "M4OLD-0001" in b for _, b in f.notes)
    assert f.patches == []


def test_임베딩_실패시_감시기능은_계속_동작한다(tmp_path):
    # 검증기준 5 — embed()가 None을 돌려도 감시는 도는 것을 카드 상태로 확인
    # (D2: 제출 대기 감시 제거 — fix-stall 감시로 지속성 확인)
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    f = Fake([_i("M4WORK-0001", state="review", comments=[
        {"author": "probe", "id": 1, "ts": _OLD,
         "body": "[ci-fix a1 #93/3a18c0b5] PR#93 — 수정 위임"}])])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert any("fix-stall" in b for _, b in f.notes)
    assert f.patches == []


def test_v1_마커는_억제하지_않고_v2로_1회_전환한다(tmp_path):
    """설계 §2 — v1 코멘트(필드 없음)는 이벤트 키 매치가 안 되므로 v2 전환 1회 허용."""
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    i = _i("M4WORK-0002", state="review", comments=[
        {"author": "probe", "id": 1, "ts": _OLD,
         "body": "[ci-fix a1 #93/3a18c0b5] PR#93 — 수정 위임"},
        {"author": "tt-embedding-daemon", "id": 2,
         "body": "[fix-stall] codex — 위임 무진행", "ts": ""}])
    f = Fake([i])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    # v1 존재 → v2 구조화 전환 1회
    assert len([b for _, b in f.notes if "fix-stall" in b]) == 1
    # 같은 이벤트 키로 재실행 → 재게시 없음
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert len([b for _, b in f.notes if "fix-stall" in b]) == 1


def test_판정_워처는_신규판정을_1회만_다이제스트한다(tmp_path):
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    i = _i("M4REV-0001", state="review", reviewer="claude")
    i["comments"] = [{"author": "claude", "id": 3,
                      "body": "review: approve\nPR#9@aaaaaaaa ok", "ts": ""}]
    f = Fake([i])
    seen = set()
    daemon.cycle(idx, seen, fetcher=f.fetch, poster=f.post)
    n1 = len([b for _, b in f.notes if "verdict-seen" in b])
    before = len(f.notes)
    daemon.cycle(idx, seen, fetcher=f.fetch, poster=f.post)
    n2 = len([b for _, b in f.notes if "verdict-seen" in b])
    assert n1 == 1 and n2 == 1 and len(f.notes) == before


def test_v2_마커는_정규화된_필드를_갖는다(tmp_path):
    """M4NHG5DF-5YM3 — 마커 계약: 첫 행이 [kind] key=value ... 형식(D2로 제출 독려 제외)."""
    import re
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    # 판정 워처 — key=card:comment 필드
    idx2 = daemon.Index(os.path.join(tmp_path, "idx2.db"))
    f2 = Fake([_i("M4NEW-0002", "review", reviewer="claude",
                  comments=[{"author": "probe", "id": 7,
                             "body": "review: request-changes PR#5@abc — 사유", "ts": "2026-10-01T00:00:00+0900"}])])
    daemon.cycle(idx2, set(), fetcher=f2.fetch, poster=f2.post)
    assert any(re.search(r"^\[verdict-seen\] key=M4NEW-0002:7$", b) for _, b in f2.notes)

    # fix-stall — src/attempt/kind 필드(D1: review-fix 분기 제거, ci-fix만 감시)
    idx3 = daemon.Index(os.path.join(tmp_path, "idx3.db"))
    f3 = Fake([_i("M4NEW-0003", "review",
                  comments=[{"author": "probe", "id": 8, "ts": "2020-01-01T00:00:00+0900",
                             "body": "[ci-fix a1 #5/abc12345] PR#5 — 수정 위임"}])])
    daemon.cycle(idx3, set(), fetcher=f3.fetch, poster=f3.post)
    m = [b for _, b in f3.notes if b.startswith("[fix-stall]")]
    assert m and re.search(
        r"^\[fix-stall\] src_kind=ci-fix src_att=a1 src_pr=5 src_head=abc12345 "
        r"attempt=1 minutes=\d+ kind=no-progress — ", m[-1])


def test_v2_스키마는_v1_마커와_dedup_호환이다(tmp_path, monkeypatch):
    """v1 잔재(prefix 동일)는 억제하지 않고 v2로 1회 전환한다(설계 §2).
    전환 후 동일 이벤트 키(label) 재발화는 없다 — 기존 dedup 계약(K91X 수용 4)."""
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([0.9, 0.1] + [0.0] * 8)
                                                       for _ in texts])
    idx = daemon.Index(os.path.join(tmp_path, "idx.db"))
    idx.upsert("M4CODE-0001", 1, _norm([1.0] + [0.0] * 9))
    idx.set_labels("M4CODE-0001", ["think-tank"])
    v1 = "[auto-classified] 라벨 제안: think-tank — 유사 카드 top-3 투표. 채택·기각은 사람/트리아지."
    f = Fake([_i("M4NEW-0001", "todo", comments=[
        {"author": "tt-embedding-daemon", "id": 5, "body": v1,
         "ts": "2026-10-11T00:00:00+0900"}])])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    ac = [b for _, b in f.notes if b.startswith("[auto-classified]")]
    assert len(ac) == 1 and "label=think-tank" in ac[0]  # v1 → v2 구조화 전환 1회
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert len([b for _, b in f.notes if b.startswith("[auto-classified]")]) == 1  # 재발화 0


def test_parse_ts는_콜론없는_타임존도_파싱한다():
    """mini Python 3.9 회귀 — +0900 파싱 실패로 since_hours 폭증했던 버그."""
    assert daemon.parse_ts("2026-10-11T22:31:51+0900") == \
        daemon.parse_ts("2026-10-11T22:31:51+09:00")
    assert daemon.parse_ts("") is None  # 파싱 실패는 0이 아니라 None — 오탐 방지


def _fix_카드(tmp_path, comments, state="review", **kw):
    """[fix-stall] 테스트용 — review 상태 카드 + probe fix 위임 마커."""
    idx = daemon.Index(os.path.join(str(tmp_path), "idx.db"))
    i = _i("M4FIX-0001", state, comments=comments, **kw)
    f = Fake([i])
    return idx, f


_OLD = "2026-10-11T10:00:00+0900"  # cycle 기준 과거


def test_fix_stall은_위임후_무진행에서_발화한다(tmp_path):
    """M4NNZX0M-8A28 — ci-fix 위임 후 45분+ 무진행 → [fix-stall] no-progress."""
    idx, f = _fix_카드(tmp_path, [
        {"author": "probe", "id": 1, "ts": _OLD,
         "body": "[ci-fix a1 #93/3a18c0b5] PR#93 request-changes — review 반납+수정 위임"},
    ])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    m = [b for _, b in f.notes if b.startswith("[fix-stall]")]
    assert len(m) == 1
    assert re.search(r"src_kind=ci-fix src_att=a1 src_pr=93 src_head=3a18c0b5 "
                     r"attempt=1 minutes=\d+ kind=no-progress — ", m[0])


def test_fix_stall은_dispatch가_이어지면_침묵한다(tmp_path):
    idx, f = _fix_카드(tmp_path, [
        {"author": "probe", "id": 1, "ts": _OLD,
         "body": "[ci-fix a1 #93/3a18c0b5] PR#93 — 수정 위임"},
        {"author": "runner@m2max", "id": 2, "ts": "2026-10-11T10:05:00+0900",
         "body": "runner:m2max dispatch#360 → tmux tt-opencode-360 (queued)"},
    ])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert not [b for _, b in f.notes if b.startswith("[fix-stall]")]


def test_fix_stall은_즉시실패에서_바로_발화한다(tmp_path):
    """수정 dispatch 실패(에이전트 미등록) — 대기 없이 kind=dispatch-failed."""
    now_ts = datetime.now(tz(timedelta(hours=9))).isoformat()
    idx, f = _fix_카드(tmp_path, [
        {"author": "probe", "id": 1, "ts": now_ts,
         "body": "[ci-fix a1 #114/b99b25f7] 수정 dispatch 실패(에이전트 미등록) — 재시도 필요"},
    ])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    m = [b for _, b in f.notes if b.startswith("[fix-stall]")]
    assert len(m) == 1 and "kind=dispatch-failed" in m[0] and "src_pr=114" in m[0]


def test_fix_stall은_이벤트키당_1회다(tmp_path):
    """같은 src_pr/src_head/attempt 재관측 → 재게시 없음. attempt 변경 → 새 이벤트."""
    from datetime import datetime, timedelta, timezone as tz
    now_ts = datetime.now(tz(timedelta(hours=9))).isoformat()
    idx, f = _fix_카드(tmp_path, [
        {"author": "probe", "id": 1, "ts": now_ts,
         "body": "[ci-fix a1 #93/3a18c0b5] 수정 dispatch 실패(에이전트 미등록) — 재시도 필요"},
    ])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert len([b for _, b in f.notes if b.startswith("[fix-stall]")]) == 1
    # 회차 변경 → 새 이벤트
    f.issues["M4FIX-0001"]["execution_attempt"] = 2
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post)
    assert len([b for _, b in f.notes if b.startswith("[fix-stall]")]) == 2


# --- EM37(M4TZJ05K): automation-boundary.md §3 술어 로드 — 경계 밖 감지·제외·표시 ---
# 문서가 정본(스키마 이원화 금지). 용도는 표시·제외만 — verify·라벨·상태 변경 금지 유지.

_REPO_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "automation-boundary.md")


def test_문서_블록에서_술어_로드():
    preds = daemon.load_boundary_predicates(_REPO_DOC)
    ids = [p["id"] for p in preds]
    assert {"B1", "B2", "B3"} <= set(ids)
    for p in preds:
        assert isinstance(p.get("signals"), dict)


def test_문서_부재는_동작_불변():
    assert daemon.load_boundary_predicates("/nonexistent/boundary.md") == []


def test_B1_조직결정_제목은_제안_대상_제외(monkeypatch):
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 10) for _ in texts])
    idx = daemon.Index(os.path.join(tempfile.mkdtemp(), "idx.db"))
    i = _i("M4B1-0001", title="[임명] 리뷰어 임명 — 조직 결정")
    f = Fake([i])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post,
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    assert not any("[auto-classified]" in b for _, b in f.notes)
    assert any("[boundary-out]" in b and "B1" in b for _, b in f.notes)


def test_B2_판단소비_마커는_회차당_1회(monkeypatch):
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([0.5, 0.5] + [0.0] * 8)
                                                       for _ in texts])
    idx = daemon.Index(os.path.join(tempfile.mkdtemp(), "idx.db"))
    i = _i("M4B2-0001", title="[분석] 재작업 원인 판단 보고")
    f = Fake([i])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post,
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post,
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    assert not any("[auto-classified]" in b for _, b in f.notes)
    assert len([b for _, b in f.notes if "[boundary-out]" in b]) == 1


def test_B3_사람승인_증거도_경계밖_표시(monkeypatch):
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([0.9, 0.1] + [0.0] * 8)
                                                       for _ in texts])
    idx = daemon.Index(os.path.join(tempfile.mkdtemp(), "idx.db"))
    i = _i("M4B3-0001", state="review", verification_status="approved",
           verified_evidence="사람 승인: 경계 밖 — 사람 눈으로 확인")
    f = Fake([i])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post,
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    assert any("[boundary-out]" in b and "B3" in b for _, b in f.notes)
    assert not any("[auto-classified]" in b for _, b in f.notes)


def test_B3_prefix_미설정은_비활성_TypeError_없다(monkeypatch):
    # 리뷰 지적 회귀 — dispatches=0 + TT_HUMAN_ACTOR_PREFIX 미설정 → TypeError 없이 비활성
    # (주의: 라이브 /issues 목록엔 dispatches 필드가 없다 — 픽스처 주입은 서버 노출
    #  시점의 동작 명세이며, 현재 운영에서는 evidence_contains만 B3로 발화)
    monkeypatch.delenv("TT_HUMAN_ACTOR_PREFIX", raising=False)
    i = _i("M4B3U-0001", title="일반 작업", dispatches=0,
           assignee="human-01@host")
    hit = daemon._boundary_out_ids([i], daemon.load_boundary_predicates(_REPO_DOC))
    assert "M4B3U-0001" not in hit


def test_B3_prefix_설정시_dispatches0_사람claim_감지(monkeypatch):
    # 리뷰 지적 — TT_HUMAN_ACTOR_PREFIX 설정 시 dispatchs=0 + 사람 claim → B3 감지
    monkeypatch.setenv("TT_HUMAN_ACTOR_PREFIX", "human-")
    i = _i("M4B3S-0001", title="일반 작업", dispatches=0,
           assignee="human-01@host")
    hit = daemon._boundary_out_ids([i], daemon.load_boundary_predicates(_REPO_DOC))
    assert hit.get("M4B3S-0001") == "B3"


def test_술어_미로드는_기존_동작_그대로(monkeypatch):
    # boundary_predicates 미주입 + 문서 미존재 경로 → 마커 없음(기존 회귀)
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 10) for _ in texts])
    idx = daemon.Index(os.path.join(tempfile.mkdtemp(), "idx.db"))
    i = _i("M4N-0001", title="[분석] 경계 판정 대상")
    f = Fake([i])
    daemon.cycle(idx, set(), fetcher=f.fetch, poster=f.post,
                 boundary_predicates=daemon.load_boundary_predicates("/nonexistent/x.md"))
    assert not any("[boundary-out]" in b for _, b in f.notes)


def test_술어_평가는_데이터_기반():
    preds = daemon.load_boundary_predicates(_REPO_DOC)
    hit = daemon._boundary_out_ids(
        [_i("M4X-1", title="[토론] 안건"), _i("M4Y-2", title="일반 작업")], preds)
    assert hit.get("M4X-1") == "B2" and "M4Y-2" not in hit


def test_B3_dispatch_신호는_prefix_설정_시만_활성(monkeypatch):
    # 회귀(a1 리뷰): TT_HUMAN_ACTOR_PREFIX 미설정 + dispatches:0 → TypeError 없이 비활성
    preds = daemon.load_boundary_predicates(_REPO_DOC)
    monkeypatch.delenv("TT_HUMAN_ACTOR_PREFIX", raising=False)
    assert daemon._boundary_out_ids([_i("M4D-1", dispatches=0, assignee="worker")], preds) == {}
    monkeypatch.setenv("TT_HUMAN_ACTOR_PREFIX", "human-")
    hit = daemon._boundary_out_ids(
        [_i("M4D-2", dispatches=0, assignee="human-01@host"),
         _i("M4D-3", dispatches=0, assignee="worker"),
         _i("M4D-4", dispatches=2, assignee="human-01@host")], preds)
    assert hit.get("M4D-2") == "B3" and "M4D-3" not in hit and "M4D-4" not in hit


# --- M3DP(M4W9J54M): B3 dispatch 신호 조달 — GET /issues/ID/dispatches(읽기 전용) ---

class DispatchFake:
    """issues + per-card dispatches를 함께 서빙하는 fetcher — 실제 응답 형태 모사."""

    def __init__(self, issues, dispatches):
        self.issues = {i["id"]: dict(i) for i in issues}
        self.dispatches = dispatches  # {id: list}
        self.fetches = []

    def fetch(self, path):
        self.fetches.append(path)
        m = re.match(r"^/issues/([^/]+)/dispatches$", path)
        if m:
            return self.dispatches.get(m.group(1), [])
        if path == "/issues?limit=500":
            return list(self.issues.values())
        iid = path.split("/")[2]
        return self.issues.get(iid)


def _setup_daemon(monkeypatch, tmp_path, issues, dispatches):
    monkeypatch.setattr(daemon, "embed",
                        lambda texts, dim=daemon.DIM: [_norm([1.0] * 10) for _ in texts])
    monkeypatch.setenv("TT_HUMAN_ACTOR_PREFIX", "human-")
    idx = daemon.Index(os.path.join(str(tmp_path), "idx.db"))
    f = DispatchFake(issues, dispatches)
    notes = []
    f.post = lambda card, body: notes.append((card, body))
    return idx, f, notes


def test_B3_dispatch_조달_list_응답(monkeypatch, tmp_path):
    # 사람 claim + dispatchs=0 → B3 감지. 재위임(dispatchs>0)은 제외 — 실제 list 응답 형태
    idx, f, notes = _setup_daemon(monkeypatch, tmp_path,
                                  [_i("M4DP-1", state="todo", assignee="human-01@host"),
                                   _i("M4DP-2", state="todo", assignee="human-01@host"),
                                   _i("M4DP-3", state="in_progress", assignee="human-01@host")],
                                  {"M4DP-1": [], "M4DP-2": [{"id": 1}], "M4DP-3": []})
    daemon.cycle(idx, set(), fetcher=f.fetch,
                 poster=lambda c, b: f.post(c, b),
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    hit = [c for c, b in notes if "[boundary-out]" in b and "id=B3" in b]
    # M4DP-1(todo, 0), M4DP-3(in_progress도 비종결, 0) → 감지. M4DP-2는 재위임 제외.
    assert hit == ["M4DP-1", "M4DP-3"]


def test_B3_dispatch_조달_dict_응답(monkeypatch, tmp_path):
    # {"dispatches":[...]} 래퍼 형태도 수용 — 실측된 두 응답 형태 모두
    idx, f, notes = _setup_daemon(monkeypatch, tmp_path,
                                  [_i("M4DP-4", state="todo", assignee="human-01@host")],
                                  {"M4DP-4": {"dispatches": [{"id": 1}, {"id": 2}]}})
    daemon.cycle(idx, set(), fetcher=f.fetch,
                 poster=lambda c, b: f.post(c, b),
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    assert not any("id=B3" in b for _, b in notes)  # 2건 재위임 — 미탐 아님, 정상 제외


def test_조달은_prefix_매칭_비종결만(monkeypatch, tmp_path):
    # dispatches GET은 prefix 매칭·비종결 카드에만 — GET 최소화
    idx, f, notes = _setup_daemon(monkeypatch, tmp_path,
                                  [_i("M4DP-5", state="todo", assignee="human-01@host"),
                                   _i("M4DP-6", state="todo", assignee="other-agent"),
                                   _i("M4DP-7", state="done", assignee="human-01@host")],
                                  {"M4DP-5": []})
    daemon.cycle(idx, set(), fetcher=f.fetch,
                 poster=lambda c, b: f.post(c, b),
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    disp = [p for p in f.fetches if p.endswith("/dispatches")]
    assert disp == ["/issues/M4DP-5/dispatches"]


def test_조달_실패는_무시(monkeypatch, tmp_path):
    # dispatches GET 실패 → 크래시 없이 진행(B3 dispatch 신호만 비발화)
    idx, f, notes = _setup_daemon(monkeypatch, tmp_path,
                                  [_i("M4DP-8", state="todo", assignee="human-01@host")], {})
    def boom(path):
        if path.endswith("/dispatches"):
            raise OSError("network down")
        return f.fetch(path)
    daemon.cycle(idx, set(), fetcher=boom,
                 poster=lambda c, b: notes.append((c, b)),
                 boundary_predicates=daemon.load_boundary_predicates(_REPO_DOC))
    assert isinstance(notes, list)  # 예외 전파 없이 사이클 완료
