"""A launching dispatch records the item's acceptance criteria (#2290).

The host already reads the issue body to build the prompt, and the record is
written once the run id exists and before that prompt is built: the row and its
chain events name the run that read the body, and the prompt can name the sha
the ledger actually holds instead of asserting a write it never saw.
"""

from __future__ import annotations

import pytest

from kew import dispatch_core
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore
from kew.triage.plan import body_digest

_BODY = """### Change

- Record the plan for the run that reads the body.

### Acceptance

- The ledger has a row for the dispatched body.
- The prompt names the plan sha.
"""


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _prepare(store, body: str, *, run_id: str | None = None, num: int = 2290):
    return dispatch_core._prepare_dispatch_context(
        issue={
            "number": num,
            "title": "Record acceptance criteria",
            "body": body,
            "labels": [{"name": "ready"}],
        },
        config=DispatchConfig(),
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=None,
        item_id=None,
        provider="github",
        on_refuse=None,
        on_terminal_failure=None,
        run_kind="dispatch",
        worktree_dir=None,
        run_id=run_id,
        branch_override=None,
        allow_unrecorded=True,
    )


def _record(store, body: str, *, run_id: str | None = None, num: int = 2290):
    """Prepare a dispatch and run the plan-recording seam the launch calls."""
    ctx = _prepare(store, body, run_id=run_id, num=num)
    ctx.run_id = run_id
    dispatch_core._record_dispatch_plan(ctx)
    return ctx


@pytest.fixture(autouse=True)
def _no_checkout_capture(monkeypatch):
    monkeypatch.setattr(
        dispatch_core, "_capture_root_checkout_state", lambda *_a, **_k: None
    )


def test_the_launching_run_records_the_plan_for_its_body(store):
    ctx = _record(store, _BODY, run_id="run-1")

    assert ctx.issue_body == _BODY
    row = store.current_item_plan("2290")
    assert row is not None
    assert (row.body_digest, row.criteria_count) == (body_digest(_BODY), 2)
    assert (row.source, row.run_id) == ("extracted", "run-1")
    assert ctx.recorded_plan_sha == row.plan_sha


def test_preparing_the_context_records_nothing_on_its_own(store):
    """Admission mints no run id on the classic path, so it writes no plan."""
    _prepare(store, _BODY)

    assert store.current_item_plan("2290") is None


def test_the_classic_path_mints_the_run_id_the_plan_binds_to(store):
    """#2309 P2: no caller supplies a run id, so the row would record NULL."""
    ctx = _prepare(store, _BODY)
    assert ctx.run_id is None

    ctx.run_id = "minted-at-launch"
    dispatch_core._record_dispatch_plan(ctx)

    row = store.current_item_plan("2290")
    assert row is not None and row.run_id == "minted-at-launch"
    [event] = store.list_audit_events(limit=None, event="plan_recorded")
    assert event.run_id == "minted-at-launch"


def test_a_second_run_against_the_same_body_records_nothing_new(store):
    _record(store, _BODY, run_id="run-1")
    _record(store, _BODY, run_id="run-2")

    assert len(store.list_item_plans("2290")) == 1


def test_an_edited_body_supersedes_at_the_next_dispatch(store):
    _record(store, _BODY, run_id="run-1")
    _record(store, _BODY + "- A third criterion.\n", run_id="run-2")

    history = store.list_item_plans("2290")
    assert [row.criteria_count for row in history] == [2, 3]
    assert history[0].superseded_at is not None
    current = store.current_item_plan("2290")
    assert current is not None and current.criteria_count == 3


def test_a_dispatch_without_a_store_still_prepares_the_context():
    ctx = _record(None, _BODY, run_id="run-1")

    assert ctx.issue_body == _BODY
    assert ctx.recorded_plan_sha is None


def test_a_plan_write_failure_never_refuses_the_dispatch(store, monkeypatch, caplog):
    monkeypatch.setattr(
        store,
        "record_item_plan",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger is gone")),
    )

    ctx = _record(store, _BODY, run_id="run-1")

    assert ctx.issue_body == _BODY
    assert ctx.recorded_plan_sha is None
    assert "Could not record the acceptance criteria" in caplog.text
