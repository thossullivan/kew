"""Exercise retained Linear claims through the detached child command."""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_remediation_flow_gate import pinned_dispatch_scene as pinned_dispatch_scene
from typer.testing import CliRunner

from kew import dispatch_core as core
from kew.cli._apps import epic_app
from kew.run_store import SqliteRunStore


class RecordingLinearClient:
    """Keep remote issue versions and mutation order independent of the tracker."""

    def __init__(self, store):
        self.store = store
        self.issue = {
            "id": "issue-uuid",
            "identifier": "SFD-42",
            "title": "Claim compensation",
            "updatedAt": "version-0",
            "state": {"id": "ready", "type": "unstarted"},
            "assignee": None,
        }
        self.operations = []
        self.mutations = []
        self.queries = []
        self.claims = []
        self.after_claim = lambda: None

    def execute(self, query, variables=None):
        self.queries.append(query)
        if "IssueUpdate" in query:
            return self._mutate(variables)
        if "IssueForDispatch" in query:
            return {"issue": deepcopy(self.issue)}
        if "IssueClaimState" in query:
            # Round-18 baseline support; retained claims never make this read.
            self.operations.append("read")
            return {"issue": deepcopy(self.issue)}
        if "Viewer" in query:
            return {"viewer": {"id": "viewer"}}
        if "TeamStates" in query:
            return {
                "teams": {
                    "nodes": [
                        {
                            "states": {
                                "nodes": [
                                    {
                                        "id": "ready",
                                        "name": "Todo",
                                        "type": "unstarted",
                                    },
                                    {
                                        "id": "started",
                                        "name": "In Progress",
                                        "type": "started",
                                    },
                                ]
                            }
                        }
                    ]
                }
            }
        raise AssertionError(f"Unexpected query: {query}")

    def _mutate(self, variables):
        fields = variables["input"]
        operation = "claim" if fields["assigneeId"] else "reset"
        self.operations.append(operation)
        self.mutations.append(operation)
        self.issue["updatedAt"] = f"version-{len(self.mutations)}"
        self.issue["state"] = {
            "id": fields["stateId"],
            "type": "started" if operation == "claim" else "unstarted",
        }
        self.issue["assignee"] = (
            {"id": fields["assigneeId"]} if fields["assigneeId"] else None
        )
        snapshot = deepcopy(self.issue)
        if operation == "claim":
            self._observe_admission()
            self.claims.append(snapshot)
            self.after_claim()
        return {"issueUpdate": {"success": True, "issue": snapshot}}

    def _observe_admission(self):
        # A second connection verifies admission committed before the mutation.
        with SqliteRunStore(self.store._db_path, read_only=True) as reader:
            [reservation] = reader.list_epic_reservations("alpha")
            bindings = reader.list_audit_events(event="dispatch_reservation_bound")
            [binding] = [
                event
                for event in bindings
                if event.payload["reservation_run_id"] == reservation.run_id
            ]
            assert reader.get(binding.run_id).status == "pending"


@pytest.fixture
def linear_child(pinned_dispatch_scene, monkeypatch):
    import kew.cli.epic as epic

    scene = pinned_dispatch_scene
    scene.config.tracker.type = "linear"
    scene.config.tracker.linear.team = "SFD"
    # The replacement B admits while the stale A's pending row is still live;
    # the cap counts runs (#2628), so it needs a second slot.
    scene.config.parallel.max_workers = 2
    client = RecordingLinearClient(scene.store)
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    monkeypatch.setattr("kew.linear.client.LinearClient", lambda *_: client)
    monkeypatch.setattr(epic, "activate_detached_publication_token", lambda: None)
    monkeypatch.setattr(epic, "_require_dispatch_child_runner", lambda *_: None)
    monkeypatch.setattr(epic._c, "preflight_tools", lambda: None)
    monkeypatch.setattr(epic._c, "load_config", lambda **_: scene.config)
    monkeypatch.setattr(epic._c, "build_notifier", lambda _: None)
    monkeypatch.setattr(epic._c, "_open_run_store", lambda _: nullcontext(scene.store))
    monkeypatch.setattr(epic._c, "execute_dispatch", core.execute_dispatch)
    monkeypatch.setattr(core, "_cleanup_dispatch_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "mark_failed", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "_compute_breaker_baseline", _over_cap)
    reservations = scene.store.list_epic_reservations

    def read_reservations(epic_ref):
        client.operations.append("reservation")
        return reservations(epic_ref)

    monkeypatch.setattr(scene.store, "list_epic_reservations", read_reservations)
    assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "A")
    return SimpleNamespace(
        store=scene.store,
        config=scene.config,
        client=client,
        invoke=_invoke_child,
        replace=lambda: _replace_reservation(scene.store),
    )


