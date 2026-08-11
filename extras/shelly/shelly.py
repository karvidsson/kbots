"""Shelly smart-home control — local LAN RPC to configured devices only.

Devices are registered in config.yaml (overlay), never supplied by the model:

    shelly:
      devices:
        office_light: 192.168.1.42                        # shorthand: gen2 switch
        heater: {host: 192.168.1.43, gen: 1}               # full form
        blinds: {host: 192.168.1.44, kind: cover}
        lamp: {host: 192.168.1.45, gen: 1, kind: dimmer}   # brightness-capable
        hall_b: {host: 192.168.1.46, channel: 1}           # 2nd relay of a 2PM
      groups:
        downstairs: [office_light, lamp]                   # address many at once

`kind` is switch (default), dimmer, or cover. `channel` selects the relay/light
id on multi-channel devices (Plus 2PM etc.) and defaults to 0 — it is part of
the *registry*, not a model argument, so each name maps to exactly one output.

Security model is the INVERSE of the generic http_request tool (which blocks
private networks): these tools may ONLY talk to private/LAN hosts from the
registry — the model chooses a device *name*, never a host, so the worst case
is toggling the wrong configured device. `shelly_switch`/`shelly_dim` ship
ungated (lights/plugs are reversible; gating would deadlock unattended
automations — add them to security.hitl.gated_tools to require approval);
`shelly_cover` is HITL-gated by default (physical movement). Optional Gen2
digest auth: store the device password in the vault as `shelly_<device>`.

Tiny schemas on purpose — 1-2 params with enums — so small local models call
these reliably (see docs/LOCAL_MODELS.md).
"""

import asyncio
import ipaddress
import json
import logging
from typing import Annotated

import aiohttp
import yaml

from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=5)
_KINDS = ("switch", "dimmer", "cover")


def _config() -> dict:
    cfg_file = resolve_config_file("config.yaml")
    if not cfg_file.exists():
        return {}
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return (cfg.get("shelly", {}) or {}) if isinstance(cfg, dict) else {}


def _load_devices() -> dict[str, dict]:
    """Device registry from config.yaml: name → {host, gen, kind, channel}."""
    raw = _config().get("devices", {}) or {}
    devices = {}
    for name, spec in raw.items():
        if isinstance(spec, str):
            spec = {"host": spec}
        if not isinstance(spec, dict) or not spec.get("host"):
            continue
        kind = str(spec.get("kind", "switch")).lower()
        try:
            channel = int(spec.get("channel", 0))
        except (TypeError, ValueError):
            channel = 0
        devices[str(name)] = {"host": str(spec["host"]),
                              "gen": int(spec.get("gen", 2)),
                              "kind": kind if kind in _KINDS else "switch",
                              "channel": channel}
    return devices


def _load_groups() -> dict[str, list[str]]:
    """Group registry: name → member device names (unknown members dropped)."""
    raw = _config().get("groups", {}) or {}
    devices = _load_devices()
    groups = {}
    for name, members in raw.items():
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list):
            continue
        valid = [str(m) for m in members if str(m) in devices]
        if valid:
            groups[str(name)] = valid
    return groups


def _lan_only(host: str) -> bool:
    """Only private/loopback hosts are allowed — this tool must never become
    an egress path (inverse of http_request's SSRF guard)."""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return host.endswith(".local")  # mDNS names are LAN-scoped


def _norm(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _resolve(device: str) -> tuple[dict | None, str]:
    """(device spec, error). Unknown device errors list valid names so a small
    model can self-correct in one round."""
    devices = _load_devices()
    if not devices:
        return None, ("No Shelly devices configured. Add them under shelly.devices "
                      "in config.yaml (see docs/LOCAL_MODELS.md).")
    spec = devices.get(device) or devices.get(_norm(device))
    if not spec:
        known = ", ".join(sorted(devices))
        groups = _load_groups()
        if groups:
            known += f" | groups: {', '.join(sorted(groups))}"
        return None, f"Unknown device '{device}'. Configured: {known}"
    if not _lan_only(spec["host"]):
        return None, f"Device '{device}' host {spec['host']} is not a LAN address — refusing."
    return spec, ""


def _members(name: str) -> list[str]:
    """Device names addressed by `name` — a group expands, a device is itself.

    'all' is implicit and covers every non-cover device, so an agent can always
    say "everything off" without the registry having to define a group for it.
    """
    groups = _load_groups()
    group = groups.get(name) or groups.get(_norm(name))
    if group:
        return group
    if _norm(name) == "all":
        return sorted(n for n, s in _load_devices().items() if s["kind"] != "cover")
    return []


async def _digest_session(ctx: ToolContext, device: str):
    """aiohttp middlewares tuple with digest auth when a vault password exists."""
    if not ctx.vault:
        return ()
    try:
        pw = ctx.vault.get(f"shelly_{device}")
    except Exception:
        pw = None
    if not pw:
        return ()
    try:
        return (aiohttp.DigestAuthMiddleware(login="admin", password=pw),)
    except Exception:
        return ()


async def _call(ctx: ToolContext, device: str, spec: dict, path_gen2: str,
                path_gen1: str) -> dict | str:
    host = spec["host"]
    url = f"http://{host}{path_gen2 if spec['gen'] >= 2 else path_gen1}"
    middlewares = await _digest_session(ctx, device)
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, middlewares=middlewares) as s:
            async with s.get(url) as resp:
                body = await resp.text()
                if resp.status == 401:
                    return (f"Device '{device}' requires auth — store its password in "
                            f"the vault as shelly_{device}.")
                if resp.status != 200:
                    return f"Device HTTP {resp.status}: {body[:150]}"
                return json.loads(body) if body.strip() else {}
    except (aiohttp.ClientError, TimeoutError) as e:
        return f"'{device}' unreachable ({type(e).__name__}) — is it powered and on the LAN?"
    except json.JSONDecodeError:
        return f"'{device}' returned non-JSON — is gen={spec['gen']} correct?"


