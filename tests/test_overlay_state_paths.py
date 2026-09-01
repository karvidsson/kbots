"""Small shared state files live under the overlay's data/, never its root.

Five modules each resolved their own path against the overlay ROOT. A hardened
service unit grants ReadWritePaths to the subdirectories it needs, which leaves
that root read-only: inside the service every write failed while the identical
code worked from a shell. These pin the location, and pin that an install which
predates the move keeps its state.
"""

import json
from pathlib import Path

import pytest

from src.core import feedback_map, runtime_state, schedules, session_consent, triggers
from src.core.base import (
    overlay_state_legacy_path,
    overlay_state_path,
    overlay_state_read_path,
)


@pytest.fixture(autouse=True)
def _isolate_runtime_flags():
    """Opt out of conftest's runtime-flag isolation.

    That fixture redirects runtime_state's path resolvers at a tmp file so no
    test reads the deployment's live flags. These tests ARE the path resolvers,
    so they need the real ones. Shadowing the fixture by name is pytest's
    supported way to say that, and it keeps the exception next to the reason
    rather than as a list of names in conftest.
    """
    return None


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


# --- the helper -------------------------------------------------------------

def test_write_path_is_under_data(overlay):
    assert overlay_state_path("x.json") == overlay / "data" / "x.json"
    assert overlay_state_legacy_path("x.json") == overlay / "x.json"


def test_no_overlay_means_no_path(monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    assert overlay_state_path("x.json") is None
    assert overlay_state_read_path("x.json") is None


def test_read_prefers_current_then_legacy_then_nothing(overlay):
    assert overlay_state_read_path("x.json") is None

    (overlay / "x.json").write_text("{}")
    assert overlay_state_read_path("x.json") == overlay / "x.json"

    (overlay / "data").mkdir()
    (overlay / "data" / "x.json").write_text("{}")
    assert overlay_state_read_path("x.json") == overlay / "data" / "x.json"


# --- every module that uses it ---------------------------------------------

WRITERS = [
    ("schedules.json", lambda: schedules.create_schedule(
        "a", "c", "p", "u", spec_type="cron", spec="0 8 * * *", now=0.0)),
    ("runtime.json", lambda: runtime_state.set_flag("hitl", True)),
    ("triggers.json", lambda: triggers.create_trigger("e", "a", "c", "p", "u")),
    ("session_consent.json", lambda: session_consent.grant("a", "cmd")),
    ("feedback_map.json", lambda: feedback_map.record("m1", "a", ["l1"])),
]


@pytest.mark.parametrize("filename,write", WRITERS, ids=[w[0] for w in WRITERS])
def test_writes_land_under_data_not_the_overlay_root(overlay, filename, write):
    write()
    assert (overlay / "data" / filename).exists()
    assert not (overlay / filename).exists()


# --- upgrade: state written before the move keeps governing -----------------

def test_legacy_schedules_are_read_then_carried_forward(overlay):
    (overlay / "schedules.json").write_text(json.dumps({"enabled": True, "schedules": [
        {"id": "old", "agent_id": "a", "channel_id": "c", "instruction": "p",
         "spec_type": "cron", "spec": "0 8 * * *", "connector": "discord"}]}))

    assert [s["id"] for s in schedules.list_schedules()] == ["old"]

    schedules.create_schedule("a", "c", "p", "u", spec_type="cron",
                              spec="0 9 * * *", now=0.0)
    migrated = json.loads((overlay / "data" / "schedules.json").read_text())
    assert "old" in {s["id"] for s in migrated["schedules"]}
    assert len(migrated["schedules"]) == 2


def test_legacy_runtime_flags_are_read_then_carried_forward(overlay):
    (overlay / "runtime.json").write_text(json.dumps({"hitl": True}))
    assert runtime_state.get_flag("hitl") is True

    runtime_state.set_flag("schedule_board", "c1")
    carried = json.loads((overlay / "data" / "runtime.json").read_text())
    assert carried == {"hitl": True, "schedule_board": "c1"}


# --- the reflector's scratch dir, same class of defect --------------------

def test_reflector_work_dir_honours_the_kbots_tmp_override(overlay, monkeypatch):
    """It hand-rolled $KBOTS_OVERLAY/tmp, bypassing the one override a host with
    a read-only overlay root has. The mkdir is the first thing it does."""
    from src.core.reflector import Reflector

    elsewhere = overlay / "writable"
    monkeypatch.setenv("KBOTS_TMP", str(elsewhere))

    d = Reflector._work_dir(object.__new__(Reflector))
    assert d == str(elsewhere / "reflector")
    assert (elsewhere / "reflector").is_dir()
    assert not (overlay / "tmp").exists()


def test_clearing_a_legacy_only_flag_writes_the_current_file(overlay):
    """Otherwise the flag reads cleared, the next set_flag migrates the legacy
    copy forward, and the cleared flag comes back."""
    (overlay / "runtime.json").write_text(json.dumps({"hitl": True}))

    runtime_state.clear_flag("hitl")
    assert (overlay / "data" / "runtime.json").exists()
    assert runtime_state.get_flag("hitl", "unset") == "unset"

    runtime_state.set_flag("other", 1)
    assert runtime_state.get_flag("hitl", "unset") == "unset"


# --- the unit's writable list and the write path are one thing -------------

def test_every_state_path_is_inside_the_rendered_read_write_paths(overlay):
    """The durable fix. setup.py used to restate `overlay / "data"` by hand
    next to the helper's own `Path(overlay) / "data"`; nothing made them agree.
    Now the unit derives the directory from src.core.base, and this pins it:
    a state file the helper can produce is, by construction, under a path
    the rendered unit grants ReadWritePaths to.
    """
    import setup as setup_wizard
    from src.core import tool_scope

    template = setup_wizard.ENGINE_ROOT / "config" / "kbots.service"
    rendered = setup_wizard.render_service_unit(
        template.read_text(), overlay, [f"Environment=KBOTS_OVERLAY={overlay}"])
    rw = [ln for ln in rendered.splitlines()
          if ln.startswith("ReadWritePaths=")][0].split("=", 1)[1].split()
    # Only the grants the unit derives from the overlay. /tmp and the home
    # dotfile dirs are always-writable system paths, and on Linux CI the
    # pytest tmp_path itself lives under /tmp — which would make the overlay
    # root look "granted" for a reason that has nothing to do with the unit.
    granted = [Path(p) for p in rw if Path(p).is_relative_to(overlay)]

    def is_granted(path: Path) -> bool:
        return any(path == g or g in path.parents for g in granted)

    for name in ("schedules.json", "runtime.json", "triggers.json",
                 "session_consent.json", "feedback_map.json", "anything-new.json"):
        path = overlay_state_path(name)
        assert is_granted(path), f"{path} is not under any ReadWritePaths entry"
        assert not is_granted(overlay_state_legacy_path(name)), \
            "the overlay root must stay read-only — only data/ is granted"

    assert is_granted(tool_scope._scope_path())
