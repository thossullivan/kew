"""AgentRunner Protocol and supporting data types."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.inference_transport import RunnerTransportCapability
    from kew.tracking import DispatchResult


LoopStyle = Literal["native"]
"""Whether a runner drives its own agentic loop.

All supported runners (Claude Code, OpenCode, pi) loop internally — kew
delegates and observes.
"""

PublicationStyle = Literal["agent", "host"]
"""Who performs commit, push, PR creation, and report publication."""

ContainmentPosture = Literal["native"]
"""Runner-owned containment accepted in place of an external container."""

COMMAND_GUARD_RUNNER = "claude-code"
"""The only runner that can load the project-local Claude command guard."""


def loop_style_to_drive_mode(style: LoopStyle) -> Literal["delegated"]:
    """The single source of truth for the UI's drive-mode word.

    Native runners are *delegated to*. Kept here (next to ``LoopStyle``) so
    the service/UI layer never re-derives the vocab.
    """
    return "delegated"


@dataclass
class ResultClassification:
    """Structured result from a runner's error classification."""

    category: str  # "transient", "permanent", "budget_exceeded", "watchdog_kill"
    session_id: str | None = None
    subtype: str = ""
    stop_reason: str = ""


#: ``StreamEvent.metadata`` key under which a terminal ``result`` event carries
#: a :class:`RunTotals`. A runner with no end-of-run summary omits the key.
RUN_TOTALS_KEY = "run_totals"


@dataclass
class RunTotals:
    """A runner's authoritative end-of-run totals, carried on a ``result`` event.

    Some runners close their stream with a summary that restates the WHOLE run's
    spend (claude-code's ``modelUsage`` block); others report only per-turn
    figures and never restate a total (opencode, pi, lemonade, hermes). A
    consumer that accumulates ``usage``/``step`` events — the sandbox telemetry
    path (#1147) — OVERWRITES its running totals when the terminal event carries
    a ``RunTotals``, and keeps the accumulated per-turn figures when it does not.
    Overwriting with an absent summary is exactly the zeroing #761 was about, so
    "no summary" must stay expressible: runners that have none omit
    :data:`RUN_TOTALS_KEY` entirely rather than emitting an empty ``RunTotals``.

    ``cost_usd`` is ``None`` — not ``0.0`` — when the summary reports no dollar
    figure, keeping "no native total" distinguishable from "genuinely free" (the
    same convention as the per-step ``cost_usd`` metadata, #1082).
    """

    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    model: str | None = None
    model_display: str = ""  # Redacted diagnostic text; never a pricing identity.


@dataclass
class StreamEvent:
    """What the orchestrator cares about from a parsed output line."""

    raw_line: str
    event_type: str = (
        ""  # "activity", "tool_use", "usage", "step", "init", "result", or ""
    )
    message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tool_uses: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


class TransportCapableRunner(Protocol):
    """Public capability declaration for optional inference transports."""

    @property
    def name(self) -> str: ...

    @property
    def transport_capability(self) -> RunnerTransportCapability:
        """Proven support for isolated inference transport overrides."""
        ...


