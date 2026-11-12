"""The merge verify runs its cheap gates and held-out before the suite (#3212).

A verify runs as a gate job (the static gates, the containment test, then the
held-out phase) and, only once kew judged that job clean, the suite job (the
pytest phases and everything after them). A red static gate, or a held-out
failure no **Behavior changes** line declares, ends the verification before
any pytest phase starts, and the result names the phase it stopped in.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.fake_gcloud import launchable
from support.held_out_log import opened, report_line
from support.verify_switches import environ_without_switches

from kew import merge_verify
from kew.config import DispatchConfig, VerifyConfig
from kew.hosts import HostProvider, HostTimeout, Job, JobResult, LocalProvider
from kew.hosts.lock import host_lock, host_lock_held
from kew.merge_verify import PHASE_MARK, GateOpening, run_verify

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

#: The base test the candidate's src breaks.
_PINNED = "tests/test_pinned.py::test_pinned_behaviour"

#: A gate job's log whose held-out phase reports *_PINNED* failing.
_GATE_HELD_OUT_RED = f"{PHASE_MARK} static-gates\n{PHASE_MARK} held-out\n" + opened(
    f"{report_line(_PINNED)}kew-held-out: exit 1 seconds 2\n"
    f"{report_line()}kew-held-out: exit 0 seconds 1\n"
)

#: A gate job's log whose held-out phase ran both its runs clean.
_GATE_CLEAN = f"{PHASE_MARK} static-gates\n{PHASE_MARK} held-out\n" + opened(
    "kew-held-out: exit 0 seconds 1\n" * 2
)

_STAGE = "KEW_VERIFY_STAGE"


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", message], repo)


def _project(tmp_path: Path, verify_sh: str | None = None) -> Path:
    """A repo whose HEAD is one candidate commit ahead of ``main``.

    *verify_sh* is a stand-in ``kew/scripts/verify.sh`` the base carries, so
    a real job script has a gate to run; the pinned-tool preflight is a
    stand-in that passes.
    """
    repo = tmp_path / "repo"
    (repo / "kew").mkdir(parents=True)
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    if verify_sh is not None:
        (repo / "kew" / "scripts").mkdir()
        (repo / "kew" / "scripts" / "verify.sh").write_text(verify_sh, "utf-8")
        (repo / "scripts").mkdir()
        (repo / "scripts" / "scan-agent-image.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
    _commit(repo, "base")
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "candidate")
    return repo


def _config(tmp_path: Path, **verify: Any) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(**verify)
    return config


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A HOME the stand-in gate writes what it was asked to run into."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    yield path


#: A stand-in gate that records which stage it was asked for, and where.
_RECORDING_GATE = (
    f'#!/bin/sh\necho "${{{_STAGE}:-all}} $(pwd)" >> "$HOME/kew-stages"\nFAIL\nexit 0\n'
)


def _stages(home: Path) -> list[list[str]]:
    path = home / "kew-stages"
    if not path.exists():
        return []
    return [line.split(" ", 1) for line in path.read_text("utf-8").splitlines()]


class TestTheJobsTheHostRuns:
    """The composed scripts really run, on a local host, in this order."""

    def test_a_red_static_gate_starts_no_pytest_phase(
        self, tmp_path: Path, home: Path
    ) -> None:
        gate = _RECORDING_GATE.replace("FAIL", f'[ "${_STAGE}" = gates ] && exit 3')
        repo = _project(tmp_path, gate)

        _candidate, result = run_verify(
            _config(tmp_path, host="local"), store=None, repo_root=repo
        )

        assert result.outcome == "failed" and result.exit_code == 3
        assert [stage for stage, _where in _stages(home)] == ["gates"]
        assert f"{PHASE_MARK} static-gates" in result.log
        assert f"{PHASE_MARK} suite" not in result.log
        assert result.error is not None
        assert "stopped in phase static-gates" in result.error
        assert "no pytest phase was started" in result.error

    def test_a_clean_gate_job_is_followed_by_the_suite_in_the_same_checkout(
        self, tmp_path: Path, home: Path
    ) -> None:
        repo = _project(tmp_path, _RECORDING_GATE.replace("FAIL", ""))

        _candidate, result = run_verify(
            _config(tmp_path, host="local"), store=None, repo_root=repo
        )

        assert result.outcome == "passed", result.error
        stages = _stages(home)
        assert [stage for stage, _where in stages] == ["gates", "suite"]
        assert stages[0][1] == stages[1][1], "the suite ran in another checkout"
        log = result.log
        assert log.index(f"{PHASE_MARK} static-gates") < log.index(
            f"{PHASE_MARK} held-out"
        )
        assert log.index(f"{PHASE_MARK} held-out") < log.index(f"{PHASE_MARK} suite")


class TestTheHeldOutVerdictComesFirst:
    """kew judges the gate job's held-out section before it starts the suite."""

    def _verify(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pr_body: str | None,
    ) -> tuple[list[str], merge_verify.VerifyResult]:
        repo = _project(tmp_path)
        ran: list[str] = []

        def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
            ran.append(job.command)
            if f"{_STAGE}=gates" in job.command:
                return 0, _GATE_HELD_OUT_RED
            return 0, f"{PHASE_MARK} suite\nsuite passed\n"

        monkeypatch.setattr(LocalProvider, "_execute", _execute)
        _candidate, result = run_verify(
            _config(tmp_path, host="local"),
            store=None,
            repo_root=repo,
            pr_body=pr_body,
        )
        return ran, result

    def test_an_undeclared_failure_fails_before_any_pytest_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran, result = self._verify(tmp_path, monkeypatch, pr_body=None)

        assert len(ran) == 1 and f"{_STAGE}=gates" in ran[0]
        assert result.outcome == "failed"
        assert result.held_out is not None
        assert result.held_out.undeclared == (_PINNED,)
        assert f"{PHASE_MARK} suite" not in result.log
        assert result.error is not None
        assert "stopped in phase held-out" in result.error
        assert "no pytest phase was started" in result.error

    def test_the_same_failure_declared_goes_on_to_the_suite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = f"**Behavior changes**\n\n- {_PINNED} — the pin changed on purpose\n"

        ran, result = self._verify(tmp_path, monkeypatch, pr_body=body)

        assert len(ran) == 2 and f"{_STAGE}=suite" in ran[1]
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.declared == (_PINNED,)
        assert "suite passed" in result.log

    def test_held_out_lines_the_suite_prints_are_never_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the gate job's log carries the held-out section: a section the
        suite job's output opens is not read, so it can neither pass nor fail
        the phase."""
        repo = _project(tmp_path)

        def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
            if f"{_STAGE}=gates" in job.command:
                return 0, _GATE_CLEAN
            return 0, _GATE_HELD_OUT_RED

        monkeypatch.setattr(LocalProvider, "_execute", _execute)
        _candidate, result = run_verify(
            _config(tmp_path, host="local"), store=None, repo_root=repo
        )

        assert result.outcome == "passed", result.error
        assert result.held_out is not None and result.held_out.failed == ()


class _PoolHost(HostProvider):
    """One host of a pool: answers each job from *answers* by a substring."""

    name = "gcp"

    def __init__(self, index: int, lock_dir: Path, answers: dict[str, tuple[int, str]]):
        super().__init__(lock_dir=lock_dir)
        self.index = index
        self.answers = answers
        self.ran: list[str] = []

    @property
    def address(self) -> str:
        return f"verify-{self.index}"

    @property
    def lock_key(self) -> str:
        return f"pool-{self.index}"

    def _ensure_up(self) -> None:
        pass

    def _ship(self, bundle_path: Path) -> None:
        pass

    def _release(self) -> None:
        pass

    def _execute(self, job: Job) -> tuple[int, str]:
        self.ran.append(job.command)
        return next(
            (answer for match, answer in self.answers.items() if match in job.command),
            (0, ""),
        )


def test_a_sharded_verify_starts_no_shard_behind_a_red_held_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every host gets the bundle, but no shard or single-run job runs until
    the first host's gate job is judged — and this one never passes."""
    repo = _project(tmp_path)
    answers = {f"{_STAGE}=gates": (0, _GATE_HELD_OUT_RED)}
    pool = [_PoolHost(index, tmp_path / "locks", answers) for index in (1, 2, 3)]
    monkeypatch.setattr(merge_verify, "provider_pool", lambda *_a, **_k: pool)

    _candidate, result = run_verify(
        _config(tmp_path), store=None, repo_root=repo, shards=3
    )

    assert result.outcome == "failed"
    assert [len(host.ran) for host in pool] == [1, 0, 0]
    assert f"{_STAGE}=gates" in pool[0].ran[0]
    assert result.held_out is not None
    assert result.held_out.undeclared == (_PINNED,)
    assert result.error is not None and "stopped in phase held-out" in result.error


