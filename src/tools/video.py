"""Video tools — extract frames and clips using ffmpeg.

Requires ffmpeg installed on the system.
"""

import asyncio
import logging
import os
import shlex
import tempfile
from pathlib import Path

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.lib.clidep import cli_hint, resolve_cli

logger = logging.getLogger(__name__)


@tool(name="video_frames", description="Extract frames from a video file", category="media")
async def video_frames(ctx: ToolContext, video_path: str, output_dir: str = "",
                       interval: float = 1.0, max_frames: int = 10) -> str:
    """Extract frames from a video at regular intervals.

    Args:
        video_path: Path to video file
        output_dir: Where to save frames
        interval: Seconds between frames
        max_frames: Maximum number of frames to extract
    """
    video = Path(video_path)
    if not video.exists():
        return f"Video not found: {video_path}"

    if not output_dir:
        media_dir = KBOTS_TMP / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        output_dir = tempfile.mkdtemp(prefix="kbots-frames-", dir=str(media_dir))
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ff = resolve_cli("ffmpeg")
    if not ff:
        return cli_hint("ffmpeg")
    cmd = (
        f"{' '.join(ff)} -i {shlex.quote(video_path)} -vf fps=1/{interval} "
        f"-frames:v {max_frames} "
        f"{shlex.quote(str(out / 'frame_%04d.png'))} -y 2>&1"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

    if proc.returncode != 0:
        return f"ffmpeg failed: {stderr.decode()[:200]}"

    frames = list(out.glob("frame_*.png"))
    return f"Extracted {len(frames)} frames to {output_dir}"


@tool(name="video_clip", description="Extract a clip from a video", category="media")
async def video_clip(ctx: ToolContext, video_path: str, start: str, duration: str,
                     output_path: str = "") -> str:
    """Extract a clip from a video.

    Args:
        video_path: Source video
        start: Start time (e.g. "00:01:30" or "90")
        duration: Duration (e.g. "00:00:10" or "10")
        output_path: Output file path
    """
    video = Path(video_path)
    if not video.exists():
        return f"Video not found: {video_path}"

    if not output_path:
        media_dir = KBOTS_TMP / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        fd, output_path = tempfile.mkstemp(prefix="kbots-clip-", suffix=".mp4", dir=str(media_dir))
        os.close(fd)

    ff = resolve_cli("ffmpeg")
    if not ff:
        return cli_hint("ffmpeg")
    cmd = (f"{' '.join(ff)} -i {shlex.quote(video_path)} -ss {shlex.quote(start)} "
           f"-t {shlex.quote(duration)} -c copy {shlex.quote(output_path)} -y 2>&1")

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

    if proc.returncode != 0:
        return f"ffmpeg failed: {stderr.decode()[:200]}"

    out = Path(output_path)
    if out.exists():
        size_mb = out.stat().st_size / (1024 * 1024)
        return f"Clip saved to {output_path} ({size_mb:.1f}MB)"
    return "Clip extraction completed but output not found"
