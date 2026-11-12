"""`kew report provenance`: each console feature's build history (#3143)."""

from __future__ import annotations

import errno
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kew.audit import DEFAULT_ANCHORS_PATH, Actor, emit
from kew.cli import app
from kew.ledger_identity import open_managed_store
from kew.merge_verify import VERIFY_EVENT
from kew.publication_scan import OUTPUT_SCAN_EVENT
from kew.report_provenance import (
    OTHER_ERROR_CATEGORY,
    OTHER_MODEL,
    PUBLISHED_ERROR_CATEGORIES,
    RUN_EXPORT_KEYS,
    Feature,
    build_provenance,
    load_features,
    read_corpus_flags,
    write_provenance,
)
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_report_sink import render_report
from kew.run_store import RunRecord, SqliteRunStore
from kew.service.merge_verify_reading import item_merge_verify

runner = CliRunner()

#: Two of kew's test-integrity flag names (``kew.test_integrity``).
FLAG_ASSERTS_REMOVED = "asserts_removed"
FLAG_SKIP_ADDED = "skip_added"

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
REPO = "acme-demo/console"
ALPHA = "epic:demo-alpha"
BETA = "epic:demo-beta"
MAIN_SHA = "a" * 40
HEAD_101 = "b" * 40
MERGE_101 = "c" * 40
HEAD_201 = "e" * 40
LANDED_11 = "d" * 40
CATALOG_MODEL = "claude-opus-4-5"
PRIVATE_MODEL = "acme-private/llm-7-internal"
#: Strings the run records carry that must never reach either output file.
SECRETS = (
    "SECRET-TITLE-TEXT",
    "agent/secret-branch",
    PRIVATE_MODEL,
    "held-out free text",
    "tests/test_hidden.py::test_private_node",
)

FEATURES_TOML = f"""
[[features]]
id = "epics-list"
title = "Epics list"
routes = ["/epics"]
epics = ["{ALPHA}"]

[[features]]
id = "monitor"
title = "Monitor"
routes = ["/monitor", "/console"]
epics = ["{BETA}", "epic:demo-unknown"]
issues = [999]
"""


def _pr(number: int, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/pull/{number}"


_RUN_NAMESPACE = uuid.UUID("6f1c2b3a-0000-4000-8000-000000003144")


def _rid(label: str) -> str:
    """A run id as kew mints one (a uuid), named by a short label."""
    return str(uuid.uuid5(_RUN_NAMESPACE, label))


def _record(
    run_id: str,
    item_id: str,
    *,
    cost: float,
    pr: int | None,
    run_kind: str = "dispatch",
    model: str = CATALOG_MODEL,
    minutes: int = 0,
    repo: str | None = REPO,
    pr_repo: str = REPO,
) -> RunRecord:
    started = _NOW + timedelta(minutes=minutes)
    return RunRecord(
        run_id=run_id,
        issue_num=int(item_id),
        item_id=item_id,
        branch="agent/secret-branch",
        status="success",
        exit_code=0,
        started_at=started,
        finished_at=started + timedelta(minutes=10),
        estimated_cost_usd=cost,
        title="SECRET-TITLE-TEXT",
        pr_url=_pr(pr, pr_repo) if pr else None,
        repo=repo,
        run_kind=run_kind,
        model=model,
        runner_name="claude-code",
        self_check=True,
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


def _seed_epics(store: SqliteRunStore) -> None:
    for epic_ref, nodes in ((ALPHA, ["101", "102"]), (BETA, ["201"])):
        store.append_epic_tick(
            epic_ref=epic_ref,
            tick_ts=_NOW.isoformat(),
            snapshot=_snapshot(epic_ref, nodes),
            actions={"schema": 1, "dispatched": [], "merged": [], "held": []},
            snapshot_hash=f"h-{epic_ref}",
        )


def _seed_runs(store: SqliteRunStore) -> None:
    for record in (
        _record(_rid("a1"), "101", cost=1.5, pr=11),
        _record(
            _rid("a2"),
            "101",
            cost=0.5,
            pr=11,
            run_kind="adversarial_review",
            minutes=1,
        ),
        _record(_rid("a3"), "102", cost=2.0, pr=12, model=PRIVATE_MODEL, minutes=2),
        _record(_rid("b1"), "201", cost=3.0, pr=21, minutes=3),
        _record(_rid("x1"), "999", cost=0.25, pr=None, minutes=4),
        _record(_rid("z1"), "555", cost=9.0, pr=55, minutes=5),
    ):
        store.save(record)


def _review(
    store: SqliteRunStore,
    item_id: str,
    head: str,
    verdict: str,
    repo: str | None,
    *,
    epic_ref: str = ALPHA,
) -> None:
    """One governed-review round and the verdict the driver attests for it."""
    review_round = store.record_review_round(
        epic_ref, item_id, head_sha=head, verdict=verdict
    )
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "epic-driver"),
        issue_num=int(item_id),
        item_id=item_id,
        repo=repo,
        payload={
            "item_id": item_id,
            "head_sha": head,
            "round": review_round,
            "verdict": verdict,
        },
    )


def _seed_reviews(store: SqliteRunStore) -> None:
    _review(store, "101", HEAD_101, "NEEDS_CHANGES", REPO)
    _review(store, "101", HEAD_101, "READY", REPO)
    _review(store, "201", HEAD_201, "READY", REPO, epic_ref=BETA)


