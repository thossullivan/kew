"""Cancellation and durable retry regressions against round 19, 00e4dd50."""

import signal
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from support.source_ast import call_sites
from test_w42_round17 import _config, _record, _reply
from test_w42_round18 import _ci_fix_scene
from test_w42_round19 import _tracked
from typer.testing import CliRunner

import kew.cli as cli
import kew.dispatch_core as core
import kew.dispatch_sandbox as contained
import kew.orchestrator as orchestrator
import kew.sandbox as sandbox
from kew.dispatch_admission import PendingAdmission
from kew.inference_transport import resolve_inference_transport
from kew.run_store import SqliteRunStore
from kew.runners import get_runner


class InterruptAtLine:
    """Interrupt the call line immediately after the old ownership assignment."""

    def __init__(self, function, callee, where=lambda guards: True):
        # The line is found on the syntax tree, as the start of the one statement
        # that calls *callee* where *where* holds of the ``if`` tests around it —
        # a reindent or a wrapped call moves nothing this depends on.
        sites = [site for site in call_sites(function, callee) if where(site.guards)]
        assert len(sites) == 1, (function.__qualname__, callee, sites)
        self.code = function.__code__
        self.line = sites[0].line
        self.original = KeyboardInterrupt(callee)
        self.hit = False

    def __call__(self, frame, event, arg):
        if (
            event == "line"
            and frame.f_code == self.code
            and frame.f_lineno == self.line
        ):
            self.hit = True
            raise self.original
        return self

    def __enter__(self):
        self.previous = sys.gettrace()
        sys.settrace(self)
        return self

    def __exit__(self, *args):
        sys.settrace(self.previous)


def _assert_terminal(store, *, launched=False, created=False):
    record = store.get("interrupted")
    if created:
        # A container created before its launch: the launch layer finalizes the
        # row once the owner confirms the kill; the admission's cancellation
        # then releases the reservation and settles the receipt.
        assert record.status == "failure"
        assert record.error_category == "sandbox_launch_error"
    else:
        assert record.status == ("killed" if launched else "refused")
        assert record.error_category == (
            "execution_interrupted" if launched else "admission_hook_interrupted"
        )
    assert record.finished_at is not None
    assert not store.list_pending()
    assert not store.list_epic_reservations("round20")
    assert store.live_run_count() == 0
    events = store.list_audit_events(event="dispatch_completed")
    assert len(events) == 1, "Execution and admission must never finalize twice."
    assert events[0].payload["launched"] is launched
    assert store.try_admit_run(_record("next", item="99"), cap=1)


def _confirmed_cleanup(argv, **_kwargs):
    """The runtime confirms kill and wait for a container created before launch."""
    return subprocess.CompletedProcess(argv, 0, "", "")


def _sandbox_scene(stack, config, store, tmp_path, sidecar):
    from contextlib import nullcontext

    transport = resolve_inference_transport(config, get_runner("claude-code"), "model")
    if sidecar:
        transport = replace(transport, mode="switchyard", sidecar=True)
    # The container ownership guard kills a container created before launch and
    # confirms it through the runtime; nothing here launches a real container.
    stack.enter_context(patch.object(contained, "_run", side_effect=_confirmed_cleanup))
    stack.enter_context(
        patch.object(
            contained,
            "_resolve_launch_inputs",
            return_value=(
                "sha256:" + "a" * 64,
                transport,
                "model",
                None,
                get_runner("claude-code"),
            ),
        )
    )
    for name in (
        "_sandbox_route_profile",
        "_require_initial_sandbox_publication_credentials",
        "_require_checkout_generation_or_finalize",
        "_capture_launch_evidence_or_finalize",
        "_record_ready_sidecar_profile",
    ):
        stack.enter_context(patch.object(contained, name, return_value=None))
    stack.enter_context(
        patch.object(
            contained,
            "sidecar_switchyard_context",
            return_value=nullcontext(
                SimpleNamespace(endpoint="http://proxy", config_sha256="abc")
            ),
        )
    )
    stack.enter_context(
        patch.object(
            contained,
            "prepare_sidecar_worker",
            return_value=(
                sandbox.ContainedExecution(
                    ("docker", "exec", "worker"),
                    "worker",
                    "docker",
                    container_id="a" * 64,
                    prepared=True,
                )
            ),
        )
    )
    return partial(
        contained._run_sandboxed_container,
        prompt="repair",
        checkout_dir=tmp_path,
        config=config,
        image="image",
        runner="claude-code",
        issue_num=42,
        item_id="42",
        branch="agent/42",
        title="repair",
        model="model",
        store=store,
        run_id="interrupted",
        worker_cap=1,
    )


def _direct(guards):
    return "sidecar_supervisor is None" in guards


