"""W1.1 node 5d: run the shadow comparison beside a real dispatch.

This is the only module that knows both the broker and ``DispatchConfig``.
:mod:`kew.publication_shadow` is deliberately config-free so it can be tested
against fixtures; everything that reads configuration, touches the operator's
directories, or prints lives here.

**Nothing here may affect the dispatch it observes.** ``compare_publication``
already guarantees that for itself; this adds the two steps that happen before
it — resolving the base commit and the legacy tree — and the ones after —
reporting and pruning. All of them are inside one fail-soft boundary, because a
shadow that can raise makes "leave it off" the correct operational choice.
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.audit import Actor, emit
from kew.progress import _dim, _log, _warn
from kew.publication import sanitize_publication_evidence
from kew.publication_shadow import (
    STATUS_FAILED,
    STATUS_MATCH,
    ShadowOutcome,
    compare_publication,
)
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore

__all__ = ["EVIDENCE_DIR_NAME", "resolve_shadow_inputs", "run_shadow_comparison"]

#: Evidence lives beside the logs it explains, under the operator's configured
#: log directory — never inside the agent's checkout, which is the one place it
#: must not be.
EVIDENCE_DIR_NAME = "shadow"

#: A workspace touched more recently than this may still be being written, so it
#: is never pruned regardless of the keep limit. Comparisons involve a network
#: fetch and a container run, so "recent" has to be generous; these are small
#: diagnostic artifacts and deleting one late costs nothing, while deleting one
#: early corrupts the evidence the feature exists to collect.
IN_FLIGHT_GRACE_SECONDS = 3600


def _git_read(checkout_dir: Path, args: list[str]) -> str:
    """A host git READ of the agent's checkout — shadow mode only.

    Acceptance criterion 1 of the spec forbids exactly this, and it is here on
    purpose and temporarily: these two reads sit next to the ones the legacy
    path already performs (``_checkout_has_no_changes``), so shadow mode adds no
    new CLASS of exposure to a flow that has not been fixed yet. They go away
    with the flip in node 6, which is when the criterion becomes enforceable.
    """
    result = run(["git", "-C", str(checkout_dir), *args], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(result.stderr or '').strip()}")
    value = (result.stdout or "").strip()
    if not value:
        raise RuntimeError(f"git {' '.join(args)} returned nothing")
    return value


def resolve_shadow_inputs(checkout_dir: Path, base_branch: str) -> tuple[str, str]:
    """``(base_sha, legacy_tree)`` for one comparison.

    *base_sha* is the merge base with the base branch — the commit the agent
    branched from, and therefore the one the producer must diff against. Taken
    from ``origin/<base>`` rather than the local branch so it names a commit the
    canonical remote actually has; a purely local base could not be fetched.

    *legacy_tree* is ``HEAD^{tree}``: exactly what the legacy path is about to
    publish, read here so the comparison itself never touches the checkout.
    """
    base_sha = _git_read(checkout_dir, ["merge-base", f"origin/{base_branch}", "HEAD"])
    legacy_tree = _git_read(checkout_dir, ["rev-parse", "HEAD^{tree}"])
    return base_sha, legacy_tree


def _publication_slug(config: DispatchConfig) -> str | None:
    """``owner/name`` for the canonical remote, resolved the way kew already
    resolves it for publication.

    Explicit config binds strongest; otherwise the OPERATOR checkout's ``origin``
    supplies it — deliberately not the agent's. Reading the agent's remote would
    let a dispatch point this fetch at a server it controls, and the whole point
    of the canonical repository is that it is the one source no agent has
    written to.

    Found by running shadow mode against kew's own configuration, which leaves
    ``project.repo`` empty: the feature refused to start, exactly as a real
    dispatch would have.
    """
    explicit = (config.project.repo or "").strip()
    if explicit:
        return explicit
    from kew.config import project_root
    from kew.dispatch_core import _repo_slug_from_origin

    return _repo_slug_from_origin(project_root())


def _project_host() -> str:
    """Return the operator checkout's pinned project host."""
    from kew.config import project_root
    from kew.repo_host import repo_host

    return repo_host(project_root())


