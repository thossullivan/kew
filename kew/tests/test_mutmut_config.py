"""The weekly mutmut job reads mutmut 3 config and counts mutmut 3 survivors (#2479).

mutmut 3 iterates ``paths_to_mutate``/``tests_dir`` as lists, so a mutmut-2
colon-joined string is read one character at a time; and it prints lowercase
``survived``, so a grep for ``Survived`` counted 0 on every run.
"""

from __future__ import annotations

import importlib.util
import math
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.policy

KEW = Path(__file__).resolve().parents[1]
SAMPLER = KEW / "scripts" / "mutmut-sampler.py"
THIS_FILE = Path(__file__).resolve().relative_to(KEW)


def _mutmut_config() -> dict:
    """The ``[tool.mutmut]`` table of kew's pyproject.toml (config, not source)."""
    return tomllib.loads((KEW / "pyproject.toml").read_text())["tool"]["mutmut"]


WORKFLOW = KEW.parent / ".github" / "workflows" / "scheduled-audits.yml"

# Captured `mutmut results` output from mutmut 3.5 (format: `    <mutant>: <status>`).
RESULTS_TWO_SURVIVORS = """\
    kew.breaker.x_record_failure__mutmut_1: survived
    kew.breaker.x_record_failure__mutmut_4: timeout
    kew.breaker.x_is_open__mutmut_2: no tests
    kew.breaker.x_is_open__mutmut_7: survived
    kew.breaker.x_reset__mutmut_3: suspicious
"""
RESULTS_NO_SURVIVORS = """\
    kew.breaker.x_record_failure__mutmut_4: timeout
    kew.breaker.x_is_open__mutmut_2: no tests
    kew.breaker.x_survived__mutmut_9: not checked
"""


def _sampler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutmut_sampler", SAMPLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _count_via_cli(results: str) -> int:
    out = subprocess.run(
        [sys.executable, str(SAMPLER), "--count-survivors"],
        input=results,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def test_mutmut_config_is_lists_of_existing_paths() -> None:
    config = _mutmut_config()
    for key in ("paths_to_mutate", "tests_dir"):
        value = config[key]
        assert isinstance(value, list) and value, f"{key} must be a non-empty TOML list"
        for path in value:
            assert isinstance(path, str)
            assert (KEW / path).exists(), f"{key} entry {path!r} does not exist"


def test_mutmut_runs_only_the_suites_of_the_mutated_modules() -> None:
    """mutmut 3 runs ``tests_dir`` from kew/mutants/, where scripts/ and .github/
    are absent; one failing test there aborts stats collection, so the whole
    tests/ tree (this module included) must not be selected.
    """
    config = _mutmut_config()
    selected = [Path(p) for p in config["tests_dir"]]
    assert all((KEW / p).is_file() for p in selected), "tests_dir lists files"
    assert THIS_FILE not in selected
    for path in config["paths_to_mutate"]:
        suite = Path("tests") / f"test_{Path(path).stem}.py"
        assert suite in selected, f"tests_dir must run {suite} for {path}"


def test_workflow_links_the_repo_root_entries_into_the_mutmut_root() -> None:
    """From kew/mutants/, parents[2] of a test and parents[3] of sandbox.py are
    kew/, so the repo-root files they read must be linked there before the run.
    """
    workflow = WORKFLOW.read_text()
    link = workflow.index("Link repo-root files into the mutmut root")
    assert link < workflow.index("uv run mutmut run")
    entries = workflow[link:].split("for entry in ", 1)[1].split(";", 1)[0].split()
    for entry in (".claude", "docker", "docs", "kew.toml"):
        assert entry in entries
    for entry in entries:
        assert (KEW.parent / entry).exists(), f"{entry} is not at the repo root"
        assert not (KEW / entry).exists(), f"kew/{entry} exists; ln would fail"


def test_mutmut_copies_the_package_of_every_mutated_file() -> None:
    """mutmut 3 copies only the mutated files; the rest of the package must follow.

    Without it ``mutants/src/kew`` has no ``__init__.py``, ``import kew`` falls
    through to the checkout, and no test ever runs a mutant.
    """
    config = _mutmut_config()
    copied = [Path(p) for p in config.get("also_copy", [])]
    for path in config["paths_to_mutate"]:
        package = Path(path).parent
        assert (KEW / package / "__init__.py").exists()
        assert package in copied, f"also_copy must include {package}/"


@pytest.mark.parametrize(
    ("results", "expected"),
    [(RESULTS_TWO_SURVIVORS, 2), (RESULTS_NO_SURVIVORS, 0), ("", 0)],
)
def test_survivor_counter_reads_mutmut3_results(results: str, expected: int) -> None:
    assert _sampler().count_survivors(results) == expected
    assert _count_via_cli(results) == expected


def test_workflow_counts_survivors_with_the_sampler() -> None:
    workflow = WORKFLOW.read_text()
    assert "scripts/mutmut-sampler.py --count-survivors" in workflow
    assert 'grep -c "Survived"' not in workflow


def test_sampler_rewrites_the_list_to_a_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler = _sampler()
    original = (KEW / "pyproject.toml").read_text()
    (tmp_path / "pyproject.toml").write_text(original)
    monkeypatch.chdir(tmp_path)
    sampler.main()

    all_paths = sampler.mutmut_paths(original)
    sampled = sampler.mutmut_paths((tmp_path / "pyproject.toml").read_text())
    assert len(sampled) == max(
        sampler.MIN_PATHS, math.ceil(len(all_paths) * sampler.SAMPLE_RATIO)
    )
    assert set(sampled) <= set(all_paths)


def test_sampler_refuses_a_colon_joined_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\npaths_to_mutate = "src/a.py:src/b.py"\n'
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="must be a TOML list"):
        _sampler().main()
