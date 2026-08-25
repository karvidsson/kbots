"""Base interfaces for all kbots modules."""

import logging
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)

# Project root — directory containing src/
# This is the single source of truth. Import from here, don't recompute.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def resolve_kbots_tmp() -> Path:
    """Shared temp directory for agent-generated files (media, docs, scratch).

    Precedence: KBOTS_TMP env → KBOTS_OVERLAY/tmp → /tmp

    A function as well as the KBOTS_TMP constant below, because the constant
    freezes whatever the environment held at import time. Callers that build
    configuration for a *different* process — writing an agent's sandbox
    permissions, say — need the value at call time, and must not re-implement
    this precedence and drift from it.
    """
    tmp = os.environ.get("KBOTS_TMP", "")
    if not tmp:
        overlay = os.environ.get("KBOTS_OVERLAY", "")
        tmp = os.path.join(overlay, "tmp") if overlay else "/tmp"
    return Path(tmp)


KBOTS_TMP = resolve_kbots_tmp()


def resolve_overlay() -> Path | None:
    """The deployment overlay root, or None on an engine-local install."""
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) if overlay else None


def agent_session_dirs(extra_dirs=None, configured=None) -> list[str]:
    """Directories an agent session may reach beyond its own agent directory.

    A CLI agent enforces a working-DIRECTORY boundary on top of the permission
    allow-list, and the two are separate gates. `Read($KBOTS_TMP/**)` in
    settings.json is therefore not enough on its own: the path has to be inside
    the session's directory set as well. Without the shared temp dir there, an
    agent produces a screenshot, a chart or a PDF through an MCP tool and is
    then refused permission to open the file it just made. The tool reports
    success and a path, the read is denied, and nothing in the refusal names
    the real cause. Every visual task degrades into guesswork.

    The codex is here for the same reason. Each agent's startup context lists
    the shared documents and tells it to open the file when the work touches
    it, which is not an instruction an agent can follow if the directory is out
    of bounds.

    Nothing else is granted by default. The overlay root holds config/
    (including the encrypted vault), data/ (every agent's memory, turns and
    audit log) and agents/<other-agent>/, so widening to it is a change to the
    isolation model rather than a path fix. `defaults.sandbox.additional_dirs`
    exists for a deployment that wants it, deliberately.

    Only directories that exist are returned: --add-dir on a missing path is
    refused, which would take the whole session down rather than just that one
    directory.
    """
    candidates: list[str] = [str(resolve_kbots_tmp())]
    overlay = resolve_overlay()
    if overlay:
        candidates.append(str(overlay / "codex"))
    candidates += [str(d) for d in (configured or [])]
    candidates += [str(d) for d in (extra_dirs or [])]

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        if not raw:
            continue
        p = Path(raw).expanduser()
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir():
            out.append(key)
        else:
            logger.warning(
                f"Agent workspace directory does not exist, not granting it: {key}")
    return out


def resolve_data_dir(config: dict) -> Path:
    """The deployment's data directory, absolute.

    `kbots.data_dir` is the one place a deployment says where its state lives.
    """
    d = Path(config.get("kbots", {}).get("data_dir", "./data"))
    return d if d.is_absolute() else (PROJECT_ROOT / d)


def memory_config(config: dict) -> dict:
    """`defaults.memory` with its store paths anchored to the data dir.

    Both memory stores took a relative default and resolved it against
    PROJECT_ROOT, so `data_dir` never governed either one. Pointing data_dir at
    an overlay moved the training corpus, the audit log and version.json and
    silently left memory behind, producing two divergent stores with the config
    naming the one nothing was writing to. A scrub or an audit run against the
    configured path read a stale database and reported success.

    An explicit `path` in config still wins; this only supplies the default.
    """
    cfg = dict(config.get("defaults", {}).get("memory", {}) or {})
    data_dir = resolve_data_dir(config)
    cfg.setdefault("path", str(data_dir / "memory.db"))
    # The embedding model is state, not code, and it is downloaded at runtime.
    # Left relative it landed under the repo, so an overlay deployment kept its
    # memories in one place and re-fetched a 130MB model into another.
    cfg.setdefault("model_dir", str(data_dir / "models" / "bge-small-en-v1.5"))
    graph = dict(cfg.get("graph") or {})
    if graph:
        graph.setdefault("path", str(data_dir / "graph" / "memory.lbdb"))
        cfg["graph"] = graph

    # A relative store path is the trap that caused this. It reads as
    # configured, and then resolves against PROJECT_ROOT rather than the data
    # dir, landing the store somewhere the config never mentions. Rewriting it
    # silently would be its own surprise, so say it instead.
    for label, p in (("defaults.memory.path", cfg.get("path")),
                     ("defaults.memory.graph.path", graph.get("path"))):
        if p and not Path(p).is_absolute():
            logger.warning(
                f"{label} = {p!r} is relative, so it resolves against {PROJECT_ROOT} "
                f"and NOT against data_dir ({data_dir}). Make it absolute, or drop "
                f"the key to take the data_dir default."
            )
    return cfg


