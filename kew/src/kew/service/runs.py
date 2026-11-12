"""Run + flight-recording reads. The single surface for TUI and serve.

The store is injected (a RunStore-shaped object) — never constructed here, and
no orchestrator is imported, so the import-linter contract for kew.serve stays
clean. Nothing here APPENDS to the ledger: the only thing taken from the audit
side is ``kew.review_audit``'s event NAME, so the verdict read below names the
attested fact instead of restating its string. The one thing taken from
``kew.run_store`` is its ``run_kind`` vocabulary (the same import
``kew.service.pipeline`` makes), so the stage projection below names the
recorded kinds instead of restating them.

Flight reads (I16) resolve against durable evidence only: the ``audit_events``
host sequence, the artifact receipt that seals a prefix of it, and the
preserved work package that receipt addresses by digest. No function here
consults a repository — a working tree, an index, or an object database — so a
checkout that has moved on since the run ended cannot change what a past run
is reported to have done.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from kew.artifact_receipt import ArtifactReceipt
from kew.artifact_store import (
    PackageStore,
    PackageStoreError,
    default_root,
)
from kew.audit_chain import ChainResult
from kew.flight.evidence import (
    AVAILABLE,
    UNAVAILABLE,
    ArtifactDiff,
    ArtifactUnbound,
    FlightTimeline,
    artifact_node_id,
    build_artifact_diff,
    build_flight_timeline,
)
from kew.flight.timeline import Timeline, build_timeline, linear_path
from kew.journal_replay import ReplayResult, replay_host_sequence
from kew.pricing import is_free_model
from kew.publication_artifact import ArtifactRejected
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    CI_FIX_RUN_KIND,
    PR_SUBJECT_RUN_KINDS,
    REVIEW_REMEDIATION_RUN_KIND,
)
from kew.run_store import (
    # Re-exported (the `as` form is the explicit re-export) for the serve
    # layer, which must not import kew.run_store itself — import-linter
    # contract "kew.serve reads runs only through kew.service".
    MAX_RUN_PAGE as MAX_RUN_PAGE,
)
from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    SensitivePromptError,
    read_prompt,
)
from kew.sensitive_prompt import default_root as protected_root
from kew.service.audit import chain_verdict
from kew.service.models import (
    VERDICT_SCOPE_ITEM,
    VERDICT_SCOPE_RUN,
    ArtifactFileDTO,
    DiffDTO,
    EvidenceAvailabilityDTO,
    EvidenceGapDTO,
    FlightEventDTO,
    FlightTimelineDTO,
    MetaDTO,
    ProtectedPromptDTO,
    RunDTO,
    RunVerdictDTO,
    SandboxPostureDTO,
    StepDTO,
    TimelineDTO,
    WorkViewDTO,
)
from kew.service.work import cached_pull_request, get_work_view

_logger = logging.getLogger(__name__)

_POSTURE_EVENT_LIMIT = 32
_POSTURE_READ_PASSES = 3

#: The stage vocabulary for :attr:`RunDTO.stage` (FUI-13), in pipeline order.
#: ``unknown`` is an answer, not a fallback guess: the Monitor's Stage column
#: would rather say nothing than name the wrong stage.
RUN_STAGES: tuple[str, ...] = ("implement", "publish", "review", "ci_fix", "unknown")

#: Recorded ``run_kind`` -> the stage that kind executes.
#:
#: Governed review IS a row in the runs table — ``adversarial_review`` for the
#: reviewer, ``review_remediation`` for the driver-invoked remediation pass — so
#: the Monitor needs no second source for it and this projection reads no
#: review-round rows to find one.
#:
#: A kind absent from this map is ``unknown``, deliberately: ``resolve`` (a
#: conflict resolution on an open PR), ``readiness_probe`` (not customer work at
#: all), the bare ``review`` kind (``kew review <PR>`` on a PR with no tracked
#: item, which is UNgoverned, and labelling it ``review`` would let the Monitor
#: caption it "governed review") and any kind from a newer host all have no word
#: in kew's vocabulary. So does a row that predates the ``run_kind`` column,
#: or a narrow read double that never had it: reading an absent column as
#: ``implement`` would put a confident stage on a run nobody asked.
_KIND_STAGE: dict[str, str] = {
    "dispatch": "implement",
    ADVERSARIAL_REVIEW_RUN_KIND: "review",
    REVIEW_REMEDIATION_RUN_KIND: "review",
    CI_FIX_RUN_KIND: "ci_fix",
}

#: ``awaiting_publication`` is the compatibility encoding of an implementation
#: run that DID succeed with only publication outstanding (the same reading
#: ``kew.service.pipeline`` gives it), so the stage it has reached is publish.
_PUBLISHING_STATUS = "awaiting_publication"

#: The status that means this run is still executing. Only then does the payload
#: carry a round number (see :func:`_round_in_flight`).
_LIVE_STATUS = "pending"

#: Governed-review kind -> where its round number sits relative to the rounds
#: already recorded in the cycle. A round row is written when the round ENDS, so
#: the live reviewer's own round is one past the count; a remediation pass was
#: dispatched to fix what the newest recorded round found, so it belongs to that
#: round rather than to the next one.
_ROUND_OFFSET: dict[str, int] = {
    ADVERSARIAL_REVIEW_RUN_KIND: 1,
    REVIEW_REMEDIATION_RUN_KIND: 0,
}

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)/?$")


class _Store(Protocol):
    """The read surface these functions compose over (structural, not imported).

    Only ``list_all`` is required of every caller. Detail selection also uses
    the work reads exposed by the committed cursor.
    """

    def list_all(self) -> list[Any]: ...
    def read_committed(self) -> AbstractContextManager[Any]: ...
    def read_snapshot(self) -> AbstractContextManager[None]: ...
    def list_by_item(self, item_id: str) -> list[Any]: ...
    def get_admission_identity(self, run_id: str) -> Any | None: ...
    def list_orphan_prs(self, include_acked: bool = False) -> list[Any]: ...


def read_events(event_file: Path) -> list[dict[str, Any]]:
    """Parse a .events.jsonl file, skipping blank/partial lines."""
    out: list[dict[str, Any]] = []
    if not event_file or not Path(event_file).exists():
        return out
    for raw in Path(event_file).read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _find_records(store: _Store, run_ids: Iterable[str]) -> dict[str, Any]:
    """Index requested records once, using point reads when the store supports them."""
    requested = dict.fromkeys(run_ids)
    get = getattr(store, "get", None)
    if callable(get):
        return {run_id: get(run_id) for run_id in requested}
    # Compatibility for narrow read-only stores without a point accessor.
    records = {str(rec.run_id): rec for rec in store.list_all()}
    return {run_id: records.get(run_id) for run_id in requested}


def _find_record(store: _Store, run_id: str) -> Any | None:
    return _find_records(store, [run_id]).get(run_id)


def event_file_for(store: _Store, run_id: str) -> Path | None:
    """Derive the .events.jsonl path from a record's log_file (it is not a
    stored field — see run_store / fleet)."""
    rec = _find_record(store, run_id)
    if rec is None or not getattr(rec, "log_file", None):
        return None
    return Path(rec.log_file).with_suffix(".events.jsonl")


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


# Coercion helpers keep _record_to_run_dto branch-free (xenon rank A, #315):
# each `getattr(...) or default` is a decision point, so we hoist them here.
def _int(rec: Any, name: str, default: int = 0) -> int:
    return int(getattr(rec, name, default) or default)


def _float(rec: Any, name: str, default: float = 0.0) -> float:
    return float(getattr(rec, name, default) or default)


def _str(rec: Any, name: str) -> str:
    return str(getattr(rec, name, "") or "")


def _opt_str(rec: Any, name: str) -> str | None:
    """A recorded text column, or None when the row simply has no such column.

    Unlike :func:`_str` this keeps the difference between "recorded as empty"
    and "not recorded": the run page prints the second as unavailable rather
    than as a blank value it would read as a fact.
    """
    value = getattr(rec, name, None)
    return str(value) if isinstance(value, str) and value else None


def _opt_float(rec: Any, name: str) -> float | None:
    """A recorded figure, or None. ``0.0`` is a figure and survives.

    ``bool`` is excluded deliberately — it passes ``isinstance(.., int)``, and
    a column read as ``True`` would be projected as a cost of $1.00.
    """
    value = getattr(rec, name, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _stage_of(rec: Any) -> str:
    """The stage this run's recorded kind executes (:data:`RUN_STAGES`)."""
    stage = _KIND_STAGE.get(_str(rec, "run_kind"), "unknown")
    publishing = stage == "implement" and _str(rec, "status") == _PUBLISHING_STATUS
    return "publish" if publishing else stage


