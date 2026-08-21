"""render_diagram prefers the vendored mermaid.min.js (offline) and falls back to the CDN."""

from src.tools import design


def test_mermaid_html_uses_vendored_script(tmp_path, monkeypatch):
    js = tmp_path / "mermaid.min.js"
    js.write_text("window.mermaid={render:()=>{}}; var s='</script>';")
    monkeypatch.setattr(design, "MERMAID_VENDOR", js)
    monkeypatch.setattr(design, "_mermaid_cache", {})
    html = design._mermaid_html("flowchart LR\n a-->b", {"primaryColor": "#000"})
    assert "window.mermaid" in html
    assert design.MERMAID_CDN not in html
    assert "import mermaid" not in html
    # a literal </script> inside the bundle must not close the loader tag early
    assert "var s='<\\/script>'" in html
    assert '"primaryColor": "#000"' in html


def test_mermaid_html_falls_back_to_cdn(tmp_path, monkeypatch):
    monkeypatch.setattr(design, "MERMAID_VENDOR", tmp_path / "missing.js")
    monkeypatch.setattr(design, "_mermaid_cache", {})
    html = design._mermaid_html("flowchart LR\n a-->b", {})
    assert f'import mermaid from "{design.MERMAID_CDN}"' in html
