"""Run-report sink (#1578): export contract, sparse honesty, non-fatality.

The frontmatter contract is pinned by parsing with a real YAML parser
(PyYAML, available transitively via the docs toolchain) — the renderer is
hand-rolled, so "it is valid YAML" must be proven, not asserted.
"""

import os
import shutil
import stat
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew import run_report_sink, subprocess_utils
from kew.config import DispatchConfig, ProjectConfig, RunReportsConfig
from kew.run_report_sink import IntegrityDiff, export_run_report, render_report
from kew.run_store import AuditEvent, RunRecord, SqliteRunStore

yaml = pytest.importorskip("yaml")


def _config(tmp_path: Path, *, enabled: bool = True) -> DispatchConfig:
    return DispatchConfig(
        run_reports=RunReportsConfig(enabled=enabled, sink_path=str(tmp_path / "sink"))
    )


def _full_record() -> RunRecord:
    return RunRecord(
        run_id="run-abc123",
        issue_num=1578,
        branch="agent/issue-1578",
        status="success",
        exit_code=0,
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 21, 12, 30, tzinfo=UTC),
        duration_seconds=1800.0,
        total_tokens=50_000,
        input_tokens=40_000,
        output_tokens=10_000,
        cache_read_tokens=30_000,
        estimated_cost_usd=1.23456,
        attempt_number=2,
        pr_url="https://github.com/o/r/pull/9",
        pr_merged_at="2026-08-21T13:00:00Z",
        review_cycles=1,
        runner_name="claude-code",
        model="claude-opus-5",
        title="Add the sink",
        complexity="medium",
        repo="o/r",
        run_kind="dispatch",
    )


def _sparse_record() -> RunRecord:
    # Only the constructor-required fields — the under-populated-RunRecord
    # reality the exporter must render honestly (omit, never fabricate).
    return RunRecord(
        run_id="run-sparse",
        issue_num=0,
        branch="",
        status="failure",
        exit_code=1,
        started_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )


class _StubStore:
    """Honors the issue_num/item_id filters the way the real store does, so
    the exporter's scoping logic is actually exercised."""

    def __init__(self, events: list[AuditEvent] | None = None, *, raises: bool = False):
        self._events = events or []
        self._raises = raises

    def list_audit_events(self, limit=None, *, issue_num=None, item_id=None, **_kwargs):
        if self._raises:
            raise RuntimeError("db locked")
        events = self._events
        if issue_num is not None:
            events = [e for e in events if e.issue_num == issue_num]
        if item_id is not None:
            events = [e for e in events if e.item_id == item_id]
        return events[:limit] if limit else events


def _events_for(run_id: str, issue_num: int = 1578) -> list[AuditEvent]:
    # Real kew event names: dispatch_completed + the imported runner evidence.
    return [
        AuditEvent(
            id=7,
            timestamp="t",
            event="dispatch_completed",
            run_id=run_id,
            issue_num=issue_num,
        ),
        AuditEvent(
            id=9,
            timestamp="t",
            event="evidence.runner_progress",
            run_id=run_id,
            issue_num=issue_num,
        ),
        AuditEvent(
            id=8,
            timestamp="t",
            event="dispatch_completed",
            run_id="other-run",
            issue_num=issue_num,
        ),
        AuditEvent(
            id=6,
            timestamp="t",
            event="dispatch_completed",
            run_id=run_id,
            issue_num=42,
        ),
    ]


def _split_fences(content: str) -> list[str]:
    # Split on fence LINES only — a "---" inside a quoted scalar is legal
    # YAML and must not terminate the frontmatter.
    import re

    return re.split(r"^---$", content, flags=re.MULTILINE)


def _indexer_rewrite(path: Path, frontmatter: dict) -> str:
    """Rewrite ``path`` exactly as basic-memory does: python-frontmatter strips
    the body on read, then dump_frontmatter writes block-style YAML, a blank
    line, and the stripped body with no trailing newline."""
    body = _split_fences(path.read_text(encoding="utf-8"))[2].strip()
    dumped = yaml.dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        Dumper=yaml.SafeDumper,
    )
    path.write_text(f"---\n{dumped}---\n\n{body}", encoding="utf-8")
    return dumped


def _frontmatter(path: Path) -> dict:
    parsed = yaml.safe_load(_split_fences(path.read_text(encoding="utf-8"))[1])
    assert isinstance(parsed, dict)
    return parsed


