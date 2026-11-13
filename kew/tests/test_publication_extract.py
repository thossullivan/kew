"""W1.1 node 5b: the extraction container, argv and live.

Two halves. The argv tests always run and lock the shape — in particular that
every way this container differs from the agent's is a REDUCTION. The live
tests need docker and the agent image, and they check the things an argv
assertion cannot: that the read-only mount is really read-only, that a network
namespace with nothing in it really has nothing in it, that a credential in the
host environment really does not arrive, and that the whole chain reproduces the
tree git had.

Set ``KEW_TEST_AGENT_IMAGE`` to run the live half against a locally built image
(``docker build -f docker/agent.Dockerfile -t <tag> .``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kew import subprocess_utils
from kew.publication_apply import apply_to_tree
from kew.publication_artifact import read_artifact
from kew.publication_extract import (
    _GIT_FILE_MAX_BYTES,
    _PRODUCER_EXITS,
    OVERSIZE_BASE_BLOB,
    ExtractionFailed,
    extract_in_container,
    extract_on_host,
)
from kew.publication_filter import PublicationRefused, filter_artifact
from kew.publication_repository import CanonicalRepository
from kew.sandbox import (
    EXTRACT_ENTRYPOINT,
    EXTRACT_MOUNT_PATH,
    build_extract_argv,
    build_run_argv,
    resource_limit_args,
)

_OID = "a" * 40
_CPU_CAP_REJECTION = (
    "docker: Error response from daemon: Range of CPUs is from "
    "0.01 to 2.00, as there are only 2 CPUs available.\n"
)

_IMAGE = os.environ.get("KEW_TEST_AGENT_IMAGE", "")

# Opted in without docker names docker, so KEW_REQUIRE_TOOLS=1 can tell an
# opt-in left off (intended) from a missing tool (tests/support/required_tools.py).
_REQUIRES_IMAGE = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker"),
    reason=(
        "needs docker for the KEW_TEST_AGENT_IMAGE live half"
        if _IMAGE
        else "opt-in: set KEW_TEST_AGENT_IMAGE to a locally built agent image"
    ),
)

_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="needs git to build the layouts git itself writes",
)

_REQUIRES_PRODUCER = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "git", "jq", "sha256sum")),
    reason="needs the shell producer toolchain",
)


def _argv(tmp_path: Path, **overrides: object) -> list[str]:
    kwargs: dict[str, object] = {
        "runtime": "docker",
        "image": "kew-agent:latest",
        "checkout_dir": tmp_path / "checkout",
        "artifact_dir": tmp_path / "artifact",
        "slug": "1234",
        "base_sha": _OID,
        "uid": 501,
        "gid": 20,
    }
    kwargs.update(overrides)
    return build_extract_argv(**kwargs)  # type: ignore[arg-type]


class TestEveryDifferenceFromTheAgentIsAReduction:
    def test_it_gets_no_network_at_all(self, tmp_path: Path):
        # The agent needs egress and gets an allowlist. Extraction reads
        # committed objects on a local disk and needs none.
        argv = _argv(tmp_path)
        assert argv[argv.index("--network") + 1] == "none"

    def test_it_gets_no_credentials(self, tmp_path: Path):
        agent = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=tmp_path / "checkout",
            slug="1234",
            network="allowlist",
            runner="claude-code",
        )
        # Stated as a contrast, because "no credential names" is only meaningful
        # against a builder that does inject them.
        assert "CLAUDE_CODE_OAUTH_TOKEN" in agent

        argv = _argv(tmp_path)
        env_values = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        # Nothing here reads a model, so a token in this container could only
        # ever leak — never be used.
        assert env_values == [
            "HOME=/tmp",
            "HTTP_PROXY=",
            "http_proxy=",
            "HTTPS_PROXY=",
            "https_proxy=",
            "FTP_PROXY=",
            "ftp_proxy=",
            "ALL_PROXY=",
            "all_proxy=",
            "NO_PROXY=",
            "no_proxy=",
        ]

    def test_the_checkout_is_mounted_read_only(self, tmp_path: Path):
        argv = _argv(tmp_path)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert f"{tmp_path / 'checkout'}:/work:ro" in mounts
        # The artifact directory is the one thing it may write.
        assert f"{tmp_path / 'artifact'}:{EXTRACT_MOUNT_PATH}" in mounts

    def test_the_container_name_cannot_collide_with_the_agents(self, tmp_path: Path):
        # `docker kill kew-<slug>` must be unambiguous about which container it
        # stops, or a kill aimed at a runaway agent could take the extraction —
        # or miss.
        argv = _argv(tmp_path)
        assert argv[argv.index("--name") + 1] == "kew-extract-1234"

    def test_it_runs_the_extraction_entrypoint_over_the_agents(self, tmp_path: Path):
        argv = _argv(tmp_path)
        assert argv[argv.index("--entrypoint") + 1] == EXTRACT_ENTRYPOINT
        assert argv[-2:] == [_OID, EXTRACT_MOUNT_PATH]

    def test_the_runtime_stays_a_bare_name(self, tmp_path: Path):
        # argv[0] goes through the subprocess allowlist, which matches NAMES; a
        # resolved /usr/local/bin/docker is rejected at launch (#1017).
        assert _argv(tmp_path, runtime="docker")[0] == "docker"


class TestTheHardeningDoesNotDriftFromTheAgents:
    def test_both_containers_carry_the_same_hardening(self, tmp_path: Path):
        """Asserted rather than factored into a shared helper.

        ``build_run_argv``'s output is contractually byte-stable, and reordering
        it to share five flags would be a worse trade than a test that fails
        when the two disagree. The #1431 resource caps are the exception — both
        argvs take them from ``resource_limit_args()``, and that they are
        IDENTICAL is asserted below rather than transcribed here.
        """
        shared = [
            ("--rm", None),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--read-only", None),
            # Sized (#1431); the size is pinned in tests/test_sandbox.py.
            ("--tmpfs", "/tmp:size=1g"),
            ("--pids-limit", "1024"),
        ]
        agent = build_run_argv(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=tmp_path / "checkout",
            slug="1234",
            network="allowlist",
            runner="claude-code",
        )
        extract = _argv(tmp_path)

        for flag, value in shared:
            for argv, which in ((agent, "agent"), (extract, "extract")):
                assert flag in argv, f"{which} lost {flag}"
                if value is not None:
                    assert argv[argv.index(flag) + 1] == value, (
                        f"{which} changed {flag}"
                    )

        # A resource cap present on only one of the two containers is a cap an
        # attacker avoids by choosing the other one (#1431).
        caps = resource_limit_args()
        for flag, value in zip(caps[::2], caps[1::2], strict=True):
            for argv, which in ((agent, "agent"), (extract, "extract")):
                pairs = [argv[i : i + 2] for i, a in enumerate(argv) if a == flag]
                assert [flag, value] in pairs, f"{which} is missing {flag} {value}"


class TestTheBaseMustBeAResolvedObjectId:
    @pytest.mark.parametrize(
        "base",
        [
            "-x",  # would become an OPTION to the producer's git commands
            "--upload-pack=touch /tmp/pwned",
            "main",
            "origin/main",
            "a" * 39,
            "a" * 41,
            "A" * 40,  # uppercase is not what git prints
            "",
            "a" * 40 + " extra",
        ],
    )
    def test_anything_that_is_not_a_full_hex_oid_is_refused(
        self, tmp_path: Path, base: str
    ):
        with pytest.raises(ValueError, match="full hex object id"):
            _argv(tmp_path, base_sha=base)

    def test_both_widths_git_supports_are_accepted(self, tmp_path: Path):
        for width in (40, 64):
            assert _argv(tmp_path, base_sha="0" * width)[-2] == "0" * width


class TestEveryProducerRefusalHasAnExplanation:
    """The exit-code table and the producer must not drift apart.

    A code the table does not know surfaces to an operator as "exit 10" — a
    number with no meaning, at the exact moment a publication was refused and
    someone has to decide whether work was lost. Both halves are read from the
    real script rather than restated, so adding a refusal without explaining it
    fails here.
    """

    #: Docker's own launch failures, which the producer never emits itself.
    _RUNTIME_CODES = frozenset({125, 126, 127})

    def _script_codes(self) -> set[int]:
        repo_root = Path(__file__).resolve().parents[2]
        script = (repo_root / "docker" / "extract-artifact.sh").read_text(
            encoding="utf-8"
        )
        # `exit 0` is success and never reaches the table.
        return {int(code) for code in re.findall(r"\bexit (\d+)\b", script)} - {0}

    def test_every_code_the_producer_can_exit_with_is_explained(self) -> None:
        assert self._script_codes() <= set(_PRODUCER_EXITS)

    def test_the_table_explains_nothing_the_producer_cannot_emit(self) -> None:
        assert set(_PRODUCER_EXITS) - self._RUNTIME_CODES <= self._script_codes()

    def test_an_unknown_code_still_says_something(self, tmp_path: Path) -> None:
        with pytest.raises(ExtractionFailed, match="exit 42"):
            _fail_with(tmp_path, 42, "")


def _fail_with(tmp_path: Path, code: int, stderr: str) -> None:
    """Drive ``extract_in_container`` to a producer failure, without a container."""
    with mock.patch(
        "kew.publication_extract.run_container_with_cpu_cap_retry",
        return_value=subprocess.CompletedProcess([], code, "", stderr),
    ):
        extract_in_container(
            runtime="docker",
            image="kew-agent:latest",
            checkout_dir=tmp_path / "checkout",
            artifact_dir=tmp_path / "artifact",
            slug="1234",
            base_sha=_OID,
            proxy_environment="none",
        )


class TestTheOutputDirectoryIsOurs:
    def test_it_refuses_a_directory_that_already_exists(self, tmp_path: Path):
        # Docker creates a missing bind-mount target as ROOT, and the producer
        # refuses a directory with content in it. Creating it here, 0700, is
        # what keeps both of those from being the caller's problem.
        artifact_dir = tmp_path / "artifact"
        artifact_dir.mkdir()

        with pytest.raises(FileExistsError):
            extract_in_container(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=tmp_path / "checkout",
                artifact_dir=artifact_dir,
                slug="1234",
                base_sha=_OID,
                proxy_environment="none",
            )

    def test_a_refused_base_leaves_nothing_behind(self, tmp_path: Path):
        """Validation must happen before anything is created.

        Creating the directory first means a rejected base leaves it there, and
        the corrected retry then hits "already exists" — so one bad call
        poisons the path for the good one that follows it.
        """
        artifact_dir = tmp_path / "artifact"

        with pytest.raises(ValueError, match="full hex object id"):
            extract_in_container(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=tmp_path / "checkout",
                artifact_dir=artifact_dir,
                slug="1234",
                base_sha="origin/main",
                proxy_environment="none",
            )

        assert not artifact_dir.exists()

    def test_a_restrictive_umask_cannot_take_the_owner_bits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``mkdir(mode=...)`` is masked by the umask, verified not assumed:
        under ``0o277`` it yields ``0500``, which the container — running as
        this uid — cannot write the artifact into. The mode is set explicitly
        afterwards so it does not depend on ambient process state.
        """
        artifact_dir = tmp_path / "artifact"
        monkeypatch.setattr(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 4, "", "no such base"),
        )
        old = os.umask(0o277)
        try:
            with pytest.raises(ExtractionFailed):
                extract_in_container(
                    runtime="docker",
                    image="kew-agent:latest",
                    checkout_dir=tmp_path / "checkout",
                    artifact_dir=artifact_dir,
                    slug="1234",
                    base_sha=_OID,
                    proxy_environment="none",
                )
        finally:
            os.umask(old)

        assert artifact_dir.stat().st_mode & 0o777 == 0o700
        assert os.access(artifact_dir, os.W_OK | os.X_OK)


