"""Merge verify's diff-scoped mutation phase (#2495).

The sampler picks only the mutants on the ``src/`` lines a change touched,
named as mutmut 3 names them; a run over the time cap stops and still reports
what it finished, naming the cap; and merge verify reads the report off the
job log into the verify event without it ever touching the verdict.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import replace
from pathlib import Path
from types import ModuleType

from mutmut.__main__ import get_mutant_name
from mutmut.file_mutation import mutate_file_contents

from kew.merge_verify import (
    MUTATION_CAP_SECONDS,
    VerifyCandidate,
    VerifyResult,
    job_script,
    mutation_job_script,
    mutation_lines,
    mutation_report,
    summary_line,
)

KEW = Path(__file__).resolve().parents[1]
SAMPLER = KEW / "scripts" / "mutmut-sampler.py"
MODULE = "src/kew/shapes.py"

# Lines 10-20 are `area`'s body; `perimeter` (above) and `scale` (below) are
# full of mutants too, so a selection that ignored the lines would show.
BASE_SOURCE = textwrap.dedent(
    """\
    def perimeter(width, height):
        if width < 0 or height < 0:
            return None
        return 2 * (width + height)


    def area(width, height):
        if width < 0:
            return None
        total = 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        total = total + 0
        return width * height


    def scale(value, factor):
        if factor > 10:
            return value * 10
        return value * factor + 1
    """
)

CHANGED_SOURCE = BASE_SOURCE.replace(
    "    total = total + 0\n", "    total = total - 1\n"
).replace("    total = 0\n", "    total = 1\n")

PYPROJECT = textwrap.dedent(
    """\
    [project]
    name = "shapes"

    [tool.mutmut]
    paths_to_mutate = [
        "src/kew/other.py",
    ]
    tests_dir = [
        "tests/test_other.py",
    ]
    """
)

# A stand-in for `mutmut`: `run` records its first mutant as survived — the
# work a real run finishes before the cap — then hangs past any cap; `results`
# prints what was recorded, as mutmut 3 does.
FAKE_MUTMUT = textwrap.dedent(
    """\
    import sys, time
    from pathlib import Path
    state = Path("fake-mutmut-results.txt")
    if sys.argv[1] == "run":
        state.write_text(f"    {sys.argv[2]}: survived\\n")
        time.sleep(60)
    else:
        print(state.read_text() if state.exists() else "", end="")
    """
)


def _sampler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutmut_sampler", SAMPLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@invalid.local", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _mutmut_table(root: Path) -> dict:
    """The ``[tool.mutmut]`` config the phase left in *root*'s pyproject.toml."""
    return tomllib.loads((root / "pyproject.toml").read_text())["tool"]["mutmut"]


