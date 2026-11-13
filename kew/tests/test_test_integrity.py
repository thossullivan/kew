"""Test-diff integrity report (#2483): each case is a small real unified diff."""

from __future__ import annotations

from textwrap import dedent

import pytest

from kew.test_integrity import (
    FLAG_ASSERTS_REMOVED,
    FLAG_CHANGED_EXPECTED,
    FLAG_MOCK_ANY_ADDED,
    FLAG_RAISES_LOOSENED,
    FLAG_SKIP_ADDED,
    FLAG_TEST_ONLY_FIX_ROUND,
    FLAG_TESTS_REMOVED,
    Evidence,
    build_report,
    edited_tests,
    is_declared,
    is_test_path,
    parse_diff,
)


def _diff(text: str) -> str:
    return dedent(text).lstrip("\n")


ASSERTS_REMOVED = _diff(
    """
    diff --git a/kew/tests/test_calc.py b/kew/tests/test_calc.py
    index 1111111..2222222 100644
    --- a/kew/tests/test_calc.py
    +++ b/kew/tests/test_calc.py
    @@ -1,7 +1,6 @@
     def test_total():
         result = total([10, 20])
    -    assert result.value == 30
    -    assert result.currency == "EUR"
    +    assert result is not None
         assert result.ok


    """
)


def test_two_asserts_removed_one_added_flags_with_both_removed_lines() -> None:
    report = build_report(ASSERTS_REMOVED)

    assert FLAG_ASSERTS_REMOVED in report.flags
    assert report.evidence_for(FLAG_ASSERTS_REMOVED) == (
        Evidence("kew/tests/test_calc.py", 3, "assert result.value == 30"),
        Evidence("kew/tests/test_calc.py", 4, 'assert result.currency == "EUR"'),
    )
    assert len(report.asserts_added) == 1


@pytest.mark.parametrize(
    ("added", "line"),
    [
        ("@pytest.mark.xfail(reason='flaky')", 3),
        ("    pytest.skip('later')", 3),
        ("@pytest.mark.skipif(sys.platform == 'darwin', reason='x')", 3),
    ],
)
def test_added_skip_or_xfail_flags_file_and_line(added: str, line: int) -> None:
    diff = (
        "diff --git a/kew/tests/test_io.py b/kew/tests/test_io.py\n"
        "--- a/kew/tests/test_io.py\n"
        "+++ b/kew/tests/test_io.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import pytest\n"
        " \n"
        f"+{added}\n"
        " def test_read():\n"
    )
    report = build_report(diff)

    assert report.flags == (FLAG_SKIP_ADDED,)
    (evidence,) = report.evidence_for(FLAG_SKIP_ADDED)
    assert (evidence.path, evidence.line) == ("kew/tests/test_io.py", line)
    assert evidence.text == added.strip()


def test_skip_in_a_non_test_file_is_not_a_test_signal() -> None:
    diff = _diff(
        """
        --- a/kew/src/kew/runner.py
        +++ b/kew/src/kew/runner.py
        @@ -1,1 +1,2 @@
         x = 1
        +pytest.skip("not here")
        """
    )
    assert build_report(diff).flags == ()


def test_fix_round_touching_only_tests_flags_the_commit_sha() -> None:
    commits = [
        ("aaa1111", ["kew/src/kew/calc.py", "kew/tests/test_calc.py"]),
        ("bbb2222", ["kew/src/kew/calc.py"]),
        ("ccc3333", ["kew/tests/test_calc.py", "docs/calc.md"]),
    ]
    report = build_report("", commits)

    assert report.flags == (FLAG_TEST_ONLY_FIX_ROUND,)
    assert report.evidence_for(FLAG_TEST_ONLY_FIX_ROUND) == (
        Evidence("kew/tests/test_calc.py", None, "ccc3333", commit="ccc3333"),
    )


def test_fix_round_touching_a_production_module_named_like_a_test_is_not_flagged() -> (
    None
):
    commits = [
        ("aaa1111", ["kew/src/kew/calc.py", "kew/tests/test_calc.py"]),
        ("bbb2222", ["kew/src/kew/test_integrity.py"]),
    ]
    assert build_report("", commits).flags == ()


