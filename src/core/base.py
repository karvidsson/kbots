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
