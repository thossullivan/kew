"""`--shard=K/N` splits the xdist phase across verify VMs without losing a test.

The gate this file is: **N shards run exactly the suite, once**. Sharding is the
one test-speed change that can make the suite faster by running less of it, and
a shard that silently drops a test reports green having never executed it. So
the partition properties are asserted on the real collection — every id this
session collected, not a handful of invented ones — for every N the verify host
might use.

The rest is balance (a split that leaves one VM holding half the suite buys
nothing) and the plumbing: the option's validation, its placement after pytest's
own `-m` deselection, and the script that records the durations.

See `tests/support/shard.py` and `scripts/refresh_test_durations.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Generator, Iterable
from pathlib import Path

import conftest
import pytest
from support import repo_inventory, shard

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_test_durations.py"
_spec = importlib.util.spec_from_file_location("refresh_test_durations", SCRIPT)
assert _spec is not None and _spec.loader is not None
refresh = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = refresh
_spec.loader.exec_module(refresh)

#: The Ns the verify host can plausibly use. N=1 is in the list because it is
#: the degenerate case an operator reaches for first ("does --shard work?") and
#: the one where an off-by-one in the index would be invisible.
SHARD_COUNTS = (1, 2, 3, 4)

#: AC2's bound: with three VMs the slowest shard must be within 15% of a third
#: of the recorded total, or the third VM is paying for a split it does not get.
#: Applied to the test COUNT as well, because recorded seconds alone are not the
#: wall clock — see test_three_shards_also_divide_the_number_of_tests.
BALANCE_TOLERANCE = 1.15


@pytest.fixture
def forget_recorded_durations() -> Generator[None]:
    """Drop the cached recording around a test that points the path elsewhere."""
    shard.recorded_durations.cache_clear()
    yield
    shard.recorded_durations.cache_clear()


# --- The partition, on the real suite ----------------------------------------


@pytest.mark.policy
@pytest.mark.parametrize("total", SHARD_COUNTS)
def test_the_shards_of_the_real_suite_are_a_partition_of_it(total: int) -> None:
    """Union == the unsharded selection, and the shards are disjoint (AC1).

    Read off this session's own collection (``support/repo_inventory``), so it
    is the suite's actual ids — parametrised ids, container tests and all — and
    not a sample that happens to divide evenly.
    """
    collected = set(repo_inventory.collected_marks())
    assert collected, "collection produced no items"
    groups = shard.shard_groups(collected, total)
    assert len(groups) == total
    assert set().union(*groups) == collected
    # Disjoint: N sets whose union is the whole and whose sizes sum to the
    # whole's cannot overlap.
    assert sum(len(group) for group in groups) == len(collected)


@pytest.mark.policy
def test_three_shards_divide_the_recorded_cost_evenly() -> None:
    """The slowest of three shards is within 15% of a third (AC2).

    Skipped only when nothing is recorded at all, which
    ``test_the_recording_covers_the_suite_it_balances`` fails on: a missing
    recording is reported once, as itself, rather than as three vague failures.
    """
    recorded = shard.recorded_durations()
    if not recorded:
        pytest.skip(f"no durations recorded at {shard.DURATIONS_PATH}")
    collected = set(repo_inventory.collected_marks())
    groups = shard.shard_groups(collected, 3)
    totals = [sum(recorded.get(node, 0.0) for node in group) for group in groups]
    fair_share = sum(recorded.get(node, 0.0) for node in collected) / 3
    assert fair_share > 0
    assert max(totals) <= fair_share * BALANCE_TOLERANCE, (
        f"three shards came out {[round(total) for total in totals]}s against a "
        f"fair share of {fair_share:.0f}s. Refresh "
        f"{shard.DURATIONS_PATH.name} (scripts/refresh_test_durations.py) — or a "
        "single test now costs more than a third of the suite, which no split "
        f"can fix: slowest is {max(recorded.items(), key=lambda kv: kv[1])}."
    )


@pytest.mark.policy
def test_three_shards_also_divide_the_number_of_tests() -> None:
    """Recorded seconds alone are not the wall clock.

    pytest prints two decimals, so 10,175 of this suite's tests record as 0.00s,
    and a shard total is blind to them. The first version of the greedy charged
    them nothing: three shards came out an even 226s each holding 6,297, 6,297
    and 13,080 tests, AC2 passed, and the third VM would still have been the
    whole wall clock. ``shard.NOMINAL_SECONDS`` is the fix; this is the
    assertion that notices if it goes away.
    """
    collected = set(repo_inventory.collected_marks())
    sizes = [len(group) for group in shard.shard_groups(collected, 3)]
    assert max(sizes) <= (len(collected) / 3) * BALANCE_TOLERANCE, (
        f"three shards came out {sizes} tests against a fair share of "
        f"{len(collected) // 3}: the split is balancing recorded seconds while "
        "one shard collects every test too fast to have been measured."
    )


@pytest.mark.policy
def test_the_recording_covers_the_suite_it_balances() -> None:
    """A recording of another tree, or of a tenth of this one, balances nothing.

    The floor is deliberately generous — the recording is refreshed by hand and
    drifts as tests land — but it has to be a floor: at 10% coverage the greedy
    assignment is a round-robin wearing a hat, and AC2 would still pass because
    the unrecorded 90% weighs nothing in either the shard total or the fair
    share it is compared against.
    """
    recorded = shard.recorded_durations()
    assert recorded, (
        f"{shard.DURATIONS_PATH} holds no durations. Record them with "
        "scripts/refresh_test_durations.py (its docstring has the two commands)."
    )
    collected = set(repo_inventory.collected_marks())
    covered = len(collected & set(recorded)) / len(collected)
    assert covered >= 0.5, (
        f"only {covered:.0%} of the {len(collected)} collected tests have a "
        f"recorded duration. Refresh {shard.DURATIONS_PATH.name} with "
        "scripts/refresh_test_durations.py."
    )


#: What a secret scanner reads as a provider's key: a known prefix, then a long
#: token run. The prefix must start a word, so `task-...` is not `sk-...`.
_KEY_SHAPED = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|gh[oprsu]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|[sr]k_live_[A-Za-z0-9]{16,}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|pypi-AgEIcHlwaS5vcmc"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
    r")"
)


@pytest.mark.policy
def test_no_test_id_carries_a_key_shaped_value() -> None:
    """A node id is a published literal: the recording stores every id whole.

    A test that takes a key-shaped sample builds it from parts, so no scanner
    reads a key in its source. Without ``ids=`` pytest puts the built value in
    the node id, the recording stores it as one literal, and a host that scans
    pushes for secrets refuses the push. The message names the test and never
    the value.
    """
    carried = set(shard.recorded_durations()) | set(repo_inventory.collected_marks())
    offenders = _tests_with_a_key_shaped_id(carried)
    assert not offenders, (
        "these tests put a key-shaped value in a node id; give the parametrized "
        f"cases explicit ids= and rename their keys in {shard.DURATIONS_PATH.name}: "
        f"{offenders}"
    )


def _tests_with_a_key_shaped_id(nodes: Iterable[str]) -> list[str]:
    """The tests, without their case ids, whose node id holds a key shape."""
    return sorted({node.split("[", 1)[0] for node in nodes if _KEY_SHAPED.search(node)})


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("sk-" + "or-v1-" + "f" * 64, True),
        ("sk-" + "T3BlbkFJ" * 6, True),
        ("token xox" + "b-1234567890-0987654321-AbCd", True),
        ("gh" + "p_" + "A1b2" * 9, True),
        ("github" + "_pat_" + "A1b2_" * 5, True),
        ("AK" + "IA" + "ABCDEFGH12345678", True),
        ("long-sk-" + "placeholder0123456789", True),
        ("sk" + "_live_" + "A1b2" * 6, True),
        ("np" + "m_" + "A1b2" * 9, True),
        ("pypi" + "-AgEIcHlwaS5vcmc" + "A1b2" * 6, True),
        ("-----BEGIN RSA " + "PRIVATE KEY-----", True),
        ("task-queue-is-empty-on-start", False),
        ("skip-merge-verify-failed-on-main", False),
        ("sk-short", False),
        ("legacy-key", False),
    ],
    ids=[
        "openrouter-shape",
        "openai-shape",
        "slack-shape-after-a-word",
        "github-token-shape",
        "github-fine-grained-shape",
        "aws-key-id-shape",
        "key-shape-after-a-hyphen",
        "stripe-shape",
        "npm-token-shape",
        "pypi-token-shape",
        "private-key-header",
        "a-word-that-ends-in-sk",
        "a-word-that-starts-with-sk",
        "a-short-run",
        "an-explicit-id",
    ],
)
def test_the_key_shape_rule_names_keys_and_passes_words(case: str, named: bool) -> None:
    node = f"tests/test_x.py::test_y[{case}]"

    expected = ["tests/test_x.py::test_y"] if named else []
    assert _tests_with_a_key_shaped_id([node, "tests/test_x.py::test_z"]) == expected


# --- The partition, as an algorithm ------------------------------------------


def test_the_partition_ignores_the_order_the_ids_arrive_in() -> None:
    """Each VM computes the whole split and keeps its slice, so it must agree.

    Collection order is xdist's and the filesystem's to decide; two shards that
    disagreed about the partition would both drop and double-run tests.
    """
    durations = {"a": 3.0, "b": 1.0, "c": 1.0}
    ids = ["c", "a", "b", "unrecorded-one", "unrecorded-two"]
    assert shard.shard_groups(ids, 2, durations) == shard.shard_groups(
        reversed(ids), 2, durations
    )


def test_a_repeated_id_is_not_placed_twice() -> None:
    groups = shard.shard_groups(["a", "a", "b"], 2, {"a": 1.0, "b": 1.0})
    assert sorted(node for group in groups for node in group) == ["a", "b"]


def test_the_longest_test_is_placed_first_so_the_shards_come_out_even() -> None:
    """Longest-processing-time first: the 10s test cannot join the 10s of small.

    Placing in id order instead would put slow+a (14s) against b+c (6s).
    """
    durations = {"slow": 10.0, "a": 4.0, "b": 3.0, "c": 3.0}
    groups = shard.shard_groups(durations, 2, durations)
    assert sorted(sum(durations[node] for node in group) for group in groups) == [
        10.0,
        10.0,
    ]


def test_a_test_recorded_at_zero_still_weighs_on_the_shard_it_lands_on() -> None:
    """Measured-below-the-resolution is not free (``shard.NOMINAL_SECONDS``).

    Charged nothing, a 0.00s test never moves its shard's total, so the lightest
    shard stays the lightest and takes the next one too — all eight here would
    go to one, under a split that still looked perfectly balanced in seconds.
    """
    durations = {"a": 0.02, "b": 0.02} | {f"zero{n}": 0.0 for n in range(8)}
    groups = shard.shard_groups(durations, 2, durations)
    assert sorted(len(group) for group in groups) == [5, 5]


def test_tests_with_no_recorded_duration_are_spread_round_robin() -> None:
    """Never measured is not the same as measured at zero: nothing to weigh by.

    So these are spread by position rather than charged a nominal cost, which
    keeps a batch of newly added tests from arriving on one VM together.
    """
    groups = shard.shard_groups(["t1", "t2", "t3", "t4", "t5"], 2, {})
    assert groups == (frozenset({"t1", "t3", "t5"}), frozenset({"t2", "t4"}))


def test_one_shard_is_everything() -> None:
    assert shard.shard_groups(["a", "b"], 1, {"a": 1.0}) == (frozenset({"a", "b"}),)


def test_more_shards_than_tests_leaves_empty_shards_not_missing_tests() -> None:
    groups = shard.shard_groups(["a", "b"], 4, {"a": 1.0, "b": 2.0})
    assert len(groups) == 4
    assert set().union(*groups) == {"a", "b"}


def test_zero_shards_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        shard.shard_groups(["a"], 0)


# --- The option --------------------------------------------------------------


@pytest.mark.parametrize("spec", ["2/3", "1/1", "4/4"])
def test_a_well_formed_spec_parses(spec: str) -> None:
    index, total = shard.parse_shard(spec)
    assert f"{index}/{total}" == spec


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "3",
        "/3",
        "3/",
        "0/3",
        "4/3",
        "1/0",
        "a/b",
        "1/3/5",
        "-1/3",
        "1.5/3",
        "1 / 3",
    ],
)
def test_a_malformed_spec_is_a_usage_error(spec: str) -> None:
    """Out of range and unparseable both stop the run before collection.

    ``--shard=0/3`` taken as an index would quietly run two of three groups
    across three VMs and report green; that is the failure mode this refuses.
    """
    with pytest.raises(pytest.UsageError):
        shard.parse_shard(spec)


def test_the_option_is_registered_and_defaults_to_the_whole_suite(
    pytestconfig: pytest.Config,
) -> None:
    assert pytestconfig.getoption("shard") in (None, "1/1")


def test_the_deselection_runs_after_pytests_marker_deselection(
    pytestconfig: pytest.Config,
) -> None:
    """So `-m "not serial" --shard=K/N` balances the phase that actually runs.

    pluggy calls ``pytest_collection_modifyitems`` implementations in reverse
    list order, so "runs last" means "earliest in the list". The shard plugin
    must come before pytest's own ``mark`` plugin there: sharding first would
    spend each shard's budget on the serial phase it is about to drop, and
    ``conftest``'s own implementation — which applies the ``serial`` marker
    ``mark`` then reads — must come after both.
    """
    names = _modifyitems_order(pytestconfig)
    assert "mark" in names, f"pytest's marker deselection is not registered: {names}"
    assert names.index(conftest.SHARD_PLUGIN_NAME) < names.index("mark")


def test_the_repo_inventory_is_recorded_before_the_shard_deselects(
    pytestconfig: pytest.Config,
) -> None:
    """The repo-wide gates still answer for the whole suite inside a shard.

    ``conftest`` records the collection that ``support/repo_inventory`` hands
    the policy gates (#2340), and those gates assert about every test in the
    suite. Deselecting first would have shard 2 of 3 record a third of the
    collection: ``covers_every_test_module`` would reject it and every policy
    gate in that shard would fall back to a whole-tree child collection, which
    is the cost #2340 removed. So the shard runs last of the three.
    """
    names = _modifyitems_order(pytestconfig)
    recorder = pytestconfig.pluginmanager.get_name(conftest)
    assert recorder in names, f"the conftest hook is not registered: {names}"
    assert names.index(conftest.SHARD_PLUGIN_NAME) < names.index(recorder)


def _modifyitems_order(config: pytest.Config) -> list[str]:
    """The hookimpl plugin names, in pluggy's list order (last = called first)."""
    hook = config.pluginmanager.hook.pytest_collection_modifyitems
    return [impl.plugin_name for impl in hook.get_hookimpls()]


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[_Item] = []

    def pytest_deselected(self, items: list[_Item]) -> None:
        self.deselected.extend(items)


class _Config:
    """Just enough of ``pytest.Config`` for the hook: option, hook, stash."""

    def __init__(self, spec: str | None) -> None:
        self._spec = spec
        self.hook = _Hook()
        self.stash = pytest.Stash()

    def getoption(self, name: str, default: object = None) -> object:
        return self._spec if name == "shard" else default


def test_the_hook_keeps_its_group_and_deselects_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shard, "recorded_durations", lambda: {})
    items = [_Item(f"tests/test_x.py::t{n}") for n in range(5)]
    kept = list(items)
    config = _Config("2/2")
    shard.pytest_collection_modifyitems(config, kept)  # type: ignore[arg-type]
    assert [item.nodeid for item in kept] == [
        "tests/test_x.py::t1",
        "tests/test_x.py::t3",
    ]
    assert [item.nodeid for item in config.hook.deselected] == [
        "tests/test_x.py::t0",
        "tests/test_x.py::t2",
        "tests/test_x.py::t4",
    ]


