"""`kew rename-repo` moves a project's stored rows from an old repository slug
to the configured ``project.repo`` without rewriting the audit chain (#3527).

Each test builds a real managed ledger holding rows under the old slug ``o/old``
in every column the command rewrites, and rows of an unrelated repository
``o/other`` that must never move. ``gh`` is answered at the process boundary by
a fake GitHub that knows only the new repository.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import GhProcessBoundary
from typer.testing import CliRunner

from kew import drive, subprocess_utils
from kew.cli import app
from kew.graph.driver.lock import single_driver_lock
from kew.ledger_identity import open_managed_store
from kew.run_report_rename import RenamedValues, move_repo_reports
from kew.run_report_sink import render_report
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()

OLD = "o/old"
NEW = "o/new"
OTHER = "o/other"
R_OLD = "11111111-1111-4111-8111-111111111111"
R_OTHER = "22222222-2222-4222-8222-222222222222"
_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

#: Every (table, column) the command rewrites, and what the old row reads after.
REWRITTEN = {
    ("runs", "repo"): NEW,
    ("run_launches", "repo"): NEW,
    ("run_admission_identity", "delivery_repo"): NEW,
    ("loops", "repo"): NEW,
    ("merge_queue", "repo"): NEW,
    ("merge_queue_suspects", "repo"): NEW,
    ("publication_job", "delivery_repo"): NEW,
    ("human_review_policy", "delivery_repo"): NEW,
}
UNTOUCHED = ("audit_events", "epic_ticks", "artifact_receipt", "outcome_sync_jobs")


class FakeGitHub:
    """GitHub as ``gh api repos/<slug>`` answers: the named repos exist."""

    def __init__(self, repos: dict[str, str]) -> None:
        self.repos = repos
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[1] == "api" and cmd[2].startswith("repos/"):
            full_name = self.repos.get(cmd[2].removeprefix("repos/"))
            if full_name is not None:
                body = json.dumps({"full_name": full_name, "private": True})
                return subprocess.CompletedProcess(cmd, 0, body, "")
        return subprocess.CompletedProcess(cmd, 1, "", "gh: Not Found (HTTP 404)")


class Site:
    def __init__(self, root: Path, forge: FakeGitHub) -> None:
        self.root = root
        self.forge = forge
        # The project is the CLI's project root (conftest), so `kew audit`
        # finds the ledger this site registers.
        self.project = root
        self.logs = self.project / "logs"
        self.db = self.logs / "dispatch.db"
        self.sink = root / "sink"
        self.anchors = root / "anchors.jsonl"

    @contextmanager
    def store(self) -> Iterator[SqliteRunStore]:
        store = open_managed_store(
            project_root=self.root,
            db_path=self.db,
            anchors_path=self.root / "audit-anchors.jsonl",
        )
        try:
            yield store
        finally:
            store.close()

    def sql(
        self, statement: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def dump(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def table(self, name: str) -> list[tuple[Any, ...]]:
        return self.sql(f"SELECT * FROM {name} ORDER BY rowid")  # noqa: S608


def _write_config(site: Site, repo: str = NEW) -> None:
    (site.project / "kew.toml").write_text(
        f'[project]\nrepo = "{repo}"\n'
        f'[logging]\ndir = "{site.logs}"\n'
        f'[run_reports]\nenabled = true\nsink_path = "{site.sink}"\n'
    )


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    # GitHub redirects the renamed repository's old name to the new one.
    forge = FakeGitHub({NEW: NEW, OLD: NEW})
    built = Site(tmp_path, forge)
    built.logs.mkdir(parents=True)
    _write_config(built)
    monkeypatch.chdir(built.project)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The forge answers every `gh` argv; this stub only lets preflight find one.
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    with built.store() as store:
        _seed(store)
    _seed_raw(built)
    return built


def _run(run_id: str, repo: str, pr: int, *, status: str = "success") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=pr // 10,
        item_id=str(pr // 10),
        branch=f"agent/issue-{pr // 10}",
        status=status,
        exit_code=0,
        started_at=_NOW,
        finished_at=_NOW + timedelta(minutes=5),
        repo=repo,
        pr_url=f"https://github.com/{repo}/pull/{pr}",
        run_kind="dispatch",
    )


def _seed(store: SqliteRunStore) -> None:
    """Rows written through the store's own writers."""
    store.save(_run(R_OLD, OLD, 50))
    store.save(_run(R_OTHER, OTHER, 60))
    for run_id in (R_OLD, R_OTHER):
        store.register_ci_fix_attempt(run_id, 50 if run_id == R_OLD else 60)
        store.record_launch(run_id)
    for repo in (OLD, OTHER):
        entry, _ = store.enqueue_merge(
            repo, 50, "a" * 40, session="s", behavior_changes=()
        )
        assert store.settle_merge_entry(entry.id, "merged", "landed")
        store.suspect_merge_queue_main(repo, "main moved")
        store.append_audit_event(
            timestamp=_NOW.isoformat(),
            event="dispatch_started",
            run_id=R_OLD if repo == OLD else R_OTHER,
            repo=repo,
            actor_type="policy",
            actor_id="test",
            payload={"repo": repo},
        )
    store.append_epic_tick(
        epic_ref="epic:demo",
        tick_ts=_NOW.isoformat(),
        snapshot={"schema": 1, "epic_ref": "epic:demo", "repo": OLD, "nodes": []},
        actions={"schema": 1, "dispatched": [], "merged": [], "held": []},
        snapshot_hash="h-demo",
    )


