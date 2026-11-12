"""Tests for Phase 3: cache-TTL-aware dispatch ordering (#350)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew.config import CacheConfig, DispatchConfig, RouteEntry, RoutingConfig


def _issue(
    num: int,
    priority: int = 9,
    created: str = "2026-01-01T00:00:00Z",
    complexity: str | None = None,
) -> dict[str, Any]:
    """Build a minimal issue dict for ordering tests."""
    labels: list[dict[str, str]] = []
    if priority < 9:
        labels.append({"name": f"p{priority}-test"})
    if complexity:
        labels.append({"name": complexity})
    labels.append({"name": "ready"})
    return {
        "number": num,
        "title": f"Issue #{num}",
        "labels": labels,
        "createdAt": created,
        "body": "",
    }


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestCacheConfig:
    def test_defaults(self):
        cfg = CacheConfig()
        assert cfg.order_aware is False
        assert cfg.max_reorder_lookahead == 10

    def test_explicit_values(self):
        cfg = CacheConfig(order_aware=True, max_reorder_lookahead=5)
        assert cfg.order_aware is True
        assert cfg.max_reorder_lookahead == 5

    def test_lookahead_must_be_positive(self):
        with pytest.raises(ValueError):
            CacheConfig(max_reorder_lookahead=0)

    def test_nested_under_routing(self):
        routing = RoutingConfig(
            default="claude-code",
            cache=CacheConfig(order_aware=True, max_reorder_lookahead=3),
        )
        assert routing.cache.order_aware is True
        assert routing.cache.max_reorder_lookahead == 3

    def test_routing_default_cache(self):
        routing = RoutingConfig(default="claude-code")
        assert routing.cache.order_aware is False
        assert routing.cache.max_reorder_lookahead == 10


# ---------------------------------------------------------------------------
# Reorder logic tests
# ---------------------------------------------------------------------------


class TestReorderWithinTiers:
    """Within-tier clustering groups same-prefix_key issues."""

    def test_single_issue_unchanged(self):
        from kew.selector import reorder_within_tiers

        issues = [_issue(1)]
        config = DispatchConfig()
        result, _ = reorder_within_tiers(issues, config)
        assert [i["number"] for i in result] == [1]

    def test_empty_list(self):
        from kew.selector import reorder_within_tiers

        config = DispatchConfig()
        result, _ = reorder_within_tiers([], config)
        assert result == []

    def test_same_complexity_same_cluster(self):
        """Issues with the same complexity route to the same prefix cluster."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig()
        issues = [
            _issue(1, priority=1, created="2026-01-01"),
            _issue(2, priority=1, created="2026-01-02"),
            _issue(3, priority=1, created="2026-01-03"),
        ]
        result, _ = reorder_within_tiers(issues, config)
        assert [i["number"] for i in result] == [1, 2, 3]

    def test_clusters_same_prefix_adjacently(self):
        """Issues with different complexities get interleaved by cluster key."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        # P1 tier: simple, medium, simple, medium, simple
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
            _issue(4, priority=1, created="2026-01-04", complexity="medium"),
            _issue(5, priority=1, created="2026-01-05", complexity="simple"),
        ]
        result, _ = reorder_within_tiers(issues, config)
        nums = [i["number"] for i in result]
        # First cluster: simple issues (1, 3, 5), then medium (2, 4)
        assert nums == [1, 3, 5, 2, 4]

    def test_priority_invariant_preserved(self):
        """A lower-priority issue never floats above a higher tier."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        issues = [
            _issue(1, priority=0, created="2026-01-01", complexity="simple"),
            _issue(2, priority=0, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
            _issue(4, priority=1, created="2026-01-04", complexity="medium"),
        ]
        result, _ = reorder_within_tiers(issues, config)
        nums = [i["number"] for i in result]
        # P0 issues (1, 2) must come before P1 issues (3, 4)
        p0_indices = [nums.index(n) for n in [1, 2]]
        p1_indices = [nums.index(n) for n in [3, 4]]
        assert max(p0_indices) < min(p1_indices)

    def test_lookahead_bound_respected(self):
        """Issues beyond the lookahead window are not pulled forward."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        # With lookahead=2: only scan 2 positions ahead
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="medium"),
            _issue(4, priority=1, created="2026-01-04", complexity="simple"),  # too far
        ]
        result, _ = reorder_within_tiers(issues, config, max_lookahead=2)
        nums = [i["number"] for i in result]
        # Issue 4 is at index 3 (3 positions away from issue 1 at index 0),
        # but lookahead is 2 so we scan indices [1, 3) = [1, 2] — issue 4 NOT pulled
        assert nums == [1, 2, 3, 4]

    def test_lookahead_includes_boundary(self):
        """Issue exactly at the lookahead boundary IS pulled forward."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        # With lookahead=3: scan 3 positions ahead from start
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="medium"),
            _issue(4, priority=1, created="2026-01-04", complexity="simple"),
        ]
        result, _ = reorder_within_tiers(issues, config, max_lookahead=3)
        nums = [i["number"] for i in result]
        # Lookahead=3: scan from index 1 to 4 (exclusive), so index 3 IS included
        assert nums == [1, 4, 2, 3]

    def test_deterministic_ordering(self):
        """Calling reorder twice produces the same result."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        r1, _ = reorder_within_tiers(issues, config)
        r2, _ = reorder_within_tiers(issues, config)
        assert [i["number"] for i in r1] == [i["number"] for i in r2]

    def test_no_routing_all_same_cluster(self):
        """Without routing config, all issues share one cluster — no reorder."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig()
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="complex"),
        ]
        result, _ = reorder_within_tiers(issues, config)
        assert [i["number"] for i in result] == [1, 2, 3]

    def test_mixed_tiers_reorder_within_each(self):
        """Reorder happens independently within each priority tier."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        issues = [
            # P0 tier
            _issue(1, priority=0, created="2026-01-01", complexity="simple"),
            _issue(2, priority=0, created="2026-01-02", complexity="medium"),
            _issue(3, priority=0, created="2026-01-03", complexity="simple"),
            # P1 tier
            _issue(4, priority=1, created="2026-01-04", complexity="medium"),
            _issue(5, priority=1, created="2026-01-05", complexity="simple"),
            _issue(6, priority=1, created="2026-01-06", complexity="medium"),
        ]
        result, _ = reorder_within_tiers(issues, config)
        nums = [i["number"] for i in result]
        # P0: simple cluster (1,3) then medium (2)
        # P1: medium cluster (4,6) then simple (5)
        assert nums == [1, 3, 2, 4, 6, 5]


# ---------------------------------------------------------------------------
# Advise vs auto mode integration
# ---------------------------------------------------------------------------


class TestOrderAwareMode:
    """order_aware=false returns original order; true returns reordered."""

    @patch("kew.selector._gh_issue_list")
    def test_order_aware_false_original_order(self, mock_gh):
        """With order_aware=false, list_ready_issues returns today's order."""
        from kew.selector import list_ready_issues

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
                cache=CacheConfig(order_aware=False),
            ),
        )
        mock_gh.return_value = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        result = list_ready_issues(config=config)
        # order_aware=false → identical to today's priority+age sort
        assert [i["number"] for i in result] == [1, 2, 3]

    @patch("kew.selector._gh_issue_list")
    def test_order_aware_true_applies_reorder(self, mock_gh):
        """With order_aware=true, list_ready_issues returns reordered list."""
        from kew.selector import list_ready_issues

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
                cache=CacheConfig(order_aware=True),
            ),
        )
        mock_gh.return_value = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        result = list_ready_issues(config=config)
        # Reordered: simple cluster (1, 3) then medium (2)
        assert [i["number"] for i in result] == [1, 3, 2]

    @patch("kew.selector._gh_issue_list")
    def test_no_routing_no_reorder(self, mock_gh):
        """Without routing config, no reorder even with no cache config."""
        from kew.selector import list_ready_issues

        config = DispatchConfig()
        mock_gh.return_value = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
        ]
        result = list_ready_issues(config=config)
        assert [i["number"] for i in result] == [1, 2]

    @patch("kew.selector._gh_issue_list")
    def test_list_queue_issues_reorders_ready_partition(self, mock_gh):
        """list_queue_issues reorders the ready partition when order_aware=true."""
        from kew.selector import list_queue_issues

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
                cache=CacheConfig(order_aware=True),
            ),
        )
        mock_gh.return_value = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in ready] == [1, 3, 2]
        assert failed == []
        assert backlog == []


