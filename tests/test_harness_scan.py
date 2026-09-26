"""Real filesystem/CLI scans, with no deployment data or network access."""

import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_scan", ROOT / "scripts/harness-scan.py")
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return path


@pytest.fixture
def install(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    engine, overlay, home = (base / n for n in ("engine", "overlay", "home"))
    for p in (engine, overlay, home):
        p.mkdir()
    settings = write(
        overlay / "agents/demo/.claude/settings.json",
        json.dumps(
            {
                "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(private/*)"]},
                "env": {"MODE": "safe"},
            }
        ),
    )
    mcp = write(
        overlay / "config/mcp.yaml",
        yaml.safe_dump(
            {
                "servers": {
                    "local": {
                        "transport": "stdio",
                        "command": "/usr/bin/python3",
                        "args": ["-m", "example"],
                        "cwd": "/opt/app",
                    },
                }
            }
        ),
    )
    config = write(overlay / "config/config.yaml", "defaults: {}\n")
    prompt = write(overlay / "agents/demo/AGENTS.md", "The key to this task is careful review.\n")
    vault = write(overlay / "config/secrets.enc", "DO NOT READ VAULT")
    keys = [write(home / ".config" / name, "DO NOT READ KEY") for name in ("kbots-vault-key", "kbots-backup-key")]
    archive = write(home / "kbots-backups/kbots-20000101.tar.gz.enc", "DO NOT READ BACKUP")
    archive.parent.chmod(0o700)
    skill = write(overlay / "skills/demo.yaml", "name: demo\nprompt: Work carefully\n")
    tool = write(overlay / "tools/demo.py", "raise RuntimeError('must never be imported')\n")
    monkeypatch.setattr(scan.Path, "home", classmethod(lambda cls: home))
    for name in ("KBOTS_MODULES", "KBOTS_BACKUP_DIR", "KBOTS_VAULT_KEY_FILE", "KBOTS_BACKUP_KEY_FILE", "KBOTS_OVERLAY"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(**locals(), baseline=overlay / "config/harness-baseline.json")


def invoke(i, *args):
    return scan.main(["--engine", str(i.engine), "--overlay", str(i.overlay), *args])


def accept(i, capsys):
    assert invoke(i, "--accept") == 0
    output = capsys.readouterr().out
    assert "ACCEPTING" in output and "accepted" in output
    return json.loads(i.baseline.read_text())


def findings(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("FINDING ")]


def test_first_inventory_requires_accept_then_second_scan_is_clean(install, capsys):
    i = install
    assert invoke(i) == 1
    output = capsys.readouterr().out
    assert "missing baseline" in output and "permissions/allow" in output and "--accept" in output
    assert not i.baseline.exists()
    data = accept(i, capsys)
    assert set(data) == {"version", "items"}
    assert all(scan.HEX.fullmatch(value) for value in data["items"].values())
    serialized = i.baseline.read_text()
    for value in ("Bash(git status)", "Read(private/*)", "DO NOT READ", "Work carefully", "/usr/bin/python3"):
        assert value not in serialized
    assert stat.S_IMODE(i.baseline.stat().st_mode) == 0o600
    assert invoke(i) == 0
    assert capsys.readouterr().out.startswith("HARNESS OK:")


@pytest.mark.parametrize(
    "change,location",
    [
        ("widen", "permissions/allow"),
        ("add_allow", "permissions/allow"),
        ("remove_deny", "permissions/deny"),
        ("env", "#env"),
        ("other", "#unknownFutureOption"),
        ("mcp_new", "#servers/second"),
        ("mcp_command", "#servers/local"),
        ("mcp_args", "#servers/local"),
        ("mcp_url", "#servers/local"),
        ("hook", "#hooks"),
        ("engine_hook", "#/defaults/hooks"),
        ("skill", "skills/new.yaml"),
        ("tool", "tools/demo.py"),
        ("mode", "config/secrets.enc"),
    ],
)
def test_each_single_change_produces_one_finding(install, capsys, change, location):
    i = install
    accept(i, capsys)
    settings = json.loads(i.settings.read_text())
    mcp = yaml.safe_load(i.mcp.read_text())
    if change == "widen":
        settings["permissions"]["allow"] = ["Bash(*)"]
    elif change == "add_allow":
        settings["permissions"]["allow"].append("Write(*)")
    elif change == "remove_deny":
        settings["permissions"]["deny"] = []
    elif change == "env":
        settings["env"]["MODE"] = "changed"
    elif change == "other":
        settings["unknownFutureOption"] = True
    elif change == "mcp_new":
        mcp["servers"]["second"] = {"command": "/bin/example", "args": []}
    elif change == "mcp_command":
        mcp["servers"]["local"]["command"] = "/bin/new-command"
    elif change == "mcp_args":
        mcp["servers"]["local"]["args"] = ["--changed"]
    elif change == "mcp_url":
        mcp["servers"]["local"]["url"] = "https://example.invalid/new"
    elif change == "hook":
        settings["hooks"] = {"PreToolUse": [{"command": "/bin/example"}]}
    elif change == "engine_hook":
        i.config.write_text("defaults:\n  hooks:\n    before: /bin/example\n")
    elif change == "skill":
        write(i.overlay / "skills/new.yaml", "name: new\nprompt: Review\n")
    elif change == "tool":
        i.tool.write_text("raise RuntimeError('changed, never execute')\n")
    elif change == "mode":
        i.vault.chmod(0o644)
    i.settings.write_text(json.dumps(settings))
    i.mcp.write_text(yaml.safe_dump(mcp))
    assert invoke(i) != 0
    rows = findings(capsys)
    assert len(rows) == 1 and location in rows[0]
    if change != "mode":
        accept(i, capsys)
        assert invoke(i) == 0


@pytest.mark.parametrize(
    "content",
    [
        "phc_" + "a9ZrT2pQw8vNx5Ds" * 2,
        "phx_" + "b9ZrT2pQw8vNx5Ds" * 2,
        "sk-" + "c9ZrT2pQw8vNx5Ds" * 2,
        "https://discord.com/api/webhooks/12345/" + "testToken" * 4,
        "-----BEGIN RSA PRIVATE KEY-----",
        "access_token = " + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL",
    ],
)
def test_credential_shapes_are_located_and_redacted_not_accepted(install, capsys, content):
    i = install
    accept(i, capsys)
    prior = i.baseline.read_bytes()
    i.prompt.write_text("Ordinary text\n" + content + "\n")
    assert invoke(i, "--accept") == 2
    output = capsys.readouterr().out
    assert "line 2" in output and "value withheld" in output and content not in output
    assert i.baseline.read_bytes() == prior and content not in prior.decode()


@pytest.mark.parametrize(
    "text",
    [
        "The key is to understand the token and secret requirements.",
        "Use phc_, phx_ or sk- prefixes in the examples, not real keys.",
        "api_key: <your-key-here>",
        "key = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "A key decision requires extraordinarilycarefullyconsideredjudgment.",
    ],
)
def test_ordinary_sentences_and_placeholders_do_not_trip(install, capsys, text):
    accept(install, capsys)
    install.prompt.write_text(text)
    assert invoke(install) == 0
    assert "FINDING" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "command,args,valid",
    [
        ("/usr/bin/python3", ["-m", "app"], True),
        ("python3", ["-m", "app"], False),
        ("npx", ["--yes", "@example/mcp@1.2.3"], True),
        ("/bin/npx", ["pkg@latest"], False),
        ("npx", ["pkg@^1.2.3"], False),
        ("npx", ["pkg"], False),
        ("uvx", ["example==1.2.3"], True),
        ("uvx", ["example>=1.2"], False),
        ("npx", ["-y", "--package=@example/mcp@1.2.3", "example-mcp"], True),
        ("npx", ["--package", "pkg@1.2.3", "app"], True),
        ("npx", ["-p", "pkg@1.2.3", "app"], True),
        ("npx", ["-p=pkg@1.2.3", "app"], True),
        ("/bin/npx", ["--package=pkg@1.2.3", "--yes", "app"], True),
        ("npx", ["--package=a@1.2.3", "-p", "b@2.3.4", "--package", "c@3.4.5", "app"], True),
        ("npx", ["--package=pkg@1.2.3-rc.1+build.2", "--", "app"], True),
        ("npx", ["--", "pkg@1.2.3", "--package=application-argument"], True),
        ("npx", ["--package=pkg@1.2.3", "app", "--package=application-argument"], True),
        ("npx", ["--package=pkg@latest", "app"], False),
        ("npx", ["--package", "pkg@next", "app"], False),
        ("npx", ["-p", "pkg@~1.2.3", "app"], False),
        ("npx", ["--package=pkg@1.x", "app"], False),
        ("npx", ["--package=git+https://example.invalid/pkg.git#v1.2.3", "app"], False),
        ("npx", ["--package=example/pkg#v1.2.3", "app"], False),
        ("npx", ["--package=a@1.2.3", "--package=b@latest", "app"], False),
        ("npx", ["-p", "a", "-p", "b@1.2.3", "app"], False),
        ("npx", ["--package=", "app"], False),
        ("npx", ["--package"], False),
        ("npx", ["--package", "--yes", "app"], False),
        ("npx", ["--package=pkg@1.2.3"], False),
        ("npx", ["--userconfig", "pkg@1.2.3", "app"], False),
        ("npx", ["--package=a@1.2.3", "--unknown=b@latest", "app"], False),
        ("uvx", ["--from", "example==1.2.3", "app"], True),
        ("uvx", ["--from=example==1.2.3", "app"], True),
        ("/bin/uvx", ["--with=extra==2.3.4", "--from=example==1.2.3", "app"], True),
        ("uvx", ["--from=example==1.2.3", "--with", "extra==2.3.4", "-w", "more==3.4.5", "app"], True),
        ("uvx", ["--with=extra==2.3.4", "example==1.2.3"], True),
        ("uvx", ["--with=extra==2.3.4", "example@1.2.3"], True),
        ("uvx", ["--from=example[feature]==1.2.3rc1", "--", "app", "--with=application-argument"], True),
        ("uvx", ["--from=example==1.2.3", "app", "--from=application-argument"], True),
        ("uvx", ["--from=example==1.2.3", "--with=extra", "app"], False),
        ("uvx", ["--with=extra==2.3.4", "--with=more>=3", "example==1.2.3"], False),
        ("uvx", ["--with=extra==2.3.4", "example"], False),
        ("uvx", ["--from=example@latest", "app"], False),
        ("uvx", ["--from=example>=1.2.3", "app"], False),
        ("uvx", ["--from=example==1.2.*", "app"], False),
        ("uvx", ["--from=git+https://example.invalid/pkg.git@v1.2.3", "app"], False),
        ("uvx", ["--from=example @ https://example.invalid/pkg.whl", "app"], False),
        ("uvx", ["--from=example==1.2.3", "--with=extra==latest", "app"], False),
        ("uvx", ["--from=example==1.2.3", "--with"], False),
        ("uvx", ["--from=", "app"], False),
        ("uvx", ["--from=example==1.2.3", "--from=other==2.3.4", "app"], False),
        ("uvx", ["--from=example==1.2.3", "--with-requirements", "deps.txt", "app"], False),
    ],
)
def test_mcp_command_policy(install, capsys, command, args, valid):
    i = install
    accept(i, capsys)
    prior = i.baseline.read_bytes()
    i.mcp.write_text(yaml.safe_dump({"servers": {"local": {"command": command, "args": args}}}))
    assert invoke(i, "--accept") == (0 if valid else 2)
    if valid:
        capsys.readouterr()
        assert invoke(i) == 0
        assert "FINDING" not in capsys.readouterr().out
    else:
        assert i.baseline.read_bytes() == prior
        rows = findings(capsys)
        assert len(rows) == 1 and "exactly pinned" in rows[0]


