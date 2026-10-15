"""IP-KVM control — drive a physical machine through a PiKVM-compatible KVM.

The KVM (GL.iNet Comet/GLKVM, PiKVM, …) captures the target's HDMI and acts as
its USB keyboard and mouse, so an agent can operate *any* machine plugged into
it — any OS, BIOS, installers — with nothing installed on the target.

Devices are registered in config.yaml (overlay), never supplied by the model:

    kvm:
      devices:
        lab: glkvm.tail1234.ts.net                       # shorthand
        bench:
          host: 100.101.102.103                          # Tailscale IP
          user: admin                                    # default: admin
          agents: [atlas]                                # who may drive it (default: any)
          keymap: en-us                                  # target layout for kvm_type
          verify_tls: false                              # KVMs ship self-signed certs
          login:                                         # for kvm_login (optional)
            user: alice                                  # typed first, then Tab
            reveal: esc                                  # key that brings up the field

The KVM password lives in the vault as `kvm_<device>`; the target machine's
own login password as `kvm_<device>_login` (kvm_login types it, never returns it).

Reachability: hosts must be LAN, loopback, Tailscale (100.64.0.0/10, *.ts.net)
or mDNS (*.local) — so a KVM on another network is reached over a tailnet, not
by exposing its web UI to the internet. `public: true` on a device opts out of
that guard for setups that front the KVM some other way.

Coordinates: the agent works in the pixels of the last screenshot it took with
kvm_screen; the tool maps them to the KVM's absolute -32768..32767 range, so
the screenshot size (downscaled to save tokens) never matters.

Security: every tool is non-safe (owner/admin only via access control), the
model picks a device *name* never a host, `agents:` pins a machine to the
agents allowed to use it, and kvm_power is HITL-gated (it can cut power).
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated

import aiohttp
import yaml

from src.core.base import KBOTS_TMP, ToolContext, resolve_config_file, resolve_data_dir
from src.core.tools import tool

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_TYPE_TIMEOUT = aiohttp.ClientTimeout(total=120)    # /hid/print returns when typing is done
_SCREEN_MAX_WIDTH = 1280                            # preview width sent to the model
_TAILNET = ipaddress.ip_network("100.64.0.0/10")    # Tailscale CGNAT range
_WAKE_TIMEOUT = 12                                  # seconds to wait for HDMI after a nudge
_SETTLE = 1.5                                       # display wake + encoder lag after a nudge
_CLEAR_KEYS = 24                                    # Backspaces before typing a login

# Last screenshot size per device: (width, height). Clicks are scaled against
# what the model actually saw, so a downscaled preview still lands exactly.
_screen_size: dict[str, tuple[int, int]] = {}

_MODIFIERS = {
    "ctrl": "ControlLeft", "control": "ControlLeft",
    "alt": "AltLeft", "option": "AltLeft", "opt": "AltLeft", "altgr": "AltRight",
    "shift": "ShiftLeft",
    "win": "MetaLeft", "windows": "MetaLeft", "super": "MetaLeft", "meta": "MetaLeft",
    "cmd": "MetaLeft", "command": "MetaLeft",
}
_NAMED_KEYS = {
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "Space", "backspace": "Backspace",
    "delete": "Delete", "del": "Delete", "insert": "Insert", "ins": "Insert",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "home": "Home", "end": "End", "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown",
    "printscreen": "PrintScreen", "prtsc": "PrintScreen", "pause": "Pause",
    "capslock": "CapsLock", "numlock": "NumLock", "scrolllock": "ScrollLock",
    "menu": "ContextMenu",
    "minus": "Minus", "equal": "Equal", "comma": "Comma", "period": "Period",
    "slash": "Slash", "backslash": "Backslash", "semicolon": "Semicolon",
    "quote": "Quote", "backquote": "Backquote",
    "bracketleft": "BracketLeft", "bracketright": "BracketRight",
}
_PUNCT_KEYS = {"-": "Minus", "=": "Equal", ",": "Comma", ".": "Period", "/": "Slash",
               "\\": "Backslash", ";": "Semicolon", "'": "Quote", "`": "Backquote",
               "[": "BracketLeft", "]": "BracketRight"}


# ── registry ──────────────────────────────────────────────────────────

def _full_config() -> dict:
    cfg_file = resolve_config_file("config.yaml")
    if not cfg_file.exists():
        return {}
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _config() -> dict:
    return _full_config().get("kvm", {}) or {}


def _load_devices() -> dict[str, dict]:
    """Every usable device: config.yaml first, then owner-approved registrations."""
    devices = _config_devices()
    for name, spec in _registered_devices().items():
        devices.setdefault(name, spec)
    return devices


def _config_devices() -> dict[str, dict]:
    """Device registry from config.yaml: name → {host, user, agents, keymap, …}."""
    raw = _config().get("devices", {}) or {}
    devices = {}
    for name, spec in raw.items():
        if isinstance(spec, str):
            spec = {"host": spec}
        if not isinstance(spec, dict) or not spec.get("host"):
            continue
        agents = spec.get("agents") or []
        if isinstance(agents, str):
            agents = [agents]
        devices[str(name)] = {
            "host": str(spec["host"]).strip().rstrip("/"),
            "user": str(spec.get("user", "admin")),
            "agents": [str(a) for a in agents],
            "keymap": str(spec["keymap"]) if spec.get("keymap") else "",
            "verify_tls": bool(spec.get("verify_tls", False)),
            "public": bool(spec.get("public", False)),
            "login": _login_spec(spec.get("login")),
            # Hand-written config is the operator's own grant: sign-in and power
            # behave as they always did. Registered devices start with both off.
            "source": "config", "restricted": bool(agents),
            "allow_login": True, "allow_power": True,
        }
    return devices


def _login_spec(raw) -> dict:
    """Target sign-in settings: {user, reveal}. Both optional."""
    raw = raw if isinstance(raw, dict) else {}
    return {"user": str(raw["user"]) if raw.get("user") else "",
            "reveal": str(raw["reveal"]) if raw.get("reveal") else ""}


def _bare_host(host: str) -> str:
    """Hostname/IP without scheme or port, for the reachability guard."""
    h = host.split("://", 1)[-1].split("/", 1)[0]
    if h.startswith("["):                       # [ipv6]:port
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _reachable_privately(host: str) -> bool:
    """LAN, loopback, tailnet or mDNS only — never an arbitrary internet host."""
    h = _bare_host(host)
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or (ip.version == 4 and ip in _TAILNET)
    except ValueError:
        return h.endswith(".ts.net") or h.endswith(".local")


def _base_url(spec: dict) -> str:
    host = spec["host"]
    return host if "://" in host else f"https://{host}"


def _norm(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _resolve(ctx: ToolContext, device: str) -> tuple[dict | None, str, str]:
    """(spec, canonical name, error). Unknown names list the valid ones so a
    small model self-corrects in one round."""
    _settle()
    devices = _load_devices()
    if not devices:
        return None, "", ("No KVM devices yet. Add one with kvm_add (the owner approves it), "
                          "or under kvm.devices in config.yaml.")
    name = device if device in devices else _norm(device)
    if name not in devices and len(devices) == 1 and not device.strip():
        name = next(iter(devices))
    spec = devices.get(name)
    if not spec:
        return None, "", f"Unknown KVM '{device}'. Configured: {', '.join(sorted(devices))}"
    if spec["restricted"] and ctx.agent_id not in spec["agents"]:
        return None, "", (f"KVM '{name}' is not assigned to agent '{ctx.agent_id}'. "
                          "Ask the owner with kvm_allow.")
    if not spec["public"] and not _reachable_privately(spec["host"]):
        return None, "", (f"KVM '{name}' host {spec['host']} is not a LAN/Tailscale address — "
                          "refusing. Reach it over a tailnet, or set public: true.")
    return spec, name, ""


# ── registrations (owner-approved, from chat) ─────────────────────────
#
# kvm.db holds devices, per-agent grants and the owner decisions they wait on.
# Nothing here becomes usable on an agent's say-so: _settle reads the answer
# from the engine's own owner-asks.db, where only the owner's tap is recorded.

_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_SETTINGS = ("login", "power")
_APPROVE = {"add": "Add", "grant": "Allow", "setting": "Turn on"}


def _data_dir() -> Path:
    return resolve_data_dir(_full_config())


def _db() -> sqlite3.Connection:
    path = _data_dir() / "kvm.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            name TEXT PRIMARY KEY, host TEXT NOT NULL, user TEXT NOT NULL, keymap TEXT NOT NULL,
            login INTEGER NOT NULL DEFAULT 0, power INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL, added_by TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (
            device TEXT NOT NULL, agent TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY (device, agent));
        CREATE TABLE IF NOT EXISTS pending (
            ask_id TEXT PRIMARY KEY, kind TEXT NOT NULL, device TEXT NOT NULL,
            agent TEXT NOT NULL, setting TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            question TEXT NOT NULL DEFAULT '');
    """)
    return db


