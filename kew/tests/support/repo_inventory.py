"""One repository inventory per session, shared by the repo-wide gates (#2340).

Several gates here ask a question about the WHOLE repository: which tracked
files the gate runners name (``tests/test_merge_gate_paths.py``), which
collected test carries ``serial`` (``tests/test_serial_phase.py``), which
carries ``requires_switchyard_image``
(``tests/test_switchyard_image_skip.py``). Each answer was recomputed per
assertion — a ``git ls-files`` per sweep, a nested whole-tree ``pytest
--collect-only`` per probe — and the repeated scanning, not the assertions,
dominated the parallel phase: nine tests, about 190 CPU-seconds, with two of
them 40 s each (#2340).

So each inventory is computed once per session and the gates assert against
it. Nothing about what they assert changes; a probe that used to ask a child
pytest "what does ``-m serial tests`` select?" now reads the answer off a
collection that already happened.

The collected-id inventory comes from the session's OWN collection whenever
that collection covered every test module on disk. A full run has already
imported and collected everything a child pytest would be asked for — real
node ids, parametrised ids and all, with the markers ``conftest`` applies as
well as the hand-written ones — and ``conftest`` records it BEFORE any
deselection, so the opt-in Switchyard tests are in the record whatever
``KEW_SWITCHYARD_IMAGE`` says. A narrowed run (one module under development, a
nested pytest driving two ids) has no such inventory. It takes the record its
parent handed down, when a parent handed one down (``share``), and only when
neither is available does one child ``pytest --collect-only -q tests`` supply
it, writing its record to the sink this module names.

Each of the three paths is the same answer, and every one of them is refused
unless it covers every test module on disk: a record that is missing a module
cannot say which test in the suite carries a marker, and a gate that answered
from one would pass on a test it never saw.
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
KEW_DIR = TESTS_DIR.parent
REPO_ROOT = KEW_DIR.parent

#: Where a child pytest writes the collection it just made. Set by
#: :func:`child_inventory` and obeyed by :func:`record_collection`, which runs
#: in the child's ``conftest``.
INVENTORY_SINK_ENV = "KEW_TEST_INVENTORY_SINK"

#: Where a child pytest may READ a collection its parent already made, so a
#: nested run of two ids does not collect the tree to answer one gate. Pointed
#: at a file by :func:`share`, and taken only when it fits (see
#: :func:`shared_inventory`).
INVENTORY_SHARE_ENV = "KEW_TEST_INVENTORY"

#: This session's own collection, as recorded by ``tests/conftest.py``.
_LIVE: dict[str, frozenset[str]] = {}

#: The environment :func:`share` hands a child, once it has written the file.
_SHARED: dict[str, str] = {}

#: The machine's temp root, read at import time. The suite points
#: ``tempfile.tempdir`` at each test's own ``tmp_path`` (``_hermetic_temp_root``
#: in ``tests/conftest.py``), which is gone by the time the NEXT test launches
#: a child, so a record shared with every child of this session cannot live
#: there. Same reason ``conftest`` takes its session directories at import.
_TEMP_ROOT = tempfile.gettempdir()


def record_collection(items: Iterable[pytest.Item]) -> None:
    """Remember this session's collection, before anything deselects from it.

    Called from ``pytest_collection_modifyitems`` in ``tests/conftest.py``
    once the serial marker has been applied and before the Switchyard
    deselection, so the record is what an opted-in, unfiltered run collects.
    ``tests/test_switchyard_image_skip.py`` pins that: it reads the marked set
    out of this record on a run that deselected every one of those tests.
    """
    inventory = {
        item.nodeid: frozenset(mark.name for mark in item.iter_markers())
        for item in items
    }
    _LIVE.clear()
    _LIVE.update(inventory)
    sink = os.environ.get(INVENTORY_SINK_ENV)
    if sink:
        _write(Path(sink), inventory)


def _write(path: Path, inventory: dict[str, frozenset[str]]) -> None:
    """Write a record out, naming the checkout it describes.

    The root travels with it because a record only answers for the tree it was
    taken in: a child pytest running in a COPY of this checkout — the review
    environment tests make those — must not read this one as its own.
    """
    path.write_text(
        json.dumps(
            {
                "root": str(KEW_DIR),
                "items": {node: sorted(marks) for node, marks in inventory.items()},
            }
        ),
        encoding="utf-8",
    )


def _read(path: Path) -> dict[str, frozenset[str]] | None:
    """A record written by :func:`_write`, or None if it answers for elsewhere."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("root") != str(KEW_DIR):
        return None
    return {node: frozenset(marks) for node, marks in payload["items"].items()}


