"""CLI entry point for kew.

Orchestrates config loading, preflight, issue selection, and
delegates dispatch execution to dispatch_core.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn

import typer
from typer.core import TyperGroup

from kew import work_dirs
from kew.runner_fields import exception_text

if TYPE_CHECKING:
    from kew.canary import GovernanceFields
    from kew.loops.delegate import DelegateDriver
    from kew.merge_claim import BusyRun, HeldClaim
    from kew.merge_verify import VerifyCandidate, VerifyResult
    from kew.report_provenance import ProvenanceExport
    from kew.run_store import LoopRecord, RunRecord, RunStore, SqliteRunStore
    from kew.runners.protocol import ResultClassification
    from kew.scheduler import DrainResult
    from kew.service.models import SandboxPostureDTO, WorkViewDTO

import kew.resolve as _resolve_module
from kew.audit import (
    DEFAULT_ANCHORS_PATH,
    audit_app,
    bind_policy_context,
    record_current_policy,
)
from kew.canary import CanaryReport, RunnerOutcome
from kew.ci_fix import CI_FIX_CANNOT_RUN, CI_FIX_QUERY_FAILED, dispatch_ci_fix
from kew.cli import json_result
from kew.cli._apps import (
    config_app,
    delegate_app,
    doctor_app,
    drive_app,
    epic_app,
    hosts_app,
    ingest_app,
    loop_app,
    merge_app,
    model_eol_app,
    models_app,
    no_change_app,
    plan_app,
    prompts_app,
    publication_app,
    quota_app,
    run_request_app,
    serve_app,
    triage_app,
)
from kew.command_outcome import (
    COULD_NOT_RUN_EXIT_CODE,
    FINDINGS_EXIT_CODE,
    SUCCESS_EXIT_CODE,
    CommandOutcome,
)
from kew.config import (
    ConfigError,
    DispatchConfig,
    RouteEntry,
    project_root,
    resolved_config_path,
)
from kew.config import (
    load_config as _load_config,
)
from kew.continuation_recovery import checkout_refusal_categories
from kew.dispatch_core import execute_dispatch
from kew.fleet_placement import placement_lines
from kew.fleet_placement import run_site as _run_site
from kew.health import CheckResult, HealthReport, run_health_checks
from kew.labels import (
    ClaimResult,
    classify_admission,
    release_approval_hold,
    try_claim_issue,
)
from kew.ledger_identity import (
    LedgerIdentityError,
    canonical_project_root,
    open_managed_store,
)
from kew.links import UPGRADE_HINT, docs_url
from kew.models import GhIssue, GhLabel
from kew.notifier import Notifier, ReportDeliveryError, build_notifier, deliver_report
from kew.outcomes import (
    OrphanReconciliationResult,
    _QueryFailure,
    detect_orphan_prs,
    reconcile_orphan_prs,
    sync_outcomes,
)
from kew.overlap import DispatchOutcome
from kew.progress import _dim, _error, _log, _success, _warn
from kew.prompt import build_prompt
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL, PromptConfigurationError
from kew.report import (
    build_model_scorecard_result,
    build_report_result,
    ingest_cap_alert,
)
from kew.review import dispatch_review
from kew.run_store import SchemaTooNewError
from kew.runner_preflight import (
    runner_preflight_failure as runner_preflight_failure,  # noqa: F401  (re-export for tests + epic submodule)
)
from kew.runner_preflight import runner_preflight_result
from kew.runners.protocol import AgentRunner
from kew.runners.registry import ResolvedRoute
from kew.runners.registry import (
    resolve_runner as resolve_runner,  # noqa: F401  (re-export for the epic submodule)
)
from kew.scheduler import DispatchScheduler
from kew.selector import (
    GhQueryError,
    empty_queue_message,
    list_ready_issues,
    select_next_issue,
)
from kew.selector import (
    get_complexity as get_complexity,  # noqa: F401  (re-export for the epic submodule)
)
from kew.service import (
    verbs as service_verbs,  # noqa: F401  (re-export for epic submodule + tests)
)
from kew.service.prompt_profiles import launch_addendum
from kew.skills_cli import skills_app
from kew.subprocess_utils import (
    TOOL_PATHS,
    inspect_runner,
    popen,
    preflight_tools,
    run,
    run_with_backoff,
)
from kew.switchyard import SwitchyardProbeResult, probe_switchyard_endpoint
from kew.switchyard_benchmark import (
    BenchmarkError,
    render_benchmark_json,
    run_live_switchyard_benchmark,
)
from kew.switchyard_metrics import (
    ProxyMetricsObservation,
    SwitchyardMetricsError,
    observe_switchyard_metrics,
)
from kew.tui.palette import ATTESTED, JUDGMENT, WARN
from kew.worktree import (
    remote_branch_exists,
)


def _managed_store(db_path: Path, *, read_only: bool = False) -> SqliteRunStore:
    """The managed ledger open itself, with its refusals left UNHANDLED.

    ``_open_run_store`` wraps this in the CLI's report-and-exit boundary, which
    is what nearly every caller wants. A caller for which a refusal is not the
    answer but a reason to do something else calls this and catches: the
    journaled ``kew epic watch`` read pays for the live read instead, because a
    legacy ledger carrying neither a registry entry nor an identity binding is
    refused by the READ-ONLY open alone — adopting one is a write (#2476).
    """
    root = canonical_project_root(project_root())
    open_kwargs: dict[str, Any] = {
        "project_root": root,
        "db_path": db_path,
        "anchors_path": root / DEFAULT_ANCHORS_PATH,
    }
    if read_only:
        open_kwargs["read_only"] = True
    return open_managed_store(**open_kwargs)


@contextmanager
def _open_run_store(
    db_path: str | Path, *, read_only: bool = False
) -> Generator[SqliteRunStore]:
    """Open the registered project ledger at the shared CLI boundary."""
    canonical_db_path = Path(db_path).resolve()
    try:
        store = _managed_store(canonical_db_path, read_only=read_only)
    except LedgerIdentityError as exc:
        _error(exception_text(exc))
        raise typer.Exit(2) from exc
    except SchemaTooNewError as exc:
        _error(
            f"{exception_text(exc)}\n"
            f"       Fix with:\n"
            f"         {UPGRADE_HINT}\n"
            f"       (db: {canonical_db_path})"
        )
        raise typer.Exit(2) from exc
    try:
        yield store
    finally:
        store.close()


CONFIG_ERROR_EXIT_CODE = 2


class _ConfigErrorGroup(TyperGroup):
    """Render configuration failures at the root command boundary."""

    # `Any`, not typer.Context: click's Group.invoke takes click.Context and a
    # narrower override violates the supertype; the body only forwards.
    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            typer.echo(exception_text(exc), err=True)
            raise typer.Exit(CONFIG_ERROR_EXIT_CODE) from None


app = typer.Typer(
    help="The open-source control plane for autonomous coding agents",
    cls=_ConfigErrorGroup,
)
app.add_typer(audit_app, name="audit")


def load_config(
    config_path: Path | None = None,
    *,
    store: RunStore | None = None,
) -> DispatchConfig:
    """Preserve the CLI monkeypatch surface for configuration loading."""
    return _load_config(config_path, store=store)


# Public failure code: the command could not determine the queue state.
GH_QUERY_FAILURE_EXIT_CODE = 2


@dataclass(frozen=True)
class DispatchCommandResult:
    """Keep selection states separate from raw runner exit codes."""

    kind: Literal["executed", "not_launched", "empty", "query_failed", "refused"]
    exit_code: int = 0
    #: Why nothing ran, for the ``--json`` summary (#2505); progress names it
    #: too. Prose, not state: two results of one kind are the same result.
    reason: str = field(default="", compare=False)


# Translate internal results only at public command boundaries.
_DISPATCH_EMPTY = DispatchCommandResult("empty")
_DISPATCH_GH_QUERY_FAILURE = DispatchCommandResult("query_failed")
# One named issue that was not dispatched: nothing ran, and a caller must see it.
_DISPATCH_REFUSED = DispatchCommandResult("refused")


def _dispatch_refusal(
    force_issue: int | None, reason: str = ""
) -> DispatchCommandResult:
    """Grade a not-dispatched issue by whether the caller named it (#2058).

    A queue cycle that dispatches nothing is an empty cycle (0). Refusing the one
    issue named on the command line is a could-not-run outcome (2): a script or a
    ``kew wait``-style caller has to be able to tell that no agent started.
    """
    result = _DISPATCH_REFUSED if force_issue is not None else _DISPATCH_EMPTY
    return replace(result, reason=reason)


#: The ``--json`` outcome of a dispatch that ran no agent (#2505); an executed
#: dispatch takes its outcome from its run record instead.
_JSON_DISPATCH_OUTCOMES = {
    "empty": "empty",
    "query_failed": "query_failed",
    "refused": "refused",
    "not_launched": "refused",
}


def _note_dispatch_result(result: DispatchCommandResult, *, dry_run: bool) -> None:
    """Hand a single dispatch's result to the ``--json`` summary, if one is on."""
    outcome = _JSON_DISPATCH_OUTCOMES.get(result.kind)
    if outcome is None and dry_run:
        outcome = "dry_run"
    json_result.note(outcome=outcome, reason=result.reason or None)


def _command_exit_code(outcome: CommandOutcome) -> int:
    """Translate execution state only at a public command boundary."""
    if outcome.kind == "not_run":
        return COULD_NOT_RUN_EXIT_CODE
    return FINDINGS_EXIT_CODE if outcome.exit_code else SUCCESS_EXIT_CODE


def _dispatch_exit_code(result: DispatchCommandResult) -> int:
    """Translate an internal dispatch result to the public exit scheme.

    ``executed`` carries the raw runner code and maps to 0/1; ``not_launched``
    is a nonzero outcome decided before any agent started (capacity, admission,
    approval, connectivity, launch failure, refusal) and maps to 2 like every
    other could-not-run outcome. ``refused`` is the same class for a named issue
    the admission checks turned away (#2058).
    """
    return {
        "executed": int(result.exit_code != 0),
        "not_launched": GH_QUERY_FAILURE_EXIT_CODE,
        "empty": 0,
        "query_failed": GH_QUERY_FAILURE_EXIT_CODE,
        "refused": COULD_NOT_RUN_EXIT_CODE,
    }[result.kind]


app.add_typer(loop_app, name="loop")
app.add_typer(config_app, name="config")
app.add_typer(ingest_app, name="ingest")
app.add_typer(models_app, name="models")
app.add_typer(model_eol_app, name="model-eol")
app.add_typer(epic_app, name="epic")
app.add_typer(drive_app, name="drive")
app.add_typer(plan_app, name="plan")
app.add_typer(triage_app, name="triage")
app.add_typer(delegate_app, name="delegate")
app.add_typer(doctor_app, name="doctor")
app.add_typer(hosts_app, name="hosts")
app.add_typer(publication_app, name="publication")
app.add_typer(no_change_app, name="no-change")
app.add_typer(prompts_app, name="prompts")
app.add_typer(quota_app, name="quota")
app.add_typer(merge_app, name="merge")
app.add_typer(run_request_app, name="run-request")

report_app = typer.Typer(
    help="Outcome-linked spend reports and the evidence-pack export."
)
app.add_typer(report_app, name="report")

app.add_typer(skills_app, name="skills")

sandbox_app = typer.Typer(help="Sandboxed-dispatch container tooling.")
app.add_typer(sandbox_app, name="sandbox")

app.add_typer(serve_app, name="serve")

switchyard_app = typer.Typer(help="Human-gated Switchyard passthrough evidence tools.")
app.add_typer(switchyard_app, name="switchyard")


@switchyard_app.command("benchmark")
def switchyard_benchmark(
    corpus_path: Annotated[
        Path,
        typer.Option(
            "--corpus",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Fixed benchmark corpus JSON file.",
        ),
    ],
    runner_name: Annotated[
        str,
        typer.Option(
            "--runner",
            help="Passthrough-capable runner: claude-code or codex.",
        ),
    ],
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help="Exact model used by both direct and passthrough legs.",
        ),
    ],
) -> None:
    """Compare a fixed corpus through direct and managed passthrough modes."""
    try:
        report = run_live_switchyard_benchmark(
            config=load_config(),
            corpus_path=corpus_path,
            runner_name=runner_name,
            model=model,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        _error(f"Switchyard benchmark refused: {exception_text(exc)}")
        raise typer.Exit(2) from None
    typer.echo(render_benchmark_json(report), nl=False)
    raise typer.Exit(report.exit_code)


@sandbox_app.command("build")
def sandbox_build(
    runner: Annotated[
        str,
        typer.Option(
            "--runner",
            help="Packaged image runner: claude-code, codex or opencode.",
        ),
    ] = "claude-code",
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag",
            help="Output tag. Defaults to the packaged input digest pin.",
        ),
    ] = None,
    unpinned: Annotated[
        bool,
        typer.Option(
            "--unpinned",
            help="Allow --tag to differ from the packaged input digest pin.",
        ),
    ] = False,
) -> None:
    """Build a runner image from the inputs installed with kew."""
    from kew.sandbox import SandboxBuildFailed, _image_config_field, build_sandbox_image

    supported = ("claude-code", "codex", "opencode")
    if runner not in supported:
        _error(f"sandbox build supports {', '.join(supported)}; got runner {runner!r}")
        raise typer.Exit(2)
    sandbox = load_config().sandbox
    try:
        result = build_sandbox_image(
            sandbox,
            runner=runner,
            tag=tag,
            unpinned=unpinned,
        )
    except SandboxBuildFailed as exc:
        _error(f"sandbox build failed: {exception_text(exc)}")
        raise typer.Exit(1) from None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        _error(f"sandbox build refused: {exception_text(exc)}")
        raise typer.Exit(2) from None
    _success(f"sandbox build OK — tag {result.tag}")
    typer.echo(f"Image ID: {result.image_id}")
    if not result.pinned:
        # Dispatch admits whatever tag the configuration names, so the honest
        # statement is what the tag is NOT, and what admitting it would mean.
        typer.echo(
            "Warning: this tag is not the packaged input pin. Dispatch runs it "
            f"only if {_image_config_field(runner)} names it, and "
            "containment then proves an image kew did not pin.",
            err=True,
        )
    name = result.network_name
    if name is None:
        typer.echo(
            'Network: [sandbox] network = "open". Dispatch launches with full '
            "egress and warns on every run; no network is consulted.",
            err=True,
        )
    elif result.allowlist_network_exists:
        typer.echo(f"Network: {name} exists. kew did not modify it.")
    else:
        typer.echo(f"Network: {name} is absent. kew did not create it.")
        typer.echo("Dispatch will refuse until it exists.")
        typer.echo(f"Provision a restricted network named {name} before dispatch.")
        typer.echo(
            "Do not use a plain bridge. A plain bridge permits unrestricted egress."
        )


@sandbox_app.command("network")
def sandbox_network(
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Report whether the network exists; create nothing."
        ),
    ] = False,
) -> None:
    """Create the kew-allowlist network contained dispatch attaches to."""
    from kew.runtime_names import OPENSHELL_RUNTIME
    from kew.sandbox import ALLOWLIST_NETWORK, ensure_allowlist_network

    sandbox = load_config().sandbox
    if sandbox.runtime == OPENSHELL_RUNTIME:
        typer.echo(
            f'[sandbox] runtime is "{OPENSHELL_RUNTIME}": OpenShell enforces its '
            f"own egress policy and attaches no {ALLOWLIST_NETWORK} network, so "
            "kew created nothing. Prove its egress with "
            "'kew sandbox certify --runtime openshell'."
        )
        return
    if sandbox.network != "allowlist":
        _error(
            f"[sandbox] network is {sandbox.network!r}, so dispatch never attaches "
            f'{ALLOWLIST_NETWORK}; set [sandbox] network = "allowlist" first'
        )
        raise typer.Exit(2)
    try:
        result = ensure_allowlist_network(sandbox, create=not check)
    except RuntimeError as exc:
        _error(f"sandbox network refused: {exception_text(exc)}")
        raise typer.Exit(2) from None
    if result.outcome == "absent":
        typer.echo(f"Network: {ALLOWLIST_NETWORK} is absent on {result.runtime}.")
        typer.echo("Run 'kew sandbox network' to create it.")
        raise typer.Exit(1)
    if result.outcome == "created":
        _success(f"Network: created {ALLOWLIST_NETWORK} on {result.runtime}.")
    else:
        typer.echo(f"Network: {ALLOWLIST_NETWORK} exists. kew did not modify it.")
    if result.driver == "bridge" and result.internal is False:
        typer.echo(
            f"Warning: {ALLOWLIST_NETWORK} is a plain bridge. kew installs no "
            "firewall rules on it, so the container has full egress until you "
            "restrict it to your inference endpoint and GitHub.",
            err=True,
        )


@sandbox_app.command("smoke")
def sandbox_smoke(
    runner: Annotated[
        str | None,
        typer.Option(
            "--runner",
            help=(
                "Runner whose containment to prove; its image comes from "
                "\\[sandbox] images.<runner> (default: claude-code)."
            ),
        ),
    ] = None,
    no_build: Annotated[
        bool,
        typer.Option(
            "--no-build",
            help="Fail (don't build) if the configured image tag is absent locally.",
        ),
    ] = False,
) -> None:
    """Launch one container end-to-end and name the first failing layer.

    Proves the LIVE sandbox path the mock suite cannot: runtime resolution,
    image presence (built from packaged inputs when a standard tag is absent),
    container launch, and the prompt-file handoff. Exits 0 on success.
    Exits 2 when the runtime prerequisite is missing. Later layer failures
    exit 1 and name the image, launch, execution, or auth layer.

    ``--runner`` picks which containment is proven: the image is resolved
    through the same runner-aware resolver dispatch uses, so smoking
    the default while dispatch routes elsewhere can no longer pass against an
    image that will never run.
    """
    from kew.runners.registry import KNOWN_RUNNER_NAMES
    from kew.sandbox import DEFAULT_IMAGE_RUNNER, redact_credentials, run_smoke

    target = runner or DEFAULT_IMAGE_RUNNER
    if target not in KNOWN_RUNNER_NAMES:
        # Caught here rather than as an "unmapped image" layer failure: an
        # unknown name can never be routed to, so the honest error is the name,
        # not the missing mapping it would otherwise report.
        _error(
            f"unknown runner {redact_credentials(target)!r} — known runners: "
            f"{', '.join(sorted(KNOWN_RUNNER_NAMES))}"
        )
        raise typer.Exit(2)

    config = load_config()
    result = run_smoke(config.sandbox, runner=target, build_if_missing=not no_build)
    detail = redact_credentials(result.detail)
    if result.ok:
        _success(f"sandbox smoke OK — {detail}")
        return
    _error(f"sandbox smoke FAILED at [{result.layer}] layer: {detail}")
    raise typer.Exit(2 if result.layer == "runtime" else 1)


