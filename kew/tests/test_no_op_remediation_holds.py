"""A remediation that changes nothing holds the node after one run (#3233).

A standing NEEDS_CHANGES round whose remediation ran on the same head and
published nothing used to be remediated again on every tick: a no-op
remediation records no review round, so the round cap never bounded it. The
node is now held as ``remediation_no_change``, naming the findings left
standing, and the hold is a needs-you event a person releases with
``kew epic unhold``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from support.driver_fakes import EPIC

from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import (
    HOLD_REASON_MAX_CHARS,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    holds_node_out_of_frontier,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.review_outcome import STATUS_COMPLETED, ReviewOutcome, record_review_outcome
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, RunRecord, SqliteRunStore
from kew.waits import AttentionKind, classify_wait, fill_command

ITEM = "3317"
HEAD = "a1b2c3d4e5f6"
#: The head a remediation published when kew's own base integration merged
#: the base into the checkout before the agent ran: merge(HEAD, main).
MERGED = "9e9e9e9e9e9e"
#: The tree that merge left, before the agent ran.
MERGED_TREE = "7a7a7a7a7a7a"
#: A tree with the agent's own edit on top of that merge.
EDITED_TREE = "5b5b5b5b5b5b"
PR = 3400
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
FIRST = "the retry loop never re-reads the lock record after a steal"
SECOND = "the heartbeat writes without checking it still owns the row"


def _finding(summary: str, line: int) -> dict[str, Any]:
    return {
        "severity": "high",
        "location": f"src/kew/graph/driver/lock.py:{line}",
        "summary": summary,
        "evidence": {"command": "uv run pytest tests/x.py", "output_digest": "1 x"},
    }


def _round(store: Any, tmp_path: Path) -> None:
    """An ordinary NEEDS_CHANGES round at HEAD, with two evidenced findings."""
    sidecar = tmp_path / "logs" / "round-2.sidecar.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [_finding(FIRST, 10), _finding(SECOND, 20)],
            }
        )
    )
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=HEAD,
        verdict="NEEDS_CHANGES",
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        sidecar_path=str(sidecar),
    )


def _remediation(run_id: str, *, started_at: datetime) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=PR,
        item_id=ITEM,
        branch=f"agent/issue-{ITEM}",
        status="success",
        exit_code=0,
        started_at=started_at,
        finished_at=started_at,
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
    )


def _facts(head: str = HEAD) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


def _drive(
    store: Any, tmp_path: Path, *, head: str = HEAD
) -> tuple[Any, list[RemediationContext]]:
    calls: list[RemediationContext] = []
    facts = _facts(head)
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=next((h.reason for h in store.list_epic_holds(EPIC)), None),
        review=ReviewOverlay(
            enabled=True,
            reserved_kind=None,
            round_count=1,
            latest_verdict="NEEDS_CHANGES",
            latest_head_sha=HEAD,
            current_head_sha=head,
        ),
    )
    outcome = drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=_config(tmp_path),
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        remediator=calls.append,
        now=NOW,
    )
    return outcome, calls


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=8),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _remediated_after_the_round(
    store: Any,
    tmp_path: Path,
    *,
    cut: str = HEAD,
    published: str = HEAD,
    integrated_tree: str = "",
    published_tree: str = "",
) -> None:
    """A remediation that ran to success after the round: its run row and the
    completed outcome recording the head it was cut at and the head it
    published (equal when it changed nothing) — and, when given, the tree
    kew's base integration left before the agent ran beside the tree it
    published (equal when the agent changed nothing)."""
    recorded = store.latest_review_round(ITEM).recorded_at
    store.save(_remediation("rem-1", started_at=recorded + timedelta(seconds=5)))
    record_review_outcome(
        ReviewOutcome(
            pr_number=PR,
            status=STATUS_COMPLETED,
            stage="runner",
            classification="completed",
            branch=f"agent/issue-{ITEM}",
            head_sha=published,
            item_id=ITEM,
            cut_head_sha=cut,
            integrated_tree=integrated_tree,
            published_tree=published_tree,
        ),
        store=store,
        config=_config(tmp_path),
        notify=False,
    )


def test_a_remediation_that_publishes_nothing_holds_the_node_after_one_run(
    tmp_path: Path,
) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(store, tmp_path)

        outcome, calls = _drive(store, tmp_path)

        assert calls == [], "a second remediation would end the same way"
        assert outcome.held == [ITEM]
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: ")
        assert holds_node_out_of_frontier(reason)

        # The next tick reads the hold and dispatches nothing either.
        outcome, calls = _drive(store, tmp_path)
        assert calls == []
        assert len(store.list_epic_holds(EPIC)) == 1


def test_the_hold_names_the_findings_left_standing(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(store, tmp_path)

        _drive(store, tmp_path)

        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert "2 findings stand" in reason
        assert FIRST in reason
        assert len(reason) <= HOLD_REASON_MAX_CHARS


def test_the_first_remediation_of_a_standing_round_still_runs(
    tmp_path: Path,
) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        recorded = store.latest_review_round(ITEM).recorded_at
        # A remediation of an EARLIER round does not count against this one.
        store.save(_remediation("rem-0", started_at=recorded - timedelta(hours=1)))

        outcome, calls = _drive(store, tmp_path)

        assert [(c.item_id, c.head_sha) for c in calls] == [(ITEM, HEAD)]
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_remediation_row_without_an_outcome_at_an_unmoved_head_holds_no_progress(
    tmp_path: Path,
) -> None:
    """A run row says a remediation ran, not what it published, so it is no
    ``remediation_no_change``. But the PR still stands at the round's head, so
    it pushed nothing, and no second remediation runs there (#3475)."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        recorded = store.latest_review_round(ITEM).recorded_at
        store.save(_remediation("rem-1", started_at=recorded + timedelta(seconds=5)))

        outcome, calls = _drive(store, tmp_path)

        assert calls == []
        assert outcome.held == [ITEM]
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")


def test_an_outcome_without_a_cut_head_at_an_unmoved_head_holds_no_progress(
    tmp_path: Path,
) -> None:
    """An outcome recorded before the cut head was proves no no-op, but the
    head it published is the round's own: nothing was pushed (#3475)."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(store, tmp_path, cut="")

        outcome, calls = _drive(store, tmp_path)

        assert calls == []
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: ")


def test_a_remediation_that_published_a_new_head_is_not_held(tmp_path: Path) -> None:
    """This tick's head read predates the push; the next tick reads the new head."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(store, tmp_path, published="f" * 12)

        outcome, _ = _drive(store, tmp_path)

        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_the_no_change_hold_is_an_attention_event(tmp_path: Path) -> None:
    """The hold reaches `kew epic watch` and the needs-you set with its action
    and the command that releases it."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(store, tmp_path)
        _drive(store, tmp_path)
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]

    wait = classify_wait(reason, held=True)

    assert wait is not None
    assert wait.kind is AttentionKind.HELD
    assert wait.label, "a bucketed hold carries its own words"
    assert wait.action == (
        "read the findings, fix the branch or answer them, then kew epic unhold"
    )
    assert fill_command(wait.command, epic=EPIC, item=ITEM) == (
        f"kew epic unhold {EPIC} {ITEM}"
    )


def test_a_remediation_that_only_merged_the_base_holds_the_node_on_the_merged_head(
    tmp_path: Path,
) -> None:
    """kew merged main into the checkout before the agent ran, and the agent
    changed nothing: the remediation published merge(HEAD, main). The head
    moved, but only by kew's own merge, so the round stands on it and the node
    is held instead of handed to a reviewer as if the findings were answered."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(
            store,
            tmp_path,
            published=MERGED,
            integrated_tree=MERGED_TREE,
            published_tree=MERGED_TREE,
        )

        outcome, calls = _drive(store, tmp_path, head=MERGED)

        assert calls == []
        assert outcome.held == [ITEM]
        (reason,) = [h.reason for h in store.list_epic_holds(EPIC)]
        assert reason.startswith(f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: ")
        assert "moved" not in reason, "the head is the one the no-op published"


def test_an_agent_edit_on_top_of_the_base_merge_answers_the_round(
    tmp_path: Path,
) -> None:
    """The same merge, with the agent's own edit on top: the moved head is its
    work, so a reviewer reads it and nothing is held or remediated."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(
            store,
            tmp_path,
            published=MERGED,
            integrated_tree=MERGED_TREE,
            published_tree=EDITED_TREE,
        )

        outcome, calls = _drive(store, tmp_path, head=MERGED)

        assert calls == []
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []


def test_a_head_pushed_past_a_no_op_publication_is_reviewed(tmp_path: Path) -> None:
    """Only the head the no-op itself published keeps an ordinary round
    standing: a push past it is someone's work, and a reviewer reads it."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _round(store, tmp_path)
        _remediated_after_the_round(
            store,
            tmp_path,
            published=MERGED,
            integrated_tree=MERGED_TREE,
            published_tree=MERGED_TREE,
        )

        outcome, calls = _drive(store, tmp_path, head="1c1c1c1c1c1c")

        assert calls == []
        assert outcome.held == []
        assert store.list_epic_holds(EPIC) == []
