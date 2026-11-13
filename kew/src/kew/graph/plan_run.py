"""Orchestrates `kew epic plan`: governed dispatch of the planning agent.

Flow: refuse-if-exists → (optionally) dispatch planning agent in a fresh
standalone checkout → load + validate manifest → dry-run preview or create issues →
mandatory post-creation validate gate → Mermaid render + next steps.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE, FINDINGS_EXIT_CODE
from kew.criteria import extract_criteria
from kew.graph.adapters import make_dependency_source
from kew.graph.build import build_graph
from kew.graph.export import render
from kew.graph.plan import (
    MANIFEST_RELPATH,
    TARGET_FILES_LIMIT,
    CreatedIssue,
    EpicPlanManifest,
    ManifestError,
    PartialCreationError,
    PlanNodes,
    build_epic_plan_prompt,
    build_resplit_instruction,
    create_issues,
    draft_graph,
    ensure_labels,
    link_native_relations,
    load_manifest,
    oversized_issues,
    plan_node_issues,
    plan_reuse,
    validate_manifest,
)
from kew.graph.validate import validate
from kew.inference_transport import resolve_inference_transport
from kew.progress import _error, _log, _success, _warn
from kew.sandbox import SandboxRuntimeUnavailable, resolve_flow_containment
from kew.subprocess_utils import run
from kew.tracking import LaunchState, startup_refused
from kew.worktree import (
    _clone_checkout_on,
    claim_checkout,
    detect_base_branch,
    release_checkout_claim,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.graph.domain import Graph, Node
    from kew.run_store import RunStore


# The name lands in a git branch ref and a GitHub label; keep it to
# characters that are safe in both ("..`" and trailing ".lock" are illegal
# in refnames even with this alphabet).
_EPIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_MANIFEST_EXPORT_BYTES = 1024 * 1024
"""Largest planning manifest copied from an agent checkout."""


@dataclass(frozen=True)
class PlanDispatchOutcome:
    """Retain launch evidence and availability through planning retries."""

    launched: bool
    manifest_path: Path | None = None
    failure: str | None = None
    could_not_run: bool = False


def _valid_epic_name(name: str) -> bool:
    return (
        bool(_EPIC_NAME_RE.match(name))
        and ".." not in name
        and not name.endswith(".lock")
    )


def _plan_budget_bucket(name: str) -> str:
    """Distinct per-invocation budget bucket (durable ``item_id``) for a
    planning run.

    Planning runs carry ``issue_num=0`` (there is no work item). Without a
    durable ``item_id`` every planning run collapses onto the ``#0`` sentinel
    bucket, so ``list_by_issue(0)`` sums the spend of *all* planning runs ever —
    the per-issue cap then eventually blocks every future planning dispatch
    (#1038). Keying each invocation on its own bucket makes the per-issue cap
    govern one planning run; a re-split retry (same invocation) intentionally
    shares the bucket because the timestamp+uuid is computed once and threaded
    to both attempts. The uuid suffix guarantees distinctness even for two
    invocations of the same epic within the same second. Daily/weekly aggregate
    caps are unaffected — they sum over every run regardless of item id.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"epic-plan:{name}:{ts}:{uuid.uuid4().hex[:8]}"


def _plan_branch(name: str) -> str:
    """The branch kew creates for a planning run.

    One definition, used by setup and by cleanup, so the name cleanup deletes
    is the name kew authored rather than whatever the planning agent left the
    checkout pointing at (#1445).
    """
    return f"agent/epic-plan-{name}"


def _plan_checkout_path(name: str, config: DispatchConfig) -> Path:
    """Compute the lease target without inspecting the planning checkout."""
    return Path(config.branches.worktree_base) / "agent" / f"epic-plan-{name}"


def _setup_plan_worktree(name: str, config: DispatchConfig) -> Path:
    """Create a standalone clone on origin/<base> for one planning run."""
    branch_name = _plan_branch(name)
    worktree_dir = _plan_checkout_path(name, config)
    claim_checkout(worktree_dir)
    base_branch = detect_base_branch(config.branches.base)
    run(["git", "fetch", "origin", base_branch, "--quiet"])
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    _clone_checkout_on(worktree_dir, branch_name, start_point=base_branch)
    return worktree_dir


def _copy_plan_into(worktree_dir: Path, plan_path: Path) -> str:
    """Copy the plan file into the checkout at the same relative path.

    Plans are often uncommitted when planning starts, and the checkout is cut
    from origin/<base> — without the copy the agent may find nothing to read.
    Returns the checkout-relative path handed to the prompt.
    """
    toplevel = run(["git", "rev-parse", "--show-toplevel"], check=False)
    root = (
        Path(toplevel.stdout.strip())
        if toplevel.returncode == 0 and toplevel.stdout.strip()
        else Path.cwd()
    )
    try:
        rel = plan_path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(plan_path.name)
    target = worktree_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_path, target)
    return str(rel)


