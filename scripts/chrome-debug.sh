#!/usr/bin/env bash
# chrome-debug.sh — launch a debug-enabled Google Chrome that the chrome_browser
# tool can drive, carrying your real logins.
#
#   scripts/chrome-debug.sh                    # start (or reuse) the debug Chrome
#   scripts/chrome-debug.sh --refresh          # re-seed logins from your live profile
#   scripts/chrome-debug.sh --status           # report port state + known profiles
#   scripts/chrome-debug.sh --profile NAME     # open a window on a named profile
#   scripts/chrome-debug.sh --profile NAME --open URL   # …at a specific URL
#
# macOS only. This runs a SEPARATE Chrome instance on its own profile directory,
# so your everyday Chrome keeps running untouched. The separate directory is
# required: since Chrome 136, --remote-debugging-port is ignored on the default
# profile dir for security (https://developer.chrome.com/blog/remote-debugging-port).
# We seed that dir from your real profile once, so cookies/logins carry over.
#
# Named profiles (--profile) live inside the same user-data-dir, so one Chrome
# instance and one debug port serve all of them. A fresh named profile starts
# with NO logins — the point is the sign-in-once flow: the user logs in by hand
# in its visible window, the session then persists for the agent across
# restarts, and deleting "$DEBUG_DIR/NAME" revokes it.
set -euo pipefail

PORT="${KBOTS_CHROME_DEBUG_PORT:-9222}"
DEBUG_DIR="${KBOTS_CHROME_DEBUG_DIR:-$HOME/.kbots-chrome-debug}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REAL_PROFILE="$HOME/Library/Application Support/Google/Chrome"

MODE="start"
PROFILE=""
OPEN_URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --status)     MODE="status" ;;
    --refresh)    MODE="refresh" ;;
    --foreground) MODE="foreground" ;;
    --install)    MODE="install" ;;
    --uninstall)  MODE="uninstall" ;;
    --profile)    PROFILE="${2:-}"; shift ;;
    --open)       OPEN_URL="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -n "$PROFILE" ] && ! printf '%s' "$PROFILE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$'; then
  echo "invalid profile name: '$PROFILE' (letters, digits, space, - and _ only)" >&2
  exit 2
fi

port_up() { curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }

# Endpoint discovery file: who owns this port, which user-data-dir, which pid.
# The chrome_browser tool reads it to verify the responder is OUR Chrome (not a
# squatter or the user's own instance), and agent scripts read it instead of
# hardcoding the port. Default instance → chrome-debug.json; others get -PORT.
endpoint_file() {
  local dir="${KBOTS_OVERLAY:-$HOME/kbots-overlay}/data"
  [ "$PORT" = "9222" ] && echo "$dir/chrome-debug.json" || echo "$dir/chrome-debug-$PORT.json"
}

write_endpoint() {  # $1 = chrome pid
  local f; f="$(endpoint_file)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  printf '{"port": %s, "user_data_dir": "%s", "pid": %s, "started_at": "%s"}\n' \
    "$PORT" "$DEBUG_DIR" "$1" "$(date -Iseconds)" > "$f" 2>/dev/null || true
}

LABEL="com.kbots.chrome-debug"; [ "$PORT" != "9222" ] && LABEL="com.kbots.chrome-debug-$PORT"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

