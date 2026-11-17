"""The agent-image CVE gate, and the invariants that hold for EVERY agent image.

Two gaps the round-1 adversarial review on PR #1790 found (Minor 8):

* ``test_agent_image.py`` reads only ``docker/agent.Dockerfile``. Deleting the
  base digest from ``docker/agent-opencode.Dockerfile`` failed no test, even
  though that image is what the ``simple`` routing tier dispatches into.
* ``scripts/scan-agent-image.sh`` had no test at all. Its refusals — an
  unpinned scanner, an empty vulnerability database, a scan that read nothing —
  are the guards that stop the gate passing while proving nothing, and a guard
  with no test reports coverage that does not exist.

The script tests run it for real against fake ``docker`` and ``trivy`` shims on
PATH, rather than grepping its source: a static assertion that the string
``exit 1`` appears would survive the branch never being reached.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from support import source_ast
from support.scan_checkout import (
    host_trivy_pin_key,
    pin_stub_trivy,
    pin_trivy_binaries,
    plant_scan_inputs,
)

from kew.hosts import pin_path, pinned_trivy_version, read_pins

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCAN_SCRIPT = _REPO_ROOT / "scripts" / "scan-agent-image.sh"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_AGENT_DOCKERFILES = (
    _REPO_ROOT / "docker" / "agent.Dockerfile",
    _REPO_ROOT / "docker" / "agent-opencode.Dockerfile",
    _REPO_ROOT / "docker" / "agent-codex.Dockerfile",
)
_SWITCHYARD_DOCKERFILE = _REPO_ROOT / "docker" / "switchyard.Dockerfile"
_SWITCHYARD_REPOSITORY = "https://github.com/NVIDIA-NeMo/Switchyard"
_SWITCHYARD_REVISION = "46331725daebda02b83781f63bb9c94db9059a1e"
_SWITCHYARD_TREE = "41f84a3f62124058200c3603e999b1342b87019c"
_SWITCHYARD_LOCKFILE_SHA256 = (
    "2e561fbd0cb752a12bed92e96ad817a0ab288365dcc7619b704f16400bdd4538"
)
_SWITCHYARD_VERSION = "0.3.0"
_SWITCHYARD_TOOLCHAIN = "1.99.1"
_SWITCHYARD_BUILDER = (
    "rust:1.99.1-slim-bookworm@sha256:"
    "f2c004e3d2d8b547e8cf7ffbc29205385f4e71d23e53e4e383065db5e9f822cd"
)

_CREDENTIAL_NAME_RE = re.compile(
    r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE
)

# The pinned scanner, read from the one file the gate, the provisioning recipe
# and CI all read it from (#2462), so no test here can drift from it.
_TRIVY_PIN = pin_path()
_PINNED_TRIVY = pinned_trivy_version()


def _join_continuations(text: str) -> str:
    return re.sub(r"\\\n\s*", " ", text)


def _docker_copy_inputs(text: str) -> set[str]:
    """Return repo-local docker inputs named by COPY instructions."""
    copied: set[str] = set()
    for line in _join_continuations(text).splitlines():
        match = re.match(r"^COPY\b(.*)$", line.strip(), re.IGNORECASE)
        if match is None:
            continue
        argument = match.group(1).strip()
        bracket = argument.find("[")
        if bracket >= 0:
            values = json.loads(argument[bracket:])
            copied.update(
                value
                for value in values[:-1]
                if isinstance(value, str) and value.startswith("docker/")
            )
        else:
            copied.update(
                token for token in argument.split() if token.startswith("docker/")
            )
    return copied


def _assert_overlay_verifier_is_enforced(dockerfile: Path) -> None:
    """Assert that a verifier failure stops the Docker build."""
    content = dockerfile.read_text()
    assert "docker/verify-npm-overlay.js" in content, (
        f"{dockerfile.name} must COPY the overlay verifier"
    )
    run_lines = [
        line
        for line in _join_continuations(content).splitlines()
        if re.match(r"^\s*RUN\b", line)
    ]
    invocation = re.compile(r"\bnode\s+\S*kew-verify-npm-overlay(?=\s|[;&|]|$)")
    matches = [
        (line, match)
        for line in run_lines
        if (match := invocation.search(line)) is not None
    ]
    for line, match in matches:
        after = line[match.end() :].lstrip()
        control_operators = re.findall(r"\|\||&&|[;|&]", after)
        safely_chained = not after or (
            after.startswith("&&")
            and all(operator == "&&" for operator in control_operators)
        )
        assert safely_chained, (
            f"{dockerfile.name} does not propagate the overlay verifier's "
            "failure — its invocation must end the RUN or use only && afterward"
        )
    assert matches, (
        f"{dockerfile.name} copies the overlay verifier but never RUNs it — "
        "a verifier that is not invoked verifies nothing"
    )


def _overlay_packages(dockerfile: Path) -> tuple[dict[str, str], set[str]]:
    """Return packages staged by npm and copied into npm's vendored tree."""
    content = _join_continuations(dockerfile.read_text())
    overlay = re.search(
        r"npm\s+install\s+--prefix\s+/tmp/npmfix\b(?P<install>.*?)"
        r"\s+&&\s+for\s+pkg\s+in\s+(?P<copied>.*?);\s*do\b",
        content,
    )
    assert overlay is not None, f"{dockerfile.name} has no npm overlay install loop"

    installed: dict[str, str] = {}
    for token in overlay.group("install").split():
        if token.startswith("-"):
            continue
        name, separator, version = token.rpartition("@")
        assert separator and name, (
            f"{dockerfile.name}: overlay package {token!r} has no exact version"
        )
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version), (
            f"{dockerfile.name}: overlay package {token!r} must use an exact version"
        )
        assert name not in installed, (
            f"{dockerfile.name}: overlay package {name!r} is staged more than once"
        )
        installed[name] = version

    return installed, set(overlay.group("copied").split())


@pytest.mark.parametrize("dockerfile", _AGENT_DOCKERFILES, ids=lambda p: p.name)
class TestEveryAgentDockerfile:
    """Invariants for every image kew dispatches an agent into.

    Parameterized over BOTH agent Dockerfiles. The claude-specific shape tests
    stay in test_agent_image.py; these are the ones whose absence from the
    opencode image would be a hole, not a difference.
    """

    def test_base_image_pinned_by_digest(self, dockerfile: Path) -> None:
        content = _join_continuations(dockerfile.read_text())
        stage_names = {
            m.lower() for m in re.findall(r"\bAS\s+(\S+)", content, re.IGNORECASE)
        }
        froms = re.findall(r"^\s*FROM\s+(\S+)", content, re.MULTILINE | re.IGNORECASE)
        external = [
            f for f in froms if f.lower() != "scratch" and f.lower() not in stage_names
        ]
        assert external, f"no external base image FROM in {dockerfile.name}"
        for ref in external:
            assert re.search(r"@sha256:[0-9a-f]{64}$", ref), (
                f"{dockerfile.name}: FROM must be pinned by a full 64-char "
                f"digest, got {ref!r}"
            )

    def test_every_image_shares_one_base_digest(self, dockerfile: Path) -> None:
        # They are built from the same base on purpose. Advancing one pin and
        # not the others silently gives the lanes different security posture.
        digests = set()
        for path in _AGENT_DOCKERFILES:
            digests.update(re.findall(r"@(sha256:[0-9a-f]{64})", path.read_text()))
        assert len(digests) == 1, (
            f"the agent Dockerfiles pin different base digests: {sorted(digests)}. "
            "Advance them together."
        )

    def test_runs_as_a_literal_nonroot_user(self, dockerfile: Path) -> None:
        content = _join_continuations(dockerfile.read_text())
        users = re.findall(r"^\s*USER\s+(\S+)", content, re.MULTILINE)
        assert users, f"no USER directive in {dockerfile.name}"
        name = users[-1].split(":", 1)[0]
        assert "$" not in name and name not in ("root", "0"), (
            f"{dockerfile.name}: final USER must be a literal non-root user, "
            f"got {users[-1]!r}"
        )

    def test_no_credential_baked_into_arg_or_env(self, dockerfile: Path) -> None:
        content = _join_continuations(dockerfile.read_text())
        names: list[str] = []
        for match in re.finditer(r"^\s*(?:ARG|ENV)\s+(.+)$", content, re.MULTILINE):
            body = match.group(1)
            if "=" in body:
                names.extend(t.split("=", 1)[0] for t in body.split() if "=" in t)
            else:
                names.append(body.split()[0])
        for name in names:
            assert not _CREDENTIAL_NAME_RE.search(name), (
                f"{dockerfile.name} bakes a credential-named ARG/ENV: {name!r}"
            )

    def test_npm_is_pinned_not_latest(self, dockerfile: Path) -> None:
        # #1432 round-1 review, Major 5: `npm install -g npm@latest` lets an
        # unreviewed npm into the containment boundary on any rebuild, with no
        # repository change to review and no CVE for Trivy to catch.
        content = _join_continuations(dockerfile.read_text())
        globals_installed = re.findall(r"npm\s+install\s+-g\s+(\S+)", content)
        npm_pins = [p for p in globals_installed if p.startswith("npm@")]
        assert npm_pins, f"{dockerfile.name} does not install a pinned npm"
        for pin in npm_pins:
            assert not pin.endswith("@latest"), (
                f"{dockerfile.name} installs {pin} — pin npm to an exact "
                "reviewed version; @latest is an unreviewed supply-chain input"
            )
            assert re.match(r"^npm@\d+\.\d+\.\d+$", pin), (
                f"{dockerfile.name}: npm must be pinned to an exact "
                f"version, got {pin!r}"
            )

    def test_overlay_is_verified_at_build_time(self, dockerfile: Path) -> None:
        # The overlay replaces packages inside npm's vendored tree and relies on
        # the surviving siblings satisfying their ranges. The verifier turns
        # that reliance into a build failure instead of a broken dispatch.
        # Assert the INVOCATION, not the string: the COPY line above also
        # contains the destination path, so a substring check passes even when
        # nothing ever runs it. (Mutation R8 survived exactly that test.)
        # Its failure must reach the build. A semicolon or OR list can swallow
        # the status even when a later command succeeds.
        _assert_overlay_verifier_is_enforced(dockerfile)

    def test_overlay_packages_have_exact_versions(self, dockerfile: Path) -> None:
        installed, _ = _overlay_packages(dockerfile)
        assert installed, f"{dockerfile.name} stages no npm overlay packages"

    def test_overlay_install_copy_and_verifier_sets_match(
        self, dockerfile: Path
    ) -> None:
        installed, copied = _overlay_packages(dockerfile)
        expected = _expected_versions()
        assert installed == expected, (
            f"{dockerfile.name}: staged overlay versions differ from verifier: "
            f"installed={installed}, expected={expected}"
        )
        assert copied == set(installed), (
            f"{dockerfile.name}: copied overlay packages differ from staged packages: "
            f"copied={sorted(copied)}, installed={sorted(installed)}"
        )


def test_overlay_verifier_followed_by_semicolon_true_is_rejected(
    tmp_path: Path,
) -> None:
    source = _AGENT_DOCKERFILES[0].read_text()
    unsafe = source.replace(
        "node /usr/local/bin/kew-verify-npm-overlay &&",
        "node /usr/local/bin/kew-verify-npm-overlay; true &&",
        1,
    )
    assert unsafe != source
    dockerfile = tmp_path / "agent.Dockerfile"
    dockerfile.write_text(unsafe)

    with pytest.raises(
        AssertionError, match="must end the RUN or use only && afterward"
    ):
        _assert_overlay_verifier_is_enforced(dockerfile)


# The ci.yml steps that run a pull request's code on the self-hosted runner.
_PR_CODE_STEPS = ("Test (pytest)", "Agent image CVE gate")
# ci.yml holds no secret at all, so the guard is a line rule over its raw text
# rather than a walk over the fields a secret usually sits in: a `run:` line,
# a function argument, container credentials, a quoted key or a flow-mapping
# `secrets: inherit` all reach a job some other way. Case-insensitively, a line
# is refused when it holds the word `secrets` anywhere, or `github.token`, an
# index into `github`, or the whole `github` context passed to a function
# (`toJSON(github)` includes the token). Two lines of today's ci.yml use the
# word legitimately and are allowed by their exact content: a comment naming
# "the `secrets` job" and that job's id. Any other spelling is refused.
_SECRETS_WORD = re.compile(r"(?<![a-z])secrets(?![a-z])", re.IGNORECASE)
_GITHUB_CREDENTIAL = re.compile(
    r"github\s*\.\s*token|github\s*\[|[(,]\s*github\s*[,)]", re.IGNORECASE
)
_ALLOWED_SECRETS_LINES = frozenset(
    {
        "      # without it, so it installs the `secrets` job's pinned, checksummed",
        "  secrets:",
    }
)


def _ci_workflow() -> dict:
    import yaml

    return yaml.safe_load(_CI_WORKFLOW.read_text())


def _named_ci_steps(workflow: dict, name: str) -> list[dict]:
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == name
    ]


def _secret_references(text: str) -> list[str]:
    """Every line of a workflow's raw *text* that could carry a credential."""
    return [
        line.strip()
        for line in text.splitlines()
        if (_SECRETS_WORD.search(line) and line not in _ALLOWED_SECRETS_LINES)
        or _GITHUB_CREDENTIAL.search(line)
    ]


def _insert_after(text: str, anchor: str, lines: str) -> str:
    assert text.count(anchor) == 1, anchor
    return text.replace(anchor, anchor + lines, 1)


def _assert_no_environment(step: dict) -> None:
    assert "env" not in step, f"{step.get('name')} maps {sorted(step['env'])}"


class TestScanScriptIsWiredIn:
    def test_script_exists_and_is_executable(self) -> None:
        assert _SCAN_SCRIPT.exists(), f"missing {_SCAN_SCRIPT}"
        assert _SCAN_SCRIPT.stat().st_mode & stat.S_IXUSR, (
            "scan-agent-image.sh must be executable"
        )

    def test_ci_job_runs_the_shared_script(self) -> None:
        # Parity with verify.sh is enforced by test_verify_parity; this pins the
        # other half — that the CI job exists and calls the script at all.
        content = _CI_WORKFLOW.read_text()
        assert "agent-image-scan:" in content, "ci.yml lost the agent-image-scan job"
        assert "bash scripts/scan-agent-image.sh" in content, (
            "the agent-image-scan job must run the shared gate script"
        )

    @pytest.mark.parametrize("step_name", _PR_CODE_STEPS)
    def test_the_steps_that_run_pr_code_map_no_environment(
        self, step_name: str
    ) -> None:
        """Both run a pull request's code on the self-hosted runner, so
        neither gets a mapped variable a test could read: the Switchyard
        build that once needed one now builds from public source."""
        (step,) = _named_ci_steps(_ci_workflow(), step_name)
        _assert_no_environment(step)

    def test_ci_yml_references_no_secret(self) -> None:
        assert _secret_references(_CI_WORKFLOW.read_text()) == []

    @pytest.mark.parametrize(
        ("anchor", "inserted"),
        [
            (
                "      - name: Test (pytest)\n",
                "        run: GH=${{ secrets.SOME_SECRET }} uv run pytest\n",
            ),
            (
                "      - name: Agent image CVE gate\n",
                "        env:\n          SOME_INPUT: ${{ secrets['SOME_SECRET'] }}\n",
            ),
            (
                "      - name: Test (pytest)\n",
                "        env:\n          ALL_SECRETS: ${{ toJSON(secrets) }}\n",
            ),
            (
                "  test:\n",
                "    container:\n      image: example/image\n"
                "      credentials:\n        username: someone\n"
                "        password: ${{ secrets.SOME_SECRET }}\n",
            ),
            ("  agent-image-scan:\n", "    secrets: inherit\n"),
            (
                "      - name: Agent image CVE gate\n",
                "        with:\n          token: ${{ github.token }}\n",
            ),
            (
                "      - name: Test (pytest)\n",
                "        env:\n          TOKEN: ${{ github['token'] }}\n",
            ),
            (
                "      - name: Test (pytest)\n",
                "        env:\n"
                "          VALUE: ${{ format('{0}', secrets.SOME_SECRET) }}\n",
            ),
            (
                "      - name: Agent image CVE gate\n",
                "        env:\n          CONTEXT: ${{ toJSON(github) }}\n",
            ),
            (
                "      - name: Test (pytest)\n",
                "        env:\n          VALUE: ${{ SECRETS.SOME_SECRET }}\n",
            ),
            (
                "      - name: Agent image CVE gate\n",
                "        with:\n          token: ${{ GITHUB.TOKEN }}\n",
            ),
            ("  agent-image-scan:\n", '    "secrets": inherit\n'),
            ("jobs:\n", "  extra: {uses: ./x.yml, secrets: inherit}\n"),
        ],
        ids=[
            "run-line",
            "index-expression",
            "whole-context",
            "container-credentials",
            "secrets-inherit",
            "github-token",
            "github-index",
            "format-call",
            "whole-github-context",
            "upper-case-secrets",
            "upper-case-github-token",
            "quoted-key",
            "flow-mapping-job",
        ],
    )
    def test_a_secret_put_back_into_ci_yml_is_caught(
        self, anchor: str, inserted: str
    ) -> None:
        mutated = _insert_after(_CI_WORKFLOW.read_text(), anchor, inserted)

        assert len(_secret_references(mutated)) == 1, inserted

    def test_gate_scans_every_agent_image(self) -> None:
        content = _SCAN_SCRIPT.read_text()
        for dockerfile in _AGENT_DOCKERFILES:
            rel = f"docker/{dockerfile.name}"
            assert rel in content, f"the gate does not scan {rel}"

    def test_gate_scans_the_switchyard_image(self) -> None:
        assert "docker/switchyard.Dockerfile" in _SCAN_SCRIPT.read_text()


def _switchyard_build_script() -> str:
    """The proxy Dockerfile with every line continuation joined by one space."""
    return re.sub(r"[ \t]*\\\n\s*", " ", _SWITCHYARD_DOCKERFILE.read_text())


# A command position: the start of a line, or just after `;`, `&&`, `|` or `(`.
_TOOLCHAIN_CALL_RE = re.compile(
    r"(?:^|[;&|(])\s*(cargo|rustc)(?![\w-])\s+(\S+)", re.MULTILINE
)


