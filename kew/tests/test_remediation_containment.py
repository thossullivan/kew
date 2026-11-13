"""W1.1 node 3d: the contained-execution seam for the remediation flows.

Built and tested before it has callers, so moving review and resolve onto it is
a wiring change against a tested surface rather than a rewrite and a wiring
change at once.
"""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

import pytest

from kew.config import DispatchConfig, SandboxConfig
from kew.remediation_containment import (
    RemediationContainment,
    resolve_remediation_containment,
)

# The stock claude-code image, read from the config default rather than
# copied — a second copy of the default is a second thing to update.
CLAUDE_IMAGE = SandboxConfig().image

# These cases are all `runtime = "docker"`, which has no certification contract
# (:func:`kew.sandbox.require_certified_boundary`), so there is no ledger for
# the decision to read. `store` is still passed explicitly: it is keyword-only
# and has no default, so a new caller cannot omit it by accident (#2389 review).


def _config(**sandbox: object) -> DispatchConfig:
    return DispatchConfig(sandbox=SandboxConfig(**sandbox))  # type: ignore[arg-type]


def _with_runtime(present: bool = True):
    return patch(
        "kew.remediation_containment.detect_runtime_cached",
        return_value="/usr/bin/docker" if present else None,
    )


def _with_credential(value: str = "sk-test"):
    return patch.dict("os.environ", {"ANTHROPIC_API_KEY": value})


class TestResolution:
    """Contain when possible; enforce policy and direct credential gates."""

    def test_contains_when_the_host_can(self):
        with _with_runtime(), _with_credential():
            decision = resolve_remediation_containment(
                _config(enabled="auto", runtime="docker"),
                runner="claude-code",
                store=None,
            )
        assert decision.posture == "sandboxed"
        assert decision.image == CLAUDE_IMAGE
        assert decision.contained
        assert decision.reason is None

    def test_a_native_runner_is_already_contained(self):
        """Codex's own boundary applies here as much as on dispatch."""
        with _with_runtime(present=False):
            decision = resolve_remediation_containment(
                _config(enabled="auto", runtime="docker"), runner="codex", store=None
            )
        assert decision.posture == "native"
        assert decision.contained
        assert decision.image is None

    def test_an_uncontainable_runner_says_why(self):
        with _with_runtime():
            decision = resolve_remediation_containment(
                _config(enabled="auto", runtime="docker"), runner="lemonade", store=None
            )
        assert not decision.contained
        assert "host-local inference server" in (decision.reason or "")

    @pytest.mark.parametrize(
        ("label", "kwargs", "runtime", "fragment"),
        [
            ("no runtime", {"enabled": "auto"}, False, "not found"),
            ("opted out", {"enabled": False}, True, "enabled = false"),
        ],
    )
    def test_host_execution_names_its_specific_obstacle(
        self, label, kwargs, runtime, fragment
    ):
        """ "Uncontained" alone is not an answer an operator can act on.

        A missing runtime is theirs to fix; an unmapped image is kew's. The
        ledger has to tell them apart.
        """
        with _with_runtime(present=runtime), _with_credential():
            decision = resolve_remediation_containment(
                _config(runtime="docker", **kwargs), runner="claude-code", store=None
            )
        assert not decision.contained, label
        assert fragment in (decision.reason or ""), label

    def test_an_unmapped_image_does_not_refuse_here(self):
        """Auto mode retains host fallback for an unmapped runner."""
        with _with_runtime(), patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            decision = resolve_remediation_containment(
                _config(enabled="auto", runtime="docker"), runner="pi", store=None
            )
        assert not decision.contained
        assert "[sandbox.images]" in (decision.reason or "")

    @pytest.mark.parametrize("enabled", ["auto", True])
    def test_round8_missing_direct_credential_refuses(self, enabled):
        from kew.sandbox import SandboxRuntimeUnavailable, _resolve_effective_sandbox

        with (
            _with_runtime(),
            patch("kew.sandbox.detect_runtime_cached", return_value="/fake/docker"),
            patch("kew.sandbox.resolve_effective_sandbox", _resolve_effective_sandbox),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SandboxRuntimeUnavailable) as refusal,
        ):
            resolve_remediation_containment(
                _config(enabled=enabled, runtime="docker"),
                runner="claude-code",
                store=None,
            )
        assert refusal.value.reason == "credential_missing"

    def test_auto_and_disabled_resolution_do_not_refuse(self):
        """Preserve the historical auto and disabled behavior."""
        for runner in ("claude-code", "codex", "lemonade", "pi", "not-a-runner"):
            for enabled in ("auto", False):
                with _with_runtime(present=False):
                    decision = resolve_remediation_containment(
                        _config(enabled=enabled, runtime="docker"),
                        runner=runner,
                        store=None,
                    )
                assert isinstance(decision, RemediationContainment)