# ---------------------------------------------------------------------------
# Audit recording
# ---------------------------------------------------------------------------


class TestReorderAudit:
    """Ordering decisions written to the audit as attested facts."""

    def test_audit_payload_contains_decisions(self):
        """reorder_within_tiers returns audit decisions."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        _, decisions = reorder_within_tiers(issues, config)
        assert len(decisions) >= 1
        d = decisions[0]
        assert "original_order" in d
        assert "reordered" in d
        assert "tier" in d

    def test_audit_no_decisions_when_no_change(self):
        """No audit record when the order doesn't change."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig()
        issues = [
            _issue(1, priority=1, created="2026-01-01"),
            _issue(2, priority=1, created="2026-01-02"),
        ]
        _, decisions = reorder_within_tiers(issues, config)
        assert decisions == []

    def test_emit_reorder_audit_writes_to_store(self):
        """emit_reorder_audit calls audit.emit with the decision payload."""
        from kew.selector import emit_reorder_audit

        store = MagicMock()
        decisions = [
            {
                "tier": 1,
                "original_order": [1, 2, 3],
                "reordered": [1, 3, 2],
                "cluster_keys": {"1": "a", "2": "b", "3": "a"},
            }
        ]
        with patch("kew.audit.emit") as mock_emit:
            emit_reorder_audit(decisions, applied=True, store=store)
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs[0][1] == "cache_reorder"
            assert call_kwargs[1]["payload"]["applied"] is True

    def test_emit_reorder_audit_noop_without_store(self):
        """No audit write when store is None."""
        from kew.selector import emit_reorder_audit

        decisions = [{"tier": 1, "original_order": [1], "reordered": [1]}]
        with patch("kew.audit.emit") as mock_emit:
            emit_reorder_audit(decisions, applied=False, store=None)
            mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Advise-mode logging
