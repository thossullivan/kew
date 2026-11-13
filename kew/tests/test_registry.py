"""Tests for runner registry and model resolution."""

from pathlib import Path

import pytest

from kew.config import DispatchConfig, RouteEntry, RoutingConfig
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.registry import KNOWN_RUNNER_NAMES, resolve_runner


class TestResolveRunner:
    def test_explicit_codex_resolves_without_changing_gpt_inference(self):
        from kew.runners.codex import CodexRunner
        from kew.runners.opencode import OpenCodeRunner

        explicit = DispatchConfig()
        explicit.agent.runner = "codex"
        explicit.agent.model = "gpt-5.6-sol"
        assert isinstance(resolve_runner(explicit).runner, CodexRunner)

        inferred = DispatchConfig()
        inferred.agent.model = "gpt-5.6-sol"
        assert isinstance(resolve_runner(inferred).runner, OpenCodeRunner)

    def test_explicit_claude_code(self):
        config = DispatchConfig()
        config.agent.runner = "claude-code"
        runner = resolve_runner(config).runner
        assert isinstance(runner, ClaudeCodeRunner)

    def test_explicit_pi(self):
        from kew.runners.pi import PiRunner

        config = DispatchConfig()
        config.agent.runner = "pi"
        runner = resolve_runner(config).runner
        assert isinstance(runner, PiRunner)

    def test_infer_from_claude_model(self):
        config = DispatchConfig()
        config.agent.model = "claude-sonnet-4"
        runner = resolve_runner(config).runner
        assert isinstance(runner, ClaudeCodeRunner)

    def test_infer_from_ollama_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "ollama/deepseek-coder"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_default_to_claude_code(self):
        config = DispatchConfig()
        runner = resolve_runner(config).runner
        assert isinstance(runner, ClaudeCodeRunner)

    def test_unknown_runner_raises(self):
        config = DispatchConfig()
        config.agent.runner = "nonexistent"
        with pytest.raises(ValueError, match="Unknown runner"):
            resolve_runner(config)

    def test_infer_from_gpt_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "gpt-4o"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_unknown_model_no_runner_raises(self):
        config = DispatchConfig()
        config.agent.model = "some-unknown-model"
        with pytest.raises(ValueError, match="Can't infer runner"):
            resolve_runner(config)

    def test_explicit_opencode(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.runner = "opencode"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_infer_from_gemini_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "gemini-2.5-pro"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_infer_from_o3_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "o3-mini"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_infer_provider_slash_model_form(self):
        """Multi-segment provider/model strings without a known prefix -> opencode."""
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "openrouter/anthropic/claude-sonnet-4-6"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_infer_from_openrouter_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "openrouter/qwen/qwen3-coder"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)

    def test_openrouter_prefix_is_explicit(self):
        """openrouter/ maps via an explicit pattern, not the generic "/" fallback."""
        from kew.runners.registry import _MODEL_PATTERNS

        assert ("openrouter/", "opencode") in _MODEL_PATTERNS

    def test_infer_simple_provider_slash_model(self):
        from kew.runners.opencode import OpenCodeRunner

        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        runner = resolve_runner(config).runner
        assert isinstance(runner, OpenCodeRunner)


class TestResolvedRoute:
    def test_construction(self):
        from kew.runners.claude_code import ClaudeCodeRunner
        from kew.runners.registry import ResolvedRoute

        route = ResolvedRoute(runner=ClaudeCodeRunner(), model="claude-opus-4")
        assert route.runner.name == "claude-code"
        assert route.model == "claude-opus-4"


