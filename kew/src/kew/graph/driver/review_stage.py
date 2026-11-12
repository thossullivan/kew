"""The governed-review drive pass — the connective tissue for the adversarial
review stage (#977 epic, issue #991).

Every component of the stage was built and unit-tested in isolation
(``dispatch_reviewer``, ``materialize_reviewer_worktree``,
``parse_verdict_sidecar``, ``record_review_round``, ``emit_review_verdict``) but
no driver node called them in sequence, so the merge-pass invariant (#988) had a
live gate that nothing fed. This module is that node: for a review-enabled epic
it dispatches a fresh-context reviewer against the recorded head SHA, collects +
parses the host-stored sidecar, records the round, emits the attested verdict,
releases the reservation, and drives the lifecycle transitions #988 defined.

The action per node is keyed on the PERSISTED review state + the live head SHA,
not on the ``DriverStatus`` label alone, so the head-move signal distinguishes
"NEEDS_CHANGES not yet remediated" (remediate) from "remediation landed a new
commit → re-review round N+1" (dispatch), exactly mirroring the overlay's own
``_review_status`` derivation.

The reviewer *launcher* and *remediator* are seams (a real container reviewer
needs a read-only, non-pushing sandbox variant — a follow-up); the driver injects
them. When none is wired the pass logs loudly and takes no action rather than
silently accepting — fail-loud, never a silent green.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from kew import review_comment, review_status
from kew.audit import Actor, emit
from kew.behavior_declarations import (
    DeclarationRequest,
    accepted_lines,
    asks_only_lines,
    carry_declared_lines,
    drop_proposed_lines,
    load_declaration_request,
    load_held_out_request,
    proposal_request,
    proposals_answer,
    record_held_out_request,
    requested_by_round,
    undeclared,
    unneeded_proposals,
)
from kew.budget import (
    REMEDIATION_POOL_KEY,
    WINDOW_BUDGET_REFUSAL,
    BudgetExceeded,
    check_review_budget,
    is_window_budget_hold,
    review_budget_hold_reason,
)
from kew.epic_holds import (
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX,
    REVIEW_LAUNCH_FAILED_HOLD_PREFIX,
    REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX,
    REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
    REVIEW_RATE_LIMITED_WAIT_PREFIX,
    REVIEW_RETRYING_WAIT_PREFIX,
    REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
    ROUNDS_EXHAUSTED_HOLD_REASON,
    declaration_record_failed_hold_reason,
    declarations_missing_hold_reason,
    declarations_not_written_hold_reason,
    deferred_publication_key,
    is_another_writers_hold,
    is_ci_pending_hold,
    is_pr_body_unreadable_hold,
    pr_body_unreadable_hold_reason,
    pre_review_gate_exhausted_hold_reason,
    publication_held_hold_reason,
    remediation_no_change_hold_reason,
    remediation_no_progress_hold_reason,
    review_gate_paths_unread_hold_reason,
    safe_hold_reason,
)
from kew.graph.driver.base_integration import (
    HEAD_MOVED_SKIP,
    INTEGRATION_SKIPPED_EVENT,
    MARKER_HEAD_CHARS,
    MAX_CONSECUTIVE_STEPS,
    OPERATOR_OWNED_SKIP,
    BaseIntegrator,
    base_integration_due,
    base_integration_exhausted_hold_reason,
    base_integration_hold_reason,
    consecutive_base_integrations,
    operator_commit_at_head,
    record_base_integration_step,
    run_base_integration,
)
from kew.graph.driver.overlay import DriverStatus, admission_refused_hold_reason
from kew.graph.driver.pre_review_gate import (
    HELD_OUT_GATE,
    HELD_OUT_HALF,
    MAX_GATE_ROUNDS,
    GateOutcome,
    configured_halves,
    first_failing_line,
    gate_rounds,
    gate_skip_reason,
    private_names,
    record_settled,
    record_skips,
    run_pre_review_gate,
    settled_halves,
)
from kew.graph.driver.review_detach import (
    DetachedReview,
    ReviewDetacher,
    held_reservation,
    launch_detached,
    report_in_flight,
    run_detached_review,
)
from kew.ids import (
    GITHUB_ID_RE,
    InvalidItemIdError,
    branch_slug,
    provider_from_config,
    validate_item_id,
)
from kew.pr_text import proposed_section_lines
from kew.progress import line_scope
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL, PromptConfigurationError
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication import PUBLICATION_CONFIGURATION_REFUSAL
from kew.publication_credentials import (
    BRANCH_RUNS_CAP,
    CREDENTIAL_PUBLICATION_REFUSAL,
    is_branch_run_cap,
)
from kew.review_audit import (
    VERDICT_DEGRADED,
    VERDICT_UNPARSEABLE,
    emit_review_retried,
    emit_review_verdict,
    review_retries,
)
from kew.review_checkout import DEFERRED_CHECKOUT_UNRECORDED
from kew.review_contract import (
    FAILED_NO_VERDICT_CATEGORY,
    MULTIPLE_RESULTS_CATEGORY,
    RATE_LIMITED_CATEGORY,
    pre_review_gate_sidecar,
)
from kew.review_criteria import contract_test_ids as recorded_contract_test_ids
from kew.review_criteria import criterion_ids as recorded_criterion_ids
from kew.review_delta import (
    REVIEW_MODE_FULL,
    REVIEW_MODE_OPERATOR_SEND_BACK,
    REVIEW_MODE_PRE_REVIEW_GATE,
    VERDICT_NEEDS_FULL,
    confirms_full_ready,
    effective_verdict,
    plan_review_mode,
    remediation_rounds_spent,
)
from kew.review_dispatch import (
    NO_PROVENANCE,
    PromptProvenance,
    ReviewerLauncher,
    ReviewerLaunchError,
    dispatch_reviewer,
    head_gate_paths,
)
from kew.review_integrity import read_pr_body
from kew.review_outcome import (
    REVIEW_OUTCOME_EVENT,
    STAGE_PRE_LAUNCH,
    STATUS_COMPLETED,
    ReviewOutcome,
    latest_review_outcome,
    list_review_outcomes,
)
from kew.review_residuals import (
    READY_WITH_RESIDUALS,
    ResidualFilingError,
    RoundMarkRetryError,
    accepted_residuals,
    comment_residual_round,
    comment_residuals,
    file_residual_issue,
    mark_round_accepted,
    matching_residual_issue,
    record_acceptance,
    residual_issue_body,
    residual_issue_is_open,
    stop_rule_decision,
    unmarked_acceptance,
)
from kew.runner_quota import (
    RunCredential,
    first_that_fits,
    read_run_credential,
    record_run_credential,
    refused_token,
)
from kew.tracking import STREAM_SILENT_CATEGORY
from kew.verify_lease import verify_deferral
from kew.worktree import remove_checkout_tree

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.graph.driver.facts import NodeFacts
    from kew.review_contract import CollectedSidecar, Finding, ParsedVerdict
    from kew.review_dispatch import ReviewerDispatch
    from kew.run_store import ReviewRound, RunRecord, RunStore, SqliteRunStore

_logger = logging.getLogger(__name__)

_VERDICT_READY = "READY"
_VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"

# Hold reasons the overlay routes to HELD (``kew.epic_holds.REVIEW_HOLD_REASONS``,
# Task 4). The round cap comes from kew.epic_holds: unhold has to recognise the same
# string to restart the cycle the cap counts (#2154), so it has one owner.
_HOLD_ROUNDS_EXHAUSTED = ROUNDS_EXHAUSTED_HOLD_REASON
_HOLD_UNPARSEABLE = "review_unparseable"
_HOLD_RESIDUALS_UNFILED = "review_residuals_unfiled"
_HOLD_SANDBOX_REFUSED = REVIEW_SANDBOX_REFUSED_HOLD_PREFIX
_HOLD_LAUNCH_FAILED = REVIEW_LAUNCH_FAILED_HOLD_PREFIX
_HOLD_RATE_LIMIT_EXHAUSTED = REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX
_HOLD_MULTIPLE_RESULTS = REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX
_HOLD_FAILED_NO_VERDICT = REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX

# The review-outcome classifications a remediation dispatch reports when it
# refused BEFORE launching an agent. Each one repeats identically every tick
# until a person changes something, so each one HOLDs the node rather than
# leaving it `remediating` — a state the tick signal reads as progressing
# (#1144/#2153). ``budget_exhausted`` is the remediation pool (#993) running
# out mid-cycle: the node is intact, one config key short of resuming.
# ``budget_window`` is the daily or weekly cap (#3205): the node waits under
# that cap's hold until the window moves on or the cap is raised.
# ``prompt_configuration_refused`` is the operator's active remediation profile
# refused at launch (#3023): only retiring or replacing it changes the answer.
# ``deferred_checkout_unrecorded`` is the review checkout holding a deferred
# publication's work whose move could not be recorded, or whose ledger could
# not be read to tell (#3186): it stays put
# until a person publishes or discards that work.
# ``github_auth`` is a gh that was never logged in or whose token GitHub
# refuses (#3334): it holds, as dispatch does, until a person fixes the login.
_BUDGET_EXHAUSTED = "budget_exhausted"
_CONTAINMENT_REFUSED = "containment_refused"
_GITHUB_AUTH = "github_auth"
_REMEDIATION_REFUSALS = frozenset(
    {
        PUBLICATION_CONFIGURATION_REFUSAL,
        _CONTAINMENT_REFUSED,
        _BUDGET_EXHAUSTED,
        WINDOW_BUDGET_REFUSAL,
        PROMPT_CONFIGURATION_REFUSAL,
        DEFERRED_CHECKOUT_UNRECORDED,
        _GITHUB_AUTH,
    }
)

# Pre-launch remediation refusals that may be transient — an unreadable ledger
# read refuses the same way as evidence that is really missing — so the first
# ones are retried. A run of identical ones (same classification, detail and
# head) is not clearing by itself: the drive retried one for five hours and
# posted 253 error comments (#3067). After ``_MAX_REPEATED_REFUSALS`` the node
# is held under ``admission_refused`` like the refusals above.
_REPEATABLE_REFUSALS = frozenset({CREDENTIAL_PUBLICATION_REFUSAL})
_MAX_REPEATED_REFUSALS = 3

# The reviewer sidecar's error category for a refusal the sandbox layer made
# BEFORE any container ran — no verdict channel, no mapped image, or an image
# the local runtime could not resolve (``review_launcher``, failure layers
# ``runner`` / ``image``). No review happened, so it is infrastructure, not a
# verdict (#2147): the refused round is retried on the next tick, bounded by
# ``_MAX_REFUSED_ATTEMPTS`` consecutive refusals, and only then held under
# ``_HOLD_SANDBOX_REFUSED``. Recording it as an unparseable verdict held #2085
# needs-human over an image that was present again a minute later.
_REFUSED_BEFORE_LAUNCH = "sandbox_refused"
_MAX_REFUSED_ATTEMPTS = 3

# A reviewer whose stream went silent for ``[agent] no_output_minutes`` and was
# stopped by the watchdog (#2240; ``tracking.STREAM_SILENT_CATEGORY``).
# A stalled provider stream, not a verdict: retried like a pre-launch refusal,
# bounded by ``_MAX_SILENT_ATTEMPTS`` rounds in a row, then held naming it.
_MAX_SILENT_ATTEMPTS = 3
_STREAM_SILENT_REFUSAL = (
    "the reviewer's stream went silent and its container was stopped "
    f"({STREAM_SILENT_CATEGORY}, {_MAX_SILENT_ATTEMPTS} rounds in a row)"
)

# A runtime call that timed out at review launch — every create of the
# reviewer's container, or the provisioning container's ``rm -f`` — is a loaded
# host, not a fault (#3246). No reviewer ran, so the round is retried like a
# sandbox refusal, but only once a back-off has passed since the last one: the
# first wait is ``_LAUNCH_TIMEOUT_RETRY_WAIT`` and each further one doubles. On
# the ``_MAX_LAUNCH_TIMEOUT_ATTEMPTS``-th consecutive timeout the node is held
# ``review_launch_failed`` naming the timeout; never on the first.
_LAUNCH_TIMEOUT_RETRY_WAIT = timedelta(minutes=2)
_MAX_LAUNCH_TIMEOUT_ATTEMPTS = 3

# The reviewer-run category a provider usage limit earns (#2219). A limit ends
# the reviewer mid-run: the stream carries the provider's limit notice and the
# run ends with no verdict block — on an error result, or on nothing at all.
# That is infrastructure, not a verdict — recorded as one it held the incident's
# node ``review_unparseable`` (needs-human) for 30 h over a limit that reset by
# itself, and sent its operator looking for a verdict channel to repair.
#
# So the round is NOT held and NOT counted: nothing is written against the
# review cycle, and the node is re-reviewed once the limit has had time to
# reset. ``_RATE_LIMIT_RETRY_WAIT`` is that spacing — the host cannot read the
# provider's exact reset instant, so it waits a window and tries again.
#
# ``_MAX_RATE_LIMIT_WAIT`` bounds how long that goes on, measured across the
# node's streak of limited rounds — the ones a live retry cadence could have
# produced, per ``_RATE_LIMIT_STREAK_GAP``. It has to exist: a node waiting in
# ADVERSARIAL_REVIEW carries no hold row, and every stall clock runs off a hold
# row (``tick._held_signal``) or an open IN_REVIEW PR
# (``tick._stale_review_stuck``), so an unbounded wait is one no stall clock can
# ever reach — a persistent (or planted) limit would relaunch a reviewer
# container every ``_RATE_LIMIT_RETRY_WAIT`` forever while the epic read
# PROGRESSING off the node's own review status and ``kew epic run`` never
# exited. Six hours is longer than a provider rolling usage window resets in, so
# the limit this issue is about is never held; a limit still rejecting every
# round after six hours is not resetting on its own, and the hold
# (``_HOLD_RATE_LIMIT_EXHAUSTED``) names the person who can raise it.
_RATE_LIMITED = RATE_LIMITED_CATEGORY
_RATE_LIMIT_RETRY_WAIT = timedelta(minutes=15)
_MAX_RATE_LIMIT_WAIT = timedelta(hours=6)

# How far apart two limited rounds can be and still be ONE limit incident.
#
# The bound above is a wall-clock span over the node's limited rows, so what
# counts as one incident cannot be "every limited row with no better row after
# it": nothing writes a review row while the driver is not running, so downtime
# between two unrelated limits sat INSIDE the span. A host stopped overnight
# between two ordinary limits came back, hit a fresh transient one, and read a
# span of hours off its own history — the FIRST round of the new incident was
# held needs-human under ``_HOLD_RATE_LIMIT_EXHAUSTED``, telling its operator to
# raise a limit that had never once been waited out.
#
# A live incident's rounds are spaced by the retry wait plus the poll interval
# (60s by default) and the reviewer's own startup, so four times the wait
# absorbs every late tick and still refuses to join rounds separated by a driver
# that was not running. A gap longer than this ends the streak exactly as a
# round that reached the provider does.
_RATE_LIMIT_STREAK_GAP = 4 * _RATE_LIMIT_RETRY_WAIT

# The retry count that reaches the same bound when wall clock cannot (#2231
# review).
#
# The gap above is a fixed hour because widening it to fit a slow driver would
# re-admit the downtime it exists to refuse — the two facts are the same shape,
# and a gap can only be read as one of them. But the driver's cadence is NOT
# fixed: ``kew epic run --interval`` takes any spacing and ``kew epic advance``
# runs from cron, so a host that ticks every two hours (or nightly) leaves every
# pair of limited rounds further apart than any gap a live retry cadence could,
# and the span bound above reads one round per incident — never a span it can
# reach, however long the limit goes on rejecting rounds.
# That made ``_MAX_RATE_LIMIT_WAIT``, the only bound that ever reaches a person
# for a node waiting in ADVERSARIAL_REVIEW, unreachable at those cadences: the
# relaunch loop this issue is about, with no hold at the end of it.
#
# So the limit is also spent by COUNT, over every consecutive limited round
# regardless of gaps — a quantity no cadence can dilute, ended (like the span's
# streak) by any round that got past the provider. One more than the span bound
# allows at the fastest cadence, so the count never fires first on a default
# 60s-poll host and the hold's "no reviewer round got past a usage limit for 6 h"
# reason is true on both routes.
_MAX_RATE_LIMIT_ROUNDS = int(_MAX_RATE_LIMIT_WAIT / _RATE_LIMIT_RETRY_WAIT) + 1

# The reviewer-run error categories that mean the reviewer NEVER STARTED, and
# the hold each one earns (#2043). ``launch_failed`` is the lane failing before
# the container ran — no runtime, or the pre-launch materialize step raising —
# and ``credential_missing`` is admission refusing a runner kew cannot
# authenticate inside a container. Neither is a verdict: held as
# ``review_unparseable`` they sent the incident's operator looking for a
# reviewer verdict, and a verdict channel to repair, that never existed.
#
# Every hold here publishes "the reviewer never started" to the PR, so a
# category only belongs in this table while EVERY producer of it can PROVE the
# container did not run. Both producers are narrowed to exactly that:
# ``review_launcher._review_launch_failure`` for a run that raised inside the
# launcher (a reviewer that started and then died mid-stream reports the stream
# layer instead), and ``_dispatch_failure_evidence`` for a raise that escaped
# ``dispatch_reviewer`` entirely. Both land the unprovable cases on
# ``review_unparseable`` below.
_LAUNCH_FAILED = "launch_failed"
_CREDENTIAL_MISSING = "credential_missing"
# kew could not create or start the reviewer's container (#2973). Its producers
# set it only on a refused create, or on a container the runtime still reports
# never started, so it too proves the reviewer did not run.
_CONTAINER_START_FAILED = "container_start_failed"
# Every ``create`` of the reviewer's container timed out before the runtime
# answered (#3103): the host was too slow and no reviewer ever started.
_RUNTIME_TIMEOUT = "runtime_timeout"
# The reviewer's container was made but refused its agent's startup: the
# startup acknowledgement never came and no agent event followed, so the
# runtime returned with no reviewer having run (#3186).
_SANDBOX_STARTUP_REFUSED = "sandbox_startup_refused"
# The operator's active reviewer profile refused at launch (#3024): its digest
# no longer matches, or it opens a Markdown block. Raised while the prompt is
# assembled, before any checkout exists, so the reviewer provably never started.
#
# Keyed by exactly ``tracking.STARTUP_REFUSED_CATEGORIES`` (a test holds the
# two equal): one set says "no agent started" to every lane.
_PRE_LAUNCH_HOLDS = {
    _REFUSED_BEFORE_LAUNCH: _HOLD_SANDBOX_REFUSED,
    _LAUNCH_FAILED: _HOLD_LAUNCH_FAILED,
    _CREDENTIAL_MISSING: _HOLD_LAUNCH_FAILED,
    _CONTAINER_START_FAILED: _HOLD_LAUNCH_FAILED,
    _RUNTIME_TIMEOUT: _HOLD_LAUNCH_FAILED,
    _SANDBOX_STARTUP_REFUSED: _HOLD_LAUNCH_FAILED,
    PROMPT_CONFIGURATION_REFUSAL: _HOLD_LAUNCH_FAILED,
}

# Every crash-path category that earns a hold of its own rather than the bare
# ``review_unparseable``. A spent rate-limit wait (#2219) is one of them but is
# deliberately NOT in ``_PRE_LAUNCH_HOLDS``: that table also answers "did the
# container run?" for ``_dispatch_failure_evidence``, and a usage limit ends a
# reviewer that very much started.
_CRASH_HOLDS = {
    **_PRE_LAUNCH_HOLDS,
    _RATE_LIMITED: _HOLD_RATE_LIMIT_EXHAUSTED,
    MULTIPLE_RESULTS_CATEGORY: _HOLD_MULTIPLE_RESULTS,
    FAILED_NO_VERDICT_CATEGORY: _HOLD_FAILED_NO_VERDICT,
    STREAM_SILENT_CATEGORY: _HOLD_FAILED_NO_VERDICT,
}

# The refusal text a crash category names in its hold, over the stream's own:
# a killed container's bare signal exit says nothing about why it was killed.
_CATEGORY_REFUSALS = {STREAM_SILENT_CATEGORY: _STREAM_SILENT_REFUSAL}

# The crash holds of a reviewer that RAN and whose stream says what was wrong
# (#3174). Their reason is the stream's own refusal text — "reviewer exited 1",
# "2 result events" — never a remedy for a launch that did happen.
_STREAM_REFUSAL_HOLDS = frozenset({_HOLD_MULTIPLE_RESULTS, _HOLD_FAILED_NO_VERDICT})

# A runner that answered more than once on a clean exit is retried this many
# times per node and head before the node holds (#3174, owner decision).
_MAX_MULTIPLE_RESULTS_RETRIES = 1

# The category for a raise that escaped ``dispatch_reviewer`` AFTER the reviewer
# run was admitted — the host's own post-run bookkeeping (the run finalizer, the
# prompt-handoff removal, the stream collector) failing around a container that
# may well have run and exited 0. Deliberately NOT in ``_PRE_LAUNCH_HOLDS``: the
# host cannot prove the reviewer never started, so it earns the crash path's
# ``review_unparseable`` — kew got no usable verdict, which is exactly true —
# rather than telling the operator to go fix a launch that worked.
_REVIEW_HOST_FAILED = "review_host_failed"

# The fix for each layer that can refuse before the reviewer starts, named IN
# the hold reason. Composed from the layer and the runner — never from the
# refusal's own prose, which runs past the length every operator surface echoes
# a reason at (``HOLD_REASON_MAX_CHARS``) and would lose exactly the command at
# its end. The full prose stays in the tick log and the attested verdict.
_PRE_LAUNCH_REMEDY = {
    "image": (
        "run kew sandbox build --runner {runner}, or map [sandbox] images.{runner}"
    ),
    "runner": "pin [review] runner to a runner that has a verdict channel",
    "runtime": "start the container runtime [sandbox] runtime names",
    "credential": (
        "pin [review] runner to a runner kew can authenticate in a container"
    ),
    "container": "container {container} could not be started; see {runtime} ps -a",
    "profile": (
        "approve a new adversarial_review prompt profile version, or retire "
        "the active one"
    ),
}
# A category whose own name is the lead, ahead of its layer's remedy: a create
# the runtime never answered is a slow host, not a container to inspect (#3103).
_CATEGORY_REMEDY = {
    _RUNTIME_TIMEOUT: (
        "a {runtime} call timed out on {attempts} launches in a row "
        "(runtime_timeout); the host is too slow, check {runtime} info"
    ),
}
# An unknown or absent layer still names where to look: a hold with no next
# step is the dead end this issue is about.
_PRE_LAUNCH_REMEDY_FALLBACK = (
    "read this tick's reviewer log and the review run's error_category"
)

# Human text for the GitHub-native review status a crashed round publishes.
_CRASH_STATUS_TEXT = {
    _HOLD_UNPARSEABLE: "unparseable verdict",
    _HOLD_SANDBOX_REFUSED: "sandbox refused the reviewer before launch",
    _HOLD_LAUNCH_FAILED: "the reviewer never started",
    _HOLD_RATE_LIMIT_EXHAUSTED: "a provider usage limit that is not resetting",
    _HOLD_MULTIPLE_RESULTS: "the reviewer answered more than once",
    _HOLD_FAILED_NO_VERDICT: "the reviewer run failed without a verdict",
}


@dataclass(frozen=True)
class RemediationContext:
    """Everything the remediation seam needs to act on a NEEDS_CHANGES verdict.

    The adapted ``kew review`` path (#987) fixes the findings and lands a new
    commit; the driver detects the moved head next tick and re-reviews.
    """

    item_id: str
    issue_num: int
    pr_number: int | None
    head_sha: str
    review_round: int
    sidecar_path: str | None
    """Host path of the NEEDS_CHANGES sidecar whose evidenced findings the
    remediator feeds as input (#1030); None only for pre-v32 rounds (grandfathered
    — the remediator logs and no-ops rather than remediate against no findings)."""
    criterion_ids: tuple[str, ...] = ()
    """The recorded criterion ids that round was judged against (#2291), from
    its own ``criteria_json``. The remediator needs them to re-parse the stored
    sidecar: without them every finding's ``criterion`` is dropped, and the
    brief that must list the failing criteria first would list none."""
    reservation_held: bool = False
    """The caller already holds this item's review reservation (a detached
    review-stage child, #2459), so the remediation must run under it rather
    than take its own — the item has one reservation row, and a second
    reserve would refuse the very run the slot was taken for."""
    declaration: DeclarationRequest | None = None
    """Set when the stage read this round as asking only for **Behavior
    changes** lines the PR body still lacks (#2872): the brief asks for those
    lines and no code change. None for every other round, which gets the
    ordinary brief. kew takes the tests the body declares off a declaration
    finding before the round is recorded (#3516), so one still naming them
    is a round that fell short of what a READY needs or tied the finding to a
    criterion. A declared test failing in a way its line's reason does not
    explain is an ordinary finding, never a declaration one."""
    proposal: DeclarationRequest | None = None
    """Set when every finding of an ordinary round is a ``kind: "declaration"``
    finding the PR body does not declare yet (#3493), though the round is not
    one :attr:`declaration` covers (the host flagged no such tests: a frontend
    test file, say). The brief makes one proposed **Behavior changes** line
    per test the whole job; the lines reach only the proposed block, where the
    reviewer rules on them. Never set beside :attr:`declaration`."""


# A seam so the drive pass is testable without a container; production wiring
# (the read-only, non-pushing reviewer sandbox) is a follow-up on this node.
Remediator = Callable[[RemediationContext], ReviewOutcome | None]


@dataclass
class ReviewStageOutcome:
    """What the drive pass did this tick, for the tick's action summary + tests."""

    dispatched: list[str] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)
    remediating: list[str] = field(default_factory=list)
    unwired: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    """Nodes whose concurrent action raised, or never started because the worker
    could not open its ledger handle (#2110); the first error re-raises."""
    findings: dict[str, int] = field(default_factory=dict)
    """Node -> evidenced finding count of the verdict recorded this tick."""
    residuals: dict[str, int] = field(default_factory=dict)
    """Node -> residual issue number for rounds the stop rule accepted (#2112)."""
    refused: dict[str, str] = field(default_factory=dict)
    """Node -> waiting reason for a review the sandbox refused BEFORE launch
    (#2147). The node is neither held nor credited a round, so the next tick
    finds it due and re-reviews it; the tick reports this reason meanwhile."""
    integrated: dict[str, str] = field(default_factory=dict)
    """Node -> waiting reason for a node whose one action this tick was the
    merge-main step (#2149). The node ends the tick un-held and in the review
    lifecycle, so — exactly like ``refused`` — nothing else in the tick would
    show the operator that the step ran: the line would read as an untouched
    in-review node. The next tick reviews the head the step produced.

    It is also the set of nodes whose facts THIS tick has spent: the branch the
    tick read is gone (the step moved the head, or the ``DIRTY`` fact was stale
    and the PR already merged clean). The merge pass runs after this stage on
    those same facts, so it must skip them for this one tick — see
    ``tick._run_merge_pass``. Classifying one anyway re-derived ``ci_pending``
    from the resolved ``DIRTY`` fact, and the merge gate then refuses the
    verdict the moved head just staled, so the node was neither re-reviewed nor
    merged — the exact opposite of the line above. The ``conflicted`` /
    ``failed`` outcomes need no such exclusion: they leave the branch untouched,
    so ``DIRTY`` is still true of it and the pass's own reading stays honest.

    Skipping the pass is also what takes away the ``ci_pending`` hold that used
    to bound a repeating step by accident, which is why an integrated step
    records its own marker row — see
    ``base_integration.MAX_CONSECUTIVE_STEPS``."""
    gate_skipped: dict[str, str] = field(default_factory=dict)
    """Node -> why the pre-review gate did not check the head that went to
    review (#3280): a refused launch, a timeout, a tool the image lacks, a red
    base. A gate that ran and passed adds nothing."""
    detached: dict[str, str] = field(default_factory=dict)
    """Node -> waiting reason for a node whose action this tick handed to a
    detached child (#2459). Its result lands in the ledger when the child
    finishes, and the next tick reads it; meanwhile the node's live review
    reservation keeps it out of this pass. Like ``integrated``, the facts the
    tick read are no longer the child's to act on, so the merge pass skips it."""


def _hold(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    reason: str,
    *,
    provider: str,
    node_ids: tuple[str, ...] = (),
) -> None:
    """Hold a node via the provider-appropriate façade (mirrors tick's helper)."""
    if provider == "github":
        store.hold_epic_node(epic_ref, item_id, reason, node_ids=node_ids)
    else:
        store.hold_epic_node_item(epic_ref, item_id, reason, node_ids=node_ids)


def _record_dispatch_failure(
    store: RunStore,
    config: DispatchConfig,
    *,
    run_id: str,
    item_id: str,
    issue_num: int,
    error_category: str,
) -> None:
    """Leave a terminal review row when the dispatch raised before a verdict.

    A row the launcher already wrote is STAMPED, never rebuilt: its telemetry is
    what ``check_review_budget`` sums, so replacing a finished reviewer's row
    with a synthetic $0 one would silently return that round's spend to the
    review pool and uncap ``max_review_usd``. Only a raise that landed before
    the launcher admitted anything (no row at all) writes a fresh one.
    """
    from kew.graph.driver.review_launcher import review_model
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord

    now = datetime.now(UTC)
    stored = store.get(run_id)
    if stored is not None:
        stored.error_category = error_category
        # The run yielded no verdict however far it got, so the row is terminal
        # and failed — but every accounting field it earned stays as recorded.
        stored.status = "failure"
        if stored.finished_at is None:
            stored.finished_at = now
        store.save(stored)
        return
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            status="failure",
            exit_code=-1,
            started_at=now,
            finished_at=now,
            error_category=error_category,
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            model=review_model(config),
            runner_name=config.review.runner or "claude-code",
            repo=config.project.repo or None,
        )
    )


def _raised_failure_evidence(
    stored: RunRecord | None, exc: Exception
) -> tuple[str, str]:
    """:func:`_dispatch_failure_evidence`, except that a refused reviewer profile
    names itself (#3024): only the operator's profile can fix it, and calling
    it a launch failure sends them to the container runtime instead."""
    if isinstance(exc.__cause__, PromptConfigurationError):
        return PROMPT_CONFIGURATION_REFUSAL, "profile"
    return _dispatch_failure_evidence(stored)


def _dispatch_failure_evidence(stored: RunRecord | None) -> tuple[str, str]:
    """The ``(error_category, failure_layer)`` a raise that escaped
    ``dispatch_reviewer`` earns, decided from the run row that raise left behind.

    ``launch_failed`` publishes "the reviewer never started" to the PR, so the
    stage claims it only where the host's own evidence proves it: NO reviewer
    row exists for this ``run_id``. The launcher writes that row (``_record_pending``)
    immediately before the container, and ``dispatch_reviewer`` writes none of
    its own, so an absent row means the raise landed in the reservation, the
    budget preflight, the prompt load or the worktree materialize — all of them
    before any container.

    Once a row exists the container may have run, streamed and exited 0, with
    only the host's post-run bookkeeping raising: a category already recorded on
    the row is the producing layer's own verdict and stands, and anything else
    is a host failure the stage must not dress up as a launch that never
    happened.
    """
    if stored is None:
        return _LAUNCH_FAILED, "launch"
    recorded = stored.error_category
    if recorded:
        return recorded, "launch" if recorded in _PRE_LAUNCH_HOLDS else "stream"
    return _REVIEW_HOST_FAILED, "host"


def _consecutive_refusals(
    store: RunStore, item_id: str, category: str = _REFUSED_BEFORE_LAUNCH
) -> int:
    """Review runs under *category* at the END of the node's review history.

    Any review run with another outcome — a collected verdict, a budget
    refusal, a launch failure — ends the streak, so the retry bound is spent per
    refusal incident rather than once per node lifetime.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    streak = 0
    for record in reversed(store.list_by_item(item_id)):
        if record.run_kind != ADVERSARIAL_REVIEW_RUN_KIND:
            continue
        if record.error_category != category:
            break
        streak += 1
    return streak


def _defer_refused_review(
    node_id: str,
    item_id: str,
    issue_num: int,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    run_id: str,
) -> bool:
    """Retry a pre-launch sandbox refusal on the next tick, bounded (#2147).

    The refusal landed before any container ran, so NO review happened: there is
    no verdict to record and nothing for a human to judge. Nothing is written
    against the review cycle and the node is not held, so the next tick finds it
    due and re-reviews it. Returns False once ``_MAX_REFUSED_ATTEMPTS``
    consecutive refusals have been recorded — the refusal is no longer plausibly
    transient, and the crash path holds the node under its own reason.
    """
    _record_infra_failure_run(
        store,
        config,
        run_id=run_id,
        item_id=item_id,
        issue_num=issue_num,
        error_category=_REFUSED_BEFORE_LAUNCH,
    )
    attempt = _consecutive_refusals(store, item_id)
    if attempt >= _MAX_REFUSED_ATTEMPTS:
        _logger.error(
            "review: %s — the sandbox refused the reviewer before launch %d "
            "times in a row; no longer treating it as transient",
            item_id,
            attempt,
        )
        return False
    _logger.warning(
        "review: %s — the sandbox refused the reviewer before launch "
        "(attempt %d of %d); no review ran, re-reviewing on the next tick",
        item_id,
        attempt,
        _MAX_REFUSED_ATTEMPTS,
    )
    # A wait, not a hold (#3186): nobody is needed until the bound is spent.
    # When the round runs again is the caller's to say — the drive's next tick,
    # or the operator's next `kew review --governed` — so the reason does not.
    outcome.refused[node_id] = (
        f"{REVIEW_RETRYING_WAIT_PREFIX} — {_HOLD_SANDBOX_REFUSED}: the sandbox "
        f"refused the reviewer before launch (attempt {attempt} of "
        f"{_MAX_REFUSED_ATTEMPTS})"
    )
    return True


def _defer_silent_review(
    node_id: str,
    item_id: str,
    issue_num: int,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    run_id: str,
) -> bool:
    """Retry a round the silence watchdog stopped on the next tick (#2240).

    The reviewer's stream said nothing for ``[agent] no_output_minutes`` and
    its container was stopped: a stalled provider, not a verdict. Nothing is
    written against the review cycle and the node is not held. Returns False
    once ``_MAX_SILENT_ATTEMPTS`` such rounds in a row are recorded, and the
    crash path holds the node naming the silence.
    """
    _record_infra_failure_run(
        store,
        config,
        run_id=run_id,
        item_id=item_id,
        issue_num=issue_num,
        error_category=STREAM_SILENT_CATEGORY,
    )
    attempt = _consecutive_refusals(store, item_id, STREAM_SILENT_CATEGORY)
    if attempt >= _MAX_SILENT_ATTEMPTS:
        _logger.error(
            "review: %s — the reviewer's stream went silent %d rounds in a "
            "row; no longer treating it as transient",
            item_id,
            attempt,
        )
        return False
    _logger.warning(
        "review: %s — the reviewer's stream went silent and was stopped "
        "(attempt %d of %d); no verdict, re-reviewing on the next tick",
        item_id,
        attempt,
        _MAX_SILENT_ATTEMPTS,
    )
    outcome.refused[node_id] = (
        f"{REVIEW_RETRYING_WAIT_PREFIX} — the reviewer's stream went silent "
        f"and was stopped ({STREAM_SILENT_CATEGORY}, attempt {attempt} of "
        f"{_MAX_SILENT_ATTEMPTS})"
    )
    return True


#: The waiting reason of a node whose reviewer launch timed out (#3246).
#: Constant, like the rate-limit wait's: a reason that moved per tick would be
#: a new attention state on every poll of one wait.
_LAUNCH_TIMEOUT_WAIT_REASON = (
    f"{REVIEW_RETRYING_WAIT_PREFIX} — a runtime call timed out before the "
    "reviewer started (runtime_timeout); re-reviewing after a back-off"
)


def _timed_out_launches(store: RunStore, item_id: str) -> list[RunRecord]:
    """The review runs a launch timeout ended since the node last got past one.

    Newest first. Any review run with another outcome ends the streak, so the
    bound is spent per incident, as :func:`_consecutive_refusals` reads it.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    streak: list[RunRecord] = []
    for record in reversed(store.list_by_item(item_id)):
        if record.run_kind != ADVERSARIAL_REVIEW_RUN_KIND:
            continue
        if record.error_category != _RUNTIME_TIMEOUT:
            break
        streak.append(record)
    return streak


def _launch_timeout_wait_remaining(
    store: RunStore, item_id: str, *, now: datetime
) -> timedelta | None:
    """What is left of the back-off the node's last timed-out launch earned.

    The wait after the n-th consecutive timeout is ``_LAUNCH_TIMEOUT_RETRY_WAIT``
    doubled n - 1 times, timed from that run's own finish. A spent streak waits
    for nothing: the node is held, and a release relaunches straight away.
    """
    streak = _timed_out_launches(store, item_id)
    if not streak or len(streak) >= _MAX_LAUNCH_TIMEOUT_ATTEMPTS:
        return None
    finished = streak[0].finished_at
    if finished is None:
        return None
    wait = _LAUNCH_TIMEOUT_RETRY_WAIT * 2 ** (len(streak) - 1)
    remaining = finished + wait - now
    return remaining if remaining > timedelta(0) else None


def _defer_for_launch_timeout_wait(
    node_id: str,
    item_id: str,
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
    now: datetime,
) -> bool:
    """Is the node still backing off after a timed-out reviewer launch? (#3246)

    True means this tick launches nothing: a container straight back into a
    daemon that just timed out would only time out again.
    """
    remaining = _launch_timeout_wait_remaining(store, item_id, now=now)
    if remaining is None:
        return False
    _logger.info(
        "review: %s — backing off a timed-out reviewer launch (~%d s left)",
        item_id,
        int(remaining.total_seconds()),
    )
    outcome.refused[node_id] = _LAUNCH_TIMEOUT_WAIT_REASON
    return True


def _defer_timed_out_launch(
    node_id: str,
    item_id: str,
    issue_num: int,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    run_id: str,
) -> bool:
    """Retry a reviewer launch a runtime timeout ended, after a back-off (#3246).

    No reviewer ran: nothing is written against the review cycle and the node
    is not held, so a tick after the back-off re-reviews it. Returns False on
    the ``_MAX_LAUNCH_TIMEOUT_ATTEMPTS``-th consecutive timeout, and the crash
    path holds the node ``review_launch_failed`` naming the timeout.
    """
    _record_infra_failure_run(
        store,
        config,
        run_id=run_id,
        item_id=item_id,
        issue_num=issue_num,
        error_category=_RUNTIME_TIMEOUT,
    )
    attempt = len(_timed_out_launches(store, item_id))
    if attempt >= _MAX_LAUNCH_TIMEOUT_ATTEMPTS:
        _logger.error(
            "review: %s — a runtime call timed out at reviewer launch %d times "
            "in a row; no longer treating it as a slow host",
            item_id,
            attempt,
        )
        return False
    _logger.warning(
        "review: %s — a runtime call timed out before the reviewer started "
        "(attempt %d of %d); re-reviewing after ~%d s",
        item_id,
        attempt,
        _MAX_LAUNCH_TIMEOUT_ATTEMPTS,
        int((_LAUNCH_TIMEOUT_RETRY_WAIT * 2 ** (attempt - 1)).total_seconds()),
    )
    outcome.refused[node_id] = _LAUNCH_TIMEOUT_WAIT_REASON
    return True


def _record_infra_failure_run(
    store: RunStore,
    config: DispatchConfig,
    *,
    run_id: str,
    item_id: str,
    issue_num: int,
    error_category: str,
) -> None:
    """Guarantee the failure left a terminal review row under *error_category*.

    That row is the only evidence the retry reads — the refusal bound (#2147)
    counts it, and the rate-limit wait (#2219) times from its ``finished_at``.
    The production launcher already wrote one for this run; a launcher that did
    not must never make the failure invisible to them.
    """
    existing = store.get(run_id)
    if existing is not None and existing.error_category == error_category:
        return
    _record_dispatch_failure(
        store,
        config,
        run_id=run_id,
        item_id=item_id,
        issue_num=issue_num,
        error_category=error_category,
    )


#: The waiting reason a node under a reviewer usage limit reports (#2219).
#:
#: Named ``review_rate_limited`` and never ``review_unparseable``: no reviewer
#: verdict exists, and the operator reading this line has nothing to repair.
#:
#: Deliberately CONSTANT per token — no countdown, no attempt number, nothing
#: that moves per tick. The reason is part of what makes an attention event a
#: distinct state (``AttentionEvent.state_key``), so a reason carrying
#: "re-reviewing in ~N min" made every poll a new state and `kew epic watch
#: --follow` emitted one event per minute of a single benign wait. How long is
#: left is a log line's job, not an operator-facing state's.
#:
#: It ends with the token the limited round ran under (#3330), by env var and
#: fingerprint, so the operator knows which token to raise; one token reads as
#: one state. :func:`_rate_limit_wait_reason` fills ``{reviewer}`` and
#: ``{token}`` in, and falls back to the SHORT wording when a token named by
#: two env vars would run the line past the echo cap.
_RATE_LIMIT_WAIT_REASON = (
    f"{REVIEW_RATE_LIMITED_WAIT_PREFIX} — a usage limit ended the {{reviewer}}; "
    "re-reviewing once it resets: {token}"
)
_RATE_LIMIT_WAIT_REASON_SHORT = (
    f"{REVIEW_RATE_LIMITED_WAIT_PREFIX} — a usage limit ended the reviewer; "
    "re-reviewing at reset: {token}"
)


def _limited_reviewer(credential: RunCredential | None) -> tuple[str, str]:
    """The limited reviewer and the token it ran under, as a reason names them;
    no *credential* (no record of the round's) is ``unknown credential``."""
    if credential is None:
        return "reviewer", refused_token((), "")
    return f"{credential.runner} reviewer", credential.token()


def _rate_limit_wait_reason(credential: RunCredential | None) -> str:
    """:data:`_RATE_LIMIT_WAIT_REASON` for the round's *credential*."""
    reviewer, token = _limited_reviewer(credential)
    return first_that_fits(
        _RATE_LIMIT_WAIT_REASON.format(reviewer=reviewer, token=token),
        _RATE_LIMIT_WAIT_REASON_SHORT.format(token=token),
    )


def _limited_round_credential(
    store: RunStore, run_id: str | None
) -> RunCredential | None:
    """The credential the reviewer round *run_id* ran under, as
    :func:`_defer_rate_limited_review` recorded it — never the environment's
    at a later tick, which a restarted driver may have swapped (#3330). None
    when the round left no record."""
    return read_run_credential(store, run_id) if run_id else None


def _held_by_merge_claim(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    head: str,
    *,
    action: str,
    outcome: ReviewStageOutcome,
    store: RunStore,
    now: datetime,
) -> bool:
    """Whether an operator's merge claim on *head* holds *action* back (#3198).

    True, with the node's wait recorded as ``merge_claimed``, while a live
    claim holds the PR at *head*: anything that pushes onto it — a
    remediation, the merge-main step — would move the head the verify is
    testing. Not a hold: the claim ends with the verify, or lapses one TTL
    after a killed one, and the first tick after that takes the action.
    """
    from kew.merge_claim import merge_claim_wait_reason

    if facts.pr_number is None:
        return False
    claim = store.live_merge_claim(facts.pr_number, head, now=now)
    if claim is None:
        return False
    _logger.info(
        "review: %s is due a %s, but %s claims PR #%s at %s for a merge "
        "verify until %s — not launched",
        item_id,
        action,
        claim.owner,
        facts.pr_number,
        head[:12],
        claim.expires_at.isoformat(timespec="seconds"),
    )
    outcome.refused[node_id] = merge_claim_wait_reason(claim, action=action)
    return True


def _pushing_action(node: _DueNode, status: DriverStatus | None) -> str | None:
    """The push a due node's action makes, when the tick can tell it will.

    The merge-main step when the head stopped merging (it outranks every review
    action), else the remediation a REMEDIATING node owes. A reviewer round
    pushes nothing, so a claimed head is still reviewed.
    """
    if base_integration_due(node.facts):
        return "merge-main step"
    if not node.integration_only and status is DriverStatus.REMEDIATING:
        return "remediation"
    return None


def _unclaimed(
    due: list[_DueNode],
    statuses: dict[str, DriverStatus],
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    epic_ref: str,
    store: RunStore,
    now: datetime | None,
) -> list[_DueNode]:
    """*due* without the nodes whose push an operator's merge claim holds back,
    or whose merge-main step an operator-owned head leaves alone (#3200).

    Asked in the tick, before a detached child is reserved or spawned (#3198):
    a child would only find the claim, record its wait in an outcome nobody
    reads, and exit — so every tick of a long verify spent a review slot, a run
    row and a process, and the node read as "review stage running detached".
    The node's wait is recorded here instead; the child's own check stays as
    the second line, for a claim taken after this.
    """
    when = now or datetime.now(UTC)
    kept: list[_DueNode] = []
    for node in due:
        action = _pushing_action(node, statuses.get(node.node_id))
        if action == "merge-main step":
            held = _left_to_operator(
                node.node_id,
                node.item_id,
                node.facts,
                outcome=outcome,
                config=config,
                epic_ref=epic_ref,
                store=store,
                now=when,
            )
        else:
            held = action is not None and _held_by_merge_claim(
                node.node_id,
                node.item_id,
                node.facts,
                node.facts.head_oid or "",
                action=action,
                outcome=outcome,
                store=store,
                now=when,
            )
        if not held:
            kept.append(node)
    return kept


def _rate_limit_exhausted_hold_reason(credential: RunCredential | None = None) -> str:
    """The hold a limit that stopped looking transient earns (#2219).

    The wait above is nobody's job while a provider window resets. Past
    ``_MAX_RATE_LIMIT_WAIT`` it is, because nothing on this host will clear it:
    the reason says no reviewer round got past the limit in that long and ends
    with the token the last round ran under (*credential*, #3330; None, no
    record of it: ``unknown credential``). The token is what the operator acts
    on, so it is never what the echo cap cuts: a token named by every env var
    its runner reads keeps the line whole by dropping the runner's name instead.
    """
    hours = int(_MAX_RATE_LIMIT_WAIT.total_seconds() // 3600)
    reviewer, token = _limited_reviewer(credential)
    return safe_hold_reason(
        first_that_fits(
            f"{_HOLD_RATE_LIMIT_EXHAUSTED}: no {reviewer} round "
            f"got past a usage limit for {hours} h: {token}",
            f"{_HOLD_RATE_LIMIT_EXHAUSTED}: no reviewer round got past a usage "
            f"limit for {hours} h: {token}",
        )
    )


def _rate_limited_rounds(store: RunStore, item_id: str) -> list[RunRecord]:
    """Every reviewer round a usage limit has ended since the node last got past
    one. Newest first.

    One thing ends the run: any review run that got past the provider — a
    collected verdict, a budget refusal, a pre-launch refusal — so the wait and
    the bounds on it are spent per limit incident rather than once per node
    lifetime (the same reading ``_consecutive_refusals`` takes).

    This is what ``_MAX_RATE_LIMIT_ROUNDS`` counts: no gap splits it, because a
    count needs no assumption about how often the driver ticks.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    rounds: list[RunRecord] = []
    for record in reversed(store.list_by_item(item_id)):
        if record.run_kind != ADVERSARIAL_REVIEW_RUN_KIND:
            continue
        if record.error_category != _RATE_LIMITED:
            break
        rounds.append(record)
    return rounds


def _live_streak(rounds: list[RunRecord]) -> list[RunRecord]:
    """The leading rounds of *rounds* a LIVE retry cadence could have produced.

    A gap wider than ``_RATE_LIMIT_STREAK_GAP`` between two limited rounds ends
    it: no retry cadence leaves one; a driver that was not running does, and
    including it charged a fresh limit for hours it never rejected anything in.
    The wall-clock span bound is read off this, never off the whole run.
    """
    streak: list[RunRecord] = []
    for record in rounds:
        if streak and _idle_gap(record, streak[-1]) > _RATE_LIMIT_STREAK_GAP:
            break
        streak.append(record)
    return streak


def _rate_limited_streak(store: RunStore, item_id: str) -> list[RunRecord]:
    """The usage-limited review runs of the node's CURRENT limit incident."""
    return _live_streak(_rate_limited_rounds(store, item_id))


def _idle_gap(older: RunRecord, newer: RunRecord) -> timedelta:
    """How long nothing limited this node between two adjacent limited rounds.

    From the older round's finish to the newer round's start. A round that never
    finalized has no finish to measure from, so its start stands in: that can
    only make the gap look WIDER, which splits an incident rather than welding
    two together. Safe in the direction that matters — a round with no finish
    gets no wait either (``_rate_limit_wait_remaining``), so the relaunch loop
    the bound contains leaves rounds minutes apart, nowhere near this gap.
    """
    ended = older.finished_at or older.started_at
    return newer.started_at - ended


def _rate_limited_span(streak: list[RunRecord]) -> timedelta:
    """How long this limit incident has been rejecting reviewer rounds.

    Both ends come from the node's own run rows — the first limited round's
    start to the newest one's finish — so the span needs no clock of its own and
    its two ends can never be read off different ones.

    Zero for an empty streak: the round's own row is written before this is
    read, so an empty one means the store could not show it back, and that is
    the start of an incident rather than the end of a long one. Never an
    ``IndexError`` — a bound this far from the verdict may not be what takes a
    driver tick down.
    """
    if not streak:
        return timedelta(0)
    newest = streak[0].finished_at or streak[0].started_at
    return newest - streak[-1].started_at


def _rate_limit_wait_remaining(
    store: RunStore, item_id: str, *, now: datetime
) -> tuple[timedelta, RunRecord] | None:
    """What is left of the wait the node's last rate-limited round earned, and
    that round; else None.

    Only the NEWEST review run counts: any later review run that reached the
    provider at all proves the limit is over, and a wait is never carried over
    from one incident to the next.
    """
    streak = _rate_limited_streak(store, item_id)
    if not streak or streak[0].finished_at is None:
        return None
    remaining = streak[0].finished_at + _RATE_LIMIT_RETRY_WAIT - now
    return (remaining, streak[0]) if remaining > timedelta(0) else None


def _defer_rate_limited_review(
    node_id: str,
    item_id: str,
    issue_num: int,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    run_id: str,
) -> bool:
    """Record a reviewer a usage limit cut off as retryable infrastructure (#2219).

    No hold, no round, no verdict: the reviewer never finished judging anything.
    The node keeps its review status, so the tick after the wait re-reviews it.

    Returns False once this limit incident has been rejecting rounds for longer
    than ``_MAX_RATE_LIMIT_WAIT``, or has rejected ``_MAX_RATE_LIMIT_ROUNDS`` of
    them — the limit is no longer plausibly a window resetting, and the crash
    path holds the node under its own reason. Those bounds are the only thing
    that ever reaches a person: the waiting node carries no hold row, so no
    stall clock can see it. The count is what reaches it on a driver whose
    cadence is wider than one streak gap (cron, a long ``--interval``), where
    every pair of rounds reads as a separate incident and the span never grows.
    """
    _record_infra_failure_run(
        store,
        config,
        run_id=run_id,
        item_id=item_id,
        issue_num=issue_num,
        error_category=_RATE_LIMITED,
    )
    # The round launched and was collected in this tick, from this process's
    # environment: that is the token the limit refused (#3330), and the wait
    # and the hold name it, whatever a later tick's environment holds.
    from kew.runners.registry import run_credential

    credential = run_credential(_review_runner_name(config))
    record_run_credential(store, run_id, credential)
    # Counts the row just stamped, so a first limited round reads a zero span.
    rounds = _rate_limited_rounds(store, item_id)
    span = _rate_limited_span(_live_streak(rounds))
    if span >= _MAX_RATE_LIMIT_WAIT or len(rounds) >= _MAX_RATE_LIMIT_ROUNDS:
        _logger.error(
            "review: %s — a provider usage limit has ended every reviewer round "
            "for %d h (%d rounds); no longer treating it as a window that resets",
            item_id,
            int(_rate_limited_span(rounds).total_seconds() // 3600),
            len(rounds),
        )
        return False
    _logger.warning(
        "review: %s — a provider usage limit ended the reviewer; no verdict and "
        "no round consumed, re-reviewing in ~%d min (limited for %d min of %d, "
        "round %d of %d)",
        item_id,
        int(_RATE_LIMIT_RETRY_WAIT.total_seconds() // 60),
        int(span.total_seconds() // 60),
        int(_MAX_RATE_LIMIT_WAIT.total_seconds() // 60),
        len(rounds),
        _MAX_RATE_LIMIT_ROUNDS,
    )
    outcome.refused[node_id] = _rate_limit_wait_reason(credential)
    return True


def _defer_for_rate_limit_wait(
    node_id: str,
    item_id: str,
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
    now: datetime,
) -> bool:
    """Is the node still waiting out a reviewer usage limit? (#2219)

    True means this tick takes NO review action: launching straight back into a
    live limit spends a container, a materialized checkout and a provisioned
    environment to be cut off again. The wait is reported, never held — the
    round that ends it decides whether the incident has earned a hold yet.

    A different token is a different account window (#3330), as for a runner
    quota hold: once the environment carries a known credential other than the
    one the limited round ran under, the wait ends and this tick re-reviews.
    """
    from kew.runners.registry import runner_credential_fingerprint

    wait = _rate_limit_wait_remaining(store, item_id, now=now)
    if wait is None:
        return False
    remaining, limited = wait
    credential = _limited_round_credential(store, limited.run_id)
    if credential is not None and credential.replaced_by(
        runner_credential_fingerprint(credential.runner)
    ):
        _logger.info(
            "review: %s — the reviewer's agent token changed since a usage limit "
            "ended its last round; re-reviewing now",
            item_id,
        )
        return False
    _logger.info(
        "review: %s — waiting out a provider usage limit (~%d s left) before "
        "the next reviewer round",
        item_id,
        int(remaining.total_seconds()),
    )
    outcome.refused[node_id] = _rate_limit_wait_reason(credential)
    return True


def _defer_for_running_verify(
    node_id: str,
    item_id: str,
    *,
    epic_ref: str,
    outcome: ReviewStageOutcome,
    store: RunStore,
    config: DispatchConfig,
    kind: str = "review",
) -> bool:
    """Does a running local merge verify defer this *kind* launch? (#3190)

    The spawn guard every review-stage launch passes — a reviewer, a
    remediation, a merge-main step: read at the launch, not at the tick's
    admission, so a verify that began mid-tick defers it too. The node waits
    with the deferral as its reason, unheld, and the next tick after the
    verify launches it. A hand-started ``kew review`` is never deferred; its
    command warns instead.
    """
    from kew.standalone_review import STANDALONE_REVIEW_EPIC_REF

    if epic_ref == STANDALONE_REVIEW_EPIC_REF:
        return False
    reason = verify_deferral(store, config, item_id=item_id, kind=kind)
    if reason is None:
        return False
    outcome.refused[node_id] = reason
    return True


def _launchable_reviewer(
    node_id: str,
    item_id: str,
    *,
    launcher: ReviewerLauncher | None,
    epic_ref: str,
    outcome: ReviewStageOutcome,
    store: RunStore,
    config: DispatchConfig,
    now: datetime,
) -> ReviewerLauncher | None:
    """The launcher a due reviewer round runs on, or ``None`` when it cannot
    launch this tick: a usage-limit or launch-timeout wait, no launcher
    wired, or a running local merge verify (#3190)."""
    if _defer_for_rate_limit_wait(
        node_id, item_id, outcome=outcome, store=store, now=now
    ) or _defer_for_launch_timeout_wait(
        node_id, item_id, outcome=outcome, store=store, now=now
    ):
        return None
    if launcher is None:
        _logger.warning(
            "review: %s is due a reviewer but no launcher is wired — no verdict "
            "will be produced (the merge gate stays unfed)",
            item_id,
        )
        outcome.unwired.append(node_id)
        return None
    if _defer_for_running_verify(
        node_id, item_id, epic_ref=epic_ref, outcome=outcome, store=store, config=config
    ):
        return None
    return launcher


def _crash_hold_reason(error_category: str | None) -> str:
    """The hold a crashed review round earns. A failure that landed before the
    reviewer started is held under its own reason (#2043/#2147) — an operator
    reading ``review_unparseable`` goes looking for a reviewer verdict that
    never existed, and the real fault is a layer below it."""
    return _CRASH_HOLDS.get(error_category or "", _HOLD_UNPARSEABLE)


def _crash_hold_reason_with_fix(
    hold_reason: str,
    failure_layer: str | None,
    config: DispatchConfig,
    *,
    container: str,
    credential: RunCredential | None,
    error_category: str | None = None,
    refusal: str | None = None,
) -> str:
    """The durable reason written for a crashed round.

    A reviewer that never started says so, and names the fix for the layer that
    refused: the incident's cause was an absent agent image, whose whole fix is
    one ``kew sandbox build`` away, and the sandbox layer's own refusal line
    scrolls past with the tick that printed it. A reviewer that DID run holds
    with no remedy — the stored sidecar is what a human reads — but with what
    kew knows was wrong (*refusal*, #3186): ``review_unparseable`` is bare only
    when kew knows nothing more.
    """
    if hold_reason == _HOLD_UNPARSEABLE:
        return safe_hold_reason(f"{hold_reason}: {refusal}") if refusal else hold_reason
    if hold_reason == _HOLD_RATE_LIMIT_EXHAUSTED:
        return _rate_limit_exhausted_hold_reason(credential)
    if hold_reason in _STREAM_REFUSAL_HOLDS:
        return safe_hold_reason(
            f"{hold_reason}: {refusal or 'the reviewer stream carried no verdict'}"
        )
    remedy = (
        _CATEGORY_REMEDY.get(error_category or "")
        or _PRE_LAUNCH_REMEDY.get(failure_layer or "", _PRE_LAUNCH_REMEDY_FALLBACK)
    ).format(
        runner=_review_runner_name(config),
        container=container,
        runtime=config.sandbox.runtime,
        attempts=_MAX_LAUNCH_TIMEOUT_ATTEMPTS,
    )
    return safe_hold_reason(f"{hold_reason}: the reviewer never started — {remedy}")


def _reviewer_container(store: RunStore, run_id: str | None, item_id: str) -> str:
    """The container the reviewer's run row names, else the one it is given."""
    stored = store.get(run_id) if run_id else None
    return (stored.container_name if stored else None) or f"kew-{branch_slug(item_id)}"


def _review_runner_name(config: DispatchConfig) -> str:
    """The runner the reviewer would have run as, from the launcher's own single
    resolution site (#1163) — a hold that names a build command must name the
    runner that command has to build for."""
    from kew.graph.driver.review_launcher import review_runner

    return review_runner(config)


def _live_review_items(store: RunStore) -> set[str]:
    """Item ids that currently hold a LIVE reservation under the review
    pseudo-epic — a reviewer/remediator already in flight, which this pass must
    not double-dispatch (the human-race handler owns those nodes)."""
    return {res.item_id for res in store.list_epic_reservations(REVIEW_EPIC_REF)}


#: The event a tick records once per hand-run ``kew review`` it found holding a
#: review node's item, and so launched nothing for (#3208).
REVIEW_DEFERRED_TO_HAND_RUN = "review_deferred_to_hand_run"


def _report_hand_run_holders(
    statuses: dict[str, DriverStatus],
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
    provider: str,
) -> None:
    """Name the hand-run ``kew review`` that holds a review node's item (#3208).

    Its reservation keeps the node out of the due pass, so without this the
    tick would launch nothing and say nothing about why. One event per holder
    run records the deferral; meanwhile the node waits under a line naming the
    holder, and the merge pass leaves it alone, since the review may move its
    head.
    """
    from kew.review_occupancy import hand_run_holder

    live = {res.item_id: res for res in store.list_epic_reservations(REVIEW_EPIC_REF)}
    for node_id, item_id, _ in _review_items(statuses, provider):
        reservation = live.get(item_id)
        holder = None if reservation is None else hand_run_holder(store, reservation)
        if reservation is None or holder is None:
            continue
        outcome.detached[node_id] = f"review deferred: {holder}"
        store.append_audit_event_once(
            dedup_key=f"{REVIEW_DEFERRED_TO_HAND_RUN}:{item_id}:{reservation.run_id}",
            timestamp=datetime.now(UTC).isoformat(),
            event=REVIEW_DEFERRED_TO_HAND_RUN,
            run_id=reservation.run_id,
            item_id=item_id,
            actor_type="policy",
            actor_id="review_stage",
            payload={"node_id": node_id, "holder_run_id": reservation.run_id},
        )


def drive_review_stage(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    reviewer_launcher: ReviewerLauncher | None,
    remediator: Remediator | None = None,
    base_integrator: BaseIntegrator | None = None,
    worker_cap: int = 0,
    max_concurrent: int = 1,
    now: datetime | None = None,
    detacher: ReviewDetacher | None = None,
) -> ReviewStageOutcome:
    """Drive the adversarial-review lifecycle for every review-enabled node.

    Runs after the human-race handler and before the merge pass. Mutates
    ``statuses`` in place for nodes it holds this tick (so the merge pass sees the
    HELD candidate and the blocking invariant gates it); records verdicts so the
    NEXT tick's overlay reads the transition (READY → in_review, NEEDS_CHANGES →
    remediating, round cap → HELD).

    Each due node gets at most one action per tick. With ``max_concurrent`` above
    one, up to that many nodes run at once (#2110): see
    :func:`_drive_concurrently`. At one, nodes run in order on this thread.

    Before any of that it finishes a half-written acceptance on a HELD node
    (:func:`_mark_held_acceptances`) — the one review write the lifecycle itself
    can no longer reach, because a hold outranks the lifecycle status the due
    pass keys on.

    With a *detacher* wired (#2459) no node's action runs on this tick: each
    due node is reserved, recorded and handed to a detached child
    (:func:`kew.graph.driver.review_detach.launch_detached`), and the tick
    returns without waiting on any of them.
    """
    outcome = ReviewStageOutcome()
    if not config.review.adversarial:
        return outcome
    provider = provider_from_config(config)
    _mark_held_acceptances(
        statuses,
        outcome=outcome,
        config=config,
        store=store,
        repo=repo,
        provider=provider,
    )
    _release_readable_body_holds(
        statuses,
        node_facts,
        epic_ref=epic_ref,
        store=store,
        repo=repo,
        provider=provider,
    )
    due = _due_nodes(
        statuses, node_facts, epic_ref=epic_ref, store=store, provider=provider
    )
    _report_hand_run_holders(statuses, outcome=outcome, store=store, provider=provider)
    if detacher is not None:
        due = _unclaimed(
            due,
            statuses,
            outcome=outcome,
            config=config,
            epic_ref=epic_ref,
            store=store,
            now=now,
        )
        in_flight = report_in_flight(
            (
                (node_id, item_id)
                for node_id, item_id, _ in _review_items(statuses, provider)
            ),
            outcome=outcome,
            store=store,
        )
        launch_detached(
            due,
            detacher,
            outcome=outcome,
            epic_ref=epic_ref,
            config=config,
            store=store,
            worker_cap=worker_cap,
            max_concurrent=max_concurrent,
            in_flight=in_flight,
        )
        return outcome
    if max_concurrent <= 0:
        # Admission closed by `kew drive` (#2616): nothing new starts.
        return outcome
    drive = partial(
        _drive_one_node,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        repo=repo,
        repo_root=repo_root,
        base=base,
        worker_cap=worker_cap,
        now=now or datetime.now(UTC),
    )
    if max_concurrent > 1 and len(due) > 1 and _siblings_available(store):
        _drive_concurrently(
            due,
            drive,
            statuses=statuses,
            outcome=outcome,
            store=store,
            reviewer_launcher=reviewer_launcher,
            remediator=remediator,
            base_integrator=base_integrator,
            max_concurrent=max_concurrent,
        )
        return outcome
    for node in due:
        drive(
            node.node_id,
            node.item_id,
            node.facts,
            statuses=statuses,
            outcome=outcome,
            store=store,
            reviewer_launcher=reviewer_launcher,
            remediator=remediator,
            base_integrator=base_integrator,
            integration_only=node.integration_only,
        )
    return outcome


def drive_review_node(
    item_id: str,
    facts: NodeFacts,
    *,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    reviewer_launcher: ReviewerLauncher | None,
    remediator: Remediator | None = None,
    worker_cap: int = 0,
    now: datetime | None = None,
) -> ReviewStageOutcome:
    """Take the one review action a single item owes, outside an epic tick.

    The per-node action :func:`drive_review_stage` takes for each due node —
    a reviewer round, or a remediation of findings standing on this head, with
    the same round cap, stop rule and verdict surfaces — for a PR no epic
    drives (#2556). The caller owns the checks the epic's overlay makes for a
    tick: the item is not held and no reviewer holds it. No merge-main step is
    wired, so a head that no longer merges reads as ``unwired`` rather than
    being integrated.
    """
    outcome = ReviewStageOutcome()
    _drive_one_node(
        item_id,
        item_id,
        facts,
        statuses={item_id: DriverStatus.ADVERSARIAL_REVIEW},
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider_from_config(config),
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        base=base,
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=None,
        integration_only=False,
        worker_cap=worker_cap,
        now=now or datetime.now(UTC),
    )
    return outcome


def drive_detached_node(
    review: DetachedReview,
    *,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    reviewer_launcher: ReviewerLauncher | None = None,
    remediator: Remediator | None = None,
    base_integrator: BaseIntegrator | None = None,
    worker_cap: int = 0,
    now: datetime | None = None,
) -> bool:
    """Take a detached child's one node action (#2459) — the synchronous one.

    The same :func:`_drive_one_node` a tick would have run for this node, with
    the same seams, against the facts the tick read — under the reservation
    the tick took (:func:`kew.graph.driver.review_detach.run_detached_review`).
    Returns False when that reservation is no longer the child's to use.
    """
    drive = partial(
        _drive_one_node,
        epic_ref=review.epic_ref,
        provider=provider_from_config(config),
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        base=base,
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=base_integrator,
        worker_cap=worker_cap,
        now=now or datetime.now(UTC),
    )
    return run_detached_review(review, drive=drive, store=store)


@dataclass(frozen=True)
class _DueNode:
    node_id: str
    item_id: str
    facts: NodeFacts
    integration_only: bool = False
    """The node owes the merge-main step and NOTHING else (#2149): its review
    verdict stands on the current head (IN_REVIEW), or it is held and owes no
    review action at all, so there is no reviewer or remediator round due for
    it. See :func:`_due_nodes`."""


#: The two statuses that owe a review ACTION — a reviewer round or a remediation.
_REVIEW_LIFECYCLE = (DriverStatus.ADVERSARIAL_REVIEW, DriverStatus.REMEDIATING)

#: Statuses that owe ONLY the merge-main step, never a review action (#2149).
#: IN_REVIEW is a verdict standing on the current head. HELD is admitted for one
#: reason and one only — see :func:`_integration_only_due`.
_INTEGRATION_ONLY = (DriverStatus.IN_REVIEW, DriverStatus.HELD)


def _integration_only_due(
    status: DriverStatus, facts: NodeFacts, held_reason: str | None
) -> bool:
    """Is this non-lifecycle node due the merge-main step, and nothing else?

    Only when its head actually stopped merging, and — for a HELD node — only
    under ``ci_pending``. That hold is the merge pass's own, means exactly "the
    PR is not mergeable right now", and re-derives itself from
    ``mergeStateStatus`` on every pass; a node already carrying it when its
    branch goes DIRTY would otherwise read HELD forever, because HELD is a
    status this pass used to skip and the pass's answer to DIRTY is that same
    hold again. That is the wedge #2149 exists to remove, one cause further
    back. Every other honored hold is somebody else's and un-holds by hand:
    running a governed ``kew resolve`` against a node an operator (or the
    step's own bound) parked is exactly the retry those holds exist to stop.

    Such a hold refuses the step whatever *status* says (#2869): the reasons
    are read from the store as this pass runs, while the status was overlaid
    earlier in the tick, so a detached resolver's ``base_integration_*`` hold
    written in between leaves an IN_REVIEW status the store already outranks.
    """
    if not base_integration_due(facts):
        return False
    if is_another_writers_hold(held_reason):
        return False
    return status is not DriverStatus.HELD or is_ci_pending_hold(held_reason)


def _due_nodes(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    epic_ref: str,
    store: RunStore,
    provider: str,
) -> list[_DueNode]:
    """Review-stage nodes that owe an action this tick, in node order.

    Two populations, and the second is the whole point of #2149. The review
    lifecycle proper (ADVERSARIAL_REVIEW / REMEDIATING) owes a reviewer or a
    remediator. But the branch the incident was about — a READY one that fell
    behind ``main`` — is not in either: a READY verdict bound to the current
    head overlays to IN_REVIEW (``overlay._persisted_verdict_status`` returns
    None for it), so keying the pass on the lifecycle alone left exactly the
    node the merge-main step exists for unreachable, stuck on the merge pass's
    ``ci_pending`` hold with no action taken. An IN_REVIEW node — or one already
    held on that same ``ci_pending`` — is therefore due too, but ONLY when its
    head stopped merging, and only for that one step: it is carried as
    ``integration_only`` so no reviewer or remediation is dispatched against a
    verdict that already stands, or against a node somebody parked.
    """
    admit = partial(
        _due_node,
        node_facts=node_facts,
        live=_live_review_items(store),
        held_reasons={h.item_id: h.reason for h in store.list_epic_holds(epic_ref)},
    )
    candidates = (
        admit(node_id, item_id, status)
        for node_id, item_id, status in _review_items(statuses, provider)
    )
    return [node for node in candidates if node is not None]


def _review_items(
    statuses: dict[str, DriverStatus], provider: str
) -> Iterator[tuple[str, str, DriverStatus]]:
    """``(node_id, item_id, status)`` for every review-stage node, in node order.

    The two populations of :func:`_due_nodes` and nothing else; whether a node
    actually owes an action this tick is :func:`_due_node`'s question.
    """
    for node_id in sorted(statuses):
        status = statuses[node_id]
        if status not in _REVIEW_LIFECYCLE and status not in _INTEGRATION_ONLY:
            continue
        try:
            item_id = validate_item_id(node_id, provider)
        except InvalidItemIdError:
            continue
        yield node_id, item_id, status


def _due_node(
    node_id: str,
    item_id: str,
    status: DriverStatus,
    *,
    node_facts: dict[str, NodeFacts],
    live: set[str],
    held_reasons: dict[str, str],
) -> _DueNode | None:
    """The action this node owes, or None when it owes none — see
    :func:`_due_nodes`."""
    facts = node_facts.get(node_id)
    # A live reviewer/remediator holds the slot, or the PR has no head yet:
    # nothing for this pass to do (in-flight → collected within its dispatch;
    # headless → wait for the next tick's fresh facts).
    if item_id in live or facts is None or facts.head_oid is None:
        return None
    integration_only = status not in _REVIEW_LIFECYCLE
    if integration_only and not _integration_only_due(
        status, facts, held_reasons.get(item_id)
    ):
        return None
    return _DueNode(node_id, item_id, facts, integration_only)


def _mark_held_acceptances(
    statuses: dict[str, DriverStatus],
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    provider: str,
) -> None:
    """Finish a half-written acceptance on a node the lifecycle cannot reach (#2151).

    ``acceptance_on_record`` needs BOTH the chain event and the round's ledger
    mark, and the stage re-marks a missing one on every tick that reads the
    acceptance — but only for a node it drives, and :func:`_due_nodes` drives
    ADVERSARIAL_REVIEW and REMEDIATING alone. A hold outranks both at overlay
    precedence 1, so a node carrying one (``ci_pending`` is the everyday case)
    whose acceptance is attested but unmarked — a crash between the two writes,
    or any pre-v59 acceptance — wedged permanently: nothing retried the mark, and
    the merge gate refused the node before ``_classify_open`` reached the CI
    check that releases the hold. HELD is the one status that persists by itself;
    the others are terminal, merge, or fall back into the lifecycle next tick.

    The mark is a ledger-only write bound to the round, head and digest the chain
    already names, so this takes no reservation (the non-idempotent step is the
    ``gh issue create`` it never runs) and dispatches nothing. The node stays
    HELD with its hold intact — what changes is that its two readers agree again,
    so the hold clears on its own terms.
    """
    for node_id in sorted(statuses):
        if statuses[node_id] != DriverStatus.HELD:
            continue
        try:
            item_id = validate_item_id(node_id, provider)
        except InvalidItemIdError:
            continue
        latest = store.latest_review_round(item_id)
        issue = unmarked_acceptance(store, item_id, latest)
        if latest is None or issue is None:
            continue
        _mark_held_round(
            node_id,
            item_id,
            latest,
            issue=issue,
            outcome=outcome,
            config=config,
            store=store,
            repo=repo,
        )


def _mark_held_round(
    node_id: str,
    item_id: str,
    latest: ReviewRound,
    *,
    issue: int,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """Mark *latest* accepted for a HELD node, and publish the round's status.

    The same two records the lifecycle path writes (:func:`_accept_round`), minus
    the filing the chain already carries: a mark that does not land leaves the
    node exactly as it was — still HELD, still unmergeable — and says so, for the
    next tick to retry.
    """
    try:
        mark_round_accepted(store, item_id, latest, issue=issue)
    except ResidualFilingError as exc:
        _logger.error(
            "review: %s is HELD carrying an acceptance for round %d the ledger "
            "does not, and the mark did not land (%s) — the node cannot merge "
            "until it lands; the next tick re-reads the acceptance and retries",
            item_id,
            latest.round,
            exc,
        )
        return
    _logger.warning(
        "review: %s round %d: marked the attested acceptance (issue #%d) on a "
        "HELD node the review lifecycle does not drive",
        item_id,
        latest.round,
        issue,
    )
    outcome.residuals[node_id] = issue
    if config.review.publish_status:
        review_status.publish_residuals_accepted(repo, latest.head_sha, issue)


@runtime_checkable
class StoreRebindable(Protocol):
    """A seam that can be rebuilt over another ``RunStore`` handle (#2110)."""

    def bind_store(self, store: RunStore) -> Callable[..., object]: ...


def _siblings_available(store: RunStore) -> bool:
    """True when worker threads can open their own handle on this ledger; an
    in-memory or non-SQLite store keeps the serial path."""
    from kew.run_store import SqliteRunStore

    return isinstance(store, SqliteRunStore) and store.reopenable


def _rebind[Seam](seam: Seam, store: RunStore) -> Seam:
    """The seam bound to *store*; a seam without a store binding runs as given."""
    if isinstance(seam, StoreRebindable):
        return cast("Seam", seam.bind_store(store))
    return seam


def _open_worker_store(store: RunStore, item_id: str) -> SqliteRunStore | None:
    """This worker's own handle on the ledger, or ``None`` when it cannot open one.

    Opening the handle is the one step that sits BEFORE ``_drive_one_node``'s own
    fail-closed boundary, so it carries that boundary itself: a transient ledger
    lock here degrades this node alone — nothing was dispatched and no state
    moved, so the next tick retries it — rather than aborting every other node's
    action this tick.
    """
    try:
        return cast("SqliteRunStore", store).open_sibling()
    except Exception:
        _logger.error(
            "review: %s could not open its own ledger handle — no action this "
            "tick, retried on the next",
            item_id,
            exc_info=True,
        )
        return None


def _drive_concurrently(
    due: list[_DueNode],
    drive: Callable[..., None],
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    store: RunStore,
    reviewer_launcher: ReviewerLauncher | None,
    remediator: Remediator | None,
    base_integrator: BaseIntegrator | None,
    max_concurrent: int,
) -> None:
    """Run each due node's one action on a bounded pool, then merge (#2110).

    Admission stays where it was: every reviewer reserves atomically under the
    fleet cap and checks its own node's review pool inside ``dispatch_reviewer``,
    and every remediation reserves its own node inside ``dispatch_review``. Those
    are per-node rows arbitrated by SQLite, so concurrent workers cannot
    over-admit; the width here is sized by the tick against the fleet cap before
    anything is submitted.

    SQLite handles are thread-affine, so each worker opens its own handle on the
    same ledger and rebinds the production seams to it. Each worker writes its
    own node's status and outcome into private copies, merged back here in node
    order after every worker has finished, so the merge pass sees the same
    statuses and lists the serial path would produce. One worker raising does
    not stop the others: each failure is logged against its node, and the first
    is re-raised after the merge, as the serial path would have raised it. A
    worker that cannot open its handle at all takes no action for its node
    (:func:`_open_worker_store`) and leaves the rest of the tick untouched.
    """

    def _work(node: _DueNode) -> tuple[DriverStatus, ReviewStageOutcome] | None:
        own_statuses = {node.node_id: statuses[node.node_id]}
        own_outcome = ReviewStageOutcome()
        worker_store = _open_worker_store(store, node.item_id)
        if worker_store is None:
            return None
        try:
            with line_scope(node.item_id):
                drive(
                    node.node_id,
                    node.item_id,
                    node.facts,
                    statuses=own_statuses,
                    outcome=own_outcome,
                    store=worker_store,
                    reviewer_launcher=_rebind(reviewer_launcher, worker_store),
                    remediator=_rebind(remediator, worker_store),
                    base_integrator=_rebind(base_integrator, worker_store),
                    integration_only=node.integration_only,
                )
        finally:
            worker_store.close()
        return own_statuses[node.node_id], own_outcome

    with ThreadPoolExecutor(
        max_workers=min(max_concurrent, len(due)), thread_name_prefix="kew-review"
    ) as pool:
        futures = [(node, pool.submit(_work, node)) for node in due]
    first_error: BaseException | None = None
    for node, future in futures:
        error = future.exception()
        if error is not None:
            _logger.error(
                "review: %s review/remediation raised — re-raised once every "
                "other node's action has finished",
                node.item_id,
                exc_info=error,
            )
            outcome.failed.append(node.node_id)
            first_error = first_error or error
            continue
        result = future.result()
        if result is None:  # no handle, so no action: this node alone stands still
            outcome.failed.append(node.node_id)
            continue
        status, own = result
        statuses[node.node_id] = status
        _merge_outcome(outcome, own)
    if first_error is not None:
        raise first_error


def _merge_outcome(into: ReviewStageOutcome, part: ReviewStageOutcome) -> None:
    into.dispatched.extend(part.dispatched)
    into.verdicts.update(part.verdicts)
    into.findings.update(part.findings)
    into.held.extend(part.held)
    into.remediating.extend(part.remediating)
    into.unwired.extend(part.unwired)
    into.failed.extend(part.failed)
    into.residuals.update(part.residuals)
    into.refused.update(part.refused)
    into.integrated.update(part.integrated)
    into.gate_skipped.update(part.gate_skipped)


def _remediation_rounds(store: RunStore, item_id: str) -> int:
    """This cycle's rounds that ``[review] max_rounds`` caps.

    NOT ``review_round_count``: that counts every recorded round, and a
    merge-queue round (#2148) is not a remediation cycle — no reviewer ran, and
    the round IS the finding remediation must answer. Counting it let the
    reviewer's READY plus the queue's NEEDS_CHANGES exhaust the default budget of
    2, so the node converged to ``review_rounds_exhausted`` instead of ever
    remediating the red combined tree. An operator send-back (#3070) restarts
    the count — see :func:`remediation_rounds_spent`.
    """
    return remediation_rounds_spent(store.review_rounds_in_cycle(item_id))


def _drive_standing_findings(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    head: str,
    round_count: int,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    remediator: Remediator | None,
    gate_paths_touched: Callable[[], bool],
    declaration: _StandingDeclaration | None = None,
    now: datetime | None = None,
) -> None:
    """Act on NEEDS_CHANGES findings still standing against the CURRENT head.

    Seven outcomes, in precedence order: the stop rule accepts the round, a
    previous remediation for this head is waiting on a person, a
    declaration-only round's remediation already ran and declared nothing
    (#2872) — or the PR body could not be read to tell, and the node waits a
    tick — any other round's remediation already ran and changed nothing
    (#3233), a remediation at this head pushed nothing and earns no second
    one there (#3475), the round cap is reached, or the node is remediated.
    """
    verdict = _standing_verdict(latest)
    if _apply_stop_rule(
        node_id,
        item_id,
        facts,
        latest,
        verdict,
        # No reviewer is in flight on this path, so the filing takes the
        # node's review reservation itself — unless a detached stage child
        # already holds it for this whole action (#2459).
        reserved=held_reservation() is not None,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        store=store,
        repo=repo,
        gate_paths_touched=gate_paths_touched,
    ):
        return
    deferred = _deferred_remediation(store, item_id, head)
    if deferred is not None:
        # A remediation for this very head already ran and its publication is
        # held for a person: the findings stand because its work never reached
        # the branch, not because nothing was done about them (#2362).
        # Dispatching another one buys nothing, moves the preserved checkout
        # aside, and costs a full remediation run.
        _hold_deferred_publication(
            node_id,
            item_id,
            deferred,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            store=store,
        )
        return
    if declaration is not None and _remediated_since(store, item_id, latest):
        if declaration.missing is None:
            # The body could not be read: whether the lines landed is unknown,
            # not "missing" — a sticky hold on a transient read failure would
            # strand a node a person never needed to touch. Re-read next tick,
            # up to a bound; then hold, and clear the hold on the first read.
            _wait_for_unread_body(
                node_id,
                item_id,
                facts,
                latest,
                statuses=statuses,
                outcome=outcome,
                epic_ref=epic_ref,
                provider=provider,
                store=store,
            )
            return
        # The round asked only for Behavior changes lines, and a remediation
        # ran against this head and left them out of the PR body: another one
        # would end the same way (#2872). A person declares them.
        missing = declaration.missing
        reason = declarations_missing_hold_reason(missing)
        _logger.error(
            "review: %s remediation declared none of the tests its round "
            "asked for — no new remediation dispatched; node HELD: %s; "
            "declare: %s",
            item_id,
            reason,
            ", ".join(missing),
        )
        # The full list rides on the hold (#2884): the reason is capped, and
        # the console reads every id from the row.
        _hold(store, epic_ref, item_id, reason, provider=provider, node_ids=missing)
        statuses[node_id] = DriverStatus.HELD
        outcome.held.append(node_id)
        return
    if _held_for_no_change(
        node_id,
        item_id,
        latest,
        head,
        verdict,
        declared=declaration is not None,
        facts=facts,
        repo=repo,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        store=store,
    ):
        return
    if _held_for_no_progress(
        node_id,
        item_id,
        latest,
        head,
        facts=facts,
        repo=repo,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        store=store,
    ):
        return
    if _remediation_rounds(store, item_id) >= config.review.max_rounds:
        _converge(
            node_id,
            item_id,
            head,
            round_count,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
            repo=repo,
        )
        return
    body = _remember_proposals(store, item_id, latest, facts, repo=repo)
    _remediate(
        node_id,
        item_id,
        facts,
        head,
        round_count,
        sidecar_path=latest.sidecar_path,
        criterion_ids=recorded_criterion_ids(latest.criteria_json),
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        store=store,
        remediator=remediator,
        declaration=_request_of(declaration),
        proposal=_proposal_of(declaration, verdict, body),
        now=now or datetime.now(UTC),
    )


def _proposal_of(
    declaration: _StandingDeclaration | None,
    verdict: ParsedVerdict | None,
    body: str | None,
) -> DeclarationRequest | None:
    """The lines an ordinary round asks its remediation to propose, when its
    findings ask for nothing else (#3493)."""
    if declaration is not None or verdict is None:
        return None
    return proposal_request(verdict.findings, body)


def _request_of(declaration: _StandingDeclaration | None) -> DeclarationRequest | None:
    """The request a remediation of a declaration round is briefed with."""
    return None if declaration is None else declaration.request


def _held_for_no_change(
    node_id: str,
    item_id: str,
    latest: ReviewRound,
    head: str,
    verdict: ParsedVerdict | None,
    *,
    declared: bool,
    facts: NodeFacts,
    repo: str,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    store: RunStore,
) -> bool:
    """Hold a node whose remediation of the standing round moved nothing (#3233).

    The findings stand where they were filed, and another remediation reads
    the same brief and ends the same way, once per tick. Nothing else bounds
    it: a no-op remediation records no review round, so the round cap never
    comes. The reason names how many findings stand and the first of them.

    A declaration-only round (*declared*) has its own hold, which the caller
    has already decided on.
    """
    no_op = None if declared else _no_change_outcome(store, item_id, latest)
    if no_op is None:
        return False
    summaries = tuple(f.summary for f in verdict.findings) if verdict else ()
    published = str(no_op.get("head_sha") or "")
    reason = remediation_no_change_hold_reason(
        summaries, moved_to=head if published and published != head else None
    )
    _logger.error(
        "review: %s remediation changed nothing on %s — no new remediation "
        "dispatched; node HELD: %s",
        item_id,
        head[:12],
        reason,
    )
    _hold(store, epic_ref, item_id, reason, provider=provider)
    _remember_held_proposals(store, item_id, latest, facts, head=head, repo=repo)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    return True


_PROPOSALS_META_PREFIX = "review_remediation_proposals:"
"""``meta`` key prefix (per item) of the PR body's proposed **Behavior
changes** block as a remediation was launched (#3475): ``{"head", "round",
"digest"}``, the digest None when the body could not be read."""
_NO_PUSH_RELEASES = (
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
)
"""The holds whose release by a person buys one more remediation at a head a
remediation did not move (#3475): either is their word on that head."""
_NO_PUSH_RE_REVIEW_META_PREFIX = "review_no_push_re_review:"
"""``meta`` key prefix (per item) of the one re-review a remediation that
pushed nothing earned (#3475): ``{"head", "round", "digest"}`` of the round it
re-read and the proposed block that earned it."""
_HELD_PROPOSALS_META_PREFIX = "review_held_proposals:"
"""``meta`` key prefix (per item) of the PR body's proposed block as a
``remediation_no_change`` or ``remediation_no_progress`` hold was set at the
round's own head, or at one a no-op remediation of it published (#3493):
``{"head", "round", "digest", "held_at"}``. A person who changes that block
and releases the hold earns the head one re-review."""


def _proposals_digest(body: str | None) -> str | None:
    if body is None:
        return None
    block = "\n".join(proposed_section_lines(body))
    return hashlib.sha256(block.encode()).hexdigest()


def _remember_proposals(
    store: RunStore, item_id: str, latest: ReviewRound, facts: NodeFacts, *, repo: str
) -> str | None:
    """Record the PR body's proposed block as a remediation of *latest* launches,
    so the next pass can tell whether that run changed it (#3475); the body
    read, None when it could not be."""
    body = read_pr_body(repo, facts.pr_number)
    value = {
        "head": latest.head_sha,
        "round": latest.round,
        "digest": _proposals_digest(body),
    }
    store.set_meta(f"{_PROPOSALS_META_PREFIX}{item_id}", json.dumps(value))
    return body


def _meta_json(store: RunStore, key: str) -> dict[str, Any]:
    """A ``meta`` JSON object, or empty when it is unset or unreadable."""
    try:
        value = json.loads(store.get_meta(key) or "{}")
    except ValueError as exc:
        _logger.warning("review: meta %s unreadable, read as unset: %s", key, exc)
        return {}
    return value if isinstance(value, dict) else {}


def _no_push_remediation(
    store: RunStore, item_id: str, latest: ReviewRound, head: str
) -> RunRecord | None:
    """The newest review remediation of *latest* that ended at *head* without
    moving it (#3475), or None.

    Only a run that finished cleanly after the round and after a person's
    last release of a ``remediation_no_progress`` or ``remediation_no_change``
    hold counts, and only while the PR still stands at the round's head. A
    completed outcome since the round that published another head means the
    push landed after this tick's head read, so nothing is judged on it.
    """
    if latest.head_sha != head:
        return None
    released = _released_at(store, item_id, latest, _NO_PUSH_RELEASES)
    runs = _clean_remediations_since(
        store, item_id, max(_aware(latest.recorded_at), released or _MIN_AWARE)
    )
    if not runs or _pushed_since(store, item_id, latest, head):
        return None
    return max(runs, key=lambda run: _aware(run.started_at))


_MIN_AWARE = datetime.min.replace(tzinfo=UTC)


def _clean_remediations_since(
    store: RunStore, item_id: str, since: datetime
) -> list[RunRecord]:
    """The node's review remediations that finished cleanly, started at or
    after *since*."""
    from kew.run_store import REVIEW_REMEDIATION_RUN_KIND

    return [
        run
        for run in store.list_by_item(item_id)
        if run.run_kind == REVIEW_REMEDIATION_RUN_KIND
        and run.status == "success"
        and _aware(run.started_at) >= since
    ]


def _pushed_since(
    store: RunStore, item_id: str, latest: ReviewRound, head: str
) -> bool:
    """Whether a remediation completed since *latest* publishing a head other
    than *head*."""
    return any(
        payload.get("status") == STATUS_COMPLETED
        and str(payload.get("head_sha") or head) != head
        for payload in _outcomes_since(store, item_id, _aware(latest.recorded_at))
    )


def _re_review_after_no_push(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
) -> bool:
    """Whether *head* is reviewed once more, instead of remediated again (#3475).

    True when a remediation of *latest* ended at *head* with no push but
    changed the PR body's proposed **Behavior changes** block: the reviewer
    rules on the new lines, which no second remediation could do. The one
    re-review is recorded, so a NEEDS_CHANGES it finds is held, never
    remediated at *head* again (:func:`_held_for_no_progress`). An unread
    body, or one with no record of the block at launch, proves no change.
    """
    run = _no_push_remediation(store, item_id, latest, head)
    if run is None:
        return False
    launched = _meta_json(store, f"{_PROPOSALS_META_PREFIX}{item_id}")
    if (launched.get("head"), launched.get("round")) != (head, latest.round):
        return False
    now = _proposals_digest(read_pr_body(repo, facts.pr_number))
    if launched.get("digest") is None or now is None or now == launched["digest"]:
        return False
    _grant_re_review(store, item_id, latest, head, now)
    _logger.info(
        "review: %s remediation %s pushed nothing at %s but changed the proposed "
        "Behavior changes lines — re-reviewing the head once",
        item_id,
        run.run_id,
        head[:MARKER_HEAD_CHARS],
    )
    return True


def _grant_re_review(
    store: RunStore, item_id: str, latest: ReviewRound, head: str, digest: str
) -> None:
    """Record the one re-review of *head* that the proposed block *digest*
    earned *latest* (#3475), so the round it finds is never remediated there."""
    store.set_meta(
        f"{_NO_PUSH_RE_REVIEW_META_PREFIX}{item_id}",
        json.dumps({"head": head, "round": latest.round, "digest": digest}),
    )


def _granted_on(
    store: RunStore, item_id: str, latest: ReviewRound, head: str, digest: str
) -> bool:
    """Whether a re-review of *head* granted on the proposed block *digest*
    has run (#3493): the reviewer has read that block at that head. A grant
    for *latest*'s own round has not — its reviewer was deferred (a wait, an
    unwired launcher, a running verify) — so the release still earns it."""
    granted = _meta_json(store, f"{_NO_PUSH_RE_REVIEW_META_PREFIX}{item_id}")
    if (granted.get("head"), granted.get("digest")) != (head, digest):
        return False
    return _earlier_round(granted, latest)


def _known_proposals(store: RunStore, item_id: str, latest: ReviewRound) -> str | None:
    """The proposed block's digest as kew last read it for *latest*, without a
    new read: the one a remediation of *latest* launched on, or else the one
    that earned the re-review *latest* is. Either equals the body when a hold
    is set at the round's head, or the re-review check before it would have
    granted one. None when kew holds no such read."""
    launched = _meta_json(store, f"{_PROPOSALS_META_PREFIX}{item_id}")
    granted = _meta_json(store, f"{_NO_PUSH_RE_REVIEW_META_PREFIX}{item_id}")
    if (launched.get("head"), launched.get("round")) == (
        latest.head_sha,
        latest.round,
    ):
        digest = launched.get("digest")
    elif granted.get("head") == latest.head_sha and _earlier_round(granted, latest):
        digest = granted.get("digest")
    else:
        digest = None
    return digest if isinstance(digest, str) else None


def _earlier_round(record: dict[str, Any], latest: ReviewRound) -> bool:
    return isinstance(record.get("round"), int) and record["round"] < latest.round


def _remember_held_proposals(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
) -> None:
    """Record the proposed block a no-push hold at *head* was set on (#3493),
    for :func:`_re_review_after_release` to compare a release with.

    *head* is *latest*'s own, or one a remediation of it that changed nothing
    published when the base had moved: either is the code the round read.
    Any other head records nothing, so its release earns no re-review. The
    block is the one kew last read for *latest*; when it holds no such read
    (the body could not be read as the remediation launched), the body is
    read now, so a person's later edit to it still reads as a change."""
    if latest.head_sha != head and not _no_op_published(store, item_id, latest, head):
        return
    digest = _known_proposals(store, item_id, latest)
    if digest is None:
        digest = _proposals_digest(read_pr_body(repo, facts.pr_number))
    value = {
        "head": head,
        "round": latest.round,
        "digest": digest,
        "held_at": datetime.now(UTC).isoformat(),
    }
    store.set_meta(f"{_HELD_PROPOSALS_META_PREFIX}{item_id}", json.dumps(value))


def _re_review_after_release(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
    approvers: Sequence[str],
) -> bool | None:
    """Whether a person's release of a no-push hold earns *head* one re-review
    instead of a remediation (#3493); None when the PR body could not be read
    to tell, and the node waits a tick rather than spend the release.

    The release must be an approver's (``[approval] approvers``; any
    operator's when that list is empty), of a hold set at *head* for
    *latest*'s round (:func:`_remember_held_proposals` records only *latest*'s
    own head or one a no-op remediation of it published), and no remediation
    may have run since it. Then the head is re-reviewed when the PR body's
    proposed **Behavior changes** block differs from the one the hold was set
    on, or when it already holds a line for every test the round asks about,
    whoever wrote those lines and whenever: only a reviewer can rule on
    them — a remediation launched now would record the block as its own and
    change nothing. Never twice on one block at one head: a re-review already
    granted there on the same block was the reviewer's look at it. The
    re-review is recorded like the one a no-push remediation earns, so a
    NEEDS_CHANGES it finds is held again; any other release buys one more
    remediation, as before. A hold that recorded no proposed block, on a
    round that asks for more than lines, is never earned by the body, so its
    release buys the remediation without reading it.
    """
    held = _meta_json(store, f"{_HELD_PROPOSALS_META_PREFIX}{item_id}")
    if not _release_pending(store, item_id, latest, held, head, approvers):
        return False
    verdict = _standing_verdict(latest)
    if not _body_can_earn(held, verdict):
        # No read of the body could earn a re-review: the release buys its
        # remediation, even while the body is unreadable.
        return False
    body = read_pr_body(repo, facts.pr_number)
    if body is None:
        _logger.warning(
            "review: %s released but its PR body is unreadable; whether the "
            "release earns a re-review is re-read on the next tick",
            item_id,
        )
        return None
    _forget_unread_body(store, item_id)
    now = _proposals_digest(body)
    changed = held.get("digest") is not None and now != held["digest"]
    if not changed and not (verdict and proposals_answer(verdict.findings, body)):
        return False
    assert now is not None  # the body was read
    if _granted_on(store, item_id, latest, head, now):
        # The reviewer already read this very block at this head: a release
        # that changed nothing since buys a remediation, never a second look.
        return False
    _grant_re_review(store, item_id, latest, head, now)
    _logger.info(
        "review: %s released with proposed Behavior changes lines at %s — "
        "re-reviewing the head once",
        item_id,
        head[:MARKER_HEAD_CHARS],
    )
    return True


def _body_can_earn(held: dict[str, Any], verdict: ParsedVerdict | None) -> bool:
    """Whether the PR body could earn a released hold *held* a re-review: it
    recorded a proposed block a person may have changed since, or *verdict*
    asks only for lines a proposed block can answer (#3493). Neither, and the
    release is spent on a remediation without reading the body."""
    if held.get("digest") is not None:
        return True
    return verdict is not None and asks_only_lines(verdict.findings)


def _release_pending(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    held: dict[str, Any],
    head: str,
    approvers: Sequence[str],
) -> bool:
    """Whether an approver released the no-push hold *held* records — set at
    *head* for *latest*'s round — after it was set, and no remediation has
    run since that release (#3493).

    An empty *approvers* list is any operator's release, as it is for every
    other ``[approval] approvers`` gate (:func:`kew.approval._authorized`)."""
    if (held.get("head"), held.get("round")) != (head, latest.round):
        return False
    release = max(
        _hold_releases(store, item_id, latest, _NO_PUSH_RELEASES),
        key=lambda r: cast(datetime, r["released_at"]),
        default=None,
    )
    if release is None:
        return False
    if approvers and release.get("released_by") not in approvers:
        return False
    released = cast(datetime, release["released_at"])
    if released < _held_at(held):
        return False
    return not _clean_remediations_since(store, item_id, released)


def _held_at(held: dict[str, Any]) -> datetime:
    """When a :data:`_HELD_PROPOSALS_META_PREFIX` record was written; the
    latest moment for one that names none, so no release postdates it."""
    try:
        return _aware(datetime.fromisoformat(str(held.get("held_at"))))
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)


def _spent_re_review(
    store: RunStore, item_id: str, latest: ReviewRound, head: str
) -> bool:
    """Whether *latest* is the round the one no-push re-review of *head* found,
    and no person has released the node since."""
    granted = _meta_json(store, f"{_NO_PUSH_RE_REVIEW_META_PREFIX}{item_id}")
    if granted.get("head") != head or latest.head_sha != head:
        return False
    if not _earlier_round(granted, latest):
        return False
    return _released_at(store, item_id, latest, _NO_PUSH_RELEASES) is None


def _held_for_no_progress(
    node_id: str,
    item_id: str,
    latest: ReviewRound,
    head: str,
    *,
    facts: NodeFacts,
    repo: str,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    store: RunStore,
) -> bool:
    """Hold a node rather than launch a second remediation at *head* (#3475).

    Either a remediation of the standing round ended at *head* with no push
    and changed nothing a re-review could rule on, or the round is the one
    re-review such a run earned. Another remediation would read the same
    brief at the same head and end the same way, once a tick, to the branch's
    run cap. A person's release of the hold buys one more remediation, or one
    re-review when they changed the proposed block (#3493).
    """
    run = _no_push_remediation(store, item_id, latest, head)
    if run is None and not _spent_re_review(store, item_id, latest, head):
        return False
    reason = remediation_no_progress_hold_reason(head, latest.round)
    _logger.error(
        "review: %s remediation %s pushed nothing at %s — no new remediation "
        "dispatched; node HELD: %s",
        item_id,
        run.run_id if run is not None else "(before the re-review)",
        head[:MARKER_HEAD_CHARS],
        reason,
    )
    _hold(store, epic_ref, item_id, reason, provider=provider)
    _remember_held_proposals(store, item_id, latest, facts, head=head, repo=repo)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    return True


@dataclass(frozen=True)
class _StandingDeclaration:
    """A standing round that asks only for Behavior changes lines (#2872)."""

    request: DeclarationRequest
    missing: tuple[str, ...] | None
    """The requested node ids the PR body does not declare yet — never empty
    unless :attr:`landed` — or None when the body could not be read, which is
    unknown rather than missing."""
    landed: bool = False
    """Every requested line is in the body, and no earlier round at this head
    already asked for all of them — so a re-review is due, not a remediation.
    Any other earlier round at the head (a NEEDS_FULL delta round, or a
    declaration round that asked for fewer tests) does not count against it."""


def _standing_declaration(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    facts: NodeFacts,
    *,
    repo: str,
) -> _StandingDeclaration | None:
    """The declaration *latest* asks for, when that is all it asks for.

    None too when the PR body already declares every requested test and an
    earlier round at this head asked for them all: that round's re-review was
    the one those lines earned. Since #3516 that re-review drops a declaration
    finding asking for them, so it can still ask only when it fell short of
    what a READY needs (no acceptance evidence, say) and kew kept it as
    written. A declaration cannot settle that, so it is remediated as an
    ordinary round — never held as "undeclared", which a person could only
    answer by declaring the same lines again.

    A pre-review round's held-out failures (#3212) are read the same way: once
    the PR body declares them all the round is answered and the head goes back
    through the gate, and a remediation that ran at this head and neither
    fixed nor declared them is held for a person.
    """
    request = load_declaration_request(
        latest.sidecar_path, recorded_criterion_ids(latest.criteria_json)
    ) or load_held_out_request(latest.sidecar_path)
    if request is None:
        return None
    body = read_pr_body(repo, facts.pr_number)
    if body is not None:
        _forget_unread_body(store, item_id)
    missing = None if body is None else undeclared(request, body)
    if missing != ():
        return _StandingDeclaration(request, missing)
    if _repeats_declaration(store, item_id, latest, request):
        return None
    return _StandingDeclaration(request, missing, landed=True)


def _repeats_declaration(
    store: RunStore, item_id: str, latest: ReviewRound, request: DeclarationRequest
) -> bool:
    """Whether an earlier round at *latest*'s head asked for every test *request*
    names — the re-review a landed declaration earns has already been spent.

    An earlier round counts with every test it asked for, those kew excused
    off it as declared included (#3516): its request names only the rest, and
    a later round asking for both must not earn a second re-review."""
    asked = set(request.node_ids)
    for round_ in store.review_rounds_in_cycle(item_id):
        if round_.head_sha != latest.head_sha or round_.round >= latest.round:
            continue
        earlier = requested_by_round(
            round_.sidecar_path, recorded_criterion_ids(round_.criteria_json)
        )
        if earlier and asked <= set(earlier):
            return True
    return False


@dataclass(frozen=True)
class _BodyUnread:
    """A released no-push hold whose PR body could not be read to tell whether
    the release earns a re-review (#3493): the node waits a tick, as a
    declaration round's unread body does, rather than spend the release on a
    remediation."""

    latest: ReviewRound


def _standing_round(
    store: RunStore,
    item_id: str,
    latest: ReviewRound | None,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
    approvers: Sequence[str] = (),
) -> tuple[ReviewRound, _StandingDeclaration | None] | _BodyUnread | None:
    """*latest* when its NEEDS_CHANGES findings still stand against *head*.

    None when a reviewer round is due instead — including a round that asked
    only for Behavior changes lines the PR body now carries: the finding is
    answered there, so the head is re-reviewed (#2872).

    An operator's send-back stands on a moved head too, until a remediation
    answers it (#3200): its finding is the operator's, not a reviewer's verdict
    on the old head, and a merge-main step or base integration that moves the
    head fixes none of it. Re-reviewing the moved head instead would let a
    reviewer pass the very code the operator sent back.

    Any other round stands, too, on a head that a remediation of it published
    without an agent change (#3276): kew's own merge of the base moved the
    commit, the findings did not move, and the no-change hold catches it. A
    send-back already stands on such a head unless a person released it there,
    and that release has the head reviewed.

    A round a remediation or an approver's release earned one re-review of
    (#3475, #3493) is None too; :class:`_BodyUnread` when the release's PR
    body could not be read to tell.
    """
    if latest is None or latest.verdict != _VERDICT_NEEDS_CHANGES:
        return None
    if latest.head_sha != head:
        # A send-back is judged by its own rule alone: a person's release at
        # this head answers it even when a no-op published the head.
        stands = (
            _send_back_stands(store, item_id, latest, head)
            if latest.review_mode == REVIEW_MODE_OPERATOR_SEND_BACK
            else _no_op_published(store, item_id, latest, head)
        )
        if not stands:
            return None
    declaration = _standing_declaration(store, item_id, latest, facts, repo=repo)
    if declaration is not None:
        # Its remediation pushes nothing by design: the lines landing earn
        # its re-review (#2872), never the one no-push re-review (#3475).
        return None if declaration.landed else (latest, declaration)
    due = _re_review_earned(
        store, item_id, latest, facts, head=head, repo=repo, approvers=approvers
    )
    if due is None:
        return _BodyUnread(latest)
    return None if due else (latest, None)


def _re_review_earned(
    store: RunStore,
    item_id: str,
    latest: ReviewRound,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
    approvers: Sequence[str],
) -> bool | None:
    """Whether *latest* has earned its one re-review at *head*; None when the
    PR body that decides it could not be read."""
    if _no_push_remediation(store, item_id, latest, head) is not None:
        # A remediation ran since the last release: its own block change is
        # what may earn the re-review. With none, the release's may (#3493).
        return _re_review_after_no_push(
            store, item_id, latest, facts, head=head, repo=repo
        )
    return _re_review_after_release(
        store, item_id, latest, facts, head=head, repo=repo, approvers=approvers
    )


#: Passes in a row a declaration round's PR body may go unread after its
#: remediation before the node is held for a person (#2872).
_MAX_BODY_UNREAD_PASSES = 5
_BODY_UNREAD_META_PREFIX = "review_body_unread:"


def _wait_for_unread_body(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    store: RunStore,
) -> None:
    """Count one more pass the PR body went unread; hold once the bound is spent.

    The hold names the failed read and clears itself on the first read that
    succeeds (:func:`_release_readable_body_holds`), so a node is never left
    reading "remediating" on a body that never becomes readable, and never
    parked for a person once it does.
    """
    reads = _count_unread_body(store, item_id, latest)
    if reads < _MAX_BODY_UNREAD_PASSES:
        _logger.warning(
            "review: %s PR body unreadable (%d of %d passes); whether its "
            "declaration landed is re-read on the next tick",
            item_id,
            reads,
            _MAX_BODY_UNREAD_PASSES,
        )
        return
    reason = pr_body_unreadable_hold_reason(facts.pr_number, reads)
    _logger.error("review: %s node HELD: %s", item_id, reason)
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)


def _count_unread_body(store: RunStore, item_id: str, latest: ReviewRound) -> int:
    """Record one more unread pass against *latest*; how many in a row now."""
    key = f"{_BODY_UNREAD_META_PREFIX}{item_id}"
    stamp = f"{latest.head_sha}#{latest.round}"
    seen, _, count = (store.get_meta(key) or "").rpartition("|")
    reads = int(count) + 1 if seen == stamp and count.isdigit() else 1
    store.set_meta(key, f"{stamp}|{reads}")
    return reads


def _forget_unread_body(store: RunStore, item_id: str) -> None:
    """A read succeeded: the next failure starts the count over."""
    key = f"{_BODY_UNREAD_META_PREFIX}{item_id}"
    if store.get_meta(key):
        store.set_meta(key, "")


def _release_readable_body_holds(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    epic_ref: str,
    store: RunStore,
    repo: str,
    provider: str,
) -> None:
    """Clear a ``pr_body_unreadable`` hold once the PR body reads again (#2872).

    That hold records a failed read, not a decision anyone owes, so the first
    read that succeeds releases it. The node re-enters the lifecycle this same
    pass, whose own read of the body decides what it owes: a re-review, the
    ``test_declarations_missing`` hold, or nothing new.
    """
    held = {
        hold.item_id
        for hold in store.list_epic_holds(epic_ref)
        if is_pr_body_unreadable_hold(hold.reason)
    }
    for node_id, item_id, status in _review_items(statuses, provider):
        facts = node_facts.get(node_id)
        if status is not DriverStatus.HELD or item_id not in held or facts is None:
            continue
        if read_pr_body(repo, facts.pr_number) is None:
            continue
        if provider == "github":
            store.clear_epic_hold(item_id)
        else:
            store.clear_epic_hold_item(item_id)
        _forget_unread_body(store, item_id)
        statuses[node_id] = DriverStatus.REMEDIATING
        _logger.info(
            "review: %s PR body reads again; pr_body_unreadable cleared", item_id
        )


def _send_back_stands(
    store: RunStore, item_id: str, latest: ReviewRound, head: str
) -> bool:
    """Whether *latest* is an operator send-back nothing has answered on *head*.

    Only an agent's change answers it: a remediation whose completed outcome
    shows it published a tree other than the one kew handed it (#3276). A
    head moved by kew's own merge of the base — in a merge-main step, or in the
    remediation's checkout before an agent that then changed nothing — fixes
    none of the operator's finding, and a run with no completed outcome proves
    nothing reached the branch. The other answer is a person's: a release of
    the ``remediation_no_change`` hold, for the head the PR stood at when they
    released it. A send-back still standing after a no-op is held as
    ``remediation_no_change`` instead of remediated again.
    """
    if latest.review_mode != REVIEW_MODE_OPERATOR_SEND_BACK:
        return False
    if _agent_changed_since(store, item_id, latest):
        return False
    return head not in _released_heads(store, item_id, latest)


def _agent_change(payload: dict[str, object], round_head: str) -> bool | None:
    """What a ``review.outcome`` payload proves about the agent's own change.

    ``True`` when it changed something, ``False`` when it changed nothing,
    ``None`` when the payload proves neither: a run that did not complete, or
    one that recorded too little. The trees decide when both were recorded:
    the published tree against the one kew's base integration handed the agent
    (#3276), so a commit moved by that merge alone is no change. An outcome
    recorded before the trees falls back to commit ids: the published head
    against the head the checkout was cut at, or, with no cut head either,
    against the round's head — and then only a moved head proves anything.
    """
    if payload.get("status") != STATUS_COMPLETED:
        return None
    integrated = str(payload.get("integrated_tree") or "")
    published_tree = str(payload.get("published_tree") or "")
    if integrated and published_tree:
        return published_tree != integrated
    return _head_change(payload, round_head)


def _head_change(payload: dict[str, object], round_head: str) -> bool | None:
    """:func:`_agent_change` for an outcome that recorded no trees: commit ids."""
    published = str(payload.get("head_sha") or "")
    cut = str(payload.get("cut_head_sha") or "")
    if not published:
        return None
    if cut:
        return published != cut
    return True if published != round_head else None


def _outcomes_since(
    store: RunStore, item_id: str, since: datetime
) -> list[dict[str, object]]:
    """The node's ``review.outcome`` payloads recorded since *since*, newest first."""
    events = store.list_audit_events(
        limit=None, event=REVIEW_OUTCOME_EVENT, item_id=item_id, since=since
    )
    ordered = sorted(events, key=lambda e: _aware(datetime.fromisoformat(e.timestamp)))
    return [dict(event.payload or {}) for event in reversed(ordered)]


def _agent_changed_since(store: RunStore, item_id: str, latest: ReviewRound) -> bool:
    """Whether a remediation completed after *latest* with an agent change."""
    return any(
        _agent_change(payload, latest.head_sha) is True
        for payload in _outcomes_since(store, item_id, _aware(latest.recorded_at))
    )


def _no_change_outcome(
    store: RunStore, item_id: str, latest: ReviewRound
) -> dict[str, object] | None:
    """The newest completed remediation since *latest* that changed nothing.

    ``None`` once any remediation since *latest* changed something: a run
    recorded as an agent change is never outweighed by a no-op, even while
    this tick's head read still shows the old head — that read predates its
    push, and the next tick reads the moved head. A run row alone, or an
    outcome that proves nothing, is not a no-op either, and the round is
    remediated as before. A person's release of the ``remediation_no_change``
    hold clears the runs before it, so the release buys one more remediation
    rather than the same hold again.
    """
    if _agent_changed_since(store, item_id, latest):
        return None
    since = _no_change_released_at(store, item_id, latest) or _aware(latest.recorded_at)
    return next(
        (
            payload
            for payload in _outcomes_since(store, item_id, since)
            if _agent_change(payload, latest.head_sha) is False
        ),
        None,
    )


def _no_op_published(
    store: RunStore, item_id: str, latest: ReviewRound, head: str
) -> bool:
    """Whether *head* is a head a no-op remediation of *latest* published.

    kew merges the base into a remediation's checkout before the agent runs,
    so an agent that changed nothing still publishes a new commit when the
    base had moved (#3276). That head is kew's merge, not an answer to the
    round, so the round stands on it and the no-change hold catches it.
    """
    if _agent_changed_since(store, item_id, latest):
        return False
    return any(
        _agent_change(payload, latest.head_sha) is False
        and payload.get("head_sha") == head
        for payload in _outcomes_since(store, item_id, _aware(latest.recorded_at))
    )


def _no_change_releases(
    store: RunStore, item_id: str, latest: ReviewRound
) -> list[dict[str, object]]:
    """A person's attested releases of a ``remediation_no_change`` hold after
    *latest*: each payload, with the ``released_at`` time it was recorded."""
    return _hold_releases(store, item_id, latest, REMEDIATION_NO_CHANGE_HOLD_PREFIX)


def _released_at(
    store: RunStore, item_id: str, latest: ReviewRound, prefix: str | tuple[str, ...]
) -> datetime | None:
    """When a person last released a *prefix* hold after *latest*."""
    return max(
        (
            cast(datetime, release["released_at"])
            for release in _hold_releases(store, item_id, latest, prefix)
        ),
        default=None,
    )


def _hold_releases(
    store: RunStore, item_id: str, latest: ReviewRound, prefix: str | tuple[str, ...]
) -> list[dict[str, object]]:
    """A person's attested releases of a hold whose reason starts *prefix*
    after *latest*: each payload, with the ``released_at`` time it was recorded
    and the ``released_by`` actor who released it."""
    return [
        {
            **(event.payload or {}),
            "released_at": _aware(datetime.fromisoformat(event.timestamp)),
            "released_by": event.actor_id,
        }
        for event in store.list_audit_events(
            limit=None,
            event="epic_node_unhold",
            item_or_issue=item_id,
            since=_aware(latest.recorded_at),
        )
        if str((event.payload or {}).get("reason", "")).startswith(prefix)
    ]


def _no_change_released_at(
    store: RunStore, item_id: str, latest: ReviewRound
) -> datetime | None:
    """When a person last released a ``remediation_no_change`` hold after *latest*."""
    return _released_at(store, item_id, latest, REMEDIATION_NO_CHANGE_HOLD_PREFIX)


def _released_heads(
    store: RunStore, item_id: str, latest: ReviewRound
) -> frozenset[str]:
    """The PR heads a person vouched for by releasing the no-change hold (#3276).

    ``kew epic unhold`` records the head the PR stood at when the person let it
    go. The release answers a send-back on that head only: a head a base
    integration moved past it afterwards was never in front of them. A release
    recorded without a head vouches for none.
    """
    return frozenset(
        str(release.get("pr_head"))
        for release in _no_change_releases(store, item_id, latest)
        if release.get("pr_head")
    )


def _remediated_since(store: RunStore, item_id: str, latest: ReviewRound) -> bool:
    """Whether a review remediation finished cleanly after *latest* was recorded.

    The declaration path's question only. Whether the run changed anything is
    :func:`_agent_change`'s, read off its completed outcome: a run row says the
    agent finished, not what reached the branch.
    """
    from kew.run_store import REVIEW_REMEDIATION_RUN_KIND

    recorded = _aware(latest.recorded_at)
    return any(
        run.run_kind == REVIEW_REMEDIATION_RUN_KIND
        and run.status == "success"
        and _aware(run.started_at) >= recorded
        for run in store.list_by_item(item_id)
    )


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _drive_one_node(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    reviewer_launcher: ReviewerLauncher | None,
    remediator: Remediator | None,
    base_integrator: BaseIntegrator | None,
    integration_only: bool,
    worker_cap: int,
    now: datetime,
) -> None:
    """Take the one action the node's persisted review state calls for."""
    # A head that no longer merges into the base outranks every review action:
    # reviewing or remediating a delta that cannot land spends a round on work
    # the merge-main step invalidates anyway (#2149).
    if _integrate_base(
        node_id,
        item_id,
        facts,
        base=base,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        store=store,
        base_integrator=base_integrator,
        now=now,
    ):
        return
    if integration_only:
        # This node is only ever due the merge-main step (`_due_nodes`): its
        # verdict stands on the current head, or it is held. No round to run.
        return
    latest = store.latest_review_round(item_id)
    round_count = store.review_round_count(item_id)
    head = facts.head_oid
    assert head is not None  # guarded by the caller

    # NEEDS_CHANGES findings still standing against the CURRENT head: the code has
    # not moved since the review, so remediate (or, at the cap, converge loudly).
    standing = _standing_round(
        store,
        item_id,
        latest,
        facts,
        head=head,
        repo=repo,
        approvers=config.approval.approvers,
    )
    if isinstance(standing, _BodyUnread):
        _wait_for_unread_body(
            node_id,
            item_id,
            facts,
            standing.latest,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            store=store,
        )
        return
    if standing is not None:
        latest, declaration = standing
        _drive_standing_findings(
            node_id,
            item_id,
            facts,
            latest,
            head,
            round_count,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
            repo=repo,
            remediator=remediator,
            gate_paths_touched=_gate_paths_reader(
                store, item_id, repo_root, base, head, config
            ),
            declaration=declaration,
            now=now,
        )
        return

    # Otherwise a reviewer round is due: a fresh PR (round 0), a READY/NEEDS
    # voided by a moved head, a re-review after remediation landed a new commit,
    # or the full round a NEEDS_FULL delta READY calls for (#2111). The
    # pre-review gate (#3201) runs inside it, once the reviewer could launch.
    gate = partial(
        _gate_before_review,
        node_id,
        item_id,
        facts,
        head,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        base=base,
        remediator=remediator,
        worker_cap=worker_cap,
        now=now,
    )
    _dispatch_and_collect(
        node_id,
        item_id,
        facts,
        head,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        base=base,
        reviewer_launcher=reviewer_launcher,
        worker_cap=worker_cap,
        now=now,
        gate=gate,
    )


def _gate_before_review(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    head: str,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    remediator: Remediator | None,
    worker_cap: int,
    now: datetime,
) -> bool:
    """Run the pre-review gate on *head*; True when it stopped the review (#3201).

    Called only once the launch guards said a reviewer can launch (a usage-limit
    wait, an unwired launcher and a running local merge verify all stop it
    first), so the gate never runs where no reviewer would. A detached stage
    child runs it inside the review slot its tick reserved; on the inline path a
    full fleet defers it with the reviewer, to the next tick.

    A half that passed on this head, or was skipped for a reason the head cannot
    change, is not run again (a review deferred after it would otherwise re-run
    it every tick). A gate that did not run proves nothing about the head, so
    the head goes to review as it did before the step existed — and the skip
    and its reason are recorded for the head (#3280), on this tick's outcome,
    for ``kew epic status`` and for the verdict comment. A failure is recorded
    as a NEEDS_CHANGES round carrying the output and remediated on this same
    tick.
    """
    configured = configured_halves(config)
    if not configured:
        return False  # no gate configured: nothing is read, run or written
    settled = settled_halves(store, item_id, head)
    if not configured - settled:
        _note_gate_skip(node_id, item_id, head, outcome=outcome, store=store)
        return False
    if (
        held_reservation() is None
        and worker_cap > 0
        and store.live_run_count() >= worker_cap
    ):
        return True  # the reviewer would find the fleet full: both wait a tick
    ran = _run_gate(
        item_id,
        facts,
        head,
        settled,
        configured,
        config=config,
        store=store,
        repo=repo,
        repo_root=repo_root,
        base=base,
    )
    if ran is None or ran[0].kind != "failed":
        _note_gate_skip(node_id, item_id, head, outcome=outcome, store=store)
        return False
    result, pr_body = ran
    if gate_rounds(store, item_id) >= MAX_GATE_ROUNDS:
        _exhaust_gate(
            node_id,
            item_id,
            head,
            result,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
            repo=repo,
        )
        return True
    _record_gate_round(
        node_id,
        item_id,
        head,
        result,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        pr_body=pr_body,
    )
    latest = store.latest_review_round(item_id)
    assert latest is not None  # recorded just above
    _drive_standing_findings(
        node_id,
        item_id,
        facts,
        latest,
        head,
        store.review_round_count(item_id),
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        provider=provider,
        config=config,
        store=store,
        repo=repo,
        remediator=remediator,
        gate_paths_touched=_gate_paths_reader(
            store, item_id, repo_root, base, head, config
        ),
        now=now,
    )
    return True


def _run_gate(
    item_id: str,
    facts: NodeFacts,
    head: str,
    settled: frozenset[str],
    configured: frozenset[str],
    *,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
) -> tuple[GateOutcome, str | None] | None:
    """Run the halves not yet settled on *head* and record what they settled
    and skipped; None when the step itself could not run."""
    kept = settled
    private = private_names(config)
    settled, pr_body = _held_out_body(item_id, facts, settled, configured, repo=repo)
    unread = (
        [(HELD_OUT_HALF, f"{HELD_OUT_GATE}: the PR body could not be read")]
        if HELD_OUT_HALF in settled - kept
        else []
    )
    try:
        result = run_pre_review_gate(
            config,
            repo_root=repo_root,
            base=base,
            head=head,
            settled=settled,
            pr_body=pr_body,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001 — a step that could not run is not a defect
        _logger.warning(
            "review: the pre-review gate could not run on %s at %s — the head "
            "goes to review without it",
            item_id,
            head[:MARKER_HEAD_CHARS],
            exc_info=True,
        )
        why = f"the gate could not run ({type(exc).__name__}: {exc})"
        record_skips(store, item_id, head, [("gate", why)], kept=kept, private=private)
        return None
    record_settled(store, item_id, head, result.settled)
    record_skips(
        store,
        item_id,
        head,
        [*unread, *result.skipped],
        kept=kept,
        private=private,
    )
    _log_gate_gaps(item_id, head, result)
    return result, pr_body


def _note_gate_skip(
    node_id: str,
    item_id: str,
    head: str,
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
) -> None:
    """Put the gate's recorded skip for *head*, if any, on the tick's outcome."""
    reason = gate_skip_reason(store, item_id, head)
    if reason is not None:
        outcome.gate_skipped[node_id] = reason


def recorded_gate_skips(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    config: DispatchConfig,
    store: RunStore,
) -> dict[str, str]:
    """Node -> the gate's recorded skip on its current head, for every
    review-stage node (#3344).

    The drive's review stage is detached: the gate runs in a ``kew epic
    _review-node`` child, whose outcome never reaches the tick. What the
    child recorded in the run store does, so the tick reads every skip from
    there rather than from the stage's outcome alone.
    """
    if not config.review.adversarial:
        return {}
    skips: dict[str, str] = {}
    for node_id, item_id, _ in _review_items(statuses, provider_from_config(config)):
        facts = node_facts.get(node_id)
        head = facts.head_oid if facts is not None else None
        reason = gate_skip_reason(store, item_id, head) if head else None
        if reason is not None:
            skips[node_id] = reason
    return skips


def _held_out_body(
    item_id: str,
    facts: NodeFacts,
    settled: frozenset[str],
    configured: frozenset[str],
    *,
    repo: str,
) -> tuple[frozenset[str], str | None]:
    """The PR body the held-out half judges against, when it runs (#3212).

    Read only when that half is due. A body that does not read cannot say
    what is declared, so the half sits this head out — counted as settled for
    this call only, never recorded — rather than send declared failures back.
    """
    if HELD_OUT_HALF not in configured - settled:
        return settled, None
    body = read_pr_body(repo, facts.pr_number)
    if body is None:
        _logger.warning(
            "review: PR #%s body unreadable — the held-out check does not run on "
            "%s this tick",
            facts.pr_number,
            item_id,
        )
        return settled | {HELD_OUT_HALF}, None
    return settled, body


def _log_gate_gaps(item_id: str, head: str, result: GateOutcome) -> None:
    """Say what the gate did not prove about *head*: a skipped check, and the
    changed surfaces no ui-check module covers."""
    if result.detail:
        _logger.warning(
            "review: the pre-review gate did not check %s at %s (%s) — the head "
            "goes to review without that check",
            item_id,
            head[:MARKER_HEAD_CHARS],
            result.detail,
        )
    if result.uncovered:
        _logger.warning(
            "review: no ui-check module covers %s changed in %s at %s — the "
            "browser checks are not counted as passed for it",
            ", ".join(result.uncovered),
            item_id,
            head[:MARKER_HEAD_CHARS],
        )


def _exhaust_gate(
    node_id: str,
    item_id: str,
    head: str,
    result: GateOutcome,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """The head failed the gate again with its bound spent: no remediation.

    The node is held under the gate's own reason, naming the check and its
    first failing line: no reviewer read this head, so the round cap's reason
    would send an operator looking for reviewer findings.

    Advisory review (``[review] blocking = false``) holds it too, rather than
    attest DEGRADED and proceed as at the round cap (#3279): that let a head no
    reviewer read move on. Holding loses nothing in advisory mode — a head
    that fails the merge gate's own static checks cannot pass the merge verify
    either.
    """
    last = result.findings[-1]
    _logger.error(
        "review: %s failed the pre-review gate (%s) after %d remediated rounds "
        "this cycle — no further remediation of it is dispatched",
        item_id,
        last.check,
        MAX_GATE_ROUNDS,
    )
    reason = pre_review_gate_exhausted_hold_reason(
        last.check, MAX_GATE_ROUNDS, first_failing_line(last.output)
    )
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    if config.review.publish_status:
        review_status.publish_failure(repo, head, "pre-review gate exhausted")


def _record_gate_round(
    node_id: str,
    item_id: str,
    head: str,
    result: GateOutcome,
    *,
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    pr_body: str | None = None,
) -> None:
    """Record the gate's failures as the NEEDS_CHANGES round remediation answers.

    Held-out failures are also recorded beside the sidecar
    (:func:`record_held_out_request`), so the remediation's brief names them
    and the lines it declares for them are carried into the PR body.
    """
    sidecar = pre_review_gate_sidecar(
        Path(config.logging.dir).resolve(),
        f"pre-review-{item_id.replace('/', '-')}-{head[:12]}",
        findings=result.findings,
    )
    if result.held_out:
        try:
            record_held_out_request(
                sidecar.stored_path,
                result.held_out,
                pr_body,
            )
        except OSError as exc:  # the findings still name every failing test
            _logger.warning(
                "review: held-out request for %s not recorded: %s", item_id, exc
            )
    round_num = store.record_review_round(
        epic_ref,
        item_id,
        head_sha=head,
        verdict=_VERDICT_NEEDS_CHANGES,
        sidecar_digest=sidecar.digest,
        sidecar_path=str(sidecar.stored_path),
        review_mode=REVIEW_MODE_PRE_REVIEW_GATE,
    )
    _logger.error(
        "review: %s at %s fails the pre-review gate (%s) — round %d recorded "
        "with %d finding(s); no reviewer launches on this head",
        item_id,
        head[:MARKER_HEAD_CHARS],
        ", ".join(result.ran),
        round_num,
        len(result.findings),
    )
    outcome.verdicts[node_id] = _VERDICT_NEEDS_CHANGES
    outcome.findings[node_id] = len(result.findings)


def _left_to_operator(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    epic_ref: str,
    store: RunStore,
    now: datetime,
) -> bool:
    """Whether the merge-main step stays off this head: an operator owns it.

    An operator owns the head when its newest commit is theirs, not one kew
    published (:func:`operator_commit_at_head`), or while their merge verify
    claims it (#3198). On 2026-10-25 the step pushed resolutions onto two such
    branches, and one reintroduced a symbol the operator's fix had removed
    (#3200). The step records ``operator-owned: integration skipped`` instead.
    """
    head = facts.head_oid or ""
    branch = f"{config.branches.prefix}{branch_slug(item_id)}"
    if operator_commit_at_head(store, item_id=item_id, branch=branch, head_sha=head):
        _skip_operator_owned(
            node_id,
            item_id,
            head,
            why="an operator commit kew did not publish",
            outcome=outcome,
            epic_ref=epic_ref,
            store=store,
        )
        return True
    if _held_by_merge_claim(
        node_id,
        item_id,
        facts,
        head,
        action="merge-main step",
        outcome=outcome,
        store=store,
        now=now,
    ):
        _skip_operator_owned(
            node_id,
            item_id,
            head,
            why="claimed by a live operator merge verify",
            outcome=outcome,
            epic_ref=epic_ref,
            store=store,
            reason=outcome.refused[node_id],
        )
        return True
    return False


def _skip_operator_owned(
    node_id: str,
    item_id: str,
    head: str,
    *,
    why: str,
    outcome: ReviewStageOutcome,
    epic_ref: str,
    store: RunStore,
    reason: str | None = None,
) -> None:
    """Record that the merge-main step left an operator's head alone (#3200).

    Not a hold: the branch is the operator's to integrate, and the first head
    kew publishes itself (or the claim ending) gives the step back. *reason*
    is the wait the node reports, when one is already set (a merge claim's).
    """
    _logger.info(
        "review: %s no longer merges, but its head %s is %s — %s, nothing pushed",
        item_id,
        head[:12],
        why,
        OPERATOR_OWNED_SKIP,
    )
    outcome.refused[node_id] = reason or (
        f"{OPERATOR_OWNED_SKIP} — {head[:12]} is {why}; merge the base on the "
        "branch yourself"
    )
    emit(
        store,
        INTEGRATION_SKIPPED_EVENT,
        actor=Actor("policy", "epic-driver"),
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        payload={
            "epic_ref": epic_ref,
            "node": node_id,
            "head_sha": head,
            "reason": OPERATOR_OWNED_SKIP,
            "owner": why,
        },
        dedup_key=f"{INTEGRATION_SKIPPED_EVENT}:{item_id}:{head}:{why}",
    )


def _skip_moved_head(
    node_id: str,
    item_id: str,
    head: str,
    *,
    detail: str,
    outcome: ReviewStageOutcome,
    epic_ref: str,
    store: RunStore,
) -> None:
    """Record a merge-main step that found the PR head moved past *head* (#3200).

    The stage checked *head* for an operator; the resolve, pinned to it, found
    another head on the PR and pushed nothing. Not a hold and not a step: the
    node waits, and the next tick reads the new head and decides again whether
    the branch is kew's to integrate.
    """
    _logger.info("review: %s — %s (%s)", item_id, HEAD_MOVED_SKIP, detail)
    outcome.refused[node_id] = f"{HEAD_MOVED_SKIP} — {detail}"
    emit(
        store,
        INTEGRATION_SKIPPED_EVENT,
        actor=Actor("policy", "epic-driver"),
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        payload={
            "epic_ref": epic_ref,
            "node": node_id,
            "head_sha": head,
            "reason": HEAD_MOVED_SKIP,
            "detail": detail,
        },
        dedup_key=f"{INTEGRATION_SKIPPED_EVENT}:{item_id}:{head}:head-moved",
    )


def _integrate_base(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    *,
    base: str,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    base_integrator: BaseIntegrator | None,
    now: datetime,
) -> bool:
    """Merge *base* into a head that stopped merging cleanly (#2149).

    Returns True when this was the node's one action for the tick — the base
    was integrated (the moved head is re-reviewed next tick), the node was HELD
    because the integration could not finish or because the step's bound is
    spent, no seam is wired to do it, or an operator's merge verify claims the
    head (#3198): the step pushes a merge commit onto it, so it waits with the
    remediation's ``merge_claimed`` reason. A node whose head still merges
    falls through to its review action.
    """
    if not base_integration_due(facts):
        return False
    if base_integrator is None:
        _logger.warning(
            "review: %s no longer merges into %s but no merge-main step is "
            "wired — the node stays in review against a head that cannot land",
            item_id,
            base,
        )
        outcome.unwired.append(node_id)
        return True
    assert facts.head_oid is not None  # guarded by `base_integration_due`
    if _left_to_operator(
        node_id,
        item_id,
        facts,
        outcome=outcome,
        config=config,
        epic_ref=epic_ref,
        store=store,
        now=now,
    ):
        return True
    spent = consecutive_base_integrations(store, item_id, head_sha=facts.head_oid)
    if spent >= MAX_CONSECUTIVE_STEPS:
        _exhaust_integration(
            node_id,
            item_id,
            facts,
            spent,
            base=base,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
        )
        return True
    if _defer_for_running_verify(
        node_id,
        item_id,
        epic_ref=epic_ref,
        outcome=outcome,
        store=store,
        config=config,
        kind="merge-main",
    ):
        return True
    result = run_base_integration(item_id, facts, base=base, integrator=base_integrator)
    if result.kind == "integrated":
        _integrated(
            node_id,
            item_id,
            facts,
            base=base,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
        )
        return True
    if result.kind == "head_moved":
        _skip_moved_head(
            node_id,
            item_id,
            facts.head_oid,
            detail=result.detail,
            outcome=outcome,
            epic_ref=epic_ref,
            store=store,
        )
        return True
    reason = base_integration_hold_reason(result, base=base)
    _logger.error("review: merge-main step for %s did not land — %s", item_id, reason)
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    return True


def _integrated(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    *,
    base: str,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
) -> None:
    """Record a landed merge-main step and put the node back in the lifecycle."""
    assert facts.head_oid is not None  # guarded by `base_integration_due`
    _logger.info(
        "review: %s no longer merged into %s — merge-main step completed; "
        "the new head is reviewed on the next tick",
        item_id,
        base,
    )
    record_base_integration_step(
        store,
        config,
        item_id=item_id,
        issue_num=facts.issue_num,
        head_sha=facts.head_oid,
    )
    # The node may have reached the step from under the merge pass's own
    # ``ci_pending`` hold. That hold was derived from the facts this step has
    # just spent, and leaving it would put the node straight back where #2149
    # found it: HELD, never re-reviewed, never merged. The merge pass re-derives
    # it on the next tick if the moved head is still not mergeable.
    if _release_ci_hold(store, epic_ref, item_id, provider=provider):
        statuses[node_id] = DriverStatus.IN_REVIEW
    outcome.integrated[node_id] = (
        f"integrating {base} — the branch no longer merged into it; the "
        "merge-main step landed the base and the new head is reviewed on "
        "the next tick"
    )


def _exhaust_integration(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    spent: int,
    *,
    base: str,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
) -> None:
    """Stop a merge-main step that keeps succeeding without landing (#2149).

    The node is HELD before the seam is called, not after: the whole point is
    that the next governed ``kew resolve`` run is the one that must not happen.
    """
    assert facts.head_oid is not None  # guarded by `base_integration_due`
    reason = base_integration_exhausted_hold_reason(
        base=base, attempts=spent, head_sha=facts.head_oid
    )
    _logger.error(
        "review: %s — the merge-main step ran %d times in a row at head %s and "
        "reported success each time, but the head never moved and still does "
        "not merge into %s; holding instead of spending another run",
        item_id,
        spent,
        facts.head_oid[:MARKER_HEAD_CHARS],
        base,
    )
    record_base_integration_step(
        store,
        config,
        item_id=item_id,
        issue_num=facts.issue_num,
        head_sha=facts.head_oid,
        exhausted=True,
    )
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)


def _release_ci_hold(
    store: RunStore, epic_ref: str, item_id: str, *, provider: str
) -> bool:
    """Clear a ``ci_pending`` hold this node carries; others stay (mirrors the
    merge pass's own helper). True when a hold was actually released."""
    if not any(
        hold.item_id == item_id and is_ci_pending_hold(hold.reason)
        for hold in store.list_epic_holds(epic_ref)
    ):
        return False
    if provider == "github":
        store.clear_epic_hold(item_id)
    else:
        store.clear_epic_hold_item(item_id)
    return True


def _converge(
    node_id: str,
    item_id: str,
    head: str,
    round_count: int,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """Remediation is non-convergent (round cap reached). Blocking → HELD with an
    attested reason; advisory → attest DEGRADED and proceed (spec failure table)."""
    if config.review.blocking:
        _logger.warning(
            "review: %s exhausted %d remediation round(s) — HELD needs-human",
            item_id,
            round_count,
        )
        _hold(store, epic_ref, item_id, _HOLD_ROUNDS_EXHAUSTED, provider=provider)
        statuses[node_id] = DriverStatus.HELD
        outcome.held.append(node_id)
        if config.review.publish_status:
            review_status.publish_failure(repo, head, "remediation rounds exhausted")
        return
    _attest_degraded(
        node_id,
        item_id,
        head,
        None,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        repo=repo,
    )


def _latest_review_outcome_event_id(
    store: RunStore, pr_number: int | None
) -> int | None:
    """Return the newest review-outcome event id for one PR."""
    if pr_number is None:
        return None
    events = store.list_audit_events(
        limit=1,
        event=REVIEW_OUTCOME_EVENT,
        issue_num=pr_number,
    )
    return events[0].id if events else None


def _new_admission_refusal(
    store: RunStore,
    pr_number: int | None,
    prior_event_id: int | None,
) -> ReviewOutcome | None:
    """Return a new remediation refusal that requires operator admission."""
    event_id = _latest_review_outcome_event_id(store, pr_number)
    if pr_number is None or event_id is None or event_id == prior_event_id:
        return None
    outcome = latest_review_outcome(store, pr_number)
    if outcome is None:
        return None
    if outcome.classification in _REMEDIATION_REFUSALS or _run_cap_refusal(outcome):
        return outcome
    return outcome if _refusal_repeated(store, pr_number, outcome) else None


def _run_cap_refusal(outcome: ReviewOutcome) -> bool:
    """A refusal because the branch reached the producer guard's run cap (#3193).

    The cap is hard (it bounds a security check), and every later launch on the
    branch is refused the same way, so it holds at once instead of after three.
    """
    return outcome.classification == CREDENTIAL_PUBLICATION_REFUSAL and (
        is_branch_run_cap(outcome.detail)
    )


def _refusal_repeated(store: RunStore, pr_number: int, outcome: ReviewOutcome) -> bool:
    """Whether *outcome* closes a run of identical pre-launch refusals."""
    if (
        outcome.classification not in _REPEATABLE_REFUSALS
        or outcome.stage != STAGE_PRE_LAUNCH
    ):
        return False
    recent = list_review_outcomes(
        store, pr_number=pr_number, limit=_MAX_REPEATED_REFUSALS
    )
    return len(recent) == _MAX_REPEATED_REFUSALS and all(
        (prior.classification, prior.stage, prior.detail, prior.head_sha)
        == (outcome.classification, outcome.stage, outcome.detail, outcome.head_sha)
        for prior in recent
    )


def _remediation_budget_hold(
    store: RunStore, config: DispatchConfig, item_id: str
) -> str:
    """The hold a remediation-pool refusal earns (#2153).

    The refusal landed before the remediator launched, nothing is in flight, and
    every later tick refuses identically — so the node is held, not left
    `remediating`. Re-read the pool here so the reason names the CURRENT spend
    against the CURRENT cap: the two numbers an operator needs to decide what to
    raise ``[review].max_remediation_usd`` to.
    """
    cap = config.review.max_remediation_usd
    try:
        spent = check_review_budget(store, item_id, cap).issue_spent_usd
    except BudgetExceeded as exc:
        spent = exc.spent_usd
    return review_budget_hold_reason(
        limit_usd=cap, spent_usd=spent, config_key=REMEDIATION_POOL_KEY
    )


#: The hold a send-back at the branch run cap earns (#3193): what happened and
#: the operator's options, short enough to survive the 160-character echo. A
#: pushed fix moves the head, which releases the hold for one review.
_RUN_CAP_DETAIL = (
    f"branch_run_cap: the branch reached the {BRANCH_RUNS_CAP} runs the "
    "publication guard reads"
)
_RUN_CAP_OPTIONS = "options: push a fix, merge by hand, or close the PR"


def _refusal_hold_reason(
    refusal: ReviewOutcome,
    item_id: str,
    *,
    head: str,
    store: RunStore,
    config: DispatchConfig,
) -> str:
    """The durable reason one pre-launch remediation refusal is held under.

    A budget refusal clears itself the moment the cap is raised (the driver
    re-checks the pool every tick), so it is held under the review pool's own
    ``review_budget`` reason rather than the operator-unhold-only
    ``admission_refused`` one — un-holding by hand would not have helped anyway.
    """
    if refusal.classification == _BUDGET_EXHAUSTED:
        return _remediation_budget_hold(store, config, item_id)
    if refusal.classification == WINDOW_BUDGET_REFUSAL and is_window_budget_hold(
        refusal.detail
    ):
        return str(refusal.detail)
    detail = refusal.detail or refusal.classification
    if refusal.classification in {
        PROMPT_CONFIGURATION_REFUSAL,
        DEFERRED_CHECKOUT_UNRECORDED,
        *_REPEATABLE_REFUSALS,
    }:
        detail = f"{refusal.classification}: {detail}"
    if refusal.classification == _CONTAINMENT_REFUSED:
        detail += (
            " Remediation requires containment unless [sandbox] "
            "allow_uncontained_flows = true."
        )
    if refusal.classification != CREDENTIAL_PUBLICATION_REFUSAL:
        return admission_refused_hold_reason(detail, item_id)
    # A credential refusal is about the head's own lineage, so the hold names
    # that head and the tick releases it the first time the PR head moves
    # (#3193). A configuration refusal is not: a new head refuses alike, so it
    # stays the operator's to release.
    refused_head = refusal.head_sha or head
    if _run_cap_refusal(refusal):
        return admission_refused_hold_reason(
            _RUN_CAP_DETAIL, item_id, head=refused_head, action=_RUN_CAP_OPTIONS
        )
    return admission_refused_hold_reason(detail, item_id, head=refused_head)


#: What the hold says when a deferral left no evidence of its checkout. The
#: row is still a remediation whose work never reached the branch, so it still
#: stops the node; only the path a person would open is missing.
_CHECKOUT_UNKNOWN = "preserved checkout unknown"


def _deferred_publication_evidence(
    store: RunStore, run_id: str
) -> dict[str, object] | None:
    """One deferred publication's recorded facts, or None if there are none.

    ``None`` is never silently clean: the caller still holds the node, under a
    reason that says the checkout is unknown, and anything unreadable here is
    said out loud first — a row that should carry evidence and does not is the
    one case where the hold cannot name what a person must open.
    """
    raw = store.get_meta(deferred_publication_key(run_id))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        _logger.warning(
            "review: the deferred-publication evidence for run %s is not "
            "readable JSON — the hold cannot name its preserved checkout",
            run_id,
        )
        return None
    if isinstance(parsed, dict):
        return parsed
    _logger.warning(
        "review: the deferred-publication evidence for run %s is %s, not a "
        "table — the hold cannot name its preserved checkout",
        run_id,
        type(parsed).__name__,
    )
    return None


def _checkout_is_gone(path: Path) -> bool:
    """Whether an absolute preserved checkout is DEFINITELY not on disk.

    Only absence answers yes. An unreadable path — a parent the driver may not
    traverse, an I/O error — is not an absent one, and reading it as a discard
    would dispatch a remediation on top of work that is still there.
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        _logger.warning(
            "review: could not read the preserved checkout %s (%s) — it is read "
            "as still there, which is the safe way to read an unknown",
            path,
            exc,
        )
    return False


def _preserved_checkout(evidence: dict[str, object]) -> str | None:
    """One deferral's preserved checkout, or ``None`` once it is gone from disk.

    Discarding that checkout is half of the recovery every reader of this hold
    prints (``docs/cli/epic.md``, ``docs/how-it-works.md``, ``waits.py``, the
    console's hold reasons) and it is the only half the host can observe: the
    other half — publishing it — moves the PR head, and the moved head is what
    stops the deferral matching. A discard moves nothing, and the run row stays
    ``awaiting_publication`` for good, so without this probe the next tick
    re-derived the identical hold and `kew epic unhold` could never stick.

    Only an ABSOLUTE path is probed: a relative one resolves against whatever
    directory this tick happens to run in, so its absence proves nothing, and
    reading it as a discard would re-dispatch a remediation on top of work that
    is still there — the whole loop #2362 was. :func:`_checkout_is_gone` holds
    the same line for a path it cannot read.
    """
    recorded = str(evidence.get("checkout") or "")
    if not recorded:
        return _CHECKOUT_UNKNOWN
    path = Path(recorded)
    if path.is_absolute() and _checkout_is_gone(path):
        return None
    return recorded


def _deferred_remediation(store: RunStore, item_id: str, head: str) -> str | None:
    """What a person must look at, when a remediation for THIS head is deferred.

    ``None`` means nothing is deferred against *head* and the round may be
    remediated. Anything else is the detail of the hold the caller writes:
    there is a remediation run for this node whose publication the host held,
    so its work sits in a preserved checkout and the PR head is unmoved. Round
    scoping is by that head rather than by a round number, because the head is
    what a published remediation changes — an operator who publishes the
    preserved work moves it, and this stops answering.

    A row with no recorded evidence is read as deferred anyway (it is still a
    remediation that published nothing), named as such: the alternative is
    re-dispatching one, which is what #2352 did three times. A row whose
    preserved checkout is GONE is the other way out — see
    :func:`_preserved_checkout` — and stops answering, so the operator's
    `kew epic unhold` after a discard is not undone on the next tick.
    """
    from kew.run_store import REVIEW_REMEDIATION_RUN_KIND

    for run in reversed(store.list_by_item(item_id)):
        if (
            run.status != "awaiting_publication"
            or run.run_kind != REVIEW_REMEDIATION_RUN_KIND
        ):
            continue
        evidence = _deferred_publication_evidence(store, run.run_id)
        if evidence is None:
            return _CHECKOUT_UNKNOWN
        if evidence.get("head") != head:
            # A deferral against an earlier head: its work either landed (the
            # head moved, and this round is a different one) or was discarded.
            continue
        checkout = _preserved_checkout(evidence)
        if checkout is None:
            _logger.warning(
                "review: the preserved checkout for deferred remediation %s is "
                "gone (%s) — reading it as discarded, so this row no longer "
                "holds %s; `kew epic unhold` releases the node for good",
                run.run_id,
                evidence.get("checkout"),
                item_id,
            )
            continue
        return checkout
    return None


def _hold_deferred_publication(
    node_id: str,
    item_id: str,
    detail: str,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    store: RunStore,
) -> None:
    """HOLD a node whose previous remediation is waiting on a person (#2362)."""
    reason = publication_held_hold_reason(detail)
    _logger.error(
        "review: %s has a remediation awaiting publication — no new remediation "
        "dispatched; node HELD: %s",
        item_id,
        reason,
    )
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)


def _remediate(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    head: str,
    round_count: int,
    *,
    sidecar_path: str | None,
    criterion_ids: tuple[str, ...] = (),
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    remediator: Remediator | None,
    declaration: DeclarationRequest | None = None,
    proposal: DeclarationRequest | None = None,
    now: datetime,
) -> None:
    """Hand the node to the remediation seam (adapted ``kew review``, #987).

    Not while an operator's merge verify claims this head (#3198): a
    remediation pushed under it moves the head, and the merge that verify was
    for then fails on a head mismatch. The findings stay recorded on the
    round, the node is neither held nor credited a round, and the first tick
    after the claim is released or lapses remediates it.
    """
    if remediator is None:
        _logger.warning(
            "review: %s has NEEDS_CHANGES findings but no remediator is wired — "
            "node stays remediating until one is",
            item_id,
        )
        outcome.unwired.append(node_id)
        return
    if _held_by_merge_claim(
        node_id,
        item_id,
        facts,
        head,
        action="remediation",
        outcome=outcome,
        store=store,
        now=now,
    ):
        return
    if _defer_for_running_verify(
        node_id,
        item_id,
        epic_ref=epic_ref,
        outcome=outcome,
        store=store,
        config=config,
        kind="remediation",
    ):
        return
    if declaration is None:
        # A pre-review round's held-out failures (#3212): fixed or declared.
        declaration = load_held_out_request(sidecar_path)
    prior_event_id = _latest_review_outcome_event_id(store, facts.pr_number)
    result = remediator(
        RemediationContext(
            item_id=item_id,
            issue_num=facts.issue_num,
            pr_number=facts.pr_number,
            head_sha=head,
            review_round=round_count,
            sidecar_path=sidecar_path,
            criterion_ids=criterion_ids,
            reservation_held=held_reservation() is not None,
            declaration=declaration,
            proposal=None if declaration is not None else proposal,
        )
    )
    refusal = (
        result
        if result is not None and result.classification in _REMEDIATION_REFUSALS
        else _new_admission_refusal(store, facts.pr_number, prior_event_id)
    )
    if refusal is not None:
        reason = _refusal_hold_reason(
            refusal, item_id, head=head, store=store, config=config
        )
        _logger.error(
            "review: remediation refused for %s (%s) — node HELD: %s",
            item_id,
            refusal.classification,
            reason,
        )
        _hold(store, epic_ref, item_id, reason, provider=provider)
        statuses[node_id] = DriverStatus.HELD
        outcome.held.append(node_id)
        return
    outcome.remediating.append(node_id)


def _dispatch_and_collect(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    head: str,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    repo_root: Path,
    base: str,
    reviewer_launcher: ReviewerLauncher | None,
    worker_cap: int,
    now: datetime,
    gate: Callable[[], bool] = lambda: False,
) -> None:
    """Dispatch a fresh-context reviewer round and process its verdict.

    *gate* is the pre-review gate (#3201), run once the launch guards admit a
    reviewer and before anything is reserved for it; True means it took the
    node's action (a remediation round, a hold, a full fleet) and no reviewer
    launches.

    The reservation is released and the materialized worktree cleaned on EVERY
    terminal path (F6/F7 reservation hygiene); a budget refusal already holds +
    attests inside ``dispatch_reviewer``.
    """
    launcher = _launchable_reviewer(
        node_id,
        item_id,
        launcher=reviewer_launcher,
        epic_ref=epic_ref,
        outcome=outcome,
        store=store,
        config=config,
        now=now,
    )
    if launcher is None:
        return
    # The head may fail the merge gate's static checks or its ui-checks on the
    # merge candidate (#3201): that is a remediation round with the output, and
    # no reviewer is paid to find it.
    if gate():
        return
    run_id = str(uuid.uuid4())
    blocking = config.review.adversarial and config.review.blocking
    issue_num = facts.issue_num
    # A reviewer round is due on this head: surface the GitHub-native gate as
    # `pending` NOW, before the (minutes-long) reviewer runs, so a human can't
    # race the merge button while the verdict is still outstanding (#1056). A
    # force-push that moved the head lands here on the new SHA → back to pending.
    if config.review.publish_status:
        review_status.publish_pending(repo, head)
    # Round 2+ can run as a delta round against the previous reviewed head
    # (#2111); the dispatcher settles ancestry in the reviewer checkout.
    mode_plan = plan_review_mode(store.latest_review_round(item_id), current_head=head)
    # The body the merge's held-out gate will read, so an edited test it does
    # not name is a finding now rather than a failure at merge (#2830).
    pr_body = read_pr_body(repo, facts.pr_number)
    try:
        result = dispatch_reviewer(
            store,
            config,
            item_id=item_id,
            issue_num=issue_num,
            head_sha=head,
            repo_root=repo_root,
            base_ref=base,
            max_review_usd=config.review.max_review_usd,
            run_id=run_id,
            node_epic_ref=epic_ref,
            blocking=blocking,
            worker_cap=worker_cap,
            launcher=launcher,
            now=now,
            mode_plan=mode_plan,
            reserved=held_reservation() is not None,
            pr_body=pr_body,
        )
    except Exception as exc:
        # A reviewer INFRA failure (e.g. the worktree clone / materialize, or a
        # launcher that escapes its own guard) must NEVER crash the driver tick
        # (#1002). Fail closed: release the reservation the dispatch may have
        # taken (run_id-keyed, no-op if none) and route to the crash path — the
        # same handling an unparseable sidecar gets: HELD (blocking) / DEGRADED
        # attested (advisory). The spec's fail-closed contract, extended from the
        # launcher to the pre-launch materialize step.
        #
        # WHICH failure this was is read off the run row the raise left behind,
        # never assumed from the fact that something raised here (#2043): the
        # launcher's post-run bookkeeping sits outside its own except guard, so
        # a reviewer that started, streamed and exited 0 can still land in this
        # handler — and must not be published to the PR as one that never
        # started. A raise once the prompt was known still names it (#3024).
        _logger.exception(
            "review: reviewer dispatch failed for %s (infra error) — fail-closed",
            item_id,
        )
        store.release_epic_node_item(item_id, run_id=run_id)
        category, failure_layer = _raised_failure_evidence(store.get(run_id), exc)
        _record_dispatch_failure(
            store,
            config,
            run_id=run_id,
            item_id=item_id,
            issue_num=issue_num,
            error_category=category,
        )
        _handle_unparseable(
            node_id,
            item_id,
            head,
            None,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            config=config,
            store=store,
            repo=repo,
            run_id=run_id,
            failure_layer=failure_layer,
            error_category=category,
            provenance=_raised_provenance(exc),
            refusal=_raised_refusal(category, exc),
        )
        return
    if result.outcome == "budget_refused":
        # dispatch_reviewer already released the reservation + attested; in
        # blocking mode it also held the node (surface it this tick).
        if blocking:
            statuses[node_id] = DriverStatus.HELD
            outcome.held.append(node_id)
            if config.review.publish_status:
                review_status.publish_failure(repo, head, "budget refused")
        return
    if result.outcome != "dispatched":
        # fleet_full / reservation_lost: another actor beat us — retry next tick.
        return
    _process_and_release(
        node_id,
        item_id,
        facts,
        result,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        repo=repo,
        run_id=run_id,
    )


def _raised_refusal(category: str, exc: Exception) -> str | None:
    """What a dispatch that raised AFTER its reviewer run was recorded says
    went wrong (#3186), for the hold and the verdict audit: the row's category
    and the raise, redacted. ``None`` for a pre-launch category, whose hold
    names the layer's remedy instead."""
    if category in _PRE_LAUNCH_HOLDS:
        return None
    from kew.credential_surface import exception_text

    # The launcher wraps what raised; the wrapped raise is the one to name.
    cause = exc.__cause__ if isinstance(exc, ReviewerLaunchError) else None
    raised = cause if isinstance(cause, Exception) else exc
    return (
        f"the review raised after its run was recorded ({category}): "
        f"{type(raised).__name__}: {exception_text(raised)}"
    )


def _raised_provenance(exc: Exception) -> PromptProvenance:
    """The prompt a dispatch that raised had already assembled (#3024)."""
    if isinstance(exc, ReviewerLaunchError):
        return exc.provenance
    return NO_PROVENANCE


def _process_and_release(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    result: ReviewerDispatch,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    run_id: str,
) -> None:
    """Process a dispatched round's result, then release what it took.

    The reservation and the materialized checkout are given back on EVERY path
    out of the processing — a verdict, a crash route, or a raise (F6/F7
    reservation hygiene).
    """
    try:
        _process_result(
            node_id,
            item_id,
            facts,
            result,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            config=config,
            store=store,
            repo=repo,
        )
    finally:
        try:
            store.release_epic_node_item(item_id, run_id=run_id)
        finally:
            if result.worktree is not None:
                # The one removal path for a kew-made checkout: it revokes the
                # identity, removes the tree, and removes the lock file while
                # this thread still holds the claim.
                remove_checkout_tree(result.worktree)


# The crash categories that are infrastructure, not a verdict: each is retried
# by its own bounded deferral, and held by the crash path once that is spent.
_INFRA_RETRIES = {
    _RATE_LIMITED: _defer_rate_limited_review,
    _REFUSED_BEFORE_LAUNCH: _defer_refused_review,
    _RUNTIME_TIMEOUT: _defer_timed_out_launch,
    STREAM_SILENT_CATEGORY: _defer_silent_review,
}


def _route_crash_path(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    result: ReviewerDispatch,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """No usable verdict was collected. A refusal the sandbox made before launch
    is retried on the next tick (#2147), and so are a round a provider usage
    limit ended (#2219) and a launch a runtime timeout ended (#3246), each
    until its own bound is spent; anything else is the crash
    path — HELD (blocking) / DEGRADED attested (advisory), never guessed into a
    READY."""
    sidecar = result.sidecar
    digest = failure_layer = error_category = refusal = None
    if sidecar is not None:
        digest = sidecar.digest
        failure_layer = sidecar.failure_layer
        error_category = sidecar.error_category
        # A reviewer that never started left no stream to refuse: its category
        # says why, and an exit of -1 "with no result capture" would only
        # dress the launch up as a run (#3186).
        if error_category not in _PRE_LAUNCH_HOLDS:
            refusal = sidecar.parsed.unparseable_reason
        refusal = _CATEGORY_REFUSALS.get(error_category or "", refusal)
    defer = _INFRA_RETRIES.get(error_category or "")
    if defer is not None and defer(
        node_id,
        item_id,
        facts.issue_num,
        outcome=outcome,
        config=config,
        store=store,
        run_id=result.run_id,
    ):
        return
    if error_category == MULTIPLE_RESULTS_CATEGORY and _retry_multiple_results(
        node_id,
        item_id,
        result,
        refusal,
        outcome=outcome,
        store=store,
        repo=repo,
    ):
        return
    _handle_unparseable(
        node_id,
        item_id,
        result.head_sha,
        digest,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        repo=repo,
        run_id=result.run_id,
        failure_layer=failure_layer,
        error_category=error_category,
        provenance=result.provenance,
        refusal=refusal,
    )


def _retry_multiple_results(
    node_id: str,
    item_id: str,
    result: ReviewerDispatch,
    refusal: str | None,
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
    repo: str,
) -> bool:
    """Run a round whose runner answered more than once again, once (#3174).

    The reviewer exited 0 but its runner started a second turn after its first
    ``result``: a runner fault, not a verdict. Nothing is recorded against the
    review cycle and the node is not held, so the next tick re-reviews it. The
    bound is per node and per head, read from the attested retries; once spent,
    the crash path holds the node naming the refusal. Returns False then.

    The bound fails closed (#3186): a retry whose count cannot be read, or
    whose row cannot be appended, holds instead — an unrecorded retry would
    leave the count at zero and launch another paid run every tick.
    """
    try:
        spent = review_retries(
            store,
            item_id=item_id,
            head_sha=result.head_sha,
            error_category=MULTIPLE_RESULTS_CATEGORY,
        )
        if spent >= _MAX_MULTIPLE_RESULTS_RETRIES:
            return False
        emit_review_retried(
            store,
            item_id=item_id,
            head_sha=result.head_sha,
            error_category=MULTIPLE_RESULTS_CATEGORY,
            refusal=refusal,
            repo=repo,
            run_id=result.run_id,
        )
    except Exception:
        _logger.error(
            "review: %s — the retry of a reviewer that answered more than once "
            "could not be recorded; holding instead of retrying",
            item_id,
            exc_info=True,
        )
        return False
    _logger.warning(
        "review: %s — the reviewer answered more than once (%s); no verdict was "
        "taken, re-reviewing on the next tick",
        item_id,
        refusal,
    )
    # A wait, not a hold: nobody is needed while the one retry is unspent.
    outcome.refused[node_id] = (
        f"{REVIEW_RETRYING_WAIT_PREFIX} — {_HOLD_MULTIPLE_RESULTS}: the reviewer "
        f"answered more than once ({refusal or 'more than one result event'}); "
        "its one retry is next"
    )
    return True


def _held_for_unrecorded_declarations(
    node_id: str,
    item_id: str,
    result: ReviewerDispatch,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
) -> bool:
    """Hold, and record nothing, when a NEEDS_CHANGES round's flagged-test
    record did not land beside its sidecar (#2872).

    The round and its record stand together or not at all: a round recorded
    without one reads as ordinary findings, and the same head is remediated
    with the ordinary brief — the declaration loop all over again. With no
    round recorded, the release a person gives after fixing the write sends
    the head back to review, which writes the record then.
    """
    sidecar = result.sidecar
    error = result.declaration_record_error
    if error is None or sidecar is None:
        return False
    if sidecar.parsed.verdict != _VERDICT_NEEDS_CHANGES:
        return False
    reason = declaration_record_failed_hold_reason(error)
    _logger.error(
        "review: %s round at %s not recorded — its flagged-test record beside "
        "%s did not land; node HELD: %s",
        item_id,
        result.head_sha[:MARKER_HEAD_CHARS],
        sidecar.stored_path,
        reason,
    )
    _hold(store, epic_ref, item_id, reason, provider=provider_from_config(config))
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    return True


def _accepted_lines_written(
    item_id: str,
    facts: NodeFacts,
    result: ReviewerDispatch,
    verdict: str,
    *,
    repo: str,
) -> bool:
    """Write the lines the reviewer accepted into the PR's **Behavior changes**
    (#3475); False when a READY's lines are not confirmed there.

    The lines are rebuilt from the proposals in the body the reviewer was
    shown (:func:`accepted_lines`), and written with a re-read and confirm
    (:func:`carry_declared_lines`) before the round is recorded. A READY
    stands only with them in place: the merge's held-out gate reads that
    section and nothing else. Any other verdict's accepted lines are written
    the same way, and one that did not land is only logged — the round sends
    the PR back regardless, and the next reviewer rules on them again.
    """
    sidecar = result.sidecar
    assert sidecar is not None  # the caller routed the crash path already
    lines = accepted_lines(sidecar.parsed.declarations, result.pr_body)
    if not lines:
        return True
    pr_number = facts.pr_number
    if pr_number is not None and carry_declared_lines(repo, pr_number, lines):
        _logger.info(
            "review: %s reviewer accepted %d proposed Behavior changes lines; "
            "written to PR #%s",
            item_id,
            len(lines),
            pr_number,
        )
        return True
    _logger.error(
        "review: %s reviewer accepted %d proposed Behavior changes lines that "
        "are not confirmed in PR #%s",
        item_id,
        len(lines),
        pr_number,
    )
    return verdict != _VERDICT_READY


def _drop_unneeded_proposals(
    item_id: str, facts: NodeFacts, result: ReviewerDispatch, *, repo: str
) -> None:
    """Drop the proposals the reviewer ruled need no declaration (#3500).

    Those rulings are no finding, so the round stands whether or not the
    drop lands: a proposal declares nothing, and one left behind is only
    ruled on again by the next reviewer.
    """
    sidecar = result.sidecar
    assert sidecar is not None  # the caller routed the crash path already
    node_ids = unneeded_proposals(sidecar.parsed.declarations)
    pr_number = facts.pr_number
    if not node_ids or pr_number is None:
        return
    if drop_proposed_lines(repo, pr_number, node_ids):
        _logger.info(
            "review: %s reviewer ruled %d proposed Behavior changes lines not "
            "needed; dropped from PR #%s",
            item_id,
            len(node_ids),
            pr_number,
        )


def _reviewer_model_used(
    store: RunStore, run_id: str | None, config: DispatchConfig
) -> str | None:
    """The model the reviewer ran on, as its own run row recorded it (#2741).

    ``review.model`` is overridable from the console, so the config this tick
    holds can name a model set AFTER the reviewer launched. The launcher writes
    the model it launched onto the run row; that row is the record. Only a
    round with no row (the raise landed before the launcher admitted anything)
    falls back to the launcher's own resolution.
    """
    from kew.graph.driver.review_launcher import review_model

    stored = store.get(run_id) if run_id else None
    if stored is not None and stored.model:
        return stored.model
    return review_model(config) or None


def _process_result(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    result: ReviewerDispatch,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """Record + attest the collected verdict, or route the crash path."""
    sidecar = result.sidecar
    if sidecar is None or sidecar.parsed.unparseable:
        _route_crash_path(
            node_id,
            item_id,
            facts,
            result,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            config=config,
            store=store,
            repo=repo,
        )
        return
    if _held_for_unrecorded_declarations(
        node_id,
        item_id,
        result,
        statuses=statuses,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
    ):
        return
    reviewer_verdict = sidecar.parsed.verdict
    assert reviewer_verdict is not None  # not unparseable
    # A delta READY stands only with a full round in the last two (#2111),
    # and a READY on a head that touches merge gate paths only once a second
    # full round on that head confirms it (#3422) — and so do residuals the
    # stop rule would accept there; otherwise the round is recorded as
    # NEEDS_FULL and the next round runs full.
    prior = store.review_rounds_in_cycle(item_id)
    verdict = effective_verdict(
        reviewer_verdict,
        result.review_mode,
        prior,
        head_sha=result.head_sha,
        gate_paths_touched=bool(result.gate_paths),
    )
    verdict = _residuals_await_confirmation(
        verdict, result, sidecar.parsed, prior, config
    )
    if verdict != reviewer_verdict:
        _logger.warning(
            "review: %s %s round returned %s that does not stand yet "
            "(gate paths touched: %d); recording %s and running a full round next",
            item_id,
            result.review_mode,
            reviewer_verdict,
            len(result.gate_paths),
            verdict,
        )
    if not _accepted_lines_written(item_id, facts, result, verdict, repo=repo):
        reason = declarations_not_written_hold_reason(facts.pr_number)
        _logger.error("review: %s round not recorded; node HELD: %s", item_id, reason)
        _hold(store, epic_ref, item_id, reason, provider=provider_from_config(config))
        statuses[node_id] = DriverStatus.HELD
        outcome.held.append(node_id)
        return
    _drop_unneeded_proposals(item_id, facts, result, repo=repo)
    # Every later decision on this head reads these, not a checkout of its own.
    _remember_gate_paths(store, item_id, result.head_sha, result.gate_paths, config)
    round_num = store.record_review_round(
        epic_ref,
        item_id,
        head_sha=result.head_sha,
        verdict=verdict,
        sidecar_digest=sidecar.digest,
        # Persist WHERE the findings are stored so the remediation leg (#1030),
        # which runs on a later tick from persisted state, can load them.
        sidecar_path=str(sidecar.stored_path),
        review_mode=result.review_mode,
        # WHICH recorded plan this round was judged against, and the criteria
        # text it received (#2291). Both None when the item has no recorded
        # criteria, which reads as "judged against nothing stated".
        plan_sha=result.plan_sha,
        criteria_json=result.criteria_json,
        # The operator addendum the reviewer prompt carried (#3024).
        prompt_profile_version_id=result.prompt_profile_version_id,
    )
    emit_review_verdict(
        store,
        config.review,
        pr_number=facts.pr_number,
        item_id=item_id,
        head_sha=result.head_sha,
        review_round=round_num,
        verdict=verdict,
        sidecar_digest=sidecar.digest,
        reviewer_model=_reviewer_model_used(store, result.run_id, config),
        prompt_artifact_sha=result.prompt_artifact_sha,
        effective_prompt_sha=result.effective_prompt_sha,
        prompt_profile_version_id=result.prompt_profile_version_id,
        base_ref=result.base_ref,
        base_commit=result.base_commit,
        acceptance_executed=sidecar.parsed.acceptance_executed,
        acceptance_reason=sidecar.parsed.acceptance_reason,
        # Attest WHICH scope produced this verdict (#2111), so a delta READY is
        # distinguishable from a full one on the chain and the "a READY always
        # has a recent full pass behind it" rule is checkable from the chain.
        review_mode=result.review_mode,
        repo=repo,
        run_id=result.run_id,
    )
    _publish_verdict_surfaces(
        facts,
        result,
        sidecar,
        verdict,
        round_num,
        config=config,
        repo=repo,
        store=store,
    )
    outcome.verdicts[node_id] = verdict
    outcome.findings[node_id] = len(sidecar.parsed.findings)
    outcome.dispatched.append(node_id)
    recorded = store.latest_review_round(item_id)
    if verdict == _VERDICT_NEEDS_CHANGES and recorded is not None:
        _apply_stop_rule(
            node_id,
            item_id,
            facts,
            recorded,
            sidecar.parsed,
            # The reviewer's reservation on this item is still held by
            # ``_dispatch_and_collect`` (released in its ``finally``).
            reserved=True,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider_from_config(config),
            config=config,
            store=store,
            repo=repo,
            # Read off the pinned refs before the reviewer launched.
            gate_paths_touched=lambda: bool(result.gate_paths),
        )


_GATE_PATHS_META_PREFIX = "review_gate_paths:"
"""``meta`` key prefix (per item) of the merge gate paths last read for one of
its heads (#3422): ``{"head", "patterns", "paths"}`` as JSON. Written where a
reviewer dispatch read them, so later ticks on that head decide without a
checkout of their own; a new head or changed ``merge_gate_paths`` misses."""


class GatePathsUnreadError(Exception):
    """The merge gate paths of a head could not be read: nothing is decided."""


def _gate_patterns_key(config: DispatchConfig) -> str:
    patterns = json.dumps(list(config.epic.merge_gate_paths))
    return hashlib.sha256(patterns.encode()).hexdigest()


def _remember_gate_paths(
    store: RunStore,
    item_id: str,
    head: str,
    gate_paths: Sequence[str],
    config: DispatchConfig,
) -> None:
    """Record the merge gate paths read for *head*, for :func:`_gate_paths_reader`."""
    value = {
        "head": head,
        "patterns": _gate_patterns_key(config),
        "paths": list(gate_paths),
    }
    store.set_meta(f"{_GATE_PATHS_META_PREFIX}{item_id}", json.dumps(value))


def _remembered_gate_paths(
    store: RunStore, item_id: str, head: str, config: DispatchConfig
) -> list[str] | None:
    """The gate paths recorded for *head* under today's patterns, else None."""
    raw = store.get_meta(f"{_GATE_PATHS_META_PREFIX}{item_id}")
    try:
        value = json.loads(raw) if raw else None
    except ValueError as exc:
        _logger.warning(
            "review: %s recorded gate paths are unreadable, reading the head again: %s",
            item_id,
            exc,
        )
        return None
    if not isinstance(value, dict) or value.get("head") != head:
        return None
    paths = value.get("paths")
    if value.get("patterns") != _gate_patterns_key(config):
        return None
    return paths if isinstance(paths, list) else None


def _gate_paths_reader(
    store: RunStore,
    item_id: str,
    repo_root: Path,
    base: str,
    head: str,
    config: DispatchConfig,
) -> Callable[[], bool]:
    """Whether *head* touches merge gate paths, read only when asked (#3422).

    For the stop rule on a tick where no reviewer ran. The paths the reviewer
    dispatch for this head recorded answer first; otherwise they are read as a
    dispatch reads them, in a checkout of its own, and recorded. A read that
    fails raises :class:`GatePathsUnreadError`: unread is neither "touches
    none" nor "touches one", so the stop rule decides nothing this tick.
    """

    def touched() -> bool:
        known = _remembered_gate_paths(store, item_id, head, config)
        if known is not None:
            return bool(known)
        try:
            paths = head_gate_paths(repo_root, head, base, config)
        except Exception as exc:  # noqa: BLE001 — any failure is "unread"
            raise GatePathsUnreadError(
                f"the merge gate paths at {head[:MARKER_HEAD_CHARS]} could not "
                f"be read: {exc}"
            ) from exc
        _remember_gate_paths(store, item_id, head, paths, config)
        _forget_unread_gate_paths(store, item_id)
        return bool(paths)

    return touched


_GATE_PATHS_UNREAD_META_PREFIX = "review_gate_paths_unread:"
"""``meta`` key prefix (per item) of ``<head>|<count>``: the ticks in a row the
stop rule could not read that head's merge gate paths (#3422)."""


def _wait_for_unread_gate_paths(
    node_id: str,
    item_id: str,
    latest: ReviewRound,
    exc: GatePathsUnreadError,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    store: RunStore,
) -> None:
    """Count one more tick the head's gate paths went unread; hold at the bound.

    The bound is the unread PR body's (:data:`_MAX_BODY_UNREAD_PASSES`): a
    read that never succeeds would otherwise leave the node waiting with no
    hold row. Any read that succeeds starts the count over, and so does the
    hold, so a release buys the same number of reads again.
    """
    key = f"{_GATE_PATHS_UNREAD_META_PREFIX}{item_id}"
    seen, _, count = (store.get_meta(key) or "").rpartition("|")
    reads = int(count) + 1 if seen == latest.head_sha and count.isdigit() else 1
    store.set_meta(key, f"{latest.head_sha}|{reads}")
    if reads < _MAX_BODY_UNREAD_PASSES:
        _logger.warning(
            "stop rule: %s round %d waits a tick (%d of %d unread): %s",
            item_id,
            latest.round,
            reads,
            _MAX_BODY_UNREAD_PASSES,
            exc,
        )
        return
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    reason = review_gate_paths_unread_hold_reason(latest.head_sha, reads, str(cause))
    _logger.error("review: %s node HELD: %s", item_id, reason)
    _forget_unread_gate_paths(store, item_id)
    _hold(store, epic_ref, item_id, reason, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)


def _forget_unread_gate_paths(store: RunStore, item_id: str) -> None:
    """The count of unread ticks starts over."""
    key = f"{_GATE_PATHS_UNREAD_META_PREFIX}{item_id}"
    if store.get_meta(key):
        store.set_meta(key, "")


def _residuals_await_confirmation(
    verdict: str,
    result: ReviewerDispatch,
    parsed: ParsedVerdict,
    prior: Sequence[ReviewRound],
    config: DispatchConfig,
) -> str:
    """NEEDS_FULL for residuals that cannot stand on one review (#3422).

    On a head that touches merge gate paths, a full round whose findings the
    stop rule would accept as residuals is recorded like a first full READY
    there: not remediated, no ``max_rounds`` round spent, and an independent
    full round runs next on the same head. That round confirms it
    (:func:`confirms_full_ready`): its READY stands, and residuals the stop
    rule accepts again are accepted. A round that already confirms an earlier
    full READY on the head passes through for the stop rule to accept.
    """
    if (
        verdict != _VERDICT_NEEDS_CHANGES
        or result.review_mode != REVIEW_MODE_FULL
        or not result.gate_paths
        or confirms_full_ready(result.review_mode, result.head_sha, prior)
    ):
        return verdict
    next_round = max((r.round for r in prior), default=0) + 1
    if _stop_rule_verdict(next_round, verdict, parsed, config) != READY_WITH_RESIDUALS:
        return verdict
    return VERDICT_NEEDS_FULL


def _standing_verdict(latest: ReviewRound) -> ParsedVerdict | None:
    """The parsed verdict of a recorded NEEDS_CHANGES round, from its sidecar.

    None when the sidecar is unrecorded, unreadable, no longer matches the
    attested digest, or no longer parses as NEEDS_CHANGES: the stop rule then
    has nothing it can trust and the round stays in remediation.

    Parsed against the criterion ids the round itself recorded (#2291), the
    same way the remediation leg re-reads it: without them every
    ``criteria_results`` entry is dropped, and the stop rule would accept a
    round whose recorded criterion the reviewer judged ``fail``. A round with no
    recorded criteria has no ids and parses exactly as it did before #2291.
    """
    from kew.review_contract import parse_verdict_sidecar, stored_declared_tests

    if not latest.sidecar_path:
        return None
    try:
        data = Path(latest.sidecar_path).read_bytes()
    except OSError as exc:
        _logger.warning(
            "stop rule: round %s sidecar unreadable, stays in remediation: %s",
            latest.round,
            exc,
        )
        return None
    if hashlib.sha256(data).hexdigest() != latest.sidecar_digest:
        _logger.warning(
            "stop rule: round %s sidecar no longer matches its digest, "
            "stays in remediation",
            latest.round,
        )
        return None
    # The round's contract-test ids too (#2732): a cited failing contract test
    # is a ``fail`` the stop rule must see, or the round reads as residual-only.
    parsed = parse_verdict_sidecar(
        data,
        plan_criterion_ids=recorded_criterion_ids(latest.criteria_json),
        contract_test_ids=recorded_contract_test_ids(latest.criteria_json),
        # The declared set the round itself was parsed against (#3516).
        declared_tests=stored_declared_tests(latest.sidecar_path),
    )
    if parsed.verdict != _VERDICT_NEEDS_CHANGES:
        return None
    return parsed


def _apply_stop_rule(
    node_id: str,
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    parsed: ParsedVerdict | None,
    *,
    reserved: bool,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    gate_paths_touched: Callable[[], bool],
) -> bool:
    """Accept a late residual-only NEEDS_CHANGES round as READY-with-residuals (#2112).

    ``reserved`` says the caller already holds this item's review reservation —
    true on the dispatch path, where the reviewer's own reservation is still
    held; false on the standing-round path, where the filing takes one itself.
    ``gate_paths_touched`` says whether the round's head changes merge gate
    paths (#3422); see :func:`_gate_paths_allow`.

    Returns False when the stop rule does not apply, so the caller remediates or
    converges as before. When it applies, the residual issue is filed (once per
    round), the acceptance attested, and the ``kew-review`` status flipped to
    success; the overlay and merge gate then read the round as READY. A filing
    or attestation failure holds the node instead (advisory mode degrades), so it
    never merges on an acceptance that is not on record. A ledger mark that fails
    on a round still standing is the one retryable case: nothing moves and the
    next tick re-marks it (:func:`_mark_retry_pending`).

    An acceptance ALREADY attested for *latest* runs this path even when the rule
    can no longer be re-decided (:func:`_rule_accepts` needs a sidecar the driver
    still trusts): the attested fact is what the readers owe a mark to. Requiring
    the sidecar for the retry would strand a round whose sidecar has since been
    rotated away — or a pre-v59 round, which never had a mark — in remediation,
    with the merge gate waiting on a write nothing makes.
    """
    attested = accepted_residuals(store, item_id, latest)
    try:
        accepts = attested is not None or _rule_accepts(
            latest, parsed, config, store=store, gate_paths_touched=gate_paths_touched
        )
    except GatePathsUnreadError as exc:
        # The round stands as it is, and the next tick reads the head again
        # rather than accept or remediate on a guess — up to a bound.
        _wait_for_unread_gate_paths(
            node_id,
            item_id,
            latest,
            exc,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            store=store,
        )
        return True
    if not accepts:
        return False
    try:
        issue = _accept_round(
            item_id,
            facts,
            latest,
            parsed.findings if parsed is not None else [],
            reserved=reserved,
            config=config,
            store=store,
            repo=repo,
        )
    except RoundMarkRetryError as exc:
        _mark_retry_pending(item_id, latest, exc)
        return True
    except ResidualFilingError as exc:
        _residuals_unfiled(
            node_id,
            item_id,
            latest,
            exc,
            statuses=statuses,
            outcome=outcome,
            epic_ref=epic_ref,
            provider=provider,
            config=config,
            store=store,
            repo=repo,
        )
        return True
    if issue is None:
        # Another actor holds the node's review reservation: it owns the filing.
        # Nothing moved, so the node keeps the status it came in with and the
        # next tick re-reads the acceptance (or files it).
        _logger.info(
            "review: %s round %d met the stop rule but another actor holds the "
            "node's review reservation — its residuals file on a later tick",
            item_id,
            latest.round,
        )
        return True
    _logger.warning(
        "review: %s round %d accepted as READY-with-residuals (issue #%d)",
        item_id,
        latest.round,
        issue,
    )
    if config.review.publish_status:
        review_status.publish_residuals_accepted(repo, latest.head_sha, issue)
    outcome.verdicts[node_id] = READY_WITH_RESIDUALS
    outcome.residuals[node_id] = issue
    return True


def _rule_accepts(
    latest: ReviewRound,
    parsed: ParsedVerdict | None,
    config: DispatchConfig,
    *,
    store: RunStore,
    gate_paths_touched: Callable[[], bool],
) -> bool:
    """Whether the stop rule accepts *latest* on the findings it returned.

    None *parsed* is a round whose sidecar the driver cannot trust (unrecorded,
    unreadable, digest-mismatched, or no longer NEEDS_CHANGES): the rule has
    nothing to decide on, so it does not fire.

    *parsed* must have been parsed against the round's recorded criterion ids
    (``criteria_json``), or its ``failing_criteria`` are empty and a round that
    missed what it was for reads as residual-only — see
    :func:`_standing_verdict`, which re-parses with them for exactly this.
    """
    if parsed is None:
        return False
    decision = _stop_rule_verdict(latest.round, latest.verdict, parsed, config)
    if decision != READY_WITH_RESIDUALS:
        return False
    return _gate_paths_allow(latest, store, gate_paths_touched)


def _stop_rule_verdict(
    round_num: int, verdict: str, parsed: ParsedVerdict, config: DispatchConfig
) -> str:
    """:func:`stop_rule_decision` on a parsed round under ``[review]``."""
    return stop_rule_decision(
        round_num,
        verdict,
        parsed.findings,
        after_rounds=config.review.stop_rule_after_rounds,
        max_severity=config.review.stop_rule_max_severity,
        dropped_findings=parsed.dropped_findings,
        # A failed recorded criterion is never residual (#2291): the rule
        # accepts defects in how the work was done, not the work not doing
        # what the item asked for.
        failing_criteria=parsed.failing_criteria,
    )


def _gate_paths_allow(
    latest: ReviewRound, store: RunStore, gate_paths_touched: Callable[[], bool]
) -> bool:
    """Whether the merge gate paths rule lets the stop rule accept *latest* (#3422).

    A head that touches merge gate paths owes two full reviews before anything
    stands on it, and accepted residuals stand like a READY. So the stop rule
    accepts such a round only when it is the full round confirming an earlier
    full round on the same head that returned READY, or residuals the rule
    would accept (both recorded ``NEEDS_FULL``; see
    :func:`_residuals_await_confirmation` and :func:`confirms_full_ready`).

    A round the reviewer dispatch records never reaches the refusal below: an
    unconfirmed residual-only full round on a gate-path head is recorded
    ``NEEDS_FULL``, so it is neither remediated nor counted against
    ``max_rounds``, and a confirming full round runs next. Only a
    ``NEEDS_CHANGES`` row dispatch did not decide that way (one recorded
    before #3422, or a round that was no full review) is refused here; its
    NEEDS_CHANGES then stands and is remediated. The gate paths are read last,
    only for a round the rule would otherwise accept, and a head whose paths
    cannot be read (:class:`GatePathsUnreadError`) waits for the next tick.
    """
    prior = [
        r
        for r in store.review_rounds_in_cycle(latest.item_id)
        if r.round < latest.round
    ]
    if confirms_full_ready(latest.review_mode, latest.head_sha, prior):
        return True
    if not gate_paths_touched():
        return True
    _logger.warning(
        "stop rule: %s round %d is residual-only, but its head touches merge "
        "gate paths and no full round on it confirmed a READY; it stays in "
        "remediation",
        latest.item_id,
        latest.round,
    )
    return False


def _mark_retry_pending(
    item_id: str, latest: ReviewRound, exc: RoundMarkRetryError
) -> None:
    """The acceptance is attested but not yet marked on its round (#2151).

    Nothing moved: the round stays the standing ``NEEDS_CHANGES`` this tick read,
    so the next tick re-reads the same acceptance and retries the mark. Recording
    a DEGRADED round instead would make that round the latest and cost a full
    reviewer round on the same head to get back here; holding would freeze the
    node until an operator unheld it for a write the driver can redo itself.

    The merge gate reads the mark too (``acceptance_on_record``), so the node
    cannot merge while the ledger disagrees — which is why leaving it in review
    is safe rather than silent.
    """
    _logger.error(
        "review: %s round %d: %s — no action this tick; the next tick retries "
        "the mark, and the merge gate holds the node until it lands",
        item_id,
        latest.round,
        exc,
    )


def _residuals_unfiled(
    node_id: str,
    item_id: str,
    latest: ReviewRound,
    exc: ResidualFilingError,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    provider: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> None:
    """The stop rule fired but its acceptance is not on record.

    Blocking → HELD ``review_residuals_unfiled``; advisory → attest DEGRADED and
    proceed, exactly as ``_converge`` and ``_handle_unparseable`` do (spec failure
    table). Branching on ``blocking`` is what keeps a transient ``gh`` error from
    turning the advisory on-ramp into a durable hold only ``kew epic unhold``
    clears.

    A ledger mark the driver can simply redo never reaches here
    (:func:`_mark_retry_pending`): what does is a filing or attestation that is
    not on record at all, or an acceptance no recorded round matches — neither
    heals by re-running the rule against the same state.
    """
    if not config.review.blocking:
        _logger.warning(
            "review: %s round %d met the stop rule but its residuals are not on "
            "record (%s) — advisory review, attesting DEGRADED and proceeding",
            item_id,
            latest.round,
            exc,
        )
        _attest_degraded(
            node_id,
            item_id,
            latest.head_sha,
            latest.sidecar_digest,
            outcome=outcome,
            epic_ref=epic_ref,
            config=config,
            store=store,
            repo=repo,
        )
        return
    _logger.error(
        "review: %s round %d met the stop rule but its residuals are not on "
        "record (%s) — HELD %s; fix the cause, then kew epic unhold",
        item_id,
        latest.round,
        exc,
        _HOLD_RESIDUALS_UNFILED,
    )
    _hold(store, epic_ref, item_id, _HOLD_RESIDUALS_UNFILED, provider=provider)
    statuses[node_id] = DriverStatus.HELD
    outcome.held.append(node_id)
    if config.review.publish_status:
        review_status.publish_failure(repo, latest.head_sha, "residual issue not filed")


def _accept_round(
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    findings: list[Finding],
    *,
    reserved: bool,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> int | None:
    """The accepted round's residual issue — filed once, and marked on the round.

    The chain event is the attested acceptance; the mark (#2151) is what a reader
    polling ``review_rounds`` sees, and the merge gate requires both
    (``acceptance_on_record``), so neither half can merge the node on its own.
    This runs on every tick that reads an acceptance, not only the filing one, so
    a mark that failed once is retried rather than lost.

    Returns None when another actor holds the item's review reservation: that
    actor owns both writes.
    """
    issue = _file_residuals_once(
        item_id,
        facts,
        latest,
        findings,
        reserved=reserved,
        config=config,
        store=store,
        repo=repo,
    )
    if issue is not None:
        mark_round_accepted(store, item_id, latest, issue=issue)
    return issue


def _file_residuals_once(
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    findings: list[Finding],
    *,
    reserved: bool,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> int | None:
    """The round's residual issue number, filing it only if none is on record.

    ``gh issue create`` is not idempotent, so the "is one on record?" check and
    the filing must not straddle another actor: two overlapping driver ticks
    would each file an issue for the same round while the acceptance dedup key
    admits only one, orphaning a bug issue and leaving a PR comment naming an
    issue the chain does not. The pair therefore runs inside this item's review
    reservation — the same per-item mutex the reviewer and remediator take, and
    UNIQUE on ``item_id`` across the whole table, so only one holder exists.
    Returns ``None`` when another actor holds it: that actor owns the filing and
    this tick takes no action for the node.

    ``reserved`` is the dispatch path, where the reviewer's own reservation is
    still held (releasing and re-taking it would open the very window this
    closes), so the reservation is already proven and the filing runs under it.
    """
    existing = accepted_residuals(store, item_id, latest)
    if existing is not None and existing.payload is not None:
        return int(existing.payload["issue"])
    if reserved:
        return _file_and_record(
            item_id, facts, latest, findings, config=config, store=store, repo=repo
        )
    run_id = str(uuid.uuid4())
    if not store.reserve_epic_node_item(REVIEW_EPIC_REF, item_id, run_id):
        return None
    try:
        # Re-read inside the mutex: the actor that just released it may have
        # filed this very round's issue between the check above and the reserve.
        existing = accepted_residuals(store, item_id, latest)
        if existing is not None and existing.payload is not None:
            return int(existing.payload["issue"])
        return _file_and_record(
            item_id, facts, latest, findings, config=config, store=store, repo=repo
        )
    finally:
        store.release_epic_node_item(item_id, run_id=run_id)


def _file_and_record(
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    findings: list[Finding],
    *,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
) -> int:
    """File the round's residual issue and attest its acceptance.

    An earlier accepted round that reported the same findings already has an
    OPEN issue for them (a push voids the acceptance, not the findings), so that
    issue is reused rather than re-filed — the new acceptance points at it, the
    first one is never orphaned, and no duplicate is filed for a residual that
    drifted a line or a word between rounds (#2152).
    """
    after_rounds = config.review.stop_rule_after_rounds
    max_severity = config.review.stop_rule_max_severity
    match = matching_residual_issue(
        store,
        item_id,
        findings,
        is_open=lambda issue: residual_issue_is_open(repo, issue),
    )
    issue = (
        match.issue
        if match is not None
        else _file_new(
            item_id,
            facts,
            latest,
            findings,
            after_rounds=after_rounds,
            max_severity=max_severity,
            repo=repo,
        )
    )
    record_acceptance(
        store,
        item_id=item_id,
        repo=repo,
        pr_number=facts.pr_number,
        round_num=latest.round,
        head_sha=latest.head_sha,
        sidecar_digest=latest.sidecar_digest,
        issue=issue,
        after_rounds=after_rounds,
        findings=findings,
        reused=match is not None,
    )
    if match is not None:
        comment_residual_round(
            repo,
            issue,
            item_id=item_id,
            pr_number=facts.pr_number,
            round_num=latest.round,
            head_sha=latest.head_sha,
            sidecar_digest=latest.sidecar_digest,
            findings=findings,
            exact=match.exact,
        )
    comment_residuals(
        repo,
        facts.pr_number,
        issue=issue,
        round_num=latest.round,
        reused=match is not None,
    )
    return issue


def _file_new(
    item_id: str,
    facts: NodeFacts,
    latest: ReviewRound,
    findings: list[Finding],
    *,
    after_rounds: int,
    max_severity: str,
    repo: str,
) -> int:
    """File a fresh residual issue for the round: no open issue matched it."""
    pr_ref = f"PR #{facts.pr_number}" if facts.pr_number is not None else item_id
    return file_residual_issue(
        repo,
        title=f"Review residuals: {pr_ref} round {latest.round}",
        body=residual_issue_body(
            item_id=item_id,
            pr_number=facts.pr_number,
            round_num=latest.round,
            head_sha=latest.head_sha,
            sidecar_digest=latest.sidecar_digest,
            after_rounds=after_rounds,
            max_severity=max_severity,
            findings=findings,
        ),
    )


def _publish_verdict_surfaces(
    facts: NodeFacts,
    result: ReviewerDispatch,
    sidecar: CollectedSidecar,
    verdict: str,
    round_num: int,
    *,
    config: DispatchConfig,
    repo: str,
    store: RunStore,
) -> None:
    # Mirror the verdict onto the PR head SHA: READY (bound to this head) flips
    # the required `kew-review` check to success; NEEDS_CHANGES holds it failure
    # until a later round on the current head records READY (#1056).
    # NEEDS_FULL (#2111) is not a failure: the full round it calls for is
    # still due on this head, so the gate stays pending.
    if config.review.publish_status and verdict == VERDICT_NEEDS_FULL:
        review_status.publish_pending(repo, result.head_sha)
    elif config.review.publish_status:
        review_status.publish_verdict(repo, result.head_sha, verdict)
    # Publish the full verdict details to the PR as an edit-in-place comment
    # (#1046): one comment per PR, updated in place on every round.
    if config.review.publish_comment:
        review_comment.post_verdict_comment(
            repo,
            facts.pr_number,
            verdict=verdict,
            head_sha=result.head_sha,
            current_head_sha=facts.head_oid or result.head_sha,
            review_round=round_num,
            sidecar_id=sidecar.digest,
            findings=sidecar.parsed.findings,
            acceptance_evidence=sidecar.parsed.acceptance_evidence,
            acceptance_executed=sidecar.parsed.acceptance_executed,
            acceptance_reason=sidecar.parsed.acceptance_reason,
            # The per-criterion verdict (#2291), and every criterion this round
            # did NOT judge: the ids the prompt could not carry, and the whole
            # recorded plan when a stale artifact meant none were rendered. The
            # comment is the surface the merge is approved from, so a narrowed
            # contract has to be legible there, not only in the driver log.
            plan_sha=result.plan_sha,
            criteria_results=sidecar.parsed.criteria_results,
            omitted_criterion_ids=result.omitted_criterion_ids,
            unjudged_criteria=result.unjudged_criteria,
            # What the reviewer checked, and could not check (#3419) — shown
            # only for a round that was held to the coverage contract.
            coverage=sidecar.parsed.coverage,
            not_checked=(
                sidecar.parsed.not_checked if sidecar.parsed.coverage_required else None
            ),
            not_checked_dropped=sidecar.parsed.dropped_not_checked,
            blocking=config.review.blocking,
            # How near the branch is to the producer guard's run cap (#3193).
            branch_runs=_branch_run_count(store, result.item_id),
            # The pre-review gate's skip on this head, if any (#3280).
            gate_skipped=gate_skip_reason(store, result.item_id, result.head_sha),
            # Why a full READY awaits a second full review (#3422). A delta
            # round's NEEDS_FULL is the full-pass rule's, not this one's.
            gate_paths=(
                result.gate_paths if result.review_mode == REVIEW_MODE_FULL else ()
            ),
            # The tests kew excused as declared off the findings (#3516).
            excused_tests=sidecar.parsed.excused_tests,
        )


def _branch_run_count(store: RunStore, item_id: str) -> int:
    """How many runs the producer guard reads on this item's branch, or 0.

    The branch is the one the item's own runs name. Best-effort: a count that
    cannot be read only drops the warning, never the comment.
    """
    from kew.publication_credentials import publication_branch_runs

    try:
        branch = next(
            (run.branch for run in reversed(store.list_by_item(item_id)) if run.branch),
            "",
        )
        return len(publication_branch_runs(store, branch=branch)) if branch else 0
    except Exception as exc:  # noqa: BLE001 — the warning is advisory
        _logger.warning(
            "review: cannot count the runs on %s's branch: %s", item_id, exc
        )
        return 0


def _handle_unparseable(
    node_id: str,
    item_id: str,
    head: str,
    digest: str | None,
    *,
    statuses: dict[str, DriverStatus],
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    run_id: str | None = None,
    failure_layer: str | None = None,
    error_category: str | None = None,
    provenance: PromptProvenance = NO_PROVENANCE,
    refusal: str | None = None,
) -> None:
    """An unparseable / evidence-free sidecar is the crash path — never guessed
    into a READY. Blocking → HELD review_unparseable; advisory → DEGRADED
    attested + proceed. Either way the verdict names the prompt the reviewer
    ran under (*provenance*, #3024). A reviewer that provably never started
    received no prompt, so its verdict and round attest no received text and
    no profile; the base artifact governed the attempt and is still named."""
    hold_reason = _crash_hold_reason(error_category)
    if error_category in _PRE_LAUNCH_HOLDS:
        provenance = provenance.artifact_only()
    if config.review.adversarial and config.review.blocking:
        _logger.warning(
            "review: %s produced no usable verdict (%s) — HELD needs-human",
            item_id,
            hold_reason,
        )
        # The HELD node still owes the chain its launch evidence (#1393): attest
        # the crash with its failing layer + error category so a dark lane is
        # diagnosable from the verdict stream under blocking too, not only in
        # advisory mode. Emit-only (no recorded round — the round never
        # produced a verdict), mirroring the MERGED_UNREVIEWED precedent.
        emit_review_verdict(
            store,
            config.review,
            pr_number=None,
            item_id=item_id,
            head_sha=head,
            review_round=store.review_round_count(item_id) + 1,
            verdict=VERDICT_UNPARSEABLE,
            sidecar_digest=digest,
            reviewer_model=_reviewer_model_used(store, run_id, config),
            prompt_artifact_sha=provenance.prompt_artifact_sha,
            effective_prompt_sha=provenance.effective_prompt_sha,
            prompt_profile_version_id=provenance.prompt_profile_version_id,
            base_ref=provenance.base_ref,
            base_commit=provenance.base_commit,
            repo=repo,
            run_id=run_id,
            failure_layer=failure_layer,
            error_category=error_category,
            refusal=refusal,
        )
        _hold(
            store,
            epic_ref,
            item_id,
            _crash_hold_reason_with_fix(
                hold_reason,
                failure_layer,
                config,
                container=_reviewer_container(store, run_id, item_id),
                credential=_limited_round_credential(store, run_id),
                error_category=error_category,
                refusal=refusal,
            ),
            provider=provider_from_config(config),
        )
        statuses[node_id] = DriverStatus.HELD
        outcome.held.append(node_id)
        if config.review.publish_status:
            review_status.publish_failure(repo, head, _CRASH_STATUS_TEXT[hold_reason])
        return
    _attest_degraded(
        node_id,
        item_id,
        head,
        digest,
        outcome=outcome,
        epic_ref=epic_ref,
        config=config,
        store=store,
        repo=repo,
        run_id=run_id,
        failure_layer=failure_layer,
        error_category=error_category,
        provenance=provenance,
        refusal=refusal,
    )


def _attest_degraded(
    node_id: str,
    item_id: str,
    head: str,
    digest: str | None,
    *,
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    repo: str,
    run_id: str | None = None,
    failure_layer: str | None = None,
    error_category: str | None = None,
    provenance: PromptProvenance = NO_PROVENANCE,
    refusal: str | None = None,
) -> None:
    """Advisory-mode degrade: record a DEGRADED round + attest it, then proceed
    (the overlay reads DEGRADED as neither READY nor NEEDS_CHANGES → IN_REVIEW).
    A degrade from a launched reviewer records the prompt it ran under
    (*provenance*, #3024) on the round and the attested verdict."""
    round_num = store.record_review_round(
        epic_ref,
        item_id,
        head_sha=head,
        verdict=VERDICT_DEGRADED,
        sidecar_digest=digest,
        prompt_profile_version_id=provenance.prompt_profile_version_id,
    )
    emit_review_verdict(
        store,
        config.review,
        pr_number=None,
        item_id=item_id,
        head_sha=head,
        review_round=round_num,
        verdict=VERDICT_DEGRADED,
        sidecar_digest=digest,
        repo=repo,
        run_id=run_id,
        reviewer_model=_reviewer_model_used(store, run_id, config),
        prompt_artifact_sha=provenance.prompt_artifact_sha,
        effective_prompt_sha=provenance.effective_prompt_sha,
        prompt_profile_version_id=provenance.prompt_profile_version_id,
        base_ref=provenance.base_ref,
        base_commit=provenance.base_commit,
        failure_layer=failure_layer,
        error_category=error_category,
        refusal=refusal,
    )
    # Preserve the DEGRADED shadow verdict without making advisory review a
    # delivery gate. Other required-check failures still keep GitHub non-green.
    if config.review.publish_status:
        review_status.publish_advisory_degraded(repo, head)
    outcome.verdicts[node_id] = VERDICT_DEGRADED