def _seed_failed_verify(store: SqliteRunStore) -> None:
    """The drive's merge verify of item 101's head: a merge commit, held-out red."""
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "operator"),
        repo=REPO,
        payload={
            "ref": MERGE_101,
            "ref_sha": MERGE_101,
            "ref_parents": [HEAD_101, MAIN_SHA],
            "base_ref": "main",
            "base_sha": MAIN_SHA,
            "outcome": "failed",
            "error": "held-out free text",
            "error_category": "held_out_failed",
            "recorded_at": _NOW.isoformat(),
            "held_out": {
                "outcome": "failed",
                "failed": [
                    "tests/test_a.py::test_one",
                    "tests/test_hidden.py::test_private_node",
                ],
                "declared": ["tests/test_a.py::test_one"],
                "undeclared": ["tests/test_hidden.py::test_private_node"],
                "seconds": 12,
                "error": "held-out free text",
            },
        },
    )


def _seed_passed_verify(store: SqliteRunStore) -> None:
    """Item 201's bare head verified green: no held-out failure to count."""
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "operator"),
        repo=REPO,
        payload={
            "ref_sha": HEAD_201,
            "ref_parents": [MAIN_SHA],
            "base_sha": MAIN_SHA,
            "outcome": "passed",
            "recorded_at": _NOW.isoformat(),
            "held_out": {"outcome": "passed", "failed": [], "undeclared": []},
        },
    )


