"""Test-value report: which tests can go without losing coverage or a kill (#3214).

Honest pruning means one thing: a removal never loses a covered line, a covered
arc or a killed mutant. This tool produces the evidence a pruning change
cites; it never edits a test. Every error path ends in *not proven, keep the
test*, never in *removable*.

1. **One measuring run** of the whole suite on one clean commit, with per-test
   coverage contexts (``--cov-context=test``, arcs on), ``--durations=0``, and
   a JSON item report this module writes as a pytest plugin (``items.json``:
   node id, markers after collection, wall seconds). This file is the plugin,
   so it is left out of the measured coverage (:data:`OMITTED_FROM_COVERAGE`):
   its hooks run inside every test's context.
2. **Candidates**: every item in a legacy round-named file (the quality
   ratchet's ``legacy_names``), every item of a function with
   :data:`WIDE_PARAMETRIZE` or more parametrized items, every member of a group
   of same-shape test bodies, and every item whose context adds no line or
   arc over the rest of the suite.
3. **Exclusions**, never candidates: anything marked ``policy`` or ``serial``,
   an acceptance contract (``tests/acceptance/``), the containment suite, the
   verify gate's own tests, and every test named in ``--caught-defects`` (the
   tests that caught a real defect). The run refuses to start without
   ``--caught-defects <file>`` unless ``--no-caught-defects`` says, on purpose,
   that there is no such list.
4. **Coverage proof**: candidates are taken greedily, slowest first, and one is
   kept the moment taking it would leave a line or arc with no remaining
   test. An item that covers no measured line is never removable: nothing
   proves it redundant.
5. **Mutation proof**: for each module the set covers, mutmut runs on that
   module with the tests that cover it, in a work copy built from ``git
   archive`` of the measured commit (the whole tracked tree, so data files
   exist) under the project's own ``[tool.mutmut]`` settings. The base run goes
   twice; the module is proven only when both base runs give the same verdict
   for every mutant, every verdict is ``killed``, ``survived`` or ``no tests``,
   and at least one mutant is killed. A slow or killed base run (mutmut's
   ``timeout`` and ``segfault``) proves nothing. A trial with candidates
   deselected counts only when every mutant the base killed is still
   ``killed`` and every other mutant still survives; any other verdict makes
   the candidate set not removable, and it is halved until the item that
   alone holds a verdict is found and kept. Each module has a wall-clock cap;
   a module over its cap, or whose run fails, leaves its candidates *not
   proven*. Each work copy is deleted as soon as its verdicts are read.

The report — ``report.json`` and ``report.md`` under
``<work_root>/test-value/<sha>/`` — lists candidates for a person to review:
per file and per function, the removable items, the seconds they cost, and the
proof (lines, arcs, mutants), plus the xdist-phase seconds the removable set
saves. Nothing is deleted automatically.

Only code run inside the pytest process is seen, by coverage and by mutmut
alike, so the measuring run's plugin watches each test through an audit hook
(:data:`SPAWN_EVENTS`): a test that starts any process is never a candidate.

The run is one heavy job: it holds the merge-queue lock (as the nightly does)
and the local merge verify's lease (so the drive yields and no merge verify
runs beside it), and passes both descriptors to each mutmut session, so the
locks stay held while one lives. SIGTERM and SIGHUP kill the running mutmut
session and unwind through the locks' release; a second one during that
unwind exits at once. It refuses a dirty working tree, a load average above
:data:`MAX_LOAD`, under :data:`MIN_FREE_BYTES` of free disk, and starts only
before 01:00 or from 04:00 on; no mutmut run starts unless its cap ends before
03:00. A ``--caught-defects`` entry that names no collected test refuses the
run, as does an empty list.

Usage (from ``kew/``; keep the caught-defect list outside the checkout)::

    uv run python scripts/test_value_report.py --caught-defects /tmp/caught.txt
    uv run python scripts/test_value_report.py --no-caught-defects --cap 1800

Exit codes: 0 report written, 1 the measuring run failed, 2 refused to start,
128 + N stopped by signal N.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess  # nosec B404 - argv lists only, never a shell
import sys
import tarfile
import time
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN = Path(__file__).stem

#: Files the measuring run loads as its own plugin: never measured, because
#: their hooks run inside every test's coverage context.
OMITTED_FROM_COVERAGE = frozenset({Path(__file__).resolve()})

#: A function with this many parametrized items or more is a candidate.
WIDE_PARAMETRIZE = 20
#: Above this one-minute load average the run refuses to start.
MAX_LOAD = 30.0
#: The quiet hour, 03:00-04:00: no mutmut run starts unless its cap ends first.
QUIET_HOUR = 3
#: The measuring run starts only before 01:00 or from 04:00 on.
MEASURE_CUTOFF_HOUR = 1
#: Seconds a SIGKILLed mutmut session gets to be reaped before it is left.
KILL_WAIT_S = 10.0
#: Default wall-clock seconds mutmut gets per module, both runs and all halvings.
DEFAULT_CAP_S = 1800.0
#: Below this much free disk where the report goes, the run refuses to start.
MIN_FREE_BYTES = 20 * 1024**3

#: The only verdicts a base run may give for its module to be proven.
CLEAN_VERDICTS = frozenset({"killed", "survived", "no tests"})
#: Verdicts that mean no test killed the mutant.
SURVIVING = frozenset({"survived", "no tests"})

EXCLUDED_MARKERS = frozenset({"policy", "serial"})
EXCLUDED_FILES = frozenset(
    {
        "tests/test_sandbox_containment.py",
        "tests/test_verify_parity.py",
        "tests/test_serial_phase.py",
    }
)
EXCLUDED_PREFIXES = ("tests/test_held_out_", "tests/acceptance/")

ITEMS_ENV = "KEW_TEST_VALUE_ITEMS"
DESELECT_ENV = "KEW_TEST_VALUE_DESELECT"
_CONTEXT_PHASE = re.compile(r"\|(run|setup|teardown)$")
_RESULT_LINE = re.compile(r"^\s+(\S+): (.+)$")

#: How mutmut runs: its CLI, with process titles made a no-op. mutmut sets one
#: in every mutant's forked child, and on macOS 27 setproctitle's first call in
#: a forked child segfaults it in about half of all mutmut processes, which
#: reads as every mutant ``segfault``. A title is cosmetic; a verdict is not.
_MUTMUT_LAUNCHER = """\
import sys
import types

