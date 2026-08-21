"""Claude Code CLI as LLM provider.

Spawns Claude Code CLI sessions per agent in their project directory.
Authenticates via the Claude Code CLI's own login (Pro/Max subscription —
no API key needed — or --console API billing). Claude Code reads CLAUDE.md
(a stub importing AGENTS.md)
automatically and is sandboxed to the project dir.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from src.core.base import (
    LLMProvider,
    LLMResponse,
    Message,
    MessageRole,
    ToolDef,
    agent_session_dirs,
)

logger = logging.getLogger(__name__)

# asyncio's default StreamReader line buffer is 64KB; Claude Code emits one JSON
# object per line and a single line can far exceed that (large tool results, base64
# screenshots from chrome_browser). Raise it so readline() doesn't raise LimitOverrunError.
_STREAM_LIMIT = 16 * 1024 * 1024  # 16MB


def session_transcript_path(cwd: "Path | str", session_id: str) -> Path:
    """Path to the Claude Code per-session transcript JSONL.

    Claude Code stores sessions at
    ``$CLAUDE_CONFIG_DIR/projects/<cwd-slugged>/<session_id>.jsonl`` where the slug
    is the cwd with ``/`` replaced by ``-``. This is the complete, faithful record
    of a session (every tool_use + tool_result + assistant text) and is reused by
    the training-data collector to recover the tool-call trace of a turn.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    slug = str(cwd).replace("/", "-")
    return Path(config_dir) / "projects" / slug / f"{session_id}.jsonl"

# Model families high → low, and the next cheaper step for auto-downgrade.
# When a session hits a per-model usage cap (common on Max: Opus is capped
# tighter than Sonnet), we fall back to the next tier to keep the user going.
_DOWNGRADE_NEXT = {"opus": "sonnet", "sonnet": "haiku"}


_TOOL_FRIENDLY = {
    "Read": "reading a file", "Write": "writing a file", "Edit": "editing a file",
    "MultiEdit": "editing files", "Grep": "searching files", "Glob": "finding files",
    "WebSearch": "searching the web", "WebFetch": "fetching a page",
    "TodoWrite": "planning the work", "Task": "working on a sub-task",
}


def _humanize_tool(name: str, inp: dict) -> str:
    """A short human label for a tool_use event, for the live status."""
    if not name:
        return ""
    if name == "Bash":
        detail = (inp.get("description") or "").strip() or (inp.get("command") or "").strip()
        return detail[:50] or "running a command"
    if name.startswith("mcp__"):
        return name.split("__")[-1].replace("_", " ")  # e.g. web_search → "web search"
    return _TOOL_FRIENDLY.get(name, name)


_trusted_dirs: set[str] = set()


def _claude_json_path() -> Path:
    """Path to Claude Code's projects config (.claude.json)."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        return Path(cfg_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def _atomic_write_secret_json(path: Path, data: dict) -> None:
    """Write JSON to `path` via a temp file + rename (mode 0600).

    The rename only needs the (user-owned) parent directory to be writable, so
    this succeeds even when `path` itself is owned by another user — e.g. a Claude
    Code session run as root leaves ~/.claude.json root-owned, which would make a
    plain write EACCES. It also never truncates the file on a concurrent write.
    mkstemp creates the temp at 0600 up front, so the auth secrets in this file
    are never briefly world-readable.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _git_toplevel(path: Path) -> Path | None:
    """Innermost directory at or above `path` that contains a `.git` entry.

    `.git` may be a directory (normal repo) or a file (worktree/submodule);
    `exists()` covers both. Returns None when `path` is not inside a repo.
    """
    for p in (path, *path.parents):
        if (p / ".git").exists():
            return p
    return None