# ---------------------------------------------------------------------------


class TestAdviseLogging:
    def test_advise_mode_logs_suggestion(self, caplog):
        """order_aware=false logs the suggested reorder."""
        from kew.selector import reorder_within_tiers

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        issues = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        with caplog.at_level(logging.INFO, logger="kew.selector"):
            reorder_within_tiers(issues, config)
        assert any("cache reorder" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Cluster key prediction
# ---------------------------------------------------------------------------


class TestPredictClusterKey:
    def test_same_complexity_same_key(self):
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
            ),
        )
        k1 = predict_cluster_key(config, "simple")
        k2 = predict_cluster_key(config, "simple")
        assert k1 == k2

    def test_different_complexity_different_key(self):
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            ),
        )
        k1 = predict_cluster_key(config, "simple")
        k2 = predict_cluster_key(config, "medium")
        assert k1 != k2

    def test_no_routing_all_same_key(self):
        from kew.selector import predict_cluster_key

        config = DispatchConfig()
        k1 = predict_cluster_key(config, "simple")
        k2 = predict_cluster_key(config, "medium")
        k3 = predict_cluster_key(config, "complex")
        assert k1 == k2 == k3

    def test_fallback_to_default_route(self):
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-opus-4-6"),
            ),
        )
        k1 = predict_cluster_key(config, "simple")
        k2 = predict_cluster_key(config, "complex")
        assert k1 == k2

    def test_string_shorthand_route(self):
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            routing=RoutingConfig(default="claude-code"),
        )
        key = predict_cluster_key(config, "medium")
        assert isinstance(key, str)
        assert len(key) > 0

    def test_model_only_entry_inherits_default_runner(self):
        """A RouteEntry with only model inherits runner from routing.default."""
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            routing=RoutingConfig(
                default="opencode",
                simple=RouteEntry(model="gemini-2.5-flash"),
            ),
        )
        key = predict_cluster_key(config, "simple")
        assert key.startswith("opencode|")

    def test_model_only_entry_inherits_agent_runner(self):
        """A RouteEntry with only model falls back to config.agent.runner."""
        from kew.selector import predict_cluster_key

        config = DispatchConfig(
            agent={"runner": "opencode", "model": "gemini-2.5-flash"},
            routing=RoutingConfig(
                default=RouteEntry(model="gemini-2.5-pro"),
                simple=RouteEntry(model="gemini-2.5-flash"),
            ),
        )
        key = predict_cluster_key(config, "simple")
        assert key.startswith("opencode|")


# ---------------------------------------------------------------------------
# Dispatch ordering (scheduler integration)
# ---------------------------------------------------------------------------


class TestDispatchOrdering:
    """Cache reorder is applied to the dispatch batch, not just display."""

    @patch("kew.scheduler.select_next_issue")
    def test_select_available_issues_applies_reorder(
        self, mock_select, tmp_path, monkeypatch
    ):
        """DispatchScheduler.select_available_issues reorders the batch."""
        from kew.scheduler import DispatchScheduler

        monkeypatch.setattr("kew.scheduler.project_root", lambda: tmp_path)
        config = DispatchConfig(
            logging={"dir": str(tmp_path)},
            routing=RoutingConfig(
                default="claude-code",
                simple=RouteEntry(runner="claude-code", model="claude-haiku-4-5"),
                medium=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
                cache=CacheConfig(order_aware=True),
            ),
        )
        batch = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
            _issue(3, priority=1, created="2026-01-03", complexity="simple"),
        ]
        mock_select.side_effect = batch + [None]

        scheduler = DispatchScheduler(config)
        selected = scheduler.select_available_issues(max_count=3)
        assert [i["number"] for i in selected] == [1, 3, 2]

    @patch("kew.scheduler.select_next_issue")
    def test_select_available_issues_no_reorder_without_routing(self, mock_select):
        """Without routing config, dispatch order is unchanged."""
        from kew.scheduler import DispatchScheduler

        config = DispatchConfig()
        batch = [
            _issue(1, priority=1, created="2026-01-01", complexity="simple"),
            _issue(2, priority=1, created="2026-01-02", complexity="medium"),
        ]
        mock_select.side_effect = batch + [None]

        scheduler = DispatchScheduler(config)
        selected = scheduler.select_available_issues(max_count=2)
        assert [i["number"] for i in selected] == [1, 2]
