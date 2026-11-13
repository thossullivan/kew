"""Tests for config loading and validation."""

import json as _json
import pickle
import re
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import kew.config as kew_config
from kew.config import (
    AgentConfig,
    AlertingConfig,
    ApprovalConfig,
    BranchConfig,
    BudgetConfig,
    ClaudeCodeConfig,
    CommandsConfig,
    ConfigError,
    ConfigErrorKind,
    DelegationConfig,
    DispatchConfig,
    EpicConfig,
    HumanReviewConfig,
    LoggingConfig,
    ModelEolConfig,
    OpenShellConfig,
    OverlapConfig,
    ParallelConfig,
    PricingConfig,
    PromptConfig,
    RetryConfig,
    ReviewConfig,
    SandboxConfig,
    SwitchyardSidecarConfig,
    TriggersConfig,
    load_config,
    project_root,
    resolved_config_path,
)
from kew.model_eol_snapshot import LifecycleEntry

_COLLECTION_DEFAULT_WORKTREE_BASE = DispatchConfig().branches.worktree_base


def test_codex_config_defaults_to_cli_reasoning():
    config = DispatchConfig()
    assert config.agent.codex.reasoning_effort == ""
    assert config.agent.codex.service_tier == ""


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_codex_config_accepts_documented_reasoning_effort(effort: str):
    config = DispatchConfig.model_validate(
        {"agent": {"codex": {"reasoning_effort": effort}}}
    )
    assert config.agent.codex.reasoning_effort == effort


def test_codex_config_rejects_unknown_reasoning_effort():
    with pytest.raises(ValidationError, match="reasoning_effort"):
        DispatchConfig.model_validate(
            {"agent": {"codex": {"reasoning_effort": "maximum"}}}
        )


def test_codex_config_accepts_fast_service_tier():
    config = DispatchConfig.model_validate(
        {"agent": {"codex": {"service_tier": "fast"}}}
    )
    assert config.agent.codex.service_tier == "fast"


@pytest.mark.parametrize("service_tier", ["standard", "priority", "flex", "turbo"])
def test_codex_config_rejects_ungoverned_service_tier(service_tier):
    with pytest.raises(ValidationError, match="service_tier"):
        DispatchConfig.model_validate(
            {"agent": {"codex": {"service_tier": service_tier}}}
        )


def test_codex_config_rejects_arbitrary_extra_args():
    with pytest.raises(ValidationError, match="extra_args"):
        DispatchConfig.model_validate(
            {"agent": {"codex": {"extra_args": ["--search"]}}}
        )


class TestW43RemovedConfigSpellings:
    """W4.3 (#2000): removed config spellings fail at load; the new ones work."""

    RENAMED_KEYS = (
        # (section path, old key, new key, value) -- rows 8-12
        (
            ("tracker", "linear"),
            "reconcile_interval_s",
            "reconcile_interval_seconds",
            7,
        ),
        (("delegation",), "poll_interval_s", "poll_interval_seconds", 7),
        (("delegation",), "fast_interval_s", "fast_interval_seconds", 7),
        (("delegation",), "refusal_cooldown_s", "refusal_cooldown_seconds", 7),
        (("commands",), "ci_poll_timeout_secs", "ci_poll_timeout_seconds", 7),
    )

    @staticmethod
    def _nest(path: tuple[str, ...], key: str, value: object) -> dict:
        data: dict = {key: value}
        for part in reversed(path):
            data = {part: data}
        return data

    @staticmethod
    def _read(config: DispatchConfig, path: tuple[str, ...], key: str) -> object:
        node: object = config
        for part in (*path, key):
            node = getattr(node, part)
        return node

    @pytest.mark.parametrize(("path", "old", "new", "value"), RENAMED_KEYS)
    def test_renamed_key_old_spelling_fails_new_works(
        self, tmp_path: Path, path, old, new, value
    ) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DispatchConfig.model_validate(self._nest(path, old, value))
        toml = tmp_path / "kew.toml"
        toml.write_text(f"[{'.'.join(path)}]\n{old} = {value}\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert old in str(exc_info.value)
        toml.write_text(f"[{'.'.join(path)}]\n{new} = {value}\n")
        assert self._read(load_config(config_path=toml), path, new) == value

    @pytest.mark.parametrize(
        "key",
        [
            "minimum_kew_version",  # row 14
            "minimum_schema_version",  # row 15
            "min_schema",  # row 15
            "schema",  # row 15
        ],
    )
    def test_capability_field_aliases_are_rejected(self, key: str) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "capability": {key: "0" if "kew" in key else 0}
                    }
                }
            )

    def test_capability_canonical_fields_still_work(self) -> None:
        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "capability": {"min_kew_version": "0.1", "min_schema_version": 0}
                }
            }
        )
        assert config.inference_transport.capability.min_kew_version == "0.1"
        assert config.inference_transport.capability.min_schema_version == 0

    def test_capability_pin_table_alias_is_rejected(self) -> None:
        """Row 16: the table is spelled ``capability`` only."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DispatchConfig.model_validate(
                {"inference_transport": {"capability_pin": {"min_schema_version": 0}}}
            )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("min_kew_version", "0.1"),
            ("min_schema_version", 0),
            ("minimum_kew_version", "0.1"),
            ("schema", 0),
        ],
    )
    def test_section_level_capability_fields_are_rejected(
        self, key: str, value: object
    ) -> None:
        """Row 17: the pin lives only in the nested ``capability`` table."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DispatchConfig.model_validate({"inference_transport": {key: value}})


class TestInferenceTransportConfig:
    def test_inference_transport_defaults_to_direct(self):
        config = DispatchConfig()
        assert config.inference_transport.mode == "direct"

    def test_capability_pin_accepts_current_build(self):
        from kew import __version__
        from kew.run_store import CURRENT_SCHEMA_VERSION

        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "allow_adaptive": True,
                    "capability": {
                        "min_kew_version": __version__,
                        "min_schema_version": CURRENT_SCHEMA_VERSION,
                    },
                }
            }
        )
        assert config.inference_transport.capability.min_kew_version == __version__
        assert (
            config.inference_transport.capability.min_schema_version
            == CURRENT_SCHEMA_VERSION
        )

    def test_capability_pin_rejects_older_kew(self):
        with pytest.raises(ValidationError, match="requires kew"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "allow_adaptive": True,
                        "capability": {"min_kew_version": "999.0.0"},
                    }
                }
            )

    def test_capability_pin_rejects_older_schema(self):
        from kew.run_store import CURRENT_SCHEMA_VERSION

        with pytest.raises(ValidationError, match="schema"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "allow_adaptive": True,
                        "capability": {
                            "min_schema_version": CURRENT_SCHEMA_VERSION + 1
                        },
                    }
                }
            )

    def test_inference_transport_accepts_external_switchyard(self):
        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "mode": "switchyard",
                    "switchyard": {
                        "endpoint": "https://switchyard.example.com:4000",
                        "route": "switchyard/passthrough",
                        "expected_version": "6ff491a",
                    },
                }
            }
        )
        assert config.inference_transport.switchyard.managed is False
        assert config.inference_transport.switchyard.config_path is None

    def test_inference_transport_accepts_managed_switchyard(self, tmp_path: Path):
        route_config = tmp_path / "routes.toml"
        route_config.write_text("[routes]\n")
        binary = tmp_path / "switchyard-server"
        binary.write_bytes(b"binary")
        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "mode": "switchyard",
                    "switchyard": {
                        "endpoint": "http://127.0.0.1:4000",
                        "route": "switchyard/passthrough",
                        "expected_version": "6ff491a",
                        "managed": True,
                        "config_path": str(route_config),
                        "binary": str(binary),
                        "binary_sha256": "a" * 64,
                        "startup_timeout_seconds": 20,
                    },
                }
            }
        )
        assert config.inference_transport.switchyard.managed is True

    def test_switchyard_sidecar_candidate_is_separate_and_inactive(self):
        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "mode": "switchyard",
                    "switchyard": {
                        "endpoint": "https://switchyard.example.com:4000",
                        "route": "switchyard/passthrough",
                        "expected_version": "6ff491a",
                        "sidecar": {
                            "image": "kew-switchyard:docker-4f6d5727b081",
                            "egress_network": "approved-egress",
                        },
                    },
                }
            }
        )

        sidecar = config.inference_transport.switchyard.sidecar
        assert sidecar is not None
        assert sidecar.enabled is False
        assert sidecar.image == "kew-switchyard:docker-4f6d5727b081"
        assert sidecar.egress_network == "approved-egress"
        assert config.inference_transport.switchyard.managed is False

    def test_sidecar_transport_separates_upstream_and_worker_auth(
        self, tmp_path: Path
    ) -> None:
        route_config = tmp_path / "routes.toml"
        route_config.write_text("[routes]\n", encoding="utf-8")

        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "mode": "switchyard",
                    "switchyard": {
                        "route": "switchyard/passthrough",
                        "expected_version": "0.1.0",
                        "config_path": str(route_config),
                        "credential_env": ["OPENAI_API_KEY"],
                        "sidecar": {
                            "enabled": True,
                            "image": "kew-switchyard:pinned",
                            "egress_network": "approved-egress",
                        },
                    },
                }
            }
        )

        switchyard = config.inference_transport.switchyard
        assert switchyard.endpoint == ""
        assert switchyard.managed is False
        assert switchyard.credential_env == ("OPENAI_API_KEY",)
        assert switchyard.sidecar == SwitchyardSidecarConfig(
            enabled=True,
            image="kew-switchyard:pinned",
            egress_network="approved-egress",
        )
        from kew.inference_transport import resolve_inference_transport
        from kew.runners import get_runner

        resolved = resolve_inference_transport(
            config,
            get_runner("claude-code"),
            "claude-sonnet-4-6",
        )
        assert resolved.endpoint is None
        assert resolved.sidecar is True
        assert resolved.credential_env_names == ("OPENAI_API_KEY",)
        assert resolved.worker_credential_env_name == "ANTHROPIC_API_KEY"

    def test_sidecar_config_preserves_host_supervisor_guards(
        self, tmp_path: Path
    ) -> None:
        route_config = tmp_path / "routes.toml"
        route_config.write_text("[routes]\n", encoding="utf-8")
        binary = tmp_path / "switchyard-server"
        binary.write_bytes(b"binary")
        sidecar = {
            "enabled": True,
            "image": "kew-switchyard:pinned",
            "egress_network": "approved-egress",
        }

        with pytest.raises(ValidationError, match="separate modes"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "http://127.0.0.1:4000",
                            "route": "switchyard/passthrough",
                            "expected_version": "0.1.0",
                            "managed": True,
                            "config_path": str(route_config),
                            "binary": str(binary),
                            "binary_sha256": "a" * 64,
                            "sidecar": sidecar,
                        },
                    }
                }
            )

        with pytest.raises(ValidationError, match="endpoint"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "route": "switchyard/passthrough",
                            "expected_version": "0.1.0",
                        },
                    }
                }
            )

    def test_sidecar_requires_exact_runtime_version(self, tmp_path: Path) -> None:
        route_config = tmp_path / "routes.toml"
        route_config.write_text("[routes]\n", encoding="utf-8")
        compatibility = tmp_path / "compatibility.md"
        compatibility.write_text("reviewed", encoding="utf-8")

        with pytest.raises(ValidationError, match="expected_version"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "route": "switchyard/passthrough",
                            "config_path": str(route_config),
                            "compatibility_record_path": str(compatibility),
                            "sidecar": {
                                "enabled": True,
                                "image": "kew-switchyard:pinned",
                                "egress_network": "approved-egress",
                            },
                        },
                    }
                }
            )

    @pytest.mark.parametrize(
        "sidecar",
        [
            {"enabled": True, "image": "proxy", "egress_network": "egress"},
            {"enabled": 0, "image": "proxy", "egress_network": "egress"},
            {"image": "", "egress_network": "egress"},
            {"image": " proxy", "egress_network": "egress"},
            {"image": "proxy", "egress_network": ""},
            {"image": "proxy", "egress_network": "--internal"},
            {"image": "proxy", "egress_network": "egress network"},
        ],
    )
    def test_switchyard_sidecar_candidate_rejects_activation_or_partial_config(
        self, sidecar: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError, match="sidecar|egress_network|image"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "https://switchyard.example.com:4000",
                            "route": "switchyard/passthrough",
                            "expected_version": "6ff491a",
                            "sidecar": sidecar,
                        },
                    }
                }
            )

    def test_host_managed_switchyard_rejects_sidecar_candidate(self, tmp_path: Path):
        route_config = tmp_path / "routes.toml"
        route_config.write_text("[routes]\n")
        binary = tmp_path / "switchyard-server"
        binary.write_bytes(b"binary")

        with pytest.raises(ValidationError, match="sidecar"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "http://127.0.0.1:4000",
                            "route": "switchyard/passthrough",
                            "expected_version": "6ff491a",
                            "managed": True,
                            "config_path": str(route_config),
                            "binary": str(binary),
                            "binary_sha256": "a" * 64,
                            "sidecar": {
                                "image": "proxy",
                                "egress_network": "egress",
                            },
                        },
                    }
                }
            )

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://localhost:4000",
            "http://192.0.2.1:4000",
            "ftp://127.0.0.1:4000",
            "https://user:pass@example.com",
            "https://example.com/path",
            "https://example.com?token=value",
            "https://example.com#fragment",
        ],
    )
    def test_inference_transport_rejects_unsafe_endpoint(self, endpoint: str):
        with pytest.raises(ValidationError, match="endpoint"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": endpoint,
                            "route": "switchyard/passthrough",
                            "expected_version": "6ff491a",
                        },
                    }
                }
            )

    def test_inference_transport_requires_version_or_compatibility_record(self):
        with pytest.raises(ValidationError, match="expected_version"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "https://switchyard.example.com",
                            "route": "switchyard/passthrough",
                        },
                    }
                }
            )

    def test_inference_transport_accepts_compatibility_record(self, tmp_path: Path):
        record = tmp_path / "compatibility.md"
        record.write_text("approved compatibility evidence")
        config = DispatchConfig.model_validate(
            {
                "inference_transport": {
                    "mode": "switchyard",
                    "switchyard": {
                        "endpoint": "https://switchyard.example.com",
                        "route": "switchyard/passthrough",
                        "compatibility_record_path": str(record),
                    },
                }
            }
        )
        assert config.inference_transport.switchyard.compatibility_record_path == record

    @pytest.mark.parametrize(
        "overrides,match",
        [
            ({"fallback": "direct"}, "fallback"),
            ({"startup_timeout_seconds": 0}, "startup_timeout_seconds"),
            ({"route": ""}, "route"),
            ({"unknown": True}, "unknown"),
        ],
    )
    def test_inference_transport_rejects_invalid_switchyard_settings(
        self, overrides: dict[str, object], match: str
    ):
        switchyard = {
            "endpoint": "https://switchyard.example.com",
            "route": "switchyard/passthrough",
            "expected_version": "6ff491a",
            **overrides,
        }
        with pytest.raises(ValidationError, match=match):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": switchyard,
                    }
                }
            )

    def test_inference_transport_managed_requires_files_and_digest(self):
        with pytest.raises(ValidationError, match="config_path"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "http://127.0.0.1:4000",
                            "route": "switchyard/passthrough",
                            "expected_version": "6ff491a",
                            "managed": True,
                        },
                    }
                }
            )

    @pytest.mark.parametrize(
        "credential_env",
        [
            ["GITHUB_TOKEN"],
            ["OPENAI_API_KEY", "OPENAI_API_KEY"],
            [""],
        ],
    )
    def test_managed_switchyard_rejects_unsafe_credential_declarations(
        self, tmp_path: Path, credential_env: list[str]
    ) -> None:
        binary = tmp_path / "switchyard-server"
        binary.write_text("binary")
        config_path = tmp_path / "routes.toml"
        config_path.write_text("schema_version = 1")

        with pytest.raises(ValidationError, match="credential_env"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "http://127.0.0.1:4400",
                            "route": "switchyard/passthrough",
                            "expected_version": "0.1.0",
                            "managed": True,
                            "config_path": str(config_path),
                            "binary": str(binary),
                            "binary_sha256": "a" * 64,
                            "credential_env": credential_env,
                        },
                    }
                }
            )

    def test_inference_transport_managed_rejects_symlink_config(self, tmp_path: Path):
        target = tmp_path / "routes.toml"
        target.write_text("[routes]\n")
        link = tmp_path / "routes-link.toml"
        link.symlink_to(target)
        binary = tmp_path / "switchyard-server"
        binary.write_bytes(b"binary")
        with pytest.raises(ValidationError, match="symlink"):
            DispatchConfig.model_validate(
                {
                    "inference_transport": {
                        "mode": "switchyard",
                        "switchyard": {
                            "endpoint": "http://127.0.0.1:4000",
                            "route": "switchyard/passthrough",
                            "expected_version": "6ff491a",
                            "managed": True,
                            "config_path": str(link),
                            "binary": str(binary),
                            "binary_sha256": "a" * 64,
                        },
                    }
                }
            )


