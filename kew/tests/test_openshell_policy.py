"""The per-runner OpenShell policies say what they look like they say.

``docker/openshell/policy-*.yaml`` is the container boundary under
``[sandbox] runtime = "openshell"``. Two properties make it containment rather
than decoration, and neither is visible when reading the file quickly:

* every endpoint is ``enforcement: enforce``. OpenShell's observe/report modes
  ALLOW the endpoint and log it, so one relaxed entry turns a policy that reads
  like an allowlist into open egress for that host;
* no host outside the per-runner allowlist below. The whole point is inference
  endpoints only — no telemetry sink, no package registry, and no github.com,
  because the container holds no GitHub token and the host broker owns every
  push.

The allowlist here is the reviewable constant: widening the boundary means
editing this file, which shows up in a diff as exactly what it is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Inference hosts each runner's policy may name — and nothing else.
_ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "claude-code": frozenset({"api.anthropic.com"}),
    # ChatGPT-mode Codex is not a single-endpoint client: kew-openshell-launch
    # writes `auth_mode: chatgptAuthTokens`, so the ChatGPT backend and its
    # token endpoint are inference surface too.
    "codex": frozenset({"api.openai.com", "chatgpt.com", "auth.openai.com"}),
    "opencode": frozenset({"openrouter.ai"}),
}

#: The only enforcement mode kew accepts. OpenShell also has observe/report.
_ENFORCE = "enforce"

#: Image prefixes every governed binary path must sit under. A policy entry
#: pointing into /work or a HOME would name a path the agent can replace.
_SYSTEM_PREFIXES = ("/usr/bin/", "/usr/local/bin/", "/usr/lib/node_modules/")

_RUNNERS = sorted(_ALLOWED_HOSTS)

#: The NATIVE binary each image installs, per runner — the process that owns the
#: inference socket, and so the one entry the policy cannot do without. Every
#: image installs its CLI through npm, which puts a SYMLINK on PATH
#: (``/usr/bin/<runner>``) pointing at the real executable under
#: ``/usr/lib/node_modules``; node never runs the CLI on either path. A policy
#: that governs only the symlink and node governs nothing that talks to the API,
#: so under ``enforce`` every contained dispatch fails closed on its first model
#: call. Each Dockerfile asserts its path before sealing the layer, which is
#: what keeps these strings from being a guess (see the tests below).
#:
#: Codex has TWO, and that is not a hedge: `@openai/codex` ships only
#: bin/codex.js and resolves the executable out of a platform
#: optionalDependency, so the path is arch-specific. An image carries exactly
#: one of them; a governed path that does not exist governs nothing and costs
#: nothing, while a missing one costs every contained dispatch on that arch.
_NATIVE_BINARIES: dict[str, tuple[str, ...]] = {
    "claude-code": ("/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",),
    "codex": (
        "/usr/lib/node_modules/@openai/codex/node_modules/@openai/"
        "codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex",
        "/usr/lib/node_modules/@openai/codex/node_modules/@openai/"
        "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex",
    ),
    "opencode": ("/usr/lib/node_modules/opencode-ai/bin/opencode.exe",),
}

#: What each Dockerfile must `test -x` before sealing its layer. Claude Code and
#: OpenCode resolve to one arch-independent path, so the assertion names it
#: literally; the Codex Dockerfile picks between the two paths above in an arch
#: `case` and asserts the variable it picked — which is only as good as those
#: two assignments, hence the Codex-specific test below.
_NATIVE_BINARY_ASSERTIONS = {
    "claude-code": (
        "test -x /usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    ),
    "codex": 'test -x "$native_binary"',
    "opencode": "test -x /usr/lib/node_modules/opencode-ai/bin/opencode.exe",
}

#: The npm global-bin SYMLINK on PATH, per runner — the name
#: kew-openshell-launch resolves and execs.
_PATH_SHIMS = {
    "claude-code": "/usr/bin/claude",
    "codex": "/usr/bin/codex",
    "opencode": "/usr/bin/opencode",
}

#: The Dockerfile that builds each runner's image.
_DOCKERFILES = {
    "claude-code": "agent.Dockerfile",
    "codex": "agent-codex.Dockerfile",
    "opencode": "agent-opencode.Dockerfile",
}


def _dockerfile(runner: str) -> str:
    return (_REPO_ROOT / "docker" / _DOCKERFILES[runner]).read_text(encoding="utf-8")


def _governed_paths(runner: str) -> set[str]:
    return {binary["path"] for binary in _policy(runner)["binaries"]}


def _policy_path(runner: str, packaged: bool) -> Path:
    base = _REPO_ROOT / ("kew/src/kew/_docker/docker" if packaged else "docker")
    return base / "openshell" / f"policy-{runner}.yaml"


def _policy(runner: str, packaged: bool = False) -> dict[str, Any]:
    document = yaml.safe_load(
        _policy_path(runner, packaged).read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _endpoints(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        endpoint
        for binary in policy["binaries"]
        for endpoint in binary.get("endpoints", [])
    ]


@pytest.mark.parametrize("runner", _RUNNERS)
@pytest.mark.parametrize("packaged", [False, True], ids=["repository", "packaged"])
def test_a_policy_exists_for_each_runner(runner: str, packaged: bool) -> None:
    path = _policy_path(runner, packaged)
    assert path.is_file(), f"missing OpenShell policy: {path}"
    assert _policy(runner, packaged)["runner"] == runner


@pytest.mark.parametrize("runner", _RUNNERS)
def test_every_endpoint_is_enforced(runner: str) -> None:
    endpoints = _endpoints(_policy(runner))
    assert endpoints, f"{runner} policy declares no endpoint at all"
    modes = {endpoint.get("enforcement") for endpoint in endpoints}
    assert modes == {_ENFORCE}, (
        f"{runner} policy has endpoints outside {_ENFORCE!r} mode: {sorted(modes)} — "
        "OpenShell's other modes allow the host and merely log it"
    )


@pytest.mark.parametrize("runner", _RUNNERS)
def test_no_host_outside_the_runners_inference_allowlist(runner: str) -> None:
    hosts = {endpoint.get("host") for endpoint in _endpoints(_policy(runner))}
    assert hosts == _ALLOWED_HOSTS[runner], (
        f"{runner} policy names hosts outside its inference allowlist: "
        f"{sorted(hosts - _ALLOWED_HOSTS[runner])}"
    )


@pytest.mark.parametrize("runner", _RUNNERS)
def test_every_endpoint_names_an_explicit_port(runner: str) -> None:
    for endpoint in _endpoints(_policy(runner)):
        assert endpoint.get("ports") == [443], endpoint


@pytest.mark.parametrize("runner", _RUNNERS)
def test_governed_binaries_are_resolved_image_paths(runner: str) -> None:
    """Paths, not PATH names: a name resolves through whatever shadows it."""
    paths = [binary["path"] for binary in _policy(runner)["binaries"]]
    assert paths, f"{runner} policy governs no binary"
    for path in paths:
        assert path.startswith(_SYSTEM_PREFIXES), path
    assert len(set(paths)) == len(paths), paths


@pytest.mark.parametrize("runner", _RUNNERS)
def test_every_policy_governs_the_npm_shim_on_path(runner: str) -> None:
    """What kew-openshell-launch resolves through PATH and execs."""
    assert _PATH_SHIMS[runner] in _governed_paths(runner)


@pytest.mark.parametrize("runner", _RUNNERS)
def test_every_policy_governs_its_images_native_binary(runner: str) -> None:
    """The npm shim is a symlink; the native binary is what holds the socket.

    Omit it and the policy reads like containment while governing no process
    that can reach the API — every contained dispatch then fails closed on its
    first model call, which is a whole runner broken on this runtime only.
    """
    assert set(_NATIVE_BINARIES[runner]) <= _governed_paths(runner)


@pytest.mark.parametrize("runner", _RUNNERS)
def test_each_dockerfile_asserts_the_native_binary_its_policy_governs(
    runner: str,
) -> None:
    """The coupling that stops the governed path being a plausible guess.

    Grepping the install line alone proved nothing: it let the OpenCode policy
    name ``bin/opencode`` while the image materializes ``bin/opencode.exe``, and
    the test named for checking that passed anyway (PR #2364 review). So each
    Dockerfile ``test -x``'s the exact path its policy governs, and this test
    requires the two to be the same string — a wrong path now fails the BUILD
    instead of every dispatch.
    """
    dockerfile = _dockerfile(runner)
    assert _NATIVE_BINARY_ASSERTIONS[runner] in dockerfile
    for path in _NATIVE_BINARIES[runner]:
        assert path in dockerfile, (
            f"the {runner} policy governs {path}, which its Dockerfile never "
            "names — one of the two is a guess"
        )


def test_the_codex_dockerfile_assigns_only_governed_native_binaries() -> None:
    """Codex asserts a VARIABLE, so what that variable can hold is the coupling.

    `test -x "$native_binary"` proves a path exists, not that it is a path the
    policy governs. The arch `case` is the only thing that assigns it, so every
    assignment must be one of the governed paths — otherwise a build could seal
    a layer around a binary with no egress rule and fail closed on the first
    model call, on one architecture only.
    """
    dockerfile = _dockerfile("codex")
    assigned = set(re.findall(r"native_binary=(\S+)", dockerfile))

    assert assigned == set(_NATIVE_BINARIES["codex"]), (
        f"the Codex Dockerfile assigns {sorted(assigned)}; the policy governs "
        f"{sorted(_NATIVE_BINARIES['codex'])}"
    )


def test_the_opencode_policy_matches_the_postinstall_the_dockerfile_describes() -> None:
    """The Dockerfile states where postinstall puts the platform binary."""
    dockerfile = _dockerfile("opencode")
    assert "cd /usr/lib/node_modules/opencode-ai && node postinstall.mjs" in dockerfile
    # The statement wraps across comment lines; rejoin before reading it.
    prose = re.sub(r"\n#\s*", " ", dockerfile)
    stated = re.search(r"places the platform binary at (\S+)\.\s", prose)
    assert stated, "the Dockerfile no longer states the postinstall destination"
    assert _NATIVE_BINARIES["opencode"][0].endswith("/" + stated.group(1)), (
        f"the Dockerfile materializes {stated.group(1)}, the policy governs "
        f"{_NATIVE_BINARIES['opencode'][0]}"
    )


def test_the_claude_policy_matches_the_native_install_the_dockerfile_runs() -> None:
    """The native package plus install.cjs is what materializes the binary."""
    dockerfile = _dockerfile("claude-code")
    assert "@anthropic-ai/claude-code-linux-arm64" in dockerfile
    assert "@anthropic-ai/claude-code-linux-x64" in dockerfile
    assert (
        "node /usr/lib/node_modules/@anthropic-ai/claude-code/install.cjs" in dockerfile
    )


@pytest.mark.parametrize("runner", _RUNNERS)
def test_the_packaged_policy_matches_the_repository_copy(runner: str) -> None:
    repository = _policy_path(runner, packaged=False)
    packaged = _policy_path(runner, packaged=True)

    assert packaged.read_bytes() == repository.read_bytes()
    # Policies are read, never executed — an exec bit here would be noise the
    # wheel's mode assertions then have to carry.
    assert not repository.stat().st_mode & 0o111
    assert not packaged.stat().st_mode & 0o111


@pytest.mark.parametrize("runner", _RUNNERS)
def test_the_policies_are_pinned_build_inputs(runner: str) -> None:
    """A boundary change must move the image pin kew.toml names."""
    from kew.sandbox import _AGENT_IMAGE_BUILD_INPUTS

    assert f"docker/openshell/policy-{runner}.yaml" in _AGENT_IMAGE_BUILD_INPUTS


# --------------------------------------------------------------------------- #
# The reviewer's two boundaries (epic node N12)                               #
# --------------------------------------------------------------------------- #
# One image, three boundaries. The adversarial reviewer EXECUTES the reviewed
# PR's tests, so a boundary proven for a dispatch says nothing about it: it runs
# under its own policy, certified against its own digest, and its environment is
# installed beforehand in a third sandbox under a third policy that can reach a
# package index and nothing else.
#
# These are the files that say what each of those launches may reach, so this is
# where widening one shows up in a diff as exactly what it is.

#: The hosts the reviewer's own policy may name — and nothing else. The model
#: API: the reviewer's environment is already installed when it starts, so an
#: index would only let it install whatever the PR asked it to.
_REVIEW_HOSTS = frozenset({"api.anthropic.com"})

#: The hosts the provisioning policy may name — and nothing else. uv reads the
#: simple index at the first and downloads the wheels it names from the second.
#: No inference host: nothing in that sandbox has a credential to use one.
_PROVISION_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})

#: The one binary the provisioning policy governs. uv fetches the wheels itself;
#: the build backends it then executes are the PR's own code, and under
#: ``enforce`` an ungoverned binary has no egress at all — which is what keeps a
#: PR's setup.py off the network while its dependencies are fetched for it.
_PROVISION_BINARY = "/usr/local/bin/uv"

#: The reviewer runner. Only claude-code can review at all
#: (``kew.review_stream.STREAM_VERDICT_RUNNERS``), so only claude-code ships
#: these two policies — a reviewer pinned elsewhere is refused, not defaulted.
_REVIEWER = "claude-code"


def _flow_policy(flow: str, packaged: bool = False) -> dict[str, Any]:
    from kew.openshell_egress import policy_file_stem

    base = _REPO_ROOT / ("kew/src/kew/_docker/docker" if packaged else "docker")
    stem = policy_file_stem(_REVIEWER, flow)
    document = yaml.safe_load(
        (base / "openshell" / f"policy-{stem}.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def test_the_reviewer_policy_allows_the_inference_host_and_nothing_else() -> None:
    """No index, no forge, no telemetry sink — one host, enforced."""
    policy = _flow_policy("review")
    endpoints = _endpoints(policy)

    assert endpoints, "the review policy declares no endpoint at all"
    assert {endpoint["host"] for endpoint in endpoints} == _REVIEW_HOSTS
    assert {endpoint.get("enforcement") for endpoint in endpoints} == {_ENFORCE}
    assert {tuple(endpoint["ports"]) for endpoint in endpoints} == {(443,)}
    assert policy["runner"] == _REVIEWER


def test_the_reviewer_policy_is_never_wider_than_the_dispatch_policy() -> None:
    """The reviewer boundary may not become the loose one.

    For claude-code the two endpoint sets are equal today, because the dispatch
    policy is already one host. That is a property of this runner, not a licence
    to share the file: the point of the separate digest is that widening the
    dispatch policy cannot widen the review boundary, and this is the assertion
    that keeps the inequality pointing the right way.
    """
    review = {endpoint["host"] for endpoint in _endpoints(_flow_policy("review"))}
    dispatch = {endpoint["host"] for endpoint in _endpoints(_policy(_REVIEWER))}

    assert review <= dispatch
    assert review == _REVIEW_HOSTS


def test_the_provisioning_policy_allows_the_package_index_and_nothing_else() -> None:
    """The index, enforced — and no inference host at all."""
    policy = _flow_policy("review-provision")
    endpoints = _endpoints(policy)

    assert endpoints, "the provisioning policy declares no endpoint at all"
    assert {endpoint["host"] for endpoint in endpoints} == _PROVISION_HOSTS
    assert {endpoint.get("enforcement") for endpoint in endpoints} == {_ENFORCE}
    assert {tuple(endpoint["ports"]) for endpoint in endpoints} == {(443,)}
    assert not _PROVISION_HOSTS & _REVIEW_HOSTS


def test_the_provisioning_policy_governs_only_uv() -> None:
    """Every build backend uv runs is the PR's code, and has no egress.

    One governed path, and it is an image path under a system prefix: the
    reviewed tree cannot replace ``/usr/local/bin/uv``, and anything it CAN
    replace is governed by nothing and so reaches nowhere.
    """
    paths = [binary["path"] for binary in _flow_policy("review-provision")["binaries"]]

    assert paths == [_PROVISION_BINARY]
    assert _PROVISION_BINARY.startswith(_SYSTEM_PREFIXES)
    # And it is where the image really puts it.
    assert re.search(
        r"tar -xzf \S+ -C /usr/local/bin\b[^\n]*\n[^\n]*\"uv-\$target/uv\"",
        _dockerfile(_REVIEWER),
    )


def test_the_reviewer_policy_governs_no_interpreter_the_reviewed_code_runs() -> None:
    """Nothing the reviewer executes to CHECK the PR has egress.

    Not uv, not python, not the project's test runner: under ``enforce`` an
    ungoverned binary is denied every destination, which is what makes "the
    reviewer needs nothing else once its environment is provisioned" a rule.
    Node IS governed, and deliberately: it is the package's own wrapper and the
    program `kew sandbox certify` proves this boundary with.
    """
    governed = {binary["path"] for binary in _flow_policy("review")["binaries"]}

    assert governed == set(_governed_paths(_REVIEWER))
    assert _PROVISION_BINARY not in governed
    assert not {"/usr/bin/python", "/usr/bin/python3", "/usr/local/bin/pytest"} & (
        governed
    )


@pytest.mark.parametrize("flow", ["review", "review-provision"])
def test_the_packaged_flow_policy_matches_the_repository_copy(flow: str) -> None:
    from kew.openshell_egress import openshell_policy_path, policy_file_stem

    stem = policy_file_stem(_REVIEWER, flow)
    repository = _REPO_ROOT / "docker" / "openshell" / f"policy-{stem}.yaml"
    packaged = openshell_policy_path(_REVIEWER, flow=flow)

    assert packaged.read_bytes() == repository.read_bytes()
    assert not repository.stat().st_mode & 0o111
    assert not packaged.stat().st_mode & 0o111
    # A boundary change must move the image pin kew.toml names.
    from kew.sandbox import _AGENT_IMAGE_BUILD_INPUTS

    assert f"docker/openshell/policy-{stem}.yaml" in _AGENT_IMAGE_BUILD_INPUTS


def test_the_dispatch_flow_name_has_one_spelling() -> None:
    """``kew.sandbox`` cannot import the flow constants, so it repeats one.

    That duplicate is the only one, and it is held here: a default that drifted
    would resolve the dispatch policy for a caller that named no flow and the
    review policy for one that named the same thing.
    """
    from kew.openshell_egress import DISPATCH_FLOW
    from kew.sandbox import OPENSHELL_DISPATCH_FLOW

    assert OPENSHELL_DISPATCH_FLOW == DISPATCH_FLOW
