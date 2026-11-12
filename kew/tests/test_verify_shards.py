"""``kew verify --shards N`` — the parallel phase across N hosts (#2380).

The gcp pool is driven entirely through the fake ``gcloud`` on PATH, which
answers each job by a substring of its command (``job_responses``): that is how
one failing shard, one silent shard and a set of counts that do not add up are
asserted without a cloud account and without running the gate.

What the jobs *are* is asserted on the composed scripts, and the one real
pytest run here is deliberate: the union check reads pytest's own collect-only
summary, so a test runs a real ``--collect-only`` pass and holds kew's pattern
to what pytest actually printed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
from support.fake_gcloud import fake_gcloud_on_path, launchable
from support.fake_gcloud import read_state as _read_state
from support.fake_gcloud import recorded_calls as _calls
from support.fake_gcloud import write_state as _write_state
from support.held_out_log import opened
from support.verify_switches import SUITE_JOB_SWITCHES, environ_without_switches
from typer.testing import CliRunner

from kew import verify_executed
from kew.cli import app
from kew.config import DispatchConfig, GcpHostConfig, HostsConfig, VerifyConfig
from kew.hosts import GcpProvider, HostNotProvisioned, LocalProvider, provider_pool
from kew.hosts.gcp import IDLE_STOP_UNIT
from kew.merge_verify import (
    COLLECTED_PATTERN,
    PREFLIGHT_COMMAND,
    SINGLE_JOB,
    SUITE_ELSEWHERE,
    SUITE_ENV,
    UI_CHECK_COMMAND,
    VERIFY_EVENT,
    XDIST_WORKERS,
    XDIST_WORKERS_ENV,
    ShardReport,
    VerifyCandidate,
    VerifyRefused,
    VerifyResult,
    collect_lines,
    count_check,
    executed_lines,
    read_executed,
    read_selected,
    run_verify,
    shard_job_script,
    shard_sessions,
    sharded_result,
    single_job_script,
    summary_line,
    terminate_unwinds,
)
from kew.verify_executed import REPORT_FORMAT

runner = CliRunner()

KEW_DIR = Path(__file__).resolve().parent.parent
VERIFY_SH = KEW_DIR / "scripts" / "verify.sh"
CI_YML = KEW_DIR.parent / ".github" / "workflows" / "ci.yml"

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

#: The three instances every pooled test addresses.
POOL = ["verify-1", "verify-2", "verify-3"]


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _project(tmp_path: Path) -> Path:
    """A project repo whose HEAD is one candidate commit ahead of ``main``."""
    repo = tmp_path
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "kew").mkdir(exist_ok=True)
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", "base"], repo)
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", "candidate"], repo)
    return repo


def _candidate(**overrides: Any) -> VerifyCandidate:
    fields: dict[str, Any] = {
        "ref": "HEAD",
        "ref_sha": "a" * 40,
        "base_ref": "origin/main",
        "base_sha": "b" * 40,
        "frontend_changed": False,
    }
    fields.update(overrides)
    return VerifyCandidate(**fields)


def _config(tmp_path: Path, *, instances: list[str] | None = None) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(host="gcp")
    config.hosts = HostsConfig(
        gcp=GcpHostConfig(
            project="proj",
            zone="us-central1-a",
            instances=POOL if instances is None else instances,
        )
    )
    return config


def _report(
    job: str,
    *,
    instance: str = "verify-1",
    exit_code: int | None = 0,
    selected: int | None = 10,
    executed: int | None | str = "selected",
    error: str | None = None,
) -> ShardReport:
    """A report whose run executed what it selected unless told otherwise."""
    if executed == "selected":
        executed = None if job == SINGLE_JOB else selected
    return ShardReport(
        job=job,
        instance=instance,
        result=VerifyResult(
            host="gcp", exit_code=exit_code, seconds=1.0, log="", error=error
        ),
        selected=selected,
        executed=executed if isinstance(executed, int) else None,
    )


#: A job's held-out run, reported clean (#2485), in the phase's own section.
_HELD_OUT_RAN = opened("kew-held-out: exit 0 seconds 1\n")

#: The gate job's two held-out runs, parallel and serial, reported clean: the
#: held-out phase runs once, in the gate job, before any shard (#3212).
_GATE_PASSED = opened(
    "kew-held-out: exit 0 seconds 1\nkew-held-out: exit 0 seconds 1\n"
)


def _collect_log(selected: int, total: int = 18900) -> str:
    """What a collect-only pass prints, in the shape pytest prints it."""
    return (
        f"{selected}/{total} tests collected ({total - selected} deselected) "
        "in 20.02s\n"
    )


def _shard_log(selected: int, executed: int | None = None) -> str:
    """A shard's whole log: its count, its run, then the executed check's line."""
    ran = selected if executed is None else executed
    return (
        f"{_collect_log(selected)}... run output ...\n"
        f"{REPORT_FORMAT.format(executed=ran, collected=selected)}\n"
        f"{_HELD_OUT_RAN}"
    )


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    fake = tmp_path / "fake"
    fake.mkdir()
    with fake_gcloud_on_path(fake, monkeypatch) as state:
        yield state


@contextlib.contextmanager
def _recording_unwind(events: list[object]) -> Iterator[None]:
    """:func:`terminate_unwinds`, recording each ``SIGTERM`` it handles.

    The record is ``("handled", <native id of the thread the handler ran
    on>)``, appended to *events* before the handler unwinds.
    """
    with terminate_unwinds():
        unwind = signal.getsignal(signal.SIGTERM)
        assert callable(unwind)

        def record(signum: int, frame: FrameType | None) -> None:
            events.append(("handled", threading.get_native_id()))
            unwind(signum, frame)

        signal.signal(signal.SIGTERM, record)
        yield


def _ssh_commands(state: Path) -> list[tuple[str, str]]:
    """``(instance, command)`` for every ssh call the fake was handed."""
    return [
        (call[2], call[call.index("--command") + 1])
        for call in _calls(state)
        if call[:2] == ["compute", "ssh"] and "--command" in call
    ]


#: The provisioning recipe's own marker. Every job now runs under a root-owned
#: systemd scope (#2876), so a privileged command alone no longer means "the
#: recipe, not a job".
_RECIPE_MARKER = "KEW_JOB_PATH"


def _is_job(command: str) -> bool:
    return "KEW_HOST_EXIT" in command and _RECIPE_MARKER not in command


def _jobs(state: Path) -> list[str]:
    """Every job command the fake was asked to run, in order."""
    return [command for _instance, command in _ssh_commands(state) if _is_job(command)]


def _instances(state: Path) -> dict[str, str]:
    """Which instance ran which job, keyed by the job's shard spec."""
    placed = {}
    for instance, command in _ssh_commands(state):
        if not _is_job(command):
            continue
        match = re.search(r"--shard=(\d+/\d+)", command)
        placed[match.group(1) if match else SINGLE_JOB] = instance
    return placed


