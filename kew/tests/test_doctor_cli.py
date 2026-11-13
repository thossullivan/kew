"""CLI tests for `kew doctor` — the command must be reachable as `kew doctor`.

Regression guard for the sub-app footgun: a single-command Typer group would
expose the check only as `kew doctor doctor` and make bare `kew doctor` error
with "Missing command".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from support.fake_gcloud import fake_gcloud_on_path, write_state
from typer.testing import CliRunner

import kew.cli.doctor as doctor_module
from kew import openshell, openshell_doctor, subprocess_utils
from kew.cli import app
from kew.config import DispatchConfig, SandboxConfig
from kew.model_eol_snapshot import LifecycleEntry
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_audit import emit_review_verdict
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord, SqliteRunStore
from kew.runner_liveness import RunnerLivenessResult
from kew.runtime_names import SUPPORTED_RUNTIMES
from kew.sandbox import _PACKAGED_IMAGE_TAGS, ImageStalenessResult

runner = CliRunner()
_REAL_TEMPORARY_STORAGE_CHECK = getattr(doctor_module, "_check_temporary_storage", None)
_REAL_RETAINED_AGENT_TAG_CHECK = getattr(
    doctor_module, "_check_retained_agent_tags", None
)

_IMAGE_LIST_FORMAT = "{{json .Repository}}\t{{json .Tag}}\t{{json .ID}}\t{{json .Size}}"
_DOCKER_CONTAINER_IMAGE_FORMAT = "{{json .Image}}"
_PODMAN_CONTAINER_IMAGE_FORMAT = "{{json .ImageID}}"
_IMAGE_ID_FORMAT = "{{json .Id}}"
_CLAUDE_PIN = "kew-agent:docker-111111111111"
_OPENCODE_PIN = "kew-agent-opencode:docker-222222222222"
_CODEX_PIN = "kew-agent-codex:docker-333333333333"
_IMAGE_A = f"sha256:{'a' * 64}"
_IMAGE_B = f"sha256:{'b' * 64}"
_IMAGE_C = f"sha256:{'c' * 64}"
_IMAGE_D = f"sha256:{'d' * 64}"


@pytest.fixture(autouse=True)
def _isolated_ledger_project(tmp_path):
    """Keep registry discovery inside the temporary test project."""
    (tmp_path / ".git").mkdir()


_REAL_GH_AUTH_STATUS = doctor_module._gh_auth_status


@pytest.fixture(autouse=True)
def _gh_is_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated doctor tests off the host's real ``gh`` login."""
    monkeypatch.setattr(
        doctor_module,
        "_gh_auth_status",
        lambda: _completed(["gh", "auth", "status"], stdout="Logged in"),
    )


@pytest.fixture(autouse=True)
def _editable_install_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated doctor tests independent of the checkout running them."""
    monkeypatch.setattr("kew.cli.doctor.check_editable_staleness", lambda: None)
    monkeypatch.setattr(
        "kew.cli.doctor.check_runner_liveness",
        lambda repo: RunnerLivenessResult(configured=False),
    )
    monkeypatch.setattr(
        "kew.cli.doctor._check_temporary_storage", lambda: None, raising=False
    )
    monkeypatch.setattr(
        "kew.cli.doctor._check_retained_agent_tags",
        lambda config, container_route: None,
        raising=False,
    )


def _image_row(repository: str, tag: str, identity: str, size: str) -> str:
    return "\t".join(json.dumps(value) for value in (repository, tag, identity, size))


def _container_row(reference: str) -> str:
    return json.dumps(reference)


def _runtime_repository(runtime: str, repository: str) -> str:
    if runtime == "podman":
        return f"localhost/{repository}"
    return repository


def _retained_inventory(
    images: dict[str, list[str]],
    *,
    containers: tuple[str, ...] = (),
    container_image_ids: tuple[str, ...] | None = None,
    container_returncode: int = 0,
    resolutions: dict[str, str] | None = None,
    commands: list[list[str]] | None = None,
):
    """Build a synthetic runtime inventory through the subprocess seam."""
    resolved = resolutions or {}
    observed = commands if commands is not None else []

    def run(cmd, *, check, timeout):
        observed.append(cmd)
        assert check is False
        assert timeout == 5
        if cmd[1:4] == ["image", "ls", "--all"]:
            assert cmd[4:] == [
                "--no-trunc",
                "--filter",
                f"reference={cmd[6].partition('=')[2]}",
                "--format",
                _IMAGE_LIST_FORMAT,
            ]
            repository = cmd[6].partition("=")[2]
            return _completed(cmd, stdout="\n".join(images.get(repository, ())))
        if cmd[1:4] == ["container", "ls", "--all"]:
            assert cmd[4:-1] == ["--no-trunc", "--format"]
            requested_format = cmd[-1]
            if requested_format == _DOCKER_CONTAINER_IMAGE_FORMAT:
                values = containers
            elif requested_format == _PODMAN_CONTAINER_IMAGE_FORMAT:
                values = container_image_ids or ()
            else:
                raise AssertionError(
                    f"unexpected container image format: {requested_format!r}"
                )
            return _completed(
                cmd,
                returncode=container_returncode,
                stdout="\n".join(_container_row(value) for value in values),
            )
        if cmd[1:3] == ["image", "inspect"]:
            assert cmd[3:] == ["--format", _IMAGE_ID_FORMAT, "--", cmd[-1]]
            identity = resolved.get(cmd[-1])
            if identity is None:
                return _completed(cmd, returncode=1, stderr="not found")
            return _completed(cmd, stdout=json.dumps(identity))
        raise AssertionError(f"unexpected runtime command: {cmd!r}")

    return run


def _run_retained_agent_tag_check(
    monkeypatch: pytest.MonkeyPatch,
    config: SandboxConfig,
    runtime_run,
    *,
    container_route: bool = True,
) -> tuple[str | None, list[str]]:
    messages: list[str] = []
    monkeypatch.setattr(
        doctor_module, "_check_retained_agent_tags", _REAL_RETAINED_AGENT_TAG_CHECK
    )
    monkeypatch.setattr(
        doctor_module,
        "_current_packaged_agent_pins",
        lambda: (_CLAUDE_PIN, _CODEX_PIN, _OPENCODE_PIN),
    )
    monkeypatch.setattr(
        doctor_module, "detect_runtime", lambda config: f"/bin/{config.runtime}"
    )
    monkeypatch.setattr(subprocess_utils, "run", runtime_run)
    monkeypatch.setattr(doctor_module, "_warn", messages.append)
    monkeypatch.setattr(doctor_module, "_success", messages.append)
    monkeypatch.setattr(doctor_module, "_dim", messages.append)
    issue = doctor_module._check_retained_agent_tags(config, container_route)
    return issue, messages


def _assert_only_read_only_agent_inventory(commands: list[list[str]]) -> None:
    allowed = {
        ("image", "ls"),
        ("image", "inspect"),
        ("container", "ls"),
    }
    assert commands
    assert all(tuple(command[1:3]) in allowed for command in commands)
    forbidden = {"build", "pull", "prune", "remove", "rm", "stop", "tag"}
    assert not any(forbidden.intersection(command[1:]) for command in commands)


def _write_config(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[logging]\ndir = "{logs}"\n'
    )


def _write_contained_config(tmp_path, *, image: str = "kew-agent:latest") -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "claude-code"\n'
        f'[sandbox.images]\nclaude-code = "{image}"\n'
        f'[logging]\ndir = "{logs}"\n'
    )


def _completed(
    cmd: list[str], returncode: int = 0, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _seed_review_reservation(tmp_path, *, finished_ago_seconds: int) -> None:
    """Reserve a ``review`` pseudo-epic slot + write its terminal run row,
    finished ``finished_ago_seconds`` ago, so `kew doctor` sees a review
    reservation whose run has ended (pinned iff past the terminal grace)."""
    db_path = tmp_path / "logs" / "dispatch.db"
    now = datetime.now(UTC)
    with SqliteRunStore(str(db_path)) as store:
        assert (
            store.reserve_epic_node_item_under_cap(REVIEW_EPIC_REF, "42", "rev-1", 0)
            == "reserved"
        )
        store.save(
            RunRecord(
                run_id="rev-1",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=now - timedelta(seconds=finished_ago_seconds + 30),
                finished_at=now - timedelta(seconds=finished_ago_seconds),
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )


def test_doctor_warns_for_stale_tags_in_every_exact_agent_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [
            _image_row("kew-agent", "old-claude", _IMAGE_A, "1.2GB"),
            _image_row("kew-agent-proxy", "old", _IMAGE_C, "900MB"),
            _image_row("ghcr.io/example/kew-agent", "old", _IMAGE_D, "800MB"),
        ],
        "kew-agent-opencode": [
            _image_row("kew-agent-opencode", "old-opencode", _IMAGE_B, "1.3GB"),
            _image_row("switchyard-kew-agent", "old", _IMAGE_C, "700MB"),
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "2 stale retained agent tags across 2 image identities"
    assert (
        "retained-agent-tags: WARN (2 stale tags across 2 image identities)" in output
    )
    assert "kew-agent:old-claude" in output
    assert "kew-agent-opencode:old-opencode" in output
    assert "kew-agent-proxy" not in output
    assert "ghcr.io" not in output
    assert "switchyard" not in output
    assert commands == [
        *(
            [
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"reference={repository}",
                "--format",
                _IMAGE_LIST_FORMAT,
            ]
            for repository in doctor_module._RETAINED_AGENT_REPOSITORIES
        ),
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--format",
            _DOCKER_CONTAINER_IMAGE_FORMAT,
        ],
    ]
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_reports_only_exact_and_podman_localhost_agent_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [
            _image_row("localhost/kew-agent", "old-claude", _IMAGE_A, "1.2GB"),
            _image_row("kew-agent", "legacy-claude", _IMAGE_B, "1.1GB"),
            _image_row("localhost/kew-agent-proxy", "old", _IMAGE_C, "900MB"),
            _image_row("registry.example/kew-agent", "old", _IMAGE_D, "800MB"),
        ],
        "kew-agent-opencode": [
            _image_row(
                "localhost/kew-agent-opencode", "old-opencode", _IMAGE_C, "1.3GB"
            ),
            _image_row("kew-agent-opencode", "legacy-opencode", _IMAGE_D, "1.2GB"),
            _image_row("localhost/team/kew-agent-opencode", "old", _IMAGE_A, "700MB"),
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime="podman"),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "4 stale retained agent tags across 4 image identities"
    assert "localhost/kew-agent:old-claude" in output
    assert "kew-agent:legacy-claude" in output
    assert "localhost/kew-agent-opencode:old-opencode" in output
    assert "kew-agent-opencode:legacy-opencode" in output
    assert "kew-agent-proxy" not in output
    assert "registry.example" not in output
    assert "localhost/team" not in output
    assert all(command[0] == "podman" for command in commands)
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_excludes_podman_localhost_repositories_from_docker_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [
            _image_row("localhost/kew-agent", "podman-only", _IMAGE_A, "1.2GB"),
            _image_row("kew-agent", "docker-only", _IMAGE_B, "1.1GB"),
        ],
        "kew-agent-opencode": [
            _image_row("localhost/kew-agent-opencode", "podman-only", _IMAGE_C, "1.3GB")
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime="docker"),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "1 stale retained agent tag across 1 image identity"
    assert "retained-agent-tags: kew-agent:docker-only ->" in output
    assert "retained-agent-tags: localhost/" not in output
    assert all(command[0] == "docker" for command in commands)
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_inspects_retained_tags_on_native_route_when_runtime_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [_image_row("kew-agent", "old", _IMAGE_A, "1.2GB")],
        "kew-agent-opencode": [],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(),
        _retained_inventory(inventory, commands=commands),
        container_route=False,
    )

    assert issue == "1 stale retained agent tag across 1 image identity"
    assert any("retained-agent-tags: WARN" in message for message in messages)
    _assert_only_read_only_agent_inventory(commands)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_doctor_protects_both_packaged_pins_and_latest_tags(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    commands: list[list[str]] = []
    claude_repository = _runtime_repository(runtime, "kew-agent")
    opencode_repository = _runtime_repository(runtime, "kew-agent-opencode")
    inventory = {
        "kew-agent": [
            _image_row(claude_repository, "docker-111111111111", _IMAGE_A, "1.0GB"),
            _image_row(claude_repository, "latest", _IMAGE_B, "1.1GB"),
            _image_row(claude_repository, "old", _IMAGE_C, "1.2GB"),
        ],
        "kew-agent-opencode": [
            _image_row(opencode_repository, "docker-222222222222", _IMAGE_A, "1.0GB"),
            _image_row(opencode_repository, "latest", _IMAGE_B, "1.1GB"),
            _image_row(opencode_repository, "old", _IMAGE_D, "1.3GB"),
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime=runtime),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "2 stale retained agent tags across 2 image identities"
    assert f"{claude_repository}:old" in output
    assert f"{opencode_repository}:old" in output
    assert f"{claude_repository}:docker-111111111111" not in output
    assert f"{opencode_repository}:docker-222222222222" not in output
    assert f"{claude_repository}:latest" not in output
    assert f"{opencode_repository}:latest" not in output
    _assert_only_read_only_agent_inventory(commands)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_doctor_protects_configured_tags_and_arbitrary_configured_aliases(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    commands: list[list[str]] = []
    arbitrary_alias = "registry.example/team/agent:configured"
    claude_repository = _runtime_repository(runtime, "kew-agent")
    opencode_repository = _runtime_repository(runtime, "kew-agent-opencode")
    inventory = {
        "kew-agent": [
            _image_row(claude_repository, "configured-old", _IMAGE_A, "1.0GB"),
            _image_row(claude_repository, "aliased", _IMAGE_C, "1.4GB"),
            _image_row(claude_repository, "unused", _IMAGE_D, "1.5GB"),
        ],
        "kew-agent-opencode": [
            _image_row(opencode_repository, "configured-old", _IMAGE_B, "1.2GB"),
            _image_row(opencode_repository, "unused", _IMAGE_D, "1.5GB"),
        ],
    }
    config = SandboxConfig(
        runtime=runtime,
        image="kew-agent:configured-old",
        images={
            "opencode": "kew-agent-opencode:configured-old",
            "pi": arbitrary_alias,
        },
    )

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        config,
        _retained_inventory(
            inventory,
            resolutions={arbitrary_alias: _IMAGE_C},
            commands=commands,
        ),
    )

    output = "\n".join(messages)
    assert issue == "2 stale retained agent tags across 1 image identity"
    assert f"{claude_repository}:unused" in output
    assert f"{opencode_repository}:unused" in output
    assert f"{claude_repository}:configured-old" not in output
    assert f"{opencode_repository}:configured-old" not in output
    assert f"{claude_repository}:aliased" not in output
    assert [
        runtime,
        "image",
        "inspect",
        "--format",
        _IMAGE_ID_FORMAT,
        "--",
        arbitrary_alias,
    ] in commands
    _assert_only_read_only_agent_inventory(commands)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_doctor_protects_running_and_stopped_container_images_by_alias_and_id(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    commands: list[list[str]] = []
    running_alias = "registry.example/team/active:run"
    claude_repository = _runtime_repository(runtime, "kew-agent")
    opencode_repository = _runtime_repository(runtime, "kew-agent-opencode")
    inventory = {
        "kew-agent": [
            _image_row(claude_repository, "running", _IMAGE_A, "1.0GB"),
            _image_row(claude_repository, "by-id", _IMAGE_C, "1.2GB"),
            _image_row(claude_repository, "unused", _IMAGE_D, "1.3GB"),
        ],
        "kew-agent-opencode": [
            _image_row(opencode_repository, "stopped", _IMAGE_B, "1.1GB")
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime=runtime),
        _retained_inventory(
            inventory,
            containers=(
                running_alias,
                f"{opencode_repository}:stopped",
                _IMAGE_C,
            ),
            container_image_ids=(_IMAGE_A, _IMAGE_B, _IMAGE_C),
            resolutions={running_alias: _IMAGE_A},
            commands=commands,
        ),
    )

    output = "\n".join(messages)
    assert issue == "1 stale retained agent tag across 1 image identity"
    assert f"{claude_repository}:unused" in output
    assert f"{claude_repository}:running" not in output
    assert f"{claude_repository}:by-id" not in output
    assert f"{opencode_repository}:stopped" not in output
    assert any(
        command[1:4] == ["container", "ls", "--all"] and "--all" in command
        for command in commands
    )
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_uses_podman_container_image_ids_after_creation_alias_retag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    moved_creation_alias = "registry.example/team/base:creation"
    inventory = {
        "kew-agent": [
            _image_row("localhost/kew-agent", "running", _IMAGE_A, "1.0GB"),
            _image_row("localhost/kew-agent", "stopped", _IMAGE_B, "1.1GB"),
            _image_row("localhost/kew-agent", "unused", _IMAGE_D, "1.3GB"),
        ],
        "kew-agent-opencode": [],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime="podman"),
        _retained_inventory(
            inventory,
            containers=(moved_creation_alias, _IMAGE_B),
            container_image_ids=(_IMAGE_A, _IMAGE_B),
            resolutions={moved_creation_alias: _IMAGE_C},
            commands=commands,
        ),
    )

    output = "\n".join(messages)
    assert issue == "1 stale retained agent tag across 1 image identity"
    assert "localhost/kew-agent:unused" in output
    assert "localhost/kew-agent:running" not in output
    assert "localhost/kew-agent:stopped" not in output
    assert [
        "podman",
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--format",
        _PODMAN_CONTAINER_IMAGE_FORMAT,
    ] in commands
    assert not any(
        command[1:3] == ["image", "inspect"] and command[-1] == moved_creation_alias
        for command in commands
    )
    _assert_only_read_only_agent_inventory(commands)


@pytest.mark.parametrize(
    ("container_image_ids", "container_returncode"),
    [
        pytest.param(("sha256:abc",), 0, id="malformed"),
        pytest.param((_IMAGE_A,), 1, id="unavailable"),
    ],
)
def test_doctor_reports_unknown_for_untrusted_podman_container_image_inventory(
    monkeypatch: pytest.MonkeyPatch,
    container_image_ids: tuple[str, ...],
    container_returncode: int,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [
            _image_row("localhost/kew-agent", "active", _IMAGE_A, "1.0GB"),
            _image_row("localhost/kew-agent", "apparently-unused", _IMAGE_D, "1.3GB"),
        ],
        "kew-agent-opencode": [],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(runtime="podman"),
        _retained_inventory(
            inventory,
            containers=(_IMAGE_A,),
            container_image_ids=container_image_ids,
            container_returncode=container_returncode,
            commands=commands,
        ),
    )

    output = "\n".join(messages)
    assert issue == "retained agent tag inventory unknown"
    assert "inventory unknown" in output
    assert "localhost/kew-agent:apparently-unused" not in output
    assert "stale tags across" not in output
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_deduplicates_tag_aliases_and_logical_image_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    inventory = {
        "kew-agent": [
            _image_row("kew-agent", "old-a", _IMAGE_A, "1.25GB"),
            _image_row("kew-agent", "old-b", _IMAGE_A, "1.25GB"),
            _image_row("kew-agent", "old-b", _IMAGE_A, "1.25GB"),
        ],
        "kew-agent-opencode": [],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "2 stale retained agent tags across 1 image identity"
    assert "kew-agent:old-a, kew-agent:old-b" in output
    assert output.count("logical size 1.25GB") == 1
    assert "physical" in output
    assert "reclaimed" not in output.lower()
    assert "reclaimable bytes" not in output.lower()
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_distinguishes_complete_empty_retained_tag_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(),
        _retained_inventory(
            {"kew-agent": [], "kew-agent-opencode": []}, commands=commands
        ),
    )

    assert issue is None
    assert any("retained-agent-tags: PASS (no stale tags found)" in m for m in messages)
    assert not any("unknown" in m.lower() for m in messages)
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_sweeps_every_packaged_agent_image_repository() -> None:
    """Doctor's retained-tag inventory must cover every packaged agent image.

    A packaged runner whose repository is missing here falls past
    ``_exact_agent_tag`` into a live ``image inspect`` that exits 1 for an
    image this host has not built, degrading the check to "inventory unknown"
    and failing `kew doctor` (#2317 review: the `[sandbox.images] codex` pin).
    """
    packaged = {tag.partition(":")[0] for tag in _PACKAGED_IMAGE_TAGS.values()}

    assert packaged <= set(doctor_module._RETAINED_AGENT_REPOSITORIES)
    assert {
        f"localhost/{repository}": repository
        for repository in doctor_module._RETAINED_AGENT_REPOSITORIES
    } == doctor_module._PODMAN_AGENT_REPOSITORIES


def test_doctor_protects_configured_codex_pin_without_live_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-runner pin naming a packaged repository resolves from the read-only
    inventory, never through an ``image inspect`` that exits 1 for an image this
    host has not built yet (#2317 review)."""
    commands: list[list[str]] = []
    config = SandboxConfig(images={"codex": "kew-agent-codex:docker-d148816b388f"})

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        config,
        _retained_inventory({}, commands=commands),
    )

    assert issue is None
    output = "\n".join(messages)
    assert "retained-agent-tags: PASS (no stale tags found)" in output
    assert "inventory unknown" not in output
    assert not any(command[1:3] == ["image", "inspect"] for command in commands)
    _assert_only_read_only_agent_inventory(commands)


