"""Every agent-workload container carries ``--pids-limit 1024``, once (#2927).

``--pids-limit`` is part of the sandbox containment: it is a fork bomb's
ceiling. Docker counts threads toward it, so 256 was a ceiling an ordinary
frontend run reached (``npm run ui-check`` beside the agent CLI and a dev
server) and the runner died with exit 134. The cap was raised to 1024 and stays
a HARD cap — these tests drive each launch path's production function, stop
it at the runtime boundary, and assert on the argv the runtime would have
received; they also pin that no config key or environment variable reaches the
value, and that the agent prompt claims the cap only where the runtime has it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kew.dispatch_core as dispatch_core
import kew.review_environment as review_environment
from kew.config import ConfigError, DispatchConfig, SandboxConfig, load_config
from kew.dispatch_sandbox import build_agent_container_argv, prepare_sidecar_worker
from kew.inference_transport import ResolvedInferenceTransport
from kew.prompt import build_prompt
from kew.review_frontend import prepare_frontend_mount
from kew.runtime_names import DOCKER_COMPATIBLE_RUNTIMES
from kew.subprocess_utils import TOOL_PATHS

IMAGE = "sha256:" + "a" * 64
MANIFEST = b'{"name": "kew-frontend", "private": true}\n'
LOCK = b'{"name": "kew-frontend", "lockfileVersion": 3, "packages": {}}\n'


def _pids_limits(argv: list[str]) -> list[str]:
    values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--pids-limit"]
    # The `--flag=value` spelling would be a second, unseen copy of the cap.
    assert not [arg for arg in argv if arg.startswith("--pids-limit=")]
    return values


def _direct_transport() -> ResolvedInferenceTransport:
    return ResolvedInferenceTransport(
        mode="direct", requested_model="sonnet", model="sonnet", config_sha256="d" * 64
    )


def _implementer_argv(config: DispatchConfig, checkout: Path) -> list[str]:
    return build_agent_container_argv(
        config,
        image=IMAGE,
        runner="claude-code",
        checkout_dir=checkout,
        slug="2927",
        model="sonnet",
        inference_transport=_direct_transport(),
    )


def _tracked_flow_argv(config: DispatchConfig, checkout: Path, kind: str) -> list[str]:
    """The argv ``run_tracked_agent`` launches for review, resolve and ci-fix.

    Those flows swap their execution step for a container through
    ``dispatch_core._contained_execution_for``. With no store there is no
    container to register, so the execution carries the built argv itself.
    """
    run = SimpleNamespace(
        contained_image="kew-agent:latest",
        resolved_image_id=IMAGE,
        transport=SimpleNamespace(resolved=_direct_transport()),
        config=config,
        runner=SimpleNamespace(name="claude-code"),
        item_id=f"{kind}-2927",
        issue_num=2927,
        effective_model="sonnet",
        store=None,
        run_id=f"run-{kind}",
        credential=None,
    )
    contained = dispatch_core._contained_execution_for(run, checkout)  # type: ignore[arg-type]
    assert contained is not None
    return list(contained.argv)


class _Launched(Exception):
    """Raised where the container runtime would have started a container."""


class _RuntimeBoundary:
    """``subprocess.Popen`` with the container runtime cut off.

    A runtime ``run``/``create`` is recorded and refused, so no container ever
    starts; any other runtime call (the daemon CPU probe) finds no binary, as
    on a host without docker. Every other process — git — really runs. The
    host reports ample free disk, so a crowded test machine's free-space floor
    cannot refuse the review-environment copy before its launch.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.launched: list[list[str]] = []
        self._popen = subprocess.Popen
        for runtime in DOCKER_COMPATIBLE_RUNTIMES:
            monkeypatch.setitem(TOOL_PATHS, runtime, f"/usr/bin/{runtime}")
        git = shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)
        monkeypatch.setattr(subprocess, "Popen", self._open)
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _p: SimpleNamespace(free=1 << 50)
        )

    def _open(self, args: Any, *rest: Any, **kwargs: Any) -> Any:
        if Path(args[0]).name not in DOCKER_COMPATIBLE_RUNTIMES:
            return self._popen(args, *rest, **kwargs)
        if args[1] not in ("run", "create"):
            raise FileNotFoundError(args[0])
        self.launched.append(list(args))
        raise _Launched(args[1])

    def only_launch(self, before: int) -> list[str]:
        new = self.launched[before:]
        assert len(new) == 1, new
        return new[0]


class _Supervisor:
    """The sidecar network supervisor: ``create_worker`` runs the runtime."""

    network_name = "kew-2927-internal"
    worker_name = "kew-2927-worker"
    owner = "0" * 32

    def __init__(self) -> None:
        self.created: list[list[str]] = []

    def worker_environment(self, name: str) -> dict[str, str]:
        return {name: "synthetic"}

    def create_worker(self, argv: list[str], **_: Any) -> str:
        self.created.append(list(argv))
        raise _Launched("create")


def _git(cwd: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, *args], cwd=cwd, check=True, capture_output=True)


def _frontend_repo(root: Path) -> Path:
    """A one-commit repo whose ``frontend`` carries a manifest and lockfile."""
    repo = root / "frontend-repo"
    (repo / "frontend").mkdir(parents=True)
    (repo / "frontend" / "package.json").write_bytes(MANIFEST)
    (repo / "frontend" / "package-lock.json").write_bytes(LOCK)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@t.t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "base")
    return repo


