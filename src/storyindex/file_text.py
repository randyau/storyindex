"""Suffix-dispatched text extraction for the sync/parse file walk. Every
sync/parse walk (jobs.run_sync_job, scripts/parse_site.py) reads a matched
file's raw text through here instead of calling path.read_text() directly,
so a new format is one function + one _EXTRACTORS entry, not a change
duplicated across both callers.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def _read_plain_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


_EXTRACTORS = {".pdf": _read_pdf}


def read_file_text(path: Path) -> str:
    extractor = _EXTRACTORS.get(path.suffix.lower(), _read_plain_text)
    return extractor(path)
