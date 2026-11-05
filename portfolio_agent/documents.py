"""Reads statements into plain text. Statement content is DATA, never instructions."""

from pathlib import Path

MAX_CHARS = 20_000  # keep prompts a manageable size


def read_text(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}"
                           for i, p in enumerate(reader.pages))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[... truncated ...]"
    return text