def test_the_hook_reports_the_shard_in_the_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shard, "recorded_durations", lambda: {"tests/t.py::a": 7.0})
    config = _Config("1/2")
    shard.pytest_collection_modifyitems(  # type: ignore[arg-type]
        config, [_Item("tests/t.py::a"), _Item("tests/t.py::b")]
    )
    lines: list[str] = []
    reporter = type(
        "_Reporter",
        (),
        {"config": config, "write_line": lambda _self, line: lines.append(line)},
    )()
    shard.pytest_terminal_summary(reporter)  # type: ignore[arg-type]
    # Both ids land in group 1 — the recorded one by weight, the unrecorded one
    # first in the round-robin — and only the recorded one adds to the seconds.
    assert lines == ["shard 1/2: selected 2 tests, 7s recorded"]


def test_the_summary_is_silent_where_the_collection_did_not_happen() -> None:
    """An xdist controller never collects, so it has no partition to report.

    Its workers do, and the header names the shard on both. A summary that
    read the option instead of the stash would print `0 tests` there — the one
    reading that would make an operator think the shard ran nothing.
    """
    lines: list[str] = []
    config = _Config("1/2")
    reporter = type(
        "_Reporter",
        (),
        {"config": config, "write_line": lambda _self, line: lines.append(line)},
    )()
    shard.pytest_terminal_summary(reporter)  # type: ignore[arg-type]
    assert lines == []