def _round_in_flight(store: _Store, rec: Any) -> int:
    """The governed-review round this live run belongs to, or 0 for "unknown".

    Only a run that is still executing gets a number. Which round a FINISHED
    review run served cannot be recovered — ``review_rounds`` rows carry no run
    id — and inventing one is exactly the guess this projection refuses.

    0 is also the answer when the round store cannot be read at all: a store
    predating the table, a narrow read double, or a failed read. The detail line
    then omits the round rather than naming a number nobody counted.
    """
    offset = _ROUND_OFFSET.get(_str(rec, "run_kind"))
    item_id = _work_item_id(rec)
    read = getattr(store, "review_round_count", None)
    if offset is None or not item_id or not callable(read):
        return 0
    try:
        return int(read(item_id)) + offset
    except Exception:  # noqa: BLE001 — the round is omitted, and said so here
        _logger.warning(
            "review round count unreadable for item %s; the run's stage detail "
            "will omit the round number",
            item_id,
            exc_info=True,
        )
        return 0


def _subject_pr_url(store: _Store, rec: Any) -> str:
    """The PR url of the run that OPENED this run's branch, or "".

    A run in ``PR_SUBJECT_RUN_KINDS`` — the governed reviewer among them — works
    on a pull request it did not open, so no writer ever puts a ``pr_url`` on
    its row: ``dispatch_core._persist_pr_url`` stamps the authoring run, and the
    only other writer (``outcomes.sync_outcomes``) sweeps terminal rows long
    after the fact, so a LIVE reviewer's own row is empty by construction. The
    PR it is reviewing is still a recorded fact, one join away: the store's own
    PR->item join, ``find_run_by_branch``, returns the authoring run for a
    branch and looks past the PR-subject kinds while doing it.

    Read defensively, like every other optional store read here: a narrow read
    double has no branch join, and a read that fails costs the detail line its
    PR reference rather than raising out of a projection the Monitor polls.
    """
    branch = _str(rec, "branch")
    find = getattr(store, "find_run_by_branch", None)
    subject = _str(rec, "run_kind") in PR_SUBJECT_RUN_KINDS
    if not subject or not branch or not callable(find):
        return ""
    try:
        return _str(find(branch), "pr_url").strip()
    except Exception:  # noqa: BLE001 — the PR is omitted, and said so here
        _logger.warning(
            "branch join unreadable for %s; the run's stage detail will omit "
            "the pull request it is working on",
            branch,
            exc_info=True,
        )
        return ""


def _pr_ref(store: _Store, rec: Any) -> str:
    """``PR #2059`` for the pull request this run is working on, or "".

    The run's own persisted url first — that is the authoring run's own fact —
    then the branch join for the kinds that never author one.
    """
    url = _str(rec, "pr_url").strip() or _subject_pr_url(store, rec)
    match = _PR_NUMBER_RE.search(url)
    return f"PR #{match.group(1)}" if match else ""


