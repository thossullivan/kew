"""``kew verify`` — the merge verification, as a kew command (#2379).

The gcp host is driven entirely through the fake ``gcloud`` on PATH that the
host-provider tests use: no test here touches a real cloud, and no test runs
``scripts/verify.sh`` — what the job *is* is asserted on the composed script,
and the plumbing around it (bundle, ship, run, exit code, audit row) is
asserted end to end with the gate itself stubbed out.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support.fake_gcloud import (
    fake_gcloud_on_path,
    read_state,
)
from support.fake_gcloud import (
    recorded_calls as _calls,
)
from support.fake_gcloud import (
    write_state as _write_state,
)
from support.held_out_log import opened, report_line
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, GcpHostConfig, HostsConfig, VerifyConfig
from kew.host_takeovers import (
    LEASE_LAPSED_EVENT,
    LEASE_TAKEOVER_PENDING_EVENT,
    LEASE_TAKEOVER_REFUSED_EVENT,
    LEASE_TAKEOVER_UNCONFIRMED_EVENT,
    AuditTakeoverLedger,
    record_lapsed_leases,
)
from kew.hosts import (
    HostConnectionLost,
    HostError,
    HostNotProvisioned,
    Job,
    LocalProvider,
    host_lock_dir,
)
from kew.hosts.gcp import (
    BOOTS_FILE,
    IDLE_STOP_UNIT,
    JOB_SURVIVES_EXIT,
    LEASE_FILE,
    TAKEOVER_FENCED_EXIT,
    lease_token,
    parse_lease_token,
)
from kew.hosts.provider import TakeoverLedger
from kew.merge_verify import (
    HELD_OUT_PARALLEL,
    HELD_OUT_SERIAL,
    MEMORY_GUARD_ENV,
    PHASE_GATES,
    PHASE_HELD_OUT,
    PHASE_MARK,
    PREFLIGHT_COMMAND,
    SWITCHYARD_ARCHIVE_ENV,
    SWITCHYARD_IMAGE_ENV,
    UI_CHECK_COMMAND,
    VERIFY_EVENT,
    VerifyCandidate,
    VerifyRefused,
    VerifyResult,
    commit_parents,
    environment_probe_lines,
    environment_record_lines,
    held_out_lines,
    job_environment,
    job_script,
    killing_signal,
    main_python_lines,
    pinned_images,
    probed_suite_lines,
    record_verify_event,
    resolve_candidate,
    run_verify,
    suite_job_script,
    write_bundle,
)
from kew.run_store import SqliteRunStore
from kew.verify_ledger import (
    FLAKY_EVENT,
    GH_TIMEOUT,
    LEDGER_FAILED_EVENT,
    RESOLVED_EVENT,
    SETUP,
    phase_line,
)

runner = CliRunner()

#: What a host's log carries when the held-out phase ran both its runs clean
#: (#2485). A stubbed gate that reported no held-out phase is a verification
#: that never reported it, which fails. The phase's lines sit in its own
#: section of the log, behind its opening line (``held_out_section``).
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", message], repo)
    return _run([*_GIT, "rev-parse", "HEAD"], repo)


def _project(tmp_path: Path, *, frontend: bool = False) -> Path:
    """A project repo whose HEAD is one candidate commit ahead of ``main``."""
    repo = tmp_path
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "kew").mkdir(exist_ok=True)
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    if frontend:
        (repo / "frontend").mkdir()
        (repo / "frontend" / "app.ts").write_text("export const a = 1\n", "utf-8")
    else:
        (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "candidate")
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


def _config(tmp_path: Path, **verify: Any) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(**verify)
    return config


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    """The bundle is built with git and the local host runs sh."""
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake ``gcloud`` first on PATH; yields its JSON state file."""
    fake = tmp_path / "fake"
    fake.mkdir()
    with fake_gcloud_on_path(fake, monkeypatch) as state:
        yield state


