"""Webhook connector — inbound HTTP events that trigger agent actions.

An external system (a smart-home hub, CI, cron, anything) POSTs to a trigger's
unique URL; the connector re-invokes the bound agent with the stored
instruction. The agent's reply goes out through its normal connector (e.g.
Discord), so this is inbound-only.

Security:
  - Binds to 127.0.0.1 by default.
  - Each trigger has its OWN secret and URL (POST /event/<trigger_id> with the
    X-Webhook-Secret header). A leaked secret can only fire that one trigger.
  - A global killswitch (triggers.set_enabled(False), or `/admin triggers off`)
    instantly stops ALL event-triggered inference — events are accepted but no
    agent runs.
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.core import triggers as trig
from src.core.base import Connector, IncomingMessage, VaultBackend

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024      # reject oversized POST bodies before reading them
_RATE_PER_TRIGGER_PER_MIN = 30   # a single leaked secret can't drive unbounded turns
_RATE_GLOBAL_PER_MIN = 120


class WebhookConnector(Connector):
    """Inbound HTTP listener that dispatches events to agents via triggers."""
    name = "webhook"

    def __init__(self, config: dict, vault: VaultBackend | None = None):
        super().__init__(config, vault)
        self._host = config.get("host", "127.0.0.1")
        self._port = int(config.get("port", 8090))
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Sliding-window fire timestamps for rate limiting (per trigger + global).
        self._fire_log: dict[str, deque] = {}
        self._global_log: deque = deque()

    def _rate_ok(self, trigger_id: str) -> bool:
        """True if firing this trigger now stays under the per-trigger and global
        per-minute caps. Prevents a leaked secret from driving unbounded inference."""
        now = time.monotonic()
        cutoff = now - 60
        g = self._global_log
        while g and g[0] < cutoff:
            g.popleft()
        t = self._fire_log.setdefault(trigger_id, deque())
        while t and t[0] < cutoff:
            t.popleft()
        if len(g) >= _RATE_GLOBAL_PER_MIN or len(t) >= _RATE_PER_TRIGGER_PER_MIN:
            return False
        g.append(now)
        t.append(now)
        return True

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._server = ThreadingHTTPServer((self._host, self._port), self._make_handler())
        except OSError as e:
            logger.error(f"Webhook: cannot bind {self._host}:{self._port}: {e}")
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="webhook")
        self._thread.start()
        logger.info(f"Webhook listening on http://{self._host}:{self._port}/event/<id>")

    async def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    async def send(self, channel_id: str, content: str, **kwargs) -> None:
        # Inbound-only: an agent triggered here replies via its own connector.
        return

    async def _fire(self, trigger: dict, payload: dict) -> bool:
        """Re-invoke the trigger's agent. Returns False if the killswitch is off
        or the agent is unknown."""
        if not trig.is_enabled():
            logger.info(f"Trigger {trigger['id']} suppressed — killswitch is OFF")
            return False
        if not trigger.get("enabled", True):
            logger.info(f"Trigger {trigger['id']} suppressed — this trigger is disabled")
            return False
        if not self._rate_ok(trigger["id"]):
            logger.warning(f"Trigger {trigger['id']} rate-limited (>{_RATE_PER_TRIGGER_PER_MIN}/min)")
            return False
        mgr = getattr(self, "_agent_manager", None)
        if not mgr or trigger["agent_id"] not in getattr(mgr, "agent_configs", {}):
            logger.warning(f"Trigger {trigger['id']} → unknown agent '{trigger.get('agent_id')}'")
            return False

        # Tool-direct action: run the bound tool with its FIXED args — no LLM
        # turn, and the inbound payload never reaches a prompt (kills the
        # payload-injection surface for fixed automations).
        if trigger.get("action"):
            from src.core.actions import run_tool_action
            asyncio.create_task(
                run_tool_action(mgr, trigger, "trigger"),
                name=f"trigger-{trigger['id']}")
            logger.info(f"Trigger {trigger['id']} ('{trigger['event']}') fired action")
            return True

        data = {k: v for k, v in payload.items() if k != "event"}
        content = (f"⚡ Automated trigger '{trigger['event']}' fired.\n\n"
                   f"Do this now: {trigger['instruction']}")
        if data:
            content += f"\n\nEvent data: {json.dumps(data)[:500]}"
        routing = mgr.agent_configs[trigger["agent_id"]].get("routing", {})
        account = (routing.get(trigger["connector"], {}) or {}).get("account")
        skill_params = None
        if trigger.get("skill"):
            skill_params = dict(trigger.get("skill_params") or {})
            if data:
                skill_params.setdefault("event_data", json.dumps(data)[:500])
        msg = IncomingMessage(
            connector=trigger["connector"],
            channel_id=trigger["channel_id"],
            user_id=trigger.get("created_by") or "",
            user_name="automation",
            content=content,
            bot_account=account,
            skill=trigger.get("skill") or None,
            skill_params=skill_params,
        )
        asyncio.create_task(
            mgr.handle_message(trigger["agent_id"], msg), name=f"trigger-{trigger['id']}")
        logger.info(f"Trigger {trigger['id']} ('{trigger['event']}') fired")
        return True

    def _make_handler(self):
        connector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # silence stdlib request logging

            def _json(self, code: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {"service": "kbots webhook", "ok": True})

            def do_POST(self):
                parts = [p for p in self.path.split("?")[0].split("/") if p]
                if len(parts) != 2 or parts[0] != "event":
                    return self._json(404, {"error": "POST /event/<trigger_id>"})
                trigger = trig.get_by_id(parts[1])
                # Same 401 for unknown-id and bad-secret — don't reveal which.
                if not trigger or not trig.verify_secret(
                        trigger, self.headers.get("X-Webhook-Secret", "")):
                    return self._json(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    return self._json(400, {"error": "bad content-length"})
                if length > _MAX_BODY_BYTES:
                    return self._json(413, {"error": "payload too large"})
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
                except json.JSONDecodeError:
                    return self._json(400, {"error": "invalid json"})
                if not isinstance(payload, dict):
                    payload = {}
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        connector._fire(trigger, payload), connector._loop)
                    fired = fut.result(timeout=10)
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                if fired:
                    return self._json(200, {"ok": True, "fired": True})
                return self._json(200, {"ok": True, "fired": False,
                                        "reason": "killswitch off or agent unavailable"})

        return Handler
