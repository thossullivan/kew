"""``[sandbox] check_image``: the image kew's own checks run in (#3281).

The pre-review gate's static and ui-check halves and the resolver's checks run
in the check image when one is set, and in the runner image when not. The
container runtime is the boundary faked here: a stub runtime executable that
answers ``image inspect`` with the image IDs it holds, and the container launch
(``run_container_with_cpu_cap_retry``), which records the image and command of
every run and executes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from support.container_runtime import runtime_present
from support.image_resolution import inspect_config

from kew import sandbox
from kew.config import DispatchConfig, ResolveConfig, SandboxConfig
from kew.graph.driver.pre_review_gate import run_pre_review_gate
from kew.loops import verification
from kew.loops.verification import (
    CONTAINED_VERIFICATION_POSTURE,
    resolve_check_image_admission,
)
from kew.resolve_checks import ResolutionChecksFailed, run_resolution_checks
from kew.subprocess_utils import TOOL_PATHS

RUNNER_IMAGE = "kew-agent:docker-d148816b388f"
CHECK_IMAGE = "kew-check:0123456789ab"
RUNNER_ID = "sha256:" + "a" * 64
CHECK_ID = "sha256:" + "c" * 64
STATIC = "cd kew && uv run ruff check . && uv run mypy src/"
UI_CHECK = "npm run ui-check -- {modules}"
PYTHON_CHECK = "cd kew && uv run mypy src/"

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-check-image.sh"
#: What the build script reads, relative to the repository root.
TAG_INPUTS = (
    "scripts/check-image.Dockerfile",
    "kew/pyproject.toml",
    "kew/uv.lock",
    "frontend/package.json",
    "frontend/package-lock.json",
    "kew.toml",
)
#: The tool pin file the build script reads its semgrep and setuptools from.
TOOL_PINS = "kew/scripts/tool-pins.env"
#: What a head that changes the check image's inputs is told.
STALE_IMAGE_REASON = "the head changes the check image's inputs"
#: The files kew's own check image is built from (docs/configuration.md).
KEW_CHECK_IMAGE_INPUTS = [
    "kew/pyproject.toml",
    "kew/uv.lock",
    "frontend/package.json",
    "frontend/package-lock.json",
]
#: An image the stub runtime holds but cannot answer for (a daemon that is down).
UNANSWERED = "unanswered"


_STUB_RUNTIME = """\
import json, pathlib, sys
images = json.loads({images!r})
configs = json.loads({configs!r})
argv = sys.argv[1:]
with open(pathlib.Path(sys.argv[0]).with_name("calls.log"), "a") as log:
    log.write(" ".join(argv) + "\\n")
if argv[:2] == ["image", "inspect"] and argv[2:3] and argv[2] in images:
    if images[argv[2]] == UNANSWERED:
        sys.stderr.write("Cannot connect to the Docker daemon\\n")
        sys.exit(1)
    print(json.dumps([{{"Id": images[argv[2]], "Config": configs[argv[2]]}}]))
    sys.exit(0)
sys.stderr.write("Error: No such image: " + " ".join(argv[2:]) + "\\n")
sys.exit(1)
"""


class _Runtime:
    """A container runtime that runs nothing: every launch is kept as
    ``(image, command)`` and exits 0, or exits :attr:`exit_code` when
    :attr:`fails` says the mounted checkout is one the command fails on."""

    def __init__(self) -> None:
        self.launches: list[tuple[str, str]] = []
        self.argvs: list[list[str]] = []
        self.fails: Callable[[Path], bool] = lambda _checkout: False
        self.exit_code = 1

    def launch(
        self, argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        image = next(arg for arg in argv if arg.startswith("sha256:"))
        self.launches.append((image, argv[-1]))
        self.argvs.append(list(argv))
        mount = argv[argv.index("-v") + 1]
        if self.fails(Path(mount.split(":", 1)[0])):
            stderr = "error: Cannot find implementation or library stub for newdep\n"
            return subprocess.CompletedProcess(argv, self.exit_code, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _imports_newdep(checkout: Path) -> bool:
    """The checkout uses a dependency the image was not built with."""
    run_py = checkout / "kew" / "src" / "app" / "run.py"
    return run_py.exists() and "import newdep" in run_py.read_text()


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real git, sh and timeout, so a check wrongly run on the host runs."""
    for tool in ("git", "sh", "timeout", "gtimeout"):
        found = shutil.which(tool)
        if found:
            monkeypatch.setitem(TOOL_PATHS, tool, found)