def _pooled_responses(**counts: int) -> list[dict[str, object]]:
    """One passing answer per job, each reporting the count it selected.

    Keyed ``s1``/``s2``/``s3`` for the shards and ``single`` for the job that
    runs the collection the others are summed against. The gate job, which
    the first host runs before any of them, answers last.
    """
    responses: list[dict[str, object]] = [
        {"match": f"--shard={index}/3", "output": _shard_log(counts[f"s{index}"])}
        for index in (1, 2, 3)
    ]
    responses.append(
        {
            "match": "KEW_VERIFY_SUITE=elsewhere",
            "output": _collect_log(counts["single"]),
        }
    )
    responses.append({"match": "KEW_VERIFY_STAGE=gates", "output": _GATE_PASSED})
    return responses


class TestJobScripts:
    """What each host is actually asked to run."""

    def test_a_shard_runs_the_parallel_phase_and_nothing_that_runs_once(self) -> None:
        script = shard_job_script(2, 3, _candidate())
        assert 'uv run pytest -n auto -m "not serial" --shard=2/3' in script
        assert "scripts/verify.sh" not in script
        assert "-m serial" not in script
        assert "ui-check" not in script

    def test_a_shard_counts_its_own_selection_before_running_it(self) -> None:
        script = shard_job_script(1, 3, _candidate())
        collect = next(line for line in script.splitlines() if "--collect-only" in line)
        assert "--shard=1/3" in collect and '-m "not serial"' in collect
        assert script.index(collect) < script.index("-n auto")
        # The ids never reach the log that comes back: one line per selected
        # test would be megabytes of node ids per shard.
        assert ">" in collect
        assert "tests collected" in script

    def test_a_shard_checks_its_run_executed_what_it_collected(self) -> None:
        """#2629: the run writes junit, a stale one is removed first, and the
        operator's check — shipped as source, not read off the candidate —
        runs after the run whatever the run's own status."""
        script = shard_job_script(2, 3, _candidate())
        run = next(line for line in script.splitlines() if "-n auto" in line)
        assert "--junitxml=" in run and "|| run_status=$?" in run
        assert script.index("rm -f ../.kew-verify-junit.xml") < script.index(run)
        source = Path(verify_executed.__file__).read_text(encoding="utf-8")
        assert source.rstrip("\n") in script
        assert "python -I -" in script
        assert script.index(run) < script.index(source.rstrip("\n"))
        assert script.rstrip().endswith('exit "$run_status"')

    def test_a_shard_runs_under_the_same_worker_cap_as_the_phase_it_slices(
        self,
    ) -> None:
        cap = f'export {XDIST_WORKERS_ENV}="${{{XDIST_WORKERS_ENV}:-{XDIST_WORKERS}}}"'
        script = shard_job_script(1, 2, _candidate())
        assert cap in script
        assert script.index(cap) < script.index("-n auto")

    @pytest.mark.policy
    def test_the_cap_is_the_one_ci_and_verify_sh_apply(self) -> None:
        """Three places name this number; a shard is comparable only if they
        agree, and an N=1 reading against an N=3 one is the whole measurement."""
        import yaml

        ci = yaml.safe_load(CI_YML.read_text())
        assert str(ci["jobs"]["test"]["env"][XDIST_WORKERS_ENV]) == str(XDIST_WORKERS)
        assert (
            f'export {XDIST_WORKERS_ENV}="${{{XDIST_WORKERS_ENV}:-{XDIST_WORKERS}}}"'
            in VERIFY_SH.read_text()
        )

    def test_the_single_run_job_runs_the_gate_the_serial_phase_and_the_count(
        self,
    ) -> None:
        script = single_job_script(_candidate(), images=[])
        assert f"{SUITE_ENV}={SUITE_ELSEWHERE} bash scripts/verify.sh" in script
        assert "uv run pytest -m serial" in script
        collect = next(line for line in script.splitlines() if "--collect-only" in line)
        assert "--shard=" not in collect, "the union check needs one whole collection"
        assert script.index(collect) < script.index("scripts/verify.sh")
        # Cheapest first: the pinned-tool preflight is seconds and the gate it
        # guards is the last step of a twelve-minute run (#2462).
        assert script.index(PREFLIGHT_COMMAND) < script.index(collect)

    def test_the_single_run_job_runs_ui_check_only_when_the_console_moved(self) -> None:
        assert "ui-check" not in single_job_script(_candidate(), images=[])
        moved = single_job_script(_candidate(frontend_changed=True), images=[])
        # The held-out phase ran in the gate job, before any shard (#3212).
        assert "kew-held-out: begin" not in moved
        assert moved.index("uv run pytest -m serial") < moved.index(UI_CHECK_COMMAND)

    def test_the_single_run_job_builds_a_missing_pinned_image(self) -> None:
        script = single_job_script(_candidate(), images=[("codex", "kew-codex:1")])
        assert "docker image inspect kew-codex:1" in script
        assert "kew sandbox build --runner codex" in script

    @pytest.mark.policy
    def test_verify_sh_runs_the_two_phases_unless_told_they_are_elsewhere(
        self,
    ) -> None:
        """The switch is opt-in by exact value: a typo runs the whole gate."""
        text = VERIFY_SH.read_text()
        assert f'verify_suite="${{{SUITE_ENV}:-all}}"' in text
        guard = f'if [ "$verify_suite" != "{SUITE_ELSEWHERE}" ]; then'
        assert guard in text
        after = text[text.index(guard) :]
        phase = after[: after.index("\nfi")]
        assert 'uv run pytest -n auto -m "not serial" --cov=src/kew' in phase
        assert "--cov-fail-under" in phase, (
            "the coverage floor must travel with the phases it reads: a sharded "
            "run has no joined data for it"
        )


