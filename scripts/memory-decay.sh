#!/usr/bin/env bash
# RETIRED 2026-08-22. Memory decay runs inside the engine now.
#
# This script implemented the decay lifecycle a second time, in SQL, and said
# it ran "daily at 03:00 UTC via kbots-memory-decay.timer" — a systemd unit
# that no macOS install has and no installer creates. It never ran anywhere.
# It also resolved the database as $KBOTS_HOME/data/memory.db, which stopped
# being the live store when data_dir moved to the overlay, so scheduling it
# today would have decayed a retired database.
#
# It is kept as a signpost rather than deleted, because the timer name is
# referenced in older docs and in scripts/settings.py, and a missing script
# reads as "not installed yet" rather than "replaced".
#
# The engine task: src/core/memory_decay.py, gated on
# defaults.memory.decay_enabled, tuned under defaults.memory.decay.
set -uo pipefail

cat <<'EOF'
memory-decay.sh is retired.

Decay now runs inside the engine (src/core/memory_decay.py) on a daily task,
against the store the engine itself opened. Enable and tune it in config:

  defaults:
    memory:
      decay_enabled: true
      decay:
        interval_hours: 24
        rate: 0.0108           # confidence lost per day when not recalled
        archive_threshold: 0.05
        purge_archived: false  # archiving is reversible, deleting is not

Lessons never decay. To check what it has done:

  sqlite3 <data_dir>/memory.db \
    "SELECT timestamp, new_value FROM changelog WHERE action IN ('decay','purge')
     ORDER BY timestamp DESC LIMIT 10;"
EOF
exit 0
