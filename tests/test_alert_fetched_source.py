import subprocess

import pytest

from src.core import alert_git
from src.core.alert_channels import AlertError
from src.core.alert_diagnosis import source_evidence


def run(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args]).decode().strip()


def commit(folder):
    run(folder, "add", ".")
    run(folder, "-c", "user.name=Fixture", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
    return run(folder, "rev-parse", "HEAD")


@pytest.fixture
def remote_repo(tmp_path, monkeypatch):
    upstream, shared = tmp_path / "upstream", tmp_path / "shared"
    upstream.mkdir()
    run(upstream, "init", "-q", "-b", "trunk")
    (upstream / "old.ts").write_text("export const old = true;\n")
    old = commit(upstream)
    subprocess.run(["git", "clone", "-q", str(upstream), str(shared)], check=True)
    run(shared, "remote", "set-url", "origin", "https://github.com/example/sample.git")
    (upstream / "utils").mkdir()
    (upstream / "utils/formatLabel.ts").write_text("export const formatLabel = value => value.trim();\n")
    head = commit(upstream)
    (shared / "old.ts").write_text("user's uncommitted edit\n")
    (shared / "private.txt").write_text("not source evidence")
    original = alert_git.git

    def offline(folder, *args, **kwargs):
        mapped = [str(upstream) if a == "https://github.com/example/sample.git" else a for a in args]
        return original(folder, *mapped, **kwargs)

    monkeypatch.setattr(alert_git, "git", offline)
    return {
        "source": {"config": {"repo": str(shared)}},
        "shared": shared,
        "upstream": upstream,
        "old": old,
        "head": head,
        "root": tmp_path,
    }


def issue(commit_id=None):
    return {
        "name": "Error",
        "sample": {
            "exceptions": [{"frames": [{"source": "utils/formatLabel.ts"}]}],
            "releases": [{"commit_id": commit_id}] if commit_id else [],
        },
    }


def test_fetched_object_resolves_source_missing_from_stale_dirty_clone(remote_repo):
    r = remote_repo
    before = run(r["shared"], "status", "--porcelain")
    tree = alert_git.AlertRepository(r["root"] / "data").fetch(r["source"], issue())
    evidence = source_evidence(tree["repo"], issue(), tree["revision"])
    assert tree["default_branch"] == "trunk" and evidence["revision"] == r["head"]
    assert [x["path"] for x in evidence["snippets"]] == ["utils/formatLabel.ts"]
    assert run(r["shared"], "rev-parse", "HEAD") == r["old"]
    assert run(r["shared"], "status", "--porcelain") == before
    assert not (r["shared"] / "utils/formatLabel.ts").exists()


def test_verified_release_commit_is_used_without_trusting_arbitrary_revision(remote_repo):
    r = remote_repo
    repo = alert_git.AlertRepository(r["root"] / "data")
    assert repo.fetch(r["source"], issue(r["old"][:12]))["revision"] == r["old"]
    assert repo.fetch(r["source"], issue("f" * 40))["revision"] == r["head"]
    assert repo.fetch(r["source"], issue("HEAD~1"))["revision"] == r["head"]


def test_source_reader_uses_blobs_and_rejects_tracked_symlink(remote_repo):
    r = remote_repo
    (r["upstream"] / "link.ts").symlink_to(r["shared"] / "private.txt")
    revision = commit(r["upstream"])
    (r["upstream"] / "utils/formatLabel.ts").write_text("dirty working copy")
    evidence = source_evidence(r["upstream"], issue(), revision)
    assert "value.trim" in evidence["snippets"][0]["source"]
    assert "link.ts" not in evidence["source_files"]


def test_failed_fetch_never_reuses_cached_revision(remote_repo, monkeypatch):
    r = remote_repo
    repo = alert_git.AlertRepository(r["root"] / "data")
    repo.fetch(r["source"])
    original = alert_git.git

    def fail(folder, *args, **kwargs):
        if "fetch" in args:
            raise AlertError("synthetic unavailable")
        return original(folder, *args, **kwargs)

    monkeypatch.setattr(alert_git, "git", fail)
    with pytest.raises(AlertError, match="unavailable"):
        repo.fetch(r["source"])


@pytest.mark.parametrize("failure", ["non_github", "outage"])
def test_diagnosis_falls_back_to_committed_head_without_changing_shared_clone(remote_repo, monkeypatch, failure):
    r = remote_repo
    if failure == "non_github":
        run(r["shared"], "remote", "set-url", "origin", "https://example.com/team/app.git")
    else:
        original = alert_git.git

        def unavailable(folder, *args, **kwargs):
            if "ls-remote" in args:
                raise AlertError("remote unavailable")
            return original(folder, *args, **kwargs)

        monkeypatch.setattr(alert_git, "git", unavailable)
    before = run(r["shared"], "status", "--porcelain")
    repo = alert_git.AlertRepository(r["root"] / "data")
    sample = {"name": "old", "sample": {"exceptions": [{"frames": [{"source": "old.ts"}]}]}}
    tree = repo.evidence_tree(r["source"], sample)
    evidence = source_evidence(tree["repo"], sample, tree["revision"])
    assert tree["stale"] and "may be stale" in tree["selection"]
    assert evidence["revision"] == r["old"]
    assert evidence["snippets"][0]["source"] == "export const old = true;\n"
    assert run(r["shared"], "status", "--porcelain") == before
    assert run(r["shared"], "rev-parse", "HEAD") == r["old"]
    with pytest.raises(AlertError):
        repo.fetch(r["source"], sample)  # Only diagnosis gets fallback.
