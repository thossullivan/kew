"""Round 21 regressions against fc65ab2f; assertions name the missing behavior."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from functools import partial
from threading import Barrier
from unittest.mock import patch

import pytest
from test_w42_round17 import _record
from test_w42_round18 import _ci_fix_scene
from test_w42_round20 import ChildProcess, RemoteLabels, _process_scene, _remote_scene
from typer.testing import CliRunner

import kew.ci_fix as ci_fix
import kew.cli as cli
import kew.dispatch_core as core
import kew.orchestrator as orchestrator
import kew.sandbox as sandbox
from kew.dispatch_admission import PendingAdmission
from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore
from kew.tracking import DispatchResult


def _assert_receipt_transaction(sql):
    """Receipt settlement commits before the independent stop-label outbox work."""
    committed = sql.index("COMMIT")
    later = sql[committed + 1 :]
    assert sum(statement == "COMMIT" for statement in sql[: committed + 1]) == 1
    assert any("INSERT OR IGNORE INTO stop_label_outbox" in line for line in later)
    assert not any(
        line.lstrip().startswith(
            ("UPDATE runs", "INSERT INTO runs", "UPDATE run_launches")
        )
        for line in later
    )


def _receipt(store, run_id):
    row = store._conn.execute(
        "SELECT * FROM run_launches WHERE run_id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row is not None else {}


def _seed(store, run_id="retained", *, started=True):
    row = _record(run_id, kind="ci_fix", item="42")
    row.repo = "o/r"
    store.save(row)
    store.register_ci_fix_attempt(run_id, 87)
    if started:
        store.record_launch(run_id)
        record = store.get(run_id)
        record.status = "failure"
        store.save(record)


class FiniteChild(ChildProcess):
    """Run a real host process without invoking an agent or Docker."""

    def __init__(self, store, *, cidfile=None, output="", code=1):
        super().__init__()
        self.store = store
        self.cidfile = cidfile
        self.output = output
        self.code = code
        self.before_spawn = []
        self.returned = False

    def __call__(self, *args, **kwargs):
        self.before_spawn.append(self.store.count_launched_ci_fix_attempts("o/r", 87))
        if self.cidfile is not None:
            self.cidfile.write_text("a" * 64)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"print({self.output!r}); raise SystemExit({self.code})",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.returned = True
        return self.proc


class ReceiptWriter:
    def __init__(self, store, child, failures):
        self.original = store.record_launch
        self.child = child
        self.failures = failures
        self.calls = 0

    def __call__(self, run_id):
        assert self.child.returned, "Host receipts must follow Popen's return."
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("injected non-locking receipt failure")
        self.original(run_id)


def _container_scene(stack, child, tmp_path):
    cidfile = child.cidfile
    argv = [
        "docker",
        "run",
        "--name",
        "kew-round21",
        "--cidfile",
        str(cidfile),
        "-e",
        f"{sandbox.STARTUP_NONCE_ENV}=round21",
        "image",
    ]
    execution = sandbox.ContainedExecution(tuple(argv), "kew-round21", "docker")
    stack.enter_context(
        patch.object(core, "_contained_execution_for", return_value=execution)
    )
    stack.enter_context(patch.object(orchestrator, "_sweep_stale_container"))
    for method in ("kill", "remove", "start_watchdog", "stop_watchdog"):
        stack.enter_context(patch.object(sandbox.ContainerHandle, method))


@pytest.mark.serial
@pytest.mark.parametrize("acknowledged", [False, True])
def test_round21_created_container_refuses_without_consuming_retry(
    tmp_path, monkeypatch, acknowledged
):
    """Base counts one launch after OCI failure; the expected count is zero."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        config = _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        config.retry.max_attempts = 3
        config.retry.retriable_exit_codes = [125, 127]
        config.retry.backoff_base_seconds = 0
        output = f"{sandbox.STARTUP_ACK_PREFIX}round21\n" if acknowledged else ""
        child = FiniteChild(
            store,
            cidfile=tmp_path / "cid",
            output=output + "ERROR: OCI startup failed",
            code=127 if acknowledged else 125,
        )
        _process_scene(stack, child)
        _container_scene(stack, child, tmp_path)
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 2, result.output
        assert child.proc.returncode in (125, 127)
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0, (
            "Container creation cannot consume a repair."
        )
        assert child.before_spawn == [0], "Refusal must end the released repair slot."
        row = store.list_all()[0]
        assert _receipt(store, row.run_id).get("state") == "refused"
        assert _receipt(store, row.run_id)["launched_at"] is None
        assert _receipt(store, row.run_id)["ordinal"] is None
        # The next command must still acquire its first repair slot.
        launch = stack.enter_context(
            patch.object(core, "_run_retry_loop", side_effect=_refuse_admitted)
        )
        retry = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert retry.exit_code == 2, retry.output
        launch.assert_called_once()