def test_fix_round_touching_only_a_frontend_test_under_src_is_flagged() -> None:
    commits = [
        ("aaa1111", ["frontend/src/x.ts", "frontend/src/x.test.ts"]),
        ("bbb2222", ["frontend/src/x.test.ts"]),
    ]
    report = build_report("", commits)

    assert report.flags == (FLAG_TEST_ONLY_FIX_ROUND,)
    assert report.evidence_for(FLAG_TEST_ONLY_FIX_ROUND) == (
        Evidence("frontend/src/x.test.ts", None, "bbb2222", commit="bbb2222"),
    )


def test_first_commit_touching_only_tests_is_not_a_fix_round() -> None:
    commits = [("aaa1111", ["kew/tests/test_calc.py"])]
    assert build_report("", commits).flags == ()


ONLY_ADDS = _diff(
    """
    diff --git a/kew/tests/test_calc.py b/kew/tests/test_calc.py
    --- a/kew/tests/test_calc.py
    +++ b/kew/tests/test_calc.py
    @@ -1,3 +1,8 @@
     def test_total():
         assert total([10, 20]).value == 30

    +
    +def test_total_empty():
    +    assert total([]).value == 0
    +    assert total([]).ok
    +
    diff --git a/kew/tests/test_new.py b/kew/tests/test_new.py
    new file mode 100644
    --- /dev/null
    +++ b/kew/tests/test_new.py
    @@ -0,0 +1,2 @@
    +def test_new():
    +    assert new() == 1
    """
)


def test_diff_that_only_adds_tests_and_asserts_is_clean() -> None:
    report = build_report(ONLY_ADDS, [("a", ["kew/tests/test_new.py"])])

    assert report.flags == ()
    assert [e.text for e in report.tests_added] == [
        "def test_total_empty():",
        "def test_new():",
    ]
    assert len(report.asserts_added) == 3
    assert report.asserts_removed == ()


CHANGED_EXPECTED = _diff(
    """
    diff --git a/kew/tests/test_calc.py b/kew/tests/test_calc.py
    --- a/kew/tests/test_calc.py
    +++ b/kew/tests/test_calc.py
    @@ -1,3 +1,3 @@
     def test_total():
    -    assert total([10, 20]).value == 30
    +    assert total([10, 20]).value == 31

    """
)


def test_expected_literal_changed_on_same_left_side_flags() -> None:
    report = build_report(CHANGED_EXPECTED)

    assert report.flags == (FLAG_CHANGED_EXPECTED,)
    (evidence,) = report.evidence_for(FLAG_CHANGED_EXPECTED)
    assert evidence.path == "kew/tests/test_calc.py"
    assert evidence.line == 2
    assert "== 30" in evidence.text and "== 31" in evidence.text


def test_changed_left_side_is_not_a_changed_expected() -> None:
    diff = CHANGED_EXPECTED.replace(
        "+    assert total([10, 20]).value == 31",
        "+    assert total([10, 21]).value == 31",
    )
    assert FLAG_CHANGED_EXPECTED not in build_report(diff).flags


def test_removed_test_function_is_flagged_but_a_moved_one_is_not() -> None:
    removed = _diff(
        """
        --- a/tests/test_a.py
        +++ b/tests/test_a.py
        @@ -1,4 +1,1 @@
         import x
        -
        -def test_gone():
        -    pass
        """
    )
    report = build_report(removed)
    assert report.flags == (FLAG_TESTS_REMOVED,)
    assert report.tests_removed == (Evidence("tests/test_a.py", 3, "def test_gone():"),)

    moved = _diff(
        """
        --- a/tests/test_a.py
        +++ b/tests/test_a.py
        @@ -1,5 +1,5 @@
        -@pytest.mark.skip
        -def test_kept():
        -    assert f() == 1
         import x
        +@pytest.mark.skip
        +def test_kept():
        +    assert f() == 1
         y = 2
        """
    )
    assert build_report(moved).flags == ()


