"""Main's health: the nightly full verify, the red-main gate, the bisect (#3195).

A fixture repository with an ``origin`` stands in for the project. The verify
host is the local one with its job execution faked at the process boundary
(:meth:`LocalProvider._execute`): a job's verdict is read off the shipped
checkout itself — ``kew/state`` reads ``broken`` from the merge that breaks
test t on — so the bisect's probes are answered by the commit they ran on.
``gh`` is :class:`FakeGh`, which records every call and files nothing.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from support.fake_launchctl import calls, fake_launchctl
from support.gh_fakes import FakeGh
from support.held_out_log import opened
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, load_config
from kew.graph.driver.merge_queue import merge_queue_lock, merge_queue_lock_path
from kew.hosts import Job, LocalProvider
from kew.hosts.gcp import with_stderr
from kew.main_health import (
    CLEARED_EVENT,
    CVE_EVENT,
    NIGHTLY_KIND,
    NOT_REPRODUCED,
    REGRESSION_EVENT,
    REGRESSION_LABEL,
    SCAN_FAILED_EVENT,
    STALE_AFTER,
    TIP_PROBE_EVENT,
    Regression,
    bisect,
    failing_gate,
    failing_tests,
    file_regression,
    log_tail,
    probe_budget,
    probe_script,
    probe_verdict,
    read_main_health,
)
from kew.merge_verify import (
    ENVIRONMENT_BROKEN_EXIT,
    ENVIRONMENT_BROKEN_MARK,
    IMAGE_SCAN_ENV,
    JOB_NAME,
    PREFLIGHT_PASSED_MARK,
    SUITE_JOB_NAME,
    SWITCHYARD_ARCHIVE_ENV,
    SWITCHYARD_IMAGE_ENV,
    UI_CHECK_COMMAND,
    VERIFY_EVENT,
    image_inputs_changed,
    resolve_candidate,
    run_verify,
    summary_line,
)
from kew.nightly import NightlyBusy, run_nightly
from kew.nightly_agent import NIGHTLY_MARK, nightly_label
from kew.nightly_scan import read_scan
from kew.run_store import SqliteRunStore

runner = CliRunner()

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)
_TEST_T = "tests/test_t.py::test_t"
_T_PASSES = "def test_t():\n    assert True\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(repo: Path, subject: str, *, state: str | None = None) -> str:
    if state is not None:
        (repo / "kew" / "state").write_text(f"{state}\n", encoding="utf-8")
    (repo / "kew" / "log.txt").open("a", encoding="utf-8").write(f"{subject}\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


class Main:
    """The project, its origin, and the commits on main."""

    def __init__(self, root: Path, origin: Path) -> None:
        self.root, self.origin = root, origin
        origin.mkdir(parents=True, exist_ok=True)
        _git(origin, "init", "--quiet", "--bare", "-b", "main", ".")
        _git(root, "init", "--quiet", "-b", "main", ".")
        (root / ".git" / "info" / "exclude").write_text(
            "logs/\nkew.toml\n", encoding="utf-8"
        )
        (root / "kew" / "tests").mkdir(parents=True, exist_ok=True)
        # Test t itself, which a PR verify's red-main phase runs for real, and
        # the config that makes kew/ pytest's rootdir, as in kew's own tree.
        (root / "kew" / "tests" / "test_t.py").write_text(_T_PASSES, encoding="utf-8")
        (root / "kew" / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n", encoding="utf-8"
        )
        _git(root, "remote", "add", "origin", str(origin))
        self.green = _commit(root, "base", state="ok")
        self.push()

    def push(self) -> None:
        _git(self.root, "push", "--quiet", "origin", "main")
        _git(self.root, "fetch", "--quiet", "origin")

    def merges(self, count: int, *, breaks_at: int | None) -> list[str]:
        """*count* squash merges on main; merge *breaks_at* breaks test t."""
        shas = []
        for n in range(1, count + 1):
            state = "broken" if n == breaks_at else None
            shas.append(_commit(self.root, f"change {n} (#{100 + n})", state=state))
        self.push()
        return shas

    def branch(self, name: str, *, state: str | None, file: str = "kew/x.py") -> str:
        _git(self.root, "checkout", "--quiet", "-b", name, "main")
        (self.root / file).parent.mkdir(parents=True, exist_ok=True)
        (self.root / file).write_text(f"{name}\n", encoding="utf-8")
        sha = _commit(self.root, name, state=state)
        _git(self.root, "checkout", "--quiet", "main")
        return sha

    def squash_land(self, sha: str, subject: str) -> str:
        """Land *sha* as a squash merge: main gains a commit with its tree."""
        tree = _git(self.root, "rev-parse", f"{sha}^{{tree}}")
        landed = _git(self.root, "commit-tree", tree, "-p", "main", "-m", subject)
        _git(self.root, "update-ref", "refs/heads/main", landed)
        _git(self.root, "checkout", "--quiet", "-f", "main")
        self.push()
        return landed


#: ``uv`` on the host, as a shell function: ``uv run X`` is ``python -m X``
#: with this interpreter, which has pytest.
_UV = f'uv() {{ shift; "{sys.executable}" -m "$@"; }}'


def _run_red_main_phase(checkout: Path, command: str) -> str:
    """Run the job's red-main phase for real, in the shipped checkout.

    The phase is the one part of the job these tests do not answer for the
    host: it runs the red's failing ids with real pytest against the
    candidate's own tests, so what it reports is what that tree does.
    """
    start = command.find('red_main_nonce="')
    if start < 0:
        return ""
    end = command.index('rm -rf "$red_main_dir"', start)
    phase = command[start:end] + 'rm -rf "$red_main_dir"\n'
    done = subprocess.run(
        ["bash", "-c", f"set -eu\n{_UV}\n{phase}"],
        cwd=checkout / "kew",
        env={"PATH": "/usr/bin:/bin", "HOME": str(checkout)},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


#: A scan that gated every image and found nothing.
_CLEAN_SCAN = (
    "scan-agent-image.sh: trivy 0.58.1, the pinned scanner.\n"
    "scan-agent-image.sh: gated every image: 3 of 3 agent images\n"
    "all 3 agent images passed their CRITICAL/HIGH policies\n"
)

#: Jobs that never really ran their gates: what the host answered.
_NEVER_RAN = {
    # The kernel's OOM killer reached the gate: 128 + SIGKILL.
    "oom": (137, f"{PREFLIGHT_PASSED_MARK}\ncollecting ...\n"),
    "environment": (
        ENVIRONMENT_BROKEN_EXIT,
        f"{PREFLIGHT_PASSED_MARK}\n"
        f"{ENVIRONMENT_BROKEN_MARK} .venv/bin/python is missing\n",
    ),
    "preflight": (
        1,
        "scan-agent-image.sh: trivy 0.1.0 is not the pinned 0.58.1 — a different "
        "scanner version is a different gate\n",
    ),
}


class FakeHost:
    """The verify host's job execution, answered from the shipped checkout."""

    def __init__(self, config: DispatchConfig) -> None:
        self.config = config
        self.jobs: list[dict[str, Any]] = []
        self.scan_output = ""
        self.scan_exit: int | None = None
        #: Commits whose full verify fails on test t though t passes alone
        #: there: a flake, or an interaction with the rest of the suite.
        self.flaky: set[str] = set()
        #: The node id a red verify names.
        self.failing = _TEST_T
        #: What every bisect probe exits with instead of the commit's answer.
        self.probe_exit: int | None = None
        #: A broken commit's verify never really runs its gates: one of
        #: :data:`_NEVER_RAN`.
        self.never_ran: str | None = None
        #: A broken commit's verify fails a gate with this log and no failing
        #: test id (a conftest import, the coverage floor, a static gate).
        self.red_log: str | None = None

    def lease_held(self) -> bool:
        with merge_queue_lock(merge_queue_lock_path(self.config)) as lease:
            return not lease.acquired

    def __call__(self, provider: LocalProvider, job: Job) -> tuple[int, str]:
        checkout = provider.checkout
        assert checkout is not None
        broken = (checkout / "kew" / "state").read_text().strip() == "broken"
        sha = _git(checkout, "rev-parse", "HEAD")
        self.jobs.append(
            {
                "name": job.name,
                "sha": sha,
                "command": job.command,
                "environment": dict(job.environment),
                "lease_held": self.lease_held(),
            }
        )
        if job.name == "main-bisect":
            if self.probe_exit is not None:
                return self.probe_exit, ""
            return (1 if broken else 0), ""
        if job.name == "image-scan":
            if self.scan_exit is not None:
                return self.scan_exit, self.scan_output
            if not self.scan_output:
                return 0, _CLEAN_SCAN
            return 1, self.scan_output
        red_main = _run_red_main_phase(checkout, job.command)
        if (broken or sha in self.flaky) and self.never_ran is not None:
            return _NEVER_RAN[self.never_ran]
        if broken or sha in self.flaky:
            if self.red_log is not None:
                return 1, f"{PREFLIGHT_PASSED_MARK}\n{self.red_log}"
            failed = _summary_log(f"FAILED {self.failing} - assert False")
            return 1, f"{PREFLIGHT_PASSED_MARK}\n{failed}{_HELD_OUT_PASSED}"
        passed = f"{PREFLIGHT_PASSED_MARK}\ngate passed\n"
        return 0, passed + red_main + _HELD_OUT_PASSED

    def named(self, name: str) -> list[dict[str, Any]]:
        return [job for job in self.jobs if job["name"] == name]


