"""Tests for the pricing audit module (pricing freshness loop, #269)."""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kew.run_store import RunRecord, SqliteRunStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pricing_audit.py"
_spec = importlib.util.spec_from_file_location("pricing_audit", SCRIPT)
assert _spec is not None and _spec.loader is not None
pricing_audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pricing_audit
_spec.loader.exec_module(pricing_audit)

AuditRun = pricing_audit.AuditRun
DriftEntry = pricing_audit.DriftEntry
diff_pricing = pricing_audit.diff_pricing
estimate_run_impact = pricing_audit.estimate_run_impact
find_missing_models = pricing_audit.find_missing_models
find_new_upstream_models = pricing_audit.find_new_upstream_models
load_acknowledged_entries = pricing_audit.load_acknowledged_entries
load_config_overrides = pricing_audit.load_config_overrides
load_recent_runs = pricing_audit.load_recent_runs
parse_litellm_snapshot = pricing_audit.parse_litellm_snapshot
render_report = pricing_audit.render_report

SAMPLE_SNAPSHOT = {
    "claude-sonnet-4-5": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
    "claude-opus-4-6": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
    },
    "claude-fable-5-20260609": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
    },
    "gpt-999-turbo": {
        "litellm_provider": "openai",
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 2e-06,
    },
    "claude-broken-entry": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": "not-a-number",
    },
    "claude-bool-entry": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": True,
        "output_cost_per_token": 5e-05,
    },
}


def _store_with_runs(tmp_path: Path, records: list[RunRecord]) -> str:
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        for record in records:
            store.save(record)
    return db_path


def _run(
    issue: int,
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    age_days: int = 0,
) -> RunRecord:
    return RunRecord(
        run_id=f"run-{issue}",
        issue_num=issue,
        branch="main",
        status="success",
        exit_code=0,
        started_at=datetime.now(UTC) - timedelta(days=age_days),
        model=model,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )


class TestParseLitellmSnapshot:
    def test_codex_uses_conservative_above_272k_tier(self):
        snapshot = {
            "gpt-5.6-sol": {
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 3e-5,
                "input_cost_per_token_above_272k_tokens": 1e-5,
                "output_cost_per_token_above_272k_tokens": 4.5e-5,
            }
        }
        upstream = parse_litellm_snapshot(snapshot)
        assert upstream["gpt-5.6-sol"] == (10.0, 45.0)
        assert diff_pricing({"gpt-5.6-sol": (10.0, 45.0)}, upstream) == []

    def test_codex_long_context_tier_drift_is_detected(self):
        snapshot = {
            "gpt-5.6-sol": {
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 3e-5,
                "input_cost_per_token_above_272k_tokens": 1.1e-5,
                "output_cost_per_token_above_272k_tokens": 4.5e-5,
            }
        }
        upstream = parse_litellm_snapshot(snapshot)
        assert len(diff_pricing({"gpt-5.6-sol": (10.0, 45.0)}, upstream)) == 1

    def test_normalizes_costs_to_per_million(self):
        pricing = parse_litellm_snapshot(SAMPLE_SNAPSHOT)
        assert pricing["claude-sonnet-4-5"] == (3.0, 15.0)
        assert pricing["claude-opus-4-6"] == (5.0, 25.0)

    def test_is_provider_agnostic(self):
        # Drift comparison is restricted to shipped keys, so keeping all
        # providers costs nothing and covers future non-Anthropic entries.
        pricing = parse_litellm_snapshot(SAMPLE_SNAPSHOT)
        assert pricing["gpt-999-turbo"] == (1.0, 2.0)

    def test_skips_entries_with_non_numeric_costs(self):
        pricing = parse_litellm_snapshot(SAMPLE_SNAPSHOT)
        assert "claude-broken-entry" not in pricing

    def test_skips_boolean_costs(self):
        # bool is an int subclass; True must not become $1,000,000 per 1M.
        pricing = parse_litellm_snapshot(SAMPLE_SNAPSHOT)
        assert "claude-bool-entry" not in pricing


