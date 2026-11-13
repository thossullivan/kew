"""Pure inference transport resolution and application contracts."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass

import pytest

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.inference_transport import (
    RunnerTransportCapability,
    TransportAdmissionError,
    TransportResolutionError,
    admit_inference_transport,
    apply_inference_transport,
    resolve_inference_transport,
)
from kew.runners.hermes import HermesRunner
from kew.runners.lemonade import LemonadeRunner
from kew.runners.pi import PiRunner


@dataclass
class _Runner:
    name: str = "test-runner"
    transport_capability: RunnerTransportCapability = RunnerTransportCapability()


def test_direct_resolution_is_immutable_and_preserves_inputs() -> None:
    config = DispatchConfig()
    runner = _Runner()

    resolved = resolve_inference_transport(config, runner, "provider/model")
    argv = ["agent", "--model", "provider/model"]
    env = {"EXISTING": "value"}
    applied = apply_inference_transport(resolved, argv=argv, env=env)

    assert resolved.mode == "direct"
    assert resolved.requested_model == "provider/model"
    assert resolved.model == "provider/model"
    assert resolved.endpoint is None
    assert resolved.route is None
    assert len(resolved.config_sha256) == 64
    assert applied.model == "provider/model"
    assert applied.argv == tuple(argv)
    assert applied.environment == env
    assert argv == ["agent", "--model", "provider/model"]
    assert env == {"EXISTING": "value"}
    with pytest.raises(FrozenInstanceError):
        resolved.mode = "switchyard"  # type: ignore[misc]


def test_digest_is_stable_for_equivalent_validated_configuration() -> None:
    first = resolve_inference_transport(DispatchConfig(), _Runner(), "model")
    second = resolve_inference_transport(DispatchConfig(), _Runner(), "model")

    assert first.config_sha256 == second.config_sha256


def test_switchyard_resolves_exact_route_and_endpoint() -> None:
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route="switchyard/passthrough",
                expected_version="pinned-version",
            ),
        )
    )
    capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )

    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=capability), "gpt-5.6"
    )

    assert resolved.mode == "switchyard"
    assert resolved.endpoint == "http://127.0.0.1:4000"
    assert resolved.route == "switchyard/passthrough"
    assert resolved.requested_model == "gpt-5.6"
    assert resolved.model == "switchyard/passthrough"
    assert resolved.api_format == "openai-responses"
    assert resolved.managed is False
    assert resolved.expected_version == "pinned-version"


def test_switchyard_resolution_carries_only_non_secret_managed_identity(
    tmp_path,
) -> None:
    binary = tmp_path / "switchyard-server"
    binary.write_text("binary")
    config_path = tmp_path / "routes.toml"
    config_path.write_text("schema_version = 1")
    config = DispatchConfig(
        inference_transport={
            "mode": "switchyard",
            "switchyard": {
                "endpoint": "http://127.0.0.1:4000",
                "route": "switchyard/passthrough",
                "expected_version": "0.1.0",
                "managed": True,
                "config_path": config_path,
                "binary": binary,
                "binary_sha256": "a" * 64,
                "credential_env": ["OPENAI_API_KEY"],
            },
        }
    )
    capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )

    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=capability), "gpt-5.6-luna"
    )

    assert resolved.binary_sha256 == "a" * 64
    assert resolved.credential_env_names == ("OPENAI_API_KEY",)
    assert "secret" not in repr(resolved)


def test_managed_switchyard_defers_admission_to_run_owned_supervisor(tmp_path) -> None:
    binary = tmp_path / "switchyard-server"
    binary.write_text("binary")
    route_config = tmp_path / "routes.toml"
    route_config.write_text(
        "schema_version = 1\n"
        "[llm_clients.openai]\n"
        'api_key_env = "OPENAI_API_KEY"\n'
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "openai"\n'
        "[routes.classifier]\n"
        'id = "switchyard/llm_classifier"\n'
        'type = "llm_classifier"\n'
        'classifier_target = "strong"\n'
    )
    config = DispatchConfig(
        inference_transport={
            "mode": "switchyard",
            "switchyard": {
                "endpoint": "http://127.0.0.1:4000",
                "route": "switchyard/passthrough",
                "expected_version": "0.1.0",
                "managed": True,
                "config_path": route_config,
                "binary": binary,
                "binary_sha256": "a" * 64,
            },
        }
    )
    capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=capability), "gpt-5.6-luna"
    )

    admitted = admit_inference_transport(resolved)

    assert admitted.preflight_verdict == "managed_pending"
    assert admitted.observed_version is None


def _managed_adaptive_config(tmp_path, **switchyard_overrides):
    binary = tmp_path / "switchyard-server"
    binary.write_text("binary")
    binary.chmod(0o755)
    route_config = tmp_path / "routes.toml"
    route_config.write_text(
        "schema_version = 1\n"
        "[llm_clients.openai]\n"
        'api_key_env = "OPENAI_API_KEY"\n'
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "openai"\n'
        "[routes.classifier]\n"
        'id = "switchyard/llm_classifier"\n'
        'type = "llm_classifier"\n'
        'classifier_target = "strong"\n'
    )
    settings = {
        "endpoint": "http://127.0.0.1:4000",
        "route": "switchyard/llm_classifier",
        "expected_version": "0.1.0",
        "managed": True,
        "config_path": route_config,
        "binary": binary,
        "binary_sha256": hashlib.sha256(b"binary").hexdigest(),
        "credential_env": ["OPENAI_API_KEY"],
        **switchyard_overrides,
    }
    return DispatchConfig(
        inference_transport={
            "mode": "switchyard",
            "switchyard": settings,
        }
    )


def _adaptive_capability() -> RunnerTransportCapability:
    return RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )


def test_adaptive_switchyard_opt_in_defaults_to_false() -> None:
    assert DispatchConfig().inference_transport.allow_adaptive is False


def test_classifier_route_is_refused_by_admission_without_explicit_opt_in(
    tmp_path,
) -> None:
    config = _managed_adaptive_config(tmp_path)
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=_adaptive_capability()), "requested"
    )

    with pytest.raises(
        TransportAdmissionError,
        match="adaptive_routing_disabled",
    ) as exc_info:
        admit_inference_transport(resolved, config=config)

    assert exc_info.value.reason == "adaptive_routing_disabled"


def test_governed_dispatch_admission_refuses_classifier_route_by_default(
    tmp_path,
) -> None:
    from kew.dispatch_core import _require_dispatch_transport_admission

    config = _managed_adaptive_config(tmp_path)

    with pytest.raises(
        TransportAdmissionError,
        match="adaptive_routing_disabled",
    ):
        _require_dispatch_transport_admission(
            config,
            _Runner(transport_capability=_adaptive_capability()),
            "requested",
        )


def test_classifier_route_is_admitted_only_for_opted_in_managed_transport(
    tmp_path,
) -> None:
    config = _managed_adaptive_config(tmp_path)
    config.inference_transport.allow_adaptive = True
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=_adaptive_capability()), "requested"
    )

    admitted = admit_inference_transport(resolved, config=config)

    assert admitted.preflight_verdict == "managed_pending"
    assert admitted.resolved.route == "switchyard/llm_classifier"


def test_admission_rechecks_capability_pin_after_config_validation(tmp_path) -> None:
    from kew.run_store import CURRENT_SCHEMA_VERSION

    config = _managed_adaptive_config(tmp_path)
    config.inference_transport.allow_adaptive = True
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=_adaptive_capability()), "requested"
    )
    config.inference_transport.capability.min_schema_version = (
        CURRENT_SCHEMA_VERSION + 1
    )

    with pytest.raises(ValueError, match="capability requires schema"):
        admit_inference_transport(resolved, config=config)


def test_classifier_route_refuses_opt_in_without_managed_mode(tmp_path) -> None:
    config = _managed_adaptive_config(tmp_path, managed=False)
    config.inference_transport.allow_adaptive = True
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=_adaptive_capability()), "requested"
    )

    with pytest.raises(
        TransportAdmissionError,
        match="adaptive_route_requires_managed_switchyard",
    ):
        admit_inference_transport(resolved, config=config)


def test_external_switchyard_still_requires_preflight() -> None:
    resolved = resolve_inference_transport(
        DispatchConfig(
            inference_transport={
                "mode": "switchyard",
                "switchyard": {
                    "endpoint": "https://switchyard.example",
                    "route": "exact-route",
                    "expected_version": "pinned",
                },
            }
        ),
        _Runner(
            transport_capability=RunnerTransportCapability(
                incoming_api_formats=("openai-responses",),
                endpoint_override=True,
                model_override=True,
            )
        ),
        "model",
    )

    with pytest.raises(TransportAdmissionError, match="preflight is unavailable"):
        admit_inference_transport(resolved)


@pytest.mark.parametrize(
    ("capability", "reason"),
    [
        (RunnerTransportCapability(), "incoming API"),
        (
            RunnerTransportCapability(
                incoming_api_formats=("openai-responses",), model_override=True
            ),
            "endpoint override",
        ),
        (
            RunnerTransportCapability(
                incoming_api_formats=("openai-responses",), endpoint_override=True
            ),
            "model override",
        ),
    ],
)
def test_switchyard_fails_closed_for_unsupported_capability(
    capability: RunnerTransportCapability, reason: str
) -> None:
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="https://switchyard.example",
                route="exact-route",
                expected_version="pinned-version",
            ),
        )
    )

    with pytest.raises(TransportResolutionError, match=reason):
        resolve_inference_transport(
            config, _Runner(transport_capability=capability), "m"
        )


@pytest.mark.parametrize("runner_type", [PiRunner, HermesRunner, LemonadeRunner])
def test_unproven_runner_is_explicitly_unsupported(
    runner_type: type[PiRunner] | type[HermesRunner] | type[LemonadeRunner],
) -> None:
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="https://switchyard.example",
                route="exact-route",
                expected_version="pinned-version",
            ),
        )
    )
    runner = runner_type()

    assert runner.transport_capability == RunnerTransportCapability()
    with pytest.raises(
        TransportResolutionError,
        match=rf"{runner.name} has no proven Switchyard incoming API capability",
    ):
        resolve_inference_transport(config, runner, "original-model")


def test_apply_switchyard_is_pure_and_uses_exact_route() -> None:
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="https://switchyard.example",
                route="exact-route",
                expected_version="pinned-version",
            ),
        )
    )
    capability = RunnerTransportCapability(
        incoming_api_formats=("anthropic-messages",),
        endpoint_override=True,
        model_override=True,
    )
    resolved = resolve_inference_transport(
        config, _Runner(transport_capability=capability), "original-model"
    )

    applied = apply_inference_transport(
        resolved,
        argv=["runner"],
        env={"UNCHANGED": "yes"},
        endpoint_environment_variable="RUNNER_BASE_URL",
    )

    assert applied.model == "exact-route"
    assert applied.argv == ("runner",)
    assert applied.environment == {
        "UNCHANGED": "yes",
        "RUNNER_BASE_URL": "https://switchyard.example",
    }