@pytest.mark.parametrize(
    "command,args",
    [
        ("npx", ["--package=example@1.2.3", "-p", "extra@2.3.4", "app"]),
        ("uvx", ["--from=example==1.2.3", "--with", "extra==2.3.4", "app"]),
    ],
)
def test_pinned_package_version_change_still_needs_review(install, capsys, command, args):
    i = install
    i.mcp.write_text(yaml.safe_dump({"servers": {"local": {"command": command, "args": args}}}))
    accept(i, capsys)
    prior = i.baseline.read_bytes()
    args[0] = args[0].replace("1.2.3", "1.2.4")
    i.mcp.write_text(yaml.safe_dump({"servers": {"local": {"command": command, "args": args}}}))
    assert invoke(i) == 1
    rows = findings(capsys)
    assert len(rows) == 1 and "CHANGED" in rows[0] and "mcp.yaml" in rows[0]
    assert i.baseline.read_bytes() == prior
    accept(i, capsys)
    assert invoke(i) == 0


def test_never_opens_secrets_or_calls_network_or_executes_tools(install, capsys, monkeypatch):
    i = install
    protected = {i.vault.name, i.archive.name, *(p.name for p in i.keys)}
    opened = []
    original = os.open

    def guard(path, flags, *args, **kwargs):
        opened.append(str(path))
        assert Path(path).name not in protected, "secret content opened"
        return original(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network/process call")

    monkeypatch.setattr(os, "open", guard)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    accept(i, capsys)
    assert invoke(i) == 0 and opened
    # A content-only rotation must not trigger a vault/key/archive read or drift.
    for p in [i.vault, i.archive, *i.keys]:
        p.write_text("ROTATED, STILL NEVER READ")
    assert invoke(i) == 0


@pytest.mark.parametrize("target", ["vault", "key", "archive"])
def test_secret_mode_faults_cannot_be_blessed(install, capsys, target):
    i = install
    accept(i, capsys)
    original = i.baseline.read_bytes()
    path = {"vault": i.vault, "key": i.keys[0], "archive": i.archive}[target]
    path.chmod(0o640)
    assert invoke(i, "--accept") == 2
    rows = findings(capsys)
    assert len(rows) == 1 and "requires 0600" in rows[0]
    assert i.baseline.read_bytes() == original


@pytest.mark.parametrize("uid", [0, 123456])
def test_secret_root_or_wrong_owner_is_a_finding(install, capsys, monkeypatch, uid):
    i = install
    accept(i, capsys)
    original = scan.metadata

    def changed(path):
        info = original(path)
        if path == i.vault:
            values = list(info)
            values[4] = uid
            return os.stat_result(values)
        return info

    monkeypatch.setattr(scan, "metadata", changed)
    assert invoke(i) == 2
    rows = findings(capsys)
    assert len(rows) == 1 and f"owner uid {uid}" in rows[0]


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "parent_link", "fifo"])
def test_prompt_cannot_be_redirected_into_a_secret(install, capsys, attack):
    i = install
    accept(i, capsys)
    i.prompt.unlink()
    if attack == "symlink":
        i.prompt.symlink_to(i.keys[0])
    elif attack == "hardlink":
        os.link(i.keys[0], i.prompt)
    elif attack == "fifo":
        os.mkfifo(i.prompt)
    else:
        moved = i.prompt.parent.with_name("moved")
        i.prompt.parent.rename(moved)
        i.prompt.parent.symlink_to(moved, target_is_directory=True)
    assert invoke(i, "--accept") == 2
    assert "DO NOT READ" not in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["duplicate_json", "duplicate_yaml", "broken_yaml", "recursive_yaml", "invalid_utf8"])