def _registered_devices() -> dict[str, dict]:
    path = _data_dir() / "kvm.db"
    if not path.exists():
        return {}
    db = _db()
    try:
        out = {}
        for row in db.execute("SELECT * FROM devices WHERE state='active'"):
            agents = [r["agent"] for r in db.execute(
                "SELECT agent FROM grants WHERE device=? AND state='active'", (row["name"],))]
            out[row["name"]] = {
                "host": row["host"], "user": row["user"], "agents": agents, "keymap": row["keymap"],
                "verify_tls": False, "public": False, "login": _login_spec(None),
                "source": "registered", "restricted": True,
                "allow_login": bool(row["login"]), "allow_power": bool(row["power"]),
            }
        return out
    finally:
        db.close()


def _owner_answer(ask_id: str, question: str) -> tuple[str, str]:
    """(state, answer) of an owner ask, read-only from the engine's store.
    ('missing', '') when there is no such ask, no store, or the recorded card
    asked something else: an answer only counts for the question we posted."""
    path = _data_dir() / "owner-asks.db"
    if not path.exists():
        return "missing", ""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT state, answer, answered_by, recipient_id, payload FROM owner_asks "
                             "WHERE id=?", (ask_id,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return "missing", ""
    if not row:
        return "missing", ""
    state, answer, by, recipient, payload = row
    try:
        asked = json.loads(payload).get("question")
    except (ValueError, AttributeError):
        asked = None
    if asked != question:
        return "missing", ""
    if state == "answered" and by != recipient:        # the store forbids it; trust nothing else
        return "missing", ""
    return state, answer or ""


def _settle() -> None:
    """Apply every owner decision that has come in since the last tool call."""
    path = _data_dir() / "kvm.db"
    if not path.exists():
        return
    db = _db()
    try:
        for p in db.execute("SELECT * FROM pending").fetchall():
            state, answer = _owner_answer(p["ask_id"], p["question"])
            if state not in ("answered", "stale"):
                continue
            approved = state == "answered" and answer == _APPROVE[p["kind"]]
            if p["kind"] == "add":
                if approved:
                    db.execute("UPDATE devices SET state='active' WHERE name=?", (p["device"],))
                    db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,'active')", (p["device"], p["agent"]))
                else:
                    db.execute("DELETE FROM devices WHERE name=? AND state='pending'", (p["device"],))
            elif p["kind"] == "grant":
                if approved:
                    db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,'active')", (p["device"], p["agent"]))
            elif p["kind"] == "setting" and approved and p["setting"] in _SETTINGS:
                db.execute(f"UPDATE devices SET {p['setting']}=1 WHERE name=?", (p["device"],))
            db.execute("DELETE FROM pending WHERE ask_id=?", (p["ask_id"],))
            logger.info(f"kvm {p['kind']} {p['device']}/{p['agent']} {p['setting']}: "
                        f"{'approved' if approved else 'not approved'}")
    finally:
        db.close()


