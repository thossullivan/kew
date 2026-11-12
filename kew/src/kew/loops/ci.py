"""CI poll loop for the ci_green stopping condition (#289).

Polls GitHub commit check-runs to a terminal conclusion. Pure and gate-agnostic
(the gate adapter is CiGreenCondition in verification.py). All time and network
are injected so tests run with zero real network and zero real sleep. Anti-proxy:
every "can't verify" path returns a non-"success" conclusion, which the gate maps
to needs_human.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from kew import subprocess_utils
from kew.ci_checks import (
    NO_CI_EVIDENCE,
    CheckRun,
    CommitStatus,
    check_runs_command,
    classify_check_runs,
    classify_commit_status,
    commit_status_command,
    parse_check_runs,
    parse_commit_status,
)

CiConclusion = Literal[
    "success", "failure", "timeout", "no_checks", "awaiting_approval"
]

# How a red CI run failed. ``code_failure`` = at least one job executed a step
# and something under our control went red (a fixable failure). ``infra_dead``
# = NO job executed any step across the whole run (billing failure, runner
# offline) — a first-class distinct verdict from "steps ran and failed" so the
# driver can park the node with an infra diagnostic instead of burning a
# doomed ci-fix run (F5; see the 2026-07-20 zero-step billing-red incident).
CiFailureClass = Literal["code_failure", "infra_dead"]

# gh step conclusions that mean the step actually executed. A step that never
# ran (job died at setup: billing/runner-offline) is null or "skipped".
_EXECUTED_STEP_CONCLUSIONS = frozenset({"success", "failure", "cancelled"})

POLL_INTERVAL_SECS = 15
GRACE_SECS = 90


@dataclass(frozen=True)
class CiResult:
    conclusion: CiConclusion
    evidence: str


@dataclass(frozen=True)
class JobRun:
    """One GitHub Actions job of a workflow run, with how many of its steps
    actually executed. ``executed_steps == 0`` for a job the runner never got
    to run (billing failure, runner offline)."""

    name: str
    conclusion: str | None  # success | failure | ... | None while running
    executed_steps: int


def _executed_step_count(steps: object) -> int:
    """Count steps that actually ran. A step that never executed (job died at
    setup) reports a null/"skipped" conclusion and is not counted."""
    if not isinstance(steps, list):
        return 0
    return sum(
        1
        for s in steps
        if isinstance(s, dict) and s.get("conclusion") in _EXECUTED_STEP_CONCLUSIONS
    )


def _gh_run_jobs(repo: str, run_id: str) -> list[JobRun]:
    """Fetch all jobs (with step-execution counts) for an Actions run via gh.

    Uses the same fixed-argv, ``--jq``-streamed NDJSON contract as
    ``_gh_check_runs`` so ``--paginate`` merges pages on this OBJECT endpoint
    ({total_count, jobs:[...]}). Raises ``RuntimeError`` on a gh failure so the
    caller can fail closed (treat an unfetchable run as not-yet-classifiable).
    """
    result = subprocess_utils.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/actions/runs/{run_id}/jobs",
            "--jq",
            ".jobs[] | {name, conclusion, steps}",
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise RuntimeError(f"gh run jobs failed (rc={result.returncode}): {stderr}")
    jobs: list[JobRun] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        jobs.append(
            JobRun(
                name=str(obj.get("name") or ""),
                conclusion=obj.get("conclusion"),
                executed_steps=_executed_step_count(obj.get("steps")),
            )
        )
    return jobs


def classify_ci_failure(jobs: list[JobRun]) -> CiFailureClass:
    """Classify a red run's jobs as ``code_failure`` or ``infra_dead``.

    ``infra_dead`` iff NO job executed a single step across the whole run —
    the billing-failure / runner-offline signature where CI reports red
    without ever running our code. If ANY job ran at least one step, the
    failure is (potentially) under our control, so it is ``code_failure`` and
    a bounded ci-fix run may be worth attempting. An empty job list (nothing
    ran at all) is also infra-dead.
    """
    if any(job.executed_steps > 0 for job in jobs):
        return "code_failure"
    return "infra_dead"


def _gh_check_runs(repo: str, sha: str) -> list[CheckRun]:
    """Fetch all check-runs for a commit via gh (allowlisted). Raises on error.

    Uses a fixed argv (no shell, no agent input). The ``--jq`` filter is
    MANDATORY for the NDJSON contract: it makes gh stream one JSON object per
    check-run per line AND merges paginated pages — without it, ``--paginate``
    on this OBJECT endpoint ({total_count, check_runs:[...]}) emits one merged
    object, not the per-line records this parser expects. (Verified gh 2.76.x.)

    A transient or rate-limited ``gh`` failure is intentionally surfaced as
    ``RuntimeError`` and handled by the poll loop's transient path (re-fetched
    on the next tick, ultimately fail-closing to timeout→needs_human) — so no
    ``run_with_backoff`` is needed here.
    """
    result = subprocess_utils.run(
        check_runs_command(repo, sha),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise RuntimeError(f"gh check-runs failed (rc={result.returncode}): {stderr}")
    return parse_check_runs(result.stdout)


def _gh_commit_status(repo: str, sha: str) -> CommitStatus:
    """Read a head's legacy combined commit status via gh. Raises on error."""
    result = subprocess_utils.run(
        commit_status_command(repo, sha),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        raise RuntimeError(
            f"gh commit status failed (rc={result.returncode}): {stderr}"
        )
    return parse_commit_status(result.stdout)


def _aggregate(runs: list[CheckRun], gated_checks: Sequence[str]) -> tuple[str, str]:
    """Classify a NON-empty check-run list -> (state, evidence).

    The decision is :func:`kew.ci_checks.classify_check_runs`, shared with the
    epic driver's merge gate. Callers handle the empty case (grace period).
    """
    assert runs, "_aggregate requires a non-empty list; callers guard for empty"
    return classify_check_runs(runs, gated_checks=gated_checks)


def _no_runs_terminal(
    status: CommitStatus | str | None, elapsed: float, grace_secs: int
) -> tuple[CiResult | None, str]:
    """No check runs: decide from the legacy commit status, as the driver does
    (#2033). A commit status never makes the head green: failure/error is red,
    pending keeps polling, and success keeps polling until the grace period (the
    head's check runs may still register) and then ends as ``no_checks``. None
    at all is not green either. A string is why the status could not be read:
    at any elapsed time it keeps polling, as an unreadable check-run list does,
    with the reason on the status line; the overall loop timeout bounds it."""
    if isinstance(status, str):
        return None, f"no check runs; commit status unreadable: {status}"
    if status is not None and status.total_count > 0:
        state, evidence = classify_commit_status(status)
        if state == "failure":
            return CiResult("failure", evidence), evidence
        if state == "pending" or elapsed < grace_secs:
            return None, evidence
        return CiResult("no_checks", evidence), evidence
    if elapsed >= grace_secs:
        ev = (
            f"{NO_CI_EVIDENCE} — no check runs and no commit statuses after "
            f"{int(elapsed)}s (branch not pushed, or no CI?)"
        )
        return CiResult("no_checks", ev), ev
    return None, "no checks registered yet"


def _terminal(
    runs: list[CheckRun] | None,
    fetch_err: str,
    elapsed: float,
    grace_secs: int,
    status: CommitStatus | str | None,
    gated_checks: Sequence[str],
) -> tuple[CiResult | None, str]:
    """One poll result -> (terminal CiResult or None to keep polling, status line)."""
    if runs is None:
        return None, f"ci poll error: {fetch_err}"
    if not runs:
        return _no_runs_terminal(status, elapsed, grace_secs)
    state, evidence = _aggregate(runs, gated_checks)
    if state in ("success", "failure", "awaiting_approval", "no_checks"):
        return CiResult(cast(CiConclusion, state), evidence), evidence
    return None, evidence  # pending


def _read_status(
    fetch: Callable[[str, str], CommitStatus], repo: str, sha: str
) -> CommitStatus | str:
    """The legacy commit status, or why it could not be read. An unreadable
    status never passes: the poll reports the reason and keeps waiting."""
    try:
        return fetch(repo, sha)
    except Exception as exc:  # noqa: BLE001 - reported, never green
        return f"{type(exc).__name__}: {exc}"[:200]


def wait_for_ci(
    repo: str,
    sha: str,
    *,
    timeout_secs: int,
    gated_checks: Sequence[str],
    poll_interval_secs: int = POLL_INTERVAL_SECS,
    grace_secs: int = GRACE_SECS,
    _fetch: Callable[[str, str], list[CheckRun]] = _gh_check_runs,
    _fetch_status: Callable[[str, str], CommitStatus] = _gh_commit_status,
    _now: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> CiResult:
    """Poll commit check-runs until a terminal CiConclusion.

    success/failure resolve as soon as all checks complete; with no check runs
    the legacy commit status decides (#2033); zero checks and zero statuses
    past grace -> no_checks; still pending past timeout -> timeout; a fetch error is
    transient (keep polling) and never passes.
    """
    start = _now()
    last = "no checks registered yet"
    while True:
        try:
            runs: list[CheckRun] | None = _fetch(repo, sha)
            fetch_err = ""
        except Exception as exc:  # noqa: BLE001 - any fetch failure is transient
            runs, fetch_err = None, str(exc)
        status = _read_status(_fetch_status, repo, sha) if runs == [] else None
        elapsed = _now() - start
        terminal, last = _terminal(
            runs, fetch_err, elapsed, grace_secs, status, gated_checks
        )
        if terminal is not None:
            return terminal
        if elapsed >= timeout_secs:
            msg = f"CI still pending after {timeout_secs}s: {last}"
            return CiResult("timeout", msg)
        _sleep(poll_interval_secs)
