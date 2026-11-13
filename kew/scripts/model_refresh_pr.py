"""Build the weekly model-catalog refresh PR body (#2718).

``.github/workflows/pricing-freshness.yml`` runs ``kew models refresh --write
--report REPORT`` and then this script. The report (``render_report`` in
``kew.model_refresh``) already lists every price and lifecycle change with its
source URL; this script wraps it with the labels the PR carries and the rule
that a person merges it.

A report whose price and lifecycle tables both say ``None.`` means the catalog
did not change, so there is no PR: the script writes no body and prints
``changed=false``. A report missing either table is refused, never read as
"no change".

    uv run python scripts/model_refresh_pr.py --report R --label L... --output B

Prints ``changed=true`` or ``changed=false`` (a ``$GITHUB_OUTPUT`` line).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REFRESH_BRANCH = "models-refresh/weekly"
REQUIRED_LABELS = ("model-catalog-refresh", "needs-human-merge")
# The tables whose rows ``--write`` applies to the catalog. The other report
# sections (disagreements, new ids) are informational and change nothing.
CHANGE_TABLES = ("### Price changes", "### Lifecycle changes")
# GitHub refuses a PR body over 65536 characters; leave room for the wrapper.
MAX_REPORT_CHARS = 60_000


class ReportError(ValueError):
    """The report or the label set cannot back a refresh PR."""


def _table_rows(report: str, heading: str) -> list[str]:
    lines = report.splitlines()
    if heading not in lines:
        raise ReportError(f"report has no {heading!r} section")
    rows = []
    for line in lines[lines.index(heading) + 1 :]:
        if line.startswith("#"):
            break
        if line.strip():
            rows.append(line)
    return rows


def report_changes_catalog(report: str) -> bool:
    """True when the report lists at least one price or lifecycle change."""
    return any(_table_rows(report, h) != ["None."] for h in CHANGE_TABLES)


def build_pr_body(report: str, labels: Sequence[str]) -> str | None:
    """The refresh PR body, or None when the catalog did not change."""
    missing = [label for label in REQUIRED_LABELS if label not in labels]
    if missing:
        raise ReportError(f"refresh PR must carry labels {missing}")
    if not report_changes_catalog(report):
        return None
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n\n_(report truncated; see run log)_\n"
    label_list = ", ".join(f"`{label}`" for label in labels)
    return "\n".join(
        [
            report.rstrip("\n"),
            "",
            "---",
            "",
            "**A person merges this PR. No automation does**: kew, CI and the",
            "weekly workflow never merge or approve it. Check each price change",
            "against its source URL before you merge.",
            "",
            f"Labels: {label_list}.",
            "",
            f"Opened by the weekly pricing-freshness workflow from `{REFRESH_BRANCH}`,",
            "which `kew models refresh --write` rebuilds from `main` each week.",
            "",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--label", action="append", default=[], dest="labels")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        body = build_pr_body(args.report.read_text(encoding="utf-8"), args.labels)
    except (OSError, ReportError) as exc:
        print(f"model refresh PR: {exc}", file=sys.stderr)
        return 2
    if body is None:
        print("changed=false")
        return 0
    args.output.write_text(body, encoding="utf-8")
    print("changed=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
