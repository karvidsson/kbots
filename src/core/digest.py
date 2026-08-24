"""Hot-reload and capability ingestion system.

Watches skills/, src/tools/, and config/mcp.yaml for changes across all layers
(Core, modules, overlay). Reloads without full restart.
"""

import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path

import yaml

from src.core.base import PROJECT_ROOT
from src.core.skills import _skill_registry, load_skills
from src.core.tools import _tool_registry

logger = logging.getLogger(__name__)


def _build_watch_paths() -> dict[str, Path]:
    """Build watch paths across all layers: Core, modules ($KBOTS_MODULES), overlay ($KBOTS_OVERLAY)."""
    paths: dict[str, Path] = {
        "skills": PROJECT_ROOT / "skills",
        "tools": PROJECT_ROOT / "src" / "tools",
        "mcp_config": PROJECT_ROOT / "config" / "mcp.yaml",
    }

    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        modules_path = Path(mod_path.strip())
        modules_label = modules_path.name
        for subdir in ("tools", "services"):
            d = modules_path / subdir
            if d.is_dir():
                paths[f"tools_modules_{modules_label}_{subdir}"] = d
        d = modules_path / "skills"
        if d.is_dir():
            paths[f"skills_modules_{modules_label}"] = d

    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        overlay_path = Path(overlay)
        d = overlay_path / "tools"
        if d.is_dir():
            paths["tools_overlay"] = d
        d = overlay_path / "skills"
        if d.is_dir():
            paths["skills_overlay"] = d

    return paths


class Digest:
    """Watches for changes and hot-reloads modules."""

    def __init__(self, check_interval: float = 5.0):
        self.check_interval = check_interval
        self._mtimes: dict[str, float] = {}
        self._running = False

        # Directories to watch (across all layers)
        self._watch_paths = _build_watch_paths()

    async def start(self, on_reload=None):
        """Start the file watcher loop."""
        import asyncio
        self._running = True
        self._on_reload = on_reload
        self._snapshot_mtimes()

        while self._running:
            await asyncio.sleep(self.check_interval)
            changes = self._check_changes()
            if changes:
                await self._handle_changes(changes)

    def stop(self):
        self._running = False

    def _snapshot_mtimes(self) -> None:
        """Record current modification times for all watched files."""
        for category, path in self._watch_paths.items():
            if path.is_dir():
                for f in path.glob("*.yaml" if category == "skills" else "*.py"):
                    self._mtimes[str(f)] = f.stat().st_mtime
            elif path.is_file():
                self._mtimes[str(path)] = path.stat().st_mtime

    def _check_changes(self) -> dict[str, list[str]]:
        """Check for new/modified/deleted files. Returns changes by category."""
        changes: dict[str, list[str]] = {}

        for category, path in self._watch_paths.items():
            if path.is_dir():
                pattern = "*.yaml" if category == "skills" else "*.py"
                current_files = {str(f): f.stat().st_mtime for f in path.glob(pattern)}
            elif path.is_file() and path.exists():
                current_files = {str(path): path.stat().st_mtime}
            else:
                current_files = {}

            # Check for new or modified files
            for filepath, mtime in current_files.items():
                old_mtime = self._mtimes.get(filepath)
                if old_mtime is None or mtime > old_mtime:
                    changes.setdefault(category, []).append(filepath)
                    self._mtimes[filepath] = mtime

            # Check for deleted files (only for directories)
            if path.is_dir():
                for filepath in list(self._mtimes.keys()):
                    if filepath.startswith(str(path)) and filepath not in current_files:
                        changes.setdefault(category, []).append(f"DELETED:{filepath}")
                        del self._mtimes[filepath]

        return changes

    async def _handle_changes(self, changes: dict[str, list[str]]) -> None:
        """Process detected changes."""
        # Any skills category (core, modules, overlay) triggers full skills reload
        skills_changed = any(k.startswith("skills") for k in changes)
        tools_changed = any(k.startswith("tools") for k in changes)

        if skills_changed:
            count = reload_skills()
            skills_files = [f for k, v in changes.items() if k.startswith("skills") for f in v]
            logger.info(f"Hot-reload: {count} skills reloaded ({skills_files})")

        if tools_changed:
            count = reload_tools()
            tools_files = [f for k, v in changes.items() if k.startswith("tools") for f in v]
            logger.info(f"Hot-reload: {count} tools reloaded ({tools_files})")

        if "mcp_config" in changes:
            logger.info("Hot-reload: MCP config changed — reconnect needed")

        if self._on_reload:
            # Normalise category names for callback (just "skills" and "tools")
            normalised = {}
            for k, v in changes.items():
                if k.startswith("skills"):
                    normalised.setdefault("skills", []).extend(v)
                elif k.startswith("tools"):
                    normalised.setdefault("tools", []).extend(v)
                else:
                    normalised[k] = v
            await self._on_reload(normalised)