def _enter(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in *project* as the operator would.

    ``tests/conftest.py`` already points ``kew.cli.project_root`` at
    ``tmp_path``, which is why every CLI test here builds its repository
    there: the command bundles the project root, not cwd.
    """
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.chdir(project)


def _payload(event: Any) -> dict[str, Any]:
    """The event's payload, however the store hands it back."""
    payload = event.payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _events(log_dir: Path) -> list[Any]:
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        return store.list_audit_events(event=VERIFY_EVENT)


def _merge_main_into(repo: Path) -> tuple[str, str]:
    """Make HEAD what the drive verifies: main merged ``--no-ff`` ON the PR head.

    Main moves first, so the merge is a real two-parent commit. Returns the PR
    head and main, the merge's first and second parents.
    """
    head = _run([*_GIT, "rev-parse", "HEAD"], repo)
    _run([*_GIT, "checkout", "--quiet", "main"], repo)
    (repo / "MAIN.md").write_text("main moved\n", encoding="utf-8")
    main = _commit(repo, "main moved")
    _run([*_GIT, "checkout", "--quiet", "candidate"], repo)
    _run([*_GIT, "merge", "--quiet", "--no-ff", "--no-edit", "main"], repo)
    return head, main


class TestVerifiedCommitParents:
    """#3140 — the event names the parents of the commit it verified.

    A drive verify is of a merge commit, not of the PR head; the head is that
    commit's first parent, and the work page joins the event to its item by it.
    """

    def test_a_merge_candidate_lists_the_pr_head_first_then_main(
        self, tmp_path: Path
    ) -> None:
        repo = _project(tmp_path)
        head, main = _merge_main_into(repo)
        candidate = resolve_candidate(repo)
        assert candidate.ref_parents == (head, main)

    def test_parents_git_cannot_read_are_recorded_as_none(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        assert commit_parents(repo, "f" * 40) == ()

    def test_the_recorded_event_carries_the_parents(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        head, main = _merge_main_into(repo)
        result = VerifyResult(host="local", exit_code=0, seconds=1.0, log="")
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            record_verify_event(
                store, resolve_candidate(repo), result, config=_config(tmp_path)
            )
            [event] = store.list_audit_events(event=VERIFY_EVENT)
        payload = _payload(event)
        assert payload["ref_sha"] == _run([*_GIT, "rev-parse", "HEAD"], repo)
        assert payload["ref_parents"] == [head, main]

    def test_kew_verify_records_the_parents_of_what_it_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command an operator's ad hoc merge runs, end to end on the local
        host with its own kew.toml: nothing below the CLI is stubbed but the
        gate, which is a stand-in script in the candidate."""
        repo = _project(tmp_path)
        (repo / "kew" / "scripts").mkdir(parents=True)
        (repo / "kew" / "scripts" / "verify.sh").write_text(
            "#!/bin/sh\necho GATE RAN\n", encoding="utf-8"
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "scan-agent-image.sh").write_text(
            "#!/bin/sh\necho PREFLIGHT RAN\n", encoding="utf-8"
        )
        _commit(repo, "a gate to run")
        head, main = _merge_main_into(repo)
        (repo / "kew.toml").write_text(
            '[project]\nrepo = "o/r"\n'
            f"[logging]\ndir = {str(repo / 'logs')!r}\n"
            '[verify]\nhost = "local"\n',
            encoding="utf-8",
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _enter(repo, monkeypatch)

        result = runner.invoke(app, ["verify", "--host", "local"])

        assert "GATE RAN" in result.output, result.output
        [event] = _events(repo / "logs")
        payload = _payload(event)
        assert payload["ref_sha"] == _run([*_GIT, "rev-parse", "HEAD"], repo)
        assert payload["ref_parents"] == [head, main]

    def test_kew_verify_records_phase_seconds_that_add_up_to_its_wall_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real local verify whose stand-in gate marks its phases, each of
        which sleeps, in the gate job and the suite job: every phase kew
        records, setup included, adds up to the wall time it measured around
        the jobs, and each marked phase took what it slept."""
        repo = _project(tmp_path)
        (repo / "kew" / "scripts").mkdir(parents=True)
        (repo / "kew" / "scripts" / "verify.sh").write_text(
            "#!/bin/sh\n"
            'if [ "$KEW_VERIFY_STAGE" = suite ]; then\n'
            '  echo "kew-phase: xdist start $(date +%s)"\n'
            "  sleep 1\n"
            '  echo "kew-phase: xdist end $(date +%s)"\n'
            "  exit 0\n"
            "fi\n"
            'echo "kew-phase: static start $(date +%s)"\n'
            "sleep 1\n"
            'echo "kew-phase: static end $(date +%s)"\n'
            'echo "kew-phase: containment start $(date +%s)"\n'
            "sleep 2\n"
            'echo "kew-phase: containment end $(date +%s)"\n',
            encoding="utf-8",
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "scan-agent-image.sh").write_text(
            "#!/bin/sh\necho PREFLIGHT RAN\n", encoding="utf-8"
        )
        _commit(repo, "a gate that marks its phases")
        (repo / "kew.toml").write_text(
            '[project]\nrepo = "o/r"\n'
            f"[logging]\ndir = {str(repo / 'logs')!r}\n"
            '[verify]\nhost = "local"\n',
            encoding="utf-8",
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _enter(repo, monkeypatch)

        result = runner.invoke(app, ["verify", "--host", "local"])

        [event] = _events(repo / "logs")
        payload = _payload(event)
        phases = payload["phases"]
        assert {"setup", "static", "containment", "held_out"} <= set(phases), (
            result.output
        )
        total = sum(phase["seconds"] for phase in phases.values())
        assert total == pytest.approx(payload["seconds"], rel=0.05)
        assert 0 <= phases["static"]["seconds"] <= 2
        assert 1 <= phases["containment"]["seconds"] <= 3
        assert phases["static"]["outcome"] == "passed"


class TestCandidate:
    """What is verified, against what, and whether the console moved."""

    def test_the_default_ref_is_head_and_the_base_is_the_merge_base(
        self, tmp_path: Path
    ) -> None:
        repo = _project(tmp_path)
        candidate = resolve_candidate(repo)
        assert candidate.ref == "HEAD"
        assert candidate.ref_sha == _run([*_GIT, "rev-parse", "HEAD"], repo)
        assert candidate.base_sha == _run([*_GIT, "rev-parse", "main"], repo)
        assert candidate.base_ref == "main"

    def test_a_named_ref_is_resolved_to_its_commit(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        candidate = resolve_candidate(repo, ref="HEAD~1")
        assert candidate.ref_sha == _run([*_GIT, "rev-parse", "HEAD~1"], repo)

    def test_a_frontend_change_is_detected_against_the_base(
        self, tmp_path: Path
    ) -> None:
        """AC3 — the candidate that touches frontend/ is the one that says so."""
        assert resolve_candidate(
            _project(tmp_path / "a", frontend=True)
        ).frontend_changed
        assert not resolve_candidate(_project(tmp_path / "b")).frontend_changed

    def test_an_unknown_ref_is_refused_before_any_host_is_touched(
        self, tmp_path: Path
    ) -> None:
        repo = _project(tmp_path)
        with pytest.raises(VerifyRefused, match="rev-parse"):
            resolve_candidate(repo, ref="no-such-ref")

    def test_an_explicit_base_sha_is_the_base_not_the_checkout_main(
        self, tmp_path: Path
    ) -> None:
        """A caller that fetched main itself names it; the checkout's own main,
        which may lag, is not consulted."""
        repo = _project(tmp_path)
        fetched = _run([*_GIT, "rev-parse", "HEAD"], repo)
        (repo / "kew" / "later.py").write_text("y = 2\n", encoding="utf-8")
        _commit(repo, "on top of the fetched main")
        candidate = resolve_candidate(repo, base_sha=fetched)
        assert candidate.base_sha == candidate.base_ref == fetched
        assert resolve_candidate(repo).base_sha != fetched

    def test_a_checkout_without_a_base_is_refused_by_name(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        with pytest.raises(VerifyRefused, match="no base"):
            resolve_candidate(repo, base="release")


class TestJob:
    """The job is kew's own gate, plus exactly what the gate needs present."""

    def test_the_script_is_umask_then_the_preflight_then_the_gate(self) -> None:
        script = job_script(_candidate(), images=[])
        gate = [
            "set -eu",
            "umask 022",
            phase_line(SETUP, "start"),
            "cd kew",
            PREFLIGHT_COMMAND,
            *environment_probe_lines(),
            *environment_record_lines(),
            *main_python_lines(),
            f'echo "{PHASE_MARK} {PHASE_GATES}"',
            *probed_suite_lines("KEW_VERIFY_STAGE=gates bash scripts/verify.sh"),
            f'echo "{PHASE_MARK} {PHASE_HELD_OUT}"',
            phase_line("held_out", "start"),
        ]
        assert script.splitlines()[: len(gate)] == gate
        # Then the held-out phase (#2485), before any pytest phase (#3212), and
        # nothing else but its closing ledger marker (#3215): the mutation
        # report runs after the merge, as its own job (#2817).
        assert script.splitlines()[len(gate) :] == [
            *held_out_lines(
                _candidate(), (HELD_OUT_PARALLEL, HELD_OUT_SERIAL), main_python=True
            ),
            phase_line("held_out", "end"),
        ]
        assert "mutmut-sampler" not in script

    def test_the_pinned_tool_preflight_runs_before_the_suite(self) -> None:
        """#2462 — the scanner check used to be the gate's LAST step.

        The whole suite passed on the verify host and then the run failed at the
        agent-image scan, because the host's trivy was not the pinned one:
        twelve minutes burned per attempt. The same check, in the same script,
        in a mode that builds nothing, runs first.
        """
        script = job_script(_candidate(), images=[("codex", "kew-agent-codex:pin")])
        assert "--check-tools" in PREFLIGHT_COMMAND
        assert script.index(PREFLIGHT_COMMAND) < script.index("docker image inspect")
        assert script.index(PREFLIGHT_COMMAND) < script.index("bash scripts/verify.sh")

    def test_ui_check_runs_only_when_the_frontend_moved(self) -> None:
        """AC3 — one candidate runs ui-check; the other does not."""
        moved = suite_job_script(_candidate(frontend_changed=True))
        still = suite_job_script(_candidate(frontend_changed=False))
        lines = moved.splitlines()
        start = lines.index("cd ../frontend")
        assert lines[start : start + 3] == [
            "cd ../frontend",
            UI_CHECK_COMMAND,
            "cd ../kew",
        ]
        # In the suite job, after its pytest phases; the held-out phase ran in
        # the gate job before them (#3212).
        assert moved.index("KEW_VERIFY_STAGE=suite") < moved.index(UI_CHECK_COMMAND)
        assert "kew-held-out: begin" not in moved
        assert "ui-check" not in still
        assert "ui-check" not in job_script(
            _candidate(frontend_changed=True), images=[]
        )

    def test_a_pinned_image_is_built_only_when_it_is_missing(self) -> None:
        script = job_script(_candidate(), images=[("codex", "kew-agent-codex:pin")])
        assert (
            "docker image inspect kew-agent-codex:pin >/dev/null 2>&1 || "
            "uv run kew sandbox build --runner codex" in script
        )
        # A host without docker builds nothing rather than failing the gate.
        assert "if command -v docker >/dev/null 2>&1; then" in script

    def test_only_buildable_pinned_runners_are_named(self) -> None:
        config = DispatchConfig()
        config.sandbox.images = {
            "claude-code": "kew-agent:pin",
            "opencode": "kew-agent-opencode:pin",
        }
        assert pinned_images(config) == [
            ("claude-code", "kew-agent:pin"),
            ("opencode", "kew-agent-opencode:pin"),
        ]

    def test_the_guard_base_is_the_resolved_base_sha(self, tmp_path: Path) -> None:
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=True,
            ambient={},
        )
        assert environment[MEMORY_GUARD_ENV] == "b" * 40

    def test_the_switchyard_opt_in_reaches_only_a_host_that_can_build_it(
        self, tmp_path: Path
    ) -> None:
        ambient = {
            SWITCHYARD_IMAGE_ENV: "1",
            SWITCHYARD_ARCHIVE_ENV: "/private/kew-switchyard-source.tar",
        }
        local = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=True,
            ambient=ambient,
        )
        remote = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=False,
            ambient=ambient,
        )
        assert local[SWITCHYARD_IMAGE_ENV] == "1"
        assert SWITCHYARD_IMAGE_ENV not in remote

    def test_the_opt_in_carries_the_input_the_gate_looks_for(
        self, tmp_path: Path
    ) -> None:
        """The opt-in without its archive is the gate's ``input is missing``.

        ``scripts/scan-agent-image.sh`` reads the private input's path from
        ``KEW_SWITCHYARD_SOURCE_ARCHIVE``; forwarding the opt-in without it
        refuses the gate on the one host that *can* build the layer, which
        attests a failed verdict the candidate never earned.
        """
        archive = "/private/kew-switchyard-source.tar"
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=True,
            ambient={
                SWITCHYARD_IMAGE_ENV: "1",
                SWITCHYARD_ARCHIVE_ENV: archive,
            },
        )
        assert environment[SWITCHYARD_IMAGE_ENV] == "1"
        assert environment[SWITCHYARD_ARCHIVE_ENV] == archive

    def test_the_input_never_reaches_a_host_that_cannot_build_it(
        self, tmp_path: Path
    ) -> None:
        """Where the archive lives stays on the operator machine."""
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=False,
            ambient={
                SWITCHYARD_IMAGE_ENV: "1",
                SWITCHYARD_ARCHIVE_ENV: "/private/kew-switchyard-source.tar",
            },
        )
        assert SWITCHYARD_ARCHIVE_ENV not in environment

    @pytest.mark.parametrize("archive", ["", "   "])
    def test_an_opt_in_without_its_input_skips_the_layer_rather_than_failing(
        self, tmp_path: Path, archive: str
    ) -> None:
        """A machine that cannot name the input is a machine without it.

        Skipping that one layer is what every machine without the input
        already does; refusing the whole gate would record a verdict about
        the candidate that is really a fact about this machine.
        """
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=True,
            ambient={SWITCHYARD_IMAGE_ENV: "1", SWITCHYARD_ARCHIVE_ENV: archive},
        )
        assert SWITCHYARD_IMAGE_ENV not in environment
        assert SWITCHYARD_ARCHIVE_ENV not in environment

    def test_an_unset_opt_in_is_not_invented_for_the_local_host(
        self, tmp_path: Path
    ) -> None:
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path),
            builds_private_images=True,
            ambient={SWITCHYARD_ARCHIVE_ENV: "/private/kew-switchyard-source.tar"},
        )
        assert SWITCHYARD_IMAGE_ENV not in environment
        assert SWITCHYARD_ARCHIVE_ENV not in environment

    def test_configured_deselects_reach_the_job_as_pytest_addopts(
        self, tmp_path: Path
    ) -> None:
        environment = job_environment(
            _candidate(),
            config=_config(tmp_path, deselect=["tests/test_a.py::test_x"]),
            builds_private_images=False,
            ambient={},
        )
        assert environment["PYTEST_ADDOPTS"] == "--deselect tests/test_a.py::test_x"

    def test_no_declared_variable_is_credential_shaped(self, tmp_path: Path) -> None:
        from kew.hosts import refuse_credentials

        environment = job_environment(
            _candidate(),
            config=_config(tmp_path, deselect=["tests/test_a.py::test_x"]),
            builds_private_images=True,
            ambient={
                SWITCHYARD_IMAGE_ENV: "1",
                SWITCHYARD_ARCHIVE_ENV: "/private/kew-switchyard-source.tar",
            },
        )
        assert refuse_credentials(environment) == environment


