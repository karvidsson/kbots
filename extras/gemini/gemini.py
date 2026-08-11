"""Gemini API tools — video/image analysis and image generation via Google Gemini.

API key from vault: secrets/gemini-api-key
"""

import base64
import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_IMAGE_GEN_MODEL = "gemini-3.1-flash-image-preview"


async def _gemini_api(ctx: ToolContext, model: str, contents: list, generation_config: dict | None = None) -> dict:
    """Make an authenticated Gemini API call."""
    api_key = ctx.vault.get("secrets/gemini-api-key") if ctx.vault else None
    if not api_key:
        return {"error": "secrets/gemini-api-key not configured in vault"}

    url = f"{GEMINI_API}/models/{model}:generateContent?key={api_key}"
    payload = {"contents": contents}
    if generation_config:
        payload["generationConfig"] = generation_config
    body = json.dumps(payload).encode()

    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.readable() else ""
        return {"error": f"Gemini API {e.code}: {error_body[:500]}"}
    except URLError as e:
        return {"error": f"Gemini API request failed: {e}"}


@tool(name="gemini_analyze_video", category="media",
      description="Analyze a video file using Gemini's vision model. Accepts file paths or URLs.")
async def gemini_analyze_video(ctx: ToolContext, video_path: str, prompt: str,
                                model: str = DEFAULT_MODEL) -> str:
    """Send a video to Gemini for analysis. Video must be under 20MB.

    Args:
        video_path: Path to video file OR URL (Discord CDN, http/https)
        prompt: Analysis prompt (e.g. "Analyze this video ad for hook strength, product visibility...")
        model: Gemini model to use (default: gemini-2.5-pro)
    """
    path, err = await _resolve_file(video_path, vault=ctx.vault)
    if err:
        return f"Error: {err}"

    try:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 20:
            return f"Video too large ({size_mb:.1f}MB). Gemini inline limit is ~20MB. Use frame extraction instead."

        # Detect mime type
        suffix = path.suffix.lower()
        mime_map = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".avi": "video/x-msvideo"}
        mime_type = mime_map.get(suffix, "video/mp4")

        video_b64 = base64.b64encode(path.read_bytes()).decode()

        contents = [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": video_b64}},
            ]
        }]

        result = await _gemini_api(ctx, model, contents)

        if "error" in result:
            return f"Gemini error: {result['error']}"

        # Extract text from response
        try:
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return "\n".join(p.get("text", "") for p in parts if "text" in p)
            return "Gemini returned no candidates."
        except (KeyError, IndexError) as e:
            return f"Failed to parse Gemini response: {e}"
    finally:
        cleanup_temp_files()


def _extract_drive_file_id(url: str) -> str | None:
    """Extract Google Drive file ID from various URL formats."""
    import re
    # https://drive.google.com/file/d/FILE_ID/view
    # https://drive.google.com/open?id=FILE_ID
    # https://docs.google.com/document/d/FILE_ID/edit
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


_tmp_files: list[Path] = []  # Track temp files for cleanup


def cleanup_temp_files():
    """Remove temp files created by _resolve_file(). Call after processing."""
    for p in _tmp_files:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    _tmp_files.clear()


async def _resolve_file(file_path: str, vault=None) -> tuple[Path, str | None]:
    """Resolve a file path or URL to a local file. Downloads URLs to temp dir.

    Supports: direct URLs, Discord CDN, Google Drive share links.
    For Drive links, uses OAuth2 token from vault for authenticated download.
    Non-Drive URLs are validated against SSRF blocklist before fetching.
    """
    if file_path.startswith("http://") or file_path.startswith("https://"):
        import aiohttp

        url = file_path
        headers = {}
        is_drive = "drive.google.com" in url or "docs.google.com" in url

        # Google Drive URL — convert to API download URL with auth
        if is_drive:
            file_id = _extract_drive_file_id(url)
            if not file_id:
                return Path(""), f"Could not extract file ID from Drive URL: {url}"
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            if vault:
                try:
                    from src.auth.oauth2 import GoogleAuth
                    auth = GoogleAuth(vault)
                    headers = await auth.get_headers()
                except Exception as e:
                    return Path(""), f"Google auth failed for Drive download: {e}"
        else:
            # SSRF validation for non-Drive URLs
            from src.tools.ingest import _validate_url
            err, _resolved_ip = _validate_url(url)
            if err:
                return Path(""), err

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return Path(""), f"Failed to download: HTTP {resp.status}"
                ct = resp.content_type or ""
                ext = ".bin"
                ext_map = {
                    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp",
                    "gif": ".gif", "mp4": ".mp4", "webm": ".webm", "mp3": ".mp3",
                    "mpeg": ".mp3", "ogg": ".ogg", "wav": ".wav", "pdf": ".pdf",
                    "quicktime": ".mov",
                }
                for key, val in ext_map.items():
                    if key in ct:
                        ext = val
                        break
                else:
                    from urllib.parse import urlparse
                    url_path = urlparse(file_path).path
                    if "." in url_path:
                        ext = "." + url_path.rsplit(".", 1)[-1].split("?")[0][:5]
                import tempfile
                fd, tmp = tempfile.mkstemp(prefix="kbots-download-", suffix=ext)
                os.close(fd)
                tmp_path = Path(tmp)
                tmp_path.write_bytes(await resp.read())
                _tmp_files.append(tmp_path)
                return tmp_path, None
    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(file_path)
    if path_err:
        return Path(""), path_err
    path = Path(file_path)
    if not path.exists():
        return Path(""), f"File not found: {file_path}"
    return path, None


