"""kew health — read-only attestation that the control plane is functioning (#247).

Pure check functions plus a thin aggregator. Each check returns a CheckResult;
the CLI renders the HealthReport and maps it to an exit code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from kew.config import BudgetConfig
from kew.pricing import catalog_pricing, is_free_model, resolve_pricing_key
from kew.subprocess_utils import RunnerReachability, inspect_runners

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord, RunStore
    from kew.switchyard import SwitchyardProbeResult

Status = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True)
class CheckResult:
    """One health check's verdict."""

    name: str
    status: Status
    summary: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class HealthReport:
    """The aggregate of all checks."""

    checks: list[CheckResult]

    @property
    def healthy(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 1


def _should_meter(model: str) -> bool:
    """True for a non-free, non-empty model — includes models with no pricing
    entry (e.g. gpt-4o), which must not blind-spot the meter check."""
    return bool(model) and not is_free_model(model)


def _meter_verdict(metered: int, total: int, detail: dict[str, Any]) -> CheckResult:
    """Map metered/total counts to the appropriate CheckResult status."""
    label = f"{metered}/{total} runs metered"
    if metered == total:
        return CheckResult("meter_running", "pass", label, detail)
    if metered == 0:
        return CheckResult(
            "meter_running",
            "fail",
            f"0/{total} runs metered — meter may be down",
            detail,
        )
    return CheckResult("meter_running", "warn", label, detail)


def check_meter_running(runs: list[RunRecord]) -> CheckResult:
    """Are runs recording cost/tokens? A run that produced $0 and 0 tokens means
    cost capture broke (the ccusage silent-zero class). The caller passes only
    SUCCESSFUL runs — a failed/killed run legitimately has no usage."""
    candidates = [r for r in runs if _should_meter(r.model)]
    if not candidates:
        return CheckResult(
            "meter_running",
            "skip",
            "no successful non-free runs in window",
            {"runs": 0, "unknown_priced": 0},
        )
    metered = [r for r in candidates if r.estimated_cost_usd > 0 or r.total_tokens > 0]
    prices = catalog_pricing()
    unknown_priced = sum(
        1 for r in candidates if resolve_pricing_key(r.model, prices) is None
    )
    detail = {
        "runs": len(candidates),
        "metered": len(metered),
        "unknown_priced": unknown_priced,
    }
    return _meter_verdict(len(metered), len(candidates), detail)


_RANK: dict[Status, int] = {"skip": 0, "pass": 0, "warn": 1, "fail": 2}  # nosec B105 — status ranks, not a password


def _budget_subverdict(spent: float, limit: float, label: str) -> tuple[Status, str]:
    if limit <= 0:
        return "skip", f"{label} limit unset"
    if spent > limit:
        return "fail", f"{label} ${spent:.2f} over ${limit:.2f}"
    if spent >= 0.8 * limit:
        return "warn", f"{label} ${spent:.2f} near ${limit:.2f}"
    return "pass", f"{label} ${spent:.2f} of ${limit:.2f}"


def check_spend_within_limits(
    daily_spent: float,
    weekly_spent: float,
    budget: BudgetConfig,
    *,
    guard_fired: int,
) -> CheckResult:
    """Is current spend within the configured limits? A limit of 0 is unset
    (dormant). Attests spend is within bounds — NOT that the guard enforced them
    (that needs per-dispatch attestation, out of scope). guard_fired is the window
    count of budget_exceeded + circuit_breaker runs (supporting evidence). Its
    window is fixed at daily/last-7-days (mirrors the live guard), not --window."""
    subs = [
        _budget_subverdict(daily_spent, budget.daily_usd, "daily"),
        _budget_subverdict(weekly_spent, budget.weekly_usd, "weekly"),
    ]
    detail = {
        "daily_spent": round(daily_spent, 4),
        "weekly_spent": round(weekly_spent, 4),
        "daily_limit": budget.daily_usd,
        "weekly_limit": budget.weekly_usd,
        "guard_fired": guard_fired,
        "window": "daily/7d (fixed)",
    }
    non_skip = [s for s in subs if s[0] != "skip"]
    if not non_skip:
        return CheckResult(
            "spend_within_limits", "skip", "no budget limits configured", detail
        )
    status, summary = max(non_skip, key=lambda s: _RANK[s[0]])
    return CheckResult(
        "spend_within_limits",
        status,
        f"{summary} (guard fired {guard_fired}x)",
        detail,
    )


def _runner_verdict(
    reaches: list[RunnerReachability], detail: dict[str, Any]
) -> CheckResult:
    """Map a non-empty list of reaches to a CheckResult (pass/warn/fail)."""
    checks = (
        _runner_binary_failure,
        _runner_version_failure,
        _runner_login_failure,
        _runner_credential_warning,
    )
    for check in checks:
        verdict = check(reaches, detail)
        if verdict is not None:
            return verdict
    return CheckResult(
        "runner_reachable",
        "pass",
        f"{len(reaches)} runner(s) reachable",
        detail,
    )


def _runner_binary_failure(
    reaches: list[RunnerReachability], detail: dict[str, Any]
) -> CheckResult | None:
    missing = [r for r in reaches if r.binary is not None and not r.binary_present]
    if not missing:
        return None
    names = ", ".join(f"{r.runner_name}:{r.binary}" for r in missing)
    return CheckResult(
        "runner_reachable", "fail", f"runner binary not on PATH: {names}", detail
    )


def _runner_version_failure(
    reaches: list[RunnerReachability], detail: dict[str, Any]
) -> CheckResult | None:
    bad_versions = [r for r in reaches if r.version_supported is False]
    if not bad_versions:
        return None
    names = ", ".join(r.runner_name for r in bad_versions)
    return CheckResult(
        "runner_reachable",
        "fail",
        f"runner version unsupported or unparseable: {names}",
        detail,
    )


def _runner_login_failure(
    reaches: list[RunnerReachability], detail: dict[str, Any]
) -> CheckResult | None:
    codex_logged_out = [
        r
        for r in reaches
        if r.runner_name == "codex" and r.credential_detected is not True
    ]
    if not codex_logged_out:
        return None
    return CheckResult(
        "runner_reachable",
        "fail",
        "codex subscription login unavailable",
        detail,
    )


def _runner_credential_warning(
    reaches: list[RunnerReachability], detail: dict[str, Any]
) -> CheckResult | None:
    no_cred = [r for r in reaches if r.credential_detected is False]
    if not no_cred:
        return None
    names = ", ".join(r.runner_name for r in no_cred)
    return CheckResult(
        "runner_reachable",
        "warn",
        f"reachable but no credentials detected (env var or CLI login): {names}",
        detail,
    )


def check_runner_reachable(reaches: list[RunnerReachability]) -> CheckResult:
    """Can we dispatch with every configured tier runner? Worst-of across them."""
    if not reaches:
        return CheckResult(
            "runner_reachable", "skip", "no runner resolved", {"runners": []}
        )
    detail = {"runners": [_runner_reachability_detail(r) for r in reaches]}
    return _runner_verdict(reaches, detail)


def _runner_reachability_detail(reach: RunnerReachability) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "runner": reach.runner_name,
        "binary": reach.binary,
        "binary_present": reach.binary_present,
        "version": reach.version,
        "version_supported": reach.version_supported,
        "login": (reach.credential_detected if reach.runner_name == "codex" else None),
        "credential_detected": reach.credential_detected,
    }
    return detail