class TestBundle:
    """The candidate goes in as a bundle, and the repo is only ever read."""

    def test_the_bundle_clones_to_the_candidate_commit(self, tmp_path: Path) -> None:
        repo = _project(tmp_path / "repo")
        candidate = resolve_candidate(repo, ref="HEAD~1")
        workspace = tmp_path / "work"
        workspace.mkdir()
        bundle = write_bundle(repo, candidate, workspace)
        checkout = tmp_path / "checkout"
        _run([*_GIT, "clone", "--quiet", str(bundle), str(checkout)], tmp_path)
        assert _run([*_GIT, "rev-parse", "HEAD"], checkout) == candidate.ref_sha
        # A bundle without a HEAD clones into an empty tree; this one does not.
        assert (checkout / "README.md").is_file()

    def test_the_bundle_is_written_from_a_linked_worktree(self, tmp_path: Path) -> None:
        """A linked worktree's ``.git`` is a file, so git names the objects.

        Hard-coding ``<root>/.git/objects`` made every ``kew verify`` run from
        a worktree fail with "unable to normalize alternate object path" (#2379
        review) rather than bundling the candidate it was standing on.
        """
        repo = _project(tmp_path / "repo")
        linked = tmp_path / "linked"
        _run([*_GIT, "worktree", "add", "--quiet", "--detach", str(linked)], repo)
        assert (linked / ".git").is_file()
        candidate = resolve_candidate(linked)
        workspace = tmp_path / "work"
        workspace.mkdir()

        bundle = write_bundle(linked, candidate, workspace)

        checkout = tmp_path / "checkout"
        _run([*_GIT, "clone", "--quiet", str(bundle), str(checkout)], tmp_path)
        assert _run([*_GIT, "rev-parse", "HEAD"], checkout) == candidate.ref_sha
        assert (checkout / "README.md").is_file()

    def test_bundling_writes_no_ref_into_the_operators_repository(
        self, tmp_path: Path
    ) -> None:
        repo = _project(tmp_path / "repo")
        before = _run([*_GIT, "for-each-ref", "--format=%(refname)"], repo)
        workspace = tmp_path / "work"
        workspace.mkdir()
        write_bundle(repo, resolve_candidate(repo), workspace)
        assert _run([*_GIT, "for-each-ref", "--format=%(refname)"], repo) == before