def _report_path(tmp_path: Path) -> Path:
    return tmp_path / "sink" / "o--r" / "run-abc123.md"


def _write_current_report(tmp_path: Path, evidence: list[str] | None = None) -> Path:
    path = _report_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_report(_full_record(), "o/r", evidence or []), encoding="utf-8"
    )
    return path


def _replace_frontmatter(path: Path, frontmatter: str) -> None:
    parts = _split_fences(path.read_text(encoding="utf-8"))
    path.write_text(f"---{frontmatter}---{parts[2]}", encoding="utf-8")


def test_disabled_writes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    assert export_run_report(config, _StubStore(), _full_record()) is None
    assert not (tmp_path / "sink").exists()


def test_full_record_report_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _StubStore(_events_for("run-abc123"))
    path = export_run_report(config, store, _full_record())
    assert path == tmp_path / "sink" / "o--r" / "run-abc123.md"
    fm = _frontmatter(path)
    assert fm["run_id"] == "run-abc123"
    assert fm["repo"] == "o/r"
    assert fm["issue"] == 1578
    assert fm["complexity"] == "medium"
    assert fm["model"] == "claude-opus-5"
    assert fm["cost_usd"] == pytest.approx(1.23456)
    assert isinstance(fm["cost_usd"], float)
    assert fm["attempts"] == 2
    assert fm["review_cycles"] == 1
    # merged beats the raw status in the retrieval-facing outcome
    assert fm["outcome"] == "merged"
    assert fm["status"] == "success"
    assert fm["schema"] == 1
    # only this run's + this issue's chain-attested events, real event names
    assert fm["evidence"] == [
        "audit:7:dispatch_completed",
        "audit:9:evidence.runner_progress",
    ]


