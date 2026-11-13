"""The reviewer container's stderr rides its own pipe (#2053, residual of #2047).

The runtime client merges the container's stderr into the pipe that carries its
stdout, so a stray non-event line — a Python runtime warning from the reviewed
code, a daemon notice — landed in the stream the governed review reads its
verdict from, where any line that is not a typed event fails the round (#2047,
deliberately fail-closed). The fix is a SECOND pipe: only the agent's own
stdout reaches the review capture.

The client double here models the real pipe behaviour: with
``stderr=subprocess.STDOUT`` it merges its stderr lines into stdout, the way
``docker run`` does, and with ``stderr=subprocess.PIPE`` it keeps them apart.
So these tests fail against a merged launch and pass against a separated one.

The two runtime facts the merged stream used to carry must survive the split:
the daemon's CPU-cap refusal (which drives the one relaunch) and the
entrypoint's pre-acknowledgement ``ERROR:`` diagnostic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support import driver_fakes as _fx
from support.container_launch import ContainerLaunchCapture

import kew.dispatch_core as _dispatch_core
from kew import sandbox
from kew.dispatch_sandbox import _separates_container_stderr
from kew.graph.driver.review_launcher import sandbox_reviewer_launcher
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, SqliteRunStore

store = _fx.store

ITEM = "1"
HEAD = "d" * 40
BEGIN = "<<<KEW-REVIEW-VERDICT-BEGIN>>>"
END = "<<<KEW-REVIEW-VERDICT-END>>>"

READY: dict[str, Any] = {
    "verdict": "READY",
    "head_sha": HEAD,
    "findings": [],
    "acceptance_evidence": [{"command": "pytest", "output_digest": "sha256:a"}],
}

# A real stray line: the reviewed code's test run emits a runtime warning, and
# the client folds the container's stderr wherever its pipes point.
WARNING_LINE = (
    "sys:1: ResourceWarning: unclosed file <_io.TextIOWrapper name='x.txt'>\n"
)

CPU_CAP_REJECTION = (
    "docker: Error response from daemon: Range of CPUs is from "
    "0.01 to 2.00, as there are only 2 CPUs available.\n"
)

ENTRYPOINT_REFUSAL = "ERROR: kew agent executable missing or not executable\n"


@pytest.fixture(autouse=True)
def _reviewer_image_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


def _result(message: str) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": message}
    )


def _reviewer_stdout() -> list[str]:
    """One genuine reviewer run: an init notice and one READY result event."""
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n",
        _result(f"Done.\n{BEGIN}\n{json.dumps(READY)}\n{END}\n") + "\n",
    ]


class _Pipe:
    """One client pipe, read the way the host reads it."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.closed = False

    def readline(self, limit: int | None = None) -> str:
        return self._lines.pop(0) if self._lines else ""

    def close(self) -> None:
        self.closed = True


def _client(
    stdout_lines: list[str],
    stderr_lines: list[str],
    exit_code: int,
    *,
    stderr: int | None,
) -> Any:
    """A runtime client whose pipes behave the way the real client's do.

    ``stderr=subprocess.STDOUT`` is the merge this issue is about: the
    container's stderr arrives interleaved on the stdout pipe, ahead of the
    agent's own output. A separated launch gets two pipes.
    """
    if stderr == subprocess.STDOUT:
        stdout_lines = [*stderr_lines, *stdout_lines]
        stderr_lines = []
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = _Pipe(stdout_lines)
    proc.stderr = None if stderr == subprocess.STDOUT else _Pipe(stderr_lines)
    proc.returncode = exit_code
    proc.poll.return_value = exit_code
    proc.wait.return_value = exit_code
    return proc


def _plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the checkout-quota cap out of the way of the pipe under test."""
    monkeypatch.setattr(
        sandbox.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1 << 40),
    )


def _ctx(tmp_path: Path, config: Any, run_id: str) -> ReviewerLaunchContext:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return ReviewerLaunchContext(
        item_id=ITEM,
        run_id=run_id,
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha=HEAD,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )


def _review_config(tmp_path: Path) -> Any:
    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    return config


def _review_through_real_launch(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    clients: list[Any],
    run_id: str,
) -> tuple[CollectedSidecar, list[bool]]:
    """Drive the production launcher down the REAL ``launch_container`` path.

    Returns the collected verdict and the ``separate_stderr`` argument each
    launch was asked for, so the plumbing from the run kind is asserted too.
    """
    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl
    import kew.review_environment as _re

    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    argv = sandbox.build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        slug="42",
        network="allowlist",
        runner="claude-code",
    )
    no_container = subprocess.CompletedProcess([], 0, "", "")
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "popen",
        lambda *a, **k: clients.pop(0)(k.get("stderr", subprocess.STDOUT)),
    )
    monkeypatch.setattr(sandbox.subprocess_utils, "run", lambda *a, **k: no_container)
    monkeypatch.setattr(sandbox, "_daemon_cpu_count", lambda _runtime: 2)
    monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: "docker")
    monkeypatch.setattr(_re, "provision_review_environment", lambda *a, **k: True)

    separated: list[bool] = []

    def _launch(*_a: Any, **kwargs: Any) -> Any:
        separated.append(bool(kwargs.get("separate_stderr")))
        return sandbox.launch_container(
            argv, separate_stderr=bool(kwargs.get("separate_stderr"))
        )

    config = _review_config(tmp_path)
    with patch.object(_dc, "_launch_container", side_effect=_launch):
        collected = sandbox_reviewer_launcher(config, store)(
            _ctx(tmp_path, config, run_id)
        )
    return collected, separated


def test_a_stray_stderr_line_does_not_fail_a_genuine_round(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = [
        lambda stderr: _client(_reviewer_stdout(), [WARNING_LINE], 0, stderr=stderr)
    ]

    collected, separated = _review_through_real_launch(
        tmp_path, store, monkeypatch, clients, "rev-stderr-noise"
    )

    assert separated == [True], "the review launch must ask for a second pipe"
    assert collected.parsed.unparseable_reason is None
    assert collected.parsed.verdict == "READY"


def test_the_capture_counts_no_unparsed_line_for_separated_stderr(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture beside the log is the evidence: no unparsed line at all."""
    clients = [
        lambda stderr: _client(
            _reviewer_stdout(), [WARNING_LINE, WARNING_LINE], 0, stderr=stderr
        )
    ]

    _review_through_real_launch(
        tmp_path, store, monkeypatch, clients, "rev-stderr-capture"
    )

    capture = json.loads(
        (tmp_path / "logs" / "review-rev-stderr-capture.review-result.json").read_text()
    )
    assert capture["unparsed_lines"] == 0
    assert capture["split_lines"] == 0
    assert capture["terminal_is_result"] is True


