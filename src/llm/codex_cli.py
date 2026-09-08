"""Codex CLI provider — OpenAI's coding agent as a kbots LLM engine.

Each call spawns `codex exec --json` in the agent's project directory.
Codex reads AGENTS.md natively for identity/instructions (the same canonical
file Claude Code reaches through its CLAUDE.md stub), so agents keep one
identity across providers. Sessions persist via `codex exec resume <id>`.

MCP: codex has no per-project .mcp.json — servers live in ~/.codex/config.toml.
The agent's scaffolded .mcp.json is translated to `-c mcp_servers.*` config
overrides per invocation, so kbots-tools and mcp.yaml servers work without
touching the user's global codex config.

That translation carries the whole environment of each MCP server, because
codex builds it from the config table alone and does not forward its own env
the way the Claude Code CLI does. Two things therefore have to be added here
rather than assumed: `${VAR}` references in .mcp.json (codex does no
expansion), and the loopback API address/token the engine mints at boot.
Without the latter, every inter-agent tool inside the MCP server answers "no
agent manager available" while looking, from the outside, like a refusal.

Config (under the agent's llm block or defaults.llm):
  provider: codex_cli
  model: gpt-5-codex          # optional — omitted -> codex default
  codex_bin: codex            # optional
  sandbox: workspace-write    # read-only | workspace-write | danger-full-access
  approval_policy: on-request # on-request | never
  approvals_reviewer: auto_review  # user | auto_review
  timeout: 3600               # seconds the turn may run
  resume_startup_timeout: 600 # seconds a RESUMED session may take to come up

Tool grants: the engine's `disallowed_tools` (agent tool list, per-sender
access control, other agents' private tools) reaches codex through
KBOTS_MCP_DENY on the MCP server rather than a CLI flag, because codex has no
per-tool switch. See deny_env.

Limitation: blocked BUILTINS are still not enforced. `disallow_builtins:
[Bash]` is honoured for claude_code via --disallowedTools and has no codex
equivalent — codex's shell is governed by the sandbox, and its execpolicy
rules engine (`codex execpolicy check`) cannot be reached from `codex exec`
in 0.153.4: there is no --rules flag, no config key, no discovered
default.rules at either $CODEX_HOME or <project>/.codex, and the
`request_rule` feature is marked removed. An agent that must not run shell
commands needs `sandbox: read-only`, which also costs it file edits.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path

from src.core.base import LLMProvider, LLMResponse, Message, agent_session_dirs
from src.llm.claude_code import (
    _RESUME_STARTUP_TIMEOUT,
    _STREAM_LIMIT,
    _StartupTimeoutError,
    build_cli_prompt,
)

logger = logging.getLogger(__name__)

_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_APPROVAL_POLICIES = ("on-request", "never")
_APPROVAL_REVIEWERS = ("user", "auto_review")
# kbots effort levels -> codex model_reasoning_effort
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high",
               "xhigh": "xhigh", "max": "xhigh"}


def _toml_str(value: str) -> str:
    """A TOML basic string (json string quoting is valid TOML)."""
    return json.dumps(value)


def _toml_inline_table(d: dict) -> str:
    return "{" + ", ".join(f"{k} = {_toml_str(str(v))}" for k, v in d.items()) + "}"


# ${VAR} / ${VAR:-fallback} — the shell-style refs the scaffolder writes into
# .mcp.json for vault-backed secrets. Claude Code expands them; codex does not.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Vars every kbots MCP server needs and .mcp.json cannot carry, because the
# engine mints them per boot: the loopback internal API. Tools run in the MCP
# subprocess with no handle on the AgentManager and reach it over this address
# (src/tools/builtin.py), so an MCP server that starts without them can read
# and write files all day but cannot talk to another agent.
_LOOPBACK_ENV = ("KBOTS_INTERNAL_API", "KBOTS_INTERNAL_TOKEN")

# Conversation context the engine hands to complete() per call. Claude Code
# puts these in the CLI's own env and the MCP stdio child inherits them; codex
# builds each server's env from the config table alone, so they have to be
# copied in explicitly. Without KBOTS_USER_ID every identity-gated tool
# (agent_config, set_hitl, set_schedule_board) sees ToolContext.user_id=None
# and refuses the owner — indistinguishable, from Discord, from a tier denial.
# Absent by design for scheduler/trigger/agent-to-agent turns: no sender, no
# admin rights, which is the fail-closed behaviour those gates expect.
# KBOTS_MCP_DENY rides the same path: see deny_env below.
_CONTEXT_ENV = ("KBOTS_USER_ID", "KBOTS_MCP_DENY")

# Prefix the engine puts on kbots tool names when it builds CLI grant lists.
_KBOTS_TOOL_PREFIX = "mcp__kbots-tools__"


def deny_env(disallowed_tools) -> dict:
    """KBOTS_MCP_DENY for this turn, from the engine's disallowed_tools list.

    Claude Code enforces that list itself via --disallowedTools. Codex has no
    per-tool switch at all: `mcp_servers.<name>` carries command/args/env and
    nothing else (verified against codex-cli 0.153.4 — an `enabled_tools` key
    is silently dropped), and kbots exposes every tool through the single
    kbots-tools server, so server-level enable/disable cannot express "all of
    these except three". Enforcement therefore moves into the MCP server, which
    codex respawns per `codex exec`, so a per-turn value is safe.

    Builtins in the list (Bash, Edit, ...) are dropped here: they are not MCP
    tools and codex's shell is governed by the sandbox instead. That gap is
    real and unclosed — see the module docstring.
    """
    names = sorted({
        t[len(_KBOTS_TOOL_PREFIX):] for t in (disallowed_tools or [])
        if t.startswith(_KBOTS_TOOL_PREFIX)
    })
    return {"KBOTS_MCP_DENY": ",".join(names)} if names else {}


def _expand_env_refs(value: str, env: dict) -> str:
    """Substitute ${VAR} / ${VAR:-fallback} from env. Unset and empty both
    take the fallback, matching shell semantics and Claude Code's behaviour."""
    return _ENV_REF.sub(
        lambda m: env.get(m.group(1)) or (m.group(2) or ""), str(value))


