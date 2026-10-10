import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stale-base-check.sh"


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit(repo, name, lines, msg):
    (repo / name).write_text("\n".join(lines) + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "stale-base-check.sh").write_text(SCRIPT.read_text())
    commit(repo, "f.txt", ["one"], "init")
    git(repo, "remote", "add", "origin", "https://example.com/x.git")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # 로컬 가짜 origin/main
    git(repo, "branch", "feature")
    return repo


def check(repo, args=(), env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["bash", "scripts/stale-base-check.sh", ".", *args],
                          cwd=repo, capture_output=True, text=True, env=env)


def test_최신_base는_통과(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "feature")
    assert check(repo).returncode == 0


def test_뒤처진_base는_차단(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "main.txt", ["m1"], "main 이동")  # origin/main 전진
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "checkout", "-q", "feature")  # feature는 그대로 — 뒤처짐
    p = check(repo)
    assert p.returncode == 1 and "rebase" in p.stderr and "origin/main" in p.stderr


def test_main에_우리_커밋_포함이면_통사(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "feature")
    commit(repo, "f.txt", ["f1"], "feature 작업")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "feature")  # main이 feature를 포함
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "feature")
    # feature는 origin/main 뒤가 아니라 포함 관계 — 통과
    assert check(repo).returncode == 0


def test_tt_push_는_경고만하고_통과(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "main.txt", ["m1"], "main 이동")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "checkout", "-q", "feature")
    p = check(repo, env_extra={"TT_STALE_WARN": "1"})
    assert p.returncode == 0 and "경고" in p.stderr


def test_base_ref_없으면_생략(tmp_path):
    repo = make_repo(tmp_path)
    p = check(repo, args=["refs/heads/없음"])
    assert p.returncode == 0 and "생략" in p.stderr


def test_캐시된_origin_main도_새로_고친다(tmp_path):
    """회귀(P75 R2): 로컬 origin/main이 뒤처져 있으면 갱신 후 판정한다."""
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "feature")
    commit(repo, "f.txt", ["f1"], "feature 작업")  # feature 전진
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")  # 아직 feature 이전
    git(repo, "checkout", "-q", "feature")
    p = check(repo)  # fetch 실패(가짜 URL) → 기존 ref 폴백: 여전히 최신으로 통과
    assert p.returncode == 0
    # 이제 가짜 원격을 구성해 fetch가 실제로 갱신하는 경우를 본다
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    git(repo, "remote", "remove", "origin")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "fetch", "-q", "origin")
    git(repo, "update-ref", "refs/remotes/origin/main",
        subprocess.run(["git", "rev-parse", "main"], cwd=repo, check=True,
                       capture_output=True).stdout.decode().strip())
    git(repo, "checkout", "-q", "main")
    commit(repo, "m.txt", ["m1"], "main 이동")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")  # 로컬 캐시를 뒤로
    git(repo, "checkout", "-q", "feature")  # feature는 main 이전 — 뒤처짐
    p = check(repo)
    assert p.returncode == 1 and "rebase" in p.stderr
