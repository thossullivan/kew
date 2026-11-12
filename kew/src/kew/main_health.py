"""Main's health: the nightly full verify, the red-main gate, and the bisect (#3195).

Main's health is read off the audit trail, with no table of its own. The newest
of these about the newest commit on main's first-parent history that has one
decides it:

* a ``verify_completed`` of the full suite with ``kind: nightly`` — what
  ``kew verify nightly`` records for ``origin/main``;
* a ``verify_completed`` of the full suite for a PR whose candidate landed
  unchanged — the candidate itself, or a main commit on the verified base
  carrying the candidate's tree (a squash merge of exactly what was verified);
* a ``main_health_cleared`` an operator wrote with
  ``kew verify main-health clear <sha> --issue N --reason …``.

A verification that reached no verdict says nothing about main and is skipped.

While main is red every PR verify runs the full suite and the verdict names the
red commit and its issue: an unrelated PR fails, because main's failing test
still fails in it, and a PR that fixes it passes, and reads main green once it
lands. Both the drive and the operator scripts merge through ``kew verify``, so
this one rule blocks both.

A red nightly (:mod:`kew.nightly`) first runs the failing test ids alone at
the red commit. When none of them fails there, the red did not reproduce (a
flake, or an interaction with the rest of the suite): its issue says so, names
no PR, and nothing bisects. When one does, it walks main's first-parent commits
since the last green full verify by binary search, running only those tests at
each probe; the first commit where they fail names the PR. It files one
``regression`` issue per red main commit, deduplicated by sha, one issue per
new CVE the nightly image scan's gate finds, deduplicated by CVE id, and one
per sha whose scan failed without naming a CVE.

How a red clears depends on that first probe. A red that reproduced is a real
break: a landed PR whose full verify passed with the failing tests still in its
tree clears it. Any other red (one that did not reproduce, one no probe reached
a verdict on, one whose log named no failing test) clears only through
``kew verify main-health clear`` or a newer green nightly; a landed PR never
clears it.
"""

from __future__ import annotations

import logging
import math
import re
import shlex
import subprocess  # nosec B404 — only for its exception types; runs go through subprocess_utils
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import subprocess_utils
from kew.audit import emit, resolve_actor
from kew.red_main_check import PASSED
from kew.verify_ledger import FILEABLE_ID, summary_text

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, RunStore

logger = logging.getLogger(__name__)

#: The ``kind`` a ``verify_completed`` payload carries: a PR candidate's
#: verification, or the nightly's verification of main itself.
PR_KIND = "pr"
NIGHTLY_KIND = "nightly"

#: The ``suite`` a ``verify_completed`` payload carries. Only a verification
#: of the whole suite says anything about main: while main is red, one that ran
#: less could not tell a fix from a PR that never ran main's failing test.
SUITE_FULL = "full"

#: The ``suite`` of a PR verify that ran only the test files its change selects
#: (``[verify] suite = "tiered"``, #3202). Health never reads one.
SUITE_SELECTED = "selected"

#: The ``frontend_selection.mode`` of a PR verify that ran only the vitest
#: files and ui-check modules its change owns (``[verify] frontend =
#: "scoped"``, #3213). Like a selected suite, health never reads one: the full
#: vitest and ui-check never ran on it. Only ``full`` is evidence.
FRONTEND_FULL = "full"
FRONTEND_SCOPED = "scoped"

#: The audit event an operator's clear writes: main reads green at that sha.
CLEARED_EVENT = "main_health_cleared"

#: The audit event a filed regression issue writes, deduplicated by sha.
REGRESSION_EVENT = "main_regression_filed"

#: The audit event a filed CVE issue writes, deduplicated by CVE id.
CVE_EVENT = "image_cve_filed"

#: The audit event an image scan that failed without its gate naming a CVE
#: writes; its issue is filed once per sha.
SCAN_FAILED_EVENT = "image_scan_failed"

#: The audit event the nightly writes after running a red commit's failing
#: tests alone there: whether the red reproduced.
TIP_PROBE_EVENT = "main_red_tip_probed"

#: What that first probe found: a failing test failed alone at the red commit,
#: or none did. A probe with no verdict records neither.
REPRODUCED = "reproduced"
NOT_REPRODUCED = "not_reproduced"

#: The label every regression issue carries; created when the repo lacks it.
REGRESSION_LABEL = "regression"

#: A nightly older than this reads stale: the nightly runs once a day, so a
#: day and two hours is a nightly that did not run.
STALE_AFTER = timedelta(hours=26)

#: How many of main's first-parent commits health and the bisect read at most.
MAIN_WINDOW = 500

#: How many audit rows of each kind health reads, newest first.
HEALTH_EVENTS = 500

#: How long one probe of the bisect may run.
PROBE_TIMEOUT_SECONDS = 1800

#: How long the image scan job may run.
SCAN_TIMEOUT_SECONDS = 3600

