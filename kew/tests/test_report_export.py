"""Tests for kew.report_export — the `kew report export` evidence pack (V1)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from test_liveness_owner import _install_dispatch_owner
from test_liveness_owner import process_table as process_table

from kew.config import DispatchConfig
from kew.outcomes import SyncResult
from kew.report_export import (
    build_evidence_pack,
    pack_to_dict,
    render_evidence_pack_html,
    render_evidence_pack_json,
)
from kew.run_store import RunRecord, SqliteRunStore
from kew.tui.palette import LIGHT

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _report_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy export fixtures focused on pack behavior, not identity."""
    monkeypatch.setattr("kew.report.project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "kew.report.open_managed_store",
        lambda **kwargs: SqliteRunStore(
            str(kwargs["db_path"]),
            read_only=bool(kwargs["read_only"]),
            check_same_thread=bool(kwargs["check_same_thread"]),
        ),
    )


def _record(
    run_id: str,
    issue_num: int,
    *,
    status: str = "success",
    cost: float = 1.0,
    title: str = "",
    pr_url: str | None = None,
    pr_state: str | None = None,
    run_kind: str = "dispatch",
    item_id: str | None = None,
    started_at: datetime | None = None,
    model: str = "",
    complexity: str = "",
    review_cycles: int | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        branch=f"agent/issue-{issue_num}",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "success" else 1,
        started_at=started_at or (_NOW - timedelta(days=1)),
        estimated_cost_usd=cost,
        title=title,
        pr_url=pr_url,
        pr_state=pr_state,
        run_kind=run_kind,
        model=model,
        complexity=complexity,
        review_cycles=review_cycles,
    )


def _snapshot(epic_ref: str, node_ids: list[str]) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": epic_ref,
        "signal": "progressing",
        "counts": {},
        "frontier": [],
        "nodes": [{"id": nid, "title": f"issue {nid}"} for nid in node_ids],
        "edges": [],
    }


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.project.repo = "acme/widgets"
    return config


