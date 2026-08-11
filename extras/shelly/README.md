# Shelly smart-home control

Local LAN control of Shelly devices (Gen1 REST and Gen2+/Plus/Pro RPC).
Five tools: `shelly_devices`, `shelly_switch`, `shelly_dim`, `shelly_status`,
`shelly_cover`.

## Install

```bash
cp extras/shelly/shelly.py "$KBOTS_OVERLAY/tools/"
```

Restart the service, then `shelly_devices` should list your registry.

## Configure

Devices are registered in the overlay's `config/config.yaml` — **the model never
supplies a host**. It picks a name; the name maps to exactly one output.

```yaml
shelly:
  devices:
    office_light: 192.168.1.42                        # shorthand: gen2 switch
    heater: {host: 192.168.1.43, gen: 1}              # full form
    blinds: {host: 192.168.1.44, kind: cover}
    lamp: {host: 192.168.1.45, gen: 1, kind: dimmer}  # brightness-capable
    hall_b: {host: 192.168.1.46, channel: 1}          # 2nd relay of a 2PM
  groups:
    downstairs: [office_light, lamp]                  # address many at once
```

- `kind` — `switch` (default), `dimmer`, or `cover`. A Gen1 Dimmer 2 has no
  `/relay` endpoint, so `kind: dimmer` is required or calls 404.
- `channel` — relay/light id on multi-channel devices (Plus 2PM exposes
  `switch:0` and `switch:1`). Defaults to 0.
- `groups` — fan one call out to many devices. An implicit `all` group covers
  every device except covers, so bulk-moving blinds stays an explicit act.

Optional Gen2 digest auth: store the device password in the vault as
`shelly_<device>`.

## Security model

The inverse of the generic `http_request` tool, which blocks private networks:
these tools may talk **only** to private/LAN hosts drawn from the registry. Worst
case is toggling the wrong configured device.

`shelly_switch` and `shelly_dim` ship ungated — lights and plugs are reversible,
and gating would deadlock unattended automations. Add them to
`security.hitl.gated_tools` to require approval. `shelly_cover` is HITL-gated by
default because it moves something physical.

## Tests

```bash
uv run pytest extras/shelly
```
