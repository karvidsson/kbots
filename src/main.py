"""kbots entry point — load config, init registry, start connectors, run."""

import asyncio
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from src.core import runtime_state
from src.core.agent_manager import AgentManager
from src.core.base import read_vault_key_file, resolve_vault_key_file
from src.core.preflight import run_preflight
from src.core.registry import Registry
from src.core.router import Router
from src.core.skills import get_all_skills
from src.core.storage import Storage, resolve_db_path
from src.vault.fernet import FernetVault

logger = logging.getLogger("kbots")


def _resolve_config_dirs() -> list[Path]:
    """Return config directories in priority order (highest-priority first).

    Resolution order:
      1. $KBOTS_OVERLAY/config/  — client overlay (wins on conflict)
      2. $KBOTS_MODULES/config/     — modules layer (Layer 2)
      3. ./config/              — Core defaults / examples
    """
    dirs: list[Path] = []
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config"
        if p.is_dir():
            dirs.append(p)
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        p = Path(mod_path.strip()) / "config"
        if p.is_dir():
            dirs.append(p)
    dirs.append(Path("config"))
    return dirs


def _find_config_file(name: str, config_dirs: list[Path]) -> Path | None:
    """Find first matching config file across config dirs (highest-priority first)."""
    for d in config_dirs:
        f = d / name
        if f.exists():
            return f
    return None


def load_config(profile: str | None = None) -> dict:
    """Load all YAML config files.

    If profile is set (e.g. 'test'), loads config.test.yaml and agents.test.yaml
    instead of the defaults. Falls back to default files if profile files don't exist.

    Config resolution walks: $KBOTS_OVERLAY/config → $KBOTS_MODULES/config → ./config
    (first file found wins).
    """
    config_dirs = _resolve_config_dirs()
    suffix = f".{profile}" if profile else ""

    main_file = _find_config_file(f"config{suffix}.yaml", config_dirs)
    if not main_file:
        main_file = _find_config_file("config.yaml", config_dirs)
    main_config = {}
    if main_file:
        with open(main_file) as f:
            main_config = yaml.safe_load(f) or {}
        logger.info(f"Config loaded from {main_file}")
    else:
        # Falling through to {} used to be silent, so a fresh clone failed
        # later with schema errors that never named the actual problem.
        logger.warning(
            "No config.yaml found in any config dir ("
            + ", ".join(str(d) for d in config_dirs)
            + ") — starting with an empty config."
        )

    agents_file = _find_config_file(f"agents{suffix}.yaml", config_dirs)
    if not agents_file:
        agents_file = _find_config_file("agents.yaml", config_dirs)
    agents_config = {}
    if agents_file:
        with open(agents_file) as f:
            agents_config = yaml.safe_load(f) or {}
        logger.info(f"Agents loaded from {agents_file}")

    return {
        **main_config,
        "agents": agents_config.get("agents", {}),
    }


def unserved_discord_accounts(connectors_config: dict, agent_configs: dict) -> set[str]:
    """Enabled Discord bot accounts that no loaded agent routes to.

    Such a bot connects and shows online but silently drops every message —
    there's no agent to dispatch to. Returns the set of orphaned account names.
    """
    discord_cfg = connectors_config.get("discord", {}) or {}
    if not discord_cfg.get("enabled"):
        return set()
    enabled = set((discord_cfg.get("accounts") or {}).keys()) or {"default"}
    served = {
        ((cfg.get("routing") or {}).get("discord") or {}).get("account", "default")
        for cfg in agent_configs.values()
        if "discord" in (cfg.get("routing") or {})
    }
    return enabled - served


