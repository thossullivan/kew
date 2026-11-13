"""What the nightly's image scan found, and the issues it files (#3195).

``scripts/scan-agent-image.sh`` gates every image and fails at the end. Per
image it prints a gate section — ``==> gate: … in <tag> (<id>)`` and trivy's
table — and, when the gate did not pass, one line saying how:
``gate findings in <tag>`` (trivy's findings exit) or ``gate error in <tag>:
…`` (any other exit: a scanner that could not run). The ``triage input`` table
after each gate lists every CRITICAL/HIGH, gates nothing, and files nothing.

From that the nightly files one issue per advisory a gate table names — a
``CVE-…``, a ``GHSA-…``, a ``RUSTSEC-…`` or any other id trivy reports —
deduplicated by id, and one issue per sha for everything else: an image whose
gate failed with no advisory kew could read, a gate that errored, a refusal of
the whole scan. Failure is read per image, so a CVE in one image never hides a
broken scan of the next.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kew.audit import emit, resolve_actor
from kew.main_health import (
    CVE_EVENT,
    HEALTH_EVENTS,
    SCAN_FAILED_EVENT,
    Runner,
    _payload,
    create_issue,
)
from kew.public_log import public_line, public_lines

if TYPE_CHECKING:
    from kew.run_store import RunStore

#: The header the scan prints before each image's gating table; any other
#: ``==> `` header ends that section.
GATE_HEADER = "==> gate:"
_SECTION = "==> "
_PREFIX = "scan-agent-image.sh: "

_GATE_TAG = re.compile(r"^==> gate: .* in (\S+) \(")
_FINDINGS_IN = re.compile(r"^scan-agent-image\.sh: gate findings in (\S+)\s*$")
_ERROR_IN = re.compile(r"^scan-agent-image\.sh: gate error in (\S+): (.*?)\s*$")

#: The closing summary the scan prints once every image's gate has run. A
#: log without it is a scan that stopped part-way, whatever it found first.
SCAN_COMPLETE = "gated every image:"

#: The scan's own closing lines, and lines it prints that report nothing.
_NOT_A_REFUSAL = (
    SCAN_COMPLETE,
    "the gate found CRITICAL/HIGH findings in",
    "the gate could not scan",
    "gate findings in",
    "gate error in",
    "could not remove this run's reference",
)
_PINNED_SCANNER = re.compile(r"^trivy \S+, the pinned scanner\.$")

#: A trivy VulnerabilityID: ``CVE-2024-1234``, ``GHSA-xxxx-xxxx-xxxx``,
#: ``RUSTSEC-2024-0001``, ``DLA-3400-1`` and the like.
_ADVISORY = re.compile(r"^[A-Z][A-Z0-9]*-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*$")
_CELL = re.compile(r"[│|]")

#: How much of a failure an issue title carries, how long one recorded
#: failure may be, and how many of a stopped scan's last lines are cleaned to
#: find its last one.
_REASON_CHARS = 160
_FAILURE_CHARS = 600
_STOP_WINDOW = 200


@dataclass(frozen=True)
class ScanFindings:
    """What one scan's log says."""

    #: Every advisory a gating table names, once, in order.
    advisories: tuple[str, ...] = ()
    #: Every way the scan failed other than by an advisory kew can file.
    failures: tuple[str, ...] = ()


def _row_advisory(line: str) -> str | None:
    """The Vulnerability column of one of trivy's table rows, if it is an id."""
    if not _CELL.search(line):
        return None
    cells = [cell.strip() for cell in _CELL.split(line)]
    if len(cells) < 3:
        return None
    candidate = cells[2]
    return candidate if _ADVISORY.match(candidate) else None


@dataclass
class _Reading:
    by_tag: dict[str, list[str]]
    findings: list[str]
    errors: list[str]
    last_line: str | None = None
    complete: bool = False


def _read(log: str) -> _Reading:
    reading = _Reading(by_tag={}, findings=[], errors=[])
    tag: str | None = None
    for line in log.splitlines():
        if line.startswith(_SECTION):
            match = _GATE_TAG.match(line) if line.startswith(GATE_HEADER) else None
            tag = match.group(1) if match else None
            if tag is not None:
                reading.by_tag.setdefault(tag, [])
            continue
        if line.startswith(_PREFIX):
            _read_scan_line(reading, line.strip())
            continue
        found = _row_advisory(line) if tag is not None else None
        if tag is not None and found is not None:
            reading.by_tag[tag].append(found)
    return reading


def _read_scan_line(reading: _Reading, line: str) -> None:
    if (found := _FINDINGS_IN.match(line)) is not None:
        reading.findings.append(found.group(1))
    elif (error := _ERROR_IN.match(line)) is not None:
        tag, detail = error.groups()
        reading.errors.append(f"the gate could not scan {tag}: {detail}")
    if line.removeprefix(_PREFIX).startswith(SCAN_COMPLETE):
        reading.complete = True
    if not _PINNED_SCANNER.match(line.removeprefix(_PREFIX)):
        reading.last_line = line