def _read_plan_manifest(source: Path) -> bytes:
    """Read a bounded regular file without following its final path component."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeError(
            f"could not securely open {source}; symlinks and special files "
            f"are refused: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                f"refusing manifest export: {source} is not a regular file"
            )
        if metadata.st_size > MAX_MANIFEST_EXPORT_BYTES:
            raise RuntimeError(
                f"refusing manifest export: {source} exceeds the "
                f"{MAX_MANIFEST_EXPORT_BYTES}-byte size limit"
            )
        content = bytearray()
        while len(content) <= MAX_MANIFEST_EXPORT_BYTES:
            chunk = os.read(descriptor, MAX_MANIFEST_EXPORT_BYTES + 1 - len(content))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise RuntimeError(
            f"refusing manifest export: {source} exceeds the "
            f"{MAX_MANIFEST_EXPORT_BYTES}-byte size limit"
        )
    except OSError as exc:
        raise RuntimeError(f"could not securely read manifest {source}: {exc}") from exc
    finally:
        os.close(descriptor)


def _export_plan_manifest(source: Path, destination: Path) -> None:
    """Copy a validated agent manifest into the host logging directory."""
    content = _read_plan_manifest(source)
    try:
        destination.write_bytes(content)
    except OSError as exc:
        raise RuntimeError(f"could not save manifest at {destination}: {exc}") from exc


def _dispatch_plan_agent(
    *,
    name: str,
    plan_path: Path,
    config: DispatchConfig,
    store: RunStore | None,
    item_id: str,
    extra_instruction: str | None = None,
) -> PlanDispatchOutcome:
    """Classify planning failures using evidence retained at the launch boundary.

    The planning checkout lease is held through preparation, execution, and
    export; a busy checkout is a could-not-run outcome like any other refusal.
    """
    launch_state = LaunchState()
    checkout = _plan_checkout_path(name, config)
    try:
        claim_checkout(checkout)
        try:
            manifest_path = _run_plan_agent(
                name=name,
                plan_path=plan_path,
                config=config,
                store=store,
                item_id=item_id,
                launch_state=launch_state,
                extra_instruction=extra_instruction,
            )
        finally:
            release_checkout_claim(checkout)
    except Exception as exc:
        _error(f"Planning dispatch failed: {exc}")
        launched = launch_state.kind == "executed"
        return PlanDispatchOutcome(
            launched=launched, failure=str(exc), could_not_run=not launched
        )
    return PlanDispatchOutcome(
        launched=launch_state.kind == "executed", manifest_path=manifest_path
    )


def _require_plan_runner(
    config: DispatchConfig, runner_name: str, sandboxed: bool, worktree_dir: Path
) -> None:
    """Refuse native planning before launch when the runner is unavailable."""
    from kew.runner_preflight import runner_preflight_failure

    if sandboxed:
        return
    failure = runner_preflight_failure(config, runner_name=runner_name)
    if failure is not None:
        _warn(f"Checkout preserved for debugging: {worktree_dir}")
        raise RuntimeError(failure)


def _run_plan_agent(
    *,
    name: str,
    plan_path: Path,
    config: DispatchConfig,
    store: RunStore | None,
    item_id: str,
    launch_state: LaunchState,
    extra_instruction: str | None = None,
) -> Path:
    """Run the planning agent and export its manifest; raise on failure.

    ``extra_instruction`` is appended to the base prompt on a re-split retry so
    the agent sees exactly which nodes to break up (see
    ``_dispatch_plan_with_resplit``).

    ``item_id`` is the per-invocation budget bucket (:func:`_plan_budget_bucket`)
    so planning spend does not accumulate onto the shared ``#0`` sentinel (#1038).
    """
    from kew.dispatch_core import budget_watchdog, run_tracked_agent  # lazy: heavy
    from kew.runners import resolve_runner

    try:
        worktree_dir = _setup_plan_worktree(name, config)
        plan_relpath = _copy_plan_into(worktree_dir, plan_path)
    except (subprocess.CalledProcessError, OSError) as exc:
        _error(f"Could not prepare the planning checkout: {exc}")
        raise
    prompt = build_epic_plan_prompt(
        epic_name=name, plan_path=plan_relpath, config=config
    )
    if extra_instruction:
        prompt = f"{prompt}\n\n{extra_instruction}"
    resolved = resolve_runner(config, complexity="complex")
    flow_run_id = str(uuid.uuid4())
    inference_transport = resolve_inference_transport(
        config, resolved.runner, resolved.model
    )
    try:
        containment = resolve_flow_containment(
            store,
            flow="epic planning",
            runner=resolved.runner.name,
            config=config.sandbox,
            # The planning agent runs as a container argv, so the OpenShell
            # gateway is not a boundary this flow can launch into.
            gateway_launch=False,
            run_id=flow_run_id,
            checkout_dir=worktree_dir,
            repo=config.project.repo or None,
            issue_num=0,
            item_id=item_id,
            inference_transport=inference_transport,
        )
    except SandboxRuntimeUnavailable as exc:
        _error(f"Planning agent containment refused: {exc}")
        _warn(f"Checkout preserved for debugging: {worktree_dir}")
        raise
    _require_plan_runner(
        config, resolved.runner.name, containment.sandboxed, worktree_dir
    )
    _log(f"Dispatching planning agent for epic '{name}'...")
    result = run_tracked_agent(
        launch_state=launch_state,
        prompt=prompt,
        worktree_dir=worktree_dir,
        config=config,
        issue_num=0,  # no issue; per-item spend keys on item_id, not this sentinel
        store=store,
        title=f"epic-plan:{name}",
        branch="",  # disable the PR retry guard — no PR is expected
        model=resolved.model,
        runner=resolved.runner,
        complexity="complex",
        run_kind="planning",  # bucket planning spend apart from dispatch (#445)
        run_id=flow_run_id,
        item_id=item_id,  # per-invocation budget bucket, not the #0 sentinel (#1038)
        contained_image=containment.image,
        inference_transport=inference_transport,
        sandbox_posture=containment.posture,
        validate_continuation=False,
        budget_check=budget_watchdog(
            config.agent.max_budget_usd,
            runner_name=resolved.runner.name,
            contained=containment.sandboxed,
        ),
    )
    launch_state.observe(result)
    if result.exit_code != 0 or startup_refused(result):
        _error(f"Planning agent failed (exit {result.exit_code}).")
        _warn(f"Checkout preserved for debugging: {worktree_dir}")
        raise RuntimeError(f"Planning agent failed (exit {result.exit_code}).")

    manifest_src = worktree_dir / MANIFEST_RELPATH
    if not os.path.lexists(manifest_src):
        _error(f"Agent exited 0 but wrote no {MANIFEST_RELPATH}.")
        _warn(f"Checkout preserved for debugging: {worktree_dir}")
        raise RuntimeError(f"Agent exited 0 but wrote no {MANIFEST_RELPATH}.")

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(config.logging.dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / f"epic-plan-{name}-{ts}.json"
    try:
        _export_plan_manifest(manifest_src, saved)
    except RuntimeError as exc:
        _error(f"Could not export the planning manifest: {exc}")
        _warn(f"Checkout preserved for debugging: {worktree_dir}")
        raise
    _log(f"Manifest saved: {saved}")
    shutil.rmtree(worktree_dir, ignore_errors=True)
    return saved


def _dispatch_plan_with_resplit(
    *,
    name: str,
    plan_path: Path,
    config: DispatchConfig,
    store: RunStore | None,
) -> PlanDispatchOutcome:
    """Dispatch the planning agent, re-splitting an oversized draft once.

    An oversized node (>``TARGET_FILES_LIMIT`` target_files) is not a hard fail
    on the first pass: the agent gets one retry with an explicit re-split
    instruction naming the offending nodes. A still-oversized second draft
    hard-fails the plan (returns a failed outcome). The gate never carries an
    oversized node through to creation. Any other draft problem is left for the shared
    ``_load_validated_manifest`` gate to report.
    """
    # One budget bucket for the whole invocation: the re-split retry is part of
    # the same planning run, so both attempts share it (#1038).
    bucket = _plan_budget_bucket(name)
    extra: str | None = None
    launched = False
    for attempt in range(2):
        outcome = _dispatch_plan_agent(
            name=name,
            plan_path=plan_path,
            config=config,
            store=store,
            item_id=bucket,
            extra_instruction=extra,
        )
        launched = launched or outcome.launched
        outcome = replace(
            outcome,
            launched=launched,
            could_not_run=(outcome.could_not_run and not launched),
        )
        manifest_path = outcome.manifest_path
        if manifest_path is None:
            return outcome
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            # Malformed/schema-invalid draft: not a re-split case. Hand it to
            # the shared validation gate, which reports the precise problem.
            return outcome
        oversized = oversized_issues(manifest)
        if not oversized:
            return outcome
        if attempt == 0:
            ids = ", ".join(repr(i.local_id) for i in oversized)
            _warn(
                f"Draft has {len(oversized)} node(s) over the "
                f"{TARGET_FILES_LIMIT}-target-file limit ({ids}); re-dispatching "
                "the planning agent with a re-split instruction (one retry)."
            )
            extra = build_resplit_instruction(oversized)
    _error(
        "Re-planned draft is still oversized after one re-split retry — nothing "
        "created. Split the oversized node(s) by hand, then retry with "
        "--from-manifest."
    )
    return PlanDispatchOutcome(
        launched=launched, failure="Re-planned draft is still oversized."
    )


def _print_dry_run(
    manifest: EpicPlanManifest,
    name: str,
    manifest_path: Path,
    reuse: dict[str, int],
) -> None:
    graph = draft_graph(manifest)
    by_id = {i.local_id: i for i in manifest.issues}
    _log(
        f"[dry-run] {len(manifest.issues) - len(reuse)} issue(s) would be "
        f"created for epic:{name}"
    )
    for wave, level in enumerate(validate(graph).levels, start=1):
        for lid in level:
            issue = by_id[lid]
            deps = ", ".join(issue.depends_on) or "-"
            made = f" (already #{reuse[lid]})" if lid in reuse else ""
            _log(
                f"  wave {wave}: [{lid}]{made} {issue.title} "
                f"({issue.priority}, {issue.complexity}, {issue.type}; "
                f"depends on: {deps})"
            )
            # The criteria are what the finished work is judged against, so the
            # preview shows them: the dry run is the only look a human gets at
            # the draft before the issues exist.
            for criterion in extract_criteria(issue.body):
                _log(f"      - {criterion}")
    _log(render(graph))
    _log(f"Create for real: kew epic plan {name} --from-manifest {manifest_path}")


def _epic_precondition(
    name: str, config: DispatchConfig, amend: bool
) -> list[Node] | int:
    """Resolve the epic's existing nodes and enforce the fresh/amend rule.

    Returns the existing node list on success, or an int exit code on refusal.
    Bare plan (no --amend) plans a fresh epic only. Closed issues count too:
    the driver treats a closed dependency as DONE, so mixing a new DAG with a
    stale epic's issues silently satisfies dependencies. With --amend the
    existing nodes are returned so new local_ids can be checked for collisions
    and cross-existing-node depends_on edges resolved.
    """
    source = make_dependency_source(config)
    try:
        existing = source.list_epic_nodes(name)
    except RuntimeError as exc:
        _error(str(exc))
        return 2
    if existing and not amend:
        _error(
            f"Label 'epic:{name}' already has {len(existing)} issue(s) "
            "(open or closed). Bare `epic plan` plans a fresh epic only — pick "
            "a new name, or re-plan with --amend to append new nodes."
        )
        return 2
    if not existing and not amend:
        return _refuse_unlisted_plan_nodes(name, config)
    return existing


def _refuse_unlisted_plan_nodes(name: str, config: DispatchConfig) -> list[Node] | int:
    """Refuse a bare plan over issues an earlier, partly failed run made.

    Such a run created issues with the epic label but never wrote native
    membership, so the epic reads empty; planning afresh over them would make
    every node a second time. Their plan-node markers name them. Markers in
    issues another login wrote refuse it too: they may be an earlier run made
    before the gh login changed.
    """
    try:
        made = plan_node_issues(name, repo=config.project.repo)
    except RuntimeError as exc:
        _error(str(exc))
        return 2
    refusal = _unlisted_plan_node_refusal(name, made)
    if refusal:
        _error(refusal)
        return 2
    return []


def _unlisted_plan_node_refusal(name: str, made: PlanNodes) -> str:
    """Why a bare plan may not run over the label's plan-node issues, or ""."""
    if made.own:
        count = sum(len(claims) for claims in made.own.values())
        return (
            f"Label 'epic:{name}' already has {count} issue(s) an earlier "
            "`epic plan` made. Re-run that command with --amend to reuse them "
            "and create only the missing nodes."
        )
    if made.foreign:
        foreign = sorted(
            (c for claims in made.foreign.values() for c in claims),
            key=lambda c: c.number,
        )
        listed = ", ".join(
            f"#{c.number} (by {c.author or 'an unknown login'})" for c in foreign
        )
        return (
            f"Label 'epic:{name}' holds plan-node markers in issues the gh "
            f"login {made.login!r} did not write: {listed}. They may be an "
            "earlier `epic plan` run under another login. Re-run with --amend "
            "to plan beside them; kew never reuses them."
        )
    return ""