titles = types.ModuleType("setproctitle")
titles.setproctitle = lambda title: None
titles.getproctitle = lambda: ""
sys.modules["setproctitle"] = titles
sys.argv[0] = "mutmut"
from mutmut.__main__ import cli
cli()
"""
_TABLE_HEADER = re.compile(r"^\[\[?\s*[A-Za-z0-9_.\"' -]+\s*\]\]?\s*(#.*)?$")

#: Audit events that start another process. Coverage and mutmut both see only
#: the pytest process, so a test that raises one is never a candidate.
SPAWN_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.exec",
        "os.posix_spawn",
        "os.system",
        "os.spawn",
        "os.fork",
        "os.forkpty",
    }
)
SPAWNED = "not proven: runs code in a process coverage cannot see"

NOT_MEASURED = (
    "Only Python faults in the measured `src/` and `scripts/` count. Docs, the "
    "frontend, workflow YAML, data-file assertions and anything a test checks "
    "outside covered Python are invisible to this report: a test listed here "
    "may still be the only check on one of them.",
    "Only code run inside the pytest process is measured. A test that starts "
    "any process (a subprocess, `os.system`, an exec, a spawn or a fork) while "
    "it runs is never a candidate; a process started by a shared fixture is "
    "charged only to the test it started for.",
    "Never candidates: tests marked `policy` or `serial`, acceptance contracts, "
    "held-out, containment, serial-phase and verify-parity tests, and the tests "
    "named as having caught a real defect.",
)


# --- pytest plugin: loaded with ``-p test_value_report`` by the measuring run.

_collected: dict[str, dict[str, Any]] = {}
#: The item running now, from its setup to its teardown.
_running: list[str] = []
#: Items that started another process while they ran.
_spawned: set[str] = set()


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event in SPAWN_EVENTS and _running:
        _spawned.add(_running[0])


def pytest_configure(config: Any) -> None:
    """In the measuring run, watch every test for a process it starts."""
    if os.environ.get(ITEMS_ENV):
        sys.addaudithook(_audit)  # for the life of that pytest process only


def pytest_collection_finish(session: Any) -> None:
    """Record each item after every hook has marked it (``serial`` included)."""
    for item in session.items:
        _collected[item.nodeid] = {
            "markers": sorted({mark.name for mark in item.iter_markers()}),
            "seconds": 0.0,
        }


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    _running[:] = [nodeid]


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    _running.clear()


def pytest_runtest_logreport(report: Any) -> None:
    if report.nodeid in _collected:
        _collected[report.nodeid]["seconds"] += report.duration


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Drop exactly the listed node ids: ``--deselect`` matches by prefix.

    Fails closed: when the whole selection is collected (mutmut's stats run,
    or any run outside mutmut), a listed id that was not collected aborts the
    run, because a deselection that silently missed would let a trial keep
    the very test it claims to have removed.
    """
    source = os.environ.get(DESELECT_ENV)
    if not source:
        return
    drop = set(json.loads(Path(source).read_text(encoding="utf-8")))
    gone = [item for item in items if item.nodeid in drop]
    missing = drop - {item.nodeid for item in gone}
    if missing and os.environ.get("MUTANT_UNDER_TEST", "stats") == "stats":
        import pytest

        raise pytest.UsageError(
            f"test-value: {len(missing)} deselected id(s) were not collected: "
            + ", ".join(sorted(missing)[:3])
        )
    if gone:
        items[:] = [item for item in items if item.nodeid not in drop]
        config.hook.pytest_deselected(items=gone)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    target = os.environ.get(ITEMS_ENV)
    if target:
        for nodeid, entry in _collected.items():
            entry["spawned"] = nodeid in _spawned
        payload = {"exitstatus": int(exitstatus), "items": _collected}
        Path(target).write_text(json.dumps(payload, indent=1), encoding="utf-8")


# --- git, signals and the lease.


