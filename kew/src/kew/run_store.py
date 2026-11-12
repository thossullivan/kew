"""Run history storage: RunRecord, RunStore protocol, and SqliteRunStore."""

import contextlib
import csv
import io
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, TypeVar, runtime_checkable

from kew import stop_recovery
from kew.artifact_receipt import ArtifactLineage as ArtifactLineage
from kew.artifact_receipt import ArtifactReceipt as ArtifactReceipt
from kew.artifact_receipt import ArtifactReceiptStoreMixin
from kew.artifact_receipt import ReceiptConflictError as ReceiptConflictError
from kew.artifact_receipt import ReceiptError as ReceiptError
from kew.artifact_receipt import ReceiptMissingError as ReceiptMissingError
from kew.artifact_receipt import ReceiptSealedError as ReceiptSealedError
from kew.artifact_receipt import ReceiptValueError as ReceiptValueError
from kew.artifact_receipt import ReceiptVerification as ReceiptVerification
from kew.audit_chain import GENESIS_PREV, entry_hash
from kew.audit_identity import continuation_evidence_identity
from kew.cancellation_journal import CancellationJournal, StartupState
from kew.driver_cadence import cadence_lapsed
from kew.ids import GITHUB_ID_RE, MAX_ISSUE_NUM_DIGITS
from kew.no_change import NoChangeEvidence as NoChangeEvidence
from kew.no_change import NoChangeIntent as NoChangeIntent
from kew.no_change import NoChangeJudgment as NoChangeJudgment
from kew.process_groups import (
    attempt_descends_from,
    attempts_are_dead,
    birth_matches,
    group_absent,
    host_stop_is_dead,
    owner_alive,
    process_start_time,
    process_start_token,
)
from kew.publication_job import LegacyPublicationView as LegacyPublicationView
from kew.publication_job import PublicationIntent as PublicationIntent
from kew.publication_job import PublicationJob as PublicationJob
from kew.publication_job import (
    PublicationJobConflictError as PublicationJobConflictError,
)
from kew.publication_job import PublicationJobError as PublicationJobError
from kew.publication_job import (
    PublicationJobsInactiveError as PublicationJobsInactiveError,
)
from kew.publication_job import PublicationJobStateError as PublicationJobStateError
from kew.publication_job import PublicationJobValueError as PublicationJobValueError
from kew.publication_job import RemoteObservation as RemoteObservation
from kew.run_store_acceptance import AcceptanceContractRow as AcceptanceContractRow
from kew.run_store_acceptance import AcceptanceContractStoreMixin
from kew.run_store_artifacts import ArtifactConflictError as ArtifactConflictError
from kew.run_store_artifacts import ArtifactStoreError as ArtifactStoreError
from kew.run_store_artifacts import ArtifactStoreMixin
from kew.run_store_artifacts import ArtifactValueError as ArtifactValueError
from kew.run_store_artifacts import StoredArtifact as StoredArtifact

# Re-exported from run_store_audit (Task 1 extraction) so callers importing
# these names from run_store keep working unchanged (e.g. backup.py imports
# _row_to_chain_row and _CHAIN_SELECT_ALL). The `as`-alias form marks them as
# explicit re-exports for ruff.
from kew.run_store_audit import _CHAIN_SELECT_ALL as _CHAIN_SELECT_ALL
from kew.run_store_audit import _CHAIN_SELECT_TAIL as _CHAIN_SELECT_TAIL
from kew.run_store_audit import AuditChainStoreMixin, audit_values
from kew.run_store_audit import _normalize_event_ts as _normalize_event_ts
from kew.run_store_audit import _row_to_chain_row as _row_to_chain_row
from kew.run_store_committed import CommittedReadStoreMixin, RunReadCursor
from kew.run_store_item_plans import ItemPlanStoreMixin
from kew.run_store_launches import RunLaunchStoreMixin
from kew.run_store_liveness import (
    DeadPending,
    NeverStartedReap,
    count_admission_runs,
    prepare_admission,
    reap_dead_never_started,
    recorded_daemon,
    reservation_generation,
    stage_shadowed,
    stopped_container_remover,
)
from kew.run_store_merge_claims import MergeClaim as MergeClaim
from kew.run_store_merge_claims import MergeClaimStoreMixin
from kew.run_store_merge_queue import MergeQueueEntry as MergeQueueEntry
from kew.run_store_merge_queue import MergeQueueStoreMixin
from kew.run_store_merge_queue import MergeQueueSuspect as MergeQueueSuspect
from kew.run_store_no_change import NoChangeStoreMixin
from kew.run_store_outcomes import MAX_OUTCOME_WEEKS as MAX_OUTCOME_WEEKS
from kew.run_store_outcomes import MergedPrWeek as MergedPrWeek
from kew.run_store_outcomes import OutcomeStoreMixin
from kew.run_store_outcomes import ReadyItemWeek as ReadyItemWeek
from kew.run_store_outcomes import SpendWeek as SpendWeek
from kew.run_store_prompt_profiles import PromptProfileStoreMixin, PromptProfileVersion
from kew.run_store_publication_jobs import (
    HumanReviewStoreMixin,
    PublicationJobStoreMixin,
)
from kew.run_store_readiness import ProbeLeaseHeldError as ProbeLeaseHeldError
from kew.run_store_readiness import ProbeLookupResult as ProbeLookupResult
from kew.run_store_readiness import ProbeState as ProbeState
from kew.run_store_readiness import ProbeStateError as ProbeStateError
from kew.run_store_readiness import ProbeVerdict as ProbeVerdict
from kew.run_store_readiness import ReadinessProbeError as ReadinessProbeError
from kew.run_store_readiness import ReadinessProbeStoreMixin, RouteProfileStoreMixin
from kew.run_store_readiness import RecordableProbeCost as RecordableProbeCost
from kew.run_store_readiness import RecordableRouteProfile as RecordableRouteProfile
from kew.run_store_readiness import RouteObservation as RouteObservation
from kew.run_store_readiness import StoredReadinessProbe as StoredReadinessProbe
from kew.run_store_readiness import StoredRouteProfile as StoredRouteProfile
from kew.run_store_rename import RepoRenameStoreMixin
from kew.run_store_reservations import EpicReservationStoreMixin
from kew.run_store_review_rounds import ReviewRoundStoreMixin
from kew.run_store_runs import RunRecordStoreMixin
from kew.run_store_stop import StopLabelStoreMixin
from kew.run_store_triage import TriageDecisionStoreMixin
from kew.run_store_work import AdmissionIdentity as AdmissionIdentity
from kew.run_store_work import (
    AdmissionIdentityConflictError as AdmissionIdentityConflictError,
)
from kew.run_store_work import AdmissionIdentityError as AdmissionIdentityError
from kew.run_store_work import AdmissionIdentityStoreMixin
from kew.run_store_work import ReadSnapshotWriteError as ReadSnapshotWriteError
from kew.stop_recovery import LabelReset
from kew.switchyard import sanitize_endpoint

CURRENT_SCHEMA_VERSION = 77
"""Highest schema version this build understands. Bump in lockstep with each
new ``_apply_vN`` migration step (a test asserts they stay equal)."""

MAX_RUN_PAGE = 200
"""Largest page ``list_run_page`` will return. A poll that asks for more is a
bug in the caller, not a request to read the whole ledger."""

# Governed adversarial-review run kinds (F4/#977). A reviewer runs under
# ``adversarial_review``; the driver-invoked remediation pass under
# ``review_remediation``. Both are bucketed away from implementation spend
# (run_kind bucketing precedent #445) AND filtered out of node-status
# derivation: a pending reviewer row must not flip a node IN_PROGRESS and a
# killed reviewer must not derive FAILED (the review crash table owns those
# outcomes, not ``overlay_status``).
ADVERSARIAL_REVIEW_RUN_KIND = "adversarial_review"
REVIEW_REMEDIATION_RUN_KIND = "review_remediation"
REVIEW_RUN_KINDS = frozenset({ADVERSARIAL_REVIEW_RUN_KIND, REVIEW_REMEDIATION_RUN_KIND})
# The detached review stage's own row (#2459): a ``kew epic _review-node``
# child that takes one node's review action (a reviewer round, a remediation or
# the merge-main step) outside the tick. It carries the node's review
# reservation and its owner, so the reconciler can prove a dead child dead; the
# reviewer and remediation it starts keep their own kinds and rows. Its subject
# is the open PR, so it sits with the PR-subject kinds and never derives status.
REVIEW_STAGE_RUN_KIND = "review_stage"
# CI fixes track existing PR work.
# They never consume governed review pools or reservations.
CI_FIX_RUN_KIND = "ci_fix"
RESOLVE_RUN_KIND = "resolve"
# Runs whose subject is an already-open PR. They never opened one, so the
# PR->item join (``find_run_by_branch``) looks past them and the duplicate-PR
# retry guard (``dispatch_core._check_existing_pr``) does not apply to them.
PR_SUBJECT_RUN_KINDS = frozenset(
    {
        "review",
        CI_FIX_RUN_KIND,
        RESOLVE_RUN_KIND,
        REVIEW_STAGE_RUN_KIND,
        *REVIEW_RUN_KINDS,
    }
)

# The one operational run kind a readiness probe pays on (I05A). A probe row is
# real money — it belongs in total and budget spend — but it is not customer
# work: it carries no tracker item, no epic, no PR, and no complexity, so it is
# excluded from per-item/epic economics here and from the outcome, completion,
# and scorecard cohorts in kew.report / kew.scorecard. The canonical definition
# is kew.readiness.READINESS_PROBE_RUN_KIND; this module sits BELOW kew.runners
# (which kew.readiness imports) and so restates the literal rather than
# importing it. tests/test_run_store.py guards the two against drift.
READINESS_PROBE_RUN_KIND = "readiness_probe"

# The triage station's own run kind (#2293). A triage run classifies an item;
# it publishes nothing (``publication_credentials._NON_PUBLICATION_RUN_KINDS``)
# and it is filtered out of node-status derivation
# (``graph.driver.overlay._implementation_runs``) for the same reason a reviewer
# is: a classifier reading an item must never read as the item being worked on.
TRIAGE_RUN_KIND = "triage"

# The acceptance author's run kind (#2726): it writes a node's read-only
# acceptance test file before the implementer starts. It writes no
# implementation, so the scorecard never credits it with the PR the node ships
# (``scorecard._NON_AUTHORING_RUN_KINDS``) even though it runs first; the
# ledger records what it wrote in ``acceptance_contracts``.
ACCEPTANCE_AUTHOR_RUN_KIND = "acceptance_author"

#: Every run kind this build launches. The constants above and the role sets
#: (``TRACKER_WORK_RUN_KINDS``, ``PR_SUBJECT_RUN_KINDS``, ``REVIEW_RUN_KINDS``)
#: each answer "what is this kind FOR"; this answers "is this a kind at all",
#: which is what config has to check a key *naming* a run kind against. A
#: misspelled ``[sandbox] hosts`` key would otherwise pass load-time validation
#: and silently resolve to the default host (#2437 review).
#: ``tests/test_run_store.py`` guards it against the role sets it must cover.
KNOWN_RUN_KINDS = frozenset(
    {
        "dispatch",
        "loop",
        "delegation",
        "review",
        "planning",
        ADVERSARIAL_REVIEW_RUN_KIND,
        REVIEW_REMEDIATION_RUN_KIND,
        REVIEW_STAGE_RUN_KIND,
        CI_FIX_RUN_KIND,
        RESOLVE_RUN_KIND,
        READINESS_PROBE_RUN_KIND,
        TRIAGE_RUN_KIND,
        ACCEPTANCE_AUTHOR_RUN_KIND,
    }
)

#: The host a run lands on when nothing places it elsewhere: the operator
#: machine. Every pre-v66 ``runs`` row reads as this, and so does every run
#: under the default ``[sandbox] placement`` — ``local`` is the one host that
#: needs no cloud account, and it stays the default (#2398).
LOCAL_RUN_HOST = "local"

#: The gcp host: a Compute Engine instance reached through the ``gcloud`` CLI
#: (:mod:`kew.hosts.gcp`). Unlike ``local`` it is a *pool* of machines, so a
#: run placed here also records WHICH instance in ``runs.run_host_instance``.
GCP_RUN_HOST = "gcp"

#: Every host a run may be placed on. The names live here, at the bottom of the
#: stack, because the ledger is what records a placement and config is what
#: refuses an unknown one — two readings of one list, so one list.
KNOWN_RUN_HOSTS = frozenset({LOCAL_RUN_HOST, GCP_RUN_HOST})

#: The outcomes a ``triage_decisions`` row may carry. Restated here rather than
#: imported from ``kew.triage.policy`` because this module sits below it; the
#: two are held together by a test (tests/test_run_store_triage.py).
TRIAGE_OUTCOMES = frozenset(
    {
        "propose_dispatch",
        "ask",
        "duplicate_candidate",
        "human_decision",
        "split",
        "unavailable",
    }
)

#: How long an epic keeps contending for fleet worker slots after its last tick
#: (#2094). Long enough that an epic driven on any sane interval (``kew epic
#: run`` defaults to 60s) never ages out mid-flight, short enough that a driver
#: stopped yesterday stops dividing today's fleet cap.
CONTENTION_STALE_AFTER = timedelta(days=1)


_MAX_CONTINUATION_GENERATIONS = 32
_CONTINUATION_FAMILY_FIELDS = (
    "epic_ref",
    "item_id",
    "original_run_id",
    "branch",
    "worktree_identity",
)
_CONTINUATION_TOKEN_FIELDS = (
    *_CONTINUATION_FAMILY_FIELDS,
    "continuation_run_id",
    "evidence_identity",
)


def _continuation_claim_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Validate one retained claim using only hash-chained row fields."""
    payload = _decode_continuation_claim(row["payload"])
    _require_continuation_claim_identity(payload, row_run_id=row["run_id"])
    _require_continuation_claim_generation(payload)
    expected_identity = continuation_evidence_identity("continuation_claimed", payload)
    if payload["evidence_identity"] != expected_identity:
        raise RuntimeError("continuation claim identity is inconsistent")
    return payload


def _decode_continuation_claim(raw_payload: Any) -> dict[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("continuation claim payload is unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("continuation claim payload is unavailable")
    return payload


def _require_continuation_claim_identity(
    payload: dict[str, Any], *, row_run_id: str | None
) -> None:
    if (
        payload.get("recovery") != "preserved_work_continuation"
        or payload.get("recovery_phase") != "claim"
        or any(
            not isinstance(payload.get(field), str) or not payload[field]
            for field in _CONTINUATION_TOKEN_FIELDS
        )
        or row_run_id != payload["continuation_run_id"]
    ):
        raise RuntimeError("continuation claim identity is incomplete")


def _require_continuation_claim_generation(payload: dict[str, Any]) -> None:
    generation = payload.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise RuntimeError("continuation claim generation is invalid")


def _continuation_claims_by_generation(
    claims: list[dict[str, Any]],
    releases: list[dict[str, Any]],
    *,
    highest: int | None = None,
    launched: Callable[[str], bool],
) -> dict[int, dict[str, Any] | None]:
    """Require claim or release evidence for every consumed generation.

    A release can fill a claim-less generation. It cannot erase a launched
    run's evidence. Parent selection skips only runs that never launched.
    An entirely unevidenced generation requires operator abandonment.
    """
    by_generation: dict[int, dict[str, Any] | None] = {}
    for retained_claim in claims:
        generation = int(retained_claim["generation"])
        if generation in by_generation:
            raise ValueError("continuation generation has conflicting claims")
        by_generation[generation] = retained_claim
    for release in releases:
        _include_continuation_release(by_generation, release, launched=launched)
    if highest is None:
        highest = max(by_generation, default=0)
    missing = set(range(1, highest + 1)) - by_generation.keys()
    if missing:
        raise RuntimeError(
            f"continuation generation {min(missing)} has no claim or release "
            "evidence; a released generation is transparent only without a run "
            "row or with an initial pending row, and without execution evidence "
            "(finish, process, container, session, duration, usage, cost, route "
            "profile, dispatch_started, or publication_intent); "
            "use `kew epic abandon` to recover"
        )
    return by_generation


def _include_continuation_release(
    by_generation: dict[int, dict[str, Any] | None],
    release: dict[str, Any],
    *,
    launched: Callable[[str], bool],
) -> None:
    """Fill a released gap without obscuring a conflicting or launched claim."""
    generation = int(release["generation"])
    claim = by_generation.get(generation)
    if (
        claim is not None
        and release.get("outcome") != "claim_undone"
        and claim["continuation_run_id"] != release["continuation_run_id"]
    ):
        raise ValueError("continuation release conflicts with its claim")
    if (
        claim is None or claim["continuation_run_id"] != release["continuation_run_id"]
    ) and launched(release["continuation_run_id"]):
        raise RuntimeError(
            "released continuation generation lacks claim evidence for a "
            "launched run; only absent or initial pending runs without "
            "execution evidence are transparent; use `kew epic abandon` to recover"
        )
    by_generation.setdefault(generation, None)


def _continuation_parent(
    target: dict[str, Any],
    by_generation: dict[int, dict[str, Any] | None],
    *,
    run_id: str,
    launched: Callable[[str], bool],
) -> str:
    target_generation = int(target["generation"])
    recorded = by_generation.get(target_generation)
    if recorded is None or recorded["continuation_run_id"] != run_id:
        raise ValueError("continuation run identity conflicts with its generation")
    for generation in range(target_generation - 1, 0, -1):
        claim = by_generation[generation]
        if claim is not None and launched(claim["continuation_run_id"]):
            return str(claim["continuation_run_id"])
    return str(target["original_run_id"])


def _continuation_release_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Validate immutable release evidence without trusting mutable owners."""
    payload = _decode_continuation_claim(row["payload"])
    _require_continuation_claim_generation(payload)
    expected = continuation_evidence_identity("continuation_released", payload)
    if (
        payload.get("recovery_phase") != "release"
        or expected is None
        or payload.get("evidence_identity") != expected
        or not payload.get("reason")
        or row["run_id"] != payload.get("continuation_run_id")
        or any(not payload.get(field) for field in _CONTINUATION_FAMILY_FIELDS)
    ):
        raise RuntimeError(
            "continuation release evidence is incomplete; a released generation "
            "is transparent only with its identity and reason; "
            "use `kew epic abandon` to recover"
        )
    return payload


def _cancelled_claim_matches(
    release: dict[str, Any], claims: list[dict[str, Any]]
) -> bool:
    """Match cancellation evidence to its claim, including the restored generation."""
    if release.get("outcome") != "claim_undone":
        return False
    if release.get("reason") == "claim_evidence_not_recorded":
        # Round-five evidence predates restored_generation and has no claim row.
        return not claims
    return (
        str(release.get("reason", "")).startswith("dispatch_prelaunch_failure: ")
        and release.get("state") == "awaiting_continuation"
        and type(release.get("restored_generation")) is int
        and release["restored_generation"] == release["generation"] - 1
        and len(claims) == 1
        and claims[0]["generation"] == release["generation"]
    )


def _require_complete_owner_history(
    claims: list[dict[str, Any]],
    releases: list[dict[str, Any]],
    generation: int,
) -> None:
    """Abandonment requires a claim for every consumed checkout generation."""
    by_generation = {claim["generation"]: claim for claim in claims}
    for release in releases:
        claim = by_generation.get(release["generation"])
        if claim is None:
            raise RuntimeError(
                f"continuation generation {release['generation']} has release "
                "evidence but no claim evidence"
            )
        if (
            release.get("outcome") != "claim_undone"
            and release["continuation_run_id"] != claim["continuation_run_id"]
        ):
            raise RuntimeError("continuation release conflicts with its claim")
    expected = set(range(1, generation + 1))
    if len(claims) != generation or set(by_generation) != expected:
        missing = expected - by_generation.keys()
        gap = f"generation {min(missing)} has no claim; " if missing else ""
        raise RuntimeError(
            f"continuation {gap}claim count {len(claims)} does not match "
            f"{generation} generations or their identities"
        )


def _retained_recovery_identity(
    run: sqlite3.Row | None,
) -> tuple[str, str] | None:
    """Return the nonempty item and branch used for retained recovery lookup."""
    if run is None:
        return None
    item_id = run["item_id"]
    branch = run["branch"]
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(branch, str) or not branch:
        return None
    return item_id, branch


def _retained_generation_exceeds_bound(row: sqlite3.Row) -> bool:
    generation = row["generation"]
    if not isinstance(generation, int):
        return True
    return generation > _MAX_CONTINUATION_GENERATIONS


def _require_retained_recovery_bound(family_rows: list[sqlite3.Row]) -> None:
    if len(family_rows) > _MAX_CONTINUATION_GENERATIONS or any(
        _retained_generation_exceeds_bound(row) for row in family_rows
    ):
        raise OverflowError("continuation recovery history exceeds policy bound")


def _retained_family_names_other_original(
    family_rows: list[sqlite3.Row], run_id: str
) -> bool:
    return any(row["original_run_id"] != run_id for row in family_rows)


class SchemaTooNewError(RuntimeError):
    """The dispatch.db was written by a newer kew than this build understands."""


_T = TypeVar("_T")

_logger = logging.getLogger(__name__)

_BUSY_MAX_RETRIES = 5
_BUSY_BASE_DELAY = 0.05  # 50ms

#: Page size for :meth:`SqliteRunStore.latest_node_pr_facts`, and the ceiling a
#: caller may not raise past: the read fans one row out per linked PR across
#: every epic's newest snapshot, so an unbounded page is one request that scales
#: with the whole journal.
DEFAULT_NODE_PR_FACTS_LIMIT = 200
MAX_NODE_PR_FACTS_LIMIT = 1000

#: Driver statuses a node's head can no longer go stale under: the work landed,
#: or it was cancelled. Kept as plain strings because ``kew.run_store`` may not
#: import ``kew.graph`` (import-linter contract); ``test_run_store_epic.py``
#: pins them against ``DriverStatus``.
SETTLED_NODE_STATUSES = ("done", "cancelled")


#: :meth:`SqliteRunStore.latest_node_pr_facts`, as literal SQL fragments.
#:
#: Written out rather than composed from an f-string so the statement carries no
#: interpolation at all: the only variable parts are bind parameters, and the
#: ``NOT IN`` placeholder count is fixed by :data:`SETTLED_NODE_STATUSES` (which
#: ``test_run_store_epic.py`` pins against both the statuses and this arity).
_NODE_PR_FACTS_SELECT = (
    "SELECT t.epic_ref AS epic_ref, t.tick_ts AS tick_ts,"
    " json_extract(n.value, '$.id') AS item_id,"
    " json_extract(n.value, '$.status') AS status,"
    " json_extract(n.value, '$.pr_url') AS pr_url,"
    " json_extract(n.value, '$.head_oid') AS head_oid,"
    " json_extract(n.value, '$.merge_state') AS merge_state,"
    " json_extract(n.value, '$.merge_state_since') AS merge_state_since,"
    " CASE WHEN r.merge_state IS json_extract(n.value, '$.merge_state')"
    " AND r.head_oid IS json_extract(n.value, '$.head_oid')"
    " AND julianday(r.merge_state_at)"
    " > COALESCE(julianday(json_extract(n.value, '$.merge_state_at')), 0)"
    " THEN r.merge_state_at"
    " ELSE json_extract(n.value, '$.merge_state_at') END AS merge_state_at,"
    " json_extract(n.value, '$.pr_pushed_at') AS pr_pushed_at"
    " FROM (SELECT epic_ref, tick_ts, snapshot FROM epic_ticks"
    " WHERE id IN (SELECT MAX(id) FROM epic_ticks GROUP BY epic_ref)"
    " AND json_valid(snapshot)) t,"
    " json_each(t.snapshot, '$.nodes') n"
    " LEFT JOIN epic_merge_state_reads r ON r.epic_ref = t.epic_ref"
    " AND r.node_id = json_extract(n.value, '$.id')"
    " WHERE "
)
_NODE_PR_FACTS_LINKED = "json_extract(n.value, '$.pr_url') IS NOT NULL"
_NODE_PR_FACTS_OPEN = "COALESCE(json_extract(n.value, '$.status'), '') NOT IN (?,?)"
_NODE_PR_FACTS_AFTER = (
    "(t.epic_ref > ? OR (t.epic_ref = ? AND json_extract(n.value, '$.id') > ?))"
)
#: One epic's rows only. A surface drawing ONE epic must be able to ask for it:
#: without this, a page bounded by ``limit`` over every epic's newest snapshot
#: can fill up before reaching the epic being looked at, and the caller cannot
#: tell "nothing is stale here" from "this epic was past the page" (#2303).
_NODE_PR_FACTS_EPIC = "t.epic_ref = ?"
_NODE_PR_FACTS_PAGE = " ORDER BY epic_ref, item_id LIMIT ?"

#: :meth:`SqliteRunStore.append_epic_tick`'s write of each node's latest
#: merge-state read (#2879). A re-read of the same state on the same head only
#: ever moves the instant forward: a tick GitHub answered ``UNKNOWN`` on carries
#: the journaled row's older stamp, and must not undo a later read.
_MERGE_STATE_READ_UPSERT = (
    "INSERT INTO epic_merge_state_reads"
    " (epic_ref, node_id, head_oid, merge_state, merge_state_at)"
    " VALUES (?, ?, ?, ?, ?)"
    " ON CONFLICT (epic_ref, node_id) DO UPDATE SET"
    " head_oid = excluded.head_oid, merge_state = excluded.merge_state,"
    " merge_state_at = excluded.merge_state_at"
    " WHERE epic_merge_state_reads.head_oid IS NOT excluded.head_oid"
    " OR epic_merge_state_reads.merge_state IS NOT excluded.merge_state"
    " OR COALESCE(julianday(excluded.merge_state_at), 0)"
    " > COALESCE(julianday(epic_merge_state_reads.merge_state_at), 0)"
)


def _journal_row_matches(
    row: sqlite3.Row,
    snapshot_hash: str,
    rehash: Callable[[dict[str, Any]], str] | None,
) -> bool:
    """Whether the epic's latest journal row records the state *snapshot_hash*
    names, by its stored hash or, under *rehash*, by its snapshot re-hashed."""
    if row["snapshot_hash"] == snapshot_hash:
        return True
    if rehash is None:
        return False
    try:
        stored = json.loads(row["snapshot"])
        return isinstance(stored, dict) and rehash(stored) == snapshot_hash
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # Not a snapshot this build can hash (a missing field, or a node that
        # is not an object): no match, so the tick appends. Raising here would
        # roll the tick back and every later tick would meet the same row.
        _logger.warning(
            "epic_ticks row %s: stored snapshot cannot be re-hashed (%s);"
            " journaling this tick as a change",
            row["id"],
            type(exc).__name__,
        )
        return False


def _merge_state_reads(
    epic_ref: str, snapshot: dict[str, Any]
) -> list[tuple[str, str, str | None, str, str]]:
    """One :data:`_MERGE_STATE_READ_UPSERT` row per node carrying a reading."""
    nodes = snapshot.get("nodes")
    rows: list[tuple[str, str, str | None, str, str]] = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        node_id = _opt_str(node.get("id"))
        state = _opt_str(node.get("merge_state"))
        read_at = _opt_str(node.get("merge_state_at"))
        if node_id and state and read_at:
            rows.append(
                (epic_ref, node_id, _opt_str(node.get("head_oid")), state, read_at)
            )
    return rows


def _opt_str(value: Any) -> str | None:
    """A non-empty string, or None — the shape every absent snapshot fact takes."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class _PrecommitCheckError(BaseException):
    """Keep callback failures outside SQLite's busy-error retry channel."""

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(str(original))


class _PrecommitRollbackError(RuntimeError):
    """Report that rollback could not confirm a precommit refusal."""

    def __init__(self, original: BaseException, rollback_error: BaseException) -> None:
        self.original = original
        self.rollback_error = rollback_error
        super().__init__(
            "precommit failure rollback failed: "
            f"{type(rollback_error).__name__}: {rollback_error}"
        )


def _invoke_precommit_check(check: Callable[[], None] | None) -> None:
    """Invoke the caller's final checkout-generation validation.

    The caller retains its checkout claim and original generation expectation.
    The callback must not acquire claims or call store methods. It must not emit
    events, sleep, or perform Git, provider, network, or other external effects.
    """
    if check is None:
        return
    try:
        check()
    except BaseException as exc:
        raise _PrecommitCheckError(exc) from exc


def _rollback_after_failure(
    connection: sqlite3.Connection, error: BaseException
) -> None:
    """Rollback or expose the uncertain transaction state with its root cause."""
    try:
        connection.rollback()
    except BaseException as rollback_error:
        original = error.original if isinstance(error, _PrecommitCheckError) else error
        raise _PrecommitRollbackError(original, rollback_error) from original


def _rollback_for_outcome(connection: sqlite3.Connection) -> None:
    """Rollback a normal lost-ownership outcome, exposing rollback failure."""
    try:
        connection.rollback()
    except BaseException as rollback_error:
        raise _PrecommitRollbackError(
            RuntimeError("transaction outcome required rollback"), rollback_error
        ) from rollback_error


def _raise_precommit_error(error: _PrecommitCheckError) -> NoReturn:
    """Restore the callback's original exception type after busy retry exits."""
    raise error.original.with_traceback(error.original.__traceback__) from None


RunStatus = Literal[
    "pending",
    "awaiting_publication",
    "success",
    "failure",
    "refused",
    "budget_exceeded",
    "budget_stopped",
    "budget_stop",
    "killed",
]
# Outcome of an atomic reserve-under-cap (N10): the item was reserved, the
# reservation was lost to another surface (already reserved), or the fleet cap
# was already full at reserve time — all decided inside one transaction.
ReserveResult = Literal["reserved", "lost_claim", "fleet_full"]
ContinuationState = Literal["awaiting_continuation", "continuing", "terminal"]
ContinuationClaimResult = Literal[
    "claimed", "fleet_full", "lost_claim", "not_found", "already_claimed", "terminal"
]
LoopStatus = Literal[
    "pursuing", "achieved", "unmet", "budget_limited", "needs_human", "paused"
]

_FLEET_STOPPED_KEY = "fleet_stopped"


@dataclass
class RunRecord:
    """Persistent record of a single dispatch run."""

    run_id: str
    issue_num: int
    branch: str
    status: RunStatus
    exit_code: int
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    estimated_cost_usd: float = 0.0
    error_category: str = ""
    attempt_number: int = 1
    session_id: str | None = None
    log_file: Path | None = None
    pr_url: str | None = None
    runner_name: str = ""
    model: str = ""  # which model was used (from routing)
    title: str = ""  # issue title
    complexity: str = ""  # issue complexity label (simple/medium/complex)
    pgid: int | None = None  # owning dispatch group; stable across retry attempts
    attempt_pgid: int | None = None  # latest attempt group; termination only
    owner_pid: int | None = None  # exclusive owner; NULL means legacy/unknown
    attempt_start_time: str | None = None
    owner_start_time: str | None = None
    owner_start_token: str | None = None
    attempt_start_token: str | None = None
    # V69: ``<machine>/<boot id>`` the owner ran under (#2812); see
    # ``process_groups.machine_boot``. NULL means unrecorded, which proves nothing.
    owner_boot_id: str | None = None
    owner_ppid: int | None = None
    owner_command: str | None = None
    container_runtime: str | None = None
    cleanup_runtime: str | None = None
    attempt_owner_verified: bool = False
    # V54: trusted pre-termination snapshots, retained after leader exit.
    termination_members: dict[int, dict[int, str]] = field(default_factory=dict)
    # V8 audit envelope (all nullable; pre-V8 rows read as None)
    actor_type: str | None = None  # human | policy | agent
    actor_id: str | None = None
    repo: str | None = None
    endpoint: str | None = None  # configured, not attested (see #251)
    policy_hash: str | None = None  # null until #250
    # Served model reported by the runner or reconciled from bounded
    # Switchyard selection evidence; ``resolved_model_source`` records which.
    reported_model: str | None = None
    # V40 adaptive Switchyard accounting: the route alias remains in ``model``;
    # these fields carry the separately evidenced served identity and tier.
    resolved_model_source: str | None = None
    selected_tier: str | None = None
    requested_service_tier: str | None = None
    reported_service_tier: str | None = None
    classifier_overhead_model: str | None = None
    classifier_overhead_input_tokens: int = 0
    classifier_overhead_output_tokens: int = 0
    classifier_overhead_cost_usd: float = 0.0
    switchyard_proxy_observation: str | None = None
    # V41 managed adaptive evidence reconciliation verdict.
    accounting_status: str | None = None
    # V8 outcome slots for #261 — schema-only, nothing writes them here
    pr_state: str | None = None
    pr_merged_at: str | None = None
    review_cycles: int | None = None
    diff_additions: int | None = None
    diff_deletions: int | None = None
    diff_files: int | None = None
    # V16 cache observability (#348)
    prefix_key: str | None = None
    provider: str | None = None
    # V22 sandbox container name (e.g. "kew-42"); None for host-subprocess runs
    container_name: str | None = None
    container_id: str | None = None
    container_cidfile: str | None = None
    # V24 durable work-item id (A3). For GitHub the numeric string ("42"); for
    # non-numeric trackers the opaque id ("SFD-12"). This is the fleet-wide
    # concurrency key (see ``live_run_count``) — NOT ``issue_num``, which for a
    # non-numeric item is written as the ``0`` sentinel (``runs.issue_num`` is
    # NOT NULL and SQLite cannot drop that; ``0`` is never a valid GitHub
    # number). ``None`` on write is backfilled so every row carries a distinct
    # fleet key: GitHub rows from ``issue_num``, and ``0``-sentinel rows (no
    # work item — e.g. an ``epic plan`` run) from the unique ``run_id`` rather
    # than the literal ``"0"`` (see ``_run_record_params``).
    item_id: str | None = None
    # V26 run-kind discriminator (I1). Separates the cost-anomaly baseline
    # bucket by dispatch path: "dispatch" (ordinary issue), "review" (PR
    # review, routinely ~$5.37), "loop", "planning". Existing rows migrate to
    # "dispatch" so heavyweight review runs stop being judged against cheap
    # first-dispatch medians (#445). Never NULL: the column is
    # NOT NULL DEFAULT 'dispatch'.
    run_kind: str = "dispatch"
    # V30 first-edit metrics (spec D2 — context-memory measurement substrate).
    # Derived from the stream-json transcript: how many model-response steps and
    # how many cumulative tokens the run spent before its first Edit/Write
    # tool_use. Both NULL (never 0) for a zero-edit run — a NULL distinguishes
    # "measured, never edited" from "edited on step 0". See transcript_metrics.
    steps_to_first_edit: int | None = None
    tokens_to_first_edit: int | None = None
    # V30 injected-context accounting (spec D2 / R1-8 — governor cost-model
    # inputs). ``map_tokens`` is the repo map actually rendered into the prompt;
    # ``lesson_corpus_tokens`` is a placeholder that stays NULL until Phase 2
    # ships the lesson-corpus layer. Both nullable INTEGER: existing rows and
    # runs with no injected context migrate to NULL ("not measured").
    map_tokens: int | None = None
    lesson_corpus_tokens: int | None = None
    # V34 planned half of the cost pair (#1092). The token×pricing-bound figure
    # dispatch planned against, kept alongside ``estimated_cost_usd`` (the
    # authoritative actual — the runner's native billed total when it reports
    # one, #1082). NULL means no bound was recorded: historical rows, and runs
    # whose cost was never computed. Never 0.0-for-missing — a $0 planned bound
    # would read as an infinite over-run in the accuracy ratio.
    planned_cost_usd: float | None = None
    # V37 immutable inference-transport admission evidence. Historical rows
    # migrate to direct mode; nullable values mean the datum did not apply or
    # was not observed. Endpoints contain a sanitized origin only.
    transport_mode: str = "direct"
    transport_endpoint: str | None = None
    transport_route: str | None = None
    transport_managed: bool = False
    transport_expected_version: str | None = None
    transport_observed_version: str | None = None
    transport_config_sha256: str | None = None
    transport_preflight_verdict: str | None = None
    # Written only by the shared pre-admission refusal writer.
    pre_admission_refusal: bool = False
    # V65: the dispatch prompt this run was launched with carried the
    # pre-publish self-check (#2454). Read off the rendered prompt bytes, so it
    # cannot claim a section the agent never saw. False on every historical row
    # and on every run kind whose prompt has no such section (review,
    # remediation, resolve, ci-fix) — those were never offered one.
    self_check: bool = False
    # V66 fleet placement (#2398). WHERE this run's agent ran: ``"local"`` for
    # the operator machine (every pre-v66 row, and every run under the default
    # ``[sandbox] placement``) or a host kew knows, e.g. ``"gcp"``.
    # ``run_host_instance`` names the individual machine within that host — the
    # Compute Engine instance — and is NULL for ``local``, which has exactly
    # one. The pair is the fleet's slot-accounting key: it is what tells the
    # per-host capacity check (``[hosts.gcp] max_runs``) which instances are
    # full, and what stops an idle stop from taking down a VM that still has a
    # run on it. Decided at admission and immutable for the run — the upsert
    # preserves it, exactly as it preserves ``container_name``.
    run_host: str = "local"
    run_host_instance: str | None = None
    # V74: the ``prompt_profile_versions.id`` of the operator addendum this
    # run's prompt rendered (#3022), or NULL for the bare base prompt. Fixed
    # at launch: the upsert keeps the first non-NULL value, so a later save
    # built without it cannot erase what the agent ran under.
    prompt_profile_version_id: int | None = None


@dataclass(frozen=True)
class EpicReservation:
    """One epic-DAG node's atomic in-flight reservation (#427 Phase 2a).

    ``reserve_epic_node`` is the authoritative in-flight signal for the epic
    driver — it must NOT use ``try_claim_issue``. Uniqueness on ``issue_num``
    is enforced by the table's PRIMARY KEY, not a check-then-insert race.
    """

    issue_num: int
    epic_ref: str
    run_id: str
    reserved_at: datetime
    # V25 durable work-item id (L3). For a GitHub node this is the stringified
    # ``issue_num``; for a non-numeric tracker (Linear) ``issue_num`` is the
    # ``0`` sentinel and ``item_id`` carries the real key (``SFD-12``).
    item_id: str = "0"


@dataclass(frozen=True)
class ContinuationExpiry:
    """Validated replacement authority retained independently of run history."""

    abandonment_identity: str
    abandoned_generation: int
    replacing_checkout_id: str
    replacing_run_id: str
    replaced_at: str


@dataclass(frozen=True)
class ContinuationRecord:
    """A preserved worktree waiting for, or owned by, a continuation.

    The five identity fields form the durable key.  ``generation`` changes on
    every successful claim, so a writer that lost ownership cannot finalize a
    later continuation of the same preserved worktree.
    """

    epic_ref: str
    item_id: str
    original_run_id: str
    branch: str
    worktree_identity: str
    state: ContinuationState
    generation: int
    continuation_run_id: str | None
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None = None
    terminal_at: datetime | None = None

    @property
    def worktree(self) -> str:
        """Compatibility façade for callers that call the identity a path."""
        return self.worktree_identity

    @property
    def claim_run_id(self) -> str | None:
        """The run id that currently owns this continuation generation."""
        return self.continuation_run_id


@dataclass(frozen=True)
class EpicHold:
    """One epic-DAG node CI hold (e.g. ``reason="ci_pending"``)."""

    issue_num: int
    epic_ref: str
    reason: str
    held_at: datetime
    # V25 durable work-item id (L3); see ``EpicReservation.item_id``.
    item_id: str = "0"


@dataclass(frozen=True)
class OrphanPRRecord:
    """A persisted orphan PR row (open agent PR with no successful run, #680).

    ``detected_at`` is the first-sighting timestamp (preserved across re-syncs);
    ``acked_at`` is NULL until an operator acknowledges the PR (the ack verb
    lands in a later group — this group only persists and queries the state).
    """

    pr_number: int
    url: str
    branch: str
    reason: str  # "failed-run" | "no-record"
    detected_at: datetime
    acked_at: datetime | None


NO_EARLIER_RETRY = datetime(1, 1, 1, tzinfo=UTC)
"""The ``retried_at`` and cutoff of a run-bound retry marker with no operator
retry before it (#2969): earlier than any run, so it supersedes nothing by
time."""


@dataclass(frozen=True)
class EpicRetry:
    """One epic-DAG node's operator-initiated retry marker (#427 Phase 2).

    Non-destructive alternative to deleting a node's terminal-failure
    ``runs`` rows: ``retried_at`` records WHEN the operator retried, and the
    overlay (``overlay_status``) treats any failure run OLDER than this
    timestamp as superseded — preserving cost/token history for `kew
    report`.
    """

    issue_num: int
    epic_ref: str
    retried_at: datetime
    # V25 durable work-item id (L3); see ``EpicReservation.item_id``.
    item_id: str = "0"
    # V72 (#2969): the runs a host-restart re-admission supersedes, by
    # identity: the reaped run and every run an earlier retry had already
    # superseded. They are superseded whatever the clocks say; ``retried_at``
    # keeps the earlier operator retry's real time (the review cycle's start),
    # or :data:`NO_EARLIER_RETRY`.
    superseded_run_ids: frozenset[str] = frozenset()
    # V72 (#2969): the failure-supersession cutoff of a run-bound marker, kept
    # apart from ``retried_at``. None means ``retried_at`` is the cutoff.
    superseded_before: datetime | None = None

    @property
    def supersedes_before(self) -> datetime:
        """Failure runs started before this are superseded by time."""
        if self.superseded_before is not None:
            return self.superseded_before
        return self.retried_at


@dataclass(frozen=True)
class ReviewRound:
    """One persisted governed-review round for a DAG node (F8/#977).

    Append-only history: ``round`` is 1-based within the current review cycle,
    where a cycle boundary is the node's latest ``epic_retries`` marker (a
    ``kew epic retry`` grants a fresh review allowance, F15). The reset is
    non-destructive — prior-cycle rows stay in the history; only
    :meth:`SqliteRunStore.review_round_count` and
    :meth:`SqliteRunStore.latest_review_round` scope to the current cycle. This
    is operational state, not an attested fact: the attested per-round verdict
    is the ``adversarial_review_verdict`` audit event (Task 13), so the table
    lives off the audit hash chain.
    """

    epic_ref: str
    item_id: str
    round: int
    head_sha: str
    verdict: str
    sidecar_digest: str | None
    recorded_at: datetime
    sidecar_path: str | None = None
    """Host path of this round's stored sidecar (``logs/dispatch/...``), when the
    round was recorded from a collected sidecar. NEEDS_CHANGES remediation reads
    the findings from here (#1030); None for pre-v32 rows and refusal/DEGRADED
    rounds that carry no sidecar file."""
    review_mode: str | None = None
    """``full`` or ``delta`` (#2111): whether the reviewer reviewed the whole PR
    diff or the change since the previous round's head. None for pre-v58 rows
    (all full reviews) and for DEGRADED/refusal rounds that reviewed nothing."""
    accepted_verdict: str | None = None
    """The driver's own disposition for this round when the review stop rule
    accepted it: ``READY_WITH_RESIDUALS`` (#2151). None for every other round,
    and for pre-v59 rows — a pre-v59 acceptance is on the audit chain only,
    which is the reader split #2151 closes. A separate column, never an
    overwrite: ``verdict`` stays exactly as the reviewer returned it.
    """
    plan_sha: str | None = None
    """The recorded ``item_plans`` plan this round was judged against (#2290).
    None for pre-v60 rows and for any round whose reviewer was given no
    criteria — which, until the reviewer is handed them, is every round. A
    reader must treat None as "judged against nothing stated", never as
    "judged against the current plan"."""
    criteria_json: str | None = None
    """The criteria list, as JSON, exactly as it was handed to the reviewer for
    this round. Stored beside ``plan_sha`` rather than resolved through it: the
    plan a later body edit supersedes must still be readable as what THIS round
    was judged against."""

    @property
    def effective_verdict(self) -> str:
        """The disposition a reader should act on for this round.

        The driver's acceptance when the stop rule accepted the round, else the
        reviewer's verdict. A watcher polling ``verdict`` alone reads an accepted
        round as ``NEEDS_CHANGES`` and waits for a merge that already may go.
        """
        return self.accepted_verdict or self.verdict


@dataclass(frozen=True)
class ItemPlanRow:
    """One recorded ``item_plans`` row: what the host read an item to ask for.

    Operational state, not an attested fact — the attested pair is the
    ``plan_recorded`` / ``plan_superseded`` audit events, so the table lives off
    the hash chain and carries the readable copy.
    """

    id: int
    item_id: str
    run_id: str | None
    """The dispatch run that read this body and recorded its plan. A dispatch
    records the plan once its run id exists, so every dispatch-recorded row
    carries one; None means the plan was recorded outside a dispatch (a
    ``human`` edit, say)."""
    body_digest: str
    """sha256 of the issue body this plan was read from — the supersession key."""
    source: str
    """``extracted``, ``model`` or ``human`` (:data:`PLAN_SOURCES`)."""
    plan_sha: str
    """sha256 of the canonical plan JSON. Two bodies stating the same criteria
    share it; the BODY's identity is ``body_digest``."""
    plan_json: str
    criteria_count: int
    approved_by: str | None
    """The person who accepted this plan. Only a person may: a model proposes."""
    recorded_at: datetime
    superseded_at: datetime | None
    """When a later body displaced this row. None means it is the current plan."""


@dataclass(frozen=True)
class ItemPlanWrite:
    """The outcome of one :meth:`SqliteRunStore.record_item_plan` call.

    ``recorded`` is False when the body was unchanged, which is the common case
    on a re-reading tick; the caller emits no audit event for it. ``superseded``
    names the row an edited body displaced, so the caller can attest that too.
    """

    current: ItemPlanRow
    recorded: bool
    superseded: ItemPlanRow | None


@dataclass(frozen=True)
class TriageDecisionRow:
    """One recorded ``triage_decisions`` row: what triage PROPOSED about an item.

    Operational state, not an attested fact — the attested pair is the
    ``triage_decided`` / ``triage_resolved`` audit events, so the table lives
    off the hash chain and carries the readable copy.

    The decision fields are nullable because an ``unavailable`` row has no
    decision: the classifier was unreachable, or its JSON never validated after
    the one repair retry. A reader must not read NULL ``type`` as "no type was
    needed" — it means no classification was obtained, which is why the row
    exists at all.
    """

    id: int
    item_id: str
    body_digest: str
    """sha256 of the issue body this decision was taken over — the supersession
    key, and the same digest ``item_plans`` uses, so a plan and a decision read
    from one body are joinable."""
    backend: str
    """``local`` or ``runner`` — where the classifier ran."""
    model: str
    prompt_sha: str
    """sha256 of the rendered prompt, with its per-render sentinel normalized
    (:func:`kew.triage.classify.prompt_sha`)."""
    outcome: str
    """A :data:`TRIAGE_OUTCOMES` member: what the policy proposed. In shadow
    mode every one of them is a proposal, including ``propose_dispatch``."""
    decision_json: str
    """The classifier's answer verbatim, or ``{}`` when there was none."""
    recorded_at: datetime
    source_tracker: str | None = None
    issue_num: int | None = None
    run_id: str | None = None
    type: str | None = None
    priority: str | None = None
    complexity: str | None = None
    confidence: float | None = None
    actionable: bool | None = None
    needs_info: bool | None = None
    duplicate_of: int | None = None
    resolution: str | None = None
    """What a PERSON decided (:data:`kew.run_store_triage.TRIAGE_RESOLUTIONS`).
    None means nobody has; nothing but ``kew triage decide`` ever fills it."""
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    superseded_at: datetime | None = None
    """When a later body displaced this row. None means it is current."""

    @property
    def unresolved(self) -> bool:
        """Whether this proposal is still waiting on a person."""
        return self.resolution is None and self.superseded_at is None


@dataclass(frozen=True)
class TriageDecisionWrite:
    """The outcome of one :meth:`SqliteRunStore.record_triage_decision` call.

    ``recorded`` is False when the body was unchanged, which is the common case
    on a re-triage; the caller emits no audit event for it. ``superseded`` names
    the row an edited body displaced, so the caller can attest that too.
    """

    current: TriageDecisionRow
    recorded: bool
    superseded: TriageDecisionRow | None


EPIC_TICK_OUTCOME_EMPTY_GRAPH = "empty_graph"
"""The one tick outcome that is evidence AGAINST the ref naming an epic.

``advance`` raises ``EmptyEpicGraphError`` when the tracker query matched no
work at all, which it documents as exactly two things: a typo'd epic name or a
wrong-tracker cwd. So a ref whose attempts ALL ended here is not an epic whose
ticks were refused — it is a name that never named an epic, and
:meth:`SqliteRunStore.list_epic_refs` leaves it out for that reason (#2438
review).
"""

EPIC_TICK_OUTCOME_FAILED = "failed"
"""The outcome of a tick that raised — the one a change strip keeps beside the
changes, as its red × (#2844 D23)."""

#: Outcomes an epic-driver tick attempt can finish on (#2304). The first four
#: are what the tick DID, worst-first-is-not-the-point: they are read in this
#: order so a tick that dispatched and merged in one pass reads as the dispatch
#: an operator was waiting for. The next three are the paths that used to reach
#: only stderr — the exception, the lock-out and the empty graph.
#:
#: ``abandoned`` is the one outcome no tick writes about itself: it is written
#: for a row by a LATER tick of the same epic, because the driver that opened it
#: died before it could close anything (see
#: :meth:`SqliteRunStore.abandon_open_epic_tick_logs`).
EPIC_TICK_OUTCOMES = (
    "dispatched",
    "merged",
    "held",
    "nothing",
    EPIC_TICK_OUTCOME_FAILED,
    "locked_out",
    EPIC_TICK_OUTCOME_EMPTY_GRAPH,
    "abandoned",
)

#: The outcomes of an attempt that got past ``begin_epic_tick`` and ran the
#: tick to a result (``_tick_outcome``): what the tick DID.
EPIC_TICK_REACHED_OUTCOMES = frozenset(("dispatched", "merged", "held", "nothing"))

EPIC_TICK_LOG_REACHED_TICK = "reached_tick"
"""What an attempt's ``signal`` holds from the moment ``begin_epic_tick``
succeeds until the attempt closes on a result (#2882).

An attempt row is opened before the lock, the preflight and the graph build,
so "open" alone said nothing about whether a tick ever began, and an attempt
blocked in preflight read as a heartbeat. ``signal`` is the column an open row
has no other use for, and the one ``finish_epic_tick_log`` writes the tick's
real signal into when it records a result — so no schema step. A close with no
signal (the exception path) and the ``abandoned`` sweep keep it: such an
attempt did reach its tick. Readers never serve it as a signal —
:meth:`SqliteRunStore._row_to_epic_tick_log` turns it into
:attr:`EpicTickLogEntry.reached_tick`."""

EPIC_TICK_LOG_CAP = 2000
"""Rows ``epic_tick_log`` keeps PER EPIC. A tick log is a strip of the recent
past, not a ledger: the strip surfaces read the last couple of hundred and
nothing reads further back, so an epic ticking every 30s is held to about
seventeen hours of history rather than growing without bound. Pruning is by row
id (monotonic per insert), so it never depends on a stamp a bad historical row
might be missing."""

EPIC_TICK_LOG_TAIL_MAX = 500
"""Largest page ``list_epic_tick_log_tail`` will return."""


def _epic_tick_log_stamp(last_id: int | None, closed: int, reached: int) -> str:
    """The stamp a reader re-reads a tick strip on (#2304 review round 3).

    A strip can only change when the driver ATTEMPTS a tick, and the readers
    cannot poll a strip per row on a five-second roster. The heartbeat's
    ``tick_count`` looked like the signal, but it is written by
    ``begin_epic_tick`` — the far side of the lock, the preflight and the
    non-empty-graph guard — so a driver that is locked out, failing preflight
    or finding an empty graph moves it never, and those are exactly the
    attempts a strip exists to show. Stamping on this table instead makes the
    signal the same fact the strip is drawn from.

    Two numbers, because an attempt moves the strip twice: ``last_id`` when the
    row is OPENED (the slot the strip paints live) and ``closed`` when any row
    of the epic is closed (the slot's real outcome, including the ``abandoned``
    sweep of a dead driver's row, which moves no id at all). An epic with no
    attempts stamps ``""`` — itself a stable key, so a never-driven epic is
    not re-read either.

    A third, ``reached``, counts the rows marked as having reached their tick
    (:data:`EPIC_TICK_LOG_REACHED_TICK`, #2882): that mark lands on an OPEN
    row, between the two moves above, and a strip read before it would keep
    reading the attempt as one that never began.
    """
    return "" if last_id is None else f"{last_id}.{closed}.{reached}"


@dataclass(frozen=True)
class EpicTickLogEntry:
    """One epic-driver tick ATTEMPT, finished or not (#2304).

    ``epic_ticks`` is a change journal: it dedups on the snapshot hash, so a
    tick that changed nothing leaves no row and about nine ticks in ten are
    invisible (the merge-path epic ran 476 ticks and journaled 18). This table
    is the other reading — one row per attempt, including the attempts that
    never reach the journal at all: a tick that raised, one locked out by
    another driver, one whose graph came back empty.

    ``ts_finished`` and ``outcome`` are NULL while the tick is still running,
    which is the one state a surface draws as live — for a bounded while: a
    driver that dies mid-tick closes nothing, so the epic's next tick closes
    the row as ``abandoned`` and the reader stops calling an open row live once
    it is older than the driver-liveness threshold. ``journal_tick_id`` points
    at the ``epic_ticks`` row this attempt appended, when it appended one.
    """

    id: int
    epic_ref: str
    ts_started: str  # ISO-8601 UTC
    ts_finished: str | None  # None while the tick is in progress
    outcome: str | None  # one of EPIC_TICK_OUTCOMES; None while in progress
    signal: str | None
    n_dispatched: int
    n_merged: int
    n_held: int
    n_stuck: int
    journal_tick_id: int | None
    error_class: str | None
    #: Whether the attempt got past ``begin_epic_tick`` (#2882): an outcome in
    #: :data:`EPIC_TICK_REACHED_OUTCOMES`, or the mark
    #: :data:`EPIC_TICK_LOG_REACHED_TICK`. An open attempt without it is still
    #: before the lock, the preflight or the graph build — no heartbeat.
    reached_tick: bool = False


_JOURNAL_MARK_SQL = (
    "SELECT id, tick_ts, julianday(tick_ts) AS at,"  # nosec B608
    " CASE WHEN json_valid(snapshot) THEN json_extract(snapshot, '$.signal') END"
    " AS signal,"
    + ",".join(
        f" CASE WHEN json_valid(actions)"
        f" THEN coalesce(json_array_length(actions, '$.{key}'), 0) ELSE 0 END"
        f" AS n_{key}"
        for key in ("dispatched", "merged", "closed", "held", "stuck")
    )
    + " FROM epic_ticks WHERE epic_ref = ? ORDER BY id DESC LIMIT ?"
)
"""A change strip's journal rows (#2882): the instant and the action counts,
read in SQL so a malformed historical row reads as zero counts and an
unreadable stamp as a NULL instant, rather than raising (the operator rule)."""


def _journal_outcome(row: sqlite3.Row) -> str:
    """A journal row's outcome, read the way the driver closes an attempt
    (``_tick_outcome``): dispatched, then merged, then held."""
    if row["n_dispatched"]:
        return "dispatched"
    if row["n_merged"] or row["n_closed"]:
        return "merged"
    return "held" if row["n_held"] else "nothing"


def _journal_mark(epic_ref: str, row: sqlite3.Row) -> EpicTickLogEntry:
    """A change mark for a journal row no attempt row carries (#2882)."""
    signal = row["signal"]
    return EpicTickLogEntry(
        id=-int(row["id"]),
        epic_ref=epic_ref,
        ts_started=str(row["tick_ts"]),
        ts_finished=str(row["tick_ts"]),
        outcome=_journal_outcome(row),
        signal=signal if isinstance(signal, str) else None,
        n_dispatched=int(row["n_dispatched"]),
        n_merged=int(row["n_merged"]) + int(row["n_closed"]),
        n_held=int(row["n_held"]),
        n_stuck=int(row["n_stuck"]),
        journal_tick_id=int(row["id"]),
        error_class=None,
        reached_tick=True,
    )


def _merge_journal_marks(
    epic_ref: str,
    attempts: list[sqlite3.Row],
    journal: list[sqlite3.Row],
) -> list[tuple[float, int, EpicTickLogEntry]]:
    """A change strip's marks: one per journal row, plus the failed and the
    running attempts (see :meth:`SqliteRunStore.list_epic_tick_log_marks`),
    each with its instant (julian day; an unreadable stamp sorts oldest) and
    id, which is the order the two tables share.

    A journal row is drawn as the attempt that carries its id when that
    attempt began no later than the row — a later attempt carrying the id is a
    no-op over the unchanged snapshot. An unreadable stamp is taken at the
    attempt's word. An attempt carrying a journal id outside the fetched page
    keeps its own reading — the row is older than every change on the page,
    or (a store that never journaled it) the attempt is all there is.
    """
    by_journal = {a["journal_tick_id"]: a for a in attempts if a["journal_tick_id"]}
    marks: list[tuple[float, int, EpicTickLogEntry]] = []
    claimed: set[int] = set()
    for row in journal:
        attempt = by_journal.get(row["id"])
        if attempt is not None and _began_by(attempt["at"], row["at"]):
            claimed.add(attempt["id"])
            continue
        marks.append((_at(row["at"]), int(row["id"]), _journal_mark(epic_ref, row)))
    paged = {int(row["id"]) for row in journal}
    marks.extend(
        (_at(a["at"]), int(a["id"]), SqliteRunStore._row_to_epic_tick_log(a))
        for a in attempts
        if a["id"] in claimed or _unpaired_mark(a, paged)
    )
    return marks


def _at(julian: float | None) -> float:
    return float("-inf") if julian is None else float(julian)


def _began_by(began: float | None, appended: float | None) -> bool:
    return began is None or appended is None or began <= appended


def _unpaired_mark(attempt: sqlite3.Row, paged: set[int]) -> bool:
    """Whether an attempt no journal row on the page claimed is still a mark:
    it is running, it failed, or the journal row it carries is off the page.
    A later no-op carrying a paged row's id is not."""
    if attempt["ts_finished"] is None or attempt["outcome"] == EPIC_TICK_OUTCOME_FAILED:
        return True
    journal_id = attempt["journal_tick_id"]
    return journal_id is not None and journal_id not in paged


@dataclass(frozen=True)
class EpicTick:
    """One epic-driver tick journal entry (#454 Phase A).

    Immutable snapshot of DAG state at one tick: ``signal`` + ``counts`` +
    ``nodes`` + ``edges`` + ``frontier`` (never tick_ts or actions, per spec).
    Appends are dedup'd against the epic's latest row only: a snapshot whose
    hash matches the latest row is not re-inserted, but a hash recurring after
    intervening changes appends a new row.
    """

    id: int
    epic_ref: str
    tick_ts: str  # ISO-8601 UTC
    snapshot: dict[str, Any]  # parsed JSON, schema v1
    actions: dict[str, Any]  # parsed JSON, schema v1
    snapshot_hash: str


@dataclass(frozen=True)
class NodePrFacts:
    """One journaled DAG node's pull-request facts, read back (#2303).

    The driver reads ``headRefOid``, ``mergeStateStatus`` and the head's push
    time every tick and now persists them in the snapshot node, so a reader with
    no GitHub token can still tell a review verdict's head from the current one,
    and a conflict from a base that is merely behind.

    Every field but ``epic_ref``/``item_id``/``observed_at`` is ``None`` when the
    snapshot does not carry it — a tick that could not read the forge, or a
    pre-#2303 row. ``None`` is therefore "nobody looked", never "clean".

    ``observed_at`` is the TICK, not the merge read: the driver carries a merge
    state forward across a tick GitHub answered ``UNKNOWN`` on (so the flip does
    not churn the snapshot hash), and ``merge_state_at`` is when the state was
    last actually read. A caller that acts on ``merge_state`` must age it by
    ``merge_state_at``; ``observed_at`` would read it as fresher than it is.
    """

    epic_ref: str
    item_id: str
    observed_at: str
    status: str | None = None
    pr_url: str | None = None
    head_oid: str | None = None
    merge_state: str | None = None
    merge_state_since: str | None = None
    merge_state_at: str | None = None
    pr_pushed_at: str | None = None


@dataclass(frozen=True)
class EpicDriverState:
    """One epic driver's heartbeat state (#454 Phase A).

    Two-phase commit: begin_epic_tick (phase 1, UPSERT on epic_ref) writes
    last_tick_started_at and increments tick_count; finish_epic_tick (phase 2)
    writes the remaining fields. Both are per-epic (no sharding). Nullable
    interval_s for one-shot runs.

    ``tracker`` is the durable v33 column recording which control plane the
    driver reads (``github``/``linear``); None on a pre-v33 row that no tick has
    refreshed. It is part of the state a health reader needs, so it is carried
    here rather than re-read through ``epic_trackers()`` (#2297).
    """

    epic_ref: str
    last_tick_started_at: str | None
    last_tick_ts: str | None
    last_tick_duration_s: float | None
    tick_count: int
    interval_s: float | None
    last_signal: str | None
    last_tick_id: int | None
    tracker: str | None = None
    #: The v62 cadence EXPECTATION (``[epic] driver_expected_interval_seconds``)
    #: last recorded by a drive that has no cadence of its own. Never mixed into
    #: ``interval_s``: a reader must be able to tell a measured cadence from a
    #: configured guess (#2297). None where no drive ever supplied one.
    expected_interval_s: float | None = None
    #: The v63 snapshot of this epic's driver LOCK, taken by the serve sweep for
    #: readers that cannot open the lock file themselves (every serve-side
    #: liveness reading). ``lock_pid``/``lock_pid_alive`` are the holding process
    #: and whether it was still alive; ``lock_heartbeat_at`` is the heartbeat the
    #: holder had refreshed (the one that keeps beating mid-tick, where
    #: ``last_tick_started_at`` goes stale); ``lock_observed_at`` is when the
    #: sweep looked, so an old snapshot can be refused instead of believed. All
    #: None where no sweep has looked since v63 — or where it looked and found
    #: the lock free, which is normal between ticks and proves nothing.
    lock_pid: int | None = None
    lock_pid_alive: bool | None = None
    lock_heartbeat_at: str | None = None
    lock_observed_at: str | None = None


@dataclass
class LoopRecord:
    """A first-class loop: a goal kew pursues to a terminal state.

    ``drive_mode`` is intentionally NOT stored — derive it from the runner's
    ``loop_style`` at read time (spec R3). ``wall_clock_secs``/``max_steps`` are
    mandatory non-null so a $0 cost estimate can never run unbounded (spec R2).
    """

    loop_id: str
    source: str  # "issue" (v1)
    goal_text: str
    runner_name: str
    created_at: datetime
    issue_num: int | None = None
    repo: str | None = None
    status: LoopStatus = "pursuing"
    status_reason: str = ""
    budget_usd: float | None = None
    wall_clock_secs: int = 1800
    max_steps: int = 200
    spent_usd: float = 0.0
    step_count: int = 0
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    pause_requested: bool = False
    # V24 durable work-item id (A3); mirrors ``RunRecord.item_id``. ``loops``
    # allows a NULL ``issue_num`` so no ``0`` sentinel is needed here.
    item_id: str | None = None
    # V35 durable route (#1111): the model this loop runs under, recorded
    # alongside ``runner_name`` so a later actor (resume, another process) can
    # reconstruct the SAME route instead of re-resolving the config default.
    # Empty = pre-v35 loop with no recorded model -> callers fall back to config.
    model: str = ""
    # V35 routed complexity tier (#1111): the `[routing]` tier the route above was
    # resolved at, so a later actor re-resolving the route (a DB override raised
    # mid-flight) resolves it at the SAME tier instead of `[routing].default`.
    # Empty = pre-v35 loop with no recorded tier -> the default tier, as before.
    complexity: str = ""


@dataclass
class InteractiveUsage:
    """One ingested interactive-usage observation: (runner, model, session,
    export-instant). Stored off the hash chain in interactive_usage (#256)."""

    runner: str
    model: str
    observed_at: str  # ISO-8601 UTC export instant (data point timeUnixNano)
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    session_id: str | None
    ingest_source: str
    dedup_key: str
    # V16 cache observability (#348)
    prefix_key: str | None = None
    provider: str | None = None
    # V18 per-subagent attribution (#382)
    query_source: str | None = None
    agent_name: str | None = None


@dataclass
class TrackerEvent:
    """One inbound tracker webhook delivery (Linear Issue/Comment/Project),
    normalized off the audit hash chain in tracker_events (W1/#893).

    ``delivery_id`` is the tracker's delivery identifier and the idempotency
    key (UNIQUE); a redelivered event collapses to the same row. ``payload``
    is the raw webhook JSON verbatim — an external fact, never an attested
    governance action, so it stays OFF-CHAIN like interactive_usage (#256)."""

    delivery_id: str
    source: str  # tracker source, e.g. "linear"
    event_type: str  # Linear entity type: Issue / Comment / Project
    action: str | None  # create / update / remove
    entity_id: str | None  # the tracked entity's id (data.id)
    webhook_timestamp: int | None  # ms-epoch from the payload, for reconcile
    payload: str  # raw webhook JSON body
    received_at: str | None = None  # ISO-8601 UTC ingest instant (set on write)


@dataclass
class IterationRecord:
    """One governance checkpoint within a loop (a step / assistant turn).

    ``run_id`` is the PARENT run and is nullable: in delegate mode one run has
    many iterations, so it is shared, not unique (spec R4). ``status`` is a
    free-form lifecycle snapshot string (intentionally not the ``LoopStatus``
    Literal — an iteration records a moment, not the loop's terminal verdict).
    """

    iteration_id: str
    loop_id: str
    idx: int
    started_at: datetime
    run_id: str | None = None
    kind: str = ""
    status: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    finished_at: datetime | None = None


@dataclass
class AuditEvent:
    """One row of the audit_events table — the governance envelope."""

    id: int
    timestamp: str  # ISO 8601 UTC
    event: str
    run_id: str | None = None
    issue_num: int | None = None
    # V24 durable work-item id (mirrors RunRecord.item_id). In the entry_hash
    # preimage of every row written from v77 on (#2029); rows written before
    # that step leave it out. Null on the classic GitHub path (issue_num
    # carries the key).
    item_id: str | None = None
    repo: str | None = None
    actor_type: str | None = None  # human | policy | agent
    actor_id: str | None = None
    endpoint: str | None = None
    policy_hash: str | None = None  # null until #250
    prev_hash: str | None = None  # null until #249
    payload: dict[str, Any] | None = None
    # V39 idempotency identity. Hashed like item_id on rows written from v77
    # on; continuation payloads also carry it inside the hashed payload.
    dedup_key: str | None = None


@runtime_checkable
class RunStore(Protocol):
    """Protocol for run history storage backends."""

    def read_ledger_id(self) -> str | None: ...
    def read_committed(self) -> contextlib.AbstractContextManager[RunReadCursor]: ...
    def save(self, record: RunRecord, *, update_ownership: bool = False) -> None: ...
    def save_over_dead_child(self, record: RunRecord) -> bool: ...
    def register_ci_fix_attempt(
        self, run_id: str, pr_number: int, *, max_attempts: int | None = None
    ) -> None: ...
    def allocate_ci_fix_attempt(
        self, run_id: str, pr_number: int, *, max_attempts: int
    ) -> None: ...
    def refuse_launch(self, run_id: str) -> None: ...
    def record_launch(self, run_id: str) -> None: ...
    def count_launched_ci_fix_attempts(self, repo: str, pr_number: int) -> int: ...
    def count_refused_ci_fix_attempts(self, repo: str, pr_number: int) -> int: ...
    def finalize_cancelled_run(
        self, run_id: str, *, startup_state: StartupState
    ) -> bool: ...

    def defer_cancelled_run(
        self, run_id: str, *, startup_state: StartupState
    ) -> None: ...

    def reap_cancelled_runs(self) -> None: ...
    def settle_never_started(
        self, record: RunRecord, *, actor: tuple[str, str]
    ) -> NeverStartedReap: ...
    def transition_run_status(
        self,
        run_id: str,
        *,
        expected_status: RunStatus,
        new_status: RunStatus,
        error_category: str,
        pr_url: str | None = None,
    ) -> bool: ...
    def finish_pending_refusal(
        self,
        run_id: str,
        *,
        status: RunStatus,
        error_category: str,
        exit_code: int,
        finished_at: datetime,
    ) -> bool: ...
    def finalize_orphaned_pending(
        self,
        run_id: str,
        *,
        error_category: str,
        finished_at: datetime,
        duration_seconds: float | None = None,
        precommit_check: Callable[[], None] | None = None,
        reservation: EpicReservation | None = None,
        expected_owner: RunRecord | None = None,
        retry_reason: str | None = None,
        retry_repo: str | None = None,
    ) -> bool: ...
    def request_orphan_cleanup(self, record: RunRecord, runtime: str) -> bool: ...
    def finalize_awaiting_publication(
        self, run_id: str, *, pr_url: str | None = None
    ) -> bool: ...
    def record_managed_transport_admission(
        self, run_id: str, *, observed_version: str, config_sha256: str
    ) -> bool: ...
    def try_admit_run(
        self,
        record: RunRecord,
        cap: int,
        *,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        exclude_run: str | None = None,
        on_admit: Callable[[], None] | None = None,
    ) -> bool: ...
    def admit_run_with_identity(
        self,
        record: RunRecord,
        identity: AdmissionIdentity,
        *,
        cap: int = 0,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        exclude_run: str | None = None,
        on_admit: Callable[[], None] | None = None,
        enrich: Callable[[sqlite3.Cursor], None] | None = None,
        reservation: EpicReservation | None = None,
        reservation_pinned: bool = False,
    ) -> bool: ...
    def build_admission_identity(
        self,
        run_id: str,
        *,
        tracker: str,
        item_id: str | None,
        repo: str | None,
        attempt_number: int = 1,
    ) -> AdmissionIdentity: ...
    def get_admission_identity(self, run_id: str) -> AdmissionIdentity | None: ...
    def route_profile_enricher(
        self, profile: RecordableRouteProfile
    ) -> Callable[[sqlite3.Cursor], None]: ...
    def record_route_profile(self, profile: RecordableRouteProfile) -> None: ...
    def get_route_profile(
        self, run_id: str, attempt_number: int | None = None
    ) -> StoredRouteProfile | None: ...
    def route_profile_json(
        self, run_id: str, attempt_number: int = 1
    ) -> str | None: ...
    def require_route_profile(
        self, run_id: str, attempt_number: int
    ) -> StoredRouteProfile: ...
    def list_route_profiles(self, run_id: str) -> list[StoredRouteProfile]: ...
    def append_route_observation(
        self,
        run_id: str,
        attempt_number: int,
        *,
        field: str,
        value: str | None,
        source: str,
    ) -> None: ...
    def list_route_observations(
        self, run_id: str, attempt_number: int | None = None
    ) -> list[RouteObservation]: ...
    def list_admission_identities(self, run_id: str) -> list[AdmissionIdentity]: ...
    def find_admissions_for_item(
        self, source_tracker: str, item_id: str
    ) -> list[AdmissionIdentity]: ...
    def get(self, run_id: str) -> RunRecord | None: ...
    def list_by_issue(self, issue_num: int) -> list[RunRecord]: ...
    def list_by_item(self, item_id: str) -> list[RunRecord]: ...
    def list_runs_by_branch(
        self, branch: str, *, limit: int = 33, offset: int = 0
    ) -> list[RunRecord]: ...
    def list_runs_by_branch_after(
        self, branch: str, *, limit: int = 33, after: tuple[str, int] | None = None
    ) -> tuple[list[RunRecord], tuple[str, int] | None]: ...
    def find_run_by_branch(self, branch: str) -> RunRecord | None: ...
    def get_publication_job(self, job_id: str) -> "PublicationJob | None": ...
    def get_normalized_job(self, job_id: str) -> "PublicationJob | None": ...
    def publication_jobs_for_head(
        self, *, branch: str, head_sha: str, limit: int = 2
    ) -> list["PublicationJob"]: ...
    def normalized_jobs_for_head(
        self, *, branch: str, head_sha: str, limit: int = 2
    ) -> list["PublicationJob"]: ...
    def sum_costs_by_issue(
        self, issue_nums: list[int], *, since: str, until: str | None = None
    ) -> dict[int, float]: ...
    def sum_costs_by_item(
        self, item_ids: list[str], *, since: str, until: str | None = None
    ) -> dict[str, float]: ...
    def list_by_items(self, item_ids: list[str]) -> dict[str, list[RunRecord]]: ...
    def list_all(self) -> list[RunRecord]: ...
    def list_run_page(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[RunRecord]: ...
    def list_recent_completions(self, *, limit: int = 5) -> list[RunRecord]: ...
    def list_since(self, since: datetime) -> list[RunRecord]: ...
    def median_cost_for_bucket(
        self,
        model: str,
        complexity: str,
        run_kind: str = "dispatch",
        *,
        other_kinds: bool = False,
    ) -> tuple[float | None, int]: ...
    def delete(self, run_id: str) -> bool: ...
    def list_pending(self) -> list[RunRecord]: ...
    def live_run_count(
        self,
        *,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> int: ...
    def set_pgid(self, run_id: str, pgid: int) -> None: ...

    def set_container_identity(
        self,
        run_id: str,
        name: str,
        container_id: str | None,
        cidfile: str | None = None,
        *,
        runtime: str | None = None,
    ) -> None: ...

    def set_run_site(
        self, run_id: str, host: str, instance: str | None = None
    ) -> bool: ...

    def set_attempt_pgid(self, run_id: str, pgid: int) -> None: ...

    def retain_termination_members(
        self, run_id: str, pgid: int, members: dict[int, str]
    ) -> None: ...
    def request_run_stop(
        self,
        run_id: str,
        *,
        label_reset: dict[str, str] | None = None,
        reset_labels: LabelReset | None = None,
    ) -> None: ...
    def retry_stop_label_resets(
        self,
        run_id: str | None = None,
        *,
        reset_labels: LabelReset | None = None,
    ) -> None: ...
    def reconcile_host_stops(self) -> int: ...
    def set_outcome(
        self,
        run_id: str,
        *,
        pr_url: str | None,
        pr_state: str | None,
        pr_merged_at: str | None,
        review_cycles: int | None,
        diff_additions: int | None,
        diff_deletions: int | None,
        diff_files: int | None,
    ) -> None: ...
    def list_syncable_runs(
        self, *, item_ids: Sequence[str] | None = None
    ) -> list[RunRecord]: ...
    def set_fleet_stopped(self, reason: str = "") -> None: ...
    def clear_fleet_stopped(self) -> None: ...
    def get_fleet_stopped(self) -> dict[str, str] | None: ...
    def append_audit_event(
        self,
        *,
        timestamp: str,
        event: str,
        run_id: str | None = None,
        issue_num: int | None = None,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        endpoint: str | None = None,
        policy_hash: str | None = None,
        prev_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int | None: ...
    def append_audit_event_once(
        self,
        *,
        dedup_key: str,
        timestamp: str,
        event: str,
        run_id: str | None = None,
        issue_num: int | None = None,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        endpoint: str | None = None,
        policy_hash: str | None = None,
        prev_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int | None: ...
    def list_driver_key_issuances(
        self, *, after_id: int = 0, limit: int = 100
    ) -> list[AuditEvent]: ...
    def list_run_key_issuances(
        self, *, after_id: int = 0, limit: int = 100
    ) -> list[AuditEvent]: ...
    def head_audit_verdicts(
        self, *, event: str, run_id: str, actor: tuple[str, str], head_sha: str
    ) -> frozenset[str]: ...
    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        event_prefix: str | None = None,
        event_prefixes: Sequence[str] | None = None,
        item_id: str | None = None,
        item_ids: Sequence[str] | None = None,
        item_or_issue: str | None = None,
        epic_ref: str | None = None,
        epic_node_ids: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        actor_type: str | None = None,
        issue_num: int | None = None,
        issue_nums: Sequence[int] | None = None,
        after_id: int | None = None,
        after_seq: int | None = None,
        run_id: str | None = None,
        loop_id: str | None = None,
    ) -> list[AuditEvent]: ...
    def audit_event_histogram(
        self,
        *,
        since: datetime,
        until: datetime,
        bucket_s: int,
        event: str | None = None,
        event_prefix: str | None = None,
        event_prefixes: Sequence[str] | None = None,
        item_id: str | None = None,
        item_or_issue: str | None = None,
        epic_ref: str | None = None,
        epic_node_ids: Sequence[str] | None = None,
        actor_type: str | None = None,
        issue_num: int | None = None,
        issue_nums: Sequence[int] | None = None,
        run_id: str | None = None,
    ) -> list[tuple[int, str, int]]: ...
    # I06/I07 sealed lineage and I10A no-change judgments. Declared here
    # because the zero-change dispatch path (I10B) reads and writes them
    # through this protocol, not through the concrete store.
    def list_artifact_receipts(
        self, *, run_id: str | None = None
    ) -> list[ArtifactReceipt]: ...
    def open_no_change_judgment(
        self,
        *,
        intent: "NoChangeIntent",
        policy: str = ...,
        now: datetime,
    ) -> "NoChangeJudgment": ...
    def attest_no_change(
        self,
        *,
        judgment_id: str,
        evidence: "NoChangeEvidence",
        now: datetime,
    ) -> "NoChangeJudgment": ...
    def decide_no_change(
        self,
        *,
        judgment_id: str,
        now: datetime,
    ) -> "NoChangeJudgment": ...
    def save_loop(self, record: "LoopRecord") -> None: ...
    def get_loop(self, loop_id: str) -> "LoopRecord | None": ...
    def list_loops(self, status: str | None = None) -> list["LoopRecord"]: ...
    def request_pause(self, loop_id: str) -> bool: ...
    def clear_pause(self, loop_id: str) -> None: ...
    def add_iteration(self, record: "IterationRecord") -> None: ...
    def list_iterations(self, loop_id: str) -> list["IterationRecord"]: ...
    def list_iterations_tail(
        self, loop_id: str, *, limit: int = 200
    ) -> list["IterationRecord"]: ...
    def last_iteration_at_by_loop(self) -> dict[str, str]: ...
    def list_meta(
        self, prefix: str, *, after: str = "", limit: int = 100
    ) -> list[tuple[str, str]]: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def compare_set_meta(self, key: str, expected: str | None, value: str) -> bool: ...
    def append_audit_event_with_metas(
        self,
        writes: Sequence[tuple[str, str | None, str]],
        *,
        timestamp: str,
        event: str,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        policy_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool: ...
    def claim_meta_unless_item_ran(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        item_id: str,
        since: datetime,
        superseded: Callable[[str], str],
    ) -> tuple[bool, str | None]: ...
    def audit_parse_report(self, since: datetime | None = None) -> tuple[int, int]: ...
    def count_container_runtimes(self, since: datetime) -> dict[str, int]: ...
    def count_successful_runs_by_runner(self, since: datetime) -> dict[str, int]: ...
    def record_interactive_usage(self, usage: "InteractiveUsage") -> bool: ...
    def list_interactive_usage_since(
        self, since: datetime
    ) -> list["InteractiveUsage"]: ...
    def record_tracker_event(self, event: "TrackerEvent") -> bool: ...
    def list_tracker_events_since(self, since: datetime) -> list["TrackerEvent"]: ...
    def has_tracker_events_since(
        self, since: datetime, *, source: str | None = None
    ) -> bool: ...
    def get_epic_graph_cache(self, epic_ref: str) -> tuple[datetime, str] | None: ...
    def put_epic_graph_cache(
        self, epic_ref: str, source: str, fetched_at: datetime, graph_json: str
    ) -> None: ...
    def warm_prefixes(self, provider: str, now: datetime) -> set[str]: ...
    def list_config_overrides(self) -> list[dict[str, Any]]: ...
    def get_config_override(self, field: str) -> dict[str, Any] | None: ...
    def set_config_override(
        self,
        *,
        field: str,
        value_json: str,
        file_value_json: str | None,
        set_at: str,
        set_by: str,
        reason: str = "",
        audit_event_id: int | None = None,
    ) -> None: ...
    def revert_config_override(self, field: str) -> bool: ...
    def reserve_epic_node(self, epic_ref: str, issue_num: int, run_id: str) -> bool: ...
    def reserve_epic_node_item(
        self, epic_ref: str, item_id: str, run_id: str
    ) -> bool: ...
    def append_audit_event_reserving_item(
        self,
        epic_ref: str,
        item_id: str,
        run_id: str,
        *,
        event: str,
        actor_type: str,
        actor_id: str,
        repo: str | None,
        payload: dict[str, Any],
    ) -> bool: ...
    def reserve_epic_node_item_under_cap(
        self, epic_ref: str, item_id: str, run_id: str, cap: int
    ) -> ReserveResult: ...
    def register_continuation_and_release_reservation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        reservation_run_id: str,
        *,
        checkout_generation: str,
        precommit_check: Callable[[], None] | None = None,
    ) -> "ContinuationRecord | None": ...
    def record_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        *,
        checkout_generation: str,
        precommit_check: Callable[[], None] | None = None,
    ) -> "ContinuationRecord": ...
    def get_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
    ) -> "ContinuationRecord | None": ...
    def list_continuations(self, epic_ref: str) -> list["ContinuationRecord"]: ...
    def list_all_continuations(self) -> list["ContinuationRecord"]: ...
    def continuations_for_checkout(
        self, checkout: str | Path, *, item_id: str, branch: str
    ) -> list["ContinuationRecord"]: ...
    def continuation_checkout_generation(self, record: ContinuationRecord) -> str: ...
    def continuation_is_complete(self, record: ContinuationRecord) -> bool: ...
    def continuation_is_recoverable(self, record: ContinuationRecord) -> bool: ...
    def continuation_origins(self, run_id: str, *, limit: int = 2) -> list[str]: ...
    def continuation_run_id_in_use(self, run_id: str) -> bool: ...
    def require_continuation_history(self, record: ContinuationRecord) -> None: ...
    def continuation_was_abandoned(self, record: ContinuationRecord) -> bool: ...
    def continuation_abandonment_identity(
        self, record: ContinuationRecord
    ) -> str | None: ...
    def continuation_expiry(
        self, record: ContinuationRecord
    ) -> ContinuationExpiry | None: ...
    def record_continuation_expiry(
        self, record: ContinuationRecord, expiry: ContinuationExpiry
    ) -> None: ...
    def continuation_replacement_events(
        self, record: ContinuationRecord
    ) -> Iterator[AuditEvent]: ...
    def continuation_owner_run_ids(self, record: ContinuationRecord) -> list[str]: ...
    def require_continuation_owner_history(
        self, record: ContinuationRecord
    ) -> None: ...
    def claim_continuation_under_cap(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        continuation_run_id: str,
        cap: int,
        *,
        claim_evidence: dict[str, Any],
        actor_id: str = "continuation_claim",
        repo: str | None = None,
    ) -> ContinuationClaimResult: ...
    def mark_continuation_awaiting(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str,
        *,
        reason: str = "claim_released",
        cancel_unlaunched: bool = False,
        precommit_check: Callable[[], None] | None = None,
    ) -> bool: ...
    def release_unevidenced_claim(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str,
    ) -> bool: ...
    def finalize_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str | None,
        *,
        abandon_reason: str | None = None,
        actor_id: str | None = None,
        repo: str | None = None,
    ) -> bool: ...
    def release_epic_node(self, issue_num: int, *, run_id: str) -> None: ...
    def release_epic_node_item(
        self, item_id: str, run_id: str | None = None
    ) -> None: ...
    def release_epic_node_item_unless(
        self, item_id: str, run_id: str, *, keep: Callable[[], bool]
    ) -> bool: ...
    def item_reservation(self, item_id: str) -> "EpicReservation | None": ...
    def list_epic_reservations(self, epic_ref: str) -> list["EpicReservation"]: ...
    def live_reservation_counts_by_epic(
        self,
        *,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> dict[str, int]: ...
    def contending_epic_refs(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = CONTENTION_STALE_AFTER,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> set[str]: ...
    def hold_epic_node(
        self, epic_ref: str, item: str, reason: str, *, node_ids: Sequence[str] = ()
    ) -> None: ...
    def hold_epic_node_item(
        self, epic_ref: str, item_id: str, reason: str, *, node_ids: Sequence[str] = ()
    ) -> None: ...
    def hold_epic_node_unless(
        self,
        epic_ref: str,
        item_id: str,
        reason: str,
        *,
        keep: Callable[[str], bool],
        meta: tuple[str, str] | None = None,
    ) -> bool: ...
    def clear_epic_hold(self, item: str) -> None: ...
    def clear_epic_hold_item(self, item_id: str) -> None: ...
    def remove_epic_hold(
        self,
        epic_ref: str,
        item_id: str,
        *,
        fresh_review_cycle_reasons: "Collection[str]" = frozenset(),
        receipt_key: str | None = None,
        receipt_reason_prefixes: "Collection[str]" = (),
        only_reason_prefixes: "Collection[str]" = (),
        keep: "Callable[[EpicHold], bool] | None" = None,
    ) -> "EpicHold | None": ...
    def list_epic_holds(self, epic_ref: str) -> list["EpicHold"]: ...
    def list_all_epic_holds(self) -> list["EpicHold"]: ...
    def epic_hold_node_ids(self, item_id: str) -> tuple[str, ...]: ...
    def sync_orphan_prs(
        self, detected: Sequence[tuple[int, str, str, str]]
    ) -> None: ...
    def list_orphan_prs(
        self, include_acked: bool = False
    ) -> list["OrphanPRRecord"]: ...
    def ack_orphan_pr(self, pr_number: int) -> bool: ...
    def mark_epic_retry(self, epic_ref: str, issue_num: int) -> None: ...
    def mark_epic_retry_item(self, epic_ref: str, item_id: str) -> None: ...
    def mark_epic_retry_item_attested(
        self,
        epic_ref: str,
        item_id: str,
        *,
        audit: Mapping[str, Any],
        precheck: Callable[[], None],
    ) -> None: ...
    def list_epic_retries(self, epic_ref: str) -> list["EpicRetry"]: ...
    def record_review_round(
        self,
        epic_ref: str,
        item_id: str,
        *,
        head_sha: str,
        verdict: str,
        sidecar_digest: str | None = None,
        sidecar_path: str | None = None,
        review_mode: str | None = None,
        plan_sha: str | None = None,
        criteria_json: str | None = None,
        prompt_profile_version_id: int | None = None,
    ) -> int: ...
    def accept_review_round(
        self,
        item_id: str,
        *,
        round_num: int,
        head_sha: str,
        sidecar_digest: str | None,
        verdict: str,
    ) -> bool: ...
    def merge_ready_review_rounds(self, item_id: str) -> list["ReviewRound"]: ...
    def review_round_count(self, item_id: str) -> int: ...
    def latest_review_round(self, item_id: str) -> "ReviewRound | None": ...
    def review_cycle_start(self, item_id: str) -> str | None: ...
    def review_rounds_in_cycle(self, item_id: str) -> list["ReviewRound"]: ...
    def list_review_rounds(self, item_id: str) -> list["ReviewRound"]: ...
    def list_review_rounds_for_epic(self, epic_ref: str) -> list["ReviewRound"]: ...
    def merged_prs_by_week(self, *, since: datetime) -> list["MergedPrWeek"]: ...
    def ready_items_by_week(self, *, since: datetime) -> list["ReadyItemWeek"]: ...
    def spend_by_week(self, *, since: datetime) -> list["SpendWeek"]: ...
    def nodes_done_by_week(self, *, since: datetime) -> dict[str, int]: ...
    def record_item_plan(
        self,
        item_id: str,
        *,
        body_digest: str,
        source: str,
        plan_sha: str,
        plan_json: str,
        criteria_count: int,
        run_id: str | None = None,
        approved_by: str | None = None,
    ) -> "ItemPlanWrite": ...
    def current_item_plan(self, item_id: str) -> "ItemPlanRow | None": ...
    def active_prompt_profile(self, role: str) -> "PromptProfileVersion | None": ...
    def list_item_plans(self, item_id: str) -> list["ItemPlanRow"]: ...
    def record_acceptance_contract(
        self,
        epic_ref: str,
        item_id: str,
        *,
        path: str,
        sha256: str,
        author_run_id: str,
        commit: str,
        plan_sha: str | None = None,
        test_names: tuple[str, ...] = (),
    ) -> "AcceptanceContractRow": ...
    def current_acceptance_contract(
        self, epic_ref: str, item_id: str
    ) -> "AcceptanceContractRow | None": ...
    def current_acceptance_contracts_for_item(
        self, item_id: str
    ) -> list["AcceptanceContractRow"]: ...
    def list_acceptance_contracts(
        self, epic_ref: str, item_id: str
    ) -> list["AcceptanceContractRow"]: ...
    def enqueue_merge(
        self,
        repo: str,
        pr: int,
        head: str,
        *,
        session: str,
        behavior_changes: tuple[str, ...],
    ) -> tuple["MergeQueueEntry", bool]: ...
    def list_merge_queue(
        self, repo: str, *, include_settled: bool = False
    ) -> list["MergeQueueEntry"]: ...
    def settle_merge_entry(
        self,
        entry_id: int,
        state: str,
        reason: str | None,
        *,
        report_pending: bool = False,
    ) -> bool: ...
    def list_pending_merge_reports(self, repo: str) -> list["MergeQueueEntry"]: ...
    def mark_merge_reported(self, entry_id: int) -> bool: ...
    def requeue_merge_entry(
        self, entry_id: int, reason: str, *, count_main_move: bool
    ) -> bool: ...
    def claim_merge_entry(
        self,
        entry_id: int,
        owner: str,
        *,
        expected_base: str,
        expected_tree: str,
    ) -> bool: ...
    def release_merge_entry(self, entry_id: int) -> bool: ...
    def record_unknown_merge(self, entry_id: int, reason: str) -> bool: ...
    def record_landed_merge(
        self, entry_id: int, landed_sha: str, reason: str
    ) -> bool: ...
    def suspect_merge_queue_main(
        self,
        repo: str,
        reason: str,
        *,
        red_on: str | None = None,
        mismatch_on: str | None = None,
        failure: str | None = None,
    ) -> None: ...
    def merge_queue_suspect(self, repo: str) -> tuple[str, str | None] | None: ...
    def list_merge_queue_suspects(self) -> list["MergeQueueSuspect"]: ...
    def clear_merge_queue_suspect(self, repo: str) -> None: ...
    def merge_queue_ledger_lock_path(self) -> Path | None: ...
    def take_merge_claim(
        self,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        now: datetime,
        ttl: timedelta = ...,
    ) -> list["MergeClaim"]: ...
    def renew_merge_claim(
        self,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        now: datetime,
        ttl: timedelta = ...,
    ) -> bool: ...
    def release_merge_claim(
        self, pr: int, heads: Sequence[str], *, owner: str
    ) -> int: ...
    def live_merge_claim(
        self, pr: int, head: str, *, now: datetime
    ) -> "MergeClaim | None": ...
    def list_merge_claims(self) -> list["MergeClaim"]: ...
    def review_items_at_head(self, head_sha: str) -> list[str]: ...
    def record_triage_decision(
        self,
        item_id: str,
        *,
        body_digest: str,
        backend: str,
        model: str,
        prompt_sha: str,
        outcome: str,
        decision_json: str,
        source_tracker: str | None = None,
        issue_num: int | None = None,
        run_id: str | None = None,
        decision_type: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        confidence: float | None = None,
        actionable: bool | None = None,
        needs_info: bool | None = None,
        duplicate_of: int | None = None,
    ) -> "TriageDecisionWrite": ...
    def resolve_triage_decision(
        self, row_id: int, *, resolution: str, resolved_by: str
    ) -> "TriageDecisionRow | None": ...
    def current_triage_decision(self, item_id: str) -> "TriageDecisionRow | None": ...
    def list_triage_decisions(self, item_id: str) -> list["TriageDecisionRow"]: ...
    def list_unresolved_triage_decisions(self) -> list["TriageDecisionRow"]: ...
    def append_epic_tick(
        self,
        *,
        epic_ref: str,
        tick_ts: str,
        snapshot: dict[str, Any],
        actions: dict[str, Any],
        snapshot_hash: str,
        rehash: Callable[[dict[str, Any]], str] | None = None,
    ) -> tuple[int, bool]: ...
    def latest_epic_tick(self, epic_ref: str) -> "EpicTick | None": ...
    def latest_epic_tick_snapshot(self, epic_ref: str) -> dict[str, Any] | None: ...
    def list_epic_ticks(
        self, epic_ref: str, *, after_id: int = 0, limit: int = 1000
    ) -> list["EpicTick"]: ...
    def list_epic_ticks_tail(
        self, epic_ref: str, *, limit: int = 50
    ) -> list["EpicTick"]: ...
    def count_epic_ticks(self, epic_ref: str) -> int: ...
    def begin_epic_tick_log(self, epic_ref: str, ts_started: str) -> int: ...
    def finish_epic_tick_log(
        self,
        row_id: int,
        *,
        ts_finished: str,
        outcome: str,
        signal: str | None = ...,
        n_dispatched: int = ...,
        n_merged: int = ...,
        n_held: int = ...,
        n_stuck: int = ...,
        journal_tick_id: int | None = ...,
        error_class: str | None = ...,
        journaled: bool = ...,
    ) -> None: ...
    def mark_epic_tick_log_reached(self, row_id: int) -> None: ...
    def abandon_open_epic_tick_logs(
        self, epic_ref: str, *, before_id: int, ts_finished: str
    ) -> int: ...
    def list_epic_tick_log_tail(
        self, epic_ref: str, *, limit: int = 200
    ) -> list["EpicTickLogEntry"]: ...
    def list_epic_tick_log_marks(
        self, epic_ref: str, *, limit: int = 200
    ) -> list["EpicTickLogEntry"]: ...
    def epic_tick_log_stamp(self, epic_ref: str) -> str: ...
    def epic_tick_log_stamps(self, refs: Sequence[str]) -> dict[str, str]: ...
    def latest_epic_tick_attempts(
        self, refs: Sequence[str]
    ) -> dict[str, "EpicTickLogEntry"]: ...
    def first_epic_tick_ts(self, epic_ref: str) -> str | None: ...
    def latest_epic_ticks(self) -> dict[str, "EpicTick"]: ...
    def latest_node_pr_facts(
        self,
        *,
        limit: int = ...,
        after: tuple[str, str] | None = ...,
        open_only: bool = ...,
        epic_ref: str | None = ...,
    ) -> list["NodePrFacts"]: ...
    def epic_tick_spans(self) -> dict[str, tuple[int, str | None]]: ...
    def list_epic_driver_states(self) -> dict[str, "EpicDriverState"]: ...
    def sum_costs_by_item_windows(
        self, windows: Collection[tuple[str, str]]
    ) -> dict[tuple[str, str], float]: ...
    def list_epic_refs(self) -> list[str]: ...
    def epic_trackers(self) -> dict[str, str | None]: ...
    def record_epic_tracker(self, epic_ref: str, tracker: str) -> None: ...
    def register_epic(self, epic_ref: str) -> None: ...
    def begin_epic_tick(self, epic_ref: str, started_at: str) -> None: ...
    def finish_epic_tick(
        self,
        epic_ref: str,
        *,
        tick_ts: str,
        duration_s: float,
        interval_s: float | None,
        signal: str,
        tick_id: int | None,
        expected_interval_s: float | None = None,
    ) -> None: ...
    def record_epic_driver_lock(
        self,
        epic_ref: str,
        *,
        pid: int | None,
        pid_alive: bool | None,
        heartbeat_at: str | None,
        observed_at: str,
    ) -> None: ...
    def get_epic_driver_state(self, epic_ref: str) -> "EpicDriverState | None": ...


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    -- NOT NULL (SQLite cannot drop it); ``0`` is the sentinel for a non-numeric
    -- work item, whose durable id lives in ``item_id`` (added by the v24
    -- migration). ``0`` is never a valid GitHub number.
    issue_num INTEGER NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    error_category TEXT NOT NULL DEFAULT '',
    attempt_number INTEGER NOT NULL DEFAULT 1,
    session_id TEXT,
    log_file TEXT,
    pr_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_issue_num ON runs(issue_num);
"""

_SCHEMA_V3 = "ALTER TABLE runs ADD COLUMN model TEXT NOT NULL DEFAULT ''"

_SCHEMA_V4 = "ALTER TABLE runs ADD COLUMN title TEXT NOT NULL DEFAULT ''"

_SCHEMA_V5 = (
    "CREATE INDEX IF NOT EXISTS idx_runs_status_started ON runs(status, started_at)"
)

_SCHEMA_V6 = "ALTER TABLE runs ADD COLUMN complexity TEXT NOT NULL DEFAULT ''"

_SCHEMA_V7_PGID = "ALTER TABLE runs ADD COLUMN pgid INTEGER"

_SCHEMA_V7_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

_SCHEMA_V8_RUNS = [
    "ALTER TABLE runs ADD COLUMN actor_type TEXT",
    "ALTER TABLE runs ADD COLUMN actor_id TEXT",
    "ALTER TABLE runs ADD COLUMN repo TEXT",
    "ALTER TABLE runs ADD COLUMN endpoint TEXT",
    "ALTER TABLE runs ADD COLUMN policy_hash TEXT",
    "ALTER TABLE runs ADD COLUMN pr_state TEXT",
    "ALTER TABLE runs ADD COLUMN pr_merged_at TEXT",
    "ALTER TABLE runs ADD COLUMN review_cycles INTEGER",
    "ALTER TABLE runs ADD COLUMN diff_additions INTEGER",
    "ALTER TABLE runs ADD COLUMN diff_deletions INTEGER",
    "ALTER TABLE runs ADD COLUMN diff_files INTEGER",
]

_SCHEMA_V8_AUDIT_EVENTS = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    run_id TEXT,
    issue_num INTEGER,
    repo TEXT,
    actor_type TEXT,
    actor_id TEXT,
    endpoint TEXT,
    policy_hash TEXT,
    prev_hash TEXT,
    payload TEXT
)
"""

_SCHEMA_V8_AUDIT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_events_event_ts"
    " ON audit_events(event, timestamp)"
)

_SCHEMA_V9 = [
    """
    CREATE TABLE IF NOT EXISTS loops (
        loop_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        issue_num INTEGER,
        repo TEXT,
        goal_text TEXT NOT NULL,
        runner_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pursuing',
        status_reason TEXT NOT NULL DEFAULT '',
        budget_usd REAL,
        wall_clock_secs INTEGER NOT NULL DEFAULT 1800,
        max_steps INTEGER NOT NULL DEFAULT 200,
        spent_usd REAL NOT NULL DEFAULT 0.0,
        step_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_loops_status ON loops(status)",
    "CREATE INDEX IF NOT EXISTS idx_loops_issue_num ON loops(issue_num)",
    """
    CREATE TABLE IF NOT EXISTS iterations (
        iteration_id TEXT PRIMARY KEY,
        loop_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        run_id TEXT,
        kind TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        cost_usd REAL NOT NULL DEFAULT 0.0,
        tokens INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_iterations_loop_id ON iterations(loop_id)",
]

_SCHEMA_V11 = [
    "ALTER TABLE runs ADD COLUMN cache_creation_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
]

_SCHEMA_V22 = "ALTER TABLE runs ADD COLUMN container_name TEXT"

# V25 (L3): re-key the epic tables on ``item_id``. Kept as per-table literal
# constants (never f-string-interpolated) so bandit's B608 stays quiet — the
# table names are a fixed allowlist, not caller input.
#: Where the full node-id list one epic hold was written with lives, keyed by
#: the held item (#2884): a ``test_declarations_missing`` reason is capped and
#: ends ``+N more``. Written and deleted only with the hold row itself.
_EPIC_HOLD_NODE_IDS_META_PREFIX = "epic_hold_node_ids:"


_EPIC_ITEM_ID_ALTER = {
    "epic_reservations": "ALTER TABLE epic_reservations ADD COLUMN item_id TEXT",
    "epic_holds": "ALTER TABLE epic_holds ADD COLUMN item_id TEXT",
    "epic_retries": "ALTER TABLE epic_retries ADD COLUMN item_id TEXT",
}
_EPIC_ITEM_ID_BACKFILL = {
    "epic_reservations": (
        "UPDATE epic_reservations SET item_id = CAST(issue_num AS TEXT)"
        " WHERE item_id IS NULL"
    ),
    "epic_holds": (
        "UPDATE epic_holds SET item_id = CAST(issue_num AS TEXT) WHERE item_id IS NULL"
    ),
    "epic_retries": (
        "UPDATE epic_retries SET item_id = CAST(issue_num AS TEXT)"
        " WHERE item_id IS NULL"
    ),
}
_EPIC_ITEM_ID_INDEX = {
    "epic_reservations": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_epic_res_item"
        " ON epic_reservations(item_id)"
    ),
    "epic_holds": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_epic_holds_item ON epic_holds(item_id)"
    ),
    "epic_retries": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_epic_retries_item"
        " ON epic_retries(item_id)"
    ),
}

_UPSERT_SQL = """
INSERT INTO runs (
    run_id, issue_num, branch, status, exit_code, started_at,
    finished_at, duration_seconds, total_tokens, input_tokens,
    output_tokens, cache_creation_tokens, cache_read_tokens,
    estimated_cost_usd, error_category,
    attempt_number, session_id, log_file, pr_url, runner_name, model, title,
    complexity, pgid, actor_type, actor_id, repo, endpoint, policy_hash,
    reported_model,
    pr_state, pr_merged_at, review_cycles, diff_additions, diff_deletions,
    diff_files, prefix_key, provider, container_name, item_id, run_kind,
    steps_to_first_edit, tokens_to_first_edit, map_tokens, lesson_corpus_tokens,
    planned_cost_usd, transport_mode, transport_endpoint, transport_route,
    transport_managed, transport_expected_version, transport_observed_version,
    transport_config_sha256, transport_preflight_verdict,
    resolved_model_source, selected_tier, requested_service_tier,
    reported_service_tier, classifier_overhead_model,
    classifier_overhead_input_tokens, classifier_overhead_output_tokens,
    classifier_overhead_cost_usd, switchyard_proxy_observation,
    accounting_status, attempt_pgid, owner_pid, attempt_start_time, owner_start_time,
    attempt_owner_verified, termination_members, container_id, container_cidfile,
    owner_ppid, owner_command,
    container_runtime, cleanup_runtime, owner_start_token, attempt_start_token,
    pre_admission_refusal, self_check, run_host, run_host_instance, owner_boot_id,
    prompt_profile_version_id
) VALUES (
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(run_id) DO UPDATE SET
    issue_num=excluded.issue_num,
    branch=excluded.branch,
    status=CASE WHEN runs.status = 'killed' OR
        (runs.error_category = 'killed' AND excluded.status != 'killed')
        THEN runs.status ELSE excluded.status END,
    exit_code=CASE WHEN runs.status = 'killed' OR
        (runs.error_category = 'killed' AND excluded.status != 'killed')
        THEN runs.exit_code ELSE excluded.exit_code END,
    started_at=excluded.started_at,
    finished_at=CASE WHEN runs.status = 'killed' OR
        (runs.error_category = 'killed' AND excluded.status != 'killed')
        THEN runs.finished_at ELSE excluded.finished_at END,
    duration_seconds=excluded.duration_seconds,
    total_tokens=excluded.total_tokens,
    input_tokens=excluded.input_tokens,
    output_tokens=excluded.output_tokens,
    cache_creation_tokens=excluded.cache_creation_tokens,
    cache_read_tokens=excluded.cache_read_tokens,
    estimated_cost_usd=excluded.estimated_cost_usd,
    error_category=CASE WHEN runs.error_category = 'killed' OR runs.status = 'killed'
        THEN runs.error_category ELSE excluded.error_category END,
    attempt_number=MAX(runs.attempt_number, excluded.attempt_number),
    session_id=excluded.session_id,
    log_file=excluded.log_file,
    pr_url=excluded.pr_url,
    runner_name=excluded.runner_name,
    model=excluded.model,
    title=excluded.title,
    complexity=excluded.complexity,
    pgid=runs.pgid,
    actor_type=excluded.actor_type,
    actor_id=excluded.actor_id,
    repo=excluded.repo,
    endpoint=excluded.endpoint,
    policy_hash=excluded.policy_hash,
    reported_model=excluded.reported_model,
    pr_state=excluded.pr_state,
    pr_merged_at=excluded.pr_merged_at,
    review_cycles=excluded.review_cycles,
    diff_additions=excluded.diff_additions,
    diff_deletions=excluded.diff_deletions,
    diff_files=excluded.diff_files,
    prefix_key=excluded.prefix_key,
    provider=excluded.provider,
    container_name=runs.container_name,
    run_host=runs.run_host,
    run_host_instance=runs.run_host_instance,
    container_id=runs.container_id,
    container_cidfile=runs.container_cidfile,
    item_id=excluded.item_id,
    run_kind=excluded.run_kind,
    steps_to_first_edit=excluded.steps_to_first_edit,
    tokens_to_first_edit=excluded.tokens_to_first_edit,
    map_tokens=excluded.map_tokens,
    lesson_corpus_tokens=excluded.lesson_corpus_tokens,
    planned_cost_usd=excluded.planned_cost_usd,
    resolved_model_source=excluded.resolved_model_source,
    selected_tier=excluded.selected_tier,
    requested_service_tier=excluded.requested_service_tier,
    reported_service_tier=excluded.reported_service_tier,
    classifier_overhead_model=excluded.classifier_overhead_model,
    classifier_overhead_input_tokens=excluded.classifier_overhead_input_tokens,
    classifier_overhead_output_tokens=excluded.classifier_overhead_output_tokens,
    classifier_overhead_cost_usd=excluded.classifier_overhead_cost_usd,
    switchyard_proxy_observation=excluded.switchyard_proxy_observation,
    pre_admission_refusal=excluded.pre_admission_refusal,
    self_check=excluded.self_check,
    prompt_profile_version_id=COALESCE(
        runs.prompt_profile_version_id, excluded.prompt_profile_version_id),
    accounting_status=excluded.accounting_status,
    attempt_pgid=runs.attempt_pgid,
    owner_pid=runs.owner_pid,
    attempt_start_time=runs.attempt_start_time,
    owner_start_time=runs.owner_start_time,
    owner_start_token=runs.owner_start_token,
    owner_boot_id=runs.owner_boot_id,
    attempt_start_token=runs.attempt_start_token,
    owner_ppid=runs.owner_ppid,
    owner_command=runs.owner_command,
    container_runtime=runs.container_runtime,
    cleanup_runtime=runs.cleanup_runtime,
    attempt_owner_verified=runs.attempt_owner_verified,
    termination_members=json_patch(
        runs.termination_members, excluded.termination_members)
"""

_LOOP_UPSERT_SQL = """
INSERT INTO loops (
    loop_id, source, issue_num, repo, goal_text, runner_name, status,
    status_reason, budget_usd, wall_clock_secs, max_steps, spent_usd,
    step_count, created_at, updated_at, finished_at, pause_requested, item_id,
    model, complexity
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(loop_id) DO UPDATE SET
    source=excluded.source,
    issue_num=excluded.issue_num,
    item_id=excluded.item_id,
    repo=excluded.repo,
    goal_text=excluded.goal_text,
    runner_name=excluded.runner_name,
    status=excluded.status,
    status_reason=excluded.status_reason,
    budget_usd=excluded.budget_usd,
    wall_clock_secs=excluded.wall_clock_secs,
    max_steps=excluded.max_steps,
    spent_usd=excluded.spent_usd,
    step_count=excluded.step_count,
    updated_at=excluded.updated_at,
    finished_at=excluded.finished_at,
    pause_requested=excluded.pause_requested,
    model=excluded.model,
    complexity=excluded.complexity
"""


def _stale_host_is_dead(record: RunRecord) -> bool:
    """The age-gated sweep may prove a birthless legacy group completely absent."""
    if record.owner_start_time is None and record.owner_start_token is None:
        return (
            record.pgid is not None
            and group_absent(record.pgid)
            and attempts_are_dead(record.attempt_pgid, record.log_file)
        )
    return host_stop_is_dead(
        record.pgid,
        record.owner_pid,
        record.attempt_pgid,
        record.log_file,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
    )


def _dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Datetime values must be timezone-aware")
    return dt.astimezone(UTC).isoformat()


def _iso_to_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Backward-compat for existing rows written without tz info.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _tick_is_stale(last_tick_ts: str | None, cutoff: datetime) -> bool:
    """Whether a driver-state row's last tick predates ``cutoff`` (#2094).

    Fails OPEN — a NULL (never ticked) or unparseable timestamp counts as fresh,
    so a malformed row keeps its epic contending rather than silently handing
    its fair share to the other epics.
    """
    if last_tick_ts is None:
        return False
    try:
        parsed = _iso_to_dt(last_tick_ts)
    except ValueError:
        _logger.warning("epic driver tick timestamp %r is not ISO-8601", last_tick_ts)
        return False
    return parsed is not None and parsed < cutoff


def _epic_drive_silence_s(
    last_tick_ts: str | None, tick_ts: str, duration_s: float
) -> float | None:
    """Seconds of quiet between the previous drive's end and this drive's start.

    The start is derived (``tick_ts`` minus this drive's ``duration_s``) rather
    than read off the row, because ``begin_epic_tick`` has already stamped
    ``last_tick_started_at`` with THIS drive's start.

    Fails OPEN like :func:`_tick_is_stale`: no previous drive, or a stamp that
    is not ISO-8601, returns None — "we cannot tell" — and the one caller
    (:meth:`SqliteRunStore._surviving_recorded_cadence`) then changes nothing.
    A stamp is tolerated, never repaired (#2297 review round 2).
    """
    if last_tick_ts is None:
        return None
    try:
        previous = _iso_to_dt(last_tick_ts)
        ended = _iso_to_dt(tick_ts)
    except ValueError:
        _logger.warning(
            "epic driver tick timestamps %r/%r are not ISO-8601; "
            "the recorded cadence is left as it stands",
            last_tick_ts,
            tick_ts,
        )
        return None
    if previous is None or ended is None:
        return None
    return (ended - previous).total_seconds() - duration_s


def _canonical_worktree_identity(worktree: str | Path) -> str:
    """Normalize a worktree path before it becomes durable identity."""
    return str(Path(worktree).expanduser().resolve(strict=False))


def continuation_checkout_matches(
    recorded: str | Path,
    checkout: str | Path,
    *,
    recorded_owner: tuple[str, str] | None = None,
    candidate_owner: tuple[str, str] | None = None,
) -> bool:
    """Compare filesystem identity, retaining unverifiable owners for admission.

    Normalize missing paths at read time; schema v50 and evidence stay unchanged.
    Admission retains missing paths only for the candidate's item and branch.
    Replacement requires abandonment and explicit missing-path rebind evidence.
    Call admission under the checkout claim, before changing the directory.
    """
    recorded = os.path.realpath(Path(recorded).expanduser())
    checkout = os.path.realpath(Path(checkout).expanduser())
    normalized_match = os.path.normcase(recorded) == os.path.normcase(checkout)
    try:
        recorded_stat = os.stat(recorded)
    except OSError:
        if candidate_owner is not None:
            return recorded_owner == candidate_owner
        return normalized_match
    try:
        return os.path.samestat(recorded_stat, os.stat(checkout))
    except OSError:
        return normalized_match


def _run_duration_seconds(row: sqlite3.Row) -> float:
    """Return a row's duration, backfilling from timestamps when missing.

    Rows may lack ``duration_seconds`` (e.g. stale pending rows cleaned up
    by ``cleanup_stale_pending``); fall back to finished_at - started_at.
    """
    duration = row["duration_seconds"] or 0.0
    if duration or not (row["started_at"] and row["finished_at"]):
        return float(duration)
    try:
        started = _iso_to_dt(row["started_at"])
        finished = _iso_to_dt(row["finished_at"])
    except (ValueError, TypeError):
        return 0.0
    if started and finished:
        return (finished - started).total_seconds()
    return 0.0


def _finalize_bucket(bucket: dict[str, float | int]) -> None:
    """Replace private accumulators with averages and add success_rate."""
    n = bucket["total"]
    if "_dur" in bucket:
        bucket["avg_duration_seconds"] = bucket.pop("_dur") / n if n else 0.0
    bucket["avg_cost_usd"] = bucket.pop("_cost") / n if n else 0.0
    bucket["success_rate"] = bucket["success"] / n if n else 0.0


def _aggregate_by(
    rows: list[sqlite3.Row],
    *,
    key_column: str,
    include_duration: bool = False,
) -> dict[str, dict[str, float | int]]:
    """Group rows by *key_column*, accumulating success/cost (and duration)."""
    buckets: dict[str, dict[str, float | int]] = {}
    for row in rows:
        key = row[key_column] or "unknown"
        bucket = buckets.setdefault(key, {"total": 0, "success": 0, "_cost": 0.0})
        bucket["total"] += 1
        bucket["success"] += int(row["status"] == "success")
        bucket["_cost"] += row["estimated_cost_usd"] or 0.0
        if include_duration:
            bucket["_dur"] = bucket.get("_dur", 0.0) + _run_duration_seconds(row)
    for bucket in buckets.values():
        _finalize_bucket(bucket)
    return buckets


def _count_failure_categories(rows: list[sqlite3.Row]) -> dict[str, int]:
    """Count error_category occurrences among non-successful rows."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["status"] != "success" and row["error_category"]:
            cat = row["error_category"]
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def _col(row: sqlite3.Row, keys: "list[str]", name: str) -> Any:
    """Defensive column read: None when the column predates this schema."""
    return row[name] if name in keys else None


def _col_or(row: sqlite3.Row, keys: "list[str]", name: str, default: Any) -> Any:
    """``_col`` with a default that also covers a NULL or zero-valued column."""
    return _col(row, keys, name) or default


def _decode_termination_members(raw: str) -> dict[int, dict[int, str]]:
    """Restore integer identifiers from trusted database snapshot JSON."""
    return {
        int(group): {int(pid): birth for pid, birth in members.items()}
        for group, members in json.loads(raw).items()
    }


def _attempt_owner_verified(
    record: RunRecord | None, pgid: int, birth: str | None, token: str | None
) -> bool:
    """Require both kernel identities before retaining attempt signal authority."""
    return bool(
        record is not None
        and record.owner_start_token is not None
        and token is not None
        and record.owner_pid is not None
        and record.owner_pid == record.pgid
        and owner_alive(
            record.owner_pid,
            record.owner_pid,
            record.owner_start_time,
            record.owner_ppid,
            record.owner_command,
            record.owner_start_token,
        )
        is True
        and attempt_descends_from(pgid, record.owner_pid)
        and birth_matches(pgid, birth)
        and token == process_start_token(pgid)
    )


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    started_at = _iso_to_dt(row["started_at"])
    assert started_at is not None, "started_at cannot be NULL"
    keys = row.keys()
    return RunRecord(
        run_id=row["run_id"],
        issue_num=row["issue_num"],
        branch=row["branch"],
        status=row["status"],
        exit_code=row["exit_code"],
        started_at=started_at,
        finished_at=_iso_to_dt(row["finished_at"]),
        duration_seconds=row["duration_seconds"],
        total_tokens=row["total_tokens"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_creation_tokens=_col_or(row, keys, "cache_creation_tokens", 0),
        cache_read_tokens=_col_or(row, keys, "cache_read_tokens", 0),
        estimated_cost_usd=row["estimated_cost_usd"],
        error_category=row["error_category"],
        pre_admission_refusal=bool(_col(row, keys, "pre_admission_refusal")),
        self_check=bool(_col(row, keys, "self_check")),
        run_host=_col_or(row, keys, "run_host", LOCAL_RUN_HOST) or LOCAL_RUN_HOST,
        run_host_instance=_col_or(row, keys, "run_host_instance", None),
        prompt_profile_version_id=_col_or(row, keys, "prompt_profile_version_id", None),
        attempt_number=row["attempt_number"],
        session_id=row["session_id"],
        log_file=Path(row["log_file"]) if row["log_file"] else None,
        pr_url=row["pr_url"],
        runner_name=row["runner_name"],
        model=row["model"],
        title=row["title"],
        complexity=row["complexity"] if "complexity" in keys else "",
        pgid=row["pgid"] if "pgid" in keys else None,
        attempt_pgid=row["attempt_pgid"] if "attempt_pgid" in keys else None,
        owner_pid=row["owner_pid"] if "owner_pid" in keys else None,
        attempt_start_time=row["attempt_start_time"]
        if "attempt_start_time" in keys
        else None,
        owner_start_time=row["owner_start_time"]
        if "owner_start_time" in keys
        else None,
        attempt_owner_verified=bool(_col_or(row, keys, "attempt_owner_verified", 0)),
        owner_ppid=_col_or(row, keys, "owner_ppid", None),
        owner_start_token=_col_or(row, keys, "owner_start_token", None),
        owner_boot_id=_col_or(row, keys, "owner_boot_id", None),
        attempt_start_token=_col_or(row, keys, "attempt_start_token", None),
        owner_command=_col_or(row, keys, "owner_command", None),
        container_runtime=_col_or(row, keys, "container_runtime", None),
        cleanup_runtime=_col_or(row, keys, "cleanup_runtime", None),
        termination_members=_decode_termination_members(
            _col_or(row, keys, "termination_members", "{}")
        ),
        actor_type=_col(row, keys, "actor_type"),
        actor_id=_col(row, keys, "actor_id"),
        repo=_col(row, keys, "repo"),
        endpoint=_col(row, keys, "endpoint"),
        policy_hash=_col(row, keys, "policy_hash"),
        reported_model=_col(row, keys, "reported_model"),
        resolved_model_source=_col(row, keys, "resolved_model_source"),
        selected_tier=_col(row, keys, "selected_tier"),
        requested_service_tier=_col(row, keys, "requested_service_tier"),
        reported_service_tier=_col(row, keys, "reported_service_tier"),
        classifier_overhead_model=_col(row, keys, "classifier_overhead_model"),
        classifier_overhead_input_tokens=_col_or(
            row, keys, "classifier_overhead_input_tokens", 0
        ),
        classifier_overhead_output_tokens=_col_or(
            row, keys, "classifier_overhead_output_tokens", 0
        ),
        classifier_overhead_cost_usd=_col_or(
            row, keys, "classifier_overhead_cost_usd", 0.0
        ),
        switchyard_proxy_observation=_col(row, keys, "switchyard_proxy_observation"),
        accounting_status=_col(row, keys, "accounting_status"),
        pr_state=_col(row, keys, "pr_state"),
        pr_merged_at=_col(row, keys, "pr_merged_at"),
        review_cycles=_col(row, keys, "review_cycles"),
        diff_additions=_col(row, keys, "diff_additions"),
        diff_deletions=_col(row, keys, "diff_deletions"),
        diff_files=_col(row, keys, "diff_files"),
        prefix_key=_col(row, keys, "prefix_key"),
        provider=_col(row, keys, "provider"),
        container_name=_col(row, keys, "container_name"),
        container_id=_col(row, keys, "container_id"),
        container_cidfile=_col(row, keys, "container_cidfile"),
        item_id=_col(row, keys, "item_id"),
        run_kind=_col_or(row, keys, "run_kind", "dispatch"),
        steps_to_first_edit=_col(row, keys, "steps_to_first_edit"),
        tokens_to_first_edit=_col(row, keys, "tokens_to_first_edit"),
        map_tokens=_col(row, keys, "map_tokens"),
        lesson_corpus_tokens=_col(row, keys, "lesson_corpus_tokens"),
        planned_cost_usd=_col(row, keys, "planned_cost_usd"),
        transport_mode=_col_or(row, keys, "transport_mode", "direct"),
        transport_endpoint=_col(row, keys, "transport_endpoint"),
        transport_route=_col(row, keys, "transport_route"),
        transport_managed=bool(_col(row, keys, "transport_managed")),
        transport_expected_version=_col(row, keys, "transport_expected_version"),
        transport_observed_version=_col(row, keys, "transport_observed_version"),
        transport_config_sha256=_col(row, keys, "transport_config_sha256"),
        transport_preflight_verdict=_col(row, keys, "transport_preflight_verdict"),
    )


def _event_prefix_bounds(prefix: str) -> tuple[str, str | None]:
    """Half-open ``[low, high)`` bounds matching exactly the strings that start
    with *prefix* under SQLite's default BINARY collation.

    Used instead of ``LIKE ? ESCAPE`` because an ESCAPE clause disables SQLite's
    LIKE optimization outright: the prefix read full-scanned ``audit_events``
    where a range seeks ``idx_audit_events_event_ts`` (#2129). A range is also
    an exact prefix test, where LIKE's default is case-insensitive.

    BINARY compares UTF-8 bytes, an order that agrees with code-point order, so
    the bound is the prefix with its last incrementable code point bumped by
    one. ``high`` is ``None`` only for a prefix of nothing but ``U+10FFFF``,
    where ``event >= prefix`` is already exact on its own.
    """
    for i in reversed(range(len(prefix))):
        nxt = ord(prefix[i]) + 1
        if nxt == 0xD800:  # lone surrogates are unstorable — skip the block
            nxt = 0xE000
        if nxt <= 0x10FFFF:
            return prefix, prefix[:i] + chr(nxt)
    return prefix, None


def _or_group(parts: list[str]) -> list[str]:
    """*parts* as a single parenthesized OR clause, or nothing when empty.

    One clause, because the caller AND-joins what it gets back: an unbracketed
    ``a OR b`` would bind looser than that AND and silently widen the read.
    """
    if not parts:
        return []
    return [parts[0] if len(parts) == 1 else f"({' OR '.join(parts)})"]


def _in_clause(column: str, values: Sequence[Any]) -> str:
    """``column IN (?, ?, …)`` — only placeholders are interpolated."""
    return f"{column} IN ({','.join('?' * len(values))})"


#: A clause that matches no row, for a scope filter that names nothing.
_NO_ROWS = "1 = 0"


def _scope_names_nothing(*scopes: Sequence[Any] | None) -> bool:
    """Does any given scope filter name nothing at all?

    ``None`` is unscoped and says nothing either way; an EMPTY sequence is a
    caller stating that no item is in scope, and the answer to that is no rows
    rather than every row.
    """
    return any(scope is not None and not list(scope) for scope in scopes)


def _as_issue_num(value: str) -> int | None:
    """*value* as an issue number, or None when it is not one (``SFD-232``).

    ``isascii()`` as well as ``isdigit()``: the latter is true of superscripts
    and other unicode digit forms that ``int()`` then refuses, and the value
    reaches here straight off a query param.

    Anything past :data:`MAX_ISSUE_NUM_DIGITS` digits is None too. What comes
    back is bound as a parameter, and sqlite3 refuses an int wider than int64
    (``OverflowError``) — a 500 on ``item=``/``epic=`` for a value that is no
    issue number anyway, so it drops out with the non-numeric ids instead.
    """
    if value.isascii() and value.isdigit() and len(value) <= MAX_ISSUE_NUM_DIGITS:
        return int(value)
    return None


def _audit_prefix_filters(prefixes: Sequence[str]) -> tuple[list[str], list[Any]]:
    """An OR of index-served prefix ranges on ``event`` — never a LIKE (#2129).

    Each prefix becomes its own half-open range (:func:`_event_prefix_bounds`);
    SQLite's OR optimization can still serve the union from
    ``idx_audit_events_event_ts`` as one index scan per arm.
    """
    parts: list[str] = []
    params: list[Any] = []
    for prefix in dict.fromkeys(p for p in prefixes if p):
        low, high = _event_prefix_bounds(prefix)
        if high is None:
            parts.append("event >= ?")
            params.append(low)
        else:
            parts.append("(event >= ? AND event < ?)")
            params.extend((low, high))
    return _or_group(parts), params


def _audit_item_filters(item_or_issue: str | None) -> tuple[list[str], list[Any]]:
    """One durable work item: the ``item_id`` column, or ``issue_num`` when the
    value is numeric — the two keys the same item can be recorded under."""
    if not item_or_issue:
        return [], []
    parts = ["item_id = ?"]
    params: list[Any] = [item_or_issue]
    num = _as_issue_num(item_or_issue)
    if num is not None:
        parts.append("issue_num = ?")
        params.append(num)
    return _or_group(parts), params


def _node_id_strings(node_ids: Sequence[str] | None) -> list[str]:
    """Snapshot node ids as non-empty strings, duplicates dropped."""
    return [s for s in (str(n) for n in dict.fromkeys(node_ids or ())) if s]


def _audit_node_id_filters(
    node_ids: Sequence[str] | None,
) -> tuple[list[str], list[Any]]:
    """``IN`` arms for the two key columns a node id can be recorded under.

    A numeric node id is an issue number on the classic GitHub path and the
    ``item_id`` on a durable-id tracker, so it goes into both arms.
    """
    ids = _node_id_strings(node_ids)
    nums = [n for n in (_as_issue_num(i) for i in ids) if n is not None]
    parts: list[str] = []
    params: list[Any] = []
    for column, values in (("issue_num", nums), ("item_id", ids)):
        if values:
            parts.append(_in_clause(column, values))
            params.extend(values)
    return parts, params


def _audit_item_scope(item_ids: Sequence[str] | None) -> tuple[list[str], list[Any]]:
    """``item_ids=`` — a durable-id scope, as index-served ``IN`` arms.

    The sibling of ``issue_nums`` for a tracker whose ids are not numbers, and
    a scope rather than a second rule for the same reason (#2129): a caller
    holding a handful of nodes — an epic's unsettled ones, read on a 1s poll
    (#2392) — must not have to parse every event of its kind ever appended.
    ``idx_audit_events_item_event`` serves the ``item_id`` arm; the
    ``issue_num`` one catches a pre-v24 row that has no ``item_id`` at all.

    ``None`` means unscoped. An EMPTY sequence means nothing is in scope, which
    the caller short-circuits to no rows.
    """
    if item_ids is None:
        return [], []
    parts, params = _audit_node_id_filters(item_ids)
    # Never silently unscoped: an id set that normalizes away is still a scope
    # naming nothing, and dropping the clause would widen the read to every row.
    return (_or_group(parts), params) if parts else ([_NO_ROWS], [])


def _audit_epic_filters(
    epic_ref: str | None, node_ids: Sequence[str] | None
) -> tuple[list[str], list[Any]]:
    """One epic's reach: events keyed to its nodes OR its epic-level events.

    *node_ids* are the epic's latest-snapshot node ids, matched against both
    key columns (numeric ones also against ``issue_num``). The third arm
    catches an epic-level event, which carries no node id of its own and is
    attributable only through ``$.epic_ref`` in its payload.

    That arm is UNINDEXED: ``json_extract`` cannot be served by an index, so
    an epic read falls back to a scan of whatever the other filters leave. A
    ``since``/``until`` window bounds it to that window — the histogram always
    passes one; the event feed does not require one, which on a ~10k-row
    ledger is a full scan of a small table rather than a problem.
    """
    if not epic_ref:
        return [], []
    parts, params = _audit_node_id_filters(node_ids)
    parts.append("(json_valid(payload) AND json_extract(payload, '$.epic_ref') = ?)")
    params.append(epic_ref)
    return _or_group(parts), params


def _audit_window_filters(
    since: datetime | None, until: datetime | None
) -> tuple[list[str], list[Any]]:
    """The time bound on a page: ``since`` inclusive, ``until`` EXCLUSIVE, so
    adjacent windows tile the log without double-counting the boundary row."""
    clauses: list[str] = []
    params: list[Any] = []
    for clause, value in (("timestamp >= ?", since), ("timestamp < ?", until)):
        if value is not None:
            clauses.append(clause)
            params.append(_dt_to_iso(value))
    return clauses, params


def _audit_scope_filters(
    event_prefixes: Sequence[str] | None,
    issue_nums: list[int] | None,
    *,
    item_or_issue: str | None = None,
    epic_ref: str | None = None,
    epic_node_ids: Sequence[str] | None = None,
    item_ids: Sequence[str] | None = None,
) -> tuple[list[str], list[Any]]:
    """The narrowing filters of :meth:`SqliteRunStore.list_audit_events`, as
    index-served comparisons: an OR of half-open ranges on ``event`` (never a
    LIKE — see :func:`_event_prefix_bounds`), an ``IN`` over the issue scope
    (#2129) or the durable-id scope (:func:`_audit_item_scope`), and the
    ``item=``/``epic=`` disjunctions."""
    issue_scope = (
        ([], [])
        if issue_nums is None
        else ([_in_clause("issue_num", issue_nums)], list(issue_nums))
    )
    clauses: list[str] = []
    params: list[Any] = []
    for group, vals in (
        _audit_prefix_filters(event_prefixes or ()),
        issue_scope,
        _audit_item_scope(item_ids),
        _audit_item_filters(item_or_issue),
        _audit_epic_filters(epic_ref, epic_node_ids),
    ):
        clauses.extend(group)
        params.extend(vals)
    return clauses, params


# Every equality filter the audit reads share, as (clause, keyword) pairs.
_AUDIT_EQ_FILTERS = (
    ("event = ?", "event"),
    ("item_id = ?", "item_id"),
    ("actor_type = ?", "actor_type"),
    ("issue_num = ?", "issue_num"),
    ("run_id = ?", "run_id"),
    # A loop stamps its id inside the payload (there has never been a column
    # for it), so the loop detail page used to read every audit row ever
    # appended and filter in Python. The JSON arm is unindexed, which is why
    # the caller must also bound the read (C3/#2296).
    ("json_valid(payload) AND json_extract(payload, '$.loop_id') = ?", "loop_id"),
)


def _audit_where(filters: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """The WHERE clauses and bound params shared by the page read and the
    histogram, so the two can never disagree about what a filter means.

    Every clause is a hardcoded literal; every value is a bound parameter.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for clause, key in _AUDIT_EQ_FILTERS:
        val = filters.get(key)
        if val is not None:
            clauses.append(clause)
            params.append(val)
    prefixes = list(filters.get("event_prefixes") or ())
    single = filters.get("event_prefix")
    if single:
        prefixes.append(single)
    nums = filters.get("issue_nums")
    ids = filters.get("item_ids")
    scope_clauses, scope_params = _audit_scope_filters(
        prefixes,
        None if nums is None else list(dict.fromkeys(nums)),
        item_or_issue=filters.get("item_or_issue"),
        epic_ref=filters.get("epic_ref"),
        epic_node_ids=filters.get("epic_node_ids"),
        item_ids=None if ids is None else list(dict.fromkeys(ids)),
    )
    clauses.extend(scope_clauses)
    params.extend(scope_params)
    window_clauses, window_params = _audit_window_filters(
        filters.get("since"), filters.get("until")
    )
    clauses.extend(window_clauses)
    params.extend(window_params)
    return clauses, params


def _optional_bool(raw: Any) -> bool | None:
    """A nullable SQLite boolean: None stays None, 0/1 become False/True.

    Not ``bool(raw)``: that reads a NULL (nothing was ever observed) as False,
    and for ``lock_pid_alive`` False means "this pid is PROVABLY dead" — a
    conclusive gone verdict, invented from an absence (#2297 review).
    """
    return None if raw is None else bool(raw)


def _row_to_epic_driver_state(row: sqlite3.Row) -> "EpicDriverState":
    """One ``epic_driver_state`` row, shared by the point read and the roster."""
    # A read-only open of a pre-v33/pre-v62/pre-v63 database has no ``tracker``
    # / ``expected_interval_s`` / lock-snapshot column; the row is tolerated and
    # read as "unknown" for the missing fact, never repaired.
    columns = row.keys()
    return EpicDriverState(
        epic_ref=row["epic_ref"],
        last_tick_started_at=row["last_tick_started_at"],
        last_tick_ts=row["last_tick_ts"],
        last_tick_duration_s=row["last_tick_duration_s"],
        tick_count=int(row["tick_count"]),
        interval_s=row["interval_s"],
        last_signal=row["last_signal"],
        last_tick_id=row["last_tick_id"],
        tracker=row["tracker"] if "tracker" in columns else None,
        expected_interval_s=(
            row["expected_interval_s"] if "expected_interval_s" in columns else None
        ),
        lock_pid=row["lock_pid"] if "lock_pid" in columns else None,
        lock_pid_alive=_optional_bool(
            row["lock_pid_alive"] if "lock_pid_alive" in columns else None
        ),
        lock_heartbeat_at=(
            row["lock_heartbeat_at"] if "lock_heartbeat_at" in columns else None
        ),
        lock_observed_at=(
            row["lock_observed_at"] if "lock_observed_at" in columns else None
        ),
    )


def _row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    payload: dict[str, Any] | None = None
    raw = row["payload"]
    if raw:
        try:
            parsed = json.loads(raw)
            payload = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            payload = None
    return AuditEvent(
        id=row["id"],
        timestamp=row["timestamp"],
        event=row["event"],
        run_id=row["run_id"],
        issue_num=row["issue_num"],
        item_id=_col(row, row.keys(), "item_id"),
        repo=row["repo"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        endpoint=row["endpoint"],
        policy_hash=row["policy_hash"],
        prev_hash=row["prev_hash"],
        payload=payload,
        dedup_key=_col(row, row.keys(), "dedup_key"),
    )


def _row_to_loop(row: sqlite3.Row) -> LoopRecord:
    created_at = _iso_to_dt(row["created_at"])
    assert created_at is not None, "created_at cannot be NULL"
    keys = row.keys()
    return LoopRecord(
        loop_id=row["loop_id"],
        source=row["source"],
        goal_text=row["goal_text"],
        runner_name=row["runner_name"],
        created_at=created_at,
        issue_num=row["issue_num"],
        repo=row["repo"],
        status=row["status"],
        status_reason=row["status_reason"],
        budget_usd=row["budget_usd"],
        wall_clock_secs=row["wall_clock_secs"],
        max_steps=row["max_steps"],
        spent_usd=row["spent_usd"],
        step_count=row["step_count"],
        updated_at=_iso_to_dt(row["updated_at"]),
        finished_at=_iso_to_dt(row["finished_at"]),
        pause_requested=bool(row["pause_requested"])
        if "pause_requested" in keys
        else False,
        item_id=_col(row, keys, "item_id"),
        # Pre-v35 rows have no model column / a NULL model -> "" (no recorded
        # route); resume falls back to the config route for those (#1111).
        model=_col(row, keys, "model") or "",
        complexity=_col(row, keys, "complexity") or "",
    )


def _row_to_iteration(row: sqlite3.Row) -> IterationRecord:
    started_at = _iso_to_dt(row["started_at"])
    assert started_at is not None, "started_at cannot be NULL"
    return IterationRecord(
        iteration_id=row["iteration_id"],
        loop_id=row["loop_id"],
        idx=row["idx"],
        started_at=started_at,
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        cost_usd=row["cost_usd"],
        tokens=row["tokens"],
        finished_at=_iso_to_dt(row["finished_at"]),
    )


def _epic_item_id(row: sqlite3.Row) -> str:
    """The durable ``item_id`` for an epic-table row (L3).

    Pre-v25 seeds that stamp the version without the column read as NULL — fall
    back to the stringified ``issue_num`` so a row never presents a NULL id."""
    item_id = _col(row, row.keys(), "item_id")
    if item_id is not None:
        return str(item_id)
    return str(row["issue_num"])


def _epic_issue_num(item_id: str) -> int:
    """The int-shim ``issue_num`` DERIVED from ``item_id`` (L3): the number
    itself for a GitHub id, else the ``0`` non-numeric sentinel. The stored
    ``issue_num`` column (an auto-rowid for a Linear row) is never trusted."""
    return int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0


# The complete Linear-era epics whose runs carry numeric GitHub-twin item_ids
# from a pre-SFD recording era. Item-id-shape inference reads a numeric id as a
# GitHub issue number, so it misclassified these as ``github``; `epic run --all`
# then probed them against GitHub labels, found them not-ready, and false-STALLED
# them (#927, the third leak of #877). Their durable tracker is Linear, so
# ``_apply_v33`` backfills the explicit ``epic_driver_state.tracker`` column that
# ``epic_trackers`` now reads first. Identifiable only by name — a numeric twin
# is indistinguishable from a real GitHub epic by shape alone.
_LINEAR_ERA_EPIC_BACKFILL: tuple[str, ...] = (
    "driver-polish",
    "hold-controls",
    "ops-resilience",
    "pilot-rivets",
)


def _infer_epic_tracker(item_ids: set[str]) -> str | None:
    """Infer an epic's tracker from the shape of its recorded work-item ids
    (#882/B1): a numeric id is a GitHub issue number (``"github"``), any opaque
    key like ``SFD-12`` is Linear (``"linear"``), and no items at all yields
    ``None`` (unknown — drive under the active tracker rather than guess). Reuses
    the ``GITHUB_ID_RE`` numeric shape :func:`_epic_issue_num` keys on."""
    if not item_ids:
        return None
    if any(not GITHUB_ID_RE.fullmatch(iid) for iid in item_ids):
        return "linear"
    return "github"


def _epic_db_issue_num(item_id: str) -> int | None:
    """The value to WRITE into the epic table's ``issue_num`` column (L3).

    Always ``None``: ``issue_num`` is an ``INTEGER PRIMARY KEY``, so a NULL
    insert takes a fresh auto-rowid (always ``MAX(rowid)+1``, so unique and
    monotonic). The real atomic key is now the UNIQUE ``item_id`` index. Writing
    the actual GitHub number here instead would risk a collision: a Linear row's
    auto-rowid could equal a GitHub issue number reserved later, and that
    explicit insert would then fail the PK — wedging a real GitHub node. So the
    column is left vestigial; ``.issue_num`` on a record is derived from
    ``item_id`` via :func:`_epic_issue_num`, never read back from here."""
    return None


def _row_to_epic_reservation(row: sqlite3.Row) -> EpicReservation:
    reserved_at = _iso_to_dt(row["reserved_at"])
    assert reserved_at is not None, "reserved_at cannot be NULL"
    item_id = _epic_item_id(row)
    return EpicReservation(
        issue_num=_epic_issue_num(item_id),
        epic_ref=row["epic_ref"],
        run_id=row["run_id"],
        reserved_at=reserved_at,
        item_id=item_id,
    )


def _continuation_checkout_binding_matches(
    event: AuditEvent, record: ContinuationRecord
) -> bool:
    """Match a registration event to the complete continuation identity."""
    payload = event.payload or {}
    expected = {
        "epic_ref": record.epic_ref,
        "original_run_id": record.original_run_id,
        "branch": record.branch,
        "worktree_identity": record.worktree_identity,
    }
    return event.run_id == record.original_run_id and all(
        payload.get(key) == value for key, value in expected.items()
    )


def _row_to_continuation(row: sqlite3.Row) -> ContinuationRecord:
    created_at = _iso_to_dt(row["created_at"])
    updated_at = _iso_to_dt(row["updated_at"])
    assert created_at is not None, "created_at cannot be NULL"
    assert updated_at is not None, "updated_at cannot be NULL"
    return ContinuationRecord(
        epic_ref=row["epic_ref"],
        item_id=row["item_id"],
        original_run_id=row["original_run_id"],
        branch=row["branch"],
        worktree_identity=row["worktree_identity"],
        state=row["state"],
        generation=int(row["generation"]),
        continuation_run_id=row["continuation_run_id"],
        created_at=created_at,
        updated_at=updated_at,
        claimed_at=_iso_to_dt(row["claimed_at"]),
        terminal_at=_iso_to_dt(row["terminal_at"]),
    )


def _hold_reason_admitted(reason: str, only_prefixes: Collection[str]) -> bool:
    """Whether ``remove_epic_hold`` may delete this reason (see its docstring)."""
    return not only_prefixes or reason.startswith(tuple(only_prefixes))


def _row_to_epic_hold(row: sqlite3.Row) -> EpicHold:
    held_at = _iso_to_dt(row["held_at"])
    assert held_at is not None, "held_at cannot be NULL"
    item_id = _epic_item_id(row)
    return EpicHold(
        issue_num=_epic_issue_num(item_id),
        epic_ref=row["epic_ref"],
        reason=row["reason"],
        held_at=held_at,
        item_id=item_id,
    )


def _row_to_orphan_pr(row: sqlite3.Row) -> OrphanPRRecord:
    detected_at = _iso_to_dt(row["detected_at"])
    assert detected_at is not None, "detected_at cannot be NULL"
    return OrphanPRRecord(
        pr_number=row["pr_number"],
        url=row["url"],
        branch=row["branch"],
        reason=row["reason"],
        detected_at=detected_at,
        acked_at=_iso_to_dt(row["acked_at"]),
    )


def _row_to_epic_retry(row: sqlite3.Row) -> EpicRetry:
    retried_at = _iso_to_dt(row["retried_at"])
    assert retried_at is not None, "retried_at cannot be NULL"
    item_id = _epic_item_id(row)
    return EpicRetry(
        issue_num=_epic_issue_num(item_id),
        epic_ref=row["epic_ref"],
        retried_at=retried_at,
        item_id=item_id,
        superseded_run_ids=_superseded_run_ids(row["superseded_run_ids"]),
        superseded_before=_iso_to_dt(row["superseded_before"]),
    )


def _superseded_run_ids(raw: str | None) -> frozenset[str]:
    """The run ids a V72 marker binds; none for a NULL or pre-v72 column."""
    if not raw:
        return frozenset()
    return frozenset(str(run_id) for run_id in json.loads(raw))


def _started_before(started: str | None, cutoff: datetime | None) -> bool:
    """Whether a run's stored start sorts before a retry marker's cutoff."""
    start = _iso_to_dt(started)
    return start is not None and cutoff is not None and start < cutoff


def _strict_round_number(value: object) -> int:
    """A stored review round is an integer or the row is malformed.

    ``int()`` would silently turn a REAL ``1.5`` or a huge float into a
    plausible round number, and the publication guards compare rounds for
    exact identity, so a malformed ledger must read as an error (which the
    guards refuse on) rather than as a matching row.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"review_rounds.round is not an integer: {value!r}")
    return value


def _row_to_review_round(row: sqlite3.Row) -> ReviewRound:
    recorded_at = _iso_to_dt(row["recorded_at"])
    assert recorded_at is not None, "recorded_at cannot be NULL"
    return ReviewRound(
        epic_ref=row["epic_ref"],
        item_id=row["item_id"],
        round=_strict_round_number(row["round"]),
        head_sha=row["head_sha"],
        verdict=row["verdict"],
        sidecar_digest=row["sidecar_digest"],
        recorded_at=recorded_at,
        sidecar_path=_col(row, row.keys(), "sidecar_path"),
        review_mode=_col(row, row.keys(), "review_mode"),
        accepted_verdict=_col(row, row.keys(), "accepted_verdict"),
        plan_sha=_col(row, row.keys(), "plan_sha"),
        criteria_json=_col(row, row.keys(), "criteria_json"),
    )


def _row_to_item_plan(row: sqlite3.Row) -> ItemPlanRow:
    recorded_at = _iso_to_dt(row["recorded_at"])
    assert recorded_at is not None, "recorded_at cannot be NULL"
    return ItemPlanRow(
        id=int(row["id"]),
        item_id=row["item_id"],
        run_id=row["run_id"],
        body_digest=row["body_digest"],
        source=row["source"],
        plan_sha=row["plan_sha"],
        plan_json=row["plan_json"],
        criteria_count=int(row["criteria_count"]),
        approved_by=row["approved_by"],
        recorded_at=recorded_at,
        superseded_at=_iso_to_dt(row["superseded_at"]),
    )


def _row_to_triage_decision(row: sqlite3.Row) -> TriageDecisionRow:
    recorded_at = _iso_to_dt(row["recorded_at"])
    assert recorded_at is not None, "recorded_at cannot be NULL"
    actionable = row["actionable"]
    needs_info = row["needs_info"]
    return TriageDecisionRow(
        id=int(row["id"]),
        item_id=row["item_id"],
        body_digest=row["body_digest"],
        backend=row["backend"],
        model=row["model"],
        prompt_sha=row["prompt_sha"],
        outcome=row["outcome"],
        decision_json=row["decision_json"],
        recorded_at=recorded_at,
        source_tracker=row["source_tracker"],
        issue_num=None if row["issue_num"] is None else int(row["issue_num"]),
        run_id=row["run_id"],
        type=row["type"],
        priority=row["priority"],
        complexity=row["complexity"],
        confidence=None if row["confidence"] is None else float(row["confidence"]),
        actionable=None if actionable is None else bool(actionable),
        needs_info=None if needs_info is None else bool(needs_info),
        duplicate_of=(
            None if row["duplicate_of"] is None else int(row["duplicate_of"])
        ),
        resolution=row["resolution"],
        resolved_by=row["resolved_by"],
        resolved_at=_iso_to_dt(row["resolved_at"]),
        superseded_at=_iso_to_dt(row["superseded_at"]),
    )


class SqliteRunStore(
    CommittedReadStoreMixin,
    AcceptanceContractStoreMixin,
    AdmissionIdentityStoreMixin,
    ArtifactReceiptStoreMixin,
    ArtifactStoreMixin,
    AuditChainStoreMixin,
    EpicReservationStoreMixin,
    HumanReviewStoreMixin,
    ItemPlanStoreMixin,
    MergeClaimStoreMixin,
    MergeQueueStoreMixin,
    NoChangeStoreMixin,
    OutcomeStoreMixin,
    PromptProfileStoreMixin,
    PublicationJobStoreMixin,
    ReadinessProbeStoreMixin,
    RepoRenameStoreMixin,
    ReviewRoundStoreMixin,
    RouteProfileStoreMixin,
    RunRecordStoreMixin,
    RunLaunchStoreMixin,
    StopLabelStoreMixin,
    TriageDecisionStoreMixin,
):
    """SQLite-backed implementation of RunStore."""

    # The store owns the work projection vocabulary. Committed readers expose
    # only these reads through their private connection-bound store copy.
    _committed_work_reads = frozenset(
        {
            "list_by_item",
            "get_admission_identity",
            "list_admission_identities",
            "list_orphan_prs",
            "get_fleet_stopped",
            "active_publication_job",
            "publication_review",
            "live_no_change_judgment",
            "list_outcome_sync_jobs",
            "list_dependency_effects",
            "get_route_profile",
            "lookup_readiness_probe",
            "review_observation",
            "get_human_review_policy",
            "list_publication_jobs",
            "review_capacity_occupancy",
        }
    )

    def __init__(
        self,
        db_path: str,
        *,
        project_root: str | Path | None = None,
        check_same_thread: bool = True,
        read_only: bool = False,
    ) -> None:
        self._stop_label_reset: LabelReset | None = None
        self._db_path = db_path
        self._cancellation_journal = CancellationJournal(db_path)
        self._init_committed_reader(check_same_thread)
        self._bind_ledger_identity = project_root is not None
        self._project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(db_path).resolve().parent
        )
        self._read_only = read_only
        if read_only:
            # A diagnostics reader (e.g. `kew doctor`) must NOT migrate the DB
            # as a side effect — bumping the schema under a live older process
            # hard-blocks it with SchemaTooNewError (#972 review Finding 3).
            # Open read-only so no write (migration OR WAL pragma) can happen.
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                check_same_thread=check_same_thread,
            )
        else:
            self._conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
        self._conn.row_factory = sqlite3.Row
        self._register_sql_functions(self._conn)
        if not read_only:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Serialize BEGIN IMMEDIATE...COMMIT sections in-process (I7). The TUI
        # shares one connection across threads (check_same_thread=False); Python
        # guards each individual execute() call, but NOT a multi-statement write
        # transaction — two threads issuing BEGIN IMMEDIATE on the same
        # connection interleave, and the second raises "cannot start a
        # transaction within a transaction". This lock makes each transaction
        # body atomic in-process; BEGIN IMMEDIATE still guards cross-process
        # (multi-connection) writers.
        self._txn_lock = threading.Lock()
        # Include single-statement writers in provider-delivery fencing.
        self._write_lock = threading.RLock()
        # The busy-retry back-off. A per-store hook, so a test can step this
        # store's retry without replacing the process-wide ``time.sleep`` that
        # every other store and thread also calls.
        self.busy_sleep: Callable[[float], None] = time.sleep
        if read_only:
            self._check_schema_version()
        else:
            self._migrate()

    def _register_sql_functions(self, connection: sqlite3.Connection) -> None:
        # Filesystem predicates run in Python; path strings never exclude aliases.
        connection.create_function(
            "continuation_checkout_matches",
            2,
            lambda recorded, checkout: (
                isinstance(recorded, str)
                and continuation_checkout_matches(recorded, checkout)
            ),
        )

    def _check_schema_version(self) -> None:
        """Read-only schema guard used by the read-only open path.

        Migration writes, so a read-only store cannot run ``_migrate``. It
        still must reject a DB written by a newer kew (its schema may have
        renamed or dropped a column this build reads), and surface that as the
        normal ``SchemaTooNewError`` its caller already handles — never a raw
        traceback from a failed query later on.
        """
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            # No schema_version table yet (empty/foreign DB) — nothing to read.
            return
        current_version = row["version"] if row else 0
        if current_version > CURRENT_SCHEMA_VERSION:
            self._conn.close()
            raise SchemaTooNewError(
                f"dispatch.db is schema v{current_version}; this kew supports up "
                f"to v{CURRENT_SCHEMA_VERSION}. Upgrade kew to open it."
            )

    def _execute(
        self,
        sql: str,
        params: "sqlite3._Parameters | None" = None,
    ) -> sqlite3.Cursor:
        """Thin wrapper around self._conn.execute for patching in tests."""
        if params is not None:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def close(self) -> None:
        """Close this thread's reader and the writer connection.

        Inside a ``read_snapshot`` block ``_conn`` is the snapshot's
        connection, which the block closes itself; this closes the writer.
        """
        self.close_committed_reader()
        self._close_writer()

    @property
    def reopenable(self) -> bool:
        """False for an in-memory ledger, which no second handle can reach."""
        return self._db_path != ":memory:"

    def open_sibling(self) -> "SqliteRunStore":
        """Open a second handle on this ledger for another thread (#2110).

        The handle binds the same file and project identity with its own
        thread-affine connection; WAL and ``busy_timeout`` arbitrate its writes
        against this handle's exactly as they do between two kew processes.
        """
        if not self.reopenable:
            raise ValueError("an in-memory ledger has no second handle")
        return SqliteRunStore(
            self._db_path,
            project_root=self._project_root if self._bind_ledger_identity else None,
            read_only=self._read_only,
        )

    def __enter__(self) -> "SqliteRunStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _try_alter(cursor: sqlite3.Cursor, sql: str) -> None:
        """Execute an ALTER TABLE statement, ignoring duplicate-column errors.

        SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.
        When multiple connections open the same DB concurrently, they each
        attempt to apply the same migration. The second one to run will hit
        ``OperationalError: duplicate column name``.  That is harmless — the
        schema is already correct — so we swallow it here.
        """
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

    @staticmethod
    def _table_exists(cursor: sqlite3.Cursor, name: str) -> bool:
        """True if a table named ``name`` exists in the current DB."""
        return (
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def _apply_v1(self, cursor: sqlite3.Cursor) -> None:
        # OR IGNORE: a concurrent bootstrap may have inserted the row already
        cursor.execute(
            "INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1)"
        )

    def _apply_v2(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN runner_name TEXT NOT NULL DEFAULT ''",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 2) WHERE id = 1"
        )

    def _apply_v3(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(cursor, _SCHEMA_V3)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 3) WHERE id = 1"
        )

    def _apply_v4(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(cursor, _SCHEMA_V4)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 4) WHERE id = 1"
        )

    def _apply_v5(self, cursor: sqlite3.Cursor) -> None:
        cursor.execute(_SCHEMA_V5)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 5) WHERE id = 1"
        )

    def _apply_v6(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(cursor, _SCHEMA_V6)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 6) WHERE id = 1"
        )

    def _apply_v7(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(cursor, _SCHEMA_V7_PGID)
        cursor.execute(_SCHEMA_V7_META)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 7) WHERE id = 1"
        )

    def _apply_v8(self, cursor: sqlite3.Cursor) -> None:
        for stmt in _SCHEMA_V8_RUNS:
            self._try_alter(cursor, stmt)
        cursor.execute(_SCHEMA_V8_AUDIT_EVENTS)
        cursor.execute(_SCHEMA_V8_AUDIT_INDEX)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 8) WHERE id = 1"
        )

    def _apply_v9(self, cursor: sqlite3.Cursor) -> None:
        for stmt in _SCHEMA_V9:
            cursor.execute(stmt)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 9) WHERE id = 1"
        )

    def _apply_v10(self, cursor: sqlite3.Cursor) -> None:
        self._try_alter(
            cursor,
            "ALTER TABLE loops ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 10) WHERE id = 1"
        )

    def _apply_v11(self, cursor: sqlite3.Cursor) -> None:
        for stmt in _SCHEMA_V11:
            self._try_alter(cursor, stmt)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 11) WHERE id = 1"
        )

    def _apply_v12(self, cursor: sqlite3.Cursor) -> None:
        """Backfill the prev_hash chain over existing rows (one-time).

        Runs inside the migration loop's BEGIN IMMEDIATE transaction, which
        serializes the walk against concurrent migrators and live appends.
        """
        row = cursor.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        if (row["version"] if row else 0) >= 12:
            return
        self._backfill_chain()
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 12) WHERE id = 1"
        )

    def _apply_v13(self, cursor: sqlite3.Cursor) -> None:
        """Create the content-addressed policy-snapshot table (#250).

        Simple form (no BEGIN IMMEDIATE): an empty CREATE TABLE IF NOT EXISTS
        plus a monotonic MAX(version) bump races benignly under N concurrent
        openers — it walks no rows and need not serialize against live appends
        (unlike v12's chain backfill). The table is introduced here, not in the
        _SCHEMA_V1 bootstrap, matching the audit_events-in-_apply_v8 convention.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS policy_snapshots ("
            " policy_hash TEXT PRIMARY KEY,"
            " snapshot_json TEXT NOT NULL,"
            " first_seen TEXT NOT NULL)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 13) WHERE id = 1"
        )

    def _apply_v14(self, cursor: sqlite3.Cursor) -> None:
        """Add the runner-reported resolved-model column (#251).

        Simple form (no BEGIN IMMEDIATE/backfill): an idempotent ALTER via
        _try_alter plus a monotonic MAX(version) bump races benignly under N
        concurrent openers. reported_model binds into the audit chain through
        the completion event's payload, so no re-chain is needed here.
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN reported_model TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 14) WHERE id = 1"
        )

    def _apply_v15(self, cursor: sqlite3.Cursor) -> None:
        """Create the off-chain interactive_usage table (#256).

        New-table precedent of _apply_v13: a plain CREATE TABLE IF NOT EXISTS
        plus a monotonic MAX(version) bump races benignly under N concurrent
        openers. UNIQUE(dedup_key) gives idempotent INSERT OR IGNORE. This
        table is deliberately off the audit hash chain.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS interactive_usage ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " runner TEXT NOT NULL,"
            " model TEXT NOT NULL,"
            " observed_at TEXT NOT NULL,"
            " input_tokens INTEGER NOT NULL DEFAULT 0,"
            " output_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_creation_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
            " cost_usd REAL NOT NULL DEFAULT 0.0,"
            " session_id TEXT,"
            " ingest_source TEXT NOT NULL,"
            " ingested_at TEXT NOT NULL,"
            " dedup_key TEXT NOT NULL UNIQUE)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_interactive_usage_observed_at"
            " ON interactive_usage(observed_at)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 15) WHERE id = 1"
        )

    def _apply_v16(self, cursor: sqlite3.Cursor) -> None:
        """Add prefix_key and provider columns for cache observability (#348).

        Additive ALTER on both runs and interactive_usage. Pre-v16 rows
        read as NULL → treated as cold/unknown (no history migration).
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN prefix_key TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN provider TEXT")
        tbl = cursor.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='interactive_usage'"
        ).fetchone()
        if tbl:
            self._try_alter(
                cursor, "ALTER TABLE interactive_usage ADD COLUMN prefix_key TEXT"
            )
            self._try_alter(
                cursor, "ALTER TABLE interactive_usage ADD COLUMN provider TEXT"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 16) WHERE id = 1"
        )

    def _apply_v17(self, cursor: sqlite3.Cursor) -> None:
        """Add config_overrides table for web config-manager (#368).

        Additive CREATE TABLE IF NOT EXISTS + index; pre-v17 DBs upgrade
        cleanly with no history migration.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS config_overrides ("
            "  field TEXT PRIMARY KEY,"
            "  value_json TEXT NOT NULL,"
            "  file_value_json TEXT,"
            "  set_at TEXT NOT NULL,"
            "  set_by TEXT NOT NULL,"
            "  reason TEXT NOT NULL DEFAULT '',"
            "  audit_event_id INTEGER"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 17) WHERE id = 1"
        )

    def _apply_v18(self, cursor: sqlite3.Cursor) -> None:
        """Add query_source and agent_name to interactive_usage (#382)."""
        tbl = cursor.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='interactive_usage'"
        ).fetchone()
        if tbl:
            self._try_alter(
                cursor,
                "ALTER TABLE interactive_usage ADD COLUMN query_source TEXT",
            )
            self._try_alter(
                cursor,
                "ALTER TABLE interactive_usage ADD COLUMN agent_name TEXT",
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 18) WHERE id = 1"
        )

    def _apply_v19(self, cursor: sqlite3.Cursor) -> None:
        """Add epic_reservations and epic_holds tables (#427 Phase 2a).

        New-table precedent of _apply_v13/_apply_v15/_apply_v17: plain
        CREATE TABLE IF NOT EXISTS + indexes plus a monotonic MAX(version)
        bump races benignly under N concurrent openers. Uniqueness comes
        from the ``issue_num`` PRIMARY KEY on each table, not an app-level
        check-then-insert (see ``reserve_epic_node``).
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_reservations ("
            "  issue_num INTEGER PRIMARY KEY,"
            "  epic_ref TEXT NOT NULL,"
            "  run_id TEXT NOT NULL,"
            "  reserved_at TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_res_ref ON epic_reservations(epic_ref)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_holds ("
            "  issue_num INTEGER PRIMARY KEY,"
            "  epic_ref TEXT NOT NULL,"
            "  reason TEXT NOT NULL,"
            "  held_at TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_holds_ref ON epic_holds(epic_ref)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 19) WHERE id = 1"
        )

    def _apply_v20(self, cursor: sqlite3.Cursor) -> None:
        """Add epic_retries table (#427 Phase 2 retry-marker).

        Same new-table precedent as _apply_v13/_apply_v15/_apply_v17/
        _apply_v19: plain CREATE TABLE IF NOT EXISTS + index plus a
        monotonic MAX(version) bump races benignly under N concurrent
        openers. Uniqueness comes from the ``issue_num`` PRIMARY KEY, not
        an app-level check-then-insert.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_retries ("
            "  issue_num INTEGER PRIMARY KEY,"
            "  epic_ref TEXT NOT NULL,"
            "  retried_at TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_retries_ref ON epic_retries(epic_ref)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 20) WHERE id = 1"
        )

    def _apply_v22(self, cursor: sqlite3.Cursor) -> None:
        """Add container_name column for sandboxed dispatch (Node S5).

        Additive ALTER via _try_alter; pre-v22 rows read as NULL → treated
        as host-subprocess runs (no container kill path).
        """
        self._try_alter(cursor, _SCHEMA_V22)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 22) WHERE id = 1"
        )

    def _apply_v21(self, cursor: sqlite3.Cursor) -> None:
        """Add epic_ticks journal + epic_driver_state heartbeat (#454 Phase A).

        New-table precedent of _apply_v13/_apply_v15/_apply_v17/_apply_v19/
        _apply_v20: plain CREATE TABLE IF NOT EXISTS + indexes plus a monotonic
        MAX(version) bump races benignly under N concurrent openers. epic_ticks
        is multi-row per epic (indexed on epic_ref, id); epic_driver_state is
        one row per epic (PRIMARY KEY on epic_ref).
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_ticks ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " epic_ref TEXT NOT NULL,"
            " tick_ts TEXT NOT NULL,"
            " snapshot TEXT NOT NULL,"
            " actions TEXT NOT NULL,"
            " snapshot_hash TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_ticks_ref_id"
            " ON epic_ticks(epic_ref, id)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_driver_state ("
            " epic_ref TEXT PRIMARY KEY,"
            " last_tick_started_at TEXT,"
            " last_tick_ts TEXT,"
            " last_tick_duration_s REAL,"
            " tick_count INTEGER NOT NULL DEFAULT 0,"
            " interval_s REAL,"
            " last_signal TEXT,"
            " last_tick_id INTEGER"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 21) WHERE id = 1"
        )

    def _apply_v23(self, cursor: sqlite3.Cursor) -> None:
        """Add orphan_prs persistence table (#680 Group 1).

        Same new-table precedent as _apply_v19/_apply_v20/_apply_v21: plain
        CREATE TABLE IF NOT EXISTS + index plus a monotonic MAX(version) bump
        races benignly under N concurrent openers. Uniqueness comes from the
        ``pr_number`` PRIMARY KEY (upsert on conflict), not a check-then-insert.
        ``acked_at`` is NULL until an operator acknowledges the orphan.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS orphan_prs ("
            " pr_number INTEGER PRIMARY KEY,"
            " url TEXT NOT NULL,"
            " branch TEXT NOT NULL,"
            " reason TEXT NOT NULL,"
            " detected_at TEXT NOT NULL,"
            " acked_at TEXT"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_orphan_prs_acked_at ON orphan_prs(acked_at)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 23) WHERE id = 1"
        )

    def _apply_v24(self, cursor: sqlite3.Cursor) -> None:
        """Add ``item_id`` to runs/loops/audit_events + backfill + indexes (A2).

        Additive ALTERs via _try_alter (same duplicate-column-tolerant precedent
        as _apply_v22) so N concurrent openers race benignly. Existing rows are
        backfilled from ``issue_num`` (``CAST`` to TEXT; a NULL ``issue_num`` on
        loops/audit_events yields a NULL ``item_id``). The backfill only touches
        rows whose ``item_id`` is still NULL so a re-run never clobbers ids a
        later version may have written. Indexes ``idx_runs_item_id`` /
        ``idx_loops_item_id`` mirror the existing ``issue_num`` indexes;
        ``idx_runs_branch`` backs A6's ``find_run_by_branch`` (``runs.branch``
        had no index before now).

        The ``loops`` / ``audit_events`` steps are guarded on table existence:
        a genuine pre-v24 DB always has them (created at v9 / v8), but
        hand-rolled test seeds that stamp a mid-schema version without those
        tables must migrate cleanly — same guard rationale as ``_backfill_chain``.
        """
        # runs is created in _SCHEMA_V1 and is always present.
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN item_id TEXT")
        cursor.execute(
            "UPDATE runs SET item_id = CAST(issue_num AS TEXT) WHERE item_id IS NULL"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_item_id ON runs(item_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_branch ON runs(branch)")
        if self._table_exists(cursor, "loops"):
            self._try_alter(cursor, "ALTER TABLE loops ADD COLUMN item_id TEXT")
            cursor.execute(
                "UPDATE loops SET item_id = CAST(issue_num AS TEXT)"
                " WHERE item_id IS NULL AND issue_num IS NOT NULL"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_loops_item_id ON loops(item_id)"
            )
        if self._table_exists(cursor, "audit_events"):
            self._try_alter(cursor, "ALTER TABLE audit_events ADD COLUMN item_id TEXT")
            cursor.execute(
                "UPDATE audit_events SET item_id = CAST(issue_num AS TEXT)"
                " WHERE item_id IS NULL AND issue_num IS NOT NULL"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 24) WHERE id = 1"
        )

    def _apply_v25(self, cursor: sqlite3.Cursor) -> None:
        """Key ``epic_reservations``/``epic_holds``/``epic_retries`` on
        ``item_id TEXT`` (L3).

        Additive ALTER via _try_alter (same duplicate-column-tolerant precedent
        as _apply_v22/_apply_v24) so N concurrent openers race benignly. Each
        table's atomic key moves from the ``issue_num`` PRIMARY KEY to a UNIQUE
        index on ``item_id``: a GitHub node stores its numeric ``issue_num`` and
        the stringified ``item_id``; a Linear node stores ``issue_num=NULL`` (an
        auto-rowid — the ``0`` sentinel would collide across items under the PK)
        and its real ``item_id`` (``SFD-12``). The UNIQUE index is what the
        item-keyed reserve/hold/retry writes conflict on. Existing rows backfill
        ``item_id = CAST(issue_num AS TEXT)`` (all distinct — issue_num was the
        PK — so the new UNIQUE index is satisfiable).

        The three tables are created at v19/v20, always present before v25 in a
        real forward migration; the existence guard only protects hand-rolled
        test seeds that stamp a mid-schema version without them.
        """
        for table in ("epic_reservations", "epic_holds", "epic_retries"):
            if not self._table_exists(cursor, table):
                continue
            self._try_alter(cursor, _EPIC_ITEM_ID_ALTER[table])
            cursor.execute(_EPIC_ITEM_ID_BACKFILL[table])
            cursor.execute(_EPIC_ITEM_ID_INDEX[table])
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 25) WHERE id = 1"
        )

    def _apply_v26(self, cursor: sqlite3.Cursor) -> None:
        """Add ``run_kind`` to runs (I1/#445).

        Additive ALTER via _try_alter (same duplicate-column-tolerant precedent
        as _apply_v22/_apply_v24) so N concurrent openers race benignly. The
        column is ``NOT NULL DEFAULT 'dispatch'`` so every existing row
        backfills to the ordinary-dispatch bucket — no separate UPDATE needed.
        No index is added: the ``median_cost_for_bucket`` query already scanned
        the (model, complexity) predicate un-indexed, so run_kind-keying it
        regresses nothing.
        """
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'dispatch'",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 26) WHERE id = 1"
        )

    def _apply_v27(self, cursor: sqlite3.Cursor) -> None:
        """Create the off-chain tracker_events table (W1/#893).

        Inbound Linear (and later GitHub) webhook deliveries land here,
        normalized and idempotent on the tracker's delivery id. Same off-chain
        new-table precedent as _apply_v15 (interactive_usage): a plain
        CREATE TABLE IF NOT EXISTS + index plus a monotonic MAX(version) bump
        races benignly under N concurrent openers, and UNIQUE(delivery_id)
        makes INSERT OR IGNORE idempotent on redelivery. Deliberately off the
        audit hash chain — a tracker event is an external fact, not an
        attested governance action.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS tracker_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " delivery_id TEXT NOT NULL UNIQUE,"
            " source TEXT NOT NULL,"
            " event_type TEXT NOT NULL,"
            " action TEXT,"
            " entity_id TEXT,"
            " webhook_timestamp INTEGER,"
            " payload TEXT NOT NULL,"
            " received_at TEXT NOT NULL)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_tracker_events_received_at"
            " ON tracker_events(received_at)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 27) WHERE id = 1"
        )

    def _apply_v28(self, cursor: sqlite3.Cursor) -> None:
        """Create the off-chain epic_graph_cache table (W2/#894).

        Caches the last full provider graph fetch per epic so the driver /
        `kew epic status` can short-circuit the GraphQL fetch when no
        ``tracker_events`` have arrived since and the reconcile window hasn't
        elapsed. Same off-chain new-table precedent as _apply_v27
        (tracker_events): a plain CREATE TABLE IF NOT EXISTS plus a monotonic
        MAX(version) bump races benignly under N concurrent openers. A cached
        graph is a disposable performance artifact, never an attested fact, so
        it stays off the audit hash chain.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_graph_cache ("
            " epic_ref TEXT PRIMARY KEY,"
            " source TEXT NOT NULL,"
            " fetched_at TEXT NOT NULL,"
            " graph_json TEXT NOT NULL)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 28) WHERE id = 1"
        )

    def _apply_v29(self, cursor: sqlite3.Cursor) -> None:
        """Create the off-chain review_rounds table (F8/#977).

        Persists per-node governed-review state (round counter + per-round
        ``{head_sha, verdict, sidecar_digest}``) so a driver restart mid-review
        resumes the round count instead of resetting it. Same off-chain
        new-table precedent as _apply_v27 (tracker_events) / _apply_v28
        (epic_graph_cache): a plain CREATE TABLE IF NOT EXISTS + index plus a
        monotonic MAX(version) bump races benignly under N concurrent openers.
        Review state is operational, not an attested fact — the attested
        per-round verdict is the ``adversarial_review_verdict`` audit event —
        so this table stays off the audit hash chain.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS review_rounds ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " epic_ref TEXT NOT NULL,"
            " item_id TEXT NOT NULL,"
            " round INTEGER NOT NULL,"
            " head_sha TEXT NOT NULL,"
            " verdict TEXT NOT NULL,"
            " sidecar_digest TEXT,"
            " recorded_at TEXT NOT NULL)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_rounds_item"
            " ON review_rounds(item_id)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 29) WHERE id = 1"
        )

    def _apply_v30(self, cursor: sqlite3.Cursor) -> None:
        """Add ``steps_to_first_edit``/``tokens_to_first_edit`` to runs (D2).

        First-edit metrics — the context-memory epic's measurement substrate.
        Both are nullable INTEGER columns (no DEFAULT): existing rows migrate to
        NULL, which is the correct "not measured" value, and a zero-edit run also
        stays NULL (never 0), so the metric distinguishes "measured, never
        edited" from "edited immediately". Additive ALTERs via _try_alter (same
        duplicate-column-tolerant precedent as _apply_v24/_apply_v26) so N
        concurrent openers race benignly. No index: these columns are read for
        per-run reporting/backfill, never as a query predicate.

        History note: this step briefly also carried the Task-2 columns
        (amended in-place instead of minting v31); DBs stamped 30 in that
        window lack them, which is exactly why _apply_v31 exists. Migration
        steps are immutable history — never amend a merged step.
        """
        self._try_alter(
            cursor, "ALTER TABLE runs ADD COLUMN steps_to_first_edit INTEGER"
        )
        self._try_alter(
            cursor, "ALTER TABLE runs ADD COLUMN tokens_to_first_edit INTEGER"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 30) WHERE id = 1"
        )

    def _apply_v31(self, cursor: sqlite3.Cursor) -> None:
        """Add ``map_tokens``/``lesson_corpus_tokens`` to runs (D2 / R1-8).

        Injected-context sizes — the governor's cost-model inputs. Nullable
        INTEGER columns (no DEFAULT), same NULL-means-not-measured semantics as
        v30. Minted as its own step because Task 2 originally amended the
        already-merged _apply_v30 in place: a DB stamped 30 by Task 1's code
        never re-runs the v30 step, so the columns were missing and every
        ``store.save()`` raised OperationalError (the PR #1033 adversarial-
        review CRITICAL finding, live-hit 2026-07-24). _try_alter keeps this
        idempotent for DBs that migrated through the amended-v30 window and
        already have the columns.
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN map_tokens INTEGER")
        self._try_alter(
            cursor, "ALTER TABLE runs ADD COLUMN lesson_corpus_tokens INTEGER"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 31) WHERE id = 1"
        )

    def _apply_v32(self, cursor: sqlite3.Cursor) -> None:
        """Add ``sidecar_path`` to review_rounds (#1030).

        The NEEDS_CHANGES remediation leg feeds the host-stored sidecar findings
        (not deletable PR comments, F10) as its input. Remediation runs on a
        LATER tick than the review that produced the verdict, driven purely from
        persisted state, so the driver must be able to LOCATE that round's stored
        sidecar — the digest alone is not a path. This nullable column records
        ``logs/dispatch/review-sidecar-<run_id>.json`` for the round so
        ``_remediate`` can load the findings. Additive ALTER via _try_alter (same
        duplicate-column-tolerant precedent as _apply_v30/_apply_v31); pre-v32
        rows migrate to NULL, which the remediator treats as "no findings
        located" (loud no-op, never a silent accept).
        """
        self._try_alter(
            cursor, "ALTER TABLE review_rounds ADD COLUMN sidecar_path TEXT"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 32) WHERE id = 1"
        )

    def _apply_v33(self, cursor: sqlite3.Cursor) -> None:
        """Add durable ``tracker`` to epic_driver_state + backfill Linear-era
        epics with numeric GitHub-twin item ids (#927/N3, third leak of #877).

        ``epic_trackers`` inferred an epic's tracker from the SHAPE of its
        recorded work-item ids: a numeric id => github, an opaque key (SFD-12)
        => linear. Four complete Linear epics (``_LINEAR_ERA_EPIC_BACKFILL``)
        carry numeric GitHub-twin ids from a pre-SFD recording era, so inference
        read them github, ``epic run --all`` probed them against GitHub labels,
        found them not-ready, and false-STALLED them — poisoning the fleet exit
        code. The driver now records the ACTIVE tracker here at first tick
        (:meth:`record_epic_tracker`); inference is a fallback only. This
        one-time backfill repairs the four whose numeric twins would otherwise
        re-trigger the misclassification (they are Linear projects, resolvable
        by name). Additive ALTER via _try_alter (same duplicate-column-tolerant
        precedent as _apply_v30/_apply_v31/_apply_v32); the guarded UPDATE only
        sets a still-NULL tracker so a real later recording is never clobbered.
        """
        self._try_alter(cursor, "ALTER TABLE epic_driver_state ADD COLUMN tracker TEXT")
        for epic_ref in _LINEAR_ERA_EPIC_BACKFILL:
            cursor.execute(
                "UPDATE epic_driver_state SET tracker = 'linear'"
                " WHERE epic_ref = ? AND tracker IS NULL",
                (epic_ref,),
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 33) WHERE id = 1"
        )

    def _apply_v34(self, cursor: sqlite3.Cursor) -> None:
        """Add ``planned_cost_usd`` to runs — the planned half of the
        planned/actual cost pair (#1092, phase 2 of #1082).

        ``estimated_cost_usd`` became the runner's native provider-billed total
        when it reports one (#1082/PR #1091), which silently discarded the
        token×pricing-bound figure dispatch had planned against (#351: bound
        $1.0787 vs native actual $0.1064). This column keeps that bound, so
        every row carries the pair and the planning-accuracy ratio per model is
        computable. Nullable REAL with no DEFAULT: historical rows migrate to
        NULL — "no bound recorded", never a fabricated $0 planned (which would
        report an infinite over-run). Additive ALTER via _try_alter, same
        duplicate-column-tolerant precedent as _apply_v30..v33.
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN planned_cost_usd REAL")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 34) WHERE id = 1"
        )

    def _apply_v35(self, cursor: sqlite3.Cursor) -> None:
        """Add durable ``model``/``complexity`` to loops — its route (#1111).

        A loop stored only ``runner_name``, so the model half of its route lived
        nowhere but the in-memory ``ResolvedRoute`` of the process that started
        it. Once loops resolve through ``[routing]`` (a complexity tier can pick
        a different runner AND model than ``[routing].default``), that made the
        route unreconstructable: ``kew loop resume`` re-resolved the config
        default and silently escalated a simple-tier loop back onto the default
        model for the rest of its life. Recording the model here makes the route
        durable, so resume continues on the route the loop actually started on.
        Additive ALTER via _try_alter (duplicate-column-tolerant, same precedent
        as _apply_v30.._apply_v34); pre-v35 rows migrate to NULL, read as ``""``
        = "no recorded model" -> callers fall back to the config route exactly as
        they did before.

        ``complexity`` records the routing TIER that decision was made at, which
        the record needs for the same reason: when a mid-flight DB route override
        (#659) has to be re-resolved for a continuation, resolving it without the
        tier would land on ``[routing].default`` — the same escalation by another
        door. Pre-v35 rows read as ``""`` = "no recorded tier" -> default tier.

        Guarded on table existence like the ``loops`` leg of ``_apply_v24``: a
        genuine pre-v35 DB always has ``loops`` (created at v9), but hand-rolled
        seeds that stamp a mid-schema version without it must still migrate.
        """
        if self._table_exists(cursor, "loops"):
            self._try_alter(cursor, "ALTER TABLE loops ADD COLUMN model TEXT")
            self._try_alter(cursor, "ALTER TABLE loops ADD COLUMN complexity TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 35) WHERE id = 1"
        )

    def _apply_v36(self, cursor: sqlite3.Cursor) -> None:
        """Mint the singleton ledger identity and bind it into the audit chain."""
        if not self._bind_ledger_identity:
            cursor.execute(
                "UPDATE schema_version SET version = MAX(version, 36) WHERE id = 1"
            )
            return
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_identity (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                ledger_id TEXT NOT NULL UNIQUE,
                project_root TEXT NOT NULL,
                created_at TEXT NOT NULL,
                binding_event_id INTEGER UNIQUE
            )
            """
        )
        existing = cursor.execute(
            "SELECT 1 FROM ledger_identity WHERE id = 1"
        ).fetchone()
        if existing is not None:
            cursor.execute(
                "UPDATE schema_version SET version = MAX(version, 36) WHERE id = 1"
            )
            return
        if not self._table_exists(cursor, "audit_events"):
            raise RuntimeError("cannot bind ledger identity without audit_events")
        ledger_id = str(uuid.uuid4())
        created_at = datetime.now(UTC).isoformat()
        project_root = str(self._project_root)
        cursor.execute(
            "INSERT INTO ledger_identity "
            "(id, ledger_id, project_root, created_at) VALUES (1, ?, ?, ?)",
            (ledger_id, project_root, created_at),
        )
        payload = json.dumps(
            {
                "format_version": 1,
                "ledger_id": ledger_id,
                "project_root": project_root,
            },
            separators=(",", ":"),
        )
        event_id = self._chained_insert_in_transaction(
            (
                created_at,
                "ledger_identity_bound",
                None,
                None,
                None,
                "system",
                "kew",
                None,
                None,
                payload,
                None,
            )
        )
        cursor.execute(
            "UPDATE ledger_identity SET binding_event_id = ? WHERE id = 1",
            (event_id,),
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 36) WHERE id = 1"
        )

    def _apply_v37(self, cursor: sqlite3.Cursor) -> None:
        """Add secret-free inference-transport admission evidence to runs."""
        columns = (
            "transport_mode TEXT NOT NULL DEFAULT 'direct'",
            "transport_endpoint TEXT",
            "transport_route TEXT",
            "transport_managed INTEGER NOT NULL DEFAULT 0",
            "transport_expected_version TEXT",
            "transport_observed_version TEXT",
            "transport_config_sha256 TEXT",
            "transport_preflight_verdict TEXT",
        )
        for column in columns:
            self._try_alter(cursor, f"ALTER TABLE runs ADD COLUMN {column}")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 37) WHERE id = 1"
        )

    def _apply_v38(self, cursor: sqlite3.Cursor) -> None:
        """Persist preserved-work continuation ownership (SFD recovery Task 1).

        The composite primary key keeps separate recovery records distinct when
        an item is reused by another original run, branch, or worktree.  A
        continuation claim changes ``generation`` and is paired with its
        ``epic_reservations`` row by the reservation mixin inside one write
        transaction; this table itself is additive, so pre-v38 rows need no
        backfill.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_continuations ("
            "  epic_ref TEXT NOT NULL,"
            "  item_id TEXT NOT NULL,"
            "  original_run_id TEXT NOT NULL,"
            "  branch TEXT NOT NULL,"
            "  worktree_identity TEXT NOT NULL,"
            "  state TEXT NOT NULL CHECK (state IN ("
            "    'awaiting_continuation', 'continuing', 'terminal'"
            "  )),"
            "  generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),"
            "  continuation_run_id TEXT,"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL,"
            "  claimed_at TEXT,"
            "  terminal_at TEXT,"
            "  PRIMARY KEY (epic_ref, item_id, original_run_id, branch,"
            "    worktree_identity)"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_continuations_state"
            " ON epic_continuations(epic_ref, state)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_continuations_owner"
            " ON epic_continuations(continuation_run_id)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 38) WHERE id = 1"
        )

    def _apply_v39(self, cursor: sqlite3.Cursor) -> None:
        """Add an atomic idempotency key for continuation audit evidence."""
        if not self._table_exists(cursor, "audit_events"):
            cursor.execute(
                "UPDATE schema_version SET version = MAX(version, 39) WHERE id = 1"
            )
            return
        self._try_alter(cursor, "ALTER TABLE audit_events ADD COLUMN dedup_key TEXT")
        claimed = {
            str(row["dedup_key"])
            for row in cursor.execute(
                "SELECT dedup_key FROM audit_events WHERE dedup_key IS NOT NULL"
            )
        }
        rows = cursor.execute(
            "SELECT id, event, payload FROM audit_events ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else None
            except (json.JSONDecodeError, TypeError):
                payload = None
            if not isinstance(payload, dict):
                continue
            dedup_key = continuation_evidence_identity(row["event"], payload)
            if dedup_key is None or dedup_key in claimed:
                continue
            cursor.execute(
                "UPDATE audit_events SET dedup_key = ? WHERE id = ?",
                (dedup_key, row["id"]),
            )
            claimed.add(dedup_key)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_dedup_key"
            " ON audit_events(dedup_key) WHERE dedup_key IS NOT NULL"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 39) WHERE id = 1"
        )

    def _apply_v40(self, cursor: sqlite3.Cursor) -> None:
        """Add adaptive Switchyard identity, tier, and overhead evidence."""
        columns = (
            "resolved_model_source TEXT",
            "selected_tier TEXT",
            "requested_service_tier TEXT",
            "reported_service_tier TEXT",
            "classifier_overhead_model TEXT",
            "classifier_overhead_input_tokens INTEGER NOT NULL DEFAULT 0",
            "classifier_overhead_output_tokens INTEGER NOT NULL DEFAULT 0",
            "classifier_overhead_cost_usd REAL NOT NULL DEFAULT 0.0",
            "switchyard_proxy_observation TEXT",
        )
        for column in columns:
            self._try_alter(cursor, f"ALTER TABLE runs ADD COLUMN {column}")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 40) WHERE id = 1"
        )

    def _apply_v41(self, cursor: sqlite3.Cursor) -> None:
        """Add the managed adaptive accounting reconciliation verdict."""
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN accounting_status TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 41) WHERE id = 1"
        )

    def _apply_v42(self, cursor: sqlite3.Cursor) -> None:
        """Add the normalized immutable admission identity (I01).

        Legacy rows are backfilled to an *honest partial* identity: the repo
        and attempt they actually recorded, and nothing else. No item id: the
        tracker was never persisted, an item without a tracker to interpret it
        is not an identity, and the v24/v26 relabeling makes old review shims
        indistinguishable from dispatches. ``runs.item_id`` keeps the legacy
        key for accounting. ``source_tracker``, ``epic_ref``, and ``node_id``
        are NULL for every legacy row — inferring them from an id's shape
        would be the guess this table exists to end.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS run_admission_identity ("
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL DEFAULT 1,"
            "source_tracker TEXT,"
            "item_id TEXT,"
            "delivery_repo TEXT,"
            "epic_ref TEXT,"
            "node_id TEXT,"
            "recorded_at TEXT NOT NULL,"
            "PRIMARY KEY (run_id, attempt_number)"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_admission_item"
            " ON run_admission_identity(source_tracker, item_id)"
        )
        # Column-introspected because a partially-migrated DB reaches here
        # without the later ``runs`` columns (an old backup restored at v17
        # never ran ``_apply_v8``'s ``repo`` add). A missing column is honestly
        # NULL, not a migration crash.
        present = {row[1] for row in cursor.execute("PRAGMA table_info(runs)")}

        def _col(name: str, expr: str = "") -> str:
            return (expr or name) if name in present else "NULL"

        # No legacy row ever persisted a source tracker, and this module
        # refuses an item without a tracker to interpret it — so the only
        # honest legacy backfill records NO item at all. Run-kind taxonomy
        # cannot rescue it: v24 copied issue_num into item_id for every row
        # and v26 relabeled every pre-existing row 'dispatch', so an old
        # standalone review of PR 87 is indistinguishable from a dispatch of
        # issue 87. runs.item_id keeps the legacy key for accounting.
        cursor.execute(
            # nosec B608 — interpolated fragments are module-constant column
            # names; nothing external reaches the SQL
            "INSERT OR IGNORE INTO run_admission_identity"  # nosec B608
            " (run_id, attempt_number, source_tracker, item_id, delivery_repo,"
            " epic_ref, node_id, recorded_at)"
            f" SELECT run_id, {_col('attempt_number', 'MAX(attempt_number, 1)')},"
            " NULL, NULL,"
            f" {_col('repo')}, NULL, NULL, {_col('started_at')} FROM runs"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 42) WHERE id = 1"
        )

    def _apply_v43(self, cursor: sqlite3.Cursor) -> None:
        """Add the realized route profile of every paid attempt (I03C).

        Deliberately NOT backfilled. The columns a legacy row carries
        (``runner_name``, ``model``, ``container_name``) describe what kew
        *asked for*, never the executable digest or the immutable image ID it
        actually launched — writing them here would manufacture exactly the
        borrowed evidence this table exists to refuse. A pre-v43 attempt
        therefore has no row, and ``route_profile_or_unknown`` answers with
        explicit unknowns.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS run_route_profile ("
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL DEFAULT 1,"
            "runner TEXT NOT NULL,"
            "requested_model TEXT NOT NULL,"
            "containment TEXT,"
            "source_provider TEXT,"
            "inference_provider TEXT,"
            "runner_executable TEXT,"
            "runner_version TEXT,"
            "runner_digest TEXT,"
            "image_ref TEXT,"
            "image_id TEXT,"
            "endpoint TEXT,"
            "service_tier TEXT,"
            "auth_path TEXT,"
            "fingerprint TEXT NOT NULL,"
            "profile_json TEXT NOT NULL,"
            "recorded_at TEXT NOT NULL,"
            "PRIMARY KEY (run_id, attempt_number)"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_route_profile_fingerprint"
            " ON run_route_profile(fingerprint)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS run_route_observation ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL,"
            "field TEXT NOT NULL,"
            "value TEXT,"
            "source TEXT NOT NULL,"
            "observed_at TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_route_observation_attempt"
            " ON run_route_observation(run_id, attempt_number)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 43) WHERE id = 1"
        )

    def _apply_v44(self, cursor: sqlite3.Cursor) -> None:
        """Create the readiness-probe lifecycle/lease/evidence table (I05A).

        Off the audit hash chain and deliberately NOT backfilled: no run that
        predates this table was a probe, so there is nothing to reconstruct.

        ``idx_readiness_probe_live`` is a PARTIAL unique index over
        ``fingerprint`` restricted to leased rows. That is the atomic
        single-spend guarantee in the schema itself: however many drivers race,
        at most one live lease can exist per exact boundary fingerprint, while
        the settled history for that fingerprint stays unbounded.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS readiness_probe ("
            "probe_id TEXT PRIMARY KEY,"
            "fingerprint TEXT NOT NULL,"
            "run_id TEXT NOT NULL UNIQUE,"
            "state TEXT NOT NULL,"
            "planned_cost_usd REAL NOT NULL,"
            "planned_cost_source TEXT NOT NULL,"
            "planned_cost_certainty TEXT NOT NULL,"
            "recorded_cost_usd REAL,"
            "recorded_cost_source TEXT,"
            "recorded_cost_certainty TEXT,"
            "session_id TEXT,"
            "container_name TEXT,"
            "pgid INTEGER,"
            "cleanup_acknowledged INTEGER NOT NULL DEFAULT 0,"
            "detail TEXT NOT NULL DEFAULT '',"
            "leased_at TEXT NOT NULL,"
            "lease_expires_at TEXT NOT NULL,"
            "settled_at TEXT"
            ")"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_readiness_probe_live"
            " ON readiness_probe(fingerprint) WHERE state = 'leased'"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_readiness_probe_settled"
            " ON readiness_probe(fingerprint, settled_at)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 44) WHERE id = 1"
        )

    def _apply_v45(self, cursor: sqlite3.Cursor) -> None:
        """Create the normalized artifact index (I06).

        Metadata only: kind, digest, byte length, an opaque locator and the
        retention/purge lifecycle. There is deliberately NO content column —
        the bytes of an exact compiled prompt live under the owner-only
        ``.kew/private/prompts`` tree (:mod:`kew.sensitive_prompt`) and must
        never reach SQLite, a backup, or an export.

        Not backfilled: a run that predates this table retained no artifact,
        and inventing a digest for bytes nobody kept would be exactly the
        borrowed evidence the table exists to refuse.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS run_artifact ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL DEFAULT 1,"
            "kind TEXT NOT NULL,"
            "storage TEXT NOT NULL,"
            "digest TEXT NOT NULL,"
            "byte_length INTEGER NOT NULL,"
            "locator TEXT,"
            "created_at TEXT NOT NULL,"
            "terminal_at TEXT,"
            "expires_at TEXT,"
            "purged_at TEXT,"
            "purge_reason TEXT,"
            "UNIQUE (run_id, attempt_number, kind)"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_artifact_expiry"
            " ON run_artifact(kind, expires_at)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 45) WHERE id = 1"
        )

    def _apply_v46(self, cursor: sqlite3.Cursor) -> None:
        """Create the immutable artifact-receipt table (I07).

        One row per sealed lineage: source, run/attempt, trusted base,
        recoverable package, immutable tree, how the tree was verified, and the
        audit prefix (``through_seq``) plus chain head the seal closed.

        The two triggers are the point of the table. Later publication,
        judgment and sync facts are appended to the audit chain as attestations
        that REFERENCE a receipt; nothing downstream has a reason to edit one,
        so the schema refuses to let anything try. A receipt that could be
        revised after the fact would not be evidence of what the host was about
        to publish — only of what it currently says it was.

        Not backfilled: no run that predates this table had a sealed lineage,
        and inventing one would be exactly the borrowed evidence it exists to
        refuse.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS artifact_receipt ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL,"
            "source_kind TEXT NOT NULL,"
            "source_id TEXT NOT NULL,"
            "base_sha TEXT NOT NULL,"
            "package_digest TEXT NOT NULL,"
            "tree_sha TEXT NOT NULL,"
            "verification TEXT NOT NULL,"
            "locator TEXT,"
            "receipt_digest TEXT NOT NULL,"
            "through_seq INTEGER NOT NULL,"
            "chain_head TEXT NOT NULL,"
            "sealed_at TEXT NOT NULL,"
            "UNIQUE (run_id, attempt_number)"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_artifact_receipt_package"
            " ON artifact_receipt(package_digest)"
        )
        cursor.execute(
            "CREATE TRIGGER IF NOT EXISTS artifact_receipt_no_update"
            " BEFORE UPDATE ON artifact_receipt BEGIN"
            " SELECT RAISE(ABORT, 'an artifact receipt is sealed evidence');"
            " END"
        )
        cursor.execute(
            "CREATE TRIGGER IF NOT EXISTS artifact_receipt_no_delete"
            " BEFORE DELETE ON artifact_receipt BEGIN"
            " SELECT RAISE(ABORT, 'an artifact receipt is sealed evidence');"
            " END"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 46) WHERE id = 1"
        )

    def _apply_v47(self, cursor: sqlite3.Cursor) -> None:
        """Create the normalized publication-job table (I08).

        One row owns the whole publication of one lineage: the commit, push,
        open and reconcile intent, the attempt that was about to happen, and
        the last thing the remote actually showed. The legacy encoding
        (``runs.status = 'awaiting_publication'``) recorded only that
        publication had not finished, which cannot tell a restarted host
        whether the push landed.

        The partial unique index is the invariant, not a hint: at most one job
        per lineage may be in a live state, so two hosts cannot hold two
        publication intents for one branch. Terminal rows are excluded from it,
        which is what lets a successor SHA take a lineage over.

        Not backfilled. A legacy ``awaiting_publication`` run has no sealed
        receipt, base or tree, so a row written on its behalf would claim
        evidence that never existed; those rows are decoded on read instead
        (:func:`kew.publication_job.decode_legacy_publication`).
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS publication_job ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "job_id TEXT NOT NULL UNIQUE,"
            "lineage_key TEXT NOT NULL,"
            "source_kind TEXT NOT NULL,"
            "source_id TEXT NOT NULL,"
            "run_id TEXT NOT NULL,"
            "attempt_number INTEGER NOT NULL,"
            "branch TEXT NOT NULL,"
            "base_sha TEXT NOT NULL,"
            "tree_sha TEXT NOT NULL,"
            "receipt_digest TEXT NOT NULL,"
            "message TEXT NOT NULL DEFAULT '',"
            "state TEXT NOT NULL,"
            "resume_state TEXT,"
            "head_sha TEXT,"
            "remote_state TEXT,"
            "remote_ref TEXT,"
            "attempt_count INTEGER NOT NULL DEFAULT 0,"
            "last_step TEXT,"
            "last_attempt_at TEXT,"
            "detail TEXT,"
            "predecessor_job_id TEXT,"
            "superseded_by TEXT,"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,"
            "UNIQUE (run_id, attempt_number)"
            ")"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_job_live"
            " ON publication_job(lineage_key)"
            " WHERE state NOT IN ('abandoned', 'reconciled', 'superseded')"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_publication_job_receipt"
            " ON publication_job(receipt_digest)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 47) WHERE id = 1"
        )

    _V48_JOB_COLUMNS = (
        "delivery_repo TEXT NOT NULL DEFAULT ''",
        "review_state TEXT NOT NULL DEFAULT 'not_required'",
        "risk_added_lines INTEGER",
        "risk_deleted_lines INTEGER",
        "risk_paths INTEGER",
        "risk_digest TEXT",
        "risk_reasons TEXT",
        "policy_hash TEXT",
        "policy_generation INTEGER",
        "policy_json TEXT",
        "reserved_at TEXT",
        "reserved_by TEXT",
        "released_at TEXT",
        "confirmed_at TEXT",
        "confirmed_by TEXT",
    )

    def _apply_v48(self, cursor: sqlite3.Cursor) -> None:
        """Give the existing publication job its risk, policy and slot (I13).

        Deliberately additive columns on ``publication_job`` rather than a
        second table: a publication that a person is holding is the SAME job as
        the one that will be pushed, and splitting it would let the two rows
        disagree about which tree was confirmed.

        ``delivery_repo`` is the canonical GitHub repository the work is
        delivered to, and it is what review occupancy is counted over. The
        index below is keyed on it first so the count can never be taken
        globally by accident — there is no index that would make a
        cross-repository count cheap.

        Not backfilled. An existing job was created before any policy was
        bound, so writing a delivery repository, risk or generation onto it
        would attest to a measurement nobody took; those rows read as the
        empty defaults until they are decided under a policy.

        The override table is keyed by delivery repository for the same
        reason, and carries a monotonic ``generation`` plus an optional UTC
        ``expires_at`` so an expiry is a durable fact rather than a timer in
        one host's memory.
        """
        for column in self._V48_JOB_COLUMNS:
            self._try_alter(cursor, f"ALTER TABLE publication_job ADD COLUMN {column}")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_publication_job_review"
            " ON publication_job(delivery_repo, review_state)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS human_review_policy ("
            "delivery_repo TEXT PRIMARY KEY,"
            "generation INTEGER NOT NULL,"
            "max_pending_reviews INTEGER NOT NULL,"
            "max_diff_lines INTEGER NOT NULL,"
            "max_paths INTEGER NOT NULL,"
            "approvers TEXT NOT NULL DEFAULT '[]',"
            "expires_at TEXT,"
            "set_at TEXT NOT NULL,"
            "set_by TEXT NOT NULL DEFAULT '',"
            "reason TEXT NOT NULL DEFAULT '',"
            "audit_event_id INTEGER"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 48) WHERE id = 1"
        )

    _V49_JUDGMENT = (
        "CREATE TABLE IF NOT EXISTS no_change_judgment ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "judgment_id TEXT NOT NULL UNIQUE,"
        "lineage_key TEXT NOT NULL,"
        "generation INTEGER NOT NULL,"
        "source_kind TEXT NOT NULL,"
        "source_id TEXT NOT NULL,"
        "item_id TEXT,"
        "run_id TEXT NOT NULL,"
        "attempt_number INTEGER NOT NULL,"
        "base_sha TEXT NOT NULL,"
        "state TEXT NOT NULL,"
        "policy TEXT NOT NULL DEFAULT 'confirm',"
        "evidence_digest TEXT,"
        "evidence_json TEXT,"
        "decision_reason TEXT NOT NULL DEFAULT '',"
        "decided_by TEXT,"
        "decided_at TEXT,"
        "contract_digest TEXT,"
        "audit_event_id INTEGER,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "UNIQUE (run_id, attempt_number),"
        "UNIQUE (lineage_key, generation)"
        ")"
    )

    _V49_SYNC_JOBS = (
        "CREATE TABLE IF NOT EXISTS outcome_sync_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "sync_job_id TEXT NOT NULL UNIQUE,"
        "judgment_id TEXT NOT NULL,"
        "generation INTEGER NOT NULL,"
        "target_kind TEXT NOT NULL,"
        "target_ref TEXT NOT NULL,"
        "idempotency_key TEXT NOT NULL,"
        "state TEXT NOT NULL,"
        "observation_kind TEXT,"
        "observation_ref TEXT,"
        "observed_at TEXT,"
        "acked_at TEXT,"
        "detail TEXT NOT NULL DEFAULT '',"
        "audit_event_id INTEGER,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "UNIQUE (judgment_id, generation, target_kind, target_ref)"
        ")"
    )

    _V49_EFFECTS = (
        "CREATE TABLE IF NOT EXISTS no_change_dependency_effect ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "effect_id TEXT NOT NULL UNIQUE,"
        "judgment_id TEXT NOT NULL,"
        "generation INTEGER NOT NULL,"
        "dependent_id TEXT NOT NULL,"
        "ordinal INTEGER NOT NULL,"
        "state TEXT NOT NULL,"
        "applied_at TEXT,"
        "acked_at TEXT,"
        "detail TEXT NOT NULL DEFAULT '',"
        "audit_event_id INTEGER,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "UNIQUE (judgment_id, generation, dependent_id),"
        "UNIQUE (judgment_id, generation, ordinal)"
        ")"
    )

    def _apply_v49(self, cursor: sqlite3.Cursor) -> None:
        """Persist the no-change judgment, its synchronization and its effects (I10A).

        A zero-diff exit is the one outcome kew cannot read off its artifacts:
        "there was nothing to do" and "nothing got written" produce the same
        empty diff. These three tables make the difference durable — one
        projection of the judgment, one generation-bound sync intent per
        authority, and one ordered intent per dependent.

        The partial unique index is the invariant, not a hint: at most one
        judgment per item may be live, so two hosts cannot hold two outcomes
        for one item. Terminal rows are excluded, which is what lets a later
        attempt open the next generation. ``UNIQUE (lineage_key, generation)``
        makes the generation a real key rather than a counter that can repeat.

        Dependency effects are unique on ``(judgment, generation, ordinal)``
        as well as on the dependent, so the order a crash resumes into is the
        order the decision was made against.

        Not backfilled. A legacy successful run that opened no pull request
        recorded no trusted base, sealed work package or named check, so a
        candidate written on its behalf would attest to evidence that never
        existed; those rows are decoded as ``unknown`` on read instead
        (:func:`kew.no_change.decode_legacy_no_change`).
        """
        cursor.execute(self._V49_JUDGMENT)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_no_change_judgment_live"
            " ON no_change_judgment(lineage_key)"
            " WHERE state NOT IN ('completed', 'rejected')"
        )
        cursor.execute(self._V49_SYNC_JOBS)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcome_sync_jobs_judgment"
            " ON outcome_sync_jobs(judgment_id, state)"
        )
        cursor.execute(self._V49_EFFECTS)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_no_change_effect_next"
            " ON no_change_dependency_effect(judgment_id, ordinal)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 49) WHERE id = 1"
        )

    def _apply_v50(self, cursor: sqlite3.Cursor) -> None:
        """Separate attempt termination from dispatch-owner liveness.

        Legacy PGIDs identify attempts. Preserve them for termination and leave
        their unknown owners NULL. Never infer owner death from an old attempt.
        Check column existence so replay cannot overwrite a newly recorded owner.
        """
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(runs)")}
        if "attempt_pgid" not in columns:
            self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN attempt_pgid INTEGER")
            if "pgid" in columns:
                cursor.execute("UPDATE runs SET attempt_pgid = pgid, pgid = NULL")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 50) WHERE id = 1"
        )

    def _apply_v51(self, cursor: sqlite3.Cursor) -> None:
        """Record exclusive owner identity without inventing legacy owners.

        V50 attempt anchors remain intact, including those on terminal rows.
        NULL owner_pid explicitly means unknown ownership after migration.
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 51) WHERE id = 1"
        )

    def _apply_v52(self, cursor: sqlite3.Cursor) -> None:
        """Retain birth identities; never invent identities for existing targets."""
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN attempt_start_time TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_start_time TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 52) WHERE id = 1"
        )

    def _apply_v53(self, cursor: sqlite3.Cursor) -> None:
        """Retain registration ancestry without granting authority to legacy rows."""
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN attempt_owner_verified"
            " INTEGER NOT NULL DEFAULT 0",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 53) WHERE id = 1"
        )

    def _apply_v54(self, cursor: sqlite3.Cursor) -> None:
        """Preserve termination authority across cleanup calls and store reopen."""
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN termination_members"
            " TEXT NOT NULL DEFAULT '{}'",
        )

        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_start_token TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN attempt_start_token TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_ppid INTEGER")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_command TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN container_runtime TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN cleanup_runtime TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN container_id TEXT")
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN container_cidfile TEXT")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS stop_label_outbox ("
            "request_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE,"
            "lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,"
            "completed INTEGER NOT NULL DEFAULT 0)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_stop_label_pending"
            " ON stop_label_outbox(completed, lease_until)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 54) WHERE id = 1"
        )

    def _apply_v56(self, cursor: sqlite3.Cursor) -> None:
        """Retain launch receipts without inferring starts from legacy labels."""
        from kew.run_store_launches import create_launch_schema

        create_launch_schema(cursor)
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 56) WHERE id = 1"
        )

    def _apply_v57(self, cursor: sqlite3.Cursor) -> None:
        """Retain abandonment expiry after run retention removes its producer."""
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS continuation_expiries ("
            "abandonment_identity TEXT PRIMARY KEY,"
            "epic_ref TEXT NOT NULL, item_id TEXT NOT NULL,"
            "original_run_id TEXT NOT NULL, branch TEXT NOT NULL,"
            "worktree_identity TEXT NOT NULL, abandoned_generation INTEGER NOT NULL,"
            "replacing_checkout_id TEXT NOT NULL, replacing_run_id TEXT NOT NULL,"
            "replaced_at TEXT NOT NULL)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 57) WHERE id = 1"
        )

    def _apply_v58(self, cursor: sqlite3.Cursor) -> None:
        """Add ``review_mode`` to review_rounds (#2111).

        Records whether a round was a full review of ``base...head`` or a delta
        review of the change since the previous round's head, so the driver can
        require a full pass before a READY stands. Pre-v58 rows migrate to
        NULL, which reads as full for rounds with a reviewer verdict: every
        round before this column was a full review. A ledger without the
        table (a hand-built pre-v29 fixture) has no rounds to annotate.
        """
        if self._table_exists(cursor, "review_rounds"):
            self._try_alter(
                cursor, "ALTER TABLE review_rounds ADD COLUMN review_mode TEXT"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 58) WHERE id = 1"
        )

    def _apply_v59(self, cursor: sqlite3.Cursor) -> None:
        """Add ``accepted_verdict`` to review_rounds (#2151).

        Records the review stop rule's acceptance of a low/medium-only
        ``NEEDS_CHANGES`` round on the round itself, so a reader polling the
        ledger sees the merge-ready state instead of the reviewer's standing
        ``NEEDS_CHANGES``. Pre-v59 rows migrate to NULL: their acceptance, if
        any, is on the audit chain alone. A ledger without the table (a
        hand-built pre-v29 fixture) has no rounds to annotate.
        """
        if self._table_exists(cursor, "review_rounds"):
            self._try_alter(
                cursor, "ALTER TABLE review_rounds ADD COLUMN accepted_verdict TEXT"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 59) WHERE id = 1"
        )

    def _apply_v60(self, cursor: sqlite3.Cursor) -> None:
        """Record each item's acceptance criteria, and bind a round to them (#2290).

        ``item_plans`` is the ledger's copy of what an item asks for, one row
        per (item, body digest) with the plan JSON, its sha and how it was
        obtained; the index on ``item_id`` backs the per-item current/history
        reads. ``review_rounds.plan_sha`` / ``criteria_json`` are the round's
        side of the binding: which plan a round was judged against, and the
        criteria text as the reviewer received it. Both are nullable and
        migrate to NULL — every round before this one was judged against
        nothing stated, and a reader must not read NULL as "the current plan".
        A ledger without ``review_rounds`` (a hand-built pre-v29 fixture) has no
        rounds to annotate.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS item_plans ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " item_id TEXT NOT NULL,"
            " run_id TEXT,"
            " body_digest TEXT NOT NULL,"
            " source TEXT NOT NULL,"
            " plan_sha TEXT NOT NULL,"
            " plan_json TEXT NOT NULL,"
            " criteria_count INTEGER NOT NULL,"
            " approved_by TEXT,"
            " recorded_at TEXT NOT NULL,"
            " superseded_at TEXT"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_item_plans_item ON item_plans(item_id)"
        )
        if self._table_exists(cursor, "review_rounds"):
            self._try_alter(
                cursor, "ALTER TABLE review_rounds ADD COLUMN plan_sha TEXT"
            )
            self._try_alter(
                cursor, "ALTER TABLE review_rounds ADD COLUMN criteria_json TEXT"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 60) WHERE id = 1"
        )

    def _apply_v61(self, cursor: sqlite3.Cursor) -> None:
        """Record what triage proposed about each item, and who decided (#2293).

        ``triage_decisions`` is the ledger's copy of one classification: the
        item and body digest it was taken over, the backend/model/prompt that
        produced it, the decision fields, the outcome the policy derived, and
        the resolution columns a PERSON fills through ``kew triage decide``. The
        index on ``item_id`` backs the per-item current/history reads; the
        partial index backs the unresolved-queue read that the needs-you set and
        ``kew triage queue`` both run.

        Every decision column is nullable because an ``unavailable`` row has no
        decision — and that row is the whole point of the fail-closed rule, so
        the schema has to be able to hold it. New table only: nothing existing
        is rewritten, so there is no data to migrate.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS triage_decisions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " item_id TEXT NOT NULL,"
            " source_tracker TEXT,"
            " issue_num INTEGER,"
            " run_id TEXT,"
            " body_digest TEXT NOT NULL,"
            " backend TEXT NOT NULL,"
            " model TEXT NOT NULL,"
            " prompt_sha TEXT NOT NULL,"
            " type TEXT,"
            " priority TEXT,"
            " complexity TEXT,"
            " confidence REAL,"
            " actionable INTEGER,"
            " needs_info INTEGER,"
            " duplicate_of INTEGER,"
            " outcome TEXT NOT NULL,"
            " decision_json TEXT NOT NULL,"
            " recorded_at TEXT NOT NULL,"
            " resolution TEXT,"
            " resolved_by TEXT,"
            " resolved_at TEXT,"
            " superseded_at TEXT"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_triage_decisions_item"
            " ON triage_decisions(item_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_triage_decisions_open"
            " ON triage_decisions(item_id)"
            " WHERE superseded_at IS NULL AND resolution IS NULL"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 61) WHERE id = 1"
        )

    def _apply_v62(self, cursor: sqlite3.Cursor) -> None:
        """Separate the driver's EXPECTED cadence from its recorded one (#2297).

        ``epic_driver_state.interval_s`` is what the driver itself polls at
        (``epic run --interval``). A one-shot ``kew epic advance`` has no cadence
        of its own, so it records the operator's configured expectation — but
        writing that guess into ``interval_s`` froze it: the first weak value
        won forever, a later ``[epic] driver_expected_interval_seconds`` change
        was inert, and liveness reported a guess as ``cadence_source=recorded``.
        It gets its own nullable column, refreshed by every tick that supplies
        one, and NULL on every pre-v62 row (no expectation was ever recorded
        separately — history is tolerated, not repaired).
        """
        if self._table_exists(cursor, "epic_driver_state"):
            self._try_alter(
                cursor,
                "ALTER TABLE epic_driver_state ADD COLUMN expected_interval_s REAL",
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 62) WHERE id = 1"
        )

    def _apply_v63(self, cursor: sqlite3.Cursor) -> None:
        """Land the driver LOCK's evidence in the store (#2297 review).

        The lock file carries the two facts a heartbeat age cannot supply: the
        pid holding the epic and whether that pid is still a live process, plus
        the heartbeat the holder refreshes mid-tick. Only a reader that can open
        that file ever saw them — ``kew epic status`` — while every serve-side
        reading sits the far side of the import-linter boundary forbidding
        ``kew.service``/``kew.serve`` from importing ``kew.graph``. So a driver
        provably alive mid-long-tick read ``gone`` on the console while the CLI
        read the same driver ``live``.

        The serve sweep (CLI-side, so it may read the lock) snapshots those facts
        here once a cycle, with the instant it looked (``lock_observed_at``) so a
        reader can refuse to settle the pid question off a snapshot taken by a
        serve that has since stopped. Four nullable columns, NULL on every
        pre-v63 row: history is tolerated, not repaired.
        """
        if self._table_exists(cursor, "epic_driver_state"):
            self._try_alter(
                cursor,
                "ALTER TABLE epic_driver_state ADD COLUMN lock_pid INTEGER",
            )
            self._try_alter(
                cursor,
                "ALTER TABLE epic_driver_state ADD COLUMN lock_pid_alive INTEGER",
            )
            self._try_alter(
                cursor,
                "ALTER TABLE epic_driver_state ADD COLUMN lock_heartbeat_at TEXT",
            )
            self._try_alter(
                cursor,
                "ALTER TABLE epic_driver_state ADD COLUMN lock_observed_at TEXT",
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 63) WHERE id = 1"
        )

    def _apply_v64(self, cursor: sqlite3.Cursor) -> None:
        """Add the epic_tick_log tick-attempt table (#2304).

        ``epic_ticks`` only ever held the ticks that CHANGED something, so the
        ticks that failed, were locked out, or found an empty graph reached no
        reader but stderr. Same new-table precedent as _apply_v21 (epic_ticks)
        / _apply_v27 (tracker_events): a plain CREATE TABLE IF NOT EXISTS plus
        a monotonic MAX(version) bump, which races benignly under N concurrent
        openers. Capped per epic by the writer (:data:`EPIC_TICK_LOG_CAP`), not
        by a trigger, so an old DB gains no behaviour it cannot also lose.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_tick_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " epic_ref TEXT NOT NULL,"
            " ts_started TEXT NOT NULL,"
            " ts_finished TEXT,"
            " outcome TEXT,"
            " signal TEXT,"
            " n_dispatched INTEGER NOT NULL DEFAULT 0,"
            " n_merged INTEGER NOT NULL DEFAULT 0,"
            " n_held INTEGER NOT NULL DEFAULT 0,"
            " n_stuck INTEGER NOT NULL DEFAULT 0,"
            " journal_tick_id INTEGER,"
            " error_class TEXT"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_epic_tick_log_ref_id"
            " ON epic_tick_log(epic_ref, id)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 64) WHERE id = 1"
        )

    def _apply_v65(self, cursor: sqlite3.Cursor) -> None:
        """Record whether a run's prompt carried the pre-publish self-check (#2454).

        The round-1 READY rate is the measurement this change exists to move,
        and it cannot be read without knowing which runs were offered the
        self-check. One NOT NULL DEFAULT 0 column: every pre-v65 row is a run
        that genuinely had none, so the default is a fact rather than a
        backfilled guess.
        """
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN self_check INTEGER NOT NULL DEFAULT 0",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 65) WHERE id = 1"
        )

    def _backfill_chain(self) -> None:
        """Walk rows ascending, setting prev_hash = running entry hash.

        Single transaction over the whole table; on a very large audit log
        this holds the write lock for the duration (accepted in v1; chunked
        backfill deferred).
        """
        tbl = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone()
        if tbl is None:
            # audit_events is created in _SCHEMA_V1 before any migration step,
            # so this guard cannot fire in real migrations — it only protects
            # hand-rolled test seeds that bypass the normal schema creation path.
            return
        rows = self._conn.execute(_CHAIN_SELECT_ALL).fetchall()  # nosemgrep
        running = GENESIS_PREV
        for raw in rows:
            chained = replace(_row_to_chain_row(raw), prev_hash=running)
            self._conn.execute(
                "UPDATE audit_events SET prev_hash = ? WHERE id = ?",
                (running, chained.id),
            )
            running = entry_hash(chained)

    def _snapshot_for_migrate(self, dest_path: str) -> None:
        """Copy the current on-disk DB to ``dest_path`` (online-backup API).

        Uses a bare read connection so the snapshot is a consistent,
        WAL-inclusive copy of the committed pre-migration state.
        """
        src = sqlite3.connect(self._db_path)
        try:
            dest = sqlite3.connect(dest_path)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()

    def _backup_before_migrate(self, current_version: int) -> None:
        """Snapshot an older DB to ``<db>.bak-pre-v<N>-<ts>`` before migrating.

        ``N`` is the schema version this build migrates TO
        (:data:`CURRENT_SCHEMA_VERSION`); the backup captures the DB exactly as
        it was before that version's migrations ran. A copy failure aborts the
        migration (the live DB is left untouched) rather than proceeding
        silently — see #688.
        """
        if self._db_path in (":memory:", "") or self._db_path.startswith(
            "file::memory:"
        ):
            return  # in-memory DB — nothing to snapshot
        ts = time.strftime("%Y%m%dT%H%M%S")
        dest_path = f"{self._db_path}.bak-pre-v{CURRENT_SCHEMA_VERSION}-{ts}"
        try:
            self._snapshot_for_migrate(dest_path)
        except Exception as exc:
            self._conn.close()  # don't leak the handle on the abort path
            raise RuntimeError(
                f"failed to back up {self._db_path} before schema migration "
                f"(v{current_version} -> v{CURRENT_SCHEMA_VERSION}); "
                f"migration aborted: {exc}"
            ) from exc

    def _apply_v55(self, cursor: sqlite3.Cursor) -> None:
        """Retain a no-launch marker with every new pre-admission refusal row."""
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN"
            " pre_admission_refusal INTEGER NOT NULL DEFAULT 0",
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 55) WHERE id = 1"
        )

    def _apply_v66(self, cursor: sqlite3.Cursor) -> None:
        """Land WHERE a run ran in the ledger (#2398).

        The fleet cap was always counted off this table, but every row read as
        "on this machine": the only host there was. Once a run can land on a
        Compute Engine instance the count is still right and the *placement* is
        not — and two things need the placement, not the count. The per-host
        capacity check (``[hosts.gcp] max_runs``) has to know which instances
        are full before it places a run on one, and the idle stop has to know
        an instance still has a run on it before it stops the machine out from
        under the agent.

        ``run_host`` is NOT NULL DEFAULT ``'local'``: every pre-v66 row ran on
        the operator machine, which is a fact about history and not a
        placeholder for one. ``run_host_instance`` is nullable because
        ``local`` is one machine and has no instance to name.
        """
        self._try_alter(
            cursor,
            "ALTER TABLE runs ADD COLUMN run_host TEXT NOT NULL DEFAULT 'local'",
        )
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN run_host_instance TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 66) WHERE id = 1"
        )

    def _apply_v67(self, cursor: sqlite3.Cursor) -> None:
        """Record each node's acceptance contract (#2726).

        ``acceptance_contracts`` holds one row per version of a node's
        acceptance test file: path, sha256, the ``acceptance_author`` run that
        wrote it and the commit that carries it. The ``UNIQUE`` key is the
        version identity, and its index backs the current/history reads. A new
        table: no pre-v67 node had a contract, so there is nothing to backfill.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS acceptance_contracts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " epic_ref TEXT NOT NULL,"
            " item_id TEXT NOT NULL,"
            " version INTEGER NOT NULL,"
            " path TEXT NOT NULL,"
            " sha256 TEXT NOT NULL,"
            " author_run_id TEXT NOT NULL,"
            " commit_sha TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " UNIQUE (epic_ref, item_id, version)"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 67) WHERE id = 1"
        )

    def _apply_v68(self, cursor: sqlite3.Cursor) -> None:
        """Bind each acceptance contract to the plan it was written for (#2774).

        ``plan_sha`` is the :func:`kew.triage.plan.plan_sha` of the plan the
        author read, and ``test_names`` the JSON list of the ``test_ac<k>``
        functions that plan required. A redispatch restores a contract only
        while the item's plan still has that digest. Both are nullable: a
        pre-v68 row was written for a plan nobody recorded, so it never
        matches and the next dispatch authors a new contract.
        """
        self._try_alter(
            cursor, "ALTER TABLE acceptance_contracts ADD COLUMN plan_sha TEXT"
        )
        self._try_alter(
            cursor, "ALTER TABLE acceptance_contracts ADD COLUMN test_names TEXT"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 68) WHERE id = 1"
        )

    def _apply_v69(self, cursor: sqlite3.Cursor) -> None:
        """Record the boot each run's owner ran under (#2812).

        ``owner_boot_id`` is ``<machine>/<boot id>``: a run recorded under an
        earlier boot of this machine had an owner that died with that boot, so
        the driver reaps it on its first tick instead of after the liveness
        window. Nullable: a pre-v69 row names no boot and proves nothing.
        """
        self._try_alter(cursor, "ALTER TABLE runs ADD COLUMN owner_boot_id TEXT")
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 69) WHERE id = 1"
        )

    def _apply_v70(self, cursor: sqlite3.Cursor) -> None:
        """Keep each node's merge-state read time out of the journal (#2879).

        ``epic_ticks`` is an immutable change journal, and a node's
        ``merge_state_at`` is not state: hashing it made every quiet tick a
        row, and rewriting the latest row in place would make a row say what a
        later tick saw. This mutable table holds the latest read instead: one
        row per ``(epic_ref, node_id)``, written by every tick GitHub answers,
        and preferred by :meth:`SqliteRunStore.latest_node_pr_facts` over the
        snapshot's stamp while it is about the same state on the same head.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS epic_merge_state_reads ("
            " epic_ref TEXT NOT NULL,"
            " node_id TEXT NOT NULL,"
            " head_oid TEXT,"
            " merge_state TEXT NOT NULL,"
            " merge_state_at TEXT NOT NULL,"
            " PRIMARY KEY (epic_ref, node_id)"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 70) WHERE id = 1"
        )

    def _apply_v71(self, cursor: sqlite3.Cursor) -> None:
        """The project's merge queue (#2814).

        One ``merge_queue`` row per queued PR head, in FIFO order by id, shared
        by every session on the host. The row stores admission-time behavior
        declarations and the base, tree, and commit of a landed merge until
        validation finishes. It also stores a terminal verdict's unpredictable
        comment token and confirmed delivery time. ``owner`` names the pass
        that claimed an entry.
        ``merge_queue_suspects`` holds a repo whose main a partial train left
        unverified, until a pass verifies main again. New tables: nothing to
        backfill.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS merge_queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " repo TEXT NOT NULL,"
            " pr INTEGER NOT NULL,"
            " head TEXT NOT NULL,"
            " session TEXT NOT NULL,"
            " state TEXT NOT NULL,"
            " reason TEXT,"
            " requeues INTEGER NOT NULL DEFAULT 0,"
            " owner TEXT,"
            " behavior_changes TEXT NOT NULL,"
            " expected_base TEXT,"
            " expected_tree TEXT,"
            " landed_sha TEXT,"
            " merge_unknown INTEGER NOT NULL DEFAULT 0,"
            " report_token TEXT,"
            " reported_at TEXT,"
            " enqueued_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS merge_queue_suspects ("
            " repo TEXT PRIMARY KEY,"
            " reason TEXT NOT NULL,"
            " red_on TEXT,"
            " since TEXT NOT NULL"
            ")"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_merge_queue_repo_state"
            " ON merge_queue (repo, state, id)"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 71) WHERE id = 1"
        )

    def _apply_v72(self, cursor: sqlite3.Cursor) -> None:
        """Bind a host-restart retry marker to the run it supersedes (#2969).

        ``superseded_run_ids`` (a JSON array) names the reaped run and every
        run an earlier retry had already superseded, which the overlay then
        treats as superseded whatever the clocks say. ``superseded_before``
        holds that marker's failure-supersession cutoff, so ``retried_at``
        keeps the real operator retry time the review cycle starts from.
        Both nullable: a pre-v72 marker names no run and keeps its
        time-based meaning.
        """
        if self._table_exists(cursor, "epic_retries"):
            self._try_alter(
                cursor, "ALTER TABLE epic_retries ADD COLUMN superseded_run_ids TEXT"
            )
            self._try_alter(
                cursor, "ALTER TABLE epic_retries ADD COLUMN superseded_before TEXT"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 72) WHERE id = 1"
        )

    def _apply_v73(self, cursor: sqlite3.Cursor) -> None:
        """Say why a merge queue is stopped, and keep the failure (#2883).

        ``merge_queue_suspects.red_on`` held both a main a verify found red and
        a commit that landed a tree nobody verified, so a stopped queue could
        only call either "red". ``kind`` tells them apart, and ``failure``
        keeps the latest failed verify of main (or the mismatch) past the
        one-off attention event, for the durable needs-you row and ``kew merge
        queue list``. Nullable and not backfilled: a pre-v73 row's kind is
        unknown, and is said as "not verified green", never as red.
        """
        self._try_alter(cursor, "ALTER TABLE merge_queue_suspects ADD COLUMN kind TEXT")
        self._try_alter(
            cursor, "ALTER TABLE merge_queue_suspects ADD COLUMN failure TEXT"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 73) WHERE id = 1"
        )

    def _apply_v74(self, cursor: sqlite3.Cursor) -> None:
        """Operator prompt profiles: versioned, approved addenda (#3020).

        ``prompt_profile_versions`` holds every addendum an operator proposed
        for an agent role, numbered per role. A row moves ``proposed`` ->
        ``active`` | ``declined``, and ``active`` -> ``retired``; the partial
        unique index lets a role have one ``active`` row at most. ``generation``
        is the role's generation the row's last decision produced, so the
        role's generation is the MAX over its rows (see
        :mod:`kew.run_store_prompt_profiles`). ``runs`` and ``review_rounds``
        name the version they ran under; both columns are nullable, and a
        pre-v74 row ran under no profile.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS prompt_profile_versions ("
            " id INTEGER PRIMARY KEY,"
            " role TEXT NOT NULL,"
            " version INTEGER NOT NULL,"
            " body TEXT NOT NULL,"
            " body_sha256 TEXT NOT NULL,"
            " state TEXT NOT NULL"
            "  CHECK (state IN ('proposed', 'active', 'retired', 'declined')),"
            " proposed_by TEXT NOT NULL,"
            " proposed_at TEXT NOT NULL,"
            " decided_by TEXT,"
            " decided_at TEXT,"
            " audit_event_id INTEGER,"
            " generation INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE (role, version)"
            ")"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_profile_one_active"
            " ON prompt_profile_versions (role) WHERE state = 'active'"
        )
        self._try_alter(
            cursor, "ALTER TABLE runs ADD COLUMN prompt_profile_version_id INTEGER"
        )
        if self._table_exists(cursor, "review_rounds"):
            self._try_alter(
                cursor,
                "ALTER TABLE review_rounds"
                " ADD COLUMN prompt_profile_version_id INTEGER",
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 74) WHERE id = 1"
        )

    def _apply_v75(self, cursor: sqlite3.Cursor) -> None:
        """Record whether a tick attempt inserted its journal row (#2949).

        Every quiet tick after a change carries the change's journal id, so the
        change strip took the FIRST attempt carrying an id as the change. Once
        :data:`EPIC_TICK_LOG_CAP` pruned that attempt, the oldest kept quiet
        tick was drawn as the change instead, moving forward with every prune.
        ``journaled`` is set by the attempt that appended the row. A pre-v75
        row is backfilled the old way (the first kept attempt carrying each
        id), which is the reading it had before this step. A store whose
        history never created the table has no attempt to mark.
        """
        if self._table_exists(cursor, "epic_tick_log"):
            self._try_alter(
                cursor,
                "ALTER TABLE epic_tick_log"
                " ADD COLUMN journaled INTEGER NOT NULL DEFAULT 0",
            )
            cursor.execute(
                "UPDATE epic_tick_log SET journaled = 1 WHERE id IN ("
                " SELECT MIN(id) FROM epic_tick_log"
                " WHERE journal_tick_id IS NOT NULL"
                " GROUP BY epic_ref, journal_tick_id)"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 75) WHERE id = 1"
        )

    def _apply_v76(self, cursor: sqlite3.Cursor) -> None:
        """Merge claims: an operator verify holds a PR head (#3198).

        One row per claimed ``(pr, head)``, owned by the verify (or hand flow)
        that took it, live until ``expires_at`` — see
        :mod:`kew.run_store_merge_claims`. A new table: no existing row changes.
        """
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS merge_claims ("
            " pr INTEGER NOT NULL,"
            " head TEXT NOT NULL,"
            " owner TEXT NOT NULL,"
            " taken_at TEXT NOT NULL,"
            " expires_at TEXT NOT NULL,"
            " PRIMARY KEY (pr, head)"
            ")"
        )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 76) WHERE id = 1"
        )

    def _apply_v77(self, cursor: sqlite3.Cursor) -> None:
        """The audit chain format: item_id and dedup_key enter the hash (#2029).

        Every row already stored reads as the legacy format and keeps its
        hash — the published anchors cover them, so nothing is re-chained.
        Every row written from here on carries ``chain_format = 2`` and hashes
        ``item_id`` and ``dedup_key`` too (``kew.audit_chain.entry_hash``).

        A kew opened before this step never names the column, so its rows
        take the default legacy format. Once a format-2 row is stored such a
        row would be a permanent downgrade break in ``verify_chain``, so the
        trigger refuses it and the stale writer fails instead.
        """
        if self._table_exists(cursor, "audit_events"):
            self._try_alter(
                cursor,
                "ALTER TABLE audit_events"
                " ADD COLUMN chain_format INTEGER NOT NULL DEFAULT 1",
            )
            cursor.execute(
                "CREATE TRIGGER IF NOT EXISTS audit_chain_format_no_downgrade"
                " BEFORE INSERT ON audit_events"
                " WHEN NEW.chain_format = 1 AND EXISTS"
                " (SELECT 1 FROM audit_events WHERE chain_format > 1) BEGIN"
                " SELECT RAISE(ABORT, 'audit_events: a legacy chain format row"
                " after a format-2 row; this kew predates schema v77');"
                " END"
            )
        cursor.execute(
            "UPDATE schema_version SET version = MAX(version, 77) WHERE id = 1"
        )

    def _migrate(self) -> None:
        cursor = self._conn.cursor()
        cursor.executescript(_SCHEMA_V1)
        row = cursor.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        current_version = row["version"] if row else 0
        if current_version > CURRENT_SCHEMA_VERSION:
            self._conn.close()  # don't leak the handle on the reject path
            raise SchemaTooNewError(
                f"dispatch.db is schema v{current_version}; this kew supports up "
                f"to v{CURRENT_SCHEMA_VERSION}. Upgrade kew to open it."
            )
        if 0 < current_version < CURRENT_SCHEMA_VERSION:
            # Snapshot the existing DB before mutating its schema, so a bad
            # (or unexpected) migration is always recoverable — the #688
            # incident auto-migrated the live dispatch.db to a newer version
            # with no backup. A copy failure ABORTS the migration rather than
            # silently proceeding.
            self._backup_before_migrate(current_version)
        steps = [
            self._apply_v1,
            self._apply_v2,
            self._apply_v3,
            self._apply_v4,
            self._apply_v5,
            self._apply_v6,
            self._apply_v7,
            self._apply_v8,
            self._apply_v9,
            self._apply_v10,
            self._apply_v11,
            self._apply_v12,
            self._apply_v13,
            self._apply_v14,
            self._apply_v15,
            self._apply_v16,
            self._apply_v17,
            self._apply_v18,
            self._apply_v19,
            self._apply_v20,
            self._apply_v21,
            self._apply_v22,
            self._apply_v23,
            self._apply_v24,
            self._apply_v25,
            self._apply_v26,
            self._apply_v27,
            self._apply_v28,
            self._apply_v29,
            self._apply_v30,
            self._apply_v31,
            self._apply_v32,
            self._apply_v33,
            self._apply_v34,
            self._apply_v35,
            self._apply_v36,
            self._apply_v37,
            self._apply_v38,
            self._apply_v39,
            self._apply_v40,
            self._apply_v41,
            self._apply_v42,
            self._apply_v43,
            self._apply_v44,
            self._apply_v45,
            self._apply_v46,
            self._apply_v47,
            self._apply_v48,
            self._apply_v49,
            self._apply_v50,
            self._apply_v51,
            self._apply_v52,
            self._apply_v53,
            self._apply_v54,
            self._apply_v55,
            self._apply_v56,
            self._apply_v57,
            self._apply_v58,
            self._apply_v59,
            self._apply_v60,
            self._apply_v61,
            self._apply_v62,
            self._apply_v63,
            self._apply_v64,
            self._apply_v65,
            self._apply_v66,
            self._apply_v67,
            self._apply_v68,
            self._apply_v69,
            self._apply_v70,
            self._apply_v71,
            self._apply_v72,
            self._apply_v73,
            self._apply_v74,
            self._apply_v75,
            self._apply_v76,
            self._apply_v77,
        ]
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for target_version, step in enumerate(steps, start=1):
                if current_version < target_version:
                    step(cursor)
                    current_version = target_version
            if self._bind_ledger_identity:
                self._apply_v36(cursor)
            # Replay additive extensions without changing existing owner identities.
            self._apply_v54(cursor)
            self._apply_v55(cursor)
            self._apply_v56(cursor)
            self._ensure_additive_indexes(cursor)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    @staticmethod
    def _ensure_additive_indexes(cursor: sqlite3.Cursor) -> None:
        """Create indexes that are purely additive over existing columns (I8).

        Run on EVERY open, gated on nothing and bumping no schema version:
        ``CREATE INDEX IF NOT EXISTS`` on an already-present column is
        idempotent and back-compatible, so it needs no versioned migration
        step (a version bump would be a coordinated schema mint — see the
        run_store schema-migration skill). Keeping it out of the ``steps`` list
        means an already-migrated DB still gains the index on next open.

        ``idx_runs_started`` backs the budget/report ``list_since`` window: a
        bare ``started_at`` range had no usable index (``idx_runs_status_started``
        leads with ``status``), so the daily/weekly preflight full-scanned the
        table every dispatch tick. With this index the range is a seek.
        """
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)"
        )
        # ``idx_runs_finished`` backs the dashboard's recent-completions strip
        # (C3/#2296): it keeps five rows, and without this index keeping them
        # meant ordering every finished run in the ledger on every poll. The
        # partial predicate keeps still-running rows out of the index.
        # Synthetic pre-v3 fixtures build a ``runs`` table without the column.
        columns = {
            str(row["name"]) for row in cursor.execute("PRAGMA table_info(runs)")
        }
        if "finished_at" in columns:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_finished"
                " ON runs(finished_at) WHERE finished_at IS NOT NULL"
            )
        # ``idx_runs_pr_merged`` backs the merge-evidence arm of the outcome
        # scorecard's merged-PR read (``run_store_outcomes``, C3/#2301): which
        # week a PR belongs to is decided by when it MERGED, not by when its
        # runs started, and an unindexed range on this column would turn that
        # read from a MULTI-INDEX OR of three seeks into a full ledger scan.
        # Partial, because almost every run row carries no merge timestamp.
        # Synthetic pre-v8 fixtures build a ``runs`` table without the column.
        if "pr_merged_at" in columns:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_pr_merged"
                " ON runs(pr_merged_at) WHERE pr_merged_at IS NOT NULL"
            )
        # The continuation claim readback and the run-id uniqueness check
        # look audit events up by run id; without this each lookup scans the
        # whole audit table. Synthetic pre-v8 fixtures open without the table.
        has_audit = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone()
        if has_audit is not None:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_run_id"
                " ON audit_events(run_id)"
            )
            # Every per-item audit read — the evidence chain, the work screen's
            # attested facts, one item's lifecycle — filters on `item_id` AND
            # `event`, and before this that pair had no index at all:
            # `idx_audit_events_event_ts` leads with `event` and then the
            # timestamp, so the item filter was a scan of every row that event
            # ever had (C3/#2303).
            #
            # `(item_id, event)` and NOT `(event, item_id)`: the latter is a
            # left-prefix match for the `event`-range predicate #2129's
            # prefix read uses, and SQLite then PREFERS it over
            # `idx_audit_events_event_ts` — which serves the same seek AND the
            # `ORDER BY timestamp` for free. The plan loses the ordered read and
            # gains a `USE TEMP B-TREE FOR ORDER BY`, which is the 1s-poll
            # regression #2129 exists to prevent, reintroduced by an index meant
            # to help. Leading with the selective column keeps this index out of
            # that query's way and is the better order for its own reads anyway.
            # `test_run_store.py::TestAuditEventsStore` pins both halves.
            #
            # Synthetic pre-v24 fixtures open without the column.
            audit_columns = {
                str(row["name"])
                for row in cursor.execute("PRAGMA table_info(audit_events)")
            }
            if "item_id" in audit_columns:
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_audit_events_item_event"
                    " ON audit_events(item_id, event)"
                )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_driver_issuance"
                " ON audit_events(id) WHERE event = 'sandbox_resolved'"
                " AND json_valid(payload)"
                " AND json_extract(payload, '$.phase') = 'issuance'"
                " AND json_extract(payload, '$.issued_by') = 'driver'"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_run_key_issuance"
                " ON audit_events(id) WHERE event = 'sandbox_resolved'"
                " AND actor_type = 'policy' AND actor_id = 'credential-issuer'"
                " AND json_valid(payload)"
                " AND json_extract(payload, '$.phase') IN"
                " ('issuance', 'revocation_pending', 'revocation')"
                " AND json_extract(payload, '$.credential_issuer.issuer')"
                " = 'openrouter'"
            )

    def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T:
        """Execute fn() with retry on SQLITE_BUSY errors."""
        for attempt in range(_BUSY_MAX_RETRIES + 1):
            try:
                with self._write_lock:
                    return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == _BUSY_MAX_RETRIES:
                    raise
                with self._write_lock, contextlib.suppress(Exception):
                    self._conn.rollback()
                delay = _BUSY_BASE_DELAY * (2**attempt)
                _logger.debug(
                    "SQLITE_BUSY on %s (attempt %d/%d), retrying in %.3fs",
                    operation,
                    attempt + 1,
                    _BUSY_MAX_RETRIES,
                    delay,
                )
                self.busy_sleep(delay)
        raise RuntimeError("unreachable")

    @staticmethod
    def _run_record_params(record: RunRecord) -> tuple[Any, ...]:
        """The ``_UPSERT_SQL`` bind tuple for *record* — shared by ``save`` and
        the atomic ``try_admit_run`` slot insert so the field order lives once.

        A NULL ``item_id`` is backfilled so a row never persists without a
        distinct fleet-concurrency key. GitHub rows (``issue_num`` > 0) take the
        stringified issue number (matches the v24 backfill). The ``0`` sentinel
        means there is no work item — an ``epic plan`` run (``plan_run.py``) or
        a non-numeric tracker record that failed to set ``item_id`` — so it
        falls back to the unique ``run_id`` rather than the literal ``"0"``,
        which would collapse every such row onto a single live-count slot.
        """
        item_id = record.item_id
        if item_id is None:
            item_id = str(record.issue_num) if record.issue_num != 0 else record.run_id
        return (
            record.run_id,
            record.issue_num,
            record.branch,
            record.status,
            record.exit_code,
            _dt_to_iso(record.started_at),
            _dt_to_iso(record.finished_at),
            record.duration_seconds,
            record.total_tokens,
            record.input_tokens,
            record.output_tokens,
            record.cache_creation_tokens,
            record.cache_read_tokens,
            record.estimated_cost_usd,
            record.error_category,
            record.attempt_number,
            record.session_id,
            str(record.log_file) if record.log_file else None,
            record.pr_url,
            record.runner_name,
            record.model,
            record.title,
            record.complexity,
            record.pgid,
            record.actor_type,
            record.actor_id,
            record.repo,
            record.endpoint,
            record.policy_hash,
            record.reported_model,
            record.pr_state,
            record.pr_merged_at,
            record.review_cycles,
            record.diff_additions,
            record.diff_deletions,
            record.diff_files,
            record.prefix_key,
            record.provider,
            record.container_name,
            item_id,
            record.run_kind,
            record.steps_to_first_edit,
            record.tokens_to_first_edit,
            record.map_tokens,
            record.lesson_corpus_tokens,
            record.planned_cost_usd,
            record.transport_mode,
            sanitize_endpoint(record.transport_endpoint)
            if record.transport_endpoint
            else None,
            record.transport_route,
            int(record.transport_managed),
            record.transport_expected_version,
            record.transport_observed_version,
            record.transport_config_sha256,
            record.transport_preflight_verdict,
            record.resolved_model_source,
            record.selected_tier,
            record.requested_service_tier,
            record.reported_service_tier,
            record.classifier_overhead_model,
            record.classifier_overhead_input_tokens,
            record.classifier_overhead_output_tokens,
            record.classifier_overhead_cost_usd,
            record.switchyard_proxy_observation,
            record.accounting_status,
            record.attempt_pgid,
            record.owner_pid,
            record.attempt_start_time,
            record.owner_start_time,
            int(record.attempt_owner_verified),
            json.dumps(record.termination_members),
            record.container_id,
            record.container_cidfile,
            record.owner_ppid,
            record.owner_command,
            record.container_runtime,
            record.cleanup_runtime,
            record.owner_start_token,
            record.attempt_start_token,
            int(record.pre_admission_refusal),
            int(record.self_check),
            record.run_host or LOCAL_RUN_HOST,
            record.run_host_instance,
            record.owner_boot_id,
            record.prompt_profile_version_id,
        )

    @staticmethod
    def _exclude_key(exclude_item: str | None, exclude_issue: int | None) -> str | None:
        """Resolve the item-keyed exclusion for the worker-cap trio (A3).

        ``exclude_item`` (the durable id) wins; ``exclude_issue`` is the legacy
        int shim — a GitHub issue number stringified to match ``item_id``.
        """
        if exclude_item is not None:
            return exclude_item
        if exclude_issue is not None:
            return str(exclude_issue)
        return None

    def try_admit_run(
        self,
        record: RunRecord,
        cap: int,
        *,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        exclude_run: str | None = None,
        on_admit: Callable[[], None] | None = None,
    ) -> bool:
        """Atomically admit a run under the fleet-wide worker cap (#745).

        Closes the read-then-insert overshoot window the #721 preflight left
        open: two concurrent dispatchers could each read the same live count
        and each admit, pushing the fleet past ``parallel.max_workers`` until
        their ``pending`` rows landed. Here the count and the slot claim are
        one transaction, so a second contender cannot slip between them.

        Under ``BEGIN IMMEDIATE`` (grabs the write lock up front, serializing
        contending writers): count the live runs (same UNION as
        :meth:`live_run_count`) EXCLUDING *exclude_issue*, and only if the
        fleet is below *cap* insert *record*'s ``pending`` row — the row IS the
        worker slot — then COMMIT and return True. Otherwise roll back without
        inserting and return False (the caller requeues). A *cap* of 0 or less
        disables the check: the row is always inserted (returns True).

        Release is free: every terminal dispatch path already flips this row
        off ``pending``, so a finished or failed run vacates its slot with no
        extra bookkeeping.

        *on_admit* folds a second admission gate into this same transaction
        (H2): it runs AFTER the pending row is inserted (so the row is visible
        to reads on this connection and counts toward its own reservation) but
        BEFORE COMMIT, and RAISES to reject. Because ``BEGIN IMMEDIATE``
        serializes contending writers, the reservation a rejected callback sees
        already includes every earlier admit's committed pending row — this is
        how a nearly-exhausted budget cap admits at most one of N racers. A
        raised rejection rolls the insert back and propagates to the caller.

        Owns its rollback — ``_with_busy_retry`` only rolls back on the
        "locked" branch, so any other exception must not leave the write
        transaction open (that would wedge every other writer under WAL).

        This is the identity-less form. Dispatch admission goes through
        :meth:`admit_run_with_identity`, which shares this exact transaction
        (via ``_admit_run_txn``) and adds the run's immutable admission
        identity to it.
        """
        return self._admit_run_txn(
            "try_admit_run",
            record,
            cap,
            exclude_issue=exclude_issue,
            exclude_item=exclude_item,
            exclude_run=exclude_run,
            on_admit=on_admit,
        )

    def _admit_run_txn(
        self,
        operation: str,
        record: RunRecord,
        cap: int,
        *,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        exclude_run: str | None = None,
        on_admit: Callable[[], None] | None = None,
        in_txn: Callable[[], None] | None = None,
    ) -> bool:
        """The shared ``BEGIN IMMEDIATE`` cap-count-then-insert admission body.

        ``in_txn`` runs after the pending row and after *on_admit*, still
        inside the transaction; raising from either rolls the insert back. Both
        admission surfaces (:meth:`try_admit_run` and
        :meth:`admit_run_with_identity`) share this so the cap semantics can
        never diverge between the identity-less and identity-bearing forms.

        ``exclude_run`` names the slot the admitting run was started under — a
        detached review stage's held reservation (#2459) — so neither that
        reservation nor the ``review_stage`` row keyed to it counts against
        the run it started (#2628).
        """
        exclude = self._exclude_key(exclude_item, exclude_issue)
        dead_pending = self._prepare_admission()

        def _do_admit() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if (
                        cap > 0
                        and self._count_live_runs(
                            exclude,
                            dead_pending=dead_pending,
                            exclude_run=exclude_run,
                        )
                        >= cap
                    ):
                        self._conn.rollback()
                        return False
                    self._execute(_UPSERT_SQL, self._run_record_params(record))
                    if on_admit is not None:
                        on_admit()
                    if in_txn is not None:
                        in_txn()
                    self._conn.commit()
                    return True
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry(operation, _do_admit)

    def _prepare_admission(self) -> list[DeadPending]:
        """Probe and terminate outside the admission transaction."""
        return prepare_admission(self)

    def sweep_dead_pending(self) -> int:
        """Finalize every ``pending`` row whose owner is PROVABLY dead.

        The sweep admission runs on its way in, offered on its own to a caller
        that decides before admission — a strict placement refuses there, so a
        crashed run's row would otherwise hold its instance for good, the sweep
        that frees it never reached (#2627). Same proof, so a live or
        unprovable row survives. Returns how many rows it finalized.
        """
        return len(self._prepare_admission())

    def settle_never_started(
        self, record: RunRecord, *, actor: tuple[str, str]
    ) -> NeverStartedReap:
        """Reap *record* by hand under the never-started rule (``kew settle``).

        The same proof the reconcilers use, so an owner whose liveness is
        unknown is refused rather than reaped. The result says whether this
        call reaped the run, why not, and any container it removed.
        """
        return reap_dead_never_started(
            self, reservation_generation(self, record), actor=actor
        )

    def _count_live_runs(
        self,
        exclude_item: str | None,
        liveness: Callable[[RunRecord], bool | None] | None = None,
        *,
        dead_pending: list[DeadPending] | None = None,
        exclude_run: str | None = None,
    ) -> int:
        """Live fleet concurrency: every ``pending`` run, plus each active epic
        reservation whose item has no ``pending`` run yet (see
        :meth:`live_run_count`). Factored out so :meth:`try_admit_run` can reuse
        it inside its transaction.

        The cap counts RUNS (#2628): two pending runs of one item hold two
        slots — except a detached review, whose ``review_stage`` row and the
        run its child started under it are one piece of work and hold one
        (:func:`~kew.run_store_liveness.stage_shadowed`, #2697).
        ``exclude_item`` drops only that item's own reservation — the slot
        reserved for the run being admitted — never a pending run, so a
        second run of an item already running cannot admit under it.
        ``exclude_run`` (admission only) drops the pending row and reservation
        with that run id: the held slot a run was started under (#2459).

        Reservations are matched to runs on ``item_id`` (A3), NOT
        ``issue_num`` — every non-numeric item shares the ``0`` sentinel, so
        matching on ``issue_num`` would let one Linear item's pending run hide
        every other Linear reservation. Reservations carry their own
        ``item_id`` since L3, so their side is read directly (no CAST) and
        compared against ``runs.item_id``. Pending rows fall
        back to ``issue_num`` when ``item_id`` is NULL (rows written by A2-only
        code).

        ``liveness`` (#1059) makes the count owner-liveness-aware: a killed/
        crashed run finalizes its ``pending`` row only through an in-process
        ``finally``, so a hard-killed owner (breaker process-group kill, driver
        restart, crash) orphans the row as ``pending`` forever — and the plain
        count would let that dead orphan permanently steal a fleet worker slot.
        When a probe is supplied, an item is dropped from the count only when
        EVERY ``pending`` run backing it is *provably* dead (probe -> ``False``,
        never ``None``): a mid-startup or unprobeable run stays counted
        (concurrent-state invariant — prove dead, never infer from state-shape),
        so the cap never frees a slot a live run still holds. A reservation with
        no run row (a claim written before its child's ``pending`` row) is never
        probeable and always counts. The default (``None``) keeps the pure-SQL
        fast path the in-transaction cap admissions use — no per-row probing
        inside a ``BEGIN IMMEDIATE``.
        """
        if liveness is not None:
            return self._count_live_runs_liveness_aware(exclude_item, liveness)
        if dead_pending:
            return count_admission_runs(
                self, exclude_item, dead_pending, exclude_run=exclude_run
            )
        # A review stage whose child's run is pending is that run's slot (#2697).
        row = self._execute(
            "SELECT (SELECT COUNT(*) FROM runs WHERE status = 'pending'"  # nosec B608
            " AND run_id IS NOT ? AND NOT " + stage_shadowed("runs", "runs") + ")"
            " + (SELECT COUNT(DISTINCT reservation.item_id)"
            " FROM epic_reservations AS reservation"
            " WHERE (? IS NULL OR reservation.item_id != ?)"
            " AND reservation.run_id IS NOT ?"
            " AND NOT EXISTS (SELECT 1 FROM runs WHERE status = 'pending'"
            " AND COALESCE(runs.item_id, CAST(runs.issue_num AS TEXT))"
            " = reservation.item_id))",
            (exclude_run, exclude_item, exclude_item, exclude_run),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _dead_pending_item_ids(
        self, liveness: Callable[[RunRecord], bool | None]
    ) -> tuple[set[str], set[str]]:
        """Split every ``pending`` run's work item into (seen, provably dead).

        An item is dead only when NONE of its pending runs is (possibly) alive —
        the concurrent-state invariant is prove dead, never infer from state
        shape, so an undeterminable probe (``None``) or a container whose client
        is not provably gone keeps the item live. Shared by every fleet-capacity
        read that has a probe in hand (:meth:`_count_live_runs_liveness_aware`,
        :meth:`live_reservation_counts_by_epic`) so they cannot drift.
        """
        alive_ids: set[str] = set()
        seen_ids: set[str] = set()
        for raw in self._execute("SELECT * FROM runs WHERE status = 'pending'"):
            record = _row_to_record(raw)
            live_id = record.item_id or str(record.issue_num)
            seen_ids.add(live_id)
            if liveness(record) is not False or (
                record.container_name
                and not stop_recovery.contained_client_is_dead(record)
            ):
                alive_ids.add(live_id)
        return seen_ids, seen_ids - alive_ids

    def _count_live_runs_liveness_aware(
        self,
        exclude_item: str | None,
        liveness: Callable[[RunRecord], bool | None],
    ) -> int:
        """Owner-liveness-aware live count (#1059; see :meth:`_count_live_runs`).

        Resolves each ``pending`` row's live_id and marks an item dead only when
        NONE of its pending runs is (possibly) alive — the dead item is then
        dropped from BOTH the runs and reservation sides so a hard-killed orphan
        stops consuming a fleet worker slot. Every pending run of a live item
        counts on its own (#2628), except a review stage whose child's run is
        pending (#2697). A reservation with no run row is unprobeable
        and always counts.

        Host probes use ``pgid`` for the owner. ``attempt_pgid`` never frees a slot.
        """
        seen_ids, dead_ids = self._dead_pending_item_ids(liveness)
        live_runs = sum(
            1
            for raw in self._execute(
                "SELECT COALESCE(item_id, CAST(issue_num AS TEXT)) FROM runs"  # nosec B608
                " WHERE status = 'pending' AND NOT " + stage_shadowed("runs", "runs")
            )
            if raw[0] not in dead_ids
        )
        reserved: set[str] = set()
        for res in self._execute("SELECT item_id FROM epic_reservations").fetchall():
            res_id = res["item_id"]
            if res_id is not None and res_id not in seen_ids:
                reserved.add(res_id)
        if exclude_item is not None:
            reserved.discard(exclude_item)
        return live_runs + len(reserved)

    def live_run_count(
        self,
        *,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> int:
        """Fleet-wide live concurrency: every pending run plus each active epic
        reservation not yet backed by one (#721; runs, not items, since #2628).

        A run is "live" from the moment its ``pending`` row is written until it
        finishes; an epic-DAG node is reserved BEFORE its child writes that row
        (see ``reserve_epic_node``). The two sources overlap — a reserved node
        that has already written its pending run appears in both — so they are
        deduped by ``item_id`` — a reservation counts only while its item has
        no pending run — so a reserved node counts once, never twice.

        This is the cheap store-backed signal the dispatch preflight uses to
        enforce ``parallel.max_workers`` across every process (fleet-wide),
        not per-process. ``exclude_item`` (or the legacy int ``exclude_issue``
        shim) drops the reservation of the item being dispatched so a run never
        counts its own reserved slot; another pending run of that item still
        counts (#2628).

        ``liveness`` (#1059) drops a ``pending`` row whose owning process/
        container is *provably* dead so one crashed/killed run can never
        permanently shrink the fleet below ``max_workers - live_real``; see
        :meth:`_count_live_runs`. Omit it for the pure-SQL fast path.
        """
        return self._count_live_runs(
            self._exclude_key(exclude_item, exclude_issue), liveness
        )

    def save_loop(self, record: LoopRecord) -> None:
        def _do_save() -> None:
            self._execute(
                _LOOP_UPSERT_SQL,
                (
                    record.loop_id,
                    record.source,
                    record.issue_num,
                    record.repo,
                    record.goal_text,
                    record.runner_name,
                    record.status,
                    record.status_reason,
                    record.budget_usd,
                    record.wall_clock_secs,
                    record.max_steps,
                    record.spent_usd,
                    record.step_count,
                    _dt_to_iso(record.created_at),
                    _dt_to_iso(record.updated_at),
                    _dt_to_iso(record.finished_at),
                    int(record.pause_requested),
                    # Stored as-is: unlike runs (where item_id is the live-count
                    # key and GitHub callers pass None), loops carry no such
                    # constraint and delegate.py sets item_id explicitly.
                    record.item_id,
                    record.model,
                    record.complexity,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("save_loop", _do_save)

    def get_loop(self, loop_id: str) -> LoopRecord | None:
        row = self._conn.execute(
            "SELECT * FROM loops WHERE loop_id = ?", (loop_id,)
        ).fetchone()
        return _row_to_loop(row) if row is not None else None

    def list_loops(self, status: str | None = None) -> list[LoopRecord]:
        # Secondary sort on loop_id keeps ordering deterministic when two loops
        # share a created_at.
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM loops ORDER BY created_at, loop_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM loops WHERE status = ? ORDER BY created_at, loop_id",
                (status,),
            ).fetchall()
        return [_row_to_loop(row) for row in rows]

    def request_pause(self, loop_id: str) -> bool:
        """Set pause_requested on a PURSUING loop. Atomic — returns True iff a
        pursuing loop with this id was flagged (False = unknown or not running)."""

        def _do() -> bool:
            cur = self._conn.execute(
                "UPDATE loops SET pause_requested = 1 "
                "WHERE loop_id = ? AND status = 'pursuing'",
                (loop_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

        return self._with_busy_retry("request_pause", _do)

    def clear_pause(self, loop_id: str) -> None:
        def _do() -> None:
            self._conn.execute(
                "UPDATE loops SET pause_requested = 0 WHERE loop_id = ?", (loop_id,)
            )
            self._conn.commit()

        self._with_busy_retry("clear_pause", _do)

    def add_iteration(self, record: IterationRecord) -> None:
        def _do_add() -> None:
            self._execute(
                "INSERT INTO iterations (iteration_id, loop_id, idx, run_id,"
                " kind, status, cost_usd, tokens, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.iteration_id,
                    record.loop_id,
                    record.idx,
                    record.run_id,
                    record.kind,
                    record.status,
                    record.cost_usd,
                    record.tokens,
                    _dt_to_iso(record.started_at),
                    _dt_to_iso(record.finished_at),
                ),
            )
            self._conn.commit()

        self._with_busy_retry("add_iteration", _do_add)

    def list_iterations(self, loop_id: str) -> list[IterationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM iterations WHERE loop_id = ? ORDER BY idx",
            (loop_id,),
        ).fetchall()
        return [_row_to_iteration(row) for row in rows]

    def list_iterations_tail(
        self, loop_id: str, *, limit: int = 200
    ) -> list[IterationRecord]:
        """The loop's newest ``limit`` iterations, oldest-first in the page.

        ``list_iterations`` reads every iteration a loop ever ran; the strip
        surfaces want the tail, and a long-lived loop has thousands (#2304).
        Bounded in SQL on ``idx``, which is the loop's own monotonic counter.
        """
        if limit < 1:
            raise ValueError("iteration tail limit must be >= 1")
        rows = self._conn.execute(
            "SELECT * FROM iterations WHERE loop_id = ? ORDER BY idx DESC LIMIT ?",
            (loop_id, limit),
        ).fetchall()
        return [_row_to_iteration(row) for row in reversed(rows)]

    def last_iteration_at_by_loop(self) -> dict[str, str]:
        """loop_id -> its newest iteration stamp, for every loop with one.

        ``add_iteration`` is the write a working loop makes between its session
        start and its terminal — and it does NOT touch ``loops.updated_at`` — so
        this is the freshest evidence a loop is moving (#2297). One grouped
        query, not one per loop: the loops page reads every row at once.
        """
        rows = self._conn.execute(
            "SELECT loop_id, MAX(COALESCE(finished_at, started_at)) AS last_at"
            " FROM iterations GROUP BY loop_id"
        ).fetchall()
        return {
            str(row["loop_id"]): str(row["last_at"])
            for row in rows
            if row["last_at"] is not None
        }

    def set_pgid(self, run_id: str, pgid: int) -> None:
        """Record the owning dispatch's process group on an existing run."""

        def _do_set() -> None:
            self._execute("UPDATE runs SET pgid = ? WHERE run_id = ?", (pgid, run_id))
            self._conn.commit()

        self._with_busy_retry("set_pgid", _do_set)

    def set_container_identity(
        self,
        run_id: str,
        name: str,
        container_id: str | None,
        cidfile: str | None = None,
        *,
        runtime: str | None = None,
    ) -> None:
        """Preserve the creation intent before creation, then its immutable target.

        ``runtime="openshell"`` records a gateway sandbox id instead, under the
        gateway's own shape check; every other caller keeps the 64-hex rule.
        """
        from kew.runtime_names import OPENSHELL_RUNTIME

        valid = (
            stop_recovery.valid_gateway_sandbox_id
            if runtime == OPENSHELL_RUNTIME
            else stop_recovery.valid_container_id
        )
        if container_id is not None and not valid(container_id):
            raise ValueError("container identity must be a full container id")

        def write() -> None:
            with self._txn_lock:
                self._execute(
                    "UPDATE runs SET container_name = ?, container_id = ?,"
                    " container_cidfile = COALESCE(?, container_cidfile)"
                    " WHERE run_id = ?",
                    (name, container_id, cidfile, run_id),
                )
                self._conn.commit()

        self._with_busy_retry("set_container_identity", write)

    def set_attempt_pgid(self, run_id: str, pgid: int) -> None:
        """Record the waiting leader and its birth without changing its owner."""
        token = process_start_token(pgid)
        birth = process_start_time(pgid)
        record = self.get(run_id)
        verified = _attempt_owner_verified(record, pgid, birth, token)
        if not verified:
            reason = (
                "owner-identity-unavailable"
                if record is None or record.owner_start_token is None
                else "attempt-identity-unavailable"
            )
            _logger.warning("%s for %s: attempt group %s", reason, run_id, pgid)

        def _do_set() -> None:
            self._execute(
                "UPDATE runs SET attempt_pgid = ?, attempt_start_time = ?,"
                " attempt_owner_verified = ?, attempt_start_token = ?"
                " WHERE run_id = ?",
                (
                    pgid,
                    birth,
                    int(verified),
                    token,
                    run_id,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("set_attempt_pgid", _do_set)

    def retain_termination_members(
        self, run_id: str, pgid: int, members: dict[int, str]
    ) -> None:
        """Merge authenticated births without overwriting concurrent stop intent."""

        def _do_retain() -> None:
            self._execute(
                "UPDATE runs SET termination_members ="
                " json_patch(termination_members, ?)"
                " WHERE run_id = ? AND status = 'pending'",
                (json.dumps({pgid: members}), run_id),
            )
            self._conn.commit()

        self._with_busy_retry("retain_termination_members", _do_retain)

    def request_run_stop(
        self,
        run_id: str,
        *,
        label_reset: dict[str, str] | None = None,
        reset_labels: LabelReset | None = None,
    ) -> None:
        """Persist label intent before signalling or terminalizing the row."""
        if reset_labels is not None:
            self._stop_label_reset = reset_labels
        if label_reset is not None:
            self._request_stop_label_reset(run_id, label_reset)

        def _do_stop() -> None:
            self._execute(
                "UPDATE runs SET error_category = 'killed'"
                " WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            )
            self._conn.commit()

        self._with_busy_retry("request_run_stop", _do_stop)

    def reconcile_host_stops(self) -> int:
        """Recover stopped capacity after independent owner and attempt proofs."""
        self.retry_stop_label_resets()
        with self._txn_lock:
            records = self._execute(
                "SELECT * FROM runs WHERE status = 'pending'"
                " AND error_category = 'killed' AND container_name IS NULL"
            ).fetchall()
        reconciled = 0
        for raw in records:
            record = _row_to_record(raw)
            if not host_stop_is_dead(
                record.pgid,
                record.owner_pid,
                record.attempt_pgid,
                record.log_file,
                record.owner_start_time,
                record.owner_ppid,
                record.owner_command,
                record.owner_start_token,
            ):
                continue
            now = datetime.now(UTC)
            if not self.finalize_orphaned_pending(
                record.run_id,
                error_category="killed",
                finished_at=now,
                expected_owner=record,
            ):
                continue
            reconciled += 1
            self.append_audit_event(
                timestamp=now.isoformat(),
                event="host_stop_reconciled",
                run_id=record.run_id,
                issue_num=record.issue_num,
                payload={
                    "reason": "Fleet stop completed: owner and attempt groups are dead"
                },
            )
        return reconciled + self._reconcile_contained_stops()

    def reconcile_dead_pending(self) -> int:
        """Prove orphan deaths before admission; keep the locked count pure SQL."""
        reconciled = 0
        with self._txn_lock:
            pending = self.list_pending()
        for record in pending:
            now = datetime.now(UTC)
            if not stop_recovery.pending_owner_is_dead(
                record,
                now,
                remove_stopped=stopped_container_remover(self),
                daemon=recorded_daemon(self, record),
            ):
                continue
            if not self.finalize_orphaned_pending(
                record.run_id,
                error_category="orphaned_pending_reaped",
                finished_at=now,
                expected_owner=record,
            ):
                continue
            reconciled += 1
            self.append_audit_event(
                timestamp=now.isoformat(),
                event="orphaned_run_reaped",
                run_id=record.run_id,
                issue_num=record.issue_num,
                payload={
                    "reason": "Owner and attempt deaths reconciled before admission"
                },
            )
        return reconciled

    def _reconcile_contained_stops(self) -> int:
        """Recover stopped containers using the runtime persisted by fleet stop."""
        reconciled = 0
        with self._txn_lock:
            pending = self.list_pending()
        for record in pending:
            if not record.container_name or record.error_category != "killed":
                continue
            with self._txn_lock:
                tasks = self.list_audit_events(
                    event="stop_label_reset_requested", run_id=record.run_id
                )
            if not tasks:
                continue
            payload = tasks[0].payload or {}
            runtime = str(payload.get("runtime", ""))
            now = datetime.now(UTC)
            reason = stop_recovery.contained_stop_reason(
                record,
                now,
                partial(stop_recovery.record_container_state, runtime, record),
            )
            if reason and self.finalize_orphaned_pending(
                record.run_id,
                error_category="killed",
                finished_at=now,
                expected_owner=record,
            ):
                reconciled += 1
                self.append_audit_event(
                    timestamp=now.isoformat(),
                    event="contained_stop_reconciled",
                    run_id=record.run_id,
                    issue_num=record.issue_num,
                    payload={"reason": reason},
                )
        return reconciled

    def set_outcome(
        self,
        run_id: str,
        *,
        pr_url: str | None,
        pr_state: str | None,
        pr_merged_at: str | None,
        review_cycles: int | None,
        diff_additions: int | None,
        diff_deletions: int | None,
        diff_files: int | None,
    ) -> None:
        """Persist a run's PR outcome fields (touches only the outcome columns)."""

        def _do_set() -> None:
            # Terminal-state guard: only write when the run is not already in a
            # terminal PR state. A run's PR state only ever advances
            # (None/ABSENT -> OPEN -> MERGED/CLOSED), so this makes the UPDATE atomic
            # against a stale concurrent sync that fetched an older OPEN snapshot
            # and would otherwise downgrade a newer MERGED/CLOSED outcome.
            self._execute(
                "UPDATE runs SET pr_url = ?, pr_state = ?, pr_merged_at = ?,"
                " review_cycles = ?, diff_additions = ?, diff_deletions = ?,"
                " diff_files = ? WHERE run_id = ?"
                " AND (pr_state IS NULL OR pr_state IN ('OPEN', 'ABSENT'))",
                (
                    pr_url,
                    pr_state,
                    pr_merged_at,
                    review_cycles,
                    diff_additions,
                    diff_deletions,
                    diff_files,
                    run_id,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("set_outcome", _do_set)

    def list_syncable_runs(
        self, *, item_ids: Sequence[str] | None = None
    ) -> list[RunRecord]:
        """Runs whose PR outcome is not yet terminal (None, ABSENT, or OPEN) and worth a
        GitHub poll.

        Includes every successful run, plus any non-success run on a branch that
        has *no* successful sibling — an "orphan failure" whose PR may still have
        merged out-of-band (e.g. a rescued run that failed its own gate; #611).
        A failure superseded by a later success on the same branch is excluded:
        that branch's PR is already carried by the successful run.

        ``item_ids`` narrows the result to those work items, resolved the way
        every reader resolves a record's item key: the durable ``item_id`` when
        it has one, else the issue number. It is a scope, not a second rule —
        the syncability predicate above is unchanged, and the orphan-failure
        subquery still sees every sibling on the branch. A caller that wants a
        handful of items (an epic's nodes on a 1s poll) must not pay to
        materialize every open PR on the host (#2096).
        """
        scope = None if item_ids is None else list(dict.fromkeys(item_ids))
        if scope == []:
            return []
        # Only ``?`` placeholders are interpolated; every id is a bound value.
        # ``NULLIF`` before ``COALESCE`` so a blank item_id falls through to the
        # issue number exactly as ``rec_item_id`` does for the same record.
        narrow = (
            ""
            if scope is None
            else " AND COALESCE(NULLIF(item_id, ''), CAST(issue_num AS TEXT))"
            f" IN ({','.join('?' * len(scope))})"
        )
        sql = (
            "SELECT * FROM runs WHERE"  # nosec B608 — placeholders are ? params
            " (pr_state IS NULL OR pr_state IN ('OPEN', 'ABSENT'))"
            " AND branch <> '' AND ("
            "  status = 'success'"
            "  OR NOT EXISTS ("
            "    SELECT 1 FROM runs s"
            "    WHERE s.branch = runs.branch AND s.status = 'success'"
            "  )"
            ")" + narrow
        )

        def _do() -> list[RunRecord]:
            cur = self._execute(sql, None if scope is None else tuple(scope))
            return [_row_to_record(row) for row in cur.fetchall()]

        return self._with_busy_retry("list_syncable_runs", _do)

    def set_fleet_stopped(self, reason: str = "") -> None:
        """Engage the fleet-stop latch (idempotent upsert)."""
        value = json.dumps(
            {"stopped_at": datetime.now(UTC).isoformat(), "reason": reason}
        )

        def _do_set() -> None:
            self._execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_FLEET_STOPPED_KEY, value),
            )
            self._conn.commit()

        self._with_busy_retry("set_fleet_stopped", _do_set)

    def clear_fleet_stopped(self) -> None:
        """Clear the fleet-stop latch. No-op if not set."""

        def _do_clear() -> None:
            self._execute("DELETE FROM meta WHERE key = ?", (_FLEET_STOPPED_KEY,))
            self._conn.commit()

        self._with_busy_retry("clear_fleet_stopped", _do_clear)

    def get_fleet_stopped(self) -> dict[str, str] | None:
        """Return {'stopped_at', 'reason'} when latched, else None."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_FLEET_STOPPED_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return {
            "stopped_at": str(data.get("stopped_at", "")),
            "reason": str(data.get("reason", "")),
        }

    def record_policy_snapshot(
        self, policy_hash: str, snapshot_json: str, first_seen: str
    ) -> None:
        """Retain a policy-snapshot preimage, store-once (idempotent)."""

        def _do_record() -> None:
            self._execute(
                "INSERT OR IGNORE INTO policy_snapshots"
                " (policy_hash, snapshot_json, first_seen) VALUES (?, ?, ?)",
                (policy_hash, snapshot_json, first_seen),
            )
            self._conn.commit()

        self._with_busy_retry("record_policy_snapshot", _do_record)

    def get_policy_snapshot(self, policy_hash: str) -> tuple[str, str] | None:
        """Return (snapshot_json, first_seen) for a retained hash, else None."""
        row = self._conn.execute(
            "SELECT snapshot_json, first_seen FROM policy_snapshots"
            " WHERE policy_hash = ?",
            (policy_hash,),
        ).fetchone()
        if row is None:
            return None
        return row["snapshot_json"], row["first_seen"]

    def record_interactive_usage(self, usage: InteractiveUsage) -> bool:
        """Idempotent insert keyed by dedup_key. True iff a row was written."""

        def _do_record() -> bool:
            cursor = self._execute(
                "INSERT OR IGNORE INTO interactive_usage ("
                " runner, model, observed_at, input_tokens, output_tokens,"
                " cache_creation_tokens, cache_read_tokens, cost_usd,"
                " session_id, ingest_source, ingested_at, dedup_key,"
                " prefix_key, provider, query_source, agent_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    usage.runner,
                    usage.model,
                    usage.observed_at,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_creation_tokens,
                    usage.cache_read_tokens,
                    usage.cost_usd,
                    usage.session_id,
                    usage.ingest_source,
                    datetime.now(UTC).isoformat(),
                    usage.dedup_key,
                    usage.prefix_key,
                    usage.provider,
                    usage.query_source,
                    usage.agent_name,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("record_interactive_usage", _do_record)

    def list_interactive_usage_since(self, since: datetime) -> list[InteractiveUsage]:
        """Interactive-usage rows with observed_at >= since, oldest first."""
        since_iso = _dt_to_iso(since)
        rows = self._conn.execute(
            "SELECT runner, model, observed_at, input_tokens, output_tokens,"
            " cache_creation_tokens, cache_read_tokens, cost_usd, session_id,"
            " ingest_source, dedup_key, prefix_key, provider,"
            " query_source, agent_name"
            " FROM interactive_usage"
            " WHERE observed_at >= ? ORDER BY observed_at",
            (since_iso,),
        ).fetchall()
        keys = rows[0].keys() if rows else []
        return [
            InteractiveUsage(
                runner=r["runner"],
                model=r["model"],
                observed_at=r["observed_at"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cache_creation_tokens=r["cache_creation_tokens"],
                cache_read_tokens=r["cache_read_tokens"],
                cost_usd=r["cost_usd"],
                session_id=r["session_id"],
                ingest_source=r["ingest_source"],
                dedup_key=r["dedup_key"],
                prefix_key=_col(r, keys, "prefix_key"),
                provider=_col(r, keys, "provider"),
                query_source=_col(r, keys, "query_source"),
                agent_name=_col(r, keys, "agent_name"),
            )
            for r in rows
        ]

    def record_tracker_event(self, event: TrackerEvent) -> bool:
        """Idempotent insert keyed by delivery_id. True iff a row was written.

        A redelivered webhook (same delivery_id) is a no-op and returns False.
        """

        def _do_record() -> bool:
            cursor = self._execute(
                "INSERT OR IGNORE INTO tracker_events ("
                " delivery_id, source, event_type, action, entity_id,"
                " webhook_timestamp, payload, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.delivery_id,
                    event.source,
                    event.event_type,
                    event.action,
                    event.entity_id,
                    event.webhook_timestamp,
                    event.payload,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("record_tracker_event", _do_record)

    def list_tracker_events_since(self, since: datetime) -> list[TrackerEvent]:
        """Tracker-event rows with received_at >= since, oldest first."""
        since_iso = _dt_to_iso(since)
        rows = self._conn.execute(
            "SELECT delivery_id, source, event_type, action, entity_id,"
            " webhook_timestamp, payload, received_at"
            " FROM tracker_events"
            " WHERE received_at >= ? ORDER BY received_at",
            (since_iso,),
        ).fetchall()
        return [
            TrackerEvent(
                delivery_id=r["delivery_id"],
                source=r["source"],
                event_type=r["event_type"],
                action=r["action"],
                entity_id=r["entity_id"],
                webhook_timestamp=r["webhook_timestamp"],
                payload=r["payload"],
                received_at=r["received_at"],
            )
            for r in rows
        ]

    def has_tracker_events_since(
        self, since: datetime, *, source: str | None = None
    ) -> bool:
        """True iff a tracker_events row has received_at >= since.

        Cheap existence probe (LIMIT 1) for the reconcile short-circuit (W2):
        the driver / `kew epic status` ask "did anything change since the last
        fetch?" without materializing the rows. ``source`` scopes to one
        tracker (e.g. "linear"); ``None`` matches any. Passing an epoch-old
        ``since`` answers the cold-path question "have webhooks ever delivered?"
        """
        since_iso = _dt_to_iso(since)
        if source is None:
            row = self._conn.execute(
                "SELECT 1 FROM tracker_events WHERE received_at >= ? LIMIT 1",
                (since_iso,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM tracker_events"
                " WHERE received_at >= ? AND source = ? LIMIT 1",
                (since_iso, source),
            ).fetchone()
        return row is not None

    def get_epic_graph_cache(self, epic_ref: str) -> tuple[datetime, str] | None:
        """The cached (fetched_at, graph_json) for ``epic_ref``, or None.

        Read side of the reconcile short-circuit (W2). ``fetched_at`` is the
        instant the cached graph was fetched from the provider; ``graph_json``
        is the adapter-serialized graph replayed when the fetch is skipped.
        """
        row = self._conn.execute(
            "SELECT fetched_at, graph_json FROM epic_graph_cache WHERE epic_ref = ?",
            (epic_ref,),
        ).fetchone()
        if row is None:
            return None
        fetched_at = _iso_to_dt(row["fetched_at"])
        assert fetched_at is not None  # NOT NULL column
        return fetched_at, row["graph_json"]

    def put_epic_graph_cache(
        self, epic_ref: str, source: str, fetched_at: datetime, graph_json: str
    ) -> None:
        """Upsert the cached provider graph for ``epic_ref`` (W2)."""

        def _do_put() -> None:
            self._execute(
                "INSERT OR REPLACE INTO epic_graph_cache"
                " (epic_ref, source, fetched_at, graph_json)"
                " VALUES (?, ?, ?, ?)",
                (epic_ref, source, _dt_to_iso(fetched_at), graph_json),
            )
            self._conn.commit()

        self._with_busy_retry("put_epic_graph_cache", _do_put)

    def warm_prefixes(self, provider: str, now: datetime) -> set[str]:
        """Distinct prefix_keys with cache hits within the provider's TTL.

        Returns prefix_key values from runs where provider matches,
        started_at > now - ttl, and cache_read_tokens > 0. Pure read.
        """
        from kew.pricing import get_provider_cache_params

        params = get_provider_cache_params(provider)
        if not params.caches or params.ttl_seconds <= 0:
            return set()
        cutoff = now - timedelta(seconds=params.ttl_seconds)
        cutoff_iso = _dt_to_iso(cutoff)
        rows = self._conn.execute(
            "SELECT DISTINCT prefix_key FROM runs"
            " WHERE provider = ? AND started_at > ?"
            " AND (cache_read_tokens > 0 OR cache_creation_tokens > 0)"
            " AND prefix_key IS NOT NULL",
            (provider, cutoff_iso),
        ).fetchall()
        return {r["prefix_key"] for r in rows}

    def list_driver_key_issuances(
        self, *, after_id: int = 0, limit: int = 100
    ) -> list[AuditEvent]:
        """Seek one bounded page through the partial driver-issuance index."""
        rows = self._conn.execute(
            "SELECT * FROM audit_events INDEXED BY idx_audit_driver_issuance"
            " WHERE id > ?"
            " AND event = 'sandbox_resolved'"
            " AND json_valid(payload)"
            " AND json_extract(payload, '$.phase') = 'issuance'"
            " AND json_extract(payload, '$.issued_by') = 'driver'"
            " ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [_row_to_audit_event(row) for row in rows]

    def head_audit_verdicts(
        self, *, event: str, run_id: str, actor: tuple[str, str], head_sha: str
    ) -> frozenset[str]:
        """Every distinct ``verdict`` one actor recorded for one run and head.

        Not a newest-N window: later events for other heads of the run never
        push an older verdict for this head out of view.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT json_extract(payload, '$.verdict') AS verdict"
            " FROM audit_events WHERE run_id = ? AND event = ?"
            " AND actor_type = ? AND actor_id = ? AND json_valid(payload)"
            " AND json_extract(payload, '$.head_sha') = ?",
            (run_id, event, actor[0], actor[1], head_sha),
        ).fetchall()
        return frozenset(
            row["verdict"] for row in rows if isinstance(row["verdict"], str)
        )

    def list_run_key_issuances(
        self, *, after_id: int = 0, limit: int = 100
    ) -> list[AuditEvent]:
        """Seek lifecycle evidence for both issuance sources, including old failures."""
        rows = self._conn.execute(
            "SELECT * FROM audit_events INDEXED BY idx_audit_run_key_issuance"
            " WHERE id > ? AND event = 'sandbox_resolved'"
            " AND actor_type = 'policy' AND actor_id = 'credential-issuer'"
            " AND json_valid(payload)"
            " AND json_extract(payload, '$.phase') IN"
            " ('issuance', 'revocation_pending', 'revocation')"
            " AND json_extract(payload, '$.credential_issuer.issuer')"
            " = 'openrouter'"
            " ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
        return [_row_to_audit_event(row) for row in rows]

    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        event_prefix: str | None = None,
        event_prefixes: Sequence[str] | None = None,
        item_id: str | None = None,
        item_ids: Sequence[str] | None = None,
        item_or_issue: str | None = None,
        epic_ref: str | None = None,
        epic_node_ids: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        actor_type: str | None = None,
        issue_num: int | None = None,
        issue_nums: Sequence[int] | None = None,
        after_id: int | None = None,
        after_seq: int | None = None,
        run_id: str | None = None,
        loop_id: str | None = None,
    ) -> list[AuditEvent]:
        """Newest-first audit events; limit=None returns everything.

        ``event_prefix``/``event_prefixes`` filter by index-served ranges
        rather than LIKE patterns (see :func:`_event_prefix_bounds`); several
        prefixes OR together.

        ``issue_nums`` narrows to those issues — a scope, not a second rule, so
        a caller that wants a handful of items (an epic's nodes on a 1s poll)
        need not parse every matching event on the host (#2129). An empty
        sequence means no issues are in scope and reads nothing. ``item_ids``
        is the same scope keyed by durable item id, for the trackers whose ids
        are not numbers and for a caller that holds ids rather than issue
        numbers (:func:`_audit_item_scope`).

        ``since`` is inclusive and ``until`` exclusive, so paging with
        ``after_id`` inside a window returns every event in it exactly once.
        ``item_or_issue`` and ``epic_ref`` are the operator-facing ``item=``
        and ``epic=`` filters (:func:`_audit_item_filters`,
        :func:`_audit_epic_filters`).

        ``after_seq`` is the FORWARD cursor (C3/#2300): a strict
        lower-exclusive id bound read oldest-first, which is the only ordering
        a forward page can be taken in. ``after_id`` pages the other way, so
        the two are alternatives — a caller that sets both gets the
        intersection of the two bounds, read ascending.
        """
        filters = dict(
            event=event,
            event_prefix=event_prefix,
            event_prefixes=event_prefixes,
            item_id=item_id,
            item_ids=item_ids,
            item_or_issue=item_or_issue,
            epic_ref=epic_ref,
            epic_node_ids=epic_node_ids,
            since=since,
            until=until,
            actor_type=actor_type,
            issue_num=issue_num,
            issue_nums=issue_nums,
            run_id=run_id,
            loop_id=loop_id,
        )
        if _scope_names_nothing(issue_nums, item_ids):
            return []
        clauses, params = _audit_where(filters)
        if after_id is not None:
            clauses.append("id < ?")
            params.append(after_id)
        if after_seq is not None:
            clauses.append("id > ?")
            params.append(after_seq)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if after_seq is not None else "DESC"
        sql = f"SELECT * FROM audit_events{where} ORDER BY id {order}"  # nosec B608 — clauses are literals, values are bound params
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        # WHERE clauses are hardcoded literals; every value (including
        # LIMIT) is a bound parameter — no untrusted input reaches the SQL.
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [_row_to_audit_event(row) for row in rows]

    def audit_event_histogram(
        self,
        *,
        since: datetime,
        until: datetime,
        bucket_s: int,
        event: str | None = None,
        event_prefix: str | None = None,
        event_prefixes: Sequence[str] | None = None,
        item_id: str | None = None,
        item_or_issue: str | None = None,
        epic_ref: str | None = None,
        epic_node_ids: Sequence[str] | None = None,
        actor_type: str | None = None,
        issue_num: int | None = None,
        issue_nums: Sequence[int] | None = None,
        run_id: str | None = None,
    ) -> list[tuple[int, str, int]]:
        """``(bucket_index, event, count)`` over the same filters as
        :meth:`list_audit_events`, aggregated in SQLite rather than on the host.

        The window is ``[since, until)`` and bucket *i* covers
        ``[since + i*bucket_s, since + (i+1)*bucket_s)``, so the counts of a
        full read of the window sum to the counts here.

        ``julianday`` returns a float, and ``(j2 - j1) * 86400`` lands up to a
        few hundred nanoseconds either side of a whole second — enough that a
        bare CAST puts an event sitting exactly ON a bucket boundary in the
        bucket below (and leaves that bucket empty). Rounding the offset to
        milliseconds first makes the boundary exact, at the cost of attributing
        a sub-millisecond straddle to the later bucket.
        """
        if issue_nums is not None and not list(issue_nums):
            return []
        bucket_expr = (
            "CAST(ROUND((julianday(timestamp) - julianday(?)) * 86400, 3) / ?"
            " AS INTEGER)"
        )
        clauses, where_params = _audit_where(
            dict(
                event=event,
                event_prefix=event_prefix,
                event_prefixes=event_prefixes,
                item_id=item_id,
                item_or_issue=item_or_issue,
                epic_ref=epic_ref,
                epic_node_ids=epic_node_ids,
                since=since,
                until=until,
                actor_type=actor_type,
                issue_num=issue_num,
                issue_nums=issue_nums,
                run_id=run_id,
            )
        )
        sql = (
            # Clauses are literals; since/until/bucket and every filter value
            # are bound parameters.
            f"SELECT {bucket_expr} AS bucket, event, COUNT(*) AS n"  # nosec B608
            f" FROM audit_events WHERE {' AND '.join(clauses)}"
            " GROUP BY bucket, event ORDER BY bucket"
        )
        params: list[Any] = [_dt_to_iso(since), bucket_s, *where_params]
        # The grouping expression and every clause are hardcoded literals;
        # since/until/bucket and all filters are bound parameters.
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [(int(r["bucket"]), str(r["event"]), int(r["n"])) for r in rows]

    def list_meta(
        self, prefix: str, *, after: str = "", limit: int = 100
    ) -> list[tuple[str, str]]:
        """Seek a bounded page of existing meta rows without a schema change."""
        rows = self._conn.execute(
            "SELECT key, value FROM meta WHERE key >= ? AND key < ? AND key > ? "
            "ORDER BY key LIMIT ?",
            (prefix, prefix + "\uffff", after, limit),
        ).fetchall()
        return [(row["key"], row["value"]) for row in rows]

    def get_meta(self, key: str) -> str | None:
        """Read a single meta value, or None when the key is absent."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    def schema_version(self) -> int:
        """The schema version the ledger records now, 0 when it records none.

        Read on this open store, past the open guard: a ledger a newer kew
        migrated after this store opened is read rather than refused (#3197).
        """
        row = self._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        return int(row["version"]) if row is not None else 0

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a single meta value (generic KV; e.g. the delegation watch
        cursor). Mirrors :meth:`set_fleet_stopped`'s ON CONFLICT upsert."""

        def _do_set() -> None:
            self._execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

        self._with_busy_retry("set_meta", _do_set)

    def compare_set_meta(self, key: str, expected: str | None, value: str) -> bool:
        """Claim a retry across processes without overwriting another claim."""
        with self._txn_lock:
            cursor = self._execute(
                "INSERT INTO meta (key, value) SELECT ?, ? WHERE ? IS NULL"
                " ON CONFLICT(key) DO NOTHING",
                (key, value, expected),
            )
            if expected is not None:
                cursor = self._execute(
                    "UPDATE meta SET value = ? WHERE key = ? AND value = ?",
                    (value, key, expected),
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def append_audit_event_with_metas(
        self,
        writes: Sequence[tuple[str, str | None, str]],
        *,
        timestamp: str,
        event: str,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        policy_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Write every ``(key, expected, value)`` and chain one audit row, in
        one ``BEGIN IMMEDIATE`` transaction, or none of it (#2975): each key
        must still read *expected* (``None`` = absent), so no other connection
        sees half the writes, and the writes never land without their fact.
        False, with nothing written, when a key moved; a fact the chain cannot
        confirm raises, with nothing written.
        """
        values = audit_values(
            timestamp=timestamp,
            event=event,
            item_id=item_id,
            repo=repo,
            actor_type=actor_type,
            actor_id=actor_id,
            policy_hash=policy_hash,
            payload=payload,
        )
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if not self._metas_read(writes):
                    self._conn.rollback()
                    return False
                for key, _, value in writes:
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
                event_id = self._chained_insert_in_transaction(values)
                if type(event_id) is not int or event_id < 1:
                    raise RuntimeError(
                        f"Audit event {event} persistence was not confirmed"
                    )
                self._conn.commit()
                return True
            except BaseException:
                self._conn.rollback()
                raise

    def _metas_read(self, writes: Sequence[tuple[str, str | None, str]]) -> bool:
        """Whether every key in *writes* still reads its expected value."""
        for key, expected, _ in writes:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            if (row["value"] if row is not None else None) != expected:
                return False
        return True

    def claim_meta_unless_item_ran(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        item_id: str,
        since: datetime,
        superseded: Callable[[str], str],
    ) -> tuple[bool, str | None]:
        """Write *value* over *key* if it still reads *expected*, unless
        *item_id* has a pending run, or a dispatch run started after *since*
        (#2975): then ``superseded(run_id)`` is written instead, naming it.
        One ``BEGIN IMMEDIATE`` transaction, so no run is recorded between the
        look and the write. ``(wrote, run_id)``; False when *key* moved.

        The liveness reconciliation admission runs comes first (#3010 F2): a
        pending row whose owner is proven dead is finalized, so it fences
        nothing, while a live or unprovable one still does.
        """
        self.sweep_dead_pending()
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if not self._metas_read([(key, expected, value)]):
                    self._conn.rollback()
                    return False, None
                run_id = self._item_run_since(item_id, since)
                self._conn.execute(
                    "UPDATE meta SET value = ? WHERE key = ?",
                    (value if run_id is None else superseded(run_id), key),
                )
                self._conn.commit()
                return True, run_id
            except BaseException:
                self._conn.rollback()
                raise

    def _item_run_since(self, item_id: str, since: datetime) -> str | None:
        """A pending run of *item_id*, or a dispatch run of it started after
        *since*; the newest first. None when there is neither."""
        rows = self._conn.execute(
            "SELECT run_id, status, run_kind, started_at FROM runs"
            " WHERE COALESCE(item_id, CAST(issue_num AS TEXT)) = ?"
            " AND (status = 'pending' OR run_kind = 'dispatch')"
            " ORDER BY started_at DESC",
            (item_id,),
        ).fetchall()
        for row in rows:
            started = _iso_to_dt(row["started_at"])
            if row["status"] == "pending" or (started is not None and started > since):
                return str(row["run_id"])
        return None

    def count_container_runtimes(self, since: datetime) -> dict[str, int]:
        """Runs per ``container_runtime`` since *since*, counted SQL-side.

        What the fleet has ACTUALLY run inside, as against what the config says
        it should: the health page draws the two side by side, because a lane
        configured for docker whose last week of runs recorded nothing is the
        shape of a posture claim nobody has tested.

        A run with no recorded runtime is counted under the empty string rather
        than dropped — a host run and a run whose posture event could not be
        read both land there, and silently omitting them would let a window of
        entirely uncontained runs read as a window with no runs at all. Rows are
        read, never repaired.
        """
        rows = self._conn.execute(
            "SELECT COALESCE(container_runtime, '') AS runtime, COUNT(*) AS n"
            " FROM runs WHERE started_at >= ?"
            " GROUP BY COALESCE(container_runtime, '')",
            (_dt_to_iso(since),),
        ).fetchall()
        return {str(row["runtime"]): int(row["n"]) for row in rows}

    def count_successful_runs_by_runner(self, since: datetime) -> dict[str, int]:
        """Successful runs per ``runner_name`` since *since*, counted SQL-side.

        The proof the health page needs before it calls a credential missing
        from ``kew serve``'s own environment a note rather than an outage
        (#2566): a runner that finished a run successfully this window was
        launched by a process that held its credential. A run with no recorded
        runner proves nothing about any runner, so it is not counted.
        """
        rows = self._conn.execute(
            "SELECT runner_name AS runner, COUNT(*) AS n FROM runs"
            " WHERE status = 'success' AND started_at >= ? AND runner_name != ''"
            " GROUP BY runner_name",
            (_dt_to_iso(since),),
        ).fetchall()
        return {str(row["runner"]): int(row["n"]) for row in rows}

    def audit_parse_report(self, since: datetime | None = None) -> tuple[int, int]:
        """(total, malformed) audit_events since *since* (all if None).

        A row is malformed when a required envelope field (timestamp/event/
        actor_type) is null/empty, OR its payload TEXT is present but does not
        parse as a JSON object — the integrity gap list_audit_events hides by
        coercing bad payloads to None.
        """
        where = ""
        params: list[Any] = []
        if since is not None:
            where = " WHERE timestamp >= ?"
            params = [_dt_to_iso(since)]
        # WHERE clause is a hardcoded literal; every value is a bound parameter.
        rows = self._conn.execute(  # nosemgrep
            f"SELECT timestamp, event, actor_type, payload FROM audit_events{where}",  # nosec B608
            params,
        ).fetchall()
        malformed = 0
        for r in rows:
            if not r["timestamp"] or not r["event"] or not r["actor_type"]:
                malformed += 1
                continue
            raw = r["payload"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                malformed += 1
                continue
            if not isinstance(parsed, dict):
                malformed += 1
        return len(rows), malformed

    def get(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_by_issue(self, issue_num: int) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE issue_num = ? ORDER BY started_at",
            (issue_num,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_runs_by_branch(
        self, branch: str, *, limit: int = 33, offset: int = 0
    ) -> list[RunRecord]:
        """Return one bounded page of exact-branch runs, newest first."""
        if limit < 1 or limit > 33:
            raise ValueError("branch run lookup limit must be between 1 and 33")
        if offset < 0:
            raise ValueError("branch run lookup offset must be nonnegative")
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE branch = ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (branch, limit, offset),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_runs_by_branch_after(
        self, branch: str, *, limit: int = 33, after: tuple[str, int] | None = None
    ) -> tuple[list[RunRecord], tuple[str, int] | None]:
        """Return a page and its last (stored started_at, rowid) key.

        Order both keys descending. Read strictly below ``after`` with one SELECT.
        Keep the timestamp's stored text so cursor comparisons match SQLite sorting.
        Inserts and deletes cannot shift retained rows across this key boundary.
        An empty page has no cursor. Hold no transaction between page reads.
        """
        if limit < 1 or limit > 33:
            raise ValueError("branch run lookup limit must be between 1 and 33")
        started_at, rowid = after if after is not None else (None, None)
        rows = self._conn.execute(
            "SELECT rowid AS branch_rowid, *"
            " FROM runs WHERE branch = ?"
            " AND (? IS NULL OR (started_at, rowid) < (?, ?))"
            " ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (branch, started_at, started_at, rowid, limit),
        ).fetchall()
        cursor = (rows[-1]["started_at"], rows[-1]["branch_rowid"]) if rows else None
        return [_row_to_record(row) for row in rows], cursor

    def find_run_by_branch(self, branch: str) -> RunRecord | None:
        """The latest PR-opening run for an EXACT branch match, or None (A3).

        Written fresh for the Linear PR→item resolution path (A6): with no
        ``Closes #N`` marker to parse, the branch is the join key back to the
        run that opened the PR. Index-backed by ``idx_runs_branch``. Runs whose
        subject was that PR (``PR_SUBJECT_RUN_KINDS``: reviews and their
        remediations name the branch they worked on) are looked past, since
        their ``issue_num`` is the PR, not the item.
        """
        placeholders = ", ".join("?" for _ in PR_SUBJECT_RUN_KINDS)
        row = self._conn.execute(
            "SELECT * FROM runs WHERE branch = ?"
            f" AND COALESCE(run_kind, 'dispatch') NOT IN ({placeholders})"  # nosec B608 — placeholders are ? params, values are bound
            " ORDER BY started_at DESC LIMIT 1",
            (branch, *sorted(PR_SUBJECT_RUN_KINDS)),
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def sum_costs_by_issue(
        self, issue_nums: list[int], *, since: str, until: str | None = None
    ) -> dict[int, float]:
        """Aggregate estimated_cost_usd per issue_num, single indexed query.

        Replaces an N-per-node store.list_by_issue scan (one query per DAG
        node, per 1s poll) in kew.service.epics._drain_costs — see #454
        Phase B PR #458 review. Readiness probes are excluded: they are real
        spend (see :func:`kew.report.compute_stats`) but they are not this
        item's economics, so folding them in would charge a node for a
        fleet-wide capability check. finished_at is stored as ISO-8601 UTC text
        (_dt_to_iso); string comparison against *since*/*until* is
        intentional and sufficient (same rationale as _run_in_window in
        kew/service/epics.py). Issues with no matching runs are simply
        absent from the returned dict.
        """
        if not issue_nums:
            return {}
        placeholders = ",".join("?" * len(issue_nums))
        params: list[Any] = [*issue_nums, READINESS_PROBE_RUN_KIND, since]
        sql = (
            "SELECT issue_num, SUM(estimated_cost_usd) AS total FROM runs "  # nosec B608 — placeholders are ? params, values are bound
            f"WHERE issue_num IN ({placeholders}) "
            "AND run_kind != ? "
            "AND finished_at IS NOT NULL AND finished_at >= ?"
        )
        if until is not None:
            sql += " AND finished_at <= ?"
            params.append(until)
        sql += " GROUP BY issue_num"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return {int(row["issue_num"]): float(row["total"] or 0.0) for row in rows}

    def sum_costs_by_item(
        self, item_ids: list[str], *, since: str, until: str | None = None
    ) -> dict[str, float]:
        """``sum_costs_by_issue`` twin keyed on the durable ``item_id`` (A3).

        Same windowing semantics (inclusive both ends; pending rows excluded,
        readiness probes excluded); items with no matching runs are absent from
        the returned dict.
        """
        if not item_ids:
            return {}
        placeholders = ",".join("?" * len(item_ids))
        params: list[Any] = [*item_ids, READINESS_PROBE_RUN_KIND, since]
        sql = (
            "SELECT item_id, SUM(estimated_cost_usd) AS total FROM runs "  # nosec B608 — placeholders are ? params, values are bound
            f"WHERE item_id IN ({placeholders}) "
            "AND run_kind != ? "
            "AND finished_at IS NOT NULL AND finished_at >= ?"
        )
        if until is not None:
            sql += " AND finished_at <= ?"
            params.append(until)
        sql += " GROUP BY item_id"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return {str(row["item_id"]): float(row["total"] or 0.0) for row in rows}

    def list_by_items(self, item_ids: list[str]) -> dict[str, list[RunRecord]]:
        """``list_by_item`` for a whole set of items, in ONE query (#2270).

        The epic timeline needs every run of every node of an epic; per-node
        reads turn one page into one query per node. Returns oldest-first
        lists keyed by ``item_id``; an item with no runs is absent from the
        dict rather than present with an empty list, matching
        :meth:`sum_costs_by_item`.
        """
        if not item_ids:
            return {}
        unique = list(dict.fromkeys(item_ids))
        placeholders = ",".join("?" * len(unique))
        sql = (
            "SELECT * FROM runs "  # nosec B608 — placeholders are ? params, values are bound
            f"WHERE item_id IN ({placeholders}) ORDER BY started_at"
        )
        rows = self._conn.execute(sql, unique).fetchall()  # nosemgrep
        grouped: dict[str, list[RunRecord]] = {}
        for row in rows:
            grouped.setdefault(str(row["item_id"]), []).append(_row_to_record(row))
        return grouped

    def list_all(self) -> list[RunRecord]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        return [_row_to_record(row) for row in rows]

    def list_run_page(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[RunRecord]:
        """One bounded page of runs, newest first, filtered in SQL (C3).

        The console polls this every few seconds. ``list_all`` read every row
        of the ledger to keep the newest ``limit``, so the cost of one poll
        grew with history forever. Here the window, the status filter and the
        kind filter are all SQL predicates and the page is ONE query.

        ``before`` is the stored ``started_at`` text of the last row of the
        previous page, so cursor comparisons match SQLite's own ordering;
        rows are read strictly below it. ``ORDER BY started_at DESC`` walks
        ``idx_runs_started`` (or ``idx_runs_status_started`` when a status is
        named) backwards and stops at ``limit``.
        """
        if limit < 1 or limit > MAX_RUN_PAGE:
            raise ValueError(f"run page limit must be between 1 and {MAX_RUN_PAGE}")
        rows = self._conn.execute(
            "SELECT * FROM runs"
            " WHERE (? IS NULL OR started_at < ?)"
            " AND (? IS NULL OR status = ?)"
            " AND (? IS NULL OR COALESCE(run_kind, 'dispatch') = ?)"
            " ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (before, before, status, status, kind, kind, limit),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_recent_completions(self, *, limit: int = 5) -> list[RunRecord]:
        """The newest finished runs, newest first — one bounded query (C3).

        The dashboard strip keeps five. Reading them used to mean materializing
        every run in the ledger and sorting in Python; ``idx_runs_finished``
        makes this a backwards walk that stops at ``limit``.
        """
        if limit < 1 or limit > MAX_RUN_PAGE:
            raise ValueError(f"completion limit must be between 1 and {MAX_RUN_PAGE}")
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL"
            " ORDER BY finished_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_since(self, since: datetime) -> list[RunRecord]:
        """Return all records with started_at >= since.

        Binds the *naive* (19-char) prefix of the UTC-normalized cutoff as a
        single lower bound so the range is a seek on ``idx_runs_started`` (I8)
        rather than a full scan. ``_dt_to_iso`` always writes UTC, so a
        tz-suffixed row (``...+00:00``, 25 chars) sorts strictly after its own
        naive prefix and a legacy naive row (19 chars) compares directly;
        ``started_at >= since_naive`` is therefore a superset of the exact
        window that never drops a matching row. The post-filter trims the few
        lexical over-matches to the precise datetime cutoff — the earlier
        ``OR (length(started_at) = 19 ...)`` form defeated the index seek.
        """
        since_iso = _dt_to_iso(since)
        assert since_iso is not None
        # legacy rows may lack a tz suffix; the naive prefix is the broader
        # (index-seekable) lower bound covering both row shapes.
        since_naive = since_iso[:19]
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at",
            (since_naive,),
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        return [r for r in records if r.started_at >= since]

    def list_pending(self) -> list[RunRecord]:
        cur = self._conn.execute(
            "SELECT * FROM runs WHERE status = 'pending' ORDER BY started_at DESC"
        )
        return [_row_to_record(row) for row in cur.fetchall()]

    def set_run_site(self, run_id: str, host: str, instance: str | None = None) -> bool:
        """Record WHERE this run is placed: its host, and the instance on it.

        A separate write rather than a field the upsert carries, for the reason
        :meth:`set_container_identity` is one: the placement is settled by the
        launch, and every later ``save()`` of the run rebuilds a record that
        knows nothing about it. ``_UPSERT_SQL`` therefore PRESERVES both columns
        on conflict, and this is the only way they change after admission.

        *instance* is stored as NULL when empty: ``local`` is one machine and
        has no instance to name, and an empty string would read as a machine
        named nothing.

        Returns whether a row was actually updated. An ``UPDATE`` whose
        ``WHERE`` matches nothing is not an error to SQLite, and the caller
        cannot see the difference from the ledger afterwards: an unwritten
        placement reads as ``local``, which reads as *that instance is free*.
        So the rowcount is reported rather than dropped, and
        :func:`kew.fleet_placement.record_placement` refuses on it.
        """

        def _do_set() -> bool:
            cursor = self._execute(
                "UPDATE runs SET run_host = ?, run_host_instance = ? WHERE run_id = ?",
                (host or LOCAL_RUN_HOST, instance or None, run_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("set_run_site", _do_set)

    def delete(self, run_id: str) -> bool:
        def _do_delete() -> bool:
            cursor = self._execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("delete", _do_delete)

    def _stale_pending_candidates(self, cutoff: datetime) -> list[RunRecord]:
        """Pending rows the stale sweep takes: older than ``cutoff``, not in a
        container, and with a proven-dead host owner. Reads only."""
        cutoff_iso = _dt_to_iso(cutoff)
        assert cutoff_iso is not None
        # legacy rows may lack timezone suffix
        cutoff_naive = cutoff_iso[:19]
        rows = self._conn.execute(
            """
            SELECT * FROM runs
            WHERE status = 'pending'
              AND (started_at < ?
                   OR (length(started_at) = 19 AND started_at < ?))
            """,
            (cutoff_iso, cutoff_naive),
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        return [
            r
            for r in records
            if r.started_at < cutoff
            and r.container_name is None
            and _stale_host_is_dead(r)
        ]

    def list_stale_pending(self, max_age_hours: float = 24.0) -> list[RunRecord]:
        """The rows ``cleanup_stale_pending`` would mark failed now, unchanged.

        ``kew cleanup --dry-run`` previews the sweep with this (F18).
        """
        return self._stale_pending_candidates(
            datetime.now(UTC) - timedelta(hours=max_age_hours)
        )

    def cleanup_stale_pending(self, max_age_hours: float = 24.0) -> int:
        """Mark stale pending records as failed.

        Age selects candidates. Only proven host deaths release ownership.
        Unknown owners and unresolved attempts remain pending, including while
        the fleet is latched. Existing stop intent survives finalization.

        Returns the number of records cleaned up.

        The SELECT and the UPDATE run inside ONE ``BEGIN IMMEDIATE`` critical
        section (#1059): the age-gated sweep is the backstop that reclaims a
        crashed run's orphaned worker slot, and it was observed no-op'ing under
        review-recovery's concurrent writes — a bare autocommit SELECT+UPDATE
        can race a peer that finalizes a row between the two statements, so the
        blind ``run_id`` UPDATE would clobber a fresh terminal outcome (and the
        rowcount would misreport). Serializing the read-modify-write means a
        momentarily-busy DB is *retried* by ``_with_busy_retry`` (never a silent
        0), and the ``AND status = 'pending'`` guard makes the UPDATE idempotent
        against a peer that reclaimed the same row first — it flips only rows
        still pending at commit, so two concurrent sweeps never double-count.
        """
        self.retry_stop_label_resets()
        cleaned_ids: list[str] = []
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=max_age_hours)
        finished_at_iso = _dt_to_iso(now)

        def _do_cleanup() -> int:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    stale = self._stale_pending_candidates(cutoff)
                    if not stale:
                        self._conn.rollback()
                        return 0
                    placeholders = ",".join("?" * len(stale))
                    sql = (
                        "UPDATE runs SET status = 'failure',"  # nosec B608 — placeholders are ? params, values are bound
                        " error_category = CASE WHEN error_category = 'killed'"
                        " THEN error_category ELSE 'stale_pending' END,"
                        " finished_at = ? WHERE status = 'pending'"
                        f" AND run_id IN ({placeholders})"
                    )
                    cursor = self._execute(
                        sql,
                        (finished_at_iso, *[r.run_id for r in stale]),
                    )
                    self._conn.commit()
                    cleaned_ids.extend(r.run_id for r in stale)
                    return cursor.rowcount
                except BaseException:
                    self._conn.rollback()
                    raise

        cleaned = self._with_busy_retry("cleanup_stale_pending", _do_cleanup)
        for run_id in cleaned_ids:
            self.retry_stop_label_resets(run_id)
        return cleaned

    def analytics(self) -> dict[str, Any]:
        """Compute aggregate dispatch metrics from completed runs.

        Returns a dict with keys:
        - overall: {total, success, success_rate}
        - by_complexity: {label: {total, success, avg_duration_seconds, avg_cost_usd}}
        - by_runner: {name: {total, success, avg_cost_usd}}
        - by_model: {name: {total, success, avg_cost_usd}}
        - failure_categories: {category: count}

        Pending runs are excluded from all calculations.
        """
        rows = self._conn.execute(
            "SELECT status, exit_code, runner_name, model, complexity, "
            "       duration_seconds, estimated_cost_usd, error_category, "
            "       started_at, finished_at "
            "FROM runs WHERE status != 'pending'"
        ).fetchall()

        total = len(rows)
        success_count = sum(1 for r in rows if r["status"] == "success")

        by_complexity = _aggregate_by(
            rows, key_column="complexity", include_duration=True
        )
        by_runner = _aggregate_by(rows, key_column="runner_name")
        by_model = _aggregate_by(rows, key_column="model")
        failure_categories = _count_failure_categories(rows)

        # Flag insufficient data when fewer than 5 completed runs exist.
        # Downstream consumers (e.g. the TUI Analytics panel) should show a
        # notice rather than presenting potentially misleading statistics.
        _MIN_RUNS_FOR_ANALYTICS = 5
        insufficient_data = total < _MIN_RUNS_FOR_ANALYTICS

        return {
            "overall": {
                "total": total,
                "success": success_count,
                "success_rate": success_count / total if total else 0.0,
            },
            "insufficient_data": insufficient_data,
            "by_complexity": by_complexity,
            "by_runner": by_runner,
            "by_model": by_model,
            "failure_categories": failure_categories,
        }

    def purge_older_than(self, days: int) -> int:
        """Delete records older than the given number of days.

        Returns the number of records deleted.
        """
        if days <= 0:
            raise ValueError("days must be > 0")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        cutoff_iso = _dt_to_iso(cutoff)
        assert cutoff_iso is not None
        cutoff_naive = cutoff_iso[:19]

        def _do_purge() -> int:
            cursor = self._execute(
                """
                DELETE FROM runs
                WHERE started_at < ?
                   OR (length(started_at) = 19 AND started_at < ?)
                """,
                (cutoff_iso, cutoff_naive),
            )
            self._conn.commit()
            return cursor.rowcount

        return self._with_busy_retry("purge_older_than", _do_purge)

    # ── config overrides (web config-manager #368) ────────────────────

    def list_config_overrides(self) -> list[dict[str, Any]]:
        """All active config overrides, one dict per row."""
        rows = self._conn.execute(
            "SELECT field, value_json, file_value_json, set_at, set_by,"
            " reason, audit_event_id FROM config_overrides ORDER BY field"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_config_override(self, field: str) -> dict[str, Any] | None:
        """Single override by dotted field path, or None."""
        row = self._conn.execute(
            "SELECT field, value_json, file_value_json, set_at, set_by,"
            " reason, audit_event_id FROM config_overrides WHERE field = ?",
            (field,),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_config_override(
        self,
        *,
        field: str,
        value_json: str,
        file_value_json: str | None,
        set_at: str,
        set_by: str,
        reason: str = "",
        audit_event_id: int | None = None,
    ) -> None:
        """Upsert a config override row."""
        params = (
            field,
            value_json,
            file_value_json,
            set_at,
            set_by,
            reason,
            audit_event_id,
        )
        self._conn.execute(
            "INSERT INTO config_overrides"
            " (field, value_json, file_value_json,"
            "  set_at, set_by, reason, audit_event_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(field) DO UPDATE SET"
            "  value_json = excluded.value_json,"
            "  file_value_json = excluded.file_value_json,"
            "  set_at = excluded.set_at,"
            "  set_by = excluded.set_by,"
            "  reason = excluded.reason,"
            "  audit_event_id = excluded.audit_event_id",
            params,
        )
        self._conn.commit()

    def revert_config_override(self, field: str) -> bool:
        """Delete an override row. Returns True if a row was actually removed."""
        cursor = self._conn.execute(
            "DELETE FROM config_overrides WHERE field = ?", (field,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def _bind_continuation_checkout(
        self, record: ContinuationRecord, generation: str, *, inserted: bool
    ) -> None:
        """Commit the checkout binding with its first continuation registration."""
        if not inserted:
            if generation != self.continuation_checkout_generation(record):
                raise ValueError("continuation checkout generation cannot change")
            return
        payload = {
            "epic_ref": record.epic_ref,
            "item_id": record.item_id,
            "original_run_id": record.original_run_id,
            "branch": record.branch,
            "worktree_identity": record.worktree_identity,
            "checkout_id": generation,
        }
        event_id = self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                "continuation_checkout_bound",
                record.original_run_id,
                _epic_db_issue_num(record.item_id),
                None,
                "policy",
                "continuation-registration",
                None,
                None,
                json.dumps(payload),
                record.item_id,
            )
        )
        if event_id is None:
            raise RuntimeError("continuation checkout binding was not durably recorded")

    def continuation_checkout_generation(self, record: ContinuationRecord) -> str:
        """Read immutable registration evidence without inspecting the checkout."""
        generations = {
            event.payload.get("checkout_id", "")
            for event in self.list_audit_events(
                limit=None, event="continuation_checkout_bound", item_id=record.item_id
            )
            if event.payload is not None
            and _continuation_checkout_binding_matches(event, record)
        }
        if not generations:
            return ""
        if len(generations) != 1:
            raise ValueError("continuation has conflicting checkout generations")
        generation = generations.pop()
        if not isinstance(generation, str):
            raise ValueError("continuation has an unreadable checkout generation")
        return generation

    def record_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        *,
        checkout_generation: str,
        precommit_check: Callable[[], None] | None = None,
    ) -> ContinuationRecord:
        """Insert an awaiting continuation record, preserving existing state.

        Detection can run on every driver tick, so this is deliberately an
        idempotent insert.  Once a continuation is owned or terminal, a later
        observation must not erase that state or its generation. A successful
        original run is inserted terminal: publication finalization may have
        raced ahead of this late insert, and a finished run must never gain a
        newly active recovery handle.
        """
        now = _dt_to_iso(datetime.now(UTC))
        assert now is not None

        def _do_record() -> ContinuationRecord:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    worktree_identity = self._continuation_worktree_key(
                        epic_ref, item_id, original_run_id, branch, worktree
                    )
                    inserted = self._execute(
                        "INSERT INTO epic_continuations"
                        " (epic_ref, item_id, original_run_id, branch,"
                        "  worktree_identity, state, generation,"
                        "  continuation_run_id, created_at, updated_at,"
                        "  claimed_at, terminal_at)"
                        " VALUES (?, ?, ?, ?, ?,"
                        " CASE WHEN EXISTS(SELECT 1 FROM runs"
                        " WHERE run_id = ? AND status = 'success')"
                        " THEN 'terminal' ELSE 'awaiting_continuation' END,"
                        " 0, NULL, ?, ?, NULL,"
                        " CASE WHEN EXISTS(SELECT 1 FROM runs"
                        " WHERE run_id = ? AND status = 'success')"
                        " THEN ? ELSE NULL END)"
                        " ON CONFLICT(epic_ref, item_id, original_run_id, branch,"
                        " worktree_identity) DO NOTHING",
                        (
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                            original_run_id,
                            now,
                            now,
                            original_run_id,
                            now,
                        ),
                    )
                    row = self._execute(
                        "SELECT epic_ref, item_id, original_run_id, branch,"
                        " worktree_identity, state, generation,"
                        " continuation_run_id, created_at, updated_at,"
                        " claimed_at, terminal_at FROM epic_continuations"
                        " WHERE epic_ref = ? AND item_id = ?"
                        " AND original_run_id = ? AND branch = ?"
                        " AND worktree_identity = ?",
                        (
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                        ),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("continuation insert was not persisted")
                    record = _row_to_continuation(row)
                    self._bind_continuation_checkout(
                        record, checkout_generation, inserted=inserted.rowcount == 1
                    )
                    _invoke_precommit_check(precommit_check)
                    self._conn.commit()
                    return record
                except BaseException as exc:
                    _rollback_after_failure(self._conn, exc)
                    raise

        try:
            return self._with_busy_retry("record_continuation", _do_record)
        except _PrecommitCheckError as exc:
            _raise_precommit_error(exc)

    def get_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
    ) -> ContinuationRecord | None:
        """Read one continuation by its complete recovery identity."""
        rows = self._execute(
            "SELECT * FROM epic_continuations"
            " WHERE epic_ref = ? AND item_id = ? AND original_run_id = ?"
            " AND branch = ?",
            (epic_ref, item_id, original_run_id, branch),
        ).fetchall()
        matches = [
            row
            for row in rows
            if continuation_checkout_matches(row["worktree_identity"], worktree)
        ]
        if len(matches) > 1:
            raise RuntimeError("ambiguous continuation identity for checkout")
        return _row_to_continuation(matches[0]) if matches else None

    def _continuation_worktree_key(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
    ) -> str:
        """Keep the immutable database key after verifying a caller's alias."""
        record = self.get_continuation(
            epic_ref, item_id, original_run_id, branch, worktree
        )
        if record is not None:
            return record.worktree_identity
        return _canonical_worktree_identity(worktree)

    def list_continuations(self, epic_ref: str) -> list[ContinuationRecord]:
        """List recovery records for an epic in stable key order."""
        rows = self._execute(
            "SELECT epic_ref, item_id, original_run_id, branch,"
            " worktree_identity, state, generation, continuation_run_id,"
            " created_at, updated_at, claimed_at, terminal_at"
            " FROM epic_continuations WHERE epic_ref = ?"
            " ORDER BY item_id, original_run_id, branch, worktree_identity",
            (epic_ref,),
        ).fetchall()
        return [_row_to_continuation(row) for row in rows]

    def list_all_continuations(self) -> list[ContinuationRecord]:
        """Every recovery record across epics, in stable key order."""
        rows = self._execute(
            "SELECT * FROM epic_continuations"
            " ORDER BY epic_ref, item_id, original_run_id, branch, worktree_identity",
        ).fetchall()
        return [_row_to_continuation(row) for row in rows]

    def continuations_for_checkout(
        self, checkout: str | Path, *, item_id: str, branch: str
    ) -> list[ContinuationRecord]:
        """Find filesystem owners and logical abandonment families across epics."""
        rows = self._execute(
            "SELECT * FROM epic_continuations"
            " ORDER BY epic_ref, item_id, original_run_id, branch",
        ).fetchall()
        return [
            record
            for record in map(_row_to_continuation, rows)
            if continuation_checkout_matches(
                record.worktree_identity,
                checkout,
                recorded_owner=(record.item_id, record.branch),
                candidate_owner=(item_id, branch),
            )
            or (
                (record.item_id, record.branch) == (item_id, branch)
                and self.continuation_was_abandoned(record)
            )
        ]

    def continuation_is_complete(self, record: ContinuationRecord) -> bool:
        """Require successful owner evidence before excluding a terminal row."""
        if record.state != "terminal" or record.terminal_at is None:
            return False
        owner = record.continuation_run_id or record.original_run_id
        return self._continuation_run_succeeded(
            owner
        ) or self._continuation_run_succeeded(self._released_continuation_owner(record))

    def _continuation_run_succeeded(self, run_id: str | None) -> bool:
        """Read retained success evidence without inferring it from terminal state."""
        run = self.get(run_id) if run_id is not None else None
        return run is not None and run.status == "success"

    def _released_continuation_owner(self, record: ContinuationRecord) -> str | None:
        """Find the latest owner whose publication may finish after requeue."""
        if record.continuation_run_id is not None or record.generation == 0:
            return None
        try:
            claims, releases = self._continuation_owner_events(record)
        except (RuntimeError, ValueError, OverflowError) as exc:
            _logger.warning(
                "cannot prove continuation %s/%s complete: %s",
                record.epic_ref,
                record.item_id,
                exc,
            )
            return None
        latest = [claim for claim in claims if claim["generation"] == record.generation]
        if len(latest) != 1:
            return None
        owner = str(latest[0]["continuation_run_id"])
        released = any(
            event["generation"] == record.generation
            and event["continuation_run_id"] == owner
            for event in releases
        )
        return owner if released else None

    def continuation_is_recoverable(self, record: ContinuationRecord) -> bool:
        """Allow recovery until success or an explicit abandonment is proven."""
        return record.state == "awaiting_continuation" or (
            record.state == "terminal"
            and not self.continuation_is_complete(record)
            and not self.continuation_was_abandoned(record)
        )

    def continuation_run_id_in_use(self, run_id: str) -> bool:
        """Whether *run_id* already names anything a run id can name.

        A continuation run id must be new everywhere before it is claimed: a
        run row (an existing run would gain a second ancestry), an epic
        reservation (a run about to exist), a mutable continuation owner (two
        owners of one id), and claim evidence (a second claim event makes the
        ancestry ambiguous). Tables that hold a run id only after its run row
        exists (publication jobs, receipts, artifacts) are covered by the run
        row.
        """
        for sql in (
            "SELECT 1 FROM runs WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM epic_reservations WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM epic_continuations WHERE continuation_run_id = ? LIMIT 1",
            "SELECT 1 FROM audit_events"
            " WHERE event = 'continuation_claimed' AND run_id = ? LIMIT 1",
        ):
            if self._execute(sql, (run_id,)).fetchone() is not None:
                return True
        return False

    def continuation_origins(self, run_id: str, *, limit: int = 2) -> list[str]:
        """Return the retained direct parent for one continuation generation.

        Claim payloads are inside the audit hash-chain preimage. The mutable
        continuation row only detects missing evidence. It never reconstructs
        a prior generation after requeue clears its owner.

        A released generation without claim evidence is transparent. A claim
        is also transparent when its run never launched. This means no run
        row, or an initial pending row without a finish, process, container,
        session, elapsed duration, token usage, or cost. Retained route profiles,
        dispatch_started, and publication_intent always count as launch evidence.
        A released run with launch evidence remains a publication ancestor.
        """
        if limit < 1 or limit > 32:
            raise ValueError("continuation origin limit must be between 1 and 32")
        target = self._continuation_target_claim(run_id)
        if target is None:
            return []
        claims, releases = self._continuation_history_events(target)
        by_generation = _continuation_claims_by_generation(
            claims, releases, launched=self._continuation_run_launched
        )
        return [
            _continuation_parent(
                target,
                by_generation,
                run_id=run_id,
                launched=self._continuation_run_launched,
            )
        ]

    def _continuation_run_launched(self, run_id: str) -> bool:
        """Retain positive execution evidence even for a still-pending row."""
        evidence = self._execute(
            "SELECT 1 FROM run_route_profile WHERE run_id = ?"
            " UNION ALL SELECT 1 FROM audit_events WHERE run_id = ?"
            " AND event IN ('dispatch_started', 'publication_intent') LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        if evidence is not None:
            return True
        run = self.get(run_id)
        if run is None:
            return False
        return run.status != "pending" or any(
            (
                run.finished_at,
                run.pgid,
                run.attempt_pgid,
                run.container_name,
                run.session_id,
                run.duration_seconds,
                run.total_tokens,
                run.input_tokens,
                run.output_tokens,
                run.cache_creation_tokens,
                run.cache_read_tokens,
                run.estimated_cost_usd,
            )
        )

    def require_continuation_history(self, record: ContinuationRecord) -> None:
        """Refuse a fresh claim with a legacy gap before consuming a generation."""
        from kew.audit_identity import continuation_evidence

        target = continuation_evidence(
            phase="claim",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            continuation_run_id=record.continuation_run_id,
        )
        claims, releases = self._continuation_history_events(target)
        _continuation_claims_by_generation(
            claims,
            releases,
            highest=record.generation,
            launched=self._continuation_run_launched,
        )

    def continuation_owner_run_ids(self, record: ContinuationRecord) -> list[str]:
        """Include claims, validated releases, and retained checkout owners."""
        claims, releases = self._continuation_owner_events(record)
        owners = [
            record.original_run_id,
            *(event["continuation_run_id"] for event in [*claims, *releases]),
            *self._retained_checkout_owner_run_ids(record),
        ]
        if record.continuation_run_id is not None:
            owners.append(record.continuation_run_id)
        return list(dict.fromkeys(owners))

    def require_continuation_owner_history(self, record: ContinuationRecord) -> None:
        """Refuse missing owners even when every known owner is dead."""
        claims, releases = self._continuation_owner_events(record)
        _require_complete_owner_history(claims, releases, record.generation)

    def _continuation_owner_events(
        self, record: ContinuationRecord
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from kew.audit_identity import continuation_evidence

        target = continuation_evidence(
            phase="claim",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            continuation_run_id=None,
        )
        return self._continuation_history_events(target)

    def _continuation_history_events(
        self, target: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Exclude cancelled pre-launch claims without erasing their audit events.

        The claim_undone event and generation rollback share one transaction.
        A later claim can reuse that generation. The cancelled attempt cannot
        supply its claim evidence or become a checkout owner.
        Matching retained claims are excluded only after a pre-launch rollback.
        Run rows, execution evidence, or retained ownership prevent exclusion.
        """
        claims = [
            _continuation_claim_payload(row)
            for row in self._continuation_family_events(target, "continuation_claimed")
        ]
        releases = [
            _continuation_release_payload(row)
            for row in self._continuation_family_events(target, "continuation_released")
        ]
        cancelled = {
            (release["continuation_run_id"], release["generation"])
            for release in releases
            if self._continuation_claim_was_cancelled(release, claims)
        }
        claims, releases = (
            [
                event
                for event in events
                if (event["continuation_run_id"], event["generation"]) not in cancelled
            ]
            for events in (claims, releases)
        )
        consumed = {event["generation"] for event in [*claims, *releases]}
        if max(len(claims), len(consumed)) > _MAX_CONTINUATION_GENERATIONS:
            raise OverflowError("continuation claim history exceeds policy bound")
        return claims, releases

    def _continuation_claim_was_cancelled(
        self, release: dict[str, Any], claims: list[dict[str, Any]]
    ) -> bool:
        """Require validated rollback evidence and no possible launched owner."""
        run_id = release["continuation_run_id"]
        retained = [claim for claim in claims if claim["continuation_run_id"] == run_id]
        if not _cancelled_claim_matches(release, retained):
            return False
        ownership = self._execute(
            "SELECT 1 FROM epic_continuations WHERE continuation_run_id = ?"
            " UNION ALL SELECT 1 FROM epic_reservations WHERE run_id = ? LIMIT 1",
            (run_id, run_id),
        ).fetchone()
        return (
            ownership is None
            and self.get(run_id) is None
            and not self._continuation_run_launched(run_id)
        )

    def _retained_checkout_owner_run_ids(self, record: ContinuationRecord) -> list[str]:
        """Run rows retain checkout paths through continuation and audit records.

        Legacy rows with worker identities but no path binding remain possible
        owners when their item and branch match. Terminal status cannot exclude them.
        """
        rows = self._execute(
            "SELECT run_id FROM runs WHERE item_id = ? AND branch = ?"
            " AND (pgid IS NOT NULL OR owner_pid IS NOT NULL"
            " OR attempt_pgid IS NOT NULL OR container_name IS NOT NULL)"
            " UNION SELECT r.run_id FROM runs r JOIN epic_continuations c"
            " ON (r.run_id = c.original_run_id OR r.run_id = c.continuation_run_id)"
            " WHERE continuation_checkout_matches(c.worktree_identity, ?)"
            " UNION SELECT r.run_id FROM runs r JOIN audit_events a"
            " ON r.run_id = a.run_id WHERE continuation_checkout_matches("
            " CASE WHEN json_valid(a.payload)"
            " THEN json_extract(a.payload, '$.checkout') END, ?)",
            (
                record.item_id,
                record.branch,
                record.worktree_identity,
                record.worktree_identity,
            ),
        ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def continuation_expiry(
        self, record: ContinuationRecord
    ) -> ContinuationExpiry | None:
        """Read durable expiry without consulting purgeable run rows."""
        row = self._execute(
            "SELECT abandonment_identity, abandoned_generation, replacing_checkout_id,"
            " replacing_run_id, replaced_at FROM continuation_expiries"
            " WHERE epic_ref = ? AND item_id = ? AND original_run_id = ?"
            " AND branch = ? AND worktree_identity = ? AND abandoned_generation = ?",
            (
                *(getattr(record, key) for key in _CONTINUATION_FAMILY_FIELDS),
                record.generation,
            ),
        ).fetchone()
        return ContinuationExpiry(**dict(row)) if row is not None else None

    def record_continuation_expiry(
        self, record: ContinuationRecord, expiry: ContinuationExpiry
    ) -> None:
        """Persist validated replacement evidence before the worker can write.

        No foreign key points to runs. Retention may purge the replacing run,
        but cannot restore the consumed abandonment authority.
        """

        def write() -> None:
            self._execute(
                "INSERT INTO continuation_expiries (abandonment_identity,"
                " epic_ref, item_id, original_run_id, branch, worktree_identity,"
                " abandoned_generation, replacing_checkout_id, replacing_run_id,"
                " replaced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(abandonment_identity) DO NOTHING",
                (
                    expiry.abandonment_identity,
                    *(getattr(record, key) for key in _CONTINUATION_FAMILY_FIELDS),
                    expiry.abandoned_generation,
                    expiry.replacing_checkout_id,
                    expiry.replacing_run_id,
                    expiry.replaced_at,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("record_continuation_expiry", write)

    def continuation_replacement_events(
        self, record: ContinuationRecord
    ) -> Iterator[AuditEvent]:
        """Page only this abandoned generation's later replacement evidence.

        Query failures propagate. No partial unreadable history grants authority.
        The replacement payload calls its worktree identity ``checkout``.
        """
        assert record.terminal_at is not None
        after_id = 0
        while True:
            rows = self._continuation_replacement_page(record, after_id)
            if not rows:
                return
            yield from (_row_to_audit_event(row) for row in rows)
            after_id = rows[-1]["id"]

    def _continuation_replacement_page(
        self, record: ContinuationRecord, after_id: int
    ) -> list[sqlite3.Row]:
        assert record.terminal_at is not None
        return self._execute(
            "SELECT * FROM audit_events"
            " WHERE event = 'checkout_replaced' AND id > ? AND timestamp > ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.epic_ref') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.item_id') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.original_run_id') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.branch') END = ?"
            " AND continuation_checkout_matches(CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.checkout') END, ?)"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.generation') END = ?"
            " ORDER BY id ASC LIMIT ?",
            (
                after_id,
                _dt_to_iso(record.terminal_at),
                *(getattr(record, key) for key in _CONTINUATION_FAMILY_FIELDS),
                record.generation,
                _MAX_CONTINUATION_GENERATIONS,
            ),
        ).fetchall()

    def continuation_was_abandoned(self, record: ContinuationRecord) -> bool:
        """Match abandonment to the exact terminal recovery identity."""
        return self.continuation_abandonment_identity(record) is not None

    def continuation_abandonment_identity(
        self, record: ContinuationRecord
    ) -> str | None:
        """Return the validated event identity that authorizes checkout replacement."""
        from kew.audit_identity import continuation_evidence

        if record.state != "terminal":
            return None
        payload = continuation_evidence(
            phase="abandon",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            generation=record.generation,
            continuation_run_id=record.continuation_run_id,
        )
        expected = continuation_evidence_identity("continuation_abandoned", payload)
        matched = any(
            isinstance(event.payload, dict)
            and event.payload.get("evidence_identity") == expected
            and continuation_evidence_identity("continuation_abandoned", event.payload)
            == expected
            for event in self.list_audit_events(
                limit=None,
                event="continuation_abandoned",
                run_id=record.continuation_run_id or record.original_run_id,
            )
        )
        return expected if matched else None

    def _continuation_target_claim(self, run_id: str) -> dict[str, Any] | None:
        target_rows = self._execute(
            "SELECT id, run_id, payload FROM audit_events"
            " WHERE event = 'continuation_claimed' AND run_id = ?"
            " ORDER BY id ASC LIMIT 2",
            (run_id,),
        ).fetchall()
        if not target_rows:
            mutable = self._execute(
                "SELECT 1 FROM epic_continuations"
                " WHERE continuation_run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            if mutable is not None:
                raise RuntimeError(
                    "continuation claim evidence is missing; "
                    "use `kew epic abandon` to recover"
                )
            self._require_no_retained_missing_claim(run_id)
            return None
        if len(target_rows) != 1:
            raise ValueError("continuation run has ambiguous claim evidence")
        return _continuation_claim_payload(target_rows[0])

    def _require_no_retained_missing_claim(self, run_id: str) -> None:
        """Reject a possible requeued claim that lacks immutable evidence."""
        run = self._execute(
            "SELECT item_id, branch, run_kind, started_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        # PR-subject work never continued the branch it names.
        if run is not None and run["run_kind"] in PR_SUBJECT_RUN_KINDS:
            return
        identity = _retained_recovery_identity(run)
        if identity is None:
            return
        item_id, branch = identity
        family_rows = self._execute(
            "SELECT * FROM epic_continuations"
            " WHERE item_id = ? AND branch = ? AND generation > 0"
            " ORDER BY epic_ref, original_run_id, worktree_identity LIMIT ?",
            (item_id, branch, _MAX_CONTINUATION_GENERATIONS + 1),
        ).fetchall()
        _require_retained_recovery_bound(family_rows)
        if run is not None:
            family_rows = [
                row
                for row in family_rows
                if not self._run_follows_abandonment(row, run["started_at"])
            ]
        if family_rows and _retained_family_names_other_original(family_rows, run_id):
            raise RuntimeError(
                "continuation claim evidence is missing; "
                "use `kew epic abandon` to recover"
            )

    def _run_follows_abandonment(self, row: sqlite3.Row, started_at: str) -> bool:
        """A fresh run after abandonment does not inherit the discarded checkout."""
        record = _row_to_continuation(row)
        started = _iso_to_dt(started_at)
        return (
            record.terminal_at is not None
            and started is not None
            and started > record.terminal_at
            and self.continuation_was_abandoned(record)
        )

    def _continuation_family_events(
        self, target: dict[str, Any], event: str
    ) -> Iterator[sqlite3.Row]:
        """Page immutable evidence before filtering or enforcing generation bounds.

        Keyset pagination preserves every audit row. A failed page read raises;
        no page count truncates cancellation history or hides later owners.
        """
        after_id = 0
        while True:
            rows = self._continuation_family_page(target, event, after_id)
            if not rows:
                return
            yield from rows
            after_id = rows[-1]["id"]

    def _continuation_family_page(
        self, target: dict[str, Any], event: str, after_id: int
    ) -> list[sqlite3.Row]:
        return self._execute(
            "SELECT id, run_id, payload FROM audit_events"
            " WHERE event = ? AND id > ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.epic_ref') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.item_id') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.original_run_id') END = ?"
            " AND CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.branch') END = ?"
            " AND continuation_checkout_matches(CASE WHEN json_valid(payload)"
            " THEN json_extract(payload, '$.worktree_identity') END, ?)"
            " ORDER BY id ASC LIMIT ?",
            (
                event,
                after_id,
                *(target[field] for field in _CONTINUATION_FAMILY_FIELDS),
                _MAX_CONTINUATION_GENERATIONS,
            ),
        ).fetchall()

    def live_reservation_counts_by_epic(
        self,
        *,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> dict[str, int]:
        """Live epic-node reservations grouped by ``epic_ref``, fleet-wide (#864).

        Powers the fleet-wide ``[parallel] max_workers`` cap's per-epic fairness
        split (F4): every reservation row is one live slot held by its epic (a
        node stays reserved from dispatch until its run terminates — see
        :meth:`live_run_count`), so counting rows per ``epic_ref`` across the
        WHOLE table gives each concurrently-driven epic's current footprint off
        the same rows the aggregate cap counts — no separate counter to drift.

        ``liveness`` (#2094) makes the footprint owner-liveness-aware, exactly as
        it does for :meth:`live_run_count`: a hard-killed run strands its
        ``pending`` row AND the reservation backing it, and only the epic's own
        driver reconciles that away — so an epic abandoned mid-drive leaks a
        reservation nobody reaps. Unprobed (the default) every row counts; given
        a probe, a row whose work item is PROVABLY dead is dropped. A
        reservation with no run row stays unprobeable and always counts.
        """
        if liveness is None:
            rows = self._execute(
                "SELECT epic_ref, COUNT(*) FROM epic_reservations GROUP BY epic_ref"
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}
        _, dead_ids = self._dead_pending_item_ids(liveness)
        counts: dict[str, int] = {}
        for row in self._execute(
            "SELECT epic_ref, item_id FROM epic_reservations"
        ).fetchall():
            if row["item_id"] in dead_ids:
                continue
            epic_ref = str(row["epic_ref"])
            counts[epic_ref] = counts.get(epic_ref, 0) + 1
        return counts

    def contending_epic_refs(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = CONTENTION_STALE_AFTER,
        liveness: Callable[[RunRecord], bool | None] | None = None,
    ) -> set[str]:
        """Epic refs currently CONTENDING for fleet worker slots (#864 F4).

        An epic contends when it is on the driver-state roster (``kew epic
        plan`` seeds a row via :meth:`register_epic`), its last tick did NOT
        reach a terminal verdict — i.e. it is fresh (never ticked,
        ``last_signal IS NULL``) or still ``progressing`` — and it is still
        being driven. ``complete``, ``stalled`` and ``awaiting_merges`` (see
        :class:`~kew.graph.driver.result.TerminalSignal`) are excluded: those
        epics hold no dispatchable frontier, so counting them would shrink every
        other epic's fair share and idle fleet slots.

        This is the cold-start half of the fairness denominator: a sibling that
        is registered and ready but has not dispatched yet holds no reservation,
        so :meth:`live_reservation_counts_by_epic` cannot see it. Without this a
        single independent tick would find itself the only "active" epic, take
        the whole cap, and permanently starve the sibling whose nodes never get
        a slot to reserve. Counting registered non-terminal epics makes that
        sibling divide the cap regardless of which epic ticks first.

        Contention AGES OUT (#2094). A ``progressing`` row is only evidence of
        contention while some driver is still ticking it: an epic finished by
        hand or abandoned keeps that row forever and would divide the cap for
        good (four such rows once pinned a live epic to one node per tick). A
        row whose last tick is older than ``stale_after`` therefore stops
        contending UNLESS the epic still holds a live reservation — a real
        fleet slot outranks any clock. A registered-but-never-ticked row has no
        tick to age and keeps contending: that is the cold-start case above,
        and it clears itself on the epic's first tick.

        The reservation exemption PROBES the slot rather than trusting the row.
        Reservations are reconciled by their own epic's driver, so the very
        epic the age-out targets — abandoned mid-drive, its dispatch
        hard-killed — is the one whose leaked reservation nobody reaps; read
        blind, that leak would exempt it forever and re-create the failure
        this age-out retires. ``liveness`` is threaded to
        :meth:`live_reservation_counts_by_epic` so only a slot that is not
        provably dead exempts its epic. Unprobed (the default) every
        reservation still exempts: prove dead, never infer.
        """
        rows = self._execute(
            "SELECT epic_ref, last_tick_ts FROM epic_driver_state"
            " WHERE last_signal IS NULL"
            " OR last_signal NOT IN ('complete', 'stalled', 'awaiting_merges')"
        ).fetchall()
        cutoff = (now if now is not None else datetime.now(UTC)) - stale_after
        reserved = set(self.live_reservation_counts_by_epic(liveness=liveness))
        return {
            str(row["epic_ref"])
            for row in rows
            if str(row["epic_ref"]) in reserved
            or not _tick_is_stale(row["last_tick_ts"], cutoff)
        }

    def hold_epic_node_item(
        self, epic_ref: str, item_id: str, reason: str, *, node_ids: Sequence[str] = ()
    ) -> None:
        """Hold a DAG node keyed on the durable ``item_id`` (L3).

        *node_ids* is the full list a hold whose reason is capped names
        (#2884); it is written in the hold's own transaction and replaces
        whatever list the row carried, so a hold written without one carries
        none. :meth:`epic_hold_node_ids` reads it back.
        """
        issue_num = _epic_db_issue_num(item_id)

        def _do_hold() -> None:
            # held_at means "when the hold STARTED" — preserve it across
            # repeat calls for the same still-held node (only reason may
            # change). A genuinely new hold gets a fresh held_at via the
            # INSERT, since clear_epic_hold deletes the row first.
            try:
                self._execute(
                    "INSERT INTO epic_holds"
                    " (issue_num, epic_ref, reason, held_at, item_id)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(item_id) DO UPDATE SET"
                    " reason = excluded.reason",
                    (
                        issue_num,
                        epic_ref,
                        reason,
                        _dt_to_iso(datetime.now(UTC)),
                        item_id,
                    ),
                )
                self._write_hold_node_ids_in_transaction(item_id, node_ids)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        self._with_busy_retry("hold_epic_node_item", _do_hold)

    def hold_epic_node_unless(
        self,
        epic_ref: str,
        item_id: str,
        reason: str,
        *,
        keep: Callable[[str], bool],
        meta: tuple[str, str] | None = None,
    ) -> bool:
        """Atomically hold ``item_id`` with *reason* unless its current hold is
        one *keep* says to leave alone; True when the row now says *reason*.

        The conditional upsert a writer that owns ONE reason needs: read the
        standing hold and write over it inside one ``BEGIN IMMEDIATE``
        transaction, so another writer's hold upserted between a separate
        lookup and the write is never replaced (#2869). ``held_at`` is kept
        across a repeat hold, exactly as :meth:`hold_epic_node_item` keeps it.
        Owns its rollback, as :meth:`remove_epic_hold` does.

        *meta* is one ``(key, value)`` upserted in the same transaction, for a
        writer whose record of why it held must stand or fall with the hold
        (#3191): written only when the hold is.
        """
        issue_num = _epic_db_issue_num(item_id)

        def _do_hold() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._execute(
                        "SELECT reason FROM epic_holds WHERE item_id = ?",
                        (item_id,),
                    ).fetchone()
                    if row is not None and keep(str(row["reason"])):
                        self._conn.rollback()
                        return False
                    self._execute(
                        "INSERT INTO epic_holds"
                        " (issue_num, epic_ref, reason, held_at, item_id)"
                        " VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT(item_id) DO UPDATE SET"
                        " reason = excluded.reason",
                        (
                            issue_num,
                            epic_ref,
                            reason,
                            _dt_to_iso(datetime.now(UTC)),
                            item_id,
                        ),
                    )
                    self._write_hold_node_ids_in_transaction(item_id, ())
                    if meta is not None:
                        self._execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            meta,
                        )
                    self._conn.commit()
                    return True
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("hold_epic_node_unless", _do_hold)

    def hold_epic_node(
        self, epic_ref: str, item: str, reason: str, *, node_ids: Sequence[str] = ()
    ) -> None:
        """Item-id façade over :meth:`hold_epic_node_item`; coerces a legacy
        int (a GitHub issue number) to its ``str`` id at the boundary."""
        self.hold_epic_node_item(epic_ref, str(item), reason, node_ids=node_ids)

    def _write_hold_node_ids_in_transaction(
        self, item_id: str, node_ids: Sequence[str]
    ) -> None:
        """Bind *node_ids* to ``item_id``'s hold, inside the caller's
        transaction; none deletes the list, so a release or another writer's
        hold never leaves an earlier hold's ids behind (#2884)."""
        key = f"{_EPIC_HOLD_NODE_IDS_META_PREFIX}{item_id}"
        if not node_ids:
            self._execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(list(node_ids))),
        )

    def epic_hold_node_ids(self, item_id: str) -> tuple[str, ...]:
        """The node ids ``item_id``'s standing hold was written with (#2884).

        Empty when the node is not held, its hold carries no list, or the
        stored value is unreadable.
        """
        held = self._execute(
            "SELECT 1 FROM epic_holds WHERE item_id = ?", (item_id,)
        ).fetchone()
        raw = self.get_meta(f"{_EPIC_HOLD_NODE_IDS_META_PREFIX}{item_id}")
        if held is None or raw is None:
            return ()
        try:
            data = json.loads(raw)
        except ValueError:
            _logger.warning("unreadable node ids on %s's hold", item_id)
            return ()
        if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
            return ()
        return tuple(data)

    def clear_epic_hold_item(self, item_id: str) -> None:
        def _do_clear() -> None:
            try:
                self._execute("DELETE FROM epic_holds WHERE item_id = ?", (item_id,))
                self._write_hold_node_ids_in_transaction(item_id, ())
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        self._with_busy_retry("clear_epic_hold_item", _do_clear)

    def clear_epic_hold(self, item: str) -> None:
        """Item-id façade over :meth:`clear_epic_hold_item`; coerces a legacy
        int (a GitHub issue number) to its ``str`` id at the boundary."""
        self.clear_epic_hold_item(str(item))

    def _write_release_receipt_in_transaction(
        self, reason: str, key: str | None, prefixes: Collection[str]
    ) -> None:
        """Record that an operator released THIS hold reason, inside the caller's
        transaction (see ``remove_epic_hold``'s ``receipt_key``).

        A no-op unless the caller named a key and the released reason starts
        with one of its prefixes — ``startswith(())`` is False, so the default
        empty vocabulary leaves every other release a plain delete.
        """
        if key is None or not reason.startswith(tuple(prefixes)):
            return
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, reason),
        )

    def remove_epic_hold(
        self,
        epic_ref: str,
        item_id: str,
        *,
        fresh_review_cycle_reasons: Collection[str] = frozenset(),
        receipt_key: str | None = None,
        receipt_reason_prefixes: Collection[str] = (),
        only_reason_prefixes: Collection[str] = (),
        keep: Callable[[EpicHold], bool] | None = None,
    ) -> EpicHold | None:
        """Atomically remove and return the hold for ``epic_ref``/``item_id``.

        The operator-unhold primitive (#SFD-136): a single ``BEGIN IMMEDIATE``
        transaction reads the row then deletes it, so a concurrent re-hold
        cannot slip a fresh row in between a separate lookup and clear — which
        would delete the newly written hold while reporting the old one's
        reason/held-since. Returns the removed :class:`EpicHold`, or ``None``
        when no row matched that epic/item (the operator's exit-2 signal).

        ``fresh_review_cycle_reasons`` names the reasons whose release must also
        restart the node's review cycle — the ``epic_retries`` marker the round
        counters read as the cycle lower bound. It rides inside the same
        transaction because the driver's next review pass is the thing being
        raced: a release that left the count standing would be re-HELD before a
        second call could reset it (#2154). Caller-supplied (the vocabulary
        lives in :mod:`kew.epic_holds`) and empty by default, so the driver's
        own ``ci_pending`` release stays a plain delete. A name matches the
        reason itself or the reason carrying ``: <detail>`` after it (#3201).

        ``receipt_key``/``receipt_reason_prefixes`` are the same shape for a
        DERIVED hold — one a later pass would simply compute again (the merge
        queue's ``merge_gate_touched``, #2184). Deleting the row releases
        nothing there, so the released reason is written to that ``meta`` key
        for the deriving pass to read back, and for the same reason it rides in
        this transaction: between a bare delete and a separate write, the
        driver's next pass re-derives and re-writes the hold. Vocabulary is
        caller-supplied and empty by default.

        ``only_reason_prefixes`` narrows the delete to holds the caller owns: a
        row whose reason carries none of them is left alone and ``None`` is
        returned. It is read INSIDE the transaction because that is the whole
        point — the driver clearing its own superseded dispatch refusal (#2217)
        must not delete the honored hold another writer upserted over it
        between a separate lookup and the clear. Empty by default: the operator
        unhold releases whatever reason it finds.

        ``keep`` is the same narrowing as a predicate, read in the same
        transaction: a row it returns True for is left alone and ``None`` is
        returned. The drive's reservation sweep uses it to retire only a hold
        that predates its own check while no newer generation holds the item
        (#3192). It may read the store; it must not write.

        Owns its rollback: ``_with_busy_retry`` only rolls back in its "locked"
        branch, so any other exception here must not leave the write
        transaction open (that would wedge every other writer under WAL).
        """

        def _do_remove() -> EpicHold | None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._execute(
                        "SELECT issue_num, epic_ref, reason, held_at, item_id"
                        " FROM epic_holds WHERE epic_ref = ? AND item_id = ?",
                        (epic_ref, item_id),
                    ).fetchone()
                    if row is None:
                        self._conn.rollback()
                        return None
                    hold = _row_to_epic_hold(row)
                    if not _hold_reason_admitted(hold.reason, only_reason_prefixes):
                        self._conn.rollback()
                        return None
                    if keep is not None and keep(hold):
                        self._conn.rollback()
                        return None
                    self._execute(
                        "DELETE FROM epic_holds WHERE epic_ref = ? AND item_id = ?",
                        (epic_ref, item_id),
                    )
                    self._write_hold_node_ids_in_transaction(item_id, ())
                    if any(
                        hold.reason == name or hold.reason.startswith(f"{name}:")
                        for name in fresh_review_cycle_reasons
                    ):
                        self._mark_epic_retry_item_in_transaction(
                            epic_ref, item_id, datetime.now(UTC)
                        )
                    self._write_release_receipt_in_transaction(
                        hold.reason, receipt_key, receipt_reason_prefixes
                    )
                    self._conn.commit()
                    return hold
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("remove_epic_hold", _do_remove)

    def list_epic_holds(self, epic_ref: str) -> list[EpicHold]:
        rows = self._execute(
            "SELECT issue_num, epic_ref, reason, held_at, item_id FROM epic_holds"
            " WHERE epic_ref = ? ORDER BY item_id",
            (epic_ref,),
        ).fetchall()
        return [_row_to_epic_hold(row) for row in rows]

    def list_all_epic_holds(self) -> list[EpicHold]:
        """Every epic-DAG node hold, across all epics (#680 Group 1).

        The cross-epic generalization of ``list_epic_holds``; ordered by
        ``epic_ref`` then ``item_id`` for stable display.
        """
        rows = self._execute(
            "SELECT issue_num, epic_ref, reason, held_at, item_id FROM epic_holds"
            " ORDER BY epic_ref, item_id"
        ).fetchall()
        return [_row_to_epic_hold(row) for row in rows]

    def sync_orphan_prs(self, detected: Sequence[tuple[int, str, str, str]]) -> None:
        """Reconcile the orphan_prs table to the currently ``detected`` set.

        Each entry is ``(pr_number, url, branch, reason)``. Upserts every
        detected orphan — preserving ``detected_at`` and ``acked_at`` on rows
        that persist (only url/branch/reason may change) — and deletes any
        stored row not in ``detected`` (its PR closed or its branch gained a
        successful run). Detection lives in ``kew.outcomes``; this method only
        persists its result.
        """

        def _do_sync() -> None:
            now = _dt_to_iso(datetime.now(UTC))
            keep = [int(pr_number) for (pr_number, _u, _b, _r) in detected]
            for pr_number, url, branch, reason in detected:
                self._execute(
                    "INSERT INTO orphan_prs"
                    " (pr_number, url, branch, reason, detected_at, acked_at)"
                    " VALUES (?, ?, ?, ?, ?, NULL)"
                    " ON CONFLICT(pr_number) DO UPDATE SET"
                    " url = excluded.url,"
                    " branch = excluded.branch,"
                    " reason = excluded.reason",
                    (int(pr_number), url, branch, reason, now),
                )
            if keep:
                placeholders = ",".join("?" for _ in keep)
                delete_sql = (
                    "DELETE FROM orphan_prs "
                    f"WHERE pr_number NOT IN ({placeholders})"  # nosec B608
                )
                self._execute(delete_sql, keep)
            else:
                self._execute("DELETE FROM orphan_prs")
            self._conn.commit()

        self._with_busy_retry("sync_orphan_prs", _do_sync)

    def list_orphan_prs(self, include_acked: bool = False) -> list[OrphanPRRecord]:
        """Persisted orphan PRs; hides acked rows unless ``include_acked`` (#680)."""
        sql = (
            "SELECT pr_number, url, branch, reason, detected_at, acked_at"
            " FROM orphan_prs"
        )
        if not include_acked:
            sql += " WHERE acked_at IS NULL"
        sql += " ORDER BY pr_number"
        rows = self._execute(sql).fetchall()
        return [_row_to_orphan_pr(row) for row in rows]

    def ack_orphan_pr(self, pr_number: int) -> bool:
        """Set ``acked_at`` on an orphan PR row (the ack verb, #682). Idempotent.

        Never touches the PR itself — acknowledging is a ledger-only act; the
        human decides whether to close. Returns True if a tracked orphan row
        exists for ``pr_number`` (whether newly acked or already acked), False
        if no such row is persisted. The first ``acked_at`` is preserved across
        repeat calls (the UPDATE only fires while ``acked_at IS NULL``).
        """

        def _do_ack() -> bool:
            cursor = self._execute(
                "UPDATE orphan_prs SET acked_at = ?"
                " WHERE pr_number = ? AND acked_at IS NULL",
                (_dt_to_iso(datetime.now(UTC)), pr_number),
            )
            self._conn.commit()
            if cursor.rowcount and cursor.rowcount > 0:
                return True
            # No row updated: either already acked, or no such orphan. Probe to
            # tell the idempotent re-ack (row exists) from an unknown PR.
            row = self._execute(
                "SELECT 1 FROM orphan_prs WHERE pr_number = ?", (pr_number,)
            ).fetchone()
            return row is not None

        return self._with_busy_retry("ack_orphan_pr", _do_ack)

    def mark_epic_retry_item(self, epic_ref: str, item_id: str) -> None:
        """Mark a DAG-node retry keyed on the durable ``item_id`` (L3)."""

        def _do_mark() -> None:
            self._mark_epic_retry_item_in_transaction(
                epic_ref, item_id, datetime.now(UTC)
            )
            self._conn.commit()

        self._with_busy_retry("mark_epic_retry_item", _do_mark)

    def mark_epic_retry_item_attested(
        self,
        epic_ref: str,
        item_id: str,
        *,
        audit: Mapping[str, Any],
        precheck: Callable[[], None],
    ) -> None:
        """Re-run *precheck*, then append the ``epic_node_retry`` event and the
        retry marker, in one ``BEGIN IMMEDIATE`` (#2960).

        *audit* holds the event's other :meth:`append_audit_event` keywords.
        *precheck* reads through this connection under the write lock, so the
        state it refuses cannot change before the writes land; if it raises,
        nothing is written.
        """
        payload = audit.get("payload")

        def _do_mark() -> None:
            values = (
                _normalize_event_ts(audit["timestamp"]),
                "epic_node_retry",
                audit.get("run_id"),
                audit.get("issue_num"),
                audit.get("repo"),
                audit.get("actor_type"),
                audit.get("actor_id"),
                audit.get("endpoint"),
                audit.get("policy_hash"),
                json.dumps(payload) if payload is not None else None,
                audit.get("item_id"),
            )
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    precheck()
                    self._chained_insert_in_transaction(values)
                    self._mark_epic_retry_item_in_transaction(
                        epic_ref, item_id, datetime.now(UTC)
                    )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        self._with_busy_retry("mark_epic_retry_item_attested", _do_mark)

    def _mark_epic_retry_item_in_transaction(
        self, epic_ref: str, item_id: str, retried_at: datetime
    ) -> None:
        """Write the retry marker within the caller's transaction.

        Runs the marker is already bound to stay superseded (#2969): a reaped
        run whose clock reads ahead of this retry would otherwise become the
        node's newest failure again. The retry's own time is the cutoff
        again, so a bound marker's own cutoff is cleared.
        """
        self._execute(
            "INSERT INTO epic_retries"
            " (issue_num, epic_ref, retried_at, item_id, superseded_run_ids,"
            " superseded_before)"
            " VALUES (?, ?, ?, ?, NULL, NULL)"
            " ON CONFLICT(item_id) DO UPDATE SET"
            " epic_ref = excluded.epic_ref,"
            " retried_at = excluded.retried_at,"
            " superseded_before = NULL",
            (
                _epic_db_issue_num(item_id),
                epic_ref,
                _dt_to_iso(retried_at),
                item_id,
            ),
        )

    def _supersede_run_in_transaction(
        self, epic_ref: str, item_id: str, run_id: str
    ) -> None:
        """Bind the node's retry marker to one reaped run (#2969).

        The marker supersedes *run_id* by identity, whatever the clocks say,
        together with every run it already bound and every implementation run
        an earlier operator retry superseded by time (:meth:`_carried_runs`).
        Its own failure cutoff becomes :data:`NO_EARLIER_RETRY`, so no clock
        comparison supersedes anything else: a replacement run, however its
        clock reads, is never superseded by this marker. ``retried_at`` is
        left as an earlier operator retry wrote it (the review cycle's real
        start), or :data:`NO_EARLIER_RETRY` with none.
        """
        superseded = sorted(self._carried_runs(item_id, run_id) | {run_id})
        self._execute(
            "INSERT INTO epic_retries"
            " (issue_num, epic_ref, retried_at, item_id, superseded_run_ids,"
            " superseded_before)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(item_id) DO UPDATE SET"
            " epic_ref = excluded.epic_ref,"
            " superseded_run_ids = excluded.superseded_run_ids,"
            " superseded_before = excluded.superseded_before",
            (
                _epic_db_issue_num(item_id),
                epic_ref,
                _dt_to_iso(NO_EARLIER_RETRY),
                item_id,
                json.dumps(superseded),
                _dt_to_iso(NO_EARLIER_RETRY),
            ),
        )

    def _carried_runs(self, item_id: str, run_id: str) -> frozenset[str]:
        """The runs a run-bound marker keeps from the marker before it (#2969).

        The runs that marker bound, plus the implementation runs other than
        *run_id* started before its cutoff — those an operator retry
        superseded by time. Carrying them by identity rather than as a cutoff
        means a replacement whose clock was set back past them is never
        superseded with them. Review, CI-fix and triage runs never derive node
        status, so they are never carried either.
        """
        row = self._execute(
            "SELECT retried_at, superseded_run_ids, superseded_before"
            " FROM epic_retries WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return frozenset()
        cutoff = _iso_to_dt(row["superseded_before"] or row["retried_at"])
        rows = self._execute(
            "SELECT run_id, started_at, run_kind FROM runs"
            " WHERE item_id = ? AND run_id != ?",
            (item_id, run_id),
        ).fetchall()
        superseded_by_time = {
            str(other)
            for (other, started, kind) in rows
            if kind not in PR_SUBJECT_RUN_KINDS
            and kind != TRIAGE_RUN_KIND
            and _started_before(started, cutoff)
        }
        return _superseded_run_ids(row["superseded_run_ids"]) | superseded_by_time

    def mark_epic_retry(self, epic_ref: str, issue_num: int) -> None:
        """Int-shim over :meth:`mark_epic_retry_item` for GitHub callers."""
        self.mark_epic_retry_item(epic_ref, str(issue_num))

    def list_epic_retries(self, epic_ref: str) -> list[EpicRetry]:
        rows = self._execute(
            "SELECT issue_num, epic_ref, retried_at, item_id, superseded_run_ids,"
            " superseded_before FROM epic_retries WHERE epic_ref = ? ORDER BY item_id",
            (epic_ref,),
        ).fetchall()
        return [_row_to_epic_retry(row) for row in rows]

    def superseded_runs_for_item(self, item_id: str) -> frozenset[str]:
        """The runs a host-restart retry marker binds for *item_id* (#2969)."""
        row = self._execute(
            "SELECT superseded_run_ids FROM epic_retries WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return _superseded_run_ids(row["superseded_run_ids"] if row else None)

    def _row_to_epic_tick(self, row: sqlite3.Row) -> EpicTick:
        return EpicTick(
            id=int(row["id"]),
            epic_ref=row["epic_ref"],
            tick_ts=row["tick_ts"],
            snapshot=json.loads(row["snapshot"]),
            actions=json.loads(row["actions"]),
            snapshot_hash=row["snapshot_hash"],
        )

    def append_epic_tick(
        self,
        *,
        epic_ref: str,
        tick_ts: str,
        snapshot: dict[str, Any],
        actions: dict[str, Any],
        snapshot_hash: str,
        rehash: Callable[[dict[str, Any]], str] | None = None,
    ) -> tuple[int, bool]:
        """Dedup-append one journal row. NOT best-effort: raises on failure.

        Returns (id, inserted) tuple: id is the row id (new or existing), and
        inserted is True iff a row was written. Dedup compares ONLY against
        the epic's LATEST row (per #454 spec constraint 2) — a snapshot hash
        that recurs after intervening state changes appends a new row, so the
        journal preserves the full state sequence.

        *rehash* is the caller's current hash function. When given, the latest
        row also matches if its stored snapshot re-hashes to *snapshot_hash*:
        a row hashed by an older build (one that hashed ``merge_state_at``,
        #2879) would otherwise never match, and the first upgraded tick would
        append a false change.

        A journal row is never rewritten. Each node's merge-state read time is
        written to ``epic_merge_state_reads`` in the same transaction, matched
        or not, so a quiet tick appends no row yet the freshness readers
        (:meth:`latest_node_pr_facts`) still see when the state was last read.
        """
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        reads = _merge_state_reads(epic_ref, snapshot)

        def _do() -> tuple[int, bool]:
            with self._txn_lock:
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "SELECT id, snapshot, snapshot_hash FROM epic_ticks"
                        " WHERE epic_ref = ? ORDER BY id DESC LIMIT 1",
                        (epic_ref,),
                    )
                    latest_row = cur.fetchone()
                    cur.executemany(_MERGE_STATE_READ_UPSERT, reads)
                    if latest_row and _journal_row_matches(
                        latest_row, snapshot_hash, rehash
                    ):
                        self._conn.commit()
                        return int(latest_row["id"]), False
                    cur.execute(
                        "INSERT INTO epic_ticks"
                        " (epic_ref, tick_ts, snapshot, actions, snapshot_hash)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            epic_ref,
                            tick_ts,
                            snapshot_json,
                            json.dumps(actions, sort_keys=True, separators=(",", ":")),
                            snapshot_hash,
                        ),
                    )
                    self._conn.commit()
                    return int(cur.lastrowid or 0), True
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("append_epic_tick", _do)

    @staticmethod
    def _row_to_epic_tick_log(row: sqlite3.Row) -> EpicTickLogEntry:
        marked = row["signal"] == EPIC_TICK_LOG_REACHED_TICK
        return EpicTickLogEntry(
            id=int(row["id"]),
            epic_ref=str(row["epic_ref"]),
            ts_started=str(row["ts_started"]),
            ts_finished=row["ts_finished"],
            outcome=row["outcome"],
            signal=None if marked else row["signal"],
            n_dispatched=int(row["n_dispatched"] or 0),
            n_merged=int(row["n_merged"] or 0),
            n_held=int(row["n_held"] or 0),
            n_stuck=int(row["n_stuck"] or 0),
            journal_tick_id=(
                int(row["journal_tick_id"])
                if row["journal_tick_id"] is not None
                else None
            ),
            error_class=row["error_class"],
            reached_tick=marked or row["outcome"] in EPIC_TICK_REACHED_OUTCOMES,
        )

    def begin_epic_tick_log(self, epic_ref: str, ts_started: str) -> int:
        """Open one tick-attempt row and return its id (#2304).

        The row is written BEFORE the tick does anything, so a tick that never
        finishes — killed driver, raised exception before the handler, host
        reboot — still leaves evidence it was attempted. An open row (no
        ``ts_finished``) is exactly what the strip draws as live, so there is no
        separate "running" flag to go stale. A row whose driver never comes
        back is closed as ``abandoned`` by a later tick of the same epic
        (:meth:`abandon_open_epic_tick_logs`); nothing here waits on that,
        because the reader bounds how long an open row may read as live.

        Prunes the epic's oldest rows past :data:`EPIC_TICK_LOG_CAP` in the same
        transaction. Pruning here rather than on finish means the cap holds even
        for an epic whose driver keeps dying before it can close a row.
        """

        def _do() -> int:
            with self._txn_lock:
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "INSERT INTO epic_tick_log (epic_ref, ts_started)"
                        " VALUES (?, ?)",
                        (epic_ref, ts_started),
                    )
                    row_id = int(cur.lastrowid or 0)
                    # The oldest row we keep is the CAP'th from the newest;
                    # everything strictly older goes. With fewer than CAP rows
                    # the subquery is NULL, `id < NULL` is NULL, and nothing
                    # is deleted — no special case needed.
                    cur.execute(
                        "DELETE FROM epic_tick_log WHERE epic_ref = ? AND id < ("
                        " SELECT id FROM epic_tick_log WHERE epic_ref = ?"
                        " ORDER BY id DESC LIMIT 1 OFFSET ?"
                        ")",
                        (epic_ref, epic_ref, EPIC_TICK_LOG_CAP - 1),
                    )
                    self._conn.commit()
                    return row_id
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("begin_epic_tick_log", _do)

    def finish_epic_tick_log(
        self,
        row_id: int,
        *,
        ts_finished: str,
        outcome: str,
        signal: str | None = None,
        n_dispatched: int = 0,
        n_merged: int = 0,
        n_held: int = 0,
        n_stuck: int = 0,
        journal_tick_id: int | None = None,
        error_class: str | None = None,
        journaled: bool = False,
    ) -> None:
        """Close the attempt opened by :meth:`begin_epic_tick_log`.

        Writes the counts and the outcome in one UPDATE. A close with no
        ``signal`` keeps the :data:`EPIC_TICK_LOG_REACHED_TICK` mark the row
        may carry (#2882), so a tick that raised after it began still reads
        as one that began. The exception path
        calls this too, with ``outcome="failed"`` (or ``"empty_graph"``) and the
        exception's class name — never its message, which can carry a repo path
        or a token-bearing URL into a row every reader of the console can see.

        *journaled* says whether this attempt inserted the journal row it
        carries (#2949) — the change strip draws only those. It defaults to
        False: a quiet tick carries the id of the row it matched, and only the
        caller knows whether its own append inserted that row, so an attempt
        is never drawn as the change unless its caller says so.
        """
        if outcome not in EPIC_TICK_OUTCOMES:
            raise ValueError(f"unknown epic tick outcome: {outcome!r}")

        def _do() -> None:
            with self._txn_lock:
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "UPDATE epic_tick_log SET ts_finished = ?, outcome = ?,"
                        " signal = coalesce(?, signal), n_dispatched = ?,"
                        " n_merged = ?, n_held = ?, n_stuck = ?,"
                        " journal_tick_id = ?, error_class = ?, journaled = ?"
                        " WHERE id = ?",
                        (
                            ts_finished,
                            outcome,
                            signal,
                            n_dispatched,
                            n_merged,
                            n_held,
                            n_stuck,
                            journal_tick_id,
                            error_class,
                            int(journaled),
                            row_id,
                        ),
                    )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        self._with_busy_retry("finish_epic_tick_log", _do)

    def mark_epic_tick_log_reached(self, row_id: int) -> None:
        """Mark an OPEN attempt as having reached its tick (#2882).

        Called once ``begin_epic_tick`` has committed: the attempt is past the
        lock, the preflight and the empty-graph guard, so it is a heartbeat and
        no longer only a claim to be running. Stored in ``signal``
        (:data:`EPIC_TICK_LOG_REACHED_TICK`); a row already closed is left as
        it is.
        """

        def _do() -> None:
            with self._txn_lock:
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "UPDATE epic_tick_log SET signal = ?"
                        " WHERE id = ? AND ts_finished IS NULL",
                        (EPIC_TICK_LOG_REACHED_TICK, row_id),
                    )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        self._with_busy_retry("mark_epic_tick_log_reached", _do)

    def abandon_open_epic_tick_logs(
        self, epic_ref: str, *, before_id: int, ts_finished: str
    ) -> int:
        """Close this epic's rows left open by a driver that never came back.

        An open row (no ``ts_finished``) IS the live state, so a driver killed
        mid-tick — TaskStop, OOM, host reboot — leaves a row that nothing ever
        closes and every strip paints as a tick still running, permanently.
        The only process that can tell the difference is a driver that HOLDS
        this epic's lock: no other process can then be mid-tick on this epic,
        so an older open row belongs to one that is gone. Returns how many rows
        were closed.

        Bounded by row id, not by an elapsed stamp: ids are insert order, so
        "older than the row I just opened" needs no clock and can never catch a
        slow-but-living tick of the caller's own. The one row it can race is a
        locked-out driver's, opened a moment before the caller's and closed a
        moment after — ``finish_epic_tick_log`` is keyed by id and overwrites
        the ``abandoned`` outcome with the real one.
        """

        def _do() -> int:
            with self._txn_lock:
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    cur.execute(
                        "UPDATE epic_tick_log"
                        " SET ts_finished = ?, outcome = 'abandoned'"
                        " WHERE epic_ref = ? AND id < ? AND ts_finished IS NULL",
                        (ts_finished, epic_ref, before_id),
                    )
                    closed = cur.rowcount
                    self._conn.commit()
                    return closed
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("abandon_open_epic_tick_logs", _do)

    def list_epic_tick_log_tail(
        self, epic_ref: str, *, limit: int = 200
    ) -> list[EpicTickLogEntry]:
        """The epic's newest ``limit`` tick attempts, oldest-first in the page.

        Bounded in SQL and ordered by row id: a strip draws left-to-right in
        time, and id order IS time order here because the ids come from the
        begin-write.
        """
        if limit < 1 or limit > EPIC_TICK_LOG_TAIL_MAX:
            raise ValueError(
                f"epic tick log limit must be between 1 and {EPIC_TICK_LOG_TAIL_MAX}"
            )
        rows = self._execute(
            "SELECT * FROM epic_tick_log WHERE epic_ref = ? ORDER BY id DESC LIMIT ?",
            (epic_ref, limit),
        ).fetchall()
        return [self._row_to_epic_tick_log(r) for r in reversed(rows)]

    def list_epic_tick_log_marks(
        self, epic_ref: str, *, limit: int = 200
    ) -> list[EpicTickLogEntry]:
        """The epic's newest ``limit`` attempts a change strip draws (#2844).

        The changes (what CHANGE counts), the attempts that failed, and the one
        still running — oldest-first, like the tail. Filtered in SQL rather
        than from a tail page: on live data 196 of 200 attempts are no-ops, so
        a change reached the Epics list's strip only while fewer than a page of
        no-ops followed it. Reaches back over the whole kept log
        (:data:`EPIC_TICK_LOG_CAP`).

        A change is read off ``epic_ticks`` itself, not off the attempt that
        appended it (#2882): the journal append is the committed fact, and the
        tick-log writes around it are best-effort. An attempt whose begin or
        finish write failed carries no journal id, so a change read only off
        the log drew no dot while CHANGE went up. Each journal row is drawn
        as its own attempt where the log holds the attempt marked
        ``journaled`` — the one whose append inserted the row (#2949) — and
        that attempt began no later than the row. On an
        unchanged snapshot :meth:`append_epic_tick` returns the existing
        row's id, so every no-op after a change carries that id too, but
        never the mark, so pruning the change's attempt cannot promote a
        no-op in its place. Where the log no longer holds that attempt, the
        row is drawn as a closed row stamped with the journal's own instant.
        A journal-only row's id is the journal id negated, so it can never
        collide with an attempt's.
        """
        if limit < 1 or limit > EPIC_TICK_LOG_TAIL_MAX:
            raise ValueError(
                f"epic tick log limit must be between 1 and {EPIC_TICK_LOG_TAIL_MAX}"
            )
        attempts = self._execute(
            "SELECT *, julianday(ts_started) AS at FROM epic_tick_log"
            " WHERE epic_ref = ?"
            " AND ((journaled = 1 AND journal_tick_id IS NOT NULL)"
            " OR outcome = ? OR ts_finished IS NULL)"
            " ORDER BY id DESC LIMIT ?",
            (epic_ref, EPIC_TICK_OUTCOME_FAILED, limit),
        ).fetchall()
        journal = self._execute(_JOURNAL_MARK_SQL, (epic_ref, limit)).fetchall()
        marks = sorted(
            _merge_journal_marks(epic_ref, attempts, journal), key=lambda m: m[:2]
        )
        return [entry for _, _, entry in marks[-limit:]]

    def latest_epic_tick_attempts(
        self, refs: Sequence[str]
    ) -> dict[str, EpicTickLogEntry]:
        """The newest tick attempt of each epic in *refs*, keyed by ref, in ONE
        query.

        The roster's heartbeat reading (#2844 N18): a driver whose attempts
        are turned away before ``begin_epic_tick`` — or that changed nothing —
        moves no heartbeat field, and the epic page reads this row instead.
        Only the refs passed are read, on the ``(epic_ref, id)`` index, so an
        epic outside the roster — an archived one — costs nothing (#2900 C4).
        Epics with no attempt rows are absent.
        """
        wanted = list(dict.fromkeys(refs))
        if not wanted:
            return {}
        rows = self._execute(
            "SELECT * FROM epic_tick_log WHERE id IN"  # nosec B608 — placeholders are ? params, values are bound
            " (SELECT MAX(id) FROM epic_tick_log"
            f" WHERE {_in_clause('epic_ref', wanted)} GROUP BY epic_ref)",
            tuple(wanted),
        ).fetchall()
        return {str(r["epic_ref"]): self._row_to_epic_tick_log(r) for r in rows}

    def epic_tick_log_stamp(self, epic_ref: str) -> str:
        """This epic's tick-attempt stamp — see :func:`_epic_tick_log_stamp`.

        ``""`` for an epic with no attempt rows, which is the same stable key
        a never-driven epic gets, not an error. ``MAX(id)`` and the closed
        count are both plain aggregates — no bare column travels with the MAX,
        so this leans on no SQLite-specific aggregate behaviour.
        """
        row = self._execute(
            "SELECT MAX(id) AS last_id, COUNT(ts_finished) AS closed,"
            " COUNT(CASE WHEN signal = ? THEN 1 END) AS reached"
            " FROM epic_tick_log WHERE epic_ref = ?",
            (EPIC_TICK_LOG_REACHED_TICK, epic_ref),
        ).fetchone()
        if row is None:
            return ""
        return _epic_tick_log_stamp(
            row["last_id"], int(row["closed"] or 0), int(row["reached"] or 0)
        )

    def epic_tick_log_stamps(self, refs: Sequence[str]) -> dict[str, str]:
        """The tick-attempt stamp of each epic in *refs*, keyed by epic ref, in
        ONE query.

        The roster needs one per row and reads seventy rows every five seconds
        (C3/#2296), so this joins the batched-read bundle rather than costing a
        query per epic. Epics with no attempt rows are absent — the caller
        reads a missing stamp as the same ``""`` the point read returns.

        One grouped aggregate over a per-epic-capped table, and only over the
        refs passed, on the ``(epic_ref, id)`` index — so an epic outside the
        roster, an archived one, costs nothing, the same bound
        :meth:`latest_epic_tick_attempts` keeps over this table (#2900 C4).
        """
        wanted = list(dict.fromkeys(refs))
        if not wanted:
            return {}
        rows = self._execute(
            "SELECT epic_ref, MAX(id) AS last_id, COUNT(ts_finished) AS closed,"  # nosec B608 — placeholders are ? params, values are bound
            " COUNT(CASE WHEN signal = ? THEN 1 END) AS reached"
            f" FROM epic_tick_log WHERE {_in_clause('epic_ref', wanted)}"
            " GROUP BY epic_ref",
            (EPIC_TICK_LOG_REACHED_TICK, *wanted),
        ).fetchall()
        return {
            str(row["epic_ref"]): _epic_tick_log_stamp(
                row["last_id"], int(row["closed"] or 0), int(row["reached"] or 0)
            )
            for row in rows
        }

    def latest_epic_tick(self, epic_ref: str) -> EpicTick | None:
        row = self._execute(
            "SELECT * FROM epic_ticks WHERE epic_ref = ? ORDER BY id DESC LIMIT 1",
            (epic_ref,),
        ).fetchone()
        return self._row_to_epic_tick(row) if row else None

    def latest_epic_tick_snapshot(self, epic_ref: str) -> dict[str, Any] | None:
        """The epic's latest journal snapshot, or None when it cannot be read.

        The driver's predecessor read (#2949). It decodes only the ``snapshot``
        column, never ``actions``, which the predecessor does not use. A row
        whose snapshot is not a JSON object reads as no predecessor, with a
        warning: :meth:`latest_epic_tick` raises on it, after the tick's
        driver work, so the journal write and the finish heartbeat would never
        happen and every later tick would meet the same row.
        """
        row = self._execute(
            "SELECT id, snapshot FROM epic_ticks"
            " WHERE epic_ref = ? ORDER BY id DESC LIMIT 1",
            (epic_ref,),
        ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot"])
        except (ValueError, TypeError) as exc:
            _logger.warning(
                "epic_ticks row %s: stored snapshot is not JSON (%s);"
                " journaling this tick with no predecessor",
                row["id"],
                type(exc).__name__,
            )
            return None
        if not isinstance(snapshot, dict):
            _logger.warning(
                "epic_ticks row %s: stored snapshot is not an object;"
                " journaling this tick with no predecessor",
                row["id"],
            )
            return None
        return snapshot

    def list_epic_ticks(
        self, epic_ref: str, *, after_id: int = 0, limit: int = 1000
    ) -> list[EpicTick]:
        rows = self._execute(
            "SELECT * FROM epic_ticks WHERE epic_ref = ? AND id > ?"
            " ORDER BY id ASC LIMIT ?",
            (epic_ref, after_id, limit),
        ).fetchall()
        return [self._row_to_epic_tick(r) for r in rows]

    def list_epic_ticks_tail(self, epic_ref: str, *, limit: int = 50) -> list[EpicTick]:
        """The epic's NEWEST ``limit`` ticks, oldest-first within the page.

        A live subscriber opening a stream wants the tail, and reading it with
        ``after_id=0`` meant reading up to a thousand snapshots to keep the
        last fifty — and then setting its cursor to the thousandth row rather
        than the newest, so a long-running epic replayed its own history
        before catching up (C3/#2296).
        """
        if limit < 1:
            raise ValueError("epic tick tail limit must be >= 1")
        rows = self._execute(
            "SELECT * FROM epic_ticks WHERE epic_ref = ? ORDER BY id DESC LIMIT ?",
            (epic_ref, limit),
        ).fetchall()
        return [self._row_to_epic_tick(r) for r in reversed(rows)]

    def count_epic_ticks(self, epic_ref: str) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM epic_ticks WHERE epic_ref = ?",
            (epic_ref,),
        ).fetchone()
        return int(row["n"])

    def first_epic_tick_ts(self, epic_ref: str) -> str | None:
        row = self._execute(
            "SELECT tick_ts FROM epic_ticks WHERE epic_ref = ? ORDER BY id ASC LIMIT 1",
            (epic_ref,),
        ).fetchone()
        return row["tick_ts"] if row else None

    def latest_epic_ticks(self) -> dict[str, EpicTick]:
        """Every epic's newest journaled tick, keyed by epic ref, in ONE query.

        The Epics roster used to call :meth:`latest_epic_tick` once per epic;
        at 70 epics that was 70 queries before any of the other four per-epic
        reads (C3/#2296). An epic with no tick yet is absent from the dict.
        """
        rows = self._execute(
            "SELECT * FROM epic_ticks"
            " WHERE id IN (SELECT MAX(id) FROM epic_ticks GROUP BY epic_ref)"
        ).fetchall()
        return {str(row["epic_ref"]): self._row_to_epic_tick(row) for row in rows}

    def latest_node_pr_facts(
        self,
        *,
        limit: int = DEFAULT_NODE_PR_FACTS_LIMIT,
        after: tuple[str, str] | None = None,
        open_only: bool = True,
        epic_ref: str | None = None,
    ) -> list[NodePrFacts]:
        """Every epic's newest snapshot, flattened to one row per linked PR.

        ONE query, with every filter pushed SQL-side through ``json_each`` over
        the snapshot's ``nodes`` array: no Python-side JSON walk over the whole
        tick journal, and nothing materialized for a node that carries no pull
        request. The page is ordered by ``(epic_ref, item_id)`` and bounded by
        *limit*; *after* is the last ``(epic_ref, item_id)`` of the previous page,
        which is what makes it a cursor rather than an offset nothing pins.

        ``open_only`` (the default) drops the settled statuses — a merged or
        cancelled node's head cannot go stale on anybody. A boundary copy of
        another epic's node carries no ``pr_url`` and so never reaches a row.

        ``epic_ref`` narrows the read to one epic, SQL-side. A surface drawing a
        single epic must be able to ask only for it: a global page can fill
        before reaching that epic, and silence then reads as "nothing stale".

        An unparseable snapshot is skipped by ``json_valid`` rather than raising:
        never repair a ledger row, and never let one bad row take the read down.
        """
        bounded = max(1, min(int(limit), MAX_NODE_PR_FACTS_LIMIT))
        where = [_NODE_PR_FACTS_LINKED]
        params: list[Any] = []
        if epic_ref is not None:
            where.append(_NODE_PR_FACTS_EPIC)
            params.append(epic_ref)
        if open_only:
            where.append(_NODE_PR_FACTS_OPEN)
            params.extend(SETTLED_NODE_STATUSES)
        if after is not None:
            where.append(_NODE_PR_FACTS_AFTER)
            params.extend([after[0], after[0], after[1]])
        params.append(bounded)
        rows = self._execute(
            _NODE_PR_FACTS_SELECT + " AND ".join(where) + _NODE_PR_FACTS_PAGE,
            tuple(params),
        ).fetchall()
        return [
            NodePrFacts(
                epic_ref=str(row["epic_ref"]),
                item_id=str(row["item_id"]),
                observed_at=str(row["tick_ts"]),
                status=_opt_str(row["status"]),
                pr_url=_opt_str(row["pr_url"]),
                head_oid=_opt_str(row["head_oid"]),
                merge_state=_opt_str(row["merge_state"]),
                merge_state_since=_opt_str(row["merge_state_since"]),
                merge_state_at=_opt_str(row["merge_state_at"]),
                pr_pushed_at=_opt_str(row["pr_pushed_at"]),
            )
            for row in rows
            if row["item_id"] is not None
        ]

    def epic_tick_spans(self) -> dict[str, tuple[int, str | None]]:
        """``epic_ref -> (tick_count, first tick_ts)`` for every epic, ONE query.

        The pair is the roster's ``change_count`` and economics window start,
        which cost a COUNT and a seek per epic apiece. The join onto
        ``MIN(id)`` reads the first tick by ledger position — the same row
        :meth:`first_epic_tick_ts` returns — rather than by MIN(tick_ts),
        which would differ if a clock ever stepped backwards.
        """
        rows = self._execute(
            "SELECT t.epic_ref AS epic_ref, c.n AS n, t.tick_ts AS tick_ts"
            " FROM epic_ticks t JOIN ("
            "SELECT epic_ref, COUNT(*) AS n, MIN(id) AS first_id"
            " FROM epic_ticks GROUP BY epic_ref"
            ") c ON c.first_id = t.id"
        ).fetchall()
        return {str(row["epic_ref"]): (int(row["n"]), row["tick_ts"]) for row in rows}

    def list_epic_driver_states(self) -> dict[str, EpicDriverState]:
        """Every epic's heartbeat row, keyed by epic ref, in ONE query."""
        rows = self._execute("SELECT * FROM epic_driver_state").fetchall()
        states = [_row_to_epic_driver_state(row) for row in rows]
        return {state.epic_ref: state for state in states}

    def sum_costs_by_item_windows(
        self, windows: Collection[tuple[str, str]]
    ) -> dict[tuple[str, str], float]:
        """:meth:`sum_costs_by_item` with a PER-PAIR window, in ONE query.

        Each epic's drain window starts at its own first tick, so summing a
        whole roster's nodes meant one aggregate per epic. ``windows`` is a
        collection of ``(item_id, since)`` pairs, ``since`` being that pair's
        inclusive lower bound on ``finished_at``; the same exclusions apply
        (pending rows and readiness probes are out). The answer is keyed by
        the SAME pair, not by item id alone: two epics can share a DAG node
        and still open their windows at different first ticks, and that node
        then has two different drains — one per window (#2375 review). Pairs
        with no matching runs are absent from the returned dict.
        """
        pairs = sorted(set(windows))
        if not pairs:
            return {}
        values = ",".join("(?,?)" for _ in pairs)
        # Bound in SQL text order: the VALUES rows sit in the FROM clause,
        # ahead of the run-kind predicate in the WHERE clause.
        params: list[Any] = []
        for item_id, since in pairs:
            params.extend((item_id, since))
        params.append(READINESS_PROBE_RUN_KIND)
        sql = (
            "SELECT s.item_id AS item_id, s.since AS since,"  # nosec B608 — placeholders are ? params, values are bound
            " SUM(r.estimated_cost_usd) AS total"
            " FROM runs r JOIN (SELECT column1 AS item_id, column2 AS since"
            f" FROM (VALUES {values})) s ON r.item_id = s.item_id"
            " WHERE r.run_kind != ?"
            " AND r.finished_at IS NOT NULL AND r.finished_at >= s.since"
            " GROUP BY s.item_id, s.since"
        )
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return {
            (str(row["item_id"]), str(row["since"])): float(row["total"] or 0.0)
            for row in rows
        }

    def list_epic_refs(self) -> list[str]:
        """Every epic ref any of the three epic tables knows about, sorted.

        The attempt log is the third arm, not an optional one: a tick turned
        away before ``begin_epic_tick`` — failed preflight, locked out — writes
        an ``epic_tick_log`` row and touches neither the heartbeat nor the
        change journal. An epic whose attempts were ALL refused that way was
        therefore unreachable from every surface that enumerates epics through
        here: the roster omitted the row, and
        ``/api/epics/<ref>/tick-strip`` 404'd on it via ``known_epic_refs`` —
        so the one table that recorded those attempts had no reader at all
        (#2304 review round 4).

        Such an epic reads like a :meth:`register_epic` one: known, with no
        heartbeat and no snapshot, which is a state every caller already
        handles (``health`` skips a ``None`` driver state, the roster serves
        empty counts).

        ``empty_graph`` attempts are the arm's ONE exclusion, because that
        outcome is the one reading that argues the ref is not an epic at all
        (:data:`EPIC_TICK_OUTCOME_EMPTY_GRAPH`). Admitting them made a mistyped
        ``kew epic advance`` name a permanent roster member — and this list is
        the roster ``kew epic run --all`` drives, which re-raised that epic's
        empty graph as a STALLED classification and a non-zero exit on every
        subsequent run, with no way to take it back off (nothing deletes an
        epic's attempt rows) (#2438 review). A ref with any OTHER attempt —
        including an open row, which is a tick in flight — stays admitted: that
        is a real epic being refused, which is the gap this arm exists to close.
        """
        rows = self._execute(
            "SELECT epic_ref FROM epic_driver_state"
            " UNION SELECT epic_ref FROM epic_ticks"
            " UNION SELECT epic_ref FROM epic_tick_log WHERE outcome IS NOT ?"
            " ORDER BY 1",
            (EPIC_TICK_OUTCOME_EMPTY_GRAPH,),
        ).fetchall()
        return [r["epic_ref"] for r in rows]

    def epic_trackers(self) -> dict[str, str | None]:
        """Classify each known epic's tracker, reading the durable ``tracker``
        column FIRST and falling back to item-id-shape inference (#927/N3,
        #882/B1).

        The driver records the active tracker on ``epic_driver_state.tracker``
        at each tick (:meth:`record_epic_tracker`), which is ground truth — a
        driver only builds a graph from, and drives, epics on its active
        tracker. Reading that explicit value first is what fixes the third leak
        of #877: four complete Linear epics carry numeric GitHub-twin item_ids
        from a pre-SFD recording era, so inference read them ``"github"`` and
        ``epic run --all`` false-STALLED them. An explicit ``"linear"`` (recorded
        or backfilled in ``_apply_v33``) now overrides the misleading shape.

        Inference is the fallback for rows with no recorded tracker (pre-v33 or
        never-ticked). A GitHub node records a numeric ``item_id`` (the issue
        number); a Linear node records an opaque key like ``SFD-12`` (see
        ``RunRecord.item_id``). So a numeric id => ``"github"``, anything else
        => ``"linear"``. An epic with no recorded items yet (freshly planned,
        never driven) resolves to ``None`` — ``epic run --all`` then drives it
        under the active tracker rather than guessing.

        The final UNION arm backfills epics whose rows predate ``item_id`` being
        recorded on the epic tables (v25, #877/R2): a pre-v25 reservation carries
        only its ``run_id``, so its own ``item_id`` column is NULL, but the
        durable id still lives on the run it points at (``runs.item_id``,
        recorded since v24). Joining reservation ``run_id`` back to ``runs``
        recovers those ids so a complete Linear-era epic resolves ``"linear"``
        instead of ``None`` (which would drive — and misclassify — it under the
        active GitHub tracker).

        Returns one entry per :meth:`list_epic_refs` ref so the ``--all`` loop
        can scope itself to the active project's tracker, skipping epics that
        belong to a different one.
        """
        recorded = {
            r["epic_ref"]: r["tracker"]
            for r in self._execute(
                "SELECT epic_ref, tracker FROM epic_driver_state"
                " WHERE tracker IS NOT NULL"
            ).fetchall()
        }
        rows = self._execute(
            "SELECT epic_ref, item_id FROM epic_reservations"
            " UNION SELECT epic_ref, item_id FROM epic_holds"
            " UNION SELECT epic_ref, item_id FROM epic_retries"
            " UNION SELECT er.epic_ref, r.item_id FROM epic_reservations er"
            " JOIN runs r ON r.run_id = er.run_id"
        ).fetchall()
        items_by_epic: dict[str, set[str]] = {}
        for r in rows:
            item_id = r["item_id"]
            if item_id:
                items_by_epic.setdefault(r["epic_ref"], set()).add(item_id)
        return {
            ref: recorded.get(ref) or _infer_epic_tracker(items_by_epic.get(ref, set()))
            for ref in self.list_epic_refs()
        }

    def record_epic_tracker(self, epic_ref: str, tracker: str) -> None:
        """Durably record the tracker an epic is driven under (#927/N3).

        Called each tick with the active tracker (``config.tracker.type``),
        which is ground truth: a driver only builds a graph from — and drives —
        epics on its own active tracker. :meth:`epic_trackers` reads this column
        FIRST, so a Linear epic whose runs carry numeric GitHub-twin item ids is
        no longer misclassified ``"github"`` by shape inference. Upserts the
        driver-state row (never clobbering an in-flight heartbeat's tick_count),
        so recording does not depend on :meth:`begin_epic_tick` ordering."""

        def _do_record() -> None:
            self._execute(
                "INSERT INTO epic_driver_state (epic_ref, tracker, tick_count)"
                " VALUES (?, ?, 0)"
                " ON CONFLICT(epic_ref) DO UPDATE SET tracker=excluded.tracker",
                (epic_ref, tracker),
            )
            self._conn.commit()

        self._with_busy_retry("record_epic_tracker", _do_record)

    def register_epic(self, epic_ref: str) -> None:
        """Seed a driver-state row (tick_count=0) so a planned-but-unstarted
        epic is visible to `list_epic_refs()` — hence to `epic run --all` —
        before its first tick (#740). No-op if the epic already has state, so
        it never clobbers a live heartbeat."""

        def _do_register() -> None:
            self._execute(
                "INSERT INTO epic_driver_state (epic_ref, tick_count)"
                " VALUES (?, 0) ON CONFLICT(epic_ref) DO NOTHING",
                (epic_ref,),
            )
            self._conn.commit()

        self._with_busy_retry("register_epic", _do_register)

    def begin_epic_tick(self, epic_ref: str, started_at: str) -> None:
        def _do_begin() -> None:
            self._execute(
                "INSERT INTO epic_driver_state"
                " (epic_ref, last_tick_started_at, tick_count)"
                " VALUES (?, ?, 1)"
                " ON CONFLICT(epic_ref) DO UPDATE SET"
                " last_tick_started_at=excluded.last_tick_started_at,"
                " tick_count=tick_count+1",
                (epic_ref, started_at),
            )
            self._conn.commit()

        self._with_busy_retry("begin_epic_tick", _do_begin)

    def _surviving_recorded_cadence(
        self, epic_ref: str, *, tick_ts: str, duration_s: float
    ) -> float | None:
        """The recorded ``interval_s`` a cadence-less drive must leave in place.

        A one-shot ``kew epic advance`` knows one thing about the poller whose
        cadence is on the row: how long the epic was quiet before this drive
        started (``tick_ts`` minus this drive's own ``duration_s``, against the
        previous drive's ``last_tick_ts`` — ``begin_epic_tick`` has already
        overwritten ``last_tick_started_at``, so that column cannot be used).
        If that silence is a lapse by the shared rule
        (:func:`kew.driver_cadence.cadence_lapsed`), nothing is polling at the
        recorded cadence and it is retired (None); otherwise a poller
        consistent with it may well be asleep between ticks, and the recorded
        value survives (#2297 review round 2).

        The bound is the reader's own ``gone`` threshold, which makes the rule
        exact: a cadence is retired precisely when keeping it would have made
        the next reading ``gone``. Evidence that cannot settle it — no previous
        tick, an unreadable stamp — keeps the value, and the next drive that
        does observe a lapse retires it.
        """
        row = self._execute(
            "SELECT interval_s, last_tick_ts FROM epic_driver_state WHERE epic_ref=?",
            (epic_ref,),
        ).fetchone()
        if row is None or row["interval_s"] is None:
            return None
        recorded = float(row["interval_s"])
        silence_s = _epic_drive_silence_s(row["last_tick_ts"], tick_ts, duration_s)
        return None if cadence_lapsed(silence_s, recorded) else recorded

    def finish_epic_tick(
        self,
        epic_ref: str,
        *,
        tick_ts: str,
        duration_s: float,
        interval_s: float | None,
        signal: str,
        tick_id: int | None,
        expected_interval_s: float | None = None,
    ) -> None:
        """Commit phase 2 of the tick heartbeat.

        ``interval_s`` is the cadence of the drive that JUST ticked (``epic run
        --interval``), written as given: a poller is authoritative about its own
        cadence. A drive that passes None has no cadence of its own (a one-shot
        ``kew epic advance``), and that is NOT a licence to blank the column —
        it hands the decision to :meth:`_surviving_recorded_cadence`, which
        retires a recorded cadence only once the gap this drive just observed
        proves nothing kept up with it.

        Both halves of that are load-bearing. Carrying the old value forward
        unconditionally made a retired poller's cadence permanently unclearable,
        so an epic moved from ``epic run --interval 60`` to a 5-minute cron read
        ``gone`` two minutes after a SUCCESSFUL advance, forever (#2297 review).
        Clearing it unconditionally was the mirror-image bug: a one-shot advance
        landing in a LIVE ``epic run --interval 1800`` poller's inter-tick gap
        blanked its cadence, and the console read ``gone`` until the poller's
        next tick. The lock does not prevent that — it is held for the duration
        of a tick, not between ticks, so a one-shot in the gap acquires it and
        reaches this write (#2297 review round 2).

        ``expected_interval_s`` is the operator's configured expectation, and it
        lands in its own v62 column — refreshed by every drive that supplies one
        and never folded into ``interval_s``, so changing
        ``[epic] driver_expected_interval_seconds`` takes effect at the next
        drive and liveness can report a guess AS a guess (#2297).
        """

        def _do_finish() -> None:
            recorded = (
                self._surviving_recorded_cadence(
                    epic_ref, tick_ts=tick_ts, duration_s=duration_s
                )
                if interval_s is None
                else interval_s
            )
            self._execute(
                "UPDATE epic_driver_state SET"
                " last_tick_ts=?,"
                " last_tick_duration_s=?,"
                " interval_s=?,"
                " expected_interval_s=COALESCE(?, expected_interval_s),"
                " last_signal=?,"
                " last_tick_id=?"
                " WHERE epic_ref=?",
                (
                    tick_ts,
                    duration_s,
                    recorded,
                    expected_interval_s,
                    signal,
                    tick_id,
                    epic_ref,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("finish_epic_tick", _do_finish)

    def record_epic_driver_lock(
        self,
        epic_ref: str,
        *,
        pid: int | None,
        pid_alive: bool | None,
        heartbeat_at: str | None,
        observed_at: str,
    ) -> None:
        """Snapshot one epic driver LOCK's evidence for readers who cannot read it.

        The lock file stays the source of truth (and the only place a steal is
        arbitrated); this is a dated copy of what the sweep saw, so a serve-side
        reading can threshold the holder's own mid-tick heartbeat and see a dead
        holder pid the way ``kew epic status`` does (#2297 review). An all-None
        observation is recorded as such: "we looked and the lock was free" is a
        fact, and between ticks it is the normal one.

        Never read to decide ownership. Upserted, because a lock can be observed
        before this epic's first ``begin_epic_tick`` row exists.
        """

        def _do_record() -> None:
            self._execute(
                "INSERT INTO epic_driver_state"
                " (epic_ref, tick_count, lock_pid, lock_pid_alive,"
                " lock_heartbeat_at, lock_observed_at)"
                " VALUES (?, 0, ?, ?, ?, ?)"
                " ON CONFLICT(epic_ref) DO UPDATE SET"
                " lock_pid=excluded.lock_pid,"
                " lock_pid_alive=excluded.lock_pid_alive,"
                " lock_heartbeat_at=excluded.lock_heartbeat_at,"
                " lock_observed_at=excluded.lock_observed_at",
                (
                    epic_ref,
                    pid,
                    None if pid_alive is None else int(pid_alive),
                    heartbeat_at,
                    observed_at,
                ),
            )
            self._conn.commit()

        self._with_busy_retry("record_epic_driver_lock", _do_record)

    def get_epic_driver_state(self, epic_ref: str) -> EpicDriverState | None:
        row = self._execute(
            "SELECT * FROM epic_driver_state WHERE epic_ref = ?", (epic_ref,)
        ).fetchone()
        return _row_to_epic_driver_state(row) if row is not None else None


_EXPORT_FIELDS = [
    "run_id",
    "issue_num",
    "branch",
    "status",
    "exit_code",
    "started_at",
    "finished_at",
    "duration_seconds",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "estimated_cost_usd",
    "error_category",
    "attempt_number",
    "session_id",
    "log_file",
    "pr_url",
    "runner_name",
    "model",
    "title",
    "complexity",
    "pgid",
    "attempt_pgid",
    "owner_pid",
    "attempt_start_time",
    "owner_start_time",
    "owner_start_token",
    "attempt_start_token",
    "owner_boot_id",
    "owner_ppid",
    "owner_command",
    "container_runtime",
    "cleanup_runtime",
    "attempt_owner_verified",
    "termination_members",
    "actor_type",
    "actor_id",
    "repo",
    "endpoint",
    "policy_hash",
    "reported_model",
    "pr_state",
    "pr_merged_at",
    "review_cycles",
    "diff_additions",
    "diff_deletions",
    "diff_files",
    "prefix_key",
    "provider",
    "container_name",
    "container_id",
    "container_cidfile",
    "item_id",
    "run_kind",
    "steps_to_first_edit",
    "tokens_to_first_edit",
    "map_tokens",
    "lesson_corpus_tokens",
    "planned_cost_usd",
    "transport_mode",
    "transport_endpoint",
    "transport_route",
    "transport_managed",
    "transport_expected_version",
    "transport_observed_version",
    "transport_config_sha256",
    "transport_preflight_verdict",
    "resolved_model_source",
    "selected_tier",
    "requested_service_tier",
    "reported_service_tier",
    "classifier_overhead_model",
    "classifier_overhead_input_tokens",
    "classifier_overhead_output_tokens",
    "classifier_overhead_cost_usd",
    "switchyard_proxy_observation",
    "accounting_status",
    "pre_admission_refusal",
    "self_check",
    "run_host",
    "run_host_instance",
    "prompt_profile_version_id",
]


def _record_to_dict(record: RunRecord) -> dict[str, Any]:
    """Convert a RunRecord to an export-friendly dict with ISO datetime strings."""
    d = asdict(record)
    d["started_at"] = record.started_at.isoformat()
    d["finished_at"] = record.finished_at.isoformat() if record.finished_at else None
    d["log_file"] = str(record.log_file) if record.log_file else None
    return d


def export_runs_json(records: list[RunRecord]) -> str:
    """Export run records to a JSON string."""
    data = [_record_to_dict(r) for r in records]
    return json.dumps(data, indent=2)


def export_runs_csv(records: list[RunRecord]) -> str:
    """Export run records to a CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for record in records:
        writer.writerow(_record_to_dict(record))
    return output.getvalue()