#: The exit the nightly answers when the verify lease stayed held all along.
NIGHTLY_BUSY_EXIT = 2

GREEN = "green"
RED = "red"
UNKNOWN = "unknown"

#: A pytest short-summary line. The id runs to `` - `` or the end of the line:
#: a parametrized id can hold spaces (``test_x[3-has a service account]``).
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py\b.*)$", re.M)
_NODE_ID = re.compile(r"^\S+?\.py(?:::\S.*)?$")
#: A test file pytest could not collect, as its ``ERROR`` line names it: the
#: path charset of :data:`~kew.verify_ledger.FILEABLE_ID`, with no ``::``.
_BARE_TEST_FILE = re.compile(r"[A-Za-z0-9_./-]+\.py")
_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_PR = re.compile(r"^Merge pull request #(\d+)\b")
_ISSUE_URL = re.compile(r"/issues/(\d+)\s*$")

#: Runs one command for the forge (``gh``); the process boundary tests fake.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class MainHealthError(RuntimeError):
    """Main's health could not be read or written."""


@dataclass(frozen=True)
class MainHealth:
    """What main's newest full verify says, and where it came from."""

    state: str = UNKNOWN
    #: The main commit the deciding record is about.
    sha: str | None = None
    #: How it was decided: ``nightly``, ``pr`` or ``clear``.
    via: str | None = None
    #: The regression issue filed for a red sha, when one was.
    issue: int | None = None
    #: The test ids that failed, for a red sha.
    tests: tuple[str, ...] = ()
    recorded_at: str | None = None
    #: The newest nightly is older than :data:`STALE_AFTER`, or there is none.
    stale: bool = False
    #: For a red sha: :data:`REPRODUCED`, :data:`NOT_REPRODUCED`, or ``None``
    #: when no probe reached a verdict there.
    reproduced: str | None = None
    #: For a green read: the red sha a landed PR cleared, the main commit it
    #: landed as, and its PR number.
    cleared_red: str | None = None
    cleared_by: str | None = None
    cleared_pr: int | None = None

    @property
    def red(self) -> bool:
        return self.state == RED

    def describe(self) -> str:
        """One line for an operator."""
        if self.sha is None:
            return "main health is unknown: no full verify of main is on record"
        issue = "" if self.issue is None else f" (issue #{self.issue})"
        tests = "" if not self.tests else f"; failing: {', '.join(self.tests[:5])}"
        stale = "; the nightly is stale" if self.stale else ""
        return (
            f"main is {self.state} at {self.sha[:12]}{issue}, "
            f"from {self.via} at {self.recorded_at}{tests}{self._clearing()}{stale}"
        )

    def _clearing(self) -> str:
        if self.cleared_red is not None and self.cleared_by is not None:
            pr = "" if self.cleared_pr is None else f" (PR #{self.cleared_pr})"
            return (
                f"; the red at {self.cleared_red[:12]} was cleared by "
                f"{self.cleared_by[:12]}{pr}"
            )
        if not self.red or self.reproduced == REPRODUCED:
            return ""
        why = (
            "it did not reproduce alone (flake or interaction)"
            if self.reproduced == NOT_REPRODUCED
            else "kew could not reproduce it"
        )
        return (
            f"; {why}, so no landed PR clears it: only `kew verify main-health "
            "clear` or a newer green nightly"
        )

    def payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "sha": self.sha,
            "via": self.via,
            "issue": self.issue,
            "tests": list(self.tests),
            "reproduced": self.reproduced,
        }


# ── reading main ────────────────────────────────────────────────────────────


