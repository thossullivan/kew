"""No test reaches the operator's ``gh`` (#3069).

``preflight_tools()`` registers whatever ``gh`` is on PATH, and on the
operator's machine that is an authenticated GitHub CLI. Review-stage tests
drove a real tick without faking the forge, so they posted a commit status and
a PR comment for the placeholder repo ``o/r``; GitHub answered 404, the
best-effort publishers swallowed it, and the tests passed. A fixture naming a
real repository would have written to it.

Each check here runs a child pytest with a recording stub first on PATH, so
the stub is the ``gh`` that child session finds, exactly as the operator's is.
The stub keeps one line per call. Zero lines is the claim.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import repo_inventory

# Only the import-path test carries the policy mark (#3487). kew's pre-review
# gate runs `pytest -m policy` in the check image, which has no gh and mounts
# /tmp noexec, so the two guard tests below have nothing to guard there and no
# place for their gh stub; they run in the full suite, on hosts with a gh.
KEW_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = KEW_DIR / "tests"

REVIEW_STAGE_TEST = (
    "tests/test_review_stage_production_wiring.py::"
    "test_blocking_mode_dispatches_reviewer_and_produces_a_bound_verdict"
)


@contextmanager
def _recording_gh(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A ``gh`` that appends its argv to a log and fails like a 404.

    Written where a script can run: ``tmp_path`` first, then HOME, for a
    sandbox that mounts the temp root ``noexec``.
    """
    for base in (tmp_path, Path.home()):
        root = Path(tempfile.mkdtemp(prefix="kew-gh-recorder-", dir=base))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "gh"
        log = root / "calls.log"
        stub.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\n'
            "echo 'gh: Not Found (HTTP 404)' >&2\nexit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        if os.access(stub, os.X_OK):
            break
        shutil.rmtree(root)
    else:
        pytest.fail("no directory here can hold an executable gh stub")
    try:
        # Prove the recorder records, or zero lines below would prove nothing.
        subprocess.run([str(stub), "probe"], check=False, capture_output=True)
        assert log.read_text(encoding="utf-8") == "probe\n"
        log.unlink()
        yield bin_dir, log
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _child_pytest(
    bin_dir: Path, args: list[str], *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    extra = dict(extra_env or {})
    # The caller's import path goes first; the parent's stays behind it. The
    # check image runs kew from the checkout through PYTHONPATH, not an install.
    import_path = [extra.pop("PYTHONPATH", ""), os.environ.get("PYTHONPATH", "")]
    env = dict(os.environ) | repo_inventory.share() | extra
    joined = os.pathsep.join(
        part for entry in import_path for part in entry.split(os.pathsep) if part
    )
    if joined:
        env["PYTHONPATH"] = joined
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=KEW_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_a_review_stage_test_makes_no_gh_call(tmp_path: Path) -> None:
    """The review stage publishes a status and a comment; neither reaches gh."""
    with _recording_gh(tmp_path) as (bin_dir, log):
        proc = _child_pytest(bin_dir, [REVIEW_STAGE_TEST])

        assert _calls(log) == ""
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
        assert "1 passed" in proc.stdout


def test_an_unfaked_gh_call_is_refused_and_fails_the_test_by_name(
    tmp_path: Path,
) -> None:
    """A test that shells ``gh`` with no fake fails, naming itself, and the
    ``gh`` it reached for never runs."""
    probe_dir = Path(tempfile.mkdtemp(prefix="kew-gh-guard-", dir=tmp_path))
    probe = probe_dir / "test_shells_gh.py"
    probe.write_text(
        "import subprocess\n\n\n"
        "def test_posts_a_status():\n"
        "    try:\n"
        "        subprocess.run(['gh', 'api', '-X', 'POST', 'repos/o/r/statuses/x'])\n"
        "    except OSError:\n"
        "        pass  # swallowed, as the best-effort publishers do\n",
        encoding="utf-8",
    )
    with _recording_gh(tmp_path) as (bin_dir, log):
        # The suite's conftest, loaded as a plugin for a module outside tests/.
        proc = _child_pytest(
            bin_dir,
            ["-p", "conftest", "--rootdir", str(probe_dir), str(probe)],
            extra_env={"PYTHONPATH": str(TESTS_DIR)},
        )

        assert _calls(log) == ""
        assert proc.returncode == 1, proc.stdout[-3000:] + proc.stderr[-2000:]
        assert "test_shells_gh.py::test_posts_a_status launched the operator's gh" in (
            proc.stdout
        )


@pytest.mark.policy
def test_the_child_pytest_keeps_the_parents_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's ``PYTHONPATH`` joins the parent's rather than replacing it,
    so a child still imports kew where kew is only on the import path."""
    parent_dir = tmp_path / "parent_path"
    caller_dir = tmp_path / "caller_path"
    probe_dir = tmp_path / "probe"
    # No gh is called, so the child needs no stub: an empty PATH entry runs
    # wherever a child pytest can, a noexec temp root included.
    bin_dir = tmp_path / "bin"
    for directory in (parent_dir, caller_dir, probe_dir, bin_dir):
        directory.mkdir()
    (parent_dir / "parent_probe_mod.py").write_text("PARENT = 1\n", encoding="utf-8")
    (caller_dir / "caller_probe_mod.py").write_text("CALLER = 1\n", encoding="utf-8")
    probe = probe_dir / "test_imports_both.py"
    probe.write_text(
        "def test_imports_both():\n"
        "    import caller_probe_mod\n"
        "    import kew\n"
        "    import parent_probe_mod\n\n"
        "    assert (caller_probe_mod.CALLER, parent_probe_mod.PARENT) == (1, 1)\n"
        "    assert kew.__name__ == 'kew'\n",
        encoding="utf-8",
    )
    # Prepend, never replace: the check image imports kew only through the
    # PYTHONPATH it starts with.
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{os.pathsep}{parent_dir}{os.pathsep}{inherited}")
    proc = _child_pytest(
        bin_dir,
        ["--rootdir", str(probe_dir), str(probe)],
        extra_env={"PYTHONPATH": str(caller_dir)},
    )

    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout
