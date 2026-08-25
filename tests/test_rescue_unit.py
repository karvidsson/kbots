"""The rescue (ops) unit must be rendered, never raw-copied.

A raw copy of config/kbots-rescue.service boots with %h = root's home (wrong
vault key path, wrong Claude credentials) and no KBOTS_OVERLAY (no config, no
agents.rescue.yaml) — the exact crash-loop a first ops-instance install hit.
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("setup_rescue", _ROOT / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

TEMPLATE = (_ROOT / "config" / "kbots-rescue.service").read_text()
ENV = ["Environment=KBOTS_OVERLAY=/srv/overlay"]
UV = "/home/kbots/.local/bin/uv"


def _render() -> str:
    return setup.render_rescue_unit(TEMPLATE, Path("/srv/overlay"), ENV, UV)


def test_no_percent_h_survives():
    rendered = _render()
    assert "%h" not in rendered


def test_overlay_env_injected():
    assert "Environment=KBOTS_OVERLAY=/srv/overlay" in _render()


def test_execstart_uses_resolved_uv():
    rendered = _render()
    assert f"ExecStart={UV} run --no-sync python -m src.main --profile rescue" in rendered


def test_engine_root_replaces_opt_kbots():
    rendered = _render()
    assert f"WorkingDirectory={setup.ENGINE_ROOT}" in rendered


def test_stays_unsandboxed():
    """render_service_unit must not smuggle the main unit's sandbox in —
    the rescue instance exists precisely because it has none."""
    rendered = _render()
    for directive in ("ProtectSystem", "ReadWritePaths", "ReadOnlyPaths",
                      "NoNewPrivileges", "CapabilityBoundingSet"):
        assert directive not in rendered, directive


def test_service_identity_preserved():
    rendered = _render()
    assert "User=kbots" in rendered
    assert "SyslogIdentifier=kbots-rescue" in rendered