def test_invalid_configs_fail_without_exposing_parser_contents(install, capsys, bad):
    i = install
    accept(i, capsys)
    if bad == "duplicate_json":
        i.settings.write_text('{"env": {}, "env": {"SUPERSECRET": "hidden"}}')
    elif bad == "duplicate_yaml":
        i.mcp.write_text("servers: {}\nservers: {SUPERSECRET: hidden}\n")
    elif bad == "broken_yaml":
        i.mcp.write_text("servers: [SUPERSECRET: hidden\n")
    elif bad == "recursive_yaml":
        i.mcp.write_text("servers: &x {SUPERSECRET: *x}\n")
    else:
        i.mcp.write_bytes(b"\xffSUPERSECRET")
    assert invoke(i, "--accept") == 2
    assert "SUPERSECRET" not in capsys.readouterr().out


def test_removed_skill_and_changed_module_tool_are_not_missed(install, capsys):
    i = install
    module = i.base / "module"
    path = write(module / "tools/tool.py", "ONE = 1\n")
    assert invoke(i, "--module", str(module), "--accept") == 0
    capsys.readouterr()
    i.skill.unlink()
    path.write_text("ONE = 2\n")
    assert invoke(i, "--module", str(module)) == 1
    rows = findings(capsys)
    assert len(rows) == 2 and any("REMOVED" in row for row in rows)


