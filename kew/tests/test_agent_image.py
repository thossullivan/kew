"""Static shape tests for docker/agent.Dockerfile.

No live Docker daemon is required for the shape tests. The live build is
gated behind _REQUIRES_DOCKER, matching the _REQUIRES_BASH/_REQUIRES_JQ
pattern in tests/test_security.py.

The live build reuses an image already built from the current ``docker/``
inputs rather than rebuilding it — see _resolve_agent_image.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple, cast

import pytest


def _docker_available() -> bool:
    """True only if the docker CLI is present AND its daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_REQUIRES_DOCKER = pytest.mark.skipif(
    not _docker_available(), reason="docker daemon not reachable"
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "agent.Dockerfile"

# ARG/ENV names that would indicate baked credentials
_CREDENTIAL_NAME_RE = re.compile(
    r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL",
    re.IGNORECASE,
)


def _join_continuations(text: str) -> str:
    """Collapse backslash line-continuations so one logical instruction
    (e.g. a multi-line ``apt-get install ... \\``) is searchable on one line."""
    return re.sub(r"\\\n\s*", " ", text)


_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: The two paths the live-build class can take, as they appear in its output.
_SOURCE_REUSED = "reuse"
_SOURCE_BUILT = "build"


class _AgentImage(NamedTuple):
    """The image the live-build class ran against, and where it came from."""

    id: str
    source: str
    tag: str


def _agent_image_pin_tag(repo_root: Path = _REPO_ROOT) -> str:
    """The pin tag the inputs under ``docker/`` name: ``kew-agent:docker-<12>``.

    This is the tag ``kew sandbox build`` produces and kew.toml pins, and the
    digest covers every file under ``docker/`` — content, path and executable
    bit — so one tag means one set of build inputs.
    """
    from kew.sandbox import agent_image_inputs_digest

    return f"kew-agent:docker-{agent_image_inputs_digest(repo_root)[:12]}"


def _local_image_id(reference: str) -> str | None:
    """The local image id for ``reference``, or None if the runtime has none.

    Never pulls: a reference the daemon does not already hold is absent, which
    is the same rule ``docker run --pull=never`` applies to the dispatch path.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    image_id = result.stdout.strip()
    return image_id if _IMAGE_ID_RE.fullmatch(image_id) else None


def _build_agent_image(iidfile: Path) -> str:
    """Build the agent image from the repository's ``docker/`` inputs."""
    result = subprocess.run(
        [
            "docker",
            "build",
            "--no-cache",
            "-f",
            str(_DOCKERFILE),
            "--iidfile",
            str(iidfile),
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"docker build failed:\n{result.stderr}"
    image_id = iidfile.read_text().strip()
    assert _IMAGE_ID_RE.fullmatch(image_id)
    return image_id


def _exec_dir(tmp_path: Path) -> Path:
    """A temp directory a shim can actually run from, or skip the test.

    kew's own agent container mounts the temp root ``noexec``, so a PATH shim
    written under ``tmp_path`` is unrunnable when the suite runs inside a
    dispatched agent. CI and a developer checkout run it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "probe"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    try:
        probed = subprocess.run([str(probe)], capture_output=True, timeout=10)
        runnable = probed.returncode == 0
    except OSError:
        runnable = False
    if not runnable:
        pytest.skip(f"{tmp_path} is mounted noexec; a PATH shim cannot run there")
    probe.unlink()
    return bin_dir


def _announce(config: pytest.Config, message: str) -> None:
    """Write one line above pytest's capture, so a passing run shows it.

    Captured stdout is reported for failures only, and which path the live
    build took is exactly what a green run has to say.
    """
    capture = config.pluginmanager.getplugin("capturemanager")
    reporter = config.pluginmanager.getplugin("terminalreporter")
    suspend = getattr(capture, "global_and_fixture_disabled", None)
    if reporter is None or suspend is None:  # -p no:terminal / -p no:capture
        print(message)
        return
    with suspend():
        reporter.write_line(message)


def _resolve_agent_image(announce: Callable[[str], None], iidfile: Path) -> _AgentImage:
    """Reuse the image built from the current ``docker/`` inputs, or build it.

    The tag pins a digest of every file under ``docker/``, so an image present
    under it was built from exactly the inputs these tests assert against. A
    changed Dockerfile or entrypoint names a tag the daemon does not hold, and
    the live build runs as before — reuse cannot mask a changed input.
    """
    tag = _agent_image_pin_tag()
    present = _local_image_id(tag)
    if present is not None:
        announce(
            f"agent image: {_SOURCE_REUSED} {tag} "
            "— present locally, built from these docker/ inputs"
        )
        return _AgentImage(id=present, source=_SOURCE_REUSED, tag=tag)
    announce(
        f"agent image: {_SOURCE_BUILT} {tag} "
        "— absent locally, building from docker/ with no cache"
    )
    return _AgentImage(id=_build_agent_image(iidfile), source=_SOURCE_BUILT, tag=tag)


class TestAgentDockerfileShape:
    """Parse docker/agent.Dockerfile and assert structural security properties."""

    @pytest.fixture()
    def content(self) -> str:
        assert _DOCKERFILE.exists(), f"agent.Dockerfile not found: {_DOCKERFILE}"
        return _join_continuations(_DOCKERFILE.read_text())

    def test_nonroot_user_present(self, content: str) -> None:
        users = re.findall(r"^\s*USER\s+(\S+)", content, re.MULTILINE)
        assert users, "No USER directive found in agent.Dockerfile"
        for user in users:
            name = user.split(":", 1)[0]  # user portion before an optional :group
            assert "$" not in name, (
                f"USER must resolve to a literal non-root user, got: {user!r}"
            )
            assert name not in ("root", "0"), (
                f"USER directive must not be root, got: {user!r}"
            )

    def test_base_image_pinned_by_digest(self, content: str) -> None:
        stage_names = {
            m.lower() for m in re.findall(r"\bAS\s+(\S+)", content, re.IGNORECASE)
        }
        froms = re.findall(r"^\s*FROM\s+(\S+)", content, re.MULTILINE | re.IGNORECASE)
        assert froms, "No FROM directive found in agent.Dockerfile"
        # Every external base (not `scratch`, not a reference to a prior stage)
        # must be pinned by a full digest.
        external = [
            f for f in froms if f.lower() != "scratch" and f.lower() not in stage_names
        ]
        assert external, "No external base image FROM found"
        for ref in external:
            assert re.search(r"@sha256:[0-9a-f]{64}$", ref), (
                f"FROM image must be pinned by a full 64-char digest: {ref!r}"
            )

    def test_no_credential_in_arg_or_env(self, content: str) -> None:
        names: list[str] = []
        for match in re.finditer(r"^\s*(?:ARG|ENV)\s+(.+)$", content, re.MULTILINE):
            body = match.group(1)
            if "=" in body:
                # `KEY=val [KEY2=val2 ...]` — one or more assignments per line
                for token in body.split():
                    if "=" in token:
                        names.append(token.split("=", 1)[0])
            else:
                # legacy `ENV KEY value` form — the first token is the name
                names.append(body.split()[0])
        for name in names:
            assert not _CREDENTIAL_NAME_RE.search(name), (
                f"agent.Dockerfile bakes credential-named ARG/ENV: {name!r}"
            )

    def test_git_installed(self, content: str) -> None:
        assert re.search(r"apt-get\s+install[^\n]*\bgit\b", content), (
            "git must be installed via apt-get in the agent image"
        )

    def test_claude_code_cli_installed(self, content: str) -> None:
        assert "@anthropic-ai/claude-code@2.1.295" in content
        assert "@anthropic-ai/claude-code-linux-arm64" in content
        assert "@anthropic-ai/claude-code-linux-x64" in content
        assert '"$native_package@2.1.295"' in content
        assert "npm install -g --ignore-scripts" in content
        installer = "node /usr/lib/node_modules/@anthropic-ai/claude-code/install.cjs"
        assert installer in content

    def test_claude_native_package_selection_fails_closed(self, content: str) -> None:
        assert "dpkg --print-architecture" in content
        assert re.search(r"arm64\).*claude-code-linux-arm64", content)
        assert re.search(r"amd64\).*claude-code-linux-x64", content)
        assert "unsupported Claude Code image architecture" in content
        selection = re.search(r'(case "\$arch" in .*? esac)', content)
        assert selection is not None
        refused = subprocess.run(
            ["/bin/sh", "-c", f"arch=riscv64; {selection.group(1)}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert refused.returncode != 0
        assert "unsupported Claude Code image architecture: riscv64" in refused.stderr

    def test_claude_startup_guard_runs_as_final_user(self, content: str) -> None:
        instructions = [
            (match.group(1), match.group(2).strip())
            for match in re.finditer(r"^\s*(USER|RUN)\s+(.+)$", content, re.MULTILINE)
        ]
        startup = [
            index
            for index, instruction in enumerate(instructions)
            if instruction == ("RUN", "claude --version")
        ]
        assert len(startup) == 1, "expected one active `RUN claude --version` guard"
        active_users = [
            body for kind, body in instructions[: startup[0]] if kind == "USER"
        ]
        assert active_users and active_users[-1] == "agent", (
            "Claude startup guard must run after the active `USER agent` instruction"
        )

    def test_procps_installed(self, content: str) -> None:
        # #762: `ps` is needed by test_fleet; procps is a trivial apt addition.
        assert re.search(r"apt-get\s+install[^\n]*\bprocps\b", content), (
            "procps (for `ps`) must be installed via apt-get in the agent image"
        )

    def test_jq_installed(self, content: str) -> None:
        # #797: the security-guard.sh hook parses its JSON payload with `jq`.
        # Without jq in the image the guard fails closed (refuses every call),
        # so jq must be present for the guard to inspect commands in-container.
        assert re.search(r"apt-get\s+install[^\n]*\bjq\b", content), (
            "jq must be installed via apt-get in the agent image "
            "(security-guard.sh parses its payload with jq)"
        )

    def test_worker_filter_tools_installed(self, content: str) -> None:
        assert re.search(r"apt-get\s+install[^\n]*\bnftables\b", content), (
            "nftables must be installed for the worker namespace filter"
        )
        assert re.search(r"apt-get\s+install[^\n]*\biptables\b", content), (
            "iptables must be installed for complete global firewall observation"
        )

    def test_uv_preinstalled(self, content: str) -> None:
        # #762: every Python-repo agent otherwise re-downloads uv into the
        # bind-mounted /work per run. Preinstall it in the image instead.
        # From the digest-checked release archive, never the astral.sh install
        # script (#2428).
        assert re.search(r"github\.com/astral-sh/uv/releases/download/", content), (
            "uv must be preinstalled in the agent image (uv release archive)"
        )

    def test_uv_lands_on_shared_path(self, content: str) -> None:
        # uv must install to a system dir on PATH (not the agent's HOME, which
        # is a throwaway tmpfs per run), so every dispatch finds it.
        assert re.search(r"tar -xzf \S+ -C /usr/local/bin\b", content), (
            "uv must install into /usr/local/bin so it is on PATH for the "
            "agent user without depending on the ephemeral tmpfs HOME"
        )


@_REQUIRES_DOCKER
# The build is a 45 s no-cache build on the shared Docker daemon; under
# parallel workers the buildkit session dropped mid-build (rpc Unavailable,
# EOF). Serial phase: tests/conftest.py. It runs only when the daemon holds no
# image for the current docker/ inputs (#2341) — _resolve_agent_image.
@pytest.mark.serial
class TestAgentDockerfileLiveBuild:
    """Live build test — requires a running Docker daemon."""

    @pytest.fixture(scope="class")
    def agent_image(
        self,
        request: pytest.FixtureRequest,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> _AgentImage:
        return _resolve_agent_image(
            functools.partial(_announce, request.config),
            tmp_path_factory.mktemp("agent-image") / "iid",
        )

    @pytest.fixture(scope="class")
    def image_id(self, agent_image: _AgentImage) -> str:
        return agent_image.id

    def test_the_image_the_class_uses_is_present_locally(
        self, agent_image: _AgentImage
    ) -> None:
        assert agent_image.source in {_SOURCE_REUSED, _SOURCE_BUILT}
        assert _IMAGE_ID_RE.fullmatch(agent_image.id)
        # Whichever path ran, the id names an image this daemon can run — on the
        # reuse path that is the pin tag resolving to a real local image.
        assert _local_image_id(agent_image.id) == agent_image.id

    def test_claude_starts_under_hardened_runtime_controls(self, image_id: str) -> None:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:size=1g",
                "--tmpfs",
                "/home/agent:exec,size=4g,uid=1001,gid=1001,mode=0700",
                "--pids-limit",
                "256",
                "--memory",
                "8g",
                "--memory-swap",
                "8g",
                "--ulimit",
                "nofile=4096:8192",
                "--ulimit",
                "fsize=1073741824",
                "--ulimit",
                "core=0",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-lc",
                "claude --version",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "2.1.295" in result.stdout


class _FakeCaptureManager:
    """pytest's capture plugin, as _announce uses it."""

    def __init__(self) -> None:
        self.suspended = 0

    @contextlib.contextmanager
    def global_and_fixture_disabled(self) -> Iterator[None]:
        self.suspended += 1
        yield


class _FakeReporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, line: str) -> None:
        self.lines.append(line)


class _FakeConfig:
    def __init__(self, plugins: dict[str, object]) -> None:
        self.pluginmanager = _FakePluginManager(plugins)


class _FakePluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def getplugin(self, name: str) -> object | None:
        return self._plugins.get(name)


class TestAgentImageReuse:
    """The reuse decision the live-build class makes — no daemon required."""

    def test_the_pin_tag_names_the_digest_of_the_docker_inputs(self) -> None:
        from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE, agent_image_inputs_digest

        tag = _agent_image_pin_tag()

        assert AGENT_IMAGE_PIN_TAG_RE.fullmatch(tag), tag
        assert tag == f"kew-agent:docker-{agent_image_inputs_digest(_REPO_ROOT)[:12]}"

    def test_editing_a_file_under_docker_changes_the_pin_tag(
        self, tmp_path: Path
    ) -> None:
        """Reuse cannot mask a changed input: the tag follows docker/ byte for byte.

        A copy of the real inputs names the real tag; one added comment in the
        Dockerfile names a different one, which no daemon holds, so the live
        build runs.
        """
        checkout = tmp_path / "checkout"
        shutil.copytree(_REPO_ROOT / "docker", checkout / "docker")
        assert _agent_image_pin_tag(checkout) == _agent_image_pin_tag()

        edited = checkout / "docker" / _DOCKERFILE.name
        edited.write_text(f"{edited.read_text()}# a changed input\n")

        assert _agent_image_pin_tag(checkout) != _agent_image_pin_tag()

    def test_a_present_image_is_reused_without_a_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        present = f"sha256:{'ab' * 32}"
        monkeypatch.setattr(f"{__name__}._local_image_id", lambda reference: present)
        monkeypatch.setattr(
            f"{__name__}._build_agent_image",
            lambda iidfile: pytest.fail("rebuilt an image that is already present"),
        )
        records: list[str] = []

        image = _resolve_agent_image(records.append, tmp_path / "iid")

        assert image == _AgentImage(
            id=present, source=_SOURCE_REUSED, tag=_agent_image_pin_tag()
        )
        assert len(records) == 1
        assert records[0].startswith(f"agent image: {_SOURCE_REUSED} {image.tag}")

    def test_an_absent_image_falls_back_to_the_live_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        built = f"sha256:{'cd' * 32}"
        iidfiles: list[Path] = []
        monkeypatch.setattr(f"{__name__}._local_image_id", lambda reference: None)
        monkeypatch.setattr(
            f"{__name__}._build_agent_image",
            lambda iidfile: (iidfiles.append(iidfile), built)[1],
        )
        records: list[str] = []

        image = _resolve_agent_image(records.append, tmp_path / "iid")

        assert image == _AgentImage(
            id=built, source=_SOURCE_BUILT, tag=_agent_image_pin_tag()
        )
        assert iidfiles == [tmp_path / "iid"]
        assert len(records) == 1
        assert records[0].startswith(f"agent image: {_SOURCE_BUILT} {image.tag}")

    def test_the_path_taken_is_written_above_pytest_capture(self) -> None:
        reporter = _FakeReporter()
        capture = _FakeCaptureManager()
        config = _FakeConfig({"terminalreporter": reporter, "capturemanager": capture})

        _announce(cast(pytest.Config, config), "agent image: reuse kew-agent:docker-0")

        assert reporter.lines == ["agent image: reuse kew-agent:docker-0"]
        assert capture.suspended == 1, "a captured line is shown on failures only"

    def test_the_path_taken_falls_back_to_stdout_without_those_plugins(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _announce(cast(pytest.Config, _FakeConfig({})), "agent image: build")

        assert "agent image: build" in capsys.readouterr().out

    @staticmethod
    def _docker_shim(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        """Put a fake ``docker`` first on PATH, so the probe runs for real."""
        bin_dir = _exec_dir(tmp_path)
        shim = bin_dir / "docker"
        shim.write_text(f"#!/bin/sh\n{body}\n")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir), prepend=":")

    def test_the_probe_reads_the_local_image_id_without_pulling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = tmp_path / "argv"
        image_id = f"sha256:{'ef' * 32}"
        self._docker_shim(
            tmp_path, monkeypatch, f'printf "%s\\n" "$@" > {argv}\necho {image_id}'
        )

        assert _local_image_id("kew-agent:docker-000000000000") == image_id
        assert argv.read_text().split() == [
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "kew-agent:docker-000000000000",
        ]

    def test_the_probe_reports_an_unknown_tag_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._docker_shim(tmp_path, monkeypatch, "echo 'No such image' >&2\nexit 1")

        assert _local_image_id("kew-agent:docker-000000000000") is None

    def test_the_probe_treats_an_unreadable_id_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail towards the build: an id this class cannot run is not an image."""
        self._docker_shim(tmp_path, monkeypatch, "echo '<no value>'")

        assert _local_image_id("kew-agent:docker-000000000000") is None


# --- The inputs label --------------------------------------------------------


@pytest.fixture()
def docker_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory a fake ``docker`` can run from.

    Under ``HOME`` when the pytest temporary filesystem is mounted ``noexec``,
    as it is inside kew's own agent container: the assertion is about kew, not
    about the sandbox's mount options. kew allowlists a runtime it resolved to
    an absolute path under that path, so the shim's entry is registered here
    and removed with the test rather than left in the process-wide allowlist.
    """
    from support.fake_gcloud import launchable

    from kew import subprocess_utils

    fallback: str | None = None
    if launchable(tmp_path):
        directory = tmp_path / "docker-bin"
        directory.mkdir()
    else:
        fallback = tempfile.mkdtemp(prefix="kew-docker-bin-", dir=Path.home())
        directory = Path(fallback)
    shim = str(directory / "docker")
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, shim, shim)
    try:
        yield directory
    finally:
        if fallback is not None:
            shutil.rmtree(fallback, ignore_errors=True)


def _fake_docker(docker_bin: Path, calls: Path, inspect_payload: Path) -> Path:
    """A ``docker`` that logs every argv and answers only ``image inspect``.

    ``build`` succeeds and does nothing; anything else — ``create``, ``run``,
    ``start`` — fails, so a launch that got past the label check cannot pass
    for one that was refused.
    """
    shim = docker_bin / "docker"
    shim.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        'if [ "$1" = image ] && [ "$2" = inspect ]; then\n'
        f"  cat '{inspect_payload}'\n"
        "  exit 0\n"
        "fi\n"
        '[ "$1" = build ] && exit 0\n'
        "exit 1\n"
    )
    shim.chmod(0o755)
    return shim