def test_a_cpu_cap_refusal_on_the_separated_pipe_still_retries(
    tmp_path: Path,
    store: SqliteRunStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The daemon's refusal now arrives on the second pipe, not the stream.

    It must still drive the one relaunch and still reach the operator, or a
    host whose CPU cap exceeds its core count could never run a review.
    """
    clients = [
        lambda stderr: _client([], [CPU_CAP_REJECTION], 125, stderr=stderr),
        lambda stderr: _client(_reviewer_stdout(), [], 0, stderr=stderr),
    ]

    collected, _ = _review_through_real_launch(
        tmp_path, store, monkeypatch, clients, "rev-cpu-retry"
    )

    assert clients == [], "the CPU-cap retry did not launch"
    assert collected.parsed.verdict == "READY"
    assert "Range of CPUs is from 0.01 to 2.00" in capsys.readouterr().err


def test_an_entrypoint_refusal_on_the_separated_pipe_still_names_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-acknowledgement ``ERROR:`` line still names the startup refusal."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    argv = sandbox.build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        slug="42",
        network="allowlist",
        runner="claude-code",
    )
    no_container = subprocess.CompletedProcess([], 0, "", "")
    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "popen",
        lambda *a, **k: _client([], [ENTRYPOINT_REFUSAL], 78, stderr=k.get("stderr")),
    )
    monkeypatch.setattr(sandbox.subprocess_utils, "run", lambda *a, **k: no_container)

    handle = sandbox.launch_container(argv, separate_stderr=True)
    assert list(handle) == []
    assert handle.wait() == 78

    assert "executable missing" in handle.startup_refusal_reason
    assert handle.stderr_lines == 1


def test_a_merged_pipe_is_the_default_for_every_other_run() -> None:
    """Only the governed review needs the split; dispatch keeps one pipe."""
    assert _separates_container_stderr(ADVERSARIAL_REVIEW_RUN_KIND) is True
    assert _separates_container_stderr("implementation") is False
    assert _separates_container_stderr("ci_fix") is False


def test_launch_container_asks_for_one_pipe_unless_stderr_is_separated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    argv = sandbox.build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        slug="42",
        network="allowlist",
        runner="claude-code",
    )
    asked: list[int | None] = []

    def _popen(*_a: Any, **kwargs: Any) -> Any:
        asked.append(kwargs.get("stderr", subprocess.STDOUT))
        return _client([], [], 0, stderr=kwargs.get("stderr", subprocess.STDOUT))

    _plenty_of_disk(monkeypatch)
    monkeypatch.setattr(sandbox.subprocess_utils, "popen", _popen)
    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
    )

    sandbox.launch_container(argv)
    sandbox.launch_container(argv, separate_stderr=True)

    assert asked == [subprocess.STDOUT, subprocess.PIPE]


@pytest.mark.parametrize("sidecar", [False, True])
def test_a_review_gets_its_second_pipe_under_a_sidecar_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: bool
) -> None:
    """Both launch branches must forward the split, not just the native one.

    A switchyard sidecar starts the worker its supervisor prepared, so a review
    running under one takes the other branch of ``_launch_streaming_container``.
    A branch that drops ``separate_stderr`` leaves that review on the merged
    pipe, where a stray stderr line fails a genuine round exactly as before.
    """
    from kew import dispatch_sandbox
    from kew.dispatch_admission import PendingAdmission
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import get_runner

    config = _fx._config()
    transport = resolve_inference_transport(
        config, get_runner("claude-code"), "claude-sonnet-4-6"
    )
    monkeypatch.setattr(
        dispatch_sandbox,
        "prepare_sidecar_worker",
        lambda *a, **kw: SimpleNamespace(
            argv=["docker", "start", "--attach", "worker"],
            runtime="docker",
            name="worker",
            container_id="c" * 64,
            checkout_dir=tmp_path,
            startup_nonce=None,
        ),
    )
    launch = ContainerLaunchCapture()

    with patch.object(_dispatch_core, "_launch_container", side_effect=launch):
        dispatch_sandbox._launch_streaming_container(
            config,
            None,
            run_id="rev-sidecar-split",
            log_file=tmp_path / "rev-sidecar-split.log",
            image="sha256:" + "b" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="2053",
            model=transport.model,
            transport=transport,
            sidecar_supervisor=object() if sidecar else None,
            sidecar_ready=None,
            admission=PendingAdmission(store=None, run_id="rev-sidecar-split"),
            separate_stderr=True,
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        )

    assert launch.separated_stderr == [True]
