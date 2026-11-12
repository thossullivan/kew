"""The held-out phase: the base branch's tests, run against the head's src (#2485).

Every other gate runs the head's own tests, which the implementer can edit. The
base branch's tests were written before the implementer started, so a merge
verify also runs *those* against the candidate's ``src`` — and a base test that
fails there fails the verification unless the PR body declares it under
**Behavior changes**.

Each case here is a tiny git repository with a base on ``main`` and one head
commit, verified end to end on the local host: the composed job script runs
in the shipped clone, builds the held-out tree outside it, runs pytest there and
reports, and kew judges the report against the PR body. Only the gate
(``scripts/verify.sh``, a stub) and the test runner (this interpreter's pytest
instead of ``uv run``, because the tiny repository is no uv project) stand in.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from support.held_out_lock import pinned_lock
from support.held_out_log import report_line

from kew import merge_verify
from kew.config import DispatchConfig, VerifyConfig
from kew.held_out_lock_check import main as lock_check_main
from kew.merge_verify import (
    VerifyResult,
    apply_held_out,
    declared_behavior_changes,
    held_out_report,
    run_verify,
)

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

#: The base test the cases below weaken, delete or keep, by node id.
BASE_TEST = "tests/test_calc.py::test_add"

_CALC = "def add(a, b):\n    return a + b\n"
_BROKEN_CALC = "def add(a, b):\n    return a - b\n"
_BASE_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
_WEAKENED_TEST = (
    "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) is not None\n"
)

#: A base test that records where it ran, so a case can ask.
_WHERE_TEST = (
    "import os\nfrom pathlib import Path\n\n\ndef test_where():\n"
    "    Path(os.environ['HOME'], 'held-out-at').write_text(__file__)\n"
)


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> None:
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", message], repo)


def _repo(
    tmp_path: Path,
    head: dict[str, str | None],
    base: dict[str, str | None] | None = None,
) -> Path:
    """A base on ``main`` with one tested function, plus *base*, and *head* on
    top of it."""
    repo = tmp_path / "project"
    repo.mkdir()
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    _write(
        repo,
        {
            "kew/pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "kew/uv.lock": pinned_lock(),
            "kew/src/calc.py": _CALC,
            "kew/tests/test_calc.py": _BASE_TEST,
            "kew/tests/test_where.py": _WHERE_TEST,
            "kew/scripts/verify.sh": "#!/bin/sh\necho GATE RAN\n",
            "scripts/scan-agent-image.sh": "#!/bin/sh\necho PREFLIGHT RAN\n",
            **(base or {}),
        },
    )
    _commit(repo, "base")
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    _write(repo, head)
    _commit(repo, "head")
    return repo


@pytest.fixture(autouse=True)
def _local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local host's tools, a HOME of its own, and pytest in place of uv."""
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # This interpreter, which runs kew's report plugin as the script.
    monkeypatch.setattr(merge_verify, "HELD_OUT_RUNNER", sys.executable)
    # The main phase's selection, without xdist: a one-test repository gains
    # nothing from six workers but their start-up time.
    monkeypatch.setattr(merge_verify, "HELD_OUT_PARALLEL", '-m "not serial"')


def _verify(repo: Path, pr_body: str | None = None) -> VerifyResult:
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    _candidate, result = run_verify(config, store=None, repo_root=repo, pr_body=pr_body)
    return result


def _held_out_at(tmp_path: Path) -> Path:
    """Where the base ``test_where.py`` recorded that it ran."""
    return Path((tmp_path / "home" / "held-out-at").read_text(encoding="utf-8"))


def _checkout_at(tmp_path: Path) -> Path:
    """The checkout root a head's gate stub recorded it ran under."""
    return Path((tmp_path / "home" / "checkout-at").read_text(encoding="utf-8").strip())


def _event_payload(tmp_path: Path, head: dict[str, str | None]) -> dict[str, Any]:
    """The verify event a verification of *head* records."""
    from kew.run_store import SqliteRunStore

    repo = _repo(tmp_path, head)
    config = DispatchConfig()
    config.verify = VerifyConfig(host="local")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        run_verify(config, store=store, repo_root=repo)
        (event,) = store.list_audit_events(event=merge_verify.VERIFY_EVENT)
    payload = event.payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _declaring(node: str) -> str:
    return (
        "## Summary\n\nSubtraction, by request.\n\n"
        "## Behavior changes\n\n"
        f"- `{node}`: add now subtracts; the old sum is no longer the contract\n\n"
        "## Testing\n\n- ran it\n"
    )