def read_scan(log: str, exit_code: int | None, error: str | None) -> ScanFindings:
    """The advisories the scan's gates name, and every other way it failed."""
    reading = _read(log)
    advisories = tuple(
        dict.fromkeys(ids for found in reading.by_tag.values() for ids in found)
    )
    failures = list(reading.errors)
    failures += [
        f"the gate found advisories in {tag} that kew could read no id from"
        for tag in reading.findings
        if not reading.by_tag.get(tag)
    ]
    if not reading.complete:
        detail = _stop_detail(reading, log, exit_code, error)
        failures.append(f"the scan stopped before every image was gated: {detail}")
    elif exit_code != 0:
        failures += _refusal(reading, exit_code, error)
    # Every failure lands in a public issue title and body: the scan's own
    # lines name host paths ($TRIVY_PIN_FILE), and a host error can carry
    # anything, so each is cleaned the way the regression tail is.
    public = (public_line(failure, limit=_FAILURE_CHARS) for failure in failures)
    return ScanFindings(advisories, tuple(dict.fromkeys(public)))


def _stop_detail(
    reading: _Reading, log: str, exit_code: int | None, error: str | None
) -> str:
    """What the log says about where a scan that never closed stopped.

    The scan's own refusal when it printed one; else the host's error (a
    timeout); else the log's last line once the window before it is cleaned
    (a ``docker build`` that failed under ``set -e``). The caller cleans what
    this returns for the public issue.
    """
    last = reading.last_line
    if last is not None and not last.removeprefix(_PREFIX).startswith(_NOT_A_REFUSAL):
        return last.removeprefix(_PREFIX)
    if error:
        return error
    window = [line for line in log.splitlines()[-_STOP_WINDOW:] if line.strip()]
    cleaned = [line for line in public_lines(window) if line]
    if cleaned:
        return cleaned[-1]
    return "reached no exit" if exit_code is None else f"exited {exit_code}"


def _refusal(reading: _Reading, exit_code: int | None, error: str | None) -> list[str]:
    """Why a scan that did not pass failed, when no image's gate says."""
    last = reading.last_line
    if last is not None and not last.removeprefix(_PREFIX).startswith(_NOT_A_REFUSAL):
        return [last.removeprefix(_PREFIX)]
    if reading.findings or reading.errors:
        return []
    exited = "reached no exit" if exit_code is None else f"exited {exit_code}"
    return [error or f"the image scan {exited}"]


def filed_scan_failure(store: RunStore, sha: str) -> int | None:
    """The scan-failure issue filed for *sha*, or ``None``."""
    for event in store.list_audit_events(HEALTH_EVENTS, event=SCAN_FAILED_EVENT):
        payload = _payload(event)
        if payload.get("sha") == sha and isinstance(payload.get("issue"), int):
            return int(payload["issue"])
    return None


def file_scan_failure(
    store: RunStore, *, sha: str, reason: str, repo: str | None, gh: Runner
) -> tuple[int | None, bool]:
    """Record a failed scan and file its issue once per sha.

    Returns the issue and whether it was filed before.
    """
    existing = filed_scan_failure(store, sha)
    if existing is not None:
        return existing, True
    number = None
    if repo:
        number = create_issue(
            gh,
            repo,
            title=f"image scan failed: {reason[:_REASON_CHARS]}",
            body=(
                f"kew verify nightly's image scan of main at {sha} failed "
                f"other than by an advisory it could file:\n\n> {reason}\n\n"
                "Until it scans again, those images on main are unscanned. "
                "This issue is filed once per sha."
            ),
        )
    emit(
        store,
        SCAN_FAILED_EVENT,
        actor=resolve_actor(),
        repo=repo,
        issue_num=number,
        payload={"sha": sha, "reason": reason, "issue": number},
        dedup_key=None if number is None else f"{SCAN_FAILED_EVENT}:{sha}",
        strict=True,
    )
    return number, False


def _filed_advisories(store: RunStore) -> set[str]:
    return {
        str(_payload(event).get("advisory"))
        for event in store.list_audit_events(None, event=CVE_EVENT)
    }


def file_advisories(
    store: RunStore, found: Sequence[str], *, sha: str, repo: str, gh: Runner
) -> dict[str, int]:
    """File one issue per advisory not filed before; return the new ones."""
    filed: dict[str, int] = {}
    known = _filed_advisories(store)
    for advisory in found:
        if advisory in known:
            continue
        number = create_issue(
            gh,
            repo,
            title=f"{advisory} in an agent image",
            body=(
                f"kew verify nightly's image scan of main at {sha} found "
                f"{advisory}.\n\nThe per-PR scan runs only when an image input "
                "changes, so this fails no unrelated PR. Bump the package or "
                "record the triage."
            ),
        )
        emit(
            store,
            CVE_EVENT,
            actor=resolve_actor(),
            repo=repo,
            issue_num=number,
            payload={"advisory": advisory, "issue": number, "sha": sha},
            dedup_key=f"{CVE_EVENT}:{advisory}",
            strict=True,
        )
        filed[advisory] = number
    return filed
