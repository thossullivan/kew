"""The triage station's policy: one pure function from a decision to an outcome.

:func:`decide` is the whole of kew's judgement about a classifier's answer. It
reads a :class:`~kew.triage.schema.TriageDecision`, the host's
:class:`~kew.triage.signals.IssueSignals`, and ``[triage]`` config, and returns
an :class:`Outcome`. It takes no store, makes no call, touches no tracker, and
has no clock — so every rule below is testable as arithmetic.

In shadow mode (#2293) **every outcome is a proposal.** ``propose_dispatch``
proposes a dispatch; it does not perform one, apply ``ready``, or remove a
human's label. The only thing that acts on a proposal is a person running
``kew triage decide``.

The rules, in the order they are applied — earlier rules win, and every one of
them can only move an item TOWARDS a person:

1. **Unavailable** — no decision at all (the model was unreachable, or its JSON
   never validated after one repair retry). Outcome ``unavailable``. This is
   the fail-closed rule, and it can never produce a dispatch.
2. **A human type** — ``[triage] human_types`` (``security``, ``question`` by
   default). Outcome ``human_decision``: an exposure judgement and a
   conversation are not a model's to route.
3. **Low confidence** — under ``[triage] min_confidence``. Outcome
   ``human_decision``.
4. **A duplicate candidate** — ``duplicate_of`` naming an issue the HOST
   offered. Outcome ``duplicate_candidate``. A number the host never searched up
   is dropped as an invention and the remaining rules decide.
5. **Needs info** — ``needs_info``, or questions with nothing to work on yet.
   Outcome ``ask``.
6. **Too big for one dispatch** — a ``complex`` item the model called
   actionable. Outcome ``split``: kew's unit of work is one PR.
7. **Actionable** — outcome ``propose_dispatch``.
8. Anything left is ``human_decision``: an item that is neither actionable nor
   short of information has nothing a model can propose about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Callable

    from kew.config import DispatchConfig
    from kew.triage.schema import TriageDecision
    from kew.triage.signals import IssueSignals

OutcomeKind = Literal[
    "propose_dispatch",
    "ask",
    "duplicate_candidate",
    "human_decision",
    "split",
    "unavailable",
]
"""The six things triage can propose. Every one of them is a proposal."""

OUTCOMES: frozenset[str] = frozenset(get_args(OutcomeKind))

UNAVAILABLE_OUTCOME: OutcomeKind = "unavailable"
DISPATCH_OUTCOME: OutcomeKind = "propose_dispatch"

#: Outcomes a person can resolve by dispatching. Exactly one, and
#: ``unavailable`` is deliberately not in it (the fail-closed contract).
DISPATCHABLE: frozenset[str] = frozenset({DISPATCH_OUTCOME})


@dataclass(frozen=True)
class Outcome:
    """What triage proposes about one item, and why.

    ``reason`` is the sentence a needs-you row prints, so it names the facts an
    operator would otherwise have to go and read: the type, the size, the
    confidence, the candidate number.
    """

    kind: OutcomeKind
    reason: str
    duplicate_of: int | None = None
    """Only set on ``duplicate_candidate``, and only ever a number the host
    itself offered as a candidate."""
    questions: tuple[str, ...] = field(default_factory=tuple)
    """Only set on ``ask``. The model's questions, verbatim."""

    @property
    def proposes_dispatch(self) -> bool:
        return self.kind in DISPATCHABLE


def decide(
    decision: TriageDecision | None,
    signals: IssueSignals,
    config: DispatchConfig,
) -> Outcome:
    """The triage policy. *decision* is None when no valid decision was obtained.

    Pure: same inputs, same outcome, no I/O. The rules are applied in
    :data:`_RULES` order — the order this module's docstring states — and the
    first one that claims the decision wins.
    """
    if decision is None:
        return _unavailable(signals)
    for rule in _RULES:
        outcome = rule(decision, signals, config)
        if outcome is not None:
            return outcome
    return _out_of_scope(decision)


def _human_type(
    decision: TriageDecision, _signals: IssueSignals, config: DispatchConfig
) -> Outcome | None:
    """Rule 2: a ``[triage] human_types`` classification is a person's to route."""
    if decision.type not in set(config.triage.human_types):
        return None
    return Outcome(kind="human_decision", reason=f"{decision.type}: needs a person")