def _invoke_child(reservation="A", generation=None):
    return CliRunner().invoke(
        epic_app,
        [
            "_dispatch-node",
            "alpha",
            "SFD-42",
            "--reservation-run-id",
            reservation,
            "--reservation-generation",
            generation or reservation,
        ],
    )


def _replace_reservation(store):
    store.release_epic_node_item("SFD-42", run_id="A")
    assert store.reserve_epic_node_item("alpha", "SFD-42", "B")


def _exception_refusal(*_a, **_k):
    raise RuntimeError("post-admission refusal")


def _over_cap(*_a, **_k):
    raise core.WorkerCapReached(0)


def _start_child(run, **_kwargs):
    run.on_launch()
    raise SystemExit(0)


RECOVERY = (
    "The item stays assigned in its claim state. A replacement dispatch re-claims it. "
    "For manual recovery, unassign the item in Linear and move it back to its "
    "unstarted state."
)


def _retained(child):
    assert "reset" not in child.client.mutations
    [event] = child.store.list_audit_events(event="linear_claim_retained")
    assert event.payload["recovery"] == RECOVERY
    assert event.payload["reservation_run_id"] == "A"
    assert event.payload["reservation_generation"] == "A"
    assert child.store.list_audit_events(event="linear_claim_rollback") == []
    return event


def _replace_and_start(child, monkeypatch):
    child.replace()
    child.client.after_claim = lambda: None
    monkeypatch.setattr(core, "_compute_breaker_baseline", lambda _: (None, None))
    monkeypatch.setattr(core, "_run_retry_loop", _start_child)
    result = child.invoke("B")
    assert result.exit_code == 0, result.output
    monkeypatch.setattr(core, "_compute_breaker_baseline", _over_cap)


def test_round19_replacement_inside_reset_cannot_be_overwritten(
    linear_child, monkeypatch
):
    from kew.linear.tracker import LinearTracker

    child = linear_child
    reset_calls = []
    reset = LinearTracker.reset_to_ready

    def unsafe_reset(tracker, item_id):
        # On round 18, B wins AFTER A's last verification but BEFORE A's write.
        reset_calls.append(item_id)
        _replace_and_start(child, monkeypatch)
        reset(tracker, item_id)
        raise AssertionError("refusal must never call reset_to_ready")

    monkeypatch.setattr(LinearTracker, "reset_to_ready", unsafe_reset)
    monkeypatch.setattr(core, "_compute_breaker_baseline", _exception_refusal)
    result = child.invoke()
    assert reset_calls == [], "refusal must never call reset_to_ready"
    # A post-admission refusal never launched: public exit 2 (could not run).
    assert result.exit_code == 2, result.output
    _replace_and_start(child, monkeypatch)
    assert reset_calls == [], "refusal must never call reset_to_ready"
    assert child.client.issue == child.client.claims[1], "A must preserve B's claim"
    assert child.client.mutations == ["claim", "claim"]
    _retained(child)


def test_round19_refusal_retains_claim_and_warns_once(linear_child):
    child = linear_child
    result = child.invoke()
    # The worker-cap refusal never launched: public exit 2 (could not run).
    assert result.exit_code == 2, result.output
    assert child.client.issue == child.client.claims[0], "refusal must retain the claim"
    event = _retained(child)
    assert event.payload["reason"] == "worker cap reached during atomic admission"
    assert event.payload["updatedAt"] == "version-1"
    assert "SFD-42" in result.output
    assert " ".join(result.output.split()).count(RECOVERY) == 1