def test_local_settings_codex_hooks_and_agent_skills_are_scanned(install, capsys):
    i = install
    accept(i, capsys)
    write(i.overlay / "agents/demo/.claude/settings.local.json", '{"permissions": {"allow": ["Bash(*)"]}}')
    write(i.overlay / "agents/demo/.codex/hooks.json", '{"hooks": {"Stop": [{"command": "x"}]}}')
    write(i.overlay / "agents/demo/skills/private.yaml", "name: private\n")
    assert invoke(i) == 1
    rows = findings(capsys)
    assert any("settings.local.json#permissions/allow" in row for row in rows)
    assert any("hooks.json#hooks" in row for row in rows)
    assert any("skills/private.yaml" in row for row in rows)


def test_baseline_write_is_atomic_and_cannot_follow_a_link(install, capsys, monkeypatch):
    i = install
    accept(i, capsys)
    original = i.baseline.read_bytes()
    write(i.overlay / "tools/new.py", "X = 1\n")

    def fail(*args, **kwargs):
        raise OSError("private parser data")

    monkeypatch.setattr(os, "replace", fail)
    assert invoke(i, "--accept") == 2
    assert "private parser data" not in capsys.readouterr().out
    assert i.baseline.read_bytes() == original
    assert not list(i.baseline.parent.glob(".harness-baseline-*.tmp"))
    i.baseline.unlink()
    i.baseline.symlink_to(i.keys[0])
    assert invoke(i, "--accept") == 2
    assert i.keys[0].read_text() == "DO NOT READ KEY"


