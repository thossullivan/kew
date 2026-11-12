"""Tests for sandbox.py runtime detection and argv builder (Node S2) and
ContainerHandle launch/stream/kill (Node S3)."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from kew.config import SandboxConfig
from kew.sandbox import (
    AGENT_MARKER_ENV,
    ALLOWLIST_NETWORK,
    CHECKOUT_QUOTA_BYTES,
    DEFAULT_IMAGE_RUNNER,
    MAX_LINE_BYTES,
    OOM_CAP_REASON_TEMPLATE,
    OOM_PROBE_TIMEOUT_SECONDS,
    POSTURE_UNCONTAINABLE_FLOW,
    PROMPT_FILENAME,
    PROMPT_MOUNT_PATH,
    RESOURCE_CAP_EVENT,
    RUNTIME_CONTROL_TIMEOUT_SECONDS,
    STARTUP_NONCE_ENV,
    STOCK_AGENT_IMAGE,
    TEST_WORKER_ENV_NAMES,
    TRANSCRIPT_BYTE_CAP,
    WALL_CLOCK_LIMIT_SECONDS,
    WORK_MOUNT_PATH,
    ContainedExecution,
    ContainedImageUnresolvedError,
    ContainerHandle,
    GlobalFirewallSnapshot,
    GlobalFirewallSource,
    ImageIdStatus,
    ImageResolution,
    KillStatus,
    OOMProbeResult,
    OOMProbeStatus,
    ResourceCapCode,
    ResourceCapObservation,
    SandboxCredentialUndeclared,
    SandboxImageUnmapped,
    SandboxRuntimeUnavailable,
    WorkerNetnsFilterError,
    attest_flow_posture,
    build_container_cleanup_argv,
    build_extract_argv,
    build_global_firewall_observer_argv,
    build_network_attachment_argv,
    build_network_cleanup_argv,
    build_per_run_network_argv,
    build_resource_inspect_argv,
    build_run_argv,
    build_worker_netns_filter_helper_argv,
    build_worker_netns_filter_install_argv,
    build_worker_netns_filter_readback_argv,
    capture_global_firewall_snapshot,
    check_agent_image_staleness,
    check_runtime_memory_capacity,
    checkout_usage_bytes,
    detect_runtime,
    detect_runtime_cached,
    emit_resolved_posture,
    evaluate_agent_image_staleness,
    inspect_image_id,
    launch_container,
    missing_image_message,
    oom_cap_reason,
    openshell_ignored_argv_settings_warning,
    require_image_id,
    reset_runtime_cache,
    resolve_effective_sandbox,
    resolve_flow_containment,
    resolve_runner_image,
    resource_caps,
    resource_limit_args,
    run_container_with_cpu_cap_retry,
    run_smoke,
    runner_credential_env_vars,
    validate_worker_netns_filter_readback,
    worker_netns_filter_ruleset,
)
from kew.sandbox_caps import (
    HARD_MAX_CPUS,
    HARD_MAX_MEMORY,
    ResourceCaps,
    format_memory_size,
    parse_memory_size,
)
from kew.tracking import MAX_RESOURCE_CAP_OBSERVATIONS, DispatchResult
from kew.transcript_metrics import read_derived_evidence, write_container_evidence

# The claude-code container env allowlist, read from the runner's OWN
# declaration (#1146) rather than a second copy of the list — these tests
# assert against exactly what a contained claude dispatch forwards.
CREDENTIAL_ENV_VARS = runner_credential_env_vars(DEFAULT_IMAGE_RUNNER)

# The image the runner-derived sandbox decision resolves for the default
# claude-code route (#1144).
SANDBOX_IMAGE = SandboxConfig().image
SANDBOX_RUNNER = DEFAULT_IMAGE_RUNNER

#: Test-pool sizes are -e assignments too (#2887), and never a credential.
_WORKER_POOL_PREFIXES = tuple(f"{name}=" for name in TEST_WORKER_ENV_NAMES)


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """Sandboxed-dispatch tests run with detect_runtime_cached forced to None
    (conftest), which the fail-closed image gate (#1550 F1) would refuse.
    Resolve to a stable fake ID by default; yields the ORIGINAL resolver so
    the gate tests can exercise the real refusal."""
    import kew.dispatch_sandbox as _ds

    original = _ds._resolve_launch_image_id
    monkeypatch.setattr(
        _ds, "_resolve_launch_image_id", lambda config, image, **_: "sha256:" + "f" * 64
    )
    yield original


@pytest.fixture()
def _checkout(tmp_path: Path) -> Path:
    return tmp_path / "checkout"


def _with_credential() -> object:
    """Context: env has an agent credential (posture tests that expect sandboxed)."""
    return patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})


def _without_credential() -> object:
    """Context: env has NO agent credential (the #721 CLI-authed-machine shape)."""
    return patch.dict(
        os.environ, {name: "" for name in CREDENTIAL_ENV_VARS}, clear=False
    )


def _valid_cidfile(tmp_path: Path, label: str = "container") -> Path:
    path = tmp_path / f"{label}.cid"
    path.write_text("a" * 64, encoding="utf-8")
    return path


class TestBuildRunArgv:
    @pytest.mark.parametrize("runtime", ["docker", "podman"])
    @pytest.mark.parametrize("operation", ["run", "create"])
    @pytest.mark.parametrize("credential_environment", [None, {}])
    def test_build_run_argv_blocks_runtime_proxy_injection(
        self, _checkout: Path, monkeypatch, runtime, operation, credential_environment
    ):
        names = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
        secret = "http://user:s3cret@proxy.invalid:3128"
        for name in names:
            monkeypatch.setenv(name, secret)
        argv = build_run_argv(
            runtime=runtime,
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="1933",
            network="open",
            runner="claude-code",
            operation=operation,
            credential_environment=credential_environment,
        )
        proxy_args = [
            argv[i + 1]
            for i, arg in enumerate(argv)
            if arg == "-e" and argv[i + 1].split("=", 1)[0] in names
        ]
        assert proxy_args == [f"{name}=" for name in names]
        assert secret not in " ".join(argv)

    def test_build_run_argv_uses_container_init(self, _checkout: Path):
        argv = _agent_argv(_checkout)
        assert "--init" in argv

    def test_build_run_argv_has_hardening(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="42",
            network="open",
            runner="claude-code",
        )
        _assert_contains_all(
            argv,
            "--rm",
            "--name",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "--pids-limit",
            "-w",
            "/work",
        )
        assert f"kew-{42}" in argv
        # The /tmp tmpfs is still there, now carrying its size cap (#1431).
        assert any(a == "/tmp" or a.startswith("/tmp:") for a in argv)

    def test_build_run_argv_mounts_only_checkout(self, _checkout: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: _checkout / "operator-home")
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="7",
            network="open",
            runner="claude-code",
        )
        # Only mount is checkout_dir:/work
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert mounts == [f"{_checkout}:/work"]

        home = Path.home()
        cidfile = argv[argv.index("--cidfile") + 1]
        for arg in argv:
            assert ".claude" not in arg
            assert "dispatch.db" not in arg
            # The host identity file can live under HOME; it is never mounted.
            assert str(home) not in arg or arg in (f"{_checkout}:/work", cidfile)

    def test_build_run_argv_anchors_relative_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # #1663: the review clone arrives as a RELATIVE path
        # (`.worktrees/review/pr-N`). The daemon resolves a relative `-v`
        # source against the docker CLI's cwd, and the launcher runs the CLI
        # with cwd=checkout_dir, so the mount landed on an empty directory
        # nested inside the checkout and the entrypoint exited 64. The argv
        # must carry the absolute path regardless of where it is built.
        monkeypatch.chdir(tmp_path)
        relative = Path(".worktrees") / "review" / "pr-1652"
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=relative,
            slug="1652",
            network="open",
            runner="claude-code",
        )
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert mounts == [f"{(tmp_path / relative).absolute()}:/work"]
        assert mounts[0].startswith("/")

    def test_build_extract_argv_anchors_both_relative_mounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # #1663, second hunk: the extraction container mounts the checkout
        # AND an artifact dir derived from logging.dir ("./logs/dispatch"),
        # both of which arrive relative. Every -v source must be absolute.
        monkeypatch.chdir(tmp_path)
        checkout = Path(".worktrees") / "review" / "pr-1652"
        artifact = Path("logs") / "dispatch" / "extract-1652"
        argv = build_extract_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=checkout,
            artifact_dir=artifact,
            slug="1652",
            base_sha="a" * 40,
            uid=501,
            gid=20,
        )
        sources = [argv[i + 1].split(":")[0] for i, a in enumerate(argv) if a == "-v"]
        assert sources == [
            str((tmp_path / checkout).absolute()),
            str((tmp_path / artifact).absolute()),
        ]
        assert all(s.startswith("/") for s in sources)

    def test_build_run_argv_no_github_token(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="99",
            network="open",
            runner="claude-code",
        )
        # Extract -e values
        env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]

        for env in env_args:
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN" not in env
            # Key value must never appear inline in argv
            assert "sk-ant-" not in env

        # Key name is forwarded (value inherited from caller's env)
        assert "ANTHROPIC_API_KEY" in env_args

    def test_build_run_argv_forwards_all_credentials_by_name(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="3",
            network="open",
            runner="claude-code",
        )
        env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        # The guard marker and empty proxy assignments carry no credentials.
        marker = f"{AGENT_MARKER_ENV}={WORK_MOUNT_PATH}"
        assert env_args.count(marker) == 1
        creds = [
            env
            for env in env_args
            if env != marker
            and not env.endswith("=")
            and not env.startswith("KEW_AGENT_START_NONCE=")
            and not env.startswith(_WORKER_POOL_PREFIXES)
        ]
        # Both credential paths forwarded by NAME only: API key AND the
        # `claude setup-token` OAuth token (subscription auth, #732).
        assert set(creds) == set(CREDENTIAL_ENV_VARS)
        assert all("=" not in env for env in creds)  # never name=value

    def test_build_run_argv_mounts_writable_agent_home(self, _checkout: Path):
        # #742: --read-only broke Claude Code's Bash tool (EROFS on ~/.claude);
        # the agent HOME must be an ephemeral tmpfs pinned to the image's
        # agent user, and --read-only must STAY.
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="8",
            network="open",
            runner="claude-code",
        )
        tmpfs_args = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        home_mounts = [t for t in tmpfs_args if t.startswith("/home/agent")]
        assert len(home_mounts) == 1
        _assert_contains_all(home_mounts[0], "uid=1001", "gid=1001", "mode=0700")
        # #762: the HOME tmpfs must be exec so agents can run tooling they
        # install into HOME (uv-managed binaries, npm globals) instead of
        # littering the bind-mounted /work. Still ephemeral, still uid-pinned.
        assert "exec" in home_mounts[0].split(":", 1)[1].split(",")
        assert "--read-only" in argv  # containment posture unchanged

    def test_tmp_tmpfs_stays_noexec(self, _checkout: Path):
        # #762 scope: only the HOME tmpfs gains exec. /tmp keeps Docker's
        # default (noexec) — we never opt it into exec.
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="8",
            network="open",
            runner="claude-code",
        )
        tmpfs_args = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        tmp_mounts = [t for t in tmpfs_args if t == "/tmp" or t.startswith("/tmp:")]
        assert len(tmp_mounts) == 1
        # /tmp gained a size cap (#1431) and nothing else: `exec` stays off it,
        # so only the agent HOME opts out of Docker's default noexec.
        options = tmp_mounts[0].split(":", 1)[1].split(",")
        assert "exec" not in options, "/tmp tmpfs must keep default (noexec)"
        assert [o for o in options if o.startswith("size=")], (
            "/tmp tmpfs must be sized — a tmpfs is host RAM"
        )

    def test_build_run_argv_uses_slug_for_container_name(self, _checkout: Path):
        # A non-numeric (Linear) work-item id reaches the container name as its
        # branch-safe slug (lowercased, docker-name charset) — never the raw id.
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="sfd-12",
            network="open",
            runner="claude-code",
        )
        name = argv[argv.index("--name") + 1]
        assert name == "kew-sfd-12"

    def test_build_run_argv_requests_a_unique_container_id_file(self, _checkout: Path):
        first = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="42",
            network="open",
            runner="claude-code",
        )
        second = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="42",
            network="open",
            runner="claude-code",
        )
        first_cidfile = first[first.index("--cidfile") + 1]
        second_cidfile = second[second.index("--cidfile") + 1]
        assert first_cidfile != second_cidfile
        assert Path(first_cidfile).name.startswith("kew-42-")
        assert not Path(first_cidfile).exists()

    def test_build_run_argv_network_open_omits_network_flag(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="1",
            network="open",
            runner="claude-code",
        )
        # "open" is not a valid Docker network name; translate to default
        # bridge by omitting --network entirely.
        assert "--network" not in argv
        assert "open" not in argv

    def test_build_run_argv_network_allowlist_uses_kew_allowlist(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="2",
            network="allowlist",
            runner="claude-code",
        )
        # "allowlist" maps to the pre-configured kew-allowlist Docker network;
        # Docker refuses to start the container if that network does not exist
        # (fail closed — no silent fallback to open egress).
        assert "--network" in argv
        idx = argv.index("--network")
        assert argv[idx + 1] == ALLOWLIST_NETWORK
        assert "open" not in argv

    def test_build_run_argv_network_custom_passes_through(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="3",
            network="my-custom-net",
            runner="claude-code",
        )
        assert "--network" in argv
        idx = argv.index("--network")
        assert argv[idx + 1] == "my-custom-net"


class TestDetectRuntime:
    def test_detect_runtime_missing_returns_none(self):
        config = SandboxConfig(runtime="docker")
        with patch("kew.sandbox.shutil.which", return_value=None):
            result = detect_runtime(config)
        assert result is None

    def test_detect_runtime_present_returns_path(self):
        config = SandboxConfig(runtime="docker")
        with patch("kew.sandbox.shutil.which", return_value="/usr/local/bin/docker"):
            result = detect_runtime(config)
        assert result == "/usr/local/bin/docker"


class TestSandboxConfigTriState:
    def test_default_enabled_is_auto(self):
        assert SandboxConfig().enabled == "auto"

    def test_bool_true_back_compat(self):
        assert SandboxConfig(enabled=True).enabled is True

    def test_bool_false_back_compat(self):
        assert SandboxConfig(enabled=False).enabled is False

    def test_string_auto_parses(self):
        assert SandboxConfig(enabled="auto").enabled == "auto"

    def test_invalid_string_rejected(self):
        with pytest.raises(ValueError):
            SandboxConfig(enabled="maybe")


class TestSandboxNetworkDefault:
    def test_default_network_is_allowlist(self):
        assert SandboxConfig().network == "allowlist"

    def test_open_opt_out_is_accepted(self):
        cfg = SandboxConfig(network="open")
        assert cfg.network == "open"

    def test_custom_network_name_is_accepted(self):
        cfg = SandboxConfig(network="kew-allowlist")
        assert cfg.network == "kew-allowlist"


class TestResolveEffectiveSandboxNetwork:
    def setup_method(self) -> None:
        reset_runtime_cache()

    def teardown_method(self) -> None:
        reset_runtime_cache()

    def test_allowlist_default_with_runtime_is_sandboxed(self):
        # Default network="allowlist": posture is "sandboxed" when runtime present.
        config = SandboxConfig()
        assert config.network == "allowlist"
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"

    def test_open_opt_out_with_runtime_is_sandboxed(self):
        # Explicit network="open" opt-out: posture is still "sandboxed".
        config = SandboxConfig(network="open")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"


class TestDetectRuntimeCached:
    def setup_method(self) -> None:
        reset_runtime_cache()

    def teardown_method(self) -> None:
        reset_runtime_cache()

    def test_probes_once_per_process(self):
        config = SandboxConfig(runtime="docker")
        with patch(
            "kew.sandbox.shutil.which", return_value="/usr/bin/docker"
        ) as mock_which:
            first = detect_runtime_cached(config)
            second = detect_runtime_cached(config)
        assert first == second == "/usr/bin/docker"
        mock_which.assert_called_once()  # cached — one probe, not per issue

    def test_reset_clears_cache(self):
        config = SandboxConfig(runtime="docker")
        with patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"):
            detect_runtime_cached(config)
        reset_runtime_cache()
        with patch("kew.sandbox.shutil.which", return_value=None) as mock_which:
            assert detect_runtime_cached(config) is None
        mock_which.assert_called_once()


class TestResolveEffectiveSandbox:
    def setup_method(self) -> None:
        reset_runtime_cache()

    def teardown_method(self) -> None:
        reset_runtime_cache()

    def test_auto_with_runtime_is_sandboxed(self):
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"
        # Bare runtime name, never the shutil.which() path: argv[0] must pass
        # the subprocess allowlist, which matches names only (the 2026-07-16
        # live incident: '/usr/local/bin/docker' was rejected mid-epic).
        assert decision.runtime == "docker"
        assert "/" not in decision.runtime
        assert decision.notice is not None
        assert "on" in decision.notice.lower()

    def test_auto_labels_orbstack_runtime(self):
        config = SandboxConfig(enabled="auto", runtime="docker")
        orb = "/Users/x/.orbstack/bin/docker"
        with patch("kew.sandbox.shutil.which", return_value=orb), _with_credential():
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.notice is not None
        assert "orbstack" in decision.notice.lower()

    def test_auto_without_runtime_refuses_instead_of_downgrading(self):
        """`auto` no longer means "run on the host if you must".

        This is the trapdoor D1 closes. `auto` on a machine with no container
        runtime used to dispatch the agent directly onto the host and say so in
        a single line of notice — so an operator could be in the uncontained
        posture without ever having chosen it, and without noticing.
        """
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value=None),
            pytest.raises(SandboxRuntimeUnavailable) as exc,
        ):
            resolve_effective_sandbox(config, runner="claude-code")
        # Both ways forward are named: contain it, or say you are not.
        assert "docker" in str(exc.value).lower()
        assert "dangerously_skip_containment" in str(exc.value)

    def test_auto_without_runtime_runs_uncontained_when_explicitly_chosen(self):
        """The escape hatch exists — it just has to be asked for by name."""
        config = SandboxConfig(
            enabled="auto", runtime="docker", dangerously_skip_containment=True
        )
        with patch("kew.sandbox.shutil.which", return_value=None):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is False
        assert decision.posture == "uncontained"
        assert decision.runtime is None
        assert decision.notice is not None
        # A banner, not a one-line notice: the old notice was routinely missed.
        assert "DANGEROUS" in decision.notice
        assert "no container boundary" in decision.notice

    def test_true_without_runtime_refuses(self):
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value=None),
            pytest.raises(SandboxRuntimeUnavailable),
        ):
            resolve_effective_sandbox(config, runner="claude-code")

    def test_true_with_runtime_is_sandboxed(self):
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"

    def test_false_alone_is_no_longer_enough_to_skip_containment(self):
        """`enabled = false` is uncontained execution and must say so.

        It used to be the silent path: posture "disabled", no notice at all.
        Two config keys now express one decision, deliberately — `enabled` says
        whether to use a container, `dangerously_skip_containment` says you
        accept what that means. Only the second one carries the warning in its
        name, and that is the one a reader of someone else's kew.toml sees.
        """
        config = SandboxConfig(enabled=False, runtime="docker")
        with pytest.raises(SandboxRuntimeUnavailable) as exc:
            resolve_effective_sandbox(config, runner="claude-code")
        assert "dangerously_skip_containment" in str(exc.value)

    def test_false_with_the_flag_is_uncontained_and_never_probes(self):
        config = SandboxConfig(
            enabled=False, runtime="docker", dangerously_skip_containment=True
        )
        with patch(
            "kew.sandbox.shutil.which", return_value="/usr/bin/docker"
        ) as mock_which:
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is False
        assert decision.posture == "uncontained"
        assert "DANGEROUS" in (decision.notice or "")
        mock_which.assert_not_called()  # opting out must not even probe

    # ── auth provisioning (#721/#732): runtime present, no env credential ─────

    def test_auto_with_runtime_but_no_credential_refuses(self):
        """A container that can't authenticate is not a reason to leave the box.

        This path also used to downgrade to the host. The runtime is present —
        the machine *can* contain — so dropping out of containment because a
        credential is missing traded a solvable problem for an unsolvable one.
        """
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc,
        ):
            resolve_effective_sandbox(config, runner="claude-code")
        # The refusal must still name BOTH provisioning paths (key + token):
        # the fix for this is to authenticate, not to skip containment.
        assert "ANTHROPIC_API_KEY" in str(exc.value)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc.value)
        assert "setup-token" in str(exc.value)

    def test_missing_credential_uncontained_only_when_chosen(self):
        config = SandboxConfig(
            enabled="auto", runtime="docker", dangerously_skip_containment=True
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _without_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.posture == "uncontained"
        assert "DANGEROUS" in (decision.notice or "")

    def test_credential_refusal_is_provider_specific_per_runner(self):
        # #1146: an OpenRouter-backed runner must not be told to run
        # `claude setup-token`. The refusal happens BEFORE any container is
        # launched, so no dispatch is burned.
        config = SandboxConfig(
            enabled=True, runtime="docker", images={"opencode": "ghcr.io/a/oc:v1"}
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
            patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False),
            pytest.raises(SandboxRuntimeUnavailable) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        message = str(excinfo.value)
        assert "OPENROUTER_API_KEY" in message
        # #1985: direct routes need the provisioning key; kew mints the run key.
        assert "OPENROUTER_PROVISIONING_KEY" in message
        assert "kew issues OPENROUTER_API_KEY for each run" in message
        assert "setup-token" not in message
        mock_launch.assert_not_called()

    def test_credential_refusal_never_echoes_a_credential_value(self):
        # No credential VALUE may reach a message (#1146) — names only.
        config = SandboxConfig(enabled=True, runtime="docker")
        env = {name: "" for name in CREDENTIAL_ENV_VARS}
        env["OPENROUTER_API_KEY"] = "sk-or-supersecret"
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch.dict(os.environ, env),
            pytest.raises(SandboxRuntimeUnavailable) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="claude-code")
        assert "supersecret" not in str(excinfo.value)

    def test_oauth_token_alone_counts_as_credential(self):
        # `claude setup-token` subscription auth — no API key needed.
        config = SandboxConfig(enabled="auto", runtime="docker")
        env = {name: "" for name in CREDENTIAL_ENV_VARS}
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "tok-test"
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch.dict(os.environ, env),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"

    def test_true_with_runtime_but_no_credential_refuses(self):
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable, match="credential"),
        ):
            resolve_effective_sandbox(config, runner="claude-code")

    def test_no_path_reaches_host_execution_without_the_flag(self):
        """The load-bearing invariant: one flag gates every uncontained outcome.

        Enumerated rather than argued. Uncontained execution was reachable from
        three unrelated branches, each with its own posture token and its own
        notice — which is why there was no single place to attach a banner and
        no single ledger token to query. If a fourth way to leave the container
        is ever added, it has to come through the same gate or fail here.
        """
        cases = {
            "no runtime": (None, False),
            "runtime, no credential": ("/usr/bin/docker", True),
        }
        for label, (which, drop_credential) in cases.items():
            config = SandboxConfig(enabled="auto", runtime="docker")
            ctx = _without_credential() if drop_credential else nullcontext()
            with (
                patch("kew.sandbox.shutil.which", return_value=which),
                ctx,
                pytest.raises(SandboxRuntimeUnavailable) as exc,
            ):
                resolve_effective_sandbox(config, runner="claude-code")
            assert "dangerously_skip_containment" in str(exc.value), (
                f"{label}: refusal does not name the only supported way to run "
                "uncontained, so an operator hitting it has no way forward"
            )

        # enabled=false short-circuits before any probe, hence its own case.
        with pytest.raises(SandboxRuntimeUnavailable) as exc:
            resolve_effective_sandbox(
                SandboxConfig(enabled=False), runner="claude-code"
            )
        assert "dangerously_skip_containment" in str(exc.value)


class TestRunnerAwareImageResolution:
    """The runner axis of the sandbox decision (#1144, epic node N2).

    The #1104 incident: an ``opencode`` node was dispatched *contained* only
    because the driver env carried ``CLAUDE_CODE_OAUTH_TOKEN`` for sibling
    claude nodes, so the decision — runtime + credential, runner never
    consulted — sent an OpenRouter model string into the claude-only image.
    The runner now picks the image, and an unmapped runner never falls back to
    the claude one.
    """

    def teardown_method(self) -> None:
        reset_runtime_cache()

    _OPENCODE_IMAGE = "ghcr.io/thossullivan/kew-agent-opencode:latest"

    def test_codex_declares_native_containment_without_a_container_image(self):
        """A live runtime must not make the official Codex route undispatchable."""
        config = SandboxConfig(enabled="auto", runtime="docker")
        with patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"):
            decision = resolve_effective_sandbox(config, runner="codex")

        assert decision.sandboxed is False
        assert decision.posture == "native"
        assert decision.runtime is None
        assert decision.image is None

    @pytest.mark.parametrize("detected", [None, "/usr/bin/docker"])
    def test_codex_native_containment_satisfies_fail_closed_on_the_docker_lane(
        self, detected
    ):
        """`enabled = true` + a runner that contains itself = no runtime needed.

        Asserted as the OUTCOME on both hosts rather than as "detection is never
        called". The resolver does consult detection before the posture question
        now, because the answer can depend on whether kew is really holding a
        boundary for this dispatch (`_offered_runtime`), and pinning the probe
        away is what let an absent gateway CLI be reported to codex as a
        boundary it should defer to — a hard `runtime not found` refusal on a run
        its own seatbelt contains (PR #2461 review round 3). What has to stay
        true is this: on the docker lane the declaration does not move, whether
        or not the daemon is installed.
        """
        config = SandboxConfig(enabled=True, runtime="docker")
        with patch("kew.sandbox.shutil.which", return_value=detected):
            decision = resolve_effective_sandbox(config, runner="codex")

        assert decision.posture == "native"
        assert (decision.sandboxed, decision.runtime, decision.image) == (
            False,
            None,
            None,
        )

    def test_enabled_false_does_not_un_contain_a_natively_contained_runner(self):
        """`enabled = false` turns off KEW's container, not codex's own.

        The native check now runs before the `enabled` check, and has to: the
        uncontained gate demands `dangerously_skip_containment`, and demanding
        it for a runner that contains itself would be both wrong and hostile.
        The ledger records what is true — the run had a boundary, just not
        kew's — rather than flattening it to "disabled".
        """
        decision = resolve_effective_sandbox(
            SandboxConfig(enabled=False), runner="codex"
        )

        assert decision.posture == "native"
        assert decision.notice is None  # nothing dangerous happened

    def test_opencode_with_claude_token_never_gets_the_claude_image(self):
        # The #1104 shape, pinned: contained-looking env (runtime + claude
        # token), resolved runner is opencode, no images mapping.
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
            pytest.raises(SandboxImageUnmapped) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        assert config.image not in str(excinfo.value)

    def test_auto_does_not_downgrade_an_unmapped_runner_to_the_host(self):
        # `auto` means "contain when the host can" — the runtime IS here, so a
        # gap on kew's side (no image for this runner) must not quietly hand
        # the dispatch to the uncontained host path. The committed kew.toml
        # routes `simple` to opencode with no images map, so this is the exact
        # live shape: it now refuses loudly instead of running guard-only.
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
            pytest.raises(SandboxImageUnmapped, match=r"images\.opencode"),
        ):
            resolve_effective_sandbox(config, runner="opencode")

    def test_unmapped_runner_fails_closed_when_containment_is_required(self):
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
            pytest.raises(SandboxImageUnmapped) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        message = str(excinfo.value)
        assert "opencode" in message
        assert "[sandbox] images.opencode" in message
        # Actionable: names the key to set, and never the claude image.
        assert 'images.opencode = "' in message
        assert config.image not in message

    def test_skip_flag_does_not_excuse_an_unmapped_runner(self):
        """`dangerously_skip_containment` is not a blanket "stop checking".

        The runtime is present and the machine can contain; what's missing is a
        kew-side image mapping. That stays a hard refusal so the flag cannot be
        cargo-culted into a config as the fix for every sandbox complaint —
        which would quietly un-contain every dispatch, not just this one.
        """
        config = SandboxConfig(
            enabled="auto", runtime="docker", dangerously_skip_containment=True
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
            pytest.raises(SandboxImageUnmapped, match="images.opencode"),
        ):
            resolve_effective_sandbox(config, runner="opencode")

    def test_an_uncontainable_runner_is_not_treated_as_an_unbuilt_one(self):
        """A runner that can NEVER be contained refuses differently (W1.1/D2).

        An unmapped image is a gap kew can still close, so it fails closed even
        with the skip flag — there is a fix pending. Lemonade drives a
        host-local server a contained network cannot reach, so no image is
        coming. Routing it through the same gate made it undispatchable on any
        host with a runtime and named a remedy that would never arrive.
        """
        config = SandboxConfig(enabled="auto", runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            pytest.raises(SandboxRuntimeUnavailable) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="lemonade")
        message = str(excinfo.value)
        assert "host-local inference server" in message
        assert "dangerously_skip_containment" in message
        # The refusal must not read as a missing-image config gap.
        assert "images.lemonade" not in message

    def test_an_uncontainable_runner_runs_only_when_skipping_is_explicit(self):
        config = SandboxConfig(
            enabled="auto", runtime="docker", dangerously_skip_containment=True
        )
        with patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"):
            decision = resolve_effective_sandbox(config, runner="lemonade")
        assert decision.posture == "uncontained"
        assert not decision.sandboxed
        assert decision.notice is not None
        assert "host-local inference server" in decision.notice

    def test_an_uncontainable_runner_decides_the_same_without_a_runtime(self):
        """Whether Docker is installed says nothing about this runner."""
        with patch("kew.sandbox.shutil.which", return_value=None):
            with pytest.raises(SandboxRuntimeUnavailable) as excinfo:
                resolve_effective_sandbox(
                    SandboxConfig(enabled="auto", runtime="docker"), runner="lemonade"
                )
            assert "host-local inference server" in str(excinfo.value)

            decision = resolve_effective_sandbox(
                SandboxConfig(
                    enabled="auto",
                    runtime="docker",
                    dangerously_skip_containment=True,
                ),
                runner="lemonade",
            )
        assert decision.posture == "uncontained"

    def test_only_runner_code_may_declare_uncontainability(self):
        """Config must not be able to assert "this cannot be contained".

        That claim un-contains a runner by design, so it belongs with the
        runner's own code — the same rule ``containment_posture`` follows. An
        unknown runner declares nothing and keeps the fail-closed path.
        """
        from kew.sandbox import runner_uncontainable

        assert runner_uncontainable("lemonade") is not None
        assert runner_uncontainable("claude-code") is None
        assert runner_uncontainable("not-a-real-runner") is None

    @pytest.mark.parametrize(
        "runner", ["claude-code", "codex", "opencode", "pi", "hermes", "lemonade"]
    )
    @pytest.mark.parametrize("enabled", [True, "auto", False])
    def test_every_runner_has_an_explicit_outcome_for_each_enabled_mode(
        self, runner: str, enabled: bool | str
    ) -> None:
        """Every registered runner resolves a posture or a named refusal."""
        from kew.sandbox import REFUSAL_RUNTIME_UNAVAILABLE

        config = SandboxConfig(
            enabled=enabled,  # type: ignore[arg-type]
            runtime="docker",
            images={runner: f"kew-{runner}:test"},
        )
        env = {
            "ANTHROPIC_API_KEY": "anthropic-test-value",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test-value",
            "OPENROUTER_API_KEY": "openrouter-test-value",
        }
        expected = (
            "native"
            if runner == "codex"
            else "refused"
            if enabled is False or runner == "lemonade"
            else "sandboxed"
        )

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(os.environ, env, clear=False),
        ):
            if expected == "refused":
                with pytest.raises(SandboxRuntimeUnavailable) as raised:
                    resolve_effective_sandbox(config, runner=runner)
                assert raised.value.reason == REFUSAL_RUNTIME_UNAVAILABLE
            else:
                decision = resolve_effective_sandbox(config, runner=runner)
                assert decision.posture == expected

    @pytest.mark.parametrize(
        ("runner", "expected"),
        [
            # The egress gate is opened below, so this is the matrix BEHIND it:
            # what the launch-path gate admits once a boundary is certified.
            # `test_openshell_refuses_an_uncertified_boundary` is the matrix in
            # front of it.
            ("claude-code", "sandboxed"),
            ("opencode", "sandboxed"),
            # No program and no packaged policy on this runtime, so the gateway
            # seam would refuse them at launch. Admission says so first, under
            # the runtime's own token, rather than recording `sandboxed` for a
            # boundary this build cannot hold (PR #2401 review).
            ("pi", "openshell-runtime-unavailable"),
            ("hermes", "openshell-runtime-unavailable"),
            # Contained HERE, and native everywhere else (epic node N10).
            # `CodexRunner.containment_posture` is asked with the configured
            # runtime, and declines its own seatbelt on this one because kew
            # holds a real boundary for it: the agent-codex image, the packaged
            # policy and a gateway launch path. It is the one runner on this
            # matrix whose `required_credentials` is empty, which is the
            # accurate declaration on a runtime where the gateway injects the
            # credential and no env name is forwarded.
            ("codex", "sandboxed"),
            # Uncontainable by its own declaration, which is checked first.
            ("lemonade", "runtime_unavailable"),
        ],
    )
    @pytest.mark.parametrize("enabled", [True, "auto"])
    def test_openshell_is_an_admitted_sandboxed_posture(
        self, runner: str, expected: str, enabled: bool | str, monkeypatch
    ) -> None:
        """The posture matrix row for `runtime = "openshell"` (epic node N6).

        It used to refuse under `openshell-runtime-unavailable`, because kew
        had no way to launch into it. `kew.openshell_launch` is that way, so
        every runner this build can actually launch there resolves `sandboxed`
        — through the gateway's PROVIDER rather than the host's environment,
        which is why the credential below is deliberately absent from the
        environment and present on the gateway. A runner with no program and no
        packaged policy is NOT one of those, and refuses here rather than at
        the gateway seam.

        The egress-certification gate (#2319) runs in front of this one and is
        opened here, as `TestOpenShellCredentialBroker` opens it: this matrix
        is about what the launch path admits. With the gate passed, egress is
        `openshell-certified` — the epic's verification node (N9) is what
        replaced the direct route's frozen `operator-managed-unverified`
        (#1945) for this boundary.
        """
        from kew.dispatch_core import _resolve_egress_verification

        config = SandboxConfig(
            enabled=enabled,  # type: ignore[arg-type]
            runtime="openshell",
            images={runner: f"kew-{runner}:test"},
            openshell={"providers": {runner: f"kew-{runner}"}},  # type: ignore[arg-type]
        )
        self._provider_on_the_gateway(monkeypatch, f"kew-{runner}")
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )

        with (
            patch(
                "kew.sandbox.detect_runtime_cached",
                return_value="/usr/bin/openshell",
            ),
            _without_credential(),
        ):
            if expected.endswith("unavailable"):
                with pytest.raises(SandboxRuntimeUnavailable) as raised:
                    resolve_effective_sandbox(config, runner=runner)
                assert raised.value.reason == expected
                return
            decision = resolve_effective_sandbox(config, runner=runner)

        assert decision.posture == expected
        if expected == "sandboxed":
            assert (decision.sandboxed, decision.runtime) == (True, "openshell")
            assert _resolve_egress_verification(decision, None) == "openshell-certified"

    @pytest.mark.parametrize(
        ("runner", "expected"),
        [
            # No ledger is supplied here, so the egress gate refuses first —
            # see test_openshell_certification.py for the ordering and for the
            # launch-path refusal that follows a matching certification (#2319).
            ("claude-code", "openshell-egress-uncertified"),
            ("opencode", "openshell-egress-uncertified"),
            # No packaged OpenShell policy, so they never reach the egress
            # gate's ledger read: their own token, because the command that
            # clears that gate cannot succeed for them (#2435).
            ("pi", "openshell-policy-unpackaged"),
            ("hermes", "openshell-policy-unpackaged"),
            # Behind the gate like any other contained runner now (node N10):
            # its own seatbelt no longer pre-empts the question, so an
            # uncertified codex boundary is refused rather than answered with a
            # host run — which is the whole point of naming this runtime.
            ("codex", "openshell-egress-uncertified"),
            # Uncontainable by its own declaration, which is checked first.
            ("lemonade", "runtime_unavailable"),
        ],
    )
    @pytest.mark.parametrize("enabled", [True, "auto"])
    def test_openshell_refuses_an_uncertified_boundary(
        self, runner: str, expected: str, enabled: bool | str, monkeypatch
    ) -> None:
        """`runtime = "openshell"` refuses under its own tokens, never silently.

        Two refusals, each with its OWN token and its own fix: the boundary has
        no fresh egress certification (``openshell-egress-uncertified``, the
        gate this test reaches, because no ledger is supplied), and the launch
        path cannot serve this dispatch (``openshell-runtime-unavailable``,
        asserted behind the opened gate in
        ``test_openshell_is_an_admitted_sandboxed_posture``). Neither is
        ``runtime_unavailable``: the host is not missing a runtime. Sandboxing
        on an openshell binary that happens to be installed would claim a
        boundary kew has not proven.
        """
        config = SandboxConfig(
            enabled=enabled,  # type: ignore[arg-type]
            runtime="openshell",
            images={runner: f"kew-{runner}:test"},
            openshell={"providers": {runner: f"kew-{runner}"}},  # type: ignore[arg-type]
        )
        self._provider_on_the_gateway(monkeypatch, f"kew-{runner}")

        with (
            patch(
                "kew.sandbox.detect_runtime_cached",
                return_value="/usr/bin/openshell",
            ),
            _without_credential(),
        ):
            if expected == "native":
                assert resolve_effective_sandbox(config, runner=runner).posture == (
                    "native"
                )
                return
            with pytest.raises(SandboxRuntimeUnavailable) as raised:
                resolve_effective_sandbox(config, runner=runner)
        assert raised.value.reason == expected

    def test_no_runner_is_admitted_that_the_launch_path_would_refuse(
        self, monkeypatch
    ) -> None:
        """Admission and the gateway seam agree on which runners can launch.

        The gate read the runtime NAME alone, so `pi` and `hermes` — neither
        of which has an entry in `RUNNER_PROGRAMS` or a packaged
        `policy-<runner>.yaml` — resolved `sandboxed` on openshell and were
        only refused once `runner_program` / `runner_policy_path` ran at
        launch, after that posture had been attested (PR #2401 review). Walked
        over the whole registry rather than the two known names, so a runner
        added to one side and not the other fails here.
        """
        from kew.openshell_launch import (
            RUNNER_PROGRAMS,
            OpenShellLaunchRefused,
            runner_policy_path,
        )
        from kew.runners.registry import _get_runner_factories

        # Opened, or every runner stops at the egress gate (#2319) and the walk
        # below `continue`s past the very check it exists to make.
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )
        for runner in _get_runner_factories():
            config = SandboxConfig(
                enabled="auto",
                runtime="openshell",
                images={runner: f"kew-{runner}:test"},
                openshell={"providers": {runner: f"kew-{runner}"}},  # type: ignore[arg-type]
            )
            self._provider_on_the_gateway(monkeypatch, f"kew-{runner}")
            with (
                patch(
                    "kew.sandbox.detect_runtime_cached",
                    return_value="/usr/bin/openshell",
                ),
                _without_credential(),
            ):
                try:
                    decision = resolve_effective_sandbox(config, runner=runner)
                except SandboxRuntimeUnavailable:
                    continue
            if decision.posture != "sandboxed":
                continue
            assert runner in RUNNER_PROGRAMS, (
                f"{runner} is admitted as sandboxed on openshell but the "
                "launch path has no program for it"
            )
            try:
                runner_policy_path(runner)
            except OpenShellLaunchRefused as exc:  # pragma: no cover - the bug
                pytest.fail(f"{runner} is admitted with no policy: {exc}")

    @staticmethod
    def _provider_on_the_gateway(monkeypatch, provider: str) -> None:
        """Answer `provider get` for ``provider``; refuse anything else."""
        import json
        import subprocess

        from kew import subprocess_utils

        def run(cmd, **kwargs):
            payload = json.dumps(
                {
                    "name": provider,
                    "profile": "anthropic",
                    "credentials": ["ANTHROPIC_API_KEY"],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        monkeypatch.setattr(subprocess_utils, "run", run)

    def test_openshell_still_refuses_when_containment_is_switched_off(self) -> None:
        """Naming a runtime asks for containment; a host run is not that answer.

        N6 admits the runtime, not the downgrade: `enabled = false` with the
        skip flag is still the operator asking for the host, and is handled
        before the runtime matters — but a runner this build cannot launch
        into would still refuse under the openshell token rather than fall
        back to the host. The egress gate in front is opened so the launch
        gate is what this asserts; the skip-flag test below is the same rule at
        the egress gate.
        """
        from kew.runtime_names import RuntimeBackendUnavailable

        config = SandboxConfig(
            enabled="auto",
            runtime="openshell",
            dangerously_skip_containment=True,
        )

        with (
            patch(
                "kew.sandbox.require_certified_boundary",
                lambda config, *, runner, store: None,
            ),
            patch(
                "kew.sandbox.require_launchable_runtime",
                side_effect=RuntimeBackendUnavailable("openshell"),
            ),
            pytest.raises(SandboxRuntimeUnavailable) as raised,
        ):
            resolve_effective_sandbox(config, runner="claude-code")

        assert raised.value.reason == "openshell-runtime-unavailable"
        assert "openshell" in str(raised.value)
        assert "docker" in str(raised.value)

    @pytest.mark.parametrize("runner", ["pi", "hermes"])
    @pytest.mark.parametrize("store", [None, object()])
    def test_a_runner_with_no_packaged_policy_refuses_under_its_own_token(
        self, runner: str, store: object | None
    ) -> None:
        """Name the missing policy, never a command that cannot succeed.

        `pi` and `hermes` have mapped images and declared credentials, so they
        reached the egress gate and failed it on the absent `policy-<r>.yaml` —
        and were told to run `kew sandbox certify`, which for them can only
        fail the same way. Whether a ledger is supplied changes nothing: kew
        packaging a policy is the only fix, so the refusal is its own token and
        names no command (#2435).
        """
        from kew.sandbox import REFUSAL_OPENSHELL_POLICY_UNPACKAGED

        config = SandboxConfig(
            enabled=True,
            runtime="openshell",
            images={runner: f"kew-{runner}:test"},
        )

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "openrouter-test-value"}, clear=False
            ),
            pytest.raises(SandboxRuntimeUnavailable) as raised,
        ):
            resolve_effective_sandbox(config, runner=runner, store=store)

        assert raised.value.reason == REFUSAL_OPENSHELL_POLICY_UNPACKAGED
        assert raised.value.runtime == "openshell"
        message = str(raised.value)
        assert f"no packaged OpenShell dispatch policy for runner {runner!r}" in message
        assert "kew sandbox certify" not in message
        # The runners that DO have one, so the operator can pick one instead.
        for packaged in ("claude-code", "codex", "opencode"):
            assert packaged in message

    def test_the_policy_gate_does_not_fire_for_a_packaged_runner(self) -> None:
        """A packaged policy leaves the egress gate's own refusal in place.

        The guard above is a carve-out, not a new front door: `claude-code`
        ships a policy, so its storeless resolution must still refuse as
        `openshell-egress-uncertified` with the command that clears it.
        """
        config = SandboxConfig(enabled=True, runtime="openshell")

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "anthropic-test-value"}, clear=False
            ),
            pytest.raises(SandboxRuntimeUnavailable) as raised,
        ):
            resolve_effective_sandbox(config, runner="claude-code")

        assert raised.value.reason == "openshell-egress-uncertified"
        assert "kew sandbox certify" in str(raised.value)

    def test_openshell_refusal_is_not_waived_by_the_skip_flag(self) -> None:
        """Naming a runtime asks for containment; a host run is not that answer.

        Every other refusal here has an operator-chosen host path, because the
        operator can want it. This one cannot: the request was "contain with
        openshell", and running uncontained instead is exactly the silent
        downgrade this resolution exists to prevent. The message names the fix
        for the gate it stopped at — here the egress certification, because no
        ledger was supplied to read one from (#2319).
        """
        config = SandboxConfig(
            enabled="auto",
            runtime="openshell",
            dangerously_skip_containment=True,
        )

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            pytest.raises(SandboxRuntimeUnavailable) as raised,
        ):
            resolve_effective_sandbox(config, runner="claude-code")

        assert raised.value.reason == "openshell-egress-uncertified"
        assert "openshell" in str(raised.value)
        assert "kew sandbox certify" in str(raised.value)

    def test_openshell_is_moot_once_containment_itself_is_switched_off(self) -> None:
        """`enabled = false` never reaches a container, so the runtime is dead config.

        The openshell refusal guards the container path. Raising it here would
        report a problem with a setting this dispatch does not use, over an
        operator choice already made by name.
        """
        config = SandboxConfig(
            enabled=False,
            runtime="openshell",
            dangerously_skip_containment=True,
        )

        decision = resolve_effective_sandbox(config, runner="claude-code")

        assert decision.posture == "uncontained"
        assert decision.runtime is None

    def test_mapped_runner_resolves_to_its_own_image(self):
        config = SandboxConfig(
            enabled=True, runtime="docker", images={"opencode": self._OPENCODE_IMAGE}
        )
        assert resolve_runner_image(config, "opencode") == self._OPENCODE_IMAGE

    def test_claude_code_still_uses_the_legacy_image_field(self):
        # Back-compat: every committed config sets `image`, not `images`.
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.sandboxed is True
        assert decision.image == config.image

    def test_images_entry_wins_over_the_legacy_image_field(self):
        pinned = "ghcr.io/thossullivan/kew-agent:v9"
        config = SandboxConfig(
            enabled=True, runtime="docker", images={"claude-code": pinned}
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _with_credential(),
        ):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.image == pinned

    def test_non_claude_runner_does_not_gate_on_claude_credentials(self):
        # The credential half was claude-only: the same committed config
        # behaved differently token-less vs token-full, for every runner. It no
        # longer does — claude's token present or absent gives opencode the SAME
        # answer, because opencode is gated on ITS OWN credential (#1146).
        config = SandboxConfig(
            enabled=True, runtime="docker", images={"opencode": self._OPENCODE_IMAGE}
        )
        outcomes = []
        for env in (_with_credential(), _without_credential()):
            with (
                patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
                env,
                patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False),
                pytest.raises(SandboxRuntimeUnavailable) as excinfo,
            ):
                resolve_effective_sandbox(config, runner="opencode")
            outcomes.append(str(excinfo.value))
            reset_runtime_cache()
        assert outcomes[0] == outcomes[1]
        # And the refusal names OPENROUTER, not claude's vars (#1146).
        assert "OPENROUTER_API_KEY" in outcomes[0]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in outcomes[0]

    def test_declared_opencode_credential_makes_it_containable(self):
        # The N5 payoff: a mapped image plus opencode's OWN credential in the
        # environment contains opencode — with no claude token anywhere.
        config = SandboxConfig(
            enabled=True, runtime="docker", images={"opencode": self._OPENCODE_IMAGE}
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _without_credential(),
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False),
        ):
            decision = resolve_effective_sandbox(config, runner="opencode")
        assert decision.sandboxed is True
        assert decision.posture == "sandboxed"
        assert decision.image == self._OPENCODE_IMAGE

    def test_mapped_runner_without_a_declared_credential_refuses_to_launch(self):
        # An image mapping alone does not make a runner containable: a runner
        # declaring no in-container credential would start a container with no
        # provider credential and authenticate as nothing. Refuse rather than
        # launch it — and never borrow another runner's key to fill the gap.
        #
        # The subject is a CONTAINABLE runner with the credential lookup
        # emptied, not lemonade. Lemonade was convenient here because it
        # declares nothing, but it is now refused one gate earlier as
        # uncontainable-by-design (W1.1/D2), which would leave this rule
        # passing for the wrong reason.
        config = SandboxConfig(
            enabled="auto", runtime="docker", images={"opencode": "kew-opencode:x"}
        )
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("kew.sandbox.runner_credential_env_vars", return_value=()),
            _with_credential(),
            pytest.raises(SandboxCredentialUndeclared) as excinfo,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        message = str(excinfo.value)
        assert "opencode" in message
        assert "no in-container credential" in message
        # Fail-closed family, so existing handlers already cover it.
        assert issubclass(SandboxCredentialUndeclared, SandboxRuntimeUnavailable)

    def test_declared_runner_credentials_are_all_actually_injected(self):
        # Drift guard on the resolution/injection seam (#1146): whatever a
        # runner declares — and therefore whatever the preflight gates on — is
        # EXACTLY what build_run_argv forwards. A declaration the injection did
        # not honour would contain a runner with an unforwarded credential; an
        # injection beyond the declaration would hand a container a key its own
        # provider never needs.
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        for runner in sorted(KNOWN_RUNNER_NAMES):
            declared = runner_credential_env_vars(runner)
            argv = build_run_argv(
                runtime="docker",
                image="example:latest",
                checkout_dir=Path("/tmp/checkout"),  # nosec B108 - argv only
                slug="42",
                network="allowlist",
                runner=runner,
            )
            injected = [
                argv[i + 1]
                for i, a in enumerate(argv)
                if a == "-e"
                and not argv[i + 1].startswith(
                    (f"{AGENT_MARKER_ENV}=", "KEW_AGENT_START_NONCE=")
                )
                and not argv[i + 1].startswith(_WORKER_POOL_PREFIXES)
                and not argv[i + 1].endswith("=")
            ]
            assert injected == list(declared), (
                f"{runner} declares {declared} but build_run_argv injects {injected}"
            )

    def test_unknown_runner_gets_no_credentials_injected(self):
        # Fail closed on the injection side too: an unregistered runner name
        # must never inherit another runner's allowlist (#1146).
        assert runner_credential_env_vars("not-a-runner") == ()
        argv = build_run_argv(
            runtime="docker",
            image="example:latest",
            checkout_dir=Path("/tmp/checkout"),  # nosec B108 - argv only
            slug="42",
            network="allowlist",
            runner="not-a-runner",
        )
        # Only the guard marker and the ten empty proxy assignments remain.
        injected = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assert [
            arg
            for arg in injected
            if not arg.startswith(("KEW_AGENT_START_NONCE=", *_WORKER_POOL_PREFIXES))
        ] == [
            f"{AGENT_MARKER_ENV}={WORK_MOUNT_PATH}",
            "HTTP_PROXY=",
            "HTTPS_PROXY=",
            "NO_PROXY=",
            "http_proxy=",
            "https_proxy=",
            "no_proxy=",
            "FTP_PROXY=",
            "ALL_PROXY=",
            "ftp_proxy=",
            "all_proxy=",
        ]

    def test_build_run_argv_sets_guard_marker_for_every_runner(self):
        # The security guard keys its agent-only rules (the .claude Write/Edit
        # block, the cross-checkout ref rule) on AGENT_ASSIGNED_WORKTREE. The
        # host child env is never inherited by a container, so build_run_argv
        # must set it as the container path of the mount for every runner,
        # known or not (#1691).
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        for runner in sorted(KNOWN_RUNNER_NAMES) + ["not-a-runner"]:
            argv = build_run_argv(
                runtime="docker",
                image="example:latest",
                checkout_dir=Path("/tmp/checkout"),  # nosec B108 - argv only
                slug="42",
                network="allowlist",
                runner=runner,
            )
            env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
            assert env_args.count(f"{AGENT_MARKER_ENV}={WORK_MOUNT_PATH}") == 1, runner
            # The marker names the mount the container actually gets.
            mount = argv[argv.index("-v") + 1]
            assert mount.endswith(f":{WORK_MOUNT_PATH}")

    def test_claude_runner_still_gates_on_claude_credentials(self):
        config = SandboxConfig(enabled=True, runtime="docker")
        with (
            patch("kew.sandbox.shutil.which", return_value="/usr/bin/docker"),
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable, match="credential"),
        ):
            resolve_effective_sandbox(config, runner="claude-code")

    def test_opting_out_short_circuits_before_any_image_lookup(self):
        config = SandboxConfig(
            enabled=False, runtime="docker", dangerously_skip_containment=True
        )
        decision = resolve_effective_sandbox(config, runner="opencode")
        assert decision.posture == "uncontained"
        # An unmapped runner is irrelevant when no image will be pulled — the
        # opt-out must not surface an image error the operator cannot act on.
        assert decision.image is None

    def test_uncontained_decision_carries_no_image(self):
        config = SandboxConfig(
            enabled="auto", runtime="docker", dangerously_skip_containment=True
        )
        with patch("kew.sandbox.shutil.which", return_value=None):
            decision = resolve_effective_sandbox(config, runner="claude-code")
        assert decision.posture == "uncontained"
        assert decision.image is None

    def test_unmapped_image_is_a_runtime_unavailable_for_old_handlers(self):
        # Callers fail closed on SandboxRuntimeUnavailable; the image failure
        # must ride that same handling rather than escaping as a new type.
        assert issubclass(SandboxImageUnmapped, SandboxRuntimeUnavailable)

    def test_resolve_runner_image_is_reusable_by_the_reviewer(self):
        config = SandboxConfig(images={"opencode": self._OPENCODE_IMAGE})
        assert resolve_runner_image(config, "opencode") == self._OPENCODE_IMAGE
        assert resolve_runner_image(config, "claude-code") == config.image
        with pytest.raises(SandboxImageUnmapped, match=r"images\.pi"):
            resolve_runner_image(config, "pi")

    def test_blanked_out_legacy_image_leaves_claude_code_unmapped(self):
        # `image = ""` is not "use the default" — there is nothing to launch,
        # so say so instead of building an argv with an empty image ref.
        config = SandboxConfig(image="")
        with pytest.raises(SandboxImageUnmapped, match=r"images\.claude-code"):
            resolve_runner_image(config, DEFAULT_IMAGE_RUNNER)


def test_command_guard_is_loaded_only_for_a_complete_claude_checkout(
    tmp_path: Path,
) -> None:
    """A marker or runner name alone cannot claim guard protection."""
    import kew.sandbox as sandbox

    guard_state = getattr(sandbox, "command_guard_state", None)
    assert callable(guard_state), "sandbox must expose exact command-guard state"
    _assert_guard_state(guard_state, tmp_path, "absent")

    hook = tmp_path / ".claude" / "hooks" / "security-guard.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _assert_guard_state(guard_state, tmp_path, "absent")

    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": matcher,
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": ".claude/hooks/security-guard.sh",
                                }
                            ],
                        }
                        for matcher in ("Bash", "Write", "Edit")
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    _assert_guard_state(guard_state, tmp_path, "absent")

    hook.chmod(0o755)
    _assert_guard_state(guard_state, tmp_path, "absent")

    trusted_hook = (
        Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "security-guard.sh"
    )
    hook.write_bytes(trusted_hook.read_bytes())
    hook.chmod(0o644)
    _assert_guard_state(guard_state, tmp_path, "absent")

    hook.chmod(0o755)
    _assert_guard_state(guard_state, tmp_path, "loaded")

    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": ".claude/hooks/security-guard.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    _assert_guard_state(guard_state, tmp_path, "absent")

    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash|Write|Edit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": ".claude/hooks/security-guard.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    _assert_guard_state(guard_state, tmp_path, "loaded")

    for invalid_matcher in ("[", "a{4294967296}"):
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": invalid_matcher,
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": ".claude/hooks/security-guard.sh",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("kew.progress._warn") as warn:
            _assert_guard_state(guard_state, tmp_path, "absent")
        assert warn.call_count > 0
        assert "invalid Claude command guard matcher" in warn.call_args.args[0]

    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash|Write|Edit",
                            "hooks": [
                                {
                                    "type": "prompt",
                                    "command": ".claude/hooks/security-guard.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    _assert_guard_state(guard_state, tmp_path, "absent")
    assert guard_state("opencode", tmp_path) == "not-applicable"


def test_uncontained_posture_names_the_complete_host_environment() -> None:
    """Host exposure is not limited to the container credential allowlist."""
    from kew.sandbox import posture_record_fields

    environment = {
        "PATH": "/usr/bin",
        "UNDECLARED_CREDENTIAL": "credential-value",
        "KEW_ACTOR": "orchestrator-only",
    }
    with patch.dict(os.environ, environment, clear=True):
        fields = posture_record_fields(
            runner="claude-code",
            posture="uncontained",
            egress_verification="unavailable",
        )

    assert fields["credential_names"] == ["PATH", "UNDECLARED_CREDENTIAL"]


def _completion_inspection(argv: list[str], code: int) -> str:
    return json.dumps(
        {
            "Name": "/" + argv[argv.index("--name") + 1],
            "State": {"Status": "exited", "ExitCode": code},
        }
    )


class TestSynchronousContainerLaunch:
    @pytest.mark.parametrize(
        "recorded_name",
        [
            "expected",
            "expected-podman",
            "replacement",
            "replacement-podman",
            "prefixed",
        ],
    )
    def test_completion_inspects_recorded_id_and_checks_name(
        self, tmp_path, monkeypatch, recorded_name
    ):
        """Docker reports ``/<name>``; Podman reports ``<name>``; both confirm.

        Any other encoding (a replacement, a prefixed name) does not.
        """
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        name = argv[argv.index("--name") + 1]
        cid = "a" * 64
        Path(argv[argv.index("--cidfile") + 1]).write_text(cid)
        calls = []
        reported = {
            "expected": "/" + name,
            "expected-podman": name,
            "replacement": "/replacement",
            "replacement-podman": "replacement",
            "prefixed": "/" + name + "-2",
        }[recorded_name]

        def inspect(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-1] != cid:
                return subprocess.CompletedProcess(cmd, 1, "", "unknown container")
            data = {
                "Name": reported,
                "State": {"Status": "exited", "ExitCode": 0},
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

        monkeypatch.setattr(subprocess_utils, "run", inspect)
        confirmed = sandbox._container_completion_confirmed(argv, 0)

        assert calls[0][-1] == cid
        assert confirmed is recorded_name.startswith("expected")

    @pytest.mark.parametrize("output_limit", [None, 128])
    @pytest.mark.parametrize("confirmed", [True, False])
    def test_completion_removes_only_recorded_id(
        self, tmp_path, monkeypatch, confirmed, output_limit
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        cid = "a" * 64
        controls = []
        monkeypatch.setenv("OPENROUTER_API_KEY", "<ambient>")
        monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage>")

        def runtime(cmd, **kwargs):
            if cmd[1] == "run":
                assert "--rm" not in cmd
                assert "OPENROUTER_API_KEY" not in kwargs["env"]
                assert "OPENROUTER_PROVISIONING_KEY" not in kwargs["env"]
                cidfile.write_text(cid)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            controls.append(cmd)
            data = {
                "Name": "/" + name,
                "State": {
                    "Status": "exited" if confirmed else "running",
                    "ExitCode": 0,
                },
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

        monkeypatch.setattr(subprocess_utils, "run", runtime)
        monkeypatch.setattr(subprocess_utils, "run_bounded", runtime)
        result = run_container_with_cpu_cap_retry(
            argv, timeout=10, output_limit=output_limit
        )

        expected_rm = ["docker", "rm", *([] if confirmed else ["-f"]), cid]
        assert controls[-1] == expected_rm
        assert name not in controls[-1]
        assert (sandbox.container_client_failure(result) is None) is confirmed
        assert not cidfile.exists()

    def test_podman_completion_confirms_and_removes_by_id(self, tmp_path, monkeypatch):
        """A Podman runtime reports the bare name: the launch completes, the
        container is removed once without force by its recorded id, and the
        cidfile is deleted (round 8; fails on a784d714 with a forced removal)."""
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        cid = "b" * 64
        controls = []

        def runtime(cmd, **kwargs):
            if cmd[1] == "run":
                cidfile.write_text(cid)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            controls.append(cmd)
            data = {"Name": name, "State": {"Status": "exited", "ExitCode": 0}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

        monkeypatch.setattr(subprocess_utils, "run", runtime)
        result = run_container_with_cpu_cap_retry(argv, timeout=10)

        assert sandbox.container_client_failure(result) is None
        assert [cmd[1] for cmd in controls] == ["inspect", "rm"]
        assert controls[-1] == ["docker", "rm", cid]
        assert not cidfile.exists()

    @pytest.mark.parametrize("contents", ["not-a-hex-id\n", "path", "A" * 64])
    def test_invalid_cidfile_never_controls_a_container(
        self, tmp_path, monkeypatch, capsys, contents
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        if contents == "path":
            contents = str(tmp_path / "container")
        calls = []

        def runtime(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "run":
                cidfile.write_text(contents)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess_utils, "run", runtime)
        result = run_container_with_cpu_cap_retry(argv, timeout=10)

        assert [cmd[1] for cmd in calls] == ["run"]
        assert sandbox.container_client_failure(result) == (
            "container completion could not be confirmed"
        )
        assert "invalid container id" in capsys.readouterr().err
        assert not cidfile.exists()

    @pytest.mark.parametrize("interrupted", [False, True])
    def test_empty_cidfile_retention_depends_on_client_exit(
        self, tmp_path, monkeypatch, interrupted
    ):
        from kew import subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        controls = []

        def runtime(cmd, **kwargs):
            if cmd[1] != "run":
                controls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            cidfile.touch()
            if interrupted:
                raise KeyboardInterrupt("client interrupted before recording identity")
            return subprocess.CompletedProcess(cmd, 125, "", "runtime failure")

        monkeypatch.setattr(subprocess_utils, "run", runtime)
        with pytest.raises(KeyboardInterrupt) if interrupted else nullcontext():
            run_container_with_cpu_cap_retry(argv, timeout=10)

        assert controls == []
        assert cidfile.exists() is interrupted

    @pytest.mark.parametrize("cid", [None, "a" * 12, "b" * 64])
    def test_absence_checks_only_recorded_id(self, tmp_path, monkeypatch, cid):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        if cid is not None:
            Path(argv[argv.index("--cidfile") + 1]).write_text(f" \n{cid}\n")
        run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        monkeypatch.setattr(subprocess_utils, "run", run)

        assert sandbox._container_absence_confirmed(argv)
        if cid is None:
            run.assert_not_called()
        else:
            assert run.call_args.args[0] == [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"id={cid}",
                "--format",
                "{{.ID}}",
            ]

    @pytest.mark.parametrize("failure", ["nonzero", "oserror", "timeout"])
    def test_confirmed_completion_retains_identity_when_normal_removal_fails(
        self, tmp_path, monkeypatch, capsys, failure
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        completed = subprocess.CompletedProcess(argv, 3, "", "")
        controls = []
        cidfile.write_text("a" * 64)
        monkeypatch.setattr(subprocess_utils, "run_bounded", lambda *a, **k: completed)

        def control(cmd, **kwargs):
            controls.append(cmd)
            assert cidfile.exists()
            assert kwargs["timeout"] == RUNTIME_CONTROL_TIMEOUT_SECONDS
            if cmd[1] == "inspect":
                return subprocess.CompletedProcess(
                    cmd, 0, _completion_inspection(argv, 3), ""
                )
            assert cmd == ["docker", "rm", "a" * 64]
            if failure == "oserror":
                raise OSError("private-runtime-diagnostic")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(
                    cmd, 10, stderr="private-runtime-diagnostic"
                )
            return subprocess.CompletedProcess(cmd, 1, "", "private-runtime-diagnostic")

        monkeypatch.setattr(subprocess_utils, "run", control)
        result = run_container_with_cpu_cap_retry(argv, timeout=10, output_limit=128)

        assert cidfile.exists()
        assert result is completed
        assert sandbox.container_client_failure(result) is None
        assert [cmd[1] for cmd in controls] == ["inspect", "rm"]
        warning = capsys.readouterr().err
        assert name in warning and "cidfile is kept" in warning
        assert "private-runtime-diagnostic" not in warning

    @pytest.mark.parametrize("bounded", [False, True], ids=["run", "bounded"])
    @pytest.mark.parametrize("retry", [False, True], ids=["first", "retry"])
    @pytest.mark.parametrize(
        "state",
        [
            '{"Status":"running","ExitCode":0}',
            '{"Status":"exited","ExitCode":3}',
            '{"Status":"exited"}',
            '{"Status":"exited","ExitCode":false}',
            '{"Status":"exited","ExitCode":"0"}',
            "null",
            "[]",
            "{}",
            "malformed private-runtime-diagnostic",
            OSError("private-runtime-diagnostic"),
            subprocess.TimeoutExpired(
                "inspect", 10, stderr="private-runtime-diagnostic"
            ),
        ],
    )
    def test_unconfirmed_runtime_state_requires_cleanup(
        self, tmp_path, monkeypatch, capsys, bounded, retry, state
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        controls = []
        launches = []

        def launch(cmd, **kwargs):
            launches.append(cmd)
            if retry and len(launches) == 1:
                return subprocess.CompletedProcess(
                    cmd, 125, "", self._CPU_CAP_REJECTION
                )
            cidfile.write_text("a" * 64)
            return subprocess.CompletedProcess(cmd, 0, "project output", "")

        def control(cmd, **kwargs):
            if cmd[1] == "run":
                return launch(cmd, **kwargs)
            controls.append(cmd)
            assert cidfile.exists()
            assert kwargs["timeout"] == RUNTIME_CONTROL_TIMEOUT_SECONDS
            if cmd[1] == "inspect":
                if isinstance(state, BaseException):
                    raise state
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    '{"Name":' + json.dumps("/" + name) + ',"State":' + state + "}",
                    "private-runtime-diagnostic",
                )
            assert cmd == ["docker", "rm", "-f", "a" * 64]
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(sandbox, "_cpu_cap_retry_argv", lambda *a: argv)
        monkeypatch.setattr(subprocess_utils, "run_bounded", launch)
        monkeypatch.setattr(subprocess_utils, "run", control)
        result = run_container_with_cpu_cap_retry(
            argv, timeout=10, output_limit=128 if bounded else None
        )

        assert sandbox.container_client_failure(result) == (
            "container completion could not be confirmed"
        )
        assert result.returncode == 0 and result.stdout == "project output"
        assert [cmd[1] for cmd in controls] == ["inspect", "rm"]
        assert len(launches) == (2 if retry else 1)
        assert all("--rm" not in cmd for cmd in launches)
        assert not cidfile.exists()
        warnings = capsys.readouterr().err
        assert "container completion could not be confirmed" in warnings
        assert "private-runtime-diagnostic" not in warnings

    @pytest.mark.parametrize("bounded", [False, True], ids=["run", "bounded"])
    @pytest.mark.parametrize("retry", [False, True], ids=["first", "retry"])
    @pytest.mark.parametrize(
        "cleanup", ["removed", "absent", "present", "error", "interrupted"]
    )
    def test_signalled_client_cleans_container(
        self, tmp_path, monkeypatch, capsys, bounded, retry, cleanup
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        killed = subprocess.CompletedProcess(argv, -9, "partial output", "")
        launches = []
        controls = []

        def launch(cmd, **kwargs):
            launches.append(cmd)
            if retry and len(launches) == 1:
                return subprocess.CompletedProcess(
                    cmd, 125, "", self._CPU_CAP_REJECTION
                )
            cidfile.write_text("a" * 64, encoding="utf-8")
            return killed

        def control(cmd, **kwargs):
            if cmd[1] == "run":
                return launch(cmd, **kwargs)
            controls.append(cmd)
            assert cidfile.read_text(encoding="utf-8") == "a" * 64
            if cmd[1:3] == ["rm", "-f"]:
                if cleanup == "error":
                    raise OSError("injected removal failure")
                if cleanup == "interrupted":
                    raise KeyboardInterrupt("injected cleanup interruption")
                return subprocess.CompletedProcess(
                    cmd, 0 if cleanup == "removed" else 1, "", ""
                )
            assert cmd[1] == "ps"
            return subprocess.CompletedProcess(
                cmd, 0, "abc123\n" if cleanup == "present" else "", ""
            )

        monkeypatch.setattr(sandbox, "_cpu_cap_retry_argv", lambda *a: argv)
        monkeypatch.setattr(subprocess_utils, "run_bounded", launch)
        monkeypatch.setattr(subprocess_utils, "run", control)
        result = run_container_with_cpu_cap_retry(
            argv, timeout=10, output_limit=128 if bounded else None
        )

        assert result is killed
        assert len(launches) == (2 if retry else 1)
        assert controls[:1] == [["docker", "rm", "-f", "a" * 64]]
        assert len(controls) == (2 if cleanup in {"absent", "present"} else 1)
        kept = cleanup in {"present", "error", "interrupted"}
        assert cidfile.exists() is kept
        warning = capsys.readouterr().err
        if kept:
            assert name in warning
            assert "signal 9" in warning
            assert "cidfile is kept" in warning
            if cleanup == "interrupted":
                assert "injected cleanup interruption" in warning
        else:
            assert not warning

    @pytest.mark.parametrize(
        "runtime,code,stderr",
        [
            ("docker", -15, ""),
            ("docker", 125, "runtime failed"),
            ("podman", 125, "runtime failed"),
            ("docker", 1, "error waiting for container: unexpected EOF"),
            ("docker", 1, "Error response from daemon: connection reset by peer"),
            ("docker", 0, "Cannot connect to the Docker daemon at unix:///socket"),
            ("podman", 1, "Error: unable to connect to Podman socket: EOF"),
            ("docker", None, "client did not report completion"),
        ],
    )
    def test_incomplete_client_exit_requires_removal(
        self, tmp_path, monkeypatch, runtime, code, stderr
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        argv[0] = runtime
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        cidfile.write_text("a" * 64, encoding="utf-8")
        completed = subprocess.CompletedProcess(argv, code, "", stderr)
        monkeypatch.setattr(subprocess_utils, "run_bounded", lambda *a, **k: completed)
        inspection = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, "", "inspect failed")
        )
        monkeypatch.setattr(subprocess_utils, "run", inspection)
        removals = []

        def remove(cmd):
            assert cidfile.exists()
            removals.append(cmd)
            return True

        monkeypatch.setattr(sandbox, "_remove_launched_container", remove)
        result = run_container_with_cpu_cap_retry(argv, timeout=10, output_limit=128)
        assert removals == [argv]
        if code is None or code < 0 or code == 125:
            assert result is completed
            inspection.assert_not_called()
        else:
            assert (result.args, result.returncode, result.stdout, result.stderr) == (
                completed.args,
                code,
                completed.stdout,
                stderr,
            )
            inspection.assert_called_once()
        assert not cidfile.exists()

    def test_cpu_rejection_with_created_container_requires_removal(
        self, tmp_path, monkeypatch
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        cidfile.write_text("a" * 64, encoding="utf-8")
        rejected = subprocess.CompletedProcess(argv, 125, "", self._CPU_CAP_REJECTION)
        monkeypatch.setattr(subprocess_utils, "run_bounded", lambda *a, **k: rejected)
        with patch.object(
            sandbox, "_remove_launched_container", return_value=False
        ) as rm:
            result = run_container_with_cpu_cap_retry(
                argv, timeout=10, output_limit=128
            )
        rm.assert_called_once_with(argv)
        assert result is rejected
        assert cidfile.exists()

    @pytest.mark.parametrize("error_type", [KeyboardInterrupt, OSError, MemoryError])
    @pytest.mark.parametrize(
        "cleanup", ["removed", "absent", "present", "error", "interrupted"]
    )
    @pytest.mark.parametrize("retry", [False, True], ids=["first", "retry"])
    def test_bounded_read_exception_cleans_container(
        self, tmp_path, monkeypatch, capsys, error_type, cleanup, retry
    ):
        from kew import sandbox, subprocess_utils

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]
        failure = error_type("injected bounded pipe read failure")
        proc = MagicMock()
        proc.__enter__.return_value = proc
        proc.poll.return_value = None
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(MagicMock(data="stdout", fd=42), 1)]
        pipe_os = MagicMock()
        pipe_os.read.side_effect = failure
        monkeypatch.setattr(subprocess_utils, "os", pipe_os)
        monkeypatch.setattr(subprocess_utils, "popen_bytes", lambda *a, **kw: proc)
        monkeypatch.setattr(
            subprocess_utils.selectors, "DefaultSelector", lambda: selector
        )
        monkeypatch.setattr(sandbox.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(sandbox, "_cpu_cap_retry_argv", lambda *a: argv)
        bounded = subprocess_utils.run_bounded
        launches = []
        controls = []

        def launch(cmd, **kwargs):
            launches.append(cmd)
            if retry and len(launches) == 1:
                return subprocess.CompletedProcess(
                    cmd, 125, "", self._CPU_CAP_REJECTION
                )
            cidfile.write_text("a" * 64, encoding="utf-8")
            return bounded(cmd, **kwargs)

        def control(cmd, **kwargs):
            controls.append(cmd)
            assert cidfile.read_text(encoding="utf-8") == "a" * 64
            if cmd[1:3] == ["rm", "-f"]:
                if cleanup == "error":
                    raise OSError("injected removal failure")
                if cleanup == "interrupted":
                    raise KeyboardInterrupt("injected cleanup interruption")
                return subprocess.CompletedProcess(
                    cmd, 0 if cleanup == "removed" else 1, "", ""
                )
            assert cmd[1] == "ps"
            return subprocess.CompletedProcess(
                cmd, 0, "abc123\n" if cleanup == "present" else "", ""
            )

        monkeypatch.setattr(subprocess_utils, "run_bounded", launch)
        monkeypatch.setattr(subprocess_utils, "run", control)
        with pytest.raises(error_type) as raised:
            run_container_with_cpu_cap_retry(argv, timeout=10, output_limit=128)

        assert raised.value is failure
        pipe_os.read.assert_called_once_with(42, 129)
        proc.kill.assert_called_once_with()
        assert len(launches) == (2 if retry else 1)
        assert controls[:1] == [["docker", "rm", "-f", "a" * 64]]
        assert len(controls) == (2 if cleanup in {"absent", "present"} else 1)
        kept = cleanup in {"present", "error", "interrupted"}
        assert cidfile.exists() is kept
        warning = capsys.readouterr().err
        if kept:
            assert name in warning
            assert error_type.__name__ in warning
            assert str(failure) in warning
            assert "cidfile is kept" in warning
            if cleanup == "interrupted":
                assert "injected cleanup interruption" in warning
        else:
            assert not warning

    @pytest.mark.parametrize("removed", [True, False])
    def test_bounded_launch_shares_deadline_and_records_overflow_cleanup(
        self, tmp_path, monkeypatch, removed
    ):
        import inspect

        from kew import sandbox, subprocess_utils

        assert (
            "output_limit"
            in inspect.signature(run_container_with_cpu_cap_retry).parameters
        ), "container verification must support bounded capture"
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        deadlines = []
        accepted = subprocess.CompletedProcess(argv, 0, "normal\n", "stderr\n")

        def capture(cmd, *, deadline, output_limit, **kwargs):
            deadlines.append(deadline)
            assert output_limit == 128
            if len(deadlines) == 1:
                return subprocess.CompletedProcess(
                    cmd, 125, "", self._CPU_CAP_REJECTION
                )
            cidfile.write_text("a" * 64, encoding="utf-8")
            return accepted

        monkeypatch.setattr(subprocess_utils, "run_bounded", capture)
        monkeypatch.setattr(sandbox, "_cpu_cap_retry_argv", lambda *a: argv)
        monkeypatch.setattr(
            subprocess_utils,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd,
                0,
                _completion_inspection(argv, 0) if cmd[1] == "inspect" else "",
                "",
            ),
        )
        with patch("kew.sandbox._remove_launched_container") as normal_cleanup:
            result = run_container_with_cpu_cap_retry(
                argv, timeout=10, output_limit=128
            )
        normal_cleanup.assert_not_called()
        assert result is accepted
        assert not cidfile.exists()
        assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
        buffers = {
            name: subprocess_utils.BoundedOutput(128) for name in ("stdout", "stderr")
        }
        buffers["stdout"].append(b"X" * 129)
        overflow = subprocess_utils.OutputBudgetExceeded("stdout", buffers)

        def flood(*args, **kwargs):
            cidfile.write_text("a" * 64)
            raise overflow

        def remove(cmd):
            assert cidfile.exists()
            return removed

        monkeypatch.setattr(subprocess_utils, "run_bounded", flood)
        monkeypatch.setattr(sandbox, "_remove_launched_container", remove)
        with pytest.raises(subprocess_utils.OutputBudgetExceeded):
            run_container_with_cpu_cap_retry(argv, timeout=10, output_limit=128)
        assert overflow.cleanup_failed is not removed
        assert cidfile.exists() is not removed

    @pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
    def test_every_exit_cleans_the_cidfile(self, tmp_path: Path, outcome: str) -> None:
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])

        def launch(cmd, *args, **kwargs):
            if cmd[1] in ("rm", "ps"):
                # Normal and timeout cleanup both confirm removal.
                assert ("-f" in cmd) is (outcome == "timeout")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[1] == "inspect":
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    _completion_inspection(argv, 0 if outcome == "success" else 42),
                    "",
                )
            cidfile.write_text("a" * 64, encoding="utf-8")
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, 300)
            return subprocess.CompletedProcess(
                argv,
                0 if outcome == "success" else 42,
                "",
                "",
            )

        with patch("kew.sandbox.subprocess_utils.run", side_effect=launch) as run:
            if outcome == "timeout":
                with pytest.raises(subprocess.TimeoutExpired):
                    run_container_with_cpu_cap_retry(argv, timeout=300)
            else:
                result = run_container_with_cpu_cap_retry(argv, timeout=300)
                assert result.returncode == (0 if outcome == "success" else 42)

        assert not cidfile.exists()
        assert run.call_count == (2 if outcome == "timeout" else 3)

    _CPU_CAP_REJECTION = (
        "docker: Error response from daemon: Range of CPUs is from "
        "0.01 to 2.00, as there are only 2 CPUs available.\n"
    )

    def test_timeout_removes_the_container_before_the_cidfile(
        self, tmp_path: Path
    ) -> None:
        # Killing the docker client leaves the container running under its cap
        # and holding the name; the retry helper must remove it, and must do so
        # while the cidfile still records that it was created.
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        removals: list[tuple[list[str], bool]] = []

        def launch(cmd, *args, **kwargs):
            if cmd[1:3] == ["rm", "-f"]:
                removals.append((cmd, cidfile.exists()))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            cidfile.write_text("a" * 64, encoding="utf-8")
            raise subprocess.TimeoutExpired(cmd, 300)

        with (
            patch("kew.sandbox.subprocess_utils.run", side_effect=launch),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            run_container_with_cpu_cap_retry(argv, timeout=300)

        assert removals == [(["docker", "rm", "-f", "a" * 64], True)]
        assert not cidfile.exists()

    def test_unconfirmed_removal_keeps_the_cidfile_and_says_so(
        self, tmp_path: Path, capsys
    ) -> None:
        # rm -f fails AND the daemon still lists the ID: the container may
        # be running under its cap, so its cidfile stays as the only handle.
        from kew.sandbox import ContainerCleanupFailed

        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        name = argv[argv.index("--name") + 1]

        def launch(cmd, *args, **kwargs):
            if cmd[1:3] == ["rm", "-f"]:
                return subprocess.CompletedProcess(cmd, 1, "", "daemon unavailable")
            if cmd[1] == "ps":
                return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
            cidfile.write_text("a" * 64, encoding="utf-8")
            raise subprocess.TimeoutExpired(cmd, 300)

        with (
            patch("kew.sandbox.subprocess_utils.run", side_effect=launch),
            pytest.raises(ContainerCleanupFailed),
        ):
            run_container_with_cpu_cap_retry(argv, timeout=300)

        assert cidfile.exists()
        assert name in capsys.readouterr().err

    def test_failed_removal_of_an_already_absent_container_is_clean(
        self, tmp_path: Path
    ) -> None:
        # rm -f returns 1 ("no such container") but the daemon lists nothing:
        # that is a stable absence, and the cidfile can go.
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        cidfile = Path(argv[argv.index("--cidfile") + 1])

        def launch(cmd, *args, **kwargs):
            if cmd[1:3] == ["rm", "-f"]:
                return subprocess.CompletedProcess(cmd, 1, "", "No such container")
            if cmd[1] == "ps":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            cidfile.write_text("a" * 64, encoding="utf-8")
            raise subprocess.TimeoutExpired(cmd, 300)

        with (
            patch("kew.sandbox.subprocess_utils.run", side_effect=launch),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            run_container_with_cpu_cap_retry(argv, timeout=300)

        assert not cidfile.exists()

    def test_both_attempts_share_one_deadline(self, tmp_path: Path) -> None:
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        rejected = subprocess.CompletedProcess(argv, 125, "", self._CPU_CAP_REJECTION)
        accepted = subprocess.CompletedProcess(argv, 0, "", "")
        Path(argv[argv.index("--cidfile") + 1]).write_text("a" * 64)
        clock = iter([1000.0, 1000.0, 1200.0])
        retry_argv = [*argv]
        retry_argv[argv.index("--cpus") + 1] = "1"

        with (
            patch("kew.sandbox.time.monotonic", side_effect=lambda: next(clock)),
            patch(
                "kew.sandbox.subprocess_utils.run",
                side_effect=[
                    rejected,
                    accepted,
                    subprocess.CompletedProcess(
                        [], 0, _completion_inspection(argv, 0), ""
                    ),
                    subprocess.CompletedProcess([], 0, "", ""),
                ],
            ) as run,
            patch("kew.sandbox._cpu_cap_retry_argv", return_value=retry_argv),
        ):
            result = run_container_with_cpu_cap_retry(argv, timeout=300)

        assert result.returncode == 0
        timeouts = [call.kwargs["timeout"] for call in run.call_args_list]
        assert timeouts == [
            pytest.approx(300.0),
            pytest.approx(100.0),
            RUNTIME_CONTROL_TIMEOUT_SECONDS,
            RUNTIME_CONTROL_TIMEOUT_SECONDS,
        ]

    def test_a_retry_with_no_budget_left_times_out_without_launching(
        self, tmp_path: Path
    ) -> None:
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        rejected = subprocess.CompletedProcess(argv, 125, "", self._CPU_CAP_REJECTION)
        clock = iter([1000.0, 1000.0, 1400.0])

        with (
            patch("kew.sandbox.time.monotonic", side_effect=lambda: next(clock)),
            patch("kew.sandbox.subprocess_utils.run", return_value=rejected) as run,
            patch("kew.sandbox._cpu_cap_retry_argv", return_value=[*argv]),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            run_container_with_cpu_cap_retry(argv, timeout=300)

        assert run.call_count == 1

    def test_two_cpu_cap_refusals_launch_twice_and_fail(self, tmp_path: Path) -> None:
        # The extraction path's own retry: rejection, absence check, rejection.
        argv = _extract_argv(tmp_path, tmp_path / "artifact")
        rejected = subprocess.CompletedProcess(argv, 125, "", self._CPU_CAP_REJECTION)
        no_container = subprocess.CompletedProcess([], 0, "", "")
        launches: list[list[str]] = []

        def run_side_effect(cmd, *args, **kwargs):
            if cmd[1] == "ps":
                return no_container
            launches.append(cmd)
            return rejected

        with (
            patch("kew.sandbox.subprocess_utils.run", side_effect=run_side_effect),
            patch("kew.sandbox._daemon_cpu_count", return_value=2),
        ):
            result = run_container_with_cpu_cap_retry(argv, timeout=300)

        assert result.returncode == 125
        assert len(launches) == 2
        assert _flag_values(launches[1], "--cpus") == ["1"]


class TestContainerHandle:
    _CPU_CAP_REJECTION = (
        "docker: Error response from daemon: Range of CPUs is from "
        "0.01 to 2.00, as there are only 2 CPUs available.\n"
    )

    # The launcher passes the process environment through to the container, so
    # a failed ``popen.assert_called_once_with(argv, env=ANY, ...)`` prints
    # whatever this process happens to hold (#2197). Every test in this class
    # launches against a fixed, minimal environment instead of the host's, so
    # the printed call can only ever contain the four values below.
    # OPENROUTER_API_KEY is one of them on purpose: the assertions that the
    # inference credential is stripped from the launch environment are only
    # meaningful when the launcher had one to strip.
    _FIXTURE_ENV = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/kew-test-home",
        "LANG": "C.UTF-8",
        "OPENROUTER_API_KEY": "fixture-inference-credential",
    }

    @pytest.fixture(autouse=True)
    def _fixed_launch_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Launch against fixture values, never the operator's environment."""
        monkeypatch.setattr(os, "environ", dict(self._FIXTURE_ENV))

    @staticmethod
    def _launch_proc(lines: list[str], exit_code: int) -> MagicMock:
        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = _FakeStdout(lines)
        proc.returncode = exit_code
        proc.poll.return_value = exit_code
        proc.wait.return_value = exit_code
        return proc

    def test_attempted_cap_above_advertised_maximum_retries_once_at_one_cpu(
        self, tmp_path: Path, capsys
    ):
        argv = _agent_argv(tmp_path)
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        rejected = self._launch_proc(
            [self._CPU_CAP_REJECTION],
            125,
        )
        accepted = self._launch_proc(["agent output\n"], 0)
        no_container = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch(
                "kew.sandbox.subprocess_utils.popen",
                side_effect=[rejected, accepted],
            ) as popen,
            patch("kew.sandbox.subprocess_utils.run", return_value=no_container) as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=2) as reprobe,
        ):
            handle = launch_container(argv)
            # The refusal is the runtime's, for a launch that created nothing:
            # it reaches the operator, never the container's output stream.
            assert list(handle) == ["agent output\n"]
            assert handle.wait() == 0
        assert self._CPU_CAP_REJECTION.strip() in capsys.readouterr().err

        assert not cidfile.exists(), "the refused launch must not create a container"
        assert popen.call_count == 2
        assert _flag_values(popen.call_args_list[0].args[0], "--cpus") == ["4"]
        retry_argv = popen.call_args_list[1].args[0]
        assert _flag_values(retry_argv, "--cpus") == ["1"]
        reprobe.assert_called_once_with("docker")
        run.assert_not_called()

    def test_attempted_cap_at_advertised_maximum_does_not_retry(self, tmp_path: Path):
        argv = _agent_argv(tmp_path)
        argv[argv.index("--cpus") + 1] = "2"
        rejected = self._launch_proc([self._CPU_CAP_REJECTION], 125)

        with (
            patch("kew.sandbox.subprocess_utils.popen", return_value=rejected) as popen,
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            handle = launch_container(argv)
            assert list(handle) == [self._CPU_CAP_REJECTION]
            assert handle.wait() == 125

        popen.assert_called_once_with(
            argv, env=ANY, credential=None, stderr=subprocess.STDOUT
        )
        assert "OPENROUTER_API_KEY" not in popen.call_args.kwargs["env"]
        run.assert_not_called()
        reprobe.assert_not_called()

    @pytest.mark.parametrize(
        ("attempted", "maximum", "should_retry"),
        [
            ("1.5", 1, True),
            ("1.5", 2, False),
            ("2.0000000000000000000000000001", 2, True),
            ("not-a-number", 2, False),
            ("4", 1, True),
            ("1", 1, False),
        ],
        ids=[
            "fraction-above",
            "fraction-below",
            "decimal-epsilon-above",
            "nonnumeric",
            "four-above-one",
            "one-at-one",
        ],
    )
    def test_retry_requires_a_numeric_cap_above_the_advertised_maximum(
        self,
        tmp_path: Path,
        attempted: str,
        maximum: int,
        should_retry: bool,
    ):
        argv = _agent_argv(tmp_path)
        argv[argv.index("--cpus") + 1] = attempted
        rejection = (
            "docker: Error response from daemon: Range of CPUs is from "
            f"0.01 to {maximum}.00, as there are only {maximum} CPUs available.\n"
        )
        rejected = self._launch_proc([rejection], 125)
        accepted = self._launch_proc([], 0)
        no_container = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch(
                "kew.sandbox.subprocess_utils.popen",
                side_effect=[rejected, accepted],
            ) as popen,
            patch("kew.sandbox.subprocess_utils.run", return_value=no_container) as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=maximum) as reprobe,
        ):
            handle = launch_container(argv)
            assert list(handle) == ([] if should_retry else [rejection])
            assert handle.wait() == (0 if should_retry else 125)

        assert popen.call_count == (2 if should_retry else 1)
        run.assert_not_called()
        assert reprobe.call_count == (1 if should_retry else 0)
        if should_retry:
            assert _flag_values(popen.call_args_list[1].args[0], "--cpus") == ["1"]

    @staticmethod
    def _cpu_cap_rejection(maximum: int) -> str:
        return (
            "docker: Error response from daemon: Range of CPUs is from "
            f"0.01 to {maximum}.00, as there are only {maximum} CPUs available.\n"
        )

    def test_a_flood_of_refusals_holds_back_only_the_last_one(self, tmp_path: Path):
        # Only the latest refusal can drive the retry, so only it is held: a
        # flood of refusal-shaped lines must not accumulate in host memory.
        argv = _agent_argv(tmp_path)
        argv[argv.index("--cpus") + 1] = "1.5"
        rejections = [self._cpu_cap_rejection(maximum) for maximum in (4, 3, 2)]
        rejected = self._launch_proc(rejections, 125)

        with (
            patch("kew.sandbox.subprocess_utils.popen", return_value=rejected) as popen,
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            handle = launch_container(argv)
            # No retry launches (1.5 is within the advertised 2), so the held
            # refusal is the launch's failure — the last one, alone.
            assert list(handle) == [rejections[-1]]
            assert handle.wait() == 125

        popen.assert_called_once_with(
            argv, env=ANY, credential=None, stderr=subprocess.STDOUT
        )
        run.assert_not_called()
        reprobe.assert_not_called()

    def test_a_retried_attempt_reports_only_the_refusal_that_drove_it(
        self, tmp_path: Path, capsys
    ):
        argv = _agent_argv(tmp_path)
        argv[argv.index("--cpus") + 1] = "4"
        rejections = [self._cpu_cap_rejection(maximum) for maximum in (3, 2)]
        rejected = self._launch_proc(rejections, 125)
        accepted = self._launch_proc(["agent output\n"], 0)
        no_container = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch(
                "kew.sandbox.subprocess_utils.popen", side_effect=[rejected, accepted]
            ) as popen,
            patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
            patch("kew.sandbox._daemon_cpu_count", return_value=2),
        ):
            handle = launch_container(argv)
            assert list(handle) == ["agent output\n"]
            assert handle.wait() == 0

        assert popen.call_count == 2
        stderr = capsys.readouterr().err
        assert stderr.count("runtime refused the launch") == 1
        assert rejections[-1].strip() in stderr
        assert rejections[0].strip() not in stderr

    def test_a_second_cpu_cap_rejection_is_not_retried(self, tmp_path: Path):
        argv = _agent_argv(tmp_path)
        rejection = self._CPU_CAP_REJECTION
        first = self._launch_proc([rejection], 125)
        second = self._launch_proc([rejection], 125)
        no_container = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch(
                "kew.sandbox.subprocess_utils.popen", side_effect=[first, second]
            ) as popen,
            patch("kew.sandbox.subprocess_utils.run", return_value=no_container) as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=2) as reprobe,
        ):
            handle = launch_container(argv)
            # The retried refusal goes to the operator; the final one is the
            # launch's failure and stays in the stream.
            assert list(handle) == [rejection]
            assert handle.wait() == 125

        assert popen.call_count == 2
        run.assert_not_called()
        assert reprobe.call_count == 1

    @pytest.mark.parametrize(
        ("line", "exit_code"),
        [
            ("failure at /tmp/range of CPUs is from/cache\n", 1),
            ("docker: Error response from daemon: unrelated refusal\n", 125),
            (_CPU_CAP_REJECTION, 1),
            (
                "docker: Error response from daemon: Range of CPUs is from "
                "0..01 to 2.00, as there are only 2 CPUs available.\n",
                125,
            ),
            (
                "docker: Error response from daemon: Range of CPUs is from "
                "0.01 to 3.00, as there are only 2 CPUs available.\n",
                125,
            ),
        ],
        ids=[
            "bare-substring",
            "unrelated-status-125",
            "wrong-exit-status",
            "invalid-number",
            "inconsistent-range",
        ],
    )
    def test_noncanonical_cpu_refusals_are_not_retried(
        self, tmp_path: Path, line: str, exit_code: int
    ):
        argv = _agent_argv(tmp_path)
        rejected = self._launch_proc([line], exit_code)

        with (
            patch("kew.sandbox.subprocess_utils.popen", return_value=rejected) as popen,
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            handle = launch_container(argv)
            assert list(handle) == [line]
            assert handle.wait() == exit_code

        popen.assert_called_once_with(
            argv, env=ANY, credential=None, stderr=subprocess.STDOUT
        )
        assert "OPENROUTER_API_KEY" not in popen.call_args.kwargs["env"]
        run.assert_not_called()
        reprobe.assert_not_called()

    def test_cpu_cap_rejection_does_not_retry_when_a_cidfile_was_created(
        self, tmp_path: Path
    ):
        argv = _agent_argv(tmp_path)
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        rejection = self._CPU_CAP_REJECTION
        rejected = self._launch_proc([rejection], 125)

        with (
            patch(
                "kew.sandbox.ContainerHandle.observe_workload_start", return_value=False
            ),
            patch("kew.sandbox.subprocess_utils.popen", return_value=rejected) as popen,
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            handle = launch_container(argv)
            cidfile.write_text("a" * 64, encoding="utf-8")
            assert list(handle) == [rejection]
            assert handle.wait() == 125

        popen.assert_called_once_with(
            argv, env=ANY, credential=None, stderr=subprocess.STDOUT
        )
        assert "OPENROUTER_API_KEY" not in popen.call_args.kwargs["env"]
        run.assert_not_called()
        reprobe.assert_not_called()

    def test_a_handle_without_a_retry_launcher_yields_the_refusal(self, tmp_path):
        # The orchestrator's contained path (ContainedExecution.handle_for)
        # retries on its own, from the refusal it reads in the stream.
        # In place, not held back to the end of the attempt: the refusal keeps
        # its position ahead of any line the client prints after it.
        rejected = self._launch_proc([self._CPU_CAP_REJECTION, "after\n"], 125)
        handle = ContainerHandle(rejected, "kew-42", launch_argv=_agent_argv(tmp_path))
        assert list(handle) == [self._CPU_CAP_REJECTION, "after\n"]

    def test_cpu_cap_retry_does_not_inspect_another_launch_by_name(self, tmp_path):
        argv = _agent_argv(tmp_path)
        rejected = self._launch_proc([self._CPU_CAP_REJECTION], 125)
        accepted = self._launch_proc(["agent output\n"], 0)
        with (
            patch(
                "kew.sandbox.subprocess_utils.popen", side_effect=[rejected, accepted]
            ) as popen,
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=2) as reprobe,
        ):
            handle = launch_container(argv)
            assert list(handle) == ["agent output\n"]
            assert handle.wait() == 0
        run.assert_not_called()
        assert popen.call_count == 2
        for launch in popen.call_args_list:
            assert "OPENROUTER_API_KEY" not in launch.kwargs["env"]
        reprobe.assert_called_once_with("docker")

    def test_container_stdout_streams_to_parser(self):
        lines = [
            '{"type":"assistant","message":{}}\n',
            '{"type":"result","subtype":"success"}\n',
        ]
        fake_proc = MagicMock()
        fake_proc.stdout = _FakeStdout(lines)

        with patch("kew.subprocess_utils.popen", return_value=fake_proc):
            argv = ["docker", "run", "--rm", "--name", "kew-42", "kew-agent:latest"]
            handle = launch_container(argv)
            result = list(handle)

        assert result == lines

    def test_container_kill_issues_docker_kill(self):
        fake_proc = MagicMock()
        handle = ContainerHandle(fake_proc, "kew-42", container_id=RECORDED_ID)

        with patch("kew.subprocess_utils.run") as mock_run:
            accepted = handle.kill()

        assert accepted is KillStatus.REFUSED
        mock_run.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )

    def test_container_kill_resolves_an_absolute_runtime_through_the_allowlist(
        self, monkeypatch
    ):
        # detect_runtime() returns a which()-resolved ABSOLUTE path and the
        # allowlist resolves by tool NAME: a kill argv carrying the absolute
        # path raises "Unknown executable" and the container runs on. Found
        # live, so pinned through the REAL resolver rather than a bare mock.
        from kew import subprocess_utils

        monkeypatch.setattr(
            subprocess_utils, "TOOL_PATHS", {"docker": "/opt/bin/docker"}
        )
        handle = ContainerHandle(
            MagicMock(),
            "kew-abs",
            runtime="/usr/local/bin/docker",
            container_id=RECORDED_ID,
        )
        with patch("kew.subprocess_utils.subprocess.run") as real_run:
            handle.kill()
        argv = real_run.call_args.args[0]
        assert argv == ["/opt/bin/docker", "kill", RECORDED_ID]

    def test_a_kill_the_runtime_refused_does_not_latch(self):
        # "No such container" at t=0 (docker run has not created it yet)
        # must leave the next attempt open, or the container runs unwatched.
        handle = ContainerHandle(MagicMock(), "kew-early", container_id=RECORDED_ID)
        refused = MagicMock(returncode=1, stderr="No such container: kew-early")
        accepted = MagicMock(returncode=0)
        with patch("kew.subprocess_utils.run", side_effect=[refused, accepted]) as run:
            assert handle.kill() is KillStatus.REFUSED
            assert not handle._killed
            assert handle.kill() is KillStatus.ACCEPTED
            assert handle._killed
            assert handle.kill() is KillStatus.REFUSED  # latched: no third call
        assert run.call_count == 2

    def test_a_refused_breach_keeps_its_observation_without_latching(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        handle = ContainerHandle(proc, "kew-refused", container_id=RECORDED_ID)
        breach = ResourceCapObservation(
            ResourceCapCode.CHECKOUT_BYTES,
            "checkout exceeded 1 byte",
        )
        refused = MagicMock(returncode=1, stderr="No such container: kew-refused")

        with patch("kew.subprocess_utils.run", return_value=refused):
            handle._breach(breach)

        assert handle.resource_cap_reason is None
        assert handle.resource_cap_code is None
        assert handle.resource_cap_observations == (breach,)
        assert handle._pending_breach is None

    def test_a_breach_on_a_handle_that_can_never_record_an_id_stops_reaping(
        self, tmp_path, monkeypatch
    ):
        """No cidfile and no id: no kill can ever be sent, so the reaper stops
        at once instead of polling a client that never exits (#3375). The
        suite's no-op ``time.sleep`` once turned that loop into a spin that
        filled the verify host's memory."""
        from support.fake_docker import LaunchDocker

        from kew import subprocess_utils

        fake = LaunchDocker(tmp_path / "runtime")
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(fake.path))
        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(proc, "kew-unrecorded", watchdog_interval_seconds=0.01)
        before = set(threading.enumerate())

        def started() -> list[threading.Thread]:
            return [
                t for t in threading.enumerate() if t not in before and t.is_alive()
            ]

        try:
            handle._breach(
                ResourceCapObservation(
                    ResourceCapCode.CHECKOUT_BYTES, "checkout exceeded 1 bytes"
                )
            )
            deadline = time.monotonic() + 2.0
            while started() and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            assert started() == []
            polls = proc.poll.call_count
            threading.Event().wait(0.1)
            assert proc.poll.call_count == polls
            assert fake.calls("kill") == []
            assert not handle.kill_accepted
        finally:
            # A reaper still running must not outlive the test.
            proc.poll.return_value = 0

    # Real wall clock. Serial phase: see tests/conftest.py.
    @pytest.mark.serial
    def test_a_breach_before_the_container_exists_is_reaped_later(self):
        # The reaper re-issues the kill every interval until the runtime
        # accepts it or the client process exits.
        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc, "kew-late", watchdog_interval_seconds=0.01, container_id=RECORDED_ID
        )
        refused = MagicMock(returncode=1)
        accepted = MagicMock(returncode=0)
        with patch(
            "kew.subprocess_utils.run", side_effect=[refused, refused, accepted]
        ) as run:
            handle._breach(
                ResourceCapObservation(
                    ResourceCapCode.CHECKOUT_BYTES,
                    "checkout exceeded 1 bytes",
                )
            )
            deadline = time.monotonic() + 1.0
            while not handle._killed and time.monotonic() < deadline:
                time.sleep(0.005)
        assert handle._killed
        assert run.call_count == 3
        assert handle.resource_cap_reason == "kew-late: checkout exceeded 1 bytes"

    def test_first_breach_reason_wins(self):
        handle = ContainerHandle(MagicMock(), "kew-first", container_id=RECORDED_ID)
        accepted = MagicMock(returncode=0)
        with patch("kew.subprocess_utils.run", return_value=accepted):
            handle._breach(
                ResourceCapObservation(
                    ResourceCapCode.CHECKOUT_BYTES,
                    "checkout exceeded 1 bytes",
                )
            )
            handle._breach(
                ResourceCapObservation(
                    ResourceCapCode.HOST_FREE_SPACE,
                    "host free space fell below floor",
                )
            )
        assert handle.resource_cap_reason == "kew-first: checkout exceeded 1 bytes"

    def test_concurrent_breaches_latch_the_cause_whose_kill_was_accepted(self):
        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc,
            "kew-competing",
            watchdog_interval_seconds=0.2,
        )
        checkout = ResourceCapObservation(
            ResourceCapCode.CHECKOUT_ENTRIES,
            "checkout exceeded the entry quota",
        )
        free_space = ResourceCapObservation(
            ResourceCapCode.HOST_FREE_SPACE,
            "host free space fell below the floor",
        )
        first_kill_started = threading.Event()
        accepted_bit_set = threading.Event()
        first_breach_finished = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        def competing_kill() -> KillStatus:
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count
            if current_call == 1:
                first_kill_started.set()
                assert accepted_bit_set.wait(timeout=1.0)
                return KillStatus.REFUSED
            handle._killed = True
            accepted_bit_set.set()
            assert first_breach_finished.wait(timeout=1.0)
            return KillStatus.ACCEPTED

        def first_breach() -> None:
            handle._breach(checkout)
            first_breach_finished.set()

        first = threading.Thread(target=first_breach)
        second = threading.Thread(target=handle._breach, args=(free_space,))
        with patch.object(handle, "kill", side_effect=competing_kill):
            first.start()
            assert first_kill_started.wait(timeout=1.0)
            second.start()
            first.join(timeout=1.0)
            second.join(timeout=1.0)
            proc.poll.return_value = 0
            if handle._reaper_thread is not None:
                handle._reaper_thread.join(timeout=1.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert call_count == 2
        assert handle.resource_cap_code is ResourceCapCode.HOST_FREE_SPACE
        assert handle.resource_cap_reason == (
            "kew-competing: host free space fell below the floor"
        )

    def test_an_unknown_kill_waits_for_daemon_evidence_before_finalization(
        self, tmp_path: Path
    ):
        from kew.sandbox import finalize_container_hard_stop

        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc,
            "kew-wedged",
            watchdog_interval_seconds=1.0,
            cidfile=_valid_cidfile(tmp_path, "wedged"),
        )
        breach = ResourceCapObservation(
            ResourceCapCode.HOST_FREE_SPACE,
            "host free space fell below the floor",
        )
        kill_started = threading.Event()
        release_kill = threading.Event()

        def runtime_answer(argv, **kwargs):
            assert kwargs == {
                "check": False,
                "timeout": RUNTIME_CONTROL_TIMEOUT_SECONDS,
            }
            if argv[1] == "kill":
                assert argv == ["docker", "kill", "a" * 64]
                kill_started.set()
                assert release_kill.wait(timeout=1.0)
                proc.poll.return_value = 137
                raise subprocess.TimeoutExpired(
                    cmd=argv,
                    timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
                )
            assert argv[:2] == ["docker", "events"]
            assert f"container={'a' * 64}" in argv
            assert "event=kill" in argv
            return MagicMock(returncode=0, stdout="container kill current-id\n")

        breach_thread = threading.Thread(target=handle._breach, args=(breach,))
        result = DispatchResult(
            exit_code=137,
            issue_num=1767,
            log_file=tmp_path / "wedged.log",
        )
        finalized = threading.Event()

        def finalize() -> None:
            finalize_container_hard_stop(
                handle,
                result,
                returncode=137,
                timeout_seconds=60,
            )
            finalized.set()

        with (
            patch("kew.subprocess_utils.run", side_effect=runtime_answer) as runtime,
            patch.object(handle, "note_oom_kill", return_value=None),
        ):
            breach_thread.start()
            assert kill_started.wait(timeout=1.0)
            finalizer = threading.Thread(target=finalize)
            finalizer.start()
            assert not finalized.wait(timeout=0.05), (
                "finalization ignored the in-flight kill transition"
            )
            assert handle.resource_cap_reason is None
            release_kill.set()
            breach_thread.join(timeout=1.0)
            finalizer.join(timeout=1.0)

        assert not breach_thread.is_alive()
        assert not finalizer.is_alive()
        _assert_finalized_breach(result, handle, breach)
        assert runtime.call_count == 2

    def test_enforcement_owners_keep_their_own_deadlines(self, tmp_path: Path):
        from kew.sandbox import finalize_container_hard_stop

        clock = {"now": 0.0}
        handle = ContainerHandle(MagicMock(), "kew-owners", now=lambda: clock["now"])
        handle._guard(wall_clock_seconds=120)
        clock["now"] = 50.0
        handle._guard(wall_clock_seconds=60)
        clock["now"] = 61.0
        result = DispatchResult(
            exit_code=137,
            issue_num=1767,
            log_file=tmp_path / "owners.log",
        )

        with patch.object(handle, "note_oom_kill", return_value=None):
            finalize_container_hard_stop(
                handle,
                result,
                returncode=137,
                timeout_seconds=60,
            )

        assert result.hard_stop == "hard_kill"
        assert result.enforced_duration_seconds == 11.0
        assert result.enforced_timeout_seconds == 60

        clock["now"] = 0.0
        single = ContainerHandle(
            MagicMock(),
            "kew-single-owner",
            now=lambda: clock["now"],
        )
        single._guard(wall_clock_seconds=60)
        clock["now"] = 61.0
        single_result = DispatchResult(
            exit_code=137,
            issue_num=1767,
            log_file=tmp_path / "single-owner.log",
        )
        with patch.object(single, "note_oom_kill", return_value=None):
            finalize_container_hard_stop(
                single,
                single_result,
                returncode=137,
                timeout_seconds=60,
            )

        assert single_result.enforced_duration_seconds == 61.0
        assert single_result.enforced_timeout_seconds == 60
        assert single_result.hard_stop == "wall_clock"

    # Real wall clock. Serial phase: see tests/conftest.py.
    @pytest.mark.serial
    def test_thread_watchdog_re_breaches_until_killed(self):
        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(proc, "kew-repeat", watchdog_interval_seconds=0.001)
        guard = MagicMock()
        guard.timed_breach.return_value = "checkout exceeded 1 bytes"
        calls = 0

        def breach(reason: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                handle._killed = True

        with (
            patch.object(handle, "_guard", return_value=guard),
            patch.object(handle, "_breach", side_effect=breach) as breached,
        ):
            handle.start_watchdog()
            assert handle._watchdog_thread is not None
            handle._watchdog_thread.join(timeout=1)
        assert breached.call_count == 3

    def test_container_kill_uses_configured_runtime(self):
        # launch_container threads the runtime (argv[0]) into the handle, so a
        # non-docker runtime (OrbStack/podman) kills with the right binary.
        handle = ContainerHandle(
            MagicMock(), "kew-7", runtime="podman", container_id=RECORDED_ID
        )
        with patch("kew.subprocess_utils.run") as mock_run:
            handle.kill()
        mock_run.assert_called_once_with(
            ["podman", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )

    def test_prepared_worker_stop_uses_its_exact_id(self):
        worker_id = "a" * 64
        handle = ContainerHandle(
            MagicMock(),
            "kew-switchyard-worker",
            container_id=worker_id,
        )
        with patch(
            "kew.subprocess_utils.run",
            return_value=MagicMock(returncode=0),
        ) as runtime:
            assert handle.kill() is KillStatus.ACCEPTED
            assert handle.remove() is True

        assert runtime.call_args_list == [
            call(
                ["docker", "kill", worker_id],
                check=False,
                timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            ),
            call(
                ["docker", "rm", "-f", worker_id],
                check=False,
                timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
            ),
        ]

    def test_container_nonzero_exit_propagated(self):
        fake_proc = MagicMock()
        fake_proc.wait.return_value = 2
        handle = ContainerHandle(fake_proc, "kew-99")

        assert handle.wait() == 2

    # ── OOM classification (#1767) ────────────────────────────────────────
    # 137 is the exit code of a cgroup OOM, of the wall-clock `timeout -k`
    # and of the watchdog's own `docker kill` alike. `--rm` has removed the
    # container before `docker inspect` could read State.OOMKilled, so the
    # daemon's event stream is the only surviving witness.

    def test_an_oom_event_names_the_memory_cap_as_the_kill_reason(self, tmp_path):
        cid = "a" * 64
        cidfile = tmp_path / "container.cid"
        cidfile.write_text(cid, encoding="utf-8")
        events = MagicMock(
            returncode=0,
            stdout=(
                "2026-08-28T19:05:00.000000000Z container oom "
                "3f1c9a (image=kew-agent:latest, name=kew-oom)\n"
            ),
        )
        with (
            patch("kew.sandbox.time.time", side_effect=[100.0, 101.0]),
            patch("kew.subprocess_utils.run", return_value=events) as run,
        ):
            handle = ContainerHandle(MagicMock(), "kew-oom", cidfile=cidfile)
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.SEEN
        expected = OOM_CAP_REASON_TEMPLATE.format(limit="8g")
        assert handle.resource_cap_reason == f"kew-oom: {expected}"
        _assert_contains_all(
            handle.resource_cap_reason, "OOM-killed", "memory cap (8g)"
        )
        argv = run.call_args.args[0]
        assert argv[:2] == ["docker", "events"]
        assert "--filter" in argv and f"container={cid}" in argv
        assert "container=kew-oom" not in argv
        assert "event=oom" in argv
        since = float(argv[argv.index("--since") + 1])
        until = float(argv[argv.index("--until") + 1])
        assert since == 95.0
        assert until == 106.0
        assert run.call_args.kwargs["timeout"] == OOM_PROBE_TIMEOUT_SECONDS == 12.0

    def test_no_oom_event_leaves_the_137_unnamed(self, tmp_path):
        # An empty (successful) events read is evidence of NO oom — naming a
        # memory cap here would invent the very fact the probe exists to find.
        handle = ContainerHandle(
            MagicMock(), "kew-plain", cidfile=_valid_cidfile(tmp_path)
        )
        with patch(
            "kew.subprocess_utils.run", return_value=MagicMock(returncode=0, stdout="")
        ):
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.ABSENT
        assert handle.resource_cap_reason is None

    def test_an_accepted_watchdog_kill_wins_and_retains_the_oom(self, tmp_path):
        from kew.dispatch_core import _resource_cap_stops_retry

        handle = ContainerHandle(
            MagicMock(), "kew-capped", cidfile=_valid_cidfile(tmp_path)
        )
        breach = ResourceCapObservation(
            ResourceCapCode.HOST_FREE_SPACE,
            "host free space 1 bytes below the 2 byte floor",
        )

        def answer(argv, **kwargs):
            if argv[:2] == ["docker", "kill"]:
                assert argv == ["docker", "kill", "a" * 64]
                return MagicMock(returncode=0, stdout="")
            assert argv[:2] == ["docker", "events"]
            assert f"container={'a' * 64}" in argv
            assert "event=oom" in argv
            return MagicMock(returncode=0, stdout="container oom current-id\n")

        with patch("kew.subprocess_utils.run", side_effect=answer) as run:
            handle._breach(breach)
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.SEEN
        assert handle.resource_cap_code is ResourceCapCode.HOST_FREE_SPACE
        assert "host free space" in (handle.resource_cap_reason or "")
        assert [item.code for item in handle.resource_cap_observations] == [
            ResourceCapCode.HOST_FREE_SPACE,
            ResourceCapCode.OOM,
        ]
        result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path / "l")
        result.hard_stop = "resource_cap"
        result.resource_cap_code = handle.resource_cap_code.value
        result.resource_cap_reason = handle.resource_cap_reason or ""
        assert _resource_cap_stops_retry(result) is False
        assert run.call_count == 2

    @pytest.mark.parametrize(
        "outcome",
        [
            MagicMock(returncode=1, stdout=""),
            OSError("no daemon"),
            ValueError("not in allowlist"),
            subprocess.TimeoutExpired(cmd="docker events", timeout=10),
        ],
    )
    def test_a_probe_the_daemon_did_not_answer_is_not_read_as_an_oom(
        self, outcome, capsys, tmp_path
    ):
        # An unreadable probe means "unknown", never "OOM": a fabricated
        # memory cap would misdirect the operator exactly like the timeout
        # mislabel this probe removes.
        handle = ContainerHandle(
            MagicMock(), "kew-quiet", cidfile=_valid_cidfile(tmp_path)
        )

        def answer(argv, **unused):
            assert argv[:2] == ["docker", "events"]
            assert f"container={'a' * 64}" in argv
            assert "event=oom" in argv
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with patch("kew.subprocess_utils.run", side_effect=answer):
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.UNAVAILABLE
        assert probe.diagnostic
        assert handle.resource_cap_reason is None
        # And it says so: an unanswered probe that returned a quiet "no OOM"
        # is the silent-clean-default this project keeps paying for.
        assert "OOM probe of kew-quiet" in capsys.readouterr().err

    def test_a_previous_same_name_event_is_not_attributed_to_the_new_id(self, tmp_path):
        cid = "b" * 64
        cidfile = tmp_path / "container.cid"
        cidfile.write_text(cid, encoding="utf-8")
        handle = ContainerHandle(MagicMock(), "kew-reused", cidfile=cidfile)

        def answer(argv, **kwargs):
            identity = argv[argv.index("--filter") + 1]
            old_name_event = (
                "old container oom\n" if identity == "container=kew-reused" else ""
            )
            return MagicMock(returncode=0, stdout=old_name_event)

        with patch("kew.subprocess_utils.run", side_effect=answer) as run:
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.ABSENT
        assert f"container={cid}" in run.call_args.args[0]
        assert handle.resource_cap_reason is None

    @staticmethod
    def _assert_unusable_cidfile(cidfile, capsys, state) -> None:
        handle = ContainerHandle(MagicMock(), "kew-fallback", cidfile=cidfile)
        stale_same_name = MagicMock(
            returncode=0,
            stdout="container oom stale (name=kew-fallback)\n",
        )

        with patch("kew.subprocess_utils.run", return_value=stale_same_name) as run:
            probe = handle.note_oom_kill()
        assert probe.status is OOMProbeStatus.UNAVAILABLE
        assert state in probe.diagnostic
        assert handle.resource_cap_reason is None
        run.assert_not_called()
        warning = capsys.readouterr().err
        assert "OOM attribution unavailable" in warning
        assert state in warning

    def test_missing_cidfile_makes_oom_attribution_unavailable(self, tmp_path, capsys):
        self._assert_unusable_cidfile(tmp_path / "missing.cid", capsys, "missing")

    def test_empty_cidfile_makes_oom_attribution_unavailable(self, tmp_path, capsys):
        cidfile = tmp_path / "empty.cid"
        cidfile.write_text("", encoding="utf-8")

        self._assert_unusable_cidfile(cidfile, capsys, "empty")

    def test_invalid_cidfile_makes_oom_attribution_unavailable(self, tmp_path, capsys):
        cidfile = tmp_path / "invalid.cid"
        cidfile.write_text("not-a-container-id", encoding="utf-8")

        self._assert_unusable_cidfile(cidfile, capsys, "invalid")

    def test_unreadable_cidfile_makes_oom_attribution_unavailable(
        self, tmp_path, capsys
    ):
        cidfile = tmp_path / "unreadable.cid"
        cidfile.mkdir()

        self._assert_unusable_cidfile(cidfile, capsys, "unreadable")

    def test_the_oom_probe_resolves_an_absolute_runtime_through_the_allowlist(
        self, monkeypatch, tmp_path
    ):
        # Same trap as the kill: the allowlist resolves by tool NAME, so an
        # absolute runtime path raises "Unknown executable" and the probe
        # would silently answer "no OOM" for every contained run.
        from kew import subprocess_utils

        monkeypatch.setattr(
            subprocess_utils, "TOOL_PATHS", {"docker": "/opt/bin/docker"}
        )
        handle = ContainerHandle(
            MagicMock(),
            "kew-abs-oom",
            runtime="/usr/local/bin/docker",
            cidfile=_valid_cidfile(tmp_path),
        )
        with patch("kew.subprocess_utils.subprocess.run") as real_run:
            real_run.return_value = MagicMock(returncode=0, stdout="")
            handle.note_oom_kill()
        assert real_run.call_args.args[0][0] == "/opt/bin/docker"

    def test_the_oom_probe_uses_the_runtime_that_launched_the_container(self, tmp_path):
        handle = ContainerHandle(
            MagicMock(),
            "kew-podman",
            runtime="podman",
            cidfile=_valid_cidfile(tmp_path),
        )
        with patch(
            "kew.subprocess_utils.run", return_value=MagicMock(returncode=0, stdout="")
        ) as run:
            handle.note_oom_kill()
        assert run.call_args.args[0][:2] == ["podman", "events"]

    def test_observation_bound_keeps_first_and_latest_and_marks_sidecar(self, tmp_path):
        from kew.sandbox import finalize_container_hard_stop

        handle = ContainerHandle(MagicMock(), "kew-bounded")
        first = ResourceCapObservation(
            ResourceCapCode.HOST_FREE_SPACE,
            "host free space first",
        )
        handle._record_observation(first)
        for free_bytes in range(MAX_RESOURCE_CAP_OBSERVATIONS + 3):
            handle._record_observation(
                ResourceCapObservation(
                    ResourceCapCode.HOST_FREE_SPACE,
                    f"host free space {free_bytes} bytes",
                )
            )
        latest = ResourceCapObservation(ResourceCapCode.OOM, "latest OOM")
        handle._record_observation(latest)

        assert len(handle.resource_cap_observations) == MAX_RESOURCE_CAP_OBSERVATIONS
        assert handle.resource_cap_observations[0] == first
        assert handle.resource_cap_observations[-1] == latest
        assert handle.resource_cap_observations_truncated is True

        log_file = tmp_path / "bounded.log"
        result = DispatchResult(exit_code=0, issue_num=1, log_file=log_file)
        finalize_container_hard_stop(
            handle,
            result,
            returncode=0,
            timeout_seconds=60,
        )
        write_container_evidence(
            log_file,
            oom_probe_status=None,
            oom_probe_diagnostic=None,
            resource_cap_observations=result.resource_cap_observations,
            resource_cap_observations_truncated=(
                result.resource_cap_observations_truncated
            ),
        )
        evidence = read_derived_evidence(log_file)
        assert result.resource_cap_observations_truncated is True
        assert evidence is not None
        assert evidence.resource_cap_observations_truncated is True
        assert evidence.resource_cap_observations[-1][0] == ResourceCapCode.OOM.value

    def test_concurrent_observation_recording_stays_bounded_and_marks_truncation(
        self,
    ):
        class CoordinatedLengthList(list[ResourceCapObservation]):
            def __init__(
                self,
                values: list[ResourceCapObservation],
            ) -> None:
                super().__init__(values)
                self._calls = 0
                self._calls_lock = threading.Lock()
                self._second_call_started = threading.Event()

            def __len__(self) -> int:
                size = super().__len__()
                with self._calls_lock:
                    self._calls += 1
                    current_call = self._calls
                if current_call == 1:
                    self._second_call_started.wait(timeout=0.05)
                elif current_call == 2:
                    self._second_call_started.set()
                return size

        proc = MagicMock()
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc,
            "kew-concurrent-bound",
            watchdog_interval_seconds=0.2,
        )
        retained = [
            ResourceCapObservation(
                ResourceCapCode.CHECKOUT_ENTRIES,
                f"checkout entry observation {index}",
            )
            for index in range(MAX_RESOURCE_CAP_OBSERVATIONS - 1)
        ]
        handle._resource_cap_observations = CoordinatedLengthList(
            retained,
        )
        observations = (
            ResourceCapObservation(
                ResourceCapCode.CHECKOUT_ENTRIES,
                "checkout entry observation final",
            ),
            ResourceCapObservation(
                ResourceCapCode.HOST_FREE_SPACE,
                "host free space observation final",
            ),
        )
        threads = [
            threading.Thread(target=handle._breach, args=(observation,))
            for observation in observations
        ]

        with patch.object(handle, "kill", return_value=KillStatus.REFUSED):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1.0)
            proc.poll.return_value = 0
            if handle._reaper_thread is not None:
                handle._reaper_thread.join(timeout=1.0)

        assert all(not thread.is_alive() for thread in threads)
        assert len(handle.resource_cap_observations) == MAX_RESOURCE_CAP_OBSERVATIONS
        assert handle.resource_cap_observations_truncated is True

    def test_observation_truncation_marker_reaches_breaker_audit(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.dispatch_sandbox import _emit_sandbox_hard_stop

        result = DispatchResult(
            exit_code=137,
            issue_num=1767,
            log_file=tmp_path / "audit.log",
            hard_stop="resource_cap",
            resource_cap_observations_truncated=True,
        )
        with patch("kew.dispatch_sandbox.emit") as emit:
            _emit_sandbox_hard_stop(
                None,
                DispatchConfig(),
                result,
                run_id="audit-truncation",
                issue_num=1767,
                item_id="item-1767",
                model="test-model",
            )

        payload = emit.call_args.kwargs["payload"]
        assert payload["resource_cap_observations_truncated"] is True


# ── Layer 2: build_run_argv prompt-handoff (command + entrypoint) ─────────────


class TestImageSupplyChain:
    """W1.4 / #1432 — the image kew launches is the one this checkout built.

    Nothing publishes a kew agent image and nothing will (the 2026-08-07
    ruling), so every property here is about keeping the runtime from going
    and fetching one from somewhere else.
    """

    def test_default_image_names_no_registry(self):
        from kew.sandbox import agent_image_inputs_digest, packaged_agent_image_context

        # The default names the installed input bytes.
        image = SandboxConfig().image
        digest = agent_image_inputs_digest(packaged_agent_image_context())[:12]
        assert image == f"kew-agent:docker-{digest}"
        # No registry host and no repository path — the two shapes that would
        # send the runtime to a network it should never reach for this image.
        assert "/" not in image
        assert "." not in image.split(":")[0]

    def test_launch_argv_refuses_to_pull(self, _checkout: Path):
        # Without --pull=never an absent local image sends the runtime to
        # docker.io/library/kew-agent — a repository kew does not own and
        # anyone may register. The containment boundary would then be a
        # stranger's image, silently, on the first dispatch after a prune.
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="42",
            network="open",
            runner="claude-code",
        )
        assert "--pull=never" in argv
        # Before the image, or the runtime reads it as the container command.
        assert argv.index("--pull=never") < argv.index("kew-agent:latest")

    def test_extract_argv_refuses_to_pull_too(self, tmp_path: Path):
        # The extraction container runs the SAME unqualified tag on the same
        # host. A second launcher that pulls reopens what the first one closed.
        argv = _extract_argv(tmp_path / "checkout", tmp_path / "artifact")
        assert "--pull=never" in argv
        assert argv.index("--pull=never") < argv.index("kew-agent:latest")

    def test_absent_image_is_told_apart_from_an_unreadable_daemon(self):
        # "not here" and "could not tell" have different remedies. Reporting
        # the second as the first hands the operator a build command for a
        # problem no build fixes — the same reason OOMProbeStatus splits them.
        absent = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: No such image: kew-agent:latest",
        )
        wedged = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        with patch("kew.subprocess_utils.run", return_value=absent):
            assert inspect_image_id("docker", "kew-agent:latest").status is (
                ImageIdStatus.ABSENT
            )
        with patch("kew.subprocess_utils.run", return_value=wedged):
            outcome = inspect_image_id("docker", "kew-agent:latest")
        assert outcome.status is ImageIdStatus.UNAVAILABLE
        assert "Docker daemon" in outcome.detail

    def test_timeout_is_unavailable_not_absent(self):
        # A wedged daemon that never answers is the case most likely to be
        # misread as "the image is gone", because both produce no id.
        with patch(
            "kew.subprocess_utils.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10),
        ):
            outcome = inspect_image_id("docker", "kew-agent:latest")
        assert outcome.status is ImageIdStatus.UNAVAILABLE

    def test_missing_image_refusal_carries_the_build_command(self):
        absent = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: No such image: kew-agent:latest",
        )
        with (
            patch("kew.subprocess_utils.run", return_value=absent),
            pytest.raises(ContainedImageUnresolvedError) as exc,
        ):
            require_image_id(
                "docker",
                "kew-agent:latest",
                runner="claude-code",
                refusal="the dispatch is refused before any charge",
            )
        message = str(exc.value)
        # The actionable half: the checkout-independent command that fixes it.
        assert "kew sandbox build --runner claude-code" in message
        # The diagnostic half: say kew will not pull, so the reader does not go
        # looking for a registry, a login, or a network problem.
        assert "never pulls" in message
        # The caller's own clause survives, so a dispatch still reads as one.
        assert "refused before any charge" in message
        # And it must NOT send them to the config, which is already correct.
        assert "[sandbox.images] pin" not in message

    def test_unreadable_daemon_refusal_does_not_tell_you_to_build(self):
        wedged = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        with (
            patch("kew.subprocess_utils.run", return_value=wedged),
            pytest.raises(ContainedImageUnresolvedError) as exc,
        ):
            require_image_id(
                "docker",
                "kew-agent:latest",
                runner="claude-code",
                refusal="refused",
            )
        message = str(exc.value)
        assert "docker build" not in message
        assert "runtime problem" in message
        assert "Cannot connect to the Docker daemon" in message

    def test_no_runtime_refusal_names_the_runtime_not_the_image(self):
        with pytest.raises(ContainedImageUnresolvedError) as exc:
            require_image_id(
                None, "kew-agent:latest", runner="claude-code", refusal="refused"
            )
        message = str(exc.value)
        assert "no container runtime is available" in message
        assert "docker build" not in message

    @pytest.mark.parametrize(
        "returned",
        [
            "kew-agent:latest",  # a tag, not an ID
            "sha256:a",  # a prefix
            "sha256:" + "z" * 64,  # right length, not hex
            "sha256:" + "a" * 63,  # one short
            "sha256:" + "a" * 65,  # one long — {64} must not become {64,}
            "A" * 64,  # uppercase is not the form any runtime reports
            "docker.io/library/kew-agent@sha256:" + "a" * 64,  # a ref, not an ID
        ],
    )
    def test_a_reference_that_is_not_a_full_digest_is_unavailable(self, returned):
        """An "immutable ID" that is mutable, partial, or non-hex is not one.

        Every non-empty string used to be accepted, so a runtime returning a
        tag had that value recorded on the route profile and launched as
        content-addressed evidence. The identical validation already guards
        `docker build --iidfile` in scripts/scan-agent-image.sh — it belongs on
        both sides of the boundary, and was applied to only one.
        """
        ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([{"Id": returned}]), stderr=""
        )
        with patch("kew.subprocess_utils.run", return_value=ok):
            outcome = inspect_image_id("docker", "kew-agent:latest")
        assert outcome.status is ImageIdStatus.UNAVAILABLE, (
            f"{returned!r} must not be reported as a resolved immutable ID"
        )
        assert outcome.image_id is None

    @pytest.mark.parametrize(
        "returned",
        [
            "sha256:" + "b" * 64,  # docker / orbstack
            "b" * 64,  # podman reports the bare digest for the same image
        ],
    )
    def test_both_runtime_spellings_resolve_to_one_normalised_id(self, returned):
        """Podman reports the bare 64 hex digits; Docker prefixes `sha256:`.

        Both are the same image. Refusing Podman's spelling would be a FALSE
        REFUSAL that blocks every dispatch on that runtime — as bad as the false
        pass this validation replaced. Normalising to one form also keeps the
        run row, the route-profile fingerprint and the launch argv naming the
        same bytes the same way.
        """
        ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([{"Id": returned}]), stderr=""
        )
        with patch("kew.subprocess_utils.run", return_value=ok):
            outcome = inspect_image_id("docker", "kew-agent:latest")
        assert outcome.status is ImageIdStatus.RESOLVED
        assert outcome.image_id == "sha256:" + "b" * 64

    def test_a_digest_with_a_trailing_newline_is_unavailable(self):
        returned = "sha256:" + "b" * 64 + "\n"
        ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([{"Id": returned}]), stderr=""
        )
        with patch("kew.subprocess_utils.run", return_value=ok):
            outcome = inspect_image_id("docker", "kew-agent:latest")
        assert outcome.status is ImageIdStatus.UNAVAILABLE
        assert outcome.image_id is None
        assert "not a full content-addressed image ID" in outcome.detail

    def test_a_non_digest_reference_refuses_the_dispatch(self):
        # And the refusal must reach the caller, not merely the status.
        ok = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"Id": "kew-agent:latest"}]),
            stderr="",
        )
        with (
            patch("kew.subprocess_utils.run", return_value=ok),
            pytest.raises(ContainedImageUnresolvedError) as exc,
        ):
            require_image_id(
                "docker", "kew-agent:latest", runner="claude-code", refusal="refused"
            )
        assert "not a full content-addressed image ID" in str(exc.value)

    def test_resolved_image_returns_its_immutable_id(self):
        ok = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"Id": "sha256:" + "a" * 64}]),
            stderr="",
        )
        with patch("kew.subprocess_utils.run", return_value=ok):
            image_id = require_image_id(
                "docker", "kew-agent:latest", runner="claude-code", refusal="refused"
            )
        assert image_id == "sha256:" + "a" * 64

    def test_a_custom_claude_image_is_not_told_to_build_the_stock_one(self):
        # An operator's hardened or internal claude image is still runner
        # "claude-code", but kew does not know how it is produced.
        message = missing_image_message(
            "registry.internal/hardened-claude:2026-08", runner="claude-code"
        )
        assert "registry.internal/hardened-claude:2026-08" in message
        # kew cannot know how a non-stock image is produced, and the two
        # possibilities need opposite advice — kew.toml itself ships a pinned
        # rebuild of kew's own Dockerfile, so withholding the command entirely
        # is unhelpful, and asserting it unconditionally destroys a hardened
        # image. The instruction must therefore be CONDITIONAL, and the
        # destructive reading must be called out in as many words.
        assert "depends on where it came from" in message
        assert "Do NOT run" in message
        assert "--tag registry.internal/hardened-claude:2026-08" in message

    def test_the_stock_image_still_gets_the_build_command(self):
        message = missing_image_message(STOCK_AGENT_IMAGE, runner="claude-code")
        assert "kew sandbox build --runner claude-code" in message

    def test_the_legacy_stock_tag_is_told_the_build_produces_the_pin(self):
        # `kew sandbox build` produces the digest pin, so an operator whose
        # config still names the legacy tag must be told to move the config
        # (or to build the legacy tag explicitly), not just to run the build.
        from kew.sandbox import (
            _expected_packaged_image_pin,
            packaged_agent_image_context,
        )

        context = packaged_agent_image_context()
        pin = _expected_packaged_image_pin(context, "claude-code")
        message = missing_image_message(STOCK_AGENT_IMAGE, runner="claude-code")
        assert pin in message
        assert "[sandbox] image" in message
        legacy_build = (
            f"kew sandbox build --runner claude-code --tag {STOCK_AGENT_IMAGE}"
        )
        assert f"{legacy_build} --unpinned" in message

    def test_the_current_pin_gets_the_plain_build_command(self):
        from kew.sandbox import (
            _expected_packaged_image_pin,
            packaged_agent_image_context,
        )

        context = packaged_agent_image_context()
        pin = _expected_packaged_image_pin(context, "claude-code")
        message = missing_image_message(pin, runner="claude-code")
        assert "kew sandbox build --runner claude-code." in message
        assert "--unpinned" not in message

    @pytest.mark.parametrize(
        "stale",
        ["kew-agent:docker-0123456789ab", "kew-agent-opencode:docker-0123456789ab"],
    )
    def test_a_stale_or_other_runner_pin_is_told_the_current_pin(self, stale):
        # The bare build produces the CURRENT pin and leaves this one absent.
        from kew.sandbox import (
            _expected_packaged_image_pin,
            packaged_agent_image_context,
        )

        context = packaged_agent_image_context()
        pin = _expected_packaged_image_pin(context, "claude-code")
        message = missing_image_message(stale, runner="claude-code")
        assert pin in message
        assert "[sandbox] image" in message
        assert f"kew sandbox build --runner claude-code --tag {stale}" not in message

    def test_a_custom_tag_suggestion_is_a_command_the_builder_accepts(self):
        from kew.sandbox import (
            _validate_packaged_image_tag,
            packaged_agent_image_context,
        )

        image = "registry.internal/operator:2026-09"
        message = missing_image_message(image, runner="claude-code")
        suggested = f"kew sandbox build --runner claude-code --tag {image} --unpinned"
        assert suggested in message
        # The suggested command must pass the builder's own validation.
        assert (
            _validate_packaged_image_tag(
                packaged_agent_image_context(), "claude-code", image, unpinned=True
            )
            is False
        )


class TestBuildEnvironment:
    def test_build_runs_under_a_scrubbed_environment(self, monkeypatch):
        from kew.sandbox import build_agent_image

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-review-secret-value")
        monkeypatch.setenv("GH_TOKEN", "ghp_secret_token_value")
        monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
        monkeypatch.setenv("DOCKER_AUTH_CONFIG", "{}")
        built = subprocess.CompletedProcess([], 0, "", "")
        with patch("kew.sandbox.subprocess_utils.run", return_value=built) as run:
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        env = run.call_args.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert "GH_TOKEN" not in env
        assert "DOCKER_AUTH_CONFIG" not in env
        assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        assert env["PATH"] == os.environ["PATH"]

    def test_build_failure_tail_redacts_credential_values(self, monkeypatch):
        from kew.sandbox import _build_failure_message

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-review-secret-value")
        built = subprocess.CompletedProcess(
            [],
            1,
            "step 1: export ANTHROPIC_API_KEY=sk-review-secret-value\n",
            "error: sk-review-secret-value rejected\n",
        )
        message = _build_failure_message("kew-agent:test", built)
        assert "sk-review-secret-value" not in message
        assert "<ANTHROPIC_API_KEY redacted>" in message
        assert "rc=1" in message

    def test_non_credential_values_are_left_alone(self, monkeypatch):
        from kew.sandbox import redact_credentials

        monkeypatch.setenv("TERM", "xterm-256color")
        text = "xterm-256color abc"
        assert redact_credentials(text) == text

    def test_a_six_character_token_is_redacted(self, monkeypatch):
        from kew.sandbox import redact_credentials

        monkeypatch.setenv("API_TOKEN", "s3cr3t")
        assert redact_credentials("got s3cr3t back") == "got <API_TOKEN redacted> back"

    def test_base64_forms_of_a_credential_are_redacted(self, monkeypatch):
        import base64

        from kew.sandbox import redact_credentials

        secret = "sk-review-secret-value"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        encoded = base64.b64encode(secret.encode()).decode()
        urlsafe = base64.urlsafe_b64encode(secret.encode()).decode()
        text = f"header: Basic {encoded} url: {urlsafe}"
        redacted = redact_credentials(text)
        assert encoded not in redacted
        assert urlsafe not in redacted
        assert "<ANTHROPIC_API_KEY redacted>" in redacted

    def test_proxy_password_is_stripped_from_the_build_env_and_redacted(
        self, monkeypatch
    ):
        from kew.sandbox import _build_env, redact_credentials

        monkeypatch.setenv("HTTPS_PROXY", "http://corp:hunter22@proxy.internal:3128")
        env = _build_env()
        assert env["HTTPS_PROXY"] == ""
        text = "proxy said http://corp:hunter22@proxy.internal:3128 refused"
        assert "hunter22" not in redact_credentials(text)
        assert "<HTTPS_PROXY redacted>" in redact_credentials(text)

    def test_a_proxy_without_userinfo_passes_through_unchanged(self, monkeypatch):
        from kew.sandbox import _build_env

        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        assert _build_env("inherit")["HTTPS_PROXY"] == "http://proxy.internal:3128"

    @pytest.mark.parametrize(
        ("secret", "printed"),
        [
            ("hunter22", "aHVudGVyMjI"),  # unpadded base64
            ("s3cr3t", "733363723374"),  # lowercase hex
            ("s3cr3t", "733363723374".upper()),  # uppercase hex
            ("p%40ssw0rd", "p@ssw0rd"),  # percent-decoded
        ],
    )
    def test_encoded_forms_of_a_credential_are_redacted(
        self, monkeypatch, secret, printed
    ):
        from kew.sandbox import redact_credentials

        monkeypatch.setenv("API_TOKEN", secret)
        assert printed not in redact_credentials(f"saw {printed} in output")

    def test_standard_and_urlsafe_base64_differ_and_both_redact(self, monkeypatch):
        import base64

        from kew.sandbox import redact_credentials

        secret = "sk-review-secret-value>>??"  # yields + and / in standard base64
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        standard = base64.b64encode(secret.encode()).decode()
        urlsafe = base64.urlsafe_b64encode(secret.encode()).decode()
        assert standard != urlsafe
        redacted = redact_credentials(f"{standard} {urlsafe}")
        assert standard not in redacted
        assert urlsafe not in redacted

    @staticmethod
    def _build_args(cmd: list[str]) -> dict[str, str]:
        pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--build-arg"]
        return dict(pair.split("=", 1) for pair in pairs)

    def test_config_proxies_reach_the_build_only_without_credentials(
        self, monkeypatch, tmp_path
    ):
        """config.json proxies become RUN-step build args by themselves.

        The default policy blanks every proxy argument and preserves DOCKER_CONFIG.
        Named contexts, TLS material, and CLI plugins remain available.
        """
        import json

        from kew.sandbox import build_agent_image

        operator = tmp_path / "docker-config"
        operator.mkdir()
        (operator / "config.json").write_text(
            json.dumps(
                {
                    "auths": {"registry.internal": {"auth": "dXNlcjpwYXNz"}},
                    "proxies": {
                        "default": {
                            "httpsProxy": "http://corp:hunter22@proxy.internal:3128",
                            "noProxy": "localhost",
                        }
                    },
                    "currentContext": "orbstack",
                }
            )
        )
        monkeypatch.setenv("DOCKER_CONFIG", str(operator))
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        seen: dict[str, object] = {}

        def capture(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs["env"]
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("kew.sandbox.subprocess_utils.run", side_effect=capture):
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        assert seen["env"]["DOCKER_CONFIG"] == str(operator)
        build_args = self._build_args(seen["cmd"])
        assert build_args["HTTPS_PROXY"] == ""
        assert build_args["NO_PROXY"] == ""
        assert build_args["HTTP_PROXY"] == ""
        assert "hunter22" not in " ".join(seen["cmd"])

    def test_environment_proxies_win_over_config_proxies(self, monkeypatch, tmp_path):
        import json

        from kew.sandbox import build_agent_image

        operator = tmp_path / "docker-config"
        operator.mkdir()
        (operator / "config.json").write_text(
            json.dumps({"proxies": {"default": {"httpsProxy": "http://cfg:3128"}}})
        )
        monkeypatch.setenv("DOCKER_CONFIG", str(operator))
        monkeypatch.setenv("HTTPS_PROXY", "http://u:p@env.proxy:3128")
        seen: dict[str, list[str]] = {}

        def capture(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("kew.sandbox.subprocess_utils.run", side_effect=capture):
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        assert self._build_args(seen["cmd"])["HTTPS_PROXY"] == ""

    def test_every_proxy_argument_is_pinned_even_without_a_config(
        self, monkeypatch, tmp_path
    ):
        from kew.sandbox import build_agent_image

        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "absent"))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
            monkeypatch.delenv(name.lower(), raising=False)
        seen: dict[str, list[str]] = {}

        def capture(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("kew.sandbox.subprocess_utils.run", side_effect=capture):
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        build_args = self._build_args(seen["cmd"])
        upper = {"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"}
        assert set(build_args) == upper | {name.lower() for name in upper}
        assert all(value == "" for value in build_args.values())

    def test_both_spellings_carry_the_same_sanitized_value(self, monkeypatch, tmp_path):
        from kew.sandbox import build_agent_image

        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "absent"))
        monkeypatch.setenv("https_proxy", "http://u:p@lower.proxy:3128")
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        seen: dict[str, list[str]] = {}

        def capture(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("kew.sandbox.subprocess_utils.run", side_effect=capture):
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        build_args = self._build_args(seen["cmd"])
        assert build_args["HTTPS_PROXY"] == ""
        assert build_args["https_proxy"] == ""

    def test_ipv6_proxy_keeps_its_brackets_without_userinfo(self):
        from kew.sandbox import _strip_proxy_userinfo

        stripped = _strip_proxy_userinfo("http://u:p@[2001:db8::10]:3128")
        assert stripped == "http://[2001:db8::10]:3128"

    @pytest.mark.parametrize(
        ("value", "stripped", "password"),
        [
            ("u:supersecret@proxy.internal:3128", "proxy.internal:3128", "supersecret"),
            ("http://u:@proxy.internal:3128", "http://proxy.internal:3128", None),
            ("http://u@proxy.internal:3128", "http://proxy.internal:3128", None),
            ("http://u:p@[2001:db8::10]", "http://[2001:db8::10]", "p"),
            ("http://proxy.internal:3128", "http://proxy.internal:3128", None),
        ],
    )
    def test_proxy_forms_are_stripped_and_their_passwords_found(
        self, value, stripped, password
    ):
        from kew.sandbox import _proxy_password, _strip_proxy_userinfo

        assert _strip_proxy_userinfo(value) == stripped
        assert _proxy_password(value) == password

    def test_a_scheme_less_credentialed_proxy_is_scrubbed_and_redacted(
        self, monkeypatch
    ):
        from kew.sandbox import _build_env, redact_credentials

        monkeypatch.setenv("HTTPS_PROXY", "u:supersecret@proxy.internal:3128")
        assert _build_env()["HTTPS_PROXY"] == ""
        assert "supersecret" not in redact_credentials("saw supersecret in output")

    def test_a_proxy_without_a_host_is_refused(self):
        from kew.sandbox import _strip_proxy_userinfo

        with pytest.raises(ValueError, match="names no host"):
            _strip_proxy_userinfo("http://u:p@")

    def test_config_no_proxy_list_passes_through_unparsed(self, monkeypatch, tmp_path):
        """``noProxy`` is a host list (IPv6 literals included), not a URL."""
        import json

        from kew.sandbox import _config_json_proxies

        operator = tmp_path / "docker-config"
        operator.mkdir()
        (operator / "config.json").write_text(
            json.dumps(
                {
                    "proxies": {
                        "default": {
                            "httpsProxy": "http://u:p@proxy.internal:3128",
                            "noProxy": "localhost,::1,.internal",
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("DOCKER_CONFIG", str(operator))
        assert _config_json_proxies() == {
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "NO_PROXY": "localhost,::1,.internal",
        }

    def test_non_string_config_proxy_values_are_ignored(self, monkeypatch, tmp_path):
        import json

        from kew.sandbox import _config_json_proxies

        operator = tmp_path / "docker-config"
        operator.mkdir()
        (operator / "config.json").write_text(
            json.dumps({"proxies": {"default": {"httpsProxy": 3128, "noProxy": None}}})
        )
        monkeypatch.setenv("DOCKER_CONFIG", str(operator))
        assert _config_json_proxies() == {}

    def test_a_malformed_proxy_url_refuses_the_build_before_docker(
        self, monkeypatch, tmp_path
    ):
        from kew.sandbox import build_agent_image

        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "absent"))
        monkeypatch.setenv("HTTPS_PROXY", "http://u:hunter22@[2001:db8::10:3128")
        with patch("kew.sandbox.subprocess_utils.run") as run:
            build_agent_image("docker", "kew-agent:docker-000000000000", unpinned=True)

        assert run.call_args.kwargs["env"]["HTTPS_PROXY"] == ""
        assert "hunter22" not in " ".join(run.call_args.args[0])

    def test_a_non_object_docker_config_is_reported_not_treated_as_empty(
        self, monkeypatch, tmp_path, capsys
    ):
        from kew.sandbox import _config_json_proxies

        operator = tmp_path / "docker-config"
        operator.mkdir()
        (operator / "config.json").write_text('["credsStore", "osxkeychain"]')
        monkeypatch.setenv("DOCKER_CONFIG", str(operator))

        assert _config_json_proxies() == {}
        assert "not a JSON object" in capsys.readouterr().err

    def test_overlapping_credentials_merge_whole_hull(self, monkeypatch):
        import base64

        from kew.sandbox import redact_credentials

        monkeypatch.setenv("SHORT_TOKEN", "abcdef")
        monkeypatch.setenv("LONG_TOKEN", "abcdefROOT-PASSWORD-12345")
        printed = base64.b64encode(b"abcdefROOT-PASSWORD-12345").decode()
        redacted = redact_credentials(f"saw {printed}")
        assert "ROOT-PASSWORD-12345" not in redacted
        tail = base64.b64encode(b"ROOT-PASSWORD-12345").decode()
        assert tail not in redacted
        assert redacted == "saw █"


class TestNetworkConfig:
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_network_is_rejected(self, blank):
        with pytest.raises(ValueError, match="cannot be blank"):
            SandboxConfig(network=blank)

    def test_network_is_stripped(self):
        assert SandboxConfig(network=" corp-egress ").network == "corp-egress"

    def test_stale_opencode_pin_names_the_images_field_only(self):
        from kew.sandbox import missing_image_message

        message = missing_image_message(
            "kew-agent-opencode:docker-0123456789ab", runner="opencode"
        )
        assert "images.opencode" in message
        assert "[sandbox] image (" not in message


class TestEffectiveNetwork:
    def test_effective_network_name_by_mode(self):
        from kew.sandbox import effective_network_name

        assert effective_network_name("allowlist") == ALLOWLIST_NETWORK
        assert effective_network_name("open") is None
        assert effective_network_name("corp-egress") == "corp-egress"

    def test_build_inspects_the_custom_network_not_the_allowlist(self):
        from kew.sandbox import ImageIdStatus, ImageResolution, build_sandbox_image

        present = subprocess.CompletedProcess([], 0, "[]", "")
        built = subprocess.CompletedProcess([], 0, "", "")
        image_id = "sha256:" + "e" * 64
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.subprocess_utils.run", return_value=present) as run,
            patch("kew.sandbox.build_agent_image", return_value=built),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.RESOLVED, image_id=image_id),
            ),
        ):
            result = build_sandbox_image(
                SandboxConfig(runtime="docker", network="corp-egress")
            )

        assert result.network_name == "corp-egress"
        assert result.allowlist_network_exists is True
        assert run.call_args.args[0] == ["docker", "network", "inspect", "corp-egress"]

    def test_open_network_is_not_inspected(self):
        from kew.sandbox import ImageIdStatus, ImageResolution, build_sandbox_image

        built = subprocess.CompletedProcess([], 0, "", "")
        image_id = "sha256:" + "f" * 64
        with (
            patch("kew.sandbox._require_build_daemon"),
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.subprocess_utils.run") as run,
            patch("kew.sandbox.build_agent_image", return_value=built),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.RESOLVED, image_id=image_id),
            ),
        ):
            result = build_sandbox_image(
                SandboxConfig(runtime="docker", network="open")
            )

        assert result.network_name is None
        run.assert_not_called()

    def test_smoke_refuses_to_build_stock_bytes_under_a_custom_tag(self):
        """The action must agree with the message.

        `missing_image_message` was taught not to *tell* an operator to build
        the stock Dockerfile under their hardened tag. `_ensure_smoke_image`
        went on *doing* it: it gated on the runner name alone, so a
        `claude-code` route pointed at `registry.internal/hardened-claude` —
        or at kew's own committed `kew-agent:docker-<digest>` pin — took the
        auto-build branch and silently replaced that image with stock bytes.
        `--pull=never` then launches the replacement forever, so the intended
        image is never retrieved again.
        """
        from kew.sandbox import _ensure_smoke_image

        with (
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image") as build,
        ):
            result = _ensure_smoke_image(
                "docker",
                "registry.internal/hardened-claude:2026-08",
                runner="claude-code",
                build_if_missing=True,
            )
        build.assert_not_called()
        assert result is not None and not result.ok
        assert result.layer == "image"
        assert "hardened-claude" in result.detail

    def test_smoke_refuses_a_pin_for_different_packaged_inputs(self):
        # A pin may be rebuilt only when it names the installed package bytes.
        from kew.sandbox import _ensure_smoke_image

        with (
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image") as build,
        ):
            result = _ensure_smoke_image(
                "docker",
                "kew-agent:docker-89d47a730000",
                runner="claude-code",
                build_if_missing=True,
            )
        build.assert_not_called()
        assert result is not None and not result.ok

    def test_smoke_builds_the_pin_for_the_packaged_inputs(self):
        from kew.sandbox import (
            _ensure_smoke_image,
            agent_image_inputs_digest,
            packaged_agent_image_context,
        )

        digest = agent_image_inputs_digest(packaged_agent_image_context())[:12]
        image = f"kew-agent:docker-{digest}"
        built = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with (
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built) as build,
        ):
            result = _ensure_smoke_image(
                "docker",
                image,
                runner="claude-code",
                build_if_missing=True,
            )

        assert result is None
        build.assert_called_once_with(
            "docker", image, runner="claude-code", proxy_environment="none"
        )

    def test_smoke_refuses_to_build_the_unpinned_stock_image(self):
        from kew.sandbox import _ensure_smoke_image

        with (
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image") as build,
        ):
            result = _ensure_smoke_image(
                "docker",
                STOCK_AGENT_IMAGE,
                runner="claude-code",
                build_if_missing=True,
            )
        build.assert_not_called()
        assert result is not None and not result.ok

    def test_non_default_runner_is_not_told_to_build_the_claude_image(self):
        # docker/agent.Dockerfile builds the claude CLI in. Telling an opencode
        # operator to build it under their own tag produces an image whose tag
        # lies about what is inside it — #1104 with extra steps.
        message = missing_image_message("kew-agent-opencode:latest", runner="opencode")
        assert "docker build -f docker/agent.Dockerfile" not in message
        assert "kew sandbox build --runner opencode" in message
        assert "kew-agent-opencode:latest" in message
        assert "opencode" in message


_NF1_TABLE_PREFIX = "kew_sc0_"


def _nf1_run_markers(
    *,
    owner: str,
    proxy_address: str,
    container_ids: tuple[str, ...],
) -> tuple[re.Pattern[str], ...]:
    """Identifiers a global firewall write by THIS run's filter would carry.

    The filter is one fixed nft batch: a table named for the owner holding a
    default-drop output chain and one accept rule naming the proxy address.
    Leaked into the host namespace, every object of it names the table, and
    its accept rule names the proxy. Any ``kew_sc0_`` table counts too: a
    correct sidecar never leaves one in the host namespace.

    The proxy address counts only inside a rule. The runtime itself indexes
    the run's containers in global sets and maps (OrbStack's ``inet
    orbstack`` domainproxy maps and ``docker_bridges`` set), and it updates
    them on its own schedule; that is the runtime serving the topology, not
    the filter writing. For the same reason the run's network and bridge are
    not markers.
    """

    def hex_identity(value: str, *, exact: bool = True) -> str:
        suffix = "(?![0-9a-f])" if exact else ""
        return rf"(?<![0-9a-f]){re.escape(value)}{suffix}"

    rule_entry = r'^(?:nft \{"rule":|ip6?tables-legacy -[AIR] )'
    patterns = [
        re.escape(_NF1_TABLE_PREFIX),
        hex_identity(owner),
        hex_identity(owner[:12], exact=False),
        rf"{rule_entry}.*(?<![0-9.]){re.escape(proxy_address)}(?![0-9])",
        *(hex_identity(identity) for identity in container_ids),
    ]
    return tuple(re.compile(pattern) for pattern in patterns)


def _global_firewall_entries(snapshot: GlobalFirewallSnapshot) -> Counter[str]:
    """Split one normalized global snapshot into comparable entries.

    Each nft object (table, chain, rule, set, ...) and each legacy iptables
    line is one entry, tagged with its source. Two snapshots are equal exactly
    when their entry multisets are equal.
    """
    entries: Counter[str] = Counter()
    for item in json.loads(snapshot.nft)["nftables"]:
        entries["nft " + json.dumps(item, separators=(",", ":"), sort_keys=True)] += 1
    for line in snapshot.iptables_legacy.splitlines():
        entries[f"iptables-legacy {line}"] += 1
    for line in snapshot.ip6tables_legacy.splitlines():
        entries[f"ip6tables-legacy {line}"] += 1
    return entries


def _run_attributable_firewall_changes(
    before: GlobalFirewallSnapshot,
    after: GlobalFirewallSnapshot,
    markers: tuple[re.Pattern[str], ...],
) -> list[str]:
    """Global entries added or removed between snapshots that name this run.

    Other containers on the same daemon (their bridges, their port maps)
    change the global tables too; only a change carrying one of this run's
    identifiers is attributable to the filter under test (#2126).
    """
    old = _global_firewall_entries(before)
    new = _global_firewall_entries(after)
    changed = [("+", entry) for entry in (new - old).elements()]
    changed += [("-", entry) for entry in (old - new).elements()]
    return [
        f"{sign} {entry}"
        for sign, entry in changed
        if any(marker.search(entry) for marker in markers)
    ]


def _cleanup_sidecar_topology(
    *,
    runtime: str,
    worker_id: str,
    helper_id: str,
    proxy_id: str,
    network_id: str,
    run: Callable[[list[str], str], subprocess.CompletedProcess[str]],
) -> None:
    """Remove exact SC-0 resources and require authoritative absence readback."""

    def require_success(
        argv: list[str], purpose: str
    ) -> subprocess.CompletedProcess[str]:
        result = run(argv, purpose)
        detail = result.stderr or result.stdout or "no diagnostic"
        assert result.returncode == 0, f"NF-1 {purpose} failed: {detail[-500:]}"
        return result

    require_success(
        build_container_cleanup_argv(runtime=runtime, container_ids=(worker_id,)),
        "worker cleanup",
    )
    require_success(
        build_container_cleanup_argv(runtime=runtime, container_ids=(proxy_id,)),
        "proxy cleanup",
    )
    require_success(
        build_network_cleanup_argv(runtime=runtime, network_id=network_id),
        "network cleanup",
    )
    container_inventory = require_success(
        [runtime, "container", "ls", "--all", "--quiet", "--no-trunc"],
        "container cleanup readback",
    ).stdout
    network_inventory = require_success(
        [runtime, "network", "ls", "--quiet", "--no-trunc"],
        "network cleanup readback",
    ).stdout
    assert isinstance(container_inventory, str), (
        "NF-1 container cleanup readback returned no inventory"
    )
    assert isinstance(network_inventory, str), (
        "NF-1 network cleanup readback returned no inventory"
    )
    container_ids = container_inventory.splitlines()
    network_ids = network_inventory.splitlines()
    assert all(re.fullmatch(r"[0-9a-f]{64}", identity) for identity in container_ids), (
        "NF-1 container cleanup readback returned malformed identities"
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", identity) for identity in network_ids), (
        "NF-1 network cleanup readback returned malformed identities"
    )
    assert worker_id not in container_ids, "NF-1 worker retained its exact ID"
    assert helper_id not in container_ids, "NF-1 helper retained its exact ID"
    assert proxy_id not in container_ids, "NF-1 proxy retained its exact ID"
    assert network_id not in network_ids, "NF-1 network retained its exact ID"


class TestIsolatedSidecarNetworkArgv:
    _OWNER = "0123456789abcdef0123456789abcdef"
    _WORKER_ID = "a" * 64
    _HELPER_ID = "b" * 64
    _IMAGE_ID = "sha256:" + "c" * 64
    _PROXY_ID = "d" * 64
    _NETWORK_ID = "e" * 64
    _PROXY_ADDRESS = "192.0.2.20"
    _PROXY_PORT = 4000

    def test_worker_filter_helper_has_only_net_admin_in_exact_worker_netns(self):
        argv = build_worker_netns_filter_helper_argv(
            runtime="docker",
            image_id=self._IMAGE_ID,
            worker_id=self._WORKER_ID,
            owner=self._OWNER,
        )

        assert argv[argv.index("--network") + 1] == f"container:{self._WORKER_ID}"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--cap-add") + 1] == "NET_ADMIN"
        assert argv[argv.index("--user") + 1] == "0:0"
        assert argv[-2:] == [self._IMAGE_ID, "30"]
        _assert_contains_all(
            argv, "--read-only", "--security-opt", "--pids-limit", "--memory", "--cpus"
        )
        _assert_excludes_all(argv, "-v", "--env", "/var/run/docker.sock")

    def test_worker_filter_uses_fixed_install_and_complete_readback_commands(self):
        assert build_worker_netns_filter_install_argv(
            runtime="docker", helper_id=self._HELPER_ID
        ) == ["docker", "exec", "-i", self._HELPER_ID, "nft", "-f", "-"]
        assert build_worker_netns_filter_readback_argv(
            runtime="docker", helper_id=self._HELPER_ID, owner=self._OWNER
        ) == [
            "docker",
            "exec",
            self._HELPER_ID,
            "nft",
            "--json",
            "list",
            "table",
            "inet",
            f"kew_sc0_{self._OWNER}",
        ]

    def test_worker_filter_rules_allow_only_established_and_exact_proxy(self):
        rules = worker_netns_filter_ruleset(
            owner=self._OWNER,
            proxy_address=self._PROXY_ADDRESS,
            proxy_port=self._PROXY_PORT,
        ).decode()

        assert "policy drop" in rules
        assert "ct state established accept" in rules
        assert f"ip daddr {self._PROXY_ADDRESS} tcp dport 4000 accept" in rules
        for forbidden in (
            "related",
            "iif lo",
            "oif lo",
            "udp",
            "dns",
            "subnet",
            "docker-user",
            "flush",
        ):
            assert forbidden not in rules.lower()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("image_id", "kew-agent:latest"),
            ("worker_id", "a" * 12),
            ("owner", "a" * 31),
        ],
    )
    def test_worker_filter_helper_refuses_mutable_or_partial_identities(
        self, field: str, value: str
    ) -> None:
        arguments = {
            "runtime": "docker",
            "image_id": self._IMAGE_ID,
            "worker_id": self._WORKER_ID,
            "owner": self._OWNER,
        }
        arguments[field] = value
        with pytest.raises(ValueError):
            build_worker_netns_filter_helper_argv(**arguments)  # type: ignore[arg-type]

    def test_worker_filter_readback_requires_the_exact_complete_ruleset(self):
        table = f"kew_sc0_{self._OWNER}"
        payload = json.dumps(
            {
                "nftables": [
                    {"metainfo": {"json_schema_version": 1}},
                    {"table": {"family": "inet", "name": table, "handle": 4}},
                    {
                        "chain": {
                            "family": "inet",
                            "table": table,
                            "name": "output",
                            "handle": 1,
                            "type": "filter",
                            "hook": "output",
                            "prio": 0,
                            "policy": "drop",
                        }
                    },
                    {
                        "rule": {
                            "family": "inet",
                            "table": table,
                            "chain": "output",
                            "handle": 2,
                            "expr": [
                                {
                                    "match": {
                                        "op": "in",
                                        "left": {"ct": {"key": "state"}},
                                        "right": "established",
                                    }
                                },
                                {"accept": None},
                            ],
                        }
                    },
                    {
                        "rule": {
                            "family": "inet",
                            "table": table,
                            "chain": "output",
                            "handle": 3,
                            "expr": [
                                {
                                    "match": {
                                        "op": "==",
                                        "left": {
                                            "payload": {
                                                "protocol": "ip",
                                                "field": "daddr",
                                            }
                                        },
                                        "right": self._PROXY_ADDRESS,
                                    }
                                },
                                {
                                    "match": {
                                        "op": "==",
                                        "left": {
                                            "payload": {
                                                "protocol": "tcp",
                                                "field": "dport",
                                            }
                                        },
                                        "right": self._PROXY_PORT,
                                    }
                                },
                                {"accept": None},
                            ],
                        }
                    },
                ]
            }
        )

        validate_worker_netns_filter_readback(
            payload,
            owner=self._OWNER,
            proxy_address=self._PROXY_ADDRESS,
            proxy_port=self._PROXY_PORT,
        )
        list_state = payload.replace(
            '"right": "established"', '"right": ["established"]'
        )
        with pytest.raises(WorkerNetnsFilterError, match="rules readback"):
            validate_worker_netns_filter_readback(
                list_state,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
            )
        malformed = payload.replace('"policy": "drop"', '"policy": "accept"')
        with pytest.raises(WorkerNetnsFilterError, match="chain readback"):
            validate_worker_netns_filter_readback(
                malformed,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
            )

    def test_global_firewall_observer_has_fixed_read_only_commands(self):
        expected_entrypoints = {
            GlobalFirewallSource.NFT: "nft",
            GlobalFirewallSource.IPTABLES_LEGACY: "iptables-legacy-save",
            GlobalFirewallSource.IP6TABLES_LEGACY: "ip6tables-legacy-save",
        }
        for source, command in expected_entrypoints.items():
            argv = build_global_firewall_observer_argv(
                runtime="docker",
                image_id=self._IMAGE_ID,
                owner=self._OWNER,
                source=source,
            )
            assert argv[argv.index("--network") + 1] == "host"
            assert argv[argv.index("--cap-drop") + 1] == "ALL"
            assert argv[argv.index("--cap-add") + 1] == "NET_ADMIN"
            assert argv[argv.index("--entrypoint") + 1] == command
            assert "--read-only" in argv
            assert "-v" not in argv
            assert "--env" not in argv

    def test_global_firewall_snapshot_normalizes_only_dynamic_metadata(self):
        def capture(
            counter: int,
            verdict: str,
            timestamp: str,
            *,
            legacy_policy: str = "DROP",
            legacy_target: str = "ACCEPT",
        ):
            nft = json.dumps(
                {
                    "nftables": [
                        {"metainfo": {"version": timestamp}},
                        {
                            "rule": {
                                "family": "inet",
                                "table": "filter",
                                "chain": "output",
                                "expr": [
                                    {"counter": {"packets": counter, "bytes": counter}},
                                    {verdict: None},
                                ],
                            }
                        },
                    ]
                }
            )
            legacy = (
                f"# Generated by iptables-save on {timestamp}\n"
                "*filter\n"
                f":FORWARD {legacy_policy} [{counter}:{counter * 60}]\n"
                f"-A FORWARD -j {legacy_target}\n"
                "COMMIT\n"
                f"# Completed on {timestamp}\n"
            )

            def answer(argv: list[str], **kwargs: object):
                del kwargs
                entrypoint = argv[argv.index("--entrypoint") + 1]
                stdout = nft if entrypoint == "nft" else legacy
                return subprocess.CompletedProcess(argv, 0, stdout, "")

            with patch("kew.sandbox.subprocess_utils.run", side_effect=answer):
                return capture_global_firewall_snapshot(
                    runtime="docker", image_id=self._IMAGE_ID, owner=self._OWNER
                )

        before = capture(1, "accept", "first")
        after_counters = capture(99, "accept", "second")
        after_rule_change = capture(99, "drop", "second")
        after_legacy_policy_change = capture(
            99, "accept", "second", legacy_policy="ACCEPT"
        )
        after_legacy_rule_change = capture(99, "accept", "second", legacy_target="DROP")

        assert after_counters == before
        assert after_rule_change != before
        assert after_legacy_policy_change != before
        assert after_legacy_rule_change != before

    @staticmethod
    def _global_snapshot(
        nft: list[dict[str, object]], legacy: list[str] | None = None
    ) -> GlobalFirewallSnapshot:
        legacy = legacy or []
        return GlobalFirewallSnapshot(
            nft=json.dumps({"nftables": nft}, separators=(",", ":"), sort_keys=True),
            iptables_legacy="\n".join(["*filter", *legacy, "COMMIT"]) + "\n",
            ip6tables_legacy="*filter\nCOMMIT\n",
        )

    def _nf1_markers(self) -> tuple[re.Pattern[str], ...]:
        return _nf1_run_markers(
            owner=self._OWNER,
            proxy_address=self._PROXY_ADDRESS,
            container_ids=(self._WORKER_ID, self._PROXY_ID, self._HELPER_ID),
        )

    _DOCKER_BASELINE: list[dict[str, object]] = [
        {"table": {"family": "ip", "name": "filter", "handle": 1}},
        {
            "chain": {
                "family": "ip",
                "table": "filter",
                "name": "DOCKER-USER",
                "handle": 2,
            }
        },
        {
            "rule": {
                "family": "ip",
                "table": "filter",
                "chain": "DOCKER-USER",
                "handle": 3,
                "expr": [{"counter": {"packets": 0, "bytes": 0}}, {"return": None}],
            }
        },
    ]

    def test_nf1_global_check_ignores_other_containers_churn(self):
        # Another network on the daemon appearing and one disappearing in the
        # window, a proxy address that is only a textual prefix of ours, and
        # the runtime re-indexing its own address maps and bridge sets, which
        # list this run's proxy and bridge (OrbStack's inet orbstack table).
        def runtime_index(peer: str, bridges: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "map": {
                        "family": "inet",
                        "table": "orbstack",
                        "name": "domainproxy4",
                        "type": "ipv4_addr",
                        "map": "ipv4_addr",
                        "handle": 13,
                        "elem": [
                            ["192.168.138.4", self._PROXY_ADDRESS],
                            ["192.168.138.5", peer],
                        ],
                    }
                },
                {
                    "set": {
                        "family": "inet",
                        "table": "orbstack",
                        "name": "docker_bridges",
                        "type": "ifname",
                        "handle": 11,
                        "elem": bridges,
                    }
                },
            ]

        run_bridge = f"br-{self._NETWORK_ID[:12]}"
        before = self._global_snapshot(
            [
                *self._DOCKER_BASELINE,
                *runtime_index("192.168.147.3", [run_bridge]),
                {"chain": {"family": "ip", "table": "filter", "name": "old"}},
            ],
            ["-A FORWARD -o br-0000deadbeef -j DOCKER"],
        )
        after = self._global_snapshot(
            [
                *self._DOCKER_BASELINE,
                *runtime_index("192.168.147.9", [run_bridge, "br-1111cafef00d"]),
                {
                    "rule": {
                        "family": "ip",
                        "table": "filter",
                        "chain": "DOCKER-USER",
                        "handle": 9,
                        "expr": [
                            {"match": {"right": "br-1111cafef00d"}},
                            {"match": {"right": self._PROXY_ADDRESS + "0"}},
                            {"accept": None},
                        ],
                    }
                },
            ],
            ["-A FORWARD -o br-1111cafef00d -j DOCKER"],
        )

        assert after != before
        assert (
            _run_attributable_firewall_changes(before, after, self._nf1_markers()) == []
        )

    @pytest.mark.parametrize(
        "leak",
        [
            pytest.param(
                [
                    {
                        "table": {
                            "family": "inet",
                            "name": "kew_sc0_0123456789abcdef0123456789abcdef",
                            "handle": 7,
                        }
                    }
                ],
                id="worker-filter-table-in-host-netns",
            ),
            pytest.param(
                [
                    {
                        "rule": {
                            "family": "ip",
                            "table": "filter",
                            "chain": "DOCKER-USER",
                            "handle": 8,
                            "expr": [
                                {"match": {"right": "192.0.2.20"}},
                                {"accept": None},
                            ],
                        }
                    }
                ],
                id="proxy-accept-in-docker-chain",
            ),
            pytest.param(
                [
                    {
                        "table": {
                            "family": "inet",
                            "name": "kew_sc0_" + "9" * 32,
                            "handle": 7,
                        }
                    }
                ],
                id="any-sidecar-table-in-host-netns",
            ),
        ],
    )
    def test_nf1_global_check_catches_nft_write_by_this_run(
        self, leak: list[dict[str, object]]
    ):
        before = self._global_snapshot(self._DOCKER_BASELINE)
        # Unrelated churn in the same window must not mask the run's write.
        noise = {"chain": {"family": "ip", "table": "filter", "name": "noise"}}
        after = self._global_snapshot([*self._DOCKER_BASELINE, noise, *leak])

        changes = _run_attributable_firewall_changes(before, after, self._nf1_markers())

        assert len(changes) == len(leak)
        assert all(change.startswith("+ nft ") for change in changes)
        assert "noise" not in "".join(changes)

    @pytest.mark.parametrize(
        ("before_legacy", "after_legacy"),
        [
            pytest.param(
                [],
                ["-A OUTPUT -d 192.0.2.20/32 -p tcp -m tcp --dport 4000 -j ACCEPT"],
                id="proxy-accept-added",
            ),
            pytest.param(
                ["-A DOCKER-USER -d 192.0.2.20/32 -j DROP"],
                [],
                id="rule-removed-for-run-proxy",
            ),
        ],
    )
    def test_nf1_global_check_catches_legacy_write_by_this_run(
        self, before_legacy: list[str], after_legacy: list[str]
    ):
        before = self._global_snapshot(self._DOCKER_BASELINE, before_legacy)
        after = self._global_snapshot(self._DOCKER_BASELINE, after_legacy)

        changes = _run_attributable_firewall_changes(before, after, self._nf1_markers())

        assert len(changes) == 1
        assert changes[0].split(" ", 2)[1] == "iptables-legacy"

    def test_nf1_global_check_catches_the_real_worker_ruleset_leaking(self):
        # The exact batch the helper installs, as the host nft would list it
        # had the helper run in the host namespace instead of the worker's.
        table = f"kew_sc0_{self._OWNER}"
        assert f"table inet {table} " in worker_netns_filter_ruleset(
            owner=self._OWNER,
            proxy_address=self._PROXY_ADDRESS,
            proxy_port=self._PROXY_PORT,
        ).decode("ascii")
        leaked = [
            {"table": {"family": "inet", "name": table, "handle": 40}},
            {
                "chain": {
                    "family": "inet",
                    "table": table,
                    "name": "output",
                    "handle": 1,
                    "type": "filter",
                    "hook": "output",
                    "prio": 0,
                    "policy": "drop",
                }
            },
        ]
        before = self._global_snapshot(self._DOCKER_BASELINE)
        after = self._global_snapshot([*self._DOCKER_BASELINE, *leaked])

        assert len(
            _run_attributable_firewall_changes(before, after, self._nf1_markers())
        ) == len(leaked)

    def test_pre_agent_boundary_removes_helper_before_agent_start(self):
        import kew.dispatch_sandbox as dispatch_sandbox

        events: list[str] = []

        def run(argv: list[str], **kwargs: object):
            del kwargs
            if argv[1] == "create":
                event, stdout = "create", self._HELPER_ID + "\n"
            elif argv[1:3] == ["container", "rm"]:
                event, stdout = "remove", self._HELPER_ID + "\n"
            elif argv[1:3] == ["container", "ls"]:
                event, stdout = "inventory", "d" * 64 + "\n"
            elif argv[1] == "start":
                event, stdout = "start", self._HELPER_ID + "\n"
            else:
                event, stdout = "readback", "{}"
            events.append(event)
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        def run_bytes(argv: list[str], **kwargs: object):
            del kwargs
            events.append("install")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with (
            patch.object(dispatch_sandbox, "_run", side_effect=run),
            patch.object(dispatch_sandbox, "_run_bytes", side_effect=run_bytes),
            patch.object(dispatch_sandbox, "validate_worker_netns_filter_readback"),
        ):
            evidence = dispatch_sandbox.start_agent_after_worker_netns_filter(
                runtime="docker",
                image_id=self._IMAGE_ID,
                worker_id=self._WORKER_ID,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
                start_agent=lambda: events.append("agent"),
            )

        assert events == [
            "create",
            "start",
            "install",
            "readback",
            "remove",
            "inventory",
            "agent",
        ]
        assert evidence.helper_id == self._HELPER_ID

    def test_readback_refusal_cleans_helper_and_prevents_agent_start(self):
        import kew.dispatch_sandbox as dispatch_sandbox

        events: list[str] = []

        def run(argv: list[str], **kwargs: object):
            del kwargs
            if argv[1] == "create":
                stdout = self._HELPER_ID + "\n"
            elif argv[1:3] == ["container", "ls"]:
                events.append("inventory")
                stdout = ""
            elif argv[1:3] == ["container", "rm"]:
                events.append("remove")
                stdout = self._HELPER_ID + "\n"
            else:
                stdout = "{}"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with (
            patch.object(dispatch_sandbox, "_run", side_effect=run),
            patch.object(
                dispatch_sandbox,
                "_run_bytes",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ),
            patch.object(
                dispatch_sandbox,
                "validate_worker_netns_filter_readback",
                side_effect=WorkerNetnsFilterError("readback mismatch"),
            ),
            pytest.raises(WorkerNetnsFilterError, match="readback mismatch"),
        ):
            dispatch_sandbox.start_agent_after_worker_netns_filter(
                runtime="docker",
                image_id=self._IMAGE_ID,
                worker_id=self._WORKER_ID,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
                start_agent=lambda: events.append("agent"),
            )

        assert events == ["remove", "inventory"]

    def test_helper_cleanup_failure_prevents_agent_start(self):
        import kew.dispatch_sandbox as dispatch_sandbox

        events: list[str] = []

        def run(argv: list[str], **kwargs: object):
            del kwargs
            if argv[1] == "create":
                return subprocess.CompletedProcess(argv, 0, self._HELPER_ID + "\n", "")
            if argv[1:3] == ["container", "rm"]:
                events.append("remove-failed")
                return subprocess.CompletedProcess(argv, 1, "", "cleanup refused")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with (
            patch.object(dispatch_sandbox, "_run", side_effect=run),
            patch.object(
                dispatch_sandbox,
                "_run_bytes",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ),
            patch.object(dispatch_sandbox, "validate_worker_netns_filter_readback"),
            pytest.raises(WorkerNetnsFilterError, match="helper cleanup failed"),
        ):
            dispatch_sandbox.start_agent_after_worker_netns_filter(
                runtime="docker",
                image_id=self._IMAGE_ID,
                worker_id=self._WORKER_ID,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
                start_agent=lambda: events.append("agent"),
            )

        assert events == ["remove-failed"]

    def test_filter_helper_owner_tracks_cleanup_failure(self):
        import kew.dispatch_sandbox as dispatch_sandbox

        events: list[tuple[str, str]] = []

        def run(argv: list[str], **kwargs: object):
            del kwargs
            if argv[1] == "create":
                return subprocess.CompletedProcess(argv, 0, self._HELPER_ID + "\n", "")
            if argv[1:3] == ["container", "rm"]:
                return subprocess.CompletedProcess(argv, 1, "", "cleanup refused")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with (
            patch.object(dispatch_sandbox, "_run", side_effect=run),
            patch.object(
                dispatch_sandbox,
                "_run_bytes",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ),
            patch.object(dispatch_sandbox, "validate_worker_netns_filter_readback"),
            pytest.raises(WorkerNetnsFilterError, match="helper cleanup failed"),
        ):
            dispatch_sandbox.install_worker_netns_filter(
                runtime="docker",
                image_id=self._IMAGE_ID,
                worker_id=self._WORKER_ID,
                owner=self._OWNER,
                proxy_address=self._PROXY_ADDRESS,
                proxy_port=self._PROXY_PORT,
                on_helper_created=lambda value: events.append(("created", value)),
                on_helper_removed=lambda value: events.append(("removed", value)),
            )

        assert events == [("created", self._HELPER_ID)]

    def test_unavailable_global_firewall_readback_fails_the_proof(self):
        unavailable = subprocess.CompletedProcess([], 1, "", "unavailable")
        with (
            patch("kew.sandbox.subprocess_utils.run", return_value=unavailable),
            pytest.raises(WorkerNetnsFilterError, match="global nft readback failed"),
        ):
            capture_global_firewall_snapshot(
                runtime="docker", image_id=self._IMAGE_ID, owner=self._OWNER
            )

    @pytest.mark.parametrize(
        ("controlled_purpose", "returncode", "stdout", "expected_message"),
        [
            ("worker cleanup", 1, "", "worker cleanup"),
            ("proxy cleanup", 1, "", "proxy cleanup"),
            ("network cleanup", 1, "", "network cleanup"),
            ("container cleanup readback", 1, "", "container cleanup readback"),
            ("network cleanup readback", 1, "", "network cleanup readback"),
            ("container cleanup readback", 0, None, "no inventory"),
            ("network cleanup readback", 0, None, "no inventory"),
            (
                "container cleanup readback",
                0,
                "not-a-full-id\n",
                "malformed identities",
            ),
            (
                "network cleanup readback",
                0,
                "not-a-full-id\n",
                "malformed identities",
            ),
            ("container cleanup readback", 0, _WORKER_ID + "\n", "worker retained"),
            ("container cleanup readback", 0, _HELPER_ID + "\n", "helper retained"),
            ("container cleanup readback", 0, _PROXY_ID + "\n", "proxy retained"),
            ("network cleanup readback", 0, _NETWORK_ID + "\n", "network retained"),
        ],
    )
    def test_sidecar_cleanup_rejects_unproved_absence(
        self,
        controlled_purpose: str,
        returncode: int,
        stdout: str | None,
        expected_message: str,
    ):
        def run(argv: list[str], purpose: str):
            if purpose != controlled_purpose:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, returncode, stdout, "injected")

        with pytest.raises(AssertionError, match=expected_message):
            _cleanup_sidecar_topology(
                runtime="docker",
                worker_id=self._WORKER_ID,
                helper_id=self._HELPER_ID,
                proxy_id=self._PROXY_ID,
                network_id=self._NETWORK_ID,
                run=run,
            )

    def test_sidecar_cleanup_uses_exact_ids_and_accepts_authoritative_absence(self):
        unrelated_container_id = "f" * 64
        unrelated_network_id = "1" * 64
        calls: list[tuple[list[str], str]] = []

        def run(argv: list[str], purpose: str):
            calls.append((argv, purpose))
            stdout = ""
            if purpose == "container cleanup readback":
                stdout = unrelated_container_id + "\n"
            if purpose == "network cleanup readback":
                stdout = unrelated_network_id + "\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        _cleanup_sidecar_topology(
            runtime="docker",
            worker_id=self._WORKER_ID,
            helper_id=self._HELPER_ID,
            proxy_id=self._PROXY_ID,
            network_id=self._NETWORK_ID,
            run=run,
        )

        assert calls == [
            (
                build_container_cleanup_argv(
                    runtime="docker", container_ids=(self._WORKER_ID,)
                ),
                "worker cleanup",
            ),
            (
                build_container_cleanup_argv(
                    runtime="docker", container_ids=(self._PROXY_ID,)
                ),
                "proxy cleanup",
            ),
            (
                build_network_cleanup_argv(
                    runtime="docker", network_id=self._NETWORK_ID
                ),
                "network cleanup",
            ),
            (
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                ],
                "container cleanup readback",
            ),
            (
                ["docker", "network", "ls", "--quiet", "--no-trunc"],
                "network cleanup readback",
            ),
        ]

    @pytest.mark.serial
    def test_sidecar_filter_never_mutates_global_firewall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import subprocess_utils
        from kew.config import SwitchyardConfig
        from kew.dispatch_sandbox import start_agent_after_worker_netns_filter
        from kew.switchyard_supervisor import SidecarAttemptSupervisor

        runtime_path = shutil.which("docker")
        assert runtime_path is not None, "NF-1 Docker executable is unavailable"
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", runtime_path)

        def run(argv: list[str], purpose: str) -> subprocess.CompletedProcess[str]:
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise AssertionError(f"NF-1 {purpose} unavailable") from exc
            assert result.returncode == 0, (
                f"NF-1 {purpose} failed: {(result.stderr or result.stdout)[-500:]}"
            )
            return result

        info = run(["docker", "info"], "Docker daemon access")
        _assert_process_output(info)
        image = run(
            ["docker", "image", "inspect", SandboxConfig().image],
            "agent image resolution",
        )
        image_id = json.loads(image.stdout)[0]["Id"]
        _assert_matches(r"sha256:[0-9a-f]{64}", image_id)

        config_contents = b"# synthetic offline SC-1 configuration copy\n"
        config_owner = SidecarAttemptSupervisor(
            SwitchyardConfig(route="synthetic-offline", expected_version="0.1.0"),
            runtime="docker",
            worker_image_id=image_id,
        )
        config_owner.config_sha256 = hashlib.sha256(config_contents).hexdigest()
        try:
            config_owner._create_config_storage(config_contents)
        finally:
            config_owner.close()
        assert config_owner._cleanup_verified is True

        owner = uuid.uuid4().hex
        prefix = f"kew-sc0-{owner[:12]}-global"
        network_id = run(
            build_per_run_network_argv(runtime="docker", network=prefix, owner=owner),
            "internal network creation",
        ).stdout.strip()
        _assert_matches(r"[0-9a-f]{64}", network_id)
        network_live = True
        containers: list[str] = []

        def create(role: str, script: str) -> str:
            created = run(
                [
                    "docker",
                    "create",
                    "--name",
                    f"{prefix}-{role}",
                    "--label",
                    f"io.kew.switchyard.attempt={owner}",
                    "--pull=never",
                    "--network",
                    prefix,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:size=1m,uid=1001,gid=1001,mode=0700",
                    "--pids-limit",
                    "16",
                    "--memory",
                    "32m",
                    "--memory-swap",
                    "32m",
                    "--cpus",
                    "0.10",
                    "--entrypoint",
                    "node",
                    image_id,
                    "-e",
                    script,
                ],
                f"{role} creation",
            ).stdout.strip()
            _assert_matches(r"[0-9a-f]{64}", created)
            containers.append(created)
            run(["docker", "start", created], f"{role} startup")
            return created

        try:
            worker_id = create("worker", "setInterval(() => {}, 1000000)")
            proxy_id = create(
                "proxy",
                'require("net").createServer((s) => s.end()).listen(4000, '
                '"0.0.0.0"); setInterval(() => {}, 1000000)',
            )
            proxy = json.loads(
                run(
                    ["docker", "container", "inspect", proxy_id],
                    "proxy inspection",
                ).stdout
            )[0]
            proxy_address = proxy["NetworkSettings"]["Networks"][prefix]["IPAddress"]
            _assert_matches(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", proxy_address)

            before = capture_global_firewall_snapshot(
                runtime="docker", image_id=image_id, owner=owner
            )

            def start_marker() -> None:
                run(
                    [
                        "docker",
                        "exec",
                        worker_id,
                        "node",
                        "-e",
                        'require("fs").writeFileSync('
                        '"/tmp/kew-agent-started", "started\\n")',
                    ],
                    "agent startup marker",
                )

            filter_evidence = start_agent_after_worker_netns_filter(
                runtime="docker",
                image_id=image_id,
                worker_id=worker_id,
                owner=owner,
                proxy_address=proxy_address,
                proxy_port=4000,
                start_agent=start_marker,
            )
            after_install = capture_global_firewall_snapshot(
                runtime="docker", image_id=image_id, owner=owner
            )
            # Other containers on this daemon, and the runtime indexing this
            # run's own containers, change the global tables in this window,
            # so whole-snapshot equality proves daemon idleness, not this
            # filter's behavior (#2126). A global change carrying the filter's
            # identifiers is a write by the filter.
            markers = _nf1_run_markers(
                owner=owner,
                proxy_address=proxy_address,
                container_ids=(worker_id, proxy_id, filter_evidence.helper_id),
            )
            assert (
                _run_attributable_firewall_changes(before, after_install, markers) == []
            )
            assert filter_evidence.table not in after_install.nft
            assert filter_evidence.worker_id == worker_id
            assert filter_evidence.image_id == image_id

            _cleanup_sidecar_topology(
                runtime="docker",
                worker_id=worker_id,
                helper_id=filter_evidence.helper_id,
                proxy_id=proxy_id,
                network_id=network_id,
                run=run,
            )
            containers.clear()
            network_live = False
        finally:
            if containers:
                subprocess.run(
                    build_container_cleanup_argv(
                        runtime="docker", container_ids=tuple(reversed(containers))
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            if network_live:
                subprocess.run(
                    build_network_cleanup_argv(runtime="docker", network_id=network_id),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )

    def test_per_run_network_is_internal_ipv6_and_owned(self):
        argv = build_per_run_network_argv(
            runtime="docker",
            network="kew-switchyard-attempt-123",
            owner="0123456789abcdef",
        )

        assert argv == [
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--ipv6",
            "--subnet",
            "fd01:2345:6789:abcd::/64",
            "--label",
            "io.kew.switchyard.attempt=0123456789abcdef",
            "kew-switchyard-attempt-123",
        ]
        assert not any(token in argv for token in ("iptables", "nft", "pfctl"))

    def test_sidecar_attachment_is_one_exact_network_and_container(self):
        assert build_network_attachment_argv(
            runtime="docker",
            network="approved-egress",
            container_id="a" * 64,
        ) == [
            "docker",
            "network",
            "connect",
            "approved-egress",
            "a" * 64,
        ]

    def test_inspection_and_cleanup_use_exact_resource_identities(self):
        container_id = "b" * 64
        network_id = "c" * 64

        assert build_resource_inspect_argv(
            runtime="docker", resource_kind="container", resource_id=container_id
        ) == ["docker", "container", "inspect", container_id]
        assert build_resource_inspect_argv(
            runtime="docker", resource_kind="network", resource_id=network_id
        ) == ["docker", "network", "inspect", network_id]
        assert build_container_cleanup_argv(
            runtime="docker", container_ids=(container_id,)
        ) == ["docker", "container", "rm", "--force", container_id]
        assert build_network_cleanup_argv(runtime="docker", network_id=network_id) == [
            "docker",
            "network",
            "rm",
            network_id,
        ]

    @pytest.mark.parametrize(
        ("builder", "kwargs"),
        [
            (
                build_per_run_network_argv,
                {"network": "--internal", "owner": "owner"},
            ),
            (
                build_network_attachment_argv,
                {"network": "egress", "container_id": ""},
            ),
            (
                build_resource_inspect_argv,
                {"resource_kind": "volume", "resource_id": "item"},
            ),
            (build_container_cleanup_argv, {"container_ids": ()}),
            (build_network_cleanup_argv, {"network_id": "--all"}),
        ],
    )
    def test_builders_refuse_ambiguous_or_option_shaped_targets(self, builder, kwargs):
        with pytest.raises(ValueError):
            builder(runtime="docker", **kwargs)


class TestBuildRunArgvPromptHandoff:
    def test_command_appended_after_image(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="42",
            network="open",
            runner="claude-code",
            command=["--print", "--model", "sonnet"],
        )
        img_i = argv.index("kew-agent:latest")
        # Container command must follow the image, in order.
        assert argv[img_i + 1 :] == ["--print", "--model", "sonnet"]

    def test_entrypoint_override_before_image(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="1",
            network="open",
            runner="claude-code",
            entrypoint="cat",
            command=[PROMPT_MOUNT_PATH],
        )
        assert "--entrypoint" in argv
        ep_i = argv.index("--entrypoint")
        assert argv[ep_i + 1] == "cat"
        # --entrypoint must come before the image, the command after it.
        assert ep_i < argv.index("kew-agent:latest")

    def test_no_command_keeps_image_last(self, _checkout: Path):
        argv = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=_checkout,
            slug="7",
            network="open",
            runner="claude-code",
        )
        assert argv[-1] == "kew-agent:latest"
        assert "--entrypoint" not in argv

    def test_prompt_mount_path_is_inside_checkout_mount(self):
        # The file the host writes (<checkout>/PROMPT_FILENAME) must appear at
        # PROMPT_MOUNT_PATH given the checkout is mounted at /work.
        assert f"/work/{PROMPT_FILENAME}" == PROMPT_MOUNT_PATH


# ── Agent image staleness (#969) ─────────────────────────────────────────────


def _fake_inspect(created_ts: str) -> subprocess.CompletedProcess[str]:
    """Fake successful docker image inspect output with the given Created timestamp."""
    payload = json.dumps([{"Created": created_ts, "Id": "sha256:abc123"}])
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")


def _fake_git(commit_ts: str) -> subprocess.CompletedProcess[str]:
    """Fake successful git log output with the given commit timestamp."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=commit_ts + "\n", stderr=""
    )


def _failed_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")


class TestCheckAgentImageStaleness:
    """Unit tests for check_agent_image_staleness with faked subprocess output."""

    def test_stale_image_returns_warning(self) -> None:
        # Image built 2024-01-10; Dockerfile last changed 2024-01-15 → stale
        image_ts = "2024-01-10T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is not None
        assert "predates" in result
        assert "docker build" in result
        assert "kew-agent:latest" in result

    def test_fresh_image_returns_none(self) -> None:
        # Image built 2024-01-20; Dockerfile last changed 2024-01-15 → fresh
        image_ts = "2024-01-20T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_equal_timestamps_returns_none(self) -> None:
        # Image built at exactly the same second as Dockerfile commit → fresh
        ts = "2024-01-15T12:00:00.000000000Z"
        git_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(ts), _fake_git(git_ts)],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_image_not_found_returns_none(self) -> None:
        # docker image inspect fails (image absent) → no warning
        with patch("kew.subprocess_utils.run", return_value=_failed_proc()):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_tool_not_registered_returns_none(self) -> None:
        # subprocess_utils.run raises ValueError when runtime not in TOOL_PATHS
        with patch(
            "kew.subprocess_utils.run", side_effect=ValueError("not in allowlist")
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_git_unavailable_returns_none(self) -> None:
        # git log fails (not in a repo) → no warning
        image_ts = "2024-01-10T12:00:00.000000000Z"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _failed_proc()],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_custom_image_warning_does_not_prescribe_stock_rebuild(self) -> None:
        image_ts = "2024-01-10T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        image = "ghcr.io/thossullivan/kew-agent:latest"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", image)
        assert result is not None
        assert image in result
        assert "kew cannot rebuild it" in result
        assert "docker build" not in result

    @pytest.mark.parametrize(
        ("runner", "image"),
        [
            ("claude-code", "kew-agent:hardened"),
            ("opencode", "kew-agent-opencode:site"),
        ],
    )
    def test_custom_local_tag_does_not_prescribe_stock_rebuild(
        self, runner: str, image: str
    ) -> None:
        image_ts = "2024-01-10T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", image, runner=runner)
        assert result is not None
        assert "kew cannot rebuild it" in result
        assert "docker build" not in result

    @pytest.mark.parametrize(
        ("runner", "image", "dockerfile"),
        [
            ("claude-code", "kew-agent:latest", "docker/agent.Dockerfile"),
            (
                "opencode",
                "kew-agent-opencode:latest",
                "docker/agent-opencode.Dockerfile",
            ),
            (
                "claude-code",
                "kew-agent:docker-0123456789ab",
                "docker/agent.Dockerfile",
            ),
            (
                "opencode",
                "kew-agent-opencode:docker-fedcba987654",
                "docker/agent-opencode.Dockerfile",
            ),
        ],
    )
    def test_kew_owned_tag_uses_runner_dockerfile(
        self, runner: str, image: str, dockerfile: str
    ) -> None:
        image_ts = "2024-01-10T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", image, runner=runner)
        assert result is not None
        assert f"docker build -f {dockerfile} -t {image} ." in result

    def test_nanosecond_precision_parsed_correctly(self) -> None:
        # Docker returns 9-digit fractional seconds; must parse without error
        image_ts = "2024-01-10T12:00:00.123456789Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is not None  # stale

    def test_non_utc_git_offset_renders_as_utc(self) -> None:
        # Git %cI can carry a non-UTC offset; the rendered `Z` timestamp must
        # reflect the UTC *instant*, not the local wall-clock relabeled as Z.
        # 2024-01-15T09:00:00-05:00 == 2024-01-15T14:00:00Z.
        image_ts = "2024-01-10T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T09:00:00-05:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is not None
        assert "2024-01-15T14:00:00Z" in result  # converted, not 09:00:00Z

    def test_build_input_change_makes_image_stale(self) -> None:
        # A commit touching a COPY'd build input (the entrypoint) — not the
        # Dockerfile itself — must still flag the image as stale, and the git
        # pathspec must include that input file.
        image_ts = "2024-01-10T12:00:00.000000000Z"
        entrypoint_commit = "2024-01-20T12:00:00+00:00"
        captured: dict[str, object] = {}

        def _spy(argv, *a, **kw):  # type: ignore[no-untyped-def]
            captured["argv"] = argv
            if argv[0] == "git":
                return _fake_git(entrypoint_commit)
            return _fake_inspect(image_ts)

        with patch("kew.subprocess_utils.run", side_effect=_spy):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is not None  # stale
        # The git log pathspec tracks the entrypoint, not just the Dockerfile.
        assert "docker/agent-entrypoint.sh" in captured["argv"]

    def test_malformed_inspect_object_does_not_crash(self) -> None:
        # A top-level JSON object (not docker's array) previously hit data[0] →
        # KeyError, which would escape and abort the dispatch. Must return None.
        obj = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"Created": "2024-01-10T12:00:00Z"}',
            stderr="",
        )
        with patch("kew.subprocess_utils.run", return_value=obj):
            result = check_agent_image_staleness("docker", "kew-agent:latest")
        assert result is None

    def test_evaluate_distinguishes_unknown_from_fresh(self) -> None:
        # A missing image (inspect fails) is `unknown`, NOT a passing `fresh`
        # check — doctor must not report it as "up to date".
        with patch("kew.subprocess_utils.run", return_value=_failed_proc()):
            result = evaluate_agent_image_staleness("docker", "kew-agent:latest")
        assert result.status == "unknown"
        assert result.warning is None
        assert result.detail is not None

    def test_evaluate_reports_fresh(self) -> None:
        image_ts = "2024-01-20T12:00:00.000000000Z"
        dockerfile_ts = "2024-01-15T12:00:00+00:00"
        with patch(
            "kew.subprocess_utils.run",
            side_effect=[_fake_inspect(image_ts), _fake_git(dockerfile_ts)],
        ):
            result = evaluate_agent_image_staleness("docker", "kew-agent:latest")
        assert result.status == "fresh"
        assert result.warning is None


# ── Layer 1: the container entrypoint contract ───────────────────────────────


class TestAgentEntrypointContract:
    def _entrypoint_path(self) -> Path:
        # tests/ -> kew/ -> repo root -> docker/agent-entrypoint.sh
        return Path(__file__).resolve().parents[2] / "docker" / "agent-entrypoint.sh"

    def test_entrypoint_reads_prompt_file_into_claude_stdin(self):
        text = self._entrypoint_path().read_text(encoding="utf-8")
        # Same prompt file both layers agree on.
        assert PROMPT_MOUNT_PATH in text
        # Feeds the file to `claude --print` on stdin (the #695 fix), never argv.
        assert "claude --print" in text
        assert "command -v claude" in text
        assert 'exec "$AGENT_PROGRAM" --print "$@" <"$PROMPT_FILE"' in text

    def test_entrypoint_fails_loudly_on_missing_prompt(self):
        text = self._entrypoint_path().read_text(encoding="utf-8")
        assert "-s" in text  # `[[ ! -s "$PROMPT_FILE" ]]` empty/missing guard
        assert "exit 64" in text

    def test_entrypoint_bootstraps_git_for_the_tmpfs_home(self):
        # #742: fresh tmpfs HOME has no ~/.gitconfig, and /work is a bind
        # mount owned by a foreign uid — without these three lines the agent's
        # `git commit` dies on dubious-ownership / unknown-identity and the
        # completed work never lands (the kew-721 incident).
        text = self._entrypoint_path().read_text(encoding="utf-8")
        assert "safe.directory /work" in text
        assert "user.name" in text
        assert "user.email" in text


# ── Layer 3: _run_sandboxed writes the prompt file + passes claude flags ──────


class TestRunSandboxedPromptHandoff:
    def test_writes_prompt_file_and_passes_print_flag(self, tmp_path: Path):
        from kew import dispatch_core
        from kew.config import DispatchConfig

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        config = DispatchConfig()
        config.sandbox.enabled = True
        config.logging.dir = str(tmp_path / "logs")

        captured: dict[str, object] = {}

        def _fake_launch(
            argv: list[str],
            *,
            admission,
            on_spawn,
            prepare,
            env,
            credential,
            separate_stderr=False,
        ):
            # A claude-code route mints no bounded key, and the provisioning
            # credential never reaches a launch environment.
            assert credential is None
            assert "OPENROUTER_PROVISIONING_KEY" not in env
            captured["env"] = env
            captured["argv"] = argv
            # The container reads the mounted prompt file while it runs — capture
            # its contents now, since the host deletes it after exit (#725).
            captured["prompt"] = (checkout / PROMPT_FILENAME).read_text(
                encoding="utf-8"
            )
            handle = MagicMock()
            handle.__iter__.return_value = iter([])
            handle.wait.return_value = 1  # nonzero => skip _push_and_open_pr
            return handle

        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", side_effect=_fake_launch),
            patch(
                "kew.dispatch_sandbox._contained_auth_path",
                return_value="workload-identity",
            ),
        ):
            from kew.run_store import SqliteRunStore

            with SqliteRunStore(":memory:") as _store:
                dispatch_core._run_sandboxed(
                    prompt="do the thing",
                    checkout_dir=checkout,
                    config=config,
                    image=SANDBOX_IMAGE,
                    runner=SANDBOX_RUNNER,
                    issue_num=5,
                    item_id="5",
                    branch="agent/issue-5",
                    store=_store,
                )

        # Host wrote the prompt into the mounted checkout dir for the container.
        assert captured["prompt"] == "do the thing"
        # ...and deleted it host-side after exit so it never reaches a PR branch.
        assert not (checkout / PROMPT_FILENAME).exists()
        # Prompt does NOT ride in argv; the entrypoint reads --print from stdin.
        argv = captured["argv"]
        assert "do the thing" not in argv
        assert "--print" in argv or "--dangerously-skip-permissions" in argv

    def test_sandboxed_run_leaves_no_prompt_in_git(self, tmp_path: Path, monkeypatch):
        """#725: after a mocked-container sandboxed run the checkout has no
        prompt file, and a simulated `git add -A` stages nothing prompt-related
        — both the host-side delete AND the worktree-local exclude hold."""
        import shutil as _shutil
        import subprocess as _sp

        from kew import dispatch_core
        from kew.config import DispatchConfig
        from kew.dispatch_core import _write_prompt_exclude
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> str:
            return _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        # A real checkout with a committed file and the worktree-local exclude
        # kew writes at materialization.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        g(checkout, "init", "-b", "main")
        (checkout / "keep.txt").write_text("keep\n")
        g(checkout, "add", "-A")
        g(checkout, "commit", "-m", "init")
        _write_prompt_exclude(checkout)

        config = DispatchConfig()
        config.sandbox.enabled = True
        config.logging.dir = str(tmp_path / "logs")

        def _fake_launch(
            argv: list[str],
            *,
            admission,
            on_spawn,
            prepare,
            env,
            credential,
            separate_stderr=False,
        ):
            assert credential is None
            assert "OPENROUTER_PROVISIONING_KEY" not in env
            # Container "ran" and read the prompt file — but even if a rogue
            # agent left it behind, the exclude must keep it out of the index.
            assert (checkout / PROMPT_FILENAME).exists()
            handle = MagicMock()
            handle.__iter__.return_value = iter([])
            handle.wait.return_value = 1  # nonzero => skip _push_and_open_pr
            return handle

        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", side_effect=_fake_launch),
            patch(
                "kew.dispatch_sandbox._contained_auth_path",
                return_value="workload-identity",
            ),
        ):
            from kew.run_store import SqliteRunStore

            with SqliteRunStore(":memory:") as _store:
                dispatch_core._run_sandboxed(
                    prompt="do the thing",
                    checkout_dir=checkout,
                    config=config,
                    image=SANDBOX_IMAGE,
                    runner=SANDBOX_RUNNER,
                    issue_num=9,
                    item_id="9",
                    branch="agent/issue-9",
                    store=_store,
                )

        # Host deleted the prompt file on exit.
        assert not (checkout / PROMPT_FILENAME).exists()

        # Belt-and-suspenders: recreate it and confirm `git add -A` still stages
        # nothing prompt-related (the exclude covers arbitrary user repos).
        (checkout / PROMPT_FILENAME).write_text("secret prompt\n")
        g(checkout, "add", "-A")
        staged = g(checkout, "diff", "--cached", "--name-only")
        assert PROMPT_FILENAME not in staged


# ── kew sandbox build ────────────────────────────────────────────────────────


class TestPackagedImageBuild:
    @pytest.mark.parametrize(
        ("runner", "dockerfile"),
        [
            ("claude-code", "agent.Dockerfile"),
            ("opencode", "agent-opencode.Dockerfile"),
        ],
    )
    def test_builder_uses_the_packaged_runner_context(self, runner, dockerfile):
        from kew.sandbox import (
            _expected_packaged_image_pin,
            agent_image_inputs_digest,
            build_agent_image,
            packaged_agent_image_context,
        )

        built = subprocess.CompletedProcess([], 0, "", "")
        context = packaged_agent_image_context()
        image = _expected_packaged_image_pin(context, runner)
        with patch("kew.subprocess_utils.run", return_value=built) as run:
            result = build_agent_image("docker", image, runner=runner)

        assert result is built
        argv = run.call_args.args[0]
        # The ten explicit proxy build-args (both spellings of five names) sit
        # between `build` and `-f`.
        proxy_args = argv[2:22]
        assert proxy_args[::2] == ["--build-arg"] * 10
        names = [a.split("=", 1)[0] for a in proxy_args[1::2]]
        assert names == [
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "FTP_PROXY",
            "ftp_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ]
        assert argv[:2] + argv[22:] == [
            "docker",
            "build",
            "-f",
            str(context / "docker" / dockerfile),
            "-t",
            image,
            # The image carries the full digest of the inputs it was built
            # from, which a launch of the pin tag reads back (#3160).
            "--label",
            f"dev.kew.agent-image-inputs={agent_image_inputs_digest(context)}",
            str(context),
        ]

    @pytest.mark.parametrize(
        "image",
        ["kew-agent:latest", "evil.example/agent:tampered"],
    )
    def test_builder_refuses_unpinned_tags_without_opt_in(self, image):
        from kew.sandbox import build_agent_image

        with (
            patch("kew.subprocess_utils.run") as run,
            pytest.raises(ValueError, match="--unpinned"),
        ):
            build_agent_image("docker", image, runner="claude-code")

        run.assert_not_called()

    def test_pin_tag_must_name_the_packaged_digest(self):
        from kew.sandbox import build_agent_image

        with (
            patch("kew.subprocess_utils.run") as run,
            pytest.raises(ValueError) as exc_info,
        ):
            build_agent_image(
                "docker",
                "kew-agent:docker-000000000000",
                runner="claude-code",
            )

        message = str(exc_info.value)
        assert "kew-agent:docker-000000000000" in message
        assert "kew-agent:docker-" in message
        run.assert_not_called()

    def test_packaged_context_refuses_symlinked_inputs(self, tmp_path, monkeypatch):
        import kew.sandbox as sandbox

        module = tmp_path / "sandbox.py"
        docker = tmp_path / "_docker" / "docker"
        docker.mkdir(parents=True)
        target = tmp_path / "outside"
        target.write_text("outside")
        (docker / "agent.Dockerfile").symlink_to(target)
        monkeypatch.setattr(sandbox, "__file__", str(module))

        with pytest.raises(ValueError, match="symbolic link in packaged image context"):
            sandbox.packaged_agent_image_context()

    def test_packaged_context_refuses_a_nested_symlink(self, tmp_path, monkeypatch):
        import shutil

        import kew.sandbox as sandbox

        installed = sandbox.packaged_agent_image_context()
        shutil.copytree(installed, tmp_path / "_docker")
        nested = tmp_path / "_docker" / "docker" / "nested"
        nested.mkdir()
        (nested / "input").symlink_to(installed / "docker" / "opencode.json")
        monkeypatch.setattr(sandbox, "__file__", str(tmp_path / "sandbox.py"))

        with pytest.raises(ValueError, match="symbolic link in packaged image context"):
            sandbox.packaged_agent_image_context()

    def test_packaged_context_refuses_an_unexpected_regular_file(
        self, tmp_path, monkeypatch
    ):
        import shutil

        import kew.sandbox as sandbox

        installed = sandbox.packaged_agent_image_context()
        shutil.copytree(installed, tmp_path / "_docker")
        (tmp_path / "_docker" / "docker" / "unexpected.txt").write_text("extra")
        monkeypatch.setattr(sandbox, "__file__", str(tmp_path / "sandbox.py"))

        with pytest.raises(ValueError, match="manifest.*unexpected"):
            sandbox.packaged_agent_image_context()

    def test_packaged_context_refuses_an_executable_bit_mismatch(
        self, tmp_path, monkeypatch
    ):
        import shutil
        import stat

        import kew.sandbox as sandbox

        installed = sandbox.packaged_agent_image_context()
        shutil.copytree(installed, tmp_path / "_docker")
        dockerfile = tmp_path / "_docker" / "docker" / "agent.Dockerfile"
        dockerfile.chmod(stat.S_IMODE(dockerfile.stat().st_mode) | stat.S_IXUSR)
        monkeypatch.setattr(sandbox, "__file__", str(tmp_path / "sandbox.py"))

        with pytest.raises(ValueError, match="executable bit"):
            sandbox.packaged_agent_image_context()

    def test_special_packaged_input_is_refused_before_runtime_action(
        self, tmp_path, monkeypatch
    ):
        import os
        import shutil

        import kew.sandbox as sandbox

        installed = sandbox.packaged_agent_image_context()
        shutil.copytree(installed, tmp_path / "_docker")
        os.mkfifo(tmp_path / "_docker" / "docker" / "device-input")
        monkeypatch.setattr(sandbox, "__file__", str(tmp_path / "sandbox.py"))

        with (
            patch(
                "kew.sandbox.detect_runtime",
                side_effect=AssertionError("runtime action must happen second"),
            ),
            pytest.raises(ValueError, match="unsupported entry type"),
        ):
            sandbox.build_sandbox_image(SandboxConfig())

    def test_network_absence_is_observed_without_creating_it(self):
        from kew.sandbox import allowlist_network_exists

        absent = subprocess.CompletedProcess([], 1, "", "not found")
        with patch("kew.subprocess_utils.run", return_value=absent) as run:
            assert allowlist_network_exists("docker") is False

        assert run.call_count == 1
        assert run.call_args.args[0] == [
            "docker",
            "network",
            "inspect",
            ALLOWLIST_NETWORK,
        ]

    def test_network_presence_is_observed_without_modifying_it(self):
        from kew.sandbox import allowlist_network_exists

        present = subprocess.CompletedProcess([], 0, "[]", "")
        with patch("kew.subprocess_utils.run", return_value=present) as run:
            assert allowlist_network_exists("docker") is True

        assert run.call_count == 1

    @pytest.mark.parametrize(
        ("route", "certified", "expected"),
        [
            ("sidecar", True, "sidecar-certified"),
            ("direct", False, "operator-managed-unverified"),
            ("direct", True, "operator-managed-unverified"),
            ("sidecar", False, "unavailable"),
        ],
    )
    def test_egress_verification_maps_each_resolved_network_selection(
        self,
        route: str,
        certified: bool,
        expected: str,
    ) -> None:
        import kew.sandbox as sandbox

        classify = getattr(sandbox, "classify_egress_verification", None)
        assert classify is not None, "the pure egress classifier must exist"
        assert (
            classify(
                route=route,
                certified=certified,
            )
            == expected
        )

    @pytest.mark.parametrize(
        "inspection",
        [
            subprocess.CompletedProcess([], 1, "", "network not found"),
            subprocess.CompletedProcess([], 125, "", "daemon unavailable"),
        ],
    )
    def test_absent_or_failed_network_inspection_does_not_verify_egress(
        self, inspection: subprocess.CompletedProcess[str]
    ) -> None:
        import kew.sandbox as sandbox

        classify = getattr(sandbox, "classify_egress_verification", None)
        assert classify is not None, "the pure egress classifier must exist"
        with patch("kew.subprocess_utils.run", return_value=inspection):
            network_available = sandbox.allowlist_network_exists("docker")

        assert network_available is False
        assert classify(route="direct") == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION

    def test_command_builder_uses_effective_tag_and_returns_immutable_id(self):
        from kew.sandbox import (
            ImageIdStatus,
            ImageResolution,
            _expected_packaged_image_pin,
            build_sandbox_image,
            packaged_agent_image_context,
        )

        config = SandboxConfig(runtime="docker", image="local/claude:test")
        expected = _expected_packaged_image_pin(
            packaged_agent_image_context(), "claude-code"
        )
        built = subprocess.CompletedProcess([], 0, "", "")
        image_id = "sha256:" + "a" * 64
        with (
            patch("kew.sandbox._require_build_daemon"),
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox.allowlist_network_exists", return_value=True) as network,
            patch("kew.sandbox.build_agent_image", return_value=built) as build,
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.RESOLVED, image_id=image_id),
            ) as inspect,
        ):
            result = build_sandbox_image(config)

        assert result.tag == expected
        assert result.image_id == image_id
        assert result.allowlist_network_exists is True
        assert result.pinned is True
        network.assert_called_once_with("docker", ALLOWLIST_NETWORK)
        build.assert_called_once_with(
            "docker", expected, runner="claude-code", proxy_environment="none"
        )
        inspect.assert_called_once_with("docker", expected)

    def test_unpinned_opt_in_builds_an_explicit_tag(self):
        from kew.sandbox import (
            ImageIdStatus,
            ImageResolution,
            build_sandbox_image,
        )

        config = SandboxConfig(runtime="docker")
        built = subprocess.CompletedProcess([], 0, "", "")
        image_id = "sha256:" + "b" * 64
        with (
            patch("kew.sandbox._require_build_daemon"),
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox.allowlist_network_exists", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built) as build,
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.RESOLVED, image_id=image_id),
            ),
        ):
            result = build_sandbox_image(
                config,
                runner="opencode",
                tag="local/opencode:test",
                unpinned=True,
            )

        assert result.tag == "local/opencode:test"
        assert result.pinned is False
        build.assert_called_once_with(
            "docker",
            "local/opencode:test",
            runner="opencode",
            unpinned=True,
            proxy_environment="none",
        )

    @pytest.mark.parametrize("tag", ["kew-agent:latest", "custom.example/agent:test"])
    def test_explicit_unpinned_tag_is_refused_before_runtime_action(self, tag):
        from kew.sandbox import build_sandbox_image

        with (
            patch("kew.sandbox.detect_runtime") as detect,
            patch("kew.sandbox.allowlist_network_exists") as network,
            pytest.raises(ValueError, match="--unpinned"),
        ):
            build_sandbox_image(SandboxConfig(), tag=tag)

        detect.assert_not_called()
        network.assert_not_called()

    def test_modified_regular_file_changes_the_default_build_tag(
        self, tmp_path, monkeypatch
    ):
        import shutil

        import kew.sandbox as sandbox
        from kew.sandbox import (
            ImageIdStatus,
            ImageResolution,
            agent_image_inputs_digest,
            packaged_agent_image_context,
        )

        installed = packaged_agent_image_context()
        original = agent_image_inputs_digest(installed)
        shutil.copytree(installed, tmp_path / "_docker")
        modified = tmp_path / "_docker" / "docker" / "opencode.json"
        modified.write_bytes(modified.read_bytes() + b"\n")
        monkeypatch.setattr(sandbox, "__file__", str(tmp_path / "sandbox.py"))
        changed = agent_image_inputs_digest(tmp_path / "_docker")
        assert changed != original

        built = subprocess.CompletedProcess([], 0, "", "")
        image_id = "sha256:" + "c" * 64
        with (
            patch("kew.sandbox._require_build_daemon"),
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox.allowlist_network_exists", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built) as build,
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.RESOLVED, image_id=image_id),
            ),
        ):
            result = sandbox.build_sandbox_image(SandboxConfig())

        expected = f"kew-agent:docker-{changed[:12]}"
        assert result.tag == expected
        build.assert_called_once_with(
            "docker", expected, runner="claude-code", proxy_environment="none"
        )

    def test_build_failure_preserves_bounded_output_tails(self):
        from kew.sandbox import build_sandbox_image

        built = subprocess.CompletedProcess(
            [],
            17,
            "discarded stdout\n" + "o" * 3000 + "stdout marker",
            "discarded stderr\n" + "e" * 3000 + "stderr marker",
        )
        with (
            patch("kew.sandbox._require_build_daemon"),
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox.allowlist_network_exists", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built),
            pytest.raises(RuntimeError) as exc_info,
        ):
            build_sandbox_image(SandboxConfig())

        message = str(exc_info.value)
        assert "stdout marker" in message
        assert "stderr marker" in message
        assert "discarded stdout" not in message
        assert "discarded stderr" not in message
        assert len(message) < 5000

    def test_smoke_refuses_unpinned_opencode_stock_tag(self):
        from kew.sandbox import _ensure_smoke_image

        with (
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image") as build,
        ):
            result = _ensure_smoke_image(
                "docker",
                "kew-agent-opencode:latest",
                runner="opencode",
                build_if_missing=True,
            )

        assert result is not None and not result.ok
        build.assert_not_called()


# ── kew sandbox smoke ────────────────────────────────────────────────────────


class TestRunSmoke:
    def test_runtime_missing_names_runtime_layer(self):
        config = SandboxConfig(runtime="docker")
        with patch("kew.sandbox.detect_runtime", return_value=None):
            result = run_smoke(config)
        assert result.ok is False
        assert result.layer == "runtime"

    def test_image_absent_and_no_build_names_image_layer(self):
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=False),
        ):
            result = run_smoke(config, build_if_missing=False)
        assert result.ok is False
        assert result.layer == "image"

    def test_image_absent_triggers_local_build(self):
        config = SandboxConfig(runtime="docker")
        built = MagicMock()
        built.returncode = 0
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built) as mock_build,
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            handle = MagicMock()
            handle.__iter__.return_value = iter(
                ["kew-sandbox-smoke-ok\n", "kew-sandbox-home-writable\n"]
            )
            handle.wait.return_value = 0
            mock_launch.return_value = handle
            result = run_smoke(config, build_if_missing=True)
        mock_build.assert_called_once()  # local build fallback, no registry pull
        assert result.ok is True
        assert result.layer == "ok"

    def test_build_failure_names_image_layer(self):
        config = SandboxConfig(runtime="docker")
        built = MagicMock()
        built.returncode = 1
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image", return_value=built),
        ):
            result = run_smoke(config, build_if_missing=True)
        assert result.ok is False
        assert result.layer == "image"

    def test_auth_layer_when_container_ok_but_no_credential(self):
        # The #721 shape: plumbing perfect, but a real dispatch would run
        # claude "Not logged in" — the smoke must name the auth layer.
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _without_credential(),
        ):
            handle = MagicMock()
            handle.__iter__.return_value = iter(
                ["kew-sandbox-smoke-ok\n", "kew-sandbox-home-writable\n"]
            )
            handle.wait.return_value = 0
            mock_launch.return_value = handle
            result = run_smoke(config)
        assert result.ok is False
        assert result.layer == "auth"
        assert "setup-token" in result.detail

    def test_a_gateway_runtime_fails_the_launch_layer_rather_than_tracebacking(self):
        """`kew sandbox smoke` has no container command line to smoke there.

        The build site now refuses `runtime = "openshell"` instead of spelling
        `openshell run ...`, and the refusal is a `SandboxRuntimeUnavailable`
        rather than the proxy subclass this handler used to name — a diagnostic
        must report that as a failed layer, not raise out of the command.
        """
        config = SandboxConfig(
            runtime="openshell",
            openshell={"providers": {"claude-code": "kew-claude-code"}},  # type: ignore[arg-type]
        )
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/openshell"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
        ):
            result = run_smoke(config)
        mock_launch.assert_not_called()
        assert result.ok is False
        assert result.layer == "launch"
        assert "command line" in result.detail

    def test_execution_layer_on_bad_echo(self):
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
        ):
            handle = MagicMock()
            handle.__iter__.return_value = iter(["nothing useful\n"])
            handle.wait.return_value = 0
            mock_launch.return_value = handle
            result = run_smoke(config)
        assert result.ok is False
        assert result.layer == "execution"

    def test_smoke_launches_through_build_run_argv(self):
        # The smoke must go through kew's OWN build_run_argv/launch_container
        # (the code path a real dispatch uses), not a hand-rolled docker call.
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            handle = MagicMock()
            handle.__iter__.return_value = iter(
                ["kew-sandbox-smoke-ok\n", "kew-sandbox-home-writable\n"]
            )
            handle.wait.return_value = 0
            mock_launch.return_value = handle
            result = run_smoke(config)
        assert result.ok is True
        argv = mock_launch.call_args.args[0]
        assert argv[0] == "docker"
        assert "--entrypoint" in argv
        # The prompt path now rides inside the smoke's sh -c script.
        assert any(PROMPT_MOUNT_PATH in a for a in argv)

    def test_execution_layer_when_home_not_writable(self):
        # Prompt echo works but the HOME probe never emits its token — the
        # #742 shape (EROFS on ~/.claude): name the execution layer with the
        # writable-HOME hint.
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            handle = MagicMock()
            handle.__iter__.return_value = iter(["kew-sandbox-smoke-ok\n"])
            handle.wait.return_value = 0
            mock_launch.return_value = handle
            result = run_smoke(config)
        assert result.ok is False
        assert result.layer == "execution"
        assert "HOME" in result.detail


class TestRunSmokeIsRunnerAware:
    """#1144 review: the smoke is the operator's only pre-dispatch containment
    proof, so it must prove the image DISPATCH will launch — resolved through
    the same runner-aware resolver, not read off ``config.image``."""

    def _handle(self) -> MagicMock:
        handle = MagicMock()
        handle.__iter__.return_value = iter(
            ["kew-sandbox-smoke-ok\n", "kew-sandbox-home-writable\n"]
        )
        handle.wait.return_value = 0
        return handle

    def test_mapped_claude_image_is_the_one_smoked(self):
        # With [sandbox.images] claude-code set, dispatch launches THAT image —
        # smoking config.image instead would prove an image that never runs.
        config = SandboxConfig(
            runtime="docker", images={"claude-code": "ghcr.io/acme/claude:pinned"}
        )
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            mock_launch.return_value = self._handle()
            result = run_smoke(config)
        assert result.ok is True
        assert "ghcr.io/acme/claude:pinned" in mock_launch.call_args.args[0]
        assert SANDBOX_IMAGE not in mock_launch.call_args.args[0]

    def test_non_claude_runner_can_be_smoked(self):
        # The epic's operator wave needs to prove a mapped opencode image
        # BEFORE routing simple-tier work at it; smoke could not target one.
        config = SandboxConfig(
            runtime="docker", images={"opencode": "ghcr.io/acme/opencode:v1"}
        )
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            mock_launch.return_value = self._handle()
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                result = run_smoke(config, runner="opencode")
        assert "ghcr.io/acme/opencode:v1" in mock_launch.call_args.args[0]
        # Plumbing is fine; auth fails because opencode's OWN credential is
        # absent — claude's being set does not answer opencode's question, and
        # the guidance names OpenRouter, not `claude setup-token` (#1146).
        assert result.ok is False
        assert result.layer == "auth"
        assert "opencode" in result.detail
        assert "OPENROUTER_API_KEY" in result.detail
        assert "setup-token" not in result.detail
        # #1985: the env-only check cannot see a key kew mints at dispatch.
        assert "receives its OPENROUTER_API_KEY at dispatch" in result.detail

    def test_undeclared_runner_names_the_auth_layer(self):
        # lemonade declares no in-container credential, so the smoke fails at
        # auth for the same reason dispatch refuses it: nothing is forwarded.
        config = SandboxConfig(
            runtime="docker", images={"lemonade": "ghcr.io/acme/lemonade:v1"}
        )
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            mock_launch.return_value = self._handle()
            result = run_smoke(config, runner="lemonade")
        assert result.ok is False
        assert result.layer == "auth"
        assert "declares no in-container credential" in result.detail

    def test_unmapped_runner_names_the_image_layer_without_launching(self):
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox.launch_container") as mock_launch,
            _with_credential(),
        ):
            result = run_smoke(config, runner="opencode")
        assert result.ok is False
        assert result.layer == "image"
        assert "images.opencode" in result.detail
        mock_launch.assert_not_called()

    def test_absent_non_claude_image_is_never_built_from_the_claude_dockerfile(self):
        # docker/agent.Dockerfile builds the claude CLI in. Building it under
        # another runner's tag would ship an image whose tag lies about what is
        # inside it — the #1104 mismatch with extra steps.
        config = SandboxConfig(
            runtime="docker", images={"opencode": "ghcr.io/acme/opencode:v1"}
        )
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=False),
            patch("kew.sandbox.build_agent_image") as mock_build,
            _with_credential(),
        ):
            result = run_smoke(config, runner="opencode", build_if_missing=True)
        mock_build.assert_not_called()
        assert result.ok is False
        assert result.layer == "image"

    def test_claude_auth_layer_still_names_the_mint_command(self):
        # The #721 message must survive the runner-keying: an operator with no
        # credential needs the exact command, not a generic complaint.
        config = SandboxConfig(runtime="docker")
        with (
            patch("kew.sandbox.detect_runtime", return_value="/usr/bin/docker"),
            patch("kew.sandbox._image_exists_locally", return_value=True),
            patch("kew.sandbox.launch_container") as mock_launch,
            _without_credential(),
        ):
            mock_launch.return_value = self._handle()
            result = run_smoke(config)
        assert result.layer == "auth"
        assert "setup-token" in result.detail
        assert "ANTHROPIC_API_KEY" in result.detail


class TestRefusalAttestation:
    """#1144 review: `refused` covers several distinct causes, so each refusal
    carries a machine-readable cause + the containment facts it actually saw.
    Hardcoding runtime=None made "this host cannot contain" and "kew cannot
    contain this runner" byte-identical on the ledger — including `enabled`."""

    def test_unmapped_image_refusal_reports_the_present_runtime(self):
        config = SandboxConfig(runtime="docker", enabled="auto")
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            _with_credential(),
            pytest.raises(SandboxImageUnmapped) as exc_info,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        exc = exc_info.value
        assert exc.reason == "image_unmapped"
        assert exc.runtime == "docker"  # a runtime WAS present — attest that
        assert exc.image is None

    def test_undeclared_credential_refusal_reports_runtime_and_image(self):
        # A containable runner with the credential lookup emptied — see
        # TestRunnerAwareImageResolution for why this is no longer lemonade.
        config = SandboxConfig(
            runtime="docker",
            enabled="auto",
            images={"opencode": "ghcr.io/acme/opencode:v1"},
        )
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch("kew.sandbox.runner_credential_env_vars", return_value=()),
            _with_credential(),
            pytest.raises(SandboxCredentialUndeclared) as exc_info,
        ):
            resolve_effective_sandbox(config, runner="opencode")
        exc = exc_info.value
        assert exc.reason == "credential_undeclared"
        assert exc.runtime == "docker"
        assert exc.image == "ghcr.io/acme/opencode:v1"

    def test_missing_credential_refusal_is_its_own_cause(self):
        config = SandboxConfig(runtime="docker", enabled=True)
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc_info,
        ):
            resolve_effective_sandbox(config, runner="claude-code")
        exc = exc_info.value
        assert exc.reason == "credential_missing"
        assert exc.runtime == "docker"
        assert exc.image == SANDBOX_IMAGE

    def test_missing_runtime_refusal_reports_no_runtime(self):
        config = SandboxConfig(runtime="docker", enabled=True)
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value=None),
            _with_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc_info,
        ):
            resolve_effective_sandbox(config, runner="claude-code")
        exc = exc_info.value
        assert exc.reason == "runtime_unavailable"
        assert exc.runtime is None
        assert exc.image is None

    def test_every_cause_is_distinguishable(self):
        """The point of the token: four causes, four values."""
        from kew.sandbox import (
            REFUSAL_CREDENTIAL_MISSING,
            REFUSAL_CREDENTIAL_UNDECLARED,
            REFUSAL_IMAGE_UNMAPPED,
            REFUSAL_PROXY_UNPARSEABLE,
            REFUSAL_RUNTIME_UNAVAILABLE,
        )

        tokens = {
            REFUSAL_RUNTIME_UNAVAILABLE,
            REFUSAL_IMAGE_UNMAPPED,
            REFUSAL_CREDENTIAL_UNDECLARED,
            REFUSAL_CREDENTIAL_MISSING,
            REFUSAL_PROXY_UNPARSEABLE,
        }
        assert len(tokens) == 5


class TestUncontainedFlowPosture:
    """W1.1 node 3d: review and resolve must not be silent about containment.

    They once consulted no posture at all — an operator with
    `[sandbox] enabled = true` got fail-closed containment on dispatch and
    silent host execution here, with nothing on the ledger to say so.

    The posture is now DECIDED by the caller and recorded here. It used to be
    derived, with the reason hardcoded to "has no contained path" — true when
    written, and false the moment these flows gained one.
    """

    def _emitted(self, monkeypatch) -> list[dict]:
        events: list[dict] = []

        def _capture(store, event, **kwargs):
            events.append({"event": event, **kwargs})

        monkeypatch.setattr("kew.audit.emit", _capture)
        return events

    def test_an_uncontained_flow_is_attested_not_silent(self, monkeypatch):
        events = self._emitted(monkeypatch)
        posture = attest_flow_posture(
            None,
            flow="review remediation",
            runner="claude-code",
            config=SandboxConfig(enabled="auto"),
            posture=POSTURE_UNCONTAINABLE_FLOW,
            reason="sandbox runtime 'docker' not found",
            repo="o/r",
            issue_num=42,
        )
        assert posture == POSTURE_UNCONTAINABLE_FLOW
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["posture"] == POSTURE_UNCONTAINABLE_FLOW
        # The caller's SPECIFIC obstacle reaches the ledger. A missing runtime
        # is the operator's to fix and an unmapped image is kew's;
        # "uncontained" alone cannot tell them apart.
        assert payload["reason"] == "sandbox runtime 'docker' not found"
        assert payload["image"] is None
        assert payload["admission"] == "prerequisite_gap"

    def test_a_contained_flow_records_its_image_and_runtime(self, monkeypatch):
        """The half that could not be expressed before these flows were wired."""
        events = self._emitted(monkeypatch)
        posture = attest_flow_posture(
            None,
            flow="review remediation",
            runner="claude-code",
            config=SandboxConfig(enabled="auto", runtime="docker"),
            posture="sandboxed",
            image="ghcr.io/example/agent:latest",
        )
        assert posture == "sandboxed"
        payload = events[0]["payload"]
        assert payload["image"] == "ghcr.io/example/agent:latest"
        assert payload["runtime"] == "docker"
        assert payload["reason"] is None

    def test_a_contained_flow_does_not_warn(self, monkeypatch, capsys):
        """There is nothing to warn about, and a warning that always fires is noise."""
        self._emitted(monkeypatch)
        attest_flow_posture(
            None,
            flow="review remediation",
            runner="claude-code",
            config=SandboxConfig(enabled=True, runtime="docker"),
            posture="sandboxed",
            image="img",
        )
        assert capsys.readouterr().err == ""  # _warn writes to stderr

    def test_a_natively_contained_runner_is_recorded_as_contained(self, monkeypatch):
        """Codex contains itself, so this flow is not a gap for it."""
        events = self._emitted(monkeypatch)
        posture = attest_flow_posture(
            None,
            flow="review remediation",
            runner="codex",
            config=SandboxConfig(enabled="auto", runtime="docker"),
            posture="native",
        )
        assert posture == "native"
        payload = events[0]["payload"]
        assert payload["reason"] is None
        # A native runner never enters a container. Recording the host's
        # runtime here would assert the wrong boundary on the ledger — and a
        # configured runtime is exactly when that mistake is invisible.
        assert payload["runtime"] is None
        assert payload["image"] is None

    def test_policy_exception_is_named_in_the_warning(self, monkeypatch, capsys):
        """The warning names the explicit permission for host fallback."""
        self._emitted(monkeypatch)
        attest_flow_posture(
            None,
            flow="conflict resolution",
            runner="claude-code",
            config=SandboxConfig(enabled=True, allow_uncontained_flows=True),
            posture=POSTURE_UNCONTAINABLE_FLOW,
            reason="sandbox runtime 'docker' not found",
        )
        warned = capsys.readouterr().err  # _warn writes to stderr
        assert "no container boundary" in warned
        assert "allow_uncontained_flows = true" in warned

    def test_the_warning_names_the_specific_obstacle(self, monkeypatch, capsys):
        self._emitted(monkeypatch)
        attest_flow_posture(
            None,
            flow="conflict resolution",
            runner="pi",
            config=SandboxConfig(enabled="auto"),
            posture=POSTURE_UNCONTAINABLE_FLOW,
            reason="no [sandbox.images] mapping for runner 'pi'",
        )
        warned = capsys.readouterr().err
        assert "no [sandbox.images] mapping for runner 'pi'" in warned

    def test_no_such_claim_when_containment_was_not_requested(
        self, monkeypatch, capsys
    ):
        self._emitted(monkeypatch)
        attest_flow_posture(
            None,
            flow="conflict resolution",
            runner="claude-code",
            config=SandboxConfig(enabled="auto"),
            posture=POSTURE_UNCONTAINABLE_FLOW,
            reason="sandbox runtime 'docker' not found",
        )
        warned = capsys.readouterr().err  # _warn writes to stderr
        assert "no container boundary" in warned
        assert "enabled = true" not in warned


class TestFlowContainmentLaunchShape:
    """The flows that attest through `resolve_flow_containment` state their shape.

    `kew ci-fix` and graph `plan_run` run their agent as a container argv
    (`dispatch_core._contained_execution_for` -> `build_agent_container_argv`
    -> `build_run_argv`), exactly like the two remediation flows that resolve
    through `kew.remediation_containment`. Resolving with the dispatch path's
    `gateway_launch=True` default put `posture=sandboxed` on the audit chain
    for `runtime = "openshell"` and left the refusal to the argv-build site,
    which is the admit-then-refuse ordering admission exists to prevent
    (PR #2401 review).
    """

    @staticmethod
    def _openshell() -> SandboxConfig:
        return SandboxConfig(
            enabled="auto",
            runtime="openshell",
            openshell={"providers": {"claude-code": "kew-claude-code"}},  # type: ignore[arg-type]
        )

    def test_the_launch_shape_has_no_default(self) -> None:
        """A flow added later must answer the question rather than inherit one.

        The whole bug was a default: every other caller of
        `resolve_transport_sandbox` may take the dispatch path's answer, but
        this one attests what it resolves, so a forgotten argument is a false
        posture on the chain.
        """
        parameter = inspect.signature(resolve_flow_containment).parameters[
            "gateway_launch"
        ]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_an_argv_flow_refuses_before_attesting_sandboxed(self, monkeypatch) -> None:
        """A REACHABLE gateway is what made the bug: the runtime probe succeeds."""
        # The egress-certification gate (#2319) runs in front of the gate
        # under test; opened so this asserts the launch path, not the proof.
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )
        events: list[dict] = []
        monkeypatch.setattr(
            "kew.audit.emit",
            lambda store, event, **kwargs: events.append({"event": event, **kwargs}),
        )
        with (
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _with_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as refused,
        ):
            resolve_flow_containment(
                None,
                flow="CI fix",
                runner="claude-code",
                config=self._openshell(),
                gateway_launch=False,
                run_id="run-1",
            )
        assert refused.value.reason == "openshell-runtime-unavailable"
        assert "command line" in str(refused.value)
        # The refusal IS attested — what must never be is a sandboxed posture.
        assert [event["payload"]["posture"] for event in events] == ["refused"]

    def test_the_argv_refusal_offers_a_remedy_these_flows_actually_have(
        self, monkeypatch
    ) -> None:
        """The flow exception is not a way out of this refusal for these flows.

        `ARGV_LAUNCH_REFUSAL` told the operator to "allow it to run uncontained
        with [sandbox] allow_uncontained_flows = true". The flag is read on
        `kew.remediation_containment`'s host-fallback arm, which `kew review`
        and `kew resolve` take; `kew ci-fix` and graph `plan_run` resolve
        through `resolve_flow_containment`, which has no such arm, so following
        that sentence returned this same refusal (PR #2401 review). The flag's
        documented scope is the remediation flows, so the message was what was
        wrong — asserted here as the behaviour, with the prose pinned only
        where it must not tell an operator to do something inert.
        """
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )
        monkeypatch.setattr("kew.audit.emit", lambda *a, **kw: None)
        config = SandboxConfig(
            enabled=True,
            allow_uncontained_flows=True,
            runtime="openshell",
            openshell={"providers": {"claude-code": "kew-claude-code"}},  # type: ignore[arg-type]
        )
        assert config.uncontained_flows_allowed, "the exception is in effect"
        with (
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _with_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as refused,
        ):
            resolve_flow_containment(
                None,
                flow="CI fix",
                runner="claude-code",
                config=config,
                gateway_launch=False,
                run_id="run-1",
            )
        assert refused.value.reason == "openshell-runtime-unavailable"
        remedy = str(refused.value)
        assert 'runtime = "docker"' in remedy
        # Naming the flag is fine; naming it without saying who it does not
        # help is the defect.
        if "allow_uncontained_flows" in remedy:
            assert "ci-fix" in remedy and "plan run" in remedy

    def test_a_gateway_launch_still_resolves_sandboxed(self, monkeypatch) -> None:
        """The refusal is the flow's shape, not the runtime: N6 admits openshell."""
        from kew import subprocess_utils

        # The egress-certification gate (#2319) runs in front of the gate
        # under test; opened so this asserts the launch path, not the proof.
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )

        def run(cmd, **kwargs):
            payload = json.dumps(
                {
                    "name": "kew-claude-code",
                    "profile": "anthropic",
                    "credentials": ["ANTHROPIC_API_KEY"],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        monkeypatch.setattr(subprocess_utils, "run", run)
        monkeypatch.setattr("kew.audit.emit", lambda *a, **kw: None)
        with (
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _without_credential(),
        ):
            decision = resolve_flow_containment(
                None,
                flow="dispatch",
                runner="claude-code",
                config=self._openshell(),
                gateway_launch=True,
            )
        assert (decision.posture, decision.runtime) == ("sandboxed", "openshell")


class TestContainedAdmissionIdentity:
    """I01: the contained pending-row writer persists the SAME normalized
    admission identity the host writer does, through the same store primitive.

    ``_record_sandbox_start`` lives in ``dispatch_sandbox`` but is the sandbox
    path's admission writer, so its identity contract is asserted here beside
    the rest of the containment surface.
    """

    def _start(self, store, tmp_path, **overrides):
        from kew.dispatch_sandbox import _record_sandbox_start

        kwargs = {
            "issue_num": 0,
            "item_id": "SFD-12",
            "branch": "agent/sfd-12",
            "title": "t",
            "model": "claude-sonnet-4-6",
            "runner_name": "claude-code",
            "log_file": tmp_path / "sandbox.log",
            "tracker": "linear",
            "repo": "acme/widgets",
        }
        kwargs.update(overrides)
        return _record_sandbox_start(store, **kwargs)

    def test_contained_admission_persists_the_composite_identity(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_id = self._start(store, tmp_path)
            identity = store.get_admission_identity(run_id)

        assert identity is not None
        assert identity.run_id == run_id
        assert identity.attempt_number == 1
        assert identity.source_tracker == "linear"
        assert identity.item_id == "SFD-12"
        assert identity.delivery_repo == "acme/widgets"

    def test_github_contained_admission_keeps_the_numeric_item_opaque(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_id = self._start(
                store, tmp_path, issue_num=42, item_id="42", tracker="github"
            )
            identity = store.get_admission_identity(run_id)

        assert identity.source_tracker == "github"
        assert identity.item_id == "42"

    def test_contained_epic_node_is_joined_from_the_reservation(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-12", "reserver")
            run_id = self._start(store, tmp_path)
            identity = store.get_admission_identity(run_id)

        assert identity.epic_ref == "epic-x"
        assert identity.node_id == "SFD-12"

    def test_contained_and_host_admission_share_one_store_primitive(self, tmp_path):
        """Both writers go through ``admit_run_with_identity`` — the guardrail
        that stops the two paths from drifting into two identity shapes."""
        from unittest.mock import MagicMock

        store = MagicMock()
        self._start(store, tmp_path)

        store.admit_run_with_identity.assert_called_once()
        assert not store.try_admit_run.called
        assert not store.save.called

    def test_tracker_mismatch_is_rejected_not_silently_persisted(self, tmp_path):
        from kew.run_store import AdmissionIdentityError, SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            with pytest.raises(AdmissionIdentityError):
                self._start(store, tmp_path, tracker="github")
            assert store.list_by_item("SFD-12") == []

    def test_inference_provider_is_not_accepted_as_the_tracker(self, tmp_path):
        from kew.run_store import AdmissionIdentityError, SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            with pytest.raises(AdmissionIdentityError, match="not a work tracker"):
                self._start(store, tmp_path, tracker="anthropic")
            assert store.list_by_item("SFD-12") == []

    def test_worker_cap_rejection_leaves_no_identity(self, tmp_path):
        from datetime import UTC, datetime

        from kew.dispatch_core import WorkerCapReached
        from kew.run_store import RunRecord, SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(
                RunRecord(
                    run_id="r-live",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    item_id="1",
                )
            )
            with pytest.raises(WorkerCapReached):
                self._start(store, tmp_path, worker_cap=1)
            assert store.list_by_item("SFD-12") == []
            assert store.find_admissions_for_item("linear", "SFD-12") == []


def test_reviewer_launcher_pending_row_has_admission_identity(tmp_path):
    from kew.config import DispatchConfig
    from kew.graph.driver.review_launcher import _record_pending
    from kew.review_dispatch import ReviewerLaunchContext
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.project.repo = "acme/widgets"
    config.tracker.type = "linear"
    ctx = ReviewerLaunchContext(
        worktree=tmp_path / "checkout",
        prompt="review",
        prompt_artifact_sha="sha256:prompt",
        run_id="review-run",
        item_id="SFD-12",
        head_sha="a" * 40,
        log_dir=tmp_path / "logs",
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.reserve_epic_node_item("epic-alpha", "SFD-12", ctx.run_id)
        _record_pending(
            store,
            ctx,
            config,
            tmp_path / "review.log",
            "claude-sonnet-4-6",
            "claude-code",
        )
        identity = store.get_admission_identity(ctx.run_id)

    assert identity is not None
    # Synthetic by taxonomy: adversarial_review is not tracker work, so the
    # identity names no item, tracker, or epic — the reviewed node stays in
    # RunRecord.item_id for accounting, never as admitted work.
    assert identity.is_synthetic
    assert identity.source_tracker is None
    assert identity.item_id is None
    assert identity.epic_ref is None
    assert identity.node_id is None
    assert identity.delivery_repo == "acme/widgets"


class TestResolveImageId:
    """A tag is a pointer; only the resolved ID identifies what ran."""

    def _inspect(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout)

    def test_resolves_the_immutable_id_behind_a_tag(self):
        from kew.sandbox import resolve_image_id

        payload = json.dumps([{"Id": "sha256:" + "a" * 64, "Created": "x"}])
        with patch(
            "kew.subprocess_utils.run", return_value=self._inspect(payload)
        ) as mock_run:
            assert resolve_image_id("docker", "kew-agent:latest") == (
                "sha256:" + "a" * 64
            )
        assert mock_run.call_args.args[0] == [
            "docker",
            "image",
            "inspect",
            "kew-agent:latest",
        ]

    def test_an_unreadable_runtime_resolves_nothing(self):
        from kew.sandbox import resolve_image_id

        cases = [
            self._inspect("", returncode=1),
            self._inspect("not json"),
            self._inspect("[]"),
            self._inspect(json.dumps([{"Created": "x"}])),
        ]
        for result in cases:
            with patch("kew.subprocess_utils.run", return_value=result):
                assert resolve_image_id("docker", "kew-agent:latest") is None

        with patch("kew.subprocess_utils.run", side_effect=OSError("no daemon")):
            assert resolve_image_id("docker", "kew-agent:latest") is None


class TestContainedRealizedRouteProfile:
    """I03C: the contained admission writer retains the boundary it launched.

    Parity with the host writer is the point: both record the same owned facts
    through the same transactional enricher, differing only in the identity a
    container can honestly own — its resolved image ID.
    """

    def _start(self, store, tmp_path, **overrides):
        from kew.dispatch_sandbox import _record_sandbox_start

        kwargs = {
            "issue_num": 0,
            "item_id": "SFD-12",
            "branch": "agent/sfd-12",
            "title": "t",
            "model": "claude-sonnet-4-6",
            "runner_name": "claude-code",
            "log_file": tmp_path / "sandbox.log",
            "tracker": "linear",
            "repo": "acme/widgets",
            "image": "kew-agent:latest",
            "image_id": "sha256:" + "b" * 64,
        }
        kwargs.update(overrides)
        return _record_sandbox_start(store, **kwargs)

    def test_contained_admission_retains_the_resolved_image_id(self, tmp_path):
        from kew.readiness import Provenance, RealizedRouteProfile
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_id = self._start(store, tmp_path)
            row = store.require_route_profile(run_id, 1)
            profile = RealizedRouteProfile.restore(row.profile_json, run_id, 1)

        assert row.containment == "contained"
        assert profile.containment.provenance is Provenance.ENFORCED
        assert row.image_id == "sha256:" + "b" * 64
        assert row.image_ref == "kew-agent:latest"
        assert row.source_provider == "linear"
        assert row.inference_provider == "anthropic"
        assert row.requested_model == "claude-sonnet-4-6"
        # A container owns no host executable identity.
        assert row.runner_digest is None
        assert profile.runner_digest.provenance is Provenance.UNAVAILABLE

    def test_an_unresolvable_image_refuses_the_dispatch_before_any_charge(
        self, tmp_path, _launch_image_resolves
    ):
        """Fail closed (#1550 review F1): a mutable tag alone is never evidence
        of what ran, so an unresolvable ID refuses instead of recording UNKNOWN
        and spending anyway. The refusal happens at the launcher seam, before
        any row exists."""
        from kew.config import DispatchConfig
        from kew.dispatch_core import ContainedImageUnresolvedError

        real_resolve = _launch_image_resolves
        with (
            patch("kew.dispatch_sandbox.detect_runtime_cached", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(ImageIdStatus.ABSENT),
            ),
            pytest.raises(ContainedImageUnresolvedError) as exc,
        ):
            real_resolve(DispatchConfig(), "kew-agent:latest", runner="claude-code")
        # The refusal must be actionable, not merely a refusal: an image that
        # was never built is fixed by a build, and nothing else.
        assert "kew sandbox build --runner claude-code" in str(exc.value)

    def test_the_launcher_binds_argv_to_the_resolved_id(
        self, tmp_path, _launch_image_resolves
    ):
        """The profile's captured ID and the launched image are the same bytes."""
        from kew.config import DispatchConfig

        with (
            patch("kew.dispatch_sandbox.detect_runtime_cached", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "e" * 64
                ),
            ),
        ):
            resolved = _launch_image_resolves(
                DispatchConfig(), "kew-agent:latest", runner="claude-code"
            )
        assert resolved == "sha256:" + "e" * 64

    def test_a_rebuilt_image_changes_the_attempt_fingerprint(self, tmp_path):
        from kew.run_store import SqliteRunStore

        fingerprints = []
        for digest in ("c" * 64, "d" * 64):
            with SqliteRunStore(str(tmp_path / f"runs-{digest[0]}.db")) as store:
                run_id = self._start(store, tmp_path, image_id="sha256:" + digest)
                fingerprints.append(store.require_route_profile(run_id, 1).fingerprint)

        assert fingerprints[0] != fingerprints[1]

    def test_a_caller_with_no_image_records_no_profile_at_all(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_id = self._start(store, tmp_path, image=None, image_id=None)

            assert store.get_route_profile(run_id, 1) is None
            assert store.get_admission_identity(run_id) is not None


class TestResolveImageIdRuntimePath:
    def test_an_absolute_which_resolved_runtime_is_usable(self, monkeypatch):
        """The #1513 incident: detect_runtime returns a which()-resolved
        ABSOLUTE path, which was never a TOOL_PATHS key, so resolution refused
        for the feature's entire life and every caller silently recorded an
        unresolved boundary (pre-#1552 it then launched anyway). An absolute
        path from which() registers under itself."""
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from kew import subprocess_utils
        from kew.sandbox import resolve_image_id

        runtime = "/usr/local/bin/docker"
        monkeypatch.delitem(subprocess_utils.TOOL_PATHS, runtime, raising=False)
        result = MagicMock(returncode=0, stdout='[{"Id": "sha256:" }]')
        result.stdout = '[{"Id": "sha256:%s"}]' % ("a" * 64)
        with _patch.object(subprocess_utils, "run", return_value=result) as run:
            resolved = resolve_image_id(runtime, "kew-agent:x")
        assert resolved == "sha256:" + "a" * 64
        # The path registered under itself so the allowlisted run() accepted it.
        assert subprocess_utils.TOOL_PATHS[runtime] == runtime
        assert run.call_args.args[0][0] == runtime


class TestContainedNoChangeExit:
    """A contained run that changed nothing names the attempt it belongs to.

    The host records ONE durable no-change candidate for a zero-diff exit
    (I10B), and it can only bind that judgment to a run id and an attempt. The
    contained path used to return a bare exit-0 result with neither, so the
    host could not tell "the container found nothing to do" from "exit 0, no
    PR, reason unknown" — and the legacy code closed the item for both.
    """

    def _config(self, tmp_path: Path):
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        return config

    @patch("kew.dispatch_sandbox._checkout_has_no_changes", return_value=True)
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log", return_value=0)
    @patch("kew.dispatch_core._launch_container")
    def test_a_no_change_container_exit_carries_run_id_and_category(
        self, _launch, _stream, mock_push, _no_changes, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(":memory:")
        with patch(
            "kew.dispatch_sandbox._contained_auth_path",
            return_value="workload-identity",
        ):
            result = _run_sandboxed_container(
                prompt="compiled prompt",
                checkout_dir=tmp_path,
                config=self._config(tmp_path),
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                title="Fix it",
                model="claude-sonnet-4-6",
                store=store,
            )

        record = store.list_by_issue(7)[0]
        assert result.exit_code == 0
        assert result.run_id == record.run_id
        assert result.error_category == "sandbox_no_change_needed"
        # Nothing is published and nothing is closed: the outcome is a
        # candidate the host records, not a completion the container claims.
        mock_push.assert_not_called()


_GUARD_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "kew"
    / "_templates"
    / "security-guard.sh"
)


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
@pytest.mark.skipif(not shutil.which("jq"), reason="jq not available")
def test_guard_marker_from_argv_is_honoured_by_the_shipped_template() -> None:
    """The ``-e`` entry build_run_argv emits must be the marker the guard keys on.

    Couples the Python constant to the bash literal in the template (#1691):
    feed the exact NAME=value from argv into the shipped guard and assert a
    ``.claude`` Write inside that worktree is denied, and allowed without it.
    """
    argv = build_run_argv(
        runtime="docker",
        image="example:latest",
        checkout_dir=Path("/tmp/checkout"),  # nosec B108 - argv only
        slug="42",
        network="allowlist",
        runner="claude-code",
    )
    entries = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    marker = next(e for e in entries if e.startswith(f"{AGENT_MARKER_ENV}="))
    name, value = marker.split("=", 1)
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": f"{value}/.claude/settings.json",
                "content": "x",
            },
        }
    )
    base_env = {k: v for k, v in os.environ.items() if k != name}

    def run(env: dict[str, str]) -> str:
        return subprocess.run(
            ["bash", str(_GUARD_TEMPLATE)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        ).stdout.strip()

    assert run({**base_env, name: value}), "marker set: .claude Write must be denied"
    assert not run(base_env), "marker unset: the same Write must be allowed"


# ── S3 (#1431): hard resource limits ─────────────────────────────────────────
# Containment stopped the agent leaving the box; it did nothing about the agent
# taking the host down from inside it. `--pids-limit` was the only cap, so a
# fork bomb was bounded and a memory bomb, a spin loop, a fill-disk and a
# huge-log agent were not. These tests pin every cap that closes that.


class _FakeStdout:
    """A container stdout that records the bound each read was given.

    ``readline(limit)`` is the whole point of the huge-log defense, so the fake
    has to be able to fail the test by ignoring the limit — a MagicMock or a
    bare iterator would pass whatever kew did.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.limits: list[int | None] = []

    def readline(self, limit: int | None = None) -> str:
        self.limits.append(limit)
        if not self._lines:
            return ""
        line = self._lines.pop(0)
        return line if limit is None else line[:limit]


class _SilentStdout:
    """A stdout read that blocks until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def readline(self, limit: int | None = None) -> str:
        self.release.wait(timeout=1)
        return ""


#: The id a handle's launch recorded; a kill targets it, never the name (#1963).
RECORDED_ID = format(42, "064x")


def _handle(lines: list[str], **kwargs) -> tuple[ContainerHandle, MagicMock]:
    """A ContainerHandle over *lines*, with a recorded container id, plus the
    mock its kill() would reach."""
    proc = MagicMock()
    proc.stdout = _FakeStdout(lines)
    kwargs.setdefault("container_id", RECORDED_ID)
    return ContainerHandle(proc, "kew-42", **kwargs), proc


def _clock(readings: list[float]):
    """A fake monotonic clock over *readings* that HOLDS the last value.

    The guard reads the clock more than once before the first line (deadline,
    then the quota-poll seed), so an exhaustible ``iter`` would make these
    tests fail on read COUNT rather than on the elapsed time they are about.
    """
    values = list(readings)

    def now() -> float:
        return values.pop(0) if len(values) > 1 else values[0]

    return now


def _cap_reason(line: str) -> str:
    event = json.loads(line)
    assert event["type"] == RESOURCE_CAP_EVENT
    return event["reason"]


def _agent_argv(checkout: Path) -> list[str]:
    return build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        slug="42",
        network="allowlist",
        runner="claude-code",
    )


def _extract_argv(checkout: Path, artifact: Path) -> list[str]:
    return build_extract_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        artifact_dir=artifact,
        slug="42",
        base_sha="a" * 40,
        uid=501,
        gid=20,
    )


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def _without_per_launch_tokens(argv: list[str]) -> list[str]:
    """*argv* with the two deliberately-fresh-per-launch values masked."""
    masked = list(argv)
    for index, value in enumerate(masked):
        if index and masked[index - 1] == "--cidfile":
            masked[index] = "<cidfile>"
        elif value.startswith(f"{STARTUP_NONCE_ENV}="):
            masked[index] = f"{STARTUP_NONCE_ENV}=<nonce>"
    return masked


class TestContainerResourceCaps:
    @pytest.fixture(autouse=True)
    def _probe_the_scripted_daemon(self, real_daemon_cpu_probe: None) -> None:
        # These tests script the daemon's answer themselves; the suite-wide
        # stub would hide the clamp they exist to prove.
        return None

    def test_memory_preflight_warns_when_runtime_is_smaller(self, capsys):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(1024**3), stderr=""
        )
        with patch("kew.subprocess_utils.run", return_value=result) as run:
            check_runtime_memory_capacity("docker")
        run.assert_called_once_with(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            check=False,
            timeout=10,
        )
        lines = capsys.readouterr().err.splitlines()
        assert len(lines) == 1
        assert "8.00 GiB" in lines[0]
        assert "1.00 GiB" in lines[0]

    def test_memory_preflight_resolves_an_absolute_runtime_through_the_allowlist(
        self, capsys, monkeypatch
    ):
        from kew import subprocess_utils

        monkeypatch.setattr(
            subprocess_utils, "TOOL_PATHS", {"docker": "/opt/bin/docker"}
        )
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(1024**3), stderr=""
        )
        with patch("kew.subprocess_utils.subprocess.run", return_value=result) as run:
            check_runtime_memory_capacity("/usr/local/bin/docker")
        assert run.call_args.args[0][:2] == ["/opt/bin/docker", "info"]
        lines = capsys.readouterr().err.splitlines()
        assert len(lines) == 1 and "8.00 GiB" in lines[0], lines

    def test_memory_preflight_logs_one_skip_when_docker_info_fails(self, capsys):
        with patch("kew.subprocess_utils.run", side_effect=OSError):
            check_runtime_memory_capacity("docker")
        lines = capsys.readouterr().err.splitlines()
        assert len(lines) == 1
        assert "memory preflight skipped" in lines[0]
        assert "continuing" in lines[0]

    def test_agent_argv_caps_memory_and_disables_swap(self, _checkout: Path):
        argv = _agent_argv(_checkout)
        memory = _flag_values(argv, "--memory")
        swap = _flag_values(argv, "--memory-swap")
        assert memory == ["8g"]
        # Equal to --memory is how Docker spells "no swap". With a swap
        # allowance the cgroup survives a memory bomb and the HOST dies of
        # swap thrash instead — the cap would move the failure, not remove it.
        assert swap == memory

    @pytest.mark.parametrize(
        ("daemon_cpu_count", "client_cpu_count", "expected_limit"),
        [(2, 16, "2"), (32, 16, "4")],
    )
    def test_resource_limit_args_clamps_cpu_to_daemon(
        self,
        monkeypatch,
        daemon_cpu_count: int,
        client_cpu_count: int,
        expected_limit: str,
    ):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(daemon_cpu_count), stderr=""
        )
        monkeypatch.setattr("kew.sandbox.os.cpu_count", lambda: client_cpu_count)
        with patch("kew.sandbox.subprocess_utils.run", return_value=result) as run:
            args = resource_limit_args("docker")
        assert _flag_values(args, "--cpus") == [expected_limit]
        run.assert_called_once_with(
            ["docker", "info", "--format", "{{.NCPU}}"],
            check=False,
            timeout=5.0,
        )

    @pytest.mark.parametrize(
        "bad_answer",
        [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="x"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="0", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="-3", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=5.0),
            OSError("no daemon"),
        ],
        ids=["nonzero", "malformed", "zero", "negative", "empty", "timeout", "oserror"],
    )
    def test_an_unanswered_probe_caps_at_one_cpu_and_never_reads_the_client(
        self, monkeypatch, capsys, bad_answer
    ):
        # One CPU is the only cap every daemon accepts. The client's count is
        # never consulted: a 16-CPU client aimed at a 2-CPU VM would otherwise
        # emit a cap the daemon rejects, which means no container at all.
        # The degradation is never silent (A4): the operator is told why.
        monkeypatch.setattr(
            "kew.sandbox.os.cpu_count", lambda: pytest.fail("client count read")
        )
        with patch("kew.sandbox.subprocess_utils.run", side_effect=[bad_answer]):
            assert _flag_values(resource_limit_args("docker"), "--cpus") == ["1"]
        lines = capsys.readouterr().err.splitlines()
        assert len(lines) == 1, lines
        assert "capping the container at 1 CPU" in lines[0]
        assert "docker" in lines[0]

    def test_the_daemon_is_asked_on_every_launch(self, monkeypatch):
        # No cache: a daemon can be restarted, resized or switched between
        # launches, and the previous answer is exactly what the new one rejects.
        failure = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="x"
        )
        two = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="2\n", stderr=""
        )
        eight = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="8", stderr=""
        )
        answers = [failure, two, eight, two]
        with patch("kew.sandbox.subprocess_utils.run", side_effect=answers) as run:
            caps = [
                _flag_values(resource_limit_args("docker"), "--cpus") for _ in answers
            ]
        assert caps == [["1"], ["2"], ["4"], ["2"]]
        assert run.call_count == 4

    def test_podman_is_asked_with_its_own_template(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="3", stderr=""
        )
        with patch("kew.sandbox.subprocess_utils.run", return_value=result) as run:
            assert _flag_values(resource_limit_args("podman"), "--cpus") == ["3"]
        run.assert_called_once_with(
            ["podman", "info", "--format", "{{.Host.CPUs}}"],
            check=False,
            timeout=5.0,
        )

    def test_agent_argv_caps_cpu(self, _checkout: Path, monkeypatch):
        # The launch argv must carry the clamped cap, not the raw ceiling: on
        # a two-CPU runner the daemon rejects --cpus 4 and no container starts.
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="2", stderr=""
        )
        monkeypatch.setattr("kew.sandbox.os.cpu_count", lambda: 16)
        with patch("kew.sandbox.subprocess_utils.run", return_value=result):
            argv = _agent_argv(_checkout)
        assert _flag_values(argv, "--cpus") == ["2"]

    def test_every_cpu_capped_launch_records_creation_in_a_cidfile(
        self, _checkout: Path, tmp_path: Path
    ):
        argvs = [_agent_argv(_checkout), _extract_argv(_checkout, tmp_path)]
        for argv in argvs:
            cidfiles = _flag_values(argv, "--cidfile")
            assert len(cidfiles) == 1
            assert not Path(cidfiles[0]).exists()

    def test_agent_argv_sizes_every_tmpfs(self, _checkout: Path):
        # A tmpfs is host RAM charged to the container's cgroup, so an unsized
        # one is a second, unmetered route to the exhaustion --memory closes.
        mounts = _flag_values(_agent_argv(_checkout), "--tmpfs")
        assert mounts, "the agent container must still have tmpfs mounts"
        for mount in mounts:
            options = mount.split(":", 1)[1].split(",") if ":" in mount else []
            assert any(o.startswith("size=") for o in options), (
                f"unsized tmpfs {mount!r} — host RAM with no cap"
            )

    def test_agent_home_keeps_its_uid_pin_and_exec_alongside_the_size(
        self, _checkout: Path
    ):
        # #742/#762 must survive the #1431 size cap: HOME stays uid-pinned,
        # mode 0700 and exec, or agents lose their writable/runnable HOME.
        mounts = _flag_values(_agent_argv(_checkout), "--tmpfs")
        home = next(m for m in mounts if m.startswith("/home/agent:"))
        options = home.split(":", 1)[1].split(",")
        assert "exec" in options
        assert "uid=1001" in options
        assert "gid=1001" in options
        assert "mode=0700" in options
        assert "size=4g" in options

    def test_agent_argv_pins_the_kernel_rlimits(self, _checkout: Path):
        ulimits = _flag_values(_agent_argv(_checkout), "--ulimit")
        assert "nofile=4096:8192" in ulimits
        # fsize is the single-file write cap — one `dd` cannot fill the
        # bind-mounted checkout's filesystem.
        assert f"fsize={1024**3}" in ulimits
        # No core dumps into the mounted checkout.
        assert "core=0" in ulimits

    def test_no_nproc_rlimit_because_it_is_a_per_host_uid_limit(self, _checkout: Path):
        # Every agent container runs as uid 1001, and --ulimit nproc is a
        # per-UID HOST limit: setting it would make one container's bound
        # apply across all concurrent ones. --pids-limit is the per-container
        # fork-bomb cap and it is asserted separately.
        ulimits = _flag_values(_agent_argv(_checkout), "--ulimit")
        assert not [u for u in ulimits if u.startswith("nproc=")]

    def test_a_fork_bomb_stays_bounded_in_both_containers(
        self, _checkout: Path, tmp_path: Path
    ):
        for argv in (
            _agent_argv(_checkout),
            _extract_argv(_checkout, tmp_path / "artifact"),
        ):
            assert _flag_values(argv, "--pids-limit") == ["1024"]

    def test_extraction_container_carries_every_agent_cap(
        self, _checkout: Path, tmp_path: Path
    ):
        # A cap present on only one of the two containers is a cap an attacker
        # avoids by choosing the other one.
        argv = _extract_argv(_checkout, tmp_path / "artifact")
        caps = resource_limit_args("docker")
        for flag, value in zip(caps[::2], caps[1::2], strict=True):
            assert [flag, value] in [
                argv[i : i + 2] for i, a in enumerate(argv) if a == flag
            ], f"extraction argv is missing {flag} {value}"

    def test_extraction_tmpfs_is_sized_too(self, _checkout: Path, tmp_path: Path):
        argv = _extract_argv(_checkout, tmp_path / "artifact")
        assert _flag_values(argv, "--tmpfs") == ["/tmp:size=1g"]

    def test_each_cap_flag_appears_once_so_none_is_overridden(self, _checkout: Path):
        # Docker takes the LAST occurrence of a repeated flag, so a duplicate
        # is how a cap silently becomes whatever the second copy says.
        argv = _agent_argv(_checkout)
        for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
            assert len(_flag_values(argv, flag)) == 1, f"{flag} appears twice"


#: What the kernel's tmpfs ``size=`` parser accepts: a byte count, optionally
#: with a k/m/g suffix. Notably NOT Docker's ``b`` — ``size=1000000000b`` is a
#: "Bad value" refusal at mount time, which is a refusal with no container.
_KERNEL_TMPFS_SIZE = re.compile(r"[0-9]+[kmgKMG]?")


class TestLowerOnlyResourceCaps:
    """``[sandbox] max_memory``/``max_cpus`` lower a cap and never remove one (#2360).

    The ceilings in ``sandbox.py`` are not knobs, and these do not make them
    knobs: there is no value of either that raises a cap or sets it back to
    "none". What they buy is the operator's own container VM — on a 16 GiB
    OrbStack VM only two agents fit at 8 GiB each, so a batch of review and
    remediation runs oversubscribes the VM and the HOST starts to swap.
    """

    @pytest.fixture(autouse=True)
    def _probe_the_scripted_daemon(self, real_daemon_cpu_probe: None) -> None:
        # Same reason as TestContainerResourceCaps: the suite-wide stub would
        # hide the daemon clamp these tests assert around.
        return None

    @staticmethod
    def _daemon(cpus: int):
        return patch(
            "kew.sandbox.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=str(cpus), stderr=""
            ),
        )

    def test_an_unset_block_reproduces_todays_argv_exactly(self, _checkout: Path):
        # The whole safety property of a lower-only seam: an operator who sets
        # nothing is in exactly the posture that shipped before it existed.
        with self._daemon(16):
            default = _agent_argv(_checkout)
            configured = build_run_argv(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=_checkout,
                slug="42",
                network="allowlist",
                runner="claude-code",
                caps=resource_caps(SandboxConfig()),
            )
        # The cidfile path and the startup nonce are fresh per launch by
        # design, so they are the two tokens compared by shape not by value.
        assert _without_per_launch_tokens(configured) == _without_per_launch_tokens(
            default
        )
        assert _flag_values(default, "--memory") == [HARD_MAX_MEMORY]
        assert _flag_values(default, "--memory-swap") == [HARD_MAX_MEMORY]
        assert _flag_values(default, "--cpus") == [str(HARD_MAX_CPUS)]

    def test_a_lowered_memory_cap_reaches_memory_swap_and_both_tmpfs_mounts(
        self, _checkout: Path
    ):
        # 3g: half to HOME, a quarter to /tmp, so the two mounts together
        # cannot outgrow the memory cgroup they are charged to.
        with self._daemon(16):
            argv = build_run_argv(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=_checkout,
                slug="42",
                network="allowlist",
                runner="claude-code",
                caps=resource_caps(SandboxConfig(max_memory="3g")),
            )
        assert _flag_values(argv, "--memory") == ["3g"]
        # Still equal to --memory — "no swap" has to survive the lowering, or
        # a lowered cap just converts the memory bomb into host IO starvation.
        assert _flag_values(argv, "--memory-swap") == ["3g"]
        mounts = _flag_values(argv, "--tmpfs")
        home = next(m for m in mounts if m.startswith("/home/agent:"))
        assert "size=1536m" in home.split(":", 1)[1].split(",")
        assert "/tmp:size=768m" in mounts
        # #742/#762 survive the resize: HOME stays uid-pinned, 0700 and exec.
        for option in ("exec", "uid=1001", "gid=1001", "mode=0700"):
            assert option in home.split(":", 1)[1].split(",")

    def test_a_lowered_cpu_cap_is_the_argv_value_on_a_larger_daemon(self):
        # The operator's 2 is below the daemon's 10, so the clamp has nothing
        # to do and the lowered value is what the container runs under.
        with self._daemon(10):
            args = resource_limit_args(
                "docker", resource_caps(SandboxConfig(max_cpus=2))
            )
        assert _flag_values(args, "--cpus") == ["2"]

    def test_a_small_daemon_still_clamps_a_lowered_cpu_cap(self):
        # Lowering never overrides the clamp: a cap above the daemon's count is
        # rejected outright, which means no container at all.
        with self._daemon(1):
            args = resource_limit_args(
                "docker", resource_caps(SandboxConfig(max_cpus=3))
            )
        assert _flag_values(args, "--cpus") == ["1"]

    def test_lowering_one_cap_leaves_the_other_at_its_ceiling(self):
        memory_only = resource_caps(SandboxConfig(max_memory="2g"))
        cpu_only = resource_caps(SandboxConfig(max_cpus=1))
        assert (memory_only.memory, memory_only.cpus) == ("2g", HARD_MAX_CPUS)
        assert (cpu_only.memory, cpu_only.cpus) == (HARD_MAX_MEMORY, 1)

    def test_the_derived_tmpfs_sizes_agree_with_the_pinned_defaults(self):
        # resource_caps() returns the module constants for an unset block
        # rather than deriving them, so this is the test that keeps the two
        # branches from drifting: at the ceiling they must be the same answer.
        ceiling = resource_caps(SandboxConfig())
        derived = resource_caps(SandboxConfig(max_memory=HARD_MAX_MEMORY))
        assert derived == ceiling

    @pytest.mark.parametrize(
        ("memory", "home", "tmp"),
        [
            ("8g", "4g", "1g"),
            ("3g", "1536m", "768m"),
            ("1g", "512m", "256m"),
            ("1536m", "768m", "384m"),
        ],
    )
    def test_every_accepted_memory_cap_keeps_both_mounts_under_it(
        self, memory: str, home: str, tmp: str
    ):
        caps = resource_caps(SandboxConfig(max_memory=memory))
        assert (caps.home_tmpfs, caps.tmp_tmpfs) == (home, tmp)
        limit = parse_memory_size(memory)
        assert parse_memory_size(caps.home_tmpfs) <= limit // 2
        assert parse_memory_size(caps.tmp_tmpfs) <= limit // 4

    def test_the_memory_preflight_warns_against_the_lowered_cap(self, capsys):
        # An operator who lowered the cap to fit the VM must stop being warned
        # about the cap they removed — and 2g over a 1 GiB daemon still warns.
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(1024**3), stderr=""
        )
        with patch("kew.subprocess_utils.run", return_value=result):
            check_runtime_memory_capacity("docker", SandboxConfig(max_memory="1g"))
            check_runtime_memory_capacity("docker", SandboxConfig(max_memory="2g"))
        lines = capsys.readouterr().err.splitlines()
        assert len(lines) == 1, lines
        assert "2.00 GiB" in lines[0]

    def test_a_confirmed_oom_names_the_cap_the_container_actually_ran_under(
        self, _checkout: Path
    ):
        # Reading the ceiling here would tell an operator their 3 GiB
        # container died at 8 GiB — a report that blames the wrong number.
        with self._daemon(16):
            argv = build_run_argv(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=_checkout,
                slug="42",
                network="allowlist",
                runner="claude-code",
                caps=resource_caps(SandboxConfig(max_memory="3g")),
            )
        handle = ContainerHandle(
            MagicMock(), "kew-42", "docker", launch_argv=argv, container_id="c" * 64
        )
        with patch.object(
            ContainerHandle,
            "_daemon_saw_oom",
            return_value=OOMProbeResult(OOMProbeStatus.SEEN),
        ):
            handle.note_oom_kill()
        assert handle.resource_cap_reason == (
            f"kew-42: {OOM_CAP_REASON_TEMPLATE.format(limit='3g')}"
        )

    def test_an_unreadable_launch_argv_falls_back_to_the_ceiling(self):
        # The pre-#2360 answer, not silence: a handle with no argv still names
        # a cap rather than reporting an OOM with no limit in it.
        assert oom_cap_reason(None) == OOM_CAP_REASON_TEMPLATE.format(
            limit=HARD_MAX_MEMORY
        )

    def test_the_launch_event_attests_the_caps_the_run_ran_under(self):
        # "What did this run run under?" has to be answerable from the ledger,
        # not from whatever the config file says by the time anyone asks.
        store = MagicMock()
        store.append_audit_event.return_value = 1
        emit_resolved_posture(
            store,
            runner="claude-code",
            config=SandboxConfig(max_memory="3g", max_cpus=2),
            posture="sandboxed",
            runtime="docker",
            image="kew-agent:latest",
            reason=None,
            run_id="run-1",
            checkout_dir=None,
            repo="o/r",
            issue_num=2360,
            item_id=None,
        )
        payload = store.append_audit_event.call_args.kwargs["payload"]
        assert payload["max_memory"] == "3g"
        assert payload["max_cpus"] == 2

    def test_an_unset_block_attests_the_ceilings_rather_than_nothing(self):
        # An operator who configured nothing still ran under a cap, and the
        # ledger has to say which one rather than leaving the field empty.
        payload = self._posture_payload(SandboxConfig(), posture="sandboxed")
        assert payload["max_memory"] == HARD_MAX_MEMORY
        assert payload["max_cpus"] == HARD_MAX_CPUS

    @pytest.mark.parametrize(
        "posture",
        # Every attested posture other than `sandboxed`, `host` included: it is
        # the token `_attest_tracked_posture` records for a tracked launch that
        # resolved no posture and no image, so it reaches this payload too.
        ["uncontained", "uncontained_flow", "native", "host", "refused"],
    )
    def test_a_posture_that_launched_no_container_attests_no_caps(self, posture: str):
        # Only `sandboxed` puts a cgroup around anything. Attesting "8g" beside
        # filesystem_boundary "host" would record a limit nothing enforced, so
        # the caps are None — present (one payload schema, no consumer
        # KeyError), but not a claim.
        payload = self._posture_payload(
            SandboxConfig(
                max_memory="3g", max_cpus=2, dangerously_skip_containment=True
            ),
            posture=posture,
        )
        assert payload["max_memory"] is None
        assert payload["max_cpus"] is None
        assert {"max_memory", "max_cpus"} <= set(payload)

    def test_a_refusal_that_names_its_runtime_and_image_still_attests_no_caps(self):
        # A `credential_missing` refusal records runtime "docker" and the
        # resolved image, because resolution DID see them before refusing. So
        # the null caps are not "whatever runtime and image do" — a populated
        # runtime and image next to a populated cap would be the dangerous
        # payload, attesting a cgroup for a container that never launched.
        payload = self._posture_payload(
            SandboxConfig(max_memory="3g", max_cpus=2),
            posture="refused",
            runtime="docker",
            image="kew-agent:latest",
            reason="credential_missing",
        )
        assert (payload["runtime"], payload["image"]) == ("docker", "kew-agent:latest")
        assert (payload["max_memory"], payload["max_cpus"]) == (None, None)

    @staticmethod
    def _posture_payload(
        config: SandboxConfig,
        *,
        posture: str,
        runtime: str | None = None,
        image: str | None = None,
        reason: str | None = None,
    ) -> dict:
        contained = posture == "sandboxed"
        store = MagicMock()
        store.append_audit_event.return_value = 1
        emit_resolved_posture(
            store,
            runner="claude-code",
            config=config,
            posture=posture,
            runtime=runtime or ("docker" if contained else None),
            image=image or ("kew-agent:latest" if contained else None),
            reason=reason or (None if contained else "runtime_unavailable"),
            run_id="run-2",
            checkout_dir=None,
            repo="o/r",
            issue_num=2360,
            item_id=None,
        )
        return store.append_audit_event.call_args.kwargs["payload"]

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (1024**3, "1g"),
            (1536 * 1024**2, "1536m"),
            (768 * 1024**2, "768m"),
            # Not "1000000000b": a `b` suffix is Docker's grammar, not the
            # kernel's, and these strings are spent on both.
            (1000000000, "1000000000"),
        ],
    )
    def test_a_size_round_trips_through_the_docker_spelling(
        self, size: int, expected: str
    ):
        assert format_memory_size(size) == expected
        assert parse_memory_size(expected) == size

    def test_a_bare_byte_count_cap_derives_mounts_the_kernel_will_mount(
        self, _checkout: Path
    ):
        # A bare byte count is a documented max_memory, and dividing one by 2
        # and 4 rarely lands on a k/m/g boundary. `size=` on a tmpfs is parsed
        # by the KERNEL, which takes a bare byte count or k/m/g and rejects
        # the `b` Docker would have accepted — so the fallback spelling has to
        # be the one both parsers read, or a load-accepted cap refuses every
        # launch (the failure this whole module exists to prevent).
        caps = resource_caps(SandboxConfig(max_memory=str(4_000_000_000)))
        assert (caps.home_tmpfs, caps.tmp_tmpfs) == ("1953125k", "1000000000")
        with self._daemon(16):
            argv = build_run_argv(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=_checkout,
                slug="42",
                network="allowlist",
                runner="claude-code",
                caps=caps,
            )
        for mount in _flag_values(argv, "--tmpfs"):
            for option in mount.split(":", 1)[1].split(","):
                name, _, size = option.partition("=")
                if name == "size":
                    assert _KERNEL_TMPFS_SIZE.fullmatch(size), mount

    def test_lowering_the_memory_constant_alone_leaves_the_mounts_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The unset branch returns each constant verbatim instead of deriving
        # the mounts, and the live cgroup-OOM probe depends on exactly that:
        # it lowers _MEMORY_LIMIT to 16m and writes 32 MiB into /tmp, which is
        # an OOM kill only while /tmp is still bigger than the memory cap. A
        # /tmp resized with the cap would answer ENOSPC and prove nothing.
        monkeypatch.setattr("kew.sandbox._MEMORY_LIMIT", "16m")
        caps = resource_caps()
        assert (caps.memory, caps.memory_swap) == ("16m", "16m")
        assert (caps.home_tmpfs, caps.tmp_tmpfs) == ("4g", "1g")

    def test_the_caps_object_cannot_be_handed_a_separate_swap_allowance(self):
        # memory_swap is derived, not stored: there is no way to construct a
        # cap set whose swap allowance exceeds its memory cap.
        caps = ResourceCaps(memory="2g", cpus=1, home_tmpfs="1g", tmp_tmpfs="512m")
        assert caps.memory_swap == caps.memory
        assert "memory_swap" not in {f.name for f in dataclasses.fields(caps)}


class TestTheGatewayRuntimeAttestsOnlyWhatItDelivered:
    """``runtime = "openshell"`` claims no docker-argv setting it cannot apply.

    Three ``[sandbox]`` settings reach a container as part of the ``docker run``
    argv — ``max_memory`` and ``max_cpus`` as ``--memory`` / ``--cpus``,
    ``proxy_environment = "inherit"`` as ``-e NAME`` — and a gateway sandbox is
    created from an image, its providers, an egress policy and labels
    (``openshell sandbox create``) with no argv to carry any of them. The ledger
    used to attest them anyway: an admitted OpenShell dispatch recorded a
    concrete ``8g`` / ``4`` for a cgroup that never existed, and recorded ten
    inherited proxy variables — in ``credential_names`` too — for an exec that
    forwards only ``kew.openshell.SANDBOX_EXEC_ENV_NAMES`` (PR #2401 review).
    """

    @staticmethod
    def _payload(config: SandboxConfig) -> dict:
        store = MagicMock()
        store.append_audit_event.return_value = 1
        emit_resolved_posture(
            store,
            runner="claude-code",
            config=config,
            posture="sandboxed",
            runtime=config.runtime,
            image="kew-agent:latest",
            reason=None,
            run_id="run-2401",
            checkout_dir=None,
            repo="o/r",
            issue_num=2401,
            item_id=None,
            egress_route="openshell" if config.runtime == "openshell" else "direct",
            certified=config.runtime == "openshell",
        )
        return store.append_audit_event.call_args.kwargs["payload"]

    def test_an_admitted_gateway_dispatch_attests_no_cgroup_cap(self):
        # Shown to fail before the fix: "3g"/2 on the one runtime where kew
        # passes no resource flag at all, so the ledger claimed a limit the
        # contained agent never ran under.
        payload = self._payload(
            SandboxConfig(runtime="openshell", max_memory="3g", max_cpus=2)
        )
        assert payload["posture"] == "sandboxed"
        assert (payload["max_memory"], payload["max_cpus"]) == (None, None)
        # Present, not absent: one payload schema across every posture and
        # runtime, so no consumer of a run report hits a KeyError.
        assert {"max_memory", "max_cpus"} <= set(payload)

    def test_the_docker_boundary_still_attests_the_caps_it_enforces(self):
        # The null above is a fact about the gateway, not a regression in the
        # attestation: the runtime that does set a cgroup still records it.
        payload = self._payload(SandboxConfig(max_memory="3g", max_cpus=2))
        assert (payload["max_memory"], payload["max_cpus"]) == ("3g", 2)

    def test_an_inherited_proxy_is_not_attested_as_crossing_the_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Shown to fail before the fix: HTTPS_PROXY appeared in
        # proxy_variables.inherited AND in credential_names, naming an exposed
        # variable the exec allowlist forwards nowhere.
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        payload = self._payload(
            SandboxConfig(runtime="openshell", proxy_environment="inherit")
        )
        assert payload["proxy_variables"] == {"inherited": [], "blanked": []}
        assert not [
            name for name in payload["credential_names"] if "PROXY" in name.upper()
        ]
        # The config fact is still recorded: what the operator asked for is not
        # rewritten, only what the launch is said to have done with it.
        assert payload["proxy_environment"] == "inherit"

    def test_the_docker_boundary_still_attests_the_proxies_it_forwards(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        payload = self._payload(SandboxConfig(proxy_environment="inherit"))
        assert "HTTPS_PROXY" in payload["proxy_variables"]["inherited"]
        assert "HTTPS_PROXY" in payload["credential_names"]

    def test_the_two_proxy_fields_cannot_disagree_about_what_crossed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The shape of the bug was two derivations of one question. The
        # credential surface is now built FROM the attested inherited list, so
        # a name can only appear in one if it appears in the other.
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        monkeypatch.setenv("no_proxy", "localhost")
        for runtime in ("docker", "openshell"):
            payload = self._payload(
                SandboxConfig(runtime=runtime, proxy_environment="inherit")
            )
            inherited = payload["proxy_variables"]["inherited"]
            named = [
                name for name in payload["credential_names"] if "PROXY" in name.upper()
            ]
            assert named == inherited, runtime

    def test_an_ignored_setting_is_warned_about_rather_than_silently_dropped(self):
        # The other half of the fix: an honest ledger an operator reads later
        # is not a substitute for being told now.
        warning = openshell_ignored_argv_settings_warning(
            SandboxConfig(
                runtime="openshell",
                proxy_environment="inherit",
                max_memory="3g",
                max_cpus=2,
            )
        )
        assert warning is not None
        assert 'proxy_environment = "inherit"' in warning
        assert "max_memory" in warning and "max_cpus" in warning
        assert 'runtime = "docker"' in warning

    @pytest.mark.parametrize(
        "config",
        [
            # Nothing configured: the ceilings are kew's, not the operator's,
            # and warning about them on every gateway dispatch would be noise.
            SandboxConfig(runtime="openshell"),
            # Another runtime applies all three, so there is nothing to say.
            SandboxConfig(max_memory="3g", max_cpus=2, proxy_environment="inherit"),
        ],
    )
    def test_nothing_is_warned_about_where_the_settings_do_apply(
        self, config: SandboxConfig
    ):
        assert openshell_ignored_argv_settings_warning(config) is None

    def test_the_memory_preflight_is_skipped_where_there_is_no_cap(self, capsys):
        # It would ask `openshell info` — a subcommand the CLI does not have —
        # and print a docker MemTotal warning about a cap kew never sets.
        with patch("kew.subprocess_utils.run") as run:
            check_runtime_memory_capacity(
                "openshell", SandboxConfig(runtime="openshell", max_memory="1g")
            )
        assert run.call_count == 0
        assert capsys.readouterr().err == ""


class TestHostSideCaps:
    """The three caps Docker has no flag for, enforced while kew streams."""

    def test_a_huge_log_line_is_read_bounded_not_buffered_whole(self):
        # An agent chooses how many bytes it writes before a newline. An
        # unbounded readline turns that choice into unbounded HOST memory.
        handle, _ = _handle(["x" * (4 * MAX_LINE_BYTES) + "\n"])
        first = next(iter(handle))
        assert len(first) == MAX_LINE_BYTES
        assert handle._proc.stdout.limits == [MAX_LINE_BYTES]

    def test_a_huge_log_agent_is_killed_at_the_transcript_cap(self):
        chunk = "y" * MAX_LINE_BYTES + "\n"
        lines = [chunk] * (TRANSCRIPT_BYTE_CAP // MAX_LINE_BYTES + 2)
        handle, _ = _handle(lines)
        with patch(
            "kew.subprocess_utils.run", return_value=MagicMock(returncode=0)
        ) as killer:
            streamed = list(handle)
        killer.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        # The stream STOPS — the host is not asked to read the rest — and the
        # last line names the cap rather than leaving a run that just went
        # quiet (rule 4).
        assert len(streamed) < len(lines)
        assert "transcript exceeded" in _cap_reason(streamed[-1])

    def test_a_multibyte_line_counts_utf8_bytes_against_transcript_cap(self):
        handle, _ = _handle(["é\n"], transcript_byte_cap=2)
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        killer.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        assert "transcript exceeded 2 bytes" in _cap_reason(streamed[-1])

    # Real wall clock: 20 ms interval, 100 ms allowance. Runs alone (serial
    # phase) so a saturated xdist host cannot turn the deadline into a flake.
    @pytest.mark.serial
    def test_silent_stdout_past_deadline_is_killed_within_one_interval(self):
        proc = MagicMock()
        silent = _SilentStdout()
        proc.stdout = silent
        interval = 0.02
        handle = ContainerHandle(
            proc,
            "kew-silent",
            container_id=RECORDED_ID,
            wall_clock_seconds=0.001,
            watchdog_interval_seconds=interval,
        )
        started = time.monotonic()
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        silent.release.set()
        assert time.monotonic() - started < interval + 0.1
        killer.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        assert "wall-clock limit reached" in _cap_reason(streamed[-1])

    # Real wall clock. Serial phase: see tests/conftest.py.
    @pytest.mark.serial
    def test_thread_watchdog_re_polls_on_its_cadence(self):
        # The thread watchdog must keep polling: a deadline that is only
        # crossed AFTER the first wait is still caught within one interval.
        crossed = threading.Event()

        def now() -> float:
            return 10.0 if crossed.is_set() else 0.0

        interval = 0.02
        handle = ContainerHandle(
            MagicMock(),
            "kew-cadence",
            container_id=RECORDED_ID,
            wall_clock_seconds=5.0,
            now=now,
            watchdog_interval_seconds=interval,
        )
        with patch(
            "kew.subprocess_utils.run", return_value=MagicMock(returncode=0)
        ) as killer:
            handle.start_watchdog()
            time.sleep(interval * 4)
            killer.assert_not_called()
            crossed.set()
            deadline = time.monotonic() + interval + 0.2
            while not killer.called and time.monotonic() < deadline:
                time.sleep(0.005)
            handle.stop_watchdog()
        killer.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        assert "wall-clock limit reached" in (handle.resource_cap_reason or "")

    def test_an_inode_bomb_is_killed_at_the_entry_quota(self, tmp_path: Path):
        # 100k empty files measure 0 bytes; the entry count catches them.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        for i in range(12):
            (checkout / f"empty-{i}").touch()
        handle, _ = _handle(
            ["a\n", "b\n"],
            checkout_dir=checkout,
            checkout_quota_bytes=1 << 30,
            checkout_quota_entries=10,
        )
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        killer.assert_called_once()
        assert "checkout exceeded 10 entries" in _cap_reason(streamed[-1])

    def test_a_host_running_out_of_space_is_a_breach_without_a_walk(
        self, tmp_path: Path
    ):
        # A fill-disk agent can outrun the quota walk's back-off window; the
        # statvfs floor cannot be outrun and never walks the tree.
        from kew.sandbox import _StreamGuard

        guard = _StreamGuard(
            checkout_dir=tmp_path,
            wall_clock_seconds=1e9,
            transcript_cap=1 << 30,
            checkout_quota=1 << 30,
            now=lambda: 0.0,
            host_free_floor=1 << 30,
        )
        usage = shutil.disk_usage(tmp_path)._replace(free=(1 << 30) - 1)
        with (
            patch("kew.sandbox.shutil.disk_usage", return_value=usage),
            patch("kew.sandbox.checkout_usage") as walk,
        ):
            reason = guard.timed_breach()
        assert reason is not None
        assert reason.code is ResourceCapCode.HOST_FREE_SPACE
        assert "host free space" in reason.reason
        walk.assert_not_called()

    def test_a_host_with_space_is_not_capped_by_the_floor(self, tmp_path: Path):
        from kew.sandbox import _StreamGuard

        guard = _StreamGuard(
            checkout_dir=tmp_path,
            wall_clock_seconds=1e9,
            transcript_cap=1 << 30,
            checkout_quota=1 << 30,
            now=lambda: 0.0,
            host_free_floor=1 << 30,
        )
        usage = shutil.disk_usage(tmp_path)._replace(free=1 << 40)
        with patch("kew.sandbox.shutil.disk_usage", return_value=usage):
            assert guard.timed_breach() is None

    def test_the_floor_is_enforced_through_the_handle(self, tmp_path: Path):
        # Pinned through ContainerHandle, not a bare guard: `_guard` must
        # thread the floor and the stream must kill on it.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        handle, _ = _handle(
            ["a\n", "b\n"], checkout_dir=checkout, host_free_floor=1 << 30
        )
        usage = shutil.disk_usage(checkout)._replace(free=(1 << 30) - 1)
        with (
            patch("kew.sandbox.shutil.disk_usage", return_value=usage),
            patch(
                "kew.subprocess_utils.run", return_value=MagicMock(returncode=0)
            ) as killer,
        ):
            streamed = list(handle)
        killer.assert_called_once()
        assert "host free space" in _cap_reason(streamed[-1])

    def test_launch_is_refused_when_the_host_is_under_the_floor(self, tmp_path: Path):
        from kew.sandbox import SandboxHostCapacityError, check_host_capacity

        usage = shutil.disk_usage(tmp_path)._replace(free=(1 << 30) - 1)
        with (
            patch("kew.sandbox.shutil.disk_usage", return_value=usage),
            pytest.raises(SandboxHostCapacityError, match="free space") as excinfo,
        ):
            check_host_capacity(tmp_path, free_floor=1 << 30)
        assert str(tmp_path) in str(excinfo.value)  # names the probed path

    def test_launch_is_refused_when_the_checkout_is_already_over_quota(
        self, tmp_path: Path
    ):
        from kew.sandbox import SandboxHostCapacityError, check_host_capacity

        (tmp_path / "blob").write_bytes(b"x" * 2048)
        with pytest.raises(SandboxHostCapacityError, match="already exceeds"):
            check_host_capacity(tmp_path, quota_bytes=1024)
        for i in range(12):
            (tmp_path / f"e{i}").touch()
        with pytest.raises(SandboxHostCapacityError, match="entries"):
            check_host_capacity(tmp_path, quota_bytes=1 << 30, quota_entries=10)
        check_host_capacity(tmp_path, quota_bytes=1 << 30, quota_entries=1000)

    def test_the_walk_back_off_has_a_ceiling(self, tmp_path: Path):
        # The walk time is agent-controlled; the blind window it buys is not.
        from kew.sandbox import QUOTA_WALK_MAX_BACKOFF_SECONDS, _StreamGuard

        clock = {"now": 0.0}
        guard = _StreamGuard(
            checkout_dir=tmp_path,
            wall_clock_seconds=1e9,
            transcript_cap=1 << 30,
            checkout_quota=1 << 30,
            now=lambda: clock["now"],
            watchdog_interval=2.0,
        )
        perf = iter([0.0, 60.0, 100.0, 100.0])  # a 60 s walk
        with (
            patch("kew.sandbox.checkout_usage", return_value=(0, 0)) as walk,
            patch("kew.sandbox.time.perf_counter", side_effect=lambda: next(perf)),
        ):
            assert guard.timed_breach() is None
            clock["now"] = QUOTA_WALK_MAX_BACKOFF_SECONDS + 0.1
            assert guard.timed_breach() is None
            assert walk.call_count == 2

    def test_a_slow_checkout_walk_backs_off(self, tmp_path: Path):
        # A walk that took 1 s is not repeated for QUOTA_WALK_BACKOFF s, so a
        # node_modules-sized checkout does not pin a core every interval.
        from kew.sandbox import QUOTA_WALK_BACKOFF, _StreamGuard

        clock = {"now": 0.0}
        guard = _StreamGuard(
            checkout_dir=tmp_path,
            wall_clock_seconds=1e9,
            transcript_cap=1 << 30,
            checkout_quota=1 << 30,
            now=lambda: clock["now"],
            watchdog_interval=2.0,
        )
        perf = iter([0.0, 1.0, 100.0, 100.0])
        with (
            patch("kew.sandbox.checkout_usage", return_value=(0, 0)) as walk,
            patch("kew.sandbox.time.perf_counter", side_effect=lambda: next(perf)),
        ):
            assert guard.timed_breach() is None  # walk 1: took 1 s
            clock["now"] = 2.5  # one watchdog interval later: no walk
            assert guard.timed_breach() is None
            assert walk.call_count == 1
            clock["now"] = QUOTA_WALK_BACKOFF * 1.0 + 0.1
            assert guard.timed_breach() is None
            assert walk.call_count == 2

    # Real wall clock. Serial phase: see tests/conftest.py.
    @pytest.mark.serial
    def test_silent_stdout_over_checkout_quota_is_killed(self, tmp_path: Path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "blob").write_bytes(b"x" * 1025)
        proc = MagicMock()
        silent = _SilentStdout()
        proc.stdout = silent
        handle = ContainerHandle(
            proc,
            "kew-silent-quota",
            container_id=RECORDED_ID,
            checkout_dir=checkout,
            checkout_quota_bytes=1024,
            watchdog_interval_seconds=0.02,
        )
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        silent.release.set()
        killer.assert_called_once_with(
            ["docker", "kill", RECORDED_ID],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        assert "checkout exceeded 1024 bytes" in _cap_reason(streamed[-1])

    def test_an_agent_past_the_wall_clock_is_killed(self):
        handle, _ = _handle(
            ["a\n", "b\n"],
            now=_clock([0.0, 0.0, WALL_CLOCK_LIMIT_SECONDS + 1.0]),
        )
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        killer.assert_called_once()
        assert "wall-clock limit reached" in _cap_reason(streamed[-1])

    def test_a_run_inside_the_wall_clock_streams_untouched(self):
        handle, _ = _handle(["a\n", "b\n"], now=_clock([0.0]))
        with patch("kew.subprocess_utils.run") as killer:
            assert list(handle) == ["a\n", "b\n"]
        killer.assert_not_called()

    def test_a_fill_disk_agent_is_killed_at_the_checkout_quota(self, tmp_path: Path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "blob").write_bytes(b"z" * 4096)
        handle, _ = _handle(
            ["a\n", "b\n"], checkout_dir=checkout, checkout_quota_bytes=1024
        )
        with patch("kew.subprocess_utils.run") as killer:
            streamed = list(handle)
        killer.assert_called_once()
        assert "checkout exceeded 1024 bytes" in _cap_reason(streamed[-1])

    def test_a_checkout_under_quota_is_not_killed(self, tmp_path: Path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "blob").write_bytes(b"z" * 16)
        handle, _ = _handle(
            ["a\n", "b\n"], checkout_dir=checkout, checkout_quota_bytes=1024
        )
        with patch("kew.subprocess_utils.run") as killer:
            assert list(handle) == ["a\n", "b\n"]
        killer.assert_not_called()

    def test_the_quota_is_enforced_over_the_argv_mounted_checkout(self, tmp_path: Path):
        # Derived from the launch argv, not passed separately: the directory a
        # quota covers must be the directory that was actually mounted.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        argv = _agent_argv(checkout)
        fake_proc = MagicMock()
        fake_proc.stdout = _FakeStdout([])
        with patch("kew.subprocess_utils.popen", return_value=fake_proc):
            handle = launch_container(argv)
        assert handle._checkout_dir == checkout.absolute()

    def test_a_contained_execution_handle_carries_the_same_quota_target(
        self, tmp_path: Path
    ):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        contained = ContainedExecution.from_argv(_agent_argv(checkout))
        handle = contained.handle_for(MagicMock())
        assert handle._checkout_dir == checkout.absolute()

    def test_the_defaults_are_the_module_caps_not_unbounded(self):
        handle, _ = _handle([])
        assert handle._wall_clock_seconds == WALL_CLOCK_LIMIT_SECONDS
        assert handle._transcript_byte_cap == TRANSCRIPT_BYTE_CAP
        assert handle._checkout_quota_bytes == CHECKOUT_QUOTA_BYTES

    def test_the_cap_event_reaches_run_evidence_as_a_named_terminal(self):
        # The kill must be legible downstream: the marker is shaped like the
        # container's own stream events so the ordinary derivation folds it in.
        from kew.transcript_metrics import StreamDerivation

        handle, _ = _handle(["a\n"], transcript_byte_cap=0)
        derivation = StreamDerivation()
        with patch("kew.subprocess_utils.run"):
            for line in handle:
                derivation.observe(line)
        assert derivation.evidence().terminal_type == RESOURCE_CAP_EVENT


class TestCheckoutUsageBytes:
    def test_sums_regular_files_recursively(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a").write_bytes(b"x" * 10)
        (tmp_path / "sub" / "b").write_bytes(b"y" * 5)
        assert checkout_usage_bytes(tmp_path) == 15

    def test_symlinks_are_measured_as_links_not_followed(self, tmp_path: Path):
        # An agent that can make the measurement follow a link can point it at
        # something small and hide the fill underneath.
        big = tmp_path / "outside"
        big.mkdir()
        (big / "blob").write_bytes(b"x" * 4096)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "link").symlink_to(big)
        assert checkout_usage_bytes(checkout) == 0

    def test_stops_early_once_over_the_stop_at_bound(self, tmp_path: Path):
        for i in range(20):
            (tmp_path / f"f{i}").write_bytes(b"x" * 100)
        assert checkout_usage_bytes(tmp_path, stop_at=150) > 150

    def test_an_unreadable_entry_does_not_stop_the_measurement(self, tmp_path: Path):
        (tmp_path / "a").write_bytes(b"x" * 10)
        (tmp_path / "gone").symlink_to(tmp_path / "missing")
        assert checkout_usage_bytes(tmp_path) == 10


# -- the daemon's CPU-range refusal, as Docker actually prints it -------------

_REAL_CPU_CAP_REFUSAL = (
    "docker: Error response from daemon: range of CPUs is from 0.01 to 2.00, "
    "as there are only 2 CPUs available"
)


@pytest.mark.parametrize(
    "line",
    [
        _REAL_CPU_CAP_REFUSAL,  # Docker 29 on a hosted two-CPU runner (no period)
        _REAL_CPU_CAP_REFUSAL + ".",  # the older spelling the fixtures used
        _REAL_CPU_CAP_REFUSAL.replace("range", "Range") + ".",
    ],
    ids=["no-period", "period", "capitalised-period"],
)
def test_cpu_cap_rejection_is_recognised_with_and_without_a_period(line: str) -> None:
    from decimal import Decimal

    from kew.sandbox import _cpu_cap_rejection_maximum

    assert _cpu_cap_rejection_maximum(line) == Decimal("2.00")


def test_cpu_cap_rejection_is_found_in_multiline_stderr() -> None:
    import subprocess as sp
    from decimal import Decimal

    from kew.sandbox import _completed_cpu_cap_rejection_maximum

    result = sp.CompletedProcess(
        ["docker", "run"],
        125,
        "",
        _REAL_CPU_CAP_REFUSAL + "\n\nRun 'docker run --help' for more information\n",
    )
    assert _completed_cpu_cap_rejection_maximum(result) == Decimal("2.00")


@pytest.mark.parametrize(
    "line",
    [
        "docker: Error response from daemon: No such image: alpine:latest",
        _REAL_CPU_CAP_REFUSAL + " extra",
        _REAL_CPU_CAP_REFUSAL.replace("2 CPUs", "3 CPUs"),
    ],
    ids=["other-error", "trailing-text", "inconsistent-count"],
)
def test_cpu_cap_rejection_ignores_other_daemon_output(line: str) -> None:
    from kew.sandbox import _cpu_cap_rejection_maximum

    assert _cpu_cap_rejection_maximum(line) is None


# -- allowlist network inspection on the admission path -----------------------


def test_network_inspection_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import progress, sandbox, subprocess_utils

    warnings: list[str] = []
    monkeypatch.setattr(progress, "_warn", warnings.append)

    def unresponsive(*_a, **_k):
        raise subprocess.TimeoutExpired(["docker"], 5)

    monkeypatch.setattr(subprocess_utils, "run", unresponsive)

    assert sandbox.allowlist_network_exists("docker") is False
    assert warnings and "could not inspect network 'kew-allowlist'" in warnings[0]


def test_network_inspection_is_bounded_by_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import sandbox, subprocess_utils

    seen: dict[str, object] = {}

    def record(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess_utils, "run", record)

    assert sandbox.allowlist_network_exists("docker") is True
    assert seen["cmd"] == ["docker", "network", "inspect", "kew-allowlist"]
    assert seen["timeout"] == sandbox._NETWORK_INSPECT_TIMEOUT_SECONDS == 5


def test_flow_posture_payload_carries_run_and_checkout_security(
    tmp_path: Path,
) -> None:
    from kew.config import SandboxConfig
    from kew.run_store import SqliteRunStore
    from kew.sandbox import attest_flow_posture

    checkout = tmp_path / "checkout"
    hook = checkout / ".claude" / "hooks" / "security-guard.sh"
    hook.parent.mkdir(parents=True)
    trusted_hook = (
        Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "security-guard.sh"
    )
    hook.write_bytes(trusted_hook.read_bytes())
    hook.chmod(0o755)
    (checkout / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": matcher,
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": ".claude/hooks/security-guard.sh",
                                }
                            ],
                        }
                        for matcher in ("Bash", "Write", "Edit")
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    with SqliteRunStore(str(tmp_path / "flow.db")) as store:
        attest_flow_posture(
            store,
            flow="review",
            runner="claude-code",
            config=SandboxConfig(),
            posture="sandboxed",
            run_id="review-run-1",
            checkout_dir=checkout,
            item_id="SFD-1",
        )
        events = store.list_audit_events(limit=None, event="sandbox_resolved")
    assert len(events) == 1
    assert events[0].run_id == "review-run-1"
    assert events[0].payload["egress_verification"] == "operator-managed-unverified"
    assert events[0].payload["egress_verification_basis"] == "operator-asserted"
    assert events[0].payload["command_guard"] == "loaded"


@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("mode", ["none", "inherit"])
@pytest.mark.parametrize(
    "launcher", ["extract", "filter", "nft", "iptables-legacy", "ip6tables-legacy"]
)
def test_infrastructure_launchers_apply_proxy_policy(
    tmp_path, monkeypatch, runtime, mode, launcher
):
    from kew.sandbox import _PROXY_BUILD_NAMES

    names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    )
    if launcher == "extract":
        names = _PROXY_BUILD_NAMES
    secret = "http://user:s3cret@proxy.invalid:3128"
    for name in names:
        monkeypatch.setenv(name, secret)
    monkeypatch.delenv("HTTPS_PROXY")
    policy = {} if mode == "none" else {"proxy_environment": mode}
    if launcher == "extract":
        # Extraction carries --network none: it takes no policy and blanks every
        # name, so a value it could never reach cannot sit in its environment
        # (governed review round 4, 2026-09-21).
        argv = build_extract_argv(
            runtime=runtime,
            image="kew-agent:test",
            checkout_dir=tmp_path,
            artifact_dir=tmp_path / "artifact",
            slug="1933",
            base_sha="a" * 40,
            uid=501,
            gid=20,
        )
        environment = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
        for name in _PROXY_BUILD_NAMES:
            assert f"{name}=" in environment
            assert name not in environment
        assert all(secret not in argument for argument in argv)
        return
    if launcher == "filter":
        image = "sha256:" + "a" * 64
        argv = build_worker_netns_filter_helper_argv(
            runtime=runtime,
            image_id=image,
            worker_id="b" * 64,
            owner="c" * 32,
            **policy,
        )
    else:
        image = "sha256:" + "a" * 64
        argv = build_global_firewall_observer_argv(
            runtime=runtime,
            image_id=image,
            owner="c" * 32,
            source=GlobalFirewallSource(launcher),
            **policy,
        )
    _assert_infrastructure_proxy_policy(argv, image, names, mode, secret)


@pytest.mark.parametrize(
    "name",
    ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"],
)
@pytest.mark.parametrize(
    "value", ["http://user:s3cret@proxy.invalid:3128", "http://proxy.invalid:3128", "x"]
)
def test_redact_credentials_removes_complete_current_proxy_value(
    monkeypatch, name, value
):
    from kew.sandbox import proxy_redaction_scope, redact_credentials

    monkeypatch.setenv(name, value)
    expected = f"before <{name} redacted> after"
    with proxy_redaction_scope("inherit"):
        assert redact_credentials(f"before {value} after") == expected
    monkeypatch.setenv(name, "http://changed.invalid:3128")
    with proxy_redaction_scope("inherit"):
        assert "http://changed.invalid:3128" not in redact_credentials(
            "http://changed.invalid:3128"
        )


@pytest.mark.parametrize("value", [None, ""])
def test_redact_credentials_ignores_empty_or_unset_proxies(monkeypatch, value):
    from kew.sandbox import redact_credentials

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    text = "Unrelated build output remains readable."
    assert redact_credentials(text) == text


@pytest.mark.parametrize("prefix_length", [8, 17, 239, 359])
@pytest.mark.parametrize("ending", ["", "...", '"}'])
def test_redact_credentials_preserves_partial_password_matches(
    monkeypatch, prefix_length, ending
):
    from kew.sandbox import redact_credentials

    password = "s3cret" * 60
    # An empty username isolates partial passwords from complete username matches.
    monkeypatch.setenv("HTTP_PROXY", f"http://:{password}@proxy.invalid:3128")
    fragment = password[:prefix_length]
    text = f"http://:{fragment}{ending}"
    assert redact_credentials(text) == text
    assert password not in redact_credentials(password)
    assert redact_credentials("ordinary model text") == "ordinary model text"


@pytest.mark.parametrize("sink", ["oom", "inspect"])
def test_runtime_diagnostics_redact_proxy_before_truncation(monkeypatch, sink):
    from kew.sandbox import _classify_inspect_failure

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    if sink == "oom":
        diagnostic = ContainerHandle._bounded_probe_diagnostic("a" * 470 + secret)
    else:
        diagnostic = _classify_inspect_failure(
            "docker", subprocess.CompletedProcess([], 1, "", "a" * 170 + secret)
        ).detail
    assert "proxy.invalid" not in diagnostic
    assert "http://user" not in diagnostic
    assert "redacted" in diagnostic


def test_proxy_policy_never_leaves_a_name_unspecified(monkeypatch):
    from itertools import combinations

    from kew.sandbox import (
        CONTAINER_PROXY_NAMES,
        CONTROLLED_PROXY_NAMES,
        _container_proxy_environment_args,
    )

    # Every subset of the six inheritable names may be present; the four
    # always-blank names are present in every case and never inherited.
    for mode in ("none", "inherit"):
        for count in range(len(CONTAINER_PROXY_NAMES) + 1):
            for present in combinations(CONTAINER_PROXY_NAMES, count):
                for name in CONTROLLED_PROXY_NAMES:
                    monkeypatch.setenv(name, "")
                for name in CONTAINER_PROXY_NAMES:
                    monkeypatch.delenv(name, raising=False)
                for name in present:
                    monkeypatch.setenv(name, "")
                args = _container_proxy_environment_args(mode)[1::2]
                assert [arg.partition("=")[0] for arg in args] == list(
                    CONTROLLED_PROXY_NAMES
                )
                assert args == [
                    name if mode == "inherit" and name in present else f"{name}="
                    for name in CONTROLLED_PROXY_NAMES
                ]


@pytest.mark.parametrize("mode", ["none", "inherit"])
@pytest.mark.parametrize("entrypoint", ["explicit", "unpinned", "smoke"])
def test_image_build_obeys_proxy_policy(
    monkeypatch, tmp_path, caplog, mode, entrypoint
):
    import json
    import logging

    from kew import sandbox

    secret = "http://user:s3cret@proxy.invalid:3128"
    config_secret = "http://config:config-password@config.invalid:3128"
    names = [
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", secret)
    monkeypatch.setenv("no_proxy", "")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "proxies": {
                    "default": {"httpProxy": config_secret, "httpsProxy": config_secret}
                }
            }
        )
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    config = SandboxConfig(runtime="docker", network="open", proxy_environment=mode)
    built = subprocess.CompletedProcess([], 1 if entrypoint == "smoke" else 0, "", "")
    caplog.set_level(logging.INFO, logger="kew.sandbox")
    with (
        patch("kew.sandbox.detect_runtime", return_value="/mock/docker"),
        patch("kew.sandbox._image_exists_locally", return_value=False),
        patch(
            "kew.sandbox.inspect_image_id",
            return_value=sandbox.ImageResolution(
                sandbox.ImageIdStatus.RESOLVED, image_id="sha256:" + "a" * 64
            ),
        ),
        patch("kew.sandbox.subprocess_utils.run", return_value=built) as run,
    ):
        if entrypoint == "smoke":
            result = sandbox.run_smoke(config)
            assert result.layer == "image"
        else:
            sandbox.build_sandbox_image(
                config,
                **(
                    {"tag": "local/test:proxy", "unpinned": True}
                    if entrypoint == "unpinned"
                    else {}
                ),
            )
    # Main's build path probes the daemon (``docker info``) before building
    # (#1943); smoke builds without that probe. The build is the last call.
    probes = [call.args[0][1:] for call in run.call_args_list[:-1]]
    assert probes == ([] if entrypoint == "smoke" else [["info"]])
    _assert_build_proxy_call(run, mode, names, secret, config_secret, tmp_path)
    _assert_build_proxy_log(caplog, mode, names)


def _assert_infrastructure_proxy_policy(argv, image, names, mode, secret):
    from kew.sandbox import CONTAINER_PROXY_NAMES

    options = argv[: argv.index(image)]
    actual = [
        options[i + 1]
        for i, arg in enumerate(options)
        if arg == "-e" and options[i + 1].partition("=")[0] in names
    ]
    assert actual == [
        name
        if mode == "inherit" and name in CONTAINER_PROXY_NAMES and name != "HTTPS_PROXY"
        else f"{name}="
        for name in names
    ]
    assert "HTTPS_PROXY=" in actual
    assert secret not in " ".join(argv)


def _assert_build_proxy_call(run, mode, names, secret, config_secret, tmp_path):
    argv = run.call_args.args[0]
    build_args = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--build-arg"]
    inherited = ["HTTP_PROXY", "no_proxy"] if mode == "inherit" else []
    assert build_args == [name if name in inherited else f"{name}=" for name in names]
    assert secret not in " ".join(argv)
    assert config_secret not in " ".join(argv)
    env = run.call_args.kwargs["env"]
    assert {name: env[name] for name in names} == {
        name: secret if name == "HTTP_PROXY" and mode == "inherit" else ""
        for name in names
    }
    assert env["DOCKER_CONFIG"] == str(tmp_path)


def _assert_build_proxy_log(caplog, mode, names):
    inherited = ["HTTP_PROXY", "no_proxy"] if mode == "inherit" else []
    records = [
        record for record in caplog.records if hasattr(record, "proxy_variables")
    ]
    assert len(records) == 1
    assert records[0].proxy_environment == mode
    assert records[0].proxy_variables == {
        "inherited": inherited,
        "blanked": [name for name in names if name not in inherited],
    }
    assert "s3cret" not in caplog.text
    assert "config-password" not in caplog.text


def _assert_contains_all(value, *members):
    assert all(member in value for member in members)


def _assert_excludes_all(value, *members):
    assert all(member not in value for member in members)


def _assert_guard_state(guard_state, tmp_path, expected):
    assert guard_state("claude-code", tmp_path) == expected


def _assert_finalized_breach(result, handle, breach):
    assert result.hard_stop == "resource_cap"
    assert handle._killed is True
    assert handle._pending_breach is None
    assert handle.resource_cap_observations == (breach,)


def _assert_matches(pattern, value):
    assert re.fullmatch(pattern, value)


def _assert_process_output(result):
    assert result.stdout or result.stderr


def test_direct_egress_classification_is_permanently_frozen() -> None:
    """Route identity prevents sidecar evidence from upgrading direct egress."""
    from kew import sandbox

    classify = sandbox.classify_egress_verification
    assert set(inspect.signature(classify).parameters) == {
        "route",
        "certified",
    }, "the classifier must exclude network configuration and caller upgrades"
    frozen = sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    assert frozen == "operator-managed-unverified"
    produced = {
        route: {
            classify(route=route, certified=certified) for certified in (False, True)
        }
        for route in ("direct", "sidecar")
    }
    assert produced == {
        "direct": {frozen},
        "sidecar": {"unavailable", "sidecar-certified"},
    }
    with pytest.raises(TypeError, match="egress_verification"):
        classify(route="direct", egress_verification="sidecar-certified")
    with pytest.raises(ValueError, match="Unknown contained egress route"):
        classify(route="operator-certified", certified=True)


class TestOpenShellCredentialBroker:
    """`[sandbox] runtime = "openshell"`: providers instead of env credentials.

    On this runtime no credential VALUE enters the agent's environment — the
    gateway holds it in a named provider and injects it at the endpoint the
    policy governs (epic `openshell-runtime`, node N5). Three consequences are
    asserted here because each one, left wrong, is silent:

      * **Admission asks the gateway.** A host that exports `ANTHROPIC_API_KEY`
        says nothing about whether the sandbox can authenticate, so an
        environment-only check would admit a dispatch that comes up
        unauthenticated with the money already spent.
      * **The refusal names which of three things is wrong.** No mapping, no
        provider, or a gateway that never answered need three different fixes,
        and "export the variable" is the right answer to none of them.
      * **Attestation does not claim an injected value.** A `credential_names`
        entry of `ANTHROPIC_API_KEY` means "this launch was handed that value"
        everywhere else kew writes it, so a brokered launch records the
        provider and the placeholder variables as what they are.
    """

    PROVIDER = "kew-claude-code"

    def _config(
        self, providers: dict[str, str] | None = None, **extra
    ) -> SandboxConfig:
        from kew.config import OpenShellConfig

        return SandboxConfig(
            runtime="openshell",
            enabled=True,
            openshell=OpenShellConfig(
                gateway="http://gateway.invalid:8080",
                providers=(
                    {"claude-code": self.PROVIDER} if providers is None else providers
                ),
            ),
            **extra,
        )

    def _gateway(self, monkeypatch, *, found: bool, broken: bool = False) -> list[str]:
        """Fake the subprocess chokepoint, recording the subcommand kew ran."""
        from kew import subprocess_utils

        asked: list[str] = []

        def run(cmd, **_):
            asked.append(" ".join(cmd[-5:]))
            if broken:
                raise OSError(111, "Connection refused")
            if not found:
                return subprocess.CompletedProcess(cmd, 1, "", "no such provider")
            payload = json.dumps(
                {
                    "name": self.PROVIDER,
                    "profile": "anthropic",
                    "credentials": ["ANTHROPIC_API_KEY"],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        monkeypatch.setattr(subprocess_utils, "run", run)
        return asked

    def _resolve(
        self,
        monkeypatch,
        config: SandboxConfig,
        runner="claude-code",
        direct=True,
    ):
        """Resolve the way the launch path does, on a host that has the CLI.

        Nothing is patched out of the runtime gate any more: node N6 gave
        `openshell` a launch path, so `require_launchable_runtime` admits it
        and only the CLI's presence is faked. The egress-certification gate
        (#2319) runs before it and is opened: it has its own refusal tests in
        `tests/test_openshell_certification.py`, and a credential-broker test
        that stopped at it would assert nothing about the broker.
        """
        from kew import sandbox

        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        return sandbox.resolve_effective_sandbox(config, runner=runner, direct=direct)

    def _resolve_sidecar(self, monkeypatch, config: SandboxConfig, runner, name):
        """Resolve the sidecar route the same way the dispatch path does."""
        from kew import sandbox

        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        return sandbox.resolve_effective_sidecar_sandbox(
            config, runner=runner, synthetic_credential_env_name=name
        )

    def _resolve_shape(self, monkeypatch, config: SandboxConfig, runner, **shape):
        """Resolve one dispatch SHAPE through the private resolver.

        The two shapes this runtime has no launch path for arrive at different
        public entry points — `resolve_effective_sidecar_sandbox` for a sidecar
        route, `resolve_effective_sandbox(gateway_launch=False)` for a flow
        whose agent is a container argv — and the rule under test is the same
        for both, so they are asked here in the one spelling that takes both.
        """
        from kew import sandbox

        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        return sandbox._resolve_effective_sandbox(config, runner=runner, **shape)

    @staticmethod
    def _open_egress_gate(monkeypatch) -> None:
        """Treat the boundary as certified so the gates behind it are reached."""
        from kew import sandbox

        monkeypatch.setattr(
            sandbox, "require_certified_boundary", lambda config, *, runner, store: None
        )

    def test_the_runtime_gate_no_longer_refuses_openshell(self, monkeypatch):
        """N6 opened this gate, and the gate it opened is the LAUNCH one.

        The two questions stayed separate through N6 — admission must not be
        refused over "can a later sweep inspect this container" — and N8
        answered the second one too, so both gates now admit this runtime
        while remaining independent of each other.
        """
        from kew.runtime_names import (
            require_backed_runtime,
            require_launchable_runtime,
        )

        assert require_launchable_runtime("openshell") == "openshell"
        assert require_backed_runtime("openshell") == "openshell"
        self._gateway(monkeypatch, found=True)
        with _without_credential():
            assert self._resolve(monkeypatch, self._config()).sandboxed is True

    def test_the_egress_gate_still_refuses_in_front_of_the_launch_gate(
        self, monkeypatch
    ):
        """The launch gate admits openshell; the egress gate is still asked first.

        Same config and gateway as the admitted case above, with the egress
        gate left CLOSED and no ledger: the refusal is the certification's, so
        opening the launch path (N6) did not open a way around the proof
        (#2319).
        """
        from kew import sandbox
        from kew.sandbox import REFUSAL_OPENSHELL_UNCERTIFIED

        self._gateway(monkeypatch, found=True)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        with (
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc_info,
        ):
            resolve_effective_sandbox(self._config(), runner="claude-code")
        assert exc_info.value.reason == REFUSAL_OPENSHELL_UNCERTIFIED

    def test_a_present_provider_admits_the_dispatch_without_reading_the_env(
        self, monkeypatch
    ):
        asked = self._gateway(monkeypatch, found=True)
        with _without_credential():
            decision = self._resolve(monkeypatch, self._config())
        assert (decision.sandboxed, decision.posture) == (True, "sandboxed")
        assert asked == [f"provider get {self.PROVIDER} -o json"]

    def test_a_missing_provider_refuses_naming_the_gateway_not_the_env(
        self, monkeypatch
    ):
        """AC2: the refusal an operator gets when the provider was never made."""
        from kew.sandbox import REFUSAL_BROKER_PROVIDER_MISSING

        self._gateway(monkeypatch, found=False)
        # The host credential is PRESENT: it is what a docker-runtime admission
        # would have accepted, and it must not admit a brokered dispatch.
        with _with_credential(), pytest.raises(SandboxRuntimeUnavailable) as exc_info:
            self._resolve(monkeypatch, self._config())
        exc = exc_info.value
        assert exc.reason == REFUSAL_BROKER_PROVIDER_MISSING
        assert exc.runtime == "openshell"
        assert exc.image == SANDBOX_IMAGE
        assert f"the OpenShell gateway has no provider '{self.PROVIDER}'" in str(exc)
        assert "ANTHROPIC_API_KEY" not in str(exc)

    CODEX_IMAGE = "kew-agent-codex:test"

    def _codex_config(self, providers: dict[str, str]) -> SandboxConfig:
        return self._config(providers=providers, images={"codex": self.CODEX_IMAGE})

    def test_codex_is_admitted_here_on_the_provider_alone(self, monkeypatch):
        """The runner with an EMPTY credential declaration, contained (node N10).

        ``codex`` is refused outright on docker for declaring no in-container
        credential — the container would authenticate as nothing. On this
        runtime that declaration is the accurate one: no credential name is
        forwarded, the gateway injects the value at the endpoint its policy
        governs, and the in-sandbox launcher writes a PLACEHOLDER
        ``auth.json``. So the env question is not asked, and the provider is
        the whole answer.
        """
        asked = self._gateway(monkeypatch, found=True)

        with _without_credential():
            decision = self._resolve(
                monkeypatch, self._codex_config({"codex": self.PROVIDER}), "codex"
            )

        assert (decision.sandboxed, decision.posture) == (True, "sandboxed")
        assert (decision.runtime, decision.image) == ("openshell", self.CODEX_IMAGE)
        assert asked == [f"provider get {self.PROVIDER} -o json"]

    @pytest.mark.parametrize("providers", [{}, {"codex": PROVIDER}])
    def test_codex_without_a_usable_provider_is_refused_not_waved_through(
        self, monkeypatch, providers
    ):
        """Dropping the ENV question must not drop the GATEWAY question too.

        Both gaps — no mapping at all, and a mapping the gateway cannot
        confirm — have to keep refusing. A dispatch admitted on "this runner
        has nothing to forward" would come up holding only the placeholder
        ``auth.json``, fail on its first model call, and have already been
        attested as contained and paid for.
        """
        from kew.sandbox import REFUSAL_BROKER_PROVIDER_MISSING

        self._gateway(monkeypatch, found=False)

        with (
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc_info,
        ):
            self._resolve(monkeypatch, self._codex_config(providers), "codex")

        exc = exc_info.value
        assert exc.reason == REFUSAL_BROKER_PROVIDER_MISSING
        assert (exc.runtime, exc.image) == ("openshell", self.CODEX_IMAGE)

    def test_an_unmapped_runner_and_a_broken_gateway_are_separate_refusals(
        self, monkeypatch
    ):
        """Three gaps, three remedies — never one collapsed "no credential"."""
        from kew.openshell_broker import provider_admission

        self._gateway(monkeypatch, found=True)
        unmapped = provider_admission(self._config(providers={}), "claude-code")
        broken_config = self._config()
        self._gateway(monkeypatch, found=True, broken=True)
        unreachable = provider_admission(broken_config, "claude-code")
        self._gateway(monkeypatch, found=False)
        absent = provider_admission(broken_config, "claude-code")
        assert [row.available for row in (unmapped, unreachable, absent)] == [
            False,
            False,
            False,
        ]
        assert unmapped.provider is None
        assert "providers maps no provider for 'claude-code'" in unmapped.detail
        assert "could not confirm provider" in unreachable.detail
        assert "has no provider" in absent.detail
        assert len({row.detail for row in (unmapped, unreachable, absent)}) == 3

    def test_the_refusal_explains_the_admission_that_actually_refused(
        self, monkeypatch
    ):
        """One question per resolution, so the refusal cannot contradict it.

        The gateway is asked once and the refusal explains THAT answer. Asking a
        second time to build the message is how a provider created between the
        two calls produced a refusal whose own sentence said the gateway holds
        the provider it was refusing over.
        """
        from kew import subprocess_utils
        from kew.sandbox import REFUSAL_BROKER_PROVIDER_MISSING

        asked: list[str] = []

        def run(cmd, **_):
            asked.append(" ".join(cmd[-5:]))
            if len(asked) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "no such provider")
            # The operator created it between the two calls kew used to make.
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": self.PROVIDER, "profile": "anthropic"}), ""
            )

        monkeypatch.setattr(subprocess_utils, "run", run)
        with _with_credential(), pytest.raises(SandboxRuntimeUnavailable) as exc_info:
            self._resolve(monkeypatch, self._config())
        exc = exc_info.value
        assert exc.reason == REFUSAL_BROKER_PROVIDER_MISSING
        assert f"the OpenShell gateway has no provider '{self.PROVIDER}'" in str(exc)
        assert "holds provider" not in str(exc)
        assert asked == [f"provider get {self.PROVIDER} -o json"]

    def test_a_mintable_route_admits_without_a_mapped_provider(self, monkeypatch):
        """AC2: the per-run provider authenticates it, not a long-lived one.

        `opencode` authenticates with the bounded key kew mints for the run, and
        on this runtime that key goes into `kew-run-<run_id>` — the operator's
        `providers` map is never consulted for it. Requiring an entry there
        refused a provisioning-key-only config over an object the launch never
        touches, on a config that admits on docker.
        """
        asked = self._gateway(monkeypatch, found=False)
        monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-2318")
        config = self._config(providers={}, images={"opencode": "kew-opencode:pinned"})
        with _without_credential():
            decision = self._resolve(monkeypatch, config, runner="opencode")
        assert (decision.sandboxed, decision.posture) == (True, "sandboxed")
        assert decision.image == "kew-opencode:pinned"
        # Asked nothing: there is no long-lived provider in this launch's path.
        assert asked == []

    def test_only_a_direct_route_is_predicted_to_mint_its_own_key(
        self, monkeypatch, tmp_path
    ):
        """AC2: a switchyard launch mints nothing, so it brokers like any other.

        `bounded_run_credentials` mints a bounded key only on a DIRECT route
        (`direct_openrouter_route` takes that flag). Predicting the per-run
        provider from runner + provisioning key alone admitted a switchyard
        launch on this runtime without consulting the gateway, minted no key,
        created no provider, and still attested it with `kew-run-<run_id>` — a
        provider that does not exist (#2318 review).

        The route itself is no longer ADMITTED on this runtime either: it has
        no launch path, as the switchyard refusal test below asserts, so the
        gateway is not asked about a provider for it. What is asserted here is
        the credential story the route keeps wherever it does launch, and that
        the attestation writer tells it.
        """
        from kew import sandbox
        from kew.openshell_broker import (
            MAPPED_ROUTE,
            MINTED_ROUTE,
            attested_provider,
            broker_route,
        )
        from kew.openshell_launch import SWITCHYARD_LAUNCH_REFUSAL
        from kew.run_store import SqliteRunStore
        from kew.sandbox import REFUSAL_OPENSHELL_UNAVAILABLE

        monkeypatch.setattr(
            sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
        )
        monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-2318")
        config = self._config(
            providers={"opencode": "kew-opencode"},
            images={"opencode": "kew-opencode:pinned"},
        )
        assert broker_route("opencode", direct=True) == MINTED_ROUTE
        assert broker_route("opencode", direct=False) == MAPPED_ROUTE
        # The transport is refused before the credential question is reached,
        # so the gateway is never asked.
        asked = self._gateway(monkeypatch, found=False)
        with (
            _without_credential(),
            pytest.raises(SandboxRuntimeUnavailable) as exc_info,
        ):
            self._resolve(monkeypatch, config, runner="opencode", direct=False)
        assert exc_info.value.reason == REFUSAL_OPENSHELL_UNAVAILABLE
        assert SWITCHYARD_LAUNCH_REFUSAL in str(exc_info.value)
        assert asked == []
        # And the posture names the mapped provider, not a per-run one.
        assert (
            attested_provider(config, "opencode", run_id="run-2318", direct=False)
            == "kew-opencode"
        )
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="opencode",
                config=config,
                posture="sandboxed",
                runtime="openshell",
                image="kew-opencode:pinned",
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
                direct_transport=False,
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        assert attested[0] == "provider:kew-opencode"
        assert "provider:kew-run-run-2318" not in attested

    @pytest.mark.parametrize("synthetic", ["ANTHROPIC_API_KEY", None])
    def test_a_sidecar_route_is_refused_before_a_posture_is_attested(
        self, monkeypatch, synthetic
    ):
        """A sidecar topology has no launch path here, and admission says so.

        `_launch_openshell_sandbox` refuses it — the sidecar path builds a
        docker `create` argv and installs a netns filter through the daemon,
        neither of which the gateway has. But `resolve_effective_sidecar_sandbox`
        admitted the same topology as `sandboxed` first, so the posture reached
        the chain and the Switchyard proxy container was created before anything
        refused: the admit-then-refuse ordering the runner and runtime gates
        were already moved out of (PR #2401 review).

        Parameterized over the worker credential NAME because the route fact is
        the topology, not the name: a sidecar route that named no credential is
        still a sidecar route, and was admitted by the old
        `synthetic_credential_env_name is not None` reading of it.
        """
        from kew.openshell_launch import SIDECAR_LAUNCH_REFUSAL
        from kew.sandbox import REFUSAL_OPENSHELL_UNAVAILABLE

        # The gateway is never consulted — not for a provider, and not to be
        # told it cannot do this.
        asked = self._gateway(monkeypatch, found=False)
        with _without_credential(), pytest.raises(SandboxRuntimeUnavailable) as refused:
            self._resolve_sidecar(
                monkeypatch, self._config(providers={}), "claude-code", synthetic
            )
        assert refused.value.reason == REFUSAL_OPENSHELL_UNAVAILABLE
        assert SIDECAR_LAUNCH_REFUSAL in str(refused.value)
        assert asked == []

    def test_a_switchyard_transport_is_refused_before_a_posture_is_attested(
        self, monkeypatch
    ):
        """Shown to fail: a switchyard launch was admitted and never wired.

        A sidecar-less Switchyard transport resolved `sandboxed` here, so the
        chain attested a switchyard transport — and then
        `launch_openshell_container` exec'd the agent with no endpoint
        override, because on this runtime there is nowhere to put one: the
        exec carries only the names `kew.openshell.SANDBOX_EXEC_ENV_NAMES`
        admits, and the packaged egress policy permits the runner's upstream
        host alone. The agent talked straight past Switchyard to that upstream
        while the ledger said otherwise (PR #2401 review).

        Injecting the endpoint would not have been the fix — no policy kew
        ships permits a switchyard destination, so the launch would fail closed
        at the first model call instead. The honest answer is the one the
        sidecar topology already gets: refuse the shape, before the posture.
        """
        from kew.openshell_launch import SWITCHYARD_LAUNCH_REFUSAL
        from kew.sandbox import REFUSAL_OPENSHELL_UNAVAILABLE

        # The gateway is never consulted — not for a provider, and not to be
        # told it cannot do this.
        asked = self._gateway(monkeypatch, found=True)
        with _with_credential(), pytest.raises(SandboxRuntimeUnavailable) as refused:
            self._resolve(monkeypatch, self._config(), direct=False)
        assert refused.value.reason == REFUSAL_OPENSHELL_UNAVAILABLE
        assert SWITCHYARD_LAUNCH_REFUSAL in str(refused.value)
        assert asked == []

    def test_the_switchyard_refusal_names_the_flow_gap_first(self, monkeypatch):
        """One dispatch can have both gaps, and the flow's is the actionable one.

        An argv flow on a switchyard transport cannot launch here for either
        reason. It hears about the launch path, because an endpoint remedy
        would not give that flow a gateway launch even once applied.
        """
        from kew import sandbox
        from kew.openshell_launch import ARGV_LAUNCH_REFUSAL

        self._gateway(monkeypatch, found=True)
        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        with _with_credential(), pytest.raises(SandboxRuntimeUnavailable) as refused:
            sandbox.resolve_effective_sandbox(
                self._config(),
                runner="claude-code",
                direct=False,
                gateway_launch=False,
            )
        assert ARGV_LAUNCH_REFUSAL in str(refused.value)

    def test_the_sidecar_refusal_keeps_its_place_in_the_resolution_order(
        self, monkeypatch
    ):
        """It is a container refusal, so it cannot outrank the no-container arm.

        An operator who set the skip flag asked for the host, and refusing that
        over a gateway topology it never launches would make an openshell
        config mean "nothing can run here".

        The native arm alongside it lives in the test below, which is the same
        rule read from the other side: this runtime cannot launch a sidecar
        topology at all, so a runner holding its own boundary keeps it rather
        than being refused over kew's.
        """
        # Built through the validator rather than copied: `enabled = false`
        # plus the skip flag is the one combination config load accepts.
        config = SandboxConfig(
            runtime="openshell",
            enabled=False,
            dangerously_skip_containment=True,
            openshell=self._config(providers={}).openshell,
        )
        self._gateway(monkeypatch, found=False)
        with _without_credential():
            decision = self._resolve_sidecar(
                monkeypatch, config, "claude-code", "ANTHROPIC_API_KEY"
            )
        assert (decision.sandboxed, decision.posture) == (False, "uncontained")

    @pytest.mark.parametrize(
        ("shape", "kwargs"),
        [
            # The remediation flows: `kew review`, `kew resolve`, `kew ci-fix`
            # and an epic's plan run swap ONE execution step for a container
            # argv, which the gateway has nowhere to put.
            ("argv-flow", {"gateway_launch": False}),
            # A sidecar Switchyard worker is a docker topology — its worker
            # container and netns filter go through the daemon.
            ("sidecar", {"sidecar": True}),
        ],
    )
    def test_codex_keeps_its_own_boundary_on_a_shape_this_runtime_cannot_launch(
        self, monkeypatch, shape, kwargs
    ):
        """Node N10 hands kew the boundary; it cannot take away the other one.

        `codex` declines its own seatbelt on this runtime so that kew's gateway
        sandbox is used instead — but only where that sandbox is actually on
        offer. These two dispatch SHAPES have no launch path here at all
        (`kew.openshell_launch.gateway_launch_refusal`), so asking the posture
        question with `openshell` traded a run contained by Codex's own
        seatbelt for a hard refusal naming a boundary neither side was ever
        going to hold: on `[sandbox] enabled = true` every Codex remediation
        flow stopped dead (PR #2461 review round 2). The resolver therefore
        asks with the runtime it can really launch THIS dispatch into
        (`kew.sandbox._offered_runtime`), which is no runtime here.

        `claude-code`, which holds no boundary of its own, still hears the
        shape refusal — the gap is not being swallowed, it is being answered by
        the runner that can.
        """
        from kew.openshell_launch import (
            ARGV_LAUNCH_REFUSAL,
            SIDECAR_LAUNCH_REFUSAL,
        )

        config = self._config(providers={}, images={"codex": "kew-agent-codex:test"})
        with _without_credential():
            decision = self._resolve_shape(monkeypatch, config, "codex", **kwargs)
        assert (decision.sandboxed, decision.posture) == (False, "native")
        assert decision.image is None

        with _without_credential(), pytest.raises(SandboxRuntimeUnavailable) as refused:
            self._resolve_shape(monkeypatch, config, "claude-code", **kwargs)
        expected = SIDECAR_LAUNCH_REFUSAL if shape == "sidecar" else ARGV_LAUNCH_REFUSAL
        assert expected in str(refused.value)

    def test_codex_is_native_on_docker_and_contained_only_here(self, monkeypatch):
        """The other half of node N10: the docker lane is unchanged.

        There kew has no lane for the packaged Codex agent image — its launcher
        reads the first container arg as the startup nonce and refuses under
        `docker run` — so Codex's own seatbelt is the only boundary available
        and stays the declared posture, mapped image or not. Only the
        gateway-launched dispatch on this runtime is `sandboxed`.
        """
        docker = SandboxConfig(
            runtime="docker", enabled=True, images={"codex": "kew-agent-codex:test"}
        )
        with _without_credential():
            decision = resolve_effective_sandbox(docker, runner="codex")
        assert (decision.sandboxed, decision.posture) == (False, "native")

        self._gateway(monkeypatch, found=True)
        with _without_credential():
            contained = self._resolve(
                monkeypatch,
                self._config(
                    providers={"codex": self.PROVIDER},
                    images={"codex": "kew-agent-codex:test"},
                ),
                "codex",
            )
        assert (contained.posture, contained.image) == (
            "sandboxed",
            "kew-agent-codex:test",
        )

    def test_codex_is_refused_when_this_host_has_no_gateway_cli(self, monkeypatch):
        """A configured runtime refuses; it does not downgrade (#2623).

        `_offered_runtime` used to answer `None` whenever `detect_runtime_cached`
        failed, so on a host with no `openshell` CLI codex fell back to its own
        seatbelt as `native` — no runtime, no notice — although the operator had
        asked for containment by naming the runtime. Codex now hears the same
        refusal as `claude-code`, which names the missing runtime and its CLI.
        """
        from kew import sandbox

        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda cfg: None)
        config = self._config(
            providers={"codex": self.PROVIDER, "claude-code": self.PROVIDER},
            images={"codex": "kew-agent-codex:test"},
        )

        for runner in ("codex", "claude-code"):
            with (
                _without_credential(),
                pytest.raises(SandboxRuntimeUnavailable) as refused,
            ):
                sandbox.resolve_effective_sandbox(config, runner=runner)
            assert "'openshell' not found" in str(refused.value)
            assert "OpenShell CLI" in str(refused.value)

    def test_codex_keeps_its_own_boundary_without_a_gateway_cli_only_on_opt_in(
        self, monkeypatch
    ):
        """`dangerously_skip_containment` is the opt-in to the host path.

        With it, codex keeps its own seatbelt (`native`) rather than running with
        no boundary at all, while `claude-code`, holding none of its own, runs
        `uncontained` exactly as before.
        """
        from kew import sandbox
        from kew.config import OpenShellConfig

        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda cfg: None)
        config = SandboxConfig(
            runtime="openshell",
            enabled="auto",
            dangerously_skip_containment=True,
            openshell=OpenShellConfig(
                gateway="http://gateway.invalid:8080",
                providers={"codex": self.PROVIDER, "claude-code": self.PROVIDER},
            ),
            images={"codex": "kew-agent-codex:test"},
        )

        with _without_credential():
            decision = sandbox.resolve_effective_sandbox(config, runner="codex")
        assert (decision.sandboxed, decision.posture) == (False, "native")
        assert (decision.runtime, decision.image) == (None, None)

        with _without_credential():
            fallback = sandbox.resolve_effective_sandbox(config, runner="claude-code")
        assert (fallback.sandboxed, fallback.posture) == (False, "uncontained")

    def test_a_preserved_continuation_leaves_codex_its_own_boundary(self, monkeypatch):
        """The continuation shape is another dispatch with no container on offer.

        A preserved continuation runs against an existing host worktree, which
        the container path cannot take without replacing the checkout the
        operator asked to keep — so `_resolve_and_apply_sandbox(host_only=True)`
        refuses any sandboxed decision. Resolving that shape without the flag
        made codex defer to a gateway boundary this launch was never going to
        get, and `continuation_host_only` killed a continuation that runs
        `native` on docker (PR #2461 review round 3).

        The refusal itself is untouched: `claude-code` holds no boundary of its
        own, so the same continuation still resolves `sandboxed` here and is
        still refused by that guard
        (`tests/test_dispatch_core.py::test_existing_continuation_refuses_a_required_sandbox_posture`).
        """
        from kew import sandbox

        self._gateway(monkeypatch, found=True)
        self._open_egress_gate(monkeypatch)
        monkeypatch.setattr(
            sandbox, "detect_runtime_cached", lambda cfg: "/usr/bin/openshell"
        )
        config = self._config(
            providers={"codex": self.PROVIDER, "claude-code": self.PROVIDER},
            images={"codex": "kew-agent-codex:test", "claude-code": "kew-agent:test"},
        )

        with _without_credential():
            decision = sandbox.resolve_effective_sandbox(
                config, runner="codex", host_only=True
            )
        assert (decision.sandboxed, decision.posture) == (False, "native")

        with _without_credential():
            contained = sandbox.resolve_effective_sandbox(
                config, runner="claude-code", host_only=True
            )
        assert (contained.sandboxed, contained.posture) == (True, "sandboxed")

    def test_a_sidecar_launch_is_not_brokered_and_keeps_its_own_credential(
        self, monkeypatch, tmp_path
    ):
        """AC2: the supervisor mints the worker credential; no provider holds it.

        A sidecar launch authenticates its worker with the per-attempt
        synthetic credential `prepare_sidecar_worker` injects, so the gateway is
        not in its credential path on ANY runtime. Consulting the gateway for
        one refused it `broker_provider_missing` over a long-lived provider it
        never uses — on a config that admits under `runtime = "docker"` — and
        attesting the provider dropped the credential that did authenticate it
        (#2318 review).

        The route itself is no longer ADMITTED on this runtime (it has no launch
        path — see above); what is asserted here is the credential story the
        route keeps wherever it does launch, and that the attestation writer
        tells it.
        """
        from kew import sandbox
        from kew.openshell_broker import SIDECAR_ROUTE, broker_route
        from kew.run_store import SqliteRunStore

        monkeypatch.setattr(
            sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
        )
        assert broker_route("claude-code", direct=False, sidecar=True) == SIDECAR_ROUTE
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="claude-code",
                config=self._config(),
                posture="sandboxed",
                runtime="openshell",
                image=SANDBOX_IMAGE,
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
                egress_route="sidecar",
                synthetic_credential_name="ANTHROPIC_API_KEY",
                direct_transport=False,
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        assert attested == ["ANTHROPIC_API_KEY"]
        assert not [name for name in attested if name.startswith("provider:")]

    def test_one_launch_puts_one_provider_name_on_the_ledger(
        self, monkeypatch, tmp_path
    ):
        """AC2: the posture event and the route profile name the same provider.

        Posture is attested before the key is minted and the realized route
        profile after it, so each derives the provider its own way. A run whose
        minted key goes into `kew-run-<run_id>` used to be attested twice — once
        naming that provider, once naming the operator's long-lived one — which
        leaves the chain with two answers to "which provider held it?".
        """
        from kew import sandbox
        from kew.openshell_broker import launch_provider, run_provider_name
        from kew.run_store import SqliteRunStore

        monkeypatch.setattr(
            sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
        )
        monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-2318")
        config = self._config(providers={"opencode": "kew-opencode"})
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="opencode",
                config=config,
                posture="sandboxed",
                runtime="openshell",
                image="kew-opencode:pinned",
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        per_run = run_provider_name("run-2318")
        assert attested[0] == f"provider:{per_run}"
        assert "provider:kew-opencode" not in attested
        # The realized route profile reaches the same provider from the key.
        assert (
            launch_provider(config, "opencode", run_id="run-2318", issued=True)
            == per_run
        )

    def test_a_run_that_mints_nothing_is_attested_with_the_mapped_provider(
        self, monkeypatch, tmp_path
    ):
        """The other half of the pair: claude-code has no key for kew to mint."""
        from kew import sandbox
        from kew.openshell_broker import launch_provider
        from kew.run_store import SqliteRunStore

        monkeypatch.setattr(
            sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
        )
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
        config = self._config()
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="claude-code",
                config=config,
                posture="sandboxed",
                runtime="openshell",
                image=SANDBOX_IMAGE,
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        assert attested[0] == f"provider:{self.PROVIDER}"
        assert (
            launch_provider(config, "claude-code", run_id="run-2318", issued=False)
            == self.PROVIDER
        )

    def test_an_inherited_bounded_key_is_brokered_like_a_minted_one(
        self, monkeypatch, tmp_path
    ):
        """AC2: holding the driver's key is holding a key for a per-run provider.

        A detached dispatch child is handed the bounded key the graph driver
        minted; the provisioning key that minted it stayed in the driver, so
        this process mints nothing and still owns a per-run key. Reading the
        provisioning key alone refused such a launch over a mapped provider it
        never uses and attested that provider beside a realized route profile
        naming `kew-run-<run_id>` — two answers to "which provider held it?"
        (#2318 review).
        """
        from kew import sandbox
        from kew.openshell_broker import (
            MINTED_ROUTE,
            broker_route,
            launch_provider,
            run_provider_name,
        )
        from kew.run_store import SqliteRunStore

        monkeypatch.setattr(
            sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
        )
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
        monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
        config = self._config(providers={}, images={"opencode": "kew-opencode:pinned"})
        assert broker_route("opencode", direct=True) == MINTED_ROUTE
        # Admitted without a mapped provider: the gateway is never asked for
        # one, because this launch's key goes into its own per-run provider.
        asked = self._gateway(monkeypatch, found=False)
        with _without_credential():
            decision = self._resolve(monkeypatch, config, runner="opencode")
        assert (decision.sandboxed, decision.posture) == (True, "sandboxed")
        assert asked == []
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="opencode",
                config=config,
                posture="sandboxed",
                runtime="openshell",
                image="kew-opencode:pinned",
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        per_run = run_provider_name("run-2318")
        assert attested[0] == f"provider:{per_run}"
        assert "provider:kew-opencode" not in attested
        # The realized route profile reaches the same provider from the key.
        assert (
            launch_provider(config, "opencode", run_id="run-2318", issued=True)
            == per_run
        )

    def test_a_native_runner_is_contained_by_itself_and_names_no_provider(
        self, monkeypatch, tmp_path
    ):
        """No container, no gateway: a `native` posture brokers nothing.

        A guard on the gate, not a past bug: `native` already fell through to
        the host branch of `_posture_credential_names`. It is asserted here
        because a brokered provider name on a runner contained by itself would
        claim a gateway in a launch that has none.
        """
        from kew import sandbox
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox.emit_resolved_posture(
                store,
                runner="claude-code",
                config=self._config(),
                posture=sandbox.POSTURE_NATIVE,
                runtime=None,
                image=None,
                reason=None,
                run_id="run-2318",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=2318,
                item_id="2318",
            )
            events = store.list_audit_events(
                event="sandbox_resolved", run_id="run-2318", limit=None
            )
        attested = events[-1].payload["credential_names"]
        assert not [name for name in attested if name.startswith("provider:")]

    def test_attested_credential_names_say_provider_and_placeholder(self):
        """AC2: no bare variable name, which would claim an injected value."""
        from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE
        from kew.sandbox import posture_record_fields, runner_credential_env_vars

        declared = runner_credential_env_vars("claude-code")
        assert declared
        with _with_credential():
            brokered = posture_record_fields(
                runner="claude-code",
                posture="sandboxed",
                egress_verification="unavailable",
                runtime="openshell",
                openshell_provider=self.PROVIDER,
            )
            docker = posture_record_fields(
                runner="claude-code",
                posture="sandboxed",
                egress_verification="unavailable",
                runtime="docker",
            )
        assert brokered["credential_names"] == [
            f"provider:{self.PROVIDER}",
            *(f"placeholder:{name}" for name in declared),
        ]
        # The docker path is unchanged, and the two are not confusable.
        assert docker["credential_names"] == ["ANTHROPIC_API_KEY"]
        assert PLACEHOLDER_CREDENTIAL_VALUE not in str(brokered)

    def test_the_auth_path_names_the_provider_and_the_ledger_accepts_it(self):
        """AC2: `broker:openshell:<provider>`, valid under the auth-path grammar."""
        from kew.openshell_broker import broker_auth_path, launch_provider
        from kew.readiness import Containment, RealizedLaunchBoundary

        config = self._config()
        assert broker_auth_path(self.PROVIDER) == f"broker:openshell:{self.PROVIDER}"
        # A run with its own minted key authenticates through that key's
        # per-run provider; every other brokered run through the mapped one.
        assert (
            launch_provider(config, "claude-code", run_id="run-2318", issued=True)
            == "kew-run-run-2318"
        )
        assert (
            launch_provider(config, "claude-code", run_id="run-2318", issued=False)
            == self.PROVIDER
        )
        assert (
            launch_provider(
                SandboxConfig(), "claude-code", run_id="run-2318", issued=False
            )
            is None
        )
        boundary = RealizedLaunchBoundary(
            containment=Containment.CONTAINED,
            image_ref="kew-agent:docker-e6dd366f7e41",
            image_id="sha256:" + "a" * 64,
            auth_path=broker_auth_path(run_provider := "kew-run-run-2318"),
        )
        assert boundary.auth_path == f"broker:openshell:{run_provider}"

    def test_every_configurable_provider_name_fits_the_auth_path_grammar(self):
        """The config gate and the ledger grammar cannot drift apart.

        A provider name config accepts but `_AUTH_PATH_RE` refuses would be a
        dispatch that passes admission and then dies at profile composition,
        long after the config that caused it was loaded.
        """
        from kew.config import OpenShellConfig
        from kew.openshell_broker import broker_auth_path, run_provider_name
        from kew.readiness import _AUTH_PATH_RE

        for name in ("kew-codex", "kew.claude_code", "A0", "x" * 64):
            accepted = OpenShellConfig(providers={"codex": name}).providers["codex"]
            assert _AUTH_PATH_RE.fullmatch(broker_auth_path(accepted)), name
        for rejected in ("kew codex", "-kew", "kew#codex", "x" * 65, ""):
            with pytest.raises(ValueError, match="sandbox.openshell.providers"):
                OpenShellConfig(providers={"codex": rejected})
        assert _AUTH_PATH_RE.fullmatch(broker_auth_path(run_provider_name("a" * 32)))
