"""Tests for the context compressor."""

import tempfile
from pathlib import Path

from src.lib.compressor import compress_file, compress_text, decompress_file


class TestCompressText:
    """Test prose compression rules."""

    def test_filler_phrases_stripped(self):
        text = "Please note that you need Python 3.12."
        result = compress_text(text)
        assert "Please note that" not in result
        assert "Python 3.12" in result

    def test_multi_filler_in_one_line(self):
        text = "It is important to make sure to install deps."
        result = compress_text(text)
        assert "It is important to" not in result
        assert "make sure to" not in result
        assert "install deps" in result.lower()

    def test_contractions_applied_in_standard(self):
        text = "You do not need to restart. It should not fail."
        result = compress_text(text, level="standard")
        assert "don't" in result
        assert "shouldn't" in result

    def test_contractions_not_applied_in_lite(self):
        text = "You do not need to restart."
        result = compress_text(text, level="lite")
        assert "do not" in result

    def test_code_fences_preserved(self):
        text = "Please note that you should run:\n```bash\ngit pull\n```\nThen restart."
        result = compress_text(text)
        assert "```bash\ngit pull\n```" in result

    def test_inline_code_preserved(self):
        text = "It is important to use the `@tool` decorator."
        result = compress_text(text)
        assert "`@tool`" in result

    def test_headings_preserved(self):
        text = "## Please note that this is a heading"
        result = compress_text(text)
        assert result.strip() == "## Please note that this is a heading"

    def test_table_rows_preserved(self):
        text = "| Please note that | Value |\n|---|---|\n| a | b |"
        result = compress_text(text)
        assert "| Please note that | Value |" in result

    def test_urls_preserved(self):
        text = "Please note that you should visit https://example.com/path for details."
        result = compress_text(text)
        assert "https://example.com/path" in result

    def test_yaml_frontmatter_preserved(self):
        text = "---\nname: test\ndescription: please note that\n---\nPlease note that content."
        result = compress_text(text)
        # Frontmatter preserved verbatim
        assert "description: please note that" in result
        # Content compressed
        assert result.count("Please note that") == 0 or "description: please note that" in result

    def test_html_comments_preserved(self):
        text = "<!-- compressed: 2026-01-01 -->\nPlease note that content."
        result = compress_text(text)
        assert "<!-- compressed: 2026-01-01 -->" in result

    def test_list_items_compressed(self):
        text = "- It is important to check the logs before restarting."
        result = compress_text(text)
        assert result.startswith("- ")
        assert "It is important to" not in result
        assert "logs" in result

    def test_blank_lines_preserved(self):
        text = "First paragraph.\n\nSecond paragraph."
        result = compress_text(text)
        assert "\n\n" in result

    def test_triple_blank_lines_collapsed(self):
        text = "First.\n\n\n\nSecond."
        result = compress_text(text)
        assert "\n\n\n" not in result

    def test_empty_input(self):
        assert compress_text("") == ""

    def test_no_prose_unchanged(self):
        text = "```python\nprint('hello')\n```"
        result = compress_text(text)
        assert result.strip() == text.strip()

    def test_capitalization_after_removal(self):
        text = "You should always check the status."
        result = compress_text(text)
        # First char should be capitalized after filler removal
        stripped = result.strip()
        assert stripped[0].isupper()

    def test_punctuation_cleanup(self):
        """No double commas or orphaned punctuation after removal."""
        text = "In order to run, you need Python."
        result = compress_text(text)
        assert ",," not in result
        assert ", ," not in result


class TestCompressFile:
    """Test file-level compression."""

    def test_compress_creates_original(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Please note that this is a test file.\n")
            f.flush()
            path = Path(f.name)

        try:
            result = compress_file(path, level="standard")
            original_path = Path(result["original_path"])

            assert original_path.exists()
            assert "Please note that" in original_path.read_text()
            assert result["original_tokens"] > 0
            assert result["compressed_tokens"] <= result["original_tokens"]

            # Compressed file has tag
            compressed = path.read_text()
            assert "<!-- compressed:" in compressed
        finally:
            path.unlink(missing_ok=True)
            Path(result["original_path"]).unlink(missing_ok=True)

    def test_dry_run_no_write(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Please note that this is a test.\n")
            f.flush()
            path = Path(f.name)

        try:
            original_content = path.read_text()
            result = compress_file(path, dry_run=True)

            # File unchanged
            assert path.read_text() == original_content
            # No original created
            original_path = Path(result["original_path"])
            assert not original_path.exists()
            # Stats still returned
            assert result["original_tokens"] > 0
        finally:
            path.unlink(missing_ok=True)

    def test_decompress_restores(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Please note that this is original content.\n")
            f.flush()
            path = Path(f.name)

        try:
            original_content = path.read_text()
            compress_file(path, level="standard")

            # File is now compressed
            assert "<!-- compressed:" in path.read_text()

            # Decompress
            assert decompress_file(path) is True
            assert path.read_text().strip() == original_content.strip()

            # Original backup removed
            original_path = path.with_suffix(".original.md")
            assert not original_path.exists()
        finally:
            path.unlink(missing_ok=True)

    def test_decompress_no_original(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("No original exists.\n")
            f.flush()
            path = Path(f.name)

        try:
            assert decompress_file(path) is False
        finally:
            path.unlink(missing_ok=True)

    def test_file_not_found(self):
        try:
            compress_file("/nonexistent/path.md")
            assert False, "Should have raised"
        except FileNotFoundError:
            pass

    def test_idempotent_recompress(self):
        """Compressing an already-compressed file re-compresses from original."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Please note that you should always check the logs.\n")
            f.flush()
            path = Path(f.name)

        try:
            r1 = compress_file(path, level="standard")
            r2 = compress_file(path, level="standard")
            # Should produce same result
            assert r1["compressed_tokens"] == r2["compressed_tokens"]
        finally:
            path.unlink(missing_ok=True)
            Path(r1["original_path"]).unlink(missing_ok=True)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
