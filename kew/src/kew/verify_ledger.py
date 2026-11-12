"""What every red merge verify was, layer by layer (#3215).

Read off audit events, with no table of its own:

* each job prints one ``kew-phase: <name> start|end <unix-seconds>`` line per
  phase (:func:`phase_line`), and ``verify_completed`` records what they add
  up to — ``phases {name: {seconds, outcome}}``, the failing phase and the
  failing ids of that phase (:func:`ledger_payload`);
* once the same PR's next verify is recorded, a red one gets its resolution
  class (:func:`classify`), written as a ``verify_red_resolved`` event
  (:func:`resolve_after`);
* a test that failed, then passed on the same head and base within
  :data:`FLAKE_WINDOW`, both times under the full suite, gets one ``flaky``
  issue, deduplicated by test id — only for a verify bound to a PR (the
  drive's merge pass, ``kew verify --pr``), never a bare hand-run one, and
  never more than :data:`MAX_FLAKY_ISSUES` for one red. Filing is a side
  effect on a public repository, so it is bounded (:data:`GH_TIMEOUT`) and
  can never fail a verify: a failure is logged and recorded
  (:data:`LEDGER_FAILED_EVENT`). kew never takes the test off the gate and
  never retries: that stays the operator's call;
* ``kew verify report`` prints reds by phase × class, and the wall seconds of
  each phase on passing verifies (:func:`report`).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import subprocess_utils
from kew.audit import emit, resolve_actor

if TYPE_CHECKING:
    from kew.main_health import Runner
    from kew.merge_verify import VerifyCandidate, VerifyResult
    from kew.run_store import AuditEvent, RunStore

logger = logging.getLogger(__name__)

#: The prefix of a job's phase marker lines.
PHASE_MARK = "kew-phase:"

#: The phase a job is in outside every named one: its sync, its probes, the
#: host's own overhead. Every job opens it first, so the phases of one
#: verification add up to its wall time.
SETUP = "setup"

#: The phases, in the order a verification runs them; a report lists them so.
PHASES = (
    SETUP,
    "static",
    "containment",
    "held_out",
    "contract",
    "xdist",
    "serial",
    "policy",
    "image_scan",
    "docs",
    "vitest",
    "svelte_check",
    "www",
    "ui_check",
)

#: What a phase reads, by repository path prefix: a PR whose diff touches one
#: changed that phase's inputs. ``image_scan`` reads
#: :attr:`VerifyCandidate.image_inputs_changed` instead.
PHASE_INPUTS: Mapping[str, tuple[str, ...]] = {
    "static": ("kew/",),
    "containment": ("kew/src/", "docker/"),
    "held_out": ("kew/src/", "kew/tests/"),
    "contract": ("kew/src/", "kew/tests/"),
    "xdist": ("kew/src/", "kew/tests/", "kew/pyproject.toml", "kew/uv.lock"),
    "serial": ("kew/src/", "kew/tests/", "kew/pyproject.toml", "kew/uv.lock"),
    "policy": ("kew/src/", "kew/tests/", "kew/pyproject.toml", "kew/uv.lock"),
    "docs": ("docs/", "mkdocs.yml"),
    "vitest": ("frontend/",),
    "svelte_check": ("frontend/",),
    "www": ("www/", "frontend/src/app.css"),
    "ui_check": ("frontend/",),
}

#: The audit event a red verify's resolution writes, once per red verify.
RESOLVED_EVENT = "verify_red_resolved"

#: The audit event a filed ``flaky`` issue writes, once per test id.
FLAKY_EVENT = "verify_flaky_filed"

#: The audit event a resolution or a filing that failed writes: it is logged
#: and recorded, never raised into the verify that ran it.
LEDGER_FAILED_EVENT = "verify_ledger_failed"

#: The label a flaky-test issue carries; created when the repo lacks it.
FLAKY_LABEL = "flaky"

#: The most failing ids a red may have and still read as a flake, and so the
#: most ``flaky`` issues one red files. A red with more failed for one cause
#: — a full disk, a broken environment — and is classed :data:`INFRA`.
MAX_FLAKY_ISSUES = 5

#: Seconds each ``gh`` call of a filing may take.
GH_TIMEOUT = 30.0

#: Where a verification's log is kept, under the ledger's directory.
LOG_DIR = "verify-logs"

#: How long a kept log stays; older ones are pruned on each write.
LOG_RETENTION = timedelta(days=30)

#: Failing phases that run the same tests whatever the suite selection: a
#: failing id of theirs ran in the next verify too.
_SUITE_FREE = frozenset({"held_out", "contract", "policy"})

#: How far apart the red and the green of one head may be to read as a flake.
FLAKE_WINDOW = timedelta(days=14)

REAL = "real"
DECLARED = "declared"
FLAKE = "flake"
MAIN = "main"
INFRA = "infra"
UNKNOWN = "unknown"

#: The resolution classes, in the order a report lists them.
CLASSES = (REAL, DECLARED, FLAKE, MAIN, INFRA, UNKNOWN)

#: A red verify the PR's next verify has not resolved yet.
PENDING = "pending"

#: How many verify events resolution and the report read at most.
LEDGER_EVENTS = 5000

_MARK = re.compile(r"^kew-phase: ([a-z_]+) (start|end) (\d+)\s*$", re.MULTILINE)

#: An error that names the host, its auth, a package feed or a ceiling.
_INFRA_ERROR = re.compile(r"\b(host|auth\w*|feed|ceiling)\b", re.IGNORECASE)

#: Paths whose change alone is not a change in code, tests or fixtures.
_PROSE = re.compile(r"^(docs/|.*\.md$)")

#: pytest's header over its short test summary, the one place failing ids
#: are read from.
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
_COUNT = (
    r"\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|errors?|warnings?|rerun)"
)
#: pytest's last line, its final counts: ``=== 1 failed, 3 passed in 0.12s ===``,
#: or bare under ``-q``: ``1 failed, 3 passed in 0.12s``.
_COUNTS_LINE = re.compile(
    rf"^(?:=+ )?(?:no tests ran|{_COUNT}(?:, {_COUNT})*)"
    r" in \d+(?:\.\d+)?s(?: \(\d+:\d\d:\d\d\))?(?: =+)?$"
)
#: A section only the start of a run's report opens: its failures or errors.
_RUN_SECTION = re.compile(r"^=+ (?:FAILURES|ERRORS) =+$")
#: Where a log holds another pytest run: a session's own header, or the line
#: :func:`kew.merge_verify.sharded_result` puts above each job's log.
_RUN_START = re.compile(r"^(?:=+ test session starts =+|--- .+ on .+ ---)$")
#: A verbose session's own header, which pytest prints before any test runs.
_SESSION_START = re.compile(r"^=+ test session starts =+$")

#: A test id kew may name in a public issue: a test file path, ``::`` names,
#: and an optional parametrization, each from a charset that renders as
#: nothing but text — no mention, no link, no markup.
FILEABLE_ID = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.py)(?:::[A-Za-z0-9_]+)+(?:\[[A-Za-z0-9_.,:=/+-]*\])?"
)

#: A commit id as git prints it (sha-1 or sha-256).
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

_DURATION = re.compile(r"^(\d+)([hdw])$")
_UNITS = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1)}


def phase_line(name: str, edge: str) -> str:
    """The shell line that prints one phase marker: ``start`` or ``end``."""
    return f'echo "{PHASE_MARK} {name} {edge} $(date +%s)"'


# ── phases ──────────────────────────────────────────────────────────────────


def _spent(log: str) -> tuple[dict[str, float], set[str], str | None]:
    """Seconds per phase, the phases that ended, and the phase open at the end.

    The clock runs for the phase last started; an ``end`` hands it back to
    :data:`SETUP`. A ``setup start`` opens a new job, whose clock starts there:
    the gap from the previous job's last line is not any phase's.
    """
    spent: dict[str, float] = {}
    ended: set[str] = set()
    current: str | None = None
    last: int | None = None
    for match in _MARK.finditer(log):
        name, edge, stamp = match.group(1), match.group(2), int(match.group(3))
        if not (name == SETUP and edge == "start") and current and last is not None:
            spent[current] = spent.get(current, 0.0) + max(stamp - last, 0)
        last = stamp
        if edge == "start":
            current = name
            spent.setdefault(name, 0.0)
            ended.discard(name)
        else:
            ended.add(name)
            current = SETUP if current == name else current
    return spent, ended, current


def read_phases(log: str, *, seconds: float, outcome: str) -> dict[str, dict[str, Any]]:
    """Each phase *log* marked, with its seconds and outcome.

    What the markers do not cover — each job's tail after its last line — is
    the open phase's: the one the job stopped in, or :data:`SETUP`. So the
    phases add up to *seconds*, the verification's wall time. A phase that
    ended passed; the one still open takes the verification's *outcome*.

    A marker's stamp is whole seconds (``date +%s`` is the portable clock), so
    each job's marked span can run up to a second past what it took. When the
    marked phases outlast *seconds*, each is scaled down by the same factor:
    the excess is the stamps' rounding, not time any phase spent.
    """
    spent, ended, current = _spent(log)
    if not spent:
        return {}
    tail = current or SETUP
    marked = sum(spent.values())
    if seconds > 0 and marked > seconds:
        spent = {name: value * seconds / marked for name, value in spent.items()}
    else:
        spent[tail] = spent.get(tail, 0.0) + max(seconds - marked, 0.0)
    return {
        name: {
            "seconds": round(value, 3),
            "outcome": "passed"
            if name in ended or name != tail or outcome == "passed"
            else outcome,
        }
        for name, value in spent.items()
    }


def _ordered(names: Iterable[str]) -> list[str]:
    names = set(names)
    known = [name for name in PHASES if name in names]
    return known + sorted(set(names) - set(PHASES))


def changed_inputs(candidate: VerifyCandidate) -> list[str]:
    """The phases whose inputs the candidate's diff touched (:data:`PHASE_INPUTS`)."""
    changed = {
        phase
        for phase, prefixes in PHASE_INPUTS.items()
        if any(path.startswith(prefixes) for path in candidate.changed_paths)
    }
    if candidate.image_inputs_changed:
        changed.add("image_scan")
    return _ordered(changed)


