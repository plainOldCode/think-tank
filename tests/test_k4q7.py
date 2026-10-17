import json

"""K4Q7(M53XP2ZA-K4Q7) — 보안: GET /agents의 webhook secret 원문 노출 차단.

2026-10-17 라이브 확인: GET /agents가 10개 에이전트의 secret 원문을 반환(7개가 동일값).
secret은 /progress 인증에 쓰이므로 tailnet 내 누구나 진행 보고 위조 가능. 응답에서
secret을 완전히 제거한다(마스킹 아님 — 소유자만 보유, 읽기 경로 없음).
"""
def _agents(client):
    return client.get("/agents").json()


def test_에이전트_목록에_secret이_없다(client):
    client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1",
                                 "secret": "super-secret-value"})
    for a in _agents(client):
        assert "secret" not in a, a.get("name")
        assert "super-secret-value" not in json.dumps(a)


def test_secret_변경_후에도_노출되지_않는다(client):
    client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1"})
    r = client.patch("/agents/worker", json={"secret": "rotated-value"})
    assert r.status_code == 200, r.text
    out = json.dumps(_agents(client))
    assert "rotated-value" not in out
    assert "secret" not in out


def test_진행_투영_인증은_새_secret을_받는다(client):
    """마스킹은 읽기 경로만 건드린다 — /progress 인증은 저장된 secret으로 계속 동작."""
    import json as _j
    client.post("/agents", json={"name": "worker", "base_url": "http://127.0.0.1:1",
                                 "secret": "old-secret", "receive_mode": "hook"})
    client.patch("/agents/worker", json={"secret": "new-secret"})
    iid = client.post("/issues", json={"title": "진행 인증", "type": "task",
                                       "acceptance": "완료 기준: 테스트 통과"}).json()["id"]
    client.post(f"/issues/{iid}/claim", json={"agent": "worker", "hours": 2})
    r = client.post(f"/issues/{iid}/dispatch", json={"agent": "worker", "message": "실행해"})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    r = client.post(f"/issues/{iid}/dispatches/{did}/progress",
                    json={"state": "running", "machine": "m", "session": "s"},
                    headers={"Authorization": "Bearer old-secret"})
    assert r.status_code == 403, r.text
    r = client.post(f"/issues/{iid}/dispatches/{did}/progress",
                    json={"state": "running", "machine": "m", "session": "s"},
                    headers={"Authorization": "Bearer new-secret"})
    assert r.status_code == 200, r.text