@pytest.fixture
def main(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Main:
    return Main(tmp_path, tmp_path_factory.mktemp("origin") / "origin.git")


@pytest.fixture
def config(main: Main, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    """The project's own ``kew.toml``, which the CLI reads as the API does."""
    toml = main.root / "kew.toml"
    toml.write_text(
        '[project]\nrepo = "o/r"\n\n'
        f'[logging]\ndir = "{main.root / "logs"}"\n\n'
        '[verify]\nhost = "local"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(main.root))
    return load_config(toml)


@pytest.fixture
def host(
    config: DispatchConfig, main: Main, monkeypatch: pytest.MonkeyPatch
) -> FakeHost:
    fake = FakeHost(config)

    def _execute(provider: LocalProvider, job: Job) -> tuple[int, str]:
        return fake(provider, job)

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    monkeypatch.setenv("KEW_PROJECT", str(main.root))
    monkeypatch.chdir(main.root)
    return fake


@pytest.fixture
def store(config: DispatchConfig) -> Iterator[SqliteRunStore]:
    log_dir = Path(config.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as opened_store:
        yield opened_store


def _payload(event: Any) -> dict[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _health(store: SqliteRunStore, main: Main, **kwargs: Any) -> Any:
    return read_main_health(store, main.root, base_ref="origin/main", **kwargs)


def _scan_log(
    gates: dict[str, list[str]],
    triage: tuple[str, ...] = (),
    *,
    unreadable: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    stopped: str | None = None,
) -> str:
    """What scripts/scan-agent-image.sh prints: per image a gate table in
    trivy's box layout, how that gate ended, and its triage table; then the
    closing lines. *unreadable* gates found something with no id in the
    Vulnerability column; *errors* gates could not scan at all."""
    lines = ["scan-agent-image.sh: trivy 0.58.1, the pinned scanner."]
    for tag, found in gates.items():
        lines.append(f"==> gate: CRITICAL/HIGH with a fix available in {tag} (x)")
        lines.append("│ Library │ Vulnerability │ Severity │ Status │")
        lines += [f"│ node │ {cve} │ HIGH │ fixed │" for cve in found]
        if tag in unreadable:
            lines.append("│ node │  │ HIGH │ fixed │")
        if found or tag in unreadable:
            lines.append(f"scan-agent-image.sh: gate findings in {tag}")
        if tag in errors:
            lines.append(f"scan-agent-image.sh: gate error in {tag}: trivy exited 1")
        lines.append(f"==> triage input: all CRITICAL/HIGH in {tag} (not a gate)")
        lines += [
            f"│ deb │ {cve} │ CRITICAL │ (unfixed) │" for cve in (*found, *triage)
        ]
    if stopped is not None:
        # The script died under set -e before the closing summary.
        return "\n".join([*lines, stopped]) + "\n"
    lines.append(
        f"scan-agent-image.sh: gated every image: {len(gates)} of {len(gates)} "
        "agent images"
    )
    failed = [tag for tag, found in gates.items() if found or tag in unreadable]
    if failed:
        lines.append(
            "scan-agent-image.sh: the gate found CRITICAL/HIGH findings in "
            f"{len(failed)} image(s): {' '.join(failed)}"
        )
    if errors:
        lines.append(
            f"scan-agent-image.sh: the gate could not scan {len(errors)} image(s): "
            + " ".join(errors)
        )
    return "\n".join(lines) + "\n"


def _red_sha(store: SqliteRunStore) -> str:
    """The commit the newest red nightly read red."""
    return next(
        _payload(e)["ref_sha"]
        for e in store.list_audit_events(None, event=VERIFY_EVENT)
        if _payload(e)["kind"] == NIGHTLY_KIND and _payload(e)["outcome"] == "failed"
    )


def _red_main(
    main: Main, config: DispatchConfig, store: SqliteRunStore, gh: FakeGh
) -> list[str]:
    """A green nightly, five merges with merge 3 breaking t, a red nightly."""
    run_nightly(config, store=store, repo_root=main.root, gh=gh)
    merges = main.merges(5, breaks_at=3)
    run_nightly(config, store=store, repo_root=main.root, gh=gh)
    return merges


class TestTheNightly:
    def test_a_red_nightly_pins_the_breaking_merge_and_files_one_issue(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        merges = _red_main(main, config, store, gh)

        nightlies = [
            _payload(e)
            for e in store.list_audit_events(None, event=VERIFY_EVENT)
            if _payload(e)["kind"] == NIGHTLY_KIND
        ]
        assert [(n["ref_sha"], n["outcome"]) for n in reversed(nightlies)] == [
            (main.green, "passed"),
            (merges[-1], "failed"),
        ]
        assert all(n["suite"] == "full" for n in nightlies)
        assert nightlies[0]["failing_tests"] == [_TEST_T]
        # The bisect ran t alone, within ceil(log2 5) + 1 probes.
        probes = host.named("main-bisect")
        assert 0 < len(probes) <= math.ceil(math.log2(5)) + 1
        assert all(_TEST_T in p["command"] for p in probes)
        assert all("scripts/verify.sh" not in p["command"] for p in probes)
        (create,) = gh.creates()
        assert create[-2:] == ["--label", REGRESSION_LABEL]
        body = create[create.index("--body") + 1]
        assert merges[2] in body and "PR #103" in body and _TEST_T in body
        (filed,) = store.list_audit_events(None, event=REGRESSION_EVENT)
        assert _payload(filed)["pr"] == 103
        assert _payload(filed)["culprit"] == merges[2]
        assert _health(store, main).red

    def test_a_second_nightly_of_the_same_red_sha_files_no_duplicate(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        _red_main(main, config, store, gh)
        probes = len(host.named("main-bisect"))

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        assert len(gh.creates()) == 1
        assert len(store.list_audit_events(None, event=REGRESSION_EVENT)) == 1
        assert len(host.named("main-bisect")) == probes
        assert report.regression is not None and report.regression.duplicate
        assert report.health.issue == 101

    def test_the_nightly_holds_the_verify_lease_for_its_whole_run(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _red_main(main, config, store, FakeGh())

        # The gate job, the suite job behind a clean one (#3212), the bisect
        # and the image scan.
        assert {job["name"] for job in host.jobs} == {
            JOB_NAME,
            SUITE_JOB_NAME,
            "main-bisect",
            "image-scan",
        }
        assert all(job["lease_held"] for job in host.jobs)
        assert not host.lease_held()

    def test_a_held_lease_refuses_the_nightly_after_its_wait(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        slept: list[float] = []
        with merge_queue_lock(merge_queue_lock_path(config)) as lease:
            assert lease.acquired
            with pytest.raises(NightlyBusy, match="stayed held"):
                run_nightly(
                    config,
                    store=store,
                    repo_root=main.root,
                    gh=FakeGh(),
                    wait_seconds=60,
                    sleep=slept.append,
                )
        assert slept == [30.0, 30.0]
        assert host.jobs == []

    def test_the_nightly_scans_every_image_and_files_each_new_cve_once(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log(
            {"kew-agent:a": ["CVE-2026-11111"], "kew-agent:b": ["CVE-2026-22222"]}
        )

        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        run_nightly(config, store=store, repo_root=main.root, gh=gh)

        titles = [c[c.index("--title") + 1] for c in gh.creates()]
        assert titles == [
            "CVE-2026-11111 in an agent image",
            "CVE-2026-22222 in an agent image",
        ]
        assert len(store.list_audit_events(None, event=CVE_EVENT)) == 2
        # The scan is its own job; the nightly's verify job skips it.
        (verify, _second) = host.named(JOB_NAME)
        assert verify["environment"][IMAGE_SCAN_ENV] == "skip"
        assert "scan-agent-image.sh" in host.named("image-scan")[0]["command"]
        # A CVE fails no test: main stays green.
        assert _health(store, main).state == "green"

    def test_the_nightly_scan_carries_the_private_layer_opt_in_to_its_host(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PR verifies leave the scan to the nightly, so it must scan that layer."""
        archive = str(main.root / "switchyard.tar")
        monkeypatch.setenv(SWITCHYARD_IMAGE_ENV, "1")
        monkeypatch.setenv(SWITCHYARD_ARCHIVE_ENV, archive)

        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())

        (scan,) = host.named("image-scan")
        assert scan["environment"][SWITCHYARD_IMAGE_ENV] == "1"
        assert scan["environment"][SWITCHYARD_ARCHIVE_ENV] == archive


class TestTheRedMainGate:
    def test_a_pr_verify_while_main_is_red_refuses_naming_the_sha_and_issue(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())
        unrelated = main.branch("unrelated", state=None)

        result = runner.invoke(app, ["verify", unrelated])

        assert result.exit_code == 1, result.output
        assert merges[-1] in result.output
        assert "issue #101" in result.output
        assert "only a change that fixes main merges" in result.output
        newest = _payload(store.list_audit_events(1, event=VERIFY_EVENT)[0])
        assert newest["suite"] == "full"
        assert newest["main_health"]["sha"] == merges[-1]
        assert newest["main_health"]["issue"] == 101

    def test_a_fixing_pr_passes_and_main_reads_green_once_it_lands(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _red_main(main, config, store, FakeGh())
        fix = main.branch("fix", state="ok")

        candidate, result = run_verify(
            config, store=store, ref=fix, repo_root=main.root
        )

        assert result.outcome == "passed"
        assert result.main_red is not None
        assert result.main_red_refusal() is None
        assert _health(store, main).red  # not landed yet
        landed = main.squash_land(candidate.ref_sha, "fix t (#200)")
        health = _health(store, main)
        assert health.state == "green"
        assert health.via == "pr"
        # The red reproduced alone, so the landing is what cleared it.
        assert health.cleared_red == _red_sha(store)
        assert health.cleared_by == landed
        assert health.cleared_pr == 200
        assert "cleared by" in health.describe()

    def test_a_failing_pr_that_never_lands_leaves_main_red(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _red_main(main, config, store, FakeGh())
        unrelated = main.branch("unrelated", state=None)

        _candidate, result = run_verify(
            config, store=store, ref=unrelated, repo_root=main.root
        )

        assert result.outcome == "failed"
        assert result.main_red_refusal() is not None
        assert _health(store, main).red

    def test_a_later_passing_verify_of_an_older_main_commit_leaves_main_red(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())

        _candidate, result = run_verify(
            config, store=store, ref=merges[1], repo_root=main.root
        )

        assert result.outcome == "passed"
        health = _health(store, main)
        assert health.red
        assert health.sha == merges[-1]
        assert health.issue == 101


class TestClearingAFlake:
    def test_clear_writes_one_audited_event_and_main_reads_green(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())
        monkeypatch.setenv("KEW_ACTOR", "tom")

        cleared = runner.invoke(
            app,
            [
                "verify",
                "main-health",
                "clear",
                merges[-1][:12],
                "--issue",
                "101",
                "--reason",
                "t flaked on the host clock",
            ],
        )
        shown = runner.invoke(app, ["verify", "main-health"])

        assert cleared.exit_code == 0, cleared.output
        (event,) = store.list_audit_events(None, event=CLEARED_EVENT)
        assert event.actor_id == "tom"
        assert _payload(event)["reason"] == "t flaked on the host clock"
        assert _payload(event)["sha"] == merges[-1]
        assert _payload(event)["issue"] == 101
        assert shown.exit_code == 0, shown.output
        assert "main is green" in shown.output
        assert _health(store, main).via == "clear"

    def test_clear_without_a_reason_is_refused_and_writes_nothing(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        result = runner.invoke(
            app, ["verify", "main-health", "clear", main.green, "--issue", "1"]
        )

        assert result.exit_code == 2
        assert store.list_audit_events(None, event=CLEARED_EVENT) == []

    def test_main_health_exits_one_while_main_is_red(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())

        result = runner.invoke(app, ["verify", "main-health"])

        assert result.exit_code == 1
        assert f"main is red at {merges[-1][:12]} (issue #101)" in result.output


class TestStaleness:
    def test_a_nightly_older_than_26_hours_reads_stale(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())
        (event,) = store.list_audit_events(None, event=VERIFY_EVENT)
        from datetime import datetime

        at = datetime.fromisoformat(event.timestamp)

        fresh = _health(store, main, now=at + STALE_AFTER - timedelta(minutes=1))
        stale = _health(store, main, now=at + STALE_AFTER + timedelta(minutes=1))

        assert not fresh.stale
        assert stale.stale
        assert stale.state == "green"


class TestTheImageScanScope:
    def test_a_diff_touching_no_image_input_skips_the_scan_and_says_so(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        sha = main.branch("app-only", state=None, file="kew/src/kew/app.py")

        result = runner.invoke(app, ["verify", sha])

        assert result.exit_code == 0, result.output
        assert "image_scan=skipped" in result.output
        (job,) = host.named(JOB_NAME)
        assert job["environment"][IMAGE_SCAN_ENV] == "skip"
        newest = _payload(store.list_audit_events(1, event=VERIFY_EVENT)[0])
        assert newest["image_scan"]["ran"] is False
        assert "no image input changed" in newest["image_scan"]["reason"]

    @pytest.mark.parametrize(
        "file", ["uv.lock", "kew/uv.lock", "docker/agent.Dockerfile"]
    )
    def test_a_diff_touching_an_image_input_scans(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any, file: str
    ) -> None:
        sha = main.branch("bump", state=None, file=file)

        run_verify(config, store=store, ref=sha, repo_root=main.root)

        (job,) = host.named(JOB_NAME)
        assert IMAGE_SCAN_ENV not in job["environment"]
        assert resolve_candidate(main.root, ref=sha).image_inputs_changed

    def test_verify_sh_skips_the_scan_only_when_told_to(self, tmp_path: Path) -> None:
        """The gate's own branch, run with the scan replaced by a marker."""
        script = Path(__file__).resolve().parents[1] / "scripts" / "verify.sh"
        text = script.read_text(encoding="utf-8")
        start = text.index('if [ "${KEW_VERIFY_IMAGE_SCAN:-run}" = "skip" ]')
        end = text.index("fi\n", text.index("bash ../scripts/scan-agent-image.sh"))
        snippet = text[start : end + 3].replace(
            "bash ../scripts/scan-agent-image.sh", "echo SCANNED"
        )
        ran = {
            value: subprocess.run(
                ["sh", "-c", snippet],
                env={"PATH": "/usr/bin:/bin", IMAGE_SCAN_ENV: value},
                capture_output=True,
                text=True,
                check=True,
            )
            for value in ("skip", "")
        }
        assert "SCANNED" not in ran["skip"].stdout
        assert "NOT run" in ran["skip"].stderr
        assert "SCANNED" in ran[""].stdout

    def test_image_inputs(self) -> None:
        assert not image_inputs_changed(["kew/src/kew/cli/__init__.py", "docs/a.md"])
        assert image_inputs_changed(["kew/src/kew/_docker/docker/agent.Dockerfile"])
        assert image_inputs_changed(["scripts/scan-agent-image.sh"])

    def test_the_scanner_pin_is_an_image_input(self) -> None:
        """A new trivy finds what the old one did not: a re-pin scans."""
        assert image_inputs_changed(["kew/src/kew/hosts/provision/trivy.pin"])


class TestTheBisect:
    @pytest.mark.parametrize("count", [1, 2, 5, 8, 9])
    def test_each_breaking_commit_is_found_within_the_budget(self, count: int) -> None:
        commits = [f"c{n}" for n in range(count)]
        for breaks in range(count):
            probed: list[str] = []

            def fails(
                sha: str, breaks: int = breaks, probed: list[str] = probed
            ) -> bool:
                probed.append(sha)
                return commits.index(sha) >= breaks

            found = bisect(commits, fails)

            assert found.culprit == commits[breaks]
            assert len(probed) <= probe_budget(count)

    def test_a_probe_with_no_verdict_pins_nothing(self) -> None:
        found = bisect(["a", "b", "c", "d"], lambda _sha: None)

        assert found.culprit is None
        assert found.reason is not None and "no verdict" in found.reason

    def test_a_probe_reads_red_when_one_test_fails_and_another_is_not_there_yet(
        self, tmp_path: Path
    ) -> None:
        """One pytest call over both ids exits 4 and runs neither."""
        tests = tmp_path / "kew" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_a.py").write_text(
            "def test_fails():\n    assert False\n", encoding="utf-8"
        )
        # ``uv`` stands in as a shell function: sync is a no-op, run is pytest.
        uv = f'uv() {{ [ "$1" = sync ] && return 0; shift; {sys.executable} -m "$@"; }}'
        script = probe_script(
            ["tests/test_b.py::test_new", "tests/test_a.py::test_fails"]
        )

        done = subprocess.run(
            ["sh", "-c", f"{uv}\n{script}"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

        assert done.returncode == 1, done.stdout + done.stderr
        assert probe_verdict(done.returncode) is True

    def test_failing_tests_are_read_from_the_short_summary(self) -> None:
        log = _summary_log(
            "FAILED tests/test_a.py::test_x - AssertionError",
            "ERROR kew/tests/test_b.py::test_y",
            "FAILED tests/test_a.py::test_x - again",
            "PASSED tests/test_c.py::test_z",
        )

        assert failing_tests(log) == (
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y",
        )

    def test_a_parametrized_id_with_spaces_is_read_whole(self) -> None:
        log = _summary_log(
            "FAILED tests/test_a.py::test_x[3-has a service account] - Assert",
            "FAILED tests/test_a.py::TestK::test_y[a b]",
            "ERROR tests/test_c.py - ImportError: no module",
        )

        assert failing_tests(log) == (
            "tests/test_a.py::test_x[3-has a service account]",
            "tests/test_a.py::TestK::test_y[a b]",
            "tests/test_c.py",
        )

    def test_the_scan_names_cves_only_from_its_gating_tables(self) -> None:
        log = _scan_log(
            {"kew-agent:a": ["CVE-2026-1111"], "kew-agent:b": []},
            triage=("CVE-2026-9999",),
        )

        assert read_scan(log, 1, None).advisories == ("CVE-2026-1111",)


def _flaky_red_main(
    main: Main,
    config: DispatchConfig,
    store: SqliteRunStore,
    host: FakeHost,
    gh: FakeGh,
    *,
    count: int = 5,
) -> list[str]:
    """A green nightly, *count* merges that break nothing, and a red nightly at
    the tip whose failing test passes alone there."""
    run_nightly(config, store=store, repo_root=main.root, gh=gh)
    merges = main.merges(count, breaks_at=None)
    host.flaky.add(merges[-1])
    run_nightly(config, store=store, repo_root=main.root, gh=gh)
    return merges


class TestARedThatReproduces:
    @pytest.mark.parametrize("breaks_at", [1, 5])
    def test_the_tip_is_probed_first_and_the_walk_stays_in_budget(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        breaks_at: int,
    ) -> None:
        gh = FakeGh()
        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        merges = main.merges(5, breaks_at=breaks_at)

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        probes = host.named("main-bisect")
        assert probes[0]["sha"] == merges[-1]
        assert len(probes) <= math.ceil(math.log2(5)) + 1
        assert report.regression is not None
        assert report.regression.culprit == merges[breaks_at - 1]
        assert report.regression.pr == 100 + breaks_at
        (probed,) = store.list_audit_events(None, event=TIP_PROBE_EVENT)
        assert _payload(probed)["reproduced"] == "reproduced"


class TestARedTheTestsAloneDoNotReproduce:
    @pytest.mark.parametrize("count", [1, 5])
    def test_the_tip_is_probed_first_and_nothing_is_pinned(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        count: int,
    ) -> None:
        gh = FakeGh()
        merges = _flaky_red_main(main, config, store, host, gh, count=count)

        # One probe, of the red tip, running t alone; no bisect after it.
        (probe,) = host.named("main-bisect")
        assert probe["sha"] == merges[-1]
        assert _TEST_T in probe["command"]
        (create,) = gh.creates()
        title = create[create.index("--title") + 1]
        body = create[create.index("--body") + 1]
        assert "did not reproduce alone (flake or interaction)" in title
        assert "did not reproduce alone (flake or interaction)" in body
        assert _TEST_T in body
        assert "PR #" not in body and "pinned to" not in title
        (filed,) = store.list_audit_events(None, event=REGRESSION_EVENT)
        assert _payload(filed)["culprit"] is None
        assert _payload(filed)["pr"] is None
        assert _payload(filed)["reproduced"] == NOT_REPRODUCED
        (probed,) = store.list_audit_events(None, event=TIP_PROBE_EVENT)
        assert _payload(probed)["sha"] == merges[-1]
        assert _payload(probed)["reproduced"] == NOT_REPRODUCED
        health = _health(store, main)
        assert health.red and health.reproduced == NOT_REPRODUCED

    def test_a_landed_passing_pr_does_not_clear_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _flaky_red_main(main, config, store, host, FakeGh())
        change = main.branch("unrelated", state=None)

        candidate, result = run_verify(
            config, store=store, ref=change, repo_root=main.root
        )
        main.squash_land(candidate.ref_sha, "unrelated (#300)")

        assert result.outcome == "passed"
        health = _health(store, main)
        assert health.red
        assert health.sha == merges[-1]
        assert "no landed PR clears it" in health.describe()

    def test_a_passing_pr_verify_of_the_red_commit_itself_does_not_clear_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _flaky_red_main(main, config, store, host, FakeGh())
        host.flaky.clear()

        _candidate, result = run_verify(
            config, store=store, ref=merges[-1], repo_root=main.root
        )

        assert result.outcome == "passed"
        assert _health(store, main).red

    def test_a_newer_green_nightly_clears_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _flaky_red_main(main, config, store, host, FakeGh())
        newer = main.merges(1, breaks_at=None)

        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())

        health = _health(store, main)
        assert health.state == "green"
        assert (health.sha, health.via) == (newer[0], NIGHTLY_KIND)

    def test_the_audited_clear_clears_it(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
    ) -> None:
        merges = _flaky_red_main(main, config, store, host, FakeGh())

        result = runner.invoke(
            app,
            ["verify", "main-health", "clear", merges[-1]]
            + ["--issue", "101", "--reason", "t flaked"],
        )

        assert result.exit_code == 0, result.output
        assert _health(store, main).via == "clear"


class TestClearingARedThatReproduced:
    def test_a_landed_pr_that_deleted_the_failing_test_does_not_clear_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())
        _git(main.root, "checkout", "--quiet", "-b", "delete-t", "main")
        _git(main.root, "rm", "--quiet", "kew/tests/test_t.py")
        deleted = _commit(main.root, "drop t", state="ok")
        _git(main.root, "checkout", "--quiet", "main")

        candidate, result = run_verify(
            config, store=store, ref=deleted, repo_root=main.root
        )
        main.squash_land(candidate.ref_sha, "drop t (#201)")

        assert result.outcome == "passed"
        health = _health(store, main)
        assert health.red
        assert health.sha == merges[-1]

    def test_a_pr_verify_while_main_is_red_runs_the_whole_gate(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _red_main(main, config, store, FakeGh())
        fix = main.branch("fix", state="ok", file="kew/src/kew/app.py")

        _candidate, result = run_verify(
            config, store=store, ref=fix, repo_root=main.root
        )

        assert result.main_red is not None
        (job,) = [j for j in host.named(JOB_NAME) if j["sha"] == fix]
        assert "bash scripts/verify.sh" in job["command"]
        assert "KEW_VERIFY_SUITE" not in job["environment"]
        newest = _payload(store.list_audit_events(1, event=VERIFY_EVENT)[0])
        assert newest["suite"] == "full"


class TestTheClearCommand:
    def test_clear_refuses_a_main_commit_no_full_verify_read_red(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        _red_main(main, config, store, FakeGh())

        result = runner.invoke(
            app,
            ["verify", "main-health", "clear", main.green]
            + ["--issue", "101", "--reason", "it looked fine"],
        )

        assert result.exit_code == 2
        assert "not a red main commit" in result.output
        assert store.list_audit_events(None, event=CLEARED_EVENT) == []
        assert _health(store, main).red


class TestTheNightlyJob:
    def test_the_nightly_runs_the_browser_ui_check(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())

        # In the suite job, which runs once the gate job is clean (#3212).
        (suite,) = host.named(SUITE_JOB_NAME)
        assert UI_CHECK_COMMAND in suite["command"]
        newest = _payload(store.list_audit_events(1, event=VERIFY_EVENT)[0])
        assert newest["ui_check"] is True

    def test_the_summary_line_still_ends_with_the_mutation_field(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        merges = _red_main(main, config, store, FakeGh())
        sha = main.branch("app-only", state=None, file="kew/src/kew/app.py")

        candidate, result = run_verify(
            config, store=store, ref=sha, repo_root=main.root
        )

        line = summary_line(candidate, result)
        assert line.endswith(" mutation=deferred"), line
        assert " image_scan=skipped" in line
        assert f" main=red@{merges[-1][:12]}" in line

    def test_the_nightly_refuses_without_gh_before_it_verifies(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kew import subprocess_utils

        # The machine has no gh: tool discovery is the boundary faked here
        # (the session's own lookup always supplies a stand-in gh).
        found = subprocess_utils.shutil.which

        def which(command: str, *args: Any, **kwargs: Any) -> str | None:
            return None if command == "gh" else found(command, *args, **kwargs)

        monkeypatch.setattr(subprocess_utils.shutil, "which", which)

        result = runner.invoke(app, ["verify", "nightly"])

        assert result.exit_code == 2
        assert "gh not found" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert host.jobs == []
        assert store.list_audit_events(None, event=VERIFY_EVENT) == []


class TestTheNightlyScan:
    def test_only_the_gating_tables_cves_are_filed(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log(
            {"kew-agent:a": ["CVE-2026-11111"]}, triage=("CVE-2026-99999",)
        )

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        titles = [c[c.index("--title") + 1] for c in gh.creates()]
        assert titles == ["CVE-2026-11111 in an agent image"]
        assert report.scan.error is None
        assert store.list_audit_events(None, event=SCAN_FAILED_EVENT) == []

    def test_a_scan_that_fails_without_a_cve_files_one_issue_per_sha(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        refusal = (
            "scan-agent-image.sh: trivy 0.1.0 is not the pinned 0.58.1 — a "
            "different scanner version is a different gate"
        )
        host.scan_output = f"trivy 0.1.0\n{refusal}\n"
        host.scan_exit = 1

        first = run_nightly(config, store=store, repo_root=main.root, gh=gh)
        second = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        (create,) = gh.creates()
        title = create[create.index("--title") + 1]
        assert title.startswith(
            "image scan failed: the scan stopped before every image was gated: "
            "trivy 0.1.0 is not the pinned"
        )
        events = store.list_audit_events(None, event=SCAN_FAILED_EVENT)
        assert len(events) == 1
        assert _payload(events[0])["sha"] == main.green
        assert _payload(events[0])["issue"] == first.scan.issue
        assert first.scan.error is not None and not first.scan.duplicate
        assert second.scan.duplicate and second.scan.issue == first.scan.issue
        assert _health(store, main).state == "green"

    def test_a_host_that_answers_nothing_is_a_scan_failure(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = "docker: Cannot connect to the Docker daemon\n"
        host.scan_exit = 125

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        stopped = (
            "the scan stopped before every image was gated: "
            "docker: Cannot connect to the Docker daemon"
        )
        assert report.scan.error == stopped
        (create,) = gh.creates()
        assert f"image scan failed: {stopped}" in create


def _set_t(main: Main, text: str) -> None:
    """Rewrite test t's file on main."""
    (main.root / "kew" / "tests" / "test_t.py").write_text(text, encoding="utf-8")
    _commit(main.root, "rewrite t")
    main.push()


def _reproduced_red(
    main: Main, config: DispatchConfig, store: Any, host: FakeHost, failing: str
) -> str:
    """A green nightly, then a merge whose nightly fails *failing*, alone too."""
    host.failing = failing
    run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())
    (red,) = main.merges(1, breaks_at=1)
    run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())
    assert _health(store, main).reproduced == "reproduced"
    return red


def _land_fix(
    main: Main, config: DispatchConfig, store: Any, text: str
) -> tuple[Any, str]:
    """A PR that rewrites test t and repairs the break, verified, then landed."""
    _git(main.root, "checkout", "--quiet", "-b", "fix-t", "main")
    (main.root / "kew" / "tests" / "test_t.py").write_text(text, encoding="utf-8")
    fix = _commit(main.root, "fix t", state="ok")
    _git(main.root, "checkout", "--quiet", "main")
    candidate, result = run_verify(config, store=store, ref=fix, repo_root=main.root)
    assert result.outcome == "passed", result.log
    return result, main.squash_land(candidate.ref_sha, "fix t (#400)")


_PARAMETRIZED = (
    "import pytest\n\n"
    '@pytest.mark.parametrize("case", {cases})\n'
    "def test_t(case):\n    assert True\n"
)
_CLASSES = (
    "class TestA:\n    def test_t(self):\n        assert True\n\n\n"
    "class TestB:\n    def test_t(self):\n        assert True\n"
)


class TestWhatALandingMustShowToClearARed:
    """A landed PR clears a reproduced red only when its own verify ran each
    failing node id, exactly, and saw it pass (#3195)."""

    @pytest.mark.parametrize(
        ("base", "failing", "fixed", "outcome"),
        [
            pytest.param(
                _PARAMETRIZED.format(cases='["a", "b"]'),
                "tests/test_t.py::test_t[b]",
                _PARAMETRIZED.format(cases='["a"]'),
                "absent",
                id="dropped-case",
            ),
            pytest.param(
                _T_PASSES,
                _TEST_T,
                "import pytest\n\n@pytest.mark.skip\ndef test_t():\n    assert True\n",
                "skipped",
                id="added-skip",
            ),
            pytest.param(
                _T_PASSES,
                _TEST_T,
                "import pytest\n\n@pytest.mark.xfail(strict=False)\n"
                "def test_t():\n    assert True\n",
                "xpassed",
                id="added-xfail",
            ),
            pytest.param(
                _CLASSES,
                "tests/test_t.py::TestA::test_t",
                "class TestB:\n    def test_t(self):\n        assert True\n",
                "absent",
                id="wrong-class",
            ),
        ],
    )
    def test_a_landing_that_did_not_pass_the_exact_id_does_not_clear(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        base: str,
        failing: str,
        fixed: str,
        outcome: str,
    ) -> None:
        _set_t(main, base)
        red = _reproduced_red(main, config, store, host, failing)

        _result, _landed = _land_fix(main, config, store, fixed)

        newest = _payload(store.list_audit_events(1, event=VERIFY_EVENT)[0])
        assert newest["red_tests"] == {failing: outcome}
        health = _health(store, main)
        assert health.red and health.sha == red

    def test_a_landing_that_passed_the_exact_id_clears_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        failing = "tests/test_t.py::TestA::test_t"
        _set_t(main, _CLASSES)
        red = _reproduced_red(main, config, store, host, failing)

        result, landed = _land_fix(main, config, store, _CLASSES)

        assert result.red_outcomes == {failing: "passed"}
        health = _health(store, main)
        assert health.state == "green"
        assert (health.cleared_red, health.cleared_by) == (red, landed)

    def test_a_verify_with_no_red_main_evidence_runs_no_phase(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())
        change = main.branch("change", state=None)

        _candidate, result = run_verify(
            config, store=store, ref=change, repo_root=main.root
        )

        (job,) = [j for j in host.named(JOB_NAME) if j["sha"] == change]
        assert "kew-red-main" not in job["command"]
        assert result.red_outcomes is None


class TestTheCommandsRunInAFreshProcess:
    """No preflight before them: the allowlist starts empty (#3195)."""

    def test_main_health_reads_a_red_main_with_nothing_registered(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        from kew import subprocess_utils

        merges = _red_main(main, config, store, FakeGh())
        subprocess_utils.TOOL_PATHS.clear()

        result = runner.invoke(app, ["verify", "main-health"])

        assert result.exit_code == 1, result.output
        assert not isinstance(result.exception, ValueError)
        assert f"main is red at {merges[-1][:12]}" in result.output

    def test_main_health_clear_runs_with_nothing_registered(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        from kew import subprocess_utils

        merges = _flaky_red_main(main, config, store, host, FakeGh())
        subprocess_utils.TOOL_PATHS.clear()

        result = runner.invoke(
            app,
            ["verify", "main-health", "clear", merges[-1]]
            + ["--issue", "101", "--reason", "t flaked"],
        )

        assert result.exit_code == 0, result.output
        (event,) = store.list_audit_events(None, event=CLEARED_EVENT)
        assert _payload(event)["sha"] == merges[-1]


#: Gates that fail with no pytest FAILED/ERROR line, as their logs show it.
_GATE_LOGS = {
    "conftest": (
        "ImportError while loading conftest "
        "'/home/runner/work/kew/kew/tests/conftest.py'.\n"
        "tests/conftest.py:3: in <module>\n"
        "    import nothing_here\n"
        "E   ModuleNotFoundError: No module named 'nothing_here'\n",
        "pytest (a conftest import)",
    ),
    "coverage": (
        "1200 passed in 300.00s\n"
        "FAIL Required test coverage of 85% not reached. Total coverage: 80.12%\n",
        "the coverage floor",
    ),
    "ruff": (
        "src/kew/x.py:1:1: F401 [*] `os` imported but unused\nFound 1 error.\n",
        "ruff check",
    ),
    "ui-check": (
        "> kew-console@0.0.0 ui-check\nui-check: the epics surface rendered no rows\n",
        "the browser ui-check",
    ),
}


class TestAGateFailureWithNoFailingTest:
    """Any failure of a job that ran its gates is red, test ids or not (#3195)."""

    @pytest.mark.parametrize("gate", sorted(_GATE_LOGS))
    def test_it_reads_red_and_files_the_gate_and_its_log_tail(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        gate: str,
    ) -> None:
        gh = FakeGh()
        log, name = _GATE_LOGS[gate]
        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        (red,) = main.merges(1, breaks_at=1)
        host.red_log = log

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        assert report.verdict == "red"
        assert host.named("main-bisect") == []
        health = _health(store, main)
        assert (health.state, health.sha, health.tests) == ("red", red, ())
        assert health.reproduced is None
        (create,) = gh.creates()
        title = create[create.index("--title") + 1]
        body = create[create.index("--body") + 1]
        assert title == f"main is red at {red[:12]}: {name}"
        assert f"**Failing gate:** {name}" in body
        # The log's last line rides along, whitespace normalized.
        assert " ".join(log.strip().splitlines()[-1].split()) in body
        assert "/home/runner/work" not in body
        (filed,) = store.list_audit_events(None, event=REGRESSION_EVENT)
        assert _payload(filed)["gate"] == name

    def test_a_landing_does_not_clear_it_and_the_audited_clear_does(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())
        (red,) = main.merges(1, breaks_at=1)
        host.red_log = _GATE_LOGS["coverage"][0]
        run_nightly(config, store=store, repo_root=main.root, gh=FakeGh())

        _result, _landed = _land_fix(main, config, store, _T_PASSES)

        health = _health(store, main)
        assert health.red and health.sha == red
        cleared = runner.invoke(
            app,
            ["verify", "main-health", "clear", red]
            + ["--issue", "101", "--reason", "coverage back over the floor"],
        )
        assert cleared.exit_code == 0, cleared.output
        assert _health(store, main).state == "green"

    def test_the_nightly_exits_one_for_it(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The CLI files through the gh on PATH: a stub that files issue 7.
        bin_dir = tmp_path / "stub-gh"
        bin_dir.mkdir()
        stub = bin_dir / "gh"
        stub.write_text(
            '#!/bin/sh\n[ "$2" = create ] && [ "$1" = issue ] && '
            "echo https://github.com/o/r/issues/7\nexit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        main.merges(1, breaks_at=1)
        host.red_log = _GATE_LOGS["ruff"][0]

        result = runner.invoke(app, ["verify", "nightly"])

        assert result.exit_code == 1, result.output
        assert "regression filed issue #7" in result.output


class TestAJobThatNeverRanItsGates:
    @pytest.mark.parametrize("never_ran", sorted(_NEVER_RAN))
    def test_it_reads_incomplete_and_leaves_main_green(
        self,
        main: Main,
        config: DispatchConfig,
        host: FakeHost,
        store: Any,
        never_ran: str,
    ) -> None:
        gh = FakeGh()
        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        main.merges(1, breaks_at=1)
        host.never_ran = never_ran

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        assert report.verdict == "incomplete"
        assert report.regression is None
        assert host.named("main-bisect") == []
        assert gh.creates() == []
        health = _health(store, main)
        assert (health.state, health.sha) == ("green", main.green)

    def test_the_nightly_exits_two_for_it(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        main.merges(1, breaks_at=1)
        host.never_ran = "preflight"

        result = runner.invoke(app, ["verify", "nightly"])

        assert result.exit_code == 2, result.output
        assert "incomplete" in result.output


class TestTheTipProbe:
    def test_a_tip_probe_that_finds_no_test_is_no_verdict(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        main.merges(2, breaks_at=1)
        host.probe_exit = 4

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        assert report.regression is not None
        assert report.regression.reproduced is None
        assert report.regression.culprit is None
        assert len(host.named("main-bisect")) == 1
        assert _health(store, main).reproduced is None

    @pytest.mark.parametrize("exists", [False, True])
    def test_the_tip_script_reads_a_missing_test_as_no_verdict(
        self, tmp_path: Path, exists: bool
    ) -> None:
        tests = tmp_path / "kew" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_a.py").write_text(
            "def test_passes():\n    assert True\n", encoding="utf-8"
        )
        uv = f'uv() {{ [ "$1" = sync ] && return 0; shift; {sys.executable} -m "$@"; }}'
        test = "tests/test_a.py::test_passes" if exists else "tests/test_b.py::test_x"

        done = subprocess.run(
            ["sh", "-c", f"{uv}\n{probe_script([test], at_tip=True)}"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

        expected = False if exists else None
        assert probe_verdict(done.returncode, at_tip=True) is expected
        assert probe_verdict(4) is False  # inside the window: not there yet

    def test_a_bracketed_id_with_a_dash_is_read_whole(self) -> None:
        log = _summary_log("FAILED tests/test_a.py::test_x[a - b] - AssertionError: no")

        assert failing_tests(log) == ("tests/test_a.py::test_x[a - b]",)


class TestTheScanReadsEveryAdvisory:
    def test_a_ghsa_in_one_image_is_filed_beside_a_cve_in_another(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log(
            {
                "kew-agent:a": ["CVE-2026-11111"],
                "kew-agent:b": ["GHSA-abcd-efgh-ijkm"],
                "kew-switchyard:c": ["RUSTSEC-2026-0001"],
            }
        )

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        titles = [c[c.index("--title") + 1] for c in gh.creates()]
        assert titles == [
            "CVE-2026-11111 in an agent image",
            "GHSA-abcd-efgh-ijkm in an agent image",
            "RUSTSEC-2026-0001 in an agent image",
        ]
        assert report.scan.error is None

    def test_only_a_ghsa_files_the_advisory_and_no_scan_failure(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log({"kew-agent:a": ["GHSA-abcd-efgh-ijkm"]})

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        (create,) = gh.creates()
        assert "GHSA-abcd-efgh-ijkm in an agent image" in create
        assert report.scan.error is None
        assert store.list_audit_events(None, event=SCAN_FAILED_EVENT) == []

    def test_a_finding_with_no_readable_id_fails_its_image_beside_a_cve(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log(
            {"kew-agent:a": ["CVE-2026-11111"], "kew-agent:b": []},
            unreadable=("kew-agent:b",),
        )

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        titles = [c[c.index("--title") + 1] for c in gh.creates()]
        assert titles[0] == "CVE-2026-11111 in an agent image"
        assert titles[1].startswith("image scan failed: ")
        assert "kew-agent:b" in titles[1]
        assert report.scan.error is not None and "kew-agent:a" not in report.scan.error

    def test_a_gate_that_could_not_scan_is_a_scan_failure_not_a_finding(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = _scan_log({"kew-agent:a": []}, errors=("kew-agent:a",))

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        (create,) = gh.creates()
        title = create[create.index("--title") + 1]
        assert title == (
            "image scan failed: the gate could not scan kew-agent:a: trivy exited 1"
        )
        assert report.scan.advisories == {}

    def test_the_pinned_scanner_line_is_not_a_refusal(self) -> None:
        log = _CLEAN_SCAN

        assert read_scan(log, 1, None).failures == ("the image scan exited 1",)
        assert read_scan(log, 0, None).failures == ()


class TestAScanThatStopsPartWay:
    """A scan is complete only once its closing summary is in the log: a CVE
    filed from the first image must not hide that the rest went unscanned."""

    def _partial(self) -> str:
        return _scan_log(
            {"kew-agent:a": ["CVE-2026-11111"]},
            stopped="ERROR: failed to solve: process did not complete",
        )

    def test_a_build_that_dies_after_a_finding_is_a_scan_failure(self) -> None:
        found = read_scan(self._partial(), 1, None)

        assert found.advisories == ("CVE-2026-11111",)
        assert found.failures == (
            "the scan stopped before every image was gated: "
            "ERROR: failed to solve: process did not complete",
        )

    def test_a_scan_that_timed_out_after_a_finding_is_a_scan_failure(self) -> None:
        found = read_scan(self._partial(), None, "timed out after 3600s")

        assert found.advisories == ("CVE-2026-11111",)
        assert found.failures == (
            "the scan stopped before every image was gated: timed out after 3600s",
        )

    def test_the_nightly_files_the_cve_and_the_stopped_scan(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        host.scan_output = self._partial()

        report = run_nightly(config, store=store, repo_root=main.root, gh=gh)

        titles = [c[c.index("--title") + 1] for c in gh.creates()]
        assert titles[0] == "CVE-2026-11111 in an agent image"
        assert titles[1].startswith(
            "image scan failed: the scan stopped before every image was gated"
        )
        assert report.scan.error is not None


#: What a passing run prints before a later gate fails: semgrep's clean
#: summary (it runs early in verify.sh) and pytest's.
_SEMGREP_CLEAN = "Ran 150 rules on 221 files: 0 findings.\n"
_PASSING_RUN = _SEMGREP_CLEAN + "4123 passed, 12 skipped in 811.20s\n"

#: A later gate's failure and the name it must get.
_LATER_FAILURES = {
    "docs": (
        "docs gate FAILED: point design references at a public page or at git "
        "history (1702613a).\n",
        "the docs gate (check-docs.sh)",
    ),
    "coverage": (
        "FAIL Required test coverage of 85% not reached. Total coverage: 80.12%\n",
        "the coverage floor",
    ),
    "mypy": (
        "src/kew/x.py:3: error: Incompatible types\n"
        "Found 1 error in 1 file (checked 423 source files)\n",
        "mypy",
    ),
    "xenon": ('ERROR:xenon:block "src/kew/x.py:9 f" has a rank of C\n', "xenon"),
    "lint-imports": ("Contracts: 3 kept, 1 broken.\n", "lint-imports"),
    "semgrep": ("Ran 150 rules on 221 files: 2 findings.\n", "semgrep"),
}


class TestTheFailingGateIsNamedRight:
    @pytest.mark.parametrize("gate", sorted(_LATER_FAILURES))
    def test_a_passing_semgrep_summary_never_names_a_later_failure(
        self, gate: str
    ) -> None:
        failure, name = _LATER_FAILURES[gate]

        found = failing_gate(_PASSING_RUN + failure)

        assert found.gate == name
        assert found.stream == "the job's output"
        assert found.tail[-1] == " ".join(failure.strip().splitlines()[-1].split())

    def test_on_a_host_that_appends_stderr_the_stdout_failure_names_the_gate(
        self,
    ) -> None:
        coverage, _name = _LATER_FAILURES["coverage"]
        stdout = "4123 passed in 811.20s\n" + coverage
        stderr = _SEMGREP_CLEAN + "".join(f"warning {n}\n" for n in range(40))

        found = failing_gate(with_stderr(stdout, stderr))

        assert found.gate == "the coverage floor"
        assert found.stream == "the job's stdout"
        assert any("Required test coverage" in line for line in found.tail)
        assert not any(line.startswith("warning") for line in found.tail)

    def test_a_failure_printed_to_stderr_takes_its_tail_from_stderr(self) -> None:
        docs, _name = _LATER_FAILURES["docs"]
        stdout = "4123 passed in 811.20s\n" + "".join(f"ok {n}\n" for n in range(40))

        found = failing_gate(with_stderr(stdout, _SEMGREP_CLEAN + docs))

        assert found.gate == "the docs gate (check-docs.sh)"
        assert found.stream == "the job's stderr"
        assert any("docs gate FAILED" in line for line in found.tail)

    def test_the_issue_says_which_stream_its_tail_is_from(
        self, main: Main, config: DispatchConfig, host: FakeHost, store: Any
    ) -> None:
        gh = FakeGh()
        coverage, _name = _LATER_FAILURES["coverage"]
        run_nightly(config, store=store, repo_root=main.root, gh=gh)
        main.merges(1, breaks_at=1)
        host.red_log = with_stderr("4123 passed\n" + coverage, _SEMGREP_CLEAN)

        run_nightly(config, store=store, repo_root=main.root, gh=gh)

        (create,) = gh.creates()
        body = create[create.index("--body") + 1]
        assert "**Failing gate:** the coverage floor" in body
        assert "The last lines of the job's stdout" in body
        assert "Required test coverage of 85% not reached" in body


_PEM_BODY = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" + "a" * 12] * 30


class TestWhatAPublicIssueMayQuote:
    def test_a_pem_body_whose_begin_is_above_the_tail_is_dropped(self) -> None:
        log = "\n".join(
            [
                "-----BEGIN " + "PRIVATE KEY-----",
                *_PEM_BODY,
                "-----END PRIVATE KEY-----",
            ]
            + [f"line {n}" for n in range(5)]
        )

        tail = log_tail(log)

        assert not any("MIIEvQIB" in line for line in tail)
        assert tail[-5:] == tuple(f"line {n}" for n in range(5))

    def test_an_orphan_base64_run_is_dropped(self) -> None:
        tail = log_tail("\n".join([*_PEM_BODY, "-----END PRIVATE KEY-----", "done"]))

        assert tail == ("done",)

    @pytest.mark.parametrize(
        ("line", "secret"),
        [
            ("token xox" + "b-1234567890-0987654321-AbCdEfGhIjKl", "AbCdEfGhIjKl"),
            ("user xox" + "p-1234567890-0987654321-ZyXwVu", "ZyXwVu"),
            ("connect postgres://admin:hunter2@db.example:5432/kew", "hunter2"),
            ("cache redis://:s3cretpw@cache:6379/0", "s3cretpw"),
            ("queue amqp://guest:rabbitpw@mq:5672/", "rabbitpw"),
            (
                "reached kew-verify.c.example-proj-12345.internal:22",
                "example-proj-12345",
            ),
            ("dns metadata.google.internal answered", "metadata.google"),
        ],
        ids=[
            "bot-token",
            "user-token",
            "postgres-url",
            "redis-url",
            "amqp-url",
            "internal-host",
            "metadata-host",
        ],
    )
    def test_secrets_and_internal_hosts_are_redacted(
        self, line: str, secret: str
    ) -> None:
        (cleaned,) = log_tail(line)

        assert secret not in cleaned

    @pytest.mark.parametrize(
        "line",
        [
            "FAILED tests/projects/test_x.py::test_y - assert 0",
            "src/kew/projects/board.py:42: AssertionError",
            "E AttributeError: obj@property has no setter",
            "installed react@18.3.1",
        ],
    )
    def test_a_failing_test_id_or_file_line_survives_cleaning(self, line: str) -> None:
        """A path segment named ``projects/`` is not a GCP resource and
        ``obj@property`` is not an address: the red-main tail keeps them."""
        (cleaned,) = log_tail(line)

        assert cleaned == line

    def test_the_scan_refusal_and_the_host_error_are_cleaned(self) -> None:
        refusal = (
            "scan-agent-image.sh: trivy not found — install it. A host kew "
            "provisioned installs it from /Users/dev/Projects/kew/kew/src/kew/"
            "hosts/provision/trivy.pin; re-run kew hosts provision on it."
        )

        stopped = read_scan(f"{refusal}\n", 1, None)
        timed_out = read_scan(
            "building\n",
            None,
            "timed out on vm.c.my-project.internal via postgres://admin:hunter2@db/x",
        )

        (failure,) = stopped.failures
        assert "/Users/dev" not in failure and "trivy.pin" in failure
        (failure,) = timed_out.failures
        assert "my-project" not in failure and "hunter2" not in failure


def _summary_log(*lines: str, captured: str = "") -> str:
    """A pytest run's tail: captured output, then its short test summary."""
    return (
        f"{captured}"
        f"{'=' * 27} short test summary info {'=' * 28}\n"
        + "".join(f"{line}\n" for line in lines)
        + f"{'=' * 25} 1 failed, 3 passed in 0.12s {'=' * 26}\n"
    )


_SESSION_START = f"{'=' * 29} test session starts {'=' * 30}\n"
_SUMMARY_HEADER = f"{'=' * 27} short test summary info {'=' * 28}\n"
#: A summary block and counts line a test prints to forge a pytest run.
_FORGED = (
    f"{_SUMMARY_HEADER}FAILED tests/test_evil.py::injected - planted\n"
    f"{'=' * 25} 1 failed in 0.10s {'=' * 26}\n"
)


def _verbose_run(*lines: str, captured: str = "") -> str:
    """A verbose pytest session as pytest prints it: its header, the failing
    test's report (where whatever the test *captured* lands), its short test
    summary of *lines*, and its counts line."""
    return (
        f"{_SESSION_START}collected 4 items\n\ntests/test_real.py F...  [100%]\n\n"
        f"{'=' * 35} FAILURES {'=' * 35}\n"
        f"{'_' * 30} test_it {'_' * 30}\n"
        "E       assert 0\n"
        "----------------------------- Captured stdout call -----\n"
        f"{captured}"
        f"{_SUMMARY_HEADER}"
        + "".join(f"{line}\n" for line in lines)
        + f"{'=' * 25} 1 failed, 3 passed in 0.12s {'=' * 26}\n"
    )


class TestOnlyTheShortSummaryNamesFailingTests:
    def test_a_failed_line_outside_the_short_summary_is_not_a_failing_test(
        self,
    ) -> None:
        log = _summary_log(
            "FAILED tests/test_real.py::test_it - AssertionError",
            captured=(
                "----------------------------- Captured stdout call -----\n"
                "FAILED tests/x.py::y\n"
                "ERROR tests/test_planted.py::test_z - planted\n"
            ),
        )

        assert failing_tests(log) == ("tests/test_real.py::test_it",)

    def test_a_log_with_no_short_summary_names_no_failing_test(self) -> None:
        assert failing_tests("FAILED tests/x.py::y\n") == ()

    def test_a_planted_summary_a_test_printed_names_no_failing_test(self) -> None:
        """A run printed inside the report reads, line for line, like the real
        run followed by one printed after it (an ``atexit`` print): which
        block is pytest's cannot be told, so neither is read."""
        log = _verbose_run(
            "FAILED tests/test_real.py::test_it - AssertionError", captured=_FORGED
        )

        assert failing_tests(log) == ()

    def test_a_summary_printed_after_the_run_names_no_failing_test(self) -> None:
        """An ``atexit`` print lands after pytest's counts line, where the
        last summary block would be read: it neither adds an id nor hides
        the real one."""
        log = _verbose_run("FAILED tests/test_real.py::test_it - assert 0") + _FORGED

        assert failing_tests(log) == ()

    def test_a_planted_run_then_a_printed_failures_rule_names_no_failing_test(
        self,
    ) -> None:
        """A printed ``FAILURES`` rule after a planted counts line would split
        one session into two runs, the first ending in the planted block."""
        log = _verbose_run(
            "FAILED tests/test_real.py::test_it - assert 0",
            captured=f"{_FORGED}{'=' * 35} FAILURES {'=' * 35}\n",
        )

        assert failing_tests(log) == ()

    @pytest.mark.parametrize(
        ("start", "counts"),
        [
            (_SESSION_START, f"{'=' * 25} 1 failed in 0.10s {'=' * 26}\n"),
            (_SESSION_START, "1 failed in 0.10s\n"),
            ("--- x on y ---\n", "1 failed in 0.10s\n"),
        ],
        ids=["session-header", "session-header-bare-counts", "job-line"],
    )
    def test_a_printed_run_start_after_a_planted_run_names_no_injected_id(
        self, start: str, counts: str
    ) -> None:
        """A printed run start would close the planted run as a run of its
        own and open a new one for the real summary."""
        planted = (
            f"{_SUMMARY_HEADER}FAILED tests/test_evil.py::injected - planted\n"
            f"{counts}{start}"
        )
        log = _verbose_run(
            "FAILED tests/test_real.py::test_it - assert 0", captured=planted
        )

        assert failing_tests(log) == ()

    def test_an_unclosed_planted_summary_names_no_failing_test(self) -> None:
        """Two summary headers in one run: one of them a test printed."""
        log = _summary_log(
            "FAILED tests/test_real.py::test_it - AssertionError",
            captured=(
                "------- Captured stdout call -------\n"
                f"{'=' * 27} short test summary info {'=' * 28}\n"
                "FAILED tests/x.py::t - planted\n"
                "_________________________ test_other _________________________\n"
                "FAILED tests/y.py::u - also planted\n"
            ),
        )

        assert failing_tests(log) == ()

    @staticmethod
    def _quiet(test: str, *, captured: str = "", counts: str = "1 failed") -> str:
        """A ``pytest -q`` run's output: no session header, bare counts line."""
        return (
            "F.                                                     [100%]\n"
            f"{'=' * 35} FAILURES {'=' * 35}\n"
            f"{'_' * 30} test_fails {'_' * 30}\n"
            f"{captured}"
            f"{'=' * 27} short test summary info {'=' * 28}\n"
            f"FAILED {test} - assert 0\n"
            f"{counts}, 1 passed in 0.05s\n"
        )

    def test_a_quiet_run_names_its_failing_test(self) -> None:
        assert failing_tests(self._quiet("tests/test_q.py::test_fails")) == (
            "tests/test_q.py::test_fails",
        )

    def test_two_quiet_runs_in_one_log_name_no_failing_test(self) -> None:
        """Two ``-q`` runs with no run start between them read, line for line,
        like one run whose test printed a whole ``-q`` run inside its report:
        which ids are pytest's cannot be told, so none is read. Failing ids
        move to pytest's structured report in #3411."""
        log = self._quiet("tests/test_a.py::test_x") + self._quiet(
            "tests/test_b.py::test_y", counts="1 failed, 2 warnings"
        )

        assert failing_tests(log) == ()

    def test_a_planted_summary_in_a_quiet_run_names_no_failing_test(self) -> None:
        """The ``-q`` form of a run printed inside the report: it reads like
        the real run followed by one printed after it."""
        planted = (
            "------- Captured stdout call -------\n"
            f"{'=' * 27} short test summary info {'=' * 28}\n"
            "FAILED tests/x.py::t - planted\n"
            "1 failed in 0.01s\n"
            f"{'_' * 30} test_other {'_' * 30}\n"
        )
        log = self._quiet("tests/test_real.py::test_it", captured=planted)

        assert failing_tests(log) == ()

    def test_a_log_mixing_verbose_and_quiet_runs_names_no_failing_test(
        self,
    ) -> None:
        log = _verbose_run("FAILED tests/test_a.py::test_x - assert 0") + self._quiet(
            "tests/test_b.py::test_y"
        )

        assert failing_tests(log) == ()

    def test_every_shard_and_every_session_names_its_failing_tests(self) -> None:
        policy = _verbose_run("FAILED tests/test_p.py::test_rule - Assert")
        suite = _verbose_run("FAILED tests/test_b.py::test_y - AssertionError")
        log = "\n".join(
            [
                f"--- shard-1 on host-a ---\n{policy}{suite}",
                f"--- shard-2 on host-b ---\n{_verbose_run('ERROR tests/test_c.py')}",
            ]
        )

        assert failing_tests(log) == (
            "tests/test_p.py::test_rule",
            "tests/test_b.py::test_y",
            "tests/test_c.py",
        )


class TestTheRegressionTitle:
    def _title(self, tmp_path: Path, tests: tuple[str, ...]) -> tuple[str, str]:
        issued: list[list[str]] = []

        def gh(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            issued.append(argv)
            out = "https://github.com/o/r/issues/41\n" if "issue" in argv else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        tmp_path.mkdir(exist_ok=True)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            file_regression(store, Regression("a" * 40, tests), repo="o/r", gh=gh)
        [create] = [argv for argv in issued if argv[1:3] == ["issue", "create"]]
        return (
            create[create.index("--title") + 1],
            create[create.index("--body") + 1],
        )

    def test_the_regression_title_names_only_strict_test_ids(
        self, tmp_path: Path
    ) -> None:
        crafted = "tests/test_a.py::test_x @everyone see https://evil.example"
        spaced = "tests/test_b.py::test_y[has a space]"
        tests = (crafted, spaced, "tests/test_c.py::TestK::test_z[1-a.b]")

        title, body = self._title(tmp_path, tests)

        assert title == (
            f"main is red at {'a' * 12}: tests/test_c.py::TestK::test_z[1-a.b]"
        )
        assert "@everyone" not in title and "evil" not in title
        assert "`tests/test_a.py::test_x @everyone see https://evil.example`" in body

    def test_a_regression_title_with_no_strict_id_counts_its_failing_tests(
        self, tmp_path: Path
    ) -> None:
        one, _ = self._title(tmp_path / "one", ("tests/test_a.py::test_x <img src=x>",))
        two, _ = self._title(
            tmp_path / "two",
            ("tests/test_a.py::test_x <img src=x>", "tests/test_b.py::t[a b]"),
        )

        assert one == f"main is red at {'a' * 12}: 1 failing test"
        assert two == f"main is red at {'a' * 12}: 2 failing tests"

    def test_a_regression_title_names_a_test_file_that_failed_to_collect(
        self, tmp_path: Path
    ) -> None:
        title, _ = self._title(tmp_path, ("tests/test_c.py",))

        assert title == f"main is red at {'a' * 12}: tests/test_c.py"


@pytest.fixture
def launchd(
    tmp_path: Path, main: Main, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """A macOS host: a fake launchctl and a HOME of its own."""
    with fake_launchctl(tmp_path, monkeypatch) as bin_dir:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KEW_PROJECT", str(main.root))
        monkeypatch.setattr(sys, "platform", "darwin")
        yield bin_dir


def _kew(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "kew"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


class TestTheLaunchdAgent:
    def test_install_writes_an_agent_that_runs_the_nightly_at_01_30(
        self, launchd: Path, main: Main, config: DispatchConfig, tmp_path: Path
    ) -> None:
        binary = _kew(tmp_path)

        result = runner.invoke(
            app, ["verify", "nightly", "install", "--kew-binary", str(binary)]
        )

        assert result.exit_code == 0, result.output
        label = nightly_label(main.root.resolve())
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        agent = plistlib.loads(plist.read_bytes())
        assert agent["ProgramArguments"] == [str(binary.resolve()), "verify", "nightly"]
        assert agent["StartCalendarInterval"] == {"Hour": 1, "Minute": 30}
        assert "KeepAlive" not in agent
        assert agent["EnvironmentVariables"][NIGHTLY_MARK] == "1"
        assert any(line.startswith("bootstrap ") for line in calls(launchd))

        removed = runner.invoke(app, ["verify", "nightly", "uninstall"])

        assert removed.exit_code == 0, removed.output
        assert not plist.exists()

    def test_install_refuses_off_macos(
        self, main: Main, config: DispatchConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        result = runner.invoke(app, ["verify", "nightly", "install"])

        assert result.exit_code == 2
        assert "macOS" in result.output

    def test_an_unknown_word_after_a_ref_is_refused(
        self, main: Main, config: DispatchConfig
    ) -> None:
        result = runner.invoke(app, ["verify", "HEAD", "extra"])

        assert result.exit_code == 2
        assert "unexpected arguments" in result.output