def _seed_landed_merge(store: SqliteRunStore) -> None:
    entry, _ = store.enqueue_merge(
        REPO, 11, HEAD_101, session="demo-session", behavior_changes=()
    )
    assert store.claim_merge_entry(
        entry.id, "demo-owner", expected_base=MAIN_SHA, expected_tree="f" * 40
    )
    assert store.record_landed_merge(entry.id, LANDED_11, "landed")


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """The project's registered ledger, as any writing kew command leaves it."""
    db_path = tmp_path / "logs" / "dispatch.db"
    db_path.parent.mkdir(parents=True)
    store = open_managed_store(
        project_root=tmp_path,
        db_path=db_path,
        anchors_path=tmp_path / DEFAULT_ANCHORS_PATH,
    )
    try:
        _seed_epics(store)
        _seed_runs(store)
        _seed_reviews(store)
        _seed_failed_verify(store)
        _seed_passed_verify(store)
        _seed_landed_merge(store)
    finally:
        store.close()
    return db_path


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Run reports as the sink writes them; two runs carry integrity flags."""
    root = tmp_path / "corpus"
    repo_dir = root / "acme-demo--console"
    repo_dir.mkdir(parents=True)
    flagged = {
        _rid("a1"): [FLAG_ASSERTS_REMOVED, FLAG_SKIP_ADDED],
        _rid("b1"): [FLAG_SKIP_ADDED],
        _rid("a3"): [],
    }
    for run_id, flags in flagged.items():
        integrity = {
            "tests_touched": 1,
            "asserts_removed": 0,
            "skips_added": 0,
            "changed_expected": 0,
            "test_only_fix_round": 0,
            "integrity_flags": flags,
        }
        record = _record(run_id, "101", cost=0.0, pr=None)
        text = render_report(record, REPO, [], integrity)
        (repo_dir / f"{run_id}.md").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def features(tmp_path: Path) -> Path:
    path = tmp_path / "features.toml"
    path.write_text(FEATURES_TOML, encoding="utf-8")
    return path


def _project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sink: Path, repo: str = REPO
) -> None:
    (tmp_path / "kew.toml").write_text(
        f'[project]\nname = "demo"\nrepo = "{repo}"\n'
        f'[logging]\ndir = "{tmp_path / "logs"}"\n'
        f'[run_reports]\nsink_path = "{sink}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["report", "provenance", *args])


def _by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in document["features"]}


@pytest.mark.parametrize(
    "malformed",
    [
        "no frontmatter fence at all\n",
        "---\nrun_id: run-demo-x9\nintegrity_flags: []\n---\nbody\n",
    ],
    ids=["missing-fence", "unquoted-run-id"],
)
def test_corpus_skips_a_malformed_report_loudly_and_keeps_the_rest(
    corpus: Path, malformed: str, caplog: pytest.LogCaptureFixture
) -> None:
    bad = corpus / "acme-demo--console" / "run-demo-x9.md"
    bad.write_text(malformed, encoding="utf-8")
    with caplog.at_level("WARNING", logger="kew.report_provenance"):
        flags = read_corpus_flags(corpus)
    assert "run-demo-x9" not in flags
    assert flags[_rid("a1")] == [FLAG_ASSERTS_REMOVED, FLAG_SKIP_ADDED]
    assert "skipped run report run-demo-x9.md" in caplog.text


def test_per_feature_totals_join_runs_reviews_verify_and_corpus(
    ledger: Path, corpus: Path, features: Path
) -> None:
    store = SqliteRunStore(str(ledger), read_only=True)
    try:
        export = build_provenance(
            store, load_features(features), read_corpus_flags(corpus), repo=REPO
        )
    finally:
        store.close()
    doc = export.provenance
    assert doc["integrity_source"] == "corpus"
    alpha = _by_id(doc)["epics-list"]
    assert alpha["routes"] == ["/epics"]
    assert alpha["epics"] == [ALPHA]
    assert alpha["issues"] == ["101", "102"]
    assert alpha["prs"] == [
        {"url": _pr(11), "landed_sha": LANDED_11},
        {"url": _pr(12), "landed_sha": None},
    ]
    assert alpha["runs"] == {"adversarial_review": 1, "dispatch": 2}
    assert alpha["list_cost_usd"] == 4.0
    assert alpha["review_rounds"] == {"NEEDS_CHANGES": 1, "READY": 1}
    assert alpha["held_out"] == {
        "items_failed": 1,
        "tests_failed": 2,
        "tests_undeclared": 1,
    }
    assert alpha["integrity"] == {
        "runs_flagged": 1,
        "flags": {FLAG_ASSERTS_REMOVED: 1, FLAG_SKIP_ADDED: 1},
    }

    monitor = _by_id(doc)["monitor"]
    assert monitor["issues"] == ["201", "999"]
    assert monitor["prs"] == [{"url": _pr(21), "landed_sha": None}]
    assert monitor["runs"] == {"dispatch": 2}
    assert monitor["list_cost_usd"] == 3.25
    assert monitor["review_rounds"] == {"READY": 1}
    assert monitor["held_out"] == {
        "items_failed": 0,
        "tests_failed": 0,
        "tests_undeclared": 0,
    }
    assert monitor["integrity"] == {"runs_flagged": 1, "flags": {FLAG_SKIP_ADDED: 1}}


def test_runs_export_keeps_only_the_allowlist_and_hides_private_models(
    ledger: Path, features: Path
) -> None:
    store = SqliteRunStore(str(ledger), read_only=True)
    try:
        export = build_provenance(store, load_features(features), None, repo=REPO)
    finally:
        store.close()
    rows = {row["run_id"]: row for row in export.runs["runs"]}
    assert set(rows) == {
        _rid("a1"),
        _rid("a2"),
        _rid("a3"),
        _rid("b1"),
        _rid("x1"),
        _rid("z1"),
    }
    for row in rows.values():
        assert tuple(row) == RUN_EXPORT_KEYS
    assert rows[_rid("a1")]["model"] == CATALOG_MODEL
    assert rows[_rid("a3")]["model"] == OTHER_MODEL
    assert rows[_rid("a1")]["epic"] == ALPHA
    assert rows[_rid("z1")]["epic"] is None
    assert rows[_rid("a1")]["pr_url"] == _pr(11)
    assert rows[_rid("a1")]["cost_usd"] == 1.5
    published = json.dumps(export.provenance) + json.dumps(export.runs)
    for secret in SECRETS:
        assert secret not in published


def test_cli_writes_both_files_and_names_the_unknown_epic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    corpus: Path,
    features: Path,
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke(
        "--features", str(features), "--out", str(out), "--corpus", str(corpus)
    )
    assert result.exit_code == 0, result.output
    assert "epic:demo-unknown" in result.stderr
    provenance = json.loads((out / "provenance.json").read_text())
    runs = json.loads((out / "runs.json").read_text())
    # The feature naming an unknown epic is still exported, from what is known.
    assert _by_id(provenance)["monitor"]["epics"] == [BETA, "epic:demo-unknown"]
    assert _by_id(provenance)["epics-list"]["integrity"]["runs_flagged"] == 1
    for row in runs["runs"]:
        assert set(row) <= set(RUN_EXPORT_KEYS)
    for secret in SECRETS:
        assert secret not in (out / "provenance.json").read_text()
        assert secret not in (out / "runs.json").read_text()


def test_cli_reads_the_configured_sink_when_no_corpus_is_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    corpus: Path,
    features: Path,
) -> None:
    _project(tmp_path, monkeypatch, sink=corpus)
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 0, result.output
    provenance = json.loads((out / "provenance.json").read_text())
    assert provenance["integrity_source"] == "corpus"
    assert _by_id(provenance)["monitor"]["integrity"]["runs_flagged"] == 1


def test_cli_without_any_corpus_counts_zero_and_says_so_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert result.stderr.count("no run-report corpus") == 1
    provenance = json.loads((out / "provenance.json").read_text())
    assert provenance["integrity_source"] == "none"
    for feature in provenance["features"]:
        assert feature["integrity"] == {"runs_flagged": 0, "flags": {}}


@pytest.mark.parametrize(
    "content",
    [
        "not = [valid",
        "[[features]]\nid = 'x'\ntitle = 'X'\n",  # no routes
        "[[features]]\nid = 'x'\ntitle = 'X'\nroutes = ['/x']\nissues = ['12']\n",
        "[[features]]\nid = 'x'\ntitle = 'X'\nroutes = ['/x']\nlogs = ['y']\n",
        "[[features]]\nid = 'x'\ntitle = 'X'\nroutes = ['/x']\n"
        "[[features]]\nid = 'x'\ntitle = 'Y'\nroutes = ['/y']\n",
        "",
    ],
)
def test_cli_refuses_a_malformed_features_file_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, content: str
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    path = tmp_path / "bad.toml"
    path.write_text(content, encoding="utf-8")
    out = tmp_path / "out"
    result = _invoke("--features", str(path), "--out", str(out))
    assert result.exit_code == 2
    assert not out.exists()


def test_cli_refuses_a_missing_features_file_or_corpus_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    missing = _invoke("--features", str(tmp_path / "nope.toml"), "--out", str(out))
    assert missing.exit_code == 2
    no_corpus = _invoke(
        "--features",
        str(features),
        "--out",
        str(out),
        "--corpus",
        str(tmp_path / "nope"),
    )
    assert no_corpus.exit_code == 2
    assert not out.exists()


def test_cli_without_a_ledger_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 2
    assert "no run store" in result.stderr
    assert not out.exists()


_DEMO = Path(__file__).resolve().parents[2] / "frontend" / "demo"


def test_committed_samples_carry_only_allowlisted_run_keys() -> None:
    runs = json.loads((_DEMO / "provenance" / "runs.sample.json").read_text())
    provenance = json.loads(
        (_DEMO / "provenance" / "provenance.sample.json").read_text()
    )
    assert runs["runs"]
    for row in runs["runs"]:
        assert tuple(row) == RUN_EXPORT_KEYS
    seed_ids = [f.id for f in load_features(_DEMO / "features.toml")]
    assert [f["id"] for f in provenance["features"]] == seed_ids


# --- review fixes: shared nodes, the full verify history, repo scope, output --


C2 = "epic:demo-c2"
C3 = "epic:demo-c3"
HEAD_301 = "9" * 40


def _managed_store(root: Path) -> SqliteRunStore:
    db_path = root / "logs" / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return open_managed_store(
        project_root=root, db_path=db_path, anchors_path=root / DEFAULT_ANCHORS_PATH
    )


def _tick(store: SqliteRunStore, epic_ref: str, nodes: list[dict[str, Any]]) -> None:
    snapshot = _snapshot(epic_ref, [])
    snapshot["nodes"] = nodes
    store.append_epic_tick(
        epic_ref=epic_ref,
        tick_ts=_NOW.isoformat(),
        snapshot=snapshot,
        actions={"schema": 1, "dispatched": [], "merged": [], "held": []},
        snapshot_hash=f"h-{epic_ref}",
    )


def _verify(store: SqliteRunStore, ref_sha: str, outcome: str) -> None:
    held = (
        {"outcome": "failed", "failed": ["tests/test_a.py::test_one"], "undeclared": []}
        if outcome == "failed"
        else {"outcome": "passed", "failed": [], "undeclared": []}
    )
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "operator"),
        repo=REPO,
        payload={
            "ref_sha": ref_sha,
            "ref_parents": [MAIN_SHA],
            "base_sha": MAIN_SHA,
            "outcome": outcome,
            "recorded_at": _NOW.isoformat(),
            "held_out": held,
        },
    )


def test_a_node_two_epics_share_counts_for_the_later_epic_a_feature_names(
    tmp_path: Path,
) -> None:
    """The drive's later epic re-snapshots a node the earlier one also holds."""
    store = _managed_store(tmp_path)
    try:
        _tick(store, C2, [{"id": "301", "title": "issue 301"}])
        _tick(
            store,
            C3,
            [
                {
                    "id": "301",
                    "title": "issue 301",
                    "pr_url": _pr(31),
                    "head_oid": HEAD_301,
                    "status": "in_review",
                },
                {"id": "302", "title": "issue 302"},
            ],
        )
        store.save(_record(_rid("s1"), "301", cost=5.0, pr=31))
        store.save(_record(_rid("s2"), "302", cost=1.0, pr=32, minutes=1))
        _verify(store, HEAD_301, "failed")
        feature = Feature(id="graph", title="Graph", routes=("/g",), epics=(C3,))
        export = build_provenance(store, [feature], None, repo=REPO)
    finally:
        store.close()
    entry = export.provenance["features"][0]
    assert entry["issues"] == ["301", "302"]
    assert entry["runs"] == {"dispatch": 2}
    assert entry["list_cost_usd"] == 6.0
    # 301's PR head lives in the epic the feature named, not the first one.
    assert entry["held_out"]["items_failed"] == 1


