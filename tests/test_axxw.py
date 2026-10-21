"""M5C4S0FB-AXXW — report 모드 리뷰 지시문에 저장소 위치 표기.

실측(#666): 리뷰 발령 지시문에 위치가 없어 리뷰어가 $HOME 전체 find로 들어가
26분+ 무음 → STALL → TIMEOUT kill. 위치는 카드 첫 줄 repo 마커 + card_repo()
라벨 폴백이 이미 계약 — 지시문에 자동 포함이 수정이다.
"""
from probe.tt_lifecycle import _review_dispatch_message, _review_repo


class _FakeCore:
    @staticmethod
    def card_repo(issue):
        from probe.core import card_repo
        return card_repo(issue)

    @staticmethod
    def api(url, path, *a, **k):
        # 부모 카드 페치 흉내 — parent_id가 path에 있으면 본문 있는 카드 반환
        if path == "PARENT":
            return {"id": "PARENT", "title": "에픽", "body": "완료 기준: 리뷰\nrepo: owner/parent-repo"}
        return None


class _NoParentCore(_FakeCore):
    @staticmethod
    def api(url, path, *a, **k):
        return None


ACT = {"issue": "TT-1", "subject": "s", "retry": 0}


def _msg(**kw):
    return _review_dispatch_message(ACT, "rev", **kw)


def test_repo_마커_카드는_slug와_find_금지를_지시문에_넣는다():
    cur = {"id": "TT-1", "title": "t", "body": "본문\nrepo: owner/child-repo", "parent_id": None}
    repo = _review_repo(_FakeCore, "u", cur)
    assert repo == "owner/child-repo"
    m = _msg(repo=repo)
    assert "owner/child-repo" in m
    assert "$HOME 전체 find" in m
    assert "~/Work/" in m and "위치 불명" in m


def test_마커_없으면_부모_카드_slug를_쓴다():
    cur = {"id": "TT-2", "title": "t", "body": "본문만", "parent_id": "PARENT"}
    repo = _review_repo(_FakeCore, "u", cur)
    assert repo == "owner/parent-repo"
    assert "owner/parent-repo" in _msg(repo=repo)


def test_둘_다_없으면_미지정_문구로_예외_없다():
    cur = {"id": "TT-3", "title": "t", "body": "본문만", "parent_id": "PARENT"}
    repo = _review_repo(_NoParentCore, "u", cur)
    assert repo is None
    m = _msg(repo=repo)
    assert "저장소 미지정" in m
    assert "$HOME 전체 find" in m


def test_라벨_폴백도_마커_대신_쓴다():
    cur = {"id": "TT-4", "title": "t", "body": "", "labels": ["think-tank"], "parent_id": None}
    assert _review_repo(_FakeCore, "u", cur) == "plainOldCode/think-tank"


def test_pr_모드_회귀_head_sha_문구_유지():
    m = _msg(repo="owner/r")
    assert "delivery.mode=pr" in m and "head_sha" in m
    assert "구현 코드는 수정하지 않는다" in m
    # PR 리뷰에도 위치 표기는 무해하게 추가된다(접두 [auto review] 불변)
    assert m.startswith("[auto review] TT 카드 TT-1, 제출물 s, 재시도=0. ")
