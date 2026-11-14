"""The test-value report: which tests can go without losing coverage or a kill.

Every test here runs the real tool on a small fixture repository laid out as
kew is: the project sits one level below the repo root, with the tool itself
under ``<project>/scripts/``. The measuring run is a real pytest run with
coverage contexts, and the acceptance case runs real mutmut. Elsewhere mutmut
is a stand-in executable at the process boundary, printing the verdicts a test
sets, so a verdict the real one gives only on a loaded machine (``segfault``,
``timeout``) can be pinned. The clock, the load average and the free-disk
reading are fixed; the report files, exit codes, processes and the verify
lease are read back.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kew.verify_lease import hold_verify_lease, verify_running

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test_value_report.py"
GIB = 1024**3
CALC = "src/fixpkg/calc.py"
T = "tests/test_calc.py::"

_PYPROJECT = """\
[project]
name = "fixpkg"
version = "0"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
markers = ["policy: repo-wide gate", "serial: one process"]

[tool.mutmut]
also_copy = ["src/fixpkg/"]
do_not_mutate = ["*/never_mutated.py"]
"""

_MODULE = """\
def add(a, b):
    return a + b


def negate(x):
    return -x


def scale(x):
    return x * 2


def touch():
    return None
"""

# mutmut times a mutant out after (its tests' stats-run seconds + 1) x 15 wall
# seconds. Padding the stats run gives every mutant 45 s or more, so a loaded
# machine cannot turn a kill into a timeout.
_CONFTEST = """\
import os
import time

import pytest


@pytest.fixture(autouse=True)
def _generous_mutant_timeouts():
    if os.environ.get("MUTANT_UNDER_TEST") == "stats":
        time.sleep(2)
    yield
"""

# test_add: every line it covers is covered by test_add_and_negate, which kills
# the same mutants -> removable.
# test_scale_value: covers nothing test_scale_runs does not, but is the only
# test that kills scale's mutants -> not removable.
# test_add_policy: same shape as test_add, adds nothing, but marked policy.
# test_add_and_negate reads a project data file and a repo-root doc, as kew's
# tests do: a mutation work copy without them fails mutmut's stats run.
_TESTS = """\
from pathlib import Path

import pytest

from fixpkg.calc import add, negate, scale, touch

PROJECT = Path(__file__).resolve().parents[1]


def test_add_and_negate():
    assert (PROJECT / "data" / "sum.txt").read_text(encoding="utf-8") == "5\\n"
    assert (PROJECT.parent / "docs" / "calc.md").is_file()
    assert add(2, 3) == 5
    assert negate(1) == -1


def test_add():
    assert add(2, 3) == 5


def test_scale_runs():
    scale(3)
    touch()


def test_scale_value():
    assert scale(3) == 6


@pytest.mark.policy
def test_add_policy():
    assert add(1, 1) == 2
"""

_FILES = {
    "docs/calc.md": "# calc\n",
    ".gitignore": "__pycache__/\n",
    "pkg/pyproject.toml": _PYPROJECT,
    "pkg/data/sum.txt": "5\n",
    "pkg/src/fixpkg/__init__.py": "",
    "pkg/src/fixpkg/calc.py": _MODULE,
    "pkg/tests/conftest.py": _CONFTEST,
    "pkg/tests/test_calc.py": _TESTS,
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit(project: Path) -> None:
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "fixture")


def _fixture_project(root: Path) -> Path:
    """A committed repo whose project, ``pkg/``, holds a copy of the tool."""
    repo = root / "repo"
    for relpath, text in _FILES.items():
        (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
        (repo / relpath).write_text(text, encoding="utf-8")
    project = repo / "pkg"
    (project / "scripts").mkdir()
    shutil.copy(SCRIPT, project / "scripts" / SCRIPT.name)
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo)
    return project


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"test_value_report_{uuid.uuid4().hex}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _tool(project: Path) -> ModuleType:
    """The tool as kew lays it out: ``<project>/scripts/test_value_report.py``."""
    return _load(project / "scripts" / SCRIPT.name)


def _run(
    tmp_path: Path,
    *extra: str,
    caught: tuple[str, ...] = ("--no-caught-defects",),
    hour: int = 12,
    load: float = 0.5,
    free: int = 100 * GIB,
    clock: Callable[[], datetime] | None = None,
    project: Path | None = None,
    out_arg: str | None = None,
) -> tuple[int, Path]:
    project = project or _fixture_project(tmp_path / "fixture")
    out = tmp_path / "out"
    code = _tool(project).main(
        [
            "--project",
            str(project),
            "--out",
            out_arg or str(out),
            "--lease",
            str(tmp_path / "verify.lease"),
            "--queue-lock",
            str(tmp_path / "merge-queue.lock"),
            *caught,
            *extra,
        ],
        now=clock or (lambda: datetime(2026, 10, 27, hour, 30)),
        load=lambda: load,
        free_bytes=lambda path: free,
    )
    return code, out


def _report(out: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(
        (out / "report.json").read_text(encoding="utf-8")
    )
    return report


def _markdown(out: Path) -> str:
    return (out / "report.md").read_text(encoding="utf-8")


def _removable(report: dict[str, Any]) -> set[str]:
    return {entry["nodeid"] for entry in report["removable"]}


# --- a stand-in mutmut at the process boundary.

_FAKE_MUTMUT = """\
import json
import os
import sys
from pathlib import Path

