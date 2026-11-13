"""Held-out and contract runs load only kew's plugins and read kew's report (#3194).

A candidate's environment can register a ``pytest11`` entry point, and pytest
resolves ``-p NAME`` through any distribution's entry point of that name; its
config can ``-p`` a module of its own, and its src can shadow a module the
run imports by name. A plugin can rewrite a failure as a pass. And the job's
log is text the candidate's own code prints into first.

So kew's report plugin runs the held-out runs: loaded first, from a file kew
writes outside the checkout under a random name, it refuses a run that loaded
any plugin, hook or pinned package that is not a file of the distribution the
BASE's ``uv.lock`` pins. Each verdict reaches the log tagged with a nonce the
phase draws after the main phase ends, and kew reads only that section.

Each case is a tiny git repository verified end to end on the local host,
where a head whose ``add`` subtracts must not pass. The plugins are real
distributions — a ``dist-info`` with an ``entry_points.txt`` — installed in a
virtualenv of their own that also sees this interpreter's packages, which is
what ``uv sync`` of a candidate that depends on one builds.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from support.fake_gcloud import launchable
from support.held_out_lock import pinned_lock
from support.held_out_log import NONCE, opened, report_line

from kew import merge_verify
from kew.config import DispatchConfig, VerifyConfig
from kew.hosts import JobResult, provider_pool
from kew.merge_verify import (
    VerifyCandidate,
    VerifyResult,
    build_job,
    held_out_check_candidate,
    held_out_job_script,
    held_out_report,
    held_out_section,
    job_script,
    resolve_candidate,
    run_verify,
    write_bundle,
)
from kew.pinned_pytest_plugins import pins
from kew.run_store_acceptance import AcceptanceContractRow

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

CONTRACT_PATH = "kew/tests/acceptance/test_issue_7.py"
CONTRACT = "tests/acceptance/test_issue_7.py"
CONTRACT_TEST = f"{CONTRACT}::test_ac1"
BASE_TEST = "tests/test_calc.py::test_add"

_CALC = "def add(a, b):\n    return a + b\n"
_BROKEN_CALC = "def add(a, b):\n    return a - b\n"
_BASE_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
_CONTRACT = "from calc import add\n\n\ndef test_ac1():\n    assert add(2, 3) == 5\n"
_PYPROJECT = '[tool.pytest.ini_options]\npythonpath = ["src"]\n'


_LOCK = pinned_lock()

#: A lock that pins pytest but no pluggy.
_NO_PLUGGY = _LOCK.replace('name = "pluggy"', 'name = "not-pluggy"')

#: A plugin that reports every failing test as passed.
_REWRITES_FAILURES = (
    "import pytest\n\n\n"
    "@pytest.hookimpl(wrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    report = yield\n"
    "    report.outcome = 'passed'\n"
    "    return report\n"
)

#: A plugin that files each failure under ``passed``, so no ``FAILED`` line
#: names it — while the run still exits 1.
_HIDES_FAILURE_LINES = (
    "def pytest_report_teststatus(report, config):\n"
    "    if report.failed:\n"
    "        return 'passed', '.', 'PASSED'\n"
)

#: The same, plus every failure rewritten as a pass except in the contract
#: run (the one given ``--confcutdir=tests``): the base runs come back green,
#: and only the contract run exits 1, with no line naming what failed.
_HIDES_THE_CONTRACT_FAILURE = _HIDES_FAILURE_LINES + (
    "\n\nimport pytest\n\n\n"
    "@pytest.hookimpl(wrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    report = yield\n"
    "    if '--confcutdir=tests' not in item.config.invocation_params.args:\n"
    "        report.outcome = 'passed'\n"
    "    return report\n"
)

#: What the independent review reproduced: a plugin answering to the pinned
#: name ``asyncio`` that renames itself past a module-name check, plants a
#: module under the name kew's plugin is loaded by, tries to write kew's
#: report itself, and rewrites every failure as a pass.
_IMPERSONATES_ASYNCIO = (
    "import json, os, sys, types\n"
    "import pytest\n\n"
    "__name__ = 'pytest_asyncio.plugin'\n"
    "_name = os.environ.get('PYTEST_PLUGINS', 'kew_held_out_report')\n"
    "sys.modules[_name] = types.ModuleType(_name)\n"
    "_path = os.environ.get('KEW_HELD_OUT_REPORT')\n\n\n"
    "@pytest.hookimpl(wrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    report = yield\n"
    "    report.outcome = 'passed'\n"
    "    return report\n\n\n"
    "def pytest_unconfigure(config):\n"
    "    if _path:\n"
    "        with open(_path, 'w') as handle:\n"
    "            json.dump({'failed': [], 'passed': [], 'refused': None,\n"
    "                       'exitstatus': 0}, handle)\n"
)


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "--allow-empty", "-m", "commit"], repo)
    return _run(["git", "rev-parse", "HEAD"], repo)


def _repo(
    tmp_path: Path,
    head: dict[str, str],
    *,
    lock: str = _LOCK,
    contract_text: str = _CONTRACT,
    config: tuple[str, str] = ("kew/pyproject.toml", _PYPROJECT),
) -> tuple[Path, AcceptanceContractRow]:
    """A base on ``main``, a contract commit, then *head*; and the contract row.

    *config* is the base's pytest configuration file and its text.
    """
    repo = tmp_path / "project"
    repo.mkdir()
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    _commit(
        repo,
        {
            config[0]: config[1],
            "kew/uv.lock": lock,
            "kew/src/calc.py": _CALC,
            "kew/tests/test_calc.py": _BASE_TEST,
            "kew/scripts/verify.sh": "#!/bin/sh\necho GATE RAN\n",
            "scripts/scan-agent-image.sh": "#!/bin/sh\necho PREFLIGHT RAN\n",
        },
    )
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    commit = _commit(repo, {CONTRACT_PATH: contract_text})
    _commit(repo, head)
    row = AcceptanceContractRow(
        id=1,
        epic_ref="epic/1",
        item_id="7",
        version=1,
        path=CONTRACT_PATH,
        sha256=hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
        author_run_id="run-author",
        commit=commit,
        created_at=datetime.now(UTC),
    )
    return repo, row


def _environment(tmp_path: Path, plugins: dict[str, tuple[str, str]]) -> Path:
    """A virtualenv with *plugins* installed: ``{dist: (entry name, source)}``.

    It sees this interpreter's packages too (pytest, xdist, …) through a
    ``.pth`` file, after its own, as a dependency of the candidate would be.
    Returns its python.
    """
    venv = tmp_path / "candidate-env"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = venv / "bin" / "python"
    purelib = _run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        tmp_path,
    )
    site = Path(purelib)
    (site / "kew_host_packages.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8"
    )
    for dist, (entry, source) in plugins.items():
        module = dist.replace("-", "_")
        (site / f"{module}.py").write_text(source, encoding="utf-8")
        info = site / f"{module}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n", encoding="utf-8"
        )
        (info / "entry_points.txt").write_text(
            f"[pytest11]\n{entry} = {module}\n", encoding="utf-8"
        )
    return python


@pytest.fixture(autouse=True)
def _local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(merge_verify, "HELD_OUT_PARALLEL", '-m "not serial"')
    _runner(monkeypatch, Path(sys.executable))


def _runner(monkeypatch: pytest.MonkeyPatch, python: Path) -> None:
    monkeypatch.setattr(merge_verify, "HELD_OUT_RUNNER", str(python))


def _verify(
    repo: Path, contract: AcceptanceContractRow | None, pr_body: str | None = None
) -> VerifyResult:
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    _candidate, result = run_verify(
        config, store=None, repo_root=repo, pr_body=pr_body, contract=contract
    )
    return result


def _refused(result: VerifyResult) -> str:
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert "was refused" in (result.held_out.error or ""), result.held_out
    return result.held_out.error or ""


def test_the_fixture_plugin_does_rewrite_failures_when_autoloaded(
    tmp_path: Path,
) -> None:
    """The control: the environment's plugin is real and does what it says."""
    python = _environment(
        tmp_path, {"rewrite-failures": ("rewrite_failures", _REWRITES_FAILURES)}
    )
    project = tmp_path / "plain"
    project.mkdir()
    (project / "test_red.py").write_text("def test_red():\n    assert 0\n")
    completed = subprocess.run(
        [str(python), "-m", "pytest", "-p", "no:cacheprovider", "test_red.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout


def test_an_honest_head_still_passes_with_autoload_off(tmp_path: Path) -> None:
    """The control for every forgery below: the same repository, honest."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"})
    result = _verify(repo, contract)
    assert result.outcome == "passed", result.log
    assert result.held_out is not None and result.held_out.outcome == "passed"


def test_a_broken_head_fails_the_held_out_phase(tmp_path: Path) -> None:
    """The other control: with nothing forged, the broken ``add`` is caught."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (CONTRACT_TEST,)


def test_an_entry_point_that_rewrites_failures_does_not_pass_the_held_out_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runner(
        monkeypatch,
        _environment(
            tmp_path, {"rewrite-failures": ("rewrite_failures", _REWRITES_FAILURES)}
        ),
    )
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (CONTRACT_TEST,)


def test_an_entry_point_that_hides_the_contract_failure_fails_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runner(
        monkeypatch,
        _environment(
            tmp_path, {"hide-lines": ("hide_lines", _HIDES_THE_CONTRACT_FAILURE)}
        ),
    )
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert CONTRACT_TEST in result.held_out.undeclared


def test_a_plugin_answering_to_a_pinned_name_is_refused_whatever_it_calls_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-p asyncio`` loads whatever entry point is named ``asyncio``. One that
    renames itself ``pytest_asyncio.plugin``, plants a module under kew's
    plugin's name and writes a passing report of its own still refuses the
    run: its file is no file of pytest-asyncio, and kew's report is kew's."""
    _runner(
        monkeypatch,
        _environment(tmp_path, {"not-asyncio": ("asyncio", _IMPERSONATES_ASYNCIO)}),
    )
    lock = pinned_lock("pytest-asyncio")
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC}, lock=lock)
    error = _refused(_verify(repo, contract))
    assert "not_asyncio.py" in error
    assert "entry point asyncio = not_asyncio" in error


def test_the_same_pinned_name_from_the_real_distribution_passes(
    tmp_path: Path,
) -> None:
    """The control: ``-p asyncio`` answered by pytest-asyncio itself."""
    lock = pinned_lock("pytest-asyncio")
    repo, contract = _repo(
        tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"}, lock=lock
    )
    result = _verify(repo, contract)
    assert result.outcome == "passed", result.log


def test_a_head_shipping_a_module_under_the_report_plugins_old_name_is_not_read(
    tmp_path: Path,
) -> None:
    """The head's ``src`` comes first on ``sys.path``; a module there named as
    kew's plugin once was, writing a passing report, is never what runs."""
    shadow = (
        "import json, os\n"
        "_path = os.environ.get('KEW_HELD_OUT_REPORT')\n\n\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if _path:\n"
        "        with open(_path, 'w') as handle:\n"
        "            json.dump({'failed': [], 'passed': ['" + CONTRACT_TEST + "'],\n"
        "                       'refused': None, 'exitstatus': 0}, handle)\n"
        "        os._exit(0)\n"
    )
    head = {
        "kew/src/calc.py": _BROKEN_CALC,
        "kew/src/kew_held_out_report.py": shadow,
    }
    repo, contract = _repo(tmp_path, head)
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (CONTRACT_TEST,)


def test_a_plugin_the_heads_addopts_name_is_never_loaded(tmp_path: Path) -> None:
    """The runs take the base's configuration, so the head's ``addopts`` — and
    the ``-p`` of a module in its src — are never read."""
    head = {
        "kew/pyproject.toml": f'{_PYPROJECT}addopts = "-p evil"\n',
        "kew/src/evil.py": _REWRITES_FAILURES,
        "kew/src/calc.py": _BROKEN_CALC,
    }
    repo, contract = _repo(tmp_path, head)
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (CONTRACT_TEST, BASE_TEST)
    assert "evil" not in (result.held_out.error or "")


#: A head's configuration that narrows what the base runs collect, each in the
#: file pytest would read before the base's.
_NARROWING = {
    "deselect": {
        "kew/pyproject.toml": f'{_PYPROJECT}addopts = "--deselect {BASE_TEST}"\n'
    },
    "python-files": {
        "kew/pyproject.toml": f'{_PYPROJECT}python_files = "nothing_*.py"\n'
    },
    "keyword": {"kew/pyproject.toml": f'{_PYPROJECT}addopts = "-k not_add"\n'},
    "testpaths": {"kew/pyproject.toml": f'{_PYPROJECT}testpaths = ["src"]\n'},
    "pytest-ini": {"kew/pytest.ini": f"[pytest]\naddopts = --deselect {BASE_TEST}\n"},
}


@pytest.mark.parametrize("config", list(_NARROWING.values()), ids=list(_NARROWING))
def test_a_head_config_that_narrows_the_base_runs_is_not_read(
    tmp_path: Path, config: dict[str, str]
) -> None:
    """Without a contract, a head that kept the base test from running would
    pass with a broken ``add``: the runs take the base's configuration."""
    repo, _contract = _repo(tmp_path, {**config, "kew/src/calc.py": _BROKEN_CALC})
    result = _verify(repo, None)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (BASE_TEST,)


def test_an_honest_head_that_edits_its_pytest_config_still_passes(
    tmp_path: Path,
) -> None:
    """The control: a config change runs the whole base tree, which passes."""
    head = {"kew/pyproject.toml": f"{_PYPROJECT}xfail_strict = true\n"}
    repo, _contract = _repo(tmp_path, head)
    result = _verify(repo, None)
    assert result.outcome == "passed", result.log
    assert result.held_out is not None and result.held_out.outcome == "passed"


_SKIPS_ITS_IMPORTERS = (
    "import pytest\n\npytest.skip('not today', allow_module_level=True)\n"
)


def test_a_head_that_makes_the_base_tests_uncollectable_is_not_passed(
    tmp_path: Path,
) -> None:
    """Its src skips every module that imports it, so the run collects
    nothing and exits 5 — where the base, on its own src, collects the test."""
    head = {
        # A change to the pytest config, so the whole base tree is held out.
        "kew/pyproject.toml": f"{_PYPROJECT}xfail_strict = true\n",
        "kew/src/calc.py": _SKIPS_ITS_IMPORTERS,
    }
    repo, _contract = _repo(tmp_path, head)
    result = _verify(repo, None)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (BASE_TEST,)
    assert result.held_out.error == (
        "base tests failed against the candidate's src and are not declared under "
        f"Behavior changes in the PR body: {BASE_TEST} (1 of them the base collects "
        f"for the run and the candidate's run never collected: {BASE_TEST})"
    )


def test_a_head_conftest_that_ignores_the_base_tests_is_not_passed(
    tmp_path: Path,
) -> None:
    head = {
        "kew/conftest.py": "collect_ignore_glob = ['tests/*']\n",
        "kew/src/calc.py": _BROKEN_CALC,
    }
    repo, contract = _repo(tmp_path, head)
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert CONTRACT_TEST in result.held_out.undeclared


def test_held_out_config_reads_the_bases_own_file(tmp_path: Path) -> None:
    """The base's ``pyproject.toml`` holds its pytest config; the head's
    ``pytest.ini``, which pytest would read first, is not the base's."""
    repo, _contract = _repo(tmp_path, {"kew/pytest.ini": "[pytest]\naddopts = -x\n"})
    candidate = resolve_candidate(repo)
    assert candidate.held_out_config.name == ".kew-held-out-base.toml"
    assert candidate.held_out_config.text == f"{_PYPROJECT}"
    assert "-c .kew-held-out-base.toml --rootdir=." in job_script(candidate, images=[])


def test_the_held_out_python_is_the_bases_request_never_the_heads(
    tmp_path: Path,
) -> None:
    """uv runs what a ``.python-version`` names; the head's is never read, and
    the runs are given the interpreter kew found for the base's request."""
    base_project = '[project]\nname = "calc"\nrequires-python = ">=3.13"\n'
    repo, _contract = _repo(
        tmp_path,
        {"kew/.python-version": "./evil\n", "kew/pyproject.toml": base_project},
    )
    candidate = resolve_candidate(repo)
    assert candidate.held_out_python == "3"
    # The base's request is what the pre-review check, which syncs no checkout
    # environment, asks for, and what the gate job falls back to.
    for script in (job_script(candidate, images=[]), held_out_job_script(candidate)):
        assert "    held_out_request=3\n" in script
        assert "./evil" not in script


#: The home directory this session started with: the autouse fixture above
#: points ``HOME`` into the noexec-able ``tmp_path`` for every test.
_SESSION_HOME = Path.home()


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory first on PATH for stub tools.

    Under ``HOME`` when the pytest temporary filesystem is mounted ``noexec``,
    as the provisioning tests' stub host is: the assertion is about the job,
    not about the sandbox's mount options.
    """
    fallback: str | None = None
    if launchable(tmp_path):
        root = tmp_path / "stub"
    else:
        fallback = tempfile.mkdtemp(prefix="kew-stub-uv-", dir=_SESSION_HOME)
        root = Path(fallback)
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    try:
        yield bin_dir
    finally:
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


def _logging_uv(
    bin_dir: Path, *, find_fails: bool = False, after_sync: str = ""
) -> Path:
    """A ``uv`` in *bin_dir* that logs each call, then runs the real one.

    After a ``uv sync`` it logs the synced ``.venv``'s interpreter version and
    runs the shell in *after_sync* in the synced project, and after a
    ``uv python find`` it logs the path it found — or, with *find_fails*, it
    answers that request as a host with no such Python does. Returns the log.
    """
    real = shutil.which("uv")
    assert real is not None
    log = bin_dir.parent / "uv.log"
    fail = 'echo "error: No interpreter found" >&2; exit 2' if find_fails else ""
    wrapper = bin_dir / "uv"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "call %s\\n" "$*" >> "{log}"\n'
        'if [ "$1 $2" = "python find" ]; then\n'
        f"    {fail}\n"
        f'    found="$("{real}" "$@")" || exit $?\n'
        f'    printf "found %s\\n" "$found" >> "{log}"\n'
        '    printf "%s\\n" "$found"\n'
        "    exit 0\n"
        "fi\n"
        f'"{real}" "$@" || exit $?\n'
        'if [ "$1" = sync ]; then\n'
        "    .venv/bin/python -c "
        "\"import platform; print('synced', platform.python_version())\" "
        f'>> "{log}"\n'
        f"{after_sync}\n"
        "fi\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    # The held-out runs, shaped as ``HELD_OUT_RUNNER`` is: logged with the
    # version of the interpreter they were given, then run by this test's own
    # interpreter, which has pytest whatever Python the fixture's ``uv sync``
    # picked on this machine. What the job hands each run is what is pinned.
    runner = bin_dir / "held-out-run"
    runner.write_text(
        "#!/bin/sh\n"
        f'printf "held-out %s\\n" "$*" >> "{log}"\n'
        '[ "$1" = --python ] || exit 99\n'
        '"$2" -I -S -c "import platform; print(\'given\', '
        f'platform.python_version())" >> "{log}" || exit 98\n'
        "shift 3\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    runner.chmod(0o755)
    return log


def _uv_log(log: Path) -> str:
    return log.read_text(encoding="utf-8")


def _uv_calls(log: Path, prefix: str) -> list[str]:
    lines = _uv_log(log).splitlines()
    return [line[len(prefix) :] for line in lines if line.startswith(prefix)]


def test_held_out_runs_ask_for_the_main_phases_exact_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin: Path
) -> None:
    """Every held-out run, the base's collection of each and the contract run
    are given the interpreter ``==X.Y.Z`` finds, X.Y.Z being the version the
    checkout's ``uv sync`` built on — not whatever the base's range admits."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"})
    log = _logging_uv(stub_bin)
    monkeypatch.setattr(
        merge_verify,
        "HELD_OUT_RUNNER",
        'held-out-run --python "$held_out_python" python',
    )
    result = _verify(repo, contract)
    assert result.outcome == "passed", result.log
    synced = {line.split()[-1] for line in _uv_calls(log, "synced ")}
    assert len(synced) == 1, _uv_log(log)
    main_version = synced.pop()
    requests = [call.split()[-1] for call in _uv_calls(log, "call python find ")]
    assert requests == [f"=={main_version}"]
    found = _uv_calls(log, "found ")
    assert len(found) == 1, _uv_log(log)
    held_out = _uv_calls(log, "held-out ")
    # Two runs and the contract run, each after the base's own collection.
    assert len(held_out) == 6, held_out
    assert all(call.startswith(f"--python {found[0]} ") for call in held_out)
    # Each interpreter a run was handed is the main phase's version.
    assert _uv_calls(log, "given ") == [main_version] * 6, _uv_log(log)