def _seed(config: DispatchConfig, records, *, ticks=None) -> None:
    db_dir = Path(config.logging.dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
        for r in records:
            store.save(r)
        for epic_ref, node_ids in (ticks or {}).items():
            store.append_epic_tick(
                epic_ref=epic_ref,
                tick_ts=_NOW.isoformat(),
                snapshot=_snapshot(epic_ref, node_ids),
                actions={"schema": 1, "dispatched": [], "merged": [], "held": []},
                snapshot_hash=f"h-{epic_ref}",
            )


def _seed_audit(config: DispatchConfig, events: list[tuple[str, str]]) -> None:
    """Append (event, timestamp) audit rows so a real hash chain exists."""
    db_dir = Path(config.logging.dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
        for event, ts in events:
            store.append_audit_event(timestamp=ts, event=event, repo="acme/widgets")


def _seed_posture(config: DispatchConfig, run_id: str) -> None:
    db_dir = Path(config.logging.dir)
    with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
        store.append_audit_event(
            timestamp="2026-07-19T01:00:00Z",
            event="sandbox_resolved",
            run_id=run_id,
            repo="acme/widgets",
            payload={
                "posture": "native",
                "filesystem_boundary": "runner-native-workspace-write",
                "network_boundary": "not-applicable",
                "credential_names": [],
                "command_guard": "not-applicable",
                "admission": "policy",
                "reason": None,
            },
        )


def _chain_head(config: DispatchConfig) -> tuple[int, str]:
    """Return (count, head_hash) for the seeded chain."""
    from kew.audit_chain import verify_chain

    db_dir = Path(config.logging.dir)
    with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
        rows = store.read_chain_rows()
    result = verify_chain(rows)
    assert result.head_hash is not None
    return result.count, result.head_hash


def _build(config: DispatchConfig, **kw):
    with patch("kew.report_export.sync_outcomes", return_value=SyncResult()):
        return build_evidence_pack(
            config=config, since=_NOW - timedelta(days=7), now=_NOW, **kw
        )


class TestBuildEvidencePack:
    @pytest.mark.parametrize("failures", [0, 2])
    @pytest.mark.parametrize("count_only", [False, True])
    def test_sync_metadata_accepts_result_or_failure_count(
        self, tmp_path, failures, count_only
    ):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, cost=1.0)])
        sync = failures if count_only else SyncResult(synced=3, query_failures=failures)
        with patch("kew.report_export.sync_outcomes", return_value=sync):
            pack = build_evidence_pack(
                config=config, since=_NOW - timedelta(days=7), now=_NOW
            )
        html = render_evidence_pack_html(pack)
        island = html.split('id="kew-evidence-data">')[1].split("</script>")[0]
        for content in (render_evidence_pack_json(pack), island):
            assert json.loads(content)["outcome_sync"] == {
                "query_failures": failures,
                "stale": failures > 0,
            }
        assert ("Outcome data is stale" in html) == (failures > 0)

    def test_missing_db_yields_empty_pack(self, tmp_path):
        pack = _build(_config(tmp_path))
        assert pack.total_spend == 0.0
        assert pack.ledgers == []
        assert pack.run_kinds == []
        assert [w.window_days for w in pack.windows] == [1, 7]
        assert pack.scorecard.rows == []

    def test_economics_totals_and_cost_per_merged_pr(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, cost=6.0, pr_url="u1", pr_state="MERGED"),
                _record("r2", 2, cost=2.0, pr_url="u2", pr_state="OPEN"),
            ],
        )
        pack = _build(config)
        assert pack.total_spend == 8.0
        assert pack.prs_opened == 2
        assert pack.prs_merged == 1
        assert pack.cost_per_merged_pr == 8.0

    def test_run_kind_breakdown(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, cost=1.0, run_kind="dispatch"),
                _record("r2", 2, cost=5.0, run_kind="review"),
                _record("r3", 3, cost=2.0, run_kind="review", status="failure"),
            ],
        )
        pack = _build(config)
        kinds = {s.run_kind: s for s in pack.run_kinds}
        assert kinds["dispatch"].runs == 1
        assert kinds["review"].runs == 2
        assert kinds["review"].cost_usd == 7.0
        assert kinds["review"].success == 1
        # dispatch sorts before review
        assert [s.run_kind for s in pack.run_kinds] == ["dispatch", "review"]

    def test_export_includes_the_recorded_posture_per_run(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1)])
        _seed_posture(config, "r1")

        data = json.loads(render_evidence_pack_json(_build(config)))

        assert data["epics"][0]["items"][0].get("postures") == [
            {
                "run_id": "r1",
                "posture": "native",
                "filesystem_boundary": "runner-native-workspace-write",
                "network_boundary": "not-applicable",
                "credential_names": [],
                "command_guard": "not-applicable",
                "admission": "policy",
                "reason": None,
            }
        ]
        html = render_evidence_pack_html(_build(config))
        assert "runner-native-workspace-write" in html
        assert "command_guard=not-applicable" in html

    def test_outcome_ledger_grouped_per_epic(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, cost=3.0, item_id="1", title="one"),
                _record("r1b", 1, cost=1.0, item_id="1", title="one"),
                _record("r2", 2, cost=2.0, item_id="2", title="two"),
                _record("r9", 9, cost=5.0, item_id="9", title="loner"),
            ],
            ticks={"alpha": ["1", "2"]},
        )
        pack = _build(config)
        by_ref = {led.epic_ref: led for led in pack.ledgers}
        assert set(by_ref) == {"alpha", "(unassigned)"}
        alpha = by_ref["alpha"]
        assert {r.issue_num for r in alpha.rows} == {1, 2}
        # issue 1 had two runs; costs aggregate
        row1 = next(r for r in alpha.rows if r.issue_num == 1)
        assert row1.run_count == 2
        assert row1.cost_usd == 4.0
        assert by_ref["(unassigned)"].rows[0].issue_num == 9

    def test_unassigned_bucket_sorts_last(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, item_id="1"),
                _record("r9", 9, item_id="9"),
            ],
            ticks={"zeta": ["1"]},
        )
        pack = _build(config)
        assert pack.ledgers[-1].epic_ref == "(unassigned)"


