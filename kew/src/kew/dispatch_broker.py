"""Host-brokered PR path, extracted from ``dispatch_core`` (Task 5).

This module owns the "kew finishes the git ceremony a weaker agent skipped"
sub-surface (#518): the broker commit message, the commit+push+PR broker
(``_broker_agent_work``), and the label+complete step for a brokered PR
(``_finalize_brokered_pr``).

It is a pure seam extraction. Collaborators that live in ``kew.dispatch_core``
— including the ones the dispatch suites ``mock.patch`` at ``kew.dispatch_core.*``
(``check_pr_exists``, ``_commit_all_if_dirty``, ``_get_changed_files``,
``_push_and_open_pr``, ``_auto_unblock_dependents``, ``mark_completed``,
``update_pr_labels``) — are reached as attributes of the ``kew.dispatch_core``
module at call time, mirroring the Wave 1 precedent (``run_store_reservations``):
that preserves the patch targets and breaks the import cycle. The non-patched
formatting helpers (``item_ref``, ``_error``, ``_success``) are imported directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import kew.dispatch_core
from kew.commit_message import build_commit_message
from kew.ids import item_ref
from kew.progress import _error, _success
from kew.publication import PublicationError, select_publication_mode
from kew.publication_credentials import ensure_publication_credentials
from kew.transcript_metrics import agent_report_for

if TYPE_CHECKING:
    from pathlib import Path

    from kew.config import DispatchConfig
    from kew.publication_canonical import CanonicalNoChange
    from kew.run_store import RunStore
    from kew.tracking import DispatchResult


def _broker_commit_message(
    item_id: str,
    config: DispatchConfig,
    *,
    title: str | None = None,
    run_id: str | None = None,
    log_file: Path | str | None = None,
) -> str:
    """Commit message kew uses when it commits work the agent left uncommitted.

    The subject describes the change (#2062) — the agent's own summary line when
    its report carries one, else the issue *title* — and the provenance lives in
    the body. With neither, it keeps the historical generic subject.
    """
    return build_commit_message(
        item_id=item_id,
        prefix=config.commands.commit_prefix.format(issue=item_id),
        title=title,
        run_id=run_id,
        report=agent_report_for(log_file),
    )


def _broker_agent_work(
    worktree_dir: Path,
    branch: str,
    item_id: str,
    title: str,
    config: DispatchConfig,
    *,
    store: RunStore | None = None,
    run_id: str | None = None,
    base_sha: str | None = None,
    log_file: Path | None = None,
    no_change: list[CanonicalNoChange] | None = None,
) -> bool:
    """Broker the git ceremony a (weaker) agent may have skipped (#518).

    Small local models often write correct code but never run
    ``git commit``/``push``/``gh pr create``. If the worktree has uncommitted
    changes, commit them; then, if the branch has any changes ahead of base,
    push and open a PR. Returns True when a PR was brokered (real work existed),
    False when the worktree was genuinely clean (a true no-op).

    ``log_file`` is the run's log: its Dispatch Report is the first choice for
    the commit subject (#2062).
    """
    if store is not None and run_id:
        ensure_publication_credentials(store, run_id=run_id)
    base = config.branches.base or "main"
    mode = select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        store=store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        repo=config.project.repo or None,
    )
    if mode == "canonical":
        pr_url = kew.dispatch_core._push_and_open_pr(
            worktree_dir,
            branch,
            item_id,
            title,
            config,
            store=store,
            run_id=run_id,
            base_sha=base_sha,
            log_file=log_file,
            on_no_change=None if no_change is None else no_change.append,
        )
        return pr_url is not None

    try:
        committed = kew.dispatch_core._commit_all_if_dirty(
            worktree_dir,
            item_id,
            config,
            title=title,
            run_id=run_id,
            log_file=log_file,
        )
    except Exception as exc:
        raise PublicationError("commit", exc) from exc
    # Only a confirmed-clean worktree is a true no-op. If we just committed
    # work, it exists regardless of what a (possibly failing) diff reports, so
    # we never let an empty/failed _get_changed_files discard it.
    if not committed and not kew.dispatch_core._get_changed_files(
        worktree_dir,
        base,
        publish="checkout",
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    ):
        return False
    kew.dispatch_core._push_and_open_pr(
        worktree_dir,
        branch,
        item_id,
        title,
        config,
        store=store,
        run_id=run_id,
        base_sha=base_sha,
        log_file=log_file,
    )
    return True


def _finalize_brokered_pr(
    issue_num: int,
    branch: str,
    config: DispatchConfig,
    *,
    item_id: str,
    complexity: str,
    result: DispatchResult,
    store: RunStore | None = None,
) -> None:
    """Label + complete an issue whose PR kew brokered on the agent's behalf.

    Completes only when the brokered PR is actually found and labeled — mirrors
    the agent-created-PR path. If either prerequisite fails, does nothing (the
    branch is already pushed, so the work is safe on origin for a human).

    ``issue_num`` still drives the GitHub-native label/close ops (int until the
    L3 tracker switch); ``item_id`` names the item in operator-facing messages.
    """
    new_pr = kew.dispatch_core.check_pr_exists(branch, config)
    if not (isinstance(new_pr, int) and new_pr > 0):
        _error(
            f"Brokered PR for {item_ref(item_id)} not found -- not marking completed"
        )
        return
    if not kew.dispatch_core.update_pr_labels(  # type: ignore[attr-defined]
        new_pr, config, add=[config.labels.pr_pending_review]
    ):
        _error(
            f"Failed to label brokered PR for {item_ref(item_id)} "
            "-- not marking completed"
        )
        return
    kew.dispatch_core.mark_completed(issue_num, config)  # type: ignore[attr-defined]
    kew.dispatch_core._auto_unblock_dependents(issue_num, config)
    if not kew.dispatch_core._post_agent_report(
        str(new_pr), result.log_file, config, result=result
    ):
        selected_model: str | None = None
        try:
            record = store.get(result.run_id) if store is not None else None
            selected_model = record.model if record is not None else None
        except Exception:  # noqa: BLE001  # nosec B110 — reporting must stay fail-soft
            pass
        kew.dispatch_core._post_dispatch_report_fallback(
            new_pr,
            item_id,
            complexity,
            result,
            config,
            selected_model=selected_model,
        )
    _success(
        f"Completed {item_ref(item_id)} -- PR brokered by kew "
        "(agent skipped commit/push)."
    )
