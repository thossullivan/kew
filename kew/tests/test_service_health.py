"""The health assembly's own rules (#2298), below the HTTP edge.

Three things are pinned here that a route test cannot reach: the mirrored review
runner (the service layer may not import ``kew.graph``, so the mirror is held to
the original), the probe cache (a page refresh must not re-probe docker), and the
4-layer config provenance shared with the CLI.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew.config import DispatchConfig, ReviewConfig, SandboxConfig, load_config
from kew.graph.driver.review_launcher import review_runner
from kew.service.config import CAP_FIELDS, config_snapshot, tracked_config_fields
from kew.service.health import (
    HealthProbes,
    ProbeCache,
    assemble_health,
    env_report,
    review_lane_runner,
)


class _Store:
    def get_meta(self, key: str) -> str | None:
        return None

    def count_container_runtimes(self, since: datetime) -> dict[str, int]:
        return {}

    def list_epic_refs(self) -> list[str]:
        return []

    def get_epic_driver_state(self, ref: str) -> Any:
        return None

    def get_fleet_stopped(self) -> Any:
        return None

    def list_pending(self) -> list[Any]:
        return []

    def list_loops(self, status: str | None = None) -> list[Any]:
        return []


def _probes(counter: dict[str, int] | None = None) -> HealthProbes:
    tally = counter if counter is not None else {}

    def image(_runtime: str, ref: str) -> bool:
        tally[ref] = tally.get(ref, 0) + 1
        return True

    return HealthProbes(
        runtime_present=lambda name: name == "docker",
        image_present=image,
        env_set=lambda name: False,
    )


@pytest.mark.parametrize("pinned", ["", "claude-code"])
def test_the_review_lane_runner_mirrors_the_launcher(pinned: str) -> None:
    """One answer to "who reviews?", held to the launcher that really launches.

    ``kew.service`` cannot import ``kew.graph`` (import-linter), so the health
    reading mirrors ``review_runner``. This is the test that keeps the mirror
    from becoming a second answer.
    """
    config = DispatchConfig(review=ReviewConfig(runner=pinned))
    assert review_lane_runner(config) == review_runner(config)


def test_a_second_read_reuses_the_cached_image_probe() -> None:
    """A tab open on /health must not shell out to docker every few seconds."""
    tally: dict[str, int] = {}
    cache = ProbeCache(_probes(tally))
    config = DispatchConfig(
        sandbox=SandboxConfig(images={"claude-code": "kew-agent:test"})
    )
    store = _Store()
    assemble_health(store, config, cache=cache)
    assemble_health(store, config, cache=cache)
    assert tally == {"kew-agent:test": 1}


def test_warming_the_cache_re_probes_so_a_repaired_host_is_seen() -> None:
    """The sweep's job: refresh, so the page does not serve a stale 'missing'."""
    tally: dict[str, int] = {}
    cache = ProbeCache(_probes(tally))
    config = DispatchConfig(
        sandbox=SandboxConfig(images={"claude-code": "kew-agent:test"})
    )
    assemble_health(_Store(), config, cache=cache)
    cache.warm(config)
    assert tally == {"kew-agent:test": 2}


def test_an_expired_entry_is_probed_again() -> None:
    tally: dict[str, int] = {}
    cache = ProbeCache(_probes(tally), ttl_s=0.0)
    config = DispatchConfig(
        sandbox=SandboxConfig(images={"claude-code": "kew-agent:test"})
    )
    assemble_health(_Store(), config, cache=cache)
    assemble_health(_Store(), config, cache=cache)
    assert tally == {"kew-agent:test": 2}


def test_env_report_reads_presence_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-token")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    report = env_report()
    by_name = {entry["name"]: entry for entry in report}
    assert by_name["GITHUB_TOKEN"]["set"] is True
    assert by_name["LINEAR_API_KEY"]["set"] is False
    assert "not-a-real-token" not in json.dumps(report)


