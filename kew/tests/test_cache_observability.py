"""Tests for Phase 1 cache observability: prefix_key, provider inference,
per-provider cache params, warm_prefixes oracle, estimate_route_cost,
schema v16 migration, and report cache section."""

from datetime import UTC, datetime, timedelta

import pytest

from kew.pricing import (
    ProviderCacheParams,
    compute_prefix_key,
    estimate_route_cost,
    get_provider_cache_params,
    infer_provider,
)
from kew.run_store import RunRecord, SqliteRunStore

# ---------------------------------------------------------------------------
# prefix_key stability and sensitivity
# ---------------------------------------------------------------------------


class TestPrefixKey:
    def test_same_inputs_same_key(self):
        a = compute_prefix_key(
            repo="owner/repo",
            runner="claude-code",
            model="claude-sonnet-4-6",
            provider="anthropic",
            repo_map_hash="abc123",
        )
        b = compute_prefix_key(
            repo="owner/repo",
            runner="claude-code",
            model="claude-sonnet-4-6",
            provider="anthropic",
            repo_map_hash="abc123",
        )
        assert a == b
        assert len(a) == 64  # SHA-256 hex

    def test_different_repo_map_different_key(self):
        base = dict(
            repo="owner/repo",
            runner="claude-code",
            model="claude-sonnet-4-6",
            provider="anthropic",
        )
        a = compute_prefix_key(**base, repo_map_hash="abc123")
        b = compute_prefix_key(**base, repo_map_hash="def456")
        assert a != b

    def test_different_template_version_different_key(self):
        base = dict(
            repo="owner/repo",
            runner="claude-code",
            model="claude-sonnet-4-6",
            provider="anthropic",
            repo_map_hash="abc",
        )
        a = compute_prefix_key(**base, template_version="1")
        b = compute_prefix_key(**base, template_version="2")
        assert a != b

    def test_different_model_different_key(self):
        base = dict(
            repo="owner/repo",
            runner="claude-code",
            provider="anthropic",
            repo_map_hash="abc",
        )
        a = compute_prefix_key(**base, model="claude-sonnet-4-6")
        b = compute_prefix_key(**base, model="claude-opus-4-6")
        assert a != b

    def test_different_provider_different_key(self):
        base = dict(
            repo="owner/repo",
            runner="claude-code",
            model="claude-sonnet-4-6",
            repo_map_hash="abc",
        )
        a = compute_prefix_key(**base, provider="anthropic")
        b = compute_prefix_key(**base, provider="openai")
        assert a != b


# ---------------------------------------------------------------------------
# infer_provider
# ---------------------------------------------------------------------------


class TestInferProvider:
    def test_claude_model(self):
        assert infer_provider("claude-sonnet-4-6") == "anthropic"

    def test_gpt_model(self):
        assert infer_provider("gpt-4o") == "openai"

    def test_o1_model(self):
        assert infer_provider("o1-preview") == "openai"

    def test_gemini_model(self):
        assert infer_provider("gemini-2.0-flash") == "google"

    def test_unknown_model_empty_runner(self):
        assert infer_provider("custom-model") == ""

    def test_unknown_model_anthropic_runner(self):
        assert infer_provider("custom-model", "claude-code") == "anthropic"

    def test_unknown_model_openai_runner(self):
        assert infer_provider("custom-model", "openai-runner") == "openai"


# ---------------------------------------------------------------------------
# Per-provider cache parameters
# ---------------------------------------------------------------------------


class TestProviderCacheParams:
    def test_anthropic_defaults(self):
        p = get_provider_cache_params("anthropic")
        assert p.caches is True
        assert p.read_mult == 0.10
        assert p.write_mult == 1.25
        assert p.ttl_seconds == 300

    def test_openai_defaults(self):
        p = get_provider_cache_params("openai")
        assert p.caches is True
        assert p.read_mult == 0.50
        assert p.write_mult == 1.0  # implicit — no write premium

    def test_google_defaults(self):
        p = get_provider_cache_params("google")
        assert p.caches is True
        assert p.read_mult == 0.25

    def test_unknown_provider_no_cache(self):
        p = get_provider_cache_params("z.ai")
        assert p.caches is False

    def test_empty_provider_no_cache(self):
        p = get_provider_cache_params("")
        assert p.caches is False

    def test_overrides_take_precedence(self):
        custom = ProviderCacheParams(
            caches=True,
            read_mult=0.05,
            write_mult=1.5,
            ttl_seconds=600,
            min_tokens=2048,
            style="explicit",
        )
        p = get_provider_cache_params("anthropic", overrides={"anthropic": custom})
        assert p.read_mult == 0.05


# ---------------------------------------------------------------------------
# estimate_route_cost
# ---------------------------------------------------------------------------


