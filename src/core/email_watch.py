"""Email watcher — wake an agent when new mail lands in its Gmail inbox.

Per-agent opt-in via agents.yaml:

    email_watch:
      enabled: true
      interval: 60                 # seconds between checks (min 15)
      google_account: pixel-fox   # vault identity ('' = install default)
      connector: discord
      channel: "123456789"         # where the agent session runs / replies
      instruction: ""              # optional override for the wake-up prompt

Polls the Gmail history API (2 quota units per check — negligible) instead of
Pub/Sub push: push needs a publicly reachable HTTPS endpoint plus watch
renewals every 7 days, which contradicts the zero-infrastructure design. At a
60s interval the agent reacts within a minute of mail arriving.

The last-seen historyId is persisted per agent in data/email_watch.json so a
restart doesn't re-announce old mail; an expired/invalid historyId resets the
baseline to "now" (anything that arrived while the service was down is picked
up by the agent's own scheduled sweeps, not re-fired here).
"""

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from src.auth.oauth2 import OAuth2AuthRevokedError
from src.core.base import IncomingMessage

logger = logging.getLogger(__name__)

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
MIN_INTERVAL = 15
_MAX_ANNOUNCE = 5  # cap per-tick detail; more than this is summarized


class EmailWatcher:
    def __init__(self, agent_manager, vault, data_dir: str):
        self.agent_manager = agent_manager
        self.vault = vault
        self._state_path = Path(data_dir) / "email_watch.json"
        self._auths: dict[str, object] = {}

    @property
    def watched_agents(self) -> dict[str, dict]:
        out = {}
        for agent_id, cfg in getattr(self.agent_manager, "agent_configs", {}).items():
            ew = cfg.get("email_watch") or {}
            if ew.get("enabled") and ew.get("channel"):
                out[agent_id] = ew
        return out

    # --- state -----------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state))
        except OSError as e:
            logger.warning(f"email-watch: could not persist state: {e}")

    # --- gmail ----------------------------------------------------------

    def _auth(self, account: str):
        if account not in self._auths:
            from src.auth.oauth2 import GoogleAuth
            self._auths[account] = GoogleAuth(self.vault, account=account)
        return self._auths[account]

    async def _api_get(self, account: str, url: str) -> dict:
        headers = await self._auth(account).get_headers()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                body = await resp.json()
                if resp.status != 200:
                    return {"error": True, "status": resp.status, "detail": str(body)[:200]}
                return body

    async def _baseline(self, account: str) -> str:
        profile = await self._api_get(account, f"{GMAIL}/profile")
        return str(profile.get("historyId", "")) if not profile.get("error") else ""

    async def _new_inbox_messages(self, account: str, history_id: str) -> tuple[list[str], str]:
        """Message ids added to INBOX since history_id, plus the new historyId.

        Returns ([], "") when the historyId is expired/invalid — caller resets.
        """
        url = (f"{GMAIL}/history?startHistoryId={history_id}"
               f"&historyTypes=messageAdded&labelId=INBOX&maxResults=100")
        result = await self._api_get(account, url)
        if result.get("error"):
            if result.get("status") in (400, 404):  # expired/invalid historyId
                return [], ""
            raise RuntimeError(f"gmail history failed: {result.get('detail')}")

        ids: list[str] = []
        for h in result.get("history", []):
            for added in h.get("messagesAdded", []):
                m = added.get("message", {})
                if "INBOX" in m.get("labelIds", []) and m.get("id") not in ids:
                    ids.append(m["id"])
        return ids, str(result.get("historyId", history_id))

    async def _summarize(self, account: str, msg_ids: list[str]) -> str:
        lines = []
        for mid in msg_ids[:_MAX_ANNOUNCE]:
            meta = await self._api_get(
                account,
                f"{GMAIL}/messages/{mid}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=Subject")
            if meta.get("error"):
                lines.append(f"- (id {mid})")
                continue
            hdrs = {h["name"]: h["value"]
                    for h in meta.get("payload", {}).get("headers", [])}
            lines.append(f"- from {hdrs.get('From', '?')} — "
                         f"\"{hdrs.get('Subject', '(no subject)')}\" (id {mid})")
        if len(msg_ids) > _MAX_ANNOUNCE:
            lines.append(f"- …and {len(msg_ids) - _MAX_ANNOUNCE} more")
        return "\n".join(lines)

    # --- delivery -------------------------------------------------------

    async def _fire(self, agent_id: str, cfg: dict, summary: str, count: int) -> None:
        mgr = self.agent_manager
        connector = cfg.get("connector", "discord")
        routing = mgr.agent_configs[agent_id].get("routing", {})
        account = (routing.get(connector, {}) or {}).get("account")
        instruction = cfg.get("instruction") or (
            "Read each one and handle it appropriately — reply if it needs a "
            "reply (send_email is approval-gated), otherwise summarize what "
            "arrived and what you did.")
        msg = IncomingMessage(
            connector=connector,
            channel_id=str(cfg["channel"]),
            user_id="",
            user_name="email-watch",
            content=(f"📬 {count} new email{'s' if count != 1 else ''} in your "
                     f"inbox:\n{summary}\n\n{instruction}"),
            bot_account=account,
        )
        logger.info(f"email-watch: {count} new message(s) → {agent_id}")
        asyncio.create_task(mgr.handle_message(agent_id, msg),
                            name=f"email-watch-{agent_id}")

    # --- loop -----------------------------------------------------------

    async def _effective_interval(self, agent_id: str, cfg: dict) -> int:
        """Config interval, unless a runtime override (/email-watch) is set."""
        base = max(MIN_INTERVAL, int(cfg.get("interval", 60)))
        storage = getattr(self.agent_manager, "storage", None)
        if storage:
            try:
                ov = await storage.get_agent_override(agent_id, "email_watch_interval")
                if ov:
                    return max(MIN_INTERVAL, int(ov))
            except Exception:  # bad value or storage error must never kill the loop
                pass
        return base

    async def _watch_agent(self, agent_id: str, cfg: dict) -> None:
        account = cfg.get("google_account", "")
        interval = await self._effective_interval(agent_id, cfg)
        state = self._load_state()
        history_id = state.get(agent_id, "")
        failures = 0

        logger.info(f"email-watch: watching inbox for {agent_id} "
                    f"(google_account={account or 'default'}, every {interval}s)")
        while True:
            try:
                if not history_id:
                    history_id = await self._baseline(account)
                    if history_id:
                        state = self._load_state()
                        state[agent_id] = history_id
                        self._save_state(state)
                else:
                    ids, new_hid = await self._new_inbox_messages(account, history_id)
                    if not new_hid:  # expired baseline — reset, don't re-fire
                        logger.warning(f"email-watch: historyId expired for "
                                       f"{agent_id}, resetting baseline")
                        history_id = ""
                        continue
                    if ids:
                        summary = await self._summarize(account, ids)
                        await self._fire(agent_id, cfg, summary, len(ids))
                    if new_hid != history_id:
                        history_id = new_hid
                        state = self._load_state()
                        state[agent_id] = history_id
                        self._save_state(state)
                failures = 0
            except OAuth2AuthRevokedError as e:
                # The grant is gone; retrying cannot bring it back. Backing off
                # is not enough — a 5-minute retry forever is 288 identical
                # ERROR lines a day, and the real damage is silent: the agent
                # simply stops being woken by mail and nothing says so. Say it
                # once, with the command that fixes it, then stop.
                account_arg = f" --account {account}" if account else ""
                logger.error(
                    f"email-watch: STOPPED watching {agent_id} — its Google "
                    f"authorisation is revoked or expired and cannot refresh "
                    f"({e.error}: {e.description}). This agent will NOT be woken by "
                    f"new mail until it is re-authorised: "
                    f"python scripts/google-reauth.py{account_arg} — then restart the service."
                )
                return
            except Exception as e:
                failures += 1
                logger.error(f"email-watch: tick failed for {agent_id}: {e}")
            # Re-read each cycle so /email-watch interval changes apply live.
            interval = await self._effective_interval(agent_id, cfg)
            # Repeated failures (network down, Gmail 5xx) back off hard so a
            # transient outage isn't hammered every minute.
            await asyncio.sleep(interval if failures < 3 else max(interval, 300))

    async def run(self) -> None:
        watched = self.watched_agents
        if not watched:
            return
        await asyncio.gather(*(
            self._watch_agent(agent_id, cfg) for agent_id, cfg in watched.items()))