def test_a_missing_main_phase_interpreter_is_reported_as_a_host_problem(
    tmp_path: Path, stub_bin: Path
) -> None:
    """A host with no Python for the request never judged the candidate: the
    verification says the host's environment is broken, not that a held-out
    run wrote no report."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"})
    _logging_uv(stub_bin, find_fails=True)
    result = _verify(repo, contract)
    assert result.error_category == merge_verify.ENVIRONMENT_BROKEN, result.log
    assert "no Python on this host answers the held-out request ==" in (
        result.error or ""
    )
    assert not merge_verify.gate_ran(result)
    assert "wrote no report" not in (result.held_out and result.held_out.error or "")


def _given_held_out_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand every held-out run to the logging ``held-out-run`` stub."""
    monkeypatch.setattr(
        merge_verify,
        "HELD_OUT_RUNNER",
        'held-out-run --python "$held_out_python" python',
    )


def _ran_on_the_found_interpreter(log: Path, runs: int) -> None:
    """*runs* held-out runs, each given the one interpreter uv found."""
    found = _uv_calls(log, "found ")
    assert len(found) == 1, _uv_log(log)
    held_out = _uv_calls(log, "held-out ")
    assert len(held_out) == runs, held_out
    assert all(call.startswith(f"--python {found[0]} ") for call in held_out)