def _ensure_workspace_trusted(cwd: Path) -> None:
    """Mark an agent workspace trusted so its settings.json permissions apply.

    Without this, headless Claude Code ignores the workspace allow-list and
    denies every tool. Verified against ~/.claude.json on EVERY spawn — the CLI
    processes rewrite that file with read-modify-write snapshots, so concurrent
    sessions (other bots, the reflector, an interactive session) can clobber an
    entry written earlier. A per-process "already trusted" cache would then keep
    the workspace untrusted until the next service restart; a disk check heals
    it on the next turn instead. `_trusted_dirs` only tracks what this process
    has trusted before, so a clobber is logged as the anomaly it is.

    The enclosing git toplevel is trusted alongside the cwd: Claude Code
    ≥2.1.232 resolves trust at the repo root, so an agent dir nested inside an
    overlay repo is governed by the root's entry — if that entry is absent or
    False, the session comes up untrusted no matter what the cwd entry says.
    An explicit False at the root (a previously declined dialog) is overwritten,
    same as for the cwd entry.
    """
    keys = [str(cwd)]
    toplevel = _git_toplevel(cwd)
    if toplevel is not None and str(toplevel) != str(cwd):
        keys.append(str(toplevel))
    path = _claude_json_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
        projects = data.setdefault("projects", {})
        stale = [k for k in keys
                 if projects.setdefault(k, {}).get("hasTrustDialogAccepted") is not True]
        if stale:
            for k in stale:
                projects[k]["hasTrustDialogAccepted"] = True
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_secret_json(path, data)
            for k in stale:
                if k in _trusted_dirs:
                    logger.warning(
                        f"Workspace trust for {k} was clobbered (concurrent "
                        f"~/.claude.json rewrite) — re-trusted"
                    )
                else:
                    logger.info(f"Trusted Claude Code workspace: {k}")
        _trusted_dirs.update(keys)
    except PermissionError as e:
        # The atomic write below only needs a writable PARENT dir, so it survives a
        # read-only target — but read_text() above fails outright on a file this user
        # cannot read at all (root-owned 0600, the usual result of running `claude`
        # under sudo against this home dir). Not recoverable here: rewriting blind
        # would destroy credentials and project history we cannot read to preserve.
        # Loud, because the symptom otherwise looks like a mysterious tool denial.
        logger.error(
            f"Cannot mark workspace trusted ({cwd}): {e}. Headless Claude Code ignores an "
            f"untrusted workspace's settings.json, so EVERY TOOL will be denied for this agent "
            f"until this is fixed. {path} is not readable by this service user; refusing to "
            f"rewrite it blind. Fix: sudo chown $(id -un) {path}"
        )
        # Escalate immediately — this failure mode silently strips every agent
        # of every tool, and the log line above is easy to miss for hours.
        from src.core.permission_watch import notify
        cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        claude_dir = Path(cfg_dir) if cfg_dir else Path.home() / ".claude"
        notify("config_unreadable", detail=f"workspace {cwd}: {e}",
               paths=[str(path), str(claude_dir)])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not mark workspace trusted ({cwd}): {e}")


def _model_family(model: str) -> str | None:
    """Return opus/sonnet/haiku if the model name contains one, else None."""
    ml = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in ml:
            return fam
    return None


def _extract_reset_hint(text: str) -> str | None:
    """Best-effort: pull the 'resets at ...' clause from a usage-limit message."""
    m = re.search(r"reset[^.\n]{0,60}", text, re.IGNORECASE)
    return m.group(0).strip() if m else None


def _extra_dir_args(extra_dirs, configured=None) -> list[str]:
    """--add-dir args for every directory this session may reach.

    The shared temp dir and the codex are added for EVERY agent, not only ones
    with an extra_dirs entry: an agent that cannot open the screenshot its own
    tool just wrote is broken by construction, and no per-agent configuration
    should be required to fix that. See base.agent_session_dirs.
    """
    args: list[str] = []
    for d in agent_session_dirs(extra_dirs, configured):
        args.extend(["--add-dir", d])
    return args