def _sidecar(guards):
    return "sidecar_supervisor is None" not in guards


SITES = {
    "tracked": (core._run_retry_attempt, "run_agent"),
    # Each launch line sits inside its ownership guard: the direct client launch
    # is nested under the sidecar-supervisor branch, the sidecar launch is not.
    "sandbox_direct": (
        contained._launch_streaming_container,
        "dispatch_core._launch_container",
        _direct,
    ),
    "sandbox_sidecar": (
        contained._launch_streaming_container,
        "dispatch_core._launch_container",
        _sidecar,
    ),
    "sandbox_delegate": (
        contained.run_container_and_stream,
        "_run_sidecar_container_and_stream",
    ),
    "sidecar_delegate": (
        contained._run_sidecar_container_and_stream,
        "run_container_and_stream",
    ),
    # The contained dispatch's own launch line. It calls ``run_placed_dispatch``
    # rather than ``run_container_and_stream`` since #2526: a dispatch placed on
    # a host runs its agent there, and a local placement is the same call this
    # line always made, wrapped in a thunk. The handover this site asserts is
    # unchanged — the admission is still the caller's across it.
    "sandbox_outer": (
        contained._run_sandboxed_container,
        "run_placed_dispatch",
    ),
}


@pytest.mark.parametrize("site", SITES)
def test_round20_caller_handover_line_keeps_admission_owned(
    tmp_path, monkeypatch, site
):
    config = _config(tmp_path, monkeypatch)
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        stack.enter_context(patch.object(core, "_admit_attempt_route_profile"))
        stack.enter_context(
            patch.object(core, "_contained_execution_for", return_value=None)
        )
        launch = stack.enter_context(
            patch.object(core, "run_agent", side_effect=KeyboardInterrupt)
        )
        container_launch = stack.enter_context(
            patch.object(core, "_launch_container", side_effect=KeyboardInterrupt)
        )
        invoke = partial(_tracked, config, store, tmp_path)
        if site != "tracked":
            invoke = _sandbox_scene(
                stack,
                config,
                store,
                tmp_path,
                site not in {"sandbox_direct", "sandbox_outer"},
            )
        trace = InterruptAtLine(*SITES[site])
        with pytest.raises(KeyboardInterrupt) as caught, trace:
            invoke()
        assert trace.hit
        assert caught.value is trace.original
        launch.assert_not_called()
        container_launch.assert_not_called()
        _assert_terminal(store, created=site == "sandbox_sidecar")


@pytest.mark.parametrize("site", ["tracked", "sandbox_direct", "sandbox_sidecar"])
def test_round20_primitive_entry_interruption_keeps_admission_owned(
    tmp_path, monkeypatch, site
):
    config = _config(tmp_path, monkeypatch)
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        stack.enter_context(patch.object(core, "_admit_attempt_route_profile"))
        stack.enter_context(
            patch.object(core, "_contained_execution_for", return_value=None)
        )
        original = KeyboardInterrupt("primitive entry")
        stack.enter_context(patch.object(core, "run_agent", side_effect=original))
        stack.enter_context(
            patch.object(core, "_launch_container", side_effect=original)
        )
        invoke = partial(_tracked, config, store, tmp_path)
        if site != "tracked":
            invoke = _sandbox_scene(
                stack, config, store, tmp_path, site == "sandbox_sidecar"
            )
        with pytest.raises(KeyboardInterrupt) as caught:
            invoke()
        assert caught.value is original
        _assert_terminal(store, created=site == "sandbox_sidecar")


class ChildProcess:
    """Keep a real child's handle even when the old implementation drops it."""

    def __init__(self, interrupt=False):
        self.proc = None
        self.interrupt = interrupt

    def __call__(self, *args, **kwargs):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if self.interrupt:
            signal.raise_signal(signal.SIGINT)
        return self.proc

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=5)
            self.proc.stdout.close()


def _process_scene(stack, child):
    stack.callback(child.close)
    stack.enter_context(patch.object(core, "_admit_attempt_route_profile"))
    stack.enter_context(
        patch.object(core, "_contained_execution_for", return_value=None)
    )
    stack.enter_context(patch.object(orchestrator, "_preflight_runner_workspace"))
    stack.enter_context(
        patch.object(
            orchestrator, "_build_agent_cmd", return_value=([sys.executable], None, 30)
        )
    )
    stack.enter_context(patch.object(orchestrator, "popen", side_effect=child))


