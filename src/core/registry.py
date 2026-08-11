"""Auto-discovery registry. Scans Core, modules, and overlay layers for modules."""

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from pathlib import Path

from src.core.base import PROJECT_ROOT, Connector, LLMProvider, MemoryBackend, VaultBackend
from src.core.skills import get_all_skills, load_skills
from src.core.tools import _tool_registry, get_all_tools

logger = logging.getLogger(__name__)


def _overlay_paths() -> tuple[list[Path], Path | None]:
    """Return (modules_paths, overlay_path) from env vars.

    KBOTS_MODULES supports colon-separated paths (like $PATH) so clients can
    selectively load tool groups:
        KBOTS_MODULES=/opt/kbots-modules/module_a:/opt/kbots-modules/module_b
    """
    modules_raw = os.environ.get("KBOTS_MODULES", "")
    modules_paths = [Path(p) for p in modules_raw.split(":") if p.strip()]
    overlay = os.environ.get("KBOTS_OVERLAY")
    return (modules_paths, Path(overlay) if overlay else None)


class Registry:
    """Central registry for all kbots modules. Auto-discovers on init.

    Discovery order (last loaded wins on name collision):
      1. Core: src/tools/, src/connectors/, etc.
      2. Modules: $KBOTS_MODULES paths (colon-separated) — tools/, connectors/, services/
      3. Overlay: $KBOTS_OVERLAY/tools/, $KBOTS_OVERLAY/connectors/
    """

    def __init__(self):
        self.connectors: dict[str, Connector] = {}
        self.llm_providers: dict[str, type[LLMProvider]] = {}
        self.memory_backends: dict[str, type[MemoryBackend]] = {}
        self.vault_backends: dict[str, type[VaultBackend]] = {}

    def discover(self) -> None:
        """Auto-discover all modules from Core, modules, and overlay layers."""
        modules_paths, overlay_path = _overlay_paths()

        # --- Layer 1: Core packages ---
        self._scan_package("src.tools")
        self._scan_package("src.connectors")
        self._scan_package("src.llm")
        self._scan_package("src.memory")
        self._scan_package("src.vault")
        self._scan_package("src.apis")

        # --- Layer 2: modules (multiple paths supported) ---
        for modules_path in modules_paths:
            self._scan_layer(modules_path, f"modules:{modules_path.name}")

        # --- Layer 3: Client overlay ---
        if overlay_path:
            self._scan_layer(overlay_path, "overlay")

        # Discover connector/provider/backend classes from imported modules
        self._collect_subclasses()

        # Load skills: Core → modules → overlay (last wins)
        load_skills("skills")
        for modules_path in modules_paths:
            modules_skills = modules_path / "skills"
            if modules_skills.is_dir():
                load_skills(modules_skills)
        if overlay_path:
            overlay_skills = overlay_path / "skills"
            if overlay_skills.is_dir():
                load_skills(overlay_skills)

        # Load agent-specific skills from agents/*/skills/
        # Check Core agents dir first, then overlay (overlay wins on collision)
        core_agents = PROJECT_ROOT / "agents"
        if core_agents.is_dir():
            self._load_agent_skills(core_agents)
        if overlay_path:
            overlay_agents = overlay_path / "agents"
            if overlay_agents.is_dir() and overlay_agents != core_agents:
                self._load_agent_skills(overlay_agents)

        tools = get_all_tools()
        skills = get_all_skills()
        logger.info(
            f"Registry: {len(self.connectors)} connectors, "
            f"{len(self.llm_providers)} LLM providers, "
            f"{len(self.memory_backends)} memory backends, "
            f"{len(self.vault_backends)} vault backends, "
            f"{len(tools)} tools, {len(skills)} skills"
        )

    def _scan_layer(self, layer_path: Path, label: str) -> None:
        """Scan a layer directory for tools and connectors.

        Imports .py files from tools/ and connectors/ subdirectories.
        The @tool and Connector subclass decorators self-register on import.
        """
        for subdir in ("tools", "connectors", "services"):
            dir_path = layer_path / subdir
            if not dir_path.is_dir():
                continue

            # Add to sys.path so files can import each other
            dir_str = str(dir_path)
            if dir_str not in sys.path:
                sys.path.insert(0, dir_str)

            for py_file in sorted(dir_path.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._import_file(py_file, label)

    def _import_file(self, py_file: Path, label: str) -> None:
        """Import a single .py file by absolute path. @tool decorators fire on import."""
        module_name = f"kbots_{label}_{py_file.stem}"

        # Layer files can import siblings by bare name (`from stocks import …`) —
        # _scan_layer put their dir on sys.path. Without the two guards below, a
        # sibling-imported file executes TWICE (once bare, once under our prefixed
        # name), re-registering its tools and logging a spurious override.
        bare = sys.modules.get(py_file.stem)
        if bare is not None and getattr(bare, "__file__", None) == str(py_file):
            logger.debug(f"[{label}] {py_file.name} already loaded via sibling import — skipping")
            return

        existing_tools = set(_tool_registry.keys())
        existing_tool_ids = {name: id(obj) for name, obj in _tool_registry.items()}

        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
                # Alias under the bare stem so a later sibling import reuses this
                # module instead of re-executing the file. Never shadow a name some
                # other package already owns (a tool file named json.py must not
                # hijack sys.modules["json"]).
                sys.modules.setdefault(py_file.stem, mod)

                # Check for new tools and overrides
                current_tools = set(_tool_registry.keys())
                new_tools = current_tools - existing_tools
                # Tools that existed before import AND still exist = potential overrides.
                # We snapshot ids before/after to detect actual re-registration.
                overridden = {name for name in existing_tools
                              if id(_tool_registry.get(name)) != existing_tool_ids.get(name)}
                if overridden:
                    logger.warning(f"[{label}] {py_file.name} overrides existing tools: {overridden}")
                if new_tools:
                    logger.info(f"[{label}] Loaded tools from {py_file.name}: {new_tools}")

                logger.debug(f"[{label}] Imported {py_file}")
        except Exception as e:
            logger.error(f"[{label}] Failed to import {py_file}: {e}")

    def _load_agent_skills(self, agents_dir: Path) -> None:
        """Load skills from agents/*/skills/ directories."""
        from src.core.skills import _load_skill_file, _skill_registry
        for agent_skills_dir in agents_dir.glob("*/skills"):
            for yaml_file in sorted(agent_skills_dir.glob("*.yaml")):
                try:
                    skill = _load_skill_file(yaml_file)
                    agent_name = agent_skills_dir.parent.name
                    skill.name = f"{agent_name}:{skill.name}"
                    _skill_registry[skill.name] = skill
                except Exception as e:
                    logger.error(f"Failed to load skill {yaml_file}: {e}")

    def _scan_package(self, package_name: str) -> None:
        """Import all modules in a package."""
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            logger.debug(f"Package {package_name} not found, skipping")
            return

        package_path = getattr(package, "__path__", None)
        if not package_path:
            return

        for importer, module_name, is_pkg in pkgutil.iter_modules(package_path):
            if module_name.startswith("_"):
                continue
            full_name = f"{package_name}.{module_name}"
            try:
                importlib.import_module(full_name)
                logger.debug(f"Imported {full_name}")
            except Exception as e:
                logger.error(f"Failed to import {full_name}: {e}")

    def _collect_subclasses(self) -> None:
        """Find all instantiated or defined subclasses of base interfaces."""
        for cls in Connector.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("connector", ""))
            self.connectors[name] = cls
            logger.debug(f"Registered connector: {name}")

        for cls in LLMProvider.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("provider", ""))
            self.llm_providers[name] = cls
            logger.debug(f"Registered LLM provider: {name}")

        for cls in MemoryBackend.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("backend", ""))
            self.memory_backends[name] = cls
            logger.debug(f"Registered memory backend: {name}")

        for cls in VaultBackend.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("backend", ""))
            self.vault_backends[name] = cls
            logger.debug(f"Registered vault backend: {name}")

    def create_connector(self, name: str, config: dict,
                         vault: VaultBackend | None = None) -> Connector:
        """Instantiate a connector by name."""
        cls = self.connectors.get(name)
        if not cls:
            raise ValueError(f"Unknown connector: {name}. Available: {list(self.connectors.keys())}")
        return cls(config=config, vault=vault)

    def create_llm_provider(self, name: str, config: dict) -> LLMProvider:
        """Instantiate an LLM provider by name."""
        cls = self.llm_providers.get(name)
        if not cls:
            raise ValueError(f"Unknown LLM provider: {name}. Available: {list(self.llm_providers.keys())}")
        return cls(config=config)

    def create_memory_backend(self, name: str, config: dict) -> MemoryBackend:
        """Instantiate a memory backend by name."""
        cls = self.memory_backends.get(name)
        if not cls:
            raise ValueError(f"Unknown memory backend: {name}. Available: {list(self.memory_backends.keys())}")
        return cls(config=config)
