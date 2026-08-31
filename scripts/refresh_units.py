#!/usr/bin/env python3
"""Re-render this install's service unit from the template it was generated from.

WHY THIS EXISTS

A unit fix could be written, reviewed, merged, pulled by every machine — and
still be running nowhere. `update.sh` and `self-deploy.sh` pull code, sync
dependencies and restart; neither had any notion of a systemd unit. The only
thing that ever turned `config/kbots.service` into `/etc/systemd/system/
kbots.service` was the setup wizard, which nobody re-runs on a working box. So
a commit like "grant the service home in ReadWritePaths so ~/.claude.json can
be rewritten" fixed a file no deployed machine would ever receive.

That is worse than an unfixed bug. It reads as fixed in the history, the report
gets closed, and the sandbox failure keeps happening on every host — which is
exactly how the same EROFS bug got diagnosed four separate times.

WHAT IT DOES

Renders the template through the same functions the wizard uses
(setup.build_service_unit / build_timer_unit / build_launchd_plist), carrying
over the install-specific values already baked into the generated unit — the uv
path, KBOTS_OVERLAY, KBOTS_MODULES, the service account. So it needs no wizard
state and asks no questions: it answers "what unit would setup generate here
today", and installs it only if that differs from what is there.

    uv run --no-sync python scripts/refresh_units.py [--reload] [--dry-run]

    --reload   also make the running manager pick it up (daemon-reload on
               systemd; bootout+bootstrap on launchd, which is the only way a
               plist edit takes effect — `kickstart -k` re-runs the OLD one).
    --dry-run  report the diff and change nothing.

Exit codes: 0 nothing changed, CHANGED (10) a unit was updated, 1 a unit was
written but could not be made live — the one state where the file on disk and
the process actually running disagree. 10 rather than 0 because a caller that
decides whether to restart has to know: update.sh skips the restart when only
skills changed, and a new unit that is never restarted into is not installed.

WHAT IT DELIBERATELY WILL NOT DO

Overwrite a hand-edited live unit. install-systemd.sh makes /etc/systemd/system/
kbots.service a SYMLINK into <overlay>/systemd/, so writing the overlay copy is
the whole install. If that path has been replaced by a regular file — `sed -i`
does this, silently orphaning the overlay copy — this reports it and stops,
because the local edit is likelier to be load-bearing than not. Drop-ins under
kbots.service.d/ are the supported way to keep a local override; they survive
this and are reported so the difference is never a mystery.

Run it from a shell, not from inside the service: a hardened unit mounts the
engine root and the overlay read-only, so the service cannot rewrite its own
unit — which is the point of the sandbox, not a defect in it.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup  # noqa: E402  (needs the path above)

ENGINE_ROOT = setup.ENGINE_ROOT
LAUNCHD_LABEL = "com.kbots.agent"

#: exit status meaning "a unit was updated" — see the module docstring.
CHANGED = 10


def log(msg: str) -> None:
    print(f"[refresh-units] {msg}")


# --- reading the install back out of its own generated unit ------------------

def systemd_install_values(unit_text: str) -> tuple[list[str], str]:
    """The env lines and uv path to carry forward, read from the live unit.

    The alternative is re-deriving them, and re-derivation is how a refresh
    turns into a silent reconfiguration: `shutil.which("uv")` under a different
    shell finds a different uv, and the unit quietly starts running a binary
    nobody chose. What is already installed is the record of what was chosen.
    """
    env_lines = [ln for ln in unit_text.splitlines()
                 if ln.startswith("Environment=KBOTS_")]
    uv_path = "/usr/local/bin/uv"
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            uv_path = line.split("=", 1)[1].split()[0]
            break
    return env_lines, uv_path


def launchd_install_values(plist_path: Path) -> dict:
    """Same, for a plist. Parsed as a plist rather than regexed."""
    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)
    env = data.get("EnvironmentVariables", {})
    args = data.get("ProgramArguments", []) or [""]
    root = data.get("WorkingDirectory")
    return {
        "uv_path": args[0],
        "home": Path(env.get("HOME", str(Path.home()))),
        "path_env": env.get("PATH", ""),
        "overlay": Path(env["KBOTS_OVERLAY"]) if env.get("KBOTS_OVERLAY") else None,
        "modules": env.get("KBOTS_MODULES", ""),
        "engine_root": Path(root) if root else None,
    }


def installed_engine_root(unit_text: str) -> Path | None:
    """The checkout the installed unit runs the service FROM.

    Rendering fills WorkingDirectory from the checkout this script lives in, so
    running a refresh out of a development clone would silently repoint the
    live service at that clone — shipping whatever is checked out there, gated
    by nothing. Caught in the first dry-run of this script, which offered to
    move a working install from ~/kbots to ~/dev/kbots.

    A refresh re-renders a unit. It does not relocate an install.
    """
    for line in unit_text.splitlines():
        if line.startswith("WorkingDirectory="):
            return Path(line.split("=", 1)[1].strip())
    return None


def assert_install_root(installed_root: Path | None) -> bool:
    """False (with an explanation) if we would move the install."""
    if installed_root is None or installed_root == ENGINE_ROOT:
        return True
    log(f"REFUSING: the installed unit runs from {installed_root}, but this is")
    log(f"  {ENGINE_ROOT}. Refreshing here would repoint the service at this")
    log(f"  checkout. Run it from the install:  cd {installed_root} && "
        "scripts/self-deploy.sh")
    return False


def resolve_overlay(generated: Path | None) -> Path | None:
    """The overlay this install uses. The unit's own answer beats the caller's.

    $KBOTS_OVERLAY in the shell running a deploy is not necessarily the one the
    SERVICE runs with, and it is the service's that the unit has to match.
    """
    if generated and generated.exists():
        prefix = "Environment=KBOTS_OVERLAY="
        for line in generated.read_text().splitlines():
            if line.startswith(prefix):
                # Not split("=", 1): that keeps the KBOTS_OVERLAY= half and
                # yields a path named "KBOTS_OVERLAY=/srv/overlay".
                return Path(line[len(prefix):].strip())
    env = os.environ.get("KBOTS_OVERLAY", "")
    return Path(env) if env else None


# --- writing -----------------------------------------------------------------

def write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    """True if the file's content is not already `content`."""
    if path.exists() and path.read_text() == content:
        return False
    log(f"{'would update' if dry_run else 'updating'} {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return True


def ensure_writable_dirs(overlay: Path) -> None:
    """Every ReadWritePaths entry must exist before the unit starts.

    systemd builds the mount namespace BEFORE exec, so a missing entry fails at
    step NAMESPACE with status=226 and restart-loops without ever reaching the
    code that would have created it. A refresh that adds a path to the grant
    without creating it would ship exactly that crash loop.
    """
    for d in setup.service_writable_dirs(overlay):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"WARNING: could not create {d}: {e} — the service may fail at step NAMESPACE")