def warn_on_split_store(config: dict) -> list[str]:
    """Names of stores that also exist, non-empty, at the pre-data_dir location.

    A leftover is not an error — it is the old store after a cutover. But it is
    the exact shape of the bug above, so it must never again be silent.
    """
    stale: list[str] = []
    if resolve_data_dir(config).resolve() == (PROJECT_ROOT / "data").resolve():
        return stale
    for rel in ("memory.db", "graph/memory.lbdb"):
        legacy = PROJECT_ROOT / "data" / rel
        try:
            if legacy.is_file() and legacy.stat().st_size > 0:
                stale.append(str(legacy))
        except OSError:
            continue
    return stale


def resolve_vault_key_file() -> Path:
    """Resolve the vault key file path.

    Checks KBOTS_VAULT_KEY_FILE env var, then falls back to ~/.config/kbots-vault-key.
    """
    return Path(os.environ.get("KBOTS_VAULT_KEY_FILE", "~/.config/kbots-vault-key")).expanduser()


def harden_path(path, file_mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Best-effort tighten permissions on a data file and its parent dir.

    Data files (SQLite DBs, audit/training JSONL, runtime state) can hold tool
    args/outputs and flags; on a multi-user host the default umask leaves them
    world-readable. Never raises — a perms failure must not break startup.
    """
    p = Path(path)
    try:
        if p.exists():
            os.chmod(p, file_mode)
        parent = p.parent
        if parent.exists():
            os.chmod(parent, dir_mode)
    except OSError as e:
        logger.debug(f"harden_path({path}) failed: {e}")


def write_private_file(path: Path, content: str, dir_mode: int = 0o700) -> None:
    """Write a sensitive file so it is 0600 from its first byte on disk.

    write_text() + chmod() leaves a window where the file sits at the umask
    default (0644 on most hosts) with the secret already inside; opening with
    an explicit 0600 mode closes it. The parent directory is created 0700 only
    when it does not exist yet — an existing ~/.config keeps its mode.
    """
    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=dir_mode)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    # O_CREAT's mode only applies to new files — repair a pre-existing one.
    os.chmod(path, 0o600)


def read_vault_key_file(key_file: Path) -> str:
    """Read the plaintext vault passphrase, enforcing owner-only permissions.

    The key file holds the passphrase in cleartext — if it is group- or
    world-readable, encryption at rest is meaningless. On a permissive mode we
    log a warning and tighten it to 0600 in place (non-fatal so a restarted
    service still comes up), then read it.
    """
    import stat as _stat
    try:
        mode = key_file.stat().st_mode
        if mode & (_stat.S_IRWXG | _stat.S_IRWXO):
            logger.warning(
                f"Vault key file {key_file} is group/world-accessible "
                f"(mode {oct(mode & 0o777)}) — tightening to 0600."
            )
            os.chmod(key_file, 0o600)
    except OSError as e:
        logger.warning(f"Could not check/tighten vault key file permissions: {e}")
    return key_file.read_text().strip()


def resolve_config_file(name: str) -> Path:
    """Find a config file across layers: overlay → modules → core.

    Returns the first matching file, or the core path as fallback.
    """
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config" / name
        if p.exists():
            return p
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        p = Path(mod_path.strip()) / "config" / name
        if p.exists():
            return p
    return PROJECT_ROOT / "config" / name


def overlay_state_path(name: str) -> Path | None:
    """Where a small shared state file is WRITTEN: the overlay's data/ directory.

    Five of these (schedules, runtime flags, triggers, session consent, the
    feedback map) each resolved their own path against the overlay ROOT. A
    hardened service unit grants ReadWritePaths to the subdirectories it needs,
    which leaves that root read-only, so inside the service every write failed
    while the same code worked perfectly from a shell. One of them suppressed
    the error and re-fired a `once` schedule every tick, forever.

    One helper rather than a sixth copy of the same two lines.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / "data" / name if overlay else None


def overlay_state_legacy_path(name: str) -> Path | None:
    """The pre-migration location at the overlay root. Read, never written."""
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / name if overlay else None


def overlay_state_read_path(name: str) -> Path | None:
    """The file to READ: the current location, else the legacy one, else None.

    State written before the move keeps governing until the first write carries
    it forward, so an existing install loses nothing on upgrade.
    """
    path = overlay_state_path(name)
    if path and path.exists():
        return path
    legacy = overlay_state_legacy_path(name)
    return legacy if legacy and legacy.exists() else None


# === Messages ===

@dataclass
class Attachment:
    filename: str
    url: str
    content_type: str | None = None
    size: int | None = None


@dataclass
class IncomingMessage:
    """Normalized message from any connector."""
    connector: str
    channel_id: str
    user_id: str
    user_name: str
    content: str
    # Human-readable channel name, when the platform has one (None for DMs)
    channel_name: str | None = None
    # True when delivered only because the agent watches this channel —
    # the message did not mention or address the agent directly
    watched: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: str | None = None
    raw: Any = None
    # Which bot account received this message (for multi-bot routing)
    bot_account: str | None = None
    # Skill invocation (set by slash command handler, None for regular messages)
    skill: str | None = None
    skill_params: dict | None = None


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class Message:
    """Internal message representation for LLM context."""
    role: MessageRole
    content: str
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None
    name: str | None = None  # for tool messages


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str
    tool_calls: list[dict] | None = None
    tokens_used: int | None = None
    model: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None  # Claude Code CLI session ID for --resume
    # Usage-limit handling: set when the provider auto-downgraded the model to
    # keep the session going after hitting a per-model usage cap.
    usage_downgraded: bool = False
    reset_hint: str | None = None  # human text about when the limit resets, if known


# === Tool types ===

@dataclass
class ToolParam:
    """A single parameter for a tool."""
    name: str
    type: str  # string, integer, boolean, number
    description: str = ""
    required: bool = True
    default: Any = None
    choices: list[Any] | None = None


@dataclass
class ToolDef:
    """A registered tool definition."""
    name: str
    description: str
    parameters: list[ToolParam]
    func: Callable  # the actual async function
    category: str = "general"
    hitl: bool = False


class ToolContext:
    """Per-call context passed to tool functions. Discarded after each call."""
    __slots__ = ("agent_id", "session_id", "channel_id", "user_id",
                 "memory", "vault", "registry", "connector_send",
                 "agent_manager", "inter_agent_depth", "project_dir")

    def __init__(self, *, agent_id: str, session_id: str | None = None,
                 channel_id: str | None = None, user_id: str | None = None,
                 memory: "MemoryBackend | None" = None,
                 vault: "VaultBackend | None" = None,
                 registry: Any = None,
                 connector_send: Callable | None = None,
                 agent_manager: Any = None,
                 inter_agent_depth: int = 0,
                 project_dir: str | None = None):
        self.agent_id = agent_id
        self.session_id = session_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.memory = memory
        self.vault = vault
        self.registry = registry
        self.connector_send = connector_send
        self.agent_manager = agent_manager
        self.inter_agent_depth = inter_agent_depth
        self.project_dir = project_dir


# === Skill types ===

@dataclass
class SkillParam:
    """A parameter defined in a skill YAML."""
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    choices: list[str] | None = None


@dataclass
class Skill:
    """A loaded skill definition."""
    name: str
    description: str
    prompt: str
    tools: list[str] = field(default_factory=list)
    parameters: list[SkillParam] = field(default_factory=list)
    command: str | None = None
    # Optional provider pin for this skill's turns, e.g.
    # {"provider": "local", "model": "qwen3.5:9b", "fallback": true}.
    # fallback (default true) retries once on the agent's default provider
    # when the pinned provider errors.
    llm: dict | None = None
    # When true, the turn's toolset is skill.tools ∩ the agent's allowlist
    # (a narrow, scoped set — what makes small local models reliable) instead
    # of the default union.
    restrict_tools: bool = False
    # Per-skill tool-round cap (0 = engine default).
    max_rounds: int = 0


# === Module interfaces ===

class Connector(ABC):
    """Base class for all connectors (Discord, HTTP, etc.)."""
    name: str

    def __init__(self, config: dict, vault: "VaultBackend | None" = None):
        self.config = config
        self.vault = vault
        self._on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None

    @property
    def on_message(self) -> Callable[[IncomingMessage], Awaitable[None]] | None:
        return self._on_message

    @on_message.setter
    def on_message(self, handler: Callable[[IncomingMessage], Awaitable[None]]):
        self._on_message = handler

    async def emit(self, message: IncomingMessage) -> None:
        """Emit an incoming message to the router."""
        if self._on_message:
            await self._on_message(message)
        else:
            logger.warning(f"[{self.name}] No message handler registered, dropping message")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, channel_id: str, content: str, **kwargs) -> None: ...

    @asynccontextmanager
    async def typing(self, channel_id: str, **kwargs) -> AsyncIterator[None]:
        """Show typing indicator. Override in subclasses for platform support.

        Accepts **kwargs (e.g. bot_account) so connectors without platform
        support don't break when the agent manager passes routing hints.
        """
        yield


class LLMProvider(ABC):
    """Base class for LLM providers."""
    name: str
    # True for CLI-backed providers that load the agent identity (AGENTS.md /
    # CLAUDE.md) from the project directory themselves — the engine must not
    # inject it as a system message on top.
    reads_project_context: bool = False

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse: ...


class MemoryBackend(ABC):
    """Base class for memory backends."""
    name: str

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def store(self, content: str, type: str, agent_id: str | None = None,
                    tags: list[str] | None = None, **kwargs) -> int: ...

    @abstractmethod
    async def search(self, query: str, agent_id: str | None = None,
                     limit: int = 10) -> list[dict]: ...

    @abstractmethod
    async def forget(self, memory_id: int) -> None: ...


class VaultBackend(ABC):
    """Base class for credential vault."""
    name: str

    @abstractmethod
    def unlock(self, passphrase: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def list_keys(self) -> list[str]: ...
