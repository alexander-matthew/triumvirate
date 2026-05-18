"""Thin wrappers around the `gh` CLI. All shell-out, no Python GitHub libs."""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any


class GhError(RuntimeError):
    pass


# Patterns in `gh` stderr that indicate a transient failure worth retrying.
# A single 401 blip killed the agentdeck loop overnight; backoff stops that.
_TRANSIENT = re.compile(
    r"\b(HTTP\s*(401|403|429|500|502|503|504)"
    r"|connection\s+reset"
    r"|temporarily\s+unavailable"
    r"|i/o\s+timeout"
    r"|tls\s+handshake)\b",
    re.I,
)

# Tunable for tests. Production wait sequence: 1s, 3s, 9s (~13s worst case).
_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 9.0)


def _is_transient(stderr: str) -> bool:
    return bool(_TRANSIENT.search(stderr or ""))


def _run(args: list[str], *, check: bool = True, input_: str | None = None) -> str:
    last_stderr = ""
    for attempt in range(len(_RETRY_DELAYS_S) + 1):
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            input=input_,
        )
        if proc.returncode == 0:
            return proc.stdout
        last_stderr = proc.stderr
        if not check:
            return proc.stdout
        if attempt < len(_RETRY_DELAYS_S) and _is_transient(last_stderr):
            time.sleep(_RETRY_DELAYS_S[attempt])
            continue
        break
    raise GhError(f"gh {' '.join(args)}: {last_stderr.strip()}")


def _run_json(args: list[str]) -> Any:
    out = _run(args)
    return json.loads(out) if out.strip() else None


# ---- queries ---------------------------------------------------------------


def list_issues(*, labels: list[str], state: str = "open", limit: int = 100) -> list[dict]:
    args = ["issue", "list", "--state", state, "--limit", str(limit),
            "--json", "number,title,body,labels,createdAt,updatedAt,author"]
    for lbl in labels:
        args += ["--label", lbl]
    return _run_json(args) or []


def list_prs(*, state: str = "open", limit: int = 50) -> list[dict]:
    return _run_json([
        "pr", "list", "--state", state, "--limit", str(limit),
        "--json", "number,title,body,headRefName,baseRefName,labels,"
                  "isDraft,mergeable,reviewDecision,statusCheckRollup,"
                  "createdAt,updatedAt,additions,deletions,changedFiles,author",
    ]) or []


def get_pr(number: int) -> dict:
    return _run_json([
        "pr", "view", str(number),
        "--json", "number,title,body,headRefName,baseRefName,labels,"
                  "isDraft,mergeable,reviewDecision,statusCheckRollup,"
                  "createdAt,updatedAt,additions,deletions,changedFiles,"
                  "files,commits,reviews,comments,author",
    ]) or {}


def get_issue(number: int) -> dict:
    return _run_json([
        "issue", "view", str(number),
        "--json", "number,title,body,labels,createdAt,updatedAt,author",
    ]) or {}


def pr_diff(number: int) -> str:
    return _run(["pr", "diff", str(number)])


def search(query: str) -> list[dict]:
    return _run_json([
        "search", "issues", query, "--limit", "20",
        "--json", "number,title,labels,repository",
    ]) or []


def marker_posts(pr: dict, marker: str = "##VERDICT:") -> list[dict]:
    """Reviews + timeline comments containing `marker`, oldest first, **trusted only**.

    Public-repo guardrail: posts not authored by a trusted user are dropped
    here. Without this, a stranger could leave a PR comment containing
    `##VERDICT: APPROVE` and the merge-gate would honor it as a real review.
    """
    from . import trust

    posts: list[dict] = []
    for r in (pr.get("reviews") or []):
        body = r.get("body") or ""
        if marker in body:
            posts.append({
                "source": "review", "body": body,
                "ts": r.get("submittedAt") or "",
                "author": (r.get("author") or {}).get("login", ""),
            })
    for c in (pr.get("comments") or []):
        body = c.get("body") or ""
        if marker in body:
            posts.append({
                "source": "comment", "body": body,
                "ts": c.get("createdAt") or "",
                "author": (c.get("author") or {}).get("login", ""),
            })
    posts = trust.filter_trusted_marker_posts(posts)
    posts.sort(key=lambda p: p["ts"])
    return posts


# ---- mutations -------------------------------------------------------------


def add_label(*, kind: str, number: int, label: str) -> None:
    """kind: 'issue' or 'pr'."""
    _run([kind, "edit", str(number), "--add-label", label])


def remove_label(*, kind: str, number: int, label: str) -> None:
    proc = subprocess.run(
        ["gh", kind, "edit", str(number), "--remove-label", label],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and "not found" not in proc.stderr.lower():
        raise GhError(f"gh {kind} edit --remove-label: {proc.stderr.strip()}")


def comment(*, kind: str, number: int, body: str) -> None:
    _run([kind, "comment", str(number), "--body", body])


def review(*, pr_number: int, verdict: str, body: str) -> None:
    """Post a review. Verdict ∈ {'approve','request-changes','comment'}.

    GitHub blocks reviewing your own PRs, so when the worker + reviewer share
    auth we fall back to `gh pr comment` and rely on the ##VERDICT: marker
    in the body for readers to find it. Promote the reviewer to a dedicated
    bot identity to recover formal-review semantics.
    """
    flag = {
        "approve": "--approve",
        "request-changes": "--request-changes",
        "comment": "--comment",
    }[verdict]
    proc = subprocess.run(
        ["gh", "pr", "review", str(pr_number), flag, "--body", body],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return
    err = proc.stderr.lower()
    if "your own pull request" in err or "cannot be reviewed" in err:
        comment(
            kind="pr", number=pr_number,
            body=f"_(reviewer agent — posted as comment because GitHub blocks self-review)_\n\n{body}",
        )
        return
    raise GhError(f"gh pr review: {proc.stderr.strip()}")


def list_pr_comments(pr_number: int) -> list[dict]:
    return _run_json([
        "pr", "view", str(pr_number),
        "--json", "comments",
    ]).get("comments", []) or []


def create_pr(*, head: str, base: str, title: str, body: str, labels: list[str]) -> int:
    args = ["pr", "create", "--head", head, "--base", base,
            "--title", title, "--body", body]
    for lbl in labels:
        args += ["--label", lbl]
    url = _run(args).strip()
    return int(url.rsplit("/", 1)[-1])


def merge_pr(*, number: int, method: str = "squash") -> None:
    _run(["pr", "merge", str(number), f"--{method}", "--delete-branch"])