def test_doctor_protects_the_codex_pin_and_latest_tag_it_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codex repository is swept for stale tags, with this install's pin and
    the stock ``latest`` tag protected exactly like the other packaged images."""
    commands: list[list[str]] = []
    inventory = {
        "kew-agent-codex": [
            _image_row("kew-agent-codex", "docker-333333333333", _IMAGE_A, "1.0GB"),
            _image_row("kew-agent-codex", "latest", _IMAGE_B, "1.1GB"),
            _image_row("kew-agent-codex", "old-codex", _IMAGE_C, "1.2GB"),
        ],
    }

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        SandboxConfig(),
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "1 stale retained agent tag across 1 image identity"
    assert "kew-agent-codex:old-codex" in output
    assert "kew-agent-codex:docker-333333333333" not in output
    assert "kew-agent-codex:latest" not in output
    _assert_only_read_only_agent_inventory(commands)


@pytest.mark.parametrize("failure", ["unavailable", "failed", "malformed", "timeout"])
def test_doctor_reports_unknown_retained_tag_inventory_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    commands: list[list[str]] = []
    messages: list[str] = []
    monkeypatch.setattr(
        doctor_module, "_check_retained_agent_tags", _REAL_RETAINED_AGENT_TAG_CHECK
    )
    monkeypatch.setattr(doctor_module, "_warn", messages.append)
    monkeypatch.setattr(doctor_module, "_success", messages.append)
    monkeypatch.setattr(
        doctor_module,
        "_current_packaged_agent_pins",
        lambda: (_CLAUDE_PIN, _CODEX_PIN, _OPENCODE_PIN),
    )

    if failure == "unavailable":
        monkeypatch.setattr(doctor_module, "detect_runtime", lambda config: None)
    else:
        monkeypatch.setattr(
            doctor_module, "detect_runtime", lambda config: "/bin/docker"
        )

        def fail(cmd, *, check, timeout):
            commands.append(cmd)
            assert check is False
            assert timeout == 5
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
            if failure == "failed":
                return _completed(cmd, returncode=1, stderr="inventory failed")
            return _completed(cmd, stdout="truncated-record")

        monkeypatch.setattr(subprocess_utils, "run", fail)

    issue = doctor_module._check_retained_agent_tags(SandboxConfig(), True)

    assert issue == "retained agent tag inventory unknown"
    assert any("retained-agent-tags: WARN (inventory unknown:" in m for m in messages)
    assert not any("PASS" in m for m in messages)
    if commands:
        _assert_only_read_only_agent_inventory(commands)


def test_doctor_does_not_report_candidates_when_protected_alias_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    protected_alias = "registry.example/team/agent:active"
    inventory = {
        "kew-agent": [_image_row("kew-agent", "apparently-old", _IMAGE_A, "1GB")],
        "kew-agent-opencode": [],
    }
    config = SandboxConfig(images={"pi": protected_alias})

    issue, messages = _run_retained_agent_tag_check(
        monkeypatch,
        config,
        _retained_inventory(inventory, commands=commands),
    )

    output = "\n".join(messages)
    assert issue == "retained agent tag inventory unknown"
    assert "inventory unknown" in output
    assert "kew-agent:apparently-old" not in output
    assert "stale tags across" not in output
    _assert_only_read_only_agent_inventory(commands)


def _doctor_page() -> str:
    return (
        Path(__file__).resolve().parents[2] / "docs" / "cli" / "doctor.md"
    ).read_text(encoding="utf-8")


def test_retained_agent_tag_docs_reject_storage_recovery_claims() -> None:
    lowered = _doctor_page().lower()
    assert "reclaimed bytes" not in lowered
    assert "reclaimable bytes" not in lowered


def test_retained_agent_tag_docs_name_every_repository_doctor_sweeps() -> None:
    """The page describes the inventory an operator is told to trust.

    `_RETAINED_AGENT_REPOSITORIES` is derived from the packaged image map, so
    adding a packaged runner silently widens the sweep and the page keeps
    describing the narrower one. It did: the page named `kew-agent` and
    `kew-agent-opencode` only, after `kew-agent-codex` joined both the sweep
    and the protected set (#2317 review). Derived from the code rather than
    restated, so the next packaged runner fails this instead of drifting.
    """
    page = _doctor_page()
    row = next(
        line
        for line in page.splitlines()
        if line.startswith("| `retained-agent-tags` |")
    )
    section = page.split("### Retained agent tags", 1)[1]

    for repository in doctor_module._RETAINED_AGENT_REPOSITORIES:
        assert f"`{repository}`" in row, repository
        assert f"`{repository}`" in section, repository

    # The counting words that made the old text false. "Both" was accurate of
    # two repositories, two pins and two `latest` tags, and became a specific
    # wrong number the moment a third packaged runner shipped.
    assert "both" not in section.lower()
    assert "both" not in row.lower()


def test_retained_agent_tag_docs_name_every_rebuild_command() -> None:
    """The page's recovery step must cover every pin the check protects.

    A protected pin whose rebuild command is absent leaves the operator who
    removed it with no documented way back (#2317 review).
    """
    section = _doctor_page().split("### Retained agent tags", 1)[1]

    for runner_name in _PACKAGED_IMAGE_TAGS:
        assert f"kew sandbox build --runner {runner_name}" in section, runner_name


def test_doctor_is_reachable_without_subcommand(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.cli.doctor._check_retained_agent_tags",
            _REAL_RETAINED_AGENT_TAG_CHECK,
        ),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "retained-agent-tags: skipped" in result.output
    assert "all checks passed" in result.output.lower()


def test_doctor_runs_the_temporary_storage_check(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor._check_temporary_storage", return_value=None) as check,
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    check.assert_called_once_with()


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [("_STORAGE_ENTRY_LIMIT", 1), ("_STORAGE_SCAN_SECONDS", -1.0)],
)
def test_doctor_exits_zero_when_storage_inventory_is_incomplete(
    tmp_path, monkeypatch, limit_name, limit_value
) -> None:
    """#2002: a capped temp inventory on a clean install is advisory only."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    temp_root = tmp_path / "temp"
    pytest_root = temp_root / "pytest-of-owner"
    pytest_root.mkdir(parents=True)
    pytest_root.joinpath("one.bin").write_bytes(b"1")
    pytest_root.joinpath("two.bin").write_bytes(b"22")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(doctor_module, limit_name, limit_value)
    monkeypatch.setattr(doctor_module, "_resolved_temp_roots", lambda: (temp_root,))
    monkeypatch.setattr(
        doctor_module, "_supported_uv_cache_dir", lambda: tmp_path / "missing-uv"
    )
    monkeypatch.setattr(
        doctor_module, "_check_temporary_storage", _REAL_TEMPORARY_STORAGE_CHECK
    )
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "incomplete lower bound" in result.output
    assert "all checks passed" in result.output.lower()


def _run_storage_check(**kwargs) -> tuple[str | None, list[str]]:
    assert callable(_REAL_TEMPORARY_STORAGE_CHECK), (
        "the temporary-storage reporting guard was removed"
    )
    messages: list[str] = []
    with (
        patch.object(doctor_module, "_log", side_effect=messages.append),
        patch.object(doctor_module, "_warn", side_effect=messages.append),
        patch.object(doctor_module, "_success", side_effect=messages.append),
        patch.object(doctor_module, "_dim", side_effect=messages.append),
    ):
        issue = _REAL_TEMPORARY_STORAGE_CHECK(**kwargs)
    return issue, messages


def test_doctor_reports_exact_temporary_and_uv_cache_sizes(tmp_path) -> None:
    temp_root = tmp_path / "temp"
    pytest_root = temp_root / "pytest-of-owner"
    task_root = temp_root / "kew-task-123"
    uv_cache = task_root / "uv-cache"
    environment = task_root / ".venv"
    ignored = task_root / "checkout"
    explicit_cache = tmp_path / "explicit-uv"
    default_cache = tmp_path / "default-uv"
    for directory in (
        pytest_root,
        uv_cache,
        environment,
        ignored,
        explicit_cache,
        default_cache,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    pytest_file = pytest_root / "data.bin"
    pytest_file.write_bytes(b"p" * 11)
    os.link(pytest_file, pytest_root / "same-inode.bin")
    uv_cache.joinpath("cache.bin").write_bytes(b"u" * 13)
    environment.joinpath("python.bin").write_bytes(b"v" * 7)
    ignored.joinpath("source.bin").write_bytes(b"x" * 19)
    explicit_cache.joinpath("archive.bin").write_bytes(b"e" * 23)
    default_cache.joinpath("archive.bin").write_bytes(b"d" * 29)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("private-name.bin").write_bytes(b"s" * 31)
    pytest_root.joinpath("linked-private").symlink_to(outside, target_is_directory=True)
    alias = tmp_path / "temp-alias"
    alias.symlink_to(temp_root, target_is_directory=True)

    issue, messages = _run_storage_check(
        temp_roots=(temp_root, alias),
        explicit_uv_cache=explicit_cache,
        default_uv_cache=default_cache,
    )
    output = "\n".join(messages)

    assert issue is None
    assert "pytest: 1 directory, 11 apparent bytes" in output
    assert "task caches/environments: 2 directories, 20 apparent bytes" in output
    assert f"explicit UV_CACHE_DIR ({explicit_cache}): 23 apparent bytes" in output
    assert f"uv default ({default_cache}): 29 apparent bytes" in output
    assert "private-name.bin" not in output


def test_doctor_reports_missing_storage_as_zero(tmp_path) -> None:
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path / "missing-temp",),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )
    output = "\n".join(messages)

    assert issue is None
    assert "pytest: 0 directories, 0 apparent bytes" in output
    assert "task caches/environments: 0 directories, 0 apparent bytes" in output
    assert "explicit UV_CACHE_DIR: not set" in output
    assert "uv default" in output and "0 apparent bytes" in output


def test_doctor_counts_the_work_root_toward_temporary_storage(
    tmp_path, monkeypatch
) -> None:
    """The trees kew keeps under ``[paths] work_root`` are in the inventory."""
    work_root = tmp_path / "work-root"
    tree = work_root / "ledger-id" / "kew-sandbox-1-abc"
    tree.mkdir(parents=True)
    tree.joinpath("file.bin").write_bytes(b"w" * 17)
    monkeypatch.setattr(doctor_module, "_TEMP_STORAGE_WARNING_BYTES", 16)
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path / "missing-temp",),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
        work_root=work_root,
    )
    output = "\n".join(messages)

    assert f"work root ({work_root}): 17 apparent bytes" in output
    assert issue == "temporary storage exceeds 2 GiB"


