"""`kew loop` command group: resume and pause first-class loops."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    pass

from kew import cli as _c
from kew.audit import bind_policy_context
from kew.cli._apps import loop_app
from kew.progress import _error, _success
from kew.selector import GhQueryError


@loop_app.command("resume")
def loop_resume(
    loop_id: str = typer.Argument(..., help="The loop id to resume."),
    guidance: str = typer.Option(
        ..., "--guidance", help="Human guidance injected into the continuation."
    ),
    budget: float | None = typer.Option(
        None, "--budget", help="Bump the loop budget cap (USD)."
    ),
    max_steps: int | None = typer.Option(
        None, "--max-steps", help="Bump the loop step cap."
    ),
    timeout: int | None = typer.Option(
        None, "--timeout", help="Bump the loop wall-clock cap (minutes)."
    ),
    runner: str | None = typer.Option(
        None,
        "--runner",
        help="Reassign the loop to a different native runner for the continuation "
        "(e.g. claude-code, opencode).",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the model for the continuation (must be priced — in "
        "the model catalog/pricing.overrides — or a free ollama/* model).",
    ),
) -> None:
    """Resume a needs_human / budget_limited loop with guidance."""
    from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE
    from kew.loops.delegate import DelegateDriver
    from kew.worktree import CheckoutAdmissionRefusal, CheckoutBusy

    # Resume shells out to git/gh (reuse_or_restore_worktree + the continuation),
    # so the subprocess allowlist must be populated — like _run_loop_dispatch does.
    # (pause is DB-only and deliberately does NOT require git/gh.)
    _c.preflight_tools()
    config = _c.load_config()
    bind_policy_context(config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve with the store so DB overrides (e.g. agent.max_budget_usd)
        # govern the continuation's budget enforcement (R1, #902).
        config = _c.load_config(store=store)
        _c._detect_repo(config)
        loop = store.get_loop(loop_id)
        if loop is None:
            _error(f"loop {loop_id} not found")
            raise typer.Exit(2)
        driver = DelegateDriver(store, config=config)
        try:
            finished = driver.resume(
                loop,
                guidance,
                runner_name=runner,
                model=model,
                budget_usd=budget,
                max_steps=max_steps,
                timeout_minutes=timeout,
            )
        except (GhQueryError, OSError, ValueError, RuntimeError) as exc:
            _error(str(exc))
            code = (
                COULD_NOT_RUN_EXIT_CODE
                if isinstance(exc, (CheckoutAdmissionRefusal, CheckoutBusy))
                else _c._loop_exception_exit_code(exc, launched=driver.launched)
            )
            raise typer.Exit(code) from exc
    _c._finish_loop_result(finished, launched=driver.launched)


@loop_app.command("pause")
def loop_pause(
    loop_id: str = typer.Argument(..., help="The loop id to pause."),
) -> None:
    """Pause a running loop by stopping its current step.

    The loop runner checks for the request every few seconds while a step's
    agent runs, then stops that agent: SIGTERM to its process group and
    SIGKILL 10 seconds later, or a kill of its container when sandboxed. The
    step's work is not finished first."""

    config = _c.load_config()
    bind_policy_context(config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        if not store.request_pause(loop_id):
            _error(
                f"loop {loop_id} is not running (only a pursuing loop can be paused)"
            )
            raise typer.Exit(2)
    _success(
        f"Pause requested for loop {loop_id}; a running agent is stopped within "
        "seconds. "
        f'Resume with: kew loop resume {loop_id} --guidance "..."'
    )
