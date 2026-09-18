"""Fresh object-tree evidence and isolated fix clones; never checkout a shared repo."""

import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path

from src.core.alert_channels import AlertError


def git(repo, *args, timeout=90, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "-c",
                "protocol.ext.allow=never",
                "-C",
                str(repo),
                *args,
            ],
            env=env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise AlertError("Repository command could not complete") from None
    if result.returncode:
        raise AlertError("Repository command failed; no fresh source revision was assumed")
    if len(result.stdout) > 4_000_000:
        raise AlertError("Repository response exceeds the bound")
    return result.stdout


def registered_remote(repo):
    value = git(repo, "config", "--local", "--no-includes", "--get", "remote.origin.url").decode().strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", value
    )
    if not match or any(x in {".", ".."} for x in match[1].split("/")):
        raise AlertError("Alerts require a registered GitHub origin without embedded credentials")
    return "https://github.com/" + match[1] + ".git", match[1]


class AlertRepository:
    locks = {}

    def __init__(self, directory):
        self.directory = Path(directory) / "repositories"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def fetch(self, source, issue=None):
        identity = registered_remote(source["config"]["repo"])[1]
        with self.locks.setdefault(identity.lower(), threading.Lock()):
            return self._fetch(source, issue)

    def evidence_tree(self, source, issue=None):
        try:
            return self.fetch(source, issue)
        except (AlertError, OSError, subprocess.SubprocessError):
            # Diagnosis remains available for local/non-GitHub registrations
            # and during remote failures. The fixer still calls fetch directly.
            folder = Path(source["config"]["repo"]).resolve(strict=True)
            revision = git(folder, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            return {
                "repo": str(folder),
                "revision": revision,
                "selection": "local HEAD snapshot; source may be stale because a fresh origin fetch was unavailable",
                "stale": True,
            }

    def _fetch(self, source, issue=None):
        remote, identity = registered_remote(source["config"]["repo"])
        folder = self.directory / hashlib.sha256(identity.lower().encode()).hexdigest()
        if not folder.exists():
            folder.mkdir(mode=0o700)
            git(folder, "init", "--bare", "--quiet")
        refs = git(folder, "ls-remote", "--symref", remote, "HEAD").decode()
        match = re.search(r"^ref: refs/heads/([^\s]+)\s+HEAD$", refs, re.MULTILINE)
        if not match:
            raise AlertError("Remote default branch could not be verified")
        branch = match[1]
        git(folder, "check-ref-format", "refs/heads/" + branch)
        git(
            folder,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            remote,
            "+refs/heads/" + branch + ":refs/heads/alert-default",
        )
        head = git(folder, "rev-parse", "refs/heads/alert-default^{commit}").decode().strip()
        revision, selection = head, "fresh origin default branch"
        commits = {
            x.get("commit_id") for x in (issue or {}).get("sample", {}).get("releases", []) if x.get("commit_id")
        }
        if len(commits) == 1:
            commit = commits.pop()
            if re.fullmatch(r"[a-fA-F0-9]{7,64}", commit):
                try:
                    candidate = git(folder, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
                    git(folder, "merge-base", "--is-ancestor", candidate, head)
                    revision, selection = candidate, "reported release commit verified in fetched default history"
                except AlertError:
                    selection += "; reported release could not be verified"
        return {
            "repo": str(folder),
            "revision": revision,
            "default_revision": head,
            "default_branch": branch,
            "remote": remote,
            "identity": identity,
            "selection": selection,
        }

    def checkout(self, tree, directory, branch):
        directory = Path(directory)
        if directory.exists():
            raise AlertError("Fix workspace already exists; refusing to reuse uncertain files")
        directory.mkdir(parents=True, mode=0o700)
        git(directory, "init", "--quiet")
        git(directory, "fetch", "--no-tags", str(Path(tree["repo"]).resolve()), tree["default_revision"])
        git(directory, "checkout", "--quiet", "-b", branch, "FETCH_HEAD")
        git(directory, "remote", "add", "origin", tree["remote"])
        return directory