class TestResolveRunnerWithRouting:
    """Tests for resolve_runner with complexity-based routing."""

    def _make_config(self, routing=None, runner="", model=""):
        """Helper to build a DispatchConfig with routing."""
        from kew.config import AgentConfig, DispatchConfig

        return DispatchConfig(
            agent=AgentConfig(runner=runner, model=model),
            routing=routing,
        )

    def test_complexity_routes_to_correct_runner(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
            ),
        )
        route = resolve_runner(config, complexity="simple")
        assert route.runner.name == "opencode"
        assert route.model == "ollama/deepseek-coder"

    def test_default_fallback_when_level_absent(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
            ),
        )
        route = resolve_runner(config, complexity="complex")
        assert route.runner.name == "claude-code"
        assert route.model == "claude-sonnet-4"

    def test_string_shorthand_resolves_runner(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default="claude-code",
            ),
            model="claude-sonnet-4",  # model comes from [agent] config
        )
        route = resolve_runner(config, complexity="simple")
        assert route.runner.name == "claude-code"

    def test_string_shorthand_inherits_model_from_agent_config(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(default="claude-code"),
            model="claude-sonnet-4",
        )
        route = resolve_runner(config, complexity="medium")
        assert route.model == "claude-sonnet-4"

    def test_no_routing_returns_resolved_route(self):
        """Without [routing], still returns ResolvedRoute for uniform API."""
        from kew.runners.registry import resolve_runner

        config = self._make_config(runner="claude-code", model="claude-sonnet-4")
        route = resolve_runner(config)
        assert route.runner.name == "claude-code"
        assert route.model == "claude-sonnet-4"

    def test_all_three_levels_specified(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        simple = resolve_runner(config, complexity="simple")
        medium = resolve_runner(config, complexity="medium")
        cmplx = resolve_runner(config, complexity="complex")

        assert simple.runner.name == "opencode"
        assert medium.model == "claude-sonnet-4"
        assert cmplx.model == "claude-opus-4"

    def test_routing_with_no_complexity_uses_default(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        route = resolve_runner(config, complexity=None)
        assert route.runner.name == "opencode"

    def test_invalid_runner_in_route_raises(self):
        from kew.runners.registry import resolve_runner

        config = self._make_config(
            routing=RoutingConfig(
                default=RouteEntry(runner="nonexistent", model="foo"),
            ),
        )
        with pytest.raises(ValueError, match="Unknown runner"):
            resolve_runner(config, complexity="simple")


class TestRunnerAcceptsModel:
    """The registry's model<->runner compatibility answer (#1111).

    Inference must name exactly ONE runner per model; compatibility is the wider
    question a route swap asks, so these are deliberately not the same function.
    """

    @pytest.mark.parametrize(
        ("runner_name", "model"),
        [
            ("claude-code", "claude-sonnet-4-6"),
            ("opencode", "openrouter/deepseek/deepseek-v4-flash"),
            ("opencode", "ollama/qwen3"),
            ("opencode", "gpt-4o"),
            ("opencode", "vendor/some-model"),  # bare provider/model fallback
            ("lemonade", "lemonade/qwen3-30b"),
            # Shared namespace: all three OpenRouter harnesses drive these, even
            # though inference attributes the prefix to opencode alone.
            ("pi", "openrouter/deepseek/deepseek-v4-flash"),
            ("hermes", "openrouter/deepseek/deepseek-v4-flash"),
        ],
    )
    def test_accepts_models_in_the_runners_namespace(self, runner_name, model):
        from kew.runners.registry import runner_accepts_model

        assert runner_accepts_model(runner_name, model)

    @pytest.mark.parametrize(
        ("runner_name", "model"),
        [
            ("claude-code", "openrouter/deepseek/deepseek-v4-flash"),
            ("claude-code", "ollama/qwen3"),
            ("opencode", "claude-sonnet-4-6"),
            ("hermes", "claude-sonnet-4-6"),  # hermes is OpenRouter-only
            ("lemonade", "openrouter/deepseek/deepseek-v4-flash"),
            ("claude-code", ""),  # nothing to validate is not a valid pair
            ("nonexistent", "claude-sonnet-4-6"),
        ],
    )
    def test_rejects_models_outside_it(self, runner_name, model):
        from kew.runners.registry import runner_accepts_model

        assert not runner_accepts_model(runner_name, model)

    def test_tracks_the_inference_table_it_is_derived_from(self):
        """A new routing prefix cannot be added without this check seeing it."""
        from kew.runners.registry import _MODEL_PATTERNS, runner_accepts_model

        for prefix, mapped_runner in _MODEL_PATTERNS:
            assert runner_accepts_model(mapped_runner, f"{prefix}x")

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "o1", "o3", "o4"])
    def test_codex_accepts_openai_models(self, model):
        from kew.runners.registry import runner_accepts_model

        assert runner_accepts_model("codex", model)

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5", "openrouter/openai/gpt-5.6-sol", "o30"],
    )
    def test_codex_rejects_other_namespaces_and_bare_prefix_collisions(self, model):
        from kew.runners.registry import runner_accepts_model

        assert not runner_accepts_model("codex", model)


class TestKnownRunnerNames:
    """KNOWN_RUNNER_NAMES is the name-only view config validation checks
    `[sandbox] images.<runner>` keys against (#1144), declared separately
    because kew.config cannot import the runner classes. Drift would either
    reject a real runner's image mapping or accept a dead one."""

    def test_matches_the_runner_factories(self):
        from kew.runners.registry import KNOWN_RUNNER_NAMES, _get_runner_factories

        assert frozenset(_get_runner_factories()) == KNOWN_RUNNER_NAMES


