"""kbots avatar composition — brand template, eye styles, accents, PNG render, Discord upload.

Every agent avatar is the mascot's screen face with two knobs: an eye style
(the agent's expression) and an accent color. The frame and screen stay
identical across agents so the fleet reads as one family (see assets/README.md).

Used by scripts/avatar.py (CLI) and src/tools/avatar_tools.py (agent tool).
"""

import base64
import json
import math
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Family constants — keep in sync with assets/README.md
SCREEN = "#0a0a0f"
SCREEN_EDGE = "#12121a"
FRAME = "#7d8695"

# === Badge geometry ===
# The screen rect and its frame stroke are the only artwork that reaches the
# edge; the eyes sit well inside. The face these were drawn against is 160
# units, and the gradient below still spans it in user space.
FACE = 160
BADGE_INSET = 9         # rect x/y
BADGE_SIZE = 142        # rect width/height
BADGE_RADIUS = 36       # rect corner radius
BADGE_STROKE = 7        # frame stroke width
BADGE_CENTRE = BADGE_INSET + BADGE_SIZE / 2      # 80.0


def badge_outer_radius() -> float:
    """Distance from the badge centre to the furthest point the frame paints.

    Two things make this larger than it looks. A stroke is centred on its path,
    so the frame reaches half a stroke width beyond the rect; and the furthest
    point is on a rounded corner, not on a flat edge — it sits on the corner
    arc, out along the diagonal from that arc's centre.
    """
    half_side = BADGE_SIZE / 2 + BADGE_STROKE / 2        # 74.5
    corner_radius = BADGE_RADIUS + BADGE_STROKE / 2      # 39.5
    offset = half_side - corner_radius                   # arc centre from badge centre
    return math.hypot(offset, offset) + corner_radius    # 88.9975


# Discord crops a bot avatar to the circle INSCRIBED in the square image, so a
# square viewBox is only safe if that inscribed circle contains the whole badge.
# Drawn against the 160 face the badge reaches 89.0 from its centre against a
# crop radius of 80, so every generated avatar lost ~11% of its corners.
#
# Widening the viewBox insets the artwork without touching a single coordinate,
# which keeps the drawing exactly as authored and perfectly centred. Deriving
# the numbers here rather than writing them into the template means the fit
# survives any later change to the rect, its radius or its stroke.
SAFE_MARGIN = 1.10                                        # ~9% breathing room
VIEW_HALF = math.ceil(badge_outer_radius() * SAFE_MARGIN)  # 98
VIEW_MIN = BADGE_CENTRE - VIEW_HALF                        # -18.0
VIEW_SIZE = VIEW_HALF * 2                                  # 196


def view_box() -> str:
    """The viewBox that keeps the badge inside Discord's circular crop."""
    return f"{VIEW_MIN:g} {VIEW_MIN:g} {VIEW_SIZE:g} {VIEW_SIZE:g}"

# Accent presets. Each is (primary, secondary) — secondary is used for the
# right eye so the face gets the subtle two-tone the brand mascot has.
ACCENTS: dict[str, tuple[str, str]] = {
    "red": ("#ff4444", "#ff6b6b"),      # brand default (arvidsson.tech)
    "teal": ("#2dd4bf", "#5eead4"),
    "amber": ("#fbbf24", "#fcd34d"),
    "violet": ("#a78bfa", "#c4b5fd"),
    "blue": ("#60a5fa", "#93c5fd"),
    "green": ("#4ade80", "#86efac"),
    "pink": ("#f472b6", "#f9a8d4"),
    "orange": ("#fb923c", "#fdba74"),
}


