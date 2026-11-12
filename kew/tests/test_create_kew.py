from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from kew.subprocess_utils import TOOL_PATHS, run

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "create-kew" / "index.js"


def _pypi_spec() -> str:
    """The published package, with the extra that carries the web console,
    held to the launcher's own major version (read from its package.json, as
    the launcher reads it)."""
    version = json.loads((LAUNCHER.parent / "package.json").read_text())["version"]
    major = int(version.split(".")[0])
    return f"kew-cli[serve]>={major},<{major + 1}"


PYPI_SPEC = _pypi_spec()

# Every stub appends one line, "<tool> <args...>", to $STUB_LOG, so a test reads
# what ran and in what order from a file rather than from captured output.
STUB = """#!/bin/sh
printf '%s' "{name}" >> "$STUB_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$STUB_LOG"; done
printf '\\n' >> "$STUB_LOG"
{body}
"""

UV_BODY = """case "$1 $2" in
  "tool install") exit "${UV_INSTALL_EXIT:-0}" ;;
  "tool dir") [ -n "$UV_TOOL_BIN" ] || exit 1; printf '%s\\n' "$UV_TOOL_BIN" ;;
esac
exit 0"""


@dataclass(frozen=True)
class LaunchRun:
    returncode: int
    stdout: str
    stderr: str
    calls: list[str]
    cwd_entries: list[str]


@pytest.fixture
def work(tmp_path: Path, node: str) -> Iterator[Path]:
    """A scratch directory the stubs can execute from.

    Some sandboxes mount the temp root noexec; the stubs then live in a
    throwaway directory under HOME instead, removed after the test.
    """
    probe = tmp_path / "probe"
    probe.write_text("#!/bin/sh\n")
    probe.chmod(0o755)
    if run(["sh", "-c", shlex.quote(str(probe))], check=False).returncode == 0:
        yield tmp_path
        return
    fallback = Path(tempfile.mkdtemp(prefix="create-kew-", dir=Path.home()))
    try:
        yield fallback
    finally:
        shutil.rmtree(fallback)


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> str:
    found = shutil.which("node")
    shell = shutil.which("sh")
    assert found is not None, "Node.js is required for create-kew tests"
    assert shell is not None, "sh is required for create-kew tests"
    monkeypatch.setitem(TOOL_PATHS, "sh", shell)
    return found


def _stub(directory: Path, name: str, body: str = "exit 0") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(STUB.format(name=name, body=body))
    path.chmod(0o755)


def _launch(
    work: Path,
    node: str,
    args: list[str],
    *,
    uv: bool = True,
    kew_body: str = "exit 0",
    extra_env: dict[str, str] | None = None,
    launcher: Path = LAUNCHER,
) -> LaunchRun:
    bin_dir = work / "bin"
    _stub(bin_dir, "kew", kew_body)
    if uv:
        _stub(bin_dir, "uv", UV_BODY)
    target = work / "target"
    target.mkdir()
    log = work / "calls.log"
    log.touch()
    env = {
        "HOME": str(work),
        "LANG": "C.UTF-8",
        "NODE_OPTIONS": "",
        "PATH": str(bin_dir),
        "STUB_LOG": str(log),
        **(extra_env or {}),
    }
    command = "exec " + " ".join(
        shlex.quote(value) for value in (node, str(launcher), *args)
    )
    completed = run(
        ["sh", "-c", command + " < /dev/null"],
        check=False,
        cwd=target,
        env=env,
        timeout=10,
    )
    return LaunchRun(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        calls=log.read_text().splitlines(),
        cwd_entries=sorted(p.name for p in target.iterdir()),
    )


def test_forward_runner_and_yes_to_kew_init(work: Path, node: str) -> None:
    result = _launch(work, node, ["--runner", "codex", "--yes"])

    assert result.returncode == 0, result.stderr
    assert [c for c in result.calls if c.startswith("kew")] == [
        "kew init --runner codex --yes"
    ]


def test_forward_installs_the_published_kew_before_init(work: Path, node: str) -> None:
    result = _launch(work, node, ["--yes"])

    assert result.returncode == 0, result.stderr
    install = result.calls.index(f"uv tool install {PYPI_SPEC}")
    assert install < result.calls.index("kew init --yes")
    assert not any("git+" in call for call in result.calls)
    # One argument, no shell: `<` and `>` redirected nothing into the cwd.
    assert result.cwd_entries == []