def _evidence_root(config: DispatchConfig) -> Path:
    return Path(config.logging.dir).expanduser() / EVIDENCE_DIR_NAME


def prune_evidence(
    root: Path, keep: int, *, protect: str | None = None, now: float | None = None
) -> None:
    """Keep the *keep* most recent evidence directories, remove the stale rest.

    An unbounded pile of bare clones turns an observability feature into a
    disk-space incident, and the operator who most needs shadow mode on is the
    one least likely to be watching for it. Newest-first by name, which sorts
    correctly because the names are timestamped.

    **Two things are never pruned, because kew dispatches in parallel and every
    worker prunes this one directory.** Verified before fixing: a comparison
    that was still running had its workspace deleted out from under it by a
    later one finishing, and with ``keep=0`` a run deleted the very workspace
    whose path it had just printed.

    * *protect* — this run's workspace name, and anything sorting after it.
      Deterministic, and it keeps the reported evidence path true.
    * Anything modified within :data:`IN_FLIGHT_GRACE_SECONDS`. *protect* alone
      cannot cover a concurrent run that started EARLIER, because that
      workspace sorts before this one; recency can. A directory that might be
      in use is not safe to delete on a count alone.
    """
    if not root.is_dir():
        return
    cutoff = (now if now is not None else time.time()) - IN_FLIGHT_GRACE_SECONDS
    ordered = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    for stale in ordered[max(keep, 0) :]:
        if protect is not None and stale.name >= protect:
            continue
        try:
            if stale.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(stale, ignore_errors=True)


def _report(outcome: ShadowOutcome, slug: str) -> None:
    """One line per comparison, and the evidence path whenever there is any.

    A retained workspace nobody can find is the same as no workspace: the path
    is printed with the finding, not left for someone to derive.
    """
    if outcome.status == STATUS_MATCH:
        _dim(
            f"shadow[{slug}]: broker agrees ({outcome.entries} entries, "
            f"{outcome.deletions} deletions)"
        )
        return
    _warn(f"shadow[{slug}]: {outcome.status} — {outcome.detail}")
    for rejection in outcome.rejections[:5]:
        _warn(f"  rejected: {rejection}")
    for flag in outcome.flags[:5]:
        _warn(f"  flagged: {flag}")
    if outcome.evidence is not None:
        _log(f"  evidence: {outcome.evidence}")


def _bounded_line(value: object, limit: int) -> str:
    """Return one printable, bounded audit string.

    Path-redacted on top of the general sanitizer: these strings outlive the
    host in the audit ledger, where a checkout path is a leak rather than a
    salvage hint.
    """
    if not str(value):
        return ""
    return sanitize_publication_evidence(value)[:limit]


def _audit_payload(
    outcome: ShadowOutcome,
    *,
    item_id: str | None,
    branch: str | None,
    base_branch: str,
    runtime: str,
    image: str,
    runner: str | None,
    provenance: dict[str, str | bool | None] | None,
    base_sha: str | None,
    legacy_tree: str | None,
) -> dict[str, Any]:
    identity = provenance or {}
    return {
        "schema_version": 1,
        "item_id": item_id,
        "branch": branch,
        "base_branch": base_branch,
        "runtime": runtime,
        "image": image,
        "runner": runner,
        "orchestrator_version": identity.get("orchestrator_version"),
        "orchestrator_commit": identity.get("orchestrator_commit"),
        "orchestrator_dirty": identity.get("orchestrator_dirty"),
        "status": outcome.status,
        "base_sha": base_sha,
        "legacy_tree": outcome.legacy_tree or legacy_tree,
        "broker_tree": outcome.broker_tree,
        "entries": outcome.entries,
        "deletions": outcome.deletions,
        "rejection_count": len(outcome.rejections),
        "flag_count": len(outcome.flags),
        "detail": _bounded_line(outcome.detail, 512),
        "rejection_samples": [
            _bounded_line(value, 256) for value in outcome.rejections[:5]
        ],
        "flag_samples": [_bounded_line(value, 256) for value in outcome.flags[:5]],
        "evidence_basename": outcome.evidence.name if outcome.evidence else None,
    }