class TestTheHeldOutPhase:
    def test_an_honest_head_passes_and_the_phase_ran(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, {"kew/tests/test_calc.py": f"# tidied\n{_BASE_TEST}"})
        result = _verify(repo)
        assert result.outcome == "passed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "passed"
        assert result.held_out.failed == ()
        assert result.held_out.seconds is not None

    def test_ac1_a_weakened_base_test_and_broken_behavior_fail_by_node_id(
        self, tmp_path: Path
    ) -> None:
        """The head's own (weakened) test passes; the base's does not."""
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
        )
        result = _verify(repo)
        assert "GATE RAN" in result.log, "the gate itself passed"
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "failed"
        assert result.held_out.undeclared == (BASE_TEST,)
        assert result.error is not None and BASE_TEST in result.error

    def test_ac2_the_same_failure_passes_when_the_pr_body_declares_it(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
        )
        result = _verify(repo, pr_body=_declaring(BASE_TEST))
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.failed == (BASE_TEST,)
        assert result.held_out.declared == (BASE_TEST,)
        assert result.held_out.undeclared == ()

    def test_ac2_a_declaration_without_a_reason_excuses_nothing(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
        )
        body = f"## Behavior changes\n\n- `{BASE_TEST}`\n"
        result = _verify(repo, pr_body=body)
        assert result.outcome == "failed"
        assert result.held_out is not None
        assert result.held_out.undeclared == (BASE_TEST,)

    @pytest.mark.parametrize(
        ("log_format", "message"),
        [
            (None, "could not add: bus unreachable"),
            ("%(levelname)s %(message)s", "could not add: bus unreachable"),
            ("%(levelname)s %(message)s", "tests/fixtures/data.json is missing"),
        ],
        ids=["pytest-default", "one-space", "one-space-path"],
    )
    def test_an_error_log_record_in_a_failures_capture_is_not_a_failure(
        self, tmp_path: Path, log_format: str | None, message: str
    ) -> None:
        """A captured ERROR record prints as ``ERROR    name:file:line``, or as
        ``ERROR message`` under a one-space ``log_format``, even one naming a
        path under ``tests/``; only result lines name a failing test (#2997)."""
        logging_test = (
            "import logging\n\nfrom calc import add\n\n\ndef test_add():\n"
            f"    logging.getLogger('calc').error({message!r})\n"
            "    assert add(2, 2) == 4\n"
        )
        pyproject = '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
        if log_format is not None:
            pyproject += f'log_format = "{log_format}"\n'
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
            base={
                "kew/tests/test_calc.py": logging_test,
                "kew/pyproject.toml": pyproject,
            },
        )
        result = _verify(repo, pr_body=_declaring(BASE_TEST))
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.failed == (BASE_TEST,)

    def test_ac3_a_deleted_base_test_still_runs_and_its_failure_fails(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": None},
        )
        result = _verify(repo)
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.undeclared == (BASE_TEST,)
        assert result.error is not None and BASE_TEST in result.error

    def test_ac3_a_deleted_base_test_failure_passes_when_declared(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": None},
        )
        result = _verify(repo, pr_body=_declaring(f"kew/{BASE_TEST}"))
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.declared == (BASE_TEST,)

    def test_a_test_only_the_head_added_is_not_held_out(self, tmp_path: Path) -> None:
        """The held-out tree is base's tests: a red head-only test is the main
        phase's business, and here the stub gate does not run it."""
        repo = _repo(
            tmp_path,
            {"kew/tests/test_new.py": "def test_new():\n    assert False\n"},
        )
        result = _verify(repo)
        assert result.outcome == "passed", result.error
        assert result.held_out is not None and result.held_out.failed == ()

    def test_the_held_out_tree_lives_outside_the_checkout(self, tmp_path: Path) -> None:
        """Base's tests ran from a temporary tree of their own, not from the
        checkout every other phase ran against, and the tree is gone after.

        The head's gate stub records the checkout it ran in, so the check is
        on resolved paths, not on a directory name.
        """
        gate = '#!/bin/sh\necho GATE RAN\n(cd .. && pwd -P) > "$HOME/checkout-at"\n'
        repo = _repo(
            tmp_path,
            {
                "kew/tests/test_where.py": f"# tidied\n{_WHERE_TEST}",
                "kew/scripts/verify.sh": gate,
            },
        )
        assert _verify(repo).outcome == "passed"
        ran_at = _held_out_at(tmp_path)
        checkout = _checkout_at(tmp_path)

        def inside_checkout(path: Path) -> bool:
            return path.resolve().is_relative_to(checkout.resolve())

        assert "kew-held-out-" in str(ran_at)
        assert not inside_checkout(ran_at)
        assert not ran_at.exists(), "the held-out tree is removed when the job ends"
        # Negative control: a tree inside the checkout fails the check, and a
        # sibling whose name merely starts with the checkout's does not.
        assert inside_checkout(checkout / "kew-held-out-x" / "repo")
        assert not inside_checkout(checkout.with_name(f"{checkout.name}-x") / "repo")