def _low_confidence(
    decision: TriageDecision, _signals: IssueSignals, config: DispatchConfig
) -> Outcome | None:
    """Rule 3: under ``[triage] min_confidence``, a person decides."""
    floor = config.triage.min_confidence
    if decision.confidence >= floor:
        return None
    return Outcome(
        kind="human_decision",
        reason=(
            f"low confidence {decision.confidence:.2f} < {floor:.2f}"
            f" ({decision.type}): needs a person"
        ),
    )


def _duplicate(
    decision: TriageDecision, signals: IssueSignals, _config: DispatchConfig
) -> Outcome | None:
    """Rule 4: a ``duplicate_of`` the HOST offered as a candidate."""
    duplicate = _offered_duplicate(decision, signals)
    if duplicate is None:
        return None
    return Outcome(
        kind="duplicate_candidate",
        reason=f"possible duplicate of #{duplicate}",
        duplicate_of=duplicate,
    )


def _needs_info(
    decision: TriageDecision, _signals: IssueSignals, _config: DispatchConfig
) -> Outcome | None:
    """Rule 5: the item is short of something a person has to supply."""
    if not (decision.needs_info or (decision.questions and not decision.actionable)):
        return None
    return Outcome(
        kind="ask",
        reason=_ask_reason(decision),
        questions=tuple(decision.questions),
    )


def _too_big(
    decision: TriageDecision, _signals: IssueSignals, _config: DispatchConfig
) -> Outcome | None:
    """Rule 6: kew's unit of work is one PR, so a complex item is split first."""
    if not (decision.actionable and decision.complexity == "complex"):
        return None
    return Outcome(
        kind="split",
        reason=(
            f"proposes: split ({decision.type}, complex, {decision.confidence:.2f})"
        ),
    )


def _dispatchable(
    decision: TriageDecision, _signals: IssueSignals, _config: DispatchConfig
) -> Outcome | None:
    """Rule 7: actionable, confident, not a duplicate — propose the dispatch."""
    if not decision.actionable:
        return None
    return Outcome(
        kind=DISPATCH_OUTCOME,
        reason=(
            f"proposes: dispatch ({decision.type}, {decision.complexity},"
            f" {decision.confidence:.2f})"
        ),
    )


#: The rules, in the order :func:`decide` applies them. Earlier wins, and every
#: one of them can only move an item TOWARDS a person. The order is the
#: contract (tests/test_triage_policy.py::TestOrdering), not an accident of
#: where each rule happens to sit in this file.
_RULES: tuple[
    Callable[[TriageDecision, IssueSignals, DispatchConfig], Outcome | None], ...
] = (
    _human_type,
    _low_confidence,
    _duplicate,
    _needs_info,
    _too_big,
    _dispatchable,
)


def _out_of_scope(decision: TriageDecision) -> Outcome:
    """Rule 8: neither actionable nor short of information — nothing to propose."""
    return Outcome(
        kind="human_decision",
        reason=(
            f"{decision.type} not actionable (out of scope or unclear): needs a person"
        ),
    )


def _unavailable(signals: IssueSignals) -> Outcome:
    """The fail-closed outcome, naming what could not be read.

    A model that never answered and a search that never ran are different
    reasons to stop, and an operator reading one row has to be able to tell
    which happened.
    """
    unread = ", ".join(signals.unavailable)
    detail = f"; also unread: {unread}" if unread else ""
    return Outcome(
        kind=UNAVAILABLE_OUTCOME,
        reason=f"triage unavailable: no valid classification{detail}",
    )


def _offered_duplicate(decision: TriageDecision, signals: IssueSignals) -> int | None:
    """*decision*'s ``duplicate_of``, but only if the host offered that number.

    The candidate list is the menu. A number off the menu is a hallucination,
    and proposing "close #1" because a body said so is exactly the move this
    station must never make — so it is dropped here rather than carried into an
    outcome a person might act on.
    """
    number = decision.duplicate_of
    if number is None:
        return None
    return number if number in signals.candidate_numbers else None


def _ask_reason(decision: TriageDecision) -> str:
    count = len(decision.questions)
    if count:
        plural = "s" if count != 1 else ""
        return f"proposes: ask ({decision.type}, {count} question{plural})"
    return f"proposes: ask ({decision.type}, needs info)"