def _inspect_payload(path: Path, labels: dict[str, str] | None) -> Path:
    path.write_text(
        json.dumps([{"Id": f"sha256:{'c' * 64}", "Config": {"Labels": labels}}])
    )
    return path


class TestTheInputsLabel:
    """#3160 item 1 — a ``docker-<digest>`` tag is checked against the image.

    The tag is a name: ``docker tag`` points it at any image at all. Every
    image kew builds now carries the full inputs digest as a label, and a
    launch of a kew pin tag reads the label back and refuses one that is
    missing or names other inputs.
    """

    def test_a_built_image_carries_its_inputs_digest_label(
        self, tmp_path: Path, docker_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import subprocess_utils
        from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL
        from kew.sandbox import (
            agent_image_inputs_digest,
            build_agent_image,
            packaged_agent_image_context,
        )

        calls = tmp_path / "docker-calls.log"
        shim = _fake_docker(docker_bin, calls, _inspect_payload(tmp_path / "i", None))
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(shim))
        digest = agent_image_inputs_digest(packaged_agent_image_context())

        result = build_agent_image("docker", f"kew-agent:docker-{digest[:12]}")

        assert result.returncode == 0, result.stderr
        argv = calls.read_text().split()
        assert argv[0] == "build"
        label = argv[argv.index("--label") + 1]
        assert label == f"{AGENT_IMAGE_INPUTS_LABEL}={digest}"
        # The packaged inputs are the repository's, so the label a build
        # writes is the digest of the docker/ tree the pins are computed from.
        assert digest == agent_image_inputs_digest(_REPO_ROOT)

    @pytest.mark.parametrize(
        "labels",
        [
            pytest.param(None, id="no-labels"),
            pytest.param({"org.opencontainers.image.title": "x"}, id="label-missing"),
            pytest.param("other", id="label-names-other-inputs"),
        ],
    )
    def test_a_pinned_tag_whose_label_names_other_inputs_is_refused_before_launch(
        self,
        tmp_path: Path,
        docker_bin: Path,
        monkeypatch: pytest.MonkeyPatch,
        labels: dict[str, str] | str | None,
    ) -> None:
        from kew.config import DispatchConfig
        from kew.dispatch_sandbox import _resolve_launch_image_id
        from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL
        from kew.sandbox import AgentImageInputsMismatchError, detect_runtime

        pinned = "0123456789ab"
        if labels == "other":
            labels = {AGENT_IMAGE_INPUTS_LABEL: "f" * 64}
        assert not isinstance(labels, str)
        calls = tmp_path / "docker-calls.log"
        _fake_docker(docker_bin, calls, _inspect_payload(tmp_path / "i", labels))
        monkeypatch.setenv("PATH", str(docker_bin), prepend=os.pathsep)
        # The suite's conftest reports no runtime anywhere; this test puts the
        # real detection back, so the fake docker on PATH is the runtime found.
        monkeypatch.setattr(
            "kew.dispatch_sandbox.detect_runtime_cached", detect_runtime
        )
        config = DispatchConfig()
        config.sandbox.images = {"claude-code": f"kew-agent:docker-{pinned}"}

        # The resolution every contained dispatch and review launch runs before
        # a run row is written or a container created: it turns the configured
        # tag into the immutable ID the container is then launched by.
        with pytest.raises(AgentImageInputsMismatchError, match="kew sandbox build"):
            _resolve_launch_image_id(
                config, config.sandbox.images["claude-code"], runner="claude-code"
            )

        commands = [line.split()[0] for line in calls.read_text().splitlines()]
        assert commands == ["image"], (
            "the label must be read and the launch refused before any container "
            f"is created: {calls.read_text()}"
        )

    def test_a_pinned_tag_whose_label_matches_resolves_to_its_id(
        self, tmp_path: Path, docker_bin: Path
    ) -> None:
        from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL
        from kew.sandbox import require_image_id

        digest = "0123456789ab" + "d" * 52
        shim = _fake_docker(
            docker_bin,
            tmp_path / "docker-calls.log",
            _inspect_payload(tmp_path / "i", {AGENT_IMAGE_INPUTS_LABEL: digest}),
        )

        resolved = require_image_id(
            str(shim),
            "kew-agent:docker-0123456789ab",
            runner="claude-code",
            refusal="refused",
        )

        assert resolved == f"sha256:{'c' * 64}"

    def test_an_image_that_is_not_a_kew_pin_tag_is_left_alone(
        self, tmp_path: Path, docker_bin: Path
    ) -> None:
        from kew.sandbox import require_image_id

        shim = _fake_docker(
            docker_bin,
            tmp_path / "docker-calls.log",
            _inspect_payload(tmp_path / "i", None),
        )

        resolved = require_image_id(
            str(shim), "operator/agent:latest", runner="claude-code", refusal="refused"
        )

        assert resolved == f"sha256:{'c' * 64}"