def share() -> dict[str, str]:
    """Environment for a child pytest, pointing it at this session's record.

    A child that collects a subset of the tree cannot answer "which test in
    the suite carries this marker", so it collects the whole tree again to
    answer it — which is the whole cost of a nested run that happens to drive
    a gate (#2340). This hands it the record this session already has. Empty
    when this session has no complete record to hand over, and the child then
    collects for itself.

    Written once and reused, but re-written if the file is gone: a record that
    has quietly vanished costs every later child a whole collection, and that
    is invisible except as a slow suite.
    """
    named = _SHARED.get(INVENTORY_SHARE_ENV)
    if named is not None and Path(named).is_file():
        return dict(_SHARED)
    if not covers_every_test_module(_LIVE):
        return {}
    directory = Path(tempfile.mkdtemp(prefix="kew-inventory-", dir=_TEMP_ROOT))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    path = directory / "inventory.json"
    _write(path, dict(_LIVE))
    _SHARED[INVENTORY_SHARE_ENV] = str(path)
    return dict(_SHARED)


def shared_inventory() -> dict[str, frozenset[str]] | None:
    """The record a parent session handed this one, if it fits.

    It must exist, name this checkout, and cover every test module on disk —
    the same condition this session's own collection has to meet. Anything
    else and the caller collects for itself: a record that is missing a module
    cannot say what carries a marker, and silently answering from one is how
    these gates would go green on a test they never saw.
    """
    named = os.environ.get(INVENTORY_SHARE_ENV)
    if not named or not Path(named).is_file():
        return None
    inventory = _read(Path(named))
    if inventory is None or not covers_every_test_module(inventory):
        return None
    return inventory


@functools.cache
def test_modules() -> tuple[Path, ...]:
    """Every test module in the suite, listed once."""
    return tuple(sorted(TESTS_DIR.rglob("test_*.py")))


@functools.cache
def tracked_paths() -> tuple[str, ...]:
    """Every tracked path, repo-root-relative — one ``git ls-files`` a session."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git checkout; nothing to scan")
    return tuple(sorted(path for path in result.stdout.split("\0") if path))


def covers_every_test_module(node_ids: Iterable[str]) -> bool:
    """Did a collection reach every test module on disk?

    The one condition under which a collection is the whole suite's. ``-k`` and
    ``-m`` do not narrow collection — they deselect from it, after the record
    above is taken — so a collection holding every module holds every item in
    every module. A collection narrowed by path, ``--ignore`` or a nested run's
    explicit ids does not, and falls back to the child collection.
    """
    reached = {node_id.split("::")[0] for node_id in node_ids}
    return all(
        module.relative_to(KEW_DIR).as_posix() in reached for module in test_modules()
    )


@functools.cache
def collected_marks() -> dict[str, frozenset[str]]:
    """Every collected node id in the suite → the marker names it carries."""
    if _LIVE and covers_every_test_module(_LIVE):
        return dict(_LIVE)
    handed_over = shared_inventory()
    if handed_over is not None:
        return handed_over
    return child_inventory()


def child_inventory(targets: tuple[str, ...] = ("tests",)) -> dict[str, frozenset[str]]:
    """Collect *targets* in a child pytest and read its record back.

    The fallback for a session that collected a subset. Opted in, so the child
    collects the Switchyard tests rather than deselecting them: the record is
    what an opted-in run collects either way, and a child that agrees with the
    live record keeps the two paths interchangeable.
    """
    with tempfile.TemporaryDirectory() as directory:
        sink = Path(directory) / "inventory.json"
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                *targets,
            ],
            cwd=KEW_DIR,
            env=dict(os.environ)
            | {INVENTORY_SINK_ENV: str(sink), "KEW_SWITCHYARD_IMAGE": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        assert proc.returncode == 0 and sink.exists(), (
            f"the inventory collection failed (exit {proc.returncode}):\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
        collected = _read(sink)
    assert collected is not None, "the child collection answered for another tree"
    return collected


def ids_with_mark(mark: str) -> set[str]:
    """Every collected id carrying *mark* — what ``-m <mark> tests`` selects."""
    return {node for node, marks in collected_marks().items() if mark in marks}


def ids_in(modules: Iterable[str | Path]) -> set[str]:
    """Every collected id in *modules*, named rootdir-relative (``tests/...``)."""
    prefixes = tuple(f"{Path(module).as_posix()}::" for module in modules)
    return {node for node in collected_marks() if node.startswith(prefixes)}