def test_round19_replacement_reclaims_retained_item_and_audits_both(
    linear_child, monkeypatch
):
    child = linear_child
    assert child.invoke().exit_code == 2  # refused before launch: could not run
    _retained(child)
    _replace_and_start(child, monkeypatch)
    assert child.client.mutations == ["claim", "claim"]
    assert child.client.issue == child.client.claims[1]
    claims = child.store.list_audit_events(event="linear_claim_completed")
    assert len(claims) == 2
    assert len({event.run_id for event in claims}) == 2
    assert {event.payload["updatedAt"] for event in claims} == {
        "version-1",
        "version-2",
    }
    assert {event.payload["assigneeId"] for event in claims} == {"viewer"}


@pytest.mark.parametrize("boundary", ["claim", "admission"])
def test_round18_replacement_claim_survives_stale_rollback(
    linear_child, monkeypatch, boundary
):
    child = linear_child
    if boundary == "claim":
        child.client.after_claim = lambda: _replace_and_start(child, monkeypatch)
    else:

        def after_admission(_run):
            _replace_and_start(child, monkeypatch)
            _over_cap()

        monkeypatch.setattr(core, "_compute_breaker_baseline", after_admission)
    result = child.invoke()
    assert result.exit_code == 2, result.output
    assert child.client.issue == child.client.claims[1]
    _retained(child)


@pytest.mark.parametrize(
    "edit", ["updatedAt", "assignee", "state", "missing_timestamp"]
)
def test_round18_operator_change_prevents_reset(linear_child, monkeypatch, edit):
    child = linear_child

    def edit_then_refuse(_run):
        changes = {
            "updatedAt": {"updatedAt": "operator-version"},
            "assignee": {"assignee": {"id": "operator"}},
            "state": {"state": {"id": "review", "type": "started"}},
            "missing_timestamp": {"updatedAt": None},
        }
        child.client.issue.update(changes[edit])
        _over_cap()

    monkeypatch.setattr(core, "_compute_breaker_baseline", edit_then_refuse)
    assert child.invoke().exit_code == 2  # refused before launch: could not run
    event = _retained(child)
    assert event.payload["updatedAt"] == "version-1"


def test_round19_audit_keeps_claim_snapshot_without_verification_reads(linear_child):
    child = linear_child
    assert child.invoke().exit_code == 2  # refused before launch: could not run
    [claim] = child.store.list_audit_events(event="linear_claim_completed")
    event = _retained(child)
    assert event.run_id == claim.run_id
    assert event.payload["updatedAt"] == claim.payload["updatedAt"] == "version-1"
    assert all("IssueClaimState" not in query for query in child.client.queries)
    mutation = next(query for query in child.client.queries if "IssueUpdate" in query)
    assert "updatedAt" in mutation
    assert "state { id" in mutation
    assert "assignee { id" in mutation


def test_round18_released_reservation_prevents_tracker_read(linear_child):
    child = linear_child
    child.client.after_claim = lambda: child.store.release_epic_node_item(
        "SFD-42", run_id="A"
    )
    result = child.invoke()
    assert result.exit_code == 2, result.output
    _retained(child)
    assert all("IssueClaimState" not in query for query in child.client.queries)


