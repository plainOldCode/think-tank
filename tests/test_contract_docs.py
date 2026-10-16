"""PR#104 리뷰 P2-2 회귀 — 계약 블록은 server/static/api.md와 skills/tt/references/api.md가
항상 일치해야 한다. 스킬 사본은 번들 스냅숏이라 어긋나면 에이전트가 구 계약을 읽는다."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARKERS = ("tt-work-contract", "tt-work-contract-v2", "tt-work-contract-v2.1", "tt-work-contract-v2.2")


def _blocks(path):
    s = (ROOT / path).read_text()
    return {m: s.split(f"<!-- {m}:start -->", 1)[1].split(f"<!-- {m}:end -->", 1)[0].strip()
            for m in MARKERS}


def test_계약_블록은_서버와_스킬_사본이_일치한다():
    src, ref = _blocks("server/static/api.md"), _blocks("skills/tt/references/api.md")
    for m in MARKERS:
        assert src[m] == ref[m], f"{m} 블록 불일치 — skills/tt/references/api.md를 재동기화할 것"


def test_제출_동사는_submit이_정본이다():
    """done --report 안내 잔재 회귀 — live 문서에는 tt submit이 정본으로 있어야 한다."""
    for f in ("server/static/api.md", "skills/tt/SKILL.md"):
        s = (ROOT / f).read_text()
        assert "tt submit" in s, f"{f}: tt submit 문서화 없음"


def test_tt_submission_identity_is_delivered_in_every_contract():
    for text in _blocks("server/static/api.md").values():
        assert "review_subject" in text
        assert "PRs are optional" in text
        assert "after merge" not in text