def _waiting(db, kind: str, device: str, agent: str = "", setting: str = "") -> bool:
    return db.execute("SELECT 1 FROM pending WHERE kind=? AND device=? AND agent=? AND setting=?",
                      (kind, device, agent, setting)).fetchone() is not None


async def _ask(ctx: ToolContext, kind: str, device: str, question: str, context: str,
               agent: str = "", setting: str = "") -> str:
    """Post an owner decision card and remember what it would unlock. '' on success."""
    from src.tools.owner_asks import ask_owner
    options = [_APPROVE[kind], "Don't"]
    out = await ask_owner(ctx, question=question, default="Nothing changes; no access is granted.",
                          options=options, context=context,
                          request_key=f"kvm-{kind}-{device}-{uuid.uuid4().hex[:8]}")
    try:
        ask_id = json.loads(out)["id"]
    except (ValueError, KeyError, TypeError):
        return f"Couldn't post the owner card: {out}"
    db = _db()
    try:
        db.execute("INSERT INTO pending VALUES (?,?,?,?,?,?,?)",
                   (ask_id, kind, device, agent or ctx.agent_id, setting, time.time(), question))
    finally:
        db.close()
    return ""


async def _ask_secret(ctx: ToolContext, key: str, label: str, purpose: str) -> str:
    """Owner types the secret into a private form; it never passes through us."""
    try:
        from src.tools.owner_secrets import ask_owner_secret
    except ImportError:
        return (f"This kbots has no ask_owner_secret yet: the owner must store it in the vault as "
                f"{key} (vault-manage.py).")
    out = await ask_owner_secret(ctx, key=key, label=label, purpose=purpose, request_key=f"{key}-secret")
    try:
        json.loads(out)["id"]
    except (ValueError, KeyError, TypeError):
        return f"Couldn't ask the owner for the password: {out}"
    return ""


async def _probe(host: str) -> bool:
    """Does a KVM answer at host? Unauthenticated auth/check: 401/403 mean yes."""
    url = (host if "://" in host else f"https://{host}") + "/api/auth/check"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url, ssl=False) as resp:
                return resp.status in (200, 401, 403)
    except (aiohttp.ClientError, TimeoutError):
        return False


# ── HTTP ──────────────────────────────────────────────────────────────

def _headers(ctx: ToolContext, name: str, spec: dict) -> dict:
    pw = None
    if ctx.vault:
        try:
            pw = ctx.vault.get(f"kvm_{name}")
        except Exception:
            pw = None
    return {"X-KVMD-User": spec["user"], "X-KVMD-Passwd": pw or ""}


async def _request(ctx: ToolContext, name: str, spec: dict, method: str, path: str,
                   params: dict | None = None, data: str | None = None,
                   timeout: aiohttp.ClientTimeout = _TIMEOUT) -> tuple[bytes | None, str]:
    """(body, error). Exactly one is set; errors are written for the model."""
    url = f"{_base_url(spec)}/api{path}"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.request(method, url, params=params, data=data,
                                 headers=_headers(ctx, name, spec),
                                 ssl=None if spec["verify_tls"] else False) as resp:
                body = await resp.read()
                if resp.status in (401, 403):
                    return None, (f"KVM '{name}' rejected the login — store its password in "
                                  f"the vault as kvm_{name} (user: {spec['user']}).")
                if resp.status == 503 and path.startswith("/streamer"):
                    return None, (f"KVM '{name}' has no video — is the target powered on and "
                                  "its HDMI connected?")
                if resp.status != 200:
                    return None, f"KVM '{name}' HTTP {resp.status}: {body[:200].decode(errors='replace')}"
                return body, ""
    except (aiohttp.ClientError, TimeoutError) as e:
        return None, (f"KVM '{name}' unreachable ({type(e).__name__}) — is it powered on, and "
                      "is this host on the same LAN or tailnet?")