@pytest.mark.parametrize("format", ["json", "html"])
def test_direct_dispatch_basis_survives_ledger_export(
    tmp_path, monkeypatch, process_table, format
):
    from datetime import timedelta

    from kew import dispatch_core, sandbox
    from kew.runners import get_runner
    from kew.tracking import DispatchResult

    _install_dispatch_owner(monkeypatch, process_table)
    config = _config(tmp_path)
    config.retry.max_attempts = 1
    config.sandbox.enabled = True
    Path(config.logging.dir).mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-credential")
    monkeypatch.setattr(dispatch_core, "_check_existing_pr", lambda *a: (False, None))
    monkeypatch.setattr(
        dispatch_core, "require_publication_launch_credentials", lambda *a, **k: None
    )
    monkeypatch.setattr(
        dispatch_core,
        "_require_contained_image_id",
        lambda *a, **k: "sha256:" + "f" * 64,
    )
    monkeypatch.setattr(
        "kew.dispatch_sandbox.build_agent_container_argv",
        lambda *a, **k: ["docker", "run", "--name", "direct-worker"],
    )
    launch = MagicMock(
        side_effect=lambda **k: DispatchResult(
            exit_code=1,
            issue_num=1945,
            log_file=k["log_file_path"],
            error_category="permanent",
        )
    )
    monkeypatch.setattr(dispatch_core, "run_agent", launch)
    # Contained dispatch persists the immutable container ID before run_agent.
    create = MagicMock(return_value="c" * 64)
    monkeypatch.setattr(sandbox, "create_registered_container", create)
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        ctx = MagicMock(
            config=config,
            store=store,
            run_id="direct-1945",
            issue_num=1945,
            item_id="1945",
        )
        dispatch_core._emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="docker",
            image="worker:pinned",
            egress_route="direct",
            egress_verification="sidecar-certified",
        )
        result = dispatch_core.run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1945,
            store=store,
            runner=get_runner("claude-code"),
            model="claude-sonnet-4-6",
            run_id="direct-1945",
            validate_continuation=False,
            contained_image="worker:pinned",
            sandbox_posture="sandboxed",
        )
        [event] = store.list_audit_events(event="sandbox_resolved")
        assert (
            event.payload["egress_verification"]
            == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
        )
        assert event.payload["egress_verification_basis"] == "operator-asserted"
        row = store.get(result.run_id)
        assert row is not None
        assert row.container_id == "c" * 64
    launch.assert_called_once()
    create.assert_called_once()
    assert launch.call_args.kwargs["contained"].container_id == "c" * 64
    now = datetime.now(UTC)
    with patch("kew.report_export.sync_outcomes", return_value=SyncResult()):
        pack = build_evidence_pack(
            config=config, since=now - timedelta(days=1), now=now
        )
    if format == "json":
        data = json.loads(render_evidence_pack_json(pack))
        posture = data["epics"][0]["items"][0]["postures"][0]
        assert posture.get("egress_verification_basis") == "operator-asserted"
    else:
        assert (
            "egress_verification_basis=operator-asserted"
            in render_evidence_pack_html(pack)
        )


def test_historical_egress_basis_remains_missing_in_exports(tmp_path):
    config = _config(tmp_path)
    _seed(config, [_record("historical", 1945)])
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        store.append_audit_event(
            timestamp=_NOW.isoformat(),
            event="sandbox_resolved",
            run_id="historical",
            payload={
                "posture": "sandboxed",
                "network_boundary": "operator-managed-unverified",
                "egress_verification": "operator-managed-unverified",
            },
        )
    pack = _build(config)
    data = json.loads(render_evidence_pack_json(pack))
    posture = data["epics"][0]["items"][0]["postures"][0]
    assert posture.get("egress_verification_basis") is None
    html = render_evidence_pack_html(pack)
    assert "operator-asserted" not in html
    assert "egress_verification_basis=not recorded" in html