class TestDiffPricing:
    def test_no_drift_for_matching_prices(self):
        shipped = {"claude-sonnet-4-5": (3.0, 15.0)}
        upstream = {"claude-sonnet-4-5": (3.0, 15.0)}
        assert diff_pricing(shipped, upstream) == []

    def test_detects_drift_on_exact_key(self):
        shipped = {"claude-opus-4-6": (15.0, 75.0)}
        upstream = {"claude-opus-4-6": (5.0, 25.0)}
        drifts = diff_pricing(shipped, upstream)
        assert len(drifts) == 1
        entry = drifts[0]
        assert entry.model == "claude-opus-4-6"
        assert entry.shipped == (15.0, 75.0)
        assert entry.upstream == (5.0, 25.0)

    def test_matches_dated_upstream_snapshot_keys(self):
        shipped = {"claude-fable-5": (9.0, 45.0)}
        upstream = {"claude-fable-5-20260609": (10.0, 50.0)}
        drifts = diff_pricing(shipped, upstream)
        assert len(drifts) == 1
        assert drifts[0].upstream == (10.0, 50.0)

    def test_dated_match_prefers_latest_snapshot(self):
        shipped = {"claude-fable-5": (9.0, 45.0)}
        upstream = {
            "claude-fable-5-20260101": (8.0, 40.0),
            "claude-fable-5-20260609": (10.0, 50.0),
        }
        assert diff_pricing(shipped, upstream)[0].upstream == (10.0, 50.0)

    def test_unmatched_key_is_not_drift(self):
        shipped = {"claude-opus-4-6": (15.0, 75.0)}
        upstream = {"claude-sonnet-4-6": (3.0, 15.0)}
        assert diff_pricing(shipped, upstream) == []

    def test_family_fallback_does_not_match_dated_member_keys(self):
        # "claude-opus-4" is a family fallback (other shipped keys extend it);
        # it must not be drift-compared against one dated family member.
        shipped = {
            "claude-opus-4": (15.0, 75.0),
            "claude-opus-4-6": (5.0, 25.0),
        }
        upstream = {"claude-opus-4-20250514": (5.0, 25.0)}
        assert diff_pricing(shipped, upstream) == []

    def test_family_fallback_still_drifts_on_exact_upstream_key(self):
        shipped = {
            "claude-opus-4": (15.0, 75.0),
            "claude-opus-4-6": (5.0, 25.0),
        }
        upstream = {"claude-opus-4": (5.0, 25.0)}
        drifts = diff_pricing(shipped, upstream)
        assert [d.model for d in drifts] == ["claude-opus-4"]

    def test_a_routable_model_extended_by_a_point_release_still_drifts(self):
        # #2531 review: adding claude-sonnet-5-5 must not turn claude-sonnet-5
        # into a family fallback whose dated upstream form is never compared.
        shipped = {
            "claude-sonnet-5": (2.0, 10.0),
            "claude-sonnet-5-5": (2.0, 10.0),
        }
        upstream = {"claude-sonnet-5-20261008": (3.0, 15.0)}
        drifts = diff_pricing(shipped, upstream)
        assert [d.model for d in drifts] == ["claude-sonnet-5"]

    def test_tolerates_float_noise(self):
        shipped = {"claude-sonnet-4-5": (3.0, 15.0)}
        upstream = {"claude-sonnet-4-5": (3.0000000001, 15.0)}
        assert diff_pricing(shipped, upstream) == []


class TestLoadRecentRuns:
    def test_reads_runs_written_by_run_store(self, tmp_path):
        db = _store_with_runs(tmp_path, [_run(1, "claude-opus-4-6", 1000, 100)])
        runs = load_recent_runs(db)
        assert runs == [AuditRun("claude-opus-4-6", 1000, 100)]

    def test_runs_outside_window_are_excluded(self, tmp_path):
        db = _store_with_runs(
            tmp_path, [_run(1, "claude-fable-9", 1000, 100, age_days=90)]
        )
        assert load_recent_runs(db, days=30) == []

    def test_opens_read_only(self, tmp_path):
        # The audit may point at a live dispatch.db: it must never write.
        db = _store_with_runs(tmp_path, [_run(1, "claude-opus-4-6", 1000, 100)])
        before = Path(db).read_bytes()
        load_recent_runs(db)
        assert Path(db).read_bytes() == before


