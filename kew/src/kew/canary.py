"""kew canary — attest kew still reads fresh vendor CLI output correctly (#265).

Pure core: reduce a run's StreamEvents + ResultClassification to GovernanceFields,
then check them. Reused by the golden-replay tests and the live `kew canary` CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

from kew.health import CheckResult
from kew.pricing import compute_cost_from_tokens, is_free_model
from kew.runners.protocol import ResultClassification, StreamEvent
from kew.usage import accumulate_usage


@dataclass(frozen=True)
class GovernanceFields:
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    session_id: str | None
    native_cost_usd: float | None
    classification: str
    model: str | None
    saw_result_event: bool
    tool_uses: int = 0


def _native_cost(events: list[StreamEvent]) -> float | None:
    """The native cost a runner reports on its terminal 'result' event (opencode)."""
    for ev in events:
        if ev.event_type == "result":
            cost = ev.metadata.get("cost_usd")
            if isinstance(cost, (int, float)):
                return float(cost)
    return None


def _stream_model(events: list[StreamEvent]) -> str | None:
    """The model the stream's 'init' event reports (claude); None when absent."""
    for ev in events:
        if ev.event_type == "init":
            model = ev.metadata.get("model")
            if model:
                return str(model)
    return None


def reduce_run(
    events: list[StreamEvent], classification: ResultClassification
) -> GovernanceFields:
    totals = accumulate_usage(events)
    category = classification.category
    return GovernanceFields(
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_creation_tokens=totals.cache_creation_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        session_id=classification.session_id,
        native_cost_usd=_native_cost(events),
        # The runners return category="" on a clean exit — there is no "success"
        # literal anywhere (claude_code.py / opencode.py classify_result).
        classification="success" if category == "" else category,
        model=_stream_model(events),
        saw_result_event=any(ev.event_type == "result" for ev in events),
        tool_uses=sum(
            max(1, ev.tool_uses) for ev in events if ev.event_type == "tool_use"
        ),
    )


@dataclass(frozen=True)
class Contract:
    needs_session: bool
    needs_native_cost: bool


CONTRACTS: dict[str, Contract] = {
    "claude-code": Contract(needs_session=True, needs_native_cost=False),
    "codex": Contract(needs_session=True, needs_native_cost=False),
    "opencode": Contract(needs_session=True, needs_native_cost=True),
}


def _check_tokens(f: GovernanceFields) -> CheckResult:
    detail = {"input_tokens": f.input_tokens, "output_tokens": f.output_tokens}
    if f.input_tokens > 0 and f.output_tokens > 0:
        return CheckResult(
            "tokens_present",
            "pass",
            f"in={f.input_tokens} out={f.output_tokens}",
            detail,
        )
    return CheckResult(
        "tokens_present",
        "fail",
        f"token capture broke: in={f.input_tokens} out={f.output_tokens}",
        detail,
    )


def _check_session(f: GovernanceFields, contract: Contract) -> CheckResult:
    detail = {"present": bool(f.session_id)}
    if not contract.needs_session:
        return CheckResult("session_captured", "skip", "runner has no session", detail)
    if f.session_id:
        return CheckResult("session_captured", "pass", "session id present", detail)
    return CheckResult("session_captured", "fail", "session id missing", detail)


