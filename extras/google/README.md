# Google Workspace

Gmail, Calendar, Meet, Drive — 15 tools (`gmail_*`, `calendar_*`, `meet_*`, `drive_*`).

Install: `cp extras/google/google.py "$KBOTS_OVERLAY/tools/"`

Auth: Google OAuth2 via Core's `src.auth.oauth2.GoogleAuth` (stays in the engine —
importable from an installed extra). First-time consent + re-auth:
`scripts/google-reauth.py`.

## Draft-before-send

`send_email` defaults to **draft mode** (`draft=True`): instead of sending
immediately, the email parks as a durable owner_ask card. The owner sees
recipient, subject, and a body preview, with options:

- **Send** — executes the send automatically
- **Edit** — cancels the draft and wakes the agent to ask what to change
- **Cancel** — cancels the draft, agent is notified

To send immediately (e.g. for scheduled tasks), pass `draft=False`. HITL
`gated_tools` still apply if configured. Attachments are validated before
the ask is created, so missing-file or oversize errors fail fast.

Bundled skill: `debrief.yaml` (daily debrief — needs the **trello** extra too).
Install it with `cp extras/google/debrief.yaml "$KBOTS_OVERLAY/skills/"`.