def test_surface_changed_during_accept_is_not_written(install, capsys, monkeypatch):
    i = install
    original = scan.Scanner.scan
    count = 0

    def mutate(self):
        nonlocal count
        count += 1
        if count == 2:
            i.settings.write_text('{"permissions": {"allow": ["Bash(*)"]}}')
        return original(self)

    monkeypatch.setattr(scan.Scanner, "scan", mutate)
    assert invoke(i, "--accept") == 2
    assert not i.baseline.exists()


def test_no_overlay_cannot_create_a_core_baseline(install, capsys):
    assert scan.main(["--engine", str(install.engine)]) == 2
    assert not (install.engine / "config/harness-baseline.json").exists()


def test_overlay_cannot_alias_core_for_baseline_acceptance(install, capsys):
    i = install
    assert scan.main(["--engine", str(i.engine), "--overlay", str(i.engine), "--accept"]) == 2
    assert not (i.engine / "config/harness-baseline.json").exists()


def test_literal_keys_cannot_mask_permission_drift(install, capsys):
    i = install
    value = json.loads(i.settings.read_text())
    value["permissions/allow"] = ["Bash(git status)"]
    value["document"] = "unrelated"
    i.settings.write_text(json.dumps(value))
    accept(i, capsys)
    value["permissions"]["allow"] = ["Bash(*)"]
    i.settings.write_text(json.dumps(value))
    assert invoke(i) == 1
    rows = findings(capsys)
    assert len(rows) == 1 and "#permissions/allow" in rows[0]