@sandbox_app.command("certify")
def sandbox_certify(
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Container runtime whose egress to certify: openshell.",
        ),
    ],
    runner: Annotated[
        str,
        typer.Option(
            "--runner",
            help="Runner whose image and OpenShell policy are certified.",
        ),
    ] = "claude-code",
    flow: Annotated[
        str,
        typer.Option(
            "--flow",
            help=(
                "Which launch's boundary to certify: dispatch (the default) or "
                "review, the adversarial reviewer's narrower policy."
            ),
        ),
    ] = "dispatch",
) -> None:
    """Prove a runtime's egress is closed, and record the proof.

    Creates a throwaway sandbox from the runner's immutable image ID and the
    runner's own OpenShell policy, proves from inside it that the runner's
    inference host is reachable (over IPv4 and over IPv6) and that the
    sandbox's own resolver answers over UDP, proves that eight other
    destination classes are not reachable, appends the result to the audit
    chain only when every part passed, and deletes the sandbox. Admission on
    that runtime requires a passing certification no older than 24 hours for
    the exact boundary tuple.

    One runtime has more than one boundary. ``--flow review`` certifies the
    adversarial reviewer's own policy, which is bound to its own digest: a
    reviewer refuses until that proof exists, and the dispatch proof does not
    satisfy it (epic node N12). The provisioning boundary the reviewer's
    environment is installed under cannot be certified at all — its policy
    governs no interpreter the probe can run — and ``--flow review-provision``
    says so instead of probing.

    The probe runs once per binary the policy governs, each time under that
    binary's own clause, and every one has to hold.

    Exit 0 means a passing certification is on the chain. Exit 1 means the
    probe ran and the boundary did not hold — the output names every target and
    what it observed, and nothing is appended. Exit 2 means the probe could not
    be run at all, or it passed and the proof could not be recorded, or a probe
    sandbox could not be deleted: exit 1 is reserved for a boundary that
    leaked, so neither a storage nor a cleanup failure is reported as one.
    """
    from kew.audit import SidecarEvidencePersistenceError
    from kew.egress_certification import RUNTIME_PACKET_DENIALS
    from kew.openshell import OpenShellError
    from kew.openshell_egress import EgressProbeError, certify_openshell_egress
    from kew.sandbox import ContainedImageUnresolvedError, SandboxImageUnmapped

    if runtime not in RUNTIME_PACKET_DENIALS:
        supported = ", ".join(sorted(RUNTIME_PACKET_DENIALS))
        _error(
            f"sandbox certify has no egress contract for runtime {runtime!r}; "
            f"this build certifies {supported}"
        )
        raise typer.Exit(2)
    config = load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    try:
        with _open_run_store(db_path) as store:
            result = certify_openshell_egress(
                config,
                runner=runner,
                store=store,
                repo=config.project.repo or None,
                flow=flow,
            )
    except SidecarEvidencePersistenceError as exc:
        # The boundary held and the proof did not land. An operational failure
        # (exit 2), never the exit-1 "certification failed" a leak gets: what
        # has to be fixed is the ledger, not the policy, and admission goes on
        # refusing until a proof is actually on the chain (#2389 review).
        _error(
            f"sandbox certify proved the boundary but could not record it: "
            f"{exception_text(exc)} — nothing was appended, so dispatch on "
            f"{runtime} still refuses. Fix the ledger, then certify again."
        )
        raise typer.Exit(2) from None
    except (
        EgressProbeError,
        OpenShellError,
        SandboxImageUnmapped,
        ContainedImageUnresolvedError,
        OSError,
        ValueError,
    ) as exc:
        _error(f"sandbox certify refused: {exception_text(exc)}")
        raise typer.Exit(2) from None
    for name, detail in sorted(result.details.items()):
        typer.echo(f"  {name}: {detail}")
    certification = result.certification
    if certification.verdict != "passed":
        _error(
            f"sandbox certify FAILED — {runtime} {flow} egress verdict "
            f"{certification.verdict}; nothing was recorded"
        )
        _report_surviving_probe(result.cleanup_error)
        raise typer.Exit(1)
    if result.cleanup_error is not None:
        # The boundary held, but kew left a container it no longer owns. Not a
        # leak (exit 1) and not a pass (exit 0): an operational failure, with
        # nothing recorded, exactly like a proof the ledger could not take.
        _error(
            f"sandbox certify FAILED — the {runtime} {flow} boundary held but "
            "a probe sandbox survived its own cleanup; nothing was recorded"
        )
        _report_surviving_probe(result.cleanup_error)
        raise typer.Exit(2)
    _success(f"sandbox certify OK — {runtime} {flow} egress proven closed")
    typer.echo(f"Audit event: {result.event_id}")
    typer.echo(f"Image ID:    {certification.image_id}")
    typer.echo(f"Policy:      sha256:{certification.policy_sha256}")
    typer.echo(f"Binaries:    {', '.join(certification.binaries)}")
    typer.echo(f"Gateway:     {certification.gateway_version}")
    typer.echo(f"Gateway ID:  sha256:{certification.gateway_identity}")
    typer.echo(f"Supervisor:  sha256:{certification.supervisor_digest}")


def _report_surviving_probe(cleanup_error: str | None) -> None:
    """Name every probe sandbox ``sandbox certify`` could not delete."""
    if cleanup_error is not None:
        typer.echo(
            f"Probe sandbox not deleted ({cleanup_error}). Delete it before "
            "the next dispatch.",
            err=True,
        )


def _resolve_version() -> str:
    """Installed distribution version, with a source-checkout fallback."""
    try:
        return importlib.metadata.version("kew-cli")
    except importlib.metadata.PackageNotFoundError:
        from kew import __version__

        return __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kew {_resolve_version()}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the kew version and exit.",
        ),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            metavar="DIR",
            help="Resolve config + DB from this project directory regardless of "
            "cwd (sets KEW_PROJECT). Guards against the wrong-cwd footgun.",
        ),
    ] = None,
) -> None:
    """The open-source control plane for autonomous coding agents."""
    if project is not None:
        root = Path(project).expanduser()
        if not root.is_dir():
            _error(f"--project {project!r} is not a directory")
            raise typer.Exit(2)
        # Feed the existing KEW_PROJECT plumbing so every later load_config()
        # (in command bodies and helpers) resolves from this dir uniformly.
        os.environ["KEW_PROJECT"] = str(root)


def _emit_staleness_warnings() -> None:
    from kew.staleness import check_staleness

    for warning in check_staleness():
        _warn(warning)


def _emit_orchestrator_skew_warnings(config: DispatchConfig) -> None:
    from kew.staleness import check_orchestrator_skew

    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    for warning in check_orchestrator_skew(db_path=db_path):
        _warn(warning)


def _emit_stray_ledger_warnings(config: DispatchConfig) -> None:
    """Say so loudly when a second dispatch.db lives under the project root.

    Pre-#1156 invocations from a subdirectory minted their own ledger. Now that
    every relative path anchors to the project root those forks are inert — and
    invisible — so name them rather than let their runs, spend and audit events
    go on being silently uncounted. Cosmetic: never breaks a command.
    """
    from kew.config import project_root
    from kew.ledger_guard import stray_ledger_warning

    try:
        canonical = Path(config.logging.dir).resolve() / "dispatch.db"
        warning = stray_ledger_warning(project_root(), canonical)
    except Exception:  # noqa: BLE001 — a filesystem scan never breaks a command.
        return
    if warning is not None:
        _warn(warning)


def _emit_governance_drift_warnings(config: DispatchConfig) -> None:
    from kew.config import resolved_config_path
    from kew.staleness import check_governance_config_drift

    config_path = resolved_config_path()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if db_path.is_file():
        try:
            with _open_run_store(db_path) as store:
                warnings = check_governance_config_drift(config_path, store=store)
        except typer.Exit:
            raise
        except Exception:  # noqa: BLE001 — drift nudge must never break startup
            warnings = check_governance_config_drift(config_path)
    else:
        warnings = check_governance_config_drift(config_path)
    for warning in warnings:
        _warn(warning)


def _detect_repo(config: DispatchConfig) -> None:
    """Auto-detect the GitHub repo if not already set in config."""
    if not config.project.repo:
        try:
            result = run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".nameWithOwner",
                ],
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            _error("Repository detection unavailable — cannot run GitHub query.")
            raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
        if result.returncode == 0 and result.stdout.strip():
            config.project.repo = result.stdout.strip()


# The pure preflight lives in kew.runner_preflight so library launch paths
# can call it without importing the CLI layer. The names below are kept so
# existing callers and test patches keep working.
_runner_preflight_result = runner_preflight_result


def _preflight_runner(
    config: DispatchConfig, *, runner_name: str | None = None
) -> None:
    """Verify a runner's CLI is installed before dispatch.

    Hard-fails if the runner binary is missing (unambiguous). Emits a warning —
    not a hard gate — when the claude-code runner has no recognizable credentials,
    since Bedrock/Vertex/token auth are valid and not all detectable here.

    ``runner_name`` checks the resolved launch route. Omitting it checks the
    configured default route for backward compatibility.
    """
    failure, warn_cli_login = _runner_preflight_result(config, runner_name=runner_name)
    if failure is not None:
        _error(failure)
        raise typer.Exit(2)

    if warn_cli_login:
        _warn(
            "No API-key env var found; relying on the runner's own login "
            "(claude-code). If the run can't authenticate, see "
            f"{docs_url('authentication')}"
        )