class TestFindMissingModels:
    def test_in_use_model_with_no_pricing_is_missing(self):
        runs = [AuditRun("claude-fable-9", 1000, 100)]
        missing = find_missing_models(runs, shipped={"claude-opus-4": (15.0, 75.0)})
        assert missing == ["claude-fable-9"]

    def test_free_provider_models_are_not_missing(self):
        runs = [AuditRun("ollama/llama3", 1000, 100)]
        assert find_missing_models(runs, shipped={}) == []

    def test_priced_models_are_not_missing(self):
        runs = [AuditRun("claude-opus-4-6", 1000, 100)]
        missing = find_missing_models(runs, shipped={"claude-opus-4-6": (5.0, 25.0)})
        assert missing == []

    def test_an_id_extending_a_priced_id_is_missing(self):
        # No prefix ride (#2709): claude-opus-4-6 does not borrow claude-opus-4.
        runs = [AuditRun("claude-opus-4-6", 1000, 100)]
        missing = find_missing_models(runs, shipped={"claude-opus-4": (15.0, 75.0)})
        assert missing == ["claude-opus-4-6"]

    def test_blank_model_is_ignored(self):
        assert find_missing_models([AuditRun("", 1000, 100)], shipped={}) == []

    def test_config_overridden_models_are_not_missing(self):
        # Operator priced the model via [pricing] overrides — it is metered.
        runs = [AuditRun("my-custom-model", 1000, 100)]
        shipped = {"my-custom-model": (1.0, 2.0)}
        assert find_missing_models(runs, shipped=shipped) == []


class TestEstimateRunImpact:
    def test_recomputes_cost_delta_for_drifted_models(self):
        runs = [
            AuditRun("claude-opus-4-6", 1_000_000, 1_000_000),
            AuditRun("claude-opus-4-6", 1_000_000, 0),
        ]
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        impacts = estimate_run_impact(
            runs, [drift], shipped={"claude-opus-4-6": (15.0, 75.0)}
        )
        assert len(impacts) == 1
        impact = impacts[0]
        assert impact.model == "claude-opus-4-6"
        assert impact.run_count == 2
        # shipped: (15+75) + 15 = 105; upstream: (5+25) + 5 = 35
        assert impact.shipped_cost == 105.0
        assert impact.upstream_cost == 35.0

    def test_cache_tokens_weighted_at_discount_in_impact(self):
        # 1M cache-read tokens should cost 0.1x the input rate, not full input.
        runs = [
            AuditRun(
                "claude-opus-4-6",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=1_000_000,
            )
        ]
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(10.0, 50.0), upstream=(5.0, 25.0)
        )
        impacts = estimate_run_impact(
            runs, [drift], shipped={"claude-opus-4-6": (10.0, 50.0)}
        )
        assert len(impacts) == 1
        assert impacts[0].shipped_cost == 1.0  # 10.0 * 0.1
        assert impacts[0].upstream_cost == 0.5  # 5.0 * 0.1

    def test_codex_impact_uses_model_scoped_cache_reads_and_writes(self):
        runs = [
            AuditRun(
                "gpt-5.6-sol",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=1_000_000,
                cache_read_tokens=1_000_000,
            )
        ]
        drift = DriftEntry(
            model="gpt-5.6-sol", shipped=(10.0, 45.0), upstream=(8.0, 40.0)
        )
        impact = estimate_run_impact(
            runs, [drift], shipped={"gpt-5.6-sol": (10.0, 45.0)}
        )[0]
        assert impact.shipped_cost == 13.5  # 10 * (1.25 write + 0.10 read)
        assert impact.upstream_cost == 10.8  # 8 * (1.25 write + 0.10 read)

    def test_runs_on_other_models_are_excluded(self):
        runs = [AuditRun("claude-haiku-4-5", 1000, 100)]
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        shipped = {
            "claude-opus-4-6": (15.0, 75.0),
            "claude-haiku-4-5": (0.80, 4.0),
        }
        assert estimate_run_impact(runs, [drift], shipped=shipped) == []

    def test_override_governed_runs_do_not_land_on_drifted_family_key(self):
        # Operator overrode the specific model; estimate_cost metered the run
        # via that longer override key, so family-key drift doesn't touch it.
        runs = [AuditRun("claude-opus-4-6", 1_000_000, 0)]
        effective = {
            "claude-opus-4": (15.0, 75.0),  # shipped, drifted
            "claude-opus-4-6": (4.0, 20.0),  # [pricing] override
        }
        drift = DriftEntry("claude-opus-4", (15.0, 75.0), (5.0, 25.0))
        assert estimate_run_impact(runs, [drift], shipped=effective) == []

    def test_run_counted_only_under_longest_prefix_key(self):
        # When a family fallback and a specific key both drift, a run must be
        # attributed only to the key estimate_cost would meter it with.
        runs = [AuditRun("claude-opus-4-6", 1_000_000, 0)]
        shipped = {
            "claude-opus-4": (15.0, 75.0),
            "claude-opus-4-6": (15.0, 75.0),
        }
        drifts = [
            DriftEntry("claude-opus-4", (15.0, 75.0), (5.0, 25.0)),
            DriftEntry("claude-opus-4-6", (15.0, 75.0), (5.0, 25.0)),
        ]
        impacts = estimate_run_impact(runs, drifts, shipped=shipped)
        assert [i.model for i in impacts] == ["claude-opus-4-6"]
        assert impacts[0].run_count == 1