async def _json(ctx, name, spec, method, path, params=None) -> tuple[dict | None, str]:
    body, err = await _request(ctx, name, spec, method, path, params=params)
    if err:
        return None, err
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None, f"KVM '{name}' returned non-JSON for {path}."
    return (data.get("result", data) if isinstance(data, dict) else {}), ""


# ── screen geometry ───────────────────────────────────────────────────

def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF header — no Pillow dependency."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def _to_abs(v: int, size: int) -> int:
    """Pixel → KVM absolute axis (-32768..32767), clamped to the screen."""
    v = max(0, min(int(v), size - 1))
    return round(v * 65535 / max(size - 1, 1)) - 32768


_MODIFIER_CODES = ("ControlLeft", "ControlRight", "ShiftLeft", "ShiftRight",
                   "AltLeft", "AltRight", "MetaLeft", "MetaRight")


async def _tap(ctx, name, spec, key: str) -> str:
    """Press and release one key with explicit states. Never use send_key without
    `state`: kvmd auto-releases only non-modifiers, so a bare Shift/Ctrl stays
    held, and on a Mac every click becomes a right-click (Ctrl+click)."""
    for state in ("true", "false"):
        _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_key",
                                params={"key": key, "state": state})
        if err:
            return err
    return ""


async def _release_modifiers(ctx, name, spec) -> None:
    """Let go of every modifier, in case something left one held."""
    for key in _MODIFIER_CODES:
        await _request(ctx, name, spec, "POST", "/hid/events/send_key",
                       params={"key": key, "state": "false"})


async def _nudge(ctx, name, spec) -> None:
    """Wake a sleeping display (screen blank, closed-lid laptop): a net-zero mouse
    jiggle and two Shift taps (a sleeping Mac ignores the mouse but wakes on a
    key). Nothing that types, clicks or toggles anything.

    Nudge unconditionally rather than asking the KVM first: GLKVM's
    `real_resolution` reads "no_signal" even while the screen is visibly on, and
    trusting it cost us whole seconds in which the target dozed off again.
    """
    for dx in (20, -20, 20, -20):
        await _request(ctx, name, spec, "POST", "/hid/events/send_mouse_relative",
                       params={"delta_x": str(dx), "delta_y": "0"})
    for i in range(2):
        if i:
            await asyncio.sleep(0.5)
        await _tap(ctx, name, spec, "ShiftLeft")


async def _snapshot(ctx, name, spec) -> tuple[bytes | None, str]:
    """Nudge, let the display and encoder settle, then grab a frame. Retries
    (re-nudging) while the KVM has no picture, up to _WAKE_TIMEOUT."""
    params = {"preview": "1", "preview_max_width": str(_SCREEN_MAX_WIDTH),
              "preview_quality": "80"}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAKE_TIMEOUT
    attempt = 0
    while True:
        if attempt % 3 == 0:
            await _nudge(ctx, name, spec)
        await asyncio.sleep(_SETTLE)
        attempt += 1
        body, err = await _request(ctx, name, spec, "GET", "/streamer/snapshot", params=params)
        if not err or "has no video" not in err:
            break
        if loop.time() >= deadline:
            return None, (f"KVM '{name}' has no video, and nudging the mouse/keyboard didn't "
                          "wake the target. Is it powered on, and its HDMI connected?")
    if err:
        return None, err
    size = _jpeg_size(body)
    if size:
        _screen_size[name] = size
    return body, ""


async def _size(ctx, name, spec) -> tuple[tuple[int, int] | None, str]:
    """Size of the screenshot the model is reasoning about; takes one if none yet."""
    if name not in _screen_size:
        _, err = await _snapshot(ctx, name, spec)
        if err:
            return None, err
    size = _screen_size.get(name)
    return (size, "") if size else (None, f"Couldn't read KVM '{name}' screen size.")


# ── keys ──────────────────────────────────────────────────────────────

def _key_code(token: str) -> str | None:
    """One key name → KVM key code (KeyboardEvent.code). None if unknown."""
    t = token.strip()
    low = t.lower()
    if low in _MODIFIERS:
        return _MODIFIERS[low]
    if low in _NAMED_KEYS:
        return _NAMED_KEYS[low]
    if t in _PUNCT_KEYS:
        return _PUNCT_KEYS[t]
    if len(t) == 1 and t.isalpha():
        return f"Key{t.upper()}"
    if len(t) == 1 and t.isdigit():
        return f"Digit{t}"
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return f"F{int(low[1:])}"
    if t[:1].isupper() and t.isalnum():          # already a code: KeyA, ArrowUp, Numpad5
        return t
    return None