def _load_validated_manifest(
    name: str, manifest_path: Path, existing_ids: set[str]
) -> EpicPlanManifest | None:
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        _error(str(exc))
        return None
    if manifest.epic != name:
        _warn(f"Manifest epic {manifest.epic!r} != {name!r}; using {name!r}.")
    problems = validate_manifest(manifest, existing_ids)
    if problems:
        _error("Draft plan failed validation — nothing created:")
        for p in problems:
            _error(f"  - {p}")
        _log(f"Edit and retry: kew epic plan {name} --from-manifest {manifest_path}")
        return None
    return manifest


def _read_expected_graph(
    name: str,
    config: DispatchConfig,
    expected_ids: set[str],
) -> tuple[Graph | None, set[str], bool]:
    """Retry the read and report whether any attempt reached GitHub."""
    seen_ids: set[str] = set()
    # The LAST attempt's outcome, not an aggregate: a read that succeeded once
    # and then failed twice is a read failure, and reporting index lag would
    # send the operator to wait instead of fixing the credential.
    read_succeeded = False
    for attempt in range(3):
        try:
            candidate = build_graph(make_dependency_source(config), name)
        except (
            RuntimeError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
        ) as exc:
            # A provider read reaches gh through subprocess: a missing binary
            # or a bad payload must return through this gate, not escape it.
            _warn(f"Round-trip read failed: {exc}")
            candidate = None
            read_succeeded = False
            seen_ids = set()
        else:
            read_succeeded = True
        if candidate is not None:
            # MEMBERS only. build_graph resolves a dependency that is not
            # an epic member as an external boundary node, so counting
            # those would let a node membership lost satisfy the gate
            # merely because a sibling still depends on it -- and the
            # driver never dispatches an external node.
            seen_ids = {
                node_id
                for node_id, node in candidate.nodes.items()
                if not node.external
            }
            if not expected_ids - seen_ids:
                return candidate, seen_ids, read_succeeded
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return None, seen_ids, read_succeeded