def test_doctors_own_inventory_measures_the_configured_work_root(
    tmp_path, monkeypatch
) -> None:
    """With no roots named, doctor measures the machine's: the work root too.

    A caller that names its own roots measures only those.
    """
    work_root = tmp_path / "configured-root"
    monkeypatch.setenv("KEW_WORK_ROOT", str(work_root))
    named = (tmp_path / "temp",)
    assert doctor_module._measured_work_root(None, None) == work_root
    assert doctor_module._measured_work_root(None, named) is None
    assert not work_root.exists(), "measuring the work root never creates it"


def test_doctor_uses_uv_supported_default_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(doctor_module.Path, "home", lambda: tmp_path)
    supported_cache = getattr(doctor_module, "_supported_uv_cache_dir", None)

    assert callable(supported_cache), "the supported uv cache guard was removed"
    assert supported_cache() == tmp_path / ".cache" / "uv"


def test_doctor_reports_private_uv_cache_separately_without_losing_its_size(
    tmp_path,
) -> None:
    uv_cache = tmp_path / "kew-task" / "uv-cache"
    uv_cache.mkdir(parents=True)
    uv_cache.joinpath("archive.bin").write_bytes(b"cache")

    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=uv_cache,
        default_uv_cache=tmp_path / "missing-default",
    )
    output = "\n".join(messages)

    assert issue is None
    assert "task caches/environments: 1 directory, 5 apparent bytes" in output
    assert f"explicit UV_CACHE_DIR ({uv_cache}): 5 apparent bytes" in output


@pytest.mark.parametrize("cache_view", ["default", "explicit", "same_path"])
@pytest.mark.parametrize("cache_location", ["separate", "task"])
def test_doctor_counts_task_hardlinks_in_each_cache_view(
    tmp_path, cache_view, cache_location
) -> None:
    temp_root = tmp_path / "temp"
    environment = temp_root / "kew-task" / ".venv"
    environment.mkdir(parents=True)
    cache_parent = environment.parent if cache_location == "task" else tmp_path
    cache = cache_parent / "uv-cache"
    cache.mkdir()
    payload = cache / "payload.bin"
    payload.write_bytes(b"cache")
    linked = environment / "payload.bin"
    os.link(payload, linked)
    assert payload.stat().st_ino == linked.stat().st_ino
    assert payload.stat().st_nlink == 2
    if cache_location == "task":
        pytest_root = temp_root / "pytest-of-owner"
        pytest_root.mkdir()
        os.link(payload, pytest_root / "payload.bin")
    explicit = cache if cache_view in {"explicit", "same_path"} else None
    default = cache if cache_view in {"default", "same_path"} else tmp_path / "missing"

    issue, messages = _run_storage_check(
        temp_roots=(temp_root,), explicit_uv_cache=explicit, default_uv_cache=default
    )
    output = "\n".join(messages)

    assert issue is None
    count = "2 directories" if cache_location == "task" else "1 directory"
    task_bytes = 0 if cache_location == "task" else 5
    assert f"task caches/environments: {count}, {task_bytes} apparent bytes" in output
    if explicit is not None:
        assert f"explicit UV_CACHE_DIR ({cache}): 5 apparent bytes" in output
    if default == cache:
        assert f"uv default ({cache}): 5 apparent bytes" in output


@pytest.mark.parametrize("aliased_view", ["explicit", "default"])
def test_doctor_cache_alias_stays_unfollowed_without_hiding_real_cache(
    tmp_path, aliased_view
) -> None:
    temp_root = tmp_path / "temp"
    environment = temp_root / "kew-task" / ".venv"
    environment.mkdir(parents=True)
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    payload = cache / "payload.bin"
    payload.write_bytes(b"cache")
    os.link(payload, environment / "payload.bin")
    alias = tmp_path / "cache-alias"
    alias.symlink_to(cache, target_is_directory=True)
    explicit, default = (alias, cache) if aliased_view == "explicit" else (cache, alias)

    issue, messages = _run_storage_check(
        temp_roots=(temp_root,), explicit_uv_cache=explicit, default_uv_cache=default
    )
    output = "\n".join(messages)

    assert issue is None
    assert "incomplete lower bound" in output
    assert "PASS" not in output
    real_label = "uv default" if aliased_view == "explicit" else "explicit UV_CACHE_DIR"
    alias_label = (
        "explicit UV_CACHE_DIR" if aliased_view == "explicit" else "uv default"
    )
    assert f"{real_label} ({cache}): 5 apparent bytes" in output
    assert f"{alias_label} ({alias}): 0 apparent bytes" in output


def test_doctor_keeps_inode_dedup_shared_within_temporary_total(
    tmp_path, monkeypatch
) -> None:
    pytest_root = tmp_path / "pytest-of-owner"
    environment = tmp_path / "kew-task" / ".venv"
    pytest_root.mkdir()
    environment.mkdir(parents=True)
    payload = pytest_root / "payload.bin"
    payload.write_bytes(b"cache")
    os.link(payload, environment / "payload.bin")
    monkeypatch.setattr(doctor_module, "_TEMP_STORAGE_WARNING_BYTES", 5)

    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing",
    )
    output = "\n".join(messages)

    assert issue is None
    assert "pytest: 1 directory, 5 apparent bytes" in output
    assert "task caches/environments: 1 directory, 0 apparent bytes" in output


def test_doctor_warns_when_temporary_storage_exceeds_threshold(
    tmp_path, monkeypatch
) -> None:
    import kew.cli.doctor as doctor_module

    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    pytest_root.joinpath("large.bin").write_bytes(b"large")
    monkeypatch.setattr(doctor_module, "_TEMP_STORAGE_WARNING_BYTES", 4)

    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )

    assert issue == "temporary storage exceeds 2 GiB"
    assert any("WARN" in message and "lower" not in message for message in messages)


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [("_STORAGE_ENTRY_LIMIT", 1), ("_STORAGE_SCAN_SECONDS", -1.0)],
)
def test_doctor_reports_bounded_inventory_as_incomplete(
    tmp_path, monkeypatch, limit_name, limit_value
) -> None:
    import kew.cli.doctor as doctor_module

    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    pytest_root.joinpath("one.bin").write_bytes(b"1")
    pytest_root.joinpath("two.bin").write_bytes(b"22")
    monkeypatch.setattr(doctor_module, limit_name, limit_value)

    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )

    assert issue is None
    assert any("incomplete lower bound" in message for message in messages)


def test_doctor_reports_permission_failure_as_incomplete(tmp_path, monkeypatch) -> None:
    import kew.cli.doctor as doctor_module

    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    real_scandir = doctor_module.os.scandir

    def deny_pytest_root(path):
        if _scanning_directory(path, pytest_root):
            raise PermissionError("synthetic denial")
        return real_scandir(path)

    monkeypatch.setattr(doctor_module.os, "scandir", deny_pytest_root)
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )

    assert issue is None
    assert any("incomplete lower bound" in message for message in messages)


def test_doctor_reports_a_disappearing_entry_as_incomplete(
    tmp_path, monkeypatch
) -> None:
    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    vanishing = pytest_root / "vanishing.bin"
    vanishing.write_bytes(b"gone")
    real_scandir = doctor_module.os.scandir

    class VanishingEntries:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            vanishing.unlink()
            return iter(self.entries)

    def remove_before_metadata(path):
        iterator = real_scandir(path)
        if _scanning_directory(path, pytest_root):
            with iterator:
                return VanishingEntries(list(iterator))
        return iterator

    monkeypatch.setattr(doctor_module.os, "scandir", remove_before_metadata)
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )

    assert issue is None
    assert any("incomplete lower bound" in message for message in messages)


def test_doctor_does_not_follow_a_replaced_directory(tmp_path, monkeypatch) -> None:
    pytest_root = tmp_path / "pytest-of-owner"
    victim = pytest_root / "nested"
    victim.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("marker.bin").write_bytes(b"x" * 123)
    children = doctor_module._StorageInventory.children
    replaced = False

    def replace_before_descent(inventory, path):
        nonlocal replaced
        if path == victim and not replaced:
            victim.rename(pytest_root / "parked")
            victim.symlink_to(outside, target_is_directory=True)
            replaced = True
        return children(inventory, path)

    monkeypatch.setattr(
        doctor_module._StorageInventory, "children", replace_before_descent
    )
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )
    output = "\n".join(messages)

    assert replaced
    assert "pytest: 1 directory, 0 apparent bytes" in output, output
    assert issue is None
    assert "incomplete lower bound" in output
    assert "PASS" not in output


@pytest.mark.parametrize("phase", ["tree_bytes", "children"])
def test_doctor_reports_discovered_directory_loss_as_incomplete(
    tmp_path, monkeypatch, phase
) -> None:
    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    original = getattr(doctor_module._StorageInventory, phase)
    removed = False

    def remove_before_descent(inventory, path):
        nonlocal removed
        if path == pytest_root and not removed:
            pytest_root.rmdir()
            removed = True
        return original(inventory, path)

    monkeypatch.setattr(doctor_module._StorageInventory, phase, remove_before_descent)
    issue, messages = _run_storage_check(
        temp_roots=(tmp_path,),
        explicit_uv_cache=None,
        default_uv_cache=tmp_path / "missing-default",
    )

    assert removed
    assert issue is None, messages
    assert any("incomplete lower bound" in message for message in messages)
    assert not any("PASS" in message for message in messages)


def _scanning_directory(path, directory: Path) -> bool:
    if isinstance(path, int):
        opened = os.fstat(path)
        expected = directory.stat()
        return (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
    return Path(path) == directory


def test_doctor_reports_directory_iteration_errors_as_incomplete(
    tmp_path, monkeypatch
) -> None:
    pytest_root = tmp_path / "pytest-of-owner"
    pytest_root.mkdir()
    scandir = os.scandir

    class BrokenIterator:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("synthetic directory iteration failure")

    def fail_iteration(path):
        return (
            BrokenIterator()
            if _scanning_directory(path, pytest_root)
            else scandir(path)
        )

    monkeypatch.setattr(doctor_module.os, "scandir", fail_iteration)
    failure = None
    try:
        issue, messages = _run_storage_check(
            temp_roots=(tmp_path,),
            explicit_uv_cache=None,
            default_uv_cache=tmp_path / "missing-default",
        )
    except OSError as exc:
        failure = exc

    assert failure is None, f"Storage check escaped with {failure!r}"
    assert issue is None, messages
    assert any("incomplete lower bound" in message for message in messages)
    assert not any("PASS" in message for message in messages)


def test_doctor_fails_when_container_runtime_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "container-runtime: FAIL" in result.output
    assert "configured runtime 'docker' is not on PATH" in result.output
    assert "install 'docker'" in result.output
    assert "change [sandbox] runtime" in result.output
    assert "dangerously_skip_containment = true" in result.output


def test_doctor_missing_podman_runtime_uses_configured_runtime_hint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        config_path.read_text().replace(
            "[sandbox.images]", '[sandbox]\nruntime = "podman"\n[sandbox.images]'
        )
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "container-runtime: FAIL" in result.output
    assert "install 'podman'" in result.output
    assert "change [sandbox] runtime" in result.output
    assert "Docker Desktop or OrbStack" not in result.output


def test_doctor_fails_when_container_runtime_does_not_answer(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    timeout = subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=5)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=timeout) as runtime_run,
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="runtime unavailable"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "container-runtime: FAIL" in result.output
    assert "did not answer within five seconds" in result.output
    runtime_run.assert_called_once_with(["docker", "info"], check=False, timeout=5)


