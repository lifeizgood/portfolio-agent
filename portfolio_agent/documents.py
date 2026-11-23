"""Reads statements into plain text. Statement content is DATA, never instructions."""

from pathlib import Path

MAX_CHARS = 20_000  # keep prompts a manageable size
TRUNCATED = "\n[... truncated ...]"
OCR_NOTE = "[text read by OCR from page images]\n"   # an image-only PDF (a scan)


class LockedPdf(Exception):
    """The PDF needs a password to open."""


def pdf_pages(path: str | Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise LockedPdf("This PDF is password-protected.")
    return [p.extract_text() or "" for p in reader.pages]


class OcrUnavailable(Exception):
    """The PDF has no text and it couldn't be read from its page images."""


def _has_text(pages: list[str]) -> bool:
    return sum(len(t.strip()) for t in pages) >= 20


def ocr_pdf(path: str | Path, max_pages: int = 50) -> str:
    """Read an image-only PDF with macOS's Vision text recognition (local only).
    Builds the small Swift helper on first use."""
    import subprocess
    from . import config
    src = Path(__file__).parent / "ocr" / "pdf_ocr.swift"
    binary = config.OCR_BIN
    if not binary.exists() or binary.stat().st_mtime < src.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)
        build = subprocess.run(["xcrun", "swiftc", "-O", "-o", str(binary), str(src)],
                               capture_output=True, text=True, timeout=300)
        if build.returncode != 0:
            raise OcrUnavailable("The text-recognition helper couldn't be built (Xcode "
                                 "command line tools needed): " + build.stderr[-300:])
    run = subprocess.run([str(binary), str(path), str(max_pages)], capture_output=True,
                         text=True, timeout=config.OCR_TIMEOUT_S)
    if run.returncode != 0:
        raise OcrUnavailable(run.stderr.strip()[:300] or "Text recognition failed.")
    return run.stdout


def read_text(path: str | Path, max_chars: int = MAX_CHARS) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        pages = pdf_pages(path)
        if _has_text(pages):
            text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pages))
        else:                 # pages are images: read them with OCR, on this Mac
            text = OCR_NOTE + ocr_pdf(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATED
    return text
