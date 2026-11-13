"""The run row a fix-up flow saves when its prompt configuration refuses (#3023).

Review remediation, resolve and CI fix read their role's approved profile
(:func:`kew.service.prompt_profiles.launch_addendum`) before any runner starts.
A row the launch refuses is fixed only by the operator, so the flow records a
``refused`` run under :data:`~kew.prompt_addendum.PROMPT_CONFIGURATION_REFUSAL`
and marks nothing failed, as dispatch does (#3022).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore


@dataclass(frozen=True)
class PromptRefusal:
    """A fix-up launch the operator's prompt profile refused, and why.

    Returned to a driver pass in place of an exit code, so it can hold the node
    on the refusal instead of retrying a launch that refuses identically.
    """

    reason: str


def record_prompt_refusal(
    store: RunStore | None,
    config: DispatchConfig,
    *,
    run_id: str,
    run_kind: str,
    issue_num: int,
    branch: str,
    item_id: str | None,
    runner: str,
) -> None:
    """Save *run_id* as a ``refused`` run no runner started."""
    from kew.run_store import RunRecord

    if store is None:
        return
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch,
            runner_name=runner,
            run_kind=run_kind,
            status="refused",
            exit_code=2,
            started_at=now,
            finished_at=now,
            error_category=PROMPT_CONFIGURATION_REFUSAL,
            repo=config.project.repo or None,
            # A proven pre-launch refusal: publication lineage skips it, so it
            # never becomes the branch's producer and a relaunch after the
            # operator fixes the profile binds to the run that pushed the head.
            pre_admission_refusal=True,
        )
    )
