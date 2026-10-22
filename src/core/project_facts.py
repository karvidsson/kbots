"""Project facts — what a repo actually is, read from the repo at session start.

Agents kept acting on remembered facts about their own projects and getting
them wrong: grepping an `app/` directory a repo does not have and concluding a
feature did not exist, writing a dev host into public links after the domain
had moved, assuming a site deploys on push when every deploy was a manual
`wrangler` run. Each was answerable by looking. Memory goes stale silently;
this block is regenerated from the working tree every session, so it cannot.

Everything here is derived, never stored: layout, branch, how the repo ships,
and which hosts answer right now with which server. Live probes are cached
for a few hours so most sessions pay nothing for them.
"""

import asyncio
import json
import logging
import re
import subprocess
import time
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_REPOS = 6          # a broad parent like ~/dev is skipped, not summarised
MAX_ROOT_ENTRIES = 14
PROBE_TIMEOUT = 3.0
PROBE_TTL = 6 * 3600
_SKIP_DIRS = {"node_modules", "dist", "build", "out", "coverage", "venv", "__pycache__"}
_DEPLOY_WORDS = re.compile(r"\b(deploy|wrangler|flyctl|fly deploy|vercel|netlify|rsync|scp|ssh)\b", re.I)
_URL = re.compile(r"https://[a-z0-9.-]+\.[a-z]{2,}", re.I)
_SITE_URL = re.compile(r"(?:site_?url|siteUrl|url|homepage)\W{0,6}(https://[a-z0-9.-]+\.[a-z]{2,})", re.I)


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def repos_for(dirs: list[str] | None) -> list[Path]:
    """Git repos an agent works in: each dir that is one, or its child repos.

    A parent holding more than MAX_REPOS repos (a whole ~/dev) is skipped:
    a summary of everything there would crowd out the work in front of it.
    """
    found: list[Path] = []
    for raw in dirs or []:
        d = Path(raw).expanduser()
        if (d / ".git").exists():
            found.append(d)
            continue
        if not d.is_dir():
            continue
        children = sorted(c for c in d.iterdir() if (c / ".git").exists())
        if 0 < len(children) <= MAX_REPOS:
            found.extend(children)
    seen, unique = set(), []
    for r in found:
        key = r.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique[:MAX_REPOS]


def _jsonc(text: str) -> dict:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'(?m)(^|[^:"])//.*$', r"\1", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except Exception:
        return {}


def _deploys(repo: Path) -> tuple[list[str], list[str]]:
    """(how this repo ships, hostnames it names), from files in the repo."""
    ways: list[str] = []
    hosts: list[str] = []
    ci = []
    wf = repo / ".github" / "workflows"
    if wf.is_dir():
        for f in sorted(wf.glob("*.y*ml")):
            try:
                if _DEPLOY_WORDS.search(f.read_text(errors="ignore")):
                    ci.append(f.name)
            except OSError:
                pass
    if ci:
        ways.append("CI deploys via .github/workflows/" + ", ".join(ci))

    fly = repo / "fly.toml"
    if fly.exists():
        try:
            app = tomllib.loads(fly.read_text()).get("app", "")
        except Exception:
            app = ""
        ways.append(f"Fly app `{app or '?'}` (`fly deploy`)")
        if app:
            hosts.append(f"{app}.fly.dev")

    for name in ("wrangler.jsonc", "wrangler.json", "wrangler.toml"):
        w = repo / name
        if not w.exists():
            continue
        text = w.read_text(errors="ignore")
        cfg = tomllib.loads(text) if name.endswith(".toml") else _jsonc(text)
        bindings = [k for k in ("d1_databases", "r2_buckets", "kv_namespaces", "durable_objects") if cfg.get(k)]
        ways.append(f"Cloudflare Worker `{cfg.get('name', '?')}` ({name}"
                    + (f"; bindings: {', '.join(bindings)}" if bindings else "") + ")")
        routes = cfg.get("routes") or ([cfg["route"]] if cfg.get("route") else [])
        for r in routes:
            pattern = r.get("pattern") if isinstance(r, dict) else r
            if pattern:
                hosts.append(str(pattern).split("/")[0])
        for value in (cfg.get("vars") or {}).values():
            hosts.extend(m.split("//")[1] for m in _URL.findall(str(value)))
        if not ci:
            ways.append("no workflow deploys it: pushing does NOT ship; deploy with `npx wrangler deploy`")
        break

    for marker, label in (("vercel.json", "Vercel"), ("netlify.toml", "Netlify"), ("Dockerfile", "Dockerfile present")):
        if (repo / marker).exists():
            ways.append(label)
    scripts = sorted({p.relative_to(repo).as_posix()
                      for pattern in ("scripts/*deploy*", "deploy/*.py", "deploy/*.sh", "bin/*deploy*", "*deploy*.sh")
                      for p in repo.glob(pattern) if p.is_file()})
    if scripts:
        ways.append("deploy scripts: " + ", ".join(scripts[:4]))

    for rel in ("nuxt.config.ts", "nuxt.config.js", "next.config.js", "next.config.mjs", "package.json", "CNAME"):
        f = repo / rel
        if f.exists():
            text = f.read_text(errors="ignore")
            if rel == "CNAME":
                hosts.append(text.strip())
            for m in _SITE_URL.findall(text):
                hosts.append(m.split("//")[1])
    if not ways:
        ways.append("no deploy config in the repo: ask the owner how it ships before assuming")
    cleaned = []
    for h in hosts:
        h = h.strip().lower().rstrip("*").rstrip(".")
        if h and "localhost" not in h and h not in cleaned and "." in h:
            cleaned.append(h)
    return ways, cleaned[:4]