def _review_environment_install_argv(
    config: DispatchConfig, root: Path, boundary: _RuntimeBoundary
) -> list[str]:
    """``provision_review_environment``'s ``uv sync`` container."""
    checkout = root / "env" / "checkout"
    (checkout / "kew").mkdir(parents=True)
    (checkout / "kew" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (checkout / "kew" / "uv.lock").write_text("version = 1\n")
    before = len(boundary.launched)
    with pytest.raises(review_environment.ProvisioningUnconfirmed):
        review_environment.provision_review_environment(
            config, image=IMAGE, runner="claude-code", checkout_dir=checkout, slug="e"
        )
    return boundary.only_launch(before)


def _frontend_install_argv(
    config: DispatchConfig, root: Path, boundary: _RuntimeBoundary
) -> list[str]:
    """``prepare_frontend_mount``'s ``npm ci`` container, built on a cache miss."""
    before = len(boundary.launched)
    mounts = prepare_frontend_mount(
        config,
        image=IMAGE,
        runner="claude-code",
        checkout_dir=_frontend_repo(root / "fe"),
        slug="2927",
        lock_sha256=hashlib.sha256(LOCK).hexdigest(),
        base_ref="main",
        cache_root=root / "fe" / "cache",
    )
    assert mounts == ()  # the install never ran, so nothing is mounted
    return boundary.only_launch(before)


def _sidecar_worker_argv(config: DispatchConfig, checkout: Path) -> list[str]:
    """``prepare_sidecar_worker``'s sleeping worker, handed to its supervisor."""
    supervisor = _Supervisor()
    transport = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="sonnet",
        model="sonnet",
        config_sha256="d" * 64,
        endpoint="http://172.30.0.2:4000",
        sidecar=True,
        worker_credential_env_name="ANTHROPIC_API_KEY",
    )
    with pytest.raises(_Launched):
        prepare_sidecar_worker(
            config,
            supervisor=supervisor,
            image_id=IMAGE,
            runner="claude-code",
            checkout_dir=checkout,
            slug="2927",
            model="sonnet",
            inference_transport=transport,
        )
    (argv,) = supervisor.created
    return argv


def _launch_paths(
    config: DispatchConfig, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[str]]:
    checkout = root / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    boundary = _RuntimeBoundary(monkeypatch)
    return {
        "implementer": _implementer_argv(config, checkout),
        "reviewer": _tracked_flow_argv(config, checkout, "review"),
        "resolve": _tracked_flow_argv(config, checkout, "resolve"),
        "ci-fix": _tracked_flow_argv(config, checkout, "ci-fix"),
        "review-environment-install": _review_environment_install_argv(
            config, root, boundary
        ),
        "review-frontend-install": _frontend_install_argv(config, root, boundary),
        "sidecar-worker": _sidecar_worker_argv(config, checkout),
    }


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    return path


def test_every_agent_launch_path_carries_the_cap_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, argv in _launch_paths(DispatchConfig(), tmp_path, monkeypatch).items():
        assert _pids_limits(argv) == ["1024"], name


def test_the_lowest_operator_caps_leave_the_pids_cap_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lower-only memory/CPU seam (#2360) is not a route to this cap."""
    config = DispatchConfig(sandbox=SandboxConfig(max_memory="1g", max_cpus=1))
    for name, argv in _launch_paths(config, tmp_path, monkeypatch).items():
        assert _pids_limits(argv) == ["1024"], name


@pytest.mark.parametrize("value", ["4096", "-1", "0"])
def test_a_sandbox_pids_limit_key_does_not_change_the_argv(
    tmp_path: Path, checkout: Path, value: str
) -> None:
    """There is no knob: the key is refused at load, so no config carries it."""
    toml = tmp_path / "kew.toml"
    toml.write_text(f"[sandbox]\npids_limit = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as refused:
        load_config(toml, env={})
    assert refused.value.key == "[sandbox].pids_limit"
    with pytest.raises(ValueError, match="pids_limit"):
        SandboxConfig(pids_limit=int(value))  # type: ignore[call-arg]
    assert _pids_limits(_implementer_argv(DispatchConfig(), checkout)) == ["1024"]


@pytest.mark.parametrize(
    "name", ["KEW_SANDBOX_PIDS_LIMIT", "KEW_PIDS_LIMIT", "KEW_SANDBOX__PIDS_LIMIT"]
)
def test_no_environment_variable_reaches_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    toml = tmp_path / "kew.toml"
    toml.write_text("[sandbox]\n", encoding="utf-8")
    monkeypatch.setenv(name, "-1")
    config = load_config(toml, env={name: "-1"})
    for path, argv in _launch_paths(config, tmp_path / "run", monkeypatch).items():
        assert _pids_limits(argv) == ["1024"], path


def _container_environment(runtime: str) -> str:
    prompt = build_prompt(
        issue_num=2927,
        issue_title="t",
        issue_body="b",
        complexity="small",
        config=DispatchConfig(sandbox=SandboxConfig(runtime=runtime)),
        has_coderabbit=False,
        sandboxed=True,
        publication_style="host",
    )
    section = prompt.split("## Container Environment", 1)[1]
    return " ".join(section.split("\n## ", 1)[0].split())


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_a_docker_cli_prompt_states_the_cap(runtime: str) -> None:
    text = _container_environment(runtime)
    assert "hard process/thread cap (`--pids-limit 1024`)" in text
    assert "Stop any dev server you started before you run `npm run ui-check`" in text


def test_an_openshell_prompt_claims_no_cap_it_does_not_have() -> None:
    """OpenShell gets no docker argv, so ``--pids-limit`` is not enforced there."""
    text = _container_environment("openshell")
    assert "pids-limit" not in text
    assert "process/thread cap" not in text
    assert "Stop any dev server you started before you run `npm run ui-check`" in text