def test_an_old_held_out_failure_counts_behind_many_newer_verifies(
    ledger: Path, features: Path
) -> None:
    """Item 101's failed verify is the OLDEST event; 501 newer ones follow it."""
    store = SqliteRunStore(str(ledger))
    try:
        for n in range(501):
            _verify(store, f"{n:040x}", "passed")
        export = build_provenance(store, load_features(features), None, repo=REPO)
    finally:
        store.close()
    alpha = _by_id(export.provenance)["epics-list"]
    assert alpha["held_out"] == {
        "items_failed": 1,
        "tests_failed": 2,
        "tests_undeclared": 1,
    }


def test_runs_off_the_project_repo_never_reach_the_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
) -> None:
    """Fail closed: a private repo's run, and a run with no repo, both drop."""
    store = SqliteRunStore(str(ledger))
    try:
        store.save(
            _record(
                _rid("p1"),
                "101",
                cost=7.0,
                pr=70,
                repo="acme-private/vault",
                pr_repo="acme-private/vault",
            )
        )
        # No repo recorded, though its PR is on the project repo: still dropped.
        store.save(_record(_rid("n1"), "101", cost=8.0, pr=80, repo=None))
    finally:
        store.close()
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 0, result.output
    runs_text = (out / "runs.json").read_text()
    provenance = json.loads((out / "provenance.json").read_text())
    run_ids = {row["run_id"] for row in json.loads(runs_text)["runs"]}
    assert _rid("p1") not in run_ids
    assert _rid("n1") not in run_ids
    assert "acme-private/vault" not in runs_text
    alpha = _by_id(provenance)["epics-list"]
    assert alpha["runs"] == {"adversarial_review": 1, "dispatch": 2}
    assert alpha["list_cost_usd"] == 4.0
    assert [pr["url"] for pr in alpha["prs"]] == [_pr(11), _pr(12)]


@pytest.mark.parametrize(
    "pr_url",
    [
        _pr(71, "acme-private/vault"),
        _pr(72, "acme-demo/console-fork"),
        _pr(73, "someone-else/console"),
        f"{_pr(74)}/files",
        "https://example.org/acme-demo/console/pull/75",
        "",
    ],
    ids=[
        "private-repo",
        "same-owner-other-repo",
        "other-owner",
        "not-a-pr",
        "off-github",
        "empty",
    ],
)
def test_a_project_run_whose_pr_is_on_another_repo_never_reaches_the_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
    pr_url: str,
) -> None:
    """Fail closed: a run recorded on the project repo whose PR link points
    anywhere else contributes nothing — no row, no PR, no count, no cost — and
    stderr says how many were dropped, never which."""
    store = SqliteRunStore(str(ledger))
    try:
        record = _record(_rid("o1"), "101", cost=7.0, pr=None, minutes=6)
        record.pr_url = pr_url
        store.save(record)
    finally:
        store.close()
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 0, result.output
    runs_text = (out / "runs.json").read_text()
    provenance_text = (out / "provenance.json").read_text()
    run_ids = {row["run_id"] for row in json.loads(runs_text)["runs"]}
    assert _rid("o1") not in run_ids
    assert _rid("a1") in run_ids
    for text in (runs_text, provenance_text):
        assert (pr_url or "\0") not in text
    alpha = _by_id(json.loads(provenance_text))["epics-list"]
    assert alpha["runs"] == {"adversarial_review": 1, "dispatch": 2}
    assert alpha["list_cost_usd"] == 4.0
    assert [pr["url"] for pr in alpha["prs"]] == [_pr(11), _pr(12)]
    assert "1 run with a pull request off the project repo was dropped" in result.stderr
    if pr_url:
        assert pr_url not in result.stderr


