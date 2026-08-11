"""Audio tools — transcription via Groq Whisper API."""

import logging
import os
from pathlib import Path

import aiohttp

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

GROQ_API = "https://api.groq.com/openai/v1/audio/transcriptions"


async def _resolve_audio(file_path: str) -> tuple[Path, str | None]:
    """Resolve a file path or URL to a local audio file."""
    if file_path.startswith("http://") or file_path.startswith("https://"):
        async with aiohttp.ClientSession() as session:
            async with session.get(file_path, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return Path(""), f"Failed to download: HTTP {resp.status}"
                ct = resp.content_type or ""
                ext = ".ogg"
                if "mp3" in ct or "mpeg" in ct:
                    ext = ".mp3"
                elif "wav" in ct:
                    ext = ".wav"
                elif "mp4" in ct:
                    ext = ".mp4"
                elif "webm" in ct:
                    ext = ".webm"
                elif "ogg" in ct:
                    ext = ".ogg"
                from urllib.parse import urlparse
                url_path = urlparse(file_path).path
                if "." in url_path:
                    url_ext = "." + url_path.rsplit(".", 1)[-1].split("?")[0][:5]
                    if url_ext in (".mp3", ".wav", ".ogg", ".mp4", ".webm", ".m4a", ".flac"):
                        ext = url_ext
                import tempfile
                fd, tmp = tempfile.mkstemp(prefix="kbots-audio-", suffix=ext)
                os.close(fd)
                tmp_path = Path(tmp)
                tmp_path.write_bytes(await resp.read())
                return tmp_path, None
    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(file_path)
    if path_err:
        return Path(""), path_err
    path = Path(file_path)
    if not path.exists():
        return Path(""), f"File not found: {file_path}"
    return path, None


@tool(
    name="transcribe_audio",
    description=("Transcribe audio to text using Groq Whisper. "
                 "Accepts file paths or URLs (e.g. Discord voice message CDN links)."),
    category="media",
)
async def transcribe_audio(
    ctx: ToolContext, audio_path: str, language: str = "en"
) -> str:
    """Transcribe an audio file to text.

    Args:
        audio_path: Path to audio file OR URL (Discord CDN, http/https).
                    Supports mp3, wav, ogg, m4a, webm, flac.
        language: Language code (default: en)
    """
    api_key = ctx.vault.get("secrets/groq-api-key") if ctx.vault else None
    if not api_key:
        return "Error: Groq API key not found in vault."

    path, err = await _resolve_audio(audio_path)
    if err:
        return f"Error: {err}"

    # Check file size (Groq limit: 25MB)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > 25:
        return f"Error: Audio file too large ({size_mb:.1f}MB). Groq limit is 25MB."

    headers = {"Authorization": f"Bearer {api_key}"}

    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field("file", path.read_bytes(), filename=path.name)
        data.add_field("model", "whisper-large-v3-turbo")
        data.add_field("language", language)
        data.add_field("response_format", "verbose_json")

        async with session.post(
            GROQ_API, headers=headers, data=data,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                return f"Groq transcription failed (HTTP {resp.status}): {error_text[:300]}"

            result = await resp.json()

    text = result.get("text", "")
    duration = result.get("duration", 0)
    segments = result.get("segments", [])

    output = f"**Transcription** ({duration:.1f}s):\n\n{text}"
    if segments and len(segments) > 1:
        output += "\n\n**Segments:**\n"
        for seg in segments[:20]:  # cap at 20 segments
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            seg_text = seg.get("text", "")
            output += f"[{start:.1f}s-{end:.1f}s] {seg_text}\n"

    return output
