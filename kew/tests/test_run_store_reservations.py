"""Parent-pinned reservation admission and reconciliation regressions."""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from kew.config import DispatchConfig
from kew.graph.driver.tick import reconcile_epic_reservations, resolve_reservation_run
from kew.run_store import RunRecord, SqliteRunStore
from kew.run_store_work import capture_dispatch_reservation


def _supported_kwargs(fn, **kwargs):
    """Exercise old admission behavior when verifying these regressions on the base."""
    return {
        key: value
        for key, value in kwargs.items()
        if key in inspect.signature(fn).parameters
    }


def _admit_child(store, reservation, run_id, *, pinned):
    row = RunRecord(
        run_id=run_id,
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        started_at=datetime.now(UTC),
        status="pending",
        exit_code=-1,
    )
    assert store.admit_run_with_identity(
        row,
        store.build_admission_identity(
            run_id, tracker="github", item_id="42", repo=None
        ),
        reservation=reservation,
        **_supported_kwargs(store.admit_run_with_identity, reservation_pinned=pinned),
    )
    return row


def test_unpinned_binding_is_explicit(tmp_path):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "owner")
        reservation = capture_dispatch_reservation(store, "alpha", "42")
        _admit_child(store, reservation, "direct-child", pinned=False)
        [event] = store.list_audit_events(event="dispatch_reservation_bound")
        assert event.payload.get("pinned") is False


@pytest.mark.parametrize("pinned_first", [True, False])
@pytest.mark.parametrize("direct_id", ["direct-child", "owner"])
def test_unpinned_binding_never_displaces_pinned_child(
    tmp_path, pinned_first, direct_id
):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "owner")
        [reservation] = store.list_epic_reservations("alpha")
        children = [("pinned-child", True), (direct_id, False)]
        for run_id, pinned in children if pinned_first else reversed(children):
            row = _admit_child(store, reservation, run_id, pinned=pinned)
            if not pinned:
                # An unpinned terminal child must not release the pinned live child.
                row.started_at = datetime.now(UTC) + timedelta(seconds=10)
                row.status = "refused"
                row.finished_at = datetime.now(UTC)
                store.save(row)
        assert resolve_reservation_run(store, reservation).run_id == "pinned-child"
        assert reconcile_epic_reservations(
            "alpha",
            store,
            DispatchConfig(),
            now=reservation.reserved_at + timedelta(seconds=1),
        ) == {"42"}


@pytest.mark.parametrize(
    "current,expected_generation", [(None, "A"), ("B", "A"), ("A", "wrong")]
)
def test_capture_rejects_missing_or_replaced_parent(
    tmp_path, current, expected_generation
):
    from kew.run_store_work import AdmissionIdentityConflictError

    with SqliteRunStore(tmp_path / "runs.db") as store:
        if current:
            assert store.reserve_epic_node_item("alpha", "42", current)
        with pytest.raises(AdmissionIdentityConflictError, match="reservation changed"):
            capture_dispatch_reservation(
                store,
                "alpha",
                "42",
                **_supported_kwargs(
                    capture_dispatch_reservation,
                    expected_reservation_run_id="A",
                    expected_reservation_generation=expected_generation,
                ),
            )


def test_unpinned_readmission_cannot_demote_pinned_child(tmp_path):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "owner")
        [reservation] = store.list_epic_reservations("alpha")
        _admit_child(store, reservation, "pinned-child", pinned=True)
        _admit_child(store, reservation, "pinned-child", pinned=False)
        _admit_child(store, reservation, "direct-child", pinned=False)
        assert resolve_reservation_run(store, reservation).run_id == "pinned-child"


def test_round15_refusal_writer_rechecks_owner_inside_transaction(
    tmp_path, monkeypatch
):
    from kew.cli.epic import _record_dispatch_child_refusal

    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "A")
        original = store.try_admit_run

        def replace_before_transaction(*args, **kwargs):
            store.release_epic_node_item("42", run_id="A")
            assert store.reserve_epic_node_item("alpha", "42", "B")
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "try_admit_run", replace_before_transaction)
        _record_dispatch_child_refusal(
            store,
            DispatchConfig(),
            "alpha",
            "42",
            "dispatch_child_refused",
            **_supported_kwargs(
                _record_dispatch_child_refusal, expected_reservation_run_id="A"
            ),
        )
        [row] = store.list_by_item("42")
        assert row.error_category == "reservation_replaced"
        assert row.run_id not in {"A", "B"}
        [reservation] = store.list_epic_reservations("alpha")
        assert reservation.run_id == "B"
        assert resolve_reservation_run(store, reservation) is None
        _admit_child(store, reservation, "child-B", pinned=True)
        assert resolve_reservation_run(store, reservation).run_id == "child-B"


def test_round15_owner_release_logs_mismatch_without_deleting(tmp_path, caplog):
    import logging

    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "B")
        with caplog.at_level(logging.DEBUG, logger="kew.run_store"):
            store.release_epic_node_item("42", run_id="A")
        assert [r.run_id for r in store.list_epic_reservations("alpha")] == ["B"]
        assert "Reservation release skipped" in caplog.text
        store.release_epic_node_item("42", run_id="B")
        assert store.list_epic_reservations("alpha") == []


@pytest.mark.policy
def test_round16_every_production_release_supplies_run_id():
    import ast
    from pathlib import Path

    import kew

    missing = []
    for module in Path(kew.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "release_epic_node_item"
                and not any(keyword.arg == "run_id" for keyword in node.keywords)
            ):
                missing.append(f"{module.name}:{node.lineno}")
    assert missing == []


def test_round16_refusal_writer_checks_generation(tmp_path):
    from kew.dispatch_preflight import _record_refusal_row

    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "42", "A")
        row = _record_refusal_row(
            store,
            DispatchConfig(),
            "alpha",
            42,
            "42",
            error_category="budget_exceeded",
            expected_reservation_run_id="A",
            **_supported_kwargs(
                _record_refusal_row, expected_reservation_generation="wrong"
            ),
        )
        assert row.error_category == "reservation_replaced"
        assert row.run_id != "A"
        assert row.exit_code == 2
        assert store.get("A") is None
