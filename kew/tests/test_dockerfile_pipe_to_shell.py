"""A download piped into a shell fails the image build when the download fails.

Without ``pipefail`` a ``curl ... | bash -`` whose download fails pipes an empty
script into bash, which exits 0. In one merge verify nodesource was unreachable,
the step went green, and apt installed Debian's own Node 20 in place of Node 22
(#2902). These tests pin both halves of that fix: every pipe-to-shell ``RUN``
runs under ``pipefail``, and the nodesource step refuses whatever Node it got
if it is not nodesource's pinned Node 22 with npm.

The agent images go further and pipe no download into a shell at all (#2428):
the base image is pinned by manifest digest, every download lands in a file
that is checked against a SHA-256 before the step uses it, apt installs one
exact nodesource ``nodejs`` build, and the uv archive is the release the verify
host's ``trivy.pin`` names.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from support.nodesource_key import NODESOURCE_KEY_SHA256

_REPO_ROOT = Path(__file__).parent.parent.parent
_DOCKERFILE_DIRS = (
    _REPO_ROOT / "docker",
    _REPO_ROOT / "kew" / "src" / "kew" / "_docker" / "docker",
)
_DOCKERFILES = sorted(p for d in _DOCKERFILE_DIRS for p in d.glob("*.Dockerfile"))

#: A pipe whose reader is a shell, optionally behind ``env VAR=value ...``.
_PIPE_TO_SHELL_RE = re.compile(
    r"\|\s*(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)?(?:ba)?sh\b"
)
_SET_PIPEFAIL_RE = re.compile(r"\bset\s+(?:-[a-z]*o\s+pipefail|-o\s+pipefail)")


def _instructions(text: str) -> list[tuple[str, str]]:
    """The Dockerfile's instructions as ``(KEYWORD, arguments)``.

    Comment lines are dropped, including inside a continued instruction, and
    a backslash-newline joins the lines it splits -- as Docker reads them.
    """
    logical: list[str] = []
    pending = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1]
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending)
    out = []
    for entry in logical:
        if entry.strip():
            keyword, _, args = entry.strip().partition(" ")
            out.append((keyword.upper(), args.strip()))
    return out


def _unguarded_pipes_to_shell(text: str) -> list[str]:
    """Every ``RUN`` that pipes into a shell with no ``pipefail`` in force."""
    shell: list[str] = []
    found = []
    for keyword, args in _instructions(text):
        if keyword == "FROM":
            shell = []
        elif keyword == "SHELL":
            shell = json.loads(args)
        elif keyword == "RUN" and _PIPE_TO_SHELL_RE.search(args):
            guarded = "pipefail" in shell and "-o" in shell
            if not guarded and not _SET_PIPEFAIL_RE.search(args):
                found.append(args)
    return found


def _nodesource_step(dockerfile: Path) -> tuple[list[str], str]:
    """The nodesource ``RUN`` body and the ``SHELL`` Docker runs it under."""
    shell = ["/bin/sh", "-c"]
    for keyword, args in _instructions(dockerfile.read_text()):
        if keyword == "FROM":
            shell = ["/bin/sh", "-c"]
        elif keyword == "SHELL":
            shell = json.loads(args)
        elif keyword == "RUN" and "deb.nodesource.com" in args:
            return shell, args
    raise AssertionError(f"{dockerfile.name} has no nodesource RUN step")


@pytest.mark.policy
def test_every_pipe_to_shell_in_the_dockerfiles_runs_under_pipefail() -> None:
    assert _DOCKERFILES, "no Dockerfiles found"
    offenders = {
        str(p.relative_to(_REPO_ROOT)): runs
        for p in _DOCKERFILES
        if (runs := _unguarded_pipes_to_shell(p.read_text()))
    }
    assert offenders == {}


def test_the_lint_flags_a_pipe_to_shell_without_pipefail() -> None:
    piped = (
        "RUN curl -fsSL https://example.invalid/setup " + "| bash - && \\\n    true\n"
    )
    assert _unguarded_pipes_to_shell("FROM debian\n" + piped)
    # A SHELL from an earlier stage does not carry past the next FROM.
    shell = 'SHELL ["/bin/bash", "-o", "pipefail", "-c"]\n'
    assert _unguarded_pipes_to_shell("FROM a\n" + shell + "FROM b\n" + piped)
    assert not _unguarded_pipes_to_shell("FROM a\n" + shell + piped)
    uv = "RUN curl -LsSf https://example.invalid/install.sh \\\n    " + "| env A=b sh\n"
    assert _unguarded_pipes_to_shell("FROM a\n" + uv)


_AGENT_IMAGE_DOCKERFILES = [p for p in _DOCKERFILES if p.name.startswith("agent")]
_AGENT_DOCKERFILES = [p for p in _DOCKERFILES if "deb.nodesource.com" in p.read_text()]
_TRIVY_PIN = _REPO_ROOT / "kew" / "src" / "kew" / "hosts" / "provision" / "trivy.pin"


def _ids(path: Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


def _steps(dockerfile: Path) -> list[tuple[list[str], str]]:
    """Every ``RUN`` body with the ``SHELL`` Docker runs it under."""
    shell = ["/bin/sh", "-c"]
    steps = []
    for keyword, args in _instructions(dockerfile.read_text()):
        if keyword == "FROM":
            shell = ["/bin/sh", "-c"]
        elif keyword == "SHELL":
            shell = json.loads(args)
        elif keyword == "RUN":
            steps.append((shell, args))
    return steps


def _step(dockerfile: Path, marker: str) -> tuple[list[str], str]:
    """The first ``RUN`` whose body names ``marker``."""
    for shell, body in _steps(dockerfile):
        if marker in body:
            return shell, body
    raise AssertionError(f"{dockerfile.name} has no RUN step naming {marker}")


def _pin_value(key: str) -> str:
    for line in _TRIVY_PIN.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip()
    raise AssertionError(f"{_TRIVY_PIN.name} has no {key}")


#: An absolute path as a step body spells it, wherever it stands: a command
#: name, an argument or a redirect target. Not the path part of a URL.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w$./:-])/[\w.+/-]+")


class _StepRun:
    """What a ``RUN`` step did against stubbed commands.

    ``calls`` holds one argv per command the step ran that is not a shell
    builtin, in order; ``checked`` holds every line ``sha256sum`` read;
    ``written`` holds, per file ``tee`` was told to write, the lines it read.
    """

    def __init__(self, result: subprocess.CompletedProcess[str], log: Path) -> None:
        self.returncode = result.returncode
        self.stderr = result.stderr
        self.calls: list[list[str]] = []
        self.checked: list[str] = []
        self.written: dict[str, list[str]] = {}
        for line in log.read_text().splitlines() if log.exists() else []:
            kind, _, rest = line.partition("\t")
            if kind == "call":
                self.calls.append(rest.split("\t"))
            elif kind == "sum":
                self.checked.append(rest)
            else:
                assert kind == "wrote", line
                path, _, text = rest.partition("\t")
                self.written.setdefault(path, []).append(text)

    def ran(self, command: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[0] == command]


def _run_step(
    tmp_path: Path,
    step: tuple[list[str], str],
    *,
    download_ok: bool = True,
    digest_ok: bool = True,
    arch: str = "amd64",
    node_version: str = "v22.23.4",
    with_npm: bool = True,
) -> _StepRun:
    """Run a Dockerfile ``RUN`` body with every outside command stubbed.

    PATH points nowhere, so every command that is not a shell builtin lands in
    bash's ``command_not_found_handle``, which records it and succeeds; ``curl``,
    ``sha256sum``, ``dpkg``, ``apt-get`` and ``tee`` are shell functions that
    record themselves too and answer as the arguments say (``tee`` records what
    it would have written). bash never consults ``command_not_found_handle``
    for a name with a slash in it, so every absolute path the body spells gets
    a recording function of that name as well: a step's ``/usr/local/bin/uv``
    is the stub, never the host's uv. Functions, not PATH shims,
    so the test runs where the temp root is mounted ``noexec``; and the host's
    node or npm can never stand in for what the stubbed ``apt-get install``
    "installed". bash runs the step with the options the Dockerfile's SHELL
    declares: Debian's ``/bin/sh`` is dash, which refuses a function named
    ``apt-get``.
    """
    shell, body = step
    assert shell[0] in ("/bin/sh", "/bin/bash"), shell
    real_bash = shutil.which("bash")
    assert real_bash is not None
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    record = (
        f"""_record() {{ local IFS=$'\\t'; printf 'call\\t%s\\n' "$*" >> {log}; }}"""
    )
    drain = "while IFS= read -r _line; do :; done"
    installs = [f"node() {{ echo {node_version}; }}"]
    if with_npm:
        installs.append("npm() { :; }")
    stubs = [
        record,
        f'command_not_found_handle() {{ _record "$@"; {drain}; return 0; }}',
        f'curl() {{ _record curl "$@"; return {0 if download_ok else 28}; }}',
        (
            'sha256sum() { _record sha256sum "$@"; '
            f"while IFS= read -r _line; do printf 'sum\\t%s\\n' \"$_line\" >> {log}; "
            f"done; return {0 if digest_ok else 1}; }}"
        ),
        f'dpkg() {{ _record dpkg "$@"; echo {arch}; }}',
        (
            'apt-get() { _record apt-get "$@"; '
            'if [ "$1" = install ]; then eval \'' + "; ".join(installs) + "'; fi; }"
        ),
        (
            'tee() { _record tee "$@"; local _line _file; '
            'while IFS= read -r _line || [ -n "$_line" ]; do '
            'for _file in "$@"; do case "$_file" in -*) ;; '
            f'*) printf \'wrote\\t%s\\t%s\\n\' "$_file" "$_line" >> {log} ;; '
            "esac; done; printf '%s\\n' \"$_line\"; done; }"
        ),
        *(
            f'{path}() {{ _record {path} "$@"; {drain}; return 0; }}'
            for path in sorted(set(_ABSOLUTE_PATH_RE.findall(body)))
        ),
    ]
    result = subprocess.run(  # noqa: S603 -- argv from the repo's own Dockerfile
        [real_bash, *shell[1:], "\n".join([*stubs, body])],
        env={"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return _StepRun(result, log)


def _download_path(curl_argv: list[str]) -> str:
    """Where a ``curl`` call writes, or "" when it writes to stdout."""
    for flag, value in zip(curl_argv, curl_argv[1:], strict=False):
        if flag in ("-o", "--output"):
            return value
    return ""


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_no_agent_dockerfile_pipes_a_download_into_a_shell(dockerfile: Path) -> None:
    piped = [body for _, body in _steps(dockerfile) if _PIPE_TO_SHELL_RE.search(body)]
    assert piped == []


def test_both_copies_of_every_agent_dockerfile_are_checked() -> None:
    names = sorted(p.name for p in _AGENT_IMAGE_DOCKERFILES)
    assert names == sorted(
        ["agent.Dockerfile", "agent-codex.Dockerfile", "agent-opencode.Dockerfile"] * 2
    )


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_every_remote_install_input_is_pinned_by_digest(
    tmp_path: Path, dockerfile: Path
) -> None:
    # The base image: by manifest digest, never by tag alone.
    bases = [
        a.split()[0] for k, a in _instructions(dockerfile.read_text()) if k == "FROM"
    ]
    assert bases
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", ref) for ref in bases), bases

    # No remote file reaches the image except through a RUN step below.
    assert not [
        a for k, a in _instructions(dockerfile.read_text()) if k == "ADD" and "://" in a
    ]
    downloaded = 0
    for index, step in enumerate(_steps(dockerfile)):
        run = _run_step(tmp_path / f"accepted-{index}", step)
        curls = run.ran("curl")
        assert not run.ran("wget"), step[1]
        if not curls:
            continue
        downloaded += 1
        # A failed check stops the step: nothing past the download and its
        # check ever runs.
        refused = _run_step(tmp_path / f"refused-{index}", step, digest_ok=False)
        assert refused.returncode != 0
        assert refused.ran("sha256sum")
        assert {argv[0] for argv in refused.calls} <= {"dpkg", "curl", "sha256sum"}

        # Each download lands in a file, and that file is checked against a
        # SHA-256 before the step runs anything else.
        for curl in curls:
            path = _download_path(curl)
            assert path, curl
            assert any(
                re.fullmatch(rf"[0-9a-f]{{64}}  {re.escape(path)}", line)
                for line in run.checked
            ), (path, run.checked)
        after = run.calls[run.calls.index(curls[-1]) + 1 :]
        assert after[0][0] == "sha256sum", after
    # The nodesource signing key and the uv archive.
    assert downloaded == 2

    # Node: one exact nodesource build, never whichever nodejs apt prefers.
    node = _run_step(tmp_path / "node", _step(dockerfile, "deb.nodesource.com"))
    installs = [a for a in node.ran("apt-get") if a[1:2] == ["install"]]
    assert installs
    packages = [arg for argv in installs for arg in argv if arg.startswith("nodejs")]
    assert packages
    assert all(re.fullmatch(r"nodejs=22\.\d+\.\d+-1nodesource1", p) for p in packages)


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
@pytest.mark.parametrize(
    ("arch", "target", "digest_key"),
    [
        ("amd64", "x86_64-unknown-linux-gnu", "UV_SHA256_X86_64_LINUX"),
        ("arm64", "aarch64-unknown-linux-gnu", "UV_SHA256_AARCH64_LINUX"),
    ],
)
def test_the_images_uv_is_the_release_the_host_pin_names(
    tmp_path: Path, dockerfile: Path, arch: str, target: str, digest_key: str
) -> None:
    run = _run_step(tmp_path, _step(dockerfile, "uv"), arch=arch)
    assert run.returncode == 0, run.stderr
    version = _pin_value("UV_VERSION")
    url = f"https://github.com/astral-sh/uv/releases/download/{version}/uv-{target}.tar.gz"
    (curl,) = run.ran("curl")
    assert curl[-1] == url
    assert run.checked == [f"{_pin_value(digest_key)}  {_download_path(curl)}"]
    (tar,) = run.ran("tar")
    assert tar[-2:] == [f"uv-{target}/uv", f"uv-{target}/uvx"]
    # The step's last word is the uv it installed, never the host's.
    assert run.calls[-1] == ["/usr/local/bin/uv", "--version"]


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_the_uv_step_refuses_an_unpinned_architecture(
    tmp_path: Path, dockerfile: Path
) -> None:
    run = _run_step(tmp_path, _step(dockerfile, "uv"), arch="riscv64")
    assert run.returncode != 0
    assert "no uv build is pinned for riscv64" in run.stderr
    assert not run.ran("curl")


@pytest.mark.parametrize("dockerfile", _AGENT_DOCKERFILES, ids=_ids)
def test_an_unreachable_nodesource_fails_the_step(
    tmp_path: Path, dockerfile: Path
) -> None:
    result = _run_step(
        tmp_path, _step(dockerfile, "deb.nodesource.com"), download_ok=False
    )
    assert result.returncode != 0, result.stderr
    # The step stops at the download: apt never ran.
    assert not result.ran("apt-get")


@pytest.mark.parametrize("dockerfile", _AGENT_DOCKERFILES, ids=_ids)
def test_a_nodesource_key_off_its_digest_fails_the_step(
    tmp_path: Path, dockerfile: Path
) -> None:
    result = _run_step(
        tmp_path, _step(dockerfile, "deb.nodesource.com"), digest_ok=False
    )
    assert result.returncode != 0
    assert "does not match its pinned SHA-256" in result.stderr
    # apt never saw the key: nothing was installed or configured.
    assert {argv[0] for argv in result.calls} == {"curl", "sha256sum"}


@pytest.mark.parametrize("dockerfile", _AGENT_DOCKERFILES, ids=_ids)
@pytest.mark.parametrize(
    ("node_version", "with_npm", "complaint"),
    [
        ("v20.19.2", True, "expected nodesource Node v22.23.4, got v20.19.2"),
        ("v22.20.0", True, "expected nodesource Node v22.23.4, got v22.20.0"),
        ("v22.23.4", False, "npm missing"),
    ],
)
def test_the_step_refuses_a_node_that_is_not_nodesource_22_with_npm(
    tmp_path: Path, dockerfile: Path, node_version: str, with_npm: bool, complaint: str
) -> None:
    result = _run_step(
        tmp_path,
        _step(dockerfile, "deb.nodesource.com"),
        node_version=node_version,
        with_npm=with_npm,
    )
    assert result.returncode != 0
    assert complaint in result.stderr


@pytest.mark.parametrize("dockerfile", _AGENT_DOCKERFILES, ids=_ids)
def test_the_step_passes_on_nodesource_22_with_npm(
    tmp_path: Path, dockerfile: Path
) -> None:
    result = _run_step(tmp_path, _step(dockerfile, "deb.nodesource.com"))
    assert result.returncode == 0, result.stderr
    assert [a[:2] for a in result.ran("apt-get")] == [
        ["apt-get", "update"],
        ["apt-get", "install"],
    ]


def _checked_files(run: _StepRun) -> dict[str, str]:
    """Each file ``sha256sum --check`` verified, with the digest it held it to."""
    files = {}
    for line in run.checked:
        digest, sep, path = line.partition("  ")
        assert sep and re.fullmatch(r"[0-9a-f]{64}", digest), line
        files[path] = digest
    return files


def _tar_archive(argv: list[str]) -> str:
    """The archive a ``tar`` call reads, from ``-f``/``--file`` in any spelling."""
    for index, arg in enumerate(argv[1:], start=1):
        if arg.startswith("--file="):
            return arg.removeprefix("--file=")
        if arg == "--file":
            return argv[index + 1]
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg:
            return arg.partition("f")[2] or argv[index + 1]
    raise AssertionError(f"tar call names no archive: {argv}")


def _apt_source_options(line: str) -> dict[str, str]:
    """The ``[key=value ...]`` options of a one-line apt source entry."""
    match = re.fullmatch(r"deb\s+\[([^\]]*)\]\s+\S+.*", line.strip())
    if match is None:
        return {}
    options = {}
    for option in match.group(1).split():
        key, _, value = option.partition("=")
        options[key] = value
    return options


def _trusted_keyrings(run: _StepRun) -> dict[str, str]:
    """Each file ``install`` wrote from a sha256-checked source, by destination."""
    checked = _checked_files(run)
    return {argv[-1]: argv[-2] for argv in run.ran("install") if argv[-2] in checked}


def test_the_harness_stubs_a_command_named_by_absolute_path(tmp_path: Path) -> None:
    # bash never hands a name with a slash to command_not_found_handle, so a
    # step's `/usr/local/bin/uv --version` would run whatever the host has.
    run = _run_step(
        tmp_path,
        (["/bin/sh", "-c"], "/bin/false --flag && /usr/local/bin/uv --version"),
    )
    assert run.returncode == 0, run.stderr
    assert run.calls == [["/bin/false", "--flag"], ["/usr/local/bin/uv", "--version"]]


def test_the_harness_records_what_tee_writes(tmp_path: Path) -> None:
    run = _run_step(
        tmp_path,
        (["/bin/sh", "-c"], "printf 'a b\\nc' | tee -a /etc/x /etc/y >/dev/null"),
    )
    assert run.returncode == 0, run.stderr
    assert run.written == {"/etc/x": ["a b", "c"], "/etc/y": ["a b", "c"]}


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_the_nodesource_apt_source_trusts_only_the_checked_key(
    tmp_path: Path, dockerfile: Path
) -> None:
    run = _run_step(tmp_path, _step(dockerfile, "deb.nodesource.com"))
    assert run.returncode == 0, run.stderr
    keyrings = _trusted_keyrings(run)
    assert keyrings, run.calls
    sources = [
        line
        for path, lines in run.written.items()
        if path.startswith("/etc/apt/sources.list.d/")
        for line in lines
        if "deb.nodesource.com" in line
    ]
    assert sources, run.written
    for line in sources:
        options = _apt_source_options(line)
        assert "trusted" not in options, line
        assert options.get("signed-by") in keyrings, (line, keyrings)


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_the_nodesource_keyring_is_the_file_sha256sum_checked(
    tmp_path: Path, dockerfile: Path
) -> None:
    run = _run_step(tmp_path, _step(dockerfile, "deb.nodesource.com"))
    assert run.returncode == 0, run.stderr
    (install,) = run.ran("install")
    assert install[-2] in _checked_files(run), (install, run.checked)
    # Nothing else names the keyring once the checked key is in place.
    keyring = install[-1]
    assert [argv for argv in run.calls if keyring in argv] == [install]


@pytest.mark.parametrize("dockerfile", _AGENT_IMAGE_DOCKERFILES, ids=_ids)
def test_tar_extracts_the_uv_archive_sha256sum_checked(
    tmp_path: Path, dockerfile: Path
) -> None:
    run = _run_step(tmp_path, _step(dockerfile, "uv"))
    assert run.returncode == 0, run.stderr
    (tar,) = run.ran("tar")
    assert _tar_archive(tar) in _checked_files(run), (tar, run.checked)


def test_every_agent_dockerfile_checks_the_one_verified_nodesource_key(
    tmp_path: Path,
) -> None:
    digests = {}
    for index, dockerfile in enumerate(_AGENT_IMAGE_DOCKERFILES):
        run = _run_step(tmp_path / str(index), _step(dockerfile, "deb.nodesource.com"))
        assert run.returncode == 0, run.stderr
        (install,) = run.ran("install")
        digests[_ids(dockerfile)] = _checked_files(run)[install[-2]]
    assert len(digests) == 6
    assert digests == dict.fromkeys(digests, NODESOURCE_KEY_SHA256)


def test_the_tar_archive_reader_finds_every_spelling() -> None:
    assert _tar_archive(["tar", "-xzf", "/a.tgz", "x"]) == "/a.tgz"
    assert _tar_archive(["tar", "-x", "-f", "/a.tgz"]) == "/a.tgz"
    assert _tar_archive(["tar", "-xf/a.tgz"]) == "/a.tgz"
    assert _tar_archive(["tar", "--file=/a.tgz", "-x"]) == "/a.tgz"
    assert _tar_archive(["tar", "-x", "--file", "/a.tgz"]) == "/a.tgz"
