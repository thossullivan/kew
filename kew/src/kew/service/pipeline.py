"""Item pipeline projection (FUI-11) — the stages one work item moved through.

The DWork swimlane draws a grid, but the payload here is deliberately NOT one:
it is a list of lanes, each a list of stages, so the surface renders whatever
it is given. kew composes exactly ONE lane — the delivery repository's
base branch — over four stages:

* **implement** — the newest implementation run (review, CI-fix and resolve
  runs are runs *about* an already-open pull request, never the attempt that
  produced it, so they are filtered out here);
* **publish** — the artifact dimension, refined by the host's
  ``publication_output_scan`` verdict for the pushed objects and by the
  standing ``credential_already_published`` fact, which a later clean scan of
  a different head does not undo;
* **review** — the governed review rounds recorded for the current cycle, with
  the attested ``adversarial_review_verdict`` as the cell's evidence;
* **merge** — the human decision on the artifact, which is the one stage that
  becomes ``needs_you``.

kew has no Reproduce stage and no backport lane; nothing here emits them.

Two rules this module holds to:

* **Nothing is invented.** A stage with no recorded fact behind it carries no
  evidence, and a stage whose fact could not be read is ``unknown`` with its
  reason NAMED in ``unavailable`` — never quietly ``pending``, which is a
  different world ("we looked, it has not happened") from "we could not look".
* **Progress, not attestation.** ``state`` says how far the stage got. Whether
  the facts behind it are attested by the host and chain-verified is the
  evidence chain's question (FUI-12), and answering it loosely here is exactly
  how a green check ends up over an unverified chain.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.publication_scan import ALREADY_PUBLISHED_EVENT, OUTPUT_SCAN_EVENT
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_store import PR_SUBJECT_RUN_KINDS, READINESS_PROBE_RUN_KIND
from kew.service.dashboard import ready_queue_item_ids
from kew.service.merge_verify_reading import VerifyUnreadable, item_merge_verify
from kew.service.models import (
    ItemPipelineDTO,
    MergeVerifyDTO,
    PipelineEvidenceDTO,
    PipelineLaneDTO,
    PipelineStageDTO,
    WorkViewDTO,
)
from kew.service.work import (
    cached_pull_request,
    collect_work_facts,
    compose_work_view,
    pending_run_max_age_s,
    review_in_flight,
)

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kew.config import DispatchConfig

#: The stages kew composes, in the order the swimlane draws them.
PIPELINE_STAGES: tuple[str, ...] = ("implement", "publish", "review", "merge")

#: Named entries in :attr:`ItemPipelineDTO.unavailable`.
UNREADABLE_ROUNDS = "pipeline:review_rounds"
UNREADABLE_AUDIT = "pipeline:audit"
UNREADABLE_JUDGMENT = "pipeline:judgment"
UNREADABLE_MERGE_VERIFY = "pipeline:merge_verify"

#: Runs that are not the implementation attempt: a probe pays money but
#: carries no tracker work, and every PR-subject kind runs *on* an artifact
#: some earlier implementation run already produced.
_NOT_IMPLEMENTATION = frozenset({READINESS_PROBE_RUN_KIND, *PR_SUBJECT_RUN_KINDS})

#: Execution dimension -> implement-stage state. ``terminal`` is the work
#: view's own catch-all, so an unlisted state reads as ``failed`` (loud)
#: rather than as ``done`` (quiet).
_IMPLEMENT_STATE: dict[str, str] = {
    "in_flight": "working",
    "completed": "done",
}
#: ``RunStatus`` -> the cell's second line. ``awaiting_publication`` is the
#: compatibility encoding of a run that DID succeed (only publication is
#: outstanding), which the publish cell answers for.
_IMPLEMENT_DETAIL: dict[str, str] = {
    "pending": "in flight",
    "awaiting_publication": "success",
    "success": "success",
}

#: Recorded round disposition -> (state, the cell's second line). An unlisted
#: verdict is a degenerate one the driver attests to short-circuit the stage
#: (DEGRADED, MERGED_UNREVIEWED, PRE_POLICY): the round is over, so ``done``,
#: worded from the verdict itself rather than from a guess about it.
#: ``READY_WITH_RESIDUALS`` is the stop rule's acceptance of a low/medium-only
#: NEEDS_CHANGES round (#2151), read from the round's ``effective_verdict``.
#: A not-ready verdict is ``not_ready``, the console's amber: the round is over
#: and went against the change, which is neither work in flight (the cyan
#: ``working`` it used to serve) nor a person's move.
_REVIEW_VERDICT: dict[str, tuple[str, str]] = {
    "READY": ("done", "ready"),
    "READY_WITH_RESIDUALS": ("done", "ready with residuals"),
    "NEEDS_CHANGES": ("not_ready", "needs changes"),
    "NEEDS_FULL": ("not_ready", "needs a full round"),
    "UNPARSEABLE": ("needs_you", "reviewer output unreadable"),
}

#: Recorded scan verdict -> (state, label, detail) for the publish cell. The
#: two the host records are the only two rendered: an unparseable payload (which
#: ``list_audit_events`` coerces to ``None``) and a verdict from some newer host
#: are facts this projection could not read, and reading either as "clean"
#: paints a green cell over a scan nobody here understood — where the host's own
#: reader (``recorded_scan_verdict``) answers ``missing`` and refuses the push.
_SCAN_CELL: dict[str, tuple[str, str, str]] = {
    "found": ("needs_you", "scan found", "a credential reached the pushed objects"),
    # An empty detail is the scanned head, short form — filled in by the cell.
    "clean": ("done", "scan clean", ""),
}

#: The one actor whose scan records the push gate reads (``recorded_scan_verdict``).
_SCAN_ACTOR = ("policy", "host-publication")

#: Judgment state -> (state, label, detail) for the merge cell.
_MERGE_CELL: dict[str, tuple[str, str, str]] = {
    "awaiting_human_decision": ("needs_you", "needs you", "merge decision"),
    # kew's own review cycle still holds the artifact (#2279/D8). The merge is
    # ahead of us, not owed by anybody: the swimlane used to print "round 1 ·
    # reviewing" beside "needs you · merge decision", which is one pull
    # request waiting on two different people at once.
    "under_review": ("pending", "after review", "the review verdict decides"),
    # Between two rounds of that cycle (#2328) — a NEEDS_CHANGES or NEEDS_FULL
    # verdict landed and kew owes the next round. The merge is the same
    # distance away as it is mid-round, so it reads the same; without an entry
    # here the cell fell through to "not started", which is the one thing the
    # merge is not.
    "between_review_rounds": ("pending", "after review", "the review verdict decides"),
    "changes_requested": ("needs_you", "needs you", "changes requested"),
    "accepted": ("done", "merged", ""),
    "declined": ("failed", "declined", "closed without merging"),
}

#: Outcomes that mean the work is over, so a merge that never applied is
#: ``not_required`` rather than still ahead of us.
_SETTLED_OUTCOMES = frozenset({"delivered", "abandoned", "failed"})


def _short(ref: str | None) -> str:
    """The display form of a run id or sha: the leading 8 characters."""
    return (ref or "")[:8]


def _reader(store: Any, name: str) -> Any:
    """A store method if this store has one, else None.

    Same degradation rule ``kew.service.work`` uses: a store that predates a
    table (or a duck-typed double that never had it) must read as "could not
    tell", which this module then NAMES, rather than as a clean answer.
    """
    reader = getattr(store, name, None)
    return reader if callable(reader) else None


@dataclass(frozen=True)
class _Facts:
    """Everything the four stages are composed from, read in one snapshot."""

    view: WorkViewDTO
    scan: Any | None
    #: The scanned head's verdict by the push gate's rule, or ``""`` when the
    #: recorded scan could not be read (named in :attr:`unavailable`).
    scan_verdict: str
    #: The newest ``credential_already_published`` record, when the host found
    #: a credential on commits the agent had already pushed. Standing, not
    #: superseded: the value is on the remote branch until a person rotates it.
    already_published: Any | None
    verdict: Any | None
    rounds: tuple[Any, ...]
    reviewing: bool
    #: The latest merge verify of the item's pull request (#3140), or None.
    merge_verify: MergeVerifyDTO | None = None
    #: Every fact that could not be read, named. Owned by :func:`_collect`:
    #: a stage renders what is here, it never appends to it.
    unavailable: tuple[str, ...] = ()


def _sorted_runs(store: Any, item_id: str) -> list[Any]:
    """This item's runs, newest attempt first (``kew.service.work``'s order)."""
    return sorted(
        store.list_by_item(item_id),
        key=lambda rec: (
            str(getattr(rec, "started_at", "") or ""),
            int(getattr(rec, "attempt_number", 1) or 1),
        ),
        reverse=True,
    )


def _implementation_runs(runs: Sequence[Any]) -> tuple[Any, ...]:
    """The runs that produced the artifact, not the ones that ran on it.

    Scoping matters here in a way it does not for the aggregate work view: a
    live reviewer is the newest run on the item, and a view composed with it
    as "latest" reads the artifact off a row that never had a pull request —
    so publish and merge would collapse to "absent" for exactly the state the
    swimlane exists to draw (published, reviewing, awaiting a decision).
    """
    return tuple(
        rec
        for rec in runs
        if str(getattr(rec, "run_kind", "dispatch") or "") not in _NOT_IMPLEMENTATION
    )


def _pull_request_fact(runs: Sequence[Any]) -> Any | None:
    """The newest run's persisted forge reading, marked stale by ``work``."""
    for rec in runs:
        if getattr(rec, "pr_url", None):
            return cached_pull_request(rec)
    return None


