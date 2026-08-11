"""Avatar generator: template composition, accent resolution, CLI output."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from avatar import ACCENTS, EYES, FRAME, SCREEN, build_svg, resolve_accent  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "avatar.py"


def test_every_eye_style_builds():
    for style in EYES:
        svg = build_svg(style, ACCENTS["red"])
        assert svg.startswith("<svg")
        assert f"eyes={style}" in svg
        assert SCREEN in svg and FRAME in svg


def test_accent_preset_lands_in_svg():
    svg = build_svg("capsule", ACCENTS["violet"])
    assert ACCENTS["violet"][0] in svg
    assert ACCENTS["violet"][1] in svg


def test_hex_accent_resolves_with_lighter_secondary():
    primary, secondary = resolve_accent("#4ade80")
    assert primary == "#4ade80"
    assert secondary != primary and secondary.startswith("#")


def test_short_hex_expands():
    primary, _ = resolve_accent("#f00")
    assert primary == "#ff0000"


def test_unknown_style_and_accent_rejected():
    import pytest

    with pytest.raises(SystemExit):
        build_svg("nope", ACCENTS["red"])
    with pytest.raises(SystemExit):
        resolve_accent("chartreuse-ish")


def test_cli_writes_svg(tmp_path):
    out = tmp_path / "sub" / "avatar"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--eyes", "ring", "--accent", "teal",
         "--out", str(out), "--no-png"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    svg = (tmp_path / "sub" / "avatar.svg").read_text()
    assert "eyes=ring" in svg and ACCENTS["teal"][0] in svg


def test_cli_list():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--list"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "capsule" in result.stdout and "red" in result.stdout


def test_upload_success(monkeypatch, tmp_path):
    import contextlib
    import urllib.request

    from avatar import upload_discord_avatar

    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG fake")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.get_header("Authorization")
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode()
        return contextlib.nullcontext()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    success, msg = upload_discord_avatar(png, "tok123")
    assert success and "set" in msg
    assert captured["auth"] == "Bot tok123"
    assert captured["method"] == "PATCH"
    assert "data:image/png;base64," in captured["body"]


def test_upload_rate_limited(monkeypatch, tmp_path):
    import io
    import urllib.error
    import urllib.request

    from avatar import upload_discord_avatar

    png = tmp_path / "a.png"
    png.write_bytes(b"x")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b'{"retry_after": 1800}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    success, msg = upload_discord_avatar(png, "tok")
    assert not success and "rate limited" in msg
