"""Prompt profiles: propose → approve → audit → write an operator addendum (#3020).

A profile is an addendum to a role's packaged prompt, never a replacement:
replacing a base still takes a merged PR. This module is the only writer of
the ``prompt_profile_versions`` rows (:mod:`kew.run_store_prompt_profiles`),
and it follows :func:`kew.service.config.set_human_review_override`:

* **validate** — the role must be one :data:`kew.service.prompts.ROLES` lets a
  profile apply to, and the body at most :data:`MAX_BODY_CHARS` characters
  after the reserved fence vocabulary is stripped from it, as the prompt
  renderer's ``finalize`` strips it (:func:`kew.fence_vocabulary.desentinel`);
* **gate** — every proposal and decision needs an actor in
  ``[approval].approvers`` (:func:`kew.approval._authorized`), and an empty
  list refuses every one (#3086): a profile renders as operator instructions,
  so the approval hold's "empty = anyone" rule does not carry over. A refusal
  raises :class:`~kew.service.config.OverrideDenied` before anything is
  written;
* **audit and act together** — the chained audit event and the state row it
  attests commit in one store transaction, the event first and the row
  carrying its id, so a refusal or a failed write leaves neither;
* **compare-and-swap** — an approval names the role generation it saw, and a
  stale one raises :class:`~kew.service.config.StaleGeneration` and changes
  nothing.

A proposal is gated before its version is allocated. It changes nothing a role
runs until an approver activates it, and it is attested like every decision.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from pydantic import BaseModel

from kew.approval import _authorized
from kew.audit import (
    Actor,
    bind_policy_context,
    current_policy_hash,
    record_current_policy,
    resolve_actor,
)
from kew.fence_vocabulary import desentinel
from kew.run_store_outcomes import READY_VERDICTS
from kew.run_store_prompt_profiles import (
    ACTIVE,
    RETIRED,
    ImplementerItemOutcome,
    ProfileAudit,
    PromptProfileConflict,
    PromptProfileVersion,
)
from kew.service.config import OverrideDenied, StaleGeneration
from kew.service.prompts import ROLES, get_role

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.prompt_addendum import ProfileAddendum
    from kew.run_store import RunStore, SqliteRunStore

__all__ = [
    "BUILT_IN",
    "MAX_BODY_CHARS",
    "MIN_COMPARABLE",
    "PROFILE_ROLES",
    "REVIEWER_ROLE",
    "ActiveProfile",
    "ImplementerVersionOutcome",
    "OverrideDenied",
    "PromptProfileConflict",
    "PromptProfileNotFound",
    "PromptOutcomesDTO",
    "PromptProfileRefused",
    "PromptProfileRefusedOnApproval",
    "ReviewerVersionOutcome",
    "StaleGeneration",
    "UNCONFIGURED_APPROVERS",
    "active",
    "addendum_refusal",
    "approve",
    "block_opener_refusal",
    "can_decide",
    "decide_refusal",
    "current_generation",
    "decline",
    "launch_addendum",
    "outcomes",
    "preview_addendum",
    "preview_profile",
    "propose",
    "retire",
]

#: The longest addendum an operator may propose, counted after cleaning.
MAX_BODY_CHARS = 4000

# A line that opens a Markdown block an addendum could leave open: a fenced
# code block (three or more backticks or tildes) or an HTML block (``<`` at the
# line start), each after up to three spaces. A fixed rule, never a parser:
# exact CommonMark pairing is a long tail an approved body could slip through.
_BLOCK_OPENER = re.compile(r"^ {0,3}(```|~~~|<)")

#: The roles a profile may apply to, read off the prompt catalog.
PROFILE_ROLES: frozenset[str] = frozenset(r.role for r in ROLES if r.profile_applies)

PROFILE_PROPOSED_EVENT = "prompt_profile_proposed"
PROFILE_ACTIVATED_EVENT = "prompt_profile_activated"
PROFILE_DECLINED_EVENT = "prompt_profile_declined"
PROFILE_RETIRED_EVENT = "prompt_profile_retired"


class PromptProfileRefused(ValueError):
    """The proposal is not one a profile may carry (role, length, emptiness)."""


class PromptProfileRefusedOnApproval(PromptProfileRefused):
    """The proposed row is not one an approval may activate (#3091).

    Its body does not match the digest it was proposed under, or carries a
    block opener: an activation would leave a profile every launch refuses.
    """


class PromptProfileNotFound(LookupError):
    """The named version does not exist, or the role has no active addendum."""


class ActiveProfile(NamedTuple):
    """The addendum a role runs under: what a run records and renders."""

    id: int
    version: int
    body: str
    body_sha256: str


def _require_profile_role(role: str) -> None:
    if role not in PROFILE_ROLES:
        raise PromptProfileRefused(
            f"a prompt profile may not apply to role {role!r};"
            f" profile roles are {sorted(PROFILE_ROLES)}"
        )


def _clean_body(body: str) -> str:
    """Strip whitespace and the reserved fence vocabulary, then bound it."""
    cleaned = desentinel(body.strip())
    if not cleaned:
        raise PromptProfileRefused("a prompt profile body may not be empty")
    if len(cleaned) > MAX_BODY_CHARS:
        raise PromptProfileRefused(
            f"a prompt profile body is at most {MAX_BODY_CHARS} characters,"
            f" not {len(cleaned)}"
        )
    _require_no_block_opener(cleaned)
    return cleaned


def block_opener_refusal(body: str) -> str | None:
    """Why *body* may not render above the safety section, or None.

    The addendum renders just above the safety section, so a block it opened
    would carry the safety heading and the precedence line into code or HTML.
    An addendum is operator instructions: inline ``code`` is all it needs, so
    any line that could open a block is refused rather than paired. A
    ``<!--`` mid-line opens no block: an unclosed inline comment ends with its
    paragraph, and the blank line after the addendum ends that (#3046).
    """
    for number, line in enumerate(body.splitlines(), start=1):
        found = _BLOCK_OPENER.match(line)
        if found is not None:
            kind = "an HTML block" if found.group(1) == "<" else "a code fence"
            return (
                f"line {number} opens {kind} ({found.group(1)!r} at the line"
                " start); a prompt profile body may not contain a block opener,"
                " so the safety rules after it still render — use inline `code`"
            )
    return None


def _require_no_block_opener(body: str) -> None:
    reason = block_opener_refusal(body)
    if reason is not None:
        raise PromptProfileRefused(f"a prompt profile body is refused: {reason}")


#: The refusal every profile decision gets while ``[approval].approvers`` is
#: empty; the CLI prints it and the API returns it as the 403's detail.
UNCONFIGURED_APPROVERS = (
    "Set [approval].approvers in kew.toml before deciding prompt profiles."
)

#: The refusal every profile write gets from a server started without a
#: config; the API returns it as the 503's detail.
NO_CONFIG = (
    "This server was started without a configuration, so no approver can be checked."
)


def authorize(config: DispatchConfig, actor: Actor | None = None) -> Actor:
    """The approver gate alone: *actor*, the acting human by default, or raise.

    Fails closed: with no approvers configured nobody may decide a profile.
    Reads nothing but *config*, so a caller can run it before it opens a
    ledger and a refused actor leaves the filesystem unchanged (#3080).
    """
    who = actor or resolve_actor()
    if not config.approval.approvers:
        raise OverrideDenied(UNCONFIGURED_APPROVERS)
    if not _authorized(config, who.id):
        raise OverrideDenied(f"actor {who.id!r} is not in [approval].approvers")
    return who


def check_proposal(role: str, body: str) -> None:
    """Raise :class:`PromptProfileRefused` for what :func:`propose` refuses.

    The input checks alone, with no store: a caller runs them before it
    opens a ledger, and :func:`propose` still runs them again.
    """
    _require_profile_role(role)
    _clean_body(body)


def check_role(role: str) -> None:
    """Raise :class:`PromptProfileRefused` for a role no profile applies to."""
    _require_profile_role(role)


def _gate(store: SqliteRunStore, config: DispatchConfig, actor: Actor) -> str | None:
    """The approver gate, then the policy binding the decision is made under."""
    authorize(config, actor)
    bind_policy_context(config)
    record_current_policy(store)
    return current_policy_hash()


def can_decide(config: DispatchConfig | None, actor: Actor | None = None) -> bool:
    """Whether *actor*, the acting human by default, passes the approver gate.

    What the console reads to know whether a decision control is the viewer's
    to use (#3092). No config, or an empty approver list, refuses every
    write (#3086), so nobody can decide then.
    """
    if config is None or not config.approval.approvers:
        return False
    return _authorized(config, (actor or resolve_actor()).id)


def decide_refusal(config: DispatchConfig | None) -> str | None:
    """The server's sentence for why nobody may write a profile, or None.

    Set only while nobody can pass the gate, and always the sentence that
    write's refusal carries, so the console names the fix rather than say
    only that an approver is needed (#3119): ``NO_CONFIG`` without a config,
    ``UNCONFIGURED_APPROVERS`` with no approvers. A viewer who is merely not
    an approver gets None and the console's own sentence.
    """
    if config is None:
        return NO_CONFIG
    if not config.approval.approvers:
        return UNCONFIGURED_APPROVERS
    return None


def _audit(
    *,
    payload: dict[str, Any],
    config: DispatchConfig,
    actor: Actor,
    policy_hash: str | None,
    now: datetime,
) -> ProfileAudit:
    """The audit row the store chains in the decision's own transaction."""
    return ProfileAudit(
        by=actor.id,
        at=now.isoformat(),
        actor_type=actor.type,
        repo=config.project.repo or None,
        policy_hash=policy_hash,
        payload={**payload, "actor": actor.id},
    )


def _version_row(
    store: SqliteRunStore, role: str, version: int
) -> PromptProfileVersion:
    row = store.get_prompt_profile_version(role, version)
    if row is None:
        raise PromptProfileNotFound(f"{role} has no profile v{version}")
    return row


def _require_state(row: PromptProfileVersion, state: str) -> None:
    if row.state != state:
        raise PromptProfileConflict(
            f"{row.role} profile v{row.version} is {row.state}, not {state}"
        )


def current_generation(store: SqliteRunStore, role: str) -> int:
    """*role*'s generation, the value an approval must name."""
    return store.prompt_profile_generation(role)


def active(store: SqliteRunStore, role: str) -> ActiveProfile | None:
    """*role*'s active addendum, or None when it runs the bare base prompt."""
    row = store.active_prompt_profile(role)
    if row is None:
        return None
    return ActiveProfile(row.id, row.version, row.body, row.body_sha256)


_PROFILE_READS = ("active_prompt_profile", "prompt_profile_generation")


def _profile_reader(store: RunStore | None) -> SqliteRunStore | None:
    """*store* when it can read approved profiles, or None for a stand-in.

    Asked of the store's class, not answered by ``isinstance``: a test that
    drops ``kew.run_store`` from ``sys.modules`` leaves two ``SqliteRunStore``
    classes, and the host store, built from the other one, read as a stand-in
    whose launch silently ran without its approved profile (#3115). The class,
    not the instance, so a ``MagicMock`` that answers every name is no reader.
    """
    if store is None or not all(
        callable(getattr(type(store), name, None)) for name in _PROFILE_READS
    ):
        return None
    return cast("SqliteRunStore", store)


def launch_addendum(
    store: RunStore | None, role: str = "dispatch"
) -> ProfileAddendum | None:
    """The addendum a *role* launch renders and records, or None (#3022).

    Read from the operator's host store and nowhere else: a store that is not
    the host ``dispatch.db`` (none, or a stand-in) carries no approved profile,
    so the launch runs the bare base prompt.

    The active body must match the ``body_sha256`` it was approved under, and
    is checked again with the rule :func:`propose` applies, so a row changed
    outside the service or approved before that rule raises
    :class:`~kew.prompt_addendum.ProfileAddendumRefusedError` — a prompt configuration
    refusal — and never renders.
    """
    from kew.prompt_addendum import ProfileAddendum, ProfileAddendumRefusedError

    reader = _profile_reader(store)
    if reader is None:
        return None
    found = active(reader, role)
    if found is None:
        return None
    reason = addendum_refusal(found)
    if reason is not None:
        raise ProfileAddendumRefusedError(
            f"active {role} prompt profile v{found.version} is refused: {reason}"
        )
    return ProfileAddendum(
        version_id=found.id, role=role, version=found.version, body=found.body
    )


def addendum_refusal(found: ActiveProfile) -> str | None:
    """Why a launch refuses to render *found*, or None when it renders.

    The one check :func:`launch_addendum` raises on and
    :func:`preview_addendum` reports, so the launch and the preview agree.
    """
    # The rendered bytes must be the ones approved: a body changed outside the
    # service would otherwise render under the approved version id.
    if hashlib.sha256(found.body.encode()).hexdigest() != found.body_sha256:
        return (
            "its body does not match the approved digest; retire the profile"
            " and propose it again"
        )
    reason = block_opener_refusal(found.body)
    if reason is not None:
        return f"{reason}; retire the profile and propose one without it"
    return None


def preview_addendum(
    store: RunStore | None, role: str
) -> tuple[ProfileAddendum | None, str]:
    """The addendum a *role* preview renders, and the variant that names it.

    A read path, never launch admission: it raises no refusal. A row the
    launch would render comes back with ``"v<N>"``; a row the launch would
    refuse comes back as None with ``"refused: <reason>"``, in the launch's
    words; no active row, or no host store, is None with ``"none"``.
    """
    from kew.prompt_addendum import ProfileAddendum

    reader = _profile_reader(store)
    found = active(reader, role) if reader is not None else None
    if found is None:
        return None, "none"
    reason = addendum_refusal(found)
    if reason is not None:
        return None, f"refused: {reason}"
    addendum = ProfileAddendum(
        version_id=found.id, role=role, version=found.version, body=found.body
    )
    return addendum, f"v{found.version}"


#: Reads of a role's active row that a concurrent decision may interrupt.
_PREVIEW_READ_ATTEMPTS = 3


def preview_profile(
    store: RunStore | None, role: str
) -> tuple[ProfileAddendum | None, str, int | None]:
    """:func:`preview_addendum`, and the role generation it was read at.

    The generation is read before and after the active row, and the read is
    retried while a decision lands in between, so the console can tell a
    preview from the history read beside it apart when they come from two
    decisions. None is no host store, or a role that changed inside every
    attempt: a generation the reader cannot match to anything.
    """
    reader = _profile_reader(store)
    if reader is None:
        return (*preview_addendum(store, role), None)
    for _ in range(_PREVIEW_READ_ATTEMPTS):
        generation = current_generation(reader, role)
        addendum, label = preview_addendum(reader, role)
        if current_generation(reader, role) == generation:
            return addendum, label, generation
    return addendum, label, None


def propose(
    store: SqliteRunStore,
    *,
    role: str,
    body: str,
    config: DispatchConfig,
    actor: Actor | None = None,
    now: datetime | None = None,
) -> PromptProfileVersion:
    """Record *body* as *role*'s next proposed addendum.

    Raises :class:`OverrideDenied` outside the approver gate. Raises
    :class:`PromptProfileRefused` for an invalid role, an empty or long body,
    or one with a line that could open a Markdown block
    (:func:`block_opener_refusal`).
    """
    _require_profile_role(role)
    cleaned = _clean_body(body)
    moment = now or datetime.now(UTC)
    who = actor or resolve_actor()
    policy_hash = _gate(store, config, who)
    digest = hashlib.sha256(cleaned.encode()).hexdigest()
    version = store.next_prompt_profile_version(role)
    audit = _audit(
        payload={"role": role, "version": version, "body_sha256": digest},
        config=config,
        actor=who,
        policy_hash=policy_hash,
        now=moment,
    )
    return store.insert_prompt_profile_proposal(
        role=role,
        version=version,
        body=cleaned,
        body_sha256=digest,
        event=PROFILE_PROPOSED_EVENT,
        audit=audit,
    )


def approve(
    store: SqliteRunStore,
    *,
    role: str,
    version: int,
    generation: int,
    config: DispatchConfig,
    actor: Actor | None = None,
    now: datetime | None = None,
) -> PromptProfileVersion:
    """Activate *role*'s proposed *version*, retiring the addendum it replaces.

    Raises :class:`OverrideDenied` for an actor outside the approver gate,
    :class:`StaleGeneration` when *generation* is no longer the role's, and
    :class:`PromptProfileConflict` for a version that is not ``proposed``, and
    :class:`PromptProfileRefusedOnApproval` for one whose body does not match
    its digest or carries a block opener. Each refusal writes nothing.
    """
    _require_profile_role(role)
    moment = now or datetime.now(UTC)
    who = actor or resolve_actor()
    policy_hash = _gate(store, config, who)
    current = store.prompt_profile_generation(role)
    if generation != current:
        raise StaleGeneration(
            f"{role} profile generation {generation} is stale; {current} is in force"
        )
    row = _version_row(store, role, version)
    _require_state(row, "proposed")
    _require_approvable(row)
    prior = store.active_prompt_profile(role)
    audit = _audit(
        payload={
            "role": role,
            "version": version,
            "body_sha256": row.body_sha256,
            "generation": current + 1,
            "previous_generation": current,
            "retired_version": None if prior is None else prior.version,
        },
        config=config,
        actor=who,
        policy_hash=policy_hash,
        now=moment,
    )
    try:
        return store.activate_prompt_profile(
            role=role,
            version=version,
            expected_generation=current,
            event=PROFILE_ACTIVATED_EVENT,
            audit=audit,
        )
    except PromptProfileConflict as exc:
        raise StaleGeneration(str(exc)) from exc


def _require_approvable(row: PromptProfileVersion) -> None:
    """Refuse a proposal whose bytes are not the ones it was proposed as.

    The approval attests the body, so it checks what :func:`addendum_refusal`
    checks at launch: an active row every launch refuses is never made.
    """
    if hashlib.sha256(row.body.encode()).hexdigest() != row.body_sha256:
        reason = (
            "its body does not match its recorded digest; decline it and"
            " propose it again"
        )
    else:
        opener = block_opener_refusal(row.body)
        if opener is None:
            return
        reason = f"{opener}; decline it and propose one without it"
    raise PromptProfileRefusedOnApproval(
        f"{row.role} profile v{row.version} is refused: {reason}"
    )


def decline(
    store: SqliteRunStore,
    *,
    role: str,
    version: int,
    config: DispatchConfig,
    actor: Actor | None = None,
    now: datetime | None = None,
) -> PromptProfileVersion:
    """Decline *role*'s proposed *version*; what the role runs is unchanged."""
    _require_profile_role(role)
    moment = now or datetime.now(UTC)
    who = actor or resolve_actor()
    policy_hash = _gate(store, config, who)
    row = _version_row(store, role, version)
    _require_state(row, "proposed")
    audit = _audit(
        payload={"role": role, "version": version, "body_sha256": row.body_sha256},
        config=config,
        actor=who,
        policy_hash=policy_hash,
        now=moment,
    )
    return store.decline_prompt_profile(
        role=role,
        version=version,
        event=PROFILE_DECLINED_EVENT,
        audit=audit,
    )


def retire(
    store: SqliteRunStore,
    *,
    role: str,
    generation: int,
    config: DispatchConfig,
    actor: Actor | None = None,
    now: datetime | None = None,
) -> PromptProfileVersion:
    """Retire *role*'s active addendum, leaving it on the bare base prompt.

    *generation* is the role's generation the caller saw, as for
    :func:`approve`: a retire against a role another decision has changed
    since raises :class:`StaleGeneration`, so it never retires a replacement
    the caller did not see. Raises :class:`PromptProfileNotFound` when the role
    has no addendum.
    """
    _require_profile_role(role)
    moment = now or datetime.now(UTC)
    who = actor or resolve_actor()
    policy_hash = _gate(store, config, who)
    previous_generation = store.prompt_profile_generation(role)
    if generation != previous_generation:
        raise StaleGeneration(
            f"{role} profile generation {generation} is stale;"
            f" {previous_generation} is in force"
        )
    current = store.active_prompt_profile(role)
    if current is None:
        raise PromptProfileNotFound(f"{role} has no active prompt profile")
    audit = _audit(
        payload={
            "role": role,
            "version": current.version,
            "body_sha256": current.body_sha256,
            "generation": previous_generation + 1,
            "previous_generation": previous_generation,
        },
        config=config,
        actor=who,
        policy_hash=policy_hash,
        now=moment,
    )
    try:
        return store.retire_prompt_profile(
            role=role,
            version=current.version,
            expected_generation=previous_generation,
            event=PROFILE_RETIRED_EVENT,
            audit=audit,
        )
    except PromptProfileConflict as exc:
        raise StaleGeneration(str(exc)) from exc


# --- Outcomes per version (#3026) -------------------------------------------

#: The role whose outcomes are its verdicts, read off the rounds it recorded
#: (``kew.review_dispatch.REVIEWER_PROFILE_ROLE``); every other profile role
#: implements, and is read off the items its runs worked on.
REVIEWER_ROLE = "adversarial_review"

#: Fewer items (rounds, for the reviewer) than this and a version's rates are
#: not shown: the console says there are too few to compare.
MIN_COMPARABLE = 10

#: What a row with no profile version is called: the role's base prompt alone.
BUILT_IN = "built-in"

#: The stored states a version is tabled in even with nothing to count: the
#: ones a run could have carried. A proposed or declined addendum never ran.
_IN_FORCE = frozenset({ACTIVE, RETIRED})


class ImplementerVersionOutcome(BaseModel):
    """One version of an implementer role's prompt, and how its items went.

    Only runs whose prompt reached an agent count; a pre-launch refusal is no
    one's outcome. Each such run of the role is charged with the review rounds
    that FOLLOW it on its item: those recorded from its start until the item's
    next executed run of the role
    (:class:`~kew.run_store_prompt_profiles.ImplementerItemOutcome`). A
    version's rounds on an item are its runs' windows together; its round one
    is the first of them. Ready means an effective verdict
    (``COALESCE(accepted_verdict, verdict)``) of READY or READY_WITH_RESIDUALS.

    ``n`` is the items an executed run of the version worked on (zero for a
    version that has not run yet). ``round_one_items`` is those whose windows
    hold a round. **First-pass approval**, ``first_pass_rate``, is the share
    of ``round_one_items`` whose round one is ready. **Rounds to ready**,
    ``mean_rounds_to_ready``, is the mean over ``ready_items`` (the items whose
    windows reach a ready round) of that round's 1-based position in the
    windows, so a round-one approval counts 1. ``merged_items`` is the items
    whose merge fell in one of the version's windows; each merge is credited
    to one version only. ``median_cost_usd`` is the median, over those merged
    items, of the whole item's cost: every run on the item, whatever its kind
    or version. Each is None when nothing was there to measure.
    """

    version: int | None
    label: str
    state: str | None
    n: int
    comparable: bool
    round_one_items: int
    first_pass_rate: float | None
    ready_items: int
    mean_rounds_to_ready: float | None
    merged_items: int
    median_cost_usd: float | None


class ReviewerVersionOutcome(BaseModel):
    """One version of the reviewer's prompt and the verdicts it returned.

    ``n`` is the rounds recorded under it (zero for a version that has not
    reviewed yet); ``verdicts`` counts them by the reviewer's own verdict.
    """

    version: int | None
    label: str
    state: str | None
    n: int
    comparable: bool
    verdicts: dict[str, int]


class PromptOutcomesDTO(BaseModel):
    """A role's outcomes, one row per version, newest first, built-in last.

    Every active or retired version has a row, run or not, and so does the
    built-in prompt once the role has one; any other version has a row only
    when something was recorded under it.

    ``generation`` is the role's profile generation the rows were read at —
    the ``generation`` its versions read serves — so the console never pairs
    one decision's table with another's history. None is a role a decision
    changed inside every attempt: a generation the reader cannot match to
    anything.
    """

    role: str
    kind: Literal["implementer", "reviewer"]
    min_items: int
    versions: list[ImplementerVersionOutcome] | list[ReviewerVersionOutcome]
    generation: int | None


class _Version(NamedTuple):
    """Where a version id sorts, and what its row is called."""

    version: int | None
    label: str
    state: str | None


def _version_names(store: SqliteRunStore, role: str) -> dict[int | None, _Version]:
    names: dict[int | None, _Version] = {None: _Version(None, BUILT_IN, None)}
    for row in store.list_prompt_profile_versions(role):
        names[row.id] = _Version(row.version, f"v{row.version}", row.state)
    return names


def _tabled(names: dict[int | None, _Version]) -> list[int | None]:
    """The version ids tabled with nothing recorded: every in-force version,
    and the built-in prompt beside them, which ran before the first approval
    and runs again after a retirement."""
    ids: list[int | None] = [k for k, v in names.items() if v.state in _IN_FORCE]
    return [*ids, None] if ids else []


def _name(names: dict[int | None, _Version], version_id: int | None) -> _Version:
    # A version id the role's history does not hold (another role's row) is
    # never folded into built-in: it is named by its id.
    return names.get(version_id) or _Version(None, f"profile #{version_id}", None)


def _newest_first(key: int | None) -> tuple[int, int]:
    return (0, -key) if key is not None else (1, 0)


def _share_ready(verdicts: list[str]) -> float | None:
    if not verdicts:
        return None
    return sum(v in READY_VERDICTS for v in verdicts) / len(verdicts)


def _mean(values: list[int]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _implementer_row(
    name: _Version, items: list[ImplementerItemOutcome]
) -> ImplementerVersionOutcome:
    round_one = [i.first_verdict for i in items if i.first_verdict]
    ready = [i.first_ready_round for i in items if i.first_ready_round is not None]
    merged = [i.cost_usd for i in items if i.merged]
    return ImplementerVersionOutcome(
        version=name.version,
        label=name.label,
        state=name.state,
        n=len(items),
        comparable=len(items) >= MIN_COMPARABLE,
        round_one_items=len(round_one),
        first_pass_rate=_share_ready(round_one),
        ready_items=len(ready),
        mean_rounds_to_ready=_mean(ready),
        merged_items=len(merged),
        median_cost_usd=_median(merged),
    )


def _implementer_outcomes(
    store: SqliteRunStore, role: str
) -> list[ImplementerVersionOutcome]:
    names = _version_names(store, role)
    grouped: dict[int | None, list[ImplementerItemOutcome]] = {
        vid: [] for vid in _tabled(names)
    }
    for item in store.implementer_item_outcomes(get_role(role).run_kinds):
        grouped.setdefault(item.version_id, []).append(item)
    rows = [
        _implementer_row(_name(names, vid), items) for vid, items in grouped.items()
    ]
    rows.sort(key=lambda r: _newest_first(r.version))
    return rows


def _reviewer_outcomes(
    store: SqliteRunStore, role: str
) -> list[ReviewerVersionOutcome]:
    names = _version_names(store, role)
    grouped: dict[int | None, dict[str, int]] = {vid: {} for vid in _tabled(names)}
    for count in store.reviewer_verdict_counts():
        grouped.setdefault(count.version_id, {})[count.verdict] = count.rounds
    rows = []
    for vid, verdicts in grouped.items():
        name = _name(names, vid)
        n = sum(verdicts.values())
        rows.append(
            ReviewerVersionOutcome(
                version=name.version,
                label=name.label,
                state=name.state,
                n=n,
                comparable=n >= MIN_COMPARABLE,
                verdicts=dict(sorted(verdicts.items())),
            )
        )
    rows.sort(key=lambda r: _newest_first(r.version))
    return rows


#: Reads of a role's outcomes that a concurrent decision may interrupt.
_OUTCOMES_READ_ATTEMPTS = 3


def outcomes(store: SqliteRunStore, role: str) -> PromptOutcomesDTO:
    """*role*'s outcomes per prompt version it ran under (#3026).

    An implementer role charges each run with the review rounds that follow
    it on its item, until the item's next run of the role, grouped by the
    run's ``runs.prompt_profile_version_id``
    (:class:`ImplementerVersionOutcome`); the reviewer groups its rounds by
    ``review_rounds.prompt_profile_version_id``. A null version is the
    built-in prompt. Every active or retired version is tabled, at zero when
    nothing ran under it. A read: nothing is written.

    The version names and states and the metrics are separate queries, so
    the role's generation is read before and after them and the read is
    retried while a decision lands in between (#3089): an approval and a
    launch between the queries would otherwise table the new version as
    proposed with a run under it, a view no committed state holds.
    """
    _require_profile_role(role)
    for _ in range(_OUTCOMES_READ_ATTEMPTS):
        generation = current_generation(store, role)
        dto = _outcomes_read(store, role, generation)
        if current_generation(store, role) == generation:
            return dto
    return dto.model_copy(update={"generation": None})


def _outcomes_read(
    store: SqliteRunStore, role: str, generation: int
) -> PromptOutcomesDTO:
    if role == REVIEWER_ROLE:
        return PromptOutcomesDTO(
            role=role,
            kind="reviewer",
            min_items=MIN_COMPARABLE,
            versions=_reviewer_outcomes(store, role),
            generation=generation,
        )
    return PromptOutcomesDTO(
        role=role,
        kind="implementer",
        min_items=MIN_COMPARABLE,
        versions=_implementer_outcomes(store, role),
        generation=generation,
    )