def test_sidecar_transport_separates_upstream_and_worker_auth(
    tmp_path: Path,
) -> None:
    TestInferenceTransportConfig().test_sidecar_transport_separates_upstream_and_worker_auth(
        tmp_path
    )


def test_sidecar_config_preserves_host_supervisor_guards(tmp_path: Path) -> None:
    TestInferenceTransportConfig().test_sidecar_config_preserves_host_supervisor_guards(
        tmp_path
    )


class TestDispatchConfigDefaults:
    """DispatchConfig should have sensible defaults for all fields."""

    def test_default_agent_timeout(self):
        config = DispatchConfig()
        assert config.agent.timeout_minutes == 120

    def test_default_agent_budget(self):
        config = DispatchConfig()
        assert config.agent.max_budget_usd == 25.0

    def test_default_agent_skip_permissions(self):
        config = DispatchConfig()
        assert config.agent.claude_code.skip_permissions is True

    def test_default_branch_prefix(self):
        config = DispatchConfig()
        assert config.branches.prefix == "agent/issue-"

    def test_default_branch_base_empty(self):
        config = DispatchConfig()
        assert config.branches.base == ""

    def test_default_labels_ready(self):
        config = DispatchConfig()
        assert config.labels.ready == "ready"

    def test_incomplete_run_label_default(self):
        config = DispatchConfig()
        assert config.labels.incomplete_run == "incomplete-run"

    def test_default_priorities(self):
        config = DispatchConfig()
        assert config.labels.priorities == [
            "p0-critical",
            "p1-high",
            "p2-medium",
            "p3-low",
        ]

    def test_default_retry_max_attempts(self):
        config = DispatchConfig()
        assert config.retry.max_attempts == 3

    def test_default_retriable_exit_codes(self):
        config = DispatchConfig()
        assert config.retry.retriable_exit_codes == [1]

    def test_default_parallel_max_workers(self):
        config = DispatchConfig()
        assert config.parallel.max_workers == 1


class TestDispatchConfigOverrides:
    """Config fields can be overridden."""

    def test_override_timeout(self):
        config = DispatchConfig(agent=AgentConfig(timeout_minutes=60))
        assert config.agent.timeout_minutes == 60

    def test_override_commands(self):
        config = DispatchConfig(
            commands=CommandsConfig(ci="npm test", format="prettier --write .")
        )
        assert config.commands.ci == "npm test"
        assert config.commands.format == "prettier --write ."


class TestParallelValidation:
    """max_workers validation."""

    def test_max_workers_1_is_valid(self):
        config = ParallelConfig(max_workers=1)
        assert config.max_workers == 1

    def test_max_workers_gt_1_is_valid(self):
        config = ParallelConfig(max_workers=3)
        assert config.max_workers == 3

    def test_max_workers_0_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            ParallelConfig(max_workers=0)

    def test_max_workers_negative_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            ParallelConfig(max_workers=-1)

    def test_default_shutdown_grace(self):
        config = ParallelConfig()
        assert config.shutdown_grace_seconds == 30


class TestReviewConfig:
    """ReviewConfig defaults and validation."""

    def test_default_auto_reply(self):
        config = ReviewConfig()
        assert config.auto_reply is True

    def test_default_include_resolved(self):
        config = ReviewConfig()
        assert config.include_resolved is False

    def test_review_config_on_dispatch_config(self):
        config = DispatchConfig()
        assert config.review.auto_reply is True
        assert config.review.include_resolved is False

    def test_governed_review_defaults(self):
        config = ReviewConfig()
        assert config.adversarial is False
        assert config.blocking is True
        assert config.max_rounds == 2
        assert config.max_review_usd == 10.0
        assert config.max_remediation_usd == 40.0
        assert config.model == ""
        assert config.runner == ""
        assert not hasattr(config, "min_severity_to_block")

    def test_runner_round_trips_through_toml(self, tmp_path: Path):
        """[review] runner pins the reviewer's own route independently of node
        routing (#1069); empty default = claude-code."""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b'[review]\nrunner = "claude-code"\n')
        assert load_config(config_path=toml_file).review.runner == "claude-code"

    @pytest.mark.parametrize("runner", ["opencode", "codex", "kimi"])
    def test_a_runner_without_a_verdict_channel_is_a_config_error(
        self, tmp_path: Path, runner: str
    ):
        """The verdict comes only from the runner's own terminal stream event
        (#2021); a reviewer that could never produce one fails at load, not on
        every review round."""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(f'[review]\nrunner = "{runner}"\n')
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml_file)
        assert exc_info.value.key == "[review].runner"
        message = str(exc_info.value)
        assert "'claude-code' or empty" in message
        assert f"got {runner!r}" in message
        assert "terminal stream event" in message

    def test_runner_typo_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ReviewConfig.model_validate({"runnner": "opencode"})  # typo
        assert "runnner" in str(exc_info.value)

    def test_max_remediation_usd_rejects_negative(self):
        with pytest.raises(ValidationError):
            ReviewConfig(max_remediation_usd=-1.0)

    def test_max_remediation_usd_rejects_inf(self):
        with pytest.raises(ValidationError):
            ReviewConfig(max_remediation_usd=float("inf"))

    def test_max_remediation_usd_accepts_zero(self):
        assert ReviewConfig(max_remediation_usd=0.0).max_remediation_usd == 0.0

    def test_review_config_refuses_the_removed_severity_floor(self, tmp_path: Path):
        """[review].min_severity_to_block is gone (#2017): it was documented and
        attested into every verdict, but nothing ever blocked on it. The
        severity knob kew enforces is ``stop_rule_max_severity``, so a config
        that still sets the floor fails at load, naming the key."""
        with pytest.raises(ValidationError) as exc_info:
            ReviewConfig(min_severity_to_block="high")  # type: ignore[call-arg]
        assert "min_severity_to_block" in str(exc_info.value)
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b'[review]\nmin_severity_to_block = "high"\n')
        with pytest.raises(ConfigError) as load_info:
            load_config(config_path=toml_file)
        assert "min_severity_to_block" in str(load_info.value)
        assert "review.stop_rule_max_severity" in str(load_info.value)

    def test_effort_key_is_rejected(self):
        """[review].effort is gone (A04): it was attested into the verdict
        payload but never reached the reviewer command, so a config that still
        sets it must fail loudly rather than be silently ignored."""
        with pytest.raises(ValidationError) as exc_info:
            ReviewConfig.model_validate({"effort": "high"})
        assert "effort" in str(exc_info.value)

    def test_effort_key_is_rejected_from_toml(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b'[review]\neffort = "high"\n')
        with pytest.raises(ConfigError):
            load_config(config_path=toml_file)


