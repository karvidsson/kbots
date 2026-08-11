"""GitHub tools — read and act on issues and pull requests via the REST API.

Fits a PR-based dev workflow: triage issues, read PRs and their diffs, comment,
open issues. Reads work unauthenticated on public repos (low rate limit); a
token lifts the limit and enables private repos + writes. Token from the vault
as secrets/github-token (a fine-grained or classic PAT). The default repo comes
from the KBOTS_GITHUB_REPO env var (owner/name) or is passed per call.

Write actions (gh_comment, gh_create_issue) are HITL-gated.
"""

import logging
import os

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

_API = "https://api.github.com"


def _token(ctx: ToolContext) -> str | None:
    if not ctx.vault:
        return None
    return (ctx.vault.get("secrets/github-token")
            or ctx.vault.get("github-token")
            or ctx.vault.get("GITHUB_TOKEN"))


def _resolve_repo(repo: str) -> str | None:
    repo = repo or os.environ.get("KBOTS_GITHUB_REPO", "")
    repo = repo.strip()
    if repo.count("/") != 1 or not all(repo.split("/")):
        return None
    return repo


async def _gh(ctx: ToolContext, method: str, path: str,
              json_body: dict | None = None, params: dict | None = None):
    """Call the GitHub API. Returns (status, data) or (None, error_string)."""
    import aiohttp
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "kbots"}
    token = _token(ctx)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(method, f"{_API}{path}", headers=headers,
                                 json=json_body, params=params,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                status = r.status
                try:
                    data = await r.json()
                except Exception:
                    data = {"message": (await r.text())[:200]}
    except Exception as e:
        return None, f"GitHub request failed: {e}"
    if status == 401:
        return None, "GitHub auth failed. Check secrets/github-token in the vault."
    if status == 403 and "rate limit" in str(data.get("message", "")).lower():
        return None, "GitHub rate limit hit. Add a token as secrets/github-token to raise it."
    if status == 404:
        return None, "Not found (repo/issue/PR missing, or token lacks access)."
    if status >= 400:
        return None, f"GitHub error {status}: {data.get('message', 'unknown')}"
    return status, data


def _need_repo(repo: str) -> str:
    return ("No repo given and KBOTS_GITHUB_REPO not set. "
            "Pass repo as 'owner/name'.")


@tool(name="gh_list_issues", description="List issues in a GitHub repo", category="github")
async def gh_list_issues(ctx: ToolContext, repo: str = "", state: str = "open",
                         labels: str = "", limit: int = 20) -> str:
    """List issues (newest first). state: open|closed|all. labels: comma-separated."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    params = {"state": state, "per_page": str(max(1, min(limit, 50)))}
    if labels:
        params["labels"] = labels
    status, data = await _gh(ctx, "GET", f"/repos/{r}/issues", params=params)
    if status is None:
        return data
    rows = [i for i in data if "pull_request" not in i]  # issues endpoint includes PRs
    if not rows:
        return f"No {state} issues in {r}."
    out = [f"**{state} issues in {r}** ({len(rows)}):"]
    for i in rows[:limit]:
        lbls = ",".join(lbl["name"] for lbl in i.get("labels", []))
        out.append(f"#{i['number']} {i['title']} — @{i['user']['login']}"
                   + (f" [{lbls}]" if lbls else ""))
    return "\n".join(out)


@tool(name="gh_get_issue", description="Get a GitHub issue or PR with comments", category="github")
async def gh_get_issue(ctx: ToolContext, number: int, repo: str = "") -> str:
    """Get an issue (or PR) by number, with its body and latest comments."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    status, issue = await _gh(ctx, "GET", f"/repos/{r}/issues/{number}")
    if status is None:
        return issue
    out = [f"**#{issue['number']} {issue['title']}** ({issue['state']}) — @{issue['user']['login']}",
           issue.get("body") or "(no description)"]
    _, comments = await _gh(ctx, "GET", f"/repos/{r}/issues/{number}/comments",
                            params={"per_page": "10"})
    if isinstance(comments, list) and comments:
        out.append(f"\n--- {len(comments)} comment(s) ---")
        for c in comments[-10:]:
            out.append(f"@{c['user']['login']}: {(c.get('body') or '').strip()[:500]}")
    return "\n".join(out)


@tool(name="gh_list_prs", description="List pull requests in a GitHub repo", category="github")
async def gh_list_prs(ctx: ToolContext, repo: str = "", state: str = "open", limit: int = 20) -> str:
    """List pull requests (newest first). state: open|closed|all."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    status, data = await _gh(ctx, "GET", f"/repos/{r}/pulls",
                             params={"state": state, "per_page": str(max(1, min(limit, 50)))})
    if status is None:
        return data
    if not data:
        return f"No {state} pull requests in {r}."
    out = [f"**{state} PRs in {r}** ({len(data)}):"]
    for p in data[:limit]:
        draft = " (draft)" if p.get("draft") else ""
        out.append(f"#{p['number']} {p['title']} — @{p['user']['login']} "
                   f"[{p['head']['ref']} → {p['base']['ref']}]{draft}")
    return "\n".join(out)


@tool(name="gh_get_pr", description="Get a GitHub pull request with its changed files", category="github")
async def gh_get_pr(ctx: ToolContext, number: int, repo: str = "") -> str:
    """Get a PR's metadata and changed-files summary (additions/deletions per file)."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    status, pr = await _gh(ctx, "GET", f"/repos/{r}/pulls/{number}")
    if status is None:
        return pr
    out = [f"**#{pr['number']} {pr['title']}** ({pr['state']}"
           + (", merged" if pr.get("merged") else "") + f") — @{pr['user']['login']}",
           f"{pr['head']['ref']} → {pr['base']['ref']} | "
           f"+{pr.get('additions', '?')} -{pr.get('deletions', '?')} "
           f"across {pr.get('changed_files', '?')} file(s)",
           (pr.get("body") or "(no description)")[:800]]
    _, files = await _gh(ctx, "GET", f"/repos/{r}/pulls/{number}/files",
                         params={"per_page": "50"})
    if isinstance(files, list) and files:
        out.append("\n--- files ---")
        for f in files[:50]:
            out.append(f"{f['status']:9} {f['filename']} (+{f['additions']} -{f['deletions']})")
    return "\n".join(out)


@tool(name="gh_comment", description="Comment on a GitHub issue or PR",
      category="github", hitl=True)
async def gh_comment(ctx: ToolContext, number: int, body: str, repo: str = "") -> str:
    """Post a comment on an issue or pull request (needs a token with write access)."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    if not _token(ctx):
        return "Commenting needs a token. Add a PAT as secrets/github-token to the vault."
    if not body.strip():
        return "Comment body is empty."
    status, data = await _gh(ctx, "POST", f"/repos/{r}/issues/{number}/comments",
                             json_body={"body": body})
    if status is None:
        return data
    return f"Commented on {r}#{number}: {data.get('html_url', 'ok')}"


@tool(name="gh_create_issue", description="Create a GitHub issue",
      category="github", hitl=True)
async def gh_create_issue(ctx: ToolContext, title: str, body: str = "",
                          labels: str = "", repo: str = "") -> str:
    """Open a new issue (needs a token with write access). labels: comma-separated."""
    r = _resolve_repo(repo)
    if not r:
        return _need_repo(repo)
    if not _token(ctx):
        return "Creating an issue needs a token. Add a PAT as secrets/github-token to the vault."
    if not title.strip():
        return "Issue title is required."
    payload: dict = {"title": title}
    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
    status, data = await _gh(ctx, "POST", f"/repos/{r}/issues", json_body=payload)
    if status is None:
        return data
    return f"Opened {r}#{data['number']}: {data.get('html_url')}"
