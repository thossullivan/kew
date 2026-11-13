"""``kew verify nightly``: the nightly full verify of main (#3195).

Holds the verify lease — the merge-queue lock the drive verifies and merges
under — for its whole run, so the drive yields to it. Verifies ``origin/main``
in full and records it as ``kind: nightly``; a red verdict is bisected and
filed once per sha (:mod:`kew.main_health`); the image scan runs as a job of
its own and files one issue per new advisory (:mod:`kew.nightly_scan`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import subprocess_utils
from kew.main_health import (
    NIGHTLY_KIND,
    NOT_REPRODUCED,
    NOT_REPRODUCED_TEXT,
    PROBE_TIMEOUT_SECONDS,
    RED,
    REPRODUCED,
    SCAN_TIMEOUT_SECONDS,
    MainHealth,
    MainHealthError,
    Probe,
    Regression,
    Runner,
    base_ref_for,
    bisect,
    failing_gate,
    failing_tests,
    file_regression,
    filed_regression,
    inside_selection,
    last_green,
    main_commits,
    pr_of,
    probe_script,
    probe_verdict,
    read_git,
    read_main_health,
    record_tip_probe,
)
from kew.nightly_scan import file_advisories, file_scan_failure, read_scan

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.merge_verify import VerifyCandidate, VerifyResult
    from kew.run_store import RunStore

logger = logging.getLogger(__name__)


class NightlyBusy(MainHealthError):
    """Another verification held the verify lease the whole wait."""


@contextmanager
def verify_lease(
    config: DispatchConfig,
    *,
    wait_seconds: float,
    poll_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold the verify lease — the merge-queue lock — for the block.

    The drive verifies and merges under the same lock, so it yields while the
    nightly holds it. A lease held elsewhere is waited for, up to
    *wait_seconds*, then refused.
    """
    from kew.graph.driver.merge_queue import merge_queue_lock, merge_queue_lock_path

    path = merge_queue_lock_path(config)
    waited = 0.0
    while True:
        with merge_queue_lock(path) as lease:
            if lease.acquired:
                yield
                return
            reason = lease.reason
        if waited >= wait_seconds:
            raise NightlyBusy(f"the verify lease stayed held: {reason}")
        sleep(poll_seconds)
        waited += poll_seconds


@dataclass(frozen=True)
class ScanReport:
    """What the nightly's image scan found and filed."""

    #: The advisories filed this run, each with its issue.
    advisories: dict[str, int] = field(default_factory=dict)
    #: Why the scan failed other than by an advisory it could file.
    error: str | None = None
    #: The issue that tracks that failure, and whether it was filed before.
    issue: int | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class NightlyReport:
    """What one nightly did."""

    candidate: VerifyCandidate
    result: VerifyResult
    health: MainHealth
    regression: Regression | None = None
    scan: ScanReport = field(default_factory=ScanReport)

    @property
    def verdict(self) -> str:
        """``passed``; ``red`` for any failure of a job that really ran its
        gates, failing test ids or not; else ``incomplete`` — no exit, a kill,
        a broken host environment or a refused preflight
        (:func:`kew.merge_verify.gate_ran`), which says nothing about main."""
        from kew.merge_verify import gate_ran

        outcome = self.result.outcome
        if outcome == "failed" and gate_ran(self.result):
            return RED
        return "passed" if outcome == "passed" else "incomplete"


def _fetch(repo_root: Path, base: str) -> None:
    try:
        read_git(
            repo_root,
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
        )
    except MainHealthError as exc:
        logger.warning(
            "kew verify nightly: fetch failed, verifying as fetched: %s", exc
        )