class GitError(RuntimeError):
    """A git command the run depends on failed."""


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise GitError(f"git {args[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def head_sha(project: Path) -> str:
    return _git(project, "rev-parse", "HEAD").strip()


def tree_refusal(project: Path) -> str | None:
    """Why *project* is not a clean checkout to measure, or ``None``."""
    try:
        head_sha(project)
        dirty = _git(project, "status", "--porcelain").splitlines()
    except (GitError, OSError) as error:
        return f"{project} is not a git checkout with a commit: {error}"
    if dirty:
        return (
            f"the working tree has {len(dirty)} uncommitted or untracked "
            f"path(s) ({dirty[0].strip()}): measure a clean commit"
        )
    return None


class Terminated(BaseException):
    """SIGTERM or SIGHUP arrived: unwind, giving the lease back on the way."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by signal {signum}")
        self.signum = signum


class Refused(RuntimeError):
    """The run cannot start as asked: nothing is measured, nothing reported."""


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
#: Process groups of the mutmut sessions running now.
_live_groups: set[int] = set()


@contextlib.contextmanager
def tracked_group(pgid: int) -> Iterator[None]:
    """Name *pgid* as a running session a second stop signal must kill."""
    _live_groups.add(pgid)
    try:
        yield
    finally:
        _live_groups.discard(pgid)


#: The sessions being started now: a first stop signal waits for them
#: (:func:`_stop_held`), and a second kills each whose pid is already stored.
_starting: list[subprocess.Popen[Any]] = []
#: The first stop signal that arrived while a session was being started.
_held: list[int] = []


def _kill_live_groups() -> None:
    started = (getattr(session, "pid", None) for session in _starting)
    for pgid in _live_groups | {pid for pid in started if pid is not None}:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


@contextlib.contextmanager
def _stop_held(session: subprocess.Popen[Any]) -> Iterator[None]:
    """Hold a first stop signal until the block ends, then raise it.

    ``Popen`` runs ``self.pid = _fork_exec(...)``, and the interpreter runs a
    pending handler between that call and the store. Raised there, the signal
    unwinds with the child running and ``pid`` still ``None``, so nothing can
    kill its session. Held, it unwinds once ``pid`` is stored.

    A held stop always ends the block, even when the start itself failed: the
    start's error becomes its cause. Letting that error win would let the
    caller treat a failed start as a finished module and carry on, with the
    operator's stop already spent.
    """
    _starting.append(session)
    failure: BaseException | None = None
    try:
        yield
    except BaseException as error:
        failure = error
    finally:
        _starting.remove(session)
        held = _held[:]
        _held.clear()
    if held:
        raise Terminated(held[0]) from failure
    if failure is not None:
        raise failure


@contextlib.contextmanager
def stop_on_signals() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into :class:`Terminated` for the life of the block.

    The first signal unwinds, giving every lock back on the way; one that
    lands while a session is being started waits until its pid is known
    (:func:`_stop_held`). A second one while that unwind runs kills the
    tracked sessions, and one being started whose pid is stored, and exits at
    once: the kernel drops the locks with the process.
    """
    stopping: list[int] = []

    def stop(signum: int, frame: FrameType | None) -> None:
        if stopping:
            _kill_live_groups()
            os._exit(128 + signum)
        stopping.append(signum)
        if _starting:
            _held.append(signum)
            return
        raise Terminated(signum)

    previous = {each: signal.signal(each, stop) for each in _STOP_SIGNALS}
    try:
        yield
    finally:
        for each, handler in previous.items():
            signal.signal(each, handler)


def lease_descriptors(*locks: Path) -> tuple[int, ...]:
    """This process's descriptors that hold one of *locks*' exclusive locks.

    A child given one keeps the lock held for as long as it lives, even if
    this process is killed without unwinding.
    """
    targets = {(facts.st_dev, facts.st_ino) for facts in map(os.stat, locks)}
    found = []
    for name in os.listdir("/dev/fd"):
        descriptor = int(name)
        try:
            facts = os.fstat(descriptor)
        except OSError:
            continue
        if (facts.st_dev, facts.st_ino) not in targets:
            continue
        try:  # succeeds only on the open file that already holds the lock
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue
        found.append(descriptor)
    return tuple(found)


# --- the measured suite.


class MeasureError(RuntimeError):
    """The measuring run did not produce a whole, passing measurement."""


@dataclass(frozen=True)
class Item:
    nodeid: str
    markers: frozenset[str]
    seconds: float
    spawned: bool = True  # unknown counts as spawned: never a candidate

    @property
    def file(self) -> str:
        return self.nodeid.split("::", 1)[0]

    @property
    def function(self) -> str:
        """The node id without its parameters: one per test function."""
        return self.nodeid.split("[", 1)[0]


@dataclass
class Coverage:
    """Which test covers which line and arc; ``""`` is import-time code."""

    facts: list[tuple[str, int, int | None]] = field(default_factory=list)
    by_test: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    owners: list[int] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    _ids: dict[tuple[str, int, int | None], int] = field(default_factory=dict)

    def add(self, test: str, fact: tuple[str, int, int | None]) -> None:
        fid = self._ids.get(fact)
        if fid is None:
            fid = self._ids[fact] = len(self.facts)
            self.facts.append(fact)
            self.owners.append(0)
        if fid not in self.by_test[test]:
            self.by_test[test].add(fid)
            self.owners[fid] += 1

    def covers(self, test: str) -> set[int]:
        return self.by_test.get(test, set())

    def unique(self, test: str) -> list[int]:
        return [fid for fid in self.covers(test) if self.owners[fid] == 1]

    def modules(self, test: str) -> set[str]:
        return {self.facts[fid][0] for fid in self.covers(test)}

    def tally(self, fids: Iterable[int]) -> dict[str, int]:
        kinds = [self.facts[fid][2] is None for fid in fids]
        return {"lines": sum(kinds), "arcs": len(kinds) - sum(kinds)}


def measure_argv(project: Path) -> list[str]:
    sources = [p for p in (project / "src", project / "scripts") if p.is_dir()]
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        PLUGIN,
        "-p",
        "no:cacheprovider",
        *(f"--cov={p}" for p in sources),
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=",
        "--durations=0",
        "-q",
    ]


def _plugin_env(extra: dict[str, str]) -> dict[str, str]:
    """The environment a pytest run needs to load this module as a plugin."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (ITEMS_ENV, DESELECT_ENV)
    }
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(SCRIPTS_DIR), env.get("PYTHONPATH")])
    )
    return {**env, **extra}


def _pytest(
    argv: list[str],
    project: Path,
    log: Path,
    env: dict[str, str],
    keep_fds: tuple[int, ...],
) -> int:
    with log.open("w", encoding="utf-8") as sink:
        done = subprocess.run(  # nosec B603 - fixed argv, no shell
            argv,
            cwd=project,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            check=False,
            pass_fds=keep_fds,
        )
    return done.returncode


def measure(project: Path, out: Path, keep_fds: tuple[int, ...] = ()) -> None:
    """Run the whole suite once, writing ``.coverage`` and ``items.json`` to *out*."""
    env = _plugin_env(
        {"COVERAGE_FILE": str(out / ".coverage"), ITEMS_ENV: str(out / "items.json")}
    )
    code = _pytest(measure_argv(project), project, out / "measure.log", env, keep_fds)
    if code != 0:
        raise MeasureError(
            f"the measuring run exited {code}; see {out / 'measure.log'}"
        )


def collect(project: Path, out: Path, keep_fds: tuple[int, ...] = ()) -> set[str]:
    """The node ids the suite collects, without running it."""
    target = out / "collected.json"
    env = _plugin_env({ITEMS_ENV: str(target)})
    argv = [sys.executable, "-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider"]
    code = _pytest(
        [*argv, "--collect-only", "-q"], project, out / "collect.log", env, keep_fds
    )
    if code != 0:
        raise MeasureError(f"collecting the suite exited {code}; see collect.log")
    return set(load_items(target))


def load_items(path: Path) -> dict[str, Item]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("exitstatus") != 0 or not raw.get("items"):
        raise MeasureError(f"{path} records no whole passing run")
    return {
        nodeid: Item(
            nodeid,
            frozenset(entry["markers"]),
            float(entry["seconds"]),
            bool(entry.get("spawned", True)),
        )
        for nodeid, entry in raw["items"].items()
    }


def _names(nodeids: Iterable[str]) -> set[str]:
    """Every spelling :func:`exclusion` matches: node id, function, file."""
    return {
        name
        for nodeid in nodeids
        for name in (nodeid, nodeid.split("[", 1)[0], nodeid.split("::", 1)[0])
    }


def unmatched_caught(caught: frozenset[str], collected: set[str]) -> list[str]:
    """Caught-defect entries that name no collected item: they protect nothing."""
    return sorted(caught - _names(collected))


def _relative(path: str, project: Path) -> str:
    try:
        return Path(path).resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return path


def _measured_files(
    connection: sqlite3.Connection, project: Path, cov: Coverage
) -> dict[int, str]:
    """File id -> project-relative path, minus :data:`OMITTED_FROM_COVERAGE`."""
    files: dict[int, str] = {}
    for fid, path in connection.execute("select id, path from file"):
        if Path(path).resolve() in OMITTED_FROM_COVERAGE:
            cov.omitted.append(_relative(path, project))
        else:
            files[fid] = _relative(path, project)
    return files


def load_coverage(db: Path, project: Path) -> Coverage:
    """Read per-test lines and arcs, leaving out :data:`OMITTED_FROM_COVERAGE`."""
    from coverage.numbits import numbits_to_nums

    cov = Coverage()
    connection = sqlite3.connect(db)
    try:
        files = _measured_files(connection, project, cov)
        contexts = {
            cid: _CONTEXT_PHASE.sub("", ctx)
            for cid, ctx in connection.execute("select id, context from context")
        }
        for file_id, context_id, start, end in connection.execute(
            "select file_id, context_id, fromno, tono from arc"
        ):
            if file_id not in files:
                continue
            test, path = contexts[context_id], files[file_id]
            cov.add(test, (path, start, end))
            for line in (start, end):
                if line > 0:
                    cov.add(test, (path, line, None))
        for file_id, context_id, numbits in connection.execute(
            "select file_id, context_id, numbits from line_bits"
        ):
            if file_id not in files:
                continue
            for line in numbits_to_nums(numbits):
                cov.add(contexts[context_id], (files[file_id], line, None))
    finally:
        connection.close()
    return cov


# --- candidates and exclusions.


def legacy_names(project: Path) -> frozenset[str]:
    ledger = project / "ratchets" / "test_quality.json"
    if not ledger.is_file():
        return frozenset()
    return frozenset(
        json.loads(ledger.read_text(encoding="utf-8")).get("legacy_names", [])
    )


class _Shape(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.Constant(value=None)


def _shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = _Shape().visit(ast.Module(body=list(node.body), type_ignores=[]))
    return ast.dump(body, annotate_fields=False)


def _test_defs(
    tree: ast.Module,
) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    for node in tree.body:
        members = node.body if isinstance(node, ast.ClassDef) else [node]
        prefix = f"{node.name}::" if isinstance(node, ast.ClassDef) else ""
        for member in members:
            if isinstance(
                member, ast.FunctionDef | ast.AsyncFunctionDef
            ) and member.name.startswith("test"):
                yield prefix + member.name, member


def same_shape_functions(project: Path, files: Iterable[str]) -> set[str]:
    """Test functions whose body, constants aside, matches another test's."""
    groups: dict[str, list[str]] = defaultdict(list)
    for relpath in sorted(set(files)):
        path = project / relpath
        if not path.is_file():
            continue
        for name, node in _test_defs(ast.parse(path.read_text(encoding="utf-8"))):
            groups[_shape(node)].append(f"{relpath}::{name}")
    return {fid for members in groups.values() if len(members) > 1 for fid in members}