def test_without_the_option_the_hook_and_the_header_do_nothing() -> None:
    config = _Config(None)
    items = [_Item("tests/test_x.py::t0")]
    shard.pytest_collection_modifyitems(config, items)  # type: ignore[arg-type]
    assert [item.nodeid for item in items] == ["tests/test_x.py::t0"]
    assert config.hook.deselected == []
    assert shard.pytest_report_header(config) is None  # type: ignore[arg-type]
    shard.pytest_terminal_summary(  # type: ignore[arg-type]
        type("_Reporter", (), {"config": config})()
    )


def test_the_header_names_the_slice_and_the_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shard, "recorded_durations", lambda: {"tests/t.py::a": 1.0})
    header = shard.pytest_report_header(_Config("2/3"))  # type: ignore[arg-type]
    assert header is not None
    assert "--shard=2/3" in header and "1 recorded durations" in header


def test_the_header_says_so_when_there_is_nothing_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shard, "recorded_durations", lambda: {})
    header = shard.pytest_report_header(_Config("1/2"))  # type: ignore[arg-type]
    assert header is not None and "round-robin" in header


# --- The recording file ------------------------------------------------------


def test_the_docs_gate_exempts_the_recording_under_the_name_shard_py_reads() -> None:
    """The one place outside this suite that has to know the recording's name.

    ``scripts/check-docs.sh`` greps ``kew/tests`` for references into the
    archived internal docs trees. A parametrised node id quotes its parameters,
    so the recording contains the ids of
    ``tests/test_docs_truthfulness.py``'s own refusal cases — which spell those
    prefixes, which is why that file synthesises them from fragments rather than
    writing them out. The gate excludes the recording by basename; renaming the
    recording without telling it turns the docs build red, and the two names
    sitting in different files is exactly how that goes unnoticed.
    """
    gate = (repo_inventory.REPO_ROOT / "scripts" / "check-docs.sh").read_text(
        encoding="utf-8"
    )
    assert f"--exclude={shard.DURATIONS_PATH.name}" in gate, (
        f"scripts/check-docs.sh does not exclude {shard.DURATIONS_PATH.name}: "
        "the recorded node ids will read as references into a deleted docs tree"
    )