def _review_rounds(store: Any, item_id: str, unavailable: list[str]) -> tuple[Any, ...]:
    """The current cycle's recorded rounds, oldest first; () when unreadable."""
    reader = _reader(store, "review_rounds_in_cycle")
    if reader is None:
        unavailable.append(UNREADABLE_ROUNDS)
        return ()
    try:
        return tuple(reader(item_id))
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean ()
        _logger.warning("review rounds unreadable for item %s", item_id, exc_info=True)
        unavailable.append(UNREADABLE_ROUNDS)
        return ()


def _legacy_run_ids(runs: Sequence[Any]) -> tuple[str, ...]:
    """Every run id on this item, newest first — the pre-item_id join keys.

    WHICH run holds a given fact differs per event: the host records the
    publication scan under the run that pushed, the driver records the review
    verdict under the reviewer's own run. Trying only the newest row's id
    loses whichever of the two did not happen to run last — a live reviewer
    (or a ci_fix, or a resolve) on top of a legacy item would hide a recorded
    ``found`` verdict behind a green "no scan recorded".
    """
    return tuple(
        dict.fromkeys(
            run_id for rec in runs if (run_id := str(getattr(rec, "run_id", "") or ""))
        )
    )


def _latest_event(
    store: Any,
    event: str,
    *,
    item_id: str,
    run_ids: Sequence[str],
    unavailable: list[str],
) -> Any | None:
    """The newest *event* for this item, joined by item id then by its runs.

    Pre-item_id rows carry the key on ``run_id`` alone, so the run walk is
    what keeps a legacy GitHub item's scan verdict visible. It stops at the
    first run that recorded the event, and a modern item never enters it.
    """
    reader = _reader(store, "list_audit_events")
    if reader is None:
        unavailable.append(UNREADABLE_AUDIT)
        return None
    try:
        rows = reader(limit=1, event=event, item_id=item_id)
        for run_id in run_ids:
            if rows:
                break
            rows = reader(limit=1, event=event, run_id=run_id)
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean None
        _logger.warning(
            "audit event %s unreadable for item %s", event, item_id, exc_info=True
        )
        unavailable.append(UNREADABLE_AUDIT)
        return None
    return rows[0] if rows else None


