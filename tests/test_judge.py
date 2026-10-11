"""R3 판단기 인터페이스 (TT M4K87YWR-ZXBJ) — §5.2 단조성+§8.1 로그+fail-to-rules.

이 카드는 인터페이스+배선까지만: 실제 모델 호출 없음(미구성=규칙만, 기록 없음).
LLM 구현·그림자 런타임은 후속 카드.
"""
import json

import pytest

import judge
import dispatchd
import probe.core

NOW = "2026-10-09T10:00:00+0900"
OLD = "2026-10-09T09:00:00+0900"


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "version": 3,
            "assignee": "a1@t", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": [], "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-09T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


def _snap(issues, prs=(), agents=()):
    return {"auto": True, "now": NOW, "agents": list(agents),
            "issues": list(issues), "prs": list(prs)}


def _pr(number=9, sha="b" * 40, checks=None):
    return {"number": number, "repo": "plainOldCode/think-tank",
            "checks": checks if checks is not None else [{"state": "SUCCESS"}],
            "isDraft": False, "branch": "tt/M4ABCDEF-GH12-x", "title": "",
            "head_sha": sha}


def _pc(body, ts=OLD, author="probe", cid=1):
    return {"id": cid, "author": author, "body": body, "ts": ts}


class FakeJudge(judge.BaseJudge):
    name = "fake@v1"

    def __init__(self, out="rank", value="nearest: ci_failed", fail=0):
        self.out, self.value, self.fail = out, value, fail
        self.calls = []

    def propose(self, point, view):
        self.calls.append((point, view))
        if self.fail and len(self.calls) <= self.fail:
            raise RuntimeError("judge down")
        if self.out == "no_response":
            return None
        return judge.Proposal(self.out, self.value)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_REVIEW_GRACE_MIN", "0")
    monkeypatch.setenv("TT_REVIEW_AGENT", "fallback@t")
    monkeypatch.setenv("TT_JUDGE_LOG", str(tmp_path / "judge-log.jsonl"))
    monkeypatch.delenv("TT_SUPPRESS_N", raising=False)
    judge.set_judge(None)
    probe.core._C1_SEEN.clear()
    yield
    judge.set_judge(None)


# --- 타입 수준 단조성(§5.2 원칙 1) ---

def test_proposal은_4값만_허용한다():
    for ok in ("hold", "escalate", "annotate", "rank"):
        assert judge.Proposal(ok).output == ok
    for bad in ("allow", "merge", "deny"):
        with pytest.raises(ValueError):
            judge.Proposal(bad)


def test_미구성_판단기는_기록도_호출도_없다(tmp_path):
    p = judge.run_advisory("C1", {"card": "X", "input": {}}, "unknown",
                           log=judge.JudgeLog(str(tmp_path / "j.jsonl")))
    assert p is None
    assert not (tmp_path / "j.jsonl").exists()  # 개입 없음 — 기록 없음


# --- §8.1 로그 + fail-to-rules ---