def _report_missing_nodes(
    name: str,
    expected_ids: set[str],
    seen_ids: set[str],
    *,
    read_succeeded: bool,
) -> None:
    """Name every node absent from the final round-trip read and its remedy.

    The three cases need different remedies. A failed read needs credential or
    connectivity repair. An empty successful read needs an index retry. A
    partial read needs membership repair. Every case exits non-zero.
    """
    if not read_succeeded:
        _error(
            "GitHub read could not be performed. Fix the credential or "
            f"connectivity, then run `kew epic validate {name}`."
        )
        return
    missing = sorted(expected_ids - seen_ids, key=lambda item: (len(item), item))
    refs = ", ".join(f"#{node_id}" for node_id in missing)
    if not seen_ids:
        _warn(
            f"GitHub hasn't indexed the new issues yet; validation deferred — "
            f"run `kew epic validate {name}` in a moment."
        )
        _error(
            f"Round-trip read returned no epic node at all; expected {refs}. "
            f"Re-read with `kew epic validate {name}` before treating any of "
            "them as lost."
        )
        return
    _error(
        f"Round-trip read is missing expected epic node(s): {refs}. Restore "
        f"those nodes to the container for epic '{name}', then run "
        f"`kew epic validate {name}`."
    )


def _post_creation_gate(
    name: str,
    config: DispatchConfig,
    created_count: int,
    expected_ids: set[str],
) -> int:
    """Validate only a round-tripped graph containing every expected node."""
    real, seen_ids, read_succeeded = _read_expected_graph(name, config, expected_ids)
    if real is None:
        _report_missing_nodes(
            name,
            expected_ids,
            seen_ids,
            read_succeeded=read_succeeded,
        )
        return FINDINGS_EXIT_CODE if read_succeeded else COULD_NOT_RUN_EXIT_CODE
    report = validate(real)
    if not report.ok:
        _error("Post-creation validation failed — inspect the issues on GitHub:")
        for cyc in report.cycles:
            _error(f"  cycle: {' -> '.join([*cyc, cyc[0]])}")
        for nid in report.dangling:
            _error(f"  dangling dependency: #{nid}")
        return 1
    _log(render(real))
    _success(
        f"Epic '{name}': {created_count} issue(s) across {len(report.levels)} wave(s)."
    )
    _log(f"Review the issues on GitHub, then: kew epic run {name}")
    return 0


