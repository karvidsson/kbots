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

Blocked builtins (`disallow_builtins: [Bash]`) are enforced through codex's
PreToolUse hook, which refuses the call and tells the model why. Codex names
its shell tool "Bash", the same name kbots already uses, so the config maps
across untranslated. See write_hook_config.

Not the execpolicy engine, which looked like the obvious fit and is not
reachable: `codex exec` in 0.153.4 has no --rules flag, no config key accepts
a rules path, no default.rules is discovered at $CODEX_HOME or
<project>/.codex (both tested against a live run), and `codex features list`
reports `request_rule` as removed.
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

# `approvals_reviewer = auto_review` puts a reviewer model in front of every
# escalated action. When it refuses, codex prints the verdict to stderr as a
# Rust debug string and the tool call returns status "declined":
#
#   ERROR codex_core::tools::router: error=exec_command failed: CreateProcess {
#     message: "Rejected(\"This action was rejected due to unacceptable risk.
#     \nReason: <why>\nDo not bypass ...\")" }
#
# A refusal is a decision with a stated reason, not a crash, and that reason is
# the only thing telling the owner what to authorise instead. It sits well past
# the first 300 characters of stderr, so it has to be dug out rather than
# truncated into.
_REJECTED = re.compile(r"Rejected\(\\?\"(.+?)\\?\"\)", re.DOTALL)
_REASON_LINE = re.compile(r"Reason:\s*(.+?)(?:\\n|\n|$)", re.DOTALL)


# Phrases that mean "this host cannot talk to the API", as opposed to any
# sentence containing the letters a-u-t-h.
_AUTH_MARKERS = (
    "codex login", "not logged in", "please log in", "please login",
    "authentication failed", "401 unauthorized", "invalid api key",
    "missing api key", "no credentials", "auth.json",
)


