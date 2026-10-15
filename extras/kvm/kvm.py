"""IP-KVM control — drive a physical machine through a PiKVM-compatible KVM.

The KVM (GL.iNet Comet/GLKVM, PiKVM, …) captures the target's HDMI and acts as
its USB keyboard and mouse, so an agent can operate *any* machine plugged into
it — any OS, BIOS, installers — with nothing installed on the target.

Devices are registered in config.yaml (overlay), never supplied by the model:

    kvm:
      devices:
        lab: <kvm>.<tailnet>.ts.net                      # shorthand
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
import tempfile
from pathlib import Path
from typing import Annotated

import aiohttp
import yaml

from src.core.base import KBOTS_TMP, ToolContext, resolve_config_file
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

def _config() -> dict:
    cfg_file = resolve_config_file("config.yaml")
    if not cfg_file.exists():
        return {}
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return (cfg.get("kvm", {}) or {}) if isinstance(cfg, dict) else {}


def _load_devices() -> dict[str, dict]:
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
    devices = _load_devices()
    if not devices:
        return None, "", ("No KVM devices configured. Add them under kvm.devices in "
                          "config.yaml (see extras/kvm/README.md).")
    name = device if device in devices else _norm(device)
    if name not in devices and len(devices) == 1 and not device.strip():
        name = next(iter(devices))
    spec = devices.get(name)
    if not spec:
        return None, "", f"Unknown KVM '{device}'. Configured: {', '.join(sorted(devices))}"
    if spec["agents"] and ctx.agent_id not in spec["agents"]:
        return None, "", f"KVM '{name}' is not assigned to agent '{ctx.agent_id}'."
    if not spec["public"] and not _reachable_privately(spec["host"]):
        return None, "", (f"KVM '{name}' host {spec['host']} is not a LAN/Tailscale address — "
                          "refusing. Reach it over a tailnet, or set public: true.")
    return spec, name, ""


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
    devices = _load_devices()
    if not devices:
        return "No KVM devices configured. Add them under kvm.devices in config.yaml."
    lines = []
    for name, spec in sorted(devices.items()):
        if spec["agents"] and ctx.agent_id not in spec["agents"]:
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
        lines.append(f"{name}: video {video}, keyboard {'ok' if kb else 'offline'}, "
                     f"mouse {'ok' if mouse else 'offline'}")
    return "\n".join(lines) or f"No KVMs are assigned to agent '{ctx.agent_id}'."


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
