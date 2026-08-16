"""Hot-reloading layer (overlay / modules) tool files.

These are loaded by path under a synthetic module name. importlib.reload does
not work on such modules — it re-resolves the spec by name through sys.path —
so every reload of an already-loaded overlay tool used to raise "spec not found"
while the caller still logged "N tools reloaded". The net effect was that edits
to any overlay tool were silently ignored until the service restarted.
"""

import sys

import pytest

from src.core.digest import _reload_tools_dir

PREFIX = "kbots_testlayer_"


@pytest.fixture
def tools_dir(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    yield d
    for name in [m for m in sys.modules if m.startswith(PREFIX)]:
        del sys.modules[name]


def test_edit_after_first_load_is_picked_up(tools_dir, caplog):
    """The regression: the second load must see the new source.

    The two versions are deliberately the same length and written in the same
    second — that is the case the bytecode cache gets wrong, and it is the
    normal case for a reload, which fires right after an edit.
    """
    f = tools_dir / "widget.py"
    f.write_text("VALUE = 1\n")
    _reload_tools_dir(tools_dir, prefix=PREFIX)
    assert sys.modules[PREFIX + "widget"].VALUE == 1

    f.write_text("VALUE = 2\n")
    caplog.clear()
    _reload_tools_dir(tools_dir, prefix=PREFIX)

    assert sys.modules[PREFIX + "widget"].VALUE == 2
    # The old code failed silently — it logged and carried on — so asserting on
    # the value alone is not enough to prove the failure path is gone.
    assert "Failed to reload" not in caplog.text


def test_broken_edit_leaves_the_working_module_in_place(tools_dir, caplog):
    """A tool that no longer imports must not take the live one down with it."""
    f = tools_dir / "widget.py"
    f.write_text("VALUE = 1\n")
    _reload_tools_dir(tools_dir, prefix=PREFIX)

    f.write_text("raise RuntimeError('bad edit')\n")
    _reload_tools_dir(tools_dir, prefix=PREFIX)

    assert sys.modules[PREFIX + "widget"].VALUE == 1
    assert "Failed to reload" in caplog.text
    assert "bad edit" in caplog.text


def test_first_load_failure_does_not_register_a_half_built_module(tools_dir):
    """Nothing should be left behind for a module that never executed."""
    (tools_dir / "widget.py").write_text("raise RuntimeError('bad from birth')\n")
    _reload_tools_dir(tools_dir, prefix=PREFIX)
    assert PREFIX + "widget" not in sys.modules


def test_underscore_files_are_skipped(tools_dir):
    (tools_dir / "_helper.py").write_text("VALUE = 1\n")
    _reload_tools_dir(tools_dir, prefix=PREFIX)
    assert PREFIX + "_helper" not in sys.modules