class TestAttestation:
    def test_empty_db_yields_empty_attestation(self, tmp_path):
        att = _build(_config(tmp_path)).attestation
        assert att.events == 0
        assert att.verified is True
        assert att.head_hash is None
        assert att.anchors_present is False
        assert (att.first_id, att.last_id) == (None, None)

    def test_chain_head_and_range_embedded(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(
            config,
            [
                ("dispatch.start", "2026-07-19T00:00:00Z"),
                ("dispatch.completed", "2026-07-19T00:01:00Z"),
                ("approve", "2026-07-19T00:02:00Z"),
            ],
        )
        count, head = _chain_head(config)
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.events == count == 3
        assert att.chain_ok is True
        assert att.verified is True
        assert att.head_hash == head
        assert (att.first_id, att.last_id) == (1, 3)
        assert att.anchors_present is False

    def test_matching_anchor_recorded(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(
            config,
            [
                ("dispatch.start", "2026-07-19T00:00:00Z"),
                ("dispatch.completed", "2026-07-19T00:01:00Z"),
            ],
        )
        count, head = _chain_head(config)
        anchor = f'{{"id":2,"entry_hash":"{head}","count":{count},"timestamp":"t"}}'
        (tmp_path / "audit-anchors.jsonl").write_text(anchor + "\n")
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.anchors_present is True
        assert att.anchors_total == 1
        assert att.anchors_matched == 1
        assert att.anchors_ok is True
        assert att.verified is True

    def test_tampered_anchor_flags_gap(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(config, [("dispatch.start", "2026-07-19T00:00:00Z")])
        bad = '{"id":1,"entry_hash":"' + "0" * 64 + '","count":1,"timestamp":"t"}'
        (tmp_path / "audit-anchors.jsonl").write_text(bad + "\n")
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.anchors_mismatched == [1]
        assert att.anchors_ok is False
        assert att.verified is False

    def test_rechained_anchor_history_flags_attestation(self, tmp_path):
        # #912: a broken anchor chain (re-chained history) surfaces in the pack.
        from kew.audit_chain import Anchor, format_anchor

        config = _config(tmp_path)
        _seed_audit(config, [("dispatch.start", "2026-07-19T00:00:00Z")])
        _count, head = _chain_head(config)
        # Two anchors whose continuity is broken (a2.prev does not bind a1).
        a1 = Anchor(id=1, entry_hash=head, count=1, timestamp="t", prev="0" * 64)
        a2 = Anchor(id=1, entry_hash=head, count=1, timestamp="t", prev="d" * 64)
        (tmp_path / "audit-anchors.jsonl").write_text(
            format_anchor(a1) + "\n" + format_anchor(a2) + "\n"
        )
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.anchor_chain_ok is False
        assert att.anchor_chain_broken_at == 1
        assert att.verified is False
        data = json.loads(render_evidence_pack_json(_build(config, repo_dir=tmp_path)))
        assert data["attestation"]["anchors"]["chain_ok"] is False
        assert "RE-CHAINED" in render_evidence_pack_html(
            _build(config, repo_dir=tmp_path)
        )

    def test_pack_reports_how_many_anchor_links_were_verified(self, tmp_path):
        # #1122: "chain intact" over a legacy anchor file verified zero links —
        # the pack must say so rather than imply the defence ran.
        from kew.audit_chain import Anchor, format_anchor

        config = _config(tmp_path)
        _seed_audit(config, [("dispatch.start", "2026-07-19T00:00:00Z")])
        _count, head = _chain_head(config)
        legacy = Anchor(id=1, entry_hash=head, count=1, timestamp="t")
        (tmp_path / "audit-anchors.jsonl").write_text(format_anchor(legacy) + "\n")
        pack = _build(config, repo_dir=tmp_path)
        assert pack.attestation.anchor_chain_checked == 0
        data = json.loads(render_evidence_pack_json(pack))
        assert data["attestation"]["anchors"]["chain_links_verified"] == 0
        assert "chain intact (0 link(s) verified)" in render_evidence_pack_html(pack)

    def test_count_shrink_anchor_flags_attestation(self, tmp_path):
        # #912: latest anchor count exceeds the live chain → count-shrink fail.
        config = _config(tmp_path)
        _seed_audit(config, [("dispatch.start", "2026-07-19T00:00:00Z")])
        _count, head = _chain_head(config)
        anchor = f'{{"id":1,"entry_hash":"{head}","count":50,"timestamp":"t"}}'
        (tmp_path / "audit-anchors.jsonl").write_text(anchor + "\n")
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.anchors_count_shrink is True
        assert att.anchors_ok is False and att.verified is False

    def test_json_twin_carries_attestation(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(config, [("dispatch.start", "2026-07-19T00:00:00Z")])
        count, head = _chain_head(config)
        data = json.loads(render_evidence_pack_json(_build(config, repo_dir=tmp_path)))
        att = data["attestation"]
        assert att["events"] == count
        assert att["head_hash"] == head
        assert att["verified"] is True
        assert att["verified_range"] == {"first_id": 1, "last_id": 1}
        assert att["anchors"]["present"] is False

    def test_html_renders_attestation_with_head_hash(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(
            config,
            [
                ("dispatch.start", "2026-07-19T00:00:00Z"),
                ("approve", "2026-07-19T00:01:00Z"),
            ],
        )
        _count, head = _chain_head(config)
        out = render_evidence_pack_html(_build(config, repo_dir=tmp_path))
        assert "Audit attestation" in out
        assert head in out

    def test_broken_chain_flags_html_and_json(self, tmp_path):
        config = _config(tmp_path)
        _seed_audit(
            config,
            [
                ("dispatch.start", "2026-07-19T00:00:00Z"),
                ("dispatch.completed", "2026-07-19T00:01:00Z"),
            ],
        )
        # Corrupt the genesis link so the chain verifies as broken at event #1.
        db_dir = Path(config.logging.dir)
        with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
            store._conn.execute(
                "UPDATE audit_events SET prev_hash = ? WHERE id = 1", ("f" * 64,)
            )
            store._conn.commit()
        att = _build(config, repo_dir=tmp_path).attestation
        assert att.chain_ok is False
        assert att.broken_at_id == 1
        assert att.verified is False
        # HTML surfaces the break (int broken_at_id rendered via _esc).
        out = render_evidence_pack_html(_build(config, repo_dir=tmp_path))
        assert "Chain breaks at event #1." in out
        # JSON twin carries the broken verdict.
        data = json.loads(render_evidence_pack_json(_build(config, repo_dir=tmp_path)))
        assert data["attestation"]["verified"] is False


class TestModelScorecard:
    """V3 — scorecard section reuses service.report.model_scorecard data."""

    def _seed_models(self, config: DispatchConfig) -> None:
        _seed(
            config,
            [
                _record(
                    "r1",
                    1,
                    model="anthropic/opus",
                    complexity="medium",
                    cost=6.0,
                    pr_url="u1",
                    pr_state="MERGED",
                    review_cycles=2,
                ),
                _record(
                    "r2",
                    2,
                    model="anthropic/opus",
                    complexity="simple",
                    cost=2.0,
                    pr_url="u2",
                    pr_state="OPEN",
                ),
                _record("r3", 3, model="local/qwen", complexity="medium", cost=1.0),
            ],
        )

    def test_scorecard_per_model_with_complexity_drilldown(self, tmp_path):
        config = _config(tmp_path)
        self._seed_models(config)
        pack = _build(config)
        rows = {r.model: r for r in pack.scorecard.rows}
        assert pack.scorecard.days == 7
        assert rows["anthropic/opus"].runs == 2
        assert rows["anthropic/opus"].opened == 2
        assert rows["anthropic/opus"].merged == 1
        assert rows["anthropic/opus"].spend == 8.0
        assert {c.complexity for c in rows["anthropic/opus"].by_complexity} == {
            "simple",
            "medium",
        }
        assert rows["local/qwen"].runs == 1
        assert rows["local/qwen"].opened == 0

    def test_json_twin_includes_scorecard(self, tmp_path):
        config = _config(tmp_path)
        self._seed_models(config)
        pack = _build(config)
        data = json.loads(render_evidence_pack_json(pack))
        sc = data["model_scorecard"]
        assert sc["days"] == 7
        opus = next(r for r in sc["rows"] if r["model"] == "anthropic/opus")
        assert opus["runs"] == 2
        assert opus["merge_rate"] == 0.5
        assert opus["cost_per_merged_pr"] == 8.0
        assert opus["reviews_avg"] == 2.0
        assert any(c["complexity"] == "medium" for c in opus["by_complexity"])
        _assert_empty_model_outcomes(sc)

    def test_html_includes_scorecard_section(self, tmp_path):
        config = _config(tmp_path)
        self._seed_models(config)
        pack = _build(config)
        out = render_evidence_pack_html(pack)
        assert "Model scorecard" in out
        assert "anthropic/opus" in out
        # complexity drill-down row rendered
        assert "medium" in out


class TestRenderers:
    def _pack(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record(
                    "r1",
                    1,
                    cost=6.0,
                    title="Fix bug",
                    pr_url="https://github.com/acme/widgets/pull/1",
                    pr_state="MERGED",
                    item_id="1",
                ),
                _record("r2", 2, cost=5.0, run_kind="review", item_id="2"),
            ],
            ticks={"alpha": ["1", "2"]},
        )
        return _build(config)

    def test_json_twin_is_machine_readable(self, tmp_path):
        pack = self._pack(tmp_path)
        data = json.loads(render_evidence_pack_json(pack))
        assert data["kind"] == "kew-evidence-pack"
        assert data["repo"] == "acme/widgets"
        assert data["economics"]["total_spend"] == 11.0
        assert data["economics"]["prs_merged"] == 1
        assert any(rk["run_kind"] == "review" for rk in data["run_kinds"])
        assert data["epics"][0]["epic_ref"] == "alpha"
        assert {i["issue_num"] for i in data["epics"][0]["items"]} == {1, 2}

    def test_html_is_self_contained_no_cdn(self, tmp_path):
        pack = self._pack(tmp_path)
        out = render_evidence_pack_html(pack)
        assert out.startswith("<!DOCTYPE html>")
        assert "<style>" in out and "</style>" in out
        assert "http://" not in out.replace("http://www.w3.org", "")
        assert "https://" not in out.replace(
            "https://github.com/acme/widgets/pull/1", ""
        )
        assert "@media print" in out
        assert "Economics summary" in out
        assert "Outcome ledger" in out
        assert "Run-kind breakdown" in out
        assert "Fix bug" in out

    def test_html_escapes_titles(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, title="<script>alert(1)</script>")])
        pack = _build(config)
        out = render_evidence_pack_html(pack)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out

    def test_html_embeds_data_island_without_breakout(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, title="</script><b>x</b>")])
        pack = _build(config)
        out = render_evidence_pack_html(pack)
        # The embedded JSON must not close the script tag prematurely.
        island = out.split('id="kew-evidence-data">', 1)[1]
        assert "</script>" in island  # the real closing tag exists
        assert island.count("</script>") == 1

    def test_pack_to_dict_round_trips_json(self, tmp_path):
        pack = self._pack(tmp_path)
        assert json.loads(json.dumps(pack_to_dict(pack))) == pack_to_dict(pack)