def _payload(row: Any) -> Mapping[str, Any]:
    """An audit row's payload, or an empty mapping when it has none.

    ``list_audit_events`` coerces an unparseable payload to ``None``: that is
    a recorded row nobody can read, never an empty one.
    """
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _row_verdict(scan: Any) -> str:
    """One row's recorded verdict, or ``""`` when the row cannot be rendered."""
    verdict = str(_payload(scan).get("verdict") or "")
    return verdict if verdict in _SCAN_CELL else ""


def _scan_verdict(store: Any, scan: Any) -> str:
    """The verdict for the scanned head, by the push gate's own rule.

    ``publication_scan.recorded_scan_verdict`` — what the gate reads before it
    pushes — answers ``found`` for a head any scan found a credential in,
    however many clean records of that head came after. Rendering the newest
    row alone would put a green cell over a push the host refuses, so a clean
    row is confirmed against every verdict recorded for the same run and head.
    """
    verdict = _row_verdict(scan)
    reader = _reader(store, "head_audit_verdicts")
    run_id = str(getattr(scan, "run_id", "") or "")
    head = str(_payload(scan).get("head_sha") or "")
    if verdict != "clean" or reader is None or not run_id or not head:
        return verdict
    try:
        recorded = reader(
            event=OUTPUT_SCAN_EVENT,
            run_id=run_id,
            actor=_SCAN_ACTOR,
            head_sha=head,
        )
    except Exception:  # noqa: BLE001 — named as unavailable by the caller
        _logger.warning("scan verdicts unreadable for run %s", run_id, exc_info=True)
        return ""
    return "found" if "found" in recorded else verdict


