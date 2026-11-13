"""Overlap coordination for concurrent agent dispatch.

Predicts which files an issue will modify, detects overlap with in-flight
work, and manages hold/release to prevent conflicting parallel dispatch.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew.config import DispatchConfig
from kew.models import GhIssue
from kew.progress import _log, _warn
from kew.subprocess_utils import run

# Regex for file paths: contains / or ends in a known extension
_FILE_EXT_RE = re.compile(
    r"(?:[\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|java|toml|yaml|yml|json|md|j2|html|css|sql))\b"
)

# Backtick identifiers: `some_identifier`
_BACKTICK_RE = re.compile(r"`(\w+)`")

# Directory scopes must be explicit. A trailing slash, a multi-part path, or a
# backticked path qualifies. Bare prose such as "update docs" does not.
_PATH_SCOPE_RE = re.compile(
    r"(?<![\w./-])((?:[\w.-]++/)++(?:[\w.-]++)?)(?=$|[\s`'\"),.:;])"
)
_BACKTICK_SCOPE_RE = re.compile(r"`([^`\r\n]+)`")
_DIRECTORY_KEY_PREFIX = "@directory:"

# Module keyword boundary: standalone words
_WORD_RE = re.compile(r"\b(\w+)\b")


def _match_file_paths(text: str, source_files: list[str]) -> set[str]:
    """Match explicit file paths / filenames mentioned in text."""
    matched: set[str] = set()
    for match in _FILE_EXT_RE.finditer(text):
        mentioned = match.group(0)
        for src_file in source_files:
            if src_file.endswith(mentioned) or mentioned == src_file:
                matched.add(src_file)
    return matched


def _match_backtick_identifiers(
    text: str, identifier_index: dict[str, set[str]]
) -> set[str]:
    """Resolve backtick identifiers in text via the identifier index."""
    matched: set[str] = set()
    for match in _BACKTICK_RE.finditer(text):
        ident = match.group(1)
        if ident in identifier_index:
            matched.update(identifier_index[ident])
    return matched


def _match_module_keywords(text: str, module_index: dict[str, set[str]]) -> set[str]:
    """Resolve file stems and explicitly scoped directory mentions."""
    matched: set[str] = set()
    words = set(_WORD_RE.findall(text.lower()))
    scoped_directories = {
        match.group(1).strip().lower().rstrip("/")
        for match in _BACKTICK_SCOPE_RE.finditer(text)
    }
    scoped_directories.update(
        match.group(1).lower().rstrip("/") for match in _PATH_SCOPE_RE.finditer(text)
    )
    for keyword, files in module_index.items():
        if keyword.startswith(_DIRECTORY_KEY_PREFIX):
            directory = keyword.removeprefix(_DIRECTORY_KEY_PREFIX)
            if directory in scoped_directories:
                matched.update(files)
        elif keyword.lower() in words:
            matched.update(files)
    return matched


def predict_files_from_text(
    text: str,
    *,
    source_files: list[str],
    identifier_index: dict[str, set[str]] | None = None,
    module_index: dict[str, set[str]] | None = None,
) -> set[str]:
    """Predict which source files an issue will modify from its text.

    Tier 1 text-matching prediction. Extracts:
    - File paths mentioned in text (matched against source_files)
    - Backtick identifiers resolved via identifier_index
    - File-stem keywords resolved via module_index
    - Direct directory children resolved from path-like or backticked mentions
    """
    predicted = _match_file_paths(text, source_files)
    if identifier_index:
        predicted |= _match_backtick_identifiers(text, identifier_index)
    if module_index:
        predicted |= _match_module_keywords(text, module_index)
    return predicted


def build_module_index(source_files: list[str]) -> dict[str, set[str]]:
    """Build a keyword -> files index from source file paths.

    Filename stems remain plain keywords. Direct parent paths use an internal
    prefix. Ancestors are not indexed because they select whole subtrees.
    """
    index: dict[str, set[str]] = {}
    for file_path in source_files:
        p = Path(file_path)
        stem = p.stem.lower().lstrip("_")
        if stem and len(stem) > 2:
            index.setdefault(stem, set()).add(file_path)
        parent_path = p.parent.as_posix().lower()
        if parent_path != "." and len(parent_path) > 2:
            key = f"{_DIRECTORY_KEY_PREFIX}{parent_path}"
            index.setdefault(key, set()).add(file_path)
    return index


def build_identifier_index(source_files: list[str]) -> dict[str, set[str]]:
    """Build an identifier -> files index by scanning for top-level definitions.

    Extracts class and function names from Python files using regex,
    mapping each identifier to the set of files that define it.
    """
    _DEF_RE = re.compile(r"^(?:class|def)\s+(\w+)", re.MULTILINE)
    index: dict[str, set[str]] = {}
    for file_path in source_files:
        if not file_path.endswith(".py"):
            continue
        try:
            content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _DEF_RE.finditer(content):
            name = match.group(1)
            if len(name) > 2:
                index.setdefault(name, set()).add(file_path)
    return index


@dataclass
class OverlapDecision:
    """Result of an overlap check."""

    action: str  # "dispatch" or "hold"
    blocking_issue: int | None = None
    reason: str = ""
    predicted_files: set[str] = field(default_factory=set)
    warning_context: str | None = None


@dataclass
class HoldInfo:
    """Metadata for a held issue."""

    blocking_issues: set[int]
    predicted_overlap_files: set[str]
    held_at: datetime


@dataclass
class MergedContext:
    """Recently completed work that may overlap with queued issues."""

    actual_files: list[str]
    completed_at: datetime


#: A refusal whose claim could not be handed back: the issue is still claimed
#: with no run, so the outcome names it instead of reporting a clean refusal.
CLAIM_ROLLBACK_FAILED = "claim_rollback_failed"


@dataclass
class DispatchOutcome:
    """Structured worker result with files and an operator-facing explanation."""

    exit_code: int
    changed_files: list[str] = field(default_factory=list)
    run_id: str | None = None
    category: str = ""
    reason: str = ""
    #: False when the outcome was decided before any agent process started
    #: (capacity, admission, approval, connectivity, launch failure, refusal).
    #: The command boundary reports such outcomes as could-not-run (exit 2)
    #: instead of "agent exited with code N" (W4.2 scheme, #1943 round 2).
    launched: bool = True

    def __post_init__(self) -> None:
        """Keep every worker completion line meaningful for the operator."""
        if not self.category:
            self.category = "succeeded" if self.exit_code == 0 else "failed"
        if not self.reason:
            self.reason = (
                "dispatch completed successfully"
                if self.exit_code == 0
                else f"dispatch ended with exit code {self.exit_code}"
            )


class OverlapCoordinator:
    """Coordinates overlap detection and hold/release for concurrent dispatch."""

    def __init__(self) -> None:
        self.in_flight: dict[int, set[str]] = {}
        self.recently_merged: dict[int, MergedContext] = {}
        self.holds: dict[int, HoldInfo] = {}
        self._hold_history: dict[int, set[int]] = {}

    def check(
        self,
        issue: GhIssue,
        config: DispatchConfig,
        *,
        source_files: list[str],
        identifier_index: dict[str, set[str]] | None = None,
        module_index: dict[str, set[str]] | None = None,
    ) -> OverlapDecision:
        if config.overlap.strategy == "off":
            return OverlapDecision(action="dispatch")

        if not self.in_flight:
            return OverlapDecision(action="dispatch")

        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        text = f"{title} {body}"

        predicted = predict_files_from_text(
            text,
            source_files=source_files,
            identifier_index=identifier_index,
            module_index=module_index,
        )

        if not predicted:
            return OverlapDecision(action="dispatch")

        for inflight_num, inflight_files in self.in_flight.items():
            overlap = predicted & inflight_files
            if overlap:
                overlap_str = ", ".join(sorted(overlap))
                reason = f"overlaps with #{inflight_num} on {overlap_str}"

                if config.overlap.strategy == "warn":
                    warning = (
                        f"Warning: issue #{inflight_num} is currently modifying "
                        f"{overlap_str}. Your issue may touch the same files. "
                        f"Pay attention to recent changes in these files."
                    )
                    return OverlapDecision(
                        action="dispatch",
                        predicted_files=predicted,
                        warning_context=warning,
                    )

                return OverlapDecision(
                    action="hold",
                    blocking_issue=inflight_num,
                    reason=reason,
                    predicted_files=predicted,
                )

        return OverlapDecision(action="dispatch", predicted_files=predicted)

    def on_dispatch(self, issue_num: int, predicted_files: set[str]) -> None:
        self.in_flight[issue_num] = predicted_files

    def on_complete(self, issue_num: int, actual_files: list[str]) -> set[int]:
        self.in_flight.pop(issue_num, None)
        self.recently_merged[issue_num] = MergedContext(
            actual_files=actual_files,
            completed_at=datetime.now(UTC),
        )

        released: set[int] = set()
        for held_num, hold in list(self.holds.items()):
            if issue_num in hold.blocking_issues:
                hold.blocking_issues.discard(issue_num)
                if not hold.blocking_issues:
                    del self.holds[held_num]
                    released.add(held_num)
        return released

    def add_hold(
        self,
        issue_num: int,
        *,
        blocking_issues: set[int],
        overlap_files: set[str],
    ) -> None:
        if issue_num in self.holds:
            self.holds[issue_num].blocking_issues.update(blocking_issues)
            self.holds[issue_num].predicted_overlap_files.update(overlap_files)
        else:
            self.holds[issue_num] = HoldInfo(
                blocking_issues=set(blocking_issues),
                predicted_overlap_files=set(overlap_files),
                held_at=datetime.now(UTC),
            )
        self._hold_history.setdefault(issue_num, set()).update(blocking_issues)

    def get_merge_context(self, issue_num: int, config: DispatchConfig) -> str | None:
        """Return recently-merged context for a previously-held issue."""
        blocking_nums = self._hold_history.get(issue_num)
        if not blocking_nums:
            return None

        ttl_minutes = config.overlap.advanced.merge_context_ttl_minutes
        cutoff = datetime.now(UTC) - timedelta(minutes=ttl_minutes)

        sections: list[str] = []
        for blocker_num in sorted(blocking_nums):
            merged = self.recently_merged.get(blocker_num)
            if merged is None:
                continue
            if merged.completed_at < cutoff:
                continue
            file_list = "\n".join(f"- {f}" for f in sorted(merged.actual_files))
            sections.append(
                f"Issue #{blocker_num} recently merged and modified:\n{file_list}"
            )

        if not sections:
            return None

        header = "## Recently Merged Related Work\n\n"
        body = "\n\n".join(sections)
        footer = (
            "\n\nPay attention to recent changes in these files. "
            "Read them before making modifications to ensure your "
            "work builds on the latest state."
        )
        return header + body + footer


def _run_gh_json(cmd: list[str]) -> list[dict[str, Any]]:
    """Run a gh CLI command and return parsed JSON list.

    Raises RuntimeError on non-zero exit so callers can handle failures.
    """
    try:
        result = run(cmd, check=False, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh command timed out: {' '.join(cmd)}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"gh command failed (exit {result.returncode}): {stderr}")
    output = result.stdout.strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def strip_stale_holds(config: DispatchConfig) -> list[tuple[str, str]]:
    """Remove overlap-hold labels from all issues (startup cleanup)."""
    failures: list[tuple[str, str]] = []
    hold_label = config.labels.overlap_hold
    try:
        issues = _run_gh_json(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                config.project.repo,
                "--label",
                hold_label,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,labels",
            ]
        )
    except RuntimeError as exc:
        _warn(f"Failed to list overlap-hold issues: {exc}")
        return [("List stale overlap holds", str(exc))]

    for issue in issues:
        num = issue.get("number")
        if not isinstance(num, int):
            continue
        try:
            result = run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(num),
                    "--repo",
                    config.project.repo,
                    "--remove-label",
                    hold_label,
                ],
                check=False,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                failures.append(
                    (
                        f"Strip stale overlap hold #{num}",
                        f"exit {result.returncode}: {stderr}",
                    )
                )
                _warn(
                    f"Failed to strip overlap-hold from #{num} "
                    f"(exit {result.returncode}): {stderr}"
                )
            else:
                _log(f"Stripped stale overlap-hold from #{num}")
        except Exception as exc:
            failures.append((f"Strip stale overlap hold #{num}", str(exc)))
            _warn(f"Failed to strip overlap-hold from #{num}: {exc}")
    return failures