class TestRequiredCredentials:
    """Every runner declares the env var names it authenticates with
    in-container (#1146). That declaration IS the container env allowlist, so a
    runner missing it (or declaring a name-shaped value) is a security defect,
    not a typing one."""

    def test_every_registered_runner_declares_required_credentials(self):
        from kew.runners.registry import (
            KNOWN_RUNNER_NAMES,
            runner_required_credentials,
        )

        for name in sorted(KNOWN_RUNNER_NAMES):
            declared = runner_required_credentials(name)
            assert isinstance(declared, tuple)
            for var in declared:
                # Env var NAMES only — a value, a path, or a `NAME=value` pair
                # here would travel straight into the container argv.
                assert var == var.upper()
                assert "=" not in var and "/" not in var

    def test_no_runner_borrows_another_providers_credential(self):
        from kew.runners.registry import runner_required_credentials

        claude = set(runner_required_credentials("claude-code"))
        assert claude == {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
        for openrouter_runner in ("opencode", "pi", "hermes"):
            declared = set(runner_required_credentials(openrouter_runner))
            assert declared == {"OPENROUTER_API_KEY"}
            assert not (declared & claude)
        # lemonade's endpoint is host-local: nothing to forward, so nothing is.
        assert runner_required_credentials("lemonade") == ()

    def test_unknown_runner_declares_nothing(self):
        from kew.runners.registry import runner_required_credentials

        assert runner_required_credentials("not-a-runner") == ()


class TestContainmentPosture:
    """Native containment is an explicit runner-code capability (#1322)."""

    def test_only_codex_declares_native_containment(self):
        from kew.runners.registry import (
            KNOWN_RUNNER_NAMES,
            runner_containment_posture,
        )

        declared = {
            name: runner_containment_posture(name)
            for name in sorted(KNOWN_RUNNER_NAMES)
        }
        assert declared == {
            "claude-code": None,
            "codex": "native",
            "hermes": None,
            "lemonade": None,
            "opencode": None,
            "pi": None,
        }

    def test_the_runtime_reaches_the_runner_that_answers_per_runtime(self):
        """Codex declines its own posture on ``openshell`` (node N10).

        The registry forwards the runtime rather than answering from a table of
        its own, so this is the proof the plumbing is there for every runner —
        the declaration stays runner-owned, and no config value can grant one.
        """
        from kew.runners.registry import (
            KNOWN_RUNNER_NAMES,
            runner_containment_posture,
        )

        declared = {
            name: runner_containment_posture(name, runtime="openshell")
            for name in sorted(KNOWN_RUNNER_NAMES)
        }
        assert declared == dict.fromkeys(sorted(KNOWN_RUNNER_NAMES))
        assert runner_containment_posture("codex", runtime="docker") == "native"
        assert runner_containment_posture("codex", runtime="podman") == "native"

    def test_unknown_runner_cannot_acquire_native_containment(self):
        from kew.runners.registry import runner_containment_posture

        assert runner_containment_posture("not-a-runner") is None
        assert runner_containment_posture("not-a-runner", runtime="docker") is None


class TestExplainRunnerRoute:
    """``resolve_runner``'s sibling: the same answer, plus the rule (#2302)."""

    def test_it_agrees_with_resolve_runner_on_every_tier(self):
        from kew.runners.registry import explain_runner_route

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
                simple=RouteEntry(runner="codex", model="gpt-5.6-sol"),
            )
        )
        for tier in (None, "simple", "medium", "complex"):
            explained = explain_runner_route(config, tier)
            resolved = resolve_runner(config, tier)
            assert explained.runner_name == resolved.runner.name
            assert explained.model == resolved.model

    def test_it_names_the_tier_entry_that_decided_the_route(self):
        from kew.runners.registry import explain_runner_route

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="codex", model="gpt-5.6-sol"),
            )
        )
        explained = explain_runner_route(config, "simple")
        assert explained.runner_source == "kew.toml [routing].simple"
        assert explained.field == "routing.simple"
        # A tier nobody configured falls to the default, and says which.
        fallback = explain_runner_route(config, "complex")
        assert fallback.runner_source == "kew.toml [routing].default"
        assert fallback.field == "routing.default"

    def test_the_two_halves_of_a_route_can_come_from_different_layers(self):
        from kew.runners.registry import explain_runner_route

        config = DispatchConfig(
            routing=RoutingConfig(default=RouteEntry(runner="codex"))
        )
        config.agent.model = "gpt-5.6-sol"
        explained = explain_runner_route(config)
        assert explained.runner_source == "kew.toml [routing].default"
        assert explained.model_source == "kew.toml [agent].model"

    def test_each_half_names_the_override_field_that_really_steers_it(self):
        """``field`` and ``model_field`` are two fields, not one.

        A caller asks the override table one question per row, and a tier entry
        without a ``model`` leaves the runner steered by ``routing.<tier>`` and
        the model by ``agent.model`` — so one dotted field for both rows
        reported an override against a row it does not set (#2302 review, P2).
        """
        from kew.runners.registry import explain_runner_route

        shared = RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
            medium=RouteEntry(runner="codex"),
        )
        config = DispatchConfig(routing=shared)
        config.agent.model = "gpt-5.6-sol"

        from_entry = explain_runner_route(config, "default")
        assert (from_entry.field, from_entry.model_field) == (
            "routing.default",
            "routing.default",
        )
        from_agent = explain_runner_route(config, "medium")
        assert (from_agent.field, from_agent.model_field) == (
            "routing.medium",
            "agent.model",
        )
        # And with no [routing] at all, both halves come off [agent].
        bare = DispatchConfig()
        bare.agent.model = "claude-opus-4-8"
        assert explain_runner_route(bare).model_field == "agent.model"

    def test_the_no_routing_path_distinguishes_a_pin_from_an_inference(self):
        from kew.runners.registry import explain_runner_route

        pinned = DispatchConfig()
        pinned.agent.runner = "codex"
        pinned.agent.model = "gpt-5.6-sol"
        assert explain_runner_route(pinned).runner_source == "kew.toml [agent].runner"

        inferred = DispatchConfig()
        inferred.agent.model = "claude-opus-4-8"
        assert explain_runner_route(inferred).runner_source == (
            "inferred from [agent].model"
        )

    def test_an_environment_runner_is_never_reported_as_a_kew_toml_line(
        self, monkeypatch
    ):
        """``KEW_RUNNER`` is a layer the file cannot see, and it wins.

        ``load_config`` applies the env AFTER the file and the DB overrides, so
        a plan that named ``kew.toml [agent].runner`` sent the operator to a
        file line that does not exist — the rule
        ``kew.service.config.config_snapshot`` states ("an environment variable
        can never be reported as something the file said"), on the one layer
        this explanation could not see (#2302 review).
        """
        from kew.runners.registry import explain_runner_route

        monkeypatch.setenv("KEW_RUNNER", "codex")
        env_only = DispatchConfig()
        env_only.agent.runner = "codex"  # what load_config leaves behind
        explained = explain_runner_route(env_only)
        assert explained.runner_source == "env (KEW_RUNNER)"
        assert explained.field == "agent.runner"
        # Unset, the file pin is the answer again — the env layer is not
        # claimed on a host that never set it.
        monkeypatch.delenv("KEW_RUNNER")
        assert explain_runner_route(env_only).runner_source == (
            "kew.toml [agent].runner"
        )

    def test_an_empty_config_reports_the_default_runner_as_a_default(self):
        from kew.runners.registry import explain_runner_route

        bare = DispatchConfig()
        bare.agent.runner = ""
        bare.agent.model = ""
        explained = explain_runner_route(bare)
        assert isinstance(explained.route.runner, ClaudeCodeRunner)
        assert explained.runner_source == "default (claude-code)"
        assert explained.model_source == "default"