def _stage_detail(store: _Store, rec: Any, stage: str, round_number: int) -> str:
    """The Stage cell's second line: the recorded facts that qualify the stage.

    Every part is read off the row (or counted in the round store, or joined to
    the authoring run by branch); nothing here is composed from a status word.
    An unmapped kind is NAMED rather than left blank, so ``unknown`` does not
    leave an operator with nothing to look up.
    """
    attempt = _int(rec, "attempt_number", 1)
    retried = stage == "implement" and attempt > 1
    parts = (
        f"round {round_number}" if round_number else "",
        _str(rec, "run_kind") if stage == "unknown" else "",
        f"attempt {attempt}" if retried else "",
        _pr_ref(store, rec),
    )
    return " · ".join(part for part in parts if part)


def _run_stage(store: _Store, rec: Any) -> tuple[str, str]:
    """This run's stage and its detail line, both from recorded facts only."""
    stage = _stage_of(rec)
    live = stage == "review" and _str(rec, "status") == _LIVE_STATUS
    round_number = _round_in_flight(store, rec) if live else 0
    return stage, _stage_detail(store, rec, stage, round_number)


def _record_to_run_dto(
    rec: Any,
    sandbox: SandboxPostureDTO | None = None,
    stage: tuple[str, str] = ("unknown", ""),
    verdict: RunVerdictDTO | None = None,
) -> RunDTO:
    """One recorded run as the console reads it.

    ``verdict`` is supplied by the DETAIL read only. A list page may run to
    200 rows and the verdict costs an indexed audit read per work item, so
    :func:`list_runs` leaves it None rather than turning one poll into 200
    queries (the C3 rule: the cost of a poll is the page).

    `motion` and `alive` are not passed: `MotionDTO` derives both from
    `status`, which for a run means a `pending` row reads `executing`.

    #2305's rule is stricter than that — a pending run whose owning process
    group is PROVABLY dead is an orphan kew owes a reap rather than work in
    flight (`run_motion(status, owner_alive=...)` in kew/motion.py) — and this
    read path deliberately does not apply it. Establishing owner liveness means
    reading the host process table, and a Flight read may never shell out:
    `TestNoMutableRepositoryIsConsulted` in tests/test_flight_evidence.py pins
    this module to that, and the same rule is why slow probes belong in the
    serve sweep. Nothing is lost for long — the driver's own tick reaps a
    provably dead owner and records `killed`, which this reads as settled on the
    next poll. A caller that already HOLDS a liveness reading passes the refined
    motion itself.
    """
    model = _str(rec, "model")
    posture = sandbox or SandboxPostureDTO()
    return RunDTO(
        run_id=str(rec.run_id),
        issue_num=_int(rec, "issue_num"),
        title=_str(rec, "title"),
        runner=_str(rec, "runner_name"),
        model=model,
        status=_str(rec, "status"),
        cost_usd=_float(rec, "estimated_cost_usd"),
        channel="local" if is_free_model(model) else "hosted",
        started_at=_iso(getattr(rec, "started_at", None)),
        finished_at=_iso(getattr(rec, "finished_at", None)),
        duration_seconds=_float(rec, "duration_seconds"),
        input_tokens=_int(rec, "input_tokens"),
        output_tokens=_int(rec, "output_tokens"),
        cache_read_tokens=_int(rec, "cache_read_tokens"),
        cache_creation_tokens=_int(rec, "cache_creation_tokens"),
        diff_additions=getattr(rec, "diff_additions", None),
        diff_deletions=getattr(rec, "diff_deletions", None),
        diff_files=getattr(rec, "diff_files", None),
        review_cycles=getattr(rec, "review_cycles", None),
        pr_url=getattr(rec, "pr_url", None),
        pr_state=getattr(rec, "pr_state", None),
        attempt_number=_int(rec, "attempt_number", 1),
        error_category=_str(rec, "error_category"),
        complexity=_str(rec, "complexity"),
        branch=_str(rec, "branch"),
        stage=stage[0],
        stage_detail=stage[1],
        run_kind=_str(rec, "run_kind"),
        item_id=_opt_str(rec, "item_id"),
        reported_model=_opt_str(rec, "reported_model"),
        provider=_opt_str(rec, "provider"),
        transport_mode=_str(rec, "transport_mode"),
        planned_cost_usd=_opt_float(rec, "planned_cost_usd"),
        accounting_status=_opt_str(rec, "accounting_status"),
        verdict=verdict,
        **posture.model_dump(),
    )


def _verdict_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _payload_int(payload: dict[str, Any], name: str) -> int:
    """A whole number recorded in a payload, or 0. Never raises."""
    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _payload_str(payload: dict[str, Any], name: str) -> str | None:
    """A non-empty string recorded in a payload, or None."""
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _timestamp_of(event: Any) -> str | None:
    """The row's ISO timestamp, or None when it is not a string."""
    value = getattr(event, "timestamp", None)
    return value if isinstance(value, str) and value else None


def _verdict_dto(event: Any, scope: str) -> RunVerdictDTO | None:
    """Project one verdict row, or None when it attests no verdict word.

    Every field is type-checked before it is projected. A bad historical row is
    TOLERATED by the reader and never rewritten (the operator rule), so a
    payload whose ``round`` is a string reads as round 0 rather than raising
    out of the detail endpoint.
    """
    payload = _verdict_payload(event)
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        return None
    return RunVerdictDTO(
        round=_payload_int(payload, "round"),
        verdict=verdict,
        head_sha=_payload_str(payload, "head_sha"),
        reviewer_model=_payload_str(payload, "reviewer_model"),
        ts=_timestamp_of(event),
        scope=scope,
    )


def _newest_verdict_event(read: Any, subject: str, **narrow: Any) -> Any | None:
    """The newest verdict row matching one narrowing, or None."""
    try:
        events = list(read(limit=1, event=REVIEW_VERDICT_EVENT, **narrow))
    except Exception as exc:
        # A store that cannot answer this filter (a narrow read double, an
        # older schema) leaves the verdict unread, and the page says so by
        # showing no verdict. The reason is logged rather than swallowed: "no
        # verdict recorded" and "the verdict could not be read" are different
        # facts, and only the log can tell them apart afterwards.
        _logger.warning("Could not read the review verdict for %s: %s", subject, exc)
        return None
    return events[0] if events else None


