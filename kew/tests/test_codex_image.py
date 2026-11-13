"""Static contract tests for the Codex agent image.

``docker/agent-codex.Dockerfile`` is the image the OpenShell pilot launches for
``codex`` nodes, and three of its properties are load-bearing in a way reading
the file quickly does not reveal:

* the Codex version is PINNED. ``@latest`` would let an unreviewed CLI inside
  the containment boundary on any rebuild, with no diff to review;
* ``kew-openshell-launch`` is installed, and nothing else can launch the image.
  Under ``[sandbox] runtime = "openshell"`` the ENTRYPOINT never runs, so the
  launcher is not a fallback — it is the only path, and it is where the
  placeholder ``auth.json`` is written;
* the Dockerfile and ``docker/openshell/policy-codex.yaml`` name the SAME native
  binary. Under ``enforce`` a policy that misnames it governs nothing that can
  reach the API, so every contained dispatch fails closed on its first model
  call. The enforcement/allowlist properties of that policy live in
  tests/test_openshell_policy.py; what is checked here is the Dockerfile side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "docker" / "agent-codex.Dockerfile"
_PACKAGED_DOCKERFILE = (
    _REPO_ROOT / "kew/src/kew/_docker/docker" / "agent-codex.Dockerfile"
)
_POLICY = _REPO_ROOT / "docker" / "openshell" / "policy-codex.yaml"
_LAUNCHER_INSTALL = (
    "COPY --chown=agent:agent --chmod=0755 docker/kew-openshell-launch "
    "/usr/local/bin/kew-openshell-launch"
)


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _joined() -> str:
    """The Dockerfile with ``\\``-continuations folded into single lines."""
    return re.sub(r"\\\n\s*", " ", _dockerfile())


def test_the_codex_cli_is_pinned_to_an_exact_version() -> None:
    installs = re.findall(r"@openai/codex@(\S+)", _joined())

    assert installs, "the Codex image installs no @openai/codex at all"
    for pin in installs:
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
            f"@openai/codex must be pinned to an exact version, got {pin!r} — "
            "an unpinned CLI enters the containment boundary with no diff to "
            "review and no CVE for the image scan to catch"
        )


def test_the_codex_install_runs_no_package_scripts() -> None:
    """The package declares none, so running them could only add surface."""
    install = next(line for line in _joined().splitlines() if "@openai/codex@" in line)

    assert "--ignore-scripts" in install


def test_the_image_installs_the_openshell_launcher() -> None:
    assert _LAUNCHER_INSTALL in _dockerfile()


def test_the_image_has_no_agent_entrypoint_of_its_own() -> None:
    """OpenShell-only, and the ENTRYPOINT says so by refusing.

    ``openshell sandbox exec`` replaces the ENTRYPOINT, and the docker lane for
    codex does not exist (CodexRunner declares native containment and no
    in-container credential). The launcher is therefore the ENTRYPOINT too: a
    ``docker run`` reads the first container arg as the startup nonce and
    refuses before any model call. An image-owned entrypoint script here would
    be a second, untested launch path — the deleted
    ``docker/agent-codex-entrypoint.sh`` was exactly that, and it built the
    impossible argv ``codex exec exec``.
    """
    dockerfile = _dockerfile()

    assert 'ENTRYPOINT ["/usr/local/bin/kew-openshell-launch"]' in dockerfile
    assert "kew-agent-entrypoint" not in dockerfile
    assert not (_REPO_ROOT / "docker" / "agent-codex-entrypoint.sh").exists()
    assert not (
        _REPO_ROOT / "kew/src/kew/_docker/docker" / "agent-codex-entrypoint.sh"
    ).exists()


def test_the_image_ships_the_publication_producer() -> None:
    """Element 6 of docs/sandbox-entrypoint-contract.md, for every image.

    Without it publication silently depends on which runner was chosen.
    """
    assert (
        "COPY --chown=agent:agent --chmod=0755 docker/extract-artifact.sh "
        "/usr/local/bin/kew-extract-artifact"
    ) in _dockerfile()


def test_the_policy_governs_the_vendor_path_the_dockerfile_asserts() -> None:
    """Both halves of the arch `case`, and nothing the policy does not govern."""
    policy = yaml.safe_load(_POLICY.read_text(encoding="utf-8"))
    governed = {binary["path"] for binary in policy["binaries"]}
    assigned = set(re.findall(r"native_binary=(\S+)", _dockerfile()))

    assert assigned, "the Codex Dockerfile resolves no native binary path"
    assert assigned <= governed, (
        f"the Dockerfile seals the layer around {sorted(assigned - governed)}, "
        "which policy-codex.yaml does not govern — under `enforce` that is a "
        "dispatch that fails closed on its first model call"
    )
    # The resolved path is where `@openai/codex` puts the platform binary: the
    # package ships only bin/codex.js and resolves the executable out of the
    # platform optionalDependency's vendor directory.
    for path in assigned:
        assert re.fullmatch(
            r"/usr/lib/node_modules/@openai/codex/node_modules/@openai/"
            r"codex-linux-(arm64|x64)/vendor/\S+/bin/codex",
            path,
        ), path


@pytest.mark.parametrize("arch", ["arm64", "amd64"])
def test_the_arch_case_covers_both_debian_architectures(arch: str) -> None:
    """`dpkg --print-architecture` spellings, not the triples they map to."""
    assert re.search(rf"^\s*{arch}\) native_binary=", _dockerfile(), re.MULTILINE)


def test_the_packaged_dockerfile_matches_the_repository_copy() -> None:
    assert _PACKAGED_DOCKERFILE.read_bytes() == _DOCKERFILE.read_bytes()
    assert not _DOCKERFILE.stat().st_mode & 0o111
    assert not _PACKAGED_DOCKERFILE.stat().st_mode & 0o111


def test_the_image_is_a_packaged_build_input_kew_can_build() -> None:
    from kew.sandbox import (
        _AGENT_IMAGE_BUILD_INPUTS,
        _PACKAGED_IMAGE_DOCKERFILES,
        AGENT_IMAGE_PIN_TAG_RE,
    )

    assert _PACKAGED_IMAGE_DOCKERFILES["codex"] == "agent-codex.Dockerfile"
    assert "docker/agent-codex.Dockerfile" in _AGENT_IMAGE_BUILD_INPUTS
    assert AGENT_IMAGE_PIN_TAG_RE.fullmatch("kew-agent-codex:docker-0123456789ab")
