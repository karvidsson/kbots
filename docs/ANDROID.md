# Android Device Control

The `android_device` tool lets agents drive an Android device over ADB —
either a **headless emulator** running on the host, or a **real phone**
connected over USB/Wi-Fi. The tool is backend-agnostic: agents written
against the emulator work unchanged when a phone is plugged in (a physical
device is preferred automatically when both are attached).

## How agents use it

Screenshot-driven loop, like `computer` control:

1. `android_device(action="screenshot")` → saves a PNG; open it with Read
2. Decide coordinates from the image
3. `tap` / `swipe` / `type` / `key` at those pixels
4. Screenshot again to verify — Android UIs animate

Other actions: `launch` (by package name), `open_url` (links and app deep
links), `install` (sideload an APK), `status`, `release`.

There is **one shared device** — driving actions take the cross-process
reservation (same mechanism as `chrome_browser`); a second agent is refused
with a message naming the holder. Agents should `release` when done; idle
reservations expire on their own.

## Host setup (macOS, Apple Silicon)

```bash
brew install openjdk@21 android-commandlinetools
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
export PATH="$JAVA_HOME/bin:$PATH"
yes | sdkmanager --licenses
sdkmanager "platform-tools" "emulator" "system-images;android-35;google_apis;arm64-v8a"
echo no | avdmanager create avd -n kbots \
    -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_7
```

The emulator is booted headless by `scripts/android-emulator.sh` (no window,
software GPU — works on a lid-closed Mac). The tool auto-launches it on first
use when no device is attached; cold boot takes about a minute, snapshot
boots take seconds.

Env overrides:

| Var | Meaning | Default |
|---|---|---|
| `KBOTS_ANDROID_SDK` | SDK root | `/opt/homebrew/share/android-commandlinetools` |
| `KBOTS_ANDROID_AVD` | AVD the launcher boots | `kbots` |
| `KBOTS_ANDROID_SERIAL` | Pin a specific device serial | first physical, else emulator |

## Emulator vs real phone

| | Emulator | Real phone |
|---|---|---|
| Play Integrity | ❌ fails — some apps restrict/refuse | ✅ passes |
| Social platforms | Higher-risk sessions (bans/shadowbans) | Normal |
| Incoming SMS | ❌ no SIM | ✅ readable over ADB |
| Setup | Automatic | Enable USB debugging, plug in |

**Before automating a valuable account (e.g. a real social identity), prefer
a real phone.** The emulator is for development, testing flows, and
low-stakes browsing.

Installing apps on the emulator: the `google_apis` image has no Play Store —
sideload APKs (`action='install'`) from a trusted source, or use an
alternative front-end like Aurora Store. Avoid signing a valuable Google
account into an emulator.

## Agent playbook

Guidance to give agents that drive the device (put deployment-specific facts
— which phone, which accounts are logged in — in the agent's CLAUDE.md; this
section is the generic technique). The `/android_task` skill packages this
loop as a slash command.

**The loop is look → act → verify.** Never chain blind actions:

1. `screenshot` and Read it — this is the only source of truth for what is
   on screen. UI state from memory or a previous turn is stale.
2. Decide ONE action from what you actually see (tap/swipe/type/key).
3. `screenshot` again to confirm the result before the next step. Android
   animates; a tap during a transition lands on the wrong thing.

**Coordinates:** they refer to the full-resolution screen (the tool reports
it, e.g. 1080x2400). If the image you read is scaled, map your estimate back
to device pixels before tapping. Tap the *center* of a target, not its edge.

**Typing:** tap the input field first (verify the keyboard appeared), then
`type`. For search boxes, follow with `key` enter. If text lands in the
wrong place, you typed into an unfocused screen — screenshot, refocus, retry.

**Waiting:** apps load asynchronously. If a screenshot shows a spinner or
skeleton UI, wait a few seconds and screenshot again rather than tapping
into the void.

**Getting unstuck:** `key` back exits dialogs and misnavigations; `key` home
+ `launch` restarts an app flow cleanly. Three failed attempts at the same
step = stop and report what you see instead of thrashing.

**SMS codes (real phone only):** verification codes arrive on the device —
swipe down from the top (`swipe`, e.g. 540,50 → 540,1200) to read the
notification shade, or open the messages app. You do not need the user to
relay codes when the number attached to the account is this phone's.

**Sharing:** the device is shared across agents — the reservation is taken
automatically on your first driving action and refused to others while you
hold it. `release` the moment you're done. If refused, the message names the
holder; do not retry in a loop.

**Real-account discipline:** on a phone with real logged-in identities,
anything you tap is real — posts publish, messages send, purchases charge.
Confirm with the user before irreversible or public actions unless they
already told you to proceed. The headless emulator is the place for
experiments; note it fails Play Integrity and has no SIM.

## Real phone checklist

1. Settings → About phone → tap *Build number* 7× (enables Developer options)
2. Developer options → enable *USB debugging*
3. Plug into the host over USB, accept the RSA fingerprint prompt on the phone
4. `adb devices` should show the serial as `device` — the tool now prefers it

For Wi-Fi ADB (Android 11+): Developer options → *Wireless debugging* →
pair, then `adb connect <ip>:<port>`.
