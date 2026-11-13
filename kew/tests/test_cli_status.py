"""Matched-attempt status regressions."""

import json
import re
from contextlib import contextmanager, nullcontext

import pytest
from test_cli import TestStatusRunWorkView as _StatusWorkView
from test_service_runs import _admit_run, _record_sidecar_posture
from typer.testing import CliRunner

from kew import cli
from kew.run_store import SqliteRunStore
from kew.service import runs


def _assert_status_attempt(output, export):
    if export == "json":
        payload = json.loads(output)
        attempt = payload["run"]["attempt_number"]
        assert payload["sandbox"]["reason"] == f"attempt {attempt}"
        assert payload["run"]["branch"] == f"attempt-{attempt}"
    else:
        branch = re.search(r"Branch:\s+attempt-(\d+)", output)
        assert branch is not None
        attempt = int(branch[1])
        assert f"reason=attempt {attempt}" in output
        assert re.search(rf"Attempt:\s+{attempt}\b", output)


def _read_then_retry(original, commit_retry):
    def read(*args, **kwargs):
        selected = original(*args, **kwargs)
        commit_retry()
        return selected

    return read


@pytest.mark.parametrize("export", ["text", "json"])
@pytest.mark.parametrize("boundary", ["row", "events"])
def test_round9_status_uses_matched_attempt(tmp_path, monkeypatch, export, boundary):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text("")
    (tmp_path / ".kew").mkdir()
    (tmp_path / ".git").mkdir()
    config = _StatusWorkView._config(tmp_path)
    _StatusWorkView._admit(config, attempt_number=2, branch="attempt-2")
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as writer:
        _record_sidecar_posture(writer, 2, admitted=True)
        record = writer.get("r1")
        raced = False

        def commit_retry():
            nonlocal raced
            if not raced:
                raced = True
                record.attempt_number = 3
                record.branch = "attempt-3"
                writer.save(record)
                _record_sidecar_posture(writer, 3, admitted=True)
                assert not writer._conn.in_transaction

        if boundary == "row":
            monkeypatch.setattr(
                SqliteRunStore,
                "get",
                _read_then_retry(SqliteRunStore.get, commit_retry),
            )
            monkeypatch.setattr(
                runs, "_find_record", _read_then_retry(runs._find_record, commit_retry)
            )
        else:
            monkeypatch.setattr(
                runs,
                "_posture_events",
                _read_then_retry(runs._posture_events, commit_retry),
            )
        monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
        monkeypatch.setattr(cli, "_preflight", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_detect_repo", lambda *a, **k: None)
        args = ["status", "--run", "r1"]
        if export == "json":
            args += ["--json"]
        result = CliRunner().invoke(cli.app, args)
        if result.exception is not None:
            raise result.exception
        assert result.exit_code == 0, result.output
        assert raced
        _assert_status_attempt(result.output, export)


@contextmanager
def _round10_stores(tmp_path, storage):
    path = ":memory:" if storage == "memory" else str(tmp_path / "detail.db")
    with SqliteRunStore(path) as store:
        _admit_run(
            store, attempt_number=2, branch="attempt-2", status="awaiting_publication"
        )
        _record_sidecar_posture(store, 2, admitted=True)
        writer = nullcontext(store) if storage == "memory" else SqliteRunStore(path)
        with writer as connection:
            yield store, connection


def _round10_retry(writer):
    record = writer.get("r1")
    record.attempt_number = 3
    record.branch = "attempt-3"
    writer.save(record)
    _record_sidecar_posture(writer, 3, admitted=True)
    assert not writer._conn.in_transaction


def _assert_round10_status_identity(output, export):
    _assert_status_attempt(output, export)
    if export == "json":
        payload = json.loads(output)
        assert payload["run"]["attempt_number"] == 2
        assert payload["work"]["identity"]["run_id"] == payload["run"]["run_id"]
        assert (
            payload["work"]["identity"]["attempt_number"]
            == payload["run"]["attempt_number"]
        )
    else:
        attempt = re.search(r"Attempt:\s+(\d+)", output)
        execution = re.search(r"Execution:.*attempt (\d+)", output)
        assert attempt is not None and execution is not None
        assert attempt[1] == "2"
        assert execution[1] == attempt[1]


def _assert_round10_status_sections(output, export):
    if export == "json":
        payload = json.loads(output)
        assert {
            "run",
            "item_id",
            "item_ref",
            "view_revision",
            "composed_at",
            "healthy",
            "route",
            "safest_action",
            "actions",
            "evidence",
            "sandbox",
            "work",
        } == payload.keys()
        assert {
            "identity",
            "source",
            "route",
            "execution",
            "artifact",
            "judgment",
            "outcome",
            "integration",
            "readiness",
            "no_change",
            "certificate_status",
            "review_pressure",
            "exception",
            "safest_action",
            "actions",
            "view_revision",
            "composed_at",
            "observations",
        } == payload["work"].keys()
    else:
        for section in (
            "Run",
            "Attempt",
            "Item",
            "Branch",
            "Status",
            "Exit",
            "Started",
            "Finished",
            "Duration",
            "Cost",
            "Tokens",
            "Posture",
            "Revision",
            "Execution",
            "Artifact",
            "Judgment",
            "Source",
            "Route",
            "Exception",
            "Evidence",
            "Next",
            "Actions",
        ):
            assert re.search(rf"^{section}:", output, re.MULTILINE)


@pytest.mark.parametrize("storage", ["file", "memory"])
@pytest.mark.parametrize("export", ["text", "json"])
@pytest.mark.parametrize("race", [True, False], ids=["retry", "stable"])
def test_round10_status_composes_one_attempt(
    tmp_path, monkeypatch, storage, export, race
):
    config = _StatusWorkView._config(tmp_path)
    with _round10_stores(tmp_path, storage) as (store, writer):
        reads = []

        def after_posture(*args, **kwargs):
            selected = original(*args, **kwargs)
            reads.append(selected[0])
            if race:
                _round10_retry(writer)
            return selected

        original = runs._verify_posture
        monkeypatch.setattr(runs, "_verify_posture", after_posture)
        monkeypatch.setattr(cli, "_open_run_store", lambda *a, **k: nullcontext(store))
        monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
        monkeypatch.setattr(cli, "_preflight", lambda *a, **k: None)
        args = ["status", "--run", "r1"]
        if export == "json":
            args += ["--json"]
        result = CliRunner().invoke(cli.app, args)
        if result.exception is not None:
            raise result.exception
        assert result.exit_code == 0, result.output
        assert reads == [2]
        _assert_round10_status_identity(result.output, export)
        _assert_round10_status_sections(result.output, export)


def test_status_run_reads_a_pending_row_against_the_operator_s_budget(
    tmp_path, monkeypatch
):
    """`kew status --run` composes under THIS host's in-flight bound (#2279/D8).

    ``pending_run_max_age_s`` is twice the operator's ``agent.timeout_minutes``,
    floored at four hours, and the printed ``view_revision`` is what a scripted
    caller pins an action to — which the action gate re-derives under that same
    bound. Composed against the bare floor, a five-hour pending row on a
    six-hour agent printed ``run_abandoned`` with an "inspect" recommendation
    while every other surface on the host called the run live, and handed out a
    revision no action on that item could satisfy.
    """
    from datetime import UTC, datetime, timedelta

    from kew.config import AgentConfig
    from kew.service.runs import get_run_work_view
    from kew.service.work import pending_run_max_age_s

    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text("")
    (tmp_path / ".git").mkdir()
    config = _StatusWorkView._config(tmp_path)
    config.agent = AgentConfig(timeout_minutes=360)
    _StatusWorkView._admit(
        config,
        status="pending",
        started_at=datetime.now(UTC) - timedelta(hours=5),
        finished_at=None,
    )
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_detect_repo", lambda *a, **k: None)

    result = CliRunner().invoke(cli.app, ["status", "--run", "r1", "--json"])

    if result.exception is not None:
        raise result.exception
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["work"]["exception"]["state"] == "in_flight"
    assert payload["safest_action"]["action"] == "wait"
    # And the revision it prints is the one composed under the host's bound,
    # which is the one `perform_work_action` recomposes and compares against.
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        expected = get_run_work_view(
            store,
            "r1",
            delivery_repo=config.project.repo or None,
            pending_max_age_s=pending_run_max_age_s(config),
        )
    assert expected is not None
    assert payload["view_revision"] == expected.view_revision