class TestSuiteSwitch:
    """verify.sh really skips the phases, and really runs them by default."""

    @staticmethod
    def _stub_bin(tmp_path: Path) -> Path:
        root = tmp_path if launchable(tmp_path) else Path(os.environ["HOME"])
        bin_dir = Path(root) / f"stub-{os.getpid()}-{id(tmp_path)}"
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
        # The agent-image scan's first docker call is logged and refused, so a
        # run that reaches the scan says so and never builds an image.
        (bin_dir / "docker").write_text(
            '#!/bin/sh\necho "docker $*" >> "$KEW_STUB_LOG"\nexit 1\n',
            encoding="utf-8",
        )
        for entry in bin_dir.iterdir():
            entry.chmod(0o755)
        return bin_dir

    def _run_verify_sh(self, tmp_path: Path, **env: str) -> tuple[int, str]:
        completed, log = self._verify_sh(tmp_path, **env)
        return completed.returncode, log

    def _verify_sh(
        self, tmp_path: Path, **env: str
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        """The stubbed verify.sh run itself, and every stubbed call it made."""
        bin_dir = self._stub_bin(tmp_path)
        log = tmp_path / "stub.log"
        log.write_text("", encoding="utf-8")
        environment = environ_without_switches()
        environment.update(
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            KEW_STUB_LOG=str(log),
        )
        environment.update(env)
        completed = subprocess.run(
            ["bash", str(VERIFY_SH)],
            cwd=KEW_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        return completed, log.read_text(encoding="utf-8")

    def test_the_phases_run_when_nothing_says_otherwise(self, tmp_path: Path) -> None:
        code, seen = self._run_verify_sh(tmp_path)
        assert code == 86, f"verify.sh did not reach the stubbed xdist phase: {seen}"

    # The suite-job case also carries, in the ambient environment, the
    # image-scan skip a suite job sets when no image input changed; the run
    # below drops it with every other switch and passes its own.
    @pytest.mark.parametrize(
        "ambient",
        [{}, {**SUITE_JOB_SWITCHES, "KEW_VERIFY_IMAGE_SCAN": "skip"}],
        ids=["clean", "inside-a-suite-job"],
    )
    def test_elsewhere_skips_both_phases_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ambient: dict[str, str],
    ) -> None:
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        # environ_without_switches() drops an ambient image-scan skip, so the
        # skip that keeps this run off the real scan is passed here.
        completed, seen = self._verify_sh(
            tmp_path, KEW_VERIFY_SUITE=SUITE_ELSEWHERE, KEW_VERIFY_IMAGE_SCAN="skip"
        )
        assert "-n auto" not in seen
        assert "pytest -m serial" not in seen
        assert "ruff check" in seen, "the static gates still ran"
        # Read off verify.sh itself, so it holds on a host whose scan would
        # stop at its trivy check before any docker call.
        assert "kew-phase: image_scan start" not in completed.stdout, (
            "the agent-image scan ran"
        )
        assert "KEW_VERIFY_IMAGE_SCAN=skip" in completed.stderr
        assert "docker " not in seen, "the agent-image scan ran"

    def test_a_typo_runs_the_whole_gate_rather_than_dropping_the_suite(
        self, tmp_path: Path
    ) -> None:
        code, seen = self._run_verify_sh(tmp_path, KEW_VERIFY_SUITE="elsewher")
        assert code == 86, f"a misspelt switch silently dropped the suite: {seen}"


class TestTheCountPattern:
    """The union check reads pytest's own number, so pin it to pytest's."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("6300/18900 tests collected (12600 deselected) in 20.02s", 6300),
            ("18900 tests collected in 20.02s", 18900),
            ("1 test collected in 0.01s", None),
            ("collected 18900 items / 12600 deselected / 6300 selected", None),
            ("", None),
        ],
    )
    def test_the_selection_is_read_off_either_shape(
        self, line: str, expected: int | None
    ) -> None:
        assert read_selected(f"a log\n{line}\nmore log\n") == expected

    def test_the_first_count_wins_because_the_collect_pass_runs_first(self) -> None:
        log = f"{_collect_log(100)}\n{_collect_log(999)}"
        assert read_selected(log) == 100

    def test_a_real_collect_only_pass_prints_what_kew_reads(self) -> None:
        """pytest's wording is the contract here; this is the test that breaks
        if a pytest upgrade changes it, instead of a sharded verify failing on
        a real VM with an unreadable count."""
        completed = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:randomly",
                "--shard=1/2",
                str(Path(__file__).name),
            ],
            cwd=KEW_DIR / "tests",
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout[-2000:]
        selected = read_selected(completed.stdout)
        assert selected is not None, completed.stdout[-2000:]
        ids = [
            line
            for line in completed.stdout.splitlines()
            if re.match(r"^[^ ]+\.py::", line)
        ]
        assert selected == len(ids), COLLECTED_PATTERN.pattern


class TestTheExecutedCheck:
    """The check a shard runs after its run, run for real against a candidate
    whose conftest keeps a test at collect-only and drops it at run time."""

    _DESELECT_AT_RUN_TIME = (
        "def pytest_collection_modifyitems(config, items):\n"
        "    if config.option.collectonly:\n"
        "        return\n"
        "    kept = [item for item in items if 'hidden' not in item.name]\n"
        "    config.hook.pytest_deselected(\n"
        "        items=[item for item in items if item not in kept]\n"
        "    )\n"
        "    items[:] = kept\n"
    )

    @staticmethod
    def _shard(tmp_path: Path, conftest: str, tests: str) -> tuple[int, str]:
        """The shard's own collect and executed lines, with this interpreter
        standing in for ``uv run`` because the candidate here is no uv project."""
        checkout = tmp_path / "kew"
        (checkout / "tests").mkdir(parents=True)
        (checkout / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (checkout / "tests" / "conftest.py").write_text(conftest, encoding="utf-8")
        (checkout / "tests" / "test_some.py").write_text(tests, encoding="utf-8")
        script = "\n".join(
            [
                "set -eu",
                *collect_lines(None),
                *executed_lines("uv run pytest -p no:randomly -p no:cacheprovider"),
                'exit "$run_status"',
            ]
        ).replace("uv run pytest", f"{sys.executable} -m pytest")
        script = script.replace("uv run python", sys.executable)
        completed = subprocess.run(
            ["sh", "-c", script],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout + completed.stderr

    _TESTS = (
        "def test_seen():\n    pass\n\n"
        "class TestBox:\n"
        "    def test_hidden(self):\n        pass\n\n"
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_param(n):\n    pass\n"
    )

    def test_a_test_deselected_only_at_run_time_fails_the_shard(
        self, tmp_path: Path
    ) -> None:
        code, log = self._shard(tmp_path, self._DESELECT_AT_RUN_TIME, self._TESTS)
        assert code == 1, log
        assert read_selected(log) == 4, log
        assert read_executed(log) == 3, log
        assert "tests/test_some.py::TestBox::test_hidden" in log

    def test_an_honest_run_reports_every_collected_test_executed(
        self, tmp_path: Path
    ) -> None:
        code, log = self._shard(tmp_path, "", self._TESTS)
        assert code == 0, log
        assert read_selected(log) == 4 and read_executed(log) == 4, log

    def test_a_failing_run_keeps_its_own_status_and_is_still_counted(
        self, tmp_path: Path
    ) -> None:
        tests = "def test_red():\n    assert False\n\ndef test_green():\n    pass\n"
        code, log = self._shard(tmp_path, "", tests)
        assert code == 1, log
        assert read_executed(log) == 2, log

    def test_the_report_line_is_the_one_kew_reads(self) -> None:
        line = REPORT_FORMAT.format(executed=7, collected=9)
        assert read_executed(f"noise\n{line}\nmore\n") == 7

    def test_the_last_report_wins_because_the_check_runs_last(self) -> None:
        """A test can print a forged line into the run's output; the check's
        own line comes after it."""
        forged = REPORT_FORMAT.format(executed=9, collected=9)
        real = REPORT_FORMAT.format(executed=8, collected=9)
        assert read_executed(f"{forged}\n{real}\n") == 8
        assert read_executed("no check ran\n") is None


class TestCountCheck:
    """The union check: the shards' counts must be the whole collection."""

    def test_counts_that_sum_to_the_collection_pass(self) -> None:
        reports = [
            _report("1/2", selected=40),
            _report("2/2", selected=60),
            _report(SINGLE_JOB, selected=100),
        ]
        assert count_check(reports) == (100, None)

    def test_counts_that_do_not_sum_to_the_collection_fail_by_name(self) -> None:
        reports = [
            _report("1/2", selected=40),
            _report("2/2", selected=50),
            _report(SINGLE_JOB, selected=100),
        ]
        collected, error = count_check(reports)
        assert collected == 100
        assert error is not None
        assert "90" in error and "100" in error and "partition" in error

    def test_a_shard_with_no_count_is_named_not_read_as_zero(self) -> None:
        reports = [
            _report("1/2", selected=None),
            _report("2/2", selected=100),
            _report(SINGLE_JOB, selected=100),
        ]
        _, error = count_check(reports)
        assert error is not None and "1/2" in error

    def test_a_shard_that_ran_fewer_than_it_selected_fails_by_name(self) -> None:
        reports = [
            _report("1/2", selected=40, executed=39),
            _report("2/2", selected=60),
            _report(SINGLE_JOB, selected=100),
        ]
        collected, error = count_check(reports)
        assert collected == 100
        assert error is not None and "1/2 ran 39 of 40" in error

    def test_a_shard_with_no_executed_count_is_named(self) -> None:
        reports = [
            _report("1/2", selected=40, executed=None),
            _report("2/2", selected=60),
            _report(SINGLE_JOB, selected=100),
        ]
        _, error = count_check(reports)
        assert error is not None and "executed" in error and "1/2" in error

    def test_a_collection_that_never_reported_fails_the_check(self) -> None:
        reports = [_report("1/1", selected=100), _report(SINGLE_JOB, selected=None)]
        collected, error = count_check(reports)
        assert collected is None
        assert error is not None and SINGLE_JOB in error


class TestAggregateVerdict:
    """A sharded verification passes only when every part of it does."""

    @staticmethod
    def _result(reports: list[ShardReport]) -> VerifyResult:
        return sharded_result(reports, host="gcp", seconds=12.5)

    def test_every_job_green_and_the_counts_adding_up_passes(self) -> None:
        result = self._result(
            [_report("1/1", selected=100), _report(SINGLE_JOB, selected=100)]
        )
        assert result.exit_code == 0 and result.outcome == "passed"
        assert result.error is None and result.collected == 100

    def test_one_failing_shard_fails_the_verification_with_its_own_code(self) -> None:
        result = self._result(
            [
                _report("1/2", selected=50),
                _report("2/2", selected=50, exit_code=3),
                _report(SINGLE_JOB, selected=100),
            ]
        )
        assert result.exit_code == 3 and result.outcome == "failed"

    def test_one_shard_that_never_reported_is_not_a_skipped_one(self) -> None:
        result = self._result(
            [
                _report("1/2", selected=50),
                _report("2/2", exit_code=None, selected=None, error="host gone"),
                _report(SINGLE_JOB, selected=100),
            ]
        )
        assert result.exit_code is None
        assert result.outcome == "incomplete"
        assert result.error is not None and "2/2" in result.error

    def test_green_jobs_whose_counts_disagree_still_fail(self) -> None:
        result = self._result(
            [
                _report("1/2", selected=50),
                _report("2/2", selected=49),
                _report(SINGLE_JOB, selected=100),
            ]
        )
        assert result.exit_code == 1 and result.outcome == "failed"
        assert result.error is not None and "partition" in result.error

    def test_the_summary_line_names_the_shards_and_the_collection(self) -> None:
        result = self._result(
            [_report("1/1", selected=100), _report(SINGLE_JOB, selected=100)]
        )
        line = summary_line(_candidate(), result)
        assert "shards=1" in line and "collected=100" in line

    def test_an_unsharded_result_says_nothing_about_shards(self) -> None:
        line = summary_line(
            _candidate(), VerifyResult(host="local", exit_code=0, seconds=1.0, log="")
        )
        assert "shards=" not in line and "collected=" not in line

    def test_each_report_prints_its_code_count_and_seconds(self) -> None:
        line = _report("2/3", instance="verify-2", exit_code=7, selected=42).line
        assert "2/3" in line and "verify-2" in line
        assert "exit=7" in line and "selected=42" in line and "seconds=1.0" in line


class TestThePool:
    """Which hosts a sharded verification addresses."""

    def test_three_shards_address_the_first_three_of_the_pool(
        self, tmp_path: Path
    ) -> None:
        providers = provider_pool(_config(tmp_path), provider="gcp", count=3)
        assert [provider.address for provider in providers] == POOL
        assert len({provider.lock_key for provider in providers}) == 3

    def test_one_shard_is_the_host_an_unsharded_verify_already_used(
        self, tmp_path: Path
    ) -> None:
        (provider,) = provider_pool(_config(tmp_path), provider="gcp", count=1)
        assert isinstance(provider, GcpProvider)
        assert provider.address == "verify-1"

    def test_a_pool_shorter_than_the_shards_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path, instances=["verify-1", "verify-2"])
        with pytest.raises(HostNotProvisioned, match="2 instance"):
            provider_pool(config, provider="gcp", count=3)

    def test_the_local_host_is_refused_above_one_shard(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        with pytest.raises(HostNotProvisioned, match="one job at a time"):
            provider_pool(config, provider="local", count=2)
        (provider,) = provider_pool(config, provider="local", count=1)
        assert isinstance(provider, LocalProvider)

    def test_no_shards_at_all_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(HostNotProvisioned, match="at least one shard"):
            provider_pool(_config(tmp_path), provider="gcp", count=0)

    def test_a_pool_entry_is_one_instance_and_never_the_same_one_twice(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            GcpHostConfig(project="p", zone="z", instances=["a", "a"])
        with pytest.raises(ValueError, match="one instance"):
            GcpHostConfig(project="p", zone="z", instances=["a b"])
        with pytest.raises(ValueError, match="one instance"):
            GcpHostConfig(project="p", zone="z", instances=[" "])

    def test_the_instance_defaults_to_the_pools_first_host(self) -> None:
        settings = GcpHostConfig(project="p", zone="z", instances=["first", "second"])
        assert settings.instance == "first"
        assert settings.configured()
        assert settings.pool() == ("first", "second")

    def test_a_host_with_no_pool_is_a_pool_of_one(self) -> None:
        settings = GcpHostConfig(project="p", zone="z", instance="only")
        assert settings.pool() == ("only",)

    def test_the_first_host_runs_its_shard_and_then_the_single_run_job(
        self, tmp_path: Path
    ) -> None:
        providers = provider_pool(_config(tmp_path), provider="gcp", count=3)
        labelled = [("1/3", None), ("2/3", None), ("3/3", None), (SINGLE_JOB, None)]
        sessions = shard_sessions(providers, labelled)  # type: ignore[arg-type]
        assert [[label for label, _ in jobs] for _, jobs in sessions] == [
            ["1/3", SINGLE_JOB],
            ["2/3"],
            ["3/3"],
        ]


class TestShardedRunOnTheFakePool:
    """AC1 and AC2, end to end, against a fake ``gcloud`` and three hosts."""

    @staticmethod
    def _verify(
        tmp_path: Path, state: Path, *, shards: int = 3
    ) -> tuple[Any, VerifyResult]:
        from kew.run_store import SqliteRunStore

        project = _project(tmp_path / "repo")
        config = _config(tmp_path)
        # A gcp verify needs a ledger for any lease takeover (#2874).
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            return run_verify(
                config, store=store, shards=shards, repo_root=project, host="gcp"
            )

    def test_three_shards_and_one_single_run_job_run(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        _candidate_, result = self._verify(tmp_path, fake_gcloud)
        assert result.exit_code == 0, result.error
        assert result.outcome == "passed"
        specs = sorted(
            re.search(r"--shard=(\d+/3)", command).group(1)  # type: ignore[union-attr]
            for command in _jobs(fake_gcloud)
            if "--shard=" in command
        )
        assert specs == ["1/3", "2/3", "3/3"]
        gates = [job for job in _jobs(fake_gcloud) if "scripts/verify.sh" in job]
        # The gate job's static half and the single-run job's suite half: each
        # part of the gate that runs once ran once, the gate job first.
        assert len(gates) == 2, "the gate that runs once ran more than once"
        assert "KEW_VERIFY_STAGE=gates" in gates[0]
        assert "KEW_VERIFY_STAGE=suite" in gates[1]
        assert _instances(fake_gcloud) == {
            "1/3": "verify-1",
            "2/3": "verify-2",
            "3/3": "verify-3",
            SINGLE_JOB: "verify-1",
        }
        assert [report.job for report in result.shards] == [
            "1/3",
            "2/3",
            "3/3",
            SINGLE_JOB,
        ]
        assert [report.selected for report in result.shards] == [40, 30, 30, 100]

    def test_one_failing_shard_fails_the_verification(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        responses = _pooled_responses(s1=40, s2=30, s3=30, single=100)
        responses[1]["exit"] = 3
        _write_state(fake_gcloud, job_responses=responses)
        _candidate_, result = self._verify(tmp_path, fake_gcloud)
        assert result.exit_code == 3 and result.outcome == "failed"
        failed = [r for r in result.shards if r.result.exit_code != 0]
        assert [r.job for r in failed] == ["2/3"]
        assert failed[0].instance == "verify-2"
        # The other hosts still answered; a failure is not a lost report.
        assert all(r.result.exit_code == 0 for r in result.shards if r.job != "2/3")

    def test_one_missing_shard_fails_the_verification_never_skips_it(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        responses = _pooled_responses(s1=40, s2=30, s3=30, single=100)
        responses[2]["swallow"] = True
        _write_state(fake_gcloud, job_responses=responses)
        _candidate_, result = self._verify(tmp_path, fake_gcloud)
        assert result.exit_code is None
        assert result.outcome == "incomplete"
        silent = next(r for r in result.shards if r.job == "3/3")
        assert silent.result.exit_code is None and silent.selected is None
        assert result.error is not None and "3/3" in result.error

    def test_counts_that_do_not_sum_to_the_collection_fail_a_green_run(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=29, single=100),
        )
        _candidate_, result = self._verify(tmp_path, fake_gcloud)
        assert all(r.result.exit_code == 0 for r in result.shards)
        assert result.exit_code == 1 and result.outcome == "failed"
        assert result.error is not None and "99" in result.error

    def test_a_shard_that_skipped_a_collected_test_fails_a_green_run(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        responses = _pooled_responses(s1=40, s2=30, s3=30, single=100)
        responses[1]["output"] = _shard_log(30, executed=29)
        _write_state(fake_gcloud, job_responses=responses)
        _candidate_, result = self._verify(tmp_path, fake_gcloud)
        assert all(r.result.exit_code == 0 for r in result.shards)
        assert result.exit_code == 1 and result.outcome == "failed"
        assert [r.executed for r in result.shards] == [40, 29, 30, None]
        assert result.error is not None and "2/3 ran 29 of 30" in result.error

    def test_a_pool_too_short_is_refused_before_any_job(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        project = _project(tmp_path / "repo")
        config = _config(tmp_path, instances=["verify-1"])
        with pytest.raises(VerifyRefused, match="shards were asked for"):
            run_verify(config, store=None, shards=3, repo_root=project, host="gcp")
        assert _jobs(fake_gcloud) == []

    def test_every_job_is_shipped_the_bundle_and_the_hosts_are_released(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        self._verify(tmp_path, fake_gcloud)
        calls = _calls(fake_gcloud)
        copies = [call for call in calls if call[:2] == ["compute", "scp"]]
        assert len(copies) == 3, "each host in the pool gets the bundle once"
        stops = [
            instance
            for instance, command in _ssh_commands(fake_gcloud)
            if f"--unit={IDLE_STOP_UNIT} " in command
        ]
        assert sorted(stops) == POOL

    def test_every_job_is_run_by_a_worker_that_leaves_sigterm_to_the_main_thread(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#3121 — a worker that took ``SIGTERM`` would hold the unwind off.

        A process-directed signal goes to any thread that does not block it,
        so the shard workers block the unwinding signals. Each job's process
        inherits its worker's mask, which is where the fake reads it.
        """
        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        _candidate, result = self._verify(tmp_path, fake_gcloud)

        assert result.exit_code == 0, result.error
        masks = _read_state(fake_gcloud)["job_blocked_signals"]
        assert isinstance(masks, list) and len(masks) == len(_jobs(fake_gcloud))
        for blocked in masks:
            assert {int(signal.SIGTERM), int(signal.SIGINT)} <= set(blocked)

    def test_sigterm_mid_job_ends_every_hosts_job_and_releases_it_promptly(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2811 — the signal does not wait out the other shards' jobs.

        The fake runs one call at a time, so the jobs queue behind the first,
        and every job waits at a gate this test holds until the run has
        exited. The first job to start sends this process ``SIGTERM``,
        directed at the process as a supervisor's is. The run unwinds, and
        ends every job, while the gate is still held: a run that waited for
        its jobs could never get past them, and only the fake's hang guard
        would let them go (#3121). The handler records the thread it ran on.
        """
        started = tmp_path / "job-started"
        gate_path = tmp_path / "job-gate"
        events: list[object] = []
        with gate_path.open("a") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            _write_state(
                fake_gcloud,
                job_gate=str(gate_path),
                job_started={"marker": str(started), "pid": os.getpid()},
            )
            with pytest.raises(SystemExit) as exited, _recording_unwind(events):
                self._verify(tmp_path, fake_gcloud)
            events.append("gate released")
        state = _read_state(fake_gcloud)

        assert state.get("gate_hang_guard", 0) == 0, (
            "hang guard: a job waited 60s at the gate, so the run did not "
            "unwind while the gate was held"
        )
        assert exited.value.code == 128 + signal.SIGTERM
        assert started.exists()
        assert events == [
            ("handled", threading.main_thread().native_id),
            "gate released",
        ]
        assert state.get("jobs_finished", 0) == 0
        stops = [
            instance
            for instance, command in _ssh_commands(fake_gcloud)
            if f"--unit={IDLE_STOP_UNIT} " in command
        ]
        assert sorted(stops) == POOL

    def test_without_the_mask_a_sigterm_a_worker_takes_waits_out_the_job(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#3121's negative control: unmasked, the run exits only after the job.

        ``signal.pthread_sigmask``, the process boundary the shard workers set
        their mask through, is replaced by one that blocks nothing and records
        the worker that asked, so every worker can take ``SIGTERM``. Which
        unblocked thread a process-directed signal goes to is the kernel's
        choice, so this control makes the bad choice on purpose and sends the
        signal to a worker. The wakeup fd shows the worker took it; the main
        thread sleeps on, and its handler runs only once this test has let the
        job past the gate.
        """
        workers: list[int] = []

        def leave_unmasked(_how: int, _signals: object) -> set[signal.Signals]:
            workers.append(threading.get_ident())
            return set()

        monkeypatch.setattr(signal, "pthread_sigmask", leave_unmasked)
        started = tmp_path / "job-started"
        os.mkfifo(started)
        gate_path = tmp_path / "job-gate"
        gate = gate_path.open("a")
        fcntl.flock(gate, fcntl.LOCK_EX)
        reader, writer = socket.socketpair()
        writer.setblocking(False)
        reader.settimeout(60)
        abandoned = threading.Event()
        events: list[object] = []

        def take_it_on_a_worker() -> None:
            try:
                # The first job holds the FIFO open until this read; EOF is
                # the job having started.
                with started.open("rb") as marker:
                    marker.read()
                if abandoned.is_set():
                    return
                events.append("job started")
                signal.pthread_kill(workers[0], signal.SIGTERM)
                try:
                    taken = reader.recv(1)
                except TimeoutError:
                    events.append("hang guard: no worker took SIGTERM in 60s")
                else:
                    events.append(("taken by a worker", taken[0]))
            finally:
                events.append("gate released")
                gate.close()

        _write_state(
            fake_gcloud, job_gate=str(gate_path), job_started={"marker": str(started)}
        )
        previous = signal.set_wakeup_fd(writer.fileno())
        sender = threading.Thread(target=take_it_on_a_worker)
        sender.start()
        try:
            with pytest.raises(SystemExit) as exited, _recording_unwind(events):
                self._verify(tmp_path, fake_gcloud)
        finally:
            signal.set_wakeup_fd(previous)
            abandoned.set()
            with contextlib.suppress(OSError):
                # A sender still waiting on the FIFO is let go (no reader: ENXIO).
                os.close(os.open(started, os.O_WRONLY | os.O_NONBLOCK))
            sender.join()
            reader.close()
            writer.close()
        state = _read_state(fake_gcloud)

        assert state.get("gate_hang_guard", 0) == 0, "hang guard: the gate never opened"
        assert exited.value.code == 128 + signal.SIGTERM
        assert len(workers) == len(POOL)
        assert events == [
            "job started",
            ("taken by a worker", int(signal.SIGTERM)),
            "gate released",
            ("handled", threading.main_thread().native_id),
        ]
        assert state.get("jobs_finished", 0) >= 1

    def test_sigterm_mid_ship_ends_every_copy_and_releases_promptly(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """Review of #2811: the ship phase is as interruptible as the job.

        Only the job's session used to be tracked, so a signal landing on a
        copy waited out gcloud's 300s plumbing bound — long enough for a
        supervisor to follow up with a SIGKILL before any release ran.
        """
        _write_state(fake_gcloud, scp_sleep=60)
        sent: list[float] = []

        def terminate_once_a_copy_runs() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    copying = any(
                        call[:2] == ["compute", "scp"] for call in _calls(fake_gcloud)
                    )
                except (json.JSONDecodeError, OSError):
                    copying = False
                if copying:
                    sent.append(time.monotonic())
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        sender = threading.Thread(target=terminate_once_a_copy_runs)
        sender.start()
        try:
            with pytest.raises(SystemExit) as exited, terminate_unwinds():
                self._verify(tmp_path, fake_gcloud)
        finally:
            sender.join()
        elapsed = time.monotonic() - sent[0]

        assert exited.value.code == 128 + signal.SIGTERM
        assert elapsed < 30, f"exit waited {elapsed:.1f}s for the copies to finish"
        assert _jobs(fake_gcloud) == []
        stops = [
            instance
            for instance, command in _ssh_commands(fake_gcloud)
            if f"--unit={IDLE_STOP_UNIT} " in command
        ]
        assert sorted(stops) == POOL

    def test_no_job_is_handed_a_credential_shaped_variable(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        _candidate_, _result = self._verify(tmp_path, fake_gcloud)
        for command in _jobs(fake_gcloud):
            assert "TOKEN" not in command and "_KEY" not in command


class TestTheRecordAndTheCommand:
    """What a sharded verification attests, and what it prints."""

    def test_the_event_records_every_job_its_count_and_the_collection(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew.run_store import SqliteRunStore

        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        project = _project(tmp_path / "repo")
        config = _config(tmp_path)
        log_dir = Path(config.logging.dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            run_verify(config, store=store, shards=3, repo_root=project, host="gcp")
            events = store.list_audit_events(event=VERIFY_EVENT)
        assert len(events) == 1
        payload = events[0].payload
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload["outcome"] == "passed"
        assert payload["shards"] == 3
        assert payload["collected"] == 100
        rows = payload["jobs"]
        assert [row["job"] for row in rows] == ["1/3", "2/3", "3/3", SINGLE_JOB]
        assert [row["instance"] for row in rows] == [*POOL, "verify-1"]
        assert [row["selected"] for row in rows] == [40, 30, 30, 100]
        assert all(row["exit_code"] == 0 for row in rows)

    def test_a_sharded_event_names_the_verified_commits_parents(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#3140 — the work page joins the event to its item by these."""
        from kew.run_store import SqliteRunStore

        _write_state(
            fake_gcloud,
            job_responses=_pooled_responses(s1=40, s2=30, s3=30, single=100),
        )
        project = _project(tmp_path / "repo")
        main = _run([*_GIT, "rev-parse", "main"], project)
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            run_verify(
                _config(tmp_path),
                store=store,
                shards=3,
                repo_root=project,
                host="gcp",
            )
            [event] = store.list_audit_events(event=VERIFY_EVENT)
        payload = event.payload
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload["shards"] == 3
        assert payload["ref_parents"] == [main]

    def test_an_unsharded_verification_records_no_shard_rows(
        self, tmp_path: Path
    ) -> None:
        from kew.merge_verify import _shard_payload

        assert (
            _shard_payload(VerifyResult(host="local", exit_code=0, seconds=1.0, log=""))
            == {}
        )

    def test_the_command_prints_a_line_per_shard_and_exits_the_shards_code(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The command bundles ``project_root()``, which tests/conftest.py
        # points at tmp_path itself — so the repository goes there.
        project = _project(tmp_path)
        responses = _pooled_responses(s1=40, s2=30, s3=30, single=100)
        responses[0]["exit"] = 4
        _write_state(fake_gcloud, job_responses=responses)
        (project / "kew.toml").write_text(
            "[hosts.gcp]\n"
            'project = "proj"\n'
            'zone = "us-central1-a"\n'
            f"instances = {json.dumps(POOL)}\n"
            "\n[verify]\n"
            'host = "gcp"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.chdir(project)
        result = runner.invoke(app, ["verify", "--shards", "3"])
        assert result.exit_code == 4, result.output
        for spec in ("1/3", "2/3", "3/3", SINGLE_JOB):
            assert spec in result.output
        assert "instance=verify-2" in result.output
