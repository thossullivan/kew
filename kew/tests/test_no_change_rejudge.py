"""A no-change stays confirmable while main moves past none of its paths (#2828).

The judgment used to be bound to the exact base sha, so on a driven repo every
merge elsewhere made it stale, and the refusal named no way forward. These
tests build the repository with real commits: the judgment is made on main~4,
the forge answers main's current sha, and git in the project's repository says
what the commits since touched. Only ``gh`` is faked.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from kew import subprocess_utils
from kew.cli import app
from kew.ledger_identity import open_managed_store
from kew.no_change import (
    ITEM_CLOSED,
    ITEM_OPEN,
    PENDING_CONFIRMATION,
    ItemObservation,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
    judged_paths_from,
)
from kew.subprocess_utils import run as real_run

runner = CliRunner()

PKG = "sha256:" + "c" * 64
CHECK = "sha256:" + "d" * 64
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ITEM = "2445"
JUDGED = "src/app.py"


class _Tracker:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.state = ITEM_OPEN
        self.writes: list[dict[str, Any]] = []

    def observe(self, *, marker: str) -> ItemObservation:
        return ItemObservation(
            available=self.available, state=self.state, marker_found=False
        )

    def apply(self, *, body: str, close: bool) -> bool:
        self.writes.append({"body": body, "close": close})
        self.state = ITEM_CLOSED if close else self.state
        return True


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"touch {path}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The project's repository: main~4 is the judged base, four commits on top."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@example.invalid")
    _git(checkout, "config", "user.name", "t")
    _commit(checkout, JUDGED, "print('hello')\n")
    monkeypatch.setenv("KEW_PROJECT", str(checkout))
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)
    return checkout


def _project_ledger(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """*project*'s own kew.toml and ledger; ``ada`` is the named approver."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    project.mkdir(parents=True, exist_ok=True)
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{log_dir}"\n\n'
        '[human_review]\napprovers = ["ada"]\n'
    )
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_ACTOR", "ada")
    return log_dir / "dispatch.db"


@pytest.fixture
def ledger(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch):
    with patch("kew.cli.preflight_tools"):
        yield _project_ledger(tmp_path, repo, monkeypatch)


@pytest.fixture
def subdir_ledger(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch):
    """The project is ``proj/`` inside the repository, as kew's own is ``kew/``."""
    with patch("kew.cli.preflight_tools"):
        yield _project_ledger(tmp_path, repo / "proj", monkeypatch)


def _open(db_path: Path):
    root = db_path.parent.parent
    return open_managed_store(
        project_root=root,
        db_path=db_path,
        anchors_path=root / "audit-anchors.jsonl",
    )


