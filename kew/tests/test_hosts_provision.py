"""One checked-in recipe builds a host, and kew records what it reports.

The recipe is driven two ways here. Its **self-check** runs for real, against a
stub PATH of fake tools, because that is the half an operator depends on: a
host missing one tool has to fail loudly at provisioning time rather than an
hour later in a verification run. The **provisioning run** is driven through
the fake ``gcloud`` on PATH, so no test touches a real cloud.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from support.fake_gcloud import fake_gcloud_on_path, launchable
from support.fake_gcloud import read_state as _read_state
from support.fake_gcloud import recorded_calls as _calls
from support.fake_gcloud import write_state as _write_state
from support.required_tools import HOST_PROVIDED_TOOLS, REQUIRED_TOOLS
from support.takeover_ledger import dead_holder
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, GcpHostConfig, HostsConfig, LoggingConfig
from kew.host_takeovers import LEASE_LAPSED_EVENT
from kew.hosts import (
    DEFAULT_RECIPE,
    PIN_FILE,
    GcpProvider,
    HostError,
    LocalProvider,
    host_lock_dir,
    pin_path,
    pinned_trivy_version,
    provider_named,
    provision_host,
    read_pins,
    read_record,
    recipe_path,
    record_path,
)
from kew.hosts.environment import REMOTE_PATH, remote_path_prefix
from kew.hosts.gcp import REMOTE_WORKSPACE
from kew.hosts.provisioning import parse_tool_versions
from kew.merge_verify import HOST_BROWSERS_PATH, UI_CHECK_COMMAND
from kew.sandbox import ALLOWLIST_NETWORK

runner = CliRunner()

#: The repository root, for the paths this module reads and prints.
REPO_ROOT = Path(__file__).parent.parent.parent

#: Every tool the recipe's self-check insists on, and the command each one
#: answers a version with. Kept here, not read out of the recipe, so a tool
#: quietly dropped from the recipe fails this suite. ``ripgrep`` onward are
#: here because tests need them on a verify host (#2763).
STUB_TOOLS = (
    "git",
    "docker",
    "node",
    "npm",
    "uv",
    "trivy",
    "playwright-core",
    "ripgrep",
    "ruby",
    "perl",
    "jq",
    "lsof",
    "ssh",
    "gitleaks",
)

#: The executable a stub tool is installed as, where it is not the tool's name.
STUB_EXECUTABLES = {"ripgrep": "rg"}


#: The secrets scanner verify.sh gates on, read out of verify.sh itself so the
#: recipe is held to the gate's pin rather than to a third literal.
def _verify_sh_gitleaks_pin() -> str:
    verify = (REPO_ROOT / "kew" / "scripts" / "verify.sh").read_text(encoding="utf-8")
    match = re.search(r'^GITLEAKS_VERSION="([^"]+)"$', verify, flags=re.MULTILINE)
    assert match is not None, "scripts/verify.sh no longer pins GITLEAKS_VERSION"
    return match[1]


PINNED_GITLEAKS = _verify_sh_gitleaks_pin()

#: The pins the recipe installs, asserted against the recipe's own text below
#: so a stub that drifts from the file fails rather than passing quietly.
PINNED_NODE_MAJOR = "22"
PINNED_PLAYWRIGHT = "1.63.0"

#: The scanner pin, read from the one file the recipe installs it from and
#: ``scripts/scan-agent-image.sh`` gates on (#2462) — never restated here, which
#: is the whole point of the file.
PINNED_TRIVY = pinned_trivy_version()

#: The fake docker. `docker --version` is answered by the client alone, which
#: is why the recipe asks for the *server* version: this stub refuses that when
#: ``STUB_DOCKER_DAEMON`` is not ``up``, exactly as a real client with no
#: reachable dockerd does, while still answering `docker --version` happily.
DOCKER_STUB = """\
#!/bin/sh
if [ "$1" = "version" ] && [ "${STUB_DOCKER_DAEMON:-up}" != "up" ]; then
    echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock." >&2
    exit 1
fi
echo "Docker version %s, build ce12230"
"""

#: The browser build the stub tree pretends to hold.
STUB_BROWSER_BUILD = "1200"

#: The Python the stub host's job login gets from uv: the tested 3.13.
STUB_JOB_PYTHON = "3.13.5"

#: The fake uv. ``uv python find --show-version`` answers the login's Python,
#: ``STUB_JOB_PYTHON`` in the environment when a test sets one; anything else
#: answers uv's own version.
UV_STUB = """\
#!/bin/sh
if [ "$1 $2 $3" = "python find --show-version" ]; then
    echo "${STUB_JOB_PYTHON:-%s}"
    exit 0
fi
echo "uv version %s"
"""

#: The fake systemctl: systemd's own banner, which leads with its version.
SYSTEMCTL_STUB = '#!/bin/sh\necho "systemd 255 (255.4-1ubuntu8.4)"\n'

#: The logind drop-in the recipe's keep_ipc step writes.
KEEP_IPC_DROP_IN = "[Login]\nRemoveIPC=no\n"

#: What the fake host's self-check prints back to kew.
REPORTED_VERSIONS = {
    "git": "2.43.0",
    "docker": "27.3.1",
    "node": "22.9.0",
    "npm": "10.8.3",
    "uv": "0.5.11",
    "trivy": PINNED_TRIVY,
    "playwright-core": "1.63.0",
    "chromium-headless-shell": STUB_BROWSER_BUILD,
    "ripgrep": "14.1.0",
    "ruby": "3.2.3",
    "perl": "5.38.2",
    "jq": "1.7.1",
    "lsof": "4.95.0",
    "ssh": "9.6",
    "gitleaks": PINNED_GITLEAKS,
    "python": STUB_JOB_PYTHON,
    "logind-keep-ipc": "255",
}


def _self_check_output(versions: dict[str, str]) -> str:
    """The recipe's self-check lines, as a provisioned host would print them."""
    lines = [f"KEW_HOST_TOOL {tool}={version}" for tool, version in versions.items()]
    return "==> base packages\n" + "\n".join(lines) + "\nkew host self-check OK\n"


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake ``gcloud`` first on PATH; yields its JSON state file."""
    with fake_gcloud_on_path(tmp_path, monkeypatch) as state:
        yield state


def _settings() -> GcpHostConfig:
    return GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")


def _gcp(tmp_path: Path) -> GcpProvider:
    return GcpProvider(
        _settings(),
        lock_dir=tmp_path / "locks",
        sleep=lambda _seconds: None,
        poll_seconds=0.0,
    )


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        hosts=HostsConfig(provider="gcp", gcp=_settings()),
    )


def _write_stub(bin_dir: Path, tool: str, script: str) -> None:
    """Put an executable *tool* on the stub PATH, running *script*.

    Whatever was there goes first: a symlink to a real tool is replaced, never
    written through.
    """
    stub = bin_dir / tool
    stub.unlink(missing_ok=True)
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)


def _executable(tool: str) -> str:
    """The command *tool* is run as on a host."""
    return STUB_EXECUTABLES.get(tool, tool)


def _version_stub(reported: str) -> str:
    """A stub that answers every invocation with *reported*."""
    return f'#!/bin/sh\necho "{reported}"\n'


@pytest.fixture
def stub_host(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A stub PATH of fake tools and a stub playwright browsers tree.

    The host also has the drop-in keep_ipc writes, in ``logind-conf`` beside
    the bin directory (:func:`_run_self_check` points the recipe at it), and
    the real ``id`` and ``env`` the self-check asks uv as the job's login
    through.

    Yields the bin directory and the browsers directory. The directories go
    under ``HOME`` when the pytest temporary filesystem is mounted ``noexec``,
    for the same reason the fake ``gcloud`` does: the assertion is about the
    recipe, not about the sandbox's mount options.
    """
    fallback: str | None = None
    if launchable(tmp_path):
        root = tmp_path / "stub-host"
    else:
        fallback = tempfile.mkdtemp(prefix="kew-stub-host-", dir=Path.home())
        root = Path(fallback)
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in STUB_TOOLS:
        reported = f"{tool} version {REPORTED_VERSIONS[tool]}"
        _write_stub(bin_dir, _executable(tool), _version_stub(reported))
    _write_stub(bin_dir, "docker", DOCKER_STUB % REPORTED_VERSIONS["docker"])
    _write_stub(bin_dir, "uv", UV_STUB % (STUB_JOB_PYTHON, REPORTED_VERSIONS["uv"]))
    _write_stub(bin_dir, "systemctl", SYSTEMCTL_STUB)
    for tool in ("id", "env"):
        real = shutil.which(tool)
        assert real is not None, f"{tool} is needed to run the self-check"
        (bin_dir / tool).symlink_to(real)
    conf_dir = _logind_conf_dir(bin_dir)
    conf_dir.mkdir()
    (conf_dir / "kew-keep-ipc.conf").write_text(KEEP_IPC_DROP_IN, encoding="utf-8")
    browsers = root / "browsers"
    shell = (
        browsers
        / f"chromium_headless_shell-{STUB_BROWSER_BUILD}"
        / "chrome-linux"
        / "headless_shell"
    )
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text("", encoding="utf-8")
    shell.chmod(0o755)
    try:
        yield bin_dir, browsers
    finally:
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


