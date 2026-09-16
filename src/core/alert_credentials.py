"""Local terminal entry into the running vault. No secret reads or chat route."""

import asyncio
import json
import logging
import os
import re
import socket
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


class CredentialEntry:
    def __init__(self, directory, vault, allowed_hosts=()):
        self.path = Path(directory) / "credentials.sock"
        self.vault = vault
        self.allowed_hosts = frozenset(allowed_hosts)
        self.server = None
        self.identity = None

    async def start(self):
        if self.server:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir leaves an existing directory's mode unchanged. Repair it before
        # binding the socket, and never follow a substituted directory symlink.
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(directory).st_uid != os.getuid():
                raise RuntimeError("Credential entry directory is not owned by this user")
            os.fchmod(directory, 0o700)
        finally:
            os.close(directory)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Credential entry path is not an owned socket")
            # Only remove a positively stale owned socket, never an active server.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()
            else:
                raise RuntimeError("Credential entry is already running")
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(
            self.handle, path=str(self.path), limit=8192, start_serving=False
        )
        try:
            info = self.path.lstat()
            self.identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
            await self.server.start_serving()
        except BaseException:
            # Preserve the startup failure even if cleanup encounters a second
            # problem. Cleanup must be safe before socket chmod has succeeded.
            try:
                await self.stop()
            except Exception as error:
                logger.warning("Credential entry startup cleanup failed (%s)", type(error).__name__)
            raise

    async def stop(self):
        server, identity = self.server, self.identity
        self.server = self.identity = None
        if server:
            server.close()
            try:
                await server.wait_closed()
            finally:
                try:
                    info = self.path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                        self.path.unlink()

    async def handle(self, reader, writer):
        result = {"ok": False, "error": "Credential was not stored"}
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            body = json.loads(line)
            key, value, host = body["key"], body["value"], body["host"]
            if (
                set(body) != {"key", "value", "host"}
                or not re.fullmatch(r"secrets/[A-Za-z0-9_-]{1,100}", key)
                or host not in self.allowed_hosts
                or not isinstance(value, str)
                or not 20 <= len(value) <= 2048
                or not value.isascii()
                or any(c.isspace() for c in value)
            ):
                raise ValueError("Invalid entry")
            if not getattr(self.vault, "_fernet", None):
                raise ValueError("A persistent encrypted vault is required")
            # A single encrypted value binds credential and host atomically.
            # Separate writes can pair an old key with a new host after a crash.
            self.vault.set(key, json.dumps({"host": host, "value": value}, separators=(",", ":")))
            result = {"ok": True}
        except Exception as error:
            # Only a type name: no exception message, traceback or payload.
            logger.warning("Credential entry rejected (%s)", type(error).__name__)
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