def _refuse_admitted(run, **kwargs):
    record = run.store.get(run.run_id)
    record.status = "refused"
    run.store.save(record)
    return core._RetryOutcome(
        early_return=DispatchResult(1, 42, run.log_file, launched=False)
    )


@pytest.mark.serial
def test_round21_host_receipt_retries_after_popen(tmp_path, monkeypatch):
    """Base leaves a started process uncounted after one receipt write failure."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        config = _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        config.retry.max_attempts = 1
        child = FiniteChild(store)
        _process_scene(stack, child)
        writer = ReceiptWriter(store, child, failures=1)
        stack.enter_context(patch.object(store, "record_launch", side_effect=writer))
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 1, result.output
        assert child.before_spawn == [0]
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1, (
            "Retry failed receipt persistence after a real start."
        )
        assert child.proc.returncode == 1
        assert writer.calls >= 2
        assert _receipt(store, store.list_all()[0].run_id).get("state") == "started"
        # Pre-spawn refusal never records another receipt.
        preflight = stack.enter_context(
            patch.object(
                orchestrator,
                "_preflight_runner_workspace",
                side_effect=RuntimeError("pre-spawn refusal"),
            )
        )
        calls = writer.calls
        refused = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert refused.exit_code == 2, refused.output
        preflight.assert_called_once()
        assert writer.calls == calls
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1


@pytest.mark.serial
def test_round21_finalization_retries_receipt(tmp_path, monkeypatch):
    """Base does not retry receipt persistence at run finalization."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        config = _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        config.retry.max_attempts = 1
        child = FiniteChild(store)
        _process_scene(stack, child)
        writer = FinalizationWriter(store, child)
        stack.enter_context(patch.object(store, "record_launch", side_effect=writer))
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 1, result.output
        assert writer.finalization_write, (
            "Finalization must retry after primitive receipt failures."
        )
        assert _receipt(store, store.list_all()[0].run_id).get("state") == "started"


class FinalizationWriter(ReceiptWriter):
    def __init__(self, store, child):
        super().__init__(store, child, 0)
        self.finalization_write = False

    def __call__(self, run_id):
        import inspect

        if any(frame.function == "_run_process" for frame in inspect.stack()):
            raise OSError("receipt storage unavailable throughout the primitive")
        self.finalization_write = True
        super().__call__(run_id)


