"""The OpenShell in-sandbox launcher, run for real against a stub runner.

Under ``[sandbox] runtime = "openshell"`` the image ENTRYPOINT never runs:
``openshell sandbox exec`` replaces it and the host invokes
``docker/kew-openshell-launch`` instead. The launcher therefore owns everything
docker/agent-entrypoint.sh does before the runner starts, and the failures that
matter are invisible to a shape test:

* no ``KEW_AGENT_START <nonce>`` line and every attempt looks like a launch
  failure to :func:`kew.sandbox.is_startup_ack`;
* OpenShell points HOME at the image workdir, so a launcher that leaves it
  alone writes ~/.gitconfig and every runner config directory INTO the checkout
  kew publishes;
* each entrypoint does pre-dispatch work for the ONE runner it serves — claude's
  ``--print``, OpenCode's catalog probe. One launcher serves every image, so a
  dropped branch removes that guarantee on the OpenShell path and nowhere else.
  Codex has no entrypoint at all — its image is OpenShell-only — so the
  placeholder ``$HOME/.codex/auth.json`` exists only if this script writes it.

So these tests execute the script. They execute it with the HOST's bash and
coreutils, which is not the image's: see the launch-host probe below for the
two things the launcher needs from a userland, and what a host missing
either of them — macOS — does to a run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest
from support.process_facts import is_linux

from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER_NAME = "kew-openshell-launch"
_NONCE = "a" * 32

# Printed by the stub runner, so one stdout assertion covers the ack line, the
# exported environment the launcher hands the runner, and the piped prompt.
_RUNNER_MARKER = "runner-exec"

# OpenRouter-shaped, because that is the only prefix the catalog probe gates on.
_MODEL_IN_CATALOG = "openrouter/vendor/model-in-catalog"
_MODEL_ABSENT = "openrouter/vendor/model-not-in-catalog"


# The launcher is written for the IMAGE's userland — bash 5, GNU coreutils —
# but these tests execute it with the HOST's, and two of the things it leans on
# are absent on macOS (#2370). Both are fatal rather than cosmetic:
#
#   * `realpath -m`, which BSD realpath rejects outright ("illegal option -- m")
#     — so every launch exits 78 at the HOME normalization and nothing below is
#     ever reached;
#   * expanding an empty array under `set -u`, which bash 3.2 (still /bin/bash
#     on a Mac) calls an unbound variable — and the launcher's closing `exec`
#     ends on exactly that expansion of `AGENT_FLAGS`.
#
# Neither is a launcher bug to fix: `realpath -m` is what folds a `..` spelling
# away BEFORE the HOME-in-checkout refusal, and it is correct for the image. So
# the tests look for a userland that has both — Homebrew installs one alongside
# the BSD tools — and skip, naming that install, when no host one does.
_LAUNCH_PATH = "/usr/bin:/bin"

#: Homebrew's `coreutils` keeps the GNU tools under their unprefixed names here,
#: one directory per prefix (Apple silicon, then Intel).
_GNU_COREUTILS_DIRS = (
    "/opt/homebrew/opt/coreutils/libexec/gnubin",
    "/usr/local/opt/coreutils/libexec/gnubin",
)

#: `/bin/bash` first: on Linux it already IS the image-grade shell. The rest are
#: where Homebrew's bash 5 lands when `/bin/bash` is the Mac's 3.2.
_BASH_CANDIDATES = ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash")

_NO_LAUNCH_HOST = (
    "no userland here can run the OpenShell launcher: it needs `realpath -m` "
    "(GNU coreutils) and a bash that expands an empty array under `set -u` "
    "(bash >= 4.4). On macOS: `brew install coreutils bash`."
)


def _runs(argv: Sequence[str]) -> bool:
    """Does this command exist here and exit 0? The probe behind both checks."""
    try:
        reply = subprocess.run(list(argv), capture_output=True, check=False)
    except OSError:
        return False
    return reply.returncode == 0


def _resolves_missing_paths(realpath: Sequence[str]) -> bool:
    """GNU's `-m`: canonicalize a path whose components need not exist."""
    return _runs([*realpath, "-m", "--", "/nonexistent-for-probe/../x"])


def _expands_empty_arrays(bash: Sequence[str]) -> bool:
    """bash >= 4.4: `"${empty[@]}"` under `set -u` is empty, not an error."""
    return _runs([*bash, "-c", 'set -u; empty=(); printf "%s" "${empty[@]}"'])


class _LaunchHost(NamedTuple):
    """Where the launcher can be executed here: an interpreter and a PATH."""

    bash: str
    path: str


@cache
def _launch_bash() -> str | None:
    for candidate in (*_BASH_CANDIDATES, shutil.which("bash")):
        if candidate and _expands_empty_arrays([candidate]):
            return candidate
    return None


@cache
def _launch_path() -> str | None:
    for prefix in ("", *(f"{directory}:" for directory in _GNU_COREUTILS_DIRS)):
        path = f"{prefix}{_LAUNCH_PATH}"
        realpath = shutil.which("realpath", path=path)
        if realpath and _resolves_missing_paths([realpath]):
            return path
    return None


def _launch_host() -> _LaunchHost | None:
    bash, path = _launch_bash(), _launch_path()
    return None if bash is None or path is None else _LaunchHost(bash, path)


def _launcher(packaged: bool) -> Path:
    base = _REPO_ROOT / ("kew/src/kew/_docker/docker" if packaged else "docker")
    script = base / _LAUNCHER_NAME
    assert script.exists(), f"missing OpenShell launcher: {script}"
    return script