class _Host:
    """Runs one job at one main commit on the verify host."""

    def __init__(self, config: DispatchConfig, store: RunStore, repo_root: Path):
        self.config, self.store, self.repo_root = config, store, repo_root

    def run(self, sha: str, *, name: str, script: str, timeout: float) -> Any:
        from kew.merge_verify import run_host_job

        return run_host_job(
            self.config,
            store=self.store,
            repo_root=self.repo_root,
            sha=sha,
            name=name,
            script=script,
            timeout_seconds=timeout,
        )

    def probe(self, tests: Sequence[str], *, at_tip: bool = False) -> Probe:
        def fails(sha: str) -> bool | None:
            result = self.run(
                sha,
                name="main-bisect",
                script=probe_script(tests, at_tip=at_tip),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            logger.info("bisect probe %s: exit %s", sha[:12], result.exit_code)
            return probe_verdict(result.exit_code, at_tip=at_tip)

        return fails


def pin_regression(
    store: RunStore,
    repo_root: Path,
    *,
    sha: str,
    tests: tuple[str, ...],
    base_ref: str,
    probe: Callable[..., Probe],
) -> Regression:
    """Bisect *sha*'s failing *tests* back to the first main commit they fail.

    The red commit itself is probed first, with the failing tests alone. When
    none fails there, the red did not reproduce — a flake, or an interaction
    with the rest of the suite — and nothing is pinned: walking the window
    would only end at the red commit and name its PR for a failure the tests
    alone never showed. So the whole walk takes at most
    ``probe_budget(len(window)) + 1`` probes. *probe* builds the probe of
    some tests, ``at_tip=True`` for that first one.
    """
    if not tests:
        return Regression(sha, tests, reason="the log names no failing test")
    verdict = probe(tests, at_tip=True)(sha)
    if verdict is None:
        reason = f"the probe of the failing tests alone at {sha[:12]} ran no verdict"
        return Regression(sha, tests, probes=(sha,), reason=reason)
    if not verdict:
        return Regression(sha, tests, probes=(sha,), reproduced=NOT_REPRODUCED)
    reproduced = Regression(sha, tests, probes=(sha,), reproduced=REPRODUCED)
    return _walk(store, repo_root, reproduced, base_ref=base_ref, fails=probe(tests))


def _walk(
    store: RunStore,
    repo_root: Path,
    regression: Regression,
    *,
    base_ref: str,
    fails: Probe,
) -> Regression:
    """Bisect a red that reproduced at its own sha, back to the last green."""
    sha, tests = regression.sha, regression.tests
    commits = main_commits(repo_root, base_ref)
    shas = [c.sha for c in commits]
    green = last_green(store, commits)
    if green is None or sha not in shas or shas.index(green) <= shas.index(sha):
        return replace(regression, reason="no green full verify of main before it")
    window = list(reversed(shas[shas.index(sha) : shas.index(green)]))
    found = bisect(window, fails)
    probes = (*regression.probes, *found.probes)
    if found.culprit is None:
        return replace(regression, probes=probes, reason=found.reason)
    subject = next(c.subject for c in commits if c.sha == found.culprit)
    return replace(
        regression,
        culprit=found.culprit,
        pr=pr_of(subject),
        inside_selection=inside_selection(repo_root, found.culprit, tests),
        probes=probes,
    )


def scan_script() -> str:
    """The nightly's image scan: every agent image, from ``kew/``."""
    return "set -eu\numask 022\ncd kew\nbash ../scripts/scan-agent-image.sh\n"


def run_nightly(
    config: DispatchConfig,
    *,
    store: RunStore,
    repo_root: Path,
    gh: Runner = subprocess_utils.run,
    wait_seconds: float = 3 * 3600,
    sleep: Callable[[float], None] = time.sleep,
) -> NightlyReport:
    """Verify ``origin/main`` in full, holding the verify lease the whole run.

    Records ``verify_completed`` with ``suite: full, kind: nightly``. A red
    verdict bisects and files the regression issue, once per sha; the image
    scan files one issue per new CVE.
    """
    from kew.merge_verify import run_verify

    base = config.branches.base or "main"
    repo = config.project.repo
    host = _Host(config, store, repo_root)
    with verify_lease(config, wait_seconds=wait_seconds, sleep=sleep):
        _fetch(repo_root, base)
        base_ref = base_ref_for(repo_root, base)
        sha = read_git(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        candidate, result = run_verify(
            config,
            store=store,
            ref=sha,
            repo_root=repo_root,
            base_sha=sha,
            kind=NIGHTLY_KIND,
            require_receipt=True,
        )
        regression = None
        if NightlyReport(candidate, result, MainHealth()).verdict == RED:
            regression = _regression(host, log=result.log, sha=sha, base_ref=base_ref)
            if repo:
                regression = file_regression(store, regression, repo=repo, gh=gh)
        scan = _scan(host, store, sha=sha, repo=repo, gh=gh)
        health = read_main_health(store, repo_root, base_ref=base_ref)
    return NightlyReport(candidate, result, health, regression, scan)


def _regression(host: _Host, *, log: str, sha: str, base_ref: str) -> Regression:
    """Pin a red *sha*, unless its issue is filed: a later nightly of the same
    red commit files no duplicate and runs no second bisect.

    A red that named no failing test bisects nothing: its issue names the gate
    that failed and carries the log's last lines (:func:`failing_gate`).
    """
    tests = failing_tests(log)
    filed = filed_regression(host.store, sha)
    if filed is not None:
        return Regression(sha, tests, issue=filed, duplicate=True)
    if not tests:
        failure = failing_gate(log)
        return Regression(
            sha,
            tests,
            reason="the failure names no failing test, so nothing can bisect it",
            gate=failure.gate,
            tail=failure.tail,
            tail_stream=failure.stream,
        )
    regression = pin_regression(
        host.store,
        host.repo_root,
        sha=sha,
        tests=tests,
        base_ref=base_ref,
        probe=host.probe,
    )
    if regression.probes:
        record_tip_probe(host.store, regression, repo=host.config.project.repo or None)
    return regression


def _scan(
    host: _Host, store: RunStore, *, sha: str, repo: str, gh: Runner
) -> ScanReport:
    """Scan every image at *sha*; file an issue per new advisory, and one per
    sha for every other way the scan failed (:mod:`kew.nightly_scan`)."""
    result = host.run(
        sha, name="image-scan", script=scan_script(), timeout=SCAN_TIMEOUT_SECONDS
    )
    found = read_scan(result.log, result.exit_code, result.error)
    filed = {}
    if found.advisories and repo:
        filed = file_advisories(store, found.advisories, sha=sha, repo=repo, gh=gh)
    if not found.failures:
        return ScanReport(filed)
    error = "; ".join(found.failures)
    logger.warning("kew verify nightly: the image scan failed: %s", error)
    issue, duplicate = file_scan_failure(
        store, sha=sha, reason=error, repo=repo or None, gh=gh
    )
    return ScanReport(filed, error, issue, duplicate)


def nightly_summary(report: NightlyReport) -> list[str]:
    """What the nightly prints, one line each."""
    lines = [report.health.describe()]
    if report.regression is not None:
        lines += _regression_lines(report.regression)
    return lines + _scan_lines(report.scan)


def _regression_lines(regression: Regression) -> list[str]:
    lines = []
    if regression.issue is not None:
        verb = "already filed as" if regression.duplicate else "filed"
        lines.append(f"regression {verb} issue #{regression.issue}")
    if regression.reproduced == NOT_REPRODUCED:
        lines.append(f"the failing tests {NOT_REPRODUCED_TEXT}; nothing pinned")
    if regression.culprit is not None:
        pr = "" if regression.pr is None else f" (PR #{regression.pr})"
        lines.append(f"pinned to {regression.culprit[:12]}{pr}")
    if not regression.tests and regression.gate is not None:
        lines.append(f"failing gate: {regression.gate}; no test id to bisect")
    return lines


def _scan_lines(scan: ScanReport) -> list[str]:
    lines = [
        f"{found} filed as issue #{number}" for found, number in scan.advisories.items()
    ]
    if scan.error:
        tracked = ""
        if scan.issue is not None:
            verb = "already filed as" if scan.duplicate else "filed as"
            tracked = f" ({verb} issue #{scan.issue})"
        lines.append(f"image scan failed: {scan.error}{tracked}")
    return lines