class TestSchemaContract:
    """Evidence pack schema contract — additive-only, tolerant (see plan doc)."""

    SCHEMA_1_REQUIRED_ATTESTATION = frozenset(
        {
            "verified",
            "events",
            "chain_ok",
            "broken_at_id",
            "head_hash",
            "verified_range",
            "anchors",
        }
    )
    SCHEMA_1_REQUIRED_ANCHORS = frozenset(
        {
            "present",
            "ok",
            "total",
            "matched",
            "mismatched",
            "absent",
            "lagging",
        }
    )

    @staticmethod
    def _pack(tmp_path: Path):
        return TestRenderers()._pack(tmp_path)

    def test_schema_1_attestation_required_keys_present(self, tmp_path):
        data = pack_to_dict(self._pack(tmp_path))
        att = data["attestation"]
        missing = self.SCHEMA_1_REQUIRED_ATTESTATION - att.keys()
        assert not missing, f"Schema 1 attestation missing required keys: {missing}"

    def test_schema_1_anchors_required_keys_present(self, tmp_path):
        data = pack_to_dict(self._pack(tmp_path))
        anchors = data["attestation"]["anchors"]
        missing = self.SCHEMA_1_REQUIRED_ANCHORS - anchors.keys()
        assert not missing, f"Schema 1 anchors missing required keys: {missing}"


