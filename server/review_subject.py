"""Provider-independent review identity and verdict parsing (Q614).

This identifies a submitted report, not the truth of its evidence. Reviewers
must inspect the referenced artifacts; submitting new artifacts requires a new
report even within the same execution attempt.
"""
import hashlib
import json
import re


def subject(issue):
    report = issue.get("completion_report")
    if not isinstance(report, dict) or not issue.get("execution_attempt"):
        return None
    keys = ("id", "execution_attempt", "submission_revision", "title", "body",
            "acceptance", "assignee", "work_contract", "completion_report")
    raw = json.dumps({k: issue.get(k) for k in keys}, sort_keys=True,
                     ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"TT#{issue['id']}@a{issue['execution_attempt']}/{digest}"


def verdict(issue):
    """Latest exact-subject verdict by the assigned/requested independent reviewer.

The trusted-network identity model matches claim-review; a random comment or
PR-only approval cannot authorize TT verification. Request markers bind a
reviewer to a subject even after their lease is released.
"""
    token = subject(issue)
    if not token:
        return None
    reviewers = {issue.get("reviewer")} - {None, "", issue.get("assignee")}
    result = None
    for c in issue.get("comments") or []:
        body = c.get("body") or ""
        request = re.fullmatch(r"\[tt-review " + re.escape(token) + r"\] reviewer=(\S+)",
                               body.splitlines()[0] if body else "")
        if c.get("author") in ("probe", "tt-server") and request:
            if request[1] != issue.get("assignee"):
                reviewers.add(request[1])
        lines = body.strip().splitlines()
        if len(lines) < 2 or lines[1].strip() != token or c.get("author") not in reviewers:
            continue
        m = re.fullmatch(r"review: (approve|request-changes)", lines[0].strip())
        if m:
            result = m[1]
    return result