_SCAN_DOCKER = """#!/bin/sh
# Records each build's argv, one argument per line, and reports an image ID
# derived from the tag; every other command succeeds and does nothing.
if [ "$1" = build ]; then
  log="$(dirname "$0")/builds.log"
  tag=
  iidfile=
  previous=
  for argument in "$@"; do
    printf '%s\\n' "$argument" >> "$log"
    [ "$previous" = "-t" ] && tag="$argument"
    [ "$previous" = "--iidfile" ] && iidfile="$argument"
    previous="$argument"
  done
  printf -- '--\\n' >> "$log"
  # cksum is POSIX and in /usr/bin on Linux and macOS alike; sha256sum is not
  # on macOS's /usr/bin. Eight hex of the tag's CRC, repeated to 64.
  crc="$(printf '%s' "$tag" | cksum | cut -d' ' -f1)"
  hex="$(printf '%08x' "$crc")"
  printf 'sha256:%s%s%s%s%s%s%s%s\\n' \\
    "$hex" "$hex" "$hex" "$hex" "$hex" "$hex" "$hex" "$hex" > "$iidfile"
fi
exit 0
"""


def _scan_trivy(version: str) -> str:
    """A trivy with a fresh database, a full inventory and no findings."""
    stamp = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    packages = ",".join("{}" for _ in range(60))
    inventory = json.dumps(
        {
            "Results": [
                {"Class": "os-pkgs", "Type": "debian", "Packages": []},
                {"Class": "lang-pkgs", "Type": "node-pkg", "Packages": []},
            ]
        }
    ).replace("[]", f"[{packages}]")
    return (
        "#!/bin/sh\n"
        f'[ "$1" = --version ] && echo "Version: {version}" && exit 0\n'
        f'[ "$1" = version ] && echo \'{{"VulnerabilityDB":'
        f'{{"UpdatedAt":"{stamp}"}}}}\' && exit 0\n'
        'for a in "$@"; do\n'
        f"  [ \"$a\" = --list-all-pkgs ] && echo '{inventory}' && exit 0\n"
        "done\n"
        "exit 0\n"
    )


