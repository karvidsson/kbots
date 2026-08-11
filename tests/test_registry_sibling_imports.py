"""Layer files that import siblings by bare name must execute exactly once.

_scan_layer puts a layer's tools/ dir on sys.path so files can import each
other (`from stocks import …`). Before the guards in _import_file, that made
the sibling-imported file execute twice — once under the bare name (triggered
by the sibling import) and once under the registry's prefixed module name —
re-registering its tools and logging a spurious override. The two cases differ
by scan order, so both are covered:

- consumer scans FIRST (consumer < helper alphabetically): the bare import
  loads the helper before the scan reaches it → the scan must skip it.
- helper scans FIRST (helper < consumer): the scan loads it under the prefixed
  name → the later bare import must hit the sys.modules alias, not the file.

Each generated file appends a line to a marker file at import time; one line
per marker = one execution.
"""

import sys

from src.core.registry import Registry
from src.core.tools import _tool_registry


def _write_layer(tmp_path, files: dict[str, str]):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in files.items():
        (tools / name).write_text(body)
    return tmp_path


def _cleanup(*module_names):
    for m in module_names:
        sys.modules.pop(m, None)
    for t in list(_tool_registry):
        if t.startswith("sib_"):
            del _tool_registry[t]


def _exec_counts(tmp_path, names):
    return {n: (tmp_path / f"{n}.marker").read_text().count("x") for n in names}


def test_consumer_scanned_before_helper(tmp_path):
    """a_consumer.py imports z_helper by bare name before the scan reaches it."""
    marker = tmp_path / "{stem}.marker"
    layer = _write_layer(tmp_path, {
        "a_consumer.py": (
            f"open(r'{marker}'.format(stem='a_consumer'), 'a').write('x')\n"
            "from z_helper import shared\n"
            "from src.core.tools import tool\n"
            "from src.core.base import ToolContext\n"
            "@tool(name='sib_consumer', description='d')\n"
            "async def sib_consumer(ctx: ToolContext) -> str:\n"
            "    return shared()\n"
        ),
        "z_helper.py": (
            f"open(r'{marker}'.format(stem='z_helper'), 'a').write('x')\n"
            "from src.core.tools import tool\n"
            "from src.core.base import ToolContext\n"
            "def shared() -> str:\n"
            "    return 'ok'\n"
            "@tool(name='sib_helper', description='d')\n"
            "async def sib_helper(ctx: ToolContext) -> str:\n"
            "    return shared()\n"
        ),
    })
    try:
        Registry()._scan_layer(layer, "sibtest")
        assert _exec_counts(tmp_path, ["a_consumer", "z_helper"]) == {
            "a_consumer": 1, "z_helper": 1}
        assert "sib_consumer" in _tool_registry and "sib_helper" in _tool_registry
    finally:
        _cleanup("a_consumer", "z_helper",
                 "kbots_sibtest_a_consumer", "kbots_sibtest_z_helper")
        sys.path.remove(str(layer / "tools"))


def test_helper_scanned_before_consumer(tmp_path):
    """a_helper.py is scanned first; z_consumer's bare import must reuse it."""
    marker = tmp_path / "{stem}.marker"
    layer = _write_layer(tmp_path, {
        "a_helper.py": (
            f"open(r'{marker}'.format(stem='a_helper'), 'a').write('x')\n"
            "def shared() -> str:\n"
            "    return 'ok'\n"
        ),
        "z_consumer.py": (
            f"open(r'{marker}'.format(stem='z_consumer'), 'a').write('x')\n"
            "from a_helper import shared\n"
            "from src.core.tools import tool\n"
            "from src.core.base import ToolContext\n"
            "@tool(name='sib_consumer2', description='d')\n"
            "async def sib_consumer2(ctx: ToolContext) -> str:\n"
            "    return shared()\n"
        ),
    })
    try:
        Registry()._scan_layer(layer, "sibtest2")
        assert _exec_counts(tmp_path, ["a_helper", "z_consumer"]) == {
            "a_helper": 1, "z_consumer": 1}
        assert "sib_consumer2" in _tool_registry
    finally:
        _cleanup("a_helper", "z_consumer",
                 "kbots_sibtest2_a_helper", "kbots_sibtest2_z_consumer")
        sys.path.remove(str(layer / "tools"))


def test_bare_alias_never_shadows_existing_module(tmp_path):
    """A tool file named like an already-imported module must not hijack it."""
    import json as real_json
    layer = _write_layer(tmp_path, {
        "json.py": "PROBE = True\n",
    })
    try:
        Registry()._scan_layer(layer, "sibtest3")
        assert sys.modules["json"] is real_json          # stdlib untouched
        assert "kbots_sibtest3_json" in sys.modules      # still loaded, prefixed
    finally:
        _cleanup("kbots_sibtest3_json")
        sys.path.remove(str(layer / "tools"))