@pytest.mark.parametrize("allow", [None, False])
@pytest.mark.parametrize(
    ("runner", "runtime", "images", "reason", "image"),
    [
        ("claude-code", None, {}, "runtime_unavailable", None),
        ("pi", "/usr/bin/docker", {}, "image_unmapped", None),
        ("pi", "/usr/bin/docker", {"pi": "pi:test"}, "credential_missing", "pi:test"),
        ("claude-code", "/usr/bin/docker", {}, "credential_missing", CLAUDE_IMAGE),
    ],
)
def test_required_containment_uses_dispatch_refusal(
    allow, runner, runtime, images, reason, image
):
    from kew.sandbox import SandboxRuntimeUnavailable

    config = _config(enabled=True, images=images)
    if allow is not None:
        config.sandbox.allow_uncontained_flows = allow
    with (
        patch("kew.sandbox.detect_runtime_cached", return_value=runtime),
        _with_runtime(present=runtime is not None),
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(SandboxRuntimeUnavailable) as refused,
    ):
        resolve_remediation_containment(config, runner=runner, store=None)
    assert refused.value.reason == reason
    assert refused.value.runtime == ("docker" if runtime else None)
    assert refused.value.image == image


def test_required_containment_refuses_undeclared_credential():
    from kew.sandbox import SandboxCredentialUndeclared

    with (
        _with_runtime(),
        patch("kew.sandbox.detect_runtime_cached", return_value="docker"),
        patch("kew.sandbox.runner_credential_env_vars", return_value=()),
        patch(
            "kew.remediation_containment.runner_credential_env_vars", return_value=()
        ),
        pytest.raises(SandboxCredentialUndeclared) as refused,
    ):
        resolve_remediation_containment(
            _config(enabled=True), runner="claude-code", store=None
        )
    assert refused.value.reason == "credential_undeclared"
    assert refused.value.runtime == "docker"
    assert refused.value.image == CLAUDE_IMAGE


@pytest.mark.parametrize("runner", ["claude-code", "pi", "lemonade"])
def test_required_containment_explicit_exception_retains_host_fallback(runner):
    with _with_runtime(present=False):
        decision = resolve_remediation_containment(
            _config(enabled=True, allow_uncontained_flows=True),
            runner=runner,
            store=None,
        )
    assert decision.posture == "uncontained_flow"
    assert not decision.contained
    assert decision.reason


@pytest.mark.parametrize("posture", ["host", "uncontained", "native", "sandboxed"])
@pytest.mark.parametrize("flow", ["dispatch", "loop command verification"])
def test_shared_posture_writer_does_not_consult_remediation_policy(posture, flow):
    from kew.sandbox import attest_flow_posture

    with (
        patch.object(
            SandboxConfig,
            "uncontained_flows_allowed",
            new_callable=PropertyMock,
            side_effect=AssertionError(
                "ordinary routes must not read remediation policy"
            ),
        ),
        patch("kew.audit.emit") as emit,
    ):
        attest_flow_posture(
            None,
            flow=flow,
            runner="claude-code",
            config=SandboxConfig(enabled=False),
            posture=posture,
        )
    assert emit.call_args.kwargs["payload"]["admission"] == (
        "dangerously_skip_containment" if posture == "uncontained" else "policy"
    )


def _egress_gate_open(module: str):
    """Pass the OpenShell egress-certification gate (#2319) where ``module`` reads it.

    It runs in front of the launch-path gate these tests assert, and has its
    own refusal tests in ``tests/test_openshell_certification.py``.
    """
    return patch(
        f"{module}.require_certified_boundary",
        lambda config, *, runner, store: None,
    )


