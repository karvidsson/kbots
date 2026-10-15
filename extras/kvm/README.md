# IP-KVM control

Give an agent a physical computer. A PiKVM-compatible IP-KVM (GL.iNet
Comet/GLKVM, PiKVM, …) captures a machine's HDMI and acts as its USB keyboard
and mouse, so an agent can operate **any machine plugged into it**: any OS,
BIOS, installers. Nothing is installed on the target.

Eight tools:

| Tool | Does |
|---|---|
| `kvm_devices` | list the machines this agent may drive, with video/keyboard status |
| `kvm_screen` | screenshot (`mode: image`) or OCR'd screen text (`mode: text`) |
| `kvm_click` | click at x,y (left/right/middle/double, or `move` to hover) |
| `kvm_type` | type text |
| `kvm_key` | key or shortcut: `enter`, `ctrl+c`, `win+r`, `ctrl+alt+delete` |
| `kvm_scroll` | scroll the wheel, optionally over a point |
| `kvm_power` | ATX power: status/on/off/off_hard/reset_hard. **HITL-gated** |
| `kvm_login` | sign in to the target's lock screen with a vault password |

The loop is screenshot → act → screenshot. `kvm_screen` wakes a sleeping
target first (display sleep, a laptop with its lid closed) by nudging the
mouse and tapping Shift, so nothing gets typed or clicked. It nudges before
every grab rather than trusting the KVM's signal status: GLKVM reports
`no_signal` even while the screen is on. Frames can lag the screen by a second
or two, which the tool waits out. Click coordinates are pixels of the
last `kvm_screen` image (downscaled to 1280 wide to save tokens); the tool maps
them to the KVM's absolute axis, so they land exactly at any target resolution.

## Install

```bash
cp extras/kvm/kvm.py "$KBOTS_OVERLAY/tools/"
```

## Configure

Register KVMs in the overlay's `config/config.yaml`. **The model never supplies
a host**: it picks a name, and the name maps to one KVM.

```yaml
kvm:
  devices:
    lab: <kvm>.<tailnet>.ts.net                # shorthand
    bench:
      host: 100.101.102.103                    # Tailscale IP
      user: admin                              # default: admin
      agents: [atlas]                          # agents allowed to drive it (default: any)
      keymap: en-us                            # target's layout, used by kvm_type
      verify_tls: false                        # default; KVMs ship self-signed certs
```

Store each KVM's web-UI password in the vault as `kvm_<device>`
(`kvm_bench` above). With one device configured, `device` can be omitted.

## Signing in to the target

`kvm_login` types the target machine's own login password, stored in the vault
as `kvm_<device>_login`, and presses Enter. It wakes the screen, clears the
field, types and submits in one go, because a lock screen that dozes off
between waking and typing swallows the keys. Set the device's `keymap` to the
target's keyboard layout, or any character that differs between layouts
comes out wrong. The password goes straight from the
vault to the KVM: the model never sees it, and the tool's reply never includes
it. Optional settings per device:

```yaml
    bench:
      host: ...
      login:
        user: alice        # typed first, then Tab (login screens with a user field)
        reveal: esc        # key that brings up the password field (e.g. a Windows lock curtain)
```

It runs **without approval** so agents can sign in unattended. The catch: it
types into whatever has focus, so if the lock screen isn't actually up (say a
chat window is), the password lands there and Enter sends it. Agents are told
to screenshot first. To require a human ✅ instead, add `kvm_login` to
`security.hitl.gated_tools`.

## Target setup tips

- **The KVM must show the screen the agent works on.** On a laptop with its
  lid open, the KVM is a second display: the absolute mouse lands on the main
  display and new windows open there, so the agent clicks and types blind.
  Close the lid, or make the KVM display the main one (macOS: System Settings →
  Displays → Use as Main display).
- **Mac with a Swedish/ISO keyboard:** macOS swaps a couple of keys for
  non-Apple ISO keyboards, so `~` can come out as `>`. Prefer full paths over
  `~`, and avoid those characters in a login password.
- **Launch apps by keyboard** (Spotlight: `cmd+space`, name, `enter`) rather than
  assuming which app is frontmost. A screenshot shows only the KVM display.

## Reaching a KVM on another network

Use **Tailscale**. GLKVM firmware has it built in (web UI → Remote Access →
Tailscale), and PiKVM supports it too. Put the KVM and the kbots host on the
same tailnet, then use the KVM's MagicDNS name (`*.ts.net`) or `100.x` address
as `host`. Traffic stays end-to-end encrypted, and the KVM's web UI is never
exposed to the internet.

The tool only talks to LAN, loopback, Tailscale (`100.64.0.0/10`, `*.ts.net`)
and mDNS (`*.local`) hosts. If you front the KVM some other way, such as a
reverse proxy or a tunnel, set `public: true` on that device to opt out of the
guard. Then **set `verify_tls: true`**, because the password crosses the
internet.

## Security model

- Every tool is non-safe, so access control limits them to owner/admin senders.
- `agents:` pins a machine to the agents allowed to drive it. Other agents
  don't even see it in `kvm_devices`.
- `kvm_power` is HITL-gated by default because a hard off loses the target's
  unsaved work. `kvm_login` is not (see above). To approve every action instead, add `kvm_click`/`kvm_type`/
  `kvm_key` to `security.hitl.gated_tools`. Anything an agent can type on the
  target, it can run there.
- Give the agent its own KVM user if your firmware supports one, not your
  admin login.

## Tests

```bash
uv run --extra dev pytest extras/kvm
```