def test_a_project_with_nothing_to_sync_asks_for_the_bases_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin: Path
) -> None:
    """With no ``kew/pyproject.toml`` the gate job syncs no environment, so it
    has no interpreter of its own to ask for: the held-out runs ask for the
    base's request, and still run."""
    pytest_ini = ("kew/pytest.ini", "[pytest]\npythonpath = src\n")
    repo, contract = _repo(
        tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"}, config=pytest_ini
    )
    candidate = resolve_candidate(repo)
    log = _logging_uv(stub_bin)
    _given_held_out_runner(monkeypatch)
    result = _verify(repo, contract)
    assert result.outcome == "passed", result.log
    assert _uv_calls(log, "synced ") == [], _uv_log(log)
    requests = [call.split()[-1] for call in _uv_calls(log, "call python find ")]
    assert requests == [candidate.held_out_python]
    _ran_on_the_found_interpreter(log, runs=6)


#: Run after the fixture's ``uv sync``: a ``.pth`` in the synced environment
#: that prints whenever its interpreter starts without ``-I`` — which uv's own
#: interpreter query passes, so the suite job's ``uv sync`` still reads it.
_PTH_THAT_PRINTS = (
    "for site in .venv/lib/python*/site-packages; do\n"
    "    printf \"import sys; sys.flags.isolated or print('from a pth')\\n\" "
    '> "$site/kew_noise.pth"\n'
    "done"
)

