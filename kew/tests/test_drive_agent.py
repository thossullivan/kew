"""`kew drive install`: the drive survives a reboot as a launchd agent (#2810).

The plist is read back with ``plistlib`` and asserted on what launchd would
run; launchd itself is a fake ``launchctl`` that remembers which labels are
loaded (``tests/support/fake_launchctl.py``). Every test runs on a real ledger.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from support.fake_launchctl import calls, fake_launchctl
from typer.testing import CliRunner

from kew.cli import app
from kew.config import load_config
from kew.drive_agent import (
    AgentSpec,
    InstalledAgent,
    agent_label,
    checked_env_file,
    definition_hash,
    record_agent,
    recorded_agent,
)
from kew.run_store import SqliteRunStore

runner = CliRunner()

SECRET = "ghp_kew2810secretvalue0000000000000000"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose kew.toml puts its ledger under ``logs``."""
    root = (tmp_path / "project").resolve()
    root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    SqliteRunStore(str(log_dir / "dispatch.db")).close()
    (root / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{log_dir}"\n'
    )
    return root


@pytest.fixture
def cfg(project: Path) -> Any:
    return load_config(project / "kew.toml")


@pytest.fixture
def launchd(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """A macOS host: a fake launchctl, a HOME of its own and this project."""
    with fake_launchctl(tmp_path, monkeypatch) as bin_dir:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.setenv("GITHUB_TOKEN", SECRET)
        monkeypatch.setattr(sys, "platform", "darwin")
        yield bin_dir


def _kew(tmp_path: Path, name: str = "bin") -> Path:
    """An executable only this user can change, standing in for kew."""
    binary = tmp_path / name / "kew"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text('#!/bin/sh\necho kew-ran "$KEW_SEEN" "$@"\n')
    binary.chmod(0o755)
    return binary


def _env_file(tmp_path: Path, body: str = f"export GITHUB_TOKEN={SECRET}\n") -> Path:
    env_dir = tmp_path / "secrets"
    env_dir.mkdir(mode=0o700, exist_ok=True)
    env_file = env_dir / "kew.env"
    env_file.write_text(body)
    env_file.chmod(0o600)
    return env_file


def _plist(project: Path) -> Path:
    label = agent_label(project)
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _ledger(cfg: Any) -> SqliteRunStore:
    return SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db"))


def test_install_writes_an_agent_that_runs_this_projects_drive_at_login(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    env_file = _env_file(tmp_path)
    binary = _kew(tmp_path)

    result = runner.invoke(
        app,
        [
            "drive",
            "install",
            "--interval",
            "45",
            "--kew-binary",
            str(binary),
            "--env-file",
            str(env_file),
        ],
    )

    assert result.exit_code == 0, result.output
    path = _plist(project)
    raw = path.read_bytes()
    agent = plistlib.loads(raw)
    assert agent["Label"] == agent_label(project)
    assert agent["WorkingDirectory"] == str(project)
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    argv = agent["ProgramArguments"]
    # The env file is sourced by path, then the drive is exec'd on the kew.
    assert str(env_file.resolve()) in argv
    drive = argv[argv.index(str(binary)) :]
    assert drive == [
        str(binary),
        "drive",
        "--interval",
        "45",
        "--kew-binary",
        str(binary),
        "--epic-prefix",
        "epic:",
    ]
    assert agent["StandardOutPath"] == str(Path(cfg.logging.dir) / "drive.log")
    # No credential value anywhere: not the env file's, not this shell's.
    assert SECRET.encode() not in raw
    assert set(agent["EnvironmentVariables"]) == {
        "PATH",
        "KEW_PROJECT",
        "KEW_DRIVE_AGENT",
        "KEW_AGENT_DEFINITION_SHA256",
    }
    # kew's ownership mark, so an unrecorded plist is claimed only when marked.
    assert agent["EnvironmentVariables"]["KEW_DRIVE_AGENT"] == "1"
    # The whole definition's stamp, which reconciliation reads back (#2930).
    assert agent["EnvironmentVariables"]["KEW_AGENT_DEFINITION_SHA256"] == (
        definition_hash(agent)
    )
    assert f"bootstrap gui/{_uid()} {path}" in calls(launchd)
    with _ledger(cfg) as store:
        recorded = recorded_agent(store)
    assert recorded is not None and recorded.plist == path


def test_install_without_an_env_file_runs_the_drive_directly(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    binary = _kew(tmp_path)

    result = runner.invoke(app, ["drive", "install", "--kew-binary", str(binary)])

    assert result.exit_code == 0, result.output
    agent = plistlib.loads(_plist(project).read_bytes())
    assert agent["ProgramArguments"][:2] == [str(binary), "drive"]


def test_the_agents_drive_resolves_the_config_install_was_run_under(
    launchd: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KEW_* config set at install reaches the agent: same ledger, same cap."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("KEW_LOG_DIR", "ops-ledger")
    monkeypatch.setenv("KEW_MAX_BUDGET", "7.5")
    installed_ledger = Path(load_config().logging.dir).resolve()

    result = runner.invoke(
        app, ["drive", "install", "--kew-binary", str(_kew(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    agent = plistlib.loads(_plist(project).read_bytes())
    # Now resolve the config as the agent would: its environment, its cwd.
    for var in ("KEW_LOG_DIR", "KEW_MAX_BUDGET", "KEW_PROJECT"):
        monkeypatch.delenv(var)
    for var, value in agent["EnvironmentVariables"].items():
        monkeypatch.setenv(var, value)
    monkeypatch.chdir(agent["WorkingDirectory"])
    under_agent = load_config()
    assert under_agent.logging.dir == str(elsewhere / "ops-ledger")
    assert Path(under_agent.logging.dir) == installed_ledger
    assert under_agent.agent.max_budget_usd == 7.5
    with _ledger(under_agent) as store:
        assert recorded_agent(store) is not None


def test_install_refuses_a_missing_env_file(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "drive",
            "install",
            "--kew-binary",
            str(_kew(tmp_path)),
            "--env-file",
            str(tmp_path / "absent.env"),
        ],
    )

    assert result.exit_code == 2
    assert "no env file" in result.output
    assert not _plist(project).exists()


def test_install_refuses_off_macos(
    launchd: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    result = runner.invoke(
        app, ["drive", "install", "--kew-binary", str(_kew(tmp_path))]
    )

    assert result.exit_code == 2
    assert "macOS" in result.output
    assert not _plist(project).exists()


def test_uninstall_unloads_and_removes_the_agent(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    binary = str(_kew(tmp_path))
    assert (
        runner.invoke(app, ["drive", "install", "--kew-binary", binary]).exit_code == 0
    )

    result = runner.invoke(app, ["drive", "uninstall"])

    assert result.exit_code == 0, result.output
    assert not _plist(project).exists()
    assert f"bootout gui/{_uid()}/{agent_label(project)}" in calls(launchd)
    with _ledger(cfg) as store:
        assert recorded_agent(store) is None
    again = runner.invoke(app, ["drive", "uninstall"])
    assert "no agent is installed" in again.output


def _install(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["drive", "install", "--kew-binary", str(_kew(tmp_path))]
    )
    assert result.exit_code == 0, result.output


def _status(*args: str) -> Any:
    return runner.invoke(app, ["drive", "status", *args])


def test_status_reports_a_loaded_agent(launchd: Path, tmp_path: Path) -> None:
    _install(tmp_path)

    result = _status("--json")

    assert json.loads(result.output)["agent"]["state"] == "loaded"


def test_status_reports_an_installed_but_unloaded_agent(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    _install(tmp_path)
    (launchd / f"loaded.{agent_label(project)}").unlink()  # `launchctl bootout`

    result = _status()

    assert result.exit_code == 1  # no drive runs, as before
    assert f"launchd agent {agent_label(project)}: not loaded" in result.output
    document = json.loads(_status("--json").output)
    assert document["agent"]["state"] == "not loaded"


def test_status_reports_a_disabled_agent(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    _install(tmp_path)
    (launchd / "disabled").write_text(
        f'disabled services = {{\n\t"{agent_label(project)}" => disabled\n}}\n'
    )

    result = _status()

    assert f"{agent_label(project)}: disabled" in result.output


def test_status_reports_an_installed_agent_whose_plist_is_missing(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    _install(tmp_path)
    _plist(project).unlink()

    result = _status()

    assert f"{agent_label(project)}: missing" in result.output


def test_status_without_an_agent_says_nothing_about_one(launchd: Path) -> None:
    result = _status("--json")

    assert json.loads(result.output)["agent"] is None
    assert "launchd agent" not in _status().output


def _uid() -> int:
    return os.getuid()


def test_status_reports_an_unreadable_agent_record_instead_of_none(
    launchd: Path, cfg: Any
) -> None:
    with _ledger(cfg) as store:
        store.set_meta("drive:agent", "{not json")

    result = _status()

    assert "launchd agent ?: unreadable" in result.output
    assert json.loads(_status("--json").output)["agent"]["state"] == "unreadable"
    assert runner.invoke(app, ["drive", "uninstall"]).exit_code == 2


def test_an_install_launchd_refuses_to_load_is_still_reported(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    shim = launchd / "launchctl"
    shim.write_text(
        shim.read_text().replace(
            "  bootstrap)\n",
            '  bootstrap)\n    echo "Bootstrap failed: 5" >&2\n    exit 5\n',
        )
    )

    result = runner.invoke(
        app, ["drive", "install", "--kew-binary", str(_kew(tmp_path))]
    )

    assert result.exit_code == 2
    assert "Bootstrap failed: 5" in result.output
    assert f"{agent_label(project)}: not loaded" in _status().output


def _install_args(tmp_path: Path, *extra: str) -> list[str]:
    return ["drive", "install", "--kew-binary", str(_kew(tmp_path)), *extra]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda f: f.chmod(0o620),
        lambda f: f.chmod(0o602),
        lambda f: f.parent.chmod(0o770),
    ],
    ids=["group-writable", "world-writable", "group-writable-directory"],
)
def test_install_refuses_an_env_file_another_account_could_change(
    launchd: Path, project: Path, tmp_path: Path, spoil: Any
) -> None:
    env_file = _env_file(tmp_path)
    spoil(env_file)

    result = runner.invoke(app, _install_args(tmp_path, "--env-file", str(env_file)))

    assert result.exit_code == 2
    assert "group- or world-writable" in result.output
    assert not _plist(project).exists()
    assert calls(launchd) == []


def test_install_refuses_a_symlinked_env_file(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    link = tmp_path / "secrets" / "link.env"
    link.parent.mkdir(mode=0o700)
    link.symlink_to(_env_file(tmp_path))

    result = runner.invoke(app, _install_args(tmp_path, "--env-file", str(link)))

    assert result.exit_code == 2
    assert "is a symlink" in result.output
    assert not _plist(project).exists()


def test_install_refuses_a_relative_or_shared_kew_binary(
    launchd: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _kew(tmp_path)
    monkeypatch.chdir(binary.parent)
    relative = runner.invoke(app, ["drive", "install", "--kew-binary", "kew"])
    binary.chmod(0o775)
    shared = runner.invoke(app, ["drive", "install", "--kew-binary", str(binary)])
    binary.chmod(0o755)
    binary.parent.chmod(0o777)
    shared_dir = runner.invoke(app, ["drive", "install", "--kew-binary", str(binary)])
    binary.parent.chmod(0o755)

    assert relative.exit_code == 2 and "relative" in relative.output
    assert shared.exit_code == 2 and "group- or world-writable" in shared.output
    assert shared_dir.exit_code == 2 and "directory" in shared_dir.output
    assert not _plist(project).exists()


def test_install_pins_the_kew_binary_to_its_real_path_and_a_minimal_path(
    launchd: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _kew(tmp_path)
    link = tmp_path / "links" / "kew"
    link.parent.mkdir()
    link.symlink_to(binary)
    shell_dir = tmp_path / "anyone-can-write"
    monkeypatch.setenv("PATH", f"{shell_dir}:{os.environ['PATH']}")

    result = runner.invoke(app, ["drive", "install", "--kew-binary", str(link)])

    assert result.exit_code == 0, result.output
    agent = plistlib.loads(_plist(project).read_bytes())
    argv = agent["ProgramArguments"]
    assert argv[0] == str(binary)
    assert argv[argv.index("--kew-binary") + 1] == str(binary)
    search = agent["EnvironmentVariables"]["PATH"].split(":")
    assert search[0] == str(binary.parent)
    assert search[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert str(shell_dir) not in search


def test_the_agents_start_sources_a_private_env_file_and_refuses_one_spoiled_later(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """The start command re-checks the env file before sourcing it, so a
    chmod after install is caught. Run with ``printenv`` standing in for kew:
    it prints the variables the sourced file set. One name per ``printenv``:
    GNU's prints every name it is given, BSD's (macOS) only the first."""
    env_file = _env_file(tmp_path, "drive=yes\nKEW_SEEN=sourced\n")
    stand_in = tmp_path / "stand-in" / "printenv-each"
    stand_in.parent.mkdir()
    stand_in.write_text('#!/bin/sh\nfor name; do printenv "$name" || exit; done\n')
    stand_in.chmod(0o755)
    spec = AgentSpec(
        project=project,
        binary=stand_in,
        drive_args=("KEW_SEEN",),
        log=tmp_path / "drive.log",
        env_file=checked_env_file(env_file),
    )

    started = subprocess.run(spec.program(), capture_output=True, text=True)
    env_file.chmod(0o666)
    refused = subprocess.run(spec.program(), capture_output=True, text=True)

    assert (started.returncode, started.stdout) == (0, "yes\nsourced\n")
    assert refused.returncode == 78
    assert refused.stdout == ""
    assert f"refusing to source {env_file}" in refused.stderr


def test_install_writes_the_plist_whole_with_a_fixed_mode(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    args = _install_args(tmp_path)
    old_umask = os.umask(0o000)
    try:
        result = runner.invoke(app, args)
    finally:
        os.umask(old_umask)

    assert result.exit_code == 0, result.output
    path = _plist(project)
    assert path.stat().st_mode & 0o777 == 0o644
    # Beside it only the agent lock, which outlives the install (#2870).
    lock = path.with_name(f".{agent_label(project)}.lock")
    assert sorted(p.name for p in path.parent.iterdir()) == [lock.name, path.name]
    assert lock.stat().st_mode & 0o777 == 0o600


def test_install_makes_a_private_log_directory(
    launchd: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path / "fresh-ledger"))

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "fresh-ledger").stat().st_mode & 0o777 == 0o700


def test_install_refuses_a_symlinked_log_or_launch_agents_directory(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    log = Path(cfg.logging.dir) / "drive.log"
    log.symlink_to(tmp_path / "elsewhere.log")
    via_log = runner.invoke(app, _install_args(tmp_path))
    log.unlink()
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.parent.mkdir(parents=True)
    (tmp_path / "planted").mkdir()
    agents.symlink_to(tmp_path / "planted")
    via_dir = runner.invoke(app, _install_args(tmp_path))

    assert via_log.exit_code == 2 and "drive log" in via_log.output
    assert via_dir.exit_code == 2 and "symlink" in via_dir.output
    assert list((tmp_path / "planted").iterdir()) == []
    assert calls(launchd) == []


def test_install_creates_a_private_log_before_the_plist(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 0, result.output
    log = Path(cfg.logging.dir) / "drive.log"
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("spoil", "named"),
    [
        (lambda log: log.parent.chmod(0o777), "drive log directory"),
        (lambda log: log.parent.chmod(0o775), "drive log directory"),
        (lambda log: (log.touch(), log.chmod(0o666)), "drive log"),
        (lambda log: (log.touch(), log.chmod(0o620)), "drive log"),
    ],
)
def test_install_refuses_a_log_or_log_directory_another_account_could_change(
    launchd: Path,
    project: Path,
    cfg: Any,
    tmp_path: Path,
    spoil: Any,
    named: str,
) -> None:
    log = Path(cfg.logging.dir) / "drive.log"
    spoil(log)

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 2
    assert named in result.output and "writable" in result.output
    assert not _plist(project).exists()
    assert calls(launchd) == []


def test_install_neither_stops_nor_replaces_a_job_it_did_not_write(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    path = _plist(project)
    path.parent.mkdir(parents=True)
    foreign = plistlib.dumps(
        {"Label": agent_label(project), "ProgramArguments": ["/usr/bin/true"]}
    )
    path.write_bytes(foreign)

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 2
    assert "not the agent kew writes" in result.output
    assert path.read_bytes() == foreign
    assert calls(launchd) == []


def _record(cfg: Any, label: str, plist: Path) -> None:
    with _ledger(cfg) as store:
        record_agent(store, InstalledAgent(label, plist))


@pytest.mark.parametrize("which", ["label", "path", "content"])
def test_uninstall_refuses_what_install_did_not_write(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, which: str
) -> None:
    _install(tmp_path)
    path = _plist(project)
    label = agent_label(project)
    if which == "label":
        other = agent_label(tmp_path / "other")
        target = path.with_name(f"{other}.plist")
        target.write_bytes(path.read_bytes())
        _record(cfg, other, target)
    elif which == "path":
        target = tmp_path / f"{label}.plist"
        target.write_bytes(path.read_bytes())
        _record(cfg, label, target)
    else:
        target = path
        target.write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/true"]})
        )
    before = target.read_bytes()

    result = runner.invoke(app, ["drive", "uninstall"])

    assert result.exit_code == 2, result.output
    assert target.read_bytes() == before
    assert not any(c.startswith("bootout") for c in calls(launchd)[1:])
    with _ledger(cfg) as store:
        assert recorded_agent(store) is not None


def test_uninstall_fails_while_launchd_still_holds_the_agent(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    _install(tmp_path)
    shim = launchd / "launchctl"
    shim.write_text(
        shim.read_text().replace(
            "  bootout)\n",
            '  bootout)\n    echo "Boot-out failed: 5" >&2\n    exit 5\n',
        )
    )

    result = runner.invoke(app, ["drive", "uninstall"])

    assert result.exit_code == 2
    assert "still loaded" in result.output and "Boot-out failed: 5" in result.output
    assert _plist(project).exists()
    with _ledger(cfg) as store:
        assert recorded_agent(store) is not None


def _project(root: Path, logs: Path) -> Path:
    root.mkdir()
    (root / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{logs}"\n'
    )
    return root


def _launchable_dir(tmp_path: Path) -> Path:
    """A directory a script can be run from: the pytest tmp unless it is
    mounted noexec, then one under HOME (removed when the test ends)."""
    import tempfile

    from support.fake_gcloud import launchable

    if launchable(tmp_path):
        return tmp_path
    root = Path(tempfile.mkdtemp(dir=Path.home()))
    _cleanup.append(root)
    return root


_cleanup: list[Path] = []


@pytest.fixture(autouse=True)
def _remove_launchable_dirs() -> Iterator[None]:
    yield
    while _cleanup:
        shutil.rmtree(_cleanup.pop(), ignore_errors=True)


def test_the_env_file_cannot_point_the_agent_at_another_project_or_ledger(
    tmp_path: Path,
) -> None:
    """Executes the rendered program on a real kew: an env file exporting
    another KEW_PROJECT and KEW_LOG_DIR is sourced, then the pins win, so
    `kew drive status` opens project A's ledger, not B's or the stray one."""
    a_logs, b_logs, stray = tmp_path / "a-logs", tmp_path / "b-logs", tmp_path / "x"
    a = _project(tmp_path / "a", a_logs)
    b = _project(tmp_path / "b", b_logs)
    for logs in (a_logs, b_logs, stray):  # each ledger says whose it is
        logs.mkdir()
        with SqliteRunStore(str(logs / "dispatch.db")) as store:
            record_agent(store, InstalledAgent(logs.name, logs / "absent.plist"))
    env_file = _env_file(tmp_path, f"KEW_PROJECT={b}\nKEW_LOG_DIR={stray}\n")
    shim = _launchable_dir(tmp_path) / "kew"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m kew "$@"\n')
    shim.chmod(0o755)
    spec = AgentSpec(
        project=a,
        binary=shim,
        drive_args=("status", "--json"),
        log=tmp_path / "drive.log",
        env_file=checked_env_file(env_file),
    )

    done = subprocess.run(
        spec.program(),
        cwd=a,
        env={**spec.environment_variables(), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert done.returncode == 1, done.stderr  # no drive runs in A
    assert json.loads(done.stdout)["agent"]["label"] == "a-logs"


def test_install_refuses_a_kew_repo_that_carries_a_credential(
    launchd: Path, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_REPO", f"https://alice:{SECRET}@github.com/o/r")

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 2
    assert "KEW_REPO must be owner/name" in result.output
    assert not _plist(project).exists()
    assert calls(launchd) == []


GENERIC_SECRET = "correct-horse-battery"


@pytest.mark.parametrize(
    "repo",
    [
        f"https://forge.example/o/r?access_token={GENERIC_SECRET}",
        f"https://forge.example/o/r#{GENERIC_SECRET}",
        f"http://forge.example/{GENERIC_SECRET}/r",
        f"ftp://forge.example/{GENERIC_SECRET}/r",
        f"ssh://{GENERIC_SECRET}@forge.example/o/r",
    ],
)
def test_install_refuses_a_kew_repo_that_is_not_a_bare_identifier(
    launchd: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
) -> None:
    """A secret that matches no token shape still never reaches the plist when
    it rides in a query, fragment, userinfo or an unaccepted scheme."""
    monkeypatch.setenv("KEW_REPO", repo)

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 2
    assert "https:// or ssh:// URL with no user, query or fragment" in result.output
    assert not _plist(project).exists()
    agents = Path.home() / "Library" / "LaunchAgents"
    written = [p.read_bytes() for p in agents.rglob("*")] if agents.exists() else []
    assert all(GENERIC_SECRET.encode() not in data for data in written)
    assert calls(launchd) == []


@pytest.mark.parametrize(
    "repo", ["o/r", "https://github.com/o/r", "ssh://github.com/o/r.git"]
)
def test_install_accepts_a_credential_free_kew_repo(
    launchd: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: str,
) -> None:
    monkeypatch.setenv("KEW_REPO", repo)

    result = runner.invoke(app, _install_args(tmp_path))

    assert result.exit_code == 0, result.output
    agent = plistlib.loads(_plist(project).read_bytes())
    assert agent["EnvironmentVariables"]["KEW_REPO"] == repo


def test_install_refuses_a_plist_that_would_hold_a_token_shape(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """Whatever carries it in — here the kew binary's path — a token-shaped
    string never reaches the plist."""
    binary = _kew(tmp_path, SECRET)

    result = runner.invoke(app, ["drive", "install", "--kew-binary", str(binary)])

    assert result.exit_code == 2
    assert "would hold a token" in result.output
    assert not _plist(project).exists()
    assert calls(launchd) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "}",
        "  }  ",
        "epic:\n}",
        "epic:\nnext",
        "epic:\rnext",
        "epic:\u2028next",
        "epic:\u2029next",
        "epic:\x85next",
        "epic:\x0bnext",
        "epic:\x1cnext",
    ],
)
def test_install_refuses_a_program_argument_launchctl_print_cannot_read_back(
    launchd: Path, project: Path, tmp_path: Path, prefix: str
) -> None:
    """``launchctl print`` lists one argument per line and ends the list at a
    ``}`` line, so such an argument would make a restore read as another
    program. The installed agent and its job are left as they were."""
    assert runner.invoke(app, _install_args(tmp_path)).exit_code == 0
    before = _plist(project).read_bytes()
    loaded = calls(launchd)

    result = runner.invoke(app, _install_args(tmp_path, "--epic-prefix", prefix))

    assert result.exit_code == 2
    assert repr(prefix) in result.output
    assert "would not read back from `launchctl print`" in result.output
    assert _plist(project).read_bytes() == before
    changed = [c for c in calls(launchd)[len(loaded) :] if not c.startswith("print")]
    assert changed == []


def test_an_install_killed_before_it_records_is_still_found_and_removed(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """The plist is renamed into place, then the ledger write dies: launchd
    would load that plist at the next login, so status names it and
    uninstall removes it, with nothing in the ledger."""
    import sqlite3

    set_meta = SqliteRunStore.set_meta

    def dies_on_the_record(self: Any, key: str, value: str) -> None:
        if key == "drive:agent":
            raise sqlite3.OperationalError("disk I/O error")
        set_meta(self, key, value)

    monkeypatch.setattr(SqliteRunStore, "set_meta", dies_on_the_record)
    result = runner.invoke(app, _install_args(tmp_path))
    monkeypatch.setattr(SqliteRunStore, "set_meta", set_meta)

    assert result.exit_code == 2
    assert _plist(project).is_file()
    with _ledger(cfg) as store:
        assert recorded_agent(store) is None
    status = _status()
    assert f"launchd agent {agent_label(project)}: not loaded" in status.output
    assert "not recorded in the ledger" in status.output
    document = json.loads(_status("--json").output)["agent"]
    assert document["plist"] == str(_plist(project))

    removed = runner.invoke(app, ["drive", "uninstall"])

    assert removed.exit_code == 0, removed.output
    assert not _plist(project).exists()
    assert f"bootout gui/{_uid()}/{agent_label(project)}" in calls(launchd)
    assert json.loads(_status("--json").output)["agent"] is None


def test_status_does_not_claim_a_plist_kew_did_not_write(
    launchd: Path, project: Path
) -> None:
    path = _plist(project)
    path.parent.mkdir(parents=True)
    path.write_bytes(
        plistlib.dumps(
            {"Label": agent_label(project), "ProgramArguments": ["/usr/bin/true"]}
        )
    )

    assert json.loads(_status("--json").output)["agent"] is None
    assert "no agent is installed" in runner.invoke(app, ["drive", "uninstall"]).output
    assert path.exists()


def test_a_reinstall_launchd_refuses_puts_the_previous_agent_back(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """The working agent runs until its replacement is written and recorded;
    when launchd refuses the replacement, the previous plist and job are
    back."""
    _install(tmp_path)
    before = _plist(project).read_bytes()
    shim = launchd / "launchctl"
    shim.write_text(
        shim.read_text().replace(
            "  bootstrap)\n",
            "  bootstrap)\n    if grep -q '<string>77</string>' \"$3\"; then\n"
            '      echo "Bootstrap failed: 5" >&2; exit 5; fi\n',
        )
    )

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert "Bootstrap failed: 5" in result.output
    assert "previous agent is restored" in result.output
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").exists()
    with _ledger(cfg) as store:
        assert recorded_agent(store) == InstalledAgent(
            agent_label(project), _plist(project)
        )
    assert json.loads(_status("--json").output)["agent"]["state"] == "loaded"


# Kills the kew process running this launchctl, which records its pid
# (:func:`_reinstall_code`): launchctl's parent may be the keeper holding the
# agent lock, not kew.
_KILL_KEW = 'kill -9 "$(cat "$here/kew.pid")"'

# Stands in for launchctl around the fake: SIGKILLs the install calling it
# before (or after) the named verb, as a power cut or `kill -9` would.
_KILLING_LAUNCHCTL = f"""#!/bin/sh
here="$(dirname "$0")"
[ -f "$here/kill-before.$1" ] && {{ {_KILL_KEW}; exit 137; }}
"$here/launchctl.real" "$@"
rc=$?
[ -f "$here/kill-after.$1" ] && {_KILL_KEW}
exit $rc
"""


def _reinstall_code(shim: Path, tmp_path: Path) -> str:
    """A fresh kew process reinstalling at ``--interval 77`` through ``shim``,
    its pid in ``kew.pid`` beside it."""
    return _kew_code(shim, tmp_path, _install_args(tmp_path, "--interval", "77"))


def _kew_code(shim: Path, tmp_path: Path, argv: list[str]) -> str:
    """A fresh kew process running ``kew ARGV`` through ``shim``, its pid in
    ``kew.pid`` beside it."""
    return (
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import kew.cli\n"
        "from kew import subprocess_utils\n"
        "from kew.cli import app\n"
        f"Path({str(shim.parent / 'kew.pid')!r}).write_text(str(os.getpid()))\n"
        f"subprocess_utils.TOOL_PATHS['launchctl'] = {str(shim)!r}\n"
        # The conftest's hermetic registry root, as the in-process tests have.
        f"kew.cli.project_root = lambda: Path({str(tmp_path)!r})\n"
        "sys.platform = 'darwin'\n"
        f"app({argv!r})\n"
    )


def _reinstall_killed(
    launchd: Path, project: Path, tmp_path: Path, when: str, verb: str
) -> None:
    """A real `kew drive install --interval 77` process over the installed
    agent, SIGKILLed by launchctl ``when`` (before or after) ``verb``."""
    shim = launchd / "launchctl"
    if not (launchd / "launchctl.real").exists():
        shim.rename(launchd / "launchctl.real")
        shim.write_text(_KILLING_LAUNCHCTL)
        shim.chmod(0o755)
    (launchd / f"kill-{when}.{verb}").touch()
    proc = subprocess.run(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)],
        cwd=project,
        timeout=120,
        check=False,
    )
    assert proc.returncode == -9
    (launchd / f"kill-{when}.{verb}").unlink()
    _wait_for_agent_lock(project)


def _wait_for_agent_lock(project: Path) -> None:
    """Block until the killed kew's keeper lets the agent lock go.

    The keeper holds the lock it was handed until its launchctl exits (#2930),
    so it can outlive the SIGKILLed kew by a moment; the next kew process would
    then read the switch as still in progress (#3163). The kernel answers when
    it is free: a blocking ``flock``, never a sleep."""
    lock = _plist(project).with_name(f".{agent_label(project)}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("when", "verb", "kept"),
    [
        # The new plist is in place and recorded; the previous job still runs.
        ("before", "bootout", "previous"),
        # The previous job is booted out; nothing is loaded.
        ("after", "bootout", "previous"),
        # The new job loaded; only the switch's own bookkeeping is left.
        ("after", "bootstrap", "new"),
    ],
)
def test_a_reinstall_killed_partway_is_settled_by_the_next_kew_process(
    launchd: Path,
    project: Path,
    cfg: Any,
    tmp_path: Path,
    when: str,
    verb: str,
    kept: str,
) -> None:
    """The previous plist is kept beside the new one and the switch is in the
    ledger before anything is replaced, so a fresh process finds the kill:
    a new job launchd loaded stays, anything short of that is rolled back to
    the previous plist and running job."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    saved = _plist(project).with_name(_plist(project).name + ".previous")

    _reinstall_killed(launchd, project, tmp_path, when, verb)

    assert saved.read_bytes() == before
    assert saved.stat().st_mode & 0o777 == 0o600
    assert pending_switch(agent_label(project)) is not None

    agent = json.loads(_status("--json").output)["agent"]

    assert agent["state"] == "loaded"
    assert (launchd / f"loaded.{agent_label(project)}").exists()
    after = _plist(project).read_bytes()
    if kept == "previous":
        assert after == before
        assert "rolled back" in agent["recovered"]
    else:
        assert after != before and b"<string>77</string>" in after
        assert "it stays" in agent["recovered"]
    assert not saved.exists()
    assert pending_switch(agent_label(project)) is None
    with _ledger(cfg) as store:
        assert recorded_agent(store) == InstalledAgent(
            agent_label(project), _plist(project)
        )
    assert "recovered" not in json.loads(_status("--json").output)["agent"]


def test_install_and_uninstall_settle_a_killed_reinstall_first(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    _install(tmp_path)
    saved = _plist(project).with_name(_plist(project).name + ".previous")
    _reinstall_killed(launchd, project, tmp_path, "after", "bootout")

    again = runner.invoke(app, _install_args(tmp_path, "--interval", "78"))

    assert again.exit_code == 0, again.output
    assert "rolled back" in again.output
    assert b"<string>78</string>" in _plist(project).read_bytes()
    assert not saved.exists()

    _reinstall_killed(launchd, project, tmp_path, "after", "bootout")
    removed = runner.invoke(app, ["drive", "uninstall"])

    assert removed.exit_code == 0, removed.output
    assert not _plist(project).exists() and not saved.exists()
    with _ledger(cfg) as store:
        assert recorded_agent(store) is None


def _unrecord(cfg: Any) -> None:
    with _ledger(cfg) as store:
        record_agent(store, None)


def test_unrecorded_plist_for_another_project_is_not_discovered_or_removed(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """Label, program shape and kew's mark all match; the working directory
    and KEW_PROJECT name another project, so it is not this project's."""
    _install(tmp_path)
    path = _plist(project)
    other = str(tmp_path / "other-project")
    agent = plistlib.loads(path.read_bytes())
    agent["WorkingDirectory"] = other
    agent["EnvironmentVariables"]["KEW_PROJECT"] = other
    path.write_bytes(plistlib.dumps(agent))
    _unrecord(cfg)
    before, made = path.read_bytes(), len(calls(launchd))

    assert json.loads(_status("--json").output)["agent"] is None
    result = runner.invoke(app, ["drive", "uninstall"])

    assert "no agent is installed" in result.output
    assert path.read_bytes() == before
    assert not any(c.startswith("bootout") for c in calls(launchd)[made:])


@pytest.mark.parametrize(
    "spoil",
    [
        lambda a: a["EnvironmentVariables"].pop("KEW_DRIVE_AGENT"),
        lambda a: a.update(KeepAlive=True),
        lambda a: a.update(StandardErrorPath="/tmp/elsewhere.log"),  # nosec B108
    ],
    ids=["unmarked", "launch-policy", "log-paths"],
)
def test_an_unrecorded_plist_is_claimed_only_with_kews_whole_identity(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, spoil: Any
) -> None:
    _install(tmp_path)
    path = _plist(project)
    agent = plistlib.loads(path.read_bytes())
    spoil(agent)
    path.write_bytes(plistlib.dumps(agent))
    _unrecord(cfg)

    assert json.loads(_status("--json").output)["agent"] is None
    assert "no agent is installed" in runner.invoke(app, ["drive", "uninstall"]).output
    assert path.exists()


# Holds a bootout until the test releases it: the reinstall is live, its
# switch recorded and the new plist in place, mid-way through the switch.
_PAUSING_LAUNCHCTL = """#!/bin/sh
here="$(dirname "$0")"
if [ "$1" = bootout ]; then
  touch "$here/paused"
  n=0
  while [ ! -f "$here/release" ] && [ "$n" -lt 1200 ]; do sleep 0.05; n=$((n+1)); done
fi
exec "$here/launchctl.real" "$@"
"""


def _wait_for_file(path: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 60
    while not path.exists():
        assert proc.poll() is None, "the reinstall exited before its bootout"
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_status_and_uninstall_leave_a_reinstall_in_progress_alone(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """A switch recorded by an install still running is not a killed one:
    status reads past it and uninstall refuses, so the install finishes with
    the new agent loaded and recorded rather than rolled back under it."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    shim = launchd / "launchctl"
    shim.rename(launchd / "launchctl.real")
    shim.write_text(_PAUSING_LAUNCHCTL)
    shim.chmod(0o755)
    proc = subprocess.Popen(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)], cwd=project
    )
    try:
        _wait_for_file(launchd / "paused", proc)

        status = _status("--json")
        removed = runner.invoke(app, ["drive", "uninstall"])

        assert (
            "changing it right now" in json.loads(status.output)["agent"]["recovered"]
        )
        assert removed.exit_code == 2
        assert "another kew process" in removed.output
        assert pending_switch(agent_label(project)) is not None
    finally:
        (launchd / "release").touch()
        assert proc.wait(timeout=120) == 0

    after = _plist(project).read_bytes()
    assert after != before and b"<string>77</string>" in after
    assert (launchd / f"loaded.{agent_label(project)}").exists()
    assert pending_switch(agent_label(project)) is None
    with _ledger(cfg) as store:
        assert recorded_agent(store) == InstalledAgent(
            agent_label(project), _plist(project)
        )
    assert "recovered" not in json.loads(_status("--json").output)["agent"]


def _refuse_bootstrap_at_77(launchd: Path, refusal: str) -> None:
    """launchd registers a plist written for ``--interval 77`` and then fails
    its bootstrap with ``refusal`` (shell run after the registration)."""
    shim = launchd / "launchctl"
    shim.write_text(
        shim.read_text().replace(
            "  bootstrap)\n",
            "  bootstrap)\n    if grep -q '<string>77</string>' \"$3\"; then\n"
            '      cp "$3" "$here/loaded.$(basename "$3" .plist)"\n'
            f"      {refusal}; fi\n",
        )
    )


@pytest.mark.parametrize(
    ("refusal", "said"),
    [
        ('echo "Bootstrap failed: 5" >&2; exit 5', "Bootstrap failed: 5"),
        # `exec` so the timeout's kill reaches the sleeping process itself.
        ("exec sleep 30", "timed out after 1s"),
    ],
    ids=["non-zero", "timeout"],
)
def test_bootstrap_side_effect_then_failure_restores_previous_loaded_definition(
    launchd: Path,
    project: Path,
    cfg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    said: str,
) -> None:
    """A bootstrap that registers the replacement and then fails (or runs out
    of time) leaves launchd holding the replacement; the rollback boots it out
    before the previous plist goes back, so launchd holds the previous job's
    own definition, not the replacement's over the previous file."""
    from kew import drive_agent

    _install(tmp_path)
    before = _plist(project).read_bytes()
    _refuse_bootstrap_at_77(launchd, refusal)
    monkeypatch.setattr(drive_agent, "LAUNCHCTL_TIMEOUT_S", 1)

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert said in result.output
    assert "previous agent is restored and loaded" in result.output
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert drive_agent.pending_switch(agent_label(project)) is None
    assert not _plist(project).with_name(_plist(project).name + ".previous").exists()
    assert json.loads(_status("--json").output)["agent"]["state"] == "loaded"


def test_a_switch_whose_loaded_job_does_not_match_its_plist_is_rolled_back(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """A reinstall killed after a bootstrap that registered the replacement
    and failed: launchd holds a job, but not the previous plist's. The next
    kew process does not take "loaded" for "settled"; it puts the previous
    job back."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    _refuse_bootstrap_at_77(launchd, f"{_KILL_KEW}; exit 137")
    proc = subprocess.run(
        [sys.executable, "-c", _reinstall_code(launchd / "launchctl", tmp_path)],
        cwd=project,
        timeout=120,
        check=False,
    )
    assert proc.returncode == -9
    # The kill came after the switch was journalled as booted out; put the
    # previous bytes back as an interrupted rollback would, under the
    # replacement launchd still holds.
    _plist(project).write_bytes(before)
    assert pending_switch(agent_label(project)) is not None

    agent = json.loads(_status("--json").output)["agent"]

    assert "rolled back" in agent["recovered"]
    assert agent["state"] == "loaded"
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


def test_a_reinstall_carrying_an_empty_kew_variable_settles_and_status_leaves_it(
    launchd: Path, project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``launchctl print`` shows an empty value as ``NAME => `` with nothing
    after the arrow; the job launchd holds still matches its plist, so the
    reinstall settles and a later status stops nothing."""
    from kew.drive_agent import pending_switch

    monkeypatch.setenv("KEW_WORKTREE_BASE", "")
    _install(tmp_path)
    assert b"<key>KEW_WORKTREE_BASE</key>" in _plist(project).read_bytes()

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 0, result.output
    assert b"<string>77</string>" in _plist(project).read_bytes()
    assert pending_switch(agent_label(project)) is None
    before = len(calls(launchd))
    for _ in range(3):
        assert json.loads(_status("--json").output)["agent"]["state"] == "loaded"
    assert not any(c.startswith("bootout") for c in calls(launchd)[before:])


def test_concurrent_same_project_installs_with_distinct_ledgers_are_serialized(
    launchd: Path,
    project: Path,
    cfg: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plist and label follow the project alone, so the lock does too:
    an install pointed at another ledger (``KEW_LOG_DIR``) waits its turn
    rather than switching the same plist under a reinstall in progress."""
    _install(tmp_path)
    shim = launchd / "launchctl"
    shim.rename(launchd / "launchctl.real")
    shim.write_text(_PAUSING_LAUNCHCTL)
    shim.chmod(0o755)
    proc = subprocess.Popen(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)], cwd=project
    )
    try:
        _wait_for_file(launchd / "paused", proc)
        other = tmp_path / "other-ledger"
        other.mkdir(mode=0o700)
        SqliteRunStore(str(other / "dispatch.db")).close()
        monkeypatch.setenv("KEW_LOG_DIR", str(other))
        # A ledger registry that does not know the first ledger, so nothing
        # but the agent lock stands between the two installs.
        (tmp_path / "other-root").mkdir()
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path / "other-root")

        second = runner.invoke(app, _install_args(tmp_path, "--interval", "78"))

        assert second.exit_code == 2
        assert "another kew process" in second.output
    finally:
        (launchd / "release").touch()
        assert proc.wait(timeout=120) == 0

    after = _plist(project).read_bytes()
    assert b"<string>77</string>" in after
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == after


# Stands in for launchctl around the fake: during the verb named by an
# ``orphan.<verb>`` file it closes every descriptor it inherited, as a
# launchctl may, SIGKILLs the install calling it, and then blocks, as a
# launchctl stuck in launchd would, until ``release`` appears; then carries
# out the verb and leaves ``finished``. A held bootstrap loads the plist as it
# read it when called, as launchd does, not as the file reads on release.
_ORPHANING_LAUNCHCTL = """#!/bin/sh
here="$(dirname "$0")"
[ -f "$here/orphan.$1" ] || exec "$here/launchctl.real" "$@"
exec "@PYTHON@" -I "$here/orphaning" "$@"
"""
_ORPHANING = """import os, shutil, signal, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[0]).parent
args = sys.argv[1:]
(here / f"orphan.{args[0]}").unlink()
os.closerange(3, os.sysconf("SC_OPEN_MAX"))
if args[0] == "bootstrap":
    (here / "held").mkdir(exist_ok=True)
    args[2] = shutil.copy(args[2], here / "held")
os.kill(int((here / "kew.pid").read_text()), signal.SIGKILL)
(here / "paused").touch()
deadline = time.monotonic() + 60
while not (here / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.05)
rc = subprocess.run([str(here / "launchctl.real"), *args]).returncode
(here / "finished").touch()
sys.exit(rc)
"""


def _wait_until(path: Path) -> None:
    deadline = time.monotonic() + 60
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


@pytest.mark.parametrize(
    ("verb", "kept"), [("bootout", "previous"), ("bootstrap", "new")]
)
def test_an_orphaned_launchctl_cannot_act_after_a_concurrent_recovery(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, verb: str, kept: str
) -> None:
    """The install is SIGKILLed while its launchctl is blocked in launchd; the
    launchctl outlives it, holding none of the descriptors it inherited. The
    agent lock is still held until it exits, so a
    concurrent status leaves the switch alone rather than restoring the
    previous job under it, and once it exits the next kew process settles the
    switch against what launchd then holds: the agent is loaded with the
    definition the switch's plist names (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    shim = launchd / "launchctl"
    shim.rename(launchd / "launchctl.real")
    shim.write_text(_ORPHANING_LAUNCHCTL.replace("@PYTHON@", sys.executable))
    shim.chmod(0o755)
    (launchd / "orphaning").write_text(_ORPHANING)
    (launchd / f"orphan.{verb}").touch()
    proc = subprocess.Popen(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)], cwd=project
    )
    try:
        assert proc.wait(timeout=120) == -9
        _wait_until(launchd / "paused")

        during = _status("--json")

        assert (
            "changing it right now" in json.loads(during.output)["agent"]["recovered"]
        )
        assert pending_switch(agent_label(project)) is not None
    finally:
        (launchd / "release").touch()
    _wait_until(launchd / "finished")
    deadline = time.monotonic() + 60
    # The orphan exits right after it leaves `finished`; until then it still
    # holds the lock and the status says so.
    while "changing it right now" in json.dumps(
        json.loads(_status("--json").output)["agent"]
    ):
        assert time.monotonic() < deadline, "the agent lock was never released"
        time.sleep(0.05)

    agent = json.loads(_status("--json").output)["agent"]

    assert agent["state"] == "loaded"
    after = _plist(project).read_bytes()
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == after
    if kept == "previous":
        assert after == before
    else:
        assert after != before and b"<string>77</string>" in after
    assert pending_switch(agent_label(project)) is None


def test_a_reinstall_whose_bootstrap_loads_another_job_is_rolled_back(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """A bootstrap that exits 0 does not settle the switch: launchd must hold
    the new plist's own job first. One it does not is booted out and the
    previous agent put back (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    _load_at_77_instead(launchd, 'job["ProgramArguments"] = ["other"]')

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert "another program" in result.output
    assert "previous agent is restored and loaded" in result.output
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


def _load_at_77_instead(launchd: Path, edit: str) -> None:
    """launchd's bootstrap of a plist written for ``--interval 77`` exits 0
    but loads that plist's job changed by ``edit`` (Python over ``job``)."""
    (launchd / "stale-job").write_text(
        "import plistlib, sys\n"
        "with open(sys.argv[1], 'rb') as f:\n"
        "    job = plistlib.load(f)\n"
        f"{edit}\n"
        "with open(sys.argv[2], 'wb') as f:\n"
        "    plistlib.dump(job, f)\n"
    )
    _refuse_bootstrap_at_77(
        launchd,
        f'"{sys.executable}" -I "$here/stale-job" "$3" '
        '"$here/loaded.$(basename "$3" .plist)"; exit 0',
    )


@pytest.mark.parametrize(
    ("edit", "said"),
    [
        ('job["EnvironmentVariables"]["KEW_LOG_DIR"] = "/stale/ledger"', "environment"),
        ('job["WorkingDirectory"] = "/stale/project"', "working directory"),
        ('job["StandardOutPath"] = "/stale/drive.log"', "stdout path"),
        ('job["StandardErrorPath"] = "/stale/drive.log"', "stderr path"),
        ('job["RunAtLoad"] = False', "launch policy"),
        ('del job["KeepAlive"]', "launch policy"),
        ('job["KeepAlive"] = True', "launch policy"),
        ('job["KeepAlive"] = {"SuccessfulExit": True}', "launch policy"),
    ],
    ids=[
        "environment",
        "working-directory",
        "stdout-path",
        "stderr-path",
        "run-at-load",
        "keep-alive",
        "keep-alive-always",
        "keep-alive-on-success",
    ],
)
def test_a_reinstall_whose_job_loads_with_stale_settings_is_rolled_back(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, edit: str, said: str
) -> None:
    """launchd holding the new plist's program is not enough to settle the
    switch: a job with another environment, working directory, log path or
    launch policy — ``KeepAlive`` in its exact form, so an unconditional or
    on-success one is not kew's restart-on-failure — is not the new plist's,
    so the previous agent is put back (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    _load_at_77_instead(launchd, edit)

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert f"launchd holds another {said}" in result.output
    assert "previous agent is restored and loaded" in result.output
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


def test_an_orphaned_uninstall_bootout_without_a_launchagents_directory_keeps_the_lock(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """An uninstall whose LaunchAgents directory has gone still takes the
    agent lock, so when it is SIGKILLed while its bootout is blocked in
    launchd, an install cannot load a replacement the released bootout would
    then unload: the install is refused until the bootout exits, and the
    agent it loads afterwards stays loaded (#2930)."""
    _install(tmp_path)
    shutil.rmtree(Path.home() / "Library" / "LaunchAgents")
    shim = launchd / "launchctl"
    shim.rename(launchd / "launchctl.real")
    shim.write_text(_ORPHANING_LAUNCHCTL.replace("@PYTHON@", sys.executable))
    shim.chmod(0o755)
    (launchd / "orphaning").write_text(_ORPHANING)
    (launchd / "orphan.bootout").touch()
    proc = subprocess.Popen(
        [sys.executable, "-c", _kew_code(shim, tmp_path, ["drive", "uninstall"])],
        cwd=project,
    )
    try:
        assert proc.wait(timeout=120) == -9
        _wait_until(launchd / "paused")

        during = runner.invoke(app, _install_args(tmp_path))

        assert during.exit_code == 2
        assert "another kew process is installing or removing" in during.output
    finally:
        (launchd / "release").touch()
    _wait_until(launchd / "finished")
    deadline = time.monotonic() + 60
    # The orphan exits right after it leaves `finished`; until then it still
    # holds the lock and an install is refused.
    while (after := runner.invoke(app, _install_args(tmp_path))).exit_code != 0:
        assert "another kew process" in after.output, after.output
        assert time.monotonic() < deadline, "the agent lock was never released"
        time.sleep(0.05)

    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == (
        _plist(project).read_bytes()
    )
    assert json.loads(_status("--json").output)["agent"]["state"] == "loaded"


@pytest.mark.parametrize(
    ("edit", "said"),
    [
        ('job["ThrottleInterval"] = 5', "definition"),
        ('job["ProcessType"] = "Interactive"', "definition"),
    ],
    ids=["throttle-interval", "process-type"],
)
def test_a_reinstall_whose_job_differs_only_where_print_is_silent_is_rolled_back(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, edit: str, said: str
) -> None:
    """``launchctl print`` shows neither ``ThrottleInterval`` nor
    ``ProcessType``, but every definition kew writes carries the hash of the
    whole of it in its environment. A loaded job written with another value
    carries another hash, so it is not the new plist's and the previous agent
    is put back (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    # The stale job carries the stamp of its own definition, as kew writes it.
    _load_at_77_instead(
        launchd,
        f"{edit}\n"
        "from kew.drive_agent import DEFINITION_HASH, definition_hash\n"
        "job['EnvironmentVariables'][DEFINITION_HASH] = definition_hash(job)",
    )

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert f"launchd holds another {said}" in result.output
    assert "previous agent is restored and loaded" in result.output
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


# Stands in for launchctl around the fake: during the verb named by an
# ``interrupt.<verb>`` file it sends SIGINT to the install calling it alone (not
# its process group), then blocks until ``release`` appears and carries out the
# verb. Any launchctl call made while it is blocked is noted in ``early``.
_INTERRUPTED_LAUNCHCTL = """#!/bin/sh
here="$(dirname "$0")"
if [ -f "$here/paused" ] && [ ! -f "$here/finished" ]; then
  echo "$*" >> "$here/early"
fi
[ -f "$here/interrupt.$1" ] || exec "$here/launchctl.real" "$@"
exec "@PYTHON@" -I "$here/interrupted" "$@"
"""
_INTERRUPTED = """import os, signal, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[0]).parent
args = sys.argv[1:]
(here / f"interrupt.{args[0]}").unlink()
os.kill(int((here / "kew.pid").read_text()), signal.SIGINT)
(here / "paused").touch()
deadline = time.monotonic() + 60
while not (here / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.05)
rc = subprocess.run([str(here / "launchctl.real"), *args]).returncode
(here / "finished").touch()
sys.exit(rc)
"""


def _interrupt_at(launchd: Path, verb: str) -> Path:
    """Wrap the fake launchctl so ``verb`` interrupts its install and blocks
    (:data:`_INTERRUPTED_LAUNCHCTL`); the wrapper's path."""
    shim = launchd / "launchctl"
    shim.rename(launchd / "launchctl.real")
    shim.write_text(_INTERRUPTED_LAUNCHCTL.replace("@PYTHON@", sys.executable))
    shim.chmod(0o755)
    (launchd / "interrupted").write_text(_INTERRUPTED)
    (launchd / f"interrupt.{verb}").touch()
    return shim


@pytest.mark.parametrize("verb", ["bootout", "bootstrap"])
def test_an_interrupted_reinstall_rolls_back_only_after_its_launchctl_exits(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path, verb: str
) -> None:
    """A SIGINT to the install while its launchctl is blocked does not kill
    the keeper holding the agent lock: the install waits for that launchctl
    to exit before it rolls back, so nothing it restores is undone by the
    launchctl afterwards, and the previous agent is left loaded (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    shim = _interrupt_at(launchd, verb)
    proc = subprocess.Popen(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)], cwd=project
    )
    try:
        _wait_until(launchd / "paused")
        # Long enough for an install that does not wait to start rolling back
        # (``time.sleep`` is a no-op under the conftest).
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1)

        assert not (launchd / "early").exists()
    finally:
        (launchd / "release").touch()
    assert proc.wait(timeout=120) != 0

    assert not (launchd / "early").exists()
    assert _plist(project).read_bytes() == before
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


def test_a_second_interrupt_leaves_the_reinstall_to_the_next_kew_process(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """A second SIGINT while the install waits for its launchctl ends the
    install without rolling back: it says launchctl is still running, the
    journal stays, and the keeper keeps the agent lock until launchctl exits.
    Then the next kew process settles the switch (#2930)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    shim = _interrupt_at(launchd, "bootout")
    proc = subprocess.Popen(
        [sys.executable, "-c", _reinstall_code(shim, tmp_path)],
        cwd=project,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until(launchd / "paused")
        # Long enough for the first interrupt to reach the wait for launchctl.
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.5)
        os.kill(proc.pid, signal.SIGINT)
        _, err = proc.communicate(timeout=120)

        assert proc.returncode == 2, err
        assert "launchctl bootout is still running" in " ".join(err.split())
        assert not (launchd / "early").exists()
        assert b"<string>77</string>" in _plist(project).read_bytes()
        assert pending_switch(agent_label(project)) is not None
        during = json.loads(_status("--json").output)["agent"]
        assert "changing it right now" in during["recovered"]
    finally:
        (launchd / "release").touch()
    _wait_until(launchd / "finished")
    deadline = time.monotonic() + 60
    while "changing it right now" in json.dumps(
        json.loads(_status("--json").output)["agent"]
    ):
        assert time.monotonic() < deadline, "the agent lock was never released"
        time.sleep(0.05)

    agent = json.loads(_status("--json").output)["agent"]

    assert agent["state"] == "loaded"
    after = _plist(project).read_bytes()
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == after
    assert pending_switch(agent_label(project)) is None


def test_a_reinstall_refuses_to_stop_a_loaded_agent_whose_plist_is_gone(
    launchd: Path, project: Path, cfg: Any, tmp_path: Path
) -> None:
    """With the recorded agent loaded and its plist deleted there is nothing
    to put back if the replacement fails to load, so the reinstall refuses
    and the old job keeps running (#2955)."""
    _install(tmp_path)
    loaded = launchd / f"loaded.{agent_label(project)}"
    running = loaded.read_bytes()
    _plist(project).unlink()
    _refuse_bootstrap_at_77(launchd, 'echo "Bootstrap failed: 5" >&2; exit 5')

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code != 0
    assert "kew drive uninstall" in result.output
    assert loaded.read_bytes() == running
    assert not _plist(project).exists()

    # The fix the refusal names works.
    assert runner.invoke(app, ["drive", "uninstall"]).exit_code == 0
    assert not loaded.exists()
    _install(tmp_path)
    assert loaded.exists()


def test_a_reinstall_leaves_the_agent_alone_when_launchd_does_not_answer(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """Only launchd's not-found answer means nothing is loaded: a `print`
    that fails any other way stops the reinstall before it writes or boots
    out anything, so a loaded agent whose plist is gone keeps running
    (#2955)."""
    _install(tmp_path)
    loaded = launchd / f"loaded.{agent_label(project)}"
    running = loaded.read_bytes()
    _plist(project).unlink()
    shim = launchd / "launchctl"
    working = shim.read_text()
    shim.write_text(
        working.replace(
            "  print)\n", '  print)\n    echo "launchd is busy" >&2; exit 5\n', 1
        )
    )
    before = len(calls(launchd))

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code != 0
    assert "rerun once launchd answers" in result.output
    assert loaded.read_bytes() == running
    assert not _plist(project).exists()
    assert [c.split()[0] for c in calls(launchd)[before:]] == ["print"]


def test_a_reinstall_whose_unloaded_agent_lost_its_plist_writes_a_new_one(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """Nothing runs to be lost, so a recorded agent that is neither loaded nor
    on disk is simply installed again."""
    _install(tmp_path)
    (launchd / f"loaded.{agent_label(project)}").unlink()
    _plist(project).unlink()

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 0, result.output
    assert b"<string>77</string>" in _plist(project).read_bytes()
    assert json.loads(_status("--json").output)["agent"]["state"] == "loaded"


def _refuse_every_bootstrap(launchd: Path, refusal: str) -> None:
    """launchd fails every bootstrap from now on with ``refusal`` (shell run
    where the fake would register the plist)."""
    shim = launchd / "launchctl"
    shim.write_text(
        shim.read_text().replace("  bootstrap)\n", f"  bootstrap)\n    {refusal}\n", 1)
    )


@pytest.mark.parametrize(
    "refusal",
    [
        'echo "Bootstrap failed: 5" >&2; exit 5',
        # `exec` so the timeout's kill reaches the sleeping process itself.
        "exec sleep 30",
    ],
    ids=["non-zero", "timeout"],
)
def test_a_restore_that_does_not_load_keeps_its_recovery_files(
    launchd: Path,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """When the replacement and the saved plist both fail to load, the journal
    and the saved plist stay, so the next `kew drive status` retries the
    restore and the previous agent runs again (#2955)."""
    from kew import drive_agent

    _install(tmp_path)
    before = _plist(project).read_bytes()
    shim = launchd / "launchctl"
    working = shim.read_text()
    _refuse_every_bootstrap(launchd, refusal)
    monkeypatch.setattr(drive_agent, "LAUNCHCTL_TIMEOUT_S", 1)

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert "did not load either" in result.output
    assert _plist(project).read_bytes() == before
    assert not (launchd / f"loaded.{agent_label(project)}").exists()
    assert drive_agent.pending_switch(agent_label(project)) is not None
    saved = _plist(project).with_name(_plist(project).name + ".previous")
    assert saved.read_bytes() == before

    shim.write_text(working)
    agent = json.loads(_status("--json").output)["agent"]

    assert "rolled back" in agent["recovered"]
    assert agent["state"] == "loaded"
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert drive_agent.pending_switch(agent_label(project)) is None
    assert not saved.exists()


def test_a_second_install_leaves_an_unrestored_agent_for_status_to_restore(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """A failed reinstall leaves the previous agent unrestored; a second
    install whose retry fails too stops without touching the journal or the
    saved plist, so a later `status` still puts the original agent back
    (#2955)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    saved = _plist(project).with_name(_plist(project).name + ".previous")
    shim = launchd / "launchctl"
    working = shim.read_text()
    _refuse_every_bootstrap(launchd, 'echo "Bootstrap failed: 5" >&2; exit 5')

    assert (
        runner.invoke(app, _install_args(tmp_path, "--interval", "77")).exit_code == 2
    )
    journal = pending_switch(agent_label(project))
    assert journal is not None
    recorded = journal.journal.read_bytes()

    second = runner.invoke(app, _install_args(tmp_path, "--interval", "88"))

    assert second.exit_code == 2
    assert "previous agent is not yet restored" in second.output
    assert "`kew drive status` retries the restore" in second.output
    assert journal.journal.read_bytes() == recorded
    assert saved.read_bytes() == before
    assert _plist(project).read_bytes() == before

    shim.write_text(working)
    agent = json.loads(_status("--json").output)["agent"]

    assert agent["state"] == "loaded"
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None
    assert not saved.exists()


def test_a_restore_that_registers_then_fails_counts_as_loaded(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """Both bootstraps register their plist and then exit non-zero: launchd
    holds the previous job's own definition after the restore, so it is
    settled as restored rather than booted out (#2955)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    _refuse_every_bootstrap(
        launchd,
        'cp "$3" "$here/loaded.$(basename "$3" .plist)"; '
        'echo "Bootstrap failed: 5" >&2; exit 5',
    )

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert "previous agent is restored and loaded" in result.output
    assert (launchd / f"loaded.{agent_label(project)}").read_bytes() == before
    assert pending_switch(agent_label(project)) is None


def test_a_restore_launchd_does_not_confirm_stays_loaded_with_its_recovery_files(
    launchd: Path, project: Path, tmp_path: Path
) -> None:
    """Both bootstraps register their plist and exit non-zero, then the
    `print` verifying the restore fails without launchd's not-found answer:
    an unanswered query is not a failed restore, so the restored job stays
    loaded, the recovery files stay, and the next `status` settles it
    (#2955)."""
    from kew.drive_agent import pending_switch

    _install(tmp_path)
    before = _plist(project).read_bytes()
    loaded = launchd / f"loaded.{agent_label(project)}"
    saved = _plist(project).with_name(_plist(project).name + ".previous")
    shim = launchd / "launchctl"
    working = shim.read_text()
    # The second bootstrap (the restore) leaves the next `print` unanswered.
    shim.write_text(
        working.replace(
            "  print)\n",
            '  print)\n    if [ -f "$here/busy" ]; then rm "$here/busy"; '
            'echo "launchd is busy" >&2; exit 5; fi\n',
            1,
        )
    )
    _refuse_every_bootstrap(
        launchd,
        'cp "$3" "$here/loaded.$(basename "$3" .plist)"; '
        'if [ -f "$here/once" ]; then touch "$here/busy"; fi; touch "$here/once"; '
        'echo "Bootstrap failed: 5" >&2; exit 5',
    )

    result = runner.invoke(app, _install_args(tmp_path, "--interval", "77"))

    assert result.exit_code == 2
    assert "`status` retries" in result.output
    assert loaded.read_bytes() == before
    assert _plist(project).read_bytes() == before
    assert pending_switch(agent_label(project)) is not None
    assert saved.read_bytes() == before

    shim.write_text(working)
    agent = json.loads(_status("--json").output)["agent"]

    assert "previous agent" in agent["recovered"]
    assert agent["state"] == "loaded"
    assert loaded.read_bytes() == before
    assert pending_switch(agent_label(project)) is None
    assert not saved.exists()