def _latest_verdict(store: _Store, rec: Any) -> RunVerdictDTO | None:
    """The governed-review verdict this run's page may show, and whose it is.

    Read from the ``adversarial_review_verdict`` audit event rather than from
    the ``review_rounds`` table: the audit row IS the attested fact (the table
    is operational state and carries no reviewer model), and the event name is
    served by the ``(event, timestamp)`` index with the subject narrowed in
    SQL, so each arm below is one bounded read.

    THIS RUN's verdict first. A governed review attests under its own run id,
    so the round a review run judged is attested evidence about that run and is
    returned as :data:`VERDICT_SCOPE_RUN`.

    Only when no verdict names this run does the work item's latest stand in,
    as :data:`VERDICT_SCOPE_ITEM`. An implementation run has no verdict of its
    own and what its operator needs to know is which decision now stands over
    the branch it pushed — but that decision may have been recorded for a LATER
    attempt, so the scope travels with it and the page says whose it is instead
    of printing another attempt's round and reviewed head as this run's. (The
    same cross-attempt error the Gantt lane fixes by naming the attempt a bar
    links to.) Two indexed reads at worst, on the detail path only.

    A row with no recorded verdict word, or a store with no audit surface at
    all, is None — never a round number this projection invented.
    """
    read = getattr(store, "list_audit_events", None)
    if not callable(read):
        return None
    run_id = _str(rec, "run_id")
    own = _newest_verdict_event(read, run_id, run_id=run_id) if run_id else None
    if own is not None:
        dto = _verdict_dto(own, VERDICT_SCOPE_RUN)
        if dto is not None:
            return dto
    item_id = _work_item_id(rec)
    if not item_id:
        return None
    standing = _newest_verdict_event(read, item_id, item_or_issue=item_id)
    return None if standing is None else _verdict_dto(standing, VERDICT_SCOPE_ITEM)


def _posture_events(
    store: _Store, *, run_id: str | None = None, limit: int = _POSTURE_EVENT_LIMIT
) -> list[Any]:
    """Read posture events when the injected store exposes the audit ledger."""
    read = getattr(store, "list_audit_events", None)
    if not callable(read):
        return []
    filters: dict[str, object] = {"limit": limit, "event": "sandbox_resolved"}
    if run_id is not None:
        filters["run_id"] = run_id
    return list(read(**filters))


def _recorded_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return value if isinstance(value, str) and value else "not recorded"


def _posture_dto(event: Any | None) -> SandboxPostureDTO:
    if event is None or not isinstance(getattr(event, "payload", None), dict):
        return SandboxPostureDTO()
    payload = event.payload
    raw_names = payload.get("credential_names")
    names = (
        [name for name in raw_names if isinstance(name, str)]
        if isinstance(raw_names, list)
        else []
    )
    reason = payload.get("reason")
    basis = payload.get("egress_verification_basis")
    return SandboxPostureDTO(
        posture=_recorded_text(payload, "posture"),
        filesystem_boundary=_recorded_text(payload, "filesystem_boundary"),
        network_boundary=_recorded_text(payload, "network_boundary"),
        egress_verification_basis=basis if isinstance(basis, str) else None,
        credential_names=names,
        command_guard=_recorded_text(payload, "command_guard"),
        admission=_recorded_text(payload, "admission"),
        reason=reason if isinstance(reason, str) else None,
    )


def list_run_postures(
    store: _Store, run_ids: Iterable[str]
) -> dict[str, SandboxPostureDTO]:
    """Read each requested run's latest attempt through the shared selector."""
    return {
        run_id: selection[2]
        for run_id, selection in select_run_postures(store, run_ids).items()
    }


def select_run_postures(
    store: _Store, run_ids: Iterable[str]
) -> dict[str, tuple[int, Any, SandboxPostureDTO]]:
    """Return each selected attempt, its matching row, and its posture."""
    return {
        run_id: _select_run_posture(store, run_id) for run_id in dict.fromkeys(run_ids)
    }


@dataclass(frozen=True)
class RunDetail:
    """One selected attempt and the facts composed before its snapshot closes."""

    attempt_number: int
    record: Any
    posture: SandboxPostureDTO
    work: WorkViewDTO | None


class _SelectedWorkReader:
    """Keep the selected row authoritative, including in memory degradation."""

    def __init__(self, reader: Any, record: Any) -> None:
        self._reader = reader
        self._record = record

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)

    @staticmethod
    def _order(record: Any) -> tuple[str, int]:
        return (
            _iso(getattr(record, "started_at", None)) or "",
            _int(record, "attempt_number", 1),
        )

    def list_by_item(self, item_id: str) -> list[Any]:
        selected = self._record
        history = self._reader.list_by_item(item_id)
        return [selected] + [
            record
            for record in history
            if record.run_id != selected.run_id
            and self._order(record) < self._order(selected)
        ]

    def get_admission_identity(self, run_id: str) -> Any:
        list_identities = getattr(self._reader, "list_admission_identities", None)
        if not callable(list_identities):
            return self._reader.get_admission_identity(run_id)
        eligible = [
            identity
            for identity in list_identities(run_id)
            if identity.attempt_number <= _int(self._record, "attempt_number", 1)
        ]
        return max(eligible, key=lambda identity: identity.attempt_number, default=None)


def _selected_work_view(reader: Any, record: Any, **reads: Any) -> WorkViewDTO | None:
    work_store = getattr(reader, "work_store", reader)
    if record is None or not callable(getattr(work_store, "list_by_item", None)):
        return None
    return get_work_view(
        _SelectedWorkReader(work_store, record),
        _work_item_id(record),
        pull_request=cached_pull_request(record),
        **reads,
    )


