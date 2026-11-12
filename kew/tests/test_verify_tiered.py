"""A PR's merge verify on the change's selection, behind ``[verify] suite`` (#3202).

A fixture repository with an ``origin`` stands in for the project: a tiny
``kew/`` with a leaf module, a core module most tests import, a policy test,
and a ``scripts/verify.sh`` that only says it ran. The verify host is the local
one with its job execution faked at the process boundary
(:meth:`LocalProvider._execute`). A selected verify's own pytest phases are run
for real, with real pytest, against the shipped checkout, so a failing selected
test is the job's real verdict. The 26-hour window reads a pinned clock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.fake_gcloud import launchable
from support.held_out_log import opened
from support.verify_switches import SUITE_JOB_SWITCHES, environ_without_switches
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, load_config
from kew.hosts import Job, LocalProvider
from kew.main_health import NIGHTLY_KIND, read_main_health
from kew.merge_verify import (
    PREFLIGHT_PASSED_MARK,
    SUITE_ENV,
    SUITE_SELECTED,
    VERIFY_EVENT,
    run_verify,
    summary_line,
)
from kew.run_store import SqliteRunStore
from kew.verify_suite import SUITE_TIERED, SuiteRequest, choose_suite

runner = CliRunner()

KEW_DIR = Path(__file__).resolve().parents[1]
VERIFY_SH = KEW_DIR / "scripts" / "verify.sh"

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)

#: ``uv`` on the host, as a shell function: ``uv run X`` is ``python -m X``
#: with this interpreter, which has pytest and pytest-xdist.
_UV = f'uv() {{ shift; "{sys.executable}" -m "$@"; }}'

_CORE_TESTS = {
    f"kew/tests/test_core{i}.py": (
        f"from kew.core import VALUE\n\n\ndef test_core{i}():\n    assert VALUE\n"
    )
    for i in range(6)
}

_PROJECT = {
    "kew/pyproject.toml": (
        "[tool.pytest.ini_options]\n"
        'pythonpath = ["src"]\n'
        'markers = ["serial: runs alone", "policy: a tree rule"]\n'
    ),
    "kew/scripts/verify.sh": (
        'echo "fixture verify.sh ran: KEW_VERIFY_SUITE=${KEW_VERIFY_SUITE:-all}"\n'
    ),
    "kew/src/kew/__init__.py": "",
    "kew/src/kew/leaf.py": "VALUE = 1\n",
    "kew/src/kew/core.py": "VALUE = 2\n",
    "kew/tests/conftest.py": "",
    "kew/tests/test_leaf.py": (
        "from kew.leaf import VALUE\n\n\ndef test_leaf():\n    assert VALUE == 1\n"
    ),
    "kew/tests/test_rules.py": (
        "import pytest\n\nimport kew.core\n\n\n@pytest.mark.policy\n"
        "def test_rule():\n    assert kew.core.VALUE\n"
    ),
    **_CORE_TESTS,
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


class Project:
    """The fixture project, its origin, and its branches."""

    def __init__(self, root: Path, origin: Path) -> None:
        self.root = root
        origin.mkdir(parents=True, exist_ok=True)
        _git(origin, "init", "--quiet", "--bare", "-b", "main", ".")
        _git(root, "init", "--quiet", "-b", "main", ".")
        (root / ".git" / "info" / "exclude").write_text(
            "logs/\nkew.toml\nhost-locks/\n", encoding="utf-8"
        )
        _write(root, _PROJECT)
        _git(root, "add", "--all")
        _git(root, "commit", "--quiet", "-m", "base")
        _git(root, "remote", "add", "origin", str(origin))
        _git(root, "push", "--quiet", "origin", "main")
        _git(root, "fetch", "--quiet", "origin")
        self.main = _git(root, "rev-parse", "HEAD")

    def branch(self, name: str, files: dict[str, str]) -> str:
        _git(self.root, "checkout", "--quiet", "-b", name, "main")
        _write(self.root, files)
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "--quiet", "-m", name)
        sha = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "--quiet", "main")
        return sha

    def squash_land(self, sha: str) -> str:
        tree = _git(self.root, "rev-parse", f"{sha}^{{tree}}")
        landed = _git(self.root, "commit-tree", tree, "-p", "main", "-m", "land (#9)")
        _git(self.root, "update-ref", "refs/heads/main", landed)
        _git(self.root, "checkout", "--quiet", "-f", "main")
        _git(self.root, "push", "--quiet", "origin", "main")
        _git(self.root, "fetch", "--quiet", "origin")
        return landed


def _run_selected_phases(checkout: Path, command: str) -> tuple[int, str]:
    """Run the job's suite step for real, from its selected-run function on.

    The fixture's ``scripts/verify.sh`` only says it ran; the selected pytest
    phases after it run real pytest on the shipped checkout's own tests.
    """
    lines = command.splitlines()
    start = lines.index("kew_selected_run() {")
    end = next(i for i, line in enumerate(lines) if 'verify_status="$?"' in line)
    step = "\n".join(["kew_env_probed=0", *lines[start : end + 1]])
    done = subprocess.run(
        # ``sh``, as both hosts run a job (``sh -c``).
        ["sh", "-c", f'set -eu\n{_UV}\n{step}\nexit "$verify_status"\n'],
        cwd=checkout / "kew",
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(checkout),
            "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
        },
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout + done.stderr


class FakeHost:
    """The verify host's job execution: a full suite passes, and a selected
    one runs its own pytest phases for real."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def __call__(self, provider: LocalProvider, job: Job) -> tuple[int, str]:
        checkout = provider.checkout
        assert checkout is not None
        self.jobs.append({"name": job.name, "command": job.command})
        if "kew_selected_run() {" not in job.command:
            return 0, f"{PREFLIGHT_PASSED_MARK}\ngate passed\n{_HELD_OUT_PASSED}"
        code, output = _run_selected_phases(checkout, job.command)
        return code, f"{PREFLIGHT_PASSED_MARK}\n{output}{_HELD_OUT_PASSED}"