def _runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, images: dict[str, str]
) -> _Runtime:
    """A runtime executable holding *images* (tag -> ID), found by the probe.

    Each image is one kew built: a pin tag carries the inputs label it names.
    """
    configs = {tag: inspect_config(tag) for tag in images}
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir(parents=True)
    stub.write_text(
        f"#!{sys.executable}\nUNANSWERED = {UNANSWERED!r}\n"
        + _STUB_RUNTIME.format(images=json.dumps(images), configs=json.dumps(configs))
    )
    stub.chmod(0o755)
    # Registered here so the allowlist entry is undone after the test.
    monkeypatch.setitem(TOOL_PATHS, str(stub), str(stub))
    runtime = _Runtime()
    runtime_present(monkeypatch, str(stub))
    monkeypatch.setattr(
        verification, "run_container_with_cpu_cap_retry", runtime.launch
    )
    return runtime


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(work: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", message)
    return _git(work, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """``main`` and a head that changes Python and a ui-checked page:
    ``(work, base, head)``."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t.t")
    _git(work, "config", "user.name", "t")
    base = _commit(
        work,
        {
            "kew/src/app/__init__.py": "",
            "frontend/src/routes/work/+page.svelte": "<h1>Work</h1>\n",
            "frontend/scripts/ui-checks/work-rows.mjs": (
                "// ui-check-owns: src/routes/work/**\n"
            ),
        },
        "base",
    )
    _git(work, "checkout", "-qb", "agent/issue-1")
    head = _commit(
        work,
        {
            "kew/src/app/run.py": "VALUE = 1\n",
            "frontend/src/routes/work/+page.svelte": "<h1>Work queue</h1>\n",
        },
        "agent work",
    )
    _git(work, "checkout", "-q", "main")
    return work, base, head


def _head_with(work: Path, files: dict[str, str]) -> str:
    """One more commit on the head branch of :func:`_repo`; the new head."""
    _git(work, "checkout", "-q", "agent/issue-1")
    head = _commit(work, files, "more agent work")
    _git(work, "checkout", "-q", "main")
    return head


def _config(*, check_image: str = "", skip_containment: bool = False) -> DispatchConfig:
    config = DispatchConfig(
        resolve=ResolveConfig(python_checks=PYTHON_CHECK),
        sandbox=SandboxConfig(
            images={"claude-code": RUNNER_IMAGE},
            check_image=check_image,
            dangerously_skip_containment=skip_containment,
        ),
    )
    config.commands.static = STATIC
    config.commands.ui_check = UI_CHECK
    return config


def _resolved_worktree(tmp_path: Path) -> tuple[Path, str]:
    """A resolved worktree whose integration changed a Python file, and the
    PR head it is compared with."""
    work, base, head = _repo(tmp_path)
    _git(work, "checkout", "-q", head)
    _commit(work, {"kew/src/app/panel.py": "VALUE = 2\n"}, "merge main")
    return work, head


def test_the_pre_review_gate_runs_in_the_check_image_when_one_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    work, _, head = _repo(tmp_path)
    config = _config(check_image=CHECK_IMAGE)

    admission = resolve_check_image_admission(config.sandbox, runner="claude-code")
    outcome = run_pre_review_gate(config, repo_root=work, base="main", head=head)

    assert (admission.posture, admission.image_ref, admission.image) == (
        CONTAINED_VERIFICATION_POSTURE,
        CHECK_IMAGE,
        CHECK_ID,
    )
    assert outcome.kind == "passed", outcome.detail
    assert runtime.launches == [
        (CHECK_ID, STATIC),
        (CHECK_ID, "npm run ui-check -- work-rows"),
    ]


def test_resolution_checks_run_in_the_check_image_when_one_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    worktree, since = _resolved_worktree(tmp_path)

    run_resolution_checks(worktree, _config(check_image=CHECK_IMAGE), since=since)

    assert runtime.launches == [(CHECK_ID, PYTHON_CHECK)]


def test_a_check_image_run_gives_vite_a_writable_temp_on_a_read_only_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """The exact argv of a resolution check in the check image (#3495).

    ``kew-check-frontend`` links the tree's ``node_modules`` to the image's,
    where Vite bundles ``vite.config.ts`` into ``node_modules/.vite-temp``:
    the root stays ``--read-only`` and only that one path is a writable tmpfs.
    Without it, every frontend check fails with ENOENT on any tree."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    worktree, since = _resolved_worktree(tmp_path)
    config = _config(check_image=CHECK_IMAGE)
    admission = resolve_check_image_admission(config.sandbox, runner="claude-code")
    assert admission.runtime is not None

    run_resolution_checks(worktree, config, since=since)

    [argv] = runtime.argvs
    name = argv[argv.index("--name") + 1]
    cidfile = argv[argv.index("--cidfile") + 1]
    assert name.startswith("kew-verify-")
    assert argv == [
        admission.runtime,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--cidfile",
        cidfile,
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
        "/opt/kew-check/frontend/node_modules/.vite-temp:size=64m,mode=1777",
        *sandbox.resource_limit_args(admission.runtime),
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        *[
            arg
            for proxy in ("HTTP", "HTTPS", "FTP", "ALL", "NO")
            for name_ in (f"{proxy}_PROXY", f"{proxy.lower()}_proxy")
            for arg in ("-e", f"{name_}=")
        ],
        "--entrypoint",
        "/bin/sh",
        "-v",
        f"{worktree.absolute()}:/work:ro",
        "-w",
        "/work",
        CHECK_ID,
        "-c",
        PYTHON_CHECK,
    ]


def test_a_runner_image_run_mounts_nothing_writable_over_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """Without a check image the scratch mount is not added: only ``/tmp`` is
    writable in the runner image."""
    runtime = _runtime(monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID})
    worktree, since = _resolved_worktree(tmp_path)

    run_resolution_checks(worktree, _config(), since=since)

    [argv] = runtime.argvs
    tmpfs = [argv[i + 1] for i, word in enumerate(argv) if word == "--tmpfs"]
    assert tmpfs == ["/tmp:size=1g"]
    assert "--read-only" in argv


def test_without_a_check_image_the_runner_image_is_used_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    work, _, head = _repo(tmp_path)
    config = _config()

    admission = resolve_check_image_admission(config.sandbox, runner="claude-code")
    command_admission = verification.resolve_command_verification_admission(
        config.sandbox, runner="claude-code"
    )
    outcome = run_pre_review_gate(config, repo_root=work, base="main", head=head)
    worktree, since = _resolved_worktree(tmp_path / "resolve")
    run_resolution_checks(worktree, config, since=since)

    assert admission == command_admission
    assert (admission.posture, admission.image_ref, admission.image) == (
        CONTAINED_VERIFICATION_POSTURE,
        RUNNER_IMAGE,
        RUNNER_ID,
    )
    assert outcome.kind == "passed", outcome.detail
    assert runtime.launches == [
        (RUNNER_ID, STATIC),
        (RUNNER_ID, "npm run ui-check -- work-rows"),
        (RUNNER_ID, PYTHON_CHECK),
    ]


def test_an_unresolvable_check_image_refuses_and_never_runs_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """The runner image resolves and the operator lets kew skip containment,
    yet a check image the runtime does not hold is a refusal: never the runner
    image, never the host."""
    runtime = _runtime(monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID})
    marker = tmp_path / "ran-on-host"
    work, _, head = _repo(tmp_path)
    config = _config(check_image=CHECK_IMAGE, skip_containment=True)
    config.commands.static = f"touch {marker}"
    config.resolve.python_checks = f"touch {marker}"

    admission = resolve_check_image_admission(config.sandbox, runner="claude-code")
    outcome = run_pre_review_gate(config, repo_root=work, base="main", head=head)
    worktree, since = _resolved_worktree(tmp_path / "resolve")
    with pytest.raises(
        ResolutionChecksFailed,
        match=r"python check \(\[resolve\] python_checks\) could not run",
    ) as refused:
        run_resolution_checks(worktree, config, since=since)

    assert (admission.posture, admission.image_ref, admission.image) == (
        None,
        CHECK_IMAGE,
        None,
    )
    assert admission.refusal is not None
    assert CHECK_IMAGE in admission.refusal
    assert "build-check-image.sh" in admission.refusal
    assert outcome.kind == "skipped"
    assert admission.refusal in outcome.detail
    assert CHECK_IMAGE in str(refused.value)
    assert runtime.launches == []
    assert not marker.exists()


def _tag(root: Path) -> str:
    result = subprocess.run(
        ["bash", str(root / "scripts" / "build-check-image.sh"), "--print-tag"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )
    return result.stdout.strip()


def _tag_inputs(root: Path) -> Path:
    """A copy at *root* of everything the build script reads."""
    for rel in (*TAG_INPUTS, TOOL_PINS, "scripts/build-check-image.sh"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, root / rel)
    return root


def _set_pin(root: Path, name: str, version: str | None) -> None:
    """Write *version* as the tool pin file's *name* pin, keeping its other
    lines; ``None`` drops the pin."""
    pins = root / TOOL_PINS
    kept = [
        line
        for line in pins.read_text().splitlines()
        if not line.startswith(f"{name}=")
    ]
    added = [] if version is None else [f"{name}={version}"]
    pins.write_text("\n".join([*kept, *added]) + "\n")


@pytest.mark.parametrize("pin", ["SEMGREP_VERSION", "SETUPTOOLS_VERSION"])
def test_the_check_image_tag_moves_with_each_pin_it_installs(
    tmp_path: Path, pin: str
) -> None:
    root = _tag_inputs(tmp_path / "repo")
    _set_pin(root, pin, "1.0.0")
    first = _tag(root)
    _set_pin(root, pin, "1.0.1")

    assert _tag(root) != first


@pytest.mark.parametrize("pin", ["SEMGREP_VERSION", "SETUPTOOLS_VERSION"])
def test_without_a_pin_it_installs_no_check_image_is_named(
    tmp_path: Path, pin: str
) -> None:
    """The image installs only what tool-pins.env names: no fallback version."""
    root = _tag_inputs(tmp_path / "repo")
    _set_pin(root, pin, None)

    result = subprocess.run(
        ["bash", str(root / "scripts" / "build-check-image.sh"), "--print-tag"],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert pin in result.stderr


@pytest.mark.parametrize("runtime_path", ["absolute", "relative"])
def test_the_check_image_is_built_with_the_pinned_semgrep_and_setuptools(
    tmp_path: Path, runtime_path: str
) -> None:
    """The build hands the Dockerfile the versions tool-pins.env names, read
    from the file wherever the script is called from, through a runtime named
    by an absolute path or by one relative to the caller (`./podman`)."""
    root = _tag_inputs(tmp_path / "repo")
    _set_pin(root, "SEMGREP_VERSION", "9.8.7")
    _set_pin(root, "SETUPTOOLS_VERSION", "6.5.4")
    argv_log = tmp_path / "runtime.argv"
    runtime = tmp_path / "runtime"
    runtime.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_log}"\n', encoding="utf-8"
    )
    runtime.chmod(0o755)
    named = str(runtime) if runtime_path == "absolute" else "../../runtime"

    built = subprocess.run(
        ["bash", "../scripts/build-check-image.sh"],
        cwd=root / "kew",
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "KEW_CHECK_RUNTIME": named},
    )

    argv = argv_log.read_text(encoding="utf-8").splitlines()
    build_args = [argv[i + 1] for i, word in enumerate(argv) if word == "--build-arg"]
    assert "SEMGREP_VERSION=9.8.7" in build_args
    assert "SETUPTOOLS_VERSION=6.5.4" in build_args
    assert built.stdout.strip() == _tag(root)
    assert argv[argv.index("--tag") + 1] == _tag(root)


