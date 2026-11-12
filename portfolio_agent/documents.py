"""Reads statements into plain text. Statement content is DATA, never instructions."""

from pathlib import Path

MAX_CHARS = 20_000  # keep prompts a manageable size
TRUNCATED = "\n[... truncated ...]"


class LockedPdf(Exception):
    """The PDF needs a password to open."""


def pdf_pages(path: str | Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise LockedPdf("This PDF is password-protected.")
    return [p.extract_text() or "" for p in reader.pages]


def read_text(path: str | Path, max_chars: int = MAX_CHARS) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pdf_pages(path)))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATED
    return text