def test_doctor_fails_when_default_route_image_is_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    image = "kew-agent:latest"
    _write_contained_config(tmp_path, image=image)

    def inspect(cmd, **_kwargs):
        if cmd[1:] == ["info"]:
            return _completed(cmd)
        if cmd[1:] == ["image", "inspect", image]:
            return _completed(cmd, 1, stderr=f"No such image: {image}")
        return _completed(cmd)

    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=inspect) as runtime_run,
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert f"route-image: FAIL ({image!r} is not present locally)" in result.output
    assert "docker build -f docker/agent.Dockerfile" in result.output
    assert any(
        call.args[0] == ["docker", "image", "inspect", image]
        for call in runtime_run.call_args_list
    )


def test_doctor_inspects_the_image_mapped_to_the_default_route(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    image = "team-claude-agent:test"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[routing]\ndefault = "claude-code"\n'
        f'[sandbox.images]\nclaude-code = "{image}"\n'
        f'[logging]\ndir = "{logs}"\n'
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ) as runtime_run,
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ) as staleness,
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert f"route-image: PASS ({image!r} is present locally" in result.output
    assert any(
        call.args[0] == ["docker", "image", "inspect", image]
        for call in runtime_run.call_args_list
    )
    staleness.assert_called_once_with("docker", image, runner="claude-code")


def test_doctor_warns_when_auto_mode_admits_host_without_runtime(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        config_path.read_text().replace(
            "[sandbox.images]",
            "[sandbox]\ndangerously_skip_containment = true\n[sandbox.images]",
        )
    )

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch("kew.cli.doctor.detect_runtime") as detect,
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: WARN" in result.output
        assert "runtime 'docker' not found" in result.output
    detect.assert_not_called()