class TestTheSelection:
    """Only the base versions of the test files the head changed are run
    (#2816); the whole base tree when it changed something they share."""

    def _ran_where(self, tmp_path: Path) -> bool:
        return (tmp_path / "home" / "held-out-at").exists()

    def test_a_src_only_head_skips_the_phase_because_no_test_file_changed(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        result = _verify(repo)
        assert result.outcome == "passed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "skipped"
        assert result.held_out.reason == "no test file changed"
        assert not self._ran_where(tmp_path)

    def test_the_event_records_the_mode_and_how_many_files_it_selected(
        self, tmp_path: Path
    ) -> None:
        payload = _event_payload(tmp_path, {"kew/tests/test_calc.py": _WEAKENED_TEST})
        assert payload["held_out_selection"] == {"mode": "selected", "files": 1}

    def test_a_skipped_phase_records_its_reason(self, tmp_path: Path) -> None:
        payload = _event_payload(tmp_path, {"kew/src/calc.py": _CALC + "\n"})
        assert payload["held_out_selection"] == {"mode": "none", "files": 0}
        assert payload["held_out"]["outcome"] == "skipped"
        assert payload["held_out"]["reason"] == "no test file changed"

    def test_a_modified_test_file_runs_exactly_its_base_version(
        self, tmp_path: Path
    ) -> None:
        """The head's weakened copy would pass; base's fails. The untouched
        base file is not run."""
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
        )
        result = _verify(repo)
        assert result.held_out is not None
        assert result.held_out.failed == (BASE_TEST,)
        assert not self._ran_where(tmp_path)

    def test_a_deleted_test_file_runs_its_base_version(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": None},
        )
        result = _verify(repo)
        assert result.held_out is not None
        assert result.held_out.failed == (BASE_TEST,)
        assert not self._ran_where(tmp_path)

    def test_a_renamed_test_file_runs_its_base_version(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/tests/test_calc.py": None,
                "kew/tests/test_sum.py": _WEAKENED_TEST,
            },
        )
        result = _verify(repo)
        assert result.held_out is not None
        assert result.held_out.failed == (BASE_TEST,)

    @pytest.mark.parametrize(
        "shared",
        ["kew/tests/conftest.py", "kew/tests/support/helpers.py"],
    )
    def test_a_change_the_tests_share_runs_the_full_base_tree(
        self, tmp_path: Path, shared: str
    ) -> None:
        repo = _repo(tmp_path, {shared: "# shared by every test\n"})
        result = _verify(repo)
        assert result.outcome == "passed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "passed"
        assert self._ran_where(tmp_path), "the untouched base file ran too"

    def test_a_test_file_another_imports_from_runs_the_full_base_tree(
        self, tmp_path: Path
    ) -> None:
        """The head weakens a helper in one test file that an untouched test
        file imports; only the full base tree runs that importer against the
        base helper."""
        expect = "def expected_sum():\n    return {}\n\n\ndef test_expected():\n"
        expect += "    assert expected_sum() == {}\n"
        importer = (
            "from calc import add\nfrom test_expect import expected_sum\n\n\n"
            "def test_add_shared():\n    assert add(2, 2) == expected_sum()\n"
        )
        repo = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/tests/test_expect.py": expect.format(0, 0),
            },
            base={
                "kew/tests/test_expect.py": expect.format(4, 4),
                "kew/tests/test_calc_shared.py": importer,
            },
        )
        result = _verify(repo)
        assert result.held_out is not None
        assert "tests/test_calc_shared.py::test_add_shared" in result.held_out.failed
        assert self._ran_where(tmp_path), "the untouched base files ran too"

    def test_an_undeclared_failure_in_a_selected_file_fails_the_verify(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, "kew/tests/test_calc.py": _WEAKENED_TEST},
        )
        result = _verify(repo)
        assert "GATE RAN" in result.log, "the gate itself passed"
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "failed"
        assert result.held_out.undeclared == (BASE_TEST,)
        assert result.error is not None and BASE_TEST in result.error