@pytest.mark.usefixtures("forget_recorded_durations")
@pytest.mark.parametrize(
    "payload", ["", "not json", "[1, 2]", '{"tests/t.py::a": "slow"}', '{"a": -1}']
)
def test_an_unusable_recording_reads_as_no_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """A corrupt dotfile must cost balance, never a refusal on every VM."""
    path = tmp_path / ".test_durations.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(shard, "DURATIONS_PATH", path)
    assert shard.recorded_durations() == {}


@pytest.mark.usefixtures("forget_recorded_durations")
def test_a_missing_recording_reads_as_no_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shard, "DURATIONS_PATH", tmp_path / "absent.json")
    assert shard.recorded_durations() == {}


@pytest.mark.usefixtures("forget_recorded_durations")
def test_a_recording_is_read_as_node_id_to_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".test_durations.json"
    path.write_text(json.dumps({"tests/t.py::a": 1.5, "tests/t.py::b": 0}), "utf-8")
    monkeypatch.setattr(shard, "DURATIONS_PATH", path)
    assert shard.recorded_durations() == {"tests/t.py::a": 1.5, "tests/t.py::b": 0.0}


# --- The refresh script ------------------------------------------------------

_REPORT = """\
============================= test session starts ==============================
0.99s call     tests/test_decoy.py::test_outside_any_section
_______________________ test_that_drove_a_child_pytest _________________________
------------------------------ Captured stdout ---------------------------------
============================ slowest durations =================================
0.99s call     tests/test_decoy.py::test_inside_a_childs_own_section
------------------------------ Captured stderr ---------------------------------
============================ slowest durations =================================
1.50s call     tests/test_a.py::test_x
0.25s setup    tests/test_a.py::test_x
0.25s teardown tests/test_a.py::test_x
0.10s call     tests/test_a.py::test_y[a b]
(12 durations < 0.005s hidden.  Use -vv to show these durations.)
=========================== short test summary info ============================
0.99s call     tests/test_decoy.py::test_after_the_section
"""