def test_round21_orphan_unknown_receipt_consumes_attempt():
    """Base reaps the run without resolving its receipt or consuming a retry."""
    with SqliteRunStore(":memory:") as store:
        _seed(store, "orphan", started=False)
        admission = PendingAdmission(store, "orphan")
        with (
            patch.object(store, "record_launch", side_effect=OSError("unavailable")),
            suppress(OSError),  # Reach the base behavioral assertion.
        ):
            orchestrator._accept_process_ownership(
                admission, None, DispatchResult(0, 42, None)
            )
        sql = []
        store._conn.set_trace_callback(sql.append)
        assert store.finalize_orphaned_pending(
            "orphan", error_category="owner_dead", finished_at=datetime.now(UTC)
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1, (
            "Unknown orphan startup must consume its reserved attempt."
        )
        assert _receipt(store, "orphan").get("state") == "unknown"
        _assert_receipt_transaction(sql)


class ConcurrentRepairs:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.read_barrier = Barrier(2)
        self.admit_barrier = Barrier(2)
        self.launched = []
        self.counts = []

    def dispatch(self, number):
        with SqliteRunStore(str(self.db)) as store:
            return ci_fix.dispatch_ci_fix(
                run_id=f"actions-{number}", config=self.config, store=store
            )

    def producer(self, *args, **kwargs):
        # Both commands have completed the early retry read before either admits.
        self.read_barrier.wait(timeout=10)
        return None, None

    def admit(self, original, store, *args, **kwargs):
        try:
            return original(store, *args, **kwargs)
        finally:
            self.admit_barrier.wait(timeout=10)

    def execute(self, run, **kwargs):
        self.launched.append(run.run_id)
        run.store.record_launch(run.run_id)
        self.counts.append(run.store.count_launched_ci_fix_attempts("o/r", 87))
        row = run.store.get(run.run_id)
        row.status = "failure"
        run.store.save(row)
        return core._RetryOutcome(
            early_return=DispatchResult(1, 42, run.log_file, launched=True)
        )


def test_round21_concurrent_checkout_repairs_share_atomic_limit(tmp_path, monkeypatch):
    """Base admits both racers and retains three consumed repair receipts."""
    db = tmp_path / "repairs.db"
    with SqliteRunStore(str(db)) as seed, ExitStack() as stack:
        config = _ci_fix_scene(stack, tmp_path, monkeypatch, seed)
        config.publication.publish = "checkout"
        _seed(seed)
        race = ConcurrentRepairs(db, config)
        original = SqliteRunStore.admit_run_with_identity
        stack.enter_context(
            patch.object(ci_fix, "_ci_fix_producer", side_effect=race.producer)
        )
        stack.enter_context(
            patch.object(
                SqliteRunStore,
                "admit_run_with_identity",
                autospec=True,
                side_effect=partial(_race_admit, race, original),
            )
        )
        stack.enter_context(
            patch.object(core, "_run_retry_loop", side_effect=race.execute)
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race.dispatch, (1, 2)))
        assert sorted(results) == [1, ci_fix.CI_FIX_CANNOT_RUN], (
            "Exactly one concurrent checkout repair may acquire the remaining slot."
        )
        assert len(race.launched) == 1
        assert max(race.counts) == 2
        assert seed.count_launched_ci_fix_attempts("o/r", 87) == 2
        assert (
            seed._conn.execute(
                "SELECT COUNT(*) FROM run_launches WHERE state != 'refused'"
            ).fetchone()[0]
            == 2
        )
        assert len(seed.list_all()) == 2, (
            "Rejected admission must roll back its run row."
        )


def _race_admit(race, original, store, *args, **kwargs):
    return race.admit(original, store, *args, **kwargs)


class LabelOrder:
    def __init__(self, store):
        self.store = store
        self.original = store.admit_run_with_identity
        self.order = []

    def admit(self, *args, **kwargs):
        result = self.original(*args, **kwargs)
        self.order.append("slot")
        return result

    def label(self, *args, **kwargs):
        self.order.append("labels")
        return True