def _preflight(config: DispatchConfig, *, check_runner: bool = False) -> None:
    """Run preflight checks: tools, auth, repo detection.

    Set ``check_runner`` for commands that actually dispatch an agent (run,
    review, resolve, ci-fix) so the configured runner's CLI + credentials are
    verified up front. Read-only commands leave it off.
    """
    # Show which control plane we're touching before doing anything (F1).
    _log_project_banner(config)

    # ...and flag any second ledger the pre-#1156 cwd resolution left behind.
    _emit_stray_ledger_warnings(config)

    preflight_tools()

    # Check gh authentication
    try:
        result = run(["gh", "auth", "status"], check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        _error("GitHub authentication check unavailable — cannot run command.")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
    if result.returncode != 0:
        _error("gh CLI not authenticated. Run: gh auth login")
        raise typer.Exit(2)

    # Auto-detect repo if not set
    _detect_repo(config)
    if not config.project.repo:
        _error("Could not detect repo. Set project.repo in kew.toml")
        raise typer.Exit(2)

    # Seed the audit envelope's default repo for this process (#246)
    bind_policy_context(config)

    # Nudge when the orchestrator is behind the target repo / schema (#433)
    _emit_orchestrator_skew_warnings(config)

    # Warn when local kew.toml governance fields differ from committed config (#638)
    _emit_governance_drift_warnings(config)

    if check_runner:
        _preflight_runner(config)

    # Create directories
    Path(config.logging.dir).resolve().mkdir(parents=True, exist_ok=True)
    Path(config.branches.worktree_base).resolve().mkdir(parents=True, exist_ok=True)

    _log(f"Repo: {config.project.repo} | Timeout: {config.agent.timeout_minutes}m")


def _refuse_if_fleet_stopped(config: DispatchConfig) -> None:
    """Exit with the stop reason when the fleet kill switch is engaged.

    No --force bypass — `kew resume` is the only way back.
    """

    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        return
    with _open_run_store(str(db_path)) as store:
        latch = store.get_fleet_stopped()
    if latch is not None:
        from kew.fleet import fleet_stop_message

        _error(fleet_stop_message(latch))
        raise typer.Exit(2)


def _warn_if_verify_running(command: str) -> None:
    """A hand-started launch is never blocked by a running local merge verify
    (#3190); it says which verify it runs beside."""
    from kew.verify_lease import verify_warning

    warning = verify_warning()
    if warning is not None:
        _warn(f"kew {command}: {warning}.")


def _log_project_banner(config: DispatchConfig) -> None:
    """Render the one-line project banner (F1) before a driver/dispatch acts.

    ``project=<dir> tracker=<type> db=<path>`` so the operator sees which
    control plane they are touching — the wrong-cwd incidents (empty-graph tick,
    wrong tracker selection) came from acting on the cwd-resolved config without
    that visibility. Modelled on ``_log_budget_banner``: cosmetic, so a failure
    to render must never break the command.
    """
    try:
        cfg_path = resolved_config_path()
        project_dir = str(cfg_path.parent) if cfg_path is not None else str(Path.cwd())
        db_path = Path(config.logging.dir).resolve() / "dispatch.db"
        _log(f"project={project_dir} tracker={config.tracker.type} db={db_path}")
    except Exception:  # noqa: BLE001 — a cosmetic banner never breaks a command.
        return


def _log_budget_banner(
    config: DispatchConfig,
    *,
    store: RunStore | None = None,
    issue: int | None = None,
) -> None:
    """Render the shared effective-budget banner block (CS-7, #649).

    Replaces the old single ``Budget: $25.0`` line at every dispatch call site
    so the per-issue and daily preflight caps are no longer hidden behind the
    runtime kill cap. One shared renderer; every site calls here.
    """
    from kew.budget import effective_budget_view, render_budget_line

    try:
        view = effective_budget_view(config, store, issue)
        line = render_budget_line(view)
    except Exception:
        # A cosmetic banner must never break a dispatch command.
        return
    _log(line)


def _format_route_line(
    config: DispatchConfig,
    complexity: str | None,
    *,
    store: RunStore | None = None,
) -> str:
    """Render the resolved route with provenance for the dispatch banner (#659).

    e.g. ``Route: claude-code / claude-opus-4-8 (routing.medium, file)`` or
    ``Route: claude-code / claude-opus-4-8 (agent.model, override by tom: race)``.
    The provenance names the config field that drove the route and whether its
    value came from the file or a DB override (so a widened-allowlist override
    steering the route is visible at dispatch time).
    """
    resolved = resolve_runner(config, complexity=complexity)
    runner_name = getattr(resolved.runner, "name", "?")

    # Which field drove the route: routing.<complexity> when that entry is set,
    # else routing.default when routing is configured, else agent.model.
    if config.routing is not None:
        if complexity is not None and getattr(config.routing, complexity, None):
            field = f"routing.{complexity}"
        else:
            field = "routing.default"
    else:
        field = "agent.model"

    provenance = "file"
    if store is not None:
        try:
            override = store.get_config_override(field)
        except Exception:
            override = None
        if override is not None:
            set_by = override.get("set_by") or "?"
            reason = (override.get("reason") or "").strip()
            provenance = f"override by {set_by}"
            if reason:
                provenance += f": {reason}"

    return f"Route: {runner_name} / {resolved.model} ({field}, {provenance})"


@dataclass(frozen=True)
class _DispatchIssue:
    """A selected, validated issue ready for dispatch."""

    issue: GhIssue
    num: int
    title: str
    body: str
    labels: list[GhLabel]
    complexity: str


def _select_dispatch_issue(
    config: DispatchConfig, force_issue: int | None
) -> _DispatchIssue | DispatchCommandResult:
    """Select and validate the next issue.

    Return an issue or an internal result for empty, unknown, or malformed data.
    """
    _log("Selecting next issue...")
    try:
        issue = select_next_issue(config=config, force_issue=force_issue)
    except GhQueryError as exc:
        _error(
            "GitHub query failed — cannot tell if the queue is empty "
            f"(this is NOT an empty queue): {exception_text(exc)}"
        )
        return _DISPATCH_GH_QUERY_FAILURE
    if issue is None:
        if force_issue is not None:
            message = f"Issue #{force_issue} not found or not dispatchable."
        else:
            message = empty_queue_message(config)
        _log(message)
        return _dispatch_refusal(force_issue, message)

    raw_num = issue.get("number")
    raw_title = issue.get("title")
    if not isinstance(raw_num, int) or not isinstance(raw_title, str):
        _error(f"Malformed issue data: {issue!r}")
        return _DISPATCH_GH_QUERY_FAILURE

    issue_labels = issue.get("labels", [])
    return _DispatchIssue(
        issue=issue,
        num=raw_num,
        title=raw_title,
        body=issue.get("body") or "",
        labels=issue_labels,
        complexity=get_complexity(issue_labels),
    )


def _duplicate_branch_refusal(config: DispatchConfig, issue_num: int) -> str | None:
    """Name the remote branch that blocks a dispatch of *issue_num*, or None.

    The one seam both the real dispatch and the ``--dry-run`` preview read the
    duplicate-dispatch guard through, so the preview cannot promise a dispatch
    that this guard then refuses (#2058).
    """
    branch = f"{config.branches.prefix}{issue_num}"
    if not remote_branch_exists(branch):
        return None
    return (
        f"Skipping #{issue_num} — remote branch '{branch}' "
        f"already exists (another dispatch may be in progress)"
    )


def _preview_admission(
    config: DispatchConfig,
    issue_num: int,
    issue_labels: list[GhLabel],
    *,
    approve: bool,
) -> bool:
    """Report the admission checks the real dispatch applies (#2058).

    Applies the real dispatch's checks in the real dispatch's order — the
    duplicate-branch guard, then the classifier the claim runs — and names a
    refusal with the same words, so ``--dry-run`` can never promise a dispatch
    that the real run turns away for a missing ``ready`` label or a surviving
    remote branch. Returns True when the issue is admissible.
    """
    duplicate = _duplicate_branch_refusal(config, issue_num)
    label_names = {name for lbl in issue_labels if (name := lbl.get("name", ""))}
    verdict = classify_admission(label_names, config, approve=approve)
    if duplicate is None and verdict is ClaimResult.CLAIMED:
        return True
    _warn(f"[DRY RUN] Would NOT dispatch #{issue_num} — admission check fails:")
    if duplicate is not None:
        _warn(duplicate)
    else:
        _warn_claim_failure(verdict, issue_num, issue_labels, config)
    return False


def _dispatch_dry_run(
    config: DispatchConfig,
    *,
    issue_num: int,
    issue_title: str,
    issue_body: str,
    issue_labels: list[GhLabel],
    complexity: str,
    store: RunStore | None,
    approve: bool = False,
) -> bool:
    """--dry-run preview: report the selected issue, route, and governor reasoning.

    Returns False when the admission checks would refuse this issue — the preview
    then reports the refusal instead of a route it would never take.
    """
    if not _preview_admission(config, issue_num, issue_labels, approve=approve):
        return False
    _success(f"[DRY RUN] Would dispatch: #{issue_num} — {issue_title}")
    label_names = ", ".join(lbl.get("name", "") for lbl in issue_labels)
    _dim(f"Labels: {label_names}")
    resolved = resolve_runner(config, complexity=complexity)
    if store is None:
        return True

    import hashlib

    from kew.dispatch_core import (
        _run_governor,
        build_repo_map,
    )

    repo_map_hash = ""
    if config.prompt.include_repo_map:
        try:
            # Route through the shared seam so repo_map_mode ("stable" drops
            # the hint) applies to the preview exactly as it does to the real
            # dispatch — a direct generate_repo_map(hint=...) call here would
            # preview a per-issue hash the stable-mode dispatch never computes.
            repo_map = build_repo_map(
                config, Path.cwd(), hint=f"{issue_title} {issue_body}"
            )
            if repo_map:
                repo_map_hash = hashlib.sha256(repo_map.encode()).hexdigest()
        except Exception:  # nosec B110 — best-effort dry-run preview; on failure prefix_key degrades to passthrough
            pass

    _, gov_decision = _run_governor(
        resolved=resolved,
        complexity=complexity,
        config=config,
        store=store,
        repo_map_hash=repo_map_hash,
    )
    # No `_emit_routing_decision` here (#2302): the audit chain attests what
    # kew DID, and a dry run does nothing. The row this used to write named a
    # route for a dispatch that never happened, so the log read as a dispatch
    # history with launches in it that no run row, PR or charge corresponds to
    # — and `kew dispatch --dry-run`, the one command an operator is told is
    # safe to run as often as they like, was the thing writing them.
    from kew.runners.governor import format_governor_reasoning

    _dim(format_governor_reasoning(gov_decision))
    return True


def _warn_claim_failure(
    claim: ClaimResult,
    issue_num: int,
    issue_labels: list[GhLabel],
    config: DispatchConfig,
) -> None:
    """Warn appropriately for a failed claim (not-ready / claimed / transient)."""
    if claim is ClaimResult.NOT_READY:
        current_labels = sorted(
            lbl.get("name", "") for lbl in issue_labels if lbl.get("name")
        )
        labels_hint = (
            f" (labels: {', '.join(current_labels)})" if current_labels else ""
        )
        _warn(
            f"#{issue_num} is not labeled '{config.labels.ready}'"
            f"{labels_hint}. Add the label first."
        )
    elif claim is ClaimResult.ALREADY_CLAIMED:
        _warn(f"Skipping #{issue_num}: another dispatch already claimed it.")
    else:  # FETCH_FAILED / RACE_DETECTED — transient, not a real claim
        _warn(
            f"Cannot dispatch #{issue_num}: claim failed ({claim.value}). "
            "No agent started. "
            f"The '{config.labels.in_progress}' label may remain "
            "after verification failure. "
            "Check the issue's claim state before retrying."
        )


def _claim_for_dispatch(
    issue_num: int,
    issue_labels: list[GhLabel],
    config: DispatchConfig,
    *,
    approve: bool,
) -> ClaimResult:
    """Release any approval hold and preserve the complete claim result."""
    # --approve release path: a held issue carries the approval label
    # instead of 'ready', so restore 'ready' before the claim.
    if approve:
        held_label_names = {lbl.get("name", "") for lbl in issue_labels}
        if config.approval.label in held_label_names:
            release_approval_hold(issue_num, config)

    claim = try_claim_issue(issue_num, config)
    if not claim:
        _warn_claim_failure(claim, issue_num, issue_labels, config)
    return claim


def _note_dispatch_outcome(outcome: DispatchOutcome, store: RunStore | None) -> None:
    """Hand a dispatch's outcome to the ``--json`` summary, if one is on (#2505).

    A launched run's recorded fields speak for it; one decided before any agent
    started is a refusal, or — zero-coded — a hold such as an approval hold.
    """
    if not json_result.active():
        return
    json_result.note(run_id=outcome.run_id, store=store)
    if outcome.launched and outcome.category not in checkout_refusal_categories():
        return
    json_result.note(
        outcome="refused" if outcome.exit_code else "held",
        error_category=outcome.category,
        reason=outcome.reason,
    )


def _execute_and_capture_outcome(
    *,
    issue: GhIssue,
    config: DispatchConfig,
    store: RunStore | None,
    notifier: Notifier | None,
    force_budget: bool,
    approve: bool,
    model: str | None,
) -> DispatchCommandResult:
    """Capture the PR outcome and tag the unchanged runner exit code."""
    outcome = execute_dispatch(
        issue=issue,
        config=config,
        store=store,
        notifier=notifier,
        force_budget=force_budget,
        approved=approve,
        interactive=True,
        model_override=model,
    )
    if outcome.category in checkout_refusal_categories():
        _note_dispatch_outcome(outcome, store)
        # Exit 2 here: callers otherwise treat 2 as an empty queue and exit 0.
        raise typer.Exit(2)
    # Best-effort: capture THIS run's PR outcome immediately (fail-soft, one gh call).
    try:
        if store is not None and outcome.run_id:
            sync_outcomes(store, config, run_id=outcome.run_id)
    except Exception as exc:  # noqa: BLE001 — outcome capture must never affect dispatch
        logging.getLogger(__name__).debug(
            "post-dispatch outcome capture failed for run %s: %s",
            outcome.run_id,
            exc,
        )
    # After the outcome capture, so the summary carries the PR URL.
    _note_dispatch_outcome(outcome, store)
    if outcome.exit_code and not outcome.launched:
        # Decided before any agent started: could not run (2), and the
        # message must not claim an agent exited.
        _error(
            f"Issue #{issue['number']}: dispatch did not launch — "
            f"{outcome.category}: {outcome.reason}"
        )
        return DispatchCommandResult(
            "not_launched", outcome.exit_code, reason=outcome.reason
        )
    if outcome.exit_code:
        _error(f"Issue #{issue['number']}: agent exited with code {outcome.exit_code}")
    return DispatchCommandResult("executed", outcome.exit_code)


def _prompt_configuration_refusal(
    issue_num: int, exc: PromptConfigurationError
) -> DispatchCommandResult:
    """Refuse before the claim: the issue keeps its labels, nothing is failed."""
    reason = str(exc)
    _error(
        f"Issue #{issue_num}: dispatch did not launch — "
        f"{PROMPT_CONFIGURATION_REFUSAL}: {reason}"
    )
    json_result.note(error_category=PROMPT_CONFIGURATION_REFUSAL)
    return DispatchCommandResult("not_launched", COULD_NOT_RUN_EXIT_CODE, reason=reason)


def _dispatch_one(
    config: DispatchConfig,
    *,
    force_issue: int | None,
    dry_run: bool,
    show_prompt: bool = False,
    store: RunStore | None = None,
    notifier: Notifier | None = None,
    force_budget: bool = False,
    model: str | None = None,
    approve: bool = False,
) -> DispatchCommandResult:
    """Run a single dispatch cycle.

    Preserve raw runner codes separately from selection and claim failures.
    Translate the tagged result only at the public command boundary.
    """
    selected = _select_dispatch_issue(config, force_issue)
    if isinstance(selected, DispatchCommandResult):
        return selected
    issue = selected.issue
    issue_num = selected.num
    issue_title = selected.title
    issue_body = selected.body
    issue_labels = selected.labels
    complexity = selected.complexity

    _log(f"Selected: #{issue_num} — {issue_title} [{complexity}]")
    _log(_format_route_line(config, complexity, store=store))

    # Build prompt before any side effects (needed for both show_prompt and run)
    preview_route = resolve_runner(config, complexity=complexity)
    try:
        prompt = build_prompt(
            issue_num=issue_num,
            issue_title=issue_title,
            issue_body=issue_body,
            complexity=complexity,
            config=config,
            has_coderabbit="coderabbit" in TOOL_PATHS,
            has_repo_skills=Path(".claude/skills").is_dir(),
            publication_style=preview_route.runner.publication_style,
            profile_addendum=launch_addendum(store),
        )
    except PromptConfigurationError as exc:
        return _prompt_configuration_refusal(issue_num, exc)

    if show_prompt:
        typer.echo(prompt)
        return DispatchCommandResult("executed")

    if dry_run:
        previewed = _dispatch_dry_run(
            config,
            issue_num=issue_num,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_labels=issue_labels,
            complexity=complexity,
            store=store,
            approve=approve,
        )
        if not previewed:
            return _dispatch_refusal(force_issue)
        return DispatchCommandResult("executed")

    _preflight_runner(config, runner_name=preview_route.runner.name)

    # Guard against duplicate dispatch
    duplicate = _duplicate_branch_refusal(config, issue_num)
    if duplicate is not None:
        _warn(duplicate)
        return _dispatch_refusal(force_issue, duplicate)

    claim = _claim_for_dispatch(issue_num, issue_labels, config, approve=approve)
    if claim is not ClaimResult.CLAIMED:
        return (
            _DISPATCH_GH_QUERY_FAILURE
            if claim in (ClaimResult.FETCH_FAILED, ClaimResult.RACE_DETECTED)
            else _dispatch_refusal(force_issue, f"claim failed ({claim.value})")
        )

    return _execute_and_capture_outcome(
        issue=issue,
        config=config,
        store=store,
        notifier=notifier,
        force_budget=force_budget,
        approve=approve,
        model=model,
    )


def _required_failure_exit_code(drain: DrainResult, item: str) -> int:
    """Preserve execution evidence when a later capacity read fails."""
    if item != "Fleet worker capacity":
        return COULD_NOT_RUN_EXIT_CODE
    executed = any(
        outcome.launched for _, outcome in getattr(drain, "dispatch_outcomes", ())
    ) or any(
        outcome.kind == "executed"
        for _, outcome in getattr(drain, "resolve_outcomes", ())
    )
    return FINDINGS_EXIT_CODE if executed else COULD_NOT_RUN_EXIT_CODE


def _drain_exit_code(drain: DrainResult) -> int:
    """Summarize retained failures and translate the fleet at its CLI boundary."""
    code = COULD_NOT_RUN_EXIT_CODE if drain.claim_failures else SUCCESS_EXIT_CODE
    for number, outcome in getattr(drain, "dispatch_outcomes", ()):
        if outcome.exit_code:
            item_code = (
                FINDINGS_EXIT_CODE if outcome.launched else COULD_NOT_RUN_EXIT_CODE
            )
            state = "failed" if outcome.launched else "could not run"
            _error(
                f"Drain issue #{number}: {state}: {outcome.category} — {outcome.reason}"
            )
            code = max(code, item_code)
    for number, outcome in getattr(drain, "resolve_outcomes", ()):
        item_code = _command_exit_code(outcome)
        if item_code:
            state = "could not run" if outcome.kind == "not_run" else "failed"
            _error(
                f"Drain resolve PR #{number}: {state} "
                f"(internal code {outcome.exit_code})"
            )
            code = max(code, item_code)
    for item, reason in getattr(drain, "required_failures", ()):
        _error(f"Drain {item}: could not run: {reason}")
        code = max(code, _required_failure_exit_code(drain, item))
    return code


def _apply_workers_override(config: DispatchConfig, workers: int | None) -> None:
    """``--workers N`` sets the concurrency cap, both keys that carry it (#2398).

    ``[fleet] max_concurrent`` is what every admission check, the drain's
    capacity gate and the epic driver's dispatch budget now read
    (:meth:`~kew.config.DispatchConfig.fleet_concurrency`), and it OVERRIDES
    ``[parallel] max_workers``. So a flag that wrote only ``max_workers`` would
    be silently ignored by exactly the operator who configured a fleet cap —
    and ``--workers`` is documented as the override, not as a suggestion.

    Both are written rather than only the fleet key, so nothing reading
    ``max_workers`` directly (the attested policy snapshot, ``kew config
    effective``) reports a number the flag overrode.
    """
    if workers is None:
        return
    config.parallel.max_workers = workers
    config.fleet.max_concurrent = workers


def _run_fleet_drain(
    config: DispatchConfig,
    *,
    dry_run: bool,
    show_prompt: bool,
    workers: int | None,
) -> None:
    """--drain path: run the continuous fleet scheduler until the queue drains."""
    _log("Starting continuous dispatch drain...")
    # Re-resolve with the store so DB overrides govern the fleet drain's
    # budget preflight (R1, #902) — every pooled worker inherits this
    # config, so the override must be applied before the scheduler starts.
    loop_log_dir = Path(config.logging.dir).resolve()
    loop_log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(loop_log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        _detect_repo(config)
        _apply_workers_override(config, workers)
        _log_budget_banner(config, store=store)
        scheduler = DispatchScheduler(config=config, store=store)
        try:
            drain = scheduler.run_loop(
                force_issue=None,
                dry_run=dry_run,
                show_prompt=show_prompt,
            )
        except GhQueryError as exc:
            _error(
                "GitHub query failed — stopping the drain. The queue state is "
                f"unknown (this is NOT an empty queue): {exception_text(exc)}"
            )
            raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE) from exc
        if code := _drain_exit_code(drain):
            raise typer.Exit(code)


def _run_single_dispatch(
    config: DispatchConfig,
    *,
    dry_run: bool,
    show_prompt: bool,
    workers: int | None,
    force: bool,
    model: str | None,
) -> None:
    """Single dispatch cycle: claim the next issue, run it, surface the exit code."""
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    notifier = build_notifier(config)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        _detect_repo(config)
        _apply_workers_override(config, workers)
        _log_budget_banner(config, store=store)
        cleaned = store.cleanup_stale_pending()
        if cleaned:
            _warn(
                f"Cleaned up {cleaned} stale pending record(s) from a previous crash."
            )
        result = _dispatch_one(
            config,
            force_issue=None,
            dry_run=dry_run,
            show_prompt=show_prompt,
            store=store,
            notifier=notifier,
            force_budget=force,
            model=model,
        )
        _note_dispatch_result(result, dry_run=dry_run)
    # Public code 0 (success, or the internal empty-queue sentinel) returns
    # normally, as before; only a non-zero public code exits.
    code = _dispatch_exit_code(result)
    if code:
        raise typer.Exit(code)


def _operator_branch_head(branch: str) -> str:
    """Read *branch* from the operator repository's configured origin.

    The service layer declares this seam but may not import the host's git
    surface itself; the CLI is where the two meet (#2124).
    """
    from kew.worktree import _operator_remote_branch_head, _repo_root

    return _operator_remote_branch_head(_repo_root(), branch)


@app.command()
def finalize(
    run_id: Annotated[
        str, typer.Argument(help="Run id recorded as awaiting_publication.")
    ],
) -> None:
    """Repair a run left awaiting host publication after out-of-band publication."""
    # finalize shells out to `gh` for branch/PR evidence, and to `git` to
    # read the remote head it names as this run's published output (#2124).
    preflight_tools()
    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        _detect_repo(config)
        try:
            finalized = service_verbs.finalize_publication_run(
                store,
                run_id=run_id,
                config=config,
                remote_head=_operator_branch_head,
            )
        except ValueError as exc:
            _error(f"kew finalize: {exception_text(exc)}")
            raise typer.Exit(2) from None
        except RuntimeError as exc:
            _error(f"kew finalize: {exception_text(exc)}")
            raise typer.Exit(1) from None
    _success(f"Finalized run {finalized.run_id}: {finalized.status}.")


def _run_dispatch_command(
    *,
    drain: bool,
    dry_run: bool,
    show_prompt: bool,
    workers: int | None,
    force: bool,
    model: str | None,
) -> None:
    _emit_staleness_warnings()
    config = load_config()

    if workers is not None and workers < 1:
        _error("--workers must be at least 1")
        raise typer.Exit(2)
    _apply_workers_override(config, workers)

    # The effective route is unknown until an issue is selected. Its launch
    # boundary performs the runner check. Previews still perform no runner check.
    _preflight(config)
    _refuse_if_fleet_stopped(config)

    if drain and model is not None:
        _error("--model is not currently supported with --drain")
        raise typer.Exit(2)

    if drain:
        _run_fleet_drain(
            config,
            dry_run=dry_run,
            show_prompt=show_prompt,
            workers=workers,
        )
    else:
        _run_single_dispatch(
            config,
            dry_run=dry_run,
            show_prompt=show_prompt,
            workers=workers,
            force=force,
            model=model,
        )


@app.command("run")
def run_cmd(
    drain: Annotated[
        bool,
        typer.Option("--drain", help="Keep dispatching until the queue is empty"),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without running")
    ] = False,
    show_prompt: Annotated[
        bool, typer.Option("--show-prompt", help="Print generated prompt and exit")
    ] = False,
    workers: Annotated[
        int | None,
        typer.Option("--workers", help="Agents allowed in flight anywhere"),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force-budget", help="Bypass budget checks")
    ] = False,
    model: Annotated[
        str | None, typer.Option("--model", help="Override model for this dispatch")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print one JSON result summary on stdout; progress stays on stderr.",
        ),
    ] = False,
) -> None:
    """Take the next ready issue from the queue and dispatch the configured runner.

    With --drain, keep taking issues until the queue is empty. To dispatch one
    named issue, use `kew dispatch <issue>`.
    """
    with json_result.json_result(json_output):
        if json_output:
            _refuse_options_with_json({"--drain": drain, "--show-prompt": show_prompt})
        _run_dispatch_command(
            drain=drain,
            dry_run=dry_run,
            show_prompt=show_prompt,
            workers=workers,
            force=force,
            model=model,
        )


def _refuse_options_with_json(options: dict[str, bool]) -> None:
    """--json summarises one run; a drain or a printed prompt is not one."""
    used = [flag for flag, present in options.items() if present]
    if used:
        _error(f"{', '.join(used)} cannot be used with --json")
        raise typer.Exit(2)


def _refuse_single_options_with_loop(options: dict[str, bool]) -> None:
    """--loop supervises one issue; the single-dispatch options do not apply."""
    used = [flag for flag, present in options.items() if present]
    if used:
        _error(f"{', '.join(used)} cannot be used with --loop")
        raise typer.Exit(2)


def _dispatch_single_issue(
    config: DispatchConfig,
    issue: int,
    *,
    dry_run: bool,
    show_prompt: bool,
    force: bool,
    model: str | None,
    approve: bool,
) -> None:
    """Dispatch one named issue once and surface the public exit code."""
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    notifier = build_notifier(config)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        _detect_repo(config)
        _log_budget_banner(config, store=store, issue=issue)
        result = _dispatch_one(
            config,
            force_issue=issue,
            dry_run=dry_run,
            show_prompt=show_prompt,
            store=store,
            notifier=notifier,
            force_budget=force,
            model=model,
            approve=approve,
        )
        _note_dispatch_result(result, dry_run=dry_run)
    code = _dispatch_exit_code(result)
    if code:
        raise typer.Exit(code)


@app.command()
def dispatch(
    issue: Annotated[int, typer.Argument(help="Issue number to dispatch")],
    loop: Annotated[
        bool,
        typer.Option(
            "--loop",
            help=(
                "Pursue the issue as a first-class loop (native runners only). "
                "The loop agent runs on the host: a runner that does not contain "
                "itself needs \\[sandbox] dangerously_skip_containment = true, "
                "else the loop is refused before checkout (exit 2)."
            ),
        ),
    ] = False,
    max_steps: int | None = typer.Option(
        None, "--max-steps", help="Override the loop step cap (requires --loop)."
    ),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview without running")
    ] = False,
    show_prompt: Annotated[
        bool, typer.Option("--show-prompt", help="Print generated prompt and exit")
    ] = False,
    model: Annotated[
        str | None, typer.Option("--model", help="Override model for this dispatch")
    ] = None,
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Approve this dispatch past the approval gate"),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force-budget", help="Bypass budget checks")
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print one JSON result summary on stdout; progress stays on stderr.",
        ),
    ] = False,
) -> None:
    """Dispatch an issue. With --loop, supervise the runner as a first-class loop."""
    with json_result.json_result(json_output):
        if json_output:
            _refuse_options_with_json({"--loop": loop, "--show-prompt": show_prompt})
        _dispatch_command(
            issue,
            loop=loop,
            max_steps=max_steps,
            dry_run=dry_run,
            show_prompt=show_prompt,
            model=model,
            approve=approve,
            force=force,
        )


def _dispatch_command(
    issue: int,
    *,
    loop: bool,
    max_steps: int | None,
    dry_run: bool,
    show_prompt: bool,
    model: str | None,
    approve: bool,
    force: bool,
) -> None:
    """The body of ``kew dispatch``."""
    _emit_staleness_warnings()
    config = load_config()
    _preflight(config)
    _refuse_if_fleet_stopped(config)
    _warn_if_verify_running("dispatch")

    if max_steps is not None and not loop:
        _error("--max-steps requires --loop")
        raise typer.Exit(2)

    if not loop:
        _dispatch_single_issue(
            config,
            issue,
            dry_run=dry_run,
            show_prompt=show_prompt,
            force=force,
            model=model,
            approve=approve,
        )
        return

    _refuse_single_options_with_loop(
        {
            "--dry-run": dry_run,
            "--show-prompt": show_prompt,
            "--model": model is not None,
            "--approve": approve,
            "--force-budget": force,
        }
    )
    _run_loop_dispatch(config, issue, max_steps_override=max_steps)


def _resolve_loop_route(config: DispatchConfig, complexity: str) -> ResolvedRoute:
    """Resolve the loop's route through ``[routing]``, same as ordinary dispatch.

    Loops used to resolve ``[agent]`` directly, so a complexity-routed runner or
    model was ignored and every loop ran on the global default (#1111). The
    native-loop constraint still applies: a routed runner that cannot drive its
    own loop fails closed here, before any loop record exists.
    """
    from kew.loops.delegate import loop_route_rejection

    resolved = resolve_runner(config, complexity=complexity)
    rejection = loop_route_rejection(resolved, publish=config.publication.publish)
    if rejection is not None:
        _error(
            f"{rejection} — the [routing] entry for complexity {complexity!r} "
            "cannot be used with --loop"
        )
        raise typer.Exit(2)
    return resolved


def _run_loop_dispatch(
    config: DispatchConfig,
    issue_num: int,
    *,
    max_steps_override: int | None = None,
) -> None:
    """Build a LoopRecord and run the DelegateDriver (native runners only)."""
    from kew.loops.delegate import DelegateDriver, build_loop_from_issue

    try:
        issue = select_next_issue(config=config, force_issue=issue_num)
    except GhQueryError as exc:
        _error(f"GitHub query failed — cannot start loop for #{issue_num}: {exc}")
        raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE) from exc
    if issue is None:
        _error(f"Issue #{issue_num} could not be loaded — aborting loop")
        raise typer.Exit(2)

    raw_labels = issue.get("labels", [])
    issue_labels = (
        [lbl for lbl in raw_labels if isinstance(lbl, dict)]
        if isinstance(raw_labels, list)
        else []
    )
    complexity = get_complexity(issue_labels)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        # Resolve the route from the EFFECTIVE config (file + DB overrides), so an
        # `agent.model`/`routing.*` row raised on the /config surface governs the
        # loop's route — the O1/#892 override-parity invariant, and the #659
        # runtime-override surface #1111 asks loops to honour. Resolving before
        # this re-resolve would hand the driver the stale committed file route.
        config = load_config(store=store)
        _detect_repo(config)
        resolved = _resolve_loop_route(config, complexity)
        loop = build_loop_from_issue(
            issue,
            config,
            runner_name=resolved.runner.name,
            # Record the routed MODEL on the loop too (#1111) — runner_name alone
            # left the route unreconstructable, so resume re-resolved the default.
            model=resolved.model,
            complexity=complexity,
            max_steps_override=max_steps_override,
        )
        _log_budget_banner(config, store=store, issue=issue_num)
        loop.budget_usd = config.agent.max_budget_usd
        store.save_loop(loop)
        _log(
            f"Loop {loop.loop_id} started for #{issue_num} "
            f"({resolved.runner.name} / {resolved.model})"
        )
        # Hand the routed route to the driver — it must not re-resolve the
        # agent default underneath the routing decision made above (#1111).
        _finish_loop_dispatch(DelegateDriver(store, config=config), loop, resolved)


