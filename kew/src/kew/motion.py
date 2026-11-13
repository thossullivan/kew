"""One motion vocabulary: what is MOVING, what is waiting, and on whom (#2305).

The console animates. Before this module every surface decided for itself what
"live" meant, and each one decided differently: ``lib/epic/metro.ts`` drew an
edge cyan into an ``in_review`` node — a published pull request that is waiting
for a PERSON, where nothing is moving at all — while missing
``adversarial_review`` and ``remediating``, the two statuses where kew's own
reviewer really is running. The result was motion that pointed away from the
work: a halo on what had stopped, nothing on what had not.

So there is one table, keyed by status WORD, answering one question — is this
thing executing, waiting on kew, waiting on a person, or settled? — and
``frontend/src/lib/factory/motion.ts`` holds the same table for the browser.
``tests/test_motion_parity.py`` fails if the two disagree or if either misses a
word one of the three status vocabularies can serve.

Four words, and the console's whole animation budget rides on the first:

* ``executing`` — kew is running code against this right now. ONLY these move.
* ``waiting_kew`` — stopped, and kew's own turn to act (it will, unasked).
* ``waiting_human`` — stopped, and a person is the next move.
* ``settled`` — terminal. Nothing more happens here.

``alive`` is ``motion != "settled"``, which is the one derived reading the DTOs
also carry: a surface asking "is this an open concern" must not have to know
which of the three non-terminal words applies.

WHY THIS MODULE AND NOT THE ENUM MODULES: the words come from three places —
:class:`kew.graph.driver.overlay.DriverStatus` (node), and
:data:`kew.run_store.RunStatus` / :data:`kew.run_store.LoopStatus` (run, loop) —
and the import-linter contract "epic snapshots cross to serve/service as JSON,
not imports" forbids ``kew.service`` from importing ``kew.graph`` at all. A
table living beside any ONE of the enums is therefore unreachable from the DTO
builders that need all three. This module imports nothing and is importable from
anywhere, which is what lets one table serve every vocabulary.

A node and a run can spell near-neighbours differently on purpose, and the table
reads them differently because they are different things:

* ``failed`` (a node) is work that stopped and needs an operator ``kew epic
  retry`` — ``waiting_human``;
* ``failure`` (a run) is the RECORD of an attempt that is over — ``settled``.

Every terminal run status is ``settled`` for that reason: what the ITEM needs
next is the needs-you join's answer, never a historical run row's.

And one word the two vocabularies spell the SAME and mean opposite things,
``queued``, which is why the readings take a vocabulary — see
:data:`RUN_MOTION_OF`.
"""

from __future__ import annotations

from typing import Literal, get_args

#: What a thing is doing, in the only four words any surface may animate from.
Motion = Literal["executing", "waiting_kew", "waiting_human", "settled"]

#: Every word :data:`Motion` admits, for a caller validating one off the wire.
MOTIONS: frozenset[str] = frozenset(get_args(Motion))

#: The motion of a status word this table has never heard of.
#:
#: ``waiting_kew`` and not ``executing`` (a status kew adds later must not earn
#: a halo by default), and not ``settled`` either (that would claim the work is
#: over, and would report ``alive=False`` about something still in flight). The
#: honest degradation is "stopped, kew's turn": nothing moves, nothing is
#: declared finished. ``motion.ts`` falls back to the same word.
FALLBACK_MOTION: Motion = "waiting_kew"

#: A ``pursuing`` loop quiet for longer than this is not executing.
#:
#: "Quiet" is measured from the newest thing the loop wrote, which the caller
#: supplies — ``service.loops._quiet_secs`` reads the last recorded iteration
#: ahead of ``updated_at``, because ``add_iteration`` does not bump it and a
#: loop on its third iteration must not be drawn still. ONE iteration is a whole
#: agent run — ``AgentConfig.timeout_minutes`` defaults to 120, so a loop can
#: legitimately sit silent for two hours and still be running. The threshold is
#: therefore set past that cap with slack rather than at a cadence: a loop
#: inside one honest iteration keeps its halo, and a ``pursuing`` row nobody is
#: driving any more eventually stops pretending to move. Erring long is
#: deliberate and follows the liveness rule kew applies to runs — prove it
#: stopped, never infer it from the shape of the state.
LOOP_STALE_AFTER_SECS: float = 3 * 60 * 60