def behavior_digest(pr_body: str | None) -> str | None:
    """A digest of the PR body's **Behavior changes** section, as written.

    ``None`` when the verify was given no body (a hand-run ``kew verify``
    without ``--pr-body``): the section is unknown, not empty.
    """
    from kew.merge_verify import behavior_change_lines

    if pr_body is None:
        return None
    text = "\n".join(line.rstrip() for line in behavior_change_lines(pr_body))
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def head_of(candidate: VerifyCandidate) -> str:
    """The PR head a verification tested: a merge commit's non-base parent."""
    parents = candidate.ref_parents
    if len(parents) == 2 and candidate.base_sha in parents:
        return next(parent for parent in parents if parent != candidate.base_sha)
    return candidate.ref_sha


def _merged(
    into: dict[str, dict[str, Any]], job: Mapping[str, Mapping[str, Any]]
) -> None:
    """Fold one parallel job's phases into *into*: a phase lasts as long as its
    slowest job, and fails when any job's did."""
    for name, phase in job.items():
        seen = into.setdefault(name, {"seconds": 0.0, "outcome": "passed"})
        seen["seconds"] = max(seen["seconds"], phase["seconds"])
        if seen["outcome"] == "passed":
            seen["outcome"] = phase["outcome"]


def _sharded_phases(result: VerifyResult) -> dict[str, dict[str, Any]]:
    """The phases of a sharded verification, read job by job.

    Its jobs ran at once on several hosts, so their logs, joined one after the
    other, are not one timeline: summed, the phases would outlast the wall
    clock, and a red job followed by a green one would read as the green one's
    open phase. The gate job, when one ran first, is the log before the first
    job's header; it passed, or the pool would not have run.
    """
    first = result.shards[0]
    gate_log = result.log.partition(f"--- {first.job} on {first.instance} ---\n")[0]
    gate = read_phases(gate_log, seconds=0.0, outcome="passed")
    pool: dict[str, dict[str, Any]] = {}
    for report in result.shards:
        job = report.result
        _merged(pool, read_phases(job.log, seconds=job.seconds, outcome=job.outcome))
    for name, phase in pool.items():
        before = gate.get(name, {}).get("seconds", 0.0)
        gate[name] = {**phase, "seconds": round(before + phase["seconds"], 3)}
    return gate