def _reusable_nodes(
    name: str, config: DispatchConfig, manifest: EpicPlanManifest
) -> dict[str, int] | None:
    """The manifest's local ids an earlier run already made an issue for.

    None, with each reason printed, when the issues cannot be read or a local
    id's earlier issue cannot stand in for its draft: nothing is created then.
    """
    try:
        made = plan_node_issues(name, repo=config.project.repo)
    except RuntimeError as exc:
        _error(str(exc))
        return None
    plan = plan_reuse(manifest, made, epic_name=name)
    for warning in plan.warnings:
        _warn(warning)
    for problem in plan.problems:
        _error(problem)
    return None if plan.problems else plan.numbers


def _report_partial_creation(
    exc: PartialCreationError, name: str, manifest_path: Path
) -> None:
    _error(f"Issue creation failed at {exc.failed_local_id!r}: {exc.cause}")
    made = [c for c in exc.created if not c.reused]
    if made:
        nums = ", ".join(f"#{c.number}" for c in made)
        _warn(f"Already created with label epic:{name}: {nums}")
    # Every issue kew made carries its plan-node marker, so the same manifest
    # applied again reuses those issues and creates only the rest.
    _log(
        f"Manifest kept: {manifest_path}. Re-run the same command with --amend "
        f"to finish: "
        f"kew epic plan {name} --from-manifest {manifest_path} --amend"
    )


