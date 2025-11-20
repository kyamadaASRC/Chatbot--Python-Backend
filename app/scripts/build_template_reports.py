"""
Scan every consultant template (DOCX/XLSX) and emit placeholder reports.

Usage:
    python -m app.scripts.build_template_reports
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.template_helpers import (
    export_docx_report,
    export_xlsx_report,
    export_pdf_report,
)

CONSULTANTS_ROOT = Path(__file__).resolve().parents[1] / "consultants"
REPORT_FOLDER = "placeholder_reports"


def _iter_templates(folder: Path):
    for path in folder.iterdir():
        if path.is_dir():
            continue
        if path.suffix.lower() in {".docx", ".xlsx", ".pdf"}:
            yield path


def build_reports() -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for consultant_dir in sorted(CONSULTANTS_ROOT.iterdir()):
        if not consultant_dir.is_dir():
            continue
        report_dir = consultant_dir / REPORT_FOLDER
        report_dir.mkdir(exist_ok=True)
        report_paths = []
        for template in _iter_templates(consultant_dir):
            output_path = report_dir / f"{template.stem}.json"
            suffix = template.suffix.lower()
            if suffix == ".docx":
                payload = export_docx_report(template, output_path)
            elif suffix == ".xlsx":
                payload = export_xlsx_report(template, output_path)
            else:
                payload = export_pdf_report(template, output_path)
            report_paths.append(str(output_path.relative_to(CONSULTANTS_ROOT.parent)))
            print(f"[template-report] {template.name} -> {output_path.name}")
            if os.environ.get("VERBOSE_REPORTS") == "1":
                print(payload)
        summary[consultant_dir.name] = report_paths
    return summary


if __name__ == "__main__":
    reports = build_reports()
    print(json.dumps(reports, indent=2))