def test_the_parser_sums_the_three_phases_of_each_test() -> None:
    """setup + call + teardown is what a shard pays for a test."""
    assert refresh.parse_durations(_REPORT.splitlines()) == {
        "tests/test_a.py::test_x": 2.0,
        "tests/test_a.py::test_y[a b]": 0.1,
    }


def test_the_parser_reads_only_the_reports_own_last_section() -> None:
    """A child pytest's output carries a whole durations section of its own.

    This suite's tests drive child pytests, and a failing one has the child's
    banner and duration lines in its captured output — reported before the
    parent's own section. Both decoys here are refused: the line outside any
    section, and the section inside the capture block.
    """
    parsed = refresh.parse_durations(_REPORT.splitlines())
    assert not any("decoy" in node for node in parsed)


def test_the_parser_accepts_a_numbered_section_banner() -> None:
    lines = ["===== slowest 10 durations =====", "1.0s call     tests/t.py::a"]
    assert refresh.parse_durations(lines) == {"tests/t.py::a": 1.0}


def test_a_report_captured_outside_the_kew_directory_is_refused() -> None:
    """Ids from a repo-root run (``kew/tests/...``) are never collected here."""
    lines = ["== slowest durations ==", "1.0s call     kew/tests/test_a.py::test_x"]
    with pytest.raises(SystemExit, match="never be collected"):
        refresh.parse_durations(lines)


