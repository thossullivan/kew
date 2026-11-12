"""`kew no-change` decides a no-change hold from the terminal (#2463).

Before this group existed, the only way out of a no-change hold was the web
console — and the console was refusing the repository's own owner, because
authority was read for the local OS username rather than the forge login. So
these tests assert both halves of the fix at the command boundary: the verb is
authorized through the forge identity, and the CLI reaches the same verb with
the same authority the web route does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, LoggingConfig
from kew.ledger_identity import open_managed_store
from kew.no_change import (
    COMPLETED,
    ITEM_CLOSED,
    ITEM_OPEN,
    PENDING_CONFIRMATION,
    REJECTED,
    ItemObservation,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
)

runner = CliRunner()

BASE = "a" * 40
PKG = "sha256:" + "c" * 64
CHECK = "sha256:" + "d" * 64
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
#: The operator names the work item; kew resolves the judgment that owns it.
ITEM = "2445"


class _Tracker:
    """The item adapter, remembering what the sync asked it to write."""

    def __init__(self) -> None:
        self.state = ITEM_OPEN
        self.writes: list[dict[str, Any]] = []

    def observe(self, *, marker: str) -> ItemObservation:
        return ItemObservation(available=True, state=self.state, marker_found=False)

    def apply(self, *, body: str, close: bool) -> bool:
        self.writes.append({"body": body, "close": close})
        self.state = ITEM_CLOSED if close else self.state
        return True


def _gh(*, login: str = "thossullivan", permission: str = "admin"):
    """A ``gh`` answering the login, permission and base reads, by argv."""

    def _answer(argv, **_: Any) -> MagicMock:
        if argv[:3] == ["gh", "api", "user"]:
            return MagicMock(returncode=0, stdout=login, stderr="")
        route = argv[2] if len(argv) > 2 else ""
        if "/collaborators/" in route:
            asked = route.split("/collaborators/")[1].split("/")[0]
            return MagicMock(
                returncode=0, stdout=permission if asked == login else "none", stderr=""
            )
        return MagicMock(returncode=0, stdout=BASE, stderr="")

    return patch("kew.outcomes.run", side_effect=_answer)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A project ledger at <tmp>/logs/dispatch.db, with load_config on it.

    No approvers are configured, which is the condition the reported failure
    needed: the permission rule is the one in force.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEW_ACTOR", "localuser")
    config = DispatchConfig(logging=LoggingConfig(dir=str(log_dir)))
    config.project.repo = "thossullivan/kew"
    config.human_review.approvers = []
    config.approval.approvers = []
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
    ):
        yield log_dir / "dispatch.db"


def _open(db_path: Path):
    root = db_path.parent.parent
    return open_managed_store(
        project_root=root,
        db_path=db_path,
        anchors_path=root / "audit-anchors.jsonl",
    )


def _pending(db_path: Path, item: str = ITEM) -> str:
    """One attested no-change candidate, waiting on a person."""
    with _open(db_path) as store:
        judgment = store.open_no_change_judgment(
            intent=NoChangeIntent(
                source_kind="github",
                source_id=item,
                run_id="run-" + item,
                attempt_number=1,
                base_sha=BASE,
                item_id=item,
            ),
            now=NOW,
        )
        store.attest_no_change(
            judgment_id=judgment.judgment_id,
            evidence=NoChangeEvidence(
                trusted_base_sha=BASE,
                observed_base_sha=BASE,
                package_digest=PKG,
                package_scope="workspace",
                package_entry_count=0,
                package_deletion_count=0,
                checks=(NamedCheck(name="pytest", outcome="passed", digest=CHECK),),
            ),
            now=NOW,
        )
        return store.decide_no_change(
            judgment_id=judgment.judgment_id, now=NOW
        ).judgment_id


def test_confirm_authorizes_through_the_forge_login_and_closes_the_item(ledger):
    """The OS user is `localuser`; the account holding admin is `thossullivan`."""
    judgment_id = _pending(ledger)
    tracker = _Tracker()

    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=tracker):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 0, result.output
    assert f"Confirmed {judgment_id}" in result.output
    assert tracker.writes[-1]["close"] is True
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).decided_by == "thossullivan"


def test_reject_records_the_reason_and_leaves_the_item_open(ledger):
    judgment_id = _pending(ledger)
    tracker = _Tracker()

    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=tracker):
        result = runner.invoke(
            app,
            ["no-change", "reject", ITEM, "--reason", "the design is wrong"],
        )

    assert result.exit_code == 0, result.output
    assert f"Rejected {judgment_id}" in result.output
    assert tracker.state == ITEM_OPEN
    assert tracker.writes[-1]["close"] is False
    assert "the design is wrong" in tracker.writes[-1]["body"]
    with _open(ledger) as store:
        judgment = store.get_no_change_judgment(judgment_id)
    assert judgment.state == REJECTED
    assert "the design is wrong" in judgment.decision_reason


def test_reject_without_a_reason_is_a_usage_error(ledger):
    judgment_id = _pending(ledger)

    result = runner.invoke(app, ["no-change", "reject", ITEM])

    assert result.exit_code == 2
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state != REJECTED


def test_reject_with_a_blank_reason_is_refused_and_decides_nothing(ledger):
    judgment_id = _pending(ledger)

    with _gh():
        result = runner.invoke(app, ["no-change", "reject", ITEM, "--reason", "   "])

    assert result.exit_code == 2
    assert "requires a reason" in result.output
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state != REJECTED


def test_retry_sync_completes_a_confirm_whose_source_write_held(ledger):
    """The recovery path: the tracker was unreachable, so the sync held."""
    judgment_id = _pending(ledger)
    # No adapter is an outage, not an error: the outcome stays sync-pending.
    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
        assert runner.invoke(app, ["no-change", "confirm", ITEM]).exit_code == 0
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state != COMPLETED

    tracker = _Tracker()
    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=tracker):
        result = runner.invoke(app, ["no-change", "retry-sync", ITEM])

    assert result.exit_code == 0, result.output
    assert f"Retried {judgment_id}" in result.output
    assert tracker.writes[-1]["close"] is True
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state == COMPLETED


def test_a_read_only_forge_login_is_refused_and_named(ledger):
    judgment_id = _pending(ledger)

    with _gh(login="drive-by", permission="read"):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert "drive-by has 'read' on thossullivan/kew" in result.output
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).decided_by is None


def test_an_item_with_no_waiting_outcome_says_so_rather_than_crashing(ledger):
    _pending(ledger)

    with _gh():
        result = runner.invoke(app, ["no-change", "confirm", "9999"])

    assert result.exit_code == 2
    assert "has no live no-change outcome" in result.output


def test_an_already_decided_item_is_not_decidable_a_second_time(ledger):
    """A settled judgment is not live, so there is nothing left to confirm."""
    judgment_id = _pending(ledger)
    tracker = _Tracker()
    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=tracker):
        assert (
            runner.invoke(
                app, ["no-change", "reject", ITEM, "--reason", "not the answer"]
            ).exit_code
            == 0
        )
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert "has no live no-change outcome" in result.output
    with _open(ledger) as store:
        assert store.get_no_change_judgment(judgment_id).state == REJECTED


def test_a_malformed_item_id_is_refused_before_any_ledger_read(ledger):
    _pending(ledger)

    with _gh():
        result = runner.invoke(app, ["no-change", "confirm", "../../etc/passwd"])

    assert result.exit_code == 2
    assert "is not canonical for provider" in result.output


def test_a_missing_ledger_names_the_path_it_looked_for(ledger):
    result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert "no ledger at" in result.output
    assert "dispatch.db" in result.output


def _no_change_events(db_path: Path) -> list[str]:
    """Every no-change event the chain carries, in the order it carries them."""
    with _open(db_path) as store:
        return [
            event.event
            for event in store.list_audit_events(limit=200, event_prefix="no_change")
        ]


def test_a_second_confirm_attests_nothing_for_a_decision_already_made(ledger):
    """A repeated `confirm` must not chain an event for a decision nobody made.

    The first confirmation is authorized locally and holds its source write —
    the tracker is out — which leaves the judgment live at `sync_pending`, so
    the item still resolves and the verb is reachable a second time. It is the
    state that refuses it, and it refuses BEFORE the attestation: the audit
    chain is the only durable account of why a no-change closed an item, and a
    `no_change_confirmed` event standing behind a confirmation that never
    happened is a false entry in it.
    """
    _pending(ledger)
    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
        assert runner.invoke(app, ["no-change", "confirm", ITEM]).exit_code == 0
    attested = _no_change_events(ledger)

    with _gh(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
        result = runner.invoke(app, ["no-change", "confirm", ITEM])

    assert result.exit_code == 2
    assert "is sync_pending, so there is nothing to confirm" in result.output
    assert _no_change_events(ledger) == attested == ["no_change_confirmed"]


def test_retry_sync_refuses_an_outcome_nobody_has_decided_yet(ledger):
    """`retry-sync` before a decision synchronizes nothing, so it says nothing.

    Sync jobs are enqueued by the confirmation, so a judgment still waiting on
    a person owns no synchronization to re-run. Reporting "Retried" over that
    absence told an operator their source write had been carried when nothing
    had been — and attested the retry into the chain on the way.
    """
    judgment_id = _pending(ledger)

    with _gh():
        result = runner.invoke(app, ["no-change", "retry-sync", ITEM])

    assert result.exit_code == 2
    assert "no synchronization to retry" in result.output
    assert _no_change_events(ledger) == []
    with _open(ledger) as store:
        judgment = store.get_no_change_judgment(judgment_id)
    assert judgment.state == PENDING_CONFIRMATION
    assert judgment.decided_by is None
