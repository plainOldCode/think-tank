"""M6V3(M58SPTKC-M6V3): Verdict 노드 키 통일 — (card, attempt, subject_hash),
PR@sha8은 보조 속성. 판정 파싱은 verdicts.py 공유 파서 재사용(새 파서 금지)."""
import hashlib
import json

import review_subject
from ontology import node_key, project


def token(card_id, attempt, seed="x"):
    raw = json.dumps({"seed": seed, "id": card_id, "attempt": attempt}, sort_keys=True)
    return f"TT#{card_id}@a{attempt}/{hashlib.sha256(raw.encode()).hexdigest()}"


def test_new_and_legacy_project_to_same_rule():
    """수용기준 1 — 신·구 형식이 같은 키 규칙으로 투영된다."""
    new = project("M4JYTVXP-WVBY", 1, f"review: approve\n{token('M4JYTVXP-WVBY', 1)}",
                  "codex", comment_id=6916)
    assert node_key(new) == ("M4JYTVXP-WVBY", 1, new["subject_hash"])
    assert new["legacy"] is False and new["verdict"] == "approve"
    legacy = project("M4JYTVXP-WVBY", 2, "review: request-changes\nPR#79@c5f85f2b",
                     "agy", comment_id=3656)
    assert node_key(legacy) == ("M4JYTVXP-WVBY", 2, None)  # 레거시는 hash 없이 (card, attempt)
    assert legacy["legacy"] is True
    assert legacy["pr"] == 79 and legacy["sha8"] == "c5f85f2b"


def test_legacy_sha8_normalized():
    legacy = project("C", 1, "review: approve\nPR#12@C5F85F2B", "agy")
    assert legacy["sha8"] == "c5f85f2b"  # 대소문자 무관 — 노드 키 아님, 보조 속성


def test_no_token_verdict_not_projected():
    assert project("C", 1, "review: approve\n근거만 있고 바인딩 토큰이 없다", "agy") is None
    assert project("C", 1, "just chat", "agy") is None


def test_subject_hash_change_supersedes_previous_approval(client):
    """수용기준 2 — subject hash 변화 시 이전 승인 무효(supersedes)."""
    from test_tt_lifecycle import snap, submitted
    issue = submitted(client)
    t1 = issue["review_subject"]
    v1 = project(issue["id"], issue["execution_attempt"],
                 f"review: approve\n{t1}", "codex", comment_id=1)
    # 같은 회차에서 보고 재제출 → review_subject 갱신(이전 승인 무효)
    import probe.core as core
    proof = dict(issue["completion_report"])
    proof["implementation"] = dict(proof["implementation"], commands="test v2")
    r = client.patch(f"/issues/{issue['id']}",
                     json={"state": "done", "completion_report": proof})
    assert r.status_code == 200, r.text
    issue2 = client.get(f"/issues/{issue['id']}").json()
    t2 = review_subject.subject(issue2)
    assert t2 != t1  # 새 판정 대상
    assert review_subject.verdict(issue2) is None  # 이전 승인은 새 subject에 무효
    v2 = project(issue2["id"], issue2["execution_attempt"],
                 f"review: approve\n{t2}", "codex", comment_id=2)
    assert node_key(v1) != node_key(v2)  # 키가 갈라진다 — 이전 승인 노드는 stale
    assert node_key(v2)[2] == t2.split("/")[1]
    # 현재 subject 키는 v2와 정확히 일치 — 그래프 소비자는 이 키만 승인으로 읽는다
    assert node_key(v2) == (issue2["id"], issue2["execution_attempt"], t2.split("/")[1])
    assert not core.decide(snap(issue2)) or all(a["action"] != "tt-verify"
                                                for a in core.decide(snap(issue2)))


def test_projection_uses_shared_parser_only():
    """판정어 인식은 공유 파서 — project는 first_line_verdict를 통해서만 본다."""
    import inspect
    import ontology.verdict_key as vk
    src = inspect.getsource(vk)
    assert "re.compile" in src  # 토큰 파싱 정규식만
    assert "approve|request-changes" not in src  # 판정어 정규식을 새로 만들지 않는다