def test_a_wedged_container_runtime_reads_unknown_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A docker daemon that never answers must not take /api/health down.

    This exercises the PRODUCTION image probe, not a fake one: the 15s bound on
    ``run`` expires as ``subprocess.TimeoutExpired``, which is a
    ``SubprocessError`` and NOT an ``OSError`` — so a handler listing only
    ``(OSError, ValueError)`` let it escape ``assemble_health`` and turned the
    documented ``unknown`` reading into a 500.
    """

    def wedged(cmd: list[str], **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr("kew.subprocess_utils.run", wedged)
    config = DispatchConfig(
        sandbox=SandboxConfig(
            runtime="docker", images={"claude-code": "kew-agent:test"}
        )
    )
    # Production probes — only the image probe's runtime is faked, by wedging
    # the subprocess chokepoint underneath it.
    reading = assemble_health(_Store(), config, cache=ProbeCache(HealthProbes()))
    by_ref = {entry["ref"]: entry for entry in reading["images"]}
    assert by_ref["kew-agent:test"]["present"] is None


def test_an_unreadable_heartbeat_ages_to_unknown_never_to_zero() -> None:
    """A stamp nobody can parse is tolerated, not repaired — and not aged to 0,
    which would read as "written just now"."""

    class BadStamp(_Store):
        def get_meta(self, key: str) -> str | None:
            return "yesterday-ish" if key == "scheduler_heartbeat" else None

    reading = assemble_health(
        BadStamp(),
        DispatchConfig(),
        cache=ProbeCache(_probes()),
        now=datetime.now(UTC),
    )
    assert reading["sweep"]["heartbeat_at"] == "yesterday-ish"
    assert reading["sweep"]["heartbeat_age_s"] is None


# ── the shared 4-layer provenance ───────────────────────────────────────────


def test_the_caps_the_console_omits_are_tracked_fields() -> None:
    assert set(tracked_config_fields()) >= CAP_FIELDS


def test_the_file_layer_is_the_file_and_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this issue names: ``env`` was being reported as ``kew.toml``.

    With ``KEW_MAX_WORKERS`` exported over a committed ``max_workers``, the
    snapshot must say the environment won — not describe the env number as
    something the file said, which is how a web override came to be shown as in
    force when it was not.
    """
    toml = tmp_path / "kew.toml"
    toml.write_text("[parallel]\nmax_workers = 2\n")
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.setenv("KEW_MAX_WORKERS", "7")
    effective = load_config(toml)
    snapshot = config_snapshot(effective, config_path=toml, overrides={})
    entry = snapshot["parallel.max_workers"]
    assert entry == {"value": 7, "source": "env (KEW_MAX_WORKERS)"}
    # And the file layer, asked for itself, answers 2 rather than 7.
    assert load_config(toml, env={}).parallel.max_workers == 2


def test_the_env_seam_leaves_discovery_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env={}`` suppresses the value layer, not which file is in force."""
    toml = tmp_path / "kew.toml"
    toml.write_text("[review]\nmax_rounds = 4\n")
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    assert load_config(toml, env={}).review.max_rounds == 4


# ── #2534: "down" counts only what is required AND down ─────────────────────


def _seed_driver(store: Any, ref: str, *, signal: str) -> None:
    """One epic with a driver row whose last tick was long ago."""
    stamp = "2026-07-23T00:00:00+00:00"
    snapshot = {"schema": 1, "epic_ref": ref, "signal": signal, "counts": {}}
    tick_id, _ = store.append_epic_tick(
        epic_ref=ref,
        tick_ts=stamp,
        snapshot=snapshot,
        actions={"dispatched": [], "merged": [], "stuck": []},
        snapshot_hash=f"h-{ref}",
    )
    store.begin_epic_tick(ref, stamp)
    store.finish_epic_tick(
        ref,
        tick_ts=stamp,
        duration_s=1.0,
        interval_s=60.0,
        signal=signal,
        tick_id=tick_id,
    )


def test_a_finished_or_archived_epic_s_driver_is_not_active(tmp_path: Path) -> None:
    """An archived or complete epic has no driver to lose, so its months-old
    silence is reported as inactive — the header counts only active drivers."""
    from kew.epic_archive import EPIC_ARCHIVED_EVENT
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _seed_driver(store, "finished", signal="complete")
        _seed_driver(store, "retired", signal="progressing")
        _seed_driver(store, "working", signal="progressing")
        store.append_audit_event(
            timestamp="2026-08-01T00:00:00+00:00",
            event=EPIC_ARCHIVED_EVENT,
            actor_type="human",
            actor_id="owner",
            payload={"epic_ref": "retired", "reason": "retired"},
        )
        reading = assemble_health(
            store,
            DispatchConfig(),
            cache=ProbeCache(_probes()),
            now=datetime(2026, 10, 8, tzinfo=UTC),
        )
    drivers = {row["epic_ref"]: row for row in reading["drivers"]}
    assert {ref: row["liveness"] for ref, row in drivers.items()} == {
        "finished": "gone",
        "retired": "gone",
        "working": "gone",
    }
    assert {ref: row["active"] for ref, row in drivers.items()} == {
        "finished": False,
        "retired": False,
        "working": True,
    }


def test_the_console_s_own_missing_credential_is_not_a_lane_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console holds no agent credential; the loops that dispatch do. The
    lane is not refused for it — the console's inability is its own field."""
    from kew.sandbox import (
        REFUSAL_CREDENTIAL_MISSING,
        REFUSAL_IMAGE_UNMAPPED,
        SandboxRuntimeUnavailable,
    )

    def refuse(reason: str) -> Any:
        def resolve(*_args: Any, **_kwargs: Any) -> Any:
            raise SandboxRuntimeUnavailable("refused", reason=reason)

        return resolve

    monkeypatch.setattr(
        "kew.service.health.resolve_effective_sandbox",
        refuse(REFUSAL_CREDENTIAL_MISSING),
    )
    reading = assemble_health(_Store(), DispatchConfig(), cache=ProbeCache(_probes()))
    assert reading["posture"]
    for lane in reading["posture"]:
        assert lane["refusal"] is None
        assert lane["console_refusal"] == REFUSAL_CREDENTIAL_MISSING
        assert lane["posture"] == "unknown"

    # Any other refusal is about the lane itself, and still refuses it.
    monkeypatch.setattr(
        "kew.service.health.resolve_effective_sandbox",
        refuse(REFUSAL_IMAGE_UNMAPPED),
    )
    reading = assemble_health(_Store(), DispatchConfig(), cache=ProbeCache(_probes()))
    for lane in reading["posture"]:
        assert lane["refusal"] == REFUSAL_IMAGE_UNMAPPED
        assert lane["console_refusal"] is None