def test_raises_losing_match_or_broadened_is_loosened() -> None:
    diff = _diff(
        """
        --- a/tests/test_err.py
        +++ b/tests/test_err.py
        @@ -1,4 +1,4 @@
         def test_err():
        -    with pytest.raises(ValueError, match="bad id"):
        +    with pytest.raises(ValueError):
                 parse("x")
        -    with pytest.raises(KeyError):
        +    with pytest.raises(Exception):
        """
    )
    report = build_report(diff)

    assert report.flags == (FLAG_RAISES_LOOSENED,)
    assert [e.line for e in report.raises_loosened] == [2, 4]


def test_mock_any_added_is_flagged_but_its_import_is_not() -> None:
    diff = _diff(
        """
        --- a/tests/test_call.py
        +++ b/tests/test_call.py
        @@ -1,2 +1,3 @@
        -import os
        +from unittest.mock import ANY
        -    fake.assert_called_once_with("a", 3)
        +    fake.assert_called_once_with("a", ANY)
        +    other.assert_called_with(mock.ANY)
        """
    )
    report = build_report(diff)

    assert FLAG_MOCK_ANY_ADDED in report.flags
    assert [e.line for e in report.mock_any_added] == [2, 3]


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_x.py", True),
        ("kew/tests/support/helpers.py", True),
        ("pkg/test_thing.py", True),
        ("pkg/thing_test.py", True),
        ("kew/conftest.py", True),
        ("kew/src/kew/test_integrity.py", True),
        ("kew/src/kew/calc.py", False),
        ("docs/testing.md", False),
    ],
)
def test_test_path_globs(path: str, is_test: bool) -> None:
    assert is_test_path(path) is is_test


def test_parse_diff_tracks_base_and_head_line_numbers() -> None:
    (fd,) = parse_diff(ASSERTS_REMOVED)
    assert fd.path == "kew/tests/test_calc.py"
    assert [d.line for d in fd.removed] == [3, 4]
    assert [d.line for d in fd.added] == [3]


def test_deleted_test_file_counts_its_asserts_as_removed() -> None:
    diff = _diff(
        """
        diff --git a/tests/test_old.py b/tests/test_old.py
        deleted file mode 100644
        --- a/tests/test_old.py
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -def test_old():
        -    assert old() == 1
        """
    )
    report = build_report(diff)
    assert report.flags == (FLAG_ASSERTS_REMOVED, FLAG_TESTS_REMOVED)
    assert report.asserts_removed[0].path == "tests/test_old.py"


_BASE_TESTS = dedent(
    """
    import pytest


    def test_rows():
        assert rows() == 2


    class TestQueue:
        def test_read_once(self):
            assert reads() == 1


    def test_untouched():
        assert True
    """
)


def test_an_edited_existing_test_is_named_with_its_head_line() -> None:
    head = _BASE_TESTS.replace("rows() == 2", "rows() == 3")
    assert edited_tests(_BASE_TESTS, head) == (("test_rows", 5),)


def test_an_edited_method_is_named_through_its_class() -> None:
    head = _BASE_TESTS.replace("reads() == 1", "reads() == 2")
    assert edited_tests(_BASE_TESTS, head) == (("TestQueue::test_read_once", 10),)


def test_a_changed_decorator_is_an_edit() -> None:
    head = _BASE_TESTS.replace(
        "def test_rows():", '@pytest.mark.parametrize("n", [1])\ndef test_rows():'
    )
    assert [name for name, _ in edited_tests(_BASE_TESTS, head)] == ["test_rows"]


def test_a_reformat_comment_or_new_test_is_not_an_edit() -> None:
    head = _BASE_TESTS.replace(
        "assert rows() == 2", "assert rows() == (  # same\n        2\n    )"
    )
    head += "\n\ndef test_new():\n    assert new()\n"
    assert edited_tests(_BASE_TESTS, head) == ()


def test_an_unparseable_side_names_no_edit() -> None:
    assert edited_tests(_BASE_TESTS, "def test_rows(:\n") == ()


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (frozenset({"tests/test_a.py::test_rows"}), True),
        (frozenset({"tests/test_a.py::test_rows[one]"}), True),
        (frozenset({"tests/test_a.py::test_rows_more"}), False),
        (frozenset(), False),
    ],
)
def test_a_declaration_covers_the_test_or_its_cases(
    declared: frozenset[str], expected: bool
) -> None:
    assert is_declared("tests/test_a.py::test_rows", declared) is expected