def test_the_check_image_tag_moves_with_the_agent_image_pin(tmp_path: Path) -> None:
    root = _tag_inputs(tmp_path / "repo")
    first = _tag(root)
    kew_toml = root / "kew.toml"
    pinned = f'claude-code = "{RUNNER_IMAGE}"'
    assert pinned in kew_toml.read_text()
    kew_toml.write_text(
        kew_toml.read_text().replace(
            pinned, 'claude-code = "kew-agent:docker-ffffffffffff"'
        )
    )

    assert _tag(root) != first


def test_the_check_image_tag_moves_when_a_lock_file_moves(tmp_path: Path) -> None:
    """The tag names the inputs, so a lock that moved names an image that
    must be built again rather than one already on the machine."""
    root = _tag_inputs(tmp_path / "repo")

    first = _tag(root)
    assert first == _tag(root)
    assert first.startswith("kew-check:")
    assert len(first.removeprefix("kew-check:")) == 12
    with (root / "kew/uv.lock").open("a") as lock:
        lock.write("\n")
    second = _tag(root)
    with (root / "frontend/package-lock.json").open("a") as lock:
        lock.write("\n")
    third = _tag(root)

    assert len({first, second, third}) == 3
    assert _tag(REPO_ROOT) == first


@pytest.mark.policy
def test_kew_toml_pins_the_check_image_this_tree_builds() -> None:
    """kew's own ``[sandbox] check_image`` is the tag the build script prints
    for this tree, so a lock, Dockerfile, base-pin or tool-pin change that
    moves the tag cannot land without the re-pin (#3344)."""
    with (REPO_ROOT / "kew.toml").open("rb") as fh:
        pinned = tomllib.load(fh)["sandbox"]["check_image"]

    assert pinned == _tag(REPO_ROOT), (
        "kew.toml's [sandbox] check_image is stale: rebuild with "
        "`bash scripts/build-check-image.sh` and pin the tag it prints"
    )