here = Path(sys.argv[0])
spec = json.loads(here.with_suffix(".json").read_text(encoding="utf-8"))
log = here.with_suffix(".log")
deselect = json.loads(
    Path(os.environ["KEW_TEST_VALUE_DESELECT"]).read_text(encoding="utf-8")
)
lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
earlier = [json.loads(line) for line in lines]
call = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "deselect": deselect,
    "pyproject": Path("pyproject.toml").read_text(encoding="utf-8"),
    "present": sorted(p for p in spec.get("probe", []) if Path(p).exists()),
    "stale": sorted(
        {c["cwd"] for c in earlier if c["cwd"] != os.getcwd()}
        - {c["cwd"] for c in earlier if not Path(c["cwd"]).exists()}
    ),
}
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(call) + "\\n")
if sys.argv[1] == "run":
    sys.exit(0)
bases = [c for c in earlier if c["argv"][0] == "results" and not c["deselect"]]
if deselect:
    verdicts = dict(spec["base"][0])
    for nodeid, names in spec.get("kills", {}).items():
        if nodeid in deselect:
            verdicts.update({name: spec.get("without", "survived") for name in names})
else:
    verdicts = spec["base"][min(len(bases), len(spec["base"]) - 1)]
for name, status in verdicts.items():
    print(f"    {name}: {status}")