@pytest.fixture
def project(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Project:
    return Project(tmp_path, tmp_path_factory.mktemp("origin") / "origin.git")


def _config(
    project: Project, monkeypatch: pytest.MonkeyPatch, suite: str | None
) -> DispatchConfig:
    toml = project.root / "kew.toml"
    line = "" if suite is None else f'suite = "{suite}"\n'
    toml.write_text(
        '[project]\nrepo = "o/r"\n\n'
        f'[logging]\ndir = "{project.root / "logs"}"\n\n'
        f'[verify]\nhost = "local"\n{line}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(project.root))
    monkeypatch.delenv("GATE_RE", raising=False)
    return load_config(toml)


@pytest.fixture
def tiered(project: Project, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    return _config(project, monkeypatch, SUITE_TIERED)


@pytest.fixture
def default(project: Project, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    return _config(project, monkeypatch, None)


@pytest.fixture
def host(project: Project, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    fake = FakeHost()

    def _execute(provider: LocalProvider, job: Job) -> tuple[int, str]:
        return fake(provider, job)

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    monkeypatch.chdir(project.root)
    return fake


@pytest.fixture
def store(project: Project) -> Iterator[SqliteRunStore]:
    log_dir = project.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as opened_store:
        yield opened_store


def _payload(event: Any) -> dict[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _events(store: SqliteRunStore) -> list[dict[str, Any]]:
    """Every ``verify_completed`` payload, oldest first."""
    events = store.list_audit_events(None, event=VERIFY_EVENT)
    return [_payload(e) for e in sorted(events, key=lambda e: e.id)]


def _nightly(config: DispatchConfig, store: SqliteRunStore, project: Project) -> Any:
    """A green full verify of main, as the nightly records one."""
    run_verify(
        config,
        store=store,
        ref="origin/main",
        repo_root=project.root,
        kind=NIGHTLY_KIND,
    )
    (event,) = store.list_audit_events(None, event=VERIFY_EVENT)
    return datetime.fromisoformat(event.timestamp)


def _audit_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    """Pin the clock the audit trail stamps its events with to *at*."""
    import kew.audit

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(kew.audit, "datetime", _Pinned)


def _landed_full_verify(
    config: DispatchConfig, store: SqliteRunStore, project: Project
) -> str:
    """A PR whose verify fell back to the full suite, passed, and landed
    unchanged: main-health evidence, but no nightly."""
    sha = project.branch("conftest", {"kew/tests/conftest.py": "X = 1\n"})
    candidate, result = run_verify(config, store=store, ref=sha, repo_root=project.root)
    assert not candidate.suite.selected and result.outcome == "passed", result.log
    return project.squash_land(sha)


_LEAF_CHANGE = {"kew/src/kew/leaf.py": "VALUE = 1\nOTHER = 3\n"}


class TestTieredSelection:
    def test_a_leaf_change_runs_the_selected_files_and_policy(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, result = run_verify(
            tiered, store=store, ref=sha, repo_root=project.root
        )

        assert result.outcome == "passed", result.log
        assert "KEW_VERIFY_SUITE=selected" in result.log, candidate.suite
        # The parallel phase ran the two selected files' tests, the serial
        # phase found none in them, and the policy run read the whole tree.
        assert "2 workers [2 items]" in result.log, result.log
        assert "collected 2 items / 2 deselected / 0 selected" in result.log
        assert "collected 8 items / 7 deselected / 1 selected" in result.log
        assert " suite=selected files=2" in summary_line(candidate, result)
        recorded = _events(store)[-1]
        assert recorded["suite"] == "selected"
        assert recorded["suite_selection"]["mode"] == "selected"
        assert recorded["suite_selection"]["files"] == [
            "tests/test_leaf.py",
            "tests/test_rules.py",
        ]
        assert recorded["suite_selection"]["selector_version"] >= 1
        assert re.fullmatch(r"[0-9a-f]{64}", recorded["suite_selection"]["digest"])

    def test_a_failing_selected_test_fails_the_verify(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("break-leaf", {"kew/src/kew/leaf.py": "VALUE = 7\n"})

        candidate, result = run_verify(
            tiered, store=store, ref=sha, repo_root=project.root
        )

        assert candidate.suite.selected
        assert result.outcome == "failed"
        assert result.exit_code == 1
        assert "test_leaf" in result.log
        assert _events(store)[-1]["outcome"] == "failed"

    def test_a_conftest_change_runs_the_full_suite(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("conftest", {"kew/tests/conftest.py": "X = 1\n"})

        candidate, result = run_verify(
            tiered, store=store, ref=sha, repo_root=project.root
        )

        assert not candidate.suite.selected
        assert "conftest.py" in candidate.suite.reason
        assert SUITE_ENV not in host.jobs[-1]["command"]
        assert (
            'bash scripts/verify.sh || verify_status="$?"' in (host.jobs[-1]["command"])
        )
        recorded = _events(store)[-1]
        assert recorded["suite"] == "full"
        assert recorded["suite_selection"]["mode"] == "full"
        assert recorded["suite_selection"]["digest"] is not None
        assert " suite=full" in summary_line(candidate, result)

    def test_a_27_hour_old_nightly_runs_the_full_suite_and_says_why(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        at = _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        fresh, _ = run_verify(
            tiered,
            store=store,
            ref=sha,
            repo_root=project.root,
            now=at + timedelta(hours=25),
        )
        stale, _ = run_verify(
            tiered,
            store=store,
            ref=sha,
            repo_root=project.root,
            now=at + timedelta(hours=27),
        )

        assert fresh.suite.selected
        assert not stale.suite.selected
        assert stale.suite.reason.startswith("the nightly is stale")
        assert "no green nightly of a commit on main within 26 h" in (
            stale.suite.reason
        )
        assert SUITE_ENV not in host.jobs[-1]["command"]
        recorded = _events(store)[-1]["suite_selection"]
        assert recorded["mode"] == "full"
        assert "26 h" in recorded["reason"]

    def test_with_no_full_verify_on_record_the_verify_runs_full(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, _ = run_verify(tiered, store=store, ref=sha, repo_root=project.root)

        assert not candidate.suite.selected
        assert candidate.suite.reason.startswith("the nightly is missing")

    def test_a_landed_full_verify_does_not_stand_in_for_a_stale_nightly(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start = datetime.now(UTC)
        _audit_clock(monkeypatch, start)
        _nightly(tiered, store, project)
        _audit_clock(monkeypatch, start + timedelta(hours=25))
        landed = _landed_full_verify(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, _ = run_verify(
            tiered,
            store=store,
            ref=sha,
            repo_root=project.root,
            now=start + timedelta(hours=27),
        )

        # The landing still reads as main's health, two hours old ...
        health = read_main_health(store, project.root, base_ref="origin/main")
        assert (health.state, health.sha, health.via) == ("green", landed, "pr")
        # ... but only a nightly keeps the selection running.
        assert not candidate.suite.selected
        assert candidate.suite.reason.startswith("the nightly is stale")
        assert project.main[:12] in candidate.suite.reason
        assert SUITE_ENV not in host.jobs[-1]["command"]
        recorded = _events(store)[-1]["suite_selection"]
        assert recorded["mode"] == "full"
        assert recorded["reason"].startswith("the nightly is stale")

    def test_a_fresh_nightly_of_an_older_main_commit_still_selects(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start = datetime.now(UTC)
        _audit_clock(monkeypatch, start)
        _nightly(tiered, store, project)
        _audit_clock(monkeypatch, start + timedelta(hours=1))
        _landed_full_verify(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, result = run_verify(
            tiered,
            store=store,
            ref=sha,
            repo_root=project.root,
            now=start + timedelta(hours=2),
        )

        assert candidate.suite.selected, candidate.suite.reason
        assert result.outcome == "passed", result.log

    def test_with_no_nightly_ever_a_landed_full_verify_runs_full(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _landed_full_verify(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, _ = run_verify(tiered, store=store, ref=sha, repo_root=project.root)

        health = read_main_health(store, project.root, base_ref="origin/main")
        assert (health.state, health.via) == ("green", "pr")
        assert not candidate.suite.selected
        assert candidate.suite.reason == (
            "the nightly is missing: no green nightly of a commit on main is on record"
        )

    def test_a_red_main_runs_the_full_suite(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _red(provider: LocalProvider, job: Job) -> tuple[int, str]:
            return 1, f"{PREFLIGHT_PASSED_MARK}\nFAILED tests/test_leaf.py::test_leaf\n"

        monkeypatch.setattr(LocalProvider, "_execute", _red)
        _nightly(tiered, store, project)
        monkeypatch.setattr(
            LocalProvider, "_execute", lambda provider, job: host(provider, job)
        )
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, _ = run_verify(tiered, store=store, ref=sha, repo_root=project.root)

        assert not candidate.suite.selected
        assert candidate.suite.reason.startswith(f"main is red at {project.main}")

    def test_a_selection_that_cannot_be_computed_runs_the_full_suite(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)
        monkeypatch.setenv("GATE_RE", "(unclosed")

        candidate, _ = run_verify(tiered, store=store, ref=sha, repo_root=project.root)

        assert not candidate.suite.selected
        assert candidate.suite.reason.startswith("the selection could not be computed")
        assert SUITE_ENV not in host.jobs[-1]["command"]

    def test_the_nightly_and_a_sharded_verify_run_the_full_suite(
        self, project: Project, tiered: DispatchConfig, store: SqliteRunStore
    ) -> None:
        sha = project.branch("leaf", _LEAF_CHANGE)
        request = SuiteRequest(
            repo_root=project.root,
            base_ref="origin/main",
            base_sha=project.main,
            ref_sha=sha,
        )

        nightly = choose_suite(
            SUITE_TIERED, replace(request, kind=NIGHTLY_KIND), store=store
        )
        sharded = choose_suite(SUITE_TIERED, replace(request, shards=3), store=store)

        assert nightly.mode == sharded.mode == "full"
        assert nightly.reason == "the nightly verifies main in full"
        assert sharded.reason == "a sharded verify runs the full suite"

    def test_a_selected_verify_is_never_main_health_evidence(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)
        candidate, result = run_verify(
            tiered, store=store, ref=sha, repo_root=project.root
        )
        assert candidate.suite.selected and result.outcome == "passed"

        project.squash_land(sha)
        health = read_main_health(store, project.root, base_ref="origin/main")

        # Main still reads green from the nightly's commit, never the landing.
        assert health.sha == project.main
        assert health.via == NIGHTLY_KIND


class TestTheDefault:
    def test_the_default_full_suite_runs_todays_phases(
        self,
        project: Project,
        default: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(default, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)

        candidate, result = run_verify(
            default, store=store, ref=sha, repo_root=project.root
        )

        command = host.jobs[-1]["command"]
        assert 'bash scripts/verify.sh || verify_status="$?"' in command
        assert SUITE_ENV not in command
        assert "-m policy" not in command
        assert "suite=" not in summary_line(candidate, result)
        recorded = _events(store)[-1]
        assert recorded["suite"] == "full"
        assert recorded["suite_selection"] == {
            "mode": "full",
            "reason": '[verify] suite = "full"',
            "files": [],
            "digest": None,
            "selector_version": None,
        }


class TestTheCommand:
    def test_full_runs_the_full_suite_and_says_why(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)
        store.close()

        result = runner.invoke(app, ["verify", sha, "--full"])

        assert result.exit_code == 0, result.output
        assert "kew verify: suite: full (--full was passed)" in result.output
        assert SUITE_ENV not in host.jobs[-1]["command"]

    def test_a_selected_verify_says_the_floor_did_not_run(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)
        store.close()

        result = runner.invoke(app, ["verify", sha])

        assert result.exit_code == 0, result.output
        assert "suite: selected, 2 test files plus -m policy" in result.output
        assert "the coverage floor is not applied" in result.output
        assert "suite=selected files=2" in result.output

    def test_explain_selection_reproduces_the_recorded_selection(
        self,
        project: Project,
        tiered: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(tiered, store, project)
        sha = project.branch("leaf", _LEAF_CHANGE)
        run_verify(tiered, store=store, ref=sha, repo_root=project.root)
        recorded = _events(store)[-1]["suite_selection"]

        result = runner.invoke(app, ["verify", "--explain-selection", sha])

        assert result.exit_code == 0, result.output
        assert f"digest sha256:{recorded['digest']}" in result.output
        assert f"selector v{recorded['selector_version']}" in result.output
        for path in recorded["files"]:
            assert re.search(rf"^{re.escape(path)}  ", result.output, re.M)

    def test_full_with_explain_selection_is_refused(self) -> None:
        result = runner.invoke(app, ["verify", "--explain-selection", "--full"])

        assert result.exit_code == 2
        assert "--explain-selection runs nothing, so it would ignore --full" in (
            result.output
        )


class TestTheVerifyShSwitch:
    """verify.sh really skips the two phases for ``selected``, and only then."""

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
        for entry in bin_dir.iterdir():
            entry.chmod(0o755)
        return bin_dir

    def _run(self, tmp_path: Path, suite: str) -> tuple[int, str, str]:
        bin_dir = self._stub_bin(tmp_path)
        log = tmp_path / "stub.log"
        log.write_text("", encoding="utf-8")
        environment = environ_without_switches()
        environment.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            KEW_STUB_LOG=str(log),
            KEW_VERIFY_IMAGE_SCAN="skip",
        )
        environment[SUITE_ENV] = suite
        completed = subprocess.run(
            ["bash", str(VERIFY_SH)],
            cwd=KEW_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        return completed.returncode, log.read_text(encoding="utf-8"), completed.stderr

    @pytest.mark.parametrize(
        "ambient", [{}, SUITE_JOB_SWITCHES], ids=["clean", "inside-a-suite-job"]
    )
    def test_selected_skips_both_phases_and_the_floor_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ambient: dict[str, str],
    ) -> None:
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        _, seen, stderr = self._run(tmp_path, SUITE_SELECTED)

        assert "-n auto" not in seen
        assert "pytest -m serial" not in seen
        assert "--cov-fail-under" not in seen
        assert "ruff check" in seen, "the static gates still ran"
        assert "KEW_VERIFY_SUITE=selected" in stderr
        assert "coverage floor are NOT run here" in stderr

    def test_a_typo_runs_the_full_suite(self, tmp_path: Path) -> None:
        code, seen, _ = self._run(tmp_path, "selectd")

        assert code == 86, f"a misspelt switch silently dropped the suite: {seen}"
