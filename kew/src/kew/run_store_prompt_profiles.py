"""Operator prompt profiles: versioned addenda to an agent role's prompt (#3020).

A profile is an ADDENDUM, never a replacement: each role's packaged prompt
stays the reviewed base, and the effective prompt is the base plus the role's
one ``active`` addendum. Addenda live only here, in the operator's
``dispatch.db``; they are written only through :mod:`kew.service.prompt_profiles`,
behind the approver gate, and never read from a checkout, a worktree, an
environment variable or ``kew.toml``.

One ``prompt_profile_versions`` row per proposal, numbered per role. A row
moves ``proposed`` -> ``active`` | ``declined``, and ``active`` -> ``retired``;
the partial unique index allows one ``active`` row per role.
``decided_by`` / ``decided_at`` / ``audit_event_id`` name the decision that put
the row in its current state, so a row retired by a newer approval carries
that approval's event.

**The role's generation** is the MAX of its rows' ``generation``. Every
decision that changes which addendum a role runs (an approval, a retirement)
stamps ``generation + 1`` on the row it changes, and an approval names the
generation it saw: a stale one is refused instead of silently replacing an
addendum its approver never saw.

**Evidence and state commit together.** Each write here takes the audit row
the service built (:func:`kew.run_store_audit.audit_values`) and, in one
``BEGIN IMMEDIATE`` transaction, re-checks that the state the event describes
is still the state in force, chains the event onto the audit log, then writes
the state row bound to the event's id. A re-check that fails
(:class:`PromptProfileConflict`) or a state write that fails rolls the event
back with it, so the audit stream never names a decision that did not happen.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from kew.run_store_audit import audit_values
from kew.run_store_outcomes import READY_VERDICTS

_T = TypeVar("_T")
_C = TypeVar("_C")

PROPOSED = "proposed"
ACTIVE = "active"
RETIRED = "retired"
DECLINED = "declined"


class PromptProfileConflict(Exception):
    """The role's state moved between the audit event and the state write.

    Also raised for a decision on a version that is not in a state the
    decision applies to (approving a declined row, retiring a role with no
    addendum).
    """


@dataclass(frozen=True)
class PromptProfileVersion:
    """One proposed, active, retired or declined addendum."""

    id: int
    role: str
    version: int
    body: str
    body_sha256: str
    state: str
    proposed_by: str
    proposed_at: str
    decided_by: str | None
    decided_at: str | None
    audit_event_id: int | None
    generation: int


def _row_to_version(row: sqlite3.Row) -> PromptProfileVersion:
    return PromptProfileVersion(
        id=int(row["id"]),
        role=row["role"],
        version=int(row["version"]),
        body=row["body"],
        body_sha256=row["body_sha256"],
        state=row["state"],
        proposed_by=row["proposed_by"],
        proposed_at=row["proposed_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        audit_event_id=row["audit_event_id"],
        generation=int(row["generation"]),
    )


@dataclass(frozen=True)
class ProfileAudit:
    """The audit row a decision chains, all but its event name.

    *by* and *at* are also the row's ``decided_by`` / ``decided_at`` (or
    ``proposed_by`` / ``proposed_at``): the row and its event name one actor
    and one moment.
    """

    by: str
    at: str
    actor_type: str
    repo: str | None
    policy_hash: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Decision:
    """Who decided, when, and the audit event that attests it."""

    by: str
    at: str
    audit_event_id: int


@dataclass(frozen=True)
class ImplementerItemOutcome:
    """One item one version of an implementer role worked on (#3026).

    Only **executed** runs count: a run of the role's kinds whose row shows
    its prompt reached an agent (:data:`_EXECUTED_RUN_SQL`). A pre-launch
    refusal is not an outcome of any prompt, so it neither adds an item nor
    closes a window.

    ``version_id`` is the ``prompt_profile_version_id`` the role's executed
    runs on the item recorded (None for the built-in prompt); an item two
    versions ran on is one outcome per version. Each executed run owns a
    **review window** on its item: the rounds recorded at or after the run
    launched and before the item's next executed run of the role's kinds
    launched (after the last run: every later round). A run launches at its
    ``started`` receipt's ``launched_at``, not at admission
    (``runs.started_at``), so a round or merge between the two is never its;
    a run with no launch time (no receipt, or one settled ``unknown``) falls
    back to its ``started_at``. A version's rounds on the
    item are the union of its runs' windows, oldest row first; a round
    recorded before the role's first run on the item, or in another version's
    window, is never its.

    ``first_verdict`` is the effective verdict,
    ``COALESCE(accepted_verdict, verdict)``, of the version's first round (None
    when its windows hold no round). ``first_ready_round`` is the 1-based
    ordinal, within those rounds, of the first whose effective verdict is in
    :data:`~kew.run_store_outcomes.READY_VERDICTS` (None when none is). It
    counts rows rather than reading ``round``, which restarts at 1 after a
    ``kew epic retry``: superseded rounds in the window still cost rounds.
    ``cost_usd`` is the whole item's cost: what every run on the item spent,
    whatever its kind, version or status (dispatch, remediation, resolve, CI
    fix and the rest). Every version that worked on the item carries the same
    figure; only the version credited with the merge counts it.
    ``merged`` is true for exactly one version of a merged item, the one whose
    window holds the merge: the version of the item's last executed run of the
    role that launched at or before the earliest ``pr_merged_at`` on the item,
    or of its last such run when the merge is known only by
    ``pr_state = 'MERGED'``. A merge before the role's first run credits none.
    """

    item_id: str
    version_id: int | None
    first_verdict: str | None
    first_ready_round: int | None
    cost_usd: float
    merged: bool


@dataclass(frozen=True)
class ReviewerVerdictCount:
    """How many review rounds one profile version returned *verdict* on."""

    version_id: int | None
    verdict: str
    rounds: int


#: The ``error_category`` values kew writes only for a run whose agent never
#: started: the dispatch refusal and rollback writers in
#: :mod:`kew.dispatch_core` / :mod:`kew.dispatch_preflight`
#: (``dispatch_child_refused`` is what ``_rollback_admitted_claim`` leaves on
#: an admitted claim that ended before launch) and every category in
#: ``kew.graph.driver.review_stage._PRE_LAUNCH_HOLDS``. Spelled out here
#: because the store sits below both; a test holds it to the
#: driver's table.
NOT_LAUNCHED_CATEGORIES = frozenset(
    {
        "containment_refused",
        "container_start_failed",
        "credential_issuer_refused",
        "credential_missing",
        "dispatch_child_refused",
        "github_auth",
        "github_unreachable",
        "launch_failed",
        "prompt_configuration_refused",
        "requeued_over_cap",
        "reservation_replaced",
        # Every container create timed out (#3103): the host, not the prompt.
        "runtime_timeout",
        "sandbox_refused",
        # The container refused its agent's startup (#3186).
        "sandbox_startup_refused",
    }
)

#: The statuses a row reaches only after its agent ran, so its duration is
#: the agent's: a pre-launch refusal also times its own few seconds.
_RAN_STATUSES = ("success", "awaiting_publication")


def _sql_strings(values: Iterable[str]) -> str:
    # Code constants only, never input: quoted inline so the predicate needs
    # no placeholders of its own.
    return ", ".join(f"'{v}'" for v in sorted(values))


#: The launch receipt states that say the agent's process started
#: (:mod:`kew.run_store_launches`): ``started`` is recorded at the launch
#: boundary, and ``unknown`` is what a pending receipt settles to when its run
#: ends some other way than a refusal, so it may have started. ``pending``
#: (not launched yet) and ``refused`` (startup refused) say it did not.
_LAUNCHED_RECEIPTS = ("started", "unknown")

#: The predicate a ``runs`` row (joined to its ``run_launches`` receipt as
#: ``launch``) must meet to be an outcome of its prompt. Two exclusions win
#: over everything: the row must NOT be a marked or legacy pre-launch refusal,
#: as :func:`kew.publication_credentials.prelaunch_refusal_without_launch`
#: reads one (``_marked_or_legacy_refusal``: the ``pre_admission_refusal``
#: marker the shared refusal writers set, #3023's
#: ``prompt_configuration_refused`` rows among them, or a legacy
#: containment/reservation refusal), and must NOT carry a
#: :data:`NOT_LAUNCHED_CATEGORIES` category. Then the launch receipt decides:
#: a :data:`_LAUNCHED_RECEIPTS` state counts the run, even one that failed at
#: once with no session, tokens, spend or PR; any other receipt state does
#: not. Only a row with no receipt (one written before receipts existed, or by
#: a path that keeps none) falls back to evidence that exists only once an
#: agent ran: a ``session_id``, spent tokens (total, input or output) or
#: money, a first-edit measurement, a ``pr_url``, or a positive duration on a
#: row whose status (:data:`_RAN_STATUSES`) says the agent ran. The fields kew
#: fills at admission, before any launch (``pgid``, ``owner_pid``,
#: ``log_file``, ``container_name``), prove nothing: an admitted claim rolled
#: back before launch keeps them. Status alone decides nothing either:
#: publication can refuse a run after its agent ran.
_EXECUTED_RUN_SQL = f"""
NOT (
  (pre_admission_refusal = 1 AND status IN ('refused', 'failure', 'killed')
    AND exit_code >= 0)
  OR (status = 'refused'
    AND error_category IN ('containment_refused', 'reservation_replaced')
    AND exit_code IN (1, 2))
)
AND COALESCE(error_category, '') NOT IN ({_sql_strings(NOT_LAUNCHED_CATEGORIES)})
AND (
  launch.state IN ({_sql_strings(_LAUNCHED_RECEIPTS)})
  OR (launch.state IS NULL AND (
    session_id IS NOT NULL OR pr_url IS NOT NULL
    OR steps_to_first_edit IS NOT NULL OR tokens_to_first_edit IS NOT NULL
    OR COALESCE(total_tokens, 0) > 0 OR COALESCE(input_tokens, 0) > 0
    OR COALESCE(output_tokens, 0) > 0 OR COALESCE(estimated_cost_usd, 0) > 0
    OR (COALESCE(duration_seconds, 0) > 0
      AND status IN ({_sql_strings(_RAN_STATUSES)}))
  ))
)
"""

# ``launched`` keeps the executed runs of the role's kinds (read beside their
# launch receipts) and dates each at its launch: a ``started`` receipt's
# ``launched_at``, or the row's admission ``started_at`` only when the run has
# no launch time (a row written before receipts existed, or a receipt settled
# ``unknown``). ``role_runs`` gives each the launch of the item's next one, in
# launch order (``ends_at``); ``windowed`` places each review
# round in the one window it
# falls in (windows on an item never overlap) and numbers it within its
# (item, version); ``attributed`` keeps the first round's verdict and the
# ordinal of the first ready one. ``merges`` dates each merged item by its
# earliest ``pr_merged_at`` (NULL when only ``pr_state`` says MERGED), read
# through ``julianday`` because it is GitHub's ``mergedAt`` text
# (``...T12:00:00Z``), not the store's own ISO stamp, and
# ``merge_owner`` finds the one window holding it (the open, last window for an
# undated merge). ``item_cost`` is every run's spend on each item, whatever
# its kind or version. ``{executed}`` is :data:`_EXECUTED_RUN_SQL`;
# ``{kinds}`` and ``{ready}`` are runs of ``?`` placeholders. The joins seek
# ``idx_review_rounds_item``; ``IS`` matches a null (built-in) version to itself.
_IMPLEMENTER_OUTCOMES_SQL = """
WITH launched AS (
  SELECT item_id, prompt_profile_version_id AS version_id, runs.run_id,
    COALESCE(CASE WHEN launch.state = 'started' THEN launch.launched_at END,
      started_at) AS started_at
  FROM runs LEFT JOIN run_launches launch ON launch.run_id = runs.run_id
  WHERE item_id IS NOT NULL AND run_kind IN ({kinds})
    AND {executed}
),
role_runs AS (
  SELECT item_id, version_id, started_at,
    LEAD(started_at) OVER (
      PARTITION BY item_id ORDER BY started_at, run_id) AS ends_at
  FROM launched
),
windowed AS (
  SELECT w.item_id, w.version_id,
    COALESCE(r.accepted_verdict, r.verdict) AS verdict,
    ROW_NUMBER() OVER (
      PARTITION BY w.item_id, w.version_id ORDER BY r.id) AS nth
  FROM role_runs w JOIN review_rounds r ON r.item_id = w.item_id
    AND r.recorded_at >= w.started_at
    AND (w.ends_at IS NULL OR r.recorded_at < w.ends_at)
),
attributed AS (
  SELECT item_id, version_id,
    MAX(CASE WHEN nth = 1 THEN verdict END) AS first_verdict,
    MIN(CASE WHEN verdict IN ({ready}) THEN nth END) AS first_ready_round
  FROM windowed GROUP BY item_id, version_id
),
merges AS (
  SELECT item_id, MIN(julianday(pr_merged_at)) AS merged_at FROM runs
  WHERE item_id IN (SELECT item_id FROM role_runs)
    AND (pr_state = 'MERGED' OR pr_merged_at IS NOT NULL)
  GROUP BY item_id
),
merge_owner AS (
  SELECT w.item_id, w.version_id
  FROM role_runs w JOIN merges m ON m.item_id = w.item_id
  WHERE (m.merged_at IS NULL AND w.ends_at IS NULL)
    OR (m.merged_at >= julianday(w.started_at)
      AND (w.ends_at IS NULL OR m.merged_at < julianday(w.ends_at)))
),
worked AS (
  SELECT DISTINCT item_id, version_id FROM role_runs
),
item_cost AS (
  SELECT item_id, SUM(COALESCE(estimated_cost_usd, 0.0)) AS cost_usd FROM runs
  WHERE item_id IN (SELECT item_id FROM role_runs)
  GROUP BY item_id
)
SELECT i.item_id, i.version_id, a.first_verdict, a.first_ready_round,
  c.cost_usd, o.item_id IS NOT NULL AS merged
