"""The OpenShell setup checks ``kew doctor`` reports, as pure verdicts.

One module so the question "is this host set up to dispatch on
``[sandbox] runtime = "openshell"``?" has a single answer that is not spelled
out inside a Typer command. :mod:`kew.cli.doctor` reads the gateway once and
prints what these functions return; nothing here prints, exits or writes.

Every check is fail-closed in the same direction as admission: a fact the
gateway did not report, a ledger that cannot be read and a setting kew cannot
compare all come back as a failure with the command that fixes it. "kew could
not check" is never a pass — see :func:`kew.sandbox.require_certified_boundary`,
whose refusal this module's certification check surfaces one tick earlier.

What these checks do NOT re-derive is the policy bytes a boundary enforces. kew
hands each sandbox its policy file at ``sandbox create`` time rather than
registering it on the gateway, so the gateway holds no runner-keyed policy for
a packaged digest to be compared against. The certification check below is what
covers those bytes: the certification is bound to the packaged policy's digest
and admission re-reads that file through the validated image context (#2321
review).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from kew.config import SandboxConfig
    from kew.openshell import GatewayReport, OpenShellClient

#: The Landlock ABI the OpenShell supervisor needs to enforce a per-binary
#: policy inside the container VM. ABI 1 and 2 have no network rules at all, so
#: a supervisor on either one applies the filesystem half of a policy and
#: silently allows every endpoint — which reads like containment and behaves
#: like open egress. Refused rather than warned about for that reason.
MINIMUM_LANDLOCK_ABI: Final = 3

#: The one approval mode kew admits: a person approves each change the gateway
#: makes to a policy. Any automatic mode lets the thing being contained widen
#: its own boundary between a certification and the dispatch that trusts it.
MANUAL_APPROVAL: Final = "manual"

#: The one endpoint scheme that encrypts the control channel. Checked on the
#: endpoint kew resolved and not only on the gateway's own flag: a gateway
#: listening on TLS that kew reaches over ``http://`` puts the same traffic —
#: sandbox names, provider names, policy paths — on the wire in the clear.
TLS_SCHEME: Final = "https://"

#: ``<name>@sha256:<64 hex>`` — a reference that names exactly one set of
#: bytes. A tag is a mutable pointer, so a tagged supervisor can be replaced
#: under a certification that still matches on its name.
DIGEST_PIN_RE: Final = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")

CHECK_VERSION: Final = "openshell-version"
CHECK_TLS: Final = "openshell-tls"
CHECK_TELEMETRY: Final = "openshell-telemetry"
CHECK_IMAGE_PINS: Final = "openshell-image-pins"
CHECK_PROVIDERS: Final = "openshell-providers"
CHECK_AGENT_AUTHORITY: Final = "openshell-agent-authority"
CHECK_LANDLOCK: Final = "openshell-landlock"
CHECK_CERTIFICATION: Final = "openshell-certification"

#: Said whenever a check fails for want of a mapped runner. Printed instead of
#: a pass, because an unmapped runtime proves nothing about any boundary.
NO_RUNNER_MAPPED: Final = (
    "no runner is mapped to a gateway provider, so nothing here is proven — "
    "set [sandbox.openshell] providers"
)


@dataclass(frozen=True)
class OpenShellCheck:
    """One check's verdict and the sentence ``kew doctor`` prints for it."""

    name: str
    passed: bool
    detail: str


def mapped_runners(config: SandboxConfig) -> tuple[str, ...]:
    """The runners ``[sandbox.openshell] providers`` maps, in a stable order.

    These, not just the default route's runner, are what every per-runner check
    below covers: a second runner whose policy drifted is a boundary a dispatch
    can still select, so reporting only the default route would call this host
    ready for a runner it would then refuse.
    """
    return tuple(sorted(config.openshell.providers))


