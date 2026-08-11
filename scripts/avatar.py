"""kbots avatar generator CLI — see src/lib/avatar_gen.py for the engine.

Usage:
    uv run python scripts/avatar.py --eyes capsule --accent red --out agents/foo/avatar
    uv run python scripts/avatar.py --list
    uv run python scripts/avatar.py --eyes ring --accent teal --out /tmp/bot --set-discord foo

Writes <out>.svg always, and <out>.png (512px, what Discord wants) when
Playwright's Chromium is available. --set-discord uploads it to the bot.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.lib.avatar_gen import (  # noqa: E402,F401 — re-exported for settings.py/tests
    ACCENTS,
    EYES,
    FRAME,
    SCREEN,
    build_svg,
    render_png,
    resolve_accent,
    resolve_bot_token,
    upload_discord_avatar,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a kbots agent avatar (SVG + Discord-ready PNG)")
    ap.add_argument("--eyes", default="capsule", help=f"eye style: {', '.join(EYES)}")
    ap.add_argument("--accent", default="red", help=f"preset ({', '.join(ACCENTS)}) or hex like #4ade80")
    ap.add_argument("--out", help="output path without extension, e.g. agents/foo/avatar")
    ap.add_argument("--size", type=int, default=512, help="PNG size in px (default 512)")
    ap.add_argument("--no-png", action="store_true", help="write only the SVG")
    ap.add_argument("--set-discord", metavar="ACCOUNT",
                    help="also set the PNG as this bot account's Discord avatar (vault token)")
    ap.add_argument("--list", action="store_true", help="list eye styles and accent presets")
    args = ap.parse_args()

    if args.list:
        print("eye styles: " + ", ".join(EYES))
        print("accents:    " + ", ".join(f"{k} ({v[0]})" for k, v in ACCENTS.items()))
        return
    if not args.out:
        ap.error("--out is required (or use --list)")

    accent = resolve_accent(args.accent)
    svg = build_svg(args.eyes, accent)

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    svg_path = out.with_suffix(".svg")
    svg_path.write_text(svg)
    print(f"wrote {svg_path}")

    if not args.no_png:
        png_path = out.with_suffix(".png")
        if render_png(svg_path, png_path, args.size):
            print(f"wrote {png_path} ({args.size}×{args.size})")
            if args.set_discord:
                success, msg = upload_discord_avatar(png_path, resolve_bot_token(args.set_discord))
                print(msg if success else f"Discord upload failed: {msg}", file=None if success else sys.stderr)
                if not success:
                    sys.exit(1)
            else:
                print("Upload it: Discord Developer Portal → your application → Bot → icon")
        elif args.set_discord:
            raise SystemExit("--set-discord needs the PNG — install Chromium: uv run playwright install chromium")


if __name__ == "__main__":
    main()
