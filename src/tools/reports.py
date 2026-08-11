"""Report tools — inspect data sheets, render charts, write Markdown/PDF reports.

Typical flow: inspect_data to see the columns → create_chart for each figure
(returns a PNG path) → write_report with markdown that embeds the charts via
![caption](path). Output lands in the agent's workspace under reports/.

Requires the 'reports' extra: uv sync --extra reports
(pandas + matplotlib for charts, fpdf2 + markdown for PDF output)
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.tools.ingest import validate_file_path

logger = logging.getLogger(__name__)

_INSTALL_HINT = "Run: uv sync --extra reports (then restart the service)"

CHART_TYPES = ["line", "bar", "barh", "scatter", "area", "pie"]
MAX_PREVIEW_ROWS = 5

# A Unicode-capable TTF is needed for PDF text beyond latin-1 (–, ”, emoji…).
# (regular, bold) pairs checked in order; when the bold variant is missing the
# regular file doubles for it. If none exists, text is transliterated to latin-1.
_UNICODE_FONTS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),       # Debian/Ubuntu
    ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),     # Fedora/RHEL
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", ""),   # macOS
]


def _load_dataframe(data_file: str):
    """Load a data sheet (CSV/TSV/XLSX) into a DataFrame. Returns df or error string."""
    err = validate_file_path(data_file)
    if err:
        return err
    path = Path(data_file)
    if not path.is_file():
        return f"File not found: {data_file}"

    try:
        import pandas as pd
    except ImportError:
        return f"pandas not installed. {_INSTALL_HINT}"

    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            return pd.read_csv(path)
        if suffix == ".tsv":
            return pd.read_csv(path, sep="\t")
        if suffix in (".xlsx", ".xls"):
            return pd.read_excel(path)
        if suffix == ".json":
            return pd.read_json(path)
        return f"Unsupported data format '{suffix}'. Supported: .csv, .tsv, .xlsx, .json"
    except Exception as e:
        return f"Failed to parse {path.name}: {e}"


def _output_path(ctx: ToolContext, output_file: str, default_name: str) -> Path | str:
    """Resolve where to write an output file. Returns Path or error string.

    Default: <agent project dir>/reports/<default_name>, falling back to the
    kbots tmp dir when the tool runs without a project context.
    """
    if output_file:
        err = validate_file_path(output_file)
        if err:
            return err
        path = Path(output_file).resolve()
    else:
        base = Path(ctx.project_dir) if ctx.project_dir else KBOTS_TMP
        path = (base / "reports" / default_name).resolve()
        err = validate_file_path(str(path))
        if err:
            return err
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"Cannot create output directory {path.parent}: {e}"
    return path


def _timestamp() -> str:
    # Microseconds included — successive charts in one report must not collide
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


@tool(name="inspect_data", description="Inspect a data sheet: columns, types, and sample rows", category="reports")
async def inspect_data(ctx: ToolContext, data_file: str) -> str:
    """Inspect a data sheet (CSV, TSV, XLSX, or JSON) before charting it.

    Shows row count, column names with types, and the first rows — use this
    to find the right column names for create_chart.
    """
    df = _load_dataframe(data_file)
    if isinstance(df, str):
        return df

    lines = [
        f"**{Path(data_file).name}** — {len(df)} rows × {len(df.columns)} columns",
        "",
        "| Column | Type | Non-null | Example |",
        "|--------|------|----------|---------|",
    ]
    for col in df.columns:
        example = next((str(v) for v in df[col].dropna().head(1)), "—")
        if len(example) > 40:
            example = example[:37] + "..."
        lines.append(f"| {col} | {df[col].dtype} | {df[col].notna().sum()} | {example} |")

    lines += ["", f"First {min(MAX_PREVIEW_ROWS, len(df))} rows:", "```",
              df.head(MAX_PREVIEW_ROWS).to_string(index=False), "```"]
    return "\n".join(lines)


@tool(name="create_chart", description="Render a chart from a data sheet to a PNG image", category="reports")
async def create_chart(ctx: ToolContext, data_file: str, chart_type: str, x_column: str,
                       y_columns: str, title: str = "", output_file: str = "") -> str:
    """Render a chart from a data sheet (CSV/TSV/XLSX/JSON) and save it as PNG.

    chart_type: line, bar, barh, scatter, area, or pie.
    x_column: column for the x-axis (or labels, for pie).
    y_columns: comma-separated value column(s); pie uses only the first.
    Returns the PNG path — embed it in a report with ![title](path).
    """
    if chart_type not in CHART_TYPES:
        return f"Invalid chart_type '{chart_type}'. Valid: {', '.join(CHART_TYPES)}"

    df = _load_dataframe(data_file)
    if isinstance(df, str):
        return df

    y_cols = [c.strip() for c in y_columns.split(",") if c.strip()]
    missing = [c for c in [x_column, *y_cols] if c not in df.columns]
    if missing:
        return f"Column(s) not found: {', '.join(missing)}. Available: {', '.join(map(str, df.columns))}"
    if not y_cols:
        return "Provide at least one y column."

    try:
        import matplotlib
        matplotlib.use("Agg")  # headless — no display server in the service
        import matplotlib.pyplot as plt
    except ImportError:
        return f"matplotlib not installed. {_INSTALL_HINT}"

    out = _output_path(ctx, output_file, f"chart-{chart_type}-{_timestamp()}.png")
    if isinstance(out, str):
        return out
    if out.suffix.lower() != ".png":
        out = out.with_suffix(".png")

    fig, ax = plt.subplots(figsize=(9, 5))
    try:
        if chart_type == "pie":
            data = df.groupby(x_column, sort=False)[y_cols[0]].sum()
            ax.pie(data.values, labels=[str(v) for v in data.index], autopct="%1.1f%%")
        elif chart_type == "scatter":
            for col in y_cols:
                ax.scatter(df[x_column], df[col], label=str(col), alpha=0.7)
        elif chart_type in ("bar", "barh"):
            plot_df = df.set_index(x_column)[y_cols]
            plot_df.plot(kind=chart_type, ax=ax)
        else:  # line, area
            plot_df = df.set_index(x_column)[y_cols]
            plot_df.plot(kind=chart_type, ax=ax, stacked=(chart_type == "area"))

        if chart_type != "pie":
            ax.set_xlabel(str(x_column))
            if len(y_cols) > 1 or chart_type == "scatter":
                ax.legend()
            ax.grid(True, alpha=0.3)
        if title:
            ax.set_title(title)
        fig.tight_layout()
        fig.savefig(out, dpi=120)
    except Exception as e:
        return f"Chart rendering failed: {e}"
    finally:
        plt.close(fig)

    return f"Chart saved: {out}\nEmbed it in a report with: ![{title or chart_type}]({out})"


def _markdown_to_pdf(title: str, content: str, out: Path) -> str | None:
    """Render markdown content to a PDF file. Returns error string or None."""
    try:
        import markdown as md_lib
        from fpdf import FPDF
    except ImportError:
        return f"fpdf2/markdown not installed. {_INSTALL_HINT}"

    # Validate embedded images: they get read into the PDF, so they must live
    # inside the allowed roots like every other file this module touches
    for img in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content):
        if img.startswith(("http://", "https://")):
            continue
        err = validate_file_path(img)
        if err:
            return f"Embedded image rejected: {err}"
        if not Path(img).is_file():
            return f"Embedded image not found: {img}"

    html = md_lib.markdown(content, extensions=["tables", "fenced_code"])
    # fpdf2 renders <img> at natural pixel size, which overflows A4 for typical
    # chart PNGs — pin images to the content width. fpdf2 maps the width
    # attribute at 72dpi: 174mm content width ≈ 490px.
    html = re.sub(r"<img (?![^>]*width)", '<img width="490" ', html)

    pdf = FPDF()
    pdf.set_margins(18, 18)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()

    font_family = None
    for regular, bold in _UNICODE_FONTS:
        if Path(regular).is_file():
            try:
                bold_file = bold if bold and Path(bold).is_file() else regular
                pdf.add_font("unicode", fname=regular)
                # write_html uses B/I/BI styles for headings, <strong>, <em> —
                # all four must be registered or rendering raises
                for style in ("B", "I", "BI"):
                    pdf.add_font("unicode", style=style,
                                 fname=bold_file if "B" in style else regular)
                font_family = "unicode"
            except Exception as e:
                logger.info(f"write_report: could not load font {regular}: {e}")
            break
    if font_family is None:
        # Core PDF fonts are latin-1 only — transliterate the rest
        html = html.encode("latin-1", errors="replace").decode("latin-1")

    if title:
        pdf.set_font(font_family or "helvetica", size=20)
        pdf.multi_cell(0, 10, title)
        pdf.ln(4)

    try:
        if font_family:
            pdf.write_html(html, font_family=font_family)
        else:
            pdf.write_html(html)
        pdf.output(str(out))
    except Exception as e:
        return f"PDF rendering failed: {e}"
    return None


@tool(name="write_report", description="Write a report as a Markdown or PDF file", category="reports")
async def write_report(ctx: ToolContext, title: str, content: str,
                       format: str = "md", output_file: str = "") -> str:
    """Write a report document from markdown content.

    format: 'md' writes the markdown as-is; 'pdf' renders it (headings, lists,
    tables, and embedded ![caption](path) images are supported).
    Embed charts by first calling create_chart and using the returned path.
    Default location: <agent workspace>/reports/<slug>-<timestamp>.<ext>
    """
    if format not in ("md", "pdf"):
        return f"Invalid format '{format}'. Valid: md, pdf"
    if not content.strip():
        return "Report content is empty."

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "report"
    out = _output_path(ctx, output_file, f"{slug}-{_timestamp()}.{format}")
    if isinstance(out, str):
        return out

    if format == "md":
        text = content if content.lstrip().startswith("#") else f"# {title}\n\n{content}"
        try:
            out.write_text(text, encoding="utf-8")
        except OSError as e:
            return f"Failed to write report: {e}"
    else:
        err = _markdown_to_pdf(title, content, out)
        if err:
            return err

    size_kb = out.stat().st_size / 1024
    return f"Report written: {out} ({size_kb:.1f} KB)"


@tool(name="compile_report",
      description=("Compile a full report from a YAML spec file — loads the data, "
                   "renders every chart, assembles the document, and writes MD/PDF "
                   "in ONE deterministic step. Author the spec once (interactively), "
                   "then schedule this tool to produce the report forever."),
      category="reports")
async def compile_report(ctx: ToolContext, spec_file: str) -> str:
    """Run a report spec end-to-end. Spec YAML:

    title: Weekly Sales
    data_file: ./data/sales.csv
    charts:
      - {type: line, x: date, y: revenue, title: Revenue}
      - {type: bar, x: region, y: units, title: Units by region}
    format: pdf          # or md
    intro: optional markdown paragraph above the charts

    The pipeline is deterministic — no model decisions — which is what makes it
    reliable on a schedule (optionally with a local-model narration, see
    tool-direct actions in schedule_task).
    """
    import yaml as _yaml

    err = validate_file_path(spec_file)
    if err:
        return err
    spec_path = Path(spec_file)
    if not spec_path.is_file():
        return f"Spec not found: {spec_file}"
    try:
        spec = _yaml.safe_load(spec_path.read_text()) or {}
    except _yaml.YAMLError as e:
        return f"Invalid spec YAML: {e}"

    title = str(spec.get("title") or spec_path.stem)
    data_file = spec.get("data_file", "")
    if not data_file:
        return "Spec is missing data_file."
    if not Path(data_file).is_absolute():
        data_file = str((spec_path.parent / data_file).resolve())
    df = _load_dataframe(data_file)
    if isinstance(df, str):
        return df

    # Render every chart via the existing tool (same validation + output paths)
    chart_lines = []
    for i, ch in enumerate(spec.get("charts") or []):
        result = await create_chart(
            ctx, data_file, str(ch.get("type", "line")), str(ch.get("x", "")),
            str(ch.get("y", "")), title=str(ch.get("title", "")))
        if not result.startswith("Chart saved: "):
            return f"Chart {i + 1} failed: {result}"
        png = result.split("Chart saved: ", 1)[1].splitlines()[0].strip()
        chart_lines.append(f"![{ch.get('title', f'chart {i + 1}')}]({png})")

    # Assemble the document
    parts = [f"# {title}", "",
             f"*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC "
             f"from `{Path(data_file).name}` ({len(df)} rows)*", ""]
    if spec.get("intro"):
        parts += [str(spec["intro"]), ""]
    for line in chart_lines:
        parts += [line, ""]

    fmt = str(spec.get("format", "pdf")).lower()
    result = await write_report(ctx, title, "\n".join(parts),
                                format="pdf" if fmt == "pdf" else "md")
    if not result.startswith("Report written: "):
        return result
    out_path = result.split("Report written: ", 1)[1].rsplit(" (", 1)[0]
    return (f"Report compiled: {out_path} — {len(df)} rows, "
            f"{len(chart_lines)} chart(s). Spec: {spec_path.name}")