def _scan_facts(
    store: Any, item_id: str, *, run_ids: Sequence[str], unavailable: list[str]
) -> tuple[Any | None, str, Any | None]:
    """The newest recorded scan, its verdict, and any already-published finding.

    A scan whose verdict this projection cannot read is NAMED here, so the
    publish cell degrades to ``unknown`` on the same rule as an unreadable
    table rather than falling through to "clean".
    """
    scan = _latest_event(
        store,
        OUTPUT_SCAN_EVENT,
        item_id=item_id,
        run_ids=run_ids,
        unavailable=unavailable,
    )
    verdict = "" if scan is None else _scan_verdict(store, scan)
    if scan is not None and not verdict:
        unavailable.append(UNREADABLE_AUDIT)
    already_published = _latest_event(
        store,
        ALREADY_PUBLISHED_EVENT,
        item_id=item_id,
        run_ids=run_ids,
        unavailable=unavailable,
    )
    return scan, verdict, already_published


def _merge_verify(
    store: Any,
    item_id: str,
    *,
    epic_ref: str | None,
    rounds: Sequence[Any],
    events: Sequence[Any],
    unavailable: list[str],
) -> MergeVerifyDTO | None:
    """The latest verify linked to this item's pull-request heads, or None.

    The head set and the join are :mod:`kew.service.merge_verify_reading`'s;
    this passes the rounds and audit rows already read in this snapshot. When
    the rounds or the head-naming audit rows did not answer, "none linked" is
    a claim the head set cannot back, so it is named rather than served as a
    clean ``None``.
    """
    try:
        verify = item_merge_verify(
            store,
            item_id,
            epic_ref=epic_ref,
            rounds=rounds,
            events=[row for row in events if row is not None],
        )
    except VerifyUnreadable:
        _logger.warning("merge verify unreadable for item %s", item_id, exc_info=True)
        unavailable.append(UNREADABLE_MERGE_VERIFY)
        return None
    partial = UNREADABLE_ROUNDS in unavailable or UNREADABLE_AUDIT in unavailable
    if verify is None and partial:
        unavailable.append(UNREADABLE_MERGE_VERIFY)
    return verify