#: Status word -> motion, for every word the node, run and loop vocabularies
#: can serve. One table, three vocabularies; `awaiting_publication` is in two of
#: them and means the same thing in both, and `queued` is the one word they
#: spell the same and read differently (:data:`RUN_MOTION_OF` refines it).
MOTION_OF: dict[str, Motion] = {
    # --- node: DriverStatus (kew/graph/driver/overlay.py), all 14 ------------
    # Nothing has been dispatched yet, and kew dispatches it off its own
    # frontier — nobody has to do anything for this to start.
    "todo": "waiting_kew",
    # Not armed. A PERSON adds the marker the adapter looks for (#2209), so
    # this is a wait on judgment however quiet it is drawn. The RUN vocabulary
    # spells its own `queued` the same way and means the opposite — see
    # `RUN_MOTION_OF`.
    "queued": "waiting_human",
    # A run is reserved or pending against this node: code is running.
    "in_progress": "executing",
    # The run finished and kew owes it a branch and a pull request.
    "awaiting_publication": "waiting_kew",
    # Preserved dirty work, held until an operator says `kew epic continue`.
    "awaiting_continuation": "waiting_human",
    # A pull request is open and a person must review or merge it. The whole
    # reason this module exists: this is the status the console used to draw
    # as live.
    "in_review": "waiting_human",
    # kew's own fresh-context reviewer is running against the recorded head.
    "adversarial_review": "executing",
    # kew is acting on that reviewer's NEEDS_CHANGES findings.
    "remediating": "executing",
    # A zero-diff exit whose meaning is open: confirm or reject it.
    "awaiting_no_change": "waiting_human",
    "done": "settled",
    # The "blocked" label is a human's mark and only a human clears it — the
    # driver prints the recorded reason ("needs a design decision") beside it.
    # Dependency waits are not this: an unmet node is `todo`.
    "blocked": "waiting_human",
    # Work that stopped. `kew epic retry` is the way out, and nothing happens
    # until somebody runs it.
    "failed": "waiting_human",
    "cancelled": "settled",
    # An epic-level hold: `kew epic unhold`. Amber, and a person's move.
    "held": "waiting_human",
    # --- run: RunStatus (kew/run_store.py), all 9 ---------------------------
    # Refined by owner liveness — see `run_motion`.
    "pending": "executing",
    "success": "settled",
    "failure": "settled",
    "refused": "settled",
    "budget_exceeded": "settled",
    "budget_stopped": "settled",
    "budget_stop": "settled",
    "killed": "settled",
    # --- the console's own display words for a run (#2050, kpi.ts) ----------
    # Not RunStatus members: `displayStatus` refines a stored row into these
    # before a surface ever sees it, so the Monitor's live-run table holds
    # `running` where the store holds `pending`. A table that knew only the
    # enum would read the console's own vocabulary as unknown and leave the
    # live rows still. `status.ts` maps the same five words for the same
    # reason.
    "running": "executing",
    "working": "executing",
    "idle": "waiting_kew",
    # A run that went wrong is a finished record, like `failure`; the NODE
    # status `failed` above is open work, and needs a person.
    "flawed": "settled",
    "manual": "waiting_human",
    # --- loop: LoopStatus (kew/run_store.py), all 6 -------------------------
    # Refined by staleness — see `loop_motion`.
    "pursuing": "executing",
    "achieved": "settled",
    "unmet": "settled",
    # The three `_RESUMABLE` loop states (kew/loops/delegate.py): a person
    # restarts each one (`kew loop resume`, with `--budget` for the cap).
    "budget_limited": "waiting_human",
    "needs_human": "waiting_human",
    "paused": "waiting_human",
}


#: Which vocabulary a status word was handed over in.
#:
#: Only needed for a word two vocabularies spell the same and read differently,
#: which today is exactly one word — see :data:`RUN_MOTION_OF`. ``"epic"`` is
#: the default, and deliberately: it is the reading that names a PERSON as the
#: next move, so a caller that forgets to say never silently turns somebody's
#: wait into kew's own.
Vocabulary = Literal["epic", "run"]