def test_unchanged_reexport_leaves_file_untouched(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _StubStore(_events_for("run-abc123"))
    path = export_run_report(config, store, _full_record())
    original = path.read_bytes()
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()

    assert export_run_report(config, store, _full_record()) == path

    after = path.stat()
    assert path.read_bytes() == original
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_indexer_rewritten_frontmatter_is_current(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _StubStore(_events_for("run-abc123"))
    path = export_run_report(config, store, _full_record())
    parts = _split_fences(path.read_text(encoding="utf-8"))
    frontmatter = yaml.safe_load(parts[1])
    frontmatter["permalink"] = "kew-runs/o-r/run-abc123"
    _indexer_rewrite(path, frontmatter)
    indexed = path.read_bytes()
    assert not indexed.endswith(b"\n")  # the indexer drops the final newline
    before = path.stat()

    assert export_run_report(config, store, _full_record()) == path

    assert path.read_bytes() == indexed
    assert _frontmatter(path)["permalink"] == "kew-runs/o-r/run-abc123"
    assert path.stat().st_ino == before.st_ino


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unsupported")
def test_fifo_at_report_path_is_replaced_without_blocking(tmp_path: Path) -> None:
    path = _report_path(tmp_path)
    path.parent.mkdir(parents=True)
    try:
        os.mkfifo(path)
    except OSError as exc:
        pytest.skip(f"FIFOs are unsupported: {exc}")
    results: list[Path | None] = []

    thread = threading.Thread(
        target=lambda: results.append(
            export_run_report(_config(tmp_path), _StubStore(), _full_record())
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "export blocked while probing a FIFO"
    assert results == [path]
    assert stat.S_ISREG(os.lstat(path).st_mode)
    assert _frontmatter(path)["run_id"] == "run-abc123"


def test_symlink_at_report_path_is_replaced_without_following(tmp_path: Path) -> None:
    path = _report_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "target.md"
    target.write_text(render_report(_full_record(), "o/r", []), encoding="utf-8")
    original_target = target.read_bytes()
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unsupported: {exc}")

    assert export_run_report(_config(tmp_path), _StubStore(), _full_record()) == path

    assert not path.is_symlink()
    assert stat.S_ISREG(os.lstat(path).st_mode)
    assert target.read_bytes() == original_target
    assert _frontmatter(path)["run_id"] == "run-abc123"


def test_hard_link_at_report_path_is_replaced(tmp_path: Path) -> None:
    path = _report_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "target.md"
    target.write_text(render_report(_full_record(), "o/r", []), encoding="utf-8")
    original_target = target.read_bytes()
    try:
        os.link(target, path)
    except OSError as exc:
        pytest.skip(f"hard links are unsupported: {exc}")

    assert export_run_report(_config(tmp_path), _StubStore(), _full_record()) == path

    assert path.stat().st_nlink == 1
    assert path.stat().st_ino != target.stat().st_ino
    assert target.read_bytes() == original_target


def test_oversized_current_report_is_rewritten(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write_current_report(tmp_path)
    exported_prefix = "- **exported_at**: "
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    oversized_line = (
        exported_prefix + '"' + "x" * run_report_sink._MAX_REPORT_BYTES + '"\n'
    )
    oversized = "".join(
        oversized_line if line.startswith(exported_prefix) else line for line in lines
    )
    path.write_text(oversized, encoding="utf-8")
    assert path.stat().st_size > run_report_sink._MAX_REPORT_BYTES
    before = path.stat()

    assert export_run_report(config, _StubStore(), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    assert path.stat().st_size <= run_report_sink._MAX_REPORT_BYTES


def test_missing_frontmatter_key_rewrites_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write_current_report(tmp_path)
    parts = _split_fences(path.read_text(encoding="utf-8"))
    frontmatter = "".join(
        line
        for line in parts[1].splitlines(keepends=True)
        if not line.startswith("exported_at: ")
    )
    _replace_frontmatter(path, frontmatter)
    before = path.stat()

    assert export_run_report(config, _StubStore(), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    assert "exported_at" in _frontmatter(path)


def test_duplicated_frontmatter_key_rewrites_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write_current_report(tmp_path)
    parts = _split_fences(path.read_text(encoding="utf-8"))
    _replace_frontmatter(path, f'{parts[1]}run_id: "duplicate"\n')
    before = path.stat()

    assert export_run_report(config, _StubStore(), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    frontmatter = _split_fences(path.read_text(encoding="utf-8"))[1]
    assert sum(line.startswith("run_id:") for line in frontmatter.splitlines()) == 1


def test_truncated_frontmatter_rewrites_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write_current_report(tmp_path)
    parts = _split_fences(path.read_text(encoding="utf-8"))
    frontmatter = "".join(
        line
        for line in parts[1].splitlines(keepends=True)
        if not line.startswith("repo: ")
    )
    _replace_frontmatter(path, frontmatter)
    before = path.stat()

    assert export_run_report(config, _StubStore(), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    assert _frontmatter(path)["repo"] == "o/r"


def test_indexer_block_evidence_list_is_current(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events = [
        AuditEvent(
            id=index,
            timestamp="t",
            event=f"evidence.long_{index}_{'x' * 80}",
            run_id="run-abc123",
            issue_num=1578,
        )
        for index in range(20)
    ]
    store = _StubStore(events)
    evidence = [f"audit:{event.id}:{event.event}" for event in events]
    path = _write_current_report(tmp_path, evidence)
    parts = _split_fences(path.read_text(encoding="utf-8"))
    frontmatter = yaml.safe_load(parts[1])
    frontmatter["permalink"] = "kew-runs/o-r/run-abc123"
    dumped = _indexer_rewrite(path, frontmatter)
    assert "\nevidence:\n- " in f"\n{dumped}"
    indexed = path.read_bytes()
    before = path.stat()

    assert export_run_report(config, store, _full_record()) == path

    assert path.read_bytes() == indexed
    assert path.stat().st_ino == before.st_ino

    parts = _split_fences(path.read_text(encoding="utf-8"))
    without_evidence_key = "".join(
        line
        for line in parts[1].splitlines(keepends=True)
        if line.rstrip() != "evidence:"
    )
    _replace_frontmatter(path, without_evidence_key)
    malformed = path.stat()

    assert export_run_report(config, store, _full_record()) == path
    assert path.stat().st_ino != malformed.st_ino
    assert _frontmatter(path)["evidence"] == evidence


def test_changed_outcome_rewrites_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    record = _full_record()
    record.pr_merged_at = None
    record.pr_state = "open"
    path = export_run_report(config, _StubStore(), record)
    before = path.stat()

    record.pr_merged_at = "2026-08-21T13:00:00Z"
    record.pr_state = "merged"
    assert export_run_report(config, _StubStore(), record) == path

    assert path.stat().st_ino != before.st_ino
    assert _frontmatter(path)["outcome"] == "merged"


def test_changed_evidence_rewrites_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events = _events_for("run-abc123")
    path = export_run_report(config, _StubStore(events[:1]), _full_record())
    before = path.stat()

    assert export_run_report(config, _StubStore(events), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    assert _frontmatter(path)["evidence"] == [
        "audit:7:dispatch_completed",
        "audit:9:evidence.runner_progress",
    ]


def test_schema_change_rewrites_report(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    path = export_run_report(config, _StubStore(), _full_record())
    before = path.stat()
    monkeypatch.setattr(run_report_sink, "REPORT_SCHEMA_VERSION", 2)

    assert export_run_report(config, _StubStore(), _full_record()) == path

    assert path.stat().st_ino != before.st_ino
    assert _frontmatter(path)["schema"] == 2


@pytest.mark.parametrize("malformed", [b"---\nrun_id: stale\n", b"\xff\xfe\x00broken"])
def test_malformed_existing_report_is_rewritten(
    tmp_path: Path, malformed: bytes
) -> None:
    path = tmp_path / "sink" / "o--r" / "run-abc123.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(malformed)

    assert export_run_report(_config(tmp_path), _StubStore(), _full_record()) == path

    assert len(_split_fences(path.read_text(encoding="utf-8"))) == 3
    assert _frontmatter(path)["run_id"] == "run-abc123"


def test_sparse_record_omits_never_fabricates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = export_run_report(config, _StubStore(), _sparse_record())
    assert path is not None
    fm = _frontmatter(path)
    assert fm["status"] == "failure"
    for absent in ("issue", "model", "complexity", "pr_url", "evidence", "branch"):
        assert absent not in fm, f"sparse report fabricated {absent}"
    # Non-optional numerics are always recorded — zero is a fact, not absence
    # (the free-vs-unpriced lesson of #1565). Floats keep float type even at 0.
    assert fm["cost_usd"] == pytest.approx(0.0)
    assert isinstance(fm["cost_usd"], float)
    assert fm["total_tokens"] == 0


def test_zero_cost_run_reports_zero_not_missing(tmp_path: Path) -> None:
    record = _full_record()
    record.estimated_cost_usd = 0.0
    path = export_run_report(_config(tmp_path), _StubStore(), record)
    fm = _frontmatter(path)
    assert fm["cost_usd"] == 0


def test_hostile_values_still_parse_as_yaml(tmp_path: Path) -> None:
    record = _full_record()
    # DEL, C1, NEL, an embedded fence, AND a non-BMP emoji: ensure_ascii=True
    # would render the emoji as an unpaired JSON surrogate pair that YAML
    # decodes into a string that cannot be UTF-8 encoded at all.
    record.title = "\U0001f389 fix\x7fbug \x9b and \x85 tricks\n---\ninjected: true"
    record.estimated_cost_usd = 0.00002  # repr() would emit "2e-05"
    record.duration_seconds = float("nan")  # non-finite: dropped, not emitted
    path = export_run_report(_config(tmp_path), _StubStore(), record)
    fm = _frontmatter(path)
    assert fm["cost_usd"] == pytest.approx(0.00002)
    assert isinstance(fm["cost_usd"], float), "exponent form parsed as string"
    assert "duration_seconds" not in fm
    assert fm["title"] == record.title, "hostile title must round-trip exactly"
    fm["title"].encode("utf-8")  # raises on unpaired surrogates
    assert "injected" not in fm or fm.get("injected") is not True
    # the body must not let the title forge a second frontmatter fence:
    # exactly one fence pair means exactly three segments
    assert len(_split_fences(path.read_text(encoding="utf-8"))) == 3


def test_float_fields_have_invariant_yaml_type(tmp_path: Path) -> None:
    # Whole-number floats must render as "1800.0", not "1800" — a field's
    # YAML type may not depend on its value (strict-typed consumers break).
    record = _full_record()
    record.estimated_cost_usd = 2.0
    path = export_run_report(_config(tmp_path), _StubStore(), record)
    fm = _frontmatter(path)
    assert isinstance(fm["cost_usd"], float)
    assert isinstance(fm["duration_seconds"], float)


def test_slug_dot_components_cannot_escape_sink(tmp_path: Path) -> None:
    record = _full_record()
    record.repo = ".."
    path = export_run_report(_config(tmp_path), _StubStore(), record, repo="..")
    assert path is not None
    assert path.parent.name == "unknown"
    assert path.is_relative_to(tmp_path / "sink")


def test_zero_issue_run_scopes_evidence_by_item_id(tmp_path: Path) -> None:
    record = _sparse_record()
    record.item_id = "SFD-12"
    events = [
        AuditEvent(
            id=3,
            timestamp="t",
            event="dispatch_completed",
            run_id="run-sparse",
            issue_num=0,
            item_id="SFD-12",
        ),
        AuditEvent(id=4, timestamp="t", event="dispatch_completed", run_id="x"),
    ]
    path = export_run_report(_config(tmp_path), _StubStore(events), record)
    assert _frontmatter(path)["evidence"] == ["audit:3:dispatch_completed"]


def test_zero_issue_no_item_gets_no_global_window(tmp_path: Path) -> None:
    # No issue and no item: an unscoped store-wide slice would mislead, so the
    # exporter must emit no evidence at all rather than a truncated window.
    events = [
        AuditEvent(id=3, timestamp="t", event="dispatch_completed", run_id="run-sparse")
    ]
    path = export_run_report(_config(tmp_path), _StubStore(events), _sparse_record())
    assert "evidence" not in _frontmatter(path)


def test_audit_failure_still_exports(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = export_run_report(config, _StubStore(raises=True), _full_record())
    assert path is not None
    assert "evidence" not in _frontmatter(path)


def test_export_failure_is_non_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # Occupy the sink path with a file so mkdir fails inside the exporter.
    (tmp_path / "sink").write_text("not a directory", encoding="utf-8")
    assert export_run_report(config, _StubStore(), _full_record()) is None


def test_config_without_run_reports_is_non_fatal() -> None:
    # A duck-typed config with no run_reports attribute must not raise —
    # the settings read is inside the exporter's try by contract.
    assert export_run_report(object(), _StubStore(), _full_record()) is None


def test_render_report_body_is_mechanical() -> None:
    content = render_report(_full_record(), "o/r", ["audit:7:dispatch_completed"])
    assert content.startswith("---\n")
    assert "# Run run-abc123" in content
    assert "## Attested events" in content
    assert "- audit:7:dispatch_completed" in content


def test_sink_path_expands_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = DispatchConfig(
        run_reports=RunReportsConfig(enabled=True, sink_path="~/runs-sink")
    )
    path = export_run_report(config, _StubStore(), _full_record())
    assert path is not None
    assert path.is_relative_to(tmp_path / "runs-sink")


def test_sync_outcomes_reexports_with_settled_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    """The production re-export site: after set_outcome writes pr_merged_at,
    the report on disk must say outcome: merged."""
    from kew import outcomes

    record = _full_record()
    record.pr_merged_at = None
    record.pr_state = "open"

    class _SyncStore(_StubStore):
        def list_syncable_runs(self, *, item_ids=None):
            return [record]

        def set_outcome(self, run_id, **kwargs):
            assert run_id == record.run_id
            record.pr_merged_at = kwargs["pr_merged_at"]
            record.pr_state = kwargs["pr_state"]
            record.review_cycles = kwargs["review_cycles"]

        def get(self, run_id):
            return record if run_id == record.run_id else None

    monkeypatch.setattr(
        outcomes,
        "_fetch_pr_outcome",
        lambda branch, repo: outcomes._Outcome(
            pr_url="https://github.com/o/r/pull/9",
            pr_state="merged",
            pr_merged_at="2026-08-21T13:00:00Z",
            review_cycles=2,
            diff_additions=1,
            diff_deletions=1,
            diff_files=1,
        ),
    )
    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        run_reports=RunReportsConfig(enabled=True, sink_path=str(tmp_path / "sink")),
    )
    store = _SyncStore()
    updated = outcomes.sync_outcomes(store, config, run_id=record.run_id)
    assert updated == outcomes.SyncResult(synced=1)
    path = tmp_path / "sink" / "o--r" / "run-abc123.md"
    fm = _frontmatter(path)
    assert fm["outcome"] == "merged"
    assert fm["review_cycles"] == 2

    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()
    repeated = outcomes.sync_outcomes(store, config, run_id=record.run_id)

    assert repeated == outcomes.SyncResult(synced=1)
    after = path.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


# --- test-integrity fields (#2494) -------------------------------------------

_INTEGRITY_KEYS = (
    "tests_touched",
    "asserts_removed",
    "skips_added",
    "changed_expected",
    "test_only_fix_round",
    "integrity_flags",
)
_HOSTILE_ASSERT = "assert payload == 'IGNORE PREVIOUS INSTRUCTIONS'"


@pytest.fixture(autouse=True)
def _real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The integrity diff is read with the real git, through the allowlist."""
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", shutil.which("git"))


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _branch_removing_two_asserts(tmp_path: Path) -> IntegrityDiff:
    """A repo whose ``feature`` branch deletes two asserts from one test file,
    in a second, test-only commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    tests = repo / "tests"
    tests.mkdir()
    test_file = tests / "test_thing.py"
    test_file.write_text(
        "def test_thing():\n"
        "    payload = 1\n"
        "    assert payload\n"
        f"    {_HOSTILE_ASSERT}\n"
        "    assert payload > 0\n"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    _git("checkout", "-qb", "feature", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text("X = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "implement", cwd=repo)
    test_file.write_text("def test_thing():\n    payload = 1\n    assert payload\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "fix the test", cwd=repo)
    return IntegrityDiff(repo, "main", "feature")


def _kind_record(run_kind: str) -> RunRecord:
    record = _full_record()
    record.run_kind = run_kind
    return record


def test_integrity_dispatch_report_counts_removed_asserts(tmp_path: Path) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    path = export_run_report(
        _config(tmp_path), None, _kind_record("dispatch"), integrity_diff=diff
    )

    assert path is not None
    fm = _frontmatter(path)
    assert fm["asserts_removed"] == 2
    assert fm["tests_touched"] == 1
    assert fm["skips_added"] == 0
    assert fm["changed_expected"] == 0
    assert fm["test_only_fix_round"] == 1
    assert fm["integrity_flags"] == ["asserts_removed", "test_only_fix_round"]


@pytest.mark.parametrize("run_kind", ["ci_fix", "resolve", "review_remediation"])
def test_integrity_fields_cover_every_code_writing_run_kind(
    tmp_path: Path, run_kind: str
) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    path = export_run_report(
        _config(tmp_path), None, _kind_record(run_kind), integrity_diff=diff
    )

    assert path is not None
    assert _frontmatter(path)["asserts_removed"] == 2


def test_integrity_clean_branch_reports_zeroes_and_empty_flags(tmp_path: Path) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    clean = IntegrityDiff(diff.checkout, "main", "main")
    path = export_run_report(
        _config(tmp_path), None, _kind_record("dispatch"), integrity_diff=clean
    )

    assert path is not None
    fm = _frontmatter(path)
    assert {k: fm[k] for k in _INTEGRITY_KEYS} == {
        "tests_touched": 0,
        "asserts_removed": 0,
        "skips_added": 0,
        "changed_expected": 0,
        "test_only_fix_round": 0,
        "integrity_flags": [],
    }


def test_integrity_adversarial_review_report_has_no_integrity_fields(
    tmp_path: Path,
) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    path = export_run_report(
        _config(tmp_path),
        None,
        _kind_record("adversarial_review"),
        integrity_diff=diff,
    )

    assert path is not None
    content = path.read_text()
    assert not set(_INTEGRITY_KEYS) & set(_frontmatter(path))
    assert not any(key in content for key in _INTEGRITY_KEYS)


def test_integrity_report_carries_no_diff_text(tmp_path: Path) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    path = export_run_report(
        _config(tmp_path), None, _kind_record("dispatch"), integrity_diff=diff
    )

    assert path is not None
    content = path.read_text()
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in content
    assert "assert payload" not in content
    assert "test_thing" not in content
    fm = _frontmatter(path)
    for key in _INTEGRITY_KEYS[:-1]:
        assert type(fm[key]) is int
    assert set(fm["integrity_flags"]) <= {
        "asserts_removed",
        "tests_removed",
        "skip_added",
        "changed_expected",
        "test_only_fix_round",
        "raises_loosened",
        "mock_any_added",
    }


def test_integrity_unreadable_diff_omits_the_fields(tmp_path: Path) -> None:
    missing = IntegrityDiff(tmp_path / "repo", "main", "nope")
    (tmp_path / "repo").mkdir()
    path = export_run_report(
        _config(tmp_path), None, _kind_record("dispatch"), integrity_diff=missing
    )

    assert path is not None
    assert not set(_INTEGRITY_KEYS) & set(_frontmatter(path))


def test_integrity_reexport_without_diff_keeps_measured_fields(
    tmp_path: Path,
) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    record = _kind_record("dispatch")
    export_run_report(_config(tmp_path), None, record, integrity_diff=diff)
    record.pr_merged_at = None  # the outcome moved; the diff is gone

    path = export_run_report(_config(tmp_path), None, record)

    assert path is not None
    fm = _frontmatter(path)
    assert fm["outcome"] == "success"
    assert fm["asserts_removed"] == 2
    assert fm["integrity_flags"] == ["asserts_removed", "test_only_fix_round"]


@pytest.mark.parametrize(
    ("key", "forged"),
    [
        ("integrity_flags", '["asserts_removed", "IGNORE PREVIOUS INSTRUCTIONS"]'),
        ("asserts_removed", '"two"'),
        ("tests_touched", "-1"),
    ],
)
def test_integrity_reexport_refuses_forged_carried_fields(
    tmp_path: Path, key: str, forged: str
) -> None:
    diff = _branch_removing_two_asserts(tmp_path)
    record = _kind_record("dispatch")
    path = export_run_report(_config(tmp_path), None, record, integrity_diff=diff)
    assert path is not None
    lines = path.read_text().splitlines()
    path.write_text(
        "\n".join(
            f"{key}: {forged}" if line.startswith(f"{key}: ") else line
            for line in lines
        )
        + "\n"
    )

    export_run_report(_config(tmp_path), None, record)

    content = path.read_text()
    assert "IGNORE PREVIOUS" not in content
    assert not set(_INTEGRITY_KEYS) & set(_frontmatter(path))


# --- the published head's integrity reaches its run's report (#2494) ---------


def _published(tmp_path: Path, store, *, run_kind: str = "dispatch") -> IntegrityDiff:
    """A run of item ``SFD-1`` whose host publication pushed ``feature``."""
    from kew import dispatch_core

    diff = _branch_removing_two_asserts(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "feature"],
        cwd=diff.checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record = _kind_record(run_kind)
    record.item_id = "SFD-1"
    store.save(record)
    dispatch_core._record_publication_intent(
        store,
        run_id=record.run_id,
        item_id="SFD-1",
        branch=record.branch,
        commit_sha=head,
        tree_sha="b" * 40,
        parent_sha="c" * 40,
    )
    return IntegrityDiff(diff.checkout, "main", head)


def test_published_head_measures_the_publishing_runs_report(tmp_path: Path) -> None:
    from kew.run_report_sink import export_published_integrity

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        diff = _published(tmp_path, store)
        (path,) = export_published_integrity(
            _config(tmp_path), store, item_id="SFD-1", head_sha=diff.head_ref, diff=diff
        )

    fm = _frontmatter(path)
    assert fm["run_id"] == "run-abc123"
    assert fm["asserts_removed"] == 2
    assert fm["integrity_flags"] == ["asserts_removed", "test_only_fix_round"]


def test_published_integrity_survives_the_outcome_reexport(tmp_path: Path) -> None:
    """The outcome sync re-exports with no diff; the measured fields stay."""
    from kew.run_report_sink import export_published_integrity

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        diff = _published(tmp_path, store)
        export_published_integrity(
            _config(tmp_path), store, item_id="SFD-1", head_sha=diff.head_ref, diff=diff
        )
        record = store.get("run-abc123")
        assert record is not None
        path = export_run_report(_config(tmp_path), store, record)

    assert path is not None
    assert _frontmatter(path)["asserts_removed"] == 2


def test_unpublished_head_writes_no_report(tmp_path: Path) -> None:
    from kew.run_report_sink import export_published_integrity

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        diff = _published(tmp_path, store)
        written = export_published_integrity(
            _config(tmp_path), store, item_id="SFD-1", head_sha="f" * 40, diff=diff
        )
        other_item = export_published_integrity(
            _config(tmp_path), store, item_id="SFD-2", head_sha=diff.head_ref, diff=diff
        )

    assert written == other_item == []
    assert not (tmp_path / "sink").exists()


def test_published_head_of_a_non_code_run_writes_no_report(tmp_path: Path) -> None:
    from kew.run_report_sink import export_published_integrity

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        diff = _published(tmp_path, store, run_kind="adversarial_review")
        written = export_published_integrity(
            _config(tmp_path), store, item_id="SFD-1", head_sha=diff.head_ref, diff=diff
        )

    assert written == []
