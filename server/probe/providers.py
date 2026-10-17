"""Optional hosting adapter. TT review does not depend on a provider response.

Adapters return normalized PR observations, not TT verdicts. Gitea/local work
can use TT alone (provider=none); a Gitea API adapter is outside Q614's scope.
"""
import os
import re


def collect_github(repos, query):
    out = []
    for repo in dict.fromkeys(repos):
            try:
                prs = query("pr", "list", "--repo", repo, "--state", "open",
                            "--json", "number,headRefName,headRefOid,baseRefName,title,isDraft,labels") or []
            except Exception:
                continue
            for p in prs:
                try:
                    checks = query("pr", "checks", p["number"], "--repo", repo, "--json", "name,state")
                    checks = list(checks) if checks is not None else []
                except Exception:
                    checks = None  # observation failure is not confirmed zero checks
                out.append({"number": p["number"], "repo": repo, "branch": p.get("headRefName", ""),
                            "title": p.get("title", ""), "head_sha": p.get("headRefOid", ""),
                            "base": p.get("baseRefName", ""),
                            "isDraft": bool(p.get("isDraft")),
                            "labels": [l.get("name", "") for l in p.get("labels") or []],
                            "checks": checks})
    return out


ADAPTERS = {"github": collect_github}


def collect(repos, query):
    provider = os.getenv("TT_PR_PROVIDER", "github").strip().lower()
    adapter = ADAPTERS.get(provider)
    if adapter is None:
        if provider not in ("none", "local", ""):
            print(f"[probe] PR provider {provider!r} unavailable; TT review remains active", flush=True)
        return []
    return adapter(repos, query)


def observe_linked(issues, open_prs, query):
    """Read explicit GitHub URLs absent from open PRs; failures stay UNKNOWN."""
    if os.getenv("TT_PR_PROVIDER", "github").strip().lower() != "github":
        return {}
    known = {(p.get("repo"), p.get("number")) for p in open_prs}
    observations, cache = {}, {}
    for issue in issues:
        if issue.get("state") != "review":
            continue
        text = (issue.get("body") or "") + "\n" + "\n".join(
            c.get("body") or "" for c in issue.get("comments") or [])
        links = re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)\b", text)
        for repo, number in dict.fromkeys(links):
            key = (repo, int(number))
            if key in known:
                continue
            if key not in cache:
                try:
                    p = query("pr", "view", number, "--repo", repo,
                              "--json", "state,mergedAt,headRefOid") or {}
                    state = "MERGED" if p.get("mergedAt") else p.get("state", "UNKNOWN")
                except Exception:
                    state = "UNKNOWN"
                cache[key] = {"repo": repo, "number": int(number), "state": state}
            observations.setdefault(issue["id"], []).append(cache[key])
    return observations


def inspect_delivery(spec, query):
    """Observe the exact selected delivery target; errors are never local mode."""
    unknown = {**spec, "state": "UNKNOWN", "checks": None}
    if spec.get("provider") != "github" or os.getenv("TT_PR_PROVIDER", "github") != "github":
        return unknown
    try:
        info = query("pr", "view", spec["number"], "--repo", spec["repo"], "--json",
                     "state,headRefOid,baseRefName,isDraft,mergedAt,mergeCommit") or {}
        checks = query("pr", "checks", spec["number"], "--repo", spec["repo"],
                       "--json", "name,state")
        return {**spec, "state": "MERGED" if info.get("mergedAt") else info.get("state", "UNKNOWN"),
                "head_sha": info.get("headRefOid"), "base": info.get("baseRefName"),
                "isDraft": info.get("isDraft", True), "checks": checks,
                "merge_commit": (info.get("mergeCommit") or {}).get("oid"),
                "merged_at": info.get("mergedAt")}
    except Exception:
        return unknown


def merge_delivery(spec, run, head=None):
    """Provider's atomic head guard closes the query→push race; no force/admin.

    head(SRJY): patch-id 재사용으로 rebase된 PR을 병할 때 CAS할 실제 head —
    기본은 보고의 head_sha다.
    """
    if spec.get("provider") != "github" or os.getenv("TT_PR_PROVIDER", "github") != "github":
        raise ValueError("selected PR provider cannot merge automatically")
    return run("pr", "merge", spec["number"], "--repo", spec["repo"],
               "--squash", "--delete-branch", "--match-head-commit",
               head or spec["head_sha"])


def patch_id_of(repo, base, sha, query):
    """SRJY — (repo, base, sha) 비교 diff의 git patch-id. 실패 시 None(보류 유지).

    GitHub compare API의 .diff 미디어 타입으로 임의 커밋의 diff를 얻는다 —
    rebase로 PR head가 움직여도 이전 커밋의 diff는 repo에 남아 있다.
    """
    import subprocess
    try:
        diff = query("api", f"repos/{repo}/compare/{base}...{sha}",
                     "-H", "Accept: application/vnd.github.diff")
        if not diff or not diff.strip():
            return None
        out = subprocess.run(["git", "patch-id", "--stable"], input=diff,
                             capture_output=True, text=True, timeout=30)
        pid = (out.stdout or "").strip().split(" ")[0]
        return pid or None
    except Exception:
        return None