sys.exit(spec.get("results_exit", 0))
"""

_ADD, _SCALE, _TOUCH = (
    f"fixpkg.calc.x_{name}__mutmut_1" for name in ("add", "scale", "touch")
)
_CLEAN_BASE = {_ADD: "killed", _SCALE: "killed", _TOUCH: "survived"}


def _fake_mutmut(tmp_path: Path, base: list[dict[str, str]], **spec: Any) -> Path:
    fake = tmp_path / "fake-mutmut" / "mutmut"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n{_FAKE_MUTMUT}", encoding="utf-8")
    fake.chmod(0o755)
    fake.with_suffix(".json").write_text(
        json.dumps({"base": base, **spec}), encoding="utf-8"
    )
    return fake


def _calls(fake: Path) -> list[dict[str, Any]]:
    log = fake.with_suffix(".log")
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# --- the acceptance case, with real mutmut.


def test_redundant_test_is_removable_and_a_lone_killer_is_kept(tmp_path: Path) -> None:
    code, out = _run(tmp_path)

    assert code == 0
    report = _report(out)
    assert _removable(report) == {T + "test_add"}, report["modules"]
    lone = report["not_removable"][T + "test_scale_value"]
    assert lone.startswith("alone kills ")
    module = report["modules"][CALC]
    assert module["proven"] is True, module["note"]
    assert module["mutants"] > 0
    assert module["base_killed"] > 0
    assert set(report["modules"]) == {CALC}
    assert report["omitted_from_coverage"] == ["scripts/test_value_report.py"]
    assert report["files"]["tests/test_calc.py"]["removable"] == 1
    assert report["functions"][T + "test_add"]["items"] == [T + "test_add"]
    assert report["xdist_seconds_saved"] >= 0
    assert "1 removable" in _markdown(out)
    assert not (out / "mutmut").exists()


# --- exclusions, the cap and the caught-defect list.


def test_policy_marked_test_is_never_removable(tmp_path: Path) -> None:
    code, out = _run(tmp_path, "--cap", "0.001")

    assert code == 0
    report = _report(out)
    assert report["excluded"][T + "test_add_policy"] == "marked policy"
    assert T + "test_add_policy" not in _removable(report)
    assert T + "test_add_policy" not in report["not_removable"]


def test_a_module_over_its_mutation_cap_leaves_its_candidates_not_proven(
    tmp_path: Path,
) -> None:
    code, out = _run(tmp_path, "--cap", "0.001")

    assert code == 0
    report = _report(out)
    assert _removable(report) == set()
    assert report["modules"][CALC]["proven"] is False
    assert report["not_removable"][T + "test_add"].startswith(f"not proven: {CALC}")


def test_a_test_named_as_catching_a_defect_is_never_a_candidate(tmp_path: Path) -> None:
    caught = tmp_path / "caught.txt"
    caught.write_text(
        f"# caught a defect in the study\n{T}test_add\n", encoding="utf-8"
    )

    code, out = _run(
        tmp_path, "--cap", "0.001", caught=("--caught-defects", str(caught))
    )

    assert code == 0
    report = _report(out)
    assert report["excluded"][T + "test_add"] == "caught a real defect"
    assert report["caught_defects"] == str(caught)


@pytest.mark.parametrize(
    "listing",
    [None, "missing", "# only a comment\n\n"],
    ids=["none", "unreadable", "empty"],
)
def test_refuses_to_start_without_a_readable_caught_defect_choice(
    tmp_path: Path, listing: str | None
) -> None:
    caught: tuple[str, ...] = ()
    if listing is not None:
        path = tmp_path / "caught.txt"
        if listing != "missing":
            path.write_text(listing, encoding="utf-8")
        caught = ("--caught-defects", str(path))

    code, out = _run(tmp_path, caught=caught)

    assert code == 2
    assert not out.exists()


def test_refuses_a_caught_defect_entry_that_names_no_collected_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    caught = tmp_path / "caught.txt"
    # No ``tests/`` prefix: it matches nothing, so it would protect nothing.
    caught.write_text(
        f"{T}test_scale_value\ntest_calc.py::test_add\n", encoding="utf-8"
    )

    code, out = _run(tmp_path, caught=("--caught-defects", str(caught)))

    assert code == 2
    assert "test_calc.py::test_add" in capsys.readouterr().err
    assert not (out / "items.json").exists()
    assert not (out / "report.json").exists()


def test_a_run_without_a_caught_defect_list_says_so(tmp_path: Path) -> None:
    code, out = _run(tmp_path, "--cap", "0.001")

    assert code == 0
    assert _report(out)["caught_defects"] == "none, by --no-caught-defects"
    assert "Caught-defect list: none, by --no-caught-defects." in _markdown(out)


# --- the start guards.


@pytest.mark.parametrize(("hour", "load"), [(1, 0.5), (2, 0.5), (3, 0.5), (12, 31.0)])
def test_refuses_from_01_00_to_04_00_or_under_load(
    tmp_path: Path, hour: int, load: float
) -> None:
    code, out = _run(tmp_path, hour=hour, load=load)

    assert code == 2
    assert not out.exists()


def test_refuses_with_under_20_gib_free(tmp_path: Path) -> None:
    code, out = _run(tmp_path, free=19 * GIB)

    assert code == 2
    assert not out.exists()


def _untracked(project: Path) -> None:
    (project / "tests" / "test_extra.py").write_text(
        "def test_extra():\n    pass\n", encoding="utf-8"
    )


def _modified(project: Path) -> None:
    (project / "src" / "fixpkg" / "calc.py").write_text(
        _MODULE + "\n\nX = 1\n", encoding="utf-8"
    )


def _not_a_checkout(project: Path) -> None:
    shutil.rmtree(project.parent / ".git")


@pytest.mark.parametrize("spoil", [_untracked, _modified, _not_a_checkout])
def test_refuses_a_tree_that_is_not_one_clean_commit(
    tmp_path: Path, spoil: Callable[[Path], None]
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    spoil(project)

    code, out = _run(tmp_path, project=project)

    assert code == 2
    assert not out.exists()


def test_refuses_while_a_merge_verify_holds_the_lease(tmp_path: Path) -> None:
    with hold_verify_lease("main", path=tmp_path / "verify.lease"):
        code, out = _run(tmp_path)

    assert code == 2
    assert not (out / "report.json").exists()
    assert not (out / "items.json").exists()


def test_refuses_while_the_nightly_holds_the_merge_queue(tmp_path: Path) -> None:
    from kew.graph.driver.merge_queue import merge_queue_lock

    with merge_queue_lock(tmp_path / "merge-queue.lock") as held:
        assert held.acquired
        code, out = _run(tmp_path)

    assert code == 2
    assert not (out / "items.json").exists()
    assert verify_running(tmp_path / "verify.lease") is None


def test_a_failing_suite_writes_no_report_and_clears_an_earlier_one(
    tmp_path: Path,
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    (project / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert False\n", encoding="utf-8"
    )
    _commit(project)
    out = tmp_path / "out"
    out.mkdir()
    for name in ("report.json", "report.md", "items.json"):
        (out / name).write_text("{}", encoding="utf-8")

    code, out = _run(tmp_path, project=project)

    assert code == 1
    assert not (out / "report.json").exists()
    assert not (out / "report.md").exists()


def test_a_relative_out_dir_stays_out_of_the_measured_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    monkeypatch.chdir(tmp_path)

    code, out = _run(tmp_path, "--cap", "0.001", project=project, out_arg="out")

    assert code == 0
    assert (out / "report.json").is_file()
    assert _git(project, "status", "--porcelain") == ""


# --- what a base run must show before anything is proven.


@pytest.mark.parametrize(
    "base",
    [
        {_ADD: "segfault", _SCALE: "segfault", _TOUCH: "segfault"},
        {_ADD: "timeout", _SCALE: "timeout", _TOUCH: "timeout"},
        {_ADD: "killed", _SCALE: "suspicious", _TOUCH: "survived"},
        {_ADD: "killed", _SCALE: "not checked", _TOUCH: "survived"},
        {_ADD: "survived", _SCALE: "survived", _TOUCH: "no tests"},
    ],
    ids=["segfault", "timeout", "suspicious", "not-checked", "no-kill"],
)
def test_a_base_run_without_a_clean_verdict_proves_nothing(
    tmp_path: Path, base: dict[str, str]
) -> None:
    fake = _fake_mutmut(tmp_path, [base])

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    assert _removable(report) == set()
    assert report["modules"][CALC]["proven"] is False
    assert report["not_removable"][T + "test_add"].startswith(f"not proven: {CALC}")
    assert not [call for call in _calls(fake) if call["deselect"]]


def test_two_base_runs_that_disagree_prove_nothing(tmp_path: Path) -> None:
    flipped = {**_CLEAN_BASE, _SCALE: "survived"}
    fake = _fake_mutmut(tmp_path, [_CLEAN_BASE, flipped])

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    assert _removable(report) == set()
    module = report["modules"][CALC]
    assert module["proven"] is False
    assert module["note"].startswith("two base runs disagree on 1 mutant(s)")


def test_a_trial_that_turns_a_kill_into_a_timeout_keeps_the_test(
    tmp_path: Path,
) -> None:
    fake = _fake_mutmut(
        tmp_path,
        [_CLEAN_BASE],
        kills={T + "test_add": [_ADD], T + "test_scale_value": [_SCALE]},
        without="timeout",
    )

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    assert _removable(report) == set()
    assert report["modules"][CALC]["proven"] is True
    kept = report["not_removable"][T + "test_add"]
    assert kept.startswith(f"not removable: without it 1 mutant(s) in {CALC}")
    assert f"{_ADD} (killed -> timeout)" in kept


def test_a_failed_results_listing_proves_nothing(tmp_path: Path) -> None:
    fake = _fake_mutmut(tmp_path, [_CLEAN_BASE], results_exit=3)

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    assert _removable(report) == set()
    assert report["modules"][CALC]["note"].startswith("mutmut results exited 3")


@pytest.mark.parametrize(
    ("cap", "starts"),
    [("1800", False), ("600", True)],
    ids=["reaches-3am", "ends-first"],
)
def test_a_mutmut_run_starts_only_if_its_cap_ends_before_03_00(
    tmp_path: Path, cap: str, starts: bool
) -> None:
    fake = _fake_mutmut(tmp_path, [_CLEAN_BASE])
    times = iter([datetime(2026, 10, 27, 0, 30)])  # the start check

    code, out = _run(
        tmp_path,
        "--mutmut",
        str(fake),
        "--cap",
        cap,
        clock=lambda: next(times, datetime(2026, 10, 27, 2, 45)),
    )

    assert code == 0
    report = _report(out)
    assert bool(_calls(fake)) is starts
    if not starts:
        assert _removable(report) == set()
        assert report["modules"][CALC]["note"].startswith("not run: ")


# --- the work copy mutmut runs in.


def test_mutation_runs_on_the_whole_committed_tree_under_the_projects_settings(
    tmp_path: Path,
) -> None:
    fake = _fake_mutmut(
        tmp_path,
        [_CLEAN_BASE],
        kills={T + "test_scale_value": [_SCALE]},
        probe=["data/sum.txt", "docs/calc.md", CALC],
    )

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    assert _removable(report) == {T + "test_add"}
    assert set(report["modules"]) == {CALC}
    assert report["omitted_from_coverage"] == ["scripts/test_value_report.py"]
    calls = _calls(fake)
    assert calls
    for call in calls:
        assert call["present"] == sorted(["data/sum.txt", "docs/calc.md", CALC])
        config = tomllib.loads(call["pyproject"])["tool"]["mutmut"]
        assert config["paths_to_mutate"] == [CALC]
        assert config["tests_dir"] == ["tests/test_calc.py"]
        assert config["do_not_mutate"] == ["*/never_mutated.py"]
        assert {"src/fixpkg/", "data/", "src/", "tests/"} <= set(config["also_copy"])
        assert call["stale"] == []  # every earlier run's copy was already gone
        assert not Path(call["cwd"]).exists()
    assert not (out / "mutmut").exists()


def test_the_markdown_report_is_a_review_list_with_its_proof_and_limits(
    tmp_path: Path,
) -> None:
    fake = _fake_mutmut(
        tmp_path, [_CLEAN_BASE], kills={T + "test_scale_value": [_SCALE]}
    )

    code, out = _run(tmp_path, "--mutmut", str(fake))

    assert code == 0
    report = _report(out)
    entry = next(e for e in report["removable"] if e["nodeid"] == T + "test_add")
    assert set(entry["covered_elsewhere"]) == {"lines", "arcs"}
    assert entry["mutation_proof"] == {CALC: {"mutants": 3, "killed": 2}}
    assert report["modules"][CALC]["base_survivors"] == 1
    assert "adds no line or arc" in entry["reasons"]
    text = _markdown(out)
    lines = text.splitlines()
    assert lines[2] == (
        "**Candidates for a person to review — nothing here is deleted automatically.**"
    )
    assert "## What this report does not measure" in lines
    assert any("data-file assertions" in line for line in lines)
    assert f"| `{T}test_add` | 1 |" in text  # the per-function row
    assert f"`{CALC}` 2/3 killed" in text  # the item's proof
    assert f"| `{T}test_scale_value` | alone kills 1 mutant(s) in {CALC}" in text
    assert f"| `{T}test_add_policy` | marked policy |" in text
    assert "Base survivors" in text


# --- what coverage cannot see.

_SPAWN_TESTS = """\
import os
import shlex
import subprocess
import sys
from pathlib import Path