def skill_write_dir() -> Path:
    """Where a NEWLY CREATED skill is written: the overlay, when there is one.

    create_skill wrote into the Core checkout, which is wrong in three ways at
    once and was only ever visible on the third.

    A hardened systemd unit lists the engine root under ReadOnlyPaths, so the
    write fails outright: an agent on a Linux install cannot create a skill at
    all, and the same call works perfectly on a developer Mac, which has no
    sandbox. Second, Core is replaced by every `git pull`, so on the machines
    where it did work the skill survived until the next deploy. Third, the
    loader reads Core first and the overlay last, so a skill written to Core is
    also the one that loses to any overlay skill of the same name.

    Falls back to Core when no overlay is configured, which is the single-user
    dev checkout where Core is the only layer there is.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / "skills" if overlay else PROJECT_ROOT / "skills"


def reload_skills() -> int:
    """Re-scan skills directories across all layers and update the registry."""
    _skill_registry.clear()

    # Core skills
    load_skills("skills")

    # Modules-layer skills
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        modules_skills = Path(mod_path.strip()) / "skills"
        if modules_skills.is_dir():
            load_skills(modules_skills)

    # Overlay skills
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        overlay_skills = Path(overlay) / "skills"
        if overlay_skills.is_dir():
            load_skills(overlay_skills)

    return len(_skill_registry)


def reload_tools() -> int:
    """Re-import all tool modules across all layers."""
    tool_names_before = set(_tool_registry.keys())

    # Core tools
    _reload_tools_dir(PROJECT_ROOT / "src" / "tools", prefix="src.tools.")

    # Modules-layer tools
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    for mod_path in modules_raw.split(":"):
        if not mod_path.strip():
            continue
        modules_path = Path(mod_path.strip())
        modules_label = modules_path.name
        for subdir in ("tools", "services"):
            d = modules_path / subdir
            if d.is_dir():
                _reload_tools_dir(d, prefix=f"kbots_modules_{modules_label}_{subdir}_")

    # Overlay tools
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        d = Path(overlay) / "tools"
        if d.is_dir():
            _reload_tools_dir(d, prefix="kbots_overlay_")

    new_tools = set(_tool_registry.keys()) - tool_names_before
    if new_tools:
        logger.info(f"New tools discovered: {new_tools}")

    return len(_tool_registry)


def _reload_tools_dir(tools_dir: Path, prefix: str) -> None:
    """Re-import all .py files in a tools directory.

    Layer files (overlay / modules) are loaded BY PATH under a synthetic module
    name, and importlib.reload cannot be used on them: reload re-resolves the
    spec by NAME through the normal finders, and a synthetic name is on no
    sys.path, so reloading an already-loaded layer tool raised
    "spec not found for the module 'kbots_overlay_x'" every time. The exception
    was caught and logged while the caller still reported "N tools reloaded" —
    so editing an overlay tool looked like it took effect, and the process went
    on running whichever version it imported first until someone restarted the
    service. Re-executing the file spec is what reload means for these.
    """
    for py_file in tools_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        module_name = f"{prefix}{py_file.stem}"
        try:
            if prefix.startswith("src."):
                # A real package module: importable by name, so reload works.
                if module_name in importlib.sys.modules:
                    importlib.reload(importlib.sys.modules[module_name])
                else:
                    importlib.import_module(module_name)
                continue

            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if not (spec and spec.loader):
                logger.error(f"Failed to reload {module_name}: no import spec for {py_file}")
                continue
            mod = importlib.util.module_from_spec(spec)
            previous = importlib.sys.modules.get(module_name)
            # Publish before exec so a module importing itself sees the new
            # object — the same order importlib.reload uses.
            importlib.sys.modules[module_name] = mod
            try:
                # Compile from source rather than spec.loader.exec_module: the
                # bytecode cache is keyed on (size, mtime-to-the-SECOND), so two
                # edits inside the same second that happen not to change the file
                # length load the STALE .pyc. Reloading exists precisely to pick
                # up a just-made edit, which is exactly when that collides.
                source = py_file.read_bytes()
                exec(compile(source, str(py_file), "exec"), mod.__dict__)
            except Exception:
                # A broken edit must not leave a half-initialised module behind
                # where a working one used to be: put the old one back and let
                # the live tools keep running on it.
                if previous is not None:
                    importlib.sys.modules[module_name] = previous
                else:
                    importlib.sys.modules.pop(module_name, None)
                raise
        except Exception as e:
            logger.error(f"Failed to reload {module_name}: {e}")


def ingest_skill_from_text(name: str, description: str, prompt: str,
                           tools: list[str], parameters: dict | None = None,
                           llm: dict | None = None, restrict_tools: bool = False,
                           max_rounds: int = 0) -> Path:
    """Create a skill YAML file from provided text. Returns the file path."""
    skill_data = {
        "name": name,
        "description": description,
        "prompt": prompt,
        "tools": tools,
    }
    if parameters:
        skill_data["parameters"] = parameters
    if restrict_tools:
        skill_data["restrict_tools"] = True
    if max_rounds:
        skill_data["max_rounds"] = int(max_rounds)
    if llm:
        skill_data["llm"] = llm

    skill_path = skill_write_dir() / f"{name}.yaml"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    with open(skill_path, "w") as f:
        yaml.dump(skill_data, f, default_flow_style=False, sort_keys=False)

    # Hot-reload immediately
    reload_skills()
    return skill_path


def ingest_mcp_server(
    name: str,
    *,
    transport: str = "sse",
    url: str = "",
    command: str = "",
    args: list[str] | None = None,
    cwd: str = "",
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    description: str = "",
) -> None:
    """Add an MCP server to config/mcp.yaml and regenerate .mcp.json files.

    Supports both remote (SSE) and local (stdio) servers.
    """
    mcp_path = _resolve_mcp_yaml()
    if mcp_path.exists():
        with open(mcp_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
        mcp_path.parent.mkdir(parents=True, exist_ok=True)

    entry: dict = {"transport": transport}
    if transport == "stdio":
        if not command:
            raise ValueError("stdio transport requires 'command'")
        entry["command"] = command
        if command in ("npx", "pnpx", "bunx") and args and "-y" not in args and "--yes" not in args:
            # Headless safety: package runners prompt to install on first use
            # and hang forever waiting on stdin (observed live: silent agents).
            args = ["-y", *args]
        if args:
            entry["args"] = args
        if cwd:
            entry["cwd"] = cwd
        if env:
            entry["env"] = env
    else:
        if not url:
            raise ValueError(f"{transport} transport requires 'url'")
        entry["url"] = url
        if headers:
            entry["headers"] = headers
    if description:
        entry["description"] = description

    config.setdefault("servers", {})[name] = entry

    with open(mcp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Added MCP server: {name} ({transport})")

    # Regenerate .mcp.json files for all agents
    regenerate_mcp_json()


def _resolve_mcp_yaml() -> Path:
    """Find mcp.yaml — overlay takes precedence over Core."""
    overlay = os.environ.get("KBOTS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config" / "mcp.yaml"
        if p.exists():
            return p
    return PROJECT_ROOT / "config" / "mcp.yaml"


def _load_mcp_servers() -> dict:
    """Load all MCP server definitions from mcp.yaml."""
    mcp_path = _resolve_mcp_yaml()
    if not mcp_path.exists():
        return {}
    with open(mcp_path) as f:
        config = yaml.safe_load(f) or {}
    return config.get("servers", {})


def _build_mcp_json(servers: dict, agent_env: dict | None = None) -> dict:
    """Convert mcp.yaml servers to Claude Code .mcp.json format.

    Args:
        servers: Server definitions from mcp.yaml.
        agent_env: Per-agent env vars to merge into stdio servers.
    """
    mcp_servers = {}
    for name, cfg in servers.items():
        transport = cfg.get("transport", "sse")
        if transport == "stdio":
            entry = {"command": cfg["command"]}
            if cfg.get("args"):
                entry["args"] = cfg["args"]
            if cfg.get("cwd"):
                entry["cwd"] = cfg["cwd"]
            env = dict(cfg.get("env", {}))
            # 'vault:<key>' values become ${VAR} references — the engine
            # resolves the secret from the vault into the CLI subprocess env
            # at launch, so plaintext secrets never land in .mcp.json.
            env = {k: (f"${{{k}}}" if isinstance(v, str) and v.startswith("vault:") else v)
                   for k, v in env.items()}
            if name == "kbots-tools":
                # The tool server resolves the vault/config through the layer
                # env — the CLI's env allowlist doesn't pass these through, so
                # they must be pinned here. Dropping them (regression, found
                # via a 'no Discord token' failure with the token present in
                # the vault) silently strands the MCP server outside the
                # overlay: locked vault, core-only config.
                for layer_var in ("KBOTS_OVERLAY", "KBOTS_MODULES",
                                  "KBOTS_HOME", "KBOTS_PROFILE"):
                    val = os.environ.get(layer_var)
                    if val and layer_var not in env:
                        env[layer_var] = val
            if agent_env:
                env.update(agent_env)
            if env:
                entry["env"] = env
        else:
            entry = {"type": "http", "url": cfg["url"]}
            if cfg.get("headers"):
                entry["headers"] = cfg["headers"]
        mcp_servers[name] = entry
    return {"mcpServers": mcp_servers}


def regenerate_mcp_json() -> list[Path]:
    """Regenerate .mcp.json files for all agents from mcp.yaml.

    Injects per-agent env vars (KBOTS_PROFILE, KBOTS_BOT_ACCOUNT) into
    stdio servers from agent config.

    Returns list of paths that were updated.
    """
    servers = _load_mcp_servers()
    if not servers:
        logger.info("No MCP servers in mcp.yaml — nothing to generate")
        return []

    agents = _load_all_agents()
    updated = []

    for agent_id, agent_cfg, agent_dir in agents:
        # Build per-agent env overrides for stdio servers
        bot_account = (agent_cfg.get("routing", {}).get("discord", {}).get("account", ""))
        profile = agent_cfg.get("profile", agent_id if agent_id != agent_cfg.get("name", agent_id) else "")
        # Identity keys MUST survive regeneration: the MCP server resolves
        # the calling agent from KBOTS_AGENT_ID / KBOTS_PROJECT_DIR, and
        # without them every agent collapses into the 'mcp-agent' fallback —
        # which silently defeats per-agent tool scoping and memory attribution
        # (regression: found via a private tool recorded as owner 'mcp-agent').
        agent_env = {
            "KBOTS_AGENT_ID": agent_id,
            "KBOTS_PROJECT_DIR": str(agent_dir),
        }
        if bot_account:
            agent_env["KBOTS_BOT_ACCOUNT"] = bot_account
        if profile:
            agent_env["KBOTS_PROFILE"] = profile

        mcp_json = _build_mcp_json(servers, agent_env=agent_env)
        target = agent_dir / ".mcp.json"
        target.write_text(json.dumps(mcp_json, indent=2) + "\n")
        updated.append(target)
        logger.info(f"Updated {target}")

    return updated


def _load_all_agents() -> list[tuple[str, dict, Path]]:
    """Load all agents from agents*.yaml files. Returns (agent_id, config, project_dir)."""
    overlay = os.environ.get("KBOTS_OVERLAY")

    agent_files = []
    for base in [Path(overlay) if overlay else None, PROJECT_ROOT]:
        if base is None:
            continue
        for config_dir in [base, base / "config"]:
            if not config_dir.is_dir():
                continue
            for f in config_dir.iterdir():
                if f.name == "agents.yaml" or (
                    f.name.startswith("agents.") and f.name.endswith(".yaml") and f.name != "agents.yaml.example"
                ):
                    agent_files.append(f)

    all_agents = {}
    for af in agent_files:
        with open(af) as fh:
            config = yaml.safe_load(fh) or {}
        all_agents.update(config.get("agents", {}))

    results = []
    for agent_id, agent_cfg in all_agents.items():
        project_dir = agent_cfg.get("project_dir", f"./agents/{agent_id}")
        resolved = Path(project_dir).resolve()
        if not resolved.is_dir() and overlay:
            overlay_dir = Path(overlay) / "agents" / agent_id
            if overlay_dir.is_dir():
                resolved = overlay_dir
        if resolved.is_dir():
            results.append((agent_id, agent_cfg, resolved))
    return results


def vault_env_for_servers() -> dict[str, str]:
    """Env vars that must be resolved from the vault at CLI launch.

    Returns {ENV_NAME: vault_key} for every 'vault:<key>' env value in
    mcp.yaml — the counterpart of the ${VAR} references _build_mcp_json
    writes into .mcp.json.
    """
    mapping: dict[str, str] = {}
    for cfg in _load_mcp_servers().values():
        for k, v in (cfg.get("env") or {}).items():
            if isinstance(v, str) and v.startswith("vault:"):
                mapping[k] = v[len("vault:"):]
    return mapping