def test_a_run_whose_id_is_not_a_uuid_never_reaches_the_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
) -> None:
    """Fail closed: a legacy run id (not the uuid kew mints) drops the run whole —
    no row, no PR, no count, no cost — and stderr counts it without naming it."""
    legacy = "LegacyRunId0123456789ABCDEFabcdef01234567890"
    assert len(legacy) == 44
    store = SqliteRunStore(str(ledger))
    try:
        store.save(_record(legacy, "101", cost=7.0, pr=13, minutes=6))
    finally:
        store.close()
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 0, result.output
    runs_text = (out / "runs.json").read_text()
    provenance_text = (out / "provenance.json").read_text()
    assert legacy not in runs_text
    assert legacy not in provenance_text
    assert _rid("a1") in {row["run_id"] for row in json.loads(runs_text)["runs"]}
    alpha = _by_id(json.loads(provenance_text))["epics-list"]
    assert alpha["runs"] == {"adversarial_review": 1, "dispatch": 2}
    assert alpha["list_cost_usd"] == 4.0
    assert [pr["url"] for pr in alpha["prs"]] == [_pr(11), _pr(12)]
    assert "1 run with a run id that is not a uuid was dropped" in result.stderr
    assert legacy not in result.stderr


def _runs_of(store: SqliteRunStore, *records: RunRecord) -> dict[str, dict[str, Any]]:
    for record in records:
        store.save(record)
    features = [Feature(id="epics-list", title="Epics list", routes=("/epics",))]
    export = build_provenance(store, features, None, repo=REPO)
    return {row["run_id"]: row for row in export.runs["runs"]}


def test_runs_name_only_published_error_categories_and_other(tmp_path: Path) -> None:
    """A category kew writes is published as itself; any other code is
    ``other``, so a code the vocabulary has never met cannot reach the file."""
    stage = _record(_rid("e1"), "101", cost=0.0, pr=None)
    stage.error_category = "review_stage_finished"
    unknown = _record(_rid("e2"), "101", cost=0.0, pr=None, minutes=1)
    unknown.error_category = "acme_private_layoff_plan"
    clean = _record(_rid("e3"), "101", cost=0.0, pr=None, minutes=2)
    store = _managed_store(tmp_path)
    try:
        rows = _runs_of(store, stage, unknown, clean)
    finally:
        store.close()
    assert rows[_rid("e1")]["error_category"] == "review_stage_finished"
    assert rows[_rid("e2")]["error_category"] == OTHER_ERROR_CATEGORY == "other"
    assert rows[_rid("e3")]["error_category"] is None
    assert "acme_private_layoff_plan" not in json.dumps(rows)
    # The demo schema admits exactly these, and "other".
    vocabulary = json.loads((_DEMO / "error-categories.json").read_text())
    for row in rows.values():
        code = row["error_category"]
        assert code is None or code in {*vocabulary["categories"], "other"}


def test_an_empty_item_id_is_published_as_null(tmp_path: Path) -> None:
    record = _record(_rid("i1"), "101", cost=0.0, pr=None)
    record.item_id = ""
    store = _managed_store(tmp_path)
    try:
        rows = _runs_of(store, record)
    finally:
        store.close()
    assert rows[_rid("i1")]["item_id"] is None


def test_the_published_error_categories_hold_the_ledgers_review_stage_code() -> None:
    assert "review_stage_finished" in PUBLISHED_ERROR_CATEGORIES
    assert OTHER_ERROR_CATEGORY not in PUBLISHED_ERROR_CATEGORIES


def test_cli_scopes_to_the_repo_option_over_the_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink", repo="")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out), "--repo", REPO)
    assert result.exit_code == 0, result.output
    runs = json.loads((out / "runs.json").read_text())["runs"]
    assert {row["run_id"] for row in runs} >= {_rid("a1"), _rid("b1")}


OTHER_REPO = "acme-private/vault"
HEAD_12_A = "1" * 40
HEAD_12_B = "2" * 40
HEAD_13_A = "3" * 40
HEAD_13_NONE = "4" * 40
HEAD_14_B = "5" * 40


def _held_out_verify(
    store: SqliteRunStore,
    head: str,
    failed: list[str],
    *,
    repo: str | None,
    minutes: int,
) -> None:
    """A failed merge verify of *head*'s bare commit, recorded *minutes* late."""
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "operator"),
        repo=repo,
        payload={
            "ref_sha": head,
            "ref_parents": [MAIN_SHA],
            "base_sha": MAIN_SHA,
            "outcome": "failed" if failed else "passed",
            "recorded_at": (_NOW + timedelta(minutes=minutes)).isoformat(),
            "held_out": {
                "outcome": "failed" if failed else "passed",
                "failed": failed,
                "undeclared": [],
            },
        },
    )


def _issue_feature(*issues: int) -> Feature:
    return Feature(id="f", title="F", routes=("/f",), issues=issues)