def _collect(
    store: Any, item_id: str, *, config: DispatchConfig | None, now: datetime
) -> _Facts | None:
    """Read every fact the lane needs inside ONE held snapshot.

    ``None`` means this item has no runs at all: there is no pipeline to
    project, which the route answers as 404 rather than as four empty cells.
    """
    with store.read_snapshot():
        runs = _sorted_runs(store, item_id)
        if not runs:
            return None
        unavailable: list[str] = []
        # The in-flight bound this host's agent budget implies. Read here and
        # passed to BOTH readings below, because the review cell and the merge
        # cell are the two halves of #2279/D8: one predicate, and a bound that
        # differed between them would be the same disagreement with an extra
        # step.
        max_age_s = pending_run_max_age_s(config)
        facts = collect_work_facts(
            store,
            item_id,
            pull_request=_pull_request_fact(runs),
            delivery_repo=(config.project.repo or None) if config else None,
            now=now,
        )
        view = compose_work_view(
            replace(
                facts,
                pending_max_age_s=max_age_s,
                runs=_implementation_runs(facts.runs),
                # The narrowing above is for the implement and publish cells
                # only. The judgment is about the artifact, and the run it has
                # to see — a round reading that artifact — is one of the rows
                # just dropped, so the whole list travels with the facts
                # (``WorkFacts.runs_on_item``). Without it the merge cell read
                # "needs you · merge decision" beside "round 1 · reviewing"
                # (#2279/D8), which is one pull request owed to two people.
                all_runs=facts.runs,
            ),
            composed_at=now,
        )
        if view.judgment.state == "unknown":
            unavailable.append(UNREADABLE_JUDGMENT)
        run_ids = _legacy_run_ids(runs)
        scan, scan_verdict, already_published = _scan_facts(
            store, item_id, run_ids=run_ids, unavailable=unavailable
        )
        verdict = _latest_event(
            store,
            REVIEW_VERDICT_EVENT,
            item_id=item_id,
            run_ids=run_ids,
            unavailable=unavailable,
        )
        rounds = _review_rounds(store, item_id, unavailable)
        merge_verify = _merge_verify(
            store,
            item_id,
            epic_ref=view.identity.epic_ref,
            rounds=rounds,
            events=(verdict, scan, already_published),
            unavailable=unavailable,
        )
        return _Facts(
            view=view,
            scan=scan,
            scan_verdict=scan_verdict,
            already_published=already_published,
            verdict=verdict,
            rounds=rounds,
            reviewing=review_in_flight(runs, now=now, max_age_s=max_age_s),
            merge_verify=merge_verify,
            unavailable=tuple(dict.fromkeys(unavailable)),
        )


def _implement_stage(facts: _Facts) -> PipelineStageDTO:
    execution = facts.view.execution
    run_id = facts.view.identity.run_id or ""
    if execution.state == "not_started" or not run_id:
        return PipelineStageDTO(stage="implement", state="pending", label="not started")
    status = execution.run_status or ""
    return PipelineStageDTO(
        stage="implement",
        state=_IMPLEMENT_STATE.get(execution.state, "failed"),
        label=f"run {_short(run_id)}",
        detail=_IMPLEMENT_DETAIL.get(status, status.replace("_", " ")),
        evidence=PipelineEvidenceDTO(kind="run", ref=run_id),
    )


def _scan_evidence(head: str, pr_url: str | None) -> PipelineEvidenceDTO | None:
    """What to look at for a recorded verdict: the scanned head, else the PR."""
    if head:
        return PipelineEvidenceDTO(kind="output_scan", ref=head)
    return PipelineEvidenceDTO(kind="pull_request", ref=pr_url) if pr_url else None


def _scan_cell(facts: _Facts, pr_url: str | None) -> PipelineStageDTO:
    """The published cell, worded from the host's own scan of the push.

    Only the verdicts in :data:`_SCAN_CELL` are rendered; a scan this
    projection cannot read is ``unknown`` (``_collect`` named it), never a
    quiet "scan clean".
    """
    head = str(_payload(facts.scan).get("head_sha") or "")
    cell = _SCAN_CELL.get(facts.scan_verdict)
    if cell is None:
        return _publish_unknown()
    state, label, detail = cell
    return PipelineStageDTO(
        stage="publish",
        state=state,
        label=label,
        detail=detail or _short(head),
        evidence=_scan_evidence(head, pr_url),
    )


def _publish_unknown() -> PipelineStageDTO:
    """Published, but the fact that would word the cell could not be read."""
    return PipelineStageDTO(
        stage="publish",
        state="unknown",
        label="unknown",
        detail="the scan verdict could not be read",
    )