def test_the_scan_gate_labels_every_agent_image_with_its_inputs_label(
    tmp_path: Path, docker_bin: Path
) -> None:
    """The gate's own builds carry the same inputs label ``kew sandbox build``
    writes, computed by the same module from the checkout being scanned."""
    from support.scan_checkout import pin_stub_trivy, synthetic_scan_checkout

    from kew.hosts import pinned_trivy_version
    from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL, agent_image_inputs_digest

    checkout = tmp_path / "checkout"
    script = synthetic_scan_checkout(checkout)
    (checkout / "docker").mkdir()
    (checkout / "docker" / "agent.Dockerfile").write_text("FROM scratch\n")
    for name, body in (
        ("docker", _SCAN_DOCKER),
        ("trivy", _scan_trivy(pinned_trivy_version())),
    ):
        (docker_bin / name).write_text(body)
        (docker_bin / name).chmod(0o755)
    pin_stub_trivy(checkout, docker_bin / "trivy")

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={
            "PATH": f"{docker_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(tmp_path),
        },
    )

    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    builds = [
        build.strip().splitlines()
        for build in (docker_bin / "builds.log").read_text().split("--\n")
        if build.strip()
    ]
    expected = f"{AGENT_IMAGE_INPUTS_LABEL}={agent_image_inputs_digest(checkout)}"
    assert len(builds) == 3, builds
    for argv in builds:
        assert argv[argv.index("--label") + 1] == expected, argv