def _seed_raw(site: Site) -> None:
    """Rows of the tables with no small public writer, inserted as stored."""
    stamp = _NOW.isoformat()
    for run_id, repo, pr in ((R_OLD, OLD, 51), (R_OTHER, OTHER, 61)):
        site.sql(
            "INSERT INTO run_admission_identity (run_id, attempt_number,"
            " source_tracker, item_id, delivery_repo, recorded_at)"
            " VALUES (?, 1, 'github', '5', ?, ?)",
            (run_id, repo, stamp),
        )
        site.sql(
            "INSERT INTO loops (loop_id, source, repo, goal_text, created_at,"
            " status) VALUES (?, 'issue', ?, 'goal', ?, 'achieved')",
            (f"loop-{repo}", repo, stamp),
        )
        site.sql(
            "INSERT INTO publication_job (job_id, lineage_key, source_kind,"
            " source_id, run_id, attempt_number, branch, base_sha, tree_sha,"
            " receipt_digest, state, created_at, updated_at, delivery_repo)"
            " VALUES (?, ?, 'run', ?, ?, 1, 'b', 'x', 'y', 'z', 'reconciled',"
            " ?, ?, ?)",
            (f"job-{repo}", f"lk-{repo}", run_id, run_id, stamp, stamp, repo),
        )
        site.sql(
            "INSERT INTO human_review_policy (delivery_repo, generation,"
            " max_pending_reviews, max_diff_lines, max_paths, set_at)"
            " VALUES (?, 1, 1, 1, 1, ?)",
            (repo, stamp),
        )
        site.sql(
            "INSERT INTO orphan_prs (pr_number, url, branch, reason, detected_at)"
            " VALUES (?, ?, 'b', 'r', ?)",
            (pr, f"https://github.com/{repo}/pull/{pr}", stamp),
        )


def _rename(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["rename-repo", "--from", OLD, *args])
    return result.exit_code, result.output


def _report(site: Site, repo: str, run_id: str) -> Path:
    return site.sink / repo.replace("/", "--") / f"{run_id}.md"


def _old_report_text(site: Site) -> str:
    """The old run's report as the sink holds it: measured integrity, the
    attested evidence ids, and a ``permalink`` an indexer added."""
    with site.store() as store:
        record = store.get(R_OLD)
    assert record is not None
    integrity = {
        "tests_touched": 3,
        "asserts_removed": 1,
        "skips_added": 0,
        "changed_expected": 2,
        "test_only_fix_round": 0,
        "integrity_flags": ["asserts_removed"],
    }
    text = render_report(record, OLD, ["101", "102"], integrity)
    return text.replace("---\n", "---\npermalink: kew-runs/old-run\n", 1) + (
        # Prose a person added, naming the old slug: never a field to move.
        f"\nNotes: see https://github.com/{OLD}/pull/50 on {OLD}.\n"
        f'- **repo**: "{OLD}" (as first recorded)\n'
    )


