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
                        "--json", "number,headRefName,headRefOid,title,isDraft,labels") or []
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


def merge_delivery(spec, run):
    """Provider's atomic head guard closes the query→push race; no force/admin."""
    if spec.get("provider") != "github" or os.getenv("TT_PR_PROVIDER", "github") != "github":
        raise ValueError("selected PR provider cannot merge automatically")
    return run("pr", "merge", spec["number"], "--repo", spec["repo"],
               "--squash", "--delete-branch", "--match-head-commit", spec["head_sha"])