def _finish_loop_dispatch(
    driver: DelegateDriver, loop: LoopRecord, resolved: ResolvedRoute
) -> None:
    """Translate the loop's terminal state using its actual execution evidence."""
    from kew.worktree import CheckoutAdmissionRefusal, CheckoutBusy

    try:
        finished = driver.run(loop, route=resolved)
    except (CheckoutAdmissionRefusal, CheckoutBusy) as exc:
        _error(str(exc))
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    except (GhQueryError, OSError, RuntimeError, ValueError) as exc:
        _error(f"Loop failed: {exc}")
        raise typer.Exit(
            _loop_exception_exit_code(exc, launched=driver.launched)
        ) from exc
    _finish_loop_result(finished, launched=driver.launched)


def _loop_exception_exit_code(exc: Exception, *, launched: bool) -> int:
    """Required query failures override launch evidence at loop boundaries."""
    from kew.loops.verification import VERIFICATION_REFUSAL_PREFIX

    if (
        isinstance(exc, GhQueryError)
        or VERIFICATION_REFUSAL_PREFIX in str(exc)
        or not launched
    ):
        return COULD_NOT_RUN_EXIT_CODE
    return FINDINGS_EXIT_CODE


def _finish_loop_result(finished: LoopRecord, *, launched: bool) -> None:
    """Share terminal-state translation between dispatch and resume."""
    _raise_loop_verification_failure(finished.status_reason)
    message = f"Loop {finished.loop_id} finished: {finished.status}"
    if finished.status == "achieved":
        _success(message)
        return
    _error(f"{message}: {finished.status_reason}")
    raise typer.Exit(FINDINGS_EXIT_CODE if launched else COULD_NOT_RUN_EXIT_CODE)


def _raise_loop_verification_failure(reason: str | None) -> None:
    """Apply W4.2 to both initial and resumed verification results."""
    from kew.loops.verification import VERIFICATION_REFUSAL_PREFIX

    detail = reason or ""
    if detail.startswith(
        (VERIFICATION_REFUSAL_PREFIX, f"gate_failed: {VERIFICATION_REFUSAL_PREFIX}")
    ):
        _error(detail)
        raise typer.Exit(2)
    if detail.startswith("gate_failed:"):
        _error(detail)
        raise typer.Exit(1)


def _setup_artifacts(config: DispatchConfig) -> dict[str, str]:
    """Install every shipped artifact; a locally modified one is ``skipped``."""
    from kew.skills_catalog import LocallyModifiedError, apply_artifact, load_manifest

    artifacts: dict[str, str] = {}
    for artifact in load_manifest():
        try:
            action = apply_artifact(artifact, Path.cwd(), config)
        except LocallyModifiedError:
            artifacts[artifact.name] = "skipped"
            _warn(f"{artifact.name}: locally modified, skipped (kew skills diff)")
        else:
            artifacts[artifact.name] = action
            _log(f"skill {artifact.name}: {action}")
    return artifacts


def _setup_agents_md() -> str:
    """Write the kew block into ./AGENTS.md; ``skipped`` when it cannot."""
    from kew.agents_md import MalformedBlockError, write_agents_block

    try:
        action = write_agents_block(Path.cwd())
    except (MalformedBlockError, UnicodeDecodeError, OSError) as exc:
        _warn(f"AGENTS.md: {exc}; left untouched")
        return "skipped"
    _log(f"AGENTS.md: kew block {action}")
    return action


#: The ``schema`` field of ``kew setup --json``; bumped on a breaking change.
SETUP_SCHEMA_VERSION = 1


@app.command()
def setup(
    with_skills: Annotated[
        bool,
        typer.Option("--with-skills", help="Also install kew's shipped agent skills."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Print one JSON document of what setup did on stdout."
        ),
    ] = False,
) -> None:
    """Create labels, security hooks, and directories."""
    config = load_config()
    preflight_tools()

    # Auto-detect repo
    _detect_repo(config)
    if not config.project.repo:
        _error("Could not detect repo. Set project.repo in kew.toml")
        raise typer.Exit(2)

    _log(f"Setting up dispatch for {config.project.repo}...")

    # Create labels
    from kew.labels import SetupLabelsError, setup_labels

    try:
        result = setup_labels(config)
    except SetupLabelsError as exc:
        _error(exception_text(exc))
        raise typer.Exit(2) from exc

    # Generate security hook
    from kew.security import generate_security_hook, wire_hook_into_settings

    hook_path = Path(".claude/hooks/security-guard.sh")
    generate_security_hook(hook_path, config)
    wire_hook_into_settings(Path(".claude/settings.json"))
    agents_md = _setup_agents_md()

    artifacts = _setup_artifacts(config) if with_skills else {}
    if not with_skills:
        _log("Tip: `kew skills install --all` provisions kew's agent skills.")
    skipped_artifacts = sum(action == "skipped" for action in artifacts.values())

    # Create directories
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    worktree_dir = Path(config.branches.worktree_base).resolve()
    worktree_dir.mkdir(parents=True, exist_ok=True)

    _log(f"Log directory: {log_dir}")
    ok = not (result.failed or skipped_artifacts or agents_md == "skipped")
    if json_output:
        document = {
            "schema": SETUP_SCHEMA_VERSION,
            "repo": config.project.repo,
            "labels": result.names,
            "hook_path": str(hook_path.resolve()),
            "settings_path": str(Path(".claude/settings.json").resolve()),
            "directories": {"logs": str(log_dir), "worktrees": str(worktree_dir)},
            "artifacts": artifacts,
            "agents_md": {
                "path": str(Path("AGENTS.md").resolve()),
                "action": agents_md,
            },
            "ok": ok,
        }
        typer.echo(json.dumps(document, indent=2))
    if not ok:
        _warn(
            f"Setup findings: {result.failed} label(s) failed, "
            f"{skipped_artifacts} artifact(s) skipped, AGENTS.md {agents_md}"
        )
        raise typer.Exit(FINDINGS_EXIT_CODE)
    _success("Setup complete!")
    _log(f"Labels created on: {config.project.repo}")


def _status_analytics_breakdowns(stats: dict[str, Any]) -> None:
    """Print the per-complexity/runner/model/failure-category analytics tables."""
    by_complexity = stats["by_complexity"]
    if by_complexity:
        typer.echo("\n  By complexity:")
        for label in sorted(by_complexity):
            b = by_complexity[label]
            typer.echo(
                f"    {label:<10} {b['success']}/{b['total']}"
                f"  {b['success_rate'] * 100:.1f}%"
                f"  avg {b['avg_duration_seconds']:.0f}s"
                f"  avg ${b['avg_cost_usd']:.3f}"
            )

    by_runner = stats["by_runner"]
    if by_runner:
        typer.echo("\n  By runner:")
        for rname in sorted(by_runner):
            b = by_runner[rname]
            typer.echo(
                f"    {rname:<20} {b['success']}/{b['total']}"
                f"  {b['success_rate'] * 100:.1f}%"
                f"  avg ${b['avg_cost_usd']:.3f}"
            )

    by_model = stats["by_model"]
    if by_model:
        typer.echo("\n  By model:")
        for mname in sorted(by_model):
            b = by_model[mname]
            typer.echo(
                f"    {mname:<30} {b['success']}/{b['total']}"
                f"  {b['success_rate'] * 100:.1f}%"
                f"  avg ${b['avg_cost_usd']:.3f}"
            )

    failure_cats = stats["failure_categories"]
    if failure_cats:
        typer.echo("\n  Failure categories:")
        for cat, count in sorted(failure_cats.items(), key=lambda x: -x[1]):
            typer.echo(f"    {cat:<25} {count}")


def _status_analytics(config: DispatchConfig) -> None:
    """--analytics: aggregate dispatch stats from the local dispatch.db only.

    Reads only the local DB — no GitHub auth needed.
    """
    log_dir = Path(config.logging.dir).resolve()
    db_path = log_dir / "dispatch.db"
    if not db_path.exists():
        _warn(f"No dispatch history found at {db_path}")
        raise typer.Exit(2)
    with _open_run_store(str(db_path)) as store:
        store.cleanup_stale_pending()
        stats = store.analytics()
    overall = stats["overall"]
    total = overall["total"]
    success = overall["success"]
    rate = overall["success_rate"] * 100
    typer.echo("\nDispatch Analytics")
    if stats.get("insufficient_data"):
        typer.echo(
            "  ⚠ Insufficient data (< 5 runs) — statistics may not be meaningful"
        )
    typer.echo(f"  Overall:  {success}/{total} succeeded  ({rate:.1f}%)")
    _status_analytics_breakdowns(stats)


def _status_fleet_banner(store: SqliteRunStore) -> None:
    """Fleet kill-switch banner (omit from --json: output must stay JSON)."""
    latch = store.get_fleet_stopped()
    if latch is not None:
        from kew.fleet import fleet_stop_message

        typer.echo(f"\n*** {fleet_stop_message(latch)} ***")


def _status_export_run(r: RunRecord) -> dict[str, Any]:
    """One run record serialized for the --json ``runs`` array."""
    return {
        "run_id": r.run_id,
        "issue_num": r.issue_num,
        "branch": r.branch,
        "status": r.status,
        "exit_code": r.exit_code,
        "started_at": r.started_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_seconds": r.duration_seconds,
        "estimated_cost_usd": r.estimated_cost_usd,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
        "attempt_number": r.attempt_number,
    }


def _status_export_worker(r: RunRecord) -> dict[str, Any]:
    """One in-flight run serialized for the --json ``workers`` array.

    ``host``/``instance`` are flat rather than one rendered label (#2398), so a
    reader can group on the host without parsing a slash out of it; ``instance``
    is ``""`` for local, which is one machine and has none to name.
    """
    site = _run_site(r)
    return {
        "run_id": r.run_id,
        "issue_num": r.issue_num,
        "runner_name": r.runner_name,
        "model": r.model,
        "started_at": r.started_at.isoformat(),
        "host": site.host,
        "instance": site.instance,
    }


def _status_export_queue(queue_issues: list[GhIssue]) -> dict[str, Any]:
    """The --json ``queue`` block (count + per-issue metadata)."""
    return {
        "count": len(queue_issues),
        "issues": [
            {
                "number": i.get("number", 0),
                "title": i.get("title", ""),
                "labels": [
                    lbl["name"] for lbl in i.get("labels", []) if isinstance(lbl, dict)
                ],
                "created_at": i.get("createdAt", ""),
            }
            for i in queue_issues
        ],
    }


def _status_export_json(store: SqliteRunStore, config: DispatchConfig) -> None:
    """--json: emit the queue/workers/budget snapshot as valid JSON only."""
    import json as json_mod

    all_runs = store.list_all()
    try:
        queue_issues = list_ready_issues(config=config, strict=True)
    except (OSError, subprocess.SubprocessError, GhQueryError, ValueError):
        _status_query_unavailable()
    pending = [r for r in all_runs if r.status == "pending"]

    today = datetime.now(UTC).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_runs = [r for r in all_runs if r.started_at >= today]

    data = {
        "runs": [_status_export_run(r) for r in all_runs],
        "queue": _status_export_queue(queue_issues),
        "workers": [_status_export_worker(r) for r in pending],
        "budget": {
            "daily_spend": sum(r.estimated_cost_usd for r in today_runs),
            "daily_limit": config.budget.daily_usd,
            "weekly_limit": config.budget.weekly_usd,
            "per_issue_limit": config.budget.per_issue_usd,
        },
    }
    typer.echo(json_mod.dumps(data, indent=2))


# --------------------------------------------------------------------------- #
# status --run — the canonical work projection (I21)
#
# A run row answers exactly one question: what did this attempt do? Everything
# a person actually asks in front of it — is anything preserved, has a human
# decided, is the source item still open, how old is the evidence behind any of
# that — belongs to ``kew.service.work``. This surface READS that projection
# and renders it. It derives no state of its own and offers no action the
# projection did not stand behind, which is what stops the CLI from becoming a
# fourth opinion about the same work.
# --------------------------------------------------------------------------- #

#: How the CLI names the KIND of decision one canonical action belongs to.
#:
#: ``kew.service.work`` decides WHICH action is safe; this map only keeps the
#: kinds of next step visibly apart, so a scripted operator can never read
#: "admit this item again" where the projection said "release work that is
#: already finished". Adding an entry here can never add an action.
_ACTION_STAGE: dict[str, str] = {
    # Admitting new work — the only lane that spends a fresh attempt.
    "dispatch": "admission",
    # Independent (machine) review of an artifact that already exists.
    "fix_checks": "independent-review",
    "resolve_conflict": "independent-review",
    # A person releasing — or refusing — finished work.
    "await_human_decision": "human-release",
    "confirm_publication_risk": "human-release",
    "confirm_no_change": "human-release",
    "reject_no_change": "human-release",
    "acknowledge_orphan": "human-release",
    # Publishing the artifact a completed attempt already preserved.
    "finalize_publication": "publication-finalization",
    # The ball is back in our court on a review thread.
    "address_review_feedback": "address-feedback",
    # Everything else, named rather than folded into one of the five above.
    "synchronize_source": "source-synchronization",
    "synchronize_tracker": "source-synchronization",
    "retry_no_change_sync": "source-synchronization",
    "resume_fleet": "fleet",
    "observe_remote": "observation",
    "inspect": "observation",
    "wait": "observation",
    "none": "none",
}

#: An action this CLI has no stage for is labelled, never guessed into one of
#: the five: a wrong lane reads as authority the projection never gave.
_UNSTAGED_ACTION = "other"


def _action_stage(action: str) -> str:
    """The KIND of decision *action* is — never a decision of its own."""
    return _ACTION_STAGE.get(action, _UNSTAGED_ACTION)


def _observation_age(observed_at: str | None, composed_at: str) -> float | None:
    """Seconds between a reading and the instant the view was composed.

    ``None`` where there is no reading to age: an absent observation has no
    age, and reporting one as ``0`` would read as "just checked".
    """
    if not observed_at:
        return None
    try:
        observed = datetime.fromisoformat(observed_at)
        composed = datetime.fromisoformat(composed_at)
    except ValueError:
        return None
    return (composed - observed).total_seconds()


def _status_evidence(view: WorkViewDTO) -> list[dict[str, Any]]:
    """Every remote observation the view rests on, with its provenance and age."""
    return [
        {
            "dimension": o.dimension,
            "availability": o.availability,
            "provenance": o.provenance,
            "observed_at": o.observed_at,
            "age_seconds": _observation_age(o.observed_at, view.composed_at),
            "detail": o.detail,
        }
        for o in view.observations
    ]


def _work_healthy(view: WorkViewDTO) -> bool:
    """Whether this work is both on-normal AND currently readable.

    An unreadable forge is the case this exists for: an outage that renders as
    healthy is an outage nobody acts on, so a reading that is stale or missing
    is never enough to call the work fine.
    """
    if view.exception.state != "none":
        return False
    return all(o.availability == "available" for o in view.observations)


def _staged_action(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "stage": _action_stage(str(payload.get("action", "")))}


def _status_run_payload(
    record: RunRecord,
    view: WorkViewDTO,
    posture: SandboxPostureDTO | None = None,
) -> dict[str, Any]:
    """The machine contract for ``status --run --json``.

    The whole projection rides under ``work``; the keys beside it are the ones
    a script reaches for first (the revision it may pin, the route claim, the
    one safest action, and how old the evidence behind them is) hoisted so a
    caller does not have to walk the tree to find them.
    """
    if posture is None:
        from kew.service.models import SandboxPostureDTO

        posture = SandboxPostureDTO()
    return {
        "run": _status_export_run(record),
        "item_id": view.identity.item_id,
        "item_ref": view.identity.item_ref,
        "view_revision": view.view_revision,
        "composed_at": view.composed_at,
        "healthy": _work_healthy(view),
        "route": view.route.model_dump(),
        "safest_action": _staged_action(view.safest_action.model_dump()),
        "actions": [_staged_action(a.model_dump()) for a in view.actions],
        "evidence": _status_evidence(view),
        "sandbox": posture.model_dump(),
        "work": view.model_dump(),
    }


def _status_run_record_lines(
    record: RunRecord, view: WorkViewDTO, posture: SandboxPostureDTO
) -> None:
    """The run row itself — the item named by its canonical, opaque ref."""
    from kew.ids import item_ref_or_num

    typer.echo(f"Run:      {record.run_id}")
    typer.echo(f"Attempt:  {record.attempt_number}")
    ref = view.identity.item_ref or item_ref_or_num(record.item_id, record.issue_num)
    typer.echo(f"Item:     {ref}")
    typer.echo(f"Branch:   {record.branch}")
    typer.echo(f"Status:   {record.status}")
    typer.echo(f"Exit:     {record.exit_code}")
    typer.echo(f"Started:  {record.started_at}")
    if record.finished_at:
        typer.echo(f"Finished: {record.finished_at}")
    typer.echo(f"Duration: {record.duration_seconds:.0f}s")
    typer.echo(f"Cost:     ${record.estimated_cost_usd:.2f}")
    typer.echo(f"Tokens:   {record.input_tokens} in / {record.output_tokens} out")
    if record.error_category:
        typer.echo(f"Error:    {record.error_category}")
    from kew.service.runs import sandbox_posture_summary

    typer.echo(f"Posture:  {sandbox_posture_summary(posture)}")


def _evidence_line(row: dict[str, Any]) -> str:
    age = row["age_seconds"]
    aged = "never read" if age is None else f"{age:.0f}s old"
    return f"{row['dimension']} {row['availability']}/{row['provenance']} ({aged})"


def _work_field_lines(view: WorkViewDTO) -> list[tuple[str, str]]:
    execution, artifact = view.execution, view.artifact
    return [
        ("Revision:", view.view_revision),
        (
            "Execution:",
            f"{execution.state} ({execution.run_status or 'no attempt'}),"
            f" attempt {view.identity.attempt_number} of {execution.attempts}",
        ),
        ("Artifact:", f"{artifact.state} (publication {artifact.publication})"),
        ("Judgment:", view.judgment.state),
        ("Source:", f"{view.source.item_state} (sync {view.source.synchronization})"),
        (
            "Route:",
            f"{view.route.runner or 'unknown'} / {view.route.model or 'unknown'}"
            f" — claim {view.route.claim} ({view.route.provenance})",
        ),
        ("Exception:", f"{view.exception.state} — {view.exception.detail}"),
    ]


def _status_work_lines(view: WorkViewDTO) -> None:
    """The shared projection, rendered — one line per dimension, then the
    ONE safest action and the actions the projection actually stands behind."""
    typer.echo("")
    for label, value in _work_field_lines(view):
        typer.echo(f"{label:<10} {value}")
    for index, row in enumerate(_status_evidence(view)):
        typer.echo(f"{'Evidence:' if index == 0 else '':<10} {_evidence_line(row)}")
    safest = view.safest_action
    blocked = f" [blocked by {safest.blocked_by}]" if safest.blocked_by else ""
    typer.echo(
        f"{'Next:':<10} {_action_stage(safest.action)}: {safest.action}"
        f"{blocked} — {safest.reason}"
    )
    for index, action in enumerate(view.actions):
        offer = f"{_action_stage(action.action)}: {action.action}"
        typer.echo(
            f"{'Actions:' if index == 0 else '':<10} {offer}"
            f" -> {action.target_kind} {action.target_ref}"
        )


