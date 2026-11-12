"""Tests for the kew health check module (#247)."""

from datetime import UTC, datetime, timedelta

from kew.health import (
    CheckResult,
    HealthReport,
    check_audit_parses,
    check_meter_running,
)
from kew.run_store import RunRecord
from kew.switchyard import SwitchyardProbeResult


def _run(
    model: str, *, cost: float = 0.0, tokens: int = 0, status: str = "success"
) -> RunRecord:
    return RunRecord(
        run_id=f"r-{model}-{cost}-{tokens}",
        issue_num=1,
        branch="b",
        status=status,
        exit_code=0,
        started_at=datetime(2026, 6, 25, tzinfo=UTC),
        model=model,
        estimated_cost_usd=cost,
        total_tokens=tokens,
    )


def test_report_exit_code_and_healthy():
    ok = CheckResult("a", "pass", "", {})
    warn = CheckResult("b", "warn", "", {})
    bad = CheckResult("c", "fail", "", {})
    assert HealthReport([ok, warn]).healthy is True
    assert HealthReport([ok, warn]).exit_code == 0
    assert HealthReport([ok, bad]).healthy is False
    assert HealthReport([ok, bad]).exit_code == 1


def test_meter_skip_when_only_free():
    runs = [_run("ollama/qwen3", cost=0.0, tokens=500)]
    assert check_meter_running(runs).status == "skip"


def test_meter_pass_when_all_runs_metered():
    runs = [_run("claude-sonnet-4-6", cost=0.5), _run("claude-opus-4-6", tokens=1000)]
    assert check_meter_running(runs).status == "pass"


def test_meter_fail_when_no_run_metered():
    runs = [_run("claude-sonnet-4-6", cost=0.0, tokens=0)]
    res = check_meter_running(runs)
    assert res.status == "fail"
    assert res.detail == {"runs": 1, "metered": 0, "unknown_priced": 0}


def test_meter_warn_when_some_unmetered():
    runs = [
        _run("claude-sonnet-4-6", cost=0.5),
        _run("claude-opus-4-6", cost=0.0, tokens=0),
    ]
    assert check_meter_running(runs).status == "warn"


def test_meter_includes_unknown_priced_models():
    # a non-free model with no pricing entry is still checked (not skipped)
    metered = check_meter_running([_run("gpt-4o", tokens=900)])
    assert metered.status == "pass"
    assert metered.detail["unknown_priced"] == 1
    unmetered = check_meter_running([_run("gpt-4o", cost=0.0, tokens=0)])
    assert unmetered.status == "fail"


def test_audit_parses_pass_fail_skip():
    assert check_audit_parses(0, 0).status == "skip"
    assert check_audit_parses(5, 0).status == "pass"
    assert check_audit_parses(5, 2).status == "fail"


def test_spend_within_limits_pass_warn_fail_skip():
    from kew.config import BudgetConfig
    from kew.health import check_spend_within_limits

    b = BudgetConfig(daily_usd=50.0, weekly_usd=200.0)
    assert check_spend_within_limits(10.0, 50.0, b, guard_fired=0).status == "pass"
    assert (
        check_spend_within_limits(45.0, 50.0, b, guard_fired=0).status == "warn"
    )  # >=80% daily
    assert (
        check_spend_within_limits(60.0, 50.0, b, guard_fired=1).status == "fail"
    )  # over daily
    # worst-of-daily-weekly: weekly over even though daily fine
    assert check_spend_within_limits(1.0, 250.0, b, guard_fired=0).status == "fail"
    # zero limits -> skip
    z = BudgetConfig(daily_usd=0.0, weekly_usd=0.0)
    assert check_spend_within_limits(5.0, 5.0, z, guard_fired=0).status == "skip"


def test_runner_reachable_pass_warn_fail_skip():
    from kew.health import check_runner_reachable
    from kew.subprocess_utils import RunnerReachability

    ok = RunnerReachability("claude-code", "claude", True, True)
    assert check_runner_reachable([ok]).status == "pass"
    no_cred = RunnerReachability("claude-code", "claude", True, False)
    res = check_runner_reachable([ok, no_cred])
    assert res.status == "warn"
    assert "no credentials detected" in res.summary
    missing = RunnerReachability("opencode", "opencode", False, None)
    # a missing tier runner fails even when the default is fine (the I1 case)
    assert check_runner_reachable([ok, missing]).status == "fail"
    assert check_runner_reachable([]).status == "skip"


