"""The kew-switchyard image tooling is opt-in; the default skips only it.

The image builds from a pinned private source input that exists on the private
runner alone, so nothing Switchyard runs unless KEW_SWITCHYARD_IMAGE is "1".
ci.yml and release.yml set it exactly when the repository is private (pinned in
tests/test_workflow_runner_visibility.py). These tests pin what the default
does: exactly the tests marked ``requires_switchyard_image`` are deselected,
and scripts/scan-agent-image.sh drops only its Switchyard layer and never
reaches the private archive. "1" runs everything, and any other value refuses.
"""

from __future__ import annotations

import ast
import datetime
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from support import repo_inventory
from support.scan_checkout import pin_stub_trivy, synthetic_scan_checkout

from kew.hosts import pinned_trivy_version

KEW_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = KEW_DIR.parent
TESTS_DIR = KEW_DIR / "tests"
SCAN_SCRIPT = REPO_ROOT / "scripts" / "scan-agent-image.sh"
#: Read off the gate script rather than restated: the array held three
#: agent images while this module still expected two, so the default-run
#: assertion failed on a tree where the gate was correct (#2317 review).
AGENT_DOCKERFILES = tuple(
    re.findall(r"^\s+\"agent\|(\S+?)\|", SCAN_SCRIPT.read_text(), re.M)
)

MARKER = "requires_switchyard_image"
RUN_ENV = "KEW_SWITCHYARD_IMAGE"
REASON = (
    "the pinned Switchyard input includes unreleased upstream patches; "
    "it builds on the private runner until they are public"
)

# Every test that starts the SC-0 topology (it resolves the kew-switchyard
# image) or builds that image from the pinned private source input.
EXPECTED_MARKED = {
    "tests/test_agent_image_scan_gate.py::"
    "test_proxy_image_builds_from_exact_pinned_revision",
    "tests/test_switchyard_sidecar.py::test_internal_worker_reaches_only_proxy_listener",
    "tests/test_switchyard_sidecar.py::"
    "test_packet_matrix_enters_the_durable_production_contract",
    "tests/test_switchyard_sidecar.py::"
    "test_worker_netns_filter_blocks_host_gateway_and_peers",
    "tests/test_switchyard_sidecar.py::test_worker_netns_filter_allows_only_proxy",
    "tests/test_switchyard_sidecar.py::test_netns_filter_preserves_sc0_topology_baseline",
    "tests/test_switchyard_sidecar.py::test_filter_readback_failure_prevents_agent_start",
    "tests/test_switchyard_sidecar.py::"
    "test_proxy_image_starts_nonroot_with_final_hardening",
    "tests/test_switchyard_sidecar.py::"
    "test_egress_peer_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_host_gateway_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_macos_host_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::"
    "test_external_address_positive_control_then_worker_denial",
    "tests/test_switchyard_sidecar.py::test_dns_udp_ipv6_and_extra_proxy_port_are_denied",
    "tests/test_switchyard_sidecar.py::test_worker_and_proxy_share_no_pid_mount_or_socket",
    "tests/test_switchyard_sidecar.py::test_failed_topology_start_removes_all_objects",
}

# Serial tests that need Docker but not the Switchyard image. They must stay
# selected on a run that has not opted in.
DEFAULT_SERIAL_CONTROLS = (
    "tests/test_sandbox.py::TestIsolatedSidecarNetworkArgv::"
    "test_sidecar_filter_never_mutates_global_firewall",
    "tests/test_agent_image.py::TestAgentDockerfileLiveBuild::"
    "test_the_image_the_class_uses_is_present_locally",
)

# The calls that start the real topology or build the image.
IMAGE_ENTRY_POINTS = ("_TopologyAttempt", "_run_packet_gate", "--build-switchyard-only")


def _env(value: str | None) -> dict[str, str]:
    # The conftest autouse fixture has already removed the ambient switch.
    # repo_inventory.share() hands a nested run this session's collection: a
    # child that drives one of the repo-wide gates would otherwise collect the
    # whole tree again to answer it (#2340).
    env = dict(os.environ) | repo_inventory.share()
    if value is not None:
        env[RUN_ENV] = value
    return env