class TestTheReport:
    """The judgement over a log, without a host."""

    def test_a_gate_that_passed_with_no_held_out_report_fails(self) -> None:
        result = apply_held_out(
            VerifyResult(host="local", exit_code=0, seconds=1.0, log="gate ok\n"),
            runs=2,
            declared=frozenset(),
        )
        assert result.outcome == "failed"
        assert result.held_out is not None
        assert result.held_out.outcome == "not_run"

    def test_a_gate_that_already_failed_keeps_its_own_status(self) -> None:
        result = apply_held_out(
            VerifyResult(host="local", exit_code=3, seconds=1.0, log=""),
            runs=2,
            declared=frozenset(),
        )
        assert result.exit_code == 3

    def test_a_missing_run_fails(self) -> None:
        report = held_out_report(
            "kew-held-out: exit 0 seconds 4\n", runs=2, declared=frozenset()
        )
        assert report.outcome == "failed" and "1 of 2" in (report.error or "")

    def test_a_run_that_broke_fails_even_with_nothing_named(self) -> None:
        log = "kew-held-out: exit 0 seconds 1\nkew-held-out: exit 4 seconds 1\n"
        report = held_out_report(log, runs=2, declared=frozenset())
        assert report.outcome == "failed" and "exited 4" in (report.error or "")

    def test_a_red_run_that_names_nothing_fails(self) -> None:
        log = "kew-held-out: exit 1 seconds 1\nkew-held-out: exit 0 seconds 1\n"
        report = held_out_report(log, runs=2, declared=frozenset())
        assert report.outcome == "failed"

    def test_a_base_without_tests_is_skipped(self) -> None:
        report = held_out_report(
            "kew-held-out: skipped the base has no kew/tests\n",
            runs=2,
            declared=frozenset(),
        )
        assert report.outcome == "skipped" and report.error is None

    def test_the_failure_message_is_cut_off_the_node_id(self) -> None:
        log = (
            f"{report_line(BASE_TEST)}kew-held-out: exit 1 seconds 2\n"
            f"{report_line()}kew-held-out: exit 0 seconds 3\n"
        )
        report = held_out_report(log, runs=2, declared=frozenset({BASE_TEST}))
        assert report.failed == (BASE_TEST,) and report.outcome == "passed"
        assert report.seconds == 5


class TestTheDeclaration:
    def test_only_the_behavior_changes_section_declares(self) -> None:
        body = (
            "## Summary\n- `tests/test_a.py::test_x`: mentioned in passing\n"
            "**Behavior changes**\n"
            "- `kew/tests/test_b.py::test_y` — now raises instead of returning\n"
            "**Testing**\n- `tests/test_c.py::test_z`: ran it\n"
        )
        assert declared_behavior_changes(body) == {"tests/test_b.py::test_y"}

    def test_no_body_declares_nothing(self) -> None:
        assert declared_behavior_changes(None) == frozenset()


