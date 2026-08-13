"""Team tools — manage team roster.

Architecture:
  Source of truth: config/team.json (HITL-gated writes)
  Distribution:    Auto-injected into agent context at session startup
  Memory DB:       NOT used for team data — operational knowledge only
"""

import json
import logging
import re
import threading
from pathlib import Path

from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)

# Serializes roster writes — bots record their identity concurrently at startup.
_TEAM_LOCK = threading.Lock()


def record_bot_identity(name: str, discord_id: str) -> None:
    """Record a bot's Discord user ID in the roster so other agents recognize it as
    a teammate (resolve_discord_user / user-context) instead of an unknown guest.

    Targeted: only sets the `discord` field on the matching agent (never clobbers
    tier/role), and adds a minimal entry if the agent isn't in the roster yet.
    """
    if not (name and discord_id):
        return
    with _TEAM_LOCK:
        team = _load_team()
        agents = team.setdefault("agents", [])
        aid = _slug(name)
        # The Discord ID is the durable key — check it before any name match. A
        # bot already bound to a row needs nothing done, whatever it is called
        # now; without this a rename would bind a second row to the same bot.
        if _find_by_discord(team, discord_id) is not None:
            return  # already bound to this bot
        # Match on slugs, not raw lowercase. The Discord display name and the
        # config id are written by different hands: 'Data Bot' lowercases to
        # 'data bot', which equals neither the id 'data-bot' nor the name
        # 'Data.Bot'. That near-miss is what appended a duplicate stub the first
        # time each bot started.
        for a in agents:
            if _slug(a.get("id", "")) == aid or _slug(a.get("name", "")) == aid:
                if a.get("discord") == discord_id:
                    return  # already correct
                a["discord"] = discord_id
                _save_team(team)
                logger.info(f"roster: recorded discord id for agent '{name}'")
                return
        agents.append({"id": aid, "name": name, "type": "agent", "discord": discord_id})
        _save_team(team)
        logger.info(f"roster: added agent '{name}' with discord id")


def _read_rights(project_dir) -> list:
    """The allow-list from an agent's .claude/settings.json, or []."""
    if not project_dir:
        return []
    sp = Path(project_dir) / ".claude" / "settings.json"
    if not sp.exists():
        return []
    try:
        return json.loads(sp.read_text()).get("permissions", {}).get("allow", [])
    except (json.JSONDecodeError, OSError):
        return []


def reconcile_roster(config: dict) -> None:
    """Make team.json the single, current source of truth for the team.

    Runs once at startup: syncs each configured agent's tier/model/tools/rights/
    reports_to into the roster (preserving curated name/role/domain/discord) and
    PRUNES agents no longer in config. Humans are left untouched. This is the
    central place the roster injection and the /team-graph tool both read from.
    """
    agents_cfg = config.get("agents", {}) or {}
    if not agents_cfg:
        return
    with _TEAM_LOCK:
        team = _load_team()
        existing = {a.get("id"): a for a in team.get("agents", [])}
        # hub = a coordinator, else the agent on the 'main' bot account
        hub = next((aid for aid, ac in agents_cfg.items() if ac.get("tier") == "coordinator"), None)
        if not hub:
            hub = next((aid for aid, ac in agents_cfg.items()
                        if ac.get("bot_account", aid) == "main"), None)
        agents = []
        for aid, ac in agents_cfg.items():
            e = existing.get(aid, {"id": aid})
            e["type"] = "agent"
            e.setdefault("name", ac.get("display_name") or aid.capitalize())
            # Purpose: keep a curated role/domain if present, else take the config
            # description so every agent shows a purpose in the roster and graph.
            if not e.get("role") and not e.get("domain") and ac.get("description"):
                e["role"] = ac["description"]
            e["agent_tier"] = ac.get("tier", "assistant")
            model = (ac.get("llm") or {}).get("model") or ac.get("model")
            if model:
                e["model"] = model
            if ac.get("tools") is not None:
                e["tools"] = ac["tools"]
            rights = _read_rights(ac.get("project_dir"))
            if rights:
                e["rights"] = rights
            if aid != hub and not e.get("reports_to"):
                e["reports_to"] = hub or ""
            agents.append(e)
        team["agents"] = agents   # replaces the list → prunes anything not in config
        _save_team(team)
        logger.info(f"roster reconciled: {len(agents)} agents from config "
                    f"(pruned {len(existing) - len(agents)} stale)")

TEAM_FILE = resolve_config_file("team.json")


def _load_team() -> dict:
    """Load team data from team.json."""
    if TEAM_FILE.exists():
        with open(TEAM_FILE) as f:
            return json.load(f)
    return {"humans": [], "agents": []}


