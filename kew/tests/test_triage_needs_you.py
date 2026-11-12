"""A triage proposal reaches the needs-you set with its command (#2293, AC4).

Every proposal in shadow mode is a thing waiting on a person, so each one is a
row in the one needs-you query the four console surfaces read
(:func:`kew.service.needs_you.needs_you_set`, #2207). The row has to be
*actionable as printed*: the command it carries is the one an operator pastes,
and this module holds it to that by walking the Typer app — the same guard the
``kew.waits`` templates are under, because the failure #2244 is named for is a
sentence that reads like a command and is not one.

The second thing asserted here is that the row's reason is the POLICY's own
sentence, read back out of the stored decision JSON rather than re-worded by
the service layer: two wordings of one judgement is how surfaces start
disagreeing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command

from kew.cli import app
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore
from kew.service import needs_you
from kew.service.needs_you import TRIAGE_DECIDE_COMMAND
from kew.triage.policy import OUTCOMES
from kew.waits import fill_command


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _propose(store: Any, item_id: str, outcome: str, reason: str, **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "body_digest": f"digest-{item_id}",
        "backend": "local",
        "model": "local-7b",
        "prompt_sha": "prompt-sha",
        "outcome": outcome,
        "decision_json": json.dumps({"outcome": outcome, "reason": reason}),
    }
    fields.update(extra)
    return store.record_triage_decision(item_id, **fields)


class TestTheRowRendersWithItsCommand:
    def test_a_dispatch_proposal_carries_the_decide_command(self, store: Any) -> None:
        _propose(
            store,
            "2293",
            "propose_dispatch",
            "proposes: dispatch (feature, medium, 0.86)",
            issue_num=2293,
            decision_type="feature",
            complexity="medium",
            confidence=0.86,
        )
        [row] = needs_you._triage_rows(store)
        assert row.type == "triage_proposal"
        assert row.command == "kew triage decide 2293"
        assert row.primary_action == "kew triage decide 2293"
        assert row.reason == "proposes: dispatch (feature, medium, 0.86)"
        assert row.label == "dispatch"
        assert (row.item_id, row.issue_num) == ("2293", 2293)
        assert row.consequence == "review"
        assert row.attention_kind == "triage_proposal"

    def test_a_duplicate_proposal_names_the_candidate(self, store: Any) -> None:
        _propose(
            store,
            "2293",
            "duplicate_candidate",
            "possible duplicate of #812",
            duplicate_of=812,
        )
        [row] = needs_you._triage_rows(store)
        assert row.reason == "possible duplicate of #812"
        assert row.label == "close as duplicate"
        assert row.command == "kew triage decide 2293"

    def test_a_security_proposal_says_it_needs_a_person(self, store: Any) -> None:
        _propose(
            store,
            "2293",
            "human_decision",
            "security: needs a person",
            decision_type="security",
        )
        [row] = needs_you._triage_rows(store)
        assert row.reason == "security: needs a person"
        assert row.label == "decide"

    @pytest.mark.parametrize("outcome", sorted(OUTCOMES))
    def test_every_outcome_renders_a_row_with_a_command(
        self, store: Any, outcome: str
    ) -> None:
        """No outcome reaches the console without words and a command."""
        _propose(store, "2293", outcome, f"the policy said {outcome}")
        [row] = needs_you._triage_rows(store)
        assert row.command == "kew triage decide 2293"
        assert row.label, f"{outcome} rendered with no operator wording"
        assert row.reason == f"the policy said {outcome}"

    def test_a_resolved_proposal_leaves_the_set(self, store: Any) -> None:
        write = _propose(store, "2293", "propose_dispatch", "proposes: dispatch")
        store.resolve_triage_decision(
            write.current.id, resolution="dispatch", resolved_by="tom"
        )
        assert needs_you._triage_rows(store) == []

    def test_an_unreadable_decision_record_says_so(self, store: Any) -> None:
        """An unreadable record is not a clean one (rule 4)."""
        _propose(store, "2293", "propose_dispatch", "ignored")
        store._conn.execute(
            "UPDATE triage_decisions SET decision_json = ? WHERE item_id = ?",
            ("{not json", "2293"),
        )
        store._conn.commit()
        [row] = needs_you._triage_rows(store)
        assert "could not be read" in row.reason
        assert row.command == "kew triage decide 2293"


class TestTheCommandIsRunnableAsPrinted:
    @staticmethod
    def _cli_command_paths() -> set[str]:
        def walk(group: Any, prefix: str) -> list[str]:
            paths: list[str] = []
            for name, sub in sorted(getattr(group, "commands", {}).items()):
                path = f"{prefix} {name}".strip()
                paths.extend(walk(sub, path) or [path])
            return paths

        return set(walk(get_command(app), ""))

    def test_the_template_names_a_subcommand_the_app_has(self) -> None:
        known = self._cli_command_paths()
        assert "triage decide" in known, "the app walk found no triage decide command"
        words = TRIAGE_DECIDE_COMMAND.split()
        assert words[0] == "kew"
        assert " ".join(words[1:3]) in known

    def test_the_filled_command_is_the_template_with_the_item_in_it(self) -> None:
        assert fill_command(TRIAGE_DECIDE_COMMAND, item="SFD-12") == (
            "kew triage decide SFD-12"
        )

    def test_a_template_token_never_survives_the_fill(self, store: Any) -> None:
        _propose(store, "2293", "propose_dispatch", "proposes: dispatch")
        [row] = needs_you._triage_rows(store)
        assert "{" not in row.command and "<" not in row.command


class TestTheSourceIsFailSoft:
    def test_a_store_without_the_reader_contributes_nothing(self) -> None:
        """A fake store predating the station is not a failure."""

        class _Legacy:
            pass

        assert needs_you._triage_rows(_Legacy()) == []

    def test_a_failing_reader_is_named_unavailable_rather_than_counted_as_zero(
        self, store: Any
    ) -> None:
        _propose(store, "2293", "propose_dispatch", "proposes: dispatch")

        class _Broken:
            def __getattr__(self, name: str) -> Any:
                return getattr(store, name)

            def list_unresolved_triage_decisions(self) -> list[Any]:
                raise RuntimeError("the ledger predates triage")

        result = needs_you.needs_you_set(_Broken(), DispatchConfig())
        assert "triage_proposals" in result.unavailable
        assert [e for e in result.entries if e.type == "triage_proposal"] == []


class TestTheProposalReachesTheWholeSet:
    def test_the_set_carries_the_row_the_surfaces_read(self, store: Any) -> None:
        _propose(
            store,
            "2293",
            "propose_dispatch",
            "proposes: dispatch (feature, medium, 0.86)",
            issue_num=2293,
        )
        result = needs_you.needs_you_set(store, DispatchConfig())
        [row] = [e for e in result.entries if e.type == "triage_proposal"]
        assert row.command == "kew triage decide 2293"
        assert "triage_proposals" in result.as_of
        assert "triage_proposals" not in result.unavailable
