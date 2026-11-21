"""Count history in comments and docstrings, per file, against a ledger.

Two counts per file under ``src/`` and ``tests/``: lines that cite an issue or
PR number, and lines that tell review or incident history. Each count may only
fall. ``ratchets/comment_history.json`` records the current counts.

Usage: ``uv run python scripts/comment_scan.py [--check | --lower | --initial]``
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, fields
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("src", "tests")
BASELINE_PATH = PACKAGE_ROOT / "ratchets" / "comment_history.json"

# `#` then 3+ digits outside an HTML entity such as `&#128512;`, a prefixed
# `PR#123`, `GH-123`, a pull or issue URL, or "PR 123" / "issue 123".
ISSUE_REF = re.compile(
    r"(?<![\w&])#\d{3,}\b"
    r"|\b(?:PR|PRs|issue|issues|kew|gh)#\d{2,}\b"
    r"|\bGH-\d{2,}\b"
    r"|github\.com/[^/\s]+/[^/\s]+/(?:pull|issues)/\d+\b"
    r"|\b(?:PR|PRs|issue|issues)\s+\d{3,}\b",
    re.IGNORECASE,
)
# Narrow on purpose: review rounds and incidents are also kew's own domain
# terms, and remediation fixes "what the reviewer flagged", so only story
# phrasing counts.
HISTORY = re.compile(
    r"(?<!what )\bround\s+\d+\s+(?:found|caught|flagged|showed)\b"
    r"|\bR\d+\s+review\b"
    r"|(?<!what the )(?<!what a )\breviewers?\s+(?:found|caught|flagged)\b"
    r"|\b(?:it|this|that|which|we|kew)\s+used\s+to\s+(?:crash|fail|break|hang|leak)\b",
    re.IGNORECASE,
)


class ScanCannotRunError(RuntimeError):
    """The scan cannot give a verdict."""


@dataclass
class FileCounts:
    issue_refs: int = 0
    history: int = 0

    def nonzero(self) -> dict[str, int]:
        return {
            f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name)
        }


METRICS = tuple(f.name for f in fields(FileCounts))


def _prose_lines(source: str) -> list[str]:
    """Every comment line and docstring line in *source*.

    A docstring is any bare string statement: module, class and function
    docstrings, and the attribute docstrings that follow an assignment.
    """
    lines: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            lines.append(token.string)
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            lines.extend(node.value.value.splitlines())
    return lines


def scan_source(source: str, key: str) -> FileCounts:
    try:
        lines = _prose_lines(source)
    except (SyntaxError, tokenize.TokenError) as exc:
        raise ScanCannotRunError(f"{key}: cannot parse: {exc}") from exc
    return FileCounts(
        issue_refs=sum(1 for line in lines if ISSUE_REF.search(line)),
        history=sum(1 for line in lines if HISTORY.search(line)),
    )


def scan_tree(root: Path = PACKAGE_ROOT) -> dict[str, FileCounts]:
    counts: dict[str, FileCounts] = {}
    for top in SCAN_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            key = path.relative_to(root).as_posix()
            counts[key] = scan_source(path.read_text(encoding="utf-8"), key)
    return counts


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, dict[str, int]]:
    if not path.exists():
        raise ScanCannotRunError(
            f"{path} is missing; --initial writes the first ledger"
        )
    data = json.loads(path.read_text())
    files = data.get("files")
    if not isinstance(files, dict):
        raise ScanCannotRunError(f"{path} has no 'files' object")
    return files


def render_baseline(files: dict[str, dict[str, int]]) -> str:
    """Sorted keys and no zero counts, so a lowering diff is one line per change."""
    clean = {
        key: {m: n for m, n in sorted(counts.items()) if n}
        for key, counts in sorted(files.items())
    }
    clean = {key: counts for key, counts in clean.items() if counts}
    return json.dumps({"files": clean}, indent=2) + "\n"


def ratchet_problems(
    counts: dict[str, FileCounts], baseline: dict[str, dict[str, int]]
) -> list[str]:
    """Rises and falls against the ledger. Empty means the tree matches it."""
    rises: list[str] = []
    falls: list[str] = []
    for key in sorted(set(counts) | set(baseline)):
        have = counts.get(key, FileCounts())
        recorded = baseline.get(key, {})
        for metric in METRICS:
            now, was = getattr(have, metric), recorded.get(metric, 0)
            if now > was:
                rises.append(f"{key}: {metric} {now} (baseline {was})")
            elif now < was:
                falls.append(f"{key}: {metric} {now} (baseline {was})")
    problems: list[str] = []
    if rises:
        problems.append(
            "Comment history rose above the baseline. A comment states a current "
            "constraint; issue numbers, review rounds and incident stories go in "
            "the commit message (AGENTS.md, Comments). The baseline in "
            "ratchets/comment_history.json is not raised to make room. A file "
            "that moves or splits carries its entry to the new path in the "
            "same PR:\n  " + "\n  ".join(rises)
        )
    if falls:
        problems.append(
            "Comment history fell below the baseline. Bank it with "
            "`uv run python scripts/comment_scan.py --lower`:\n  " + "\n  ".join(falls)
        )
    return problems


def lowered(
    counts: dict[str, FileCounts], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every fall banked and no rise admitted."""
    return {
        key: {
            m: min(recorded.get(m, 0), getattr(counts.get(key, FileCounts()), m))
            for m in METRICS
        }
        for key, recorded in baseline.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print the verdict")
    mode.add_argument("--lower", action="store_true", help="bank decreases only")
    mode.add_argument(
        "--initial", action="store_true", help="write the first ledger only"
    )
    args = parser.parse_args(argv)

    if args.initial and BASELINE_PATH.exists():
        print(f"{BASELINE_PATH} exists; use --lower to bank a fall", file=sys.stderr)
        return 2
    counts = scan_tree(PACKAGE_ROOT)
    if args.initial:
        BASELINE_PATH.write_text(
            render_baseline({k: c.nonzero() for k, c in counts.items()})
        )
        return 0
    if args.lower:
        baseline = load_baseline(BASELINE_PATH)
        BASELINE_PATH.write_text(render_baseline(lowered(counts, baseline)))
        return 0
    if args.check:
        problems = ratchet_problems(counts, load_baseline(BASELINE_PATH))
        print("\n\n".join(problems) or "comment ratchet: clean")
        return 1 if problems else 0
    totals = {m: sum(getattr(c, m) for c in counts.values()) for m in METRICS}
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