# --- systemd -----------------------------------------------------------------

def refresh_systemd(dry_run: bool, reload: bool) -> int:
    env_overlay = resolve_overlay(None)
    unit = (env_overlay / "systemd" / "kbots.service") if env_overlay else None
    if unit is None or not unit.exists():
        log("no generated unit for this install — nothing to refresh")
        return 0
    generated_dir = unit.parent
    if not assert_install_root(installed_engine_root(unit.read_text())):
        return 1

    # The unit's own KBOTS_OVERLAY, which is the one the SERVICE runs with.
    overlay = resolve_overlay(unit) or env_overlay

    live = Path("/etc/systemd/system/kbots.service")
    if live.exists() and not live.is_symlink():
        log(f"WARNING: {live} is a regular file, not a symlink into {generated_dir}.")
        log("  A hand-edit replaced the symlink, so the generated unit is orphaned and")
        log("  refreshing it would change nothing. Not touching the local edit.")
        log("  To adopt the generated unit again, or better, move the local change into")
        log(f"  a drop-in:  sudo ln -sfn {unit} {live}")
        log("              /etc/systemd/system/kbots.service.d/10-local.conf")
        return 0

    dropins = Path("/etc/systemd/system/kbots.service.d")
    if dropins.is_dir():
        names = sorted(p.name for p in dropins.glob("*.conf"))
        if names:
            log(f"drop-ins present and still applied on top: {', '.join(names)}")

    env_lines, uv_path = systemd_install_values(unit.read_text())
    template = ENGINE_ROOT / "config" / "kbots.service"
    if not template.exists():
        log(f"template missing: {template}")
        return 0

    changed = False
    rendered = setup.build_service_unit(
        template.read_text(), overlay, env_lines, uv_path)
    if write_if_changed(unit, rendered, dry_run):
        changed = True

    rescue_template = ENGINE_ROOT / "config" / "kbots-rescue.service"
    rescue_unit = generated_dir / "kbots-rescue.service"
    if rescue_unit.exists() and rescue_template.exists():
        r_env, r_uv = systemd_install_values(rescue_unit.read_text())
        rendered_rescue = setup.render_rescue_unit(
            rescue_template.read_text(), overlay, r_env, r_uv)
        if write_if_changed(rescue_unit, rendered_rescue, dry_run):
            changed = True

    timers_dir = ENGINE_ROOT / "config" / "timers"
    if timers_dir.is_dir():
        for f in sorted(timers_dir.iterdir()):
            if not f.is_file():
                continue
            out = generated_dir / f.name
            # Only units this install actually has; a new template is the
            # wizard's to install, since enabling a timer is a decision.
            if not out.exists():
                continue
            if write_if_changed(out, setup.build_timer_unit(f.read_text(), overlay), dry_run):
                changed = True

    if not changed:
        log("units already current")
        return 0
    if dry_run:
        return CHANGED

    ensure_writable_dirs(overlay)
    if not reload:
        log("units updated — run `sudo systemctl daemon-reload` to load them")
        return CHANGED

    for cmd in (["sudo", "-n", "systemctl", "daemon-reload"],
                ["systemctl", "daemon-reload"]):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            log("daemon-reload done — the next restart uses the new unit")
            return CHANGED
    log("ERROR: unit changed but `systemctl daemon-reload` failed.")
    log("  The file on disk and the loaded unit now disagree. Run:")
    log("    sudo systemctl daemon-reload && sudo systemctl restart kbots")
    return 1