class _Sandbox:
    """One prepared launch: a checkout, a HOME, a stub runner and an env."""

    def __init__(self, tmp_path: Path) -> None:
        self.checkout = tmp_path / "work"
        self.checkout.mkdir()
        self.prompt = self.checkout / ".kew-prompt.txt"
        self.prompt.write_text("workload-prompt\n")
        self.home = tmp_path / "agent-home"
        # The stub runner is the ONLY thing allowed to write into the checkout,
        # so it writes a file there: the tree comparison below can then tell
        # "the runner wrote" apart from "nothing wrote".
        #
        # Handed to the launcher as ``/bin/sh <stub>`` rather than as an
        # executable of its own: pytest's tmp root is `noexec` on some hosts,
        # and a skipped launcher test is a launcher nobody checked.
        self.stub = tmp_path / "stub-runner.sh"
        self.stub.write_text(
            "#!/bin/sh\n"
            f"printf '{_RUNNER_MARKER}\\n'\n"
            'printf "home=%s\\n" "$HOME"\n'
            'printf "xdg=%s\\n" "$XDG_CONFIG_HOME"\n'
            'printf "traffic=%s\\n" "$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"\n'
            'printf "args=%s\\n" "$*"\n'
            'touch "$KEW_STUB_RUNNER_OUTPUT"\n'
            "cat\n"
        )
        self.runner = ("/bin/sh", str(self.stub))
        # Resolved once per session; the ``script`` fixture has already
        # skipped the test if this host has no userland that can launch.
        host = _launch_host()
        assert host is not None, _NO_LAUNCH_HOST
        self.bash = host.bash
        # Real git, not a stub: ``git config --global`` writing a real
        # ~/.gitconfig is the proof that HOME moved out of the checkout.
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "XDG_"))
        }
        self.env.update(
            {
                "PATH": host.path,
                "KEW_PROMPT_FILE": str(self.prompt),
                "KEW_AGENT_HOME": str(self.home),
                "KEW_STUB_RUNNER_OUTPUT": str(self.checkout / "runner-wrote-this"),
                # Off the host's /tmp, where the sandbox's default would land.
                "KEW_AGENT_EXEC_MARKER": str(tmp_path / "agent.exec"),
            }
        )

    def run(
        self,
        script: Path,
        *args: str,
        nonce: str = _NONCE,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, str(script), nonce, *(args or self.runner)],
            env=self.env,
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def checkout_tree(self) -> set[str]:
        return {
            path.relative_to(self.checkout).as_posix()
            for path in self.checkout.rglob("*")
        }


def _named_program(directory: Path, name: str, interpreter: str) -> Path:
    """A program the launcher resolves under ``name``, with no exec bit of its own.

    The launcher's per-runner pre-dispatch work is keyed on the program name
    (``claude``, ``codex``, ``opencode``), so exercising those branches needs a program
    actually CALLED that. It cannot be a script of its own: pytest's tmp root is
    ``noexec`` on some hosts, and a skipped launcher test is a launcher nobody
    checked. So it is a symlink to an already-executable interpreter, and the
    behaviour comes from the interpreter plus the files it is pointed at.
    """
    program = directory / name
    program.symlink_to(interpreter)
    return program


#: ``echo`` as a script: its argv on one line, whatever name it is run under.
_ARGV_ECHO = '#!/bin/sh\nprintf "%s\\n" "$*"\n'


