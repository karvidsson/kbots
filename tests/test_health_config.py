"""The wizard generates health-config.yaml — without it, the nightly health
audit and the system_audit tool exit 1, and nothing ever shipped the file, so
no deployment had a working audit until someone authored it by hand."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "setup_health", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def _state(tmp_path, **extra):
    return {"overlay": tmp_path / "overlay", **extra}


def test_paths_point_at_this_deployment(tmp_path):
    cfg = setup.build_health_config(_state(tmp_path))
    overlay = str(tmp_path / "overlay")
    assert cfg["paths"]["overlay"] == overlay
    assert cfg["paths"]["vault"].startswith(overlay)
    assert cfg["paths"]["kbots_home"] == str(setup.ENGINE_ROOT)


def test_rescue_service_audited_when_ops_configured(tmp_path):
    cfg = setup.build_health_config(_state(tmp_path, ops_profile="rescue"))
    assert cfg["services"] == ["kbots", "kbots-rescue"]
    assert setup.build_health_config(_state(tmp_path))["services"] == ["kbots"]


def test_every_stored_token_is_audited(tmp_path):
    cfg = setup.build_health_config(_state(
        tmp_path, bot_token_key="discord-token",
        extra_bots={"engineer": "discord-engineer"}))
    assert cfg["vault_expected_keys"] == ["discord-token", "discord-engineer"]


def test_skipped_discord_expects_no_token(tmp_path):
    cfg = setup.build_health_config(_state(tmp_path, discord_skip=True))
    assert cfg["vault_expected_keys"] == []


def test_thresholds_match_script_defaults(tmp_path):
    t = setup.build_health_config(_state(tmp_path))["thresholds"]
    assert t == {"disk_percent": 80, "ram_percent": 85,
                 "swap_percent": 70, "load_per_cpu": 1.5}
