"""The triage policy proposes; it never decides (#2293, AC1).

:func:`kew.triage.policy.decide` is pure, so every rule is checked here as
arithmetic over a decision, the host's signals and ``[triage]`` config — no
store, no network, no clock.

The load-bearing assertion is the last class: **``unavailable`` never proposes
a dispatch.** A classifier that was unreachable, or whose JSON never validated,
produces a row a person resolves — never work. Everything else in this file
exists so that rule cannot be satisfied by accident (a policy that returned
``unavailable`` for every input would pass it alone).
"""

from __future__ import annotations

import pytest

from kew.config import DispatchConfig, TriageConfig
from kew.triage.policy import DISPATCHABLE, OUTCOMES, decide
from kew.triage.schema import TRIAGE_TYPES, TriageDecision
from kew.triage.signals import DuplicateCandidate, IssueSignals, body_features


def _config(**triage: object) -> DispatchConfig:
    return DispatchConfig(triage=TriageConfig(**triage))  # type: ignore[arg-type]


def _signals(
    *,
    candidates: tuple[int, ...] = (),
    unavailable: tuple[str, ...] = (),
) -> IssueSignals:
    return IssueSignals(
        item_id="2293",
        issue_num=2293,
        body_digest="deadbeef",
        features=body_features("a body"),
        candidates=tuple(
            DuplicateCandidate(number=n, title=f"candidate {n}") for n in candidates
        ),
        unavailable=unavailable,
    )


def _decision(**fields: object) -> TriageDecision:
    base: dict[str, object] = {"type": "feature", "confidence": 0.9}
    base.update(fields)
    return TriageDecision.model_validate(base)


class TestEveryOutcome:
    """One test per outcome the policy can return — all six, by name."""

    def test_an_actionable_decision_proposes_a_dispatch(self) -> None:
        outcome = decide(
            _decision(actionable=True, complexity="medium", confidence=0.86),
            _signals(),
            _config(),
        )
        assert outcome.kind == "propose_dispatch"
        assert outcome.reason == "proposes: dispatch (feature, medium, 0.86)"

    def test_needs_info_asks(self) -> None:
        outcome = decide(
            _decision(needs_info=True, questions=["which repo?"]),
            _signals(),
            _config(),
        )
        assert outcome.kind == "ask"
        assert outcome.questions == ("which repo?",)
        assert outcome.reason == "proposes: ask (feature, 1 question)"

    def test_questions_without_an_actionable_body_ask(self) -> None:
        outcome = decide(
            _decision(questions=["which repo?", "which version?"]),
            _signals(),
            _config(),
        )
        assert outcome.kind == "ask"
        assert outcome.reason == "proposes: ask (feature, 2 questions)"

    def test_an_offered_duplicate_is_a_duplicate_candidate(self) -> None:
        outcome = decide(
            _decision(actionable=True, duplicate_of=812),
            _signals(candidates=(812, 900)),
            _config(),
        )
        assert outcome.kind == "duplicate_candidate"
        assert outcome.duplicate_of == 812
        assert outcome.reason == "possible duplicate of #812"

    @pytest.mark.parametrize("human_type", ["security", "question"])
    def test_a_human_type_is_a_persons_decision(self, human_type: str) -> None:
        outcome = decide(
            _decision(type=human_type, actionable=True, confidence=1.0),
            _signals(),
            _config(),
        )
        assert outcome.kind == "human_decision"
        assert outcome.reason == f"{human_type}: needs a person"

    def test_low_confidence_is_a_persons_decision(self) -> None:
        outcome = decide(
            _decision(actionable=True, confidence=0.4),
            _signals(),
            _config(min_confidence=0.8),
        )
        assert outcome.kind == "human_decision"
        assert "low confidence 0.40 < 0.80" in outcome.reason

    def test_out_of_scope_is_a_persons_decision(self) -> None:
        """Neither actionable nor short of information: nothing to propose."""
        outcome = decide(
            _decision(actionable=False, questions=[]), _signals(), _config()
        )
        assert outcome.kind == "human_decision"
        assert "not actionable" in outcome.reason

    def test_a_complex_actionable_item_proposes_a_split(self) -> None:
        outcome = decide(
            _decision(actionable=True, complexity="complex", confidence=0.95),
            _signals(),
            _config(),
        )
        assert outcome.kind == "split"
        assert outcome.reason == "proposes: split (feature, complex, 0.95)"

    def test_no_decision_is_unavailable(self) -> None:
        outcome = decide(None, _signals(), _config())
        assert outcome.kind == "unavailable"
        assert "no valid classification" in outcome.reason

    def test_every_outcome_name_is_covered_by_this_class(self) -> None:
        """The six outcomes are a closed set; a seventh needs a test here."""
        covered = {
            "propose_dispatch",
            "ask",
            "duplicate_candidate",
            "human_decision",
            "split",
            "unavailable",
        }
        assert covered == OUTCOMES