def _write_old_report(site: Site) -> Path:
    path = _report(site, OLD, R_OLD)
    path.parent.mkdir(parents=True)
    path.write_text(_old_report_text(site))
    return path


def _moved_text(original: str) -> str:
    """*original* with exactly its repo and PR link fields moved: the two
    frontmatter values and the two body bullets that render them."""
    old_pr = f'"https://github.com/{OLD}/pull/50"'
    new_pr = f'"https://github.com/{NEW}/pull/50"'
    head, sep, body = original.partition("\n---\n")
    head = head.replace(f'\nrepo: "{OLD}"\n', f'\nrepo: "{NEW}"\n')
    head = head.replace(f"\npr_url: {old_pr}\n", f"\npr_url: {new_pr}\n")
    body = body.replace(f'\n- **repo**: "{OLD}"\n', f'\n- **repo**: "{NEW}"\n')
    body = body.replace(f"\n- **pr_url**: {old_pr}\n", f"\n- **pr_url**: {new_pr}\n")
    return head + sep + body


def test_dry_run_counts_each_table_and_writes_nothing(site: Site) -> None:
    _write_old_report(site)
    before = site.dump()

    code, output = _rename()

    assert code == 0, output
    assert "runs.repo: 1" in output
    assert "runs.pr_url: 1" in output
    assert "orphan_prs.url: 1" in output
    assert "human_review_policy.delivery_repo: 1" in output
    assert "1 run report" in output
    for table in UNTOUCHED:
        assert table in output
    assert "Dry run" in output
    assert site.dump() == before
    assert _report(site, OLD, R_OLD).exists()
    assert ["gh", "api", f"repos/{NEW}"] in site.forge.calls


def test_apply_rewrites_exactly_the_listed_columns_and_keeps_the_chain(
    site: Site,
) -> None:
    untouched = {table: site.table(table) for table in UNTOUCHED}
    anchored = runner.invoke(app, ["audit", "anchor", "--anchors", str(site.anchors)])
    assert anchored.exit_code == 0, anchored.output

    code, output = _rename("--apply")

    assert code == 0, output
    for (table, column), expected in REWRITTEN.items():
        values = [row[0] for row in site.sql(f"SELECT {column} FROM {table}")]  # noqa: S608
        assert sorted(values) == sorted([expected, OTHER]), (table, column)
    assert sorted(r[0] for r in site.sql("SELECT pr_url FROM runs")) == [
        f"https://github.com/{NEW}/pull/50",
        f"https://github.com/{OTHER}/pull/60",
    ]
    assert sorted(r[0] for r in site.sql("SELECT url FROM orphan_prs")) == [
        f"https://github.com/{NEW}/pull/51",
        f"https://github.com/{OTHER}/pull/61",
    ]
    after = {table: site.table(table) for table in UNTOUCHED}
    events = after.pop("audit_events")
    untouched_events = untouched.pop("audit_events")
    assert events[: len(untouched_events)] == untouched_events
    assert after == untouched
    renamed = site.sql(
        "SELECT repo, payload, id FROM audit_events WHERE event = 'repo_renamed'"
    )
    assert len(renamed) == 1
    assert len(events) == len(untouched_events) + 1
    repo, payload, event_id = renamed[0]
    assert repo == NEW
    body = json.loads(payload)
    assert body["from"] == OLD and body["to"] == NEW
    assert body["through_audit_id"] == event_id - 1
    assert body["rewritten"]["runs.repo"] == 1
    assert body["rewritten"]["orphan_prs.url"] == 1
    verify = runner.invoke(app, ["audit", "verify", "--anchors", str(site.anchors)])
    assert verify.exit_code == 0, verify.output


def test_a_second_apply_is_a_no_op(site: Site) -> None:
    assert _rename("--apply")[0] == 0
    before = site.dump()

    code, output = _rename("--apply")

    assert code == 0, output
    assert "nothing to do" in output.lower()
    assert site.dump() == before