from fixpkg.calc import add

SRC = Path(__file__).resolve().parents[1] / "src"
CHECK = "from fixpkg.calc import scale; assert scale(3) == 6"


def test_scale_in_a_process():
    assert add(2, 3) == 5
    {spawn}
"""

_SPAWNS = {
    "subprocess": "subprocess.run([sys.executable, '-c', CHECK], "
    "env={**os.environ, 'PYTHONPATH': str(SRC)}, check=True)",
    "os-system": "assert os.system(f'PYTHONPATH={shlex.quote(str(SRC))} "
    "{shlex.quote(sys.executable)} -c {shlex.quote(CHECK)}') == 0",
}


@pytest.mark.parametrize("spawn", sorted(_SPAWNS))
def test_a_test_that_runs_code_in_another_process_is_never_removable(
    tmp_path: Path, spawn: str
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    (project / "tests" / "test_spawn.py").write_text(
        _SPAWN_TESTS.replace("{spawn}", _SPAWNS[spawn]), encoding="utf-8"
    )
    _commit(project)
    fake = _fake_mutmut(
        tmp_path, [_CLEAN_BASE], kills={T + "test_scale_value": [_SCALE]}
    )

    code, out = _run(tmp_path, "--mutmut", str(fake), project=project)

    assert code == 0
    report = _report(out)
    spawner = "tests/test_spawn.py::test_scale_in_a_process"
    assert report["not_removable"][spawner] == (
        "not proven: runs code in a process coverage cannot see"
    )
    assert _removable(report) == {T + "test_add"}
    assert any("starts any process" in line for line in _markdown(out).splitlines())


# --- the deselection a trial depends on.


@pytest.mark.parametrize(
    ("listed", "exit_code"),
    [([T + "test_add"], 0), ([T + "test_add", T + "test_gone"], 4)],
    ids=["all-collected", "one-missing"],
)
def test_a_deselection_that_misses_a_listed_test_aborts_the_run(
    tmp_path: Path, listed: list[str], exit_code: int
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    listing = tmp_path / "deselect.json"
    listing.write_text(json.dumps(listed), encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("MUTANT_UNDER_TEST", "KEW_TEST_VALUE_ITEMS")
    }
    env.update(
        PYTHONPATH=str(project / "scripts"), KEW_TEST_VALUE_DESELECT=str(listing)
    )

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "test_value_report"]
        + ["-p", "no:cacheprovider", "-q", "tests/test_calc.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == exit_code, done.stdout + done.stderr
    if exit_code:
        assert "test_gone" in done.stdout + done.stderr
    else:
        assert "1 deselected" in done.stdout


# --- interruption: no mutmut outlives the report, and the lease follows.


def _alive(pid: int) -> bool:
    """A zombie counts as gone: the kernel keeps only its exit status."""
    stat = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _until(what: str, done: Callable[[], bool], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _sleeping_mutmut(tmp_path: Path) -> tuple[Path, Path]:
    """A mutmut whose run never ends: it records its pid and its child's."""
    pids = tmp_path / "pids"
    fake = tmp_path / "sleeping-mutmut"
    fake.write_text(
        "#!/bin/sh\nsleep 300 &\n"
        f"echo $$ $! > {pids}.partial && mv {pids}.partial {pids}\nwait\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, pids


def _started(pids: Path) -> list[int]:
    _until("mutmut to start", pids.exists)
    return [int(pid) for pid in pids.read_text(encoding="utf-8").split()]


class _Interrupted(Exception):
    pass


def test_an_interrupted_mutation_run_leaves_no_mutmut_process_behind(
    tmp_path: Path,
) -> None:
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)

    def interrupt(signum: int, frame: Any) -> None:
        raise _Interrupted

    def send_once_started() -> None:
        _until("mutmut to start", pids.exists)
        os.kill(os.getpid(), signal.SIGALRM)

    previous = signal.signal(signal.SIGALRM, interrupt)
    sender = threading.Thread(target=send_once_started, daemon=True)
    sender.start()
    try:
        with pytest.raises(_Interrupted):
            mutmut.run(CALC, ["tests"], [], timeout=600)
    finally:
        sender.join(timeout=130)
        signal.signal(signal.SIGALRM, previous)

    started = _started(pids)
    assert len(started) == 2
    _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    assert not list(scratch.glob("run-*"))


