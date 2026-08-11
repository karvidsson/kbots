"""send_email attachments — proper MIME, missing-file and size guards."""

import base64
import importlib.util
import sys
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "google_extra", REPO / "extras" / "google" / "google.py")
google_extra = importlib.util.module_from_spec(spec)
sys.modules["google_extra"] = google_extra
spec.loader.exec_module(google_extra)

from src.core.base import ToolContext  # noqa: E402


def _ctx():
    return ToolContext(agent_id="test", vault=MagicMock())


def _capture_api(monkeypatch):
    calls = []

    async def fake_api(ctx, url, method="GET", data=None):
        calls.append({"url": url, "method": method, "data": data})
        return {"id": "sent-1"}

    monkeypatch.setattr(google_extra, "_google_api", fake_api)
    return calls


async def test_sends_attachment_as_mime_part(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    f = tmp_path / "cover.png"
    f.write_bytes(b"\x89PNG fake image bytes")

    out = await google_extra.send_email(
        _ctx(), "curator@example.com", "Submission", "Here is the album.",
        attachments=str(f))

    assert "Email sent" in out and "1 attachment" in out
    raw = base64.urlsafe_b64decode(calls[-1]["data"]["raw"])
    parsed = message_from_bytes(raw)
    assert parsed["To"] == "curator@example.com"
    parts = list(parsed.walk())
    att = [p for p in parts if p.get_filename() == "cover.png"]
    assert att and att[0].get_content_type() == "image/png"
    assert att[0].get_payload(decode=True) == b"\x89PNG fake image bytes"
    body = [p for p in parts if p.get_content_type() == "text/plain"]
    assert body and "Here is the album." in body[0].get_payload()


async def test_no_attachments_still_sends_plain(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    out = await google_extra.send_email(
        _ctx(), "a@b.c", "Hi", "Plain body")
    assert "Email sent" in out and "attachment" not in out
    raw = base64.urlsafe_b64decode(calls[-1]["data"]["raw"])
    parsed = message_from_bytes(raw)
    assert not parsed.is_multipart()
    assert "Plain body" in parsed.get_payload()


async def test_missing_file_refuses_before_sending(monkeypatch):
    calls = _capture_api(monkeypatch)
    out = await google_extra.send_email(
        _ctx(), "a@b.c", "Hi", "Body", attachments="/nope/missing.png")
    assert out.startswith("Not sent")
    assert calls == []


async def test_oversize_refuses_before_sending(tmp_path, monkeypatch):
    calls = _capture_api(monkeypatch)
    big = tmp_path / "big.wav"
    big.write_bytes(b"x" * 1024)
    monkeypatch.setattr(google_extra, "_MAX_ATTACHMENT_BYTES", 100)
    out = await google_extra.send_email(
        _ctx(), "a@b.c", "Hi", "Body", attachments=str(big))
    assert out.startswith("Not sent") and "limit" in out
    assert calls == []


async def test_non_ascii_subject_is_rfc2047_encoded(monkeypatch):
    """Regression: pre-#94 hand-built headers sent raw UTF-8 in the Subject,
    which receivers render as mojibake (a live Nightride submission went out
    as 'Submission Ã¢Â€Â”...'). EmailMessage must RFC-2047-encode it."""
    from email import policy

    calls = _capture_api(monkeypatch)
    await google_extra.send_email(
        _ctx(), "a@b.c", "Submission — Pixel Fox", "body")
    raw = base64.urlsafe_b64decode(calls[-1]["data"]["raw"])
    # header section must be pure ASCII on the wire
    header_bytes = raw.split(b"\n\n", 1)[0]
    header_bytes.decode("ascii")
    from email import message_from_bytes as _from_bytes
    parsed = _from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == "Submission — Pixel Fox"