def _echo_program(directory: Path, name: str) -> Path:
    """A program called ``name`` whose stdout is the argv it was exec'd with.

    A script where ``directory`` lets one run. A multi-call coreutils (uutils,
    the default on newer Ubuntu images) refuses ``/bin/echo`` under a name it
    does not know, so the symlink :func:`_named_program` makes cannot stand in
    there. Where the tmp root is ``noexec`` the script cannot run, and it falls
    back to that symlink, which a single-binary ``echo`` (GNU, BSD) runs under
    any name.
    """
    program = directory / name
    program.write_text(_ARGV_ECHO, encoding="utf-8")
    program.chmod(0o755)
    try:
        printed = subprocess.run(  # noqa: S603 - the script just written
            [str(program), "probe"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:  # EACCES: a noexec mount
        printed = ""
    if printed == "probe\n":
        return program
    program.unlink()
    return _named_program(directory, name, "/bin/echo")


#: The fake OpenCode's ``run`` script — what ``bash run ...`` executes.
_OPENCODE_RUN_SCRIPT = f"""\
printf '{_RUNNER_MARKER}\\n'
printf "args=%s\\n" "$*"
cat
"""


def _opencode_cli(tmp_path: Path, *, catalog: tuple[str, ...] | None) -> Path:
    """A fake ``opencode`` whose catalog probe and run are two readable scripts.

    ``bash <file>`` needs read permission only, so these work on a ``noexec``
    tmp root. The launcher probes ``"$AGENT_PROGRAM" models openrouter --pure``
    and then execs ``"$AGENT_PROGRAM" run ...``; with ``opencode`` pointed at
    bash those become ``bash models ...`` and ``bash run ...`` — hence the file
    names, and hence this directory being the launch's working directory.

    ``catalog=None`` makes the probe itself fail, as a broken CLI would.
    """
    directory = tmp_path / "opencode-cli"
    directory.mkdir()
    if catalog is None:
        (directory / "models").write_text("exit 1\n")
    else:
        (directory / "catalog.txt").write_text(
            "".join(f"{model}\n" for model in catalog)
        )
        (directory / "models").write_text("cat catalog.txt\n")
    (directory / "run").write_text(_OPENCODE_RUN_SCRIPT)
    _named_program(directory, "opencode", "/bin/bash")
    return directory


@pytest.fixture(params=[False, True], ids=["repository", "packaged"])
def script(request: pytest.FixtureRequest) -> Path:
    """The launcher — and the gate for every test that EXECUTES it.

    Taken by those tests and by no other, so the skip for a host that cannot
    run the script at all lands here, and the tests that only READ it (the
    packaged copy, the Dockerfiles, the build-input pin) still run everywhere.
    """
    if _launch_host() is None:
        pytest.skip(_NO_LAUNCH_HOST)
    return _launcher(request.param)


def test_launcher_acknowledges_then_pipes_the_prompt_to_the_runner(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)

    reply = sandbox.run(script, *sandbox.runner, "--print", "--model", "sonnet")

    assert reply.returncode == 0, reply.stderr
    lines = reply.stdout.splitlines()
    # The ack is FIRST: the host consumes it before parsing agent events.
    assert lines[0] == f"KEW_AGENT_START {_NONCE}"
    assert lines[1] == _RUNNER_MARKER
    # Runner flags ride in argv; the prompt rides on stdin and nowhere else.
    assert "args=--print --model sonnet" in lines
    assert "traffic=1" in lines
    assert lines[-1] == "workload-prompt"


def test_launcher_marks_the_exec_with_the_agents_own_pid_and_start_time(
    script: Path, tmp_path: Path
) -> None:
    """The marker names the process the agent becomes, not the launcher's.

    ``exec`` keeps the pid, so the runner reporting its own ``$$`` is the
    proof; the second field is that process's ``/proc`` start time, which is
    what lets the host's probe tell a reused pid from the agent (#2625).
    """
    if not is_linux():
        pytest.skip("no /proc on this host")
    sandbox = _Sandbox(tmp_path)

    reply = sandbox.run(script, "/bin/sh", "-c", 'echo "pid=$$"')

    assert reply.returncode == 0, reply.stderr
    pid, start = (tmp_path / "agent.exec").read_text().split()
    assert f"pid={pid}" in reply.stdout.splitlines()
    assert start.isdigit()


def test_launcher_puts_home_and_config_outside_the_published_checkout(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)

    reply = sandbox.run(script)

    assert reply.returncode == 0, reply.stderr
    values = dict(
        line.split("=", 1)
        for line in reply.stdout.splitlines()
        if line.startswith(("home=", "xdg="))
    )
    for name, value in values.items():
        resolved = Path(value).resolve()
        assert resolved.is_absolute(), name
        assert not resolved.is_relative_to(sandbox.checkout), (
            f"{name} lands inside the checkout kew publishes: {resolved}"
        )
    assert Path(values["xdg"]).is_dir()
    # The git bootstrap followed HOME instead of seeding the checkout.
    gitconfig = sandbox.home / ".gitconfig"
    written = gitconfig.read_text()
    assert "directory = /work" in written
    assert "kew-agent" in written


def test_launcher_writes_nothing_into_the_checkout_itself(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    before = sandbox.checkout_tree()

    reply = sandbox.run(script)

    assert reply.returncode == 0, reply.stderr
    assert sandbox.checkout_tree() - before == {"runner-wrote-this"}
    assert before == {".kew-prompt.txt"}


def test_launcher_refuses_a_home_inside_the_checkout_without_acknowledging(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    # Exactly what OpenShell hands the launcher by default: HOME == workdir.
    sandbox.env["KEW_AGENT_HOME"] = str(sandbox.checkout / "home")
    before = sandbox.checkout_tree()

    reply = sandbox.run(script)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "inside the published checkout" in reply.stderr
    # Refused BEFORE the mkdir: creating it would already be the leak.
    assert sandbox.checkout_tree() == before


def test_launcher_refuses_a_home_symlinked_back_into_the_checkout(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    decoy = tmp_path / "decoy-home"
    decoy.symlink_to(sandbox.checkout, target_is_directory=True)
    sandbox.env["KEW_AGENT_HOME"] = str(decoy)

    reply = sandbox.run(script)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "inside the published checkout" in reply.stderr


def test_launcher_refuses_a_relative_home(script: Path, tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path)
    sandbox.env["KEW_AGENT_HOME"] = "relative-home"

    reply = sandbox.run(script)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "absolute path" in reply.stderr


@pytest.mark.parametrize("nonce", ["", "not a nonce", "nonce\nforged"])
def test_launcher_refuses_a_malformed_nonce(
    script: Path, tmp_path: Path, nonce: str
) -> None:
    sandbox = _Sandbox(tmp_path)

    reply = sandbox.run(script, nonce=nonce)

    assert reply.returncode == 64
    assert "KEW_AGENT_START" not in reply.stdout


def test_launcher_refuses_a_missing_prompt_and_a_missing_runner(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    sandbox.prompt.unlink()

    refused = sandbox.run(script)

    assert refused.returncode == 64
    assert "KEW_AGENT_START" not in refused.stdout

    sandbox.prompt.write_text("workload-prompt\n")
    missing = sandbox.run(script, str(tmp_path / "runner-that-is-not-installed"))

    assert missing.returncode == 78
    assert "KEW_AGENT_START" not in missing.stdout


def test_launcher_never_resolves_the_runner_through_path(
    script: Path, tmp_path: Path
) -> None:
    """A shadowing `claude` first on PATH is never what runs (#2637).

    The checkout's own `bin/claude` is put FIRST on PATH, as a host PATH naming
    the checkout used to put it. A bare name must be refused outright — before
    the acknowledgement and before the shadow can run — and the shadow's
    side effect, a file it would write, must not exist.
    """
    sandbox = _Sandbox(tmp_path)
    shadow_dir = sandbox.checkout / "bin"
    shadow_dir.mkdir()
    ran = tmp_path / "shadow-ran"
    shadow = _named_program(shadow_dir, "claude", "/usr/bin/touch")
    sandbox.env["PATH"] = f"{shadow_dir}:{sandbox.env['PATH']}"

    reply = sandbox.run(script, "claude", str(ran))

    assert reply.returncode == 64, reply.stderr
    assert "absolute path" in reply.stderr
    assert "KEW_AGENT_START" not in reply.stdout
    assert not ran.exists()

    # Named by its absolute path instead, it is still refused: it lives in the
    # checkout, which is the PR's tree and governs nothing.
    inside = sandbox.run(script, str(shadow), str(ran))

    assert inside.returncode == 78, inside.stderr
    assert "inside the checkout" in inside.stderr
    assert "KEW_AGENT_START" not in inside.stdout
    assert not ran.exists()


def test_launcher_refuses_a_runner_path_that_traverses_into_the_checkout(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    shadow = _named_program(sandbox.checkout, "claude", "/bin/echo")
    traversal = f"{tmp_path}/elsewhere/../{sandbox.checkout.name}/{shadow.name}"

    reply = sandbox.run(script, traversal)

    assert reply.returncode == 78, reply.stderr
    assert "KEW_AGENT_START" not in reply.stdout


def test_launcher_refuses_a_call_with_no_program(script: Path, tmp_path: Path) -> None:
    sandbox = _Sandbox(tmp_path)

    reply = subprocess.run(
        [sandbox.bash, str(script), _NONCE],
        env=sandbox.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert reply.returncode == 64
    assert "usage" in reply.stderr


def test_launcher_seeds_the_opencode_catalog_into_the_moved_config_home(
    script: Path, tmp_path: Path
) -> None:
    """The OpenCode entrypoint seeds this asset; HOME moved, so it must too."""
    sandbox = _Sandbox(tmp_path)
    asset = tmp_path / "opencode.json"
    asset.write_text('{"provider": {}}\n')
    sandbox.env["KEW_OPENCODE_CONFIG"] = str(asset)

    reply = sandbox.run(script)

    assert reply.returncode == 0, reply.stderr
    seeded = sandbox.home / ".config" / "opencode" / "opencode.json"
    assert seeded.read_text() == asset.read_text()


def test_launcher_injects_the_image_owned_print_flag_for_claude(
    script: Path, tmp_path: Path
) -> None:
    """``claude`` must start HEADLESS, and ``--print`` is what makes it so.

    ``ClaudeCodeRunner.container_args()`` deliberately omits ``--print``: the
    flag belongs to the prompt handoff, which the IMAGE owns (contract element
    2), and ``docker/agent-entrypoint.sh`` execs ``claude --print "$@"``. Handed
    the host's own container args by a launcher that does not inject it,
    ``claude`` starts an INTERACTIVE session, reads nothing from the piped
    prompt file and emits no ``stream-json`` at all — so the host parses an
    empty event stream and reads every attempt as a launch failure.
    """
    from kew.config import DispatchConfig
    from kew.runners.registry import get_runner

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    host_args = get_runner("claude-code").container_args(
        config=config, model="claude-opus-5"
    )
    assert "--print" not in host_args, (
        "the host now sends --print itself; that is why the launcher injects it, "
        "so move the injection rather than dropping this assertion"
    )
    sandbox = _Sandbox(tmp_path)
    # echo under the name `claude`: the exec'd argv is then the runner's own
    # stdout, so the assertion reads the exact command line.
    claude = _echo_program(tmp_path, "claude")

    reply = sandbox.run(script, str(claude), *host_args)

    assert reply.returncode == 0, reply.stderr
    lines = reply.stdout.splitlines()
    assert lines[0] == f"KEW_AGENT_START {_NONCE}"
    # --print FIRST, then the host's args untouched: the entrypoint's argv.
    assert lines[1].split() == ["--print", *host_args]


def test_launcher_leaves_a_program_with_no_branch_of_its_own_alone(
    script: Path, tmp_path: Path
) -> None:
    """Only runners whose entrypoints do pre-dispatch work get a branch."""
    sandbox = _Sandbox(tmp_path)
    other = _echo_program(tmp_path, "some-other-runner")

    reply = sandbox.run(script, str(other), "--model", "sonnet")

    assert reply.returncode == 0, reply.stderr
    assert reply.stdout.splitlines()[1].split() == ["--model", "sonnet"]


def test_launcher_probes_the_opencode_catalog_then_hands_over_the_prompt(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    cli = _opencode_cli(
        tmp_path, catalog=("openrouter/vendor/other", _MODEL_IN_CATALOG)
    )

    reply = sandbox.run(
        script,
        str(cli / "opencode"),
        "run",
        "--format",
        "json",
        "-m",
        _MODEL_IN_CATALOG,
        cwd=cli,
    )

    assert reply.returncode == 0, reply.stderr
    lines = reply.stdout.splitlines()
    assert lines[0] == f"KEW_AGENT_START {_NONCE}"
    assert lines[1] == _RUNNER_MARKER
    assert f"args=--format json -m {_MODEL_IN_CATALOG}" in lines
    assert lines[-1] == "workload-prompt"


@pytest.mark.parametrize(
    "argv",
    [
        ("-m", _MODEL_ABSENT),
        ("--model", _MODEL_ABSENT),
        (f"--model={_MODEL_ABSENT}",),
    ],
    ids=["short", "long", "joined"],
)
def test_launcher_refuses_a_model_absent_from_the_opencode_catalog(
    script: Path, tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """``docker/agent-opencode-entrypoint.sh``'s guarantee, kept on this path.

    OpenCode answers a model missing from its local catalog with a generic
    server error, so the entrypoint refuses closed (78) BEFORE any model call.
    A launcher without the check turns that into an exit-0 dispatch that bills a
    model call and reports a runner-shaped failure instead.
    """
    sandbox = _Sandbox(tmp_path)
    cli = _opencode_cli(tmp_path, catalog=("openrouter/vendor/other",))

    reply = sandbox.run(script, str(cli / "opencode"), "run", *argv, cwd=cli)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert _RUNNER_MARKER not in reply.stdout
    assert "OpenCode catalog gap" in reply.stderr
    assert _MODEL_ABSENT in reply.stderr


def test_launcher_refuses_an_opencode_catalog_probe_that_fails(
    script: Path, tmp_path: Path
) -> None:
    sandbox = _Sandbox(tmp_path)
    cli = _opencode_cli(tmp_path, catalog=None)

    reply = sandbox.run(
        script, str(cli / "opencode"), "run", "-m", _MODEL_ABSENT, cwd=cli
    )

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert _RUNNER_MARKER not in reply.stdout
    assert "catalog probe failed" in reply.stderr


def test_launcher_probes_no_catalog_for_a_non_openrouter_opencode_model(
    script: Path, tmp_path: Path
) -> None:
    """The entrypoint gates the probe on an ``openrouter/`` model; so does this.

    The probe reads OpenCode's OpenRouter catalog, so it says nothing about any
    other provider — and a probe that cannot answer must not be the thing that
    refuses the dispatch. The fake CLI's probe fails here, and the launch still
    goes through.
    """
    sandbox = _Sandbox(tmp_path)
    cli = _opencode_cli(tmp_path, catalog=None)

    reply = sandbox.run(
        script, str(cli / "opencode"), "run", "-m", "anthropic/claude", cwd=cli
    )

    assert reply.returncode == 0, reply.stderr
    assert reply.stdout.splitlines()[0] == f"KEW_AGENT_START {_NONCE}"
    assert _RUNNER_MARKER in reply.stdout


def test_launcher_refuses_a_home_that_traverses_back_into_the_checkout(
    script: Path, tmp_path: Path
) -> None:
    """A ``..`` spelling is inside the checkout, whatever a prefix test says.

    ``KEW_AGENT_HOME`` is compared against the checkout as a textual prefix, so
    a traversal spelling passes that comparison while naming a directory inside
    the tree kew publishes — and the ``mkdir -p`` would then already have
    CREATED it there, which is the leak being refused. The launcher normalizes
    the path before the first check, so nothing is written either way.
    """
    sandbox = _Sandbox(tmp_path)
    traversal = tmp_path / "not-the-checkout" / ".." / "work" / "home"
    assert not str(traversal).startswith(str(sandbox.checkout))
    sandbox.env["KEW_AGENT_HOME"] = str(traversal)
    before = sandbox.checkout_tree()

    reply = sandbox.run(script)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "inside the published checkout" in reply.stderr
    assert sandbox.checkout_tree() == before
    assert not (sandbox.checkout / "home").exists()


#: Every value Codex's placeholder auth.json may legitimately carry, other than
#: the id_token (which is checked segment by segment below). Written as the exact
#: strings so a test failure names the drift rather than describing a shape.
_CODEX_PLACEHOLDER = "kew-openshell-placeholder"
_CODEX_ACCOUNT = f"{_CODEX_PLACEHOLDER}-account"
_CODEX_ACCESS = f"{_CODEX_PLACEHOLDER}-access-token"

#: Shapes real credentials take. None of them may appear in the file the
#: launcher writes, and the launcher refuses a forwarded value matching one —
#: the pilot's posture is no token inside the sandbox, and a provider variable
#: is exactly how a real one would arrive by accident.
_REAL_CREDENTIAL_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{8,}",  # OpenAI and Anthropic API keys
    r"sk_(live|test)_[A-Za-z0-9]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{16,}",  # GitHub tokens
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{12,}",  # AWS access key id
    r"xox[abprs]-[A-Za-z0-9-]{10,}",  # Slack
    r"AIza[0-9A-Za-z_-]{20,}",  # Google API key
)

#: One forwardable value per shape above, plus the JWT shape the sweep cannot
#: express (a signed JWT's own segments are base64url, so no literal prefix
#: distinguishes it from the unsigned placeholder id_token). The launcher must
#: refuse every one: a shape this module calls a real credential but the
#: launcher's own guard does not recognize is a token written verbatim into
#: $HOME/.codex/auth.json, which is the posture inverted (#2317 review).
_FORWARDED_CREDENTIAL_SAMPLES = {
    "openai-api-key": "sk-abcdefghijklmnopqrstuvwxyz",
    "stripe-secret-key": "sk_live_abcdefghijklmnopqr",
    "github-token": "ghp_abcdefghijklmnopqrstuvwxyz0123",
    "github-pat": "github_pat_abcdefghijklmnopqrstuvwxyz",
    "aws-access-key-id": "AKIAABCDEFGHIJKLMNOP",
    "slack-bot-token": "xoxb-1234567890-abcdefghijkl",
    "google-api-key": "AIzaSyAbcdefghijklmnopqrstuvwxyz01234",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
}

#: Shapes the launcher's guard carries and this module's sweep does not. The
#: guard may be WIDER than the sweep — refusing a value that is not a credential
#: costs a diagnostic — but never narrower. The JWT shape is here because the
#: sweep cannot express it (a signed JWT's segments are base64url, so no literal
#: prefix distinguishes it from the placeholder id_token the launcher writes on
#: purpose); the bare `sk_` prefix is here because the glob the guard replaced
#: refused all of it, not only Stripe's live/test spellings.
_LAUNCHER_ONLY_SHAPES = (
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    r"sk_[A-Za-z0-9_]{8,}",
)

#: A real credential forwarded by accident does not arrive alone — it arrives
#: inside whatever string carried it. The launcher's guard was once written with
#: prefix-anchored `case` globs while this module swept the written file with
#: re.search, so every spelling here passed IN and was only ever caught on the
#: way OUT, where nothing was looking (#2317 review).
_CREDENTIAL_AFFIXES = {
    "bearer-prefixed": "Bearer {value}",
    "assigned": "token={value}",
    "leading-space": " {value}",
    "trailing-text": "{value} (pasted from the host)",
    "wrapped": "prefix-{value}-suffix",
}


#: The two variables openshell-runtime N5 forwards. Both reach the same guard
#: and both land in auth.json, so a refusal proven on one proves nothing about
#: the other.
_CODEX_PLACEHOLDER_VARIABLES = ("KEW_CODEX_ACCOUNT_ID", "KEW_CODEX_ACCESS_TOKEN")

#: The two characters that are structural inside a JSON string: a quote ends
#: the string, and a backslash opens an escape — so neither survives being
#: interpolated into one by `printf`.
_STRUCTURAL_PLACEHOLDERS = {"quote": '"', "backslash": "\\"}

#: Control characters, which JSON forbids unescaped inside a string. Every one
#: is a member of `[[:cntrl:]]`; the guard once listed the newline ALONE, so a
#: forwarded placeholder carrying any of the rest wrote an auth.json nothing
#: can parse (#2317 review).
_CONTROL_PLACEHOLDERS = {
    "newline": "\n",
    "tab": "\t",
    "carriage-return": "\r",
    "escape": "\x1b",
    "vertical-tab": "\v",
    "form-feed": "\f",
    "bell": "\a",
    "null-adjacent-sohm": "\x01",
}

#: Everything the launcher must refuse before it writes the credential file.
_UNWRITABLE_PLACEHOLDERS = {**_STRUCTURAL_PLACEHOLDERS, **_CONTROL_PLACEHOLDERS}


def _codex_auth(sandbox: _Sandbox) -> dict[str, object]:
    path = sandbox.home / ".codex" / "auth.json"
    assert path.is_file(), f"the launcher wrote no Codex credential at {path}"
    # 0600: the file is a credential by position even when its contents are
    # placeholders, and the shape of the real one is the same file.
    assert path.stat().st_mode & 0o777 == 0o600
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _jwt_segment(value: str) -> str:
    """Decode one base64url JWT segment, restoring the stripped padding."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _codex_launch(
    sandbox: _Sandbox, script: Path, tmp_path: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Launch with a program actually NAMED codex — the branch keys on the name.

    ``echo`` under that name, so the exec'd argv is the runner's own stdout
    and the host's container args can be read back verbatim.
    """
    sandbox.env.update(environment)
    codex = _echo_program(tmp_path, "codex")
    return sandbox.run(script, str(codex), "exec", "--json")


def test_launcher_writes_the_chatgpt_mode_credential_codex_reads(
    script: Path, tmp_path: Path
) -> None:
    """Codex's own pre-dispatch work, and the only place it happens.

    Codex takes no credential from the environment: it reads
    ``$HOME/.codex/auth.json`` and exits before any model call when that file is
    absent or misshapen. The Codex image is OpenShell-only and ships no
    entrypoint, so this launcher is the one thing that can write it.

    The shape is not a guess — it is what ``codex login status`` accepts on the
    pinned CLI. ``auth_mode: apikey`` refuses outright ("API key auth is missing
    a key"), and the id_token is parsed: all three segments must be present and
    non-empty or the whole file is rejected as "invalid ID token format".
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path)

    assert reply.returncode == 0, reply.stderr
    lines = reply.stdout.splitlines()
    assert lines[0] == f"KEW_AGENT_START {_NONCE}"
    # No injected flags: CodexRunner.container_args() already sends `exec`.
    assert lines[1].split() == ["exec", "--json"]

    document = _codex_auth(sandbox)
    assert document["auth_mode"] == "chatgptAuthTokens"
    tokens = document["tokens"]
    assert isinstance(tokens, dict)
    assert tokens["access_token"] == _CODEX_ACCESS
    assert tokens["account_id"] == _CODEX_ACCOUNT
    # Empty, not a placeholder string: a refresh token is the one value here
    # that would be worth stealing, and nothing in the pilot refreshes.
    assert tokens["refresh_token"] == ""

    header, claims, signature = str(tokens["id_token"]).split(".")
    assert json.loads(_jwt_segment(header))["alg"] == "none"
    assert signature, "Codex rejects an id_token whose signature segment is empty"
    # Unsigned: a word, not cryptographic material.
    assert _jwt_segment(signature) == "unsigned"
    payload = json.loads(_jwt_segment(claims))
    assert payload["https://api.openai.com/auth"] == {
        "chatgpt_account_id": _CODEX_ACCOUNT,
        "chatgpt_plan_type": "pro",
    }


def test_the_codex_credential_contains_no_value_shaped_like_a_real_token(
    script: Path, tmp_path: Path
) -> None:
    """The pilot's posture, asserted against the bytes on disk.

    Every string the file carries is either empty, a declared placeholder, or a
    segment of an unsigned JWT whose own payload carries only placeholders. The
    id_token is unavoidably JWT-SHAPED — Codex parses it — so the check that
    matters for that field is that it decodes to nothing secret, which the test
    above makes assertion by assertion. Here the whole file is swept for the
    shapes a real credential takes.
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path)

    assert reply.returncode == 0, reply.stderr
    path = sandbox.home / ".codex" / "auth.json"
    raw = path.read_text(encoding="utf-8")
    for pattern in _REAL_CREDENTIAL_PATTERNS:
        assert re.search(pattern, raw) is None, (
            f"the placeholder credential matches {pattern!r}: {path}"
        )
    tokens = _codex_auth(sandbox)["tokens"]
    assert isinstance(tokens, dict)
    allowed = {"", _CODEX_ACCOUNT, _CODEX_ACCESS}
    for field, value in tokens.items():
        if field == "id_token":
            continue
        assert value in allowed, f"unexpected value in {field}"
    # Decoded, the JWT says only what the placeholders say.
    decoded = "".join(_jwt_segment(part) for part in str(tokens["id_token"]).split("."))
    for pattern in _REAL_CREDENTIAL_PATTERNS:
        assert re.search(pattern, decoded) is None, pattern


def test_every_swept_credential_shape_has_a_forwardable_sample() -> None:
    """Keeps the refusal test below as wide as the sweep above.

    Adding a shape to _REAL_CREDENTIAL_PATTERNS without a sample would leave
    the launcher's guard unexercised for it — which is how `xox[abprs]-` came
    to be swept out of the written file but accepted on the way in.
    """
    for pattern in _REAL_CREDENTIAL_PATTERNS + _LAUNCHER_ONLY_SHAPES:
        assert any(
            re.search(pattern, sample)
            for sample in _FORWARDED_CREDENTIAL_SAMPLES.values()
        ), f"no forwardable sample matches {pattern!r}"


@pytest.mark.parametrize(
    "value",
    _FORWARDED_CREDENTIAL_SAMPLES.values(),
    ids=_FORWARDED_CREDENTIAL_SAMPLES.keys(),
)
def test_launcher_refuses_a_forwarded_codex_value_shaped_like_a_credential(
    script: Path, tmp_path: Path, value: str
) -> None:
    """A provider variable is how a real token would arrive by accident.

    The placeholder values are the host provider's to name (openshell-runtime
    N5), so "no token inside the sandbox" has to be a property of this script
    rather than of its defaults. The refusal is BEFORE the acknowledgement and
    before the file is written: a launch that failed closed must leave no
    credential behind for a later attempt to pick up.
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path, KEW_CODEX_ACCESS_TOKEN=value)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "shaped like a real credential" in reply.stderr
    # Never echoed: the diagnostic names the variable, not its value.
    assert value not in reply.stderr
    assert not (sandbox.home / ".codex" / "auth.json").exists()


@pytest.mark.parametrize(
    "affix", _CREDENTIAL_AFFIXES.values(), ids=_CREDENTIAL_AFFIXES.keys()
)
@pytest.mark.parametrize(
    "value",
    _FORWARDED_CREDENTIAL_SAMPLES.values(),
    ids=_FORWARDED_CREDENTIAL_SAMPLES.keys(),
)
def test_launcher_refuses_a_credential_carried_inside_a_larger_value(
    script: Path, tmp_path: Path, value: str, affix: str
) -> None:
    """The guard searches the value; it does not merely look at its start.

    A forwarded credential arrives inside the string that carried it, and a
    guard anchored at position 0 reads ``Bearer ghp_...`` as an ordinary
    placeholder and writes it into $HOME/.codex/auth.json verbatim — the file
    sweep above never sees it, because that sweep runs against the DEFAULT
    placeholders (#2317 review).
    """
    sandbox = _Sandbox(tmp_path)
    forwarded = affix.format(value=value)

    reply = _codex_launch(sandbox, script, tmp_path, KEW_CODEX_ACCESS_TOKEN=forwarded)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "shaped like a real credential" in reply.stderr
    assert value not in reply.stderr
    assert not (sandbox.home / ".codex" / "auth.json").exists()


def test_the_launcher_guard_carries_every_swept_credential_shape(
    script: Path,
) -> None:
    """Parity between the two lists, compared literally rather than described.

    The sweep and the guard are two spellings of one claim — "these shapes are
    real credentials" — and they are enforced at opposite ends: the sweep over
    the file the launcher wrote, the guard over the value it was handed. Drift
    between them is not a style difference. A shape the sweep knows and the
    guard does not is a token accepted on the way in, which is the posture
    inverted (#2317 review); the same regexes on both sides, matched the same
    unanchored way, is what keeps that from recurring. The guard may carry MORE
    (_LAUNCHER_ONLY_SHAPES) — only the missing direction is a leak.
    """
    text = script.read_text(encoding="utf-8")
    block = re.search(r"^REAL_CREDENTIAL_SHAPES=\(\n(.*?)^\)$", text, re.M | re.S)
    assert block is not None, "the launcher declares no REAL_CREDENTIAL_SHAPES list"
    shapes = tuple(re.findall(r"^\s*'([^']+)'", block.group(1), re.M))

    assert shapes == _REAL_CREDENTIAL_PATTERNS + _LAUNCHER_ONLY_SHAPES


def test_launcher_writes_the_placeholder_values_the_host_forwards(
    script: Path, tmp_path: Path
) -> None:
    """The seam openshell-runtime N5 fills, exercised as a seam.

    A default nobody can replace would mean N5 reopening this script; a value
    read but never written would mean N5 thinking it had.
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(
        sandbox,
        script,
        tmp_path,
        KEW_CODEX_ACCOUNT_ID="provider-named-account",
        KEW_CODEX_ACCESS_TOKEN=PLACEHOLDER_CREDENTIAL_VALUE,
    )

    assert reply.returncode == 0, reply.stderr
    tokens = _codex_auth(sandbox)["tokens"]
    assert isinstance(tokens, dict)
    assert tokens["account_id"] == "provider-named-account"
    assert tokens["access_token"] == PLACEHOLDER_CREDENTIAL_VALUE
    claims = json.loads(_jwt_segment(str(tokens["id_token"]).split(".")[1]))
    # The claim and the field agree: Codex reads the account id out of the JWT.
    assert claims["https://api.openai.com/auth"]["chatgpt_account_id"] == (
        "provider-named-account"
    )


#: Opaque values no credential shape recognizes. Each could be a real token —
#: a vendor prefix the list never learned, a bare hex or base64 secret, a
#: session cookie — and each was written into auth.json verbatim while the
#: access token was guarded by a denylist alone (#2638).
_OPAQUE_ACCESS_TOKENS = {
    "arbitrary-word": "provider-named-placeholder",
    # Computed, not literal: a committed literal of this shape trips the
    # secret scanner on every later verify of the branch history.
    "hex-secret": hashlib.sha256(b"kew #2638 hex fixture").hexdigest(),
    "unknown-vendor-prefix": "pplx-0123456789abcdefABCDEF0123456789",
    "base64-secret": base64.b64encode(
        hashlib.sha256(b"kew #2638 base64 fixture").digest()[:27]
    ).decode(),
    "placeholder-with-suffix": f"{_CODEX_ACCESS}-0123456789abcdef",
    "placeholder-with-prefix": f"x{PLACEHOLDER_CREDENTIAL_VALUE}",
}


@pytest.mark.parametrize(
    "value", _OPAQUE_ACCESS_TOKENS.values(), ids=_OPAQUE_ACCESS_TOKENS.keys()
)
def test_launcher_refuses_an_access_token_that_is_not_an_exact_placeholder(
    script: Path, tmp_path: Path, value: str
) -> None:
    """A denylist cannot prove a placeholder; only an exact contract can.

    None of these values matches a credential shape, so the shape guard lets
    every one through — the refusal here is the exact contract's. It fails
    closed before the acknowledgement and before the file exists, and it never
    echoes the value it refused.
    """
    assert not any(
        re.search(shape, value)
        for shape in _REAL_CREDENTIAL_PATTERNS + _LAUNCHER_ONLY_SHAPES
    ), "the sample must be one the shape guard would accept"
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path, KEW_CODEX_ACCESS_TOKEN=value)

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "is not a kew placeholder value" in reply.stderr
    assert value not in reply.stderr
    assert not (sandbox.home / ".codex" / "auth.json").exists()


@pytest.mark.parametrize("value", [_CODEX_ACCESS, PLACEHOLDER_CREDENTIAL_VALUE])
def test_launcher_accepts_exactly_the_listed_access_token_placeholders(
    script: Path, tmp_path: Path, value: str
) -> None:
    """The two values the contract admits, each written through unchanged.

    The built-in default, and the value the host binds every brokered
    credential variable to — read from ``kew.openshell_broker`` rather than
    restated, so the launcher's list cannot drift from what the host forwards.
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path, KEW_CODEX_ACCESS_TOKEN=value)

    assert reply.returncode == 0, reply.stderr
    tokens = _codex_auth(sandbox)["tokens"]
    assert isinstance(tokens, dict)
    assert tokens["access_token"] == value


@pytest.mark.parametrize(
    "value", _UNWRITABLE_PLACEHOLDERS.values(), ids=_UNWRITABLE_PLACEHOLDERS.keys()
)
@pytest.mark.parametrize("variable", _CODEX_PLACEHOLDER_VARIABLES)
def test_launcher_refuses_a_codex_placeholder_it_cannot_write_as_json(
    script: Path, tmp_path: Path, variable: str, value: str
) -> None:
    """A quote would produce a file Codex reports as a corrupt credential.

    So would any of the other characters here, which is the point: the guard
    once named the newline on its own, and a forwarded placeholder carrying a
    tab, a carriage return or an escape wrote an auth.json
    :func:`json.loads` — and Codex — refuses (#2317 review). The whole
    ``[[:cntrl:]]`` class is what JSON forbids unescaped, so the whole class
    is what is pinned, on both variables the provider fills.
    """
    sandbox = _Sandbox(tmp_path)

    reply = _codex_launch(sandbox, script, tmp_path, **{variable: f"a{value}b"})

    assert reply.returncode == 78
    assert "KEW_AGENT_START" not in reply.stdout
    assert "cannot be written as JSON" in reply.stderr
    assert not (sandbox.home / ".codex" / "auth.json").exists()


def test_a_refused_placeholder_character_is_one_json_cannot_carry(
    script: Path, tmp_path: Path
) -> None:
    """The guard's premise, proven rather than asserted.

    Each value is refused because writing it verbatim breaks the file. This
    builds the document the launcher WOULD have produced and shows what
    :mod:`json` then does with it, so the refusal list cannot quietly grow
    into rejecting values that were fine all along: a control character makes
    the document unparseable, and a structural one either ends the string
    early or silently decodes to a value nobody forwarded.
    """
    for value in _CONTROL_PLACEHOLDERS.values():
        with pytest.raises(json.JSONDecodeError):
            json.loads(f'{{"account_id": "a{value}b"}}')

    for value in _STRUCTURAL_PLACEHOLDERS.values():
        handed = f"a{value}b"
        try:
            written = json.loads(f'{{"account_id": "{handed}"}}')["account_id"]
        except json.JSONDecodeError:
            continue
        assert written != handed, value

    # And the converse: an ordinary placeholder still launches.
    sandbox = _Sandbox(tmp_path)
    reply = _codex_launch(
        sandbox, script, tmp_path, KEW_CODEX_ACCOUNT_ID="provider-named-account"
    )

    assert reply.returncode == 0, reply.stderr
    assert _codex_auth(sandbox)["tokens"]


def test_launcher_writes_no_codex_credential_for_another_runner(
    script: Path, tmp_path: Path
) -> None:
    """Keyed on the program name: nothing else gets a credential file."""
    sandbox = _Sandbox(tmp_path)

    reply = sandbox.run(script)

    assert reply.returncode == 0, reply.stderr
    assert not (sandbox.home / ".codex").exists()


def test_the_suite_launches_on_an_image_grade_userland(
    script: Path, tmp_path: Path
) -> None:
    """#2370: every launch above needs `realpath -m`, so this pins the choice.

    The launcher normalizes KEW_AGENT_HOME with `realpath -m` before the
    HOME-in-checkout refusals, and ends on an empty-array expansion under
    `set -u`. A host without either — a Mac, with BSD realpath and bash 3.2 —
    failed all 30 launches at exit 78 with nothing about the launcher proven.

    A `realpath` that merely EXISTS is not evidence: BSD ships one. What is
    probed is the option, which is why the stub below is a working realpath
    that rejects exactly `-m`, and why it must not be mistaken for GNU's.

    Takes ``script`` for its skip alone: on a host with no fit userland there
    is nothing to assert here either, and the skip is the correct outcome.
    """
    bsd = tmp_path / "bsd-realpath"
    bsd.write_text(
        'case "$1" in\n'
        '  -m) printf "realpath: illegal option -- m\\n" >&2; exit 1 ;;\n'
        "esac\n"
        'printf "%s\\n" "$1"\n'
    )
    assert _runs(["/bin/sh", str(bsd), "--", "/tmp"])
    assert not _resolves_missing_paths(["/bin/sh", str(bsd)])

    sandbox = _Sandbox(tmp_path)
    chosen = shutil.which("realpath", path=sandbox.env["PATH"])
    assert chosen is not None, sandbox.env["PATH"]
    assert _resolves_missing_paths([chosen])
    assert _expands_empty_arrays([sandbox.bash])


def test_packaged_launcher_matches_the_repository_copy_bit_for_bit() -> None:
    repository, packaged = _launcher(False), _launcher(True)

    assert packaged.read_bytes() == repository.read_bytes()
    assert repository.stat().st_mode & 0o111
    assert bool(packaged.stat().st_mode & 0o111)


def test_every_agent_image_installs_the_launcher() -> None:
    expected = (
        "COPY --chown=agent:agent --chmod=0755 docker/kew-openshell-launch "
        f"/usr/local/bin/{_LAUNCHER_NAME}"
    )
    for dockerfile in (
        "agent.Dockerfile",
        "agent-opencode.Dockerfile",
        "agent-codex.Dockerfile",
    ):
        text = (_REPO_ROOT / "docker" / dockerfile).read_text(encoding="utf-8")
        assert expected in text, dockerfile


def test_the_launcher_is_a_pinned_build_input() -> None:
    """A launcher change must move the pin, or a stale image reads as fresh."""
    from kew.sandbox import _AGENT_IMAGE_BUILD_INPUTS

    assert f"docker/{_LAUNCHER_NAME}" in _AGENT_IMAGE_BUILD_INPUTS