def _looks_like_auth_error(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(m in low for m in _AUTH_MARKERS)


def refusal_reason(stderr: str) -> str | None:
    """The auto-review reviewer's stated reason, if it refused something.

    Returns just the reason, not the boilerplate around it: the rest of the
    Rejected() payload is instruction addressed to the codex model ("do not
    bypass this", "continue with a safer alternative") and repeating it to the
    owner reads as the agent lecturing them about their own request.
    """
    hit = _REJECTED.search(stderr or "")
    if not hit:
        return None
    body = hit.group(1).replace("\\n", "\n")
    reason = _REASON_LINE.search(body)
    text = (reason.group(1) if reason else body.split("\n")[0]).strip()
    return text or None


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

    Builtins in the list (Bash, Edit, ...) are not MCP tools and are dropped
    here; write_hook_config enforces those instead.
    """
    names = sorted({
        t[len(_KBOTS_TOOL_PREFIX):] for t in (disallowed_tools or [])
        if t.startswith(_KBOTS_TOOL_PREFIX)
    })
    return {"KBOTS_MCP_DENY": ",".join(names)} if names else {}


def denied_builtins(disallowed_tools) -> list[str]:
    """The non-MCP names in the engine's disallowed_tools list.

    Everything with an mcp__ prefix is a tool the MCP server withholds
    (deny_env); what is left is a CLI builtin, which is the hook's business.
    """
    return sorted({
        t for t in (disallowed_tools or [])
        if t and not t.startswith("mcp__")
    })


def write_hook_config(project_dir: Path, denied: list[str]) -> bool:
    """Write <project_dir>/.codex/hooks.json denying `denied`. True if written.

    Codex's PreToolUse hook is the only way to refuse a builtin without taking
    the sandbox to read-only, which would also cost the agent file edits. The
    hook receives {"tool_name": "Bash", "tool_input": {...}} and returns a
    deny decision; codex reports the refusal to the model as text, so it can
    say what it cannot do instead of failing opaquely.

    Three properties of codex 0.153.4 this depends on, each verified live:

    * `<cwd>/.codex/hooks.json` is the ONLY discovery path. `hooks.managed_dir`
      is a real config key and does not load a hooks.json placed there, and
      neither does $CODEX_HOME. So the file has to go in the agent's own
      project dir.
    * `"enabled": true` per entry is required. Without it the hook is parsed
      and never runs, silently.
    * an untrusted hook is skipped, also silently, so the run needs
      --dangerously-bypass-hook-trust.

    That flag is why this function OVERWRITES rather than merges, and why the
    caller must only pass the flag when this returned True. The flag trusts
    every hook codex finds, and the file lives in a directory the agent can
    write to, so a prompt-injected agent could otherwise leave a hook here and
    have the next turn run it. Rewriting the file immediately before spawn
    destroys anything the agent put there; with nothing to deny we delete it
    and pass no flag, so an agent-authored hook stays untrusted and inert.
    """
    hooks_dir = Path(project_dir) / ".codex"
    hooks_file = hooks_dir / "hooks.json"
    if not denied:
        # Not "leave it alone": a file from an earlier turn would still be
        # here, and this turn has no flag to make it run, but the next turn
        # with denials would trust whatever it contains.
        hooks_file.unlink(missing_ok=True)
        return False

    script = Path(__file__).with_name("codex_hook_deny.py")
    config = {
        "hooks": {
            "PreToolUse": [{
                # The script filters by name, so one entry covers every
                # denied tool and the command string stays identical across
                # turns and agents.
                "matcher": "*",
                "enabled": True,
                "hooks": [{
                    "type": "command",
                    # A bare executable path, NOT "<python> <script>": codex
                    # runs this string as one program and does not split it on
                    # spaces, so an interpreter prefix makes the hook fail to
                    # start, which it reports nowhere and which fails open.
                    # The script carries its own shebang and imports only the
                    # stdlib, so the interpreter on PATH is enough.
                    #
                    # No "env" key here. Codex 0.153.4 accepts one and does not
                    # apply it: the hook process inherits codex's environment
                    # instead. The deny list therefore travels in the env this
                    # provider hands the codex subprocess (KBOTS_DENIED_TOOLS,
                    # set in complete()), which the hook does receive. Putting
                    # it here instead looks right, is accepted silently, and
                    # leaves the hook allowing everything.
                    "command": str(script),
                }],
            }]
        }
    }
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(config, indent=2) + "\n")
    return True


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
        # Builtins are refused by a PreToolUse hook instead, which has to be on
        # disk before codex starts. Rewritten (or removed) every turn, so it
        # always states this turn's grants and never an earlier turn's.
        builtins = denied_builtins(kwargs.get("disallowed_tools"))
        hooks_armed = write_hook_config(cwd, builtins)
        if hooks_armed:
            # The hook reads this from the environment it inherits from codex.
            env["KBOTS_DENIED_TOOLS"] = ",".join(builtins)
            logger.debug(f"{tag}codex hook denies: {', '.join(builtins)}")

        # Why the last attempt failed, filled in by _run. A list because the
        # loop may run twice and only the final attempt's cause is the one to
        # report; a per-call local keeps concurrent agents independent.
        why: list[str] = []
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
                hooks_armed=hooks_armed,
            )
            logger.debug(f"{tag}codex exec: cwd={cwd} model={model} "
                         f"resume={resuming} prompt_len={len(prompt)}")
            # `timeout` asks how long the turn may run and is the same either
            # way; `startup` asks whether the CLI came up, and only a resume
            # can fail that way.
            result = await self._run(
                args, cwd, env, tag,
                timeout=float(kwargs.get("timeout") or self._timeout),
                startup=self._startup_timeout if resuming else None,
                why=why)
            if result is not None:
                return result
            if resuming:
                logger.warning(
                    f"{tag}codex resume {session_id} failed — starting fresh")
        # Carry the cause into the message the owner sees. "see logs for stderr"
        # made every distinct failure look identical in Discord.
        raise RuntimeError(f"codex exec failed: {why[-1]}" if why
                           else "codex exec failed (see logs for stderr)")

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
        hooks_armed: bool = False,
    ) -> list[str]:
        args = [self._codex_bin, "exec", "--json", "--skip-git-repo-check",
                "-s", sandbox,
                "-c", f"approval_policy = {_toml_str(approval_policy)}",
                "-c", f"approvals_reviewer = {_toml_str(approvals_reviewer)}"]
        if hooks_armed:
            # Codex skips an untrusted hook silently, which for a deny hook
            # means failing open. Only ever passed for the file we just wrote
            # ourselves; see write_hook_config for why that bounds the flag.
            args.append("--dangerously-bypass-hook-trust")
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
                   startup: float | None,
                   why: list[str] | None = None) -> LLMResponse | None:
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
        model = str(args[args.index('-m') + 1]) if '-m' in args else "codex-default"
        if proc.returncode != 0 or content is None:
            err = (stderr or b"").decode(errors="replace").strip()
            refusal = refusal_reason(err)
            # Refusal first, and only then auth. The old test was `"auth" in
            # err`, which matches "authorization" and "authorized" — both of
            # which appear in ordinary approval-review verdicts. A refusal was
            # therefore reported as an auth failure telling the owner to run
            # `codex login`, which is neither the problem nor a fix for it.
            if refusal is None and _looks_like_auth_error(err):
                raise RuntimeError(
                    f"codex auth error — run `codex login` on the host: {err[:300]}")
            if content:
                # A non-zero exit does NOT mean the turn produced nothing. An
                # auto-review refusal, or an abort after several of them, ends
                # the process non-zero having already written a real reply that
                # usually explains the situation. Discarding it cost the owner
                # the answer AND the reason, and left "codex exec failed" as the
                # only visible symptom; on a resume it also spent a second full
                # turn redoing work that would be refused identically.
                logger.warning(
                    f"{tag}codex exec rc={proc.returncode} but the turn "
                    f"produced a reply — returning it"
                    + (f" (blocked: {refusal})" if refusal else ""))
                return LLMResponse(
                    content=content + (
                        f"\n\n(Blocked by codex automatic approval review: "
                        f"{refusal})" if refusal else ""),
                    tokens_used=tokens,
                    model=model,
                    # Not "error": that clears the CLI session id, and the codex
                    # thread is intact and resumable. Not "stop" either, so the
                    # reflector does not mine a cut-short turn for lessons.
                    stop_reason="aborted",
                    session_id=thread_id,
                )
            cause = (f"blocked by automatic approval review: {refusal}"
                     if refusal else (err[-400:] or "no output and no stderr"))
            logger.warning(
                f"{tag}codex exec rc={proc.returncode}, no content: {cause}")
            if why is not None:
                why.append(cause)
            return None
        return LLMResponse(
            content=content,
            tokens_used=tokens,
            model=model,
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
