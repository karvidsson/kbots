#!/usr/bin/env python3
"""kbots migration — export a deployment and import it on another machine.

An overlay is self-contained (config, vault, agents, memory), so moving a
deployment is really just moving the overlay — plus rewriting the handful of
absolute paths the wizard baked in (engine root, overlay, home) for the new
machine.

    # On the old machine:
    uv run python scripts/migrate.py export --overlay <overlay> [--out DIR] [--with-key]

    # On the new machine (after cloning the engine):
    uv run python scripts/migrate.py import <bundle.tar.gz> \
        --overlay <new-overlay> --engine <new-engine-root>

The bundle is a tar.gz of the overlay + a manifest recording the original
paths. Import extracts it, rewrites paths, and restores the vault key (if it
was included) or leaves you to re-enter your passphrase.
"""

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.base import resolve_vault_key_file  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


def ok(m): print(f"  {GREEN}✓{RESET} {m}")
def warn(m): print(f"  {YELLOW}!{RESET} {m}")
def err(m): print(f"  {RED}✗{RESET} {m}")
def info(m): print(f"  {DIM}{m}{RESET}")


# Overlay contents worth moving vs regenerable/machine-local junk.
_EXCLUDE_DIRS = {"__pycache__", "models", "tmp"}      # models re-download; tmp is scratch
_EXCLUDE_SUFFIX = {".log", ".pyc"}


def _tar_filter(tarinfo: tarfile.TarInfo):
    parts = Path(tarinfo.name).parts
    if any(p in _EXCLUDE_DIRS for p in parts):
        return None
    if Path(tarinfo.name).suffix in _EXCLUDE_SUFFIX:
        return None
    return tarinfo


def _detect_engine_root(overlay: Path) -> str:
    """Read the engine root out of an agent's .mcp.json (its cwd)."""
    for mcp in overlay.glob("agents/*/.mcp.json"):
        try:
            data = json.loads(mcp.read_text())
            cwd = data.get("mcpServers", {}).get("kbots-tools", {}).get("cwd")
            if cwd:
                return cwd
        except (json.JSONDecodeError, OSError):
            continue
    return str(PROJECT_ROOT)


def cmd_export(args):
    overlay = Path(args.overlay).expanduser().resolve()
    if not (overlay / "config").is_dir():
        err(f"Not an overlay (no config/): {overlay}")
        sys.exit(1)

    engine_root = _detect_engine_root(overlay)
    manifest = {
        "schema": 1,
        "exported_at": args.timestamp or "",
        "engine_root": engine_root,
        "overlay": str(overlay),
        "home": str(Path.home()),
        "kbots_modules": os.environ.get("KBOTS_MODULES", ""),
    }

    out_dir = Path(args.out).expanduser().resolve() if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = args.timestamp or "export"
    bundle = out_dir / f"kbots-export-{stamp}.tar.gz"

    key_file = resolve_vault_key_file()
    include_key = args.with_key and key_file.exists()
    manifest["has_vault_key"] = include_key

    info(f"Bundling overlay: {overlay}")
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(overlay, arcname="overlay", filter=_tar_filter)
        # manifest
        mtmp = out_dir / ".manifest.json"
        mtmp.write_text(json.dumps(manifest, indent=2))
        tar.add(mtmp, arcname="manifest.json")
        mtmp.unlink()
        if include_key:
            tar.add(key_file, arcname="vault-key")

    ok(f"Exported: {bundle}")
    info(f"Size: {bundle.stat().st_size / 1024:.0f} KB")
    if include_key:
        warn("Bundle INCLUDES your vault passphrase key — it can unlock your "
             "secrets. Store/transfer it securely and delete it after import.")
    else:
        info("Vault key NOT included — on the new machine you'll re-enter your "
             "vault passphrase (or pass --with-key to include it).")
    print()
    info("Move it to the new machine, clone the engine there, then run:")
    info(f"  uv run python scripts/migrate.py import {bundle.name} \\")
    info("      --overlay <new-overlay-path> --engine <new-engine-root>")


def _rewrite_paths(overlay: Path, old: dict, new: dict) -> int:
    """Replace old absolute roots with new ones across path-bearing files."""
    targets = [
        overlay / "config" / "config.yaml",
        *overlay.glob("config/agents*.yaml"),
        *overlay.glob("agents/*/.mcp.json"),
        *overlay.glob("agents/*/.claude/settings.json"),
    ]
    replacements = [
        (old["engine_root"], new["engine_root"]),
        (old["overlay"], new["overlay"]),
        (old["home"], new["home"]),
    ]
    changed = 0
    for f in targets:
        if not f.is_file():
            continue
        text = original = f.read_text()
        for a, b in replacements:
            if a and a != b:
                text = text.replace(a, b)
        if text != original:
            f.write_text(text)
            changed += 1
    return changed


def cmd_import(args):
    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.exists():
        err(f"Bundle not found: {bundle}")
        sys.exit(1)
    new_overlay = Path(args.overlay).expanduser().resolve()
    new_engine = Path(args.engine).expanduser().resolve() if args.engine else PROJECT_ROOT

    if new_overlay.exists() and any(new_overlay.iterdir()):
        err(f"Target overlay exists and is not empty: {new_overlay}")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(bundle, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        manifest = json.loads((tmp / "manifest.json").read_text())

        # Move the overlay tree into place
        new_overlay.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp / "overlay"), str(new_overlay))
        ok(f"Overlay restored to {new_overlay}")

        # Rewrite machine-specific paths
        new = {"engine_root": str(new_engine), "overlay": str(new_overlay),
               "home": str(Path.home())}
        old = {"engine_root": manifest["engine_root"], "overlay": manifest["overlay"],
               "home": manifest["home"]}
        n = _rewrite_paths(new_overlay, old, new)
        ok(f"Rewrote paths in {n} file(s) for this machine")

        # Restore the vault key if it was bundled
        key_src = tmp / "vault-key"
        if key_src.exists():
            key_dst = resolve_vault_key_file()
            key_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(key_src, key_dst)
            key_dst.chmod(0o600)
            ok(f"Vault key restored to {key_dst}")
        else:
            warn("No vault key in bundle — you'll be prompted for your vault "
                 "passphrase when the engine starts (your secrets.enc is intact).")

    print()
    ok("Import complete.")
    info(f"Set KBOTS_OVERLAY={new_overlay} (add it to your shell profile), then")
    info("run the engine directly, or `uv run python setup.py` to install the")
    info("service — it detects the existing overlay and won't overwrite it.")


def main():
    p = argparse.ArgumentParser(prog="migrate.py", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("export", help="Bundle an overlay for another machine")
    pe.add_argument("--overlay", required=True, help="Overlay directory to export")
    pe.add_argument("--out", help="Output directory (default: cwd)")
    pe.add_argument("--with-key", action="store_true",
                    help="Include the vault passphrase key (sensitive!)")
    pe.add_argument("--timestamp", help="Stamp for the bundle filename")
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser("import", help="Restore an exported bundle here")
    pi.add_argument("bundle", help="Path to the .tar.gz bundle")
    pi.add_argument("--overlay", required=True, help="Where to restore the overlay")
    pi.add_argument("--engine", help="Engine root on this machine (default: this repo)")
    pi.set_defaults(func=cmd_import)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
