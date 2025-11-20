"""
CLI helper to run fuzzy anchor suggestions for any uploaded template.

Example:
    python -m app.scripts.suggest_template_anchors \\
        --template path/to/template.docx \\
        --targets anchor_targets.json \\
        --out proposed_map.json

The UI can surface `proposed_map.json` for human edits before Code Interpreter fills
the template. Low-confidence matches are flagged via `requires_review`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.template_helpers.fuzzy_mapper import propose_anchor_map


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Suggest fuzzy anchors for a template.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument(
        "--targets",
        type=Path,
        required=True,
        help="JSON array describing the desired anchor labels.",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=0.82)
    args = parser.parse_args(argv)

    targets = json.loads(args.targets.read_text())
    if not isinstance(targets, list):
        raise SystemExit("Targets JSON must be an array of strings.")

    proposal = propose_anchor_map(
        args.template,
        targets,
        top_k=args.top,
        confidence_threshold=args.threshold,
    )
    blob = json.dumps(proposal, indent=2)
    if args.out:
        args.out.write_text(blob, encoding="utf-8")
    print(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