def collect(repo: Path) -> dict:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    sha = _git(repo, "rev-parse", "--short", "HEAD")
    dirty = len([line for line in _git(repo, "status", "--porcelain").splitlines() if line.strip()])
    remote = _git(repo, "remote", "get-url", "origin") or _git(repo, "remote", "get-url", "upstream")
    remote = re.sub(r"^https://[^@/]+@", "https://", remote)  # never echo a credential in a URL
    root = sorted(p.name + ("/" if p.is_dir() else "") for p in repo.iterdir()
                  if not p.name.startswith(".") and p.name not in _SKIP_DIRS)
    ways, hosts = _deploys(repo)
    return {"path": str(repo), "branch": branch, "sha": sha, "dirty": dirty,
            "remote": remote, "root": root, "ships": ways, "hosts": hosts}


def _cache_file() -> Path:
    from src.core.base import resolve_kbots_tmp
    return resolve_kbots_tmp() / "project-facts-probes.json"


async def probe(hosts: list[str]) -> dict[str, str]:
    """host -> "200 server: cloudflare" (or "unreachable"), cached PROBE_TTL."""
    try:
        cache = json.loads(_cache_file().read_text())
    except Exception:
        cache = {}
    now = time.time()
    todo = [h for h in hosts if now - cache.get(h, {}).get("at", 0) > PROBE_TTL]
    if todo:
        async def one(session, host):
            try:
                async with session.head(f"https://{host}/", allow_redirects=False) as r:
                    server = r.headers.get("server", "?")
                    where = r.headers.get("location", "")
                    note = f"{r.status} server: {server}" + (f" -> {where}" if where else "")
            except Exception:
                note = "unreachable"
            cache[host] = {"at": now, "note": note}

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await asyncio.gather(*(one(session, h) for h in todo))
            path = _cache_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache))
        except Exception as e:
            logger.debug(f"project-facts probe failed: {e}")
    return {h: cache.get(h, {}).get("note", "not probed") for h in hosts}


async def build_block(dirs: list[str] | None) -> str | None:
    repos = repos_for(dirs)
    if not repos:
        return None
    facts = [collect(r) for r in repos]
    live = await probe([h for f in facts for h in f["hosts"]])
    lines = ["<project-facts>",
             "Read from the working tree and live hosts this session, not from memory.",
             "Where this disagrees with a lesson or a recollection, this wins."]
    for f in facts:
        state = f"{f['branch']} @ {f['sha']}" + (f", {f['dirty']} uncommitted" if f["dirty"] else "")
        lines.append(f"- {f['path']} ({state})" + (f" remote {f['remote']}" if f["remote"] else ""))
        root = f["root"][:MAX_ROOT_ENTRIES]
        more = len(f["root"]) - len(root)
        lines.append("  root: " + " ".join(root) + (f" +{more} more" if more > 0 else ""))
        for way in f["ships"]:
            lines.append(f"  ships: {way}")
        for h in f["hosts"]:
            lines.append(f"  host {h}: {live.get(h, 'not probed')}")
    lines.append("</project-facts>")
    return "\n".join(lines)
