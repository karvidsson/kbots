"""Loopback internal API — lets MCP-subprocess tools reach the AgentManager.

Tools run inside the MCP server, a stdio child of each agent's Claude Code
CLI — a separate process with no handle on the AgentManager, which is why
ask_agent/send_to_agent used to fail there with "No agent manager available".
This server runs in the MAIN process, bound to loopback only, and carries
inter-agent calls back across the process boundary.

Auth is a per-boot bearer token handed to the CLI subprocess via env
(never written to disk, never in .mcp.json). The inter-agent depth guard is
enforced on both sides: the client reads its own depth from
KBOTS_INTER_AGENT_DEPTH (threaded into the target's subprocess env by the
claude_code provider), and the server re-checks it before dispatching.
"""

import functools
import hmac
import json
import logging
import secrets

from aiohttp import web

from src.core.base import IncomingMessage
from src.tools.builtin import MAX_INTER_AGENT_DEPTH

logger = logging.getLogger(__name__)

# Graph values (e.g. query rows) can contain non-JSON types — stringify them
# rather than 500 on serialization.
_json_dumps = functools.partial(json.dumps, default=str)

# GraphMemory methods the /graph route may dispatch — everything else is 400.
_GRAPH_METHODS = frozenset({"link", "related", "unlink", "entities", "export",
                            "find", "history"})


class InternalAPI:
    """Loopback HTTP server for inter-agent communication."""

    def __init__(self, agent_manager, config: dict | None = None):
        cfg = config or {}
        self.mgr = agent_manager
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 0))  # 0 = ephemeral, resolved on start
        self.token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None

    @property
    def env(self) -> dict[str, str]:
        """Env vars a tool subprocess needs to reach this API."""
        if not self._runner:
            return {}
        return {
            "KBOTS_INTERNAL_API": f"http://{self.host}:{self.port}",
            "KBOTS_INTERNAL_TOKEN": self.token,
        }

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/agent-message", self._handle_agent_message)
        app.router.add_post("/graph", self._handle_graph)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:
            self.port = self._runner.addresses[0][1]
        logger.info(f"Internal API listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def _authed(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)

    async def _handle_graph(self, request: web.Request) -> web.Response:
        """Execute a graph-memory call for a tool subprocess (GraphClient).

        The .lbdb is single-writer, owned by this process — MCP-subprocess
        tools forward graph calls here instead of opening the file themselves.
        """
        if not self._authed(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        method = str(body.get("method", ""))
        kwargs = body.get("kwargs") or {}
        if method not in _GRAPH_METHODS or not isinstance(kwargs, dict):
            return web.json_response(
                {"error": f"unknown graph method {method!r}", "kind": "value"}, status=400)

        from src.lib.graph_store import GraphUnavailableError, get_graph
        try:
            result = await getattr(get_graph(), method)(**kwargs)
        except GraphUnavailableError as e:
            return web.json_response({"error": str(e), "kind": "unavailable"}, status=503)
        except (TypeError, ValueError) as e:
            return web.json_response({"error": str(e), "kind": "value"}, status=400)
        except Exception as e:
            logger.exception(f"graph call {method} failed")
            return web.json_response(
                {"error": f"Graph call {method} failed: {e}", "kind": "unavailable"},
                status=500)
        return web.json_response({"result": result}, dumps=_json_dumps)

    async def _handle_agent_message(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON body"}, status=400)

        target = str(body.get("target", ""))
        content = str(body.get("content", ""))
        from_agent = str(body.get("from_agent", ""))
        wait = bool(body.get("wait", True))
        try:
            depth = int(body.get("depth", 0) or 0)
        except (TypeError, ValueError):
            depth = 0

        if not target or not content or not from_agent:
            return web.json_response(
                {"error": "target, content and from_agent are required"}, status=400)
        if depth >= MAX_INTER_AGENT_DEPTH:
            return web.json_response(
                {"error": f"inter-agent depth limit reached ({MAX_INTER_AGENT_DEPTH})"},
                status=429)
        if target == from_agent:
            return web.json_response({"error": "cannot message yourself"}, status=400)
        if target not in self.mgr.agent_configs:
            return web.json_response(
                {"error": f"unknown agent '{target}'",
                 "available": sorted(self.mgr.agent_configs)}, status=404)

        logger.info(f"Inter-agent {'ask' if wait else 'send'}: {from_agent} -> {target} "
                    f"(depth {depth + 1}/{MAX_INTER_AGENT_DEPTH})")

        if wait:
            msg = IncomingMessage(
                connector="internal",
                channel_id=f"internal:{from_agent}:{target}",
                user_id=from_agent,
                user_name=f"agent:{from_agent}",
                content=content,
            )
            msg._inter_agent_depth = depth + 1  # type: ignore[attr-defined]
            # NOTE ON TRUST: unlike the send_to_agent/ask_agent tools, where the
            # sender is the caller's own ctx.agent_id, from_agent here is taken
            # from the request body. It is therefore only as trustworthy as the
            # internal API token — anything holding that token can claim to be
            # any agent. Good enough to render a teammate; not a basis for
            # granting authority, which is why the receiving context marks a
            # relayed approval as a claim rather than an instruction.
            msg._inter_agent_sender = from_agent  # type: ignore[attr-defined]
            response = await self.mgr.handle_internal_message(target, msg)
            return web.json_response({"response": response or ""})

        # Fire-and-forget: deliver into the target's home channel so the turn
        # runs with full channel context and the reply is visible — a hidden
        # side-session whose outcome nobody reads is how sends get "lost".
        result = await self.mgr.deliver_inter_agent_message(
            target, from_agent, content, depth + 1)
        return web.json_response({"status": "dispatched", **result})