def _forge(main: str, on_read: Callable[[], None] | None = None):
    """``gh`` answers main's sha; git runs for real in the project's repository.

    *on_read* runs when main's sha is read: somebody else moving the judgment
    in the moment between that read and the write.
    """

    def _answer(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        if argv[:3] == ["gh", "api", "user"]:
            return MagicMock(returncode=0, stdout="ada", stderr="")
        if on_read is not None:
            on_read()
        return MagicMock(returncode=0, stdout=main, stderr="")

    return patch("kew.outcomes.run", side_effect=_answer)


def _intent(base: str) -> NoChangeIntent:
    return NoChangeIntent(
        source_kind="github",
        source_id=ITEM,
        run_id="run-" + ITEM,
        attempt_number=1,
        base_sha=base,
        item_id=ITEM,
    )


def _pending(db_path: Path, base: str, judged: tuple[str, ...] = (JUDGED,)) -> str:
    with _open(db_path) as store:
        judgment = store.open_no_change_judgment(intent=_intent(base), now=NOW)
        store.attest_no_change(
            judgment_id=judgment.judgment_id,
            evidence=NoChangeEvidence(
                trusted_base_sha=base,
                observed_base_sha=base,
                package_digest=PKG,
                package_scope="workspace",
                package_entry_count=0,
                package_deletion_count=0,
                checks=(NamedCheck(name="pytest", outcome="passed", digest=CHECK),),
                judged_paths=judged,
            ),
            now=NOW,
        )
        return store.decide_no_change(
            judgment_id=judgment.judgment_id, now=NOW
        ).judgment_id


def _four_commits(repo: Path, *, touching: str | None = None) -> str:
    head = ""
    for n in range(4):
        path = touching if (touching and n == 2) else f"docs/note{n}.md"
        head = _commit(repo, path, f"change {n}\n")
    return head


def _base_of(db_path: Path, judgment_id: str) -> str:
    with _open(db_path) as store:
        return str(store.get_no_change_judgment(judgment_id).base_sha)


def _rejudged_events(db_path: Path) -> list[Any]:
    with _open(db_path) as store:
        return list(store.list_audit_events(event="no_change_rejudged"))


# --------------------------------------------------------------------------- #
# confirm
# --------------------------------------------------------------------------- #


def test_a_base_four_commits_on_that_touch_no_judged_path_confirms(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    assert _git(repo, "rev-parse", "HEAD~4") == judged_on
    judgment_id = _pending(ledger, judged_on)
    tracker = _Tracker()

    with (
        _forge(main),
        patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
    ):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 0, result.output
    assert f"Confirmed {judgment_id}" in result.output
    assert tracker.writes[-1]["close"] is True
    with _open(ledger) as store:
        [event] = store.list_audit_events(event="no_change_confirmed")
    assert event.payload["observed_base_sha"] == main


def test_a_commit_touching_a_judged_path_refuses_and_names_rejudge(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    judgment_id = _pending(ledger, judged_on)

    with _forge(main):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert JUDGED in result.output
    assert f"kew no-change rejudge {ITEM}" in result.output
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state == PENDING_CONFIRMATION
        assert store.list_audit_events(event="no_change_confirmed") == []


def test_a_judged_path_touched_is_seen_from_a_project_in_a_subdirectory(
    repo: Path, subdir_ledger: Path
) -> None:
    """Judged paths are repository-root-relative; git must read them there."""
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    _pending(subdir_ledger, judged_on)
    tracker = _Tracker()

    with (
        _forge(main),
        patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
    ):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2, result.output
    assert JUDGED in result.output
    assert tracker.writes == []


def test_a_main_that_rewrites_an_extensionless_file_the_item_names_is_stale(
    repo: Path, ledger: Path
) -> None:
    _commit(repo, "Makefile", "test:\n\tpytest\n")
    judged_on = _commit(repo, "docs/install.md", "Run make test.\n")
    judged = judged_paths_from(
        "The `Makefile` test target is broken; docs/install.md describes it.",
        _git(repo, "ls-tree", "-r", "--name-only", judged_on).splitlines(),
    )
    _pending(ledger, judged_on, judged=judged)
    assert "Makefile" in judged
    main = _commit(repo, "Makefile", "test:\n\tpytest -x\n")
    tracker = _Tracker()

    with (
        _forge(main),
        patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
    ):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2, result.output
    assert "touched Makefile" in result.output
    assert tracker.writes == []


def test_a_judgment_with_no_recorded_paths_stays_bound_to_its_exact_base(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    _pending(ledger, judged_on, judged=())

    with _forge(main):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert f"kew no-change rejudge {ITEM}" in result.output


def test_a_decided_outcome_gone_stale_names_reject_not_rejudge(
    repo: Path, ledger: Path
) -> None:
    """Rejudge admits only a pending judgment, so it is not the way forward
    for a synchronization that held on a base that moved afterwards."""
    judged_on = _git(repo, "rev-parse", "HEAD")
    judgment_id = _pending(ledger, judged_on)
    with (
        _forge(judged_on),
        patch(
            "kew.outcomes.no_change_item_adapter",
            return_value=_Tracker(available=False),
        ),
    ):
        held = runner.invoke(app, ["no-change", "confirm", ITEM])
    assert held.exit_code == 0, held.output
    with _open(ledger) as store:
        state = store.get_no_change_judgment(judgment_id).state
    assert state != PENDING_CONFIRMATION
    main = _four_commits(repo, touching=JUDGED)

    with (
        _forge(main),
        patch("kew.outcomes.no_change_item_adapter", return_value=_Tracker()),
    ):
        result = runner.invoke(app, ["no-change", "retry-sync", ITEM])

    assert f'kew no-change reject {ITEM} --reason "…"' in result.output
    assert "kew no-change rejudge" not in result.output


# --------------------------------------------------------------------------- #
# rejudge
# --------------------------------------------------------------------------- #


def test_rejudge_refuses_a_touched_judged_path_and_records_nothing(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    judgment_id = _pending(ledger, judged_on)

    with _forge(main):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 2
    assert JUDGED in result.output
    assert "--accept-changes" in result.output
    assert _base_of(ledger, judgment_id) == judged_on
    assert _rejudged_events(ledger) == []


def test_rejudge_with_accepted_changes_records_the_new_base_and_writes_no_run(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    judgment_id = _pending(ledger, judged_on)

    with _forge(main):
        result = runner.invoke(
            app,
            [
                "no-change",
                "rejudge",
                ITEM,
                "--accept-changes",
                "--reason",
                "the rewrite keeps the fix",
            ],
        )

    assert result.exit_code == 0, result.output
    assert main in result.output
    assert f"touched since the judged base: {JUDGED}" in result.output
    with _open(ledger) as store:
        judgment = store.get_no_change_judgment(judgment_id)
        assert judgment.base_sha == main
        assert judgment.state == PENDING_CONFIRMATION
        assert store.list_by_item(ITEM) == []
    [event] = _rejudged_events(ledger)
    assert event.payload["base_sha"] == judged_on
    assert event.payload["touched_paths"] == [JUDGED]
    assert event.payload["accepted_changes"] is True
    assert event.payload["reason"] == "the rewrite keeps the fix"
    assert event.payload["operator_actor"] == "ada"

    tracker = _Tracker()
    with (
        _forge(main),
        patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
    ):
        confirmed = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert confirmed.exit_code == 0, confirmed.output
    assert tracker.writes[-1]["close"] is True


def test_accepting_changes_without_a_reason_is_refused(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    judgment_id = _pending(ledger, judged_on)

    with _forge(main):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM, "--accept-changes"])

    assert result.exit_code == 2
    assert "--reason" in result.output
    assert _base_of(ledger, judgment_id) == judged_on


def test_rejudge_over_untouched_paths_records_the_base_and_says_none_touched(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    judgment_id = _pending(ledger, judged_on)

    with _forge(main):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 0, result.output
    assert "touched since the judged base: none" in result.output
    assert _base_of(ledger, judgment_id) == main
    [event] = _rejudged_events(ledger)
    assert event.payload["accepted_changes"] is False
    assert event.payload["touched_paths"] == []


def test_rejudge_refuses_a_main_that_does_not_descend_from_the_judged_base(
    repo: Path, ledger: Path
) -> None:
    root = _git(repo, "rev-parse", "HEAD")
    judged_on = _commit(repo, "docs/judged.md", "judged\n")
    _git(repo, "checkout", "-q", "--detach", root)
    rewritten = _commit(repo, "docs/other.md", "rewritten\n")
    judgment_id = _pending(ledger, judged_on)

    with _forge(rewritten):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 2
    assert "does not descend" in result.output
    assert _base_of(ledger, judgment_id) == judged_on


def test_rejudge_refuses_when_the_comparison_cannot_run(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    unfetched = "f" * 40
    judgment_id = _pending(ledger, judged_on)

    with _forge(unfetched):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 2
    assert "could not be compared" in result.output
    assert _base_of(ledger, judgment_id) == judged_on


def test_rejudge_with_no_judged_paths_counts_every_changed_file(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    _pending(ledger, judged_on, judged=())

    with _forge(main):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 2
    assert "docs/note0.md" in result.output


def test_a_rejudge_that_loses_the_race_attests_nothing(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    judgment_id = _pending(ledger, judged_on)
    elsewhere = _git(repo, "rev-parse", "HEAD~1")

    def somebody_else_rejudges() -> None:
        with _open(ledger) as store:
            store.rebase_no_change_judgment(
                judgment_id=judgment_id,
                expected_base_sha=judged_on,
                base_sha=elsewhere,
                now=NOW,
            )

    with _forge(main, on_read=somebody_else_rejudges):
        result = runner.invoke(app, ["no-change", "rejudge", ITEM])

    assert result.exit_code == 2
    assert _base_of(ledger, judgment_id) == elsewhere
    assert _rejudged_events(ledger) == []


def test_an_accepted_override_whose_audit_row_fails_exits_nonzero_and_says_so(
    repo: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is the only record of who overrode the re-check, and why."""
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo, touching=JUDGED)
    judgment_id = _pending(ledger, judged_on)
    # The ledger's own store class: the persistence boundary under the verb.
    with _open(ledger) as opened:
        ledger_store = type(opened)
    append = ledger_store.append_audit_event

    def full_disk(self: Any, **kwargs: Any) -> int | None:
        if kwargs.get("event") == "no_change_rejudged":
            raise sqlite3.OperationalError("database or disk is full")
        return append(self, **kwargs)

    monkeypatch.setattr(ledger_store, "append_audit_event", full_disk)
    with _forge(main):
        result = runner.invoke(
            app,
            [
                "no-change",
                "rejudge",
                ITEM,
                "--accept-changes",
                "--reason",
                "the rewrite keeps the fix",
            ],
        )

    assert result.exit_code == 2, result.output
    assert "audit row was not written" in result.output
    assert main in result.output
    monkeypatch.undo()
    assert _base_of(ledger, judgment_id) == main
    assert _rejudged_events(ledger) == []


def test_the_runs_own_intent_reopened_after_a_rejudge_converges(
    repo: Path, ledger: Path
) -> None:
    judged_on = _git(repo, "rev-parse", "HEAD")
    main = _four_commits(repo)
    judgment_id = _pending(ledger, judged_on)
    with _forge(main):
        assert runner.invoke(app, ["no-change", "rejudge", ITEM]).exit_code == 0

    with _open(ledger) as store:
        reopened = store.open_no_change_judgment(intent=_intent(judged_on), now=NOW)

    assert reopened.judgment_id == judgment_id
    assert reopened.base_sha == main


def test_evidence_without_judged_paths_serializes_as_it_always_did() -> None:
    evidence = NoChangeEvidence(
        trusted_base_sha="a" * 40,
        observed_base_sha="a" * 40,
        package_digest=PKG,
        package_scope="workspace",
        package_entry_count=0,
        package_deletion_count=0,
    )

    assert "judged_paths" not in evidence.to_json()
    with_paths = NoChangeEvidence.from_json(
        NoChangeEvidence(**{**evidence.__dict__, "judged_paths": (JUDGED,)}).to_json()
    )
    assert with_paths.judged_paths == (JUDGED,)
