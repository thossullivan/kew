"""Comment ratchet: issue references and history in comments may only fall.

``scripts/comment_scan.py`` counts them per file; ``ratchets/comment_history.json``
records the counts. The probes edit the scan of real files, so a scanner or a
ledger that stopped noticing turns these red.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.policy

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "comment_scan.py"


def _load_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comment_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


scan = _load_scan()

_ISSUE_COMMENT_PROBE = "\n# Added by the ratchet probe, see #9999.\n"
_ISSUE_DOCSTRING_PROBE = '''

def _added_by_the_ratchet_probe():
    """Fixed in PR 9999."""
'''
_HISTORY_COMMENT_PROBE = "\n# Review round 3 found this edge.\n"
_CLEAN_KEY = "src/kew/review_contract.py"


@pytest.fixture(scope="module")
def counts() -> dict:
    return scan.scan_tree()


@pytest.fixture(scope="module")
def baseline() -> dict:
    return scan.load_baseline()


def _with(counts: dict, key: str, text: str) -> dict:
    source = (scan.PACKAGE_ROOT / key).read_text()
    return {**counts, key: scan.scan_source(source + text, key)}


def test_the_tree_matches_the_ledger(counts: dict, baseline: dict) -> None:
    problems = scan.ratchet_problems(counts, baseline)
    assert problems == [], "\n\n".join(problems)


def test_the_ledger_is_written_canonically(baseline: dict) -> None:
    assert scan.BASELINE_PATH.read_text() == scan.render_baseline(baseline)


def test_the_ledger_lives_outside_tests() -> None:
    assert scan.BASELINE_PATH.parent.name == "ratchets"
    assert scan.PACKAGE_ROOT / "tests" not in scan.BASELINE_PATH.parents


@pytest.mark.parametrize(
    ("probe", "metric"),
    [
        (_ISSUE_COMMENT_PROBE, "issue_refs"),
        (_ISSUE_DOCSTRING_PROBE, "issue_refs"),
        (_HISTORY_COMMENT_PROBE, "history"),
    ],
)
def test_one_more_history_line_fails_naming_file_and_count(
    counts: dict, baseline: dict, probe: str, metric: str
) -> None:
    probed = _with(counts, _CLEAN_KEY, probe)

    problems = scan.ratchet_problems(probed, baseline)

    assert len(problems) == 1
    assert f"{_CLEAN_KEY}: {metric} 1 (baseline 0)" in problems[0]
    assert "rose above the baseline" in problems[0]


def test_a_fall_asks_for_the_baseline_to_be_lowered(
    counts: dict, baseline: dict
) -> None:
    key = max(baseline, key=lambda k: baseline[k].get("issue_refs", 0))
    have = counts[key]
    fell = {**counts, key: replace(have, issue_refs=have.issue_refs - 1)}

    problems = scan.ratchet_problems(fell, baseline)

    assert len(problems) == 1
    assert "fell below the baseline" in problems[0]
    assert f"{key}: issue_refs {have.issue_refs - 1}" in problems[0]


def test_lowering_banks_a_fall_and_never_admits_a_rise() -> None:
    recorded = {"issue_refs": 4, "history": 2}
    have = {"src/kew/x.py": scan.FileCounts(issue_refs=3, history=7)}

    lowered = scan.lowered(have, {"src/kew/x.py": recorded})

    assert lowered == {"src/kew/x.py": {"issue_refs": 3, "history": 2}}


@pytest.mark.parametrize(
    "source",
    [
        'x = "#1234 in a code string is not prose"\n',
        "# Emoji render as &#128512; in the PR body.\n",
        "# The colour #fff123 is not an issue.\n",
        "# A round-trip through JSON keeps the order.\n",
        "def f():\n    return '''#4567 in a returned string'''\n",
        "# Round 1 at the original head: NEEDS_CHANGES.\n",
        "# Append one review round and return its 1-based number.\n",
        "# Returns the dispatch integrity incident reason.\n",
        "# A workaround for CPython's tarfile filter is still needed.\n",
        "# Remediation fixes what the reviewer flagged.\n",
        "# While kew is fixing what a reviewer flagged, the node waits.\n",
        "# Remediation is dispatched to fix what round 1 found.\n",
        "# Used to fail closed on a route no operator chose.\n",
        "# The driver fetches refs/pull/7/head.\n",
        "# A link such as /issues/1 is parsed from the body.\n",
    ],
)
def test_what_is_not_history_is_not_counted(source: str) -> None:
    counted = scan.scan_source(source, "probe.py")
    assert (counted.issue_refs, counted.history) == (0, 0)


@pytest.mark.parametrize(
    "source",
    [
        "# See issue 1234.\n",
        "# Fixed in PR 812.\n",
        "# Fixed in PR#812.\n",
        "# Upstream GH-4521 tracks it.\n",
        "# https://github.com/o/r/pull/3087\n",
        "x = 1  # (#2291)\n",
        "#: A constant kept for #2291.\nX = 1\n",
        "def f():\n    '''Kept for #1234.'''\n",
        "class C:\n    '''Kept for #1234.'''\n",
        "class C:\n    x: int = 0\n    '''A field kept for #3087.'''\n",
    ],
)
def test_an_issue_reference_in_prose_is_counted(source: str) -> None:
    assert scan.scan_source(source, "probe.py").issue_refs == 1


@pytest.mark.parametrize(
    "source",
    [
        "# Review round 3 found this edge.\n",
        "# The R4 review flagged it.\n",
        "# The reviewer caught a symlink here.\n",
        "def f():\n    '''This used to crash on an empty list.'''\n",
        "# Round 2 found a second symlink.\n",
    ],
)
def test_history_phrasing_is_counted(source: str) -> None:
    assert scan.scan_source(source, "probe.py").history == 1


def _tiny_tree(root: Path, comment: str) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "a.py").write_text(f"{comment}\nX = 1\n")
    (root / "ratchets").mkdir()


def _point_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    ledger = root / "ratchets" / "comment_history.json"
    monkeypatch.setattr(scan, "PACKAGE_ROOT", root)
    monkeypatch.setattr(scan, "BASELINE_PATH", ledger)
    return ledger


def test_initial_refuses_to_overwrite_a_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _tiny_tree(tmp_path, "# See #1234.")
    ledger = _point_at(monkeypatch, tmp_path)
    ledger.write_text('{"files": {}}\n')

    assert scan.main(["--initial"]) == 2
    assert ledger.read_text() == '{"files": {}}\n'
    assert "use --lower" in capsys.readouterr().err


def test_check_exits_one_on_a_rise_and_lower_banks_only_falls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tiny_tree(tmp_path, "# See #1234.")
    ledger = _point_at(monkeypatch, tmp_path)

    assert scan.main(["--initial"]) == 0
    assert scan.main(["--check"]) == 0

    (tmp_path / "src" / "a.py").write_text("# See #1234 and #5678.\n# #9012\nX = 1\n")
    assert scan.main(["--check"]) == 1
    assert scan.main(["--lower"]) == 0
    assert scan.load_baseline(ledger) == {"src/a.py": {"issue_refs": 1}}

    (tmp_path / "src" / "a.py").write_text("X = 1\n")
    assert scan.main(["--check"]) == 1
    assert scan.main(["--lower"]) == 0
    assert scan.main(["--check"]) == 0
    assert scan.load_baseline(ledger) == {}