def _save_team(data: dict) -> None:
    """Save team data to team.json."""
    TEAM_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TEAM_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _slug(value: str) -> str:
    """The roster-id form of a name: lowercase, non-alphanumerics collapsed to
    single hyphens. 'Data Bot', 'Data.Bot' and 'data-bot' all reduce to 'data-bot',
    so the same agent written three ways still matches one row.
    """
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _find_by_discord(team: dict, discord_id: str) -> dict | None:
    """Find the agent row already bound to this Discord ID, if any.

    A Discord ID identifies a bot account, and policy is one app per agent, so
    it is the only durable key in the roster — an id derived from a display
    name drifts the moment the name is edited.
    """
    if not discord_id:
        return None
    for a in team.get("agents", []):
        if str(a.get("discord") or "") == str(discord_id):
            return a
    return None


def upsert_agent(agent_id: str, name: str, *, agent_tier: str = "assistant",
                 role: str = "", domain: str = "", discord: str = "",
                 reports_to: str = "") -> bool:
    """Add or update an agent in the roster (team.json), idempotently.

    Programmatic entry point (used by create_agent) so the relations map stays
    in sync with agents.yaml — no manual team_add needed. Only non-empty fields
    overwrite existing values. Returns True if the agent was newly added.

    Matching is by id, then by Discord ID: a row already bound to this bot is
    updated in place rather than duplicated, so a roster stub under a stale id
    cannot coexist with the real agent.
    """
    team = _load_team()
    agents = team.setdefault("agents", [])
    fields = {"name": name, "type": "agent", "agent_tier": agent_tier, "role": role}
    for k, v in (("domain", domain), ("discord", discord), ("reports_to", reports_to)):
        if v:
            fields[k] = v
    for a in agents:
        if a.get("id") == agent_id:
            a.update({k: v for k, v in fields.items() if v or k in ("agent_tier",)})
            _save_team(team)
            return False
    # No id match — before creating a row, adopt any row already holding this
    # Discord ID. Without this the roster accretes a duplicate per rename.
    bound = _find_by_discord(team, discord)
    if bound is not None:
        stale_id = bound.get("id")
        bound["id"] = agent_id
        bound.update({k: v for k, v in fields.items() if v or k in ("agent_tier",)})
        _save_team(team)
        logger.info(f"roster: adopted existing row for discord {discord} "
                    f"('{stale_id}' → '{agent_id}') instead of adding a duplicate")
        return False
    agents.append({"id": agent_id, **fields})
    _save_team(team)
    return True


def resolve_discord_user(discord_id: str) -> dict | None:
    """Resolve a Discord user ID to a team member profile."""
    team = _load_team()
    for member in team.get("humans", []):
        contact = member.get("contact", {})
        if contact.get("discord") == discord_id:
            return member
    for agent in team.get("agents", []):
        if agent.get("discord") == discord_id:
            return agent
    return None


def build_user_context(discord_id: str) -> str:
    """Build a user context block for prompt injection."""
    member = resolve_discord_user(discord_id)
    if not member:
        return (
            f"<unknown-user>\n"
            f"Discord ID: {discord_id}\n"
            f"This user is not in the team roster. Treat them as a guest.\n"
            f"</unknown-user>"
        )

    name = member.get("name", "Unknown")
    role = member.get("role", "")
    access = member.get("access", "")
    context = member.get("context", "")
    prefs = member.get("preferences", {})
    tz = prefs.get("timezone", "")

    lines = ["<user-context>"]
    lines.append(f"Name: {name}")
    if role:
        lines.append(f"Role: {role}")
    if access:
        lines.append(f"Access: {access}")
    if context:
        lines.append(f"Context: {context}")
    if tz:
        lines.append(f"Timezone: {tz}")
    lines.append("</user-context>")
    return "\n".join(lines)


# === Tools ===

@tool(name="team_list", description="List all team members", category="team")
async def team_list(ctx: ToolContext) -> str:
    """Show the full team roster — humans and agents."""
    team = _load_team()
    lines = ["**Team Roster**\n"]

    humans = team.get("humans", [])
    if humans:
        lines.append("**Humans:**")
        for h in humans:
            role = h.get("role", "")
            access = h.get("access", "")
            lines.append(f"  - {h['name']} ({role}) [{access}]")

    agents = team.get("agents", [])
    if agents:
        lines.append("\n**Agents:**")
        for a in agents:
            role = a.get("role", "")
            domain = a.get("domain", "")
            lines.append(f"  - {a['name']} ({role}): {domain}")

    return "\n".join(lines)