class TestCpuCapacityRace:
    @pytest.mark.parametrize(
        "conflict",
        [
            "docker: Error response from daemon: Conflict. The container name "
            '"/kew-extract-1234" is already in use by container "private-id".',
            "Error: creating container storage: the container name "
            '"kew-extract-1234" is already in use by "private-id".',
        ],
        ids=["docker", "podman"],
    )
    @pytest.mark.parametrize("cpu_message", ["", _CPU_CAP_REJECTION])
    def test_name_conflict_never_removes_or_retries(
        self, tmp_path, conflict, cpu_message
    ):
        calls = []
        cidfile = None

        def runtime(argv, **kwargs):
            nonlocal cidfile
            calls.append(argv)
            if argv[1] == "run":
                cidfile = Path(argv[argv.index("--cidfile") + 1])
                return subprocess.CompletedProcess(
                    argv, 125, "", cpu_message + conflict
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch("kew.sandbox.subprocess_utils.run", side_effect=runtime),
            mock.patch("kew.sandbox._daemon_cpu_count", return_value=4),
            pytest.raises(ExtractionFailed) as failed,
        ):
            extract_in_container(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=tmp_path / "checkout",
                artifact_dir=tmp_path / "artifact",
                slug="1234",
                base_sha=_OID,
                proxy_environment="none",
            )

        assert [argv[1] for argv in calls] == ["run"]
        assert "container name already in use" in str(failed.value)
        assert "private-id" not in str(failed.value)
        assert cidfile is not None and not cidfile.exists()

    def test_extraction_retries_four_to_two_once_at_one_cpu(self, tmp_path: Path):
        artifact_dir = tmp_path / "artifact"
        artifact = object()
        container_calls: list[list[str]] = []
        cidfile: Path | None = None

        def fake_run(argv, **kwargs):
            nonlocal cidfile
            if argv[1] == "inspect":
                assert argv[-1] == "a" * 64
                state = json.dumps(
                    {
                        "Name": "/kew-extract-1234",
                        "State": {"Status": "exited", "ExitCode": 0},
                    }
                )
                return subprocess.CompletedProcess(argv, 0, state, "")
            if argv[1] == "rm":
                assert argv == ["docker", "rm", "a" * 64]
                return subprocess.CompletedProcess(argv, 0, "", "")
            assert argv[1] == "run", argv
            container_calls.append(argv)
            if len(container_calls) == 1:
                return subprocess.CompletedProcess(argv, 125, "", _CPU_CAP_REJECTION)
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.write_text("a" * 64, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch("kew.sandbox.subprocess_utils.run", side_effect=fake_run) as run,
            mock.patch("kew.sandbox._daemon_cpu_count", side_effect=[4, 2]) as probe,
            mock.patch(
                "kew.publication_extract.read_artifact", return_value=artifact
            ) as read,
        ):
            result = extract_in_container(
                runtime="docker",
                image="kew-agent:latest",
                checkout_dir=tmp_path / "checkout",
                artifact_dir=artifact_dir,
                slug="1234",
                base_sha=_OID,
                proxy_environment="none",
            )

        assert result is artifact
        assert [argv[argv.index("--cpus") + 1] for argv in container_calls] == [
            "4",
            "1",
        ]
        container_run_calls = [
            call for call in run.call_args_list if call.args[0][1] == "run"
        ]
        timeouts = [call.kwargs["timeout"] for call in container_run_calls]
        assert timeouts[0] == pytest.approx(300, abs=0.5)
        assert 0 < timeouts[1] <= timeouts[0]  # one shared deadline, not two budgets
        assert probe.call_args_list == [mock.call("docker"), mock.call("docker")]
        # After the retry succeeds, completion is confirmed through runtime
        # state and the container is removed once without force.
        assert [call.args[0][1] for call in run.call_args_list] == [
            "run",
            "run",
            "inspect",
            "rm",
        ]
        assert run.call_args_list[-1].args[0] == ["docker", "rm", "a" * 64]
        read.assert_called_once_with(artifact_dir)
        assert cidfile is not None
        assert not cidfile.exists()


