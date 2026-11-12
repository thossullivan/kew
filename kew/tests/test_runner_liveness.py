from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from kew.runner_liveness import check_runner_liveness


def _probe_token(pid):
    return f"synthetic-token-{pid}"


def test_dispatch_probe_uses_owner_after_retry(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig
    from kew.dispatch_core import _record_retry_pgid
    from kew.graph.driver.tick import default_run_liveness
    from kew.run_store import RunRecord, SqliteRunStore

    monkeypatch.setattr(
        "kew.graph.driver.tick._owner_alive", lambda row: row.pgid == 111
    )
    # Synthetic tokens support registration while this test checks owner selection.
    monkeypatch.setattr("kew.process_groups.process_start_token", _probe_token)
    monkeypatch.setattr("kew.run_store.process_start_token", _probe_token)
    monkeypatch.setattr("kew.process_groups.group_alive", Mock(return_value=False))
    monkeypatch.setattr("kew.process_groups.owner_alive", lambda *a: True)
    monkeypatch.setattr("kew.run_store.birth_matches", lambda pid, birth: True)
    monkeypatch.setattr("kew.run_store.attempt_descends_from", lambda pgid, owner: True)
    monkeypatch.setattr("kew.run_store.owner_alive", lambda *a: True)
    monkeypatch.setattr("kew.run_store.process_start_time", lambda p: "synthetic-birth")
    with SqliteRunStore(str(tmp_path / "probe.db")) as store:
        store.save(
            RunRecord(
                run_id="retry",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=111,
                owner_pid=111,
                owner_start_token=_probe_token(111),
            )
        )
        _record_retry_pgid(store, "retry", 222)
        probe = default_run_liveness(DispatchConfig())
        assert probe(store.get("retry")) is True
        row = store.get("retry")
        row.pgid = row.owner_pid = 333
        row.owner_start_token = _probe_token(333)
        store.save(row, update_ownership=True)
        _record_retry_pgid(store, "retry", 111)
        assert probe(store.get("retry")) is False


def test_contained_probe_keeps_container_and_startup_grace(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from kew.config import DispatchConfig
    from kew.graph.driver.tick import _pending_run_is_dead, default_run_liveness
    from kew.run_store import RunRecord

    now = datetime.now(UTC)
    record = RunRecord(
        run_id="contained",
        issue_num=42,
        branch="agent/issue-42",
        status="pending",
        exit_code=-1,
        started_at=now,
        pgid=111,
        owner_pid=111,
        container_name="kew-42",
    )
    monkeypatch.setattr("kew.graph.driver.tick._owner_alive", lambda row: False)
    monkeypatch.setattr("kew.graph.driver.tick._container_alive", lambda *args: False)
    probe = default_run_liveness(DispatchConfig())
    assert probe(record) is False
    assert not _pending_run_is_dead(record, probe, now=now)
    assert _pending_run_is_dead(record, probe, now=now + timedelta(hours=1))


def _result(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_check_runner_liveness_reports_offline_runners(monkeypatch) -> None:
    responses = iter(
        [
            _result(stdout=json.dumps({"value": "Operator-Mac-1"})),
            _result(
                stdout=json.dumps(
                    {
                        "total_count": 2,
                        "runners": [
                            {"name": "Operator-Mac-1", "status": "offline"},
                            {"name": "Operator-Mac-1b", "status": "online"},
                        ],
                    }
                )
            ),
        ]
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "kew.runner_liveness.run",
        lambda cmd, **kwargs: calls.append(cmd) or next(responses),
    )

    result = check_runner_liveness("owner/repo")

    assert result.configured is True
    assert result.offline_runners == ("Operator-Mac-1",)
    assert calls == [
        ["gh", "api", "repos/owner/repo/actions/variables/RUNNER_TARGET"],
        [
            "gh",
            "api",
            "repos/owner/repo/actions/runners",
            "--paginate",
            "--slurp",
        ],
    ]


def test_check_runner_liveness_skips_when_target_variable_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "kew.runner_liveness.run",
        lambda cmd, **kwargs: _result(stderr="HTTP 404: Not Found", returncode=1),
    )

    result = check_runner_liveness("owner/repo")

    assert result.configured is False
    assert result.offline_runners == ()
    assert result.error is None


def test_check_runner_liveness_fails_soft_on_api_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "kew.runner_liveness.run",
        lambda cmd, **kwargs: _result(stderr="HTTP 500: server error", returncode=1),
    )

    result = check_runner_liveness("owner/repo")

    assert result.configured is False
    assert result.offline_runners == ()
    assert result.error is not None
    assert "HTTP 500" in result.error


def test_check_runner_liveness_handles_paginated_runner_payload(monkeypatch) -> None:
    responses = iter(
        [
            _result(stdout=json.dumps({"value": "self-hosted"})),
            _result(
                stdout=json.dumps(
                    [
                        {"runners": [{"name": "one", "status": "online"}]},
                        {"runners": [{"name": "two", "status": "offline"}]},
                    ]
                )
            ),
        ]
    )
    monkeypatch.setattr(
        "kew.runner_liveness.run", lambda cmd, **kwargs: next(responses)
    )

    result = check_runner_liveness("owner/repo")

    assert result.offline_runners == ("two",)