class TestRenderReport:
    def test_clean_report_says_no_drift(self):
        report = render_report(drifts=[], missing=[], impacts=[])
        assert "No pricing drift" in report

    def test_report_includes_drift_table_and_percent_delta(self):
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        report = render_report(drifts=[drift], missing=[], impacts=[])
        assert "claude-opus-4-6" in report
        assert "$15.00" in report
        assert "$5.00" in report
        assert "-66.7%" in report

    def test_report_lists_missing_models_first(self):
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        report = render_report(drifts=[drift], missing=["claude-fable-9"], impacts=[])
        assert report.index("claude-fable-9") < report.index("claude-opus-4-6")
        assert "$0" in report

    def test_empty_impact_message_reflects_run_data_presence(self):
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        without_db = render_report(
            drifts=[drift], missing=[], impacts=[], has_run_data=False
        )
        assert "No run-store data available" in without_db
        with_db = render_report(
            drifts=[drift], missing=[], impacts=[], has_run_data=True
        )
        assert "No drifted models appear in recent runs" in with_db

    def test_model_names_are_escaped_for_markdown_tables(self):
        drift = DriftEntry(model="weird|model", shipped=(1.0, 2.0), upstream=(3.0, 4.0))
        report = render_report(drifts=[drift], missing=[], impacts=[])
        assert "weird\\|model" in report


