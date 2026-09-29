import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "secret-scan.sh"

TOKEN = "ghp_" + "A" * 36


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "secret-scan.sh")
    return repo


def scan(repo, diff_ref=None):
    import os
    env = {**os.environ}
    if diff_ref:
        env["TT_SCAN_DIFF"] = diff_ref
    return subprocess.run(
        ["bash", "scripts/secret-scan.sh", "."], cwd=repo, capture_output=True,
        text=True, env=env,
    )


def commit_file(repo, name, lines):
    (repo / name).write_text("\n".join(lines) + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)


def test_history_hit_does_not_block_new_commits(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "legacy.md", [f"historical token: {TOKEN}"])
    commit_file(repo, "clean.md", ["clean new line"])

    assert scan(repo, "HEAD~1").returncode == 0
    assert scan(repo).returncode == 1


def test_new_diff_hit_still_blocks(tmp_path):
    repo = make_repo(tmp_path)
    commit_file(repo, "base.md", ["base line"])
    commit_file(repo, "leak.md", [f"added {TOKEN} here"])

    r = scan(repo, "HEAD~1")
    assert r.returncode == 1
    assert "leak.md" in r.stdout