def exclusion(item: Item, caught: frozenset[str]) -> str | None:
    marked = sorted(item.markers & EXCLUDED_MARKERS)
    if marked:
        return f"marked {marked[0]}"
    if item.file in EXCLUDED_FILES or item.file.startswith(EXCLUDED_PREFIXES):
        return "verify gate or containment test"
    if {item.nodeid, item.function, item.file} & caught:
        return "caught a real defect"
    return None


def candidate_reasons(
    items: dict[str, Item], cov: Coverage, project: Path
) -> dict[str, list[str]]:
    legacy = legacy_names(project)
    shaped = same_shape_functions(project, (item.file for item in items.values()))
    width: dict[str, int] = defaultdict(int)
    for item in items.values():
        width[item.function] += 1
    reasons: dict[str, list[str]] = {}
    for nodeid, item in items.items():
        found = [
            label
            for label, hit in (
                ("legacy round-named file", item.file in legacy),
                ("wide parametrize", width[item.function] >= WIDE_PARAMETRIZE),
                ("same-shape body", item.function in shaped),
                (
                    "adds no line or arc",
                    bool(cov.covers(nodeid)) and not cov.unique(nodeid),
                ),
            )
            if hit
        ]
        if found:
            reasons[nodeid] = found
    return reasons


def keep_coverage(
    ordered: list[str], cov: Coverage
) -> tuple[list[str], dict[str, str]]:
    """Take candidates in order while every line and arc keeps a test."""
    remaining = list(cov.owners)
    taken: list[str] = []
    kept: dict[str, str] = {}
    for nodeid in ordered:
        facts = cov.covers(nodeid)
        if not facts:
            kept[nodeid] = "covers no measured line"
            continue
        lost = [fid for fid in facts if remaining[fid] <= 1]
        if lost:
            tally = cov.tally(lost)
            kept[nodeid] = f"would lose {tally['lines']} lines and {tally['arcs']} arcs"
            continue
        for fid in facts:
            remaining[fid] -= 1
        taken.append(nodeid)
    return taken, kept


# --- the mutation proof.


@dataclass(frozen=True)
class MutationRun:
    """mutmut's verdict per mutant, or ``None`` with a note when it did not finish."""

    statuses: dict[str, str] | None
    note: str = ""


def _kill_group(process: subprocess.Popen[Any]) -> None:
    """SIGKILL the session *process* leads and reap it, waiting a bounded time."""
    if process.pid is None or process.returncode is not None:
        return  # never forked, or the exec failed and Popen reaped it: its pid is free
    with contextlib.suppress(OSError):  # gone, or (macOS: EPERM) only zombies left
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=KILL_WAIT_S)


class _Session(subprocess.Popen[Any]):
    """A ``Popen`` in a session of its own that hands itself to *owner* first.

    An exception can land after the fork but before ``Popen`` returns (a
    starved parent still waits on the exec pipe while the child runs). The
    caller never gets the object then, so it takes it from *owner* to kill
    the session; ``pid`` stays ``None`` if the fork never happened. A stop
    signal is held until ``pid`` is stored (:func:`_stop_held`).
    """

    def __init__(self, owner: list[subprocess.Popen[Any]], *args: Any, **kw: Any):
        owner.append(self)
        with _stop_held(self):
            super().__init__(*args, start_new_session=True, **kw)