def test_an_interruption_while_mutmut_is_still_starting_leaves_no_process_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interrupt lands after the fork, before ``Popen`` has returned.

    A starved parent can still be inside ``Popen`` (waiting on the exec pipe)
    when mutmut is already running: the loaded verify host hit that window.
    """
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)
    launch = subprocess.Popen._execute_child  # type: ignore[attr-defined]

    def starved(self: Any, args: Any, *rest: Any) -> Any:
        launch(self, args, *rest)
        if args and args[0] == str(fake):
            _until("mutmut to start", pids.exists)
            raise _Interrupted

    with monkeypatch.context() as patch, pytest.raises(_Interrupted):
        patch.setattr(subprocess.Popen, "_execute_child", starved)
        mutmut.run(CALC, ["tests"], [], timeout=600)

    started = _started(pids)
    assert len(started) == 2
    try:
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    except AssertionError:
        for pid in started:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        raise
    assert not list(scratch.glob("run-*"))


def test_a_stop_signal_that_lands_before_popen_stores_the_pid_still_kills_mutmut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop signal lands once the fork has returned, before ``pid`` is stored.

    ``Popen`` runs ``self.pid = _fork_exec(...)``: a C call, then a store, and
    the interpreter runs a pending Python signal handler between the two. A
    handler that raised there unwound with mutmut running and ``pid`` still
    ``None``, so nothing killed the session and the lease went back while it
    ran. ``raise_signal`` delivers the signal inside the fork's own return,
    which is that window, on any machine.
    """
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)
    fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]

    def signalled_as_it_returns(args: Any, *rest: Any) -> int:
        pid: int = fork_exec(args, *rest)
        if args and os.fsdecode(args[0]) == str(fake):
            _until("mutmut to start", pids.exists)
            signal.raise_signal(signal.SIGTERM)
        return pid

    with monkeypatch.context() as patch, pytest.raises(tool.Terminated):
        patch.setattr(subprocess, "_fork_exec", signalled_as_it_returns)
        with tool.stop_on_signals():
            mutmut.run(CALC, ["tests"], [], timeout=600)

    started = _started(pids)
    assert len(started) == 2
    try:
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    except AssertionError:
        for pid in started:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        raise
    assert not list(scratch.glob("run-*"))


