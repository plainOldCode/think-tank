"""U4 잔재 정리(M5BAF987-HJ83) — U4가 지운 테스트 파일이 여전히 검사하던 함수의 단위 테스트 복원.

- policy.is_high_risk — FHH8 LABEL_SEMANTICS 계약(옛 3f282b1 test_completion_via_merge에서)
- policy.review_eligible — 교차리뷰 관문 매트릭스(옛 8efd397^ test_cross_review #3c에서)
- probe.core._rejection_verdicts — 기계 author 제외(71NK 규칙, 현재 구현 기준 신설)
- probe.core.hydrate_reviews — 후보만 GET·실패 폴백(옛 7e943da test_review_gate F4에서)

입력·출력만 검사한다. 레거시 PR 경로 전용 케이스는 되살리지 않는다.
"""
import pytest

import policy
import probe.core
from verdicts import is_machine_author

NOW = "2026-10-20T10:00:00+0900"


def _i(**kw):
    base = {"id": "M4ABCDEF-GH12", "state": "review", "archived": 0, "version": 3,
            "assignee": "a1@t", "reviewer": "", "lease_by": "", "lease_expires": None,
            "labels": [], "dispatches": 1, "execution_attempt": 1,
            "work_contract": {"version": "v2"}, "comments": [],
            "updated_at": "2026-10-19T09:00:00+0900", "title": "M4ABCDEF-GH12"}
    base.update(kw)
    return base


# ---------- policy.is_high_risk (FHH8) ----------

def test_is_high_risk_라벨_계약():
    assert policy.is_high_risk({"labels": ["Risk:Security"]})   # 대소문자 무시
    assert policy.is_high_risk({"labels": ["risk:data-loss"]})  # prefix 어휘
    assert not policy.is_high_risk({"labels": ["security"]})    # 접두어 아님 — 미선언 무시
    assert not policy.is_high_risk({"labels": []})
    assert not policy.is_high_risk({"labels": ["docs", "self"]})  # 저위험·선언 전용은 비고위험
    assert policy.LOW_RISK_LABELS == policy.LABEL_SEMANTICS["low_risk"]["exact"]


def test_review_eligible_매트릭스():
    ok, why = policy.review_eligible(_i(), now=NOW, agent="a2@t")
    assert ok is True and why is None
    assert policy.review_eligible(_i(assignee="a2@t"), now=NOW, agent="a2@t")[1] == "own_work"
    held = dict(_i(), reviewer="r@t", lease_by="r@t",
                lease_expires="2026-10-20T11:00:00+0900")
    assert policy.review_eligible(held, now=NOW, agent="a2@t")[1] == "review_occupied"
    # 점유자 본인은 유지 가능(재개)
    assert policy.review_eligible(held, now=NOW, agent="r@t")[0] is True
    # 만료 lease는 점유 아님
    expired = dict(held, lease_expires="2026-10-19T00:00:00+0900")
    assert policy.review_eligible(expired, now=NOW, agent="a2@t")[0] is True
    assert policy.review_eligible(_i(work_contract=None), now=NOW,
                                  agent="a2@t")[1] == "no_contract"
    assert policy.review_eligible(_i(state="done"), now=NOW,
                                  agent="a2@t")[1] == "state:terminal"
    assert policy.review_eligible(_i(state="todo"), now=NOW,
                                  agent="a2@t")[1] == "state:todo"
    assert policy.review_eligible(_i(archived=1), now=NOW,
                                  agent="a2@t")[1] == "archived"


def test_review_eligible_계약없어도_저위험은_독립_리뷰_가능():
    # M4DEK6WC-MQYS — 저위험(문서·번역) 라벨 카드는 계약 없이도 수령 가능
    assert policy.review_eligible(_i(work_contract=None, labels=["docs"]),
                                  now=NOW, agent="a2@t")[0] is True
    # low_risk 주입값 — PR 라벨만으로 후보가 된 경우(decide에서 산출)
    assert policy.review_eligible(_i(work_contract=None), now=NOW, agent="a2@t",
                                  low_risk=True)[0] is True