def test_round19_refuse_callback_precedes_terminal_evidence_and_release(
    linear_child, monkeypatch
):
    import kew.cli.epic as epic

    child = linear_child
    order = []
    execute = core.execute_dispatch
    record = core._record_refusal_row
    release = child.store.release_epic_node_item

    def recorded_execute(**kwargs):
        callback = kwargs["on_refuse"]

        def on_refuse(*args):
            order.append("on_refuse")
            assert all(
                row.status == "pending" for row in child.store.list_by_item("SFD-42")
            )
            assert child.store.list_epic_reservations("alpha")[0].run_id == "A"
            callback(*args)
            order.append("callback_finished")

        return execute(**{**kwargs, "on_refuse": on_refuse})

    def terminal(*args, **kwargs):
        row = record(*args, **kwargs)
        order.append("terminal_row")
        # Simulate a reconciler acting immediately on the published terminal row.
        child.store.release_epic_node_item("SFD-42", run_id="A")
        return row

    def released(*args, **kwargs):
        order.append("release")
        return release(*args, **kwargs)

    monkeypatch.setattr(core, "_compute_breaker_baseline", _exception_refusal)
    monkeypatch.setattr(epic._c, "execute_dispatch", recorded_execute)
    monkeypatch.setattr(core, "_record_refusal_row", terminal)
    monkeypatch.setattr(child.store, "release_epic_node_item", released)
    result = child.invoke()
    assert order == ["on_refuse", "callback_finished", "terminal_row", "release"]
    assert result.exit_code == 2, result.output
    events = child.store.list_audit_events()
    retained = next(event for event in events if event.event == "linear_claim_retained")
    completed = next(event for event in events if event.event == "dispatch_completed")
    assert retained.id < completed.id


@pytest.mark.policy
def test_round19_reset_has_no_automatic_production_callers():
    import ast
    from pathlib import Path

    import kew

    callers = []
    for path in Path(kew.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reset_to_ready"
            ):
                callers.append(f"{path.name}:{node.lineno}")
    assert callers == [], "reset_to_ready must remain an explicit operator primitive"


class Round20DelegationFlow:
    """Drive the real watcher and child against one recording Linear state."""

    def __init__(self, child, config_path):
        from kew.delegate.run import make_is_terminally_decided
        from kew.delegate.watch import DelegationWatcher, MetaCursorStore

        self.child = child
        self.config_path = config_path
        self.calls = []
        self.comments = []
        self.results = []
        self.time = 0.0
        self.pending_save = core._save_pending_run
        child.store.release_epic_node_item("SFD-42", run_id="A")
        self.watcher = DelegationWatcher(
            events=child.store,
            poll=self,
            cursor_store=MetaCursorStore(child.store),
            assignee_id="viewer",
            label="kew:ready",
            is_terminally_decided=make_is_terminally_decided(child.store, "alpha"),
            poll_interval_seconds=1,
            clock=self.clock,
        )

    def clock(self):
        self.time += 2
        return self.time

    def list_delegated(self, _assignee_id, _label):
        return ["SFD-42"]

    def fetch_live(self, _item_id):
        from kew.delegate.recognize import CandidateIssue

        issue = self.child.client.issue
        state = "Todo" if issue["state"]["type"] == "unstarted" else "In Progress"
        return CandidateIssue(
            assignee_id=(issue["assignee"] or {}).get("id"),
            labels=("kew:ready",),
            state=state,
            state_id=issue["state"]["id"],
        )

    def transition_in_progress(self, _item_id):
        # Model the old watcher's claim without assuming a pending row exists.
        self.child.client.operations.append("watcher_claim")
        self.child.client.issue["assignee"] = {"id": "viewer"}
        self.child.client.issue["state"] = {"id": "started", "type": "started"}

    def comment(self, item_id, body):
        self.comments.append((item_id, body))

    def dispatch(self, item_id):
        self.calls.append(item_id)
        [reservation] = self.child.store.list_epic_reservations("alpha")
        result = CliRunner().invoke(
            epic_app,
            [
                "_dispatch-node",
                "alpha",
                item_id,
                "--run-kind",
                "delegation",
                "--reservation-run-id",
                reservation.run_id,
                "--reservation-generation",
                reservation.run_id,
            ],
        )
        self.results.append(result)
        return True

    def save_pending(self, *args, **kwargs):
        self.child.client.operations.append("last_admission_check")
        self.pending_save(*args, **kwargs)
        self.child.client.operations.append("admission_committed")

    def tick(self):
        from kew.delegate.run import process_candidate

        [candidate] = self.watcher.tick()
        return process_candidate(
            candidate,
            member_id="viewer",
            label="kew:ready",
            store=self.child.store,
            tracker=self,
            sink=self,
            fleet_cap=4,
            dry_run=False,
            config_path=self.config_path,
            epic_ref="alpha",
        )