def _raise_interrupted(signum: int, frame: Any) -> None:
    raise _Interrupted


@pytest.mark.parametrize(
    ("signum", "handler", "raised"),
    [
        (signal.SIGINT, signal.default_int_handler, KeyboardInterrupt),
        (signal.SIGALRM, _raise_interrupted, _Interrupted),
    ],
    ids=["ctrl-c", "a-handler-that-raises"],
)
def test_an_interruption_that_lands_before_popen_stores_the_pid_still_kills_mutmut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signum: int,
    handler: Callable[[int, Any], None],
    raised: type[BaseException],
) -> None:
    """Any signal whose handler raises, not only a stop, waits for the pid.

    A Ctrl-C, or any handler that raises, delivered inside the fork's own
    return unwinds with mutmut running and ``pid`` still ``None`` unless the
    start holds it until ``pid`` is stored, as it holds a stop signal.
    """
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)
    fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]

    def interrupted_as_it_returns(args: Any, *rest: Any) -> int:
        pid: int = fork_exec(args, *rest)
        if args and os.fsdecode(args[0]) == str(fake):
            _until("mutmut to start", pids.exists)
            signal.raise_signal(signum)
        return pid

    previous = signal.signal(signum, handler)
    try:
        with monkeypatch.context() as patch, pytest.raises(raised):
            patch.setattr(subprocess, "_fork_exec", interrupted_as_it_returns)
            mutmut.run(CALC, ["tests"], [], timeout=600)
        in_effect = signal.getsignal(signum)
    finally:
        signal.signal(signum, previous)

    started = _started(pids)
    assert len(started) == 2
    try:
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    except AssertionError:
        for pid in started:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        raise
    assert not list(scratch.glob("run-*"))
    assert in_effect is handler, "the start gave the signal its own handler back"


