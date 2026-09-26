#!/usr/bin/env python3
"""Offline deployment surface review. Never import or execute scanned code."""

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from collections import Counter
from pathlib import Path

import yaml

VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
IGNORED = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}
ARCHIVE_SUFFIXES = (".enc", ".tar", ".tar.gz", ".tgz", ".zip", ".tar.bz2", ".tar.xz")
HEX = re.compile(r"[0-9a-f]{64}\Z")
SHAPES = (
    ("provider credential", re.compile(r"\b(?:phc_|phx_|sk-)[A-Za-z0-9_-]{16,}")),
    (
        "Discord webhook",
        re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9_.-]{16,}"),
    ),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
)
TOKEN = re.compile(r"(?i)\b(?:[a-z0-9_]*(?:key|token|secret))\b[\"']?\s*(?::|=|\bis\b)\s*[\"']?([A-Za-z0-9_+/=-]{32,})")


class ScanError(Exception):
    """Deliberately contains no source contents or parser exception text."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def pointer(key):
    # Keep literal slash/tilde keys distinct from nested permission/hook keys.
    return key.replace("~", "~0").replace("/", "~1")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ScanError("duplicate or non-string configuration key")
        result[key] = value
    return result


class StrictLoader(yaml.SafeLoader):
    pass


def yaml_mapping(loader, node):
    return unique([(loader.construct_object(k), loader.construct_object(v)) for k, v in node.value])


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


@contextlib.contextmanager
def directory(path):
    """Open every directory component without following links, including parents."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def metadata(path):
    with directory(path.parent) as fd:
        return os.stat(path.name, dir_fd=fd, follow_symlinks=False)


def read_file(path, protected=()):
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) in protected:
                raise ScanError("not an independent regular file; contents not read")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                content = stream.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise ScanError("file exceeds scan size limit")
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ScanError("file changed during scan")
            return content
        finally:
            os.close(fd)


def credential_lines(text):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        labels = {label for label, pattern in SHAPES if pattern.search(line)}
        for match in TOKEN.finditer(line):
            token = match[1]
            # Require a credential assignment and entropy, not just the word key.
            entropy = -sum((n / len(token)) * math.log2(n / len(token)) for n in Counter(token).values())
            if entropy >= 3.5 and re.search(r"[A-Za-z]", token) and re.search(r"[0-9]", token):
                labels.add("high-entropy credential assignment")
        if labels:
            findings.append((number, ", ".join(sorted(labels))))
    return findings


def pinned_package(spec, launcher, *, positional=False):
    if launcher == "npx":
        pattern = r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+@\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?"
    else:
        name = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
        extras = rf"(?:\[{name}(?:,{name})*\])?"
        # Exact PEP 440 release, pre/post/dev and local versions, no wildcard,
        # comparison list, marker, URL or arbitrary equality (===).
        version = (
            r"v?(?:\d+!)?\d+(?:\.\d+)*"
            r"(?:[-_.]?(?:alpha|beta|preview|pre|a|b|c|rc)[-_.]?\d*)?"
            r"(?:-\d+|[-_.]?(?:post|rev|r)[-_.]?\d*)?"
            r"(?:[-_.]?dev[-_.]?\d*)?(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?"
        )
        # uvx also supports package@version for its positional command, but
        # --from/--with take requirement specs (package==version).
        separator = r"(?:==|@)" if positional else "=="
        pattern = rf"{name}{extras}{separator}{version}"
    return bool(re.fullmatch(pattern, spec, re.IGNORECASE))


def pinned_command(server):
    command = server.get("command")
    args = server.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        return False
    launcher = Path(command).name
    if launcher not in {"npx", "uvx"}:
        return os.path.isabs(command)
    package_options = {"--package", "-p"} if launcher == "npx" else {"--from", "--with", "-w"}
    switches = {"-y", "--yes", "--no-install", "--no"} if launcher == "npx" else set()
    supplies_command = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            break
        index += 1
        if arg in switches:
            continue
        option, equals, spec = arg.partition("=")
        if option not in package_options:
            return False  # Do not guess unknown flags' arity or package sources.
        if not equals:
            if index == len(args):
                return False
            spec = args[index]
            index += 1
        if not pinned_package(spec, launcher):
            return False
        if launcher == "npx" or option == "--from":
            if launcher == "uvx" and supplies_command:
                return False  # uvx --from is singular; --with can repeat.
            supplies_command = True
    if index == len(args) or not args[index] or args[index].startswith("-"):
        return False
    # Both CLIs stop parsing launcher options at the command. Later arguments
    # belong to the application. --with alone does not pin uvx's main package.
    return supplies_command or pinned_package(args[index], launcher, positional=True)