def test_doctor_warns_when_auto_mode_admits_host_without_credential(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        config_path.read_text().replace(
            "[sandbox.images]",
            "[sandbox]\ndangerously_skip_containment = true\n[sandbox.images]",
        )
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.detect_runtime") as detect,
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: WARN" in result.output
        assert "no claude-code credential" in result.output
    detect.assert_not_called()


def test_doctor_reports_fail_closed_missing_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("fresh"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "containment: FAIL (credential_missing)" in result.output
    assert "no claude-code credential in the environment" in result.output
    assert "dangerously_skip_containment = true" in result.output


def test_doctor_rebuilds_stale_opencode_image_with_its_dockerfile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    image = "kew-agent-opencode:docker-0123456789ab"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "opencode"\n'
        f'[sandbox.images]\nopencode = "{image}"\n'
        f'[logging]\ndir = "{logs}"\n'
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-token")
    image_created = datetime(2026, 1, 1, tzinfo=UTC)
    inputs_changed = datetime(2026, 1, 2, tzinfo=UTC)

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ),
        patch("kew.sandbox._get_image_created_ts", return_value=image_created),
        patch("kew.sandbox._repo_root", return_value=tmp_path),
        patch("kew.sandbox._get_dockerfile_ts", return_value=inputs_changed),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "agent-image: WARN (Agent image" in result.output
    assert (
        f"docker build -f docker/agent-opencode.Dockerfile -t {image} ."
        in result.output
    )
    assert "docker/agent.Dockerfile -t" not in result.output


def test_doctor_does_not_prescribe_stock_rebuild_for_stale_custom_image(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    image = "registry.example/team/hardened-agent:current"
    _write_contained_config(tmp_path, image=image)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")
    image_created = datetime(2026, 1, 1, tzinfo=UTC)
    inputs_changed = datetime(2026, 1, 2, tzinfo=UTC)

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ),
        patch("kew.sandbox._get_image_created_ts", return_value=image_created),
        patch("kew.sandbox._repo_root", return_value=tmp_path),
        patch("kew.sandbox._get_dockerfile_ts", return_value=inputs_changed),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert f"Agent image {image!r} predates its build inputs" in result.output
    assert "kew cannot rebuild it" in result.output
    assert "docker build" not in result.output


def test_doctor_warns_when_auto_mode_admits_uncontainable_runner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "lemonade"\n'
        "[sandbox]\ndangerously_skip_containment = true\n"
        f'[logging]\ndir = "{logs}"\n'
    )

    with (
        patch("kew.cli.doctor.detect_runtime") as detect,
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: WARN" in result.output
        assert "host-local inference server" in result.output
    detect.assert_not_called()


def test_doctor_rejects_runtime_missing_dispatch_allowlist_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    image = "team-claude-agent:test"
    runtime = "custom-runtime"
    resolved_runtime = "/opt/tools/custom-runtime"
    # [sandbox] runtime is validated against the runtime registry, so this name
    # can no longer be written in a config file. The gate it pins is still
    # live, one layer in: a runtime kew learns to accept but nobody adds to the
    # subprocess allowlist must FAIL the doctor check, not be run anyway. The
    # registry is widened here to stage exactly that oversight.
    monkeypatch.setattr("kew.config.SUPPORTED_RUNTIMES", (*SUPPORTED_RUNTIMES, runtime))
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "claude-code"\n'
        f'[sandbox]\nruntime = "{runtime}"\n'
        f'[sandbox.images]\nclaude-code = "{image}"\n'
        f'[logging]\ndir = "{logs}"\n'
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")
    monkeypatch.delitem(subprocess_utils.TOOL_PATHS, runtime, raising=False)

    with (
        patch("kew.cli.doctor.detect_runtime", return_value=resolved_runtime),
        patch("kew.sandbox.detect_runtime_cached", return_value=resolved_runtime),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", wraps=subprocess_utils.run) as runtime_run,
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "container-runtime: FAIL" in result.output
    assert f"Unknown executable {runtime!r}" in result.output
    assert runtime not in subprocess_utils.TOOL_PATHS
    runtime_run.assert_called_once_with([runtime, "info"], check=False, timeout=5)


def test_doctor_reports_containment_before_required_tool_failure(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)

    with patch(
        "kew.cli.doctor.register_tools", return_value={"gh": "https://cli.github.com"}
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: PASS (not needed on this route" in result.output


def test_doctor_fails_when_allowlist_network_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)

    def inspect(cmd, **_kwargs):
        if cmd[1:] == ["network", "inspect", "kew-allowlist"]:
            return _completed(cmd, 1, stderr="network not found")
        return _completed(cmd)

    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=inspect),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "allowlist-network: FAIL ('kew-allowlist' is not present)" in result.output
    assert "kew sandbox network" in result.output
    assert "kew installs no firewall rules on it" in result.output


@pytest.mark.parametrize(
    ("driver_key", "internal_key"),
    [
        pytest.param("Driver", "Internal", id="docker"),
        pytest.param("driver", "internal", id="libpod"),
    ],
)
@pytest.mark.parametrize(
    ("internal", "warned"),
    [(False, True), (True, False)],
)
def test_doctor_warns_when_the_allowlist_network_is_a_plain_bridge(
    tmp_path, monkeypatch, driver_key, internal_key, internal, warned
) -> None:
    """A present network still says nothing about egress unless it is internal.

    Docker and Podman spell these two attributes differently, so the WARN has
    to read whichever runtime the operator configured (#2313).
    """
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    network = {driver_key: "bridge", internal_key: internal}

    def inspect(cmd, **_kwargs):
        if cmd[1:] == ["network", "inspect", "kew-allowlist"]:
            return _completed(cmd, stdout=json.dumps([network]))
        return _completed(cmd)

    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=inspect),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert "allowlist-network: PASS ('kew-allowlist' is present)" in result.output
    warning = (
        "allowlist-network: WARN (kew-allowlist is a plain bridge: egress is "
        "open unless the host filters it, and kew cannot see host filters.)"
    )
    assert (warning in result.output) is warned


def test_doctor_missing_allowlist_network_fix_is_runtime_neutral_on_podman(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        config_path.read_text().replace(
            "[sandbox.images]", '[sandbox]\nruntime = "podman"\n[sandbox.images]'
        )
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")

    def inspect(cmd, **_kwargs):
        if cmd[1:] == ["network", "inspect", "kew-allowlist"]:
            return _completed(cmd, 1, stderr="network not found")
        return _completed(cmd)

    with (
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/podman"),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/podman"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=inspect),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "allowlist-network: FAIL ('kew-allowlist' is not present)" in result.output
    # `kew sandbox network` creates it on whichever runtime is configured, so
    # the fix names no runtime binary at all (#2501).
    assert "kew sandbox network" in result.output
    assert "network create" not in result.output


def test_doctor_reports_containment_checks_not_needed_for_native_route(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime") as detect,
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run") as runtime_run,
        patch("kew.cli.doctor._check_retained_agent_tags", return_value=None) as tags,
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: PASS (not needed on this route" in result.output
    detect.assert_not_called()
    runtime_run.assert_not_called()
    tags.assert_called_once()
    assert tags.call_args.args[0].runtime == "docker"
    assert tags.call_args.args[1] is False


def test_doctor_reports_offline_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    monkeypatch.setattr(
        "kew.cli.doctor.check_runner_liveness",
        lambda repo: RunnerLivenessResult(
            configured=True, offline_runners=("Operator-Mac-1",)
        ),
    )
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "runner-liveness" in result.output
    assert "Operator-Mac-1" in result.output


def test_doctor_reports_stale_editable_install(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.cli.doctor.check_editable_staleness",
            return_value=(
                "editable install is 3 commits behind origin/main — "
                "run git merge --ff-only origin/main"
            ),
        ),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "editable-install" in result.output
    assert "3 commits" in result.output


def test_doctor_warns_on_retired_route_without_refusing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = DispatchConfig()
    config.agent.runner = "codex"
    config.agent.model = "retired-model"
    config.model_eol.snapshot_models = {
        "retired-model": LifecycleEntry(status="retired", shutdown="2026-07-01")
    }
    with (
        patch("kew.cli.doctor.load_config", return_value=config),
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "model-eol" in result.output
    assert "retired" in result.output


def test_doctor_stale_image_exits_nonzero(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    stale = ImageStalenessResult("stale", warning="rebuild me")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ),
        patch("kew.cli.doctor.evaluate_agent_image_staleness", return_value=stale),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "rebuild me" in result.output


def test_doctor_unreadable_dispatch_db_is_skipped_not_crashed(
    tmp_path, monkeypatch
) -> None:
    """An older-schema (pre-v25), empty/foreign, or corrupt dispatch.db reaches
    the reservation query as a raw sqlite3 error — not SchemaTooNewError, which
    only rejects NEWER schemas. `kew doctor` must skip the check with the DB
    error rather than crash (#972 review Finding 3 residual)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    # A foreign/corrupt file at the dispatch.db path: read-only open succeeds but
    # the reservation query raises sqlite3.DatabaseError ("file is not a database").
    (tmp_path / "logs" / "dispatch.db").write_text("not a sqlite database at all")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "reservations: skipped" in result.output
    assert "unreadable" in result.output


def test_doctor_flags_pinned_review_reservation(tmp_path, monkeypatch) -> None:
    """#979-F2: a stranded ``review`` reservation whose run ended past the
    terminal grace must be swept by `kew doctor` (not just delegation) — it
    reports the ``review:<item>`` leak and exits non-zero."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _seed_review_reservation(tmp_path, finished_ago_seconds=600)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "reservations: WARN (1 pinned" in result.output
    assert f"{REVIEW_EPIC_REF}:42" in result.output


def test_doctor_does_not_flag_review_reservation_within_grace(
    tmp_path, monkeypatch
) -> None:
    """A review reservation whose run finished within the terminal grace is the
    healthy just-finished window — `kew doctor` must NOT flag it (exit 0)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _seed_review_reservation(tmp_path, finished_ago_seconds=1)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "reservations: PASS (no pinned reservations)" in result.output


@pytest.mark.parametrize("crash_verdict", ["DEGRADED", "UNPARSEABLE"])
def test_doctor_surfaces_consecutive_degraded_review_lane(
    tmp_path, monkeypatch, crash_verdict
) -> None:
    """A dark reviewer route is visible without waiting for shadow scoring.

    Both crash verdicts count: advisory review attests DEGRADED, blocking review
    attests UNPARSEABLE (#1393) — the same dark lane under two policies, so
    neither policy hides it from the operator."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    db_path = tmp_path / "logs" / "dispatch.db"
    config = DispatchConfig()
    config.review.adversarial = True
    config.review.model = "openrouter/moonshotai/kimi-k2.7-code"
    config.logging.dir = str(tmp_path / "logs")
    with SqliteRunStore(str(db_path)) as store:
        now = datetime.now(UTC)
        for index in range(3):
            store.save(
                RunRecord(
                    run_id=f"degraded-{index}",
                    issue_num=index + 1,
                    item_id=str(index + 1),
                    branch=f"agent/issue-{index + 1}",
                    status="failure",
                    exit_code=-1,
                    started_at=now,
                    finished_at=now,
                    error_category="launch_failed",
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                    runner_name="opencode",
                    model=config.review.model,
                )
            )
            emit_review_verdict(
                store,
                config.review,
                pr_number=index + 1,
                item_id=str(index + 1),
                head_sha=f"head-{index}",
                review_round=1,
                verdict=crash_verdict,
                reviewer_model=config.review.model,
                run_id=f"degraded-{index}",
            )

    with (
        patch("kew.cli.doctor.load_config", return_value=config),
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "degraded-review" in result.output
    assert "opencode/openrouter/moonshotai/kimi-k2.7-code" in result.output


def test_doctor_unknown_image_is_not_reported_as_pass(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-token")
    unknown = ImageStalenessResult("unknown", detail="docker unavailable")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.subprocess_utils.run",
            side_effect=lambda cmd, **_kwargs: _completed(cmd),
        ),
        patch("kew.cli.doctor.evaluate_agent_image_staleness", return_value=unknown),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    # Indeterminate check is surfaced as skipped, never as "up to date".
    assert "skipped" in result.output.lower()
    assert "up to date" not in result.output.lower()


def test_doctor_prints_canonical_db_path(tmp_path, monkeypatch) -> None:
    """kew doctor always prints the canonical dispatch.db path so operators
    know where to run raw queries (#1037)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "dispatch.db" in result.output
    assert str(tmp_path / "logs") in result.output


def test_doctor_warns_on_shadow_dispatch_db(tmp_path, monkeypatch) -> None:
    """kew doctor exits non-zero and warns when a shadow dispatch.db exists at
    CWD while the canonical store is at a different configured path (#1037)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "dispatch.db").write_bytes(b"")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "shadow-db: WARN (found" in result.output
    canonical = str(tmp_path / "logs" / "dispatch.db")
    assert canonical in result.output


def test_doctor_no_shadow_warning_when_cwd_is_canonical(tmp_path, monkeypatch) -> None:
    """When the CWD dispatch.db IS the canonical store, no shadow warning is
    emitted (#1037)."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    (tmp_path / "dispatch.db").write_bytes(b"")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert "shadow dispatch.db" not in result.output


# ── the one action that spends money to diagnose (#1516) ────────────────────
#
# `kew doctor` is a read-only command; `kew doctor route-live-confirm` is the
# single exception, and it is a separate, explicitly confirmed subcommand for
# exactly that reason. These tests hold that line: bare doctor never reaches a
# provider, and the subcommand cannot spend without being told to.


def _write_uncontained_config(tmp_path) -> None:
    """A config whose complex route resolves without a container runtime."""
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n'
        f"[sandbox]\nenabled = false\ndangerously_skip_containment = true\n"
        f'[logging]\ndir = "{logs}"\n'
    )
    (logs / "dispatch.db").touch()


def _confirmation(status, **overrides):
    from kew.readiness import ProbeCost
    from kew.readiness_probe import LiveConfirmation

    return LiveConfirmation(
        status=status,
        fingerprint="f" * 64,
        runner="claude-code",
        requested_model="claude-opus-5",
        detail=overrides.pop("detail", "detail"),
        cost=overrides.pop("cost", ProbeCost.observed(0.004, "claude-code")),
        **overrides,
    )


def test_bare_doctor_never_reaches_a_provider(tmp_path, monkeypatch) -> None:
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live") as spend,
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.UNVERIFIED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    spend.assert_not_called()
    assert result.exit_code == 1, result.output
    for check in ("container-runtime", "route-image", "allowlist-network"):
        assert f"{check}: WARN (host path admitted" in result.output
    # Static checks passing must never READ as live validity.
    assert "live-unverified" in result.output


def test_route_live_confirm_will_not_spend_without_being_told_to(
    tmp_path, monkeypatch
) -> None:
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live") as spend,
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.UNVERIFIED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor", "route-live-confirm"], input="n\n")

    # A refusal never crossed the boundary: the confirmation could not run (2),
    # which is distinct from a completed probe that refuted the route (1).
    assert result.exit_code == 2, result.output
    spend.assert_not_called()


def test_route_live_confirm_reports_a_confirmed_boundary(tmp_path, monkeypatch) -> None:
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    confirmed = _confirmation(LiveStatus.CONFIRMED, spent=True, probe_id="p-1")
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live", return_value=confirmed) as spend,
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.UNVERIFIED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor", "route-live-confirm", "--yes"])

    assert result.exit_code == 0, result.output
    assert spend.call_args.kwargs["complexity"] == "complex"
    assert "confirmed" in result.output
    assert "0.0040" in result.output


def test_route_live_confirm_exits_nonzero_when_the_boundary_is_not_live(
    tmp_path, monkeypatch
) -> None:
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    refuted = _confirmation(
        LiveStatus.REFUTED, spent=True, detail="expected exactly 'OK'"
    )
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live", return_value=refuted),
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.UNVERIFIED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor", "route-live-confirm", "--yes"])

    assert result.exit_code == 1, result.output
    assert "NOT live" in result.output


def test_route_live_confirm_exits_two_when_the_probe_is_refused(
    tmp_path, monkeypatch
) -> None:
    """A refusal never crossed the boundary: could not run (2), not a finding."""
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    refused = _confirmation(
        LiveStatus.REFUSED, spent=False, detail="GitHub authentication failed"
    )
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live", return_value=refused),
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.UNVERIFIED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor", "route-live-confirm", "--yes"])

    assert result.exit_code == 2, result.output
    assert "refused without spending" in result.output


def test_route_live_confirm_does_not_prompt_when_evidence_answers(
    tmp_path, monkeypatch
) -> None:
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    reused = _confirmation(LiveStatus.CONFIRMED, from_evidence=True, probe_id="p-1")
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live", return_value=reused),
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.CONFIRMED, None),
        ),
    ):
        result = runner.invoke(app, ["doctor", "route-live-confirm"])

    assert result.exit_code == 0, result.output
    assert "not spending" in result.output


def test_doctor_flags_a_route_whose_last_live_probe_failed(tmp_path, monkeypatch):
    from kew.readiness_probe import LiveStatus

    monkeypatch.chdir(tmp_path)
    _write_uncontained_config(tmp_path)
    failed = SimpleNamespace(detail="expected exactly 'OK', got 'no'")
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.REFUTED, failed),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "route-live: FAIL (" in result.output
    assert "FAILED its last live probe" in result.output


# ---------------------------------------------------------------------------
# Anchor staleness alarm (#1408 / #1121)
# ---------------------------------------------------------------------------


def _write_anchor(tmp_path, *, days_old: float, timestamp: str | None = None) -> None:
    """Put one v2 anchor of the given age at the project root."""
    from kew.audit_chain import Anchor, format_anchor

    stamp = (
        timestamp
        if timestamp is not None
        else (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    )
    (tmp_path / "audit-anchors.jsonl").write_text(
        format_anchor(
            Anchor(
                id=7,
                entry_hash="a" * 64,
                count=7,
                timestamp=stamp,
                version=2,
                ledger_id="1453f4bd-e62a-457f-8759-ad236b05cc0e",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _run_doctor():
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        return runner.invoke(app, ["doctor"])


def test_doctor_warns_when_the_newest_anchor_is_stale(tmp_path, monkeypatch) -> None:
    """The anchor workflow can die silently; an unwitnessed chain still verifies
    clean. doctor is the alarm that the witness stopped (#1408)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _write_anchor(tmp_path, days_old=30)

    result = _run_doctor()

    assert result.exit_code == 1, result.output
    assert "anchor-age" in result.output
    assert "30.0 day(s) old" in result.output
    assert "(threshold 7)" in result.output  # the default, named in the warning


def test_doctor_passes_on_a_fresh_anchor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _write_anchor(tmp_path, days_old=1)

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age" in result.output


def test_doctor_honours_a_configured_anchor_age_threshold(
    tmp_path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[logging]\ndir = "{logs}"\n'
        "[audit]\nanchor_max_age_days = 60\n"
    )
    monkeypatch.chdir(tmp_path)
    _write_anchor(tmp_path, days_old=30)

    result = _run_doctor()

    assert result.exit_code == 0, result.output


def test_doctor_anchor_age_threshold_of_zero_disables_the_alarm(
    tmp_path, monkeypatch
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[logging]\ndir = "{logs}"\n'
        "[audit]\nanchor_max_age_days = 0\n"
    )
    monkeypatch.chdir(tmp_path)
    _write_anchor(tmp_path, days_old=400)

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age: skipped" in result.output


def test_doctor_skips_anchor_age_without_an_anchors_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age: skipped" in result.output


def test_doctor_never_reports_an_unreadable_anchor_stamp_as_fresh(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _write_anchor(tmp_path, days_old=0, timestamp="not-a-timestamp")

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age: skipped" in result.output


def test_doctor_skips_an_unparseable_anchors_file(tmp_path, monkeypatch) -> None:
    """A corrupt anchors file is a diagnostic gap, not a doctor crash."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "audit-anchors.jsonl").write_text("{not json\n", encoding="utf-8")

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age: skipped" in result.output


def test_doctor_never_passes_a_future_dated_anchor(tmp_path, monkeypatch) -> None:
    """Clock skew (or a forged stamp) is not evidence that anchoring is alive."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _write_anchor(tmp_path, days_old=-5)

    result = _run_doctor()

    assert result.exit_code == 0, result.output
    assert "anchor-age: skipped" in result.output
    assert "future" in result.output


def test_doctor_reports_the_local_host_provider(tmp_path, monkeypatch) -> None:
    """`local` is the default host and needs no cloud account, so it is named."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch("kew.cli.doctor.register_tools", return_value={}),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "host-local: PASS (available; no cloud account needed)" in result.output


def test_doctor_reports_an_expired_gcloud_login_with_its_fix(
    tmp_path, monkeypatch
) -> None:
    """AC4: the common gcp failure is a failed check carrying its fix command."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[logging]\ndir = "{logs}"\n'
        '[hosts]\nprovider = "gcp"\n'
        '[hosts.gcp]\nproject = "proj"\nzone = "us-central1-a"\n'
        'instance = "verify-1"\n'
    )
    with fake_gcloud_on_path(tmp_path, monkeypatch) as state:
        write_state(
            state,
            auth_exit=1,
            auth_stderr="ERROR: (gcloud.auth) Reauthentication required.\n",
        )
        with (
            patch("kew.cli.doctor.detect_runtime", return_value=None),
            patch("kew.cli.doctor.register_tools", return_value={}),
        ):
            result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "host-gcloud-auth: FAIL (gcloud login is expired or absent" in result.output
    assert "fix: gcloud auth login" in result.output
    assert "issue(s) found: host-gcloud-auth" in result.output


# --- OpenShell runtime checks (#2321) ---------------------------------------
#
# One pass and one fail per check. Each check is exercised as the verdict
# function `kew doctor` prints, plus two tests through the command itself: the
# all-pass report, and the unreachable gateway that must fail as ONE issue
# rather than as eight settings nobody read.


_SUPERVISOR_PIN = f"ghcr.io/nvidia/openshell-supervisor@sha256:{'a' * 64}"
_SANDBOX_PIN = f"ghcr.io/nvidia/openshell-sandbox@sha256:{'b' * 64}"


def _gateway_report(**overrides) -> openshell.GatewayReport:
    """A gateway reporting a correctly set-up control plane."""
    fields = {
        "gateway": openshell.Gateway("https://127.0.0.1:8443", "config"),
        "version": "0.1.2",
        "tls": True,
        "telemetry": False,
        "agent_proposals": False,
        "agent_approval": "manual",
        "supervisor_image": _SUPERVISOR_PIN,
        "sandbox_image": _SANDBOX_PIN,
        "landlock_abi": 3,
        "raw": {},
    }
    fields.update(overrides)
    return openshell.GatewayReport(**fields)  # type: ignore[arg-type]


def _openshell_sandbox_config(**overrides) -> SandboxConfig:
    settings = {"version": "0.1.2", "providers": {"claude-code": "kew-claude-code"}}
    settings.update(overrides)
    return SandboxConfig(runtime="openshell", openshell=settings)


class _FakeClient:
    """An OpenShell client that answers from canned facts, never a subprocess."""

    def __init__(self, *, report, cli_version="0.1.2", held=("kew-claude-code",)):
        self._report = report
        self._cli_version = cli_version
        self._held = set(held)
        self.asked: list[str] = []

    def version(self) -> str:
        return self._cli_version

    def report(self):
        return self._report

    def provider_get(self, name: str):
        self.asked.append(name)
        if name not in self._held:
            raise openshell.OpenShellNotFound(
                ["openshell", "provider", "get", name],
                reason="provider not found",
                exit_code=1,
            )
        return SimpleNamespace(name=name, profile="anthropic")


def test_openshell_version_check_passes_when_cli_and_gateway_match() -> None:
    check = openshell_doctor.check_versions(
        pinned="0.1.2", cli="0.1.2", gateway="0.1.2"
    )
    assert check.passed, check.detail
    assert "0.1.2" in check.detail


def test_openshell_version_check_fails_on_a_mismatched_gateway() -> None:
    """The dangerous half: every call succeeds, something else enforces."""
    check = openshell_doctor.check_versions(
        pinned="0.1.2", cli="0.1.2", gateway="0.1.3"
    )
    assert not check.passed
    assert "gateway is 0.1.3" in check.detail
    assert "CLI is" not in check.detail
    assert "[sandbox.openshell] version" in check.detail


def test_openshell_telemetry_check_passes_when_the_gateway_is_quiet() -> None:
    check = openshell_doctor.check_telemetry(_gateway_report(telemetry=False))
    assert check.passed, check.detail
    assert "no telemetry" in check.detail


def test_openshell_telemetry_check_fails_when_telemetry_is_on() -> None:
    check = openshell_doctor.check_telemetry(_gateway_report(telemetry=True))
    assert not check.passed
    assert "telemetry ON" in check.detail
    assert "restart the gateway" in check.detail


def test_openshell_image_pin_check_passes_on_two_digest_references() -> None:
    check = openshell_doctor.check_image_pins(_gateway_report())
    assert check.passed, check.detail
    assert "pinned by digest" in check.detail


@pytest.mark.parametrize(
    ("field", "named"),
    [("supervisor_image", "supervisor"), ("sandbox_image", "sandbox")],
)
def test_openshell_image_pin_check_fails_on_a_tag(field: str, named: str) -> None:
    check = openshell_doctor.check_image_pins(
        _gateway_report(**{field: "ghcr.io/nvidia/openshell-supervisor:latest"})
    )
    assert not check.passed
    assert named in check.detail
    assert "not pinned by digest" in check.detail


def test_openshell_agent_authority_check_passes_when_the_agent_has_none() -> None:
    check = openshell_doctor.check_agent_authority(_gateway_report())
    assert check.passed, check.detail
    assert "proposals are off" in check.detail


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"agent_proposals": True}, "policy proposals from the agent"),
        ({"agent_approval": "auto"}, "approval is 'auto'"),
    ],
)
def test_openshell_agent_authority_check_fails_on_delegated_authority(
    overrides: dict[str, object], expected: str
) -> None:
    check = openshell_doctor.check_agent_authority(_gateway_report(**overrides))
    assert not check.passed
    assert expected in check.detail
    assert "move its" in check.detail


def test_openshell_landlock_check_passes_on_abi_three() -> None:
    check = openshell_doctor.check_landlock(
        _gateway_report(landlock_abi=openshell_doctor.MINIMUM_LANDLOCK_ABI)
    )
    assert check.passed, check.detail
    assert "ABI 3" in check.detail


def test_openshell_landlock_check_fails_on_a_kernel_without_network_rules() -> None:
    check = openshell_doctor.check_landlock(_gateway_report(landlock_abi=2))
    assert not check.passed
    assert "ABI 2" in check.detail
    assert "allow every endpoint" in check.detail


def test_openshell_provider_check_passes_when_the_gateway_holds_each_one() -> None:
    client = _FakeClient(report=_gateway_report())
    check = openshell_doctor.check_providers(
        client, providers={"claude-code": "kew-claude-code"}
    )
    assert check.passed, check.detail
    assert client.asked == ["kew-claude-code"]


def test_openshell_provider_check_fails_on_a_provider_the_gateway_lacks() -> None:
    client = _FakeClient(report=_gateway_report(), held=())
    check = openshell_doctor.check_providers(
        client, providers={"claude-code": "kew-claude-code"}
    )
    assert not check.passed
    assert "claude-code -> kew-claude-code" in check.detail
    assert "[sandbox.openshell] providers" in check.detail


def test_openshell_provider_check_fails_when_no_runner_is_mapped() -> None:
    """An unmapped runtime proves nothing, so it is not a pass."""
    client = _FakeClient(report=_gateway_report())
    check = openshell_doctor.check_providers(client, providers={})
    assert not check.passed
    assert check.detail == openshell_doctor.NO_RUNNER_MAPPED
    assert client.asked == []


def test_openshell_provider_check_reraises_an_indeterminate_read() -> None:
    """A timeout says nothing about whether the provider exists."""

    class _Timeout(_FakeClient):
        def provider_get(self, name: str):
            raise openshell.OpenShellTimeout(
                ["openshell", "provider", "get", name], reason="deadline"
            )

    with pytest.raises(openshell.OpenShellTimeout):
        openshell_doctor.check_providers(
            _Timeout(report=_gateway_report()),
            providers={"claude-code": "kew-claude-code"},
        )


def test_openshell_tls_check_passes_on_a_tls_gateway_dialled_over_https() -> None:
    check = openshell_doctor.check_tls(_gateway_report())
    assert check.passed, check.detail
    assert "listens on TLS" in check.detail
    assert "https://127.0.0.1:8443" in check.detail


def test_openshell_tls_check_fails_when_the_gateway_reports_tls_off() -> None:
    check = openshell_doctor.check_tls(_gateway_report(tls=False))
    assert not check.passed
    assert "reports TLS off" in check.detail
    assert "tls.enabled" in check.detail


def test_openshell_tls_check_fails_on_a_plaintext_endpoint() -> None:
    """#2321 review: a TLS gateway kew dials over http:// is still in the clear.

    The runbook requires TLS and the only recorded gateway payload named a
    plaintext endpoint, so the half nothing covered was the one kew decides —
    which endpoint it dials.
    """
    check = openshell_doctor.check_tls(
        _gateway_report(gateway=openshell.Gateway("http://127.0.0.1:8080", "config"))
    )
    assert not check.passed
    assert "http://127.0.0.1:8080" in check.detail
    assert "[sandbox.openshell] gateway" in check.detail
    assert "reports TLS off" not in check.detail


def test_openshell_tls_check_fails_when_the_cli_default_decided_the_scheme() -> None:
    """An endpoint neither input supplied leaves a scheme kew cannot read."""
    check = openshell_doctor.check_tls(
        _gateway_report(gateway=openshell.Gateway("", "cli-default"))
    )
    assert not check.passed
    assert "no endpoint is configured" in check.detail


def test_no_openshell_check_compares_gateway_held_policies() -> None:
    """#2321 review: the gateway holds no runner-keyed policy to compare.

    kew hands each sandbox its policy file at `sandbox create` time, so a check
    that diffed packaged digests against a gateway policy map could not pass on
    a host set up exactly as docs/openshell.md prescribes, and its fix named an
    action in neither the CLI nor the runbook. Those bytes are covered by
    `openshell-certification`, which is bound to the packaged policy's digest.
    """
    assert not hasattr(openshell_doctor, "check_policy_hashes")
    assert not hasattr(openshell_doctor, "CHECK_POLICY_HASHES")
    assert "policies" not in openshell.GatewayReport.__dataclass_fields__
    source = Path(openshell_doctor.__file__).read_text(encoding="utf-8")
    assert "re-register" not in source, (
        "no check may name a remediation that exists in neither the CLI nor "
        "docs/openshell.md"
    )


def test_openshell_certification_check_passes_on_a_proven_boundary() -> None:
    config = _openshell_sandbox_config()
    with patch("kew.sandbox.require_certified_boundary") as gate:
        check = openshell_doctor.check_certification(
            config, runners=("claude-code",), store=object()
        )
    assert check.passed, check.detail
    assert gate.call_count == 1


def test_openshell_certification_check_fails_without_fresh_proof() -> None:
    from kew.sandbox import REFUSAL_OPENSHELL_UNCERTIFIED, SandboxRuntimeUnavailable

    refusal = SandboxRuntimeUnavailable(
        "Sandbox: no ledger is available",
        reason=REFUSAL_OPENSHELL_UNCERTIFIED,
        runtime="openshell",
    )
    with patch("kew.sandbox.require_certified_boundary", side_effect=refusal):
        check = openshell_doctor.check_certification(
            _openshell_sandbox_config(), runners=("claude-code",), store=None
        )
    assert not check.passed
    assert REFUSAL_OPENSHELL_UNCERTIFIED in check.detail
    assert "kew sandbox certify --runtime openshell" in check.detail


def test_openshell_certification_check_fails_when_no_runner_is_mapped() -> None:
    check = openshell_doctor.check_certification(
        _openshell_sandbox_config(providers={}), runners=(), store=None
    )
    assert not check.passed
    assert check.detail == openshell_doctor.NO_RUNNER_MAPPED


def test_openshell_checks_are_inert_on_another_runtime(capsys) -> None:
    """`[sandbox.openshell]` is only read when the runtime selects it."""
    config = DispatchConfig(sandbox=SandboxConfig(runtime="docker"))
    assert doctor_module._check_openshell(config) == []
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    # Every OpenShell id still reports, as a skip naming why (#2499 review).
    assert len(lines) == len(doctor_module._OPENSHELL_IDS), lines
    for check_id, line in zip(doctor_module._OPENSHELL_IDS, lines, strict=True):
        assert f"{check_id}: skipped ([sandbox] runtime is 'docker'" in line


def _openshell_dispatch_config(tmp_path) -> DispatchConfig:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return DispatchConfig(
        sandbox=_openshell_sandbox_config(),
        logging={"dir": str(logs)},
    )


def test_doctor_reports_every_openshell_check_on_a_ready_host(tmp_path, capsys) -> None:
    config = _openshell_dispatch_config(tmp_path)
    report = _gateway_report()
    with (
        patch.object(
            doctor_module.OpenShellClient,
            "from_settings",
            return_value=_FakeClient(report=report),
        ),
        patch("kew.sandbox.require_certified_boundary"),
    ):
        issues = doctor_module._check_openshell(config)
    output = capsys.readouterr().err
    assert issues == [], output
    assert "openshell-gateway: PASS (https://127.0.0.1:8443 answered)" in output
    for name in (
        openshell_doctor.CHECK_VERSION,
        openshell_doctor.CHECK_TLS,
        openshell_doctor.CHECK_TELEMETRY,
        openshell_doctor.CHECK_IMAGE_PINS,
        openshell_doctor.CHECK_PROVIDERS,
        openshell_doctor.CHECK_AGENT_AUTHORITY,
        openshell_doctor.CHECK_LANDLOCK,
        openshell_doctor.CHECK_CERTIFICATION,
    ):
        assert f"{name}:" in output, name
    assert "FAIL" not in output


def test_doctor_reports_an_unreachable_gateway_as_one_issue(tmp_path, capsys) -> None:
    """A read that never completed says nothing about eight settings."""
    config = _openshell_dispatch_config(tmp_path)

    class _Unreachable(_FakeClient):
        def report(self):
            raise openshell.OpenShellGatewayUnreachable(
                ["openshell", "status"],
                reason="connection refused",
                exit_code=1,
            )

    with patch.object(
        doctor_module.OpenShellClient,
        "from_settings",
        return_value=_Unreachable(report=_gateway_report()),
    ):
        issues = doctor_module._check_openshell(config)
    output = capsys.readouterr().err
    assert issues == ["openshell gateway unproven"]
    assert "openshell-gateway: FAIL (the gateway did not answer" in output
    assert "connection refused" in output
    assert "docs/openshell.md" in output
    # Blocked, not failed: the eight settings below the gateway are unproven.
    assert output.count("FAIL") == 1, output
    for check_id in doctor_module._OPENSHELL_DOWNSTREAM_IDS:
        assert (
            f"{check_id}: skipped (not probed: the openshell-gateway check failed)"
        ) in output


def test_doctor_lists_every_failed_openshell_check(tmp_path, capsys) -> None:
    config = _openshell_dispatch_config(tmp_path)
    report = _gateway_report(
        tls=False,
        telemetry=True,
        agent_approval="auto",
        landlock_abi=1,
        supervisor_image="ghcr.io/nvidia/openshell-supervisor:latest",
    )
    with (
        patch.object(
            doctor_module.OpenShellClient,
            "from_settings",
            return_value=_FakeClient(report=report, cli_version="0.1.1", held=()),
        ),
        patch("kew.sandbox.require_certified_boundary"),
    ):
        issues = doctor_module._check_openshell(config)
    assert issues == [
        openshell_doctor.CHECK_VERSION,
        openshell_doctor.CHECK_TLS,
        openshell_doctor.CHECK_TELEMETRY,
        openshell_doctor.CHECK_IMAGE_PINS,
        openshell_doctor.CHECK_PROVIDERS,
        openshell_doctor.CHECK_AGENT_AUTHORITY,
        openshell_doctor.CHECK_LANDLOCK,
    ], capsys.readouterr().err


def test_gateway_report_parses_the_status_json() -> None:
    """The one CLI read every gateway check comes from."""
    payload = {
        "gateway": "https://127.0.0.1:8443",
        "version": "0.1.2",
        "driver": "docker",
        "tls": {"enabled": True},
        "telemetry": {"enabled": False},
        "agent": {"proposals": False, "approval": "manual"},
        "images": {"supervisor": _SUPERVISOR_PIN, "sandbox": _SANDBOX_PIN},
        "kernel": {"landlock_abi": 3},
    }
    client = openshell.OpenShellClient(
        gateway=openshell.Gateway("https://127.0.0.1:8443", "config"),
        expected_version="0.1.2",
        environment={},
    )
    with patch.object(
        subprocess_utils,
        "run",
        return_value=_completed(["openshell"], stdout=json.dumps(payload)),
    ) as run:
        report = client.report()
    assert run.call_args.args[0][-2:] == ["-o", "json"]
    assert report.version == "0.1.2"
    assert report.tls is True
    assert report.telemetry is False
    assert report.agent_proposals is False
    assert report.agent_approval == "manual"
    assert report.supervisor_image == _SUPERVISOR_PIN
    assert report.landlock_abi == 3


def _status_payload(**overrides) -> dict[str, object]:
    """A gateway status payload carrying every fact :meth:`report` reads."""
    payload: dict[str, object] = {
        "version": "0.1.2",
        "tls": {"enabled": True},
        "telemetry": {"enabled": False},
        "agent": {"proposals": False, "approval": "manual"},
        "images": {"supervisor": _SUPERVISOR_PIN, "sandbox": _SANDBOX_PIN},
        "kernel": {"landlock_abi": 3},
    }
    payload.update(overrides)
    return payload


def _report_from(payload: Mapping[str, object]) -> openshell.GatewayReport:
    client = openshell.OpenShellClient(
        gateway=openshell.Gateway("", "cli-default"),
        expected_version="0.1.2",
        environment={},
    )
    with patch.object(
        subprocess_utils,
        "run",
        return_value=_completed(["openshell"], stdout=json.dumps(payload)),
    ):
        return client.report()


@pytest.mark.parametrize(
    "dropped", ["version", "tls", "telemetry", "agent", "images", "kernel"]
)
def test_gateway_report_refuses_a_payload_missing_a_containment_fact(
    dropped: str,
) -> None:
    """An absent field is a parse failure, never a default kew would report."""
    payload = _status_payload()
    del payload[dropped]
    with pytest.raises(openshell.OpenShellError):
        _report_from(payload)


def test_gateway_report_parses_a_gateway_holding_no_policies() -> None:
    """#2321 review: a correctly set-up gateway has no policy map of its own.

    kew hands each sandbox its policy at create time, so every host the runbook
    produces answers with no `policies` object — or an empty one. Refusing that
    at parse time collapsed all eight remaining checks into a single
    `openshell-gateway` failure and hid the containment faults behind it.
    """
    for payload in (_status_payload(), _status_payload(policies={})):
        report = _report_from(payload)
        assert report.landlock_abi == 3
        assert report.telemetry is False


def test_a_gateway_with_real_faults_still_reports_each_one(tmp_path, capsys) -> None:
    """#2321 review: the faults are named, not hidden behind a parse message."""
    config = _openshell_dispatch_config(tmp_path)
    report = _report_from(
        _status_payload(
            policies={},
            tls={"enabled": False},
            telemetry={"enabled": True},
            agent={"proposals": True, "approval": "auto"},
            images={"supervisor": "ghcr.io/x:latest", "sandbox": "ghcr.io/y:latest"},
            kernel={"landlock_abi": 1},
        )
    )
    with (
        patch.object(
            doctor_module.OpenShellClient,
            "from_settings",
            return_value=_FakeClient(report=report),
        ),
        patch("kew.sandbox.require_certified_boundary"),
    ):
        issues = doctor_module._check_openshell(config)
    output = capsys.readouterr().err
    assert issues == [
        openshell_doctor.CHECK_TLS,
        openshell_doctor.CHECK_TELEMETRY,
        openshell_doctor.CHECK_IMAGE_PINS,
        openshell_doctor.CHECK_AGENT_AUTHORITY,
        openshell_doctor.CHECK_LANDLOCK,
    ], output
    assert "openshell-gateway: FAIL" not in output


def test_doctor_separates_a_silent_gateway_from_an_unreadable_report(
    tmp_path, capsys
) -> None:
    """#2321 review: both collapse to one issue, but they are not one problem."""
    config = _openshell_dispatch_config(tmp_path)

    class _Garbage(_FakeClient):
        def report(self):
            raise openshell.OpenShellError(
                ["openshell", "status"],
                reason="openshell JSON has no kernel.landlock_abi field",
                exit_code=0,
            )

    with patch.object(
        doctor_module.OpenShellClient,
        "from_settings",
        return_value=_Garbage(report=_gateway_report()),
    ):
        issues = doctor_module._check_openshell(config)
    output = capsys.readouterr().err
    assert issues == ["openshell gateway unproven"]
    assert "the gateway answered, but kew cannot read its report" in output
    assert "kernel.landlock_abi" in output
    assert "did not answer" not in output


def test_the_recorded_gateway_fixture_is_the_posture_the_runbook_prescribes() -> None:
    """#2321 review: the recorded payload named a plaintext endpoint.

    `tests/fixtures/openshell/status.json` is the one recorded gateway payload
    in the tree, so it is what a reader takes for a correctly set-up gateway.
    It has to pass every check the runbook's step 2 writes out.
    """
    fixture = Path(__file__).parent / "fixtures" / "openshell" / "status.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["gateway"].startswith(openshell_doctor.TLS_SCHEME)
    client = openshell.OpenShellClient(
        gateway=openshell.Gateway(payload["gateway"], "config"),
        expected_version="0.1.2",
        environment={},
    )
    with patch.object(
        subprocess_utils,
        "run",
        return_value=_completed(["openshell"], stdout=json.dumps(payload)),
    ):
        report = client.report()
    for check in (
        openshell_doctor.check_tls(report),
        openshell_doctor.check_telemetry(report),
        openshell_doctor.check_image_pins(report),
        openshell_doctor.check_agent_authority(report),
        openshell_doctor.check_landlock(report),
    ):
        assert check.passed, f"{check.name}: {check.detail}"


def test_doctor_runs_the_openshell_checks_from_the_command(tmp_path, monkeypatch):
    """The block is wired into bare `kew doctor`, not only callable."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[agent]\nrunner = "claude-code"\n'
        '[sandbox]\nruntime = "openshell"\n'
        '[sandbox.images]\nclaude-code = "kew-agent:latest"\n'
        '[sandbox.openshell]\ngateway = "https://127.0.0.1:8443"\n'
        '[sandbox.openshell.providers]\nclaude-code = "kew-claude-code"\n'
        f'[logging]\ndir = "{logs}"\n'
    )

    class _Unreachable(_FakeClient):
        def version(self):
            raise openshell.OpenShellGatewayUnreachable(
                ["openshell", "--version"], reason="connection refused", exit_code=1
            )

    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.detect_runtime", return_value=None),
        patch.object(
            doctor_module.OpenShellClient,
            "from_settings",
            return_value=_Unreachable(report=_gateway_report()),
        ),
    ):
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.output
    assert "openshell-gateway: FAIL (the gateway did not answer" in result.output
    assert "openshell-gateway" in result.output.splitlines()[-1]


# --- kew doctor --json (#2499) ------------------------------------------------

_PINNED_CHECK_IDS = (
    "project-config",
    "required-tools",
    "runner-credential",
    "gh-auth",
    "containment",
    "container-runtime",
    "route-image",
    "allowlist-network",
    "openshell-gateway",
    "openshell-version",
    "openshell-tls",
    "openshell-telemetry",
    "openshell-image-pins",
    "openshell-providers",
    "openshell-agent-authority",
    "openshell-landlock",
    "openshell-certification",
    "model-eol",
    "anchor-age",
    "temporary-storage",
    "editable-install",
    "runner-liveness",
    "host-local",
    "host-gcloud",
    "host-gcloud-auth",
    "host-gcp-instance",
    "agent-image",
    "retained-agent-tags",
    "degraded-review",
    "reservations",
    "shadow-db",
    "route-live",
    "runner-quota",
)
_RESULT_FIELDS = {"id", "status", "summary", "hint", "fix", "details", "warnings"}
_HUMAN_SUMMARY_PREFIX = "kew doctor: "


def _missing_allowlist_network(cmd, **_kwargs):
    if cmd[1:] == ["network", "inspect", "kew-allowlist"]:
        return _completed(cmd, 1, stderr="network not found")
    return _completed(cmd)


def _invoke_doctor_without_network(tmp_path, monkeypatch, *args: str):
    monkeypatch.chdir(tmp_path)
    if not (tmp_path / "kew.toml").exists():
        _write_contained_config(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        return runner.invoke(app, ["doctor", *args])


def _json_issue_ids(stdout: str) -> list[str]:
    report = json.loads(stdout)
    return [c["id"] for c in report["checks"] if c["status"] in ("warn", "fail")]


def _human_issue_ids(output: str) -> list[str]:
    """The ids the prose summary line names, read back off the rendered text."""
    summary = [line for line in output.splitlines() if _HUMAN_SUMMARY_PREFIX in line]
    assert len(summary) == 2, output  # the opening line and the verdict line
    verdict = summary[-1].split(_HUMAN_SUMMARY_PREFIX, 1)[1]
    if verdict == "all checks passed":
        return []
    return verdict.split("issue(s) found: ", 1)[1].split(", ")


def test_doctor_json_prints_one_entry_per_check_with_every_field(
    tmp_path, monkeypatch
) -> None:
    result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["schema"] == 1
    assert report["ok"] is False
    checks = report["checks"]
    ids = [check["id"] for check in checks]
    # Every id exactly once, in order — not a subset (#2499 review).
    assert tuple(ids) == _PINNED_CHECK_IDS
    for check in checks:
        assert set(check) == _RESULT_FIELDS, check
        assert check["status"] in {"ok", "warn", "fail", "skip"}, check
        assert isinstance(check["summary"], str) and check["summary"], check
        assert check["hint"] is None or isinstance(check["hint"], str), check
        assert check["fix"] is None or isinstance(check["fix"], str), check
    # Every check that ran on this route reports, not only the failing one.
    for expected in (
        "required-tools",
        "container-runtime",
        "route-image",
        "allowlist-network",
        "agent-image",
        "host-local",
        "shadow-db",
        "route-live",
    ):
        assert expected in ids, (expected, ids)


def test_doctor_json_keeps_prose_off_stdout(tmp_path, monkeypatch) -> None:
    result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    assert "[dispatch]" not in result.stdout
    assert "issue(s) found" not in result.stdout
    json.loads(result.stdout)


def test_doctor_json_check_ids_are_unique_and_pinned() -> None:
    """Renaming an id is a deliberate change: the console and agents key on it."""
    assert doctor_module.CHECK_IDS == _PINNED_CHECK_IDS
    assert len(set(_PINNED_CHECK_IDS)) == len(_PINNED_CHECK_IDS)
    for check_id in _PINNED_CHECK_IDS:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", check_id), check_id
    openshell_ids = {
        value
        for name, value in vars(openshell_doctor).items()
        if name.startswith("CHECK_") and isinstance(value, str)
    }
    assert openshell_ids <= set(_PINNED_CHECK_IDS)


def test_doctor_json_host_ids_are_all_pinned(tmp_path, monkeypatch) -> None:
    """Every host probe name, the gcp ones included, maps to a pinned id."""
    from kew.hosts.diagnostics import check_hosts

    config = DispatchConfig()
    config.hosts.gcp.project = "p"
    config.hosts.gcp.zone = "us-central1-a"
    config.hosts.gcp.instance = "dev-box"
    monkeypatch.setenv("PATH", str(tmp_path))  # no gcloud: every probe reports

    names = {f"host-{check.name}" for check in check_hosts(config)}
    assert names == {
        "host-local",
        "host-gcloud",
        "host-gcloud-auth",
        "host-gcp-instance",
    }
    assert names <= set(_PINNED_CHECK_IDS)


def test_doctor_json_missing_allowlist_network_fails_with_a_fix(
    tmp_path, monkeypatch
) -> None:
    result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    network = checks["allowlist-network"]
    assert network["status"] == "fail"
    assert network["fix"] == "kew sandbox network"
    assert "no firewall rules" in network["hint"]


def test_doctor_json_and_human_render_the_same_failing_ids_from_one_fixture(
    tmp_path, monkeypatch
) -> None:
    """Both outputs render from one result list, so they cannot disagree."""
    from kew.readiness import CheckResult, CheckStatus

    fixture = [
        CheckResult("required-tools", CheckStatus.OK, "every tool resolved"),
        CheckResult(
            "allowlist-network",
            CheckStatus.FAIL,
            "'kew-allowlist' is not present",
            hint="kew installs no firewall rules on it",
            fix="kew sandbox network",
        ),
        CheckResult("agent-image", CheckStatus.SKIP, "route image unresolved"),
        CheckResult("anchor-age", CheckStatus.WARN, "newest anchor is 9.0 day(s) old"),
    ]
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    monkeypatch.setattr(doctor_module, "run_checks", lambda _config: list(fixture))

    human = runner.invoke(app, ["doctor"])
    machine = runner.invoke(app, ["doctor", "--json"])

    assert human.exit_code == machine.exit_code == 1
    assert _human_issue_ids(human.output) == _json_issue_ids(machine.stdout)
    assert _json_issue_ids(machine.stdout) == ["allowlist-network", "anchor-age"]
    assert "allowlist-network: FAIL ('kew-allowlist' is not present)" in human.output
    assert "fix: kew sandbox network" in human.output
    assert "agent-image: skipped (route image unresolved)" in human.output
    assert "anchor-age: WARN (newest anchor is 9.0 day(s) old)" in human.output


def test_doctor_json_and_human_agree_when_every_check_passes(
    tmp_path, monkeypatch
) -> None:
    from kew.readiness import CheckResult, CheckStatus

    fixture = [
        CheckResult("required-tools", CheckStatus.OK, "every tool resolved"),
        CheckResult("route-live", CheckStatus.SKIP, "live-unverified"),
    ]
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    monkeypatch.setattr(doctor_module, "run_checks", lambda _config: list(fixture))

    human = runner.invoke(app, ["doctor"])
    machine = runner.invoke(app, ["doctor", "--json"])

    assert human.exit_code == machine.exit_code == 0
    assert json.loads(machine.stdout)["ok"] is True
    assert _human_issue_ids(human.output) == _json_issue_ids(machine.stdout) == []


def test_doctor_json_and_human_agree_on_a_real_run(tmp_path, monkeypatch) -> None:
    """Unstubbed checks: the prose and JSON name the same issues and exit alike."""
    human = _invoke_doctor_without_network(tmp_path, monkeypatch)
    machine = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    assert human.exit_code == machine.exit_code == 1
    assert "allowlist-network" in _json_issue_ids(machine.stdout)
    assert _human_issue_ids(human.output) == _json_issue_ids(machine.stdout)


def test_run_checks_returns_results_without_printing(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    from kew.config import load_config

    config = load_config()
    capsys.readouterr()
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        results = doctor_module.run_checks(config)

    captured = capsys.readouterr()
    assert "allowlist-network" not in captured.err + captured.out
    by_id = {result.id: result for result in results}
    assert by_id["allowlist-network"].status == "fail"
    assert by_id["container-runtime"].status == "ok"


def test_doctor_json_docs_document_the_flag_and_every_check_id() -> None:
    page = _doctor_page()
    assert "kew doctor --json" in page
    missing = [
        check_id for check_id in _PINNED_CHECK_IDS if f"`{check_id}`" not in page
    ]
    assert missing == []


# --- #2499 adversarial review -------------------------------------------------


def test_doctor_json_survives_a_check_that_raises(tmp_path, monkeypatch) -> None:
    """Finding 1: one broken check is a structured failure, not an empty stdout."""
    with patch.object(
        doctor_module, "_check_model_lifecycle", side_effect=RuntimeError("boom")
    ):
        result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    checks = {check["id"]: check for check in report["checks"]}
    assert tuple(checks) == _PINNED_CHECK_IDS
    assert checks["model-eol"]["status"] == "fail"
    assert "RuntimeError: boom" in checks["model-eol"]["summary"]
    # The checks after it still ran.
    assert checks["anchor-age"]["summary"] != checks["model-eol"]["summary"]
    assert checks["shadow-db"]["status"] == "ok"


def test_a_check_that_raises_part_way_fails_only_what_it_left_unreported(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    from kew.config import load_config

    config = load_config()
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch.object(
            doctor_module,
            "_check_allowlist_network",
            side_effect=OSError("inspect exploded"),
        ),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        by_id = {result.id: result for result in doctor_module.run_checks(config)}

    assert by_id["container-runtime"].status == "ok"
    assert by_id["allowlist-network"].status == "fail"
    assert "OSError: inspect exploded" in by_id["allowlist-network"].summary
    # Containment did not complete, so the checks that need its answer skip.
    assert by_id["agent-image"].status == "skip"
    assert "containment checks did not complete" in by_id["agent-image"].summary


def test_doctor_json_redacts_a_credential_a_check_echoes(tmp_path, monkeypatch) -> None:
    """Finding 2: the machine rendering is redacted like the prose one."""
    secret = "ghp_EXAMPLEtokenVALUE1234567890abcdef"
    monkeypatch.setenv("GH_TOKEN", secret)
    echoed = RunnerLivenessResult(
        configured=True, error=f"gh api failed: bad credentials {secret}"
    )
    with patch.object(doctor_module, "check_runner_liveness", return_value=echoed):
        result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    assert secret not in result.stdout
    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    assert "bad credentials" in checks["runner-liveness"]["summary"]


def test_check_result_redacts_every_text_field(monkeypatch) -> None:
    from kew.readiness import CheckResult, CheckStatus

    secret = "ghp_EXAMPLEtokenVALUE1234567890abcdef"
    monkeypatch.setenv("GH_TOKEN", secret)
    result = CheckResult(
        "host-gcloud-auth",
        CheckStatus.FAIL,
        f"summary {secret}",
        hint=f"hint {secret}",
        fix=f"fix {secret}",
        details=(f"detail {secret}",),
        warnings=(f"warning {secret}",),
    )
    assert secret not in json.dumps(result.to_dict())


def test_doctor_json_reports_every_openshell_id_as_skipped_on_docker(
    tmp_path, monkeypatch
) -> None:
    """Finding 3: an inapplicable check is a skip with a reason, never absent."""
    result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    for check_id in doctor_module._OPENSHELL_IDS:
        assert checks[check_id]["status"] == "skip", checks[check_id]
        assert "not 'openshell'" in checks[check_id]["summary"]
    for check_id in ("host-gcloud", "host-gcloud-auth", "host-gcp-instance"):
        assert checks[check_id]["status"] == "skip", checks[check_id]


def test_run_checks_reports_a_missing_tool_without_printing(
    tmp_path, monkeypatch, capsys
) -> None:
    """Finding 4: no CLI preflight output from the library entry point."""
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    from kew.config import load_config

    config = load_config()
    capsys.readouterr()
    with (
        patch.object(
            subprocess_utils, "_REQUIRED_TOOLS", {"kew-no-such-tool": "https://x.test"}
        ),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        results = doctor_module.run_checks(config)

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    tools = {result.id: result for result in results}["required-tools"]
    assert tools.status == "fail"
    assert "kew-no-such-tool" in tools.summary
    assert tools.details == ("kew-no-such-tool: install from https://x.test",)


def test_run_checks_skips_every_check_left_when_the_deadline_has_passed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    from kew.config import load_config

    config = load_config()
    monkeypatch.setattr(doctor_module, "_RUN_CHECKS_DEADLINE_SECONDS", -1.0)
    results = doctor_module.run_checks(config)

    assert tuple(result.id for result in results) == _PINNED_CHECK_IDS
    assert {result.status for result in results} == {"skip"}
    assert {result.summary for result in results} == {doctor_module._DEADLINE_REASON}


def test_run_checks_bounds_every_subprocess_by_one_deadline(
    tmp_path, monkeypatch
) -> None:
    """A probe that names no timeout still gets one under run_checks."""
    monkeypatch.chdir(tmp_path)
    _write_contained_config(tmp_path)
    from kew.config import load_config

    config = load_config()
    seen: list[float | None] = []

    def record(*_args, timeout=None, **_kwargs):
        seen.append(timeout)
        return subprocess.CompletedProcess(_args, 0, "", "")

    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.subprocess_utils.resolve_cmd", side_effect=lambda cmd: cmd),
        patch("kew.subprocess_utils.subprocess.run", side_effect=record),
    ):
        doctor_module.run_checks(config)

    assert seen, "no subprocess was reached"
    assert None not in seen
    assert max(seen) <= doctor_module._RUN_CHECKS_DEADLINE_SECONDS


def test_route_live_check_does_not_create_the_readiness_key(
    tmp_path, monkeypatch
) -> None:
    """Finding 4: a passive check reads the readiness key, never writes it."""
    key_path = tmp_path / "config" / "kew" / "readiness-hmac.key"
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(key_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    logs = tmp_path / "logs"
    logs.mkdir()
    SqliteRunStore(str(logs / "dispatch.db")).close()
    config = DispatchConfig(
        logging={"dir": str(logs)},
        sandbox=SandboxConfig(proxy_environment="inherit"),
    )

    doctor_module._check_route_live(logs / "dispatch.db", config)

    assert not key_path.exists()
    assert not key_path.parent.exists()


def test_advisory_warnings_render_through_the_warning_emitter() -> None:
    """Finding 6: a passing check's warning is not printed as a success."""
    from kew.readiness import CheckResult, CheckStatus

    success: list[str] = []
    warned: list[str] = []
    result = CheckResult(
        "allowlist-network",
        CheckStatus.OK,
        "'kew-allowlist' is present",
        warnings=("kew-allowlist is a plain bridge",),
    )
    with (
        patch.object(doctor_module, "_success", side_effect=success.append),
        patch.object(doctor_module, "_warn", side_effect=warned.append),
    ):
        doctor_module._render_check(result)

    assert success == ["  allowlist-network: PASS ('kew-allowlist' is present)"]
    assert warned == ["  allowlist-network: WARN (kew-allowlist is a plain bridge)"]


def test_doctor_names_the_database_just_before_the_store_checks(
    tmp_path, monkeypatch
) -> None:
    result = _invoke_doctor_without_network(tmp_path, monkeypatch)

    lines = result.output.splitlines()
    db_line = next(i for i, line in enumerate(lines) if "dispatch.db: " in line)
    assert "retained-agent-tags:" in "".join(lines[:db_line])
    assert "degraded-review:" in lines[db_line + 1]


# --- runner-credential and gh-auth (#2500) -------------------------------------

#: Credential-shaped fixtures, built at run time so no whole credential-shaped
#: literal is ever committed.
_FIXTURE_OAUTH_TOKEN = "sk-ant-oat01-" + "Q" * 40
_FIXTURE_GH_TOKEN = "gh" + "p_" + "Z" * 36


def _run_checks_by_id(tmp_path, monkeypatch, toml: str | None = None):
    monkeypatch.chdir(tmp_path)
    if toml is None:
        _write_contained_config(tmp_path)
    else:
        (tmp_path / "logs").mkdir()
        (tmp_path / "kew.toml").write_text(
            f'{toml}\n[logging]\ndir = "{tmp_path / "logs"}"\n'
        )
    from kew.config import load_config

    config = load_config()
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        return {result.id: result for result in doctor_module.run_checks(config)}


def test_runner_credential_fails_naming_both_claude_variables(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    check = _run_checks_by_id(tmp_path, monkeypatch)["runner-credential"]

    assert check.status == "fail"
    assert "claude-code" in check.summary
    assert check.hint is not None
    assert "ANTHROPIC_API_KEY" in check.hint
    assert "CLAUDE_CODE_OAUTH_TOKEN" in check.hint
    assert "claude setup-token" in check.hint
    assert check.fix == "export ANTHROPIC_API_KEY=<key>"


def test_runner_credential_passes_with_a_credential_and_names_only_the_variable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FIXTURE_OAUTH_TOKEN)

    check = _run_checks_by_id(tmp_path, monkeypatch)["runner-credential"]

    assert check.status == "ok"
    assert "claude-code (CLAUDE_CODE_OAUTH_TOKEN)" in check.summary
    assert _FIXTURE_OAUTH_TOKEN not in json.dumps(check.to_dict())


def test_runner_credential_skips_codex_for_its_native_posture(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    from kew.config import load_config

    by_id = {r.id: r for r in doctor_module.run_checks(load_config())}
    check = by_id["runner-credential"]

    assert check.status == "skip"
    assert "codex" in check.summary
    assert "native posture" in check.summary
    assert check.hint is None


def test_runner_credential_checks_every_configured_route(tmp_path, monkeypatch) -> None:
    """A codex default route does not hide a claude-code complex route."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    toml = (
        '[project]\nrepo = "o/r"\n'
        '[routing]\ndefault = { runner = "codex" }\n'
        'complex = { runner = "claude-code" }\n'
        '[sandbox.images]\nclaude-code = "kew-agent:latest"'
    )

    check = _run_checks_by_id(tmp_path, monkeypatch, toml)["runner-credential"]

    assert check.status == "fail"
    assert "claude-code" in check.summary
    assert "codex" not in check.summary
    assert any("codex: native posture" in line for line in check.details)


def test_runner_credential_warns_rather_than_fails_under_skip_containment(
    tmp_path, monkeypatch
) -> None:
    """The skip flag turns a missing credential into a host run, not a refusal."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    toml = (
        '[project]\nrepo = "o/r"\n'
        '[routing]\ndefault = { runner = "claude-code" }\n'
        "[sandbox]\ndangerously_skip_containment = true\n"
        '[sandbox.images]\nclaude-code = "kew-agent:latest"'
    )

    check = _run_checks_by_id(tmp_path, monkeypatch, toml)["runner-credential"]

    assert check.status == "warn"
    assert "claude-code" in check.summary
    assert "uncontained" in check.summary
    assert "refused" not in check.summary
    assert check.hint is not None and "CLAUDE_CODE_OAUTH_TOKEN" in check.hint


@pytest.mark.parametrize(
    ("sandbox", "reason"),
    [
        ({"runtime": "openshell"}, "OpenShell gateway's providers"),
        ({"enabled": False}, "containment is disabled"),
    ],
)
def test_runner_credential_exempts_routes_not_credentialed_from_the_environment(
    sandbox: dict[str, object], reason: str
) -> None:
    exemption = doctor_module._runner_credential_exemption(
        SandboxConfig(**sandbox), "claude-code"
    )

    assert exemption is not None and reason in exemption


@pytest.mark.parametrize("json_flag", [(), ("--json",)])
def test_doctor_output_never_carries_a_credential_value(
    tmp_path, monkeypatch, json_flag: tuple[str, ...]
) -> None:
    """Neither rendering carries a value, even where a probe echoes one."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FIXTURE_OAUTH_TOKEN)
    monkeypatch.setenv("GH_TOKEN", _FIXTURE_GH_TOKEN)
    echoed = _completed(
        ["gh", "auth", "status"],
        1,
        stderr=f"unexpected answer for token {_FIXTURE_GH_TOKEN}",
    )
    monkeypatch.setattr(doctor_module, "_gh_auth_status", lambda: echoed)

    result = _invoke_doctor_without_network(tmp_path, monkeypatch, *json_flag)

    assert "runner-credential" in result.output
    assert "gh-auth" in result.output
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output
    assert _FIXTURE_OAUTH_TOKEN not in result.output
    assert _FIXTURE_GH_TOKEN not in result.output


def _gh_auth_check(monkeypatch, status) -> object:
    monkeypatch.setattr(doctor_module, "_gh_auth_status", status)
    recorder = doctor_module._Recorder()
    token = doctor_module._RECORDER.set(recorder)
    try:
        doctor_module._check_gh_auth()
    finally:
        doctor_module._RECORDER.reset(token)
    return recorder.results["gh-auth"]


def _gh_answer(returncode: int, stderr: str = "", stdout: str = ""):
    return lambda: _completed(
        ["gh", "auth", "status"], returncode, stdout=stdout, stderr=stderr
    )


def test_gh_auth_passes_when_logged_in(monkeypatch) -> None:
    check = _gh_auth_check(monkeypatch, _gh_answer(0, stdout="Logged in to github.com"))

    assert check.status == "ok"
    assert check.fix is None


@pytest.mark.parametrize(
    "stderr",
    [
        "You are not logged into any GitHub hosts. To log in, run: gh auth login",
        "github.com\n  X Failed to log in to github.com account octo (keyring)\n"
        "  - The token in keyring is invalid.",
    ],
)
def test_gh_auth_logged_out_fails_with_gh_auth_login(monkeypatch, stderr) -> None:
    check = _gh_auth_check(monkeypatch, _gh_answer(1, stderr))

    assert check.status == "fail"
    assert check.fix == "gh auth login"
    assert check.summary == "gh is not logged in to GitHub"


@pytest.mark.parametrize(
    "stderr",
    [
        "error connecting to api.github.com\ncheck your internet connection",
        "X Timeout trying to log in to github.com account octo (keyring)",
        "dial tcp: lookup api.github.com: no such host",
    ],
)
def test_gh_auth_unreachable_host_has_a_different_summary(monkeypatch, stderr) -> None:
    check = _gh_auth_check(monkeypatch, _gh_answer(1, stderr))

    assert check.status == "fail"
    assert check.summary != "gh is not logged in to GitHub"
    assert "unreachable" in check.summary
    assert check.fix is None


def test_gh_auth_timeout_reads_as_unreachable(monkeypatch) -> None:
    def hang():
        raise subprocess.TimeoutExpired(["gh", "auth", "status"], 15)

    check = _gh_auth_check(monkeypatch, hang)

    assert check.status == "fail"
    assert "unreachable" in check.summary
    assert check.fix is None


def test_gh_auth_skips_when_gh_cannot_run(monkeypatch) -> None:
    def absent():
        raise ValueError("Unknown executable 'gh'")

    check = _gh_auth_check(monkeypatch, absent)

    assert check.status == "skip"
    assert "required-tools" in check.summary


def test_gh_auth_asks_gh_auth_status_with_a_bound(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _completed(cmd, 1, stderr="You are not logged into any GitHub hosts.")

    monkeypatch.setattr(subprocess_utils, "run", fake_run)

    check = _gh_auth_check(monkeypatch, _REAL_GH_AUTH_STATUS)

    assert calls == [(["gh", "auth", "status"], {"check": False, "timeout": 15})]
    assert check.fix == "gh auth login"


# --- project-config (#2652) ----------------------------------------------------


def _project_config_result(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    recorder = doctor_module._Recorder()
    token = doctor_module._RECORDER.set(recorder)
    try:
        doctor_module._check_project_config()
    finally:
        doctor_module._RECORDER.reset(token)
    return recorder.results["project-config"]


def test_project_config_fails_with_kew_init_when_no_config_is_in_force(
    tmp_path, monkeypatch
) -> None:
    result = _project_config_result(tmp_path, monkeypatch)

    assert result.status is doctor_module.CheckStatus.FAIL
    assert result.fix == "kew init"
    assert result.hint


def test_project_config_names_the_kew_toml_in_force(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)

    result = _project_config_result(tmp_path, monkeypatch)

    assert result.status is doctor_module.CheckStatus.OK
    assert str(tmp_path / "kew.toml") in result.summary
    assert result.fix is None


def test_doctor_json_reports_project_config_from_the_selected_project(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    result = _invoke_doctor_without_network(tmp_path, monkeypatch, "--json")

    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["project-config"]["status"] == "ok"
    assert str(tmp_path / "kew.toml") in checks["project-config"]["summary"]


def test_doctor_json_fails_project_config_without_a_kew_toml(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.doctor.detect_runtime", return_value="/usr/bin/docker"),
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.subprocess_utils.run", side_effect=_missing_allowlist_network),
        patch(
            "kew.cli.doctor.evaluate_agent_image_staleness",
            return_value=ImageStalenessResult("unknown", detail="not relevant"),
        ),
    ):
        result = runner.invoke(app, ["doctor", "--json"])

    assert not (tmp_path / "kew.toml").exists()
    assert result.exit_code == 1, result.output
    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["project-config"]["status"] == "fail"
    assert checks["project-config"]["fix"] == "kew init"