def _strip_table(text: str, header: str) -> str:
    """*text* without the TOML table *header* and its keys."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if _TABLE_HEADER.match(line.strip()):
            inside = line.strip().split("#", 1)[0].strip() == header
        if not inside:
            kept.append(line)
    return "\n".join(kept)


def mutmut_pyproject(text: str, overrides: dict[str, Any]) -> str:
    """*text* with its ``[tool.mutmut]`` merged with *overrides*.

    Every key the project sets is kept; ``also_copy`` and
    ``pytest_add_cli_args`` are extended, the rest of *overrides* replaces.
    """
    original = tomllib.loads(text)
    own = dict(original.get("tool", {}).get("mutmut", {}))
    merged = {**own, **overrides}
    merged["also_copy"] = sorted({*own.get("also_copy", []), *overrides["also_copy"]})
    merged["pytest_add_cli_args"] = [
        *own.get("pytest_add_cli_args", []),
        *overrides["pytest_add_cli_args"],
    ]
    body = "\n".join(f"{key} = {json.dumps(value)}" for key, value in merged.items())
    rest = _strip_table(text, "[tool.mutmut]").rstrip()
    result = f"{rest}\n\n[tool.mutmut]\n{body}\n"
    written = tomllib.loads(result)
    if written.get("tool", {}).get("mutmut") != merged:
        raise ValueError("could not rewrite [tool.mutmut] faithfully")
    return result


class Mutmut:
    """Runs mutmut 3 on one module in a work copy of the measured commit."""

    def __init__(
        self,
        project: Path,
        scratch: Path,
        sha: str,
        *,
        executable: Path | None = None,
        guard: Callable[[float], str | None] = lambda seconds: None,
        keep_fds: tuple[int, ...] = (),
    ) -> None:
        self.project = project
        self.scratch = scratch
        self.sha = sha
        self.command = (
            [str(executable)]
            if executable
            else [sys.executable, "-P", "-c", _MUTMUT_LAUNCHER]
        )
        self.guard = guard
        self.keep_fds = keep_fds
        self._runs = 0
        self._archive: tuple[Path, str] | None = None

    def close(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _tree(self) -> tuple[Path, str]:
        """The measured commit's tracked tree as a tar, and the project's prefix."""
        if self._archive is None:
            top = Path(_git(self.project, "rev-parse", "--show-toplevel").strip())
            prefix = _git(self.project, "rev-parse", "--show-prefix").strip()
            self.scratch.mkdir(parents=True, exist_ok=True)
            tar = self.scratch / "tree.tar"
            _git(top, "archive", "--format=tar", "-o", str(tar), self.sha)
            self._archive = (tar, prefix)
        return self._archive

    def _prepare(
        self, run_dir: Path, module: str, tests: list[str], deselect: list[str]
    ) -> Path:
        tar, prefix = self._tree()
        tree = run_dir / "tree"
        with tarfile.open(tar) as archive:
            archive.extractall(tree, filter="data")
        work = tree / prefix
        entries = sorted(
            p.name + "/" if p.is_dir() else p.name
            for p in work.iterdir()
            if p.name != "mutants"
        )
        _link_repo_root(tree, work, prefix)
        (run_dir / "deselect.json").write_text(json.dumps(deselect), encoding="utf-8")
        pyproject = work / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        overrides = {
            "paths_to_mutate": [module],
            "tests_dir": tests,
            "also_copy": entries,
            "pytest_add_cli_args": ["-p", "no:cacheprovider", "-p", PLUGIN],
        }
        pyproject.write_text(mutmut_pyproject(text, overrides), encoding="utf-8")
        return work

    def _call(
        self, argv: list[str], work: Path, deselect: Path, timeout: float
    ) -> tuple[int | None, str]:
        # Output goes to a file, never a pipe: a descendant that left mutmut's
        # session would hold a pipe open, and reading it would wait on that.
        log = deselect.parent / f"mutmut-{argv[0]}.log"
        owner: list[subprocess.Popen[Any]] = []
        try:
            with log.open("w", encoding="utf-8") as sink:
                process = _Session(  # nosec B603 - fixed argv, no shell
                    owner,
                    [*self.command, *argv],
                    cwd=work,
                    env=_plugin_env({DESELECT_ENV: str(deselect)}),
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    pass_fds=self.keep_fds,
                )
            with tracked_group(process.pid):
                code = process.wait(timeout=max(timeout, 0.001))
        except subprocess.TimeoutExpired:
            _kill_group(owner[0])
            return None, ""
        except BaseException:
            # An interrupted report gives the verify lease back on the way out,
            # so mutmut's own session must not outlive it, even one interrupted
            # while it was still starting.
            if owner:
                _kill_group(owner[0])
            raise
        return code, log.read_text(encoding="utf-8", errors="replace")

    def run(
        self, module: str, tests: list[str], deselect: list[str], timeout: float
    ) -> MutationRun:
        refused = self.guard(timeout)
        if refused:
            return MutationRun(None, f"not run: {refused}")
        self._runs += 1
        run_dir = self.scratch / f"run-{self._runs}"
        try:
            work = self._prepare(run_dir, module, tests, deselect)
            return self._verdicts(work, run_dir / "deselect.json", timeout)
        except (GitError, OSError, ValueError, tarfile.TarError) as error:
            return MutationRun(None, f"mutmut could not run: {error}")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def _verdicts(self, work: Path, deselect: Path, timeout: float) -> MutationRun:
        started = time.monotonic()
        code, output = self._call(["run"], work, deselect, timeout)
        if code is None:
            return MutationRun(None, "over its cap")
        if code != 0:
            return MutationRun(
                None, f"mutmut run exited {code}: {output.strip()[-300:]}"
            )
        left = timeout - (time.monotonic() - started)
        code, output = self._call(["results", "--all", "true"], work, deselect, left)
        if code is None:
            return MutationRun(None, "over its cap")
        if code != 0:
            return MutationRun(
                None, f"mutmut results exited {code}: {output.strip()[-300:]}"
            )
        statuses = {
            m.group(1): m.group(2).strip()
            for m in map(_RESULT_LINE.match, output.splitlines())
            if m
        }
        if not statuses:
            return MutationRun(None, "mutmut reported no mutant")
        return MutationRun(statuses)


def _link_repo_root(tree: Path, work: Path, prefix: str) -> None:
    """Make the repo root's other entries visible beside the project copy.

    mutmut runs the tests from ``<project>/mutants/``, so a test's
    ``Path(__file__).parents[2]`` is the project copy, not the repo root.
    """
    if not prefix:
        return
    own = prefix.split("/", 1)[0]
    for entry in tree.iterdir():
        target = work / entry.name
        if entry.name != own and not target.exists() and not target.is_symlink():
            target.symlink_to(entry)


def base_doubt(first: dict[str, str], second: dict[str, str]) -> str | None:
    """Why two base runs prove nothing about their module, or ``None``."""
    if first != second:
        differ = sorted(n for n in {*first, *second} if first.get(n) != second.get(n))
        return f"two base runs disagree on {len(differ)} mutant(s): " + ", ".join(
            f"{n} ({first.get(n)} / {second.get(n)})" for n in differ[:3]
        )
    odd = Counter(s for s in first.values() if s not in CLEAN_VERDICTS)
    if odd:
        return "the base run is no clean verdict: " + ", ".join(
            f"{count} {status}" for status, count in sorted(odd.items())
        )
    if "killed" not in first.values():
        return "the base run killed no mutant"
    return None


def _holds(was: str, now: str | None) -> bool:
    """A trial verdict keeps the base's: a kill stays a kill, a survivor survives."""
    return now == "killed" if was == "killed" else now in SURVIVING