@_REQUIRES_PRODUCER
def test_host_walker_matches_the_shell_producer_bytes(tmp_path: Path, _tools) -> None:
    """Both producers encode the same mixed workspace identically."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", ".")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "edit.txt").write_text("before\n")
    (checkout / "gone.txt").write_text("gone\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")

    canonical_path = tmp_path / "canonical.git"
    subprocess.run(["git", "init", "--bare", "-q", str(canonical_path)], check=True)
    _git(checkout, "push", "-q", str(canonical_path), f"{base}:refs/heads/main")
    (checkout / "edit.txt").write_text("after\n")
    (checkout / "gone.txt").unlink()
    (checkout / "added.txt").write_text("added\n")
    executable = checkout / "run.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    script = Path(__file__).resolve().parents[2] / "docker" / "extract-artifact.sh"
    shell_dir = tmp_path / "shell"
    produced = subprocess.run(
        ["bash", str(script), base, str(shell_dir)],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )
    assert produced.returncode == 0, produced.stderr

    canonical = CanonicalRepository(path=canonical_path, base_sha=base)
    objects = canonical.base_blob_oids()
    host_dir = tmp_path / "host"
    extract_on_host(
        checkout,
        host_dir,
        canonical.base_state(),
        lambda path: canonical.read_blob(objects[path]),
        base,
    )

    assert (host_dir / "manifest.json").read_bytes() == (
        shell_dir / "manifest.json"
    ).read_bytes()
    assert {
        path.name: path.read_bytes() for path in (host_dir / "blobs").iterdir()
    } == {path.name: path.read_bytes() for path in (shell_dir / "blobs").iterdir()}


# --------------------------------------------------------------------------
# Dependency installs (#2099). `npm ci` and `uv sync` leave symlinks under
# node_modules/ and .venv/; an untracked install is not work and is skipped.
# --------------------------------------------------------------------------


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _link(root: Path, relative: str, target: str | Path) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def _base_reader(contents: dict[str, str]):
    return lambda path: contents[path].encode()


class TestUntrackedDependencyInstallsAreSkipped:
    def test_installs_beside_a_real_change_do_not_refuse_the_publication(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {
            "frontend/package.json": "{}\n",
            "kew/pyproject.toml": "[project]\n",
            "kew/src/app.py": "before\n",
        }
        for relative, content in base.items():
            _write(checkout, relative, content)
        _write(checkout, "kew/src/app.py", "after\n")
        _write(checkout, "frontend/node_modules/acorn/bin/acorn", "#!/bin/sh\n")
        _link(checkout, "frontend/node_modules/.bin/acorn", "../acorn/bin/acorn")
        _link(checkout, "kew/.venv/bin/python", "/usr/bin/python3")

        artifact = extract_on_host(
            checkout,
            tmp_path / "artifact",
            dict.fromkeys(base, "100644"),
            _base_reader(base),
            _OID,
        )

        assert [(e.path, artifact.content(e)) for e in artifact.entries] == [
            ("kew/src/app.py", b"after\n")
        ]
        assert artifact.deletions == ()
        assert artifact.omitted_dependency_directories == (
            "frontend/node_modules",
            "kew/.venv",
        )

    def test_a_nested_untracked_install_is_skipped(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "a/b/real.txt", "work\n")
        _link(checkout, "a/b/node_modules/x", "/etc/passwd")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["a/b/real.txt"]
        assert artifact.omitted_dependency_directories == ("a/b/node_modules",)

    def test_a_tracked_install_directory_is_walked_as_today(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {"vendor/node_modules/pkg/index.js": "module.exports = 1\n"}
        _write(checkout, "vendor/node_modules/pkg/index.js", "module.exports = 1\n")
        _write(checkout, "vendor/node_modules/pkg/added.js", "added\n")

        artifact = extract_on_host(
            checkout,
            tmp_path / "artifact",
            dict.fromkeys(base, "100644"),
            _base_reader(base),
            _OID,
        )
        assert [e.path for e in artifact.entries] == [
            "vendor/node_modules/pkg/added.js"
        ]
        assert artifact.omitted_dependency_directories == ()

        _link(checkout, "vendor/node_modules/.bin/pkg", "../pkg/index.js")
        with pytest.raises(PublicationRefused, match="120000"):
            extract_on_host(
                checkout,
                tmp_path / "artifact-2",
                dict.fromkeys(base, "100644"),
                _base_reader(base),
                _OID,
            )

    def test_a_symlink_named_node_modules_is_refused_not_followed(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "real.txt", "work\n")
        elsewhere = tmp_path / "elsewhere"
        _write(elsewhere, "secret.txt", "outside the checkout\n")
        _link(checkout, "frontend/node_modules", elsewhere)

        with pytest.raises(PublicationRefused) as refused:
            extract_on_host(checkout, tmp_path / "artifact", {}, _base_reader({}), _OID)
        assert [r.path for r in refused.value.rejections] == ["frontend/node_modules"]
        assert "120000" in refused.value.rejections[0].detail

    def test_a_regular_file_named_venv_is_ordinary_work(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "tools/.venv", "not a directory\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["tools/.venv"]
        assert artifact.omitted_dependency_directories == ()


# --------------------------------------------------------------------------
# Base ignore rules (#2108). `npm run check` and `mkdocs build` leave
# generated output the base's .gitignore files already name; only the BASE
# commit's rules decide, and only for untracked paths.
# --------------------------------------------------------------------------


def _extract_with_base(
    tmp_path: Path, checkout: Path, base: dict[str, str], name: str = "artifact"
):
    for relative, content in base.items():
        if not (checkout / relative).exists():
            _write(checkout, relative, content)
    return extract_on_host(
        checkout,
        tmp_path / name,
        dict.fromkeys(base, "100644"),
        _base_reader(base),
        _OID,
    )


class TestBaseIgnoreRulesDropUntrackedOutput:
    def test_generated_output_is_left_out_and_named(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {
            ".gitignore": "/site/\n.kew/docs/\n",
            "frontend/.gitignore": ".svelte-kit\n",
            "frontend/src/app.ts": "before\n",
        }
        for relative, content in base.items():
            _write(checkout, relative, content)
        _write(checkout, "frontend/src/app.ts", "after\n")
        _write(checkout, "frontend/.svelte-kit/ambient.d.ts", "declare const X\n")
        _write(checkout, "site/index.html", "<html></html>\n")
        _write(checkout, ".kew/docs/note.md", "a note\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [(e.path, artifact.content(e)) for e in artifact.entries] == [
            ("frontend/src/app.ts", b"after\n")
        ]
        assert artifact.deletions == ()
        assert artifact.omitted_ignored_paths == (
            ".kew/docs",
            "frontend/.svelte-kit",
            "site",
        )

    def test_individual_ignored_files_are_dropped_and_named(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "*.log\n"}
        _write(checkout, "src/debug.log", "noise\n")
        _write(checkout, "src/real.py", "work\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["src/real.py"]
        assert artifact.omitted_ignored_paths == ("src/debug.log",)

    def test_a_tracked_file_under_an_ignored_pattern_still_publishes(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {
            ".gitignore": "build/\n*.lock\n",
            "build/keep.txt": "before\n",
            "deps.lock": "v1\n",
        }
        for relative, content in base.items():
            _write(checkout, relative, content)
        _write(checkout, "build/keep.txt", "after\n")
        _write(checkout, "deps.lock", "v2\n")
        # Untracked, beneath the ignored-but-tracked directory: git ignores it.
        _write(checkout, "build/new.txt", "generated\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [(e.path, artifact.content(e)) for e in artifact.entries] == [
            ("build/keep.txt", b"after\n"),
            ("deps.lock", b"v2\n"),
        ]
        assert artifact.omitted_ignored_paths == ("build/new.txt",)

    def test_a_tracked_file_deleted_under_an_ignored_pattern_is_a_deletion(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "build/\n", "build/keep.txt": "x\n"}
        _write(checkout, ".gitignore", base[".gitignore"])
        (checkout / "build").mkdir()

        artifact = extract_on_host(
            checkout,
            tmp_path / "artifact",
            dict.fromkeys(base, "100644"),
            _base_reader(base),
            _OID,
        )

        assert artifact.entries == ()
        assert artifact.deletions == ("build/keep.txt",)
        assert artifact.omitted_ignored_paths == ()

    def test_rules_come_from_the_base_not_the_checkout(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "*.log\n"}
        _write(checkout, ".gitignore", "*.log\nfeature.py\nsecret/\n")
        _write(checkout, "feature.py", "real work\n")
        _write(checkout, "secret/x.txt", "also work\n")
        # A .gitignore the agent planted in a new directory is data too.
        _write(checkout, "gen/.gitignore", "*\n")
        _write(checkout, "gen/out.txt", "work\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert {e.path for e in artifact.entries} == {
            ".gitignore",
            "feature.py",
            "gen/.gitignore",
            "gen/out.txt",
            "secret/x.txt",
        }
        assert artifact.omitted_ignored_paths == ()

    def test_an_agent_edit_cannot_unignore_a_base_rule(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "site/\n"}
        _write(checkout, ".gitignore", "site/\n!site/\n")
        _write(checkout, "site/index.html", "<html></html>\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == [".gitignore"]
        assert artifact.omitted_ignored_paths == ("site",)

    def test_negation_re_includes_a_file(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "dir/*\n!dir/keep.txt\n"}
        _write(checkout, "dir/keep.txt", "kept\n")
        _write(checkout, "dir/other.txt", "dropped\n")
        _write(checkout, "dir/sub/deep.txt", "dropped with its directory\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["dir/keep.txt"]
        assert artifact.omitted_ignored_paths == ("dir/other.txt", "dir/sub")

    def test_a_file_in_an_excluded_directory_cannot_be_re_included(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {
            ".gitignore": "gen/\n!gen/keep.txt\nout/\n",
            # Tracked, so out/ is walked; its negation still cannot re-include
            # a file whose parent directory the root rule excludes.
            "out/.gitignore": "!keep.txt\n",
            "other/.gitignore": "*\n!*.md\n",
        }
        _write(checkout, "gen/keep.txt", "dropped\n")
        _write(checkout, "out/keep.txt", "dropped\n")
        _write(checkout, "other/readme.md", "kept\n")
        _write(checkout, "other/x.txt", "dropped\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["other/readme.md"]
        assert artifact.omitted_ignored_paths == (
            "gen",
            "other/x.txt",
            "out/keep.txt",
        )

    def test_nested_rules_are_relative_and_deeper_ones_win(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {
            ".gitignore": "*.gen\n/top-only.txt\n",
            "pkg/.gitignore": "!keep.gen\n/local.txt\ndocs/*.html\n",
        }
        _write(checkout, "a.gen", "dropped\n")
        _write(checkout, "pkg/keep.gen", "kept by the deeper negation\n")
        _write(checkout, "pkg/drop.gen", "dropped\n")
        _write(checkout, "top-only.txt", "dropped: anchored to the root\n")
        _write(checkout, "pkg/top-only.txt", "kept: the anchor is the root\n")
        _write(checkout, "pkg/local.txt", "dropped: anchored to pkg/\n")
        _write(checkout, "local.txt", "kept: pkg's rule does not reach up\n")
        _write(checkout, "pkg/docs/index.html", "dropped\n")
        _write(checkout, "pkg/sub/docs/index.html", "kept: slash pattern anchors\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == [
            "local.txt",
            "pkg/keep.gen",
            "pkg/sub/docs/index.html",
            "pkg/top-only.txt",
        ]
        assert artifact.omitted_ignored_paths == (
            "a.gen",
            "pkg/docs/index.html",
            "pkg/drop.gen",
            "pkg/local.txt",
            "top-only.txt",
        )

    def test_directory_only_and_double_star_patterns(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "cache/\n**/tmp/**\nlogs/**/*.log\n"}
        _write(checkout, "cache", "a FILE named cache is not matched by cache/\n")
        _write(checkout, "x/cache/data.bin", "dropped\n")
        _write(checkout, "a/b/tmp/t.txt", "dropped\n")
        _write(checkout, "logs/a.log", "dropped: ** matches zero directories\n")
        _write(checkout, "logs/y/z/b.log", "dropped\n")
        _write(checkout, "logs/y/keep.txt", "kept\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["cache", "logs/y/keep.txt"]
        assert artifact.omitted_ignored_paths == (
            "a/b/tmp/t.txt",
            "logs/a.log",
            "logs/y/z/b.log",
            "x/cache",
        )

    def test_a_symlink_inside_an_ignored_directory_does_not_refuse(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": ".svelte-kit/\n"}
        _write(checkout, "real.txt", "work\n")
        _link(checkout, "web/.svelte-kit/output/link", "/etc/passwd")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["real.txt"]
        assert artifact.omitted_ignored_paths == ("web/.svelte-kit",)

    def test_an_ignored_symlink_file_does_not_refuse(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "*.lnk\n"}
        _link(checkout, "a.lnk", "/etc/passwd")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert artifact.entries == ()
        assert artifact.omitted_ignored_paths == ("a.lnk",)

    def test_a_symlink_that_is_not_ignored_still_refuses(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "site/\n"}
        _write(checkout, "real.txt", "work\n")
        _link(checkout, "escape", "/etc/passwd")

        with pytest.raises(PublicationRefused) as refused:
            _extract_with_base(tmp_path, checkout, base)
        assert [r.path for r in refused.value.rejections] == ["escape"]

    def test_omissions_are_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {".gitignore": "site/\n"}
        _write(checkout, "site/index.html", "<html></html>\n")

        with caplog.at_level("INFO", logger="kew.publication_extract"):
            _extract_with_base(tmp_path, checkout, base)

        assert "left out 1 untracked path the base .gitignore rules ignore: 'site'" in (
            caplog.text
        )

    def test_an_oversize_base_gitignore_is_not_read(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, ".gitignore", "*.log\n")
        _write(checkout, "out.log", "published\n")

        def refuse_read(path: str) -> bytes:
            raise AssertionError(f"read {path}")

        artifact = extract_on_host(
            checkout,
            tmp_path / "artifact",
            {".gitignore": "100644"},
            refuse_read,
            _OID,
            base_digests={".gitignore": OVERSIZE_BASE_BLOB},
        )
        assert [e.path for e in artifact.entries] == [".gitignore", "out.log"]
        assert artifact.omitted_ignored_paths == ()

    def test_an_unreadable_base_gitignore_fails_extraction(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, ".gitignore", "*.log\n")

        def broken(path: str) -> bytes:
            raise OSError("object store gone")

        with pytest.raises(ExtractionFailed, match="base ignore file .gitignore"):
            extract_on_host(
                checkout,
                tmp_path / "artifact",
                {".gitignore": "100644"},
                broken,
                _OID,
            )

    def test_a_symlink_to_a_directory_is_not_matched_as_a_directory(
        self, tmp_path: Path
    ) -> None:
        # git uses lstat: a symlink is never a directory, so `site/` does not
        # match a link named site, and the link refuses as before.
        checkout = tmp_path / "checkout"
        base = {".gitignore": "site/\n"}
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _link(checkout, "site", elsewhere)

        with pytest.raises(PublicationRefused) as refused:
            _extract_with_base(tmp_path, checkout, base)
        assert [r.path for r in refused.value.rejections] == ["site"]


# --------------------------------------------------------------------------
# Nested worktrees and clones (#2201). An agent that runs `git worktree add`
# or `git clone` inside the workspace leaves another repository's tree there;
# it is not this run's work, and its symlinks refused every publication.
# --------------------------------------------------------------------------


def _clone_admin_directory(root: Path) -> Path:
    """Write the admin layout a real clone has, which is what git looks for."""
    admin = root / ".git"
    (admin / "objects").mkdir(parents=True)
    (admin / "refs" / "heads").mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/main\n")
    return admin


class TestNestedRepositoriesAreSkipped:
    def test_a_nested_worktree_does_not_refuse_the_publication(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        base = {"src/app.py": "before\n"}
        _write(checkout, "src/app.py", "after\n")
        # `git worktree add .wt-main` writes a .git FILE naming the admin dir.
        _write(checkout, ".wt-main/.git", "gitdir: /elsewhere/.git/worktrees/main\n")
        _write(checkout, ".wt-main/src/app.py", "another checkout\n")
        _link(checkout, ".wt-main/.venv-link", "/usr/bin/python3")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [(e.path, artifact.content(e)) for e in artifact.entries] == [
            ("src/app.py", b"after\n")
        ]
        assert artifact.deletions == ()
        assert artifact.omitted_nested_repositories == (".wt-main",)

    def test_a_nested_clone_with_a_git_directory_is_skipped(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "real.txt", "work\n")
        _clone_admin_directory(checkout / "vendor/other")
        _write(checkout, "vendor/other/README.md", "someone else's repo\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["real.txt"]
        assert artifact.omitted_nested_repositories == ("vendor/other",)

    def test_a_linked_worktree_admin_directory_is_skipped(self, tmp_path: Path) -> None:
        # `--separate-git-dir` and a linked worktree keep objects and refs in
        # the main repository, named by `commondir` — git opens it regardless.
        checkout = tmp_path / "checkout"
        _write(checkout, "real.txt", "work\n")
        _write(checkout, "nested/.git/HEAD", "ref: refs/heads/main\n")
        _write(checkout, "nested/.git/commondir", "../..\n")
        _write(checkout, "nested/carried.txt", "someone else's work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["real.txt"]
        assert artifact.omitted_nested_repositories == ("nested",)

    def test_the_checkout_root_is_never_skipped_for_its_own_git(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, ".git/HEAD", "ref: refs/heads/main\n")
        _write(checkout, "real.txt", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["real.txt"]
        assert artifact.omitted_nested_repositories == ()

    def test_a_directory_the_base_tracks_in_is_walked_as_today(
        self, tmp_path: Path
    ) -> None:
        # A tracked path is never left out, whatever it holds: dropping it
        # would publish a deletion the agent never made.
        checkout = tmp_path / "checkout"
        base = {"fixtures/repo/kept.txt": "kept\n"}
        _write(checkout, "fixtures/repo/.git", "gitdir: /elsewhere\n")
        _write(checkout, "fixtures/repo/added.txt", "added\n")

        artifact = _extract_with_base(tmp_path, checkout, base)

        assert [e.path for e in artifact.entries] == ["fixtures/repo/added.txt"]
        assert artifact.deletions == ()
        assert artifact.omitted_nested_repositories == ()

    def test_the_skipped_directory_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "real.txt", "work\n")
        _write(checkout, ".wt-main/.git", "gitdir: /elsewhere\n")

        with caplog.at_level("INFO", logger="kew.publication_extract"):
            extract_on_host(checkout, tmp_path / "artifact", {}, _base_reader({}), _OID)

        assert (
            "skipped 1 nested git worktree or repository directory: '.wt-main'"
            in caplog.text
        )


@_REQUIRES_GIT
def test_the_layouts_git_itself_writes_are_the_ones_the_skip_detects(
    tmp_path: Path,
) -> None:
    """Live: every fixture above is hand-written, and that is the risk.

    The skip is git's definition of a repository, so what `git worktree add`
    and `git clone` actually leave behind has to be what the walk steps over
    — and the run's own change beside them has to still publish.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main", ".")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "app.py").write_text("before\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "app.py").write_text("after\n")
    _git(checkout, "worktree", "add", "-q", "wt-main")
    _git(checkout, "clone", "-q", ".", "vendor")

    artifact = extract_on_host(
        checkout,
        tmp_path / "artifact",
        {"app.py": "100644"},
        _base_reader({"app.py": "before\n"}),
        base,
    )

    assert (checkout / "wt-main" / ".git").is_file()  # a gitfile
    assert (checkout / "vendor" / ".git").is_dir()  # an admin directory
    assert [(e.path, artifact.content(e)) for e in artifact.entries] == [
        ("app.py", b"after\n")
    ]
    assert artifact.deletions == ()
    assert artifact.omitted_nested_repositories == ("vendor", "wt-main")


