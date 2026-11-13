"""Decide what a commit's GitHub check runs say (#2033).

One decision, shared by the loop ``ci_green`` gate (``kew.loops.ci``) and the
epic driver's merge gate (``kew.graph.driver``). GitHub reports a skipped
required check as passing, and private-repository CI skips the self-hosted
test job of an agent PR until a human adds ``ci-approved``. So a skipped
check is never green here:

* Each check name is decided by its latest run that was not skipped. A
  skipped run beside one that ran is a later workflow that ran nothing on
  this head (a ``labeled`` event for an unrelated label), so it cannot hide
  that run's result.
* A name whose every run was skipped did not run. When it is one of the
  approval-gated checks (``ci.approval_gated_checks``: the jobs ci.yml skips
  for an agent PR until a human adds ``ci-approved``, or a matrix leg such as
  ``test (3.12)``), the head is ``awaiting_approval``. Any other all-skipped
  check (push-only, paths-filtered, matrix-excluded) is ignored, as GitHub
  ignores it: it neither blocks nor counts as green. The PR's label never
  decides: it lands seconds before the labelled run's check runs exist.
* A failure wins over everything else.
* Green needs at least one check that concluded ``success``. Nothing here
  names "the" required test check, so a head with no successful check at
  all is not green (``no_checks``).
* A head with no check runs has only its legacy combined commit status, and
  a commit status never makes a head green on its own: an unrelated early
  status (a bot, a deploy preview) can post ``success`` before the head's
  Actions check runs register. ``failure``/``error`` is a failure (it
  blocks), ``pending`` waits, and ``success`` is ``no_evidence`` with
  :data:`STATUSES_ONLY_EVIDENCE`: a human merges by hand. No check runs and
  no statuses is ``no_evidence`` too (:data:`NO_CI_EVIDENCE`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

CheckState = Literal[
    "pending", "success", "failure", "awaiting_approval", "no_checks", "no_evidence"
]

AWAITING_APPROVAL_EVIDENCE = "awaiting ci-approved: tests did not run"
NO_CI_EVIDENCE = "no CI evidence on head: merge by hand"
STATUSES_ONLY_EVIDENCE = "commit statuses green but no check runs: merge by hand"
# The no-check-run evidence lines that ask a human to merge by hand.
HUMAN_MERGE_EVIDENCE = frozenset({NO_CI_EVIDENCE, STATUSES_ONLY_EVIDENCE})

# Conclusions that count as passing. Any OTHER value on a completed check —
# including ``skipped``, a future/unknown GitHub conclusion or None — is not.
_GREEN_CONCLUSIONS = frozenset({"success", "neutral"})
_SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | ... | None while running
    id: int = 0  # GitHub's check-run id; a later run of a name has a larger id


@dataclass(frozen=True)
class CommitStatus:
    """A head's legacy combined commit status."""

    state: str  # success | pending | failure | error
    total_count: int


def check_runs_command(repo: str, sha: str) -> list[str]:
    """The fixed ``gh`` argv that lists every check run of *sha*.

    ``filter=all``, not the default ``latest``: a later workflow for an
    unrelated label skips every job on the same head, and ``latest`` would
    then hide the run that tested it. The ``--jq`` filter makes ``gh`` stream
    one JSON object per run per line and merges paginated pages.
    """
    return [
        "gh",
        "api",
        "--paginate",
        f"repos/{repo}/commits/{sha}/check-runs?filter=all",
        "--jq",
        ".check_runs[] | {id, name, status, conclusion}",
    ]


def parse_check_runs(stdout: str | None) -> list[CheckRun]:
    """Parse the NDJSON :func:`check_runs_command` prints."""
    runs: list[CheckRun] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        runs.append(
            CheckRun(
                name=str(obj.get("name") or ""),
                status=str(obj.get("status") or ""),
                conclusion=obj.get("conclusion"),
                id=int(obj.get("id") or 0),
            )
        )
    return runs