def test_a_head_that_changes_the_check_images_inputs_is_not_failed_by_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """The check image's dependencies were frozen when it was built: a head
    that adds one to the lock fails there while the base passes, and that
    failure is the image's, which the agent (with no network) cannot fix."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    head = _head_with(
        work,
        {
            "kew/uv.lock": '[[package]]\nname = "newdep"\n',
            "kew/src/app/run.py": "import newdep\n",
        },
    )

    outcome = run_pre_review_gate(
        _config(check_image=CHECK_IMAGE), repo_root=work, base="main", head=head
    )

    assert outcome.kind == "skipped", outcome.detail
    assert outcome.findings == ()
    assert f"{STALE_IMAGE_REASON} (kew/uv.lock)" in outcome.detail
    assert "the head is held for a person at merge" in outcome.detail
    assert set(outcome.settled) == {"static", "ui_check"}


def test_a_head_that_leaves_the_check_images_inputs_alone_still_fails_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    head = _head_with(work, {"kew/src/app/run.py": "import newdep\n"})

    outcome = run_pre_review_gate(
        _config(check_image=CHECK_IMAGE), repo_root=work, base="main", head=head
    )

    assert outcome.kind == "failed", outcome.detail
    assert outcome.findings
    assert STALE_IMAGE_REASON not in outcome.detail


def test_a_head_that_edits_a_ui_check_module_is_still_judged_by_the_check_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """``[epic] merge_gate_paths`` names ``scripts/``, where the ui-check
    modules live, but a module is not something the image was built from."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    head = _head_with(
        work,
        {
            "frontend/scripts/ui-checks/work-rows.mjs": (
                "// ui-check-owns: src/routes/work/**\n// checks the queue\n"
            ),
            "kew/src/app/run.py": "import newdep\n",
        },
    )

    outcome = run_pre_review_gate(
        _config(check_image=CHECK_IMAGE), repo_root=work, base="main", head=head
    )

    assert outcome.kind == "failed", outcome.detail
    assert outcome.findings
    assert STALE_IMAGE_REASON not in outcome.detail


