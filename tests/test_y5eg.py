"""Y5EG(M53PBECP-Y5EG) — P8MN 후속: brief를 작업자에게 넘긴다.

P8MN은 brief 칸 4개(SCOPE/VERIFY/FORBIDDEN/TIMEBOX)를 만들었지만 작업자 전달 경로가
없었다. Y5EG는 ① dispatch 지시문에 [brief] 섹션 ② 러너 프롬프트 렌더 ③ CLI 입력을 연결한다.
"""
import pytest


@pytest.fixture()
def iid(client):
    return client.post("/issues", json={
        "title": "brief 전달 재현", "type": "task",
        "acceptance": "완료 기준: 테스트 통과",
        "brief_scope": "핵심 루프만", "brief_verify": "python -m pytest",
        "brief_forbidden": "스키마 변경", "brief_timebox": "2h",
    }).json()["id"]


def _reg(client):
    r = client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1"})
    assert r.status_code in (200, 201), r.text


def test_dispatch_메시지에_brief가_싣힌다(client, iid):
    _reg(client)
    client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    r = client.post(f"/issues/{iid}/dispatch",
                    json={"agent": "worker", "message": "실행해"})
    assert r.status_code == 201, r.text
    comments = client.get(f"/issues/{iid}").json()["comments"]
    body = next(c["body"] for c in comments if "[brief]" in (c["body"] or ""))
    assert "핵심 루프만" in body and "python -m pytest" in body
    assert "스키마 변경" in body and "2h" in body


def test_brief_빈_카드는_섹션이_없다(client):
    iid2 = client.post("/issues", json={"title": "빈 카드", "type": "task",
                                        "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    _reg(client)
    client.post(f"/issues/{iid2}/claim", json={"agent": "worker", "hours": 2})
    r = client.post(f"/issues/{iid2}/dispatch", json={"agent": "worker", "message": "실행해"})
    assert r.status_code == 201, r.text
    body = client.get(f"/issues/{iid2}").json()["comments"][-1]["body"]
    assert "[brief]" not in body


def test_brief_첨부는_재전송_멱등키를_깨지_않는다(client, iid):
    """idem은 원본 메시지 기준 — brief 첨부 후 동일 재전송은 기존 행 반환(DW0P와 같은 계약)."""
    _reg(client)
    client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    r1 = client.post(f"/issues/{iid}/dispatch", json={"agent": "worker", "message": "실행해"})
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/issues/{iid}/dispatch", json={"agent": "worker", "message": "실행해"})
    assert r2.status_code == 200, r2.text  # 멱등 재전송
    assert r2.json()["id"] == r1.json()["id"]


def test_러너_프롬프트에_brief가_렌더된다():
    import importlib.util as _ilu
    import os
    _here = os.path.dirname(os.path.abspath(__file__))
    spec = _ilu.spec_from_file_location("tt_runner_y5eg", os.path.join(_here, "..", "runner", "tt-runner.py"))
    R = _ilu.module_from_spec(spec)
    spec.loader.exec_module(R)
    ent = {"issue_id": "X", "message": "실행해",
           "work_contract": {"version": "v", "instructions": "i", "report_required": True},
           "execution_attempt": 1,
           "brief_scope": "핵심 루프만", "brief_verify": "pytest",
           "brief_forbidden": "", "brief_timebox": "2h"}
    p = R.task_prompt(ent)
    assert "[brief]" in p and "핵심 루프만" in p and "pytest" in p and "2h" in p
    # brief 없는 페이로드는 무영향
    p2 = R.task_prompt({"issue_id": "X", "message": "m",
                        "work_contract": {"version": "v", "instructions": "i"}})
    assert "[brief]" not in p2


def test_brief_block_순수_함수(client, iid):
    issue = client.get(f"/issues/{iid}").json()
    from service import brief_block
    b = brief_block(issue)
    assert b.startswith("[brief]") and "SCOPE" in b and "TIMEBOX" in b
    assert brief_block({"brief_scope": "", "brief_verify": "", "brief_forbidden": "",
                        "brief_timebox": ""}) == ""