FROM worked i
JOIN item_cost c ON c.item_id = i.item_id
LEFT JOIN attributed a ON a.item_id = i.item_id AND a.version_id IS i.version_id
LEFT JOIN merge_owner o ON o.item_id = i.item_id AND o.version_id IS i.version_id
ORDER BY i.item_id, i.version_id
"""


class PromptProfileStoreMixin:
    """The prompt-profile persistence surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _review_cycle_start(self, item_id: str) -> str | None: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _profile_generation(conn: sqlite3.Connection, role: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM prompt_profile_versions"
            " WHERE role = ?",
            (role,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _profile_row(
        conn: sqlite3.Connection, role: str, version: int
    ) -> PromptProfileVersion | None:
        row = conn.execute(
            "SELECT * FROM prompt_profile_versions WHERE role = ? AND version = ?",
            (role, version),
        ).fetchone()
        return None if row is None else _row_to_version(row)

    @staticmethod
    def _active_profile_row(
        conn: sqlite3.Connection, role: str
    ) -> PromptProfileVersion | None:
        row = conn.execute(
            "SELECT * FROM prompt_profile_versions WHERE role = ? AND state = ?",
            (role, ACTIVE),
        ).fetchone()
        return None if row is None else _row_to_version(row)

    def prompt_profile_generation(self, role: str) -> int:
        """*role*'s generation: 0 until its first approval."""
        return self._profile_generation(self._conn, role)

    def next_prompt_profile_version(self, role: str) -> int:
        """The version number *role*'s next proposal would take."""
        row = self._execute(
            "SELECT COALESCE(MAX(version), 0) FROM prompt_profile_versions"
            " WHERE role = ?",
            (role,),
        ).fetchone()
        return int(row[0]) + 1

    def get_prompt_profile_version(
        self, role: str, version: int
    ) -> PromptProfileVersion | None:
        return self._profile_row(self._conn, role, version)

    def active_prompt_profile(self, role: str) -> PromptProfileVersion | None:
        """*role*'s one active addendum, or None when it runs the bare base."""
        return self._active_profile_row(self._conn, role)

    def list_prompt_profile_versions(self, role: str) -> list[PromptProfileVersion]:
        """Every version of *role*'s addendum, oldest first."""
        rows = self._execute(
            "SELECT * FROM prompt_profile_versions WHERE role = ? ORDER BY version",
            (role,),
        ).fetchall()
        return [_row_to_version(row) for row in rows]

    def run_prompt_profile_version_id(self, run_id: str) -> int | None:
        """The profile version *run_id* ran under; None for none (or no run)."""
        row = self._execute(
            "SELECT prompt_profile_version_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else row[0]

    def review_round_prompt_profile_version_id(
        self, epic_ref: str, item_id: str, round_num: int
    ) -> int | None:
        """The profile version for one round in an item's current cycle."""
        cycle_start = self._review_cycle_start(item_id)
        row = self._execute(
            "SELECT prompt_profile_version_id FROM review_rounds"
            " WHERE epic_ref = ? AND item_id = ? AND round = ?"
            " AND (? IS NULL OR recorded_at >= ?) ORDER BY id DESC LIMIT 1",
            (epic_ref, item_id, round_num, cycle_start, cycle_start),
        ).fetchone()
        return None if row is None else row[0]

    def implementer_item_outcomes(
        self, run_kinds: Sequence[str]
    ) -> list[ImplementerItemOutcome]:
        """Every (item, version) an executed run of *run_kinds* worked on,
        with the review rounds of that version's windows on the item, the
        whole item's cost and whether the item's merge fell in its window
        (:class:`ImplementerItemOutcome`).
        """
        if not run_kinds:
            return []
        sql = _IMPLEMENTER_OUTCOMES_SQL.format(
            executed=_EXECUTED_RUN_SQL.strip(),
            kinds=", ".join("?" * len(run_kinds)),
            ready=", ".join("?" * len(READY_VERDICTS)),
        )
        rows = self._execute(sql, (*run_kinds, *READY_VERDICTS)).fetchall()
        return [
            ImplementerItemOutcome(
                item_id=row["item_id"],
                version_id=row["version_id"],
                first_verdict=row["first_verdict"],
                first_ready_round=row["first_ready_round"],
                cost_usd=float(row["cost_usd"]),
                merged=bool(row["merged"]),
            )
            for row in rows
        ]

    def reviewer_verdict_counts(self) -> list[ReviewerVerdictCount]:
        """Review rounds per profile version recorded on the round, by verdict.

        The reviewer's own ``verdict``, never the driver's acceptance: the
        acceptance is the stop rule's, not the prompt's.
        """
        rows = self._execute(
            "SELECT prompt_profile_version_id AS version_id, verdict,"
            " COUNT(*) AS rounds FROM review_rounds"
            " GROUP BY prompt_profile_version_id, verdict"
        ).fetchall()
        return [
            ReviewerVerdictCount(row["version_id"], row["verdict"], int(row["rounds"]))
            for row in rows
        ]

    # -- writes ------------------------------------------------------------

    def _profile_decision(
        self,
        *,
        event: str,
        audit: ProfileAudit,
        check: Callable[[], _C],
        write: Callable[[_C, _Decision], _T],
    ) -> _T:
        """One decision in one ``BEGIN IMMEDIATE`` transaction, or none of it.

        *check* re-reads the state the event describes and raises
        :class:`PromptProfileConflict` if it moved; the event is then chained,
        and *write* applies the state change bound to its id. Any failure
        rolls the event back with the state.
        """
        values = audit_values(
            timestamp=audit.at,
            event=event,
            repo=audit.repo,
            actor_type=audit.actor_type,
            actor_id=audit.by,
            policy_hash=audit.policy_hash,
            payload=audit.payload,
        )
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                checked = check()
                event_id = self._chained_insert_in_transaction(values)
                if type(event_id) is not int or event_id < 1:
                    raise RuntimeError(f"audit event {event} was not confirmed")
                result = write(checked, _Decision(audit.by, audit.at, event_id))
                self._conn.commit()
                return result
            except BaseException:
                self._conn.rollback()
                raise

    def insert_prompt_profile_proposal(
        self,
        *,
        role: str,
        version: int,
        body: str,
        body_sha256: str,
        event: str,
        audit: ProfileAudit,
    ) -> PromptProfileVersion:
        """Chain *event*, then record a ``proposed`` row as *version*.

        A concurrent proposal that took *version* first is a conflict, not a
        renumbering: the event names this number, so it rolls back with it.
        """

        def _insert(_: None, decision: _Decision) -> PromptProfileVersion:
            try:
                self._conn.execute(
                    "INSERT INTO prompt_profile_versions"
                    " (role, version, body, body_sha256, state, proposed_by,"
                    "  proposed_at, audit_event_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        role,
                        version,
                        body,
                        body_sha256,
                        PROPOSED,
                        decision.by,
                        decision.at,
                        decision.audit_event_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PromptProfileConflict(
                    f"{role} profile v{version} was proposed concurrently"
                ) from exc
            return self._require_row(role, version)

        return self._with_busy_retry(
            "insert_prompt_profile_proposal",
            lambda: self._profile_decision(
                event=event, audit=audit, check=lambda: None, write=_insert
            ),
        )

    def _require_row(self, role: str, version: int) -> PromptProfileVersion:
        row = self._profile_row(self._conn, role, version)
        if row is None:
            raise PromptProfileConflict(f"{role} has no profile v{version}")
        return row

    def _require_proposed(self, role: str, version: int) -> PromptProfileVersion:
        row = self._require_row(role, version)
        if row.state != PROPOSED:
            raise PromptProfileConflict(
                f"{role} profile v{version} is {row.state}, not {PROPOSED}"
            )
        return row

    def _set_state(
        self, row_id: int, state: str, decision: _Decision, generation: int
    ) -> None:
        self._conn.execute(
            "UPDATE prompt_profile_versions SET state = ?, decided_by = ?,"
            " decided_at = ?, audit_event_id = ?, generation = ? WHERE id = ?",
            (
                state,
                decision.by,
                decision.at,
                decision.audit_event_id,
                generation,
                row_id,
            ),
        )

    def activate_prompt_profile(
        self,
        *,
        role: str,
        version: int,
        expected_generation: int,
        event: str,
        audit: ProfileAudit,
    ) -> PromptProfileVersion:
        """Chain *event*, make *version* *role*'s active addendum, and retire
        the prior one.

        Compare-and-swap on the role's generation: *expected_generation* must
        still be the one in force, or nothing changes, the event included. The
        prior active row is retired in the same transaction, so no reader sees
        two active rows or none between them.
        """

        def _check() -> PromptProfileVersion:
            current = self._profile_generation(self._conn, role)
            if current != expected_generation:
                raise PromptProfileConflict(
                    f"{role} profile generation {expected_generation} is stale;"
                    f" {current} is in force"
                )
            return self._require_proposed(role, version)

        def _activate(
            target: PromptProfileVersion, decision: _Decision
        ) -> PromptProfileVersion:
            generation = expected_generation + 1
            prior = self._active_profile_row(self._conn, role)
            if prior is not None:
                self._set_state(prior.id, RETIRED, decision, generation)
            self._set_state(target.id, ACTIVE, decision, generation)
            return self._require_row(role, version)

        return self._with_busy_retry(
            "activate_prompt_profile",
            lambda: self._profile_decision(
                event=event, audit=audit, check=_check, write=_activate
            ),
        )

    def decline_prompt_profile(
        self,
        *,
        role: str,
        version: int,
        event: str,
        audit: ProfileAudit,
    ) -> PromptProfileVersion:
        """Chain *event* and decline a ``proposed`` *version*; the role's
        generation is unchanged."""

        def _decline(
            target: PromptProfileVersion, decision: _Decision
        ) -> PromptProfileVersion:
            self._set_state(target.id, DECLINED, decision, target.generation)
            return self._require_row(role, version)

        return self._with_busy_retry(
            "decline_prompt_profile",
            lambda: self._profile_decision(
                event=event,
                audit=audit,
                check=lambda: self._require_proposed(role, version),
                write=_decline,
            ),
        )

    def retire_prompt_profile(
        self,
        *,
        role: str,
        version: int,
        expected_generation: int,
        event: str,
        audit: ProfileAudit,
    ) -> PromptProfileVersion:
        """Chain *event* and retire *role*'s active *version*, leaving the
        role with no addendum.

        *version* is the active row the event names, and *expected_generation*
        the role's generation the caller saw: if another decision changed
        either since, this one is refused, event and all, rather than retiring
        an addendum the event never described.
        """

        def _check() -> PromptProfileVersion:
            current = self._profile_generation(self._conn, role)
            if current != expected_generation:
                raise PromptProfileConflict(
                    f"{role} profile generation {expected_generation} is stale;"
                    f" {current} is in force"
                )
            active = self._active_profile_row(self._conn, role)
            if active is None or active.version != version:
                raise PromptProfileConflict(
                    f"{role} profile v{version} is no longer the active addendum"
                )
            return active

        def _retire(
            active: PromptProfileVersion, decision: _Decision
        ) -> PromptProfileVersion:
            generation = self._profile_generation(self._conn, role) + 1
            self._set_state(active.id, RETIRED, decision, generation)
            return self._require_row(role, version)

        return self._with_busy_retry(
            "retire_prompt_profile",
            lambda: self._profile_decision(
                event=event, audit=audit, check=_check, write=_retire
            ),
        )