class TestOpenShellHasNoRemediationLaunchPath:
    """The tracked remediation flows cannot launch into a gateway sandbox.

    They do not hand their launch to `kew.openshell_launch`; they swap one
    EXECUTION STEP for a container argv (`dispatch_core._contained_execution_for`
    -> `build_agent_container_argv` -> `sandbox.build_run_argv`). Admitting
    `runtime = "openshell"` here therefore resolved `sandboxed`, attested that
    posture, and then spelled `openshell run --rm --init --pull=never --name
    ...` at a CLI with no `run` subcommand (PR #2401 review).
    """

    @staticmethod
    def _openshell(**sandbox: object) -> DispatchConfig:
        return _config(
            runtime="openshell",
            openshell={"providers": {"claude-code": "kew-claude-code"}},
            **sandbox,
        )

    @pytest.mark.parametrize("allow", [None, False])
    def test_required_containment_refuses_before_attesting_a_posture(self, allow):
        """Fails closed under the runtime's own token, not `sandboxed`."""
        from kew.sandbox import SandboxRuntimeUnavailable

        config = self._openshell(enabled=True)
        if allow is not None:
            config.sandbox.allow_uncontained_flows = allow
        with (
            _with_runtime(),
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _with_credential(),
            _egress_gate_open("kew.sandbox"),
            pytest.raises(SandboxRuntimeUnavailable) as refused,
        ):
            resolve_remediation_containment(config, runner="claude-code", store=None)
        assert refused.value.reason == "openshell-runtime-unavailable"
        assert "command line" in str(refused.value)

    @pytest.mark.parametrize("enabled", ["auto", True])
    def test_host_fallback_rather_than_a_sandboxed_posture_with_no_launcher(
        self, enabled
    ):
        """Auto mode records the gap the way it records a missing runtime.

        `allow_uncontained_flows` is what puts `enabled = true` on this arm, and
        a REACHABLE gateway is what made the bug: the runtime probe succeeds, so
        nothing downstream noticed there was no launcher.
        """
        config = self._openshell(enabled=enabled, allow_uncontained_flows=True)
        with (
            _with_runtime(),
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _with_credential(),
            _egress_gate_open("kew.remediation_containment"),
        ):
            decision = resolve_remediation_containment(
                config, runner="claude-code", store=None
            )
        assert (decision.posture, decision.image) == ("uncontained_flow", None)
        assert not decision.contained
        assert "no container argv" in (decision.reason or "")

    def test_a_sidecar_remediation_route_is_refused_too(self):
        """The sidecar branch resolves first, so it needs its own answer."""
        from kew.inference_transport import ResolvedInferenceTransport
        from kew.sandbox import SandboxRuntimeUnavailable

        transport = ResolvedInferenceTransport(
            mode="switchyard",
            requested_model="sonnet",
            model="sonnet",
            config_sha256="e" * 64,
            sidecar=True,
            endpoint="http://127.0.0.1:9/",
            worker_credential_env_name="ANTHROPIC_API_KEY",
        )
        with (
            _with_runtime(),
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/openshell"
            ),
            _with_credential(),
            _egress_gate_open("kew.sandbox"),
            pytest.raises(SandboxRuntimeUnavailable) as refused,
        ):
            resolve_remediation_containment(
                self._openshell(enabled=True),
                runner="claude-code",
                store=None,
                inference_transport=transport,
            )
        assert refused.value.reason == "openshell-runtime-unavailable"
        assert "docker topology" in str(refused.value)

    @pytest.mark.parametrize("enabled", ["auto", True, False])
    def test_a_native_runner_keeps_its_own_boundary_on_this_runtime(self, enabled):
        """The refusal above is for runners whose boundary kew would hold.

        `codex` declines its own seatbelt on `runtime = "openshell"` so that
        kew's gateway sandbox is used instead (epic node N10) — and these flows
        are precisely where that sandbox is not on offer, because they have no
        gateway launch path to put an agent argv into. Resolving the posture
        question with the configured runtime here therefore refused every Codex
        `kew review` / `kew resolve` / `kew ci-fix` / plan run outright under
        `[sandbox] enabled = true`, where the same config on `docker` contains
        it natively (PR #2461 review round 2).

        All three `enabled` values, because they reach the answer by different
        routes — `true` through `resolve_effective_sandbox`, the other two
        through this module's own host-fallback arm — and a fix that only moved
        one of them would leave the required-containment arm, the one that
        fails closed, as the odd one out.

        The egress gate is deliberately NOT opened: a run kew is not
        containing has no kew boundary to certify, and reaching that gate at
        all would be the bug.
        """
        config = self._openshell(enabled=enabled)
        with _with_runtime(), _with_credential():
            decision = resolve_remediation_containment(
                config, runner="codex", store=None
            )
        assert (decision.posture, decision.image, decision.reason) == (
            "native",
            None,
            None,
        )
        assert decision.contained

    def test_a_native_sidecar_remediation_route_is_not_refused_either(self):
        """Same rule on the sidecar arm, which resolves before the other two.

        A sidecar topology has no gateway launch path for any runner, so kew is
        offering no boundary on it and the runner's own one stands. The
        `claude-code` row above is the contrast: it holds none, so the topology
        refusal is the honest answer there.
        """
        from kew.inference_transport import ResolvedInferenceTransport

        transport = ResolvedInferenceTransport(
            mode="switchyard",
            requested_model="gpt-5",
            model="gpt-5",
            config_sha256="e" * 64,
            sidecar=True,
            endpoint="http://127.0.0.1:9/",
            worker_credential_env_name="OPENAI_API_KEY",
        )
        with _with_runtime(), _with_credential():
            decision = resolve_remediation_containment(
                self._openshell(enabled=True),
                runner="codex",
                store=None,
                inference_transport=transport,
            )
        assert (decision.posture, decision.image) == ("native", None)