#: Run after the fixture's ``uv sync``: the environment's interpreter, behind
#: a wrapper that fails whenever it is started isolated.
_INTERPRETER_THAT_FAILS_ISOLATED = (
    "mv .venv/bin/python .venv/bin/python-real\n"
    'printf \'#!/bin/sh\\n[ "$1" = -I ] && exit 3\\n'
    'exec "${0%%/*}/python-real" "$@"\\n\' > .venv/bin/python\n'
    "chmod +x .venv/bin/python"
)


def test_a_pth_file_that_prints_does_not_change_the_main_phases_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin: Path
) -> None:
    """The synced interpreter is asked for its version isolated, so a ``.pth``
    in the environment that prints cannot change the answer."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"})
    log = _logging_uv(stub_bin, after_sync=_PTH_THAT_PRINTS)
    _given_held_out_runner(monkeypatch)
    result = _verify(repo, contract)
    assert result.outcome == "passed", result.log
    synced = {line.split()[-1] for line in _uv_calls(log, "synced ")}
    assert len(synced) == 1, _uv_log(log)
    requests = [call.split()[-1] for call in _uv_calls(log, "call python find ")]
    assert requests == [f"=={synced.pop()}"]


def test_a_synced_interpreter_that_reports_no_version_is_a_broken_environment(
    tmp_path: Path, stub_bin: Path
) -> None:
    """Falling back to the base's request would hold the head out on a Python
    the suite does not run on, so the job says its environment is broken."""
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": f"# tidied\n{_CALC}"})
    log = _logging_uv(stub_bin, after_sync=_INTERPRETER_THAT_FAILS_ISOLATED)
    result = _verify(repo, contract)
    assert result.error_category == merge_verify.ENVIRONMENT_BROKEN, result.log
    assert ".venv/bin/python did not report its version" in (result.error or "")
    assert not merge_verify.gate_ran(result)
    assert _uv_calls(log, "call python find ") == [], _uv_log(log)


def _run_held_out_check_job(repo: Path, candidate: VerifyCandidate) -> JobResult:
    """The pre-review held-out job, run on the local host.

    :func:`~kew.merge_verify.run_held_out_check` refuses the local host, so
    this ships and runs the same job the same way, on this machine.
    """
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    provider = provider_pool(config, provider="local", count=1)[0]
    job = replace(
        build_job(candidate, config=config, provider=provider),
        command=held_out_job_script(candidate),
    )
    workspace = repo.parent / "bundle"
    workspace.mkdir()
    bundle = write_bundle(repo, candidate, workspace)
    with provider.claim():
        try:
            provider.ensure_up()
            provider.ship(bundle)
            return provider.run(job)
        finally:
            provider.release()


def test_the_pre_review_held_out_job_runs_on_the_bases_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_bin: Path
) -> None:
    """The pre-review check syncs no checkout environment, so its held-out
    runs ask for the base's request — and run, and judge the head."""
    repo, _contract = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
    base = _run(["git", "rev-parse", "main"], repo)
    head = _run(["git", "rev-parse", "HEAD"], repo)
    candidate = held_out_check_candidate(
        repo, ref=head, base_sha=base, only=(BASE_TEST,)
    )
    log = _logging_uv(stub_bin)
    _given_held_out_runner(monkeypatch)
    result = _run_held_out_check_job(repo, candidate)
    assert result.exit_code == 0, result.log
    report = held_out_report(
        held_out_section(result.log),
        runs=merge_verify.HELD_OUT_RUNS,
        declared=frozenset(),
    )
    assert report.outcome == "failed", result.log
    assert report.undeclared == (BASE_TEST,)
    requests = [call.split()[-1] for call in _uv_calls(log, "call python find ")]
    assert requests == [candidate.held_out_python]
    # Two runs, each after the base's own collection; no contract run.
    _ran_on_the_found_interpreter(log, runs=4)