@tool(name="team_get", description="Get full profile for a team member", category="team")
async def team_get(ctx: ToolContext, name: str) -> str:
    """Get detailed profile for a team member by name or ID."""
    team = _load_team()
    name_lower = name.lower()

    for member in team.get("humans", []) + team.get("agents", []):
        if (member.get("name", "").lower() == name_lower or
                member.get("id", "").lower() == name_lower):
            return json.dumps(member, indent=2)

    return f"Team member not found: {name}"


VALID_ACCESS_TIERS = {"owner", "admin", "staff"}
VALID_AGENT_TIERS = {"privileged", "coordinator", "assistant"}


@tool(name="team_add", description="[HITL-GATED] Add a new team member or agent", category="team", hitl=True)
async def team_add(
    ctx: ToolContext,
    id: str,
    name: str,
    role: str = "",
    type: str = "human",
    access: str = "staff",
    agent_tier: str = "assistant",
) -> str:
    """Add a new team member. Writes to team.json (HITL-gated).

    For humans: access must be owner/admin/staff.
    For agents: agent_tier must be privileged/coordinator/assistant.
    """
    team = _load_team()

    all_members = team.get("humans", []) + team.get("agents", [])
    if any(m.get("id") == id for m in all_members):
        return f"Team member with id '{id}' already exists"

    if type == "human":
        if access not in VALID_ACCESS_TIERS:
            return f"Invalid access tier '{access}'. Must be one of: {', '.join(VALID_ACCESS_TIERS)}"
        new_member = {
            "id": id,
            "name": name,
            "type": type,
            "access": access,
            "role": role,
            "contact": {},
            "preferences": {},
        }
        team.setdefault("humans", []).append(new_member)
    else:
        if agent_tier not in VALID_AGENT_TIERS:
            return f"Invalid agent_tier '{agent_tier}'. Must be one of: {', '.join(VALID_AGENT_TIERS)}"
        new_member = {
            "id": id,
            "name": name,
            "type": "agent",
            "agent_tier": agent_tier,
            "role": role,
        }
        team.setdefault("agents", []).append(new_member)

    _save_team(team)

    # Reload access control if available
    if ctx.agent_manager and hasattr(ctx.agent_manager, "access_control"):
        ac = ctx.agent_manager.access_control
        if ac:
            ac.reload_team()

    tier_info = access if type == "human" else agent_tier
    return f"Added {type} '{name}' (id: {id}, tier: {tier_info}) to team"


@tool(name="team_update", description="[HITL-GATED] Update a team member", category="team", hitl=True)
async def team_update(ctx: ToolContext, id: str, field: str, value: str) -> str:
    """Update a field on a team member's profile. Writes to team.json (HITL-gated)."""
    # Validate tier changes
    if field == "access" and value not in VALID_ACCESS_TIERS:
        return f"Invalid access tier '{value}'. Must be one of: {', '.join(VALID_ACCESS_TIERS)}"
    if field == "agent_tier" and value not in VALID_AGENT_TIERS:
        return f"Invalid agent_tier '{value}'. Must be one of: {', '.join(VALID_AGENT_TIERS)}"

    team = _load_team()

    # One Discord app per agent: refuse to bind an ID already held elsewhere,
    # which would leave two rows resolving to the same bot.
    if field == "discord" and value:
        bound = _find_by_discord(team, value)
        if bound is not None and bound.get("id") != id:
            return (f"Discord ID {value} is already bound to agent "
                    f"'{bound.get('id')}'. Remove that binding first, or "
                    f"correct that row's id — two rows must not share a bot.")

    for collection in [team.get("humans", []), team.get("agents", [])]:
        for member in collection:
            if member.get("id") == id:
                if "." in field:
                    parts = field.split(".", 1)
                    member.setdefault(parts[0], {})[parts[1]] = value
                else:
                    member[field] = value
                _save_team(team)
                # Reload access control on tier changes
                if field in ("access", "agent_tier"):
                    if ctx.agent_manager and hasattr(ctx.agent_manager, "access_control"):
                        ac = ctx.agent_manager.access_control
                        if ac:
                            ac.reload_team()
                return f"Updated {id}.{field} = {value}"

    return f"Team member not found: {id}"


@tool(name="team_remove", description="[HITL-GATED] Remove a team member", category="team", hitl=True)
async def team_remove(ctx: ToolContext, id: str) -> str:
    """Remove a team member from the roster. Writes to team.json (HITL-gated)."""
    team = _load_team()

    for key in ["humans", "agents"]:
        members = team.get(key, [])
        for i, m in enumerate(members):
            if m.get("id") == id:
                removed = members.pop(i)
                _save_team(team)
                # Reload access control
                if ctx.agent_manager and hasattr(ctx.agent_manager, "access_control"):
                    ac = ctx.agent_manager.access_control
                    if ac:
                        ac.reload_team()
                return f"Removed {removed.get('name', id)} from team"

    return f"Team member not found: {id}"