class TestOrdering:
    """Earlier rules win, and every rule moves an item towards a person."""

    def test_a_human_type_outranks_a_duplicate_candidate(self) -> None:
        outcome = decide(
            _decision(type="security", actionable=True, duplicate_of=812),
            _signals(candidates=(812,)),
            _config(),
        )
        assert outcome.kind == "human_decision"

    def test_low_confidence_outranks_a_duplicate_candidate(self) -> None:
        outcome = decide(
            _decision(confidence=0.1, duplicate_of=812),
            _signals(candidates=(812,)),
            _config(),
        )
        assert outcome.kind == "human_decision"

    def test_a_duplicate_candidate_outranks_a_dispatch(self) -> None:
        outcome = decide(
            _decision(actionable=True, duplicate_of=812),
            _signals(candidates=(812,)),
            _config(),
        )
        assert outcome.kind == "duplicate_candidate"

    def test_an_empty_human_types_list_lets_a_security_item_through(self) -> None:
        """The gate is config, not a hard-coded type — so it is checked both ways."""
        outcome = decide(
            _decision(type="security", actionable=True),
            _signals(),
            _config(human_types=[]),
        )
        assert outcome.kind == "propose_dispatch"


class TestDuplicateNumbersComeFromTheHost:
    """A ``duplicate_of`` the host never offered is an invention, not a choice."""

    def test_an_unoffered_number_is_dropped(self) -> None:
        outcome = decide(
            _decision(actionable=True, duplicate_of=1),
            _signals(candidates=(812,)),
            _config(),
        )
        assert outcome.kind == "propose_dispatch"
        assert outcome.duplicate_of is None

    def test_an_unoffered_number_on_an_unactionable_item_still_needs_a_person(
        self,
    ) -> None:
        outcome = decide(_decision(duplicate_of=1), _signals(candidates=()), _config())
        assert outcome.kind == "human_decision"
        assert outcome.duplicate_of is None

    def test_no_candidates_means_no_duplicate_outcome_is_reachable(self) -> None:
        outcome = decide(
            _decision(actionable=True, duplicate_of=812), _signals(), _config()
        )
        assert outcome.kind != "duplicate_candidate"


class TestUnavailableNeverProposesDispatch:
    """The fail-closed contract, asserted head-on."""

    def test_unavailable_is_not_a_dispatch_proposal(self) -> None:
        outcome = decide(None, _signals(), _config())
        assert outcome.kind not in DISPATCHABLE
        assert outcome.proposes_dispatch is False

    def test_unavailable_names_what_else_went_unread(self) -> None:
        outcome = decide(None, _signals(unavailable=("dedupe_search",)), _config())
        assert "dedupe_search" in outcome.reason

    @pytest.mark.parametrize("triage_type", sorted(TRIAGE_TYPES))
    @pytest.mark.parametrize("min_confidence", [0.0, 0.8, 1.0])
    @pytest.mark.parametrize("skip_epic_nodes", [True, False])
    def test_no_config_or_type_turns_unavailable_into_a_dispatch(
        self, triage_type: str, min_confidence: float, skip_epic_nodes: bool
    ) -> None:
        """No reachable configuration makes a missing decision dispatchable.

        ``min_confidence = 0.0`` is the permissive extreme and an empty
        ``human_types`` removes the other gate; the type is swept because the
        rule must not depend on what the model would have said.
        """
        outcome = decide(
            None,
            _signals(candidates=(1, 2)),
            _config(
                min_confidence=min_confidence,
                human_types=[],
                skip_epic_nodes=skip_epic_nodes,
            ),
        )
        assert outcome.kind == "unavailable"
        assert outcome.proposes_dispatch is False
        del triage_type  # swept to show the rule is independent of the type

    def test_dispatchable_holds_exactly_one_outcome(self) -> None:
        """Only ``propose_dispatch`` is dispatchable — by construction."""
        assert {"propose_dispatch"} == DISPATCHABLE
        assert "unavailable" not in DISPATCHABLE