def _unpinned_toolchain_calls(script: str) -> list[str]:
    """Every cargo or rustc call that does not name the pinned toolchain.

    Upstream's ``rust-toolchain.toml`` names the ``1.99`` channel, so a plain
    ``cargo`` inside the checkout asks rustup to sync that channel and fails
    without the network. ``+1.99.1`` is the toolchain the builder image ships.
    """
    calls = [match.groups() for match in _TOOLCHAIN_CALL_RE.finditer(script)]
    assert calls, "no cargo or rustc call found; has the shape changed?"
    return [
        f"{tool} {argument}"
        for tool, argument in calls
        if argument != f"+{_SWITCHYARD_TOOLCHAIN}"
    ]


def test_proxy_source_is_fetched_by_the_full_pinned_commit() -> None:
    script = _switchyard_build_script()
    assert re.fullmatch(r"[0-9a-f]{40}", _SWITCHYARD_REVISION)
    assert (
        'git -C "$source_dir" fetch --quiet --depth 1 '
        f"{_SWITCHYARD_REPOSITORY} {_SWITCHYARD_REVISION} "
    ) in script
    # A branch or tag name moves; only the commit names one tree.
    assert "--branch" not in script
    assert "refs/tags" not in script
    assert "checkout --quiet FETCH_HEAD" in script


def test_proxy_source_commit_and_tree_are_checked_before_anything_reads_it() -> None:
    script = _switchyard_build_script()
    commit_check = (
        'test "$(git -C "$source_dir" rev-parse HEAD)" '
        f"= {_SWITCHYARD_REVISION} "
        "|| { echo 'switchyard source commit mismatch' >&2; exit 1; }"
    )
    tree_check = (
        'test "$(git -C "$source_dir" rev-parse \'HEAD^{tree}\')" '
        f"= {_SWITCHYARD_TREE} "
        "|| { echo 'switchyard source tree mismatch' >&2; exit 1; }"
    )
    assert commit_check in script
    assert tree_check in script
    first_read = script.index('"$source_dir/Cargo.lock"')
    assert script.index(commit_check) < first_read
    assert script.index(tree_check) < first_read
    assert script.index(tree_check) < script.index('cd "$source_dir"')


def test_proxy_source_checkout_is_checked_clean() -> None:
    script = _switchyard_build_script()
    clean_check = (
        'test -z "$(git -C "$source_dir" status --porcelain --untracked-files=all)" '
        "|| { echo 'switchyard source checkout is not clean' >&2; exit 1; }"
    )
    assert clean_check in script
    assert script.index(clean_check) < script.index('cd "$source_dir"')


def test_proxy_lockfile_digest_is_checked_before_the_build() -> None:
    script = _switchyard_build_script()
    lockfile_check = (
        f"printf '%s  %s\\n' {_SWITCHYARD_LOCKFILE_SHA256} "
        '"$source_dir/Cargo.lock" | sha256sum --check --status '
        "|| { echo 'switchyard source lockfile mismatch' >&2; exit 1; }"
    )
    assert lockfile_check in script
    build = f"cargo +{_SWITCHYARD_TOOLCHAIN} auditable build --locked"
    assert script.index(lockfile_check) < script.index(build)


def test_proxy_builds_the_pinned_package_version() -> None:
    script = _switchyard_build_script()
    assert (
        '.packages | any(.name == "switchyard-server" and '
        f'.version == "{_SWITCHYARD_VERSION}")'
    ) in script
    assert (
        f"cargo +{_SWITCHYARD_TOOLCHAIN} auditable build --locked --release "
        "--package switchyard-server"
    ) in script


def test_every_cargo_and_rustc_call_names_the_pinned_toolchain() -> None:
    assert _unpinned_toolchain_calls(_switchyard_build_script()) == []


def test_an_unpinned_cargo_call_is_caught() -> None:
    script = _switchyard_build_script()
    pinned = f"cargo +{_SWITCHYARD_TOOLCHAIN} auditable build"
    assert pinned in script
    unpinned = script.replace(pinned, "cargo auditable build", 1)
    assert _unpinned_toolchain_calls(unpinned) == ["cargo auditable"]


def test_proxy_reads_no_build_secret_or_private_mount() -> None:
    content = _SWITCHYARD_DOCKERFILE.read_text()
    assert "type=secret" not in content
    assert "/run/secrets" not in content


def test_proxy_image_labels_name_the_pinned_source() -> None:
    content = _SWITCHYARD_DOCKERFILE.read_text()
    for label in (
        f'org.opencontainers.image.source="{_SWITCHYARD_REPOSITORY}"',
        f'org.opencontainers.image.revision="{_SWITCHYARD_REVISION}"',
        f'org.opencontainers.image.version="{_SWITCHYARD_VERSION}"',
        f'org.kew.switchyard.lockfile-sha256="{_SWITCHYARD_LOCKFILE_SHA256}"',
        f'org.kew.switchyard.rust-toolchain="{_SWITCHYARD_TOOLCHAIN}"',
    ):
        assert label in content


def test_proxy_uses_the_verified_official_rust_builder() -> None:
    content = _SWITCHYARD_DOCKERFILE.read_text()

    assert f"FROM {_SWITCHYARD_BUILDER} AS builder" in content
    assert "rustup toolchain install" not in content
    assert (
        "sha256:b78cfbe4c0fd96b8a06b259092f1f412cb3ad3c5aed89d188bc40a1b8637f8f6"
        in content
    )
    assert (
        "sha256:392f221964669402bf4ae133d2a46c3ae270f64734133f4b2703d40019235c1a"
        in content
    )
    assert "ARG TARGETARCH" in content
    assert f"rustc +{_SWITCHYARD_TOOLCHAIN} -vV" in _join_continuations(content)


def test_proxy_verifies_and_invokes_the_cargo_wrapper_correctly() -> None:
    content = _switchyard_build_script()
    cargo = f"cargo +{_SWITCHYARD_TOOLCHAIN}"

    assert "9dc5ff28ff204b73eae91bd96cde4413fdcb70e0" in content
    assert f"{cargo} metadata --locked --no-deps" in content
    assert (
        f"{cargo} install --locked --path /switchyard-source/cargo-auditable" in content
    )
    assert '--target "$rust_target"' in content
    assert f"{cargo} install --list --root /usr/local" in content
    assert "cargo-auditable v0.7.5 (/switchyard-source/cargo-auditable):" in content
    assert f"{cargo} auditable --help" in content
    assert "cargo-auditable --version" not in content
    assert (
        f"{cargo} auditable build --locked --release --package switchyard-server"
    ) in content
    assert "readelf --sections --wide /opt/kew/switchyard-server" in content


def test_proxy_binary_keeps_its_audit_metadata_through_strip_and_install() -> None:
    """trivy inventories the proxy's crates from ``.dep-v0``; losing it at any
    step leaves the Rust floor in the gate as the only thing that notices."""
    script = _switchyard_build_script()
    checks = [
        "readelf --sections --wide \"$binary\" | grep -Fq '.dep-v0'",
        'strip --strip-debug "$binary"',
        "readelf --sections --wide \"$binary\" | grep -Fq '.dep-v0'",
        "readelf --sections --wide /opt/kew/switchyard-server | grep -Fq '.dep-v0'",
    ]
    position = script.index(f"cargo +{_SWITCHYARD_TOOLCHAIN} auditable build --locked")
    for check in checks:
        position = script.index(check, position + 1)
    assert script.count("grep -Fq '.dep-v0'") == 3