class AgentRunner(Protocol):
    """Protocol for AI coding agent backends."""

    @property
    def name(self) -> str: ...

    @property
    def loop_style(self) -> LoopStyle: ...

    @property
    def publication_style(self) -> PublicationStyle: ...

    def containment_posture(
        self, *, runtime: str | None = None
    ) -> ContainmentPosture | None:
        """A containment boundary enforced by the runner itself, if any.

        SECURITY SURFACE (#1322). Only runner code may make this declaration;
        configuration cannot grant a runner native containment. ``None`` means
        the external container admission rules apply unchanged.

        ``runtime`` is the container boundary kew would otherwise put this
        launch inside — the ``[sandbox] runtime`` of the dispatch being
        resolved — and ``None`` means kew is offering no boundary at all (a
        first-class loop's agent, a remediation flow taking its documented host
        fallback). The two are different questions and a runner may answer them
        differently: a runner whose own sandbox is the only boundary available
        on one runtime can still be the weaker of the two on another, and
        answering ``"native"`` there would keep kew from using the boundary it
        has (epic ``openshell-runtime``, node N10). Keyword-only and defaulted
        so the question with no runtime in it — "do you contain yourself at
        all?" — stays the short spelling it reads as.
        """
        ...

    @property
    def uncontainable_reason(self) -> str | None:
        """Why this runner can NEVER run inside kew's container, if it cannot.

        SECURITY SURFACE (W1.1 / D2). ``None`` — the normal case — means the
        container admission rules apply unchanged. A string means containment
        is impossible by construction rather than merely unbuilt, and the
        string says why, in a sentence an operator can act on.

        The distinction matters because the two refuse differently. An unmapped
        image or an undeclared credential is a gap on *kew's* side: it fails
        closed and no operator opt-in can wave it through, because the fix is
        for kew to build the missing piece. A runner that cannot be contained
        at all has no such fix pending, so refusing it forever would just make
        it undispatchable. Instead it requires ``dangerously_skip_containment``
        explicitly, runs uncontained, and says so — the operator accepts a real
        risk knowingly rather than being told to wait for something that is
        never coming.

        Declaring this is a claim that the runner's own design defeats the
        boundary, not that containing it is inconvenient.
        """
        ...

    @property
    def required_credentials(self) -> tuple[str, ...]:
        """Host env var names this runner can authenticate with INSIDE a container.

        SECURITY SURFACE (#1146). This declaration is the container env
        allowlist: ``kew.sandbox.build_run_argv`` forwards exactly these names
        (by name only, never a value, never a mounted credential file) for the
        resolved runner and nothing else, so a contained run only ever sees its
        OWN provider's credentials. Widening a runner's tuple widens what a
        prompt-injected agent in that container can exfiltrate — add a name
        only when that runner genuinely cannot authenticate without it.

        Semantics: any ONE of the names being set (non-empty) is enough to
        authenticate. An EMPTY tuple means "declares no in-container
        credential", which makes the runner un-containable in an external
        container — ``resolve_effective_sandbox`` refuses rather than launching
        a container that would authenticate as nothing and burn the dispatch at
        $0 (#721). A separately declared native containment posture does not
        use this container credential path.
        """
        ...

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]: ...

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """The agent flags a sandboxed run needs: ``build_command()`` minus the
        binary and minus the prompt handoff.

        This fills the ``"$@"`` slot of the container entrypoint contract
        (``docs/sandbox-entrypoint-contract.md``, element 3). The IMAGE owns the
        binary and the prompt handoff — the prompt travels as a mounted file fed
        to stdin and must NEVER appear in argv (#695) — so a runner returns
        neither here. Host-only argv (a session id for resume, a host-path
        sidecar) is dropped too: the container has no host to resume into.

        ``model`` is the effective, ALREADY-ROUTED model the caller resolved;
        empty means "no explicit model" — the image's own default wins, and a
        runner must not substitute a config fallback of its own.
        """
        ...

    def parse_line(self, line: str) -> list[StreamEvent]: ...

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification: ...

    def supports_resume(self) -> bool: ...

    def build_resume_command(
        self,
        *,
        session_id: str,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]: ...

    def enrich_result(
        self,
        result: DispatchResult,
        config: DispatchConfig,
    ) -> None: ...


@dataclass(frozen=True)
class RunnerSecurityCapabilities:
    """Security boundaries declared by one runner adapter."""

    runner: str
    external_container: bool
    native_containment: bool
    command_guard: bool
    credential_names: tuple[str, ...]
    network_support: bool


def security_capabilities(runner: AgentRunner) -> RunnerSecurityCapabilities:
    """Return the runner's declared security capabilities.

    Asked with no runtime, so ``native_containment`` is the runner's own
    boundary considered on its own — the claim the generated docs table makes.
    A runner whose posture depends on the configured ``[sandbox] runtime``
    (see :meth:`AgentRunner.containment_posture`) is documented per runtime on
    its own reference page; one boolean cannot carry both answers.
    """
    native = runner.containment_posture() is not None
    external = not native and runner.uncontainable_reason is None
    return RunnerSecurityCapabilities(
        runner=runner.name,
        external_container=external,
        native_containment=native,
        command_guard=runner.name == COMMAND_GUARD_RUNNER,
        credential_names=tuple(runner.required_credentials),
        network_support=external or native,
    )


def _capability_cell(value: bool) -> str:
    return "yes" if value else "no"


def render_security_capability_table(runners: Iterable[AgentRunner]) -> str:
    """Render the generated security table used by the runner index."""
    lines = [
        "| Runner | External container | Native containment | Command guard | "
        "Credential names | Network support |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for runner in runners:
        capability = security_capabilities(runner)
        credentials = ", ".join(f"`{name}`" for name in capability.credential_names)
        lines.append(
            f"| `{capability.runner}` | "
            f"{_capability_cell(capability.external_container)} | "
            f"{_capability_cell(capability.native_containment)} | "
            f"{_capability_cell(capability.command_guard)} | "
            f"{credentials or 'none'} | "
            f"{_capability_cell(capability.network_support)} |"
        )
    return "\n".join(lines)