def _project(tmp_path: Path) -> tuple[Path, str]:
    """A kew-shaped project whose HEAD rewrites lines 10-20 of one module."""
    root = tmp_path / "kew"
    (root / "src" / "kew").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / MODULE).write_text(BASE_SOURCE)
    (root / "tests" / "test_shapes.py").write_text("def test_nothing():\n    pass\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    (root / MODULE).write_text(CHANGED_SOURCE)
    _git(root, "commit", "-q", "-am", "change lines 10-20")
    return root, base


def test_a_diff_touching_lines_10_to_20_selects_only_mutants_inside_them(
    tmp_path: Path,
) -> None:
    sampler = _sampler()
    root, base = _project(tmp_path)
    diff = _git(root, "diff", "-U0", "--relative", base, "HEAD", "--", "src/")

    lines = sampler.changed_lines(diff)
    assert lines == {MODULE: set(range(10, 21))}

    selected = sampler.select_mutants(MODULE, CHANGED_SOURCE, lines[MODULE])
    every = sampler.select_mutants(MODULE, CHANGED_SOURCE, set(range(1, 100)))
    assert selected, "the changed lines carry mutants"
    assert {m.line for m in selected} <= set(range(10, 21))
    assert {m.line for m in every} - set(range(10, 21)), "mutants outside exist"
    assert [m for m in every if 10 <= m.line <= 20] == selected
    assert all(m.path == MODULE for m in selected)
    assert any(m.mutation == "'total - 1' -> 'total + 1'" for m in selected)


def test_selected_names_are_the_names_mutmut_gives_the_same_mutants() -> None:
    sampler = _sampler()
    _, names = mutate_file_contents(MODULE, CHANGED_SOURCE)
    every = sampler.select_mutants(MODULE, CHANGED_SOURCE, set(range(1, 100)))
    assert [m.name for m in every] == [get_mutant_name(Path(MODULE), n) for n in names]
    assert every[0].name.startswith("kew.shapes.x_perimeter__mutmut_")


def test_a_run_over_the_cap_stops_and_reports_partial_results_naming_the_cap(
    tmp_path: Path,
) -> None:
    sampler = _sampler()
    root, base = _project(tmp_path)
    fake = tmp_path / "fake_mutmut.py"
    fake.write_text(FAKE_MUTMUT)

    report = sampler.diff_scope(
        base,
        1,
        root=root,
        mutmut=(sys.executable, str(fake)),
        grace=10,
        log=Path("mutmut.log"),
    )

    log = "\n".join(report)
    assert "kew-mutation: capped the run hit the 1s cap; these results are partial" in (
        report
    )
    survived = [line for line in report if line.startswith("kew-mutation: survived")]
    assert len(survived) == 1
    assert survived[0].startswith(f"kew-mutation: survived {MODULE}:1")
    parsed = mutation_report(log)
    assert parsed.outcome == "capped"
    assert parsed.cap_seconds == 1
    assert parsed.reason == "the run hit the 1s cap; results are partial"
    assert len(parsed.survivors) == 1 and parsed.survivors[0].path == MODULE
    assert 10 <= parsed.survivors[0].line <= 20
    assert parsed.selected is not None and parsed.selected > 1
    assert parsed.not_checked == parsed.selected - 1
    assert parsed.seconds is not None and parsed.seconds < 10
    # The phase ran mutmut on the changed module and the change's own suite.
    config = _mutmut_table(root)
    assert config["paths_to_mutate"] == [MODULE]
    assert config["tests_dir"] == ["tests/test_shapes.py"]


def test_a_run_inside_the_cap_is_not_capped(tmp_path: Path) -> None:
    sampler = _sampler()
    code, capped = sampler.run_capped(
        [sys.executable, "-c", "raise SystemExit(3)"], 30, log=tmp_path / "out.log"
    )
    assert (code, capped) == (3, False)


def test_a_change_with_no_src_lines_is_skipped(tmp_path: Path) -> None:
    sampler = _sampler()
    root, _ = _project(tmp_path)
    head = _git(root, "rev-parse", "HEAD").strip()
    assert sampler.diff_scope(head, 1, root=root, mutmut=("false",)) == [
        "kew-mutation: skipped no src/ python lines changed"
    ]


def _candidate() -> VerifyCandidate:
    return VerifyCandidate(
        ref="HEAD",
        ref_sha="a" * 40,
        base_ref="origin/main",
        base_sha="b" * 40,
        frontend_changed=False,
    )


def test_a_mutation_phase_that_dies_never_fails_the_job() -> None:
    # `uv` stands in for a sampler that dies: a shell function exiting 3.
    lines = ["set -eu", "uv() { return 3; }", *mutation_lines(_candidate())]
    out = subprocess.run(
        ["bash", "-c", "\n".join([*lines, "echo after"])],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0
    assert out.stdout.splitlines() == [
        "kew-mutation: error the mutation phase exited 3",
        "after",
    ]
    assert mutation_report(out.stdout).outcome == "error"


def test_the_report_job_runs_the_phase_against_the_base_with_the_cap() -> None:
    script = mutation_job_script(_candidate())
    assert f"--diff-scope {'b' * 40} --cap {MUTATION_CAP_SECONDS}" in script
    # Off the gate (#2817): the gating job runs no mutation phase at all.
    assert "mutmut-sampler" not in job_script(_candidate(), images=[])


def test_the_verdict_and_summary_carry_the_report_without_changing_the_outcome() -> (
    None
):
    log = "\n".join(
        [
            "kew-mutation: selected 3 mutants in 1 files against 1 test files",
            f"kew-mutation: survived {MODULE}:12 kew.shapes.x_area__mutmut_4 "
            "'-' -> '+'",
            "kew-mutation: done exit=0 capped=no cap=600s seconds=42 selected=3 "
            "killed=2 survived=1 not_checked=0 other=0",
        ]
    )
    passed = VerifyResult(host="local", exit_code=0, seconds=1.0, log=log)
    result = replace(passed, mutation=mutation_report(log))
    assert result.outcome == "passed"
    assert result.mutation is not None
    assert result.mutation.outcome == "completed"
    assert result.mutation.survivors[0].mutation == "'-' -> '+'"
    assert summary_line(_candidate(), result).endswith(
        " mutation=completed survived=1/3 mutation_seconds=42"
    )


def test_a_log_without_the_phase_reads_not_run() -> None:
    assert mutation_report("nothing here").outcome == "not_run"


def test_a_mutmut_run_that_dies_reads_as_an_error_not_zero_survivors() -> None:
    # mutmut 3 exits 1 ("failed to collect stats") when a scoped test fails
    # under mutants/; every mutant is left unchecked.
    log = (
        "kew-mutation: done exit=1 capped=no cap=600s seconds=7 selected=4 "
        "killed=0 survived=0 not_checked=4 other=0"
    )
    report = mutation_report(log)
    assert report.outcome == "error"
    assert report.reason == "mutmut exited 1 before finishing"
    assert report.not_checked == 4
    passed = VerifyResult(host="local", exit_code=0, seconds=1.0, log=log)
    line = summary_line(_candidate(), replace(passed, mutation=report))
    assert line.endswith(" mutation=error mutation_seconds=7")
    assert "survived=" not in line


# A stand-in for a mutmut whose baseline run stops on something that is not a
# test it can deselect: it prints its traceback and exits 1, checking nothing.
STATS_CRASH_MUTMUT = textwrap.dedent(
    """\
    import sys
    if sys.argv[1] == "run":
        print("Running stats")
        print("AttributeError: 'NoneType' object has no attribute 'max_stack_depth'")
        print("failed to collect stats. runner returned 1")
        raise SystemExit(1)
    """
)

# A stand-in for a mutmut whose baseline fails on one test until the phase
# deselects it — a test that runs kew in a child process, which inherits
# MUTANT_UNDER_TEST=stats but not mutmut's config (#2804). Deselected, every
# mutant is checked and killed.
CHILD_TEST = "tests/test_shapes.py::test_child_process"
BASELINE_MUTMUT = textwrap.dedent(
    f"""\
    import sys, tomllib
    from pathlib import Path
    state = Path("fake-mutmut-results.txt")
    config = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["mutmut"]
    if sys.argv[1] == "run":
        runs = Path("runs.txt")
        runs.write_text(str(int(runs.read_text()) + 1 if runs.exists() else 1))
        if "--deselect={CHILD_TEST}" not in config.get("pytest_add_cli_args", []):
            print("FAILED {CHILD_TEST} - AssertionError: child failed")
            print("!!! stopping after 1 failures !!!")
            print("failed to collect stats. runner returned 1")
            raise SystemExit(1)
        state.write_text("".join(f"    {{name}}: killed\\n" for name in sys.argv[2:]))
    else:
        print(state.read_text() if state.exists() else "", end="")
    """
)


def _diff_scope(tmp_path: Path, fake_source: str) -> tuple[Path, list[str]]:
    sampler = _sampler()
    root, base = _project(tmp_path)
    fake = tmp_path / "fake_mutmut.py"
    fake.write_text(fake_source)
    report = sampler.diff_scope(
        base,
        60,
        root=root,
        mutmut=(sys.executable, str(fake)),
        grace=10,
        log=Path("mutmut.log"),
    )
    return root, report


def test_a_mutmut_run_that_checks_no_mutant_records_its_reason_in_the_verify_event(
    tmp_path: Path,
) -> None:
    from kew.config import DispatchConfig
    from kew.merge_verify import VERIFY_EVENT, record_verify_event
    from kew.run_store import SqliteRunStore

    _, report = _diff_scope(tmp_path, STATS_CRASH_MUTMUT)

    assert "kew-mutation: reason failed to collect stats. runner returned 1" in report
    log = "\n".join(report)
    parsed = mutation_report(log)
    assert parsed.outcome == "error"
    assert parsed.selected is not None and parsed.selected == parsed.not_checked
    assert parsed.reason == (
        "mutmut exited 1 before finishing: failed to collect stats. runner returned 1"
    )
    result = replace(
        VerifyResult(host="local", exit_code=0, seconds=1.0, log=log), mutation=parsed
    )
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        record_verify_event(store, _candidate(), result, config=DispatchConfig())
        (event,) = store.list_audit_events(event=VERIFY_EVENT)
    payload = event.payload
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    assert payload["outcome"] == "passed"
    assert payload["mutation"]["outcome"] == "error"
    assert payload["mutation"]["reason"] == parsed.reason
    assert payload["mutation"]["deselected"] == []


def test_a_baseline_test_failing_under_mutants_is_deselected_and_the_mutants_checked(
    tmp_path: Path,
) -> None:
    root, report = _diff_scope(tmp_path, BASELINE_MUTMUT)

    assert (
        f"kew-mutation: deselected {CHILD_TEST} failed the baseline run under mutants/"
        in report
    )
    assert not [line for line in report if line.startswith("kew-mutation: reason")]
    parsed = mutation_report("\n".join(report))
    assert parsed.outcome == "completed"
    assert parsed.selected is not None and parsed.selected > 1
    assert parsed.killed == parsed.selected and parsed.not_checked == 0
    assert parsed.deselected == (CHILD_TEST,)
    assert (root / "runs.txt").read_text() == "2"
    assert _mutmut_table(root)["pytest_add_cli_args"] == [f"--deselect={CHILD_TEST}"]


def test_a_baseline_failure_deselecting_does_not_fix_names_the_test(
    tmp_path: Path,
) -> None:
    # The same test fails whatever is deselected: one re-run, then the reason.
    stubborn = BASELINE_MUTMUT.replace(
        f'"--deselect={CHILD_TEST}" not in', '"never" not in'
    )
    root, report = _diff_scope(tmp_path, stubborn)

    parsed = mutation_report("\n".join(report))
    assert parsed.outcome == "error"
    assert parsed.reason == (
        "mutmut exited 1 before finishing: baseline test failed under mutants/: "
        f"{CHILD_TEST}; failed to collect stats. runner returned 1"
    )
    assert parsed.deselected == (CHILD_TEST,)
    assert (root / "runs.txt").read_text() == "2"


def test_a_capped_run_reads_capped_whatever_mutmut_exited() -> None:
    log = (
        "kew-mutation: done exit=-2 capped=yes cap=600s seconds=630 selected=4 "
        "killed=1 survived=0 not_checked=3 other=0"
    )
    assert mutation_report(log).outcome == "capped"
