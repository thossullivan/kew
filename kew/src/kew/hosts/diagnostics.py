"""What ``kew doctor`` reports about the configured hosts.

Pure probes: each one returns a :class:`HostCheck` and prints nothing, so the
CLI owns the formatting and the tests own the facts. Every failure carries the
command that fixes it — an expired ``gcloud auth login`` is by far the common
one, and "instance unreachable" with no fix is the kind of diagnostic that
sends an operator reading cloud consoles.

A probe that **cannot run** because an earlier one failed is reported as
``skipped``, never as a pass: the upstream failure is the issue, and claiming a
clean downstream state kew never observed is the silent-clean-default bug.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass

from kew import subprocess_utils
from kew.config import DispatchConfig, GcpHostConfig

OK = "ok"
FAIL = "failed"
SKIP = "skipped"

GCLOUD_INSTALL_HINT = (
    "install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install"
)
GCLOUD_LOGIN_FIX = shlex.join(["gcloud", "auth", "login"])

# The shape of a GCP project id (optionally domain-scoped). A fix is a command
# an operator or agent may run as-is, so a value that is not a project id never
# reaches one.
_GCP_PROJECT_RE = re.compile(r"(?:[a-z][a-z0-9.-]*:)?[a-z](?:[a-z0-9-]*[a-z0-9])?")

_PROBE_TIMEOUT = 60.0


@dataclass(frozen=True)
class HostCheck:
    """One host diagnostic: what was probed, how it went, how to fix it.

    ``fix`` is one runnable command or empty; anything that is not a command,
    such as where to download the CLI, is ``hint``.
    """

    name: str
    status: str
    detail: str
    fix: str = ""
    hint: str = ""

    @property
    def failed(self) -> bool:
        """True iff this check observed a problem (a skip is not a pass)."""
        return self.status == FAIL


def _gcloud(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a read-only ``gcloud`` probe.

    A probe that could not be launched at all is returned as a FAILED result
    carrying the reason, never as an absent or clean one: "kew could not ask"
    and "the answer was yes" are different facts.
    """
    command = ["gcloud", *args]
    try:
        return subprocess_utils.run(command, check=False, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            command, 1, "", f"the probe itself could not run: {exc}"
        )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _check_gcloud_present() -> HostCheck:
    if subprocess_utils.ensure_tool("gcloud") is None:
        return HostCheck(
            "gcloud",
            FAIL,
            "not on PATH; the gcp host cannot be reached",
            hint=GCLOUD_INSTALL_HINT,
        )
    return HostCheck("gcloud", OK, "present on PATH")


def _check_gcloud_auth() -> HostCheck:
    """Probe the login by minting an access token — the one call expiry fails.

    ``gcloud auth list`` still names an account whose refresh token has
    expired, so it cannot answer this. The token itself is never read, logged
    or returned: only the exit status and, on failure, stderr.
    """
    result = _gcloud(["auth", "print-access-token"])
    if result.returncode != 0:
        return HostCheck(
            "gcloud-auth",
            FAIL,
            f"gcloud login is expired or absent: {_first_line(result.stderr)}",
            GCLOUD_LOGIN_FIX,
        )
    return HostCheck("gcloud-auth", OK, "gcloud holds an active login")


def _instance_list_fix(project: str) -> str:
    """The listing command for ``project``, or empty when it is not a project id."""
    if _GCP_PROJECT_RE.fullmatch(project) is None:
        return ""
    return shlex.join(["gcloud", "compute", "instances", "list", "--project", project])


def _check_gcp_instance(settings: GcpHostConfig) -> HostCheck:
    instance = settings.instance
    fix = _instance_list_fix(settings.project)
    result = _gcloud(
        [
            "compute",
            "instances",
            "describe",
            instance,
            f"--project={settings.project}",
            f"--zone={settings.zone}",
            "--format=value(status)",
            "--quiet",
        ]
    )
    if result.returncode != 0:
        return HostCheck(
            "gcp-instance",
            FAIL,
            f"instance {instance} is unreachable: {_first_line(result.stderr)}",
            fix,
            hint=""
            if fix
            else f"[hosts.gcp] project {settings.project!r} is not a GCP project id",
        )
    status = (result.stdout or "").strip() or "an unreported status"
    return HostCheck("gcp-instance", OK, f"instance {instance} reads {status}")


def _gcp_checks(settings: GcpHostConfig) -> list[HostCheck]:
    """The gcp probes, in dependency order, stopping at the first blocker."""
    checks = [_check_gcloud_present()]
    if checks[-1].failed:
        return [*checks, *_skipped("gcloud is not installed")]
    checks.append(_check_gcloud_auth())
    if checks[-1].failed:
        return [
            *checks,
            HostCheck(
                "gcp-instance",
                SKIP,
                "not probed: gcloud has no usable login",
                GCLOUD_LOGIN_FIX,
            ),
        ]
    return [*checks, _check_gcp_instance(settings)]


def _skipped(reason: str) -> list[HostCheck]:
    return [
        HostCheck(
            "gcloud-auth", SKIP, f"not probed: {reason}", hint=GCLOUD_INSTALL_HINT
        ),
        HostCheck(
            "gcp-instance", SKIP, f"not probed: {reason}", hint=GCLOUD_INSTALL_HINT
        ),
    ]


def check_hosts(config: DispatchConfig) -> list[HostCheck]:
    """Probe every configured host provider.

    ``local`` is always reported: it is the default and needs no cloud account,
    so "hosts" is never an empty section. ``gcp`` is probed whenever
    ``[hosts.gcp]`` names an instance, selected or not — a configured host that
    nobody has logged into yet is exactly what this is for.
    """
    checks = [HostCheck("local", OK, "available; no cloud account needed")]
    if config.hosts.gcp.configured():
        checks.extend(_gcp_checks(config.hosts.gcp))
    return checks