# --- launchd -----------------------------------------------------------------

def refresh_launchd(dry_run: bool, reload: bool) -> int:
    installed = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if not installed.exists():
        log("no launchd plist installed — nothing to refresh")
        return 0

    vals = launchd_install_values(installed)
    if not assert_install_root(vals["engine_root"]):
        return 1
    overlay = vals["overlay"] or resolve_overlay(None)
    if overlay is None:
        log("could not determine the overlay from the plist — skipping")
        return 0

    template = ENGINE_ROOT / "config" / "kbots.launchd.plist"
    if not template.exists():
        log(f"template missing: {template}")
        return 0

    rendered = setup.build_launchd_plist(
        template.read_text(), overlay, vals["uv_path"], vals["home"],
        vals["path_env"], vals["modules"])

    generated = overlay / "systemd" / f"{LAUNCHD_LABEL}.plist"
    stale_copy = write_if_changed(generated, rendered, dry_run)
    previous = installed.read_text()
    if not write_if_changed(installed, rendered, dry_run):
        log("plist already current" if not stale_copy
            else "installed plist already current; refreshed the overlay's stale copy")
        return CHANGED if stale_copy else 0
    if dry_run:
        return CHANGED

    ensure_writable_dirs(overlay)
    if not reload:
        log("plist updated — takes effect on the next bootout/bootstrap")
        return CHANGED

    # kickstart re-runs the LOADED job definition, so a plist edit needs a real
    # bootout+bootstrap. That means the service is genuinely down in between:
    # if the new plist fails to load, put the old one back and load that,
    # rather than leaving the box with nothing running.
    uid = os.getuid()
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
                   capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", domain, str(installed)],
                         capture_output=True, text=True)
    if res.returncode == 0:
        log("plist reloaded")
        return CHANGED

    log(f"ERROR: the new plist failed to load: {res.stderr.strip()}")
    installed.write_text(previous)
    back = subprocess.run(["launchctl", "bootstrap", domain, str(installed)],
                          capture_output=True, text=True)
    if back.returncode == 0:
        log("restored and reloaded the previous plist — the service is up on the old unit")
    else:
        log("CRITICAL: the previous plist did not load either — the service is DOWN.")
        log(f"    launchctl bootstrap {domain} {installed}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reload", action="store_true",
                    help="make the running service manager pick up the new unit")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    args = ap.parse_args()

    if sys.platform == "darwin":
        return refresh_launchd(args.dry_run, args.reload)
    if not sys.platform.startswith("linux"):
        log(f"unsupported platform {sys.platform} — nothing to refresh")
        return 0
    return refresh_systemd(args.dry_run, args.reload)


if __name__ == "__main__":
    sys.exit(main())