def _verdict_phases(result: VerifyResult) -> dict[str, dict[str, Any]]:
    """The phases of *result*, with held-out failed when its verdict was."""
    if result.shards:
        phases = _sharded_phases(result)
    else:
        phases = read_phases(result.log, seconds=result.seconds, outcome=result.outcome)
    held = result.held_out
    if held is not None and held.undeclared and "held_out" in phases:
        # Held-out never fails its own run: the verdict on it does, after.
        phases["held_out"]["outcome"] = "failed"
        phases.get(SETUP, {})["outcome"] = "passed"
    return phases


def summary_text(log: str) -> str:
    """The lines of each pytest run's own short test summary in *log*, or
    nothing when the log's shape is ambiguous.

    A log holds one run per session header and one per sharded job
    (:data:`_RUN_START`). A run's summary is its one ``=== short test summary
    info ===`` block, before its one counts line, and runs to the next ``=``
    rule. A test controls every line it prints, so it can print a summary
    block, a counts line or a run start, inside the run's report or after the
    run ends. Wherever the reader could take such a line for pytest's own
    (:func:`_ambiguous`), the log names no id at all: a forged block can then
    neither add an id nor hide the real ones. A log that holds two ``-q`` runs
    with no run start between them is ambiguous in that way too.
    """
    segments: list[list[str]] = [[]]
    for line in log.splitlines():
        text = line.rstrip()
        if _RUN_START.match(text):
            segments.append([])
        segments[-1].append(text)
    if _ambiguous(segments):
        return ""
    return "\n".join(line for segment in segments for line in _summary_block(segment))


def _ambiguous(segments: list[list[str]]) -> bool:
    """True when a pytest run in *segments* does not have the shape one run
    prints, or the log mixes ``-q`` runs (bare counts line) with verbose ones
    (counts line between ``=`` rules)."""
    styles = {
        text.startswith("=")
        for segment in segments
        for text in segment
        if _COUNTS_LINE.match(text)
    }
    return len(styles) > 1 or any(_misshapen(segment) for segment in segments)