def _failing(*nodes: str) -> str:
    """A held-out log whose first run failed *nodes* and second passed."""
    return (
        f"{report_line(*nodes)}kew-held-out: exit 1 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )


class TestADeclaredFunctionCoversItsParametrizations:
    """A test id with no ``[...]`` declares every parametrization of it (#2795)."""

    def test_a_bare_function_covers_an_id_with_spaces_and_quotes(self) -> None:
        node = 'tests/x.py::T::test_a[a b "c"]'
        report = held_out_report(
            _failing(node), runs=2, declared=frozenset({"tests/x.py::T::test_a"})
        )
        assert report.outcome == "passed"
        assert report.declared == (node,) and report.undeclared == ()

    def test_a_bare_function_from_the_pr_body_covers_its_ids(self) -> None:
        body = (
            "**Behavior changes**\n"
            "- `tests/x.py::T::test_a` — every case now expects the new error\n"
        )
        node = "tests/x.py::T::test_a[module.exports = { expand: () => ['x'] };]"
        report = held_out_report(
            _failing(node), runs=2, declared=declared_behavior_changes(body)
        )
        assert report.outcome == "passed"

    def test_it_covers_no_other_test(self) -> None:
        others = ("tests/x.py::T::test_ab[1]", "tests/x.py::T::test_b")
        report = held_out_report(
            _failing(*others), runs=2, declared=frozenset({"tests/x.py::T::test_a"})
        )
        assert report.outcome == "failed"
        assert report.undeclared == others

    def test_a_file_covers_no_test_in_it(self) -> None:
        node = "tests/x.py::T::test_a[1]"
        report = held_out_report(
            _failing(node), runs=2, declared=frozenset({"tests/x.py"})
        )
        assert report.outcome == "failed" and report.undeclared == (node,)

    def test_an_exact_parametrized_declaration_still_works(self) -> None:
        report = held_out_report(
            _failing("tests/x.py::T::test_a[1]", "tests/x.py::T::test_a[2]"),
            runs=2,
            declared=frozenset({"tests/x.py::T::test_a[1]"}),
        )
        assert report.declared == ("tests/x.py::T::test_a[1]",)
        assert report.undeclared == ("tests/x.py::T::test_a[2]",)

    def test_a_contract_parametrization_is_never_declared_away(self) -> None:
        contract = "tests/test_contract.py"
        node = f"{contract}::test_a[1]"
        report = held_out_report(
            _failing(node),
            runs=2,
            declared=frozenset({f"{contract}::test_a"}),
            contract=contract,
        )
        assert report.outcome == "failed" and report.undeclared == (node,)


#: A base test file the head's src keeps from importing, and its tests.
BROKEN_FILE = "tests/test_x.py"
BROKEN_A = f"{BROKEN_FILE}::test_a"
BROKEN_B = f"{BROKEN_FILE}::test_b[1]"
_FILE_LINE = (
    "**Behavior changes**\n"
    f"- `{BROKEN_FILE}` — the module it imports moved, as the issue asks\n"
)


def test_one_line_for_a_file_that_failed_to_collect_declares_its_uncollected_tests() -> (  # noqa: E501 — the issue names this test
    None
):
    log = (
        f"{report_line(BROKEN_FILE, uncollected=[BROKEN_A, BROKEN_B])}"
        "kew-held-out: exit 1 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )
    report = held_out_report(
        log, runs=2, declared=declared_behavior_changes(_FILE_LINE)
    )
    assert report.outcome == "passed", report.error
    assert report.declared == (BROKEN_FILE, BROKEN_A, BROKEN_B)
    assert report.undeclared == ()


def test_a_file_line_does_not_declare_a_test_of_that_file_that_ran_and_failed() -> None:
    """One run never collected the file; the other collected BROKEN_A and it
    failed there — a failure the file line does not explain."""
    log = (
        f"{report_line(BROKEN_FILE, uncollected=[BROKEN_B])}"
        "kew-held-out: exit 1 seconds 1\n"
        f"{report_line(BROKEN_A)}kew-held-out: exit 1 seconds 1\n"
    )
    report = held_out_report(
        log, runs=2, declared=declared_behavior_changes(_FILE_LINE)
    )
    assert report.outcome == "failed"
    assert report.undeclared == (BROKEN_A,)
    assert report.declared == (BROKEN_FILE, BROKEN_B)


def test_a_file_line_never_declares_a_contract_file_that_failed_to_collect() -> None:
    log = (
        f"{report_line(BROKEN_FILE, uncollected=[BROKEN_A])}"
        "kew-held-out: exit 1 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )
    report = held_out_report(
        log,
        runs=2,
        declared=declared_behavior_changes(_FILE_LINE),
        contract=BROKEN_FILE,
    )
    assert report.outcome == "failed"
    assert report.undeclared == (BROKEN_FILE, BROKEN_A)


def test_a_file_line_declares_no_uncollected_test_when_the_file_collected() -> None:
    """Tests the run never collected while their file collected cleanly have
    another cause than a collection error, so each still needs its own line."""
    log = (
        f"{report_line(uncollected=[BROKEN_A])}kew-held-out: exit 1 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )
    report = held_out_report(
        log, runs=2, declared=declared_behavior_changes(_FILE_LINE)
    )
    assert report.outcome == "failed" and report.undeclared == (BROKEN_A,)


def _project(*dependencies: str, wheels: Path | None = None) -> str:
    """A ``kew/pyproject.toml`` for the ``calc`` project, declaring
    *dependencies*, found in the *wheels* directory alone when one is named —
    so the job's own ``uv sync`` resolves them with no network."""
    listed = ", ".join(f'"{dependency}"' for dependency in dependencies)
    index = (
        ""
        if wheels is None
        else f'[tool.uv]\nno-index = true\nfind-links = ["{wheels}"]\n\n'
    )
    return (
        '[project]\nname = "calc"\nversion = "1"\nrequires-python = ">=3.11"\n'
        f"dependencies = [{listed}]\n\n{index}"
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
    )


def _unlocked_wheel(tmp_path: Path) -> Path:
    """A directory holding a wheel of ``kew-unlocked-dependency``, which ships
    no module: the job's ``uv sync`` installs it, and the held-out run, on an
    environment that never had it, cannot import it."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    info = "kew_unlocked_dependency-1.0.dist-info"
    files = {
        f"{info}/METADATA": (
            "Metadata-Version: 2.1\nName: kew-unlocked-dependency\nVersion: 1.0\n"
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: kew-tests\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    record = "".join(f"{name},,\n" for name in files) + f"{info}/RECORD,,\n"
    with zipfile.ZipFile(
        wheels / "kew_unlocked_dependency-1.0-py3-none-any.whl", "w"
    ) as wheel:
        for name, text in {**files, f"{info}/RECORD": record}.items():
            wheel.writestr(name, text)
    return wheels


def _project_lock(*dependencies: str) -> str:
    """A ``kew/uv.lock`` whose entry for ``calc`` records *dependencies*, as
    ``uv lock`` writes the project's own entry."""
    recorded = ", ".join(f'{{ name = "{dependency}" }}' for dependency in dependencies)
    locked = "".join(
        f'[[package]]\nname = "{dependency}"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        for dependency in dependencies
    )
    return (
        f"{pinned_lock()}{locked}"
        '[[package]]\nname = "calc"\nversion = "1"\nsource = { editable = "." }\n'
        f"dependencies = [{recorded}]\n\n"
        f"[package.metadata]\nrequires-dist = [{recorded}]\n"
    )


#: The head's src imports a dependency the base's tests then fail to import.
_IMPORTS_UNLOCKED = f"import kew_unlocked_dependency  # noqa: F401\n{_CALC}"


def test_a_stale_head_lock_is_named_as_the_cause(tmp_path: Path) -> None:
    """The #3234 shape: the head declares a dependency in its pyproject.toml
    and never runs ``uv lock``, so its lock is the base's, which records no
    such dependency. The frozen run has no such module, so the base tests fail
    to collect — and the error says the lock is why, not only that base tests
    failed. Read from the two files alone: no resolver, no network."""
    wheels = _unlocked_wheel(tmp_path)
    repo = _repo(
        tmp_path,
        {
            "kew/pyproject.toml": _project("kew_unlocked.dependency>=1", wheels=wheels),
            "kew/src/calc.py": _IMPORTS_UNLOCKED,
            "kew/tests/test_calc.py": f"# tidied\n{_BASE_TEST}",
        },
        base={"kew/pyproject.toml": _project(), "kew/uv.lock": _project_lock()},
    )
    result = _verify(repo)
    assert result.held_out is not None
    assert result.held_out.outcome == "failed", result.log
    error = result.held_out.error or ""
    assert error.startswith(f"{merge_verify.STALE_LOCK_ERROR}; "), error
    assert "base tests failed against the candidate's src" in error


def test_a_head_lock_that_records_the_new_dependency_is_not_named(
    tmp_path: Path,
) -> None:
    """The head locked what it declares, under the name ``uv lock`` writes:
    the base tests still fail (the dependency is not installed here), but the
    lock is not named as the cause."""
    wheels = _unlocked_wheel(tmp_path)
    repo = _repo(
        tmp_path,
        {
            "kew/pyproject.toml": _project("kew_unlocked.dependency>=1", wheels=wheels),
            "kew/uv.lock": _project_lock("kew-unlocked-dependency"),
            "kew/src/calc.py": _IMPORTS_UNLOCKED,
            "kew/tests/test_calc.py": f"# tidied\n{_BASE_TEST}",
        },
        base={"kew/pyproject.toml": _project(), "kew/uv.lock": _project_lock()},
    )
    result = _verify(repo)
    assert result.held_out is not None
    assert result.held_out.outcome == "failed", result.log
    error = result.held_out.error or ""
    assert "base tests failed against the candidate's src" in error
    assert "uv.lock" not in error


def test_a_stale_head_lock_is_not_named_when_the_phase_saw_none() -> None:
    report = held_out_report(_failing(BASE_TEST), runs=2, declared=frozenset())
    assert report.outcome == "failed"
    assert "uv.lock" not in (report.error or "")


def _lock_check(tmp_path: Path, pyproject: str, lock: str) -> tuple[str, str]:
    """What the lock check prints, on stdout and stderr, for the two files."""
    project, locked = tmp_path / "pyproject.toml", tmp_path / "uv.lock"
    project.write_text(pyproject, encoding="utf-8")
    locked.write_text(lock, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert lock_check_main([str(project), str(locked)]) == 0
    return out.getvalue(), err.getvalue()


_GROUPED_PROJECT = (
    '[project]\nname = "calc"\nversion = "1"\ndependencies = ["typer"]\n\n'
    '[dependency-groups]\ndev = ["Kew_Tool>=1"]\n'
)


def _grouped_lock(*, dev: str, runtime: str = '{ name = "typer" }') -> str:
    return (
        '[[package]]\nname = "calc"\nversion = "1"\nsource = { editable = "." }\n'
        f"dependencies = [{runtime}]\n\n"
        f"[package.metadata]\nrequires-dist = [{runtime}]\n\n"
        f"[package.metadata.requires-dev]\ndev = [{dev}]\n"
    )


@pytest.mark.parametrize(
    ("lock", "printed"),
    [
        pytest.param(_grouped_lock(dev='{ name = "kew-tool" }'), "", id="locked"),
        pytest.param(_grouped_lock(dev=""), "stale\n", id="group-unlocked"),
        pytest.param(
            _grouped_lock(dev="", runtime='{ name = "typer" }, { name = "kew-tool" }'),
            "stale\n",
            id="locked-as-a-runtime-dependency-only",
        ),
    ],
)
def test_the_lock_check_reads_dependency_groups_against_the_lock_s_dev_groups(
    tmp_path: Path, lock: str, printed: str
) -> None:
    out, _err = _lock_check(tmp_path, _GROUPED_PROJECT, lock)
    assert out == printed


def test_a_lock_with_no_entry_for_the_project_is_stale(tmp_path: Path) -> None:
    out, _err = _lock_check(tmp_path, _project(), pinned_lock())
    assert out == "stale\n"


def test_the_lock_check_on_an_interpreter_without_tomllib_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interpreter older than 3.11 has no tomllib: the check prints no
    verdict and names the reason on stderr instead of crashing."""
    monkeypatch.setitem(sys.modules, "tomllib", None)
    out, err = _lock_check(tmp_path, _project(), _project_lock())
    assert out == ""
    assert "tomllib" in err
