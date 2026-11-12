"""A reviewer launcher double that finishes its run, then raises.

The production launcher's post-run shape, with its bookkeeping failing:
``_launch`` guards only the container in its ``except``; ``_finalize_review_run``
and ``remove_prompt_handoff`` run in the ``finally`` OUTSIDE that guard, and
``collect_stream_verdict`` after it. So a reviewer that started, streamed and
exited 0 can still raise out of ``dispatch_reviewer`` — this double admits and
finalizes the run row exactly as that reviewer would have, then raises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kew.config import DispatchConfig
from kew.ids import branch_slug
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord

FINISHED_AT = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


class FinishedThenRaisesLauncher:
    """Save a finished reviewer run row for the launch, then raise.

    ``bind_store`` mirrors the production seam so the worker thread's own handle
    is the one written (SQLite handles are thread-affine).
    """

    def __init__(self, store: Any, config: DispatchConfig) -> None:
        self._store = store
        self._config = config
        self.contexts: list[ReviewerLaunchContext] = []

    def bind_store(self, store: Any) -> FinishedThenRaisesLauncher:
        bound = FinishedThenRaisesLauncher(store, self._config)
        bound.contexts = self.contexts
        return bound

    def __call__(self, ctx: ReviewerLaunchContext) -> CollectedSidecar:
        self.contexts.append(ctx)
        self._store.save(
            RunRecord(
                run_id=ctx.run_id,
                issue_num=int(ctx.item_id),
                item_id=ctx.item_id,
                branch=f"{self._config.branches.prefix}{branch_slug(ctx.item_id)}",
                status="success",
                exit_code=0,
                started_at=FINISHED_AT,
                finished_at=FINISHED_AT,
                estimated_cost_usd=1.25,
                total_tokens=4242,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                model="claude-opus-4",
                runner_name="claude-code",
            )
        )
        raise RuntimeError("prompt handoff removal failed after the reviewer exited 0")