def _resolved_base_branch(config: DispatchConfig, base_branch: str | None) -> str:
    return base_branch or config.branches.base or "main"


def _configured_repo(config: DispatchConfig) -> str | None:
    return (config.project.repo or "").strip() or None


def _report_canonical_skip(
    config: DispatchConfig,
    *,
    slug: str,
    store: RunStore | None,
    run_id: str | None,
    issue_num: int | None,
    item_id: str | None,
    branch: str | None,
) -> None:
    """Record that canonical mode has no safe legacy comparison input."""
    emit(
        store,
        "shadow.skipped",
        actor=Actor("policy", "publication-shadow"),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=_configured_repo(config),
        payload={
            "schema_version": 1,
            "reason": "canonical_publication",
            "branch": branch,
        },
        dedup_key=f"publication-shadow-skip:v1:{run_id}",
    )
    _log(f"shadow[{slug}]: skipped for canonical publication")


def run_shadow_comparison(
    config: DispatchConfig,
    *,
    checkout_dir: Path,
    slug: str,
    image: str,
    store: RunStore | None = None,
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
    runner: str | None = None,
    provenance: dict[str, str | bool | None] | None = None,
    now: datetime | None = None,
) -> ShadowOutcome | None:
    """Compare what the broker WOULD publish against what the legacy path will.

    Returns ``None`` when shadow mode is off or canonical publication is active.
    Canonical mode emits ``shadow.skipped`` instead of reading legacy input
    through checkout-rooted Git after the agent has run.

    Never raises, and never changes what is published: the caller runs this for
    its report, not for its answer.
    """
    if not config.publication.shadow:
        return None
    if config.publication.publish == "canonical":
        _report_canonical_skip(
            config,
            slug=slug,
            store=store,
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch,
        )
        return None

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%f")
    workspace = _evidence_root(config) / f"{stamp}-{slug}"
    resolved_base_branch = _resolved_base_branch(config, base_branch)
    base_sha: str | None = None
    legacy_tree: str | None = None
    repo = _configured_repo(config)
    try:
        base_sha, legacy_tree = resolve_shadow_inputs(
            checkout_dir, resolved_base_branch
        )
        repo = _publication_slug(config)
        if not repo:
            raise RuntimeError(
                "cannot resolve the repository to fetch from — set project.repo, "
                "or give the operator checkout a GitHub `origin`"
            )
        outcome = compare_publication(
            runtime=config.sandbox.runtime,
            image=image,
            proxy_environment=config.sandbox.proxy_environment,
            checkout_dir=checkout_dir,
            workspace=workspace,
            remote=f"https://{_project_host()}/{repo}.git",
            base_sha=base_sha,
            legacy_tree=legacy_tree,
            slug=slug,
        )
    except BaseException as exc:  # noqa: BLE001 — see the module docstring
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        outcome = ShadowOutcome(
            status=STATUS_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            legacy_tree=legacy_tree,
        )

    emit(
        store,
        "publication_shadow_outcome",
        actor=Actor("policy", "publication-shadow"),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload=_audit_payload(
            outcome,
            item_id=item_id,
            branch=branch,
            base_branch=resolved_base_branch,
            runtime=config.sandbox.runtime,
            image=image,
            runner=runner,
            provenance=provenance,
            base_sha=base_sha,
            legacy_tree=legacy_tree,
        ),
        dedup_key=f"publication-shadow:v1:{run_id}",
    )
    _report(outcome, slug)
    # After reporting, so a prune that somehow fails cannot swallow the finding.
    try:
        prune_evidence(
            _evidence_root(config),
            config.publication.shadow_evidence,
            protect=workspace.name,
        )
    except OSError as exc:  # noqa: BLE001 — housekeeping must not break a run
        _warn(f"shadow[{slug}]: could not prune evidence: {exc}")
    return outcome
