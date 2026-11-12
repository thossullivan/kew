"""Immutable declarations of facts owned by runner implementations.

This module deliberately stops at the runner boundary.  Tracker adapters,
workflow policy, verification, publication and metering are composed later by
``kew.readiness`` from their respective owners.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal


class CostSignal(StrEnum):
    """The strongest cost signal emitted by a runner."""

    NATIVE_TOTAL = "native_total"
    NATIVE_INCREMENTAL = "native_incremental"
    ESTIMATED_TOTAL = "estimated_total"
    UNPRICED = "unpriced"
    UNAVAILABLE = "unavailable"


class ContainmentRequirement(StrEnum):
    """How a runner can satisfy Kew's containment boundary."""

    NATIVE = "native"
    EXTERNAL = "external"
    UNAVAILABLE = "unavailable"


ResumeSemantics = Literal["session", "none"]


@dataclass(frozen=True)
class RunnerCapabilities:
    """A central runner-native capability manifest.

    Values here are claims about the runner adapter's implemented protocol,
    not claims that a particular configured route is currently usable.
    """

    runner: str
    resume: ResumeSemantics
    telemetry: tuple[str, ...]
    cost: CostSignal
    loop: Literal["native"]
    containment: ContainmentRequirement

    @property
    def resume_semantics(self) -> ResumeSemantics:
        return self.resume

    @property
    def native_telemetry(self) -> tuple[str, ...]:
        return self.telemetry

    @property
    def cost_signal(self) -> CostSignal:
        return self.cost

    @property
    def loop_behavior(self) -> Literal["native"]:
        return self.loop

    @property
    def containment_requirement(self) -> ContainmentRequirement:
        return self.containment


_MANIFESTS: Final[Mapping[str, RunnerCapabilities]] = MappingProxyType(
    {
        "claude-code": RunnerCapabilities(
            runner="claude-code",
            resume="session",
            telemetry=("activity", "tool_use", "tokens", "terminal"),
            cost=CostSignal.NATIVE_TOTAL,
            loop="native",
            containment=ContainmentRequirement.EXTERNAL,
        ),
        "codex": RunnerCapabilities(
            runner="codex",
            resume="session",
            telemetry=("activity", "tool_use", "tokens", "terminal"),
            cost=CostSignal.UNAVAILABLE,
            loop="native",
            containment=ContainmentRequirement.NATIVE,
        ),
        "opencode": RunnerCapabilities(
            runner="opencode",
            resume="session",
            telemetry=("activity", "tool_use", "tokens", "terminal"),
            cost=CostSignal.NATIVE_INCREMENTAL,
            loop="native",
            containment=ContainmentRequirement.EXTERNAL,
        ),
        "pi": RunnerCapabilities(
            runner="pi",
            resume="session",
            telemetry=("activity", "tool_use", "tokens", "terminal"),
            cost=CostSignal.NATIVE_INCREMENTAL,
            loop="native",
            containment=ContainmentRequirement.EXTERNAL,
        ),
        "hermes": RunnerCapabilities(
            runner="hermes",
            resume="session",
            telemetry=("activity", "tokens", "terminal"),
            cost=CostSignal.ESTIMATED_TOTAL,
            loop="native",
            containment=ContainmentRequirement.EXTERNAL,
        ),
        "lemonade": RunnerCapabilities(
            runner="lemonade",
            resume="session",
            telemetry=("activity", "tool_use", "tokens", "terminal"),
            cost=CostSignal.UNPRICED,
            loop="native",
            containment=ContainmentRequirement.UNAVAILABLE,
        ),
    }
)

RUNNER_CAPABILITIES = _MANIFESTS


def capability_manifest(runner: str) -> RunnerCapabilities:
    """Return ``runner``'s immutable native declaration, failing closed."""
    try:
        return _MANIFESTS[runner]
    except KeyError as exc:
        available = ", ".join(sorted(_MANIFESTS))
        raise ValueError(f"Unknown runner {runner!r}. Available: {available}") from exc
