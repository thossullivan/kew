"""Held-out runs install exactly what the lock says, under the real uv (#3194).

``uv run`` with no ``--frozen`` re-locks when ``pyproject.toml`` and
``uv.lock`` disagree, so a head that only adds ``[tool.uv.sources]`` — its
lock byte for byte the base's — could install a pluggy of its own, patched to
turn every failed report into a pass, with a ``RECORD`` that hashes clean.

These cases keep the production runner (``uv run --frozen --no-config
--python …``) and run it on a tiny uv project verified end to end on the local
host. Its dependencies come from a find-links directory of wheels repacked
from this interpreter's own installed distributions, so nothing touches the
network; the job's ``HOME``, and so uv's cache, is a fresh directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from support.wheelhouse import repack, wheelhouse

from kew import merge_verify
from kew.config import DispatchConfig, VerifyConfig
from kew.merge_verify import VerifyResult, run_verify

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

BASE_TEST = "tests/test_calc.py::test_add"
_CALC = "def add(a, b):\n    return a + b\n"
_BROKEN_CALC = "def add(a, b):\n    return a - b\n"
_BASE_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"

#: pytest and what it needs on the platforms the lock is resolved for.
_DISTRIBUTIONS = ("pytest", "pluggy", "iniconfig", "packaging", "pygments")

#: The vendored pluggy the attack ships, and where its source points.
_VENDORED = "vendor/pluggy-{version}-py3-none-any.whl"

#: The end of pluggy's ``_multicall``, and the same end with every failed
#: ``pytest_runtest_makereport`` result turned into a pass.
_RETURN = "    else:\n        return result\n"
_PATCHED_RETURN = (
    "    else:\n"
    "        if hook_name == 'pytest_runtest_makereport' and "
    "getattr(result, 'outcome', None) == 'failed':\n"
    "            result.outcome = 'passed'\n"
    "        return result\n"
)


def _pyproject(house: Path, sources: str = "") -> str:
    return (
        '[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.13"\n'
        # pluggy is a direct dependency: [tool.uv.sources] steers only those.
        'dependencies = ["pytest", "pluggy"]\n\n'
        f"[tool.uv]\npackage = false\nno-index = true\n"
        f"find-links = [{json.dumps(str(house))}]\n"
        "environments = [\"sys_platform == 'darwin'\", \"sys_platform == 'linux'\"]\n"
        f"{sources}\n"
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
    )


def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return completed.stdout.strip()


def _commit(repo: Path, files: dict[str, str | Path]) -> None:
    """Commit *files*: text, or a file (a wheel) copied in as it is."""
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            shutil.copyfile(content, path)
        else:
            path.write_text(content, encoding="utf-8")
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "--allow-empty", "-m", "commit"], repo)


def _lock(tmp_path: Path, pyproject: str, vendor: Path | None = None) -> str:
    """``uv lock --offline`` of *pyproject*, in a scratch project of its own."""
    scratch = tmp_path / "lock"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()
    (scratch / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if vendor is not None:
        shutil.copytree(vendor, scratch / "vendor")
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "lock-cache")}
    # HOME is the test's own, so no user uv.toml applies; the project's
    # [tool.uv] names the find-links directory and no index.
    _run(["uv", "lock", "--offline"], scratch, env)
    return (scratch / "uv.lock").read_text(encoding="utf-8")


def _patched_pluggy(dest: Path) -> Path:
    """A wheel of this interpreter's pluggy whose ``_multicall`` forges passes."""

    def edit(path: str, data: bytes) -> bytes:
        if path != "pluggy/_callers.py":
            return data
        text = data.decode("utf-8")
        assert text.count(_RETURN) == 1, "pluggy's _multicall changed shape"
        return text.replace(_RETURN, _PATCHED_RETURN).encode("utf-8")

    return repack("pluggy", dest, edit=edit)


@pytest.fixture(autouse=True)
def _local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(merge_verify, "HELD_OUT_PARALLEL", '-m "not serial"')


def _verify(repo: Path) -> VerifyResult:
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    _candidate, result = run_verify(config, store=None, repo_root=repo, pr_body=None)
    return result