class TestUnattributedEconomics:
    """B4 — unattributed-model bucket in economics section, no UserWarning."""

    def test_unattributed_runs_counted(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, cost=2.0, model="claude-opus-4-8"),
                _record("r2", 2, cost=1.5, model=""),
                _record("r3", 3, cost=0.5, model=""),
            ],
        )
        pack = _build(config)
        assert pack.unattributed_runs == 2

    def test_unattributed_spend_summed(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record("r1", 1, cost=2.0, model="claude-opus-4-8"),
                _record("r2", 2, cost=1.5, model=""),
                _record("r3", 3, cost=0.5, model=""),
            ],
        )
        pack = _build(config)
        assert abs(pack.unattributed_spend - 2.0) < 1e-9

    def test_json_twin_includes_unattributed(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, cost=1.0, model="")])
        pack = _build(config)
        data = json.loads(render_evidence_pack_json(pack))
        assert "unattributed_runs" in data["economics"]
        assert "unattributed_spend" in data["economics"]
        assert data["economics"]["unattributed_runs"] == 1
        assert abs(data["economics"]["unattributed_spend"] - 1.0) < 1e-6

    def test_html_shows_unattributed_bucket(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, cost=1.0, model="")])
        pack = _build(config)
        out = render_evidence_pack_html(pack)
        assert "unattributed" in out.lower()

    def test_zero_unattributed_when_all_models_recorded(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, cost=1.0, model="claude-sonnet-4-6")])
        pack = _build(config)
        assert pack.unattributed_runs == 0
        assert pack.unattributed_spend == 0.0