def test_permission_order_and_config_whitespace_are_not_drift(install, capsys):
    i = install
    value = json.loads(i.settings.read_text())
    value["permissions"]["allow"].append("Read(src/*)")
    i.settings.write_text(json.dumps(value))
    accept(i, capsys)
    value["permissions"]["allow"].reverse()
    i.settings.write_text(json.dumps(value, indent=4, sort_keys=True))
    i.mcp.write_text("# harmless comment\n" + i.mcp.read_text())
    assert invoke(i) == 0


def test_key_and_backup_overrides_are_metadata_only(install, capsys, monkeypatch):
    i = install
    key = write(i.base / "external/private-key", "not for reading")
    archive = write(i.base / "external/backups/one.tar.gz.enc", "not for reading")
    archive.parent.chmod(0o700)
    monkeypatch.setenv("KBOTS_VAULT_KEY_FILE", str(key))
    monkeypatch.setenv("KBOTS_BACKUP_DIR", str(archive.parent))
    data = accept(i, capsys)
    assert str(key) in data["items"] and str(archive.parent) in data["items"]
    assert str(archive) not in data["items"]
    assert str(i.keys[0]) in data["items"]
    archive.chmod(0o644)
    assert invoke(i) == 2
    assert len(findings(capsys)) == 1


def test_real_cli_exit_status_and_accept_inventory(install):
    i = install
    # Only replace home discovery in the isolated subprocess. No real home/key
    # paths are visited and the caller's HOME is never changed.
    launch = (
        "import runpy,sys; from pathlib import Path; from unittest.mock import patch; "
        "home, script=sys.argv[1:3]; sys.argv=sys.argv[2:]; "
        "guard=patch.object(Path, 'home', return_value=Path(home)); guard.start(); "
        "runpy.run_path(script,run_name='__main__')"
    )
    argv = [
        sys.executable,
        "-c",
        launch,
        str(i.home),
        str(ROOT / "scripts/harness-scan.py"),
        "--engine",
        str(i.engine),
        "--overlay",
        str(i.overlay),
    ]
    first = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert first.returncode == 1 and "missing baseline" in first.stdout and not first.stderr
    accepted = subprocess.run([*argv, "--accept"], capture_output=True, text=True, timeout=10)
    assert accepted.returncode == 0
    assert accepted.stdout.index("FINDING") < accepted.stdout.index("ACCEPTING")
    final = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert final.returncode == 0 and final.stdout.startswith("HARNESS OK:")


def test_hex_credential_assignment_is_detected_but_a_bare_hash_is_not():
    token = "d8461ff732bec905a29601e498f7dcfc48db05a71fcb933acdf675a3e1ed14cb"
    assert scan.credential_lines(f"secret = {token}")
    assert scan.credential_lines(f"artifact sha256: {token}") == []


def test_daily_backups_and_retention_never_change_the_baseline(install, capsys):
    i = install
    data = accept(i, capsys)
    baseline_bytes = i.baseline.read_bytes()
    assert str(i.archive.parent) in data["items"]
    assert not any(key.startswith(str(i.archive.parent) + "/") for key in data["items"])
    archives = [i.archive]
    for day in range(2, 20):
        archives.append(write(i.archive.parent / f"kbots-200001{day:02d}.tar.gz.enc", "NEVER READ ARCHIVE"))
        if len(archives) > 14:
            archives.pop(0).unlink()
        assert invoke(i) == 0
        output = capsys.readouterr().out
        assert output.startswith("HARNESS OK:") and "FINDING" not in output
        assert i.baseline.read_bytes() == baseline_bytes
    for path in archives:
        path.unlink()
    assert invoke(i) == 0
    assert i.baseline.read_bytes() == baseline_bytes