def _switchyard_command(
    arguments: list[str],
    *,
    timeout: int,
    failure: str,
    require_success: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AssertionError(failure) from None
    if require_success and result.returncode != 0:
        raise AssertionError(failure)
    return result


def _run_in_own_session(
    arguments: list[str],
    *,
    timeout: float,
    failure: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run *arguments* as the leader of a new session and, on timeout, stop it.

    ``subprocess.run(..., timeout=...)`` kills only the process it started, so
    a ``docker build`` the scan script runs would outlive a timed-out test and
    keep compiling. Here the whole process group gets TERM, then KILL.
    """
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for kind, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, kind)
            try:
                process.communicate(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        raise AssertionError(failure) from None
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _switchyard_created_id(output: str) -> str:
    container_id = output.strip()
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise AssertionError("proxy creation returned an invalid container identity")
    return container_id


def _owned_switchyard_inspection(
    container_id: str, *, name: str, label: str, image_id: str
) -> dict[str, object]:
    inspected = _switchyard_command(
        ["docker", "inspect", "--type", "container", container_id],
        timeout=30,
        failure="owned proxy container inspection failed",
    )
    try:
        records = json.loads(inspected.stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError
        container = records[0]
        if not isinstance(container, dict):
            raise ValueError
        config = container["Config"]
        if not isinstance(config, dict):
            raise ValueError
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            raise ValueError
        if (
            container["Id"] != container_id
            or container["Name"] != "/" + name
            or container["Image"] != image_id
            or labels.get("io.kew.scimg-startup") != label
        ):
            raise ValueError
        environment = config.get("Env") or []
        if not isinstance(environment, list) or not all(
            isinstance(value, str) for value in environment
        ):
            raise ValueError
        if any(
            _CREDENTIAL_NAME_RE.search(value.split("=", 1)[0]) for value in environment
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise AssertionError(
            "proxy container ownership or environment mismatch"
        ) from None
    return container


def _switchyard_container_absent(container_id: str) -> bool:
    result = _switchyard_command(
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
        ],
        timeout=30,
        failure="owned proxy container absence query failed",
    )
    if result.stderr:
        raise AssertionError("owned proxy container absence query failed")
    rows = result.stdout.splitlines()
    if not rows:
        return True
    if rows != [container_id]:
        raise AssertionError("owned proxy container absence query was ambiguous")
    return False


def _cleanup_owned_switchyard(
    container_id: str, *, name: str, label: str, image_id: str
) -> None:
    if _switchyard_container_absent(container_id):
        return
    _owned_switchyard_inspection(
        container_id, name=name, label=label, image_id=image_id
    )
    _switchyard_command(
        ["docker", "rm", "--force", container_id],
        timeout=30,
        failure="owned proxy container removal could not be completed",
        require_success=False,
    )
    if not _switchyard_container_absent(container_id):
        raise AssertionError("the owned proxy container was not removed")


def _assert_switchyard_write_controls(container_id: str, token: str) -> None:
    permissions = _switchyard_command(
        ["docker", "exec", container_id, "stat", "-c", "%a", "/tmp"],
        timeout=30,
        failure="proxy root write-control directory could not be inspected",
    )
    if permissions.stdout.strip() != "1777":
        raise AssertionError(
            "proxy root write-control directory has unexpected permissions"
        )
    _switchyard_command(
        [
            "docker",
            "exec",
            "--env",
            "LC_ALL=C",
            container_id,
            "touch",
            "/run/kew-startup-write/control",
        ],
        timeout=30,
        failure="proxy writable tmpfs control failed",
    )
    denied = _switchyard_command(
        [
            "docker",
            "exec",
            "--env",
            "LC_ALL=C",
            container_id,
            "touch",
            f"/tmp/kew-startup-{token}",
        ],
        timeout=30,
        failure="proxy read-only root control could not be completed",
        require_success=False,
    )
    if denied.returncode == 0 or "Read-only file system" not in denied.stderr:
        raise AssertionError(
            "proxy root write was not denied by the read-only filesystem"
        )


def _assert_hardened_switchyard_runtime(
    inspected: dict[str, object], *, effective_uid: str, ready: bool
) -> None:
    config = inspected["Config"]
    host = inspected["HostConfig"]
    state = inspected["State"]
    assert isinstance(config, dict)
    assert isinstance(host, dict)
    assert isinstance(state, dict)
    configured_user = str(config.get("User", ""))
    user_name = configured_user.split(":", 1)[0].casefold()
    assert user_name not in {"", "0", "root"}, "configured user is root"
    assert configured_user == "65532:65532", "unexpected configured proxy identity"
    assert effective_uid == "65532", "effective proxy identity is root or unexpected"
    assert state.get("Running") is True, "configured proxy exited during startup"
    assert host.get("ReadonlyRootfs") is True, "read-only root is disabled"
    assert host.get("PidsLimit") == 64, "process limit is absent"
    assert host.get("Memory") == 256 * 1024 * 1024, "memory limit is absent"
    assert host.get("MemorySwap") == 256 * 1024 * 1024, "swap limit is absent"
    assert host.get("NanoCpus") == 1_000_000_000, "CPU limit is absent"
    assert ready, "configured proxy health check failed"


def _valid_runtime_inspection() -> dict[str, object]:
    return {
        "Config": {"User": "65532:65532"},
        "State": {"Running": True},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "PidsLimit": 64,
            "Memory": 256 * 1024 * 1024,
            "MemorySwap": 256 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
        },
    }


@pytest.mark.parametrize("root_user", ["", "0", "0:0", "root", "root:root"])
def test_proxy_runtime_assertion_rejects_root_spellings(root_user: str) -> None:
    inspected = _valid_runtime_inspection()
    inspected["Config"]["User"] = root_user  # type: ignore[index]

    with pytest.raises(AssertionError, match="root|identity"):
        _assert_hardened_switchyard_runtime(inspected, effective_uid="0", ready=True)


def test_proxy_runtime_assertion_rejects_disabled_read_only_root() -> None:
    inspected = _valid_runtime_inspection()
    inspected["HostConfig"]["ReadonlyRootfs"] = False  # type: ignore[index]

    with pytest.raises(AssertionError, match="read-only"):
        _assert_hardened_switchyard_runtime(
            inspected, effective_uid="65532", ready=True
        )


@pytest.mark.parametrize(
    "limit",
    ["PidsLimit", "Memory", "MemorySwap", "NanoCpus"],
)
def test_proxy_runtime_assertion_rejects_absent_limits(limit: str) -> None:
    inspected = _valid_runtime_inspection()
    inspected["HostConfig"][limit] = 0  # type: ignore[index]

    with pytest.raises(AssertionError, match="limit"):
        _assert_hardened_switchyard_runtime(
            inspected, effective_uid="65532", ready=True
        )


@pytest.mark.parametrize(("running", "ready"), [(False, True), (True, False)])
def test_proxy_runtime_assertion_rejects_failed_startup(
    running: bool, ready: bool
) -> None:
    inspected = _valid_runtime_inspection()
    inspected["State"]["Running"] = running  # type: ignore[index]

    with pytest.raises(AssertionError, match="startup|health"):
        _assert_hardened_switchyard_runtime(
            inspected, effective_uid="65532", ready=ready
        )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")
@pytest.mark.requires_switchyard_image
@pytest.mark.serial
def test_proxy_image_builds_from_exact_pinned_revision(tmp_path: Path) -> None:
    from kew.switchyard import probe_switchyard_endpoint

    built = _run_in_own_session(
        ["bash", str(_SCAN_SCRIPT), "--build-switchyard-only"],
        # An uncached build fetches and compiles the pinned upstream source.
        # On timeout the script's whole group is stopped, docker build too.
        timeout=1800,
        failure="the Switchyard image build failed or timed out",
        # --build-switchyard-only is a Switchyard-only action, and the script
        # refuses one unless the run opts in. The test runs only on an opted-in
        # run, but the conftest autouse fixture keeps the ambient switch out of
        # every test, so the opt-in is restated here
        # (tests/test_switchyard_image_skip.py).
        env={**os.environ, "KEW_SWITCHYARD_IMAGE": "1"},
    )
    assert built.returncode == 0, "the Switchyard image build failed"
    image_id = built.stdout.strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
    print(f"SCIMG startup image: {image_id}")

    inspected = subprocess.run(
        ["docker", "image", "inspect", image_id],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    config = json.loads(inspected.stdout)[0]["Config"]
    assert config["Labels"]["org.opencontainers.image.revision"] == (
        _SWITCHYARD_REVISION
    )

    route = "synthetic-no-credentials"
    route_config = tmp_path / "switchyard.toml"
    route_config.write_text(
        "schema_version = 1\n"
        "[llm_clients.synthetic]\n"
        'format = "openai_responses"\n'
        'base_url = "https://example.invalid/v1"\n'
        "[targets.synthetic]\n"
        'id = "synthetic/model"\n'
        'llm_client = "synthetic"\n'
        "[routes.synthetic]\n"
        f'id = "{route}"\n'
        'type = "passthrough"\n'
        'target = "synthetic"\n',
        encoding="utf-8",
    )
    route_config.chmod(0o444)
    ownership_token = uuid.uuid4().hex
    container_name = f"kew-switchyard-test-{ownership_token}"
    owned_container_id: str | None = None
    print(f"SCIMG startup candidate: {container_name}; label={ownership_token}")
    try:
        created = _switchyard_command(
            [
                "docker",
                "create",
                "--name",
                container_name,
                "--label",
                f"io.kew.scimg-startup={ownership_token}",
                "--env",
                "OTEL_SDK_DISABLED=true",
                "--pull=never",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/run/kew-startup-write:rw,nosuid,nodev,noexec,size=1048576,mode=1777",
                "--pids-limit=64",
                "--memory=256m",
                "--memory-swap=256m",
                "--cpus=1",
                "--mount",
                f"type=bind,src={route_config},dst=/run/switchyard.toml,readonly",
                "--publish",
                "127.0.0.1::4000",
                image_id,
                "--config",
                "/run/switchyard.toml",
                "--host",
                "0.0.0.0",
                "--port",
                "4000",
            ],
            timeout=60,
            failure=(
                "proxy creation failed or has unknown outcome; "
                "inspect the recorded candidate identity"
            ),
        )
        candidate_id = _switchyard_created_id(created.stdout)
        print(f"SCIMG startup candidate ID: {candidate_id}")
        _owned_switchyard_inspection(
            candidate_id, name=container_name, label=ownership_token, image_id=image_id
        )
        owned_container_id = candidate_id
        _switchyard_command(
            ["docker", "start", owned_container_id],
            timeout=60,
            failure="the configured proxy start failed or has unknown outcome",
        )
        published = _switchyard_command(
            ["docker", "port", owned_container_id, "4000/tcp"],
            timeout=30,
            failure="the configured proxy port could not be inspected",
        ).stdout.strip()
        port = int(published.rsplit(":", 1)[1])
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        ready = False
        healthy_observations = 0
        while time.monotonic() < deadline:
            probe = probe_switchyard_endpoint(
                endpoint=endpoint,
                route=route,
                timeout_seconds=0.5,
            )
            if probe.admitted:
                healthy_observations += 1
                if healthy_observations >= 2:
                    ready = True
                    break
            else:
                healthy_observations = 0
            time.sleep(0.05)
        container = _owned_switchyard_inspection(
            owned_container_id,
            name=container_name,
            label=ownership_token,
            image_id=image_id,
        )
        effective_uid = _switchyard_command(
            ["docker", "exec", owned_container_id, "id", "-u"],
            timeout=30,
            failure="the configured proxy identity could not be inspected",
        ).stdout.strip()
        _assert_hardened_switchyard_runtime(
            container, effective_uid=effective_uid, ready=ready
        )
        _assert_switchyard_write_controls(owned_container_id, ownership_token)
    finally:
        if owned_container_id is not None:
            _cleanup_owned_switchyard(
                owned_container_id,
                name=container_name,
                label=ownership_token,
                image_id=image_id,
            )


_STARTUP_FIXTURE_CID = "a" * 64
_STARTUP_FIXTURE_IMAGE = "sha256:" + "b" * 64
_STARTUP_FIXTURE_NAME = "kew-switchyard-test-synthetic"
_STARTUP_FIXTURE_LABEL = "synthetic-ownership"


def _startup_fixture_result(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["synthetic"], code, stdout, stderr)


def _startup_fixture_ownership(**overrides):
    value = {
        "Id": _STARTUP_FIXTURE_CID,
        "Image": _STARTUP_FIXTURE_IMAGE,
        "Name": "/" + _STARTUP_FIXTURE_NAME,
        "Config": {
            "Labels": {"io.kew.scimg-startup": _STARTUP_FIXTURE_LABEL},
            "Env": ["OTEL_SDK_DISABLED=true"],
        },
    }
    value.update(overrides)
    return value


@pytest.fixture
def _startup_forbid_unarranged_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unarranged subprocess call")

    monkeypatch.setattr(subprocess, "run", forbidden)


def _startup_fixture_arrange(monkeypatch, responses):
    pending = list(responses)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert pending, "unexpected additional subprocess call"
        value = pending.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(subprocess, "run", run)
    return calls, pending


@pytest.mark.parametrize("kind", ["timeout", "oserror"])
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_command_redacts_timeout_and_oserror_without_chaining(
    monkeypatch, kind
):
    canary = "/SYNTHETIC_COMMAND_CANARY"
    arguments = ["docker", "build", "--file", canary]
    error = subprocess.TimeoutExpired(arguments, 1800, output=canary, stderr=canary)
    if kind == "oserror":
        error = OSError(canary)
    _startup_fixture_arrange(monkeypatch, [error])
    with pytest.raises(AssertionError, match="fixed build failure") as raised:
        _switchyard_command(arguments, timeout=1800, failure="fixed build failure")
    assert raised.value.__suppress_context__ is True
    import traceback

    rendered = "".join(traceback.format_exception(raised.value))
    assert canary not in rendered


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_nonzero_build_never_renders_captured_output(monkeypatch):
    canary = "SYNTHETIC_BUILD_OUTPUT"
    _startup_fixture_arrange(monkeypatch, [_startup_fixture_result(canary, canary, 1)])
    with pytest.raises(AssertionError, match="fixed build failure") as raised:
        _switchyard_command(
            ["docker", "build"], timeout=1800, failure="fixed build failure"
        )
    assert canary not in str(raised.value)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "name",
        "a" * 63,
        _STARTUP_FIXTURE_CID + "\n" + _STARTUP_FIXTURE_CID,
        "A" * 64,
    ],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_creation_identity_must_be_one_complete_id(output):
    with pytest.raises(AssertionError, match="invalid container identity"):
        _switchyard_created_id(output)


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_full_created_id_is_retained():
    assert _switchyard_created_id(_STARTUP_FIXTURE_CID + "\n") == _STARTUP_FIXTURE_CID


@pytest.mark.parametrize(
    "field,value",
    [("Id", "c" * 64), ("Image", "sha256:" + "d" * 64), ("Name", "/unrelated")],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_wrong_identity_never_reaches_removal(monkeypatch, field, value):
    calls, pending = _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
            _startup_fixture_result(
                json.dumps([_startup_fixture_ownership(**{field: value})])
            ),
        ],
    )
    with pytest.raises(AssertionError, match="ownership or environment mismatch"):
        _cleanup_owned_switchyard(
            _STARTUP_FIXTURE_CID,
            name=_STARTUP_FIXTURE_NAME,
            label=_STARTUP_FIXTURE_LABEL,
            image_id=_STARTUP_FIXTURE_IMAGE,
        )
    assert not any(arguments[1] == "rm" for arguments, _ in calls)
    assert not pending


@pytest.mark.parametrize(
    "config",
    [
        {"Labels": {"io.kew.scimg-startup": "unrelated"}, "Env": []},
        {"Labels": {}, "Env": []},
        {
            "Labels": {"io.kew.scimg-startup": _STARTUP_FIXTURE_LABEL},
            "Env": ["API_TOKEN=synthetic"],
        },
    ],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_wrong_label_or_credential_environment_never_reaches_removal(
    monkeypatch, config
):
    calls, _ = _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
            _startup_fixture_result(
                json.dumps([_startup_fixture_ownership(Config=config)])
            ),
        ],
    )
    with pytest.raises(AssertionError, match="ownership or environment mismatch"):
        _cleanup_owned_switchyard(
            _STARTUP_FIXTURE_CID,
            name=_STARTUP_FIXTURE_NAME,
            label=_STARTUP_FIXTURE_LABEL,
            image_id=_STARTUP_FIXTURE_IMAGE,
        )
    assert not any(arguments[1] == "rm" for arguments, _ in calls)


@pytest.mark.parametrize(
    "response",
    [
        _startup_fixture_result("", "daemon unavailable", 1),
        _startup_fixture_result("", "warning"),
        _startup_fixture_result(
            _STARTUP_FIXTURE_CID + "\n" + _STARTUP_FIXTURE_CID + "\n"
        ),
        _startup_fixture_result("c" * 64 + "\n"),
    ],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_absence_refuses_unavailable_or_ambiguous_queries(
    monkeypatch, response
):
    _startup_fixture_arrange(monkeypatch, [response])
    with pytest.raises(AssertionError, match="absence query"):
        _switchyard_container_absent(_STARTUP_FIXTURE_CID)


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_empty_success_is_authoritative_absence(monkeypatch):
    calls, _ = _startup_fixture_arrange(monkeypatch, [_startup_fixture_result()])
    assert _switchyard_container_absent(_STARTUP_FIXTURE_CID) is True
    assert calls[0][0][-2:] == ["--filter", "id=" + _STARTUP_FIXTURE_CID]


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_cleanup_skips_an_already_absent_owned_id(monkeypatch):
    calls, pending = _startup_fixture_arrange(monkeypatch, [_startup_fixture_result()])
    _cleanup_owned_switchyard(
        _STARTUP_FIXTURE_CID,
        name=_STARTUP_FIXTURE_NAME,
        label=_STARTUP_FIXTURE_LABEL,
        image_id=_STARTUP_FIXTURE_IMAGE,
    )
    assert len(calls) == 1 and not pending


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_cleanup_verifies_ownership_and_removes_only_full_id(monkeypatch):
    calls, pending = _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
            _startup_fixture_result(json.dumps([_startup_fixture_ownership()])),
            _startup_fixture_result(),
            _startup_fixture_result(),
        ],
    )
    _cleanup_owned_switchyard(
        _STARTUP_FIXTURE_CID,
        name=_STARTUP_FIXTURE_NAME,
        label=_STARTUP_FIXTURE_LABEL,
        image_id=_STARTUP_FIXTURE_IMAGE,
    )
    assert calls[2][0] == ["docker", "rm", "--force", _STARTUP_FIXTURE_CID]
    assert all(_STARTUP_FIXTURE_NAME not in arguments for arguments, _ in calls)
    assert not pending


@pytest.mark.parametrize(
    "final",
    [
        _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
        _startup_fixture_result("", "daemon unavailable", 1),
    ],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_cleanup_failure_cannot_pass_on_unknown_or_present_state(
    monkeypatch, final
):
    _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
            _startup_fixture_result(json.dumps([_startup_fixture_ownership()])),
            _startup_fixture_result("", "remove failed", 1),
            final,
        ],
    )
    with pytest.raises(AssertionError):
        _cleanup_owned_switchyard(
            _STARTUP_FIXTURE_CID,
            name=_STARTUP_FIXTURE_NAME,
            label=_STARTUP_FIXTURE_LABEL,
            image_id=_STARTUP_FIXTURE_IMAGE,
        )


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_remove_error_requires_successful_confirmed_absence(monkeypatch):
    _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(_STARTUP_FIXTURE_CID + "\n"),
            _startup_fixture_result(json.dumps([_startup_fixture_ownership()])),
            _startup_fixture_result("", "already removed", 1),
            _startup_fixture_result(),
        ],
    )
    _cleanup_owned_switchyard(
        _STARTUP_FIXTURE_CID,
        name=_STARTUP_FIXTURE_NAME,
        label=_STARTUP_FIXTURE_LABEL,
        image_id=_STARTUP_FIXTURE_IMAGE,
    )


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_same_touch_binary_proves_writable_tmpfs_and_readonly_root(monkeypatch):
    calls, pending = _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result("1777\n"),
            _startup_fixture_result(),
            _startup_fixture_result("", "Read-only file system", 1),
        ],
    )
    _assert_switchyard_write_controls(_STARTUP_FIXTURE_CID, _STARTUP_FIXTURE_LABEL)
    assert calls[1][0][:-1] == calls[2][0][:-1]
    assert calls[1][0][-2] == "touch"
    assert "LC_ALL=C" in calls[1][0]
    assert not pending


@pytest.mark.parametrize(
    "denial",
    [
        _startup_fixture_result("", "Permission denied", 1),
        _startup_fixture_result("", "command not found", 127),
        _startup_fixture_result(),
    ],
)
@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_arbitrary_write_failure_or_write_success_does_not_prove_readonly(
    monkeypatch, denial
):
    _startup_fixture_arrange(
        monkeypatch,
        [_startup_fixture_result("1777\n"), _startup_fixture_result(), denial],
    )
    with pytest.raises(AssertionError, match="not denied by the read-only filesystem"):
        _assert_switchyard_write_controls(_STARTUP_FIXTURE_CID, _STARTUP_FIXTURE_LABEL)


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_native_tmp_permissions_must_support_the_comparison(monkeypatch):
    calls, _ = _startup_fixture_arrange(monkeypatch, [_startup_fixture_result("755\n")])
    with pytest.raises(AssertionError, match="unexpected permissions"):
        _assert_switchyard_write_controls(_STARTUP_FIXTURE_CID, _STARTUP_FIXTURE_LABEL)
    assert len(calls) == 1


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_failed_positive_write_control_refuses_before_negative(monkeypatch):
    calls, _ = _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result("1777\n"),
            _startup_fixture_result("", "missing touch", 127),
        ],
    )
    with pytest.raises(AssertionError, match="writable tmpfs control failed"):
        _assert_switchyard_write_controls(_STARTUP_FIXTURE_CID, _STARTUP_FIXTURE_LABEL)
    assert len(calls) == 2


@pytest.mark.usefixtures("_startup_forbid_unarranged_subprocess")
def test_startup_ownership_refusal_survives_optimized_compilation(monkeypatch):
    import ast

    optimized = dict(globals())
    helper = source_ast.definition(
        sys.modules[__name__], "_owned_switchyard_inspection"
    )
    tree = ast.Module(body=[helper], type_ignores=[])
    exec(compile(tree, "<optimized-ownership-check>", "exec", optimize=2), optimized)
    _startup_fixture_arrange(
        monkeypatch,
        [
            _startup_fixture_result(
                json.dumps([_startup_fixture_ownership(Id="c" * 64)])
            ),
        ],
    )
    with pytest.raises(AssertionError, match="ownership or environment mismatch"):
        optimized["_owned_switchyard_inspection"](
            _STARTUP_FIXTURE_CID,
            name=_STARTUP_FIXTURE_NAME,
            label=_STARTUP_FIXTURE_LABEL,
            image_id=_STARTUP_FIXTURE_IMAGE,
        )


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _synthetic_scan_script(
    tmp_path: Path,
    *,
    repository_name: str = "synthetic-repo",
) -> Path:
    scripts = tmp_path / repository_name / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "scan-agent-image.sh"
    script.write_text(_SCAN_SCRIPT.read_text())
    script.chmod(0o755)
    _plant_trivy_pin(tmp_path / repository_name)
    return script


def _plant_trivy_pin(repo_root: Path) -> Path:
    """Copy the shipped scanner pin into a synthetic checkout.

    The gate reads its pinned scanner out of the tree it is called from, at the
    path kew ships that file at (#2462), so a synthetic repository needs a copy
    of it exactly there. The location is derived from the package's own
    ``pin_path()`` rather than restated: if the script's path and kew's ever
    disagree, these tests say so instead of a verification host saying it an
    hour into a run.

    The inputs-digest module the gate labels its images with is planted beside
    it, for the same reason (:mod:`support.scan_checkout`).
    """
    return plant_scan_inputs(repo_root)


def _run_scan_script(
    script: Path,
    binroot: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
    pin_stub: bool = True,
) -> subprocess.CompletedProcess[str]:
    # The stub trivy stands in for the pinned binary, so the synthetic
    # checkout's pin names the stub's digest (#3258). A test about that binary
    # check passes ``pin_stub=False`` and keeps the shipped digests.
    if pin_stub and (binroot / "trivy").exists():
        pin_stub_trivy(script.parent.parent, binroot / "trivy")
    env = dict(os.environ)
    env["PATH"] = f"{binroot}:{env['PATH']}"
    # The Switchyard layer is opt-in (tests/test_switchyard_image_skip.py).
    # Every caller here drives the whole gate, that layer included, so they
    # all opt in; the conftest autouse fixture keeps the ambient switch out,
    # so this is the only place it is set.
    env["KEW_SWITCHYARD_IMAGE"] = "1"
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *arguments],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def _run_gate(
    binroot: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    tmp_path = binroot.parent
    script = _synthetic_scan_script(tmp_path)
    return _run_scan_script(script, binroot, extra_env=extra_env)


class TestAStoppedScanStopsItsBuild:
    """A signal that stops the gate stops the ``docker build`` it started.

    The Switchyard build compiles upstream source for minutes. A caller that
    times out or is cancelled signals the script; if ``docker build`` ran as an
    unowned child it kept building after the caller gave up. The fake docker
    here blocks in ``build`` and records whether it was terminated.
    """

    @pytest.fixture()
    def binroot(self, tmp_path: Path) -> Path:
        directory = tmp_path / "bin"
        directory.mkdir()
        log = tmp_path / "docker-signals.log"
        _shim(
            directory,
            "docker",
            f"""
            case "$1" in
              info|images|tag|rmi) exit 0 ;;
              build) ;;
              *) exit 0 ;;
            esac
            sleep 120 &
            waiter=$!
            trap 'echo terminated >> "{log}"; kill "$waiter"; exit 143' TERM
            echo started >> "{log}"
            wait "$waiter"
            echo finished >> "{log}"
            """,
        )
        stamp = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _shim(
            directory,
            "trivy",
            f"""
            if [ "$1" = "--version" ]; then echo "Version: {_PINNED_TRIVY}"; exit 0; fi
            if printf '%s\\n' "$@" | grep -q -- --download-db-only; then exit 0; fi
            if [ "$1" = "version" ]; then
              echo '{{"VulnerabilityDB":{{"UpdatedAt":"{stamp}"}}}}'
              exit 0
            fi
            exit 0
            """,
        )
        return directory

    @staticmethod
    def _wait_for(log: Path, line: str, deadline: float = 60.0) -> None:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if log.exists() and line in log.read_text().split():
                return
            time.sleep(0.05)
        raise AssertionError(f"{log.name} never logged {line!r}")

    @pytest.mark.parametrize(
        ("arguments", "opt_in"),
        [(("--build-switchyard-only",), "1"), ((), "")],
        ids=["build-switchyard-only", "scan-loop"],
    )
    @pytest.mark.parametrize(
        ("delivered", "status"),
        [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
        ids=["TERM", "INT", "HUP"],
    )
    def test_signalling_the_script_terminates_the_docker_build(
        self,
        binroot: Path,
        tmp_path: Path,
        arguments: tuple[str, ...],
        opt_in: str,
        delivered: signal.Signals,
        status: int,
    ) -> None:
        script = _synthetic_scan_script(tmp_path)
        pin_stub_trivy(script.parent.parent, binroot / "trivy")
        log = tmp_path / "docker-signals.log"
        env = {**os.environ, "PATH": f"{binroot}:{os.environ['PATH']}"}
        env["KEW_SWITCHYARD_IMAGE"] = opt_in
        process = subprocess.Popen(
            ["bash", str(script), *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            self._wait_for(log, "started")
            # Only the script: the build client is reached through it.
            os.kill(process.pid, delivered)
            process.wait(timeout=30)
            # Checked before the cleanup below, which would kill a survivor.
            self._wait_for(log, "terminated", deadline=30.0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        assert process.returncode == status
        assert "finished" not in log.read_text().split()

    def test_a_timed_out_build_launch_stops_the_whole_group(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """The launcher the opted-in image test uses kills the group, not
        only the script, when its deadline passes."""
        script = _synthetic_scan_script(tmp_path)
        log = tmp_path / "docker-signals.log"

        with pytest.raises(AssertionError, match="build timed out"):
            _run_in_own_session(
                ["bash", str(script), "--build-switchyard-only"],
                timeout=5,
                failure="build timed out",
                env={
                    **os.environ,
                    "PATH": f"{binroot}:{os.environ['PATH']}",
                    "KEW_SWITCHYARD_IMAGE": "1",
                },
            )

        self._wait_for(log, "terminated", deadline=30.0)
        assert "finished" not in log.read_text().split()

    @pytest.mark.parametrize(
        ("delivered", "status"),
        [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
        ids=["TERM", "INT", "HUP"],
    )
    def test_a_signal_during_trivy_stops_trivy_before_the_exit_cleanup(
        self, tmp_path: Path, delivered: signal.Signals, status: int
    ) -> None:
        """A signal to the script alone, while trivy reads an image.

        The script must stop trivy, then exit 128+signal, and only then run its
        EXIT cleanup, which removes the run's reference to the image trivy was
        reading. INT matters most: a background child of a non-interactive
        shell ignores it, so a script that leaves trivy in charge of INT scans
        on and can exit 0 after a cancel. The script starts in a new session
        with default dispositions, as a caller's subprocess does.
        """
        binroot = tmp_path / "bin"
        binroot.mkdir()
        events = tmp_path / "events.log"
        _shim(
            binroot,
            "docker",
            f"""
            case "$1" in
              build)
                for a in "$@"; do
                  if [ "$prev" = "--iidfile" ]; then
                    echo "sha256:{"a" * 64}" > "$a"
                  fi
                  prev="$a"
                done ;;
              rmi) echo "docker-rmi $2" >> "{events}" ;;
            esac
            exit 0
            """,
        )
        stamp = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _shim(
            binroot,
            "trivy",
            f"""
            if [ "$1" = "--version" ]; then echo "Version: {_PINNED_TRIVY}"; exit 0; fi
            if printf '%s\\n' "$@" | grep -q -- --download-db-only; then exit 0; fi
            if [ "$1" = "version" ]; then
              echo '{{"VulnerabilityDB":{{"UpdatedAt":"{stamp}"}}}}'
              exit 0
            fi
            sleep 120 &
            waiter=$!
            trap 'echo trivy-terminated >> "{events}"; kill "$waiter"; exit 143' TERM
            echo trivy-scanning >> "{events}"
            wait "$waiter"
            echo trivy-finished >> "{events}"
            """,
        )
        script = _synthetic_scan_script(tmp_path)
        pin_stub_trivy(script.parent.parent, binroot / "trivy")
        env = {**os.environ, "PATH": f"{binroot}:{os.environ['PATH']}"}
        env["KEW_SWITCHYARD_IMAGE"] = ""
        process = subprocess.Popen(
            ["bash", str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            self._wait_for(events, "trivy-scanning")
            os.kill(process.pid, delivered)
            # trivy sleeps for 120 seconds: ending within 15 is not waiting.
            process.wait(timeout=15)
            # Checked before the cleanup below, which would kill a survivor.
            self._wait_for(events, "trivy-terminated", deadline=5.0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        assert process.returncode == status
        lines = events.read_text().splitlines()
        assert "trivy-finished" not in lines
        removals = [
            index
            for index, line in enumerate(lines)
            if line.startswith("docker-rmi kew-agent:scan-run-")
        ]
        assert removals, "the EXIT cleanup did not remove the run's reference"
        assert lines.index("trivy-terminated") < removals[0], lines


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestScanScriptRefusals:
    """Run the gate for real against fake docker/trivy, one refusal at a time.

    Each test breaks exactly ONE precondition and asserts the script refuses.
    Grepping the source for `exit 1` would pass even if the branch were
    unreachable, which is the failure mode these guards exist to prevent.
    """

    @pytest.fixture()
    def binroot(self, tmp_path: Path) -> Path:
        d = tmp_path / "bin"
        d.mkdir()
        # A docker that answers `info` and `build` but nothing real.
        _shim(
            d,
            "docker",
            f"""
            FAKE_DIGEST={"a" * 64}
            case "$1" in
              info) exit 0 ;;
              build)
                for a in "$@"; do
                  if [ "$prev" = "--iidfile" ]; then
                    echo "sha256:$FAKE_DIGEST" > "$a"
                  fi
                  prev="$a"
                done
                exit 0 ;;
              *) exit 0 ;;
            esac
            """,
        )
        return d

    @staticmethod
    def _fresh_stamp() -> str:
        """A database timestamp that is recent, and in the PAST.

        The first version of these tests used 2099 as the "good" value. That
        made every one of them assert the very defect they were meant to guard:
        a future timestamp produced a negative age, which sailed under a
        `-gt limit` check and was read as freshness (round-2 review, Major 3).
        A fixture must never be the only thing that makes a guard look correct.
        """
        return (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _trivy(self, binroot: Path, *, version: str, db_updated: str | None) -> None:
        db = (
            f'"VulnerabilityDB":{{"UpdatedAt":"{db_updated}"}}'
            if db_updated
            else '"VulnerabilityDB":{}'
        )
        _shim(
            binroot,
            "trivy",
            f"""
            if [ "$1" = "--version" ]; then echo "Version: {version}"; exit 0; fi
            # The real gate downloads the database before reading its metadata.
            if printf '%s\\n' "$@" | grep -q -- --download-db-only; then exit 0; fi
            if [ "$1" = "version" ]; then
              echo '{{"Version":"{version}",{db}}}'
              exit 0
            fi
            # Any scan: report a large package inventory and no findings.
            if printf '%s\\n' "$@" | grep -q -- --list-all-pkgs; then
              printf '{{"Results":[{{"Class":"os-pkgs","Type":"debian","Packages":['
              for i in $(seq 1 200); do
                if [ "$i" -gt 1 ]; then printf ','; fi
                printf '{{"Name":"deb%s"}}' "$i"
              done
              printf ']}},{{"Class":"lang-pkgs","Type":"node-pkg","Packages":['
              for i in $(seq 1 200); do
                if [ "$i" -gt 1 ]; then printf ','; fi
                printf '{{"Name":"npm%s"}}' "$i"
              done
              printf ']}},{{"Class":"lang-pkgs","Type":"rustbinary","Packages":['
              for i in $(seq 1 200); do
                if [ "$i" -gt 1 ]; then printf ','; fi
                printf '{{"Name":"crate%s"}}' "$i"
              done
              printf ']}}]}}'
              exit 0
            fi
            echo "no findings"
            exit 0
            """,
        )

    def test_a_mismatched_scanner_version_refuses(self, binroot: Path) -> None:
        # Round-1 review, Major 3: this warned and then reported success, so a
        # gate run by an unknown scanner claimed to have proven something.
        self._trivy(binroot, version="9.9.9", db_updated=self._fresh_stamp())
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a scanner that is not the pinned version must refuse, not warn:\n"
            + result.stdout
        )
        assert "not the pinned" in result.stderr
        assert "✓" not in result.stdout

    def test_a_missing_vulnerability_database_refuses(self, binroot: Path) -> None:
        # Packages come from the image, advisories come from the database. The
        # package floor cannot see an empty DB, so it gets its own check.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=None)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "no vulnerability database must refuse:\n" + result.stdout
        )
        assert "no vulnerability database" in result.stderr

    def test_a_stale_vulnerability_database_refuses(self, binroot: Path) -> None:
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated="2020-01-01T00:00:00Z")
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a stale vulnerability database must refuse:\n" + result.stdout
        )
        assert "days old" in result.stderr

    def test_an_os_only_inventory_refuses(self, binroot: Path) -> None:
        # The floor used to be a whole-image total, so a run that inventoried
        # Debian and analysed NO Node packages cleared it — while every fixable
        # CRITICAL/HIGH this gate exists to catch is an npm finding. One
        # ecosystem's absence must not hide behind the other's size.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        trivy = binroot / "trivy"
        trivy.write_text(
            trivy.read_text().replace('"Class":"lang-pkgs"', '"Class":"os-pkgs"')
        )
        trivy.chmod(0o755)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "an inventory with no Node packages must refuse:\n" + result.stdout
        )
        assert "npm packages" in result.stderr

    def test_a_pip_only_inventory_refuses(self, binroot: Path) -> None:
        # `lang-pkgs` is EVERY language ecosystem, not Node. Counting the class
        # alone meant a scan that inventoried 100 pip packages and zero npm
        # packages reported "100 Node packages" and cleared the floor — while
        # every fixable CRITICAL/HIGH in these images is an npm finding.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        trivy = binroot / "trivy"
        trivy.write_text(trivy.read_text().replace('"Type":"node-pkg"', '"Type":"pip"'))
        trivy.chmod(0o755)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "an inventory with no npm packages must refuse:\n" + result.stdout
        )
        assert "npm packages" in result.stderr

    def test_a_rust_only_language_inventory_refuses(self, binroot: Path) -> None:
        """The false pass that was actually live, not a hypothetical one.

        The agent image reports 152 debian, 151 node-pkg and 1066 rustbinary
        packages — the last from the `uv` and `uvx` binaries. Counting the bare
        `lang-pkgs` class gave 1217 and called them all Node, so a TOTAL failure
        of the Node analysis would still have cleared a floor of 50 on Rust
        alone. Every fixable CRITICAL/HIGH in these images is a Node finding.
        """
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        trivy = binroot / "trivy"
        trivy.write_text(
            trivy.read_text().replace('"Type":"node-pkg"', '"Type":"rustbinary"')
        )
        trivy.chmod(0o755)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a language inventory with no Node packages must refuse, however "
            "many packages of other kinds it found:\n" + result.stdout
        )
        assert "npm packages" in result.stderr

    def test_a_scan_that_reads_no_packages_refuses(self, binroot: Path) -> None:
        # A gate that scans nothing passes everything.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        trivy = binroot / "trivy"
        trivy.write_text(trivy.read_text().replace("seq 1 200", "seq 1 3"))
        trivy.chmod(0o755)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a scan reporting almost no packages must refuse:\n" + result.stdout
        )
        assert "did not read" in result.stderr

    def test_a_future_database_timestamp_refuses(self, binroot: Path) -> None:
        # A database dated in the future is a broken clock or a forged stamp,
        # not freshness. Flooring the age to days let it pass a `-gt` limit.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated="2099-01-01T00:00:00Z")
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a future database timestamp must refuse:\n" + result.stdout
        )
        assert "future" in result.stderr

    def test_a_database_download_failure_refuses(self, binroot: Path) -> None:
        # The gate's one network operation. If it cannot run, the scanner has
        # no advisories and would report every image clean.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        trivy = binroot / "trivy"
        probe = "grep -q -- --download-db-only; then exit"
        trivy.write_text(trivy.read_text().replace(f"{probe} 0; fi", f"{probe} 1; fi"))
        trivy.chmod(0o755)
        result = _run_gate(binroot)
        assert result.returncode != 0
        assert "vulnerability database" in result.stderr

    def test_a_build_that_reports_no_image_id_refuses(self, binroot: Path) -> None:
        # mktemp leaves the iidfile empty and `cat` of an empty file exits 0,
        # so a builder returning success without writing an ID would hand every
        # scan an empty target — the opposite of binding to the built image.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        _shim(binroot, "docker", 'case "$1" in info) exit 0 ;; *) exit 0 ;; esac\n')
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a build that writes no image ID must refuse:\n" + result.stdout
        )
        assert "did not report a full image digest" in result.stderr

    def test_a_partial_or_crafted_image_id_refuses(self, binroot: Path) -> None:
        # The first guard matched `sha256:[0-9a-f]*` with shell globbing, which
        # has no counted repetition — one hex character followed by anything at
        # all passed, including 'sha256:abc/../../etc'. A prefix is not a digest, and a
        # check that validates one character is not a check.
        self._trivy(binroot, version=_PINNED_TRIVY, db_updated=self._fresh_stamp())
        _shim(
            binroot,
            "docker",
            """
            case "$1" in
              info) exit 0 ;;
              build)
                for a in "$@"; do
                  if [ "$prev" = "--iidfile" ]; then
                    echo "sha256:abc/../../etc" > "$a"
                  fi
                  prev="$a"
                done
                exit 0 ;;
              *) exit 0 ;;
            esac
            """,
        )
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a crafted or partial image id must refuse:\n" + result.stdout
        )
        assert "full image digest" in result.stderr

    def test_a_missing_container_runtime_refuses(self, tmp_path: Path) -> None:
        d = tmp_path / "emptybin"
        d.mkdir()
        _shim(d, "docker", "exit 1\n")  # `docker info` fails
        result = _run_gate(d)
        assert result.returncode != 0
        assert "no reachable container runtime" in result.stderr


_VERIFIER = _REPO_ROOT / "docker" / "verify-npm-overlay.js"


def _pkg(root: Path, name: str, version: str, **manifest: object) -> Path:
    """Write one package into a fixture bundle and return its directory."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(
        json.dumps({"name": name, "version": version, **manifest})
    )
    return d