def test_판단_기록은_스키마_필드를_갖는다(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    judge.set_judge(FakeJudge())
    judge.run_advisory("C1", {"card": "M1", "pr": None, "input": {"k": 1}}, "unknown",
                       log=judge.JudgeLog(jp))
    rec = json.loads(open(jp, encoding="utf-8").read())
    assert set(rec) == set(judge.LOG_SCHEMA)
    assert rec["point"] == "C1" and rec["mode"] == "shadow"
    assert rec["output"].startswith("rank:") and "rank: " in rec["output"]  # 제안값 보존(리뷰 R3)
    assert rec["judge"] == "fake@v1" and rec["fallback"] is False and rec["gate"] == "allow"


def test_판단기_오류는_규칙_기본과_fallback_count로_간다(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    judge.set_judge(FakeJudge(fail=2))  # 재시도 1회 포함 전부 실패
    p = judge.run_advisory("S1", {"card": "M1", "input": {}}, "ci-fix dispatch",
                           log=judge.JudgeLog(jp))
    assert p is None  # 규칙 기본
    recs = [json.loads(l) for l in open(jp, encoding="utf-8")]
    assert len(recs) == 1 and recs[0]["fallback"] is True
    assert recs[0]["output"] == "no_response"


def test_재시도는_1회_한정이다(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    j = FakeJudge("annotate", "요약", fail=1)  # 1회 실패 후 재시도 성공
    judge.set_judge(j)
    p = judge.run_advisory("S1", {"card": "M1", "input": {}}, "rule",
                           log=judge.JudgeLog(jp))
    assert p and p.output == "annotate"
    assert len(j.calls) == 2  # 본 시도+재시도 1회 — 그 이상 없음
    recs = [json.loads(l) for l in open(jp, encoding="utf-8")]
    assert len(recs) == 1 and recs[0]["fallback"] is False


def test_counts는_count_기반만_집계한다(tmp_path):
    jp = str(tmp_path / "j.jsonl")
    judge.set_judge(FakeJudge())
    judge.run_advisory("C1", {"card": "M1", "input": {"a": 1}}, "unknown",
                       log=judge.JudgeLog(jp))
    judge.set_judge(FakeJudge(fail=2))
    judge.run_advisory("S1", {"card": "M2", "input": {"a": 2}}, "rule",
                       log=judge.JudgeLog(jp))
    c = judge.JudgeLog.counts(jp)
    assert c["total"] == 2 and c["by_point"] == {"C1": 1, "S1": 1}
    assert c["fallback"] == 1 and c["no_response"] == 1


# --- 배선: S1 ci-fix 본문 첨부 / S2 evidence 노트 첨부 / C2 no_pr rank / C1 unknown ---

def _exec_s1(monkeypatch, j):
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        return {"comments": [], "version": 3, "execution_attempt": 1}

    monkeypatch.setattr(probe.core, "api", fake_api)
    if j:
        judge.set_judge(j)
    probe.core.execute("http://x", {"agent": "dispatchd", "issue": "M1", "action": "ci-fix",
                                    "pr": 9, "repo": "plainOldCode/think-tank",
                                    "branch": "tt/M1-x", "head_sha": "b" * 40,
                                    "checks_failed": ["tests"]})
    return calls


def test_S1_판단기_없으면_ci_fix_본문이_불변이다(monkeypatch):
    calls = _exec_s1(monkeypatch, None)
    (m, p, b) = [x for x in calls if x[1].endswith("/dispatch") and x[0] == "POST"][0]
    assert "advisory: judge" not in b["message"]


def test_S1_판단기_요약이_dispatch_본문에_첨부된다(monkeypatch):
    calls = _exec_s1(monkeypatch, FakeJudge("annotate", "tests 실패 — fixture 누락 추정"))
    (m, p, b) = [x for x in calls if x[1].endswith("/dispatch") and x[0] == "POST"][0]
    assert "판정 아님" in b["message"] and "fixture 누락" in b["message"]


def test_S2_판단기_요약이_재제출_노트에_첨부된다(monkeypatch):
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        return {"comments": [_pc("done 증거 없음 또는 실패/미확정 보고", author="tt-server")],
                "version": 3, "execution_attempt": 1}

    monkeypatch.setattr(probe.core, "api", fake_api)
    judge.set_judge(FakeJudge("annotate", "보고 파일 누락 — 3단계 스키마 필요"))
    probe.core.execute("http://x", {"agent": "probe", "issue": "M1", "action": "review-note",
                                    "marker": "[evidence-rejected a1]", "attempt": 1,
                                    "reason": "done 거부(증거 없음/실패)"})
    posts = [b for m, p, b in calls if p.endswith("/comments") and m == "POST"]
    # 마커 노트와 요약은 분리된 코멘트(리뷰 R2 — 요약이 제어마커로 재해석되지 않게)
    assert len(posts) == 2 and posts[0]["body"].startswith("[evidence-rejected a1]")
    assert posts[1]["body"].startswith("[judge-note ") and "스키마 필요" in posts[1]["body"]
    assert "스키마 필요" not in posts[0]["body"]


def test_C2_라벨과_repo_마커가_없을때만_판단기가_개입한다(monkeypatch):
    j = FakeJudge("rank", "docs — 제목이 문서형")
    judge.set_judge(j)
    # 둘 다 없음 → rank 제안(동작 불변 — review-note는 그대로)
    acts = dispatchd.decide(_snap([_i()], [], []))
    assert j.calls and j.calls[0][0] == "C2"
    assert [a["action"] for a in acts] == ["review-note"]
    # 저위험 라벨 있음 → 규칙만으로 분류 — 판단기 호출 없음
    j2 = FakeJudge()
    judge.set_judge(j2)
    dispatchd.decide(_snap([_i(labels=["docs"])], [], []))
    assert not j2.calls


def test_C1_미매칭_서버_실패_기록을_rank_제안한다(monkeypatch):
    j = FakeJudge("rank", "nearest: verify_422")
    judge.set_judge(j)
    i = _i(state="review", comments=[_pc("런너 세션 503 폭발 — 원인 미상 오류",
                                         author="tt-server")])
    dispatchd.decide(_snap([i], [], []))
    assert [pt for pt, _ in j.calls if pt == "C1"] == ["C1"]
    # 같은 입력 재사이클 — 재판정 없음(dedup)
    j.calls.clear()
    dispatchd.decide(_snap([i], [], []))
    assert not [pt for pt, _ in j.calls if pt == "C1"]


def test_C1_규칙표_행에_해당하는_기록은_건너뛴다(monkeypatch):
    j = FakeJudge()
    judge.set_judge(j)
    for body in ("done 증거 없음 또는 실패/미확정 보고", "보고 접수 — review에 정지"):
        i = _i(state="review", comments=[_pc(body, author="tt-server")])
        j.calls.clear()
        dispatchd.decide(_snap([i], [], []))
        assert not [pt for pt, _ in j.calls if pt == "C1"]


# --- 리뷰 1차 지적 보강 ---

def test_seen은_기록된_해시를_찾는다(tmp_path):
    # 리뷰 R1 — 구현이 문자열 인덱싱으로 TypeError를 내던 버그
    jp = str(tmp_path / "j.jsonl")
    lg = judge.JudgeLog(jp)
    judge.set_judge(FakeJudge())
    judge.run_advisory("C1", {"card": "M1", "input": {"x": 1}}, "unknown", log=lg)
    ih = judge.input_hash("C1", {"card": "M1", "input": {"x": 1}})
    assert lg.seen("M1", "C1", ih)
    assert not lg.seen("M2", "C1", ih)  # 다른 카드 — 스코프 밖(리뷰 R9)
    assert not lg.seen("M1", "C1", "no-such-hash")


def test_미구성_판단기와_기존_로그가_있어도_규칙_집행은_계속된다(tmp_path):
    # 리뷰 R1 후반 — seen() 접근 자체가 규칙 액션을 죽이면 안 된다
    judge.JudgeLog(str(tmp_path / "j.jsonl")).record(
        point="C1", judge="x", mode="shadow", output="rank: y", rule_output="r", gate="allow",
        card="M1", input_hash_="h")
    judge.set_judge(None)  # 미구성
    acts = dispatchd.decide(_snap([_i()], [], []))
    assert [a["action"] for a in acts] == ["review-note"]  # 기존 규칙 동작 보존


def test_로그_쓰기_실패가_규칙_집행을_취소하지_않는다(monkeypatch):
    # 리뷰 R5 — TT_JUDGE_LOG가 불가능한 경로여도 dispatch는 나가야 한다
    import judge as J
    calls = _exec_s1(monkeypatch, FakeJudge("annotate", "요약"))
    monkeypatch.setenv("TT_JUDGE_LOG", "/nonexistent-dir-xyz/j.jsonl")
    calls2 = _exec_s1(monkeypatch, FakeJudge("annotate", "요약"))
    assert [x for x in calls2 if x[1].endswith("/dispatch") and x[0] == "POST"]


def test_C2_네_조합_결정표(monkeypatch):
    # 리뷰 R6 — 둘 다 없음/충돌만 판단기 대상, 정확히 하나면 규칙만
    j = FakeJudge()
    judge.set_judge(j)
    combos = {"both": (["docs"], True), "label_only": (["docs"], False),
              "repo_only": ([], True), "none": ([], False)}
    for name, (labels, repo) in combos.items():
        j.calls.clear()
        body = "repo: plainOldCode/think-tank" if repo else ""
        dispatchd.decide(_snap([_i(labels=labels, body=body)], [], []))
        called = any(pt == "C2" for pt, _ in j.calls)
        assert called == (name in ("both", "none")), name


def test_C1은_카드의_최신_사건을_판정한다(monkeypatch):
    # 리뷰 R7 — id 오름차순 순회에서 오래된 것만 잡히던 버그
    j = FakeJudge("rank", "x")
    judge.set_judge(j)
    i = _i(state="review", comments=[
        _pc("오래된 오류 A", author="tt-server", cid=1),
        _pc("최신 오류 B", author="tt-server", cid=2)])
    dispatchd.decide(_snap([i], [], []))
    c1 = [v for pt, v in j.calls if pt == "C1"]
    assert c1 and "오류 B" in c1[0]["input"]["comment"]


def test_C1은_verify_422를_알려진_사유로_건너뛴다(monkeypatch):
    # 리뷰 R8 — 정식 규칙표 행(verify_422)의 기계적 사유는 unknown이 아니다
    j = FakeJudge()
    judge.set_judge(j)
    i = _i(state="review", comments=[_pc("verify 422 — completion_report 구조 오류",
                                         author="tt-server")])
    dispatchd.decide(_snap([i], [], []))
    assert not [pt for pt, _ in j.calls if pt == "C1"]


def test_C1_dedup은_카드별로_스코프된다(tmp_path):
    # 리뷰 R9 — 같은 스니펫이 다른 카드에 있으면 각각 판정된다
    j = FakeJudge("rank", "x")
    judge.set_judge(j)
    for cid in ("M1", "M2"):
        i = _i(id=cid, state="review",
               comments=[_pc("세션 503 폭발 오류", author="tt-server")])
        dispatchd.decide(_snap([i], [], []))
    assert len([pt for pt, _ in j.calls if pt == "C1"]) == 2


def test_S1_로그에_완전한_pr_식별값과_사이클이_기록된다(monkeypatch):
    # 리뷰 R4 — pr은 repo#n@sha8, cycle_id는 스냅샷에서 전달
    jp = judge.log_path()
    calls = []

    def fake_api(url, path, method="GET", body=None):
        calls.append((method, path, body))
        return {"comments": [], "version": 3, "execution_attempt": 1}

    monkeypatch.setattr(probe.core, "api", fake_api)
    judge.set_judge(FakeJudge("annotate", "요약"))
    probe.core.execute("http://x", {"agent": "dispatchd", "issue": "M1", "action": "ci-fix",
                                    "pr": 9, "repo": "plainOldCode/think-tank",
                                    "branch": "tt/M1-x", "head_sha": "b" * 40,
                                    "checks_failed": ["t"], "cycle_id": "cyc-1"})
    rec = json.loads(open(jp, encoding="utf-8").read())
    assert rec["pr"] == "plainOldCode/think-tank#9@bbbbbbbb" and rec["cycle_id"] == "cyc-1"
