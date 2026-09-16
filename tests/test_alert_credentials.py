"""Secret entry writes the running vault without a read endpoint or chat payload."""

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.alert_credentials import CredentialEntry


async def exchange(path, body):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps(body).encode() + b"\n")
    await writer.drain()
    reply = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return reply


async def test_local_entry_is_private_write_only_and_atomic(caplog):
    # AF_UNIX has a short platform path limit. Tests create and remove an owned
    # short temporary directory, never use the installation's socket or vault.
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        vault = SimpleNamespace(_fernet=object(), set=Mock())
        entry = CredentialEntry(root, vault, {"eu.posthog.com"})
        await entry.start()
        try:
            assert stat.S_IMODE(entry.path.stat().st_mode) == 0o600
            secret = "synthetic-read-key-with-no-permissions"
            reply = await exchange(entry.path, {"key": "secrets/sample", "value": secret, "host": "eu.posthog.com"})
            assert json.loads(reply) == {"ok": True}
            vault.set.assert_called_once()
            key, value = vault.set.call_args.args
            assert key == "secrets/sample"
            assert json.loads(value) == {"host": "eu.posthog.com", "value": secret}
            assert secret not in reply.decode() + caplog.text
            read = await exchange(entry.path, {"key": "secrets/sample"})
            assert json.loads(read)["ok"] is False
            assert secret not in read.decode()
        finally:
            await entry.stop()
        assert not entry.path.exists()


@pytest.mark.parametrize(
    "change", [{"host": "evil.invalid"}, {"key": "../override"}, {"value": "bad\nheader"}, {"extra": "read_all"}]
)
async def test_invalid_input_never_reaches_vault(change):
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        vault = SimpleNamespace(_fernet=object(), set=Mock())
        entry = CredentialEntry(root, vault, {"eu.posthog.com"})
        await entry.start()
        try:
            body = {"host": "eu.posthog.com", "key": "secrets/sample", "value": "synthetic-long-secret-value"}
            assert json.loads(await exchange(entry.path, body | change))["ok"] is False
            vault.set.assert_not_called()
        finally:
            await entry.stop()


async def test_entry_refuses_existing_regular_file(tmp_path):
    path = tmp_path / "credentials.sock"
    path.write_text("must remain")
    with pytest.raises(RuntimeError, match="owned socket"):
        await CredentialEntry(tmp_path, Mock()).start()
    assert path.read_text() == "must remain"


async def test_second_instance_does_not_unlink_active_entry():
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        first = CredentialEntry(root, Mock())
        await first.start()
        inode = Path(first.path).stat().st_ino
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await CredentialEntry(root, Mock()).start()
            assert first.path.stat().st_ino == inode
        finally:
            await first.stop()


async def test_restored_directory_is_private_before_socket_creation(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        os.chmod(root, 0o777)
        original = asyncio.start_unix_server

        async def create(*args, **kwargs):
            assert stat.S_IMODE(Path(root).stat().st_mode) == 0o700
            assert kwargs["start_serving"] is False
            return await original(*args, **kwargs)

        monkeypatch.setattr(asyncio, "start_unix_server", create)
        entry = CredentialEntry(root, Mock())
        await entry.start()
        try:
            assert entry.server.is_serving()
            assert stat.S_IMODE(entry.path.stat().st_mode) == 0o600
        finally:
            await entry.stop()


async def test_symlinked_directory_is_not_followed(tmp_path):
    target = tmp_path / "real"
    target.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    before = target.stat().st_mode
    with pytest.raises(OSError):
        await CredentialEntry(link, Mock()).start()
    assert target.stat().st_mode == before
    assert not (target / "credentials.sock").exists()


async def test_chmod_failure_closes_listener_and_preserves_original_error(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        entry = CredentialEntry(root, Mock())
        entry.handle = AsyncMock(side_effect=AssertionError("must not accept connections"))
        original = os.chmod
        failure = PermissionError("synthetic-mode-failure")

        def chmod(path, mode):
            if Path(path) == entry.path:
                raise failure
            return original(path, mode)

        monkeypatch.setattr(os, "chmod", chmod)
        with pytest.raises(PermissionError) as caught:
            await entry.start()
        assert caught.value is failure
        assert entry.server is None and entry.identity is None
        assert not entry.path.exists()
        entry.handle.assert_not_awaited()
        await entry.stop()  # Repeated cleanup cannot mask the startup failure.


async def test_vault_failure_logs_type_only(caplog):
    with tempfile.TemporaryDirectory(prefix="alert-", dir="/tmp") as root:
        secret = "synthetic-secret-that-must-not-appear"
        vault = SimpleNamespace(_fernet=object(), set=Mock(side_effect=RuntimeError(secret)))
        entry = CredentialEntry(root, vault, {"eu.posthog.com"})
        await entry.start()
        try:
            reply = await exchange(entry.path, {"key": "secrets/sample", "value": secret, "host": "eu.posthog.com"})
            assert json.loads(reply) == {"ok": False, "error": "Credential was not stored"}
            assert "Credential entry rejected (RuntimeError)" in caplog.text
            assert secret not in caplog.text + reply.decode()
            assert all(record.exc_info is None for record in caplog.records)
        finally:
            await entry.stop()