def _misshapen(segment: list[str]) -> bool:
    """True when *segment* is not one run's shape: more than one summary
    header or counts line, a summary header with no counts line after it, or,
    in a verbose session, a summary before the run's report opens
    (``FAILURES`` or ``ERRORS``), which is where a test's own output lands."""
    headers = [i for i, text in enumerate(segment) if _SUMMARY_HEADER.match(text)]
    counts = [i for i, text in enumerate(segment) if _COUNTS_LINE.match(text)]
    if len(headers) > 1 or len(counts) > 1:
        return True
    if not headers:
        return False
    if not counts or counts[0] < headers[0]:
        return True
    return _unreported(segment, headers[0])


def _unreported(segment: list[str], header: int) -> bool:
    """True when *segment* is a verbose session whose summary at *header*
    comes before any ``FAILURES`` or ``ERRORS`` section opens."""
    if not _SESSION_START.match(segment[0]):
        return False
    return not any(_RUN_SECTION.match(text) for text in segment[:header])


def _summary_block(segment: list[str]) -> list[str]:
    """The lines of *segment*'s one short summary, up to the next ``=`` rule."""
    headers = [i for i, text in enumerate(segment) if _SUMMARY_HEADER.match(text)]
    if not headers:
        return []
    block: list[str] = []
    for text in segment[headers[0] + 1 :]:
        if text.startswith("="):
            break
        block.append(text)
    return block


def _failing_ids(result: VerifyResult, failing: str | None) -> Sequence[str]:
    """The ids the failing phase failed: held-out's undeclared, or the ones
    pytest's short test summaries name (:func:`summary_text`, which
    :func:`~kew.main_health.failing_tests` reads)."""
    from kew.main_health import failing_tests

    if failing == "held_out" and result.held_out is not None:
        return result.held_out.undeclared
    if result.outcome != "failed":
        return ()
    return failing_tests(result.log)


def ledger_payload(
    candidate: VerifyCandidate, result: VerifyResult, *, pr_body: str | None
) -> dict[str, object]:
    """What ``verify_completed`` adds for the ledger."""
    phases = _verdict_phases(result)
    red = [name for name in _ordered(phases) if phases[name]["outcome"] != "passed"]
    failing = next((name for name in red if name != SETUP), red[0] if red else None)
    return {
        "head": head_of(candidate),
        "phases": {name: phases[name] for name in _ordered(phases)},
        "failing_phase": failing,
        "failing_ids": list(_failing_ids(result, failing)),
        "changed_inputs": changed_inputs(candidate),
        "behavior_changes": behavior_digest(pr_body),
    }


def write_log(store: RunStore | None, ref_sha: str, log: str) -> str | None:
    """Keep the verification's log beside the ledger, and return its path.

    Logs older than :data:`LOG_RETENTION` are pruned first, so the folder
    holds a month of verifies, not every one ever run.
    """
    db = getattr(store, "_db_path", None)
    if not isinstance(db, str) or not db or db == ":memory:":
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path(db).resolve().parent / LOG_DIR / f"{stamp}-{ref_sha[:12]}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_logs(path.parent)
        path.write_text(log, encoding="utf-8")
    except OSError as exc:
        logger.warning("verify log not kept: %s", exc)
        return None
    return str(path)


