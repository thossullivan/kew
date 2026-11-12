"""Node-addressable "needs you" attention, in the judgment queue's own rules.

The epic DAG's needs-you diamond and the console's judgment queue must never
disagree about who is waiting on a person, so both read from THIS module: the
work projection (``work_projection``) and the row wording (``work_reason``,
``approval_reason``) live here, and ``kew.service.dashboard`` imports them
rather than keeping a second, faithful-for-now copy. Two implementations of one
rule is how one system starts telling one user two stories.

Only *node-addressable* attention is composed — an item's artifact awaiting a
human decision, an item's other judgment-queue exception, and an open spend
approval hold. A stalled driver, an orphan PR and an epic CI hold are not facts
about one node (a CI hold is a wait, not a person), and the console keeps
reporting them where it already does.

Silence on failure: a node's ``attention`` of ``None`` means "no person is
waited on" ONLY when the returned ``unavailable`` names nothing. Every read
that did not happen is NAMED there instead of collapsing to a clean ``None``,
because "nobody is waiting" and "we could not tell" are different worlds.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from kew.approval_basis import approval_sentence
from kew.epic_archive import epic_is_retired, is_archived
from kew.epic_holds import holds_node_out_of_frontier, safe_hold_reason
from kew.epic_node_states import DONE
from kew.ids import GITHUB_ID_RE, MAX_ISSUE_NUM_DIGITS, item_ref
from kew.pr_states import MERGED, pr_states_by_url
from kew.report import open_approval_holds
from kew.service.merge_verify_reading import (
    VerifyUnreadable,
    failed_verify_sentence,
    item_merge_verify,
    read_verify_rows,
)
from kew.service.models import (
    DashboardRowDTO,
    NodeAttentionDTO,
    WaitLinkDTO,
    WorkProjectionDTO,
    WorkViewDTO,
)
from kew.service.work import (
    PullRequestFact,
    cached_pull_request,
    compose_work_projection,
    pending_run_max_age_s,
    run_executing,
)
from kew.waits import (
    WaitClassification,
    bucket_for,
    classify_epic_wait,
    classify_wait,
    fill_command,
    wait_label,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

#: One item's journaled tree state and the instant it was read at.
_MergeStates = dict[str, tuple[str | None, str | None]]

# The canonical work projection's exceptions that are an ARTIFACT awaiting a
# person. Everything here keeps the shipped ``pr_review`` row type (the wire
# contract the frontend groups on); everything else the projection can name
# gets the additive ``work_exception`` type.
PR_REVIEW_EXCEPTIONS = frozenset(
    {
        "awaiting_human_decision",
        "changes_requested",
        "checks_failing",
        "merge_conflict",
        "declined",
        "judgment_unknown",
        "source_desynchronized",
    }
)

#: Projection exceptions that are kew's OWN clock rather than a person's move
#: (#2279). The console rations magenta for "a person is the next move"
#: (DESIGN.md), and these three spent it on work nobody had been handed:
#:
#: * ``in_flight`` — a run kew is executing. #2272 counted in the header's
#:   "2 needs you" as "in flight: run pending" while kew was running it (D28).
#:   Quiet only while the row is YOUNG: every pending row has an age bound on
#:   it (``PENDING_RUN_MAX_AGE_S``, from ``pending_run_max_age_s``, in
#:   kew.service.work), past which it is not a run kew is executing but a row a
#:   dead host left behind, and the abandoned attempt has an exception of its
#:   own — ``run_abandoned`` — deliberately NOT in this set. Leaving it quiet
#:   dropped the item from every needs-you surface for good, since the store's
#:   own backstop will not finalize a row whose owner it cannot prove dead.
#: * ``review_in_flight`` — a governed review round kew is reading the pull
#:   request with. It becomes a needs-you when the verdict lands (D8) — or when
#:   the round stops reporting, which is the same age bound and the same
#:   reasoning one dimension over: the abandoned round has ``review_abandoned``,
#:   also not in this set, because a row nothing will finalize is not a run kew
#:   is executing, and leaving it quiet hid the artifact for good.
#: * ``review_remediation_due`` / ``review_round_due`` — the same cycle
#:   BETWEEN two of its rounds (#2328). The last verdict was NEEDS_CHANGES or
#:   NEEDS_FULL, so what comes next is kew's own remediation or its next full
#:   round; #2278 sat there after round 4 and counted in the header beside
#:   #2279, which was READY and really awaiting a merge decision. Only the
#:   verdict that ENDS the cycle (READY, READY_WITH_RESIDUALS) hands the
#:   artifact to a person — and a node the driver HELD (the remediation cap,
#:   a base-integration conflict) is a needs-you through the epic-hold source,
#:   with the command that clears it, whatever the projection says here.
#: * ``terminal_failure`` — the attempt died. That is ONE reading everywhere
#:   else already: ``kew.waits``'s ``failed-terminal`` bucket names no kind,
#:   the driver's live attention stream drops terminal nodes outright
#:   (``graph/driver/attention.py``), and the epic legend counts no diamond for
#:   one. Work's magenta "manual" and this set's row were the two surfaces that
#:   disagreed, and #2053 was drawn three ways at once because of it (D7). The
#:   failure keeps the failed mark and the words the driver recorded; what it
#:   loses is the colour that means somebody was asked.
#: * ``remote_unavailable`` — the projection could not READ the tracker item or
#:   the pull request, so it does not know what the item needs (#2893). That
#:   is a read failure, not a person's next move: live #2870 sat in needs-you
#:   as "remote unavailable: the source item's current state could not be
#:   read", spending the magenta on kew's own outage. The read is retried by
#:   the next poll without anybody asking, and the view keeps saying it could
#:   not read (its exception, its ``observe_remote`` action and the
#:   unavailable observation) to whoever opens the item.
#:
#: Read by BOTH readers of the projection — the needs-you set's rows
#: (``kew.service.needs_you._work_rows``) and the node join below — so a
#: diamond on the DAG and a row in the judgment queue cannot disagree about
#: whether anybody was asked.
QUIET_EXCEPTIONS = frozenset(
    {
        "none",
        "in_flight",
        "review_in_flight",
        "review_remediation_due",
        "review_round_due",
        "terminal_failure",
        "remote_unavailable",
    }
)


def needs_a_person(view: WorkViewDTO) -> bool:
    """Whether this projection view is a thing a PERSON has to do."""
    return view.exception.state not in QUIET_EXCEPTIONS


# Named entries in ``AttentionJoin.unavailable``. ``UNREADABLE_PROJECTION`` also
# covers "the join was never attempted" (no config passed): from a reader's
# side both mean the same thing — this snapshot cannot say who is waiting.
UNREADABLE_PROJECTION = "attention"
UNREADABLE_HOLDS = "attention:approval_holds"
# The epic-hold read failed — again every node's, not one's.
UNREADABLE_EPIC_HOLDS = "attention:epic_holds"
# The archive decision could not be read, so whether this epic is retired —
# and with it whether its journaled waits belong in the set — is unknown.
UNREADABLE_ARCHIVE = "attention:epic_archive"


def unreadable_item(item_id: str) -> str:
    """The ``unavailable`` entry naming ONE item whose work facts failed."""
    return f"attention:{item_id}"


@dataclass(frozen=True)
class AttentionJoin:
    """Attention by node id, plus what could not be read to compose it."""

    by_node: dict[str, NodeAttentionDTO] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)


def rec_item_id(rec: Any) -> str:
    """The record's durable item id, falling back to the stringified issue
    number for a pre-item_id row (item_id is populated everywhere post-A3, but
    a legacy GitHub row still keys correctly off its numeric id)."""
    item_id = getattr(rec, "item_id", None)
    if item_id:
        return str(item_id)
    return str(getattr(rec, "issue_num", "") or "")


def work_projection(
    store: Any,
    config: DispatchConfig,
    *,
    item_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> WorkProjectionDTO:
    """The snapshot-bound projection every judgment surface is read from.

    One held read snapshot over every item with a live delivery artifact, plus
    ONE human-review pressure reading shared by all of them — so no two rows
    can disagree about how full the reviewer queue is, and none of them
    reinterprets a raw ``RunStatus`` on its own.

    ``item_ids`` narrows the projection to those items; ``None`` keeps every
    syncable item, which is what the console's judgment queue reads. A node
    join wants its own handful, and reading — then composing — the whole
    store's work only to discard all but three costs a quiet 1s epic poll most
    of its budget once a host holds thousands of open PRs (#2096). The scope is
    pushed down to the store's own query so the unrelated rows are never
    materialized. Narrowing is a scope change, not a rule change: each
    surviving view is composed inside the same snapshot, by the same rules, off
    the same shared pressure reading.
    """
    scope = None if item_ids is None else sorted({str(n) for n in item_ids if n})
    syncable = list(
        store.list_syncable_runs()
        if scope is None
        else store.list_syncable_runs(item_ids=scope)
    )
    merge_states = _journaled_merge_states(store, scope)
    cached = {
        rec_item_id(rec): _cached_artifact(rec, merge_states, now=now)
        for rec in syncable
        if rec.pr_url
    }
    return compose_work_projection(
        store,
        list(cached),
        pull_requests=cached,
        delivery_repo=config.project.repo or None,
        composed_at=now,
        # The in-flight bound is the OPERATOR's, not a constant's: a pending row
        # is read as dead at twice this host's ``agent.timeout_minutes``, so a
        # raised budget cannot have its live rounds called abandoned mid-review
        # (#2279/D8, pointing the other way).
        pending_max_age_s=pending_run_max_age_s(config),
        # A governed epic node with no round yet is kew's move, not a merge
        # decision — but only where this host runs the review stage (#2576).
        review_governed=config.review.adversarial,
    )


#: What an item with nothing journaled about its tree answers: no state, and no
#: instant to age one by. Both halves absent leaves ``mergeability`` unknown.
_NO_MERGE_STATE: tuple[str | None, str | None] = (None, None)


#: Rows asked for per page of the journaled-fact read, and how many pages one
#: projection follows before it stops. The store clamps the page to its own
#: ceiling (``run_store.MAX_NODE_PR_FACTS_LIMIT``, which
#: ``test_service_freshness.py`` pins this against), so the product is the
#: number of open linked artifacts one projection can read tree state for — far
#: past any real host, and still a BOUND: a journal that answered the same
#: cursor forever must not be able to spin a 1s epic poll.
_MERGE_STATE_PAGE = 1000
_MERGE_STATE_MAX_PAGES = 20

_NO_JOURNAL_READER = "this store does not journal the driver's pull-request facts"
_REFUSED_JOURNAL_READ = "the journaled merge-state read refused: "
_TRUNCATED_JOURNAL_READ = (
    "the journaled merge-state read stopped after "
    f"{_MERGE_STATE_PAGE * _MERGE_STATE_MAX_PAGES} rows"
)


@dataclass(frozen=True)
class _JournaledMergeStates:
    """The journaled tree states one projection read, and what it did NOT read.

    ``unread`` is the honesty half. An item absent from ``by_item`` has no
    journaled merge state OR was never reached, and those are different worlds:
    the first is "no tick recorded a tree state", the second is "we could not
    look". ``unread`` names the second so the composed artifact's detail can
    say it (``work.cached_pull_request``'s ``merge_state_unread``) instead of
    letting a truncated read look like a clean tree.
    """

    by_item: _MergeStates = field(default_factory=dict)
    unread: str | None = None
    #: ``item_id -> head commit`` off the same rows (#2576): the head a review
    #: round is held against. An item absent here has no head in hand.
    heads: dict[str, str] = field(default_factory=dict)


def _cached_artifact(
    record: Any, journaled: _JournaledMergeStates, *, now: datetime | None
) -> PullRequestFact:
    """One syncable run's cached artifact, with its journaled tree state."""
    item_id = rec_item_id(record)
    merge_state, read_at = journaled.by_item.get(item_id, _NO_MERGE_STATE)
    return cached_pull_request(
        record,
        merge_state=merge_state,
        merge_state_at=read_at,
        # Only an item this read never answered for needs the confession; one
        # whose state IS in hand was read, whatever happened to the rest.
        merge_state_unread=None if merge_state is not None else journaled.unread,
        head_sha=journaled.heads.get(item_id),
        now=now,
    )


def _journaled_merge_states(
    store: Any, scope: list[str] | None
) -> _JournaledMergeStates:
    """``item_id -> (mergeStateStatus, when it was read)`` (#2303).

    The one fact the composer needed and serve never had: ``mergeability``. It
    is read every tick (``graph/driver/facts.py``), journaled on the snapshot
    node, and read back here with every filter SQL-side — so ``merge_conflict``
    fires on a conflicted tree in serve, not only in a test that built the facts
    by hand.

    The read is PAGED to exhaustion, not one page deep. ``latest_node_pr_facts``
    is a bounded, cursored read (its default page is 200 rows), so a single call
    left every artifact past that page with no tree state — which on the path
    that raises the BLOCKING ``merge_conflict`` reads exactly like "nothing
    conflicts" (reviewer finding, #2303). The walk follows the cursor to the
    end, bounded by :data:`_MERGE_STATE_MAX_PAGES`, and *scope* — the
    projection's own item set, when it has one — both filters the rows and lets
    the walk stop as soon as every scoped item has answered.

    The reading instant travels WITH the state, and it is the snapshot's
    ``merge_state_at`` rather than its tick: the driver carries a merge state
    forward over a tick GitHub answered ``UNKNOWN`` on, so the tick would read
    the state as fresher than it is. ``cached_pull_request`` refuses a state it
    cannot age (and one too old to act on), which is what keeps a blocking
    ``merge_conflict`` off an abandoned journal.

    A store too old to answer, a read that refuses, and a walk that hit the page
    cap all leave the states they did not reach ``unknown`` — the reading serve
    already had — and NAME why in ``unread``. An empty map is never a claim that
    nothing conflicts.
    """
    reader = getattr(store, "latest_node_pr_facts", None)
    if not callable(reader):
        return _JournaledMergeStates(unread=_NO_JOURNAL_READER)
    wanted = None if scope is None else set(scope)
    states: _MergeStates = {}
    heads: dict[str, str] = {}
    after: tuple[str, str] | None = None
    for _page in range(_MERGE_STATE_MAX_PAGES):
        try:
            rows = list(reader(limit=_MERGE_STATE_PAGE, after=after))
        except Exception as exc:  # noqa: BLE001 - a refused read is NAMED
            _logger.warning("could not read journaled merge states", exc_info=True)
            unread = f"{_REFUSED_JOURNAL_READ}{exc}"
            return _JournaledMergeStates(states, unread, heads)
        states.update(_merge_states_of(rows, wanted))
        heads.update(_heads_of(rows, wanted))
        if len(rows) < _MERGE_STATE_PAGE or _every_item_answered(wanted, states):
            return _JournaledMergeStates(states, heads=heads)
        after = (str(rows[-1].epic_ref), str(rows[-1].item_id))
    return _JournaledMergeStates(states, _TRUNCATED_JOURNAL_READ, heads)


def _merge_states_of(rows: Iterable[Any], wanted: set[str] | None) -> _MergeStates:
    """One page's rows as merge states, narrowed to the projection's items."""
    out: _MergeStates = {}
    for row in rows:
        item = str(row.item_id)
        if not getattr(row, "merge_state", None):
            continue
        if wanted is not None and item not in wanted:
            continue
        out[item] = (
            str(row.merge_state),
            _opt_text(getattr(row, "merge_state_at", None)),
        )
    return out


def _heads_of(rows: Iterable[Any], wanted: set[str] | None) -> dict[str, str]:
    """One page's rows as journaled heads, narrowed to the projection's items."""
    return {
        str(row.item_id): str(row.head_oid)
        for row in rows
        if getattr(row, "head_oid", None)
        and (wanted is None or str(row.item_id) in wanted)
    }


def _every_item_answered(wanted: set[str] | None, states: _MergeStates) -> bool:
    """Whether a scoped walk can stop: every item it was asked about answered.

    Only ever true for a SCOPED read. An unscoped one has no set to satisfy, so
    it walks to the end of the journal (or to the page cap, which it confesses).
    """
    return wanted is not None and wanted <= states.keys()


def _opt_text(value: Any) -> str | None:
    """A non-empty string, or None — the shape an absent snapshot key takes."""
    text = str(value or "").strip()
    return text or None


def work_reason(view: WorkViewDTO, item_id: str) -> str:
    """One item's judgment-queue line: the shipped wording for the ordinary
    case, the exception's own detail for everything else."""
    # ids.item_ref: ``#N`` for a GitHub number, bare for a Linear id — never
    # the ``#0`` a 0-sentinel issue_num would render.
    ref = item_ref(item_id)
    if view.exception.state == "awaiting_human_decision":
        return f"PR open for {ref} — awaiting review"
    detail = view.exception.detail or view.safest_action.reason
    return f"{ref} — {view.exception.state.replace('_', ' ')}: {detail}"


#: An approval hold's reason, before its basis (:func:`approval_reason`).
_APPROVAL_LEAD = "awaiting approval"
#: What :func:`approval_reason` puts between the lead and the basis.
_APPROVAL_SEP = " — "


def approval_reason(hold: Any) -> str:
    """One approval hold's reason line: the lead, then the estimate's basis.

    The reason, not the sentence: :func:`wait_sentence` says the hold as an
    action from the basis this carries (design review c9, D12).
    """
    basis = getattr(hold, "basis", "")
    return f"{_APPROVAL_LEAD}{_APPROVAL_SEP}{basis}" if basis else _APPROVAL_LEAD


def _approval_sentence(reason: str, estimate_usd: float | None) -> str:
    """An approval hold as the action it waits on, from the reason above.

    Only the exact lead :func:`approval_reason` writes comes off, with the
    separator after it; any other reason is the basis as written.
    """
    text = str(reason or "").strip()
    if text == _APPROVAL_LEAD:
        basis = ""
    elif text.startswith(_APPROVAL_LEAD + _APPROVAL_SEP):
        basis = text[len(_APPROVAL_LEAD + _APPROVAL_SEP) :]
    else:
        basis = text
    return approval_sentence(basis, estimate_usd)


# --------------------------------------------------------------------------- #
# One sentence per wait (#2539)
# --------------------------------------------------------------------------- #

#: The kinds whose move is a merge on the forge: no kew command clears them,
#: so the move is a link to the pull request.
MERGE_KINDS = frozenset({"merge_decision", "merge_ready"})

#: The ``#2055 — `` the set writes onto an ``epic_wait`` reason. Every surface
#: prints the item's reference in a column of its own, so the sentence omits it.
_LEADING_REF = re.compile(r"^\S+ — ")

#: A driver's machine token at the head of a reason (``review_rounds_exhausted:``,
#: ``merge_gate_touched@a1b2c3``): it needs an underscore or a hyphen, which is
#: what tells it apart from the first word of a sentence somebody wrote.
_MACHINE_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+(?:@[^\s:]*)?\s*:?\s*")

#: A reason's ``— raise <key>`` clause: an action, kept only when no command on
#: the row already says the move (a cap to raise is a config edit, not a command).
_RAISE_CLAUSE = re.compile(r"\s*[—-]\s*raise\s.*$")
_DETAIL_EDGE = " :—-"

#: A pull request's URL and its number. Stricter than :data:`_PR_NUMBER_IN_URL`
#: on purpose: an epic wait's ``url`` falls back to the node's ISSUE when it has
#: no pull request, and ``…/issues/2055`` read as "PR #2055 is open" would name
#: a pull request that does not exist.
_PR_URL = re.compile(r"/(?:pull|pulls|pr|merge_requests)/(\d+)/?$")


def _merge_pr(kind: str, url: Any) -> str:
    """The PR number a merge wait is made on, or "" for any other wait."""
    if kind not in MERGE_KINDS:
        return ""
    found = _PR_URL.search(str(url or "").strip())
    return found.group(1) if found else ""


def _is_bare_key(body: str) -> bool:
    """Whether *body* is its bucket's whole machine key and nothing else.

    Such a reason adds nothing to the bucket's label, and echoing it after the
    label prints a raw code as prose (``failed-terminal (exit 137,
    host_restart_repeated)``) — the code stays on the row's ``reason``.
    """
    bucket = bucket_for(body)
    return bucket is not None and body == bucket.prefix


def _merge_sentence(number: str, failure: str) -> str:
    """A merge's sentence: the decision, led by its head's failure when it has one."""
    if failure:
        return f"{failure} — the merge decision is yours"
    return f"PR #{number} is open — the merge decision is yours"


def wait_sentence(
    kind: str,
    reason: str,
    command: str = "",
    url: Any = None,
    failure: str = "",
    estimate_usd: float | None = None,
) -> str:
    """One wait in one sentence — the words every console surface prints.

    The epic popover, the rail, Work and Monitor used to compose this each for
    themselves, and one merge decision read four ways (#2539). So it is written
    here, from the fields a needs-you row and a node's attention share, and both
    DTOs carry the result.

    A merge with a pull request to open says so and nothing else: the reason a
    driver or a projection wrote for it ("PR open for #2055 — awaiting review",
    "in_review awaiting merge") is a state, not a move. Every other wait is the
    bucket's operator label (``kew.waits``) and what the reason adds beyond it
    — a count, a path, the layer that refused — or, when no bucket claims the
    reason, the reason as written.

    *failure* is the merge verify's failure at the PR's current head
    (:func:`merge_verify_failures`). A merge over one leads with it (#3460):
    the decision stays the person's, but the first thing they read is that kew
    has just seen this head fail.

    An approval hold is said as the action it waits on — "approve a run
    estimated at $15.00, the per-issue cap; no cost history to estimate
    from" — from the estimate's basis
    (:mod:`kew.approval_basis`, beside the formats it is written in), and
    *estimate_usd* where the basis is not one of them (design review c9, D12).
    """
    if kind == "approval":
        return _approval_sentence(reason, estimate_usd)
    number = _merge_pr(kind, url)
    if number:
        return _merge_sentence(number, failure)
    body = _LEADING_REF.sub("", str(reason or "").strip(), count=1)
    label = wait_label(body)
    if not label:
        return body
    if _is_bare_key(body):
        return label
    if body.startswith(label):
        detail = body[len(label) :]
    else:
        detail = _MACHINE_TOKEN.sub("", body, count=1)
    detail = detail.strip(_DETAIL_EDGE)
    if detail.startswith(label):
        # A bucket whose prefix spells its label out after a machine token
        # (#2964's repeated-restart continuation) says the label once.
        detail = detail[len(label) :].strip(_DETAIL_EDGE + ";")
    if command:
        detail = _RAISE_CLAUSE.sub("", detail).strip()
    return f"{label}: {detail}" if detail else label


def wait_link(kind: str, url: Any) -> WaitLinkDTO | None:
    """A merge's move, as the link to the pull request it is made on."""
    number = _merge_pr(kind, url)
    if not number:
        return None
    return WaitLinkDTO(label=f"Open PR #{number} to merge", href=str(url).strip())


#: Where the Work screen draws an item's MERGE VERIFY section.
MERGE_VERIFY_ANCHOR = "merge-verify"


def verify_link(kind: str, url: Any, item_id: str, failure: str) -> WaitLinkDTO | None:
    """A merge over a failed verify's first move: read the failure (#3460).

    The item's MERGE VERIFY section on Work, which names the failing tests.
    ``None`` for any wait that is not a merge over a failure at its head.
    """
    if not failure or not item_id or not _merge_pr(kind, url):
        return None
    return WaitLinkDTO(
        label="See the failed merge verify",
        href=f"/work?item={quote(item_id, safe='')}#{MERGE_VERIFY_ANCHOR}",
    )


#: The unreadable-verify reasons already warned about, so a store that cannot
#: answer warns once rather than on every poll that composes the set.
_unreadable_warned: set[tuple[str, str]] = set()
_UNREADABLE_WARNED_MAX = 256


def _warn_unreadable(subject: str, exc: VerifyUnreadable) -> None:
    """Say why *subject*'s merge keeps today's sentence: its verify was not read."""
    key = (subject, str(exc))
    if key in _unreadable_warned:
        return
    if len(_unreadable_warned) >= _UNREADABLE_WARNED_MAX:
        _unreadable_warned.clear()
    _unreadable_warned.add(key)
    _logger.warning(
        "merge verify unreadable for %s, so its merge keeps today's sentence: %s",
        subject,
        exc,
    )


def merge_verify_failures(
    store: Any, items: Mapping[str, str | None]
) -> dict[str, str]:
    """Each of *items* whose merge verify failed at its PR's current head.

    *items* maps an item id to the epic it was read under (``None`` for none).
    The value is :func:`failed_verify_sentence`'s. Fail-soft: an item whose
    verify cannot be read is left out, so its merge keeps today's sentence —
    "could not read the verify" is not a failure to lead with.
    """
    if not items:
        return {}
    try:
        rows = read_verify_rows(store)
    except VerifyUnreadable as exc:
        _warn_unreadable("the needs-you set", exc)
        return {}
    failures: dict[str, str] = {}
    for item_id, epic_ref in items.items():
        try:
            verify = item_merge_verify(store, item_id, epic_ref=epic_ref, rows=rows)
        except VerifyUnreadable as exc:
            _warn_unreadable(item_id, exc)
            continue
        failure = failed_verify_sentence(verify)
        if failure:
            failures[item_id] = failure
    return failures


def merge_items(
    waits: Iterable[tuple[str, NodeAttentionDTO | DashboardRowDTO, str | None]],
) -> dict[str, str | None]:
    """The item and epic of every merge wait among *waits* (id, dto, epic)."""
    return {
        item_id: epic_ref
        for item_id, dto, epic_ref in waits
        if item_id and _merge_pr(_kind_of(dto), dto.url)
    }


def _kind_of(dto: NodeAttentionDTO | DashboardRowDTO) -> str:
    return dto.kind if isinstance(dto, NodeAttentionDTO) else dto.attention_kind


def say_wait(
    dto: NodeAttentionDTO | DashboardRowDTO,
    kind: str,
    *,
    item_id: str = "",
    failure: str = "",
) -> NodeAttentionDTO | DashboardRowDTO:
    """Write *dto*'s ``sentence`` and links — the only writer of any of them.

    *failure* is the merge verify's failure at the PR's head, from
    :func:`merge_verify_failures`, for the item *item_id* names.
    """
    dto.sentence = wait_sentence(
        kind,
        dto.reason,
        dto.command,
        dto.url,
        failure,
        getattr(dto, "estimate_usd", None),
    )
    dto.link = wait_link(kind, dto.url)
    dto.verify_link = verify_link(kind, dto.url, item_id, failure)
    return dto


def compose_node_attention(
    store: Any,
    config: DispatchConfig | None,
    node_ids: Iterable[str],
    *,
    nodes: Iterable[Mapping[str, Any]] | None = None,
    epic_ref: str | None = None,
    signal: Any = None,
    now: datetime | None = None,
) -> AttentionJoin:
    """Join the needs-you set onto *node_ids* (each a durable item id).

    ``config`` is the dispatch config the projection needs; ``None`` means the
    caller is not asking for the join, which is reported as unavailable rather
    than as an epic where nobody is waiting.

    ``nodes`` is the journaled snapshot's own node dicts — the ones *node_ids*
    was read off. They carry the epic's waits (a node awaiting a merge, a
    continuation, a publication, a no-change decision), which no work
    projection can see; leaving them out is what let an epic legend read
    ``needs you 0`` over six waiting nodes (#2207). Passing none composes the
    projection and the holds alone, which is a narrower reading, not a wrong
    one — every wait it misses is a wait the snapshot was never handed.

    ``epic_ref`` is the epic *node_ids* was read off, and it scopes the
    epic-hold read to that epic. Passing none falls back to the cross-epic
    read, which costs every hold on the host per poll — see
    :func:`_epic_hold_attention`.

    ``signal`` is that snapshot's driver signal, and with *epic_ref* it decides
    whether the epic is RETIRED — in which case the join is EMPTY. The gate
    lives here rather than in each caller so every surface composed through
    this function is gated identically: a detail read and the live stream that
    overwrites it may not disagree about one epic (#2207). See
    :func:`_retirement`.

    A node this snapshot only holds a boundary copy of leaves the join too —
    see :func:`boundary_ids`. Both gates exist to keep ONE invariant: a node
    carries attention here exactly when the needs-you set files a row for this
    epic about it, so an epic page's legend ("N needs you") and the diamonds on
    the DAG underneath it can never print two different answers.
    """
    ids = {str(n) for n in node_ids if n}
    if not ids:
        return AttentionJoin()
    if config is None:
        return AttentionJoin({}, [UNREADABLE_PROJECTION])
    retired, retired_unreadable = _retirement(store, epic_ref, signal)
    if retired:
        return AttentionJoin({}, retired_unreadable)
    ids -= boundary_ids(nodes)
    if not ids:
        return AttentionJoin()
    # Read ONCE: the live holds answer both "which node is held" and "which
    # journaled hold an operator has since cleared" (:func:`stale_hold_wait`).
    holds, epic_unreadable = _read_epic_holds(store, epic_ref)
    waits = _wait_attention_by_node(
        nodes,
        ids,
        held_items(holds),
        epic_ref,
        merged_pr_waits(store, nodes),
        signal=signal,
    )
    projected, unreadable = _projection_attention(store, config, ids, now)
    # A hold on a node this snapshot reads DONE is not a decision anybody can
    # make: nothing clears a hold once its node stops being dispatched, so the
    # row outlives the work. The needs-you set files no entry for it either
    # (``needs_you._hold_is_settled``), and the invariant this join exists to
    # keep is that the legend and the diamonds count the same things (#2392
    # review).
    epic_holds = _epic_hold_attention(holds, ids - done_node_ids(nodes))
    approvals, holds_unreadable = _approval_attention(store, ids)
    # Later sources overwrite earlier ones, in the console's own consequence
    # order: money (an approval, then an epic hold) outranks review, which
    # outranks a bare wait. So one item is ranked the same way on every
    # surface — the DAG diamond, Monitor's row and the header's count.
    joined = {**waits, **projected, **epic_holds, **approvals}
    joined, unread_runs = _without_running(joined, store, config, now)
    unreadable = [*unreadable, *unread_runs]
    failures = merge_verify_failures(
        store, merge_items((node, dto, epic_ref) for node, dto in joined.items())
    )
    for node, attention in joined.items():
        say_wait(
            attention, attention.kind, item_id=node, failure=failures.get(node, "")
        )
    return AttentionJoin(joined, [*unreadable, *epic_unreadable, *holds_unreadable])


def _without_running(
    joined: dict[str, NodeAttentionDTO],
    store: Any,
    config: DispatchConfig | None,
    now: datetime | None,
) -> tuple[dict[str, NodeAttentionDTO], list[str]]:
    """*joined* less every node a run is executing on (#2784).

    A running item is "working", never needs-you: the set drops it by the same
    reading (:func:`executing_items`), so no diamond is drawn that the legend
    has not counted. A failed read is a failed read of the work facts, and is
    confessed under the projection's own name rather than sinking the join.
    """
    try:
        running = executing_items(store, config, now or datetime.now(UTC))
    except Exception:  # noqa: BLE001 — fail-soft: confess, never sink the join
        return joined, [UNREADABLE_PROJECTION]
    return {k: v for k, v in joined.items() if k not in running}, []


def executing_items(
    store: Any, config: DispatchConfig | None, now: datetime
) -> frozenset[str]:
    """Every item a run is executing on right now (#2784).

    DESIGN.md: a running item is "working", never needs-you. #2728 wore the
    needs-you diamond beside WORKING, over a sentence about the run BEFORE the
    one executing. The needs-you set and the node join both drop these items,
    so the header, the legend and the diamonds keep counting one set.

    "Executing" is a ``pending`` run row inside the projection's own in-flight
    bound (:func:`kew.service.work.run_executing`) — the reading that prints
    WORKING on Work. Every run kind counts — dispatch, review, remediation,
    the review stage, resolve and CI fix (#2842): #2814 sat in the set as
    "held" while a conflict-resolve run worked on it. The bound is what keeps a
    dead row from hiding a real wait:
    past it the projection reads ``run_abandoned`` and the item is back. A row
    on kew's ``<prefix><n>`` branch counts for item *n* too, as the needs-you
    set keys a pull request's row (:func:`closing_item`).

    The JOURNAL's node status is deliberately not a reading here: a node stays
    ``in_progress`` in the snapshot after its run dies, and an abandoned run or
    an open hold on such a node is exactly what a person has to see.
    """
    reader = getattr(store, "list_pending", None)
    if reader is None:
        # A fake or legacy store with no run rows has nothing executing. A
        # FAILED read is different: it raises, and the caller names it.
        return frozenset()
    bound = pending_run_max_age_s(config)
    prefix = config.branches.prefix if config is not None else ""
    items: set[str] = set()
    for rec in reader():
        if not run_executing(rec, now=now, max_age_s=bound):
            continue
        own = rec_item_id(rec)
        branch = str(getattr(rec, "branch", "") or "")
        items.update((own, closing_item(own, branch, prefix)))
    items.discard("")
    return frozenset(items)


def closing_item(item_id: str, branch: str, prefix: str) -> str:
    """The item a pull request on *branch* closes — *item_id* unless the branch
    is kew's own ``<prefix><n>`` for a different GitHub item *n* (#2576).

    Only a GitHub number is read back off the branch: ``branch_slug``
    lowercases, so any other tracker's id cannot be recovered from it, and a
    guessed one would key the row to an item that does not exist.
    """
    if not prefix or not branch.startswith(prefix):
        return item_id
    tail = branch[len(prefix) :]
    return tail if GITHUB_ID_RE.fullmatch(tail) else item_id


def boundary_ids(nodes: Iterable[Mapping[str, Any]] | None) -> frozenset[str]:
    """The node ids this snapshot only holds a BOUNDARY COPY of.

    ``build_graph`` copies a resolved out-of-epic dependency into every epic
    that depends on it, flagged ``external``, and the epic that OWNS the node is
    the one where a person can act. Every source that starts from the epic
    already honoured that — the journaled waits
    (:func:`_wait_attention_by_node`), the epic holds
    (:func:`_read_epic_holds`), and the needs-you set's own
    ``_epic_by_item``/``_epic_wait_rows``, which file the node under its owner.

    The two sources that start from the ITEM did not, because neither has ever
    heard of an epic: the work projection and the approval holds are keyed by
    item id, so a merge decision on somebody else's node drew a magenta diamond
    on this epic's DAG while this epic's legend — counting the set, where the
    row is filed under the owner — read 0. One page, two answers, which is the
    #2207 disagreement by another route. So the copies leave the join as a
    whole, and the legend and the diamonds agree again.

    ``None`` nodes (a caller that passed no snapshot) names no boundary: an
    unread snapshot cannot prove a node is a copy, and guessing would drop a
    diamond the owner's own page is counting.
    """
    return frozenset(
        str(node.get("id", ""))
        for node in nodes or ()
        if isinstance(node, Mapping) and node.get("external")
    )


def done_node_ids(nodes: Iterable[Mapping[str, Any]] | None) -> frozenset[str]:
    """The node ids this snapshot reads as ``done``.

    Read off the snapshot the caller is annotating, which on every surface that
    answers "what is this epic's state NOW" is the ledger-reconciled one — so a
    node the ledger has settled is named here even when the journal still
    carries a held row for it (``kew.service.epics._reconciled``, #2392).

    ``done`` and not every terminal state: the driver's own hold precedence
    exempts a LANDED node and nothing else (``overlay._hold_stands``), so a
    ``cancelled`` node's hold still stands there and must still stand here.

    ``None`` nodes names nothing settled, for :func:`boundary_ids`' reason: an
    unread snapshot cannot prove a node is finished.
    """
    return frozenset(
        str(node.get("id", ""))
        for node in nodes or ()
        if isinstance(node, Mapping) and str(node.get("status") or "") == DONE
    )


def _retirement(
    store: Any, epic_ref: str | None, signal: Any
) -> tuple[bool, list[str]]:
    """Whether this epic is retired, and what could not be read to say so.

    A retired epic (#2206) and a ``complete`` one are out of the active views,
    so ``_epic_roster`` in :mod:`kew.service.needs_you` keeps their journaled
    node waits AND their open epic holds out of the needs-you set — a drain
    nobody intends to resume is not a thing a person has to act on, and
    `kew epic archive` promises the whole epic "is excluded from the console's
    needs-you count". The node join has to drop exactly the same entries, or an
    epic's legend counts diamonds no surface reading ``rows`` counts: the #2207
    disagreement, one epic over instead of one node. ``epic_is_retired`` is the
    one predicate both sides read.

    The gate is therefore the WHOLE join, not the epic's own two sources. It
    began as the journaled waits alone; the open hold was exempt on the grounds
    that it is a live read, which left four `budget_hold` rows on two abandoned
    drains counting against the header with no diamond behind them (#2243). An
    approval and the work projection were exempt on a second such ground — that
    neither reaches ``rows`` carrying an ``epic_ref``, so the header counts them
    either way — and that left the mirror image: a retired epic's legend read a
    confident ``0 needs you`` over a DAG still drawing the magenta diamond,
    because the count comes from the set (where no entry can be filed under an
    epic the roster calls retired) and the diamond came from here. One page, two
    answers, which is the whole of what #2207 was.

    So nothing survives retirement. An item-level wait on one of this epic's
    nodes is still a real wait and the header still counts it — on the Work
    screen and in Monitor, where it carries no epic at all, which is what the
    archive decision means: the EPIC stops being a place a person is asked to
    look, not the item.

    The signal is read first because it costs nothing: a ``complete`` epic is
    retired whatever the ledger says, so the archive read is skipped for it
    entirely. Otherwise the decision is two event-scoped audit queries, and
    there is nothing cheaper to ask — an archive decision is an audit fact and
    nothing else (#2206), so the epic carries no column to read it off. With no
    *epic_ref* there is no epic to look up and the signal alone answers.

    An UNREADABLE ledger degrades to retired and is NAMED, never swallowed.
    Guessing "nothing is archived" would silently resurrect a retired epic's
    diamonds (the quiet default :func:`kew.epic_archive.archive_states` refuses
    to take), and guessing it uncaught 500s a read route. Dropping the waits
    matches what the needs-you set does when its own archive read fails — that
    source contributes nothing and names itself — and the named entry is what
    keeps the empty answer readable as "we could not tell".
    """
    terminal = epic_is_retired(signal, archived=False)
    if terminal or epic_ref is None:
        return terminal, []
    try:
        archived = is_archived(store, epic_ref)
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean None
        return True, [UNREADABLE_ARCHIVE]
    return epic_is_retired(signal, archived=archived), []


def _projection_attention(
    store: Any, config: DispatchConfig, ids: set[str], now: datetime | None
) -> tuple[dict[str, NodeAttentionDTO], list[str]]:
    try:
        projection = work_projection(store, config, item_ids=ids, now=now)
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean None
        return {}, [UNREADABLE_PROJECTION]
    by_node = {
        view.identity.item_id or "": _exception_attention(view)
        for view in projection.views
        if (view.identity.item_id or "") in ids and needs_a_person(view)
    }
    return by_node, [
        unreadable_item(item_id)
        for item_id in sorted(projection.unreadable)
        if item_id in ids
    ]


def exception_kind(view: WorkViewDTO) -> str:
    """The attention kind one projection exception is, in the join's words.

    Shared with the needs-you set's own rows (#2244): the DAG's diamond and the
    judgment row for one item must not file the same exception under two kinds,
    which a second copy of this conditional is how they would.
    """
    if view.exception.state == "awaiting_human_decision":
        return "merge_decision"
    return "work_exception"


def _exception_attention(view: WorkViewDTO) -> NodeAttentionDTO:
    item_id = view.identity.item_id or ""
    return NodeAttentionDTO(
        kind=exception_kind(view),
        reason=work_reason(view, item_id),
        url=view.artifact.pr_url,
    )


def _approval_attention(
    store: Any, ids: set[str]
) -> tuple[dict[str, NodeAttentionDTO], list[str]]:
    # Holds are keyed by issue number, so only an id that IS one can ever match
    # a hold below — the same filter, moved into the read. This join is composed
    # on every 1s epic poll, and unscoped it asked the audit log for every
    # approval event the host had ever written (#2129). No such id means a
    # known-empty answer, which is not worth a read at all. The digit bound is
    # the int64 bind limit (:data:`MAX_ISSUE_NUM_DIGITS`): a longer node id is
    # no issue number either way, so it drops out with the non-numeric ids.
    issue_nums = [
        int(node_id)
        for node_id in ids
        if node_id.isdecimal() and len(node_id) <= MAX_ISSUE_NUM_DIGITS
    ]
    if not issue_nums:
        return {}, []
    try:
        holds = open_approval_holds(store, issue_nums=issue_nums)
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean None
        return {}, [UNREADABLE_HOLDS]
    return {
        str(hold.issue_num): NodeAttentionDTO(
            kind="approval", reason=approval_reason(hold)
        )
        for hold in holds
        if str(hold.issue_num) in ids
    }, []


# --------------------------------------------------------------------------- #
# The epic's own waits (#2207)
# --------------------------------------------------------------------------- #
#
# A node can need a person without the work projection having anything to say
# about it. The projection is composed over runs the store can sync a PULL
# REQUEST for, so a node the driver is holding, or one it parked awaiting a
# continuation, or one whose tracker says in_review while no syncable run row
# exists, joined as "nobody is waiting" — which is how an epic legend came to
# read `needs you 0` over six nodes that were each waiting on a human (#2207).
#
# The classification is NOT made here: `kew.waits` owns the one table mapping a
# waiting reason (or, where the driver wrote none down, the node's own status
# word) to a kind, a label and an action, and the driver's live attention stream
# reads the same table. Two copies of that table is the bug this module's own
# docstring opens by warning about, one layer down.

# Statuses that are a wait on a PERSON when the driver recorded no reason for
# them. The status word doubles as the lookup key because `STALL_BUCKETS` is
# prefixed on exactly these words (`in_review`, `awaiting_continuation`,
# `awaiting_publication`, `awaiting_no_change`) — the driver builds its stall
# reasons out of the same vocabulary, so one table answers both.
_WAIT_STATUSES = frozenset(
    {
        "in_review",
        "awaiting_continuation",
        "awaiting_publication",
        "awaiting_no_change",
        "held",
    }
)
# The one status that is held by definition, so an unbucketed reason on it still
# earns the generic "clear the hold" attention rather than going silent.
_HELD_STATUS = "held"


def node_wait(
    status: str, stuck: Any, *, signal: Any = None
) -> WaitClassification | None:
    """What this journaled node state asks of a person, or None for nobody.

    ``stuck`` is the driver's recorded reason (snapshot ``nodes[].stuck``) and
    is preferred when present — it names the PR, the cap or the runner. With
    nothing recorded, the node's own status word is the probe, which is why
    ``_WAIT_STATUSES`` is spelled in the vocabulary ``STALL_BUCKETS`` is
    prefixed on. A status nobody waits on (done, blocked, failed, cancelled, or
    work still in flight) classifies to None through the same table.

    *signal* is the snapshot's driver signal: an unarmed node is a person's
    move only on a STALLED epic (``kew.waits.classify_epic_wait``).
    """
    state = str(status or "")
    reason = str(stuck or "")
    if not reason and state not in _WAIT_STATUSES:
        return None
    held = state == _HELD_STATUS or holds_node_out_of_frontier(reason)
    return classify_epic_wait(reason or state, held=held, signal=signal)


#: A command quoted in a stored reason ("...; resume with `kew epic resume
#: go-live-2026-09 SFD-232`"), with the clause that introduces it AND every
#: alternative chained onto it ("...; use `kew epic continue` or `kew epic
#: abandon`"). Commands are computed (``kew.waits.fill_command``), never read
#: out of text: a journal written before #2244 still quotes a verb this CLI
#: never had, and the row printed it beside the right one.
#:
#: The clause goes with the command because half a clause is not a sentence.
#: Lifting the backticked span alone left every reason the driver writes TODAY
#: mangled — "preserved dirty work; use or", "reconcile, or use", "fix the
#: cause, then un-" — so the pattern reaches back over the words that
#: introduce the command (``use``, ``resume with``, ``then un-hold with``) and
#: forward over ``or``/``and`` alternatives. The reach back is bounded by the
#: clause separator as much as by the word count: a run of ``[\w-]+\s+`` stops
#: at the ``;``, the ``,`` or the ``—`` the reason is punctuated with, so what
#: it eats stays inside the clause that names the command.
_QUOTED_COMMAND = re.compile(
    r"[;,]?\s*(?:\b(?:or|and)\s+)?(?:\b[\w-]+\s+){0,3}?`kew\b[^`]*`"
)

#: What may be left dangling at either end once a command clause is lifted out:
#: the separator that introduced it and the dash the driver writes before a
#: remedy ("sandbox_containment: ... — un-hold with `kew epic unhold 232`").
_CLAUSE_EDGE = " ;,:—–-"


def wait_reason(
    wait: WaitClassification, status: str, stuck: Any, command: str = ""
) -> str:
    """One wait's line: the driver's own reason when it recorded one, else the
    bucket's operator label, else the bare status word — never an invention.

    *command* is the row's OWN command, already filled
    (``kew.waits.fill_command``), and a ``kew`` command quoted in the driver's
    text is dropped only when it is there to replace it (see
    ``_QUOTED_COMMAND``). A wait no kew command clears — a host publication,
    whose stored reason names ``kew finalize <run_id>``, or any row whose epic
    and node did not fill the template — keeps the driver's quoted handle,
    because dropping it would leave the row with no actionable handle at all.
    """
    reason = str(stuck or "")
    if command:
        reason = _QUOTED_COMMAND.sub("", reason).strip(_CLAUSE_EDGE)
    if reason:
        return safe_hold_reason(reason)
    return wait.label or str(status or "")


def stale_hold_wait(stuck: Any, node_id: str, held: frozenset[str] | None) -> bool:
    """Whether this journaled wait quotes a hold the live table no longer has.

    A journaled hold is a claim about an ``epic_holds`` ROW, and clearing that
    row (``kew epic unhold``, or the driver's own release) leaves no mark on the
    snapshot the driver wrote before it. So the wait outlives the hold — with
    the cleared reason and its place in every surface's count still on it —
    until the next tick rewrites the snapshot, which is never on an epic nobody
    restarted. For exactly those waits the live table is the authority, and this
    is where the two readings meet: the needs-you set's ``epic_wait`` entries
    and this module's node join both ask it, so neither can report a hold the
    other has let go (#2207).

    Only HOLD-backed waits are checked. A node parked awaiting a merge, a
    continuation, a publication or a no-change decision has no row to check
    against, and a ``held`` status with no recorded reason is the
    merged-but-still-open overlay (:func:`kew.graph.driver.overlay.settled_status`),
    which is no hold either — suppressing those would answer a stale entry with
    a missing one.

    ``held`` of ``None`` is an UNREADABLE hold table, not an empty one, and
    suppresses nothing: a read that did not happen cannot prove a hold was
    cleared.
    """
    if held is None:
        return False
    return holds_node_out_of_frontier(str(stuck or "")) and node_id not in held


#: The waits whose subject is the node's open PULL REQUEST: a merge decision
#: (``in_review`` and its aged-out form) and a PR whose checks stopped moving.
#: Once that pull request has merged, none of them is anybody's move — see
#: :func:`merged_pr_waits`.
PR_WAIT_PREFIXES = frozenset({"in_review", "review-stalled", "ci-stalled"})

#: A pull request's number is the last path segment of its URL — the same
#: loose read ``kew.service.epics`` makes, for the same reason: a URL shape
#: this cannot read answers "no number", which suppresses nothing.
_PR_NUMBER_IN_URL = re.compile(r"/(\d+)/?$")


def _pr_number(pr_url: Any) -> str:
    found = _PR_NUMBER_IN_URL.search(str(pr_url or "").strip())
    return found.group(1) if found else ""


def _pr_wait(node: Any) -> tuple[str, str] | None:
    """``(node id, PR number)`` when this journaled node waits on its own PR."""
    if not isinstance(node, Mapping) or node.get("external"):
        return None
    node_id = str(node.get("id", ""))
    number = _pr_number(node.get("pr_url"))
    if not node_id or not number:
        return None
    reason = str(node.get("stuck") or "") or str(node.get("status") or "")
    bucket = bucket_for(reason)
    if bucket is None or bucket.prefix not in PR_WAIT_PREFIXES:
        return None
    return node_id, number


def _runs_by_item(store: Any, item_ids: list[str]) -> dict[str, list[Any]]:
    """Every run of *item_ids* — one query where the store can batch it."""
    batched = getattr(store, "list_by_items", None)
    if callable(batched):
        return dict(batched(item_ids))
    return {item_id: list(store.list_by_item(item_id)) for item_id in item_ids}


def merged_pr_waits(
    store: Any, nodes: Iterable[Mapping[str, Any]] | None
) -> frozenset[tuple[str, str]]:
    """The journaled PR waits whose pull request the store records MERGED.

    The snapshot is a JOURNAL: a node the driver wrote ``in_review — awaiting
    merge (PR #2476)`` keeps saying so until a tick rewrites it, and #2469 read
    ``NEEDS YOU · Awaiting merge`` for twenty minutes after its PR merged —
    beside a pipeline column that already said ``merged`` (#2538). The run row
    is where that fact already lives (``runs.pr_state``, written by the
    outcome sync), so this reads it rather than asking the forge on every poll.

    Keyed on ``(node id, PR number)`` and matched against the PR the JOURNAL
    names, not any PR the item ever had: a node reopened onto a second pull
    request is waiting on the new one, and an older merge says nothing about
    it. A node naming no PR, and a wait that is not about one (a hold, a
    continuation, an arm), is never matched.

    A refused run read suppresses nothing and says so in the log: the journal's
    own waits then stand, which is the reading every surface served before.
    """
    wanted = {pair for node in nodes or () if (pair := _pr_wait(node))}
    if not wanted:
        return frozenset()
    try:
        runs = _runs_by_item(store, sorted({node_id for node_id, _ in wanted}))
    except Exception:  # noqa: BLE001 — fail soft, logged
        _logger.warning(
            "could not read run PR states; journaled merge waits stand",
            exc_info=True,
        )
        return frozenset()
    return frozenset(
        (node_id, number)
        for node_id, number in wanted
        if number in _merged_numbers(runs.get(node_id, ()))
    )


def _merged_numbers(runs: Iterable[Any]) -> set[str]:
    """The numbers of the PRs *runs* record merged, by the one rule every
    report reads (:func:`kew.pr_states.pr_states_by_url`)."""
    return {
        _pr_number(url)
        for url, state in pr_states_by_url(runs).items()
        if state == MERGED
    }


def pr_wait_merged(node: Any, merged: frozenset[tuple[str, str]]) -> bool:
    """Whether this node's journaled wait is on a PR :func:`merged_pr_waits`
    found merged — the one test both the set and the node join apply."""
    pair = _pr_wait(node)
    return pair is not None and pair in merged


def hold_item_id(hold: Any) -> str:
    """The node id one epic hold is keyed on (durable id, legacy number)."""
    return str(getattr(hold, "item_id", "") or getattr(hold, "issue_num", ""))


def held_items(holds: list[Any] | None) -> frozenset[str] | None:
    """The node ids *holds* currently holds — ``None`` for an unreadable read."""
    if holds is None:
        return None
    return frozenset(hold_item_id(hold) for hold in holds)


def wait_attention(
    status: str,
    stuck: Any,
    url: str | None,
    *,
    epic_ref: str = "",
    node_id: str = "",
    signal: Any = None,
) -> NodeAttentionDTO | None:
    """One node's wait as attention, or None when nobody is waited on.

    *epic_ref* and *node_id* are what turn the bucket's command TEMPLATE into a
    line an operator can paste; without both, ``fill_command`` serves "" rather
    than a half-filled one. This is the same fill the needs-you row for the
    same item makes (``kew.service.needs_you._epic_wait_row``), off the same
    bucket's template, so the DAG popover and the judgment row cannot print
    two different commands for one wait.
    """
    wait = node_wait(status, stuck, signal=signal)
    if wait is None:
        return None
    command = fill_command(wait.command, epic=epic_ref, item=node_id)
    return NodeAttentionDTO(
        kind=wait.kind.value,
        reason=wait_reason(wait, status, stuck, command),
        url=url,
        action=wait.action,
        command=command,
    )


def epic_hold_reason(hold: Any) -> str:
    """One epic hold's line, shared by the node column and the needs-you set."""
    return safe_hold_reason(str(getattr(hold, "reason", "") or "")) or "held"


def epic_hold_attention(hold: Any) -> NodeAttentionDTO | None:
    """One open epic hold as the held node's attention, or None for nobody.

    The kind comes from the same table as everything else: a budget hold reads
    as ``budget_exhausted`` and a containment refusal as ``sandbox_refused``,
    so Monitor's row, the DAG's diamond and the driver's own event stream call
    one hold one thing.

    Including when the table says NOBODY is waiting. ``classify_wait`` returns
    None for the CI gate — an operator cannot make a check finish — and this
    used to answer that None with the generic HELD fallback, which put a
    needs-you diamond and a ``kew epic unhold`` on a node the driver's own
    attention stream is silent about. Worse, the epic holds are merged after the
    projection, so the invented hold also displaced the real merge decision on
    the same node. The fallback belongs to an UNBUCKETED hold reason, which is
    ``classify_wait``'s own job and already inside it.
    """
    wait = classify_wait(str(getattr(hold, "reason", "") or ""), held=True)
    if wait is None:
        return None
    return NodeAttentionDTO(
        kind=wait.kind.value,
        reason=epic_hold_reason(hold),
        action=wait.action,
        # The hold row carries both halves of its own key, so the command this
        # node's popover prints is filled from the row that is holding it.
        command=fill_command(
            wait.command,
            epic=str(getattr(hold, "epic_ref", "") or ""),
            item=hold_item_id(hold),
        ),
    )


def _wait_attention_by_node(
    nodes: Iterable[Mapping[str, Any]] | None,
    ids: set[str],
    held: frozenset[str] | None,
    epic_ref: str | None,
    merged: frozenset[tuple[str, str]] = frozenset(),
    *,
    signal: Any = None,
) -> dict[str, NodeAttentionDTO]:
    """The snapshot's own waits, keyed by node id.

    An ``external`` node is skipped: ``build_graph`` records a resolved
    out-of-epic dependency inside every epic that depends on it, and the epic
    that OWNS the node is where a person can act, so the boundary copy must not
    raise a second claim on somebody else's work. Kept here as well as in
    :func:`boundary_ids`, which now takes the copies out of *ids* before any
    source sees them: this reader is the one that is handed the raw node dicts,
    so the rule reads off the same field it has always read off.

    *held* is the live hold table's node ids, against which a journaled hold the
    operator has already cleared drops out — see :func:`stale_hold_wait`.

    *epic_ref* is one half of the command each wait names; ``None`` (a
    cross-epic caller that named no epic) fills nothing, which serves no
    command rather than one missing its first argument.

    *merged* drops a wait on a pull request that has since merged — see
    :func:`merged_pr_waits`, which the needs-you set applies too. *signal* is
    the snapshot's driver signal, which decides whether an arm wait is
    anybody's (``kew.waits.classify_epic_wait``).
    """
    by_node: dict[str, NodeAttentionDTO] = {}
    for node in nodes or ():
        node_id = _journaled_wait_id(node, ids, held, merged)
        if node_id is None:
            continue
        attention = wait_attention(
            str(node.get("status") or ""),
            node.get("stuck"),
            node.get("pr_url"),
            epic_ref=epic_ref or "",
            node_id=node_id,
            signal=signal,
        )
        if attention is not None:
            by_node[node_id] = attention
    return by_node


def _journaled_wait_id(
    node: Any,
    ids: set[str],
    held: frozenset[str] | None,
    merged: frozenset[tuple[str, str]],
) -> str | None:
    """This node's id, when its journaled wait is still one to read — else None:
    a boundary copy, a node outside the join, a hold the live table cleared, or
    a wait on a pull request that has since merged."""
    if not isinstance(node, Mapping) or node.get("external"):
        return None
    node_id = str(node.get("id", ""))
    if node_id not in ids or stale_hold_wait(node.get("stuck"), node_id, held):
        return None
    return None if pr_wait_merged(node, merged) else node_id


def _read_epic_holds(
    store: Any, epic_ref: str | None
) -> tuple[list[Any] | None, list[str]]:
    """The open epic holds and what to name, ``None`` for a read that failed.

    Scoped to *epic_ref* when the caller named one — the same #2129 narrowing
    the approval-hold read above got, for the same reason: this join is composed
    on every 1s epic poll, and the cross-epic read asked for every hold on the
    host only to discard all but a handful. It is also the stricter reading. A
    hold is keyed by (epic, item), ``build_graph`` copies a resolved out-of-epic
    dependency into every epic that depends on it, and the epic that OWNS the
    node is where a person can act — so another epic's hold must not raise a
    diamond on this epic's boundary copy (the rule ``_wait_attention_by_node``
    keeps for the snapshot's own waits).
    """
    try:
        holds = (
            store.list_all_epic_holds()
            if epic_ref is None
            else store.list_epic_holds(epic_ref)
        )
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean None
        return None, [UNREADABLE_EPIC_HOLDS]
    return list(holds), []


def _epic_hold_attention(
    holds: list[Any] | None, ids: set[str]
) -> dict[str, NodeAttentionDTO]:
    """Every open epic hold whose node is in *ids*, as that node's attention.

    ``None`` holds is the unreadable read its own caller has already named —
    no attention, and no invented silence either.
    """
    by_node: dict[str, NodeAttentionDTO] = {}
    for hold in holds or ():
        node_id = hold_item_id(hold)
        attention = epic_hold_attention(hold) if node_id in ids else None
        if attention is not None:
            by_node[node_id] = attention
    return by_node