@pytest.mark.serial
@pytest.mark.parametrize("inside_popen", [False, True])
def test_round20_started_process_uses_execution_cleanup(
    tmp_path, monkeypatch, inside_popen
):
    config = _config(tmp_path, monkeypatch)
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        child = ChildProcess(interrupt=inside_popen)
        _process_scene(stack, child)
        if not inside_popen:
            stack.enter_context(
                patch.object(
                    orchestrator,
                    "_announce_spawn",
                    side_effect=KeyboardInterrupt,
                )
            )
        with pytest.raises(KeyboardInterrupt):
            _tracked(config, store, tmp_path)
        _assert_terminal(store, launched=True)
        assert child.proc.poll() is not None, "The execution owner must reap its child."


def test_round20_second_keyboardinterrupt_cannot_split_cleanup(tmp_path, monkeypatch):
    original = KeyboardInterrupt("first")
    with SqliteRunStore(":memory:") as store:
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        store.save(_record("interrupted", item="42"))
        sql = []
        store._conn.set_trace_callback(sql.append)
        with (
            patch.object(
                store, "save", side_effect=[KeyboardInterrupt("second"), None]
            ),
            pytest.raises(KeyboardInterrupt) as caught,
            PendingAdmission(store, "interrupted"),
        ):
            raise original
        assert caught.value is original, "Cleanup must preserve the first cancellation."
        _assert_terminal(store)
        update = next(
            i for i, stmt in enumerate(sql) if stmt.startswith("UPDATE runs SET status")
        )
        delete = next(
            i
            for i, stmt in enumerate(sql)
            if stmt.startswith("DELETE FROM epic_reservations")
        )
        commit = next(i for i, stmt in enumerate(sql) if stmt == "COMMIT")
        assert update < delete < commit, "Row and reservation must commit together."
        assert "COMMIT" not in sql[update:delete]


class InterruptTransaction:
    def __init__(self, store, *, signal_interrupt):
        self.execute = store._execute
        self.interrupted = False
        self.signal_interrupt = signal_interrupt

    def __call__(self, sql, params=None):
        result = self.execute(sql, params)
        if (
            sql.lstrip().startswith(("UPDATE runs SET status", "INSERT INTO runs"))
            and not self.interrupted
        ):
            self.interrupted = True
            if self.signal_interrupt:
                signal.raise_signal(signal.SIGINT)
            else:
                raise KeyboardInterrupt("injected inside transaction")
        return result


@pytest.mark.parametrize("signal_interrupt", [False, True])
def test_round20_cleanup_defers_sigint_and_retries_injected_cancellation(
    signal_interrupt,
):
    original = SystemExit(73)
    previous = signal.getsignal(signal.SIGINT)
    with SqliteRunStore(":memory:") as store:
        store.save(_record("interrupted", item="42"))
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        injection = InterruptTransaction(store, signal_interrupt=signal_interrupt)
        with (
            patch.object(store, "_execute", side_effect=injection),
            pytest.raises(BaseException) as caught,
            PendingAdmission(store, "interrupted"),
        ):
            raise original
        assert caught.value is original
        _assert_terminal(store)
        assert injection.interrupted
        assert isinstance(original.__cause__, KeyboardInterrupt)
        assert signal.getsignal(signal.SIGINT) == previous


class RemoteLabels:
    def __init__(self):
        self.labels = {"agent-pr"}
        self.interrupt = False
        self.edits = []

    def pr(self, **kwargs):
        return {
            "number": 87,
            "headRefName": "agent/42",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "body": "Closes #42",
            "title": "Repair",
            "labels": [{"name": name} for name in sorted(self.labels)],
        }

    def edit(self, argv, **kwargs):
        self.edits.append(argv)
        for i, arg in enumerate(argv):
            if arg == "--add-label":
                self.labels.add(argv[i + 1])
            elif arg == "--remove-label":
                self.labels.discard(argv[i + 1])
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt("gh applied label before returning")
        return _reply()


def _remote_scene(stack, remote):
    from kew.labels import update_pr_labels

    stack.enter_context(patch("kew.ci_fix.get_pr_for_run", side_effect=remote.pr))
    stack.enter_context(patch("kew.ci_fix.update_pr_labels", update_pr_labels))
    stack.enter_context(patch("kew.labels.run", side_effect=remote.edit))
    stack.enter_context(patch("kew.ci_fix.run", return_value=_reply()))


def _durable_count(store):
    # The base lacks launch receipts. Report zero to reach the behavioral assertion.
    if not store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'run_launches'"
    ).fetchone():
        return 0
    return store.count_launched_ci_fix_attempts("o/r", 87)