def _link_created(
    created: list[CreatedIssue],
    name: str,
    config: DispatchConfig,
    plan_path: str,
    existing_ids: set[str],
) -> None:
    """Print each node, then write GitHub's own linkage for them."""
    for c in created:
        _success(f"#{c.number} {c.title}" + (" (reused)" if c.reused else ""))
    # GitHub's own linkage, written after the issues exist: membership as
    # sub-issues of the epic's container issue, each depends_on as a blocked_by
    # relation. Each write failure is printed. The exact read-back gate below
    # rejects any expected node missing after a partial membership write. A
    # reused issue the epic does not list yet (its first run failed before
    # this write) goes through it again; one already listed is left alone.
    for warning in link_native_relations(
        [c for c in created if not c.reused or str(c.number) not in existing_ids],
        epic_name=name,
        repo=config.project.repo,
        plan_path=plan_path,
        existing_ids=existing_ids,
    ):
        _warn(warning)


def _create_and_gate(
    manifest: EpicPlanManifest,
    name: str,
    config: DispatchConfig,
    manifest_path: Path,
    store: RunStore | None,
    existing_ids: set[str],
    reuse: dict[str, int],
) -> int:
    try:
        ensure_labels(manifest, epic_name=name, repo=config.project.repo)
        created = create_issues(
            manifest,
            epic_name=name,
            repo=config.project.repo,
            existing_ids=existing_ids,
            reuse=reuse,
        )
    except RuntimeError as exc:
        _error(str(exc))
        return 1
    except PartialCreationError as exc:
        _report_partial_creation(exc, name, manifest_path)
        return 1
    _link_created(created, name, config, manifest.plan_path, existing_ids)
    if store is not None:
        # Seed a driver-state row so `epic run --all` can see this epic before
        # its first tick — otherwise a freshly planned epic is invisible until
        # someone drives it by hand (#740).
        store.register_epic(name)
    _warn_if_plan_uncommitted(manifest.plan_path, config)
    expected_ids = {str(c.number) for c in created} | existing_ids
    return _post_creation_gate(name, config, len(created), expected_ids)


