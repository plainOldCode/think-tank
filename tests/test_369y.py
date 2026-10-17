"""369Y(M5283JJH-369Y) — 같은 지적 반복은 구조로 막기(/correct 루프).

반려 사유를 분류해 2회 이상 반복된 유형은 구조화 체크(lint/테스트/계약) 승격
지시를 발령문에 넣고, 3회째 반려 카드는 premise 재검토를 강제한다.
pstack: correct, encode-lessons-in-structure, attack-the-premise.
"""
import json

import probe.core as core
import probe.tt_lifecycle as tt
from test_tt_lifecycle import submitted, approve, snap

_HERE_OK = True  # helpers import via test modules


def _verdict_comment(author, text):
    return {"author": author, "ts": "2026-10-17T00:00:00+0900",
            "body": f"review: request-changes\nTT#M-S@a1/x\n{text}"}


def _card_with_rejections(client, texts):
    issue = submitted(client)
    for t in texts:
        client.post(f"/issues/{issue['id']}/comments",
                    json={"author": "agy", "body": f"review: request-changes\n{issue['review_subject']}\n{t}"})
    return client.get(f"/issues/{issue['id']}").json()


# --- 분류 ---

def test_반려_사유는_버킷으로_분류된다():
    assert core._classify_rejection("마이그레이션 누락 — db 컬럼이 없다") == "migration"
    assert core._classify_rejection("테스트가 없다 — 회귀 커버 안 됨") == "test"
    assert core._classify_rejection("계약 문서 패리티 — api.md와 스킬 사본 불일치") == "contract"
    assert core._classify_rejection("보안: secret 하드코딩") == "security"
    assert core._classify_rejection("네이밍 컨벤션 문항") == "style"
    assert core._classify_rejection("이 접근 방식 자체가 근거 없다") == "general"


def test_2회_반복_유형은_구조화_체크_섹션이_나온다(client):
    cur = _card_with_rejections(client, ["마이그레이션 누락", "db 컬럼 마이그레이션 빠짐"])
    secs = core._correct_loop_sections(cur)
    assert any("구조화 체크" in s and "migration" in s for s in secs), secs


def test_한번_반려는_섹션_없다(client):
    cur = _card_with_rejections(client, ["마이그레이션 누락"])
    assert core._correct_loop_sections(cur) == []


def test_3회_반려는_premise_재검토를_강제한다(client):
    cur = _card_with_rejections(client, ["a 문제", "b 문제", "c 문제"])
    secs = core._correct_loop_sections(cur)
    assert any("premise 재검토" in s for s in secs), secs


def test_premise는_2회에서는_안_나온다(client):
    cur = _card_with_rejections(client, ["a 문제", "b 문제"])
    secs = core._correct_loop_sections(cur)
    assert not any("premise" in s for s in secs)


# --- 발령문 주입 ---

def test_tt_review_fix_발령문에_섹션이_들어가고_노트가_남는다(client, monkeypatch):
    issue = submitted(client)
    for t in ["마이그레이션 누락", "db 컬럼 빠짐", "접근이 근거 없다"]:
        client.post(f"/issues/{issue['id']}/comments",
                    json={"author": "agy", "body": f"review: request-changes\n{issue['review_subject']}\n{t}"})
    issue = client.get(f"/issues/{issue['id']}").json()
    # 리뷰어 점유 시뮬레이션(approve 헬퍼 대신 직접) — 판정 author와 동일하게
    client.post(f"/issues/{issue['id']}/claim-review", json={"agent": "agy"})
    issue = client.get(f"/issues/{issue['id']}").json()

    sent = {}
    notes = []

    def api(url, path, method="GET", body=None):
        if path.endswith("/dispatch") and method == "POST":
            sent["msg"] = body["message"]
            return {"status": "ok"}
        if path.endswith("/comments") and method == "POST":
            notes.append(body["body"])
            return {"id": 1}
        if path.endswith("/dispatches"):
            return []
        if path == f"/issues/{issue['id']}" and method == "PATCH":
            return client.patch(path, json=body).json()
        r = client.request(method, path, json=body)
        assert r.status_code < 400, r.text
        return r.json()

    monkeypatch.setattr(core, "api", api)
    act = {"issue": issue["id"], "subject": issue["review_subject"], "agent": "probe",
           "action": "tt-review-fix", "marker": "[tt-review-fix TT#M-S@a1/x]"}
    tt.execute(core, "http://tt", act)
    msg = sent.get("msg", "")
    assert "구조화 체크" in msg and "migration" in msg, msg[:400]
    assert "premise 재검토" in msg, msg[:400]
    assert any(n.startswith("[correct-loop premise") for n in notes), notes
    assert any(n.startswith("[correct-loop structure") for n in notes), notes


def test_노트는_재실행에_중복_게시되지_않는다(client, monkeypatch):
    issue = submitted(client)
    for t in ["a", "b", "c"]:
        client.post(f"/issues/{issue['id']}/comments",
                    json={"author": "agy", "body": f"review: request-changes\n{issue['review_subject']}\n{t}"})
    client.post(f"/issues/{issue['id']}/comments",
                json={"author": "probe", "body": "[correct-loop premise a1] 기록 있음"})
    cur = client.get(f"/issues/{issue['id']}").json()
    acts = core.decide(snap(cur))
    fix = [a for a in acts if a["action"] == "tt-review-fix"]
    assert not fix or not any("premise" in json.dumps(a) and a.get("note") for a in fix)
    # execute 레벨 dedup: marked()가 걸러낸다 — 섹션은 발령문에 유지되되 노트는 재게시 안 함
    secs = core._correct_loop_sections(cur)
    assert any("premise 재검토" in s for s in secs)
