"""Test-quality ratchet (#2493) — weak-test shapes may only shrink.

``scripts/test_quality_scan.py`` counts, per test file, interaction-only tests,
patches of code the file is testing, source-text tests, skip/xfail markers and
imports from other ``test_*.py`` modules. This module compares those counts
with ``ratchets/test_quality.json`` in both directions: a rise fails naming the
file and the count, a fall fails asking for the baseline to be lowered, and a
new file named for a review round or a PR number is refused.

The ledger lives outside ``tests/`` on purpose. ``kew/ratchets/`` is a
merge-gate path and an always-guarded loop path, so the change that adds a
weak test cannot also widen its own allowance unseen — which a ledger under
``tests/`` let it do.

The behaviour tests below drive the real ratchet against the real ledger: a
probe edits the scan of one real file, so a ledger or scanner that stopped
noticing would turn them red, not just the synthetic cases.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.policy

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test_quality_scan.py"


def _load_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_quality_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


scan = _load_scan()

_INTERACTION_ONLY_PROBE = """

def test_added_by_the_ratchet_probe():
    probe = Mock()
    probe()
    probe.assert_called_once_with()
"""


@pytest.fixture(scope="module")
def counts() -> dict:
    return scan.scan_tree()


@pytest.fixture(scope="module")
def baseline():
    return scan.load_baseline()


def _source_of(key: str) -> str:
    return (scan.PACKAGE_ROOT / key).read_text()


def _with(counts: dict, key: str, text: str) -> dict:
    return {**counts, key: scan.scan_source(text, key)}


# --- the ratchet over the real tree ------------------------------------------


def test_the_tree_matches_the_ledger(counts: dict, baseline) -> None:
    problems = scan.ratchet_problems(counts, baseline)
    assert problems == [], "\n\n".join(problems)


def test_the_ledger_is_written_canonically(baseline) -> None:
    """Sorted, no zero counts — so a lowering diff is one line per change."""
    on_disk = scan.BASELINE_PATH.read_text()
    assert on_disk == scan.render_baseline(baseline)


def test_pytest_collects_only_what_the_scan_reads(
    pytestconfig: pytest.Config,
) -> None:
    """A *_test.py file would run without ever being counted."""
    assert pytestconfig.getini("python_files") == ["test_*.py"]


def test_the_ledger_lives_outside_tests() -> None:
    assert scan.BASELINE_PATH.parent.name == "ratchets"
    assert scan.TESTS_ROOT not in scan.BASELINE_PATH.parents


# --- AC1: one more interaction-only test fails, naming file and count --------


@pytest.mark.parametrize(
    "key",
    [
        "tests/test_liveness_owner.py",
        "tests/test_dispatch_core.py",  # already carries interaction-only debt
        "tests/delegate/test_claim.py",
        "tests/test_merge_gate_paths.py",
    ],
)
def test_one_more_interaction_only_test_fails_naming_file_and_count(
    counts: dict, baseline, key: str
) -> None:
    before = counts[key].interaction_only
    probed = _with(counts, key, _source_of(key) + _INTERACTION_ONLY_PROBE)

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert f"{key}: interaction_only {before + 1} (baseline {before})" in problems[0]
    assert "rose above the baseline" in problems[0]


# --- AC2: round-named files and test-to-test imports -------------------------


@pytest.mark.parametrize(
    "key",
    [
        "tests/test_round99_x.py",
        "tests/test_w42_round27.py",
        "tests/test_proxy_round30.py",
        "tests/delegate/test_round3_claims.py",
        "tests/test_issue_2493.py",
        "tests/test_pr2493_fix.py",
        "tests/test_2493_ledger.py",
    ],
)
def test_a_new_round_or_pr_named_file_is_refused(
    counts: dict, baseline, key: str
) -> None:
    probed = _with(counts, key, "def test_sums():\n    assert 1 + 1 == 2\n")

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert "review round or a PR/issue number" in problems[0]
    assert key in problems[0]


@pytest.mark.parametrize(
    "key",
    [
        "tests/acceptance/helper.py",
        "tests/acceptance/conftest.py",
        "tests/acceptance/test_contract_helpers.py",
        "tests/acceptance/nested/test_issue_42.py",
    ],
)
def test_a_stray_contract_directory_file_is_told_only_contracts_belong(
    counts: dict, baseline, key: str
) -> None:
    probed = _with(counts, key, "def test_sums():\n    assert 1 + 1 == 2\n")

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert "only acceptance contracts (test_issue_N.py)" in problems[0]
    assert "review round or a PR/issue number" not in problems[0]
    assert key in problems[0]


@pytest.mark.parametrize(
    "key",
    [
        "tests/test_round_trip.py",
        "tests/test_sha256.py",
        "tests/test_c3_poll_bounds.py",
        "tests/test_a2a.py",
        "tests/test_grounded.py",
    ],
)
def test_a_behaviour_name_with_digits_is_not_refused(key: str) -> None:
    assert not scan.refused_name(key)


def test_an_acceptance_contract_keeps_its_issue_number(counts: dict, baseline) -> None:
    """A generated contract is named for its item (#2488, spec decision 4)."""
    key = "tests/acceptance/test_issue_4242.py"
    probed = _with(counts, key, "def test_ac1():\n    assert 1 + 1 == 2\n")

    assert not scan.refused_name(key)
    assert scan.ratchet_problems(probed, baseline) == []


@pytest.mark.parametrize(
    "key",
    [
        "tests/acceptance/test_round3_claims.py",
        "tests/acceptance/test_issue_4242_fix.py",
        "tests/acceptance/test_issue_4242.backup.py",
        "tests/acceptance/test_issue_004242.py",
        "tests/acceptance/test_issue_01.py",
        "tests/acceptance/test_issue_\u0664\u0662.py",
        "tests/acceptance/test_issue_4242fix.py",
        "tests/acceptance/test_contract_helpers.py",
        "tests/acceptance/test_issue_\u0664\u0662\u0664\u0662.py",
        "tests/acceptance/test_Issue_4242.py",
        "tests/acceptance/nested/test_issue_4242.py",
        "tests/acceptance/nested/test_issue_42.py",
        "tests/acceptance/test_issue_4242.txt",
        "tests/acceptance/helper.py",
        "tests/acceptance/conftest.py",
        "tests/test_issue_4242.py",
        "tests/test_Round99_x.py",
        "tests/test_PR2493_fix.py",
    ],
)
def test_only_the_exact_contract_shape_is_exempt(key: str) -> None:
    assert scan.refused_name(key)


_ONE_OF_EACH = {
    "interaction_only": "def test_ac1(m):\n    m.assert_called_once()\n",
    "self_patch": (
        "from kew import ids\n\n\ndef test_ac1(monkeypatch):\n"
        "    monkeypatch.setattr(ids, 'item_ref', str)\n    assert ids.item_ref('1')\n"
    ),
    "source_text": (
        "import inspect\n\n\ndef test_ac1():\n    assert inspect.getsource(len)\n"
    ),
    "skip_xfail": (
        "import pytest\n\n\n@pytest.mark.skip\ndef test_ac1():\n    assert True\n"
    ),
    "test_imports": (
        "from test_liveness_owner import _owner\n\n\ndef test_ac1():\n"
        "    assert _owner\n"
    ),
}


def test_the_probes_cover_every_metric() -> None:
    assert sorted(_ONE_OF_EACH) == sorted(scan.METRICS)


def test_every_file_in_the_contract_directory_is_scanned(tmp_path: Path) -> None:
    """Not only test_*.py: a helper a contract imports must not hide there."""
    tests = tmp_path / "tests"
    contracts = tests / "acceptance"
    (contracts / "__pycache__").mkdir(parents=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert 1\n")
    (contracts / "test_issue_7.py").write_text("def test_ac1():\n    assert 0\n")
    (contracts / "helper.py").write_text("X = 1\n")
    (contracts / "test_issue_7.txt").write_text("notes\n")
    (contracts / "__pycache__" / "test_issue_7.pyc").write_bytes(b"")

    found = scan.scan_tree(tests, tmp_path, tmp_path / "src")
    refused = sorted(key for key in found if scan.refused_name(key))

    assert refused == [
        "tests/acceptance/helper.py",
        "tests/acceptance/test_issue_7.txt",
    ]
    assert "tests/acceptance/test_issue_7.py" in found
    assert not any("__pycache__" in key for key in found)


@pytest.mark.parametrize("metric", sorted(_ONE_OF_EACH))
def test_a_contract_still_counts_every_weak_shape(
    counts: dict, baseline, metric: str
) -> None:
    key = "tests/acceptance/test_issue_4242.py"
    probed = _with(counts, key, _ONE_OF_EACH[metric])

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert f"{key}: {metric} 1 (baseline 0)" in problems[0]


@pytest.mark.parametrize(
    "line",
    [
        "from test_liveness_owner import _owner",
        "import test_liveness_owner",
        "from tests.test_liveness_owner import _owner",
        "from .test_liveness_owner import _owner",
        "from . import test_liveness_owner",
    ],
)
def test_a_new_import_from_a_test_module_fails(
    counts: dict, baseline, line: str
) -> None:
    key = "tests/test_merge_gate_paths.py"
    before = counts[key].test_imports
    probed = _with(counts, key, _source_of(key) + f"\n{line}\n")

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert f"{key}: test_imports {before + 1} (baseline {before})" in problems[0]


# --- AC3: a fall must be banked ---------------------------------------------


def test_a_lowered_count_fails_until_the_baseline_is_lowered(
    counts: dict, baseline
) -> None:
    key, metric = next(
        (key, metric)
        for key, recorded in sorted(baseline.files.items())
        for metric in scan.METRICS
        if recorded.get(metric)
    )
    was = baseline.files[key][metric]
    fewer = scan.FileCounts(**{**vars(counts[key]), metric: was - 1})

    problems = scan.ratchet_problems({**counts, key: fewer}, baseline)

    assert len(problems) == 1
    assert "lower the baseline" in problems[0]
    assert f"{key}: {metric} {was - 1} (baseline {was})" in problems[0]

    banked = scan.lowered({**counts, key: fewer}, baseline)
    assert scan.ratchet_problems({**counts, key: fewer}, banked) == []


def test_a_renamed_legacy_file_must_leave_the_ledger(counts: dict, baseline) -> None:
    gone = baseline.legacy_names[0]
    remaining = {k: v for k, v in counts.items() if k != gone}

    problems = scan.ratchet_problems(remaining, baseline)

    assert any("lower the baseline" in p and gone in p for p in problems)
    assert gone not in scan.lowered(remaining, baseline).legacy_names


def test_lowering_never_admits_a_rise_or_a_new_name(counts: dict, baseline) -> None:
    key = "tests/test_liveness_owner.py"
    probed = _with(counts, key, _source_of(key) + _INTERACTION_ONLY_PROBE)
    probed = _with(probed, "tests/test_round99_x.py", "def test_x():\n    pass\n")

    banked = scan.lowered(probed, baseline)

    assert banked.files.get(key, {}).get("interaction_only", 0) == (
        baseline.files.get(key, {}).get("interaction_only", 0)
    )
    assert "tests/test_round99_x.py" not in banked.legacy_names
    assert len(scan.ratchet_problems(probed, banked)) == 2


# --- the first baseline ------------------------------------------------------


def test_the_first_ledger_never_grandfathers_a_stray_contract_file(
    counts: dict, tmp_path: Path
) -> None:
    probed = {**counts, "tests/acceptance/helper.py": scan.FileCounts()}

    with pytest.raises(scan.ScanCannotRunError, match="tests/acceptance/helper.py"):
        scan.write_initial(probed, tmp_path / "test_quality.json")

    assert not (tmp_path / "test_quality.json").exists()


def test_the_initial_baseline_records_the_tree_as_it_stands(
    counts: dict, tmp_path: Path
) -> None:
    ledger = tmp_path / "test_quality.json"

    scan.write_initial(counts, ledger)

    first = scan.load_baseline(ledger)
    assert scan.ratchet_problems(counts, first) == []
    assert first.legacy_names == sorted(k for k in counts if scan.refused_name(k))
    assert ledger.read_text() == scan.render_baseline(first)


def test_the_initial_baseline_never_overwrites_a_ledger(
    counts: dict, tmp_path: Path
) -> None:
    ledger = tmp_path / "test_quality.json"
    ledger.write_text("{}\n")

    with pytest.raises(scan.ScanCannotRunError, match="already exists"):
        scan.write_initial(counts, ledger)

    assert ledger.read_text() == "{}\n"


# --- what each count means ---------------------------------------------------


def test_interaction_only_needs_every_check_to_be_on_a_mock() -> None:
    text = """
def test_calls_only(m):
    m.run()
    m.run.assert_called_once()
    assert m.run.call_count == 1

def test_calls_and_effect(m):
    m.run.assert_called_once()
    assert m.result == 3

def test_raises(m):
    with pytest.raises(ValueError):
        m.run()
    m.run.assert_called_once()

def test_no_mock(m):
    assert m == 1
"""
    assert scan.scan_source(text, "tests/test_x.py").interaction_only == 1


def test_self_patch_counts_only_defs_of_an_imported_kew_module(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "kew").mkdir(parents=True)
    (src / "kew" / "__init__.py").write_text("")
    (src / "kew" / "mod.py").write_text("import os\n\ndef run():\n    pass\n")
    (src / "kew" / "other.py").write_text("def run():\n    pass\n")
    text = """
from kew import mod
from kew.mod import run

@patch("kew.mod.run")
def test_string_target(m):
    assert run() is None

def test_object_target(monkeypatch):
    monkeypatch.setattr(mod, "run", lambda: 1)
    assert mod.run() == 1

def test_patches_a_dependency(monkeypatch):
    monkeypatch.setattr("kew.mod.os", None)
    assert True

def test_patches_an_unimported_module():
    with patch("kew.other.run"):
        assert True
"""
    assert scan.scan_source(text, "tests/test_x.py", src).self_patch == 2


def test_source_text_counts_reads_of_python_source() -> None:
    text = """
SOURCE = Path(__file__).parent.parent / "src" / "kew" / "cli.py"

def test_getsource():
    assert "x" in inspect.getsource(f)

def test_module_level_path():
    assert "x" in SOURCE.read_text()

def test_module_file(mod):
    assert "x" in Path(mod.__file__).read_text()

def test_fixture_data(tmp_path):
    assert (tmp_path / "data.json").read_text() == "{}"
"""
    assert scan.scan_source(text, "tests/test_x.py").source_text == 3


def test_skip_and_xfail_markers_are_counted() -> None:
    text = """
@pytest.mark.skip(reason="x")
def test_a():
    pass

@pytest.mark.skipif(True, reason="x")
@pytest.mark.xfail
def test_b():
    pytest.skip("x")

def test_c():
    pytest.importorskip("yaml")
    assert True
"""
    assert scan.scan_source(text, "tests/test_x.py").skip_xfail == 5


def test_skip_markers_are_counted_through_import_aliases() -> None:
    text = """
import pytest as pt
from pytest import mark as pm
from pytest import skip as bail, xfail

@pm.skip
def test_a():
    pass

@pt.mark.xfail
def test_b():
    bail("x")

def test_c():
    xfail("x")
"""
    assert scan.scan_source(text, "tests/test_x.py").skip_xfail == 4


def test_an_aliased_getsource_call_counts_as_source_text() -> None:
    text = """
from inspect import getsource as gs

def test_aliased():
    assert "x" in gs(f)

def test_unrelated_call():
    assert gs is not None
"""
    assert scan.scan_source(text, "tests/test_x.py").source_text == 1


def test_self_patch_resolves_a_dotted_kew_module_target(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "kew" / "graph").mkdir(parents=True)
    (src / "kew" / "__init__.py").write_text("")
    (src / "kew" / "ids.py").write_text("import os\n\ndef item_ref():\n    pass\n")
    (src / "kew" / "graph" / "__init__.py").write_text("")
    (src / "kew" / "graph" / "tick.py").write_text("def run():\n    pass\n")
    text = """
import kew.ids
from kew import graph
import json

def test_dotted_setattr(monkeypatch):
    monkeypatch.setattr(kew.ids, "item_ref", str)
    assert True

def test_dotted_patch_object():
    with patch.object(graph.tick, "run"):
        assert True

def test_dotted_dependency(monkeypatch):
    monkeypatch.setattr(kew.ids, "os", None)
    assert True

def test_not_kew(monkeypatch):
    monkeypatch.setattr(json.decoder, "item_ref", None)
    assert True
"""
    assert scan.scan_source(text, "tests/test_x.py", src).self_patch == 2


def test_self_patch_counts_the_real_dotted_ids_target() -> None:
    text = """
import kew.ids

def test_ac4(monkeypatch):
    monkeypatch.setattr(kew.ids, "item_ref", str)
    assert True
"""
    assert scan.scan_source(text, "tests/test_x.py").self_patch == 1


# --- the scan fails loud -----------------------------------------------------


def test_an_unparseable_file_is_an_error_not_a_clean_count() -> None:
    with pytest.raises(scan.ScanCannotRunError, match="cannot parse"):
        scan.scan_source("def test_(:\n", "tests/test_broken.py")


def test_a_missing_tree_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(scan.ScanCannotRunError, match="not found"):
        scan.scan_tree(tmp_path / "absent", relative_to=tmp_path)


def _tree_with_one_test(tmp_path: Path) -> Path:
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_plain.py").write_text("def test_plain():\n    assert 1\n")
    return tests_root


def test_a_symlinked_directory_under_the_tree_is_an_error(tmp_path: Path) -> None:
    tests_root = _tree_with_one_test(tmp_path)
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "test_hidden.py").write_text("def test_hidden():\n    pass\n")
    (tests_root / "acceptance").symlink_to(contracts, target_is_directory=True)
    with pytest.raises(scan.ScanCannotRunError, match="symlink.*tests/acceptance"):
        scan.scan_tree(tests_root, relative_to=tmp_path)


def test_a_symlinked_test_file_under_the_tree_is_an_error(tmp_path: Path) -> None:
    tests_root = _tree_with_one_test(tmp_path)
    (tests_root / "sub").mkdir()
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("def test_hidden():\n    pass\n")
    (tests_root / "sub" / "test_linked.py").symlink_to(elsewhere)
    with pytest.raises(
        scan.ScanCannotRunError, match="symlink.*tests/sub/test_linked.py"
    ):
        scan.scan_tree(tests_root, relative_to=tmp_path)