def _repo(
    tmp_path: Path, *, base: dict[str, str | Path], head: dict[str, str | Path]
) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    _commit(
        repo,
        {
            "kew/src/calc.py": _CALC,
            "kew/tests/test_calc.py": _BASE_TEST,
            "kew/scripts/verify.sh": "#!/bin/sh\necho GATE RAN\n",
            "scripts/scan-agent-image.sh": "#!/bin/sh\necho PREFLIGHT RAN\n",
            **base,
        },
    )
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    # The base test is touched, so the held-out phase runs it.
    _commit(repo, {"kew/tests/test_calc.py": f"{_BASE_TEST}# edited\n", **head})
    return repo


def _house(tmp_path: Path) -> Path:
    return wheelhouse(tmp_path / "wheels", *_DISTRIBUTIONS)


def test_the_real_runner_passes_an_honest_head(tmp_path: Path) -> None:
    """The control: the production runner, the same project, nothing forged."""
    pyproject = _pyproject(_house(tmp_path))
    lock = _lock(tmp_path, pyproject)
    base = {"kew/pyproject.toml": pyproject, "kew/uv.lock": lock}
    repo = _repo(tmp_path, base=base, head={"kew/src/calc.py": f"# tidied\n{_CALC}"})
    result = _verify(repo)
    assert result.outcome == "passed", result.log
    assert result.held_out is not None and result.held_out.outcome == "passed"


def test_a_head_that_re_sources_pluggy_with_the_lock_unchanged_is_not_passed(
    tmp_path: Path,
) -> None:
    """The head adds only a ``[tool.uv.sources]`` entry and a vendored,
    patched pluggy; its ``uv.lock`` is byte for byte the base's. The main
    phase installs the vendored copy; the held-out runs install the lock's."""
    house = _house(tmp_path)
    pyproject = _pyproject(house)
    lock = _lock(tmp_path, pyproject)
    wheel = _patched_pluggy(tmp_path / "vendor")
    vendored = _VENDORED.format(version=wheel.name.split("-")[1])
    sources = f'\n[tool.uv.sources]\npluggy = {{ path = "{vendored}" }}\n'
    repo = _repo(
        tmp_path,
        base={"kew/pyproject.toml": pyproject, "kew/uv.lock": lock},
        head={
            "kew/pyproject.toml": _pyproject(house, sources),
            f"kew/{vendored}": wheel,
            "kew/src/calc.py": _BROKEN_CALC,
        },
    )
    assert _run(["git", "diff", "main", "candidate", "--", "kew/uv.lock"], repo) == ""
    result = _verify(repo)
    assert "GATE RAN" in result.log
    assert f"{vendored}" in result.log, "the main phase installs the head's pluggy"
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert result.held_out.undeclared == (BASE_TEST,)


def test_a_pinned_distribution_installed_from_a_path_is_refused(
    tmp_path: Path,
) -> None:
    """A lock that sources pluggy from a wheel in the tree — the base's own,
    so no pin moved — installs it with a ``direct_url.json``: its ``RECORD``
    is whatever that wheel holds, so the run is refused."""
    house = _house(tmp_path)
    wheel = repack("pluggy", tmp_path / "vendor")
    vendored = _VENDORED.format(version=wheel.name.split("-")[1])
    pyproject = _pyproject(
        house, f'\n[tool.uv.sources]\npluggy = {{ path = "{vendored}" }}\n'
    )
    vendor = tmp_path / "vendor-tree"
    vendor.mkdir()
    shutil.copyfile(wheel, vendor / wheel.name)
    lock = _lock(tmp_path, pyproject, vendor=vendor)
    base: dict[str, str | Path] = {
        "kew/pyproject.toml": pyproject,
        "kew/uv.lock": lock,
        f"kew/{vendored}": wheel,
    }
    repo = _repo(tmp_path, base=base, head={})
    result = _verify(repo)
    assert result.outcome == "failed", result.log
    assert result.held_out is not None
    assert "pluggy is installed from a path, VCS or URL, not an index" in (
        result.held_out.error or ""
    )