#: How old the serve sweep's writes may be before this build stops reading them
#: as current. The sweep runs every ``kew.serve.sweep.DEFAULT_INTERVAL_S`` (60s)
#: and is the ONLY writer of both the scheduler heartbeat and the cached
#: rate-limit reading, so they stop refreshing the moment ``kew serve`` stops.
#: Ten cycles absorb a slow sweep; past that the stamp is history, not health.
STALE_SWEEP_SECONDS = 600.0


def _stamp_age_s(stamp: str | None, now: datetime) -> float | None:
    """Seconds since an ISO stamp, or None when there is none to read.

    A naive stamp is read as UTC (kew's writers stamp in UTC only). An
    unreadable one is None — "we cannot age this" — never 0, which would read as
    "written just now"; it is logged, because a verdict that then stands on an
    un-aged stamp deserves to be explainable.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        _logger.warning(
            "health: sweep stamp %r is unreadable; age reads unknown", stamp
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds()


def _stale_sweep_note(age_s: float | None) -> str:
    """The suffix a verdict carries when the sweep's stamp is too old to trust."""
    if age_s is None or age_s <= STALE_SWEEP_SECONDS:
        return ""
    return f" — stale ({int(age_s // 60)}m old, is kew serve running?)"


def check_dispatch_state(
    fleet_stopped: dict[str, str] | None,
    heartbeat: str | None,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Is dispatch enabled or halted by the fleet latch, and is the serve sweep
    still beating? warn (visible, non-paging) when latched — by kew stop OR the
    breaker — so a stuck fleet isn't hidden behind green.

    The ``scheduler_heartbeat`` the serve sweep writes each cycle is THRESHOLDED
    here, not merely reported: carried in ``detail`` alone this check returned
    ``pass`` identically with no heartbeat, a 2019 heartbeat and a fresh one, so
    a sweep that died (and with it outcome sync, orphan reconciliation and the
    auth probe) read exactly like a healthy one (#2297 review). A stale beat is a
    warn; NO beat stays a pass, because a control plane that has never run
    ``kew serve`` is not unhealthy for it — absence is not failure.
    """
    reason = (fleet_stopped or {}).get("reason", "")
    age_s = _stamp_age_s(heartbeat, now or datetime.now(UTC))
    stale = _stale_sweep_note(age_s)
    detail = {
        "halted": fleet_stopped is not None,
        "reason": reason,
        "scheduler_heartbeat": heartbeat,
        "scheduler_heartbeat_age_s": age_s,
        "scheduler_stale": bool(stale),
    }
    if fleet_stopped is None:
        if stale:
            return CheckResult(
                "dispatch_state", "warn", f"enabled, serve sweep{stale}", detail
            )
        return CheckResult("dispatch_state", "pass", "enabled", detail)
    summary = f"HALTED — {reason}" if reason else "HALTED"
    return CheckResult("dispatch_state", "warn", f"{summary}{stale}", detail)


def check_github_api(cached: str | None, *, now: datetime | None = None) -> CheckResult:
    """Is the GitHub credential working, and how much API budget is left?

    Reads ONLY the reading the serve sweep already cached in the run store
    (``github_rate_limit``) — no GitHub call of its own, so ``kew health`` stays
    free and offline-safe. A 401 reading is a **fail**: that reading is kew's
    auth probe, and before #2297 nothing read it, so a broken credential was
    surfaced nowhere. A reading that could not be taken at all is a warn, and no
    reading (no sweep has run here) is a skip — absence is not health.

    The reading's own ``observed_at`` is thresholded against
    ``STALE_SWEEP_SECONDS``, because the sweep is its only writer and stops
    refreshing it when ``kew serve`` stops: untested, a year-old ``ok`` passed
    long after the token was revoked, and a year-old 401 held ``kew health`` at
    exit 1 forever after the credential was fixed (#2297 review). A stale
    reading is a warn that says how old it is — history, not health. A reading
    with no ``observed_at`` at all cannot be aged, so its verdict stands.
    """
    if not cached:
        return CheckResult(
            "github_api", "skip", "no cached rate-limit reading", {"cached": False}
        )
    try:
        reading = json.loads(cached)
    except ValueError as exc:
        # Not a clean default: the warn verdict CARRIES why it could not be read.
        return CheckResult(
            "github_api",
            "warn",
            f"cached reading unparseable: {exc}",
            {"cached": True, "reason": "unparseable"},
        )
    if not isinstance(reading, dict):
        return CheckResult(
            "github_api",
            "warn",
            f"cached reading is {type(reading).__name__}, not an object",
            {"cached": True, "reason": "unparseable"},
        )
    age_s = _stamp_age_s(reading.get("observed_at"), now or datetime.now(UTC))
    stale = _stale_sweep_note(age_s)
    detail = {
        "observed_at": reading.get("observed_at"),
        "observed_age_s": age_s,
        "stale": bool(stale),
        "ok": reading.get("ok"),
        "status": reading.get("status"),
        "reason": reading.get("reason"),
        "core": reading.get("core"),
        "graphql": reading.get("graphql"),
        "search": reading.get("search"),
        "scopes": reading.get("scopes"),
    }
    if stale:
        # Neither verdict survives the age: a stale `ok` is not a working
        # credential and a stale 401 is not a broken one. The warn is visible and
        # non-paging, and says what to do about it.
        return CheckResult("github_api", "warn", f"cached reading{stale}", detail)
    if reading.get("ok"):
        remaining = (reading.get("core") or {}).get("remaining")
        return CheckResult("github_api", "pass", f"core {remaining} left", detail)
    reason = str(reading.get("reason") or "unavailable")
    if reason == "unauthorized":
        return CheckResult("github_api", "fail", "GitHub auth failing (401)", detail)
    return CheckResult("github_api", "warn", f"no reading — {reason}", detail)


def check_audit_parses(total: int, malformed: int) -> CheckResult:
    """Does the audit log parse end-to-end?"""
    detail = {"total": total, "malformed": malformed}
    if total == 0:
        return CheckResult("audit_parses", "skip", "no audit events in window", detail)
    if malformed == 0:
        return CheckResult(
            "audit_parses", "pass", f"{total} events parse cleanly", detail
        )
    return CheckResult(
        "audit_parses", "fail", f"{malformed}/{total} events malformed", detail
    )


def check_switchyard(result: SwitchyardProbeResult | None) -> CheckResult:
    """Expose the sanitized Switchyard preflight verdict in control-plane health."""
    if result is None:
        return CheckResult(
            "switchyard", "skip", "direct inference transport", {"mode": "direct"}
        )
    detail = {
        "endpoint": result.endpoint,
        "route": result.route,
        "observed_version": result.observed_version,
        "reason": result.reason,
    }
    if result.admitted:
        return CheckResult("switchyard", "pass", "ready", detail)
    return CheckResult("switchyard", "fail", f"refused: {result.reason}", detail)


def _configured_switchyard_result(
    config: DispatchConfig, result: SwitchyardProbeResult | None
) -> SwitchyardProbeResult | None:
    if config.inference_transport.mode == "direct":
        return None
    if result is not None:
        return result
    from kew.switchyard import SwitchyardProbeResult, sanitize_endpoint

    switchyard = config.inference_transport.switchyard
    return SwitchyardProbeResult(
        admitted=False,
        reason="not_probed",
        endpoint=sanitize_endpoint(switchyard.endpoint),
        route=switchyard.route,
    )


def run_health_checks(
    store: RunStore,
    config: DispatchConfig,
    *,
    window_days: int,
    switchyard_probe: SwitchyardProbeResult | None = None,
) -> HealthReport:
    """Fetch the data each check needs and assemble the report (does the I/O)."""
    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)

    window_runs = store.list_since(window_start)
    # Only successful runs are held to the metering bar — a failed/killed run
    # legitimately has 0 cost/0 tokens (it aborted before metering).
    successful = [r for r in window_runs if r.status == "success"]

    recent = store.list_since(week_start)
    daily_spent = sum(r.estimated_cost_usd for r in recent if r.started_at >= day_start)
    weekly_spent = sum(r.estimated_cost_usd for r in recent)
    guard_fired = sum(
        1
        for r in window_runs
        if r.status in ("budget_exceeded", "budget_stopped", "budget_stop")
        or r.error_category == "circuit_breaker"
    )

    total, malformed = store.audit_parse_report(window_start)

    return HealthReport(
        checks=[
            check_meter_running(successful),
            check_spend_within_limits(
                daily_spent, weekly_spent, config.budget, guard_fired=guard_fired
            ),
            check_audit_parses(total, malformed),
            check_runner_reachable(inspect_runners(config)),
            check_dispatch_state(
                store.get_fleet_stopped(),
                store.get_meta("scheduler_heartbeat"),
                now=now,
            ),
            check_github_api(store.get_meta("github_rate_limit"), now=now),
            check_switchyard(_configured_switchyard_result(config, switchyard_probe)),
        ]
    )