def _published_cell(facts: _Facts, pr_url: str | None) -> PipelineStageDTO:
    """The cell for an artifact that IS published, worded from the audit."""
    if UNREADABLE_AUDIT in facts.unavailable:
        # The audit table (or the row) did not answer, so "no scan recorded"
        # and "scan clean" are both claims this projection cannot make.
        return _publish_unknown()
    if facts.already_published is not None:
        # A credential reached the remote before the host could refuse the
        # push. That is a standing fact about the branch, so a later clean
        # scan of a different head does not clear it — only a person does.
        head = str(_payload(facts.already_published).get("head_sha") or "")
        return PipelineStageDTO(
            stage="publish",
            state="needs_you",
            label="scan found",
            detail="a credential reached the remote branch",
            evidence=_scan_evidence(head, pr_url),
        )
    if facts.scan is None:
        # Published, but nothing scanned it: an agent-pushed or hand-made
        # branch. The stage is over, but it is not a success: ``done`` drew
        # the green dot beside "no scan recorded". It is ``pending``, the scan
        # it still lacks, and ``passed`` says the PR is up all the same, which
        # the evidence ledger reads as a ``missing`` scan.
        return PipelineStageDTO(
            stage="publish",
            state="pending",
            passed=True,
            label="published",
            detail="no scan recorded",
            evidence=(
                PipelineEvidenceDTO(kind="pull_request", ref=pr_url) if pr_url else None
            ),
        )
    return _scan_cell(facts, pr_url)


def _publish_stage(facts: _Facts) -> PipelineStageDTO:
    artifact = facts.view.artifact
    if artifact.publication == "pending":
        return PipelineStageDTO(
            stage="publish",
            state="working",
            label="publishing",
            detail=artifact.branch or "",
        )
    if artifact.publication != "published":
        return PipelineStageDTO(
            stage="publish", state="not_required", label="not required"
        )
    return _published_cell(facts, artifact.pr_url)


def _verdict_evidence(facts: _Facts, round_row: Any) -> PipelineEvidenceDTO | None:
    """The attested verdict behind a finished round.

    The chain's join key is ``{pr_number, item_id, head_sha}``, so the head sha
    is what turns "show me this round's verdict" into a lookup. The round row
    carries it; the attested event is the fallback for a pre-v32 or refusal
    row that recorded none.
    """
    ref = str(getattr(round_row, "head_sha", "") or "") or str(
        _payload(facts.verdict).get("head_sha") or ""
    )
    return PipelineEvidenceDTO(kind="review_verdict", ref=ref) if ref else None


def _review_stage(facts: _Facts) -> PipelineStageDTO:
    if UNREADABLE_ROUNDS in facts.unavailable:
        return PipelineStageDTO(
            stage="review",
            state="unknown",
            label="unknown",
            detail="the review rounds could not be read",
        )
    if facts.reviewing:
        return PipelineStageDTO(
            stage="review",
            state="working",
            label=f"round {len(facts.rounds) + 1}",
            detail="reviewing",
        )
    if not facts.rounds:
        return PipelineStageDTO(stage="review", state="pending", label="not started")
    latest = facts.rounds[-1]
    recorded = str(
        getattr(latest, "effective_verdict", None)
        or getattr(latest, "verdict", "")
        or ""
    )
    state, detail = _REVIEW_VERDICT.get(
        recorded, ("done", recorded.lower().replace("_", " "))
    )
    return PipelineStageDTO(
        stage="review",
        state=state,
        label=f"round {getattr(latest, 'round', len(facts.rounds))}",
        detail=detail,
        evidence=_verdict_evidence(facts, latest),
    )