def changed_verdicts(base: dict[str, str], trial: dict[str, str]) -> list[str]:
    """Mutants whose trial verdict breaks the base's: a lost kill, or any oddity."""
    return sorted(n for n, was in base.items() if not _holds(was, trial.get(n)))


def _kept_reason(
    module: str, base: dict[str, str], trial: dict[str, str], changed: list[str]
) -> str:
    shown = ", ".join(changed[:3])
    if all(base[n] == "killed" and trial.get(n) in SURVIVING for n in changed):
        return f"alone kills {len(changed)} mutant(s) in {module}: {shown}"
    detail = ", ".join(f"{n} ({base[n]} -> {trial.get(n)})" for n in changed[:3])
    return (
        f"not removable: without it {len(changed)} mutant(s) in {module} "
        f"change verdict: {detail}"
    )


@dataclass
class ModuleProof:
    module: str
    proven: bool
    note: str = ""
    mutants: int = 0
    base_survivors: int = 0
    base_killed: int = 0
    removable: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)

    def unproven(self, note: str) -> ModuleProof:
        return ModuleProof(
            self.module,
            False,
            note,
            self.mutants,
            self.base_survivors,
            self.base_killed,
        )


def _base_proof(
    module: str, tests: list[str], mutmut: Mutmut, left: Callable[[], float]
) -> tuple[ModuleProof, dict[str, str]]:
    runs = []
    for _ in range(2):
        run = mutmut.run(module, tests, [], left())
        if run.statuses is None:
            return ModuleProof(module, False, run.note), {}
        runs.append(run.statuses)
    base = runs[0]
    killed = sum(status == "killed" for status in base.values())
    doubt = base_doubt(*runs)
    proof = ModuleProof(
        module, doubt is None, doubt or "", len(base), len(base) - killed, killed
    )
    return proof, base


def prove_module(
    module: str,
    candidates: list[str],
    tests: list[str],
    mutmut: Mutmut,
    cap: float,
    clock: Callable[[], float] = time.monotonic,
) -> ModuleProof:
    """Find the candidates whose removal leaves every one of *module*'s verdicts."""
    deadline = clock() + cap
    proof, base = _base_proof(module, tests, mutmut, lambda: deadline - clock())
    if not proof.proven:
        return proof
    queue = [list(candidates)]
    while queue:
        group = queue.pop(0)
        trial = mutmut.run(module, tests, proof.removable + group, deadline - clock())
        if trial.statuses is None:
            return proof.unproven(trial.note)
        if set(trial.statuses) != set(base):
            return proof.unproven("a trial reported a different set of mutants")
        changed = changed_verdicts(base, trial.statuses)
        if not changed:
            proof.removable.extend(group)
        elif len(group) == 1:
            proof.kept[group[0]] = _kept_reason(module, base, trial.statuses, changed)
        else:
            half = len(group) // 2
            queue[:0] = [group[:half], group[half:]]
    return proof


# --- the report.


@dataclass
class Report:
    sha: str
    items: dict[str, Item]
    reasons: dict[str, list[str]]
    excluded: dict[str, str]
    removable: list[str]
    kept: dict[str, str]
    modules: dict[str, ModuleProof]
    cov: Coverage
    caught_defects: str = ""

    def payload(self) -> dict[str, Any]:
        files: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"removable": 0, "seconds": 0.0}
        )
        functions: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"removable": 0, "seconds": 0.0, "items": []}
        )
        for nodeid in self.removable:
            item = self.items[nodeid]
            for bucket in (files[item.file], functions[item.function]):
                bucket["removable"] += 1
                bucket["seconds"] = round(bucket["seconds"] + item.seconds, 3)
            functions[item.function]["items"].append(nodeid)
        return {
            "sha": self.sha,
            "items": len(self.items),
            "candidates": len(self.reasons),
            "caught_defects": self.caught_defects,
            "omitted_from_coverage": sorted(self.cov.omitted),
            "excluded": self.excluded,
            "removable": [self._entry(nodeid) for nodeid in self.removable],
            "not_removable": self.kept,
            "files": dict(files),
            "functions": dict(functions),
            "modules": {
                name: self._module(proof) for name, proof in self.modules.items()
            },
            "xdist_seconds_saved": round(
                sum(self.items[n].seconds for n in self.removable), 3
            ),
        }

    def _entry(self, nodeid: str) -> dict[str, Any]:
        tally = self.cov.tally(self.cov.covers(nodeid))
        modules = sorted(self.cov.modules(nodeid))
        return {
            "nodeid": nodeid,
            "seconds": round(self.items[nodeid].seconds, 3),
            "reasons": self.reasons[nodeid],
            "covered_elsewhere": tally,
            "modules": modules,
            "mutation_proof": {
                name: {
                    "mutants": self.modules[name].mutants,
                    "killed": self.modules[name].base_killed,
                }
                for name in modules
            },
        }

    @staticmethod
    def _module(proof: ModuleProof) -> dict[str, Any]:
        return {
            "proven": proof.proven,
            "note": proof.note,
            "mutants": proof.mutants,
            "base_killed": proof.base_killed,
            "base_survivors": proof.base_survivors,
            "removable": len(proof.removable),
        }


def _prove_modules(
    taken: list[str], cov: Coverage, mutmut: Mutmut, cap: float
) -> tuple[dict[str, ModuleProof], dict[str, set[str]]]:
    """Prove each module the taken set covers, with the test files that cover it."""
    touches = {nodeid: cov.modules(nodeid) for nodeid in cov.by_test if nodeid}
    test_files: dict[str, set[str]] = defaultdict(set)
    for nodeid, modules_hit in touches.items():
        for module in modules_hit:
            test_files[module].add(nodeid.split("::", 1)[0])
    by_module: dict[str, list[str]] = defaultdict(list)
    for nodeid in taken:
        for module in sorted(touches[nodeid]):
            by_module[module].append(nodeid)
    modules = {
        module: prove_module(module, members, sorted(test_files[module]), mutmut, cap)
        for module, members in sorted(by_module.items())
    }
    return modules, touches