def _parse_combo(combo: str) -> tuple[list[str], str]:
    """'ctrl+alt+delete' → ['ControlLeft', 'AltLeft', 'Delete']."""
    raw = combo.strip()
    if not raw:
        return [], "Error: 'keys' required, e.g. 'enter', 'ctrl+c', 'win+r', 'ctrl+alt+delete'."
    tokens = ["+"] if raw == "+" else [p for p in raw.split("+") if p.strip()]
    codes = []
    for tok in tokens:
        code = _key_code(tok)
        if not code:
            return [], f"Unknown key '{tok.strip()}'. Use names like enter, esc, tab, f5, up, ctrl, alt, win, a, 1."
        codes.append(code)
    return codes, ""


# ── tools ─────────────────────────────────────────────────────────────

@tool(name="kvm_devices",
      description="List the KVM-attached machines this agent can drive, with video/keyboard status.",
      category="kvm")
async def kvm_devices(ctx: ToolContext) -> str:
    _settle()
    devices = _load_devices()
    lines = []
    for name, spec in sorted(devices.items()):
        if spec["restricted"] and ctx.agent_id not in spec["agents"]:
            continue
        _, _, err = _resolve(ctx, name)
        if err:
            lines.append(f"{name}: {err}")
            continue
        hid, err = await _json(ctx, name, spec, "GET", "/hid")
        if err:
            lines.append(f"{name}: {err}")
            continue
        streamer, _ = await _json(ctx, name, spec, "GET", "/streamer")
        source = (((streamer or {}).get("streamer") or {}).get("source") or {})
        res = source.get("resolution") or {}
        online = source.get("online")
        kb = (hid.get("keyboard") or {}).get("online")
        mouse = (hid.get("mouse") or {}).get("online")
        # real_resolution reads "no_signal" both when HDMI is dark and, on GLKVM,
        # sometimes while the screen is plainly on, so say "maybe asleep" not "off".
        live = online and source.get("real_resolution") != "no_signal"
        video = (f"{res.get('width')}x{res.get('height')}" if live
                 else "no signal reported (display may be asleep; kvm_screen wakes it)")
        extras = "" if spec["source"] == "config" else (
            f", sign-in {'on' if spec['allow_login'] else 'off'}, power {'on' if spec['allow_power'] else 'off'}")
        lines.append(f"{name}: video {video}, keyboard {'ok' if kb else 'offline'}, "
                     f"mouse {'ok' if mouse else 'offline'}{extras}")
    lines += _pending_lines()
    if not lines:
        return (f"No KVMs are assigned to agent '{ctx.agent_id}'. Add one with kvm_add, or ask for "
                "access to an existing one with kvm_allow.")
    return "\n".join(lines)


def _pending_lines() -> list[str]:
    if not (_data_dir() / "kvm.db").exists():
        return []
    db = _db()
    try:
        out = []
        for p in db.execute("SELECT * FROM pending ORDER BY created"):
            what = {"add": "to be added", "grant": f"access for {p['agent']}",
                    "setting": f"{p['setting']} on"}[p["kind"]]
            out.append(f"{p['device']}: waiting for the owner ({what})")
        return out
    finally:
        db.close()


@tool(name="kvm_screen",
      description=("See the KVM-attached machine's screen, waking it first if it is asleep. "
                   "mode 'image' saves a screenshot (click coordinates are in its pixels); "
                   "'text' returns OCR'd screen text."),
      category="kvm")
async def kvm_screen(ctx: ToolContext, device: str = "",
                     mode: Annotated[str, {"choices": ["image", "text"]}] = "image") -> str:
    """Screenshot (or OCR) the target machine's display."""
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    if mode == "text":
        # Many KVMs ship without OCR; the snapshot call then 500s with no detail.
        state, err = await _json(ctx, name, spec, "GET", "/streamer/ocr")
        if err:
            return err
        if not (state.get("ocr") or {}).get("enabled"):
            return f"KVM '{name}' has no OCR available — use kvm_screen with mode 'image'."
        body, err = await _request(ctx, name, spec, "GET", "/streamer/snapshot",
                                   params={"ocr": "1"})
        if err:
            return err
        try:
            data = json.loads(body)
        except ValueError:
            return f"KVM '{name}' OCR returned non-JSON — this KVM may not support OCR."
        text = data.get("result") if isinstance(data, dict) else None
        if isinstance(text, dict):                 # some kvmd builds wrap it: {"text": …}
            text = text.get("text")
        return (text or "").strip() or "(no text recognised on screen)"

    body, err = await _snapshot(ctx, name, spec)
    if err:
        return err
    media = KBOTS_TMP / "media"
    media.mkdir(parents=True, exist_ok=True)
    fd, out = tempfile.mkstemp(prefix=f"kvm_{name}_", suffix=".jpg", dir=str(media))
    os.close(fd)
    Path(out).write_bytes(body)
    w, h = _screen_size.get(name, (0, 0))
    logger.info(f"kvm_screen device={name} by agent={ctx.agent_id}")
    return f"Screenshot saved: {out} ({w}x{h}; click coordinates use these pixels)"


@tool(name="kvm_click",
      description=("Click on the KVM-attached machine at x,y — pixels of the last kvm_screen "
                   "image. button 'move' only moves the pointer (hover)."),
      category="kvm")