class TestEstimateRouteCost:
    def test_cold_anthropic(self):
        cost = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="anthropic",
            warm=False,
            est_prompt_tokens=100_000,
            est_output_tokens=10_000,
        )
        # cold: 100k * $3/M * 1.25 write + 10k * $15/M output
        assert cost == pytest.approx(0.525)

    def test_warm_anthropic(self):
        cost = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="anthropic",
            warm=True,
            est_prompt_tokens=100_000,
            est_output_tokens=10_000,
        )
        # warm: 100k * $3/M * 0.10 read + 10k * $15/M output
        assert cost == pytest.approx(0.18)

    def test_implicit_provider_no_write_premium(self):
        cost_cold = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="openai",
            warm=False,
            est_prompt_tokens=100_000,
            overrides={"claude-sonnet-4": (3.0, 15.0)},
        )
        # openai cold = write_mult 1.0, no premium
        assert cost_cold == pytest.approx(0.30)

    def test_unknown_provider_always_cold(self):
        cost_cold = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="unknown-provider",
            warm=False,
            est_prompt_tokens=100_000,
        )
        cost_warm = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="unknown-provider",
            warm=True,
            est_prompt_tokens=100_000,
        )
        # unknown: caches=False, no multiplier applied
        assert cost_cold == cost_warm
        assert cost_cold == pytest.approx(0.30)

    def test_below_min_tokens_treated_as_cold(self):
        cost = estimate_route_cost(
            model="claude-sonnet-4-6",
            provider="anthropic",
            warm=True,
            est_prompt_tokens=500,
            est_output_tokens=0,
        )
        # Below anthropic min_tokens=1024, no cache multiplier applied
        assert cost == pytest.approx(500 * 3.0 / 1_000_000)

    def test_free_model_zero(self):
        cost = estimate_route_cost(
            model="ollama/llama3",
            provider="",
            warm=True,
            est_prompt_tokens=1_000_000,
        )
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Schema v16 migration
# ---------------------------------------------------------------------------


class TestSchemaV16:
    def test_fresh_db_has_v16_columns(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "fresh.db")) as store:
            cols = {
                r[1] for r in store._conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            assert "prefix_key" in cols
            assert "provider" in cols

    def test_fresh_db_interactive_usage_has_v16_columns(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "fresh.db")) as store:
            cols = {
                r[1]
                for r in store._conn.execute(
                    "PRAGMA table_info(interactive_usage)"
                ).fetchall()
            }
            assert "prefix_key" in cols
            assert "provider" in cols

    def test_prefix_key_and_provider_round_trip(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "rt.db")) as store:
            rec = RunRecord(
                run_id="r-pk",
                issue_num=1,
                branch="agent/issue-1",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                prefix_key="deadbeef" * 8,
                provider="anthropic",
            )
            store.save(rec)
            got = store.get("r-pk")
            assert got is not None
            assert got.prefix_key == "deadbeef" * 8
            assert got.provider == "anthropic"

    def test_pre_v16_rows_read_as_none(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "v15.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE schema_version"
            " (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_version VALUES (1, 15)")
        conn.execute(
            "CREATE TABLE runs ("
            " run_id TEXT PRIMARY KEY, issue_num INTEGER NOT NULL,"
            " branch TEXT NOT NULL, status TEXT NOT NULL,"
            " exit_code INTEGER NOT NULL, started_at TEXT NOT NULL,"
            " finished_at TEXT, duration_seconds REAL NOT NULL DEFAULT 0.0,"
            " total_tokens INTEGER NOT NULL DEFAULT 0,"
            " input_tokens INTEGER NOT NULL DEFAULT 0,"
            " output_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_creation_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
            " estimated_cost_usd REAL NOT NULL DEFAULT 0.0,"
            " error_category TEXT NOT NULL DEFAULT '',"
            " attempt_number INTEGER NOT NULL DEFAULT 1,"
            " session_id TEXT, log_file TEXT, pr_url TEXT,"
            " runner_name TEXT NOT NULL DEFAULT '',"
            " model TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '',"
            " complexity TEXT NOT NULL DEFAULT '',"
            " pgid INTEGER,"
            " actor_type TEXT, actor_id TEXT, repo TEXT,"
            " endpoint TEXT, policy_hash TEXT,"
            " reported_model TEXT,"
            " pr_state TEXT, pr_merged_at TEXT,"
            " review_cycles INTEGER, diff_additions INTEGER,"
            " diff_deletions INTEGER, diff_files INTEGER)"
        )
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO runs (run_id, issue_num, branch, status,"
            " exit_code, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("r-old", 2, "agent/issue-2", "success", 0, now_iso),
        )
        conn.commit()
        conn.close()

        with SqliteRunStore(db_path) as store:
            got = store.get("r-old")
            assert got is not None
            assert got.prefix_key is None
            assert got.provider is None


# ---------------------------------------------------------------------------
# warm_prefixes oracle
# ---------------------------------------------------------------------------