def _status_run_detail(
    store: SqliteRunStore,
    run_id: str,
    *,
    config: DispatchConfig,
    json_output: bool = False,
) -> None:
    """--run <id>: one run, rendered from the canonical work projection.

    Unknown ids keep their scriptable contract in both modes — a message on
    stderr and exit 2, never an empty-but-successful document.
    """
    import json as json_mod

    from kew.service.runs import select_run_detail
    from kew.service.work import pending_run_max_age_s

    detail = select_run_detail(
        store,
        run_id,
        delivery_repo=config.project.repo or None,
        # This host's in-flight bound, not the bare floor: the printed
        # ``view_revision`` is what an operator pastes into `kew work act`,
        # and a view composed under a different age is refused as stale.
        pending_max_age_s=pending_run_max_age_s(config),
    )
    record, posture, view = detail.record, detail.posture, detail.work
    if record is None or view is None:
        typer.echo(f"Run {run_id} not found", err=True)
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if json_output:
        typer.echo(json_mod.dumps(_status_run_payload(record, view, posture), indent=2))
        return
    _status_run_record_lines(record, view, posture)
    _status_work_lines(view)


def _status_query_unavailable() -> NoReturn:
    """Translate process failures without printing subprocess diagnostics."""
    _error(
        "GitHub query failed — cannot determine queue status "
        "(this is NOT an empty queue): query_unavailable."
    )
    raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE) from None


#: The most open issues one ``kew status`` count asks gh for. Without an
#: explicit ``--limit``, gh stops at 30 and every count above it read as 30.
_STATUS_COUNT_LIMIT = 1000


def _format_queue_count(count: int) -> str:
    """A count that reached the query limit is a floor, not the real number."""
    if count >= _STATUS_COUNT_LIMIT:
        return f"{_STATUS_COUNT_LIMIT}+"
    return str(count)


def _status_queue_counts(store: SqliteRunStore, config: DispatchConfig) -> None:
    """Default view: open-issue label counts plus any orphaned-PR warning."""
    repo = config.project.repo
    labels = config.labels

    def _count(label: str) -> int:
        try:
            r = run_with_backoff(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--label",
                    label,
                    "--state",
                    "open",
                    "--limit",
                    str(_STATUS_COUNT_LIMIT),
                    "--json",
                    "number",
                    "--jq",
                    "length",
                ],
                check=False,
                _run=run,
            )
        except (OSError, subprocess.SubprocessError, GhQueryError, ValueError):
            _status_query_unavailable()
        if r.returncode != 0:
            detail = (r.stderr or "").strip() or (
                f"gh exited with status {r.returncode}"
            )
            _error(
                "GitHub query failed — cannot determine queue status "
                f"(this is NOT an empty queue): {detail}"
            )
            raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE)
        try:
            return int(r.stdout.strip())
        except ValueError as exc:
            _error(
                "GitHub query failed — received an invalid queue count "
                f"for {label!r} (this is NOT an empty queue): {exception_text(exc)}"
            )
            raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE) from exc

    counts = [
        (labels.ready, _count(labels.ready)),
        (labels.in_progress, _count(labels.in_progress)),
        (labels.agent_failed, _count(labels.agent_failed)),
        (labels.pr_pending_review, _count(labels.pr_pending_review)),
        (config.approval.label, _count(config.approval.label)),
    ]

    typer.echo(f"\nQueue status for {repo}:")
    for label, count in counts:
        typer.echo(f"  {label:20s} {_format_queue_count(count)}")
    if any(count >= _STATUS_COUNT_LIMIT for _, count in counts):
        typer.echo(
            f"  ({_STATUS_COUNT_LIMIT}+ means at least {_STATUS_COUNT_LIMIT}: "
            "kew stops counting there)"
        )
    # ONE number for ONE cap (#2398). `[fleet] max_concurrent` overrides
    # `[parallel] max_workers`, so printing both would print two different
    # numbers for the same ceiling and leave the reader to guess which binds.
    typer.echo(
        f"\n  Workers configured: {config.fleet_concurrency()} "
        "(agents in flight anywhere)"
    )

    # Orphaned PRs: open agent PRs with no successful run behind them (#648).
    # Without a repository, this optional query was never attempted.
    # Keep the local view usable; attempted query failures still exit 2.
    if not repo:
        return
    orphans = detect_orphan_prs(store, config)
    if isinstance(orphans, _QueryFailure):
        _error(f"Orphan PR query failed: {orphans.category}.")
        raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE)
    if orphans:
        nums = ", ".join(f"#{o.number}" for o in orphans)
        typer.echo(f"\n  ⚠ {len(orphans)} orphaned PR(s): {nums}")
        typer.echo("    Run `kew report sync` for details — review or close them.")


def _status_recent_runs(all_runs: list[RunRecord], *, history: bool) -> None:
    """Default view: the recent-runs table (last 20 with --history, else today)."""
    if history:
        runs = all_runs[-20:]
    else:
        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        runs = [r for r in all_runs if r.started_at >= today_start][-5:]

    if not runs:
        return
    typer.echo("\nRecent runs:")
    for r in runs:
        dur = f"{r.duration_seconds / 60:.0f}m" if r.duration_seconds else "?"
        cost_str = f"${r.estimated_cost_usd:.2f}"
        retry_str = f"  (attempt {r.attempt_number})" if r.attempt_number > 1 else ""
        typer.echo(
            f"  #{r.issue_num:<5} {r.status:<8} {dur:>5}  {cost_str:>7}{retry_str}"
        )


def _status_cost_by_runner(all_runs: list[RunRecord]) -> None:
    """Per-runner cost breakdown (shown only when routing is configured)."""
    runner_counts: dict[str, int] = {}
    runner_costs: dict[str, float] = {}
    for r in all_runs:
        rname = r.runner_name or "unknown"
        runner_counts[rname] = runner_counts.get(rname, 0) + 1
        runner_costs[rname] = runner_costs.get(rname, 0.0) + r.estimated_cost_usd

    typer.echo("\nCost by runner:")
    for rname in sorted(runner_counts):
        typer.echo(
            f"  {rname:<20} {runner_counts[rname]:>3} runs"
            f"    ${runner_costs[rname]:.2f}"
        )
    total_cost = sum(runner_costs.values())
    total_runs = sum(runner_counts.values())
    typer.echo(f"  {'Total':<20} {total_runs:>3} runs    ${total_cost:.2f}")


def _status_cost_summary(all_runs: list[RunRecord], config: DispatchConfig) -> None:
    """Today/this-week cost line, budget percentage, and per-runner breakdown."""
    from datetime import timedelta

    now = datetime.now(UTC)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = start_of_today - timedelta(days=6)
    day_cost = sum(
        r.estimated_cost_usd for r in all_runs if r.started_at >= start_of_today
    )
    week_cost = sum(
        r.estimated_cost_usd for r in all_runs if r.started_at >= week_start
    )
    typer.echo(f"\nCost: ${day_cost:.2f} today | ${week_cost:.2f} this week")
    if config.budget.daily_usd > 0:
        pct = (day_cost / config.budget.daily_usd) * 100
        typer.echo(f"Budget: ${config.budget.daily_usd:.2f}/day ({pct:.1f}% used)")

    if config.routing is not None:
        _status_cost_by_runner(all_runs)


def _status_in_flight(store: SqliteRunStore) -> None:
    """WHERE each in-flight run is: its host, and the instance within it (#2398).

    Off the ledger alone, so it answers with no GitHub call and no cloud call —
    a placement is a recorded fact, not a thing to go and ask a VM about. Silent
    when nothing is in flight: an empty heading reads as a broken query.
    """
    pending = store.list_pending()
    if not pending:
        return
    typer.echo("\nIn flight:")
    for line in placement_lines(pending):
        typer.echo(line)


def _status_default(
    store: SqliteRunStore, config: DispatchConfig, *, history: bool, cost: bool
) -> None:
    """Default status view: queue counts, recent runs, and optional cost summary."""
    _status_queue_counts(store, config)
    _status_in_flight(store)
    all_runs = store.list_all()
    _status_recent_runs(all_runs, history=history)
    if cost or history:
        _status_cost_summary(all_runs, config)


@app.command()
def status(
    history: Annotated[
        bool, typer.Option("--history", help="Show recent run history")
    ] = False,
    cost: Annotated[bool, typer.Option("--cost", help="Show cost summary")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON")
    ] = False,
    run_id: Annotated[
        str | None, typer.Option("--run", help="Show details for a specific run")
    ] = None,
    analytics: Annotated[
        bool, typer.Option("--analytics", help="Show aggregate dispatch analytics")
    ] = False,
) -> None:
    """Show dispatch queue status."""
    if analytics and any((json_output, history, cost, run_id)):
        typer.echo(
            "Error: --analytics cannot be combined with --json, --history,"
            " --cost, or --run",
            err=True,
        )
        raise typer.Exit(2)

    config = load_config()

    # --analytics reads only the local dispatch.db — no GitHub auth needed
    if analytics:
        _status_analytics(config)
        return

    _preflight(config)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "dispatch.db"

    with _open_run_store(str(db_path)) as store:
        if not json_output:
            _status_fleet_banner(store)

        # --run is the narrower question, so it answers first: with an id in
        # hand the caller asked about ONE work item, and --json only chooses
        # how that answer is rendered.
        if run_id is not None:
            _status_run_detail(store, run_id, config=config, json_output=json_output)
            return

        if json_output:
            _status_export_json(store, config)
            return

        _status_default(store, config, history=history, cost=cost)


@app.command()
def review(
    pr_number: Annotated[int, typer.Argument(help="PR number to review")],
    author: Annotated[
        str | None, typer.Option("--author", help="Filter comments by author")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show comments without dispatching")
    ] = False,
    force_budget: Annotated[
        bool, typer.Option("--force-budget", help="Skip budget pre-check")
    ] = False,
    governed: Annotated[
        bool,
        typer.Option(
            "--governed",
            help="Run one governed adversarial review round on a kew-published PR",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="With --governed: review an item a running epic drives",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print one JSON result summary on stdout; progress stays on stderr.",
        ),
    ] = False,
) -> None:
    """Gather PR review comments and dispatch agent to address them."""
    with json_result.json_result(json_output):
        _review_command(
            pr_number,
            author=author,
            dry_run=dry_run,
            force_budget=force_budget,
            governed=governed,
            force=force,
        )


def _review_command(
    pr_number: int,
    *,
    author: str | None,
    dry_run: bool,
    force_budget: bool,
    governed: bool,
    force: bool,
) -> None:
    """The body of ``kew review``."""
    if governed and (author is not None or dry_run or force_budget):
        _error("--governed takes no --author, --dry-run or --force-budget.")
        raise typer.Exit(2)
    if force and not governed:
        _error("--force only applies with --governed.")
        raise typer.Exit(2)
    config = load_config()
    # dispatch_review checks the resolved runner at its launch boundary.
    _preflight(config)
    _refuse_if_fleet_stopped(config)
    _warn_if_verify_running("review")
    if governed:
        _governed_review(pr_number, config, force=force)
        return

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve with the store so DB overrides govern the budget preflight
        # (R1, #902) — the file-only config above would refuse on a stale cap.
        config = load_config(store=store)
        _detect_repo(config)
        rc = dispatch_review(
            pr_number=pr_number,
            config=config,
            author_filter=author,
            dry_run=dry_run,
            force_budget=force_budget,
            store=store,
        )
        _note_review_result(rc, store, dry_run=dry_run)
    if code := _command_exit_code(rc):
        raise typer.Exit(code)


def _governed_review(
    pr_number: int, config: DispatchConfig, *, force: bool = False
) -> None:
    """`kew review --governed`: one round of the epic review stage on a
    standalone PR (#2556); see :mod:`kew.standalone_review`."""
    from kew.config import project_root
    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.policy import repo_root
    from kew.standalone_review import (
        STANDALONE_REVIEW_EPIC_REF,
        run_standalone_review,
    )

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        _detect_repo(config)
        result = run_standalone_review(
            pr_number,
            config=config,
            store=store,
            repo=config.project.repo,
            # The reviewer checkout is cloned from the git toplevel (#1002) of
            # the project, never of the cwd, so the round reads the copy
            # `kew prompts` previews (#3108).
            repo_root=repo_root(project_root()),
            reviewer_launcher=sandbox_reviewer_launcher(config, store),
            remediator=sandbox_remediator(
                config, store, epic_ref=STANDALONE_REVIEW_EPIC_REF
            ),
            force=force,
        )
    json_result.note(
        outcome=None if result.exit_code else "success",
        reason=result.message if result.exit_code else None,
    )
    if result.exit_code:
        _error(result.message)
        raise typer.Exit(result.exit_code)
    _log(result.message)


def _note_review_result(rc: CommandOutcome, store: RunStore, *, dry_run: bool) -> None:
    """Hand a review's result to the ``--json`` summary, if one is on (#2505)."""
    if not json_result.active():
        return
    if rc.kind == "not_run":
        outcome: str | None = "refused"
    elif rc.run_id is None:
        # Nothing launched: a dry run, or a PR with no comments to address.
        outcome = "dry_run" if dry_run else ("success" if not rc.exit_code else None)
    else:
        outcome = None
    json_result.note(
        run_id=rc.run_id, store=store, outcome=outcome, reason=rc.reason or None
    )


@app.command()
def resolve(
    pr_number: Annotated[
        int, typer.Argument(help="PR number to resolve conflicts for")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check for conflicts without dispatching")
    ] = False,
    force_budget: Annotated[
        bool, typer.Option("--force-budget", help="Skip budget pre-check")
    ] = False,
) -> None:
    """Resolve merge conflicts on an agent PR branch."""
    config = load_config()
    # dispatch_resolve checks the resolved runner at its launch boundary.
    _preflight(config)
    _refuse_if_fleet_stopped(config)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve with the store so DB overrides govern the budget preflight
        # (R1, #902) — the file-only config above would refuse on a stale cap.
        config = load_config(store=store)
        _detect_repo(config)
        rc = _resolve_module.dispatch_resolve(
            pr_number=pr_number,
            config=config,
            dry_run=dry_run,
            force_budget=force_budget,
            store=store,
        )
    if code := _command_exit_code(rc):
        raise typer.Exit(code)


@app.command("ci-fix")
def ci_fix_cmd(
    run_id: Annotated[str, typer.Argument(help="GitHub Actions run ID")],
) -> None:
    """Dispatch CI failure auto-fix for a GitHub Actions run."""
    config = load_config()
    # dispatch_ci_fix checks the resolved runner at its launch boundary.
    _preflight(config)
    _refuse_if_fleet_stopped(config)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    notifier = build_notifier(config)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve with the store so DB overrides govern the budget preflight
        # (R1, #902) — the file-only config above would refuse on a stale cap.
        config = load_config(store=store)
        _detect_repo(config)
        rc = dispatch_ci_fix(
            run_id=run_id, config=config, notifier=notifier, store=store
        )
    # Exit scheme: 0 dispatched or legitimately skipped, 1 the fix dispatch
    # ran and failed, 2 a PR query or CI log prerequisite failed.
    if rc in (CI_FIX_QUERY_FAILED, CI_FIX_CANNOT_RUN):
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if rc == 1:
        raise typer.Exit(1)


@app.command()
def tui() -> None:
    """Launch the terminal dashboard."""
    config = load_config()
    _preflight(config)

    from kew.tui.app import KewApp

    # Resolve so the dashboard opens the same store regardless of cwd, matching
    # health/dispatch/status/report/ci.
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(log_dir / "dispatch.db")
    # Re-resolve with the store so DB overrides govern the action handlers'
    # budget pre-checks (R1, #902) — the file-only config above would let the
    # TUI refuse on a stale cap.
    with _open_run_store(db_path) as store:
        config = load_config(store=store)
        _detect_repo(config)
    tui_app = KewApp(
        config=config,
        db_path=db_path,
        project_root=canonical_project_root(project_root()),
    )
    tui_app.run()


@app.command()
def cleanup(
    reset_to: Annotated[
        str | None,
        typer.Option(
            "--reset-to", help="Reset orphaned issues to: ready or agent-failed"
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would change without making changes"),
    ] = False,
) -> None:
    """Find and fix orphaned label states left after a crash.

    Fetches all open issues with the agent-working label and cross-references
    them with pending run records in SQLite.  Issues with no matching pending
    run are considered orphaned and reported.  Use --reset-to to fix them.
    """
    from kew.labels import fetch_agent_working_issues

    if reset_to is not None and reset_to not in ("ready", "agent-failed"):
        _error("--reset-to must be 'ready' or 'agent-failed'")
        raise typer.Exit(2)

    config = load_config()
    _preflight(config)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "dispatch.db"

    cleaned, pending_issue_nums = _cleanup_pending_runs(db_path, dry_run=dry_run)
    if not dry_run:
        _sweep_work_trees(db_path)

    try:
        agent_working_issues = fetch_agent_working_issues(config)
    except RuntimeError as exc:
        _error(f"Failed to fetch agent-working issues: {exception_text(exc)}")
        raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE) from None
    orphaned = [
        i for i in agent_working_issues if i["number"] not in pending_issue_nums
    ]

    _report_and_reset_orphans(orphaned, config, reset_to=reset_to, dry_run=dry_run)
    if orphaned or cleaned:
        raise typer.Exit(1)


def _sweep_work_trees(db_path: Path) -> None:
    """Remove work trees past retention that no live ledger row references."""
    with _open_run_store(str(db_path)) as store:
        removed = work_dirs.sweep_work_root(store, project=project_root())
    for tree in removed:
        _log(f"Removed expired work tree {tree}")


def _cleanup_pending_runs(db_path: Path, *, dry_run: bool) -> tuple[int, set[int]]:
    """Sweep stale pending runs, then return the swept count and pending issues.

    The sweep writes run rows and retries label resets, so --dry-run only
    previews the stale rows with the same staleness rule and derives the
    pending set as if they were swept (F18). Queued stop-label resets are not
    previewed; docs/cli/cleanup.md says so.
    """
    with _open_run_store(str(db_path)) as store:
        if not dry_run:
            cleaned = store.cleanup_stale_pending()
            if cleaned:
                _warn(
                    f"Cleaned up {cleaned} stale pending record(s) "
                    "from a previous crash."
                )
            return cleaned, {r.issue_num for r in store.list_pending()}
        stale = store.list_stale_pending()
        swept = {r.run_id for r in stale}
        if stale:
            _warn(
                f"[DRY RUN] Would clean up {len(stale)} stale pending record(s) "
                "from a previous crash:"
            )
            for record in stale:
                _log(f"  {record.run_id} — #{record.issue_num}")
        pending = {r.issue_num for r in store.list_pending() if r.run_id not in swept}
        return len(stale), pending


def _report_and_reset_orphans(
    orphaned: list[dict[str, Any]],
    config: DispatchConfig,
    *,
    reset_to: str | None,
    dry_run: bool,
) -> None:
    """Report orphaned issues and, when --reset-to is given, relabel them."""
    from kew.labels import reset_orphaned_issue

    if not orphaned:
        _success("No orphaned label states found.")
        return

    label = config.labels.agent_working
    _warn(f"Found {len(orphaned)} orphaned issue(s) with '{label}' label:")
    for issue in orphaned:
        _log(f"  #{issue['number']} — {issue['title']}")

    if reset_to is None:
        _log("Use --reset-to ready or --reset-to agent-failed to fix these issues.")
        return

    if dry_run:
        _log(f"[DRY RUN] Would reset {len(orphaned)} issue(s) to '{reset_to}'")
        return

    for issue in orphaned:
        issue_num = issue["number"]
        current_labels = [
            lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)
        ]
        reset_orphaned_issue(
            issue_num, config, target=reset_to, current_labels=current_labels
        )
        _success(f"Reset #{issue_num} to '{reset_to}'")


@app.command()
def kill(run_id: Annotated[str, typer.Argument(help="Run ID to stop")]) -> None:
    """Stop one run through its authenticated process identity."""
    from kew.service.fleet import stop_run

    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        result = stop_run(store, run_id, config)
    _log(result.message)
    if result.outcome in {"unverifiable", "not_found"}:
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


@app.command()
def settle(run_id: Annotated[str, typer.Argument(help="Run ID to settle")]) -> None:
    """Reap one dead run whose container never started, and free its slot."""
    from kew.service.fleet import settle_run

    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        result = settle_run(store, run_id)
    _log(result.message)
    if result.outcome != "settled":
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