def _blocker(nodeid: str, proofs: list[ModuleProof]) -> str | None:
    """Why *nodeid* stays, or ``None`` when every module it covers lets it go."""
    for proof in proofs:
        if not proof.proven:
            return f"not proven: {proof.module} {proof.note}"
        if nodeid not in proof.removable:
            return proof.kept.get(nodeid, f"not proven in {proof.module}")
    return None


def _ordered(
    items: dict[str, Item], reasons: dict[str, list[str]], excluded: dict[str, str]
) -> tuple[list[str], set[str]]:
    """Candidates slowest first, and those that start a process (never taken)."""
    open_ = [n for n in reasons if n not in excluded]
    spawning = {n for n in open_ if items[n].spawned}
    ordered = sorted(
        (n for n in open_ if n not in spawning), key=lambda n: (-items[n].seconds, n)
    )
    return ordered, spawning


def build_report(
    sha: str,
    project: Path,
    items: dict[str, Item],
    cov: Coverage,
    caught: frozenset[str],
    mutmut: Mutmut,
    cap: float,
) -> Report:
    reasons = candidate_reasons(items, cov, project)
    excluded = {n: why for n in reasons if (why := exclusion(items[n], caught))}
    ordered, spawning = _ordered(items, reasons, excluded)
    taken, kept = keep_coverage(ordered, cov)
    kept.update(dict.fromkeys(spawning, SPAWNED))
    modules, touches = _prove_modules(taken, cov, mutmut, cap)
    removable = []
    for nodeid in taken:
        why = _blocker(nodeid, [modules[m] for m in sorted(touches[nodeid])])
        if why is None:
            removable.append(nodeid)
        else:
            kept[nodeid] = why
    return Report(sha, items, reasons, excluded, removable, kept, modules, cov)


def _table(head: str, rows: Iterable[str]) -> list[str]:
    columns = head.count("|") - 1
    return [head, "|" + "---|" * columns, *rows, ""]


def _proof_cell(entry: dict[str, Any]) -> str:
    return "; ".join(
        f"`{name}` {row['killed']}/{row['mutants']} killed"
        for name, row in entry["mutation_proof"].items()
    )


def render_markdown(payload: dict[str, Any]) -> str:
    """The report a person reads before deleting anything."""
    lines = [
        f"# Test-value report at {payload['sha']}",
        "",
        "**Candidates for a person to review — nothing here is deleted "
        "automatically.**",
        "",
        f"{payload['items']} items, {payload['candidates']} candidates, "
        f"{len(payload['excluded'])} excluded, {len(payload['removable'])} removable.",
        f"Removable set saves {payload['xdist_seconds_saved']} s of the xdist phase.",
        f"Caught-defect list: {payload['caught_defects']}.",
        "",
        "## What this report does not measure",
        "",
        *(f"- {line}" for line in NOT_MEASURED),
        "",
        "## Removable, per file",
        "",
        *_table(
            "| File | Items | Seconds |",
            (
                f"| `{name}` | {row['removable']} | {row['seconds']} |"
                for name, row in sorted(payload["files"].items())
            ),
        ),
        "## Removable, per function",
        "",
        *_table(
            "| Function | Items | Seconds |",
            (
                f"| `{name}` | {row['removable']} | {row['seconds']} |"
                for name, row in sorted(payload["functions"].items())
            ),
        ),
        "## Removable items and their proof",
        "",
        *_table(
            "| Item | Seconds | Lines and arcs covered elsewhere "
            "| Mutation-proven in |",
            (
                f"| `{e['nodeid']}` | {e['seconds']} | "
                f"{e['covered_elsewhere']['lines']} lines, "
                f"{e['covered_elsewhere']['arcs']} arcs | {_proof_cell(e)} |"
                for e in payload["removable"]
            ),
        ),
        "## Not removable",
        "",
        *_table(
            "| Item | Why it stays |",
            (
                f"| `{name}` | {why} |"
                for name, why in sorted(payload["not_removable"].items())
            ),
        ),
        "## Excluded, never candidates",
        "",
        *_table(
            "| Item | Why |",
            (
                f"| `{name}` | {why} |"
                for name, why in sorted(payload["excluded"].items())
            ),
        ),
        "## Mutation proof, per module",
        "",
        *_table(
            "| Module | Proven | Mutants | Base killed | Base survivors "
            "| Removable | Note |",
            (
                f"| `{name}` | {row['proven']} | {row['mutants']} | "
                f"{row['base_killed']} | {row['base_survivors']} | "
                f"{row['removable']} | {row['note']} |"
                for name, row in sorted(payload["modules"].items())
            ),
        ),
    ]
    return "\n".join(lines).rstrip() + "\n"


# --- the command.


def quiet_hour(start: datetime, seconds: float = 0.0) -> str | None:
    """Why a job of *seconds* from *start* would run into 03:00-04:00, or ``None``."""
    three = start.replace(hour=QUIET_HOUR, minute=0, second=0, microsecond=0)
    if start >= three + timedelta(hours=1):
        three += timedelta(days=1)
    if start + timedelta(seconds=seconds) > three:
        return f"a run of {seconds:.0f} s from {start:%H:%M} would reach 03:00-04:00"
    return None


def refusal(now: datetime, load: float) -> str | None:
    if MEASURE_CUTOFF_HOUR <= now.hour < QUIET_HOUR + 1:
        return "the measuring run starts only before 01:00 or from 04:00 on"
    if load > MAX_LOAD:
        return f"load average {load:.1f} is above {MAX_LOAD:.0f}"
    return None


def _caught(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    text = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in text if line.strip() and not line.startswith("#")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        epilog="Starts only before 01:00 or from 04:00 on; no mutmut run starts "
        "unless its cap ends before 03:00.",
    )
    parser.add_argument("--project", type=Path, default=PACKAGE_ROOT)
    parser.add_argument(
        "--out", type=Path, help="default: <work_root>/test-value/<sha>/"
    )
    parser.add_argument(
        "--lease", type=Path, help="default: the local merge verify's lease"
    )
    parser.add_argument(
        "--queue-lock",
        type=Path,
        help="default: the merge-queue lock of the project's kew.toml",
    )
    parser.add_argument(
        "--cap", type=float, default=DEFAULT_CAP_S, help="mutmut seconds per module"
    )
    parser.add_argument(
        "--mutmut", type=Path, help="mutmut executable (default: beside python)"
    )
    caught = parser.add_mutually_exclusive_group()
    caught.add_argument(
        "--caught-defects",
        type=Path,
        help="node ids or test functions that caught a defect (required)",
    )
    caught.add_argument(
        "--no-caught-defects",
        action="store_true",
        help="run with no caught-defect list, on purpose",
    )
    return parser