def test_the_ci_fix_launch_count_carries_over(site: Site) -> None:
    with site.store() as store:
        assert store.count_launched_ci_fix_attempts(OLD, 50) == 1
        assert store.count_launched_ci_fix_attempts(NEW, 50) == 0

    assert _rename("--apply")[0] == 0

    with site.store() as store:
        assert store.count_launched_ci_fix_attempts(NEW, 50) == 1
        assert store.count_launched_ci_fix_attempts(OLD, 50) == 0
        assert store.count_launched_ci_fix_attempts(OTHER, 60) == 1


def test_the_sink_move_changes_only_the_repo_and_pr_link_values(site: Site) -> None:
    """The evidence ids, the indexer's permalink, the integrity fields and the
    rest of the body survive byte for byte; only the repo and PR link move, in
    the frontmatter and in the body bullets that render them."""
    old = _write_old_report(site)
    original = old.read_text()
    assert '\nevidence: ["101", "102"]\n' in original
    assert f'\n- **repo**: "{OLD}"\n' in original
    assert f'\n- **pr_url**: "https://github.com/{OLD}/pull/50"\n' in original

    code, output = _rename("--apply")

    assert code == 0, output
    assert not old.exists()
    moved = _report(site, NEW, R_OLD).read_text()
    assert moved == _moved_text(original)
    assert f'\n- **repo**: "{NEW}"\n' in moved
    assert f'\n- **pr_url**: "https://github.com/{NEW}/pull/50"\n' in moved
    assert f"\nNotes: see https://github.com/{OLD}/pull/50 on {OLD}.\n" in moved
    assert f'\n- **repo**: "{OLD}" (as first recorded)\n' in moved


def test_the_sink_move_keeps_quoting_and_leaves_other_repos_values(
    site: Site,
) -> None:
    """A report an indexer re-serialized keeps its quoting and line endings;
    a PR link of another repository is left alone, in the body too."""
    path = _report(site, OLD, R_OLD)
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\r\nrun_id: x\r\nrepo: o/old\r\n"
        f"pr_url: 'https://github.com/{OTHER}/pull/9'\r\n---\r\n\r\n"
        "- **repo**: o/old\r\n"
        f"- **pr_url**: 'https://github.com/{OTHER}/pull/9'\r\n"
    )

    assert _rename("--apply")[0] == 0

    assert (
        _report(site, NEW, R_OLD).read_bytes()
        == (
            "---\r\nrun_id: x\r\nrepo: o/new\r\n"
            f"pr_url: 'https://github.com/{OTHER}/pull/9'\r\n---\r\n\r\n"
            "- **repo**: o/new\r\n"
            f"- **pr_url**: 'https://github.com/{OTHER}/pull/9'\r\n"
        ).encode()
    )


def test_an_interrupted_move_finishes_on_the_next_apply(site: Site) -> None:
    """The move writes the target before it removes the source: a stop between
    the two leaves both, and the rerun removes the source."""
    old = _write_old_report(site)
    original = old.read_text()
    assert _rename("--apply")[0] == 0
    old.write_text(original)  # the source the interrupted move left behind

    code, output = _rename("--apply")

    assert code == 0, output
    assert not old.exists()
    assert _report(site, NEW, R_OLD).read_text() == _moved_text(original)


def test_a_move_an_older_kew_left_as_two_links_finishes(site: Site) -> None:
    old = _write_old_report(site)
    original = old.read_text()
    assert _rename("--apply")[0] == 0
    target = _report(site, NEW, R_OLD)
    target.unlink()
    old.write_text(original)
    os.link(old, target)  # linked to the target, never unlinked

    code, output = _rename("--apply")

    assert code == 0, output
    assert not old.exists()
    assert target.read_text() == _moved_text(original)


def test_a_rerun_after_old_slug_rows_reappear_refuses(site: Site) -> None:
    """Moving the new rows would append a second event; the first one stays
    the only cutoff."""
    assert _rename("--apply")[0] == 0
    with site.store() as store:
        store.save(_run("44444444-4444-4444-8444-444444444444", OLD, 80))
    before = site.dump()

    code, output = _rename("--apply")

    assert code == 2, output
    assert "already renamed" in output
    assert site.dump() == before


