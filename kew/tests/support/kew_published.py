"""A branch head kew published, as the ledger records it.

The implementation run on the branch, its contained launch profile, and the
head attestation binding the commit to that run: what a governed review
(``kew review --governed``) checks before any reviewer runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kew.audit import Actor
from kew.publication_attest import record_head_attestation
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore


def published_by_kew(
    store: SqliteRunStore, *, item: str, branch: str, head: str
) -> None:
    """Record *head* on *branch* as published by a contained kew run for *item*."""
    store.save(
        RunRecord(
            run_id="producer",
            issue_num=int(item),
            item_id=item,
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 10, 9, 1, tzinfo=UTC),
            finished_at=datetime(2026, 10, 9, 2, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-{item}",
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id="producer",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="kew-agent:latest",
                image_id="sha256:" + "a" * 64,
                auth_path="env:SYNTHETIC_PROVIDER_KEY",
            ),
        )
    )
    record_head_attestation(
        store,
        run_id="producer",
        item_id=item,
        branch=branch,
        commit_sha=head,
        parent_shas=("b" * 40,),
        actor=Actor("human", "owner"),
    )
