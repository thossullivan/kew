"""Plan and decision shapes for the triage station (#2290, #2293).

An :class:`ItemPlan` is what the host understood an item to ask for. Its field
names follow the analyst shape used by Vercel's Foreman software-factory
template (MIT) — summary, problem, approach, affected surfaces, risks,
out-of-scope and open questions around a list of acceptance criteria — so a
later node can fill them from a classifier model without reshaping the ledger.
:class:`TriageDecision`'s fields follow the same reference for the same reason.
No prompt text or code was ported from it — the field NAMES are the shared
vocabulary, and the prompt in ``kew/_templates/triage.md.j2`` was written here
— so no notice attaches to this file.

Every field except ``acceptance_criteria`` is optional: the extract-only
station that lands first fills only the criteria the body already states, and
``source = "extracted"`` on the ledger row says so.

:class:`TriageDecision` is the other half: what a classifier said about an item
(#2293). It is a *proposal* — the station in shadow mode applies nothing to a
tracker, and only a person decides.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

CriterionKind = Literal["test", "behavior", "static"]
"""How a criterion is meant to be checked.

``test`` — an automated test proves it. ``static`` — a gate (lint, type check,
a grep-able invariant) proves it. ``behavior`` — it is judged against observed
behaviour. Extraction cannot tell which a prose bullet means, so it records
``behavior``: the kind a human or a model has to narrow, never a claim that a
test exists.
"""


class AcceptanceCriterion(BaseModel):
    """One criterion an item's output is judged against.

    ``statement`` is kept verbatim from the body — the criteria are the
    contract, and a paraphrase is a different contract.
    """

    model_config = {"extra": "forbid"}

    id: str
    """Stable within one plan: ``AC1``..``ACn`` in the order the body states them."""
    statement: str
    check: str | None = None
    """The concrete command or gate that proves this criterion, when one is
    known. Extraction never invents one."""
    kind: CriterionKind = "behavior"


class ItemPlan(BaseModel):
    """The host's record of what one item asks for."""

    model_config = {"extra": "forbid"}

    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    summary: str | None = None
    problem: str | None = None
    approach: str | None = None
    affected_surfaces: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


TriageType = Literal["bug", "feature", "refactor", "question", "chore", "security"]
"""What an item IS, as a classifier reads it.

``security`` and ``question`` are in :data:`kew.config.TriageConfig.human_types`
by default: one is a judgement about exposure and the other is a conversation,
and neither is a dispatch a model gets to propose past a person.
"""

TRIAGE_TYPES: frozenset[str] = frozenset(get_args(TriageType))
"""The classification vocabulary, as a set, for config validation."""

TRIAGE_BACKENDS: frozenset[str] = frozenset({"local", "runner"})
"""Where the classifier runs. ``local`` is the OpenAI-compatible endpoint in
``[agent.lemonade]``; ``runner`` is a registered runner."""

TriagePriority = Literal["p0", "p1", "p2", "p3"]
TriageComplexity = Literal["simple", "medium", "complex"]


class TriageDecision(BaseModel):
    """What the classifier said about one item. A proposal, never an action.

    Every field is the MODEL's claim. The host does not trust it to be
    actionable: :func:`kew.triage.policy.decide` turns it into an outcome, and
    in shadow mode every outcome is a row a person resolves.

    ``extra = "forbid"`` is load-bearing on the fail-closed contract: a model
    that invents fields has not answered the schema, and a decision that does
    not validate becomes ``unavailable`` rather than a half-read dispatch.
    """

    model_config = {"extra": "forbid"}

    type: TriageType
    priority: TriagePriority = "p2"
    complexity: TriageComplexity = "medium"
    confidence: float = Field(ge=0.0, le=1.0)
    actionable: bool = False
    """Whether the model thinks the item states enough to work on."""
    needs_info: bool = False
    questions: list[str] = Field(default_factory=list)
    """What a person would have to answer first. Carried verbatim."""
    duplicate_of: int | None = None
    """An issue number the model believes this duplicates, chosen from the
    candidates the HOST supplied. A number the host never offered is a
    hallucination, and :func:`kew.triage.policy.decide` drops it."""
    summary: str = ""