#: The run vocabulary's reading of a word :data:`MOTION_OF` maps for a node.
#:
#: Refinements only: every key here is also a key of :data:`MOTION_OF`, and
#: ``tests/test_motion_parity.py`` holds this table to ``motion.ts``'s copy and
#: to that rule, so a second collision cannot land unnoticed.
#:
#: ``queued`` is the collision. For a NODE it is work nobody has armed — the
#: Linear adapter returns it precisely when the marker a person adds is absent
#: (#2209) — so the next move is a person's. For a RUN it is display-only
#: (``displayStatus`` in the console's kpi.ts): an untouched ``pending`` row
#: with no steps and no start time, which the dispatcher picks up on its own
#: turn and nobody has to touch. Same word, opposite answer to "who moves
#: next", which is the only question this module asks. ``status.ts`` splits the
#: same word for the same reason (its ``RUN_MEANING_OF``).
RUN_MOTION_OF: dict[str, Motion] = {
    "queued": "waiting_kew",
}


def normalize(raw: str) -> str:
    """``in-progress``, ``In Progress`` and ``in_progress`` are one status."""
    return "_".join(raw.replace("-", " ").lower().split())


def motion_of(status: str | None, *, vocabulary: Vocabulary = "epic") -> Motion:
    """The motion of one status word, by the tables alone.

    Never raises and never answers nothing: an unrecognised or missing word
    degrades to :data:`FALLBACK_MOTION`, the same forward-compatible rule
    ``motion.ts`` follows.

    Pass ``vocabulary="run"`` when the word was handed over as a RUN's status;
    it changes only the words the two vocabularies spell the same and read
    differently (:data:`RUN_MOTION_OF`).

    This is the UNREFINED reading. A ``pending`` run and a ``pursuing`` loop
    need one more fact each before the console may animate them — see
    :func:`run_motion` and :func:`loop_motion`.
    """
    if not status:
        return FALLBACK_MOTION
    word = normalize(status)
    if vocabulary == "run" and word in RUN_MOTION_OF:
        return RUN_MOTION_OF[word]
    return MOTION_OF.get(word, FALLBACK_MOTION)


def is_alive(motion: str) -> bool:
    """Whether this motion is an open concern rather than history."""
    return motion != "settled"


def run_motion(status: str | None, *, owner_alive: bool | None) -> Motion:
    """The motion of one run, refined by whether its owner is still alive.

    A ``pending`` row is only ``executing`` while something is actually running
    it; a run whose owning process group is PROVABLY gone is an orphan kew owes
    a reap, which is ``waiting_kew``.

    ``owner_alive=None`` means undeterminable — no recorded owner, or a
    container the cheap probe cannot ask about — and keeps ``executing``. That
    is the same asymmetry ``tick.py`` reaps on: act on ``False``, never on
    ``None``, so a run that is live or mid-startup is never declared dead by
    something that merely could not see it.

    The word is read in the RUN vocabulary, which is what tells a run's
    display-only ``queued`` — the dispatcher's own next turn — from the epic
    node status of the same name, where a person has to arm the work
    (:data:`RUN_MOTION_OF`).
    """
    motion = motion_of(status, vocabulary="run")
    if motion == "executing" and owner_alive is False:
        return "waiting_kew"
    return motion


def loop_motion(
    status: str | None,
    *,
    age_secs: float | None,
    stale_after_secs: float = LOOP_STALE_AFTER_SECS,
) -> Motion:
    """The motion of one goal loop, refined by how long since it last moved.

    ``age_secs`` is how long the loop has been QUIET in seconds — the age of the
    newest thing it wrote, not of ``updated_at`` alone, since ``add_iteration``
    never touches that column (``service.loops._quiet_secs`` is the reading) —
    or ``None`` when it recorded nothing readable. A ``pursuing`` loop quiet for
    longer than ``stale_after_secs`` is stale: it still claims to be pursuing a
    goal, but nothing has advanced it for longer than one iteration can take, so
    it stops moving on screen. An unrecorded age cannot establish staleness and
    leaves the table's reading alone.

    ``stale_after_secs`` is the window, defaulting to
    :data:`LOOP_STALE_AFTER_SECS`. A caller that knows the loop's OWN budget
    passes that instead — ``service.loops`` passes the same window its ``stale``
    flag is read against, so one DTO can never say a loop is both moving
    (``stale=False``) and stopped (``motion="waiting_kew"``).
    """
    motion = motion_of(status)
    if motion == "executing" and age_secs is not None and age_secs > stale_after_secs:
        return "waiting_kew"
    return motion