def test_a_foreign_id_in_a_decoy_section_does_not_refuse_the_report() -> None:
    """The directory check reads the kept section, not a test's captured output.

    A test may print anything, and refusing a whole report over a line a test
    printed is a script nobody can use.
    """
    lines = [
        "== slowest durations ==",
        "9.0s call     /elsewhere/tests/test_a.py::test_x",
        "== short test summary info ==",
        "== slowest durations ==",
        "1.0s call     tests/test_a.py::test_x",
    ]
    assert refresh.parse_durations(lines) == {"tests/test_a.py::test_x": 1.0}


def test_the_script_writes_a_sorted_recording_and_reports_the_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / ".test_durations.json"
    out.write_text(json.dumps({"tests/test_gone.py::test_z": 1.0}), encoding="utf-8")
    monkeypatch.setattr(refresh, "DURATIONS_PATH", out)
    report = tmp_path / "report.txt"
    report.write_text(_REPORT, encoding="utf-8")
    assert refresh.main([str(report)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "tests/test_a.py::test_x": 2.0,
        "tests/test_a.py::test_y[a b]": 0.1,
    }
    assert "+2 new, -1 gone" in capsys.readouterr().out


def test_the_script_merges_the_two_phase_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The xdist phase and the serial phase are two runs and one recording."""
    out = tmp_path / ".test_durations.json"
    monkeypatch.setattr(refresh, "DURATIONS_PATH", out)
    first = tmp_path / "xdist.txt"
    first.write_text("== slowest durations ==\n1.0s call     tests/t.py::a\n", "utf-8")
    second = tmp_path / "serial.txt"
    second.write_text("== slowest durations ==\n2.0s call     tests/t.py::b\n", "utf-8")
    assert refresh.main([str(first), str(second)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "tests/t.py::a": 1.0,
        "tests/t.py::b": 2.0,
    }


def test_a_later_report_supersedes_an_earlier_measurement_of_the_same_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reports merge; they do not add up.

    A run cut short gets topped up with a second pass over the modules it
    missed, and the two passes overlap. Adding them would double every
    overlapping test's recorded cost — a shard would then budget 2s for a 1s
    test, every top-up making the balance worse.
    """
    out = tmp_path / ".test_durations.json"
    monkeypatch.setattr(refresh, "DURATIONS_PATH", out)
    first = tmp_path / "first.txt"
    first.write_text("== slowest durations ==\n1.0s call     tests/t.py::a\n", "utf-8")
    second = tmp_path / "second.txt"
    second.write_text("== slowest durations ==\n3.0s call     tests/t.py::a\n", "utf-8")
    assert refresh.main([str(first), str(second)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"tests/t.py::a": 3.0}


def test_the_script_refuses_to_overwrite_a_recording_from_an_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run without ``--durations=0`` would otherwise blank the recording."""
    out = tmp_path / ".test_durations.json"
    out.write_text(json.dumps({"tests/t.py::a": 1.0}), encoding="utf-8")
    monkeypatch.setattr(refresh, "DURATIONS_PATH", out)
    report = tmp_path / "report.txt"
    report.write_text("1 passed in 0.10s\n", encoding="utf-8")
    assert refresh.main([str(report)]) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {"tests/t.py::a": 1.0}
    assert "--durations=0" in capsys.readouterr().err


def test_the_script_with_no_arguments_prints_its_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert refresh.main([]) == 2
    assert "--durations-min=0" in capsys.readouterr().err