def _expected_versions() -> dict[str, str]:
    """The versions the overlay script intends, read from the script itself.

    Restating them here would let the fixture drift from the thing it tests.
    """
    block = _VERIFIER.read_text().split("const EXPECTED = {", 1)[1].split("};", 1)[0]
    return dict(re.findall(r'"?([a-z-]+)"?:\s*"(\d+\.\d+\.\d+)"', block))


def _fixture_package(root: Path, name: str, version: str, source: str) -> Path:
    """Write a self-contained CommonJS package into a fixture bundle."""
    package = _pkg(root, name, version, main="index.js")
    (package / "index.js").write_text(textwrap.dedent(source).lstrip())
    return package


def _write_balanced_match(root: Path) -> Path:
    return _fixture_package(
        root,
        "balanced-match",
        "4.0.4",
        """
        "use strict";

        module.exports = function balanced(open, close, input) {
          const start = input.indexOf(open);
          const end = input.indexOf(close, start + open.length);
          if (start < 0 || end < 0) return undefined;
          return {
            pre: input.slice(0, start),
            body: input.slice(start + open.length, end),
            post: input.slice(end + close.length),
          };
        };
        """,
    )


def _write_brace_expansion(root: Path, version: str) -> Path:
    return _fixture_package(
        root,
        "brace-expansion",
        version,
        """
        "use strict";

        const balanced = require("balanced-match");

        function expand(pattern) {
          const match = balanced("{", "}", pattern);
          if (!match) return [pattern];
          return match.body
            .split(",")
            .map((part) => match.pre + part + match.post);
        }

        module.exports = { expand };
        """,
    )


def _write_ip_address(root: Path, version: str) -> Path:
    return _fixture_package(
        root,
        "ip-address",
        version,
        """
        "use strict";

        class Address4 {
          constructor(address) {
            this.address = address;
          }

          correctForm() {
            return this.address
              .split(".")
              .map((part) => String(Number.parseInt(part, 10)))
              .join(".");
          }
        }

        module.exports = { Address4 };
        """,
    )


def _write_undici(root: Path, version: str) -> Path:
    return _fixture_package(
        root,
        "undici",
        version,
        """
        "use strict";

        const http = require("http");
        let sharedDispatcher;

        function requireDispatcher(options) {
          if (!options || !(options.dispatcher instanceof Agent)) {
            throw new Error("undici dispatcher is missing");
          }
          if (sharedDispatcher === undefined) {
            sharedDispatcher = options.dispatcher;
          }
          if (options.dispatcher !== sharedDispatcher) {
            throw new Error("undici dispatcher is not shared");
          }
        }

        function get(url) {
          return new Promise((resolve, reject) => {
            const outgoing = http.get(url, (incoming) => {
              incoming.setEncoding("utf8");
              let body = "";
              incoming.on("data", (chunk) => { body += chunk; });
              incoming.on("end", () => {
                resolve({ status: incoming.statusCode, body });
              });
            });
            outgoing.on("error", reject);
          });
        }

        async function request(url, options) {
          requireDispatcher(options);
          const response = await get(url);
          return {
            statusCode: response.status,
            body: { text: async () => response.body },
          };
        }

        async function fetch(url, options) {
          requireDispatcher(options);
          const response = await get(url);
          return { status: response.status, text: async () => response.body };
        }

        class Agent {
          close() {
            return Promise.resolve();
          }
        }

        module.exports = { request, fetch, Agent };
        """,
    )


def _write_tar(root: Path, version: str) -> Path:
    return _fixture_package(
        root,
        "tar",
        version,
        r"""
        "use strict";

        const fs = require("fs");
        const path = require("path");
        const BLOCK_SIZE = 512;

        function writeText(block, offset, length, value) {
          block.write(
            value,
            offset,
            Math.min(length, Buffer.byteLength(value)),
            "ascii",
          );
        }

        function header(name, size) {
          const block = Buffer.alloc(BLOCK_SIZE);
          writeText(block, 0, 100, name);
          writeText(block, 100, 8, "0000644\0");
          writeText(block, 108, 8, "0000000\0");
          writeText(block, 116, 8, "0000000\0");
          writeText(block, 124, 12, size.toString(8).padStart(11, "0") + "\0");
          writeText(block, 136, 12, "00000000000\0");
          block.fill(0x20, 148, 156);
          writeText(block, 156, 1, "0");
          writeText(block, 257, 6, "ustar\0");
          writeText(block, 263, 2, "00");
          const checksum = block.reduce((sum, byte) => sum + byte, 0);
          writeText(block, 148, 8, checksum.toString(8).padStart(6, "0") + "\0 ");
          return block;
        }

        function readEntries(archive) {
          const entries = [];
          for (let offset = 0; offset + BLOCK_SIZE <= archive.length;) {
            const block = archive.subarray(offset, offset + BLOCK_SIZE);
            if (block.every((byte) => byte === 0)) break;
            const nul = block.indexOf(0);
            const name = block.subarray(0, nul < 0 ? 100 : nul).toString("utf8");
            const sizeText = block
              .subarray(124, 136)
              .toString("ascii")
              .replace(/\0.*$/, "")
              .trim();
            const size = Number.parseInt(sizeText || "0", 8);
            const dataStart = offset + BLOCK_SIZE;
            entries.push({ name, data: archive.subarray(dataStart, dataStart + size) });
            offset = dataStart + Math.ceil(size / BLOCK_SIZE) * BLOCK_SIZE;
          }
          return entries;
        }

        function c(options, names) {
          const blocks = [];
          for (const name of names) {
            const data = fs.readFileSync(path.join(options.cwd, name));
            blocks.push(header(name, data.length), data);
            const padding = (BLOCK_SIZE - data.length % BLOCK_SIZE) % BLOCK_SIZE;
            blocks.push(Buffer.alloc(padding));
          }
          blocks.push(Buffer.alloc(BLOCK_SIZE * 2));
          fs.writeFileSync(options.file, Buffer.concat(blocks));
        }

        function t(options) {
          const archive = fs.readFileSync(options.file);
          for (const entry of readEntries(archive)) {
            options.onentry({ path: entry.name });
          }
        }

        function x(options) {
          const archive = fs.readFileSync(options.file);
          for (const entry of readEntries(archive)) {
            const target = path.join(options.cwd, entry.name);
            fs.mkdirSync(path.dirname(target), { recursive: true });
            fs.writeFileSync(target, entry.data);
          }
        }

        module.exports = { c, t, x };
        """,
    )


