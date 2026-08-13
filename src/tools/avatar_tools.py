"""Agent avatar tool — generate an on-brand avatar and set it on a bot.

Exposes the brand avatar system (src/lib/avatar_gen.py) to agents, so
"give Milo an avatar" is one tool call instead of improvised image
generation and manual uploads.
"""

import asyncio
import logging
import os
from pathlib import Path

import yaml

from src.core.agent_scaffold import agent_entries
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool
from src.lib.avatar_gen import (
    ACCENTS,
    EYES,
    build_svg,
    render_png,
    resolve_accent,
    upload_discord_avatar,
)

logger = logging.getLogger(__name__)


def _agent_dir(agent_name: str) -> Path:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay and (Path(overlay) / "agents" / agent_name).is_dir():
        return Path(overlay) / "agents" / agent_name
    from src.core.base import KBOTS_TMP
    return KBOTS_TMP / "avatars" / agent_name


def _bot_account_for(agent_name: str) -> str:
    """The Discord account an agent posts as, which is not always its own name.

    Most agents run on an account named after themselves, so the two were used
    interchangeably. The primary agent does not: it runs on the shared 'main'
    account, so assuming account == agent name looked up a token key that was
    never created, and setting the main agent's avatar always failed with 'no
    bot token found'. Agent config carries the real account under its discord
    routing; fall back to the name when nothing is configured, which is the
    old behaviour and correct for every scaffolded agent.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay:
        entry = agent_entries(Path(overlay)).get(agent_name) or {}
        account = ((entry.get("routing") or {}).get("discord") or {}).get("account")
        if account:
            return account
    return agent_name


def _token_key_for(account: str) -> str:
    cfg_file = resolve_config_file("config.yaml")
    if cfg_file.exists():
        with open(cfg_file) as f:
            cfg = yaml.safe_load(f) or {}
        acct = (((cfg.get("connectors") or {}).get("discord") or {}).get("accounts") or {}).get(account) or {}
        if acct.get("token_key"):
            return acct["token_key"]
    return f"discord-{account}"


@tool(
    name="set_agent_avatar",
    description=(
        "Generate an on-brand avatar for an agent's Discord bot and set it. "
        f"Eye styles: {', '.join(EYES)}. Accents: {', '.join(ACCENTS)} or a hex color. "
        "The frame/screen stay fixed so the fleet reads as one family."
    ),
)
async def set_agent_avatar(
    ctx: ToolContext, agent_name: str, eyes: str = "capsule", accent: str = "red"
) -> str:
    """Compose the avatar, save it in the agent's directory, upload to Discord."""
    if eyes not in EYES:
        return f"Unknown eye style '{eyes}' — one of: {', '.join(EYES)}"
    try:
        accent_pair = resolve_accent(accent)
    except SystemExit as e:
        return str(e)

    out_dir = _agent_dir(agent_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / "avatar.svg"
    png_path = out_dir / "avatar.png"
    svg_path.write_text(build_svg(eyes, accent_pair))

    # Playwright's sync API can't run inside the event loop — render in a thread.
    rendered = await asyncio.to_thread(render_png, svg_path, png_path, 512)
    if not rendered:
        return (f"Avatar SVG written to {svg_path}, but PNG render failed (Chromium "
                "missing?) — Discord upload needs the PNG. Ask an operator to run: "
                "uv run playwright install chromium")

    account = _bot_account_for(agent_name)
    token_key = _token_key_for(account)
    token = ctx.vault.get(token_key) if ctx.vault else None
    if not token:
        return (f"Avatar written to {png_path}, but no bot token found for "
                f"'{agent_name}' (account '{account}', vault key '{token_key}') "
                "— upload it manually (Developer Portal → app → Bot → icon) "
                "or add the token first.")

    success, msg = await asyncio.to_thread(upload_discord_avatar, png_path, token)
    if success:
        return (f"Avatar set on bot '{agent_name}' ({eyes} eyes, {accent} accent). "
                f"Files: {svg_path} / {png_path}. Note: Discord allows only a couple "
                "of avatar changes per half hour.")
    return f"Avatar written to {png_path}, but the Discord upload failed: {msg}"