async def kvm_click(ctx: ToolContext, x: int, y: int, device: str = "",
                    button: Annotated[str, {"choices": ["left", "right", "middle", "double",
                                                         "move"]}] = "left") -> str:
    """Move the pointer to x,y and click."""
    if button not in ("left", "right", "middle", "double", "move"):
        return "button must be left, right, middle, double, or move."
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    size, err = await _size(ctx, name, spec)
    if err:
        return err
    w, h = size
    _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_mouse_move",
                            params={"to_x": str(_to_abs(x, w)), "to_y": str(_to_abs(y, h))})
    if err:
        return err
    if button == "move":
        return f"Pointer at {x},{y}."
    clicks = 2 if button == "double" else 1
    btn = "left" if button == "double" else button
    for _ in range(clicks):
        _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_mouse_button",
                                params={"button": btn})
        if err:
            return err
    return f"{button.capitalize()}-clicked at {x},{y}. Take a kvm_screen to see the result."


@tool(name="kvm_type",
      description="Type text on the KVM-attached machine's keyboard (use kvm_key for Enter/shortcuts).",
      category="kvm")
async def kvm_type(ctx: ToolContext, text: str, device: str = "") -> str:
    """Type a string as keystrokes, using the device's configured keymap."""
    if not text:
        return "Error: 'text' required."
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    params = {"limit": "0"}
    if spec["keymap"]:
        params["keymap"] = spec["keymap"]
    _, err = await _request(ctx, name, spec, "POST", "/hid/print", params=params,
                            data=text, timeout=_TYPE_TIMEOUT)
    return err or f"Typed {len(text)} character(s)."


@tool(name="kvm_key",
      description=("Press a key or shortcut on the KVM-attached machine, e.g. 'enter', 'esc', "
                   "'ctrl+c', 'win+r', 'alt+f4', 'ctrl+alt+delete', 'f12'."),
      category="kvm")
async def kvm_key(ctx: ToolContext, keys: str, device: str = "") -> str:
    """Press keys together, then release them in reverse order."""
    codes, err = _parse_combo(keys)
    if err:
        return err
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_shortcut",
                            params={"keys": ",".join(codes)})
    return err or f"Pressed {keys.strip()}."


@tool(name="kvm_scroll",
      description=("Scroll on the KVM-attached machine. amount > 0 scrolls down, < 0 up "
                   "(notches). Optional x,y moves the pointer there first."),
      category="kvm")
async def kvm_scroll(ctx: ToolContext, amount: int, device: str = "",
                     x: int = -1, y: int = -1) -> str:
    """Scroll the wheel, optionally over a given point."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return "amount must be a whole number."
    if amount == 0:
        return "amount must be non-zero."
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    if x >= 0 and y >= 0:
        size, err = await _size(ctx, name, spec)
        if err:
            return err
        _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_mouse_move",
                                params={"to_x": str(_to_abs(x, size[0])),
                                        "to_y": str(_to_abs(y, size[1]))})
        if err:
            return err
    step = -1 if amount > 0 else 1                 # HID wheel: negative is down
    for _ in range(min(abs(amount), 50)):
        _, err = await _request(ctx, name, spec, "POST", "/hid/events/send_mouse_wheel",
                                params={"delta_x": "0", "delta_y": str(step)})
        if err:
            return err
    return f"Scrolled {'down' if amount > 0 else 'up'} {min(abs(amount), 50)}."


@tool(name="kvm_power",
      description=("Power-control the KVM-attached machine via its ATX board: status, on, off "
                   "(graceful), off_hard, reset_hard. Needs the KVM's ATX cable wired."),
      category="kvm", hitl=True)
async def kvm_power(ctx: ToolContext, action: Annotated[str, {"choices": [
        "status", "on", "off", "off_hard", "reset_hard"]}], device: str = "") -> str:
    """ATX power control. HITL-gated: a hard off can lose the target's unsaved work."""
    if action not in ("status", "on", "off", "off_hard", "reset_hard"):
        return "action must be status, on, off, off_hard, or reset_hard."
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    if action != "status" and not spec["allow_power"]:
        return (f"Power control is off for '{name}'. Ask the owner to turn it on with "
                f"kvm_settings('{name}', 'power', 'on').")
    if action == "status":
        atx, err = await _json(ctx, name, spec, "GET", "/atx")
        if err:
            return err
        if not atx.get("enabled", True):
            return f"KVM '{name}' has ATX power control disabled (no ATX board configured)."
        leds = atx.get("leds") or {}
        return (f"{name}: power {'on' if leds.get('power') else 'off'}"
                f"{', disk active' if leds.get('hdd') else ''}")
    _, err = await _request(ctx, name, spec, "POST", "/atx/power", params={"action": action})
    logger.info(f"kvm_power device={name} action={action} by agent={ctx.agent_id}")
    return err or f"Sent power {action} to {name}."


@tool(name="kvm_login",
      description=("Sign in to the KVM-attached machine's lock/login screen with the password "
                   "stored in the vault. The password is typed directly and never shown. Take a "
                   "kvm_screen first to confirm a login screen is showing."),
      category="kvm")