# ---------- probe.core._rejection_verdicts (71NK) ----------

def _rc(cid, author, verdict="request-changes"):
    return {"id": cid, "author": author,
            "body": f"review: {verdict}\nTT#M4ABCDEF-GH12@a1/{'a' * 8}\n이유"}


def test_rejection_verdicts는_사람_반려만_남긴다():
    cur = {"comments": [_rc(1, "codex"), _rc(2, "claude", "approve"),
                        _rc(3, "codex", "comment")]}
    out = probe.core._rejection_verdicts(cur)
    assert [c["id"] for c in out] == [1]  # request-changes·사람 author만


def test_rejection_verdicts는_기계_author를_제외한다():
    for author in ("probe", "tt-server", "board", "tt-embedding-daemon", "runner@m2max"):
        cur = {"comments": [_rc(1, author)]}
        assert probe.core._rejection_verdicts(cur) == [], author
    # 사람 author와 기계 author 섞인 카드 — 사람 것만
    cur = {"comments": [_rc(1, "probe"), _rc(2, "codex")]}
    assert [c["id"] for c in probe.core._rejection_verdicts(cur)] == [2]
    assert is_machine_author("runner@other") is True
    assert is_machine_author("codex") is False


# ---------- probe.core.hydrate_reviews (t_c69b13f0 F4) ----------

def _pr(iid="M4ABCDEF-GH12"):
    # pr_card_id 조인: 브랜치 tt/<ID>-x 우선, 없으면 제목의 카드 ID
    return [{"number": 9, "repo": "plainOldCode/think-tank", "checks": [],
             "isDraft": False, "branch": f"tt/{iid}-x", "title": iid, "head_sha": "b" * 8}]


def _snap(issues, prs):
    return {"auto": True, "now": NOW, "agents": [], "issues": list(issues),
            "prs": list(prs)}


def test_hydrate_reviews는_후보카드_코멘트만_취득(monkeypatch):
    got = []

    def fake_api(url, path, method="GET", body=None):
        got.append(path)
        return {"comments": [{"author": "codex", "body": "review: approve\nPR#9@bbbbbbbb"}]}

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _i()
    del i["comments"]  # snapshot이 review 카드가 아니면 코멘트 미취득
    snap = _snap([i], _pr())
    probe.core.hydrate_reviews("u", snap)
    assert snap["issues"][0]["comments"], "PR에 묶인 후보 카드 코멘트 취득"
    assert "/issues/M4ABCDEF-GH12" in got


def test_hydrate_reviews는_비후보카드를_GET하지_않음(monkeypatch):
    got = []

    def fake_api(url, path, method="GET", body=None):
        got.append(path)
        return {"comments": []}

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _i(id="M4ABCDEF-OTHR")
    del i["comments"]
    snap = _snap([i], _pr())  # PR은 다른 카드에만 연결
    probe.core.hydrate_reviews("u", snap)
    assert not got, "비후보 카드는 GET하지 않음"
    assert "comments" not in snap["issues"][0]


def test_hydrate_reviews_api_실패시_빈코멘트_폴백(monkeypatch):
    # run_once는 hydrate를 무방비 호출 — 예외 누출 시 사이클 전체가 죽는다.
    def fake_api(url, path, method="GET", body=None):
        raise RuntimeError("tt unreachable")

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _i()
    del i["comments"]
    snap = _snap([i], _pr())
    probe.core.hydrate_reviews("u", snap)
    assert snap["issues"][0]["comments"] == []


def test_hydrate_reviews는_이미_코멘트가_있으면_다시_GET하지_않음(monkeypatch):
    # "코멘트가 아직 없는 것만" — decide 판정 입력을 덮어쓰지 않는다.
    got = []

    def fake_api(url, path, method="GET", body=None):
        got.append(path)
        return {"comments": []}

    monkeypatch.setattr(probe.core, "api", fake_api)
    i = _i(comments=[_rc(1, "codex")])
    snap = _snap([i], _pr())
    probe.core.hydrate_reviews("u", snap)
    assert not got
    assert snap["issues"][0]["comments"][0]["id"] == 1
