"""Repository setup with actual local Git configs, never a remote transport."""

import subprocess
from types import SimpleNamespace

import pytest

from src.connectors.discord_alerts import DiscordAlerts
from src.core import alert_repositories as repos
from src.core.alert_channels import AlertError

REMOTE = "https://example.com/team/sample.git"


def repository(path, remote=REMOTE):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path.resolve()


@pytest.mark.parametrize("url", [
    REMOTE, "https://EXAMPLE.COM/TEAM/SAMPLE/", "git@example.com:team/sample.git",
    "ssh://git@example.com/team/sample.GIT/", "git://example.com/team/sample",
    "https://user:super-secret@example.com/team/sample.git/?token=ignored#fragment",
    "<https://example.com/team/sample.git>", "https://example.com/team/%73ample.git",
])
def test_remote_normalization(url):
    assert repos.remote_identity(url) == "example.com/team/sample"


@pytest.mark.parametrize("url", [
    "/srv/repos/sample", "../sample", "file:///srv/repos/sample", "ext::run-command",
    "https://example.com/", "https://example.com/team/../sample", "https://[bad/team/sample",
])
def test_invalid_or_local_remote_is_not_a_hosted_identity(url):
    assert repos.remote_identity(url) is None


def test_unique_remote_matches_any_remote_without_git_network_or_env_overrides(tmp_path, monkeypatch):
    clone = repository(tmp_path / "nested" / "renamed-clone", "git@EXAMPLE.COM:TEAM/SAMPLE.git")
    subprocess.run(["git", "-C", str(clone), "remote", "add", "backup", REMOTE], check=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://other.example/evil/wrong")
    original = subprocess.run
    calls = []

    def read_only(command, **kwargs):
        calls.append(command)
        assert command[3:] == ["config", "--local", "--no-includes", "--null", "--get-regexp", r"^remote\..*\.url$"]
        assert "GIT_DIR" not in kwargs["env"] and "GIT_CONFIG_COUNT" not in kwargs["env"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        return original(command, **kwargs)

    monkeypatch.setattr(repos.subprocess, "run", read_only)
    assert repos.resolve_repository(REMOTE, [tmp_path]) == clone
    assert len(calls) == 1


def test_no_match_names_searched_roots_without_echoing_credentials(tmp_path):
    repository(tmp_path / "different", "https://other.example/team/sample")
    before = set(tmp_path.iterdir())
    with pytest.raises(AlertError) as error:
        repos.resolve_repository("https://user:super-secret@example.com/team/sample.git", [tmp_path])
    assert "No clone of example.com/team/sample" in str(error.value)
    assert str(tmp_path) in str(error.value) and "super-secret" not in str(error.value)
    assert set(tmp_path.iterdir()) == before


def test_multiple_clones_refuse_and_list_paths(tmp_path):
    first = repository(tmp_path / "first")
    second = repository(tmp_path / "second", "git@example.com:team/sample")
    with pytest.raises(AlertError, match="Multiple local clones") as error:
        repos.resolve_repository(REMOTE, [tmp_path])
    assert str(first) in str(error.value) and str(second) in str(error.value)


def test_symlink_escape_is_not_read_and_local_path_still_refused(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = repository(tmp_path / "outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(repos, "_remotes", lambda *args: pytest.fail("outside clone must not be inspected"))
    with pytest.raises(AlertError, match="No clone"):
        repos.resolve_repository(REMOTE, [root])
    with pytest.raises(AlertError, match="inside a configured"):
        repos.resolve_repository(str(root / "escape"), [root])


def test_overlapping_roots_and_symlink_alias_do_not_create_false_ambiguity(tmp_path):
    clone = repository(tmp_path / "nested" / "clone")
    (tmp_path / "alias").symlink_to(clone, target_is_directory=True)
    assert repos.resolve_repository(REMOTE, [tmp_path, tmp_path / "nested", clone]) == clone


def test_nested_explicit_root_gets_its_own_depth_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(repos, "MAX_DEPTH", 2)
    nested = tmp_path / "one" / "two"
    clone = repository(nested / "three" / "four")
    assert repos.resolve_repository(REMOTE, [tmp_path, nested]) == clone


def test_depth_bound_and_dependency_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr(repos, "MAX_DEPTH", 1)
    clone = repository(tmp_path / "one" / "two")
    repository(tmp_path / "node_modules" / "ignored")
    with pytest.raises(AlertError, match="search depth 1"):
        repos.resolve_repository(REMOTE, [tmp_path])
    assert repos.resolve_repository(str(clone), [tmp_path]) == clone


def test_local_path_without_remotes(tmp_path):
    clone = repository(tmp_path / "sample", None)
    assert repos.resolve_repository(str(clone), [tmp_path]) == clone
    with pytest.raises(AlertError, match="No clone"):
        repos.resolve_repository(REMOTE, [tmp_path])
    with pytest.raises(AlertError, match="inside a configured"):
        repos.resolve_repository(str(tmp_path), [tmp_path])


def test_gitdir_file_reads_config_without_following_config_includes(tmp_path):
    main = repository(tmp_path / "main")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main / '.git'}\n")
    assert repos.resolve_repository(REMOTE, [worktree]) == worktree
    include = tmp_path / "included.config"
    include.write_text('[remote "included"]\nurl = https://other.example/team/repo\n')
    subprocess.run(["git", "-C", str(main), "config", "include.path", str(include)], check=True)
    with pytest.raises(AlertError, match="No clone"):
        repos.resolve_repository("https://other.example/team/repo", [main])


@pytest.mark.parametrize("limit", ["MAX_DIRECTORIES", "MAX_ENTRIES", "MAX_SECONDS"])
def test_incomplete_search_never_selects_a_partial_match(tmp_path, monkeypatch, limit):
    repository(tmp_path / "match")
    (tmp_path / "unread").mkdir()
    monkeypatch.setattr(repos, limit, 0)
    with pytest.raises(AlertError, match="reached its limit"):
        repos.resolve_repository(REMOTE, [tmp_path])


def test_unreadable_config_is_not_mistaken_for_no_remote(tmp_path):
    clone = repository(tmp_path / "broken")
    (clone / ".git" / "config").write_text('broken "super-secret" [ configuration')
    with pytest.raises(AlertError, match="Could not inspect") as error:
        repos.resolve_repository(REMOTE, [tmp_path])
    assert "super-secret" not in str(error.value)


def test_timeout_error_never_includes_remote_credentials(tmp_path, monkeypatch):
    clone = repository(tmp_path / "clone")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret-command", 2, output=b"super-secret")

    monkeypatch.setattr(repos.subprocess, "run", timeout)
    with pytest.raises(AlertError, match="Could not inspect") as error:
        repos.resolve_repository(REMOTE, [tmp_path])
    assert str(clone) in str(error.value) and "super-secret" not in str(error.value)


async def test_dm_repo_question_and_url_answer_store_only_local_path(tmp_path):
    clone = repository(tmp_path / "clone")
    connector = SimpleNamespace(vault=SimpleNamespace(get=lambda _: None), _agent_manager=None)
    alerts = DiscordAlerts(connector, {
        "repository_roots": [str(tmp_path)], "adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"},
    }, tmp_path / "state")
    try:
        source = alerts.store.begin("worker", "101", "one", "201")
        source = alerts.store.update(source["id"], config={"service": "posthog"})
        await alerts.answer(source, None, "sample \t\n")  # Existing trailing whitespace behavior.
        source = alerts.store.get(source["id"])
        assert source["config"]["app"] == "sample"
        prompt = alerts.question(source, None)
        assert "URL" in prompt and "local filesystem path" in prompt
        await alerts.answer(source, None, "https://user:super-secret@example.com/team/sample.git")
        assert alerts.store.get(source["id"])["config"]["repo"] == str(clone)
        dump = "\n".join(alerts.store.db.iterdump())
        assert "super-secret" not in dump and REMOTE not in dump
    finally:
        alerts.store.close()


def test_cache_git_sentinel_does_not_block_discovery(tmp_path):
    cache = tmp_path / "project" / ".uvcache" / "sdists-v9"
    cache.mkdir(parents=True)
    (cache / ".git").write_text("Cache sentinel, not a Git repository")
    clone = repository(tmp_path / "clone")
    assert repos.resolve_repository(REMOTE, [tmp_path]) == clone


async def test_cancel_during_repository_lookup_prevents_stale_update(tmp_path, monkeypatch):
    from src.connectors import discord_alerts

    clone = repository(tmp_path / "clone")
    connector = SimpleNamespace(vault=SimpleNamespace(get=lambda _: None), _agent_manager=None)
    alerts = DiscordAlerts(connector, {"repository_roots": [str(tmp_path)]}, tmp_path / "state")
    try:
        source = alerts.store.begin("worker", "101", "one", "201")
        source = alerts.store.update(source["id"], config={"service": "posthog", "app": "sample"})

        async def delayed_result(*args):
            alerts.store.disable(source["id"])
            return clone

        monkeypatch.setattr(discord_alerts.asyncio, "to_thread", delayed_result)
        with pytest.raises(AlertError, match="Setup changed"):
            await alerts.answer(source, None, REMOTE)
        saved = alerts.store.get(source["id"])
        assert saved["state"] == "disabled" and "repo" not in saved["config"]
    finally:
        alerts.store.close()
