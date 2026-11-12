"""A local verify job: its whole session dies with it, and a broken venv says so."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from support.group_leaks import group_members
from support.process_facts import alive

from kew import merge_verify, work_dirs
from kew.hosts import LocalProvider
from kew.hosts.errors import HostTimeout
from kew.hosts.provider import Job

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]


def _bundle(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    subprocess.run([*_GIT, "init", "--quiet", "-b", "main"], cwd=repo, check=True)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run([*_GIT, "add", "."], cwd=repo, check=True)
    subprocess.run([*_GIT, "commit", "--quiet", "-m", "seed"], cwd=repo, check=True)
    bundle = tmp_path / "repo.bundle"
    subprocess.run(
        [*_GIT, "bundle", "create", str(bundle), "--all"], cwd=repo, check=True
    )
    return bundle


def test_a_timed_out_job_leaves_no_process_in_its_session_and_no_workspace(
    tmp_path: Path,
) -> None:
    """``sleep 300 &`` under a 2 s ceiling: the group is empty, the tree gone.

    The job records its session id and its held-out tree's path outside the
    workspace before it waits, so both can be checked after the workspace is
    removed. The oracle is the process table and the filesystem.
    """
    facts = tmp_path / "facts"
    facts.mkdir()
    provider = LocalProvider(lock_dir=tmp_path / "locks")
    provider.ensure_up()
    provider.ship(_bundle(tmp_path))
    workspace = provider.checkout.parent if provider.checkout else None
    command = (
        'held_out_root="$(mktemp -d "${TMPDIR:-/tmp}/kew-held-out-XXXXXX")"; '
        f'echo "$held_out_root" > {facts}/held-out; '
        "sleep 300 & "
        f"echo $! > {facts}/sleep; "
        f"echo $$ > {facts}/pgid; "
        "wait"
    )
    try:
        with pytest.raises(HostTimeout, match="exceeded its 2.0s ceiling"):
            provider.run(Job(name="verify", command=command, timeout_seconds=2.0))
    finally:
        provider.release()
    pgid = int((facts / "pgid").read_text())
    held_out = Path((facts / "held-out").read_text().strip())
    sleeper = int((facts / "sleep").read_text())
    assert (alive(sleeper), group_members(pgid)) == (False, {})
    assert workspace is not None
    assert held_out.is_relative_to(workspace)
    assert (workspace.exists(), held_out.exists()) == (False, False)


def test_a_local_workspace_lives_under_the_work_root(tmp_path: Path) -> None:
    provider = LocalProvider(lock_dir=tmp_path / "locks")
    provider.ensure_up()
    provider.ship(_bundle(tmp_path))
    try:
        result = provider.run(Job(name="verify", command='printf "%s" "$TMPDIR"'))
        checkout = provider.checkout
    finally:
        provider.release()
    assert checkout is not None
    assert checkout.parent.parent == work_dirs.configured_root() / work_dirs.UNSCOPED
    assert result.log == str(checkout.parent / "tmp")


#: The probe's module list for the fake virtualenv: ``demo``, shipped by ``demo``.
_DEMO: Mapping[str, str] = {"demo": "demo"}


def _fake_venv(checkout: Path) -> Path:
    """A real virtualenv in *checkout* with one installed distribution."""
    (checkout / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")],
        check=True,
    )
    purelib = subprocess.run(
        [
            str(checkout / ".venv/bin/python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    site = Path(purelib)
    (site / "demo").mkdir()
    (site / "demo/__init__.py").write_text("VALUE = 1\n")
    (site / "demo/helpers.py").write_text("HELP = 2\n")
    info = site / "demo-1.0.dist-info"
    info.mkdir()
    (info / "RECORD").write_text(
        "demo/__init__.py,,\ndemo/helpers.py,,\ndemo-1.0.dist-info/RECORD,,\n"
    )
    return site


def _verify(
    tmp_path: Path,
    checkout: Path,
    *,
    after_sync: str,
    suite: str,
    modules: Mapping[str, str] = _DEMO,
) -> Any:
    """Run the probe, *after_sync*, then *suite* as a local verify job does.

    ``uv sync`` is the one step stood in for (by ``true``): this host has no
    index to sync from, and the virtualenv is already built. Everything after
    it is the job's own lines, run by the local host and read back by the
    verify gate.
    """
    script = "\n".join(
        [
            "set -eu",
            f"cd {shlex.quote(str(checkout))}",
            *merge_verify.environment_probe_lines(modules),
            *merge_verify.environment_record_lines(sync="true"),
            after_sync,
            *merge_verify.probed_suite_lines(suite),
        ]
    )
    provider = LocalProvider(lock_dir=tmp_path / "locks")
    job = Job(name="verify", command=script, timeout_seconds=60.0)
    return merge_verify._run_on_host(provider, _bundle(tmp_path), job)


def test_a_file_missing_from_site_packages_fails_as_one_environment_error(
    tmp_path: Path,
) -> None:
    """The suite never runs, and the verify reports the environment, by name.

    The file is deleted after the post-sync digest, as a temp cleaner deletes
    one, and before the suite: the job ends at the probe, with the category.
    """
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    site = _fake_venv(checkout)
    result = _verify(
        tmp_path,
        checkout,
        after_sync=f"rm {shlex.quote(str(site / 'demo/helpers.py'))}",
        suite="echo SUITE-RAN",
    )
    assert "SUITE-RAN" not in result.log
    assert (result.outcome, result.error_category) == (
        "failed",
        "verify_host_environment_broken",
    )
    assert "demo/helpers.py" in (result.error or "")


def test_files_lost_while_the_suite_runs_report_the_environment(
    tmp_path: Path,
) -> None:
    """The suite fails because its venv lost a file: one environment error."""
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    site = _fake_venv(checkout)
    lost = shlex.quote(str(site / "demo/helpers.py"))
    result = _verify(
        tmp_path, checkout, after_sync="true", suite=f"sh -c 'rm {lost}; exit 1'"
    )
    assert (result.exit_code, result.error_category) == (
        merge_verify.ENVIRONMENT_BROKEN_EXIT,
        "verify_host_environment_broken",
    )


def test_an_intact_environment_runs_the_suite(tmp_path: Path) -> None:
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    _fake_venv(checkout)
    result = _verify(tmp_path, checkout, after_sync="true", suite="echo SUITE-RAN")
    assert (result.outcome, "SUITE-RAN" in result.log) == ("passed", True)


def test_a_project_without_any_listed_distribution_passes_the_probe(
    tmp_path: Path,
) -> None:
    """``kew verify`` checks any project: modules it never installs are not lost.

    The fake virtualenv holds only ``demo``, none of kew's own probe list.
    """
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    _fake_venv(checkout)
    result = _verify(
        tmp_path,
        checkout,
        after_sync="true",
        suite="echo SUITE-RAN",
        modules=merge_verify.PROBE_MODULES,
    )
    assert (result.outcome, result.error_category) == ("passed", None)
    assert "SUITE-RAN" in result.log


def test_an_installed_distribution_whose_module_fails_to_import_is_broken(
    tmp_path: Path,
) -> None:
    """Installed is read off the metadata; the import must then still work."""
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    _fake_venv(checkout)
    result = _verify(
        tmp_path,
        checkout,
        after_sync="true",
        suite="echo SUITE-RAN",
        modules={"demo.absent": "demo"},
    )
    assert "SUITE-RAN" not in result.log
    assert result.error_category == "verify_host_environment_broken"
    assert "import demo.absent" in (result.error or "")


def test_a_failing_suite_in_an_intact_environment_keeps_its_own_exit(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "venv-checkout"
    checkout.mkdir()
    _fake_venv(checkout)
    result = _verify(tmp_path, checkout, after_sync="true", suite="sh -c 'exit 3'")
    assert (result.exit_code, result.error_category) == (3, None)