def mcp_config_args(project_dir: Path, env: dict | None = None) -> list[str]:
    """Translate the agent's .mcp.json into codex `-c mcp_servers.*` overrides.

    Codex stdio servers take command/args/env but no cwd — when .mcp.json
    pins one (kbots-tools runs from the engine root), wrap through /bin/sh.

    `env` is the environment codex itself will run with: ${VAR} refs resolve
    against it and the loopback + conversation-context vars are copied out of
    it, so each server starts with what it needs instead of a bare table.
    """
    env = env or {}
    mcp_file = Path(project_dir) / ".mcp.json"
    if not mcp_file.exists():
        return []
    try:
        servers = json.loads(mcp_file.read_text()).get("mcpServers", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable .mcp.json in {project_dir}: {e}")
        return []

    forwarded = {k: env[k] for k in (*_LOOPBACK_ENV, *_CONTEXT_ENV)
                 if env.get(k)}
    args: list[str] = []
    for name, spec in servers.items():
        command = spec.get("command")
        if not command:
            continue  # url/http servers unsupported in v1
        cmd_args = [str(a) for a in spec.get("args", [])]
        cwd = spec.get("cwd")
        if cwd:
            shell_cmd = "cd " + _sh_quote(cwd) + " && exec " + " ".join(
                _sh_quote(c) for c in [command, *cmd_args])
            command, cmd_args = "/bin/sh", ["-c", shell_cmd]
        args.extend(["-c", f"mcp_servers.{name}.command = {_toml_str(command)}"])
        if cmd_args:
            args.extend(["-c", f"mcp_servers.{name}.args = {json.dumps(cmd_args)}"])
        # An unresolved ${VAR} would otherwise reach the server as its own
        # literal text and fail as a bad credential rather than a missing one.
        server_env = {k: _expand_env_refs(v, env)
                      for k, v in (spec.get("env") or {}).items()}
        server_env.update(forwarded)
        if server_env:
            args.extend(
                ["-c", f"mcp_servers.{name}.env = {_toml_inline_table(server_env)}"])
    return args


def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


class CodexCLIProvider(LLMProvider):
    """LLM provider that spawns the Codex CLI headless per turn."""
    name = "codex_cli"
    # The CLI loads the agent identity (AGENTS.md) from project_dir itself —
    # the engine must not inject it as a system message.
    reads_project_context = True

    # Only allowlisted env vars reach the subprocess — inheriting the whole
    # parent env would hand a prompt-injected Codex session GH_TOKEN, the
    # DISCORD_*/ANTHROPIC_* vars, and any vault-loaded secret. Mirrors
    # claude_code._ENV_ALLOWLIST plus Codex's own config/auth vars. Per-call
    # context (MCP secrets, internal API token) arrives via extra_env instead.
    _ENV_ALLOWLIST = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "COLORTERM", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "NODE_PATH", "EDITOR",
        "SSH_AUTH_SOCK", "PKGX_DIR", "PKGX_PANTRY_DIR",
        # Codex's own config/auth — legitimately its to use, unlike GH/Discord.
        "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
    }

    def __init__(self, config: dict):
        super().__init__(config)
        self._codex_bin = config.get("codex_bin", "codex")
        self._default_model = config.get("model")  # None -> codex default
        self._sandbox = config.get("sandbox", "workspace-write")
        if self._sandbox not in _SANDBOX_MODES:
            raise ValueError(f"Invalid codex sandbox mode: {self._sandbox}")
        # `codex exec` is headless, so an ordinary user approval prompt cannot
        # be answered. Automatic review lets eligible requests proceed while
        # kbots' own tool allowlists, access control and HITL gates remain the
        # authority for what an agent may do.
        self._approval_policy = config.get("approval_policy", "on-request")
        self._approvals_reviewer = config.get(
            "approvals_reviewer", "auto_review")
        self._validate_execution_policy(
            self._sandbox, self._approval_policy, self._approvals_reviewer)
        # Same two deadlines, same defaults, as claude_code. 600 used to be a
        # cap on the whole turn here, which killed turns that were merely long
        # and reported them as a timeout with no partial output. A resumed
        # session still gets a liveness deadline, because only a resume can
        # fail by never coming up at all.
        self._timeout = float(config.get("timeout", 3600))
        self._startup_timeout = float(
            config.get("resume_startup_timeout", _RESUME_STARTUP_TIMEOUT))

    @staticmethod
    def _validate_execution_policy(sandbox, approval_policy, approvals_reviewer) -> None:
        if sandbox not in _SANDBOX_MODES:
            raise ValueError(f"Invalid codex sandbox mode: {sandbox}")
        if approval_policy not in _APPROVAL_POLICIES:
            raise ValueError(f"Invalid codex approval policy: {approval_policy}")
        if approvals_reviewer not in _APPROVAL_REVIEWERS:
            raise ValueError(
                f"Invalid codex approvals reviewer: {approvals_reviewer}")

    async def complete(
        self,
        messages: list[Message],
        tools=None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        project_dir = kwargs.get("project_dir", ".")
        model = kwargs.get("model") or self._default_model
        session_id = kwargs.get("session_id")
        effort = kwargs.get("effort")
        agent_id = kwargs.get("agent_id")
        tag = f"[{agent_id}] " if agent_id else ""
        sandbox = kwargs.get("sandbox") or self._sandbox
        approval_policy = (
            kwargs.get("approval_policy") or self._approval_policy)
        approvals_reviewer = (
            kwargs.get("approvals_reviewer") or self._approvals_reviewer)
        self._validate_execution_policy(
            sandbox, approval_policy, approvals_reviewer)
        additional_dirs = agent_session_dirs(
            kwargs.get("extra_dirs"), kwargs.get("sandbox_dirs"))

        cwd = Path(project_dir).resolve()
        cwd.mkdir(parents=True, exist_ok=True)

        env = {k: v for k, v in os.environ.items() if k in self._ENV_ALLOWLIST}
        env.update(kwargs.get("extra_env") or {})
        # Sender identity, as resolved by the engine from the inbound message —
        # never fabricated here. mcp_config_args copies it into each MCP
        # server's table (_CONTEXT_ENV); os.environ is never mutated, so
        # concurrent sessions cannot see each other's user.
        user_id = kwargs.get("user_id") or ""
        if user_id:
            env["KBOTS_USER_ID"] = str(user_id)
        # Tool grants the engine computed for this turn. Set on codex's own env
        # only so mcp_config_args can copy it into each server's table; codex
        # itself ignores it.
        env.update(deny_env(kwargs.get("disallowed_tools")))

        # One retry: a stale/unknown session id drops resume and starts fresh.
        for resuming in ([True, False] if session_id else [False]):
            prompt = build_cli_prompt(messages, resuming=resuming)
            if not resuming:
                # No --system-prompt equivalent: a fresh session with an
                # explicit system message gets it inlined ahead of the prompt.
                system = next(
                    (m.content for m in messages
                     if getattr(m.role, "value", m.role) == "system" and m.content),
                    None)
                if system:
                    prompt = f"<system>\n{system}\n</system>\n\n{prompt}"
            args = self._build_args(
                cwd, model, effort, session_id if resuming else None, prompt,
                env=env,
                sandbox=sandbox,
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
                additional_dirs=additional_dirs,
            )
            logger.debug(f"{tag}codex exec: cwd={cwd} model={model} "
                         f"resume={resuming} prompt_len={len(prompt)}")
            # `timeout` asks how long the turn may run and is the same either
            # way; `startup` asks whether the CLI came up, and only a resume
            # can fail that way.
            result = await self._run(
                args, cwd, env, tag,
                timeout=float(kwargs.get("timeout") or self._timeout),
                startup=self._startup_timeout if resuming else None)
            if result is not None:
                return result
            if resuming:
                logger.warning(
                    f"{tag}codex resume {session_id} failed — starting fresh")
        raise RuntimeError("codex exec failed (see logs for stderr)")

    def _build_args(
        self,
        cwd: Path,
        model,
        effort,
        session_id,
        prompt,
        *,
        env: dict | None = None,
        sandbox,
        approval_policy,
        approvals_reviewer,
        additional_dirs,
    ) -> list[str]:
        args = [self._codex_bin, "exec", "--json", "--skip-git-repo-check",
                "-s", sandbox,
                "-c", f"approval_policy = {_toml_str(approval_policy)}",
                "-c", f"approvals_reviewer = {_toml_str(approvals_reviewer)}"]
        for directory in additional_dirs:
            args.extend(["--add-dir", directory])
        if model:
            args.extend(["-m", str(model)])
        mapped = _EFFORT_MAP.get(effort or "")
        if mapped:
            args.extend(["-c", f"model_reasoning_effort = {_toml_str(mapped)}"])
        args.extend(mcp_config_args(cwd, env))
        if session_id:
            args.extend(["resume", session_id])
        args.append(prompt if prompt else "Continue.")
        return args

    async def _run(self, args, cwd, env, tag, timeout: float,
                   startup: float | None) -> LLMResponse | None:
        """One codex exec invocation. None = retriable failure (resume drop)."""
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        # stdin is DEVNULL rather than inherited. `codex exec` announces
        # "Reading additional input from stdin..." on every run, so an inherited
        # stdin that never reaches EOF would block the whole turn until the
        # deadline. It happens to be /dev/null under launchd; that is luck.
        started = asyncio.Event()
        run_task = asyncio.ensure_future(self._collect(proc, started))
        try:
            if startup is not None:
                await self._await_startup(started, run_task, startup)
            stdout, stderr = await asyncio.wait_for(run_task, timeout=timeout)
        except _StartupTimeoutError:
            proc.kill()
            logger.warning(f"{tag}codex resume produced no event in "
                           f"{startup:.0f}s — dropping resume")
            return None          # retriable: complete() falls back to fresh
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"codex exec timed out after {timeout:.0f}s")
        finally:
            if not run_task.done():
                run_task.cancel()
                with contextlib.suppress(BaseException):
                    await run_task

        thread_id, content, tokens = self._parse_events(stdout, tag)
        if proc.returncode != 0 or content is None:
            err = (stderr or b"").decode(errors="replace").strip()
            if "login" in err.lower() or "auth" in err.lower():
                raise RuntimeError(
                    f"codex auth error — run `codex login` on the host: {err[:300]}")
            logger.warning(
                f"{tag}codex exec rc={proc.returncode}, "
                f"content={'yes' if content else 'no'}: {err[:300]}")
            return None
        return LLMResponse(
            content=content,
            tokens_used=tokens,
            model=str(args[args.index('-m') + 1]) if '-m' in args else "codex-default",
            stop_reason="stop",
            session_id=thread_id,
        )

    @staticmethod
    async def _collect(proc, started: asyncio.Event) -> tuple[bytes, bytes]:
        """Read the process to completion, flagging the first JSONL event.

        communicate() gives no signal until the process exits, so there was no
        way to tell "the CLI never came up" from "the turn is long". Reading
        line by line costs nothing and makes the first event observable.
        """
        out: list[bytes] = []

        async def _stdout() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                out.append(line)
                if not started.is_set() and line.lstrip().startswith(b"{"):
                    started.set()

        err_task = asyncio.ensure_future(proc.stderr.read())
        await _stdout()
        stderr = await err_task
        await proc.wait()
        return b"".join(out), stderr

    @staticmethod
    async def _await_startup(started: asyncio.Event, run_task,
                             timeout: float) -> None:
        """Wait for the first event, or for the run to end on its own.

        A process that exits early (bad flag, auth failure) must not sit here
        until the liveness deadline: its own completion is the answer.
        """
        waiter = asyncio.ensure_future(started.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, run_task}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise _StartupTimeoutError(int(timeout))
        finally:
            if not waiter.done():
                waiter.cancel()

    @staticmethod
    def _parse_events(stdout: bytes, tag: str) -> tuple[str | None, str | None, int | None]:
        """(thread_id, last agent message, total tokens) from JSONL events."""
        thread_id = content = None
        tokens = None
        for line in (stdout or b"").decode(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    content = item["text"]
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                tokens = (usage.get("input_tokens", 0) or 0) + \
                         (usage.get("output_tokens", 0) or 0)
            elif etype in ("turn.failed", "error"):
                logger.warning(f"{tag}codex event {etype}: "
                               f"{json.dumps(event)[:300]}")
        return thread_id, content, tokens