@app.command()
def stop(
    reason: Annotated[
        str, typer.Option("--reason", help="Why the fleet is being stopped")
    ] = "",
) -> None:
    """Kill all in-flight agent runs and halt dispatch until `kew resume`."""
    import kew.fleet as fleet

    config = load_config()
    _preflight(config)

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        record_current_policy(store)
        report = fleet.stop_fleet(store, config, reason=reason)

    if report.already_stopped:
        _warn(f"Fleet was already stopped at {report.stopped_at}.")
    for k in report.killed:
        note = f"  ({k.note})" if k.note else ""
        _log(
            f"  killed run {k.run_id[:8]} — issue #{k.issue_num},"
            f" spend ${k.cost_usd:.2f}{note}"
        )
    _log(f"{len(report.killed)} run(s) killed.")
    _warn("fleet stopped — run `kew resume` to re-enable dispatch")


@app.command()
def resume() -> None:
    """Clear the fleet-stop latch and re-enable dispatch."""
    import kew.fleet as fleet

    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        if fleet.resume_fleet(store, config):
            _success("Fleet resumed — dispatch re-enabled.")
        else:
            _log("Fleet is not stopped — nothing to do.")


def _attach_resolve_run(store: SqliteRunStore, issue: int) -> RunRecord:
    """Find the running dispatch for ``issue``, or exit 2 with a diagnostic."""
    pending = store.list_pending()
    active_run = next((r for r in pending if r.issue_num == issue), None)
    if active_run is None:
        typer.echo(f"No running dispatch found for issue #{issue}", err=True)
        raise typer.Exit(code=2)
    if active_run.log_file is None:
        typer.echo(f"No log file for run {active_run.run_id}", err=True)
        raise typer.Exit(code=2)
    return active_run


def _attach_emit_events(events: list[dict[str, Any]]) -> None:
    """Format and echo each event line (skipping events that render to nothing)."""
    from kew.tui.log_formatter import format_event as format_event_line

    for event in events:
        line = format_event_line(event)
        if line is not None:
            typer.echo(line)


def _attach_drain_if_ended(
    store: SqliteRunStore, active_run: RunRecord, event_path: Path, offset: int
) -> bool:
    """If the run left the pending set, drain its final events and report it ended."""
    from kew.tui.state import tail_events

    refreshed = store.list_pending()
    if any(r.run_id == active_run.run_id for r in refreshed):
        return False
    final_events, _ = tail_events(event_path, offset)
    _attach_emit_events(final_events)
    typer.echo("Run is no longer active.")
    return True


def _attach_emit_until_finished(new_events: list[dict[str, Any]]) -> bool:
    """Echo events up to and including a ``finished`` marker; True if one was seen."""
    from kew.tui.log_formatter import format_event as format_event_line

    for event in new_events:
        line = format_event_line(event)
        if line is not None:
            typer.echo(line)
        if event.get("type") == "finished":
            return True
    return False


def _attach_poll_loop(
    store: SqliteRunStore, active_run: RunRecord, event_path: Path, offset: int
) -> None:
    """Tail the event file until the run ends, times out, or the user detaches."""
    from kew.tui.state import tail_events

    start = time.monotonic()
    max_seconds = 7200  # 2 hour safety limit
    poll_count = 0
    empty_polls = 0
    try:
        while True:
            time.sleep(1)
            poll_count += 1
            if time.monotonic() - start > max_seconds:
                typer.echo("Timeout: detaching after 2 hours.", err=True)
                break
            # Re-check run status every 30s
            if poll_count % 30 == 0 and _attach_drain_if_ended(
                store, active_run, event_path, offset
            ):
                break
            new_events, offset = tail_events(event_path, offset)
            if new_events:
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls >= 60:
                    typer.echo("No events for 60s, detaching.", err=True)
                    break
            if _attach_emit_until_finished(new_events):
                return
    except KeyboardInterrupt:
        typer.echo("\nDetached.")


@app.command()
def attach(
    issue: Annotated[int, typer.Argument(help="Issue number to attach to")],
) -> None:
    """Attach to a running agent's live event stream."""
    config = load_config()
    _preflight(config)
    log_dir = Path(config.logging.dir).resolve()
    db_path = str(log_dir / "dispatch.db")

    from kew.tui.log_formatter import format_event as format_event_line
    from kew.tui.state import tail_events

    with _open_run_store(db_path) as store:
        active_run = _attach_resolve_run(store, issue)
        assert active_run.log_file is not None  # guaranteed by _attach_resolve_run

        event_path = active_run.log_file.with_suffix(".events.jsonl")
        typer.echo(
            f"Attached to #{issue} ({active_run.runner_name}/{active_run.model})"
        )

        events, offset = tail_events(event_path)
        for event in events:
            line = format_event_line(event)
            if line is not None:
                typer.echo(line)

        _attach_poll_loop(store, active_run, event_path, offset)


def _export_parse_since(since: str | None) -> datetime | None:
    """Parse the --since ISO-8601 date (UTC-normalized), or exit 1 if malformed."""
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        _error(f"Invalid --since date: {since!r} (expected ISO 8601)")
        raise typer.Exit(2) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _export_purge(store: SqliteRunStore, config: DispatchConfig) -> None:
    """--purge: delete records older than retention_days (no-op if not configured)."""
    retention = config.logging.retention_days
    if retention <= 0:
        _warn("Cannot purge: set [logging] retention_days > 0 in kew.toml first")
        return
    deleted = store.purge_older_than(retention)
    _success(f"Purged {deleted} record(s) older than {retention} days")


def _export_validate_format(fmt: str, purge: bool) -> bool:
    """Validate export options and return whether the output is an inventory."""
    inventory_format = fmt in ("model-inventory", "model-eol")
    if fmt not in ("json", "csv") and not inventory_format:
        _error("--format must be 'json', 'csv', or 'model-inventory'")
        raise typer.Exit(2)
    if inventory_format and purge:
        _error("--purge cannot be combined with model-inventory export")
        raise typer.Exit(2)
    return inventory_format


def _export_content(fmt: str, records: list[RunRecord]) -> str:
    """Render one export format without mutating the ledger."""
    if fmt in ("model-inventory", "model-eol"):
        from kew.model_inventory import build_model_inventory, render_model_inventory

        return render_model_inventory(build_model_inventory(records))
    from kew.run_store import export_runs_csv, export_runs_json

    return export_runs_json(records) if fmt == "json" else export_runs_csv(records)


def _export_records(store: SqliteRunStore, since: datetime | None) -> list[RunRecord]:
    """Read the requested export window from the ledger."""
    return store.list_since(since) if since is not None else store.list_all()


def _export_write(content: str, records: list[RunRecord], output: Path | None) -> None:
    """Write export content to a file or stdout."""
    if output is not None:
        output.write_text(content, encoding="utf-8")
        _success(f"Exported {len(records)} record(s) to {output}")
    else:
        typer.echo(content)


@app.command(name="export")
def export_cmd(
    fmt: Annotated[
        str,
        typer.Option(
            "--format", "-f", help="Export format: json, csv, or model-inventory"
        ),
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file path (default: stdout)"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only export records after this date (ISO 8601, e.g. 2025-01-01)",
        ),
    ] = None,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="After export, delete records older than retention_days "
            "(requires \\[logging] retention_days > 0 in kew.toml)",
        ),
    ] = False,
) -> None:
    """Export run history to JSON, CSV, or a model-eol inventory.

    The dispatch database lives at {logging.dir}/dispatch.db (default:
    ./logs/dispatch/dispatch.db).  Use this command to back up your run
    history or pipe it into other tools.

    Examples:

        kew export                          # JSON to stdout

        kew export -f csv -o runs.csv       # CSV to file

        kew export -f model-inventory       # model-eol inventory to stdout

        kew export --since 2025-01-01       # only recent runs

        kew export --purge                  # export then purge old records
    """
    inventory_format = _export_validate_format(fmt, purge)

    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    db_path = log_dir / "dispatch.db"

    if not db_path.exists():
        _error(f"No dispatch database found at {db_path}")
        raise typer.Exit(2)

    since_dt = _export_parse_since(since)

    with _open_run_store(str(db_path), read_only=inventory_format) as store:
        records = _export_records(store, since_dt)
        _export_write(_export_content(fmt, records), records, output)

        if purge:
            _export_purge(store, config)


def _report_write(content: str, output: Path | None, *, label: str) -> None:
    """Write ``content`` to ``output`` (or stdout), reporting the file path."""
    if output is not None:
        output.write_text(content, encoding="utf-8")
        _success(f"{label} written to {output}")
    else:
        typer.echo(content)


