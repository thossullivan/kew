"""Typer sub-app objects for the command groups split across `cli/` submodules.

These live in their own cycle-free module (it imports nothing from the package
`__init__`) so a command submodule can attach commands with
`@epic_app.command(...)` by importing the app object here, without importing the
package `__init__` at type-check/definition time. The package `__init__` imports
these same objects and wires them onto the root `app` via `add_typer`.
"""

from __future__ import annotations

from typing import Any

import typer
from typer.core import TyperGroup


class _TriageGroup(TyperGroup):
    """Route ``kew triage <item>`` to the hidden ``item`` command.

    ``kew triage`` has to accept both a bare item (``kew triage 2293``) and
    named subcommands (``queue``, ``decide``). Declaring the item as an
    argument on the GROUP cannot do that: the group's own parameters are bound
    before subcommand resolution, so ``kew triage queue`` would arrive as
    ``item = "queue"``. Resolving it here instead keeps the subcommands real
    and keeps the bare form working — a name the group does not have is an
    item, and anything else is resolved as Click would.
    """

    def resolve_command(
        self, ctx: Any, args: list[str]
    ) -> tuple[str | None, Any, list[str]]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["item", *args]
        return super().resolve_command(ctx, args)


loop_app = typer.Typer(help="Manage first-class loops.")
config_app = typer.Typer(help="Inspect effective configuration and its sources.")
models_app = typer.Typer(
    help="Model catalog: refresh prices and lifecycle from their sources."
)
model_eol_app = typer.Typer(
    help="Model lifecycle: read-only check against the curated registry."
)
ingest_app = typer.Typer(help="Ingest external runner usage.")
epic_app = typer.Typer(help="Build, validate, and visualize epic dependency graphs.")
acceptance_app = typer.Typer(
    help="Amend an epic node's acceptance contract; the implementer never edits it."
)
epic_app.add_typer(acceptance_app, name="acceptance")
plan_app = typer.Typer(
    help="Read the acceptance criteria kew recorded for a work item."
)
triage_app = typer.Typer(
    cls=_TriageGroup,
    help="Classify a work item and record the proposal; a person decides.",
)
delegate_app = typer.Typer(help="Watch for assign-to-kew delegation and dispatch it.")
hosts_app = typer.Typer(
    help="Build and inspect the machines kew ships verification jobs to."
)
doctor_app = typer.Typer(
    help="Run local environment diagnostics.", invoke_without_command=True
)
publication_app = typer.Typer(
    help="Operator evidence about heads published outside kew."
)
serve_app = typer.Typer(
    help="Serve the kew web UI + API + approval inbox on localhost.",
    invoke_without_command=True,
)
prompts_app = typer.Typer(
    help="See and change the operator profile each agent role's prompt runs with."
)
quota_app = typer.Typer(help="Release a runner lane held after a quota refusal.")
drive_app = typer.Typer(
    help="Drive every active epic of the project from one supervised process.",
    invoke_without_command=True,
)
run_request_app = typer.Typer(
    help="Queue a kew command for the running drive, as the console's Run does."
)
no_change_app = typer.Typer(
    help="Decide a no-change outcome: confirm, reject, or retry its sync."
)
merge_app = typer.Typer(
    help="The merge verdict rule, and the merge queue verified with kew verify."
)
merge_queue_app = typer.Typer(
    help="Queue READY PR heads; one FIFO shared by every session on the host."
)
merge_app.add_typer(merge_queue_app, name="queue")
merge_claim_app = typer.Typer(
    help="Hold a PR head while you verify it, so the drive pushes nothing onto it."
)
merge_app.add_typer(merge_claim_app, name="claim")
