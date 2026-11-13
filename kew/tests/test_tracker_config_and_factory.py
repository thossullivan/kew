"""[tracker] config section + the epic DependencySource factory.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Config.
One tracker per project; default github (zero change for existing users).
"""

import pytest
from pydantic import ValidationError

from kew.config import DispatchConfig, TrackerConfig
from kew.graph.adapters import make_dependency_source
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.adapters.github_native import GitHubNativeSource
from kew.graph.adapters.linear_native import LinearSource


class TestTrackerConfig:
    def test_default_is_github(self):
        cfg = DispatchConfig()
        assert cfg.tracker.type == "github"
        assert cfg.tracker.linear.in_review_state == "In Review"

    def test_linear_type_parses(self):
        cfg = DispatchConfig.model_validate(
            {"tracker": {"type": "linear", "linear": {"in_review_state": "In QA"}}}
        )
        assert cfg.tracker.type == "linear"
        assert cfg.tracker.linear.in_review_state == "In QA"

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            TrackerConfig.model_validate({"type": "jira"})


class TestMakeDependencySource:
    def test_github_default(self):
        cfg = DispatchConfig(project={"repo": "acme/widgets"})
        src = make_dependency_source(cfg, env={})
        # The native adapter is the GitHub default since W2.8a; the probe runs
        # on the first read, not here, and its fallback is the convention read.
        assert isinstance(src, GitHubNativeSource)
        assert src.repo == "acme/widgets"
        assert isinstance(src._fallback, GitHubConventionSource)

    def test_linear_requires_api_key(self):
        cfg = DispatchConfig.model_validate({"tracker": {"type": "linear"}})
        with pytest.raises(RuntimeError, match="LINEAR_API_KEY"):
            make_dependency_source(cfg, env={})

    def test_linear_with_key(self):
        cfg = DispatchConfig.model_validate(
            {"tracker": {"type": "linear", "linear": {"in_review_state": "In QA"}}}
        )
        src = make_dependency_source(cfg, env={"LINEAR_API_KEY": "lin_x"})
        assert isinstance(src, LinearSource)
        assert src._in_review_state == "In QA"

    def test_epic_prefix_reaches_github_source(self):
        cfg = DispatchConfig(project={"repo": "acme/widgets"})
        src = make_dependency_source(cfg, env={}, epic_prefix="milestone:")
        assert src.epic_prefix == "milestone:"


class TestEpicCliOnLinear:
    def test_load_epic_graph_linear_skips_gh_and_builds(self, monkeypatch):
        """End-to-end through cli._load_epic_graph: linear tracker uses the
        HTTPS client (scripted transport), never gh/preflight."""
        import json
        from unittest.mock import patch

        from kew import cli
        from kew.linear.client import LinearClient

        responses = [
            {"data": {"projects": {"nodes": [{"id": "p1", "name": "payments"}]}}},
            {
                "data": {
                    "project": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "00000000-0000-0000-0000-000000000001",
                                    "identifier": "ENG-1",
                                    "title": "a",
                                    "url": "https://linear.app/x/issue/ENG-1",
                                    "state": {"name": "Todo", "type": "unstarted"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
            {
                "data": {
                    "i0": {
                        "identifier": "ENG-1",
                        "relations": {"nodes": []},
                        "inverseRelations": {"nodes": []},
                    }
                }
            },
        ]

        def transport(url, payload, headers):
            return 200, json.dumps(responses.pop(0)).encode()

        def client_factory(api_key):
            return LinearClient(api_key, transport=transport, sleep=lambda _s: None)

        cfg = DispatchConfig.model_validate({"tracker": {"type": "linear"}})
        monkeypatch.setenv("LINEAR_API_KEY", "lin_test")
        with (
            patch("kew.cli.load_config", return_value=cfg),
            patch("kew.graph.adapters.LinearClient", client_factory),
            patch("kew.cli.preflight_tools") as preflight,
        ):
            graph = cli._load_epic_graph("payments")
        preflight.assert_not_called()
        assert list(graph.nodes) == ["ENG-1"]


class TestDriverTrackerBoundary:
    def test_driver_commands_can_construct_their_source(self):
        """Merge regression (PR #448 x #449): the driver commands reference
        GitHubConventionSource at call time; the Linear branch had replaced
        the module import with the factory, leaving a runtime NameError the
        auto-merge could not see."""
        import kew.cli as cli
        from kew.graph.adapters.github_convention import GitHubConventionSource

        assert cli.GitHubConventionSource is GitHubConventionSource

    def test_driver_config_accepts_linear_tracker(self):
        """L3: epic advance/run/retry drive Linear epics too (item-id currency).
        The gate no longer rejects tracker=linear; only `epic plan` stays
        GitHub-only. PRs still live on GitHub, so the repo is still resolved."""
        from unittest.mock import patch

        from kew import cli

        cfg = DispatchConfig.model_validate({"tracker": {"type": "linear"}})
        with (
            patch("kew.cli.load_config", return_value=cfg),
            patch("kew.cli.preflight_tools"),
            patch("kew.cli._epic_repo", return_value="o/r"),
        ):
            config, repo, _max, _approvers = cli._epic_driver_config(None)
        assert config.tracker.type == "linear"
        assert repo == "o/r"

    def test_epic_plan_still_rejects_linear_tracker(self):
        """`epic plan` stays GitHub-only (its on-ramp mints int-keyed issues)."""
        from unittest.mock import patch

        import typer

        from kew import cli

        cfg = DispatchConfig.model_validate({"tracker": {"type": "linear"}})
        with (
            patch("kew.cli.load_config", return_value=cfg),
            pytest.raises(typer.Exit) as exc,
        ):
            cli.epic_plan("myepic", from_plan=None, from_manifest=None)
        assert exc.value.exit_code == 2


class TestEpicStatusTrackerWiring:
    """`epic status` must use the same tracker-aware wiring as advance/run.

    The pilot-rivets live drive crashed here: under tracker=linear,
    `epic_status` skipped `preflight_tools()` (the Linear `_load_epic_graph`
    branch is HTTPS-only, but status still shells `gh` for repo detection and
    PR facts) and hardwired `GitHubFactsReader` instead of
    `_epic_driver_sources`' `LinearFactsReader`.
    """

    def test_status_preflights_and_uses_factory_facts(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from kew import cli

        cfg = DispatchConfig.model_validate(
            {"tracker": {"type": "linear"}, "logging": {"dir": str(tmp_path)}}
        )
        graph = MagicMock()
        sentinel_facts = object()
        mock_summarize = MagicMock()
        with (
            patch("kew.cli._load_epic_graph", return_value=graph),
            patch("kew.cli.load_config", return_value=cfg),
            patch("kew.cli.preflight_tools") as preflight,
            patch("kew.cli._epic_repo", return_value="o/r"),
            patch(
                "kew.cli._epic_driver_sources",
                return_value=(MagicMock(), sentinel_facts, None),
            ) as sources,
            patch("kew.cli.summarize", mock_summarize),
            patch("kew.cli._print_tick_summary"),
            patch("kew.cli.lock_holder", return_value=None),
        ):
            cli.epic_status("pilot-rivets")
        preflight.assert_called_once()
        sources.assert_called_once()
        assert mock_summarize.call_args.kwargs["facts"] is sentinel_facts