_CONTRACT_WITH_A_SKIP = (
    "import calc\nimport pytest\nfrom calc import add\n\n\n"
    "def test_ac1():\n    assert add(2, 3) == 5\n\n\n"
    "def test_ac2():\n"
    "    if getattr(calc, 'SKIP', False):\n"
    "        pytest.skip('the head says so')\n"
    "    assert add(1, 1) == 2\n"
)


@pytest.mark.parametrize("skips", [True, False], ids=["skips", "control"])
def test_a_contract_passes_only_when_every_contract_test_it_ran_passed(
    tmp_path: Path, skips: bool
) -> None:
    """One contract test passing is not the contract passing: a head whose
    src makes another skip is not passed."""
    calc = f"SKIP = True\n{_CALC}" if skips else f"# tidied\n{_CALC}"
    repo, contract = _repo(
        tmp_path, {"kew/src/calc.py": calc}, contract_text=_CONTRACT_WITH_A_SKIP
    )
    result = _verify(repo, contract)
    assert result.outcome == ("failed" if skips else "passed"), result.log
    assert result.held_out is not None
    if skips:
        assert result.held_out.error == (
            "acceptance contract tests ran and did not pass (skipped or xfailed): "
            f"{CONTRACT}::test_ac2"
        )


def _declaring(*nodes: str) -> str:
    lines = "".join(f"- `{node}`: add is gone on purpose\n" for node in nodes)
    return f"## Behavior changes\n\n{lines}"