class TestAStrayGitEntryIsNotANestedRepository:
    """The skip is git's definition of a repository, not the name ``.git``.

    A directory holding a stray or dangling entry by that name is ordinary
    work: the base ref published it and the contained producer still does, so
    the host walker dropping it silently would be a divergence, not a skip.
    """

    def test_a_file_named_git_that_is_not_a_gitfile_still_publishes(
        self, tmp_path: Path
    ) -> None:
        checkout = tmp_path / "checkout"
        _write(checkout, "fixtures/sample/.git", "a fixture, not a repository\n")
        _write(checkout, "fixtures/sample/data.txt", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["fixtures/sample/data.txt"]
        assert artifact.omitted_nested_repositories == ()

    def test_a_git_directory_without_object_storage_still_publishes(
        self, tmp_path: Path
    ) -> None:
        # A half-written or hand-made `.git`: git calls this no repository.
        checkout = tmp_path / "checkout"
        _write(checkout, "tool/.git/HEAD", "ref: refs/heads/main\n")
        _write(checkout, "tool/main.py", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["tool/main.py"]
        assert artifact.omitted_nested_repositories == ()

    def test_an_empty_directory_named_git_still_publishes(self, tmp_path: Path) -> None:
        checkout = tmp_path / "checkout"
        (checkout / "pkg" / ".git").mkdir(parents=True)
        _write(checkout, "pkg/mod.py", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["pkg/mod.py"]
        assert artifact.omitted_nested_repositories == ()

    def test_a_symlink_named_git_is_not_resolved_and_does_not_skip(
        self, tmp_path: Path
    ) -> None:
        # The walk follows nothing, so it cannot know what this points at; the
        # name itself is skipped as it always has been, and nothing refuses.
        checkout = tmp_path / "checkout"
        _link(checkout, "pkg/.git", "/elsewhere/.git")
        _write(checkout, "pkg/mod.py", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["pkg/mod.py"]
        assert artifact.omitted_nested_repositories == ()

    @pytest.mark.skipif(
        os.getuid() == 0, reason="root reads a directory whatever its mode says"
    )
    def test_an_unreadable_git_directory_does_not_refuse_the_publication(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Unreadable is not evidence either way, and the walk never publishes
        # the name: refusing here would lose the work beside it. It is the one
        # answer reached without reading, so it is said out loud.
        checkout = tmp_path / "checkout"
        _write(checkout, "pkg/mod.py", "work\n")
        sealed = checkout / "pkg" / ".git"
        sealed.mkdir()
        sealed.chmod(0o000)
        try:
            with caplog.at_level("WARNING", logger="kew.publication_extract"):
                artifact = extract_on_host(
                    checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
                )
        finally:
            sealed.chmod(0o700)

        assert [e.path for e in artifact.entries] == ["pkg/mod.py"]
        assert artifact.omitted_nested_repositories == ()
        assert "could not read" in caplog.text
        assert "walked as ordinary work" in caplog.text

    def test_a_huge_file_named_git_is_not_read_and_does_not_refuse(
        self, tmp_path: Path
    ) -> None:
        # Reading it to decide would let a file the walk otherwise skips
        # refuse the whole publication for its size.
        checkout = tmp_path / "checkout"
        _write(
            checkout,
            "pkg/.git",
            "gitdir: " + "x" * (_GIT_FILE_MAX_BYTES + 1),
        )
        _write(checkout, "pkg/mod.py", "work\n")

        artifact = extract_on_host(
            checkout, tmp_path / "artifact", {}, _base_reader({}), _OID
        )

        assert [e.path for e in artifact.entries] == ["pkg/mod.py"]
        assert artifact.omitted_nested_repositories == ()


# --------------------------------------------------------------------------
# Live. Everything above is a claim about a list of strings.
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def _tools(monkeypatch: pytest.MonkeyPatch):
    for tool in ("docker", "git"):
        resolved = shutil.which(tool)
        if resolved:
            monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, resolved)


@pytest.fixture
def agent_work(tmp_path: Path) -> tuple[Path, str, str, Path]:
    """A checkout with real work, and a canonical bare repo holding only base."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", ".")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    (checkout / "keep.txt").write_text("keep\n")
    (checkout / "edit.txt").write_text("before\n")
    (checkout / "gone.txt").write_text("doomed\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")

    (checkout / "edit.txt").write_text("after\n")
    (checkout / "added.txt").write_text("added\n")
    (checkout / "gone.txt").unlink()
    script = checkout / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "work")
    head_tree = _git(checkout, "rev-parse", "HEAD^{tree}")

    canonical = tmp_path / "canonical.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(canonical)], check=True, capture_output=True
    )
    _git(checkout, "push", "-q", str(canonical), f"{base}:refs/heads/base")
    return checkout, base, head_tree, canonical


@_REQUIRES_IMAGE
class TestTheWholeChainAgainstARealContainer:
    def test_the_tree_it_builds_is_the_tree_git_had(
        self, tmp_path: Path, agent_work, _tools
    ):
        checkout, base, head_tree, canonical = agent_work

        artifact = extract_in_container(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=tmp_path / "artifact",
            slug="live",
            base_sha=base,
            proxy_environment="none",
        )
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=())
        tree = apply_to_tree(
            canonical,
            base_sha=artifact.base_sha,
            approved=verdict.approved(),
            deletions=verdict.deletions(),
            content=artifact.content,
            index_file=tmp_path / "scratch.index",
        )

        assert artifact.deletions == ("gone.txt",)
        assert {e.path: e.mode for e in artifact.entries} == {
            "added.txt": "100644",
            "edit.txt": "100644",
            "run.sh": "100755",
        }
        assert tree == head_tree

    def test_the_artifact_belongs_to_the_host(self, tmp_path: Path, agent_work, _tools):
        # Written by the container, read by the host. If it landed owned by
        # anyone else the host would need a directory loose enough to read —
        # which is the thing --user avoids.
        checkout, base, _, _ = agent_work
        artifact_dir = tmp_path / "artifact"

        extract_in_container(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=artifact_dir,
            slug="live",
            base_sha=base,
            proxy_environment="none",
        )

        assert (artifact_dir / "manifest.json").stat().st_uid == os.getuid()
        assert artifact_dir.stat().st_mode & 0o777 == 0o700

    def test_the_container_cannot_write_to_the_checkout(
        self, tmp_path: Path, agent_work, _tools
    ):
        """`:ro` is a claim until something tries to write through it."""
        checkout, _, _, _ = agent_work
        argv = build_extract_argv(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=tmp_path,
            slug="probe",
            base_sha=_OID,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        # Same argv, different entrypoint: the containment under test is the
        # flags, so the probe must run under exactly them.
        at = argv.index("--entrypoint")
        probe = [*argv[:at], "--entrypoint", "/bin/sh", *argv[at + 2 : -2]]
        result = subprocess.run(  # nosec B603
            [*probe, "-c", "touch /work/PWNED"], capture_output=True, text=True
        )

        assert result.returncode != 0
        assert "read-only" in result.stderr.lower()
        assert not (checkout / "PWNED").exists()

    def test_a_host_credential_does_not_reach_the_container(
        self, tmp_path: Path, agent_work, _tools, monkeypatch: pytest.MonkeyPatch
    ):
        checkout, _, _, _ = agent_work
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sentinel-must-not-appear")
        argv = build_extract_argv(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=tmp_path,
            slug="probe",
            base_sha=_OID,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        at = argv.index("--entrypoint")
        probe = [*argv[:at], "--entrypoint", "/bin/sh", *argv[at + 2 : -2]]
        result = subprocess.run(  # nosec B603
            [*probe, "-c", "env"], capture_output=True, text=True
        )

        assert "sentinel-must-not-appear" not in result.stdout

    def test_a_re_run_into_the_same_directory_is_refused(
        self, tmp_path: Path, agent_work, _tools
    ):
        """The producer's non-empty guard, end to end.

        Stale blobs the new manifest does not reference make an artifact the
        host refuses as unreferenced content — a failure a long way from its
        cause, so it is caught at the cause.
        """
        checkout, base, _, _ = agent_work
        artifact_dir = tmp_path / "artifact"
        extract_in_container(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=artifact_dir,
            slug="live",
            base_sha=base,
            proxy_environment="none",
        )

        with pytest.raises(FileExistsError):
            extract_in_container(
                runtime="docker",
                image=_IMAGE,
                checkout_dir=checkout,
                artifact_dir=artifact_dir,
                slug="live",
                base_sha=base,
                proxy_environment="none",
            )

    def test_a_base_the_checkout_does_not_have_says_so(
        self, tmp_path: Path, agent_work, _tools
    ):
        checkout, _, _, _ = agent_work

        with pytest.raises(ExtractionFailed, match="base commit is not in"):
            extract_in_container(
                runtime="docker",
                image=_IMAGE,
                checkout_dir=checkout,
                artifact_dir=tmp_path / "artifact",
                slug="live",
                base_sha="0" * 40,
                proxy_environment="none",
            )

    def test_the_artifact_the_container_wrote_verifies_on_the_host(
        self, tmp_path: Path, agent_work, _tools
    ):
        # extract_in_container reads through read_artifact already; this reads it
        # a second time, standalone, so a digest that only verified because the
        # same process wrote it would show up.
        checkout, base, _, _ = agent_work
        artifact_dir = tmp_path / "artifact"
        extract_in_container(
            runtime="docker",
            image=_IMAGE,
            checkout_dir=checkout,
            artifact_dir=artifact_dir,
            slug="live",
            base_sha=base,
            proxy_environment="none",
        )

        again = read_artifact(artifact_dir)
        assert again.base_sha == base


def test_extraction_blanks_automatic_client_proxy_environment(tmp_path):
    argv = _argv(tmp_path)
    env = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        assert f"{name}=" in env
        assert f"{name.lower()}=" in env