list_profiles() {
  for d in "$DEBUG_DIR"/*/; do
    [ -f "$d/Preferences" ] && echo "profile: $(basename "$d")"
  done
  true
}

if [ "$MODE" = "status" ]; then
  if port_up; then
    echo "up: Chrome debug endpoint listening on 127.0.0.1:$PORT"
    list_profiles
    exit 0
  fi
  echo "down: nothing on 127.0.0.1:$PORT"
  list_profiles
  exit 1
fi

if [ "$(uname)" != "Darwin" ]; then
  echo "chrome-debug.sh is macOS-only." >&2; exit 1
fi

if [ "$MODE" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  rm -f "$(endpoint_file)"
  echo "removed: $LABEL (Chrome itself was left running if it was up)"
  exit 0
fi

if [ "$MODE" = "install" ]; then
  # Supervise the debug Chrome under launchd: KeepAlive restarts it after a
  # crash, a Chrome self-update relaunch (which drops CLI flags), ⌘Q, or login.
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  DATA_DIR_OUT="${KBOTS_OVERLAY:-$HOME/kbots-overlay}/data"
  mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR_OUT"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$SELF</string>
    <string>--foreground</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>KBOTS_CHROME_DEBUG_PORT</key><string>$PORT</string>
    <key>KBOTS_CHROME_DEBUG_DIR</key><string>$DEBUG_DIR</string>
    <key>KBOTS_OVERLAY</key><string>${KBOTS_OVERLAY:-$HOME/kbots-overlay}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$DATA_DIR_OUT/chrome-debug.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR_OUT/chrome-debug.log</string>
</dict></plist>
PLIST_EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
    echo "installed + started: $LABEL (port $PORT, dir $DEBUG_DIR)"
    echo "logs: $DATA_DIR_OUT/chrome-debug.log"
  else
    echo "wrote $PLIST but bootstrap FAILED — run: launchctl bootstrap gui/$(id -u) $PLIST" >&2
    exit 1
  fi
  exit 0
fi

if [ ! -x "$CHROME" ]; then
  echo "Google Chrome not found at: $CHROME" >&2; exit 1
fi

if [ "$MODE" = "foreground" ]; then
  # launchd entry point: exec Chrome so launchd supervises the real process.
  # If the port is already owned (an unsupervised instance, or a squatter),
  # exit non-zero — ThrottleInterval stops a tight relaunch loop and the log
  # says exactly what is in the way.
  if port_up; then
    echo "port $PORT already answering — refusing to double-start. If this is an" \
         "old unsupervised debug Chrome, quit it (⌘Q) and launchd will take over." >&2
    exit 1
  fi
  if [ ! -d "$DEBUG_DIR/Default" ]; then
    echo "seeding debug profile from your Chrome profile…"
    mkdir -p "$DEBUG_DIR"
    rsync -a --delete \
      --exclude 'Cache' --exclude 'Code Cache' --exclude 'GPUCache' \
      --exclude 'ShaderCache' --exclude 'GrShaderCache' --exclude 'DawnCache' \
      --exclude 'Service Worker/CacheStorage' --exclude 'Service Worker/ScriptCache' \
      "$REAL_PROFILE/Local State" "$REAL_PROFILE/Default" "$DEBUG_DIR/" 2>/dev/null || \
      echo "  (partial seed — some files were locked)"
  fi
  # exec replaces this shell, so Chrome's pid is $$. Write the endpoint file
  # from a detached helper once the port actually answers.
  ( for _ in $(seq 1 30); do sleep 1; port_up && { write_endpoint "$$"; exit 0; }; done ) &
  echo "foreground launch on port $PORT (pid $$)"
  exec "$CHROME" --remote-debugging-port="$PORT" --user-data-dir="$DEBUG_DIR" \
       --no-first-run --no-default-browser-check --restore-last-session=false \
       --disable-session-crashed-bubble --disable-infobars
fi

if port_up; then
  if [ -z "$PROFILE" ]; then
    echo "Chrome debug endpoint already listening on 127.0.0.1:$PORT — reusing it."
    exit 0
  fi
  # Instance already running: invoking the binary with the same user-data-dir
  # hands off to it and opens a window on the requested profile (created on
  # first use), then the target shows up in /json/list.
  echo "Opening a window on profile '$PROFILE' in the running debug Chrome…"
  ARGS=(--user-data-dir="$DEBUG_DIR" --profile-directory="$PROFILE"
        --no-first-run --no-default-browser-check)
  [ -n "$OPEN_URL" ] && ARGS+=("$OPEN_URL")
  "$CHROME" "${ARGS[@]}" >/dev/null 2>&1 &
  exit 0
fi

# Seed the debug profile from your real one so logins carry over. Runs on first
# use and whenever you pass --refresh. Caches are excluded to keep it fast/light.
# Only the Default profile is ever seeded — named profiles stay clean on purpose.
if [ ! -d "$DEBUG_DIR/Default" ] || [ "$MODE" = "refresh" ]; then
  echo "Seeding debug profile from your Chrome profile (cookies + logins)…"
  mkdir -p "$DEBUG_DIR"
  rsync -a --delete \
    --exclude 'Cache' --exclude 'Code Cache' --exclude 'GPUCache' \
    --exclude 'ShaderCache' --exclude 'GrShaderCache' --exclude 'DawnCache' \
    --exclude 'Service Worker/CacheStorage' --exclude 'Service Worker/ScriptCache' \
    "$REAL_PROFILE/Local State" "$REAL_PROFILE/Default" "$DEBUG_DIR/" 2>/dev/null || \
    echo "  (partial seed — some files were locked; log in once in the debug window if needed)"
fi

echo "Launching debug Chrome on port $PORT (separate window; your main Chrome is untouched)…"
ARGS=(--remote-debugging-port="$PORT" --user-data-dir="$DEBUG_DIR"
      --no-first-run --no-default-browser-check --restore-last-session=false
      --disable-session-crashed-bubble --disable-infobars)
[ -n "$PROFILE" ] && ARGS+=(--profile-directory="$PROFILE")
[ -n "$OPEN_URL" ] && ARGS+=("$OPEN_URL")
"$CHROME" "${ARGS[@]}" >/dev/null 2>&1 &
CHROME_PID=$!

for _ in $(seq 1 20); do
  sleep 1
  if port_up; then
    write_endpoint "$CHROME_PID"
    echo "ready: chrome_browser can now attach on 127.0.0.1:$PORT"
    echo "tip: 'scripts/chrome-debug.sh --install' supervises this under launchd so it survives crashes and updates."
    exit 0
  fi
done
echo "timed out waiting for the debug endpoint on port $PORT" >&2
exit 1
