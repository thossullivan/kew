"""Which base test files the held-out phase runs, read off a diff (#2816)."""

from __future__ import annotations

import pytest

from kew.held_out_selection import HeldOutSelection, select_held_out

_PYPROJECT = "kew/pyproject.toml"
_CONFIG = '[tool.pytest.ini_options]\naddopts = "-q"\n'


def _select(
    *changes: tuple[str, ...],
    base: str | None = _CONFIG,
    head: str | None = _CONFIG,
    mentions: dict[str, tuple[str, ...]] | None = None,
) -> HeldOutSelection:
    """*mentions* maps a module name to the base files that name it; a module
    without an entry is named nowhere."""
    name_status = "".join("\0".join(change) + "\0" for change in changes)
    return select_held_out(
        name_status,
        project="kew",
        tests="kew/tests",
        read_base=lambda _path: base,
        read_head=lambda _path: head,
        base_mentions=lambda modules: [
            path for module in modules for path in (mentions or {}).get(module, ())
        ],
    )


def test_no_change_under_the_test_tree_selects_nothing() -> None:
    assert _select(("M", "kew/src/kew/x.py")) == HeldOutSelection(mode="none")


def test_an_added_test_file_selects_nothing() -> None:
    assert _select(("A", "kew/tests/test_new.py")).mode == "none"


def test_a_modified_frontend_test_selects_nothing() -> None:
    # A vitest file is not a base pytest file: the held-out phase never runs it,
    # so editing one alongside its component holds nothing out.
    selection = _select(
        ("M", "frontend/src/lib/work/first-run.test.ts"),
        ("M", "frontend/src/lib/work/FirstRunCard.svelte"),
        ("M", "frontend/scripts/ui-checks/command-lines.mjs"),
    )
    assert selection == HeldOutSelection(mode="none")


def test_modified_and_deleted_test_files_are_selected_once_each() -> None:
    selection = _select(
        ("M", "kew/tests/test_a.py"),
        ("D", "kew/tests/sub/test_b.py"),
        ("M", "kew/src/kew/x.py"),
    )
    assert selection == HeldOutSelection(
        mode="selected", files=("kew/tests/test_a.py", "kew/tests/sub/test_b.py")
    )


def test_a_rename_selects_the_base_path() -> None:
    selection = _select(("R087", "kew/tests/test_a.py", "kew/tests/test_c.py"))
    assert selection.files == ("kew/tests/test_a.py",)


def test_a_rename_into_the_test_tree_is_an_addition() -> None:
    assert _select(("R100", "kew/scratch/test_a.py", "kew/tests/test_a.py")).mode == (
        "none"
    )


@pytest.mark.parametrize(
    "path",
    [
        "kew/tests/conftest.py",
        "kew/tests/support/tui_wait.py",
        "kew/tests/fixtures/data.json",
        "kew/tests/__init__.py",
        "kew/conftest.py",
    ],
)
def test_a_file_the_tests_share_selects_the_full_tree(path: str) -> None:
    assert _select(("M", "kew/tests/test_a.py"), ("A", path)).mode == "full"


def test_a_pytest_config_change_selects_the_full_tree() -> None:
    head = '[tool.pytest.ini_options]\naddopts = "-q --ignore=tests/test_a.py"\n'
    assert _select(("M", _PYPROJECT), head=head).mode == "full"


def test_a_pyproject_change_outside_pytest_config_is_not_shared() -> None:
    head = _CONFIG + '[project]\nname = "kew"\n'
    assert _select(("M", _PYPROJECT), head=head).mode == "none"


def test_an_unparseable_pyproject_selects_the_full_tree() -> None:
    assert _select(("M", _PYPROJECT), head="[tool.pytest\n").mode == "full"


def test_a_selected_file_another_test_module_names_selects_the_full_tree() -> None:
    selection = _select(
        ("M", "kew/tests/test_helpers.py"),
        mentions={"test_helpers": ("kew/tests/test_uses_helpers.py",)},
    )
    assert selection.mode == "full"


def test_a_selected_file_named_only_by_selected_files_stays_selected() -> None:
    selection = _select(
        ("M", "kew/tests/test_a.py"),
        ("D", "kew/tests/test_b.py"),
        mentions={"test_a": ("kew/tests/test_a.py", "kew/tests/test_b.py")},
    )
    assert selection == HeldOutSelection(
        mode="selected", files=("kew/tests/test_a.py", "kew/tests/test_b.py")
    )


def test_a_mention_search_that_cannot_answer_selects_the_full_tree() -> None:
    selection = select_held_out(
        "M\0kew/tests/test_a.py\0",
        project="kew",
        tests="kew/tests",
        read_base=lambda _path: _CONFIG,
        read_head=lambda _path: _CONFIG,
        base_mentions=lambda _modules: None,
    )
    assert selection.mode == "full"