@pytest.mark.policy
def test_the_install_is_held_to_the_launchers_major_version(
    work: Path, node: str, tmp_path: Path
) -> None:
    """A copy of the launcher whose package.json says 3.4.5 installs kew-cli
    3.x, read at run time rather than written into the source."""
    copy = tmp_path / "launcher"
    copy.mkdir()
    shutil.copy(LAUNCHER, copy / "index.js")
    (copy / "package.json").write_text('{"name": "create-kew", "version": "3.4.5"}\n')
    result = _launch(work, node, ["--yes"], launcher=copy / "index.js")

    assert result.returncode == 0, result.stderr
    assert "uv tool install kew-cli[serve]>=3,<4" in result.calls


def test_forward_preserves_argument_boundaries(work: Path, node: str) -> None:
    kew_body = """: > "$STUB_LOG.argv"
for arg in "$@"; do printf '%s\\0' "$arg" >> "$STUB_LOG.argv"; done"""
    args = ["--ci", "uv run pytest -q", "--budget-per-issue", "2.5", "--force"]

    result = _launch(work, node, args, kew_body=kew_body)

    assert result.returncode == 0, result.stderr
    argv = (work / "calls.log.argv").read_bytes().split(b"\0")[:-1]
    assert [a.decode() for a in argv] == ["init", *args]


def test_forward_prefers_the_kew_uv_just_installed(work: Path, node: str) -> None:
    tool_bin = work / "uv-tools"
    _stub(tool_bin, "kew", 'printf "tool-bin\\n" >> "$STUB_LOG"')

    result = _launch(work, node, ["--yes"], extra_env={"UV_TOOL_BIN": str(tool_bin)})

    assert result.returncode == 0, result.stderr
    assert result.calls[-2:] == ["kew init --yes", "tool-bin"]


def test_forward_returns_kew_init_exit_code(work: Path, node: str) -> None:
    result = _launch(work, node, ["--yes"], kew_body="exit 3")

    assert result.returncode == 3


def test_no_uv_exits_nonzero_naming_the_install_command(work: Path, node: str) -> None:
    result = _launch(work, node, ["--runner", "codex", "--yes"], uv=False)

    assert result.returncode != 0
    assert "https://astral.sh/uv/install.sh" in result.stderr
    assert f'uv tool install "{PYPI_SPEC}"' in result.stderr
    assert result.calls == []


def test_failed_install_stops_before_kew_init(work: Path, node: str) -> None:
    result = _launch(work, node, ["--yes"], extra_env={"UV_INSTALL_EXIT": "1"})

    assert result.returncode != 0
    assert f'uv tool install "{PYPI_SPEC}"' in result.stderr
    assert not any(c.startswith("kew") for c in result.calls)


def test_help_installs_nothing_and_writes_nothing(work: Path, node: str) -> None:
    result = _launch(work, node, ["--help"])

    assert result.returncode == 0
    assert "kew init" in result.stdout
    assert result.calls == []
    assert result.cwd_entries == []


def test_launcher_writes_no_files_of_its_own(work: Path, node: str) -> None:
    result = _launch(work, node, ["--yes"])

    assert result.returncode == 0, result.stderr
    assert result.cwd_entries == []


def test_launcher_carries_no_prompt_or_config_writer() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert not re.search(r"readline|toml", source)
    assert "writeFile" not in source
    assert "copyFile" not in source


def _package() -> dict:
    return json.loads((LAUNCHER.parent / "package.json").read_text(encoding="utf-8"))


def _same_bytes(name: str) -> bool:
    return (LAUNCHER.parent / name).read_bytes() == (REPO_ROOT / name).read_bytes()


def test_the_npm_tarball_holds_the_launcher_its_readme_and_the_license() -> None:
    # `files` decides the tarball: without it npm packed templates/, which the
    # launcher never reads, and no LICENSE or NOTICE (Apache-2.0 section 4).
    package = _package()
    assert package["files"] == ["index.js", "README.md", "LICENSE", "NOTICE"]
    assert package["bin"] == "index.js"
    for name in package["files"]:
        assert (LAUNCHER.parent / name).is_file(), name
    assert "templates" not in LAUNCHER.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_the_tarballs_license_files_are_the_repositorys(name: str) -> None:
    assert _same_bytes(name), f"create-kew/{name} differs from the root {name}"


def test_onboarding_docs_do_not_send_users_to_the_unpublished_wizard() -> None:
    # #2508: the one onboarding path is `kew init`; `npx create-kew` cannot
    # run until publication, so neither page may recommend it.
    for path in (REPO_ROOT / "README.md", REPO_ROOT / "docs/getting-started.md"):
        text = path.read_text()
        assert "npx create-kew" not in text, path
        assert "kew init" in text, path