def read_git(repo_root: Path, *args: str) -> str:
    try:
        done = subprocess_utils.run(["git", *args], cwd=repo_root, check=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # ValueError: ``git`` is not on the allowlist (no preflight registered
        # it), which is a command that cannot run, never a traceback.
        detail = getattr(exc, "stderr", "") or str(exc)
        raise MainHealthError(f"git {' '.join(args)}: {detail.strip()}") from exc
    return (done.stdout or "").strip()


@dataclass(frozen=True)
class MainCommit:
    sha: str
    tree: str
    parent: str | None
    subject: str


def main_commits(repo_root: Path, base_ref: str) -> list[MainCommit]:
    """Main's first-parent commits, newest first, at most :data:`MAIN_WINDOW`."""
    out = read_git(
        repo_root,
        "log",
        "--first-parent",
        f"--max-count={MAIN_WINDOW}",
        "--format=%H %T %P%x00%s",
        base_ref,
        "--",
    )
    commits = []
    for line in out.splitlines():
        head, _, subject = line.partition("\x00")
        sha, tree, *parents = head.split()
        commits.append(MainCommit(sha, tree, parents[0] if parents else None, subject))
    return commits


def base_ref_for(repo_root: Path, base: str) -> str:
    """``origin/<base>`` when this clone has it, else ``<base>``."""
    from kew.merge_verify import VerifyRefused, _resolve_base_ref

    try:
        return _resolve_base_ref(repo_root, base)
    except VerifyRefused as exc:
        raise MainHealthError(str(exc)) from exc


# ── health ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Record:
    event_id: int
    sha: str
    state: str
    via: str
    recorded_at: str
    tests: tuple[str, ...] = ()
    #: For a PR verification: what its red-main phase found for each failing
    #: id of the red main it ran under (:mod:`kew.red_main_check`).
    red_outcomes: tuple[tuple[str, str], ...] = ()


def _payload(event: AuditEvent) -> dict[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, dict) else {}


def _landed(payload: dict[str, Any], commits: Sequence[MainCommit]) -> str | None:
    """The main commit a PR verification's candidate landed as, unchanged."""
    ref_sha = payload.get("ref_sha")
    on_main = {c.sha for c in commits}
    if ref_sha in on_main:
        return str(ref_sha)
    tree, base = payload.get("ref_tree"), payload.get("base_sha")
    if not tree or not base:
        return None
    for commit in commits:
        if commit.parent == base and commit.tree == tree:
            return commit.sha
    return None


def _verify_record(event: AuditEvent, commits: Sequence[MainCommit]) -> _Record | None:
    payload = _payload(event)
    outcome = payload.get("outcome")
    if payload.get("suite") != SUITE_FULL or outcome not in ("passed", "failed"):
        return None
    if _frontend_scoped(payload):
        return None
    kind = payload.get("kind")
    sha = _record_sha(payload, commits)
    if sha is None:
        return None
    tests = tuple(str(test) for test in payload.get("failing_tests") or ())
    if outcome == "failed" and payload.get("gate_ran") is False:
        # The job never really ran its gates (a kill, an OOM, a broken host
        # environment, a refused preflight): incomplete, never red. Any other
        # failure is red, with or without failing test ids.
        return None
    return _Record(
        event_id=event.id,
        sha=sha,
        state=GREEN if outcome == "passed" else RED,
        via=str(kind),
        recorded_at=event.timestamp,
        tests=tests,
        red_outcomes=_red_outcomes(payload.get("red_tests")),
    )


def _frontend_scoped(payload: dict[str, Any]) -> bool:
    """Whether the verify ran a scoped frontend (:data:`FRONTEND_SCOPED`). An
    unreadable selection reads as scoped: it is no evidence either."""
    selection = payload.get("frontend_selection")
    if selection is None:
        return False
    if not isinstance(selection, dict):
        return True
    return selection.get("mode") != FRONTEND_FULL


def _record_sha(payload: dict[str, Any], commits: Sequence[MainCommit]) -> str | None:
    """The main commit a verification is about: a nightly's ref, or the
    commit a PR's candidate landed as."""
    kind = payload.get("kind")
    if kind == NIGHTLY_KIND:
        sha = payload.get("ref_sha")
    elif kind == PR_KIND:
        sha = _landed(payload, commits)
    else:
        return None
    return sha if isinstance(sha, str) and sha else None


def _red_outcomes(found: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(found, dict):
        return ()
    return tuple((str(test), str(outcome)) for test, outcome in found.items())


def _clear_record(event: AuditEvent) -> _Record | None:
    sha = _payload(event).get("sha")
    if not isinstance(sha, str) or not sha:
        return None
    return _Record(event.id, sha, GREEN, "clear", event.timestamp)


def _records(store: RunStore, commits: Sequence[MainCommit]) -> Iterator[_Record]:
    """The deciding record of each main commit, newest commit first.

    Ordered by main's history, not by when the record was written: a later
    re-verify of an older main commit says nothing about main's tip. At one
    commit the newest nightly or clear decides, and a PR verification only
    where neither exists: a PR verify that re-ran a red main commit and passed
    is not a clear.
    """
    from kew.merge_verify import VERIFY_EVENT

    events = [
        *store.list_audit_events(HEALTH_EVENTS, event=VERIFY_EVENT),
        *store.list_audit_events(HEALTH_EVENTS, event=CLEARED_EVENT),
    ]
    newest: dict[str, _Record] = {}
    newest_pr: dict[str, _Record] = {}
    for event in sorted(events, key=lambda e: e.id, reverse=True):
        if event.event == CLEARED_EVENT:
            record = _clear_record(event)
        else:
            record = _verify_record(event, commits)
        if record is not None:
            chosen = newest_pr if record.via == PR_KIND else newest
            chosen.setdefault(record.sha, record)
    for commit in commits:
        record = newest.get(commit.sha) or newest_pr.get(commit.sha)
        if record is not None:
            yield record


def red_shas(store: RunStore, commits: Sequence[MainCommit]) -> set[str]:
    """Every main commit a full verify on record read red."""
    from kew.merge_verify import VERIFY_EVENT

    reds = set()
    for event in store.list_audit_events(HEALTH_EVENTS, event=VERIFY_EVENT):
        record = _verify_record(event, commits)
        if record is not None and record.state == RED:
            reds.add(record.sha)
    return reds


def reproduction(store: RunStore, sha: str) -> str | None:
    """What the newest probe of red *sha*'s failing tests alone found."""
    for event in store.list_audit_events(HEALTH_EVENTS, event=TIP_PROBE_EVENT):
        payload = _payload(event)
        if payload.get("sha") == sha:
            found = payload.get("reproduced")
            return found if found in (REPRODUCED, NOT_REPRODUCED) else None
    return None


def _clearing_landing(
    store: RunStore, red: _Record, landed: Sequence[_Record]
) -> _Record | None:
    """The oldest of *landed* (newest first) that clears *red*, or ``None``.

    Only a red that reproduced alone is a real break a landed PR can clear,
    and only a landing whose own verify ran each failing node id — exactly,
    class and parameters included — and saw it pass. A dropped parameter, a
    deleted class, a skip or an xfail is not a pass, and a verify that
    recorded no outcomes for those ids is no evidence: it clears nothing.
    """
    if not red.tests or reproduction(store, red.sha) != REPRODUCED:
        return None
    for record in reversed(landed):
        outcomes = dict(record.red_outcomes)
        if all(outcomes.get(test) == PASSED for test in red.tests):
            return record
    return None


def filed_regression(store: RunStore, sha: str) -> int | None:
    """The regression issue filed for *sha*, or ``None``."""
    for event in store.list_audit_events(HEALTH_EVENTS, event=REGRESSION_EVENT):
        payload = _payload(event)
        if payload.get("sha") == sha and isinstance(payload.get("issue"), int):
            return int(payload["issue"])
    return None


def _stale(store: RunStore, now: datetime) -> bool:
    from kew.merge_verify import VERIFY_EVENT

    for event in store.list_audit_events(HEALTH_EVENTS, event=VERIFY_EVENT):
        if _payload(event).get("kind") == NIGHTLY_KIND:
            return now - _timestamp(event.timestamp) > STALE_AFTER
    return True


def _timestamp(text: str) -> datetime:
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def read_main_health(
    store: RunStore | None,
    repo_root: Path,
    *,
    base_ref: str,
    now: datetime | None = None,
) -> MainHealth:
    """Main's health: the newest full verify of a main commit, or a clear.

    A landed PR's green reads past an older red only when it clears that red
    (:func:`_clearing_landing`); otherwise the red still decides.
    """
    if store is None or not hasattr(store, "list_audit_events"):
        return MainHealth()
    commits = main_commits(repo_root, base_ref)
    stale = _stale(store, now or datetime.now(UTC))
    landed: list[_Record] = []
    for record in _records(store, commits):
        if record.state == GREEN and record.via == PR_KIND:
            landed.append(record)
        elif record.state == GREEN:
            return _green(landed[0] if landed else record, stale)
        else:
            return _behind_landings(store, commits, record, landed, stale)
    return _green(landed[0], stale) if landed else MainHealth(stale=stale)


def _behind_landings(
    store: RunStore,
    commits: Sequence[MainCommit],
    red: _Record,
    landed: Sequence[_Record],
    stale: bool,
) -> MainHealth:
    """Main's health when *red* is the newest record behind PR landings."""
    clearing = _clearing_landing(store, red, landed)
    if clearing is None:
        return _red(store, red, stale)
    commit = next(c for c in commits if c.sha == clearing.sha)
    return replace(
        _green(landed[0], stale),
        cleared_red=red.sha,
        cleared_by=clearing.sha,
        cleared_pr=pr_of(commit.subject),
    )


def _green(record: _Record, stale: bool) -> MainHealth:
    return MainHealth(
        state=GREEN,
        sha=record.sha,
        via=record.via,
        recorded_at=record.recorded_at,
        stale=stale,
    )


def _red(store: RunStore, record: _Record, stale: bool) -> MainHealth:
    return MainHealth(
        state=RED,
        sha=record.sha,
        via=record.via,
        issue=filed_regression(store, record.sha),
        tests=record.tests,
        recorded_at=record.recorded_at,
        stale=stale,
        reproduced=reproduction(store, record.sha),
    )


def newest_green_nightly(
    store: RunStore | None, repo_root: Path, *, base_ref: str
) -> tuple[str, datetime] | None:
    """The main commit the newest green nightly on record is about, and when
    it was recorded; ``None`` when there is none.

    Only a nightly counts. A PR's full verify that landed unchanged is still
    evidence of main's health, but it is no nightly: when the nightly stops,
    nothing bisects or pins a failure a selection let through. A clear is not
    a verify, and a nightly of a commit not on main's first-parent history is
    about something else.
    """
    from kew.merge_verify import VERIFY_EVENT

    if store is None or not hasattr(store, "list_audit_events"):
        return None
    commits = main_commits(repo_root, base_ref)
    on_main = {commit.sha for commit in commits}
    greens = [
        (_timestamp(record.recorded_at), record.sha)
        for event in store.list_audit_events(HEALTH_EVENTS, event=VERIFY_EVENT)
        if (record := _verify_record(event, commits)) is not None
        and record.state == GREEN
        and record.via == NIGHTLY_KIND
        and record.sha in on_main
    ]
    if not greens:
        return None
    at, sha = max(greens)
    return sha, at


def last_green(store: RunStore, commits: Sequence[MainCommit]) -> str | None:
    """The newest main commit a full verify or a clear read green."""
    return next((r.sha for r in _records(store, commits) if r.state == GREEN), None)


def clear(
    store: RunStore,
    repo_root: Path,
    *,
    sha: str,
    issue: int,
    reason: str,
    base_ref: str,
    repo: str | None = None,
) -> int:
    """Read main green at *sha*: one audited event with the actor and reason.

    The one way a flake clears, besides a newer green nightly. Never
    automatic: an operator names a commit a full verify read red, the issue
    that tracks it and why.
    """
    if not reason.strip():
        raise MainHealthError("a clear needs a --reason")
    full = read_git(repo_root, "rev-parse", "--verify", f"{sha}^{{commit}}")
    commits = main_commits(repo_root, base_ref)
    if full not in {c.sha for c in commits}:
        raise MainHealthError(f"{sha} is not a commit on {base_ref}")
    if full not in red_shas(store, commits):
        raise MainHealthError(
            f"{sha} is not a red main commit: no full verify on record read it red"
        )
    actor = resolve_actor()
    row = emit(
        store,
        CLEARED_EVENT,
        actor=actor,
        repo=repo,
        issue_num=issue,
        payload={
            "sha": full,
            "issue": issue,
            "reason": reason.strip(),
            "actor": actor.id,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
        strict=True,
    )
    if row is None:
        raise MainHealthError("the clear produced no durable audit row")
    return row


# ── the bisect ──────────────────────────────────────────────────────────────


def failing_tests(log: str) -> tuple[str, ...]:
    """The test ids a pytest short summary in *log* names, from ``kew/``.

    Only the ``short test summary info`` block is read (:func:`summary_text`):
    a ``FAILED`` line a test printed outside it is not a failing id, and a log
    whose summary shape is ambiguous names none at all.
    """
    seen: dict[str, None] = {}
    for match in _FAILED_LINE.finditer(summary_text(log)):
        node = _node_id(match.group(1).rstrip())
        if node is not None:
            seen.setdefault(node.removeprefix("kew/"), None)
    return tuple(seen)


def _node_id(text: str) -> str | None:
    """The node id at the start of a summary line's text.

    It runs to the first `` - `` outside its brackets, or the end of the line:
    a parametrized id can hold spaces and even `` - `` inside its brackets
    (``test_x[a - b]``), and only the reason after it is not the id.
    """
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        elif depth == 0 and text.startswith(" - ", index):
            text = text[:index]
            break
    return text if _NODE_ID.match(text) else None


#: Answers whether the tests fail at one commit; ``None`` when it reached no
#: verdict.
Probe = Callable[[str], "bool | None"]


@dataclass(frozen=True)
class Bisect:
    """What the bisect found, and how many probes it took."""

    culprit: str | None
    probes: tuple[str, ...]
    reason: str | None = None


def bisect(commits: Sequence[str], fails: Probe) -> Bisect:
    """The first of *commits* (oldest first) where *fails* answers ``True``.

    The commit before the first is known green and the last is known red —
    the caller has seen it fail first (:func:`kew.nightly.pin_regression`
    probes it before anything bisects) — so the walk probes at most
    ``ceil(log2(len(commits)))`` more commits.
    """
    if not commits:
        return Bisect(None, (), "no commit between the green and the red one")
    low, high = -1, len(commits) - 1
    probed: list[str] = []
    while high - low > 1:
        middle = (low + high) // 2
        probed.append(commits[middle])
        verdict = fails(commits[middle])
        if verdict is None:
            return Bisect(
                None,
                tuple(probed),
                f"the probe at {commits[middle][:12]} ran no verdict",
            )
        if verdict:
            high = middle
        else:
            low = middle
    return Bisect(commits[high], tuple(probed))


def probe_budget(commits: int) -> int:
    """The most probes :func:`bisect` runs over *commits* commits."""
    return math.ceil(math.log2(commits)) if commits > 1 else 0


def probe_script(tests: Sequence[str], *, at_tip: bool = False) -> str:
    """The job one probe runs: only *tests*, each on its own, from ``kew/``.

    One invocation over every id exits 4 as soon as one id does not exist yet
    at that commit, without running the rest, so a test that exists and fails
    there read as green. Each id runs alone; the job exits 1 when any failed,
    0 when none ran into anything but "not there yet", and otherwise with the
    first exit that is no verdict.

    *at_tip* is the probe of the red commit itself, where every id was just
    seen failing: one that is not there, or ran nothing, is no verdict rather
    than a pass.
    """
    ids = " ".join(shlex.quote(test) for test in tests)
    absent = "0)" if at_tip else "0|4|5)"
    return (
        "\n".join(
            [
                "set -eu",
                "umask 022",
                "cd kew",
                "uv sync --quiet",
                "verdict=0",
                f"for test in {ids}; do",
                "  status=0",
                '  uv run pytest -p no:cacheprovider -q "$test" || status=$?',
                '  case "$status" in',
                f"    {absent} ;;",
                "    1) verdict=1 ;;",
                '    *) if [ "$verdict" -eq 0 ]; then verdict=$status; fi ;;',
                "  esac",
                "done",
                'exit "$verdict"',
            ]
        )
        + "\n"
    )


def probe_verdict(exit_code: int | None, *, at_tip: bool = False) -> bool | None:
    """A probe's exit, read as "the tests fail here".

    Exit 1 is a failure. 0, and 4 or 5 — the tests do not exist yet at that
    commit — are not. Anything else, or no exit at all, is no verdict. At the
    red commit itself (*at_tip*), 4 or 5 is no verdict too: those tests just
    failed there, so not finding them is not a pass.
    """
    if exit_code == 1:
        return True
    passes = (0,) if at_tip else (0, 4, 5)
    return False if exit_code in passes else None


def pr_of(subject: str) -> int | None:
    """The PR a main commit's subject names, squash or merge commit."""
    match = _PR_SUFFIX.search(subject) or _MERGE_PR.match(subject)
    return int(match.group(1)) if match else None


def inside_selection(repo_root: Path, sha: str, tests: Sequence[str]) -> bool | None:
    """Whether the T1 selection of *sha*'s own change runs every one of *tests*.

    Inside means a flake or an interaction; outside means the selector missed
    a test the change breaks. ``None`` when the selection could not be read.
    """
    from kew import verify_selection

    try:
        selection = verify_selection.explain(
            repo_root, sha, gate_re=verify_selection.gate_pattern()
        )
    except verify_selection.SelectionError as exc:
        logger.warning("could not read the selection of %s: %s", sha[:12], exc)
        return None
    if selection.full_rule is not None:
        return True
    paths = [path for path, _why in selection.selected]
    return all(_selected(test, paths) for test in tests)


def _selected(test: str, paths: Sequence[str]) -> bool:
    file = test.split("::", 1)[0]
    return any(path.endswith(file) or file.endswith(path) for path in paths)


# ── issues ──────────────────────────────────────────────────────────────────


def _issue_number(stdout: str) -> int | None:
    match = _ISSUE_URL.search(stdout.strip())
    return int(match.group(1)) if match else None


def _gh(gh: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """One ``gh`` call; a ``gh`` that cannot run is a :class:`MainHealthError`."""
    try:
        return gh(argv, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MainHealthError(f"{' '.join(argv[:3])} could not run: {exc}") from exc


def _ensure_label(gh: Runner, repo: str) -> None:
    """Create the ``regression`` label; one that already exists is fine."""
    _gh(
        gh,
        ["gh", "label", "create", REGRESSION_LABEL, "--repo", repo]
        + ["--color", "B60205", "--description", "main went red (kew verify nightly)"],
    )


def create_issue(
    gh: Runner, repo: str, *, title: str, body: str, labels: Sequence[str] = ()
) -> int:
    argv = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        argv += ["--label", label]
    done = _gh(gh, argv)
    number = _issue_number(done.stdout or "") if done.returncode == 0 else None
    if number is None:
        detail = (done.stderr or done.stdout or "").strip()
        raise MainHealthError(f"gh issue create exited {done.returncode}: {detail}")
    return number


@dataclass(frozen=True)
class Regression:
    """A red main commit, what broke it, and the issue that tracks it."""

    sha: str
    tests: tuple[str, ...]
    culprit: str | None = None
    pr: int | None = None
    inside_selection: bool | None = None
    probes: tuple[str, ...] = ()
    reason: str | None = None
    issue: int | None = None
    duplicate: bool = False
    #: What running the failing tests alone at *sha* found
    #: (:data:`REPRODUCED`, :data:`NOT_REPRODUCED`), or ``None`` for no verdict.
    reproduced: str | None = None
    #: For a red that named no failing test: the gate that failed, as far as
    #: the log shows it (:func:`failing_gate`), and the log's last lines,
    #: redacted (:func:`log_tail`).
    gate: str | None = None
    tail: tuple[str, ...] = ()
    #: Which stream :attr:`tail` was taken from.
    tail_stream: str | None = None


#: What a regression issue says when the failing tests passed alone at the red
#: commit.
NOT_REPRODUCED_TEXT = "did not reproduce alone (flake or interaction)"


def code_span(text: str) -> str:
    """*text* as Markdown inline code: fenced by one backtick more than its
    longest backtick run, so nothing in it — a mention, an image — renders
    as markup on the forge. A plain id reads `` `id` ``, as before."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def regression_body(regression: Regression) -> str:
    # A failing id is read off the log, so it is fenced, never trusted as text.
    tests = "\n".join(f"- {code_span(test)}" for test in regression.tests)
    tests = tests or "- (none named)"
    return "\n".join(
        [
            f"kew verify nightly read main red at {regression.sha}.",
            "",
            "**Failing tests**",
            tests,
            "",
            "**Pinned**",
            _pinned_text(regression),
            "",
            f"**Inside that change's selection:** {_selection_text(regression)}",
            "",
            *_gate_text(regression),
            "Every PR verify runs the full suite and must pass while main is red. "
            + _clearing_text(regression),
        ]
    )


def _gate_text(regression: Regression) -> list[str]:
    if regression.tests or regression.gate is None:
        return []
    stream = regression.tail_stream or "the job's output"
    return [
        f"**Failing gate:** {regression.gate}",
        "",
        f"The last lines of {stream} (credentials, keys, internal host names "
        "and host paths redacted):",
        "",
        "~~~",
        *regression.tail,
        "~~~",
        "",
    ]


#: Signatures of the gates ``scripts/verify.sh`` and the job run that fail
#: without a pytest ``FAILED``/``ERROR`` line, the last one in a stream
#: winning: what a red with no failing test names as its failing gate. Each
#: matches a FAILURE only — a gate that passed early in the run (semgrep's
#: ``Ran 150 rules on 221 files: 0 findings.``) must not name a later one.
_GATE_SIGNATURES = (
    (re.compile(r"ImportError while loading conftest"), "pytest (a conftest import)"),
    (re.compile(r"Required test coverage of .* not reached"), "the coverage floor"),
    (re.compile(r"^Found \d+ errors? in \d+ files? \(checked", re.M), "mypy"),
    (re.compile(r"^Would reformat", re.M), "ruff format --check"),
    (re.compile(r"^Found \d+ errors?\.", re.M), "ruff check"),
    (re.compile(r"^ERROR:xenon:", re.M), "xenon"),
    (re.compile(r"^>> Issue: \[", re.M), "bandit"),
    (re.compile(r"Contracts: \d+ kept, [1-9]\d* broken"), "lint-imports"),
    (re.compile(r": [1-9]\d* findings?\b|\b[1-9]\d* blocking findings?\b"), "semgrep"),
    (re.compile(r"Found [1-9]\d* known vulnerabilit"), "pip-audit"),
    (re.compile(r"docs gate FAILED"), "the docs gate (check-docs.sh)"),
    (re.compile(r"svelte-check found [1-9]\d* error"), "svelte-check"),
    (re.compile(r"Test Files .*\d+ failed"), "vitest"),
    (re.compile(r"ui-check"), "the browser ui-check"),
)

#: What a red names when no signature matched.
UNNAMED_GATE = "a gate kew could not name (see the log)"

#: How many of a stream's last lines a regression issue carries, how long each
#: may be, and how many lines before them are cleaned with them.
_TAIL_LINES = 20
_TAIL_CHARS = 300
_TAIL_WINDOW = 400

#: Lines kew's own phases print, which say nothing about the gate that failed.
_KEW_LINES = re.compile(r"^(?:kew-held-out:|kew-red-main:|KEW_VERIFY_|KEW_HOST_)")


@dataclass(frozen=True)
class GateFailure:
    """The gate a red with no failing test came from, and what to quote."""

    gate: str
    #: Which stream :attr:`tail` is from: the job's output, stdout or stderr.
    stream: str
    tail: tuple[str, ...]


def _streams(log: str) -> list[tuple[str, str]]:
    """*log* as its streams, stdout first.

    A host that returns stderr after all of stdout marks where it starts
    (:data:`kew.hosts.gcp.STDERR_FOLLOWS`); a host that interleaves the two
    returns one stream.
    """
    from kew.hosts.gcp import STDERR_FOLLOWS

    stdout, found, stderr = log.rpartition(f"{STDERR_FOLLOWS}\n")
    if not found:
        return [("the job's output", log)]
    return [("the job's stdout", stdout), ("the job's stderr", stderr)]


def _last_gate(text: str) -> str | None:
    found = [
        (match.start(), name)
        for pattern, name in _GATE_SIGNATURES
        for match in pattern.finditer(text)
    ]
    return max(found)[1] if found else None


def failing_gate(log: str) -> GateFailure:
    """The gate a failure with no failing test id came from, as the log shows.

    Read stream by stream, stdout first: on a host that appends all of stderr
    after all of stdout, the last line in the log is not the last thing that
    happened, so a passing gate's stderr must not name a later failure, and
    the tail is taken from the stream the failing gate printed to.
    """
    streams = _streams(log)
    for stream, text in streams:
        gate = _last_gate(text)
        if gate is not None:
            return GateFailure(gate, stream, log_tail(text))
    stream, text = next(
        ((name, text) for name, text in streams if text.strip()), streams[0]
    )
    return GateFailure(UNNAMED_GATE, stream, log_tail(text))


def log_tail(text: str) -> tuple[str, ...]:
    """The last lines of *text*, cleaned for a public issue.

    The window before them is cleaned first, then sliced
    (:func:`kew.public_log.public_lines`): a PEM body whose ``BEGIN`` line is
    above the tail is still dropped.
    """
    from kew.public_log import public_lines

    window = [
        line
        for line in text.splitlines()[-_TAIL_WINDOW:]
        if line.strip() and not _KEW_LINES.match(line)
    ]
    cleaned = [line for line in public_lines(window, limit=_TAIL_CHARS) if line]
    return tuple(line.replace("~~~", "~ ~ ~") for line in cleaned[-_TAIL_LINES:])


def _pinned_text(regression: Regression) -> str:
    if regression.reproduced == NOT_REPRODUCED:
        return (
            f"Not pinned: the failing tests {NOT_REPRODUCED_TEXT} at "
            f"{regression.sha[:12]}, so no bisect ran and no PR is named."
        )
    if regression.culprit is None:
        return f"Not pinned: {regression.reason or 'no bisect ran'}."
    pr = "" if regression.pr is None else f" (PR #{regression.pr})"
    return (
        f"The first failing commit is {regression.culprit}{pr}, found in "
        f"{len(regression.probes)} probe(s) running only the failing tests."
    )


def _clearing_text(regression: Regression) -> str:
    command = (
        f"`kew verify main-health clear {regression.sha[:12]} --issue N --reason …`"
    )
    if regression.reproduced == REPRODUCED:
        return (
            "A landed PR whose full verify passes with these tests still in its "
            f"tree clears it; so does {command}."
        )
    return (
        "No landed PR clears this red: only "
        f"{command} (audited, operator only) or a newer green nightly does."
    )


def _selection_text(regression: Regression) -> str:
    if regression.reproduced == NOT_REPRODUCED:
        return "not read: the red did not reproduce"
    if regression.inside_selection is None:
        return "unknown"
    if regression.inside_selection:
        return "yes — a flake or an interaction"
    return "no — a selector miss: add a selection rule and a policy test"


def record_tip_probe(
    store: RunStore, regression: Regression, *, repo: str | None
) -> None:
    """Record what running *regression*'s failing tests alone at its sha found."""
    emit(
        store,
        TIP_PROBE_EVENT,
        actor=resolve_actor(),
        repo=repo,
        payload={
            "sha": regression.sha,
            "tests": list(regression.tests),
            "reproduced": regression.reproduced,
            "reason": regression.reason,
        },
        strict=True,
    )


def _title_names(regression: Regression) -> str:
    """What a regression issue's title says failed, as plain text.

    A title renders no inline code, so only an id of the strict
    :data:`~kew.verify_ledger.FILEABLE_ID` shape, or a bare test file that
    failed to collect, reaches it — no space, no mention, no link; up to
    three. Failing tests none of which may be named are counted instead. The
    body still lists every id, fenced.
    """
    tests = regression.tests
    plain = [
        test
        for test in tests
        if FILEABLE_ID.fullmatch(test) or _BARE_TEST_FILE.fullmatch(test)
    ]
    if plain:
        return ", ".join(plain[:3])
    if tests:
        return f"{len(tests)} failing test{'' if len(tests) == 1 else 's'}"
    return regression.gate or "the full suite"


def file_regression(
    store: RunStore,
    regression: Regression,
    *,
    repo: str,
    gh: Runner,
) -> Regression:
    """File the regression issue for a red sha, once."""
    existing = filed_regression(store, regression.sha)
    if existing is not None:
        return replace(regression, issue=existing, duplicate=True)
    _ensure_label(gh, repo)
    names = _title_names(regression)
    if regression.reproduced == NOT_REPRODUCED:
        suffix = f" ({NOT_REPRODUCED_TEXT})"
    else:
        suffix = "" if regression.pr is None else f", pinned to #{regression.pr}"
    number = create_issue(
        gh,
        repo,
        title=f"main is red at {regression.sha[:12]}: {names}{suffix}",
        body=regression_body(regression),
        labels=(REGRESSION_LABEL,),
    )
    emit(
        store,
        REGRESSION_EVENT,
        actor=resolve_actor(),
        repo=repo,
        issue_num=number,
        payload={
            "sha": regression.sha,
            "issue": number,
            "tests": list(regression.tests),
            "culprit": regression.culprit,
            "pr": regression.pr,
            "inside_selection": regression.inside_selection,
            "probes": list(regression.probes),
            "reproduced": regression.reproduced,
            "gate": regression.gate,
        },
        dedup_key=f"{REGRESSION_EVENT}:{regression.sha}",
        strict=True,
    )
    return replace(regression, issue=number)