class TestPreflightRunnerWorkspace:
    """One workspace-admission rule, asked by name (PR #2461 review)."""

    def test_the_codex_project_tree_rule_is_asked_by_runner_name(self, tmp_path):
        from kew.runners.codex import CodexProjectConfigError
        from kew.runners.registry import preflight_runner_workspace

        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text("[sandbox]\n")
        with pytest.raises(CodexProjectConfigError, match="entry is not allowed"):
            preflight_runner_workspace("codex", tmp_path)

    def test_no_other_runner_is_held_to_codex_s_tree_rule(self, tmp_path):
        """A `.codex/` dir is not a reason to refuse somebody else's launch."""
        from kew.runners.registry import preflight_runner_workspace

        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text("[sandbox]\n")
        for name in sorted(KNOWN_RUNNER_NAMES - {"codex"}) + ["not-a-runner"]:
            preflight_runner_workspace(name, tmp_path)

    def test_the_host_lane_asks_this_function_rather_than_its_own_copy(self):
        """`orchestrator`'s preflight is a caller, not a second rule.

        Two copies of an admission check is how the contained lane came to be
        missing one; patching the shared function has to silence both.
        """
        from unittest.mock import patch

        from kew import orchestrator
        from kew.runners.codex import CodexRunner

        with patch("kew.runners.registry.preflight_runner_workspace") as shared:
            orchestrator._preflight_runner_workspace(CodexRunner(), Path("/nowhere"))
        shared.assert_called_once_with("codex", Path("/nowhere"))