class TestLoadConfigOverrides:
    def test_reads_pricing_overrides_table(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.overrides]\n"my-model" = [5.0, 25.0]\n', encoding="utf-8"
        )
        assert load_config_overrides(config) == {"my-model": (5.0, 25.0)}

    def test_empty_when_no_pricing_section(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text('[github]\nrepo = "o/r"\n', encoding="utf-8")
        assert load_config_overrides(config) == {}


class TestCliEntrypoint:
    def _invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
        )

    def _snapshot_matching_shipped(self, tmp_path: Path) -> Path:
        from kew.pricing import catalog_pricing

        snapshot = {
            model: {
                "litellm_provider": "anthropic",
                "input_cost_per_token": inp / 1_000_000,
                "output_cost_per_token": out / 1_000_000,
            }
            for model, (inp, out) in catalog_pricing().items()
        }
        snap = tmp_path / "snap.json"
        snap.write_text(json.dumps(snapshot))
        return snap

    def test_exit_zero_when_clean(self, tmp_path):
        snap = self._snapshot_matching_shipped(tmp_path)
        out = tmp_path / "report.md"
        result = self._invoke("--snapshot", str(snap), "--output", str(out))
        assert result.returncode == 0, result.stderr
        assert "No pricing drift" in out.read_text()

    def test_exit_one_on_drift(self, tmp_path):
        snapshot = {
            "claude-opus-4-6": {
                "litellm_provider": "anthropic",
                "input_cost_per_token": 8e-06,
                "output_cost_per_token": 4e-05,
            }
        }
        snap = tmp_path / "snap.json"
        snap.write_text(json.dumps(snapshot))
        out = tmp_path / "report.md"
        result = self._invoke("--snapshot", str(snap), "--output", str(out))
        assert result.returncode == 1, result.stderr
        assert "claude-opus-4-6" in out.read_text()

    def test_exit_two_on_unreadable_snapshot(self, tmp_path):
        result = self._invoke(
            "--snapshot",
            str(tmp_path / "absent.json"),
            "--output",
            str(tmp_path / "report.md"),
        )
        assert result.returncode == 2

    def test_exit_two_not_one_on_unexpected_failure(self, tmp_path):
        # Exit 1 strictly means "drift report written" — the workflow files
        # an issue on 1, so internal failures must surface as 2.
        snap = self._snapshot_matching_shipped(tmp_path)
        bogus_db = tmp_path / "dispatch.db"
        bogus_db.write_text("this is not a sqlite database")
        result = self._invoke(
            "--snapshot",
            str(snap),
            "--output",
            str(tmp_path / "report.md"),
            "--run-store",
            str(bogus_db),
        )
        assert result.returncode == 2
        assert "pricing audit failed" in result.stderr

    def test_exit_one_on_new_upstream_model(self, tmp_path):
        # A novel upstream model absent from the model catalog → exit 1 + listed.
        snapshot = {
            "claude-x-9": {
                "litellm_provider": "anthropic",
                "mode": "chat",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
            }
        }
        snap = tmp_path / "snap.json"
        snap.write_text(json.dumps(snapshot))
        out = tmp_path / "report.md"
        result = self._invoke("--snapshot", str(snap), "--output", str(out))
        assert result.returncode == 1, result.stderr
        assert "claude-x-9" in out.read_text()

    def test_acknowledged_drift_suppressed_until_expiry(self, tmp_path):
        # A drift entry with a valid (future) acknowledgment must not appear.
        from kew.pricing import catalog_pricing

        snapshot = {
            **{
                model: {
                    "litellm_provider": "anthropic",
                    "input_cost_per_token": inp / 1_000_000,
                    "output_cost_per_token": out / 1_000_000,
                }
                for model, (inp, out) in catalog_pricing().items()
            },
            "claude-opus-4-6": {
                "litellm_provider": "anthropic",
                "input_cost_per_token": 8e-06,
                "output_cost_per_token": 4e-05,
            },
        }
        snap = tmp_path / "snap.json"
        snap.write_text(json.dumps(snapshot))
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-opus-4-6" = "2099-12-31"\n',
            encoding="utf-8",
        )
        out = tmp_path / "report.md"
        result = self._invoke(
            "--snapshot", str(snap), "--output", str(out), "--config", str(config)
        )
        assert result.returncode == 0, result.stderr
        assert "claude-opus-4-6" not in out.read_text()