def _merge_stage(facts: _Facts) -> PipelineStageDTO:
    view = facts.view
    pr_url = view.artifact.pr_url
    evidence = PipelineEvidenceDTO(kind="pull_request", ref=pr_url) if pr_url else None
    cell = _MERGE_CELL.get(view.judgment.state)
    if cell is not None:
        state, label, detail = cell
        return PipelineStageDTO(
            stage="merge", state=state, label=label, detail=detail, evidence=evidence
        )
    if view.judgment.state == "unknown":
        return PipelineStageDTO(
            stage="merge",
            state="unknown",
            label="unknown",
            detail="the pull request was not observed",
            evidence=evidence,
        )
    if view.outcome.state in _SETTLED_OUTCOMES:
        return PipelineStageDTO(
            stage="merge", state="not_required", label="not required"
        )
    return PipelineStageDTO(stage="merge", state="pending", label="not started")


def _lane_name(config: DispatchConfig | None) -> str:
    """The lane is the branch the work lands on — the configured base, or main.

    Same resolution ``kew.publication_shadow_hook`` uses, so the swimlane's row
    label and the branch the push actually targets cannot disagree.
    """
    return (config.branches.base if config else "") or "main"


def item_pipeline(
    store: Any,
    item_id: str,
    *,
    config: DispatchConfig | None = None,
    now: datetime | None = None,
) -> ItemPipelineDTO | None:
    """One item's lanes and stages, or ``None`` when the item has no runs.

    ``config`` supplies the delivery repository and the base branch the lane is
    named for; without it the lane falls back to ``main`` and the projection is
    otherwise unchanged.
    """
    moment = now or datetime.now(UTC)
    facts = _collect(store, item_id, config=config, now=moment)
    if facts is None:
        return None
    stages = [
        _implement_stage(facts),
        _publish_stage(facts),
        _review_stage(facts),
        _merge_stage(facts),
    ]
    return ItemPipelineDTO(
        item_id=item_id,
        lanes=[PipelineLaneDTO(name=_lane_name(config), stages=stages)],
        unavailable=list(facts.unavailable),
        composed_at=moment.isoformat(),
        merge_verify=facts.merge_verify,
    )


def _epic_node_ids(store: Any) -> set[str]:
    """Every node id an epic's newest tick names — the Epic screen's rows."""
    reader = _reader(store, "latest_epic_ticks")
    if reader is None:
        return set()
    return {
        str(node.get("id"))
        for tick in reader().values()
        for node in (tick.snapshot or {}).get("nodes") or ()
        if isinstance(node, Mapping) and node.get("id") is not None
    }


def _known_without_runs(
    store: Any, item_id: str, config: DispatchConfig | None
) -> bool:
    """Whether a console row can name ``item_id`` although nothing ran for it.

    Local facts only: the ready queue as last read (the Work screen's idle
    rows) and the epics' newest ticks (the Epic screen's unstarted nodes). A
    tracker read here would turn every unknown id into a remote request.
    """
    if item_id in ready_queue_item_ids(config):
        return True
    return item_id in _epic_node_ids(store)


def _no_run_pipeline(
    item_id: str, config: DispatchConfig | None, now: datetime
) -> ItemPipelineDTO:
    """Four pending stages: an item nothing has run for has an empty pipeline."""
    stages = [
        PipelineStageDTO(stage=stage, state="pending", label="not started")
        for stage in PIPELINE_STAGES
    ]
    return ItemPipelineDTO(
        item_id=item_id,
        lanes=[PipelineLaneDTO(name=_lane_name(config), stages=stages)],
        unavailable=[],
        composed_at=now.isoformat(),
    )


def route_pipeline(
    store: Any,
    item_id: str,
    *,
    config: DispatchConfig | None = None,
    now: datetime | None = None,
) -> ItemPipelineDTO | None:
    """What ``GET /api/work/{id}/pipeline`` answers (#2572).

    :func:`item_pipeline` for an item with runs; four pending stages for an
    item the console knows but nothing has run for; ``None`` — a 404 — only
    for an id nothing here knows. :func:`item_pipeline` keeps its own ``None``
    for "no runs", which the evidence chain reads as "nothing to attest".
    """
    moment = now or datetime.now(UTC)
    dto = item_pipeline(store, item_id, config=config, now=moment)
    if dto is not None:
        return dto
    with store.read_snapshot():
        known = _known_without_runs(store, item_id, config)
    return _no_run_pipeline(item_id, config, moment) if known else None