class TestWarmPrefixes:
    def _save_run(
        self, store, run_id, provider, prefix_key, started_at, cache_read_tokens=100
    ):
        rec = RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="b",
            status="success",
            exit_code=0,
            started_at=started_at,
            cache_read_tokens=cache_read_tokens,
            prefix_key=prefix_key,
            provider=provider,
        )
        store.save(rec)

    def test_warm_within_ttl(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "warm.db")) as store:
            self._save_run(
                store, "r1", "anthropic", "key-a", now - timedelta(seconds=100)
            )
            result = store.warm_prefixes("anthropic", now)
            assert "key-a" in result

    def test_cold_outside_ttl(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "cold.db")) as store:
            self._save_run(
                store, "r1", "anthropic", "key-a", now - timedelta(seconds=400)
            )
            result = store.warm_prefixes("anthropic", now)
            assert "key-a" not in result

    def test_provider_isolation(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "iso.db")) as store:
            self._save_run(
                store, "r1", "anthropic", "key-a", now - timedelta(seconds=100)
            )
            self._save_run(store, "r2", "openai", "key-b", now - timedelta(seconds=100))
            anthropic_warm = store.warm_prefixes("anthropic", now)
            openai_warm = store.warm_prefixes("openai", now)
            assert "key-a" in anthropic_warm
            assert "key-b" not in anthropic_warm
            assert "key-b" in openai_warm
            assert "key-a" not in openai_warm

    def test_cache_creation_counts_as_warm(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "create.db")) as store:
            rec = RunRecord(
                run_id="r1",
                issue_num=1,
                branch="b",
                status="success",
                exit_code=0,
                started_at=now - timedelta(seconds=100),
                cache_read_tokens=0,
                cache_creation_tokens=50_000,
                prefix_key="key-cold-write",
                provider="anthropic",
            )
            store.save(rec)
            result = store.warm_prefixes("anthropic", now)
            assert "key-cold-write" in result

    def test_zero_cache_tokens_not_warm(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "zc.db")) as store:
            self._save_run(
                store,
                "r1",
                "anthropic",
                "key-a",
                now - timedelta(seconds=100),
                cache_read_tokens=0,
            )
            result = store.warm_prefixes("anthropic", now)
            assert len(result) == 0

    def test_null_prefix_excluded(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "null.db")) as store:
            rec = RunRecord(
                run_id="r1",
                issue_num=1,
                branch="b",
                status="success",
                exit_code=0,
                started_at=now - timedelta(seconds=100),
                cache_read_tokens=100,
                provider="anthropic",
                prefix_key=None,
            )
            store.save(rec)
            result = store.warm_prefixes("anthropic", now)
            assert len(result) == 0

    def test_unknown_provider_always_empty(self, tmp_path):
        now = datetime.now(UTC)
        with SqliteRunStore(str(tmp_path / "unk.db")) as store:
            self._save_run(store, "r1", "z.ai", "key-a", now - timedelta(seconds=10))
            result = store.warm_prefixes("z.ai", now)
            assert len(result) == 0


# ---------------------------------------------------------------------------
# Report cache section
# ---------------------------------------------------------------------------


class TestReportCacheSection:
    def _make_record(self, **kw):
        defaults = dict(
            run_id="r1",
            issue_num=1,
            branch="b",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            input_tokens=100_000,
            output_tokens=10_000,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            estimated_cost_usd=0.0,
            runner_name="claude-code",
            model="claude-sonnet-4-6",
            provider="anthropic",
        )
        defaults.update(kw)
        return RunRecord(**defaults)

    def test_no_cache_data_returns_none(self):
        from kew.report import _compute_cache_stats

        records = [self._make_record()]
        assert _compute_cache_stats(records) is None

    def test_cache_stats_with_read_tokens(self):
        from kew.report import _compute_cache_stats

        records = [
            self._make_record(
                input_tokens=2_000,
                cache_read_tokens=80_000,
                cache_creation_tokens=0,
                estimated_cost_usd=0.174,  # warm price
            ),
        ]
        stats = _compute_cache_stats(records)
        assert stats is not None
        assert stats.saved_usd > 0
        group_key = "claude-code/claude-sonnet-4-6/anthropic"
        assert group_key in stats.by_group
        g = stats.by_group[group_key]
        assert g.cache_read_tokens == 80_000
        assert g.uncached_input_tokens == 2_000
        assert g.hit_ratio > 0

    def test_cache_section_in_rendered_report(self):
        from kew.config import BudgetConfig
        from kew.report import render_report

        records = [
            self._make_record(
                input_tokens=2_000,
                cache_read_tokens=50_000,
                estimated_cost_usd=0.10,
            ),
        ]
        report = render_report(
            records,
            repo="owner/repo",
            days=7,
            now=datetime.now(UTC),
            budget=BudgetConfig(),
            pr_states={},
        )
        assert "## Cache economics" in report
        assert "Cache saved" in report

    def test_leverage_banner_in_report(self):
        from kew.config import BudgetConfig
        from kew.report import render_report

        records = [
            self._make_record(
                input_tokens=2_000,
                cache_read_tokens=50_000,
                estimated_cost_usd=0.10,
            ),
        ]
        report = render_report(
            records,
            repo="owner/repo",
            days=7,
            now=datetime.now(UTC),
            budget=BudgetConfig(),
            pr_states={},
        )
        assert "Cache saved you" in report

    def test_no_cache_section_when_all_cold(self):
        from kew.config import BudgetConfig
        from kew.report import render_report

        records = [self._make_record()]
        report = render_report(
            records,
            repo="owner/repo",
            days=7,
            now=datetime.now(UTC),
            budget=BudgetConfig(),
            pr_states={},
        )
        assert "## Cache economics" not in report