def check_versions(*, pinned: str, cli: str, gateway: str) -> OpenShellCheck:
    """Both halves of the control plane are the version kew drives.

    The CLI is the argv surface kew assembles and the gateway is what enforces
    the policy, so a matching CLI against a mismatched gateway is the dangerous
    half: the calls all succeed and the enforcement is somebody else's.
    """
    wrong = [
        f"{label} is {found}"
        for label, found in (("CLI", cli), ("gateway", gateway))
        if found != pinned
    ]
    if wrong:
        return OpenShellCheck(
            CHECK_VERSION,
            False,
            f"{' and '.join(wrong)}, not the pinned {pinned} — install the "
            f"matching release, or set [sandbox.openshell] version once {pinned} "
            "is verified against the installed one",
        )
    return OpenShellCheck(CHECK_VERSION, True, f"CLI and gateway are both {pinned}")


def check_tls(report: GatewayReport) -> OpenShellCheck:
    """The control channel is encrypted at both ends.

    Two halves, because either one alone is a channel that reads closed and
    runs open: the gateway has to be listening on TLS, and the endpoint kew
    resolved — from ``OPENSHELL_GATEWAY`` or ``[sandbox.openshell] gateway`` —
    has to be the ``https://`` one. An endpoint neither input supplied leaves
    the scheme to the CLI's own default, which this host cannot read, so it is
    reported as unproven rather than as a pass.
    """
    faults = []
    if not report.tls:
        faults.append("the gateway reports TLS off")
    endpoint = report.gateway.endpoint
    if not endpoint:
        faults.append(
            "no endpoint is configured, so the CLI's own default decided the "
            f"scheme and kew cannot prove it is {TLS_SCHEME}"
        )
    elif not endpoint.lower().startswith(TLS_SCHEME):
        faults.append(f"kew dials {endpoint!r}, not {TLS_SCHEME}")
    if faults:
        return OpenShellCheck(
            CHECK_TLS,
            False,
            f"{' and '.join(faults)} — every sandbox name, provider name and "
            "policy path on this channel travels in the clear. Set tls.enabled "
            "in the gateway config and point [sandbox.openshell] gateway at the "
            f"{TLS_SCHEME} endpoint",
        )
    return OpenShellCheck(
        CHECK_TLS, True, f"the gateway listens on TLS and kew dials {endpoint}"
    )


def check_telemetry(report: GatewayReport) -> OpenShellCheck:
    """The gateway ships nothing about this host's dispatches off it."""
    if report.telemetry:
        return OpenShellCheck(
            CHECK_TELEMETRY,
            False,
            "the gateway has telemetry ON — sandbox names, images, policies and "
            "command lines leave this host. Set telemetry off in the gateway "
            "config and restart the gateway process itself",
        )
    return OpenShellCheck(CHECK_TELEMETRY, True, "the gateway sends no telemetry")


def check_image_pins(report: GatewayReport) -> OpenShellCheck:
    """The supervisor and sandbox images are named by digest, not by tag."""
    unpinned = [
        f"{label} {reference!r}"
        for label, reference in (
            ("supervisor", report.supervisor_image),
            ("sandbox", report.sandbox_image),
        )
        if DIGEST_PIN_RE.match(reference) is None
    ]
    if unpinned:
        return OpenShellCheck(
            CHECK_IMAGE_PINS,
            False,
            f"{' and '.join(unpinned)} is not pinned by digest, so the bytes "
            "that enforce the boundary can change under a certification that "
            "still matches the name. Pin <image>@sha256:<digest> in the gateway "
            "config",
        )
    return OpenShellCheck(
        CHECK_IMAGE_PINS,
        True,
        "the supervisor and sandbox images are pinned by digest",
    )