class Scanner:
    def __init__(self, engine, overlay, modules, home, backup_dir=None, vault_key=None, backup_key=None):
        # Canonicalize only explicit root anchors (macOS /var is a symlink).
        self.engine, self.overlay, self.home = (Path(p).expanduser().resolve() for p in (engine, overlay, home))
        if self.overlay.is_relative_to(self.engine):
            raise ScanError("baseline must live in a deployment overlay outside Core")
        self.layers = [("core", self.engine)] + [
            (f"module-{i}", Path(p).expanduser().resolve()) for i, p in enumerate(modules)
        ]
        self.layers.append(("overlay", self.overlay))
        self.uid = self.engine.stat().st_uid
        self.backup_dir = Path(backup_dir).expanduser().absolute() if backup_dir else self.home / "kbots-backups"
        self.keys = {self.home / ".config/kbots-vault-key", self.home / ".config/kbots-backup-key"}
        self.keys.update(Path(p).expanduser().absolute() for p in (vault_key, backup_key) if p)
        self.items = {}
        self.errors = {}
        self.protected = set()

    def error(self, location, message):
        self.errors.setdefault(location, set()).add(message)

    def item(self, location, value):
        self.items[location] = digest(value)

    def children(self, path, *, ignore_generated=True):
        try:
            with directory(path) as fd:
                return [path / name for name in sorted(os.listdir(fd)) if not ignore_generated or name not in IGNORED]
        except FileNotFoundError:
            return []
        except OSError:
            self.error(str(path), "directory unreadable or symlinked")
            return []

    def files(self, path):
        for child in self.children(path):
            try:
                info = metadata(child)
                if stat.S_ISDIR(info.st_mode):
                    yield from self.files(child)
                elif child.suffix != ".pyc":
                    yield child
            except OSError:
                self.error(str(child), "cannot inspect file")

    def secret_mode(self, path, label, required=False, *, inventory=True, is_directory=False):
        try:
            info = metadata(path)
        except FileNotFoundError:
            if required:
                self.error(label, "secret file disappeared during scan")
            elif inventory:
                self.item(label, "absent")
            return False
        except OSError:
            self.error(label, "cannot inspect secret-file metadata")
            return False
        self.protected.add((info.st_dev, info.st_ino))
        mode = stat.S_IMODE(info.st_mode)
        if inventory:
            self.item(label, {"mode": mode, "uid": info.st_uid, "type": stat.S_IFMT(info.st_mode)})
        reasons = []
        expected_mode = 0o700 if is_directory else 0o600
        if is_directory:
            if not stat.S_ISDIR(info.st_mode):
                reasons.append("must be a directory, not a link")
        elif not stat.S_ISREG(info.st_mode):
            reasons.append("must be a regular file, not a link")
        if mode != expected_mode:
            reasons.append(f"mode {mode:04o}; requires {expected_mode:04o}")
        if info.st_uid != self.uid or info.st_uid == 0:
            reasons.append(f"owner uid {info.st_uid}; requires non-root install uid {self.uid}")
        if reasons:
            self.error(label, "; ".join(reasons))
        return not reasons

    def backup_files(self, path):
        # Backup creation/pruning changes names daily, not harness rights. Check
        # all entries without adding their names to the baseline or reading them.
        for child in self.children(path, ignore_generated=False):
            try:
                info = metadata(child)
            except FileNotFoundError:
                continue  # Retention pruning between listing and stat is safe.
            except OSError:
                self.error(str(child), "cannot inspect backup metadata")
                continue
            if stat.S_ISDIR(info.st_mode) and not child.name.lower().endswith(ARCHIVE_SUFFIXES):
                self.backup_files(child)
            else:
                self.secret_mode(child, str(child), inventory=False)

    def contents(self, path, label):
        try:
            return read_file(path, self.protected)
        except (OSError, ScanError):
            self.error(label, "cannot safely read regular file (no links, hard links or oversized files)")
            return None

    def parsed(self, path, label):
        raw = self.contents(path, label)
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8")
            value = (
                json.loads(text, object_pairs_hook=unique)
                if path.suffix == ".json"
                else yaml.load(text, Loader=StrictLoader)
            )
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise ScanError("expected mapping")
            # Reject recursive aliases and non-JSON scalar types, without printing them.
            json.dumps(value, allow_nan=False)
            return value
        except (ValueError, TypeError, RecursionError, yaml.YAMLError, ScanError):
            self.error(label, "invalid or ambiguous configuration; values withheld")
            return None

    def settings(self, path, label):
        value = self.parsed(path, label)
        if value is None:
            return
        self.item(label, "settings")
        for key, item in value.items():
            if key == "permissions" and isinstance(item, dict):
                for subkey, rule in item.items():
                    if subkey in {"allow", "deny"} and isinstance(rule, list) and all(isinstance(r, str) for r in rule):
                        rule = sorted(set(rule))
                    self.item(f"{label}#permissions/{pointer(subkey)}", rule)
            else:
                self.item(f"{label}#{pointer(key)}", item)

    def hooks(self, value, label):
        if isinstance(value, dict):
            for key, child in value.items():
                location = f"{label}/{pointer(key)}"
                if "hook" in key.lower():
                    self.item(location, child)
                else:
                    self.hooks(child, location)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                self.hooks(child, f"{label}/{i}")

    def mcp(self, path, label):
        config = self.parsed(path, label)
        if config is None:
            return
        servers = config.get("servers", {})
        if not isinstance(servers, dict):
            self.error(label, "servers must be a mapping")
            return
        self.item(label, "mcp")
        for name, server in servers.items():
            location = f"{label}#servers/{pointer(name)}"
            self.item(location, server)
            if not isinstance(server, dict):
                self.error(location, "server must be a mapping")
            elif ("command" in server or server.get("transport", "stdio") == "stdio") and not pinned_command(server):
                self.error(location, "command requires an absolute executable or an exactly pinned npx/uvx package")
        for key, value in config.items():
            if key != "servers":
                self.item(f"{label}#{pointer(key)}", value)

    def prompt(self, path, label, also_hash=False):
        raw = self.contents(path, label)
        if raw is None:
            return
        try:
            hits = credential_lines(raw.decode("utf-8"))
        except UnicodeDecodeError:
            # Non-text skill assets still participate in content drift.
            if not also_hash:
                self.error(label, "prompt is not UTF-8 text")
            hits = []
        self.items[label] = hashlib.sha256(raw).hexdigest() if also_hash else digest(hits)
        for line, reason in hits:
            self.error(label, f"line {line}: {reason}; value withheld")

    def scan(self):
        # Secret metadata first. None of these paths are passed to read_file.
        for name, layer in self.layers:
            for path in self.children(layer / "config"):
                if path.name.startswith("secrets.enc"):
                    # FernetVault._persist atomically replaces this temporary
                    # file on every write. Validate it without inventory churn.
                    temporary = path.name == "secrets.enc.tmp"
                    self.secret_mode(
                        path, f"{name}/config/{path.name}", required=not temporary, inventory=not temporary
                    )
        for path in sorted(self.keys):
            self.secret_mode(path, str(path))
        if self.secret_mode(self.backup_dir, str(self.backup_dir), is_directory=True):
            self.backup_files(self.backup_dir)
        self.item(
            "scope#roots",
            [(name, str(path)) for name, path in self.layers]
            + [("home", str(self.home)), ("backups", str(self.backup_dir))],
        )
        for name, layer in self.layers:
            if not layer.is_dir():
                self.error(name, "configured layer directory missing")
                continue
            for path in self.children(layer / "config"):
                label = f"{name}/config/{path.name}"
                if path.name == "mcp.yaml":
                    self.mcp(path, label)
                elif path.name in {"config.yaml", "agents.yaml", "security.yaml"}:
                    value = self.parsed(path, label)
                    if value is not None:
                        self.hooks(value, label + "#")
            for agent in self.children(layer / "agents"):
                try:
                    info = metadata(agent)
                    if stat.S_ISLNK(info.st_mode):
                        self.error(f"{name}/{agent.relative_to(layer)}", "agent directory is symlinked")
                        continue
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                except OSError:
                    self.error(f"{name}/{agent.relative_to(layer)}", "agent directory cannot be inspected")
                    continue
                for subdir, names in [
                    (".claude", {"settings.json", "settings.local.json"}),
                    (".codex", {"hooks.json"}),
                ]:
                    for path in self.children(agent / subdir):
                        if path.name in names:
                            self.settings(path, f"{name}/{path.relative_to(layer)}")
                for path in self.children(agent):
                    if path.name in {"CLAUDE.md", "AGENTS.md"}:
                        self.prompt(path, f"{name}/{path.relative_to(layer)}")
                for path in self.files(agent / "skills"):
                    self.prompt(path, f"{name}/{path.relative_to(layer)}", also_hash=True)
                for path in self.files(agent / "codex"):
                    self.prompt(path, f"{name}/{path.relative_to(layer)}")
            for folder in ("skills", "tools", "codex"):
                for path in self.files(layer / folder):
                    self.prompt(path, f"{name}/{path.relative_to(layer)}", also_hash=folder != "codex")
            if name == "core":
                for path in self.files(layer / "src/tools"):
                    raw = self.contents(path, f"{name}/{path.relative_to(layer)}")
                    if raw is not None:
                        self.items[f"{name}/{path.relative_to(layer)}"] = hashlib.sha256(raw).hexdigest()
            for filename in ("AGENTS.md", "CLAUDE.md"):
                if any(p.name == filename for p in self.children(layer)):
                    self.prompt(layer / filename, f"{name}/{filename}")
        return self.items, self.errors


