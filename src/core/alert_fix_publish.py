"""Fixed GitHub operations for tested alert branches, never merge or force push."""

import json
import os
import re
import subprocess

from src.core.alert_channels import AlertError
from src.core.alert_git import git


def github(endpoint, payload=None):
    args = ["gh", "api", "--hostname", "github.com", endpoint]
    data = None
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
        data = json.dumps(payload).encode()
    env = {k: v for k, v in os.environ.items() if k not in {"GH_HOST", "GH_REPO", "GH_DEBUG"}}
    try:
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=env)
        if result.returncode or len(result.stdout) > 2_000_000:
            raise ValueError()
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise AlertError("GitHub operation was not confirmed") from None


class FixPublisher:
    def find(self, tree, issue, issue_url):
        matches = []
        for page in range(1, 21):
            items = github(f"repos/{tree['identity']}/pulls?state=all&per_page=100&page={page}")
            if not isinstance(items, list):
                raise AlertError("Existing issue PRs could not be checked completely")
            for item in items:
                if issue_url in (item.get("body") or ""):
                    matches.append(self.project(tree, item))
            if len(items) < 100:
                break
        else:
            raise AlertError("PR history exceeds the deduplication bound; review required")
        if len(matches) > 1:
            raise AlertError("Multiple existing PRs mention this issue; review required")
        return matches[0] if matches else None

    @staticmethod
    def project(tree, item):
        number = item.get("number")
        if (
            not isinstance(number, int)
            or item.get("html_url") != f"https://github.com/{tree['identity']}/pull/{number}"
        ):
            raise AlertError("GitHub PR response belongs to an unexpected repository")
        return {
            "number": number,
            "url": item["html_url"],
            "state": item.get("state"),
            "draft": item.get("draft") is True,
            "merged": item.get("merged_at") is not None,
            "head": item.get("head", {}).get("sha"),
            "branch": item.get("head", {}).get("ref"),
        }

    def push(self, folder, tree, branch, commit):
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit) or not re.fullmatch(r"alert-fix/[a-f0-9-]+", branch):
            raise AlertError("Invalid fix branch publication identity")
        found = git(folder, "ls-remote", "--heads", tree["remote"], "refs/heads/" + branch).decode().strip()
        if found:
            if found.split()[0] != commit:
                raise AlertError("Fix branch already contains a different commit; no overwrite allowed")
            return
        git(folder, "push", tree["remote"], commit + ":refs/heads/" + branch)
        actual = git(folder, "ls-remote", "--heads", tree["remote"], "refs/heads/" + branch).decode().split()
        if not actual or actual[0] != commit:
            raise AlertError("Pushed repair commit was not confirmed")

    def create(self, tree, branch, issue, issue_url, report, proof):
        summary = "\n".join("- " + " ".join(c["command"]) + ": passed" for c in proof["checks"])
        body = (
            f"PostHog issue: {issue_url}\n\nCause: {report['cause']}\n\nFix: {report['fix']}\n\n"
            f"Regression: `{proof['regression']}` failed with an assertion on the fetched base "
            "and passed with this fix.\n\n"
            f"Repository gates:\n{summary}\n\nOpened automatically from alert {issue}\n"
        )
        if report.get("drill_status_unconfirmed") is True:
            body += "\nRequested explicitly with Fix it; drill status unconfirmed.\n"
        result = github(
            f"repos/{tree['identity']}/pulls",
            {
                "title": "fix: " + report["cause"],
                "head": branch,
                "base": tree["default_branch"],
                "body": body,
                "draft": False,
                "maintainer_can_modify": True,
            },
        )
        pr = self.project(tree, result)
        if pr["state"] != "open" or pr["draft"] or pr["merged"]:
            raise AlertError("Created PR was not confirmed ready for review")
        return pr
