"""llmfit_scan tool — report formatting over llmfit's recommend JSON."""

from src.tools.llmfit import format_report

SAMPLE = {
    "system": {
        "cpu_name": "Apple M1 Pro",
        "cpu_cores": 10,
        "total_ram_gb": 16.0,
        "has_gpu": True,
        "gpu_name": "Apple M1 Pro",
        "gpu_vram_gb": 16.0,
        "backend": "Metal",
        "unified_memory": True,
    },
    "models": [
        {
            "name": "DeepSeek-R1-Distill-Qwen-14B",
            "params_b": 14.0,
            "fit_level": "Perfect",
            "estimated_tps": 14.9,
            "best_quant": "mlx-4bit",
            "disk_size_gb": 8.12,
            "effective_context_length": 8192,
            "runtime": "mlx",
            "use_case": "Complex reasoning",
            "installed": False,
        },
        {
            "name": "Qwen2.5-Coder-3B",
            "params_b": 3.0,
            "fit_level": "Good",
            "estimated_tps": 40.0,
            "best_quant": "q4_k_m",
            "disk_size_gb": 2.0,
            "effective_context_length": 32768,
            "runtime": "llamacpp",
            "use_case": "Code generation",
            "installed": True,
        },
    ],
}


def test_report_includes_hardware_and_models():
    out = format_report(SAMPLE)
    assert "Apple M1 Pro (10 cores), 16 GB RAM" in out
    assert "16 GB VRAM (Metal, unified memory)" in out
    assert "**DeepSeek-R1-Distill-Qwen-14B** (14B)" in out
    assert "fit: Perfect" in out
    assert "~14.9 tok/s" in out
    assert "8.12 GB disk" in out
    assert "8k ctx" in out


def test_report_flags_installed_models():
    out = format_report(SAMPLE)
    lines = [line for line in out.splitlines() if "Qwen2.5-Coder-3B" in line]
    assert lines and "**installed**" in lines[0]
    assert "installed" not in next(
        line for line in out.splitlines() if "DeepSeek" in line
    )


def test_report_no_gpu_and_no_models():
    out = format_report({"system": {"cpu_name": "i5", "has_gpu": False}, "models": []})
    assert "CPU inference only" in out
    assert "No models fit" in out


def test_report_tolerates_missing_fields():
    out = format_report({"system": {}, "models": [{"name": "tiny"}]})
    assert "**tiny**" in out
    assert "fit: ?" in out