@pytest.mark.serial
def test_round20_applied_label_interruption_does_not_consume_retry(
    tmp_path, monkeypatch
):
    remote = RemoteLabels()
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        _remote_scene(stack, remote)
        remote.interrupt = True
        first = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert first.exit_code == 130
        assert "ci-fix-attempt-1" in remote.labels
        assert _durable_count(store) == 0
        assert store.list_all()[0].status == "refused"
        child = ChildProcess()
        _process_scene(stack, child)
        stack.enter_context(
            patch.object(orchestrator, "_announce_spawn", side_effect=KeyboardInterrupt)
        )
        retry = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert retry.exit_code == 130, retry.output
        assert "attempt 1" in retry.output, (
            "An interrupted label cannot consume attempt 1."
        )
        assert remote.labels == {"agent-pr", "ci-fix-attempt-1"}
        assert any("--remove-label" in argv for argv in remote.edits)
        assert _durable_count(store) == 1


@pytest.mark.serial
def test_round20_two_real_launches_exhaust_durable_attempts(tmp_path, monkeypatch):
    remote = RemoteLabels()
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        _remote_scene(stack, remote)
        child = ChildProcess()
        _process_scene(stack, child)
        stack.enter_context(
            patch.object(orchestrator, "_announce_spawn", side_effect=KeyboardInterrupt)
        )
        for count in (1, 2):
            remote.labels = {
                "agent-pr"
            }  # Removing labels cannot reset durable history.
            result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
            assert result.exit_code == 130, result.output
            assert _durable_count(store) == count, (
                "Each real repair launch consumes one attempt."
            )
            assert child.proc.poll() is not None
            child.close()
        remote.labels = {"agent-pr"}
        exhausted = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert exhausted.exit_code == 0, exhausted.output
        assert _durable_count(store) == 2
        # Round 21 reconciles attempt labels only after a new slot commits.
        assert not any(label.startswith("ci-fix-attempt-") for label in remote.labels)
        assert "agent-needs-help" in remote.labels
        assert len(store.list_all()) == 2, (
            "Retry exhaustion must refuse a third admission."
        )


class ContainerChild(ChildProcess):
    def __init__(self, cidfile, *, identified):
        super().__init__()
        self.cidfile = cidfile
        self.identified = identified
        self.create_credentials = []

    def create(self, argv, *, credential=None):
        # A claude-code route mints no bounded key, so creation must be handed
        # None rather than an inherited one.
        self.create_credentials.append(credential)
        if not self.identified:
            raise KeyboardInterrupt("creation interrupted before identity")
        self.cidfile.write_text("a" * 64)
        return "a" * 64

    def __call__(self, *args, **kwargs):
        proc = super().__call__(*args, **kwargs)
        if self.identified:
            self.cidfile.write_text("a" * 64)
        signal.raise_signal(signal.SIGINT)
        return proc


@pytest.mark.serial
@pytest.mark.parametrize("site", ["direct", "sidecar", "unidentified"])
def test_round20_container_start_owns_cancellation_after_runtime_identity(
    tmp_path, monkeypatch, site
):
    config = _config(tmp_path, monkeypatch)
    cidfile = tmp_path / "container-id"
    child = ContainerChild(cidfile, identified=site != "unidentified")
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        stack.callback(child.close)
        assert store.reserve_epic_node_item("round20", "42", "interrupted")
        invoke = _sandbox_scene(stack, config, store, tmp_path, site == "sidecar")
        stack.enter_context(
            patch.object(
                contained,
                "build_agent_container_argv",
                return_value=[
                    "docker",
                    "run",
                    "--name",
                    "kew-42",
                    "--cidfile",
                    str(cidfile),
                    "image",
                ],
            )
        )
        stack.enter_context(patch("kew.subprocess_utils.popen", side_effect=child))
        stack.enter_context(
            patch.object(
                sandbox, "create_registered_container", side_effect=child.create
            )
        )
        kill = stack.enter_context(patch.object(sandbox.ContainerHandle, "kill"))
        remove = stack.enter_context(patch.object(sandbox.ContainerHandle, "remove"))
        with pytest.raises(KeyboardInterrupt):
            invoke()
        # The sidecar site prepares its worker instead of creating here;
        # wherever creation does run it is handed no bounded key.
        assert all(key is None for key in child.create_credentials)
        if site == "unidentified":
            # Interrupted before the create recorded an id: nothing is killed
            # by the name, which may be another run's container (#1963), so
            # the cleanup stays unconfirmed and the row is kept for recovery,
            # which reads the cidfile it names.
            record = store.get("interrupted")
            assert record.status == "pending"
            assert record.finished_at is None
            assert record.container_id is None
            assert record.container_cidfile == str(cidfile)
            assert child.proc is None
            kill.assert_not_called()
            remove.assert_not_called()
        else:
            _assert_terminal(store, launched=True)
            assert child.proc.poll() is not None
            assert store.get("interrupted").container_id == "a" * 64
            kill.assert_called_once()
            remove.assert_called_once()