def _status_paths(spec: dict) -> tuple[str, str]:
    """(gen2 path, gen1 path) reading this device's state.

    Gen1 dimmers expose no relay — /light/<ch> is both their status and their
    command endpoint, and it returns the channel dict directly.
    """
    ch = spec["channel"]
    if spec["kind"] == "dimmer":
        return f"/rpc/Light.GetStatus?id={ch}", f"/light/{ch}"
    if spec["kind"] == "cover":
        return f"/rpc/Cover.GetStatus?id={ch}", "/status"
    return f"/rpc/Switch.GetStatus?id={ch}", "/status"


def _fmt_status(device: str, data: dict, spec: dict) -> str:
    gen, kind, ch = spec["gen"], spec["kind"], spec["channel"]
    brightness = None
    if gen >= 2:
        on = data.get("output")
        brightness = data.get("brightness")
        power = data.get("apower")
        temp = (data.get("temperature") or {}).get("tC")
    elif kind == "dimmer":
        # /light/<ch> response, already the channel object
        on = data.get("ison")
        brightness = data.get("brightness")
        power = None
        temp = None
    else:
        entries = data.get("relays") or data.get("lights") or []
        entry = entries[ch] if ch < len(entries) else (entries[0] if entries else data)
        on = entry.get("ison")
        brightness = entry.get("brightness")
        meters = data.get("meters") or []
        power = meters[ch].get("power") if ch < len(meters) else None
        temp = data.get("temperature")
    parts = [f"{device} → {'ON' if on else 'OFF' if on is not None else 'unknown'}"]
    if on and brightness is not None:
        parts.append(f"{brightness}%")
    if power is not None:
        parts.append(f"{power}W")
    if temp is not None:
        parts.append(f"{temp}°C")
    return " · ".join(parts)


async def _one_status(ctx: ToolContext, name: str, spec: dict) -> str:
    gen2, gen1 = _status_paths(spec)
    data = await _call(ctx, name, spec, gen2, gen1)
    if isinstance(data, str):
        return f"{name} ({spec['kind']}, gen{spec['gen']}) — {data}"
    return _fmt_status(name, data, spec) + f" ({spec['kind']})"


async def _set_one(ctx: ToolContext, device: str, spec: dict, state: str) -> str:
    """Apply on/off/toggle to a single resolved device."""
    ch = spec["channel"]
    if spec["kind"] == "dimmer":
        if state == "toggle":
            gen2, gen1 = f"/rpc/Light.Toggle?id={ch}", f"/light/{ch}?turn=toggle"
        else:
            on = "true" if state == "on" else "false"
            gen2, gen1 = (f"/rpc/Light.Set?id={ch}&on={on}", f"/light/{ch}?turn={state}")
    elif state == "toggle":
        gen2, gen1 = f"/rpc/Switch.Toggle?id={ch}", f"/relay/{ch}?turn=toggle"
    else:
        on = "true" if state == "on" else "false"
        gen2, gen1 = (f"/rpc/Switch.Set?id={ch}&on={on}", f"/relay/{ch}?turn={state}")

    data = await _call(ctx, device, spec, gen2, gen1)
    if isinstance(data, str):
        return f"{device} — {data}"

    if spec["kind"] == "dimmer" and spec["gen"] < 2:
        # Gen1 /light returns the *new* state — the prior one is unknown, so
        # don't invent a "(was ...)" the way the relay path can.
        now = ("on" if data.get("ison") else "off") if state == "toggle" else state
        logger.info(f"shelly_switch: {device} → {now} by {ctx.agent_id}")
        return f"{device} → {now.upper()}"

    # Gen2 Set/Toggle returns {"was_on": bool}; Gen1 relays return {"ison": bool}.
    was_on = data.get("was_on", data.get("ison"))
    prev = "on" if was_on else "off"
    now = ("off" if was_on else "on") if state == "toggle" else state
    logger.info(f"shelly_switch: {device} → {now} (was {prev}) by {ctx.agent_id}")
    return f"{device} → {now.upper()} (was {prev})"


@tool(name="shelly_devices",
      description="List the configured Shelly smart-home devices, groups, and current state.",
      category="smarthome")
