"""llmfit — report which local LLM models this machine can run.

Wraps the llmfit CLI (https://github.com/AlexsJones/llmfit, MIT): it detects
RAM/CPU/GPU/VRAM and scores its model catalog on memory fit, estimated speed,
quality and context. Run via uvx so it's fetched on first use — no dependency
in pyproject.
"""

import asyncio
import json
import shutil
from typing import Annotated, Any

from src.core.base import ToolContext
from src.core.tools import tool

USE_CASES = ["general", "coding", "reasoning", "chat", "multimodal", "embedding"]

# First uvx run downloads the ~6 MB binary; scans themselves are seconds.
_TIMEOUT_S = 120


def format_report(data: dict[str, Any]) -> str:
    """Render llmfit's `recommend --json` output as compact markdown."""
    sys_info = data.get("system", {})
    models = data.get("models", [])

    lines = ["**Hardware**"]
    cpu = sys_info.get("cpu_name", "unknown CPU")
    cores = sys_info.get("cpu_cores")
    ram = sys_info.get("total_ram_gb")
    hw = f"- {cpu}" + (f" ({cores} cores)" if cores else "")
    if ram:
        hw += f", {ram:g} GB RAM"
    lines.append(hw)
    if sys_info.get("has_gpu"):
        gpu = sys_info.get("gpu_name", "GPU")
        vram = sys_info.get("gpu_vram_gb")
        backend = sys_info.get("backend", "")
        gpu_line = f"- GPU: {gpu}"
        if vram:
            gpu_line += f", {vram:g} GB VRAM"
        if backend:
            gpu_line += f" ({backend}"
            gpu_line += ", unified memory)" if sys_info.get("unified_memory") else ")"
        lines.append(gpu_line)
    else:
        lines.append("- No GPU detected — CPU inference only")

    if not models:
        lines.append("\nNo models fit this hardware.")
        return "\n".join(lines)

    lines.append("\n**Best-fitting local models** (ranked)")
    for i, m in enumerate(models, 1):
        name = m.get("name", "?")
        params = m.get("params_b")
        size = f" ({params:g}B)" if params else ""
        parts = [f"fit: {m.get('fit_level', '?')}"]
        if m.get("estimated_tps"):
            parts.append(f"~{m['estimated_tps']:g} tok/s")
        if m.get("best_quant"):
            parts.append(f"quant {m['best_quant']}")
        if m.get("disk_size_gb"):
            parts.append(f"{m['disk_size_gb']:g} GB disk")
        if m.get("effective_context_length"):
            parts.append(f"{m['effective_context_length'] // 1024}k ctx")
        if m.get("runtime"):
            parts.append(m["runtime"])
        installed = " · **installed**" if m.get("installed") else ""
        lines.append(f"{i}. **{name}**{size} — {', '.join(parts)}{installed}")
        if m.get("use_case"):
            lines.append(f"   {m['use_case']}")
    return "\n".join(lines)


@tool(
    name="llmfit_scan",
    description=(
        "Scan this machine's hardware (RAM, CPU, GPU/VRAM) and report which "
        "local LLM models it can run well, ranked by fit — with estimated "
        "speed, quantization and disk size. Optionally filter by use case."
    ),
    category="ops",
)
async def llmfit_scan(
    ctx: ToolContext,
    use_case: Annotated[str, {"choices": USE_CASES}] = "",
) -> str:
    llmfit_bin = shutil.which("llmfit")
    if llmfit_bin:
        cmd = [llmfit_bin, "recommend", "--json"]
    elif shutil.which("uvx"):
        cmd = ["uvx", "llmfit", "recommend", "--json"]
    else:
        return (
            "ERROR: neither llmfit nor uvx found on PATH. Install uv "
            "(https://docs.astral.sh/uv/) or `pip install llmfit`."
        )
    if use_case and use_case in USE_CASES:
        cmd += ["--use-case", use_case]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return f"ERROR: llmfit timed out ({_TIMEOUT_S}s)"
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "unknown error"
        return f"ERROR: llmfit failed (exit {proc.returncode}): {err[:500]}"
    try:
        data = json.loads(stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return f"ERROR: could not parse llmfit output: {e}"
    return format_report(data)