@tool(name="gemini_analyze_image", category="media",
      description=("Analyze an image using Gemini's vision model. "
                   "Accepts file paths or URLs (e.g. Discord attachment CDN links)."))
async def gemini_analyze_image(ctx: ToolContext, image_path: str, prompt: str,
                                model: str = DEFAULT_MODEL) -> str:
    """Send an image to Gemini for analysis.

    Args:
        image_path: Path to image file OR URL (Discord CDN, http/https)
        prompt: Analysis prompt
        model: Gemini model to use (default: gemini-2.5-pro)
    """
    path, err = await _resolve_file(image_path, vault=ctx.vault)
    if err:
        return f"Error: {err}"

    try:
        suffix = path.suffix.lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".gif": "image/gif"}
        mime_type = mime_map.get(suffix, "image/jpeg")

        image_b64 = base64.b64encode(path.read_bytes()).decode()

        contents = [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_b64}},
            ]
        }]

        result = await _gemini_api(ctx, model, contents)

        if "error" in result:
            return f"Gemini error: {result['error']}"

        try:
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return "\n".join(p.get("text", "") for p in parts if "text" in p)
            return "Gemini returned no candidates."
        except (KeyError, IndexError) as e:
            return f"Failed to parse Gemini response: {e}"
    finally:
        cleanup_temp_files()


@tool(name="gemini_generate_image", description="Generate an image from a text prompt using Gemini", category="media")
async def gemini_generate_image(ctx: ToolContext, prompt: str, output_path: str = "",
                                 model: str = DEFAULT_IMAGE_GEN_MODEL) -> str:
    """Generate an image using Gemini's image generation model.

    Args:
        prompt: Description of the image to generate
        output_path: Where to save the image (default: data/generated/<timestamp>.png)
        model: Gemini model to use (default: gemini-3.1-flash-image-preview)
    """
    contents = [{"parts": [{"text": prompt}]}]
    generation_config = {"responseModalities": ["TEXT", "IMAGE"]}

    result = await _gemini_api(ctx, model, contents, generation_config=generation_config)

    if "error" in result:
        return f"Gemini error: {result['error']}"

    try:
        candidates = result.get("candidates", [])
        if not candidates:
            return "Gemini returned no candidates."

        parts = candidates[0].get("content", {}).get("parts", [])
        image_data = None
        text_response = []

        for part in parts:
            if "inlineData" in part:
                image_data = part["inlineData"]
            elif "inline_data" in part:
                image_data = part["inline_data"]
            elif "text" in part:
                text_response.append(part["text"])

        if not image_data:
            if text_response:
                return "No image generated. Model response: " + "\n".join(text_response)
            return "No image data in Gemini response."

        # Decode and save
        img_bytes = base64.b64decode(image_data["data"])
        if not output_path:
            out_dir = KBOTS_TMP / "media"
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(out_dir / f"{int(time.time())}.png")

        from src.tools.ingest import validate_file_path
        path_err = validate_file_path(output_path)
        if path_err:
            return path_err

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(img_bytes)

        msg = f"Image generated and saved to: {output_path}\n"
        msg += "To send this image to Discord, use: send_message with the text and attach this file path."
        if text_response:
            msg += "\n" + "\n".join(text_response)
        return msg

    except (KeyError, IndexError) as e:
        return f"Failed to parse Gemini response: {e}"
