"""A contained run that dies on a signal records why, and reads as ``resource``.

The sandboxed epic dispatch never named a signal exit: an abort (134) or an
OOM kill (137) landed on the run row with no category and no reason, and a
confirmed OOM was a terminal ``resource_cap`` (#2908, #2925). The container
client here is a real process — a fake ``docker`` that prints one runtime
line and dies — and the run row, its failure event and its run report are
what the tests read (#3191). Everything else is faked at a process boundary
(``gh``, ``docker``, ``ps``) or runs for real against a local repository.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kew import dispatch_core, subprocess_utils
from kew.config import DispatchConfig, SandboxConfig
from kew.dispatch_core import execute_dispatch
from kew.inference_transport import ResolvedInferenceTransport
from kew.run_report_sink import render_report
from kew.run_store import SqliteRunStore
from kew.runners import get_runner
from kew.sandbox import STARTUP_ACK_PREFIX, ContainerHandle, SandboxDecision

_P = "kew.dispatch_core"
_RUNTIME_LINE = "FATAL ERROR: Reached heap limit Allocation failed"
_PROTOCOL_LINE = '{"type":"system","subtype":"init","session_id":"s-1"}'
_NONCE = "n3191"


@pytest.fixture(autouse=True)
def _contained_launch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env a contained claude launch authenticates with, and its tools."""
    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-key")
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    for tool in ("gh", "docker", "ps"):  # answered by ``_Boundary``, never run
        monkeypatch.setitem(TOOL_PATHS, tool, f"/nonexistent/{tool}")


@pytest.fixture
def operator_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local-only operator repository with ``main``; dispatch runs from it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
    ):
        subprocess.run(["git", *argv], cwd=repo, check=True)  # noqa: S603, S607
    monkeypatch.chdir(repo)
    return repo


class _Boundary:
    """``subprocess`` as ``kew.subprocess_utils`` sees it: ``gh``, ``docker``
    and ``ps`` answered here, every other argv (``git``) run for real."""

    def __init__(self, oom: bool) -> None:
        self._oom = oom

    def __getattr__(self, name: str) -> object:
        return getattr(subprocess, name)

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = [Path(str(cmd[0])).name, *map(str, cmd[1:])]
        if argv[0] not in ("gh", "docker", "ps"):
            return subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]  # noqa: S603
        return subprocess.CompletedProcess(cmd, *self._answer(argv))

    def _answer(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["ps", "-o", "lstart="]:
            return 0, "Sun Sep 20 02:00:00 2026\n", ""
        if argv[:2] == ["docker", "events"]:
            return 0, "container oom current-id\n" if self._oom else "", ""
        if argv[0] == "gh":  # reachable, and every label write lands
            return 0, "{}" if argv[1] == "api" else "", ""
        return 0, "[]", ""


def _fake_docker(tmp_path: Path, exit_code: int) -> Path:
    """A ``docker`` whose ``start --attach`` prints, then dies with *exit_code*."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"echo '{STARTUP_ACK_PREFIX}{_NONCE}'\n"
        f"echo '{_PROTOCOL_LINE}'\n"
        f"echo '{_RUNTIME_LINE}' >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker


def _attached_client(docker: Path, cidfile: Path) -> ContainerHandle:
    proc = subprocess.Popen(  # noqa: S603 - the test's own fake client
        # Through ``sh``: a tmp directory may be mounted noexec.
        ["/bin/sh", str(docker), "start", "--attach", "kew-3191"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return ContainerHandle(proc, "kew-3191", cidfile=cidfile, startup_nonce=_NONCE)


def _dispatch(
    tmp_path: Path, store: SqliteRunStore, exit_code: int, *, oom: bool
) -> str:
    """Run one contained dispatch through ``execute_dispatch``; its run id."""
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    resolved = MagicMock()
    resolved.runner.name = "claude-code"
    resolved.model = "claude-sonnet-4-6"
    transport = ResolvedInferenceTransport(
        mode="direct",
        requested_model=resolved.model,
        model=resolved.model,
        config_sha256="cfg",
        endpoint="https://api.anthropic.com",
    )
    cidfile = tmp_path / "dispatch.cid"
    cidfile.write_text("a" * 64, encoding="utf-8")
    handle = _attached_client(_fake_docker(tmp_path, exit_code), cidfile)
    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image=SandboxConfig().image,
    )
    # The host's free space is the filesystem's, not this test's: a nearly
    # full tmp would refuse the launch before the fake client ever ran.
    roomy = shutil._ntuple_diskusage(2**40, 0, 2**40)  # type: ignore[attr-defined]
    with (
        patch.object(subprocess_utils, "subprocess", _Boundary(oom)),
        patch("kew.sandbox.shutil.disk_usage", return_value=roomy),
        patch(f"{_P}.resolve_runner", return_value=resolved),
        patch(f"{_P}.resolve_effective_sandbox", return_value=decision),
        patch(f"{_P}.check_runtime_memory_capacity"),
        patch(f"{_P}.check_agent_image_staleness", return_value=None),
        patch(f"{_P}._detect_sandbox_runtime", return_value="/usr/bin/docker"),
        patch(
            "kew.dispatch_sandbox._resolve_launch_inputs",
            return_value=(
                "sha256:" + "a" * 64,
                transport,
                resolved.model,
                None,
                get_runner("claude-code"),
            ),
        ),
        patch("kew.dispatch_sandbox._capture_launch_evidence_or_finalize"),
        patch.object(dispatch_core, "_launch_container", return_value=handle),
    ):
        execute_dispatch(
            allow_unrecorded=True,
            issue={
                "number": 3191,
                "title": "Classify a contained signal exit",
                "body": "",
                "labels": [{"name": "ready"}],
            },
            config=config,
            store=store,
        )
    [row] = store.list_by_issue(3191)
    return row.run_id


@pytest.mark.parametrize(
    ("exit_code", "oom", "named"),
    [
        (134, False, "killed by SIGABRT (exit 134)"),
        (137, True, "killed by SIGKILL (exit 137, out of memory)"),
    ],
)
def test_a_contained_signal_exit_records_resource_and_its_last_runtime_line(
    tmp_path: Path, operator_repo: Path, exit_code: int, oom: bool, named: str
) -> None:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run_id = _dispatch(tmp_path, store, exit_code, oom=oom)
        row = store.get(run_id)
        [failure] = store.list_audit_events(
            event="sandbox_execution_failed", run_id=run_id
        )
        evidence = [f"audit:{e.id}:{e.event}" for e in store.list_audit_events()]
        assert row is not None
        report = render_report(row, None, evidence)

    assert row.status == "failure"
    assert row.exit_code == exit_code
    assert row.error_category == "resource"
    reason = failure.payload["reason"]
    assert reason == f"{named}; last output: {_RUNTIME_LINE}"
    assert '"type"' not in reason  # the protocol line is not runtime output
    assert failure.payload["error_category"] == "resource"
    assert 'error_category: "resource"' in report
    assert f"audit:{failure.id}:sandbox_execution_failed" in report