def _round20_budget_file(tmp_path, cap):
    path = tmp_path / "delegation.toml"
    path.write_text(
        f"[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = {cap}\n"
    )
    return path


def _round20_spend(store):
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    store.save(
        RunRecord(
            run_id="prior-spend",
            issue_num=99,
            branch="prior",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            estimated_cost_usd=50.0,
        )
    )


@pytest.mark.parametrize("assignee", [None, "viewer"])
def test_round20_watcher_budget_retry_claims_only_after_admission(
    linear_child, monkeypatch, tmp_path, assignee
):
    from kew.delegate.run import WatchOutcome

    child = linear_child
    child.client.issue["assignee"] = {"id": assignee} if assignee else None
    flow = Round20DelegationFlow(child, _round20_budget_file(tmp_path, 10.0))
    monkeypatch.setattr(core, "_save_pending_run", flow.save_pending)
    _round20_spend(child.store)
    original = deepcopy(child.client.issue)

    assert flow.tick() is WatchOutcome.REFUSED
    assert child.client.issue == original, "budget refusal must not claim Linear"
    assert flow.calls == []
    assert child.store.list_epic_reservations("alpha") == []
    assert child.store.list_audit_events(event="linear_claim_retained") == []

    _round20_budget_file(tmp_path, 2000.0)
    monkeypatch.setattr(core, "_compute_breaker_baseline", lambda _: (None, 0.0))
    monkeypatch.setattr(core, "_run_retry_loop", _start_child)
    assert flow.tick() is WatchOutcome.DISPATCHED
    assert flow.calls == ["SFD-42"]
    assert flow.results[0].exit_code == 0, flow.results[0].output
    assert child.client.issue["assignee"] == {"id": "viewer"}
    assert child.client.issue["state"]["type"] == "started"
    operations = [op for op in child.client.operations if op != "reservation"]
    assert operations == ["last_admission_check", "admission_committed", "claim"]
    [claim] = child.store.list_audit_events(event="linear_claim_completed")
    assert child.store.get(claim.run_id).run_kind == "delegation"


def test_round20_delegation_post_admission_refusal_retains_claim(
    linear_child, monkeypatch, tmp_path
):
    from kew.delegate.run import WatchOutcome

    child = linear_child
    child.client.issue["assignee"] = {"id": "viewer"}
    flow = Round20DelegationFlow(child, _round20_budget_file(tmp_path, 2000.0))
    monkeypatch.setattr(core, "_save_pending_run", flow.save_pending)
    monkeypatch.setattr(core, "_compute_breaker_baseline", _exception_refusal)
    assert flow.tick() is WatchOutcome.DISPATCHED
    # A post-admission refusal never launched: public exit 2 (could not run).
    assert flow.results[0].exit_code == 2, flow.results[0].output
    operations = [op for op in child.client.operations if op != "reservation"]
    assert operations == ["last_admission_check", "admission_committed", "claim"]
    assert child.client.mutations == ["claim"]
    assert child.client.issue == child.client.claims[0]
    [event] = child.store.list_audit_events(event="linear_claim_retained")
    assert event.payload["reason"] == "post-admission refusal"
    assert event.payload["recovery"] == RECOVERY
    assert " ".join(flow.results[0].output.split()).count(RECOVERY) == 1


def test_round20_recovery_commands_are_registered():
    import re

    from typer.main import get_command

    from kew.claim_retention import LINEAR_CLAIM_RECOVERY
    from kew.cli import app

    registered = get_command(app)
    for group, verbs in re.findall(r"\bkew\s+(\w+)\s+([\w/-]+)", LINEAR_CLAIM_RECOVERY):
        assert group in registered.commands
        for verb in verbs.split("/"):
            assert verb in registered.commands[group].commands, (
                f"unregistered: {group} {verb}"
            )
    assert "kew epic continue" not in LINEAR_CLAIM_RECOVERY
    assert "unassign the item in Linear" in LINEAR_CLAIM_RECOVERY
    assert "move it back to its unstarted state" in LINEAR_CLAIM_RECOVERY
    assert "A replacement dispatch re-claims it." in LINEAR_CLAIM_RECOVERY