# ── #2566: a serve-only credential and a slot-waiting loop are not "down" ────


def _claude_runs(store: Any, *, status: str, days_ago: float) -> None:
    from datetime import timedelta

    from kew.run_store import RunRecord

    started = datetime(2026, 10, 8, tzinfo=UTC) - timedelta(days=days_ago)
    store.save(
        RunRecord(
            run_id=f"r-{status}-{days_ago}",
            issue_num=1,
            branch="agent/issue-1",
            status=status,
            exit_code=0,
            started_at=started,
            finished_at=started,
            runner_name="claude-code",
        )
    )


def _claude_row(store: Any) -> dict[str, Any]:
    reading = assemble_health(
        store,
        DispatchConfig(),
        cache=ProbeCache(_probes()),
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    return next(row for row in reading["runners"] if row["name"] == "claude-code")


def test_a_credential_only_serve_lacks_is_a_note_once_a_run_succeeded(
    tmp_path: Path,
) -> None:
    """``kew serve`` holds no credential; the loops that dispatch do. A
    successful run in the window proves it, so the gap is a note."""
    from kew.run_store import SqliteRunStore
    from kew.service.health import SERVE_ONLY_CREDENTIAL_NOTE

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _claude_runs(store, status="success", days_ago=1)
        row = _claude_row(store)
    assert row["credential_names"]
    assert not any(cred["set"] for cred in row["credential_names"])
    assert row["succeeded_in_window"] == 1
    assert row["credential_note"] == SERVE_ONLY_CREDENTIAL_NOTE


@pytest.mark.parametrize(
    ("status", "days_ago"),
    [("failed", 1.0), ("success", 30.0)],
    ids=["only-a-failure", "success-outside-the-window"],
)
def test_without_a_successful_run_in_the_window_the_credential_stays_missing(
    tmp_path: Path, status: str, days_ago: float
) -> None:
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _claude_runs(store, status=status, days_ago=days_ago)
        row = _claude_row(store)
    assert row["succeeded_in_window"] == 0
    assert row["credential_note"] is None


def _seed_live_cadence_driver(store: Any, ref: str, *, ticked: datetime) -> None:
    """A driver with a 60s cadence whose last tick was at *ticked*."""
    stamp = ticked.isoformat()
    tick_id, _ = store.append_epic_tick(
        epic_ref=ref,
        tick_ts=stamp,
        snapshot={"schema": 1, "epic_ref": ref, "signal": "progressing"},
        actions={"dispatched": [], "merged": [], "stuck": []},
        snapshot_hash=f"h-{ref}",
    )
    store.begin_epic_tick(ref, stamp)
    store.finish_epic_tick(
        ref,
        tick_ts=stamp,
        duration_s=1.0,
        interval_s=60.0,
        signal="progressing",
        tick_id=tick_id,
    )


@pytest.mark.parametrize(
    ("beat_age_s", "liveness"),
    [(None, "gone"), (30.0, "waiting"), (119.0, "waiting"), (121.0, "gone")],
    ids=["no-beat", "fresh", "just-inside-twice", "stale"],
)
def test_a_loop_waiting_for_a_slot_reads_waiting_until_its_beat_goes_stale(
    tmp_path: Path, beat_age_s: float | None, liveness: str
) -> None:
    """Inside twice the cadence a "waiting: slot" heartbeat reads ``waiting``;
    past it, the driver is gone again — the loop may have died waiting."""
    from datetime import timedelta

    from kew.run_store import SqliteRunStore
    from kew.service.epics import record_driver_waiting

    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _seed_live_cadence_driver(
            store, "host-contract", ticked=now - timedelta(hours=1)
        )
        if beat_age_s is not None:
            record_driver_waiting(
                store, "host-contract", "slot", now=now - timedelta(seconds=beat_age_s)
            )
        reading = assemble_health(
            store, DispatchConfig(), cache=ProbeCache(_probes()), now=now
        )
    (row,) = reading["drivers"]
    assert row["active"] is True
    assert row["liveness"] == liveness
    assert row["waiting_reason"] == ("slot" if liveness == "waiting" else None)


def test_a_ticking_driver_is_not_downgraded_to_waiting(tmp_path: Path) -> None:
    """A live tick is stronger evidence than the loop's own word."""
    from datetime import timedelta

    from kew.run_store import SqliteRunStore
    from kew.service.epics import record_driver_waiting

    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _seed_live_cadence_driver(store, "e", ticked=now - timedelta(seconds=10))
        record_driver_waiting(store, "e", "slot", now=now)
        reading = assemble_health(
            store, DispatchConfig(), cache=ProbeCache(_probes()), now=now
        )
    assert reading["drivers"][0]["liveness"] == "live"
    assert reading["drivers"][0]["waiting_reason"] is None
