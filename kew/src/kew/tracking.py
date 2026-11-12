"""Dispatch result tracking and cost accumulation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kew.command_outcome import CommandOutcome

if TYPE_CHECKING:
    from kew.publication_canonical import CanonicalNoChange
    from kew.runner_quota import QuotaRejection, QuotaWarning

MAX_RESOURCE_CAP_OBSERVATIONS = 8
"""Maximum resource-cap observations retained for one container."""


def merge_launch_evidence(first: bool | None, second: bool | None) -> bool | None:
    """Confirmed startup wins; unknown telemetry cannot become a refusal."""
    if first is True or second is True:
        return True
    return None if first is None or second is None else False


@dataclass
class LaunchState:
    """Share cumulative launch evidence with callers when execution raises."""

    launched: bool | None = False

    @property
    def kind(self) -> Literal["executed", "not_run"]:
        """Classify exceptions from confirmed execution evidence."""
        return "executed" if self.launched is not False else "not_run"

    def observe(self, result: DispatchResult) -> None:
        """Retain evidence from a returned attempt before later work can fail."""
        self.launched = merge_launch_evidence(self.launched, result.launched)


def partial_dispatch_result(exc: Exception) -> DispatchResult | None:
    """Read evidence attached before a container exception crossed a boundary."""
    result = getattr(exc, "dispatch_result", None)
    return result if isinstance(result, DispatchResult) else None


@contextmanager
def preserve_dispatch_result(result: DispatchResult | None) -> Iterator[None]:
    """Carry workload evidence through every later lifecycle exception."""
    try:
        yield
    except Exception as exc:
        if result is not None:
            result.exit_code = -1
            exc.dispatch_result = result  # type: ignore[attr-defined]
        raise


#: The category of a contained run the no-output watchdog stopped (#2240): its
#: stream said nothing for ``[agent] no_output_minutes`` while a model call was
#: outstanding. Infrastructure, not a verdict, so a reviewer round is retried.
STREAM_SILENT_CATEGORY = "stream_silent"


#: The categories kew sets only where a container it made for the run never
#: ran its agent: the agent's startup refused (``sandbox_startup_refused``), a
#: create or start the runtime refused (#2973), and a create the runtime never
#: answered on any attempt (``runtime_timeout``, #3103). Each is settled as a
#: startup refusal under its own, more specific name.
CONTAINER_NEVER_STARTED_CATEGORIES = frozenset(
    {"sandbox_startup_refused", "container_start_failed", "runtime_timeout"}
)

#: Every category that proves no agent started, on every lane (#3186): the
#: container ones above, and the review lane's own refusals before any
#: container exists (a sandbox refusal, a lane that failed before the
#: container, a runner kew cannot authenticate, a refused reviewer profile).
#: The review stage's pre-launch hold table is keyed by exactly this set, so a
#: category means "never started" to ``kew review``, ``ci_fix`` and the drive
#: alike.
STARTUP_REFUSED_CATEGORIES = CONTAINER_NEVER_STARTED_CATEGORIES | frozenset(
    {
        "sandbox_refused",
        "launch_failed",
        "credential_missing",
        "prompt_configuration_refused",
    }
)


def startup_refused(result: DispatchResult) -> bool:
    """The final attempt's refusal stands even when earlier attempts ran."""
    return getattr(result, "error_category", "") in STARTUP_REFUSED_CATEGORIES


def command_outcome(result: DispatchResult) -> CommandOutcome:
    """Separate terminal failure from cumulative evidence of execution."""
    reason = result.stop_reason
    return CommandOutcome(
        "executed" if result.launched is not False else "not_run",
        (result.exit_code or 1) if startup_refused(result) else result.exit_code,
        reason if isinstance(reason, str) else "",
    )