class TestBranding:
    """The evidence pack always uses kew branding."""

    def _pack(self, tmp_path):
        config = _config(tmp_path)
        _seed(config, [_record("r1", 1, title="Fix bug", cost=1.0)])
        return _build(config)

    def test_default_logo_inlined_as_data_uri(self, tmp_path):
        pack = self._pack(tmp_path)
        # The shipped favicon is reused and inlined — no external fetch.
        assert pack.brand.logo_data_uri is not None
        assert pack.brand.logo_data_uri.startswith("data:image/svg+xml;base64,")

    def test_html_header_shows_brand_and_logo(self, tmp_path):
        out = render_evidence_pack_html(self._pack(tmp_path))
        assert '<header class="brand">' in out
        assert 'class="brand-name">kew<' in out
        assert 'src="data:image/svg+xml;base64,' in out

    def test_generated_by_kew_line_with_timestamp_and_period(self, tmp_path):
        import kew

        out = render_evidence_pack_html(self._pack(tmp_path))
        assert f"Generated by kew v{kew.__version__}" in out
        assert "2026-07-20 12:00" in out  # timestamp
        assert "2026-07-13 → 2026-07-20 (7 days)" in out  # period

    def test_json_twin_carries_brand(self, tmp_path):
        import kew

        pack = self._pack(tmp_path)
        data = json.loads(render_evidence_pack_json(pack))
        brand = data["brand"]
        assert brand["name"] == "kew"
        assert brand["accent"] == "#c81e5a"
        assert brand["logo_data_uri"].startswith("data:image/svg+xml;base64,")
        assert brand["generator"] == f"kew v{kew.__version__}"
        assert brand["kew_version"] == kew.__version__

    def test_missing_logo_file_degrades_to_no_logo(self, tmp_path, monkeypatch):
        # The shipped favicon is the only logo now; if it is unreadable the
        # header renders without an <img> rather than failing or fetching.
        from kew import report_export

        monkeypatch.setattr(
            report_export, "_default_logo_path", lambda: tmp_path / "nope.svg"
        )
        pack = self._pack(tmp_path)
        assert pack.brand.logo_data_uri is None
        out = render_evidence_pack_html(pack)
        assert '<header class="brand">' in out  # header still renders
        assert "<img" not in out.split("</header>")[0]

    def test_html_stays_self_contained(self, tmp_path):
        out = render_evidence_pack_html(self._pack(tmp_path))
        # The inlined SVG namespace is base64-encoded, never a literal fetch.
        assert "http://" not in out.replace("http://www.w3.org", "")
        assert "https://" not in out

    def test_empty_pack_still_branded(self, tmp_path):
        pack = _build(_config(tmp_path))  # no db => empty pack
        assert pack.brand.name == "kew"
        out = render_evidence_pack_html(pack)
        assert '<header class="brand">' in out
        assert "Generated by kew v" in out