def _prune_logs(folder: Path) -> None:
    """Remove the kept logs older than :data:`LOG_RETENTION`."""
    cutoff = time.time() - LOG_RETENTION.total_seconds()
    for old in folder.glob("*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError as exc:
            logger.warning("verify log %s not pruned: %s", old.name, exc)


def shown_log(path: object) -> str:
    """A kept log as an issue names it: relative to the ledger's directory,
    so no home path or user name leaves the machine."""
    if not isinstance(path, str) or not path:
        return "not kept"
    return f"{LOG_DIR}/{Path(path).name}"


# ── resolution ──────────────────────────────────────────────────────────────


def _payload(event: AuditEvent) -> dict[str, Any]:
    return event.payload if isinstance(event.payload, dict) else {}


def _key(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """The PR a verification belongs to, or its head when it named none."""
    if isinstance(payload.get("pr"), int):
        return ("pr", str(payload["pr"]))
    head = payload.get("head")
    return ("head", str(head)) if head else None


def _infra(red: Mapping[str, Any]) -> bool:
    if red.get("outcome") == "incomplete" or red.get("gate_ran") is False:
        return True
    if red.get("error_category"):
        return True
    return bool(_INFRA_ERROR.search(str(red.get("error") or "")))


def _ran_full(payload: Mapping[str, Any]) -> bool:
    """Whether the verify ran every test: the full suite and the full
    frontend, not a selection of either (``[verify] frontend = "scoped"``).
    A frontend selection that cannot be read is no evidence: not full."""
    from kew.main_health import FRONTEND_FULL, SUITE_FULL

    frontend = payload.get("frontend_selection")
    whole_frontend = frontend is None or (
        isinstance(frontend, dict) and frontend.get("mode") == FRONTEND_FULL
    )
    return payload.get("suite") == SUITE_FULL and whole_frontend


def _green_skipped(red: Mapping[str, Any], green: Mapping[str, Any]) -> bool:
    """Whether *green* may not have run a test *red* failed.

    A selected verify ran only its selection's files (and every ``policy``
    test); a red's failing id outside them says nothing about the test.
    """
    if _ran_full(green) or red.get("failing_phase") in _SUITE_FREE:
        return False
    selection = green.get("suite_selection")
    files = selection.get("files") if isinstance(selection, dict) else None
    ran = {str(path) for path in files or ()}
    return any(
        str(test).split("::", 1)[0] not in ran for test in red.get("failing_ids") or ()
    )


def _held_out_passed(payload: Mapping[str, Any]) -> bool:
    held = payload.get("held_out")
    if not isinstance(held, dict) or held.get("outcome") in (None, "not_run"):
        return False
    return not held.get("undeclared")


#: Answers the paths that differ between two heads, or ``None`` unread.
ChangedPaths = Callable[[str, str], "Sequence[str] | None"]


def classify(
    red: Mapping[str, Any], green: Mapping[str, Any], changed: ChangedPaths
) -> str:
    """What the red verify *red* was, read against the PR's next verify *green*.

    A red with more than :data:`MAX_FLAKY_ISSUES` failing ids is
    :data:`INFRA`: that many tests fail together for one cause, not one each.
    A green that may not have run a test the red one failed (a selection
    without it) cannot resolve it: :data:`UNKNOWN`.
    """
    if _infra(red) or len(red.get("failing_ids") or ()) > MAX_FLAKY_ISSUES:
        return INFRA
    if green.get("outcome") != "passed" or _green_skipped(red, green):
        return UNKNOWN
    if red.get("head") != green.get("head"):
        paths = changed(str(red.get("head")), str(green.get("head")))
        moved = paths is None or any(not _PROSE.match(path) for path in paths)
        return REAL if moved else UNKNOWN
    return _same_head(red, green)


def _body_changed(red: Mapping[str, Any], green: Mapping[str, Any]) -> bool:
    """Whether the **Behavior changes** section changed between the two.

    An unknown digest (a verify given no body) never counts as a change.
    """
    before, after = red.get("behavior_changes"), green.get("behavior_changes")
    return before is not None and after is not None and before != after


def _same_head(red: Mapping[str, Any], green: Mapping[str, Any]) -> str:
    """:func:`classify` for a green of the head the red one tested.

    ``declared`` is read first: the same head, a changed body, and held-out
    passed, whether or not main moved meanwhile. A ``flake`` needs both
    verifies to have run the full suite.
    """
    body_changed = _body_changed(red, green)
    if body_changed and _held_out_passed(green):
        return DECLARED
    if red.get("base_sha") != green.get("base_sha"):
        return UNKNOWN if body_changed else MAIN
    if body_changed:
        return UNKNOWN
    return FLAKE if _ran_full(red) and _ran_full(green) else UNKNOWN


def git_changed(repo_root: Path) -> ChangedPaths:
    """The paths two heads differ in, read with git in *repo_root*."""
    from kew.main_health import MainHealthError, read_git

    def changed(old: str, new: str) -> Sequence[str] | None:
        try:
            return read_git(repo_root, "diff", "--name-only", old, new).splitlines()
        except MainHealthError as exc:
            logger.warning("verify ledger: cannot diff %s..%s: %s", old, new, exc)
            return None

    return changed


#: Answers whether a test file (relative to ``kew/``) exists at a head, or
#: ``None`` unread.
Present = Callable[[str, str], "bool | None"]


def git_present(repo_root: Path) -> Present:
    """Whether a test file exists at a head, read with ``git cat-file -e`` in
    *repo_root* under the inert git environment (no hook, no outside config)."""
    from kew.publication_apply import inert_git_env

    def present(head: str, path: str) -> bool | None:
        if not _COMMIT.fullmatch(head):
            return None
        argv = ["git", "cat-file", "-e", f"{head}:kew/{path}"]
        try:
            done = subprocess_utils.run(
                argv, cwd=repo_root, check=False, env=inert_git_env(), timeout=30
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("verify ledger: cannot read %s at %s: %s", path, head, exc)
            return None
        return done.returncode == 0

    return present


def _stamp(event: AuditEvent) -> datetime:
    stamp = datetime.fromisoformat(event.timestamp)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _previous(events: Sequence[AuditEvent], this: AuditEvent) -> AuditEvent | None:
    """The same PR's verify recorded last before *this*."""
    key = _key(_payload(this))
    if key is None:
        return None
    for event in events:
        payload = _payload(event)
        pr_kind = payload.get("kind", "pr") == "pr"
        if event.id < this.id and pr_kind and _key(payload) == key:
            return event
    return None


def _resolved(store: RunStore, red: int) -> bool:
    return any(
        _payload(event).get("red") == red
        for event in store.list_audit_events(LEDGER_EVENTS, event=RESOLVED_EVENT)
    )


def resolve_after(
    store: RunStore | None,
    receipt: int | None,
    *,
    repo_root: Path,
    repo: str,
    gh: Runner = subprocess_utils.run,
    changed: ChangedPaths | None = None,
    present: Present | None = None,
) -> str | None:
    """Resolve the red verify recorded before *receipt*, and return its class.

    ``None`` when there was none: no earlier verify of the PR, a green one, or
    one already resolved. A flake of a PR-bound verify files its tests'
    ``flaky`` issues, once each. It runs after the verify is recorded, and
    nothing it does can fail that verify: any failure, in the resolution or
    in a filing, is logged at WARNING and recorded (:data:`LEDGER_FAILED_EVENT`).
    """
    if store is None or receipt is None:
        return None
    try:
        return _resolve(
            store,
            receipt,
            repo_root=repo_root,
            repo=repo,
            gh=gh,
            changed=changed or git_changed(repo_root),
            present=present or git_present(repo_root),
        )
    except Exception as exc:  # the verify's own verdict stands whatever this hits
        logger.warning("verify ledger: the previous verify not resolved: %s", exc)
        _record_failure(store, repo, {"verify": receipt, "step": "resolve"}, exc)
        return None


def _record_failure(
    store: RunStore, repo: str, where: dict[str, object], exc: BaseException
) -> None:
    """Record a resolution or filing that failed; a record that fails too is
    logged and dropped."""
    text = f"{type(exc).__name__}: {exc}"
    # A gh error names its whole argv, issue body and all: keep both ends.
    error = text if len(text) <= 500 else f"{text[:240]} … {text[-240:]}"
    payload = {**where, "error": error}
    try:
        emit(
            store,
            LEDGER_FAILED_EVENT,
            actor=resolve_actor(),
            repo=repo or None,
            payload=payload,
        )
    except Exception as again:  # recording the failure must not raise either
        logger.warning("verify ledger: failure not recorded: %s", again)


def _unresolved_red(
    store: RunStore, receipt: int
) -> tuple[AuditEvent, AuditEvent] | None:
    """The PR's red verify before *receipt*, unresolved, and *receipt*'s own."""
    from kew.merge_verify import VERIFY_EVENT

    events = store.list_audit_events(LEDGER_EVENTS, event=VERIFY_EVENT)
    this = next((event for event in events if event.id == receipt), None)
    if this is None or _payload(this).get("kind", "pr") != "pr":
        return None
    prior = _previous(events, this)
    if prior is None or _payload(prior).get("outcome") == "passed":
        return None
    return None if _resolved(store, prior.id) else (prior, this)


def _resolve(
    store: RunStore,
    receipt: int,
    *,
    repo_root: Path,
    repo: str,
    gh: Runner,
    changed: ChangedPaths,
    present: Present,
) -> str | None:
    pair = _unresolved_red(store, receipt)
    if pair is None:
        return None
    prior, this = pair
    red, green = _payload(prior), _payload(this)
    found = classify(red, green, changed)
    emit(
        store,
        RESOLVED_EVENT,
        actor=resolve_actor(),
        repo=repo or None,
        payload={
            "red": prior.id,
            "next": this.id,
            "class": found,
            "phase": red.get("failing_phase"),
            "failing_ids": list(red.get("failing_ids") or ()),
            "pr": green.get("pr"),
            "head": green.get("head"),
        },
        dedup_key=f"{RESOLVED_EVENT}:{prior.id}",
    )
    # Only a verify bound to a PR files: a bare hand-run `kew verify <ref>`
    # never touches GitHub.
    bound = isinstance(green.get("pr"), int)
    recent = _stamp(this) - _stamp(prior) <= FLAKE_WINDOW
    if found == FLAKE and recent and repo and bound:
        file_flakes(store, prior, this, repo=repo, gh=gh, present=present)
    return found


# ── flaky issues ────────────────────────────────────────────────────────────


def _filed(store: RunStore) -> set[str]:
    return {
        str(_payload(event).get("test"))
        for event in store.list_audit_events(LEDGER_EVENTS, event=FLAKY_EVENT)
    }


def _unfileable(test: str, head: str, present: Present) -> str | None:
    """Why *test* may not be named in a public issue, or ``None`` when it may.

    Its id must be :data:`FILEABLE_ID` throughout, with no ``..`` in its
    path, and its test file must exist at the head the red verify tested.
    """
    match = FILEABLE_ID.fullmatch(test)
    if match is None or ".." in match.group("path").split("/"):
        return "the id is not a plain test id"
    found = present(head, match.group("path"))
    if found is None:
        return "its test file could not be read at the head"
    return None if found else "its test file does not exist at the head"


def flaky_body(test: str, red: AuditEvent, green: AuditEvent) -> str:
    """The issue body for *test*, which failed in *red* and passed in *green*."""
    from kew.main_health import code_span

    first, second = _payload(red), _payload(green)
    return "\n".join(
        [
            f"{code_span(test)} failed, then passed, on the same head and the "
            "same base.",
            "",
            f"- head: `{first.get('head')}`",
            f"- base: `{first.get('base_sha')}`",
            f"- red verify: audit row {red.id}, "
            f"log `{shown_log(first.get('log_path'))}`",
            f"- green verify: audit row {green.id}, "
            f"log `{shown_log(second.get('log_path'))}`",
            "",
            "kew files one issue per test and leaves the test on the gate: "
            "taking it off is the operator's decision.",
        ]
    )


def bounded(gh: Runner) -> Runner:
    """*gh*, with every call given :data:`GH_TIMEOUT`."""

    def run(argv: list[str], **kwargs: Any) -> Any:
        return gh(argv, **{**kwargs, "timeout": GH_TIMEOUT})

    return run


def _fileable(
    store: RunStore, red: AuditEvent, green: AuditEvent, *, repo: str, present: Present
) -> list[str]:
    """*red*'s failing ids not filed before that may be named in an issue;
    each one that may not is logged and recorded as skipped, with why."""
    filed = _filed(store)
    head = str(_payload(red).get("head") or "")
    tests: list[str] = []
    for test in (str(t) for t in _payload(red).get("failing_ids") or ()):
        if test in filed:
            continue
        reason = _unfileable(test, head, present)
        if reason is None:
            tests.append(test)
            continue
        logger.warning("flaky issue for %r not filed: %s", test, reason)
        _record_skip(store, repo, red, green, test, reason)
    return tests[:MAX_FLAKY_ISSUES]


def _record_skip(
    store: RunStore,
    repo: str,
    red: AuditEvent,
    green: AuditEvent,
    test: str,
    reason: str,
) -> None:
    try:
        emit(
            store,
            LEDGER_FAILED_EVENT,
            actor=resolve_actor(),
            repo=repo or None,
            payload={
                "red": red.id,
                "green": green.id,
                "step": "skip",
                "test": test[:300],
                "reason": reason,
            },
        )
    except Exception as exc:  # recording the skip must not raise either
        logger.warning("verify ledger: skip not recorded: %s", exc)


def file_flakes(
    store: RunStore,
    red: AuditEvent,
    green: AuditEvent,
    *,
    repo: str,
    gh: Runner,
    present: Present,
) -> list[int]:
    """File one ``flaky`` issue per failing test of *red* not filed before.

    Only an id :func:`_unfileable` clears: a plain test id whose file exists
    at the head, rendered as inline code. At most :data:`MAX_FLAKY_ISSUES`,
    each ``gh`` call bounded by :data:`GH_TIMEOUT`; a call that fails is
    logged and recorded, and the rest go on.
    """
    from kew.main_health import code_span, create_issue

    tests = _fileable(store, red, green, repo=repo, present=present)
    if not tests:
        return []
    gh = bounded(gh)
    where: dict[str, object] = {"red": red.id, "green": green.id, "step": "file"}
    try:
        gh(["gh", "label", "create", FLAKY_LABEL, "--repo", repo], check=False)
    except Exception as exc:  # a label that cannot be made never fails a verify
        logger.warning("flaky label not created: %s", exc)
        _record_failure(store, repo, {**where, "step": "label"}, exc)
    numbers: list[int] = []
    for test in tests:
        try:
            number = create_issue(
                gh,
                repo,
                title=f"flaky: {code_span(test)}",
                body=flaky_body(test, red, green),
                labels=(FLAKY_LABEL,),
            )
        except Exception as exc:  # an issue that cannot be filed never fails one
            logger.warning("flaky issue for %s not filed: %s", test, exc)
            _record_failure(store, repo, {**where, "test": test}, exc)
            continue
        emit(
            store,
            FLAKY_EVENT,
            actor=resolve_actor(),
            repo=repo,
            issue_num=number,
            payload={"test": test, "issue": number, "red": red.id, "green": green.id},
            dedup_key=f"{FLAKY_EVENT}:{test}",
        )
        numbers.append(number)
    return numbers


# ── the report ──────────────────────────────────────────────────────────────


def parse_since(text: str) -> timedelta:
    """``30d``, ``12h`` or ``2w`` as a span; anything else is a ValueError."""
    match = _DURATION.match(text.strip())
    if match is None or int(match.group(1)) == 0:
        raise ValueError(f"--since takes a span like 30d, 12h or 2w, not {text!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _percentile(values: Sequence[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(
            cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
            for i, cell in enumerate(row)
        ).rstrip()
        for row in [header, *rows]
    ]


def _red_rows(verifies: Sequence[AuditEvent], classes: Mapping[int, str]) -> list[str]:
    counts: dict[str, dict[str, int]] = {}
    for event in verifies:
        payload = _payload(event)
        if payload.get("outcome") == "passed":
            continue
        phase = str(payload.get("failing_phase") or "-")
        found = classes.get(event.id, PENDING)
        row = counts.setdefault(phase, {})
        row[found] = row.get(found, 0) + 1
    columns = (*CLASSES, PENDING)
    phases = _ordered(name for name in counts if name != "-")
    phases += ["-"] if "-" in counts else []
    rows = [
        [phase, *(str(counts[phase].get(c, 0)) for c in columns)]
        + [str(sum(counts[phase].values()))]
        for phase in phases
    ]
    return _table(["phase", *columns, "total"], rows) if rows else ["(no red verify)"]


def _passing_seconds(verifies: Sequence[AuditEvent]) -> dict[str, list[float]]:
    """Each phase's seconds, one value per passing verify that ran it."""
    seconds: dict[str, list[float]] = {}
    for event in verifies:
        payload = _payload(event)
        if payload.get("outcome") != "passed":
            continue
        for phase, entry in (payload.get("phases") or {}).items():
            value = entry.get("seconds") if isinstance(entry, dict) else None
            if isinstance(value, int | float):
                seconds.setdefault(phase, []).append(float(value))
    return seconds


def _input_changes(
    verifies: Sequence[AuditEvent],
) -> tuple[set[tuple[str, str]], dict[str, set[tuple[str, str]]]]:
    """Every PR of *verifies*, and for each phase the PRs that changed its inputs."""
    prs: set[tuple[str, str]] = set()
    changed: dict[str, set[tuple[str, str]]] = {}
    for event in verifies:
        payload = _payload(event)
        key = _key(payload) or ("row", str(event.id))
        prs.add(key)
        for phase in payload.get("changed_inputs") or ():
            changed.setdefault(str(phase), set()).add(key)
    return prs, changed


def _phase_rows(verifies: Sequence[AuditEvent]) -> list[str]:
    seconds = _passing_seconds(verifies)
    prs, changed = _input_changes(verifies)
    rows = []
    for phase in _ordered(seconds):
        share = (
            f"{100 * len(changed.get(phase, ())) / len(prs):.0f}%"
            if phase in PHASE_INPUTS or phase == "image_scan"
            else "-"
        )
        values = seconds[phase]
        rows.append(
            [
                phase,
                f"{_percentile(values, 0.5):.0f}",
                f"{_percentile(values, 0.9):.0f}",
                share,
            ]
        )
    header = ["phase", "p50 s", "p90 s", "PRs changing inputs"]
    return _table(header, rows) if rows else ["(no passing verify)"]


def _left_out_line(count: int) -> str:
    noun = "verify" if count == 1 else "verifies"
    return f"(left out: {count} {noun} recorded before kew kept phases)"


def _window(store: RunStore, cutoff: datetime) -> tuple[list[AuditEvent], int]:
    """The PR verifies since *cutoff*, oldest first, and how many were left out.

    A verify recorded before the ledger kept phases carries none, and no
    head: it would read as a red no later verify can ever resolve.
    """
    from kew.merge_verify import VERIFY_EVENT

    window = [
        event
        for event in store.list_audit_events(LEDGER_EVENTS, event=VERIFY_EVENT)
        if _payload(event).get("kind", "pr") == "pr" and _stamp(event) >= cutoff
    ]
    kept = [event for event in window if "phases" in _payload(event)]
    return sorted(kept, key=lambda event: event.id), len(window) - len(kept)


def report(store: RunStore, *, since: timedelta, now: datetime | None = None) -> str:
    """Reds by phase × class, then passing verifies' seconds by phase."""
    cutoff = (now or datetime.now(UTC)) - since
    verifies, left_out = _window(store, cutoff)
    classes = {
        int(_payload(event)["red"]): str(_payload(event).get("class"))
        for event in store.list_audit_events(LEDGER_EVENTS, event=RESOLVED_EVENT)
        if isinstance(_payload(event).get("red"), int)
    }
    reds = sum(1 for e in verifies if _payload(e).get("outcome") != "passed")
    passing = len(verifies) - reds
    lines = [
        f"Red verifies by phase and class since {cutoff:%Y-%m-%d %H:%M} UTC "
        f"({reds} of {len(verifies)}):",
        *([_left_out_line(left_out)] if left_out else []),
        *_red_rows(verifies, classes),
        "",
        f"Passing verifies by phase ({passing}):",
        *_phase_rows(verifies),
    ]
    return "\n".join(lines) + "\n"