def select_run_detail(
    store: _Store,
    run_id: str,
    *,
    tracker_item: Any = None,
    delivery_repo: str | None = None,
    composed_at: Any = None,
    pending_max_age_s: float | None = None,
) -> RunDetail:
    """Select posture and compose work facts inside one committed snapshot.

    ``pending_max_age_s`` is the in-flight bound a caller holding a config
    passes (:func:`kew.service.work.pending_run_max_age_s`), for the reason
    every other composing surface passes it: a detail page that read a pending
    row against the bare floor while the projection beside it read the
    operator's own budget would print a different exception — and a
    ``view_revision`` no action on that item could satisfy.
    """
    read = getattr(store, "read_committed", None)
    with read() if callable(read) else nullcontext(store) as reader:
        attempt, record, posture = _verify_posture(reader, run_id)
        record = copy(record)
        work = _selected_work_view(
            reader,
            record,
            tracker_item=tracker_item,
            delivery_repo=delivery_repo,
            composed_at=composed_at,
            pending_max_age_s=pending_max_age_s,
        )
        return RunDetail(attempt, record, posture, work)


def _posture_attempt(event: Any) -> int | None:
    """Legacy events belong only to attempt one; invalid identities certify nothing."""
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return None
    attempt = payload.get("attempt_number", 1)
    return attempt if type(attempt) is int and attempt > 0 else None


def _pending_posture(
    events: list[Any], *, reason: str = "latest attempt has no recorded sandbox posture"
) -> SandboxPostureDTO:
    """Reject stale evidence without copying its containment or credentials."""
    sidecar = any(
        _posture_dto(event).egress_verification_basis
        in {"pending-sidecar", "sidecar-admission"}
        or _posture_dto(event).network_boundary == "sidecar-certified"
        for event in events
    )
    return SandboxPostureDTO(
        posture="pending",
        network_boundary="unavailable",
        egress_verification_basis="pending-sidecar" if sidecar else None,
        reason=reason,
    )


def get_run_posture(store: _Store, run_id: str) -> SandboxPostureDTO:
    """Select the latest attempt established by the row or posture evidence."""
    return _select_run_posture(store, run_id)[2]


def _posture_selection_attempt(attempt: int, events: list[Any]) -> int:
    """A newer event advances a stale row; an older event cannot roll it back."""
    return max(attempt, _posture_attempt(events[0]) or 0) if events else attempt


def _read_posture_attempt(
    store: _Store, run_id: str, attempt: int
) -> tuple[int, list[Any]]:
    """Select forward using the newest event and a bounded fallback page."""
    events = _posture_events(store, run_id=run_id, limit=1)
    attempt = _posture_selection_attempt(attempt, events)
    if events and _posture_attempt(events[0]) != attempt:
        events = _posture_events(store, run_id=run_id, limit=_POSTURE_EVENT_LIMIT)
        attempt = _posture_selection_attempt(attempt, events)
    return attempt, events


def _posture_for_attempt(attempt: int, events: list[Any]) -> SandboxPostureDTO:
    """Use only matching evidence from the newest bounded page."""
    for event in events:
        if _posture_attempt(event) == attempt:
            return _posture_dto(event)
    if events or attempt > 1:
        return _pending_posture(events)
    return SandboxPostureDTO()


def _select_run_posture(
    store: _Store, run_id: str
) -> tuple[int, Any, SandboxPostureDTO]:
    """Read rows and evidence only through the store's committed read surface.

    Narrow injected test stores retain plain-read compatibility.
    File snapshots cannot change between row and event reads, so use one pass.
    Test-only memory stores retain three verification passes without BEGIN.
    """
    read = getattr(store, "read_committed", None)
    with read() if callable(read) else nullcontext(store) as reader:
        return _verify_posture(reader, run_id)


def _verify_posture(reader: Any, run_id: str) -> tuple[int, Any, SandboxPostureDTO]:
    record = _find_record(reader, run_id)
    attempt = _int(record, "attempt_number", 1)
    observed_events: list[Any] = []
    passes = (
        1 if getattr(reader, "consistent_snapshot", False) else _POSTURE_READ_PASSES
    )
    for pass_number in range(passes):
        if pass_number:
            record = _find_record(reader, run_id)
        attempt = max(attempt, _int(record, "attempt_number", 1))
        attempt, events = _read_posture_attempt(reader, run_id, attempt)
        observed_events.extend(events)
        record = _find_record(reader, run_id)
        row_attempt = _int(record, "attempt_number", 1)
        if not getattr(reader, "evidence_available", True):
            return (
                row_attempt,
                record,
                _pending_posture([], reason="uncommitted posture unavailable"),
            )
        if row_attempt == attempt:
            return attempt, record, _posture_for_attempt(attempt, events)
        if row_attempt < attempt:
            return (
                row_attempt,
                record,
                _pending_posture(
                    observed_events,
                    reason="posture evidence has no matching run attempt",
                ),
            )
        attempt = row_attempt
    return (
        attempt,
        record,
        _pending_posture(observed_events, reason="posture read raced a retry"),
    )


def sandbox_posture_summary(posture: SandboxPostureDTO) -> str:
    """Render the shared one-line posture description."""
    if posture.posture == "not recorded":
        return "not recorded"
    credentials = ",".join(posture.credential_names) or "none"
    reason = posture.reason or "none"
    basis = posture.egress_verification_basis or "not recorded"
    return (
        f"{posture.posture}; filesystem_boundary={posture.filesystem_boundary}; "
        f"network_boundary={posture.network_boundary}; "
        f"egress_verification_basis={basis}; "
        f"credential_names={credentials}; "
        f"command_guard={posture.command_guard}; admission={posture.admission}; "
        f"reason={reason}"
    )


def build_meta(
    *, mode: str, plan_label: str, plan_monthly_usd: float, version: str
) -> MetaDTO:
    """Account-level cost-framing context for the UI (from BillingConfig)."""
    return MetaDTO(
        billing=mode,
        plan_label=plan_label,
        plan_monthly_usd=plan_monthly_usd,
        version=version,
    )