def test_a_sharded_verify_whose_first_host_is_busy_ends_failed_and_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first host, which runs the gate job, is held by another job: the
    verify ends at once as a failure, no host runs anything, and every host
    the other sessions claimed is free again. It never waits on a gate job
    that cannot run."""
    repo = _project(tmp_path)
    locks = tmp_path / "locks"
    answers = {f"{_STAGE}=gates": (0, _GATE_CLEAN)}
    pool = [_PoolHost(index, locks, answers) for index in (1, 2, 3)]
    monkeypatch.setattr(merge_verify, "provider_pool", lambda *_a, **_k: pool)
    answered: list[Any] = []

    def verify() -> None:
        answered.append(
            run_verify(_config(tmp_path), store=None, repo_root=repo, shards=3)[1]
        )

    with host_lock(locks, pool[0].lock_key):
        worker = threading.Thread(target=verify, daemon=True)
        worker.start()
        worker.join(timeout=60)
        assert not worker.is_alive(), "the verify waited on a gate job that never ran"

    [result] = answered
    assert result.outcome != "passed"
    assert result.error is not None
    assert [len(host.ran) for host in pool] == [0, 0, 0]
    assert not any(host_lock_held(locks, host.lock_key) for host in pool)


def _waiting(opening: GateOpening) -> tuple[threading.Thread, list[object]]:
    """A suite host's worker waiting on *opening*; what it ends with lands in
    the list: the verdict, or the error it raised."""
    ended: list[object] = []

    def wait() -> None:
        try:
            ended.append(opening.opens())
        except HostTimeout as exc:
            ended.append(exc)

    worker = threading.Thread(target=wait, daemon=True)
    worker.start()
    return worker, ended


def test_a_suite_host_stops_waiting_once_the_gate_job_cannot_still_answer() -> None:
    """Past the gate job's ceiling and the grace for its host to come up, no
    verdict is coming: the wait ends in an error, so the session fails."""
    gate = Job(name="gate", command="true", timeout_seconds=0.05)
    worker, ended = _waiting(GateOpening(gate, lambda result: result, grace=0.0))

    worker.join(timeout=30)

    assert not worker.is_alive(), "the wait ignored its bound"
    [error] = ended
    assert isinstance(error, HostTimeout)
    assert "no verdict from the gate job" in str(error)


def test_an_unbounded_gate_job_is_waited_on_until_its_session_closes_it() -> None:
    """A gate job with no ceiling is waited on as long as it runs; its session
    closing the opening with no verdict lets the suite host go, unopened."""
    gate = Job(name="gate", command="true", timeout_seconds=None)
    opening = GateOpening(gate, lambda result: result, grace=0.0)
    worker, ended = _waiting(opening)

    worker.join(timeout=0.5)
    assert worker.is_alive() and ended == []
    opening.close()
    worker.join(timeout=30)

    assert ended == [False]


class _SlowGateHost(_PoolHost):
    """A pool host whose gate job reports that it took 1000 seconds."""

    def run(self, job: Job) -> JobResult:
        answer = super().run(job)
        if f"{_STAGE}=gates" in job.command:
            return replace(answer, seconds=1000.0)
        return answer


def test_a_sharded_verifys_seconds_count_the_gate_job_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool's wall clock already spans the gate job, which ran first on
    the first host: the verify's seconds are that clock, not it plus the gate
    job's own time again."""
    repo = _project(tmp_path)
    answers = {f"{_STAGE}=gates": (0, _GATE_CLEAN)}
    pool = [_SlowGateHost(index, tmp_path / "locks", answers) for index in (1, 2, 3)]
    monkeypatch.setattr(merge_verify, "provider_pool", lambda *_a, **_k: pool)

    _candidate, result = run_verify(
        _config(tmp_path), store=None, repo_root=repo, shards=3
    )

    assert len(pool[0].ran) >= 1 and f"{_STAGE}=gates" in pool[0].ran[0]
    assert result.seconds < 1000.0