async def shelly_devices(ctx: ToolContext) -> str:
    devices = _load_devices()
    if not devices:
        return ("No Shelly devices configured. Add them under shelly.devices in "
                "config.yaml (see docs/LOCAL_MODELS.md).")
    items = sorted(devices.items())
    lines = await asyncio.gather(*(_one_status(ctx, n, s) for n, s in items))
    out = list(lines)
    groups = _load_groups()
    if groups:
        out.append("")
        out += [f"group {g}: {', '.join(m)}" for g, m in sorted(groups.items())]
        out.append("group all: every non-cover device")
    return "\n".join(out)


@tool(name="shelly_switch",
      description=("Turn a Shelly smart-home device on or off. device = the configured "
                   "name, a group name, or 'all' (use shelly_devices to list them)."),
      category="smarthome")
async def shelly_switch(ctx: ToolContext, device: str,
                        state: Annotated[str, {"choices": ["on", "off", "toggle"]}]) -> str:
    """Switch a configured device or group. state: on, off, or toggle."""
    if state not in ("on", "off", "toggle"):
        return "state must be 'on', 'off', or 'toggle'."

    members = _members(device)
    if members:
        devices = _load_devices()
        results = await asyncio.gather(
            *(_set_one(ctx, m, devices[m], state) for m in members))
        return f"{device} ({len(members)} devices) → {state.upper()}\n" + "\n".join(results)

    spec, err = _resolve(device)
    if err:
        return err
    if spec["kind"] == "cover":
        return f"'{device}' is a cover — use shelly_cover."
    return await _set_one(ctx, device, spec, state)


@tool(name="shelly_dim",
      description=("Set the brightness of a Shelly dimmer, 1-100 percent (0 turns it off). "
                   "device = the configured name, a group name, or 'all'."),
      category="smarthome")
async def shelly_dim(ctx: ToolContext, device: str, brightness: int) -> str:
    """Set a dimmer's brightness. Non-dimmer devices are skipped."""
    try:
        level = int(brightness)
    except (TypeError, ValueError):
        return "brightness must be a whole number 0-100."
    if not 0 <= level <= 100:
        return "brightness must be between 0 and 100."

    devices = _load_devices()
    members = _members(device)
    if members:
        dimmers = [m for m in members if devices[m]["kind"] == "dimmer"]
        if not dimmers:
            return f"No dimmers in '{device}'."
        results = await asyncio.gather(
            *(_dim_one(ctx, m, devices[m], level) for m in dimmers))
        return f"{device} ({len(dimmers)} dimmers) → {level}%\n" + "\n".join(results)

    spec, err = _resolve(device)
    if err:
        return err
    if spec["kind"] != "dimmer":
        return f"'{device}' is a {spec['kind']}, not a dimmer — use shelly_switch."
    return await _dim_one(ctx, device, spec, level)


async def _dim_one(ctx: ToolContext, device: str, spec: dict, level: int) -> str:
    ch = spec["channel"]
    if level == 0:
        return await _set_one(ctx, device, spec, "off")
    # Shelly clamps to 1-100; sending turn=on with the level does both in one call.
    gen2 = f"/rpc/Light.Set?id={ch}&on=true&brightness={level}"
    gen1 = f"/light/{ch}?turn=on&brightness={level}"
    data = await _call(ctx, device, spec, gen2, gen1)
    if isinstance(data, str):
        return f"{device} — {data}"
    logger.info(f"shelly_dim: {device} → {level}% by {ctx.agent_id}")
    return f"{device} → ON {level}%"


@tool(name="shelly_status",
      description="Get one Shelly device's status (on/off, brightness, power, temperature).",
      category="smarthome")
async def shelly_status(ctx: ToolContext, device: str) -> str:
    members = _members(device)
    if members:
        devices = _load_devices()
        lines = await asyncio.gather(
            *(_one_status(ctx, m, devices[m]) for m in members))
        return "\n".join(lines)
    spec, err = _resolve(device)
    if err:
        return err
    gen2, gen1 = _status_paths(spec)
    data = await _call(ctx, device, spec, gen2, gen1)
    if isinstance(data, str):
        return data
    return _fmt_status(device, data, spec)


@tool(name="shelly_cover",
      description=("Operate a Shelly cover/roller (blinds, garage): open, close, or stop. "
                   "Requires human approval — physical movement."),
      category="smarthome", hitl=True)
async def shelly_cover(ctx: ToolContext, device: str,
                       action: Annotated[str, {"choices": ["open", "close", "stop"]}]) -> str:
    """Open/close/stop a configured cover device. HITL-gated (blast radius)."""
    if action not in ("open", "close", "stop"):
        return "action must be 'open', 'close', or 'stop'."
    spec, err = _resolve(device)
    if err:
        return err
    ch = spec["channel"]
    gen2 = {"open": f"/rpc/Cover.Open?id={ch}", "close": f"/rpc/Cover.Close?id={ch}",
            "stop": f"/rpc/Cover.Stop?id={ch}"}[action]
    gen1 = f"/roller/{ch}?go={action}"
    data = await _call(ctx, device, spec, gen2, gen1)
    if isinstance(data, str):
        return data
    logger.info(f"shelly_cover: {device} → {action} by {ctx.agent_id}")
    return f"{device} cover → {action}"