@dataclass
class DispatchResult:
    """Structured result from a dispatch run."""

    exit_code: int
    issue_num: int
    log_file: Path
    duration_seconds: float = 0.0
    # Elapsed time observed by the component that enforced the timeout.
    # None means no enforcement owner started. Classification must not infer a
    # timeout from the accounting duration when this evidence is absent.
    enforced_duration_seconds: float | None = None
    # The deadline owned by enforced_duration_seconds. The pair is one window.
    enforced_timeout_seconds: float | None = None
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tool_uses: int = 0
    estimated_cost_usd: float = 0.0
    reported_cost_usd: float | None = None
    # The planned bound (#1092): this run's REALIZED tokens priced at the
    # snapshot/override bound — the conservative max-across-provider-endpoints
    # rate dispatch plans against. Derived by compute_cost from the final
    # accumulated usage, so the pair isolates *pricing-model* calibration, not
    # prompt-size forecasting. None until compute_cost runs, and None whenever
    # there is no usable rate to price against ("no bound recorded", distinct
    # from a $0 bound). Paired with estimated_cost_usd (the authoritative
    # actual) it yields the planning-accuracy ratio.
    planned_cost_usd: float | None = None
    resolved_model: str = ""  # runner-reported resolved model (#251); "" if unreported
    resolved_model_source: str = ""  # runner_reported | switchyard_proxy_metrics
    configured_model: str = (
        ""  # Validated launch identity; fallback for rejected reports.
    )
    pricing_model: str = (
        ""  # Internal accounting identity, separate from retained reports.
    )
    selected_tier: str = ""  # final Switchyard routing tier, when evidenced
    requested_service_tier: str = ""  # governed Codex request, when applicable
    reported_service_tier: str = ""  # structured runner/provider response
    classifier_overhead_model: str = ""
    classifier_overhead_input_tokens: int = 0
    classifier_overhead_output_tokens: int = 0
    classifier_overhead_cost_usd: float = 0.0
    classifier_evidence: list[dict[str, object]] | None = None
    switchyard_proxy_observation: dict[str, object] | None = None
    #: The PR the host published for this run, independent of store persistence.
    published_pr_url: str = ""
    #: A Dispatch Report for this run is on its PR; later steps must not post one.
    dispatch_report_posted: bool = False
    accounting_status: str = ""
    accounting_unreconciled_reasons: list[str] | None = None
    session_id: str | None = None
    session_id_rejection: str = ""
    session_id_warning: str = ""  # Fixed credential-collision marker; never a handle.
    error_category: str = ""
    subtype: str = ""  # e.g. "error_max_budget_usd"
    stop_reason: str = ""  # e.g. "end_turn", "tool_use"
    # The runner's last non-protocol output lines, redacted and bounded
    # (kew.exit_signal); read only to name a signal exit (#2887).
    diagnostic_tail: list[str] = field(default_factory=list, repr=False)
    event_file: Path | None = None  # path to .events.jsonl file when emit_events=True
    run_id: str = ""  # real RunRecord id (FK into runs); set by run_tracked_agent
    paused: bool = False  # set by orchestrator when should_pause() fires
    budget_exceeded: bool = (
        False  # set by the orchestrator when the budget watchdog kills the run
    )
    breaker_rule: str = ""  # set by the watchdog when a breaker rule fires
    # The anomaly threshold the breaker judged the run against, at the trip.
    breaker_threshold_usd: float | None = None
    # The breaker fired on the line that carried the runner's typed terminal
    # event: the run was flagged, not stopped, and its finished work takes the
    # publication path (#1798).
    breaker_after_terminal: bool = False
    # The line being judged carries the runner's typed terminal event
    # (claude-code ``result``, codex ``turn.completed``). It holds until the
    # next usage-bearing event clears it. False is "unknown", never "finished".
    terminal_observed: bool = False
    # How the runtime answered a contained run's cost-stop kill (#1462):
    # accepted | refused | unknown, or "" when kew never asked.
    cost_stop_kill: str = ""
    # The provider's quota refusal behind a ``quota_exhausted`` run (#2643).
    quota_rejection: QuotaRejection | None = None
    # The provider's latest "window filling up" notice on this run (#3329).
    quota_warning: QuotaWarning | None = None
    resource_cap_reason: str = ""  # set when a host cap killed the container (#1431)
    resource_cap_code: str = ""  # stable retry-policy identity for that cap
    resource_cap_observations: list[dict[str, str]] | None = None
    resource_cap_observations_truncated: bool = False
    oom_probe_status: str = ""  # seen | absent | unavailable | "" when not run
    oom_probe_diagnostic: str = ""  # bounded daemon failure detail
    # How a 124/137 exit is classified by the orchestrator once elapsed time and
    # the watchdog reason are known: wall_clock | resource_cap | hard_kill | "".
    # A 137 before the deadline with no host cap is an OOM or an in-container
    # SIGKILL, never a timeout.
    hard_stop: str = ""
    compaction_count: int = 0  # autocompaction events seen; discounts anomaly cost
    attempt_number: int = 0  # total attempts made; stamped by run_tracked_agent
    canonical_no_change: CanonicalNoChange | None = None
    # True confirms workload startup; False confirms refusal; None means UNKNOWN.
    # Missing telemetry must preserve possibly executed work.
    # Retries accumulate this evidence; error_category describes the final attempt.
    launched: bool | None = False
    container_id: str | None = None
    # The OpenShell runtime's realized checkout (epic openshell-runtime, N7):
    # the host directory the agent's work was DOWNLOADED into once its sandbox
    # ended, with the generation bound to that directory. The uploaded checkout
    # holds none of the agent's work on that runtime, so publication follows
    # this path instead — and a realization that refused leaves the path unset
    # and says why here, which refuses publication rather than pushing the
    # launch snapshot as if the agent had produced it. All three stay unset on
    # every other runtime, where the agent writes the host tree directly.
    realized_checkout: Path | None = None
    realized_checkout_generation: str = ""
    realized_checkout_refusal: str = ""

    @property
    def breaker_stopped(self) -> bool:
        """A breaker rule stopped this run, rather than flagging a finished one."""
        return bool(self.breaker_rule) and not self.breaker_after_terminal

    def accumulate_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        tool_uses: int = 0,
    ) -> None:
        """Add usage data from a stream event."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_tokens += cache_creation_tokens
        self.cache_read_tokens += cache_read_tokens
        self.total_tokens = (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )
        self.tool_uses += tool_uses

    def merge_usage(self, other: DispatchResult) -> None:
        """Merge usage data from another attempt into this result."""
        self.launched = merge_launch_evidence(self.launched, other.launched)
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.total_tokens = (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )
        self.tool_uses += other.tool_uses

        if self.reported_cost_usd is not None and other.reported_cost_usd is not None:
            self.reported_cost_usd += other.reported_cost_usd
        elif other.reported_cost_usd is not None:
            self.reported_cost_usd = other.reported_cost_usd

        self._merge_routing_identity(other)
        self._merge_classifier_evidence(other)

    def _merge_routing_identity(self, other: DispatchResult) -> None:
        """Adopt the later attempt's model/tier identity when it evidenced one.

        Identity is last-writer-wins rather than accumulated: a retry that ran
        on a different route describes the run that actually produced the
        result, and an attempt that evidenced nothing must not blank it.
        """
        if other.configured_model:
            self.configured_model = other.configured_model
        if other.pricing_model:
            self.pricing_model = other.pricing_model
        if other.resolved_model:
            self.resolved_model = other.resolved_model
            if other.resolved_model_source:
                self.resolved_model_source = other.resolved_model_source
        if other.selected_tier:
            self.selected_tier = other.selected_tier
        if other.requested_service_tier:
            self.requested_service_tier = other.requested_service_tier
        if other.reported_service_tier:
            self.reported_service_tier = other.reported_service_tier

    def _merge_classifier_evidence(self, other: DispatchResult) -> None:
        """Adopt the later attempt's classifier overhead and proxy evidence.

        Overhead tokens travel with the model that evidenced them, so they are
        replaced as a unit rather than summed across attempts.
        """
        if other.classifier_overhead_model:
            self.classifier_overhead_model = other.classifier_overhead_model
            self.classifier_overhead_input_tokens = (
                other.classifier_overhead_input_tokens
            )
            self.classifier_overhead_output_tokens = (
                other.classifier_overhead_output_tokens
            )
        if other.classifier_evidence is not None:
            self.classifier_evidence = other.classifier_evidence
        if other.switchyard_proxy_observation is not None:
            self.switchyard_proxy_observation = other.switchyard_proxy_observation
        if other.accounting_status:
            self.accounting_status = other.accounting_status
            self.accounting_unreconciled_reasons = (
                list(other.accounting_unreconciled_reasons)
                if other.accounting_unreconciled_reasons is not None
                else None
            )

    def compute_cost(
        self,
        model: str | None,
        overrides: dict[str, tuple[float, float]] | None = None,
        *,
        classifier_model: str | None = None,
        classifier_input_tokens: int = 0,
        classifier_output_tokens: int = 0,
    ) -> None:
        """Compute and store the run's planned/actual cost pair (#1092).

        ``planned_cost_usd`` is the token-bound figure — this run's realized
        usage priced at the snapshot/override bound. ``estimated_cost_usd`` is
        the authoritative spend: the runner-reported native cost when there is
        one (#1082), else the same token bound. Recording both keeps the bound
        the native figure replaced, so the planning-accuracy ratio stays
        computable per model instead of being silently discarded.

        A model with no usable rate (or none at all — the runner ran on its own
        default) records NO bound, on this path exactly as on the sandbox path:
        a $0 planned bound would read as an infinite over-run and would count
        the run into the planned-backed total it cannot back.
        """
        from kew.pricing import is_priced_model

        priced = model is not None and is_priced_model(model, overrides)
        self.classifier_overhead_input_tokens = max(0, classifier_input_tokens)
        self.classifier_overhead_output_tokens = max(0, classifier_output_tokens)
        self.classifier_overhead_model = classifier_model or ""
        self.classifier_overhead_cost_usd = self._classifier_overhead_cost(
            classifier_model,
            classifier_input_tokens,
            classifier_output_tokens,
            overrides,
        )
        overhead = self.classifier_overhead_cost_usd
        if self.reported_cost_usd is None:
            # No native figure: the token bound IS the recorded spend, and an
            # unpriced model still warns and meters $0, exactly as before.
            task_cost = self._token_bound_cost(model, overrides)
            self.estimated_cost_usd = task_cost + overhead
            self.planned_cost_usd = task_cost + overhead if priced or overhead else None
            return
        self.estimated_cost_usd = self.reported_cost_usd + overhead
        self.planned_cost_usd = (
            self._token_bound_cost(model, overrides) + overhead
            if priced or overhead
            else None
        )

    @staticmethod
    def _classifier_overhead_cost(
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        overrides: dict[str, tuple[float, float]] | None,
    ) -> float:
        """Price only separately evidenced classifier traffic.

        The classifier model is never inferred from the task model.  When the
        proxy cannot identify it unambiguously this returns zero while the
        overhead token fields remain visible for review.
        """
        if not model or not (input_tokens or output_tokens):
            return 0.0
        from kew.pricing import estimate_cost

        return estimate_cost(
            model=model,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            overrides=overrides,
        )

    def _token_bound_cost(
        self,
        model: str | None,
        overrides: dict[str, tuple[float, float]] | None,
    ) -> float:
        """Accumulated tokens priced at the model's bound; 0.0 when the runner
        used its own default model (nothing to price against)."""
        if model is None:
            return 0.0

        from kew.pricing import estimate_cost

        return estimate_cost(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens,
            overrides=overrides,
        )