def _logind_conf_dir(bin_dir: Path) -> Path:
    """Where the stub host keeps its logind drop-ins."""
    return bin_dir.parent / "logind-conf"


def _run_self_check(
    bin_dir: Path, browsers: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Run the recipe's self-check with *bin_dir* as the whole PATH.

    The self-check uses bash builtins only, so the stub tools are the only
    executables it can reach — a tool removed from *bin_dir* is genuinely
    absent, not shadowed by the real one on the developer's machine. Keyword
    arguments go into the environment, which is how a stub is told to behave
    like a tool that is installed but not working.

    ``KEW_JOB_PATH`` is the stub directory too, because that is the PATH the
    self-check looks for the tools on (#2433): a job gets the PATH kew gives
    it, not the one the shell running the recipe happens to have, so the check
    has to answer for the same list. A test that wants the two to *differ*
    passes its own ``PATH``.
    """
    bash = shutil.which("bash")
    assert bash is not None, "bash is needed to run the recipe"
    return subprocess.run(
        [bash, str(recipe_path()), "--self-check"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": str(bin_dir),
            "KEW_JOB_PATH": str(bin_dir),
            "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
            "HOME": os.environ.get("HOME", str(bin_dir)),
            "KEW_LOGIND_CONF_DIR": str(_logind_conf_dir(bin_dir)),
            **environment,
        },
    )


class TestRecipeIsReviewable:
    """AC1 — the recipe passes shellcheck and pins what the drive pinned."""

    def test_the_recipe_ships_with_kew_and_is_executable(self) -> None:
        recipe = recipe_path()
        assert recipe.name == DEFAULT_RECIPE
        assert recipe.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
        assert os.access(recipe, os.X_OK)

    def test_the_recipe_pins_the_playwright_line_the_drive_pinned(self) -> None:
        text = recipe_path().read_text(encoding="utf-8")
        assert f'PLAYWRIGHT_VERSION="{PINNED_PLAYWRIGHT}"' in text
        assert f'NODE_MAJOR="{PINNED_NODE_MAJOR}"' in text

    def test_the_scanner_pin_is_one_value_read_from_one_file(self) -> None:
        """#2462 — the recipe installed no trivy and the gate pinned one.

        A host built by kew's own recipe could not pass ``kew verify``, and a
        host carrying any other trivy failed at the agent-image scan — the last
        step of the gate, after the whole suite had already run. The fix is not
        "write the same version in both files": it is one value, in one file,
        that both of them read.
        """
        recipe = recipe_path().read_text(encoding="utf-8")
        scan = (REPO_ROOT / "scripts" / "scan-agent-image.sh").read_text(
            encoding="utf-8"
        )
        pin = pin_path()

        assert f"{PINNED_TRIVY}" in pin.read_text(encoding="utf-8")
        assert PINNED_TRIVY not in recipe, (
            "the recipe must read the scanner version from the pin file, not "
            "restate it: a second literal is what drifted"
        )
        assert PINNED_TRIVY not in scan, (
            "the gate must read the scanner version from the pin file, not restate it"
        )
        assert f"TRIVY_PIN_FILE:-${{RECIPE_DIR}}/{PIN_FILE}" in recipe, (
            "the recipe must read the pin that travels beside it"
        )
        assert str(pin.relative_to(REPO_ROOT)) in scan, (
            "the gate must read the same shipped pin file the recipe does"
        )
        assert "trivy:exact ${TRIVY_VERSION}:trivy --version" in recipe, (
            "the self-check must hold trivy to the pinned version exactly — a "
            "host answering off it fails provisioning, not every verification"
        )

    def test_the_recipe_verifies_the_archive_digest_before_extracting_it(
        self,
    ) -> None:
        """A version string is self-reported; the digest is what identifies it."""
        recipe = recipe_path().read_text(encoding="utf-8")
        assert "trivy_${TRIVY_VERSION}_${asset}.tar.gz" in recipe, (
            "the recipe must download the pinned version's own archive"
        )
        verify_at = recipe.index("sha256sum -c -")
        extract_at = recipe.index("tar xzf")
        assert verify_at < extract_at, (
            "the archive must be verified BEFORE extraction — a substituted "
            "archive answers --version correctly and scans clean forever"
        )
        pins = read_pins()
        for key in ("TRIVY_SHA256_LINUX_64BIT", "TRIVY_SHA256_LINUX_ARM64"):
            assert key in recipe, f"the recipe must pin the {key} archive"
            assert re.fullmatch(r"[0-9a-f]{64}", pins.get(key, "")), (
                f"{key} must be a 64-hex digest in {pin_path()}"
            )

    def test_a_recipe_that_cannot_read_its_pin_installs_nothing(
        self, tmp_path: Path, stub_host: tuple[Path, Path]
    ) -> None:
        """A missing pin is refused, never defaulted.

        The pin travels with the recipe. If it did not arrive, the recipe has
        no scanner version to install and no version to hold the host to, and
        guessing one would gate with a scanner nobody chose.
        """
        bin_dir, browsers = stub_host
        result = _run_self_check(
            bin_dir, browsers, TRIVY_PIN_FILE=str(tmp_path / "absent.pin")
        )
        assert result.returncode != 0
        assert "no scanner pin" in result.stderr
        assert "KEW_HOST_TOOL" not in result.stdout

    def test_shellcheck_is_a_declared_dev_dependency(self) -> None:
        """The lint below has to be installable, or it is not a gate.

        ``shellcheck`` used to be an optional external tool here, so the lint
        skipped on every machine that happened not to have it — CI included,
        which made AC1's "passes shellcheck" a criterion nobody checked.
        Declaring it in the dev group is what makes ``uv run pytest`` run it.
        """
        pyproject = tomllib.loads(
            (REPO_ROOT / "kew" / "pyproject.toml").read_text(encoding="utf-8")
        )
        declared = pyproject["dependency-groups"]["dev"]
        names = [entry.split(">")[0].split("=")[0].strip() for entry in declared]
        assert "shellcheck-py" in names, (
            "shellcheck-py must stay in [dependency-groups].dev: it is what "
            "puts shellcheck on PATH for the recipe lint"
        )

    def test_the_recipe_passes_shellcheck(self) -> None:
        """Lint the recipe, which builds every verification host.

        A failure here is a real finding, not an environment problem: the
        dev group installs ``shellcheck-py``, so ``uv run pytest`` always has
        the binary. Missing means the suite is being run outside the project
        environment, and the assertion says so rather than skipping.
        """
        shellcheck = shutil.which("shellcheck")
        assert shellcheck is not None, (
            "shellcheck is not on PATH: it ships with the dev dependency "
            "group (shellcheck-py), so run the suite as `uv run pytest` from "
            f"{REPO_ROOT / 'kew'}"
        )
        result = subprocess.run(
            [shellcheck, str(recipe_path())],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_recipe_name_that_is_a_path_is_refused(self) -> None:
        for name in ("../../etc/passwd", "/etc/passwd", "sub/dir.sh", ""):
            with pytest.raises(HostError, match="shipped recipe"):
                recipe_path(name)

    def test_an_unknown_recipe_name_lists_what_ships(self) -> None:
        with pytest.raises(HostError, match=DEFAULT_RECIPE):
            recipe_path("centos-7.sh")


class TestSelfCheck:
    """AC1 — the self-check reports every version, and fails when one is absent."""

    def test_it_prints_a_version_for_every_tool_it_needs(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        result = _run_self_check(*stub_host)
        assert result.returncode == 0, result.stdout + result.stderr
        assert parse_tool_versions(result.stdout) == REPORTED_VERSIONS

    @pytest.mark.parametrize("absent", STUB_TOOLS)
    def test_it_fails_naming_the_tool_that_is_absent(
        self, stub_host: tuple[Path, Path], absent: str
    ) -> None:
        bin_dir, browsers = stub_host
        (bin_dir / _executable(absent)).unlink()
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0
        assert absent in result.stderr
        assert absent not in parse_tool_versions(result.stdout)

    def test_it_fails_when_the_headless_shell_is_absent(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        shutil.rmtree(browsers)
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0
        assert "chromium-headless-shell" in result.stderr

    def test_a_tool_that_reports_no_version_counts_as_absent(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        _write_stub(bin_dir, "docker", "#!/bin/sh\necho 'not installed'\nexit 0\n")
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0
        assert "docker" in result.stderr

    def test_a_node_off_the_pinned_major_is_not_a_provisioned_host(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        # A version-shaped answer is not the answer: a host carrying someone
        # else's Node must not be recorded as built from this recipe.
        bin_dir, browsers = stub_host
        _write_stub(bin_dir, "node", _version_stub("v21.7.3"))
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0, result.stdout
        assert "node" in result.stderr
        assert PINNED_NODE_MAJOR in result.stderr
        assert "node" not in parse_tool_versions(result.stdout)

    def test_a_playwright_core_off_the_pin_is_not_a_provisioned_host(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        _write_stub(bin_dir, "playwright-core", _version_stub("Version 1.2.3"))
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0, result.stdout
        assert "playwright-core" in result.stderr
        assert PINNED_PLAYWRIGHT in result.stderr
        assert "playwright-core" not in parse_tool_versions(result.stdout)

    def test_a_trivy_off_the_pin_is_not_a_provisioned_host(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        """#2462 — the scanner version IS the gate, so near enough is not it.

        0.75.0 on a host the gate pins to 0.69.3 is a verification that runs
        the whole suite and then refuses at the image scan. It has to fail here
        instead, in the seconds provisioning takes.
        """
        bin_dir, browsers = stub_host
        _write_stub(bin_dir, "trivy", _version_stub("Version: 0.75.0"))
        result = _run_self_check(bin_dir, browsers)
        assert result.returncode != 0, result.stdout
        assert "trivy" in result.stderr
        assert PINNED_TRIVY in result.stderr
        assert "trivy" not in parse_tool_versions(result.stdout)

    def test_a_docker_whose_daemon_is_unreachable_fails_the_self_check(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        # `docker --version` answers from the client alone, so a host whose
        # dockerd is dead would otherwise self-check OK and then fail every
        # verification run that tries to start a container.
        bin_dir, browsers = stub_host
        result = _run_self_check(bin_dir, browsers, STUB_DOCKER_DAEMON="down")
        assert result.returncode != 0, result.stdout
        assert "docker" in result.stderr
        assert "docker" not in parse_tool_versions(result.stdout)

    def test_the_ok_line_is_only_printed_when_every_tool_agrees(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        assert "self-check OK" in _run_self_check(bin_dir, browsers).stdout
        _write_stub(bin_dir, "node", _version_stub("v21.7.3"))
        assert "self-check OK" not in _run_self_check(bin_dir, browsers).stdout


#: A docker that remembers networks as files under ``STUB_NETWORKS`` and logs
#: every call to ``STUB_DOCKER_LOG``, so a test reads what the recipe *did* off
#: the daemon's state rather than off the recipe's own output. Shell builtins
#: only: the stub PATH holds nothing else.
NETWORK_DOCKER_STUB = """\
#!/bin/sh
echo "$*" >>"$STUB_DOCKER_LOG"
if [ "$1 $2" = "network inspect" ]; then
    [ -e "$STUB_NETWORKS/$3" ] && exit 0
    echo "Error: No such network: $3" >&2
    exit 1
fi
if [ "$1 $2" = "network create" ]; then
    [ -e "$STUB_NETWORKS/$5" ] && exit 1
    echo "$3 $4" >"$STUB_NETWORKS/$5"
    exit 0
fi
exit 2
"""


class TestTheRecipeCreatesTheAllowlistNetwork:
    """#2607 — a fresh instance refused its first placed dispatch.

    A placed agent container attaches ``kew-allowlist``, and the recipe did not
    create it, so every newly provisioned instance needed a hand step before
    any run could be placed on it. The recipe now creates it with the operator
    machine's shape — a plain bridge — and only when it is absent, so a re-run
    never replaces a network an operator has restricted.
    """

    def _run(
        self, bin_dir: Path, networks: Path, log: Path
    ) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        assert bash is not None, "bash is needed to run the recipe"
        return subprocess.run(
            [bash, str(recipe_path()), "--allowlist-network"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": str(bin_dir),
                "HOME": os.environ.get("HOME", str(bin_dir)),
                "STUB_NETWORKS": str(networks),
                "STUB_DOCKER_LOG": str(log),
            },
        )

    def _host(self, stub_host: tuple[Path, Path]) -> tuple[Path, Path, Path]:
        bin_dir, _ = stub_host
        _write_stub(bin_dir, "docker", NETWORK_DOCKER_STUB)
        networks = bin_dir.parent / "networks"
        networks.mkdir()
        return bin_dir, networks, bin_dir.parent / "docker.log"

    def test_it_creates_the_network_the_sandbox_attaches_as_a_plain_bridge(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, networks, log = self._host(stub_host)
        result = self._run(bin_dir, networks, log)
        assert result.returncode == 0, result.stderr
        created = networks / ALLOWLIST_NETWORK
        assert created.read_text(encoding="utf-8").strip() == "--driver bridge"
        assert [p.name for p in networks.iterdir()] == [ALLOWLIST_NETWORK]

    def test_a_rerun_creates_nothing(self, stub_host: tuple[Path, Path]) -> None:
        bin_dir, networks, log = self._host(stub_host)
        assert self._run(bin_dir, networks, log).returncode == 0
        second = self._run(bin_dir, networks, log)
        assert second.returncode == 0, second.stderr
        calls = log.read_text(encoding="utf-8").splitlines()
        assert [c for c in calls if c.startswith("network create")] == [
            f"network create --driver bridge {ALLOWLIST_NETWORK}"
        ]

    def test_an_operators_existing_network_is_left_alone(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, networks, log = self._host(stub_host)
        existing = networks / ALLOWLIST_NETWORK
        existing.write_text("restricted by the operator\n", encoding="utf-8")
        result = self._run(bin_dir, networks, log)
        assert result.returncode == 0, result.stderr
        assert existing.read_text(encoding="utf-8") == "restricted by the operator\n"
        assert log.read_text(encoding="utf-8").splitlines() == [
            f"network inspect {ALLOWLIST_NETWORK}"
        ]

    def test_a_full_install_creates_it_once_docker_is_running(self) -> None:
        """The step is part of the install, after the daemon it asks is up."""
        text = recipe_path().read_text(encoding="utf-8")
        body = text[text.index("install_all() {") :]
        body = body[: body.index("\n}\n")]
        steps = [line.strip() for line in body.splitlines()[1:]]
        assert steps.index("create_allowlist_network") > steps.index("install_docker")


#: The fake curl: writes the archive at ``STUB_ARCHIVE`` wherever ``-o`` says
#: and logs the URL it was asked for, so nothing leaves the machine.
CURL_STUB = """\
#!/bin/bash
out=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out=$2; shift ;;
        -*) ;;
        *) url=$1 ;;
    esac
    shift