async def kvm_login(ctx: ToolContext, device: str = "") -> str:
    """Type the target's stored login password and press Enter.

    Ungated so agents can sign in unattended. It types a secret into whatever
    has focus, so add it to security.hitl.gated_tools to have a human confirm
    the lock screen is up first.
    """
    spec, name, err = _resolve(ctx, device)
    if err:
        return err
    if not spec["allow_login"]:
        return (f"Sign-in is off for '{name}'. Ask the owner to turn it on with "
                f"kvm_settings('{name}', 'login', 'on').")
    key = f"kvm_{name}_login"
    password = None
    if ctx.vault:
        try:
            password = ctx.vault.get(key)
        except Exception:
            password = None
    if not password:
        return f"No login password for '{name}' in the vault — store it as {key}."

    login = spec["login"]
    reveal = []
    if login["reveal"]:
        reveal, err = _parse_combo(login["reveal"])
        if err:
            return f"KVM '{name}' login.reveal: {err}"

    print_params = {"limit": "0"}
    if spec["keymap"]:
        print_params["keymap"] = spec["keymap"]

    async def press(codes: list[str]) -> str:
        _, e = await _request(ctx, name, spec, "POST", "/hid/events/send_shortcut",
                              params={"keys": ",".join(codes)})
        return e

    async def type_(text: str) -> str:
        _, e = await _request(ctx, name, spec, "POST", "/hid/print", params=print_params,
                              data=text, timeout=_TYPE_TIMEOUT)
        return e

    # Wake, then go straight through: a lock screen that dozes off between the
    # wake and the typing swallows keys, which is how earlier attempts failed.
    await _nudge(ctx, name, spec)
    await asyncio.sleep(_SETTLE)
    if reveal:
        if err := await press(reveal):
            return err
        await asyncio.sleep(1)
    await _release_modifiers(ctx, name, spec)      # a held Ctrl turns the password into shortcuts
    for _ in range(_CLEAR_KEYS):                   # drop leftovers from an earlier attempt
        if err := await _tap(ctx, name, spec, "Backspace"):
            return err
    if login["user"]:
        if (err := await type_(login["user"])) or (err := await press(["Tab"])):
            return err
    if err := await type_(password):
        return err                                 # _request errors never echo the body sent
    if err := await press(["Enter"]):
        return err
    logger.info(f"kvm_login device={name} by agent={ctx.agent_id}")
    return (f"Entered the stored login for '{name}' and pressed Enter. Take a kvm_screen "
            "to confirm it unlocked.")


# ── self-service: add devices and grant rights, owner-approved ────────

@tool(name="kvm_add",
      description=("Add a KVM so agents can drive the machine behind it. host: the KVM's LAN or Tailscale "
                   "address (e.g. glkvm.tail1234.ts.net). The owner approves on a card and enters the KVM "
                   "password in a private form; you never see it. Call kvm_devices to see when it is ready."),
      category="kvm")
async def kvm_add(ctx: ToolContext, name: str, host: str, keymap: str = "") -> str:
    """Register a KVM. Nothing is usable until the owner taps Add."""
    _settle()
    name = _norm(name)
    host = host.strip().rstrip("/")
    if not _NAME.match(name):
        return "name must be 1-32 lowercase letters, digits or '_', e.g. 'lab' or 'mac_mini'."
    if not _reachable_privately(host):
        return (f"{host} is not a LAN or Tailscale address. Put the KVM on your tailnet and use its "
                "*.ts.net name.")
    if name in _config_devices():
        return f"'{name}' is already set up in config.yaml."
    db = _db()
    try:
        row = db.execute("SELECT state FROM devices WHERE name=?", (name,)).fetchone()
        if row and row["state"] == "active":
            return f"'{name}' is already added. Use kvm_allow to give another agent access."
        if row and _waiting(db, "add", name, ctx.agent_id):
            return f"'{name}' is already waiting for the owner to approve."
        if row:
            return f"'{name}' is being added by another agent. Pick a different name."
    finally:
        db.close()
    if not await _probe(host):
        return f"No KVM answers at {host}. Is it on, and is this host on the same LAN or tailnet?"
    db = _db()
    try:
        db.execute("INSERT INTO devices (name,host,user,keymap,state,added_by,created) "
                   "VALUES (?,?,?,?,'pending',?,?)",
                   (name, host, "admin", keymap.strip(), ctx.agent_id, time.time()))
    finally:
        db.close()
    err = await _ask(
        ctx, "add", name,
        question=f"Add KVM '{name}' ({host}) for {ctx.agent_id}?",
        context=(f"{ctx.agent_id} will see that machine's screen and use its keyboard and mouse. "
                 "Sign-in and power control stay off unless you turn them on."))
    if err:
        db = _db()
        try:
            db.execute("DELETE FROM devices WHERE name=? AND state='pending'", (name,))
        finally:
            db.close()
        return err
    note = await _ask_secret(
        ctx, f"kvm_{name}", f"KVM password for {name}"[:45],
        f"The web login of the KVM at {host}, so {ctx.agent_id} can see and control the machine behind it.")
    asked = "Asked the owner to approve on a card" + ("" if note else " and to enter the KVM password in a "
                                                                        "private form")
    return f"{asked}. '{name}' becomes usable once they tap Add." + (f" {note}" if note else "")


