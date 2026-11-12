"""Runner preflight: can the runner a launch will use actually run?

Pure functions, no printing and no exits, so every launch path can ask the
same question: direct dispatch, the epic child and its continuations, epic
resume, planning, review remediation, conflict resolution, CI fix, driver CI
recovery, and loops. The CLI wrapper in ``kew.cli`` prints the answer and
exits; driver paths record it as a hold instead.

This lives below ``kew.cli`` on purpose. The library modules that launch
runners must not import the CLI layer (the layered-dispatch import contract
in pyproject.toml), and until this module existed they had to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kew.runner_fields import exception_text
from kew.subprocess_utils import RUNNER_REQUIREMENTS, inspect_runner

if TYPE_CHECKING:
    from kew.config import DispatchConfig


def runner_preflight_result(
    config: DispatchConfig, *, runner_name: str | None = None
) -> tuple[str | None, bool]:
    """Return ``(hard failure or None, warn about a missing CLI login)``.

    ``runner_name`` names the runner at a resolved launch boundary, which is
    the runner that will actually launch. Omitting it inspects the configured
    default route, the behaviour every caller had before routes could differ
    from the default (review of #1797, round 2).
    """
    try:
        reach = (
            inspect_runner(config)
            if runner_name is None
            else inspect_runner(config, runner_name=runner_name)
        )
    except ValueError as exc:
        return exception_text(exc), False

    if reach.binary is not None and not reach.binary_present:
        _, hint = RUNNER_REQUIREMENTS[reach.runner_name]
        return (
            f"Runner '{reach.runner_name}' needs the '{reach.binary}' CLI, "
            f"which was not found. Install: {hint}",
            False,
        )

    if reach.runner_name == "codex" and reach.version_supported is not True:
        version = reach.version or "unparseable"
        return (
            f"Runner 'codex' version {version} is unsupported. Version 0.145.0 "
            "or newer is required; upgrade with: npm install -g @openai/codex",
            False,
        )

    if reach.runner_name == "codex" and reach.credential_detected is not True:
        return "Runner 'codex' is not logged in. Authenticate with: codex login", False

    return None, reach.credential_detected is False


def runner_preflight_failure(
    config: DispatchConfig, *, runner_name: str | None = None
) -> str | None:
    """The hard preflight failure for the runner that will launch, or None."""
    failure, _warn_cli_login = runner_preflight_result(config, runner_name=runner_name)
    return failure