def build_cli_prompt(messages: list[Message], resuming: bool = False) -> str:
    """Flatten engine messages into a single prompt for a CLI-backed provider.

    CLI agents hold their own conversation state: when resuming, only the
    latest user message is sent. On a fresh session fed with replayed history
    (session reset, or resume dropped), the discontinuity is made explicit —
    otherwise the model confabulates continuity, claiming past/pending actions
    succeeded (observed live: invented reboots and message relays).

    System messages are excluded — each provider surfaces them its own way
    (flag, identity file, or inline block).
    """
    if resuming:
        for msg in reversed(messages):
            if msg.role == MessageRole.USER:
                return msg.content
        return ""

    parts = []
    if any(m.role == MessageRole.ASSISTANT for m in messages):
        parts.append(
            "<session-note>\n"
            "Fresh session: the conversation below is replayed from the message "
            "store, not from your memory. Treat '[Previous response]' content as a "
            "record that may contain unverified or mistaken claims. Do not state "
            "that any action succeeded unless a tool result in THIS session "
            "confirms it. If you lack a tool or the ability to do something, say "
            "so plainly instead of describing success.\n"
            "</session-note>"
        )
    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            continue
        elif msg.role == MessageRole.USER:
            parts.append(msg.content)
        elif msg.role == MessageRole.ASSISTANT:
            parts.append(f"[Previous response]: {msg.content}")
        elif msg.role == MessageRole.TOOL:
            parts.append(f"[Tool result ({msg.name})]: {msg.content}")
    return "\n\n".join(parts)


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that uses Claude Code CLI.

    Each call spawns `claude --print` in the agent's project directory.
    Claude Code reads CLAUDE.md (stub importing AGENTS.md) for identity.
    """
    name = "claude_code"
    reads_project_context = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._claude_bin = config.get("claude_bin", "claude")
        self._default_model = config.get("model", "sonnet")
        self._timeout = config.get("timeout", 3600)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        """Send messages to Claude Code CLI and return the response.

        kwargs:
            project_dir: str — working directory for the CLI session
            model: str — model override for this call
            session_id: str — resume a previous session
            allowed_tools: list[str] — Claude Code tools to allow
        """
        project_dir = kwargs.get("project_dir", ".")
        model = kwargs.get("model", self._default_model)
        session_id = kwargs.get("session_id")
        allowed_tools = kwargs.get("allowed_tools")
        disallowed_tools = kwargs.get("disallowed_tools")
        effort = kwargs.get("effort")
        agent_id = kwargs.get("agent_id")
        channel_id = kwargs.get("channel_id", "")
        user_id = kwargs.get("user_id", "")
        inter_agent_depth = int(kwargs.get("inter_agent_depth") or 0)
        tag = f"[{agent_id}] " if agent_id else ""

        # Resolve cwd up-front so we can probe the session file before building the prompt
        cwd = Path(project_dir).resolve()
        if not cwd.exists():
            cwd.mkdir(parents=True, exist_ok=True)

        # Claude Code IGNORES an untrusted workspace's .claude/settings.json
        # permissions (there's no trust dialog to accept headless), so the
        # agent's allow-list — Bash, MCP tools, etc. — silently doesn't apply
        # and every tool comes back "not granted". Mark the workspace trusted.
        _ensure_workspace_trusted(cwd)

        # Pre-flight probe: if the CLI has no local state for this session, skip --resume.
        # Stale session_ids that still exist in storage but not on disk cause silent hangs
        # when the CLI tries to resume them.
        if session_id and not self._session_file_exists(cwd, session_id):
            logger.info(
                f"{tag}Session file missing for {session_id} — starting fresh"
            )
            session_id = None

        # Build the prompt from messages
        resuming = session_id is not None
        prompt = self._build_prompt(messages, resuming=resuming)

        # Build CLI args. stream-json emits per-event JSONL (incl. tool_use), so
        # we can report live progress; the final 'result' event carries the same
        # fields the old json format did, so result/error parsing is unchanged.
        args = [self._claude_bin, "--print", "--output-format", "stream-json", "--verbose"]

        # Model — passed straight to the CLI. Aliases ("sonnet"/"opus"/"haiku")
        # are resolved to the current model version by Claude Code itself, so
        # the engine tracks model updates with no code change; an explicit
        # model id (e.g. a pinned "claude-sonnet-4-5-...") passes through as-is.
        args.extend(["--model", model])

        # Effort (thinking level) — accepted values: low, medium, high, xhigh, max
        if effort:
            args.extend(["--effort", effort])

        # Session management
        if session_id:
            args.extend(["--resume", session_id])
            # Don't inject system prompt on resume — session already has it
        else:
            # System prompt — only inject if there's an explicit system message
            # (Claude Code reads the identity files automatically from project_dir)
            system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM and m.content]
            if system_msgs:
                args.extend(["--system-prompt", system_msgs[0].content])

        # MCP config — use explicit path if set, otherwise auto-discover from cwd
        mcp_config = kwargs.get("mcp_config")
        if mcp_config:
            args.extend(["--mcp-config", str(mcp_config)])

        # Working directories beyond the agent dir. Always the shared temp dir
        # and the codex; then anything the deployment or this agent adds.
        args.extend(_extra_dir_args(kwargs.get("extra_dirs"),
                                    kwargs.get("sandbox_dirs")))

        # Allowed tools
        if allowed_tools:
            args.extend(["--allowedTools"] + allowed_tools)

        # Disallowed tools — hide and block these entirely
        if disallowed_tools:
            args.extend(["--disallowedTools"] + disallowed_tools)

        logger.debug(f"{tag}Claude Code: cwd={cwd} model={model} prompt_len={len(prompt)}")

        was_resuming = "--resume" in args
        max_attempts = 4  # refresh+resume, resume-dropped fresh, backoff retry, give up
        proc = None
        refreshed_token = False
        # Track auto-downgrade so we can keep going through usage caps and tell
        # the caller which model actually answered.
        tried_models = {model}
        downgraded = False
        # Tool-permission denials seen in the event stream (see _stream_run).
        denials: list[str] = []

        for attempt in range(1, max_attempts + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=self._build_env(channel_id=channel_id, user_id=user_id,
                                        extra_env=kwargs.get("extra_env"),
                                        inter_agent_depth=inter_agent_depth),
                    limit=_STREAM_LIMIT,
                )

                # Notify caller of running process (for /stop support)
                proc_callback = kwargs.get("proc_callback")
                if proc_callback:
                    proc_callback(proc)

                # Cap resume attempts at 10 min to catch hang-on-stale-resume without
                # killing legitimate long-running turns. Opus high-effort with several
                # tool calls can exceed 5 min of wall-clock. Fresh sessions keep the
                # full configured timeout.
                timeout = kwargs.get("timeout", self._timeout)
                if "--resume" in args:
                    timeout = min(timeout, 600)
                # Stream stdout events (reporting tool progress); stdout_text is
                # the final 'result' event JSON for the parsing/error paths below.
                stdout_text, stderr_text = await asyncio.wait_for(
                    self._stream_run(proc, prompt, kwargs.get("progress_callback"),
                                     denial_sink=denials),
                    timeout=timeout,
                )
                if denials:
                    from src.core.permission_watch import notify
                    notify("tool_denied", agent_id=agent_id or "",
                           detail="; ".join(denials[:3]))
                    denials.clear()

                if proc.returncode != 0:
                    # Try to extract error from JSON stdout (Claude Code sometimes returns errors there)
                    error_detail = ""
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                            if data.get("is_error"):
                                error_detail = data.get("result", "")
                        except json.JSONDecodeError:
                            error_detail = stdout_text[:500]

                    error_msg = stderr_text or error_detail or f"Exit code {proc.returncode}"
                    combined = f"{stderr_text} {error_detail}".lower()
                    is_auth_error = "401" in combined or "authentication" in combined or "not logged in" in combined
                    is_dead_session = "no conversation found" in combined
                    # Usage cap (subscription/rate quota) — distinct from server
                    # overload ("overloaded"/529), which is a transient retry.
                    is_usage_limit = (
                        "usage limit" in combined
                        or "rate limit" in combined
                        or "429" in combined
                        or "quota" in combined
                        or "limit reached" in combined
                        or "reached your" in combined
                        or "usage_limit" in combined
                    )

                    # Dead session — drop --resume immediately, no point retrying
                    if is_dead_session and "--resume" in args:
                        resume_idx = args.index("--resume")
                        dead_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                        args = [a for i, a in enumerate(args)
                                if i != resume_idx and i != resume_idx + 1]
                        prompt = self._build_prompt(messages, resuming=False)
                        logger.warning(
                            f"{tag}Dead CLI session {dead_id} — dropping --resume, starting fresh"
                        )
                        continue

                    if is_auth_error:
                        # Stage 1: Force token refresh on first auth error
                        if not refreshed_token:
                            logger.warning(
                                f"{tag}Auth error (attempt {attempt}/{max_attempts}) — "
                                f"forcing token refresh, retrying..."
                            )
                            await self._force_token_refresh()
                            refreshed_token = True
                            continue

                        # Stage 2: Token refresh didn't help — session is stale, drop --resume
                        if "--resume" in args:
                            resume_idx = args.index("--resume")
                            stale_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                            args = [a for i, a in enumerate(args)
                                    if i != resume_idx and i != resume_idx + 1]
                            prompt = self._build_prompt(messages, resuming=False)
                            logger.warning(
                                f"{tag}Auth error persists after token refresh — "
                                f"dropping stale session {stale_id}, retrying fresh"
                            )
                            continue

                        # Stage 3: No resume involved — genuine auth error, backoff and retry
                        if attempt < max_attempts:
                            logger.warning(
                                f"{tag}Claude auth error (attempt {attempt}/{max_attempts}) — "
                                f"retrying in 5s..."
                            )
                            await asyncio.sleep(5)
                            continue

                        # Stage 4: All retries exhausted
                        logger.critical(
                            f"{tag}Claude auth failed after all retries — token may be expired. "
                            "Fix: run 'claude setup-token' as the kbots service user, then restart."
                        )
                        return LLMResponse(
                            content=(
                                "I'm having trouble connecting right now — my authentication "
                                "needs attention. Try again in a few minutes, and tell an admin "
                                "if it keeps happening."
                            ),
                            # Distinct from generic "error" so the manager can alert
                            # ops and point at the reauth flow.
                            stop_reason="auth_error",
                        )

                    # Usage limit — downgrade to a cheaper model to keep going,
                    # or (if already at the cheapest) surface it distinctly so
                    # the manager can alert ops.
                    if is_usage_limit:
                        fam = _model_family(model)
                        nxt = _DOWNGRADE_NEXT.get(fam) if fam else None
                        if nxt and nxt not in tried_models and "--model" in args:
                            mi = args.index("--model")
                            args[mi + 1] = nxt
                            tried_models.add(nxt)
                            logger.warning(
                                f"{tag}Usage limit on '{model}' — downgrading to "
                                f"'{nxt}' to keep the session going"
                            )
                            model = nxt
                            downgraded = True
                            continue
                        logger.critical(f"{tag}Usage limit reached, no cheaper model available")
                        return LLMResponse(
                            content=(
                                "I've hit the usage limit for now, so I can't respond until "
                                "it resets. An admin has been notified."
                            ),
                            stop_reason="usage_limit",
                            model=model,
                            reset_hint=_extract_reset_hint(f"{stderr_text} {error_detail}"),
                        )

                    # Non-auth CLI error — retry with backoff
                    logger.error(
                        f"{tag}Claude Code failed (attempt {attempt}/{max_attempts}, "
                        f"exit={proc.returncode}): {error_msg}"
                        + (f" | stdout: {stdout_text[:300]}" if stdout_text and not error_detail else "")
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(2 * attempt)
                        continue

                    return LLMResponse(
                        content=(
                            "Sorry, something went wrong on my end. Try sending that "
                            "again — if it keeps failing, tell an admin."
                        ),
                        stop_reason="error",
                    )

                response = self._parse_response(stdout_text)

                # If we downgraded to escape a usage cap, flag it so the manager
                # can keep the session on the cheaper model and alert ops.
                if downgraded:
                    response.usage_downgraded = True
                    if not response.model:
                        response.model = model

                # If we dropped --resume to recover, log the transition
                if was_resuming and "--resume" not in args:
                    logger.info(f"{tag}Recovered from stale session — new session started")

                return response

            except FileNotFoundError:
                logger.error(f"{tag}Claude Code CLI not found at '{self._claude_bin}'")
                return LLMResponse(
                    content="I can't start up right now — there's a system issue. Tell an admin.",
                    stop_reason="error",
                )
            except asyncio.TimeoutError:
                if proc:
                    proc.kill()
                logger.error(
                    f"{tag}Claude Code timed out (attempt {attempt}/{max_attempts}) "
                    f"after {timeout}s"
                )
                # Hang on --resume is the usual cause. Drop the stale session and retry
                # fresh rather than looping on the same doomed args. Rebuild the prompt
                # with resuming=False so SQLite history is injected into the fresh session
                # — otherwise the transcript is lost every time --resume hangs.
                if "--resume" in args:
                    resume_idx = args.index("--resume")
                    hung_id = args[resume_idx + 1] if resume_idx + 1 < len(args) else "unknown"
                    args = [a for i, a in enumerate(args)
                            if i != resume_idx and i != resume_idx + 1]
                    prompt = self._build_prompt(messages, resuming=False)
                    logger.warning(
                        f"{tag}Session --resume {hung_id} hung — dropping, retrying fresh "
                        f"with history replay"
                    )
                    continue
                if attempt < max_attempts:
                    continue
                return LLMResponse(
                    content="That took too long and I had to stop. Try a shorter question, or break it into parts.",
                    stop_reason="timeout",
                )

    def _session_file_exists(self, cwd: Path, session_id: str) -> bool:
        """Check whether Claude CLI has local state for this session.

        Missing file → nothing to resume, so we skip ``--resume`` to avoid silent
        hangs. See ``session_transcript_path`` for the path convention.
        """
        return session_transcript_path(cwd, session_id).exists()

    async def _force_token_refresh(self) -> None:
        """Force the CLI to refresh its OAuth access token.

        Running 'claude auth status' triggers the CLI's internal token
        refresh if the access token is expired but the refresh token is valid.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._claude_bin, "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("Token refresh triggered via 'claude auth status'")
            else:
                logger.warning("Token refresh attempt returned non-zero — may still work")
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.warning(f"Token refresh attempt failed: {e}")

    def _build_prompt(self, messages: list[Message], resuming: bool = False) -> str:
        """Build a prompt string from messages (see build_cli_prompt)."""
        return build_cli_prompt(messages, resuming=resuming)

    async def _stream_run(self, proc, prompt: str, progress_cb,
                          denial_sink: list[str] | None = None) -> tuple[str, str]:
        """Feed the prompt, stream stdout events, report tool progress.

        Returns (result_json, stderr) where result_json is the final 'result'
        event's raw line — so the caller's existing result/error parsing works
        unchanged. Emits progress_cb(label) as each tool starts.

        When `denial_sink` is given, tool-permission denials spotted in the
        event stream ("you haven't granted it yet") are appended to it so the
        caller can escalate — an agent whose own allowed tools bounce means
        the trust/allow-list plumbing is broken, not that a human said no.
        """
        if proc.stdin:
            proc.stdin.write(prompt.encode())
            with contextlib.suppress(Exception):
                await proc.stdin.drain()
            proc.stdin.close()

        result_json = ""
        skipped = 0
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as e:
                # A single stream-json line exceeded the buffer limit even after we
                # raised it. readline() already consumed the offending line, so keep
                # reading rather than aborting the whole turn (which used to dump the
                # raw asyncio error to the user).
                skipped += 1
                logger.warning(f"skipped oversized stream line ({skipped}): {e}")
                if skipped > 50:
                    logger.error("too many oversized stream lines — ending read")
                    break
                continue
            if not line:
                break
            s = line.decode(errors="replace").strip()
            if not s:
                continue
            try:
                event = json.loads(s)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "result":
                result_json = s
            elif etype == "user" and denial_sink is not None:
                from src.core.permission_watch import scan_stream_event
                with contextlib.suppress(Exception):
                    denial_sink.extend(scan_stream_event(event))
            elif etype == "assistant" and progress_cb:
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        label = _humanize_tool(block.get("name", ""), block.get("input", {}))
                        if label:
                            with contextlib.suppress(Exception):
                                await progress_cb(label)

        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        await proc.wait()
        return result_json, stderr_bytes.decode(errors="replace").strip()

    def _parse_response(self, output: str) -> LLMResponse:
        """Parse Claude Code JSON output into LLMResponse."""
        output = output.strip()
        if not output:
            return LLMResponse(content="(empty response)", stop_reason="error")

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text (shouldn't happen with --output-format json)
            return LLMResponse(content=output, stop_reason="end_turn")

        if data.get("is_error"):
            return LLMResponse(
                content=data.get("result", "Unknown error"),
                stop_reason="error",
            )

        # Extract usage info
        usage = data.get("usage", {})
        total_tokens = (
            usage.get("input_tokens", 0) +
            usage.get("output_tokens", 0) +
            usage.get("cache_read_input_tokens", 0)
        )

        return LLMResponse(
            content=data.get("result", ""),
            tokens_used=total_tokens,
            model=data.get("model"),
            stop_reason=data.get("stop_reason", "end_turn"),
            session_id=data.get("session_id"),
        )

    # Env vars safe to pass to Claude Code subprocess.
    # Everything else is stripped to prevent secret leakage.
    _ENV_ALLOWLIST = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "COLORTERM", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "NODE_PATH", "EDITOR",
        "SSH_AUTH_SOCK", "CLAUDE_CONFIG_DIR",
        # pkgx (optional zero-install CLI provider): pass its cache/pantry
        # overrides so agent Bash sessions can `pkgx <tool>` on demand.
        "PKGX_DIR", "PKGX_PANTRY_DIR",
    }

    def _build_env(self, channel_id: str = "", user_id: str = "",
                   extra_env: dict | None = None,
                   inter_agent_depth: int = 0) -> dict[str, str]:
        """Build environment for the CLI subprocess.

        Only passes allowlisted env vars — prevents vault secrets,
        API keys, or other sensitive values from leaking into the subprocess.

        channel_id/user_id are injected as KBOTS_CHANNEL_ID/KBOTS_USER_ID so
        the MCP server (a stdio child of this CLI) can give channel-aware tools
        (schedule_task, triggers, send_message) the conversation context. These
        are non-secret and passed explicitly per call — os.environ is never
        mutated, so concurrent sessions stay isolated.
        """
        env = {k: v for k, v in os.environ.items() if k in self._ENV_ALLOWLIST}
        if extra_env:
            # Vault-resolved secrets for MCP servers (${VAR} refs in .mcp.json).
            env.update(extra_env)
        if channel_id:
            env["KBOTS_CHANNEL_ID"] = channel_id
        if user_id:
            env["KBOTS_USER_ID"] = user_id
        if inter_agent_depth:
            # Threads the inter-agent loop guard into the MCP subprocess so a
            # called agent's own ask_agent/send_to_agent count the hops.
            env["KBOTS_INTER_AGENT_DEPTH"] = str(inter_agent_depth)
        return env
