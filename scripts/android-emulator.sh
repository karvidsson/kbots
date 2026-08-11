#!/usr/bin/env bash
# Boot the kbots Android emulator headless. No-op if it is already running.
#
# Used by the android_device tool's auto-launch (src/tools/android.py) and
# usable standalone. The emulator runs with no window (works on a headless /
# lid-closed Mac) and is driven entirely over ADB.
#
# Env:
#   KBOTS_ANDROID_SDK  — SDK root (default: /opt/homebrew/share/android-commandlinetools)
#   KBOTS_ANDROID_AVD  — AVD name to boot (default: kbots)
set -euo pipefail

SDK="${KBOTS_ANDROID_SDK:-/opt/homebrew/share/android-commandlinetools}"
AVD="${KBOTS_ANDROID_AVD:-kbots}"
LOG="${TMPDIR:-/tmp}/kbots-android-emulator.log"

EMULATOR="$SDK/emulator/emulator"
ADB="$SDK/platform-tools/adb"
[ -x "$EMULATOR" ] || { echo "emulator binary not found at $EMULATOR" >&2; exit 1; }
[ -x "$ADB" ] || ADB="$(command -v adb)" || { echo "adb not found" >&2; exit 1; }

# Already running? (any emulator-* serial in 'device' state)
if "$ADB" devices | grep -qE '^emulator-[0-9]+[[:space:]]+device'; then
    echo "emulator already running"
    exit 0
fi

"$EMULATOR" -list-avds | grep -qx "$AVD" || {
    echo "AVD '$AVD' does not exist. Create it with:" >&2
    echo "  avdmanager create avd -n $AVD -k 'system-images;android-35;google_apis;arm64-v8a' -d pixel_7" >&2
    exit 1
}

echo "booting AVD '$AVD' headless (log: $LOG)"
nohup "$EMULATOR" -avd "$AVD" \
    -no-window -no-audio -no-boot-anim \
    -gpu swiftshader_indirect \
    >>"$LOG" 2>&1 &
EMULATOR_PID=$!

# The emulator is nohup'd and outlives this script, so it ends up orphaned to
# PID 1 with nobody owning it. Print the pid on a parseable line: the caller
# records it and can shut the emulator down later (src/tools/android.py).
# Only printed when *we* booted it — the "already running" path above exits
# earlier precisely so a caller never claims ownership of someone else's
# emulator.
echo "emulator_pid=$EMULATOR_PID"

"$ADB" wait-for-device
# wait-for-device returns at ADB attach, before Android finishes booting
for _ in $(seq 1 60); do
    boot="$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
    [ "$boot" = "1" ] && { echo "boot complete"; exit 0; }
    sleep 2
done
echo "emulator attached but boot did not complete within 120s" >&2
exit 1