def test_codex_runner_health_fails_closed_without_version_or_login():
    from kew.health import check_runner_reachable
    from kew.subprocess_utils import RunnerReachability

    unsupported = RunnerReachability(
        "codex",
        "codex",
        True,
        True,
        version="0.144.9",
        version_supported=False,
    )
    result = check_runner_reachable([unsupported])
    assert result.status == "fail"
    assert result.detail["runners"] == [
        {
            "runner": "codex",
            "binary": "codex",
            "binary_present": True,
            "version": "0.144.9",
            "version_supported": False,
            "login": True,
            "credential_detected": True,
        }
    ]

    logged_out = RunnerReachability(
        "codex",
        "codex",
        True,
        False,
        version="0.145.0",
        version_supported=True,
    )
    assert check_runner_reachable([logged_out]).status == "fail"


def test_dispatch_state_enabled_and_halted():
    from kew.health import check_dispatch_state

    enabled = check_dispatch_state(None, None)
    assert enabled.status == "pass"
    assert enabled.summary == "enabled"
    halted = check_dispatch_state({"reason": "daily spend $5.00 exceeds $0.01"}, None)
    assert halted.status == "warn"  # visible but non-paging (exit stays 0)
    assert "HALTED" in halted.summary
    assert halted.detail["halted"] is True


def test_github_api_check_surfaces_the_cached_rate_limit_reading():
    """#2297 review: the sweep cached a reading nobody read, so a 401 — kew's
    own auth probe failing — was surfaced nowhere."""
    import json

    from kew.health import check_github_api

    assert check_github_api(None).status == "skip"
    assert check_github_api("{not json").status == "warn"

    unauthorized = check_github_api(
        json.dumps({"ok": False, "status": 401, "reason": "unauthorized"})
    )
    assert unauthorized.status == "fail"
    assert "401" in unauthorized.summary

    unavailable = check_github_api(json.dumps({"ok": False, "reason": "unavailable"}))
    assert unavailable.status == "warn"

    healthy = check_github_api(
        json.dumps(
            {
                "ok": True,
                "status": 200,
                "core": {"limit": 5000, "remaining": 4987, "reset_at": None},
                "scopes": ["repo"],
                "observed_at": "2026-10-06T12:00:00+00:00",
            }
        ),
        # Pinned: the verdict depends on the reading's AGE, so a wall-clock
        # `now` would make this test pass or fail by the hour it ran.
        now=datetime(2026, 10, 6, 12, 0, 30, tzinfo=UTC),
    )
    assert healthy.status == "pass"
    assert "4987" in healthy.summary
    assert healthy.detail["scopes"] == ["repo"]


def test_an_aged_out_rate_limit_reading_is_history_not_health():
    """#2297 review: the cached reading's own `observed_at` was placed in
    `detail` and then ignored, so a reading of ANY age was reported as current
    health. The serve sweep is its only writer and stops refreshing the moment
    `kew serve` stops: a year-old `ok` read `pass` after the token was revoked,
    and a year-old 401 held `kew health` at exit 1 forever after the credential
    was fixed."""
    import json

    from kew.health import STALE_SWEEP_SECONDS, check_github_api

    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    year_old = (now - timedelta(days=365)).isoformat()

    stale_ok = check_github_api(
        json.dumps({"ok": True, "core": {"remaining": 4987}, "observed_at": year_old}),
        now=now,
    )
    assert stale_ok.status == "warn"
    assert "stale" in stale_ok.summary
    assert stale_ok.detail["stale"] is True
    assert stale_ok.detail["observed_age_s"] > STALE_SWEEP_SECONDS

    stale_401 = check_github_api(
        json.dumps(
            {
                "ok": False,
                "status": 401,
                "reason": "unauthorized",
                "observed_at": year_old,
            }
        ),
        now=now,
    )
    assert stale_401.status == "warn"  # no longer pinning exit 1 forever

    # A reading inside the window keeps its verdict, either way.
    fresh = (now - timedelta(seconds=90)).isoformat()
    assert (
        check_github_api(
            json.dumps({"ok": True, "core": {"remaining": 1}, "observed_at": fresh}),
            now=now,
        ).status
        == "pass"
    )
    assert (
        check_github_api(
            json.dumps({"ok": False, "reason": "unauthorized", "observed_at": fresh}),
            now=now,
        ).status
        == "fail"
    )


