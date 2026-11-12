"""#2340 gate — the shared inventory says what a real collection says.

The repo-wide gates stopped driving a nested whole-tree ``pytest
--collect-only`` per probe and read ``support.repo_inventory`` instead
(#2340). That only holds their assertions if the inventory is the same answer:
the same node ids, the same markers, from either of the two ways it can be
produced — this session's own collection, or one child collection when this
session collected a subset.

So the checks here compare the inventory against a child pytest's real
selection, module by module, over both marker sources the gates depend on: the
``serial`` marker ``conftest`` applies from the module source, and the
``requires_switchyard_image`` marker a module carries by hand. They also pin
the condition the live record is trusted under, because trusting a NARROWED
collection is how one of these gates would go quietly blind: a marked test in
a module the session never collected would be missing from the inventory, and
every "exactly this set is marked" assertion would pass without it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import repo_inventory
from support.repo_inventory import KEW_DIR, TESTS_DIR

pytestmark = pytest.mark.policy

# A module that carries a marker by hand, pinned by name because the id-for-id
# check below compares against ids pinned in tests/test_switchyard_image_skip.py.
MARKED_MODULE = "tests/test_switchyard_sidecar.py"


def _a_module_carrying(mark: str) -> str:
    """The smallest module in the record that carries *mark*.

    Resolved from the record rather than pinned by name, because which module
    carries a marker is exactly the thing that moves: this gate pinned a
    Textual pilot module for ``serial``, #2342 gave those modules bounded
    waits and moved them to the parallel phase, and the pin then failed on a
    name rather than on a fact. The question here — does the record agree
    with a real collection? — is answered by any carrier.

    Smallest by collected-id count so the child collection stays cheap, and
    tie-broken by name so every worker makes the same choice.
    """
    carriers = {node.split("::")[0] for node in repo_inventory.ids_with_mark(mark)}
    assert carriers, f"no collected test carries {mark}; update this gate"
    return min(
        carriers, key=lambda module: (len(repo_inventory.ids_in([module])), module)
    )


def _child_selection(args: list[str]) -> set[str]:
    """What a real pytest selects for *args*, opted into the image tests."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=KEW_DIR,
        env=dict(os.environ) | {"KEW_SWITCHYARD_IMAGE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"collection failed (exit {proc.returncode}):\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    return {line for line in proc.stdout.splitlines() if "::" in line}


@pytest.mark.parametrize("mark", ["serial", "requires_switchyard_image"])
def test_the_inventory_is_what_a_real_collection_selects(mark: str) -> None:
    """The gates' question, asked of the real thing: what does -m select?"""
    module = _a_module_carrying(mark)
    selected = _child_selection(["-m", mark, module])
    assert selected, f"{module} selects nothing for -m {mark}; has it moved?"
    assert repo_inventory.ids_in([module]) & repo_inventory.ids_with_mark(mark) == (
        selected
    )


def test_the_inventory_names_the_ids_a_real_collection_names() -> None:
    """The other half: the ids themselves, which are the gates' vocabulary.

    A pinned id (``EXPECTED_MARKED`` in
    ``tests/test_switchyard_image_skip.py``) is compared string for string, so
    a record that spelled a parametrised id differently would be a gate that
    cannot match what it pins.
    """
    assert repo_inventory.ids_in([MARKED_MODULE]) == _child_selection([MARKED_MODULE])


def test_the_one_pinned_module_still_carries_the_marker_it_is_pinned_for() -> None:
    """The failure mode this gate had, said out loud.

    A module name pinned for a marker it stopped carrying makes the checks
    here fail on the name rather than on the thing under test: #2342 moved
    the TUI pilot modules into the parallel phase and a pin for ``serial``
    went stale with them. The one remaining pin is checked directly, so a
    stale pin says so instead of reading as a disagreeing inventory.
    """
    assert repo_inventory.ids_in([MARKED_MODULE]) & repo_inventory.ids_with_mark(
        "requires_switchyard_image"
    ), f"{MARKED_MODULE} no longer carries requires_switchyard_image; repin it"


def test_the_inventory_names_every_test_module_in_the_suite() -> None:
    """Whichever way it was produced, it covers the tree the gates sweep."""
    inventory = repo_inventory.collected_marks()
    assert repo_inventory.covers_every_test_module(inventory)
    assert len(repo_inventory.test_modules()) > 300, "the module sweep found nothing"
    assert len(inventory) > 10_000, "the inventory is not the whole suite"


def test_a_narrowed_collection_is_never_taken_for_the_suites() -> None:
    """The condition the live record is trusted under, from both sides.

    A nested run that collected two ids, or a module under development, must
    fall back to the child collection: an inventory missing a module cannot
    answer "exactly these tests carry the marker".
    """
    every_module = [
        f"{module.relative_to(KEW_DIR).as_posix()}::test_one"
        for module in repo_inventory.test_modules()
    ]
    assert repo_inventory.covers_every_test_module(every_module)
    assert not repo_inventory.covers_every_test_module(every_module[:-1])
    assert not repo_inventory.covers_every_test_module([])


def test_a_child_collection_agrees_with_the_live_record() -> None:
    """The fallback path end to end: child run, sink, marks, agreement.

    Pointed at one module rather than the tree — what is under test is the
    round trip through ``conftest``'s record and the sink, not pytest's
    ability to collect a second directory.
    """
    module = _a_module_carrying("serial")
    child = repo_inventory.child_inventory((module,))
    assert {node: sorted(marks) for node, marks in child.items()} == {
        node: sorted(marks)
        for node, marks in repo_inventory.collected_marks().items()
        if node.startswith(f"{module}::")
    }
    assert any("serial" in marks for marks in child.values())


def test_a_handed_down_record_is_read_back_only_when_it_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a child does with the record its parent left it.

    A nested pytest reads the parent's record instead of collecting the tree
    again, so the guards on it are the ones that keep these gates honest: it
    must name THIS checkout — a child running in a copy of it has its own
    tree — and it must cover every test module, because a record missing a
    module cannot say which tests carry a marker. Failing either, the child
    collects for itself; it never answers from a partial record.
    """
    inventory = repo_inventory.collected_marks()
    fits = tmp_path / "fits.json"
    repo_inventory._write(fits, inventory)
    monkeypatch.setenv(repo_inventory.INVENTORY_SHARE_ENV, str(fits))
    assert repo_inventory.shared_inventory() == inventory

    payload = json.loads(fits.read_text(encoding="utf-8"))
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(
        json.dumps(payload | {"root": str(tmp_path / "copy-of-the-checkout")}),
        encoding="utf-8",
    )
    monkeypatch.setenv(repo_inventory.INVENTORY_SHARE_ENV, str(elsewhere))
    assert repo_inventory.shared_inventory() is None

    dropped = f"{MARKED_MODULE}::"
    partial = tmp_path / "partial.json"
    partial.write_text(
        json.dumps(
            payload
            | {
                "items": {
                    node: marks
                    for node, marks in payload["items"].items()
                    if not node.startswith(dropped)
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(repo_inventory.INVENTORY_SHARE_ENV, str(partial))
    assert repo_inventory.shared_inventory() is None

    monkeypatch.setenv(repo_inventory.INVENTORY_SHARE_ENV, str(tmp_path / "gone.json"))
    assert repo_inventory.shared_inventory() is None
    monkeypatch.delenv(repo_inventory.INVENTORY_SHARE_ENV)
    assert repo_inventory.shared_inventory() is None


def test_the_sink_is_not_written_unless_a_child_run_asks_for_it() -> None:
    """An inventory run is the only run that leaves a file behind."""
    assert repo_inventory.INVENTORY_SINK_ENV not in os.environ
    assert not list(TESTS_DIR.glob("inventory.json"))
    assert not list(KEW_DIR.glob("inventory.json"))
