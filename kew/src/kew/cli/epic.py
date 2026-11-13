"""`kew epic` command group: build, validate, and drive epic graphs."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, TextIO

import typer

from kew.credential_handoff import (
    detached_credential_handoff,
    is_detached_credential_child,
    require_credential_route,
)
from kew.credential_issuer import IssuerError

if TYPE_CHECKING:
    from kew.linear.tracker import LinearTracker
    from kew.overlap import DispatchOutcome
    from kew.run_store import EpicHold, RunRecord, RunStatus, RunStore

from kew import cli as _c
from kew.acceptance_author import AcceptanceAuthorRefused, amend_by_hand
from kew.acceptance_dispute import AcceptanceAmendmentRefused, refer_dispute_to_author
from kew.audit import resolve_actor
from kew.claim_retention import (
    LINEAR_CLAIM_RECOVERY,
    record_retained_claim,
    retained_claim_lines,
)
from kew.cli._apps import acceptance_app, epic_app
from kew.config import DispatchConfig, project_root
from kew.drive import DRIVE_RUNNING, DriveHolder, read_drive
from kew.epic_archive import archived_epic_refs
from kew.epic_holds import (
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REVIEW_CYCLE_RESET_HOLD_REASONS,
    merge_gate_ack_key,
    merge_gate_reviewed_head,
    restarts_review_cycle,
    safe_hold_reason,
)
from kew.graph.adapters import make_dependency_source
from kew.graph.domain import Graph
from kew.graph.driver.attention import (
    AttentionEvent,
    attention_events,
    bucket_for,
    driver_lock_stalled_action,
)
from kew.graph.driver.attention_journal import (
    journaled_attention_events,
    read_journaled_observations,
)
from kew.graph.driver.attention_stream import AttentionStream
from kew.graph.driver.dispatch import DispatchedOutcome, resume_continuation
from kew.graph.driver.lock import single_driver_lock
from kew.graph.driver.operator_merge import OperatorMergeRefused, operator_merge
from kew.graph.driver.overlay import admission_refused_hold_reason
from kew.graph.driver.pre_review_gate import gate_skip_lines
from kew.graph.driver.result import (
    WITHHELD_REASON_PREFIXES,
    PrLink,
    StuckNode,
    TerminalSignal,
    TickResult,
    TickSummary,
)
from kew.graph.driver.review_detach import DetachedReviewSpawner
from kew.graph.driver.review_launcher import (
    sandbox_base_integrator,
    sandbox_remediator,
    sandbox_reviewer_launcher,
)
from kew.graph.driver.tick import (
    CLOSED_UNMERGED_REASON_PREFIX,
    EmptyEpicGraphError,
    _facts_key,
    default_run_liveness,
    pseudo_epic_clone_refusal,
)
from kew.graph.export import render
from kew.graph.port import DependencySource
from kew.graph.validate import validate
from kew.ids import (
    InvalidItemIdError,
    branch_slug,
    item_id_sort_key,
    item_ref,
)
from kew.ledger_identity import LedgerIdentityError
from kew.linear.client import (
    LinearApiError,
    LinearAuthError,
    LinearUnavailableError,
)
from kew.notifier import Notifier
from kew.policy import repo_root as _git_repo_root
from kew.progress import _error, _log, _success, _warn
from kew.publication import activate_detached_publication_token
from kew.run_store import ContinuationRecord, SchemaTooNewError, SqliteRunStore
from kew.run_store_work import (
    DispatchReservationReplaced,
    capture_dispatch_reservation,
)
from kew.service.epics import (
    DRIVER_GONE,
    DriverLiveness,
    DriverLockFacts,
    driver_liveness,
    record_driver_waiting,
)
from kew.service.send_back import PullRequestHead
from kew.service.verbs import (
    ContinuationSelectionDenied,
    EpicArchiveDenied,
    select_awaiting_continuation,
    set_epic_archived,
)
from kew.subprocess_utils import (
    pin_current_executable,
    run,
    run_pinned_driver_tick,
)
from kew.tracking import LaunchState, merge_launch_evidence, partial_dispatch_result
from kew.waits import (
    EPIC_CONTINUE_COMMAND,
    HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
    HOST_RESTART_REPEATED_PRESERVED_REASON,
    HOST_RESTART_REPEATED_TAKEN_UP_REASON,
    cause_hint,
    fill_command,
)


def _load_epic_graph(
    name: str,
    epic_prefix: str = "epic:",
    *,
    source: DependencySource | None = None,
) -> Graph:
    """Build the epic graph from the configured tracker; guard empty epics.

    A caller that already holds a DependencySource (e.g. `epic status`, which
    wraps Linear in the reconcile short-circuit) passes it as ``source`` so the
    fetch routes through that instance; it is then responsible for any
    tracker-specific preflight. When ``source`` is None the tracker's default
    source is built here (and the GitHub path is preflighted first)."""
    config = _c.load_config()
    if source is None:
        if config.tracker.type == "github":
            # REQUIRED: the GitHub path shells out to `gh` (repo detection + the
            # GitHubConventionSource issue queries). Without this, `gh` isn't on
            # the subprocess allowlist and every call raises "not in allowlist".
            # The Linear path is pure HTTPS — no subprocess tooling at all.
            _c.preflight_tools()
            _c._detect_repo(config)
            if not config.project.repo:
                _error("Could not detect repo. Set project.repo in kew.toml")
                raise typer.Exit(2)
        source = make_dependency_source(config, epic_prefix=epic_prefix)
    try:
        graph = _c.build_graph(source, name)
    except LinearApiError as exc:
        _error(_linear_failure_message(name, exc))
        raise typer.Exit(2) from exc
    except RuntimeError as exc:
        _error(str(exc))
        raise typer.Exit(2) from exc
    if not graph.nodes:
        _error(_empty_epic_message(name, config, epic_prefix))
        raise typer.Exit(2)
    return graph


def _empty_epic_message(name: str, config: DispatchConfig, epic_prefix: str) -> str:
    """The tracker-specific diagnostic for an epic whose query resolves EMPTY,
    naming the epic ref, tracker type, and the empty query (Linear project vs
    GitHub label). Shared by `_load_epic_graph` and the `advance`/`run` empty-
    graph guard so both paths report the same thing (#819)."""
    if config.tracker.type == "linear":
        return f"Linear project '{name}' has no issues."
    if name.isdigit():
        return f"issue #{name} has no sub-issues"
    return f"No issues found for label '{epic_prefix}{name}'."


def _linear_failure_message(name: str, exc: LinearApiError) -> str:
    """The one-line diagnostic for any Linear API failure at the tick boundary.

    `linear-unavailable` is a transient outage (DNS/connect failure, timeout,
    reset, any 5xx or an edge 429) that outlasted the client's bounded retry: an
    infrastructure blip to re-run, not a stuck epic. `linear-auth` and
    `linear-error` fail fast (auth, TLS, 4xx, GraphQL). `linear-outcome-unknown`
    is a write whose result is unknown (not retried). No Linear failure may
    reach the operator as a traceback. The client already redacts the key."""
    detail = " ".join(str(exc).split())
    if exc.write_outcome_unknown:
        return (
            f"epic {name}: linear-outcome-unknown — a Linear write may or may not "
            f"have applied ({detail}). Check the item's state, assignee and "
            "comments in Linear, then re-run the tick; it re-reads the item first."
        )
    if isinstance(exc, LinearUnavailableError):
        return (
            f"epic {name}: linear-unavailable — Linear did not respond "
            f"({detail}). Transient; re-run the tick once Linear recovers."
        )
    if isinstance(exc, LinearAuthError):
        return (
            f"epic {name}: linear-auth — {detail}. Fix the key, then re-run the tick."
        )
    return (
        f"epic {name}: linear-error — Linear API call failed ({detail}). "
        "Not transient; fix the cause, then re-run the tick."
    )


@epic_app.command("graph")
def epic_graph(
    name: str,
    fmt: str = typer.Option("mermaid", "--format", help="mermaid or dot"),
    epic_prefix: str = "epic:",
) -> None:
    """Export the epic DAG as a Mermaid or DOT diagram."""
    graph = _c._load_epic_graph(name, epic_prefix)
    try:
        output = render(graph, fmt)
    except ValueError as exc:
        _error(str(exc))
        raise typer.Exit(2) from exc
    typer.echo(output)


def _echo_validate_ok(graph: Graph, levels: int) -> None:
    """Count member nodes only; name external dependencies on their own line."""
    members = [n for n in graph.nodes.values() if not n.external]
    externals = [n.id for n in graph.nodes.values() if n.external]
    typer.echo(f"OK: {len(members)} nodes, {levels} dependency levels.")
    if externals:
        typer.echo(f"External dependencies (outside the epic): {externals}")


@epic_app.command("validate")
def epic_validate(name: str, epic_prefix: str = "epic:") -> None:
    """Validate the epic DAG (cycles, dangling deps, self-edges)."""
    graph = _c._load_epic_graph(name, epic_prefix)
    report = validate(graph)
    for node_id in graph.multi_depends:
        # A warning, not a failure: the comments are unioned, so the DAG is
        # right; the author should still merge them into one.
        typer.echo(
            f"Warning: #{node_id} carries more than one kew:depends comment; "
            "their dependencies are combined -- merge them into one comment."
        )
    if report.self_edges:
        typer.echo(f"Self-edges (issue depends on itself): {report.self_edges}")
    if report.dangling:
        typer.echo(f"Dangling dependencies (referenced but missing): {report.dangling}")
    for cyc in report.cycles:
        typer.echo(f"Cycle detected: {' -> '.join(cyc)} -> {cyc[0]}")
    if report.orphans:
        typer.echo(f"Orphan nodes (no dependencies): {report.orphans}")
    if report.ok:
        _echo_validate_ok(graph, len(report.levels))
    else:
        raise typer.Exit(1)


@epic_app.command("plan")
def epic_plan(
    name: str,
    from_plan: Annotated[
        Path | None,
        typer.Option("--from", help="Implementation plan file to turn into issues"),
    ] = None,
    from_manifest: Annotated[
        Path | None,
        typer.Option(
            "--from-manifest",
            help="Create from an existing manifest (skips the planning agent)",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview; create nothing")
    ] = False,
    amend: Annotated[
        bool,
        typer.Option(
            "--amend",
            help="Append new nodes to an existing epic instead of refusing it",
        ),
    ] = False,
) -> None:
    """Turn an implementation plan into a drivable epic (governed planning run)."""
    from kew.graph.plan_run import run_epic_plan

    config = _c.load_config()
    if config.tracker.type != "github":
        # Same GitHub-only rule as the driver: the on-ramp creates int-keyed
        # issues that `kew epic run` drives.
        _error(
            "kew epic plan requires tracker type 'github'; "
            f"this project is configured with '{config.tracker.type}'."
        )
        raise typer.Exit(2)
    if (from_plan is None) == (from_manifest is None):
        _error("Provide exactly one of --from or --from-manifest.")
        raise typer.Exit(2)
    # The planning path checks its forced complex route at the launch boundary.
    _c._preflight(config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        rc = run_epic_plan(
            name=name,
            config=config,
            plan_path=from_plan,
            manifest_path=from_manifest,
            dry_run=dry_run,
            amend=amend,
            store=store,
        )
    if rc != 0:
        raise typer.Exit(rc)


def _epic_repo(config: DispatchConfig) -> str:
    """Resolve `config.project.repo` (auto-detecting via `gh` if unset) or exit."""
    _c._detect_repo(config)
    if not config.project.repo:
        _error("Could not detect repo. Set project.repo in kew.toml")
        raise typer.Exit(2)
    return config.project.repo


def _lock_slug(name: str) -> str:
    """Filename-safe form of an epic name for its per-epic lock file (#861)."""
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in name)
    return safe.strip("-").lower() or "epic"


def _epic_lock_base(config: DispatchConfig) -> Path:
    """The configured driver-lock path (or ``<logging.dir>/epic-driver.lock``)
    each epic's own lock file is named after."""
    return (
        Path(config.epic.lock_path)
        if config.epic.lock_path
        else Path(config.logging.dir).resolve() / "epic-driver.lock"
    )


def _epic_lock_path(config: DispatchConfig, name: str) -> Path:
    """The PER-EPIC driver-lock path (#861/F2): the config override (or
    ``<logging.dir>/epic-driver.lock``) with the epic key woven into the
    filename, so two epics lock independently and can tick concurrently.
    Shared by the driver (`advance`/`run`) and the read-only `kew epic status`
    holder line."""
    base = _epic_lock_base(config)
    return base.with_name(f"{base.stem}-{_lock_slug(name)}{base.suffix}")


def _epic_lock_files(config: DispatchConfig) -> list[Path]:
    """Every per-epic driver-lock file beside the configured base, sorted."""
    base = _epic_lock_base(config)
    return sorted(base.parent.glob(f"{base.stem}-*{base.suffix}"))


def _locked_out_message(name: str, config: DispatchConfig) -> str:
    """The lock-skip line for epic `name` (#861): name the holder (pid, epic,
    held-since) so a locked-out poller is honest about WHO holds the epic's
    lock, and hand over the right workaround — a manual tick for a live holder,
    or ``--takeover`` for a stale (dead-pid) one."""
    holder = _c.lock_holder(_c._epic_lock_path(config, name))
    if holder is None:
        return (
            f"epic {name}: another driver holds this epic's lock (holder "
            f"unknown) — tick it manually: kew epic advance {name}"
        )
    if holder.is_stalled(now=datetime.now(UTC)):
        return f"epic {name}: STALLED — {_stalled_lock_line(holder)}"
    if holder.is_alive():
        return (
            f"epic {name}: another driver holds this epic's lock "
            f"({holder.describe()}) — its agents keep running; tick it "
            f"manually: kew epic advance {name}"
        )
    return (
        f"epic {name}: this epic's lock is held by a STALE holder "
        f"({holder.describe()}) — reclaim it: kew epic advance {name} --takeover"
    )


def _stalled_lock_line(holder: Any) -> str:
    """Why a stalled holder blocks the epic and what clears it (#3331)."""
    return (
        f"its driver lock is held by pid {holder.pid}, alive but no longer "
        f"beating its heartbeat ({holder.describe()}); kew never takes the lock "
        f"from a live process: {driver_lock_stalled_action(holder.pid)}"
    )


def _resolved_max_parallel(override: int | None, config: DispatchConfig) -> int:
    """``--max-parallel`` when given — 0 included, which admits nothing new
    during an operator stop — else the configured epic cap, else the fleet's."""
    if override is not None:
        return override
    return config.epic.max_parallel or config.fleet_concurrency()


def _epic_driver_config(
    max_parallel_override: int | None,
) -> tuple[DispatchConfig, str, int, list[str]]:
    """Resolve the config/repo/max_parallel/approvers shared by `advance` and
    `run` (see Task 1's `[epic]` config + the driver design spec's "Wiring
    values"). The lock path is per-epic (#861/F2), so it is resolved at each
    call site via `_epic_lock_path(config, name)` rather than returned here."""
    config = _c.load_config()
    # Show which control plane the driver is touching before it acts (F1).
    _c._log_project_banner(config)
    # The driver drives GitHub and Linear epics on durable item-id currency
    # (L3); only `epic plan` stays GitHub-only (its on-ramp mints int-keyed
    # issues). `TrackerConfig.type` is a Literal, so any other value is a
    # config error, not a supported tracker.
    if config.tracker.type not in ("github", "linear"):
        _error(
            "kew epic advance/run/retry require tracker type 'github' or "
            f"'linear'; this project is configured with '{config.tracker.type}'."
        )
        raise typer.Exit(2)
    # PRs live on GitHub for both trackers, so the merge pass still shells out
    # to `gh` — preflight it and resolve the repo regardless of tracker type.
    _c.preflight_tools()
    repo = _c._epic_repo(config)
    approvers = config.epic.approvers or config.approval.approvers
    max_parallel = _resolved_max_parallel(max_parallel_override, config)
    return config, repo, max_parallel, approvers


#: Operator-facing names for the pinned identity slots, in ``_driver_identity``
#: order. A reload that retargets one of them has to say WHICH: "stop and
#: relaunch" with no field named leaves the operator diffing kew.toml by hand.
_DRIVER_IDENTITY_FIELDS = (
    "tracker.type",
    "project.repo",
    "project.host",
    "logging.dir/dispatch.db",
    "epic.lock_path",
)


def _driver_repo_host() -> str:
    """Resolve the project host through the origin-aware shared helper."""
    from kew.repo_host import repo_host

    return repo_host()


def _driver_identity(
    config: DispatchConfig, *, host: str | None = None
) -> tuple[str, str, str, Path, Path]:
    """Return startup-pinned controls that a live driver must never retarget."""
    log_dir = Path(config.logging.dir).resolve()
    lock_base = (
        Path(config.epic.lock_path).resolve()
        if config.epic.lock_path
        else log_dir / "epic-driver.lock"
    )
    return (
        config.tracker.type,
        config.project.repo,
        host or _driver_repo_host(),
        log_dir / "dispatch.db",
        lock_base,
    )


@dataclass
class _DriverPin:
    """What a live driver refuses to retarget, plus its last-good config.

    ``identity`` is the startup ``_driver_identity`` tuple. ``last_good`` is the
    most recent config that both loaded and validated; a reload that fails to
    load keeps it in force rather than killing a driver mid-epic over a
    half-saved kew.toml.
    """

    identity: tuple[str, str, str, Path, Path]
    last_good: DispatchConfig


def _identity_drift(
    startup: tuple[str, str, str, Path, Path],
    reloaded: tuple[str, str, str, Path, Path],
) -> list[str]:
    """``field: old -> new`` for every pinned slot a reload retargeted."""
    return [
        f"{field}: {old} -> {new}"
        for field, old, new in zip(
            _DRIVER_IDENTITY_FIELDS, startup, reloaded, strict=True
        )
        if old != new
    ]


def _load_reloaded_config(store: SqliteRunStore, pin: _DriverPin) -> DispatchConfig:
    """This tick's config, or the last-good one when the reload cannot load.

    A kew.toml that is malformed, half-saved, or rejected by validation is an
    operator mid-edit, not a retarget: exiting there would kill a healthy driver
    over a text editor's autosave. Warn, keep governing with the last config
    that loaded, and pick up the fixed file on the next tick.
    """
    try:
        config = _c.load_config(store=store)
        if "repo" in config.project.model_fields_set:
            _c._detect_repo(config)
            if not config.project.repo:
                raise ValueError("project.repo auto-detection returned no repository")
        else:
            # The startup path already resolved this omitted field. Reuse that
            # identity instead of depending on `gh` during every reload.
            config.project.repo = pin.identity[1]
    except typer.Exit:
        raise
    except Exception as exc:
        _warn(
            f"kew epic run: config reload failed ({exc}) — keeping the last-good "
            "config; the fixed kew.toml is picked up on the next tick."
        )
        return pin.last_good
    return config


def _reload_driver_controls(
    store: SqliteRunStore,
    *,
    pin: _DriverPin,
    max_parallel_override: int | None,
) -> tuple[DispatchConfig, int, list[str]]:
    """Atomically accept one tick's mutable controls, or fail closed.

    Loading and validation finish before the returned config is used, so an
    invalid reload cannot partially update approvers, concurrency, or policy.
    Tracker, repository, DB, and lock identity remain pinned to startup: a
    reload that retargets any of them names the field, its old value and its
    new one, then stops the driver. A reload that cannot load at all keeps the
    last-good config (see ``_load_reloaded_config``) instead of exiting.
    """
    config = _load_reloaded_config(store, pin)
    pinned_host = (
        pin.identity[2] if "repo" not in config.project.model_fields_set else None
    )
    drift = _identity_drift(pin.identity, _driver_identity(config, host=pinned_host))
    if drift:
        _error(
            "kew epic run: config reload changed pinned tracker/repo/DB/lock "
            f"identity ({'; '.join(drift)}) — stop and relaunch the driver."
        )
        raise typer.Exit(2)
    pin.last_good = config
    approvers = config.epic.approvers or config.approval.approvers
    resolved_max_parallel = _resolved_max_parallel(max_parallel_override, config)
    return config, resolved_max_parallel, approvers


def _resume_record(
    store: SqliteRunStore, *, epic_ref: str, item_id: str
) -> ContinuationRecord:
    """Select the one awaiting continuation identity for an epic node."""
    try:
        return select_awaiting_continuation(store, epic_ref, item_id)
    except ContinuationSelectionDenied as exc:
        raise ValueError(str(exc).format(item_id=item_ref(item_id))) from None


def _validate_resume_worktree(
    record: ContinuationRecord, config: DispatchConfig, store: RunStore
) -> Path:
    """Prove the recorded checkout is safe to hand to a continuation agent."""
    from kew.worktree import (
        CheckoutBusy,
        claim_checkout,
        is_linked_worktree,
        is_safe_standalone_clone,
        linked_worktree_resume_message,
        require_checkout_generation,
        unsafe_clone_resume_message,
    )

    # Claim the lexical path before resolve or any checkout inspection.
    worktree = Path(record.worktree_identity).expanduser().absolute()
    try:
        claim_checkout(worktree)
    except CheckoutBusy as exc:
        raise ValueError(str(exc)) from exc

    root = Path(config.branches.worktree_base).expanduser().resolve()
    worktree = Path(record.worktree_identity).expanduser().resolve()
    if worktree == root or not worktree.is_relative_to(root):
        raise ValueError(
            f"preserved worktree for {item_ref(record.item_id)} is outside the "
            "configured worktree root"
        )
    if not worktree.is_dir():
        raise ValueError(
            f"preserved worktree for {item_ref(record.item_id)} is missing"
        )

    if not is_safe_standalone_clone(worktree):
        message = (
            linked_worktree_resume_message(worktree)
            if is_linked_worktree(worktree)
            else unsafe_clone_resume_message(worktree)
        )
        raise ValueError(message)

    from kew.continuation_recovery import require_continuation_generation

    require_continuation_generation(
        store,
        record,
        validate_generation=partial(require_checkout_generation, worktree),
    )

    if config.publication.publish == "canonical":
        _require_canonical_continuation(record, config, worktree)
    else:
        _require_legacy_continuation(record, worktree)
    return worktree


def _require_canonical_continuation(
    record: ContinuationRecord, config: DispatchConfig, worktree: Path
) -> None:
    """Identity and dirtiness without git in the preserved checkout: the HEAD
    file as data and the host walker (review round 10 of #1440)."""
    from kew.graph.driver.tick import _checkout_holds_work, _recorded_head_branch

    if _recorded_head_branch(worktree) != record.branch:
        raise ValueError(
            f"preserved worktree for {item_ref(record.item_id)} is not on "
            f"recorded branch {record.branch!r}"
        )
    if not _checkout_holds_work(config, worktree, record.branch):
        raise ValueError(f"preserved worktree for {item_ref(record.item_id)} is clean")


def _require_legacy_continuation(record: ContinuationRecord, worktree: Path) -> None:
    """The checkout escape hatch's git-based identity and dirtiness checks."""
    branch = run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
        capture=True,
    )
    if branch.returncode != 0 or branch.stdout.strip() != record.branch:
        raise ValueError(
            f"preserved worktree for {item_ref(record.item_id)} is not on "
            f"recorded branch {record.branch!r}"
        )

    status = run(
        [
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
        capture=True,
    )
    if status.returncode != 0:
        raise ValueError(
            f"cannot inspect preserved worktree for {item_ref(record.item_id)}"
        )
    if not status.stdout.strip():
        raise ValueError(f"preserved worktree for {item_ref(record.item_id)} is clean")


def _resume_owner_is_dead(
    store: SqliteRunStore,
    record: ContinuationRecord,
    config: DispatchConfig,
    *,
    own_claim: str | None = None,
) -> bool:
    """Require positive proof that no recorded owner still holds the checkout.

    The original dispatch's death is proven first, by the rules
    :func:`_original_owner_is_dead` applies. A later continuation generation
    may still be running in the same checkout while the original is long dead,
    so every owner the history records is then probed the way abandon probes
    it (:func:`kew.run_liveness.continuation_owner_death_refusal`), and any
    recorded container must be stopped or absent (#2960). The child a claim
    launched passes its own run as *own_claim*: it is the owner now (#2964).
    """
    if not _original_owner_is_dead(store, record, config):
        return False
    from kew.run_liveness import continuation_owner_death_refusal

    return (
        continuation_owner_death_refusal(store, record, config, own_claim=own_claim)
        is None
    )


def _original_owner_is_dead(
    store: SqliteRunStore, record: ContinuationRecord, config: DispatchConfig
) -> bool:
    """Require positive proof that the original dispatch no longer owns work."""
    original = store.get(record.original_run_id)
    if original is None:
        return False
    from kew.fleet import attempt_is_dead
    from kew.process_groups import owner_alive
    from kew.run_liveness import host_died_under, recorded_container_stopped

    if host_died_under(original):
        # The evidence the driver reaped it on: a later boot may have handed
        # its pids to strangers, so they are not probed (#2960). A container
        # outlives the boot, so it must still be stopped or absent.
        return not original.container_name or recorded_container_stopped(
            original, config
        )
    if original.container_name:
        return default_run_liveness(config)(original) is False
    from kew.graph.driver.tick import _host_died_under

    if _host_died_under(original):
        # Every process of an earlier boot died with it; the pids it recorded
        # may name strangers now, so they prove nothing either way (#2964).
        return True
    if not attempt_is_dead(original):
        return False
    if original.owner_pid is None:
        return False
    return (
        owner_alive(
            original.pgid or original.owner_pid,
            original.owner_pid,
            original.owner_start_time,
            original.owner_ppid,
            original.owner_command,
            original.owner_start_token,
        )
        is False
    )


def _validate_continue_item(node: str, provider: str) -> str:
    """Validate the provider-specific item id used by ``epic continue``."""
    try:
        return _c.validate_item_id(node, provider)
    except InvalidItemIdError:
        _error(
            f"kew epic continue: node must be a valid {provider} item id, got {node!r}"
        )
        raise typer.Exit(2) from None


def _unknown_owner_resolution() -> str:
    """Name the abandon command only when this CLI actually registers it."""
    from typer.core import TyperGroup
    from typer.main import get_command

    commands = get_command(epic_app)
    if isinstance(commands, TyperGroup) and "abandon" in commands.commands:
        return "use kew epic abandon to resolve unknown ownership."
    return (
        "the continuation needs operator resolution. "
        "The abandon verb ships in PR #1952."
    )


def _prepare_resume_record(
    store: SqliteRunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
) -> ContinuationRecord:
    """Select, validate, and positively orphan one preserved worktree."""
    from kew.worktree import CheckoutAdmissionRefusal

    try:
        record = _resume_record(store, epic_ref=epic_ref, item_id=item_id)
        _validate_resume_worktree(record, config, store)
    except (OSError, ValueError, CheckoutAdmissionRefusal) as exc:
        _error(f"kew epic continue: {exc}")
        raise typer.Exit(2) from None
    if not _resume_owner_is_dead(store, record, config):
        _error(
            f"kew epic continue: {item_ref(item_id)} still has a live or unproven "
            "owner; continue requires a retained owner_pid and positive owner-death "
            "evidence. Inspect the recorded processes, then "
            + _unknown_owner_resolution()
        )
        raise typer.Exit(2)
    return record


def _claim_resume_record(
    store: SqliteRunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
    expect: ContinuationRecord,
) -> ContinuationRecord:
    """Claim the exact continuation identity that passed CLI validation."""
    try:
        return _c.service_verbs.epic_resume(
            store,
            epic_ref=epic_ref,
            item_id=item_id,
            config=config,
            expect=expect,
        )
    except (_c.service_verbs.EpicResumeDenied, _c.service_verbs.VerbDenied) as exc:
        _error(f"kew epic continue: {exc}")
        raise typer.Exit(2) from None


def _dispatch_github_continuation(
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    *,
    epic_ref: str,
    provider: str,
    claimed_item: str,
    claimed_worktree: Path,
    claimed_branch: str,
    continuation_run_id: str,
) -> DispatchedOutcome:
    """Dispatch a GitHub continuation against its existing worktree."""
    issue = _c.select_next_issue(config=config, force_issue=int(claimed_item))
    if issue is None:
        raise RuntimeError(
            f"could not load GitHub issue #{claimed_item} for continuation"
        )
    _require_dispatch_child_runner(config, issue)
    return _c.execute_dispatch(
        issue=issue,
        config=config,
        store=store,
        notifier=notifier,
        approved=True,
        epic_ref=epic_ref,
        item_id=claimed_item,
        provider=provider,
        worktree_dir=claimed_worktree,
        branch=claimed_branch,
        run_id=continuation_run_id,
    )


def _resume_dispatch_callback(
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    *,
    epic_ref: str,
    provider: str,
) -> Callable[[str, Path, str, str], DispatchedOutcome]:
    """Build the provider-specific callback for a claimed continuation."""

    def _dispatch(
        claimed_item: str,
        claimed_worktree: Path,
        claimed_branch: str,
        continuation_run_id: str,
    ) -> DispatchedOutcome:
        """Dispatch the claimed continuation through its tracker provider."""
        if provider == "github":
            return _dispatch_github_continuation(
                store,
                config,
                notifier,
                epic_ref=epic_ref,
                provider=provider,
                claimed_item=claimed_item,
                claimed_worktree=claimed_worktree,
                claimed_branch=claimed_branch,
                continuation_run_id=continuation_run_id,
            )
        return _dispatch_linear_node(
            store,
            config,
            notifier,
            epic_ref=epic_ref,
            item_id=claimed_item,
            provider=provider,
            continuation_worktree=claimed_worktree,
            continuation_branch=claimed_branch,
            continuation_run_id=continuation_run_id,
        )

    return _dispatch


def _run_resume_continuation(
    claimed: ContinuationRecord,
    *,
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    epic_ref: str,
    provider: str,
) -> DispatchedOutcome:
    """Run a claimed continuation and preserve its exception behavior."""
    dispatch = _resume_dispatch_callback(
        store,
        config,
        notifier,
        epic_ref=epic_ref,
        provider=provider,
    )
    observed: DispatchedOutcome | None = None

    def _observe_dispatch(
        item: str, worktree: Path, branch: str, run_id: str
    ) -> DispatchedOutcome:
        nonlocal observed
        observed = dispatch(item, worktree, branch, run_id)
        return observed

    try:
        return resume_continuation(claimed, store=store, dispatch=_observe_dispatch)
    except (typer.Exit, sqlite3.Error):
        raise
    except Exception as exc:
        _error(f"kew epic continue: continuation failed: {exc}")
        evidence = partial_dispatch_result(exc) or observed
        launched = getattr(evidence, "launched", False)
        state = getattr(exc, "launch_state", None)
        if isinstance(state, LaunchState):
            launched = merge_launch_evidence(launched, state.launched)
        raise typer.Exit(1 if launched is not False else 2) from None


def _finish_resume_continuation(item_id: str, outcome: DispatchedOutcome) -> None:
    """Map the provider outcome to the CLI's continuation exit behavior."""
    exit_code = int(outcome.exit_code)
    if exit_code != 0:
        _error(
            f"kew epic continue: {item_ref(item_id)} remains available for "
            "another continuation attempt"
        )
        raise typer.Exit(1 if getattr(outcome, "launched", False) is not False else 2)


def _epic_driver_sources(
    config: DispatchConfig,
    *,
    repo: str,
    epic_prefix: str,
    store: SqliteRunStore,
    host: str | None = None,
) -> tuple[DependencySource, Any, LinearTracker | None]:
    """Build the (deps, facts, tracker) the driver needs for this tracker (L3).

    GitHub uses the source its API supports — native sub-issue/`blocked_by`
    relations when the capability probe passes, the label/marker convention
    otherwise — plus the GitHub facts reader and no tracker (the merge pass
    closes via ``gh issue close``). Linear uses the native source +
    ``LinearFactsReader`` (PRs stay GitHub-native inside it) and a
    ``LinearTracker`` so the merge pass closes the item via the Linear API.
    """
    resolved_host = host or _driver_repo_host()
    if config.tracker.type == "linear":
        import os

        from kew.graph.adapters.linear_facts import LinearFactsReader
        from kew.graph.adapters.linear_native import LinearSource, ReconcilingSource
        from kew.linear.client import LinearClient
        from kew.linear.tracker import LinearTracker as _LinearTracker

        api_key = os.environ.get("LINEAR_API_KEY", "")
        if not api_key:
            _error(
                "tracker.type is 'linear' but the LINEAR_API_KEY environment "
                "variable is not set."
            )
            raise typer.Exit(2)
        client = LinearClient(api_key)
        # Route every fetch through the reconcile short-circuit (W2/#894): once
        # Linear webhooks are delivering, a driver tick / `epic status` skips the
        # GraphQL fetch when tracker_events show nothing changed, falling back to
        # a full fetch on a fresh event or the generous reconcile interval. Cold
        # path (no events) fetches every time — today's polling, unchanged.
        deps: DependencySource = ReconcilingSource(
            LinearSource(client, in_review_state=config.tracker.linear.in_review_state),
            store=store,
            reconcile_interval_seconds=config.tracker.linear.reconcile_interval_seconds,
        )
        facts = LinearFactsReader(
            client,
            store,
            repo,
            branch_prefix=config.branches.prefix,
            host=resolved_host,
        )
        return deps, facts, _LinearTracker(config, client)
    return (
        _c.make_github_source(repo=repo, epic_prefix=epic_prefix),
        _c.GitHubFactsReader(
            repo=repo,
            store=store,
            branch_prefix=config.branches.prefix,
            host=resolved_host,
        ),
        None,
    )


def _blast_radius_str(blast_radius: tuple[str, ...]) -> str:
    """Render a blast radius as bracketed item refs — ``#N`` for GitHub numbers,
    bare for any other tracker id (#SFD-140, matching item_ref elsewhere)."""
    return "[" + ", ".join(item_ref(b) for b in blast_radius) + "]"


def _stuck_restart(node: StuckNode, name: str) -> str:
    """The operator's restart for a stuck node, as a ` — …` suffix, or ""
    when its reason has none. Shared by the tick and `kew epic status` (#2695)."""
    if node.reason == HOST_RESTART_REPEATED_PRESERVED_REASON:
        # Its work is preserved: retry would dispatch past it (#2960).
        command = fill_command(EPIC_CONTINUE_COMMAND, epic=name, item=node.node_id)
        return f" — `{command}`" if command else ""
    if node.reason in (
        HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
        HOST_RESTART_REPEATED_TAKEN_UP_REASON,
    ):
        # Continue, abandon and retry all refuse an ambiguous history, and
        # preserved work already taken up is nobody's move (#2960).
        return ""
    if node.reason.startswith("failed-terminal"):
        # A failed-terminal node is the one an operator can non-destructively
        # re-admit — hand them the exact command (#661).
        return f" — `kew epic retry {name} {node.node_id}`"
    if node.reason.startswith(f"{CLOSED_UNMERGED_REASON_PREFIX}:"):
        # Retry is the restart: the next tick dispatches it afresh and the
        # new PR becomes the linked one (#2689). A closed PR's branch left
        # on the remote refuses that dispatch, so the line says so.
        return (
            f" — restart with `kew epic retry {name} {node.node_id}` "
            "(delete the closed PR's remote branch first if it still exists)"
        )
    return ""


def _print_stuck(nodes: tuple[StuckNode, ...], name: str) -> None:
    for s in nodes:
        _warn(
            f"  stuck: {item_ref(s.node_id)} ({s.reason}) "
            f"blast_radius={_blast_radius_str(s.blast_radius)}"
            f"{_stuck_restart(s, name)}"
        )


def _print_stuck_section(nodes: tuple[StuckNode, ...], name: str) -> None:
    """The `Stuck:` section of the `kew epic status` text view, on stdout with
    the rest of it: one line per stuck node with the reason and restart the
    tick prints. A stalled epic otherwise named no node to act on (#2695)."""
    if not nodes:
        return
    typer.echo("Stuck:")
    for s in nodes:
        # The blast radius the tick's line carries, kept when there is one.
        radius = (
            f" blast_radius={_blast_radius_str(s.blast_radius)}"
            if s.blast_radius
            else ""
        )
        typer.echo(
            f"  {item_ref(s.node_id)} {s.reason}{radius}{_stuck_restart(s, name)}"
        )


def _print_waiting(nodes: tuple[StuckNode, ...]) -> None:
    """Print the non-terminal, non-progressing nodes (IN_REVIEW / HELD /
    BLOCKED) with their reason lines — the "waiting" half of a stall (#661)."""
    for s in nodes:
        _warn(
            f"  waiting: {item_ref(s.node_id)} ({s.reason}) "
            f"blast_radius={_blast_radius_str(s.blast_radius)}"
        )


def _print_withheld(result: TickResult) -> None:
    """Print the READY nodes this tick deliberately did not start, and why.

    A STALLED tick prints its whole waiting list (`_print_tick_terminal` /
    `_report_stall`); every other tick printed only what it DID. So an epic
    whose fair share of the fleet cap let through one node per tick reported a
    dispatch and nothing else — the operator had no line naming the limiter
    (#2094). These reasons carry that name, so they are promoted onto a
    progressing tick; the rest of the waiting list stays a stall-time /
    `kew epic status` concern.
    """
    if result.signal is TerminalSignal.STALLED:
        return
    _print_waiting(
        tuple(
            w for w in result.waiting if w.reason.startswith(WITHHELD_REASON_PREFIXES)
        )
    )


def _stall_dominant(
    stuck: tuple[StuckNode, ...], waiting: tuple[StuckNode, ...]
) -> str | None:
    """One-line dominant-cause summary for a STALLED tick, e.g.
    ``2 in_review awaiting merge — merge to resume`` (#661).

    Reads the driver's own ``STALL_BUCKETS`` — the single owner of the
    reason-prefix vocabulary the operator-attention event stream also reads
    (#2150), so the dominant-cause line and the stream cannot disagree about
    what a reason means or what to do about it. The hint is each reason's own
    cause hint (``kew.waits.cause_hint``) where it has one, so a credential
    containment hold is not told to map an image (#2682); distinct hints
    within the dominant bucket are all named."""
    counts: dict[str, int] = {}
    hints: dict[str, list[str]] = {}
    for s in (*stuck, *waiting):
        bucket = bucket_for(s.reason)
        if bucket is None:
            continue
        counts[bucket.label] = counts.get(bucket.label, 0) + 1
        said = hints.setdefault(bucket.label, [])
        hint = cause_hint(s.reason) or bucket.hint
        if hint not in said:
            said.append(hint)
    if not counts:
        return None
    label = max(counts, key=lambda lbl: counts[lbl])
    return f"{counts[label]} {label} — {'; '.join(hints[label])}"


def _awaiting_merge_waiting(waiting: tuple[StuckNode, ...]) -> list[StuckNode]:
    """The IN_REVIEW-awaiting-merge waiting nodes (their reason carries the PR
    number) — what an AWAITING_MERGES tick lists for the operator (Node I7)."""
    return [w for w in waiting if w.reason.startswith("in_review")]


def _print_awaiting_merges(name: str, result: TickResult) -> None:
    """Print the benign human-gate end state (Node I7): the epic's remaining
    work is PRs open in review awaiting a human's merge — not a broken
    pipeline. List each pending PR and finish on a non-alarming success line;
    a human merging them resumes the epic with no operator repair."""
    pending = _awaiting_merge_waiting(result.waiting)
    for w in pending:
        _log(f"  awaiting merge: {item_ref(w.node_id)} ({w.reason})")
    count = len(pending)
    noun = "PR" if count == 1 else "PRs"
    _success(
        f"epic {name}: AWAITING_MERGES — {count} {noun} awaiting a human merge; "
        "merge to resume."
    )


def _dispatch_ranks_str(result: TickResult) -> str:
    """Why the frontier chose what it dispatched: priority and blast radius
    per node, in dispatch order (#2575). Empty when the tick did not say."""
    if not result.dispatch_ranks:
        return ""
    return " (" + "; ".join(r.describe() for r in result.dispatch_ranks) + ")"


def _print_tick_categories(result: TickResult) -> None:
    """Print the per-category node lists (dispatched/merged/held/failed/…)."""
    if result.dispatched:
        _log(f"  dispatched: {list(result.dispatched)}{_dispatch_ranks_str(result)}")
    if result.merged:
        _success(f"  merged: {list(result.merged)}")
    if result.closed:
        _success(f"  closed (merged out-of-band): {list(result.closed)}")
    if result.held:
        _log(f"  held: {list(result.held)}")
    if result.in_flight:
        _log(f"  in_flight: {list(result.in_flight)}")
    if result.failed:
        _warn(f"  failed: {list(result.failed)}")
    if result.cancelled:
        _warn(f"  cancelled: {list(result.cancelled)}")
    for node_id, why in result.gate_skipped:
        _warn(f"  pre-review gate skipped: {item_ref(node_id)} — {why}")


def _print_tick_terminal(name: str, result: TickResult) -> None:
    """Print the terminal-signal verdict (COMPLETE / AWAITING_MERGES / STALLED)."""
    if result.signal is TerminalSignal.COMPLETE:
        _success(f"epic {name}: COMPLETE")
    elif result.signal is TerminalSignal.AWAITING_MERGES:
        _print_awaiting_merges(name, result)
    elif result.signal is TerminalSignal.STALLED:
        _print_waiting(result.waiting)
        dominant = _stall_dominant(result.stuck, result.waiting)
        if dominant is not None:
            _error(f"epic {name}: STALLED — {dominant}")
        else:
            _error(f"epic {name}: STALLED — needs an operator")


def _points_suffix(result: TickResult) -> str:
    """The tick's GitHub GraphQL spend for its summary line (#2582), or nothing
    when no reader metered it."""
    if result.graphql_points is None:
        return ""
    return f" graphql_points={result.graphql_points}"


def _print_tick_result(name: str, result: TickResult, config: DispatchConfig) -> None:
    """Print a readable summary of one `advance()` tick."""
    if result.locked_out:
        _warn(_c._locked_out_message(name, config))
        return
    _log(f"epic {name}: tick signal={result.signal.value}{_points_suffix(result)}")
    _print_tick_categories(result)
    _print_stuck(result.stuck, name)
    _print_withheld(result)
    _log(f"  done={len(result.done)} remaining={result.remaining}")
    _print_tick_terminal(name, result)


def _print_pr_links(links: tuple[PrLink, ...]) -> None:
    """Print each PR link's provenance and closing-keyword warning."""
    if not links:
        return
    rendered = ", ".join(
        f"{item_ref(link.node_id)}->PR #{link.pr_number} ({link.source})"
        for link in links
    )
    typer.echo(f"PR linkage: {rendered}")
    for link in links:
        if link.has_closing_ref is False:
            _warn(
                f"{item_ref(link.node_id)}: PR #{link.pr_number} lacks its "
                "GitHub closing keyword"
            )


def _print_tick_summary(name: str, summary: TickSummary) -> None:
    """Print a read-only `kew epic status` report."""
    counts = ", ".join(f"{k}={v}" for k, v in sorted(summary.counts.items()))
    typer.echo(f"Status counts: {counts}")
    typer.echo(f"Ready to dispatch: {list(summary.frontier)}")
    if summary.in_flight:
        typer.echo(f"In flight: {list(summary.in_flight)}")
    if summary.held:
        typer.echo(f"Held: {list(summary.held)}")
    if summary.failed:
        typer.echo(f"Failed: {list(summary.failed)}")
    if summary.cancelled:
        typer.echo(f"Cancelled: {list(summary.cancelled)}")
    _print_pr_links(summary.pr_links)
    _print_stuck_section(summary.stuck, name)
    _print_waiting(summary.waiting)
    typer.echo(f"Done: {len(summary.done)} remaining={summary.remaining}")
    typer.echo(f"Signal: {summary.signal.value}")


@dataclass(frozen=True)
class _EpicRead:
    """One read-only pass over an epic: what `status` prints and `watch` reads."""

    config: DispatchConfig
    deps: DependencySource
    summary: TickSummary
    retained_claims: list[str]
    liveness: DriverLiveness | None = None
    graph: Graph | None = None
    gate_skips: tuple[str, ...] = ()
    """One line per node whose last gated head the pre-review gate skipped."""


def _read_epic(name: str, epic_prefix: str) -> _EpicRead:
    """Summarize an epic without dispatching or merging (`status`/`watch`).

    Not write-free: the facts readers reconcile `awaiting_publication` runs
    whose PR is observably open or merged, so a read can terminal such a run
    and emit a `publication_reconciled` audit event."""
    config = _c.load_config()
    # PRs live on GitHub for both trackers, so status still shells out to
    # `gh` (repo detection + PR facts) — preflight regardless of tracker
    # type, mirroring _epic_driver_config. The facts reader must come from
    # the tracker-aware factory: hardwiring GitHubFactsReader here crashed
    # the first live Linear drive (pilot-rivets) on non-numeric item ids.
    _c.preflight_tools()
    repo = _c._epic_repo(config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        # Build the graph from the driver's own source (the Linear reconcile
        # short-circuit needs the store) so `epic status` honors the same
        # event-driven fetch gate a driver tick does (W2/#894).
        deps, facts, _tracker = _c._epic_driver_sources(
            config, repo=repo, epic_prefix=epic_prefix, store=store
        )
        graph = _c._load_epic_graph(name, epic_prefix, source=deps)
        _c.prime_issue_snapshots_from(deps, facts)
        try:
            summary = _c.summarize(name, graph, config=config, store=store, facts=facts)
        except LinearApiError as exc:
            _error(_linear_failure_message(name, exc))
            raise typer.Exit(2) from exc
        retained_claims = retained_claim_lines(store, graph.nodes)
        # A finished node's skip is history: only open work prints one.
        gate_skips = gate_skip_lines(store, graph.nodes, statuses=summary.statuses)
        # Liveness is read here, while the store is open, and from the lock
        # file's own pid/heartbeat as well as the tick heartbeat (#2297): a
        # dead holder pid is conclusive where a heartbeat age alone is not.
        liveness = driver_liveness(
            store,
            name,
            lock=DriverLockFacts.from_holder(
                _c.lock_holder(_c._epic_lock_path(config, name))
            ),
            expected_interval_s=float(config.epic.driver_expected_interval_seconds),
        )
    return _EpicRead(
        config, deps, summary, retained_claims, liveness, graph, gate_skips
    )


#: The ``schema`` field of ``kew epic status --json``; bumped on a breaking change.
EPIC_STATUS_SCHEMA_VERSION = 1


@epic_app.command("status")
def epic_status(
    name: str,
    epic_prefix: str = "epic:",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of text."),
    ] = False,
) -> None:
    """Summarize epic progress: node counts by status, the ready frontier, stuck
    nodes + blast radius, and the terminal signal — including `awaiting_merges`
    when a human merge is all that is left. Never dispatches or merges.

    Not write-free: the facts readers reconcile `awaiting_publication` runs
    whose PR is observably open or merged, so a status read can terminal such a
    run and emit a `publication_reconciled` audit event."""
    read = _read_epic(name, epic_prefix)
    deps = read.deps
    linkage_description = getattr(deps, "linkage_description", "")
    has_membership_key = bool(getattr(deps, "membership_key", False))
    if (
        read.config.tracker.type == "github"
        and has_membership_key
        and not getattr(deps, "linkage_reported", False)
    ):
        # stderr under --json, so stdout stays one parseable document.
        typer.echo(str(linkage_description), err=json_output)
    holder = _c.lock_holder(_c._epic_lock_path(read.config, name))
    stalled = holder is not None and holder.is_stalled(now=datetime.now(UTC))
    read = _with_stalled_lock(read, stalled)
    if json_output:
        typer.echo(
            json.dumps(_epic_status_document(name, read, holder, stalled), indent=2)
        )
        return
    _c._print_tick_summary(name, read.summary)
    for warning in read.summary.token_warnings:
        typer.echo(f"Usage warning: {warning.reason()}")
    for line in read.retained_claims:
        typer.echo(line)
    for line in read.gate_skips:
        typer.echo(line)
    _print_lock_holder(name, holder, stalled)
    _print_driver_liveness(name, read.liveness)


def _with_stalled_lock(read: _EpicRead, stalled: bool) -> _EpicRead:
    """``read`` as a stalled driver lock leaves it: nothing moves while a
    stopped live process holds the lock, so an unfinished epic is STALLED
    (#3331)."""
    if not stalled or read.summary.signal is TerminalSignal.COMPLETE:
        return read
    return replace(read, summary=replace(read.summary, signal=TerminalSignal.STALLED))


def _print_lock_holder(name: str, holder: Any, stalled: bool) -> None:
    if holder is None:
        return
    state = "holds" if holder.is_alive() else "has a STALE hold on"
    typer.echo(f"Poller: {holder.describe()} {state} epic {name}'s driver lock")
    if stalled:
        _warn(f"epic {name}: STALLED — {_stalled_lock_line(holder)}")


def _epic_status_document(
    name: str, read: _EpicRead, holder: Any, stalled: bool = False
) -> dict[str, Any]:
    """The ``kew epic status --json`` document, from the read the text prints."""
    summary = read.summary
    stuck = {s.node_id: s.reason for s in summary.stuck}
    waiting = {w.node_id: w.reason for w in summary.waiting}
    return {
        "schema": EPIC_STATUS_SCHEMA_VERSION,
        "epic": name,
        "signal": summary.signal.value,
        "counts": dict(sorted(summary.counts.items())),
        "nodes": _epic_status_nodes(read.graph, summary.statuses, stuck, waiting),
        "frontier": list(summary.frontier),
        "in_flight": list(summary.in_flight),
        "held": list(summary.held),
        "failed": list(summary.failed),
        "cancelled": list(summary.cancelled),
        "done": list(summary.done),
        "remaining": summary.remaining,
        "stuck": [_stuck_entry(s) for s in summary.stuck],
        "waiting": [_stuck_entry(w) for w in summary.waiting],
        "pr_links": [
            {
                "id": link.node_id,
                "pr_number": link.pr_number,
                "source": link.source,
                "has_closing_ref": link.has_closing_ref,
            }
            for link in summary.pr_links
        ],
        "retained_claims": list(read.retained_claims),
        "token_warnings": [
            {"runner": w.runner, "reason": w.reason(), "identity": w.identity}
            for w in summary.token_warnings
        ],
        "lock_holder": None
        if holder is None
        else {
            "holder": holder.describe(),
            "alive": holder.is_alive(),
            "stalled": stalled,
            "action": driver_lock_stalled_action(holder.pid) if stalled else None,
        },
        "driver": None if read.liveness is None else asdict(read.liveness),
    }


def _stuck_entry(node: StuckNode) -> dict[str, Any]:
    return {
        "id": node.node_id,
        "reason": node.reason,
        "blast_radius": list(node.blast_radius),
    }


def _epic_status_nodes(
    graph: Graph | None,
    statuses: dict[str, str],
    stuck: dict[str, str],
    waiting: dict[str, str],
) -> list[dict[str, Any]]:
    """Every node with its title, driver status and dependencies.

    The status is the driver's overlay reading where the summary has one, else
    the provider's own status (an invalid graph is summarized without one)."""
    if graph is None:
        return []
    return [
        {
            "id": node.id,
            "title": node.title,
            "status": statuses.get(node.id, node.status.value),
            "dependencies": graph.dependencies_of(node.id),
            "external": node.external,
            "url": node.url,
            "pr_url": node.pr_url,
            "stuck": stuck.get(node.id),
            "waiting": waiting.get(node.id),
        }
        for node in sorted(graph.nodes.values(), key=lambda n: item_id_sort_key(n.id))
    ]


@epic_app.command("heartbeat")
def epic_heartbeat(
    name: str,
    waiting: Annotated[
        str,
        typer.Option(
            "--waiting",
            help="Why the loop is not ticking right now, e.g. `slot`.",
        ),
    ],
) -> None:
    """Say the epic's loop is alive and waiting on purpose.

    A loop parked in a shared slot gate does not tick, and to kew a loop that
    does not tick looks dead. Call this on each wait: inside twice the driver's
    cadence the console reads the driver `waiting`, not gone, and does not
    count it as down. Writes one ledger row per epic, overwritten each call;
    it dispatches nothing and takes no lock.
    """
    reason = waiting.strip()
    if not reason:
        _error("--waiting needs a reason, e.g. `--waiting slot`.")
        raise typer.Exit(2)
    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        beat = record_driver_waiting(store, name, reason)
    typer.echo(f"{name}: waiting ({reason}) at {beat.at}")


def _print_driver_liveness(name: str, liveness: DriverLiveness | None) -> None:
    """One line for the driver's liveness READING — including `unknown`, which
    is not "not gone": it means no cadence was recorded to threshold the
    heartbeat against (#2297), and saying so beats painting a dead driver
    green."""
    if liveness is None or not liveness.present:
        return
    age = "" if liveness.age_s is None else f" (last beat {int(liveness.age_s)}s ago)"
    cadence = (
        ""
        if liveness.interval_s is None
        else f", cadence {liveness.interval_s:g}s ({liveness.cadence_source})"
    )
    line = f"Driver: {liveness.status}{age}{cadence}"
    if liveness.status == DRIVER_GONE:
        _warn(f"epic {name}: {line}")
    else:
        typer.echo(line)


def _emit_attention(
    name: str,
    events: tuple[AttentionEvent, ...],
    *,
    json_output: bool,
    announce_empty: bool = True,
    observations_at: str = "",
) -> None:
    """Write the operator-attention events — one JSON object per line under
    `--json`, one human line otherwise. An empty stream SAYS it is empty in
    human mode rather than printing nothing an operator has to interpret —
    once, on the first poll: a `--follow` watch repeating it every interval
    would bury the events it exists to show.

    `observations_at` is the instant the facts behind these events were LAST
    RECORDED, set only on the journal-backed read (#2469). It rides on each
    JSON object as `observations_stale_since` and is printed once in human
    mode: a watch that answers in milliseconds off the driver's last tick must
    say so, or an operator reads a quiet epic as a current one."""
    if observations_at and not json_output:
        _log(f"epic {name}: observations stale since {observations_at}")
    if not events:
        if announce_empty and not json_output:
            _log(f"epic {name}: nothing is waiting on a person")
        return
    for event in events:
        if not json_output:
            typer.echo(f"epic {name}: {event.as_line()}")
            continue
        wire = event.as_dict()
        if observations_at:
            wire["observations_stale_since"] = observations_at
        typer.echo(json.dumps(wire, sort_keys=True))


def watch_memory_path(config: DispatchConfig, name: str) -> Path:
    """Where a `--follow` watch remembers what it has emitted (#2202): the
    epic's own driver-lock path plus a suffix, so the memory is per-epic
    exactly as the lock is and can never BE the lock.

    Deriving it from the WHOLE lock filename is the point. A fixed
    `epic-watch-<slug>.json` in the lock's directory collides with a configured
    ``[epic] lock_path = ".../epic-watch.json"``, and each poll's `os.replace`
    then swaps the held lock out from under its own directory entry — a second
    driver creates and flocks a fresh file and ticks the same epic."""
    lock = _epic_lock_path(config, name)
    return lock.with_name(f"{lock.name}.watch.json")


def _watch_stream(
    config: DispatchConfig, name: str, *, follow: bool, replay: bool
) -> AttentionStream:
    """The memory this watch runs on: persisted under `--follow`, in-RAM for a
    snapshot — a snapshot is a probe read on every run, so it never goes silent
    and never silences the follow after it."""
    if not follow:
        return AttentionStream(epic=name)
    return AttentionStream.following(
        name, watch_memory_path(config, name), replay=replay
    )


def _announce_resumed(name: str, stream: AttentionStream) -> None:
    """Say why a restart is quiet. A `--follow` watch that resumes a memory of
    still-open states emits nothing, and a silent watch reads as a broken one
    (#2202) — so name the states it is holding its peace about."""
    if stream.emitted or not stream.resumed:
        return
    plural = "" if stream.resumed == 1 else "s"
    _log(
        f"epic {name}: {stream.resumed} open state{plural} already reported "
        "— watching for changes"
    )


@dataclass(frozen=True)
class _WatchRead:
    """One poll's attention events, and when the facts behind them were read.

    ``observations_at`` is empty for a live read (the facts are this instant's)
    and carries the instant the ledger-backed one's facts were last recorded —
    the driver's last finished tick, not the dedup'd journal row (#2469)."""

    config: DispatchConfig
    events: tuple[AttentionEvent, ...]
    observations_at: str = ""


def _journaled_watch_read(name: str) -> _WatchRead | None:
    """A watch read off the driver's own last journaled tick (#2469).

    No `gh` call, no repo detection, no driver lock, and a READ-ONLY ledger
    open — a watch is read-only, so it must never wait on the tick holding
    either. That is what the 90 s went on: `_read_epic` re-queries the forge
    per node (a `gh pr view` per unbatched PR candidate, a head-check read per
    approved one), and a 23-node epic spent 92.6 s wall on 3.8 s of CPU while
    operator pollers timed out at 240 s.

    ``None`` when there is nothing this path can read — no ledger yet, an epic
    no tick has journaled, a ledger it cannot query (see
    :func:`_journaled_events`), or a last tick whose snapshot records no
    observations this build can read whole (an invalid graph, or a status word
    only a newer kew has) — which is the caller's signal to pay for a live read
    rather than report an unticked or frozen epic as quiet."""
    phases: dict[str, float] = {}
    mark = time.monotonic()

    def _phase(label: str) -> None:
        nonlocal mark
        now = time.monotonic()
        phases[label] = now - mark
        mark = now

    config = _c.load_config()
    _phase("config")
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        return None
    read = _journaled_events(name, db_path, _phase)
    if read is None:
        return None
    # The per-phase measurement #2469 asked for, kept as a debug line rather
    # than a CLI surface: it is how the next regression gets attributed, and an
    # operator's poller must not have to parse it.
    logging.getLogger(__name__).debug(
        "epic %s: journaled watch read in %s",
        name,
        ", ".join(f"{label}={seconds:.3f}s" for label, seconds in phases.items()),
    )
    events, recorded_at = read
    return _WatchRead(config, events, recorded_at)


def _journaled_events(
    name: str, db_path: Path, phase: Callable[[str], None]
) -> tuple[tuple[AttentionEvent, ...], str] | None:
    """This epic's journaled events and the instant they were recorded.

    ``None`` when the ledger has nothing to answer with: no journaled tick, a
    query this build cannot run against it, or an open only the read-write path
    can perform. The read-only open is the point of this path and it
    deliberately SKIPS `_migrate` — a reader must not bump the schema under a
    live older process — so the ledger it reads may predate a table or column
    this build queries, `epic_ticks` itself on one written before the journal;
    and a legacy ledger with no registry entry and no identity binding is
    refused outright, because adopting one is a WRITE. Each is a reason to pay
    for the LIVE read, which opens read-write, migrates and adopts: a watch
    documented as exiting `1` when something is waiting on a person must not
    exit `1` on a traceback, or `2` on a refusal that only this probe makes,
    instead.

    Only the cost changes when this happens — the fallback answers in full — so
    it is attributed on the same debug line as the phases rather than warned
    about on an operator's every poll."""
    try:
        # `_managed_store`, not `_open_run_store`: a refused open is not this
        # path's answer but its signal to read live, and the shared boundary
        # would have reported the refusal and exited 2 first (see below).
        with contextlib.closing(
            _c._managed_store(db_path.resolve(), read_only=True)
        ) as store:
            phase("ledger_open")
            observations = read_journaled_observations(
                store.latest_epic_tick(name),
                # The heartbeat row dates the snapshot: `epic_ticks` dedups on
                # the snapshot hash, so a held epic's latest row freezes at the
                # instant its state first appeared and cannot tell a driver
                # that ticked a minute ago from one that died days ago. One
                # point read on the connection already open (#2476 review).
                driver_state=store.get_epic_driver_state(name),
            )
            phase("journal_read")
            if observations is None:
                return None
            events = journaled_attention_events(name, observations, store=store)
            phase("compose")
            return events, observations.recorded_at
    except (sqlite3.Error, ValueError, LedgerIdentityError, SchemaTooNewError):
        # ValueError covers the snapshot/actions JSON columns: a row this build
        # cannot decode is one more reason to read live, not to crash.
        # LedgerIdentityError/SchemaTooNewError are the opens this path cannot
        # perform but the live one can or must report: a legacy unregistered
        # ledger is refused ONLY read-only (the read-write open adopts and
        # registers it), and a genuinely wrong or too-new ledger is the live
        # open's refusal to word and exit on, not this probe's.
        logging.getLogger(__name__).debug(
            "epic %s: the journaled watch read could not query %s; "
            "falling back to the live read",
            name,
            db_path,
            exc_info=True,
        )
        return None


def _live_watch_read(name: str, epic_prefix: str) -> _WatchRead:
    """The full read `kew epic status` runs: every node's facts, live."""
    read = _read_epic(name, epic_prefix)
    return _WatchRead(read.config, attention_events(name, read.summary))


def _watch_poll(
    name: str,
    epic_prefix: str,
    stream: AttentionStream | None,
    *,
    json_output: bool,
    follow: bool,
    replay: bool,
) -> AttentionStream:
    """One poll: read the epic and emit what it has not already said.

    A one-shot poll reads the driver's journaled observations and falls back to
    a live read only when nothing is recorded (#2469). A `--follow` watch keeps
    reading live: it is a process of its own with its own cadence, and the
    whole point of it is to see a state change at the instant it happens.

    The stream is built on the FIRST poll, from that read's config — the
    persisted memory lives under the epic's own state directory, so there is
    nothing to load until the epic has been read once."""
    read = None if follow else _journaled_watch_read(name)
    if read is None:
        read = _live_watch_read(name, epic_prefix)
    first = stream is None
    if stream is None:
        stream = _watch_stream(read.config, name, follow=follow, replay=replay)
    events = read.events
    _emit_attention(
        name,
        stream.fresh(events),
        json_output=json_output,
        announce_empty=first and not events,
        observations_at=read.observations_at,
    )
    if first:
        _announce_resumed(name, stream)
    # Only once it is OUT: an emit that raises must leave the memory replaying
    # the event, never recording it as already said.
    stream.commit()
    return stream


@epic_app.command("watch")
def epic_watch(
    name: str,
    json_output: bool = typer.Option(
        False, "--json", help="Emit one JSON object per event (JSON Lines)."
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Keep polling, emitting each new state once, until --timeout or Ctrl-C.",
    ),
    interval: float = typer.Option(
        30.0, "--interval", help="Seconds between polls under --follow."
    ),
    timeout: float = typer.Option(
        0.0,
        "--timeout",
        help="Give up after this many seconds under --follow (0 = never).",
    ),
    replay: bool = typer.Option(
        False,
        "--replay",
        help="Ignore what earlier --follow watches emitted: every open state once.",
    ),
    epic_prefix: str = "epic:",
) -> None:
    """Emit one structured event per epic state that needs a person.

    One stream for every operator-attention state the driver used to scatter
    across a ledger verdict, a tick line, a review log line, a sandbox warning
    and `kew epic status`: merge-ready PRs, held nodes, an exhausted budget, a
    held publication, a refused sandbox, and a complete epic.

    Under `--follow` it keeps polling after an event and emits each OPEN state
    once — an event repeats when its identity changes (a new head or
    review round for a review state, a new run for a publication one), and when
    the state cleared and then came back. That memory persists under the epic's
    state directory, so a RESTARTED watch does not repeat itself;
    `--replay` ignores it and emits every open state once.

    A SNAPSHOT watch (no `--follow`) exists to be polled, so it is priced like
    it: it composes the events from the observations the driver
    recorded on its last tick, with no host call per node and a read-only
    ledger open — a mid-drive tick holding the lock and the ledger delays it by
    nothing. Each event names `observations_stale_since`, the driver's last
    finished tick, so a poller can age them; an epic no tick has journaled yet
    — or a ledger the read-only open cannot query, having skipped its migration
    — falls back to the live read.

    Exits `1` when it emitted at least one event, `0` when nobody was waited on,
    and `2` when the epic could not be read. Read-only in the same sense as
    `kew epic status` — it never dispatches or merges."""
    deadline = time.monotonic() + timeout if timeout > 0 else None
    stream: AttentionStream | None = None
    try:
        while True:
            stream = _watch_poll(
                name,
                epic_prefix,
                stream,
                json_output=json_output,
                follow=follow,
                replay=replay,
            )
            if not follow:
                break
            if deadline is not None and time.monotonic() >= deadline:
                if not stream.emitted:
                    _warn(
                        f"epic {name}: no operator-attention event within {timeout:g}s"
                    )
                break
            _c._sleep(interval)
    except KeyboardInterrupt:
        # A follow watch ends at the operator's Ctrl-C: report what it found,
        # not a traceback.
        pass
    raise typer.Exit(1 if stream is not None and stream.emitted else 0)


@epic_app.command("advance")
def epic_advance(
    name: str,
    max_parallel: int | None = typer.Option(
        None, "--max-parallel", help="Override config.epic.max_parallel for this tick."
    ),
    takeover: bool = typer.Option(
        False,
        "--takeover",
        help="Also reclaim a legacy lock with no heartbeat (a provably-stale "
        "dead-PID or stale-heartbeat lock is reclaimed automatically).",
    ),
    epic_prefix: str = "epic:",
) -> None:
    """Run ONE tick of the epic driver: dispatch ready nodes, merge approved
    PRs, and print the resulting status."""
    config, repo, resolved_max_parallel, approvers = _c._epic_driver_config(
        max_parallel
    )
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve config WITH the store so DB config_overrides govern the
        # driver's budget-hold reconciliation, exactly as they govern the child
        # it spawns (#892/O1). Overridable keys don't touch repo/max_parallel/
        # approvers, so those stay as `_epic_driver_config` resolved them.
        config = _c.load_config(store=store)
        dispatch_sink = _c.DetachedDispatchSink(
            epic_ref=name, config=config, store=store
        )
        deps, facts, tracker = _c._epic_driver_sources(
            config, repo=repo, epic_prefix=epic_prefix, store=store
        )
        # Wire the concrete production reviewer launcher when the governed
        # adversarial-review stage is enabled (#991): flipping [review].adversarial
        # must actually dispatch a fresh-context reviewer, never a loud no-op.
        reviewer_launcher = (
            sandbox_reviewer_launcher(config, store)
            if config.review.adversarial
            else None
        )
        # The NEEDS_CHANGES remediation leg (#1030): wire the real remediator so a
        # node parked in `remediating` actually dispatches the #987 path instead of
        # stalling there forever (the "built but unwired" gap this closes).
        remediator = (
            sandbox_remediator(config, store, epic_ref=name)
            if config.review.adversarial
            else None
        )
        # The merge-main step (#2149): a READY branch that stopped merging into
        # main needs the base integrated under a kew-owned producer, or an
        # operator merges it by hand and the next remediation is refused (#2124).
        base_integrator = (
            sandbox_base_integrator(config, store, epic_ref=name)
            if config.review.adversarial
            else None
        )
        try:
            result = _c.advance(
                name,
                config=config,
                store=store,
                deps=deps,
                facts=facts,
                dispatch_sink=dispatch_sink,
                max_parallel=resolved_max_parallel,
                now=_c.datetime.now(UTC),
                lock_path=_c._epic_lock_path(config, name),
                approvers=approvers,
                repo=repo,
                base="main",
                # A one-shot advance has no cadence of its own, so it records
                # the configured EXPECTATION in its own column and leaves
                # `interval_s` to `finish_epic_tick`, which retires a recorded
                # cadence only once this drive's own gap proves nothing kept up
                # with it (#2297 + review). Without the expectation every drive
                # left a NULL interval and liveness could never fire; without
                # the retirement a dead poller's 60s stuck forever and a
                # 5-minute cron read `gone` two minutes after a SUCCESSFUL
                # advance.
                interval_s=None,
                expected_interval_s=float(config.epic.driver_expected_interval_seconds),
                holder=name,
                takeover=takeover,
                tracker=tracker,
                reviewer_launcher=reviewer_launcher,
                remediator=remediator,
                base_integrator=base_integrator,
                review_detacher=_review_detacher(config),
                operator_stop=max_parallel == 0,
            )
        except EmptyEpicGraphError as exc:
            _error(_empty_epic_message(exc.name, config, epic_prefix))
            raise typer.Exit(2) from exc
        except LinearApiError as exc:
            _error(_linear_failure_message(name, exc))
            raise typer.Exit(2) from exc
    _c._print_tick_result(name, result, config)


_sleep: Callable[[float], None] = time.sleep
"""Injectable sleep seam for `epic_run`'s poll loop — tests patch this to
avoid actually sleeping."""


def _advance_epic(
    name: str,
    *,
    config: DispatchConfig,
    store: SqliteRunStore,
    deps: DependencySource,
    facts: Any,
    repo: str,
    max_parallel: int,
    approvers: list[str],
    interval: int,
    takeover: bool = False,
    tracker: LinearTracker | None = None,
    operator_stop: bool = False,
) -> TickResult:
    """Run one `advance()` tick for epic `name`, on this epic's OWN lock
    (`_epic_lock_path(config, name)`) and stamping `holder=name` so `kew epic
    status` can name who holds the poller (#861/F2). Per-epic keying lets the
    `--all` round-robin advance several epics concurrently. The concrete reviewer
    launcher is wired when the governed adversarial-review stage is enabled
    (#991) — the same launcher `kew epic run` and `--all` drive through."""
    reviewer_launcher = (
        sandbox_reviewer_launcher(config, store) if config.review.adversarial else None
    )
    remediator = (
        sandbox_remediator(config, store, epic_ref=name)
        if config.review.adversarial
        else None
    )
    base_integrator = (
        sandbox_base_integrator(config, store, epic_ref=name)
        if config.review.adversarial
        else None
    )
    return _c.advance(
        name,
        config=config,
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=_c.DetachedDispatchSink(
            epic_ref=name, config=config, store=store
        ),
        max_parallel=max_parallel,
        now=_c.datetime.now(UTC),
        lock_path=_c._epic_lock_path(config, name),
        approvers=approvers,
        repo=repo,
        base="main",
        interval_s=float(interval),
        holder=name,
        takeover=takeover,
        tracker=tracker,
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=base_integrator,
        review_detacher=_review_detacher(config),
        operator_stop=operator_stop,
    )


def _review_detacher(config: DispatchConfig) -> DetachedReviewSpawner | None:
    """The review stage's detached launcher (#2459): every reviewer,
    remediation and merge-main step runs in a ``kew epic _review-node`` child,
    so a tick never waits on one. None when the stage is off."""
    return (
        DetachedReviewSpawner(config, base="main")
        if config.review.adversarial
        else None
    )


def _report_stall(name: str, result: TickResult) -> None:
    """Print a STALLED epic's per-node reasons + dominant-cause line (#661)."""
    _print_stuck(result.stuck, name)
    _print_waiting(result.waiting)
    dominant = _stall_dominant(result.stuck, result.waiting)
    detail = f" — {dominant}" if dominant is not None else " — needs an operator"
    _error(f"epic {name}: STALLED{detail}.")


# Exit code for a STALLED epic under the two-axis scheme (#757, #1943). A
# STALLED tick ran and needs an operator (a repair, a hold release, or a
# merge the driver cannot observe), so it is the repair code. Code 2 stays
# reserved for a driver that could not run.
_STALL_EXIT_REPAIR = 1


def _worst_exit_code(codes: Iterable[int]) -> int:
    """Aggregate per-epic exit codes for ``--all`` (#757): any repair (``1``)
    beats complete or locked out (``0``)."""
    return _STALL_EXIT_REPAIR if _STALL_EXIT_REPAIR in set(codes) else 0


# Exit code -> the operator-facing exit classification label for the `--all`
# fleet summary. Locked-out epics exit 0 like complete ones but get their own
# label since a live driver elsewhere owns them — see `_fleet_classification`.
_EXIT_CLASSIFICATION: dict[int, str] = {
    0: "complete",
    _STALL_EXIT_REPAIR: "stalled",
}


def _fleet_classification(
    terminal: dict[str, int], locked_out: set[str]
) -> dict[str, str]:
    """Map each terminal epic to its exit classification label for the combined
    `--all` fleet summary (F3 / #863). Locked-out epics are labelled distinctly
    from the complete bucket they share an exit code with."""
    return {
        name: "locked-out" if name in locked_out else _EXIT_CLASSIFICATION[code]
        for name, code in terminal.items()
    }


def _print_fleet_summary(terminal: dict[str, int], locked_out: set[str]) -> None:
    """Print ONE combined per-epic classification summary at the end of an
    `--all` run (F3 / #863). Every driven epic is named under its exit
    classification, so a mixed-outcome fleet stays legible even when the
    worst-wins aggregate exit only surfaces the repair stalls."""
    classification = _fleet_classification(terminal, locked_out)
    buckets: dict[str, list[str]] = {}
    for name, label in classification.items():
        buckets.setdefault(label, []).append(name)
    _log(f"kew epic run --all: fleet summary — {len(classification)} epic(s):")
    for label, printer in (
        ("complete", _success),
        ("locked-out", _warn),
        ("stalled", _error),
    ):
        names = sorted(buckets.get(label, ()))
        if names:
            printer(f"  {label}: {names}")


def _report_epic_tick(name: str, result: TickResult) -> TerminalSignal:
    """Print one epic's per-tick signal line and, if terminal, its outcome
    (COMPLETE success or STALLED reasons). Returns the tick's signal so the
    `--all` loop can track which epics are done."""
    _log(
        f"epic {name}: signal={result.signal.value} "
        f"done={len(result.done)} remaining={result.remaining}"
        f"{_points_suffix(result)}"
    )
    _print_withheld(result)
    if result.signal is TerminalSignal.COMPLETE:
        _success(f"epic {name}: COMPLETE — {len(result.done)} node(s) done.")
    elif result.signal is TerminalSignal.AWAITING_MERGES:
        _print_awaiting_merges(name, result)
    elif result.signal is TerminalSignal.STALLED:
        _report_stall(name, result)
    return result.signal


def _scope_epics_to_tracker(
    names: list[str], trackers: dict[str, str | None], active: str
) -> list[str]:
    """Return the epics ``--all`` should drive: those on the active project's
    tracker, dropping (with a one-line skip note) any whose recorded tracker
    differs (#882/B1). An epic with no known tracker (freshly planned, never
    driven — ``trackers[name]`` is None) is driven under the active tracker
    rather than guessed away."""
    driven: list[str] = []
    for name in names:
        tracker = trackers.get(name)
        if tracker is not None and tracker != active:
            _log(
                f"epic {name}: (skipped: tracker={tracker}) — active tracker"
                f" is {active}."
            )
        else:
            driven.append(name)
    return driven


def _fleet_advance(
    name: str,
    *,
    db: str,
    repo: str,
    epic_prefix: str,
    pin: _DriverPin,
    max_parallel_override: int | None,
    interval: int,
    takeover: bool,
) -> tuple[TickResult | None, int | None, DispatchConfig]:
    """Advance one epic on its own store/config.

    Returns ``(result, terminal_code, config)``. ``terminal_code`` is non-None
    only when an exception marks the epic terminal (repair exit); on success it
    is None and ``result`` carries the tick outcome. ``config`` is the
    store-resolved config used for any follow-up diagnostics.
    """
    with _c._open_run_store(db) as store:
        # Re-resolve config WITH the store so DB config_overrides govern
        # enforcement for every epic on the roster, same as the single
        # and child paths (#892/O1).
        config, max_parallel, approvers = _reload_driver_controls(
            store,
            pin=pin,
            max_parallel_override=max_parallel_override,
        )
        deps, facts, tracker = _c._epic_driver_sources(
            config,
            repo=repo,
            epic_prefix=epic_prefix,
            store=store,
            host=pin.identity[2],
        )
        try:
            result = _advance_epic(
                name,
                config=config,
                store=store,
                deps=deps,
                facts=facts,
                repo=repo,
                max_parallel=max_parallel,
                approvers=approvers,
                interval=interval,
                takeover=takeover,
                tracker=tracker,
                operator_stop=max_parallel_override == 0,
            )
        except EmptyEpicGraphError:
            # One epic resolving EMPTY must not silently vanish or read
            # as COMPLETE (#819): surface its diagnostic, mark it as
            # needing an operator (worst exit), and keep driving the
            # rest of the roster.
            _error(_empty_epic_message(name, config, epic_prefix))
            return None, _STALL_EXIT_REPAIR, config
        except LinearApiError as exc:
            # A Linear failure hits every epic on the roster: surface the
            # classified diagnostic, mark this one as needing an operator,
            # and keep going (the rest will classify the same way).
            _error(_linear_failure_message(name, exc))
            return None, _STALL_EXIT_REPAIR, config
    return result, None, config


def _terminal_code_for_signal(signal: TerminalSignal) -> int | None:
    """Map a terminal tick signal to its aggregate exit code, or None if not terminal.

    COMPLETE -> 0. AWAITING_MERGES remains live so an external merge can be
    observed. STALLED -> its repair code.
    See ``_worst_exit_code`` (Node I7 / #757).
    """
    if signal is TerminalSignal.COMPLETE:
        return 0
    if signal is TerminalSignal.STALLED:
        return _STALL_EXIT_REPAIR
    return None


def _tick_fleet_epic(
    name: str,
    *,
    db: str,
    repo: str,
    epic_prefix: str,
    pin: _DriverPin,
    max_parallel_override: int | None,
    interval: int,
    takeover: bool,
    terminal: dict[str, int],
    locked_out: set[str],
) -> None:
    """Advance one epic once and record its terminal state (if any) into the maps."""
    result, terminal_code, config = _fleet_advance(
        name,
        db=db,
        repo=repo,
        epic_prefix=epic_prefix,
        pin=pin,
        max_parallel_override=max_parallel_override,
        interval=interval,
        takeover=takeover,
    )
    if terminal_code is not None:
        terminal[name] = terminal_code
        return
    assert result is not None  # terminal_code is None => success path
    if result.locked_out:
        # Another live driver already holds THIS epic's lock (#861/F2):
        # it's being driven elsewhere, so treat it as terminal-for-us
        # (surface the holder) and keep round-robining the rest.
        _warn(_c._locked_out_message(name, config))
        terminal[name] = 0
        locked_out.add(name)
        return
    signal = _report_epic_tick(name, result)
    code = _terminal_code_for_signal(signal)
    if code is not None:
        terminal[name] = code


def _finalize_fleet_run(
    names: list[str], terminal: dict[str, int], locked_out: set[str]
) -> None:
    """Print the fleet summary and raise the worst aggregate exit code (#757)."""
    _print_fleet_summary(terminal, locked_out)
    repair = sorted(n for n, c in terminal.items() if c == _STALL_EXIT_REPAIR)
    if _worst_exit_code(terminal.values()) == _STALL_EXIT_REPAIR:
        _error(f"kew epic run --all: STALLED epic(s): {repair} — need an operator.")
        raise typer.Exit(_STALL_EXIT_REPAIR)
    if locked_out:
        _success(
            f"kew epic run --all: {len(names) - len(locked_out)} epic(s) COMPLETE; "
            f"{sorted(locked_out)} driven elsewhere."
        )
        return
    _success(f"kew epic run --all: all {len(names)} epic(s) COMPLETE.")


def _drop_archived_epics(names: list[str], retired: frozenset[str]) -> list[str]:
    """The epics ``--all`` should drive, minus the ones an operator retired.

    The roster only ever grows (#2206): an epic has no end state the driver
    reaches and stays at, so a drain nobody intends to resume turns every
    ``--all`` into a parade of STALLED repairs. Dropped with the same one-line
    note a foreign-tracker epic gets, so a missing epic stays diagnosable from
    the output alone — and naming the reversing verb beside it, because an
    archive is the one skip reason an operator can undo.
    """
    driven: list[str] = []
    for name in names:
        if name in retired:
            _log(f"epic {name}: (skipped: archived) — `kew epic unarchive` revives it.")
        else:
            driven.append(name)
    return driven


def _nothing_to_drive(active: str, *, scoped: int) -> str:
    """Why ``--all`` has no epic to drive: an empty tracker scope, or a roster
    that is entirely archived. Two different operator actions, so never one
    message for both."""
    if scoped:
        return (
            f"kew epic run --all: every epic on the active tracker ({active}) is"
            f" archived ({scoped}); nothing to drive. `kew epic unarchive <name>`"
            " puts one back."
        )
    return (
        "kew epic run --all: no epics belong to the active tracker"
        f" ({active}); nothing to drive."
    )


def _run_all_epics(
    *,
    config: DispatchConfig,
    repo: str,
    epic_prefix: str,
    max_parallel_override: int | None,
    log_dir: Path,
    interval: int,
    takeover: bool = False,
) -> None:
    """Round-robin poll loop advancing EVERY known epic in one process until
    all are terminal (#676). Each tick advances each not-yet-terminal epic
    once (on its OWN per-epic lock, #861/F2, so several epics tick
    concurrently), reports the
    per-epic signal, and records COMPLETE/STALLED as terminal. The aggregate
    Awaiting-merge epics remain live on the roster; COMPLETE and repair
    STALLED outcomes are terminal."""
    db = str(log_dir / "dispatch.db")
    pin = _DriverPin(_driver_identity(config), config)
    with _c._open_run_store(db) as store:
        names = store.list_epic_refs()
        trackers = store.epic_trackers()
        retired = archived_epic_refs(store)
    if not names:
        _warn(
            "kew epic run --all: no known epics to drive (source: local"
            f" {db}). A planned epic registers here once `kew epic plan`"
            " creates its issues."
        )
        return
    # Log the discovery source + roster so a missing epic is diagnosable from
    # output alone (#740): --all only sees epics recorded in the local store.
    _log(
        f"kew epic run --all: {len(names)} epic(s) discovered (source: local"
        f" {db}): {', '.join(names)}"
    )

    # Scope to the active project's tracker (#882/B1): an epic whose recorded
    # tracker differs from ours belongs to another project and can't be driven
    # by our sources — driving it would misclassify a complete epic as STALLED
    # and dishonestly fail the whole run. Skip it with a one-line note; it
    # contributes to neither the fleet classification nor the exit code.
    active = config.tracker.type
    scoped = _scope_epics_to_tracker(names, trackers, active)
    names = _drop_archived_epics(scoped, retired)
    if not names:
        _success(_nothing_to_drive(active, scoped=len(scoped)))
        return

    terminal: dict[str, int] = {}  # epic name -> exit code (0/1/2, see #757)
    locked_out: set[str] = set()  # driven elsewhere; distinct summary label (F3)
    while len(terminal) < len(names):
        for name in names:
            if name in terminal:
                continue
            _tick_fleet_epic(
                name,
                db=db,
                repo=repo,
                epic_prefix=epic_prefix,
                pin=pin,
                max_parallel_override=max_parallel_override,
                interval=interval,
                takeover=takeover,
                terminal=terminal,
                locked_out=locked_out,
            )
        if len(terminal) < len(names):
            _c._sleep(interval)

    _finalize_fleet_run(names, terminal, locked_out)


def _admit_under(
    config: DispatchConfig, max_parallel: int, cap: int
) -> tuple[DispatchConfig, int]:
    """Apply ``kew drive``'s admission cap to one tick (#2616).

    The cap becomes the tick's fleet cap, so every admission the tick makes —
    dispatch, review, remediation — counts the runs in flight on every host
    against it, exactly as ``[fleet] max_concurrent`` does (#2398). A cap of 0
    (a hold) is a per-epic budget of 0: nothing new is admitted, and the tick
    still reads, merges and reaps the work already running.
    """
    if cap <= 0:
        return config, 0
    capped = config.model_copy(deep=True)
    capped.fleet.max_concurrent = cap
    return capped, min(max_parallel, cap)


def _epic_run_tick(
    name: str,
    *,
    pin: _DriverPin,
    log_dir: Path,
    repo: str,
    epic_prefix: str,
    max_parallel_override: int | None,
    interval: int,
    takeover: bool,
    admit_cap: int | None = None,
) -> TerminalSignal | None:
    """Advance ``name`` once and report it; ``None`` means another driver
    holds the lock. The body of `kew epic run`'s loop, shared with the pinned
    `_tick` child so both paths run the same tick."""
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve config WITH the store each tick so DB config_overrides
        # govern enforcement — and a mid-run override change is picked up on
        # the next tick, not stranded at process-start file values (#892/O1).
        config, tick_max_parallel, tick_approvers = _reload_driver_controls(
            store,
            pin=pin,
            max_parallel_override=max_parallel_override,
        )
        if admit_cap is not None:
            config, tick_max_parallel = _admit_under(
                config, tick_max_parallel, admit_cap
            )
        deps, facts, tracker = _c._epic_driver_sources(
            config,
            repo=repo,
            epic_prefix=epic_prefix,
            store=store,
            host=pin.identity[2],
        )
        try:
            result = _advance_epic(
                name,
                config=config,
                store=store,
                deps=deps,
                facts=facts,
                repo=repo,
                max_parallel=tick_max_parallel,
                approvers=tick_approvers,
                interval=interval,
                takeover=takeover,
                tracker=tracker,
                operator_stop=max_parallel_override == 0,
            )
        except EmptyEpicGraphError as exc:
            _error(_empty_epic_message(exc.name, config, epic_prefix))
            raise typer.Exit(2) from exc
        except LinearApiError as exc:
            _error(_linear_failure_message(name, exc))
            raise typer.Exit(2) from exc
    if result.locked_out:
        _warn(_c._locked_out_message(name, config))
        return None
    return _report_epic_tick(name, result)


# Exit codes of the hidden `kew epic _tick` child (#2458). STALLED keeps the
# repair code and a driver that could not run keeps 2, so a pinned tick fails
# the way an in-process one does; the two non-terminal outcomes take codes no
# Python crash or Typer usage error produces.
_TICK_EXIT_CONTINUE = 10
_TICK_EXIT_LOCKED_OUT = 11
_TICK_EXIT_CODES: dict[TerminalSignal | None, int] = {
    TerminalSignal.COMPLETE: 0,
    TerminalSignal.PROGRESSING: _TICK_EXIT_CONTINUE,
    TerminalSignal.AWAITING_MERGES: _TICK_EXIT_CONTINUE,
    TerminalSignal.STALLED: _STALL_EXIT_REPAIR,
    None: _TICK_EXIT_LOCKED_OUT,
}


def _kew_build() -> str:
    """The kew version and package directory this process imports from, read
    fresh on every call so a tick can show where its code came from."""
    import importlib.metadata

    import kew

    try:
        version = importlib.metadata.version("kew")
    except importlib.metadata.PackageNotFoundError:
        version = kew.__version__
    return f"kew {version} from {Path(kew.__file__).resolve().parent}"


def _pinned_identity_arg(identity: tuple[str, str, str, Path, Path]) -> str:
    """Encode the driver's startup identity for the `_tick` child."""
    return json.dumps([str(part) for part in identity])


def _parse_pinned_identity(raw: str) -> tuple[str, str, str, Path, Path]:
    tracker, repo, host, db, lock = json.loads(raw)
    return (tracker, repo, host, Path(db), Path(lock))


@epic_app.command("_tick", hidden=True)
def epic_tick(
    name: str,
    identity: str = typer.Option(..., "--identity"),
    interval: int = typer.Option(60, "--interval"),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    takeover: bool = typer.Option(False, "--takeover"),
    epic_prefix: str = "epic:",
    admit_cap: int | None = typer.Option(None, "--admit-cap", min=0),
) -> None:
    """Run ONE `kew epic run` tick for a parent pinned with ``--kew-binary``.

    The parent's startup identity arrives in ``--identity`` so a reload that
    retargets the tracker, repo, DB or lock still stops the driver. The child
    reports its kew build at start and end; the exit code carries the signal
    (`_TICK_EXIT_CODES`)."""
    pinned = _parse_pinned_identity(identity)
    _log(f"epic {name}: tick start on {_kew_build()}")
    try:
        # A fresh process: register gh/git before the tick shells out, as
        # `_epic_driver_config` does for the parent.
        _c.preflight_tools()
        try:
            baseline = _c.load_config()
        except typer.Exit:
            raise
        except Exception as exc:
            # The in-process loop keeps its last-good config here; a fresh
            # child has none, so it skips this tick and the parent retries.
            _warn(
                f"kew epic run: config reload failed ({exc}) — skipping this "
                "tick; the fixed kew.toml is picked up on the next one."
            )
            raise typer.Exit(_TICK_EXIT_CONTINUE) from None
        baseline.project.repo = baseline.project.repo or pinned[1]
        signal = _epic_run_tick(
            name,
            pin=_DriverPin(pinned, baseline),
            log_dir=pinned[3].parent,
            repo=pinned[1],
            epic_prefix=epic_prefix,
            max_parallel_override=max_parallel,
            interval=interval,
            takeover=takeover,
            admit_cap=admit_cap,
        )
    finally:
        _log(f"epic {name}: tick end on {_kew_build()}")
    raise typer.Exit(_TICK_EXIT_CODES[signal])


def _pinned_tick_args(
    name: str,
    *,
    pin: _DriverPin,
    interval: int,
    max_parallel: int | None,
    takeover: bool,
    epic_prefix: str,
    admit_cap: int | None = None,
) -> list[str]:
    args = ["epic", "_tick", name, "--identity", _pinned_identity_arg(pin.identity)]
    args += ["--interval", str(interval), "--epic-prefix", epic_prefix]
    if max_parallel is not None:
        args += ["--max-parallel", str(max_parallel)]
    if admit_cap is not None:
        args += ["--admit-cap", str(admit_cap)]
    if takeover:
        args.append("--takeover")
    return args


def _run_pinned_tick(kew_binary: Path, args: list[str]) -> int:
    """Resolve ``kew_binary`` once — through any ``current`` symlink — and run
    one tick on that kew. A later repoint of the symlink reaches the next tick,
    never this one."""
    resolved = Path(os.path.realpath(kew_binary))
    try:
        pinned = pin_current_executable(resolved)
    except ValueError as exc:
        _error(f"kew epic run: --kew-binary {kew_binary} ({resolved}): {exc}.")
        raise typer.Exit(2) from exc
    _log(f"kew epic run: tick on pinned {resolved}")
    try:
        return run_pinned_driver_tick(pinned, args)
    except ValueError as exc:
        # Revalidation at launch: the bytes changed after they were pinned.
        _error(f"kew epic run: --kew-binary {resolved}: {exc}.")
        raise typer.Exit(2) from exc


def _run_pinned_epic(
    name: str, kew_binary: Path, args: list[str], interval: int
) -> None:
    """`kew epic run` with ``--kew-binary``: every tick is a fresh child on the
    kew that ``kew_binary`` resolves to when the tick starts (#2458)."""
    while True:
        code = _run_pinned_tick(kew_binary, args)
        if code in (0, _TICK_EXIT_LOCKED_OUT):
            return
        if code == _STALL_EXIT_REPAIR:
            raise typer.Exit(_STALL_EXIT_REPAIR)
        if code != _TICK_EXIT_CONTINUE:
            _error(f"kew epic run: pinned tick of epic {name} exited {code}.")
            raise typer.Exit(2)
        _c._sleep(interval)


@epic_app.command("run")
def epic_run(
    name: str | None = typer.Argument(
        None, help="Epic name to drive (omit with --all)."
    ),
    interval: int = typer.Option(
        60, "--interval", help="Seconds to wait between ticks while PROGRESSING."
    ),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    all_epics: bool = typer.Option(
        False, "--all", help="Drive every known epic round-robin in one poll loop."
    ),
    takeover: bool = typer.Option(
        False,
        "--takeover",
        help="Also reclaim a legacy lock with no heartbeat (a provably-stale "
        "dead-PID or stale-heartbeat lock is reclaimed automatically).",
    ),
    kew_binary: Annotated[
        Path | None,
        typer.Option(
            "--kew-binary",
            help="Run each tick in a fresh process on this kew executable, "
            "resolved (through any symlink) once at tick start, so an upgrade "
            "mid-tick never changes the running tick's code.",
        ),
    ] = None,
    epic_prefix: str = "epic:",
) -> None:
    """Poll the epic driver (advance every `--interval` seconds) until
    COMPLETE or STALLED. Never loops on STALLED — exits non-zero so an
    operator can intervene. With ``--all``, advances every known epic
    round-robin in the one loop until ALL are terminal."""
    config, repo, _, _ = _c._epic_driver_config(max_parallel)
    pin = _DriverPin(_driver_identity(config), config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    if all_epics:
        if kew_binary is not None:
            _error("kew epic run: --kew-binary drives one named epic, not --all.")
            raise typer.Exit(2)
        _run_all_epics(
            config=config,
            repo=repo,
            epic_prefix=epic_prefix,
            max_parallel_override=max_parallel,
            log_dir=log_dir,
            interval=interval,
            takeover=takeover,
        )
        return

    if name is None:
        _error("kew epic run requires an epic NAME (or pass --all to drive them all).")
        raise typer.Exit(2)

    if kew_binary is not None:
        args = _pinned_tick_args(
            name,
            pin=pin,
            interval=interval,
            max_parallel=max_parallel,
            takeover=takeover,
            epic_prefix=epic_prefix,
        )
        _run_pinned_epic(name, kew_binary, args, interval)
        return

    while True:
        signal = _epic_run_tick(
            name,
            pin=pin,
            log_dir=log_dir,
            repo=repo,
            epic_prefix=epic_prefix,
            max_parallel_override=max_parallel,
            interval=interval,
            takeover=takeover,
        )
        if signal is None or signal is TerminalSignal.COMPLETE:
            return
        if signal is TerminalSignal.STALLED:
            raise typer.Exit(_STALL_EXIT_REPAIR)
        _c._sleep(interval)


def _ledger_error(verb: str, db: Path, exc: sqlite3.Error) -> NoReturn:
    """Report ledger failures consistently at the operator command boundary."""
    _error(
        f"kew epic {verb}: could not use ledger {db}: {exc}. "
        "Check disk space, permissions, and ledger integrity before retrying."
    )
    raise typer.Exit(2) from None


@epic_app.command("continue")
def epic_continue(name: str, node: str) -> None:
    """Continue one positively-orphaned dirty epic worktree in place."""
    config = _c.load_config()
    provider = _c.provider_from_config(config)
    item_id = _validate_continue_item(node, provider)

    _c.preflight_tools()
    repo = _c._epic_repo(config)
    notifier = _c.build_notifier(config)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    db = log_dir / "dispatch.db"
    try:
        with _c._open_run_store(str(db)) as store:
            config = _c.load_config(store=store)
            if not config.project.repo:
                config.project.repo = repo
            record = _prepare_resume_record(
                store, epic_ref=name, item_id=item_id, config=config
            )
            claimed = _claim_resume_record(
                store,
                epic_ref=name,
                item_id=item_id,
                config=config,
                expect=record,
            )
            outcome = _run_resume_continuation(
                claimed,
                store=store,
                config=config,
                notifier=notifier,
                epic_ref=name,
                provider=provider,
            )
            _finish_resume_continuation(item_id, outcome)
    except sqlite3.Error as exc:
        _ledger_error("continue", db, exc)
    finally:
        from kew.worktree import release_checkout_claims

        release_checkout_claims()
    _success(f"{item_ref(item_id)}: preserved work continued in place.")


@epic_app.command("_continue-node", hidden=True)
def epic_continue_node(
    epic_ref: str,
    item: str,
    continuation_run_id: str = typer.Option(..., "--continuation-run-id"),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True),
    credential_route: str | None = typer.Option(
        None, "--credential-route", hidden=True
    ),
) -> None:
    """Detached child that runs one continuation the epic driver claimed.

    The driver claimed the continuation and its worker slot before spawning
    this process. The child proves the preserved checkout and the dead
    owner exactly as ``kew epic continue`` does; a refusal — or any failure
    before the agent's run row exists — cancels the claim before any agent
    launches, which returns the work to its continuation wait and spends one
    of the driver's two attempts.
    """
    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with (
        _c._open_run_store(str(log_dir / "dispatch.db")) as store,
        _tombstone_unlaunched_key(store, run_id),
        _undo_unlaunched_claim(store, epic_ref, continuation_run_id),
    ):
        if run_id is not None and run_id != continuation_run_id:
            _error("kew epic _continue-node: --run-id must be the continuation run id")
            raise typer.Exit(2)
        activate_detached_publication_token()
        _c.preflight_tools()
        config = _c.load_config(store=store)
        provider = _c.provider_from_config(config)
        item_id = _validate_continue_item(item, provider)
        claimed = _claimed_continuation(
            store, epic_ref=epic_ref, item_id=item_id, run_id=continuation_run_id
        )
        try:
            _prove_claimed_continuation(store, claimed, config)
            with detached_credential_handoff(credential_route, run_id):
                outcome = _run_resume_continuation(
                    claimed,
                    store=store,
                    config=config,
                    notifier=_c.build_notifier(config),
                    epic_ref=epic_ref,
                    provider=provider,
                )
            _finish_resume_continuation(item_id, outcome)
        finally:
            from kew.worktree import release_checkout_claims

            release_checkout_claims()


@contextlib.contextmanager
def _tombstone_unlaunched_key(
    store: SqliteRunStore, run_id: str | None
) -> Iterator[None]:
    """Give up the driver's bounded key when this child exits before launch.

    The driver marked the key child-owned when it spawned this process, so a
    refusal that cancels the claim before any run row exists would otherwise
    leave reconciliation treating the key as live until it expires (#2964
    review). The tombstone is written only while no run row exists: it is
    credential evidence, never launch evidence, so the claim rollback stands.
    """
    try:
        yield
    except BaseException:
        from kew.credential_prelaunch import tombstone_absent_run
        from kew.run_credentials import ISSUED_KEY_HASH_ENV

        key_hash = os.environ.get(ISSUED_KEY_HASH_ENV)
        if run_id is not None and key_hash:
            tombstone_absent_run(store, run_id, key_hash, _c.datetime.now(UTC))
        raise


@contextlib.contextmanager
def _undo_unlaunched_claim(
    store: SqliteRunStore, epic_ref: str, continuation_run_id: str
) -> Iterator[None]:
    """Cancel the driver's claim when this child fails before its run exists.

    A config read, preflight or notifier failure would otherwise leave the
    claim to missing-run reconciliation, long after the startup grace. Only
    the exact claim this child was spawned for is touched, and only while no
    run row names it: once the agent's run exists, the run owns the outcome.
    """
    try:
        yield
    except BaseException:
        if store.get(continuation_run_id) is None:
            for record in store.list_continuations(epic_ref):
                if (
                    record.state == "continuing"
                    and record.continuation_run_id == continuation_run_id
                ):
                    store.mark_continuation_awaiting(
                        record.epic_ref,
                        record.item_id,
                        record.original_run_id,
                        record.branch,
                        record.worktree_identity,
                        record.generation,
                        continuation_run_id,
                        reason="auto_continue_child_failed",
                        cancel_unlaunched=True,
                    )
        raise


def _claimed_continuation(
    store: SqliteRunStore, *, epic_ref: str, item_id: str, run_id: str
) -> ContinuationRecord:
    """The one continuing record this child's claim owns, else exit 2."""
    for record in store.list_continuations(epic_ref):
        if (
            record.item_id == item_id
            and record.state == "continuing"
            and record.continuation_run_id == run_id
        ):
            return record
    _error(
        f"kew epic _continue-node: {item_ref(item_id)} has no continuation "
        f"claimed by {run_id}"
    )
    raise typer.Exit(2)


def _prove_claimed_continuation(
    store: SqliteRunStore, claimed: ContinuationRecord, config: DispatchConfig
) -> None:
    """Prove the checkout and the dead owner, or cancel the unlaunched claim."""
    from kew.worktree import CheckoutAdmissionRefusal

    try:
        _validate_resume_worktree(claimed, config, store)
        refusal = None
        if not _resume_owner_is_dead(
            store, claimed, config, own_claim=claimed.continuation_run_id
        ):
            refusal = "the original owner is live or unproven"
    except (OSError, ValueError, CheckoutAdmissionRefusal) as exc:
        refusal = str(exc)
    if refusal is None:
        return
    assert claimed.continuation_run_id is not None
    store.mark_continuation_awaiting(
        claimed.epic_ref,
        claimed.item_id,
        claimed.original_run_id,
        claimed.branch,
        claimed.worktree_identity,
        claimed.generation,
        claimed.continuation_run_id,
        reason=f"auto_continue_refused: {refusal}",
        cancel_unlaunched=True,
    )
    _error(f"kew epic _continue-node: {refusal}")
    raise typer.Exit(2)


@epic_app.command("abandon")
def epic_abandon(
    name: str,
    node: str,
    reason: str = typer.Option(
        ..., "--reason", help="Why this continuation is abandoned."
    ),
) -> None:
    """Abandon preserved work and return the node for fresh dispatch."""
    config = _c.load_config()
    try:
        item_id = _c.validate_item_id(node, _c.provider_from_config(config))
        if not reason.strip():
            raise ValueError("reason must be non-empty")
    except ValueError as exc:
        _error(f"kew epic abandon: {exc}")
        raise typer.Exit(2) from None
    db = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db.is_file():
        _error(f"kew epic abandon: no ledger at {db}")
        raise typer.Exit(2)
    try:
        with _c._open_run_store(str(db)) as store:
            config = _c.load_config(store=store)
            from kew.worktree import checkout_claim_guard

            _c.service_verbs.epic_abandon(
                store,
                epic_ref=name,
                item_id=item_id,
                reason=reason,
                config=config,
                checkout_guard=checkout_claim_guard,
            )
    except sqlite3.Error as exc:
        _ledger_error("abandon", db, exc)
    except _c.service_verbs.EpicAbandonDenied as exc:
        _error(f"kew epic abandon: {exc}")
        raise typer.Exit(1) from None
    except (OSError, RuntimeError, LookupError, _c.service_verbs.VerbDenied) as exc:
        _error(f"kew epic abandon: {exc}")
        raise typer.Exit(2) from None
    _success(
        f"{item_ref(item_id)}: continuation abandoned; fresh dispatch on the next tick."
    )


# The verb's own name, for the diagnostics both halves share.
_ARCHIVE_VERB = {True: "archive", False: "unarchive"}


def _set_archived(name: str, *, archived: bool, reason: str) -> None:
    """The shared body of `kew epic archive` and `kew epic unarchive` (#2206).

    Both verbs write exactly one attested event and touch nothing else, so the
    only thing that differs between them is the boolean and the word in the
    confirmation line.
    """
    config = _c.load_config()
    db = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db.is_file():
        _error(f"kew epic {_ARCHIVE_VERB[archived]}: no ledger at {db}")
        raise typer.Exit(2)
    try:
        with _c._open_run_store(str(db)) as store:
            config = _c.load_config(store=store)
            state = set_epic_archived(
                store,
                epic_ref=name,
                archived=archived,
                reason=reason,
                config=config,
            )
    except sqlite3.Error as exc:
        _ledger_error(_ARCHIVE_VERB[archived], db, exc)
    except EpicArchiveDenied as exc:
        _error(f"kew epic {_ARCHIVE_VERB[archived]}: {exc}")
        raise typer.Exit(1) from None
    except LookupError as exc:
        _error(f"kew epic {_ARCHIVE_VERB[archived]}: {exc}")
        raise typer.Exit(2) from None
    note = f" — {state.reason}" if state.reason else ""
    if archived:
        _success(
            f"{name}: archived{note}. It leaves the needs-you count, "
            "`kew epic run --all` and the Epics list's active view; nothing "
            "is deleted, and `kew epic unarchive` puts it back."
        )
    else:
        _success(
            f"{name}: unarchived{note}. It is back in the needs-you count, "
            "`kew epic run --all` and the Epics list's active view."
        )


@epic_app.command("archive")
def epic_archive(
    name: str,
    reason: str = typer.Option(
        "", "--reason", help="Why this epic is retired (recorded on the chain)."
    ),
) -> None:
    """Retire a finished or abandoned epic from the active views.

    Records ONE attested `epic_archived` audit event and nothing else — no
    column, no deletion. An archived epic is excluded from the console's
    needs-you count, from `kew epic run --all`, and from the Epics list's
    active view; its ticks, runs and drain stay in the ledger and on the
    Finished/Archived shelf. Reverse it with `kew epic unarchive`.

    Exits 1 when the epic is already archived (a second archive is not a
    second decision), and 2 for an epic this ledger has never driven.
    """
    _set_archived(name, archived=True, reason=reason)


@epic_app.command("unarchive")
def epic_unarchive(
    name: str,
    reason: str = typer.Option(
        "", "--reason", help="Why this epic is active again (recorded on the chain)."
    ),
) -> None:
    """Return an archived epic to the active views.

    Records an attested `epic_unarchived` event that OUTRANKS the archive
    rather than retracting it, so `kew audit tail` reads the whole history of a
    retirement. Exits 1 when the epic is not archived.
    """
    _set_archived(name, archived=False, reason=reason)


@epic_app.command("retry")
def epic_retry(name: str, node: str) -> None:
    """Re-admit a FAILED epic node WITHOUT deleting its run history: clear
    its `agent-failed` label, release any stale reservation, and record a
    retry marker so the driver's overlay treats the stale failure run(s) as
    superseded instead of discarding them — cost/token history stays intact
    for `kew report`.

    Exits 2 without recording anything when the node is HELD: a retry restores
    admission state, which is not what a hold gates, so the hold has to go
    first via `kew epic unhold`. Retry authorizes agent spend, so like the
    console it is refused (exit 2, nothing recorded) for an actor outside
    `\\[approval].approvers`."""
    config = _c.load_config()
    provider = _c.provider_from_config(config)
    try:
        item_id = _c.validate_item_id(node, provider)
    except InvalidItemIdError:
        _error(f"kew epic retry: node must be a valid {provider} item id, got {node!r}")
        raise typer.Exit(2) from None

    _c.preflight_tools()
    _c._epic_repo(config)  # resolve/auto-detect + validate config.project.repo

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        try:
            _c.service_verbs.epic_retry_gated(
                store, epic_ref=name, item_id=item_id, config=config
            )
        except (ValueError, _c.service_verbs.VerbDenied) as exc:
            # A non-approver, a held node, or one whose dirty work is
            # preserved: every guard runs before the emit and every effector,
            # so the refusal recorded nothing (#2154, #2552).
            _error(f"kew epic retry: {exc}")
            raise typer.Exit(2) from None
        if provider == "github":
            _log(
                f"#{item_id}: restored '{config.labels.ready}' and cleared "
                "failure labels"
            )
        drive = _driving_drive(store, name)
    _success(
        f"{item_ref(item_id)}: recorded a retry marker and restored admission "
        "state; only an observed terminal reservation was released (failed run "
        "history preserved)."
    )
    if drive is not None:
        # The drive holds no per-epic lock between its turns, so the probe
        # below would read it as no driver at all (#2852).
        _log(
            f"kew drive (pid {drive.pid}) has {name} in its rotation; it picks "
            f"{item_ref(item_id)} up on its next tick."
        )
        return
    lock_path = _c._epic_lock_path(config, name)
    with single_driver_lock(lock_path, holder=f"retry-probe:{name}") as acquired:
        if acquired:
            # The retry was applied; the missing driver is operator repair (1),
            # not a command that could not run (2) (#1943).
            _error(f"no live driver — relaunch required: kew epic run {name}")
            raise typer.Exit(1)


def _driving_drive(store: RunStore, name: str) -> DriveHolder | None:
    """The running ``kew drive`` whose rotation holds ``name``, if one does.

    Read the way ``kew drive status`` reads it: a holder whose heartbeat has
    stalled, or which is provably dead, drives nothing.
    """
    reading = read_drive(store)
    beat = reading.heartbeat
    if reading.state != DRIVE_RUNNING or beat is None or not beat.drives(name):
        return None
    return reading.holder


def _release_note(reason: str) -> str:
    """What the release did beyond deleting the row, for the operator's line.

    Two reasons carry something extra out of the same transaction: the round
    cap grants a fresh review allowance (#2154), and the merge queue's gate
    hold leaves the receipt that keeps the queue from re-deriving it (#2184).
    """
    if restarts_review_cycle(reason):
        return "review cycle restarted (fresh round allowance) and node released"
    if reason.startswith(MERGE_GATE_TOUCHED_HOLD_PREFIX):
        return "reviewed head recorded (a later push is held again) and node released"
    return "node released"


def _record_unhold(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    item_id: str,
    hold: EpicHold,
    pr_head: str | None = None,
) -> None:
    """Put an operator's hold release on the tamper-evident chain (#2154).

    A round-cap release now writes the ``epic_retries`` marker itself, and that
    marker is the ``review_cycle_start`` bound the cycle-scoped verdict
    guarantee in :mod:`kew.service.evidence_chain` filters attested verdicts by.
    Unattested, the release that granted the extra reviewer rounds and moved
    that bound would be the one half of the pair with nothing on the chain:
    ``kew epic retry``, the marker's only other writer, emits
    ``epic_node_retry``.

    Emitted AFTER the store call rather than before it, unlike the operator
    verbs in :mod:`kew.service.verbs`: which reason was released — and so
    whether the cycle restarted — is only knowable from the atomic
    remove-and-return, and a lookup-then-clear pair is exactly what that call
    exists to avoid. The remaining effector (the label strip) still runs after.

    The reason is untrusted text on an ``admission_refused`` hold (a detached
    child's stderr tail), so it rides through ``safe_hold_reason``.
    """
    from kew.audit import emit
    from kew.ids import GITHUB_ID_RE

    emit(
        store,
        "epic_node_unhold",
        actor=resolve_actor(),
        # 0-sentinel issue_num for a non-numeric tracker, exactly as
        # ``epic_node_retry`` records one; the durable id rides in ``item_id``
        # and in the payload's ``node`` field regardless of currency.
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=config.project.repo or None,
        payload={
            "epic_ref": epic_ref,
            "node": item_id,
            "reason": safe_hold_reason(hold.reason),
            "review_cycle_restarted": restarts_review_cycle(hold.reason),
            # The head whose gate-definition diff this release authorizes the
            # merge queue to verify, or None for every other reason. The
            # receipt is what lets a PR that edits the gates through, so which
            # head it covers belongs on the chain beside the release.
            "merge_gate_head_reviewed": merge_gate_reviewed_head(hold.reason),
            # The PR head a `remediation_no_change` release vouches for, read
            # before the release (#3276): the drive takes it as the answer to
            # an operator send-back on that head only. None for other reasons,
            # and when the PR could not be read.
            "pr_head": pr_head
            if hold.reason.startswith(REMEDIATION_NO_CHANGE_HOLD_PREFIX)
            else None,
            "source": "cli",
        },
    )


def _no_change_release_head(
    store: SqliteRunStore,
    config: DispatchConfig,
    *,
    name: str,
    item_id: str,
    provider: str,
    repo: str,
    epic_prefix: str,
) -> str | None:
    """The PR head a release of a ``remediation_no_change`` hold vouches for.

    Read from the forge BEFORE the hold is removed, so it is a head the PR
    stood at while the node was still held: a base integration that moves the
    PR after the release is never covered by it (#3276). ``None`` for any other
    hold, and when the PR cannot be read — a release with no head answers no
    send-back, so the drive remediates once more instead.
    """
    held = next((h for h in store.list_epic_holds(name) if h.item_id == item_id), None)
    if held is None or not held.reason.startswith(REMEDIATION_NO_CHANGE_HOLD_PREFIX):
        return None
    try:
        _deps, facts, _tracker = _epic_driver_sources(
            config, repo=repo, epic_prefix=epic_prefix, store=store
        )
        return _node_pull_request(facts, item_id, provider).head_sha
    except Exception as exc:  # noqa: BLE001 — the release itself must not fail
        _warn(f"reading the pull request head failed: {exc}")
        return None


@epic_app.command("unhold")
def epic_unhold(name: str, item: str, epic_prefix: str = "epic:") -> None:
    """Release a driver hold on an epic node — the operator counterpart to the
    driver's own hold bookkeeping. Clears the `epic_holds` row for
    <name>/<item> via the store API and, on a GitHub tracker, strips the stale
    `pr-pending-review` label (mirror of the manual unwedge playbook); a
    non-github tracker owns node state in the tracker itself, so no label is
    touched. Names the cleared reason + held-since time; exits 2 when no hold
    exists for that epic/item.

    Releasing a `review_rounds_exhausted` hold restarts the node's review cycle
    in the same transaction: the cap is read from the round count, so a
    bare release would be re-HELD on the unchanged count by the driver's next
    review pass — and `kew epic retry`, the marker's only other writer, refuses
    a held node.

    Releasing a `merge_gate_touched` hold records a receipt for the head the
    hold names, in that same transaction and for the same reason: the
    merge queue re-derives that hold from the PR's diff on every pass, so a bare
    release would be re-HELD against the unchanged diff forever. The receipt
    covers the reviewed head only — a later push is held again.

    Releasing a `remediation_no_change` hold reads the PR's head first and
    records it on the release: on an operator send-back the release
    answers it for that head only, so a base integration that moves the PR
    afterwards leaves the send-back standing.

    Every release is attested as an `epic_node_unhold` audit event carrying the
    released reason and whether the review cycle restarted with it, so the
    extra reviewer rounds a round-cap release grants are on the chain exactly
    as a retry's `epic_node_retry` is."""
    config = _c.load_config()
    provider = _c.provider_from_config(config)
    try:
        item_id = _c.validate_item_id(item, provider)
    except InvalidItemIdError:
        _error(
            f"kew epic unhold: item must be a valid {provider} item id, got {item!r}"
        )
        raise typer.Exit(2) from None

    _c.preflight_tools()
    repo = _c._epic_repo(config)  # resolve/auto-detect + validate project.repo

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        pr_head = _no_change_release_head(
            store,
            config,
            name=name,
            item_id=item_id,
            provider=provider,
            repo=repo,
            epic_prefix=epic_prefix,
        )
        # Atomic remove-and-return: a lookup-then-clear pair would let a
        # concurrent driver re-hold slip a fresh row in between, so we'd
        # delete the new hold while reporting the old one's details.
        hold = store.remove_epic_hold(
            name,
            item_id,
            fresh_review_cycle_reasons=REVIEW_CYCLE_RESET_HOLD_REASONS,
            # The merge queue's gate hold is DERIVED from the PR's diff, so the
            # delete alone releases nothing — the next merge pass re-derives it
            # from the unchanged diff. The receipt, written in the same
            # transaction, is what the queue reads to let the head a human just
            # reviewed through (#2184).
            receipt_key=merge_gate_ack_key(name, item_id),
            receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
        )
        # A pseudo-epic writes no hold row, so the refusal it stands on is
        # named instead; releasing it touches no tracker label (#2971).
        refusal = (
            None
            if hold is not None
            else pseudo_epic_clone_refusal(store, name, item_id)
        )
        hold = hold or refusal
        if hold is None:
            _error(
                f"kew epic unhold: no hold for {item_ref(item_id)} in epic {name!r}."
            )
            raise typer.Exit(2)
        _record_unhold(
            store,
            config,
            epic_ref=name,
            item_id=item_id,
            hold=hold,
            pr_head=pr_head,
        )
        if pr_head is None and hold.reason.startswith(
            REMEDIATION_NO_CHANGE_HOLD_PREFIX
        ):
            _warn(
                f"{item_ref(item_id)}: could not read the pull request head, so "
                "this release vouches for no head: an operator send-back on it "
                "still stands, and the drive remediates it again instead of "
                "reviewing."
            )
        if provider == "github" and refusal is None:
            _c.clear_pending_review_label(int(item_id), config)
            _log(f"#{item_id}: stripped '{config.labels.pr_pending_review}' label")
    _success(
        f"{item_ref(item_id)}: cleared '{hold.reason}' hold (held since "
        f"{hold.held_at:%Y-%m-%d %H:%M UTC}) — {_release_note(hold.reason)} for "
        "the next `kew epic advance`."
    )


def _node_pull_request(facts: Any, item_id: str, provider: str) -> PullRequestHead:
    """The node's linked pull request as the forge reports it now (#3070).

    The link comes off the item, as ``kew epic merge`` reads it; the state and
    head off ``fresh_pr_facts``, which never answers from a tick's batch. An
    unreadable item or PR comes back with no number, state or head, which the
    send-back refuses.
    """
    observed = facts.issue_facts(_facts_key(item_id, provider))
    if observed.pr_number is None:
        return PullRequestHead(number=None, state=None, head_sha=None)
    pr = facts.fresh_pr_facts(observed.pr_number)
    return PullRequestHead(
        number=observed.pr_number,
        state=pr.get("state") or None,
        head_sha=pr.get("headRefOid") or None,
    )


@epic_app.command("send-back")
def epic_send_back(
    name: str,
    node: str,
    reason_file: Annotated[
        Path,
        typer.Option(
            "--reason-file",
            help="File holding the blockers the remediation must fix.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    epic_prefix: str = "epic:",
) -> None:
    """Send a READY epic node back for remediation — the operator's NEEDS_CHANGES.
    Records a NEEDS_CHANGES round against the head the node's READY
    verdict stands on, which must still be the pull request's head on GitHub,
    attested with you as its actor and the reason file's text as its one
    finding. The drive's next tick reads the node as remediating and runs the
    remediation under the node's own item id, so the drive keeps owning the
    node — unlike `kew review <PR>` or a commit of your own on the agent branch.

    Exits 2 with nothing recorded when the reason is empty, the node is held,
    its latest round is not READY, it has no linked pull request, its pull
    request is merged, closed or unreadable, its head moved since the READY
    review, a run holds its reservation, or you are not an approver
    (`approval.approvers`): a send-back authorizes a remediation run."""
    config = _c.load_config()
    provider = _c.provider_from_config(config)
    try:
        item_id = _c.validate_item_id(node, provider)
    except InvalidItemIdError:
        _error(
            f"kew epic send-back: node must be a valid {provider} item id, got {node!r}"
        )
        raise typer.Exit(2) from None
    reason = reason_file.read_text(encoding="utf-8", errors="replace")

    _c.preflight_tools()
    repo = _c._epic_repo(config)  # resolve/auto-detect + validate project.repo

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        _deps, facts, _tracker = _epic_driver_sources(
            config, repo=repo, epic_prefix=epic_prefix, store=store
        )
        try:
            sent = _c.service_verbs.epic_send_back_gated(
                store,
                epic_ref=name,
                item_id=item_id,
                reason=reason,
                config=config,
                read_pr=partial(_node_pull_request, facts, item_id, provider),
            )
        except (ValueError, _c.service_verbs.VerbDenied) as exc:
            _error(f"kew epic send-back: {exc}")
            raise typer.Exit(2) from None
        drive = _driving_drive(store, name)
    if sent.merged_meanwhile:
        # The round changes nothing on a merged PR, so no line says it will.
        _warn(
            f"{item_ref(sent.item_id)}: its pull request merged before the "
            "send-back could take effect — the round is recorded, but the merged "
            "code is on the base branch; fix it in a follow-up."
        )
        return
    _success(
        f"{item_ref(sent.item_id)}: recorded operator NEEDS_CHANGES as round "
        f"{sent.round} against {sent.head_sha[:12]} — the drive remediates it on "
        "its next tick."
    )
    _report_driver(config, name, drive, item_id=sent.item_id)


def _report_driver(
    config: DispatchConfig, name: str, drive: DriveHolder | None, *, item_id: str
) -> None:
    """Say whether a driver will act on the send-back, the way retry reads it.

    The round is recorded either way, so a missing driver is reported, not an
    error: the send-back exits 0 and names the command that picks it up.
    """
    if drive is not None:
        _log(
            f"kew drive (pid {drive.pid}) has {name} in its rotation; it picks "
            f"{item_ref(item_id)} up on its next tick."
        )
        return
    lock_path = _c._epic_lock_path(config, name)
    with single_driver_lock(lock_path, holder=f"send-back-probe:{name}") as acquired:
        if acquired:
            _warn(
                f"no live driver — nothing remediates {item_ref(item_id)} until "
                f"one runs: kew epic run {name}"
            )


@epic_app.command("merge")
def epic_merge(
    name: str,
    item: str,
    head: Annotated[
        str,
        typer.Option(
            "--head",
            help="Full 40-character sha of the head to merge. Every gate is "
            "proven against exactly this sha.",
        ),
    ],
    epic_prefix: str = "epic:",
) -> None:
    """Merge ONE READY node of an epic, on the operator's own authority.

    The driver's merge pass only merges on a verified PR approval from a
    non-author human with write permission. Where one person authors every
    agent PR the forge cannot produce that approval, so the pass never fires
    and READY nodes wait forever. This command is the operator performing that
    merge by hand — and it replaces the forge approval and NOTHING else.

    It merges only when ALL of these hold for exactly `--head`:
    `epic.merge_queue = true`; ITEM is a node of NAME by the epic's journaled
    membership; the node carries no hold; the node is still live work — its
    tracker item is READABLE and not closed and no zero-diff judgment is still
    deciding what the absence of a diff meant, both of which the driver's own
    merge pass would never merge either (an item kew could not read refuses as
    well: it cannot be proven live); the latest review round is READY (or is
    a NEEDS_CHANGES round whose residuals the stop rule accepted) and was
    recorded against `--head`; the PR is open and its head on the forge is
    `--head`, read fresh; CI is green on BOTH gates the merge pass applies —
    GitHub's computed `mergeStateStatus` AND the head's own check runs, because
    the first counts a skipped required check as passing; and the merge
    queue verifies the combined tree — the contained verify command, the
    gate-definition check, the base-OID re-read — live, under the repo-wide queue
    lock. Every fact another actor can have moved under that verification is
    re-read after it — the PR's open state, the head, its base branch, both CI
    gates, the item's own live-work state, the hold and the review verdict — the
    attested round is the re-proven one, the merge argv binds the head with
    `--match-head-commit`, and the merge runs inside the queue lease.

    No label and no other agent-writable signal contributes to any of it. The
    merge is recorded as an attested `epic_operator_merge` event naming the
    actor, the head and the base. Every refusal exits 2 and merges nothing;
    `kew epic advance` reconciles the merged PR and closes the item as it does
    for any other merge.
    """
    config, repo, _max_parallel, _approvers = _c._epic_driver_config(None)
    provider = _c.provider_from_config(config)
    try:
        item_id = _c.validate_item_id(item, provider)
    except InvalidItemIdError:
        _error(f"kew epic merge: item must be a valid {provider} item id, got {item!r}")
        raise typer.Exit(2) from None

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        # Re-resolve WITH the store so DB config_overrides govern this merge
        # exactly as they govern the driver's own (#892/O1).
        config = _c.load_config(store=store)
        _deps, facts, _tracker = _epic_driver_sources(
            config, repo=repo, epic_prefix=epic_prefix, store=store
        )
        try:
            outcome = operator_merge(
                config,
                store,
                epic_ref=name,
                item_id=item_id,
                head=head,
                repo=repo,
                # The same base `epic_advance` drives the queue against: a
                # different one would verify a tree the driver never merges into.
                base="main",
                repo_root=_git_repo_root(project_root()),
                reader=facts,
                actor=resolve_actor(),
                provider=provider,
            )
        except OperatorMergeRefused as exc:
            _error(f"kew epic merge: {exc}")
            raise typer.Exit(2) from None
    _success(
        f"{item_ref(item_id)}: merged PR #{outcome.pr_number} ({outcome.branch}) "
        f"at {outcome.head[:12]} into {outcome.base} as {outcome.actor} — "
        "recorded as an operator merge; `kew epic advance` closes the item."
    )


#: How many of the detached child's final operator-facing stderr lines are kept
#: as the recorded refusal reason, and the cap on the recorded string.
_REFUSAL_TAIL_LINES = 3
_REFUSAL_REASON_MAX_CHARS = 240

#: Explicit ``dispatch_completed.error_category`` values a run-less child
#: converts into a HOLD (#1658). These refusals need an operator, so another
#: dispatch without a change only burns another tick.
#: Transient refusals (worker cap, fleet stop, ``github_unreachable``) are
#: deliberately absent: they clear on their own and holding them would wedge a
#: node the next tick runs. ``github_auth`` (a gh that was never logged in, or
#: an invalid or expired gh token) is held: it needs an operator, and
#: re-spawning every tick only burns slots (#1724, #1731).
#: Budget and containment refusals are absent too — each writes its OWN, richer,
#: self-describing hold before returning, and that hold is never overwritten.
#: Pre-spawn refusals that need an operator and must hold the node. The runner
#: preflight at the dispatch boundary (the governor's final route) is one:
#: without it here, a governor-swapped runner that cannot launch left no hold
#: and no refusal row (round-5 review of #1797).
_HOLDABLE_REFUSAL_CATEGORIES = frozenset(
    {
        "credential_issuer_refused",
        "transport_admission_refused",
        "runner_preflight_refused",
        "publication_configuration_refused",
        "prompt_configuration_refused",
        "github_auth",
    }
)

#: ``_launch_failed`` uses this category for a pre-spawn exception. The same
#: category also covers failures after the run row exists. It is holdable only
#: when the ledger proves this child created no run row.
_RUNLESS_EXCEPTION_CATEGORY = "exception"

#: ``RunRecord.error_category`` for the refusal row an admission-held node gets.
#: Distinct from ``dispatch_child_refused`` (the child never reached dispatch)
#: and from ``dispatch_child_died`` (no refusal at all) so `kew report` can tell
#: a governed refusal from a corpse.
_ADMISSION_REFUSED_CATEGORY = "admission_refused"


class _RunnerPreflightRefusal(RuntimeError):
    """A resolved child route cannot launch its runner."""


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class _StderrTail(io.TextIOBase):
    """Pass-through ``sys.stderr`` wrapper that remembers the last lines written.

    The driver spawns ``kew epic _dispatch-node`` detached with stdin, stdout
    AND stderr on ``DEVNULL`` (see
    ``kew.graph.driver.dispatch.DetachedDispatchSink.dispatch``), so every
    diagnostic an admission refusal prints is discarded: to anyone reading the
    driver log the child looks like a clean exit. Teeing the stream lets the
    child persist what it actually said onto the node before it exits.
    """

    def __init__(self, stream: TextIO) -> None:
        super().__init__()
        self._stream = stream
        self._lines: deque[str] = deque(maxlen=_REFUSAL_TAIL_LINES)
        self._partial = ""

    def write(self, s: str, /) -> int:
        *complete, self._partial = (self._partial + s).split("\n")
        for line in complete:
            # Drop the shared "[dispatch]" progress prefix: it says nothing a
            # hold reason on an epic node needs, and it costs characters the
            # bounded reason string would rather spend on the cause.
            text = _ANSI_ESCAPE.sub("", line).strip().removeprefix("[dispatch]").strip()
            if text:
                self._lines.append(text)
        return self._stream.write(s)

    def writable(self) -> bool:
        return True

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    def tail(self) -> str:
        """The remembered lines, oldest first, as one bounded string."""
        return " | ".join(self._lines)[:_REFUSAL_REASON_MAX_CHARS]


def _child_refusal_category(
    store: RunStore, item_id: str, *, since: datetime
) -> str | None:
    """The refusal category THIS child's dispatch recorded on the ledger.

    ``since`` bounds the read to events this invocation emitted, so an older
    dispatch of the same node can never be mistaken for the current refusal.
    Returns None when the refusal path emitted nothing (e.g. the fleet
    kill-switch latch), which reads as "not holdable".
    """
    events = store.list_audit_events(
        limit=1, event="dispatch_completed", item_id=item_id, since=since
    )
    if not events:
        return None
    category = (events[0].payload or {}).get("error_category")
    return category if isinstance(category, str) else None


def _child_created_run(store: RunStore, item_id: str, *, since: datetime) -> bool:
    """Return whether this child created a run row for the item."""
    return any(
        run.started_at >= since and run.status != "refused"
        for run in store.list_by_item(item_id)
    )


def _release_credential_reservation(
    store: RunStore, item_id: str, reservation_run_id: str | None, category: str | None
) -> None:
    if category == "credential_issuer_refused" and reservation_run_id is not None:
        store.release_epic_node_item(item_id, run_id=reservation_run_id)


def _record_credential_child_refusal(
    store: RunStore, run_id: str | None, item_id: str, reason: str
) -> None:
    from kew.audit import Actor, emit

    emit(
        store,
        "dispatch_completed",
        actor=Actor("policy", "credential-issuer"),
        run_id=run_id,
        item_id=item_id,
        payload={"error_category": "credential_issuer_refused", "reason": reason},
    )


def _report_child_refusal(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    item_id: str,
    detail: str,
    exit_code: int,
    since: datetime,
    known_category: str | None = None,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> None:
    """Put a refused dispatch child's reason somewhere the driver can read it.

    A refusal that needs an operator holds the node with the reason. It also
    writes a terminal ``refused`` row against the driver's reservation. A
    non-refusal run row keeps its normal failure lifecycle.
    Other refusals remain admissible and only produce a warning.
    """
    _require_dispatch_child_reservation(
        store,
        config,
        epic_ref,
        item_id,
        expected_reservation_run_id,
        expected_reservation_generation,
    )
    holds = {hold.item_id for hold in store.list_epic_holds(epic_ref)}
    category = known_category or _child_refusal_category(store, item_id, since=since)
    if item_id in holds:
        _warn(
            f"epic {epic_ref}: dispatch of {item_ref(item_id)} was refused "
            f"(exit {exit_code}); keeping its existing hold."
        )
        return
    if _child_created_run(store, item_id, since=since):
        _warn(
            f"epic {epic_ref}: dispatch of {item_ref(item_id)} failed "
            f"(exit {exit_code}); the run ledger keeps its normal lifecycle."
        )
        return
    holdable = category in _HOLDABLE_REFUSAL_CATEGORIES or (
        category == _RUNLESS_EXCEPTION_CATEGORY
    )
    if not holdable:
        _warn(
            f"epic {epic_ref}: dispatch of {item_ref(item_id)} was refused "
            f"(exit {exit_code}); the node stays admissible for the next tick."
        )
        return
    reason = admission_refused_hold_reason(
        detail or category or _ADMISSION_REFUSED_CATEGORY, item_id
    )
    record = _record_dispatch_child_refusal(
        store,
        config,
        epic_ref,
        item_id,
        _ADMISSION_REFUSED_CATEGORY,
        exit_code=exit_code,
        run_id=run_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
        status="refused",
    )
    if record.error_category == "reservation_replaced":
        raise typer.Exit(2)
    store.hold_epic_node(epic_ref, item_id, reason)
    # Only the driver-supplied reservation identity may release the node: a
    # store lookup here would adopt whatever owner holds it now (#1951).
    _release_credential_reservation(
        store, item_id, expected_reservation_run_id, category
    )
    _warn(f"epic {epic_ref}: held {item_ref(item_id)} — {reason}")


def _require_dispatch_child_runner(config: DispatchConfig, issue: Any) -> None:
    """Preflight the complexity route this child will launch."""
    raw_labels = issue.get("labels", [])
    labels = raw_labels if isinstance(raw_labels, list) else []
    complexity = _c.get_complexity(labels)
    try:
        resolved = _c.resolve_runner(config, complexity=complexity)
    except ValueError as exc:
        raise _RunnerPreflightRefusal(str(exc)) from exc
    if is_detached_credential_child():
        from kew.inference_transport import resolve_inference_transport

        require_credential_route(
            resolved.runner,
            resolve_inference_transport(config, resolved.runner, resolved.model),
        )
    failure = _c.runner_preflight_failure(config, runner_name=resolved.runner.name)
    if failure is not None:
        raise _RunnerPreflightRefusal(failure)


def _dispatch_child_node(
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    *,
    epic_ref: str,
    item_id: str,
    provider: str,
    run_kind: str,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> DispatchOutcome:
    """Run the provider-appropriate dispatch for one detached-child node."""
    if provider != "github":
        return _c._dispatch_linear_node(
            store,
            config,
            notifier,
            epic_ref=epic_ref,
            item_id=item_id,
            provider=provider,
            run_kind=run_kind,
            run_id=run_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
        )
    issue_dict = _c.select_next_issue(config=config, force_issue=int(item_id))
    if issue_dict is None:
        _record_dispatch_child_refusal(
            store,
            config,
            epic_ref,
            item_id,
            "dispatch_child_refused",
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            exit_on_replaced=True,
        )
        _error(f"epic {epic_ref}: could not fetch issue #{item_id} to dispatch")
        raise typer.Exit(2)
    _require_dispatch_child_runner(config, issue_dict)
    return _c.execute_dispatch(
        issue=issue_dict,
        config=config,
        store=store,
        notifier=notifier,
        approved=True,
        epic_ref=epic_ref,
        run_kind=run_kind,
        run_id=run_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )


_CHILD_EXIT_AS_IS_CATEGORIES = frozenset({"reservation_replaced", "requeued_over_cap"})


def _child_outcome_codes(outcome: DispatchOutcome) -> tuple[int, int, str | None]:
    """Raw exit, public exit, and the category a child exits with unreported."""
    exit_code = outcome.exit_code
    public_code = _c._dispatch_exit_code(
        _c.DispatchCommandResult(
            "not_launched" if exit_code and not outcome.launched else "executed",
            exit_code,
        )
    )
    category = getattr(outcome, "category", None)
    known = category if category in _CHILD_EXIT_AS_IS_CATEGORIES else None
    return exit_code, public_code, known


def _run_dispatch_child(
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    *,
    epic_ref: str,
    item_id: str,
    provider: str,
    run_kind: str,
    run_id: str | None = None,
    credential_route: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> None:
    """Dispatch one node and refuse to exit 0 on a refusal (#1658).

    The dispatch machinery reports every pre-spawn refusal by RETURNING a
    non-zero ``DispatchOutcome`` after printing why. Detached, both halves used
    to vanish: the exit code because this command dropped the outcome on the
    floor, and the diagnostics because the driver spawns the child on DEVNULL.
    Tee stderr around the dispatch, then record what the child said and exit
    with the public execution code. Keep the raw outcome for diagnostics.
    """
    tail = _StderrTail(sys.stderr)
    started = _c.datetime.now(UTC)
    known_category: str | None = None
    refusal_detail = ""
    try:
        with (
            contextlib.redirect_stderr(tail),
            detached_credential_handoff(credential_route, run_id),
        ):
            exit_code, public_code, known_category = _child_outcome_codes(
                _dispatch_child_node(
                    store,
                    config,
                    notifier,
                    epic_ref=epic_ref,
                    item_id=item_id,
                    provider=provider,
                    run_kind=run_kind,
                    run_id=run_id,
                    expected_reservation_run_id=expected_reservation_run_id,
                    expected_reservation_generation=expected_reservation_generation,
                )
            )
    except _ReservationRefusalRecorded:
        raise typer.Exit(2) from None
    except IssuerError as exc:
        refusal_detail = exc.reason
        _error(refusal_detail)
        # A refused issuer never launched, so both axes are the refusal code.
        exit_code = public_code = 2
        known_category = "credential_issuer_refused"
        _record_credential_child_refusal(store, run_id, item_id, exc.reason)
    except _RunnerPreflightRefusal as exc:
        refusal_detail = str(exc)
        _error(refusal_detail)
        exit_code = public_code = 2
        known_category = _RUNLESS_EXCEPTION_CATEGORY
    except typer.Exit as exc:
        exit_code = int(exc.exit_code or 0)
        public_code = 2 if exit_code else 0
    if exit_code == 0:
        return
    if known_category in _CHILD_EXIT_AS_IS_CATEGORIES:
        raise typer.Exit(public_code)
    _report_child_refusal(
        store,
        config,
        epic_ref=epic_ref,
        item_id=item_id,
        detail=refusal_detail or tail.tail(),
        exit_code=exit_code,
        since=started,
        known_category=known_category,
        run_id=run_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )
    raise typer.Exit(public_code)


@epic_app.command("_review-node", hidden=True)
def epic_review_node(
    epic_ref: str,
    node_id: str,
    run_id: str = typer.Option(..., "--run-id", hidden=True),
    base: str = typer.Option("main", "--base", hidden=True),
    facts_json: str = typer.Option(..., "--facts", hidden=True),
    integration_only: bool = typer.Option(False, "--integration-only", hidden=True),
) -> None:
    """Detached review-stage child spawned by the epic driver for one node.

    The driver reserved the node under the ``review`` pseudo-epic and wrote this
    child's ``review_stage`` run row before spawning it. The child takes the
    node's one review action — a reviewer round, a remediation, or the
    merge-main step — through the same code a synchronous tick runs, against
    the facts that tick read, then releases the reservation. A child whose
    reservation is no longer its own does nothing and exits 2.
    """
    from kew.graph.driver.review_detach import DetachedReview, facts_from_json
    from kew.graph.driver.review_stage import drive_detached_node

    config, repo, _, _ = _c._epic_driver_config(None)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        config = _c.load_config(store=store)
        provider = _c.provider_from_config(config)
        try:
            item_id = _c.validate_item_id(node_id, provider)
        except InvalidItemIdError as exc:
            _error(f"epic {epic_ref}: {exc}")
            raise typer.Exit(2) from exc
        review = DetachedReview(
            epic_ref=epic_ref,
            node_id=node_id,
            item_id=item_id,
            run_id=run_id,
            facts=facts_from_json(facts_json),
            integration_only=integration_only,
        )
        ran = drive_detached_node(
            review,
            config=config,
            store=store,
            repo=repo,
            repo_root=_git_repo_root(project_root()),
            base=base,
            reviewer_launcher=sandbox_reviewer_launcher(config, store),
            remediator=sandbox_remediator(config, store, epic_ref=epic_ref),
            base_integrator=sandbox_base_integrator(config, store, epic_ref=epic_ref),
            worker_cap=config.fleet_concurrency(),
        )
    if not ran:
        raise typer.Exit(2)


@epic_app.command("_dispatch-node", hidden=True)
def epic_dispatch_node(
    epic_ref: str,
    item: str,
    run_kind: str = typer.Option("dispatch", "--run-kind", hidden=True),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True),
    credential_route: str | None = typer.Option(
        None, "--credential-route", hidden=True
    ),
    reservation_run_id: str | None = typer.Option(
        None, "--reservation-run-id", hidden=True
    ),
    reservation_generation: str | None = typer.Option(
        None, "--reservation-generation", hidden=True
    ),
) -> None:
    """Detached dispatch child spawned by the epic driver for one node.

    ``item`` is the durable work-item id (L3): a GitHub issue number as a
    string, or a tracker id like ``SFD-12``. The driver already reserved this
    node in the run-store (the authoritative in-flight signal) before spawning
    this process, so this child does NOT call ``try_claim_issue`` and dispatches
    with ``approved=True`` — the cost/approval gate must not re-hold a node the
    driver already authorized.

    Every refusal exits non-zero and, when it needs an operator, leaves the
    reason on the node as a hold (see ``_run_dispatch_child``).
    """
    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        _require_dispatch_child_reservation(
            store,
            config,
            epic_ref,
            item,
            reservation_run_id,
            reservation_generation,
            run_kind=run_kind,
        )
        activate_detached_publication_token()
        _c.preflight_tools()
        # Re-resolve config WITH the store so allowlisted DB config_overrides
        # govern this child's budget preflight (#892/O1). Without the store the
        # child silently drops every override and can refuse a node the web
        # /config surface presents as effective — the 2026-07-20 live incident.
        config = _c.load_config(store=store)
        provider = _c.provider_from_config(config)
        try:
            item_id = _c.validate_item_id(item, provider)
        except InvalidItemIdError as exc:
            _error(f"epic {epic_ref}: {exc}")
            raise typer.Exit(2) from exc
        notifier = _c.build_notifier(config)
        try:
            _run_dispatch_child(
                store,
                config,
                notifier,
                epic_ref=epic_ref,
                item_id=item_id,
                provider=provider,
                run_kind=run_kind,
                run_id=run_id,
                credential_route=credential_route,
                expected_reservation_run_id=reservation_run_id,
                expected_reservation_generation=reservation_generation,
            )
        finally:
            # The driver issued this child's bounded key against ``run_id``. A
            # child that never wrote a row for it leaves the key with nothing
            # terminal to reconcile against, so record an UNBOUND refusal here
            # — never one attributed to the current reservation owner (#1951).
            if run_id is not None and store.get(run_id) is None:
                _record_dispatch_child_refusal(
                    store,
                    config,
                    epic_ref,
                    item_id,
                    "dispatch_child_refused",
                    run_id=run_id,
                )


def _require_dispatch_child_reservation(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    item_id: str,
    expected_run_id: str | None,
    expected_generation: str | None,
    *,
    run_kind: str = "dispatch",
) -> None:
    """Reject stale children before credentials, provider setup, or remote work."""
    if expected_run_id is None:
        return
    try:
        capture_dispatch_reservation(
            store,
            epic_ref,
            item_id,
            expected_reservation_run_id=expected_run_id,
            expected_reservation_generation=expected_generation,
        )
    except DispatchReservationReplaced as exc:
        from kew.dispatch_preflight import record_replaced_reservation_refusal

        record_replaced_reservation_refusal(
            store,
            config,
            item_id,
            run_kind=run_kind,
        )
        _error(str(exc))
        raise typer.Exit(2) from exc


def _dispatch_linear_node(
    store: RunStore,
    config: DispatchConfig,
    notifier: Notifier,
    *,
    epic_ref: str,
    item_id: str,
    provider: str,
    run_kind: str = "dispatch",
    run_id: str | None = None,
    continuation_worktree: Path | None = None,
    continuation_branch: str | None = None,
    continuation_run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> DispatchOutcome:
    """Fetch a Linear node, then claim it through committed dispatch admission.

    Fetching never mutates the tracker. The admission callback assigns the
    viewer and starts the item after the pending row and reservation binding
    commit. A failure before spawn retains the claim and records manual recovery
    instructions. Continuations retain their claim.
    """

    continuation_values = (
        continuation_worktree,
        continuation_branch,
        continuation_run_id,
    )
    has_continuation_state = any(value is not None for value in continuation_values)
    is_continuation = all(value is not None for value in continuation_values)
    if has_continuation_state and not is_continuation:
        raise ValueError("continuation dispatch requires worktree, branch, and run id")

    tracker, issue_dict = _prepare_linear_dispatch(
        store,
        config,
        epic_ref,
        item_id,
        is_continuation,
        expected_reservation_run_id,
        expected_reservation_generation,
    )

    claim = _LinearDispatchClaim(
        tracker,
        store,
        item_id,
        epic_ref,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )
    return _c.execute_dispatch(
        issue=issue_dict,
        config=config,
        store=store,
        notifier=notifier,
        approved=True,
        epic_ref=epic_ref,
        item_id=item_id,
        provider=provider,
        on_admitted=claim.admit if not is_continuation else None,
        on_refuse=claim.retain if not is_continuation else None,
        on_terminal_failure=partial(
            _comment_linear_terminal_failure, tracker, item_id, epic_ref
        ),
        run_kind=run_kind,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
        worktree_dir=continuation_worktree,
        branch=continuation_branch,
        run_id=continuation_run_id or run_id,
    )


def _prepare_linear_dispatch(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    item_id: str,
    is_continuation: bool,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> tuple[Any, Any]:
    """Authenticate and fetch a Linear item without mutating its tracker state."""
    from kew.linear.client import LinearClient
    from kew.linear.tracker import LinearTracker as _LinearTracker

    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        _record_dispatch_child_refusal(
            store,
            config,
            epic_ref,
            item_id,
            "dispatch_child_refused",
            exit_code=2,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            exit_on_replaced=True,
        )
        _error(
            f"epic {epic_ref}: tracker.type is 'linear' but the LINEAR_API_KEY "
            "environment variable is not set."
        )
        raise typer.Exit(2)

    tracker = _LinearTracker(config, LinearClient(api_key))
    try:
        issue_dict = tracker.fetch_issue(item_id)
    except LinearApiError as exc:
        if not is_continuation:
            _record_dispatch_child_refusal(
                store,
                config,
                epic_ref,
                item_id,
                "dispatch_child_refused",
                expected_reservation_run_id=expected_reservation_run_id,
                expected_reservation_generation=expected_reservation_generation,
                exit_on_replaced=True,
            )
        _error(
            f"epic {epic_ref}: could not fetch Linear item {item_id} to dispatch: {exc}"
        )
        raise typer.Exit(2) from exc

    _require_dispatch_child_runner(config, issue_dict)

    return tracker, issue_dict


@dataclass
class _LinearDispatchClaim:
    """Own one post-admission claim and record retention after refusal."""

    tracker: LinearTracker
    store: RunStore
    item_id: str
    epic_ref: str
    expected_reservation_run_id: str | None = None
    expected_reservation_generation: str | None = None
    run_id: str | None = None
    attempted: bool = False
    outcome_unknown: bool = False
    snapshot: dict[str, Any] | None = None
    completion_recorded: bool = False
    completion_source: str = "mutation"
    expected_state_id: str = ""
    expected_assignee_id: str = ""
    verification_attempted: bool = False

    def _valid_snapshot(self, snapshot: Any) -> bool:
        from kew.linear.tracker import claim_snapshot_matches

        return claim_snapshot_matches(
            snapshot,
            self.item_id,
            self.expected_state_id,
            self.expected_assignee_id,
        )

    def _record(self, event: str, **payload: Any) -> None:
        self.store.append_audit_event(
            timestamp=_c.datetime.now(UTC).isoformat(),
            event=event,
            actor_type="policy",
            actor_id="dispatch-admission",
            run_id=self.run_id,
            item_id=self.item_id,
            payload={"epic_ref": self.epic_ref, **payload},
        )

    def admit(self, run_id: str) -> None:
        self.run_id = run_id
        reservation = capture_dispatch_reservation(
            self.store,
            self.epic_ref,
            self.item_id,
            expected_reservation_run_id=self.expected_reservation_run_id,
            expected_reservation_generation=self.expected_reservation_generation,
        )
        if reservation is not None:
            self.expected_reservation_run_id = reservation.run_id
            self.expected_reservation_generation = reservation.run_id
        state = self.tracker.claim_state()
        self.expected_state_id = state["id"]
        self.expected_assignee_id = self.tracker.claim_assignee()
        self._record(
            "linear_claim_started", stateId=state["id"], stateName=state["name"]
        )
        self.attempted = True
        try:
            snapshot = self.tracker.claim(self.item_id).get("issue")
            if not self._valid_snapshot(snapshot):
                self.outcome_unknown = True
                raise LinearApiError(
                    "Linear claim response has no valid issue snapshot.",
                    ambiguous=True,
                )
            self.snapshot = snapshot
        except Exception as exc:
            if (
                self.outcome_unknown
                or getattr(self.tracker, "mutation_outcome_ambiguous", False) is True
            ):
                self.outcome_unknown = True
                self._record("linear_claim_outcome_unknown", error=str(exc))
            raise
        self._record_completion()

    def _record_completion(self) -> None:
        if self.completion_recorded:
            return
        if not self._valid_snapshot(self.snapshot):
            raise LinearApiError("Linear claim completion requires a valid snapshot.")
        self._record(
            "linear_claim_completed",
            source=self.completion_source,
            **self._snapshot_payload(),
        )
        self.completion_recorded = True

    def _snapshot_payload(self) -> dict[str, Any]:
        snapshot = self.snapshot or {}
        return {
            "snapshot": self.snapshot,
            "updatedAt": snapshot.get("updatedAt"),
            "stateId": (snapshot.get("state") or {}).get("id"),
            "assigneeId": (snapshot.get("assignee") or {}).get("id"),
        }

    def _verify_unknown_claim(self) -> None:
        """Resolve ambiguity only from a matching remote assignee and state."""
        if self.verification_attempted:
            return
        self.verification_attempted = True
        try:
            snapshot = self.tracker.verify_claim(self.item_id)
            self.snapshot = snapshot if self._valid_snapshot(snapshot) else None
        except Exception as exc:
            logging.getLogger(__name__).log(
                logging.DEBUG,
                "cannot verify Linear claim for %s: %s",
                self.item_id,
                exc,
            )
            return  # retain() records the unknown outcome and warns.
        if self.snapshot is not None:
            self.completion_source = "verification"
            self.outcome_unknown = False

    def retain(self, reason: str) -> None:
        """Retain the claim: Linear updates have no version precondition."""
        if not self.attempted:
            return
        if self.outcome_unknown:
            self._verify_unknown_claim()
        if self.outcome_unknown:
            reason = "claim outcome unknown"
        elif self.snapshot is None:
            return
        if self.snapshot is not None:
            self._retry_completion()
        try:
            record_retained_claim(
                self.store,
                self.item_id,
                reason=reason,
                run_id=self.run_id,
                epic_ref=self.epic_ref,
                reservation_run_id=self.expected_reservation_run_id,
                reservation_generation=self.expected_reservation_generation,
                snapshot=self.snapshot,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Cannot persist Linear claim retention for %s; recovery required: %s",
                self.item_id,
                LINEAR_CLAIM_RECOVERY,
            )
            raise
        self.attempted = False
        _warn(
            f"epic {self.epic_ref}: Linear claim retained for {self.item_id}: "
            f"{reason}. "
            f"{LINEAR_CLAIM_RECOVERY}"
        )

    def _retry_completion(self) -> None:
        """Audit failure must not erase a confirmed remote mutation."""
        try:
            self._record_completion()
        except Exception:
            logging.getLogger(__name__).exception(
                "Cannot persist Linear claim completion for %s; recording retention",
                self.item_id,
            )


def _comment_linear_terminal_failure(
    tracker: LinearTracker, item_id: str, epic_ref: str, category: str, reason: str
) -> None:
    """Leave an auditable comment for a terminal Linear dispatch failure."""
    bounded_category = (category or "failure")[:100]
    bounded_reason = (reason or "unspecified")[:500]
    try:
        tracker.comment(
            item_id,
            f"kew terminal dispatch outcome: {bounded_category}: {bounded_reason}",
        )
    except LinearApiError as exc:
        _warn(
            f"epic {epic_ref}: could not comment on terminal Linear item "
            f"{item_id}: {exc}"
        )


class _ReservationRefusalRecorded(typer.Exit):
    """An unbound replacement refusal already exists for this child."""


def _record_dispatch_child_refusal(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    item_id: str,
    category: str,
    exit_code: int = 1,
    *,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    status: RunStatus = "failure",
    exit_on_replaced: bool = False,
) -> RunRecord:
    """Write a terminal RunRecord for a dispatch child that exits BEFORE
    ``execute_dispatch`` ever created a run row (key missing / fetch / claim /
    precondition failure).

    Without this the driver's reservation dangles until grace expiry with no
    visible outcome (#809). Only the caller's expected reservation can own
    this row. The write transaction checks ownership; replacement produces
    a fresh unbound refusal. Missing identity never adopts a current owner.
    ``exit_code`` mirrors the process exit the caller raises (2 for a missing
    key, 1 for a fetch/claim failure) so downstream reporting keyed on it is
    not misled by a hardcoded value.
    """
    from kew.dispatch_preflight import record_owned_refusal
    from kew.run_store import RunRecord, _epic_issue_num

    now = _c.datetime.now(UTC)
    record = record_owned_refusal(
        store,
        RunRecord(
            run_id=run_id or str(uuid.uuid4()),
            issue_num=_epic_issue_num(item_id),
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            status=status if expected_reservation_run_id is not None else "refused",
            exit_code=exit_code,
            started_at=now,
            finished_at=now,
            error_category=category,
            repo=config.project.repo or None,
        ),
        epic_ref=epic_ref,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )
    if exit_on_replaced and record.error_category == "reservation_replaced":
        raise _ReservationRefusalRecorded(2)
    return record


@acceptance_app.command("amend")
def acceptance_amend(
    name: str,
    item: str,
    reason: Annotated[
        str, typer.Option("--reason", help="Why the contract changes; attested.")
    ],
    author: Annotated[
        bool,
        typer.Option(
            "--author",
            help="Refer the node's open dispute to the acceptance author instead:"
            " its next dispatch re-runs the author with the dispute as input.",
        ),
    ] = False,
    checkout: Annotated[
        Path | None,
        typer.Option(
            "--checkout",
            help="Checkout whose HEAD carries the amended file (default: the"
            " project root).",
        ),
    ] = None,
) -> None:
    """Record an amendment to NAME's node ITEM's acceptance contract.

    Commit the amended `kew/tests/acceptance/test_issue_<N>.py` first; this
    records the file as committed at HEAD as the contract's next version, with
    an `acceptance_amended` audit event carrying who, why, the old and new
    sha256 and the dispute it answered, and releases an `acceptance-disputed`
    hold. Exits 2 and records nothing when the node has no contract, the file
    is not committed at HEAD, or it is unchanged.
    """
    config = _c.load_config()
    provider = _c.provider_from_config(config)
    try:
        item_id = _c.validate_item_id(item, provider)
    except InvalidItemIdError:
        _error(f"kew epic acceptance amend: not a valid {provider} item id: {item!r}")
        raise typer.Exit(2) from None
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    repo = config.project.repo or None
    with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
        try:
            if author:
                refer_dispute_to_author(
                    store,
                    epic_ref=name,
                    item_id=item_id,
                    reason=reason,
                    actor=resolve_actor(),
                    repo=repo,
                )
                _success(
                    f"{item_ref(item_id)}: dispute referred to the acceptance"
                    " author; its next dispatch answers it before the implementer."
                )
                return
            row = amend_by_hand(
                store=store,
                epic_ref=name,
                item_id=item_id,
                checkout=checkout or project_root(),
                reason=reason,
                actor=resolve_actor(),
                repo=repo,
            )
        except (AcceptanceAmendmentRefused, AcceptanceAuthorRefused) as exc:
            _error(f"kew epic acceptance amend: {exc}")
            raise typer.Exit(2) from None
    _success(
        f"{item_ref(item_id)}: acceptance contract v{row.version} recorded"
        f" ({row.sha256[:12]} at {row.commit[:12]})."
    )
