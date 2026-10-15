# Owner-entered secrets

Use `ask_owner_secret` when a tool needs a password, token or key that the
owner holds. It posts one card in the requesting agent's Discord home channel.
The owner taps **Enter**, types the value in a private Discord form, and the
engine writes it straight to the vault. The agent never sees the value. It only
gets a note in its channel that the key was saved (or declined).

```python
ask_owner_secret(
    key="kvm_lab",
    label="KVM password for lab",
    purpose="To log in to the lab KVM and take screenshots.",
    request_key="kvm-lab-password",
)
```

Never ask for a secret in chat. Chat is stored, summarised and replayed into
model context. The form is not.

## What the owner sees

One card, edited in place (see the feature UX conventions):

- **Amber:** `<agent> needs: <label>`, with the purpose, the vault key it will
  be stored under, and two buttons: **Enter <label>** and **Not now**.
- **Green:** `Saved: <label>`, with who saved it. The buttons are gone.
- **Grey:** declined, or expired after 7 days. Nothing was stored.

Only the owner (`waiting_on_you.owner_id`, or the sole Discord admin) can open
the form or decline. Anyone else gets a private refusal. Buttons survive a
restart.

## Guarantees

- **The value goes only to the vault.** It is not logged, not on the card, not
  in `owner-secrets.db`, and not in the agent's notification.
- **Allowed keys only.** An agent can request only keys under
  `owner_secrets.allowed_prefixes` (default `kvm_`), so it cannot get the owner
  to overwrite the bot token or another integration's credential.
- **Owner-, card- and revision-bound.** A form opened from an older card state,
  or after the request closed or expired, writes nothing.
- **Visible at once.** Tool processes re-read the vault when it changes on
  disk, so the saved key works on the next tool call without a restart.
  Concurrent writers are serialised, so no process can put a stale copy back.

## Configuration

```yaml
owner_secrets:
  allowed_prefixes: [kvm_]
```

Add a prefix for each integration whose tool reads owner-entered secrets.

## Caveat

Discord forms have no masked password field. The value is visible on the
owner's own screen while typing, and it passes through Discord's servers on
its way to the bot. For a secret that must never leave the host, use
`vault-manage.py` on the host instead.
