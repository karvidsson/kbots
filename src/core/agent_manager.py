"""Agent manager — loads agents from config, manages sessions, dispatches to LLM."""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.base import (
    Attachment,
    Connector,
    IncomingMessage,
    LLMProvider,
    MemoryBackend,
    Message,
    MessageRole,
    ToolContext,
    ToolDef,
    VaultBackend,
)
from src.core.skills import get_skill, render_skill_prompt
from src.core.tools import get_tool, get_tools_for_agent
from src.memory.recall import format_block, recall

if TYPE_CHECKING:
    from src.core.access_control import AccessControl
    from src.core.alerts import AlertSender
    from src.core.audit import AuditLog
    from src.core.content_safety import BehaviorMonitor
    from src.core.hitl import HITLGate
    from src.core.rate_limiter import RateLimiter
    from src.core.storage import Storage
    from src.core.training_collector import TrainingCollector

logger = logging.getLogger(__name__)

# Abstain sentinel: an agent that has nothing substantive to say replies NO_REPLY
# and the turn is dropped instead of posted. Without this, "I'll stay silent" is
# itself a message — two agents politely acknowledging each other's silence loop
# forever. Tolerates markdown/quote wrappers the model may add around it.
_NO_REPLY_RE = re.compile(r"^[\s>*_`\"'()\[\]{}~.-]*NO_REPLY\b", re.IGNORECASE)


def is_no_reply(content: str) -> bool:
    """True when the agent chose the NO_REPLY abstain sentinel."""
    return bool(_NO_REPLY_RE.match(content or ""))


def compute_cli_tool_grants(agent_tool_names, active_skill=None):
    """Allowed/disallowed MCP tool names for the Claude Code CLI.

    Always returns an explicit --allowedTools list — including for
    tools: "all". Relying on settings.json wildcards ('mcp__server__*' is
    not a valid CC permission rule) or grants accumulated in ~/.claude.json
    broke after the kbots rename changed project paths and tool names:
    headless runs auto-denied every MCP call ("you haven't granted it yet").
    """
    from src.core.tools import get_all_tools

    # External MCP servers (mcp.yaml → every agent's .mcp.json) need
    # server-level grants — they are not in the kbots tool registry, so the
    # explicit per-tool list below can never cover them. Without this, agents
    # hit CLI permission denials on every external tool (observed live:
    # an agent was observed self-editing settings.json via run_command to work around it).
    try:
        from src.core.digest import _load_mcp_servers
        external_allows = [f"mcp__{name}" for name in _load_mcp_servers()
                           if name != "kbots-tools"]
    except Exception:
        external_allows = []

    if (active_skill and getattr(active_skill, "restrict_tools", False)
            and getattr(active_skill, "tools", None)
            and agent_tool_names and agent_tool_names != "all"):
        # Mirror the restriction for Claude-run skills (CLI gates by name list)
        skill_set = set(active_skill.tools)
        agent_tool_names = [t for t in agent_tool_names if t in skill_set]
    all_tool_names = [f"mcp__kbots-tools__{t}" for t in get_all_tools().keys()]
    if agent_tool_names and agent_tool_names != "all":
        allowed = [f"mcp__kbots-tools__{t}" for t in agent_tool_names]
        disallowed = [t for t in all_tool_names if t not in allowed]
        return allowed + external_allows, disallowed
    return all_tool_names + external_allows, None