def test_an_apply_with_nothing_to_move_refuses(site: Site) -> None:
    """A mistyped --from writes no permanent event."""
    site.forge.repos["o/typo"] = NEW
    before = site.dump()

    result = runner.invoke(app, ["rename-repo", "--from", "o/typo", "--apply"])

    assert result.exit_code == 2, result.output
    assert "nothing holds o/typo" in result.output
    assert site.dump() == before


@pytest.mark.parametrize("answer", ["o/someone-else", None])
def test_an_old_name_github_does_not_redirect_to_the_new_one_refuses(
    site: Site, answer: str | None
) -> None:
    if answer is None:
        del site.forge.repos[OLD]
    else:
        site.forge.repos[OLD] = answer
    before = site.dump()

    dry_code, dry_output = _rename()
    code, output = _rename("--apply")

    assert dry_code == 0, dry_output
    assert "--apply would refuse now: GitHub does not show o/old" in dry_output
    assert code == 2, output
    assert "--from-name-reused" in output
    assert site.dump() == before


def test_from_name_reused_vouches_for_an_old_name_taken_since(site: Site) -> None:
    site.forge.repos[OLD] = "o/someone-else"

    code, output = _rename("--apply", "--from-name-reused")

    assert code == 0, output
    assert site.sql("SELECT repo FROM runs WHERE run_id = ?", (R_OLD,)) == [(NEW,)]


@pytest.mark.parametrize("slug", ["../..", "o/..", "./x", ".a/b", "o/-x", "o"])
def test_refuses_a_from_that_is_not_an_owner_name_slug(site: Site, slug: str) -> None:
    site.forge.repos[slug] = NEW
    before = site.dump()

    result = runner.invoke(app, ["rename-repo", "--from", slug, "--apply"])

    assert result.exit_code == 2, result.output
    assert "not an OWNER/NAME slug" in result.output
    assert site.dump() == before


def test_a_report_the_move_could_not_take_is_moved_by_the_next_apply(
    site: Site,
) -> None:
    old = _write_old_report(site)
    original = old.read_text()
    blocking = _report(site, NEW, R_OLD)
    blocking.parent.mkdir(parents=True)
    blocking.write_text("an unrelated file\n")

    code, output = _rename("--apply")

    assert code == 1, output
    assert old.exists()
    assert "already exists" in output
    assert site.sql("SELECT repo FROM runs WHERE run_id = ?", (R_OLD,)) == [(NEW,)]

    blocking.unlink()
    code, output = _rename("--apply")

    assert code == 0, output
    assert not old.exists()
    assert blocking.read_text() == _moved_text(original)
    assert (
        len(site.sql("SELECT id FROM audit_events WHERE event = 'repo_renamed'")) == 1
    )


def test_a_dry_run_names_what_would_refuse_the_apply(site: Site) -> None:
    _fresh_serve(site)
    before = site.dump()

    code, output = _rename()

    assert code == 0, output
    assert "--apply would refuse now: a serve process is live" in output
    assert site.dump() == before