@pytest.mark.parametrize(
    ("declared", "outcome"),
    [
        (("tests/test_calc.py",), "passed"),
        (("tests/test_calc.py", BASE_TEST), "passed"),
        ((BASE_TEST,), "failed"),
    ],
    ids=["file-only", "file-and-test", "test-only"],
)
def test_declaring_a_file_that_fails_to_collect_excuses_its_uncollected_tests(
    tmp_path: Path, declared: tuple[str, ...], outcome: str
) -> None:
    """The head removes ``add``, so the base's test file fails to import:
    the file's failure and its uncollected test are each named, and the file
    alone, declared, excuses both — one cause, one line. The test alone
    leaves the file's own failure undeclared."""
    head = {
        "kew/pyproject.toml": f"{_PYPROJECT}xfail_strict = true\n",
        "kew/src/calc.py": "def subtract(a, b):\n    return a - b\n",
    }
    repo, _contract = _repo(tmp_path, head)
    result = _verify(repo, None, pr_body=_declaring(*declared))
    assert result.outcome == outcome, result.log
    assert result.held_out is not None
    assert set(result.held_out.failed) == {"tests/test_calc.py", BASE_TEST}
    if outcome == "failed":
        assert result.held_out.undeclared == ("tests/test_calc.py",)


def _run_plugin(tmp_path: Path, files: dict[str, str]) -> dict[str, object]:
    """Run kew's report plugin as the script, as a held-out run does, over a
    checkout holding *files*, with that checkout's ``src`` on ``sys.path`` from
    start-up — as an editable install's ``.pth`` puts it — and read its report."""
    checkout = tmp_path / "checkout"
    for name, text in {
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
        **files,
    }.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text, encoding="utf-8")
    (checkout / "src").mkdir(exist_ok=True)
    plugin = tmp_path / "plugin" / "kew_held_out_0123456789abcdef.py"
    plugin.parent.mkdir()
    plugin.write_bytes(
        Path(merge_verify.__file__).with_name("held_out_report_plugin.py").read_bytes()
    )
    lock = Path(merge_verify.__file__).parents[2] / "uv.lock"
    report = tmp_path / "report.json"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("KEW_")},
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": plugin.stem,
        "PYTHONPATH": f"{checkout / 'src'}:{plugin.parent}",
        "KEW_HELD_OUT_REPORT": str(report),
        "KEW_HELD_OUT_SETTINGS": pins(lock.read_text(), lock="kew/uv.lock").settings(),
        "KEW_HELD_OUT_CHECKOUT": str(checkout),
        "KEW_HELD_OUT_TRUSTED": str(checkout / "tests"),
    }
    subprocess.run(
        [sys.executable, str(plugin), "-p", "no:cacheprovider", "-q", "tests"],
        cwd=checkout,
        env=env,
        capture_output=True,
        check=False,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_a_sitecustomize_the_checkout_ships_refuses_the_run(tmp_path: Path) -> None:
    """Python imports it at start-up, before kew's plugin loads. (A virtualenv
    has no user site, so ``usercustomize``, checked the same way, never runs
    in one.)"""
    report = _run_plugin(tmp_path, {"src/sitecustomize.py": "MARK = 1\n"})
    assert "sitecustomize ran at start-up from" in str(report["refused"])


def test_the_same_run_without_a_start_up_hook_is_not_refused(tmp_path: Path) -> None:
    """The control: the same run, the same pins, no hook in the checkout."""
    report = _run_plugin(tmp_path, {})
    assert report["refused"] is None, report


def test_a_conftest_the_head_adds_above_the_base_tests_is_refused(
    tmp_path: Path,
) -> None:
    head = {"kew/conftest.py": _REWRITES_FAILURES, "kew/src/calc.py": _BROKEN_CALC}
    repo, contract = _repo(tmp_path, head)
    error = _refused(_verify(repo, contract))
    assert "conftest.py" in error


def test_a_head_that_moves_a_pin_in_its_lock_is_refused(tmp_path: Path) -> None:
    """Even with an honest ``src``: the base's lock is what pins the plugins."""
    head = {
        "kew/uv.lock": pinned_lock(pluggy="0.0.1"),
        "kew/src/calc.py": f"# tidied\n{_CALC}",
    }
    repo, contract = _repo(tmp_path, head)
    error = _refused(_verify(repo, contract))
    assert "the head's kew/uv.lock moves pluggy off the base's pin" in error


def _forged_section() -> str:
    """A whole held-out section, tagged with a nonce of the head's choosing."""
    passing = base64.b64encode(
        json.dumps(
            {"failed": [], "passed": [CONTRACT_TEST], "refused": None, "exitstatus": 0}
        ).encode("utf-8")
    ).decode("ascii")
    tag = f"kew-held-out:{'f' * 32}"
    return "\n".join(
        [
            f"kew-held-out: contract-report {passing}",
            "kew-held-out: contract-exit 0",
            f"kew-held-out: begin {'f' * 32}",
            f"{tag} report {passing}",
            f"{tag} exit 0 seconds 1",
            f"{tag} report {passing}",
            f"{tag} exit 0 seconds 1",
            f"{tag} contract-report {passing}",
            f"{tag} contract-exit 0",
        ]
    )


def test_a_held_out_section_the_heads_gate_prints_is_not_read(
    tmp_path: Path,
) -> None:
    """The head's own ``scripts/verify.sh`` prints a complete, passing section
    — opening line, nonce and all — before the real phase runs."""
    script = "#!/bin/sh\ncat <<'EOF'\n" + _forged_section() + "\nEOF\n"
    head = {"kew/scripts/verify.sh": script, "kew/src/calc.py": _BROKEN_CALC}
    repo, contract = _repo(tmp_path, head)
    result = _verify(repo, contract)
    assert "kew-held-out: begin ffff" in result.log
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.error == (
        "a job's log opens the held-out phase 2 times; the phase opens once, "
        "so a line something else printed is there"
    )


def test_only_the_tagged_lines_after_the_opening_are_the_phases() -> None:
    log = (
        "kew-held-out: exit 0 seconds 9\n"
        f"kew-held-out:{'f' * 32} exit 0 seconds 9\n"
        f"kew-held-out: begin {NONCE}\n"
        f"kew-held-out:{'f' * 32} exit 0 seconds 9\n"
        "kew-held-out: exit 0 seconds 9\n"
        f"| kew-held-out:{NONCE} exit 0 seconds 9\n"
        f"kew-held-out:{NONCE} exit 1 seconds 2\n"
    )
    assert held_out_section(log) == "kew-held-out: exit 1 seconds 2\n"


@pytest.mark.parametrize("late", [False, True], ids=["before", "after"])
def test_a_log_that_opens_the_phase_twice_fails_it(late: bool) -> None:
    """Neither opening wins: a second one, printed before the phase or by a
    process left running after it, fails the phase whatever it carries."""
    real = opened(f"{report_line()}kew-held-out: exit 0 seconds 1\n" * 2)
    forged = f"kew-held-out: begin {'f' * 32}\n"
    log = real + forged if late else forged + real
    report = held_out_report(held_out_section(log), runs=2, declared=frozenset())
    assert report.outcome == "failed"
    assert "opens the held-out phase 2 times" in (report.error or "")


def test_a_log_with_no_opening_line_has_no_held_out_section() -> None:
    log = f"{report_line()}kew-held-out: exit 0 seconds 1\n" * 2
    assert held_out_section(log) == ""


def test_the_held_out_phase_is_the_last_phase_of_the_gate_job(tmp_path: Path) -> None:
    """Nothing the head's code prints may follow the phase's opening line: the
    held-out phase ends the gate job, and ``ui-check`` runs in the suite job
    kew starts only after it judged the held-out section (#3212)."""
    repo, _contract = _repo(tmp_path, {"frontend/x.js": "1\n"})
    candidate = resolve_candidate(repo)
    script = job_script(candidate, images=[])
    assert script.index("KEW_VERIFY_STAGE=gates bash scripts/verify.sh") < (
        script.index("kew-held-out: begin")
    )
    assert merge_verify.UI_CHECK_COMMAND not in script
    assert merge_verify.UI_CHECK_COMMAND in merge_verify.suite_job_script(candidate)


_NESTED_RUN = (
    "import subprocess\nimport sys\n\n\n"
    "def test_a_pytest_started_by_a_test_autoloads(tmp_path):\n"
    "    inner = tmp_path / 'test_inner.py'\n"
    "    inner.write_text('def test_inner(worker_id):\\n    assert worker_id\\n')\n"
    "    args = [sys.executable, '-m', 'pytest', str(inner)]\n"
    "    assert subprocess.run(args, cwd=tmp_path).returncode == 0\n"
)


@pytest.mark.parametrize("selection", ['-m "not serial"', '-n 2 -m "not serial"'])
def test_a_pytest_a_held_out_test_starts_runs_with_autoload_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    """Autoload is off for the held-out run itself, not for a pytest one of
    its tests starts: that one needs xdist's ``worker_id`` without ``-p``."""
    monkeypatch.setattr(merge_verify, "HELD_OUT_PARALLEL", selection)
    nested = "kew/tests/test_nested.py"
    repo = tmp_path / "project"
    repo.mkdir()
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    _commit(
        repo,
        {
            "kew/pyproject.toml": _PYPROJECT,
            "kew/uv.lock": pinned_lock("pytest-xdist", "execnet"),
            nested: _NESTED_RUN,
            "kew/scripts/verify.sh": "#!/bin/sh\necho GATE RAN\n",
            "scripts/scan-agent-image.sh": "#!/bin/sh\necho PREFLIGHT RAN\n",
        },
    )
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    _commit(repo, {nested: f"# tidied\n{_NESTED_RUN}"})
    result = _verify(repo, None)
    assert result.outcome == "passed", result.log
    assert result.held_out is not None and result.held_out.outcome == "passed"


def test_a_log_line_shaped_like_a_passing_contract_test_is_not_a_result(
    tmp_path: Path,
) -> None:
    """The head skips every module that imports it, with a skip reason that
    prints a ``PASSED <contract test>`` line into the run's log."""
    skipping = (
        "import pytest\n\n"
        f"pytest.skip('later\\nPASSED {CONTRACT_TEST}', allow_module_level=True)\n"
    )
    repo, contract = _repo(tmp_path, {"kew/src/calc.py": skipping})
    result = _verify(repo, contract)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    # Skipped at import, the contract is never collected: the base collects it.
    assert result.held_out.error == (
        f"acceptance contract tests failed against the candidate's src: "
        f"{CONTRACT_TEST} (1 of them the base collects for the run and the "
        f"candidate's run never collected: {CONTRACT_TEST})"
    )


class TestPins:
    """What the base's lock pins, and what the head's lock may not move."""

    def test_the_base_lock_pins_the_runner_and_the_plugins_it_carries(self) -> None:
        pinned = pins(pinned_lock("pytest-xdist"), lock="kew/uv.lock")
        assert pinned.refused == ()
        assert pinned.args() == ["-p", "xdist"]

    @pytest.mark.parametrize(
        ("base", "reason"),
        [
            (None, "the base has no kew/uv.lock to pin pytest by"),
            ("version = [\n", "the base's kew/uv.lock does not parse"),
            (_NO_PLUGGY, "the base's kew/uv.lock pins no single pluggy"),
        ],
        ids=["no-lock", "unreadable", "no-pluggy"],
    )
    def test_a_base_lock_that_pins_no_runner_refuses_every_run(
        self, base: str | None, reason: str
    ) -> None:
        refused = pins(base, lock="kew/uv.lock").refused
        assert refused and refused[0].startswith(reason)

    def test_a_head_lock_that_drops_a_pin_moves_it(self) -> None:
        assert pins(_LOCK, "", lock="kew/uv.lock").refused == (
            "the head's kew/uv.lock moves pytest off the base's pin",
            "the head's kew/uv.lock moves pluggy off the base's pin",
        )

    def test_a_head_lock_that_points_a_pin_elsewhere_moves_it(self) -> None:
        moved = _NO_PLUGGY + (
            f'[[package]]\nname = "pluggy"\nversion = "{version("pluggy")}"\n'
            'source = { path = "vendor/pluggy.whl" }\n'
        )
        assert pins(_LOCK, moved, lock="kew/uv.lock").refused == (
            "the head's kew/uv.lock moves pluggy off the base's pin",
        )


def _contract_log(status: int, report: str) -> str:
    runs = f"{report_line()}kew-held-out: exit 0 seconds 1\n" * 2
    return f"{runs}{report}kew-held-out: contract-exit {status}\n"


def test_a_contract_run_exiting_1_with_no_contract_failure_is_refused() -> None:
    log = _contract_log(1, report_line(label="contract-report"))
    report = held_out_report(log, runs=2, declared=frozenset(), contract=CONTRACT)
    assert report.outcome == "failed"
    assert report.error == (
        f"the acceptance contract {CONTRACT} run exited 1 "
        "with no failing test inside the contract"
    )


def test_a_contract_run_with_no_report_is_refused() -> None:
    log = _contract_log(0, "kew-held-out: contract-report -\n")
    report = held_out_report(log, runs=2, declared=frozenset(), contract=CONTRACT)
    assert report.outcome == "failed"
    assert report.error == f"the acceptance contract {CONTRACT} run wrote no report"


def test_a_contract_passed_line_in_the_log_no_longer_counts() -> None:
    log = _contract_log(
        5,
        f"kew-held-out: contract-passed {CONTRACT_TEST}\n"
        f"{report_line(label='contract-report')}",
    )
    report = held_out_report(log, runs=2, declared=frozenset(), contract=CONTRACT)
    assert report.error == f"the acceptance contract {CONTRACT} ran no test"


def test_a_failed_line_in_the_log_is_not_read_as_a_failure() -> None:
    log = (
        f"kew-held-out: failed {BASE_TEST}\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n" * 2
    )
    report = held_out_report(log, runs=2, declared=frozenset())
    assert report.outcome == "passed" and report.failed == ()


def test_a_run_whose_report_is_refused_fails_the_phase() -> None:
    log = (
        f"{report_line(refused='the pytest plugin x (y) from z is not pinned')}"
        "kew-held-out: exit 0 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )
    report = held_out_report(log, runs=2, declared=frozenset())
    assert report.outcome == "failed"
    assert report.error == (
        "a held-out run was refused: the pytest plugin x (y) from z is not pinned"
    )


def test_a_run_whose_report_kew_cannot_read_fails_the_phase() -> None:
    log = (
        "kew-held-out: report bm90IGpzb24=\nkew-held-out: exit 0 seconds 1\n"
        f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    )
    report = held_out_report(log, runs=2, declared=frozenset())
    assert report.outcome == "failed"
    assert report.error == "a held-out run wrote a report kew cannot read"


def test_the_helper_writes_lines_as_the_phase_tags_them() -> None:
    """The test helper's log reads back through the section reader as written."""
    lines = f"{report_line()}kew-held-out: exit 0 seconds 1\n"
    assert held_out_section(f"gate passed\n{opened(lines)}") == lines


def _uv_running(tmp_path: Path, python: Path) -> Path:
    """A uv boundary that runs its ``python`` command under *python*."""
    executable = tmp_path / "fake-uv"
    executable.write_text(
        f"""#!{sys.executable}
import subprocess
import sys

args = sys.argv[1:]
python_args = args[args.index("python") + 1:]
completed = subprocess.run([{str(python)!r}, *python_args])
raise SystemExit(completed.returncode)
"""
    )
    executable.chmod(0o755)
    return executable


def _acceptance_repo(tmp_path: Path, lock: str, test: str) -> Path:
    repo = tmp_path / "repo"
    (repo / CONTRACT_PATH).parent.mkdir(parents=True)
    (repo / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "kew" / "uv.lock").write_text(lock)
    (repo / CONTRACT_PATH).write_text(test)
    return repo


def test_an_entry_point_that_rewrites_failures_does_not_turn_a_red_acceptance_run_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.acceptance_author import run_pytest_node
    from kew.subprocess_utils import TOOL_PATHS

    python = _environment(
        tmp_path, {"rewrite-failures": ("rewrite_failures", _REWRITES_FAILURES)}
    )
    monkeypatch.setitem(TOOL_PATHS, "uv", str(_uv_running(tmp_path, python)))
    repo = _acceptance_repo(tmp_path, _LOCK, "def test_ac1():\n    assert 1 == 2\n")

    assert run_pytest_node(repo, f"{CONTRACT_PATH}::test_ac1") == "failed"


def test_an_acceptance_run_whose_pinned_name_answers_to_another_module_is_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.acceptance_author import run_pytest_node
    from kew.subprocess_utils import TOOL_PATHS

    python = _environment(tmp_path, {"not-asyncio": ("asyncio", _IMPERSONATES_ASYNCIO)})
    monkeypatch.setitem(TOOL_PATHS, "uv", str(_uv_running(tmp_path, python)))
    lock = pinned_lock("pytest-asyncio")
    repo = _acceptance_repo(tmp_path, lock, "def test_ac1():\n    assert 1 == 2\n")

    assert run_pytest_node(repo, f"{CONTRACT_PATH}::test_ac1") == "error"


@pytest.mark.parametrize(
    ("lock", "outcome"),
    [
        (pinned_lock("pytest-xdist", "execnet"), "passed"),
        (_LOCK, "error"),
    ],
    ids=["locked", "not-locked"],
)
def test_an_acceptance_run_loads_a_pinned_plugin_only_when_the_lock_installs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: str, outcome: str
) -> None:
    """``worker_id`` is xdist's fixture: it resolves only with xdist loaded."""
    from kew.acceptance_author import run_pytest_node
    from kew.subprocess_utils import TOOL_PATHS

    uv = _uv_running(tmp_path, Path(sys.executable))
    monkeypatch.setitem(TOOL_PATHS, "uv", str(uv))
    repo = _acceptance_repo(
        tmp_path, lock, "def test_ac1(worker_id):\n    assert worker_id\n"
    )

    assert run_pytest_node(repo, f"{CONTRACT_PATH}::test_ac1") == outcome
