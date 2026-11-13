"""Round 19 regressions against merge f774b929 (main 6ca5b497)."""

import subprocess
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_w42_round17 import _admitted_execution, _config, _record, _reply
from test_w42_round18 import _ci_fix_scene
from typer.testing import CliRunner

import kew.cli as cli
import kew.dispatch_core as core
import kew.dispatch_sandbox as contained
from kew.run_store import SqliteRunStore


class OperatorCancelled(BaseException):
    """Exercise interruptions beyond the two built-in operator signals."""


def _assert_released(store, run_id, category, *, created=False):
    row = store.get(run_id)
    assert row is not None
    if created:
        # A container created before its launch: the launch layer finalizes the
        # row once the owner confirms the kill; the admission releases the rest.
        assert row.status == "failure"
        assert row.error_category == "sandbox_launch_error"
    else:
        assert row.status == "refused", "An interrupted admission must become terminal."
        assert row.error_category == category
    assert row.finished_at is not None
    # A row the dispatcher itself finalized keeps its own admission group; only
    # the cancellation finalizer clears it.
    expected_owner = (
        row.owner_pid if created or category == "admission_hook_refused" else None
    )
    assert row.pgid == expected_owner
    assert not store.list_pending()
    assert not store.list_epic_reservations("round19"), "Release the owned reservation."
    assert store.live_run_count() == 0
    assert store.try_admit_run(_record("next", item="99"), cap=1)


def _tracked(config, store, tmp_path, **kwargs):
    return core.run_tracked_agent(
        prompt="repair",
        worktree_dir=tmp_path,
        config=config,
        issue_num=42,
        item_id="42",
        branch="agent/42",
        store=store,
        run_id="interrupted",
        validate_continuation=False,
        worker_cap=1,
        **kwargs,
    )


@pytest.mark.parametrize(
    "exception", [KeyboardInterrupt, SystemExit, OperatorCancelled]
)
def test_round19_hook_interrupt_releases_row_reservation_and_slot(
    tmp_path, monkeypatch, exception
):
    config = _config(tmp_path, monkeypatch)
    original = exception(73)
    with SqliteRunStore(":memory:") as store:
        assert store.reserve_epic_node_item("round19", "42", "interrupted")

        def interrupt(_run_id):
            # The admission transaction has committed before the hook runs.
            assert not store._conn.in_transaction
            assert store.get("interrupted").status == "pending"
            raise original

        with (
            patch.object(core, "run_agent") as launch,
            pytest.raises(exception) as caught,
        ):
            _tracked(config, store, tmp_path, on_admitted=interrupt)
        assert caught.value is original
        _assert_released(store, "interrupted", "admission_hook_interrupted")
        launch.assert_not_called()
        events = store.list_audit_events(event="dispatch_completed")
        assert len(events) == 1
        assert exception.__name__ in events[0].payload["reason"]
        assert events[0].payload["launched"] is False


def test_round19_ordinary_hook_exception_preserves_refusal_and_releases_reservation(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, monkeypatch)
    with SqliteRunStore(":memory:") as store:
        assert store.reserve_epic_node_item("round19", "42", "interrupted")
        hook = Mock(side_effect=RuntimeError("labels unavailable"))
        with patch.object(core, "run_agent") as launch:
            result = _tracked(config, store, tmp_path, on_admitted=hook)
        assert result.launched is False
        assert result.error_category == "admission_hook_refused"
        assert result.stop_reason == "labels unavailable"
        assert result.exit_code != 0
        _assert_released(store, "interrupted", "admission_hook_refused")
        launch.assert_not_called()
        hook.assert_called_once()