def _run_page(
    store: _Store,
    *,
    limit: int,
    before: str | None,
    status: str | None,
    kind: str | None,
) -> list[Any]:
    """One page of rows, newest first — in SQL when the store can page.

    The console re-reads this page every few seconds. A store with
    ``list_run_page`` answers with one bounded query; the narrow read-only
    fakes older callers inject have only ``list_all``, so they keep the
    in-Python sort. The fallback applies the same filters, so a caller cannot
    tell the two stores apart by their answer — only by their cost.

    The cap is refused here, not clamped, and before either branch runs: a
    caller asking for more than ``MAX_RUN_PAGE`` rows has a bug, and quietly
    handing back a shorter page would hide it behind what looks like the end
    of the ledger.
    """
    if limit < 1 or limit > MAX_RUN_PAGE:
        raise ValueError(f"run page limit must be between 1 and {MAX_RUN_PAGE}")
    page = _capability(store, "list_run_page")
    if page is not None:
        rows: list[Any] = page(limit=limit, before=before, status=status, kind=kind)
        return rows
    records = sorted(
        store.list_all(),
        key=lambda r: str(getattr(r, "started_at", "") or ""),
        reverse=True,
    )

    def kept(record: Any) -> bool:
        started = _iso(getattr(record, "started_at", None)) or ""
        return (
            (before is None or started < before)
            and (status is None or _str(record, "status") == status)
            and (kind is None or (_str(record, "run_kind") or "dispatch") == kind)
        )

    return [record for record in records if kept(record)][:limit]