def test_a_dead_serve_sweep_is_visible_in_dispatch_state():
    """#2297 review: `sweep_cycle` writes `scheduler_heartbeat` so this check can
    see a dead sweep — but the stamp only landed in `detail`, so the check
    returned `pass` identically with no heartbeat, a 2019 heartbeat and a fresh
    one. A dead sweep takes outcome sync, orphan reconciliation and the auth
    probe down with it."""
    from kew.health import check_dispatch_state

    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    fresh = check_dispatch_state(
        None, (now - timedelta(seconds=90)).isoformat(), now=now
    )
    assert fresh.status == "pass"
    assert fresh.detail["scheduler_stale"] is False

    dead = check_dispatch_state(None, "2019-01-01T00:00:00+00:00", now=now)
    assert dead.status == "warn"
    assert "stale" in dead.summary
    assert dead.detail["scheduler_stale"] is True

    # No heartbeat at all stays a pass: a control plane that never ran
    # `kew serve` is not unhealthy for it, and absence is not failure.
    never = check_dispatch_state(None, None, now=now)
    assert never.status == "pass"
    assert never.detail["scheduler_heartbeat_age_s"] is None

    # A halted fleet still reads HALTED, and says the sweep is stale too.
    halted = check_dispatch_state(
        {"reason": "daily spend"}, "2019-01-01T00:00:00+00:00", now=now
    )
    assert halted.status == "warn"
    assert "HALTED" in halted.summary and "stale" in halted.summary


def test_a_failing_auth_probe_fails_the_health_report(tmp_path, monkeypatch):
    """The reading has to reach the report's exit code, not just a detail."""
    import json

    from kew.config import DispatchConfig
    from kew.health import run_health_checks
    from kew.run_store import SqliteRunStore
    from kew.subprocess_utils import RunnerReachability

    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as store:
        store.set_meta(
            "github_rate_limit",
            json.dumps({"ok": False, "status": 401, "reason": "unauthorized"}),
        )
        report = run_health_checks(store, DispatchConfig(), window_days=7)

    check = next(c for c in report.checks if c.name == "github_api")
    assert check.status == "fail"
    assert report.healthy is False
    assert report.exit_code == 1


def test_run_health_checks_end_to_end(tmp_path, monkeypatch):
    from kew.config import DispatchConfig
    from kew.health import run_health_checks
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.subprocess_utils import RunnerReachability

    config = DispatchConfig()
    config.budget.daily_usd = 0.01  # tiny limit so the seeded spend breaches it
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as store:
        store.save(
            RunRecord(
                run_id="x",
                issue_num=1,
                branch="b",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                model="claude-sonnet-4-6",
                estimated_cost_usd=5.0,
                total_tokens=1000,
            )
        )

    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    with SqliteRunStore(db) as store:
        report = run_health_checks(store, config, window_days=7)

    names = {c.name for c in report.checks}
    assert names == {
        "meter_running",
        "spend_within_limits",
        "audit_parses",
        "runner_reachable",
        "dispatch_state",
        "github_api",
        "switchyard",
    }
    spend = next(c for c in report.checks if c.name == "spend_within_limits")
    assert spend.status == "fail"  # $5 spend over $0.01 daily limit
    switchyard = next(c for c in report.checks if c.name == "switchyard")
    assert switchyard.status == "skip"
    assert report.exit_code == 1


def test_switchyard_health_skips_direct_and_maps_probe_verdict():
    from kew.config import DispatchConfig
    from kew.health import _configured_switchyard_result, check_switchyard

    assert check_switchyard(None).status == "skip"
    ready = SwitchyardProbeResult(
        admitted=True,
        reason="ready",
        endpoint="https://switchyard.example",
        route="switchyard/passthrough",
        observed_version="0.1.0",
    )
    assert check_switchyard(ready).status == "pass"
    refused = SwitchyardProbeResult(
        admitted=False,
        reason="missing_route",
        endpoint="https://switchyard.example",
        route="switchyard/passthrough",
        observed_version="0.1.0",
    )
    result = check_switchyard(refused)
    assert result.status == "fail"
    assert result.summary == "refused: missing_route"
    assert result.detail["endpoint"] == "https://switchyard.example"

    configured = DispatchConfig.model_validate(
        {
            "inference_transport": {
                "mode": "switchyard",
                "switchyard": {
                    "endpoint": "https://switchyard.example",
                    "route": "switchyard/passthrough",
                    "expected_version": "0.1.0",
                },
            }
        }
    )
    missing_probe = _configured_switchyard_result(configured, None)
    assert missing_probe is not None
    assert check_switchyard(missing_probe).summary == "refused: not_probed"

    configured.inference_transport.switchyard.endpoint = (
        "https://token@example.test/private?api_key=secret"
    )
    sanitized = _configured_switchyard_result(configured, None)
    assert sanitized is not None
    detail = check_switchyard(sanitized).detail
    assert detail["endpoint"] == "https://example.test"
    assert "token" not in repr(detail)
    assert "api_key" not in repr(detail)