class TestGeneratedPackPalette:
    """#1126 — a *generated* pack carries nothing off the light token set.

    ``tests/test_report_style.py`` guards the stylesheet in isolation; this is
    the end-to-end grep the acceptance criteria ask for, over the real artifact
    including the data island and the inlined brand accent.
    """

    def _pack(self, tmp_path):
        config = _config(tmp_path)
        _seed(
            config,
            [
                _record(
                    "r1",
                    1,
                    cost=6.0,
                    title="Fix bug",
                    pr_url="https://github.com/acme/widgets/pull/1",
                    pr_state="MERGED",
                    item_id="1",
                ),
                _record("r2", 2, cost=5.0, pr_state="OPEN", item_id="2"),
                _record("r3", 3, cost=1.0, pr_state="CLOSED", item_id="3"),
            ],
        )
        return _build(config)

    def test_every_hex_in_the_document_is_a_light_token(self, tmp_path):
        out = render_evidence_pack_html(self._pack(tmp_path))
        found = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", out)}
        allowed = {v.lower() for v in LIGHT.values()}
        assert found <= allowed, f"off-palette: {sorted(found - allowed)}"

    def test_the_accent_on_the_white_ground_is_the_light_one(self, tmp_path):
        out = render_evidence_pack_html(self._pack(tmp_path))
        assert "--brand-accent:var(--accent)" in out
        assert "--accent:#c81e5a" in out
        assert "#ff2a6d" not in out.lower()

    def test_the_pack_is_one_self_contained_file_in_ibm_plex(self, tmp_path):
        out = render_evidence_pack_html(self._pack(tmp_path))
        assert "@font-face" in out
        assert "data:font/woff2;base64," in out
        assert "IBM Plex Mono" in out and "IBM Plex Sans" in out
        assert "https://" not in out.replace(
            "https://github.com/acme/widgets/pull/1", ""
        )
        assert "http://" not in out.replace("http://www.w3.org", "")


class TestReportConfig:
    """The removed white-label keys are rejected."""

    @pytest.mark.parametrize("key", ["brand_name", "brand_logo", "brand_accent"])
    def test_white_label_key_rejected(self, key):
        from pydantic import ValidationError

        from kew.config import DispatchConfig

        with pytest.raises(ValidationError):
            DispatchConfig.model_validate({"report": {key: "removed"}})


def _assert_empty_model_outcomes(sc):
    # qwen opened no PRs → merge rate and $/merged PR are null, not crashes.
    qwen = next(r for r in sc["rows"] if r["model"] == "local/qwen")
    assert qwen["merge_rate"] is None
    assert qwen["cost_per_merged_pr"] is None


def test_round8_export_uses_matched_attempt_row(tmp_path, monkeypatch):
    from kew import report_export

    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "export-race.db")) as store:
        record = _record("retry", 1945)
        record.attempt_number = 2
        store.save(record)
        original = store.list_since

        def retry_after_page(since):
            page = original(since)
            record.attempt_number = 3
            record.title = "attempt three"
            store.save(record)
            store.append_audit_event(
                timestamp=_NOW.isoformat(),
                event="sandbox_resolved",
                run_id=record.run_id,
                payload={
                    "attempt_number": 3,
                    "posture": "sandboxed",
                    "network_boundary": "sidecar-certified",
                    "reason": "attempt 3",
                },
            )
            return page

        monkeypatch.setattr(store, "list_since", retry_after_page)
        monkeypatch.setattr(report_export, "sync_outcomes", lambda *a: SyncResult())
        window = report_export._read_export_window(
            store,
            config,
            since=_NOW - timedelta(days=2),
            now=_NOW,
            daily_cap=None,
            weekly_cap=None,
        )
        [selected] = window.records
        posture = window.postures[record.run_id]
        assert selected.attempt_number == 3
        assert posture.reason == f"attempt {selected.attempt_number}"
        assert selected.title == "attempt three"
        assert posture.network_boundary == "sidecar-certified"
