# Google Workspace

Gmail, Calendar, Meet, Drive — 15 tools (`gmail_*`, `calendar_*`, `meet_*`, `drive_*`).

Install: `cp extras/google/google.py "$KBOTS_OVERLAY/tools/"`

Auth: Google OAuth2 via Core's `src.auth.oauth2.GoogleAuth` (stays in the engine —
importable from an installed extra). First-time consent + re-auth:
`scripts/google-reauth.py`. `send_email` is a good candidate for
`security.hitl.gated_tools`.

Bundled skill: `debrief.yaml` (daily debrief — needs the **trello** extra too).
Install it with `cp extras/google/debrief.yaml "$KBOTS_OVERLAY/skills/"`.