class TestFindNewUpstreamModels:
    def test_novel_claude_model_is_reported(self):
        raw = {
            "claude-x-9": {
                "litellm_provider": "anthropic",
                "mode": "chat",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
            }
        }
        assert "claude-x-9" in find_new_upstream_models(raw, {})

    def test_model_covered_by_shipped_key_is_excluded(self):
        raw = {
            "claude-sonnet-5": {
                "mode": "chat",
                "input_cost_per_token": 3e-06,
                "output_cost_per_token": 1.5e-05,
            }
        }
        shipped = {"claude-sonnet-5": (3.0, 15.0)}
        assert find_new_upstream_models(raw, shipped) == []

    def test_dated_variant_of_shipped_model_is_excluded(self):
        # A dated snapshot of a shipped id is the shipped model, not a new one.
        raw = {
            "claude-sonnet-5-20251001": {
                "mode": "chat",
                "input_cost_per_token": 3e-06,
                "output_cost_per_token": 1.5e-05,
            }
        }
        shipped = {"claude-sonnet-5": (3.0, 15.0)}
        assert find_new_upstream_models(raw, shipped) == []

    def test_embedding_mode_is_filtered_out(self):
        raw = {
            "claude-x-9-embed": {
                "mode": "embedding",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 0.0,
            }
        }
        assert find_new_upstream_models(raw, {}) == []

    def test_non_claude_models_are_not_reported(self):
        raw = {
            "gpt-999": {
                "mode": "chat",
                "input_cost_per_token": 1e-06,
                "output_cost_per_token": 2e-06,
            }
        }
        assert find_new_upstream_models(raw, {}) == []

    def test_dated_variant_deduplicates_against_base_in_same_snapshot(self):
        # When both base and dated variant are new, report only the base.
        raw = {
            "claude-x-9": {
                "mode": "chat",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
            },
            "claude-x-9-20260724": {
                "mode": "chat",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
            },
        }
        result = find_new_upstream_models(raw, {})
        assert result == ["claude-x-9"]

    def test_no_mode_field_claude_model_is_included(self):
        # LiteLLM doesn't always set mode; claude-* without mode is treated as chat.
        raw = {
            "claude-x-9": {
                "litellm_provider": "anthropic",
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
            }
        }
        assert "claude-x-9" in find_new_upstream_models(raw, {})

    def test_entries_with_non_numeric_costs_excluded(self):
        raw = {
            "claude-x-9": {
                "input_cost_per_token": "not-a-number",
                "output_cost_per_token": 2.5e-05,
            }
        }
        assert find_new_upstream_models(raw, {}) == []

    def test_results_are_sorted(self):
        cost = {
            "mode": "chat",
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 2e-06,
        }
        raw = {"claude-z-1": cost, "claude-a-9": cost}
        result = find_new_upstream_models(raw, {})
        assert result == sorted(result)


class TestLoadAcknowledgedEntries:
    def test_valid_future_entry_is_included(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-sonnet-5" = "2099-12-31"\n',
            encoding="utf-8",
        )
        result = load_acknowledged_entries(config, today=date(2026, 7, 24))
        assert "claude-sonnet-5" in result

    def test_expired_entry_is_excluded(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-sonnet-5" = "2026-01-01"\n',
            encoding="utf-8",
        )
        result = load_acknowledged_entries(config, today=date(2026, 7, 24))
        assert "claude-sonnet-5" not in result

    def test_entry_valid_on_expiry_date_itself(self, tmp_path):
        # "until" is inclusive: the entry is still valid on the expiry date.
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-sonnet-5" = "2026-08-31"\n',
            encoding="utf-8",
        )
        result = load_acknowledged_entries(config, today=date(2026, 8, 31))
        assert "claude-sonnet-5" in result

    def test_entry_expired_day_after_expiry(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-sonnet-5" = "2026-08-31"\n',
            encoding="utf-8",
        )
        result = load_acknowledged_entries(config, today=date(2026, 9, 1))
        assert "claude-sonnet-5" not in result

    def test_no_acknowledged_section_returns_empty(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text('[github]\nrepo = "o/r"\n', encoding="utf-8")
        assert load_acknowledged_entries(config) == set()

    def test_invalid_date_format_is_ignored(self, tmp_path):
        config = tmp_path / "kew.toml"
        config.write_text(
            '[pricing.acknowledged]\n"claude-sonnet-5" = "not-a-date"\n',
            encoding="utf-8",
        )
        assert load_acknowledged_entries(config) == set()


class TestRenderReportNewUpstream:
    def test_new_upstream_section_appears_in_report(self):
        report = render_report(
            drifts=[],
            missing=[],
            impacts=[],
            new_upstream=["claude-x-9"],
        )
        assert "claude-x-9" in report
        assert "New upstream" in report

    def test_clean_report_unchanged_when_new_upstream_is_empty(self):
        report = render_report(drifts=[], missing=[], impacts=[], new_upstream=[])
        assert "No pricing drift" in report

    def test_clean_report_unchanged_when_new_upstream_is_none(self):
        report = render_report(drifts=[], missing=[], impacts=[])
        assert "No pricing drift" in report

    def test_new_upstream_section_ordered_before_drift_table(self):
        drift = DriftEntry(
            model="claude-opus-4-6", shipped=(15.0, 75.0), upstream=(5.0, 25.0)
        )
        report = render_report(
            drifts=[drift],
            missing=[],
            impacts=[],
            new_upstream=["claude-x-9"],
        )
        assert report.index("claude-x-9") < report.index("claude-opus-4-6")