def _deciding_runs(runs: list[CheckRun]) -> tuple[list[CheckRun], list[str]]:
    """Per check name, the run that decides it, and the names that never ran."""
    by_name: dict[str, list[CheckRun]] = {}
    for run in runs:
        by_name.setdefault(run.name, []).append(run)
    deciding: list[CheckRun] = []
    never_ran: list[str] = []
    for name, named in by_name.items():
        ran = [
            r
            for r in named
            if not (r.status == "completed" and r.conclusion == _SKIPPED)
        ]
        if ran:
            deciding.append(max(ran, key=lambda r: r.id))
        else:
            never_ran.append(name)
    return deciding, never_ran


def is_approval_gated(name: str, gated_checks: Sequence[str]) -> bool:
    """Whether check-run *name* is a gated job or one of its matrix legs."""
    return any(name == gated or name.startswith(f"{gated} (") for gated in gated_checks)


def _settled(deciding: list[CheckRun], awaiting: list[str]) -> tuple[CheckState, str]:
    """Classify completed checks: failed, a gated check never ran, nothing
    passed, or green. *awaiting* holds only the approval-gated names that never
    ran; any other never-ran name was already dropped, as GitHub drops it."""
    bad = [r for r in deciding if r.conclusion not in _GREEN_CONCLUSIONS]
    if bad:
        return "failure", "CI failed: " + ", ".join(sorted(r.name for r in bad))
    if awaiting:
        names = ", ".join(sorted(awaiting))
        return "awaiting_approval", f"{AWAITING_APPROVAL_EVIDENCE} ({names} skipped)"
    if not any(r.conclusion == "success" for r in deciding):
        return "no_checks", "no check run concluded success"
    plural = "" if len(deciding) == 1 else "s"
    return "success", f"CI green ({len(deciding)} check{plural})"


def classify_check_runs(
    runs: list[CheckRun], *, gated_checks: Sequence[str]
) -> tuple[CheckState, str]:
    """Return ``(state, evidence)`` for one commit's check runs.

    An empty list is ``no_checks``; any run still in flight is ``pending``.
    *gated_checks* is ``ci.approval_gated_checks``; every caller passes it.
    """
    if not runs:
        return "no_checks", "no CI check-runs"
    incomplete = [r for r in runs if r.status != "completed"]
    if incomplete:
        return "pending", f"{len(incomplete)}/{len(runs)} checks still running"
    deciding, never_ran = _deciding_runs(runs)
    awaiting = [name for name in never_ran if is_approval_gated(name, gated_checks)]
    return _settled(deciding, awaiting)


def commit_status_command(repo: str, sha: str) -> list[str]:
    """The fixed ``gh`` argv that reads *sha*'s combined commit status."""
    return [
        "gh",
        "api",
        f"repos/{repo}/commits/{sha}/status",
        "--jq",
        "{state, total_count}",
    ]


def parse_commit_status(stdout: str | None) -> CommitStatus:
    """Parse :func:`commit_status_command`'s output; raise when malformed."""
    obj = json.loads(stdout or "")
    if not isinstance(obj, dict):
        raise ValueError("combined commit status is not an object")
    return CommitStatus(str(obj.get("state") or ""), int(obj.get("total_count") or 0))


def classify_commit_status(status: CommitStatus) -> tuple[CheckState, str]:
    """Decide a head that has no check runs from its combined commit status.

    Never ``success``: a status alone cannot show the head was tested.
    """
    if status.total_count <= 0:
        return "no_evidence", NO_CI_EVIDENCE
    count = f"{status.total_count} commit status"
    count += "" if status.total_count == 1 else "es"
    if status.state == "success":
        return "no_evidence", STATUSES_ONLY_EVIDENCE
    if status.state == "pending":
        return "pending", f"{count} pending"
    return "failure", f"CI failed: {count}, combined state {status.state or '?'}"