def test_round19_ci_fix_ctrl_c_does_not_spend_an_uncompleted_label_attempt(
    tmp_path, monkeypatch
):
    from kew.labels import get_ci_fix_attempt, update_pr_labels

    remote_labels = {"agent-pr"}
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        _ci_fix_scene(stack, tmp_path, monkeypatch, store)

        def interrupt(*args, **kwargs):
            assert len(store.list_pending()) == 1
            assert args[0][-2:] == ["--add-label", "ci-fix-attempt-1"]
            raise KeyboardInterrupt

        stack.enter_context(patch("kew.ci_fix.update_pr_labels", update_pr_labels))
        labels = stack.enter_context(patch("kew.labels.run", side_effect=interrupt))
        launch = stack.enter_context(patch.object(core, "run_agent"))
        result = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert result.exit_code == 130  # Preserve the CLI's normal Ctrl+C status.
        assert not store.list_pending(), "Ctrl+C must finalize the admitted CI repair."
        row = store.list_all()[0]
        assert row.status == "refused"
        assert row.error_category == "admission_hook_interrupted"
        assert get_ci_fix_attempt(list(remote_labels)) == 0
        labels.assert_called_once()
        launch.assert_not_called()

        def completed_label(argv, **kwargs):
            assert argv[-2:] == ["--add-label", "ci-fix-attempt-1"]
            remote_labels.add(argv[-1])
            return _reply()

        labels.side_effect = completed_label
        stack.enter_context(
            patch.object(
                core, "_run_retry_loop", side_effect=_admitted_execution(store, [])
            )
        )
        retry = CliRunner().invoke(cli.app, ["ci-fix", "87"])
        assert retry.exit_code == 1, retry.output
        assert get_ci_fix_attempt(list(remote_labels)) == 1
        assert labels.call_count == 2


@pytest.mark.parametrize(
    "step",
    [
        "_compute_breaker_baseline",
        "build_pricing_overrides",
        "_enter_managed_retry_context",
        "_check_existing_pr",
        "_record_attempt_start",
        "_attest_tracked_posture",
        "_sidecar_attempt_context",
        "_admit_attempt_route_profile",
        "paid_launch_check",
        "_contained_execution_for",
    ],
)
def test_round19_tracked_prelaunch_interruption(tmp_path, monkeypatch, step):
    config = _config(tmp_path, monkeypatch)
    original = KeyboardInterrupt(step)
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        assert store.reserve_epic_node_item("round19", "42", "interrupted")
        # Isolate preparation from external runner and credential probes.
        stack.enter_context(patch.object(core, "_admit_attempt_route_profile"))
        kwargs = {}
        if step == "paid_launch_check":
            kwargs[step] = Mock(side_effect=original)
        else:
            stack.enter_context(patch.object(core, step, side_effect=original))
        launch = stack.enter_context(patch.object(core, "run_agent"))
        with pytest.raises(KeyboardInterrupt) as caught:
            _tracked(config, store, tmp_path, **kwargs)
        assert caught.value is original
        launch.assert_not_called()
        if step == "_attest_tracked_posture":
            # The initial attestation precedes the key mint and the admission
            # (#1957): an interrupt there admits nothing, so no row or slot
            # exists and the reservation stays with the caller that took it.
            assert store.get("interrupted") is None
            assert not store.list_pending()
            assert store.list_epic_reservations("round19")
            # The caller's reservation is the one live item (#721 counts it).
            assert store.live_run_count() == 1
            return
        _assert_released(store, "interrupted", "admission_hook_interrupted")