done
echo "$url" >>"$STUB_CURL_LOG"
cat "$STUB_ARCHIVE" >"$out"
"""

#: A gitleaks that answers ``gitleaks version`` with exactly the pin and
#: passes every scan: the binary #2867 found walking past the digest.
SPOOFED_GITLEAKS = f"#!/bin/sh\necho {PINNED_GITLEAKS}\nexit 0\n"


class TestTheRecipeIdentifiesGitleaksByDigest:
    """#2867 — a gitleaks that printed the pin skipped the archive check.

    The step asked the installed ``gitleaks version`` first and returned when
    it matched, so a substituted binary answering ``8.30.0`` was never replaced
    and verify.sh reported a clean secrets gate from it. A version string is
    self-reported; the step now installs the digest-verified archive every run.
    """

    def _host(self, stub_host: tuple[Path, Path]) -> tuple[Path, Path, Path]:
        bin_dir, _ = stub_host
        root = bin_dir.parent
        for tool in ("bash", "cat", "chmod", "gzip", "mktemp", "rm", "tar"):
            real = shutil.which(tool)
            assert real is not None, f"{tool} is needed to run the recipe step"
            (bin_dir / tool).symlink_to(real)
        _write_stub(bin_dir, "uname", "#!/bin/sh\necho x86_64\n")
        _write_stub(bin_dir, "curl", CURL_STUB)
        install_dir = root / "install"
        install_dir.mkdir()
        _write_stub(install_dir, "gitleaks", SPOOFED_GITLEAKS)
        payload = root / "payload"
        payload.mkdir()
        _write_stub(payload, "gitleaks", "#!/bin/sh\necho from-the-archive\n")
        archive = root / "gitleaks.tar.gz"
        subprocess.run(
            ["tar", "czf", str(archive), "-C", str(payload), "gitleaks"], check=True
        )
        return bin_dir, install_dir, archive

    def _run(
        self, bin_dir: Path, install_dir: Path, archive: Path
    ) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        assert bash is not None, "bash is needed to run the recipe"
        return subprocess.run(
            [bash, str(recipe_path()), "--gitleaks"],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{install_dir}:{bin_dir}",
                "HOME": os.environ.get("HOME", str(bin_dir)),
                "GITLEAKS_INSTALL_DIR": str(install_dir),
                "STUB_ARCHIVE": str(archive),
                "STUB_CURL_LOG": str(bin_dir.parent / "curl.log"),
            },
        )

    def test_a_spoofed_version_does_not_skip_the_digest_check(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir, archive = self._host(stub_host)
        real = shutil.which("sha256sum")
        assert real is not None, "sha256sum is needed to verify the archive"
        (bin_dir / "sha256sum").symlink_to(real)

        result = self._run(bin_dir, install_dir, archive)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "does not match the SHA-256" in result.stderr
        assert (bin_dir.parent / "curl.log").read_text(encoding="utf-8").split() == [
            "https://github.com/gitleaks/gitleaks/releases/download/"
            f"v{PINNED_GITLEAKS}/gitleaks_{PINNED_GITLEAKS}_linux_x64.tar.gz"
        ]

    def test_the_verified_archive_replaces_a_binary_answering_the_pin(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir, archive = self._host(stub_host)
        # The digest gate is the one boundary stubbed here: this archive is not
        # the release, so a checksum that accepts it stands in for one that is.
        _write_stub(bin_dir, "sha256sum", "#!/bin/sh\ncat >/dev/null\nexit 0\n")

        result = self._run(bin_dir, install_dir, archive)

        assert result.returncode == 0, result.stdout + result.stderr
        assert (install_dir / "gitleaks").read_text(encoding="utf-8") == (
            "#!/bin/sh\necho from-the-archive\n"
        )


#: The uv release archive the recipe installs on an x86_64 host, and the
#: directory inside it that holds ``uv`` and ``uvx``.
UV_TARGET = "x86_64-unknown-linux-gnu"

#: A trivy that answers ``trivy --version`` with exactly the pin: the binary a
#: version check alone let stay on PATH (#3258).
SPOOFED_TRIVY = f"#!/bin/sh\necho 'Version: {PINNED_TRIVY}'\nexit 0\n"

#: A sha256sum that accepts any archive. The digest gate is the one boundary
#: stubbed where a test needs the install to go through: a fixture archive is
#: not the release, so a checksum that accepts it stands in for one that is.
ACCEPTING_SHA256SUM = "#!/bin/sh\ncat >/dev/null\nexit 0\n"


def _archive_step_host(stub_host: tuple[Path, Path]) -> tuple[Path, Path]:
    """The stub PATH one of the recipe's archive steps runs on, and its target.

    Real ``tar``, ``mktemp`` and friends, an x86_64 ``uname``, and a ``curl``
    that serves ``STUB_ARCHIVE`` and logs every URL it was asked for.
    """
    bin_dir, _ = stub_host
    for tool in ("bash", "cat", "chmod", "gzip", "mktemp", "rm", "tar"):
        real = shutil.which(tool)
        assert real is not None, f"{tool} is needed to run the recipe step"
        (bin_dir / tool).symlink_to(real)
    _write_stub(bin_dir, "uname", "#!/bin/sh\necho x86_64\n")
    _write_stub(bin_dir, "curl", CURL_STUB)
    install_dir = bin_dir.parent / "install"
    install_dir.mkdir()
    return bin_dir, install_dir


def _real_sha256sum(bin_dir: Path) -> None:
    real = shutil.which("sha256sum")
    assert real is not None, "sha256sum is needed to verify the archive"
    (bin_dir / "sha256sum").symlink_to(real)


def _tarball(root: Path, name: str, members: dict[str, str]) -> Path:
    """A gzipped tar at ``root/name`` holding *members* (path to script text)."""
    payload = root / f"{name}.payload"
    for member, text in members.items():
        (payload / member).parent.mkdir(parents=True, exist_ok=True)
        _write_stub((payload / member).parent, Path(member).name, text)
    archive = root / name
    subprocess.run(
        ["tar", "czf", str(archive), "-C", str(payload), *members], check=True
    )
    return archive


def _run_archive_step(
    step: str,
    bin_dir: Path,
    install_dir: Path,
    archive: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the recipe's ``--uv`` or ``--trivy`` step on the stub PATH."""
    bash = shutil.which("bash")
    assert bash is not None, "bash is needed to run the recipe"
    return subprocess.run(
        [bash, str(recipe_path()), step],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{install_dir}:{bin_dir}",
            "HOME": os.environ.get("HOME", str(bin_dir)),
            "KEW_PROVISION_BIN_DIR": str(install_dir),
            "STUB_ARCHIVE": str(archive),
            "STUB_CURL_LOG": str(bin_dir.parent / "curl.log"),
            **(extra_env or {}),
        },
    )