def test_check_image_inputs_refuses_a_blank_entry_and_strips_the_rest() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        SandboxConfig(check_image_inputs=["kew/uv.lock", "  "])

    config = SandboxConfig(check_image_inputs=[" kew/uv.lock "])

    assert config.check_image_inputs == ["kew/uv.lock"]


def _static_only(config: DispatchConfig) -> DispatchConfig:
    config.commands.ui_check = ""
    return config


@pytest.mark.parametrize(
    "unbuilt",
    [
        {"docs/.nvmrc": ""},
        {"www/package.json": '{"name": "www"}\n'},
        {"pyproject.toml": "[project]\nname = 'root'\n"},
    ],
    ids=["empty-nvmrc-in-docs", "another-package-json", "root-pyproject"],
)
def test_a_head_touching_files_the_check_image_is_not_built_from_is_judged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: None,
    unbuilt: dict[str, str],
) -> None:
    """With ``[sandbox] check_image_inputs`` naming what the image is built
    from, a dependency manifest elsewhere does not buy the head a skip."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    head = _head_with(work, {**unbuilt, "kew/src/app/run.py": "import newdep\n"})
    config = _static_only(_config(check_image=CHECK_IMAGE))
    config.sandbox.check_image_inputs = KEW_CHECK_IMAGE_INPUTS

    outcome = run_pre_review_gate(config, repo_root=work, base="main", head=head)

    assert outcome.kind == "failed", outcome.detail
    assert outcome.findings
    assert STALE_IMAGE_REASON not in outcome.detail


def test_a_head_touching_a_file_the_check_image_is_built_from_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    head = _head_with(
        work,
        {
            "kew/uv.lock": '[[package]]\nname = "newdep"\n',
            "docs/.nvmrc": "",
            "kew/src/app/run.py": "import newdep\n",
        },
    )
    config = _static_only(_config(check_image=CHECK_IMAGE))
    config.sandbox.check_image_inputs = KEW_CHECK_IMAGE_INPUTS

    outcome = run_pre_review_gate(config, repo_root=work, base="main", head=head)

    assert outcome.kind == "skipped", outcome.detail
    assert outcome.findings == ()
    assert f"{STALE_IMAGE_REASON} (kew/uv.lock)" in outcome.detail


def test_a_missing_tool_in_the_check_image_reads_as_not_runnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """Exit 127 is a tool the image lacks, whatever else the head changed."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    runtime.exit_code = 127
    work, _, _ = _repo(tmp_path)
    head = _head_with(
        work,
        {
            "kew/uv.lock": '[[package]]\nname = "newdep"\n',
            "kew/src/app/run.py": "import newdep\n",
        },
    )

    outcome = run_pre_review_gate(
        _static_only(_config(check_image=CHECK_IMAGE)),
        repo_root=work,
        base="main",
        head=head,
    )

    assert outcome.kind == "skipped", outcome.detail
    assert "the image cannot run the command (exit 127)" in outcome.detail
    assert STALE_IMAGE_REASON not in outcome.detail