@pytest.mark.parametrize(
    "step",
    [
        "_require_initial_sandbox_publication_credentials",
        "_require_checkout_generation_or_finalize",
        "_capture_launch_evidence_or_finalize",
        "get_runner",
        "build_agent_container_argv",
        "sidecar_switchyard_context",
        "prepare_sidecar_worker",
        "sidecar_ready",
    ],
)
def test_round19_sandbox_prelaunch_interruption(tmp_path, monkeypatch, step):
    from dataclasses import replace

    from kew.inference_transport import resolve_inference_transport
    from kew.runners import get_runner

    config = _config(tmp_path, monkeypatch)
    transport = resolve_inference_transport(config, get_runner("claude-code"), "model")
    sidecar = step in {
        "sidecar_switchyard_context",
        "prepare_sidecar_worker",
        "sidecar_ready",
    }
    if sidecar:
        transport = replace(transport, mode="switchyard", sidecar=True)
    original = SystemExit(73)
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        assert store.reserve_epic_node_item("round19", "42", "interrupted")
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
        ):
            stack.enter_context(patch.object(contained, name, return_value=None))
        supervisor = SimpleNamespace(endpoint="http://proxy", config_sha256="abc")
        stack.enter_context(
            patch.object(
                contained,
                "sidecar_switchyard_context",
                return_value=nullcontext(supervisor),
            )
        )
        stack.enter_context(
            patch.object(
                contained,
                "prepare_sidecar_worker",
                return_value=SimpleNamespace(
                    argv=("docker", "exec", "worker"),
                    name="worker",
                    runtime="docker",
                    container_id="a" * 64,
                    checkout_dir=tmp_path,
                ),
            )
        )
        if step == "sidecar_ready":
            stack.enter_context(
                patch.object(
                    contained, "_record_ready_sidecar_profile", side_effect=original
                )
            )
            # The ownership guard kills the prepared worker and confirms it
            # through the runtime; nothing here launches a real container.
            stack.enter_context(
                patch.object(contained, "_run", side_effect=_confirmed_cleanup)
            )
        else:
            stack.enter_context(patch.object(contained, step, side_effect=original))
        launch = stack.enter_context(patch.object(core, "_launch_container"))
        with pytest.raises(SystemExit) as caught:
            contained._run_sandboxed_container(
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
        assert caught.value is original
        _assert_released(
            store,
            "interrupted",
            "admission_hook_interrupted",
            created=step == "sidecar_ready",
        )
        launch.assert_not_called()


def _confirmed_cleanup(argv, **_kwargs):
    """The runtime confirms kill and wait for a container created before launch."""
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.parametrize("form", ["unbound", "mismatch", "table", "wrapped", "list"])
def test_round19_docs_guard_rejects_planted_wrong_claim(tmp_path, monkeypatch, form):
    import test_docs_truthfulness as guard

    root = Path(__file__).resolve().parents[2]
    docs = tmp_path / "docs"
    (docs / "cli").mkdir(parents=True)
    (docs / "cli" / "exit-codes.md").write_text(
        (root / "docs" / "cli" / "exit-codes.md").read_text()
    )
    wrong_claims = {
        "unbound": "`kew attach missing` exits 1 for a missing run.\n",
        "table": (
            "| Command | Exit code |\n| --- | --- |\n| `kew attach missing` | 1 |\n"
        ),
        "wrapped": "`kew attach missing` exits with\ncode `1` for a missing run.\n",
        "list": "`kew attach missing` exit codes:\n\n- **1** — Missing run.\n",
    }
    page = "fixture.md"
    if form == "mismatch":
        page = "operations.md"
        text = (root / "docs" / page).read_text()
        (docs / page).write_text(
            text.replace(
                "restore aborts with exit code 2", "restore aborts with exit code 1"
            )
        )
    else:
        (docs / page).write_text(wrong_claims[form])
    monkeypatch.setattr(
        guard,
        "Path",
        lambda _: SimpleNamespace(
            resolve=lambda: SimpleNamespace(parents=[None, None, tmp_path])
        ),
    )
    with pytest.raises(AssertionError, match=page):
        guard.test_round14_current_docs_never_advertise_public_exit_above_two()


def test_round19_corrected_pages_follow_scheme():
    """The merge head's held restore, missing attach, budget, and empty codes fail."""
    import re

    root = Path(__file__).resolve().parents[2]
    for page, token, expected in (
        ("operations.md", "restore aborts with exit code", 2),
    ):
        line = next(
            line
            for line in (root / "docs" / page).read_text().splitlines()
            if token in line
        )
        claim = re.search(r"exit(?:s)?(?: with)?(?: code)? (\d+)", line, re.I)
        assert claim is not None, line
        assert int(claim[1]) == expected, f"{page}: {line}"