def _pytest(args: list[str], opt_in: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=KEW_DIR,
        env=_env(opt_in),
        capture_output=True,
        text=True,
        check=False,
    )


def _collect(args: list[str], opt_in: str | None) -> list[str]:
    proc = _pytest(["--collect-only", "-q", *args], opt_in)
    ids = [line for line in proc.stdout.splitlines() if "::" in line]
    assert proc.returncode == 0 or (proc.returncode == 5 and not ids), (
        f"collection failed (exit {proc.returncode}):\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    return ids


# --- The marked set ----------------------------------------------------------


def test_the_marker_is_registered() -> None:
    text = (KEW_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{MARKER}: ' in text


def test_exactly_the_image_dependent_tests_carry_the_marker() -> None:
    """Over the whole suite, from the session's own collection (#2340).

    The inventory is recorded before the deselection, so this reads the marked
    set out of a run that deselected every one of these tests — which is also
    what pins the record to that point.
    """
    marked = repo_inventory.ids_with_mark(MARKER)
    assert marked == EXPECTED_MARKED, (
        f"unexpected: {sorted(marked - EXPECTED_MARKED)}; "
        f"missing: {sorted(EXPECTED_MARKED - marked)}"
    )


def test_marked_tests_all_run_in_the_serial_phase() -> None:
    assert repo_inventory.ids_with_mark("serial") >= EXPECTED_MARKED


def _tests_reaching_the_image(path: Path) -> dict[str, bool]:
    """Every serial test in ``path`` → whether it reaches an image entry point."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, bool] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        decorators = [ast.unparse(d) for d in node.decorator_list]
        if "pytest.mark.serial" not in decorators:
            continue
        source = ast.unparse(node)
        reaches = "topology" in [a.arg for a in node.args.args] or any(
            entry in source for entry in IMAGE_ENTRY_POINTS
        )
        found[node.name] = reaches
    return found


def test_every_real_image_test_is_marked_and_no_other() -> None:
    """A serial test that starts the topology or builds the image carries the
    marker; a serial test that only uses fakes does not. Structural, so a new
    topology test cannot land unmarked and fail every default run on a
    missing image."""
    marked_names = {node.split("::")[-1] for node in EXPECTED_MARKED}
    for name in ("test_switchyard_sidecar.py", "test_agent_image_scan_gate.py"):
        for test, reaches in _tests_reaching_the_image(TESTS_DIR / name).items():
            if not reaches:
                assert test not in marked_names, f"{test} needs no image"
                continue
            # The chunk-cancellation tests drive the build path against a
            # fake Docker; they need no real image.
            if test.startswith("test_chunk_shipped_wrapper_real_cancellation"):
                assert test not in marked_names
                continue
            assert test in marked_names, f"{name}::{test} reaches the image"


SWITCHYARD_ONLY_ACTIONS = ("--build-switchyard-only", "--validate-switchyard-source")


def _direct_scan_launches(path: Path) -> list[tuple[str, str, str]]:
    """``(where, call source, enclosing source)`` per Switchyard-only launch.

    A call that names bash and a Switchyard-only action launches the scan
    script itself, rather than through a helper that supplies the environment.
    """
    launches: list[tuple[str, str, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        enclosing = ast.unparse(node)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            source = ast.unparse(call)
            if "bash" in source and any(a in source for a in SWITCHYARD_ONLY_ACTIONS):
                launches.append((f"{path.name}::{node.name}", source, enclosing))
    return launches


@pytest.mark.policy
def test_every_switchyard_only_launch_in_the_suite_opts_in() -> None:
    """A test that runs a Switchyard-only action hands the child the switch.

    The conftest autouse fixture removes it from ``os.environ``, so a launch
    that inherits the test's environment reaches the script with the switch
    unset and is refused — on the opted-in private runner, the one place the
    marked test is selected. The opt-in must therefore be restated at the
    launch, and this finds a launch that forgot.
    """
    # This module is the one that drives the refusal path on purpose, through
    # _scan, which takes the value as an argument.
    launches = [
        entry
        for module in sorted(TESTS_DIR.rglob("test_*.py"))
        if module.name != Path(__file__).name
        for entry in _direct_scan_launches(module)
    ]
    assert launches, "no Switchyard-only launch found; has the shape changed?"
    for where, source, enclosing in launches:
        assert "env=" in source, f"{where} inherits the stripped environment"
        assert f"'{RUN_ENV}': '1'" in enclosing, f"{where} does not opt in"


@pytest.mark.policy
def test_the_marker_is_used_only_in_the_two_image_modules() -> None:
    users = sorted(
        path.name
        for path in TESTS_DIR.rglob("test_*.py")
        if f"pytest.mark.{MARKER}" in path.read_text(encoding="utf-8")
    )
    assert users == ["test_agent_image_scan_gate.py", "test_switchyard_sidecar.py"]


# --- The deselection ---------------------------------------------------------

_PROBE = [
    "-m",
    "serial",
    "tests/test_switchyard_sidecar.py",
    "tests/test_agent_image_scan_gate.py",
    *DEFAULT_SERIAL_CONTROLS,
]


def test_the_opt_in_selects_every_image_test() -> None:
    collected = set(_collect(_PROBE, "1"))
    assert collected >= EXPECTED_MARKED
    assert collected >= set(DEFAULT_SERIAL_CONTROLS)


def test_unset_or_empty_deselects_only_the_marked_tests() -> None:
    everything = set(_collect(_PROBE, "1"))
    for value in (None, ""):
        default = set(_collect(_PROBE, value))
        assert default == everything - EXPECTED_MARKED
        assert default >= set(DEFAULT_SERIAL_CONTROLS)


@pytest.mark.parametrize("value", [None, ""])
def test_the_default_prints_the_reason(value: str | None) -> None:
    proc = _pytest(["--collect-only", "-q", *_PROBE], value)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert f"deselected {len(EXPECTED_MARKED)} @pytest.mark.{MARKER}" in proc.stdout
    assert REASON in proc.stdout
    header = _pytest(["--collect-only", *_PROBE], value)
    assert f"{RUN_ENV} is not 1: deselecting @pytest.mark.{MARKER} tests: {REASON}" in (
        header.stdout
    )


def test_no_test_sees_the_switch() -> None:
    """The conftest autouse fixture removes it; the deselection already ran."""
    assert RUN_ENV not in os.environ


def test_the_opt_in_never_reaches_a_scan_script_under_test() -> None:
    """ci.yml exports the switch to the whole private `test` job. A test that
    drives scan-agent-image.sh through a copy of os.environ must still see the
    default behaviour, or the private job's first command goes red."""
    proc = _pytest(
        [
            "-q",
            "tests/test_agent_image_scan_gate.py::TestSwitchyardArchivePreflight",
            "tests/test_serial_phase.py::"
            "test_the_serial_set_is_exactly_the_pilot_modules_plus_the_pinned_tests",
        ],
        "1",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert " failed" not in proc.stdout


@pytest.mark.parametrize("value", ["true", "0", "yes", " 1"])
def test_any_other_value_is_a_usage_error(value: str) -> None:
    proc = _pytest(["--collect-only", "-q", "tests/test_switchyard_sidecar.py"], value)
    assert proc.returncode != 0
    assert f"{RUN_ENV} must be unset, empty or 1" in proc.stdout + proc.stderr


def test_the_reason_is_one_string_everywhere() -> None:
    conftest = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    assert '"the pinned Switchyard input includes unreleased upstream patches; "' in (
        conftest
    )
    assert '"it builds on the private runner until they are public"' in conftest
    for path in (
        SCAN_SCRIPT,
        REPO_ROOT / ".github" / "workflows" / "release.yml",
    ):
        assert REASON in path.read_text(encoding="utf-8"), path
    docs = " ".join((REPO_ROOT / "docs" / "operations.md").read_text().split())
    assert REASON in docs


# --- The image scan ----------------------------------------------------------


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture()
def fake_tools(tmp_path: Path) -> Path:
    """A docker that records builds and a trivy that reports a full inventory."""
    binroot = tmp_path / "bin"
    binroot.mkdir()
    log = tmp_path / "docker.log"
    _shim(
        binroot,
        "docker",
        f"""
        printf '%s\\n' "$*" >> "{log}"
        case "$1" in
          build)
            for a in "$@"; do
              if [ "$prev" = "--iidfile" ]; then echo "sha256:{"a" * 64}" > "$a"; fi
              prev="$a"
            done ;;
        esac
        exit 0
        """,
    )
    # The one pin file the gate, the provisioning recipe and CI all read (#2462).
    pinned_trivy = pinned_trivy_version()
    stamp = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shim(
        binroot,
        "trivy",
        f"""
        if [ "$1" = "--version" ]; then echo "Version: {pinned_trivy}"; exit 0; fi
        if printf '%s\\n' "$@" | grep -q -- --download-db-only; then exit 0; fi
        if [ "$1" = "version" ]; then
          echo '{{"VulnerabilityDB":{{"UpdatedAt":"{stamp}"}}}}'
          exit 0
        fi
        if printf '%s\\n' "$@" | grep -q -- --list-all-pkgs; then
          printf '{{"Results":[{{"Class":"os-pkgs","Type":"debian","Packages":['
          for i in $(seq 1 60); do [ "$i" -gt 1 ] && printf ','; printf '{{}}'; done
          printf ']}},{{"Class":"lang-pkgs","Type":"node-pkg","Packages":['
          for i in $(seq 1 60); do [ "$i" -gt 1 ] && printf ','; printf '{{}}'; done
          printf ']}}]}}'
          exit 0
        fi
        echo "no findings"
        """,
    )
    return binroot


def _scan(
    binroot: Path, opt_in: str | None, *arguments: str
) -> subprocess.CompletedProcess[str]:
    env = _env(opt_in)
    env["PATH"] = f"{binroot}{os.pathsep}{env['PATH']}"
    # A machine that has not opted in holds no private input; the secret is
    # mapped only into opted-in steps, so it resolves empty.
    env["KEW_SWITCHYARD_SOURCE_ARCHIVE"] = ""
    # The trivy here is a stub, so the scan runs from a synthetic checkout
    # whose pin names the stub's digest wherever it pins binaries (#3258); the
    # shipped pin is never rewritten.
    checkout = binroot.parent / "checkout"
    script = synthetic_scan_checkout(checkout)
    pin_stub_trivy(checkout, binroot / "trivy")
    return subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_scan_script_declares_the_agent_images_this_module_expects() -> None:
    """The parse above is load-bearing; an empty match would assert nothing."""
    assert "docker/agent.Dockerfile" in AGENT_DOCKERFILES
    assert "docker/agent-opencode.Dockerfile" in AGENT_DOCKERFILES
    assert "docker/agent-codex.Dockerfile" in AGENT_DOCKERFILES
    assert not any("switchyard" in name for name in AGENT_DOCKERFILES)


@pytest.mark.parametrize("value", [None, ""])
def test_the_default_scan_gates_every_agent_image_and_skips_only_switchyard(
    fake_tools: Path, value: str | None
) -> None:
    result = _scan(fake_tools, value)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    builds = [
        line
        for line in (fake_tools.parent / "docker.log").read_text().splitlines()
        if line.startswith("build ")
    ]
    assert len(builds) == len(AGENT_DOCKERFILES)
    for dockerfile in AGENT_DOCKERFILES:
        assert any(f"-f {dockerfile} " in line for line in builds), dockerfile
    assert not any("switchyard" in line for line in builds)
    skipped = "==> skipping docker/switchyard.Dockerfile -> kew-switchyard:ci-scan: "
    assert skipped + REASON in result.stdout
    assert f"the Switchyard proxy was skipped: {REASON}" in result.stdout
    # The certification names what was gated, not a counting word that was
    # true of two images and wrong for three (#2317 review).
    assert (
        f"\u2713 all {len(AGENT_DOCKERFILES)} agent images passed" in result.stdout
    ), result.stdout
    # The default never reaches the private source input at all: with no
    # archive present, any validation attempt would have failed the run.
    assert "Switchyard source archive" not in result.stdout + result.stderr


def test_the_opted_in_scan_still_requires_the_source_input(fake_tools: Path) -> None:
    result = _scan(fake_tools, "1")
    assert result.returncode != 0
    assert "Switchyard source archive validation failed" in result.stderr
    assert not (fake_tools.parent / "docker.log").exists()


@pytest.mark.parametrize("value", ["true", "0", "yes"])
def test_scan_refuses_any_other_value(fake_tools: Path, value: str) -> None:
    result = _scan(fake_tools, value)
    assert result.returncode != 0
    assert f"{RUN_ENV} must be unset, empty or 1" in result.stderr
    assert not (fake_tools.parent / "docker.log").exists()


@pytest.mark.parametrize(
    "action", ["--build-switchyard-only", "--validate-switchyard-source"]
)
def test_the_default_scan_refuses_switchyard_only_actions(
    fake_tools: Path, action: str
) -> None:
    result = _scan(fake_tools, None, action)
    assert result.returncode != 0
    assert f"skipped unless {RUN_ENV}=1" in result.stderr
    assert REASON in result.stderr
    assert not (fake_tools.parent / "docker.log").exists()


# --- The opted-in run is explicit --------------------------------------------
#
# Opting in is a claim that this machine runs the sidecar packet
# certification. A drifting marker or a broken deselection would empty that
# set silently, so the opted-in workflow jobs assert the selection first.
# tests/test_workflow_runner_visibility.py pins that they run this script.

ASSERT_SCRIPT = (
    REPO_ROOT / ".github" / "scripts" / "assert_switchyard_tests_selected.sh"
)


@pytest.fixture()
def fake_uv(tmp_path: Path) -> Path:
    """A ``uv`` whose pytest collection the test controls, and that records."""
    binroot = tmp_path / "bin"
    binroot.mkdir(exist_ok=True)
    _shim(
        binroot,
        "uv",
        f"""
        printf '%s\\n' "$*" >> "{tmp_path / "uv.log"}"
        cat "{tmp_path / "collected"}"
        exit "$(cat "{tmp_path / "exit"}")"
        """,
    )
    (tmp_path / "collected").write_text(
        "".join(f"{node}\n" for node in sorted(EXPECTED_MARKED))
        + f"{len(EXPECTED_MARKED)} tests collected in 0.42s\n"
    )
    (tmp_path / "exit").write_text("0")
    return binroot


def _assert_selected(
    binroot: Path, opt_in: str | None
) -> subprocess.CompletedProcess[str]:
    env = _env(opt_in)
    env["PATH"] = f"{binroot}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(ASSERT_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_assertion_passes_when_the_opted_in_run_selects_the_tests(
    fake_uv: Path,
) -> None:
    result = _assert_selected(fake_uv, "1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(EXPECTED_MARKED)} @pytest.mark.{MARKER} tests selected" in (
        result.stdout
    )
    assert (
        f"-m {MARKER} --collect-only -q tests"
        in (fake_uv.parent / "uv.log").read_text()
    )


def test_the_assertion_fails_when_an_opted_in_run_selects_nothing(
    fake_uv: Path,
) -> None:
    """A marker that stops matching must fail the job, not pass it quietly."""
    (fake_uv.parent / "collected").write_text("no tests ran in 0.01s\n")
    (fake_uv.parent / "exit").write_text("5")
    result = _assert_selected(fake_uv, "1")
    assert result.returncode != 0
    assert f"::error::{RUN_ENV}=1 but no @pytest.mark.{MARKER} test was selected" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("value", [None, ""])
def test_the_assertion_has_nothing_to_assert_by_default(
    fake_uv: Path, value: str | None
) -> None:
    result = _assert_selected(fake_uv, value)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{RUN_ENV} is not 1" in result.stdout
    assert not (fake_uv.parent / "uv.log").exists(), "it collected anyway"