def _check_cost(
    f: GovernanceFields,
    contract: Contract,
    effective_model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> CheckResult:
    if is_free_model(effective_model):
        return CheckResult(
            "cost_captured",
            "skip",
            f"{effective_model} is free",
            {"model": effective_model},
        )
    if contract.needs_native_cost:
        native = f.native_cost_usd or 0.0
        detail = {"native_cost_usd": f.native_cost_usd, "model": effective_model}
        if native > 0:
            return CheckResult(
                "cost_captured", "pass", f"native cost ${native:.4f}", detail
            )
        return CheckResult("cost_captured", "fail", "native cost missing/zero", detail)
    derived = compute_cost_from_tokens(
        model=effective_model,
        input_tokens=f.input_tokens,
        output_tokens=f.output_tokens,
        cache_creation_tokens=f.cache_creation_tokens,
        cache_read_tokens=f.cache_read_tokens,
        overrides=overrides,
    )
    detail = {"derived_cost_usd": derived, "model": effective_model}
    if derived > 0:
        return CheckResult(
            "cost_captured", "pass", f"derived cost ${derived:.4f}", detail
        )
    return CheckResult(
        "cost_captured", "fail", f"derived cost zero for {effective_model!r}", detail
    )


def _check_result(f: GovernanceFields) -> CheckResult:
    detail = {
        "classification": f.classification,
        "saw_result_event": f.saw_result_event,
    }
    if f.classification == "transient":
        return CheckResult(
            "result_classified", "skip", "transient (vendor blip)", detail
        )
    if f.classification == "success" and f.saw_result_event:
        return CheckResult("result_classified", "pass", "classified success", detail)
    return CheckResult(
        "result_classified",
        "fail",
        f"classification={f.classification} saw_result={f.saw_result_event}",
        detail,
    )


def canary_checks(
    fields: GovernanceFields,
    contract: Contract,
    *,
    model: str,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> list[CheckResult]:
    """Run all present-&-sane checks for one runner's reduced fields.

    `effective_model` is the stream-reported model when present (claude's init
    event), else the resolved model the caller passed — so claude prices via its
    real model even when the config model is empty.
    """
    effective_model = fields.model or model
    return [
        _check_tokens(fields),
        _check_session(fields, contract),
        _check_cost(fields, contract, effective_model, overrides),
        _check_result(fields),
    ]


@dataclass(frozen=True)
class PassthroughCanaryEvidence:
    """Sanitized lifecycle evidence from one live passthrough exercise."""

    proxy_healthy: bool
    cancellation_observed: bool
    resume_completed: bool
    cleanup_complete: bool

    @classmethod
    def all_pass(cls) -> PassthroughCanaryEvidence:
        """Build positive evidence for focused pure-core tests."""
        return cls(
            proxy_healthy=True,
            cancellation_observed=True,
            resume_completed=True,
            cleanup_complete=True,
        )


def _check_tools(fields: GovernanceFields) -> CheckResult:
    detail = {"tool_uses": fields.tool_uses}
    if fields.tool_uses > 0:
        return CheckResult("tools_exercised", "pass", "tool use observed", detail)
    return CheckResult("tools_exercised", "fail", "tool use missing", detail)


def _check_evidence(name: str, observed: bool, passed: str, failed: str) -> CheckResult:
    return CheckResult(
        name,
        "pass" if observed else "fail",
        passed if observed else failed,
        {"observed": observed},
    )


def passthrough_evidence_checks(
    fields: GovernanceFields, evidence: PassthroughCanaryEvidence
) -> list[CheckResult]:
    """Check only evidence unique to the Switchyard lifecycle."""
    return [
        _check_tools(fields),
        _check_evidence(
            "proxy_healthy",
            evidence.proxy_healthy,
            "proxy healthy before and after run",
            "proxy health check failed",
        ),
        _check_evidence(
            "cancellation_observed",
            evidence.cancellation_observed,
            "interrupted process reaped",
            "interrupted process was not reaped",
        ),
        _check_evidence(
            "resume_completed",
            evidence.resume_completed,
            "resume completed",
            "resume failed",
        ),
        _check_evidence(
            "cleanup_complete",
            evidence.cleanup_complete,
            "temporary state removed",
            "temporary state remains",
        ),
    ]


def passthrough_canary_checks(
    fields: GovernanceFields,
    contract: Contract,
    *,
    evidence: PassthroughCanaryEvidence,
) -> list[CheckResult]:
    """Attest the runner and proxy surfaces required by passthrough UAT.

    Proxy routes are not billing identities, so this contract deliberately omits
    ``cost_captured``. Runner-reported tokens remain authoritative; proxy metrics
    are process-wide observations only.
    """
    return [
        _check_tokens(fields),
        _check_session(fields, contract),
        _check_result(fields),
        *passthrough_evidence_checks(fields, evidence),
    ]


@dataclass(frozen=True)
class RunnerOutcome:
    runner: str
    ran: bool  # False == couldn't-run (skip, contributes exit 2)
    checks: list[CheckResult]
    reason: str = ""  # couldn't-run reason (auditable)


@dataclass(frozen=True)
class CanaryReport:
    outcomes: list[RunnerOutcome]

    @property
    def has_drift(self) -> bool:
        return any(c.status == "fail" for o in self.outcomes if o.ran for c in o.checks)

    @property
    def has_incomplete(self) -> bool:
        return any(not o.ran for o in self.outcomes)

    @property
    def exit_code(self) -> int:
        # drift (1) outranks incompleteness (2) outranks green (0)
        if self.has_drift:
            return 1
        if self.has_incomplete:
            return 2
        return 0