def test_round21_reconcile_labels_after_slot_acquisition(tmp_path, monkeypatch):
    """Base reconciles a stale label before it acquires the admission slot."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        remote = RemoteLabels()
        remote.labels.add("ci-fix-attempt-9")
        _remote_scene(stack, remote)
        order = LabelOrder(store)
        stack.enter_context(
            patch.object(store, "admit_run_with_identity", side_effect=order.admit)
        )
        stack.enter_context(
            patch.object(ci_fix, "update_pr_labels", side_effect=order.label)
        )
        stack.enter_context(
            patch.object(core, "_run_retry_loop", side_effect=_refuse_admitted)
        )
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 2, result.output
        assert order.order == ["slot", "labels", "labels"], (
            "Acquire the slot before reconciling stale labels."
        )


@pytest.mark.parametrize("from_v49", [False, True])
def test_round21_v55_shape_and_unique_slot(tmp_path, from_v49):
    """The receipt migration preserves its fields and unique PR slot index."""
    db = tmp_path / "shape.db"
    if from_v49:
        with SqliteRunStore(str(db)) as store:
            store._conn.execute("DROP TABLE run_launches")
            store._conn.execute("DROP TRIGGER IF EXISTS settle_run_launch")
            store._conn.execute("UPDATE schema_version SET version = 49")
            store._conn.commit()
    with SqliteRunStore(str(db)) as store:
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(run_launches)")
        }
        assert columns == {
            "run_id",
            "repo",
            "pr_number",
            "ordinal",
            "state",
            "launched_at",
        }, "Preserve the v55 receipt schema."
        indexes = {
            row[1]: row[2]
            for row in store._conn.execute("PRAGMA index_list(run_launches)")
        }
        assert indexes["idx_run_launches_pr_ordinal"] == 1
        indexed_columns = [
            row[2]
            for row in store._conn.execute(
                "PRAGMA index_info(idx_run_launches_pr_ordinal)"
            )
        ]
        assert indexed_columns == ["repo", "pr_number", "ordinal"]
        # 66: the continuation_expiries step (v57, PR #1952), the review_mode
        # column (v58, #2111), the accepted_verdict column (v59, #2151), the
        # item_plans table (v60, #2290), the triage_decisions table (v61,
        # #2293), the driver's expected cadence (v62), its lock snapshot
        # (v63, both #2297), the epic_tick_log tick-attempt table (v64, #2304)
        # and the prompt's self-check section (v65, #2454) and a run's fleet
        # placement (v66, #2398) and the acceptance_contracts table (v67,
        # #2726), its plan binding (v68, #2774), the owner boot (v69, #2812),
        # epic merge-state reads (v70, #2879), and the merge queue (v71, #2814)
        # landed after this receipt migration. v72 binds host-restart retries
        # to superseded runs (#2969).
        # v73 adds the queue stop's kind and failure (#2883).
        # v74 adds prompt profiles (#3020).
        # v75 marks the journaled tick attempt (#2949); v76 adds merge claims (#3198).
        assert CURRENT_SCHEMA_VERSION == 77
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == 77
        )


@pytest.mark.parametrize("terminal", ["refused", "failure", "killed"])
def test_round21_terminal_write_resolves_receipt_atomically(terminal):
    """Base leaves a terminal repair's receipt pending and uncounted."""
    with SqliteRunStore(":memory:") as store:
        _seed(store, "terminal", started=False)
        sql = []
        store._conn.set_trace_callback(sql.append)
        row = store.get("terminal")
        row.status = terminal
        store.save(row)
        expected = "refused" if terminal == "refused" else "unknown"
        assert _receipt(store, "terminal").get("state") == expected, (
            "Terminalization must resolve the receipt in the run transaction."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == (
            expected == "unknown"
        )
        _assert_receipt_transaction(sql)


def test_round21_settled_start_survives_later_refusal():
    """Base cannot retain the started receipt state through later refusal."""
    from types import SimpleNamespace

    with SqliteRunStore(":memory:") as store:
        _seed(store, "retry", started=False)
        admission = PendingAdmission(store, "retry")
        first = sandbox.ContainerHandle(
            SimpleNamespace(poll=partial(int, 1)),
            "first",
            "docker",
            container_id="a" * 64,
            launch_argv=["docker", "run", f"{sandbox.STARTUP_NONCE_ENV}=first"],
        )
        first.accept_admission(admission)
        first.observe_workload_output(f"{sandbox.STARTUP_ACK_PREFIX}first")
        first.finish_startup_observation()
        second = sandbox.ContainerHandle(
            SimpleNamespace(poll=partial(int, 125)),
            "second",
            "docker",
            container_id="b" * 64,
        )
        second.accept_admission(admission)
        second.finish_startup_observation()
        row = store.get("retry")
        row.status = "refused"
        store.save(row)
        assert _receipt(store, "retry").get("state") == "started", (
            "A later refused attempt cannot erase an earlier settled start."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1


def test_round21_unique_pr_ordinal_rejects_direct_insert():
    """Base permits duplicate PR slots because only run_id is unique."""
    with SqliteRunStore(":memory:") as store:
        _seed(store)
        store.save(_record("duplicate", kind="ci_fix"))
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(run_launches)")
        }
        statement = (
            "INSERT INTO run_launches (run_id, repo, pr_number, ordinal)"
            " VALUES ('duplicate', 'o/r', 87, 1)"
            if "ordinal" in columns
            else "INSERT INTO run_launches (run_id, pr_number) VALUES ('duplicate', 87)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            store._conn.execute(statement)
        store._conn.rollback()