class TestLocalHost:
    """AC1 — a local verification exits 0 and records exactly one event."""

    @patch("kew.cli.load_config")
    def test_the_job_really_runs_the_gate_inside_the_clone(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing stubbed below kew: the composed script runs on the host.

        The gate itself is a stand-in — ``scripts/verify.sh`` in the candidate
        is a two-line shell script — because what is under test is that the
        script kew composes executes, in the shipped clone, from ``kew/``. The
        pinned-tool preflight is a stand-in for the same reason, and the order
        the two ran in is asserted: the preflight is the cheap check that has to
        come first (#2462).
        """
        repo = _project(tmp_path)
        gate = repo / "kew" / "scripts"
        gate.mkdir(parents=True)
        (gate / "verify.sh").write_text(
            '#!/bin/sh\npwd > "$HOME/kew-gate-cwd"\necho GATE RAN\n', encoding="utf-8"
        )
        preflight = repo / "scripts"
        preflight.mkdir(parents=True)
        (preflight / "scan-agent-image.sh").write_text(
            '#!/bin/sh\ntest "$1" = --check-tools || exit 2\necho PREFLIGHT RAN\n',
            encoding="utf-8",
        )
        _commit(repo, "a gate to run")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 0, result.output
        assert "GATE RAN" in result.output
        assert result.output.index("PREFLIGHT RAN") < result.output.index("GATE RAN")
        ran_in = (home / "kew-gate-cwd").read_text(encoding="utf-8").strip()
        # It ran from kew/ inside the bundle's clone, not the project worktree.
        assert ran_in.endswith("/checkout/kew")
        assert ran_in != str(repo / "kew")

    @patch("kew.cli.load_config")
    def test_a_refused_preflight_fails_before_the_suite_runs(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2462 — a host whose trivy is off the pin burns no twelve minutes.

        The preflight is a stand-in that refuses the way the real one does (its
        own refusals are asserted against the real script in
        ``tests/test_agent_image_scan_gate.py``). What is under test here is that
        kew's composed script stops on it: the gate — the whole suite — never
        runs, and the verification is a failure rather than a pass.
        """
        repo = _project(tmp_path)
        gate = repo / "kew" / "scripts"
        gate.mkdir(parents=True)
        (gate / "verify.sh").write_text(
            '#!/bin/sh\ntouch "$HOME/kew-suite-ran"\necho GATE RAN\n', encoding="utf-8"
        )
        preflight = repo / "scripts"
        preflight.mkdir(parents=True)
        (preflight / "scan-agent-image.sh").write_text(
            "#!/bin/sh\necho 'trivy 0.75.0 is not the pinned 0.69.3' >&2\nexit 1\n",
            encoding="utf-8",
        )
        _commit(repo, "a host whose scanner is off the pin")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")

        result = runner.invoke(app, ["verify"])

        assert result.exit_code != 0
        assert "not the pinned" in result.output
        assert "GATE RAN" not in result.output
        assert not (home / "kew-suite-ran").exists(), (
            "the suite must not run behind a refused pinned-tool preflight — "
            "that is the twelve minutes this check exists to save"
        )

    @patch("kew.cli.load_config")
    def test_a_passing_local_verify_exits_zero_and_records_one_event(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")
        shipped: dict[str, Any] = {}

        def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
            shipped["checkout"] = self.checkout
            shipped["command"] = job.command
            shipped["environment"] = dict(job.environment)
            return 0, "gate passed\n" + _HELD_OUT_PASSED

        monkeypatch.setattr(LocalProvider, "_execute", _execute)

        result = runner.invoke(app, ["verify", "--host", "local"])

        assert result.exit_code == 0, result.output
        assert "gate passed" in result.output
        assert "passed: host=local" in result.output
        # The job really ran against a clone of the bundle, not the worktree.
        assert shipped["checkout"] != repo
        assert shipped["command"].startswith("set -eu\numask 022\n")
        assert shipped["environment"][MEMORY_GUARD_ENV] == _run(
            [*_GIT, "rev-parse", "main"], repo
        )
        events = _events(repo / "logs")
        assert len(events) == 1
        payload = _payload(events[0])
        assert payload["host"] == "local"
        assert payload["exit_code"] == 0
        assert payload["outcome"] == "passed"
        assert payload["ref_sha"] == _run([*_GIT, "rev-parse", "HEAD"], repo)
        assert payload["base_sha"] == _run([*_GIT, "rev-parse", "main"], repo)
        assert payload["seconds"] >= 0

    def test_a_passing_merge_gate_refuses_without_a_durable_audit_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        config = _config(repo, host="local")
        monkeypatch.setattr(
            LocalProvider,
            "_execute",
            lambda *_args, **_kwargs: (0, "gate passed\n" + _HELD_OUT_PASSED),
        )
        (repo / "logs").mkdir()
        with SqliteRunStore(str(repo / "logs" / "dispatch.db")) as store:
            store._conn.execute(
                "CREATE TRIGGER reject_verify_receipt BEFORE INSERT ON audit_events "
                "WHEN NEW.event = 'verify_completed' BEGIN "
                "SELECT RAISE(FAIL, 'audit unavailable'); END"
            )

            with pytest.raises(VerifyRefused, match="durable receipt"):
                run_verify(
                    config,
                    store=store,
                    repo_root=repo,
                    require_receipt=True,
                )

            assert store.list_audit_events(event=VERIFY_EVENT) == []

    @patch("kew.cli.load_config")
    def test_a_failing_verify_returns_the_jobs_exit_code_and_records_it(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4 — a drive script reads the gate's status, not kew's verdict."""
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)
        monkeypatch.setattr(
            LocalProvider, "_execute", lambda *_a, **_k: (3, "gate failed\n")
        )

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 3, result.output
        payload = _payload(_events(repo / "logs")[0])
        assert (payload["exit_code"], payload["outcome"]) == (3, "failed")

    @patch("kew.cli.load_config")
    def test_an_incomplete_verify_records_an_event_with_no_exit_code(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)

        def _boom(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
            raise HostError("the host went away")

        monkeypatch.setattr(LocalProvider, "_execute", _boom)

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 2, result.output
        assert "no exit code" in result.output
        payload = _payload(_events(repo / "logs")[0])
        assert payload["exit_code"] is None
        assert payload["outcome"] == "incomplete"
        assert "went away" in payload["error"]
        assert payload["error_category"] is None

    @patch("kew.cli.load_config")
    def test_a_lost_connection_is_attested_with_its_own_category(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2465 — an interruption is recorded as one, not as a red gate.

        The reason text is for a person; the category is what a retry decision
        can be made on. A host whose session died says nothing about the code
        under test, so running the job again is the work — and a plain
        ``incomplete`` with prose cannot be read for that.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)

        def _lost(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
            raise HostConnectionLost("the session ended with no exit marker")

        monkeypatch.setattr(LocalProvider, "_execute", _lost)

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 2, result.output
        payload = _payload(_events(repo / "logs")[0])
        assert payload["outcome"] == "incomplete"
        assert payload["error_category"] == "host_connection_lost"

    @patch("kew.cli.load_config")
    def test_a_release_failure_does_not_discard_the_gates_answer(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host's cleanup is not the gate's verdict (#2379 review).

        A ``release()`` that raises used to replace a passing verification
        with ``incomplete`` and no exit code at all, so a gate that passed
        was attested as never having answered and ``kew verify`` exited 2.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")
        monkeypatch.setattr(
            LocalProvider,
            "_execute",
            lambda *_a, **_k: (0, "gate passed\n" + _HELD_OUT_PASSED),
        )
        clean_up = LocalProvider._release

        def _failing_release(self: LocalProvider) -> None:
            if self.checkout is None:
                return  # ship()'s own pre-clean: there is nothing to release yet
            clean_up(self)
            raise HostError("cannot schedule the idle stop on instance verify-1")

        monkeypatch.setattr(LocalProvider, "_release", _failing_release)

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 0, result.output
        assert "passed: host=local" in result.output
        assert "idle stop" in result.output
        payload = _payload(_events(repo / "logs")[0])
        assert (payload["exit_code"], payload["outcome"]) == (0, "passed")
        # Recorded beside the verdict, because the instance may still be billing.
        assert "idle stop" in payload["error"]

    @pytest.mark.parametrize("status", [-9, 128 + 9])
    @patch("kew.cli.load_config")
    def test_a_signal_killed_job_is_incomplete_rather_than_a_verdict(
        self,
        load_cfg: Any,
        status: int,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A killed gate never answered, so nothing is passed through (#2379).

        The ``-9`` used to be recorded as ``failed`` and re-raised as a 247
        exit status, which is neither the gate's verdict nor a could-not-run
        code a drive script can act on.

        ``137`` is the same kill in the shape a *shell* reports it: the job's
        own ``sh`` propagates ``128 + 9`` when the OOM killer reaches the gate
        underneath it rather than the shell itself, and reading only the
        negative shape attested that as a ``failed`` verdict (#2379 review).
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)
        monkeypatch.setattr(
            LocalProvider, "_execute", lambda *_a, **_k: (status, "half a gate\n")
        )

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 2, result.output
        assert "half a gate" in result.output  # the log it did write is kept
        assert "signal 9" in result.output
        payload = _payload(_events(repo / "logs")[0])
        assert payload["exit_code"] is None
        assert payload["outcome"] == "incomplete"
        assert "signal 9" in payload["error"]

    @patch("kew.cli.load_config")
    def test_a_release_failure_is_recorded_when_the_job_answered_nothing(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An instance left billing is not lost behind the job's own failure.

        The no-answer path built its own result and never consulted the
        release, so a host kew could not release went unrecorded exactly when
        the job gave it nothing to be recorded beside (#2379 review).
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")
        clean_up = LocalProvider._release

        def _boom(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
            raise HostError("the host went away")

        def _failing_release(self: LocalProvider) -> None:
            if self.checkout is None:
                return  # ship()'s own pre-clean: there is nothing to release yet
            clean_up(self)
            raise HostError("cannot schedule the idle stop on instance verify-1")

        monkeypatch.setattr(LocalProvider, "_execute", _boom)
        monkeypatch.setattr(LocalProvider, "_release", _failing_release)

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 2, result.output
        assert "went away" in result.output
        assert "idle stop" in result.output
        payload = _payload(_events(repo / "logs")[0])
        assert payload["exit_code"] is None
        assert payload["outcome"] == "incomplete"
        assert "went away" in payload["error"]
        assert "idle stop" in payload["error"]

    @patch("kew.cli.load_config")
    def test_a_machine_without_the_github_cli_still_verifies(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing in a verification touches GitHub (#2379 review).

        Preflighting every required tool refused the command outright on a
        machine without ``gh``, over a CLI the job never calls.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")
        monkeypatch.setattr(
            LocalProvider,
            "_execute",
            lambda *_a, **_k: (0, "gate passed\n" + _HELD_OUT_PASSED),
        )
        # conftest's hermetic fixture hands every test a `gh`; this machine
        # has none, and every other tool is still found where it really is.
        from kew import subprocess_utils

        installed = subprocess_utils.shutil.which
        monkeypatch.setattr(
            subprocess_utils.shutil,
            "which",
            lambda tool, *a, **k: None if tool == "gh" else installed(tool, *a, **k),
        )

        result = runner.invoke(app, ["verify"])

        assert result.exit_code == 0, result.output
        assert "passed: host=local" in result.output

    @patch("kew.cli.load_config")
    def test_a_frontend_candidate_runs_ui_check_on_the_host(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3, end to end: the shipped job carries the browser check."""
        repo = _project(tmp_path, frontend=True)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)
        seen: dict[str, str] = {}

        def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
            seen["command"] = job.command
            return 0, _HELD_OUT_PASSED

        monkeypatch.setattr(LocalProvider, "_execute", _execute)

        assert runner.invoke(app, ["verify"]).exit_code == 0
        assert UI_CHECK_COMMAND in seen["command"]
        assert _payload(_events(repo / "logs")[0])["ui_check"] is True


#: The test the stand-in gate fails while it is red.
_FLAKY_TEST = "tests/test_widget.py::test_spins"


class _FlakyGate:
    """A stand-in gate, committed on the candidate: it fails one test while
    the toggle file outside the checkout exists, and passes once it is gone.
    The verify runs for real on the local host, so both verifies test one
    head on one base."""

    def __init__(self, repo: Path, toggle: Path) -> None:
        self.toggle = toggle
        toggle.write_text("red\n", encoding="utf-8")
        (repo / "kew" / "scripts").mkdir(parents=True)
        rule = "=" * 20
        (repo / "kew" / "scripts" / "verify.sh").write_text(
            f'#!/bin/sh\nif [ -f "{toggle}" ]; then\n'
            f'  echo "{rule} short test summary info {rule}"\n'
            f'  echo "FAILED {_FLAKY_TEST} - AssertionError"\n'
            f'  echo "{rule} 1 failed in 1.00s {rule}"\n'
            "  exit 1\nfi\n",
            encoding="utf-8",
        )
        # The failing test's file, which a flaky issue may only name when it
        # exists at the head.
        (repo / "kew" / "tests").mkdir(exist_ok=True)
        (repo / "kew" / "tests" / "test_widget.py").write_text(
            "def test_spins():\n    pass\n", encoding="utf-8"
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "scan-agent-image.sh").write_text(
            "#!/bin/sh\necho PREFLIGHT RAN\n", encoding="utf-8"
        )
        _commit(repo, "a gate that fails one test, then passes")

    def turn_green(self) -> None:
        self.toggle.unlink()


def _flaky_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FlakyGate:
    repo = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return _FlakyGate(repo, home / "red")


def _ledger_rows(log_dir: Path, event: str) -> list[dict[str, Any]]:
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        return [_payload(row) for row in store.list_audit_events(event=event)]


class _TimedOutGh:
    """A forge every call of which times out; records each call's timeout."""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)


class _RefusesTheProbe(SqliteRunStore):
    """A ledger that answers a takeover's write probe with a refusal, so the
    gcp host is refused before any work, and that refusal is recorded."""

    def probe_audit_write(self) -> None:
        raise OSError("disk full")


class TestTheLedgerAfterAVerify:
    """What resolving the PR's previous verify may do after this one (#3215)."""

    def test_a_drive_verify_the_host_refused_pairs_with_the_prs_next_verify(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The refusal is a red of the PR, not a stray: it names the PR and
        its body, so the PR's next verify resolves it and it never sits in
        the report as pending."""
        from kew.verify_ledger import resolve_after

        repo = _project(tmp_path)
        config = _config(repo, host="gcp")
        config.hosts = HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")
        )
        body = "## Behavior changes\n\n- `tests/test_x.py::test_y` — moved\n"
        with _RefusesTheProbe(str(tmp_path / "audit.db")) as store:
            candidate, refused = run_verify(
                config, store=store, repo_root=repo, pr=7, pr_body=body
            )
            green = record_verify_event(
                store,
                candidate,
                VerifyResult(host="local", exit_code=0, seconds=1.0, log=""),
                config=config,
                pr=7,
                pr_body=body,
            )
            found = resolve_after(store, green, repo_root=repo, repo="o/r")
            [event] = [
                _payload(row)
                for row in store.list_audit_events(event=VERIFY_EVENT)
                if row.id != green
            ]

        assert refused.exit_code is None
        assert event["pr"] == 7
        assert isinstance(event["behavior_changes"], str)
        assert found == "infra"

    def test_a_filing_that_times_out_leaves_the_verify_result_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The drive's red, then green, of one PR head: the flake's filing
        times out on every ``gh`` call, each bounded at 30 seconds. The green
        verify still returns its own result, and the failure is logged and
        recorded."""
        gate = _flaky_project(tmp_path, monkeypatch)
        repo = tmp_path
        config = _config(repo, host="local")
        gh = _TimedOutGh()
        (repo / "logs").mkdir()
        with SqliteRunStore(str(repo / "logs" / "dispatch.db")) as store:
            _, red = run_verify(config, store=store, repo_root=repo, pr=7, gh=gh)
            gate.turn_green()
            with caplog.at_level("WARNING", logger="kew.verify_ledger"):
                _, green = run_verify(config, store=store, repo_root=repo, pr=7, gh=gh)

        assert (red.outcome, red.exit_code) == ("failed", 1)
        assert (green.outcome, green.exit_code) == ("passed", 0)
        assert green.receipt is not None
        [resolved] = _ledger_rows(repo / "logs", RESOLVED_EVENT)
        assert resolved["class"] == "flake"
        assert gh.timeouts and set(gh.timeouts) == {GH_TIMEOUT}
        failed = _ledger_rows(repo / "logs", LEDGER_FAILED_EVENT)
        assert {row["step"] for row in failed} == {"label", "file"}
        assert all("timed out" in row["error"] for row in failed)
        assert _ledger_rows(repo / "logs", FLAKY_EVENT) == []
        assert "not filed" in caplog.text

    def test_a_bare_hand_run_verify_never_reaches_github(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``kew verify`` twice on one head, red then green, bound to no PR:
        the red is classed a flake, and no ``gh`` ever runs."""
        from kew import subprocess_utils

        gate = _flaky_project(tmp_path, monkeypatch)
        repo = tmp_path
        (repo / "kew.toml").write_text(
            '[project]\nrepo = "o/r"\n'
            f"[logging]\ndir = {str(repo / 'logs')!r}\n"
            '[verify]\nhost = "local"\n',
            encoding="utf-8",
        )
        _enter(repo, monkeypatch)
        # A `gh` that notes each call, first on PATH and in the allowlist, so a
        # command that registers its tools again still finds this one.
        ran = tmp_path / "home" / "gh-ran"
        tools = tmp_path / "home" / "bin"
        tools.mkdir()
        stub = tools / "gh"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{ran}"\n', encoding="utf-8")
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", str(stub))

        first = runner.invoke(app, ["verify", "--host", "local"])
        gate.turn_green()
        second = runner.invoke(app, ["verify", "--host", "local"])

        assert (first.exit_code, second.exit_code) == (1, 0), second.output
        [resolved] = _ledger_rows(repo / "logs", RESOLVED_EVENT)
        assert resolved["class"] == "flake"
        assert not ran.exists(), ran.read_text()
        assert _ledger_rows(repo / "logs", FLAKY_EVENT) == []


class TestGcpHost:
    """AC2 — with a fake gcloud, the gcp host ships, runs and is recorded."""

    def _gcp_config(self, repo: Path) -> DispatchConfig:
        config = _config(repo, host="gcp")
        config.hosts = HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")
        )
        return config

    @patch("kew.cli.load_config")
    def test_the_gcp_host_ships_a_bundle_runs_the_job_and_returns_its_code(
        self,
        load_cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = self._gcp_config(repo)
        _write_state(fake_gcloud, job_exit=3, job_output="remote gate\n")

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert result.exit_code == 3, result.output
        assert "remote gate" in result.output
        calls = _calls(fake_gcloud)
        shipped = [call for call in calls if call[:2] == ["compute", "scp"]]
        assert len(shipped) == 1
        assert any(arg.endswith("repo.bundle") for arg in shipped[0])
        ran = [
            call
            for call in calls
            if call[:2] == ["compute", "ssh"] and "verify.sh" in " ".join(call)
        ]
        assert len(ran) == 1
        payload = _payload(_events(repo / "logs")[0])
        assert payload["host"] == "gcp"
        assert payload["exit_code"] == 3
        assert payload["outcome"] == "failed"

    @patch("kew.cli.load_config")
    def test_the_gcp_job_is_told_to_skip_the_private_image_layer(
        self,
        load_cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = self._gcp_config(repo)
        monkeypatch.setenv(SWITCHYARD_IMAGE_ENV, "1")
        _write_state(fake_gcloud, job_exit=0, job_output=_HELD_OUT_PASSED)

        assert runner.invoke(app, ["verify", "--host", "gcp"]).exit_code == 0
        job = next(
            " ".join(call)
            for call in _calls(fake_gcloud)
            if call[:2] == ["compute", "ssh"] and "verify.sh" in " ".join(call)
        )
        assert SWITCHYARD_IMAGE_ENV not in job

    @patch("kew.cli.load_config")
    def test_an_oom_killed_remote_gate_is_incomplete_not_a_verdict(
        self,
        load_cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """The remote host reports a kill as ``128 + N`` (#2379 review).

        The wrapper around the job prints ``$?``, so an OOM-killed gate came
        back as ``137`` and was attested as ``failed`` with ``137`` as the
        CLI's exit code — a verdict the gate never gave, on the host the
        operator's drive actually uses.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = self._gcp_config(repo)
        _write_state(fake_gcloud, job_exit=128 + 9, job_output="half a gate\n")

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert result.exit_code == 2, result.output
        assert "half a gate" in result.output
        assert "signal 9" in result.output
        payload = _payload(_events(repo / "logs")[0])
        assert payload["host"] == "gcp"
        assert payload["exit_code"] is None
        assert payload["outcome"] == "incomplete"
        assert "signal 9" in payload["error"]

    @patch("kew.cli.load_config")
    def test_an_instance_kew_started_is_released_when_it_cannot_be_reached(
        self,
        load_cfg: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """Bringing the host up is inside the released scope (#2379 review).

        ``ensure_up()`` used to run above the ``finally`` that releases, so an
        instance kew had just started and then could not reach over ssh was
        left RUNNING and billing with no idle stop armed and no release so
        much as attempted — while the recorded reason told the operator a
        shutdown was pending on a machine that had just booted.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = self._gcp_config(repo)
        _write_state(
            fake_gcloud,
            statuses=["TERMINATED"],
            cancel_exit=255,
            cancel_stderr="ssh: connect to host port 22: Connection refused\n",
        )

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert result.exit_code == 2, result.output
        calls = _calls(fake_gcloud)
        started = [
            call for call in calls if call[:3] == ["compute", "instances", "start"]
        ]
        commands = [
            call[call.index("--command") + 1] for call in calls if "--command" in call
        ]
        assert len(started) == 1
        assert [command for command in commands if "systemd-run" in command] != []
        assert [call for call in calls if call[:2] == ["compute", "scp"]] == []
        payload = _payload(_events(repo / "logs")[0])
        assert payload["outcome"] == "incomplete"
        assert payload["exit_code"] is None
        assert "cannot be reached over ssh" in payload["error"]
        assert "is present" not in payload["error"]

    @patch("kew.cli.load_config")
    def test_gcp_without_an_instance_is_refused_before_a_job(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo)

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert result.exit_code == 2
        assert "[hosts.gcp]" in result.output
        assert _events(repo / "logs") == []


def _terminate_once_the_job_runs(state: Path, sent: list[str]) -> None:
    """SIGTERM this process once the fake has started the job — if it unwinds.

    Without a handler of kew's, SIGTERM would end the test run itself, so a
    default disposition is recorded as the failure instead of being sent.
    """
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            commands = " ".join(" ".join(call) for call in _calls(state))
        except (json.JSONDecodeError, OSError):
            commands = ""
        if "KEW_HOST_EXIT" in commands:
            if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                sent.append("no SIGTERM handler while the job ran")
                return
            sent.append("sent")
            os.kill(os.getpid(), signal.SIGTERM)
            return
        time.sleep(0.05)
    sent.append("the job never started")


class TestTheGcpLeaseOutlivesNoOperator:
    """#2811 — a dead verify does not leave the instance leased for hours."""

    @staticmethod
    def _configure(repo: Path) -> None:
        """A kew.toml in the project, so the command loads its own config."""
        (repo / "kew.toml").write_text(
            "[project]\n"
            'repo = "o/r"\n'
            "[logging]\n"
            f"dir = {str(repo / 'logs')!r}\n"
            "[verify]\n"
            'host = "gcp"\n'
            "[hosts.gcp]\n"
            'project = "proj"\n'
            'zone = "us-central1-a"\n'
            'instance = "verify-1"\n',
            encoding="utf-8",
        )

    def test_sigterm_during_the_job_runs_the_lease_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        self._configure(repo)
        _write_state(fake_gcloud, job_sleep=15)
        before = signal.getsignal(signal.SIGTERM)
        sent: list[str] = []
        sender = threading.Thread(
            target=_terminate_once_the_job_runs, args=(fake_gcloud, sent)
        )
        sender.start()
        try:
            result = runner.invoke(app, ["verify", "--host", "gcp"])
        finally:
            sender.join()

        assert sent == ["sent"]
        assert result.exit_code == 128 + signal.SIGTERM, result.output
        commands = [
            call[call.index("--command") + 1]
            for call in _calls(fake_gcloud)
            if "--command" in call
        ]
        job = next(i for i, c in enumerate(commands) if "KEW_HOST_EXIT" in c)
        released = [
            i
            for i, c in enumerate(commands)
            if f"rm -f {LEASE_FILE}" in c and IDLE_STOP_UNIT in c
        ]
        assert released and released[-1] > job
        assert signal.getsignal(signal.SIGTERM) == before

    def test_a_release_that_leaves_the_job_running_is_reported_beside_the_verdict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """Review of #2811: a host left leased is not a clean verify.

        The gate's verdict stands; the release that could not prove its job
        stopped is named beside it, in the output and the event.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        self._configure(repo)
        _write_state(
            fake_gcloud,
            job_exit=0,
            job_output=_HELD_OUT_PASSED,
            release_exit=JOB_SURVIVES_EXIT,
        )

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert "not proved stopped" in " ".join(result.output.split())
        payload = _payload(_events(repo / "logs")[0])
        assert payload["exit_code"] == 0
        assert payload["outcome"] == "passed"
        assert "not proved stopped" in payload["error"]

    def test_a_lapsed_lease_taken_over_is_an_audit_event_naming_its_holder(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        self._configure(repo)
        me = parse_lease_token(lease_token())
        assert me is not None
        earlier = uuid.uuid4().hex
        holder = f"{me.machine}-{os.getpid()}-{earlier}-{'0' * 12}"
        boots = host_lock_dir() / BOOTS_FILE
        boots.parent.mkdir(parents=True, exist_ok=True)
        boots.write_text(f"{earlier}\n", encoding="ascii")
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )

        result = runner.invoke(app, ["verify", "--host", "gcp"])

        assert result.exit_code == 0, result.output
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(repo / "logs" / "dispatch.db")) as store:
            events = store.list_audit_events(event=LEASE_LAPSED_EVENT)
        assert [_payload(event)["holder"] for event in events] == [holder]
        assert _payload(events[0])["taken_by"] == lease_token()

    def test_a_takeover_is_recorded_even_when_sigterm_ends_the_job(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """Review of #2811: the event is not left behind the verdict.

        It used to be written after verify and the held-out runs finished, so
        a SIGTERM during the job that followed the takeover lost it.
        """
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        self._configure(repo)
        me = parse_lease_token(lease_token())
        assert me is not None
        earlier = uuid.uuid4().hex
        holder = f"{me.machine}-{os.getpid()}-{earlier}-{'0' * 12}"
        boots = host_lock_dir() / BOOTS_FILE
        boots.parent.mkdir(parents=True, exist_ok=True)
        boots.write_text(f"{earlier}\n", encoding="ascii")
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder, job_sleep=15)
        sent: list[str] = []
        sender = threading.Thread(
            target=_terminate_once_the_job_runs, args=(fake_gcloud, sent)
        )
        sender.start()
        try:
            result = runner.invoke(app, ["verify", "--host", "gcp"])
        finally:
            sender.join()

        assert sent == ["sent"]
        assert result.exit_code == 128 + signal.SIGTERM, result.output
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(repo / "logs" / "dispatch.db")) as store:
            events = store.list_audit_events(event=LEASE_LAPSED_EVENT)
        assert [_payload(event)["holder"] for event in events] == [holder]

    @staticmethod
    def _lapsed_holder() -> str:
        """A lease token of this machine's naming a boot it has since left."""
        me = parse_lease_token(lease_token())
        assert me is not None
        earlier = uuid.uuid4().hex
        boots = host_lock_dir() / BOOTS_FILE
        boots.parent.mkdir(parents=True, exist_ok=True)
        boots.write_text(f"{earlier}\n", encoding="ascii")
        return f"{me.machine}-{os.getpid()}-{earlier}-{'0' * 12}"

    def test_a_takeover_is_on_record_before_the_job_ends(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """#2874: a SIGKILL or reboot mid-job runs no ``finally``.

        So the event is read from the store while the job is still running —
        what a process killed at that moment would have left behind.
        """
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        self._configure(repo)
        holder = self._lapsed_holder()
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder, job_sleep=15)
        database = repo / "logs" / "dispatch.db"
        seen: list[list[str]] = []

        def read_then_terminate() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    commands = " ".join(" ".join(c) for c in _calls(fake_gcloud))
                except (json.JSONDecodeError, OSError):
                    commands = ""
                if "KEW_HOST_EXIT" in commands:
                    with SqliteRunStore(str(database)) as store:
                        events = store.list_audit_events(event=LEASE_LAPSED_EVENT)
                    seen.append([_payload(event)["holder"] for event in events])
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        reader = threading.Thread(target=read_then_terminate)
        reader.start()
        try:
            result = runner.invoke(app, ["verify", "--host", "gcp"])
        finally:
            reader.join()

        assert seen == [[holder]], result.output

    def test_a_takeover_that_cannot_be_recorded_ships_nothing(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: no job runs under a takeover the audit trail does not hold."""

        repo = _project(tmp_path)
        config = _config(repo, host="gcp")
        config.hosts = HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")
        )
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )

        with _RefusesSettles(str(tmp_path / "audit.db")) as store:
            _, result = run_verify(config, store=store, repo_root=repo)

        assert result.exit_code is None
        assert "could not record" in (result.error or "")
        assert not [c for c in _calls(fake_gcloud) if c[:2] == ["compute", "scp"]]

    def test_a_takeover_on_a_shard_thread_is_recorded(self, tmp_path: Path) -> None:
        """#2874: a sharded verify takes over on its worker threads, and a
        SQLite handle refuses every thread but its own — the write must not."""
        from kew.run_store import SqliteRunStore

        class _TakesOver(LocalProvider):
            ledgers: list[TakeoverLedger] = []

            def record_takeovers_in(self, ledger: TakeoverLedger) -> None:
                self.ledgers.append(ledger)

        provider = _TakesOver(lock_dir=tmp_path / "locks")
        failures: list[BaseException] = []

        def take_over() -> None:
            try:
                ledger = provider.ledgers[0]
                ledger.pending("gone-1-a", "t" * 32)
                ledger.settled("gone-1-a", "t" * 32, taken_by="me", confirmed=True)
            except BaseException as exc:
                failures.append(exc)

        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            record_lapsed_leases(store, [provider], config=_config(tmp_path))
            shard = threading.Thread(target=take_over)
            shard.start()
            shard.join()
            events = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert failures == []
        assert [_payload(event)["holder"] for event in events] == ["gone-1-a"]

    def test_a_takeover_is_pending_then_settled_under_one_id(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: the pending row is written before the instance is asked."""
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )

        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            pending = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            settled = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert len(pending) == len(settled) == 1
        assert pending[0].id < settled[0].id
        takeover = _payload(pending[0])["takeover_id"]
        assert _payload(settled[0])["takeover_id"] == takeover
        assert _payload(settled[0])["confirmed_on_host"] is True
        assert read_state(fake_gcloud)["takeover_marker"] == [
            takeover,
            holder,
            lease_token(),
        ]

    def test_a_takeover_that_cannot_be_put_pending_is_not_taken(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: no pending row, no takeover — the dead holder keeps its lease."""
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=self._lapsed_holder(),
            job_output=_HELD_OUT_PASSED,
        )

        class _RefusesPending(SqliteRunStore):
            def append_audit_event(self, *args: Any, **kwargs: Any) -> Any:
                if kwargs.get("event") == LEASE_TAKEOVER_PENDING_EVENT:
                    raise OSError("disk full")
                return super().append_audit_event(*args, **kwargs)

        with _RefusesPending(str(tmp_path / "audit.db")) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)

        assert result.exit_code is None
        assert "was not taken" in (result.error or "")
        state = read_state(fake_gcloud)
        assert "taken_over" not in state
        assert not [c for c in _calls(fake_gcloud) if c[:2] == ["compute", "scp"]]

    def test_a_takeover_the_instance_refuses_is_settled_as_refused(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: the dead holder kept its lease, and the ledger says so."""
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=self._lapsed_holder(),
            takeover_refused=True,
        )

        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            (refused,) = store.list_audit_events(event=LEASE_TAKEOVER_REFUSED_EVENT)
            lapsed = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert result.exit_code is None
        assert lapsed == []
        assert _payload(refused)["takeover_id"] == _payload(pending)["takeover_id"]

    def test_a_lease_that_changed_before_the_takeover_is_never_recorded_as_taken(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: the dead holder's lease went between the hold and the takeover.

        The takeover settles as refused with the lease changed, no marker is
        written and no ``host_lease_lapsed`` is recorded; the verify takes the
        free lease on its next read and runs.
        """
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=self._lapsed_holder(),
            lease_changed_at_takeover=True,
            job_output=_HELD_OUT_PASSED,
        )

        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            (refused,) = store.list_audit_events(event=LEASE_TAKEOVER_REFUSED_EVENT)
            lapsed = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert lapsed == []
        assert _payload(refused)["takeover_id"] == _payload(pending)["takeover_id"]
        assert _payload(refused)["lease_changed"] is True
        assert "takeover_marker" not in read_state(fake_gcloud)
        assert result.exit_code == 0, result.error

    def test_a_gcp_verify_with_an_unwritable_store_does_no_host_work(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: a ledger that will not take a write is found before any host
        call, not after ``ensure_up`` has changed the host."""
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=self._lapsed_holder(),
            job_output=_HELD_OUT_PASSED,
        )

        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            store._conn.execute("PRAGMA query_only = ON")
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)

        assert result.outcome == "incomplete"
        assert "will not take a write" in (result.error or "")
        assert _calls(fake_gcloud) == []

    def test_a_gcp_verify_with_no_store_does_no_host_work(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: a takeover with nowhere to be recorded is never risked."""
        repo = _project(tmp_path)
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=self._lapsed_holder(),
            job_output=_HELD_OUT_PASSED,
        )

        _, result = run_verify(self._gcp_config(repo), store=None, repo_root=repo)

        assert result.outcome == "incomplete"
        assert "no run store" in (result.error or "")
        assert _calls(fake_gcloud) == []

    def test_a_takeover_killed_before_its_settle_is_settled_by_the_next_start(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: SIGKILL right after the instance answered the takeover.

        A child process takes the lease over and is killed by its own store
        the moment it would settle the takeover — after the SSH returned, before
        the row. The next verify finds the pending row, reads the takeover id
        back off the instance, and settles it before the first copy.
        """
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )
        database = tmp_path / "audit.db"
        child = subprocess.run(
            [sys.executable, "-c", _KILLED_AT_SETTLE, str(database)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert child.returncode == -signal.SIGKILL, child.stderr
        with SqliteRunStore(str(database)) as store:
            (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            assert store.list_audit_events(event=LEASE_LAPSED_EVENT) == []
        killed = _payload(pending)

        with SqliteRunStore(str(database)) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (settled,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert result.exit_code == 0, result.error
        payload = _payload(settled)
        assert payload["takeover_id"] == killed["takeover_id"]
        assert payload["holder"] == holder
        assert payload["taken_by"] == killed["taken_by"] != lease_token()
        assert payload["confirmed_on_host"] is True
        first_copy = read_state(fake_gcloud)["first_copy_at"]
        assert isinstance(first_copy, float)
        assert datetime.fromisoformat(settled.timestamp).timestamp() <= first_copy

    def test_a_dead_takers_pending_row_the_host_cannot_confirm_is_settled_unconfirmed(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: a stop clears ``/run``, and the takeover with it.

        Whether it went through is no longer known, so it goes on record as
        unconfirmed rather than staying pending — or being dropped.
        """
        from kew.audit import emit, resolve_actor
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        dead = self._lapsed_holder()
        _write_state(fake_gcloud, job_output=_HELD_OUT_PASSED)
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            emit(
                store,
                LEASE_TAKEOVER_PENDING_EVENT,
                actor=resolve_actor(),
                payload={
                    "host": "gcp",
                    "address": "verify-1",
                    "resource": "gcp/proj/us-central1-a/verify-1",
                    "holder": "older-1-a",
                    "takeover_id": "a" * 32,
                    "taken_by": dead,
                },
            )
            run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (settled,) = store.list_audit_events(event=LEASE_TAKEOVER_UNCONFIRMED_EVENT)
            taken = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert taken == []
        payload = _payload(settled)
        assert (payload["takeover_id"], payload["taken_by"]) == ("a" * 32, dead)
        assert payload["confirmed_on_host"] is False

    def test_a_dead_takers_pending_row_stays_pending_when_the_hold_never_ran(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: a hold whose login failed never read the takeover record.

        The instance may still name the takeover, so settling it unconfirmed
        would put the wrong answer on record for good; it stays pending for
        a hold that does reach the instance.
        """
        from kew.audit import emit, resolve_actor
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        dead = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            hold_unreachable=True,
            takeover_marker=["a" * 32, "older-1-a", dead],
            job_output=_HELD_OUT_PASSED,
        )
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            emit(
                store,
                LEASE_TAKEOVER_PENDING_EVENT,
                actor=resolve_actor(),
                payload={
                    "host": "gcp",
                    "address": "verify-1",
                    "resource": "gcp/proj/us-central1-a/verify-1",
                    "holder": "older-1-a",
                    "takeover_id": "a" * 32,
                    "taken_by": dead,
                },
            )
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            settled = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert result.exit_code != 0
        assert settled == []
        assert read_state(fake_gcloud).get("copies", 0) == 0

    def test_a_retry_in_the_same_process_settles_its_own_failed_settle(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: this process's own pending row is not left pending for good.

        The settle write fails once, so the first verify fails after the
        takeover. The second, from the same process, reads the takeover back
        off the instance and settles that row before its first copy — once.
        """
        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )

        with _RefusesSettles(str(tmp_path / "audit.db"), failures=1) as store:
            _, first = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            assert "could not record" in (first.error or "")
            assert read_state(fake_gcloud).get("copies", 0) == 0
            _, second = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            (settled,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert second.exit_code == 0, second.error
        payload = _payload(settled)
        assert payload["takeover_id"] == _payload(pending)["takeover_id"]
        assert payload["taken_by"] == lease_token()
        assert payload["confirmed_on_host"] is True
        first_copy = read_state(fake_gcloud)["first_copy_at"]
        assert isinstance(first_copy, float)
        assert datetime.fromisoformat(settled.timestamp).timestamp() <= first_copy

    def test_a_takeover_killed_before_the_instance_was_asked_is_unconfirmed(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2874: SIGKILL after the pending row, before the takeover's SSH.

        The instance never heard of that takeover, so the next verify settles
        it as not confirmed — never as a lease taken — and then takes the
        lease over itself, under a takeover id of its own.
        """
        from kew.run_store import SqliteRunStore

        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )
        database = tmp_path / "audit.db"
        child = subprocess.run(
            [sys.executable, "-c", _KILLED_AFTER_PENDING, str(database)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert child.returncode == -signal.SIGKILL, child.stderr
        assert "taken_over" not in read_state(fake_gcloud)

        with SqliteRunStore(str(database)) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            pending = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            (unconfirmed,) = store.list_audit_events(
                event=LEASE_TAKEOVER_UNCONFIRMED_EVENT
            )
            (taken,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert result.exit_code == 0, result.error
        killed = _payload(min(pending, key=lambda event: event.id))
        assert _payload(unconfirmed)["takeover_id"] == killed["takeover_id"]
        assert _payload(unconfirmed)["confirmed_on_host"] is False
        assert _payload(taken)["takeover_id"] != killed["takeover_id"]
        assert _payload(taken)["taken_by"] == lease_token()

    def test_same_named_instances_never_see_or_settle_each_others_takeovers(
        self, tmp_path: Path
    ) -> None:
        """#2874: an instance is its project, zone and name, not its name alone."""
        from kew.hosts.gcp import GcpProvider

        def ledger(project: str, zone: str, store: SqliteRunStore) -> TakeoverLedger:
            provider = GcpProvider(
                GcpHostConfig(project=project, zone=zone, instance="verify-1"),
                lock_dir=tmp_path / "locks",
            )
            return AuditTakeoverLedger(
                store,
                provider,
                config=DispatchConfig(),
                owner=threading.current_thread(),
            )

        takeover = "a" * 32
        with SqliteRunStore(str(tmp_path / "audit.db")) as store:
            here = ledger("proj-a", "zone-a", store)
            same_project = ledger("proj-a", "zone-b", store)
            same_zone = ledger("proj-b", "zone-a", store)
            here.pending("older-1-a", takeover)
            elsewhere = [same_project.unsettled(), same_zone.unsettled()]
            same_project.settled(
                "older-1-a", takeover, taken_by="other-1-b", confirmed=False
            )
            mine = here.unsettled()
            here.settled("older-1-a", takeover, taken_by="me-1-a", confirmed=True)
            (settled,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)
            left = here.unsettled()

        assert elsewhere == [[], []]
        assert [row.takeover for row in mine] == [takeover]
        assert _payload(settled)["resource"] == "gcp/proj-a/zone-a/verify-1"
        assert left == []

    def test_a_late_takeover_session_is_refused_by_the_hold_that_settled_it(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2978: the takeover's gcloud session outlives its SIGKILLed operator.

        The retry's hold reads the marker, finds no proof and settles the
        takeover unconfirmed. Only then does the delayed session reach the
        instance; the hold fenced it in the same lock hold, so it takes
        nothing, and the retry's own takeover is the one the instance names.
        """
        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        late = tmp_path / "late"
        late.mkdir()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            takeover_delay=str(late),
            job_output=_HELD_OUT_PASSED,
        )
        database = tmp_path / "audit.db"
        child = subprocess.Popen(
            [sys.executable, "-c", _KILLED_AT_SETTLE, str(database)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_file(late / "started")
        finally:
            child.kill()
            child.wait(timeout=30)

        with _ReleasesTheLateTakeover(str(database), late=late) as store:
            _, result = run_verify(self._gcp_config(repo), store=store, repo_root=repo)
            (pending, ours) = sorted(
                store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT),
                key=lambda event: event.id,
            )
            (unconfirmed,) = store.list_audit_events(
                event=LEASE_TAKEOVER_UNCONFIRMED_EVENT
            )
            (taken,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        assert result.exit_code == 0, result.error
        killed = _payload(pending)["takeover_id"]
        assert _payload(unconfirmed)["takeover_id"] == killed
        assert _payload(taken)["takeover_id"] == _payload(ours)["takeover_id"]
        state = read_state(fake_gcloud)
        assert state["late_exit"] == TAKEOVER_FENCED_EXIT
        assert state["taken_over"] == 1
        assert state["takeover_marker"][0] == _payload(ours)["takeover_id"]

    def test_a_live_process_whose_settle_failed_is_settled_by_the_next_claim(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2978: two processes, one store, one host.

        The first takes the lease over and fails to write the answer, then
        stays alive. Its pid is running, so it never reads as lapsed; the
        second, holding the host's exclusive claim, settles the takeover by
        the instance's marker anyway — once, as taken.
        """
        repo = _project(tmp_path)
        holder = self._lapsed_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_HELD_OUT_PASSED,
        )
        database = tmp_path / "audit.db"
        child = subprocess.Popen(
            [sys.executable, "-c", _ALIVE_AFTER_A_FAILED_SETTLE, str(database)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "settle failed"
            assert child.poll() is None
            with SqliteRunStore(str(database)) as store:
                _, result = run_verify(
                    self._gcp_config(repo), store=store, repo_root=repo
                )
                (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
                (settled,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)
            assert child.poll() is None
        finally:
            child.kill()
            child.wait(timeout=30)

        assert result.exit_code == 0, result.error
        payload = _payload(settled)
        assert payload["takeover_id"] == _payload(pending)["takeover_id"]
        assert payload["taken_by"] == _payload(pending)["taken_by"] != lease_token()
        assert payload["confirmed_on_host"] is True
        assert read_state(fake_gcloud)["taken_over"] == 1

    @staticmethod
    def _gcp_config(repo: Path) -> DispatchConfig:
        config = _config(repo, host="gcp")
        config.hosts = HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")
        )
        return config


#: A child that takes a lapsed lease over and is SIGKILLed by its own store
#: the moment it would settle the takeover, i.e. right after the SSH returned.
_KILLED_AT_SETTLE = """
import os, signal, sys
from kew import subprocess_utils
from kew.config import DispatchConfig, GcpHostConfig
from kew.hosts import host_lock_dir
from kew.hosts.gcp import GcpProvider
from kew.host_takeovers import LEASE_LAPSED_EVENT, record_lapsed_leases
from kew.run_store import SqliteRunStore

class KilledAtSettle(SqliteRunStore):
    def append_audit_event_once(self, *args, **kwargs):
        if kwargs.get("event") == LEASE_LAPSED_EVENT:
            os.kill(os.getpid(), signal.SIGKILL)
        return super().append_audit_event_once(*args, **kwargs)

subprocess_utils.TOOL_PATHS.pop("gcloud", None)
provider = GcpProvider(
    GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1"),
    lock_dir=host_lock_dir(),
    sleep=lambda _: None,
)
with KilledAtSettle(sys.argv[1]) as store:
    record_lapsed_leases(store, [provider], config=DispatchConfig())
    with provider.claim():
        provider.ensure_up()
"""


#: A child that puts a takeover pending and is SIGKILLed by its own store the
#: moment that row is written, i.e. before the instance is asked for it.
_KILLED_AFTER_PENDING = _KILLED_AT_SETTLE.replace(
    """    def append_audit_event_once(self, *args, **kwargs):
        if kwargs.get("event") == LEASE_LAPSED_EVENT:
            os.kill(os.getpid(), signal.SIGKILL)
        return super().append_audit_event_once(*args, **kwargs)
""",
    """    def append_audit_event(self, *args, **kwargs):
        written = super().append_audit_event(*args, **kwargs)
        if kwargs.get("event") == "host_lease_takeover_pending":
            os.kill(os.getpid(), signal.SIGKILL)
        return written
""",
)


#: A child whose takeover's settle fails, so it leaves the row pending, and
#: which then stays alive with no claim on the host.
_ALIVE_AFTER_A_FAILED_SETTLE = _KILLED_AT_SETTLE.replace(
    "            os.kill(os.getpid(), signal.SIGKILL)",
    "            raise OSError('disk full')",
).replace(
    """    with provider.claim():
        provider.ensure_up()
""",
    """    try:
        with provider.claim():
            provider.ensure_up()
    except Exception:
        pass
    print("settle failed", flush=True)
    import time
    time.sleep(120)
""",
)


def _wait_for_file(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


class _ReleasesTheLateTakeover(SqliteRunStore):
    """Lets the delayed takeover session run once a takeover is settled
    unconfirmed, and waits for it to answer before going on."""

    def __init__(self, path: str, *, late: Path) -> None:
        super().__init__(path)
        self._late = late

    def append_audit_event_once(self, *args: Any, **kwargs: Any) -> Any:
        written = super().append_audit_event_once(*args, **kwargs)
        if kwargs.get("event") == LEASE_TAKEOVER_UNCONFIRMED_EVENT:
            (self._late / "go").touch()
            _wait_for_file(self._late / "done")
        return written


class _RefusesSettles(SqliteRunStore):
    """A ledger whose first *failures* takeover settles fail (all by default)."""

    def __init__(self, path: str, *, failures: int | None = None) -> None:
        super().__init__(path)
        self._failures = failures

    def append_audit_event_once(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("event") == LEASE_LAPSED_EVENT and self._failures != 0:
            if self._failures is not None:
                self._failures -= 1
            raise OSError("disk full")
        return super().append_audit_event_once(*args, **kwargs)


class TestHostSelection:
    """``--host`` beats ``[verify] host``; both are refused when unknown."""

    def test_the_configured_host_is_the_default(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        config = _config(repo, host="gcp")
        with pytest.raises(VerifyRefused, match="hosts.gcp"):
            run_verify(config, store=None, repo_root=repo)

    def test_an_unknown_host_is_refused_by_name(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        with pytest.raises(VerifyRefused, match="unknown host"):
            run_verify(_config(repo), store=None, host="aws", repo_root=repo)

    def test_an_unknown_configured_host_fails_at_load(self) -> None:
        with pytest.raises(ValueError, match='must be "local" or "gcp"'):
            VerifyConfig(host="aws")

    def test_a_whitespace_deselect_entry_fails_at_load(self) -> None:
        with pytest.raises(ValueError, match="single-word pytest node ids"):
            VerifyConfig(deselect=["tests/a.py::t -x"])

    def test_a_negative_ceiling_fails_at_load(self) -> None:
        with pytest.raises(ValueError, match="must be 0"):
            VerifyConfig(timeout_seconds=-1)

    def test_a_zero_ceiling_leaves_the_job_unbounded(self, tmp_path: Path) -> None:
        from kew.merge_verify import build_job

        config = _config(tmp_path, timeout_seconds=0)
        provider = LocalProvider(lock_dir=tmp_path / "locks")
        assert (
            build_job(_candidate(), config=config, provider=provider).timeout_seconds
            is None
        )
        config.verify = VerifyConfig(timeout_seconds=60)
        assert (
            build_job(_candidate(), config=config, provider=provider).timeout_seconds
            == 60.0
        )

    def test_the_local_provider_is_the_one_that_builds_private_images(
        self, tmp_path: Path
    ) -> None:
        from kew.hosts import GcpProvider

        local = LocalProvider(lock_dir=tmp_path / "locks")
        remote = GcpProvider(
            GcpHostConfig(project="p", zone="z", instance="i"),
            lock_dir=tmp_path / "locks",
        )
        assert (local.builds_private_images, remote.builds_private_images) == (
            True,
            False,
        )

    def test_the_local_job_declares_the_whole_private_opt_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end from the ambient environment: opt-in *and* input.

        The job the local provider is handed is what the gate reads, so this
        is the one that catches a half-forwarded opt-in before the gate does.
        """
        from kew.merge_verify import build_job

        archive = str(tmp_path / "kew-switchyard-source.tar")
        monkeypatch.setenv(SWITCHYARD_IMAGE_ENV, "1")
        monkeypatch.setenv(SWITCHYARD_ARCHIVE_ENV, archive)
        job = build_job(
            _candidate(),
            config=_config(tmp_path),
            provider=LocalProvider(lock_dir=tmp_path / "locks"),
        )
        assert job.environment[SWITCHYARD_IMAGE_ENV] == "1"
        assert job.environment[SWITCHYARD_ARCHIVE_ENV] == archive

    def test_resolving_an_unknown_host_names_the_two_that_exist(self) -> None:
        from kew.hosts import resolve_provider

        with pytest.raises(HostNotProvisioned, match='"local" or "gcp"'):
            resolve_provider(DispatchConfig(), provider="aws")


class TestHeldOutEvent:
    """AC4 (#2485) — the verify event records the held-out phase's outcome.

    The phase itself runs for real in ``tests/test_held_out_base_tests.py``;
    here the host's log is stubbed so the event is asserted for each verdict.
    """

    _FAILED_LOG = opened(
        "gate passed\n"
        f"{report_line('tests/test_calc.py::test_add')}"
        "kew-held-out: exit 1 seconds 41\n"
        f"{report_line()}kew-held-out: exit 0 seconds 7\n"
    )

    def _verify(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        load_cfg: Any,
        log: str,
        *args: str,
    ) -> tuple[Any, dict[str, Any]]:
        repo = _project(tmp_path)
        _enter(repo, monkeypatch)
        load_cfg.return_value = _config(repo, host="local")
        monkeypatch.setattr(LocalProvider, "_execute", lambda *_a, **_k: (0, log))
        result = runner.invoke(app, ["verify", "--host", "local", *args])
        events = _events(repo / "logs")
        assert len(events) == 1
        return result, _payload(events[0])

    @patch("kew.cli.load_config")
    def test_a_clean_held_out_phase_is_recorded_as_passed(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, payload = self._verify(
            tmp_path, monkeypatch, load_cfg, _HELD_OUT_PASSED
        )
        assert result.exit_code == 0, result.output
        assert payload["held_out"] == {
            "outcome": "passed",
            "failed": [],
            "declared": [],
            "undeclared": [],
            "seconds": 2,
            "error": None,
        }
        assert "held_out=passed held_out_seconds=2" in result.output

    @patch("kew.cli.load_config")
    def test_an_undeclared_held_out_failure_fails_and_is_recorded(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, payload = self._verify(
            tmp_path, monkeypatch, load_cfg, self._FAILED_LOG
        )
        assert result.exit_code == 1, result.output
        assert payload["outcome"] == "failed" and payload["exit_code"] == 1
        held_out = payload["held_out"]
        assert held_out["outcome"] == "failed"
        assert held_out["undeclared"] == ["tests/test_calc.py::test_add"]
        assert held_out["seconds"] == 48
        assert "tests/test_calc.py::test_add" in payload["error"]

    @patch("kew.cli.load_config")
    def test_a_held_out_failure_the_pr_body_declares_passes_and_is_recorded(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Beside the project, not in it: the CLI bundles the project root.
        body = tmp_path.parent / f"{tmp_path.name}-body.md"
        body.write_text(
            "## Behavior changes\n\n"
            "- `kew/tests/test_calc.py::test_add`: add now subtracts, on purpose\n",
            encoding="utf-8",
        )
        result, payload = self._verify(
            tmp_path,
            monkeypatch,
            load_cfg,
            self._FAILED_LOG,
            "--pr-body",
            str(body),
        )
        assert result.exit_code == 0, result.output
        assert payload["outcome"] == "passed"
        assert payload["held_out"]["outcome"] == "passed"
        assert payload["held_out"]["declared"] == ["tests/test_calc.py::test_add"]

    @patch("kew.cli.load_config")
    def test_a_gate_that_never_reported_the_held_out_phase_fails(
        self, load_cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, payload = self._verify(tmp_path, monkeypatch, load_cfg, "gate ok\n")
        assert result.exit_code == 1, result.output
        assert payload["held_out"]["outcome"] == "not_run"


class TestResultShape:
    """The outcome is read off the exit code, never inferred later."""

    @pytest.mark.parametrize(
        ("exit_code", "outcome"),
        [(0, "passed"), (1, "failed"), (None, "incomplete")],
    )
    def test_outcome_names_what_happened(
        self, exit_code: int | None, outcome: str
    ) -> None:
        result = VerifyResult(
            host="local", exit_code=exit_code, seconds=1.0, log="", error=None
        )
        assert result.outcome == outcome

    @pytest.mark.parametrize(
        ("status", "signal"),
        [
            (-9, 9),
            (-15, 15),
            (128 + 9, 9),
            (128 + 1, 1),
            (128 + 64, 64),
            (0, None),
            (1, None),
            (3, None),
            (128, None),
            (128 + 65, None),
            (255, None),
        ],
    )
    def test_a_kill_is_read_from_either_shape_and_from_nothing_else(
        self, status: int, signal: int | None
    ) -> None:
        """Both shapes a kill reaches kew in, and no status outside them.

        ``128 + N`` is a shell saying a command it ran was killed; past the
        signal range a status is the gate's own, and reading one as a kill
        would discard a real verdict (#2379 review).
        """
        assert killing_signal(status) == signal