def _report_deliver(content: str, config: DispatchConfig) -> None:
    """--notify: POST the report to the configured webhook, or exit 2 on failure."""
    try:
        delivered = deliver_report(content, config)
    except ReportDeliveryError as exc:
        _error(f"kew report --notify: {exception_text(exc)}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    if not delivered:
        _error("kew report --notify: no [alerting] webhook_url configured")
        raise typer.Exit(2)
    _success("Report delivered to webhook")


def _require_report_repo(config: DispatchConfig) -> None:
    """Refuse report generation when repository detection produced no identity."""
    _detect_repo(config)
    if not config.project.repo:
        _error("Report repository unavailable. Set project.repo in kew.toml.")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


@report_app.callback(invoke_without_command=True)
def report(
    ctx: typer.Context,
    days: Annotated[int, typer.Option("--days", help="Reporting window in days")] = 7,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Write markdown to a file (default: stdout)"
        ),
    ] = None,
    notify: Annotated[
        bool,
        typer.Option("--notify", help="POST the report to the configured webhook"),
    ] = False,
    models: Annotated[
        bool,
        typer.Option(
            "--models",
            help="Render the per-model outcome scorecard instead of the spend report",
        ),
    ] = False,
) -> None:
    """Generate an outcome-linked spend report in markdown.

    Summarizes spend, run outcomes, and cost per merged PR over the
    reporting window (default: last 7 days). Renders even when GitHub
    is unreachable, then exits 2 if any outcome query failed.

    Examples:

        kew report                        # last 7 days to stdout

        kew report --days 30              # monthly rollup

        kew report -o spend-report.md     # write to a file
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand (e.g. `report export`) handles the request itself.
        return
    if days < 1:
        _error("--days must be at least 1")
        raise typer.Exit(2)
    # Usage validation before any env-dependent setup, so this fails with the
    # intended error rather than a missing-tool/repo error.
    if models and notify:
        _error(
            "kew report --models cannot be combined with --notify"
            " (the webhook delivers the spend report)"
        )
        raise typer.Exit(2)

    config = load_config()
    preflight_tools()
    _require_report_repo(config)

    if models:
        scorecard = build_model_scorecard_result(config=config, days=days)
        _report_write(scorecard.content, output, label="Scorecard")
        _report_availability(scorecard.sync.query_failures)
        return

    result = build_report_result(config=config, days=days)
    content = result.content

    cap_event = ingest_cap_alert(config, days=days)
    if cap_event is not None:
        build_notifier(config).notify(cap_event)

    try:
        if notify:
            _report_deliver(content, config)
        else:
            _report_write(content, output, label="Report")
    finally:
        _report_availability(result.sync.query_failures)


def _report_availability(query_failures: int) -> None:
    """Keep partial output usable while making unavailable outcomes explicit."""
    if query_failures:
        _error(f"Report outcome sync incomplete: query failures {query_failures}.")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


@report_app.command(name="export")
def report_export(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output file path for the evidence pack"),
    ],
    fmt: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: html or json"),
    ] = "html",
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Window start (ISO 8601, e.g. 2026-06-01). Default: 30 days ago.",
        ),
    ] = None,
) -> None:
    """Export a self-contained evidence pack (HTML or JSON twin).

    One HTML file (inline CSS/JS, no CDN, prints cleanly) or its
    machine-readable JSON twin, covering the economics summary, the outcome
    ledger (issue → run(s) → PR → merge state, per epic), the run-kind
    breakdown, and the per-model scorecard (runs, merge rate, spend).

    Examples:

        kew report export --output pack.html

        kew report export --output pack.json --format json

        kew report export --output pack.html --since 2026-06-01
    """
    from datetime import timedelta

    from kew.report_export import (
        build_evidence_pack,
        render_evidence_pack_html,
        render_evidence_pack_json,
    )

    if fmt not in ("html", "json"):
        _error("--format must be 'html' or 'json'")
        raise typer.Exit(2)

    now = datetime.now(UTC)
    if since is not None:
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError:
            _error(f"Invalid --since date: {since!r} (expected ISO 8601)")
            raise typer.Exit(2) from None
        since_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    else:
        since_dt = now - timedelta(days=30)

    config = load_config()
    preflight_tools()
    _require_report_repo(config)

    # Reload with the store so DB overrides govern the pack's caps (#1130).
    # Only when the DB already exists: opening it would create an empty one as
    # a side effect, defeating build_evidence_pack's missing-db empty pack (and
    # erroring outright when the logging dir does not exist yet).
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if db_path.exists():
        with _open_run_store(str(db_path)) as store:
            config = load_config(store=store)
        _require_report_repo(config)

    pack = build_evidence_pack(config=config, since=since_dt, now=now)
    if fmt == "json":
        content = render_evidence_pack_json(pack)
    else:
        content = render_evidence_pack_html(pack)

    output.write_text(content, encoding="utf-8")
    _success(f"Evidence pack ({fmt}) written to {output}")
    _report_availability(pack.sync.query_failures)


def _open_audit_store(config: DispatchConfig) -> Path:
    """Resolve the run-store path; Exit(2) if absent (a release must be audited)."""
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists() or db_path.stat().st_size == 0:
        _error(f"kew: no run store at {db_path} — cannot record the decision")
        raise typer.Exit(2)
    return db_path


def _apply_remote_decision(action: str, issue: int, reason: str) -> None:
    from kew.approval import apply_approval_decision

    config = load_config()
    preflight_tools()
    _open_audit_store(config)  # guard: Exit(2) if the dispatch DB is missing/empty
    result = apply_approval_decision(
        action, issue_num=issue, reason=reason, config=config, source="remote-comment"
    )
    if result == "unauthorized":
        _error(f"kew {action}: not authorized to {action} (see [approval] approvers)")
        raise typer.Exit(2)
    if result == "not_held":
        _warn(f"kew {action}: issue #{issue} is not held — nothing to do")
        raise typer.Exit(0)
    if result == "gh_error":
        _error(f"kew {action}: couldn't reach GitHub — issue #{issue} unchanged")
        raise typer.Exit(GH_QUERY_FAILURE_EXIT_CODE)
    _success(f"kew {action}: issue #{issue} {result}")


@app.command()
def approve(
    issue: int = typer.Argument(..., help="Issue number to approve"),
    reason: str = typer.Option("", "--reason", help="Optional reason (audited)"),
) -> None:
    """Release a needs-approval hold (records approval_granted)."""
    _apply_remote_decision("approve", issue, reason)


@app.command()
def deny(
    issue: int = typer.Argument(..., help="Issue number to deny"),
    reason: str = typer.Option("", "--reason", help="Optional reason (audited)"),
) -> None:
    """Deny a needs-approval hold (records approval_declined; parks the issue)."""
    _apply_remote_decision("deny", issue, reason)


@app.command()
def health(
    window: int = typer.Option(7, "--window", help="Retrospective window in days"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON"),
) -> None:
    """Attest the control plane is functioning: meter, budgets, audit, runner."""

    if window < 1:
        _error("kew health: --window must be at least 1 day")
        raise typer.Exit(2)

    config = load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    # A missing/empty run store is unhealthy for a gate — don't let SqliteRunStore
    # silently auto-create an empty db and report green (#247 adversarial C2).
    if not db_path.exists() or db_path.stat().st_size == 0:
        _error(f"kew health: no run store at {db_path} — has kew dispatched here?")
        raise typer.Exit(2)
    try:
        with _open_run_store(str(db_path)) as store:
            config = load_config(store=store)
            switchyard_probe, proxy_observation = _switchyard_diagnostics(config)
            report = run_health_checks(
                store,
                config,
                window_days=window,
                switchyard_probe=switchyard_probe,
            )
            report = _with_proxy_observation(report, proxy_observation)
    except Exception as exc:
        _error(f"kew health: cannot read run store at {db_path}: {exception_text(exc)}")
        raise typer.Exit(2) from None
    _render_health(report, window=window, json_output=json_output)
    raise typer.Exit(report.exit_code)


def _switchyard_diagnostics(
    config: DispatchConfig,
) -> tuple[SwitchyardProbeResult | None, ProxyMetricsObservation | None]:
    """Collect bounded endpoint and metrics diagnostics only in proxy mode."""
    if config.inference_transport.mode == "direct":
        return None, None
    settings = config.inference_transport.switchyard
    with ThreadPoolExecutor(max_workers=2) as executor:
        probe_future = executor.submit(
            probe_switchyard_endpoint,
            endpoint=settings.endpoint,
            route=settings.route,
            timeout_seconds=5.0,
        )
        observation_future = executor.submit(
            observe_switchyard_metrics,
            endpoint=settings.endpoint,
            timeout_seconds=5.0,
        )
        probe = probe_future.result()
        try:
            observation = observation_future.result()
        except SwitchyardMetricsError as exc:
            observation = ProxyMetricsObservation.unavailable(exc.reason)
    return probe, observation


def _with_proxy_observation(
    report: HealthReport, observation: ProxyMetricsObservation | None
) -> HealthReport:
    """Attach the observation without changing direct-mode health JSON."""
    if observation is None:
        return report
    checks = [
        CheckResult(
            name=check.name,
            status=check.status,
            summary=check.summary,
            detail={**check.detail, "proxy_observation": observation.to_json()},
        )
        if check.name == "switchyard"
        else check
        for check in report.checks
    ]
    return HealthReport(checks=checks)


def _render_health(report: HealthReport, *, window: int, json_output: bool) -> None:
    if json_output:
        import json

        print(
            json.dumps(
                {
                    "healthy": report.healthy,
                    "exit_code": report.exit_code,
                    "window_days": window,
                    "checks": [
                        {
                            "name": c.name,
                            "status": c.status,
                            "summary": c.summary,
                            "detail": c.detail,
                        }
                        for c in report.checks
                    ],
                },
                indent=2,
            )
        )
        return
    from rich.console import Console
    from rich.table import Table

    _ICON = {
        "pass": f"[{ATTESTED}]PASS[/]",  # nosec B105 — status label, not a password
        "warn": f"[{WARN}]WARN[/]",
        "fail": f"[{JUDGMENT}]FAIL[/]",
        "skip": "[dim]SKIP[/]",
    }
    table = Table(title=f"kew health (window {window}d)")
    table.add_column("check")
    table.add_column("status")
    table.add_column("summary")
    for c in report.checks:
        table.add_row(c.name, _ICON[c.status], c.summary)
    Console().print(table)


_CANARY_PROMPT = "Reply with exactly: OK and nothing else."
_CANARY_RETRIES = 3
_CANARY_TIMEOUT_SECS = 180  # hard wall-clock per attempt — a trivial prompt is seconds
# Per-runner model when [agent] model is unset. claude omits --model (its own
# default); opencode REQUIRES a model to run, so give it a concrete default.
_CANARY_DEFAULT_MODELS: dict[str, str] = {
    "claude-code": "",
    "opencode": "openrouter/deepseek/deepseek-v4-flash",
}


def _canary_model(name: str, config: DispatchConfig) -> str:
    """Resolve model for a canary leg, preferring [routing] over global agent.model.

    For the opencode runner, reads the model from ``routing.simple`` when it's
    a structured ``RouteEntry`` with an explicit model — matching what opencode
    actually uses in production. Falls back to the per-runner default when the
    routing entry is absent or has no model.
    """
    if name == "opencode" and config.routing is not None:
        simple = config.routing.simple
        if isinstance(simple, RouteEntry) and simple.model:
            return simple.model
    if name == "opencode":
        return _CANARY_DEFAULT_MODELS[name]
    return config.agent.model or _CANARY_DEFAULT_MODELS[name]


def _canary_runner(name: str) -> AgentRunner:
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.codex import CodexRunner
    from kew.runners.opencode import OpenCodeRunner

    factories: dict[str, type[AgentRunner]] = {
        "claude-code": ClaudeCodeRunner,
        "codex": CodexRunner,
        "opencode": OpenCodeRunner,
    }
    return factories[name]()


def _run_one_canary(
    runner: AgentRunner,
    model: str,
    config: DispatchConfig,
    *,
    record_dir: str | None,
) -> tuple[RunnerOutcome, bool]:
    """One live attempt. Returns (outcome, couldnt_run)."""
    import contextlib
    import os
    import shutil
    import signal
    import subprocess

    if runner.name == "codex":
        from kew.canary_live import codex_reachability_reason

        reach = inspect_runner(config, runner_name="codex")
        reason = codex_reachability_reason(reach)
        if reason is not None:
            return RunnerOutcome("codex", False, [], reason), True
        from kew.runners.codex import codex_process_env

        env = codex_process_env()
    else:
        env = dict(os.environ)
    try:
        cmd = runner.build_command(
            prompt=_CANARY_PROMPT, config=config, env=env, model=model
        )
    except (ValueError, KeyError, OSError) as exc:
        return RunnerOutcome(
            runner.name, False, [], f"build_command failed: {exception_text(exc)}"
        ), True

    tmp = str(work_dirs.make_work_dir("kew-canary-"))
    try:
        if runner.name == "codex":
            git_init = run(["git", "init"], cwd=tmp, check=False)
            if git_init.returncode != 0:
                return RunnerOutcome("codex", False, [], "git init failed"), True
            from kew.runners.codex import validate_codex_project_tree

            validate_codex_project_tree(Path(tmp))
        # Own process group (start_new_session) so a hung child tree can be killed
        # as a group on the deadline — a stuck CLI must not hang the nightly forever.
        try:
            proc = popen(cmd, cwd=tmp, env=env, start_new_session=True)
        except (ValueError, OSError) as exc:  # unresolved binary / launch failure
            return (
                RunnerOutcome(
                    runner.name, False, [], f"could not launch: {exception_text(exc)}"
                ),
                True,
            )

        try:
            stdout, _ = proc.communicate(timeout=_CANARY_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return (
                RunnerOutcome(
                    runner.name,
                    False,
                    [],
                    f"timed out after {_CANARY_TIMEOUT_SECS}s",
                ),
                True,
            )

        assert proc.returncode is not None  # set once communicate() returns
        return _canary_evaluate(
            runner, model, config, proc.returncode, stdout, record_dir=record_dir
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _canary_evaluate(
    runner: AgentRunner,
    model: str,
    config: DispatchConfig,
    exit_code: int,
    stdout: str | None,
    *,
    record_dir: str | None,
) -> tuple[RunnerOutcome, bool]:
    """Parse a completed canary run into an outcome (and couldn't-run flag)."""
    from kew.canary import CONTRACTS, canary_checks, reduce_run

    raw_lines = [ln.rstrip("\n") for ln in (stdout or "").splitlines()]
    events = [ev for line in raw_lines for ev in runner.parse_line(line)]
    last_output = next((ln for ln in reversed(raw_lines) if ln.strip()), None)
    classification = runner.classify_result(
        exit_code=exit_code, last_output=last_output, config=config
    )

    _canary_record(record_dir, runner.name, raw_lines)

    fields = reduce_run(events, classification)
    # couldn't-run: a hard failure or a transient blip that produced nothing.
    if _canary_couldnt_run(exit_code, classification, fields):
        reason = f"exit={exit_code} category={classification.category!r} no governance"
        return RunnerOutcome(runner.name, False, [], reason), True
    from kew.pricing import build_pricing_overrides

    checks = canary_checks(
        fields,
        CONTRACTS[runner.name],
        model=model,
        overrides=build_pricing_overrides(config.pricing.overrides),
    )
    if runner.name == "codex":
        from kew.canary_live import delete_codex_session

        checks.append(delete_codex_session(fields.session_id))
    return RunnerOutcome(runner.name, True, checks, ""), False


def _canary_record(
    record_dir: str | None, runner_name: str, raw_lines: list[str]
) -> None:
    """Persist a canary run's raw output lines to ``record_dir`` when requested."""
    if record_dir is None:
        return
    from pathlib import Path as _Path

    _Path(record_dir).mkdir(parents=True, exist_ok=True)
    (_Path(record_dir) / f"{runner_name}.jsonl").write_text("\n".join(raw_lines) + "\n")


def _canary_couldnt_run(
    exit_code: int,
    classification: ResultClassification,
    fields: GovernanceFields,
) -> bool:
    """True when the run hard-failed or transiently produced no governance signal."""
    no_governance = fields.input_tokens == 0 and fields.output_tokens == 0
    if exit_code == 0:
        no_governance = no_governance and not fields.saw_result_event
    return (exit_code != 0 or classification.category == "transient") and no_governance


def _run_with_retries(
    runner: AgentRunner,
    model: str,
    config: DispatchConfig,
    *,
    record_dir: str | None,
    sleep: Callable[[float], None] | None = None,
) -> RunnerOutcome:

    do_sleep = sleep or time.sleep
    outcome, _ = _run_one_canary(runner, model, config, record_dir=record_dir)
    attempt = 0
    while attempt < _CANARY_RETRIES - 1 and not outcome.ran:
        do_sleep(2.0 * (attempt + 1))
        outcome, _ = _run_one_canary(runner, model, config, record_dir=record_dir)
        attempt += 1
    return outcome


def _runner_status(o: RunnerOutcome) -> str:
    if not o.ran:
        return "skip"
    if any(c.status == "fail" for c in o.checks):
        return "fail"
    return "pass"


@app.command()
def canary(
    runners: Annotated[
        list[str] | None,
        typer.Option(
            "--runner",
            help=(
                "Runner to check (repeatable): claude-code, codex, opencode."
                " Default: claude-code and opencode."
            ),
        ),
    ] = None,
    json_output: bool = typer.Option(
        False, "--json", help="Machine-readable JSON to stdout only"
    ),
    record: str = typer.Option(
        "", "--record", help="Write each runner's raw stream to DIR/<runner>.jsonl"
    ),
    model: str = typer.Option(
        "", "--model", help="Model override (valid with exactly one runner)"
    ),
    exercise_resume: bool = typer.Option(
        False,
        "--exercise-resume",
        help="Interrupt and resume one resumable runner",
    ),
) -> None:
    """Attest kew still reads fresh vendor CLI output correctly."""
    from kew.canary import CONTRACTS

    names = runners or ["claude-code", "opencode"]
    _validate_canary_request(names, model, exercise_resume, CONTRACTS)
    config = load_config()
    preflight_tools()  # register runner binaries in the subprocess allowlist
    record_dir = record or None
    outcomes = [
        _run_requested_canary(
            name,
            config,
            model_override=model,
            exercise_resume=exercise_resume,
            record_dir=record_dir,
        )
        for name in names
    ]
    report = CanaryReport(outcomes)
    _render_canary(report, json_output=json_output)
    raise typer.Exit(report.exit_code)


def _validate_canary_request(
    names: list[str],
    model: str,
    exercise_resume: bool,
    contracts: dict[str, Any],
) -> None:
    unknown = [name for name in names if name not in contracts]
    if unknown:
        _error(
            f"kew canary: unsupported runner {unknown[0]!r} "
            f"(v1: {', '.join(contracts)})"
        )
        raise typer.Exit(2)
    if model and len(names) != 1:
        _error("--model requires exactly one runner")
        raise typer.Exit(2)
    if exercise_resume and (len(names) != 1 or names[0] != "codex"):
        _error("--exercise-resume requires exactly one resumable runner")
        raise typer.Exit(2)


def _resolved_canary_model(
    name: str, config: DispatchConfig, model_override: str
) -> str:
    if name != "codex" or model_override:
        return model_override or _canary_model(name, config)
    from kew.runners.registry import resolve_runner

    route = resolve_runner(config, "simple")
    if route.runner.name != "codex" or not route.model:
        _error("Codex canary requires a simple Codex route or --model")
        raise typer.Exit(2)
    return route.model


def _run_requested_canary(
    name: str,
    config: DispatchConfig,
    *,
    model_override: str,
    exercise_resume: bool,
    record_dir: str | None,
) -> RunnerOutcome:
    if config.inference_transport.mode == "switchyard":
        if exercise_resume:
            _error(
                "--exercise-resume is not valid in Switchyard mode; "
                "passthrough always exercises resume"
            )
            raise typer.Exit(2)
        from kew.canary_live import run_switchyard_passthrough_canary

        selected_runner = _canary_runner(name)
        requested_model = (
            model_override
            or config.agent.model
            or config.inference_transport.switchyard.route
        )
        return run_switchyard_passthrough_canary(
            selected_runner,
            config,
            requested_model,
            record_dir=record_dir,
        )
    effective_model = _resolved_canary_model(name, config, model_override)
    if name == "codex":
        from kew.pricing import build_pricing_overrides, is_priced_model

        overrides = build_pricing_overrides(config.pricing.overrides)
        if not is_priced_model(effective_model, overrides):
            _error(f"Canary model {effective_model!r} has no governed pricing")
            raise typer.Exit(2)
    if exercise_resume:
        if name != "codex":
            _error("--exercise-resume requires exactly one resumable runner")
            raise typer.Exit(2)
        from kew.canary_live import run_codex_resume_canary

        return run_codex_resume_canary(config, effective_model, record_dir=record_dir)
    return _run_with_retries(
        _canary_runner(name), effective_model, config, record_dir=record_dir
    )


def _render_canary(report: CanaryReport, *, json_output: bool) -> None:
    if json_output:
        import json

        print(
            json.dumps(
                {
                    "exit_code": report.exit_code,
                    "runners": [
                        {
                            "runner": o.runner,
                            "status": _runner_status(o),
                            "reason": o.reason,
                            "checks": [
                                {
                                    "name": c.name,
                                    "status": c.status,
                                    "summary": c.summary,
                                    "detail": c.detail,
                                }
                                for c in o.checks
                            ],
                        }
                        for o in report.outcomes
                    ],
                },
                indent=2,
            )
        )
        return
    from rich.console import Console
    from rich.table import Table

    icon = {
        "pass": f"[{ATTESTED}]PASS[/]",  # nosec B105 — status label, not a password
        "warn": f"[{WARN}]WARN[/]",
        "fail": f"[{JUDGMENT}]FAIL[/]",
        "skip": "[dim]SKIP[/]",
    }
    table = Table(title="kew canary")
    for col in ("runner", "check", "status", "summary"):
        table.add_column(col)
    for o in report.outcomes:
        if not o.ran:
            table.add_row(o.runner, "—", "[dim]SKIP[/]", o.reason)
            continue
        for c in o.checks:
            table.add_row(o.runner, c.name, icon[c.status], c.summary)
    Console().print(table)


@report_app.command(name="sync")
def report_sync() -> None:
    """Backfill PR outcomes (merged/closed, reviews, diff stats) for past runs."""
    config = load_config()
    preflight_tools()  # REQUIRED: sync shells out to `gh` — else gh isn't on the
    #                    subprocess allowlist and every fetch fail-skips (silent no-op).
    _detect_repo(config)  # parity with report/dispatch: without a repo,
    #                       sync_outcomes has no work and would silently no-op.
    if not config.project.repo:
        _error("Sync repository unavailable. Set project.repo in kew.toml.")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _open_run_store(str(log_dir / "dispatch.db")) as store:
        result = sync_outcomes(store, config)
        # Additive orphan-reconciliation pass (#648): flag open agent PRs with
        # no successful run behind them, comment once each, never auto-close.
        reconciliation = reconcile_orphan_prs(store, config)
    orphan_failures = _report_orphan_reconciliation(reconciliation)
    query_failures = result.query_failures + orphan_failures
    summary = (
        f"synced {result.synced}, no PR {result.no_pr}, query failures {query_failures}"
    )
    (_warn if query_failures else _success)(summary)
    if query_failures:
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


@report_app.command(name="provenance")
def report_provenance(
    features: Annotated[
        Path,
        typer.Option(
            "--features",
            help="TOML file listing each console feature and the work that built it",
        ),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out", help="Directory to write provenance.json and runs.json into"
        ),
    ],
    corpus: Annotated[
        Path | None,
        typer.Option(
            "--corpus",
            help="Run-report corpus directory (default: the run_reports sink)",
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="OWNER/NAME whose runs to export (default: project.repo)",
        ),
    ] = None,
) -> None:
    """Write each console feature's build history, and a cleaned run export.

    Examples:

        kew report provenance --features frontend/demo/features.toml \\
            --out frontend/demo/provenance --repo OWNER/NAME
    """
    from kew.report_provenance import (
        FeaturesError,
        build_provenance,
        load_features,
        read_corpus_flags,
    )

    try:
        feature_list = load_features(features)
    except FeaturesError as exc:
        _error(f"kew report provenance: {exc}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
    if corpus is not None and not corpus.is_dir():
        _error(f"kew report provenance: --corpus {corpus} is not a directory")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)

    config = load_config()
    project_repo = _provenance_repo(repo or config.project.repo)
    corpus_dir = corpus or Path(config.run_reports.sink_path).expanduser()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        _error(f"kew report provenance: no run store at {db_path}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)

    flags = read_corpus_flags(corpus_dir) if corpus_dir.is_dir() else None
    with _open_run_store(str(db_path), read_only=True) as store:
        export = build_provenance(store, feature_list, flags, repo=project_repo)
    _warn_provenance_notes(export, project_repo, corpus_dir if flags is None else None)
    _write_provenance_files(export, out)


def _warn_provenance_notes(
    export: ProvenanceExport, repo: str, missing_corpus: Path | None
) -> None:
    """Each stderr note of one export; *missing_corpus*: the corpus not found."""
    if not export.repo_matched:
        _warn(f"no run in the ledger is recorded against {repo}")
    for note in export.notes:
        _warn(note)
    if missing_corpus is not None:
        _warn(f"no run-report corpus at {missing_corpus}: integrity flag counts are 0")


def _provenance_repo(repo: str) -> str:
    """The repo ``report provenance`` scopes to; exits 2 when there is none."""
    if not repo.strip():
        _error(
            "kew report provenance: no repo to export. "
            "Set project.repo in kew.toml or pass --repo OWNER/NAME."
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    return repo.strip()


def _write_provenance_files(export: ProvenanceExport, out: Path) -> None:
    """Write both provenance files, or exit 2 having written neither."""
    from kew.report_provenance import write_provenance

    try:
        provenance_path, runs_path = write_provenance(export, out)
    except OSError as exc:
        _error(f"kew report provenance: cannot write {out}: {exc.strerror or exc}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from None
    _success(f"Provenance written to {provenance_path} and {runs_path}")


def _report_orphan_reconciliation(
    result: OrphanReconciliationResult | _QueryFailure,
) -> int:
    """Report confirmed progress and return the required remote failure count."""
    if isinstance(result, _QueryFailure):
        _warn(f"Orphan PR query failed: {result.category}.")
        return 1
    if result.orphans:
        nums = ", ".join(f"#{o.number}" for o in result.orphans)
        _warn(f"⚠ {len(result.orphans)} orphaned PR(s): {nums}")
        _log("  Opened by killed runs with no successful record — review or close.")
        _log(
            f"  warnings posted {len(result.warnings_posted)}, "
            f"already warned {len(result.already_warned)}, "
            f"comment-read failures {len(result.comment_read_failures)}, "
            f"comment-write failures {len(result.comment_write_failures)}"
        )
    for phase, failures in (
        ("read", result.comment_read_failures),
        ("write", result.comment_write_failures),
    ):
        for failure in failures:
            _warn(
                f"Orphan PR #{failure.pr_number} comment-{phase} failed: "
                f"{failure.category}."
            )
    return result.query_failures


def _report_selections(candidate: VerifyCandidate) -> None:
    """Which suite ran and why, beside the verdict: a selected verify did not
    apply the coverage floor, and a fallback says what put it in doubt. Then
    what of vitest and ui-check ran, and why (#3213)."""
    if candidate.suite.tiered:
        typer.echo(f"kew verify: {candidate.suite.describe()}")
    if candidate.frontend.scoped_setting:
        typer.echo(f"kew verify: {candidate.frontend.describe()}")


def _report_verify(
    candidate: VerifyCandidate,
    result: VerifyResult,
    *,
    claim: HeldClaim | None = None,
) -> None:
    """Print what the host returned, and exit with its verdict.

    A merge claim that lapsed mid-verify (#3198) is said out loud whatever
    the verdict, and changes no exit code: the gate's answer stands, but the
    head it answered for may no longer be the PR's.
    """
    from kew.merge_verify import summary_line

    if result.log:
        typer.echo(result.log)
    for report in result.shards:
        # Each shard's own exit code, selected count and seconds: a sharded
        # verification that reported only its verdict could not say which host
        # failed, or which one never answered at all.
        typer.echo(report.line)
    if claim is not None and claim.lapsed():
        _warn(
            f"kew verify: the merge claim on PR #{claim.pr} lapsed mid-verify "
            "(a renewal was refused, or the machine slept past its "
            "15-minute life): the drive may have pushed onto the head since — "
            "check the PR head is still the one verified before merging"
        )
    line = summary_line(candidate, result)
    _report_selections(candidate)
    _report_main_red(result)
    exit_code = result.exit_code
    if exit_code is not None and result.error:
        # The gate answered, and something around it did not — a host that may
        # still be running and billing. The verdict stands; the warning is the
        # operator's to act on.
        _warn(f"kew verify: {result.error}")
    if exit_code == 0:
        _success(f"kew verify {line}")
        return
    _error(f"kew verify {line}")
    if exit_code is None:
        # No exit code came back, so the gate's verdict is unknown rather than
        # negative: say so, and exit with "could not run" instead of a 1.
        _error(f"kew verify: the job returned no exit code: {result.error}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    # The host's own exit code, verbatim: a drive script replacing
    # verify_remote.sh with `kew verify` reads the gate's status, not kew's.
    raise typer.Exit(exit_code)


#: The options only a verification run reads, which a selection question ignores.
_VERIFY_RUN_OPTIONS = {
    "host": "--host",
    "shards": "--shards",
    "pr_body": "--pr-body",
    "item": "--item",
    "epic": "--epic",
    "pr": "--pr",
    "force_claim": "--force-claim",
    "force_claim_reason": "--force-claim-reason",
    "full": "--full",
}


def _given(ctx: typer.Context, name: str) -> bool:
    source = ctx.get_parameter_source(name)
    return source is not None and source.name != "DEFAULT"


def _selection_flag_conflict(
    ctx: typer.Context, *, explain: bool, report: bool, frontend_report: bool = False
) -> str | None:
    """Why the selection options given cannot all mean something, or None."""
    asked = [
        flag
        for flag, given in (
            ("--explain-selection", explain),
            ("--selection-report", report),
            ("--frontend-report", frontend_report),
        )
        if given
    ]
    if len(asked) > 1:
        return f"{' and '.join(asked)} ask different questions; pick one"
    if not asked:
        if _given(ctx, "last"):
            return (
                "--last is the number of commits --selection-report or "
                "--frontend-report replays; add one of them"
            )
        return None
    ignored = _ignored_options(ctx, explain=explain)
    if not ignored:
        return None
    return f"{asked[0]} runs nothing, so it would ignore {', '.join(ignored)}"


def _ignored_options(ctx: typer.Context, *, explain: bool) -> list[str]:
    """The options given that the selection question asked would ignore."""
    ignored = [flag for name, flag in _VERIFY_RUN_OPTIONS.items() if _given(ctx, name)]
    # --explain-selection reads REF and not --last; --selection-report the reverse.
    name, flag = ("last", "--last") if explain else ("ref", "REF")
    if _given(ctx, name):
        ignored.append(flag)
    return ignored


def _explain_verify_selection(ref: str, *, report: bool, last: int) -> None:
    """``kew verify --explain-selection`` / ``--selection-report``: read-only.

    Reads git objects only: the merge gate paths come from the candidate's
    committed ``kew.toml``, never the working tree's.
    """
    from kew import verify_selection, verify_selection_report

    preflight_tools(required=("git",))
    root = project_root()
    try:
        gate_re = verify_selection.gate_pattern()
        if report:
            commits = verify_selection_report.merged_commits(root, last)
            text = verify_selection_report.sizing_report(root, commits, gate_re=gate_re)
            typer.echo(text, nl=False)
            return
        selection = verify_selection.explain(root, ref, gate_re=gate_re)
    except verify_selection.SelectionError as exc:
        _error(f"kew verify: {exception_text(exc)}")
        raise typer.Exit(2) from None
    typer.echo(selection.render(), nl=False)


def _answer_selection_question(
    ref: str, *, explain: bool, report: bool, frontend: bool, last: int
) -> bool:
    """Answer the read-only question asked, if one was: a frontend report, or
    a test selection. Whether one was asked, and so nothing is to run."""
    if frontend:
        _frontend_report(last=last)
    elif explain or report:
        _explain_verify_selection(ref, report=report, last=last)
    else:
        return False
    return True


def _frontend_report(*, last: int) -> None:
    """``kew verify --frontend-report``: read-only, from git objects (#3213)."""
    from kew import verify_frontend, verify_selection

    preflight_tools(required=("git",))
    root = project_root()
    try:
        # Each change is replayed against main's ownership map as it is today.
        tip = verify_frontend.main_tip(root)
        commits = verify_frontend.frontend_commits(root, last, tip)
        text = verify_frontend.sizing_report(root, commits, map_rev=tip)
    except verify_selection.SelectionError as exc:
        _error(f"kew verify: {exception_text(exc)}")
        raise typer.Exit(2) from None
    typer.echo(text, nl=False)


def _item_pr_for_claim(store: SqliteRunStore, item: str | None) -> int | None:
    """The PR ``--item`` claims, or None (said out loud) when it has none."""
    from kew.merge_claim import item_pr

    if item is None:
        return None
    found = item_pr(store, item)
    if found is None:
        _warn(
            f"kew verify: no merge claim taken — the ledger links item {item} "
            "to no single open PR; pass --pr to claim its head"
        )
    return found


def _claimed_head_text(heads: tuple[str, ...], known: frozenset[str]) -> str:
    """The PR head a claim on *heads* protects, as the claim line names it.

    The head the drive last read for the PR when it is one of them; else the
    one commit, or — for a merge the ledger cannot place — both its parents,
    one of which is the PR head.
    """
    found = [head for head in heads if head in known]
    if found:
        return found[0][:12]
    if len(heads) == 1:
        return heads[0][:12]
    return (
        f"{heads[1][:12]} or {heads[2][:12]} (whichever is its head: "
        f"{heads[0][:12]} is a merge)"
    )


def _force_claim_reason(force: bool, reason: str | None) -> str | None:
    """The attested reason a forced claim carries; the two flags go together."""
    text = (reason or "").strip()
    if force and text:
        return text
    if not force and reason is None:
        return None
    _error(
        "kew verify: --force-claim and --force-claim-reason go together: a "
        "forced claim is attested with why it was forced"
    )
    raise typer.Exit(2)


def _busy_runs_refusal(
    store: SqliteRunStore,
    pr: int,
    items: frozenset[str],
    *,
    force_reason: str | None,
) -> list[BusyRun]:
    """The runs a forced claim overrides; refuse the claim unless forced.

    A remediation, conflict resolution or reviewer holding the node's review
    reservation, or a ci-fix still in flight, was launched before this claim
    existed, and all but the reviewer push onto the head when they finish.
    ``--force-claim`` with a ``--force-claim-reason`` takes the claim anyway.
    """
    from kew.merge_claim import ClaimBusy, busy_claim_runs

    busy = busy_claim_runs(store, items)
    if not busy or force_reason is not None:
        return busy
    running = ", ".join(
        f"{b.kind} of item {b.item_id} (run {b.run_id[:8]})" for b in busy
    )
    raise ClaimBusy(
        f"PR #{pr} is not claimed: {running} is running and may push onto the "
        "head when it finishes; wait for it, or pass --force-claim with "
        "--force-claim-reason to claim the head anyway"
    )


def _record_forced_claim(
    store: SqliteRunStore,
    held: HeldClaim,
    busy: list[BusyRun],
    reason: str,
) -> None:
    """Attest a claim taken over running work: who, why, and over which runs.

    Written only once the take has succeeded, so a forced take that was then
    refused leaves no ``merge_claim_forced`` row behind.
    """
    from kew.audit import emit, resolve_actor

    _warn(
        f"kew verify: --force-claim: claimed PR #{held.pr} over "
        f"{len(busy)} running run(s): {reason}"
    )
    emit(
        store,
        "merge_claim_forced",
        actor=resolve_actor(),
        payload={
            "pr": held.pr,
            "heads": list(held.heads),
            "reason": reason,
            "runs": [
                {"kind": b.kind, "item_id": b.item_id, "run_id": b.run_id} for b in busy
            ],
        },
    )


@contextmanager
def _verify_claim(
    store: SqliteRunStore,
    root: Path,
    db_path: Path,
    *,
    ref: str,
    pr: int | None,
    item: str | None,
    force_reason: str | None,
) -> Generator[HeldClaim | None]:
    """The merge claim a verify of *ref* holds on *pr*'s head (#3198), if any."""
    from kew.merge_claim import claim_owner, held_merge_claim, pr_links
    from kew.merge_verify import claim_heads

    if pr is None:
        yield None
        return
    heads = claim_heads(root, ref=ref)
    links = pr_links(store, pr)
    items = links.items | ({item} if item is not None else set())
    busy = _busy_runs_refusal(store, pr, items, force_reason=force_reason)
    typer.echo(
        f"kew verify: claiming PR #{pr} at "
        f"{_claimed_head_text(heads, links.heads)} while it runs"
    )
    with held_merge_claim(
        store,
        lambda: _open_run_store(str(db_path)),
        pr,
        heads,
        owner=claim_owner(),
    ) as held:
        if busy and force_reason is not None:
            _record_forced_claim(store, held, busy, force_reason)
        yield held


@app.command()
def verify(
    ctx: typer.Context,
    ref: Annotated[
        str,
        typer.Argument(
            help=(
                "The commit to verify. Defaults to HEAD. `nightly`, "
                "`main-health` and `report` are subcommands instead."
            )
        ),
    ] = "HEAD",
    words: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "After `nightly`: install or uninstall. After `main-health`: clear SHA."
            ),
            show_default=False,
        ),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Where the job runs: local or gcp. Defaults to verify.host.",
        ),
    ] = None,
    shards: Annotated[
        int,
        typer.Option(
            "--shards",
            min=1,
            help="Run the parallel pytest phase as N jobs on N hosts.",
        ),
    ] = 1,
    pr_body: Annotated[
        Path | None,
        typer.Option(
            "--pr-body",
            exists=True,
            dir_okay=False,
            help=(
                "The PR description; its Behavior changes section may excuse "
                "a held-out base test failure by node id."
            ),
        ),
    ] = None,
    item: Annotated[
        str | None,
        typer.Option(
            "--item",
            help=(
                "The work item the candidate implements, whose acceptance "
                "contract the verification runs. Defaults to the item whose "
                "run opened the branch REF names."
            ),
        ),
    ] = None,
    epic: Annotated[
        str | None,
        typer.Option(
            "--epic",
            help=(
                "The epic the candidate's node belongs to, when the ledger cannot tell."
            ),
        ),
    ] = None,
    pr: Annotated[
        int | None,
        typer.Option(
            "--pr",
            min=1,
            help=(
                "The pull request whose head REF tests. The verify claims that "
                "head while it runs, so the drive pushes no remediation onto "
                "it; --item claims its node's PR the same way."
            ),
        ),
    ] = None,
    force_claim: Annotated[
        bool,
        typer.Option(
            "--force-claim",
            help=(
                "Claim the PR head even while a remediation, resolve, ci-fix "
                "or reviewer of its node is running; needs --force-claim-reason."
            ),
        ),
    ] = False,
    force_claim_reason: Annotated[
        str | None,
        typer.Option(
            "--force-claim-reason",
            help="Why the claim is forced; attested with your login, or KEW_ACTOR.",
        ),
    ] = None,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help=(
                'Run the full suite even when \\[verify] suite is "tiered", '
                "and the full vitest and ui-check even when \\[verify] frontend "
                'is "scoped". The defaults already run them in full.'
            ),
        ),
    ] = False,
    explain_selection: Annotated[
        bool,
        typer.Option(
            "--explain-selection",
            help=(
                "Run nothing: print the test files REF's change selects, with "
                "the import chain behind each, or the rule that forces the "
                "full suite."
            ),
        ),
    ] = False,
    selection_report: Annotated[
        bool,
        typer.Option(
            "--selection-report",
            help=(
                "Run nothing: replay the last merged commits on main and print "
                "the median and p75 selected share of tests, three ways."
            ),
        ),
    ] = False,
    frontend_report: Annotated[
        bool,
        typer.Option(
            "--frontend-report",
            help=(
                "Run nothing: replay the last frontend commits on main and "
                "print the median and p75 selected share of ui-check modules "
                'and vitest files under \\[verify] frontend = "scoped".'
            ),
        ),
    ] = False,
    last: Annotated[
        int,
        typer.Option(
            "--last",
            min=1,
            help="How many commits --selection-report or --frontend-report replays.",
        ),
    ] = 50,
    issue: Annotated[
        int | None,
        typer.Option("--issue", help="main-health clear: the issue tracking it."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="main-health clear: why main reads green."),
    ] = None,
    kew_binary: Annotated[
        Path | None,
        typer.Option("--kew-binary", help="nightly install: the kew the agent runs."),
    ] = None,
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="report: how far back to read verifies, like 30d, 12h or 2w.",
        ),
    ] = "30d",
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="nightly install: a shell env file the agent sources first.",
        ),
    ] = None,
) -> None:
    """Run the merge verification on a host and record what it returned.

    `kew verify nightly` verifies main in full and bisects a red one;
    `kew verify main-health` reads main's health, and `clear` resets a flake;
    `kew verify report` prints what every red verify was, phase by phase.
    """
    from kew.cli import verify_health

    if ref in verify_health.SUBCOMMANDS:
        verify_health.run_subcommand(
            ref,
            list(words or []),
            issue=issue,
            reason=reason,
            kew_binary=kew_binary,
            env_file=env_file,
            since=since,
        )
        return
    if words:
        _error(f"kew verify: unexpected arguments after {ref}: {' '.join(words)}")
        raise typer.Exit(2)
    conflict = _selection_flag_conflict(
        ctx,
        explain=explain_selection,
        report=selection_report,
        frontend_report=frontend_report,
    )
    if conflict is not None:
        _error(f"kew verify: {conflict}")
        raise typer.Exit(2)
    if _answer_selection_question(
        ref,
        explain=explain_selection,
        report=selection_report,
        frontend=frontend_report,
        last=last,
    ):
        return
    force_reason = _force_claim_reason(force_claim, force_claim_reason)
    from kew.merge_claim import ClaimBusy
    from kew.merge_verify import (
        VerifyRefused,
        resolve_contract,
        run_verify,
        terminate_unwinds,
    )
    from kew.run_store_merge_claims import MergeClaimRefused

    # The bundle is built with git, and the local host clones it with git.
    # A bare `kew verify <ref>` never touches GitHub. One bound to a PR
    # (--pr, or --item's PR) may file `flaky` issues once it is recorded
    # (kew.verify_ledger), but a filing that cannot run is logged and
    # recorded, never a failed verify, so `gh` is not required: a machine
    # that can run the gate can run the verification, CLI or no CLI.
    preflight_tools(required=("git",))
    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    # A SIGTERM unwinds through the host's release, so the instance's lease is
    # dropped rather than left to a dead process until its deadline (#2811).
    with terminate_unwinds(), _open_run_store(str(log_dir / "dispatch.db")) as store:
        config = load_config(store=store)
        root = project_root()
        try:
            # The node's contract, bound the way the drive's merge path binds
            # it: a verify that skipped it would be a merge gate without it.
            binding = resolve_contract(
                store, root, ref=ref, item_id=item, epic_ref=epic
            )
            typer.echo(f"kew verify: {binding.describe()}")
            claim = _verify_claim(
                store,
                root,
                log_dir / "dispatch.db",
                ref=ref,
                pr=pr if pr is not None else _item_pr_for_claim(store, item),
                item=item,
                force_reason=force_reason,
            )
            with claim as held:
                candidate, result = run_verify(
                    config,
                    store=store,
                    ref=ref,
                    host=host,
                    shards=shards,
                    repo_root=root,
                    pr_body=(
                        None if pr_body is None else pr_body.read_text(encoding="utf-8")
                    ),
                    contract=binding.contract,
                    merge_claim=held,
                    force_full=full,
                )
        except (VerifyRefused, MergeClaimRefused, ClaimBusy) as exc:
            _error(f"kew verify: {exception_text(exc)}")
            raise typer.Exit(2) from None
    _report_verify(candidate, result, claim=held)