def test_backup_rotation_does_not_hide_a_real_permission_change(install, capsys):
    i = install
    accept(i, capsys)
    write(i.archive.parent / "kbots-next-day.tar.gz.enc", "NEVER READ")
    i.archive.unlink()
    value = json.loads(i.settings.read_text())
    value["permissions"]["allow"] = ["Bash(*)"]
    i.settings.write_text(json.dumps(value))
    assert invoke(i) == 1
    rows = findings(capsys)
    assert len(rows) == 1 and "permissions/allow" in rows[0]


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("fault", ["mode", "root_owner", "other_owner", "symlink", "fifo", "directory"])
def test_new_and_old_unsafe_archives_are_errors_not_inventory(install, capsys, monkeypatch, new, fault):
    i = install
    accept(i, capsys)
    baseline_bytes = i.baseline.read_bytes()
    path = write(i.archive.parent / "kbots-next-day.tar.gz.enc", "UNREAD") if new else i.archive
    if fault == "mode":
        path.chmod(0o644)
    elif fault in {"root_owner", "other_owner"}:
        original = scan.metadata

        def changed(file):
            info = original(file)
            if file == path:
                values = list(info)
                values[4] = 0 if fault == "root_owner" else 123456
                return os.stat_result(values)
            return info

        monkeypatch.setattr(scan, "metadata", changed)
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(i.keys[0])
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif fault == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    for args in [(), ("--accept",)]:
        assert invoke(i, *args) == 2
        rows = findings(capsys)
        assert len(rows) == 1 and str(path) in rows[0] and "UNSAFE" in rows[0]
        assert "ADDED" not in rows[0] and "CHANGED" not in rows[0]
        assert i.baseline.read_bytes() == baseline_bytes


@pytest.mark.parametrize("fault", ["mode", "root_owner", "symlink", "regular_file"])
def test_backup_directory_is_reviewed_and_must_be_private(install, capsys, monkeypatch, fault):
    i = install
    accept(i, capsys)
    path = i.archive.parent
    if fault == "mode":
        path.chmod(0o755)
    elif fault == "root_owner":
        original = scan.metadata

        def changed(file):
            info = original(file)
            if file == path:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(scan, "metadata", changed)
    else:
        moved = path.with_name("retained-backups")
        path.rename(moved)
        if fault == "symlink":
            path.symlink_to(moved, target_is_directory=True)
        else:
            write(path, "not a directory")
    assert invoke(i, "--accept") == 2
    rows = findings(capsys)
    assert len(rows) == 1 and str(path) in rows[0]


@pytest.mark.parametrize("stat_call", [1, 2])
def test_concurrent_retention_pruning_does_not_create_an_error(install, capsys, monkeypatch, stat_call):
    i = install
    accept(i, capsys)
    original = scan.metadata
    seen = 0

    def pruned(path):
        nonlocal seen
        if path == i.archive:
            seen += 1
            if seen == stat_call:
                path.unlink()
        return original(path)

    monkeypatch.setattr(scan, "metadata", pruned)
    assert invoke(i) == 0
    assert "FINDING" not in capsys.readouterr().out


def test_archive_traversal_checks_nested_and_generated_looking_paths_without_reads(install, capsys, monkeypatch):
    i = install
    accept(i, capsys)
    nested = write(i.archive.parent / ".git/nested/new.tar.gz.enc", "NEVER OPEN")
    original = os.open

    def guard(path, flags, *args, **kwargs):
        assert Path(path).name != nested.name
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guard)
    assert invoke(i) == 0
    capsys.readouterr()
    nested.chmod(0o644)
    assert invoke(i) == 2
    assert len(findings(capsys)) == 1


def test_runtime_logs_data_tmp_and_caches_do_not_grow_the_inventory(install, capsys, monkeypatch):
    i = install
    data = accept(i, capsys)
    baseline_bytes = i.baseline.read_bytes()
    generated = []
    for layer in (i.engine, i.overlay):
        for folder in (
            "logs",
            "data",
            "tmp",
            "agents/demo/logs",
            "agents/demo/data",
            "agents/demo/tmp",
            "agents/demo/.claude/projects",
            "agents/demo/.codex/sessions",
            "tools/__pycache__",
            "skills/.pytest_cache",
        ):
            generated.append(write(layer / folder / "runtime.log", "ordinary runtime state"))
    generated.append(write(i.overlay / "tools/cached.pyc", "bytecode"))
    original = scan.read_file

    def guard(path, *args, **kwargs):
        assert path not in generated, "runtime content was read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(scan, "read_file", guard)
    assert invoke(i) == 0
    assert "FINDING" not in capsys.readouterr().out
    assert i.baseline.read_bytes() == baseline_bytes
    current, errors = scan.Scanner(i.engine, i.overlay, [], i.home).scan()
    assert current == data["items"] and not errors
    for path in generated:
        path.write_text("grown\n" * 100)
    assert invoke(i) == 0
    for path in generated:
        path.unlink()
    assert invoke(i) == 0


