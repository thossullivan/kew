"""Read-only GitHub Actions self-hosted runner liveness checks."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from kew.runner_fields import exception_text
from kew.subprocess_utils import run


@dataclass(frozen=True)
class RunnerLivenessResult:
    """The liveness state of a repository's configured self-hosted fleet."""

    configured: bool
    offline_runners: tuple[str, ...] = ()
    error: str | None = None


def _error_text(result: Any) -> str:
    stderr = str(getattr(result, "stderr", "") or "").strip()
    stdout = str(getattr(result, "stdout", "") or "").strip()
    return (stderr or stdout or "gh returned a non-zero exit status")[:240]


def _run_api(command: list[str]) -> tuple[Any | None, str | None]:
    try:
        result = run(command, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, exception_text(exc)[:240]
    if result.returncode != 0:
        return result, _error_text(result)
    return result, None


def _parse_json(result: Any) -> tuple[Any | None, str | None]:
    try:
        return json.loads(result.stdout), None
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON from gh: {exception_text(exc)}"


def _runner_pages(payload: Any) -> list[dict[str, Any]]:
    pages = payload if isinstance(payload, list) else [payload]
    runners: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        rows = page.get("runners", [])
        if isinstance(rows, list):
            runners.extend(row for row in rows if isinstance(row, dict))
    return runners


def _target_configuration(repo: str) -> tuple[bool, str | None]:
    variable_result, error = _run_api(
        ["gh", "api", f"repos/{repo}/actions/variables/RUNNER_TARGET"]
    )
    if error is not None:
        if "404" in error:
            return False, None
        return False, error
    assert variable_result is not None
    variable, error = _parse_json(variable_result)
    if error is not None:
        return False, error
    if not isinstance(variable, dict) or not str(variable.get("value", "")).strip():
        return False, None
    return True, None


def _offline_runners(repo: str) -> tuple[tuple[str, ...] | None, str | None]:
    runners_result, error = _run_api(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runners",
            "--paginate",
            "--slurp",
        ]
    )
    if error is not None:
        return None, error
    assert runners_result is not None
    payload, error = _parse_json(runners_result)
    if error is not None:
        return None, error

    offline: list[str] = []
    for runner in _runner_pages(payload):
        if str(runner.get("status", "")).lower() != "offline":
            continue
        name = runner.get("name")
        if not isinstance(name, str) or not name:
            name = f"runner-{runner.get('id', 'unknown')}"
        if name not in offline:
            offline.append(name)
    return tuple(offline), None


def check_runner_liveness(repo: str) -> RunnerLivenessResult:
    """Check configured self-hosted runners without changing GitHub state.

    The repository variable is the same switch used by the CI workflows. A
    missing variable means CI uses its hosted-runner fallback, so the local
    check is skipped. API and parsing failures are returned as diagnostics
    rather than raised into ``kew doctor``.
    """
    configured, error = _target_configuration(repo)
    if error is not None or not configured:
        return RunnerLivenessResult(configured=False, error=error)
    offline, error = _offline_runners(repo)
    if error is not None:
        return RunnerLivenessResult(configured=True, error=error)
    assert offline is not None
    return RunnerLivenessResult(configured=True, offline_runners=offline)