def load_baseline(path, uid):
    try:
        info = metadata(path)
    except FileNotFoundError:
        return None
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ScanError("baseline requires install ownership and mode 0600")
    try:
        data = json.loads(read_file(path), object_pairs_hook=unique)
        if set(data) != {"version", "items"} or data["version"] != VERSION or not isinstance(data["items"], dict):
            raise ValueError
        if not all(isinstance(k, str) and isinstance(v, str) and HEX.fullmatch(v) for k, v in data["items"].items()):
            raise ValueError
        return data["items"]
    except (ValueError, TypeError):
        raise ScanError("invalid baseline; restore the last reviewed baseline") from None


def save_baseline(path, items):
    payload = (json.dumps({"version": VERSION, "items": items}, indent=2, sort_keys=True) + "\n").encode()
    with directory(path.parent) as parent:
        temp = f".harness-baseline-{uuid.uuid4().hex}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accept", action="store_true", help="print and accept reviewed drift; never suppress scan errors"
    )
    parser.add_argument("--engine", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overlay", type=Path, default=os.environ.get("KBOTS_OVERLAY"))
    parser.add_argument("--module", action="append", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.overlay is None:
        print("HARNESS BLOCKED: set KBOTS_OVERLAY or --overlay; no baseline in Core")
        return 2
    try:
        modules = (
            args.module if args.module is not None else [p for p in os.environ.get("KBOTS_MODULES", "").split(":") if p]
        )
        scan = Scanner(
            args.engine,
            args.overlay,
            modules,
            Path.home(),
            os.environ.get("KBOTS_BACKUP_DIR"),
            os.environ.get("KBOTS_VAULT_KEY_FILE"),
            os.environ.get("KBOTS_BACKUP_KEY_FILE"),
        )
        baseline = scan.overlay / "config/harness-baseline.json"
        old = load_baseline(baseline, scan.uid)
        items, errors = scan.scan()
        changes = {key for key in items.keys() | (old or {}).keys() if items.get(key) != (old or {}).get(key)}
        if old is None:
            print("HARNESS: missing baseline; full inventory requires explicit review")
        for key in sorted(changes | errors.keys()):
            before, after = (old or {}).get(key), items.get(key)
            action = "ADDED" if before is None else "REMOVED" if after is None else "CHANGED"
            detail = f"{action} {before or '-'} -> {after or '-'}" if key in changes else "UNSAFE"
            reasons = "; ".join(sorted(errors.get(key, ())))
            # JSON escaping prevents terminal controls and multiline log spoofing.
            print(f"FINDING {json.dumps(key)}: {detail}" + (f"; {reasons}" if reasons else ""))
        if errors:
            print("HARNESS BLOCKED: fix unsafe/unreadable files; --accept cannot approve scan errors")
            return 2
        if args.accept:
            if os.geteuid() != scan.uid or scan.uid == 0:
                raise ScanError("accept must run as the non-root install owner")
            print(f"ACCEPTING {len(changes)} reviewed item changes into {baseline}", flush=True)
            # Detect edits while printing/reviewing before publishing a baseline.
            again = Scanner(
                args.engine,
                args.overlay,
                modules,
                Path.home(),
                os.environ.get("KBOTS_BACKUP_DIR"),
                os.environ.get("KBOTS_VAULT_KEY_FILE"),
                os.environ.get("KBOTS_BACKUP_KEY_FILE"),
            )
            current, faults = again.scan()
            if faults or current != items:
                raise ScanError("surface changed during acceptance; scan again")
            save_baseline(baseline, items)
            print(f"HARNESS: accepted {len(items)} hashed items")
            return 0
        if old is None or changes:
            print("HARNESS BLOCKED: review the diff, then run scripts/harness-scan.py --accept as the install owner")
            return 1
        print(f"HARNESS OK: {len(items)} items match the reviewed baseline")
        return 0
    except (OSError, ScanError, RecursionError):
        print(
            "HARNESS BLOCKED: cannot safely scan or write baseline; "
            "check paths, ownership, permissions and format (values withheld)"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