def _live_drive(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        lease = drive.acquire_drive(store)
    assert isinstance(lease, drive.DriveLease)
    return nullcontext()


def _fresh_serve(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        store.set_meta("scheduler_heartbeat", datetime.now(UTC).isoformat())
    return nullcontext()


def _live_run(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        store.save(
            _run("33333333-3333-4333-8333-333333333333", OLD, 70, status="pending")
        )
    return nullcontext()


def _epic_lock(site: Site) -> AbstractContextManager[Any]:
    return single_driver_lock(site.logs / "epic-driver-epic-demo.lock", "epic:demo")


def _reservation(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        assert store.reserve_epic_node_item("epic:demo", "9", "temporary")
    return nullcontext()


def _merge_claim(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        store.take_merge_claim(50, ["a" * 40], owner="verify", now=datetime.now(UTC))
    return nullcontext()


def _queued_merge(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        store.enqueue_merge(OLD, 52, "c" * 40, session="s", behavior_changes=())
    return nullcontext()


def _pending_label_reset(site: Site) -> AbstractContextManager[Any]:
    with site.store() as store:
        store.append_audit_event(
            timestamp=_NOW.isoformat(),
            event="stop_label_reset_requested",
            run_id=R_OLD,
            repo=OLD,
            payload={"request_id": "req-1", "repo": OLD, "tracker": "github"},
        )
    return nullcontext()


def _both_slugs_hold_a_policy(site: Site) -> AbstractContextManager[Any]:
    site.sql(
        "INSERT INTO human_review_policy (delivery_repo, generation,"
        " max_pending_reviews, max_diff_lines, max_paths, set_at)"
        " VALUES (?, 1, 1, 1, 1, ?)",
        (NEW, _NOW.isoformat()),
    )
    return nullcontext()


#: When the stale loops on the live ledger last moved: long past the console's
#: staleness window.
_LONG_AGO = "2026-07-15T00:00:00+00:00"


def _add_loop(
    site: Site, loop_id: str, *, status: str, repo: str, created_at: str
) -> None:
    site.sql(
        "INSERT INTO loops (loop_id, source, repo, goal_text, created_at, status)"
        " VALUES (?, 'issue', ?, 'goal', ?, ?)",
        (loop_id, repo, created_at, status),
    )


def _fresh_pursuing_loop(site: Site) -> AbstractContextManager[Any]:
    now = datetime.now(UTC).isoformat()
    _add_loop(site, "loop-live", status="pursuing", repo=OLD, created_at=now)
    return nullcontext()


def _pursuing_loop_with_an_unreadable_stamp(
    site: Site,
) -> AbstractContextManager[Any]:
    """Quiet by its own stamps, but its newest iteration stamp is unreadable:
    that stamp could be the newest, so silence is not proven."""
    _add_loop(site, "loop-unread", status="pursuing", repo=OLD, created_at=_LONG_AGO)
    site.sql(
        "INSERT INTO iterations (iteration_id, loop_id, idx, started_at)"
        " VALUES ('it-1', 'loop-unread', 0, 'not-a-time')"
    )
    return nullcontext()


@pytest.mark.parametrize(
    ("blocker", "words"),
    [
        (_live_drive, "a drive holds its lock"),
        (_fresh_serve, "a serve process is live"),
        (_live_run, "runs in flight"),
        (_epic_lock, "an epic driver lock is held: epic-driver-epic-demo.lock"),
        (_reservation, "1 epic reservations held"),
        (_merge_claim, "1 unexpired merge claims"),
        (_queued_merge, "1 unsettled merge queue entries"),
        (_pending_label_reset, "1 pending stop-label resets"),
        (_both_slugs_hold_a_policy, "human_review_policy holds a row for both"),
        (_fresh_pursuing_loop, "a loop of o/old is pursuing: loop-live"),
        (
            _pursuing_loop_with_an_unreadable_stamp,
            "a loop of o/old is pursuing: loop-unread",
        ),
    ],
)
def test_each_refusal_writes_nothing(
    site: Site,
    blocker: Callable[[Site], AbstractContextManager[Any]],
    words: str,
) -> None:
    old = _write_old_report(site)
    with blocker(site):
        before = site.dump()
        code, output = _rename("--apply")
        after = site.dump()

    assert code == 2, output
    assert words in output
    assert after == before
    assert old.exists()


@pytest.mark.parametrize(
    ("loop_id", "status", "repo", "created_at"),
    [
        # Quiet past the console's staleness window: history, not a driver.
        ("loop-stale", "pursuing", OLD, _LONG_AGO),
        # Stopped on purpose; its row moves with the rest, so a resume acts on
        # the new name.
        ("loop-paused", "paused", OLD, None),
        # Another repository's loop, and one that recorded no repo.
        ("loop-other", "pursuing", OTHER, None),
        ("loop-no-repo", "pursuing", "", None),
    ],
)
def test_a_loop_that_is_not_driving_the_old_slug_does_not_refuse(
    site: Site, loop_id: str, status: str, repo: str, created_at: str | None
) -> None:
    stamp = created_at or datetime.now(UTC).isoformat()
    _add_loop(site, loop_id, status=status, repo=repo, created_at=stamp)

    code, output = _rename("--apply")

    assert code == 0, output
    expected = NEW if repo == OLD else repo
    assert site.sql("SELECT repo FROM loops WHERE loop_id = ?", (loop_id,)) == [
        (expected,)
    ]


def test_a_failure_part_way_rolls_back_everything_including_the_event(
    site: Site,
) -> None:
    # The last slug column the command rewrites fails its update: the event
    # and the slug columns before it were already written inside the
    # transaction, and the PR-link columns after it are never reached.
    site.sql(
        "CREATE TRIGGER fail_policy_update BEFORE UPDATE ON human_review_policy"
        " BEGIN SELECT * FROM no_such_table; END"
    )
    old = _write_old_report(site)
    before = site.dump()

    code, output = _rename("--apply")

    assert code != 0, output
    assert site.dump() == before
    assert old.exists()


def test_refuses_when_the_new_repository_is_not_on_github(site: Site) -> None:
    site.forge.repos.clear()
    before = site.dump()

    code, output = _rename("--apply")

    assert code == 2, output
    assert NEW in output
    assert site.dump() == before


@pytest.mark.parametrize("configured", ["", OLD])
def test_refuses_an_empty_or_unchanged_project_repo(
    site: Site, configured: str
) -> None:
    _write_config(site, configured)
    before = site.dump()

    code, output = _rename("--apply")

    assert code == 2, output
    assert site.dump() == before


def test_refuses_a_new_slug_that_differs_from_the_old_only_in_case(
    site: Site,
) -> None:
    """GitHub names ignore case, and so does the Mac's file system: the two
    sink directories would be one, and the move would delete every report."""
    _write_config(site, "o/OLD")
    site.forge.repos["o/OLD"] = "o/OLD"
    site.forge.repos[OLD] = "o/OLD"
    old = _write_old_report(site)
    before = site.dump()

    code, output = _rename("--apply")

    assert code == 2, output
    assert "only in case" in output
    assert site.dump() == before
    assert old.exists()


def test_a_move_into_the_directory_it_reads_keeps_every_report(
    tmp_path: Path,
) -> None:
    source = tmp_path / "o--old"
    source.mkdir()
    report = source / "r1.md"
    report.write_text('---\nrepo: "o/old"\n---\n')
    alias = tmp_path / "o--new"
    alias.symlink_to(source, target_is_directory=True)
    values = RenamedValues(
        OLD, NEW, f"https://github.com/{OLD}/pull/", f"https://github.com/{NEW}/pull/"
    )

    move = move_repo_reports(source, alias, values)

    assert move.moved == []
    assert len(move.kept) == 1
    assert "same directory" in move.kept[0]
    assert report.read_text() == '---\nrepo: "o/old"\n---\n'


def test_from_name_reused_moves_only_the_reports_of_this_ledgers_runs(
    site: Site,
) -> None:
    """Once another repository holds the old name, its reports can share the
    old slug's sink directory; only this ledger's runs are vouched for."""
    site.forge.repos[OLD] = "o/someone-else"
    ours = _write_old_report(site)
    original = ours.read_text()
    theirs = _report(site, OLD, "33333333-3333-4333-8333-333333333333")
    theirs.write_text(f'---\nrepo: "{OLD}"\n---\n')

    code, output = _rename("--apply", "--from-name-reused")

    assert code == 1, output
    assert _report(site, NEW, R_OLD).read_text() == _moved_text(original)
    assert not ours.exists()
    assert theirs.read_text() == f'---\nrepo: "{OLD}"\n---\n'
    assert "not a run of this ledger" in output


def test_a_dry_run_names_an_old_slug_nothing_holds(site: Site) -> None:
    site.forge.repos["o/typo"] = NEW

    result = runner.invoke(app, ["rename-repo", "--from", "o/typo"])

    assert result.exit_code == 0, result.output
    assert "--apply would refuse now: nothing holds o/typo" in result.output


def test_a_dry_run_after_the_rename_lists_no_blocker_the_report_move_ignores(
    site: Site,
) -> None:
    _write_old_report(site)
    assert _rename("--apply")[0] == 0
    _fresh_serve(site)

    code, output = _rename()

    assert code == 0, output
    assert "would refuse" not in output