def test_a_head_with_no_merge_base_is_still_judged_by_the_base_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """The check image's input diff cannot be taken without a merge base;
    the failure is then judged as any other, never dropped."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = _imports_newdep
    work, _, _ = _repo(tmp_path)
    _git(work, "checkout", "-q", "--orphan", "agent/unrelated")
    _git(work, "rm", "-rq", "--cached", ".")
    head = _commit(work, {"kew/src/app/run.py": "import newdep\n"}, "unrelated")
    _git(work, "checkout", "-qf", "main")

    outcome = run_pre_review_gate(
        _static_only(_config(check_image=CHECK_IMAGE)),
        repo_root=work,
        base="main",
        head=head,
    )

    assert outcome.kind == "failed", outcome.detail
    assert outcome.findings
    assert len(runtime.launches) == 2  # the head, then the base control


def test_a_check_image_the_runtime_cannot_answer_for_is_inspected_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runtime(monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: UNANSWERED})

    admission = resolve_check_image_admission(
        _config(check_image=CHECK_IMAGE).sandbox, runner="claude-code"
    )

    calls = (tmp_path / "bin" / "calls.log").read_text().splitlines()
    assert calls == [f"image inspect {CHECK_IMAGE}"]
    assert admission.refusal is not None
    assert "did not answer" in admission.refusal
    assert "Cannot connect to the Docker daemon" in admission.refusal


def test_an_absent_check_image_is_built_by_the_check_image_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew sandbox build`` builds runner images, never a check image: the
    refusal names the script that does."""
    _runtime(monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID})

    admission = resolve_check_image_admission(
        _config(check_image=CHECK_IMAGE).sandbox, runner="claude-code"
    )

    assert admission.refusal is not None
    assert "kew sandbox build" not in admission.refusal
    assert CHECK_IMAGE in admission.refusal
    assert "build it with scripts/build-check-image.sh" in admission.refusal


def test_a_red_resolution_check_in_the_check_image_names_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """A lock that main moved after the image was built fails resolution
    checks there until the image is rebuilt: the hold says so."""
    runtime = _runtime(
        monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID, CHECK_IMAGE: CHECK_ID}
    )
    runtime.fails = lambda _checkout: True
    worktree, since = _resolved_worktree(tmp_path)

    with pytest.raises(ResolutionChecksFailed) as refused:
        run_resolution_checks(worktree, _config(check_image=CHECK_IMAGE), since=since)

    assert "scripts/build-check-image.sh" in refused.value.summary
    assert "[sandbox] check_image" in refused.value.summary


def test_a_red_resolution_check_in_the_runner_image_names_no_check_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    runtime = _runtime(monkeypatch, tmp_path, {RUNNER_IMAGE: RUNNER_ID})
    runtime.fails = lambda _checkout: True
    worktree, since = _resolved_worktree(tmp_path)

    with pytest.raises(ResolutionChecksFailed) as refused:
        run_resolution_checks(worktree, _config(), since=since)

    assert "build-check-image.sh" not in refused.value.summary