class SessionState(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"


@dataclass
class Session:
    """Lightweight session handle. No conversation content stored here."""
    id: str
    agent_id: str
    channel_id: str
    user_id: str | None
    state: SessionState = SessionState.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    cli_session_id: str | None = None  # Claude Code CLI session for --resume
    # Temporary model downgrade after a usage cap — keeps this conversation on
    # a cheaper model until the estimated reset, then reverts to configured.
    model_override: str | None = None
    model_override_until: float = 0.0
    # Tier-router state: True while the latest turn(s) were handled by the local
    # model (used to clear a stale CLI session when escalating back to Claude).
    routed_local: bool = False


class AgentManager:
    """Manages agent instances, sessions, and message dispatch."""

    def __init__(
        self,
        agent_configs: dict[str, dict],
        connectors: dict[str, Connector],
        llm_providers: dict[str, LLMProvider],
        memory_backends: dict[str, MemoryBackend],
        vault: VaultBackend | None = None,
        defaults: dict | None = None,
        storage: "Storage | None" = None,
        hitl: "HITLGate | None" = None,
        rate_limiter: "RateLimiter | None" = None,
        audit: "AuditLog | None" = None,
        behavior_monitor: "BehaviorMonitor | None" = None,
        access_control: "AccessControl | None" = None,
        alerter: "AlertSender | None" = None,
        training_collector: "TrainingCollector | None" = None,
    ):
        self.agent_configs = agent_configs
        self.connectors = connectors
        self.llm_providers = llm_providers
        self.memory_backends = memory_backends
        self.vault = vault
        self.defaults = defaults or {}
        self.storage = storage
        self.hitl = hitl
        self.rate_limiter = rate_limiter
        self.audit = audit
        self.behavior_monitor = behavior_monitor
        self.access_control = access_control
        self.alerter = alerter
        self.training_collector = training_collector

        # Validate agent isolation before anything else
        self._validate_project_dirs()

        # Session map — bounded by max_sessions (LRU)
        max_sessions = self.defaults.get("session", {}).get("max_sessions", 100)
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self._max_sessions = max_sessions
        self._max_history = self.defaults.get("session", {}).get("max_history", 50)
        self._max_tool_rounds = 10  # prevent infinite tool loops
        # Track running LLM processes per agent for /stop
        self._running_procs: dict[str, asyncio.subprocess.Process] = {}
        self.active_turns = 0  # in-flight handle_message turns (drained on shutdown)
        # Metadata for each in-flight turn, so turns killed at the shutdown
        # drain timeout can be recovered next boot (src/core/recovery.py)
        self._inflight_turns: dict[int, dict] = {}
        self._inflight_seq = 0
        # Per-session locks: serialize messages to the same channel so a
        # follow-up doesn't run concurrently with the current turn and collide
        # on the shared CLI --resume session. Different channels stay parallel.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Model-tier router (opt-in per agent via llm.router.enabled): a small
        # local model triages requests; clearly-simple ones stay local.
        from src.core.model_router import ModelRouter
        self._model_router = ModelRouter(self.llm_providers)

    def _validate_project_dirs(self) -> None:
        """Validate that no non-privileged agent's project_dir is an ancestor of another's.

        Privileged agents (e.g. rescue bot) are exempt — they intentionally
        run in the project root with full access, gated by HITL.
        """
        resolved: dict[str, Path] = {}
        privileged: set[str] = set()
        overlay = os.environ.get("KBOTS_OVERLAY")
        for agent_id, cfg in self.agent_configs.items():
            project_dir = cfg.get("project_dir", f"./agents/{agent_id}")
            path = Path(project_dir).resolve()
            # In three-layer deployments, agents live in the overlay
            if not path.is_dir() and overlay:
                overlay_dir = Path(overlay) / "agents" / agent_id
                if overlay_dir.is_dir():
                    path = overlay_dir.resolve()
            resolved[agent_id] = path
            if cfg.get("privileged", False):
                privileged.add(agent_id)

        if privileged:
            logger.info(f"Privileged agents (bypass isolation): {privileged}")

        for agent_a, path_a in resolved.items():
            # Privileged agents can be ancestors — that's the point
            if agent_a in privileged:
                continue
            for agent_b, path_b in resolved.items():
                if agent_a == agent_b:
                    continue
                try:
                    path_b.relative_to(path_a)
                    raise ValueError(
                        f"Agent isolation violation: '{agent_a}' project_dir ({path_a}) "
                        f"is an ancestor of '{agent_b}' project_dir ({path_b}). "
                        f"Each agent must have its own isolated directory. "
                        f"Set 'privileged: true' if this is intentional (e.g. rescue agent)."
                    )
                except ValueError as e:
                    if "Agent isolation violation" in str(e):
                        raise
                    # Not a parent — this is what we want

    def _session_key(self, agent_id: str, channel_id: str) -> str:
        return f"{agent_id}:{channel_id}"

    def _bump(self, name: str) -> None:
        """Fire-and-forget daily counter (routing/outcome metrics)."""
        if self.storage:
            asyncio.create_task(self.storage.bump_counter(name), name=f"bump-{name}")

    def _get_or_create_session(self, agent_id: str, channel_id: str,
                                user_id: str | None = None) -> Session:
        """Get existing session or create a new one. LRU eviction if over cap."""
        key = self._session_key(agent_id, channel_id)

        if key in self.sessions:
            session = self.sessions[key]
            session.last_active = time.time()
            session.state = SessionState.ACTIVE
            # Move to end (most recently used)
            self.sessions.move_to_end(key)
            return session

        # Evict LRU if at capacity
        while len(self.sessions) >= self._max_sessions:
            evicted_key, evicted = self.sessions.popitem(last=False)
            logger.info(f"Evicted session {evicted_key} (LRU)")

        session = Session(
            id=key,
            agent_id=agent_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        self.sessions[key] = session
        return session

    def _get_agent_llm(self, agent_id: str) -> LLMProvider:
        """Get the LLM provider for an agent."""
        agent_cfg = self.agent_configs[agent_id]
        llm_cfg = agent_cfg.get("llm", self.defaults.get("llm", {}))
        provider_name = llm_cfg.get("provider", "anthropic")

        if provider_name not in self.llm_providers:
            raise ValueError(
                f"Agent {agent_id} wants LLM provider '{provider_name}' "
                f"but available: {list(self.llm_providers.keys())}"
            )
        return self.llm_providers[provider_name]

    def _get_agent_memory(self, agent_id: str) -> MemoryBackend | None:
        """Get the memory backend for an agent."""
        agent_cfg = self.agent_configs[agent_id]
        mem_cfg = agent_cfg.get("memory", self.defaults.get("memory", {}))
        backend_name = mem_cfg.get("backend")
        if not backend_name:
            return None
        return self.memory_backends.get(backend_name)

    def _get_agent_tools(self, agent_id: str) -> list[ToolDef]:
        """Get the tool definitions an agent is allowed to use."""
        agent_cfg = self.agent_configs[agent_id]
        tool_names = agent_cfg.get("tools", [])
        return get_tools_for_agent(tool_names)

    def _get_project_dir(self, agent_id: str) -> str:
        """Get the project directory for an agent.

        Resolution: if the configured path exists, use it directly.
        Otherwise, check $KBOTS_OVERLAY/agents/<id> as a fallback
        (agents live in the overlay in three-layer deployments).
        """
        agent_cfg = self.agent_configs[agent_id]
        project_dir = agent_cfg.get("project_dir", f"./agents/{agent_id}")
        resolved = Path(project_dir).resolve()
        if resolved.is_dir():
            return project_dir
        # Try overlay
        overlay = os.environ.get("KBOTS_OVERLAY")
        if overlay:
            overlay_dir = Path(overlay) / "agents" / agent_id
            if overlay_dir.is_dir():
                return str(overlay_dir)
        return project_dir  # Return original (may fail, but logs will show why)

    def _read_identity_prompt(self, agent_id: str) -> str:
        """AGENTS.md (or legacy CLAUDE.md) from the agent's project dir, or
        the inline config fallback."""
        from src.core.agent_scaffold import read_identity
        identity = read_identity(self._get_project_dir(agent_id))
        if identity:
            return identity
        return self.agent_configs[agent_id].get("system_prompt", "")

    def _build_system_prompt(self, agent_id: str) -> str:
        """Build the system prompt for an agent.

        When using Claude Code as the LLM provider, the CLI loads the identity
        itself from the project directory (CLAUDE.md, which imports AGENTS.md)
        — no injection needed. For other providers, we read the identity file
        and inject it as a system message.
        """
        agent_cfg = self.agent_configs[agent_id]
        llm_cfg = agent_cfg.get("llm", self.defaults.get("llm", {}))
        provider = self.llm_providers.get(llm_cfg.get("provider", ""))
        if getattr(provider, "reads_project_context", False):
            return ""  # the CLI loads the identity from the project dir itself

        return self._read_identity_prompt(agent_id)

    async def _auto_recall(
        self, agent_id: str, user_message: str, context_blocks: list[str]
    ) -> list[str]:
        """Auto-recall relevant memories and inject them as context.

        Runs the fused pipeline (keyword + vector + one graph hop) rather than
        a single keyword search. Returns the ids of any recalled *lesson*
        memories so a reaction on the reply can score them (see feedback_map).

        This used to pass the raw message into an FTS5 MATCH, which on the live
        store returned nothing for 96% of real user messages. See
        src/memory/recall.py and src/memory/query.py.
        """
        if not user_message or len(user_message) < 5:
            return []

        memory = self._get_agent_memory(agent_id)
        if not memory:
            return []

        query = user_message[:500]

        try:
            # A missing or unopenable graph must cost the graph hop, never the
            # whole recall: the other two engines still have answers.
            try:
                from src.lib.graph_store import get_graph
                graph = get_graph()
            except Exception:
                graph = None

            results = await recall(memory, query, agent_id=agent_id,
                                   limit=5, graph=graph)
            block = format_block(results, agent_id=agent_id)
            if not block:
                return []

            context_blocks.append(block)
            logger.debug(f"Auto-recalled {len(results)} memories for {agent_id} "
                         f"(sources: {[r.get('sources') for r in results]})")
            return [m["id"] for m in results
                    if m.get("category") == "lesson" and m.get("id")]

        except Exception as e:
            logger.debug(f"Memory auto-recall failed: {e}")
            return []

    async def handle_message(self, agent_id: str, message: IncomingMessage) -> None:
        """Handle a message, serialized per channel so follow-ups queue in order.

        A new message that arrives while this agent is mid-turn in the same
        channel waits here for the current turn to finish, then runs — avoiding
        a concurrent --resume collision on the shared CLI session.
        """
        key = self._session_key(agent_id, message.channel_id)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            logger.info(f"{agent_id} busy in {message.channel_id} — queueing follow-up")
            # Let the user know their message landed and will be handled after
            # the current turn (headless Claude Code can't steer a running turn,
            # so we queue rather than interrupt). Skip for bot senders.
            is_bot = bool(
                message.raw and getattr(getattr(message.raw, "author", None), "bot", False)
            ) or bool(getattr(message, "_inter_agent_sender", None))
            connector = self.connectors.get(message.connector)
            if connector and not is_bot:
                with contextlib.suppress(Exception):
                    await connector.send(
                        message.channel_id,
                        "👀 Noted — I'll get to that as soon as I finish the current task.",
                        reply_to=message.raw,
                        bot_account=message.bot_account,
                    )
        async with lock:
            self.active_turns += 1
            self._inflight_seq += 1
            turn_key = self._inflight_seq
            self._inflight_turns[turn_key] = {
                "agent_id": agent_id,
                "connector": message.connector,
                "channel_id": message.channel_id,
                "user_id": message.user_id,
                "bot_account": message.bot_account,
                "started_at": time.time(),
            }
            try:
                await self._handle_message_inner(agent_id, message)
            finally:
                self.active_turns -= 1
                self._inflight_turns.pop(turn_key, None)

    def inflight_snapshot(self) -> list[dict]:
        """Metadata of turns currently running — the shutdown path persists
        these when the drain window expires so they can be recovered."""
        return list(self._inflight_turns.values())

    async def _handle_message_inner(self, agent_id: str, message: IncomingMessage) -> None:
        """Full flow: build context → call LLM → dispatch tools → send response."""
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            logger.error(f"Unknown agent: {agent_id}")
            return

        # Inter-agent deliveries are authenticated upstream (loopback bearer
        # token) — the sender is a peer agent, not a Discord user, so Layer 1
        # user-tier access control does not apply.
        sender_agent = getattr(message, "_inter_agent_sender", None)

        # --- Layer 1: Can this sender talk to this agent? ---
        if self.access_control and not sender_agent:
            is_bot = bool(
                message.raw
                and hasattr(message.raw, "author")
                and getattr(message.raw.author, "bot", False)
            )
            if not self.access_control.can_message(message.user_id, agent_id, is_bot):
                tier = self.access_control.resolve_tier(message.user_id, is_bot)
                logger.info(
                    f"Access control: sender={message.user_id} tier={tier} "
                    f"blocked from messaging agent={agent_id}"
                )
                return  # Silently ignore

        session = self._get_or_create_session(
            agent_id, message.channel_id, message.user_id
        )
        session.message_count += 1

        connector = self.connectors.get(message.connector)
        if not connector:
            logger.error(f"Unknown connector: {message.connector}")
            return

        # --- Build user content with auto-injected context ---
        user_content = message.content
        context_blocks = []

        # Inject channel context
        channel_context = f"<channel id=\"{message.channel_id}\" connector=\"{message.connector}\""
        if message.channel_name:
            safe_name = message.channel_name.replace("&", "&amp;").replace('"', "&quot;")
            channel_context += f" name=\"{safe_name}\""
        if message.bot_account:
            channel_context += f" bot=\"{message.bot_account}\""
        channel_context += " />"
        context_blocks.append(channel_context)

        if sender_agent:
            context_blocks.append(
                f"<inter-agent-message from=\"{sender_agent}\">This message is "
                f"from peer agent '{sender_agent}', delivered into your home "
                f"channel so the request and your reply stay visible in your "
                f"conversation history. Reply here as usual — your reply is "
                f"posted to this channel for the team to see, it is NOT "
                f"returned to '{sender_agent}' automatically. If "
                f"'{sender_agent}' needs an answer, message them back with "
                f"send_to_agent or ask_agent. If the message genuinely needs "
                f"no action or reply, respond with exactly NO_REPLY and "
                f"nothing else.</inter-agent-message>"
            )

        # Bot-to-bot turns get an abstain path: without one, "I have nothing to
        # add" is itself a channel message and two agents ack each other forever.
        sender_is_bot = bool(
            message.raw and getattr(getattr(message.raw, "author", None), "bot", False)
        )
        if sender_is_bot:
            context_blocks.append(
                "<bot-sender-notice>This message is from another bot agent, not a "
                "human. Reply normally when there is an open request, question, "
                "hand-off, or active thread to move forward — collaborating with peer "
                "agents is expected, and you do NOT need a human in the loop to do it. "
                "Only when the message genuinely needs no reply — a pure acknowledgement, "
                "status echo, standby restatement, or thanks — respond with exactly "
                "NO_REPLY and nothing else, and that reply is dropped so nothing is "
                "posted to the channel. When in doubt and there is real work to advance, "
                "reply.</bot-sender-notice>"
            )
        elif getattr(message, "watched", False):
            # Watched-channel delivery from a human who didn't address the
            # agent — give it a way to stay silent instead of butting in.
            context_blocks.append(
                "<channel-watch-notice>You received this because you watch this "
                "channel — it was not necessarily addressed to you. Act on it or "
                "reply when it moves your active tasks forward or clearly involves "
                "you. Otherwise respond with exactly NO_REPLY and nothing else, "
                "and that reply is dropped so nothing is posted to the "
                "channel.</channel-watch-notice>"
            )

        # Inject attachment info so the LLM knows about uploaded files
        # Include attachments from the current message AND any referenced (replied-to) message
        all_attachments = list(message.attachments)
        if message.raw and hasattr(message.raw, 'reference') and message.raw.reference:
            ref_msg = message.raw.reference.resolved
            if ref_msg and hasattr(ref_msg, 'attachments'):
                for a in ref_msg.attachments:
                    all_attachments.append(Attachment(
                        filename=a.filename, url=a.url,
                        content_type=a.content_type, size=a.size,
                    ))

        if all_attachments:
            att_lines = ["<attachments>"]
            for att in all_attachments:
                att_lines.append(
                    f'  <file name="{att.filename}" url="{att.url}" '
                    f'type="{att.content_type or "unknown"}" '
                    f'size="{att.size or 0}" />'
                )
            att_lines.append("</attachments>")
            att_lines.append(
                "Note: Use download_file to save attachments locally, then "
                "Read to view images/PDFs. For audio, use transcribe_audio."
            )
            context_blocks.append("\n".join(att_lines))

        # Inject user context from team system
        try:
            from src.tools.team import build_user_context
            user_context = build_user_context(
                message.user_id,
                inter_agent_sender=getattr(message, "_inter_agent_sender", "") or "",
            )
            if user_context:
                context_blocks.append(user_context)
        except ImportError:
            pass

        # Goal context: injected every turn (goal state mutates mid-conversation,
        # unlike the roster) — kept short by build_goal_context itself.
        try:
            from src.core.goals import build_goal_context
            goal_ctx = build_goal_context(agent_id, message.channel_id)
            if goal_ctx:
                context_blocks.append(goal_ctx)
        except Exception as e:
            logger.debug(f"Goal context injection failed: {e}")

        # Startup context: inject team roster, pinned memories, codex index
        # on first message of a session (subsequent messages inherit via --resume)
        if session.message_count == 1 or not session.cli_session_id:
            try:
                from src.core.startup_context import build_startup_context
                memory = self._get_agent_memory(agent_id)
                startup_ctx = await build_startup_context(
                    agent_id, memory, project_dir=self._get_project_dir(agent_id))
                if startup_ctx:
                    context_blocks.append(startup_ctx)
            except Exception as e:
                logger.debug(f"Startup context injection failed: {e}")

        # Auto-recall: search memory for relevant context
        recalled_lessons: list[str] = []
        try:
            recalled_lessons = await self._auto_recall(
                agent_id, message.content, context_blocks) or []
        except Exception as e:
            logger.debug(f"Auto-recall failed: {e}")

        if context_blocks:
            injected = "\n\n".join(context_blocks)
            user_content = f"{injected}\n\n{user_content}"

        # Skill invocation — render skill prompt
        # Skills start fresh (no --resume) to avoid stale context from prior conversations.
        if message.skill:
            skill = get_skill(message.skill)
            if skill:
                skill_prompt = render_skill_prompt(skill, message.skill_params or {})
                user_content = f"[Skill: {skill.name}]\n{skill_prompt}"
                if message.content:
                    user_content += f"\n\nUser message: {message.content}"
                # Force fresh CLI session — skills are self-contained and should not
                # carry context from previous conversations in the channel.
                session.cli_session_id = None
            else:
                await connector.send(
                    message.channel_id, f"Unknown skill: {message.skill}",
                    ephemeral=True, raw=message.raw,
                )
                return

        # --- Load session from storage (restore cli_session_id across restarts) ---
        if self.storage:
            stored = await self.storage.get_or_create_session(
                session.id, agent_id, message.channel_id, message.user_id
            )
            # Don't restore CLI session for skill invocations — they run fresh
            if stored.get("cli_session_id") and not session.cli_session_id and not message.skill:
                session.cli_session_id = stored["cli_session_id"]
                logger.debug(f"Restored CLI session {session.cli_session_id} from storage")

        # --- Build message context ---
        # Always load SQLite history into `messages`. When the CLI resume succeeds,
        # claude_code.py's _build_prompt(resuming=True) discards it and sends only the
        # latest user message. When --resume is dropped (stale/hung/auth), the provider
        # rebuilds the prompt with resuming=False and the history is what preserves
        # transcript continuity across the fallback.
        system_prompt = self._build_system_prompt(agent_id)
        messages = []
        if system_prompt:
            messages.append(Message(role=MessageRole.SYSTEM, content=system_prompt))

        if self.storage:
            if stored.get("summary"):
                messages.append(Message(
                    role=MessageRole.SYSTEM,
                    content=f"[Conversation summary]: {stored['summary']}"
                ))
                history = await self.storage.load_history(session.id, 5)
            else:
                history = await self.storage.load_history(session.id, self._max_history)
            messages.extend(history)

        # Add current user message
        messages.append(Message(role=MessageRole.USER, content=user_content))

        # Persist user message
        if self.storage:
            await self.storage.save_message(session.id, "user", user_content)

        # --- Get tools ---
        tools = self._get_agent_tools(agent_id)
        active_skill = get_skill(message.skill) if message.skill else None
        if active_skill and active_skill.tools:
            if active_skill.restrict_tools:
                # Scoped turn: tools = skill.tools ∩ agent allowlist. A narrow
                # set is what makes small local models reliable — and dispatch
                # re-checks the agent allowlist anyway, so intersection is the
                # only consistent semantics.
                if agent_cfg.get("tools") == "all":
                    # 'all' agents have every registered tool — the skill's own
                    # list IS the scope. (get_tools_for_agent("all") returns [];
                    # intersecting against it would strip everything.)
                    tools = get_tools_for_agent(active_skill.tools)
                else:
                    allowed_names = {t.name for t in tools}
                    dropped = [t for t in active_skill.tools if t not in allowed_names]
                    if dropped:
                        logger.warning(f"Skill {active_skill.name}: tools not in "
                                       f"{agent_id}'s allowlist, dropped: {dropped}")
                    tools = get_tools_for_agent(
                        [t for t in active_skill.tools if t in allowed_names])
            else:
                skill_tools = get_tools_for_agent(active_skill.tools)
                existing_names = {t.name for t in tools}
                for st in skill_tools:
                    if st.name not in existing_names:
                        tools.append(st)

        # --- LLM call + tool dispatch loop ---
        llm = self._get_agent_llm(agent_id)
        project_dir = self._get_project_dir(agent_id)
        llm_cfg = agent_cfg.get("llm", {})
        llm_model = llm_cfg.get("model",
                     self.defaults.get("llm", {}).get("model", "opus"))
        mcp_config = llm_cfg.get("mcp_config") or self.defaults.get("llm", {}).get("mcp_config")
        llm_effort = agent_cfg.get("effort")
        # Runtime overrides (Discord /model, /effort) win over agents.yaml.
        if self.storage:
            overrides = await self.storage.get_agent_overrides(agent_id)
            llm_model = overrides.get("model", llm_model)
            llm_effort = overrides.get("effort", llm_effort)

        # Active usage-limit downgrade wins while it lasts, so this conversation
        # starts on the cheaper model instead of re-hitting the cap every turn.
        if session.model_override and time.time() < session.model_override_until:
            llm_model = session.model_override
        elif session.model_override:
            session.model_override = None  # window elapsed — back to configured model

        # --- Skill provider pin: a skill may declare llm: {provider, model} to
        # run its turns on a specific provider (e.g. a scoped local-model task).
        # Takes precedence over the tier router.
        skill_llm = (active_skill.llm or None) if active_skill else None
        skill_pinned = False
        default_llm, default_model = llm, llm_model  # for fallback on pin failure
        if skill_llm:
            pin_provider = skill_llm.get("provider")
            if pin_provider in self.llm_providers:
                llm = self.llm_providers[pin_provider]
                llm_model = skill_llm.get("model") or llm_model
                skill_pinned = True
                logger.info(f"[{agent_id}] skill '{active_skill.name}' pinned to "
                            f"{pin_provider} ({llm_model or 'default model'})")
                self._bump("skill.pinned")
            else:
                logger.warning(f"Skill {active_skill.name} wants provider "
                               f"'{pin_provider}' — not configured, using default")

        # --- Model-tier routing (opt-in): a tiny local model classifies the
        # request; clearly-simple ones are answered by a local workhorse instead
        # of Claude Code. Quality-first — anything uncertain escalates.
        router_cfg = {**(self.defaults.get("llm", {}).get("router") or {}),
                      **(llm_cfg.get("router") or {})}
        if router_cfg.get("enabled") and not skill_pinned and "local" in self.llm_providers:
            decision = await self._model_router.route(
                message.content, bool(message.attachments), router_cfg, session=session)
            if self._model_router.apply(decision, session):
                llm = self.llm_providers["local"]
                llm_model = router_cfg.get("local_model") or ""
            logger.info(f"[{agent_id}] tier-router → {decision.target} ({decision.reason})")
            self._bump("router.local" if decision.target == "local" else "router.claude")

        # Resolve the provider's registry name for observability (which brain
        # answered — recorded on every assistant message).
        provider_used = next(
            (n for n, p in self.llm_providers.items() if p is llm), "unknown")

        # CLI-backed agents get no system prompt at build time — the CLI loads
        # the identity file itself. If this turn ended up on a different provider
        # (tier-router local turn, skill pin), that assumption is wrong: the
        # model would answer with no idea which agent it is and improvise an
        # identity from the message envelope. Inject the identity now.
        if not system_prompt and not getattr(llm, "reads_project_context", False):
            identity = self._read_identity_prompt(agent_id)
            if identity:
                messages.insert(
                    0, Message(role=MessageRole.SYSTEM, content=identity))

        # Build allowed/disallowed tools for Claude Code CLI
        # allowed_tools: auto-approve these (no permission prompt)
        # disallowed_tools: block these entirely (LLM can't see or call them)
        allowed_tools, disallowed_tools = compute_cli_tool_grants(
            agent_cfg.get("tools", []), active_skill)

        # Block Claude Code built-in tools if configured
        # disallow_builtins: ["Edit", "Write", "Bash", "MultiEdit"] etc.
        blocked_builtins = agent_cfg.get("disallow_builtins", [])
        if blocked_builtins:
            if disallowed_tools is None:
                disallowed_tools = []
            disallowed_tools.extend(blocked_builtins)

        # Agent-created tools are private to their creator until promoted —
        # hide other agents' private tools from this agent.
        from src.core.tool_scope import hidden_tools_for_agent
        hidden = hidden_tools_for_agent(agent_id)
        if hidden:
            if disallowed_tools is None:
                disallowed_tools = []
            hidden_mcp = [f"mcp__kbots-tools__{t}" for t in hidden]
            disallowed_tools.extend(hidden_mcp)
            if allowed_tools:
                allowed_tools = [t for t in allowed_tools if t not in set(hidden_mcp)]

        # --- Per-sender access control: block MCP tools based on who sent the message ---
        if self.access_control:
            is_bot = bool(
                message.raw
                and hasattr(message.raw, "author")
                and getattr(message.raw.author, "bot", False)
            )
            from src.core.tools import get_all_tools
            all_mcp_names = list(get_all_tools().keys())
            sender_blocked = self.access_control.disallowed_tools_for_sender(
                message.user_id, all_mcp_names,
                is_bot=is_bot, agent_id=agent_id,
            )
            if sender_blocked:
                if disallowed_tools is None:
                    disallowed_tools = []
                prefixed = [f"mcp__kbots-tools__{t}" for t in sender_blocked]
                disallowed_tools.extend(prefixed)

            # Also block CLI builtins for non-owner/admin senders
            sender_blocked_builtins = self.access_control.disallowed_builtins_for_sender(
                message.user_id, is_bot=is_bot,
            )
            if sender_blocked_builtins:
                if disallowed_tools is None:
                    disallowed_tools = []
                disallowed_tools.extend(sender_blocked_builtins)

            if sender_blocked or sender_blocked_builtins:
                tier = self.access_control.resolve_tier(message.user_id, is_bot)
                logger.info(
                    f"Access control: sender={message.user_id} tier={tier} "
                    f"agent={agent_id} blocked={len(sender_blocked)} MCP tools, "
                    f"{len(sender_blocked_builtins)} builtins"
                )

        # Refresh the HITL toggle from shared state so a live flip (via
        # /admin hitl or the set_hitl tool in the MCP process) applies now.
        if self.hitl:
            await self.hitl.load_enabled()

        # Live status: as the LLM runs each tool, update the bot's "working"
        # status so the user can follow a long task step by step.
        # In-channel progress: after `after`s of a turn, post one ⏳ message and
        # edit it in place per tool step (throttled) — presence text alone is
        # invisible in the channel, so long turns read as silence without this.
        progress_cfg = self.defaults.get("session", {}).get("progress_message", {})
        progress_state = {"msg": None, "started": time.monotonic(), "last_edit": 0.0}
        progress_after = float(progress_cfg.get("after_seconds", 10))
        progress_interval = float(progress_cfg.get("edit_interval", 3))
        progress_enabled = bool(progress_cfg.get("enabled", True))

        async def _progress(detail: str) -> None:
            if hasattr(connector, "update_task_status"):
                try:
                    await connector.update_task_status(message.bot_account, detail)
                except Exception:
                    pass
            if not progress_enabled or not hasattr(connector, "post_progress"):
                return
            now = time.monotonic()
            elapsed = int(now - progress_state["started"])
            if elapsed < progress_after:
                return
            text = f"⏳ {detail} · {elapsed}s"
            try:
                if progress_state["msg"] is None:
                    progress_state["msg"] = await connector.post_progress(
                        message.channel_id, text, bot_account=message.bot_account)
                    progress_state["last_edit"] = now
                elif now - progress_state["last_edit"] >= progress_interval:
                    await connector.edit_progress(progress_state["msg"], text)
                    progress_state["last_edit"] = now
            except Exception:
                pass

        max_rounds = (active_skill.max_rounds
                      if active_skill and active_skill.max_rounds
                      else self._max_tool_rounds)
        fell_back = False
        async with connector.typing(message.channel_id, bot_account=message.bot_account,
                                     task_detail=message.content):
            response = None
            round_num = 0
            while round_num < max_rounds:
                round_num += 1
                try:
                    response = await llm.complete(
                        messages,
                        tools=tools if tools else None,
                        project_dir=project_dir,
                        model=llm_model,
                        effort=llm_effort,
                        session_id=session.cli_session_id,
                        allowed_tools=allowed_tools,
                        disallowed_tools=disallowed_tools,
                        mcp_config=mcp_config,
                        extra_dirs=agent_cfg.get("extra_dirs"),
                        sandbox_dirs=(self.defaults.get("sandbox", {}) or {}
                                      ).get("additional_dirs"),
                        extra_env=self._subprocess_env(),
                        agent_id=agent_id,
                        channel_id=message.channel_id,
                        user_id=message.user_id,
                        inter_agent_depth=getattr(message, "_inter_agent_depth", 0),
                        proc_callback=lambda p: self._running_procs.__setitem__(agent_id, p),
                        progress_callback=_progress,
                    )
                except Exception as e:
                    logger.error(f"LLM error for {agent_id}: {e}", exc_info=True)
                    if progress_state["msg"] is not None and hasattr(connector, "delete_progress"):
                        with contextlib.suppress(Exception):
                            await connector.delete_progress(progress_state["msg"])
                    # Post the error from THIS agent's own bot (not the default/main
                    # account) so a failed turn is attributed to the right agent.
                    await connector.send(message.channel_id, f"⚠️ Error: {e}",
                                         bot_account=message.bot_account)
                    return
                finally:
                    self._running_procs.pop(agent_id, None)

                # Skill-pinned provider failed (e.g. local runtime down): retry
                # once on the agent's default provider unless fallback: false.
                # Quality-first — a pinned task never silently dies.
                if (response.stop_reason == "error" and skill_pinned and not fell_back
                        and (skill_llm or {}).get("fallback", True)):
                    logger.warning(f"[{agent_id}] pinned provider failed — falling back "
                                   f"to default: {(response.content or '')[:120]}")
                    llm, llm_model = default_llm, default_model
                    fell_back = True
                    self._bump("local.fallback")
                    round_num = 0  # fresh budget for the fallback provider
                    continue

                # No tool calls — we're done
                if not response.tool_calls:
                    break

                # Dispatch tool calls
                tool_results = await self._dispatch_tools(
                    agent_id, session.id, response.tool_calls, connector, message,
                )

                # Add assistant message + tool results to context
                messages.append(Message(
                    role=MessageRole.ASSISTANT,
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                ))
                for tr in tool_results:
                    messages.append(Message(
                        role=MessageRole.TOOL,
                        content=tr["content"],
                        name=tr["name"],
                    ))

            else:
                # Round cap exhausted. Without this, a turn ending on a tool_call
                # round has empty content and the send guard posts NOTHING —
                # a silent failure. Say so instead.
                logger.warning(f"Agent {agent_id} hit max tool rounds ({max_rounds})")
                if response and not (response.content or "").strip():
                    response.content = (f"⚠️ Task didn't converge within {max_rounds} "
                                        f"tool rounds — stopping here. Try rephrasing, "
                                        f"or run it without the round cap.")

        # Local-turn outcome (router-selected turns): success vs error
        if session.routed_local and response:
            self._bump("local.success" if response.stop_reason == "end" else "local.error")

        # --- Save CLI session ID for --resume on next message ---
        if response and response.session_id:
            session.cli_session_id = response.session_id
            if self.storage:
                await self.storage.save_cli_session_id(session.id, response.session_id)
            logger.debug(f"Saved CLI session {response.session_id} for {session.id}")

        # If resume failed (error response), clear the stale session and retry would
        # happen on the next message with fresh context from SQLite
        if response and response.stop_reason == "error" and session.cli_session_id:
            logger.warning(f"CLI session may be stale, clearing for {session.id}")
            session.cli_session_id = None
            if self.storage:
                await self.storage.save_cli_session_id(session.id, "")

        # Skill-pinned non-Claude turns leave the stored CLI session stale (the
        # transcript is missing this turn) — clear it so the next Claude turn
        # rebuilds from SQLite history. Same rule as the tier router's apply().
        if (skill_pinned and not fell_back and response and not response.session_id
                and session.cli_session_id):
            session.cli_session_id = None
            if self.storage:
                await self.storage.save_cli_session_id(session.id, "")

        # Usage cap handling.
        if response and response.usage_downgraded and response.model:
            # Provider fell back to a cheaper model — pin this conversation to it
            # for ~5h (the rolling-limit window) so it doesn't re-hit the cap.
            session.model_override = response.model
            session.model_override_until = time.time() + 5 * 3600
            logger.warning(
                f"Usage downgrade: {agent_id} session {session.id} → {response.model}"
            )
            now = time.time()
            if now - getattr(self, "_last_usage_alert", 0) > 900:
                self._last_usage_alert = now
                if self.alerter:
                    hint = f" (resets {response.reset_hint})" if response.reset_hint else ""
                    self.alerter.send_bg(
                        f"⏳ **Usage limit hit** — switched `{agent_id}` to "
                        f"`{response.model}` to keep conversations going{hint}. "
                        f"It reverts to the configured model automatically."
                    )
        elif response and response.stop_reason == "usage_limit":
            now = time.time()
            if now - getattr(self, "_last_usage_alert", 0) > 900:
                self._last_usage_alert = now
                logger.critical(f"Usage limit reached (agent {agent_id}), no fallback left")
                if self.alerter:
                    hint = f" Resets {response.reset_hint}." if response.reset_hint else ""
                    self.alerter.send_bg(
                        f"🛑 **Claude usage limit reached** — even the cheapest model is "
                        f"capped, agents can't respond until it resets.{hint}"
                    )

        # Claude auth is down — proactively alert ops (deduped) so it's fixed
        # before more messages fail. Auto-refresh already tried and failed.
        if response and response.stop_reason == "auth_error":
            now = time.time()
            if now - getattr(self, "_last_auth_alert", 0) > 900:  # once / 15 min
                self._last_auth_alert = now
                logger.critical(f"Claude auth down (agent {agent_id}) — alerting ops")
                if self.alerter:
                    self.alerter.send_bg(
                        "🔑 **Claude Code auth is down** — agents can't respond.\n"
                        "Try `/admin claude-auth refresh` first. If that fails, run "
                        "`claude setup-token` (or `claude auth login`) on the host and "
                        "authorize in a browser, then `/admin reboot`."
                    )

        # --- Send final response ---
        if (response and response.content and response.stop_reason != "error"
                and is_no_reply(response.content)):
            # Agent chose the abstain sentinel — drop the post, keep the turn in
            # history so the transcript shows the deliberate silence.
            logger.info(f"[{agent_id}] NO_REPLY — suppressing post to {message.channel_id}")
            self._bump("reply.no_reply")
            if self.storage:
                await self.storage.save_message(
                    session.id, "assistant", response.content,
                    tokens_used=response.tokens_used,
                    provider=provider_used, model=response.model or llm_model,
                )
        elif response and response.content:
            # A failed turn's content is an error string — mark it visibly so
            # it never reads like an answer.
            out_content = response.content
            if response.stop_reason == "error" and not out_content.startswith("⚠️"):
                out_content = f"⚠️ {out_content}"
            sent = await connector.send(
                message.channel_id, out_content, reply_to=message.raw,
                bot_account=message.bot_account,
            )
            # If lessons informed this reply, remember them so a 👍/👎 reaction
            # can score them (see feedback_map + the connector's reaction handler).
            sent_id = getattr(sent, "id", None)
            if sent_id and recalled_lessons:
                try:
                    from src.core import feedback_map
                    feedback_map.record(str(sent_id), agent_id, recalled_lessons)
                except Exception as e:
                    logger.debug(f"feedback_map record failed: {e}")
            # Capture the completed turn for training data (opt-in; never raises)
            if self.training_collector:
                self.training_collector.record_turn(
                    agent_id=agent_id, session=session, message=message,
                    user_content=user_content, response=response,
                    project_dir=self._get_project_dir(agent_id),
                    reply_message_id=sent_id, recalled_lessons=recalled_lessons,
                    tools_available=tools,
                )
            # Persist assistant response
            if self.storage:
                await self.storage.save_message(
                    session.id, "assistant", response.content,
                    tokens_used=response.tokens_used,
                    provider=provider_used, model=response.model or llm_model,
                )

        # Turn over — remove the in-channel progress message (any outcome path)
        if progress_state["msg"] is not None and hasattr(connector, "delete_progress"):
            try:
                await connector.delete_progress(progress_state["msg"])
            except Exception:
                pass

        # --- Auto-summarize if conversation is getting long ---
        summarize_after = self.defaults.get("session", {}).get("summarize_after", 30)
        if self.storage and session.message_count > 0 and session.message_count % summarize_after == 0:
            import asyncio
            asyncio.create_task(
                self._auto_summarize(agent_id, session),
                name=f"summarize-{session.id}",
            )

    async def handle_internal_message(
        self, agent_id: str, message: IncomingMessage,
    ) -> str | None:
        """Handle an inter-agent message and return the text response.

        Unlike handle_message(), this does NOT send via connector. It runs the
        full LLM + tool-dispatch loop and returns the final response text.
        Used by ask_agent for synchronous inter-agent communication.
        """
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            return f"Unknown agent: {agent_id}"

        # Use a synthetic channel for inter-agent comms. Serialize on it like
        # handle_message does — two concurrent asks to the same target would
        # otherwise collide on the shared CLI --resume session.
        channel_id = f"internal:{message.user_id}:{agent_id}"
        lock = self._session_locks.setdefault(
            self._session_key(agent_id, channel_id), asyncio.Lock())
        async with lock:
            return await self._handle_internal_message_inner(
                agent_id, agent_cfg, channel_id, message)

    async def _handle_internal_message_inner(
        self, agent_id: str, agent_cfg: dict, channel_id: str,
        message: IncomingMessage,
    ) -> str | None:
        session = self._get_or_create_session(agent_id, channel_id, message.user_id)
        session.message_count += 1

        # Build message context
        system_prompt = self._build_system_prompt(agent_id)
        messages = []
        if system_prompt:
            messages.append(Message(role=MessageRole.SYSTEM, content=system_prompt))

        messages.append(Message(role=MessageRole.USER, content=message.content))

        # Get tools
        tools = self._get_agent_tools(agent_id)

        # LLM call + tool dispatch loop
        llm = self._get_agent_llm(agent_id)
        project_dir = self._get_project_dir(agent_id)
        llm_cfg = agent_cfg.get("llm", {})
        llm_model = llm_cfg.get("model",
                     self.defaults.get("llm", {}).get("model", "opus"))
        mcp_config = llm_cfg.get("mcp_config") or self.defaults.get("llm", {}).get("mcp_config")
        llm_effort = agent_cfg.get("effort")
        if self.storage:
            overrides = await self.storage.get_agent_overrides(agent_id)
            llm_model = overrides.get("model", llm_model)
            llm_effort = overrides.get("effort", llm_effort)

        response = None
        for round_num in range(self._max_tool_rounds):
            try:
                response = await llm.complete(
                    messages,
                    tools=tools if tools else None,
                    project_dir=project_dir,
                    model=llm_model,
                    effort=llm_effort,
                    session_id=session.cli_session_id,
                    mcp_config=mcp_config,
                    extra_env=self._subprocess_env(),
                    agent_id=agent_id,
                    channel_id=channel_id,
                    user_id=message.user_id,
                    inter_agent_depth=getattr(message, "_inter_agent_depth", 0),
                )
            except Exception as e:
                logger.error(f"Internal LLM error for {agent_id}: {e}", exc_info=True)
                return f"Error from {agent_id}: {e}"

            if not response.tool_calls:
                break

            # Build a dummy IncomingMessage for tool dispatch context
            tool_results = await self._dispatch_tools(
                agent_id, session.id, response.tool_calls, None, message,
            )

            messages.append(Message(
                role=MessageRole.ASSISTANT,
                content=response.content or "",
                tool_calls=response.tool_calls,
            ))
            for tr in tool_results:
                messages.append(Message(
                    role=MessageRole.TOOL,
                    content=tr["content"],
                    name=tr["name"],
                ))
        else:
            logger.warning(f"Internal agent {agent_id} hit max tool rounds")

        if response and response.session_id:
            session.cli_session_id = response.session_id

        return response.content if response else None

    async def deliver_inter_agent_message(
        self, target: str, from_agent: str, content: str, depth: int,
    ) -> dict:
        """Deliver a fire-and-forget inter-agent message visibly.

        Routes the message into the target's home channel so the turn runs
        with the target's full conversation context and the reply is posted
        where the owner (and the target's future turns) can see it. A hidden
        side-session turn whose outcome nobody reads is how inter-agent
        messages get "lost" — the target genuinely can't find them later.

        Falls back to the invisible internal side-session only when no home
        channel resolves (e.g. a connector-less target).

        Returns {"delivery": "channel"|"internal", "channel_id": str|None}.
        """
        home = await self._resolve_home_channel(target)
        if home:
            connector_name, channel_id, bot_account = home
            # Post the incoming message to the channel first — the reply the
            # agent produces must not appear out of nowhere to a human reader.
            connector = self.connectors.get(connector_name)
            if connector:
                with contextlib.suppress(Exception):
                    await connector.send(
                        channel_id,
                        f"📨 **{from_agent} → {target}:** {content}",
                        bot_account=bot_account,
                    )
            msg = IncomingMessage(
                connector=connector_name,
                channel_id=channel_id,
                user_id=from_agent,
                user_name=f"agent:{from_agent}",
                content=content,
                bot_account=bot_account,
            )
            msg._inter_agent_depth = depth  # type: ignore[attr-defined]
            msg._inter_agent_sender = from_agent  # type: ignore[attr-defined]
            task = asyncio.create_task(
                self.handle_message(target, msg),
                name=f"inter-agent:{from_agent}->{target}",
            )
            task.add_done_callback(
                lambda t: self._log_inter_agent_error(t, from_agent, target))
            return {"delivery": "channel", "channel_id": channel_id}

        msg = IncomingMessage(
            connector="internal",
            channel_id=f"internal:{from_agent}:{target}",
            user_id=from_agent,
            user_name=f"agent:{from_agent}",
            content=content,
        )
        msg._inter_agent_depth = depth  # type: ignore[attr-defined]
        # Set on the fallback path too: without it a message that lands here
        # because the target has no home channel would still read as a guest.
        msg._inter_agent_sender = from_agent  # type: ignore[attr-defined]
        task = asyncio.create_task(
            self.handle_internal_message(target, msg),
            name=f"inter-agent:{from_agent}->{target}",
        )
        task.add_done_callback(
            lambda t: self._log_inter_agent_error(t, from_agent, target))
        return {"delivery": "internal", "channel_id": None}

    async def _resolve_home_channel(
        self, agent_id: str,
    ) -> tuple[str, str, str | None] | None:
        """Resolve where an agent 'lives': (connector, channel_id, bot_account).

        An explicit `home_channel` in a routing block wins; otherwise the
        first entry of a concrete `channels` list; otherwise (wildcard
        routing) the agent's most recently active real conversation channel.
        None when nothing resolves.
        """
        routing = self.agent_configs.get(agent_id, {}).get("routing", {}) or {}
        live_blocks = [
            (name, block) for name, block in routing.items()
            if name in self.connectors and isinstance(block, dict)
        ]
        for connector_name, block in live_blocks:
            home = block.get("home_channel")
            if home:
                return connector_name, str(home), block.get("account")
            channels = block.get("channels") or []
            if channels:
                return connector_name, str(channels[0]), block.get("account")
        for connector_name, block in live_blocks:
            channel = await self._latest_session_channel(agent_id)
            if channel:
                return connector_name, channel, block.get("account")
        return None

    async def _latest_session_channel(self, agent_id: str) -> str | None:
        """The agent's most recently active non-internal channel, if any."""
        latest: Session | None = None
        for session in self.sessions.values():
            if (session.agent_id == agent_id
                    and not session.channel_id.startswith("internal:")
                    and (latest is None or session.last_active > latest.last_active)):
                latest = session
        if latest:
            return latest.channel_id
        if self.storage:
            return await self.storage.latest_channel_for_agent(agent_id)
        return None

    @staticmethod
    def _log_inter_agent_error(task: asyncio.Task, from_agent: str, target: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Inter-agent dispatch {from_agent} -> {target} failed: {exc}")

    async def _dispatch_tools(
        self, agent_id: str, session_id: str,
        tool_calls: list[dict], connector: Connector,
        message: IncomingMessage,
    ) -> list[dict]:
        """Execute tool calls and return results.

        Each tool gets a fresh ToolContext. Results are returned as
        dicts with 'name' and 'content' keys.
        """
        results = []

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args_raw = tc.get("arguments", "{}")

            # Parse arguments
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw

            # Look up the tool
            tool_def = get_tool(tool_name)
            if not tool_def:
                results.append({
                    "name": tool_name,
                    "content": f"Unknown tool: {tool_name}",
                })
                continue

            # Check tool is in agent's allowlist. "all" is a sentinel STRING,
            # not a list: `tool_name not in "all"` is a substring test against
            # three characters, so it refused every tool an all-tools agent
            # asked for on this path while the CLI/MCP path allowed them.
            agent_tools = self.agent_configs[agent_id].get("tools", [])
            if agent_tools != "all" and tool_name not in agent_tools:
                logger.warning(f"Agent {agent_id} tried to use non-allowed tool: {tool_name}")
                results.append({
                    "name": tool_name,
                    "content": f"Tool '{tool_name}' is not available to this agent.",
                })
                continue

            # --- Rate limiting ---
            if self.rate_limiter:
                rl_check = self.rate_limiter.check(agent_id, tool_name)
                if not rl_check["allowed"]:
                    if self.audit:
                        self.audit.log_rate_limit(
                            agent_id, tool_name,
                            rl_check.get("count", 0), rl_check.get("limit", 0),
                            rl_check.get("window", ""),
                        )
                    results.append({
                        "name": tool_name,
                        "content": rl_check["reason"],
                    })
                    continue

            # --- Access control ---
            hitl_forced = False
            if self.access_control:
                is_bot = bool(
                    message.raw
                    and hasattr(message.raw, "author")
                    and getattr(message.raw.author, "bot", False)
                )
                ac_check = self.access_control.check(
                    message.user_id, tool_name,
                    is_bot=is_bot, agent_id=agent_id,
                )
                if not ac_check["allowed"]:
                    if ac_check.get("gate") == "hitl" and self.hitl:
                        # Route to HITL even if tool isn't in gated_tools
                        hitl_forced = True
                        logger.info(
                            f"Access control: '{tool_name}' from "
                            f"{'bot' if is_bot else 'user'} {message.user_id} "
                            f"→ HITL gate"
                        )
                    else:
                        logger.warning(
                            f"Access denied: {message.user_id} → {tool_name} "
                            f"({ac_check['reason']})"
                        )
                        results.append({
                            "name": tool_name,
                            "content": f"Access denied: {ac_check['reason']}",
                        })
                        continue

            # --- HITL gate (skipped entirely when the runtime toggle is off) ---
            if self.hitl and self.hitl.enabled and (
                self.hitl.is_gated(tool_name) or tool_def.hitl or hitl_forced
            ):
                from src.core.hitl import build_hitl_description
                desc = build_hitl_description(tool_name, args)
                approval = await self.hitl.request_approval(agent_id, tool_name, args, desc)
                if self.audit:
                    self.audit.log_hitl(
                        approval.get("hitl_id", ""), agent_id, tool_name,
                        approval["status"], approval.get("approver"),
                    )
                if approval["status"] != "approved":
                    from src.core.hitl import hitl_result_message
                    results.append({
                        "name": tool_name,
                        "content": hitl_result_message(tool_name, approval),
                    })
                    continue

            # Build context
            memory = self._get_agent_memory(agent_id)
            ctx = ToolContext(
                agent_id=agent_id,
                session_id=session_id,
                channel_id=message.channel_id,
                user_id=message.user_id,
                memory=memory,
                vault=self.vault,
                registry=None,
                connector_send=(lambda ch, content: connector.send(ch, content)) if connector else None,
                agent_manager=self,
                inter_agent_depth=getattr(message, '_inter_agent_depth', 0),
            )

            # Record for rate limiting BEFORE execution (prevents TOCTOU race)
            if self.rate_limiter:
                self.rate_limiter.record(agent_id, tool_name)

            # Execute
            start_time = time.time()
            try:
                result = await tool_def.func(ctx, **args)
                duration_ms = int((time.time() - start_time) * 1000)
                logger.info(f"Tool {tool_name} completed in {duration_ms}ms")
                if self.behavior_monitor:
                    alerts = self.behavior_monitor.record_tool_call(agent_id, tool_name)
                    for alert in alerts:
                        logger.warning(f"Behavior alert: {alert}")
                        if self.audit:
                            self.audit.log_content_safety(
                                agent_id, 0, alert["type"],
                                [alert.get("tool", ""), alert.get("severity", "")],
                            )
                        if self.alerter:
                            self.alerter.send_bg(
                                f"\u26a0\ufe0f **Behavior Alert** [{alert.get('severity', 'MEDIUM')}]\n"
                                f"Agent: `{agent_id}`\n"
                                f"Type: `{alert['type']}`\n"
                                f"Tool: `{alert.get('tool', tool_name)}`"
                            )

                if self.storage:
                    await self.storage.log_tool_call(
                        agent_id, session_id, tool_name, args,
                        str(result)[:1000], True, duration_ms,
                    )
                if self.audit:
                    self.audit.log_tool(
                        agent_id, tool_name, args,
                        str(result)[:200], True, duration_ms,
                    )

                results.append({
                    "name": tool_name,
                    "content": str(result) if result else "(no output)",
                })

            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                logger.error(f"Tool {tool_name} failed: {e}", exc_info=True)

                if self.storage:
                    await self.storage.log_tool_call(
                        agent_id, session_id, tool_name, args,
                        str(e), False, duration_ms,
                    )
                if self.audit:
                    self.audit.log_tool(
                        agent_id, tool_name, args,
                        str(e), False, duration_ms,
                    )

                results.append({
                    "name": tool_name,
                    "content": f"Tool error: {e}",
                })

        return results

    async def _auto_summarize(self, agent_id: str, session: Session) -> None:
        """Generate a conversation summary in the background.

        Called when message count hits summarize_after threshold.
        Uses a lightweight LLM call (haiku) to summarize, then stores it.
        On future context loads without a CLI session, the summary replaces
        bulk message history — keeping token usage bounded.
        """
        if not self.storage:
            return

        try:
            # Load recent history to summarize
            history = await self.storage.load_history(session.id, self._max_history)
            if len(history) < 10:
                return  # not enough to summarize

            # Build the conversation text
            convo_lines = []
            for msg in history:
                prefix = "User" if msg.role == MessageRole.USER else "Assistant"
                content = msg.content[:500]  # truncate long messages for the summary prompt
                convo_lines.append(f"{prefix}: {content}")
            convo_text = "\n".join(convo_lines)

            # Use the agent's LLM to generate a summary (override to haiku for speed/cost)
            llm = self._get_agent_llm(agent_id)
            summary_prompt = (
                "Summarize this conversation concisely. Capture: key topics discussed, "
                "decisions made, important facts shared, any pending tasks or questions. "
                "Be specific — names, numbers, dates, technical details matter. "
                "Keep it under 500 words.\n\n"
                f"Conversation ({len(history)} messages):\n{convo_text}"
            )

            response = await llm.complete(
                [Message(role=MessageRole.USER, content=summary_prompt)],
                project_dir=self._get_project_dir(agent_id),
                model="haiku",  # cheap and fast for summaries
                agent_id=agent_id,
            )

            if response.content and response.stop_reason != "error":
                await self.storage.save_summary(session.id, response.content)
                logger.info(
                    f"Auto-summarized session {session.id} "
                    f"({len(history)} messages → {len(response.content)} chars)"
                )
            else:
                logger.warning(f"Summary generation failed for {session.id}")

        except Exception as e:
            logger.error(f"Auto-summarize error for {session.id}: {e}", exc_info=True)

    async def hold_agent(self, agent_id: str) -> dict:
        """Hold (interrupt) an agent — stop current task but keep session intact.

        Unlike stop_agent, this preserves the CLI session ID so the next message
        resumes the conversation with full context. The user can interject and
        the agent will see both its previous work and the interjection.

        Returns {"held": True/False, "reason": str}.
        """
        proc = self._running_procs.get(agent_id)
        if not proc:
            return {"held": False, "reason": "No running process for this agent."}

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            self._running_procs.pop(agent_id, None)
            # NOTE: we do NOT clear the session or cli_session_id —
            # the next message will --resume with the agent's full context
            return {"held": True,
                    "reason": f"Held {agent_id}. Send your message — "
                              f"the agent will resume with full context."}
        except ProcessLookupError:
            self._running_procs.pop(agent_id, None)
            return {"held": False, "reason": "Process already finished."}

    async def stop_agent(self, agent_id: str) -> dict:
        """Stop the running LLM process for an agent.

        Returns {"stopped": True/False, "reason": str}.
        """
        proc = self._running_procs.get(agent_id)
        if not proc:
            return {"stopped": False, "reason": "No running process for this agent."}

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            self._running_procs.pop(agent_id, None)
            return {"stopped": True, "reason": f"Stopped {agent_id}."}
        except ProcessLookupError:
            self._running_procs.pop(agent_id, None)
            return {"stopped": False, "reason": "Process already finished."}

    def _subprocess_env(self) -> dict[str, str]:
        """Extra env for the CLI subprocess: vault-resolved MCP secrets plus
        the loopback internal API address/token (set by main.py at startup) so
        tools in the MCP subprocess can make inter-agent calls."""
        env = self._mcp_vault_env()
        api = getattr(self, "internal_api", None)
        if api is not None:
            env.update(api.env)
        # GitHub token for agent git/gh — injected only into the agent CLI env
        # here, never the global process env (which non-agent tool subprocesses
        # would inherit).
        gh = self.vault.get("github-token") if self.vault else None
        if gh:
            env["GH_TOKEN"] = gh
            env["GITHUB_TOKEN"] = gh
        return env

    def _mcp_vault_env(self) -> dict[str, str]:
        """Secrets MCP servers need, resolved from the vault at launch
        (mcp.yaml 'vault:<key>' env values → ${VAR} refs in .mcp.json)."""
        if not self.vault:
            return {}
        try:
            from src.core.digest import vault_env_for_servers
            out = {}
            for env_name, vault_key in vault_env_for_servers().items():
                val = self.vault.get(vault_key)
                if val:
                    out[env_name] = val
                else:
                    logger.warning(f"MCP env {env_name}: vault key '{vault_key}' not found")
            return out
        except Exception as e:
            logger.debug(f"MCP vault env resolution failed: {e}")
            return {}

    async def reset_session(self, agent_id: str, channel_id: str) -> dict:
        """Reset the session for an agent in a channel.

        Clears the CLI session ID so next message starts fresh.
        """
        # Find matching session
        for key, session in list(self.sessions.items()):
            if session.agent_id == agent_id and session.channel_id == channel_id:
                old_session_id = session.cli_session_id
                session.cli_session_id = None
                session.message_count = 0
                if self.storage:
                    await self.storage.save_cli_session_id(session.id, "")
                return {
                    "reset": True,
                    "old_session": old_session_id or "none",
                }
        return {"reset": False, "reason": "No session found."}

    async def get_agent_status(self, agent_id: str) -> dict:
        """Get status info for an agent."""
        agent_cfg = self.agent_configs.get(agent_id)
        if not agent_cfg:
            return {"error": f"Unknown agent: {agent_id}"}

        active_sessions = [
            s for s in self.sessions.values()
            if s.agent_id == agent_id
        ]

        return {
            "agent_id": agent_id,
            "display_name": agent_cfg.get("display_name", agent_id),
            "llm_provider": agent_cfg.get("llm", {}).get("provider", "unknown"),
            "llm_model": agent_cfg.get("llm", {}).get("model", "unknown"),
            "active_sessions": len(active_sessions),
            "total_messages": sum(s.message_count for s in active_sessions),
            "tools": agent_cfg.get("tools", []),
        }
