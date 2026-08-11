"""Export → import round-trip: overlay bundling + machine-specific path rewriting."""

import importlib.util
import json
from pathlib import Path

import yaml

from src.core.agent_scaffold import scaffold_agent

# migrate.py lives in scripts/ (not a package) — load it by path.
_spec = importlib.util.spec_from_file_location(
    "migrate", Path(__file__).resolve().parent.parent / "scripts" / "migrate.py")
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def _build_install(base: Path, engine: Path, overlay: Path):
    engine.mkdir()
    (overlay / "config").mkdir(parents=True)
    (overlay / "agents").mkdir()
    (overlay / "config" / "config.yaml").write_text(
        yaml.dump({"kbots": {"data_dir": "./data"}}))
    (overlay / "config" / "secrets.enc").write_bytes(b"fake-encrypted")
    scaffold_agent(overlay, "main", "MAIN", "test", tier="coordinator", engine_root=engine)


def test_detect_engine_root(tmp_path):
    engine = tmp_path / "eng"
    overlay = tmp_path / "ov"
    _build_install(tmp_path, engine, overlay)
    assert migrate._detect_engine_root(overlay) == str(engine)


def test_rewrite_paths(tmp_path):
    old_engine = tmp_path / "old-eng"
    overlay = tmp_path / "ov"
    _build_install(tmp_path, old_engine, overlay)

    old = {"engine_root": str(old_engine), "overlay": str(overlay), "home": "/old/home"}
    new = {"engine_root": "/new/engine", "overlay": str(overlay), "home": "/new/home"}
    changed = migrate._rewrite_paths(overlay, old, new)
    assert changed >= 2  # .mcp.json + settings.json at least

    mcp = json.loads((overlay / "agents/main/.mcp.json").read_text())
    server = mcp["mcpServers"]["kbots-tools"]
    assert server["cwd"] == "/new/engine"
    assert "old-eng" not in json.dumps(mcp)
    settings = json.loads((overlay / "agents/main/.claude/settings.json").read_text())
    assert settings["env"]["PATH"].startswith("/new/engine/.venv/bin")


def test_export_import_round_trip(tmp_path, monkeypatch):
    # --- old machine ---
    old_engine = tmp_path / "old-engine"
    old_overlay = tmp_path / "old-overlay"
    _build_install(tmp_path, old_engine, old_overlay)
    key = tmp_path / "old-key"
    key.write_text("passphrase")
    monkeypatch.setenv("KBOTS_VAULT_KEY_FILE", str(key))

    out = tmp_path / "out"
    export_args = migrate.argparse.Namespace(
        overlay=str(old_overlay), out=str(out), with_key=True, timestamp="test")
    migrate.cmd_export(export_args)
    bundle = out / "kbots-export-test.tar.gz"
    assert bundle.exists()

    # --- new machine: different engine + overlay paths, different key location ---
    new_engine = tmp_path / "new-engine"
    new_overlay = tmp_path / "new-overlay"
    new_key = tmp_path / "new-key"
    monkeypatch.setenv("KBOTS_VAULT_KEY_FILE", str(new_key))
    import_args = migrate.argparse.Namespace(
        bundle=str(bundle), overlay=str(new_overlay), engine=str(new_engine))
    migrate.cmd_import(import_args)

    # Overlay restored, paths rewritten, vault + key + secret intact
    assert (new_overlay / "config" / "secrets.enc").read_bytes() == b"fake-encrypted"
    mcp = json.loads((new_overlay / "agents/main/.mcp.json").read_text())
    server = mcp["mcpServers"]["kbots-tools"]
    assert server["cwd"] == str(new_engine)
    assert server["env"]["KBOTS_OVERLAY"] == str(new_overlay)
    assert "old-engine" not in json.dumps(mcp)
    assert "old-overlay" not in json.dumps(mcp)
    assert new_key.read_text() == "passphrase"  # vault key restored


def test_export_excludes_junk(tmp_path):
    engine = tmp_path / "eng"
    overlay = tmp_path / "ov"
    _build_install(tmp_path, engine, overlay)
    # Junk that must NOT be bundled
    (overlay / "tmp").mkdir()
    (overlay / "tmp" / "scratch.txt").write_text("x")
    (overlay / "data").mkdir()
    (overlay / "data" / "models").mkdir()
    (overlay / "data" / "models" / "big.onnx").write_text("heavy")
    (overlay / "data" / "kbots.db").write_text("keepme")
    (overlay / "run.log").write_text("noise")

    out = tmp_path / "out"
    migrate.cmd_export(migrate.argparse.Namespace(
        overlay=str(overlay), out=str(out), with_key=False, timestamp="t"))

    import tarfile
    with tarfile.open(out / "kbots-export-t.tar.gz") as tar:
        names = tar.getnames()
    assert not any("tmp/scratch" in n for n in names)
    assert not any("models" in n for n in names)
    assert not any(n.endswith(".log") for n in names)
    assert any("kbots.db" in n for n in names)  # memory DB IS kept
