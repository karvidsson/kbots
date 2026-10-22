"""Project facts: derived from the repo every session, never from memory."""

import json
import subprocess
import time

import pytest

from src.core import project_facts
from src.core.project_facts import build_block, collect, probe, repos_for


def _repo(path, files=None, remote=""):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for rel, text in (files or {}).items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    # Probes read a cache in tmp; the tests seed it, so nothing goes online.
    monkeypatch.setattr(project_facts, "_cache_file", lambda: tmp_path / "probes.json")


def test_repo_dirs_resolve_to_repos_and_skip_broad_parents(tmp_path):
    single = _repo(tmp_path / "site")
    family = tmp_path / "family"
    for name in ("a", "b"):
        _repo(family / name)
    crowd = tmp_path / "crowd"
    for i in range(project_facts.MAX_REPOS + 1):
        _repo(crowd / f"r{i}")
    found = repos_for([str(single), str(family), str(crowd), str(tmp_path / "missing")])
    assert found == [single, family / "a", family / "b"]


def test_worker_without_a_deploy_workflow_says_push_does_not_ship(tmp_path):
    repo = _repo(tmp_path / "site", {
        "wrangler.jsonc": '{\n  // comment\n  "name": "site",\n  "vars": {"SITE_URL": "https://example.com"},\n'
                          '  "d1_databases": [{"binding": "DB"}],\n}\n',
    })
    facts = collect(repo)
    joined = "\n".join(facts["ships"])
    assert "Cloudflare Worker `site`" in joined and "d1_databases" in joined
    assert "pushing does NOT ship" in joined
    assert facts["hosts"] == ["example.com"]


def test_a_deploy_workflow_is_named_and_suppresses_the_manual_warning(tmp_path):
    repo = _repo(tmp_path / "site", {
        "wrangler.toml": 'name = "site"\nroute = "example.com/*"\n',
        ".github/workflows/ship.yml": "jobs:\n  go:\n    steps:\n      - run: npx wrangler deploy\n",
        ".github/workflows/lint.yml": "jobs:\n  go:\n    steps:\n      - run: npm test\n",
    })
    joined = "\n".join(collect(repo)["ships"])
    assert "CI deploys via .github/workflows/ship.yml" in joined and "lint.yml" not in joined
    assert "does NOT ship" not in joined


def test_fly_app_yields_its_host_and_an_empty_repo_says_ask(tmp_path):
    fly = collect(_repo(tmp_path / "game", {"fly.toml": "app = 'game'\n"}))
    assert any("Fly app `game`" in w for w in fly["ships"]) and "game.fly.dev" in fly["hosts"]
    bare = collect(_repo(tmp_path / "bare", {"index.html": "<p>hi</p>"}))
    assert bare["ships"] == ["no deploy config in the repo: ask the owner how it ships before assuming"]


def test_layout_lists_root_entries_without_noise(tmp_path):
    repo = _repo(tmp_path / "app", {"pages/index.vue": "", "node_modules/x/y.js": "", ".env": "SECRET=1"})
    root = collect(repo)["root"]
    assert "pages/" in root and "node_modules/" not in root and ".env" not in root


def test_a_credential_in_the_remote_url_is_never_echoed(tmp_path):
    repo = _repo(tmp_path / "site", remote="https://user:ghp_secretvalue@github.com/o/site.git")
    assert collect(repo)["remote"] == "https://github.com/o/site.git"


async def test_cached_probes_are_reused_without_a_request(tmp_path):
    (tmp_path / "probes.json").write_text(json.dumps(
        {"example.com": {"at": time.time(), "note": "200 server: cloudflare"}}))
    assert await probe(["example.com"]) == {"example.com": "200 server: cloudflare"}


async def test_block_renders_repo_and_cached_host(tmp_path):
    repo = _repo(tmp_path / "site", {"fly.toml": "app = 'game'\n"})
    (tmp_path / "probes.json").write_text(json.dumps(
        {"game.fly.dev": {"at": time.time(), "note": "200 server: Fly/abc"}}))
    block = await build_block([str(repo)])
    assert block.startswith("<project-facts>") and block.endswith("</project-facts>")
    assert "host game.fly.dev: 200 server: Fly/abc" in block
    assert await build_block([str(tmp_path / "nothing")]) is None


async def test_startup_context_survives_a_broken_facts_builder(monkeypatch, tmp_path):
    from src.core import startup_context

    async def boom(dirs):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(project_facts, "build_block", boom)
    ctx = await startup_context.build_startup_context("sample", repo_dirs=[str(tmp_path)])
    assert "<reply-contract>" in ctx and "<project-facts>" not in ctx
