# Assets

Brand figure for kbots: a screen-headed bot on a stick body with a dome base.
The screen **is** the face — which makes agent avatars trivial to derive.

```
figure.svg            — the mascot, standalone
banner.svg            — README banner (arvidsson.tech style: dark gradient,
                        40px grid, JetBrains Mono, red #ff4444 accents)
avatars/              — square agent avatars (the screen face, straight-on)
  avatar-default.svg  — capsule eyes, red (brand accent)
  avatar-happy.svg    — arc eyes, amber
  avatar-focus.svg    — round eyes, violet
```

## Deriving a new agent avatar

Don't copy by hand — generate one (the add-agent flow in
`scripts/settings.py` offers this automatically):

```bash
uv run python scripts/avatar.py --list                       # see styles + accents
uv run python scripts/avatar.py --eyes wink --accent blue \
    --out <overlay>/agents/<name>/avatar                     # writes .svg + 512px .png
```

Add `--set-discord <account>` to push it straight to the bot via the API
(token from the vault), or upload the PNG manually: Developer Portal → app → Bot.

The generator varies two things (manually: copy `avatars/avatar-default.svg`
and change the same two):

1. **Eyes** — any simple shape (capsules, arcs, dots, a scanline…). This is the agent's expression.
2. **Accent color** — one color per agent, used for the eyes.

Keep the screen (`#0a0a0f`) and the slate frame (`#7d8695`) identical across all
avatars so agents read as one family. The large corner radius means the avatars
crop cleanly into circles (Discord, Telegram, etc.).
