"""Local-only repository selection for alert setup. Never fetch or clone a URL."""

import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from src.core.alert_channels import AlertError

MAX_DEPTH = 4
MAX_DIRECTORIES = 2000
MAX_ENTRIES = 20000
MAX_SECONDS = 15
SKIP = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build",
    ".next", ".nuxt", ".output", "coverage", ".turbo", ".yarn", ".tox",
    ".uvcache", ".uv_cache", ".ruff_cache", ".pytest_cache",
}


def extract_repository_input(text):
    """Accept one URL in ordinary prose, without making a network request."""
    # Leave a real local path intact, including spaces in directory names.
    if text.strip().startswith(("/", "~/", "./", "../")):
        return text.strip()
    matches = re.findall(r"(?:https?://|ssh://|git://|git@[A-Za-z0-9.-]+:)[^\s<>\"']+", text)
    if len(matches) > 1:
        raise AlertError("Send one repository URL so I can choose the right clone")
    return matches[0].rstrip(".,);]}") if matches else text.strip()


def remote_identity(value):
    """Compare host/namespace/repository, excluding transport and credentials."""
    value = value.strip().removeprefix("<").removesuffix(">")
    try:
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https", "ssh", "git"}:
                return None
            host, path = parsed.hostname, parsed.path
        else:
            match = re.fullmatch(r"(?:[^/@:\s]+@)?([A-Za-z0-9.-]+):([^\s]+)", value)
            if not match:
                return None
            host, path = match.groups()
        path = unquote(path).strip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if (
            not host
            or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or len(parts) < 2
            or any(p in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts)
        ):
            return None
        return host.lower() + "/" + "/".join(parts).lower()
    except ValueError:
        return None


def validate_repository(path, roots):
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if roots and any(resolved.is_relative_to(root) for root in roots) and (resolved / ".git").exists():
            return resolved
    except (OSError, RuntimeError, ValueError):
        pass
    raise AlertError("Choose a Git repository inside a configured alerts.repository_roots directory")


def _remotes(path, deadline):
    # Config reads do not invoke transports, hooks, credential helpers or URL
    # rewrite rules. Ignore inherited Git overrides and non-local includes.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            [
                "git", "-C", str(path), "config", "--local", "--no-includes",
                "--null", "--get-regexp", r"^remote\..*\.url$",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            timeout=max(0.01, min(2, deadline - time.monotonic())), check=False,
        )
        if result.returncode not in {0, 1} or len(result.stdout) > 65536:
            raise ValueError("Unreadable config")
        if result.returncode == 1:
            return set()  # A valid repository may have no configured remote.
        identities = set()
        for entry in result.stdout.decode("utf-8", errors="strict").split("\0"):
            if not entry:
                continue
            _, separator, value = entry.partition("\n")
            if not separator:
                raise ValueError("Malformed config output")
            identity = remote_identity(value)
            if identity:
                identities.add(identity)
        return identities
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # Do not expose raw config, stderr or credential-bearing remote URLs.
        raise AlertError(
            f"Could not inspect local Git remotes at {path}. Fix that clone or supply a local path."
        ) from None


def resolve_repository(value, configured_roots):
    try:
        roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in configured_roots))
    except (OSError, RuntimeError, ValueError):
        raise AlertError("Configured alerts.repository_roots could not be resolved") from None
    value = extract_repository_input(value)
    identity = remote_identity(value)
    if not identity:
        if "://" in value or re.match(r"[^/\s]+@[^/\s]+:", value):
            raise AlertError("Use a Git repository URL such as https://github.com/owner/repo.git, or a local path")
        return validate_repository(value, roots)
    if not roots:
        raise AlertError("No alerts.repository_roots are configured for local clone lookup")
    deadline = time.monotonic() + MAX_SECONDS
    pending = [(root, 0) for root in reversed(roots)]
    seen, matches = {}, set()
    entries = 0
    searched = ", ".join(str(root) for root in roots)
    limit_error = f"Local clone search under {searched} reached its limit. Supply a local path or narrow the roots."
    while pending:
        if len(seen) >= MAX_DIRECTORIES or time.monotonic() >= deadline:
            raise AlertError(limit_error)
        path, depth = pending.pop()
        try:
            path = path.resolve(strict=True)
            if seen.get(path, MAX_DEPTH + 1) <= depth or not any(path.is_relative_to(root) for root in roots):
                continue
            if path not in seen and (path / ".git").exists() and identity in _remotes(path, deadline):
                matches.add(validate_repository(path, roots))
            seen[path] = depth
            if depth >= MAX_DEPTH:
                continue
            with os.scandir(path) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_ENTRIES or time.monotonic() >= deadline:
                        raise AlertError(limit_error)
                    if child.name not in SKIP and child.is_dir():
                        pending.append((Path(child.path), depth + 1))
        except (OSError, RuntimeError):
            raise AlertError(f"Local clone search could not read {path}. Fix access or supply a local path.") from None
    if not matches:
        raise AlertError(
            f"No clone of {identity} found under {searched} (search depth {MAX_DEPTH}). "
            "Supply its local path; no clone was created."
        )
    if len(matches) > 1:
        candidates = sorted(str(path) for path in matches)
        display = "\n".join(candidates[:10])
        suffix = f"\n... and {len(candidates) - 10} more" if len(candidates) > 10 else ""
        raise AlertError(
            f"Multiple local clones of {identity} found. Reply with the intended local path:\n{display}{suffix}"
        )
    # Repeat containment and .git validation after the potentially lengthy scan.
    return validate_repository(matches.pop(), roots)
