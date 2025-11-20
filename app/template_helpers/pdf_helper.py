"""
Lightweight PDF utilities that mirror the PDF.js ingestion features on the server side.

Uses PyPDF to extract text per page, detect placeholder-style anchors, and emit JSON
reports that can feed Code Interpreter or other tooling.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Sequence

from pypdf import PdfReader

PLACEHOLDER_PATTERNS: Sequence[re.Pattern[str]] = (
    re.compile(r"\[\[.+?\]\]"),
    re.compile(r"\bInsert\b", re.IGNORECASE),
)


@dataclass
class PageText:
    page: int
    text: str


@dataclass
class PdfPlaceholder:
    page: int
    snippet: str


def extract_pdf_text(path: Path | str) -> List[PageText]:
    """Return full text per page (preserves ordering)."""
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    reader = PdfReader(str(pdf_path))
    pages: List[PageText] = []
    for idx, page in enumerate(reader.pages):
        content = page.extract_text() or ""
        pages.append(PageText(page=idx + 1, text=content))
    return pages


def _yield_placeholders(pages: Iterable[PageText]) -> Iterable[PdfPlaceholder]:
    for page in pages:
        for line in (page.text or "").splitlines():
            for pattern in PLACEHOLDER_PATTERNS:
                if pattern.search(line):
                    snippet = line.strip()
                    if snippet:
                        yield PdfPlaceholder(page=page.page, snippet=snippet)
                        break


def scan_pdf_placeholders(path: Path | str) -> List[PdfPlaceholder]:
    return list(_yield_placeholders(extract_pdf_text(path)))


def export_pdf_report(path: Path | str, output_json: Path | str | None = None) -> str:
    placeholders = scan_pdf_placeholders(path)
    payload = {
        "template": str(path),
        "placeholder_count": len(placeholders),
        "placeholders": [asdict(p) for p in placeholders],
    }
    blob = json.dumps(payload, indent=2)
    if output_json:
        Path(output_json).write_text(blob, encoding="utf-8")
    return blob


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract PDF text and placeholder hints.")
    parser.add_argument("template", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON output path.")
    args = parser.parse_args(argv)
    print(export_pdf_report(args.template, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

