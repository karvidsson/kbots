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
and change the same two — keep its `viewBox`, see below):

1. **Eyes** — any simple shape (capsules, arcs, dots, a scanline…). This is the agent's expression.
2. **Accent color** — one color per agent, used for the eyes.

Keep the screen (`#0a0a0f`) and the slate frame (`#7d8695`) identical across all
avatars so agents read as one family.

Discord crops an avatar to the circle **inscribed** in the square
image, so the corners of a full-bleed square are cut off — a rounded badge drawn
edge to edge still loses part of its frame. What makes these survive the crop is
the `viewBox`: it is wider than the artwork (`-18 -18 196 196` around a badge
drawn on a 160 face), which insets the badge until it fits inside that circle.
Change the eyes and the accent, not the `viewBox` — and if you move the frame or
its stroke, regenerate rather than editing by hand, since `avatar_gen.py` derives
the fit from those numbers. `tests/test_avatar_geometry.py` enforces this for
both generated output and the files in this directory.