def test_every_interruption_held_while_mutmut_starts_reaches_its_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two signals land before ``pid`` is stored, and both handlers raise.

    The first handler's exception does not cost the second signal its
    delivery: both handlers run, in the order the signals arrived, and the
    Ctrl-C that came last is what ends the run, with the earlier exception as
    its context, as when the two land one after the other outside a start.
    """
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)
    fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]
    handled: list[int] = []

    def alarm(signum: int, frame: Any) -> None:
        handled.append(signum)
        raise _Interrupted

    def ctrl_c(signum: int, frame: Any) -> None:
        handled.append(signum)
        raise KeyboardInterrupt

    def interrupted_twice_as_it_returns(args: Any, *rest: Any) -> int:
        pid: int = fork_exec(args, *rest)
        if args and os.fsdecode(args[0]) == str(fake):
            _until("mutmut to start", pids.exists)
            signal.raise_signal(signal.SIGALRM)
            signal.raise_signal(signal.SIGINT)
        return pid

    previous = {
        signal.SIGALRM: signal.signal(signal.SIGALRM, alarm),
        signal.SIGINT: signal.signal(signal.SIGINT, ctrl_c),
    }
    either = (KeyboardInterrupt, _Interrupted)
    try:
        with monkeypatch.context() as patch, pytest.raises(either) as ending:
            patch.setattr(subprocess, "_fork_exec", interrupted_twice_as_it_returns)
            mutmut.run(CALC, ["tests"], [], timeout=600)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    started = _started(pids)
    assert len(started) == 2
    try:
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    except AssertionError:
        for pid in started:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        raise
    assert not list(scratch.glob("run-*"))
    assert handled == [signal.SIGALRM, signal.SIGINT]
    assert isinstance(ending.value, KeyboardInterrupt)
    assert isinstance(ending.value.__context__, _Interrupted)


def test_a_stop_signal_held_while_mutmut_fails_to_start_still_stops_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that lands while the start fails is the run's ending, not lost.

    The signal is held while the session starts; the start then fails as a
    missing executable or a refused fork would. The held stop must still end
    the run, carrying the start's error as its cause, rather than vanish and
    leave the operator's first SIGTERM doing nothing.
    """
    project = _fixture_project(tmp_path / "fixture")
    tool = _tool(project)
    fake, pids = _sleeping_mutmut(tmp_path)
    scratch = tmp_path / "scratch"
    mutmut = tool.Mutmut(project, scratch, tool.head_sha(project), executable=fake)
    fork_exec = subprocess._fork_exec  # type: ignore[attr-defined]

    def signalled_then_refused(args: Any, *rest: Any) -> int:
        if args and os.fsdecode(args[0]) == str(fake):
            signal.raise_signal(signal.SIGTERM)
            raise OSError(11, "fork refused")
        pid: int = fork_exec(args, *rest)
        return pid

    with monkeypatch.context() as patch, pytest.raises(tool.Terminated) as stopped:
        patch.setattr(subprocess, "_fork_exec", signalled_then_refused)
        with tool.stop_on_signals():
            mutmut.run(CALC, ["tests"], [], timeout=600)

    assert stopped.value.signum == signal.SIGTERM
    assert isinstance(stopped.value.__cause__, OSError)
    assert not pids.exists(), "no mutmut process was ever started"
    assert not list(scratch.glob("run-*"))

    # The spent stop is gone: the next run in this process is not stopped by it.
    later = tool.Mutmut(
        project,
        tmp_path / "later",
        tool.head_sha(project),
        executable=_fake_mutmut(tmp_path, [_CLEAN_BASE]),
    )
    with tool.stop_on_signals():
        assert later.run(CALC, ["tests"], [], timeout=600).statuses is not None


_DRIVER = """\
import importlib.util
import sys
from datetime import datetime

spec = importlib.util.spec_from_file_location("test_value_report", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
sys.exit(
    module.main(
        sys.argv[2:],
        now=lambda: datetime(2026, 10, 27, 12, 30),
        load=lambda: 0.5,
        free_bytes=lambda path: 1 << 40,
    )
)
"""


def _start_report(
    tmp_path: Path, fake: Path, prelude: str = ""
) -> tuple[subprocess.Popen[str], Path]:
    """The tool as its own process, fixed only at the clock, load and disk.

    *prelude* runs in that process first, before the tool is loaded.
    """
    project = _fixture_project(tmp_path / "fixture")
    driver = tmp_path / "driver.py"
    driver.write_text(prelude + _DRIVER, encoding="utf-8")
    lease = tmp_path / "verify.lease"
    process = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(project / "scripts" / SCRIPT.name),
            "--project",
            str(project),
            "--out",
            str(tmp_path / "out"),
            "--lease",
            str(lease),
            "--queue-lock",
            str(tmp_path / "merge-queue.lock"),
            "--mutmut",
            str(fake),
            "--no-caught-defects",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return process, lease


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_kills_mutmut_and_gives_the_lease_back(
    tmp_path: Path, signum: int
) -> None:
    fake, pids = _sleeping_mutmut(tmp_path)
    process, lease = _start_report(tmp_path, fake)
    try:
        started = _started(pids)
        assert verify_running(lease) is not None

        process.send_signal(signum)

        assert process.wait(timeout=120) == 128 + signum
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
        assert verify_running(lease) is None
        assert not (tmp_path / "out" / "mutmut").exists()
    finally:
        process.kill()
        process.wait()


#: Two stop signals once the report's ``Popen`` has stored mutmut's pid and
#: before it returns: the first is held, the second takes the hard exit.
_TWO_STOPS_WHILE_STARTING = """\
import signal
import subprocess
import time
from pathlib import Path

_close_pipe_fds = subprocess.Popen._close_pipe_fds


def _stopped_twice_as_it_starts(self, *fds):
    _close_pipe_fds(self, *fds)
    if self.args[0] == {fake!r}:
        while not Path({pids!r}).exists():
            time.sleep(0.05)
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)


subprocess.Popen._close_pipe_fds = _stopped_twice_as_it_starts
"""