@tool(name="kvm_allow",
      description="Ask the owner to let an agent use a KVM-attached machine. The owner approves on a card.",
      category="kvm")
async def kvm_allow(ctx: ToolContext, device: str, agent: str) -> str:
    """Request access for an agent; granted only when the owner taps Allow."""
    _settle()
    device, agent = _norm(device), agent.strip()
    if not agent:
        return "agent is required."
    spec = _registered_devices().get(device)
    if not spec:
        if device in _config_devices():
            return f"'{device}' is set up in config.yaml; edit its agents: list there."
        return f"No added KVM called '{device}'."
    if agent in spec["agents"]:
        return f"{agent} can already use '{device}'."
    db = _db()
    try:
        if _waiting(db, "grant", device, agent):
            return f"Already waiting for the owner to allow {agent} on '{device}'."
    finally:
        db.close()
    err = await _ask(ctx, "grant", device, agent=agent,
                     question=f"Let {agent} use KVM '{device}'?",
                     context=f"{agent} will see that machine's screen and use its keyboard and mouse.")
    return err or f"Asked the owner. {agent} can use '{device}' once they tap Allow."


@tool(name="kvm_revoke",
      description="Stop an agent from using a KVM-attached machine. Takes effect immediately.",
      category="kvm")
async def kvm_revoke(ctx: ToolContext, device: str, agent: str) -> str:
    """Removing access needs no approval: it only ever reduces what an agent can do."""
    _settle()                     # apply earlier approvals first, or they would re-grant after this
    device, agent = _norm(device), agent.strip()
    if device in _config_devices():
        return f"'{device}' is set up in config.yaml; edit its agents: list there."
    if not (_data_dir() / "kvm.db").exists():
        return f"No added KVM called '{device}'."
    db = _db()
    try:
        removed = db.execute("DELETE FROM grants WHERE device=? AND agent=?", (device, agent)).rowcount
        db.execute("DELETE FROM pending WHERE kind='grant' AND device=? AND agent=?", (device, agent))
    finally:
        db.close()
    logger.info(f"kvm_revoke device={device} agent={agent} by {ctx.agent_id}")
    return f"{agent} can no longer use '{device}'." if removed else f"{agent} had no access to '{device}'."


_LOGIN_PURPOSE = "The password of the computer behind KVM '{}', so agents can sign in at its lock screen."


@tool(name="kvm_settings",
      description=("Turn sign-in ('login': type the machine's stored password) or power control ('power') "
                   "on or off for an added KVM. Turning one on asks the owner; turning one off is immediate."),
      category="kvm")
async def kvm_settings(ctx: ToolContext, device: str,
                       setting: Annotated[str, {"choices": ["login", "power"]}],
                       value: Annotated[str, {"choices": ["on", "off"]}]) -> str:
    """Off is immediate (less authority); on waits for the owner's tap."""
    _settle()
    device = _norm(device)
    if setting not in _SETTINGS or value not in ("on", "off"):
        return "setting must be 'login' or 'power', value 'on' or 'off'."
    spec = _registered_devices().get(device)
    if not spec:
        if device in _config_devices():
            return f"'{device}' is set up in config.yaml, where sign-in and power are always available."
        return f"No added KVM called '{device}'."
    if ctx.agent_id not in spec["agents"]:
        return f"KVM '{device}' is not assigned to agent '{ctx.agent_id}'."
    what = "sign-in" if setting == "login" else "power control"
    if value == "off":
        db = _db()
        try:
            db.execute(f"UPDATE devices SET {setting}=0 WHERE name=?", (device,))
            db.execute("DELETE FROM pending WHERE kind='setting' AND device=? AND setting=?", (device, setting))
        finally:
            db.close()
        return f"Turned {what} off for '{device}'."
    login_key = f"kvm_{device}_login"
    if spec["allow_login" if setting == "login" else "allow_power"]:
        if setting == "login" and not (ctx.vault and ctx.vault.get(login_key)):
            note = await _ask_secret(ctx, login_key, f"Login password for {device}"[:45],
                                     _LOGIN_PURPOSE.format(device))
            return f"Sign-in is on for '{device}'. " + (note or "Asked the owner for the computer's password.")
        return f"{what.capitalize()} is already on for '{device}'."
    db = _db()
    try:
        if _waiting(db, "setting", device, ctx.agent_id, setting):
            return f"Already waiting for the owner to turn {what} on for '{device}'."
    finally:
        db.close()
    context = ("Agents will be able to type the computer's stored password at its lock screen. It types into "
               "whatever has focus." if setting == "login" else
               "Agents will be able to power the machine on, off and reset it. Each use still needs your tap.")
    err = await _ask(ctx, "setting", device, setting=setting,
                     question=f"Turn {what} on for KVM '{device}'?", context=context)
    if err:
        return err
    if setting != "login":
        return f"Asked the owner to turn {what} on for '{device}'."
    note = await _ask_secret(ctx, login_key, f"Login password for {device}"[:45], _LOGIN_PURPOSE.format(device))
    return (f"Asked the owner to turn {what} on for '{device}'"
            + (f". {note}" if note else " and to enter the computer's password in a private form."))
