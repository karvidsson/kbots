"""Controller file IO anchored to directory descriptors, never symlink traversal."""

import contextlib
import hashlib
import os
import stat
from pathlib import Path

from src.core.alert_channels import AlertError

ARTIFACTS = (
    ".alert-test",
    ".nuxt",
    ".output",
    "dist",
    "coverage",
    "node_modules/.cache",
    "node_modules/.vite",
    "node_modules/.vite-temp",
    "node_modules/.vue-global-types",
)


@contextlib.contextmanager
def directory(root, parts=(), *, create=False):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise AlertError("Invalid controller directory component")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError:
        raise AlertError("Controller path is unavailable or traverses a symlink") from None
    finally:
        os.close(fd)


def read_bytes(root, name, limit=100_000):
    parts = Path(name).parts
    with directory(root, parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise AlertError("Requested file is not a bounded, private regular file")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise AlertError("Requested file exceeds its size bound")
            return data
        finally:
            os.close(fd)


def write_bytes(root, name, data):
    parts = Path(name).parts
    with directory(root, parts[:-1], create=True) as parent:
        # No truncation until the opened descriptor itself has been checked.
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AlertError("Controller refuses a non-private regular file")
            os.ftruncate(fd, 0)
            with os.fdopen(os.dup(fd), "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)


def hash_file(root, name):
    """Hash existing assets without loading them into model context or RAM."""
    parts = Path(name).parts
    with directory(root, parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000_000:
                raise AlertError("Integrity file is not a bounded, private regular file")
            digest, total = hashlib.sha256(), 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                if total > info.st_size:
                    raise AlertError("Integrity file changed during hashing")
                digest.update(chunk)
            if total != info.st_size:
                raise AlertError("Integrity file changed during hashing")
            return digest.hexdigest()
        finally:
            os.close(fd)


def artifact_directories(root, expected=None):
    identities = {}
    for name in ARTIFACTS:
        with directory(root, Path(name).parts, create=expected is None) as fd:
            st = os.fstat(fd)
            identities[name] = (st.st_dev, st.st_ino)
    if expected is not None and identities != expected:
        raise AlertError("Sandbox artifact directory changed; repair stopped")
    return identities