def test_a_second_stop_while_mutmut_is_still_starting_kills_it_on_the_hard_exit(
    tmp_path: Path,
) -> None:
    """The hard exit kills a session still inside ``Popen`` once its pid is known.

    The first stop is held until ``Popen`` returns; a second one exits at once.
    That exit kills the sessions it knows of, and a session still being started
    has to be one of them, or mutmut outlives the report.
    """
    fake, pids = _sleeping_mutmut(tmp_path)
    prelude = _TWO_STOPS_WHILE_STARTING.format(fake=str(fake), pids=str(pids))
    process, _lease = _start_report(tmp_path, fake, prelude)
    started: list[int] = []
    try:
        assert process.wait(timeout=120) == 128 + signal.SIGTERM
        started = _started(pids)
        _until("mutmut to die", lambda: not any(map(_alive, started)), timeout=30)
    finally:
        process.kill()
        process.wait()
        for pid in started:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def test_a_killed_report_leaves_the_lease_held_while_its_mutmut_lives(
    tmp_path: Path,
) -> None:
    fake, pids = _sleeping_mutmut(tmp_path)
    process, lease = _start_report(tmp_path, fake)
    started: list[int] = []
    try:
        started = _started(pids)

        process.kill()
        process.wait(timeout=120)

        assert verify_running(lease) is not None
        os.killpg(started[0], signal.SIGKILL)
        _until("the lease to be free", lambda: verify_running(lease) is None)
    finally:
        process.kill()
        process.wait()
        if started:
            with contextlib.suppress(OSError):  # macOS: EPERM once the group is gone
                os.killpg(started[0], signal.SIGKILL)


# --- a descendant that leaves mutmut's session.

_ESCAPING_MUTMUT = """\
import subprocess
import sys
import time
from pathlib import Path

here = Path(sys.argv[0])
child = subprocess.Popen(
    ["sleep", "150"], start_new_session=True, stdout=sys.stdout, stderr=sys.stderr
)
partial = here.with_suffix(".partial")
partial.write_text(str(child.pid), encoding="utf-8")
partial.rename(here.with_suffix(".pid"))
time.sleep(600)
"""


def _escaping_mutmut(tmp_path: Path) -> Path:
    """A mutmut that never ends and leaves a child in a session of its own."""
    fake = tmp_path / "escaping" / "mutmut"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\n{_ESCAPING_MUTMUT}", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _kill_escapee(fake: Path) -> None:
    pid = fake.with_suffix(".pid")
    if pid.exists():
        with contextlib.suppress(OSError):
            os.kill(_read_pid(pid), signal.SIGKILL)


def _read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8"))


def test_an_escaped_descendant_cannot_hold_a_mutmut_run_past_its_cap(
    tmp_path: Path,
) -> None:
    fake = _escaping_mutmut(tmp_path)
    started = time.monotonic()
    try:
        code, out = _run(tmp_path, "--mutmut", str(fake), "--cap", "20")
        elapsed = time.monotonic() - started
        assert fake.with_suffix(".pid").exists()  # the escapee really started
    finally:
        _kill_escapee(fake)

    assert elapsed < 120  # the escapee lives 150 s
    assert code == 0
    report = _report(out)
    assert report["modules"][CALC]["proven"] is False
    assert report["modules"][CALC]["note"] == "over its cap"
    assert _removable(report) == set()
    assert verify_running(tmp_path / "verify.lease") is None


def test_a_stop_signal_is_prompt_while_an_escaped_descendant_lives(
    tmp_path: Path,
) -> None:
    fake = _escaping_mutmut(tmp_path)
    process, lease = _start_report(tmp_path, fake)
    try:
        _until("the escapee to start", fake.with_suffix(".pid").exists)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=60) == 128 + signal.SIGTERM
        assert verify_running(lease) is None
    finally:
        process.kill()
        process.wait()
        _kill_escapee(fake)


_STALLED_UNWIND = """\
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("test_value_report", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
marker = Path(sys.argv[2])
with module.stop_on_signals():
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    with module.tracked_group(sleeper.pid):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(60)
        finally:
            partial = marker.with_suffix(".partial")
            partial.write_text(str(sleeper.pid), encoding="utf-8")
            partial.rename(marker)
            # An unwind that stalls for 120 s. Short sleeps, not one long one:
            # a signal landing just before a sleep's syscall waits for it to end.
            for _ in range(1200):
                time.sleep(0.1)
"""


def test_a_second_stop_signal_during_a_stalled_unwind_exits_at_once(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "stalled.py"
    driver.write_text(_STALLED_UNWIND, encoding="utf-8")
    marker = tmp_path / "sleeper.pid"
    process = subprocess.Popen(
        [sys.executable, str(driver), str(SCRIPT), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sleeper = 0
    try:
        _until("the unwind to stall", marker.exists)
        sleeper = _read_pid(marker)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=60) == 128 + signal.SIGTERM
        _until("the tracked session to die", lambda: not _alive(sleeper), timeout=30)
    finally:
        process.kill()
        process.wait()
        if sleeper:
            with contextlib.suppress(OSError):
                os.kill(sleeper, signal.SIGKILL)