def _report_main_red(result: VerifyResult) -> None:
    """Name main's red commit and its issue beside the verdict."""
    if result.main_red is None:
        return
    refusal = result.main_red_refusal()
    if refusal is not None:
        _error(f"kew verify: {refusal}")
        return
    _warn(
        f"kew verify: main is red at {result.main_red.sha}; this candidate "
        "passes the full suite, so main reads green once it lands"
    )


@app.command()
def backup(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Archive path or directory (default: log dir)."
        ),
    ] = None,
    include_logs: Annotated[
        bool,
        typer.Option(
            "--include-logs",
            help="Also bundle per-run derived-evidence sidecars "
            "(raw transcripts and event files are never included).",
        ),
    ] = False,
) -> None:
    """Snapshot the dispatch.db record (+ config) into a portable archive."""
    from kew.backup import create_backup

    config = load_config()
    log_dir = Path(config.logging.dir).resolve()
    db_path = log_dir / "dispatch.db"
    if not db_path.exists():
        typer.echo(f"No database at {db_path}", err=True)
        raise typer.Exit(2)
    out = output if output is not None else log_dir
    manifest = create_backup(
        db_path, resolved_config_path(), out, include_logs=include_logs
    )
    typer.echo(
        f"Backup written (schema v{manifest.schema_version}, {manifest.runs} runs, "
        f"chain ok={manifest.chain_ok})."
    )


@app.command()
def restore(
    archive: Annotated[Path, typer.Argument(help="Path to a kew-backup-*.tar.gz.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing db (moved aside first)."),
    ] = False,
    with_config: Annotated[
        bool,
        typer.Option(
            "--with-config", help="Also restore kew.toml (existing one moved aside)."
        ),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify", help="Verify the chain matches the manifest."
        ),
    ] = True,
) -> None:
    """Restore a backup archive into the configured log directory."""
    from kew.audit import default_anchors_path
    from kew.backup import BackupError, RestorePrerequisiteError, restore_backup
    from kew.config import project_root

    if not archive.is_file():
        typer.echo(f"No backup archive at {archive}", err=True)
        raise typer.Exit(2)
    config = load_config()
    target_dir = Path(config.logging.dir).resolve()
    cfg = resolved_config_path()
    config_dir = cfg.parent if cfg is not None else Path.cwd()
    try:
        result = restore_backup(
            archive,
            target_dir,
            config_dir,
            force=force,
            with_config=with_config,
            verify=verify,
            project_root=project_root(),
            anchors_path=default_anchors_path(),
        )
    except (RestorePrerequisiteError, SchemaTooNewError) as exc:
        typer.echo(exception_text(exc), err=True)
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    except BackupError as exc:
        typer.echo(exception_text(exc), err=True)
        raise typer.Exit(1) from exc
    moved = f" (old db saved at {result.moved_old_to})" if result.moved_old_to else ""
    typer.echo(
        f"Restored {result.manifest.runs} runs (schema "
        f"v{result.manifest.schema_version}){moved}."
    )


# Re-exports so `kew.cli` stays the single namespace the test suite monkeypatches
# and reads, exactly as it was when cli.py was a flat module. Some of these are
# consumed only by the moved command submodules (via `kew.cli.<name>`); others are
# asserted present / patched as `kew.cli.<name>` in tests.
from kew.cli import config as _config_cmds  # noqa: E402,F401
from kew.cli import delegate as _delegate_cmds  # noqa: E402,F401
from kew.cli import doctor as _doctor_cmds  # noqa: E402,F401
from kew.cli import drive as _drive_cmds  # noqa: E402,F401
from kew.cli import epic as _epic_cmds  # noqa: E402
from kew.cli import gc as _gc_cmds  # noqa: E402
from kew.cli import hosts as _hosts_cmds  # noqa: E402,F401
from kew.cli import ingest as _ingest_cmds  # noqa: E402,F401
from kew.cli import init as _init_cmds  # noqa: E402
from kew.cli import loop as _loop_cmds  # noqa: E402,F401
from kew.cli import merge as _merge_cmds  # noqa: E402,F401
from kew.cli import model_eol as _model_eol_cmds  # noqa: E402,F401
from kew.cli import models as _models_cmds  # noqa: E402,F401
from kew.cli import no_change as _no_change_cmds  # noqa: E402,F401
from kew.cli import plan as _plan_cmds  # noqa: E402,F401
from kew.cli import prompts as _prompts_cmds  # noqa: E402,F401
from kew.cli import publication as _publication_cmds  # noqa: E402,F401
from kew.cli import quota as _quota_cmds  # noqa: E402,F401
from kew.cli import rename_repo as _rename_repo_cmds  # noqa: E402
from kew.cli import run_request as _run_request_cmds  # noqa: E402,F401
from kew.cli import schema as _schema_cmds  # noqa: E402
from kew.cli import serve as _serve_cmds  # noqa: E402,F401
from kew.cli import triage as _triage_cmds  # noqa: E402,F401
from kew.graph.adapters import make_github_source  # noqa: E402, F401
from kew.graph.adapters.github_convention import (  # noqa: E402, F401
    GitHubConventionSource,
)
from kew.graph.build import build_graph  # noqa: E402, F401
from kew.graph.driver.dispatch import DetachedDispatchSink  # noqa: E402, F401
from kew.graph.driver.facts import (  # noqa: E402, F401
    GitHubFactsReader,
    prime_issue_snapshots_from,
)
from kew.graph.driver.lock import lock_holder  # noqa: E402, F401
from kew.graph.driver.review_launcher import (  # noqa: E402, F401
    sandbox_reviewer_launcher,
)
from kew.graph.driver.tick import advance, summarize  # noqa: E402, F401
from kew.ids import provider_from_config, validate_item_id  # noqa: E402, F401
from kew.labels import clear_pending_review_label  # noqa: E402, F401

# Root commands, not groups: registered here because `app` is defined in this
# module, which the submodules cannot name at import time without a cycle.
app.command("gc")(_gc_cmds.gc)
app.command("init")(_init_cmds.init)
app.command("rename-repo")(_rename_repo_cmds.rename_repo)
app.command("schema")(_schema_cmds.schema)

# Re-export epic helpers/commands referenced as kew.cli.<name> by tests / patched.
_load_epic_graph = _epic_cmds._load_epic_graph
_epic_repo = _epic_cmds._epic_repo
_print_tick_summary = _epic_cmds._print_tick_summary
_print_tick_result = _epic_cmds._print_tick_result
_sleep = _epic_cmds._sleep
_blast_radius_str = _epic_cmds._blast_radius_str
_epic_lock_path = _epic_cmds._epic_lock_path
_epic_driver_sources = _epic_cmds._epic_driver_sources
_epic_driver_config = _epic_cmds._epic_driver_config
_locked_out_message = _epic_cmds._locked_out_message
_dispatch_linear_node = _epic_cmds._dispatch_linear_node
epic_plan = _epic_cmds.epic_plan
epic_status = _epic_cmds.epic_status

# Explicit re-export surface so the command submodules can reach these through
# `kew.cli.<name>` (the single monkeypatch namespace) without tripping mypy's
# no-implicit-reexport rule. Names the submodules resolve at call time via `_c.`.
__all__ = [
    "DetachedDispatchSink",
    "GitHubConventionSource",
    "GitHubFactsReader",
    "make_github_source",
    "prime_issue_snapshots_from",
    "advance",
    "build_graph",
    "build_notifier",
    "clear_pending_review_label",
    "datetime",
    "execute_dispatch",
    "load_config",
    "lock_holder",
    "preflight_tools",
    "project_root",
    "provider_from_config",
    "select_next_issue",
    "service_verbs",
    "summarize",
    "validate_item_id",
    "_detect_repo",
    "_preflight",
    "_log_project_banner",
    "_open_run_store",
    "_load_epic_graph",
    "_epic_repo",
    "_print_tick_summary",
    "_print_tick_result",
    "_sleep",
    "_epic_lock_path",
    "_epic_driver_sources",
    "_epic_driver_config",
    "_locked_out_message",
    "_dispatch_linear_node",
]