def _curl_urls(bin_dir: Path) -> list[str]:
    log = bin_dir.parent / "curl.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


class TestTheRecipeInstallsUvFromAPinnedArchive:
    """#2631 — uv came from the astral.sh installer script, run as root.

    Whatever that URL served on the day was the uv every verify job ran. The
    step now downloads the release archive ``trivy.pin`` pins and checks its
    SHA-256 before anything is extracted.
    """

    def _uv_archive(self, root: Path) -> Path:
        return _tarball(
            root,
            f"uv-{UV_TARGET}.tar.gz",
            {
                f"uv-{UV_TARGET}/uv": "#!/bin/sh\necho uv-from-the-archive\n",
                f"uv-{UV_TARGET}/uvx": "#!/bin/sh\necho uvx-from-the-archive\n",
            },
        )

    def test_uv_is_installed_from_a_digest_checked_archive_never_a_script(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir = _archive_step_host(stub_host)
        _real_sha256sum(bin_dir)
        archive = self._uv_archive(bin_dir.parent)

        result = _run_archive_step("--uv", bin_dir, install_dir, archive)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "does not match the SHA-256" in result.stderr
        assert not (install_dir / "uv").exists()
        assert not (install_dir / "uvx").exists()
        pins = read_pins()
        assert _curl_urls(bin_dir) == [
            "https://github.com/astral-sh/uv/releases/download/"
            f"{pins['UV_VERSION']}/uv-{UV_TARGET}.tar.gz"
        ]

    def test_the_verified_archive_puts_uv_and_uvx_on_path(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir = _archive_step_host(stub_host)
        _write_stub(bin_dir, "sha256sum", ACCEPTING_SHA256SUM)
        # A uv already on the host does not skip the step.
        _write_stub(install_dir, "uv", "#!/bin/sh\necho uv 0.13.5\n")
        archive = self._uv_archive(bin_dir.parent)

        result = _run_archive_step("--uv", bin_dir, install_dir, archive)

        assert result.returncode == 0, result.stdout + result.stderr
        assert (install_dir / "uv").read_text(encoding="utf-8") == (
            "#!/bin/sh\necho uv-from-the-archive\n"
        )
        assert (install_dir / "uvx").read_text(encoding="utf-8") == (
            "#!/bin/sh\necho uvx-from-the-archive\n"
        )
        assert os.access(install_dir / "uv", os.X_OK)

    def test_astrals_install_dir_variable_does_not_move_the_install(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        """``UV_INSTALL_DIR`` is the astral installer's own variable, which an
        operator's environment may set for other reasons; the recipe's binaries
        go where ``KEW_PROVISION_BIN_DIR`` says, and nowhere else."""
        bin_dir, install_dir = _archive_step_host(stub_host)
        _write_stub(bin_dir, "sha256sum", ACCEPTING_SHA256SUM)
        elsewhere = bin_dir.parent / "astral-install-dir"
        elsewhere.mkdir()
        archive = self._uv_archive(bin_dir.parent)

        result = _run_archive_step(
            "--uv",
            bin_dir,
            install_dir,
            archive,
            extra_env={"UV_INSTALL_DIR": str(elsewhere)},
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert (install_dir / "uv").exists()
        assert list(elsewhere.iterdir()) == []

    def test_both_linux_archives_are_pinned_by_digest(self) -> None:
        pins = read_pins()
        assert pins.get("UV_VERSION")
        for key in ("UV_SHA256_X86_64_LINUX", "UV_SHA256_AARCH64_LINUX"):
            assert re.fullmatch(r"[0-9a-f]{64}", pins.get(key, "")), (
                f"{key} must be a 64-hex digest in {pin_path()}"
            )


class TestTheRecipeIdentifiesTrivyByDigest:
    """#3258 — a trivy that printed the pin skipped the archive check.

    ``install_trivy`` returned early when ``trivy --version`` answered the
    pinned version, the shape #2867 removed from the gitleaks step. The step
    now installs the digest-verified archive on every run.
    """

    def _trivy_archive(self, root: Path) -> Path:
        return _tarball(
            root,
            "trivy.tar.gz",
            {"trivy": "#!/bin/sh\necho trivy-from-the-archive\n"},
        )

    def test_a_spoofed_trivy_version_does_not_skip_the_digest_check(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir = _archive_step_host(stub_host)
        _real_sha256sum(bin_dir)
        _write_stub(install_dir, "trivy", SPOOFED_TRIVY)

        result = _run_archive_step(
            "--trivy", bin_dir, install_dir, self._trivy_archive(bin_dir.parent)
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert "does not match the SHA-256" in result.stderr
        assert _curl_urls(bin_dir) == [
            "https://github.com/aquasecurity/trivy/releases/download/"
            f"v{PINNED_TRIVY}/trivy_{PINNED_TRIVY}_Linux-64bit.tar.gz"
        ]
        assert (install_dir / "trivy").read_text(encoding="utf-8") == SPOOFED_TRIVY

    def test_a_trivy_answering_the_pinned_version_is_still_replaced_from_the_verified_archive(  # noqa: E501
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, install_dir = _archive_step_host(stub_host)
        _write_stub(bin_dir, "sha256sum", ACCEPTING_SHA256SUM)
        _write_stub(install_dir, "trivy", SPOOFED_TRIVY)

        result = _run_archive_step(
            "--trivy", bin_dir, install_dir, self._trivy_archive(bin_dir.parent)
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert (install_dir / "trivy").read_text(encoding="utf-8") == (
            "#!/bin/sh\necho trivy-from-the-archive\n"
        )

    def test_every_platform_binary_is_pinned_by_digest(self) -> None:
        pins = read_pins()
        for platform in ("LINUX_64BIT", "LINUX_ARM64", "MACOS_64BIT", "MACOS_ARM64"):
            key = f"TRIVY_BINARY_SHA256_{platform}"
            assert re.fullmatch(r"[0-9a-f]{64}", pins.get(key, "")), (
                f"{key} must be a 64-hex digest in {pin_path()}"
            )


def _recipe_function_body(text: str, name: str) -> str:
    """The body of the recipe's shell function *name*."""
    body = text[text.index(f"{name}() {{") :]
    return body[: body.index("\n}\n")]


def _recipe_apt_packages(text: str) -> set[str]:
    """Every package the recipe's base step hands to ``apt_install``."""
    packages: set[str] = set()
    for line in _recipe_function_body(text, "install_base").splitlines():
        words = line.split()
        if words[:1] == ["apt_install"]:
            packages.update(words[1:])
    return packages


def _recipe_self_check_commands(text: str) -> set[str]:
    """Every word each ``TOOL_CHECKS`` entry's version command runs.

    Where the command is one of the recipe's own functions (``jq_version``),
    the words of that function's body count too: that is what it runs.
    """
    table = text[text.index("TOOL_CHECKS=(") :]
    table = table[: table.index("\n)\n")]
    entries = re.findall(r'^\s*"([^"]+)"$', table, flags=re.MULTILINE)
    words: set[str] = set()
    for entry in entries:
        command = entry.split(":", 2)[2].split()[0]
        words.add(command)
        if f"{command}() {{" in text:
            body = _recipe_function_body(text, command)
            words.update(re.findall(r"[\w.-]+", body))
    return words


def _install_steps(text: str) -> list[str]:
    """The steps ``install_all`` runs, in order."""
    return _recipe_function_body(text, "install_all").split()


def _literal_names(
    node: ast.expr | None, constants: dict[str, ast.expr]
) -> list[str] | None:
    """The strings *node* is, read off the source, or None when it is not that.

    A string literal, a tuple/list/set of them, or a module constant bound to
    either. Anything else — a call, an attribute, a fixture — cannot be read.
    """
    if isinstance(node, ast.Name) and node.id in constants:
        node = constants[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    names: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Name) and element.id in constants:
            element = constants[element.id]
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        names.append(element.value)
    return names


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level names bound exactly once, to the expression they hold."""
    bound: dict[str, list[ast.expr]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name):
            bound.setdefault(target.id, []).append(value)
    return {name: values[0] for name, values in bound.items() if len(values) == 1}


def _parametrized_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
    constants: dict[str, ast.expr],
) -> list[str] | None:
    """The values ``@pytest.mark.parametrize("<name>", ...)`` gives *name*."""
    for decorator in function.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize"
            and len(decorator.args) >= 2
        ):
            continue
        argnames = decorator.args[0]
        if isinstance(argnames, ast.Constant) and argnames.value == name:
            return _literal_names(decorator.args[1], constants)
    return None


def _loop_binding(node: ast.AST, name: str) -> ast.expr | None:
    """What a ``for`` or comprehension at *node* iterates *name* over, if it does."""
    if isinstance(node, ast.For):
        loops = [(node.target, node.iter)]
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        loops = [(each.target, each.iter) for each in node.generators]
    else:
        return None
    for target, iterable in loops:
        if isinstance(target, ast.Name) and target.id == name:
            return iterable
    return None


def _resolve_which_argument(
    argument: ast.expr,
    ancestors: list[ast.AST],
    constants: dict[str, ast.expr],
) -> list[str] | None:
    """Every name a ``which(<argument>)`` can be asked, or None when unreadable.

    Read outward from the call: the innermost loop binding the name, else the
    parametrization of the function taking it as a parameter, else a module
    constant. A name bound any other way inside a function is unreadable.
    """
    if not isinstance(argument, ast.Name):
        return _literal_names(argument, constants)
    name = argument.id
    for node in reversed(ancestors):
        iterable = _loop_binding(node, name)
        if iterable is not None:
            return _literal_names(iterable, constants)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            parameters = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ]
            if any(parameter.arg == name for parameter in parameters):
                if isinstance(node, ast.Lambda):
                    return None
                return _parametrized_names(node, name, constants)
            if any(
                isinstance(inner, ast.Name)
                and inner.id == name
                and isinstance(inner.ctx, ast.Store)
                for inner in ast.walk(node)
            ):
                return None
    return _literal_names(argument, constants)


def _which_lookups(
    tree: ast.Module, where: str
) -> tuple[dict[str, list[str]], list[str]]:
    """Every name *tree* asks ``which`` for, and every call it cannot read.

    Returns ``{tool: [where:line, ...]}`` and ``["where:line: source", ...]``.
    """
    constants = _module_constants(tree)
    found: dict[str, list[str]] = {}
    unreadable: list[str] = []

    def visit(node: ast.AST, ancestors: list[ast.AST]) -> None:
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            if name is None and isinstance(func, ast.Name):
                name = func.id
            if name == "which":
                argument = node.args[0] if node.args else None
                if argument is None:
                    argument = next(
                        (kw.value for kw in node.keywords if kw.arg == "cmd"), None
                    )
                tools = (
                    None
                    if argument is None
                    else _resolve_which_argument(argument, ancestors, constants)
                )
                if tools is None:
                    unreadable.append(f"{where}:{node.lineno}: {ast.unparse(node)}")
                for tool in tools or ():
                    found.setdefault(tool, []).append(f"{where}:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            visit(child, [*ancestors, node])

    visit(tree, [])
    return found, unreadable


def _scan_tests_for_which() -> tuple[dict[str, list[str]], list[str]]:
    tests_dir = REPO_ROOT / "kew" / "tests"
    found: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_found, module_unreadable = _which_lookups(
            tree, str(path.relative_to(tests_dir))
        )
        for tool, where in module_found.items():
            found.setdefault(tool, []).extend(where)
        unreadable.extend(module_unreadable)
    return found, unreadable


def _tools_tests_look_up() -> dict[str, list[str]]:
    """Every name a ``shutil.which(...)`` under tests/ asks for → where it is.

    Not only ``which("<literal>")``: a lookup inside a literal loop, a
    parametrized test, or one naming a module constant is resolved to every
    name it can be asked. ``which(interpreter)`` under a ``perl``/``ruby``
    parametrization is how Perl went unprovisioned (#2867).
    """
    return _scan_tests_for_which()[0]


class TestTheRecipeInstallsEveryToolATestRequires:
    """#2763 — a host from this recipe failed every verify under KEW_REQUIRE_TOOLS.

    The first merge verify run with ``KEW_REQUIRE_TOOLS=1`` refused two skips:
    no ``rg`` and no ``ruby`` on the host — and gitleaks, jq, lsof and ssh
    skips were waiting behind them. The list of what tests need is
    ``tests/support/required_tools.py``; this holds the recipe to it, both in
    what it installs and in what its self-check refuses a host without, and
    holds every tool a test looks up to that list.
    """

    pytestmark = pytest.mark.policy

    @pytest.mark.parametrize(
        "executable", sorted(t for t, pkg in REQUIRED_TOOLS.items() if pkg)
    )
    def test_the_recipe_installs_the_package(self, executable: str) -> None:
        text = recipe_path().read_text(encoding="utf-8")
        assert REQUIRED_TOOLS[executable] in _recipe_apt_packages(text)

    @pytest.mark.parametrize("executable", sorted(REQUIRED_TOOLS))
    def test_the_self_check_refuses_a_host_without_it(self, executable: str) -> None:
        text = recipe_path().read_text(encoding="utf-8")
        assert executable in _recipe_self_check_commands(text)

    def test_the_recipe_installs_the_gitleaks_verify_sh_pins(self) -> None:
        """verify.sh exits without gitleaks; an older one ignores the allowlist."""
        text = recipe_path().read_text(encoding="utf-8")
        assert f'GITLEAKS_VERSION="{PINNED_GITLEAKS}"' in text
        assert '"gitleaks:exact ${GITLEAKS_VERSION}:gitleaks version"' in text
        assert "install_gitleaks" in _install_steps(text)

    def test_the_issue_named_tools_are_required(self) -> None:
        assert {"rg": "ripgrep", "ruby": "ruby"}.items() <= REQUIRED_TOOLS.items()

    def test_every_tool_a_test_looks_up_is_one_a_host_has(self) -> None:
        """A new ``shutil.which`` skip cannot leave the recipe behind.

        The list alone could not catch a tool nobody added to it: gitleaks,
        jq, lsof and ssh skips sat outside it while the policy passed.
        """
        unaccounted = {
            tool: where
            for tool, where in _tools_tests_look_up().items()
            if tool not in REQUIRED_TOOLS and tool not in HOST_PROVIDED_TOOLS
        }
        assert not unaccounted, (
            "tests look these tools up, but tests/support/required_tools.py "
            "neither requires them of the verify-host recipe nor says where a "
            f"host gets them: {unaccounted}"
        )

    def test_every_tool_lookup_names_a_tool_the_scan_can_read(self) -> None:
        """#2867 — a lookup the scan cannot read is a tool it cannot account for.

        ``shutil.which(interpreter)`` under a ``perl``/``ruby`` parametrization
        was invisible to a literal-only scan, so the recipe never installed
        Perl. Name the tool where the scan can read it — a literal, a literal
        loop, a parametrization or a module constant.
        """
        _, unreadable = _scan_tests_for_which()
        assert not unreadable, (
            "these which() lookups name a tool this scan cannot resolve, so it "
            f"cannot hold it to the verify-host recipe: {unreadable}"
        )

    def test_the_scan_reads_loops_parametrizations_and_constants(self) -> None:
        source = textwrap.dedent(
            """
            import shutil
            import pytest

            _RUNTIME = "podman"
            _TOOLS = ("bash", _RUNTIME)
            _GATE = any(shutil.which(tool) is None for tool in _TOOLS)

            @pytest.mark.parametrize("interpreter", ("perl", "python"))
            def test_a(interpreter):
                shutil.which(interpreter)
                shutil.which(_RUNTIME)
                for name in ["ps", "lsof"]:
                    shutil.which(name)

            def test_b(tmp_path, fixture_tool):
                shutil.which(fixture_tool)
                computed = fixture_tool + "x"
                shutil.which(computed)
                shutil.which(f"{fixture_tool}")
                shutil.which(cmd="zsh")
            """
        )
        found, unreadable = _which_lookups(ast.parse(source), "m.py")
        assert sorted(found) == [
            "bash",
            "lsof",
            "perl",
            "podman",
            "ps",
            "python",
            "zsh",
        ]
        assert found["podman"] == ["m.py:7", "m.py:12"]
        assert [entry.split(": ", 1)[1] for entry in unreadable] == [
            "shutil.which(fixture_tool)",
            "shutil.which(computed)",
            "shutil.which(f'{fixture_tool}')",
        ]

    def test_the_two_lists_do_not_overlap(self) -> None:
        assert not REQUIRED_TOOLS.keys() & HOST_PROVIDED_TOOLS.keys()


class TestTheSelfCheckAnswersForTheJobsPath:
    """#2433 — a host passed provisioning and then failed every job with 127.

    The job died in 1.6 seconds with ``sh: 6: uv: not found``: a job arrives
    over ``gcloud compute ssh --command``, a non-interactive, non-login shell
    that sources no profile, so ``~/.local/bin`` was on nobody's PATH. The
    self-check had looked for uv on the PATH of whoever ran the recipe, which
    is how it could pass on a host where no job could run.
    """

    def test_a_tool_only_the_callers_path_can_reach_is_missing(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        login_only = bin_dir.parent / "login-only"
        login_only.mkdir()
        (bin_dir / "uv").rename(login_only / "uv")

        result = _run_self_check(
            bin_dir, browsers, PATH=f"{login_only}{os.pathsep}{bin_dir}"
        )

        assert result.returncode != 0
        assert "missing uv" in result.stderr
        assert "KEW_HOST_TOOL uv=" not in result.stdout

    def test_the_recipe_and_kew_agree_on_what_a_jobs_path_is(self) -> None:
        """One list, in two languages: a drift here is a host that lies.

        kew exports :data:`kew.hosts.environment.REMOTE_PATH` before every
        remote command and hands the recipe the expanded value; the recipe's
        default is for an operator running the file by hand. If the two ever
        disagreed, the self-check would answer for a PATH no job gets.
        """
        text = recipe_path().read_text(encoding="utf-8")
        assert f'KEW_JOB_PATH="${{KEW_JOB_PATH:-{REMOTE_PATH}}}"' in text

    def test_the_recipe_and_verify_agree_on_where_the_browser_is(self) -> None:
        """The recipe installs the headless shell where verify tells ui-check
        to look. The recipe's only other mention of the directory is in
        ``/etc/profile.d``, which a job's non-login shell never reads (#2630).
        """
        text = recipe_path().read_text(encoding="utf-8")
        assert (
            'PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-'
            f'{HOST_BROWSERS_PATH}}}"'
        ) in text
        assert f"PLAYWRIGHT_BROWSERS_PATH:-{HOST_BROWSERS_PATH}" in UI_CHECK_COMMAND

    def test_provisioning_hands_the_recipe_the_logins_own_job_path(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """Expanded by the host's shell: ``$HOME`` is the login's, not root's.

        ``sudo`` would otherwise point the self-check at root's home and
        ``secure_path``, and a tool in the SSH login's ``~/.local/bin`` — the
        one a job actually reaches — would be checked nowhere.
        """
        _write_state(fake_gcloud, job_output=_self_check_output(REPORTED_VERSIONS))

        provision_host(_gcp(tmp_path), record_dir=tmp_path / "records")

        commands = [
            call[call.index("--command") + 1]
            for call in _calls(fake_gcloud)
            if "--command" in call
        ]
        recipe_runs = [
            command
            for command in commands
            if "provision.sh" in command and "bash" in command
        ]
        assert recipe_runs, commands
        assert all(
            command.startswith(remote_path_prefix())
            and 'env KEW_JOB_PATH="$PATH"' in command
            for command in recipe_runs
        ), recipe_runs


class TestProvisionAGcpHost:
    """AC2 — the recipe is copied, run, and its versions recorded."""

    def test_it_copies_and_runs_the_recipe_and_records_the_versions(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_output=_self_check_output(REPORTED_VERSIONS))
        provider = _gcp(tmp_path)
        records = tmp_path / "records"

        report = provision_host(provider, record_dir=records)

        assert report.ok and report.exit_code == 0
        assert report.host == "gcp"
        assert report.recipe == DEFAULT_RECIPE
        assert dict(report.versions) == REPORTED_VERSIONS
        assert report.seconds >= 0.0

        calls = _calls(fake_gcloud)
        copies = [call for call in calls if call[:2] == ["compute", "scp"]]
        # The recipe, and the pin file it reads its scanner version out of
        # instead of carrying a literal (#2462). Both, or the recipe refuses to
        # install an unpinned scanner.
        assert len(copies) == 2
        assert copies[0][2] == str(recipe_path())
        assert copies[0][3].endswith(f"{REMOTE_WORKSPACE}/provision.sh")
        assert copies[1][2] == str(pin_path())
        assert copies[1][3].endswith(f"{REMOTE_WORKSPACE}/{PIN_FILE}"), (
            "the pin must land beside the recipe, under the name the recipe "
            "looks for next to itself"
        )
        commands = [
            call[call.index("--command") + 1] for call in calls if "--command" in call
        ]
        assert any(
            f'sudo -n env KEW_JOB_PATH="$PATH" bash {REMOTE_WORKSPACE}/provision.sh'
            in command
            for command in commands
        ), commands
        # Through N1: the host is brought up (its pending stop cancelled) and
        # released (its idle stop armed) exactly as a job does it.
        assert any("shutdown -c" in command for command in commands)
        assert any("systemd-run" in command for command in commands)

    def test_the_record_names_the_recipe_that_built_the_host(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_output=_self_check_output(REPORTED_VERSIONS))
        provider = _gcp(tmp_path)
        records = tmp_path / "records"

        report = provision_host(provider, record_dir=records)

        written = record_path(records, provider.lock_key)
        assert written.is_file()
        record = json.loads(written.read_text(encoding="utf-8"))
        assert record["host"] == "gcp"
        assert record["lock_key"] == provider.lock_key
        assert record["recipe"] == DEFAULT_RECIPE
        assert record["recipe_sha256"] == report.recipe_sha256
        assert record["versions"] == REPORTED_VERSIONS
        assert record["provisioned_at"].endswith("+00:00")
        assert read_record(records, provider.lock_key) == record

    def test_a_failed_recipe_leaves_no_record(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        partial = dict(REPORTED_VERSIONS)
        partial.pop("docker")
        _write_state(
            fake_gcloud,
            job_output=_self_check_output(partial),
            job_exit=1,
            job_stderr="kew host self-check FAILED: missing docker\n",
        )
        provider = _gcp(tmp_path)
        records = tmp_path / "records"

        report = provision_host(provider, record_dir=records)

        assert not report.ok and report.exit_code == 1
        assert "docker" not in report.versions
        assert "missing docker" in report.log
        assert read_record(records, provider.lock_key) is None

    def test_a_host_whose_scanner_is_off_the_pin_is_never_recorded(
        self, tmp_path: Path, fake_gcloud: Path, stub_host: tuple[Path, Path]
    ) -> None:
        """The refusal kew records is the recipe's own, run for real (#2462).

        The self-check runs here against a stub host whose trivy is a different
        version, and exactly what it printed — output, stderr and exit code — is
        what the fake host answers with. So this asserts the chain: a scanner off
        the pin fails the recipe, and a failed recipe leaves no record claiming
        the host was built from it.
        """
        bin_dir, browsers = stub_host
        _write_stub(bin_dir, "trivy", _version_stub("Version: 0.75.0"))
        refusal = _run_self_check(bin_dir, browsers)
        assert refusal.returncode != 0, refusal.stdout
        _write_state(
            fake_gcloud,
            job_output=refusal.stdout,
            job_exit=refusal.returncode,
            job_stderr=refusal.stderr,
        )
        provider = _gcp(tmp_path)
        records = tmp_path / "records"

        report = provision_host(provider, record_dir=records)

        assert not report.ok
        assert "trivy" not in report.versions
        assert "trivy" in report.log
        assert PINNED_TRIVY in report.log
        assert read_record(records, provider.lock_key) is None

    def test_a_run_with_no_exit_marker_is_never_read_as_provisioned(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, swallow_marker=True)
        provider = _gcp(tmp_path)
        with pytest.raises(HostError, match="no exit marker"):
            provision_host(provider, record_dir=tmp_path / "records")
        assert read_record(tmp_path / "records", provider.lock_key) is None

    def test_a_failed_bring_up_still_arms_the_idle_stop(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        # The instance is TERMINATED, so bringing it up *starts* it, and then
        # cancelling its pending shutdown fails — sshd on a just-started
        # instance refuses connections for a while. Leaving without releasing
        # would be a running, billing VM with no idle stop armed, which is
        # exactly what docs/hosts.md promises cannot happen.
        _write_state(
            fake_gcloud,
            statuses=["TERMINATED"],
            cancel_exit=255,
            cancel_stderr="ssh: connect to host port 22: Connection refused\n",
        )
        provider = _gcp(tmp_path)
        records = tmp_path / "records"

        with pytest.raises(HostError, match="cannot be reached over ssh"):
            provision_host(provider, record_dir=records)

        assert _read_state(fake_gcloud).get("started") == 1
        commands = [
            call[call.index("--command") + 1]
            for call in _calls(fake_gcloud)
            if "--command" in call
        ]
        assert any("systemd-run" in command for command in commands), commands
        assert read_record(records, provider.lock_key) is None

    def test_the_local_host_is_refused_by_name(self, tmp_path: Path) -> None:
        local = LocalProvider(lock_dir=tmp_path / "locks")
        with pytest.raises(HostError, match="does not install system packages"):
            provision_host(local, record_dir=tmp_path / "records")

    def test_an_unknown_host_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(HostError, match="unknown host"):
            provider_named(_config(tmp_path), "aws")

    def test_a_host_that_answers_with_crlf_still_reports_its_versions(self) -> None:
        log = "KEW_HOST_TOOL docker=27.3.1\r\nKEW_HOST_TOOL node=22.9.0\r\n"
        assert parse_tool_versions(log) == {"docker": "27.3.1", "node": "22.9.0"}

    def test_versions_reported_by_the_host_are_data_not_commands(self) -> None:
        # Whatever a host prints, kew reads tool lines off it and nothing else.
        log = (
            "KEW_HOST_TOOL docker=27.3.1\n"
            "rm -rf /; echo KEW_HOST_TOOL evil=$(whoami)\n"
            "KEW_HOST_TOOL node=22.9.0\n"
        )
        assert parse_tool_versions(log) == {"docker": "27.3.1", "node": "22.9.0"}


class TestProvisionCommand:
    """AC2 — `kew hosts provision` drives it, and says what it recorded."""

    @patch("kew.cli.load_config")
    def test_it_provisions_the_gcp_host_and_prints_the_versions(
        self, load_config: object, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        config = _config(tmp_path)
        load_config.return_value = config  # type: ignore[attr-defined]
        _write_state(fake_gcloud, job_output=_self_check_output(REPORTED_VERSIONS))

        result = runner.invoke(app, ["hosts", "provision", "gcp"])

        assert result.exit_code == 0, result.output
        assert "docker 27.3.1" in result.output
        assert "playwright-core 1.63.0" in result.output
        records = host_lock_dir()
        record = read_record(records, "gcp-proj-us-central1-a-verify-1")
        assert record is not None
        assert record["versions"] == REPORTED_VERSIONS
        assert f"trivy {PINNED_TRIVY}" in result.output
        # The recipe and the pin file it reads its scanner version from (#2462).
        assert _read_state(fake_gcloud).get("copies") == 2

    def test_a_dead_holders_lease_is_taken_over_on_the_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gcloud: Path
    ) -> None:
        """#2874: provisioning takes a lease over only with it on the audit trail."""
        from kew.run_store import SqliteRunStore

        (tmp_path / "kew.toml").write_text(
            "[logging]\n"
            f"dir = {str(tmp_path / 'logs')!r}\n"
            "[hosts.gcp]\n"
            'project = "proj"\n'
            'zone = "us-central1-a"\n'
            'instance = "verify-1"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        holder = dead_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_output=_self_check_output(REPORTED_VERSIONS),
        )

        result = runner.invoke(app, ["hosts", "provision", "gcp"])

        assert result.exit_code == 0, result.output
        assert _read_state(fake_gcloud)["taken_over"] == 1
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            (taken,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)
        assert (taken.payload or {})["holder"] == holder

    @patch("kew.cli.load_config")
    def test_a_failing_recipe_exits_two_and_records_nothing(
        self, load_config: object, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        config = _config(tmp_path)
        load_config.return_value = config  # type: ignore[attr-defined]
        _write_state(
            fake_gcloud,
            job_output=_self_check_output({"git": "2.43.0"}),
            job_exit=1,
            job_stderr="kew host self-check FAILED: missing docker\n",
        )

        result = runner.invoke(app, ["hosts", "provision", "gcp"])

        assert result.exit_code == 2, result.output
        assert "missing docker" in result.output
        records = host_lock_dir()
        assert read_record(records, "gcp-proj-us-central1-a-verify-1") is None

    @patch("kew.cli.load_config")
    def test_the_local_host_is_refused_with_a_reason(
        self, load_config: object, tmp_path: Path
    ) -> None:
        load_config.return_value = DispatchConfig(  # type: ignore[attr-defined]
            logging=LoggingConfig(dir=str(tmp_path / "logs"))
        )

        result = runner.invoke(app, ["hosts", "provision", "local"])

        assert result.exit_code == 2, result.output
        assert "does not install system packages" in result.output

    @patch("kew.cli.load_config")
    def test_an_unshipped_recipe_is_refused_before_the_host_is_touched(
        self, load_config: object, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        load_config.return_value = _config(tmp_path)  # type: ignore[attr-defined]

        result = runner.invoke(
            app, ["hosts", "provision", "gcp", "--recipe", "centos-7.sh"]
        )

        assert result.exit_code == 2, result.output
        assert _calls(fake_gcloud) == []


def _run_recipe_step(
    step: str, bin_dir: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Run one of the recipe's own steps with *bin_dir* as the whole PATH."""
    bash = shutil.which("bash")
    assert bash is not None, "bash is needed to run the recipe"
    return subprocess.run(
        [bash, str(recipe_path()), step],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": str(bin_dir),
            "KEW_JOB_PATH": str(bin_dir),
            "HOME": os.environ.get("HOME", str(bin_dir)),
            **environment,
        },
    )


def _logging_stub(log: Path, name: str, then: str = "") -> str:
    """A stub that appends ``name args`` to *log*, then runs *then*."""
    return f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >>"{log}"\n{then}\n'


def _ci_python_pin() -> str:
    """The Python CI's pytest matrix pins, read out of ci.yml."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    pins = re.findall(r'python-version:\s*\["([0-9.]+)"\]', workflow)
    assert len(set(pins)) == 1, pins
    return pins[0]


class TestTheRecipeNeedsNoHandFixOnAFreshGcpHost:
    """#3238 — four fixes the operator made by hand on a fresh Ubuntu host.

    logind removed the shipped bundle under ``/dev/shm`` between ssh sessions,
    the host had no Python 3.13 for the suite, and the self-check knew only
    playwright's old headless-shell layout.
    """

    def test_the_recipe_keeps_a_logins_ipc_between_sessions(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, _ = stub_host
        for tool in ("mkdir",):
            real = shutil.which(tool)
            assert real is not None, f"{tool} is needed to run the recipe step"
            (bin_dir / tool).symlink_to(real)
        log = bin_dir.parent / "calls.log"
        _write_stub(bin_dir, "systemctl", _logging_stub(log, "systemctl"))
        conf_dir = bin_dir.parent / "logind.conf.d"

        result = _run_recipe_step(
            "--keep-ipc", bin_dir, KEW_LOGIND_CONF_DIR=str(conf_dir)
        )

        assert result.returncode == 0, result.stdout + result.stderr
        drop_in = (conf_dir / "kew-keep-ipc.conf").read_text(encoding="utf-8")
        assert drop_in.splitlines() == ["[Login]", "RemoveIPC=no"]
        assert log.read_text(encoding="utf-8").splitlines() == [
            "systemctl try-reload-or-restart systemd-logind"
        ]
        assert "keep_ipc" in _install_steps(recipe_path().read_text(encoding="utf-8"))

    def test_the_recipe_installs_and_pins_the_tested_python_for_the_login(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        """Run as root through sudo, the recipe installs CI's Python and pins
        it for the login that ran it — the login every job runs as."""
        bin_dir, _ = stub_host
        log = bin_dir.parent / "calls.log"
        _write_stub(bin_dir, "id", "#!/bin/sh\necho root\n")
        # sudo -u <login> -H <command...>: logged, then the command runs.
        _write_stub(bin_dir, "sudo", _logging_stub(log, "sudo", 'shift 3\nexec "$@"'))
        _write_stub(bin_dir, "uv", _logging_stub(log, "uv"))

        result = _run_recipe_step("--python", bin_dir, SUDO_USER="kewop")

        assert result.returncode == 0, result.stdout + result.stderr
        tested = _ci_python_pin()
        calls = log.read_text(encoding="utf-8").splitlines()
        job_path = f"env PATH={bin_dir}"
        assert calls == [
            f"sudo -u kewop -H {job_path} uv python install {tested}",
            f"uv python install {tested}",
            f"sudo -u kewop -H {job_path} uv python pin --global {tested}",
            f"uv python pin --global {tested}",
        ]
        steps = _install_steps(recipe_path().read_text(encoding="utf-8"))
        assert steps.index("install_python") > steps.index("install_uv")

    def test_the_self_check_finds_the_current_headless_shell_layout(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        bin_dir, browsers = stub_host
        build = browsers / f"chromium_headless_shell-{STUB_BROWSER_BUILD}"
        shutil.rmtree(build / "chrome-linux")
        shell = build / "chrome-headless-shell-linux64" / "chrome-headless-shell"
        shell.parent.mkdir(parents=True)
        shell.write_text("", encoding="utf-8")
        shell.chmod(0o755)

        result = _run_self_check(bin_dir, browsers)

        assert result.returncode == 0, result.stderr
        assert (
            f"KEW_HOST_TOOL chromium-headless-shell={STUB_BROWSER_BUILD}"
            in result.stdout.splitlines()
        )

    @pytest.mark.parametrize("reported", ["3.12.3", "3.130.0"])
    def test_the_self_check_refuses_a_login_whose_python_is_not_the_tested_one(
        self, stub_host: tuple[Path, Path], reported: str
    ) -> None:
        """Ubuntu's own 3.12 — or anything else the pin does not name — is a
        host whose jobs sync on a Python CI does not test."""
        result = _run_self_check(*stub_host, STUB_JOB_PYTHON=reported)

        assert result.returncode != 0, result.stdout
        assert "wrong version python" in result.stderr
        assert f"minor {_ci_python_pin()}" in result.stderr
        assert "python" not in parse_tool_versions(result.stdout)

    def test_the_self_check_asks_uv_for_the_python_of_the_login_jobs_run_as(
        self, stub_host: tuple[Path, Path]
    ) -> None:
        """Run as root through sudo, the self-check asks the login's uv, which
        holds the pin, not root's."""
        bin_dir, browsers = stub_host
        log = bin_dir.parent / "calls.log"
        _write_stub(bin_dir, "id", "#!/bin/sh\necho root\n")
        _write_stub(bin_dir, "sudo", _logging_stub(log, "sudo", 'shift 3\nexec "$@"'))

        result = _run_self_check(bin_dir, browsers, SUDO_USER="kewop")

        assert result.returncode == 0, result.stdout + result.stderr
        assert parse_tool_versions(result.stdout)["python"] == STUB_JOB_PYTHON
        assert log.read_text(encoding="utf-8").splitlines() == [
            f"sudo -u kewop -H env PATH={bin_dir} uv python find --show-version"
        ]

    @pytest.mark.parametrize(
        "drop_in", [None, "[Login]\nRemoveIPC=yes\n"], ids=["absent", "yes"]
    )
    def test_the_self_check_refuses_a_host_whose_logind_removes_ipc(
        self, stub_host: tuple[Path, Path], drop_in: str | None
    ) -> None:
        """A host without keep_ipc's drop-in loses the shipped bundle between
        a job's ssh sessions."""
        bin_dir, browsers = stub_host
        conf = _logind_conf_dir(bin_dir) / "kew-keep-ipc.conf"
        if drop_in is None:
            conf.unlink()
        else:
            conf.write_text(drop_in, encoding="utf-8")

        result = _run_self_check(bin_dir, browsers)

        assert result.returncode != 0, result.stdout
        assert "missing logind-keep-ipc" in result.stderr
        assert "logind-keep-ipc" not in parse_tool_versions(result.stdout)


def test_the_docs_mark_gcp_provisioning_and_verify_experimental() -> None:
    """Each page that documents the GCP host commands says they are
    experimental, in a sentence that names the command."""
    pages = {
        "docs/hosts.md": ("kew hosts provision gcp", "kew verify --host gcp"),
        "docs/cli/hosts.md": ("kew hosts provision gcp",),
        "docs/cli/verify.md": ("kew verify --host gcp", "--shards"),
    }
    for page, commands in pages.items():
        text = (REPO_ROOT / page).read_text(encoding="utf-8")
        paragraphs = [" ".join(block.split()) for block in text.split("\n\n")]
        sentences = [
            sentence
            for paragraph in paragraphs
            for sentence in paragraph.split(". ")
            if "experimental" in sentence.lower()
        ]
        for command in commands:
            assert any(command in sentence for sentence in sentences), (
                page,
                command,
            )