_KEW_DIR = Path(__file__).resolve().parent.parent
_VERIFY_SH = _KEW_DIR / "scripts" / "verify.sh"


class TestVerifyShStages:
    """The real ``scripts/verify.sh``, its tools stubbed, split by stage."""

    @staticmethod
    def _run_verify_sh(tmp_path: Path, **env: str) -> tuple[int, str]:
        root = tmp_path if launchable(tmp_path) else Path(os.environ["HOME"])
        bin_dir = Path(root) / f"stage-stub-{os.getpid()}-{id(tmp_path)}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "uv").write_text(
            "#!/bin/sh\n"
            'echo "uv $*" >> "$KEW_STUB_LOG"\n'
            'case "$*" in *"-n auto"*) exit 86;; esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        for tool in ("uvx", "gitleaks", "npm"):
            (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for entry in bin_dir.iterdir():
            entry.chmod(0o755)
        log = tmp_path / "stub.log"
        log.write_text("", encoding="utf-8")
        environment = environ_without_switches()
        environment.update(
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            KEW_STUB_LOG=str(log),
        )
        environment.update(env)
        completed = subprocess.run(
            ["bash", str(_VERIFY_SH)],
            cwd=_KEW_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        return completed.returncode, log.read_text(encoding="utf-8")

    def test_the_gates_stage_runs_the_static_gates_and_containment_only(
        self, tmp_path: Path
    ) -> None:
        code, seen = self._run_verify_sh(tmp_path, **{_STAGE: "gates"})
        assert code == 0, seen
        assert "uv run ruff check ." in seen
        assert "uv run pytest tests/test_sandbox_containment.py -v" in seen
        assert "-n auto" not in seen and "-m serial" not in seen

    def test_the_suite_stage_skips_them_and_reaches_the_pytest_phases(
        self, tmp_path: Path
    ) -> None:
        code, seen = self._run_verify_sh(tmp_path, **{_STAGE: "suite"})
        assert code == 86, f"verify.sh did not reach the xdist phase: {seen}"
        assert "ruff" not in seen and "test_sandbox_containment" not in seen

    @pytest.mark.parametrize("stage", [None, "gate"], ids=["unset", "typo"])
    def test_without_an_exact_stage_every_gate_runs_in_order(
        self, tmp_path: Path, stage: str | None
    ) -> None:
        env = {} if stage is None else {_STAGE: stage}
        code, seen = self._run_verify_sh(tmp_path, **env)
        assert code == 86, f"verify.sh did not reach the xdist phase: {seen}"
        assert seen.index("ruff check") < seen.index("test_sandbox_containment")
        assert seen.index("test_sandbox_containment") < seen.index("-n auto")