def _default_out(sha: str) -> Path:
    from kew import work_dirs

    return work_dirs.work_root() / "test-value" / sha


def _default_lease() -> Path:
    from kew import work_dirs

    return work_dirs.verify_lease_path()


def _default_queue_lock(project: Path) -> Path:
    """The merge-queue lock the drive and the nightly take, from kew.toml."""
    from kew.config import load_config
    from kew.graph.driver.merge_queue import merge_queue_lock_path

    top = Path(_git(project, "rev-parse", "--show-toplevel").strip())
    found = next(
        (p for p in (project / "kew.toml", top / "kew.toml") if p.is_file()), None
    )
    return merge_queue_lock_path(load_config(found))


def _free_bytes(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _check_caught(args: argparse.Namespace, out: Path, keep: tuple[int, ...]) -> None:
    """Refuse a caught-defect entry that names no collected test."""
    if not args.caught:
        return
    unmatched = unmatched_caught(args.caught, collect(args.project, out, keep))
    if unmatched:
        raise Refused(
            f"{len(unmatched)} caught-defect entr(y/ies) name no collected test, "
            "so they would protect nothing: " + ", ".join(unmatched)
        )


def _measure_and_report(
    args: argparse.Namespace,
    out: Path,
    sha: str,
    now: Callable[[], datetime],
    keep_fds: tuple[int, ...],
) -> Report:
    _check_caught(args, out, keep_fds)
    measure(args.project, out, keep_fds)
    if head_sha(args.project) != sha:
        raise MeasureError(f"HEAD moved off {sha} during the measuring run")
    items = load_items(out / "items.json")
    cov = load_coverage(out / ".coverage", args.project)
    mutmut = Mutmut(
        args.project,
        out / "mutmut",
        sha,
        executable=args.mutmut,
        guard=lambda seconds: quiet_hour(now(), seconds),
        keep_fds=keep_fds,
    )
    try:
        report = build_report(
            sha, args.project, items, cov, args.caught, mutmut, args.cap
        )
    finally:
        mutmut.close()
    report.caught_defects = (
        str(args.caught_defects)
        if args.caught_defects
        else "none, by --no-caught-defects"
    )
    return report


def _locked_report(
    args: argparse.Namespace, out: Path, sha: str, now: Callable[[], datetime]
) -> Report:
    """Hold the merge-queue lock, then the verify lease, for the whole run."""
    from kew.graph.driver.merge_queue import merge_queue_lock
    from kew.verify_lease import hold_verify_lease

    try:
        queue = args.queue_lock or _default_queue_lock(args.project)
    except Exception as error:  # noqa: BLE001 - any failure here refuses
        raise Refused(f"cannot find the merge-queue lock: {error}") from error
    with merge_queue_lock(queue) as held:
        if not held.acquired:
            raise Refused(f"the merge queue is held: {held.reason}")
        lease = args.lease or _default_lease()
        with hold_verify_lease(f"test-value {sha}", path=lease):
            keep_fds = lease_descriptors(queue, lease)
            if len(keep_fds) < 2:
                raise MeasureError(f"no descriptor of this process holds {lease}")
            return _measure_and_report(args, out, sha, now, keep_fds)


def run(
    args: argparse.Namespace,
    out: Path,
    sha: str,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    from kew.verify_lease import VerifyLeaseBusy

    try:
        with stop_on_signals():
            report = _locked_report(args, out, sha, now)
    except (VerifyLeaseBusy, Refused) as busy:
        print(f"refused: {busy}", file=sys.stderr)
        return 2
    except (MeasureError, GitError) as error:
        print(f"measuring run failed: {error}", file=sys.stderr)
        return 1
    except Terminated as stop:
        print(f"stopped: {stop}; nothing reported", file=sys.stderr)
        return 128 + stop.signum
    payload = report.payload()
    (out / "report.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    print(
        f"{len(payload['removable'])} removable, "
        f"{payload['xdist_seconds_saved']} s saved: {out / 'report.md'}"
    )
    return 0


def _absolute(args: argparse.Namespace) -> None:
    """Resolve every path argument now: later steps run in other directories."""
    for name in ("project", "out", "lease", "queue_lock", "mutmut", "caught_defects"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())


def _start_refusal(args: argparse.Namespace, now: datetime, load: float) -> str | None:
    if args.caught_defects is None and not args.no_caught_defects:
        return (
            "pass --caught-defects <file> (the tests that caught a real defect), "
            "or --no-caught-defects to run without one on purpose"
        )
    try:
        args.caught = _caught(args.caught_defects)
    except (OSError, UnicodeDecodeError) as error:
        return f"cannot read the caught-defect list: {error}"
    if args.caught_defects and not args.caught:
        return (
            f"the caught-defect list {args.caught_defects} names no test: "
            "use --no-caught-defects on purpose"
        )
    return refusal(now, load) or tree_refusal(args.project)


#: Files an earlier run left in the out dir; a failed re-run must not leave them.
_EARLIER_OUTPUT = (
    "report.json",
    "report.md",
    "items.json",
    "collected.json",
    ".coverage",
)


def main(
    argv: list[str] | None = None,
    now: Callable[[], datetime] = datetime.now,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    free_bytes: Callable[[Path], int] = _free_bytes,
) -> int:
    args = _parser().parse_args(argv)
    _absolute(args)
    refused = _start_refusal(args, now(), load())
    if refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    sha = head_sha(args.project)
    out = args.out or _default_out(sha)
    free = free_bytes(out)
    if free < MIN_FREE_BYTES:
        print(
            f"refused: {free / 1024**3:.1f} GiB free under {out}, "
            f"below {MIN_FREE_BYTES / 1024**3:.0f} GiB",
            file=sys.stderr,
        )
        return 2
    out.mkdir(parents=True, exist_ok=True)
    for name in _EARLIER_OUTPUT:
        (out / name).unlink(missing_ok=True)
    return run(args, out, sha, now)


if __name__ == "__main__":
    sys.exit(main())