def test_a_same_numbered_issue_of_another_repo_adds_no_review_or_held_out(
    tmp_path: Path,
) -> None:
    """Fail closed: issue 12 of another repo in a shared ledger counts nothing."""
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r12"), "12", cost=1.0, pr=120))
        _review(store, "12", HEAD_12_A, "READY", REPO)
        _held_out_verify(
            store, HEAD_12_A, ["tests/test_a.py::t1"], repo=REPO, minutes=1
        )
        store.save(
            _record(
                "run-other-r12",
                "12",
                cost=4.0,
                pr=121,
                repo=OTHER_REPO,
                pr_repo=OTHER_REPO,
                minutes=2,
            )
        )
        _review(store, "12", HEAD_12_B, "NEEDS_CHANGES", OTHER_REPO)
        _held_out_verify(
            store,
            HEAD_12_B,
            ["tests/test_b.py::t1", "tests/test_b.py::t2"],
            repo=OTHER_REPO,
            minutes=3,
        )
        export = build_provenance(store, [_issue_feature(12)], None, repo=REPO)
    finally:
        store.close()
    entry = export.provenance["features"][0]
    assert entry["runs"] == {"dispatch": 1}
    assert entry["review_rounds"] == {"READY": 1}
    assert entry["held_out"] == {
        "items_failed": 1,
        "tests_failed": 1,
        "tests_undeclared": 0,
    }


HEAD_17 = "c2" * 20