def _verifier_env(home: Path) -> dict[str, str]:
    """Node's environment with every host package search path removed.

    ``NODE_PATH`` adds fallback resolution roots and ``NODE_OPTIONS`` can
    ``--require`` or ``--import`` arbitrary code; either lets a host package
    stand in for a fixture package, which is exactly the host coupling the
    fixture exists to remove. An empty ``NODE_PATH`` disables that fallback.
    ``HOME`` is pointed at an empty directory because Node's global search
    paths (``~/.node_modules``, ``~/.node_libraries``) derive from it; the
    ``--no-global-search-paths`` flag in :func:`_run_verifier` removes the
    compiled-in prefix path as well.
    """
    dropped = {
        "NODE_PATH",
        "NODE_OPTIONS",
        # Preserve-symlinks mode changes the spelling of every resolved path;
        # the confine hook canonicalizes anyway, but the fixture should not
        # inherit a resolution mode from the host at all.
        "NODE_PRESERVE_SYMLINKS",
        "NODE_PRESERVE_SYMLINKS_MAIN",
    }
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env["NODE_PATH"] = ""
    env["HOME"] = str(home)
    return env


#: Preloaded into the verifier's Node process. Node resolves a bare require by
#: walking every ancestor ``node_modules`` directory, and no flag turns that
#: off, so a ``balanced-match`` sitting in ``/tmp/node_modules`` on a
#: persistent runner could satisfy a fixture that deliberately omitted it.
#: This hook makes any resolution that lands outside the bundle a
#: ``MODULE_NOT_FOUND``, exactly as if the ancestor copy were absent. The main
#: script and Node's builtins are exempt; nothing else may load from outside.
_CONFINE_JS = """
"use strict";
const Module = require("module");
const fs = require("fs");
const path = require("path");
const bundle = fs.realpathSync(process.env.KEW_TEST_BUNDLE) + path.sep;
const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  const resolved = resolve.call(this, request, parent, ...rest);
  if (!parent || Module.isBuiltin(request)) return resolved;
  // Compare canonical paths on both sides: under preserve-symlinks mode Node
  // keeps the link spelling of a resolved file while the bundle above is
  // already canonical, and a spelling mismatch must not read as "outside".
  const real = fs.realpathSync(resolved);
  if (!real.startsWith(bundle)) {
    const err = new Error(
      `Cannot find module '${request}' (resolved outside the bundle: ${real})`
    );
    err.code = "MODULE_NOT_FOUND";
    throw err;
  }
  return resolved;
};
"""