def list_runs(
    store: _Store,
    *,
    limit: int = 50,
    before: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[RunDTO]:
    """One bounded page of runs, newest first, filtered SQL-side (C3).

    ``before`` is the previous page's last stored ``started_at`` text, so the
    cursor compares the way SQLite ordered the rows in the first place.
    """
    page = _run_page(store, limit=limit, before=before, status=status, kind=kind)
    selections = select_run_postures(store, (str(record.run_id) for record in page))
    return [
        _record_to_run_dto(record, posture, _run_stage(store, record))
        for _, record, posture in selections.values()
        if record is not None
    ]


def get_run(store: _Store, run_id: str) -> RunDTO | None:
    _, record, posture = _select_run_posture(store, run_id)
    if record is None:
        return None
    return _record_to_run_dto(
        record,
        posture,
        _run_stage(store, record),
        _latest_verdict(store, record),
    )


def run_exists(store: _Store, run_id: str) -> bool:
    """Whether this store has a row for this run — one point read, no projection.

    The 404 answer is all a route that then reads something else about the run
    needs, and it is the cheapest question the store answers. :func:`get_run`
    answers a much larger one: posture evidence, the stage detail and the two
    indexed verdict reads it documents as detail-path-only. A live tail polling
    a run's ledger page once a second paid for all of them per beat and threw
    every one away (C3: the cost of a poll is the page).
    """
    return _find_record(store, run_id) is not None


def get_run_work_view(
    store: _Store,
    run_id: str,
    *,
    tracker_item: Any = None,
    delivery_repo: str | None = None,
    composed_at: Any = None,
    pending_max_age_s: float | None = None,
) -> WorkViewDTO | None:
    """The canonical work projection at this run's selected attempt.

    The selection pins posture and work facts to one committed snapshot.
    Later runs cannot replace the selected execution identity.

    The run detail surface asks "what is happening with this work, and what
    should I do about it?" of :mod:`kew.service.work` rather than answering it
    from ``RunDTO.status``: a run row knows its own exit code and nothing about
    publication, judgment, source synchronization or review capacity, so every
    surface that read it was guessing the other five answers.

    The run's persisted ``pr_url``/``pr_state`` ride along as the *cached*
    forge observation they are — a real reading, marked stale — so a detail
    page with no network still shows a truthful judgment instead of a blank.
    """
    return select_run_detail(
        store,
        run_id,
        tracker_item=tracker_item,
        delivery_repo=delivery_repo,
        composed_at=composed_at,
        pending_max_age_s=pending_max_age_s,
    ).work


def _work_item_id(rec: Any) -> str:
    """The record's durable item id, falling back to its numeric GitHub id."""
    return _str(rec, "item_id") or str(getattr(rec, "issue_num", "") or "")


def _timeline_for(store: _Store, run_id: str) -> Timeline | None:
    event_file = event_file_for(store, run_id)
    if event_file is None:
        return None
    return build_timeline({run_id: read_events(event_file)})


def get_timeline(store: _Store, run_id: str) -> TimelineDTO | None:
    """The live activity stream for a run, from the host-written event file.

    Activity markers only. What the run *produced* is the receipt-backed
    projection (:func:`get_flight_timeline`) and the artifact-bound change set
    (:func:`get_diff`); a step here is not a checkpoint anything is diffed
    against, which is why every node reports ``has_diff`` False.
    """
    tl = _timeline_for(store, run_id)
    if tl is None:
        return None
    ordered = linear_path(tl, tl.head_id)
    dto_nodes = [
        StepDTO(
            id=node.id,
            parent_id=node.parent_id,
            seq=node.seq,
            ts=node.ts,
            kind=node.kind,
            label=node.label,
            tokens_in=node.tokens_in,
            tokens_out=node.tokens_out,
            cost_usd=node.cost_usd,
        )
        for node in ordered
    ]
    return TimelineDTO(
        run_id=run_id, root_id=tl.root_id, head_id=tl.head_id, nodes=dto_nodes
    )


# --------------------------------------------------------------------------- #
# Receipt-backed Flight evidence (I16)
# --------------------------------------------------------------------------- #

_NO_LEDGER = "this store has no audit ledger surface to replay"
_NO_PACKAGE_ROOT = "no preserved-package root exists for this project"

_EMPTY_REPLAY = ReplayResult(
    chain=ChainResult(ok=False, broken_at_id=None, head_hash=None, count=0),
    records=(),
    gaps=(),
)


def _capability(store: _Store, name: str) -> Any:
    """The store method *name*, or None when this store does not have one.

    The service surface is handed anything RunStore-shaped, including the
    narrow fakes older callers pass. A store without an evidence surface must
    make the dimension unavailable, not raise into a read.
    """
    method = getattr(store, name, None)
    return method if callable(method) else None


def _replay_for(store: _Store, run_id: str) -> ReplayResult | None:
    """This run's replayed evidence, or None when the store has no ledger.

    The chain verdict comes from the memoized one every other audit reader
    shares (``service.audit.chain_verdict``) rather than from a fresh O(N)
    hash walk per call: Flight is preloaded on hover, so a per-call
    verification made pointing at a run cost the whole audit history
    (C3/#2296).
    """
    if _capability(store, "list_audit_events") and _capability(
        store, "read_chain_rows"
    ):
        return replay_host_sequence(store, run_id=run_id, chain=chain_verdict(store))
    return None


def _receipt_for(
    store: _Store, run_id: str, attempt_number: int | None
) -> ArtifactReceipt | None:
    """The sealed receipt for this attempt, or the run's latest seal."""
    listing = _capability(store, "list_artifact_receipts")
    if listing is None:
        return None
    receipts: list[ArtifactReceipt] = listing(run_id=run_id)
    if attempt_number is not None:
        receipts = [r for r in receipts if r.attempt_number == attempt_number]
    return receipts[-1] if receipts else None


def _seal_detail(store: _Store, receipt: ArtifactReceipt | None) -> str:
    """Empty when the receipt still verifies; why not when it does not."""
    if receipt is None:
        return ""
    verify = _capability(store, "verify_artifact_receipt")
    if verify is None:
        return "this store cannot verify an artifact receipt"
    verification = verify(run_id=receipt.run_id, attempt_number=receipt.attempt_number)
    return "" if verification.ok else verification.detail


def _attestations_for(store: _Store, receipt: ArtifactReceipt | None) -> list[Any]:
    read = _capability(store, "artifact_receipt_attestations")
    if receipt is None or read is None:
        return []
    found: list[Any] = read(
        run_id=receipt.run_id, attempt_number=receipt.attempt_number
    )
    return found


def _packages_root(project_root: Path | str | None) -> Path | None:
    """The host's private package root, only if one has ever been created.

    Deliberately does not create it: a read must not bring evidence storage
    into existence as a side effect of someone opening a page.
    """
    if project_root is None:
        return None
    root = default_root(Path(project_root))
    return root if root.is_dir() else None


def _load_artifact_diff(
    receipt: ArtifactReceipt, project_root: Path | str | None
) -> tuple[ArtifactDiff | None, str]:
    """``(diff, detail)`` — a diff, or why the sealed bytes did not yield one."""
    root = _packages_root(project_root)
    if root is None:
        return None, _NO_PACKAGE_ROOT
    try:
        artifact = PackageStore(root).load(receipt.package_digest)
        return build_artifact_diff(receipt=receipt, artifact=artifact), ""
    except (PackageStoreError, ArtifactRejected, ArtifactUnbound) as exc:
        return None, str(exc)


def _availability_dtos(rows: Any) -> list[EvidenceAvailabilityDTO]:
    return [
        EvidenceAvailabilityDTO(
            dimension=row.dimension,
            availability=row.availability,
            provenance=row.provenance,
            detail=row.detail,
        )
        for row in rows
    ]


def _timeline_dto(timeline: FlightTimeline) -> FlightTimelineDTO:
    return FlightTimelineDTO(
        run_id=timeline.run_id,
        attempt_number=timeline.attempt_number,
        item_id=timeline.item_id,
        issue_num=timeline.issue_num,
        source_tracker=timeline.source_tracker,
        source_id=timeline.source_id,
        sealed=timeline.sealed,
        through_seq=timeline.through_seq,
        chain_head=timeline.chain_head,
        chain_verified=timeline.chain_verified,
        artifact_node_id=timeline.artifact_node_id,
        runner_telemetry=timeline.runner_telemetry,
        events=[
            FlightEventDTO(
                seq=event.seq,
                timestamp=event.timestamp,
                kind=event.kind,
                origin=event.origin,
                producer_kind=event.producer_kind,
                producer_name=event.producer_name,
                producer_version=event.producer_version,
                attested_by_host=event.attested_by_host,
                sealed=event.sealed,
                terminal=event.terminal,
                facts=dict(event.facts),
            )
            for event in timeline.events
        ],
        gaps=[
            EvidenceGapDTO(seq=gap.seq, event=gap.event, reason=gap.reason)
            for gap in timeline.gaps
        ],
        availability=_availability_dtos(timeline.availability),
    )


def get_flight_timeline(
    store: _Store,
    run_id: str,
    *,
    attempt_number: int | None = None,
    project_root: Path | str | None = None,
) -> FlightTimelineDTO:
    """Replay one run's evidence into the canonical Flight projection.

    Always returns a projection: a run with no evidence is a run whose
    dimensions are all unavailable, which is a different statement from "no
    such run" and must not be collapsed into one. ``project_root`` is optional
    because the diff dimension is the only thing that needs the package root —
    without it the timeline reports the diff as unavailable rather than
    pretending nothing was sealed.
    """
    replay = _replay_for(store, run_id)
    receipt = _receipt_for(store, run_id, attempt_number)
    diff, diff_detail = (
        _load_artifact_diff(receipt, project_root)
        if receipt is not None
        else (None, "")
    )
    return _timeline_dto(
        build_flight_timeline(
            run_id=run_id,
            attempt_number=(
                receipt.attempt_number if receipt else (attempt_number or 0)
            ),
            replay=replay if replay is not None else _EMPTY_REPLAY,
            receipt=receipt,
            attestations=_attestations_for(store, receipt),
            seal_detail=(
                _seal_detail(store, receipt) if replay is not None else _NO_LEDGER
            ),
            diff_available=diff is not None,
            diff_detail=diff_detail,
        )
    )


def _diff_dto(diff: ArtifactDiff) -> DiffDTO:
    return DiffDTO(
        node_id=diff.node_id,
        text=diff.text,
        run_id=diff.run_id,
        attempt_number=diff.attempt_number,
        base_sha=diff.base_sha,
        tree_sha=diff.tree_sha,
        package_digest=diff.package_digest,
        receipt_digest=diff.receipt_digest,
        verification=diff.verification,
        files=[
            ArtifactFileDTO(
                path=entry.path,
                change=entry.change,
                mode=entry.mode,
                digest=entry.digest,
                byte_length=entry.byte_length,
                text=entry.text,
                text_availability=entry.text_availability,
                detail=entry.detail,
            )
            for entry in diff.files
        ],
        availability=_availability_dtos(diff.availability),
    )


def get_diff(
    store: _Store, run_id: str, node_id: str, project_root: Path
) -> DiffDTO | None:
    """The change set of this run's sealed artifact, or nothing.

    ``node_id`` must be the sealed artifact's own node — the ledger position
    the receipt covers through. Any other node is refused rather than answered
    with the nearest available diff: the guarantee this route makes is that a
    diff it returns is the artifact, and a diff bound to something else would
    quietly break it.

    Nothing is returned when the receipt no longer verifies or the preserved
    package no longer re-hashes to the digest it was sealed with. Evidence that
    stopped standing is not evidence, and showing it anyway is the failure this
    whole path exists to prevent.
    """
    receipt = _receipt_for(store, run_id, None)
    if receipt is None or node_id != artifact_node_id(receipt):
        return None
    if _seal_detail(store, receipt):
        return None
    diff, _ = _load_artifact_diff(receipt, project_root)
    return None if diff is None else _diff_dto(diff)


# --------------------------------------------------------------------------- #
# The journal tail (I17)
# --------------------------------------------------------------------------- #


def list_flight_events(
    store: _Store,
    run_id: str,
    *,
    after_seq: int = 0,
    attempt_number: int | None = None,
) -> list[FlightEventDTO]:
    """One run's journal, strictly after ledger position *after_seq*.

    The cursor is the ``audit_events`` row id, not an offset into a list and
    not a clock: it survives a server restart, a reconnect and a reordering of
    anything else, so a client that stopped at ``n`` resumes at exactly the
    next fact rather than replaying from the top or guessing.

    The diff dimension is deliberately not resolved here — a tail is polled,
    and re-opening the sealed package on every poll to answer a question the
    caller did not ask would make the cheap read the expensive one.
    """
    timeline = get_flight_timeline(store, run_id, attempt_number=attempt_number)
    return [event for event in timeline.events if event.seq > after_seq]


# --------------------------------------------------------------------------- #
# The protected prompt read (I17)
# --------------------------------------------------------------------------- #

_PURGED = "retention removed the retained bytes"


def protected_prompt_root(config: Any, fallback: Path | str) -> Path:
    """Where this project's exact compiled prompts are retained.

    Anchored on the configured log directory, which is the anchor
    :func:`kew.dispatch_sandbox.protected_prompt_root` retains them under — so
    a read resolves the bytes the dispatch that wrote them actually wrote, not
    a same-named tree under whatever directory the server was started in.
    *fallback* covers an embedded caller with no dispatch config wired.
    """
    log_dir = getattr(getattr(config, "logging", None), "dir", None)
    return protected_root(Path(log_dir).resolve() if log_dir else Path(fallback))


def _prompt_row(store: _Store, run_id: str, attempt_number: int | None) -> Any | None:
    """The normalized artifact row for this attempt's prompt, or None."""
    listing = _capability(store, "list_artifacts")
    if listing is None:
        return None
    rows = [
        row
        for row in listing(run_id=run_id, kind=PROMPT_ARTIFACT_KIND)
        if attempt_number is None or row.attempt_number == attempt_number
    ]
    return rows[-1] if rows else None


def _reveal(row: Any, prompt_root: Path | str) -> tuple[str | None, str, str]:
    """``(text, availability, detail)`` — the bytes, or why they are not there.

    Every failure to read is an ``unavailable`` with a reason attached rather
    than an exception into a page render or, worse, a partial answer: bytes
    that no longer hash to the digest they were retained under are not the
    prompt, and showing them anyway is the substitution this route refuses.
    """
    if not row.available:
        return None, UNAVAILABLE, row.purge_reason or _PURGED
    try:
        text = read_prompt(
            prompt_root,
            run_id=row.run_id,
            attempt_number=row.attempt_number,
            expect_digest=row.digest,
        )
    except SensitivePromptError as exc:
        return None, UNAVAILABLE, str(exc)
    return text, AVAILABLE, ""


def get_run_prompt(
    store: _Store,
    run_id: str,
    *,
    prompt_root: Path | str,
    attempt_number: int | None = None,
) -> ProtectedPromptDTO | None:
    """The exact compiled prompt for one attempt — the only read that has it.

    ``None`` means this run indexed no prompt at all, which the caller turns
    into a 404. A run that indexed one and can no longer produce the bytes is
    NOT None: it comes back with its digest, byte length and purge provenance
    intact and ``text`` empty, because "retention removed it" and "there was
    never one" are different answers.
    """
    row = _prompt_row(store, run_id, attempt_number)
    if row is None:
        return None
    text, availability, detail = _reveal(row, prompt_root)
    return ProtectedPromptDTO(
        run_id=row.run_id,
        attempt_number=row.attempt_number,
        artifact_kind=row.kind,
        digest=row.digest,
        byte_length=row.byte_length,
        storage=row.storage,
        availability=availability,
        detail=detail,
        created_at=row.created_at,
        terminal_at=row.terminal_at,
        expires_at=row.expires_at,
        purged_at=row.purged_at,
        purge_reason=row.purge_reason or "",
        text=text,
    )