def test_vault_atomic_write_temp_is_validated_without_inventory_churn(install, capsys, monkeypatch):
    i = install
    data = accept(i, capsys)
    pending = write(i.overlay / "config/secrets.enc.tmp", "NEVER READ TEMP VAULT")
    original = os.open

    def guard(path, flags, *args, **kwargs):
        assert Path(path).name != pending.name
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guard)
    assert invoke(i) == 0
    capsys.readouterr()
    pending.chmod(0o644)
    assert invoke(i, "--accept") == 2
    rows = findings(capsys)
    assert len(rows) == 1 and "secrets.enc.tmp" in rows[0] and "UNSAFE" in rows[0]
    pending.chmod(0o600)
    os.replace(pending, i.vault)
    assert invoke(i) == 0
    assert json.loads(i.baseline.read_text()) == data


@pytest.mark.parametrize("exitcode", [0, 1, 2])
def test_real_deploy_shell_orders_gate_and_never_restarts_on_rejection(tmp_path, exitcode):
    """Execute the deploy shell with command doubles, not string-only assertions."""
    root = tmp_path / "install"
    script = write(root / "scripts/self-deploy.sh", (ROOT / "scripts/self-deploy.sh").read_text(), 0o700)
    write(root / "scripts/sync.sh", '#!/bin/sh\necho sync >> "$TEST_EVENTS"\n', 0o700)
    bin_dir = tmp_path / "bin"
    events = tmp_path / "events"
    write(
        bin_dir / "git",
        """#!/bin/sh
case "$*" in
  'rev-parse HEAD')
    if [ -f "$TEST_PULLED" ]; then echo new; else echo old; fi ;;
  'pull --ff-only --no-rebase') touch "$TEST_PULLED"; echo pull >> "$TEST_EVENTS" ;;
  'reset --hard old') echo rollback >> "$TEST_EVENTS" ;;
  *) echo ref ;;
esac
""",
        0o700,
    )
    write(
        bin_dir / "uv",
        """#!/bin/sh
case "$*" in
  *'ruff check'*) echo lint >> "$TEST_EVENTS" ;;
  *'pytest -q'*) echo tests >> "$TEST_EVENTS" ;;
  *'harness-scan.py'*) echo scan >> "$TEST_EVENTS"; exit "$TEST_SCAN_EXIT" ;;
esac
exit 0
""",
        0o700,
    )
    write(bin_dir / "uname", "#!/bin/sh\necho Darwin\n", 0o700)
    write(
        bin_dir / "launchctl",
        '#!/bin/sh\necho restart >> "$TEST_EVENTS"\nprintf "running — ready\\n" >> "$TEST_LOG"\n',
        0o700,
    )
    # Prevent host system commands even if the test runs under uid 0 in CI.
    write(bin_dir / "id", "#!/bin/sh\necho 1234\n", 0o700)
    write(bin_dir / "sudo", "#!/bin/sh\nexit 99\n", 0o700)
    log = tmp_path / "service.log"
    overlay = tmp_path / "overlay"
    (overlay / "data").mkdir(parents=True)
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "KBOTS_OVERLAY": str(overlay),
        "KBOTS_LOG": str(log),
        "TEST_LOG": str(log),
        "TEST_EVENTS": str(events),
        "TEST_PULLED": str(tmp_path / "pulled"),
        "TEST_SCAN_EXIT": str(exitcode),
    }
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=10)
    calls = events.read_text().splitlines()
    assert calls.index("lint") < calls.index("tests") < calls.index("scan")
    if exitcode:
        assert result.returncode == 1 and "--accept" in result.stdout
        assert "rollback" in calls and "restart" not in calls
    else:
        assert result.returncode == 0 and calls.index("scan") < calls.index("restart")
