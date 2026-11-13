"""Transport DTOs (Pydantic v2) — separate from storage models (run_store)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from kew.motion import Vocabulary, is_alive, motion_of


def _utc_iso(value: Any) -> Any:
    """One wire format for a timestamp: ISO-8601 UTC carrying its offset.

    The console renders every timestamp in the viewer's zone, which it can
    only do from an instant — and a naive string is not one. JavaScript reads
    an offset-less ISO date-time as the *viewer's* wall clock, so a row
    written before this store was timezone-aware (``_iso_to_dt`` in
    run_store.py still carries that backward-compat path) would be shifted by
    whatever offset the reader happens to sit at, silently and differently per
    reader (#2205).

    An offset that is already there is preserved as the same instant in UTC. A
    string this cannot parse is passed through untouched rather than dropped:
    the field is declared ``str`` and the surfaces that render it already have
    an answer for a timestamp they cannot read ("—"), which is a louder
    failure than a field that went missing.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


#: A timestamp field normalized to ISO-8601 UTC with an offset on the way out.
UtcIso = Annotated[str, BeforeValidator(_utc_iso)]
#: The same, for a field that may legitimately have no timestamp at all.
UtcIsoOrNone = Annotated[str | None, BeforeValidator(_utc_iso)]


class MotionDTO(BaseModel):
    """``motion`` and ``alive``, for a DTO that carries a ``status`` (#2305).

    One motion vocabulary serves every surface that animates (``kew/motion.py``):
    a thing is ``executing`` (kew is running code against it NOW — the only
    motion the console animates), ``waiting_kew``, ``waiting_human`` or
    ``settled``. ``alive`` is ``motion != "settled"``, served so a caller asking
    "is this an open concern" need not know which of the three non-terminal
    words applies.

    Both fields DERIVE from ``status`` unless the builder passes them, so a DTO
    can never be constructed claiming a motion its status contradicts — the
    failure mode a plain default has, where a builder nobody updated quietly
    reports every row as settled and the surface animates nothing. A builder
    that holds the extra fact the status cannot carry — a pending run's owner
    liveness, a pursuing loop's staleness — passes the refined motion instead
    (``run_motion`` / ``loop_motion``).

    ``motion_vocabulary`` is which status vocabulary this DTO's ``status`` is
    spelled in, and it matters for exactly one word: ``queued``, which a node
    wears while a PERSON must arm it and a run wears (display-only) while the
    dispatcher has yet to pick it up — see ``RUN_MOTION_OF`` in kew/motion.py.
    """

    #: Overridden per DTO; see the class docstring.
    motion_vocabulary: ClassVar[Vocabulary] = "epic"

    motion: str = ""
    alive: bool = True

    @model_validator(mode="after")
    def _derive_motion(self) -> MotionDTO:
        if "motion" not in self.model_fields_set:
            self.motion = motion_of(
                getattr(self, "status", None), vocabulary=self.motion_vocabulary
            )
        if "alive" not in self.model_fields_set:
            self.alive = is_alive(self.motion)
        return self


class SandboxPostureDTO(BaseModel):
    """Recorded effective security boundary for one run."""

    posture: str = "not recorded"
    filesystem_boundary: str = "not recorded"
    network_boundary: str = "not recorded"
    egress_verification_basis: str | None = None
    credential_names: list[str] = Field(default_factory=list)
    command_guard: str = "not recorded"
    admission: str = "not recorded"
    reason: str | None = None


#: Whose decision a :class:`RunVerdictDTO` carries.
#:
#: ``"run"`` — THIS run attested it. A governed review emits its verdict under
#: its own run id, so a review run's page shows the round it actually judged.
#:
#: ``"item"`` — no verdict names this run, and the reading is the latest one
#: standing over the work item. For an implementation run that is the useful
#: answer ("which review decision now stands over the branch I pushed"), but it
#: may have been recorded for a LATER attempt, so the surface must say whose it
#: is rather than print its round and ``head_sha`` as this run's own evidence.
VERDICT_SCOPE_RUN = "run"
VERDICT_SCOPE_ITEM = "item"


class RunVerdictDTO(BaseModel):
    """One attested governed-review verdict, as a run page reads it.

    Read from the ``adversarial_review_verdict`` audit event — the attested
    fact — rather than from the ``review_rounds`` table, which is operational
    state and carries no reviewer model. Null fields are the honest answer for
    a degenerate round (a refusal, a DEGRADED verdict) that produced no
    reviewer output, never a guess.

    ``scope`` says WHOSE verdict it is (:data:`VERDICT_SCOPE_RUN` /
    :data:`VERDICT_SCOPE_ITEM`). Without it an earlier attempt's run page
    reported a later attempt's round and reviewed head as its own — the
    cross-attempt confusion #2300 fixed on the epic's Gantt lane by naming the
    attempt a bar links to.
    """

    round: int
    verdict: str
    head_sha: str | None = None
    reviewer_model: str | None = None
    ts: UtcIsoOrNone = None
    scope: str = VERDICT_SCOPE_ITEM


class RunDTO(MotionDTO):
    # A run's `status` is a RunStatus, or one of the display-only words the
    # console refines it into (`displayStatus` in kpi.ts): `queued` there is the
    # dispatcher's own next turn, not a person's.
    motion_vocabulary: ClassVar[Vocabulary] = "run"

    run_id: str
    issue_num: int
    title: str
    runner: str
    model: str
    status: str
    cost_usd: float
    # Execution channel for this run's cost. "local" = free/local inference
    # (cost_usd is API-equivalent only); "hosted" = a hosted provider, whose
    # real cost depends on the account billing mode (see MetaDTO).
    channel: str = "hosted"
    started_at: str | None = None
    finished_at: str | None = None
    # Flight-recorder columns (F3a) — additive, optional/defaulted so no
    # existing caller breaks. Sourced from the recorded run_store row.
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    diff_additions: int | None = None
    diff_deletions: int | None = None
    diff_files: int | None = None
    review_cycles: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    attempt_number: int = 1
    error_category: str = ""
    complexity: str = ""
    branch: str = ""
    # The stage this run's recorded kind executes, and the detail line under it
    # (FUI-13). `stage` is one of kew.service.runs.RUN_STAGES; "unknown" is the
    # honest answer for a kind this vocabulary has no word for, never a guess.
    # `stage_detail` is the dot-separated second line ("round 1 · PR #2059"),
    # empty when nothing recorded qualifies it.
    stage: str = "unknown"
    stage_detail: str = ""
    posture: str = "not recorded"
    filesystem_boundary: str = "not recorded"
    network_boundary: str = "not recorded"
    egress_verification_basis: str | None = None
    credential_names: list[str] = Field(default_factory=list)
    command_guard: str = "not recorded"
    admission: str = "not recorded"
    reason: str | None = None
    # C3 (#2300) — the recorded columns the run page reads, projected straight
    # off the row. Every one is additive and defaulted, so a narrow read double
    # or a row that predates the column answers "" / None rather than a guess.
    #
    # `model` is what kew ROUTED to and `reported_model` is what the runner
    # said it used; the run page shows both because a substitution is only
    # visible as the difference. `planned_cost_usd` is the governor's estimate
    # against `cost_usd`'s actual, and `accounting_status` says whether that
    # actual has settled.
    run_kind: str = ""
    item_id: str | None = None
    reported_model: str | None = None
    provider: str | None = None
    transport_mode: str = ""
    planned_cost_usd: float | None = None
    accounting_status: str | None = None
    # Detail-only: filled by `get_run`, never by `list_runs`. The verdict costs
    # one indexed audit read per run, which a 200-row page may not spend.
    verdict: RunVerdictDTO | None = None


class MetaDTO(BaseModel):
    """Account-level context for framing cost figures honestly."""

    billing: str  # "metered" | "subscription"
    plan_label: str
    plan_monthly_usd: float
    version: str
    # `[serve] palette_ask` (#2518): whether the palette may offer ask mode.
    palette_ask: bool = False


class BundleDTO(BaseModel):
    """Deployed web-bundle build stamp, for a footer freshness badge.

    `built_at` is a unix timestamp (0.0 if the bundle predates stamping);
    `stale` is true when the checked-in `frontend/` source is newer than the
    deployed bundle (only detectable from a source checkout).
    """

    present: bool
    git_sha: str = ""
    built_at: float = 0.0
    stale: bool = False


class StepDTO(BaseModel):
    """One checkpoint in the live activity stream for a run.

    ``has_diff`` is retained for wire compatibility and is now always False:
    per-step diffs came from speculative git snapshots of the agent's own
    worktree, and I16 replaced them with one diff bound to the sealed artifact
    (:class:`ArtifactDiffDTO`). A step is an activity marker, not a checkpoint
    anything can be diffed against.
    """

    id: str
    parent_id: str | None
    seq: int
    ts: float
    kind: str
    label: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    has_diff: bool = False


class TimelineDTO(BaseModel):
    run_id: str
    root_id: str | None
    head_id: str | None
    nodes: list[StepDTO]


# --- Receipt-backed Flight evidence (I16) ----------------------------------
#
# The canonical projection of one run's evidence: the audit host sequence, the
# receipt prefix that seals part of it, and the change set of the preserved
# package that receipt addresses. Every field here comes from a durable,
# host-owned row or from bytes under the host's private package root — nothing
# is read out of a repository that could have moved on since the run ended.


class EvidenceAvailabilityDTO(BaseModel):
    """Whether one evidence dimension could be read, and on what basis.

    ``provenance`` distinguishes "nothing was ever recorded" (``unobserved``)
    from "something was recorded and cannot be trusted" (``error``). A surface
    must be able to say which without inventing the difference.
    """

    dimension: str
    availability: str  # "available" | "unavailable"
    provenance: str  # "observed" | "unobserved" | "error"
    detail: str = ""


class FlightEventDTO(BaseModel):
    """One event at its position in the host sequence (``audit_events.id``)."""

    seq: int
    timestamp: str
    kind: str
    origin: str  # "evidence" | "attestation"
    producer_kind: str  # "host" | "runner"
    producer_name: str
    producer_version: str
    attested_by_host: bool
    # Covered by the receipt's sealed prefix. False marks a later chained fact
    # — E5's publication, judgment and synchronization all arrive after a seal.
    sealed: bool
    terminal: bool = False
    facts: dict[str, Any] = {}


class EvidenceGapDTO(BaseModel):
    """A ledger row that claims to be evidence and could not be read as one."""

    seq: int
    event: str
    reason: str


class FlightTimelineDTO(BaseModel):
    """One run/attempt's replayed evidence, and what it could not say."""

    run_id: str
    attempt_number: int
    # Durable work-item id and, for a numeric tracker only, its number. A
    # Linear item carries ``item_id`` alone; nothing synthesizes a number.
    item_id: str | None = None
    issue_num: int | None = None
    source_tracker: str | None = None
    source_id: str | None = None
    sealed: bool
    through_seq: int | None = None
    chain_head: str | None = None
    chain_verified: bool
    # The one node a diff may be requested for, or None when nothing is sealed.
    artifact_node_id: str | None = None
    runner_telemetry: str  # "structured" | "activity_only" | "missing"
    events: list[FlightEventDTO] = []
    gaps: list[EvidenceGapDTO] = []
    availability: list[EvidenceAvailabilityDTO] = []


class ArtifactFileDTO(BaseModel):
    """One path the sealed package accounts for.

    ``change`` is ``changed`` or ``deleted`` and never ``added``: a package
    carries final content, not the base that would say whether a path existed
    before. ``text`` is the final content when this route will inline it, and
    ``text_availability`` says why when it will not.
    """

    path: str
    change: str  # "changed" | "deleted"
    mode: str = ""
    digest: str = ""
    byte_length: int = 0
    text: str | None = None
    text_availability: str = "available"
    detail: str = ""


class DiffDTO(BaseModel):
    """The change set of one sealed artifact, named by what binds it.

    ``node_id`` and ``text`` keep the shape the earlier snapshot diff had, so
    an existing client keeps working; everything else states the lineage the
    change set is bound to. A response only exists when the receipt verified
    and the preserved package re-hashed to the digest it was sealed with.
    """

    node_id: str
    text: str
    run_id: str = ""
    attempt_number: int = 0
    base_sha: str = ""
    tree_sha: str = ""
    package_digest: str = ""
    receipt_digest: str = ""
    verification: str = ""
    files: list[ArtifactFileDTO] = []
    availability: list[EvidenceAvailabilityDTO] = []


class ProtectedPromptDTO(BaseModel):
    """The one response that may carry the exact compiled prompt bytes (I17).

    Every field except ``text`` is the digest-only description that a list, a
    receipt, an export or a certificate is allowed to carry; ``text`` is filled
    in only on the dedicated protected read, and only while the bytes are still
    retained. Once retention purges them the digest, the byte length and the
    purge provenance stay behind, so a reader sees *unavailable* rather than
    *empty* — the two are different facts about the run.
    """

    run_id: str
    attempt_number: int
    artifact_kind: str
    digest: str
    byte_length: int
    storage: str  # "local_protected" | "unavailable"
    availability: str  # "available" | "unavailable"
    detail: str = ""
    created_at: str = ""
    terminal_at: str | None = None
    expires_at: str | None = None
    purged_at: str | None = None
    purge_reason: str = ""
    #: The exact retained bytes, or None when they cannot be read. Deliberately
    #: has no schema example: an example would put a prompt in the OpenAPI
    #: artifact, which is exactly the copy this whole boundary exists to refuse.
    text: str | None = None


class IterationDTO(BaseModel):
    iteration_id: str
    idx: int
    kind: str
    status: str
    cost_usd: float
    tokens: int
    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class LoopDTO(MotionDTO):
    loop_id: str
    source: str
    issue_num: int | None = None
    repo: str | None = None
    goal_text: str
    runner_name: str
    # The durable model half of the loop's route (#1111). "" for pre-v35 loops,
    # which recorded a runner but no model.
    model: str = ""
    # Derived from the runner's loop_style; NOT stored (spec R3).
    drive_mode: str  # "delegated" | "driven" | "unknown"
    status: str
    status_reason: str = ""
    needs_human: bool = False
    # Derived on read (never written): a `pursuing` loop that has not iterated
    # in 2x its cadence. Loops have no owner and no heartbeat, so this is the
    # only signal that one has stopped moving (#2297).
    stale: bool = False
    budget_usd: float | None = None
    spent_usd: float = 0.0
    wall_clock_secs: int = 0
    max_steps: int = 0
    step_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None


class LoopDetailDTO(LoopDTO):
    iterations: list[IterationDTO] = []


class LoopAuditDTO(BaseModel):
    id: int
    timestamp: str
    event: str
    actor_type: str | None = None
    actor_id: str | None = None
    loop_id: str | None = None
    summary: str = ""


class HoldDTO(BaseModel):
    issue_num: int
    estimate_usd: float | None
    basis: str


class ApprovalResultDTO(BaseModel):
    status: str  # released | denied | not_held | gh_error | unauthorized


class FleetStopResultDTO(BaseModel):
    stopped: bool
    already_stopped: bool
    runs_killed: list[dict[str, Any]]


class FleetStatusDTO(BaseModel):
    stopped: bool
    pending: int


class WaitLinkDTO(BaseModel):
    """The one link a wait's move is made through, when it is not a command.

    A merge decision has no kew command (the merge happens on the forge), so
    its move is a link: ``Open PR #2059 to merge``. The label carries no arrow;
    the console's markup adds ``↗`` to every link that leaves kew.
    """

    label: str
    href: str


class NodeAttentionDTO(BaseModel):
    """Why ONE epic node needs a person, in the judgment queue's own words.

    Composed in ``kew.service.attention`` from the same work projection and row
    wording the console's judgment queue reads, so the DAG's needs-you diamond
    and the work surface's needs-you band cannot disagree.

    ``kind`` is an open vocabulary (a reader must tolerate an unknown one):
    ``merge_decision`` (an artifact awaiting a human decision), ``approval``
    (an open spend approval hold) and ``work_exception`` (any other
    judgment-queue exception on this item) are composed from the work
    projection, and ``kew.waits``'s own kinds — ``merge_ready``, ``held``,
    ``budget_exhausted``, ``publication_held``, ``sandbox_refused`` — are
    composed from the epic's journaled node states and its open holds (#2207).
    ``url`` is the artifact to act on when there is one, never invented.

    ``action`` is what the operator does next, in the words the driver's own
    attention stream uses (``kew.waits``). Empty when the kind carries no
    single next move.

    ``command`` is that move as a line an operator can paste, with this node's
    epic and item already in it — never a template, and never scraped back out
    of ``action``. ``action`` is PROSE about the move ("kew epic unhold, which
    also restarts the review cycle", "re-check the worktree its reason names,
    then kew epic unhold"), so a surface that printed its leading words under a
    Copy button offered ``kew epic unhold`` with no arguments, which exits 2.
    The same guard the needs-you rows use fills this one
    (``kew.waits.fill_command``), and the same rule holds: "" when no kew
    command clears this wait, or when the epic and item are not both plain
    arguments.
    """

    kind: str
    reason: str
    url: str | None = None
    action: str = ""
    command: str = ""
    # The wait in one sentence, written by ``kew.service.attention.say_wait``
    # — the same call that writes ``DashboardRowDTO.sentence`` for the same
    # item, so the epic popover, the rail, Work and Monitor print one string
    # rather than four paraphrases of it (#2539).
    sentence: str = ""
    link: WaitLinkDTO | None = None
    # A merge over a merge verify that failed at the PR's current head: the
    # item's MERGE VERIFY section, which is the first move — ``link`` (the PR)
    # is the second (#3460). None on every other wait.
    verify_link: WaitLinkDTO | None = None


class EpicNodeDTO(MotionDTO):
    id: str
    title: str = ""
    status: str
    status_since: str | None = None
    url: str | None = None
    pr_url: str | None = None
    external: bool = False
    stuck: str | None = None
    # None means "no person is waited on" ONLY when the snapshot's
    # ``unavailable`` names nothing — see EpicSnapshotDTO.unavailable.
    attention: NodeAttentionDTO | None = None
    # What the driver recorded about a blocked node, verbatim; None when it
    # recorded nothing. A reader renders nothing rather than inventing a cause.
    blocked_reason: str | None = None
    # A failed node's recovery command, filled from its own stuck reason
    # (``kew.waits.failed_node_command``): retry for a plain failure, continue
    # for preserved work, "" when no command recovers it or for any other
    # status. A reader prints this, never a command inferred from a missing
    # ``attention`` — attention clears while a continuation runs (#2960).
    recovery_command: str = ""


class EpicSnapshotDTO(BaseModel):
    epic_ref: str
    tick_id: int
    tick_ts: UtcIso
    signal: str
    counts: dict[str, int]
    frontier: list[str]
    nodes: list[EpicNodeDTO]
    edges: list[tuple[str, str]]  # (src, dst)
    # Derived annotations this snapshot could NOT compose, named so a clean
    # ``attention=None`` is never mistaken for "nobody is waiting": "attention"
    # (the whole join did not happen or its projection was unreadable),
    # "attention:approval_holds" (the hold reader failed) or "attention:<item>"
    # (that one item's work facts were unreadable).
    unavailable: list[str] = []


class EpicAttentionDTO(BaseModel):
    """The live needs-you join on its own, as a stream frame.

    Attention moves for reasons the tick journal never records: serve's own
    sweep persisting a PR merge, an approval hold opening or clearing. Binding
    the streamed join to a new journal row would leave a live subscriber
    rendering the reading composed at the last tick — and the watch hub's
    latest-frame replay would then overwrite a fresh tab's detail read with it.

    Carrying ONLY the join lets a reader refresh the annotations on the
    snapshot it already holds without re-animating a tick it has already shown.
    ``by_node`` is complete for the nodes it was composed against: a node
    absent from it has no one waiting on it, subject to ``unavailable`` (same
    vocabulary and same silence-on-failure rule as
    :attr:`EpicSnapshotDTO.unavailable`).
    """

    by_node: dict[str, NodeAttentionDTO]
    unavailable: list[str] = []


class NodeRecoveryDTO(BaseModel):
    """One node's recovery state as it reads now, for :class:`EpicRecoveryDTO`."""

    stuck: str | None = None
    blocked_reason: str | None = None
    recovery_command: str = ""


class EpicRecoveryDTO(BaseModel):
    """Each node's recovery state on its own, as a stream frame (#2960).

    A node's stuck reason and recovery command move without a tick: a
    continuation claimed or abandoned by another operator rereads the journaled
    reason against the continuation history as it stands now. A snapshot frame
    is sent only for a new journal row, and the attention frame carries only
    the join, so a mounted page would keep printing the command of the moment
    it loaded. Hashed apart from the attention frame, so it is re-sent exactly
    when one of these fields moves. ``by_node`` covers every node of the
    epic's newest journaled snapshot.
    """

    by_node: dict[str, NodeRecoveryDTO]


class EpicEconomicsDTO(BaseModel):
    """Drain-scoped, and says so: window starts at the epic's first journal
    row. label is part of the contract so the UI can't silently relabel."""

    window_start: str | None  # None = pre-journal epic
    drain_total_usd: float
    node_drain_usd: dict[str, float]
    label: str = "drain spend"


class EpicHeartbeatDTO(BaseModel):
    last_tick_started_at: UtcIsoOrNone
    last_tick_ts: UtcIsoOrNone
    last_tick_duration_s: float | None
    tick_count: int
    # The cadence this heartbeat is thresholded against: a poller's own
    # `--interval`, else the operator's configured expectation for an
    # externally-driven epic. `cadence_source` below says which (#2297).
    interval_s: float | None
    last_signal: str | None
    change_count: int  # journaled-change count (count_epic_ticks)
    # Which control plane this driver reads ("github"/"linear"), off the durable
    # epic_driver_state.tracker column; None on a row no tick has refreshed
    # since the v33 migration. Defaulted so a client predating it still parses.
    tracker: str | None = None
    # The liveness READING for this driver: "live" | "gone" | "unknown".
    # "unknown" is not "not gone" — it means no cadence was recorded to
    # threshold the heartbeat against (#2297).
    driver_status: str = "unknown"
    # Where the thresholding cadence came from: "recorded" | "expected" | "none"
    # — which is also the provenance of `interval_s` above, since that field
    # carries the cadence this reading was taken against (#2297).
    cadence_source: str = "none"
    # The epic's tick-ATTEMPT stamp (#2304): a token that moves whenever a row
    # of `epic_tick_log` is opened or closed. `tick_count` above cannot serve:
    # it is written past the lock, the preflight and the non-empty-graph guard,
    # so a driver that is locked out or failing preflight moves it never — and
    # those attempts are exactly what a tick strip is for. Readers re-read a
    # strip when this changes. Defaulted so a client predating it still parses.
    tick_attempt_stamp: str = ""


class EpicSummaryDTO(BaseModel):
    epic_ref: str
    signal: str | None
    counts: dict[str, int]
    drain_total_usd: float
    last_tick_ts: UtcIsoOrNone
    # The tick's START, written before any work is done, where `last_tick_ts`
    # only lands once a tick FINISHES. This is the field `driver_liveness`
    # thresholds (kew.service.epics), and the Epics list needs the same one:
    # read off the finish stamp, a driver whose tick runs longer than its own
    # interval draws "no heartbeat" in the list while the server, the dashboard
    # and the epic page all call it live. Defaulted so a client predating the
    # field still parses.
    last_tick_started_at: UtcIsoOrNone = None
    # The rest of the heartbeat the epic page reads (#2844 N18), so the row and
    # the page take ONE freshness reading of one driver. The page widens its
    # window with the last tick's duration, and takes the newest tick ATTEMPT's
    # instants over the driver state's when the log is ahead — an attempt that
    # changed nothing or was turned away before the tick began moves no
    # heartbeat field. Served on every row, so the reading does not depend on
    # which rows' strips the list fetched. Defaulted so a client predating the
    # fields still parses.
    last_tick_duration_s: float | None = None
    last_attempt_started_at: UtcIsoOrNone = None
    last_attempt_finished_at: UtcIsoOrNone = None
    # That attempt's outcome (#2882), null while it runs: only an attempt
    # that reached a tick may advance the heartbeat, and a failed, locked-out
    # or empty-graph attempt is a refusal, not a beat.
    last_attempt_outcome: str | None = None
    # Whether that attempt got past ``begin_epic_tick`` (#2882): an OPEN
    # attempt is a heartbeat only once it has, since the row opens before the
    # lock, the preflight and the graph build.
    last_attempt_reached_tick: bool = False
    interval_s: float | None
    change_count: int
    label: str = "drain spend"  # mirrors EpicEconomicsDTO.label (see there)
    # The operator's retirement decision (#2206), derived from the latest
    # attested `epic_archived`/`epic_unarchived` event (kew.epic_archive). The
    # Epics list splits Active from Finished/Archived on `archived`; the other
    # two say who retired it and why, so the shelf reads without a second
    # request. Defaulted so a client predating the field still parses.
    archived: bool = False
    archived_at: str | None = None
    archive_reason: str = ""
    # Same tick-ATTEMPT stamp as `EpicHeartbeatDTO.tick_attempt_stamp` (see
    # there): what the roster re-reads a row's tick strip on. Read for the
    # whole roster in one query, and independent of the heartbeat row — an
    # epic with attempts but no driver state still stamps.
    tick_attempt_stamp: str = ""
    # Same liveness READING as `EpicHeartbeatDTO.driver_status` — "live" |
    # "gone" | "unknown" — which the roster already composes per row and used
    # to drop on the floor. The row's tick strip needs it: an OPEN attempt row
    # carries only its start instant, so a strip bounding "running" on that age
    # alone draws a tick that legitimately outlasts the bound as quiet and
    # words it "abandoned" — while this reading, thresholded off the newest
    # heartbeat the driver writes (the lock's own included, the one
    # `_lock_keepalive` beats MID-tick), still says live (#2304 review round 5).
    driver_status: str = "unknown"


class EpicTickDTO(BaseModel):
    id: int
    tick_ts: UtcIso
    snapshot: EpicSnapshotDTO
    actions: dict[str, Any]  # merged entries enriched: [{"id","approved_by"}]
    economics: EpicEconomicsDTO | None = None  # frame-windowed (<= tick_ts)


class EpicTickStripDTO(BaseModel):
    """One epic-driver tick ATTEMPT, as the tick strip draws it (#2304).

    Deliberately not an ``EpicTickDTO``: that one carries a whole DAG snapshot
    and exists for the journal reader. A strip of two hundred ticks needs the
    outcome, the counts and two instants — nothing a snapshot would have to be
    parsed for. ``ts_finished``/``outcome`` are null while the tick is running,
    which is the one state the strip paints live.
    """

    id: int
    ts_started: UtcIso
    ts_finished: UtcIso | None = None
    outcome: str | None = None
    signal: str | None = None
    n_dispatched: int = 0
    n_merged: int = 0
    n_held: int = 0
    n_stuck: int = 0
    journal_tick_id: int | None = None
    error_class: str | None = None
    # Past ``begin_epic_tick`` (#2882): an open attempt without it has not
    # begun a tick, so it is no heartbeat.
    reached_tick: bool = False


class EpicNodeRunDTO(BaseModel):
    run_id: str
    status: str
    model: str
    runner: str
    cost_usd: float
    total_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    pr_url: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class EpicNodeDetailDTO(BaseModel):
    id: str
    runs: list[EpicNodeRunDTO]
    lifetime_usd: float  # labeled "lifetime issue spend" in UI
    drain_usd: float


class EpicDetailDTO(BaseModel):
    epic_ref: str
    snapshot: EpicSnapshotDTO | None  # None = pre-journal epic (honest empty state)
    heartbeat: EpicHeartbeatDTO | None
    economics: EpicEconomicsDTO


class EpicTimelineSpanDTO(BaseModel):
    """The window the timeline draws. ``start`` is the earliest band or run
    the epic has; None when nothing has happened yet, which is honest rather
    than a zero-width span anchored at ``now``."""

    start: UtcIsoOrNone = None
    end: UtcIso


class EpicTimelineBandDTO(BaseModel):
    """One stretch a node spent in one status.

    Built from the ``epic_node_status`` audit events, so a band opens at the
    transition that announced the status and closes at the next one. ``to``
    is None on the last band — the node is in that status still, and the
    reader draws it to ``now`` rather than inventing an end the ledger never
    recorded. The wire name is ``from``, a Python keyword, hence the alias.
    """

    model_config = ConfigDict(populate_by_name=True)

    status: str
    # Named for the wire on both legs, and ``from_`` in Python. A bare
    # ``alias=`` would make the keyword the only way to construct one, and
    # ``from=`` is not a thing anybody can type.
    from_: UtcIso = Field(validation_alias="from", serialization_alias="from")
    to: UtcIsoOrNone = None


class EpicTimelineRunDTO(BaseModel):
    """One run on a node's lane. ``kind`` is the stored ``run_kind``
    (``dispatch``, ``adversarial_review``, ``review_remediation``,
    ``resolve``, ...) and a None ``finished_at`` means still in flight."""

    run_id: str
    kind: str
    status: str
    started_at: UtcIsoOrNone = None
    finished_at: UtcIsoOrNone = None
    cost_usd: float = 0.0
    # Which attempt this bar is (C3/#2300). The Gantt links a bar to the run
    # page as `?attempt=`, and a run page with no attempt named selects the
    # latest — which is the wrong evidence for an earlier bar on the lane.
    attempt_number: int = 1


class EpicTimelineRoundDTO(BaseModel):
    """One recorded governed-review verdict, as a point on the node's lane.

    ``verdict`` is the round's ``effective_verdict`` — the driver's
    ``READY_WITH_RESIDUALS`` acceptance when the stop rule accepted a
    low/medium-only NEEDS_CHANGES round (#2151), the reviewer's verdict
    otherwise — so a lane never disagrees with its own merge marker.
    """

    round: int
    verdict: str
    recorded_at: UtcIso


class EpicTimelineNodeDTO(BaseModel):
    id: str
    title: str = ""
    bands: list[EpicTimelineBandDTO] = []
    runs: list[EpicTimelineRunDTO] = []
    rounds: list[EpicTimelineRoundDTO] = []
    # Newest non-null runs.pr_merged_at for the node; None = never merged.
    merged_at: UtcIsoOrNone = None


class EpicTimelineDTO(BaseModel):
    """Where an epic's time went, per node (#2270).

    Read-only and ledger-only: bands from the audit chain, runs and the merge
    from ``runs``, verdicts from ``review_rounds``. External boundary nodes
    are left out — they are another epic's work and have no lane here.
    ``nodes`` is ordered by first activity, nodes with none last in snapshot
    order, so the chart reads top-down as the epic actually unfolded.
    """

    epic_ref: str
    now: UtcIso
    span: EpicTimelineSpanDTO
    nodes: list[EpicTimelineNodeDTO] = []


class ComplexityCellDTO(BaseModel):
    complexity: str
    runs: int
    opened: int
    merged: int
    review_total: int
    review_n: int
    spend: float
    # Planned/actual cost pair (#1092). ``planned_spend`` is the token x
    # pricing-bound figure dispatch planned against, ``paired_spend`` the actual
    # spend of the same ``planned_n`` runs. Runs predating the column record no
    # bound and are excluded from all three, so the ratio stays honest.
    planned_spend: float = 0.0
    paired_spend: float = 0.0
    planned_n: int = 0


class ModelScorecardRowDTO(BaseModel):
    model: str
    # Execution channel for this model's spend, computed on the backend via
    # ``kew.pricing.is_free_model`` (N11). "local" = free/local inference
    # (ollama/, lemonade/); "hosted" = a hosted provider. The frontend reads
    # this instead of re-deriving the free-model heuristic from the model id.
    channel: str = "hosted"
    # Lifecycle status from the model-eol snapshot: "ok", "untracked", "watch",
    # "scheduled", "retiring", or "retired". Empty string when the model is not
    # present in the snapshot or the snapshot is not loaded.
    lifecycle_status: str = ""
    runs: int
    opened: int
    merged: int
    review_total: int
    review_n: int
    spend: float
    planned_spend: float = 0.0
    paired_spend: float = 0.0
    planned_n: int = 0
    by_complexity: list[ComplexityCellDTO] = []


class ModelScorecardDTO(BaseModel):
    days: int
    rows: list[ModelScorecardRowDTO]


class SpendCellDTO(BaseModel):
    window_days: int
    spent: float
    cap: float | None
    # Governance split of ``spent`` (additive; invariant: spent == agentic +
    # interactive). ``agentic`` is the ENFORCED figure: the cost of every run kew
    # records, reviews included, over the governor's window (today's UTC day /
    # that day and the six before it),
    # computed identically to ``budget.check_budget`` — this is what the web bar
    # must compare against ``cap`` (N5). ``interactive`` is Claude Code OTel
    # interactive_usage cost over the same window: observed only, never gated.
    agentic: float = 0.0
    interactive: float = 0.0


class SpendWindowsDTO(BaseModel):
    daily: SpendCellDTO
    weekly: SpendCellDTO


class OutcomeQuantilesDTO(BaseModel):
    """A distribution printed as the three readings a reader acts on.

    None, not 0.0, when the week has no samples: a zero median hour reads as
    "instant", which is the opposite of "nothing to measure".
    """

    p50: float | None = None
    p90: float | None = None
    max: float | None = None


class OutcomeRoundsDTO(BaseModel):
    """Review rounds to READY. The worst case matters as much as the middle,
    so ``max`` is served beside ``p50`` and nothing else."""

    p50: float | None = None
    max: int | None = None


class OutcomeCostDTO(BaseModel):
    """Cost per merged PR. ``mean`` is the week's attributed spend over its
    merge count — the figure a budget is read against — and ``p50`` the
    typical PR, which a handful of expensive epics otherwise hides."""

    mean: float | None = None
    p50: float | None = None


class OutcomeWeekDTO(BaseModel):
    """One UTC week of delivered outcomes, Monday-first.

    ``merged_main`` is the only field not from the ledger: it is a local
    ``git log`` read of the configured base branch's remote ref
    (``kew.merged_main``, ``origin/main`` unless ``branches.base`` says
    otherwise) and is None when that read was unavailable, which is why the
    console can say *why* a chart is empty rather than drawing a zero.
    """

    week_start: str
    """ISO date of the week's Monday, UTC."""
    merged_main: int | None = None
    """Distinct PRs squash-merged into the base branch that week, or None when
    the local clone could not be read."""
    merged_kew_touched: int = 0
    """Distinct merged PRs with any kew run against them — including review,
    CI-fix and rebase runs on a PR a person opened."""
    merged_kew_authored: int = 0
    """Of those, the ones an implementation run of kew's own opened."""
    ready_items: int = 0
    """The sample size behind ``ready_hours`` and ``rounds_to_ready``."""
    ready_hours: OutcomeQuantilesDTO = Field(default_factory=OutcomeQuantilesDTO)
    """Dispatch -> first READY verdict, in hours."""
    rounds_to_ready: OutcomeRoundsDTO = Field(default_factory=OutcomeRoundsDTO)
    cost_per_merged: OutcomeCostDTO = Field(default_factory=OutcomeCostDTO)
    spend_total: float = 0.0
    """Ledger spend on runs DISPATCHED in the week (not the cost of its
    merges, which is ``cost_per_merged``)."""
    spend_unattributed: float = 0.0
    """The part of ``spend_total`` carrying no PR — printed beside a
    cost-per-merged figure so the division is never read as the whole bill."""
    nodes_done: int = 0
    """Distinct epic nodes that reached ``done`` — the closed-issue proxy."""
    pr_sync_observed_at: UtcIsoOrNone = None
    """When the newest merged PR snapshot in the week was written. The merge
    counts are no fresher than this."""
    partial: bool = False
    """True for the week in progress: its counts are not a full seven days."""


class OutcomeReportDTO(BaseModel):
    """The weekly outcome scorecard: one row per week, oldest first."""

    weeks: list[OutcomeWeekDTO]
    min_sample: int
    """The sample size below which a week is too thin to read. The console
    greys those points rather than hiding them — a thin week is a fact."""
    merged_main_head: str | None = None
    """Short id of the base branch tip the denominator was read at."""
    merged_main_unavailable: str | None = None
    """Why ``merged_main`` is None on every week, when it is."""


class AuditEventDTO(BaseModel):
    """One hash-chained audit event, projected for the web surface."""

    id: int
    timestamp: UtcIso
    event: str
    issue_num: int | None = None
    item_id: str | None = None  # durable work-item key, when the event has one
    run_id: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    summary: str | None = None  # compact payload excerpt, or None
    # Epic this event belongs to, read from the FULL payload — `summary` is
    # size-capped, so a long payload would otherwise hide the attribution.
    epic_ref: str | None = None


class AuditListDTO(BaseModel):
    events: list[AuditEventDTO]
    next_after: int | None = None  # cursor: pass as ?after= for the next page


class RunAuditEventDTO(BaseModel):
    """One ledger event of one run, as the run page's event list reads it.

    Narrower than :class:`AuditEventDTO` and carrying the FULL payload rather
    than a size-capped excerpt: this feed is already scoped to a single run, so
    the envelope fields that exist to attribute an event across the whole
    ledger (``issue_num``, ``actor_id``, ``epic_ref``) are not what the reader
    is asking, and a truncated payload is the one thing a per-run event list
    cannot work from.
    """

    seq: int  # the ledger id — also the forward cursor
    ts: UtcIso
    event: str
    actor_type: str | None = None
    item_id: str | None = None
    payload: dict[str, Any] | None = None


class RunAuditPageDTO(BaseModel):
    """One forward page of a run's ledger events, oldest first.

    ``next_seq`` is where to RESUME, not a has-more flag: it is the last
    returned event's seq, or the ``after_seq`` the page was asked with when
    nothing came back. A live reader therefore polls the same cursor forever
    without a special case, and a client paging to the end stops when
    ``events`` comes back empty.
    """

    run_id: str
    events: list[RunAuditEventDTO]
    next_seq: int


class AuditHistogramBucketDTO(BaseModel):
    """One fixed-width time bucket: when it starts, and counts per event name."""

    start: UtcIso
    counts: dict[str, int]


class AuditHistogramDTO(BaseModel):
    """Event counts over ``[since, until)`` in ``bucket_s``-wide buckets.

    Buckets are dense — every bucket in the window is present, empty ones with
    no counts — so a client can chart the series without reindexing. The bucket
    counts sum to ``total``, which equals what a paged read over the same
    filters would return.
    """

    since: UtcIso
    until: UtcIso
    bucket_s: int
    buckets: list[AuditHistogramBucketDTO]
    total: int


class AuditStatusDTO(BaseModel):
    """Result of the same chain verification `kew audit verify` runs."""

    events: int
    verified: bool
    anchors_matched: int
    anchors_total: int


# --- Canonical work projection (I02) ---------------------------------------
#
# One tracker-neutral view of one work item, composed (never persisted) from
# the normalized facts each later slice owns. The six dimensions below are
# deliberately ORTHOGONAL: `RunStatus` stays execution state and nothing else,
# so publication, judgment, source synchronization, and remote availability
# each get their own field rather than another value in that enum.


class RemoteObservationDTO(BaseModel):
    """When and how one remote-derived dimension was last read.

    ``availability`` is the honesty gate this type exists for. ``unavailable``
    means the read did not happen or failed, and every dimension resting on it
    stays unknown — an empty/absent result is only ever reported under
    ``available``, i.e. a successful read that genuinely found nothing.
    """

    dimension: str
    availability: str  # "available" | "stale" | "unavailable"
    provenance: str  # "observed" | "cached" | "unobserved" | "error"
    observed_at: str | None = None
    detail: str | None = None


class WorkIdentityDTO(BaseModel):
    """Who this work is — joinable without a title, path, or numeric shim."""

    source_tracker: str | None = None
    item_id: str | None = None
    item_ref: str = ""  # display form: "#42" for GitHub, "SFD-12" bare
    delivery_repo: str | None = None
    epic_ref: str | None = None
    node_id: str | None = None
    run_id: str | None = None
    attempt_number: int = 0


class WorkSourceDTO(BaseModel):
    """Dimension 1 — the tracker item the work exists for."""

    tracker: str | None = None
    item_state: str = "unknown"  # "open" | "closed" | "unknown"
    # "synchronized" | "pending" | "not_applicable" | "unknown"
    synchronization: str = "unknown"
    observation: RemoteObservationDTO


class WorkRouteDTO(BaseModel):
    """Dimension 2 — the lane the latest attempt ran on.

    ``provenance`` is ``declared`` here: these are the configured/recorded
    route facts. Enforced/observed/measured provenance belongs to the resolved
    route profile E2 owns; this dimension must not claim it early.
    """

    runner: str = ""
    model: str = ""
    reported_model: str | None = None
    containment: str = "unknown"  # "host" | "contained" | "unknown"
    # WHERE the attempt ran (#2398). ``host`` is the fleet host — "local" for
    # the operator machine, "gcp" for a Compute Engine instance — and
    # ``instance`` names the machine within it, "" for local, which is one
    # machine. Two fields rather than one rendered string so the console can
    # group by host without parsing. Declared, like everything in this
    # dimension: it is what the ledger recorded at admission, not a probe of
    # the VM.
    host: str = ""
    instance: str = ""
    provenance: str = "declared"
    # The route CLAIM, kept separate from the route itself: ``model`` is what
    # the lane declared, ``reported_model`` is what the runner said it served.
    # "honored" only when both were read and agree; "substituted" when they
    # were both read and differ; "unverified" when nothing came back to check.
    claim: str = "unverified"  # "honored" | "substituted" | "unverified"


class WorkExecutionDTO(BaseModel):
    """Dimension 3 — execution state, carrying the raw ``RunStatus`` verbatim."""

    state: str  # "not_started" | "in_flight" | "completed" | "terminal"
    run_status: str | None = None
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error_category: str = ""


class WorkArtifactDTO(BaseModel):
    """Dimension 4 — what the attempt preserved or published.

    ``orphan`` marks a published artifact with no successful run behind it
    (an open agent PR the run never claimed): the artifact is real, so nothing
    may dispatch on top of it.
    """

    state: str  # "absent" | "preserved" | "published"
    publication: str  # "not_required" | "pending" | "published" | "unknown"
    branch: str | None = None
    pr_url: str | None = None
    orphan: bool = False


class WorkJudgmentDTO(BaseModel):
    """Dimension 5 — the human decision on the artifact."""

    # "not_applicable" | "awaiting_human_decision" | "under_review"
    # | "between_review_rounds" | "changes_requested" | "accepted"
    # | "declined" | "unknown"
    state: str
    observation: RemoteObservationDTO
    decided_at: str | None = None


class WorkOutcomeDTO(BaseModel):
    """Dimension 6 — where the work ended up."""

    state: str  # "in_progress" | "delivered" | "abandoned" | "failed" | "unknown"
    detail: str = ""


class SafestActionDTO(BaseModel):
    """The ONE next action, derived centrally so no surface invents its own.

    ``blocked_by`` is set when the safest action is only "find out": a
    dimension the decision depends on could not be read, so recommending the
    optimistic action would be a guess.
    """

    action: str
    reason: str
    blocked_by: str | None = None


# --- Exception, pressure and availability composition (I19) -----------------
#
# I02 gave one work item six orthogonal dimensions. I19 adds the four that
# later slices made durable — integration health, boundary readiness, the
# zero-diff judgment, and the human-review pressure the delivery repository is
# under — plus ONE canonical name for what is off-normal (``WorkExceptionDTO``).
#
# The dimensions stay orthogonal on purpose: an exception is *derived* from
# them and never replaces them, so a surface that wants "why" still reads the
# dimension rather than parsing a label.


class WorkIntegrationDTO(BaseModel):
    """Dimension 7 — whether a published artifact can actually land.

    Separate from ``WorkJudgmentDTO`` because a person approving a change and
    the change being mergeable are different facts: an approved pull request
    with a red build or a conflicted tree is still not landable, and reporting
    either as "accepted" is how work quietly stops moving.
    """

    # "passing" | "failing" | "pending" | "none" | "unknown"
    checks: str = "unknown"
    # "clean" | "conflicted" | "unknown" | "not_applicable"
    mergeability: str = "unknown"
    # "approved" | "changes_requested" | "review_required" | "none" | "unknown"
    review_decision: str = "unknown"
    observation: RemoteObservationDTO


class WorkReadinessDTO(BaseModel):
    """Dimension 8 — is the exact route boundary proven usable right now?

    ``state`` is about *evidence*, never optimism: ``unproven`` means nobody
    probed this exact fingerprint, which is not the same as ``refuted`` (a
    probe proved it broken) and emphatically not the same as ``proven``.
    """

    # "proven" | "refuted" | "expired" | "unproven" | "unknown"
    state: str = "unknown"
    fingerprint: str | None = None
    evidence_ref: str | None = None
    detail: str = ""
    observation: RemoteObservationDTO


class WorkCertificateDTO(BaseModel):
    """Dimension 12 — does a journey certificate stand behind this work?

    The third orthogonal truth, beside the configured capability profile and
    execution validity. It is deliberately incapable of implying either: a
    passing live probe never produces a certificate, and a standing
    certificate never proves current reachability.

    ``state`` is about *evidence that was actually read*. ``uncertified`` is
    only ever reported when the carrier was read successfully and genuinely
    holds nothing for this subject; an unfetched, unreadable, stale, or
    invalid carrier is ``unknown`` with a non-available ``observation``, which
    surfaces render as "certificate evidence unavailable" rather than
    converting to ``uncertified`` or preserving an unverified ``certified``.
    """

    # "certified" | "recertification_due" | "suspended"
    # | "uncertified" | "unknown"
    state: str = "unknown"
    certified_at: str | None = None
    lane: str | None = None
    evidence_ref: str | None = None
    detail: str = ""
    observation: RemoteObservationDTO


class WorkNoChangeDTO(BaseModel):
    """Dimension 9 — the zero-diff judgment and everything it still owes.

    ``state`` collapses the I10-I12 lifecycle into what a surface must act on;
    ``judgment_state`` keeps the raw lifecycle value verbatim beside it, the
    same way ``WorkExecutionDTO`` keeps ``run_status``.
    """

    # "not_applicable" | "pending" | "authorized" | "synchronizing"
    # | "sync_failed" | "applying_effects" | "completed" | "rejected"
    state: str = "not_applicable"
    judgment_state: str = ""
    policy: str = ""
    reasons: list[str] = []
    sync_outstanding: int = 0
    sync_failed: int = 0
    effects_outstanding: int = 0


class ReviewPressureDTO(BaseModel):
    """How much human review one delivery repository is holding, and why.

    ``used``/``total`` are the authoritative capacity numbers; ``held``,
    ``oldest_hold_age_seconds`` and ``diff_burden_lines`` are the bounded
    local detail that explains them to a person and is never compared against
    the cap.

    ``countable`` is the honesty gate. False means the forge could not be read
    or the reading is too old to count from — which is NOT zero, and is
    reported as saturated, because an unreadable review queue is the one most
    likely to be full.

    ``enforcement`` says whether the configured prevention is actually in
    force. Before the E7 broker promotion it is ``inactive``: the policy is
    configured and reported, but nothing is being prevented by it yet, and
    saying otherwise would be a claim the code cannot keep.
    """

    delivery_repo: str = ""
    engaged: bool = False
    countable: bool = False
    used: int = 0
    total: int = 0
    held: int = 0
    oldest_hold_age_seconds: float | None = None
    diff_burden_lines: int = 0
    saturated: bool = False
    reasons: list[str] = []
    enforcement: str = "inactive"  # "inactive" | "active"
    enforcement_detail: str = ""
    policy_generation: int = 0
    observation: RemoteObservationDTO


class WorkExceptionDTO(BaseModel):
    """The ONE canonical name for what is off-normal about this work.

    Derived from the dimensions, never stored, and never the only thing a
    surface may read — ``detail`` explains it and the dimension it came from
    still carries the facts. ``blocking`` marks the exceptions that need a
    person rather than time.
    """

    state: str = "none"
    detail: str = ""
    blocking: bool = False
    dimension: str = ""


class WorkActionDTO(BaseModel):
    """One executable next action, offered by the SAME verdict that named the
    exception beside it.

    I20 exposes actions; it does not decide them. The offered set is derived
    from :class:`WorkExceptionDTO` + :class:`SafestActionDTO` in
    ``kew.service.work`` and nowhere else, so a surface can never present a
    button the projection does not stand behind — and an action whose next
    step belongs to a person (resolve the conflict, fix the build, decide on
    the pull request) is offered as nothing at all rather than as a redispatch.

    ``target_kind``/``target_ref`` name the thing the owning domain reconciler
    acts on (a run, a no-change judgment, a publication job, an orphan pull
    request, this item, the fleet). They are read back out of the same facts,
    so a caller never has to construct one — and never gets to substitute one.
    """

    action: str
    intent: str = ""
    #: Which kind of thing ``target_ref`` names — see the docstring above for
    #: the set. ``kew.service.work`` owns the mapping; nothing else assigns one.
    target_kind: str = ""
    target_ref: str = ""
    #: Whether this offer IS the derived safest action rather than its
    #: authorized counterpart (rejecting a no-change is not the safest move,
    #: but it is a decision the same person is entitled to make).
    safest: bool = False
    requires_reason: bool = False


class WorkViewDTO(BaseModel):
    """One snapshot-bound projection of one work item.

    ``view_revision`` digests the fact material (never ``composed_at``), so two
    surfaces holding the same revision are showing the same facts and can be
    compared directly instead of at an undefined instant.
    """

    identity: WorkIdentityDTO
    source: WorkSourceDTO
    route: WorkRouteDTO
    execution: WorkExecutionDTO
    artifact: WorkArtifactDTO
    judgment: WorkJudgmentDTO
    outcome: WorkOutcomeDTO
    # I19 dimensions — orthogonal to the six above, never a substitute for them.
    integration: WorkIntegrationDTO
    readiness: WorkReadinessDTO
    no_change: WorkNoChangeDTO
    #: P1.1 — journey certification, orthogonal to route and readiness above.
    certificate_status: WorkCertificateDTO
    review_pressure: ReviewPressureDTO
    exception: WorkExceptionDTO
    safest_action: SafestActionDTO
    #: Every action a caller may actually request for this work, derived from
    #: the verdict above. Empty is a real answer: it means nothing kew can do
    #: is safe right now, which is exactly when a surface used to invent one.
    actions: list[WorkActionDTO] = []
    view_revision: str
    composed_at: str
    observations: list[RemoteObservationDTO] = []


class WorkActionResultDTO(BaseModel):
    """What one guarded work action did, and the truth immediately after it.

    ``view`` is recomposed AFTER the effect, so the caller's next decision is
    made against the post-write world rather than against the one it asked
    about. ``idempotent`` marks a replay of a request that already applied:
    the effect is not run twice, and the authoritative projection is still
    what comes back.
    """

    action: str
    target_kind: str = ""
    target_ref: str = ""
    applied: bool
    idempotent: bool = False
    detail: str = ""
    expected_revision: str
    view: WorkViewDTO


# --------------------------------------------------------------------------- #
# Console dashboard (#700, Group 1) — the aggregate judgment-queue payload.
# --------------------------------------------------------------------------- #


class DashboardBannerDTO(BaseModel):
    """Top-of-console alarms. Both default False — a fresh repo is not an alarm."""

    fleet_stopped: bool = False
    no_live_driver: bool = False


class DashboardRowDTO(BaseModel):
    """One judgment-queue row, discriminated on ``type``.

    Additive by contract: clients ignore unknown ``type`` values, and the
    type-specific ids are all optional so a future row type round-trips cleanly
    through an older client.
    """

    type: str
    consequence: str = ""
    age: str = ""
    reason: str = ""
    primary_action: str = ""
    issue_num: int | None = None
    # Durable work-item id (string) alongside the legacy numeric ``issue_num``.
    # For a non-numeric tracker (Linear) ``issue_num`` is the 0 sentinel and
    # this carries the real key (``SFD-12``); GitHub rows stringify their number.
    item_id: str | None = None
    estimate_usd: float | None = None
    epic_ref: str | None = None
    url: str | None = None
    pr_number: int | None = None
    # True when the work projection could not read THIS item's work facts (I19).
    # Carried as a flag rather than only as the ``work_unreadable`` row type
    # because the set collapses to one entry per item: an unreadable item that
    # also has a higher-consequence entry keeps that entry, and without this the
    # payload would stop saying its work facts were never read — which is the
    # fact the console's per-item dismissal guard is made of (`unreadItems` in
    # frontend/src/lib/dashboard.ts).
    work_unread: bool = False
    # The three fields that let a row say what it IS and what to run (#2244).
    # Additive, default "", so a client that predates them reads every row it
    # read before.
    #
    # ``attention_kind`` is a ``kew.waits.AttentionKind`` value (or one of the
    # work projection's own: ``merge_decision``, ``work_exception``,
    # ``approval``) — the SAME kind the epic DAG's diamond and the driver's
    # event stream call this wait, so no surface invents a category from the
    # row ``type``, which is historical (every ``epic_holds`` row is served as
    # ``budget_hold`` whatever held the node).
    attention_kind: str = ""
    # The reason in an operator's words: ``kew.waits.StallBucket.label``, which
    # is where the wording lives. "" when no bucket claims the reason — then
    # ``reason`` is the only thing anybody wrote, and a surface prints that.
    label: str = ""
    # A command an operator can paste, with this row's epic and item already in
    # it, or "" when no kew command clears this row (a cap to raise, a merge
    # decision, an approval the console has a button for). Never a template:
    # the console used to print ``kew epic continue <name> <node>`` and a
    # ``kew epic resume`` that has never existed. See ``kew.waits.fill_command``.
    command: str = ""
    # The row in ONE sentence, written by the server (#2539), with no leading
    # item reference: every surface prints that in a column of its own. Work,
    # Monitor, the epic popover and the rail print this and never paraphrase
    # it; ``kew.service.attention.say_wait`` is its only writer.
    sentence: str = ""
    # The item's own title, as its tracker names it (#2786), or "" where the
    # source holds none. Work titles a row by this and prints ``sentence`` on
    # the quiet line under it: titled by the wait, SFD-229 read "the review
    # round cap was reached" on Work and "budget caps per epic" in its epic.
    title: str = ""
    # A merge decision's move: no command clears it, so it is a link instead.
    link: WaitLinkDTO | None = None
    # A merge over a merge verify that failed at the PR's current head leads
    # with the failure: this is the item's MERGE VERIFY section, the row's
    # first move, and ``link`` its second (#3460). None on every other row.
    verify_link: WaitLinkDTO | None = None
    # Every pytest node id a ``test_declarations_missing`` hold asks a person
    # to declare, untruncated (#2884): the reason is capped and ends
    # ``+N more``. Empty on every other row.
    node_ids: list[str] = Field(default_factory=list)
    # The repository a row names when it names no item: a held merge-queue
    # main (#2982) is one per repo, so the repo is its identity. None elsewhere.
    repo: str | None = None


class RouteRowDTO(BaseModel):
    complexity: str
    route: str
    provenance: str  # "file" | "default"


class ReadyIssueDTO(BaseModel):
    issue_num: int
    # Durable work-item id (string) mirroring ``issue_num`` for the id-sweep
    # currency; GitHub-sourced here, so it is the stringified number.
    item_id: str = ""
    title: str
    complexity: str = ""
    route: str = ""
    # No per-issue estimator is wired in Group 1; the field exists for a later group.
    estimate_usd: float | None = None


class QueueCandidateDTO(BaseModel):
    """One open, un-ready issue of the pinned repo — a candidate to queue.

    Sourced from the same non-lagging query as ``surfaced_candidates`` (which
    ``queue_issue`` validates against), so what the picker offers and what the
    verb accepts agree.
    """

    issue_num: int
    # Mirrors ``issue_num`` in the durable-id currency (stringified number for
    # a GitHub candidate).
    item_id: str = ""
    title: str = ""


class CompletionDTO(BaseModel):
    run_id: str
    issue_num: int
    # Durable work-item id (string). For a non-numeric tracker (Linear)
    # ``issue_num`` is the 0 sentinel and this carries the real key.
    item_id: str = ""
    title: str = ""
    status: str = ""
    cost_usd: float = 0.0
    model: str = ""
    finished_at: str = ""


class DashboardGlanceDTO(BaseModel):
    fleet: FleetStatusDTO | None = None
    caps: SpendWindowsDTO | None = None
    routing: list[RouteRowDTO] = []
    ready_queue: list[ReadyIssueDTO] = []
    queue_candidates: list[QueueCandidateDTO] = []
    recent_completions: list[CompletionDTO] = []
    # Human-review capacity (I19). ``None`` means the projection itself could
    # not be read — deliberately not a zeroed reading, which would render an
    # unreadable review queue as an empty one.
    pressure: ReviewPressureDTO | None = None


class DashboardDTO(BaseModel):
    banner: DashboardBannerDTO = DashboardBannerDTO()
    rows: list[DashboardRowDTO] = []
    glance: DashboardGlanceDTO = DashboardGlanceDTO()
    as_of: dict[str, str] = {}
    unavailable: list[str] = []
    #: ``rows``, counted per epic (#2279). Served rather than left to each
    #: client because "the needs-you set, filtered by epic" is ONE reading and
    #: three surfaces take it — the header's counter, each epic page's legend
    #: and the Epics list. Composed by
    #: :func:`kew.service.needs_you.needs_you_by_epic`; an epic with nothing
    #: waiting is absent, and an entry naming no epic is in no bucket, so the
    #: header's count is this map's sum plus the fleet-wide entries.
    needs_you_by_epic: dict[str, int] = {}
    #: The entries naming no epic, counted (#2784) — the "no epic" bucket the
    #: map above has no key for. The Epics list draws it as a row of its own, so
    #: ``sum(needs_you_by_epic.values()) + needs_you_no_epic == len(rows)``.
    needs_you_no_epic: int = 0


class WorkProjectionDTO(BaseModel):
    """Many work views and one pressure reading, bound to ONE snapshot.

    ``projection_revision`` digests every view revision plus the pressure, so
    two surfaces holding the same projection revision are showing the same
    facts for the same set of items — the aggregate form of the guarantee
    ``WorkViewDTO.view_revision`` gives for one item.
    """

    views: list[WorkViewDTO] = []
    pressure: ReviewPressureDTO
    projection_revision: str
    composed_at: str
    unreadable: list[str] = []


# --------------------------------------------------------------------------- #
# Item pipeline projection (FUI-11) — the stages one work item has moved
# through, as the DWork swimlane draws them.
# --------------------------------------------------------------------------- #


class PipelineEvidenceDTO(BaseModel):
    """What one stage's claim rests on, named so a reader can look it up.

    ``kind`` is an open vocabulary (``run``, ``output_scan``,
    ``review_verdict``, ``pull_request`` today); ``ref`` is the identifier
    within that kind — a run id, a head sha, a pull-request URL. Never
    invented: a stage with nothing recorded behind it carries ``None``.
    """

    kind: str
    ref: str


class PipelineStageDTO(BaseModel):
    """One cell of the pipeline swimlane: where this stage got to, and why.

    ``state`` is a PROGRESS vocabulary, deliberately not an attestation one —
    whether the facts behind a stage are attested and chain-verified is the
    evidence chain's question, not this projection's. It is open (a reader
    must tolerate an unknown value); the states composed today are:

    ``pending`` (not reached yet, or a published artifact with no scan
    recorded), ``working`` (in flight), ``done`` (completed), ``not_ready``
    (a review round ended with a not-ready verdict), ``needs_you`` (a person
    must act), ``failed`` (ended terminally), ``not_required`` (does not apply
    to this item) and
    ``unknown`` (a fact could not be read — the reason is named in
    :attr:`ItemPipelineDTO.unavailable`, never collapsed into a clean state).

    ``label`` is the cell's first line and ``detail`` its second, both already
    worded for display so two surfaces cannot word the same stage differently.

    ``passed`` says the stage is over when ``state`` alone does not: a
    published artifact nobody scanned is ``pending`` (the scan it still
    lacks), but its pull request is up. A reader keys "this stage happened" on
    it, never on the worded ``label``.
    """

    stage: str
    state: str
    label: str = ""
    detail: str = ""
    evidence: PipelineEvidenceDTO | None = None
    passed: bool = False


class PipelineLaneDTO(BaseModel):
    """One row of the swimlane, named for the branch the work lands on.

    kew composes exactly one lane (the delivery repository's base
    branch). The list shape is the point: a backport lane is a later,
    additive fact about the same item, not a different payload.
    """

    name: str
    stages: list[PipelineStageDTO] = []


class MergeVerifyHeldOutDTO(BaseModel):
    """The held-out phase of one merge verify: main's tests run on the PR.

    ``failed`` is every base test that failed against the candidate, by node
    id; ``declared`` the ones the PR body's **Behavior changes** excused;
    ``undeclared`` the ones it did not, any of which fails the verify.

    Each list carries at most its first 50 ids, each clipped to 300 characters
    with an ellipsis; ``*_more`` counts the ids of that list left out.
    """

    outcome: str
    failed: list[str] = []
    declared: list[str] = []
    undeclared: list[str] = []
    failed_more: int = 0
    declared_more: int = 0
    undeclared_more: int = 0


class MergeVerifyDTO(BaseModel):
    """The latest ``verify_completed`` for an item's pull request (#3140).

    Joined by head: the event's ``ref_sha``, or the parent that is not
    ``base_sha`` of a two-parent merge of the base, against the heads the
    item's pull request has had in its current review cycle. ``outcome`` is
    ``passed``, ``failed`` or ``incomplete``; ``head`` the pull-request head
    the event joined on, and ``is_current_head`` whether that is the head the
    pull request has now —
    a verify of an older head is not this PR's present state. Allowlisted
    fields only: the event's free-text error never crosses.
    """

    outcome: str
    head: str
    is_current_head: bool
    error_category: str | None = None
    held_out: MergeVerifyHeldOutDTO | None = None
    recorded_at: str = ""


class ItemPipelineDTO(BaseModel):
    """Every lane and stage for ONE work item, bound to one read snapshot.

    Open-ended by construction: a reader renders the lanes and stages it is
    given, in order, rather than a fixed five-column grid — kew never
    emits a Reproduce stage or a backport lane, and must not have to change
    shape when a newer server does.

    ``unavailable`` names every fact that could not be read. A stage is only
    honestly ``pending`` when this list is empty of its reason.
    """

    item_id: str
    lanes: list[PipelineLaneDTO] = []
    unavailable: list[str] = []
    composed_at: str
    #: The latest merge verify of the item's pull request; ``None`` when none
    #: is linked to its heads (or, with ``pipeline:merge_verify`` named, none
    #: could be read).
    merge_verify: MergeVerifyDTO | None = None


class EvidenceChainRowDTO(BaseModel):
    """One guarantee on the item's evidence checklist (FUI-12).

    ``n`` is the 1-based position both artboards number (they render it
    zero-padded); ``label`` is the guarantee ("Sandboxed"), ``event`` the audit
    event that records it, and ``summary`` the already-worded second line
    ("clean · 3670e43f", or the stage's own words when nothing is recorded yet).

    ``ts`` is the recorded row's ISO timestamp, empty when there is no row —
    the surface formats it. ``state`` is an ATTESTATION vocabulary, not a
    progress one: ``attested`` (the host's own fact, on a chain that verifies),
    ``unverified`` (recorded, but not vouched for — never a green check),
    ``pending`` (expected, not recorded yet) and ``missing`` (expected, and its
    stage has passed).
    """

    n: int
    label: str
    event: str
    summary: str = ""
    ts: str = ""
    state: str


class ItemEvidenceDTO(BaseModel):
    """The guarantees ONE item's route promises, in order, with their evidence.

    Always ``total`` rows, even for an item nothing has been dispatched for:
    the promises exist before the work does, and they are then all ``pending``
    with ``reason`` saying why.

    ``attested_count / total`` is the artboard's "3 / 4 attested".
    ``chain_verified`` is the audit chain's own verdict — the one
    ``/api/audit/status`` and ``kew audit verify`` reach — and no row reads
    ``attested`` while it is false. ``reason`` is one sentence for the reader;
    ``unavailable`` names every fact that could not be read, machine-side.
    """

    item_id: str
    rows: list[EvidenceChainRowDTO] = []
    attested_count: int
    total: int
    chain_verified: bool
    reason: str = ""
    unavailable: list[str] = []
    composed_at: str


class ItemDescriptionDTO(BaseModel):
    """What a person wrote about ONE item on its tracker (FUI-31).

    The only remote read on the Work screen, so the honesty fields come first:
    ``availability`` is :class:`RemoteObservationDTO`'s vocabulary —
    ``available`` (read, or read recently), ``stale`` (an older reading, served
    because the refresh failed) or ``unavailable`` (no reading at all). A body
    is reportable as empty ONLY under ``available``; every other answer carries
    ``reason``, and a surface falls back to its "Open on GitHub" link.

    ``expected`` and ``actual`` are filled only from headings that name them;
    the rest of the body — headings included — stays in ``description``.
    """

    item_id: str
    availability: str  # "available" | "stale" | "unavailable"
    provenance: str  # "observed" | "cached" | "error"
    observed_at: str | None = None
    title: str = ""
    url: str | None = None
    description: str = ""
    expected: str = ""
    actual: str = ""
    reason: str = ""
    composed_at: str


class ReviewVerdictRefDTO(BaseModel):
    """The last governed-review round recorded for one item (#2303).

    ``head_sha`` is the head the reviewer actually read. It is the whole point
    of the row: a verdict is only about the commit it was given, so a head that
    moved since makes the verdict old news rather than wrong.
    """

    round: int
    verdict: str
    head_sha: str
    recorded_at: str


class LatestHeadDTO(BaseModel):
    """The newest head this host has a record of, and where that came from.

    ``source`` is ``snapshot`` (the driver journaled ``headRefOid`` on its last
    tick), ``review_round`` (no journaled head, so the newest thing known is
    the one a verdict named) or ``unobserved`` — in which case ``sha`` is empty
    and no staleness claim is made at all.
    """

    sha: str = ""
    source: str = "unobserved"
    at: str | None = None


class BaseStateDTO(BaseModel):
    """How this head stands against its base branch (#2303).

    ``state`` is ``clean``, ``behind`` (the base moved ahead; the factory can
    catch up on its own), ``conflicted`` (a person has to rebase), ``blocked``
    (a branch rule or a draft stands in the way) or ``unknown``. ``reason`` is
    always a sentence — never a blank box — and ``since`` is when this state was
    first journaled, so a surface ages it instead of inventing a duration.

    ``at`` is when the state was last actually READ, which is not the row's
    ``observed_at``: the driver carries a merge state forward across a tick
    GitHub answered ``UNKNOWN`` on, so a state can be older than the tick that
    reported it. ``None`` means nothing read it.
    """

    state: str = "unknown"
    reason: str = ""
    since: str | None = None
    at: str | None = None
    source: str = "unobserved"


class PrFreshnessRowDTO(BaseModel):
    """One open item's artifact, as freshness (#2303).

    ``review_state`` is ``current``, ``stale`` (the head moved after the
    verdict), ``remediating`` (kew owes the next round), ``reviewing`` (a round
    is reading it now) or ``no_round`` (nothing has reviewed it yet).
    """

    item_id: str
    epic_ref: str
    pr_number: int | None = None
    pr_url: str | None = None
    last_verdict: ReviewVerdictRefDTO | None = None
    latest_head: LatestHeadDTO = LatestHeadDTO()
    review_state: str = "no_round"
    base: BaseStateDTO = BaseStateDTO()
    observed_at: str


class PrFreshnessDTO(BaseModel):
    """A bounded page of :class:`PrFreshnessRowDTO`.

    ``next_cursor`` is the ``epic_ref|item_id`` of the last row when the page
    filled, else ``None`` — a full page with no cursor would be a silent
    truncation. ``unavailable`` names every read that did not happen, so an
    empty ``rows`` is only "nothing is stale" when it is empty too.
    """

    rows: list[PrFreshnessRowDTO] = []
    next_cursor: str | None = None
    unavailable: list[str] = []
    composed_at: str
