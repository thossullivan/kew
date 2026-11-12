"""`kew init`: write a project's kew.toml from detection and flags (#2502).

The command is a thin shell over :mod:`kew.onboarding`: detect, choose, write
the smallest valid file, then run ``kew setup`` and the ``kew doctor`` checks
and say what to fix and what to run first.

On a terminal without ``--yes`` it asks for what detection could not answer
(and the runner, with the posture table), offers to run the fixes it can, and
ends with the ``kew init --yes`` command that repeats the run (#2503). Without
a terminal and without ``--yes`` it refuses, naming the flags it would have
asked for, rather than wait on a prompt nobody can answer.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from kew import cli as _c
from kew import onboarding
from kew.cli.doctor import _render_check, run_checks
from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE, FINDINGS_EXIT_CODE
from kew.config import ConfigError, DispatchConfig, project_root
from kew.progress import _error, _log, _success, _warn
from kew.readiness import CheckResult, CheckStatus
from kew.runner_fields import exception_text
from kew.subprocess_utils import RUNNER_REQUIREMENTS

CONFIG_FILE = "kew.toml"

Field = tuple[str, str]

#: The question asked for each field an interactive init may ask for.
_QUESTIONS: dict[Field, str] = {
    ("project", "repo"): "GitHub repository (owner/name)",
    ("commands", "ci"): "CI command agents must pass",
    ("commands", "format"): "Format command (empty for none)",
    ("sandbox", "runtime"): "Container runtime (docker, podman or openshell)",
}

#: Runners ``kew sandbox build`` has a packaged image for.
_BUILDABLE_RUNNERS = ("claude-code", "codex", "opencode")
_IMAGE_CHECKS = frozenset({"route-image", "agent-image"})
_NETWORK_CHECK = "allowlist-network"


def _stdin_is_tty() -> bool:
    """Whether there is a person on stdin to ask."""
    return sys.stdin.isatty()


def _write(path: Path, text: str, *, force: bool) -> None:
    """Write ``text``; without ``force`` an existing file is never touched.

    Exclusive create, not check-then-write: a file that appears between the
    existence check and the write is refused too.
    """
    mode = "w" if force else "x"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(text)


def _run_setup() -> bool:
    """``kew setup`` against the file just written; ``False`` if it did not finish.

    Setup ends with ``SystemExit`` when a required tool is missing (its tool
    preflight), not only ``typer.Exit``; either is a setup finding here, so the
    doctor checks still run and say what to install.
    """
    try:
        _c.setup(with_skills=False)
    except typer.Exit as exc:
        return exc.exit_code == 0
    except SystemExit as exc:
        return exc.code in (None, 0)
    return True


def _readiness(config: DispatchConfig) -> list[CheckResult]:
    """Every ``kew doctor`` check, ``runner-credential`` among them."""
    return run_checks(config)


def _report(results: list[CheckResult]) -> list[str]:
    for result in results:
        _render_check(result)
    return [result.id for result in results if result.is_issue]


def _check_runner(runner: str | None) -> None:
    if runner is not None and runner not in RUNNER_REQUIREMENTS:
        _error(
            f"Unknown runner {runner!r}; choose one of: "
            f"{', '.join(RUNNER_REQUIREMENTS)}"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


def _refuse_without_terminal(ask: tuple[Field, ...]) -> None:
    """No terminal and no ``--yes``: name the flags instead of hanging on a prompt."""
    missing = ", ".join(onboarding.INIT_FLAGS[key] for key in ask)
    _error(
        "kew init: stdin is not a terminal, so there is no one to ask; pass --yes"
        + (f", and {missing} for what was not given or detected" if missing else "")
    )
    raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


def _known_runner(answer: str) -> str:
    """A runner name, or a re-prompt naming the choices."""
    if answer not in RUNNER_REQUIREMENTS:
        raise typer.BadParameter(f"choose one of: {', '.join(RUNNER_REQUIREMENTS)}")
    return answer


def _ask_runner(detection: onboarding.Detection, runtime: str | None) -> str:
    """The runner, chosen with every posture and its cost on one screen."""
    _log("Each runner runs agents under one containment posture:")
    for line in onboarding.posture_table(runtime, detection.runners):
        typer.echo(f"  {line}")
    default = (
        detection.runners[0] if detection.runners else onboarding.RUNNER_PREFERENCE[0]
    )
    return str(
        typer.prompt(
            "Runner",
            default=default,
            value_proc=_known_runner,
        )
    )


def _ask(
    detection: onboarding.Detection,
    flags: dict[Field, object | None],
    ask: tuple[Field, ...],
) -> dict[Field, object | None]:
    """``flags`` with each field in ``ask`` answered at the terminal."""
    answers = dict(flags)
    defaults = DispatchConfig()
    for key in ask:
        if key == onboarding.RUNNER_FIELD:
            runtime = answers[("sandbox", "runtime")] or detection.runtime
            answers[key] = _ask_runner(detection, str(runtime) if runtime else None)
            continue
        default = str(getattr(getattr(defaults, key[0]), key[1]))
        answers[key] = typer.prompt(
            _QUESTIONS[key], default=default, show_default=bool(default)
        )
    return answers


def _render(chosen: dict[Field, object]) -> str:
    text = onboarding.render_toml(onboarding.minimal_values(chosen))
    try:
        onboarding.validate_toml(text)
    except ConfigError as exc:
        _error(f"kew init would write an invalid {CONFIG_FILE}: {exception_text(exc)}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
    return text


def _run_fix(command: str, fix: Callable[[], None]) -> None:
    """Run ``command`` if the person says so; a failure is reported, not raised."""
    if not typer.confirm(f"Run `{command}` now?", default=True):
        return
    try:
        fix()
    except typer.Exit as exc:
        if exc.exit_code != 0:
            _warn(f"`{command}` did not finish (exit {exc.exit_code})")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _warn(f"`{command}` did not finish (exit {exc.code})")


def _offer_fixes(results: list[CheckResult], runner: str) -> None:
    """Offer to run the fixes init can run itself, before the closing checks."""
    issues = {result.id for result in results if result.is_issue}
    if issues & _IMAGE_CHECKS and runner in _BUILDABLE_RUNNERS:
        _run_fix(
            f"kew sandbox build --runner {runner}",
            lambda: _c.sandbox_build(runner=runner, tag=None, unpinned=False),
        )
    if _NETWORK_CHECK in issues:
        _run_fix("kew sandbox network", lambda: _c.sandbox_network(check=False))
    credential_failed = any(
        result.id == onboarding.RUNNER_CREDENTIAL_CHECK
        and result.status is CheckStatus.FAIL
        for result in results
    )
    if credential_failed and runner == "claude-code":
        _log(
            "To get a claude-code credential: run `claude setup-token` in another "
            "terminal, then `export CLAUDE_CODE_OAUTH_TOKEN=<token>` in the shell "
            "you dispatch from"
        )


def _finish(
    config: DispatchConfig, chosen: dict[Field, object], *, interactive: bool
) -> None:
    """Setup, the readiness checks, and the closing summary."""
    setup_ok = _run_setup()
    if interactive:
        from kew.runners.registry import resolve_runner

        _offer_fixes(_readiness(config), resolve_runner(config).runner.name)
    issues = _report(_readiness(config))
    if not setup_ok:
        issues.insert(0, "setup")
    if interactive:
        _log(f"To repeat this without prompts: {onboarding.replay_command(chosen)}")
    if issues:
        _warn(f"kew init: {len(issues)} issue(s) to fix: {', '.join(issues)}")
        _log("Then run: kew doctor")
        raise typer.Exit(FINDINGS_EXIT_CODE)
    _log("Ready. First command to run: kew run --dry-run")


def init(
    runner: Annotated[
        str | None,
        typer.Option("--runner", help="Runner to dispatch with (default: detected)."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model the runner should use.")
    ] = None,
    ci: Annotated[
        str | None,
        typer.Option("--ci", help="CI command agents must pass (default: detected)."),
    ] = None,
    format_cmd: Annotated[
        str | None,
        typer.Option("--format", help="Format command (default: detected)."),
    ] = None,
    runtime: Annotated[
        str | None,
        typer.Option("--runtime", help="Container runtime (default: detected)."),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="GitHub repository owner/name (default: detected)."
        ),
    ] = None,
    budget_per_issue: Annotated[
        float | None,
        typer.Option("--budget-per-issue", min=0, help="Spend cap per issue, in USD."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Write without asking anything."),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing kew.toml.")
    ] = False,
) -> None:
    """Write kew.toml from detection, flags and answers, then run setup and doctor."""
    _check_runner(runner)
    root = project_root()
    path = root / CONFIG_FILE
    if path.exists() and not force:
        _error(f"{path} already exists; pass --force to overwrite it")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)

    flags: dict[Field, object | None] = {
        ("project", "repo"): repo,
        onboarding.RUNNER_FIELD: runner,
        ("agent", "model"): model,
        ("commands", "ci"): ci,
        ("commands", "format"): format_cmd,
        ("sandbox", "runtime"): runtime,
        ("budget", "per_issue_usd"): budget_per_issue,
    }
    detection = onboarding.detect(root)
    interactive = not yes and _stdin_is_tty()
    if not yes and not interactive:
        _refuse_without_terminal(onboarding.fields_to_ask(detection, flags))
    if interactive:
        flags = _ask(detection, flags, onboarding.fields_to_ask(detection, flags))
    chosen = onboarding.choose(detection, flags)
    text = _render(chosen)

    typer.echo(text, nl=False)
    if interactive and not typer.confirm(f"Write {path}?", default=True):
        _warn("kew init: declined; nothing was written")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    try:
        _write(path, text, force=force)
    except FileExistsError:
        _error(f"{path} already exists; pass --force to overwrite it")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
    _success(f"Wrote {path}")
    _finish(_c.load_config(path), chosen, interactive=interactive)