def _run_verifier(
    bundle: Path, *, node_flags: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run the overlay verifier against *bundle* in a host-isolated Node.

    *node_flags* are extra Node options a test wants exercised (for example
    ``--preserve-symlinks``); the environment never supplies any.
    """
    home = bundle.parent / "verifier-home"
    home.mkdir(exist_ok=True)
    confine = home / "confine.js"
    confine.write_text(_CONFINE_JS.lstrip())
    env = _verifier_env(home)
    env["KEW_TEST_BUNDLE"] = str(bundle)
    return subprocess.run(
        [
            "node",
            "--no-global-search-paths",
            *node_flags,
            "--require",
            str(confine),
            str(_VERIFIER),
            str(bundle),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestThePinnedToolPreflight:
    """``--check-tools``: the pinned-tool check, and nothing else (#2462).

    The whole suite used to pass on a verification host and then the run failed
    at this gate's version check — the last step of ``scripts/verify.sh`` —
    because the host's trivy was not the pinned one. Twelve minutes burned per
    attempt. ``kew verify`` now runs this mode on the host first
    (:data:`kew.merge_verify.PREFLIGHT_COMMAND`), so the same refusal arrives in
    seconds.
    """

    def _preflight(
        self,
        tmp_path: Path,
        binroot: Path,
        *,
        trivy: str | None,
        pin_stub: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if trivy is not None:
            _shim(
                binroot,
                "trivy",
                f"""
                if [ "$1" = "--version" ]; then
                  echo "Version: {trivy}"
                  exit 0
                fi
                echo "the preflight must ask for nothing but the version" >&2
                exit 9
                """,
            )
        log = tmp_path / "docker-calls.log"
        _shim(binroot, "docker", f'printf \'%s\\n\' "$*" >> "{log}"\nexit 0\n')
        script = _synthetic_scan_script(tmp_path)
        result = _run_scan_script(
            script,
            binroot,
            "--check-tools",
            # The stub directory and the system tools only: a developer machine
            # with its own trivy must not stand in for the host under test.
            extra_env={"PATH": f"{binroot}{os.pathsep}/usr/bin{os.pathsep}/bin"},
            pin_stub=pin_stub,
        )
        assert not log.exists(), (
            "the preflight must not touch docker — it is the cheap check that "
            f"runs before the suite:\n{result.stdout}{result.stderr}"
        )
        return result

    @pytest.fixture()
    def binroot(self, tmp_path: Path) -> Path:
        directory = tmp_path / "bin"
        directory.mkdir()
        return directory

    def test_the_pinned_scanner_passes_and_names_itself(
        self, tmp_path: Path, binroot: Path
    ) -> None:
        result = self._preflight(tmp_path, binroot, trivy=_PINNED_TRIVY)

        assert result.returncode == 0, result.stdout + result.stderr
        assert _PINNED_TRIVY in result.stdout

    def test_a_mismatched_scanner_refuses_without_building_anything(
        self, tmp_path: Path, binroot: Path
    ) -> None:
        result = self._preflight(tmp_path, binroot, trivy="0.75.0")

        assert result.returncode != 0, result.stdout
        assert "not the pinned" in result.stderr
        assert _PINNED_TRIVY in result.stderr

    def test_a_trivy_binary_off_the_pinned_digest_is_refused(
        self, tmp_path: Path, binroot: Path
    ) -> None:
        """#3258 — a scanner answering the pinned version is not thereby it.

        ``trivy --version`` is self-reported: a Homebrew build, or anything at
        all, can print the pin and return clean scans forever. The gate holds
        the installed binary to the SHA-256 ``trivy.pin`` records for the trivy
        inside the pinned release archive.
        """
        result = self._preflight(tmp_path, binroot, trivy=_PINNED_TRIVY, pin_stub=False)

        assert result.returncode != 0, result.stdout
        assert "is not the pinned trivy" in result.stderr
        assert "SHA-256" in result.stderr
        assert "the pinned scanner." not in result.stdout
        assert "brew install" not in result.stderr, (
            "a Homebrew trivy fails the binary check, so the remedy must be the "
            "pinned release archive"
        )

    def test_an_absent_scanner_refuses_and_names_the_pin_file(
        self, tmp_path: Path, binroot: Path
    ) -> None:
        result = self._preflight(tmp_path, binroot, trivy=None)

        assert result.returncode != 0, result.stdout
        assert "trivy not found" in result.stderr
        assert "trivy.pin" in result.stderr, (
            "a host missing the scanner must be told where the version it needs "
            "is written down, which is what provisioning installs it from"
        )

    def test_the_gate_refuses_a_tree_with_no_pin_at_all(self, tmp_path: Path) -> None:
        """The pin is the gate's identity, so its absence is not a default."""
        script = _synthetic_scan_script(tmp_path)
        (tmp_path / "synthetic-repo" / _TRIVY_PIN.relative_to(_REPO_ROOT)).unlink()
        binroot = tmp_path / "empty-bin"
        binroot.mkdir()

        result = _run_scan_script(
            script,
            binroot,
            "--check-tools",
            extra_env={"PATH": f"{binroot}{os.pathsep}/usr/bin{os.pathsep}/bin"},
        )

        assert result.returncode != 0
        assert "no scanner pin" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
class TestOverlayVerifier:
    """The overlay check RUNS the replaced packages; it does not model npm.

    It used to walk the dependency graph and compare semver ranges — a proxy
    for "npm still works" that had to be right about npm's resolver, and four
    review rounds found it wrong four different ways (caret below 0.1.0,
    compound ranges, optional peers, ancestor lookup). Requiring and calling
    the packages is both smaller and stronger: Node does the resolution for
    real, so a dependency that cannot resolve throws instead of being
    adjudicated by a rule of ours.

    These tests drive the real script against fixture bundles.
    """

    @pytest.fixture()
    def bundle(self, tmp_path: Path) -> Path:
        """A hermetic bundle implementing the package behavior under test.

        Every package is written here, never copied from the host. The
        previous fixture copied ``brace-expansion``, ``ip-address`` and ``tar``
        out of the host npm and rewrote only the version string; on
        ubuntu-latest (Node 22, whose bundled npm vendors brace-expansion 2.x
        with a default export only) the "healthy" bundle then failed the
        verifier's named-export check, so the Release job was red on Linux
        while the macOS runners (npm 12, brace-expansion 5.x) stayed green.
        Do not reintroduce host copying.

        The bundle sits one level down so that ``tmp_path / "node_modules"``
        is a test-controlled *ancestor* of it: the search-path regression
        test poisons that directory to prove Node's ancestor walk cannot
        reach past the bundle.
        """
        root = tmp_path / "work" / "node_modules"
        root.mkdir(parents=True)
        expected = _expected_versions()
        _write_balanced_match(root)
        _write_brace_expansion(root, expected["brace-expansion"])
        _write_ip_address(root, expected["ip-address"])
        _write_tar(root, expected["tar"])
        _write_undici(root, expected["undici"])
        for index in range(50):
            _pkg(root, f"pad-{index:02d}", "0.0.0")
        return root

    def test_a_healthy_bundle_verifies(self, bundle: Path) -> None:
        result = _run_verifier(bundle)
        assert result.returncode == 0, result.stderr
        assert "each exercised" in result.stdout

    @pytest.mark.parametrize(
        "package", ["brace-expansion", "ip-address", "tar", "undici"]
    )
    def test_a_version_that_did_not_land_fails(
        self, bundle: Path, package: str
    ) -> None:
        manifest = bundle / package / "package.json"
        data = json.loads(manifest.read_text())
        data["version"] = "0.0.0"
        manifest.write_text(json.dumps(data))
        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "the copy did not land" in result.stderr

    def test_a_missing_undici_overlay_fails(self, bundle: Path) -> None:
        shutil.rmtree(bundle / "undici")

        result = _run_verifier(bundle)

        assert result.returncode != 0
        assert "undici" in result.stderr

    def test_an_undici_request_that_throws_fails(self, bundle: Path) -> None:
        package = bundle / "undici"
        (package / "stub.js").write_text(
            "module.exports = {"
            "request() { throw new Error('undici request is broken'); },"
            "fetch() {},"
            "Agent: class { close() { return Promise.resolve(); } }"
            "};"
        )
        manifest = json.loads((package / "package.json").read_text())
        manifest["main"] = "stub.js"
        manifest.pop("exports", None)
        manifest.pop("type", None)
        (package / "package.json").write_text(json.dumps(manifest))

        result = _run_verifier(bundle)

        assert result.returncode != 0
        assert "undici request is broken" in result.stderr

    def test_a_nested_older_copy_behind_exports_fails(self, bundle: Path) -> None:
        parent = bundle / "tar"
        nested = _write_tar(parent / "node_modules", "7.5.11")
        nested_manifest = nested / "package.json"
        nested_data = json.loads(nested_manifest.read_text())

        entry = str(nested_data.get("main", "index.js")).removeprefix("./")
        redirected = f"./node_modules/tar/{entry}"
        parent_manifest = parent / "package.json"
        parent_data = json.loads(parent_manifest.read_text())
        parent_data["main"] = redirected
        parent_data["exports"] = redirected
        parent_manifest.write_text(json.dumps(parent_data))

        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "loads code outside its own package directory" in result.stderr

    def test_a_package_carrying_nested_node_modules_fails(self, bundle: Path) -> None:
        _pkg(bundle / "tar" / "node_modules", "shadow-copy", "0.0.0")

        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "tar: package carries a nested node_modules" in result.stderr

    def test_a_default_only_brace_export_fails(self, bundle: Path) -> None:
        package = bundle / "brace-expansion"
        (package / "stub.js").write_text(
            'module.exports = { default: () => ["abd", "acd"] };'
        )
        manifest = json.loads((package / "package.json").read_text())
        manifest["main"] = "stub.js"
        manifest.pop("exports", None)
        manifest.pop("type", None)
        (package / "package.json").write_text(json.dumps(manifest))

        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "named expand export" in result.stderr

    def test_tar_that_cannot_extract_fails(self, bundle: Path) -> None:
        package = bundle / "tar"
        (package / "stub.js").write_text(
            "module.exports = {"
            "c: ({ file }) => require('fs').writeFileSync(file, 'archive'),"
            "t: ({ onentry }) => onentry({ path: 'f.txt' }),"
            "x: () => {}"
            "};"
        )
        manifest = json.loads((package / "package.json").read_text())
        manifest["main"] = "stub.js"
        manifest.pop("exports", None)
        manifest.pop("type", None)
        (package / "package.json").write_text(json.dumps(manifest))

        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "tar extraction is broken" in result.stderr

    def test_a_staging_directory_is_not_accepted_as_the_bundle(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        for package in ("brace-expansion", "ip-address", "tar", "undici"):
            shutil.copytree(bundle / package, staging / package)
        balanced_match = bundle / "balanced-match"
        if balanced_match.exists():
            shutil.copytree(balanced_match, staging / "balanced-match")
        for index in range(50):
            (staging / f"padding-{index:02d}").mkdir()

        result = _run_verifier(staging)
        assert result.returncode != 0
        assert "does not look like npm's bundled node_modules" in result.stderr

    def test_a_missing_transitive_dependency_fails(self, bundle: Path) -> None:
        # The case the graph walk existed for. Requiring the package finds it
        # too, because Node performs the resolution rather than modelling it.
        shutil.rmtree(bundle / "brace-expansion" / "node_modules", ignore_errors=True)
        shutil.rmtree(bundle / "balanced-match", ignore_errors=True)
        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "Cannot find module 'balanced-match'" in result.stderr

    def test_a_symlink_spelled_bundle_path_still_verifies(
        self, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Temp roots are often symlinks (/var -> /private/var on macOS). The
        # confine hook must compare canonical paths on both sides, so a
        # healthy bundle reached through a link is not rejected as "outside",
        # even when the host asks Node to preserve symlink spellings.
        link = tmp_path / "link"
        link.symlink_to(bundle.parent, target_is_directory=True)
        # The env form is dropped by _verifier_env; the flag form is what
        # actually puts this Node into preserve-symlinks mode.
        monkeypatch.setenv("NODE_PRESERVE_SYMLINKS", "1")
        result = _run_verifier(
            link / "node_modules",
            node_flags=("--preserve-symlinks", "--preserve-symlinks-main"),
        )
        assert result.returncode == 0, result.stderr

    def test_a_host_package_on_any_search_path_cannot_stand_in(
        self, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fixture must not depend on the host: a balanced-match reachable
        # only through NODE_PATH, through HOME's global search paths, or
        # through an ANCESTOR node_modules of the bundle must not satisfy the
        # missing-dependency case. The ancestor is the one no Node flag can
        # switch off; the confine preload in _run_verifier is what stops it.
        host = tmp_path / "host_modules"
        _write_balanced_match(host)
        poisoned_home = tmp_path / "poisoned_home"
        _write_balanced_match(poisoned_home / ".node_modules")
        _write_balanced_match(poisoned_home / ".node_libraries")
        assert bundle.parent.parent == tmp_path
        _write_balanced_match(tmp_path / "node_modules")
        monkeypatch.setenv("NODE_PATH", str(host))
        monkeypatch.setenv("NODE_OPTIONS", "--no-warnings")
        monkeypatch.setenv("HOME", str(poisoned_home))
        shutil.rmtree(bundle / "brace-expansion" / "node_modules", ignore_errors=True)
        shutil.rmtree(bundle / "balanced-match", ignore_errors=True)
        result = _run_verifier(bundle)
        assert result.returncode != 0
        assert "Cannot find module 'balanced-match'" in result.stderr

    @pytest.mark.parametrize(
        ("package", "stub"),
        [
            ("brace-expansion", "module.exports = { expand: () => ['wrong'] };"),
            ("tar", "module.exports = { c: () => {}, t: () => {} };"),
            (
                "ip-address",
                "module.exports = { Address4: class { correctForm() "
                "{ return '0.0.0.0'; } } };",
            ),
            (
                "undici",
                "module.exports = { request() {}, fetch() {}, "
                "Agent: class { close() { return Promise.reject("
                "new Error('undici is broken')); } } };",
            ),
        ],
    )
    def test_a_package_that_loads_but_misbehaves_fails(
        self, bundle: Path, package: str, stub: str
    ) -> None:
        """Each replaced package is CALLED, not merely required.

        Requiring proves the dependency chain resolves. It does not prove the
        package does its job — a stub that loads cleanly and returns nonsense
        passes a require-only check. These stubs load fine and answer wrongly,
        so they pin the functional assertions rather than the imports.
        """
        pkg = bundle / package
        (pkg / "stub.js").write_text(stub)
        manifest = json.loads((pkg / "package.json").read_text())
        manifest["main"] = "stub.js"
        # `exports` wins over `main`, and `"type": "module"` makes Node read
        # this CommonJS stub as ESM and fail to LOAD it — which would test
        # requiring, not behaviour. Drop both so the stub actually runs.
        manifest.pop("exports", None)
        manifest.pop("type", None)
        (pkg / "package.json").write_text(json.dumps(manifest))
        result = _run_verifier(bundle)
        assert result.returncode != 0, (
            f"a {package} that loads but returns the wrong answer must fail:\n"
            + result.stdout
        )
        assert "is broken" in result.stderr

    def test_a_broken_package_fails(self, bundle: Path) -> None:
        # A package present at the right version but not functioning.
        (bundle / "ip-address" / "package.json").write_text(
            json.dumps({"name": "ip-address", "version": "10.3.1", "main": "nope.js"})
        )
        # Node falls back to index.js when a package's main file is missing.
        (bundle / "ip-address" / "index.js").unlink()
        result = _run_verifier(bundle)
        assert result.returncode != 0
        # The failure must be THIS package failing to load, not a Node startup
        # error or an unrelated verifier exception.
        assert "Cannot find module" in result.stderr
        assert "ip-address" in result.stderr


def _gate_binroot(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    _shim(
        directory,
        "docker",
        """
        case "$1" in
          info) exit 0 ;;
          build)
            printf '%s\n' "$*" >> "$(dirname "$0")/docker-build-argv.log"
            shift
            tag=
            iidfile=
            no_cache_count=0
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -t|--tag) tag="$2"; shift 2 ;;
                --iidfile) iidfile="$2"; shift 2 ;;
                --no-cache) no_cache_count=$((no_cache_count + 1)); shift ;;
                *) shift ;;
              esac
            done
            [ -n "$tag" ] && [ -n "$iidfile" ] || exit 1
            hash_code='import hashlib, sys;'
            hash_code="$hash_code data = sys.argv[1].encode();"
            hash_code="$hash_code print(hashlib.sha256(data).hexdigest())"
            digest="$(python3 -c "$hash_code" "$tag")"
            image_id="sha256:$digest"
            printf '%s\n' "$image_id" > "$iidfile"
            build_log="$(dirname "$0")/docker-builds.log"
            printf '%s %s\n' "$tag" "$image_id" >> "$build_log"
            args_log="$(dirname "$0")/docker-build-args.log"
            printf '%s %s\n' "$tag" "$no_cache_count" >> "$args_log"
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )
    return directory


_SHARED_SCAN_REFS = frozenset(
    {
        "kew-agent:ci-scan",
        "kew-agent-opencode:ci-scan",
        "kew-agent-codex:ci-scan",
        "kew-switchyard:ci-scan",
    }
)


def _daemon_docker(binroot: Path) -> None:
    """A fake docker whose images exist only while some reference names them.

    It keeps one file per image reference under ``refs/``, and an image ID is
    present exactly when at least one reference still holds it: the containerd
    image store the operator Mac's daemon uses, which keeps no dangling images
    and deletes an image the moment its last name goes. ``build`` writes the ID
    the way the shared gate fake does (derived from the tag), so the trivy fake
    still tells the proxy image from an agent image. With
    ``FAKE_REBUILD_DURING_BUILD`` set, another run moves the shared tag before
    ``build`` returns, so the ID it reports is already gone.
    """
    _shim(
        binroot,
        "docker",
        """
        store="$(dirname "$0")/refs"
        mkdir -p "$store"
        ref_file() { printf '%s/%s' "$store" "$(printf '%s' "$1" | tr ':/' '@%')"; }
        present() { grep -rqxF -- "$1" "$store"; }
        case "$1" in
          info) exit 0 ;;
          build)
            shift
            tags=()
            iidfile=
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -t|--tag) tags+=("$2"); shift 2 ;;
                --iidfile) iidfile="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            [ "${#tags[@]}" -gt 0 ] && [ -n "$iidfile" ] || exit 1
            hash_code='import hashlib, sys;'
            hash_code="$hash_code data = sys.argv[1].encode();"
            hash_code="$hash_code print(hashlib.sha256(data).hexdigest())"
            image_id="sha256:$(python3 -c "$hash_code" "${tags[0]}")"
            for tag in "${tags[@]}"; do
              printf '%s\\n' "$image_id" > "$(ref_file "$tag")"
            done
            printf '%s\\n' "$image_id" > "$iidfile"
            printf '%s %s\\n' "${tags[0]}" "$image_id" \\
              >> "$(dirname "$0")/docker-builds.log"
            if [ -n "${FAKE_REBUILD_DURING_BUILD:-}" ]; then
              printf 'sha256:%064d\\n' 0 > "$(ref_file "${tags[0]}")"
            fi
            exit 0 ;;
          images)
            repository="${!#}"
            for file in "$store"/*; do
              [ -e "$file" ] || continue
              name="$(basename "$file" | tr '@%' ':/')"
              [ "${name%%:*}" = "$repository" ] && printf '%s\\n' "$name"
            done
            exit 0 ;;
          tag)
            case "$2" in
              sha256:*) image_id="$2" ;;
              *) image_id="$(cat "$(ref_file "$2")" 2>/dev/null)" ;;
            esac
            if [ -z "$image_id" ] || ! present "$image_id"; then
              echo "Error response from daemon: No such image: $2" >&2
              exit 1
            fi
            printf '%s\\n' "$image_id" > "$(ref_file "$3")"
            exit 0 ;;
          rmi)
            shift
            for ref in "$@"; do
              if [ -e "$(ref_file "$ref")" ]; then
                rm -f "$(ref_file "$ref")"
              else
                echo "Error response from daemon: No such image: $ref" >&2
                exit 1
              fi
            done
            exit 0 ;;
          references)
            for file in "$store"/*; do
              [ -e "$file" ] || continue
              name="$(basename "$file" | tr '@%' ':/')"
              printf '%s %s\\n' "$name" "$(cat "$file")"
            done
            exit 0 ;;
          *) exit 0 ;;
        esac
        """,
    )


def _concurrent_rebuild_before_every_read(binroot: Path) -> None:
    """Put a second gate run on the same daemon in front of every trivy read.

    Before each ``trivy image <id>``, the other run rebuilds every shared
    ``:ci-scan`` tag, so each moves to an ID this run never built, and the
    daemon fake drops any image no reference still holds. Then trivy reads
    the target or fails the way the real one did: "No such image".
    """
    (binroot / "trivy").rename(binroot / "trivy-inner")
    _shim(
        binroot,
        "trivy",
        """
        target="${!#}"
        case "$target" in
          sha256:*)
            store="$(dirname "$0")/refs"
            counter="$(dirname "$0")/other-run-builds"
            builds="$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))"
            printf '%s\\n' "$builds" > "$counter"
            for ref in "$store"/*@ci-scan; do
              [ -e "$ref" ] || continue
              printf 'sha256:%064d\\n' "$builds" > "$ref"
            done
            if ! grep -rqxF -- "$target" "$store"; then
              echo "unable to inspect the image ($target): No such image" >&2
              exit 1
            fi ;;
        esac
        exec "$(dirname "$0")/trivy-inner" "$@"
        """,
    )


def _image_refs(binroot: Path) -> dict[str, str]:
    """Every reference the fake daemon still holds, as ``{ref: image ID}``."""
    listing = subprocess.run(
        [str(binroot / "docker"), "references"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return dict(line.split() for line in listing.stdout.splitlines())


def _switchyard_rust_packages(count: int = 50) -> list[dict[str, str]]:
    packages = [
        {
            "ID": f"switchyard-server@{_SWITCHYARD_VERSION}",
            "Name": "switchyard-server",
            "Version": _SWITCHYARD_VERSION,
            "Relationship": "root",
        }
    ]
    packages.extend(
        {
            "ID": f"synthetic-dependency-{index}@1.0.{index}",
            "Name": f"synthetic-dependency-{index}",
            "Version": f"1.0.{index}",
            "Relationship": "direct" if index < 5 else "indirect",
        }
        for index in range(1, count)
    )
    return packages


def _switchyard_result(
    packages: list[dict[str, str]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "Target": "usr/local/bin/switchyard-server",
        "Class": "lang-pkgs",
        "Type": "rustbinary",
        "Packages": packages if packages is not None else _switchyard_rust_packages(),
    }
    result.update(overrides)
    return result


def _os_inventory_result(count: int = 100) -> dict[str, object]:
    return {
        "Class": "os-pkgs",
        "Type": "debian",
        "Packages": [{"Name": f"deb{index}"} for index in range(count)],
    }


class TestTheGateActuallyGates:
    """The `--exit-code 3` scan IS the gate. Nothing tested it.

    (It was `--exit-code 1` until #3195: trivy's own fatal errors exit 1 too,
    so a findings exit of its own is what tells a finding from a scan that
    could not run.)

    Every other refusal test fails BEFORE the gate command runs, so the suite
    passed with `--exit-code 1` changed to `--exit-code 0`, with `|| true`
    appended, and with a `break` after the first image (round-4 review,
    Minor 4). The guard deciding whether a CVE stops the build was the one
    guard in this file with no test at all.
    """

    @pytest.fixture()
    def binroot(self, tmp_path: Path) -> Path:
        return _gate_binroot(tmp_path)

    def _trivy(
        self,
        binroot: Path,
        *,
        gate_exit: int,
        log: Path,
        proxy_gate_exit: int = 0,
        gate_error: int = 0,
        proxy_rust_packages: int = 50,
        proxy_results: list[dict[str, object]] | None = None,
        proxy_scan_tag: str = "ci-scan",
    ) -> None:
        stamp = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        os_inventory = _os_inventory_result()
        agent_inventory = json.dumps(
            {
                "Results": [
                    os_inventory,
                    {
                        "Class": "lang-pkgs",
                        "Type": "node-pkg",
                        "Packages": [{"Name": f"npm{index}"} for index in range(100)],
                    },
                    {
                        "Target": "usr/local/bin/unrelated-agent-tool",
                        "Class": "lang-pkgs",
                        "Type": "rustbinary",
                        "Packages": _switchyard_rust_packages(100),
                    },
                ]
            },
            separators=(",", ":"),
        )
        if proxy_results is None:
            proxy_results = [
                os_inventory,
                _switchyard_result(_switchyard_rust_packages(proxy_rust_packages)),
            ]
        proxy_inventory = json.dumps({"Results": proxy_results}, separators=(",", ":"))
        proxy_image_id = (
            "sha256:"
            + hashlib.sha256(f"kew-switchyard:{proxy_scan_tag}".encode()).hexdigest()
        )
        _shim(
            binroot,
            "trivy",
            f"""
            printf '%s\\n' "$*" >> {log}
            if [ "$1" = "--version" ]; then echo "Version: {_PINNED_TRIVY}"; exit 0; fi
            if [ "$1" = "version" ]; then
              echo '{{"Version":"{_PINNED_TRIVY}",'\\
                   '"VulnerabilityDB":{{"UpdatedAt":"{stamp}"}}}}'
              exit 0
            fi
            if printf '%s\\n' "$@" | grep -q -- --download-db-only; then exit 0; fi
            if printf '%s\\n' "$@" | grep -q -- --list-all-pkgs; then
              target="${{!#}}"
              if [ "$target" = "{proxy_image_id}" ]; then
                printf '%s\n' '{proxy_inventory}'
              else
                printf '%s\n' '{agent_inventory}'
              fi
              exit 0
            fi
            finding_exit=0
            previous=
            for argument in "$@"; do
              if [ "$previous" = "--exit-code" ] && [ "$argument" != "0" ]; then
                finding_exit="$argument"
              fi
              previous="$argument"
            done
            # Like trivy: a finding exits with the requested --exit-code; a
            # scanner that cannot scan exits with an error of its own.
            if [ "$finding_exit" != 0 ]; then
              if printf '%s\\n' "$@" | grep -q -- --ignore-unfixed; then
                if [ {gate_error} != 0 ]; then
                  echo "FATAL scan error"
                  exit {gate_error}
                fi
                echo "a fixable CRITICAL was found"
                [ {gate_exit} = 0 ] || exit "$finding_exit"
                exit 0
              fi
              echo "an unfixed proxy CRITICAL was found"
              [ {proxy_gate_exit} = 0 ] || exit "$finding_exit"
              exit 0
            fi
            echo "unfiltered report"
            exit 0
            """,
        )

    def test_a_fixable_finding_fails_the_gate(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=1, log=log)
        result = _run_gate(binroot)
        assert result.returncode != 0, (
            "a scanner reporting a fixable CRITICAL/HIGH must fail the gate — "
            "that is the entire point of the job:\n" + result.stdout
        )
        assert "\u2713" not in result.stdout

    def test_a_finding_in_one_image_does_not_stop_the_scan_of_the_next(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """Every image is gated, and the run fails at the end (#3195)."""
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=1, log=log)
        result = _run_gate(binroot)
        assert result.returncode != 0
        calls = log.read_text()
        expected = len(_AGENT_DOCKERFILES)
        assert calls.count("--ignore-unfixed") == expected, calls
        assert result.stdout.count("==> triage input:") >= expected
        assert (
            f"the gate found CRITICAL/HIGH findings in {expected} image(s)"
            in result.stderr
        )
        assert "✓" not in result.stdout

    def test_a_failed_build_of_one_image_still_scans_the_rest_and_fails_at_the_end(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """One image's broken build is recorded, and every other image is gated.

        A failed ``docker build`` used to end the run under ``set -e``, so the
        images after it were never scanned. Now it is recorded with the image
        and why, the scan goes on, and the run fails at the end naming it.
        """
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        docker = binroot / "docker"
        broken = "kew-agent-opencode:ci-scan"
        original = docker.read_text()
        failing = original.replace(
            '[ -n "$tag" ] && [ -n "$iidfile" ] || exit 1\n',
            '[ -n "$tag" ] && [ -n "$iidfile" ] || exit 1\n'
            f'[ "$tag" = "{broken}" ] && echo "build exploded" >&2 && exit 1\n',
            1,
        )
        assert failing != original, "the fake build moved; re-point this failure"
        docker.write_text(failing)

        result = _run_gate(binroot, {"KEW_SWITCHYARD_IMAGE": ""})

        assert result.returncode != 0, result.stdout
        expected = len(_AGENT_DOCKERFILES)
        assert log.read_text().count("--ignore-unfixed") == expected - 1
        built = (binroot / "docker-builds.log").read_text()
        # The broken image is the middle one: the image after it was built.
        assert "kew-agent-codex:ci-scan" in built
        assert broken not in built
        assert (
            f"scan-agent-image.sh: gate error in {broken}: docker build of "
            "docker/agent-opencode.Dockerfile failed" in result.stderr
        )
        assert "gated every image:" in result.stderr
        assert "the gate could not scan 1 image(s):" in result.stderr
        assert f"{broken} (docker build of" in result.stderr
        assert "\u2713" not in result.stdout

    def test_an_inventory_floor_failure_in_one_image_still_scans_the_rest(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        trivy = binroot / "trivy"
        # The fake build derives each image ID from its tag.
        broken_id = "sha256:" + hashlib.sha256(b"kew-agent:ci-scan").hexdigest()
        empty = """'{"Results":[]}'"""
        original = trivy.read_text()
        failing = original.replace(
            'target="${!#}"\n',
            'target="${!#}"\n'
            f'[ "$target" = "{broken_id}" ] && echo {empty} && exit 0\n',
            1,
        )
        assert failing != original, "the fake inventory moved; re-point this failure"
        trivy.write_text(failing)

        result = _run_gate(binroot, {"KEW_SWITCHYARD_IMAGE": ""})

        assert result.returncode != 0, result.stdout
        expected = len(_AGENT_DOCKERFILES)
        assert log.read_text().count("--ignore-unfixed") == expected - 1
        assert (
            "scan-agent-image.sh: gate error in kew-agent:ci-scan: inventoried 0 OS "
            "packages" in result.stderr
        )
        assert "gated every image:" in result.stderr
        assert "\u2713" not in result.stdout

    def test_a_scanner_error_in_the_gate_is_reported_apart_from_a_finding(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """trivy's own fatal exit is a scan that could not run (#3195)."""
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, gate_error=1, log=log)
        result = _run_gate(binroot)
        assert result.returncode != 0
        expected = len(_AGENT_DOCKERFILES)
        assert result.stderr.count("scan-agent-image.sh: gate error in ") == expected
        assert f"the gate could not scan {expected} image(s)" in result.stderr
        assert "gate findings in" not in result.stderr
        assert "the gate found CRITICAL/HIGH findings" not in result.stderr

    def test_a_clean_scan_passes_and_scans_every_agent_image(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        result = _run_gate(binroot)
        assert result.returncode == 0, result.stderr
        calls = log.read_text()
        # Every image, not just the first: a `break` after one iteration left
        # the opencode image unscanned and the suite green.
        expected = len(_AGENT_DOCKERFILES)
        assert calls.count("--ignore-unfixed") == expected, (
            f"the gate scan must run once per agent image, and there are "
            f"{expected}:\n" + calls
        )
        assert "--exit-code 3" in calls, (
            "the gate scan must carry --exit-code 3, or a finding cannot fail it "
            "apart from a scanner error"
        )

    def test_the_shipped_scanner_pin_is_never_rewritten_by_a_test(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        self._trivy(binroot, gate_exit=0, log=tmp_path / "calls.log")
        shipped = _TRIVY_PIN.read_bytes()

        with pytest.raises(ValueError, match="real checkout"):
            pin_stub_trivy(_REPO_ROOT, binroot / "trivy")

        assert _TRIVY_PIN.read_bytes() == shipped

    def _gate_with_binary_pins(
        self, binroot: Path, tmp_path: Path, digest_for: Callable[[str], str]
    ) -> subprocess.CompletedProcess[str]:
        """The real gate in a synthetic checkout whose four trivy binary pins
        are ``digest_for(key)``, run with this machine's ``uname``."""
        script = _synthetic_scan_script(tmp_path)
        pin_trivy_binaries(script.parent.parent, digest_for)
        return _run_scan_script(script, binroot, pin_stub=False)

    def test_only_this_platforms_binary_pin_is_the_one_the_gate_checks(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """The stub's digest is pinned under this host's platform key alone;
        the other three name some other binary. The gate maps ``uname`` to that
        key and runs on, so a mapping that read any other key would refuse."""
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        stub = hashlib.sha256((binroot / "trivy").read_bytes()).hexdigest()
        here = host_trivy_pin_key()

        result = self._gate_with_binary_pins(
            binroot, tmp_path, lambda key: stub if key == here else "e" * 64
        )

        assert result.returncode == 0, result.stderr
        assert "is not the pinned trivy" not in result.stderr
        assert log.read_text().count("--ignore-unfixed") == len(_AGENT_DOCKERFILES)

    def test_a_scanner_off_this_platforms_binary_pin_is_refused_before_any_scan(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """Every other platform key names the stub; this host's key names
        another binary. The gate refuses before it builds or scans anything,
        so a mapping that read another platform's key, or a digest check that
        only warned, would let the stub through."""
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        stub = hashlib.sha256((binroot / "trivy").read_bytes()).hexdigest()
        here = host_trivy_pin_key()
        other = "e" * 64

        result = self._gate_with_binary_pins(
            binroot, tmp_path, lambda key: other if key == here else stub
        )

        assert result.returncode != 0, result.stdout
        assert "is not the pinned trivy" in result.stderr
        assert f"its SHA-256 is {stub}" in result.stderr
        assert f"pins {other} for this platform" in result.stderr
        calls = log.read_text() if log.exists() else ""
        assert "--ignore-unfixed" not in calls
        assert "--list-all-pkgs" not in calls
        assert not (binroot / "docker-builds.log").exists()
        assert "✓" not in result.stdout

    def test_the_success_line_certifies_the_number_actually_gated(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """The gate's own certification has to be arithmetic, not a word.

        It read "both agent images" while `IMAGES` held three, so a clean run
        of the three-image gate signed off on two — an operator reading the
        certification could not tell the codex lane had been scanned at all
        (#2317 review). Derived from the Dockerfiles here, so the next agent
        image fails this instead of quietly widening the gap.
        """
        self._trivy(binroot, gate_exit=0, log=tmp_path / "calls.log")

        # The default run: every machine that has not opted into the
        # Switchyard layer takes this branch, and it is the branch whose
        # certification named a number.
        result = _run_gate(binroot, {"KEW_SWITCHYARD_IMAGE": ""})

        assert result.returncode == 0, result.stderr
        expected = len(_AGENT_DOCKERFILES)
        assert f"\u2713 all {expected} agent images" in result.stdout, result.stdout
        # And no counting word can stand in for the number again.
        assert "both agent images" not in result.stdout

    def test_a_partial_sweep_refuses_to_certify_anything(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """The certification counts what the loop gated, not what it declared.

        A `break` after the first image is the exact mutation this class was
        written for, and a count read off `IMAGES` alone would have printed
        "all 3 agent images passed" over a run that scanned one.
        """
        self._trivy(binroot, gate_exit=0, log=tmp_path / "calls.log")
        script = _synthetic_scan_script(tmp_path)
        original = script.read_text()
        mutated = original.replace(
            '    echo "==> building $dockerfile -> $tag"\n',
            '    if [ "$agent_images_gated" -ge 1 ]; then break; fi\n'
            '    echo "==> building $dockerfile -> $tag"\n',
            1,
        )
        assert mutated != original, "the build step moved; re-point this mutation"
        script.write_text(mutated)

        result = _run_scan_script(script, binroot)

        assert result.returncode != 0, result.stdout
        assert "Refusing to certify a partial run" in result.stderr
        assert "\u2713" not in result.stdout

    def test_agent_builds_bypass_cache_without_changing_proxy_cache_policy(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)

        result = _run_gate(binroot)

        assert result.returncode == 0, result.stderr
        no_cache_counts = {
            tag: int(count)
            for tag, count in (
                line.split()
                for line in (binroot / "docker-build-args.log").read_text().splitlines()
            )
        }
        assert no_cache_counts == {
            "kew-agent:ci-scan": 1,
            "kew-agent-opencode:ci-scan": 1,
            "kew-agent-codex:ci-scan": 1,
            "kew-switchyard:ci-scan": 0,
        }

    def test_the_proxy_build_reads_nothing_but_the_build_context(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """The proxy source is public and fetched by the Dockerfile itself, so
        its build takes no secret, build argument or extra context."""
        self._trivy(binroot, gate_exit=0, log=tmp_path / "calls.log")

        result = _run_gate(binroot)

        assert result.returncode == 0, result.stderr
        proxy_builds = [
            line.split()
            for line in (binroot / "docker-build-argv.log").read_text().splitlines()
            if "kew-switchyard:ci-scan" in line
        ]
        assert len(proxy_builds) == 1, proxy_builds
        argv = proxy_builds[0]
        assert argv[:5] == [
            "build",
            "-f",
            "docker/switchyard.Dockerfile",
            "-t",
            "kew-switchyard:ci-scan",
        ]
        assert argv[5] == "--iidfile"
        assert argv[7:] == ["."], argv

    def test_build_switchyard_only_prints_only_the_built_image_id(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        script = _synthetic_scan_script(tmp_path)

        result = _run_scan_script(script, binroot, "--build-switchyard-only")

        assert result.returncode == 0, result.stderr
        image_id = "sha256:" + hashlib.sha256(b"kew-switchyard:ci-scan").hexdigest()
        assert result.stdout == f"{image_id}\n"
        builds = (binroot / "docker-build-args.log").read_text().splitlines()
        # Uncached: the image the opted-in test starts is built from the pins
        # as they are now, not from a layer an earlier build left behind.
        assert builds == ["kew-switchyard:ci-scan 1"]

    def test_build_switchyard_only_fails_without_an_image_id_when_the_build_fails(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        _shim(binroot, "docker", 'echo "build exploded" >&2\nexit 1\n')
        script = _synthetic_scan_script(tmp_path)

        result = _run_scan_script(script, binroot, "--build-switchyard-only")

        assert result.returncode != 0
        assert result.stdout == ""
        assert "docker build of docker/switchyard.Dockerfile failed" in result.stderr

    def _check_switchyard_scan_gates_all_high_and_critical_findings(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, proxy_gate_exit=1, log=log)
        result = _run_gate(binroot)
        assert result.returncode != 0
        assert "unfixed proxy CRITICAL" in result.stdout
        proxy_id = "sha256:" + hashlib.sha256(b"kew-switchyard:ci-scan").hexdigest()
        proxy_calls = [
            line
            for line in log.read_text().splitlines()
            if line.endswith(f" {proxy_id}")
        ]
        gate_call = next(line for line in proxy_calls if "--exit-code 3" in line)
        assert "--severity CRITICAL,HIGH" in gate_call
        assert "--ignore-unfixed" not in gate_call

    def _check_switchyard_inventory_uses_os_and_rust_floors(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_rust_packages=3,
        )
        result = _run_gate(binroot)
        assert result.returncode != 0
        assert "Rust packages" in result.stderr
        assert "npm packages" not in result.stderr

    def test_switchyard_inventory_counts_only_the_exact_binary_result(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        unrelated = _switchyard_result(
            _switchyard_rust_packages(300),
            Target="usr/local/bin/unrelated-helper",
        )
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[
                _os_inventory_result(),
                unrelated,
                _switchyard_result(_switchyard_rust_packages(50)),
            ],
        )

        result = _run_gate(binroot)

        assert result.returncode == 0, result.stderr
        assert "50 Rust packages" in result.stdout
        assert "350 Rust packages" not in result.stdout

    @pytest.mark.parametrize(
        "matching_results",
        [
            [],
            [
                _switchyard_result(
                    Target="/usr/local/bin/switchyard-server",
                )
            ],
            [_switchyard_result(Target="usr/local/bin/other")],
            [_switchyard_result(Class="os-pkgs")],
            [_switchyard_result(Type="cargo")],
            [_switchyard_result(), _switchyard_result()],
        ],
        ids=[
            "missing",
            "leading-slash-target",
            "wrong-target",
            "wrong-class",
            "wrong-type",
            "duplicate-result",
        ],
    )
    def test_switchyard_inventory_requires_one_exact_result(
        self,
        binroot: Path,
        tmp_path: Path,
        matching_results: list[dict[str, object]],
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[_os_inventory_result(), *matching_results],
        )

        result = _run_gate(binroot)

        assert result.returncode != 0
        assert "Rust inventory result is missing or duplicated" in result.stderr

    def test_switchyard_inventory_requires_the_exact_root_package(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        packages = _switchyard_rust_packages()
        packages[0] = {
            "ID": "wrong-root@0.1.0",
            "Name": "wrong-root",
            "Version": "0.1.0",
            "Relationship": "root",
        }
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[_os_inventory_result(), _switchyard_result(packages)],
        )

        result = _run_gate(binroot)

        assert result.returncode != 0
        assert "root package mismatch" in result.stderr

    def test_switchyard_inventory_rejects_duplicate_package_ids(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        packages = _switchyard_rust_packages()
        packages.append(dict(packages[-1]))
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[_os_inventory_result(), _switchyard_result(packages)],
        )

        result = _run_gate(binroot)

        assert result.returncode != 0
        assert "duplicate package IDs" in result.stderr

    @pytest.mark.parametrize(
        "malformation", ["missing-packages", "missing-id", "bad-id"]
    )
    def test_switchyard_inventory_rejects_malformed_package_identities(
        self,
        binroot: Path,
        tmp_path: Path,
        malformation: str,
    ) -> None:
        log = tmp_path / "calls.log"
        packages = _switchyard_rust_packages()
        result_record = _switchyard_result(packages)
        if malformation == "missing-packages":
            result_record.pop("Packages")
        elif malformation == "missing-id":
            packages[1].pop("ID")
        else:
            packages[1]["ID"] = "does-not-match-name-and-version"
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[_os_inventory_result(), result_record],
        )

        result = _run_gate(binroot)

        assert result.returncode != 0
        assert "malformed package identities" in result.stderr

    def test_switchyard_inventory_requires_fifty_distinct_packages(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_results=[
                _os_inventory_result(),
                _switchyard_result(_switchyard_rust_packages(49)),
            ],
        )

        result = _run_gate(binroot)

        assert result.returncode != 0
        assert "only 49 Rust packages" in result.stderr

    def test_every_image_scan_targets_the_built_image_id(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        result = _run_gate(binroot)
        assert result.returncode == 0, result.stderr

        build_lines = (binroot / "docker-builds.log").read_text().splitlines()
        build_records = [tuple(line.split()) for line in build_lines]
        builds = dict(build_records)
        assert set(builds) == {
            "kew-agent:ci-scan",
            "kew-agent-opencode:ci-scan",
            "kew-agent-codex:ci-scan",
            "kew-switchyard:ci-scan",
        }
        assert len(set(builds.values())) == 4, builds

        scan_calls = [
            line
            for line in log.read_text().splitlines()
            if line.startswith("image ") and "--download-db-only" not in line
        ]
        assert len(scan_calls) == 12, scan_calls
        scanned_ids = {line.rsplit(" ", 1)[-1] for line in scan_calls}
        assert scanned_ids == set(builds.values()), (
            "the scans must use the image IDs produced by their builds:\n"
            + "\n".join(scan_calls)
        )
        for index, (tag, image_id) in enumerate(build_records):
            matching = scan_calls[index * 3 : (index + 1) * 3]
            assert len(matching) == 3 and all(
                line.endswith(f" {image_id}") for line in matching
            ), f"every scan for {tag} must use its own build's image ID:\n" + "\n".join(
                scan_calls
            )
        assert not any(":ci-scan" in line for line in scan_calls)

    def _check_switchyard_image_is_scanned_by_immutable_id(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        result = _run_gate(binroot)
        assert result.returncode == 0, result.stderr
        builds = dict(
            tuple(line.split())
            for line in (binroot / "docker-builds.log").read_text().splitlines()
        )
        image_id = builds["kew-switchyard:ci-scan"]
        proxy_scans = [
            line
            for line in log.read_text().splitlines()
            if line.startswith("image ")
            and "--download-db-only" not in line
            and line.endswith(f" {image_id}")
        ]
        assert len(proxy_scans) == 3
        assert not any("kew-switchyard:ci-scan" in line for line in proxy_scans)

    def test_a_concurrent_rebuild_of_the_shared_tags_cannot_remove_the_scanned_image(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """A second run on the same daemon moves `:ci-scan` mid-scan; this run holds on.

        CI and a local merge verify share one Docker daemon on the operator
        Mac, and it uses the containerd image store, which keeps no dangling
        images. The other run rebuilt the shared tags, this run's image lost
        its last name and was deleted at once, no prune involved, and trivy
        failed with "No such image" on an ID it had been handed a second
        earlier. Mutation caught: dropping the per-run reference taken right
        after each build.
        """
        log = tmp_path / "calls.log"
        _daemon_docker(binroot)
        self._trivy(binroot, gate_exit=0, log=log)
        _concurrent_rebuild_before_every_read(binroot)

        result = _run_gate(binroot)

        assert result.returncode == 0, result.stdout + result.stderr
        built = dict(
            tuple(line.split())
            for line in (binroot / "docker-builds.log").read_text().splitlines()
        )
        assert set(built) == _SHARED_SCAN_REFS
        scanned = [
            line.rsplit(" ", 1)[-1]
            for line in log.read_text().splitlines()
            if line.startswith("image ") and "--download-db-only" not in line
        ]
        assert len(scanned) == 12, scanned
        assert set(scanned) == set(built.values()), (
            "every trivy read must target an image this run built, even after "
            "the other run moved the shared tags:\n" + "\n".join(scanned)
        )
        # The other run's tags are its own: this run removed none of them.
        assert set(_image_refs(binroot)) == _SHARED_SCAN_REFS

    @pytest.mark.parametrize(
        ("gate_exit", "proxy_gate_exit"),
        [(0, 0), (1, 0), (0, 1)],
        ids=["passing-run", "agent-gate-fails", "proxy-gate-fails"],
    )
    def test_only_the_shared_tags_remain_after_a_run(
        self,
        binroot: Path,
        tmp_path: Path,
        gate_exit: int,
        proxy_gate_exit: int,
    ) -> None:
        """The per-run references go on every exit path; the shared tags stay.

        A passing run leaves each `:ci-scan` tag on the image it built, as
        before: CI and a re-pin read those. A failing run leaves the shared
        tags of what it built and nothing else. Mutations caught: removing the
        per-run references only on success, and removing by the shared tag.
        """
        log = tmp_path / "calls.log"
        _daemon_docker(binroot)
        self._trivy(
            binroot, gate_exit=gate_exit, proxy_gate_exit=proxy_gate_exit, log=log
        )

        result = _run_gate(binroot)

        assert (result.returncode == 0) == (gate_exit == proxy_gate_exit == 0), (
            result.stdout + result.stderr
        )
        built = dict(
            tuple(line.split())
            for line in (binroot / "docker-builds.log").read_text().splitlines()
        )
        assert _image_refs(binroot) == built, (
            "after the run, only the shared tags of the images it built may "
            "remain, each still on its own build"
        )
        # A gate finding stops nothing: every image is built and gated, and
        # the run fails at the end (#3195).
        assert set(built) == _SHARED_SCAN_REFS

    def test_an_image_gone_before_it_is_held_refuses_before_any_scan(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """No per-run reference, no scan: the refusal points at docker's error.

        Here the other run moves the shared tag before this run's build has
        returned, so the reported ID has no name left when the script tags it.
        Mutation caught: ignoring a failed `docker tag` and scanning anyway.
        """
        log = tmp_path / "calls.log"
        _daemon_docker(binroot)
        self._trivy(binroot, gate_exit=0, log=log)

        result = _run_gate(binroot, {"FAKE_REBUILD_DURING_BUILD": "1"})

        assert result.returncode != 0, result.stdout
        assert "Error response from daemon: No such image" in result.stderr
        assert "docker's error is just above" in result.stderr
        assert "One possible cause" in result.stderr
        assert not any(
            line.startswith("image ") and "--download-db-only" not in line
            for line in log.read_text().splitlines()
        ), "no image may be scanned once its per-run reference failed"
        assert set(_image_refs(binroot)) == {"kew-agent:ci-scan"}

    def test_references_left_by_dead_runs_are_removed_and_live_ones_kept(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """A SIGKILLed run never runs its trap; the next run removes what it leaked.

        Only a reference of the exact per-run form, in a repository this
        script builds, whose PID is not running. Mutations caught: removing a
        live run's hold, removing a tag of any other form, skipping the sweep.
        """
        log = tmp_path / "calls.log"
        _daemon_docker(binroot)
        self._trivy(binroot, gate_exit=0, log=log)
        dead = "99999999"  # above every platform's pid_max, so never running
        live = str(os.getpid())
        held = "sha256:" + "c" * 64
        planted = {
            f"kew-agent:scan-run-{dead}-0123456789abcdef": False,
            f"kew-switchyard:scan-run-{dead}-fedcba9876543210": False,
            f"kew-agent-codex:scan-run-{live}-0123456789abcdef": True,
            "kew-agent:docker-0123456789ab": True,
            f"kew-agent:scan-run-{dead}-0123456789abcde": True,  # 15 hex
            f"other-image:scan-run-{dead}-0123456789abcdef": True,
        }
        store = binroot / "refs"
        store.mkdir()
        for ref in planted:
            (store / ref.replace(":", "@")).write_text(held + "\n")

        result = _run_gate(binroot)

        assert result.returncode == 0, result.stdout + result.stderr
        built = dict(
            tuple(line.split())
            for line in (binroot / "docker-builds.log").read_text().splitlines()
        )
        kept = {ref: held for ref, keep in planted.items() if keep}
        assert _image_refs(binroot) == {**built, **kept}
        for ref, keep in planted.items():
            assert (f"==> removed {ref}:" in result.stdout) is not keep, result.stdout

    def test_the_scan_tag_is_overridable_for_a_re_pin(
        self, binroot: Path, tmp_path: Path
    ) -> None:
        """KEW_SCAN_TAG names the built images directly.

        A re-pin used to build under the shared `:ci-scan` tag and retag it
        afterwards. A concurrent verify run (kew review runs the repo gate in
        its own checkout) rebuilt `:ci-scan` from another tree in between, so
        the retag recorded the wrong images while the scan, bound to the
        iidfile ID, was fine. Naming the images at build time removes the
        retag step. Mutation caught: hard-coding the tag.
        """
        log = tmp_path / "calls.log"
        self._trivy(
            binroot,
            gate_exit=0,
            log=log,
            proxy_scan_tag="docker-0123456789ab",
        )
        result = _run_gate(binroot, {"KEW_SCAN_TAG": "docker-0123456789ab"})
        assert result.returncode == 0, result.stderr
        builds = dict(
            tuple(line.split())
            for line in (binroot / "docker-builds.log").read_text().splitlines()
        )
        assert set(builds) == {
            "kew-agent:docker-0123456789ab",
            "kew-agent-opencode:docker-0123456789ab",
            "kew-agent-codex:docker-0123456789ab",
            "kew-switchyard:docker-0123456789ab",
        }

    @pytest.mark.parametrize(
        "bad_tag",
        [
            "5000/re-pin",  # docker reads kew-agent:5000 as a registry host
            "docker:bad",  # two tag separators
            "docker-0123456789ag",  # not hex
            "docker-0123456789abc",  # 13 chars
            "docker-0123456789AB",  # uppercase: a locale-collated [a-f] admits it
            "latest",  # the mutable stock tag
        ],
    )
    def test_an_unexpected_scan_tag_is_refused_before_any_build(
        self, binroot: Path, tmp_path: Path, bad_tag: str
    ) -> None:
        """Only ci-scan or docker-<12 hex> may name a scanned image.

        Mutation caught: deleting the validation lets `5000/re-pin` build an
        image under a registry-shaped name nothing in kew.toml can pin.
        """
        log = tmp_path / "calls.log"
        self._trivy(binroot, gate_exit=0, log=log)
        result = _run_gate(binroot, {"KEW_SCAN_TAG": bad_tag})
        assert result.returncode != 0
        assert "KEW_SCAN_TAG must be ci-scan or docker-<12 hex>" in result.stderr
        assert not (binroot / "docker-builds.log").exists(), (
            "a refused tag must refuse before docker build runs"
        )


class TestInstallerIsPinnedByDigest:
    """The CI installer step, which parity testing deliberately ignores.

    `INFRA_STEP_NAME_PREFIXES` excludes installers from verify.sh parity — the
    gate is the scan, not the download. That exclusion also means nothing else
    checks this step, and it is where the scanner's identity is established.
    """

    def test_every_platform_archive_has_a_pinned_sha256(self) -> None:
        content = _CI_WORKFLOW.read_text()
        block = content.split("Install Trivy (pinned by digest)", 1)[1].split(
            "- name:", 1
        )[0]
        assets = re.findall(r'asset="([A-Za-z0-9-]+)"', block)
        keys = re.findall(r"sha=\"\$\(pinned (TRIVY_SHA256_[A-Z0-9_]+)\)\"", block)
        assert len(assets) >= 4, f"expected four platform archives, found {assets}"
        assert len(keys) == len(assets), (
            f"{len(assets)} platform archives but {len(keys)} pinned digests — "
            "every downloaded artifact must be verifiable"
        )
        # The digests are read from the one pin file, not written out here
        # (#2462), so the installer and the gate cannot drift. A key that
        # resolves to nothing would install an unverified archive.
        pins = read_pins()
        for key in keys:
            assert re.fullmatch(r"[0-9a-f]{64}", pins.get(key, "")), (
                f"{key} must name a 64-hex digest in {_TRIVY_PIN}"
            )

    def test_the_installer_reads_the_one_pin_file_and_restates_nothing(self) -> None:
        """#2462 — a fourth literal is a fourth place for the four to disagree.

        The runner's scanner, the gate's pin and the version kew's provisioning
        recipe installs are one value from one file. A host built off that pin
        passed its own provisioning and then failed every verification at the
        final image scan, after the whole suite had already run.
        """
        content = _CI_WORKFLOW.read_text()
        block = content.split("Install Trivy (pinned by digest)", 1)[1].split(
            "- name:", 1
        )[0]
        relative = str(_TRIVY_PIN.relative_to(_REPO_ROOT))
        assert relative in block, (
            f"the installer step must read {relative}, the file the gate and "
            "the provisioning recipe read"
        )
        body = block.split("run: |", 1)[1]
        assert f'TRIVY_VERSION: "{_PINNED_TRIVY}"' not in block, (
            "the step must not restate the pinned version"
        )
        assert not re.search(r"[0-9a-f]{64}", body), (
            "the step must not restate an archive digest"
        )

    def test_the_archive_is_verified_before_extraction(self) -> None:
        content = _CI_WORKFLOW.read_text()
        block = content.split("Install Trivy (pinned by digest)", 1)[1].split(
            "- name:", 1
        )[0]
        assert "| checksum" in block, "the downloaded archive must be checksummed"
        verify_at = block.index("| checksum")
        extract_at = block.index("tar xzf")
        assert verify_at < extract_at, (
            "the checksum must be verified BEFORE extraction — a version string "
            "is self-reported, so a substituted archive can answer correctly and "
            "return clean scans forever"
        )

    def test_it_does_not_pipe_the_download_straight_into_tar(self) -> None:
        # `curl ... | tar xz` cannot be checksummed: the bytes are consumed as
        # they arrive. The archive has to land on disk first.
        content = _CI_WORKFLOW.read_text()
        block = content.split("Install Trivy (pinned by digest)", 1)[1].split(
            "- name:", 1
        )[0]
        assert "| tar xz" not in block, (
            "piping the download into tar makes verification impossible"
        )


class TestTheGateRunsOnTheHostInterpreter:
    """The gate's inline Python runs under the HOST's `python3`, not the repo's.

    `scripts/scan-agent-image.sh` pipes trivy's JSON through a python3 heredoc.
    That interpreter is whatever the machine provides — on the self-hosted CI
    runner it is older than the uv-managed 3.13 the test suite uses, and older
    than the 3.14 on this developer machine.

    `datetime.UTC` (3.11+) therefore passed every local run and every one of
    five adversarial review rounds, then crashed the job on CI:

        AttributeError: module 'datetime' has no attribute 'UTC'

    Nothing else in this repository executes under the host interpreter, so
    nothing else would have caught it. This is a static ratchet against the
    newest-version-only spellings, which is the only check that does not
    require provisioning an old interpreter to run against.
    """

    # Spellings that exist only in recent Pythons, with the portable form.
    _TOO_NEW = {
        "datetime.UTC": "datetime.timezone.utc (3.11+ alias)",
        "datetime.date.fromisoformat": "not needed here (3.11 widened it)",
        "itertools.batched": "3.12+",
        "typing.override": "3.12+",
    }

    def _host_python_source(self) -> str:
        """The heredoc bodies, without the surrounding shell or its comments."""
        text = _SCAN_SCRIPT.read_text()
        chunks = []
        for piece in text.split("python3 -c '")[1:]:
            chunks.append(piece.split("')", 1)[0])
        assert chunks, "no python3 heredoc found in the gate script"
        return "\n".join(
            line
            for chunk in chunks
            for line in chunk.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_no_version_gated_spellings_in_the_host_python(self) -> None:
        source = self._host_python_source()
        for spelling, why in self._TOO_NEW.items():
            assert spelling not in source, (
                f"{spelling} appears in the gate's host-interpreter Python, and "
                f"is unavailable on older runners — use {why}. This exact "
                "spelling passed five review rounds and crashed CI."
            )

    def test_the_heredoc_is_actually_reached(self) -> None:
        # A ratchet over source that is never executed proves nothing. Pin that
        # the script really does shell out to python3.
        assert "python3 -c" in _SCAN_SCRIPT.read_text()


class TestTheCommittedPinsAreNotStale:
    """`kew.toml` names the images every dispatch launches. Nothing checked it.

    #1432's fix shipped and changed nothing for real runs, because the pins
    still named an image built before that fix,
    still carrying the CRITICAL node-tar CVE. CI scans freshly built `:ci-scan`
    tags that nothing dispatches into, so it could not see the gap, and no test
    asserted the pins at all: reverting them would have failed nothing.

    This ratchet names the complete docker input digest. It survives squash
    merges and includes uncommitted changes without consulting git history.
    """

    def _pins(self) -> dict[str, str]:
        # Parse the TOML, do not split on the table name: kew.toml explains
        # `[sandbox.images]` in a comment ABOVE the real table, and a text split
        # landed in the comment and read zero pins.
        with (_REPO_ROOT / "kew.toml").open("rb") as fh:
            config = tomllib.load(fh)
        return dict((config.get("sandbox") or {}).get("images") or {})

    def test_every_runner_is_pinned(self) -> None:
        pins = self._pins()
        assert set(pins) == {"claude-code", "codex", "opencode"}, (
            f"kew.toml must pin every agent image explicitly, found {pins}"
        )

    def test_every_pin_has_the_pinned_form(self) -> None:
        from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE

        expected_names = {
            "claude-code": "kew-agent",
            "codex": "kew-agent-codex",
            "opencode": "kew-agent-opencode",
        }
        for runner, pin in self._pins().items():
            match = AGENT_IMAGE_PIN_TAG_RE.fullmatch(pin)
            assert match is not None, (
                f"{runner} must use a docker-<12 hex> image pin, got {pin!r}"
            )
            assert match.group(1) == expected_names[runner], (
                f"{runner} uses the wrong image name in {pin!r}"
            )

    def test_a_nonhex_digest_suffix_is_rejected(self) -> None:
        from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE

        assert AGENT_IMAGE_PIN_TAG_RE.fullmatch("kew-agent:docker-ce13ff05fabz") is None

    def test_every_pin_names_the_same_build(self) -> None:
        # The images share a base and are rebuilt together. Advancing one and
        # not the others gives those dispatch lanes different posture.
        from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE

        matches = [
            AGENT_IMAGE_PIN_TAG_RE.fullmatch(pin) for pin in self._pins().values()
        ]
        assert all(matches), f"invalid image pins: {self._pins()}"
        digests = {match.group(2) for match in matches if match is not None}
        assert len(digests) == 1, (
            f"the [sandbox.images] pins name different builds: {digests}. "
            "Rebuild and re-pin them together."
        )

    def test_copy_parser_handles_indentation_and_continuations(self) -> None:
        dockerfile = (
            "    COPY docker/indented.sh /usr/local/bin/indented\n"
            "COPY --chmod=0755 \\\n"
            "    docker/continued.sh /usr/local/bin/continued\n"
            'COPY ["docker/json-array.sh", "/usr/local/bin/json-array"]\n'
        )
        assert _docker_copy_inputs(dockerfile) == {
            "docker/indented.sh",
            "docker/continued.sh",
            "docker/json-array.sh",
        }

    def test_the_input_list_covers_every_file_the_images_copy(self) -> None:
        """The ratchet is only as complete as the list it walks.

        `_AGENT_IMAGE_BUILD_INPUTS` named the claude Dockerfile and its
        entrypoint only, so a change to `extract-artifact.sh` — copied into
        BOTH images — marked neither stale, and nothing in the opencode image
        counted at all. Derive the expectation from the Dockerfiles rather than
        restating it, or the list drifts again the moment a COPY is added.
        """
        from kew.sandbox import _AGENT_IMAGE_BUILD_INPUTS

        copied: set[str] = set()
        for dockerfile in _AGENT_DOCKERFILES:
            copied.add(f"docker/{dockerfile.name}")
            copied.update(_docker_copy_inputs(dockerfile.read_text()))
        missing = copied - set(_AGENT_IMAGE_BUILD_INPUTS)
        assert not missing, (
            "these files are COPY'd into an agent image but are absent from "
            "_AGENT_IMAGE_BUILD_INPUTS, so changing one leaves the staleness "
            f"check calling a stale image fresh: {sorted(missing)}"
        )

    def test_the_pins_name_the_current_build_inputs(self) -> None:
        from kew.sandbox import AGENT_IMAGE_PIN_TAG_RE, agent_image_inputs_digest

        digest = agent_image_inputs_digest(_REPO_ROOT)[:12]
        matches = [
            AGENT_IMAGE_PIN_TAG_RE.fullmatch(pin) for pin in self._pins().values()
        ]
        pinned = {match.group(2) for match in matches if match is not None}
        assert pinned == {digest}, (
            f"Agent image inputs now name docker-{digest}. Rebuild every image, "
            f"tag them kew-agent:docker-{digest}, "
            f"kew-agent-opencode:docker-{digest} and "
            f"kew-agent-codex:docker-{digest}, and re-pin kew.toml."
        )

    def test_packaged_inputs_equal_the_repository_source(self) -> None:
        from kew.sandbox import (
            agent_image_inputs_digest,
            packaged_agent_image_context,
        )

        source = _REPO_ROOT / "docker"
        packaged_root = packaged_agent_image_context()
        packaged = packaged_root / "docker"
        source_files = {
            path.relative_to(source): path
            for path in source.rglob("*")
            if path.is_file()
        }
        packaged_files = {
            path.relative_to(packaged): path
            for path in packaged.rglob("*")
            if path.is_file()
        }

        assert packaged_files.keys() == source_files.keys()
        for relative, source_path in source_files.items():
            packaged_path = packaged_files[relative]
            assert packaged_path.read_bytes() == source_path.read_bytes()
            assert bool(packaged_path.stat().st_mode & 0o111) == bool(
                source_path.stat().st_mode & 0o111
            )
        assert agent_image_inputs_digest(packaged_root) == agent_image_inputs_digest(
            _REPO_ROOT
        )

    def test_the_digest_changes_when_any_docker_file_changes(
        self, tmp_path: Path
    ) -> None:
        from kew.sandbox import agent_image_inputs_digest

        docker = tmp_path / "docker"
        docker.mkdir()
        first_file = docker / "first"
        first_file.write_bytes(b"a")
        (docker / "second").write_bytes(b"b")
        first = agent_image_inputs_digest(tmp_path)

        first_file.write_bytes(b"c")
        changed = agent_image_inputs_digest(tmp_path)
        assert changed != first

        (docker / "third").write_bytes(b"d")
        added = agent_image_inputs_digest(tmp_path)
        assert added != changed

        first_file.rename(docker / "renamed")
        renamed = agent_image_inputs_digest(tmp_path)
        assert renamed != added

    def test_the_digest_encoding_separates_paths_from_content(
        self, tmp_path: Path
    ) -> None:
        from kew.sandbox import agent_image_inputs_digest

        one = tmp_path / "one"
        one_docker = one / "docker"
        one_docker.mkdir(parents=True)
        (one_docker / "a").write_bytes(b"x\0docker/b\0")

        two = tmp_path / "two"
        two_docker = two / "docker"
        two_docker.mkdir(parents=True)
        (two_docker / "a").write_bytes(b"x")
        (two_docker / "b").write_bytes(b"")

        assert agent_image_inputs_digest(one) != agent_image_inputs_digest(two)

    def test_the_digest_changes_when_file_mode_changes(self, tmp_path: Path) -> None:
        from kew.sandbox import agent_image_inputs_digest

        docker = tmp_path / "docker"
        docker.mkdir()
        source = docker / "entrypoint"
        source.write_bytes(b"same")
        first = agent_image_inputs_digest(tmp_path)

        old_mode = stat.S_IMODE(source.stat().st_mode)
        source.chmod(old_mode ^ stat.S_IXUSR)

        assert agent_image_inputs_digest(tmp_path) != first

    def test_the_digest_ignores_the_checkout_umask(self, tmp_path: Path) -> None:
        """Only the executable bit is identity; read/write bits are not.

        Git records nothing but the executable bit, so two checkouts of the
        same commit differ in the other bits exactly by the operator's umask.
        Folding those into the digest would make the committed pin correct on
        CI and wrong on a contributor's machine with identical inputs.
        """
        from kew.sandbox import agent_image_inputs_digest

        docker = tmp_path / "docker"
        docker.mkdir()
        source = docker / "entrypoint"
        source.write_bytes(b"same")
        source.chmod(0o644)
        first = agent_image_inputs_digest(tmp_path)

        source.chmod(0o600)  # umask 077 checkout
        assert agent_image_inputs_digest(tmp_path) == first

        source.chmod(0o664)  # umask 002 checkout
        assert agent_image_inputs_digest(tmp_path) == first

    def test_the_digest_does_not_depend_on_unicode_normalisation(
        self, tmp_path: Path
    ) -> None:
        """One git pathname, two filesystem spellings, one pin.

        macOS may return a decomposed (NFD) spelling of a name git stores
        composed (NFC). The pin must not change with the filesystem that
        produced the checkout (round-4 review).
        """
        import unicodedata

        from kew.sandbox import agent_image_inputs_digest

        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        assert nfc != nfd  # the two byte spellings really differ

        composed = tmp_path / "composed"
        (composed / "docker").mkdir(parents=True)
        (composed / "docker" / nfc).write_bytes(b"x")
        decomposed = tmp_path / "decomposed"
        (decomposed / "docker").mkdir(parents=True)
        (decomposed / "docker" / nfd).write_bytes(b"x")

        assert agent_image_inputs_digest(composed) == agent_image_inputs_digest(
            decomposed
        )

    def test_the_digest_refuses_a_symlinked_dockerfile(self, tmp_path: Path) -> None:
        from kew.sandbox import agent_image_inputs_digest

        docker = tmp_path / "docker"
        docker.mkdir()
        alternate = tmp_path / "alternate.Dockerfile"
        alternate.write_bytes(b"FROM example.invalid/base:latest\n")
        (docker / "agent.Dockerfile").symlink_to("../alternate.Dockerfile")

        with pytest.raises(
            ValueError,
            match=r"symbolic link under docker: docker/agent\.Dockerfile",
        ):
            agent_image_inputs_digest(tmp_path)


def test_switchyard_image_is_scanned_by_immutable_id(tmp_path: Path) -> None:
    case = TestTheGateActuallyGates()
    binroot = _gate_binroot(tmp_path)
    case._check_switchyard_image_is_scanned_by_immutable_id(binroot, tmp_path)


def test_switchyard_scan_gates_all_high_and_critical_findings(
    tmp_path: Path,
) -> None:
    case = TestTheGateActuallyGates()
    binroot = _gate_binroot(tmp_path)
    case._check_switchyard_scan_gates_all_high_and_critical_findings(binroot, tmp_path)


def test_switchyard_inventory_uses_os_and_rust_floors(
    tmp_path: Path,
) -> None:
    case = TestTheGateActuallyGates()
    binroot = _gate_binroot(tmp_path)
    case._check_switchyard_inventory_uses_os_and_rust_floors(binroot, tmp_path)


def test_union_docker_digest_repins_both_stock_worker_tags() -> None:
    TestTheCommittedPinsAreNotStale().test_the_pins_name_the_current_build_inputs()