def setup_logging(level: str = "info") -> None:
    """Configure structured logging."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def acquire_lock(profile: str | None = None) -> int:
    """Acquire an exclusive lock file. Exits if another instance is running."""
    suffix = f"-{profile}" if profile else ""
    lock_path = Path(f"data/kbots{suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another kbots instance is already running. Exiting.")
        sys.exit(1)
    # Write our PID for visibility
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd  # Keep fd open — lock released when process exits


async def main() -> None:
    # Parse --profile early so lock file is profile-aware
    profile = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--profile" and i < len(sys.argv):
            profile = sys.argv[i + 1] if i + 1 <= len(sys.argv) else None
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]

    lock_fd = acquire_lock(profile)  # noqa: F841 — keep ref: GC would release the flock

    if profile:
        os.environ["KBOTS_PROFILE"] = profile
    config = load_config(profile=profile)

    log_level = config.get("kbots", {}).get("log_level", "info")
    setup_logging(log_level)

    logger.info("Starting kbots...")

    # --- Vault ---
    # Resolve secrets.enc from config dirs (overlay → modules → core)
    config_dirs = _resolve_config_dirs()
    vault_file = _find_config_file("secrets.enc", config_dirs)
    vault_path = vault_file if vault_file else Path("config/secrets.enc")
    vault = FernetVault(vault_path=str(vault_path))
    if vault_path.exists():
        # Try key file first (for systemd), then interactive prompt
        key_file = resolve_vault_key_file()
        if key_file.exists():
            passphrase = read_vault_key_file(key_file)
        elif not sys.stdin.isatty():
            passphrase = sys.stdin.readline().strip()
        else:
            import getpass
            passphrase = getpass.getpass("Vault passphrase: ")
        try:
            vault.unlock(passphrase)
            del passphrase  # Don't keep in memory
        except ValueError as e:
            logger.error(f"Vault unlock failed: {e}")
            sys.exit(1)
    else:
        # Dev mode — load from .env
        vault.unlock_from_env()

    # GitHub token for agents that shell out to `gh`/`git`. Do NOT put it in the
    # global process env — every utility-tool subprocess (tmux, video, audio,
    # computer) would inherit it and could exfiltrate it. It is injected only
    # into the agent CLI subprocess via AgentManager._subprocess_env (sourced
    # from the vault per launch).
    if vault.get("github-token"):
        logger.info("GitHub token available in vault (injected per agent CLI launch)")

    # --- Preflight checks ---
    data_dir = config.get("kbots", {}).get("data_dir", "./data")
    preflight_ok = await run_preflight(config, vault, str(resolve_db_path(data_dir)))
    if not preflight_ok:
        logger.critical("Preflight failed — fix errors above and restart")
        sys.exit(1)

    # --- Storage ---
    storage = Storage(db_path=resolve_db_path(data_dir))
    await storage.init()

    # Prune stale sessions on startup (keeps DB bounded across long-running installs).
    # Configurable via kbots.session_retention_days, default 30. Set to 0 to disable.
    retention_days = config.get("kbots", {}).get("session_retention_days", 30)
    if retention_days > 0:
        try:
            pruned = await storage.prune_stale_sessions(max_age_days=retention_days)
            if pruned:
                logger.info(f"Startup prune: removed {pruned} sessions older than {retention_days}d")
        except Exception as e:
            logger.error(f"Startup prune failed: {e}")

    # --- Auto-discover modules ---
    registry = Registry()
    registry.discover()

    # --- LLM providers ---
    defaults = config.get("defaults", {})
    llm_providers = {}
    for provider_name, provider_cls in registry.llm_providers.items():
        provider_config = defaults.get("llm", {})
        try:
            llm_providers[provider_name] = provider_cls(config=provider_config)
            logger.info(f"LLM provider: {provider_name}")
        except Exception as e:
            logger.error(f"Failed to init LLM provider {provider_name}: {e}")

    # --- Memory backends ---
    memory_backends = {}
    from src.core.base import memory_config as _memory_config
    from src.core.base import warn_on_split_store as _warn_on_split_store
    mem_config = _memory_config(config)
    for backend_name, backend_cls in registry.memory_backends.items():
        try:
            memory_backends[backend_name] = backend_cls(config=mem_config)
            logger.info(f"Memory backend: {backend_name}")
        except Exception as e:
            logger.error(f"Failed to init memory backend {backend_name}: {e}")

    # Note: team module reads from team.json directly, no memory backend needed

    # --- Graph memory (optional, additive to sqlite; opens lazily on first use) ---
    from src.lib.graph_store import close_graph, init_graph
    graph = init_graph(mem_config)
    if graph:
        logger.info(f"Graph memory: LadybugDB ({graph.path})")

    for legacy in _warn_on_split_store(config):
        logger.warning(
            f"Legacy memory store still present at {legacy}, outside the configured "
            f"data_dir. Nothing reads it — confirm it is migrated, then delete it. "
            f"Two stores is how a scrub or an audit silently reads stale data."
        )

    # --- Security: HITL, rate limiting, audit, content safety ---
    from src.core.audit import AuditLog
    from src.core.content_safety import BehaviorMonitor
    from src.core.hitl import HITLGate
    from src.core.rate_limiter import RateLimiter

    security_cfg = config.get("security", {})

    # HITL — always constructed so gated / hitl=True tools fail closed even when
    # no channel is configured (previously a missing channel meant no gate at all).
    hitl_cfg = security_cfg.get("hitl", {})
    admin_discord = (config.get("admin_users", {}) or {}).get("discord", [])
    hitl = HITLGate(hitl_cfg, storage._db, admin_users=admin_discord)
    await hitl.init_schema()
    await hitl.load_enabled()
    if not hitl_cfg.get("channel"):
        logger.warning("HITL: no security.hitl.channel configured — gated and hitl=True "
                       "tools fail closed (denied) on the in-process path.")
    if not hitl.enabled:
        logger.warning("HITL approval gate is DISABLED (full-control mode) — "
                       "tools run without human approval")
    recovered = await hitl.recover_pending()
    if recovered:
        logger.info(f"HITL: recovered {recovered} expired pending requests")
    logger.info(f"HITL gates: {len(hitl_cfg.get('gated_tools', []))} tools gated")

    # Rate limiter
    rate_limiter = RateLimiter(security_cfg.get("rate_limits", {}))
    logger.info("Rate limiter initialized")

    # Audit log
    audit = AuditLog(f"{data_dir}/audit.jsonl")
    audit.log_auth("startup", "kbots starting")
    logger.info(f"Audit log: {data_dir}/audit.jsonl")

    # Behavior monitor
    behavior_monitor = BehaviorMonitor()

    # Security alerter — sends alerts to configured Discord channel
    from src.core.alerts import AlertSender
    alerter = AlertSender(config, vault)
    if alerter.enabled:
        logger.info(f"Security alerts: channel {alerter.channel_id}")
    else:
        logger.info("Security alerts: logger only (no alert_channel configured)")

    # Training-data collector — opt-in (stores full conversation content locally)
    training_collector = None
    tc_cfg = config.get("kbots", {}).get("training_collection", {})
    if tc_cfg.get("enabled"):
        from src.core.training_collector import TrainingCollector
        tc_path = tc_cfg.get("path") or f"{data_dir}/training"
        training_collector = TrainingCollector(
            tc_path, include_tool_trace=tc_cfg.get("include_tool_trace", True))
        logger.info(f"Training-data collection: ON → {tc_path}")

    # Access control — opt-in (enabling it fail-closes unknown senders, which
    # would silently ignore any human not in team.json, so it must be a
    # deliberate operator choice). When unconfigured we log a loud warning rather
    # than silently enforcing and risking a lockout. `admin_users` bridge to owner
    # so turning it on can never lock the operator out.
    from src.core.access_control import AccessControl
    ac_cfg = security_cfg.get("access_control", {})
    hitl_gated = security_cfg.get("hitl", {}).get("gated_tools", [])
    if ac_cfg:
        access_control = AccessControl(ac_cfg, hitl_gated_tools=hitl_gated,
                                       admin_users=admin_discord)
        logger.info(f"Access control: ENABLED — {len(ac_cfg.get('safe_tools', []))} safe tools, "
                     f"isolated agents: {ac_cfg.get('isolated_agents', [])}")
    else:
        access_control = None
        logger.warning(
            "Access control is NOT configured — any user who can post in a routed "
            "channel can drive agents with their full toolset (including Bash). "
            "Enable security.access_control in config.yaml (see config.yaml.example)."
        )

    # Keep team.json (the central roster) in sync with config — prune stale agents,
    # refresh tier/model/tools/rights. The roster injection + /team-graph read from it.
    from src.tools.team import reconcile_roster
    reconcile_roster(config)

    # --- Agent manager ---
    agent_configs = config.get("agents", {})
    agent_manager = AgentManager(
        agent_configs=agent_configs,
        connectors={},
        llm_providers=llm_providers,
        memory_backends=memory_backends,
        vault=vault,
        defaults=defaults,
        storage=storage,
        hitl=hitl,
        rate_limiter=rate_limiter,
        audit=audit,
        behavior_monitor=behavior_monitor,
        access_control=access_control,
        alerter=alerter,
        training_collector=training_collector,
    )

    # --- Internal loopback API: inter-agent calls from tool subprocesses ---
    from src.core.internal_api import InternalAPI
    internal_api = InternalAPI(agent_manager, config.get("internal_api", {}))
    try:
        await internal_api.start()
        agent_manager.internal_api = internal_api
    except Exception as e:
        # Degrades to the old behavior: ask_agent/send_to_agent unavailable.
        logger.error(f"Internal API failed to start — inter-agent tools disabled: {e}")

    # --- Connectors ---
    connectors_config = config.get("connectors", {})
    active_connectors: dict[str, object] = {}

    for conn_name, conn_cfg in connectors_config.items():
        if not conn_cfg or not conn_cfg.get("enabled", False):
            continue

        if conn_name not in registry.connectors:
            logger.warning(f"Connector '{conn_name}' enabled but not found in registry")
            continue

        try:
            connector = registry.create_connector(
                conn_name,
                config={**conn_cfg, "admin_users": config.get("admin_users", {}).get(conn_name, [])},
                vault=vault,
            )

            if hasattr(connector, "set_agent_configs"):
                connector.set_agent_configs(agent_configs)
            if hasattr(connector, "set_skills"):
                connector.set_skills(get_all_skills())
            # Give connector a reference to agent manager for /status etc.
            connector._agent_manager = agent_manager

            active_connectors[conn_name] = connector
            logger.info(f"Connector: {conn_name}")
        except Exception as e:
            logger.error(f"Failed to create connector {conn_name}: {e}")

    agent_manager.connectors = active_connectors

    # Defense-in-depth: a Discord bot account that no loaded agent routes to
    # will connect and go online but silently drop every message (no agent to
    # dispatch to). Warn loudly so this misconfiguration is visible instead of
    # looking like a dead bot. (Common cause: an ops agent left in a separate
    # profile, or a bot added without an agent.)
    for acct in unserved_discord_accounts(connectors_config, agent_configs):
        logger.warning(
            f"Discord bot account '{acct}' is connected but NO agent routes "
            f"to it — its messages (incl. DMs) will be ignored. Add an agent "
            f"with routing.discord.account: {acct}, or remove the account."
        )

    # Wire HITL to Discord connector for approval messages and reactions
    if hitl and "discord" in active_connectors:
        hitl.connector = active_connectors["discord"]
        active_connectors["discord"]._hitl = hitl

    # --- Router ---
    router = Router(agent_manager)
    for connector in active_connectors.values():
        connector.on_message = router.route

    # --- Server auto-setup: a new Discord server provisions itself ---
    # The channels are created by the connector deterministically. What comes
    # back here is only the part that needs an agent: its nickname, its avatar,
    # and telling the owner what it just did. If this turn fails the server is
    # still correctly wired, which is the ordering that matters.
    if "discord" in active_connectors:
        from src.core.server_setup import build_join_intro_message, build_setup_message

        def _agent_identity(agent_id):
            from src.core.identity_boot import configured_name
            cfg = (agent_manager.agent_configs or {}).get(agent_id) or {}
            account = ((cfg.get("routing") or {}).get("discord") or {}).get("account", "")
            return configured_name(agent_manager.agent_configs, agent_id), str(account)

        async def _on_guild_setup(agent_id, guild_id, guild_name, outcomes):
            channels = {o.key: o.channel_id for o in outcomes if o.channel_id}
            home = channels.get("platform_updates") or channels.get("alerts")
            if not home:
                logger.warning(
                    f"Server setup for '{guild_name}' wired no channel the agent "
                    f"can post in — skipping the introduction turn")
                return
            display_name, account = _agent_identity(agent_id)
            await agent_manager.handle_message(agent_id, build_setup_message(
                agent_id, guild_id, guild_name, outcomes, "discord", home,
                display_name=display_name, account=account))

        async def _on_guild_intro(agent_id, guild_id, guild_name, channel_id):
            display_name, account = _agent_identity(agent_id)
            await agent_manager.handle_message(agent_id, build_join_intro_message(
                agent_id, guild_id, guild_name, "discord", channel_id,
                display_name=display_name, account=account))

        active_connectors["discord"].set_setup_context(
            config,
            str(config.get("kbots", {}).get("data_dir", "./data")),
            _on_guild_setup,
            profile=profile or "",
            on_guild_intro=_on_guild_intro,
        )

    # --- Start connectors ---
    for conn_name, connector in active_connectors.items():
        try:
            await connector.start()
            logger.info(f"Started: {conn_name}")
        except Exception as e:
            logger.error(f"Failed to start {conn_name}: {e}")

    if not active_connectors:
        logger.error("No connectors started. Nothing to do.")
        close_graph()
        await storage.close()
        return

    logger.info(
        f"kbots running — {len(active_connectors)} connector(s), "
        f"{len(agent_configs)} agent(s), {len(llm_providers)} LLM provider(s)"
    )

    # --- Platform version: freeze the running commit; announce real updates ---
    from src.core import version as _version
    data_dir = Path(config.get("kbots", {}).get("data_dir", "./data"))
    _version.set_data_dir(data_dir)  # so in-process readers agree with the writer
    _prev = _version.read_running_version(data_dir)
    _running = _version.write_running_version(data_dir)
    _run_v = _running.get("version") or _running["short"]
    logger.info(f"Running version {_run_v} ({_running['short']}) — {_running.get('subject', '')}")
    if _version.is_update(_prev, _running):
        _prev_v = _prev.get("version") or _prev["short"]
        logger.info(f"Platform updated {_prev_v} → {_run_v}")
        _changes = _version.commits_between(_prev["commit"], _running["commit"])
        _detail = f"\n```\n{_changes}\n```" if _changes else ""
        if alerter:
            # Its own channel when one is wired, else the alert channel, which is
            # where this has always gone. "The platform changed" and "something
            # attacked your agent" are different audiences, but an install that
            # has not run server setup must not lose the notice entirely.
            _updates = (runtime_state.get_flag("platform_updates_channel", None)
                        or (config.get("platform", {}) or {}).get("updates_channel", ""))
            alerter.post_bg(
                _updates,
                f"🔄 **Platform updated** — now running **{_run_v}** (was {_prev_v}). "
                f"`{_running['short']}`{_detail}"
            )

    # --- Start hot-reload watcher ---
    from src.core.digest import Digest
    digest = Digest(check_interval=5.0)

    async def on_digest_reload(changes):
        # Update skills reference on connectors when skills change
        if "skills" in changes:
            skills = get_all_skills()
            for conn in active_connectors.values():
                if hasattr(conn, "set_skills"):
                    conn.set_skills(skills)
            logger.info(f"Skills updated on connectors: {list(skills.keys())}")

    asyncio.create_task(digest.start(on_reload=on_digest_reload), name="digest")
    logger.info("Hot-reload watcher started")

    # --- Heartbeat: prove the engine is alive to an external watchdog ---
    # The watchdog (scripts/watchdog.sh) rolls back + restarts if this goes
    # stale, catching crash-loops and hangs a plain "is the process up?" misses.
    heartbeat_file = data_dir / "heartbeat"

    async def _heartbeat():
        while True:
            try:
                heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
                heartbeat_file.write_text(str(int(time.time())))
            except OSError:
                pass
            await asyncio.sleep(30)

    asyncio.create_task(_heartbeat(), name="heartbeat")

    # --- Scheduler: fire agents' self-scheduled tasks when due ---
    from src.core.scheduler import Scheduler
    sched_cfg = config.get("schedules", {}) or {}
    sched_channel = sched_cfg.get("channel", "")
    sched_bot = sched_cfg.get("bot", "")
    scheduler = Scheduler(agent_manager, schedules_channel=sched_channel,
                          schedules_bot=sched_bot)
    asyncio.create_task(scheduler.run(), name="scheduler")

    # --- Durable background jobs ---
    # Started alongside the scheduler because it is the same kind of component:
    # something has to notice that time passed. Its first tick reconciles jobs
    # that ended while the process was down, which is the case that used to look
    # like "still building" forever.
    from src.core.jobs import JobWatcher
    from src.core.jobs import set_data_dir as _jobs_set_data_dir
    _jobs_set_data_dir(data_dir)
    asyncio.create_task(JobWatcher(agent_manager).run(), name="job-watcher")
    # Let the Discord connector's ❌-reaction handler cancel schedule cards
    if sched_channel and "discord" in active_connectors:
        active_connectors["discord"]._schedules_channel = sched_channel

    # --- Restart recovery: turns killed at the last shutdown's drain timeout
    # get one synthetic turn each, telling the agent to resume or report.
    from src.core.recovery import build_recovery_message, load_and_clear
    interrupted = load_and_clear(data_dir)
    if interrupted:
        async def _deliver_recovery():
            await asyncio.sleep(20)  # let connectors finish coming online
            for turn in interrupted:
                agent_id = turn.get("agent_id")
                if agent_id not in agent_manager.agent_configs:
                    logger.warning(f"Restart recovery: unknown agent {agent_id!r} — skipped")
                    continue
                logger.info(f"Restart recovery → {agent_id} in {turn.get('channel_id')}")
                try:
                    await agent_manager.handle_message(
                        agent_id, build_recovery_message(turn))
                except Exception as e:
                    logger.error(f"Restart recovery for {agent_id} failed: {e}")
        asyncio.create_task(_deliver_recovery(), name="restart-recovery")

    # --- Identity reconcile: an agent whose Discord ACCOUNT name disagrees with
    # its config gets one turn to rename itself. Off by default: a rename is
    # outward-facing and rate-limited at two per hour, so an established fleet
    # should opt in rather than discover it after a restart.
    if (config.get("identity", {}) or {}).get("reconcile_on_boot", False):
        from src.core.identity_boot import build_identity_message, pending_renames

        async def _deliver_identity():
            await asyncio.sleep(25)  # after restart-recovery, connectors online
            discord_conn = active_connectors.get("discord")
            if not discord_conn:
                return
            live_names = {
                acct: bot.client.user.name
                for acct, bot in getattr(discord_conn, "bots", {}).items()
                if getattr(bot, "client", None) and bot.client.user
            }
            from src.core.identity_boot import owner_discord_id, record_attempt
            from src.tools.team import _load_team
            owner_id = owner_discord_id(_load_team())
            for pending in pending_renames(
                    live_names, agent_manager.agent_configs, data_dir):
                agent_id = pending["agent_id"]
                home = await agent_manager._resolve_home_channel(agent_id)
                if not home:
                    logger.warning(
                        f"Identity reconcile: {agent_id} has no home channel — skipped")
                    continue
                connector_name, channel_id, _ = home
                logger.info(
                    f"Identity reconcile -> {agent_id}: account is "
                    f"{pending['live_name']!r}, configured as "
                    f"{pending['configured_name']!r}")
                record_attempt(data_dir, agent_id, pending["configured_name"])
                try:
                    await agent_manager.handle_message(
                        agent_id, build_identity_message(
                            pending, owner_id, connector_name, channel_id))
                except Exception as e:
                    logger.error(f"Identity reconcile for {agent_id} failed: {e}")
        asyncio.create_task(_deliver_identity(), name="identity-reconcile")

    # --- Android emulator reaper: shut the emulator down once nobody uses it ---
    # Must live here, in the long-running service: the failure mode is "no agent
    # calls android_device again", so a check inside the tool would never run for
    # the one case that matters (an abandoned emulator burning ~7 of 10 cores).
    from src.tools.android import EmulatorReaper
    emulator_reaper = EmulatorReaper()
    asyncio.create_task(emulator_reaper.run(), name="android-emulator-reaper")

    # --- Email watcher: wake agents when new mail lands in their Gmail inbox ---
    from src.core.email_watch import EmailWatcher
    email_watcher = EmailWatcher(agent_manager, vault, data_dir=str(data_dir))
    if email_watcher.watched_agents:
        asyncio.create_task(email_watcher.run(), name="email-watch")
        logger.info(f"Email watch: ON for {sorted(email_watcher.watched_agents)}")

    # --- Permission watch: detect rights failures at runtime, brief the main agent ---
    from src.core.permission_watch import PermissionWatcher, set_watcher
    perm_watcher = PermissionWatcher(agent_manager, config, alerter=alerter)
    set_watcher(perm_watcher)
    if perm_watcher.enabled:
        asyncio.create_task(perm_watcher.run(), name="permission-watch")
        logger.info(f"Permission watch: ON (sweep every {perm_watcher.interval}s, "
                    f"escalation → {perm_watcher.agent or 'alert channel'})")

    # --- Reflector: consolidate each agent's lessons into LESSONS.md (cheap model);
    # when graph memory is enabled it also extracts edges from memories on the same cadence ---
    memory_cfg = config.get("defaults", {}).get("memory", {})
    reflection_cfg = memory_cfg.get("reflection", {})
    if reflection_cfg.get("enabled", True) or (memory_cfg.get("graph") or {}).get("enabled"):
        from src.core.reflector import Reflector
        reflector = Reflector(agent_manager, reflection_cfg,
                              graph_cfg=memory_cfg.get("graph"))
        asyncio.create_task(reflector.run(), name="reflector")

    # --- Memory decay: fade what nothing recalls, archive what has faded ---
    # Reads the backend the engine already opened, so it cannot decay a
    # different database from the one being written to. The shell script it
    # replaces resolved its own path and would have decayed the retired
    # pre-data_dir store.
    decay_backend = memory_backends.get(memory_cfg.get("backend", "sqlite")) \
        or next(iter(memory_backends.values()), None)
    if decay_backend is not None:
        from src.core.memory_decay import MemoryDecay
        decay = MemoryDecay(decay_backend, memory_cfg)
        if decay.enabled:
            asyncio.create_task(decay.run(), name="memory-decay")
        else:
            logger.info("Memory decay: OFF (defaults.memory.decay_enabled)")

    # --- Browser janitor: quit the shared debug Chrome after hours of idleness ---
    from src.core.browser_janitor import BrowserJanitor
    janitor = BrowserJanitor(config.get("browser", {}))
    if janitor.enabled:
        asyncio.create_task(janitor.run(), name="browser-janitor")

    # --- Turn judge: auto-label collected turns for training export (default off) ---
    judge_cfg = tc_cfg.get("judge", {}) or {}
    if training_collector and judge_cfg.get("enabled"):
        from src.core.judge import TurnJudge
        judge = TurnJudge(agent_manager, tc_cfg.get("path") or f"{data_dir}/training",
                          judge_cfg)
        asyncio.create_task(judge.run(), name="judge")
        logger.info(f"Turn judge: ON (provider={judge.provider}, model={judge.model})")
    elif training_collector:
        # Collection without labelling is a corpus nobody can filter. It ran
        # that way for 1163 turns here, and the only way to notice was to go
        # looking for a judgments file that had never been created.
        status = training_collector.status()
        logger.info(
            f"Turn judge: OFF — {status['turns']} turns collected, "
            f"{status['judgments']} judged, {status['rewards']} human reactions. "
            f"Enable at kbots.training_collection.judge.enabled to label them.")

    # --- Run until interrupted ---
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()

    # --- Graceful shutdown ---
    # Drain in-flight agent turns before tearing down connectors — a restart
    # mid-turn kills the turn silently (the user's message never gets a reply,
    # and any in-channel progress message is orphaned). Cap the wait so a hung
    # turn can't block restarts; launchd/systemd kill us after ExitTimeOut.
    drain_timeout = float(config.get("kbots", {}).get("shutdown_drain_seconds", 60))
    if agent_manager.active_turns > 0:
        logger.info(f"Draining {agent_manager.active_turns} in-flight turn(s) "
                    f"(up to {drain_timeout:.0f}s)...")
        import time as _time
        _deadline = _time.monotonic() + drain_timeout
        while agent_manager.active_turns > 0 and _time.monotonic() < _deadline:
            await asyncio.sleep(1)
        if agent_manager.active_turns > 0:
            logger.warning(f"Drain timeout — {agent_manager.active_turns} turn(s) still "
                           "running; restarting anyway")
            # Snapshot the turns we're about to kill so the next boot can
            # tell each affected agent to pick its work back up.
            from src.core.recovery import save_interrupted
            saved = save_interrupted(data_dir, agent_manager.inflight_snapshot())
            if saved:
                logger.info(f"Recorded {saved} interrupted turn(s) for recovery on next boot")
        else:
            logger.info("All turns drained cleanly")

    logger.info("Shutting down...")
    for conn_name, connector in active_connectors.items():
        try:
            await connector.stop()
        except Exception as e:
            logger.error(f"Error stopping {conn_name}: {e}")

    audit.log_auth("shutdown", "kbots stopping")
    audit.close()
    close_graph()
    await storage.close()
    logger.info("kbots stopped.")


if __name__ == "__main__":
    asyncio.run(main())