def _lighten(hex_color: str, amount: float = 0.25) -> str:
    """Mix a hex color toward white for the secondary eye tint."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(c + (255 - c) * amount)  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


def resolve_accent(name: str) -> tuple[str, str]:
    if name.lower() in ACCENTS:
        return ACCENTS[name.lower()]
    if name.startswith("#") and len(name) in (4, 7):
        primary = name if len(name) == 7 else "#" + "".join(c * 2 for c in name[1:])
        return primary, _lighten(primary)
    raise SystemExit(f"Unknown accent '{name}' — use a preset ({', '.join(ACCENTS)}) or a hex color like #4ade80")


# Eye styles: SVG fragments inside the 160×160 face, eyes centred around
# (58,80) and (103,80). {a} = primary accent, {b} = secondary accent.
EYES: dict[str, str] = {
    "capsule": (
        '  <ellipse cx="58" cy="82" rx="10.5" ry="16" fill="{a}" transform="rotate(-5 58 82)"/>\n'
        '  <ellipse cx="103" cy="78" rx="10.5" ry="16" fill="{b}" transform="rotate(6 103 78)"/>'
    ),
    "arc": (
        '  <path d="M44 87 C 50 74, 66 74, 72 87" stroke="{a}" stroke-width="9" stroke-linecap="round" fill="none"/>\n'
        '  <path d="M90 84 C 96 71, 112 71, 118 84" stroke="{b}" stroke-width="9" stroke-linecap="round" fill="none"/>'
    ),
    "ring": (
        '  <circle cx="58" cy="80" r="12.5" stroke="{a}" stroke-width="8" fill="none"/>\n'
        '  <circle cx="103" cy="80" r="12.5" stroke="{b}" stroke-width="8" fill="none"/>'
    ),
    "dot": (
        '  <circle cx="58" cy="80" r="9" fill="{a}"/>\n'
        '  <circle cx="103" cy="80" r="9" fill="{b}"/>'
    ),
    "wink": (
        '  <ellipse cx="58" cy="80" rx="10.5" ry="16" fill="{a}" transform="rotate(-5 58 80)"/>\n'
        '  <path d="M90 82 C 96 74, 112 74, 118 82" stroke="{b}" stroke-width="9" stroke-linecap="round" fill="none"/>'
    ),
    "scan": (
        '  <rect x="42" y="72" width="76" height="15" rx="7.5" fill="{a}"/>\n'
        '  <rect x="88" y="72" width="22" height="15" rx="7.5" fill="{b}"/>'
    ),
}


def build_svg(eyes: str, accent: tuple[str, str]) -> str:
    if eyes not in EYES:
        raise SystemExit(f"Unknown eye style '{eyes}' — one of: {', '.join(EYES)}")
    eye_svg = EYES[eyes].format(a=accent[0], b=accent[1])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box()}" fill="none">
  <!-- kbots agent avatar — generated by scripts/avatar.py (eyes={eyes}) -->
  <defs>
    <linearGradient id="screen" x1="0" y1="0" x2="{FACE}" y2="{FACE}" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{SCREEN}"/>
      <stop offset="1" stop-color="{SCREEN_EDGE}"/>
    </linearGradient>
  </defs>
  <rect x="{BADGE_INSET}" y="{BADGE_INSET}" width="{BADGE_SIZE}" height="{BADGE_SIZE}" rx="{BADGE_RADIUS}" \
fill="url(#screen)" stroke="{FRAME}" stroke-width="{BADGE_STROKE}"/>
{eye_svg}
</svg>
"""


def render_png(svg_path: Path, png_path: Path, size: int) -> bool:
    """Render the SVG to a square PNG via Playwright Chromium. Returns success."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    html = (
        f'<body style="margin:0"><img src="{svg_path.name}" '
        f'width="{size}" height="{size}"></body>'
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", dir=svg_path.parent, delete=False
    ) as f:
        f.write(html)
        html_path = Path(f.name)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": size, "height": size})
            page.goto(f"file://{html_path}")
            page.screenshot(path=str(png_path), omit_background=True)
            browser.close()
        return True
    except Exception as e:  # noqa: BLE001 — report and fall back to SVG-only
        print(f"PNG render failed ({e}) — SVG written, convert manually if needed", file=sys.stderr)
        return False
    finally:
        html_path.unlink(missing_ok=True)


def upload_discord_avatar(png_path: Path, token: str) -> tuple[bool, str]:
    """Set a bot's Discord avatar via PATCH /users/@me. Returns (ok, message).

    Note: Discord rate-limits avatar changes hard (a couple per half hour).
    """
    b64 = base64.b64encode(png_path.read_bytes()).decode()
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        data=json.dumps({"avatar": f"data:image/png;base64,{b64}"}).encode(),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "kbots (https://github.com/karvidsson/kbots, 1.0)",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True, "Discord avatar set"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        if e.code == 429:
            return False, f"rate limited — Discord allows only a couple of avatar changes per half hour ({body})"
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:  # noqa: BLE001 — network errors surface as a message
        return False, str(e)


def resolve_bot_token(account: str) -> str:
    """Resolve a bot token from the vault (key-file unlock, non-interactive).

    Reads the account's token_key from config.yaml (falls back to
    discord-<account>, then the wizard's default discord-token).
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    import yaml

    from src.core.base import resolve_config_file, resolve_vault_key_file
    from src.vault.fernet import FernetVault

    token_key = f"discord-{account}"
    cfg_file = resolve_config_file("config.yaml")
    if cfg_file.exists():
        with open(cfg_file) as f:
            cfg = yaml.safe_load(f) or {}
        acct = (((cfg.get("connectors") or {}).get("discord") or {}).get("accounts") or {}).get(account) or {}
        token_key = acct.get("token_key", token_key)

    vault_file = resolve_config_file("secrets.enc")
    key_file = resolve_vault_key_file()
    if not vault_file.exists():
        raise SystemExit("vault (config/secrets.enc) not found — run from the install/overlay, or use settings.py")
    if not key_file.exists():
        raise SystemExit("vault key file not found — use scripts/settings.py (it can prompt for the passphrase)")
    vault = FernetVault(str(vault_file))
    vault.unlock(key_file.read_text().strip())
    token = vault.get(token_key) or (vault.get("discord-token") if token_key != "discord-token" else None)
    if not token:
        raise SystemExit(f"no vault secret '{token_key}' — is '{account}' a configured bot account?")
    return token


