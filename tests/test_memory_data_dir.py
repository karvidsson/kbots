"""Both memory stores must live where `kbots.data_dir` says they do.

Regression (found 2026-08-17, root-caused 2026-08-20): `defaults.memory` carries
no `path`, so SQLiteMemory fell back to the relative "data/memory.db" and
resolved it against PROJECT_ROOT. The graph store did the same with a relative
`graph.path`. `data_dir` therefore never governed either store.

Pointing data_dir at an overlay moved the training corpus, the audit log and
version.json, and silently left memory behind. The result was two divergent
stores with the config naming the one nothing was writing to: 230 live rows
against 191 stale ones. A scrub or an audit run against the configured path
read the stale database and reported success.

The main process and the MCP subprocess resolve this independently, so they are
pinned here to agree — disagreement would mean an agent's tool calls read a
different store from the one its turns were recorded against.
"""

from pathlib import Path

from src.core.base import PROJECT_ROOT, memory_config, resolve_data_dir, warn_on_split_store

OVERLAY = {"kbots": {"data_dir": "/tmp/kbots-overlay-test/data"},
           "defaults": {"memory": {"backend": "sqlite",
                                   "graph": {"enabled": True}}}}


def test_sqlite_path_follows_data_dir():
    cfg = memory_config(OVERLAY)
    assert cfg["path"] == "/tmp/kbots-overlay-test/data/memory.db"


def test_graph_path_follows_data_dir_too():
    cfg = memory_config(OVERLAY)
    assert cfg["graph"]["path"] == "/tmp/kbots-overlay-test/data/graph/memory.lbdb"


def test_a_relative_store_path_is_warned_about_not_silently_reinterpreted(caplog):
    """The live config pinned graph.path to a relative value, which reads as
    configured and then lands outside data_dir. Rewriting it silently would be
    its own surprise, so it has to be said out loud."""
    with caplog.at_level("WARNING"):
        cfg = memory_config({"kbots": {"data_dir": "/tmp/elsewhere"},
                             "defaults": {"memory": {
                                 "graph": {"path": "data/graph/memory.lbdb"}}}})
    assert cfg["graph"]["path"] == "data/graph/memory.lbdb", "not rewritten"
    assert any("relative" in r.message and "graph.path" in r.message
               for r in caplog.records), caplog.records


def test_neither_store_lands_under_the_repo_when_data_dir_is_elsewhere():
    cfg = memory_config(OVERLAY)
    for p in (cfg["path"], cfg["graph"]["path"]):
        assert not str(p).startswith(str(PROJECT_ROOT)), p


def test_an_explicit_path_still_wins():
    cfg = memory_config({**OVERLAY, "defaults": {"memory": {
        "path": "/srv/pinned/memory.db",
        "graph": {"path": "/srv/pinned/g.lbdb"}}}})
    assert cfg["path"] == "/srv/pinned/memory.db"
    assert cfg["graph"]["path"] == "/srv/pinned/g.lbdb"


def test_default_deployment_is_unchanged():
    """With no data_dir set, paths stay where they have always been."""
    cfg = memory_config({"defaults": {"memory": {}}})
    assert Path(cfg["path"]) == PROJECT_ROOT / "data" / "memory.db"


def test_a_relative_data_dir_resolves_against_the_repo_not_the_cwd():
    """MCP subprocesses inherit CWD from the agent's project dir."""
    cfg = memory_config({"kbots": {"data_dir": "./var"}, "defaults": {"memory": {}}})
    assert Path(cfg["path"]) == PROJECT_ROOT / "var" / "memory.db"


def test_absent_graph_config_is_not_invented():
    cfg = memory_config({"kbots": {"data_dir": "/tmp/x"}, "defaults": {"memory": {}}})
    assert "graph" not in cfg


def test_main_and_mcp_resolve_to_the_same_file():
    """Both entry points must go through the one helper, not two copies."""
    main_src = (PROJECT_ROOT / "src" / "main.py").read_text()
    mcp_src = (PROJECT_ROOT / "src" / "mcp_server.py").read_text()
    for src, name in ((main_src, "main.py"), (mcp_src, "mcp_server.py")):
        assert "memory_config" in src, f"{name} does not use the shared resolver"
    # The old shape: reading defaults.memory straight into a backend.
    assert "SQLiteMemory(config=mem_cfg)" in mcp_src
    assert "_memory_config(config)" in mcp_src


def test_split_store_is_reported_when_a_legacy_file_survives(tmp_path, monkeypatch):
    legacy = PROJECT_ROOT / "data" / "memory.db"
    cfg = {"kbots": {"data_dir": str(tmp_path)}}
    stale = warn_on_split_store(cfg)
    if legacy.is_file() and legacy.stat().st_size > 0:
        assert str(legacy) in stale, "a surviving legacy store must be named"
    else:
        assert str(legacy) not in stale


def test_no_split_warning_when_the_data_dir_is_the_default_one():
    """Otherwise a stock install would warn about its own live database."""
    assert warn_on_split_store({"kbots": {"data_dir": str(PROJECT_ROOT / "data")}}) == []
    assert warn_on_split_store({}) == []


def test_resolve_data_dir_is_always_absolute():
    for cfg in ({}, {"kbots": {}}, {"kbots": {"data_dir": "rel"}},
                {"kbots": {"data_dir": "/abs"}}):
        assert resolve_data_dir(cfg).is_absolute()