class TestLoadConfig:
    """load_config reads kew.toml and applies env overrides."""

    def test_load_from_toml_string(self, tmp_path: Path):
        toml_content = b"""
[project]
name = "test-project"

[agent]
timeout_minutes = 60

[commands]
ci = "make test"
"""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(toml_content)

        config = load_config(config_path=toml_file)
        assert config.project.name == "test-project"
        assert config.agent.timeout_minutes == 60
        assert config.commands.ci == "make test"
        # Non-overridden fields keep defaults
        assert config.agent.max_budget_usd == 25.0

    def test_load_missing_file_returns_defaults(self, tmp_path: Path):
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.project.name == ""
        assert config.agent.timeout_minutes == 120

    @pytest.mark.parametrize(
        ("content", "key"),
        [
            ("[agent\ntimeout_minutes = 60\n", "key -:"),
            ('[agent]\ntimeout_minutes = "soon"\n', "[agent].timeout_minutes"),
            ("[agent]\ntimeout_minuttes = 60\n", "[agent].timeout_minuttes"),
        ],
    )
    def test_cli_config_errors_are_short_actionable_envelopes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        content: str,
        key: str,
    ) -> None:
        from typer.testing import CliRunner

        from kew.cli import app

        config_path = tmp_path / "kew.toml"
        config_path.write_text(content, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(app, ["status"])

        # A configuration failure is "could not run": public code 2 (W4.2).
        assert result.exit_code == 2
        assert len(result.output.splitlines()) <= 3
        assert str(config_path) in result.output
        assert key in result.output
        assert "expected" in result.output
        assert "fix:" in result.output
        assert "Traceback" not in result.output

    def test_config_error_dict_round_trips_through_json(self, tmp_path: Path) -> None:
        related = ConfigError(
            path=tmp_path / "kew.toml",
            key="[budget].weekly_usd",
            expectation="weekly_usd must be >= 0",
            fix="use zero or a positive number",
            kind="invalid_value",
        )
        error = ConfigError(
            path=tmp_path / "kew.toml",
            key="[agent].timeout_minutes",
            expectation="an integer",
            fix="use an integer for [agent].timeout_minutes",
            kind="invalid_value",
            related=(related,),
        )

        assert _json.loads(_json.dumps(error.to_dict())) == {
            "path": str(tmp_path / "kew.toml"),
            "key": "[agent].timeout_minutes",
            "expectation": "an integer",
            "fix": "use an integer for [agent].timeout_minutes",
            "kind": "invalid_value",
            "related": [
                {
                    "path": str(tmp_path / "kew.toml"),
                    "key": "[budget].weekly_usd",
                    "expectation": "weekly_usd must be >= 0",
                    "fix": "use zero or a positive number",
                    "kind": "invalid_value",
                    "related": [],
                }
            ],
        }

    def test_config_error_pickle_round_trip(self, tmp_path: Path) -> None:
        error = ConfigError(
            path=tmp_path / "kew.toml",
            key="[agent].timeout_minutes",
            expectation="an integer",
            fix="use an integer",
            kind="invalid_value",
        )

        restored = pickle.loads(pickle.dumps(error))

        assert isinstance(restored, ConfigError)
        assert restored.to_dict() == error.to_dict()
        assert str(restored) == str(error)

    def test_config_error_kinds_use_snake_case(self) -> None:
        kinds = get_args(ConfigErrorKind)

        assert kinds
        assert all(re.fullmatch(r"[a-z_]+", kind) for kind in kinds)

    def test_all_pydantic_errors_are_rendered(self, tmp_path: Path) -> None:
        config_path = tmp_path / "kew.toml"
        config_path.write_text("[budget]\ndaily_usd = -1\nweekly_usd = -2\n")

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=config_path)

        rendered = str(exc_info.value)
        assert "[budget].daily_usd" in rendered
        assert "[budget].weekly_usd" in rendered
        assert "Value error, " not in rendered

    def test_env_overrides_toml(self, tmp_path: Path):
        toml_content = b"""
[agent]
timeout_minutes = 120
"""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(toml_content)

        with patch.dict("os.environ", {"KEW_TIMEOUT": "30"}):
            config = load_config(config_path=toml_file)
        assert config.agent.timeout_minutes == 30

    def test_env_override_repo(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")

        with patch.dict("os.environ", {"KEW_REPO": "owner/repo"}):
            config = load_config(config_path=toml_file)
        assert config.project.repo == "owner/repo"

    def test_env_override_max_budget(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")

        with patch.dict("os.environ", {"KEW_MAX_BUDGET": "25.0"}):
            config = load_config(config_path=toml_file)
        assert config.agent.max_budget_usd == 25.0

    def test_audit_staleness_window_defaults_zero(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")
        assert load_config(config_path=toml_file).audit.staleness_window == 0

    def test_env_override_audit_staleness_window(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")
        with patch.dict("os.environ", {"KEW_AUDIT_STALENESS_WINDOW": "42"}):
            config = load_config(config_path=toml_file)
        assert config.audit.staleness_window == 42

    def test_audit_staleness_window_rejects_negative(self):
        from pydantic import ValidationError

        from kew.config import AuditConfig

        with pytest.raises(ValidationError):
            AuditConfig(staleness_window=-1)

    def test_audit_anchor_max_age_days_defaults_seven(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")
        assert load_config(config_path=toml_file).audit.anchor_max_age_days == 7

    def test_env_override_audit_anchor_max_age_days(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"")
        with patch.dict("os.environ", {"KEW_AUDIT_ANCHOR_MAX_AGE_DAYS": "60"}):
            config = load_config(config_path=toml_file)
        assert config.audit.anchor_max_age_days == 60

    def test_audit_anchor_max_age_days_rejects_negative(self):
        from pydantic import ValidationError

        from kew.config import AuditConfig

        with pytest.raises(ValidationError):
            AuditConfig(anchor_max_age_days=-1)

    def test_finds_kew_toml_in_parent_directory(self, tmp_path: Path):
        """load_config walks up from cwd to find kew.toml in a parent dir."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        sub = root / "packages" / "backend"
        sub.mkdir(parents=True)
        (root / "kew.toml").write_bytes(b'[project]\nname = "from-parent"\n')
        with patch("kew.config.Path.cwd", return_value=sub):
            config = load_config()
        assert config.project.name == "from-parent"

    def test_dispatch_toml_in_parent_directory_refuses_to_load(self, tmp_path: Path):
        """W4.3 row 19: a lone dispatch.toml is refused, never parsed or defaulted.

        Loading defaults in its place would silently drop the operator's budget
        caps; the file holds invalid TOML to prove kew never reads it.
        """
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        sub = root / "src"
        sub.mkdir()
        (root / "dispatch.toml").write_bytes(b"[budget\nper_issue_usd = 3.0\n")
        with (
            patch("kew.config.Path.cwd", return_value=sub),
            pytest.raises(ConfigError) as exc_info,
        ):
            load_config()
        assert exc_info.value.kind == "unread_config_file"
        assert exc_info.value.path == root / "dispatch.toml"
        assert "kew does not read dispatch.toml" in str(exc_info.value)

    def test_nearer_dispatch_toml_refuses_before_a_parent_kew_toml(
        self, tmp_path: Path
    ):
        """The nearest directory holding either file decides."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "kew.toml").write_bytes(b'[project]\nname = "parent"\n')
        sub = root / "svc"
        sub.mkdir()
        (sub / "dispatch.toml").write_bytes(b"")
        with (
            patch("kew.config.Path.cwd", return_value=sub),
            pytest.raises(ConfigError, match="dispatch.toml"),
        ):
            load_config()

    def test_dispatch_toml_above_the_git_root_is_not_consulted(self, tmp_path: Path):
        (tmp_path / "dispatch.toml").write_bytes(b"")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch("kew.config.Path.cwd", return_value=repo):
            assert load_config().project.name == ""

    def test_explicit_config_path_skips_the_dispatch_toml_check(self, tmp_path: Path):
        (tmp_path / "dispatch.toml").write_bytes(b"")
        explicit = tmp_path / "custom.toml"
        explicit.write_text('[project]\nname = "explicit"\n')
        with patch("kew.config.Path.cwd", return_value=tmp_path):
            assert load_config(config_path=explicit).project.name == "explicit"

    def test_stops_at_git_root_boundary(self, tmp_path: Path):
        """Should not look above the git root for kew.toml."""
        # kew.toml above the git root — should NOT be found
        (tmp_path / "kew.toml").write_bytes(b'[project]\nname = "above-git-root"\n')
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "src"
        sub.mkdir()
        with patch("kew.config.Path.cwd", return_value=sub):
            config = load_config()
        # Should get defaults, not the config above git root
        assert config.project.name == ""

    def test_returns_defaults_when_no_config_found(self, tmp_path: Path):
        """Returns defaults when no kew.toml exists anywhere up the tree."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "deep" / "nested"
        sub.mkdir(parents=True)
        with patch("kew.config.Path.cwd", return_value=sub):
            config = load_config()
        assert config.project.name == ""
        assert config.agent.model == ""

    def test_dispatch_toml_beside_kew_toml_is_ignored(self, tmp_path: Path):
        """Only kew.toml is read; a sibling dispatch.toml has no effect."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        sub = root / "src"
        sub.mkdir()
        (root / "kew.toml").write_bytes(b'[project]\nname = "kew-config"\n')
        (root / "dispatch.toml").write_bytes(b'[project]\nname = "dispatch-config"\n')
        with patch("kew.config.Path.cwd", return_value=sub):
            config = load_config()
        assert config.project.name == "kew-config"


class TestProjectSelection:
    """--project / KEW_PROJECT resolve config + DB from a chosen dir (F1, #862)."""

    def test_kew_project_resolves_config_regardless_of_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """KEW_PROJECT reads config from that dir even from an unrelated cwd."""
        project = tmp_path / "svc-a"
        project.mkdir()
        (project / "kew.toml").write_bytes(b'[project]\nname = "svc-a"\n')
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".git").mkdir()
        (elsewhere / "kew.toml").write_bytes(b'[project]\nname = "wrong-cwd"\n')

        monkeypatch.setenv("KEW_PROJECT", str(project))
        with patch("kew.config.Path.cwd", return_value=elsewhere):
            config = load_config()
        assert config.project.name == "svc-a"

    def test_kew_project_anchors_relative_db_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A relative logging.dir (and thus the DB) is anchored to the project."""
        project = tmp_path / "svc-b"
        project.mkdir()
        (project / "kew.toml").write_bytes(b'[logging]\ndir = "logs/dispatch"\n')
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()

        monkeypatch.setenv("KEW_PROJECT", str(project))
        with patch("kew.config.Path.cwd", return_value=elsewhere):
            config = load_config()
        assert Path(config.logging.dir) == (project / "logs" / "dispatch").resolve()

    def test_kew_project_anchors_default_db_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With no configured logging.dir the default DB still lands under project."""
        project = tmp_path / "svc-c"
        project.mkdir()
        (project / "kew.toml").write_bytes(b'[project]\nname = "svc-c"\n')

        # Drop conftest's absolute test-only default so this exercises the real
        # production default ("./logs/dispatch"), which is what gets anchored.
        monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", None)
        monkeypatch.setenv("KEW_PROJECT", str(project))
        config = load_config()
        assert Path(config.logging.dir) == (project / "logs" / "dispatch").resolve()

    def test_kew_project_no_config_file_resolves_from_project_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A selected project with no kew.toml never falls back to the cwd."""
        project = tmp_path / "empty-project"
        project.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".git").mkdir()
        (cwd / "kew.toml").write_bytes(b'[project]\nname = "wrong-cwd"\n')

        monkeypatch.setenv("KEW_PROJECT", str(project))
        with patch("kew.config.Path.cwd", return_value=cwd):
            config = load_config()
        # Defaults, not the cwd's config.
        assert config.project.name == ""

    def test_env_log_dir_still_wins_over_project_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Explicit KEW_LOG_DIR overrides the project-anchored log dir."""
        project = tmp_path / "svc-d"
        project.mkdir()
        (project / "kew.toml").write_bytes(b'[project]\nname = "svc-d"\n')
        override = tmp_path / "explicit-logs"

        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.setenv("KEW_LOG_DIR", str(override))
        config = load_config()
        assert config.logging.dir == str(override)

    def test_resolved_config_path_honors_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """resolved_config_path resolves from the selected project dir."""
        project = tmp_path / "svc-e"
        project.mkdir()
        (project / "kew.toml").write_bytes(b'[project]\nname = "svc-e"\n')
        elsewhere = tmp_path / "cwd"
        elsewhere.mkdir()

        monkeypatch.setenv("KEW_PROJECT", str(project))
        with patch("kew.config.Path.cwd", return_value=elsewhere):
            path = resolved_config_path()
        assert path == project / "kew.toml"

    def test_collection_time_default_worktree_is_session_isolated(self) -> None:
        collection_default = Path(_COLLECTION_DEFAULT_WORKTREE_BASE)

        assert collection_default.is_absolute()
        assert collection_default.name.startswith("kew-test-worktrees-")
        from conftest import _SESSION_WORKTREE_DIR

        # TMPDIR may be inside the checkout; the unique session root is isolated.
        assert collection_default == Path(_SESSION_WORKTREE_DIR)
        assert collection_default.is_dir()

    def test_each_test_gets_an_isolated_default_worktree(self, tmp_path: Path) -> None:
        assert DispatchConfig().branches.worktree_base == str(
            tmp_path / "kew-worktrees"
        )

    def test_explicit_worktree_setting_ignores_the_test_default(
        self, tmp_path: Path
    ) -> None:
        explicit = tmp_path / "operator-checkouts"

        config = DispatchConfig(branches=BranchConfig(worktree_base=str(explicit)))

        assert config.branches.worktree_base == str(explicit)

    def test_production_worktree_default_remains_dot_worktrees(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kew_config, "_DEFAULT_WORKTREE_BASE_OVERRIDE", None)

        assert BranchConfig().worktree_base == ".worktrees"

    @pytest.mark.parametrize("configured", [None, "var/checkouts"])
    def test_project_anchors_relative_worktree_base_outside_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured: str | None,
    ) -> None:
        project = (tmp_path / "selected").resolve()
        project.mkdir()
        value = configured or ".worktrees"
        content = (
            "" if configured is None else f'[branches]\nworktree_base = "{value}"\n'
        )
        (project / "kew.toml").write_text(content, encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.delenv("KEW_WORKTREE_BASE", raising=False)
        if configured is None:
            monkeypatch.setattr(kew_config, "_DEFAULT_WORKTREE_BASE_OVERRIDE", None)

        config = load_config()

        assert config.branches.worktree_base == str((project / value).resolve())

    def test_absolute_worktree_base_remains_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = (tmp_path / "selected").resolve()
        project.mkdir()
        checkout_root = tmp_path / "absolute-checkouts"
        (project / "kew.toml").write_text(
            f'[branches]\nworktree_base = "{checkout_root}"\n', encoding="utf-8"
        )
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.delenv("KEW_WORKTREE_BASE", raising=False)

        config = load_config()

        assert config.branches.worktree_base == str(checkout_root)

    def test_project_anchors_relative_worktree_base_environment_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = (tmp_path / "selected").resolve()
        project.mkdir()
        (project / "kew.toml").write_text(
            '[branches]\nworktree_base = "toml-checkouts"\n', encoding="utf-8"
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.setenv("KEW_WORKTREE_BASE", "environment-checkouts")

        config = load_config()

        assert config.branches.worktree_base == str(
            (project / "environment-checkouts").resolve()
        )


class TestRelativeLogDirAnchoring:
    """A relative logging.dir follows the project root, not cwd (#1156)."""

    @staticmethod
    def _make_project(tmp_path: Path, log_dir: str = "./logs/dispatch") -> Path:
        project = (tmp_path / "repo").resolve()
        (project / ".git").mkdir(parents=True)
        (project / "kew.toml").write_text(
            f'[logging]\ndir = "{log_dir}"\n', encoding="utf-8"
        )
        (project / "kew").mkdir()
        (project / "www").mkdir()
        return project

    def test_relative_log_dir_anchors_to_project_root_from_a_subdirectory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The classic footgun: `cd kew/` used to mint `kew/logs/dispatch`."""
        project = self._make_project(tmp_path)
        monkeypatch.chdir(project / "kew")

        config = load_config()

        assert Path(config.logging.dir) == project / "logs" / "dispatch"

    def test_db_equals_project_plus_log_dir_from_every_subdirectory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`db=` must agree with `project=` — the banner's two halves (#1156).

        This is the acceptance criterion: status / audit verify / report /
        dispatch all derive the DB from ``logging.dir``, so one ledger from any
        directory inside the project.
        """
        project = self._make_project(tmp_path)
        expected = project / "logs" / "dispatch" / "dispatch.db"

        for cwd in (project, project / "kew", project / "www"):
            monkeypatch.chdir(cwd)
            config = load_config()
            banner_project = Path(str(resolved_config_path().parent))
            db_path = Path(config.logging.dir).resolve() / "dispatch.db"
            assert db_path == expected
            assert db_path == banner_project / "logs" / "dispatch" / "dispatch.db"

    def test_absolute_log_dir_keeps_its_meaning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Absolute paths are left exactly as configured."""
        elsewhere = (tmp_path / "elsewhere").resolve()
        project = self._make_project(tmp_path, log_dir=str(elsewhere))
        monkeypatch.chdir(project / "www")

        config = load_config()

        assert Path(config.logging.dir) == elsewhere

    def test_explicit_config_path_anchors_to_its_own_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A caller-supplied config file defines the root for its relative paths."""
        project = self._make_project(tmp_path)
        monkeypatch.chdir(project / "kew")

        config = load_config(project / "kew.toml")

        assert Path(config.logging.dir) == project / "logs" / "dispatch"

    def test_project_root_prefers_kew_project_over_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        project = self._make_project(tmp_path)
        selected = (tmp_path / "selected").resolve()
        selected.mkdir()
        monkeypatch.chdir(project / "www")
        monkeypatch.setenv("KEW_PROJECT", str(selected))

        assert project_root() == selected

    def test_project_root_falls_back_to_cwd_without_a_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bare = (tmp_path / "bare").resolve()
        (bare / ".git").mkdir(parents=True)
        monkeypatch.chdir(bare)

        assert project_root() == bare


class TestRetryConfig:
    """RetryConfig fields."""

    def test_default_max_attempts(self):
        config = RetryConfig()
        assert config.max_attempts == 3

    def test_default_max_resume_attempts(self):
        config = RetryConfig()
        assert config.max_resume_attempts == 2

    def test_default_backoff_base_seconds(self):
        config = RetryConfig()
        assert config.backoff_base_seconds == 30

    def test_default_backoff_multiplier(self):
        config = RetryConfig()
        assert config.backoff_multiplier == 2.0

    def test_max_resume_cannot_exceed_max_attempts(self):
        """max_resume_attempts is clamped to max_attempts - 1."""
        config = RetryConfig(max_attempts=2, max_resume_attempts=5)
        assert config.max_resume_attempts <= config.max_attempts - 1

    def test_max_attempts_zero_normalized_to_one(self):
        """max_attempts=0 is normalized to 1 to prevent unbound result."""
        config = RetryConfig(max_attempts=0)
        assert config.max_attempts == 1


class TestTriggersConfig:
    def test_default_ci_failure_max_retries(self):
        config = TriggersConfig()
        assert config.ci_failure_max_retries == 2

    def test_override(self):
        config = TriggersConfig(ci_failure_max_retries=5)
        assert config.ci_failure_max_retries == 5


class TestDeletedDeadKnobsAreUnknownKeys:
    """A01: four config surfaces that nothing read are gone, not silent.

    Each key below had zero consumers. Deleting the field is only half the
    job — ``extra="forbid"`` is what turns a stale ``kew.toml`` into a loud
    load error instead of a setting the operator believes is in force.
    """

    @pytest.mark.parametrize(
        "section, key",
        [
            ("triggers", "on_label"),
            ("triggers", "on_review"),
            ("triggers", "on_ci_failure"),
            ("alerting", "budget_warning_threshold_usd"),
            ("alerting", "budget_limit_usd"),
            ("logging", "level"),
            ("overlap.advanced", "llm_prediction"),
            ("overlap.advanced", "llm_model"),
            ("epic", "merge_strategy"),
            ("retry", "backoff_seconds"),
        ],
    )
    def test_a_deleted_key_is_rejected_as_unknown(self, section: str, key: str):
        parts = section.split(".")
        payload: dict[str, object] = {key: "x"}
        for part in reversed(parts):
            payload = {part: payload}

        with pytest.raises(ValidationError) as exc_info:
            DispatchConfig.model_validate(payload)

        assert "Extra inputs are not permitted" in str(exc_info.value)

    def test_the_whole_quality_section_is_rejected(self):
        """The section goes with its deprecation path — no warn-and-accept."""
        with pytest.raises(ValidationError) as exc_info:
            DispatchConfig.model_validate({"quality": {"max_diff_lines": 999}})

        assert "Extra inputs are not permitted" in str(exc_info.value)

    def test_quality_config_is_gone_from_the_module(self):
        assert not hasattr(kew_config, "QualityConfig")

    def test_a_stale_toml_fails_the_load_instead_of_warning(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"[quality]\nmax_diff_lines = 999\n")

        with pytest.raises(ConfigError):
            load_config(config_path=toml_file)

    def test_ci_failure_max_retries_is_the_only_trigger_key_and_still_loads(
        self, tmp_path: Path
    ):
        """The one wired key (ci_fix.py) survives the section it lived in."""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(b"[triggers]\nci_failure_max_retries = 5\n")

        config = load_config(config_path=toml_file)

        assert config.triggers.ci_failure_max_retries == 5
        assert set(TriggersConfig.model_fields) == {"ci_failure_max_retries"}

    def test_webhook_url_still_loads(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(
            b'[alerting]\nwebhook_url = "https://hooks.example.invalid/alert"\n'
        )

        config = load_config(config_path=toml_file)

        assert config.alerting.webhook_url == "https://hooks.example.invalid/alert"
        assert config.alerting.notify_on_failure is True

    def test_logging_keeps_dir_and_retention_days(self):
        config = DispatchConfig()

        assert set(LoggingConfig.model_fields) == {"dir", "retention_days"}
        assert config.logging.retention_days == 0


class TestBudgetConfig:
    def test_defaults(self):
        config = BudgetConfig()
        assert config.per_issue_usd == 15.0
        assert config.daily_usd == 50.0
        assert config.weekly_usd == 200.0

    def test_override(self):
        config = BudgetConfig(daily_usd=100.0)
        assert config.daily_usd == 100.0


class TestPricingConfig:
    def test_defaults_empty(self):
        config = PricingConfig()
        assert config.overrides == {}

    def test_custom_overrides(self):
        config = PricingConfig(overrides={"my-model": [10.0, 50.0]})
        assert config.overrides["my-model"] == [10.0, 50.0]


class TestNewLabelFields:
    def test_new_label_defaults(self):
        config = DispatchConfig()
        assert config.labels.agent_needs_help == "agent-needs-help"
        assert config.labels.budget_exceeded == "budget-exceeded"
        assert config.labels.large_pr == "large-pr"
        assert config.labels.ready_to_merge == "ready-to-merge"

    def test_new_labels_configurable(self):
        raw = {
            "labels": {
                "agent_needs_help": "custom-help",
                "budget_exceeded": "custom-budget",
            }
        }
        config = DispatchConfig.model_validate(raw)
        assert config.labels.agent_needs_help == "custom-help"
        assert config.labels.budget_exceeded == "custom-budget"


class TestCiApprovedLabel:
    """The human CI approval label for agent PRs (#2033)."""

    def test_missing_ci_approved_falls_back_to_the_default(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            '[project]\nname = "p"\n\n[labels]\nagent_pr = "bot-pr"\n',
            encoding="utf-8",
        )
        config = load_config(config_path=toml_file)
        assert config.labels.agent_pr == "bot-pr"
        assert config.labels.ci_approved == "ci-approved"

    def test_ci_approved_is_configurable_beside_agent_pr(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            '[project]\nname = "p"\n\n[labels]\nci_approved = "run-ci"\n',
            encoding="utf-8",
        )
        assert load_config(config_path=toml_file).labels.ci_approved == "run-ci"


class TestDispatchConfigNewSections:
    def test_triggers_in_config(self):
        config = DispatchConfig()
        assert isinstance(config.triggers, TriggersConfig)

    def test_budget_in_config(self):
        config = DispatchConfig()
        assert isinstance(config.budget, BudgetConfig)

    def test_pricing_in_config(self):
        config = DispatchConfig()
        assert isinstance(config.pricing, PricingConfig)

    def test_toml_loading_with_new_sections(self, tmp_path: Path):
        toml_content = b"""
[triggers]
ci_failure_max_retries = 5

[budget]
daily_usd = 100.0
per_issue_usd = 25.0

[pricing]
overrides = {"custom-model" = [10.0, 50.0]}
"""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(toml_content)
        config = load_config(config_path=toml_file)
        assert config.triggers.ci_failure_max_retries == 5
        assert config.budget.daily_usd == 100.0
        assert config.budget.per_issue_usd == 25.0
        assert config.pricing.overrides["custom-model"] == [10.0, 50.0]


class TestTheHumanReviewPolicySection:
    """I13: [human_review] owns the review policy — the only one."""

    def test_human_review_defaults_are_three_reviews_500_lines_and_20_paths(self):
        config = DispatchConfig()

        assert isinstance(config.human_review, HumanReviewConfig)
        assert config.human_review.max_pending_reviews == 3
        assert config.human_review.max_diff_lines == 500
        assert config.human_review.max_paths == 20
        assert config.human_review.approvers == []

    def test_human_review_toml_loading(self, tmp_path: Path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(
            b"[human_review]\n"
            b"max_pending_reviews = 1\n"
            b"max_diff_lines = 250\n"
            b"max_paths = 5\n"
            b'approvers = ["ada"]\n'
        )

        config = load_config(config_path=toml_file)

        assert config.human_review.max_pending_reviews == 1
        assert config.human_review.max_diff_lines == 250
        assert config.human_review.max_paths == 5
        assert config.human_review.approvers == ["ada"]

    def test_human_review_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            HumanReviewConfig(max_diff_liness=500)  # typo
        assert "max_diff_liness" in str(exc_info.value)

    @pytest.mark.parametrize(
        "field", ["max_pending_reviews", "max_diff_lines", "max_paths"]
    )
    def test_human_review_rejects_a_negative_bound(self, field: str):
        with pytest.raises(ValidationError) as exc_info:
            HumanReviewConfig(**{field: -1})
        assert "must be >= 0" in str(exc_info.value)

    @pytest.mark.parametrize(
        "field", ["max_pending_reviews", "max_diff_lines", "max_paths"]
    )
    def test_human_review_accepts_zero_as_hold_everything(self, field: str):
        """Zero is the loud end of the range, not a disabled check."""
        assert getattr(HumanReviewConfig(**{field: 0}), field) == 0


class TestPromptConfigRepoMap:
    """PromptConfig.repo_map_tokens validation."""

    def test_default_repo_map_tokens(self):
        config = PromptConfig()
        assert config.repo_map_tokens == 1024

    def test_repo_map_tokens_zero_raises(self):
        with pytest.raises(ValueError, match="repo_map_tokens must be at least 1"):
            PromptConfig(repo_map_tokens=0)

    def test_repo_map_tokens_negative_raises(self):
        with pytest.raises(ValueError, match="repo_map_tokens must be at least 1"):
            PromptConfig(repo_map_tokens=-5)

    def test_repo_map_tokens_one_is_valid(self):
        config = PromptConfig(repo_map_tokens=1)
        assert config.repo_map_tokens == 1

    def test_default_include_repo_map(self):
        config = PromptConfig()
        assert config.include_repo_map is True

    def test_default_repo_map_mode(self):
        """Default stays 'hint' — no behavior change without the experiment."""
        config = PromptConfig()
        assert config.repo_map_mode == "hint"

    def test_repo_map_mode_stable_is_valid(self):
        config = PromptConfig(repo_map_mode="stable")
        assert config.repo_map_mode == "stable"

    def test_repo_map_mode_invalid_raises(self):
        with pytest.raises(ValidationError):
            PromptConfig(repo_map_mode="fuzzy")

    def test_repo_map_mode_round_trips_through_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The new field survives a kew.toml load round-trip."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text('[prompt]\nrepo_map_mode = "stable"\n')
        config = load_config()
        assert config.prompt.repo_map_mode == "stable"

    @pytest.mark.parametrize("field", ["include_lessons", "lessons_corpus_tokens"])
    def test_deleted_lessons_fields_are_rejected(self, field: str):
        with pytest.raises(ValidationError):
            PromptConfig(**{field: True})


class TestKewTomlLoading:
    """kew.toml is the only discovered config file name."""

    def test_load_kew_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kew.toml is loaded when present."""
        monkeypatch.chdir(tmp_path)
        kew_toml = tmp_path / "kew.toml"
        kew_toml.write_text('[project]\nname = "from-kew-toml"\n')
        config = load_config()
        assert config.project.name == "from-kew-toml"

    def test_lone_dispatch_toml_in_cwd_refuses_to_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W4.3 row 19: kew refuses rather than loading defaults."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dispatch.toml").write_text("[budget]\nper_issue_usd = 3.0\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config()
        assert exc_info.value.kind == "unread_config_file"

    def test_project_dir_dispatch_toml_refuses_to_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--project / KEW_PROJECT applies the same refusal."""
        from kew.config import resolved_config_path

        (tmp_path / "dispatch.toml").write_text('[project]\nname = "legacy"\n')
        monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
        assert resolved_config_path() is None
        with pytest.raises(ConfigError, match="kew does not read dispatch.toml"):
            load_config()

    def test_project_dir_kew_toml_beside_dispatch_toml_loads_kew_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "kew.toml").write_text('[project]\nname = "kew-wins"\n')
        (tmp_path / "dispatch.toml").write_text("[budget\n")
        monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
        assert load_config().project.name == "kew-wins"


class TestKewEnvVars:
    """KEW_* env vars override config values; DISPATCH_* names are not read."""

    def test_kew_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KEW_* env vars override config values."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text("")
        monkeypatch.setenv("KEW_TIMEOUT", "30")
        config = load_config()
        assert config.agent.timeout_minutes == 30

    @pytest.mark.parametrize(
        ("legacy", "value", "read"),
        [
            ("DISPATCH_TIMEOUT", "60", lambda c: c.agent.timeout_minutes),
            ("DISPATCH_REPO", "legacy/repo", lambda c: c.project.repo),
            ("DISPATCH_LOG_DIR", "/legacy/logs", lambda c: c.logging.dir),
            ("DISPATCH_MAX_BUDGET", "1.5", lambda c: c.agent.max_budget_usd),
            ("DISPATCH_MAX_WORKERS", "9", lambda c: c.parallel.max_workers),
            ("WORKTREE_BASE", "/legacy/checkouts", lambda c: c.branches.worktree_base),
        ],
    )
    def test_removed_env_names_are_not_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        legacy: str,
        value: str,
        read,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """W4.3 rows 18 and 20: the pre-rename env names change nothing, and
        kew says so, since a silent drop would lose a cap such as the budget."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text("")
        baseline = read(load_config())
        assert legacy not in capsys.readouterr().err
        monkeypatch.setenv(legacy, value)
        assert read(load_config()) == baseline
        err = capsys.readouterr().err
        assert f"{legacy} is no longer read and is ignored" in err
        assert f"set KEW_{legacy.removeprefix('DISPATCH_')} instead" in err

    def test_malformed_removed_env_name_is_not_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A junk value in a removed name cannot fail the load: it is never read."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text("")
        monkeypatch.setenv("DISPATCH_TIMEOUT", "bad")
        assert load_config().agent.timeout_minutes == 120

    def test_kew_worktree_base_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W4.3 row 20: KEW_WORKTREE_BASE replaces WORKTREE_BASE."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text("")
        target = tmp_path / "checkouts"
        monkeypatch.setenv("KEW_WORKTREE_BASE", str(target))
        assert load_config().branches.worktree_base == str(target)

    def test_default_log_dir_ignores_dispatch_log_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kew.config as kew_config

        monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", None)
        monkeypatch.delenv("KEW_LOG_DIR", raising=False)
        monkeypatch.setenv("DISPATCH_LOG_DIR", "/legacy/logs")
        assert kew_config._default_log_dir() == "./logs/dispatch"
        monkeypatch.setenv("KEW_LOG_DIR", "/kew/logs")
        assert kew_config._default_log_dir() == "/kew/logs"


class TestAlertingConfig:
    """AlertingConfig defaults, overrides, and TOML loading."""

    def test_defaults(self):
        config = AlertingConfig()
        assert config.webhook_url == ""
        assert config.notify_on_failure is True
        assert config.notify_on_success is False

    def test_override(self):
        config = AlertingConfig(
            webhook_url="https://hooks.example.com/alert",
            notify_on_failure=False,
            notify_on_success=True,
        )
        assert config.webhook_url == "https://hooks.example.com/alert"
        assert config.notify_on_failure is False
        assert config.notify_on_success is True

    def test_alerting_in_dispatch_config(self):
        config = DispatchConfig()
        assert isinstance(config.alerting, AlertingConfig)
        assert config.alerting.webhook_url == ""
        assert config.alerting.notify_on_failure is True

    def test_toml_loading(self, tmp_path: Path):
        toml_content = b"""
[alerting]
webhook_url = "https://hooks.example.com/alert"
notify_on_failure = false
notify_on_success = true
"""
        toml_file = tmp_path / "kew.toml"
        toml_file.write_bytes(toml_content)
        config = load_config(config_path=toml_file)
        assert config.alerting.webhook_url == "https://hooks.example.com/alert"
        assert config.alerting.notify_on_failure is False
        assert config.alerting.notify_on_success is True


class TestAgentConfigValidation:
    """AgentConfig field validators."""

    def test_timeout_minutes_1_is_valid(self):
        config = AgentConfig(timeout_minutes=1)
        assert config.timeout_minutes == 1

    def test_timeout_minutes_large_is_valid(self):
        config = AgentConfig(timeout_minutes=480)
        assert config.timeout_minutes == 480

    def test_timeout_minutes_0_raises(self):
        with pytest.raises(ValueError, match="timeout_minutes"):
            AgentConfig(timeout_minutes=0)

    def test_timeout_minutes_negative_raises(self):
        with pytest.raises(ValueError, match="timeout_minutes"):
            AgentConfig(timeout_minutes=-5)

    def test_max_budget_usd_positive_is_valid(self):
        config = AgentConfig(max_budget_usd=0.01)
        assert config.max_budget_usd == 0.01

    def test_max_budget_usd_0_raises(self):
        with pytest.raises(ValueError, match="max_budget_usd"):
            AgentConfig(max_budget_usd=0.0)

    def test_max_budget_usd_negative_raises(self):
        with pytest.raises(ValueError, match="max_budget_usd"):
            AgentConfig(max_budget_usd=-1.0)

    def test_max_budget_usd_nan_raises(self):
        with pytest.raises(ValueError, match="finite"):
            AgentConfig(max_budget_usd=float("nan"))

    def test_max_budget_usd_inf_raises(self):
        with pytest.raises(ValueError, match="finite"):
            AgentConfig(max_budget_usd=float("inf"))

    def test_budget_stop_grace_seconds_default(self):
        assert AgentConfig().budget_stop_grace_seconds == 30

    def test_budget_stop_grace_seconds_1_is_valid(self):
        assert AgentConfig(budget_stop_grace_seconds=1).budget_stop_grace_seconds == 1

    def test_budget_stop_grace_seconds_0_raises(self):
        with pytest.raises(ValueError, match="budget_stop_grace_seconds"):
            AgentConfig(budget_stop_grace_seconds=0)


class TestBudgetConfigValidation:
    """BudgetConfig field validators."""

    def test_per_issue_usd_zero_is_valid(self):
        config = BudgetConfig(per_issue_usd=0.0)
        assert config.per_issue_usd == 0.0

    def test_per_issue_usd_positive_is_valid(self):
        config = BudgetConfig(per_issue_usd=5.0)
        assert config.per_issue_usd == 5.0

    def test_per_issue_usd_negative_raises(self):
        with pytest.raises(ValueError, match="per_issue_usd"):
            BudgetConfig(per_issue_usd=-1.0)

    def test_daily_usd_zero_is_valid(self):
        config = BudgetConfig(daily_usd=0.0)
        assert config.daily_usd == 0.0

    def test_daily_usd_negative_raises(self):
        with pytest.raises(ValueError, match="daily_usd"):
            BudgetConfig(daily_usd=-10.0)

    def test_weekly_usd_zero_is_valid(self):
        config = BudgetConfig(weekly_usd=0.0)
        assert config.weekly_usd == 0.0

    def test_weekly_usd_negative_raises(self):
        with pytest.raises(ValueError, match="weekly_usd"):
            BudgetConfig(weekly_usd=-50.0)

    def test_per_issue_usd_nan_raises(self):
        with pytest.raises(ValueError, match="finite"):
            BudgetConfig(per_issue_usd=float("nan"))

    def test_daily_usd_inf_raises(self):
        with pytest.raises(ValueError, match="finite"):
            BudgetConfig(daily_usd=float("inf"))

    def test_weekly_usd_neg_inf_raises(self):
        with pytest.raises(ValueError, match="finite"):
            BudgetConfig(weekly_usd=float("-inf"))


class TestClaudeCodeConfig:
    def test_defaults(self):
        c = ClaudeCodeConfig()
        assert c.skip_permissions is True
        assert c.autocompact_pct == 70


class TestAgentConfigRunnerField:
    def test_runner_defaults_empty(self):
        config = DispatchConfig()
        assert config.agent.runner == ""

    def test_runner_from_toml(self, tmp_path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            '[agent]\nrunner = "opencode"\nmodel = "ollama/deepseek"\n'
        )
        config = load_config(toml_file)
        assert config.agent.runner == "opencode"
        assert config.agent.model == "ollama/deepseek"

    def test_claude_code_nested_config(self, tmp_path):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            "[agent]\n"
            'model = "claude-sonnet-4"\n'
            "[agent.claude_code]\n"
            "skip_permissions = false\n"
            "autocompact_pct = 50\n"
        )
        config = load_config(toml_file)
        assert config.agent.claude_code.skip_permissions is False
        assert config.agent.claude_code.autocompact_pct == 50

    def test_kew_runner_env_override(self, tmp_path, monkeypatch):
        toml_file = tmp_path / "kew.toml"
        toml_file.write_text("")
        monkeypatch.setenv("KEW_RUNNER", "opencode")
        config = load_config(toml_file)
        assert config.agent.runner == "opencode"


class TestRouteEntry:
    def test_full_construction(self):
        from kew.config import RouteEntry

        entry = RouteEntry(runner="claude-code", model="claude-opus-4")
        assert entry.runner == "claude-code"
        assert entry.model == "claude-opus-4"

    def test_defaults(self):
        from kew.config import RouteEntry

        entry = RouteEntry()
        assert entry.runner == ""
        assert entry.model == ""

    def test_runner_only_valid_in_routing_config(self):
        from kew.config import RouteEntry, RoutingConfig

        rc = RoutingConfig(default=RouteEntry(runner="claude-code"))
        assert rc.default.runner == "claude-code"
        assert rc.default.model == ""

    def test_model_only_valid_in_routing_config(self):
        from kew.config import RouteEntry, RoutingConfig

        rc = RoutingConfig(default=RouteEntry(model="claude-opus-4"))
        assert rc.default.runner == ""
        assert rc.default.model == "claude-opus-4"


class TestRoutingConfig:
    def test_full_dict_entries(self):
        from kew.config import RouteEntry, RoutingConfig

        rc = RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
            simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
            complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
        )
        assert rc.default.runner == "claude-code"
        assert rc.simple.runner == "opencode"
        assert rc.complex.runner == "claude-code"
        assert rc.medium is None

    def test_string_shorthand(self):
        from kew.config import RoutingConfig

        rc = RoutingConfig(default="claude-code", complex="opencode")
        assert rc.default == "claude-code"
        assert rc.complex == "opencode"

    def test_missing_routing_on_dispatch_config(self):
        """When [routing] is absent, routing is None."""
        from kew.config import DispatchConfig

        config = DispatchConfig()
        assert config.routing is None


class TestRouteEntryValidation:
    def test_empty_runner_and_model_raises(self):
        """RouteEntry with both runner and model empty is invalid."""
        from pydantic import ValidationError

        from kew.config import RouteEntry, RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(default=RouteEntry(runner="", model=""))

    def test_empty_string_shorthand_raises(self):
        """Empty string shorthand is invalid."""
        from pydantic import ValidationError

        from kew.config import RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(default="")

    def test_whitespace_only_string_shorthand_raises(self):
        """Whitespace-only string shorthand is invalid."""
        from pydantic import ValidationError

        from kew.config import RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(default="   ")

    def test_empty_string_shorthand_on_optional_level_raises(self):
        """Empty string on an optional complexity level is also invalid."""
        from pydantic import ValidationError

        from kew.config import RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(default="claude-code", simple="")


class TestResolveConfig:
    def test_defaults(self):
        from kew.config import ResolveConfig

        rc = ResolveConfig()
        assert rc.strategy == "merge"
        assert rc.auto is False

    def test_dispatch_config_has_resolve(self):
        from kew.config import DispatchConfig

        config = DispatchConfig()
        assert config.resolve.strategy == "merge"
        assert config.resolve.auto is False

    def test_strategy_rebase(self):
        from kew.config import ResolveConfig

        rc = ResolveConfig(strategy="rebase")
        assert rc.strategy == "rebase"

    def test_invalid_strategy_raises(self):
        import pytest

        from kew.config import ResolveConfig

        with pytest.raises(ValueError, match="strategy must be one of"):
            ResolveConfig(strategy="invalid_strategy")


class TestRoutingConfigFromToml:
    def test_parse_full_routing(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text("""\
[routing]
default = { runner = "claude-code", model = "claude-sonnet-4" }
simple = { runner = "opencode", model = "ollama/deepseek-coder" }
complex = { runner = "claude-code", model = "claude-opus-4" }
""")
        config = load_config(toml_file)
        assert config.routing is not None
        assert config.routing.default.runner == "claude-code"
        assert config.routing.simple.runner == "opencode"
        assert config.routing.complex.model == "claude-opus-4"
        assert config.routing.medium is None

    def test_parse_mixed_shorthand(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text("""\
[routing]
default = "claude-code"
complex = { runner = "claude-code", model = "claude-opus-4" }
""")
        config = load_config(toml_file)
        assert config.routing is not None
        assert config.routing.default == "claude-code"
        assert config.routing.complex.runner == "claude-code"

    def test_no_routing_section(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text("[agent]\ntimeout_minutes = 60\n")
        config = load_config(toml_file)
        assert config.routing is None


class TestOverlapConfigDefaults:
    """OverlapConfig and OverlapAdvancedConfig defaults and validation."""

    def test_overlap_strategy_default(self):
        config = DispatchConfig()
        assert config.overlap.strategy == "serialize"

    def test_overlap_advanced_merge_context_ttl_minutes_default(self):
        config = DispatchConfig()
        assert config.overlap.advanced.merge_context_ttl_minutes == 60

    def test_invalid_strategy_raises(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            OverlapConfig(strategy="invalid")

    def test_strategy_serialize_accepted(self):
        config = OverlapConfig(strategy="serialize")
        assert config.strategy == "serialize"

    def test_strategy_warn_accepted(self):
        config = OverlapConfig(strategy="warn")
        assert config.strategy == "warn"

    def test_strategy_off_accepted(self):
        config = OverlapConfig(strategy="off")
        assert config.strategy == "off"

    def test_labels_overlap_hold_default(self):
        config = DispatchConfig()
        assert config.labels.overlap_hold == "overlap-hold"

    def test_overlap_toml_loading(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            '[project]\nrepo = "test/repo"\n'
            "[overlap]\n"
            'strategy = "warn"\n'
            "[overlap.advanced]\n"
            "merge_context_ttl_minutes = 15\n"
        )
        config = load_config(config_path=toml_file)
        assert config.overlap.strategy == "warn"
        assert config.overlap.advanced.merge_context_ttl_minutes == 15

    def test_overlap_toml_defaults(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text('[project]\nrepo = "test/repo"\n')
        config = load_config(config_path=toml_file)
        assert config.overlap.strategy == "serialize"
        assert config.overlap.advanced.merge_context_ttl_minutes == 60
        assert config.labels.overlap_hold == "overlap-hold"

    def test_overlap_toml_invalid_strategy(self, tmp_path):
        from kew.config import load_config

        toml_file = tmp_path / "kew.toml"
        toml_file.write_text(
            '[project]\nrepo = "test/repo"\n[overlap]\nstrategy = "invalid"\n'
        )
        with pytest.raises(ConfigError):
            load_config(config_path=toml_file)


class TestOpenCodeConfig:
    def test_defaults(self):
        from kew.config import OpenCodeConfig

        cfg = OpenCodeConfig()
        assert cfg.extra_args == []
        assert cfg.agent == ""

    def test_parses_from_toml(self, tmp_path):
        import tomllib

        from kew.config import DispatchConfig

        toml_path = tmp_path / "kew.toml"
        toml_path.write_text(
            """
[agent.opencode]
extra_args = ["--share"]
agent = "build"
"""
        )
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        cfg = DispatchConfig.model_validate(data)
        assert cfg.agent.opencode.extra_args == ["--share"]
        assert cfg.agent.opencode.agent == "build"

    def test_default_when_section_absent(self):
        from kew.config import DispatchConfig

        cfg = DispatchConfig.model_validate({})
        assert cfg.agent.opencode.extra_args == []
        assert cfg.agent.opencode.agent == ""


class TestDelegationConfig:
    """DelegationConfig defaults and interval validators."""

    def test_default_assignee(self):
        cfg = DelegationConfig()
        assert cfg.assignee == ""

    def test_default_label(self):
        cfg = DelegationConfig()
        assert cfg.label == "kew:ready"

    def test_default_poll_interval_seconds(self):
        cfg = DelegationConfig()
        assert cfg.poll_interval_seconds == 20

    def test_default_fast_interval_seconds(self):
        cfg = DelegationConfig()
        assert cfg.fast_interval_seconds == 2

    def test_default_refusal_cooldown_seconds(self):
        cfg = DelegationConfig()
        assert cfg.refusal_cooldown_seconds == 300

    def test_poll_interval_s_zero_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            DelegationConfig(poll_interval_seconds=0)

    def test_poll_interval_s_negative_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            DelegationConfig(poll_interval_seconds=-1)

    def test_fast_interval_s_zero_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            DelegationConfig(fast_interval_seconds=0)

    def test_fast_interval_s_negative_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            DelegationConfig(fast_interval_seconds=-5)


class TestCommandsVerifyLoop:
    def test_commands_verify_loop_defaults_empty(self):
        from kew.config import CommandsConfig

        assert CommandsConfig().verify_loop == ""

    def test_commands_verify_loop_loads_from_toml(self, tmp_path):
        from kew.config import load_config

        cfg_path = tmp_path / "kew.toml"
        cfg_path.write_text('[commands]\nverify_loop = "pytest -q"\n')
        config = load_config(cfg_path)
        assert config.commands.verify_loop == "pytest -q"


class TestCommandsTestTargeted:
    """#2115: the governed remediation brief needs a runnable targeted-test
    command, and a value that would run the whole suite must be refused."""

    def test_defaults_empty(self):
        assert CommandsConfig().test_targeted == ""

    def test_accepts_a_paths_placeholder(self):
        cmd = "uv run pytest {paths}"
        assert CommandsConfig(test_targeted=cmd).test_targeted == cmd

    def test_rejects_a_command_without_the_paths_placeholder(self):
        with pytest.raises(ValueError, match=r"\{paths\}"):
            CommandsConfig(test_targeted="uv run pytest")

    def test_loads_from_toml(self, tmp_path):
        from kew.config import load_config

        cfg_path = tmp_path / "kew.toml"
        cfg_path.write_text('[commands]\ntest_targeted = "pytest {paths}"\n')
        config = load_config(cfg_path)
        assert config.commands.test_targeted == "pytest {paths}"


class TestCommandsTestPolicyMark:
    """#2140/#2174: targeted selection picks tests by the changed files, so a
    repo-wide policy gate that the change does not import never runs. The gates
    carry this mark and join the targeted set unconditionally — a hand list of
    paths drifted and let the id sweep through."""

    def test_defaults_empty(self):
        assert CommandsConfig().test_policy_mark == ""

    def test_accepts_a_mark_name(self):
        assert CommandsConfig(test_policy_mark="policy").test_policy_mark == "policy"

    def test_strips_surrounding_whitespace(self):
        assert CommandsConfig(test_policy_mark=" policy ").test_policy_mark == "policy"

    @pytest.mark.parametrize(
        "mark",
        [
            "policy or serial",
            "not policy",
            "policy; rm -rf /",
            "-m policy",
            "policy gate",
            "2policy",
        ],
    )
    def test_rejects_anything_but_a_bare_marker_name(self, mark):
        with pytest.raises(ValueError, match="bare pytest marker name"):
            CommandsConfig(test_policy_mark=mark)

    def test_loads_from_toml(self, tmp_path):
        from kew.config import load_config

        cfg_path = tmp_path / "kew.toml"
        cfg_path.write_text('[commands]\ntest_policy_mark = "policy"\n')
        config = load_config(cfg_path)
        assert config.commands.test_policy_mark == "policy"

    def test_the_replaced_path_list_names_its_successor(self, tmp_path):
        """#2174 renamed the key; an unknown key otherwise stalls kew with a
        fix line that does not say what to write instead."""
        from kew.config import ConfigError, load_config

        cfg_path = tmp_path / "kew.toml"
        cfg_path.write_text('[commands]\ntest_policy_paths = ["tests/test_a.py"]\n')
        with pytest.raises(ConfigError) as exc_info:
            load_config(cfg_path)
        assert exc_info.value.kind == "unknown_key"
        assert "commands.test_policy_mark" in exc_info.value.fix


def test_verify_loop_mode_default_is_command():
    assert CommandsConfig().verify_loop_mode == "command"


def test_verify_loop_mode_accepts_ci_green():
    assert CommandsConfig(verify_loop_mode="ci_green").verify_loop_mode == "ci_green"


def test_verify_loop_mode_rejects_unknown():
    with pytest.raises(ValueError):
        CommandsConfig(verify_loop_mode="banana")


def test_ci_poll_timeout_default():
    assert CommandsConfig().ci_poll_timeout_seconds == 1800


def test_ci_poll_timeout_rejects_non_positive():
    with pytest.raises(ValueError):
        CommandsConfig(ci_poll_timeout_seconds=0)


class TestApprovalConfig:
    """[approval] section: threshold gate config. None threshold = feature off."""

    def test_defaults_feature_off(self):
        from kew.config import ApprovalConfig

        cfg = ApprovalConfig()
        assert cfg.threshold_usd is None
        assert cfg.label == "needs-approval"

    def test_default_when_section_absent(self):
        from kew.config import DispatchConfig

        cfg = DispatchConfig.model_validate({})
        assert cfg.approval.threshold_usd is None
        assert cfg.approval.label == "needs-approval"

    def test_parses_from_toml(self, tmp_path):
        import tomllib

        from kew.config import DispatchConfig

        toml_path = tmp_path / "kew.toml"
        toml_path.write_text(
            """
[approval]
threshold_usd = 5.0
label = "hold-for-human"
"""
        )
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        cfg = DispatchConfig.model_validate(data)
        assert cfg.approval.threshold_usd == 5.0
        assert cfg.approval.label == "hold-for-human"

    def test_rejects_negative_threshold(self):
        import pytest
        from pydantic import ValidationError

        from kew.config import ApprovalConfig

        with pytest.raises(ValidationError):
            ApprovalConfig(threshold_usd=-1.0)

    def test_rejects_non_finite_threshold(self):
        import pytest
        from pydantic import ValidationError

        from kew.config import ApprovalConfig

        with pytest.raises(ValidationError):
            ApprovalConfig(threshold_usd=float("nan"))

    def test_rejects_inf_threshold(self):
        import pytest
        from pydantic import ValidationError

        from kew.config import ApprovalConfig

        with pytest.raises(ValidationError):
            ApprovalConfig(threshold_usd=float("inf"))

    def test_rejects_neg_inf_threshold(self):
        import pytest
        from pydantic import ValidationError

        from kew.config import ApprovalConfig

        with pytest.raises(ValidationError):
            ApprovalConfig(threshold_usd=float("-inf"))


def test_breaker_config_defaults():
    from kew.config import BreakerConfig, DispatchConfig

    b = BreakerConfig()
    assert b.enabled is True
    assert b.cost_anomaly_factor == 3.0
    assert b.min_samples == 5
    assert b.min_anomaly_threshold_usd == 0.0
    assert b.compaction_allowance_factor == 0.5
    assert DispatchConfig().breaker.enabled is True


def test_breaker_config_rejects_bad_values():
    import pytest
    from pydantic import ValidationError

    from kew.config import BreakerConfig

    with pytest.raises(ValidationError):
        BreakerConfig(cost_anomaly_factor=1.0)  # must be > 1.0
    with pytest.raises(ValidationError):
        BreakerConfig(min_samples=0)  # must be >= 1
    with pytest.raises(ValidationError):
        BreakerConfig(min_anomaly_threshold_usd=-1.0)  # must be >= 0
    with pytest.raises(ValidationError):
        BreakerConfig(compaction_allowance_factor=-0.1)  # must be >= 0
    # A zero floor is valid (the no-op default).
    assert BreakerConfig(min_anomaly_threshold_usd=0.0).min_anomaly_threshold_usd == 0.0
    # A zero allowance disables the compaction discount.
    assert (
        BreakerConfig(compaction_allowance_factor=0.0).compaction_allowance_factor
        == 0.0
    )


def test_approval_approvers_defaults_empty():
    assert ApprovalConfig().approvers == []
    assert ApprovalConfig(approvers=["alice", "bob"]).approvers == ["alice", "bob"]


def test_alerting_notify_on_hold_defaults_true():
    assert AlertingConfig().notify_on_hold is True
    assert AlertingConfig(notify_on_hold=False).notify_on_hold is False


def test_resolved_config_path_finds_upward(tmp_path, monkeypatch):
    from kew.config import resolved_config_path

    (tmp_path / ".git").mkdir()
    cfg = tmp_path / "kew.toml"
    cfg.write_text("")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert resolved_config_path() == cfg


def test_resolved_config_path_ignores_dispatch_toml(tmp_path, monkeypatch):
    # W4.3 row 19: the pre-rename dispatch.toml name is not discovered.
    from kew.config import resolved_config_path

    (tmp_path / ".git").mkdir()
    (tmp_path / "dispatch.toml").write_text("")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert resolved_config_path() is None


def test_resolved_config_path_none_when_absent(tmp_path, monkeypatch):
    from kew.config import resolved_config_path

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolved_config_path() is None


def test_resolved_config_path_explicit(tmp_path):
    from kew.config import resolved_config_path

    p = tmp_path / "x.toml"
    p.write_text("")
    assert resolved_config_path(p) == p
    assert resolved_config_path(tmp_path / "missing.toml") is None


def test_ingest_config_defaults():
    from kew.config import DispatchConfig

    cfg = DispatchConfig()
    assert cfg.ingest.enabled is False
    assert cfg.ingest.host == "127.0.0.1"
    assert cfg.ingest.port == 4318
    assert cfg.ingest.cap_usd == 0.0
    assert cfg.ingest.combine_with_dispatch is False


def test_ingest_config_accepts_loopback_hosts():
    from kew.config import IngestConfig

    for host in ("127.0.0.1", "127.5.6.7", "localhost", "::1"):
        assert IngestConfig(host=host).host == host


def test_ingest_config_rejects_non_loopback_host():
    import pytest
    from pydantic import ValidationError

    from kew.config import IngestConfig

    with pytest.raises(ValidationError):
        IngestConfig(host="0.0.0.0")
    with pytest.raises(ValidationError):
        IngestConfig(host="192.168.1.10")


def test_ingest_config_rejects_out_of_range_port():
    import pytest
    from pydantic import ValidationError

    from kew.config import IngestConfig

    with pytest.raises(ValidationError):
        IngestConfig(port=0)
    with pytest.raises(ValidationError):
        IngestConfig(port=70000)


def test_ingest_config_rejects_negative_cap_usd():
    import pytest
    from pydantic import ValidationError

    from kew.config import IngestConfig

    assert IngestConfig(cap_usd=0.0).cap_usd == 0.0  # 0 = no cap alert, allowed
    with pytest.raises(ValidationError):
        IngestConfig(cap_usd=-1.0)


# ---------------------------------------------------------------------------
# FlightRecorderConfig — expose + loopback validator (#252)
# ---------------------------------------------------------------------------


def test_flight_recorder_loopback_default_ok():
    from kew.config import FlightRecorderConfig

    fr = FlightRecorderConfig()
    assert fr.host == "127.0.0.1"
    assert fr.expose is False


def test_flight_recorder_nonloopback_without_expose_rejected():
    import pytest
    from pydantic import ValidationError

    from kew.config import FlightRecorderConfig

    with pytest.raises(ValidationError, match="expose"):
        FlightRecorderConfig(host="0.0.0.0")


def test_flight_recorder_nonloopback_with_expose_ok():
    from kew.config import FlightRecorderConfig

    fr = FlightRecorderConfig(host="0.0.0.0", expose=True)
    assert fr.host == "0.0.0.0"


def test_flight_recorder_localhost_and_ipv6_loopback_ok():
    from kew.config import FlightRecorderConfig

    assert FlightRecorderConfig(host="localhost").host == "localhost"
    assert FlightRecorderConfig(host="::1").host == "::1"


def test_flight_recorder_extra_origins_default_empty():
    from kew.config import FlightRecorderConfig

    assert FlightRecorderConfig().extra_origins == []


def test_flight_recorder_extra_origins_accepts_bare_origin():
    from kew.config import FlightRecorderConfig

    fr = FlightRecorderConfig(extra_origins=["https://kew.example.com"])
    assert fr.extra_origins == ["https://kew.example.com"]


def test_flight_recorder_extra_origins_rejects_path_and_bad_scheme():
    import pytest
    from pydantic import ValidationError

    from kew.config import FlightRecorderConfig

    bad_origins = (
        "https://kew.example.com/inbox",  # has a path
        "ftp://kew.example.com",  # bad scheme
        "kew.example.com",  # no scheme
    )
    for bad in bad_origins:
        with pytest.raises(ValidationError, match="extra_origins"):
            FlightRecorderConfig(extra_origins=[bad])


# ── config overrides (web config-manager #368) ──────────────────────


class TestConfigOverridePrecedence:
    """load_config(store=…): DB beats toml; env beats DB; no-store = file unchanged."""

    def test_db_override_beats_toml(self, tmp_path: Path):
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\nper_issue_usd = 15.0\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="budget.per_issue_usd",
                value_json="25.0",
                file_value_json="15.0",
                set_at="2026-06-29T00:00:00+00:00",
                set_by="test",
            )
            cfg = load_config(config_path=toml, store=store)
        assert cfg.budget.per_issue_usd == 25.0

    def test_env_beats_db_override(self, tmp_path: Path):
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[agent]\nmax_budget_usd = 10.0\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="agent.max_budget_usd",
                value_json="20.0",
                file_value_json="10.0",
                set_at="2026-06-29T00:00:00+00:00",
                set_by="test",
            )
            with patch.dict("os.environ", {"KEW_MAX_BUDGET": "30.0"}):
                cfg = load_config(config_path=toml, store=store)
        assert cfg.agent.max_budget_usd == 30.0

    def test_no_store_returns_file_unchanged(self, tmp_path: Path):
        from kew.config import load_config

        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\nper_issue_usd = 7.5\n")
        cfg = load_config(config_path=toml)
        assert cfg.budget.per_issue_usd == 7.5

    def test_multiple_overrides_applied(self, tmp_path: Path):
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="budget.per_issue_usd",
                value_json="1.0",
                file_value_json="15.0",
                set_at="2026-06-29T00:00:00+00:00",
                set_by="test",
            )
            store.set_config_override(
                field="budget.daily_usd",
                value_json="2.0",
                file_value_json="50.0",
                set_at="2026-06-29T00:00:00+00:00",
                set_by="test",
            )
            cfg = load_config(config_path=toml, store=store)
        assert cfg.budget.per_issue_usd == 1.0
        assert cfg.budget.daily_usd == 2.0


class TestConfigOverrideAllowlist:
    """Off-allowlist fields ignored on read, rejected on write."""

    def test_off_allowlist_field_ignored_on_read(self, tmp_path: Path):
        """A DB row with an off-allowlist field is silently skipped."""
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[agent]\ntimeout_minutes = 60\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store._conn.execute(
                "INSERT INTO config_overrides"
                " (field, value_json, set_at, set_by)"
                " VALUES (?, ?, ?, ?)",
                ("agent.timeout_minutes", "999", "2026-06-29T00:00:00+00:00", "test"),
            )
            store._conn.commit()
            cfg = load_config(config_path=toml, store=store)
        assert cfg.agent.timeout_minutes == 60

    def test_off_allowlist_field_rejected_on_validate(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="not overridable"):
            validate_override("agent.timeout_minutes", 999)

    def test_valid_override_passes_validation(self):
        from kew.config import validate_override

        validate_override("budget.per_issue_usd", 25.0)

    def test_invalid_value_rejected_on_validate(self):
        from kew.config import validate_override

        with pytest.raises(ValueError):
            validate_override("budget.per_issue_usd", -1.0)

    def test_nan_rejected_on_validate(self):
        from kew.config import validate_override

        with pytest.raises(ValueError):
            validate_override("budget.daily_usd", float("nan"))

    def test_malformed_override_skipped_with_warning(self, tmp_path: Path):
        """An invalid allowlisted value is skipped; load_config still succeeds."""
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\nper_issue_usd = 7.5\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store._conn.execute(
                "INSERT INTO config_overrides"
                " (field, value_json, set_at, set_by)"
                " VALUES (?, ?, ?, ?)",
                ("budget.per_issue_usd", "-1.0", "2026-06-29T00:00:00+00:00", "test"),
            )
            store._conn.commit()
            cfg = load_config(config_path=toml, store=store)
        assert cfg.budget.per_issue_usd == 7.5

    def test_nan_override_skipped(self, tmp_path: Path):
        """NaN in a DB override row is skipped (json.loads('NaN') succeeds)."""
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\ndaily_usd = 50.0\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store._conn.execute(
                "INSERT INTO config_overrides"
                " (field, value_json, set_at, set_by)"
                " VALUES (?, ?, ?, ?)",
                ("budget.daily_usd", "NaN", "2026-06-29T00:00:00+00:00", "test"),
            )
            store._conn.commit()
            cfg = load_config(config_path=toml, store=store)
        assert cfg.budget.daily_usd == 50.0

    def test_overridable_fields_include_routing_and_model(self):
        from kew.config import OVERRIDABLE_FIELDS

        expected = frozenset(
            {
                "budget.per_issue_usd",
                "budget.daily_usd",
                "budget.weekly_usd",
                "agent.max_budget_usd",
                "agent.model",
                "review.model",
                "routing.default",
                "routing.simple",
                "routing.medium",
                "routing.complex",
                "breaker.cost_anomaly_factor",
            }
        )
        assert expected == OVERRIDABLE_FIELDS


class TestBreakerOverrideValidation:
    """validate_override widened to steer breaker.cost_anomaly_factor (#712)."""

    def test_cost_anomaly_factor_accepted(self):
        from kew.config import validate_override

        validate_override("breaker.cost_anomaly_factor", 5.0)

    def test_cost_anomaly_factor_below_one_rejected(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="cost_anomaly_factor"):
            validate_override("breaker.cost_anomaly_factor", 0.5)

    def test_cost_anomaly_factor_at_one_rejected(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="cost_anomaly_factor"):
            validate_override("breaker.cost_anomaly_factor", 1.0)

    def test_cost_anomaly_factor_round_trips_through_db(self, tmp_path: Path):
        """A valid breaker override in the DB beats the committed kew.toml."""
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text("[breaker]\ncost_anomaly_factor = 3.0\n")
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store._conn.execute(
                "INSERT INTO config_overrides"
                " (field, value_json, set_at, set_by)"
                " VALUES (?, ?, ?, ?)",
                (
                    "breaker.cost_anomaly_factor",
                    "6.0",
                    "2026-07-17T00:00:00+00:00",
                    "test",
                ),
            )
            store._conn.commit()
            cfg = load_config(config_path=toml, store=store)
        assert cfg.breaker.cost_anomaly_factor == 6.0


class TestRoutingModelOverrideValidation:
    """validate_override widened to steer agent.model + routing.* (#659)."""

    def test_agent_model_accepted(self):
        from kew.config import validate_override

        validate_override("agent.model", "claude-opus-4-8")

    def test_agent_model_empty_rejected(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="non-empty"):
            validate_override("agent.model", "")

    def test_routing_entry_dict_accepted(self):
        from kew.config import validate_override

        validate_override(
            "routing.medium", {"runner": "claude-code", "model": "claude-opus-4-8"}
        )

    def test_routing_default_dict_accepted(self):
        from kew.config import validate_override

        validate_override("routing.default", {"runner": "claude-code"})

    def test_routing_bare_string_rejected_with_runner_hint(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="runner-name"):
            validate_override("routing.medium", "claude-code")

    def test_new_routing_field_off_allowlist_sibling_still_rejected(self):
        from kew.config import validate_override

        with pytest.raises(ValueError, match="not overridable"):
            validate_override("routing.cache_aware", True)


class TestRoutingModelOverrideResolution:
    """resolve_runner(load_config(store=…)) honors DB overrides for the route."""

    def test_agent_model_override_beats_file(self, tmp_path: Path):
        from kew.config import load_config
        from kew.run_store import SqliteRunStore
        from kew.runners.registry import resolve_runner

        toml = tmp_path / "kew.toml"
        toml.write_text('[agent]\nmodel = "claude-sonnet-4"\n')
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="agent.model",
                value_json='"claude-opus-4-8"',
                file_value_json='"claude-sonnet-4"',
                set_at="2026-07-16T00:00:00+00:00",
                set_by="tom",
            )
            cfg = load_config(config_path=toml, store=store)
        resolved = resolve_runner(cfg)
        assert resolved.model == "claude-opus-4-8"

    def test_routing_entry_override_beats_file(self, tmp_path: Path):
        from kew.config import load_config
        from kew.run_store import SqliteRunStore
        from kew.runners.registry import resolve_runner

        toml = tmp_path / "kew.toml"
        toml.write_text(
            "[routing]\n"
            'default = { runner = "claude-code", model = "claude-sonnet-4" }\n'
            'medium = { runner = "claude-code", model = "claude-sonnet-4" }\n'
        )
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="routing.medium",
                value_json='{"runner": "claude-code", "model": "claude-opus-4-8"}',
                file_value_json=(
                    '{"runner": "claude-code", "model": "claude-sonnet-4"}'
                ),
                set_at="2026-07-16T00:00:00+00:00",
                set_by="tom",
            )
            cfg = load_config(config_path=toml, store=store)
        resolved = resolve_runner(cfg, complexity="medium")
        assert resolved.model == "claude-opus-4-8"
        assert resolved.runner.name == "claude-code"

    def test_env_beats_override_beats_file_for_route(self, tmp_path: Path):
        """The generic override layer keeps env > override > file for the route.

        agent.model has no env var, so the chain is proven through the shared
        precedence machinery: override beats file above, and env beats the
        override here via KEW_RUNNER steering the no-routing resolution path.
        """
        from kew.config import load_config
        from kew.run_store import SqliteRunStore
        from kew.runners.registry import resolve_runner

        toml = tmp_path / "kew.toml"
        toml.write_text('[agent]\nmodel = "claude-sonnet-4"\n')
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="agent.model",
                value_json='"claude-opus-4-8"',
                file_value_json='"claude-sonnet-4"',
                set_at="2026-07-16T00:00:00+00:00",
                set_by="tom",
            )
            # No env for agent.model: override wins over file.
            cfg = load_config(config_path=toml, store=store)
            assert cfg.agent.model == "claude-opus-4-8"
            # Env layer applies on top: KEW_RUNNER forces the runner regardless.
            with patch.dict("os.environ", {"KEW_RUNNER": "opencode"}):
                cfg_env = load_config(config_path=toml, store=store)
        assert cfg_env.agent.runner == "opencode"
        assert resolve_runner(cfg_env).runner.name == "opencode"


class TestEpicConfig:
    """[epic] section: Epic DAG driver settings."""

    def test_epic_config_defaults(self):
        cfg = DispatchConfig()
        assert cfg.epic.stall_days == 3
        assert cfg.epic.max_parallel is None
        assert cfg.epic.approvers == []
        assert cfg.epic.lock_path is None
        assert cfg.epic.auto_ci_fix is False  # opt-in recovery (F5)
        assert cfg.epic.merge_queue is False  # opt-in serial merge queue (#2148)

    def test_epic_config_rejects_nonpositive_stall_days(self):
        with pytest.raises(ValueError):
            EpicConfig(stall_days=0)

    def test_merge_queue_without_the_review_stage_is_refused_at_load(self):
        """#2148: the review stage is the ONLY thing that remediates the
        NEEDS_CHANGES round a red combined tree records. Without it the queue
        repeats the same refusal every tick, nothing ever fixes the tree, and the
        node reads as awaiting a merge that can never happen — so the combination
        must fail loudly at load instead of wedging a node at merge time."""
        with pytest.raises(ValidationError, match="requires \\[review\\] adversarial"):
            DispatchConfig(
                epic=EpicConfig(merge_queue=True),
                review=ReviewConfig(adversarial=False),
            )

    def test_merge_queue_with_the_review_stage_loads(self):
        cfg = DispatchConfig(
            epic=EpicConfig(merge_queue=True),
            review=ReviewConfig(adversarial=True),
            commands=CommandsConfig(verify_loop="bash scripts/verify.sh"),
        )
        assert cfg.epic.merge_queue is True

    def test_merge_queue_without_verify_loop_is_refused_at_load(self):
        """#2184: the queue no longer falls back to `commands.ci`. On this repo
        `ci` is pytest alone, so the fallback merged combined trees nothing had
        linted, typed or scanned while the node's evidence said the queue had
        verified them — a gate whose coverage depends on which key happens to be
        set. The command the queue runs has to be chosen for the queue."""
        with pytest.raises(ValidationError, match=r"requires \[commands\] verify_loop"):
            DispatchConfig(
                epic=EpicConfig(merge_queue=True),
                review=ReviewConfig(adversarial=True),
                commands=CommandsConfig(ci="uv run pytest"),
            )

    def test_merge_gate_path_defaults_name_the_gate_definitions(self):
        """The default list is shipped, not left to the operator to discover:
        an empty one would mean a PR may rewrite the gate that proves it."""
        paths = EpicConfig().merge_gate_paths
        assert "**/scripts/" in paths
        assert "**/pyproject.toml" in paths
        assert "**/conftest.py" in paths
        assert ".github/workflows/" in paths
        # Every file entry is `**/`-anchored (#2192): a gate definition lives
        # wherever the project it gates lives, and the root-anchored spellings
        # matched none of this repo's own (`kew/.semgrep/`, `kew/uv.lock`).
        assert {"**/uv.lock", "**/.semgrep/", "**/.xenon-excludes"} <= set(paths)
        assert not {".semgrep/", ".xenon-excludes", "uv.lock"} & set(paths)
        # Every gate's own configuration, not only the Python ones (#2192): the
        # secrets gate IS its allowlist and the docs gate IS its exclude list.
        assert {
            "**/.gitleaks.toml",
            "**/.gitleaksignore",
            "**/mkdocs.y*ml",
            "**/.dockerignore",
            "**/tsconfig*.json",
            "**/*.config.*",
        } <= set(paths)
        # Configs a tool reads BY CONVENTION and that need not exist yet
        # (#2192): each outranks the `pyproject.toml` the same tool would
        # otherwise read, so a PR that ADDS one decides what proves it.
        assert {
            "**/.ruff.toml",
            "**/ruff.toml",
            "**/pytest.ini",
            "**/tox.ini",
            "**/setup.cfg",
            "**/.python-version",
            "**/.npmrc",
        } <= set(paths)
        # The scanner's OTHER conventional file (#2192): an ignore list does
        # not choose the rules, it chooses the targets, and it is obeyed
        # whatever ruleset `--config` pinned. `**/.semgrep/` covered the pin
        # and read as if the SAST gate were handled while a one-line
        # `.semgrepignore` scanned nothing and was attested `result: pass`.
        assert {
            "**/.semgrepignore",
            "**/.trivyignore",
            "**/trivy.y*ml",
        } <= set(paths)
        assert EpicConfig().merge_queue_uncontained_gates == []

    @pytest.mark.parametrize(
        "kwargs",
        [{"merge_gate_paths": ["  "]}, {"merge_queue_uncontained_gates": [""]}],
    )
    def test_blank_epic_list_entries_are_refused(self, kwargs):
        """A blank pattern matches nothing and a blank gate name records
        nothing: both are typos that would silently weaken the queue."""
        with pytest.raises(ValidationError, match="must not be blank"):
            EpicConfig(**kwargs)


class TestSandboxLowerOnlyCaps:
    """[sandbox] max_memory / max_cpus may only lower a cap (#2360).

    The container caps are not knobs and these do not make them knobs: every
    value that would raise a cap, remove it, or spell it "unlimited" is a load
    error. Rejected at load rather than clamped, because a clamped value runs
    a container under a cap the operator never chose and hides the typo that
    produced it.
    """

    def test_both_keys_are_unset_by_default(self):
        cfg = DispatchConfig()
        assert cfg.sandbox.max_memory is None
        assert cfg.sandbox.max_cpus is None

    @pytest.mark.parametrize("value", ["1g", "3g", "1536m", "8g", " 3g "])
    def test_a_value_at_or_below_the_cap_loads(self, tmp_path: Path, value: str):
        path = tmp_path / "kew.toml"
        path.write_text(f'[sandbox]\nmax_memory = "{value}"\n')
        assert load_config(path).sandbox.max_memory == value.strip()

    @pytest.mark.parametrize(
        "value",
        ["16g", "9g", "0", "0g", "512m", "", "unlimited", "none", "-1", "8gb", "8G B"],
        ids=[
            "double",
            "just-over",
            "zero",
            "zero-g",
            "under-floor",
            "blank",
            "unlimited",
            "none",
            "negative",
            "bad-unit",
            "spaced",
        ],
    )
    def test_a_value_that_is_not_a_lowering_is_a_load_error(
        self, tmp_path: Path, value: str
    ):
        path = tmp_path / "kew.toml"
        path.write_text(f'[sandbox]\nmax_memory = "{value}"\n')
        with pytest.raises(ConfigError, match="max_memory is lower-only"):
            load_config(path)

    def test_an_integer_zero_is_a_load_error_not_an_unlimited_cap(self, tmp_path: Path):
        # TOML types it as an int, which must not reach Docker as "--memory 0"
        # — the spelling Docker reads as no limit at all.
        path = tmp_path / "kew.toml"
        path.write_text("[sandbox]\nmax_memory = 0\n")
        with pytest.raises(ConfigError, match="max_memory is lower-only"):
            load_config(path)

    def test_a_bare_byte_count_inside_the_range_loads(self, tmp_path: Path):
        path = tmp_path / "kew.toml"
        path.write_text(f"[sandbox]\nmax_memory = {3 * 1024**3}\n")
        assert load_config(path).sandbox.max_memory == str(3 * 1024**3)

    @pytest.mark.parametrize("value", [1, 2, 4])
    def test_a_cpu_count_at_or_below_the_cap_loads(self, tmp_path: Path, value: int):
        path = tmp_path / "kew.toml"
        path.write_text(f"[sandbox]\nmax_cpus = {value}\n")
        assert load_config(path).sandbox.max_cpus == value

    @pytest.mark.parametrize(
        "literal", ["0", "-1", "5", "10", '"2"', '"unlimited"', "true", "2.5"]
    )
    def test_a_cpu_value_that_is_not_a_lowering_is_a_load_error(
        self, tmp_path: Path, literal: str
    ):
        path = tmp_path / "kew.toml"
        path.write_text(f"[sandbox]\nmax_cpus = {literal}\n")
        with pytest.raises(ConfigError, match="max_cpus is lower-only"):
            load_config(path)

    def test_the_error_names_the_rule_and_the_hard_cap(self, tmp_path: Path):
        # The operator has to learn "lower-only, ceiling 8g" from the failure
        # itself; a bare "invalid value" sends them to the source.
        path = tmp_path / "kew.toml"
        path.write_text('[sandbox]\nmax_memory = "16g"\n')
        with pytest.raises(ConfigError) as exc_info:
            load_config(path)
        message = str(exc_info.value)
        assert "lower-only" in message
        assert "8g" in message and "1g" in message
        assert "'16g'" in message


class TestSandboxHostOverrides:
    """``[sandbox.hosts]``: which host each run kind is placed on."""

    def test_a_misspelled_run_kind_is_refused_at_load(self) -> None:
        """An override is read ONCE, at placement, by exact key.

        A key naming no run kind matches no placement, so the kind the operator
        meant to move keeps the default host — the same invisible "I asked for a
        VM and got this machine" failure the host half is checked for.
        """
        with pytest.raises(ValidationError, match="sandbox.hosts keys"):
            SandboxConfig(host="local", hosts={"dispatc": "gcp"})

    def test_the_refusal_names_the_run_kinds_kew_does_run(self) -> None:
        with pytest.raises(ValidationError, match="dispatch, loop"):
            SandboxConfig(hosts={"disaptch": "gcp"})

    def test_every_run_kind_kew_runs_is_accepted(self) -> None:
        from kew.run_store import KNOWN_RUN_KINDS

        sandbox = SandboxConfig(hosts=dict.fromkeys(KNOWN_RUN_KINDS, "gcp"))

        assert {sandbox.host_for(kind) for kind in KNOWN_RUN_KINDS} == {"gcp"}

    def test_a_key_with_surrounding_space_still_names_its_kind(self) -> None:
        assert SandboxConfig(hosts={" dispatch ": "gcp"}).host_for("dispatch") == "gcp"

    def test_a_misspelled_run_kind_in_kew_toml_is_a_load_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "kew.toml"
        path.write_text('[sandbox.hosts]\nadversarial-review = "gcp"\n')

        with pytest.raises(ConfigError) as refusal:
            load_config(path)

        assert "sandbox.hosts keys" in str(refusal.value)

    @pytest.mark.parametrize(
        ("toml", "kind", "host"),
        [
            ('[sandbox]\nplacement = "gcp"\n', "dispatch", "gcp"),
            ('[sandbox]\nhost = "gcp"\n', "dispatch", "gcp"),
            ('[sandbox.hosts]\ndispatch = "gcp"\n', "dispatch", "gcp"),
        ],
    )
    def test_placement_off_this_machine_loads_once_the_boundary_holds(
        self, tmp_path: Path, toml: str, kind: str, host: str
    ) -> None:
        """#2569 closed the gaps the #2550 load gate held for: a config that
        places a run on a VM loads again."""
        path = tmp_path / "kew.toml"
        path.write_text(toml)

        assert load_config(path, env={}).sandbox.host_for(kind) == host

    def test_local_placement_still_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "kew.toml"
        path.write_text(
            '[sandbox]\nplacement = "local"\nhost = "gcp"\n'
            '[sandbox.hosts]\nreview = "local"\n'
        )

        assert load_config(path, env={}).sandbox.host_for("dispatch") == "local"


class TestSandboxConfig:
    """[sandbox] section: sandboxed dispatch container settings."""

    def test_sandbox_config_defaults_and_parse(self):
        cfg = DispatchConfig()
        # #654: default is now tri-state "auto" (sandbox when a runtime exists).
        assert cfg.sandbox.enabled == "auto"
        assert cfg.sandbox.runtime == "docker"
        assert isinstance(cfg.sandbox.image, str) and cfg.sandbox.image != ""
        assert cfg.sandbox.images == {}
        assert cfg.sandbox.network == "allowlist"
        assert cfg.sandbox.proxy_environment == "none"
        # Uncontained execution is opt-in, so the default must be False —
        # a fresh install cannot land on the host path by accident.
        assert cfg.sandbox.dangerously_skip_containment is False

    @pytest.mark.parametrize("mode", ["none", "inherit"])
    def test_sandbox_proxy_environment_parses(self, tmp_path: Path, mode: str):
        assert "proxy_environment" in SandboxConfig.model_fields
        path = tmp_path / "kew.toml"
        path.write_text(f'[sandbox]\nproxy_environment = "{mode}"\n')
        assert load_config(path).sandbox.proxy_environment == mode

    @pytest.mark.parametrize("mode", ["auto", "", "INHERIT", " inherit "])
    def test_sandbox_proxy_environment_rejects_invalid_values(self, mode: str):
        with pytest.raises(
            ValidationError,
            match="sandbox.proxy_environment must be 'none' or 'inherit'",
        ):
            SandboxConfig(proxy_environment=mode)

    @pytest.mark.parametrize("runtime", ["docker", "podman", "openshell"])
    def test_sandbox_runtime_accepts_every_supported_runtime(
        self, tmp_path: Path, runtime: str
    ):
        path = tmp_path / "kew.toml"
        path.write_text(f'[sandbox]\nruntime = "{runtime}"\n')
        assert load_config(path).sandbox.runtime == runtime

    @pytest.mark.parametrize(
        "runtime",
        ["", "nerdctl", "Docker", " docker ", "/usr/local/bin/docker", "custom"],
    )
    def test_sandbox_runtime_rejects_unknown_values(self, runtime: str):
        """An unsupported runtime name is inert, so it must fail at load.

        It used to be a free string: a typo, or a path where only a name is
        resolvable, reached dispatch and failed as "runtime not found" — a
        missing-Docker message for a config error.
        """
        with pytest.raises(
            ValidationError,
            match="sandbox.runtime must be one of 'docker', 'podman', 'openshell'",
        ):
            SandboxConfig(runtime=runtime)

    def test_sandbox_openshell_providers_map_parses(self, tmp_path: Path):
        """`[sandbox.openshell.providers]` maps a runner to its gateway provider.

        On the OpenShell runtime this map, not the host environment, is what
        admission checks: no credential VALUE reaches the agent, so an unmapped
        runner cannot authenticate at all (epic `openshell-runtime`, node N5).
        """
        toml = tmp_path / "kew.toml"
        toml.write_text(
            "[project]\nrepo = 'owner/repo'\n"
            "[sandbox]\nruntime = 'openshell'\n"
            "[sandbox.openshell.providers]\n"
            'claude-code = "kew-claude-code"\n'
            'codex = " kew-codex "\n'
        )
        providers = load_config(toml).sandbox.openshell.providers
        assert providers == {"claude-code": "kew-claude-code", "codex": "kew-codex"}

    def test_sandbox_openshell_providers_rejects_a_key_no_runner_resolves_to(self):
        """An unknown key is silently inert — it would read as set and never match."""
        with pytest.raises(
            ValidationError, match="providers key 'claude' is not a known runner"
        ):
            OpenShellConfig(providers={"claude": "kew-claude-code"})
        with pytest.raises(
            ValidationError, match="providers entries must be non-empty"
        ):
            OpenShellConfig(providers={"codex": "   "})
        with pytest.raises(ValidationError, match="maps runner 'codex' more than once"):
            OpenShellConfig(providers={"codex": "kew-codex", " codex": "kew-other"})

    def test_sandbox_images_map_parses(self, tmp_path: Path):
        """[sandbox.images] maps a runner name to the image that runs it (#1144).

        model_config forbids extra keys, so an unrecognised `images` table would
        be a hard config-load error, not a tolerated no-op.
        """
        toml = tmp_path / "kew.toml"
        toml.write_text(
            "[project]\nrepo = 'owner/repo'\n"
            "[sandbox.images]\n"
            'opencode = "ghcr.io/example/kew-agent-opencode:latest"\n'
        )
        cfg = load_config(toml)
        assert cfg.sandbox.images == {
            "opencode": "ghcr.io/example/kew-agent-opencode:latest"
        }
        # The legacy single-image field is untouched by the map.
        assert cfg.sandbox.image != ""

    def test_sandbox_images_rejects_empty_entries(self):
        # An empty ref would fail closed later with a confusing message; catch
        # the typo where the fix is obvious.
        with pytest.raises(ValidationError):
            SandboxConfig(images={"opencode": "  "})
        with pytest.raises(ValidationError):
            SandboxConfig(images={"": "some/image:tag"})

    def test_sandbox_images_strips_surrounding_whitespace(self):
        # Lookup is by exact runner name, so a padded key would never match and
        # the mapping would sit there looking set while being inert.
        cfg = SandboxConfig(images={" opencode ": "  example/img:tag  "})
        assert cfg.images == {"opencode": "example/img:tag"}

    @pytest.mark.parametrize("key", ["opencodee", "OpenCode", "claude", "cluade-code"])
    def test_sandbox_images_rejects_unknown_runner_keys(self, key: str):
        # A misspelled or wrong-case key resolves to nothing at dispatch time:
        # the runner is looked up by its registry name, misses, and the dispatch
        # fails closed as "unmapped" while pointing at a key the operator can
        # see they already set. Reject it at load instead.
        with pytest.raises(ValidationError) as excinfo:
            SandboxConfig(images={key: "example/img:tag"})
        message = str(excinfo.value)
        assert key in message
        # Actionable: the error lists what IS accepted.
        assert "claude-code" in message and "opencode" in message

    def test_sandbox_images_rejects_keys_colliding_after_strip(self):
        # Two keys that normalize to the same runner: one silently wins in dict
        # order, so say so rather than picking.
        with pytest.raises(ValidationError, match="more than once"):
            SandboxConfig(images={"opencode": "a/b:1", " opencode": "c/d:2"})

    def test_sandbox_images_accepts_every_registry_runner(self):
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        cfg = SandboxConfig(images={n: "example/img:tag" for n in KNOWN_RUNNER_NAMES})
        assert set(cfg.images) == set(KNOWN_RUNNER_NAMES)

    def test_sandbox_config_missing_table_yields_defaults(self, tmp_path: Path):
        """No [sandbox] table in kew.toml → backward-compatible defaults."""
        toml = tmp_path / "kew.toml"
        toml.write_text("[project]\nrepo = 'owner/repo'\n")
        cfg = load_config(config_path=toml)
        assert cfg.sandbox.enabled == "auto"
        assert cfg.sandbox.dangerously_skip_containment is False

    def test_sandbox_config_parses_enabled_true(self, tmp_path: Path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[sandbox]\nenabled = true\nruntime = 'docker'\n")
        cfg = load_config(config_path=toml)
        assert cfg.sandbox.enabled is True

    def test_sandbox_config_parses_enabled_false(self, tmp_path: Path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[sandbox]\nenabled = false\n")
        cfg = load_config(config_path=toml)
        assert cfg.sandbox.enabled is False

    def test_sandbox_config_parses_enabled_auto(self, tmp_path: Path):
        toml = tmp_path / "kew.toml"
        toml.write_text('[sandbox]\nenabled = "auto"\n')
        cfg = load_config(config_path=toml)
        assert cfg.sandbox.enabled == "auto"

    def test_sandbox_on_missing_runtime_is_removed_with_a_migration(self):
        """The removed key names its replacement instead of dying generically.

        `extra = "forbid"` would reject it with "Extra inputs are not
        permitted", which tells an upgrading operator nothing about why their
        config stopped working or what to do — the error-quality problem the
        gap audit raised as its own theme.
        """
        with pytest.raises(ValueError, match="dangerously_skip_containment"):
            SandboxConfig(on_missing_runtime="host")
        with pytest.raises(ValueError, match="refusing is now the default"):
            SandboxConfig(on_missing_runtime="ignore")

    def test_require_containment_and_skip_containment_cannot_both_be_set(self):
        """A config that demands containment and skips it is rejected at load.

        `enabled = true` means "refuse without a container"; the skip flag means
        "run without one". Resolution silently favoured the flag, so a config
        asking for a guarantee got the opposite with no signal at all. kew
        cannot know which half was meant, and guessing wrong in one direction
        dispatches an uncontained agent.
        """
        with pytest.raises(ValueError, match="contradict"):
            SandboxConfig(enabled=True, dangerously_skip_containment=True)

        # Each half alone stays valid — this rejects the combination, not either
        # setting, and must not become a reason people stop using `enabled`.
        assert SandboxConfig(enabled=True).dangerously_skip_containment is False
        assert SandboxConfig(dangerously_skip_containment=True).enabled == "auto"
        assert (
            SandboxConfig(enabled=False, dangerously_skip_containment=True).enabled
            is False
        )


class TestExtraForbidden:
    """Config models must reject unknown keys (extra='forbid')."""

    # --- unknown top-level section ---

    def test_dispatch_config_rejects_unknown_section(self):
        with pytest.raises(ValidationError) as exc_info:
            DispatchConfig.model_validate({"typo_section": {"foo": 1}})
        assert "typo_section" in str(exc_info.value)

    # --- unknown key inside a known section (one test per section family) ---

    def test_budget_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            BudgetConfig(per_isseu_usd=10.0)  # typo: isseu
        assert "per_isseu_usd" in str(exc_info.value)

    def test_agent_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            AgentConfig.model_validate({"timeout_minuttes": 60})  # typo
        assert "timeout_minuttes" in str(exc_info.value)

    def test_logging_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            LoggingConfig(diir="./logs")  # typo: diir
        assert "diir" in str(exc_info.value)

    def test_commands_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            CommandsConfig(cii="make test")  # typo: cii
        assert "cii" in str(exc_info.value)

    def test_retry_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            RetryConfig(max_attemtps=3)  # typo
        assert "max_attemtps" in str(exc_info.value)

    def test_parallel_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            ParallelConfig(max_workerss=2)  # typo
        assert "max_workerss" in str(exc_info.value)

    def test_sandbox_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            SandboxConfig(runttime="docker")  # typo
        assert "runttime" in str(exc_info.value)

    def test_prompt_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            PromptConfig(templat="")  # typo
        assert "templat" in str(exc_info.value)

    def test_alerting_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            AlertingConfig(notify_on_failuree=True)  # typo
        assert "notify_on_failuree" in str(exc_info.value)

    def test_review_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            ReviewConfig(auto_repply=True)  # typo
        assert "auto_repply" in str(exc_info.value)

    def test_triggers_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            TriggersConfig(ci_failure_max_retriess=1)  # typo
        assert "ci_failure_max_retriess" in str(exc_info.value)

    def test_overlap_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            OverlapConfig(strateggy="serialize")  # typo
        assert "strateggy" in str(exc_info.value)

    def test_approval_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            ApprovalConfig(threshold_usdd=5.0)  # typo
        assert "threshold_usdd" in str(exc_info.value)

    def test_epic_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            EpicConfig(stall_dayss=3)  # typo
        assert "stall_dayss" in str(exc_info.value)

    def test_pricing_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            PricingConfig(overridess={})  # typo
        assert "overridess" in str(exc_info.value)

    # --- typo'd nested key (via DispatchConfig round-trip) ---

    def test_nested_claude_code_rejects_unknown_key(self):
        with pytest.raises(ValidationError) as exc_info:
            ClaudeCodeConfig(skip_permissionss=True)  # typo
        assert "skip_permissionss" in str(exc_info.value)

    def test_dispatch_config_propagates_nested_error(self, tmp_path: Path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\nper_issue_usd = 10.0\ntypo_key = 999\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert "typo_key" in str(exc_info.value)

    # --- W4.3 row 13: the flat [agent] Claude Code keys are removed ---

    @pytest.mark.parametrize(
        ("key", "value"), [("skip_permissions", False), ("autocompact_pct", 50)]
    )
    def test_agent_flat_claude_code_key_is_rejected(self, key, value):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AgentConfig.model_validate({key: value})
        nested = AgentConfig.model_validate({"claude_code": {key: value}})
        assert getattr(nested.claude_code, key) == value

    def test_agent_flat_claude_code_key_fails_at_load(self, tmp_path: Path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[agent]\nskip_permissions = true\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert "skip_permissions" in str(exc_info.value)

    # --- W4.3 row 21: [agent.claude-code] is spelled [agent.claude_code] ---

    def test_agent_kebab_claude_code_table_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AgentConfig.model_validate({"claude-code": {"autocompact_pct": 50}})
        toml = tmp_path / "kew.toml"
        toml.write_text("[agent.claude-code]\nautocompact_pct = 50\n")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert "claude-code" in str(exc_info.value)
        toml.write_text("[agent.claude_code]\nautocompact_pct = 50\n")
        assert load_config(config_path=toml).agent.claude_code.autocompact_pct == 50

    # --- canonical config files still validate ---

    def test_dogfood_kew_toml_validates(self, tmp_path: Path):
        """The repo's own kew.toml must not fail the stricter models."""
        import tomllib
        from pathlib import Path as _Path

        repo_toml = _Path(__file__).parents[2] / "kew.toml"
        assert repo_toml.exists(), f"kew.toml not found at {repo_toml}"
        with open(repo_toml, "rb") as f:
            raw = tomllib.load(f)
        cfg = DispatchConfig.model_validate(raw)
        assert cfg.project.name == "kew"
        # #2349: this repo accepts only lows, from round 2 on.
        assert cfg.review.stop_rule_after_rounds == 1
        assert cfg.review.stop_rule_max_severity == "low"
        # The rule fires strictly AFTER the threshold and counts every recorded
        # round, while max_rounds counts only remediation cycles. A threshold at
        # or above the cap is usually dead config (the driver HELDs
        # `review_rounds_exhausted` first, and a release restarts round
        # numbering at 1) but a merge-queue round can instead carry it one round
        # PAST the cap — see test_review_stop_rule.py for both arrangements.
        assert cfg.review.stop_rule_after_rounds < cfg.review.max_rounds

    def test_create_kew_template_validates(self, tmp_path: Path):
        """create-kew/templates/kew.toml must validate under strict models."""
        import tomllib
        from pathlib import Path as _Path

        template_toml = (
            _Path(__file__).parents[2] / "create-kew" / "templates" / "kew.toml"
        )
        assert template_toml.exists(), f"template not found at {template_toml}"
        content = template_toml.read_text()
        # Replace template placeholders with valid values
        content = content.replace("{{PROJECT_NAME}}", '"test-project"')
        content = content.replace("{{CI_COMMAND}}", '"make test"')
        content = content.replace("{{FORMAT_COMMAND}}", '"make format"')
        content = content.replace("{{COMMIT_PREFIX}}", '"feat(#{issue}): "')
        with open(tmp_path / "kew.toml", "wb") as f:
            f.write(content.encode())
        with open(tmp_path / "kew.toml", "rb") as f:
            raw = tomllib.load(f)
        DispatchConfig.model_validate(raw)  # must not raise


class TestEnvVarConverterErrors:
    """Malformed env var values must raise a clear ValueError."""

    def test_malformed_int_env_var_raises_value_error(
        self, tmp_path: Path, monkeypatch
    ):
        toml = tmp_path / "kew.toml"
        toml.write_text("")
        monkeypatch.setenv("KEW_TIMEOUT", "not-a-number")
        with pytest.raises(ValueError, match="KEW_TIMEOUT"):
            load_config(config_path=toml)

    def test_malformed_float_env_var_raises_value_error(
        self, tmp_path: Path, monkeypatch
    ):
        toml = tmp_path / "kew.toml"
        toml.write_text("")
        monkeypatch.setenv("KEW_MAX_BUDGET", "not-a-float")
        with pytest.raises(ValueError, match="KEW_MAX_BUDGET"):
            load_config(config_path=toml)

    def test_malformed_int_env_var_has_structured_envelope(
        self, tmp_path: Path, monkeypatch
    ):
        toml = tmp_path / "kew.toml"
        toml.write_text("")
        monkeypatch.setenv("KEW_MAX_WORKERS", "two")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert exc_info.value.kind == "env_override"
        assert exc_info.value.key == "KEW_MAX_WORKERS"
        assert exc_info.value.expectation == "an integer"
        assert exc_info.value.fix == "set KEW_MAX_WORKERS to an integer; got 'two'"

    def test_malformed_kew_timeout_raises_config_error(
        self, tmp_path: Path, monkeypatch
    ):
        toml = tmp_path / "kew.toml"
        toml.write_text("")
        monkeypatch.setenv("KEW_TIMEOUT", "bad")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_path=toml)
        assert exc_info.value.kind == "env_override"
        assert exc_info.value.key == "KEW_TIMEOUT"
        assert exc_info.value.expectation == "an integer"
        assert exc_info.value.fix == "set KEW_TIMEOUT to an integer; got 'bad'"

    def test_valid_env_var_still_works(self, tmp_path: Path, monkeypatch):
        toml = tmp_path / "kew.toml"
        toml.write_text("")
        monkeypatch.setenv("KEW_TIMEOUT", "90")
        cfg = load_config(config_path=toml)
        assert cfg.agent.timeout_minutes == 90


class TestSnapshotProvenanceIsLoaderOnly:
    """[model_eol] snapshot_models is loader-set, never operator-set (#1679).

    The values are provenance read out of the curated lifecycle registry. They
    are still carried on the loaded config — every consumer reads them from
    there — but they are off the operator-writable schema, so a kew.toml that
    writes one fails to load naming the key.
    """

    PROVENANCE = [
        (ModelEolConfig, "model_eol", "snapshot_models"),
    ]

    @pytest.mark.parametrize(("model_cls", "section", "key"), PROVENANCE)
    def test_provenance_is_not_an_operator_field(
        self, model_cls: type, section: str, key: str
    ) -> None:
        assert key not in model_cls.model_fields
        with pytest.raises(ValidationError):
            model_cls(**{key: {}})

    @pytest.mark.parametrize(("model_cls", "section", "key"), PROVENANCE)
    def test_operator_toml_setting_provenance_fails_to_load_naming_the_key(
        self, tmp_path: Path, model_cls: type, section: str, key: str
    ) -> None:
        toml = tmp_path / "kew.toml"
        toml.write_text(f'[{section}]\n{key} = "anything"\n', encoding="utf-8")

        with pytest.raises(ValueError, match=re.escape(f"[{section}].{key}")):
            load_config(config_path=toml)

    def test_config_effective_rejects_operator_written_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rejection is visible on the operator's own surface."""
        from typer.testing import CliRunner

        from kew.cli import app

        monkeypatch.chdir(tmp_path)
        (tmp_path / "kew.toml").write_text(
            '[model_eol]\nsnapshot_models = ["vendor/hand-written"]\n',
            encoding="utf-8",
        )

        result = CliRunner().invoke(app, ["config", "effective"])

        assert result.exit_code == 2, result.output
        assert result.output.splitlines()[0] == str(tmp_path / "kew.toml")
        assert "[model_eol].snapshot_models" in result.output

    @pytest.mark.parametrize(
        "key",
        [
            "snapshot_path",
            "warn_age_days",
            "max_age_days",
            "snapshot_fetched_at",
            "snapshot_models",
        ],
    )
    def test_deleted_openrouter_overlay_keys_are_rejected(
        self, tmp_path: Path, key: str
    ) -> None:
        # `kew models refresh` replaced the overlay with no shim (#2711).
        toml = tmp_path / "kew.toml"
        toml.write_text(f'[pricing]\n{key} = "x"\n', encoding="utf-8")

        with pytest.raises(ValueError, match=key):
            load_config(config_path=toml)

    def test_loader_still_carries_lifecycle_provenance(self, tmp_path: Path) -> None:
        toml = tmp_path / "kew.toml"
        toml.write_text('[project]\nname = "t"\n', encoding="utf-8")

        config = load_config(config_path=toml)

        entries = config.model_eol.snapshot_models
        assert entries, "the packaged registry must still hydrate the loaded config"
        assert all(isinstance(entry, LifecycleEntry) for entry in entries.values())

    def test_provenance_stays_assignable_in_process(self) -> None:
        """Consumers and fixtures set provenance on a built config object."""
        config = DispatchConfig()

        config.model_eol.snapshot_models = {
            "vendor/priced": LifecycleEntry(status="retired", shutdown="2026-07-01")
        }

        assert config.model_eol.snapshot_models["vendor/priced"].status == "retired"

    def test_two_configs_do_not_share_provenance(self) -> None:
        first, second = DispatchConfig(), DispatchConfig()

        first.model_eol.snapshot_models = {"vendor/priced": LifecycleEntry()}

        assert "vendor/priced" not in second.model_eol.snapshot_models


def test_flight_recorder_trusted_proxies_default_empty():
    from kew.config import FlightRecorderConfig

    assert FlightRecorderConfig().trusted_proxies == []


def test_flight_recorder_trusted_proxies_rejects_host_bits_and_open_networks():
    """A host inside a network ('10.0.0.5/24') parses for us but is an inert
    literal to uvicorn's strict parser, so it must fail at load; a /0 is '*'."""
    from pydantic import ValidationError

    from kew.config import FlightRecorderConfig

    for entry in ("10.0.0.5/24", "127.0.0.1/24", "0.0.0.0/0", "::/0"):
        with pytest.raises(ValidationError):
            FlightRecorderConfig(trusted_proxies=[entry])


def test_flight_recorder_trusted_proxies_accepts_addresses_and_networks():
    from kew.config import FlightRecorderConfig

    fr = FlightRecorderConfig(trusted_proxies=["127.0.0.1", "10.0.0.0/24", "::1"])
    assert fr.trusted_proxies == ["127.0.0.1", "10.0.0.0/24", "::1"]


def test_flight_recorder_trusted_proxies_rejects_silently_inert_entries():
    """uvicorn keeps an unparseable entry as an opaque literal that matches no
    peer — a proxy allowlist that is silently inert. Reject at load instead."""
    import pytest
    from pydantic import ValidationError

    from kew.config import FlightRecorderConfig

    for bad in ("*", "proxy.example.com", "10.0.0.0/99", ""):
        with pytest.raises(ValidationError, match="trusted_proxies"):
            FlightRecorderConfig(trusted_proxies=[bad])


@pytest.mark.parametrize("enabled", ["auto", True, False])
@pytest.mark.parametrize("allow", [None, True, False])
def test_allow_uncontained_flows_config_policy(tmp_path, enabled, allow):
    import json

    toml = tmp_path / "kew.toml"
    text = "[sandbox]\n"
    if enabled != "auto":
        text += f"enabled = {json.dumps(enabled)}\n"
    if allow is not None:
        text += f"allow_uncontained_flows = {json.dumps(allow)}\n"
    toml.write_text(text)
    config = load_config(config_path=toml)
    assert config.sandbox.allow_uncontained_flows is allow
    assert config.sandbox.uncontained_flows_allowed is (
        enabled is True and allow is True
    )


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0, [], {}])
def test_allow_uncontained_flows_requires_a_boolean(invalid):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        SandboxConfig(allow_uncontained_flows=invalid)
    assert error.value.errors()[0]["type"] == "bool_type"


def test_command_verification_write_declaration_is_operator_opt_in():
    from kew.config import CommandsConfig

    assert "verify_loop_needs_writes" in CommandsConfig.model_fields
    assert CommandsConfig().verify_loop_needs_writes is False
    assert (
        CommandsConfig(verify_loop_needs_writes=True).verify_loop_needs_writes is True
    )


class TestTriageConfig:
    """``[triage]`` (#2293) — the station is off until an operator says so.

    Every knob is refused rather than coerced when it is nonsense: a classifier
    pointed at a backend that does not exist, or gated on a confidence outside
    [0, 1], is a station whose behaviour nobody can predict from its config.
    """

    def test_the_station_is_off_by_default(self):
        from kew.config import TriageConfig

        triage = TriageConfig()
        assert triage.enabled is False
        assert triage.intake_label == "kew:triage"
        assert (triage.backend, triage.fallback_backend) == ("local", "runner")
        assert triage.model == ""
        assert triage.min_confidence == 0.8
        assert triage.human_types == ["security", "question"]
        assert triage.dedupe_candidates == 10
        assert triage.max_body_chars == 20000
        assert triage.max_usd_per_item == 0.50
        assert triage.skip_epic_nodes is True

    def test_it_is_reachable_from_the_root_config(self):
        from kew.config import DispatchConfig

        assert DispatchConfig().triage.enabled is False

    def test_it_loads_from_kew_toml(self, tmp_path):
        toml = tmp_path / "kew.toml"
        toml.write_text(
            '[triage]\nenabled = true\nmodel = "local-7b"\nmin_confidence = 0.5\n'
        )
        config = load_config(config_path=toml)
        assert config.triage.enabled is True
        assert config.triage.model == "local-7b"
        assert config.triage.min_confidence == 0.5

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("backend", "openai"),
            ("fallback_backend", "openai"),
            ("min_confidence", 1.5),
            ("min_confidence", -0.1),
            ("human_types", ["bogus"]),
            ("dedupe_candidates", -1),
            ("max_body_chars", -1),
            ("max_usd_per_item", -0.01),
        ],
    )
    def test_nonsense_is_refused_not_coerced(self, field, value):
        from pydantic import ValidationError

        from kew.config import TriageConfig

        with pytest.raises(ValidationError):
            TriageConfig(**{field: value})

    def test_an_unknown_key_is_refused(self):
        from pydantic import ValidationError

        from kew.config import TriageConfig

        with pytest.raises(ValidationError) as error:
            TriageConfig(propose_and_apply=True)
        assert error.value.errors()[0]["type"] == "extra_forbidden"

    def test_every_human_type_is_a_triage_type(self):
        """The gate names classifications, so it is checked against the schema."""
        from kew.config import TriageConfig
        from kew.triage.schema import TRIAGE_TYPES

        assert set(TriageConfig().human_types) <= TRIAGE_TYPES
        assert TriageConfig(human_types=sorted(TRIAGE_TYPES)).human_types

    def test_an_empty_human_types_list_is_legal(self):
        """The loud end of the range, not a disabled check."""
        from kew.config import TriageConfig

        assert TriageConfig(human_types=[]).human_types == []