def test_a_re_verify_with_no_verdict_keeps_the_failure_in_the_held_out_totals(
    tmp_path: Path,
) -> None:
    """A later verify of the same head that ended with no verdict knows nothing
    new about it: the failed verify before it is what the totals count."""
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r17"), "17", cost=1.0, pr=170))
        _review(store, "17", HEAD_17, "READY", REPO)
        _held_out_verify(store, HEAD_17, ["tests/test_a.py::t1"], repo=REPO, minutes=1)
        emit(
            store,
            VERIFY_EVENT,
            actor=Actor("human", "operator"),
            repo=REPO,
            payload={
                "ref_sha": HEAD_17,
                "ref_parents": [MAIN_SHA],
                "base_sha": MAIN_SHA,
                "outcome": "incomplete",
                "recorded_at": (_NOW + timedelta(minutes=2)).isoformat(),
                "held_out": None,
            },
        )
        export = build_provenance(store, [_issue_feature(17)], None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["features"][0]["held_out"] == {
        "items_failed": 1,
        "tests_failed": 1,
        "tests_undeclared": 0,
    }


def test_a_review_verdict_or_verify_with_no_repo_does_not_count(
    tmp_path: Path,
) -> None:
    """Fail closed: only the recorded repo vouches for a verdict or a verify."""
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r13"), "13", cost=1.0, pr=130))
        _review(store, "13", HEAD_13_A, "READY", REPO)
        _held_out_verify(store, HEAD_13_A, [], repo=REPO, minutes=1)
        _review(store, "13", HEAD_13_NONE, "NEEDS_FULL", None)
        _held_out_verify(
            store, HEAD_13_NONE, ["tests/test_c.py::t1"], repo=None, minutes=2
        )
        export = build_provenance(store, [_issue_feature(13)], None, repo=REPO)
    finally:
        store.close()
    entry = export.provenance["features"][0]
    assert entry["review_rounds"] == {"READY": 1}
    assert entry["held_out"] == {
        "items_failed": 0,
        "tests_failed": 0,
        "tests_undeclared": 0,
    }


def test_an_issue_only_another_repo_holds_is_named_unknown(tmp_path: Path) -> None:
    store = _managed_store(tmp_path)
    try:
        store.save(
            _record(
                "run-other-r14",
                "14",
                cost=1.0,
                pr=140,
                repo=OTHER_REPO,
                pr_repo=OTHER_REPO,
            )
        )
        _review(store, "14", HEAD_14_B, "READY", OTHER_REPO)
        export = build_provenance(store, [_issue_feature(14)], None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["features"][0]["review_rounds"] == {}
    assert export.notes == [
        "feature f: issue 14 has no run, review round or epic node in the ledger",
        "item 14: 1 review round has no in-repo verdict and is not counted",
    ]


HEAD_15 = "6" * 40
HEAD_16_OLD = "7" * 40
HEAD_16_NEW = "8" * 40
HEAD_16_B = "a1" * 20


@pytest.mark.parametrize("fork_verdict", ["NEEDS_CHANGES", "READY"])
def test_a_fork_round_on_the_same_round_and_head_counts_no_second_time(
    tmp_path: Path, fork_verdict: str
) -> None:
    """After a retry, another repo's round 1 reviews the same commit (a fork).

    Only this repo's one attested verdict counts, whether the fork's verdict
    differs or matches.
    """
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r15"), "15", cost=1.0, pr=150))
        _review(store, "15", HEAD_15, "READY", REPO)
        store.mark_epic_retry_item(ALPHA, "15")
        _review(store, "15", HEAD_15, fork_verdict, OTHER_REPO)
        export = build_provenance(store, [_issue_feature(15)], None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["features"][0]["review_rounds"] == {"READY": 1}
    assert export.notes == [
        "item 15: 1 review round has no in-repo verdict and is not counted"
    ]


def test_another_repos_newer_head_event_raises_no_held_out_failure(
    tmp_path: Path,
) -> None:
    """Item 16's newest head passed; another repo's newer scan must not hide it.

    The old head's failed verify is not the latest one: a head event of a
    same-numbered item elsewhere must not drop the new head from the join.
    """
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r16"), "16", cost=1.0, pr=160))
        _review(store, "16", HEAD_16_OLD, "READY", REPO)
        _held_out_verify(
            store, HEAD_16_OLD, ["tests/test_a.py::t1"], repo=REPO, minutes=1
        )
        for head, repo in ((HEAD_16_NEW, REPO), (HEAD_16_B, OTHER_REPO)):
            emit(
                store,
                OUTPUT_SCAN_EVENT,
                actor=Actor("policy", "epic-driver"),
                issue_num=16,
                item_id="16",
                repo=repo,
                payload={"item_id": "16", "head_sha": head},
            )
        _held_out_verify(store, HEAD_16_NEW, [], repo=REPO, minutes=3)
        export = build_provenance(store, [_issue_feature(16)], None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["features"][0]["held_out"] == {
        "items_failed": 0,
        "tests_failed": 0,
        "tests_undeclared": 0,
    }


def test_a_round_no_in_repo_verdict_attests_is_named_and_not_counted(
    tmp_path: Path,
) -> None:
    """A round recorded with no verdict row (a merge-queue round, say)."""
    store = _managed_store(tmp_path)
    try:
        store.save(_record(_rid("r17"), "17", cost=1.0, pr=170))
        _review(store, "17", HEAD_15, "READY", REPO)
        store.record_review_round(
            ALPHA, "17", head_sha=HEAD_16_OLD, verdict="NEEDS_CHANGES"
        )
        store.record_review_round(
            ALPHA, "17", head_sha=HEAD_16_NEW, verdict="NEEDS_CHANGES"
        )
        features = [_issue_feature(17), Feature("g", "G", ("/g",), issues=(17,))]
        export = build_provenance(store, features, None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["features"][0]["review_rounds"] == {"READY": 1}
    # Once per item, however many features name it.
    assert export.notes == [
        "item 17: 2 review rounds have no in-repo verdict and are not counted"
    ]


def test_cli_without_a_repo_could_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    features: Path,
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink", repo="")
    out = tmp_path / "out"
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 2
    assert "repo" in result.stderr
    assert not out.exists()


def test_cli_refuses_a_features_file_that_is_not_utf8_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    path = tmp_path / "latin1.toml"
    path.write_bytes(b"[[features]]\nid = 'caf\xe9'\ntitle = 'X'\nroutes = ['/x']\n")
    out = tmp_path / "out"
    result = _invoke("--features", str(path), "--out", str(out))
    assert result.exit_code == 2
    assert "UTF-8" in result.stderr
    assert not out.exists()


def test_cli_refuses_an_out_path_that_is_a_file_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    out.write_text("keep me", encoding="utf-8")
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 2
    assert "cannot write" in result.stderr
    assert out.read_text(encoding="utf-8") == "keep me"


def test_cli_leaves_no_partial_file_when_one_output_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    out = tmp_path / "out"
    (out / "runs.json").mkdir(parents=True)
    result = _invoke("--features", str(features), "--out", str(out))
    assert result.exit_code == 2
    assert "cannot write" in result.stderr
    assert sorted(p.name for p in out.iterdir()) == ["runs.json"]
    assert (out / "runs.json").is_dir()


def test_two_exports_of_one_ledger_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    for name in ("first", "second"):
        result = _invoke("--features", str(features), "--out", str(tmp_path / name))
        assert result.exit_code == 0, result.output
    for filename in ("provenance.json", "runs.json"):
        first = (tmp_path / "first" / filename).read_bytes()
        assert first == (tmp_path / "second" / filename).read_bytes()
    provenance = json.loads((tmp_path / "first" / "provenance.json").read_text())
    runs = json.loads((tmp_path / "first" / "runs.json").read_text())
    newest = max(row["finished_at"] for row in runs["runs"])
    assert provenance["as_of"] == runs["as_of"] == newest
    assert "generated_at" not in provenance
    assert "generated_at" not in runs


def test_an_export_with_no_runs_has_no_as_of(tmp_path: Path) -> None:
    store = _managed_store(tmp_path)
    try:
        feature = Feature(id="x", title="X", routes=("/x",), issues=(1,))
        export = build_provenance(store, [feature], None, repo=REPO)
    finally:
        store.close()
    assert export.provenance["as_of"] is None
    assert export.runs["as_of"] is None


def test_cli_names_an_issue_the_ledger_holds_nothing_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    path = tmp_path / "typo.toml"
    path.write_text(
        "[[features]]\nid = 't'\ntitle = 'T'\nroutes = ['/t']\n"
        "issues = [123456, 999]\n",
        encoding="utf-8",
    )
    result = _invoke("--features", str(path), "--out", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    assert result.stderr.count("issue 123456") == 1
    # Issue 999 has a run in the ledger, so it is not named.
    assert "issue 999" not in result.stderr


def test_committed_samples_state_the_newest_run_they_hold() -> None:
    runs = json.loads((_DEMO / "provenance" / "runs.sample.json").read_text())
    provenance = json.loads(
        (_DEMO / "provenance" / "provenance.sample.json").read_text()
    )
    newest = max(row["finished_at"] for row in runs["runs"] if row["finished_at"])
    assert provenance["as_of"] == runs["as_of"] == newest


def test_a_run_report_with_an_unquoted_run_id_is_read(corpus: Path) -> None:
    """A YAML emitter writes a uuid run id as a bare scalar, not a JSON string."""
    run_id = _rid("bare")
    report = corpus / "acme-demo--console" / f"{run_id}.md"
    report.write_text(
        f'---\nrun_id: {run_id}\nintegrity_flags: ["{FLAG_SKIP_ADDED}"]\n---\nbody\n',
        encoding="utf-8",
    )
    flags = read_corpus_flags(corpus)
    assert flags[run_id] == [FLAG_SKIP_ADDED]
    assert flags[_rid("a1")] == [FLAG_ASSERTS_REMOVED, FLAG_SKIP_ADDED]


def _export_pair(store: SqliteRunStore, features: list[Feature]) -> Any:
    return build_provenance(store, features, None, repo=REPO)


def _fail_second_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.replace`` raises on its second call, as a full disk would."""
    real = os.replace
    calls = {"n": 0}

    def flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device", str(dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)


def test_a_failed_second_move_leaves_the_old_pair_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "provenance.json").write_bytes(b"old provenance\n")
    (out / "runs.json").write_bytes(b"old runs\n")
    store = SqliteRunStore(str(ledger))
    try:
        export = _export_pair(store, list(load_features(features)))
    finally:
        store.close()
    _fail_second_replace(monkeypatch)
    with pytest.raises(OSError, match="No space left"):
        write_provenance(export, out)
    assert (out / "provenance.json").read_bytes() == b"old provenance\n"
    assert (out / "runs.json").read_bytes() == b"old runs\n"
    assert sorted(p.name for p in out.iterdir()) == ["provenance.json", "runs.json"]


def test_a_failed_second_move_into_an_empty_dir_writes_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    out = tmp_path / "out"
    store = SqliteRunStore(str(ledger))
    try:
        export = _export_pair(store, list(load_features(features)))
    finally:
        store.close()
    _fail_second_replace(monkeypatch)
    with pytest.raises(OSError, match="No space left"):
        write_provenance(export, out)
    assert list(out.iterdir()) == []


def test_cli_notes_when_no_run_matches_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink", repo="")
    out = tmp_path / "out"
    result = _invoke(
        "--features", str(features), "--out", str(out), "--repo", "nobody/none"
    )
    assert result.exit_code == 0, result.output
    assert "no run in the ledger is recorded against nobody/none" in result.stderr
    assert json.loads((out / "runs.json").read_text())["runs"] == []


def test_cli_says_nothing_of_a_repo_that_matches_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Path, features: Path
) -> None:
    _project(tmp_path, monkeypatch, sink=tmp_path / "no-sink")
    result = _invoke("--features", str(features), "--out", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    assert "no run in the ledger" not in result.stderr


class _CountingStore:
    """A real store that counts its reads of the ``verify_completed`` history."""

    def __init__(self, store: SqliteRunStore) -> None:
        self._store = store
        self.verify_reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def list_audit_events(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("event") == VERIFY_EVENT:
            self.verify_reads += 1
        return self._store.list_audit_events(*args, **kwargs)


def test_the_export_reads_the_verify_history_once_whatever_the_item_count(
    tmp_path: Path,
) -> None:
    store = _managed_store(tmp_path)
    try:
        heads = {"12": HEAD_12_A, "13": HEAD_13_A, "14": HEAD_14_B}
        for minutes, (item, head) in enumerate(heads.items()):
            store.save(_record(_rid(f"v{item}"), item, cost=1.0, pr=int(item)))
            _review(store, item, head, "READY", REPO)
            failed = [f"tests/test_{item}.py::t{n}" for n in range(int(item) - 11)]
            _held_out_verify(store, head, failed, repo=REPO, minutes=minutes)
        counting = _CountingStore(store)
        export = build_provenance(
            counting, [_issue_feature(12, 13, 14)], None, repo=REPO
        )
        per_item = [
            item_merge_verify(store, item, window=None, repo=REPO) for item in heads
        ]
    finally:
        store.close()
    assert counting.verify_reads == 1
    held = [v.held_out for v in per_item if v is not None and v.held_out]
    assert export.provenance["features"][0]["held_out"] == {
        "items_failed": sum(h.outcome == "failed" for h in held),
        "tests_failed": sum(len(h.failed) + h.failed_more for h in held),
        "tests_undeclared": 0,
    }
    assert export.provenance["features"][0]["held_out"]["tests_failed"] == 6


#: The name the project's repository had before ``kew rename-repo`` moved it.
RENAMED_FROM = "acme-demo/console-old"


def test_old_slug_verdicts_and_verifies_count_only_until_the_rename(
    tmp_path: Path,
) -> None:
    """Hashed audit rows keep the slug they were recorded under, so a rename
    to the project repo vouches for the old slug's rows before it; a row the
    old slug records after it is another repository's."""
    store = _managed_store(tmp_path)
    try:
        store.save(
            _record(
                _rid("r12"),
                "12",
                cost=1.0,
                pr=120,
                repo=RENAMED_FROM,
                pr_repo=RENAMED_FROM,
            )
        )
        _review(store, "12", HEAD_12_A, "READY", RENAMED_FROM)
        _held_out_verify(
            store, HEAD_12_A, ["tests/test_a.py::t1"], repo=RENAMED_FROM, minutes=1
        )
        store.rename_repo(
            RENAMED_FROM, REPO, host="github.com", actor_id="operator", now=_NOW
        )
        _review(store, "12", HEAD_12_B, "NEEDS_CHANGES", RENAMED_FROM)
        _held_out_verify(
            store,
            HEAD_12_B,
            ["tests/test_b.py::t1", "tests/test_b.py::t2"],
            repo=RENAMED_FROM,
            minutes=3,
        )
        export = build_provenance(store, [_issue_feature(12)], None, repo=REPO)
        verify = item_merge_verify(store, "12", window=None, repo=REPO)
    finally:
        store.close()
    entry = export.provenance["features"][0]
    assert entry["runs"] == {"dispatch": 1}
    assert entry["review_rounds"] == {"READY": 1}
    assert entry["held_out"] == {
        "items_failed": 1,
        "tests_failed": 1,
        "tests_undeclared": 0,
    }
    assert verify is not None
    assert verify.head == HEAD_12_A