def _warn_if_plan_uncommitted(plan_path: str, config: DispatchConfig) -> None:
    """Dispatch checkouts are cut from origin/<base>; if the plan referenced
    by every issue body isn't there, the epic's dispatch agents fly blind."""
    try:
        base = detect_base_branch(config.branches.base)
        exists = run(
            ["git", "cat-file", "-e", f"origin/{base}:{plan_path}"], check=False
        )
    except Exception:  # best-effort check — never block on it
        return
    if exists.returncode != 0:
        _warn(
            f"The plan file `{plan_path}` is not on origin/{base}. Dispatch "
            "agents read it from a fresh checkout — commit and push it "
            "before `kew epic run`."
        )


def _plan_from_file(
    name: str, plan_path: Path | None, config: DispatchConfig, store: RunStore | None
) -> PlanDispatchOutcome:
    """Resolve the source plan before dispatching a planning agent."""
    if plan_path is None or not plan_path.exists():
        failure = f"Plan file not found: {plan_path}"
        _error(failure)
        return PlanDispatchOutcome(False, failure=failure, could_not_run=True)
    return _dispatch_plan_with_resplit(
        name=name, plan_path=plan_path, config=config, store=store
    )


def _apply_manifest(
    manifest: EpicPlanManifest,
    name: str,
    config: DispatchConfig,
    manifest_path: Path,
    store: RunStore | None,
    existing_ids: set[str],
    *,
    amend: bool,
    dry_run: bool,
) -> int:
    # Only an amend can meet issues an earlier run made: a bare plan refuses
    # an epic whose members or label-only plan nodes show any.
    reuse = _reusable_nodes(name, config, manifest) if amend else {}
    if reuse is None:
        return COULD_NOT_RUN_EXIT_CODE
    if dry_run:
        _print_dry_run(manifest, name, manifest_path, reuse)
        return 0
    return _create_and_gate(
        manifest, name, config, manifest_path, store, existing_ids, reuse
    )


def run_epic_plan(
    *,
    name: str,
    config: DispatchConfig,
    plan_path: Path | None,
    manifest_path: Path | None,
    dry_run: bool,
    store: RunStore | None,
    amend: bool = False,
) -> int:
    """Exit codes: 0 success, 1 executed/validation failure,
    2 could not run/query failure.
    """
    if not _valid_epic_name(name):
        _error(
            f"Invalid epic name {name!r}: use letters, digits, '.', '_' or "
            "'-' (it becomes a git branch and a GitHub label)."
        )
        return 2
    existing = _epic_precondition(name, config, amend)
    if isinstance(existing, int):
        return existing
    existing_ids = {n.id for n in existing}

    if manifest_path is None:
        outcome = _plan_from_file(name, plan_path, config, store)
        if outcome.could_not_run:
            return COULD_NOT_RUN_EXIT_CODE
        manifest_path = outcome.manifest_path
        if manifest_path is None:
            return FINDINGS_EXIT_CODE

    manifest = _load_validated_manifest(name, manifest_path, existing_ids)
    if manifest is None:
        return 1

    return _apply_manifest(
        manifest,
        name,
        config,
        manifest_path,
        store,
        existing_ids,
        amend=amend,
        dry_run=dry_run,
    )