def check_agent_authority(report: GatewayReport) -> OpenShellCheck:
    """The contained agent cannot widen its own boundary."""
    faults = []
    if report.agent_proposals:
        faults.append("the gateway accepts policy proposals from the agent")
    if report.agent_approval != MANUAL_APPROVAL:
        faults.append(f"approval is {report.agent_approval!r}, not {MANUAL_APPROVAL!r}")
    if faults:
        return OpenShellCheck(
            CHECK_AGENT_AUTHORITY,
            False,
            f"{' and '.join(faults)} — the thing being contained can move its "
            "own boundary. Turn agent proposals off and set approval to "
            f"{MANUAL_APPROVAL!r} in the gateway config",
        )
    return OpenShellCheck(
        CHECK_AGENT_AUTHORITY,
        True,
        f"agent proposals are off and approval is {MANUAL_APPROVAL}",
    )


def check_landlock(report: GatewayReport) -> OpenShellCheck:
    """The container VM kernel can enforce the network half of a policy."""
    if report.landlock_abi < MINIMUM_LANDLOCK_ABI:
        return OpenShellCheck(
            CHECK_LANDLOCK,
            False,
            f"the container VM kernel reports Landlock ABI {report.landlock_abi}; "
            f"ABI {MINIMUM_LANDLOCK_ABI} is the first with network rules, so a "
            "policy would apply its filesystem half and allow every endpoint. "
            "Update the container VM's kernel",
        )
    return OpenShellCheck(
        CHECK_LANDLOCK,
        True,
        f"the container VM kernel reports Landlock ABI {report.landlock_abi}",
    )


def check_providers(
    client: OpenShellClient, *, providers: Mapping[str, str]
) -> OpenShellCheck:
    """Every mapped credential provider exists on the gateway.

    On this runtime no credential value reaches the agent's environment, so a
    provider the gateway does not hold is not a degraded run — it is a dispatch
    that authenticates as nobody and fails at its first model call.

    Only the gateway's own "not there" answer is reported as absent. Any other
    failure — a timeout, an unreachable gateway — is raised for the caller to
    report as an unproven gateway, because a read that did not complete says
    nothing about whether the provider exists.
    """
    if not providers:
        return OpenShellCheck(CHECK_PROVIDERS, False, NO_RUNNER_MAPPED)
    from kew.openshell import OpenShellNotFound

    missing = []
    for runner, provider in sorted(providers.items()):
        try:
            client.provider_get(provider)
        except OpenShellNotFound:
            missing.append(f"{runner} -> {provider}")
    if missing:
        return OpenShellCheck(
            CHECK_PROVIDERS,
            False,
            "the gateway does not hold a provider for "
            + "; ".join(missing)
            + " — create it on the gateway, or correct "
            "[sandbox.openshell] providers",
        )
    return OpenShellCheck(
        CHECK_PROVIDERS,
        True,
        f"the gateway holds every mapped provider ({len(providers)})",
    )


def check_certification(
    config: SandboxConfig, *, runners: tuple[str, ...], store: object | None
) -> OpenShellCheck:
    """A fresh passing egress certification matches every mapped boundary.

    The same gate admission applies, read one tick earlier and over every
    mapped runner rather than only the route a dispatch happens to select. It
    is read-only: the check reports what is on the ledger and never probes, so
    bare ``kew doctor`` still spends nothing.
    """
    if not runners:
        return OpenShellCheck(CHECK_CERTIFICATION, False, NO_RUNNER_MAPPED)
    from kew.sandbox import SandboxRuntimeUnavailable, require_certified_boundary

    uncertified = []
    for runner in runners:
        try:
            require_certified_boundary(config, runner=runner, store=store)
        except SandboxRuntimeUnavailable as exc:
            uncertified.append(f"{runner} ({exc.reason})")
    if uncertified:
        return OpenShellCheck(
            CHECK_CERTIFICATION,
            False,
            "no fresh passing egress certification matches "
            + "; ".join(uncertified)
            + " — run `kew sandbox certify --runtime openshell --runner <runner>` "
            "for each",
        )
    return OpenShellCheck(
        CHECK_CERTIFICATION,
        True,
        f"a fresh certification matches the boundary for {', '.join(runners)}",
    )
